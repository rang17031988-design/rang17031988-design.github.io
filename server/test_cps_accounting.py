import unittest
from datetime import datetime,timedelta,timezone
import cps_accounting as cps

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,7,tzinfo=timezone.utc)
        self.row={'payment_status':'succeeded','payment_id':'verified','shipment_id':'shipment','ozon_status':'delivered'}
        self.receipt={'provider_verified':True,'all_postings_received':True,'received_at':self.now-timedelta(days=14),
                      'return_window_verified':True,'return_checked_at':self.now}
    def test_exact_boundary(self):
        self.assertEqual(cps.reward_state(self.row,self.receipt,now=self.now)['reward_rub'],200)
        self.assertEqual(cps.reward_state(self.row,self.receipt,now=self.now-timedelta(seconds=1))['reward_rub'],0)
    def test_missing_receipt_and_stale_returns_fail_closed(self):
        self.assertEqual(cps.reward_state(self.row,None,now=self.now)['state'],'EVIDENCE_PENDING')
        self.receipt['return_checked_at']=self.now-timedelta(hours=2)
        self.assertEqual(cps.reward_state(self.row,self.receipt,now=self.now)['reward_rub'],0)
    def test_return_refund_nonpickup(self):
        self.assertEqual(cps.reward_state(self.row,self.receipt,True,self.now)['state'],'REJECTED')
        self.receipt['refund_detected']=True
        self.assertEqual(cps.reward_state(self.row,self.receipt,now=self.now)['reward_rub'],0)
        self.row['ozon_status']='not_picked_up'
        self.assertEqual(cps.reward_state(self.row,None,now=self.now)['state'],'REJECTED')
    def test_last_touch_window_future_conflict(self):
        t=[{'at':self.now-timedelta(days=31),'source_id':'old'},
           {'at':self.now-timedelta(days=1),'source_id':'A'},
           {'at':self.now,'source_id':'B'}, {'at':self.now+timedelta(seconds=1),'source_id':'future'}]
        self.assertEqual(cps.choose_touch(t,self.now)['source_id'],'B')
        self.assertEqual(cps.choose_touch(t+[{'at':self.now,'source_id':'C'}],self.now),{'conflict':True})
    def test_unverified_paid(self):
        self.row['payment_status']='pending'
        self.assertEqual(cps.reward_state(self.row,self.receipt,now=self.now)['reward_rub'],0)
    def test_only_explicit_valid_source(self):
        self.assertIsNone(cps.source_id({'utm_source':'direct','utm_medium':'cps','utm_content':'fake'}))
        self.assertIsNone(cps.source_id({'utm_source':'affiliate','utm_medium':'cps','utm_content':'fake'}))

class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_receipt_not_poll_time_and_owner_excluded(self):
        from unittest.mock import AsyncMock
        c=AsyncMock();row={'order_id':1,'shipment_id':'x','ozon_status':'delivered','quantity':2}
        posts=[{'status':'delivered','order_number':'x','status_changed_at':'2026-10-01T01:00:00Z','posting_number':'a'},
               {'status':'delivered','order_number':'x','status_changed_at':'2026-10-02T01:00:00Z','posting_number':'b'}]
        await cps.record_receipt(c,row,posts)
        import json
        self.assertEqual(json.loads(c.execute.call_args.args[3])['received_at'],'2026-10-02T01:00:00+00:00')
        self.assertIn('ON CONFLICT(order_id) DO NOTHING',c.execute.call_args.args[0])
        c.reset_mock();await cps.record_receipt(c,{**row,'is_internal':True},posts)
        c.execute.assert_not_called()
        await cps.record_receipt(c,row,posts[:1]);c.execute.assert_not_called()

if __name__=='__main__':unittest.main()
