"""Owner-approved CPS accounting. No payout implementation or customer PII.

Only registered ACTIVE sources can receive credit. Missing provider receipt time
or incomplete return evidence prevents reward approval. No historical click backfill.
"""
import json, uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone

POLICY={'version':'owner-20261007-v1','hold_days':14,'attribution_days':30,
        'multitouch':'LAST_VALID_AFFILIATE_TOUCH_BEFORE_ORDER','reward_rub':200,
        'reward_unit':'ONE_FULLY_RECEIVED_ORDER','payout':'MANUAL_OWNER_APPROVED'}

def obj(x):
    return json.loads(x) if isinstance(x,str) else (x or {})

def source_id(attr):
    if attr.get('utm_source')!='affiliate' or attr.get('utm_medium')!='cps':return None
    try:return str(uuid.UUID(attr['utm_content']))
    except (ValueError,KeyError,TypeError):return None

def choose_touch(touches,ordered_at):
    valid=[t for t in touches if ordered_at-timedelta(days=30)<=t['at']<=ordered_at]
    if not valid:return None
    latest=max(t['at'] for t in valid)
    candidates=[t for t in valid if t['at']==latest]
    if len({t['source_id'] for t in candidates})>1:return {'conflict':True}
    return candidates[0]

def reward_state(row,receipt,returned=False,now=None):
    now=now or datetime.now(timezone.utc)
    if returned or row.get('payment_status') in ('canceled','refunded') or row.get('ozon_status') in ('canceled','cancelled','returned','not_picked_up'):
        return {'state':'REJECTED','reward_rub':0,'reason':'CANCEL_NONPICKUP_RETURN_REFUND'}
    if row.get('payment_status')!='succeeded' or not row.get('payment_id'):
        return {'state':'ORDER_CREATED','reward_rub':0,'reason':'AWAIT_VERIFIED_PAID'}
    if not row.get('shipment_id'):return {'state':'PAID','reward_rub':0,'reason':'AWAIT_SHIPMENT'}
    if row.get('ozon_status')!='delivered':return {'state':'SHIPPED','reward_rub':0,'reason':'AWAIT_CONFIRMED_PICKUP'}
    # status_checked_at/updated_at are NOT the customer's receipt timestamp.
    if not receipt or not receipt.get('provider_verified') or not receipt.get('all_postings_received'):
        return {'state':'EVIDENCE_PENDING','reward_rub':0,'reason':'RECEIPT_TIMESTAMP_OR_ALL_POSTINGS_UNVERIFIED'}
    at=receipt.get('received_at')
    if isinstance(at,str):at=datetime.fromisoformat(at)
    if not isinstance(at,datetime) or not at.tzinfo or at>now:
        return {'state':'EVIDENCE_PENDING','reward_rub':0,'reason':'INVALID_RECEIPT_TIMESTAMP'}
    until=at+timedelta(days=14)
    # Return closure is provider/owner-confirmed evidence, never assumed from absence.
    checked=receipt.get('return_checked_at')
    if isinstance(checked,str):checked=datetime.fromisoformat(checked)
    fresh=isinstance(checked,datetime) and checked.tzinfo and timedelta(0)<=now-checked<=timedelta(hours=1)
    if receipt.get('refund_detected'):return {'state':'REJECTED','reward_rub':0,'reason':'PROVIDER_REFUND'}
    if now>=until and receipt.get('return_window_verified') and fresh:
        return {'state':'APPROVED','reward_rub':200,'reason':'VERIFIED_RECEIPT_HOLD_COMPLETE','hold_until':until.isoformat()}
    return {'state':'HOLD','reward_rub':0,'reason':'HOLD_OR_RETURN_EVIDENCE_PENDING','hold_until':until.isoformat()}

async def schema(c):
    await c.execute('''CREATE TABLE IF NOT EXISTS cps_sources(
      source_id TEXT PRIMARY KEY,affiliate_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'OWNER_REVIEW',
      offer_version TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
    CREATE TABLE IF NOT EXISTS cps_touches(
      touch_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES cps_sources,
      clicked_at TIMESTAMPTZ NOT NULL,observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
    CREATE TABLE IF NOT EXISTS cps_order_credit(
      order_id BIGINT NOT NULL,offer_version TEXT NOT NULL,affiliate_id TEXT,source_id TEXT,
      credit_provider TEXT NOT NULL DEFAULT 'OWN',touch_id TEXT,linkage JSONB NOT NULL DEFAULT '{}',
      state TEXT NOT NULL,reward_rub INT NOT NULL DEFAULT 0,reason TEXT NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY(order_id,offer_version));
    CREATE TABLE IF NOT EXISTS cps_evidence(
      order_id BIGINT PRIMARY KEY,shipment_id TEXT NOT NULL,evidence JSONB NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
    CREATE TABLE IF NOT EXISTS cps_reason_log(
      id BIGSERIAL PRIMARY KEY,order_id BIGINT NOT NULL,offer_version TEXT NOT NULL,
      before_state TEXT,after_state TEXT NOT NULL,reason TEXT NOT NULL,observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW());''')

