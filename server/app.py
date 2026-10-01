import os, time, uuid, asyncio, json
from urllib.parse import urljoin, urlsplit

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
        "https://www.snoved-ai.ru",
        "https://snoved-ai.ru",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Internal-Key"],
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

async def _ozon_post(path: str, payload: dict, idempotency_key: str | None = None):
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
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"message": r.text[:500]}
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
    global db, _sync_task
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
    global _sync_task
    if _sync_task:
        _sync_task.cancel()
        try:
            await _sync_task
        except asyncio.CancelledError:
            pass
    await _http.aclose()
    if db:
        await db.close()

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
        "points_cached": int(cached or 0),
        "sync_complete": sync_complete,
    }

@app.get("/api/ozon/points")
async def points(query: str = Query(min_length=2, max_length=100), limit: int = Query(30, ge=1, le=50)):
    if not db:
        raise HTTPException(503, "Database is not configured")
    q = " ".join(query.split())
    like = "%" + q + "%"
    async with db.acquire() as c:
        count = await c.fetchval("""
            SELECT COUNT(*) FROM ozon_delivery_points_cache
            WHERE is_active=TRUE AND (point_address ILIKE $1 OR point_name ILIKE $1)
        """, like)
        rows = await c.fetch("""
            SELECT delivery_point_id, shipment_method_ids, point_name, point_address, point_type,
                   latitude, longitude, storage_period_days, schedule
            FROM ozon_delivery_points_cache
            WHERE is_active=TRUE AND (point_address ILIKE $1 OR point_name ILIKE $1)
            ORDER BY updated_at DESC
            LIMIT $2
        """, like, limit)
    return {"query": query, "count": int(count or 0), "items": [_cache_row_to_point(r) for r in rows]}


@app.get("/api/ozon/map-points")
async def map_points(
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
        count = await c.fetchval(f"""
            SELECT COUNT(*) FROM ozon_delivery_points_cache
            WHERE is_active=TRUE AND latitude BETWEEN $1 AND $3 AND {lon_where}
        """, *args)
        rows = await c.fetch(f"""
            SELECT delivery_point_id, shipment_method_ids, point_name, point_address, point_type,
                   latitude, longitude, storage_period_days, schedule
            FROM ozon_delivery_points_cache
            WHERE is_active=TRUE AND latitude BETWEEN $1 AND $3 AND {lon_where}
            ORDER BY ((latitude-$5)*(latitude-$5) + (longitude-$6)*(longitude-$6))
            LIMIT $7
        """, *args, center_lat, center_lon, limit)
    return {"count": int(count or 0), "returned": len(rows), "items": [_cache_row_to_point(r) for r in rows]}

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
from payment_validation import validate_payment

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
                payment_id TEXT PRIMARY KEY, status TEXT, observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW());''')
        _payment_schema_ready = True

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
async def yookassa_notification(body: YooNotification):
    if body.type != 'notification' or body.event not in ('payment.succeeded', 'payment.canceled'):
        return {'ok': True, 'ignored': True}
    payment_id = str(body.object.id)
    # A webhook body is not evidence of payment: re-fetch with server credentials.
    payment = await _yookassa_read('payments/' + payment_id, INTERNAL_KEY)
    await _payment_schema()
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
                return {'ok': True, 'duplicate': True}
            if not token or not owner:
                raise HTTPException(503, 'Owner notification channel is not configured')
            # Commit the claim BEFORE the external send. Ambiguous sends never retry automatically.
            await c.execute("UPDATE insales_yookassa_payments SET status='succeeded',notification_state='claimed',error_code=NULL,updated_at=NOW() WHERE payment_id=$1", payment_id)
            snapshot = json.loads(row['order_snapshot']) if isinstance(row['order_snapshot'], str) else row['order_snapshot']
            amount = str(row['amount'])
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
    # No Ozon shipment, fulfillment, or review action is performed here.
    return {'ok': True, 'verified_paid': True, 'notification_state': state}

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
