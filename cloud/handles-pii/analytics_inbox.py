"""Short-lived non-PII analytics transport. PostgreSQL remains reporting store."""
import base64,json,uuid,datetime,re,math
from urllib.parse import urlsplit

ORIGIN='https://xn--163-5cdt3dgrs.xn--p1ai'
CLIENT_EVENTS=set('VIDEO_CTA_VIEW VIDEO_OPEN VIDEO_PLAY VIDEO_PAUSE VIDEO_25 VIDEO_50 VIDEO_75 VIDEO_COMPLETE VIDEO_CLOSE SITE_SESSION PRODUCT_VIEW BUY_BUTTON_CLICK CHECKOUT_OPEN CONTACTS_STARTED CONTACTS_COMPLETED PVZ_PICKER_OPEN PVZ_SEARCH PVZ_LOADED PVZ_SELECTED PAYMENT_BUTTON_CLICK SCROLL_25 SCROLL_50 SCROLL_75 SCROLL_90 PRODUCT_GALLERY_INTERACTION REVIEWS_VIEW SESSION_TIMING WEB_VITAL JS_ERROR PVZ_ERROR PVZ_TIMEOUT PAYMENT_ERROR'.split())

def ensure(execute):
    execute('''CREATE TABLE IF NOT EXISTS analytics_inbox(
        batch_id Utf8 NOT NULL,payload Utf8,created_at Timestamp,expire_at Timestamp,
        PRIMARY KEY(batch_id)) WITH(TTL=Interval("PT0S") ON expire_at);''')

def response(status,body):
    return {'statusCode':status,'headers':{'Content-Type':'application/json','Access-Control-Allow-Origin':ORIGIN,'Cache-Control':'no-store','Vary':'Origin'},'body':json.dumps(body)}

def ingest(event,execute):
    headers={str(k).lower():str(v) for k,v in (event.get('headers') or {}).items()}
    if headers.get('origin')!=ORIGIN:return response(403,{'ok':False})
    try:
        raw=event.get('body') or ''
        if event.get('isBase64Encoded'):raw=base64.b64decode(raw).decode('utf-8')
        if len(raw)>16000:return response(413,{'ok':False})
        data=json.loads(raw);sid=str(uuid.UUID(data['session_id']));bid=str(uuid.UUID(data['batch_id']))
        allowed_top={'session_id','checkout_session_id','attribution','referrer_host','new_visitor','events','batch_id'}
        if set(data)-allowed_top:raise ValueError()
        if len(data.get('events',[]))>30:raise ValueError()
        for e in data.get('events',[]):
            uuid.UUID(e['event_id'])
            if e['name'] not in CLIENT_EVENTS:raise ValueError()
            if set(e)-{'event_id','name','timestamp','elapsed_ms','duration_sec','value','metric','error_code','page','video_view_id','video_watch_seconds','video_duration_seconds','video_completion_percent'}:raise ValueError()
        if set(data.get('attribution') or {})-set('yclid client_id utm_source utm_medium utm_campaign utm_content utm_term source_token ad_group keyword'.split()):raise ValueError()
        for e in data.get('events',[]):
            for key in ('elapsed_ms','duration_sec','value'):
                if key in e:
                    n=float(e[key])
                    if not math.isfinite(n) or n<0 or n>86400000:raise ValueError()
                    e[key]=n
            for key in ('video_watch_seconds','video_duration_seconds','video_completion_percent'):
                if key in e:
                    n=e[key];limit=100 if key=='video_completion_percent' else 86400
                    if isinstance(n,bool) or not isinstance(n,(float,int)) or not math.isfinite(n) or not 0<=n<=limit:raise ValueError()
            if 'video_view_id' in e:e['video_view_id']=str(uuid.UUID(e['video_view_id']))
            if e.get('metric') not in (None,'LCP','INP','CLS','TTFB'):raise ValueError()
            if e.get('page') not in (None,'home','cart','checkout','order','article','other'):raise ValueError()
            if 'error_code' in e and not re.fullmatch(r'[A-Z_]{1,40}',str(e['error_code'])):raise ValueError()
            datetime.datetime.fromisoformat(str(e['timestamp']).replace('Z','+00:00'))
        attr={}
        for k,v in (data.get('attribution') or {}).items():
            v=str(v)[:200]
            if '@' not in v and not re.search(r'(?:secret|password|bearer|token=|\+?7[0-9]{10})',v,re.I):attr[k]=v
        data['attribution']=attr
        data['referrer_host']=urlsplit('https://'+str(data.get('referrer_host',''))).hostname or ''
        if '@' in data['referrer_host']:data['referrer_host']=''
        if data.get('checkout_session_id'):data['checkout_session_id']=str(uuid.UUID(data['checkout_session_id']))
        data['new_visitor']=bool(data.get('new_visitor'))
        data['client_ua']=headers.get('user-agent','')[:500]
        execute('''DECLARE $id AS Utf8;DECLARE $payload AS Utf8;
            UPSERT INTO analytics_inbox(batch_id,payload,created_at,expire_at)
            VALUES($id,$payload,CurrentUtcTimestamp(),CurrentUtcTimestamp()+Interval("P2D"));''',
            {'$id':bid,'$payload':json.dumps(data,separators=(',',':'))})
        return response(202,{'queued':True})
    except (ValueError,KeyError,TypeError):return response(400,{'ok':False})
    except Exception:return response(202,{'queued':False})

def read(execute):
    rs=execute('SELECT batch_id,payload FROM analytics_inbox ORDER BY created_at LIMIT 50;')
    rows=rs[0].rows if rs else []
    return {'ok':True,'items':[{'batch_id':r.batch_id,'batch':json.loads(r.payload)} for r in rows]}

def ack(data,execute):
    ids=[str(uuid.UUID(v)) for v in data.get('batch_ids',[])[:50]]
    for bid in ids:
        execute('DECLARE $id AS Utf8;DELETE FROM analytics_inbox WHERE batch_id=$id;',{'$id':bid})
    return {'ok':True,'acked':len(ids)}
