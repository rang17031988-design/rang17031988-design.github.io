"""Owner-only reporting consumer. Never creates payments, orders or ad mutations.

All monetary calculations are deterministic. Existing commerce/controller tables
remain authoritative; analytics stores only pseudonymous events and aggregates.
"""
import asyncio
import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import profit_presentation as presentation
import traffic_attribution
import posting_controls
import wordstat_demand
from product_catalog import COGS_UNIT_RUB

MSK = ZoneInfo('Europe/Moscow')
UTC = timezone.utc
COMMANDS = 'today yesterday week funnel organic devices browsers speed ads profit orders returns stock errors status cpa cpa_pause cpa_resume'.split() + posting_controls.COMMANDS
CLIENT_EVENTS = set('VIDEO_CTA_VIEW VIDEO_OPEN VIDEO_PLAY VIDEO_PAUSE VIDEO_25 VIDEO_50 VIDEO_75 VIDEO_COMPLETE VIDEO_CLOSE SITE_SESSION PRODUCT_VIEW BUY_BUTTON_CLICK CART_OPEN CART_QUANTITY_CHANGED CHECKOUT_OPEN CONTACTS_STARTED CONTACTS_COMPLETED PVZ_PICKER_OPEN PVZ_SEARCH PVZ_LOADED PVZ_SELECTED PAYMENT_BUTTON_CLICK SCROLL_25 SCROLL_50 SCROLL_75 SCROLL_90 PRODUCT_GALLERY_INTERACTION REVIEWS_VIEW SESSION_TIMING WEB_VITAL JS_ERROR PVZ_ERROR PVZ_TIMEOUT PAYMENT_ERROR'.split())
STAGES = 'SITE_SESSION PRODUCT_VIEW BUY_BUTTON_CLICK CART_OPEN CHECKOUT_OPEN CONTACTS_COMPLETED PVZ_PICKER_OPEN PVZ_LOADED PVZ_SELECTED PAYMENT_STARTED PAYMENT_SUCCESS ORDER_RECEIVED'.split()
ATTR_KEYS = 'yclid client_id utm_source utm_medium utm_campaign utm_content utm_term source_token ad_group keyword'.split()
ATTR_KEYS += [f'{side}_{field}' for side in ('first','last') for field in ('source','medium','campaign','content','term','yclid','referrer_host')]
LOCK = 715029849


def value(v):
    return json.loads(v) if isinstance(v, str) else v


def window(period, now=None):
    now = (now or datetime.now(UTC)).astimezone(MSK)
    today = now.date()
    if period == 'today':
        start, end = today, today + timedelta(days=1)
    elif period == 'week':
        end, start = today, today - timedelta(days=7)
    elif period == '7d':
        end, start = today, today - timedelta(days=7)
    else:
        end, start = today, today - timedelta(days=1)
    return datetime.combine(start, datetime.min.time(), MSK), datetime.combine(end, datetime.min.time(), MSK)


def ratio(a, b):
    return round(a * 100 / b, 2) if b else None


def distribution(numbers):
    numbers = sorted(float(x) for x in numbers if x is not None and math.isfinite(float(x)))
    n = len(numbers)
    return {'n': n, 'mean': statistics.mean(numbers) if n else None,
            'median': statistics.median(numbers) if n else None,
            'p75': numbers[math.ceil(n * .75)-1] if n >= 20 else None,
            'quality': 'CONFIRMED' if n >= 20 else 'LOW SAMPLE' if n else 'UNKNOWN'}


def channel(a, referrer=''):
    return traffic_attribution.legacy_channel(a, referrer)


def device(ua):
    tablet = bool(re.search(r'iPad|Tablet|Android(?!.*Mobile)', ua, re.I))
    kind = 'TABLET' if tablet else 'MOBILE' if re.search(r'Mobile|iPhone', ua, re.I) else 'DESKTOP'
    system = 'Android' if 'Android' in ua else 'iPadOS' if 'iPad' in ua else 'iOS' if 'iPhone' in ua else 'Windows' if 'Windows' in ua else 'macOS' if 'Macintosh' in ua else 'Linux' if 'Linux' in ua else 'OTHER'
    browser = 'Yandex' if re.search(r'YaBrowser|YaApp', ua) else 'Edge' if 'Edg' in ua else 'Firefox' if 'Firefox' in ua else 'Chrome' if re.search(r'Chrome|CriOS', ua) else 'Safari' if 'Safari' in ua else 'OTHER'
    return kind, system, browser


def safe_client(batch, ua, now=None):
    """Allowlist rejects fake PAID/server lifecycle and all contact/secret fields."""
    now = now or datetime.now(UTC)
    sid = str(uuid.UUID(batch['session_id']))
    checkout = str(uuid.UUID(batch['checkout_session_id'])) if batch.get('checkout_session_id') else None
    a = {}
    for k in ATTR_KEYS:
        v = (batch.get('attribution') or {}).get(k)
        if v is not None:
            v = str(v)[:250]
            if k in ('yclid','client_id','first_yclid','last_yclid') and not re.fullmatch(r'\d{1,100}', v): continue
            if k.endswith('referrer_host') and not re.fullmatch(r'[a-zA-Z0-9.-]{1,120}',v):continue
            if '@' in v or re.search(r'\+7\d{10}|\d{7,12}:[\w-]{25,}',v): continue
            a[k] = v
    referrer = str(batch.get('referrer_host') or '')[:120]
    if not re.fullmatch(r'[a-zA-Z0-9.-]*',referrer): referrer = ''
    if referrer:a['referrer_host']=referrer
    elif a.get('last_referrer_host'):a['referrer_host']=a['last_referrer_host']
    if a:a.update(traffic_attribution.touch_fields(a))
    events = []
    for e in batch.get('events', [])[:30]:
        if e.get('name') not in CLIENT_EVENTS: raise ValueError('non_client_event')
        eid = str(uuid.UUID(e['event_id']))
        at = datetime.fromisoformat(str(e.get('timestamp') or now.isoformat()).replace('Z','+00:00'))
        if not at.tzinfo or abs((now-at).total_seconds()) > 43200: raise ValueError('timestamp')
        p = {}
        for key in ('elapsed_ms','duration_sec','value'):
            raw = e.get(key)
            if isinstance(raw,(float,int)) and not isinstance(raw,bool) and math.isfinite(raw) and 0 <= raw <= 86400000: p[key] = raw
        for key in ('video_watch_seconds','video_duration_seconds','video_completion_percent'):
            raw=e.get(key)
            limit=100 if key=='video_completion_percent' else 86400
            if raw is not None:
                if isinstance(raw,bool) or not isinstance(raw,(float,int)) or not math.isfinite(raw) or not 0<=raw<=limit: raise ValueError('video_metric')
                p[key]=raw
        if e.get('video_view_id') is not None: p['video_view_id']=str(uuid.UUID(e['video_view_id']))
        if e.get('metric') in ('LCP','INP','CLS','TTFB'): p['metric'] = e['metric']
        if e.get('error_code') in ('NETWORK','TIMEOUT','HTTP','JS','RENDER','PAYMENT','OZON','EMAIL'): p['error_code'] = e['error_code']
        # Paths are fixed page categories, never checkout keys or URLs.
        if e.get('page') in ('home','cart','checkout','order','article','other'): p['page'] = e['page']
        events.append((eid,e['name'],at,p))
    from owner_traffic import classify
    traffic_class=classify(batch,os.getenv('PII_INTERNAL_KEY',''))
    return {'traffic_class':traffic_class,'session_id':sid,'checkout_session_id':checkout,'attribution':a,'channel':channel(a,referrer),
            'device':device(ua[:500]),'new_visitor':bool(batch.get('new_visitor')),'events':events,
            'is_internal':traffic_class in ('owner','internal_test'),'is_test':traffic_class=='internal_test'}


