"""Public PVZ response timing/compression only; never processes order data."""
import gzip
import json
import time
import uuid
from datetime import datetime, timezone
from starlette.responses import Response

def start(request):
    try: request_id=str(uuid.UUID(request.query_params.get('pvz_request_id','')))
    except (ValueError, TypeError, AttributeError): request_id=str(uuid.uuid4())
    received=datetime.now(timezone.utc).isoformat()
    request.state.pvz_timing={'request_id':request_id,'received':received,'started':time.perf_counter(),'stage':time.perf_counter(),'durations':{},'stage_times':{}}
    print(json.dumps({'event':'pvz_request_received','pvz_request_id':request_id,'request_id':request_id,'request_received_at':received,'client_ua':request.headers.get('user-agent','')[:300],'transport':request.headers.get('x-pvz-transport','direct')[:30],'path':request.url.path,'city':request.query_params.get('city',''),'query':request.query_params.get('query',''),'coordinates':{k:request.query_params[k] for k in ('south','west','north','east') if k in request.query_params},'geocode_start':None,'geocode_end':None,'ozon_start':None,'ozon_end':None,'ozon_http_status':None,'provider_stage':'skipped_real_server_cache'},ensure_ascii=False),flush=True)

def stage(request,name):
    timing=request.state.pvz_timing
    now=time.perf_counter()
    timing['durations'][name]=round((now-timing['stage'])*1000,2)
    timing['stage_times'][name]=datetime.now(timezone.utc).isoformat()
    timing['stage']=now

def failure(request,error_code):
    timing=request.state.pvz_timing
    print(json.dumps({'event':'pvz_error','pvz_request_id':timing['request_id'],'request_received_at':timing['received'],'response_sent_at':datetime.now(timezone.utc).isoformat(),'total_ms':round((time.perf_counter()-timing['started'])*1000,2),'error_code':error_code,'timeout_source':'backend' if 'Timeout' in error_code else None}),flush=True)

def response(request,payload):
    timing=request.state.pvz_timing
    payload={**payload,'pvz_request_id':timing['request_id'],'source':'postgres_real_ozon_cache'}
    raw=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    stage(request,'normalize_encode')
    compressed='gzip' in request.headers.get('accept-encoding','').lower()
    body=gzip.compress(raw,compresslevel=5) if compressed else raw
    stage(request,'compress')
    total=round((time.perf_counter()-timing['started'])*1000,2)
    durations=timing['durations']
    event={'event':'pvz_response_sent','request_id':timing['request_id'],'pvz_request_id':timing['request_id'],'request_received_at':timing['received'],'response_sent_at':datetime.now(timezone.utc).isoformat(),'normalize_start':timing['stage_times'].get('points_query'),'normalize_end':timing['stage_times'].get('normalize_encode'),'path':request.url.path,'city':request.query_params.get('city',''),'query':request.query_params.get('query',''),'source':'postgres_real_ozon_cache','geocode_used':False,'ozon_request_used':False,'geocode_ms':0,'ozon_api_ms':0,'ozon_http_status':None,'ozon_result_count':payload['count'],'durations_ms':durations,'total_ms':total,'count':payload['count'],'raw_count':len(payload['items']),'normalized_count':len(payload['items']),'returned':len(payload['items']),'bytes':len(body),'raw_bytes':len(raw),'error_code':None,'timeout_source':None}
    print(json.dumps(event,ensure_ascii=False),flush=True)
    headers={'Cache-Control':'no-store','Vary':'Accept-Encoding','X-PVZ-Request-ID':timing['request_id'],'Server-Timing':','.join(f'{name};dur={duration}' for name,duration in durations.items())+f',total;dur={total}'}
    if compressed:headers['Content-Encoding']='gzip'
    return Response(body,media_type='application/json',headers=headers)
