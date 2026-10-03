"""TTL cache of public Ozon catalogue, pushed by authenticated existing Railway."""
import datetime,json,math,re,time,uuid
import ydb
from collections import OrderedDict
RESPONSES=OrderedDict()
from pvz_transport import response

SCHEMA='''CREATE TABLE IF NOT EXISTS public_ozon_pvz_cache (
 city_key Utf8 NOT NULL, point_id Uint64 NOT NULL, geo_key Utf8,
 address_norm Utf8, latitude Double, longitude Double, payload Utf8,
 updated_at Timestamp, expires_at Timestamp,
 INDEX by_geo GLOBAL ON (geo_key),
 PRIMARY KEY(city_key,point_id)
) WITH (TTL=Interval("PT0S") ON expires_at);'''
TTL=48*3600
ready=False

def city_key(value):
    value=re.sub(r'^(?:г\.?\s+|город\s+)','',value.strip(),flags=re.I).casefold()
    return {'samara':'самара','moscow':'москва','kazan':'казань'}.get(value,value)

def store(data,execute):
    global ready
    if not ready: execute(SCHEMA);ready=True
    points=data.get('points') or []
    if not isinstance(points,list) or len(points)>100: raise ValueError('INVALID_CACHE_BATCH')
    declarations=[];params={};values=[]
    now=datetime.datetime.now(datetime.timezone.utc)
    for p in points:
        if p.get('type')!='pvz' or not p.get('shipment_method_ids') or not p.get('full_address'): continue
        lat=float(p['latitude']);lon=float(p['longitude'])
        if not (-90<=lat<=90 and -180<=lon<=180): continue
        updated=datetime.datetime.fromisoformat(p['cache_updated_at'])
        expires=updated+datetime.timedelta(seconds=TTL)
        if expires<=now: continue
        parts=[v.strip() for v in p['full_address'].split(',')]
        city=(parts[2] if len(parts)>2 and re.search('обл|край|республик|округ',parts[1],re.I) else parts[1] if len(parts)>1 else '')
        i=len(values);public={k:p[k] for k in ('delivery_point_id','shipment_method_ids','name','full_address','type','latitude','longitude','schedule') if k in p}
        fields=[('city','Utf8',city_key(city)),('id','Uint64',int(p['delivery_point_id'])),('geo','Utf8',f'{math.floor(lat)}:{math.floor(lon)}'),('addr','Utf8',p['full_address'].casefold()),('lat','Double',lat),('lon','Double',lon),('payload','Utf8',json.dumps(public,ensure_ascii=False)),('updated','Timestamp',updated),('expires','Timestamp',expires)]
        args=[]
        for name,typ,val in fields:
            key=f'${name}{i}';declarations.append(f'DECLARE {key} AS {typ};');params[key]=ydb.TypedValue(val,getattr(ydb.PrimitiveType,typ));args.append(key)
        values.append('('+','.join(args)+')')
    if values:
        execute('\n'.join(declarations)+'\nUPSERT INTO public_ozon_pvz_cache (city_key,point_id,geo_key,address_norm,latitude,longitude,payload,updated_at,expires_at) VALUES '+','.join(values)+';',params)
    return {'ok':True,'stored':len(values),'ttl_seconds':TTL}