def funnel(events, sessions):
    groups = {}
    for s in sessions:
        if s.get('is_test') or s.get('is_internal'):continue
        s = dict(s)
        groups[s['session_id']] = {'session':s,'events':{},'all':[]}
    for e in events:
        e = dict(e)
        if e.get('session_id') not in groups: continue
        g = groups[e['session_id']]
        g['events'].setdefault(e['name'],e['occurred_at'])
        g['all'].append(e)
    counts = {stage:sum(stage in g['events'] for g in groups.values()) for stage in STAGES}
    drops = []
    for start, end in zip(STAGES, STAGES[1:]):
        eligible = [g for g in groups.values() if start in g['events']]
        complete = sum(end in g['events'] and g['events'][end] >= g['events'][start] for g in eligible)
        drops.append({'from':start,'to':end,'start':len(eligible),'end':complete,
                      'conversion_percent':ratio(complete,len(eligible)), 'lost':len(eligible)-complete,
                      'drop_percent':ratio(len(eligible)-complete,len(eligible)),
                      'quality':'LOW SAMPLE' if len(eligible)<20 else 'CONFIRMED'})
    comparisons = [('SITE_SESSION','BUY_BUTTON_CLICK'),('SITE_SESSION','CHECKOUT_OPEN'),
                   ('SITE_SESSION','PVZ_PICKER_OPEN'),('PVZ_PICKER_OPEN','PVZ_SELECTED'),
                   ('PVZ_SELECTED','PAYMENT_STARTED'),('PAYMENT_STARTED','PAYMENT_SUCCESS')]
    timings = {a+'→'+b:distribution((g['events'][b]-g['events'][a]).total_seconds() for g in groups.values()
               if a in g['events'] and b in g['events'] and g['events'][b]>=g['events'][a]) for a,b in comparisons}
    segments = {}
    for g in groups.values():
        s = g['session']
        key = '/'.join([s['device_type'],s['os'],s['browser']])
        seg = segments.setdefault(key,{'sessions':0,'stages':{k:0 for k in STAGES}})
        seg['sessions']+=1
        for stage in STAGES: seg['stages'][stage] += int(stage in g['events'])
    for seg in segments.values():
        seg.update(paid_cr=ratio(seg['stages']['PAYMENT_SUCCESS'],seg['sessions']),
                   quality='LOW SAMPLE' if seg['sessions']<20 else 'CONFIRMED')
    durations = []
    for g in groups.values():
        measured = [value(e['payload']).get('duration_sec') for e in g['all'] if e['name']=='SESSION_TIMING']
        measured = [x for x in measured if x is not None]
        if measured: durations.append(max(measured))
    buckets = {'<3':0,'3–10':0,'10–30':0,'30–60':0,'>60':0}
    for n in durations:
        buckets['<3' if n<3 else '3–10' if n<10 else '10–30' if n<30 else '30–60' if n<=60 else '>60']+=1
    speed = {}
    for name in ('LCP','INP','CLS','TTFB'):
        speed[name] = {}
        for kind in ('MOBILE','DESKTOP','TABLET'):
            readings = []
            for g in groups.values():
                if g['session']['device_type'] != kind: continue
                samples = [value(e['payload']).get('value') for e in g['all']
                           if e['name']=='WEB_VITAL' and value(e['payload']).get('metric') == name]
                if samples: readings.append(samples[-1])
            speed[name][kind] = distribution(readings)
    errors = {}
    performance_conversion={}
    channel_device={}
    visitor_split={k:{'sessions':0,'paid':0,'durations':[]} for k in ('new','returning')}
    for g in groups.values():
        s=g['session'];seg='/'.join([s['device_type'],s['os'],s['browser']])
        cd=channel_device.setdefault(s['channel']+'/'+s['device_type']+'/'+s['os'],{'sessions':0,'paid':0,'checkout':0,'buy':0})
        cd['buy']+=int('BUY_BUTTON_CLICK' in g['events']);cd['sessions']+=1;cd['paid']+=int('PAYMENT_SUCCESS' in g['events']);cd['checkout']+=int('CHECKOUT_OPEN' in g['events'])
        vs=visitor_split['new' if s['new_visitor'] else 'returning'];vs['sessions']+=1;vs['paid']+=int('PAYMENT_SUCCESS' in g['events'])
        ds=[value(e['payload']).get('duration_sec') for e in g['all'] if e['name']=='SESSION_TIMING']
        ds=[x for x in ds if x is not None]
        if ds:vs['durations'].append(max(ds))
        lcp=[value(e['payload']).get('value') for e in g['all'] if e['name']=='WEB_VITAL' and value(e['payload']).get('metric')=='LCP' and value(e['payload']).get('page')=='home']
        if lcp:
            bucket='<3s' if lcp[-1]<3000 else '3–5s' if lcp[-1]<=5000 else '>5s'
            p=performance_conversion.setdefault(bucket,{'sessions':0,'buy':0,'checkout':0,'paid':0})
            p['sessions']+=1
            for label,name in [('buy','BUY_BUTTON_CLICK'),('checkout','CHECKOUT_OPEN'),('paid','PAYMENT_SUCCESS')]:p[label]+=name in g['events']
        for e in g['all']:
            if e['name'] in ('JS_ERROR','PVZ_ERROR','PVZ_TIMEOUT','PAYMENT_ERROR'):
                k=seg+'/'+e['name'];errors[k]=errors.get(k,0)+1
    for v in list(performance_conversion.values())+list(channel_device.values()):
        v.update(paid_cr=ratio(v['paid'],v['sessions']),quality='LOW SAMPLE' if v['sessions']<20 else 'CONFIRMED')
    for v in visitor_split.values():
        v.update(paid_cr=ratio(v['paid'],v['sessions']),duration=distribution(v.pop('durations')))
    biggest = max((x for x in drops if x['start']>=20),key=lambda x:x['lost'],default=None)
    return {'video':video_summary(groups),'sessions':len(groups),'counts':counts,'drops':drops,'biggest_drop':biggest,
            'time_to_action':timings,'duration':distribution(durations),'duration_buckets':buckets,
            'early_exit':{f'<{n}s':{'count':sum(x<n for x in durations),'percent':ratio(sum(x<n for x in durations),len(durations))} for n in (3,5,10)},
            'segments':segments,'speed':speed,'errors':errors,
            'channel_device':channel_device,'performance_conversion':performance_conversion,'visitor_split':visitor_split,
            'scroll':{str(n):sum('SCROLL_'+str(n) in g['events'] for g in groups.values()) for n in (25,50,75,90)},
            'new_returning':{k:sum(g['session']['new_visitor']==v for g in groups.values()) for k,v in [('new',True),('returning',False)]},
            'quality':'LOW SAMPLE' if len(groups)<20 else 'CONFIRMED'}


def video_summary(groups):
    """Session cohorts and real playback snapshots; no implied sales causality."""
    counts={n:0 for n in ('cta_view','open','play','viewers_25','viewers_50','viewers_75','complete')}
    snapshots=[];orders=[]
    cohorts={k:{'sessions':0,'buy':0,'checkout':0,'paid':0} for k in ('viewers','non_viewers')}
    after={'buy':0,'checkout':0,'paid':0}
    names={'cta_view':'VIDEO_CTA_VIEW','open':'VIDEO_OPEN','play':'VIDEO_PLAY','viewers_25':'VIDEO_25','viewers_50':'VIDEO_50','viewers_75':'VIDEO_75','complete':'VIDEO_COMPLETE'}
    for sid,g in groups.items():
        es=g['all'];seen={e['name'] for e in es}
        for k,n in names.items():counts[k]+=n in seen
        played=[e['occurred_at'] for e in es if e['name']=='VIDEO_PLAY']
        cohort=cohorts['viewers' if played else 'non_viewers'];cohort['sessions']+=1
        for label,n in [('buy','BUY_BUTTON_CLICK'),('checkout','CHECKOUT_OPEN'),('paid','PAYMENT_SUCCESS')]:
            reached=any(e['name']==n for e in es);cohort[label]+=reached
            if played:after[label]+=any(e['name']==n and e['occurred_at']>=min(played) for e in es)
        views={}
        for e in es:
            if not e['name'].startswith('VIDEO_'):continue
            p=value(e['payload']);vid=p.get('video_view_id')
            if not vid or 'video_watch_seconds' not in p:continue
            v=views.setdefault(vid,{'watch':0,'percent':0,'duration':0})
            v['watch']=max(v['watch'],p['video_watch_seconds']);v['percent']=max(v['percent'],p.get('video_completion_percent',0));v['duration']=max(v['duration'],p.get('video_duration_seconds',0))
        snapshots.extend(v for v in views.values() if v['watch']>0)
        if played:
            ids=sorted({str(e['order_id']) for e in es if e.get('order_id')})
            if ids:orders.append({'session_id':sid,'order_ids':ids})
    for c in cohorts.values():
        for name in ('buy','checkout','paid'):c[name+'_cr']=ratio(c[name],c['sessions'])
        c['quality']='CONFIRMED' if c['sessions']>=20 else 'LOW SAMPLE'
    return {**counts,'watch_seconds':distribution(v['watch'] for v in snapshots),
            'completion_percent':distribution(v['percent'] for v in snapshots),
            'after_video':after,'cohorts':cohorts,'order_links':orders,
            'basis':'Pseudonymous sessions, not distinct people; association only, not causality',
            'quality':'CONFIRMED' if counts['play']>=20 else 'LOW SAMPLE'}

