import unittest
from datetime import datetime,timezone,timedelta
from uuid import uuid4
from profit_funnel import safe_client,video_summary
from profit_presentation import video_view

class VideoTests(unittest.TestCase):
    def test_allowlist_metrics_and_server_paid_boundary(self):
        batch={'session_id':str(uuid4()),'events':[{'event_id':str(uuid4()),'name':'VIDEO_CLOSE','video_view_id':str(uuid4()),'video_watch_seconds':12,'video_duration_seconds':66,'video_completion_percent':18.2}]}
        self.assertEqual(safe_client(batch,'Android Mobile')['events'][0][3]['video_watch_seconds'],12)
        for field,bad in [('video_watch_seconds',float('nan')),('video_completion_percent',101),('video_duration_seconds',True)]:
            copy={**batch,'events':[{**batch['events'][0],field:bad}]}
            with self.assertRaises(ValueError):safe_client(copy,'Android')
        batch['events'][0]['name']='PAYMENT_SUCCESS'
        with self.assertRaises(ValueError):safe_client(batch,'Android')

    def test_snapshots_not_added_twice_and_sales_must_follow_play(self):
        t=datetime.now(timezone.utc);vid=str(uuid4())
        def ev(name,sec,watch=0):return {'name':name,'occurred_at':t+timedelta(seconds=sec),'payload':{'video_view_id':vid,'video_watch_seconds':watch,'video_duration_seconds':66,'video_completion_percent':watch/66*100},'order_id':42 if name=='PAYMENT_SUCCESS' else None}
        groups={'a':{'all':[ev('BUY_BUTTON_CLICK',0),ev('VIDEO_PLAY',1),ev('VIDEO_PAUSE',12,10),ev('VIDEO_CLOSE',23,20),ev('CHECKOUT_OPEN',25),ev('PAYMENT_SUCCESS',30)]},'b':{'all':[ev('BUY_BUTTON_CLICK',1)]}}
        r=video_summary(groups)
        self.assertEqual(r['watch_seconds']['mean'],20)
        self.assertEqual(r['after_video'],{'buy':0,'checkout':1,'paid':1})
        self.assertEqual(r['cohorts']['non_viewers']['buy_cr'],100)
        self.assertEqual(r['order_links'][0]['order_ids'],['42'])
        text='\n'.join(video_view(r,True));self.assertNotIn('VIDEO_PLAY',text);self.assertIn('Пока мало данных',text)

    def test_no_views_means_unknown_duration_not_fake_zero(self):
        r=video_summary({});self.assertIsNone(r['watch_seconds']['mean']);self.assertIsNone(r['completion_percent']['mean'])

if __name__=='__main__':unittest.main()
