import os
import unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from profit_funnel import window,safe_client,device,channel,distribution,funnel,money,ProfitFunnel

class ReportingTests(unittest.TestCase):
    def test_full_calendar_moscow(self):
        now=datetime(2026,10,3,22,0,tzinfo=timezone.utc)
        start,end=window('yesterday',now)
        self.assertEqual(start.isoformat(),'2026-10-03T00:00:00+03:00')
        self.assertEqual(end.isoformat(),'2026-10-04T00:00:00+03:00')
        start,end=window('week',datetime(2026,10,5,6,10,tzinfo=timezone.utc))
        self.assertEqual(start.date().isoformat(),'2026-09-28')
        self.assertEqual(end.date().isoformat(),'2026-10-05')

    def test_browser_and_device_priority(self):
        self.assertEqual(device('Android Mobile Chrome Safari YaBrowser'),('MOBILE','Android','Yandex'))
        self.assertEqual(device('iPhone Safari'),('MOBILE','iOS','Safari'))
        self.assertEqual(device('iPad Safari'),('TABLET','iPadOS','Safari'))
        self.assertEqual(device('Android Chrome'),('TABLET','Android','Chrome'))

    def test_channel_does_not_guess_search_vs_rsya(self):
        self.assertEqual(channel({'utm_campaign':'715029848'}),'UNKNOWN')
        self.assertEqual(channel({'utm_campaign':'714566814'}),'UNKNOWN')
        self.assertEqual(channel({'yclid':'123','utm_campaign':'715029848'}),'YANDEX_RSYA')
        self.assertEqual(channel({'utm_source':'yandex','utm_medium':'cpc'}),'OTHER_YANDEX_PAID')
        self.assertEqual(channel({},'google.com'),'SEO_ORGANIC')

    def test_owner_both_identifiers_and_private_chat(self):
        worker=ProfitFunnel(None,None)
        with patch.dict(os.environ,{'OWNER_CHAT_ID':'42'}):
            self.assertTrue(worker.owner_allowed({'message':{'chat':{'id':42,'type':'private'},'from':{'id':42}}}))
            for update in [{'message':{'chat':{'id':42,'type':'private'},'from':{'id':9}}},
                           {'message':{'chat':{'id':42,'type':'group'},'from':{'id':42}}},{}]:
                self.assertFalse(worker.owner_allowed(update))

    def test_client_cannot_fake_paid_or_submit_pii(self):
        batch={'session_id':'a2c37719-6dc1-46bb-8b0b-6fa456fe3d45','events':[{'event_id':'14917ffb-35d5-494e-a930-793df352ce61','name':'PAYMENT_SUCCESS'}]}
        with self.assertRaises(ValueError):safe_client(batch,'Android Mobile')
        batch['events'][0].update(name='CONTACTS_COMPLETED',phone='+79879218327',email='private@example.com')
        batch['attribution']={'email':'private@example.com','utm_term':'private@example.com','client_id':'1234'}
        result=safe_client(batch,'Android Mobile')
        self.assertEqual(result['attribution']['client_id'],'1234')
        self.assertNotIn('email',result['attribution']);self.assertNotIn('utm_term',result['attribution'])
        self.assertEqual(result['events'][0][3],{})

    def test_funnel_requires_ordered_pair_and_low_sample(self):
        t=datetime.now(timezone.utc)
        sessions=[{'session_id':'a','device_type':'MOBILE','os':'Android','browser':'Yandex','channel':'DIRECT','new_visitor':True}]
        events=[{'session_id':'a','name':'PVZ_PICKER_OPEN','occurred_at':t,'payload':{}},
                {'session_id':'a','name':'PVZ_SELECTED','occurred_at':t-timedelta(seconds=1),'payload':{}}]
        result=funnel(events,sessions)
        drop=next(x for x in result['drops'] if x['from']=='PVZ_PICKER_OPEN' and x['to']=='PVZ_LOADED')
        self.assertEqual(drop['end'],0)
        self.assertIsNone(result['biggest_drop'])
        self.assertEqual(result['time_to_action']['PVZ_PICKER_OPEN→PVZ_SELECTED']['n'],0)

    def test_distribution_never_invents_p75_or_zero_median(self):
        self.assertIsNone(distribution([])['median'])
        self.assertIsNone(distribution([1,2,3])['p75'])
        self.assertEqual(distribution(range(1,21))['p75'],15)

    def test_cogs_tax_received_and_unknown_logistics(self):
        rows=[{'order_id':1,'quantity':1,'amount':800,'payment_status':'succeeded','ozon_status':'delivered','yookassa':15.2,'ozon':None,'returns_other':0}]
        result=money(rows,100,{})
        self.assertEqual(result['cogs_received'],230)
        self.assertEqual(result['tax_received'],48)
        self.assertAlmostEqual(result['profit_before_unknown'],406.8)
        self.assertIsNone(result['final_net_profit'])
        rows[0]['ozon_status']='on_way'
        self.assertEqual(money(rows,100,{})['tax_received'],0)

    def test_return_condition_needs_confirmation(self):
        row={'order_id':1,'quantity':1,'amount':800,'payment_status':'succeeded','ozon_status':'returned','yookassa':15.2,'ozon':80,'returns_other':None}
        result=money([row],0,{1:{'has_return':True}})
        self.assertEqual(result['writeoff'],0)
        self.assertIsNone(result['ozon_return_actual'])
        self.assertEqual(money([row],0,{1:{'has_return':True,'condition':'resellable','return_logistics':70}})['writeoff'],0)
        self.assertEqual(money([row],0,{1:{'has_return':True,'condition':'damaged','return_logistics':70}})['writeoff'],230)

    def test_unpaid_does_not_poison_actual_costs(self):
        rows=[{'order_id':1,'quantity':1,'amount':800,'payment_status':'succeeded','ozon_status':'delivered','yookassa':15.2,'ozon':80,'returns_other':0},
              {'order_id':2,'quantity':1,'amount':800,'payment_status':'pending','ozon_status':None,'yookassa':None,'ozon':None,'returns_other':None}]
        self.assertAlmostEqual(money(rows,100,{})['final_net_profit'],326.8)

    def test_return_never_double_counts_cogs_and_writeoff(self):
        rows=[{'order_id':1,'quantity':1,'amount':800,'payment_status':'succeeded','ozon_status':'delivered','yookassa':15.2,'ozon':80,'returns_other':None}]
        result=money(rows,0,{1:{'has_return':True,'condition':'damaged','return_logistics':70,'refund_rub':800}})
        self.assertEqual(result['cogs_received'],0)
        self.assertEqual(result['writeoff'],230)
        self.assertEqual(result['tax_received'],0)
        result=money(rows,0,{1:{'has_return':True,'condition':'resellable','return_logistics':70,'refund_rub':None}})
        self.assertEqual(result['writeoff'],0)
        self.assertIsNone(result['final_net_profit'])

    def test_paid_fee_is_expense_before_receipt(self):
        r={'order_id':1,'quantity':1,'amount':800,'payment_status':'succeeded','ozon_status':'on_way','yookassa':15.2,'ozon':None,'returns_other':0}
        self.assertAlmostEqual(money([r],59.31,{})['profit_before_unknown'],-74.51)
        self.assertEqual(money([r],59.31,{})['tax_received'],0)

    def test_refunds_outside_cohort_are_not_deducted(self):
        r={'order_id':1,'quantity':1,'amount':800,'payment_status':'succeeded','ozon_status':'delivered','yookassa':15.2,'ozon':80,'returns_other':0}
        self.assertAlmostEqual(money([r],100,{2:{'has_return':True,'refund_rub':800}})['final_net_profit'],326.8)

    def test_not_picked_up_ad_cost_is_unknown_without_attribution(self):
        r={'order_id':1,'quantity':1,'amount':800,'payment_status':'succeeded','ozon_status':'returned','yookassa':15.2,'ozon':80,'returns_other':None}
        d={1:{'has_return':True,'not_picked_up':True,'condition':'resellable','return_logistics':70,'refund_rub':800}}
        loss=money([r],0,d)['not_picked_up_loss'][0]
        self.assertIsNone(loss['loss_rub'])
        self.assertAlmostEqual(loss['known_non_ad_costs_rub'],165.2)

if __name__=='__main__':unittest.main()