async def ingest_touch(c,clean):
    if clean.get('traffic_class')!='customer' or clean.get('is_test') or clean.get('is_internal'):return
    src=source_id(clean['attribution'])
    if not src:return
    registered=await c.fetchval("SELECT EXISTS(SELECT 1 FROM cps_sources WHERE source_id=$1 AND status='ACTIVE' AND offer_version=$2)",src,POLICY['version'])
    if not registered:return
    # Only a fresh explicit affiliate URL touch event qualifies, not sticky UTM on cart.
    for eid,name,at,p in clean['events']:
        if name!='AFFILIATE_TOUCH':continue
        await c.execute('''INSERT INTO cps_touches(touch_id,session_id,source_id,clicked_at)
           VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING''',eid,clean['session_id'],src,at)

async def record_receipt(c,row,postings):
    """Called only after the existing authenticated Ozon read and linkage checks."""
    if row.get('is_internal') or row.get('is_test') or row.get('ozon_status')!='delivered':return
    if len(postings)!=row['quantity'] or not postings:return
    if any(p.get('status')!='delivered' or p.get('order_number')!=row['shipment_id'] for p in postings):return
    try:
        stamps=[datetime.fromisoformat(p['status_changed_at'].replace('Z','+00:00')) for p in postings]
        at=max(stamps)
        if any(not t.tzinfo for t in stamps) or at>datetime.now(timezone.utc):return
    except (ValueError,TypeError,KeyError):return
    evidence={'provider_verified':True,'all_postings_received':True,'received_at':at.isoformat(),
      'posting_ids':sorted(p['posting_number'] for p in postings),'source':'Ozon posting/info delivered status_changed_at',
      'return_window_verified':False}
    # Receipt time is immutable on replay; do not restart the hold on every poll.
    await c.execute('''INSERT INTO cps_evidence(order_id,shipment_id,evidence) VALUES($1,$2,$3::jsonb)
      ON CONFLICT(order_id) DO NOTHING''',row['order_id'],row['shipment_id'],json.dumps(evidence))

async def verify_return_window(c,row,payment_read,returns_read):
    """Read-only providers; failed/incomplete evidence never approves a reward."""
    if row.get('is_internal') or row.get('is_test'):return
    stored=await c.fetchrow('SELECT shipment_id,evidence FROM cps_evidence WHERE order_id=$1',row['order_id'])
    if not stored or stored['shipment_id']!=row.get('shipment_id'):return
    e=obj(stored['evidence']); at=datetime.fromisoformat(e['received_at']);now=datetime.now(timezone.utc)
    if now<at+timedelta(days=14):return
    if not await c.fetchval('SELECT EXISTS(SELECT 1 FROM cps_order_credit WHERE order_id=$1)',row['order_id']):return
    payment=await payment_read(row['payment_id'])
    amount=payment.get('amount',{});refund=payment.get('refunded_amount',{})
    if (payment.get('id')!=row['payment_id'] or payment.get('status')!='succeeded' or payment.get('paid') is not True
        or amount.get('currency')!='RUB' or Decimal(amount.get('value','-1'))!=Decimal(str(row['amount']))
        or refund.get('currency')!='RUB'):return
    refunded=Decimal(refund.get('value','-1'))
    if refunded<0:return
    e['refund_detected']=refunded>0
    returns=await returns_read(c,at)
    e['return_window_verified']=bool(returns.get('available') and returns.get('unlinked')==0 and refunded==0)
    e['return_checked_at']=now.isoformat()
    await c.execute('UPDATE cps_evidence SET evidence=$2::jsonb,updated_at=NOW() WHERE order_id=$1',row['order_id'],json.dumps(e))