def money(rows, ad_spend, dispositions):
    """Never interprets returned condition, combined costs or no attribution as zero."""
    cogs = Decimal(COGS_UNIT_RUB)
    order_ids={r['order_id'] for r in rows}
    dispositions={k:v for k,v in dispositions.items() if k in order_ids}
    stats={k:{'orders':0,'units':0,'rub':Decimal(0)} for k in ('ORDERED','PAID','IN_TRANSIT','READY','RECEIVED','CANCELLED','NOT_PICKED_UP','RETURNED_RESELLABLE','RETURNED_DAMAGED')}
    received=[]
    outbound=[];returned=[];fees=[];extra=[]
    for r in rows:
        d=dispositions.get(r['order_id'],{})
        labels=['ORDERED']
        if r['payment_status']=='succeeded':labels.append('PAID');fees.append(r.get('yookassa'));outbound.append(r.get('ozon'))
        status=r.get('ozon_status')
        if status in ('on_way','in_courier_service','acceptance_in_progress'):labels.append('IN_TRANSIT')
        if status=='in_delivery_point':labels.append('READY')
        if status=='delivered':labels.append('RECEIVED');received.append(r)
        if status=='canceled':labels.append('CANCELLED')
        if d.get('not_picked_up') is True:labels.append('NOT_PICKED_UP')
        if d.get('condition') in ('resellable','damaged'):labels.append('RETURNED_'+d['condition'].upper())
        for k in labels:
            stats[k]['orders']+=1;stats[k]['units']+=r['quantity'];stats[k]['rub']+=Decimal(str(r['amount']))
        if r['payment_status']=='succeeded':
            returned.append(d.get('return_logistics') if d.get('has_return') else Decimal(0) if r.get('returns_other') == 0 else None)
            extra.append(d.get('extra_cost',Decimal(0)))
    total=lambda vs:float(sum(Decimal(str(v)) for v in vs)) if all(v is not None for v in vs) else None
    revenue=stats['RECEIVED']['rub']
    classified_received_units=sum(r['quantity'] for r in received if dispositions.get(r['order_id'],{}).get('condition') in ('resellable','damaged'))
    cogs_received=(stats['RECEIVED']['units']-classified_received_units)*cogs
    refunds=[d.get('refund_rub') for d in dispositions.values() if d.get('has_return')]
    refund_total=total(refunds)
    if refund_total is not None:revenue-=Decimal(str(refund_total))
    tax=max(Decimal(0),revenue)*Decimal('.06')
    writeoff=stats['RETURNED_DAMAGED']['units']*cogs
    rf=total(fees);ro=total(outbound)
    rr=total(returned);extras=total(extra)
    before=revenue-cogs_received-tax-writeoff-Decimal(str(ad_spend))-Decimal(str(rf or 0))-Decimal(str(ro or 0))-Decimal(str(rr or 0))-Decimal(str(extras or 0))
    final=float(before) if all(v is not None for v in (rf,ro,rr,extras,total(outbound),refund_total)) else None
    denominator=stats['RECEIVED']['orders']
    not_picked=[]
    for r in rows:
        d=dispositions.get(r['order_id'],{})
        if not d.get('not_picked_up'):continue
        costs=[r.get('yookassa'),r.get('ozon'),d.get('return_logistics'),d.get('extra_cost',0)]
        known_costs=total(costs)
        costs.append(d.get('attributed_ad_cost'))
        loss=total(costs)
        if d.get('condition') not in ('resellable','damaged'):loss=None
        if loss is not None and d.get('condition')=='damaged':loss+=r['quantity']*230
        not_picked.append({'order_id':r['order_id'],'loss_rub':loss,'known_non_ad_costs_rub':known_costs,'attributed_ad_cost':d.get('attributed_ad_cost'),'condition':d.get('condition') or 'UNKNOWN'})
    return {'statuses':{k:{**v,'rub':float(v['rub'])} for k,v in stats.items()},'cogs_unit':COGS_UNIT_RUB,
            'cogs_received':float(cogs_received),'tax_received':float(tax),'writeoff':float(writeoff),
            'yookassa_actual':total(fees),'ozon_outbound_actual':total(outbound),'ozon_return_actual':rr,
            'extra_confirmed':extras,'advertising':float(ad_spend),'profit_before_unknown':float(before),
            'refunds_actual':refund_total,'not_picked_up_loss':not_picked,
            'final_net_profit':final,'profit_per_received_order':final/denominator if final is not None and denominator else None,
            'quality':'PROVISIONAL' if final is None else 'CONFIRMED',
            'cac':{k:float(ad_spend)/stats[k]['orders'] if stats[k]['orders'] else None for k in ('ORDERED','PAID','RECEIVED')},
            'roas_paid':float(stats['PAID']['rub'])/float(ad_spend) if ad_spend else None,
            'romi':final/float(ad_spend)*100 if final is not None and ad_spend else None,
            'ad_allocation_basis':'period total; cohort CAC/ROAS are blended, NOT verified attributed sales',
            'return_condition_unknown':sum(d.get('has_return',False) and not d.get('condition') for d in dispositions.values())}


