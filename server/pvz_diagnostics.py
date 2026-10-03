"""Public PVZ response timing/compression only; never processes order data."""
import gzip
import json
import time
import uuid
from starlette.responses import Response

def start(request):
    request.state.pvz_timing={'request_id':str(uuid.uuid4()),'started':time.perf_counter(),'stage':time.perf_counter(),'durations':{}}
    print(json.dumps({'event':'pvz_request_received','request_id':request.state.pvz_timing['request_id'],'path':request.url.path,'city':request.query_params.get('city',''),'query':request.query_params.get('query',''),'map_bounds_present':all(k in request.query_params for k in ('south','west','north','east'))},ensure_ascii=False),flush=True)

def stage(request,name):
    timing=request.state.pvz_timing
    now=time.perf_counter()
    timing['durations'][name]=round((now-timing['stage'])*1000,2)
    timing['stage']=now

def response(request,payload):
    timing=request.state.pvz_timing
    raw=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    stage(request,'normalize_encode')
    compressed='gzip' in request.headers.get('accept-encoding','').lower()
    body=gzip.compress(raw,compresslevel=5) if compressed else raw
    stage(request,'compress')
    total=round((time.perf_counter()-timing['started'])*1000,2)
    durations=timing['durations']
    event={'event':'pvz_response_sent','request_id':timing['request_id'],'path':request.url.path,'city':request.query_params.get('city',''),'query':request.query_params.get('query',''),'source':'postgres_real_ozon_cache','geocode_used':False,'ozon_request_used':False,'geocode_ms':0,'ozon_api_ms':0,'durations_ms':durations,'total_ms':total,'count':payload['count'],'returned':len(payload['items']),'bytes':len(body),'raw_bytes':len(raw)}
    print(json.dumps(event,ensure_ascii=False),flush=True)
    headers={'Cache-Control':'no-store','Vary':'Accept-Encoding','X-PVZ-Request-ID':timing['request_id'],'Server-Timing':','.join(f'{name};dur={duration}' for name,duration in durations.items())+f',total;dur={total}'}
    if compressed:headers['Content-Encoding']='gzip'
    return Response(body,media_type='application/json',headers=headers)
