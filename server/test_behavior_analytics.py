import unittest,json
from datetime import datetime,timezone,timedelta
from uuid import uuid4
from profit_funnel import safe_client
from behavior_analytics import summary,traffic
from traffic_attribution import source_report

class BehaviorTests(unittest.TestCase):
 def test_unknown_and_direct_are_not_free_and_unknown_paid_is_paid(self):
  sessions=[{'session_id':str(i),'attribution':a} for i,a in enumerate([{}, {'utm_source':'unexpected'},{'yclid':'123'}, {'utm_source':'telegram','utm_campaign':'content_engine'}])]
  t=traffic(source_report(sessions,[]));self.assertEqual((t['business_visits'],t['paid'],t['free'],t['direct_unknown']),(4,1,1,2))
 def test_safe_review_identifiers_and_no_contact_fields(self):
  b={'session_id':str(uuid4()),'events':[{'event_id':str(uuid4()),'name':'REVIEW_PHOTO_OPEN','review_id':'wb497049795-290820251055','media_index':0,'source_store':'IP_ALEKSEEVA_LV','featured_review':True,'email':'private@example.com'}]}
  p=safe_client(b,'Android')['events'][0][3];self.assertNotIn('email',p);self.assertEqual(p['media_index'],0)
  b['events'][0]['review_id']='private@example.com'
  with self.assertRaises(ValueError):safe_client(b,'Android')
 def test_client_cannot_invent_verified_payment(self):
  for n in ('VERIFIED_PAID','PAYMENT_SUCCESS'):
   with self.assertRaises(ValueError):safe_client({'session_id':str(uuid4()),'events':[{'event_id':str(uuid4()),'name':n}]},'')
 def test_cohorts_deduplicate_media_exclude_owner_and_require_subsequent_buy(self):
  now=datetime.now(timezone.utc);sessions=[{'session_id':'a','attribution':{}},{'session_id':'owner','traffic_class':'owner'}]
  def e(s,n,t,p={}):return {'session_id':s,'name':n,'occurred_at':now+timedelta(seconds=t),'payload':p}
  events=[e('a','BUY_BUTTON_CLICK',0),e('a','REVIEWS_OPEN',1),e('a','REVIEW_PHOTO_OPEN',2,{'review_id':'review-1','media_index':0}),e('a','REVIEW_PHOTO_OPEN',3,{'review_id':'review-1','media_index':0}),e('owner','PAYMENT_SUCCESS',4)]
  r=summary(events,sessions);self.assertEqual(r['ALL']['counts']['visitors'],1);self.assertEqual(r['ALL']['cohorts']['reviews']['buy'],0);self.assertEqual(r['ALL']['photos_unique_visitors'],{'1':1,'2':0,'5':0});self.assertEqual(r['FREE']['counts']['visitors'],0)