class ProfitFunnel:
    def __init__(self, pool, controller, transport=None):
        self.pool,self.controller=pool,controller
        self.http=httpx.AsyncClient(timeout=25)
        self.status={'state':'starting','last_error':None}
        self.ready=False
        self.transport=transport

    async def schema(self,c):
        if self.ready:return
        await c.execute('''
        SELECT pg_advisory_xact_lock(714566816);
        CREATE TABLE IF NOT EXISTS profit_funnel_sessions (
            session_id TEXT PRIMARY KEY,checkout_session_id TEXT,device_type TEXT NOT NULL,
            os TEXT NOT NULL,browser TEXT NOT NULL,channel TEXT NOT NULL,new_visitor BOOLEAN NOT NULL,
            attribution JSONB NOT NULL,started_at TIMESTAMPTZ NOT NULL,last_seen TIMESTAMPTZ NOT NULL);
        CREATE INDEX IF NOT EXISTS profit_funnel_checkout_idx ON profit_funnel_sessions(checkout_session_id);
        CREATE INDEX IF NOT EXISTS profit_funnel_session_time ON profit_funnel_sessions(started_at);
        CREATE TABLE IF NOT EXISTS profit_funnel_events (
            event_id TEXT PRIMARY KEY,session_id TEXT,name TEXT NOT NULL,occurred_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}',origin TEXT NOT NULL,order_id BIGINT,payment_id TEXT,shipment_id TEXT,
            observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
        CREATE INDEX IF NOT EXISTS profit_funnel_event_time ON profit_funnel_events(occurred_at,name);
        CREATE TABLE IF NOT EXISTS profit_funnel_state(key TEXT PRIMARY KEY,payload JSONB NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
        CREATE TABLE IF NOT EXISTS profit_funnel_reports (
            period TEXT NOT NULL,start_at TIMESTAMPTZ NOT NULL,end_at TIMESTAMPTZ NOT NULL,payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(period,start_at));
        CREATE TABLE IF NOT EXISTS profit_funnel_outbox (
            key TEXT PRIMARY KEY,state TEXT NOT NULL,telegram_message_id BIGINT,error_code TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
        CREATE TABLE IF NOT EXISTS profit_funnel_alerts (
            key TEXT PRIMARY KEY,severity TEXT NOT NULL,first_seen TIMESTAMPTZ NOT NULL,last_seen TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL,last_sent TIMESTAMPTZ);
        CREATE TABLE IF NOT EXISTS profit_funnel_return_dispositions (
            order_id BIGINT PRIMARY KEY,condition TEXT CHECK(condition IN ('resellable','damaged')),
            not_picked_up BOOLEAN,return_logistics NUMERIC,extra_cost NUMERIC NOT NULL DEFAULT 0,
            confirmed_at TIMESTAMPTZ,source TEXT NOT NULL);
        ALTER TABLE profit_funnel_return_dispositions ADD COLUMN IF NOT EXISTS refund_rub NUMERIC;
        ALTER TABLE profit_funnel_sessions ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE profit_funnel_sessions ADD COLUMN IF NOT EXISTS is_internal BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE profit_funnel_sessions ADD COLUMN IF NOT EXISTS traffic_class TEXT NOT NULL DEFAULT 'customer';
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='commerce_pending_orders' AND column_name='is_internal') THEN
            UPDATE profit_funnel_sessions s
            SET is_test=s.is_test OR p.is_test,is_internal=TRUE,traffic_class=CASE WHEN s.is_test OR p.is_test THEN 'internal_test' ELSE 'owner' END
            FROM commerce_pending_orders p
            WHERE s.checkout_session_id=p.session_id AND (p.is_test OR p.is_internal)
              AND (NOT s.is_internal OR (p.is_test AND NOT s.is_test) OR s.traffic_class NOT IN ('owner','internal_test'));
          END IF;
        END $$;

        ''')
        # Preserve every old unidentified visit as UNKNOWN; never infer ownership.
        if not await c.fetchval("SELECT EXISTS(SELECT 1 FROM profit_funnel_state WHERE key='owner_classification_v1')"):
            await c.execute("UPDATE profit_funnel_sessions SET traffic_class='unknown' WHERE NOT is_test AND NOT is_internal AND traffic_class='customer'")
            await c.execute("UPDATE profit_funnel_sessions SET traffic_class='internal_test' WHERE (is_test OR is_internal) AND traffic_class IN ('test','internal','owner_test')")
            await self.put(c,'owner_classification_v1',{'migrated':True,'unknown_history_preserved':True})
        self.ready=True

    async def ingest(self,batch,ua):
        clean=safe_client(batch,ua)
        async with self.pool.acquire() as c:
            await self.schema(c)
            async with c.transaction():
                await c.execute('SELECT pg_advisory_xact_lock(hashtext($1))',clean['session_id'])
                count=await c.fetchval("SELECT count(*) FROM profit_funnel_events WHERE session_id=$1 AND observed_at>NOW()-INTERVAL '1 minute'",clean['session_id'])
                if count>100:return {'accepted':0,'limited':True}
                now=datetime.now(UTC);kind,system,browser=clean['device']
                await c.execute('''INSERT INTO profit_funnel_sessions
                    (session_id,checkout_session_id,device_type,os,browser,channel,new_visitor,attribution,started_at,last_seen,is_test,is_internal,traffic_class)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$9,$10,$11,$12)
                    ON CONFLICT(session_id) DO UPDATE SET last_seen=GREATEST(profit_funnel_sessions.last_seen,EXCLUDED.last_seen),
                    checkout_session_id=coalesce(EXCLUDED.checkout_session_id,profit_funnel_sessions.checkout_session_id),
                    attribution=(profit_funnel_sessions.attribution||EXCLUDED.attribution)||jsonb_build_object(
                        'first_source',COALESCE(NULLIF(profit_funnel_sessions.attribution->'first_source','null'::jsonb),EXCLUDED.attribution->'first_source'),
                        'first_medium',COALESCE(NULLIF(profit_funnel_sessions.attribution->'first_medium','null'::jsonb),EXCLUDED.attribution->'first_medium'),
                        'first_campaign',COALESCE(NULLIF(profit_funnel_sessions.attribution->'first_campaign','null'::jsonb),EXCLUDED.attribution->'first_campaign'),
                        'first_content',COALESCE(NULLIF(profit_funnel_sessions.attribution->'first_content','null'::jsonb),EXCLUDED.attribution->'first_content'),
                        'first_term',COALESCE(NULLIF(profit_funnel_sessions.attribution->'first_term','null'::jsonb),EXCLUDED.attribution->'first_term'),
                        'first_yclid',COALESCE(NULLIF(profit_funnel_sessions.attribution->'first_yclid','null'::jsonb),EXCLUDED.attribution->'first_yclid'),
                        'first_referrer_host',COALESCE(NULLIF(profit_funnel_sessions.attribution->'first_referrer_host','null'::jsonb),EXCLUDED.attribution->'first_referrer_host')),
                    is_test=profit_funnel_sessions.is_test OR EXCLUDED.is_test,
                    is_internal=profit_funnel_sessions.is_internal OR EXCLUDED.is_internal,
                    traffic_class=CASE WHEN profit_funnel_sessions.is_test OR EXCLUDED.is_test THEN 'internal_test' WHEN EXCLUDED.is_internal THEN EXCLUDED.traffic_class ELSE profit_funnel_sessions.traffic_class END''',
                    clean['session_id'],clean['checkout_session_id'],kind,system,browser,clean['channel'],clean['new_visitor'],json.dumps(clean['attribution']),now,
                    clean['is_test'],clean['is_internal'],clean['traffic_class'])
                accepted=0
                for eid,name,at,p in clean['events']:
                    result=await c.fetchval('''INSERT INTO profit_funnel_events(event_id,session_id,name,occurred_at,payload,origin)
                        VALUES($1,$2,$3,$4,$5::jsonb,'client') ON CONFLICT DO NOTHING RETURNING event_id''',eid,clean['session_id'],name,at,json.dumps(p))
                    accepted+=bool(result)
        return {'accepted':accepted}

    async def state(self,c,key):
        data=await c.fetchval('SELECT payload FROM profit_funnel_state WHERE key=$1',key)
        return value(data) if data else None

    async def put(self,c,key,payload):
        await c.execute('''INSERT INTO profit_funnel_state(key,payload) VALUES($1,$2::jsonb)
            ON CONFLICT(key) DO UPDATE SET payload=EXCLUDED.payload,updated_at=NOW()''',key,json.dumps(payload,default=str))

    async def observe(self,c):
        """Existing verified commerce is authoritative; unknown transition times are labeled."""
        rows=await c.fetch('''SELECT p.order_id,p.session_id,p.payment_id,p.payment_status,p.shipment_id,p.ozon_status,
                p.created_at,p.updated_at,(y.status='succeeded') AS verified_paid FROM commerce_pending_orders p
                LEFT JOIN insales_yookassa_payments y ON y.payment_id=p.payment_id WHERE p.order_id IS NOT NULL''')
        initialized=bool(await self.state(c,'lifecycle_initialized'))
        for r in rows:
            sid=await c.fetchval('SELECT session_id FROM profit_funnel_sessions WHERE checkout_session_id=$1 ORDER BY last_seen DESC LIMIT 1',r['session_id'])
            names=[]
            if r['payment_id']:names.append('PAYMENT_STARTED')
            if r['payment_status']=='succeeded' and r['verified_paid']:names += ['PAYMENT_SUCCESS','ORDER_PAID']
            if r['payment_status']=='canceled':names += ['PAYMENT_FAILED']
            if r['shipment_id']:names += ['OZON_SHIPMENT_CREATED']
            if r['ozon_status'] in ('on_way','in_courier_service'):names += ['OZON_IN_TRANSIT']
            if r['ozon_status']=='in_delivery_point':names += ['OZON_READY_FOR_PICKUP']
            if r['ozon_status']=='delivered':names += ['ORDER_RECEIVED']
            if r['ozon_status']=='canceled':names += ['ORDER_CANCELLED']
            for name in names:
                payload={'timestamp_basis':'first_observed' if initialized else 'historical_state_backfill; transition time unavailable'}
                # Do not bind historical backfill to today's sessions or invent timings.
                await c.execute('''INSERT INTO profit_funnel_events(event_id,session_id,name,occurred_at,payload,origin,order_id,payment_id,shipment_id)
                    VALUES($1,$2,$3,$4,$5::jsonb,'verified_commerce',$6,$7,$8) ON CONFLICT DO NOTHING''',
                    'order:'+str(r['order_id'])+':'+name,sid if initialized else None,name,
                    datetime.now(UTC) if initialized else r['updated_at'],json.dumps(payload),r['order_id'],r['payment_id'],r['shipment_id'])
        await self.put(c,'lifecycle_initialized',{'ok':True})
        inspections=await c.fetch('SELECT order_id,condition,not_picked_up,confirmed_at FROM profit_funnel_return_dispositions WHERE confirmed_at IS NOT NULL')
        for d in inspections:
            names=['NOT_PICKED_UP'] if d['not_picked_up'] else []
            if d['condition']:names.append('RETURNED_'+d['condition'].upper())
            for name in names:
                await c.execute('''INSERT INTO profit_funnel_events(event_id,name,occurred_at,payload,origin,order_id)
                    VALUES($1,$2,$3,'{"timestamp_basis":"owner_physical_inspection"}','verified_inspection',$4)
                    ON CONFLICT DO NOTHING''','order:'+str(d['order_id'])+':'+name,name,d['confirmed_at'],d['order_id'])
        returns=await c.fetch("SELECT DISTINCT order_id FROM profit_controller_returns WHERE status='received'")
        for row in returns:
            await c.execute('''INSERT INTO profit_funnel_events(event_id,name,occurred_at,payload,origin,order_id)
                VALUES($1,'ORDER_RETURNED',NOW(),'{"timestamp_basis":"first_observed"}','verified_ozon',$2)
                ON CONFLICT DO NOTHING''','order:'+str(row['order_id'])+':ORDER_RETURNED',row['order_id'])

    async def drain(self):
        if not self.transport:return
        data=await self.transport('analytics_read')
        acknowledged=[]
        for item in data.get('items',[]):
            try:
                batch=item['batch']
                await self.ingest(batch,batch.pop('client_ua',''))
                acknowledged.append(item['batch_id'])
            except (ValueError,KeyError,TypeError):
                acknowledged.append(item['batch_id'])
        if acknowledged:await self.transport('analytics_ack',{'batch_ids':acknowledged})

    async def ads(self,start,end):
        fields=['CampaignId','Impressions','Clicks','Cost']
        spec={'SelectionCriteria':{'DateFrom':start.date().isoformat(),'DateTo':(end-timedelta(days=1)).date().isoformat(),
                  'Filter':[{'Field':'CampaignId','Operator':'IN','Values':['714566814','715029848']}]},
              'FieldNames':fields,'ReportName':f'profit-funnel-{start.date()}-{end.date()}',
              'ReportType':'CUSTOM_REPORT','DateRangeType':'CUSTOM_DATE','Format':'TSV','IncludeVAT':'YES','IncludeDiscount':'YES'}
        headers={**self.controller.headers(),'processingMode':'auto','returnMoneyInMicros':'false','skipReportHeader':'true','skipReportSummary':'true'}
        for attempt in range(3):
            r=await self.http.post('https://api.direct.yandex.com/json/v501/reports',headers=headers,json={'params':spec})
            if r.status_code not in (201,202):break
            if attempt<2:await asyncio.sleep(min(15,max(2,int(r.headers.get('retryIn','3')))))
        if r.status_code!=200:return {'quality':'UNKNOWN','reason':'report_pending' if r.status_code in (201,202) else 'provider_http_'+str(r.status_code)}
        reader=csv.DictReader(io.StringIO(r.text),delimiter='\t')
        if not reader.fieldnames or not set(fields).issubset(reader.fieldnames):return {'quality':'UNKNOWN','reason':'columns'}
        result={name:{'impressions':0,'clicks':0,'spend':0.0,'quality':'CONFIRMED'} for name in ('YANDEX_SEARCH','YANDEX_RSYA')}
        for row in reader:
            name={'714566814':'YANDEX_SEARCH','715029848':'YANDEX_RSYA'}.get(row['CampaignId'])
            if not name:continue
            result[name]['impressions']+=int(row['Impressions']);result[name]['clicks']+=int(row['Clicks']);result[name]['spend']+=float(Decimal(row['Cost']))
        for v in result.values():v.update(ctr=ratio(v['clicks'],v['impressions']),cpc=v['spend']/v['clicks'] if v['clicks'] else None)
        return {'quality':'CONFIRMED','channels':result,'cost_basis':'includes VAT; actual provider report'}

    async def metrika(self,start,end):
        token=os.getenv('METRIKA_OAUTH_TOKEN','')
        if not token:return {'quality':'UNKNOWN','reason':'missing_token'}
        base={'ids':112544007,'date1':start.date().isoformat(),'date2':(end-timedelta(days=1)).date().isoformat(),
              'accuracy':'full','limit':1000,'metrics':'ym:s:visits,ym:s:avgVisitDurationSeconds,ym:s:goal612630973visits,ym:s:goal667045201visits,ym:s:goal666936854visits'}
        results={}
        for name,dimensions in [('devices','ym:s:deviceCategory,ym:s:operatingSystemRootName,ym:s:browserName'),('new_returning','ym:s:isNewUser'),('channels','ym:s:lastSignTrafficSource')]:
            try:
                r=await self.http.get('https://api-metrika.yandex.net/stat/v1/data',headers={'Authorization':'OAuth '+token},params={**base,'dimensions':dimensions})
                if r.status_code!=200:results[name]={'quality':'UNKNOWN','http':r.status_code};continue
                data=r.json();results[name]={'quality':'ESTIMATED' if data.get('sampled') else 'CONFIRMED','rows':data.get('data',[]),'totals':data.get('totals'), 'source':'existing Metrika goals; never add a second browser paid goal'}
            except Exception as exc:results[name]={'quality':'UNKNOWN','reason':type(exc).__name__}
        return results

    async def report(self,period='yesterday',now=None):
        now=now or datetime.now(UTC);start,end=window(period,now)
        # Provider date2 includes today for a live snapshot, clearly marked incomplete.
        async with self.pool.acquire() as c:
            await self.schema(c)
            events=[dict(x) for x in await c.fetch('SELECT * FROM profit_funnel_events WHERE occurred_at >= $1 AND occurred_at < $2 ORDER BY occurred_at',start,end)]
            sessions=[dict(x) for x in await c.fetch('SELECT * FROM profit_funnel_sessions WHERE started_at >= $1 AND started_at < $2',start,end)]
            # A session can continue past midnight. Include sessions with real
            # events inside the requested window, retaining original attribution.
            sessions=[dict(x) for x in await c.fetch('''SELECT * FROM profit_funnel_sessions s
                WHERE (started_at >= $1 AND started_at < $2) OR EXISTS (
                    SELECT 1 FROM profit_funnel_events e WHERE e.session_id=s.session_id
                    AND e.occurred_at >= $1 AND e.occurred_at < $2)''',start,end)]
            rows=await self.controller.order_cohort(c,start,end)
            dispositions={x['order_id']:dict(x) for x in await c.fetch('SELECT * FROM profit_funnel_return_dispositions')}
            returns=await c.fetch('SELECT order_id,return_type,status FROM profit_controller_returns')
            for ret in returns:
                dispositions.setdefault(ret['order_id'],{})['has_return']=True
            stock=await self.controller.stock(c,now)
            cpa=await self.controller.state(c,'cpa_monitor') or {}
            demand=wordstat_demand.report_window(await self.controller.state(c,'wordstat_demand') or {},start,end)
            # Physical inspection alone may mark a returned item resellable/damaged.
            stock['confirmed_resellable_return_units']=sum(r['quantity'] for r in rows if dispositions.get(r['order_id'],{}).get('condition')=='resellable')
            # Native stock already includes its real stock events; never deduct PAID or add returns twice.
            actions=[dict(x) for x in await c.fetch('SELECT action,state,count(*) AS count FROM profit_controller_actions WHERE created_at >= $1 AND created_at < $2 GROUP BY action,state',start,end)]
            failures=[dict(x) for x in await c.fetch("SELECT kind,state,count(*) AS count FROM commerce_service_messages WHERE order_id>0 AND updated_at >= $1 AND updated_at < $2 GROUP BY kind,state",start,end)]
            shipment_failures=await c.fetchval('SELECT count(*) FROM commerce_pending_orders WHERE payment_status=\'succeeded\' AND post_purchase_failures>=3')
            verification_errors=[dict(x) for x in await c.fetch('SELECT error_code,count(*) AS count FROM insales_yookassa_payments WHERE error_code IS NOT NULL AND updated_at >= $1 AND updated_at < $2 GROUP BY error_code',start,end)]
            status_sync_errors=await c.fetchval("SELECT count(*) FROM commerce_pending_orders WHERE shipment_id IS NOT NULL AND status_checked_at<NOW()-INTERVAL '10 minutes'")
            delivery_counts=[dict(x) for x in await c.fetch("SELECT ozon_status,count(*) AS count FROM commerce_pending_orders WHERE payment_status='succeeded' GROUP BY ozon_status")]
            email_counts=[dict(x) for x in await c.fetch("SELECT kind,state,delivery_state,count(*) AS count FROM commerce_service_messages WHERE order_id>0 AND kind IN ('paid_email','ready_email') GROUP BY kind,state,delivery_state")]
            from customer_messages import production_email_allowed
            communication=[]
            communication_rows=await c.fetch('''SELECT p.order_number,p.created_at,p.ozon_status,
                kind.value AS kind,m.state,m.delivery_state,m.created_at AS queued_at FROM commerce_pending_orders p
                JOIN insales_yookassa_payments y ON y.payment_id=p.payment_id
                CROSS JOIN (VALUES ('paid_email'),('ready_email')) AS kind(value)
                LEFT JOIN commerce_service_messages m ON m.order_id=p.order_id AND m.kind=kind.value
                WHERE p.payment_status='succeeded' AND y.status='succeeded'
                AND (kind.value='paid_email' OR p.ozon_status IN ('in_delivery_point','ready_for_pickup'))''')
            for message in communication_rows:
                if not production_email_allowed(message):continue
                if message['state']=='sent' and message['delivery_state'] not in ('bounced','failed'):continue
                if now-(message['queued_at'] or message['created_at'])>=timedelta(minutes=10):
                    communication.append({'order_number':message['order_number'],
                        'kind':message['kind'] or ('ready_email' if message['ozon_status'] in ('in_delivery_point','ready_for_pickup') else 'paid_email'),
                        'state':message['state'] or 'not_queued','delivery_state':message['delivery_state']})
        ads,metric=await asyncio.gather(self.ads(start,end),self.metrika(start,end))
        spend=sum(v['spend'] for v in ads.get('channels',{}).values())
        economics=money(rows,spend,dispositions)
        if ads.get('quality')=='UNKNOWN':economics.update(final_net_profit=None,advertising=None,quality='PROVISIONAL',profit_before_unknown_basis='advertising also UNKNOWN')
        f=funnel(events,sessions)
        slices={}
        for label in ('PAID','FREE'):
            selected=[]
            for s in sessions:
                attr=value(s.get('attribution')) or {}
                source=traffic_attribution.classify(attr,attr.get('referrer_host',''))
                if (source['paid_evidence'] if label=='PAID' else not source['paid_evidence'] and source['primary_attribution']!='UNKNOWN'):
                    selected.append(s)
            slices[label]=funnel(events,selected)
        channels={}
        for s in sessions:
            if s.get('is_test') or s.get('is_internal'):continue
            entry=channels.setdefault(s['channel'],{'sessions':0,'paid':0,'orders':0})
            entry['sessions']+=1
        for r in rows:
            attr=value(r.get('attribution')) or {}
            if not any(attr.values()):name='UNATTRIBUTED'
            else:name=channel(attr)
            entry=channels.setdefault(name,{'sessions':0,'paid':0,'orders':0});entry['orders']+=1
            entry['paid']+=r['payment_status']=='succeeded'
        for name,e in channels.items():
            costs=ads.get('channels',{}).get(name,{}).get('spend')
            e.update(paid_cr=ratio(e['paid'],e['sessions']),cac=costs/e['paid'] if costs is not None and e['paid'] else None,
                     quality='LOW SAMPLE' if e['sessions']<20 else 'CONFIRMED')
        result={'period':period,'start_msk':start.isoformat(),'end_msk_exclusive':end.isoformat(),
                'day_not_finished':period=='today','generated_at':now.isoformat(),'ads':ads,'metrika':metric,
                'instrumented_funnel':f,'economics':economics,'channel_attribution':channels,
                'source_attribution':traffic_attribution.source_report(sessions,rows),
                'funnel_slices':slices, 'wordstat_demand':demand,
                'stock':stock,'controller_actions':actions,'technical':{'service_messages':failures,'shipment_failures':shipment_failures,
                    'verification_errors':verification_errors,'status_sync_stale':status_sync_errors,
                    'customer_communication_warnings':communication},
                'operational':{'delivery':delivery_counts,'email':email_counts},'cpa_controller':cpa,
                'traffic_exclusions':{'owner':sum(s.get('traffic_class')=='owner' for s in sessions),'internal_test':sum(s.get('traffic_class')=='internal_test' for s in sessions),'unknown':sum(s.get('traffic_class')=='unknown' for s in sessions)},
                'collection_started':'profit_funnel instrumentation release; earlier missing events UNKNOWN, never zero',
                'estimated_lost_revenue':None,'estimated_lost_revenue_reason':'requires sufficient comparable baseline'}
        async with self.pool.acquire() as c:
            baseline=await c.fetch('''SELECT payload FROM profit_funnel_reports WHERE period='yesterday'
                AND start_at >= $1 AND start_at < $2 ORDER BY start_at''',start-timedelta(days=7),start)
            result['comparison']=self.compare(result,[value(r['payload']) for r in baseline])
            await c.execute('''INSERT INTO profit_funnel_reports(period,start_at,end_at,payload) VALUES($1,$2,$3,$4::jsonb)
                ON CONFLICT(period,start_at) DO UPDATE SET payload=EXCLUDED.payload,created_at=NOW()''',period,start,end,json.dumps(result,default=str))
        return result

    def compare(self,current,baseline):
        eligible=[r for r in baseline if r['instrumented_funnel']['sessions']>=20]
        result={'days':len(baseline),'eligible_days':len(eligible),'quality':'LOW SAMPLE','yesterday':baseline[-1].get('instrumented_funnel',{}).get('counts') if baseline else None}
        if len(eligible)<3:return result
        result['quality']='CONFIRMED'
        result['seven_day_average']={k:statistics.mean(r['instrumented_funnel']['counts'][k] for r in eligible) for k in STAGES}
        result['seven_day_duration_mean']=statistics.mean(r['instrumented_funnel']['duration']['mean'] for r in eligible if r['instrumented_funnel']['duration']['mean'] is not None) if any(r['instrumented_funnel']['duration']['mean'] is not None for r in eligible) else None
        result['seven_day_ads']={channel:{metric:statistics.mean(r['ads'].get('channels',{}).get(channel,{}).get(metric,0) for r in eligible if r['ads'].get('quality')=='CONFIRMED') if any(r['ads'].get('quality')=='CONFIRMED' for r in eligible) else None for metric in ('impressions','clicks','spend')} for channel in ('YANDEX_SEARCH','YANDEX_RSYA')}
        result['drop_comparison']=[]
        for current_drop in current['instrumented_funnel']['drops']:
            old=[next(x for x in r['instrumented_funnel']['drops'] if x['from']==current_drop['from']) for r in eligible]
            starts=sum(x['start'] for x in old);lost=sum(x['lost'] for x in old)
            baseline_percent=ratio(lost,starts)
            result['drop_comparison'].append({'from':current_drop['from'],'to':current_drop['to'],'seven_day_percent':baseline_percent,
                'difference_percentage_points':current_drop['drop_percent']-baseline_percent if current_drop['drop_percent'] is not None and baseline_percent is not None else None})
        first='PVZ_PICKER_OPEN';last='PAYMENT_SUCCESS'
        denominators=sum(r['instrumented_funnel']['counts'][first] for r in eligible)
        successes=sum(r['instrumented_funnel']['counts'][last] for r in eligible)
        f=current['instrumented_funnel'];n=f['counts'][first]
        if denominators>=100 and n>=20:
            missed=max(0,n*successes/denominators-f['counts'][last])
            current['estimated_lost_revenue']={'rub':round(missed*current['stock']['unit_price'],2),'basis':'Оценка, не фактический убыток','baseline_sessions':denominators}
        return result

    def text(self,r,command='today'):
        return presentation.render(r,command,self.status)

    def owner_allowed(self,update):
        query=update.get('callback_query') or {}
        m=update.get('message') or query.get('message') or {};chat=m.get('chat') or {};sender=query.get('from') or m.get('from') or {}
        owner=os.getenv('PROFIT_FUNNEL_OWNER_USER_ID') or os.getenv('OWNER_CHAT_ID','')
        destination=os.getenv('PROFIT_FUNNEL_OWNER_CHAT_ID') or os.getenv('OWNER_CHAT_ID','')
        return chat.get('type')=='private' and str(chat.get('id'))==destination and str(sender.get('id'))==owner and not sender.get('is_bot')

    async def telegram(self,method,payload):
        token=os.getenv('PROFIT_FUNNEL_BOT_TOKEN','')
        if not token:return {'ok':False,'error_code':'not_configured'}
        try:
            r=await self.http.post('https://api.telegram.org/bot'+token+'/'+method,json=payload)
            data=r.json()
            if method=='editMessageText' and r.status_code==400 and 'message is not modified' in str(data.get('description','')).lower():return {'ok':True}
            return data if r.status_code==200 else {'ok':False,'error_code':str(r.status_code)}
        except Exception as exc:
            # Never log exception repr: HTTP exceptions contain credential-bearing URLs.
            return {'ok':False,'error_code':type(exc).__name__,'delivery_unknown':True}

    async def send(self,key,text,reply_markup=None):
        if not os.getenv('PROFIT_FUNNEL_BOT_TOKEN'):return False
        async with self.pool.acquire() as c:
            claimed=await c.fetchval("INSERT INTO profit_funnel_outbox(key,state) VALUES($1,'claimed') ON CONFLICT DO NOTHING RETURNING key",key)
        if not claimed:return False
        # Claim the whole report before sending; ambiguous delivery is never retried blindly.
        chunks=[text[i:i+3900] for i in range(0,len(text),3900)]
        ok=True;mid=None
        for i,chunk in enumerate(chunks):
            result=await self.telegram('sendMessage',{'chat_id':os.getenv('PROFIT_FUNNEL_OWNER_CHAT_ID') or os.getenv('OWNER_CHAT_ID'),
                                                   'text':chunk,'disable_web_page_preview':True,**({'reply_markup':reply_markup} if reply_markup and i==len(chunks)-1 else {})})
            if not result.get('ok'):ok=False;break
            mid=result['result']['message_id']
        async with self.pool.acquire() as c:
            await c.execute('UPDATE profit_funnel_outbox SET state=$2,telegram_message_id=$3,error_code=$4,updated_at=NOW() WHERE key=$1',
                            key,'sent' if ok else 'delivery_unknown' if result.get('delivery_unknown') else 'failed',mid,None if ok else str(result.get('error_code'))[:60])
        return ok

    async def show(self,key,r,command,period,query=None,page=0):
        content=presentation.pages(self.text(r,command))
        page=max(0,min(page,len(content)-1))
        text=content[page]
        if len(content)>1:text+=f'\n\n📄 {page+1} / {len(content)}'
        markup=presentation.keyboard(command,period,page,len(content))
        if not query:return await self.send(key,text,markup)
        async with self.pool.acquire() as c:
            claimed=await c.fetchval("INSERT INTO profit_funnel_outbox(key,state) VALUES($1,'claimed') ON CONFLICT DO NOTHING RETURNING key",key)
        if not claimed:return False
        result=await self.telegram('editMessageText',{'chat_id':query['message']['chat']['id'],
            'message_id':query['message']['message_id'],'text':text,'reply_markup':markup,'disable_web_page_preview':True})
        async with self.pool.acquire() as c:
            await c.execute('UPDATE profit_funnel_outbox SET state=$2,telegram_message_id=$3,error_code=$4,updated_at=NOW() WHERE key=$1',
                key,'sent' if result.get('ok') else 'delivery_unknown' if result.get('delivery_unknown') else 'failed',
                query['message']['message_id'],None if result.get('ok') else str(result.get('error_code'))[:60])
        return result.get('ok',False)

    async def commands(self):
        async with self.pool.acquire() as c:
            state=await self.state(c,'telegram_offset') or {'value':0}
        data=await self.telegram('getUpdates',{'offset':state['value'],'timeout':0,'allowed_updates':['message','callback_query']})
        reports={}
        for update in data.get('result',[]):
            query=update.get('callback_query')
            if query:await self.telegram('answerCallbackQuery',{'callback_query_id':query['id']})
            if self.owner_allowed(update):
                period='today';page=0;command=None
                if query:
                    parts=str(query.get('data','')).split(':')
                    if len(parts)==4 and parts[0]=='pf' and parts[1] in COMMANDS+['menu','behavior','attention'] and parts[2] in ('today','yesterday','week') and parts[3].isdigit():
                        command,period,page=parts[1],parts[2],min(int(parts[3]),50)
                else:
                    command=((update['message'].get('text') or '').split(' ')[0].split('@')[0]).lstrip('/')
                    phrase=(update['message'].get('text') or '').strip().upper()
                    if phrase=='PAUSE CPA AGENT':command='cpa_pause'
                    if phrase=='RESUME CPA AGENT':command='cpa_resume'
                    if command in ('start','menu'):command='menu'
                    if command in ('today','yesterday','week'):period=command
                if command in COMMANDS+['menu','behavior','attention','debug','debug_last']:
                    if command in posting_controls.COMMANDS:
                        async with self.pool.acquire() as c:
                            response=await posting_controls.handle(c,command,update.get('message',{}).get('text',''))
                        await self.send('command:'+str(update['update_id']),response)
                        async with self.pool.acquire() as c:
                            await self.put(c,'telegram_offset',{'value':update['update_id']+1})
                        continue
                    if command in ('cpa_pause','cpa_resume'):
                        async with self.pool.acquire() as c:
                            await self.controller.put(c,'cpa_owner_paused',command=='cpa_pause')
                            await self.controller.put(c,'cpa_monitor',{})
                        await self.send('command:'+str(update['update_id']),
                            '🤖 CPA Agent приостановлен владельцем.' if command=='cpa_pause' else '🤖 CPA Agent возобновлён. Проверки, границы 200–350 ₽ и cooldown сохранены. Остановленные рекламные кампании автоматически не запускаются.')
                        async with self.pool.acquire() as c:
                            await self.put(c,'telegram_offset',{'value':update['update_id']+1})
                        continue
                    if period not in reports:reports[period]=await self.report(period)
                    r=reports[period]
                    if command in ('debug','debug_last'):
                        debug={'runtime':self.status,'period':r['period'],'stages':r['instrumented_funnel']['counts'],
                            'quality':r['instrumented_funnel']['quality'],'technical':r['technical']}
                        await self.send('command:'+str(update['update_id']),'🔎 Диагностика для владельца\n'+json.dumps(debug,ensure_ascii=False,default=str)[:3000])
                    else:await self.show(('callback:' if query else 'command:')+str(update['update_id']),r,command,period,query,page)
            async with self.pool.acquire() as c:
                await self.put(c,'telegram_offset',{'value':update['update_id']+1})

    async def alerts(self,r,now):
        f=r['instrumented_funnel'];n=f['counts'];candidates={}
        for start,end,minimum in [('PVZ_PICKER_OPEN','PVZ_SELECTED',10),('PAYMENT_STARTED','PAYMENT_SUCCESS',10),('CHECKOUT_OPEN','PVZ_SELECTED',20),('PVZ_SELECTED','PAYMENT_STARTED',10)]:
            if n[start]>=minimum and n[end]==0:candidates[start+'-zero-'+end]=('WARNING',f'⚠️ {n[start]} {start} и 0 {end}. Проверьте сегмент в Webvisor; это сигнал, не диагноз.')
        if r['technical']['shipment_failures']:candidates['paid-no-shipment']=('CRITICAL','⚠️ Verified PAID: повторные ошибки Ozon shipment. Проверьте existing posting, дубль не создавайте.')
        if r['technical']['status_sync_stale']:candidates['ozon-stale']=('WARNING','⚠️ Ozon status sync: есть отправления без свежей проверки более 10 минут.')
        if any(x['state']=='failed' and x['kind'].endswith('_email') for x in r['technical']['service_messages']):candidates['email-failed']=('WARNING','⚠️ Сервисное письмо по реальному заказу не отправлено. Проверьте доставку Resend; дубли автоматически не отправляйте.')
        for warning in r['technical'].get('customer_communication_warnings',[]):
            paid=warning['kind']=='paid_email';number=warning['order_number']
            heading='ОПЛАТА БЕЗ ПИСЬМА КЛИЕНТУ' if paid else 'КЛИЕНТ МОЖЕТ НЕ ЗНАТЬ О ПРИБЫТИИ ЗАКАЗА'
            text=f'⚠️ {heading}\nЗаказ: №{number}\n'+('Оплата подтверждена.' if paid else 'Статус Ozon: В ПВЗ.')+'\nEmail клиенту: не отправлен, доставка не подтверждена или зарегистрирована ошибка.\nПроверьте раздел «Заказы». Не отправляйте дубль вслепую.'
            candidates[f'customer-email-{warning["kind"]}-{number}']=('WARNING',text)
        if sum(v for k,v in f['errors'].items() if k.endswith('/PVZ_TIMEOUT'))>=10:candidates['pvz-timeouts']=('CRITICAL','⚠️ 10+ реальных PVZ_TIMEOUT за день. Проверьте transport и сегменты устройств.')
        for key,count in f['errors'].items():
            if count>=10:candidates['errors-'+key]=('WARNING',f'⚠️ {count} технических ошибок: {key}. Проверьте сегмент, персональные поля читать не требуется.')
        for seg,v in f['segments'].items():
            opened=v['stages']['PVZ_PICKER_OPEN']
            if opened>=20 and ratio(v['stages']['PVZ_SELECTED'],opened)<20:
                candidates['device-'+seg]=('WARNING',f'⚠️ Possible device PVZ problem: {seg}, выбор {v["stages"]["PVZ_SELECTED"]}/{opened}; смотрите 5–10 Webvisor sessions.')
        for name,a in r['ads'].get('channels',{}).items():
            matching=[v for k,v in f['channel_device'].items() if k.startswith(name+'/')]
            visits=sum(v['sessions'] for v in matching);buys=sum(v.get('buy',0) for v in matching)
            if a['clicks']>=30 and visits>=20 and not buys:
                candidates['ad-no-buy-'+name]=('WARNING',f'⚠️ {name}: {a["clicks"]} clicks, {visits} measured sessions, no Buy. Check attribution and landing page; do not increase budget.')
        if r['stock']['estimated_units']<100:candidates['stock']=('WARNING',f'⚠️ Estimated stock {r["stock"]["estimated_units"]}; confirm physical stock.')
        desktop=[v for k,v in f['segments'].items() if k.startswith('DESKTOP/')]
        base_n=sum(v['stages']['PVZ_PICKER_OPEN'] for v in desktop);base_ok=sum(v['stages']['PVZ_SELECTED'] for v in desktop)
        if base_n>=20 and base_ok/base_n>=.5:
            for key,v in f['segments'].items():
                n=v['stages']['PVZ_PICKER_OPEN']
                if key.startswith('MOBILE/') and n>=20 and v['stages']['PVZ_SELECTED']/n<.5*base_ok/base_n:
                    candidates['relative-device-'+key]=('WARNING',f'⚠️ PVZ conversion in {key} is less than half desktop; inspect 5–10 sessions. LOW SAMPLE guard passed.')
        async with self.pool.acquire() as c:
            for key,(severity,text) in candidates.items():
                row=await c.fetchrow('''INSERT INTO profit_funnel_alerts(key,severity,first_seen,last_seen,status)
                    VALUES($1,$2,$3,$3,'OPEN') ON CONFLICT(key) DO UPDATE SET last_seen=EXCLUDED.last_seen,status='OPEN'
                    RETURNING last_sent''',key,severity,now)
                if not row['last_sent'] or now-row['last_sent']>=timedelta(hours=6):
                    sent=await self.send('alert:'+key+':'+now.strftime('%Y%m%d%H'),text if key.startswith('customer-email-') else presentation.alert_text(key,severity),presentation.keyboard('errors','today'))
                    if sent:await c.execute('UPDATE profit_funnel_alerts SET last_sent=$2 WHERE key=$1',key,now)
            await c.execute("UPDATE profit_funnel_alerts SET status='RESOLVED' WHERE status='OPEN' AND NOT(key=ANY($1::text[]))",list(candidates))

    async def loop(self):
        while True:
            try:
                async with self.pool.acquire() as leader:
                    await self.schema(leader)
                    await leader.execute("UPDATE profit_funnel_outbox SET state='delivery_unknown',error_code='worker_interrupted',updated_at=NOW() WHERE state='claimed' AND updated_at<NOW()-INTERVAL '15 minutes'")
                    locked=await leader.fetchval('SELECT pg_try_advisory_lock($1)',LOCK)
                    if locked:
                        try:
                            await self.observe(leader)
                            try:
                                await self.drain()
                                self.status.pop('transport_error',None)
                            except Exception as exc:self.status['transport_error']=type(exc).__name__
                            if os.getenv('PROFIT_FUNNEL_BOT_TOKEN'):
                                await self.commands()
                                now=datetime.now(UTC);local=now.astimezone(MSK)
                                if local.hour>=9:
                                    key='daily:'+local.date().isoformat()
                                    if not await leader.fetchval('SELECT EXISTS(SELECT 1 FROM profit_funnel_outbox WHERE key=$1)',key):
                                        r=await self.report('yesterday');await self.show(key,r,'yesterday','yesterday')
                                if local.weekday()==0 and (local.hour>9 or local.hour==9 and local.minute>=10):
                                    key='weekly:'+local.date().isoformat()
                                    if not await leader.fetchval('SELECT EXISTS(SELECT 1 FROM profit_funnel_outbox WHERE key=$1)',key):
                                        r=await self.report('week');await self.show(key,r,'week','week')
                                if local.minute%10==0:
                                    r=await self.report('today');await self.alerts(r,now)
                            self.status={**self.status,'state':'running','checked_at':datetime.now(UTC).isoformat(),'last_error':None,
                                         'bot_configured':bool(os.getenv('PROFIT_FUNNEL_BOT_TOKEN')),
                                         'daily':'09:00 Europe/Moscow','weekly':'Monday 09:10 Europe/Moscow'}
                        finally:await leader.execute('SELECT pg_advisory_unlock($1)',LOCK)
            except asyncio.CancelledError:raise
            except Exception as exc:self.status={'state':'retrying','last_error':type(exc).__name__}
            await asyncio.sleep(15)