def read(event,execute):
    received=datetime.datetime.now(datetime.timezone.utc).isoformat();start=time.perf_counter();headers={str(k).lower():str(v) for k,v in (event.get('headers') or {}).items()};origin=headers.get('origin','')
    q=event.get('queryStringParameters') or {}
    try:rid=str(uuid.UUID(q.get('pvz_request_id','')))
    except (ValueError,TypeError,AttributeError):rid=str(uuid.uuid4())
    try:
        kind=q.get('kind','points');limit=max(1,min(int(q.get('limit',50)),150 if kind=='map-points' else 50))
        declarations=['DECLARE $limit AS Uint64;'];params={'$limit':ydb.TypedValue(limit,ydb.PrimitiveType.Uint64)};view='public_ozon_pvz_cache';where=['expires_at>CurrentUtcTimestamp()']
        if kind=='points':
            city=city_key(q.get('city',''));query=q.get('query','').strip().casefold()
            if len(query)<2 or len(query)>100 or len(city)>100: raise ValueError('INVALID_QUERY')
            region=bool(re.search(r'обл|край|республик|округ',city,re.I))
            if region:city=''
            if city:
                declarations.append('DECLARE $city AS Utf8;');params['$city']=city;where.append('city_key=$city')
            if city_key(query)==city:query=city
            for i,token in enumerate(query.replace(',',' ').replace('.',' ').split()):
                key=f'$query{i}';declarations.append(f'DECLARE {key} AS Utf8;');params[key]=token;where.append(f'String::Contains(CAST(address_norm AS String),CAST({key} AS String))')
        elif kind=='map-points':
            south,west,north,east=[float(q[k]) for k in ('south','west','north','east')]
            if not (-90<=south<north<=90 and -180<=west<=180 and -180<=east<=180):raise ValueError('INVALID_BOUNDS')
            ranges=[(west,east)] if west<=east else [(west,180),(-180,east)]
            keys=[f'{lat}:{lon}' for lat in range(math.floor(south),math.floor(north)+1) for lo,hi in ranges for lon in range(math.floor(lo),math.floor(hi)+1)]
            if len(keys)>512:raise ValueError('ZOOM_IN')
            declarations+=['DECLARE $keys AS List<Utf8>;','DECLARE $south AS Double;','DECLARE $west AS Double;','DECLARE $north AS Double;','DECLARE $east AS Double;']
            params.update({'$keys':keys,'$south':south,'$west':west,'$north':north,'$east':east});view+=' VIEW by_geo';where+=['geo_key IN $keys','latitude BETWEEN $south AND $north','longitude BETWEEN $west AND $east' if west<=east else '(longitude>=$west OR longitude<=$east)']
        else:raise ValueError('INVALID_KIND')
        condition=' AND '.join(where);prefix='\n'.join(declarations)
        cache_key=tuple(sorted((k,str(v)) for k,v in q.items() if k in ('kind','limit','city','query','south','west','north','east')));cached=RESPONSES.get(cache_key);now=time.time();response_source='real_ozon_ydb_cache'
        if cached and now-cached[0]<300 and cached[1]>now:
            count,items=cached[2:];response_source='memory_real_ozon_cache';normalize_start=datetime.datetime.now(datetime.timezone.utc).isoformat();normalize_end=normalize_start
        else:
            try:result=execute(prefix+f'\nSELECT COUNT(*) AS n FROM {view} WHERE {condition};\nSELECT payload,expires_at FROM {view} WHERE {condition} LIMIT $limit;',params)
            except Exception:
                if not cached or now-cached[0]>3600 or cached[1]<=now:raise
                count,items=cached[2:];response_source='memory_real_ozon_fallback';result=None
            if result is not None:
                normalize_start=datetime.datetime.now(datetime.timezone.utc).isoformat();count=int(result[0].rows[0].n);items=[json.loads(r.payload) for r in result[1].rows];normalize_end=datetime.datetime.now(datetime.timezone.utc).isoformat()
                expiry=min((r.expires_at.timestamp() if hasattr(r.expires_at,'timestamp') else float(r.expires_at)/1000000 for r in result[1].rows),default=now)
                if items:
                    RESPONSES[cache_key]=(now,expiry,count,items);RESPONSES.move_to_end(cache_key)
                    while len(RESPONSES)>128:RESPONSES.popitem(last=False)
            else:normalize_start=datetime.datetime.now(datetime.timezone.utc).isoformat();normalize_end=normalize_start
        elapsed=round((time.perf_counter()-start)*1000,2)
        print(json.dumps({'event':'pvz_ru_cache_response','pvz_request_id':rid,'request_received_at':received,'response_sent_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'geocode_start':None,'geocode_end':None,'ozon_start':None,'ozon_end':None,'ozon_http_status':None,'ozon_result_count':len(items),'upstream_skipped':'fresh_real_ozon_cache','normalize_start':normalize_start,'normalize_end':normalize_end,'coordinates':{k:q[k] for k in ('south','west','north','east') if k in q},'client_ua':headers.get('user-agent','')[:300],'city':q.get('city',''),'kind':kind,'count':count,'raw_count':len(items),'normalized_count':len(items),'total_ms':elapsed,'source':response_source,'error_code':None,'timeout_source':None},ensure_ascii=False),flush=True)
        return response(200,{'pvz_request_id':rid,'count':count,'items':items,'source':response_source,'transport':'yandex-ru','cache_ttl_seconds':TTL},origin)
    except Exception as exc:
        print(json.dumps({'event':'pvz_ru_cache_error','pvz_request_id':rid,'error_code':type(exc).__name__,'detail':str(exc)[:200],'total_ms':round((time.perf_counter()-start)*1000,2)},ensure_ascii=False),flush=True)
        return response(503,{'pvz_request_id':rid,'error_code':'REAL_CACHE_UNAVAILABLE','diagnostic_code':type(exc).__name__,'timeout_source':'ydb_session_or_query'},origin)
