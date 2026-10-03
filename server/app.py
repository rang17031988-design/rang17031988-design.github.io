import os, time, uuid, asyncio, json
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse, Response
from customer_messages import STATUS_LABELS, email_ready, send_customer_email, production_email_allowed, send_resend_test, EmailDeliveryRejected
from paid_metrika import paid_conversion, conversion_csv
from profit_controller import Controller
import pvz_diagnostics

OZON_TOKEN_URL = "https://xapi.ozon.ru/oauth/token"
OZON_API_BASE = "https://api-delivery.ozon.ru/"
CLIENT_ID = os.getenv("OZON_DELIVERY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("OZON_DELIVERY_CLIENT_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
INTERNAL_KEY = os.getenv("OZON_INTERNAL_KEY", "")
PII_FUNCTION_URL = os.getenv("PII_FUNCTION_URL", "")
PII_INTERNAL_KEY = os.getenv("PII_INTERNAL_KEY", "")
ENABLE_REAL_OZON_CREATE = os.getenv("ENABLE_REAL_OZON_CREATE", "false").strip().lower() in ("1", "true", "yes", "on")
PRODUCT_PRICE = os.getenv("PRODUCT_PRICE_RUB", "800")
PRODUCT_WEIGHT_G = int(os.getenv("PRODUCT_WEIGHT_G", "200"))
PRODUCT_LENGTH_MM = int(os.getenv("PRODUCT_LENGTH_MM", "210"))
PRODUCT_WIDTH_MM = int(os.getenv("PRODUCT_WIDTH_MM", "50"))
PRODUCT_HEIGHT_MM = int(os.getenv("PRODUCT_HEIGHT_MM", "50"))

app = FastAPI(title="Snoved Ozon Delivery Gateway", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://xn--163-5cdt3dgrs.xn--p1ai",
        "https://www.xn--163-5cdt3dgrs.xn--p1ai",
        "https://myshop-ddv761.myinsales.ru",
        "https://www.snoved-ai.ru",
        "https://snoved-ai.ru",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Internal-Key"],
    expose_headers=["X-PVZ-Request-ID", "Server-Timing"],
)

_http = httpx.AsyncClient(timeout=20.0, follow_redirects=False)
_token = ""
_token_exp = 0.0
_token_lock = asyncio.Lock()
_points_cache = []
_points_cache_at = 0.0
_points_lock = asyncio.Lock()
db = None
_sync_task = None
_payment_task = None
_profit_task = None
_profit_controller = None
_pvz_export_task = None

def _safe_origin(url: str):
    p = urlsplit(url)
    return (p.scheme.lower(), p.hostname.lower() if p.hostname else "", p.port or (443 if p.scheme == "https" else 80))

async def _pii_call(action: str, payload: dict | None = None):
    if not PII_FUNCTION_URL or not PII_INTERNAL_KEY:
        raise HTTPException(503, "Russian temporary customer-data storage is not configured")
    body = {"action": action, "_internal_key": PII_INTERNAL_KEY}
    if payload:
        body.update(payload)
    try:
        r = await _http.post(
            PII_FUNCTION_URL,
            json=body,
            headers={"Content-Type": "application/json"},
        )
    except httpx.HTTPError:
        raise HTTPException(502, "Temporary customer-data storage is unavailable")
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400 or not data.get("ok"):
        safe_error = str(data.get("error") or "unknown")[:80]
        raise HTTPException(
            502,
            f"Temporary customer-data storage rejected the request: upstream={r.status_code} error={safe_error}",
        )
    return data

async def _get_token():
    global _token, _token_exp
    now = time.time()
    if _token and _token_exp - now > 60:
        return _token
    async with _token_lock:
        now = time.time()
        if _token and _token_exp - now > 60:
            return _token
        if not CLIENT_ID or not CLIENT_SECRET:
            raise HTTPException(503, "Ozon API credentials are not configured")
        r = await _http.post(
            OZON_TOKEN_URL,
            json={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "client_credentials",
                "scope": ["delivery-api.all"],
            },
        )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400:
            raise HTTPException(502, f"Ozon OAuth error: {data.get('message') or r.status_code}")
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise HTTPException(502, "Ozon OAuth returned no access token")
        raw_exp = data.get("expires_in", 3600)
        try:
            exp = float(raw_exp)
        except (TypeError, ValueError):
            exp = 3600.0
        # Ozon has returned both absolute Unix time and TTL-like values in integrations.
        _token_exp = exp if exp > now + 120 else now + max(exp, 300)
        _token = token
        return token

async def _ozon_post(path: str, payload: dict, idempotency_key: str | None = None, binary=False):
    token = await _get_token()
    url = urljoin(OZON_API_BASE, path.lstrip("/"))
    expected = _safe_origin(OZON_API_BASE)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    seen = set()
    for _ in range(4):
        if _safe_origin(url) != expected or url in seen:
            raise HTTPException(502, "Unsafe Ozon redirect")
        seen.add(url)
        r = await _http.post(url, json=payload, headers=headers)
        if r.status_code not in (302, 307):
            if binary and r.status_code == 200:
                if not r.content.startswith(b'%PDF-') or len(r.content) > 8000000:
                    raise HTTPException(502, 'Ozon label is not a valid PDF')
                return r.content
            data = r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else {"message": r.text[:500]}
            if r.status_code >= 400:
                msg = data.get("message") or data.get("error") or f"HTTP {r.status_code}"
                raise HTTPException(502, f"Ozon API error: {msg}")
            return data
        loc = r.headers.get("location")
        if not loc:
            raise HTTPException(502, "Ozon redirect without Location")
        url = urljoin(url, loc)
    raise HTTPException(502, "Too many Ozon redirects")

async def _load_all_points(force=False):
    global _points_cache, _points_cache_at
    if not force and _points_cache and time.time() - _points_cache_at < 3600:
        return _points_cache
    async with _points_lock:
        if not force and _points_cache and time.time() - _points_cache_at < 3600:
            return _points_cache
        summaries = []
        cursor = None
        seen = set()
        for _ in range(60):
            page = await _ozon_post("/v1/delivery-point/list", {"pagination": {"cursor": cursor, "limit": 100}})
            rows = page.get("delivery_points") or []
            for row in rows:
                pid = row.get("delivery_point_id")
                mids = row.get("shipment_method_ids") or []
                if isinstance(mids, int):
                    mids = [mids]
                if isinstance(pid, int):
                    summaries.append({"delivery_point_id": pid, "shipment_method_ids": [int(x) for x in mids if isinstance(x, int)]})
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        details = []
        for i in range(0, len(summaries), 100):
            batch = summaries[i:i+100]
            ids = [x["delivery_point_id"] for x in batch]
            method_map = {x["delivery_point_id"]: x["shipment_method_ids"] for x in batch}
            info = await _ozon_post("/v1/delivery-point/info", {"delivery_point_ids": ids})
            for row in info.get("delivery_points") or []:
                pid = row.get("delivery_point_id")
                if not isinstance(pid, int) or not row.get("is_active", True):
                    continue
                coords = row.get("coordinates") or {}
                details.append({
                    "delivery_point_id": pid,
                    "shipment_method_ids": method_map.get(pid, []),
                    "name": row.get("name") or "ПВЗ Ozon",
                    "full_address": row.get("full_address") or "",
                    "type": row.get("type") or "",
                    "latitude": coords.get("latitude"),
                    "longitude": coords.get("longitude"),
                    "storage_period_days": row.get("storage_period_days"),
                    "schedule": row.get("schedule") or [],
                })
        _points_cache = details
        _points_cache_at = time.time()
        return details


def _json_value(v, default):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return default
    return default

def _cache_row_to_point(row):
    return {
        "delivery_point_id": row["delivery_point_id"],
        "shipment_method_ids": _json_value(row["shipment_method_ids"], []),
        "name": row["point_name"] or "ПВЗ Ozon",
        "full_address": row["point_address"] or "",
        "type": row["point_type"] or "",
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "storage_period_days": row["storage_period_days"],
        "schedule": _json_value(row["schedule"], []),
    }

async def _publish_pvz_batch(rows):
    today=datetime.now(timezone.utc).date().isoformat()
    points=[]
    for row in rows:
        point=_cache_row_to_point(row)
        point['schedule']=[d for d in point.get('schedule',[]) if d.get('date','')>=today][:1]
        point['cache_updated_at']=row['updated_at'].isoformat()
        points.append(point)
    if points:
        result=await _pii_call('store_pvz_cache',{'points':points})
        print(json.dumps({'event':'pvz_cache_published','source':'real_ozon_postgres_cache','stored':result.get('stored'),'ttl_seconds':result.get('ttl_seconds')}),flush=True)

async def _export_pvz_catalog():
    exported=0
    try:
        # Seed the incident cities first, then the full real national catalogue.
        queries=[(" AND point_address ILIKE $1",('%'+city+'%',)) for city in ('Самара','Москва','Казань')]+[('',())]
        for suffix,args in queries:
            async with db.acquire() as c:
                async with c.transaction(readonly=True):
                    cur=await c.cursor("SELECT * FROM ozon_delivery_points_cache WHERE is_active=TRUE AND point_type='pvz' AND jsonb_array_length(shipment_method_ids)>0 AND latitude IS NOT NULL AND longitude IS NOT NULL AND updated_at>NOW()-INTERVAL '48 hours'"+suffix,*args)
                    while True:
                        rows=await cur.fetch(100)
                        if not rows:break
                        await _publish_pvz_batch(rows);exported+=len(rows)
                        await asyncio.sleep(.05)
        print(json.dumps({'event':'pvz_cache_export_complete','exported':exported}),flush=True)
    except Exception as exc:
        print(json.dumps({'event':'pvz_cache_export_error','exported':exported,'error_code':type(exc).__name__}),flush=True)

async def _sync_one_catalog_page(cursor):
    page = await _ozon_post("/v1/delivery-point/list", {"pagination": {"cursor": cursor, "limit": 100}})
    summaries = page.get("delivery_points") or []
    ids = []
    method_map = {}
    for row in summaries:
        pid = row.get("delivery_point_id")
        mids = row.get("shipment_method_ids") or []
        if isinstance(mids, int):
            mids = [mids]
        if isinstance(pid, int):
            ids.append(pid)
            method_map[pid] = [int(x) for x in mids if isinstance(x, int)]

    if ids:
        info = await _ozon_post("/v1/delivery-point/info", {"delivery_point_ids": ids})
        records = []
        for row in info.get("delivery_points") or []:
            pid = row.get("delivery_point_id")
            if not isinstance(pid, int):
                continue
            coords = row.get("coordinates") or {}
            lat = coords.get("latitude")
            lon = coords.get("longitude")
            records.append((
                pid,
                json.dumps(method_map.get(pid, []), ensure_ascii=False),
                row.get("name") or "ПВЗ Ozon",
                row.get("full_address") or "",
                row.get("type") or "",
                float(lat) if isinstance(lat, (int, float)) else None,
                float(lon) if isinstance(lon, (int, float)) else None,
                row.get("storage_period_days") if isinstance(row.get("storage_period_days"), int) else None,
                json.dumps(row.get("schedule") or [], ensure_ascii=False),
                bool(row.get("is_active", True)),
            ))
        if records and db:
            async with db.acquire() as c:
                await c.executemany("""
                    INSERT INTO ozon_delivery_points_cache
                    (delivery_point_id, shipment_method_ids, point_name, point_address, point_type,
                     latitude, longitude, storage_period_days, schedule, is_active, updated_at)
                    VALUES ($1,$2::jsonb,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,NOW())
                    ON CONFLICT (delivery_point_id) DO UPDATE SET
                      shipment_method_ids=EXCLUDED.shipment_method_ids,
                      point_name=EXCLUDED.point_name,
                      point_address=EXCLUDED.point_address,
                      point_type=EXCLUDED.point_type,
                      latitude=EXCLUDED.latitude,
                      longitude=EXCLUDED.longitude,
                      storage_period_days=EXCLUDED.storage_period_days,
                      schedule=EXCLUDED.schedule,
                      is_active=EXCLUDED.is_active,
                      updated_at=NOW()
                """, records)
                try:
                    published=await c.fetch('SELECT * FROM ozon_delivery_points_cache WHERE delivery_point_id=ANY($1::bigint[]) AND is_active=TRUE AND point_type=\'pvz\' AND latitude IS NOT NULL AND longitude IS NOT NULL',[r[0] for r in records])
                    await _publish_pvz_batch(published)
                except Exception as exc:
                    print(json.dumps({'event':'pvz_cache_publish_error','error_code':type(exc).__name__}),flush=True)

    next_cursor = page.get("next_cursor")
    if next_cursor == "":
        next_cursor = None
    if db:
        async with db.acquire() as c:
            await c.execute("""
                INSERT INTO ozon_delivery_sync_state (id, cursor, complete, updated_at)
                VALUES (1,$1,$2,NOW())
                ON CONFLICT (id) DO UPDATE SET cursor=EXCLUDED.cursor, complete=EXCLUDED.complete, updated_at=NOW()
            """, next_cursor, next_cursor is None)
    return next_cursor

async def _background_sync_loop():
    # Build a persistent catalog gradually so map requests never need to load
    # tens of thousands of Ozon points in one HTTP request.
    while True:
        try:
            if not db or not CLIENT_ID or not CLIENT_SECRET:
                await asyncio.sleep(60)
                continue
            async with db.acquire() as c:
                state = await c.fetchrow("SELECT cursor, complete, updated_at FROM ozon_delivery_sync_state WHERE id=1")
            cursor = state["cursor"] if state else None
            complete = bool(state["complete"]) if state else False
            updated_at = state["updated_at"] if state else None
            if complete and updated_at:
                age = time.time() - updated_at.timestamp()
                if age < 21600:
                    await asyncio.sleep(min(1800, max(60, 21600-age)))
                    continue
                async with db.acquire() as c:
                    await c.execute("UPDATE ozon_delivery_sync_state SET cursor=NULL, complete=FALSE, updated_at=NOW() WHERE id=1")
                cursor = None

            failures = 0
            while True:
                try:
                    next_cursor = await _sync_one_catalog_page(cursor)
                    failures = 0
                    cursor = next_cursor
                    if cursor is None:
                        break
                    await asyncio.sleep(0.12)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failures += 1
                    if failures >= 6:
                        await asyncio.sleep(300)
                        break
                    await asyncio.sleep(min(30, 2 ** failures))
            if cursor is None:
                await asyncio.sleep(21600)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(60)

def _parcel(request_id: int, shipment_method_id: int, cutoff_at=None):
    return {
        "request_id": request_id,
        "shipment_method_id": shipment_method_id,
        "cutoff_at": cutoff_at,
        "declared_value": {"amount": PRODUCT_PRICE, "currency_code": "RUB"},
        "dimensions": {
            "weight_g": PRODUCT_WEIGHT_G,
            "length_mm": PRODUCT_LENGTH_MM,
            "width_mm": PRODUCT_WIDTH_MM,
            "height_mm": PRODUCT_HEIGHT_MM,
        },
    }

class SelectionIn(BaseModel):
    source_token: str = Field(min_length=8, max_length=80)
    delivery_point_id: int
    shipment_method_id: int
    name: str = Field(max_length=200)
    address: str = Field(max_length=500)

class ManualSelectionIn(BaseModel):
    source_token: str = Field(min_length=8, max_length=80)
    manual_text: str = Field(min_length=5, max_length=500)

class AvailabilityIn(BaseModel):
    delivery_point_id: int
    shipment_method_id: int

class QuoteIn(BaseModel):
    phone_number: str
    delivery_point_id: int
    shipment_method_id: int

class CreateOrderIn(BaseModel):
    phone_number: str
    full_name: str | None = None
    delivery_point_id: int
    shipment_method_id: int
    source_token: str | None = None
    order_external_id: str | None = None

class CheckoutSessionIn(BaseModel):
    source_token: str = Field(min_length=8, max_length=80)
    full_name: str = Field(min_length=5, max_length=120)
    phone_number: str = Field(min_length=7, max_length=30)
    email: str = Field(min_length=5, max_length=160)
    quantity: int = Field(ge=1, le=99)
    unit_price: int = Field(ge=1, le=1000000)
    total_amount: int = Field(ge=1, le=100000000)

class DeliveryStatusIn(BaseModel):
    status: str = Field(min_length=2, max_length=60)
    tracking_number: str | None = Field(default=None, max_length=160)

@app.on_event("startup")
async def startup():
    global db, _sync_task, _payment_task, _profit_task, _profit_controller
    if DATABASE_URL:
        db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with db.acquire() as c:
            await c.execute("""
                CREATE TABLE IF NOT EXISTS ozon_delivery_selections (
                    source_token TEXT PRIMARY KEY,
                    delivery_point_id BIGINT NOT NULL,
                    shipment_method_id BIGINT NOT NULL,
                    point_name TEXT,
                    point_address TEXT,
                    selection_type TEXT NOT NULL DEFAULT 'api',
                    manual_text TEXT,
                    selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await c.execute("ALTER TABLE ozon_delivery_selections ADD COLUMN IF NOT EXISTS selection_type TEXT NOT NULL DEFAULT 'api'")
            await c.execute("ALTER TABLE ozon_delivery_selections ADD COLUMN IF NOT EXISTS manual_text TEXT")
            await c.execute("""
                CREATE TABLE IF NOT EXISTS ozon_delivery_points_cache (
                    delivery_point_id BIGINT PRIMARY KEY,
                    shipment_method_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                    point_name TEXT,
                    point_address TEXT,
                    point_type TEXT,
                    latitude DOUBLE PRECISION,
                    longitude DOUBLE PRECISION,
                    storage_period_days INTEGER,
                    schedule JSONB NOT NULL DEFAULT '[]'::jsonb,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await c.execute("CREATE INDEX IF NOT EXISTS ozon_points_lat_lon_idx ON ozon_delivery_points_cache(latitude, longitude)")
            await c.execute("CREATE INDEX IF NOT EXISTS ozon_points_address_idx ON ozon_delivery_points_cache USING gin (to_tsvector('simple', coalesce(point_address,'') || ' ' || coalesce(point_name,'')))")
            await c.execute("""
                CREATE TABLE IF NOT EXISTS ozon_delivery_sync_state (
                    id SMALLINT PRIMARY KEY,
                    cursor TEXT,
                    complete BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await c.execute("INSERT INTO ozon_delivery_sync_state(id,cursor,complete) VALUES(1,NULL,FALSE) ON CONFLICT(id) DO NOTHING")
            await c.execute("""
                CREATE TABLE IF NOT EXISTS site_checkout_sessions (
                    source_token TEXT PRIMARY KEY,
                    full_name TEXT,
                    phone_number TEXT,
                    email TEXT,
                    quantity INTEGER NOT NULL,
                    unit_price INTEGER NOT NULL,
                    total_amount INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'FORM_COMPLETE',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await c.execute("ALTER TABLE site_checkout_sessions ALTER COLUMN full_name DROP NOT NULL")
            await c.execute("ALTER TABLE site_checkout_sessions ALTER COLUMN phone_number DROP NOT NULL")
            await c.execute("ALTER TABLE site_checkout_sessions ALTER COLUMN email DROP NOT NULL")
            await c.execute("UPDATE site_checkout_sessions SET full_name=NULL, phone_number=NULL, email=NULL WHERE full_name IS NOT NULL OR phone_number IS NOT NULL OR email IS NOT NULL")
            await c.execute("CREATE INDEX IF NOT EXISTS site_checkout_sessions_updated_idx ON site_checkout_sessions(updated_at)")
            await c.execute("DELETE FROM site_checkout_sessions WHERE status='FORM_COMPLETE' AND updated_at < NOW() - INTERVAL '30 days'")
            await c.execute("""
                CREATE TABLE IF NOT EXISTS order_fulfillment (
                    source_token TEXT PRIMARY KEY,
                    payment_status TEXT NOT NULL DEFAULT 'UNPAID',
                    ozon_status TEXT,
                    ozon_order_id TEXT,
                    ozon_posting_id TEXT,
                    tracking_number TEXT,
                    ozon_response JSONB,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        _sync_task = asyncio.create_task(_background_sync_loop())
        _payment_task = asyncio.create_task(_background_payment_reconciliation())
        if os.getenv('PROFIT_CONTROLLER_ENABLED', '').lower() == 'true':
            _profit_controller = Controller(db, _http, _ozon_post)
            _profit_task = asyncio.create_task(_profit_controller.loop())

    if PII_FUNCTION_URL and PII_INTERNAL_KEY:
        try:
            result = await _pii_call("health")
            print("PII_HEALTH_OK=1 region=" + str(result.get("region") or "unknown"))
        except Exception as exc:
            detail = getattr(exc, "detail", None)
            print("PII_HEALTH_OK=0 error=" + type(exc).__name__ + " detail=" + str(detail or "")[:160])
    else:
        print("PII_HEALTH_OK=0 error=not_configured")

@app.on_event("shutdown")
async def shutdown():
    global _sync_task, _payment_task, _profit_task
    for task in (_sync_task, _payment_task, _profit_task):
        if not task:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await _http.aclose()
    if db:
        await db.close()

@app.get('/api/internal/commerce/controller-audit', include_in_schema=False)
async def controller_audit(x_internal_key: str | None = Header(default=None)):
    import hmac
    if not INTERNAL_KEY or not x_internal_key or not hmac.compare_digest(INTERNAL_KEY, x_internal_key):
        raise HTTPException(403, 'Forbidden')
    if not _profit_controller:
        return {'state': 'disabled', 'live_writes': False}
    async with db.acquire() as c:
        await _profit_controller.schema(c)
        snapshots = await c.fetch('SELECT hour FROM profit_controller_snapshots ORDER BY hour DESC LIMIT 5')
        states = await c.fetch('SELECT key,value,updated_at FROM profit_controller_state ORDER BY key')
        counts = await c.fetch('SELECT state,count(*) AS count FROM profit_controller_actions GROUP BY state')
    return {'runtime': _profit_controller.status, 'snapshots': [dict(r) for r in snapshots],
            'states': [dict(r) for r in states], 'actions': [dict(r) for r in counts]}

@app.get("/health")
async def health():
    cached = 0
    sync_complete = False
    if db:
        async with db.acquire() as c:
            cached = await c.fetchval("SELECT COUNT(*) FROM ozon_delivery_points_cache")
            sync_complete = bool(await c.fetchval("SELECT complete FROM ozon_delivery_sync_state WHERE id=1"))
    return {
        "ok": True,
        "ozon_configured": bool(CLIENT_ID and CLIENT_SECRET),
        "db_configured": bool(DATABASE_URL),
        "pii_configured": bool(PII_FUNCTION_URL and PII_INTERNAL_KEY),
        "real_ozon_create_enabled": bool(ENABLE_REAL_OZON_CREATE),
        "payment_reconciliation_running": bool(_payment_task and not _payment_task.done()),
        "points_cached": int(cached or 0),
        "sync_complete": sync_complete,
    }

@app.middleware('http')
async def pvz_request_logging(request: Request, call_next):
    if request.url.path not in ('/api/ozon/points','/api/ozon/map-points'):
        return await call_next(request)
    pvz_diagnostics.start(request)
    try:
        response=await call_next(request)
    except Exception as exc:
        pvz_diagnostics.failure(request,type(exc).__name__)
        raise
    if response.status_code>=400: pvz_diagnostics.failure(request,'HTTP_'+str(response.status_code))
    return response

@app.post('/api/ozon/client-trace')
async def pvz_client_trace(request: Request):
    raw=await request.body()
    if len(raw)>2048: raise HTTPException(413,'Trace too large')
    try:
        data=json.loads(raw)
        request_id=str(uuid.UUID(data['pvz_request_id']))
    except (ValueError,KeyError,TypeError): raise HTTPException(400,'Invalid trace')
    allowed_stages={'start','response','filtered','rendered','timeout','network_error','render_error','superseded'}
    if data.get('stage') not in allowed_stages: raise HTTPException(400,'Invalid stage')
    entry={'event':'pvz_client_trace','pvz_request_id':request_id,'stage':data['stage'],'client_ua':request.headers.get('user-agent','')[:300]}
    for key in ('raw_count','normalized_count','filtered_count','rendered_count','marker_count','elapsed_ms'):
        if isinstance(data.get(key),(int,float)): entry[key]=max(0,min(data[key],1000000))
    for key in ('error_code','timeout_source','transport'):
        if key in data: entry[key]=str(data[key])[:50]
    print(json.dumps(entry,ensure_ascii=False),flush=True)
    return {'ok':True,'pvz_request_id':request_id}

@app.get('/api/ozon/provider-diagnostic')
async def pvz_provider_diagnostic(city: str, x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key!=INTERNAL_KEY: raise HTTPException(403,'Forbidden')
    if city not in ('Самара','Москва','Казань'): raise HTTPException(400,'Invalid diagnostic city')
    started=time.perf_counter()
    async with db.acquire() as c:
        count=await c.fetchval("SELECT count(*) FROM ozon_delivery_points_cache WHERE is_active=TRUE AND point_type='pvz' AND jsonb_array_length(shipment_method_ids)>0 AND point_address ILIKE $1",'%'+city+'%')
        rows=await c.fetch("SELECT delivery_point_id,updated_at FROM ozon_delivery_points_cache WHERE is_active=TRUE AND point_type='pvz' AND jsonb_array_length(shipment_method_ids)>0 AND point_address ILIKE $1 ORDER BY updated_at DESC LIMIT 3",'%'+city+'%')
    ozon_start=time.perf_counter()
    try:
        data=await _ozon_post('/v1/delivery-point/info',{'delivery_point_ids':[r['delivery_point_id'] for r in rows]})
        result={'ozon_http_status':200,'ozon_result_count':len(data.get('delivery_points',[])),'error_code':None}
    except Exception as exc:
        result={'ozon_http_status':None,'ozon_result_count':0,'error_code':type(exc).__name__}
    return {'city':city,'pvz_count':count,'sample_ids':[r['delivery_point_id'] for r in rows],'cache_updated_at':[r['updated_at'].isoformat() for r in rows],'ozon_ms':round((time.perf_counter()-ozon_start)*1000,2),'total_ms':round((time.perf_counter()-started)*1000,2),**result}

@app.post('/api/ozon/publish-cache')
async def publish_pvz_cache(x_internal_key: str | None = Header(default=None)):
    global _pvz_export_task
    if not INTERNAL_KEY or x_internal_key!=INTERNAL_KEY:raise HTTPException(403,'Forbidden')
    if not _pvz_export_task or _pvz_export_task.done():_pvz_export_task=asyncio.create_task(_export_pvz_catalog())
    return {'ok':True,'status':'running'}

@app.get("/api/ozon/points")
async def points(request: Request, query: str = Query(min_length=2, max_length=100), city: str = Query(default="", max_length=100), limit: int = Query(30, ge=1, le=50)):
    if not db:
        raise HTTPException(503, "Database is not configured")
    q = " ".join(query.split())
    # Match street and house separately: Ozon uses punctuation between them.
    tokens = q.replace(",", " ").split()
    patterns = ["%" + t.replace("%", "\\%").replace("_", "\\_") + "%" for t in tokens]
    city_like = "%" + city.strip().replace("%", "\\%").replace("_", "\\_") + "%"
    async with db.acquire() as c:
        pvz_diagnostics.stage(request,'pool_acquire')
        count = await c.fetchval("""
            SELECT COUNT(*) FROM ozon_delivery_points_cache
            WHERE is_active=TRUE AND point_type='pvz'
              AND jsonb_array_length(shipment_method_ids)>0
              AND (coalesce(point_address,'') || ' ' || coalesce(point_name,'')) ILIKE ALL($1::text[])
              AND ($2='%%' OR point_address ILIKE $2)
        """, patterns, city_like)
        pvz_diagnostics.stage(request,'count_query')
        rows = await c.fetch("""
            SELECT delivery_point_id, shipment_method_ids, point_name, point_address, point_type,
                   latitude, longitude, storage_period_days, schedule
            FROM ozon_delivery_points_cache
            WHERE is_active=TRUE AND point_type='pvz'
              AND jsonb_array_length(shipment_method_ids)>0
              AND (coalesce(point_address,'') || ' ' || coalesce(point_name,'')) ILIKE ALL($1::text[])
              AND ($2='%%' OR point_address ILIKE $2)
            ORDER BY updated_at DESC
            LIMIT $3
        """, patterns, city_like, limit)
        pvz_diagnostics.stage(request,'points_query')
    return pvz_diagnostics.response(request,{"query": query, "count": int(count or 0), "items": [_cache_row_to_point(r) for r in rows]})


@app.get("/api/ozon/map-points")
async def map_points(
    request: Request,
    south: float = Query(ge=-90, le=90),
    west: float = Query(ge=-180, le=180),
    north: float = Query(ge=-90, le=90),
    east: float = Query(ge=-180, le=180),
    limit: int = Query(350, ge=1, le=600),
):
    if north <= south:
        raise HTTPException(400, "Invalid latitude bounds")
    if not db:
        raise HTTPException(503, "Database is not configured")
    center_lat = (south + north) / 2
    if west <= east:
        center_lon = (west + east) / 2
        lon_where = "longitude BETWEEN $2 AND $4"
        args = [south, west, north, east]
    else:
        center_lon = ((west + east + 360) / 2) % 360
        if center_lon > 180:
            center_lon -= 360
        lon_where = "(longitude >= $2 OR longitude <= $4)"
        args = [south, west, north, east]
    async with db.acquire() as c:
        pvz_diagnostics.stage(request,'pool_acquire')
        count = await c.fetchval(f"""
            SELECT COUNT(*) FROM ozon_delivery_points_cache
            WHERE is_active=TRUE AND point_type='pvz' AND jsonb_array_length(shipment_method_ids)>0 AND latitude BETWEEN $1 AND $3 AND {lon_where}
        """, *args)
        pvz_diagnostics.stage(request,'count_query')
        rows = await c.fetch(f"""
            SELECT delivery_point_id, shipment_method_ids, point_name, point_address, point_type,
                   latitude, longitude, storage_period_days, schedule
            FROM ozon_delivery_points_cache
            WHERE is_active=TRUE AND point_type='pvz' AND jsonb_array_length(shipment_method_ids)>0 AND latitude BETWEEN $1 AND $3 AND {lon_where}
            ORDER BY ((latitude-$5)*(latitude-$5) + (longitude-$6)*(longitude-$6))
            LIMIT $7
        """, *args, center_lat, center_lon, limit)
        pvz_diagnostics.stage(request,'points_query')
    return pvz_diagnostics.response(request,{"count": int(count or 0), "returned": len(rows), "items": [_cache_row_to_point(r) for r in rows]})

@app.post("/api/ozon/refresh-points")
async def refresh_points(x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(403, "Forbidden")
    if not db:
        raise HTTPException(503, "Database is not configured")
    async with db.acquire() as c:
        await c.execute("UPDATE ozon_delivery_sync_state SET cursor=NULL, complete=FALSE, updated_at=NOW() WHERE id=1")
    return {"ok": True, "status": "scheduled"}


@app.post("/api/checkout/session")
async def save_checkout_session(body: CheckoutSessionIn):
    if not db:
        raise HTTPException(503, "Database is not configured")
    name = " ".join(body.full_name.split())
    phone = " ".join(body.phone_number.split())
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(422, "Invalid email")
    async with db.acquire() as c:
        delivery = await c.fetchrow("""
            SELECT point_name, point_address, selection_type, manual_text
            FROM ozon_delivery_selections WHERE source_token=$1
        """, body.source_token)
    await _pii_call("store_checkout", {
        "source_token": body.source_token,
        "full_name": name,
        "phone_number": phone,
        "email": email,
        "quantity": body.quantity,
        "unit_price": body.unit_price,
        "total_amount": body.total_amount,
        "delivery_point_name": (delivery["point_name"] if delivery else "") or "",
        "delivery_point_address": (delivery["point_address"] if delivery else "") or "",
    })
    async with db.acquire() as c:
        await c.execute("""
            INSERT INTO site_checkout_sessions
            (source_token, quantity, unit_price, total_amount, status, created_at, updated_at)
            VALUES ($1,$2,$3,$4,'FORM_COMPLETE',NOW(),NOW())
            ON CONFLICT (source_token) DO UPDATE SET
              full_name=NULL,
              phone_number=NULL,
              email=NULL,
              quantity=EXCLUDED.quantity,
              unit_price=EXCLUDED.unit_price,
              total_amount=EXCLUDED.total_amount,
              status='FORM_COMPLETE',
              updated_at=NOW()
        """, body.source_token, body.quantity, body.unit_price, body.total_amount)
    return {"ok": True, "source_token": body.source_token, "personal_data_storage": "ru-central1-temporary"}

@app.get("/api/checkout/session/{source_token}")
async def get_checkout_session(source_token: str, x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(403, "Forbidden")
    if not db:
        raise HTTPException(503, "Database is not configured")
    async with db.acquire() as c:
        row = await c.fetchrow("""
            SELECT source_token, quantity, unit_price, total_amount, status, created_at, updated_at
            FROM site_checkout_sessions WHERE source_token=$1
        """, source_token)
        delivery = await c.fetchrow("""
            SELECT delivery_point_id, shipment_method_id, point_name, point_address, selection_type, manual_text, selected_at
            FROM ozon_delivery_selections WHERE source_token=$1
        """, source_token)
    checkout = dict(row) if row else None
    if checkout:
        pii = await _pii_call("get", {"source_token": source_token})
        customer = pii.get("customer") or {}
        checkout.update({
            "full_name": customer.get("full_name"),
            "phone_number": customer.get("phone_number"),
            "email": customer.get("email"),
        })
    return {"checkout": checkout, "delivery": dict(delivery) if delivery else None}

@app.post("/api/ozon/availability")
async def availability(body: AvailabilityIn):
    payload = {
        "delivery_point_ids": [body.delivery_point_id],
        "shipment_method_id": body.shipment_method_id,
        "postings": [{
            "request_id": 1,
            "cutoff_at": None,
            "declared_value": {"amount": PRODUCT_PRICE, "currency_code": "RUB"},
            "dimensions": {
                "weight_g": PRODUCT_WEIGHT_G,
                "length_mm": PRODUCT_LENGTH_MM,
                "width_mm": PRODUCT_WIDTH_MM,
                "height_mm": PRODUCT_HEIGHT_MM,
            },
        }],
    }
    return await _ozon_post("/v1/delivery-point/check-availability", payload)

@app.post("/api/ozon/selection")
async def save_selection(body: SelectionIn):
    if db:
        async with db.acquire() as c:
            await c.execute("""
                INSERT INTO ozon_delivery_selections
                (source_token, delivery_point_id, shipment_method_id, point_name, point_address, selection_type, manual_text, selected_at)
                VALUES ($1,$2,$3,$4,$5,'api',NULL,NOW())
                ON CONFLICT (source_token) DO UPDATE SET
                  delivery_point_id=EXCLUDED.delivery_point_id,
                  shipment_method_id=EXCLUDED.shipment_method_id,
                  point_name=EXCLUDED.point_name,
                  point_address=EXCLUDED.point_address,
                  selection_type='api',
                  manual_text=NULL,
                  selected_at=NOW()
            """, body.source_token, body.delivery_point_id, body.shipment_method_id, body.name, body.address)
    return {"ok": True}


@app.post("/api/ozon/manual-selection")
async def save_manual_selection(body: ManualSelectionIn):
    text = " ".join(body.manual_text.split())
    if db:
        async with db.acquire() as c:
            await c.execute("""
                INSERT INTO ozon_delivery_selections
                (source_token, delivery_point_id, shipment_method_id, point_name, point_address, selection_type, manual_text, selected_at)
                VALUES ($1,0,0,'ПВЗ Ozon указан вручную',$2,'manual',$2,NOW())
                ON CONFLICT (source_token) DO UPDATE SET
                  delivery_point_id=0,
                  shipment_method_id=0,
                  point_name='ПВЗ Ozon указан вручную',
                  point_address=EXCLUDED.point_address,
                  selection_type='manual',
                  manual_text=EXCLUDED.manual_text,
                  selected_at=NOW()
            """, body.source_token, text)
    return {"ok": True, "selection_type": "manual", "manual_text": text}

@app.get("/api/ozon/selection/{source_token}")
async def get_selection(source_token: str, x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(403, "Forbidden")
    if not db:
        raise HTTPException(503, "Database is not configured")
    async with db.acquire() as c:
        row = await c.fetchrow("""
            SELECT delivery_point_id, shipment_method_id, point_name, point_address, selection_type, manual_text, selected_at
            FROM ozon_delivery_selections WHERE source_token=$1
        """, source_token)
    return {"item": dict(row) if row else None}

@app.post("/api/ozon/quote")
async def quote(body: QuoteIn, x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(403, "Forbidden")
    payload = {
        "recipient": {"phone_number": body.phone_number},
        "postings": [_parcel(1, body.shipment_method_id)],
        "delivery": {"delivery_point": {"delivery_point_id": body.delivery_point_id}},
    }
    return await _ozon_post("/v1/order/checkout", payload)

@app.post("/api/ozon/create-order")
async def create_order(body: CreateOrderIn, x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(403, "Forbidden")
    # This legacy request has no trusted InSales/payment linkage. Fail closed.
    raise HTTPException(409, "Verified YooKassa payment linkage is required before fulfillment")

@app.post("/api/order/{source_token}/paid")
async def mark_order_paid(source_token: str, x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(403, "Forbidden")
    # Internal access alone is not evidence that money was received.
    raise HTTPException(409, "Payment status can only be accepted after YooKassa API verification")

@app.post("/api/order/{source_token}/delivery-status")
async def update_delivery_status(source_token: str, body: DeliveryStatusIn, x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(403, "Forbidden")
    # This legacy callback cannot prove a native paid order binding.
    raise HTTPException(409, "Verified paid order linkage is required before delivery or review processing")

@app.post("/api/order/{source_token}/return-open")
async def open_return(source_token: str, x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(403, "Forbidden")
    result = await _pii_call("return_open", {"source_token": source_token})
    if db:
        async with db.acquire() as c:
            await c.execute("UPDATE site_checkout_sessions SET status='RETURN_OPEN', updated_at=NOW() WHERE source_token=$1", source_token)
    return result

@app.post("/api/order/{source_token}/return-close")
async def close_return(source_token: str, x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(403, "Forbidden")
    result = await _pii_call("return_close", {"source_token": source_token})
    if db:
        async with db.acquire() as c:
            await c.execute("UPDATE site_checkout_sessions SET status='RETURN_CLOSED', updated_at=NOW() WHERE source_token=$1", source_token)
    return result

# Server-side YooKassa diagnostics; credentials never enter storefront responses.
async def _yookassa_read(path: str, supplied_key: str | None):
    import hmac
    if not INTERNAL_KEY or not supplied_key or not hmac.compare_digest(supplied_key, INTERNAL_KEY):
        raise HTTPException(403, "Forbidden")
    shop = os.getenv("YOOKASSA_SHOP_ID", "")
    secret = os.getenv("YOOKASSA_SECRET_KEY", "")
    if not shop or not secret:
        raise HTTPException(503, "YooKassa credentials are not configured")
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
            response = await client.get("https://api.yookassa.ru/v3/" + path, auth=(shop, secret))
    except httpx.HTTPError:
        raise HTTPException(502, "YooKassa connection failed")
    if response.status_code != 200:
        raise HTTPException(502, {"provider_http_status": response.status_code})
    return response.json()

@app.get("/api/internal/yookassa/account", include_in_schema=False)
async def yookassa_account(x_internal_key: str | None = Header(default=None)):
    data = await _yookassa_read("me", x_internal_key)
    return {key: data.get(key) for key in ("account_id", "status", "test", "payment_methods")}

@app.get("/api/internal/yookassa/payments", include_in_schema=False)
async def yookassa_recent_payments(x_internal_key: str | None = Header(default=None)):
    data = await _yookassa_read("payments?limit=30", x_internal_key)
    return {"items": [{key: item.get(key) for key in
        ("id", "status", "paid", "amount", "description", "test", "created_at")}
        for item in data.get("items", [])]}

@app.get("/api/internal/yookassa/payments/{payment_id}", include_in_schema=False)
async def yookassa_payment_status(payment_id: str, x_internal_key: str | None = Header(default=None)):
    try:
        canonical = str(uuid.UUID(payment_id))
    except ValueError:
        raise HTTPException(400, "Invalid payment ID")
    data = await _yookassa_read("payments/" + canonical, x_internal_key)
    return {key: data.get(key) for key in ("id", "status", "paid", "amount", "description", "metadata", "test", "payment_method", "recipient", "merchant_customer_id", "confirmation")}

# Native InSales payment reconciliation. Bindings require trusted internal access.
from decimal import Decimal
from payment_validation import validate_payment, native_order_number, can_replace_unpaid_attempt
from native_order_proof import verify_native_proof

class NativePaymentBinding(BaseModel):
    order_id: int = Field(gt=0)
    order_number: int = Field(gt=0)
    payment_id: uuid.UUID
    quantity: int = Field(ge=1, le=100)
    recipient_name: str = Field(min_length=1, max_length=250)
    phone: str = Field(min_length=5, max_length=40)
    email: str = Field(max_length=250)
    pickup_point_id: str = Field(min_length=1, max_length=100)
    pickup_title: str = Field(max_length=250)
    pickup_address: str = Field(min_length=1, max_length=1000)
    pickup_city: str = Field(min_length=1, max_length=250)

class YooNotificationObject(BaseModel):
    id: uuid.UUID

class YooNotification(BaseModel):
    type: str
    event: str
    object: YooNotificationObject

_payment_schema_ready = False
_payment_schema_lock = asyncio.Lock()

async def _payment_schema():
    global _payment_schema_ready
    if not db:
        raise HTTPException(503, 'Database is not configured')
    async with _payment_schema_lock:
        if _payment_schema_ready:
            return
        async with db.acquire() as c:
            await c.execute('''CREATE TABLE IF NOT EXISTS insales_yookassa_payments (
                payment_id TEXT PRIMARY KEY, order_id BIGINT NOT NULL,
                order_number BIGINT NOT NULL, quantity INT NOT NULL, amount NUMERIC(12,2) NOT NULL,
                order_snapshot JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                notification_state TEXT NOT NULL DEFAULT 'not_sent', error_code TEXT,
                notification_message_id BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
                CREATE INDEX IF NOT EXISTS insales_yoo_order_idx ON insales_yookassa_payments(order_id);
                CREATE TABLE IF NOT EXISTS insales_yookassa_unbound_events (
                payment_id TEXT PRIMARY KEY, status TEXT, observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
                CREATE TABLE IF NOT EXISTS commerce_pending_orders (
                internal_order_token TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                quantity INT NOT NULL, amount NUMERIC(12,2) NOT NULL, snapshot JSONB NOT NULL,
                order_key TEXT UNIQUE, order_id BIGINT UNIQUE, order_number BIGINT UNIQUE,
                payment_id TEXT UNIQUE, payment_status TEXT NOT NULL DEFAULT 'pending',
                shipment_state TEXT NOT NULL DEFAULT 'not_created', shipment_id TEXT,
                shipment_idempotency_key TEXT NOT NULL, shipment_response JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());''')
            await c.execute('''ALTER TABLE commerce_pending_orders ADD COLUMN IF NOT EXISTS tracking_number TEXT;
                ALTER TABLE commerce_pending_orders ADD COLUMN IF NOT EXISTS ozon_status TEXT;
                ALTER TABLE commerce_pending_orders ADD COLUMN IF NOT EXISTS status_checked_at TIMESTAMPTZ;
                ALTER TABLE commerce_pending_orders ADD COLUMN IF NOT EXISTS post_purchase_failures INT NOT NULL DEFAULT 0;
                CREATE TABLE IF NOT EXISTS commerce_service_messages (
                order_id BIGINT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
                attempts INT NOT NULL DEFAULT 0,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(order_id,kind));''')
            await c.execute('''CREATE TABLE IF NOT EXISTS commerce_paid_conversions (
                order_id BIGINT PRIMARY KEY, payment_id TEXT UNIQUE NOT NULL,
                payload JSONB NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INT NOT NULL DEFAULT 0, upload_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());''')
        _payment_schema_ready = True

class AttributionIn(BaseModel):
    yclid: str | None = Field(default=None, pattern=r'^\d{1,100}$')
    client_id: str | None = Field(default=None, pattern=r'^\d{1,100}$')
    utm_source: str | None = Field(default=None, max_length=250)
    utm_medium: str | None = Field(default=None, max_length=250)
    utm_campaign: str | None = Field(default=None, max_length=250)
    utm_content: str | None = Field(default=None, max_length=250)
    utm_term: str | None = Field(default=None, max_length=250)

class PendingOrderIn(BaseModel):
    session_id: uuid.UUID
    attribution: AttributionIn | None = None
    quantity: int = Field(ge=1, le=100)
    product_id: int
    variant_id: int
    recipient_name: str = Field(min_length=2, max_length=250)
    phone: str = Field(pattern=r'^\+7\d{10}$')
    email: str = Field(min_length=5, max_length=250)
    pickup_point_id: int = Field(gt=0)
    pickup_city: str = Field(min_length=2, max_length=250)

class NativeOrderLinkIn(BaseModel):
    internal_order_token: str = Field(pattern=r'^[a-f0-9]{64}$')
    order_key: str = Field(pattern=r'^[A-Za-z0-9_-]{16,200}$')

@app.post('/api/commerce/pending', include_in_schema=False)
async def create_pending_order(body: PendingOrderIn):
    if body.product_id != 1825508753 or body.variant_id != 2184195121 or '@' not in body.email:
        raise HTTPException(422, 'Invalid product or recipient')
    await _payment_schema()
    async with db.acquire() as c:
        point = await c.fetchrow('SELECT * FROM ozon_delivery_points_cache WHERE delivery_point_id=$1 AND is_active=true', body.pickup_point_id)
        if not point:
            raise HTTPException(422, 'Active Ozon pickup point required')
        methods = _json_value(point['shipment_method_ids'], [])
        if not methods:
            raise HTTPException(422, 'Ozon shipment method unavailable')
        import secrets
        token = secrets.token_hex(32)
        snapshot = body.model_dump(mode='json')
        snapshot.update(internal_order_token=token, pickup_title=point['point_name'],
                        pickup_address=point['point_address'], delivery_point_id=body.pickup_point_id,
                        shipment_method_ids=methods, product='Съёмная ручка для сковороды',
                        unit_price=800, delivery_price=0)
        await c.execute('''INSERT INTO commerce_pending_orders
            (internal_order_token,session_id,quantity,amount,snapshot,shipment_idempotency_key)
            VALUES($1,$2,$3,$4,$5::jsonb,$6)''', token, str(body.session_id), body.quantity,
            Decimal(body.quantity)*800, json.dumps(snapshot), str(uuid.uuid4()))
    return {'ok': True, 'internal_order_token': token, 'amount': body.quantity*800,
            'pickup_address': snapshot['pickup_address']}

@app.post('/api/commerce/link-order', include_in_schema=False)
async def link_native_order(body: NativeOrderLinkIn):
    await _payment_schema()
    async with db.acquire() as c:
        row = await c.fetchrow('SELECT * FROM commerce_pending_orders WHERE internal_order_token=$1', body.internal_order_token)
    if not row:
        raise HTTPException(404, 'Pending record not found')
    if row['order_key'] and row['order_key'] != body.order_key:
        raise HTTPException(409, 'Native order binding is immutable')
    # Never accept an arbitrary URL or browser-supplied order number. Fixed host and opaque key only.
    origin = 'https://xn--163-5cdt3dgrs.xn--p1ai'
    url = origin + '/client_account/client_order?key=' + body.order_key
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        for _ in range(3):
            response = await client.get(url)
            if response.status_code not in (301,302,303,307,308):
                break
            target = urljoin(url, response.headers.get('location', ''))
            if _safe_origin(target) != _safe_origin(origin) or urlsplit(target).path not in (
                    '/orders/' + body.order_key, '/client_account/orders/' + body.order_key):
                raise HTTPException(409, 'Native order is not available for verification')
            url = target
    if response.status_code != 200 or len(response.content) > 2000000:
        raise HTTPException(409, 'Native order verification unavailable')
    try:
        order_id, number = verify_native_proof(response.json(), row, body.order_key)
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        # Only fixed verification errors; never print native order/gateway configuration.
        import logging
        logging.warning('Native proof rejected: %s', str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        raise HTTPException(409, 'Native order proof does not match pending record')
    async with db.acquire() as c:
        async with c.transaction():
            locked = await c.fetchrow('SELECT * FROM commerce_pending_orders WHERE internal_order_token=$1 FOR UPDATE', body.internal_order_token)
            if locked['order_key'] and (locked['order_key'] != body.order_key or locked['order_number'] != number):
                raise HTTPException(409, 'Native order binding is immutable')
            await c.execute('UPDATE commerce_pending_orders SET order_key=$2,order_id=$3,order_number=$4,updated_at=NOW() WHERE internal_order_token=$1', body.internal_order_token, body.order_key, order_id, number)
    asyncio.create_task(_discover_native_payment(number))
    return {'ok': True, 'order_number': number}

async def _discover_native_payment(number):
    # The native payment is created after the order response. A short bounded poll fills its reference.
    for _ in range(24):
        try:
            data = await _yookassa_read('payments?limit=100', INTERNAL_KEY)
            candidates = [p for p in data.get('items', []) if native_order_number(p) == number and p.get('status') != 'canceled']
            if len(candidates) == 1 and await _register_pending_payment(candidates[0]):
                if candidates[0].get('status') == 'succeeded' and candidates[0].get('paid') is True:
                    await yookassa_notification(YooNotification(type='notification', event='payment.succeeded', object={'id': candidates[0]['id']}))
                return
        except Exception:
            pass  # Do not log provider payloads or credentials.
        await asyncio.sleep(5)

@app.get('/api/internal/commerce/pending-audit', include_in_schema=False)
async def pending_audit(x_internal_key: str | None = Header(default=None)):
    import hmac
    if not INTERNAL_KEY or not x_internal_key or not hmac.compare_digest(INTERNAL_KEY, x_internal_key):
        raise HTTPException(403, 'Forbidden')
    await _payment_schema()
    async with db.acquire() as c:
        rows = await c.fetch('''SELECT order_id,order_number,payment_id,quantity,amount,
            payment_status,shipment_state,shipment_id,created_at FROM commerce_pending_orders ORDER BY created_at DESC LIMIT 20''')
    return {'items': [dict(r) for r in rows]}

async def _register_pending_payment(payment):
    number = native_order_number(payment)
    if not number:
        return False
    async with db.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow('SELECT * FROM commerce_pending_orders WHERE order_number=$1 AND order_key IS NOT NULL FOR UPDATE', number)
            if not row:
                return False
            expected = {'payment_id': payment['id'], 'order_number': number, 'amount': row['amount']}
            error = validate_payment(payment, expected, os.getenv('YOOKASSA_SHOP_ID', ''))
            if error and error != 'not_paid':
                return False
            if row['payment_id'] and row['payment_id'] != payment['id']:
                previous = await _yookassa_read('payments/' + row['payment_id'], INTERNAL_KEY)
                if not can_replace_unpaid_attempt(previous, payment):
                    return False
            snapshot = _json_value(row['snapshot'], {})
            snapshot.update(order_id=row['order_id'], order_number=number, payment_id=payment['id'])
            await c.execute('''INSERT INTO insales_yookassa_payments
                (payment_id,order_id,order_number,quantity,amount,order_snapshot)
                VALUES($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT(payment_id) DO NOTHING''',
                payment['id'], row['order_id'], number, row['quantity'], row['amount'], json.dumps(snapshot))
            await c.execute('UPDATE commerce_pending_orders SET payment_id=$2,payment_status=$3,updated_at=NOW() WHERE internal_order_token=$1 AND (payment_id IS DISTINCT FROM $2 OR payment_status IS DISTINCT FROM $3)',
                row['internal_order_token'], payment['id'], payment['status'])
    return True

async def _background_payment_reconciliation():
    # Native OAuth payments may notify InSales rather than this merchant URL.
    # Recover through the same API-verified, idempotent paid handler.
    await _payment_schema()
    while True:
        try:
            async with db.acquire() as c:
                rows = await c.fetch('''SELECT order_number FROM commerce_pending_orders
                    WHERE order_key IS NOT NULL AND (payment_status <> 'succeeded'
                    OR ($1 AND shipment_id IS NULL)) ''', ENABLE_REAL_OZON_CREATE)
                numbers = {r['order_number'] for r in rows}
            if numbers:
                cursor = None
                while True:
                    path = 'payments?limit=100' + ('&cursor=' + cursor if cursor else '')
                    data = await _yookassa_read(path, INTERNAL_KEY)
                    for payment in data.get('items', []):
                        if native_order_number(payment) in numbers and payment.get('status') == 'succeeded' and payment.get('paid') is True:
                            await yookassa_notification(YooNotification(type='notification',event='payment.succeeded',object={'id':payment['id']}))
                            print('PAYMENT_RECONCILED order=' + str(native_order_number(payment)))
                    cursor = data.get('next_cursor')
                    if not cursor:
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print('PAYMENT_RECONCILIATION_FAILED type=' + type(exc).__name__)
        try:
            await _sync_post_purchase()
        except Exception as exc:
            print('POST_PURCHASE_SYNC_FAILED type=' + type(exc).__name__)
        try:
            await _sync_paid_metrika()
        except Exception as exc:
            print('PAID_METRIKA_SYNC_FAILED type=' + type(exc).__name__)
        await asyncio.sleep(60)

async def _sync_paid_metrika():
    # Re-use the verified native-order/payment linkage, independent of redirect and fulfillment.
    async with db.acquire() as c:
        rows = await c.fetch('''SELECT p.* FROM commerce_pending_orders p
            LEFT JOIN commerce_paid_conversions m ON m.order_id=p.order_id
            WHERE p.payment_status='succeeded' AND p.order_id IS NOT NULL
            AND p.order_key IS NOT NULL AND m.order_id IS NULL
            AND (coalesce(p.snapshot->'attribution'->>'client_id','')<>''
                 OR coalesce(p.snapshot->'attribution'->>'yclid','')<>'')''')
    for row in rows:
        try:
            payment = await _yookassa_read('payments/' + row['payment_id'], INTERNAL_KEY)
            conversion = paid_conversion(payment, row, _json_value(row['snapshot'], {}), os.getenv('YOOKASSA_SHOP_ID',''))
        except (ValueError, HTTPException):
            continue  # Unpaid, invalid linkage and missing identifiers never emit a conversion.
        async with db.acquire() as c:
            await c.execute('''INSERT INTO commerce_paid_conversions(order_id,payment_id,payload)
                VALUES($1,$2,$3::jsonb) ON CONFLICT DO NOTHING''',row['order_id'],row['payment_id'],json.dumps(conversion))
    counter = os.getenv('METRIKA_COUNTER_ID','')
    oauth = os.getenv('METRIKA_OAUTH_TOKEN','')
    if not counter.isdigit() or not oauth or os.getenv('METRIKA_PAID_ENABLED','false').lower() != 'true':
        return
    async with db.acquire() as c:
        rows = await c.fetch("SELECT * FROM commerce_paid_conversions WHERE state IN ('pending','retry') AND attempts<5")
    for row in rows:
        async with db.acquire() as c:
            claimed = await c.fetchval('''UPDATE commerce_paid_conversions SET state='claimed',attempts=attempts+1,updated_at=NOW()
                WHERE order_id=$1 AND state IN ('pending','retry') AND attempts<5 RETURNING order_id''',row['order_id'])
        if not claimed:
            continue
        state, upload_id = 'unknown', None
        try:
            conversion = _json_value(row['payload'],{})
            async with httpx.AsyncClient(timeout=25) as client:
                response = await client.post('https://api-metrika.yandex.net/management/v1/counter/' + counter + '/offline_conversions/upload',
                    headers={'Authorization':'OAuth '+oauth},params={'comment':
                        'paid order '+str(row['order_id'])+' product 1825508753 quantity '+str(conversion['quantity'])+' Съемная ручка для сковороды'},
                    files={'file':('paid.csv',conversion_csv(conversion),'text/csv')})
            if response.status_code in (200,201):
                upload_id = response.json().get('uploading',{}).get('id')
                if upload_id is not None:
                    state = 'accepted'
            elif response.status_code in (400,401,403,422,429):
                state = 'retry'  # Explicit rejection: no import accepted.
        except Exception:
            pass  # Ambiguous timeout never blindly uploads twice; no secrets/provider bodies in logs.
        async with db.acquire() as c:
            await c.execute('''UPDATE commerce_paid_conversions SET state=$2,upload_id=$3,updated_at=NOW()
                WHERE order_id=$1''',row['order_id'],state,str(upload_id) if upload_id is not None else None)
    # Acceptance is not attribution proof: retain the provider's processing result.
    async with db.acquire() as c:
        uploads = await c.fetch("SELECT order_id,upload_id FROM commerce_paid_conversions WHERE state='accepted' AND upload_id IS NOT NULL")
    for row in uploads:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get('https://api-metrika.yandex.net/management/v1/counter/'+counter+
                    '/offline_conversions/uploading/'+row['upload_id'],headers={'Authorization':'OAuth '+oauth})
            if response.status_code != 200:
                continue
            status = response.json().get('uploading',{}).get('status')
            if status in ('PROCESSED','LINKAGE_FAILURE'):
                async with db.acquire() as c:
                    await c.execute('UPDATE commerce_paid_conversions SET state=$2,updated_at=NOW() WHERE order_id=$1',
                        row['order_id'],'processed' if status=='PROCESSED' else 'linkage_failure')
        except httpx.HTTPError:
            pass

@app.get('/api/internal/commerce/metrika-audit', include_in_schema=False)
async def metrika_audit(x_internal_key: str | None = Header(default=None)):
    import hmac
    if not INTERNAL_KEY or not x_internal_key or not hmac.compare_digest(INTERNAL_KEY,x_internal_key):
        raise HTTPException(403,'Forbidden')
    await _payment_schema()
    async with db.acquire() as c:
        rows = await c.fetch('''SELECT p.order_number,p.payment_status,
            coalesce(p.snapshot->'attribution'->>'yclid','')<>'' AS has_yclid,
            coalesce(p.snapshot->'attribution'->>'client_id','')<>'' AS has_client_id,
            coalesce(p.snapshot->'attribution'->>'utm_source','')<>'' AS has_utm,
            m.state,m.upload_id FROM commerce_pending_orders p
            LEFT JOIN commerce_paid_conversions m ON m.order_id=p.order_id
            ORDER BY p.created_at DESC LIMIT 20''')
    return {'configured': bool(os.getenv('METRIKA_OAUTH_TOKEN','') and os.getenv('METRIKA_COUNTER_ID','')),
            'enabled': os.getenv('METRIKA_PAID_ENABLED','false').lower()=='true','items':[dict(r) for r in rows]}

async def _create_paid_ozon_shipment(payment_id):
    # Only called after an API-verified payment. Legacy manually-bound orders never enter automation.
    async with db.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow('SELECT * FROM commerce_pending_orders WHERE payment_id=$1 FOR UPDATE', payment_id)
            if not row:
                return {'automated': False}
            if row['payment_status'] != 'succeeded':
                raise HTTPException(409, 'Verified paid status required')
            if row['shipment_id']:
                return {'shipment_id': row['shipment_id'], 'duplicate': True}
            if not ENABLE_REAL_OZON_CREATE:
                return {'automated': False, 'reason': 'real_create_disabled'}
            if row['shipment_state'] == 'creating' and time.time() - row['updated_at'].timestamp() < 120:
                raise HTTPException(503, 'Shipment creation already in progress')
            await c.execute("UPDATE commerce_pending_orders SET shipment_state='creating',updated_at=NOW() WHERE payment_id=$1", payment_id)
            snapshot = _json_value(row['snapshot'], {})
    external_id = 'posuda-insales-' + str(row['order_id'])
    # One parcel per item uses the existing single-item packaged dimensions without inventing a bulk size.
    postings = []
    for i in range(row['quantity']):
        parcel = _parcel(i+1, int(snapshot['shipment_method_ids'][0]))
        parcel.update(posting_external_id=external_id+'-'+str(i+1), description='Съёмная ручка для сковороды, 1 шт.')
        postings.append(parcel)
    payload = {'order_external_id': external_id,
               'recipient': {'phone_number': snapshot['phone'], 'full_name': snapshot['recipient_name']},
               'delivery': {'delivery_point': {'delivery_point_id': snapshot['pickup_point_id']}}, 'postings': postings}
    try:
        # Official Ozon contract requires a fresh quote with the same shipment parameters.
        quote_postings = [{k: p[k] for k in ('request_id','shipment_method_id','cutoff_at','declared_value','dimensions')} for p in postings]
        quote = await _ozon_post('/v1/order/checkout', {
            'recipient': {'phone_number': snapshot['phone']},
            'delivery': payload['delivery'], 'postings': quote_postings})
        results = quote.get('results', [])
        if len(results) != len(postings) or any(not item.get('posting') or item.get('error') for item in results):
            raise HTTPException(409, 'Ozon shipping calculation failed')
        result = await _ozon_post('/v1/order/create', payload, row['shipment_idempotency_key'])
        shipment_id = result.get('order_number')
        if not shipment_id:
            raise HTTPException(502, 'Ozon returned no shipment identifier')
    except Exception:
        async with db.acquire() as c:
            await c.execute("UPDATE commerce_pending_orders SET shipment_state='retry_same_key',updated_at=NOW() WHERE payment_id=$1", payment_id)
        raise
    async with db.acquire() as c:
        await c.execute("UPDATE commerce_pending_orders SET shipment_state='created',shipment_id=$2,shipment_response=$3::jsonb,updated_at=NOW() WHERE payment_id=$1",
            payment_id, str(shipment_id), json.dumps(result))
    return {'shipment_id': str(shipment_id)}

async def _owner_post_purchase_alert(row, detail):
    async with db.acquire() as c:
        claimed = await c.fetchval('''INSERT INTO commerce_service_messages(order_id,kind,state)
            VALUES($1,'ozon_alert','claimed') ON CONFLICT DO NOTHING RETURNING order_id''',row['order_id'])
    if not claimed:
        return
    state='unknown'
    try:
        response=await _http.post('https://api.telegram.org/bot'+os.environ['TELEGRAM_BOT_TOKEN']+'/sendMessage',
            json={'chat_id':os.environ['OWNER_CHAT_ID'],'text':f"⚠️ ОПЛАЧЕННЫЙ ЗАКАЗ №{row['order_number']}\n{detail}\nПроверьте Ozon. Не создавайте повторное отправление без проверки."})
        if response.status_code==200 and response.json().get('ok'):state='sent'
    except Exception:
        pass
    async with db.acquire() as c:
        await c.execute("UPDATE commerce_service_messages SET state=$2,updated_at=NOW() WHERE order_id=$1 AND kind='ozon_alert'",row['order_id'],state)

async def _send_queued_customer_email(row,kind):
    if not email_ready() or not production_email_allowed(row):return
    async with db.acquire() as c:
        claimed=await c.fetchval('''UPDATE commerce_service_messages SET state='claimed',attempts=attempts+1,updated_at=NOW()
            WHERE order_id=$1 AND kind=$2 AND state IN ('pending','retry') AND attempts<3 RETURNING order_id''',row['order_id'],kind)
    if not claimed:return
    snapshot=_json_value(row['snapshot'],{})
    state='unknown'
    try:
        await asyncio.to_thread(send_customer_email,snapshot['email'],row['order_number'],kind,row['tracking_number'])
        state='sent'
    except EmailDeliveryRejected:
        # Explicit HTTP rejection means Resend did not accept the message: bounded retry is safe.
        state='retry'
    except Exception:
        # A timeout after the API request may already have delivered the email. Never resend blindly.
        pass
    async with db.acquire() as c:
        await c.execute('UPDATE commerce_service_messages SET state=$3,updated_at=NOW() WHERE order_id=$1 AND kind=$2',row['order_id'],kind,state)

async def _sync_post_purchase():
    async with db.acquire() as c:
        rows=await c.fetch("SELECT * FROM commerce_pending_orders WHERE payment_status='succeeded' AND order_key IS NOT NULL")
    for record in rows:
        row=dict(record)
        try:
            if not row['shipment_id']:raise ValueError('shipment_missing')
            tracking=row['tracking_number']
            if not tracking:
                response=_json_value(row['shipment_response'],{})
                postings=response.get('postings',[])
                if postings and len(postings)==row['quantity']:
                    tracking=', '.join(p['posting_number'] for p in postings if p.get('posting_number'))
                if not tracking:
                    # Read only recovery by the immutable external order id; never recreate.
                    cursor=None;matches=[]
                    while True:
                        result=await _ozon_post('/v1/posting/search',{'filters':{'created_at_from':row['created_at'].isoformat()},'pagination':{'limit':100,'cursor':cursor}})
                        matches.extend(p for p in result.get('postings',[]) if p.get('order_external_id')=='posuda-insales-'+str(row['order_id']))
                        cursor=result.get('next_cursor')
                        if not cursor:break
                    if len(matches)==row['quantity']:
                        tracking=', '.join(p['posting_number'] for p in matches)
                if not tracking:raise ValueError('tracking_missing')
            numbers=tracking.split(', ')
            info=await _ozon_post('/v1/posting/info',{'posting_numbers':numbers})
            postings=info.get('postings',[])
            if len(postings)!=row['quantity'] or {p.get('posting_number') for p in postings}!=set(numbers):raise ValueError('posting_missing')
            snapshot=_json_value(row['snapshot'],{})
            if any(p.get('order_number')!=row['shipment_id'] or
                p.get('delivery',{}).get('delivery_point_id')!=snapshot['pickup_point_id'] for p in postings):
                raise ValueError('posting_mismatch')
            statuses={p.get('status','unknown') for p in postings}
            if statuses=={'delivered'}:status='delivered'
            elif statuses<={'in_delivery_point','delivered'}:status='in_delivery_point'
            elif 'canceled' in statuses:status='canceled'
            elif statuses & {'forming_failed','not_accepted_to_delivery'}:status='forming_failed'
            elif len(statuses)==1:status=next(iter(statuses))
            else:status='on_way'
            row['tracking_number']=tracking
            async with db.acquire() as c:
                await c.execute('''UPDATE commerce_pending_orders SET tracking_number=$2,ozon_status=$3,
                    status_checked_at=NOW(),post_purchase_failures=0 WHERE order_id=$1''',row['order_id'],tracking,status)
                await c.execute("INSERT INTO commerce_service_messages(order_id,kind) VALUES($1,'paid_email') ON CONFLICT DO NOTHING",row['order_id'])
                if status=='in_delivery_point':
                    await c.execute("INSERT INTO commerce_service_messages(order_id,kind) VALUES($1,'ready_email') ON CONFLICT DO NOTHING",row['order_id'])
            await _process_posting_operations(row, postings)
            for kind in ('paid_email','ready_email'):
                await _send_queued_customer_email(row,kind)
        except Exception as exc:
            async with db.acquire() as c:
                failures=await c.fetchval('''UPDATE commerce_pending_orders SET post_purchase_failures=post_purchase_failures+1
                    WHERE order_id=$1 RETURNING post_purchase_failures''',row['order_id'])
            if failures>=3:await _owner_post_purchase_alert(row,'Ozon: отправление/номер/статус не подтверждён после повторных проверок.')

async def _ozon_operations_schema():
    async with db.acquire() as c:
        await c.execute('''CREATE TABLE IF NOT EXISTS commerce_posting_operations (
            posting_number TEXT PRIMARY KEY,order_id BIGINT NOT NULL,approve_state TEXT,
            label_pdf BYTEA,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());''')

async def _process_posting_operations(row, postings):
    """Existing paid, identity-checked postings only. Never create an order here."""
    if row['payment_status'] != 'succeeded': return
    await _ozon_operations_schema()
    for posting in postings:
        number, state = posting['posting_number'], posting.get('status')
        async with db.acquire() as c:
            await c.execute('''INSERT INTO commerce_posting_operations(posting_number,order_id)
                VALUES($1,$2) ON CONFLICT DO NOTHING''',number,row['order_id'])
        if state == 'created' and os.getenv('OZON_AUTO_APPROVE','').lower() == 'true':
            async with db.acquire() as c:
                claim = await c.fetchval('''UPDATE commerce_posting_operations SET approve_state='claimed',updated_at=NOW()
                    WHERE posting_number=$1 AND order_id=$2 AND approve_state IS NULL RETURNING posting_number''',number,row['order_id'])
            if claim:
                approved = 'unknown'
                try:
                    await _ozon_post('/v1/posting/approve', {'posting_number':number})
                    approved = 'accepted'
                except Exception:
                    await _owner_post_purchase_alert(row,'Ozon: подтверждение сборки не подтверждено. Проверьте баланс и отправление; автоматического повторного подтверждения не будет.')
                async with db.acquire() as c:
                    await c.execute('UPDATE commerce_posting_operations SET approve_state=$2,updated_at=NOW() WHERE posting_number=$1',number,approved)
        elif state in ('forming','ready_for_shipping','in_container','acceptance_in_progress','on_way','in_delivery_point','in_courier_service','delivered'):
            async with db.acquire() as c:
                await c.execute("UPDATE commerce_posting_operations SET approve_state='verified',updated_at=NOW() WHERE posting_number=$1 AND approve_state IS DISTINCT FROM 'verified'",number)
        if state == 'ready_for_shipping':
            async with db.acquire() as c:
                missing = await c.fetchval('SELECT label_pdf IS NULL FROM commerce_posting_operations WHERE posting_number=$1',number)
            if missing:
                try:
                    pdf = await _ozon_post('/v1/posting/label',{'posting_number':number},binary=True)
                    async with db.acquire() as c:
                        await c.execute('UPDATE commerce_posting_operations SET label_pdf=$2,updated_at=NOW() WHERE posting_number=$1 AND label_pdf IS NULL',number,pdf)
                except Exception:
                    await _owner_post_purchase_alert(row,'Ozon: этикетка пока недоступна. Откройте отправление в Ozon для проверки; дубль не создавайте.')

def _operations_access(key):
    import hmac
    if not INTERNAL_KEY or not key or not hmac.compare_digest(INTERNAL_KEY,key):
        raise HTTPException(403,'Forbidden')

@app.get('/api/internal/commerce/shipment-queue',include_in_schema=False)
async def shipment_queue(x_internal_key: str | None = Header(default=None)):
    _operations_access(x_internal_key)
    await _ozon_operations_schema()
    async with db.acquire() as c:
        items = await c.fetch('''SELECT p.order_number,p.order_id,p.quantity,p.shipment_id,p.tracking_number,p.ozon_status,
            p.created_at,p.status_checked_at,o.posting_number,o.approve_state,o.label_pdf IS NOT NULL AS label_ready
            FROM commerce_pending_orders p LEFT JOIN commerce_posting_operations o USING(order_id)
            WHERE p.payment_status='succeeded' AND p.ozon_status IN ('created','forming','forming_failed','ready_for_shipping')
            ORDER BY p.created_at''')
    return {'automatic_confirmation':os.getenv('OZON_AUTO_APPROVE','').lower()=='true','items':[dict(r) for r in items]}

@app.get('/api/internal/commerce/shipment-labels.zip',include_in_schema=False)
async def shipment_labels(x_internal_key: str | None = Header(default=None)):
    _operations_access(x_internal_key)
    await _ozon_operations_schema()
    async with db.acquire() as c:
        items = await c.fetch('''SELECT o.posting_number,o.label_pdf FROM commerce_posting_operations o
            JOIN commerce_pending_orders p USING(order_id) WHERE p.payment_status='succeeded'
            AND p.ozon_status='ready_for_shipping' AND o.label_pdf IS NOT NULL ORDER BY p.created_at LIMIT 100''')
    import io,zipfile,re
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            name=re.sub(r'[^A-Za-z0-9-]','_',item['posting_number'])
            archive.writestr(name+'.pdf',bytes(item['label_pdf']))
    return Response(out.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="ozon-ready-labels.zip"','Cache-Control':'no-store'})

class CustomerStatusIn(BaseModel):
    order_key: str = Field(pattern=r'^[a-f0-9]{32}$')

@app.post('/api/commerce/customer-status')
async def customer_order_status(body: CustomerStatusIn):
    await _payment_schema()
    async with db.acquire() as c:
        row=await c.fetchrow('''SELECT payment_status,tracking_number,ozon_status,status_checked_at
            FROM commerce_pending_orders WHERE order_key=$1''',body.order_key)
    if not row:raise HTTPException(404,'Order not found')
    paid=row['payment_status']=='succeeded'
    return JSONResponse({'paid':paid,'tracking_number':row['tracking_number'] if paid else None,
        'status':row['ozon_status'] if paid else None,
        'status_label':STATUS_LABELS.get(row['ozon_status'],'уточняется') if paid else None},
        headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

@app.get('/api/internal/commerce/post-purchase-audit',include_in_schema=False)
async def post_purchase_audit(x_internal_key: str | None = Header(default=None)):
    import hmac
    if not INTERNAL_KEY or not x_internal_key or not hmac.compare_digest(INTERNAL_KEY,x_internal_key):raise HTTPException(403,'Forbidden')
    await _payment_schema()
    async with db.acquire() as c:
        rows=await c.fetch('''SELECT order_number,tracking_number,ozon_status,status_checked_at,post_purchase_failures
            FROM commerce_pending_orders WHERE payment_status='succeeded' ''')
        messages=await c.fetch('SELECT order_id,kind,state,attempts FROM commerce_service_messages')
    return {'email_transport':'resend','resend_configured':email_ready(),
        'production_email_enabled':os.getenv('CUSTOMER_EMAIL_ENABLED','').lower()=='true',
        'email_start_at':os.getenv('CUSTOMER_EMAIL_START_AT'),
        'orders':[dict(r) for r in rows],'messages':[dict(r) for r in messages]}

@app.post('/api/internal/commerce/resend-test',include_in_schema=False)
async def resend_test(x_internal_key: str | None = Header(default=None)):
    import hmac
    if not INTERNAL_KEY or not x_internal_key or not hmac.compare_digest(INTERNAL_KEY,x_internal_key):
        raise HTTPException(403,'Forbidden')
    if not email_ready():raise HTTPException(409,'Resend not configured')
    await _payment_schema()
    # A reserved service marker, not an InSales order. A durable claim prevents
    # duplicate tests even after HTTP retries, restarts or ambiguous Resend API replies.
    async with db.acquire() as c:
        claimed=await c.fetchval('''INSERT INTO commerce_service_messages(order_id,kind,state,attempts)
            VALUES(-1,'resend_test_20261002','claimed',1) ON CONFLICT DO NOTHING RETURNING order_id''')
        if claimed is None:
            state=await c.fetchval("SELECT state FROM commerce_service_messages WHERE order_id=-1 AND kind='resend_test_20261002'")
            return {'state':state,'already_attempted':True}
    result=await asyncio.to_thread(send_resend_test)
    async with db.acquire() as c:
        await c.execute("UPDATE commerce_service_messages SET state=$1,updated_at=NOW() WHERE order_id=-1 AND kind='resend_test_20261002'",result['state'])
    return result

@app.post('/api/internal/yookassa/bind', include_in_schema=False)
async def bind_native_payment(body: NativePaymentBinding, x_internal_key: str | None = Header(default=None)):
    payment_id = str(body.payment_id)
    payment = await _yookassa_read('payments/' + payment_id, x_internal_key)
    snapshot = body.model_dump(mode='json')
    expected = {'payment_id': payment_id, 'order_number': body.order_number, 'amount': Decimal(body.quantity)*800}
    error = validate_payment(payment, expected, os.getenv('YOOKASSA_SHOP_ID', ''))
    if error and error != 'not_paid':
        raise HTTPException(409, error)
    await _payment_schema()
    async with db.acquire() as c:
        await c.execute('''INSERT INTO insales_yookassa_payments
            (payment_id,order_id,order_number,quantity,amount,order_snapshot)
            VALUES($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT(payment_id) DO NOTHING''',
            payment_id, body.order_id, body.order_number, body.quantity, expected['amount'], json.dumps(snapshot))
        row = await c.fetchrow('SELECT order_id,order_number,quantity FROM insales_yookassa_payments WHERE payment_id=$1', payment_id)
        if row['order_id'] != body.order_id or row['order_number'] != body.order_number or row['quantity'] != body.quantity:
            raise HTTPException(409, 'Existing payment binding cannot be changed')
    return {'ok': True, 'payment_id': payment_id, 'order_id': body.order_id}

@app.post('/api/yookassa/notifications', include_in_schema=False)
async def yookassa_notification(body: YooNotification, request: Request = None):
    if request is not None:
        print('YOOKASSA_WEBHOOK_RECEIVED event=' + body.event + ' payment_id=' + str(body.object.id))
    if body.type != 'notification' or body.event not in ('payment.succeeded', 'payment.canceled'):
        return {'ok': True, 'ignored': True}
    payment_id = str(body.object.id)
    # A webhook body is not evidence of payment: re-fetch with server credentials.
    payment = await _yookassa_read('payments/' + payment_id, INTERNAL_KEY)
    await _payment_schema()
    await _register_pending_payment(payment)
    token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    owner = os.getenv('OWNER_CHAT_ID', '')
    async with db.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow('SELECT * FROM insales_yookassa_payments WHERE payment_id=$1 FOR UPDATE', payment_id)
            if not row:
                await c.execute('''INSERT INTO insales_yookassa_unbound_events(payment_id,status)
                    VALUES($1,$2) ON CONFLICT(payment_id) DO UPDATE SET status=EXCLUDED.status,observed_at=NOW()''', payment_id, payment.get('status'))
                # Native order data must be registered from a trusted source first.
                return {'ok': True, 'quarantined': True}
            error = validate_payment(payment, row, os.getenv('YOOKASSA_SHOP_ID', ''))
            if error:
                await c.execute('UPDATE insales_yookassa_payments SET status=$2,error_code=$3,updated_at=NOW() WHERE payment_id=$1',
                    payment_id, 'canceled' if payment.get('status') == 'canceled' else 'pending', error)
                return {'ok': True, 'processed': False, 'reason': error}
            if row['notification_state'] != 'not_sent':
                duplicate = True
            else:
                duplicate = False
            if not token or not owner:
                raise HTTPException(503, 'Owner notification channel is not configured')
            # Commit the claim BEFORE the external send. Ambiguous sends never retry automatically.
            if not duplicate:
                await c.execute("UPDATE insales_yookassa_payments SET status='succeeded',notification_state='claimed',error_code=NULL,updated_at=NOW() WHERE payment_id=$1", payment_id)
            snapshot = json.loads(row['order_snapshot']) if isinstance(row['order_snapshot'], str) else row['order_snapshot']
            amount = str(row['amount'])
    if duplicate:
        shipment = await _create_paid_ozon_shipment(payment_id)
        return {'ok': True, 'duplicate': True, 'shipment': shipment}
    message = ('✅ НОВЫЙ ОПЛАЧЕННЫЙ ЗАКАЗ\n\n'
        f"Заказ: №{snapshot['order_number']}\nОплачено: {amount} ₽\nКоличество: {snapshot['quantity']}\n"
        f"Получатель: {snapshot['recipient_name']}\nТелефон: {snapshot['phone']}\nEmail: {snapshot['email']}\n"
        f"Ozon ПВЗ: {snapshot['pickup_title']}\nАдрес ПВЗ: {snapshot['pickup_address']}")
    state, message_id = 'unknown', None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post('https://api.telegram.org/bot' + token + '/sendMessage',
                json={'chat_id': owner, 'text': message, 'disable_web_page_preview': True})
        if response.status_code == 200 and response.json().get('ok'):
            state, message_id = 'sent', response.json()['result']['message_id']
    except httpx.HTTPError:
        pass  # Never log Telegram URLs: they contain the bot credential.
    async with db.acquire() as c:
        await c.execute('UPDATE insales_yookassa_payments SET notification_state=$2,notification_message_id=$3,updated_at=NOW() WHERE payment_id=$1', payment_id, state, message_id)
    shipment = await _create_paid_ozon_shipment(payment_id)
    return {'ok': True, 'verified_paid': True, 'notification_state': state, 'shipment': shipment}

@app.get('/api/internal/yookassa/processing/{payment_id}', include_in_schema=False)
async def native_payment_processing(payment_id: uuid.UUID, x_internal_key: str | None = Header(default=None)):
    import hmac
    if not INTERNAL_KEY or not x_internal_key or not hmac.compare_digest(INTERNAL_KEY, x_internal_key):
        raise HTTPException(403, 'Forbidden')
    await _payment_schema()
    async with db.acquire() as c:
        row = await c.fetchrow('SELECT payment_id,order_id,status,notification_state,error_code,notification_message_id FROM insales_yookassa_payments WHERE payment_id=$1', str(payment_id))
    if not row:
        raise HTTPException(404, 'Payment is not linked to a trusted native order')
    return dict(row)

@app.get('/api/internal/yookassa/fulfillment-audit', include_in_schema=False)
async def payment_fulfillment_audit(x_internal_key: str | None = Header(default=None)):
    import hmac
    if not INTERNAL_KEY or not x_internal_key or not hmac.compare_digest(x_internal_key, INTERNAL_KEY):
        raise HTTPException(403, 'Forbidden')
    if not db:
        raise HTTPException(503, 'Database is not configured')
    async with db.acquire() as c:
        row = await c.fetchrow("SELECT count(*) AS fulfillment_rows, count(*) FILTER (WHERE ozon_order_id IS NOT NULL) AS ozon_orders_created, count(*) FILTER (WHERE payment_status='PAID') AS paid_fulfillment_rows FROM order_fulfillment")
    return dict(row)
