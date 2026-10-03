"""Public PVZ transport only. Never accepts order data or authentication keys."""
import json, time, uuid, urllib.request, urllib.parse, gzip, base64
from collections import OrderedDict
from datetime import datetime, timezone

ORIGIN='https://xn--163-5cdt3dgrs.xn--p1ai'
UPSTREAM='https://api.xn--163-5cdt3dgrs.xn--p1ai'
CACHE=OrderedDict()
TTL=3600

def client_trace(event):
    headers={str(k).lower():str(v) for k,v in (event.get('headers') or {}).items()}
    origin=headers.get('origin','')
    raw=event.get('body') or ''
    if len(raw)>2048: return response(413,{'ok':False},origin)
    try:
        data=json.loads(raw)
        uuid.UUID(data['pvz_request_id'])
        req=urllib.request.Request(UPSTREAM+'/api/ozon/client-trace',data=raw.encode(),headers={'Content-Type':'application/json','User-Agent':headers.get('user-agent','PVZ-RU-transport')[:300]},method='POST')
        with urllib.request.urlopen(req,timeout=2) as r: r.read(2048)
        return response(200,{'ok':True},origin)
    except Exception: return response(503,{'ok':False},origin)

def response(status,body,origin):
    headers={'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','Vary':'Origin'}
    if origin==ORIGIN: headers['Access-Control-Allow-Origin']=origin
    headers['Content-Encoding']='gzip'
    encoded=gzip.compress(json.dumps(body,ensure_ascii=False).encode(),compresslevel=5)
    return {'statusCode':status,'headers':headers,'isBase64Encoded':True,'body':base64.b64encode(encoded).decode()}

def handler(event):
    started=time.perf_counter()
    headers={str(k).lower():str(v) for k,v in (event.get('headers') or {}).items()}
    origin=headers.get('origin','')
    params=event.get('queryStringParameters') or {}
    try: request_id=str(uuid.UUID(params.get('pvz_request_id','')))
    except (ValueError,TypeError,AttributeError): request_id=str(uuid.uuid4())
    kind=params.get('kind','points')
    if kind not in ('points','map-points'): return response(400,{'error_code':'INVALID_KIND'},origin)
    allowed=('query','city','limit') if kind=='points' else ('south','west','north','east','limit')
    args={k:str(params[k])[:100] for k in allowed if k in params}
    try: args['limit']=str(max(1,min(int(args.get('limit','50')),50 if kind=='points' else 150)))
    except ValueError: return response(400,{'error_code':'INVALID_LIMIT'},origin)
    cache_key=(kind,tuple(sorted(args.items())))
    now=time.time()
    cached=CACHE.get(cache_key)
    log={'event':'pvz_ru_transport','pvz_request_id':request_id,'request_received_at':datetime.now(timezone.utc).isoformat(),'client_ua':headers.get('user-agent','')[:300],'city':args.get('city',''),'kind':kind}
    try:
        if cached and now-cached[0]<300:
            data=dict(cached[1]); source='real_cache_fresh'; upstream_ms=0
        else:
            args.update({'pvz_request_id':request_id})
            req=urllib.request.Request(UPSTREAM+'/api/ozon/'+kind+'?'+urllib.parse.urlencode(args),headers={'User-Agent':headers.get('user-agent','PVZ-RU-transport')[:300],'X-PVZ-Transport':'yandex-ru'})
            t=time.perf_counter()
            try:
                with urllib.request.urlopen(req,timeout=5) as r:
                    if r.status!=200: raise ValueError('UPSTREAM_STATUS')
                    data=json.loads(r.read(2000000))
                if not isinstance(data.get('items'),list): raise ValueError('UPSTREAM_FORMAT')
                CACHE[cache_key]=(now,dict(data)); CACHE.move_to_end(cache_key)
                while len(CACHE)>128: CACHE.popitem(last=False)
                source='railway_real_cache';upstream_ms=round((time.perf_counter()-t)*1000,2)
            except Exception as exc:
                if not cached or now-cached[0]>TTL: raise
                data=dict(cached[1]);source='real_cache_fallback';upstream_ms=round((time.perf_counter()-t)*1000,2)
                log['upstream_error']=type(exc).__name__
        data.update({'pvz_request_id':request_id,'transport':'yandex-ru','transport_source':source,'cache_ttl_seconds':TTL,'cache_age_seconds':round(now-cached[0]) if cached else 0})
        log.update({'status':200,'raw_count':len(data['items']),'upstream_ms':upstream_ms,'total_ms':round((time.perf_counter()-started)*1000,2),'source':source,'error_code':None,'timeout_source':None})
        print(json.dumps(log,ensure_ascii=False),flush=True)
        return response(200,data,origin)
    except Exception as exc:
        log.update({'status':503,'total_ms':round((time.perf_counter()-started)*1000,2),'error_code':type(exc).__name__,'timeout_source':'upstream_transport'})
        print(json.dumps(log,ensure_ascii=False),flush=True)
        return response(503,{'pvz_request_id':request_id,'error_code':'PVZ_UPSTREAM_UNAVAILABLE'},origin)
