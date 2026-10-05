"""One PostgreSQL-backed request cache for the three existing Wordstat callers.

Provider responses are stored without authentication material. Cost is an
estimate, not a provider invoice. Missing/error responses are never zero demand.
"""
import hashlib,json,os
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone

BASE='https://searchapi.api.cloud.yandex.net/v2/wordstat'
MAX_PROVIDER_CALLS_PER_MSK_DAY=64
ESTIMATED_UNIT_RUB=.02
OPERATIONS={'topRequests','dynamics','regions'}

@dataclass
class Result:
    status_code:int
    data:dict
    cache_hit:bool=False
    def json(self):return self.data

def scope(operation,body,folder):
    if operation not in OPERATIONS:raise ValueError('unsupported_operation')
    b=dict(body)
    if not isinstance(b.get('phrase'),str) or not b['phrase'].strip() or len(b['phrase'])>500:
        raise ValueError('invalid_phrase')
    if b.get('folderId',folder)!=folder:raise ValueError('folder_mismatch')
    b['folderId']=folder;b['phrase']=' '.join(b['phrase'].split())
    # Daily endpoints receive calendar boundaries consistently; the operation,
    # period, regions, devices and window are all part of the key.
    for key in ('fromDate','toDate'):
        if key in b:
            d=datetime.fromisoformat(str(b[key]).replace('Z','+00:00'))
            if d.tzinfo is None:raise ValueError('timezone_required')
            b[key]=d.astimezone(timezone.utc).date().isoformat()+'T00:00:00Z'
    encoded=json.dumps({'operation':operation,'body':b},ensure_ascii=False,sort_keys=True,separators=(',',':'))
    return b,hashlib.sha256(encoded.encode()).hexdigest()

async def schema(c):
    async with c.transaction():
        await c.execute('SELECT pg_advisory_xact_lock(714566818)')
        await c.execute('''CREATE TABLE IF NOT EXISTS wordstat_api_cache (
          cache_key TEXT PRIMARY KEY, operation TEXT NOT NULL, request_scope JSONB NOT NULL,
          status_code INTEGER NOT NULL, raw_response JSONB NOT NULL,
          fetched_at TIMESTAMPTZ NOT NULL, cache_until TIMESTAMPTZ NOT NULL);
          CREATE TABLE IF NOT EXISTS wordstat_api_usage (
          id BIGSERIAL PRIMARY KEY, caller TEXT NOT NULL, cache_key TEXT NOT NULL,
          status_code INTEGER NOT NULL, requested_at TIMESTAMPTZ NOT NULL,
          estimated_unit_cost_rub NUMERIC NOT NULL, cost_status TEXT NOT NULL);''')

async def usage(c,now):
    rows=await c.fetch('''SELECT (requested_at AT TIME ZONE 'Europe/Moscow')::date date_msk,
      count(*) calls,sum(estimated_unit_cost_rub) estimated_rub FROM wordstat_api_usage
      WHERE (requested_at AT TIME ZONE 'Europe/Moscow')::date>=
      ($1::timestamptz AT TIME ZONE 'Europe/Moscow')::date-6 GROUP BY 1 ORDER BY 1''',now)
    today=now.astimezone(__import__('zoneinfo').ZoneInfo('Europe/Moscow')).date()
    current=next((r for r in rows if r['date_msk']==today),None)
    return {'checked_at':now.isoformat(),'observation_scope':'shared cache rollout onward',
      'cost_status':'ESTIMATED_NOT_INVOICED','provider_calls_today':int(current['calls']) if current else 0,
      'estimated_rub_today':float(current['estimated_rub']) if current else 0,
      'provider_calls_7d':sum(int(r['calls']) for r in rows),
      'estimated_rub_7d':sum(float(r['estimated_rub']) for r in rows),
      'provider_daily_call_cap':MAX_PROVIDER_CALLS_PER_MSK_DAY,
      'daily_estimated_cap_rub':MAX_PROVIDER_CALLS_PER_MSK_DAY*ESTIMATED_UNIT_RUB}

async def request(c,http,operation,body,caller,now=None):
    now=now or datetime.now(timezone.utc)
    key,folder=os.getenv('YANDEX_WORDSTAT_API_KEY'),os.getenv('YANDEX_FOLDER_ID')
    if not key or not folder:return Result(503,{'error':'WORDSTAT_CREDENTIALS_UNAVAILABLE'})
    b,cache_key=scope(operation,body,folder)
    await schema(c)
    async with c.transaction():
        await c.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',cache_key)
        cached=await c.fetchrow('SELECT status_code,raw_response FROM wordstat_api_cache WHERE cache_key=$1 AND cache_until>$2',cache_key,now)
        if cached:
            raw=cached['raw_response'];raw=json.loads(raw) if isinstance(raw,str) else raw
            return Result(cached['status_code'],raw,True)
        # Serial reservation prevents simultaneous callers exceeding the cap.
        await c.execute('SELECT pg_advisory_xact_lock(714566817)')
        recent_quota=await c.fetchval('''SELECT EXISTS(SELECT 1 FROM wordstat_api_usage
          WHERE status_code IN (401,403,429,502) AND requested_at>$1)''',now-timedelta(hours=6))
        if recent_quota:return Result(503,{'error':'WORDSTAT_PROVIDER_BACKOFF'})
        calls=await c.fetchval("SELECT count(*) FROM wordstat_api_usage WHERE (requested_at AT TIME ZONE 'Europe/Moscow')::date=($1::timestamptz AT TIME ZONE 'Europe/Moscow')::date",now)
        if calls>=MAX_PROVIDER_CALLS_PER_MSK_DAY:return Result(429,{'error':'WORDSTAT_DAILY_CALL_CAP','cost_status':'ESTIMATED'})
        try:
            response=await http.post(BASE+'/'+operation,headers={'Authorization':'Api-key '+key},json=b,timeout=30)
            status=response.status_code
            # Store provider JSON only on success; error bodies may expose
            # operational details and are not useful evidence of zero demand.
            raw=response.json() if status==200 else {'error':'WORDSTAT_PROVIDER_HTTP','provider_http':status}
        except Exception as exc:
            status=502;raw={'error':'WORDSTAT_TRANSPORT_FAILURE','error_type':type(exc).__name__}
        await c.execute('''INSERT INTO wordstat_api_usage(caller,cache_key,status_code,requested_at,estimated_unit_cost_rub,cost_status)
          VALUES($1,$2,$3,$4,$5,'ESTIMATED_NOT_INVOICED')''',caller,cache_key,status,now,ESTIMATED_UNIT_RUB)
        ttl=timedelta(days=7) if operation=='regions' or b.get('period')=='PERIOD_WEEKLY' else timedelta(days=1)
        if status!=200:ttl=timedelta(hours=6)
        await c.execute('''INSERT INTO wordstat_api_cache VALUES($1,$2,$3::jsonb,$4,$5::jsonb,$6,$7)
          ON CONFLICT(cache_key) DO UPDATE SET status_code=EXCLUDED.status_code,raw_response=EXCLUDED.raw_response,
          fetched_at=EXCLUDED.fetched_at,cache_until=EXCLUDED.cache_until''',cache_key,operation,json.dumps(b),status,json.dumps(raw),now,now+ttl)
        return Result(status,raw)
