import os, time, uuid, asyncio
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

def _safe_origin(url: str):
    p = urlsplit(url)
    return (p.scheme.lower(), p.hostname.lower() if p.hostname else "", p.port or (443 if p.scheme == "https" else 80))

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

@app.on_event("startup")
async def startup():
    global db
    if DATABASE_URL:
        db = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
        async with db.acquire() as c:
            await c.execute("""
                CREATE TABLE IF NOT EXISTS ozon_delivery_selections (
                    source_token TEXT PRIMARY KEY,
                    delivery_point_id BIGINT NOT NULL,
                    shipment_method_id BIGINT NOT NULL,
                    point_name TEXT,
                    point_address TEXT,
                    selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

@app.on_event("shutdown")
async def shutdown():
    await _http.aclose()
    if db:
        await db.close()

@app.get("/health")
async def health():
    return {
        "ok": True,
        "ozon_configured": bool(CLIENT_ID and CLIENT_SECRET),
        "db_configured": bool(DATABASE_URL),
        "points_cached": len(_points_cache),
    }

@app.get("/api/ozon/points")
async def points(query: str = Query(min_length=2, max_length=100), limit: int = Query(30, ge=1, le=50)):
    rows = await _load_all_points()
    q = " ".join(query.lower().replace("ё", "е").split())
    def norm(v): return " ".join(str(v or "").lower().replace("ё", "е").split())
    found = [x for x in rows if q in norm(x["full_address"]) or q in norm(x["name"])]
    return {"query": query, "count": len(found), "items": found[:limit]}

@app.post("/api/ozon/refresh-points")
async def refresh_points(x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(403, "Forbidden")
    rows = await _load_all_points(force=True)
    return {"ok": True, "count": len(rows)}

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
                (source_token, delivery_point_id, shipment_method_id, point_name, point_address, selected_at)
                VALUES ($1,$2,$3,$4,$5,NOW())
                ON CONFLICT (source_token) DO UPDATE SET
                  delivery_point_id=EXCLUDED.delivery_point_id,
                  shipment_method_id=EXCLUDED.shipment_method_id,
                  point_name=EXCLUDED.point_name,
                  point_address=EXCLUDED.point_address,
                  selected_at=NOW()
            """, body.source_token, body.delivery_point_id, body.shipment_method_id, body.name, body.address)
    return {"ok": True}

@app.get("/api/ozon/selection/{source_token}")
async def get_selection(source_token: str, x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_KEY or x_internal_key != INTERNAL_KEY:
        raise HTTPException(403, "Forbidden")
    if not db:
        raise HTTPException(503, "Database is not configured")
    async with db.acquire() as c:
        row = await c.fetchrow("""
            SELECT delivery_point_id, shipment_method_id, point_name, point_address, selected_at
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
    posting = _parcel(1, body.shipment_method_id)
    posting["posting_external_id"] = body.order_external_id
    posting["description"] = "Съёмная ручка для сковороды, 1 шт."
    payload = {
        "order_external_id": body.order_external_id,
        "recipient": {"phone_number": body.phone_number, "full_name": body.full_name},
        "delivery": {"delivery_point": {"delivery_point_id": body.delivery_point_id}},
        "postings": [posting],
    }
    idem = str(uuid.uuid5(uuid.NAMESPACE_URL, "snoved-ozon:" + (body.order_external_id or body.phone_number + ":" + str(body.delivery_point_id))))
    result = await _ozon_post("/v1/order/create", payload, idempotency_key=idem)
    return {"ok": True, "ozon": result}