async def sync(c):
    # Registry approval remains owner-controlled; no financial or fulfillment writes.
    rows=await c.fetch('''SELECT p.* FROM commerce_pending_orders p WHERE order_id IS NOT NULL
       AND NOT is_test AND NOT is_internal AND EXISTS(
         SELECT 1 FROM profit_funnel_sessions s JOIN cps_touches t ON t.session_id=s.session_id
         WHERE s.checkout_session_id=p.session_id OR
           (NULLIF(s.attribution->>'client_id','') IS NOT NULL AND
            s.attribution->>'client_id'=p.snapshot->'attribution'->>'client_id'))''')
    for raw in rows:
        row=dict(raw)
        async with c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock($1)',row['order_id'])
            touches=await c.fetch('''SELECT t.touch_id,t.source_id,t.clicked_at AS at,r.affiliate_id,r.offer_version
              FROM cps_touches t JOIN cps_sources r USING(source_id)
              JOIN profit_funnel_sessions s ON s.session_id=t.session_id
              WHERE (s.checkout_session_id=$1 OR
                  (NULLIF(s.attribution->>'client_id','') IS NOT NULL AND s.attribution->>'client_id'=$3))
              AND NOT s.is_internal AND NOT s.is_test
              AND r.status='ACTIVE' AND r.offer_version=$2 ''',row['session_id'],POLICY['version'],obj(row['snapshot']).get('attribution',{}).get('client_id'))
            chosen=choose_touch([dict(t) for t in touches],row['created_at'])
            if not chosen:continue
            old=await c.fetchrow('SELECT * FROM cps_order_credit WHERE order_id=$1 AND offer_version=$2 FOR UPDATE',row['order_id'],POLICY['version'])
            if chosen.get('conflict') or old and (old['credit_provider']!='OWN' or old['source_id']!=chosen['source_id']):
                result={'state':'MANUAL_REVIEW','reward_rub':0,'reason':'ATTRIBUTION_CONFLICT'}
            else:
                receipt=await c.fetchrow('SELECT shipment_id,evidence FROM cps_evidence WHERE order_id=$1',row['order_id'])
                evidence=obj(receipt['evidence']) if receipt and receipt['shipment_id']==row.get('shipment_id') else None
                returned=await c.fetchval("SELECT EXISTS(SELECT 1 FROM profit_controller_returns WHERE order_id=$1)",row['order_id'])
                result=reward_state(row,evidence,returned)
            if old and old['state']==result['state'] and old['reward_rub']==result['reward_rub'] and old['reason']==result['reason']:continue
            if not old:
                snapshot=obj(row['snapshot'])
                linkage={'payment_id':row['payment_id'],'shipment_id':row['shipment_id'],
                  'pickup_point_id':snapshot.get('pickup_point_id'),'checkout_session_id':row['session_id'],
                  'policy':POLICY,'hold_until':result.get('hold_until')}
                await c.execute('''INSERT INTO cps_order_credit(order_id,offer_version,affiliate_id,source_id,touch_id,linkage,state,reward_rub,reason)
                  VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9)''',row['order_id'],POLICY['version'],chosen.get('affiliate_id'),chosen.get('source_id'),chosen.get('touch_id'),json.dumps(linkage),result['state'],result['reward_rub'],result['reason'])
            else:
                await c.execute('''UPDATE cps_order_credit SET state=$3,reward_rub=$4,reason=$5,
                  linkage=linkage || $6::jsonb,updated_at=NOW() WHERE order_id=$1 AND offer_version=$2''',row['order_id'],POLICY['version'],result['state'],result['reward_rub'],result['reason'],json.dumps({'payment_id':row['payment_id'],'shipment_id':row['shipment_id'],'hold_until':result.get('hold_until')}))
            await c.execute('''INSERT INTO cps_reason_log(order_id,offer_version,before_state,after_state,reason)
              VALUES($1,$2,$3,$4,$5)''',row['order_id'],POLICY['version'],old['state'] if old else None,result['state'],result['reason'])

async def report(c):
    rows=await c.fetch('''SELECT affiliate_id,state,count(*) AS orders,sum(reward_rub) AS reward_rub
        FROM cps_order_credit GROUP BY affiliate_id,state''')
    return {'policy':POLICY,'automatic_payout_enabled':False,'sources':await c.fetchval('SELECT count(*) FROM cps_sources'),
      'measured_touches':await c.fetchval('SELECT count(*) FROM cps_touches'),'items':[dict(r) for r in rows],
      'receipt_adapter':'FAIL_CLOSED_UNTIL_VERIFIED_PROVIDER_RECEIPT_AND_RETURN_WINDOW_EVIDENCE'}
