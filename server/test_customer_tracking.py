import sys,unittest,json,base64,hmac,hashlib
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).parent))
from customer_tracking import tracking_token,token_digest,tracking_url,public_tracking,verify_resend_event,delivery_state
from customer_messages import customer_message,customer_email_html

class CustomerTrackingTests(unittest.TestCase):
 def test_separate_unpredictable_token_and_scope(self):
  internal='a'*64
  token=tracking_token('example-only-secret',internal)
  self.assertNotEqual(token,internal);self.assertEqual(len(token),64)
  self.assertNotEqual(token,tracking_token('example-only-secret','b'*64))
  self.assertNotEqual(token,tracking_token('rotated',internal))
  self.assertEqual(tracking_url(token).split('#')[0],'https://xn--163-5cdt3dgrs.xn--p1ai/page/track')
  for invalid in ['1009','a'*63,'../1009','G'*64]:
   with self.assertRaises(ValueError):token_digest(invalid)
 def test_public_status_minimal_and_human(self):
  row={'order_number':1009,'order_id':123,'payment_id':'hidden','payment_status':'succeeded',
   'shipment_id':'internal shipment','tracking_number':'public posting','quantity':1,'amount':800,
   'ozon_status':'in_delivery_point','updated_at':datetime(2026,10,4,tzinfo=timezone.utc)}
  snap={'email':'private@example.com','phone':'+70000000000','recipient_name':'Private','pickup_address':'Пирогова,16'}
  data=public_tracking(row,snap)
  self.assertEqual(data['stage'],4);self.assertIn('можно забирать',data['status_label'])
  for key in ('email','phone','recipient_name','order_id','payment_id','shipment_id','ozon_status'):
   self.assertNotIn(key,data)
 def test_paid_ack_without_shipment(self):
  details={'order_number':1013,'amount':'1200.00','quantity':1,'pickup_address':'Самара','tracking_url':'https://example.com/page/track#token'}
  text=customer_message('paid_email',None,details)
  self.assertIn('прошла успешно',text);self.assertIn('1200.00',text)
  self.assertNotIn('передан в Ozon',text);self.assertNotIn('установите',text.lower())
  self.assertIn('Отследить заказ',customer_email_html('paid_email',None,details))
 def test_html_escapes_provider_text(self):
  html=customer_email_html('ready_email','<script>',{'order_number':1009,'pickup_address':'<img onerror=x>','tracking_url':'https://example.com/#token'})
  self.assertNotIn('<script>',html);self.assertIn('&lt;img',html)
 def test_verified_event_rejects_tampering_replay_and_missing_secret(self):
  raw=json.dumps({'type':'email.delivered','data':{'email_id':'resend-123'}}).encode();key=b'test-secret'
  secret='whsec_'+base64.b64encode(key).decode();stamp='1000';id='message-123'
  signature=base64.b64encode(hmac.new(key,id.encode()+b'.'+stamp.encode()+b'.'+raw,hashlib.sha256).digest()).decode()
  headers={'svix-id':id,'svix-timestamp':stamp,'svix-signature':'v1,'+signature}
  self.assertEqual(verify_resend_event(raw,headers,secret,now=1000)[1]['type'],'email.delivered')
  for payload,sec,now in [(raw+b' ',secret,1000),(raw,secret,1301),(raw,'',1000)]:
   with self.assertRaises(ValueError):verify_resend_event(payload,headers,sec,now)
 def test_event_order_cannot_downgrade_delivery(self):
  self.assertEqual(delivery_state('delivered','email.sent'),'delivered')
  self.assertEqual(delivery_state('sent','email.delivered'),'delivered')
  self.assertEqual(delivery_state('bounced','email.sent'),'bounced')

if __name__=='__main__':unittest.main()
