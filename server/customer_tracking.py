"""Minimal customer tracking and authenticated Resend event helpers."""
import base64, hashlib, hmac, json, re, time
from datetime import datetime

TRACK_ORIGIN='https://xn--163-5cdt3dgrs.xn--p1ai'
STATUS_LABELS={
 'created':'Заказ готовится к передаче в доставку',
 'forming':'Заказ готовится к передаче в доставку',
 'ready_for_shipping':'Заказ готовится к передаче в доставку',
 'in_container':'Заказ готовится к передаче в доставку',
 'acceptance_in_progress':'Заказ передан в Ozon',
 'on_way':'Заказ в пути', 'in_courier_service':'Заказ в пути',
 'in_delivery_point':'Заказ прибыл в пункт выдачи — можно забирать',
 'ready_for_pickup':'Заказ прибыл в пункт выдачи — можно забирать',
 'delivered':'Заказ получен','canceled':'Заказ отменён','cancelled':'Заказ отменён',
 'forming_failed':'Уточняем доставку заказа','not_accepted_to_delivery':'Уточняем доставку заказа',
 'unknown':'Уточняем статус доставки'}

def tracking_token(secret, internal_token):
 if not secret or not re.fullmatch(r'[a-f0-9]{64}',str(internal_token)):
  raise ValueError('Tracking unavailable')
 return hmac.new(secret.encode(),('customer-tracking-v1:'+internal_token).encode(),hashlib.sha256).hexdigest()

def token_digest(token):
 if not re.fullmatch(r'[a-f0-9]{64}',str(token)):raise ValueError('Invalid tracking link')
 return hashlib.sha256(token.encode()).hexdigest()

def tracking_url(token):
 token_digest(token)
 # Fragment is not transmitted in the InSales request or Referer/access logs.
 return TRACK_ORIGIN+'/page/track#'+token

def public_tracking(row,snapshot):
 paid=row.get('payment_status')=='succeeded'
 status=row.get('ozon_status')
 label=STATUS_LABELS.get(status,STATUS_LABELS['unknown']) if paid else 'Ожидаем подтверждение оплаты'
 if paid and not row.get('shipment_id'):label='Оплата подтверждена. Заказ готовится к доставке'
 stage=0
 if paid:stage=1
 if row.get('shipment_id') and status in ('acceptance_in_progress','on_way','in_courier_service','in_delivery_point','ready_for_pickup','delivered'):stage=2
 if status in ('on_way','in_courier_service','in_delivery_point','ready_for_pickup','delivered'):stage=3
 if status in ('in_delivery_point','ready_for_pickup','delivered'):stage=4
 if status=='delivered':stage=5
 updated=row.get('status_checked_at') or row.get('updated_at')
 return {'order_number':row['order_number'],'paid':paid,'quantity':row['quantity'],
  'amount':float(row['amount']),'currency':'RUB','delivery_price':0,'status_label':label,
  'stage':stage,'cancelled':status in ('canceled','cancelled') or row.get('payment_status')=='canceled',
  'tracking_number':row.get('tracking_number') if paid else None,
  'pickup_title':snapshot.get('pickup_title','Ozon ПВЗ'),
  'pickup_address':snapshot.get('pickup_address',''),
  'pickup_city':snapshot.get('pickup_city',''),
  'last_update':updated.isoformat() if hasattr(updated,'isoformat') else updated}

def verify_resend_event(raw,headers,secret,now=None):
 """Verify Standard Webhooks/Svix raw-body signature before trusting JSON."""
 if not secret:raise ValueError('Webhook not configured')
 message_id=headers.get('svix-id','');stamp=headers.get('svix-timestamp','')
 try:ts=int(stamp)
 except (ValueError,TypeError):raise ValueError('Invalid webhook')
 if abs((now if now is not None else time.time())-ts)>300:raise ValueError('Expired webhook')
 if not message_id or len(message_id)>200:raise ValueError('Invalid webhook')
 try:key=base64.b64decode(secret.removeprefix('whsec_'),validate=True)
 except Exception:raise ValueError('Invalid webhook configuration')
 expected=base64.b64encode(hmac.new(key,message_id.encode()+b'.'+stamp.encode()+b'.'+raw,hashlib.sha256).digest()).decode()
 signatures=[s[3:] for s in headers.get('svix-signature','').split() if s.startswith('v1,')]
 if not any(hmac.compare_digest(expected,s) for s in signatures):raise ValueError('Invalid webhook signature')
 event=json.loads(raw)
 if not isinstance(event,dict):raise ValueError('Invalid webhook')
 return message_id,event

def delivery_state(current,event_type):
 """Never downgrade a final event when a delayed sent event arrives."""
 target={'email.sent':'sent','email.delivered':'delivered','email.bounced':'bounced',
         'email.failed':'failed','email.complained':'bounced'}.get(event_type)
 if target is None:return current
 if current in ('bounced','failed'):return current
 if current=='delivered' and target=='sent':return current
 return target
