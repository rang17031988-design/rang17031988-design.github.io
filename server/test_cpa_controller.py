import unittest
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from profit_controller import cpa_decision,technical_paid_health,prepaid_delivery_window

class CPAGuards(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,4,tzinfo=timezone.utc)
        self.data={'strategy_verified':True,'balance':1000,'health':True,'economic_max':350,
            'paid_7d':3,'clicks_7d':10,'cac_paid':100,'observed_days':3,
            'delivery_limited':True,'attribution_complete':True}
    def decision(self,current=200,**changes):return cpa_decision(current,self.data|changes,self.now)
    def test_step_and_upper_bound(self):
        self.assertEqual(self.decision()[:2],('SET',Decimal(225)))
        self.assertEqual(self.decision(325)[:2],('SET',Decimal(350)))
        self.assertEqual(self.decision(350)[0],'HOLD')
        self.assertEqual(self.decision(99)[0],'REVIEW');self.assertEqual(self.decision(351)[0],'REVIEW')
    def test_unknown_economics_never_raise(self):self.assertEqual(self.decision(economic_max=None)[2],'economics_unknown')
    def test_funds_do_not_change_strategy(self):self.assertEqual(self.decision(balance=3.58)[:2],('WAITING_FOR_FUNDS',None))
    def test_pause_health_and_goal(self):
        for changes,state in [({'paused':True},'PAUSED'),({'health':False},'BLOCKED_FUNNEL'),({'strategy_verified':False},'BLOCKED')]:
            self.assertEqual(self.decision(**changes)[:2],(state,None))
    def test_cooldown(self):self.assertEqual(cpa_decision(200,self.data,self.now,self.now-timedelta(hours=23))[2],'24h_cooldown')
    def test_broken_funnel_not_cured_with_higher_cpa(self):self.assertEqual(self.decision(clicks_7d=30,paid_7d=0)[2],'traffic_without_verified_payments')
    def test_unprofitable_suspend_instead_of_gradual_loss(self):
        self.assertEqual(self.decision(250,economic_max=200)[:2],('SUSPEND',None))
        self.assertEqual(self.decision(economic_max=50)[:2],('SUSPEND',None))
    def test_attribution_required_for_scaling(self):self.assertEqual(self.decision(attribution_complete=False)[0],'HOLD')
    def test_empty_paid_does_not_raise(self):self.assertEqual(self.decision(paid_7d=0)[0],'HOLD')

    def test_first_ad_payment_not_required_for_readiness_but_scaling_stays_blocked(self):
        checks={k:True for k in ('backend','landing','payment_api','checkout','pvz','metrika_goal',
            'paid_server_enabled','paid_dedupe_ready','no_failed_paid_uploads','no_mass_errors')}
        self.assertTrue(technical_paid_health(checks|{'paid_delivery_proven':False}))
        self.assertFalse(technical_paid_health(checks|{'paid_dedupe_ready':False,'paid_delivery_proven':True}))
        self.assertFalse(technical_paid_health({'paid_server_enabled':True}))
        self.assertFalse(technical_paid_health({k:v for k,v in checks.items() if k!='payment_api'}))
        self.assertEqual(self.decision(economic_max=None,paid_7d=0,attribution_complete=False)[:2],('HOLD',None))

    def test_zero_paid_never_scales_for_delivery_target(self):
        # Regression: even a ready/unbilled probe with low impressions must HOLD.
        for current in (200,225,350):
            for cap in (None,350):
                for demand in (False,True):
                    for clicks in (0,10,60):
                        with self.subTest(current=current,cap=cap,demand=demand,clicks=clicks):
                            result=self.decision(current,economic_max=cap,paid_7d=0,
                                clicks_7d=clicks,prepaid_probe_ready=True,current_regime_unbilled=True,
                                demand_exists=demand,probe_economic_max=350)
                            self.assertEqual(result,('HOLD',None,'await_verified_paid_quality_before_scale'))

    def test_global_delivery_target_and_current_spend_guard(self):
        previous={'delivery_probe_policy_version':1,'delivery_probe_since':'2026-10-01T10:00:00+00:00'}
        rows=[{'Date':'2026-10-02','Impressions':500,'Clicks':30,'Cost':0},
              {'Date':'2026-10-03','Impressions':600,'Clicks':40,'Cost':0}]
        global_rows=rows+[{'Date':'2026-10-03','Impressions':3000,'Clicks':20,'Cost':0}]
        probe=prepaid_delivery_window(previous,self.now,True,rows,global_rows)
        self.assertTrue(probe['ready']);self.assertEqual(probe['global_impressions_average_day'],2050)
        self.assertFalse(prepaid_delivery_window(previous,self.now,True,rows,
            global_rows+[{'Date':'2026-10-03','Impressions':40000,'Clicks':0,'Cost':0}])['ready'])
        self.assertFalse(prepaid_delivery_window(previous,self.now,True,
            rows+[{'Date':'2026-10-03','Impressions':0,'Clicks':0,'Cost':1}],global_rows)['ready'])

    def test_probe_cannot_bypass_funnel_pause_or_cooldown(self):
        data=self.data|{'economic_max':None,'paid_7d':0,'prepaid_probe_ready':True}
        self.assertEqual(cpa_decision(200,data,self.now,self.now-timedelta(hours=23))[2],'24h_cooldown')
        for changes,state in [({'health':False},'BLOCKED_FUNNEL'),({'paused':True},'PAUSED'),
                              ({'strategy_verified':False},'BLOCKED'),({'clicks_7d':30},'HOLD')]:
            self.assertEqual(cpa_decision(200,data|changes,self.now)[0],state)

    def test_probe_needs_full_days_current_regime_and_low_delivery(self):
        previous={'delivery_probe_policy_version':1,'delivery_probe_since':'2026-10-01T10:00:00+00:00'}
        rows=[{'Date':'2026-10-01','Impressions':9999,'Clicks':99},
              {'Date':'2026-10-02','Impressions':5,'Clicks':0},
              {'Date':'2026-10-03','Impressions':8,'Clicks':1},
              {'Date':'2026-10-04','Impressions':999,'Clicks':40}]
        probe=prepaid_delivery_window(previous,self.now,True,rows)
        self.assertTrue(probe['ready']);self.assertEqual(probe['impressions'],13)
        self.assertFalse(prepaid_delivery_window({},self.now,True,rows)['ready'])
        self.assertFalse(prepaid_delivery_window(previous,self.now,False,rows)['ready'])
        self.assertFalse(prepaid_delivery_window(previous,self.now,True,rows+
            [{'Date':'2026-10-03','Impressions':100,'Clicks':0}])['ready'])
        self.assertFalse(prepaid_delivery_window(previous,self.now,True,rows+
            [{'Date':'2026-10-03','Impressions':0,'Clicks':9}])['ready'])


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ended_period_continues_same_campaign_at_new_minimum(self):
        import copy,os
        from profit_controller import Controller
        from unittest.mock import AsyncMock,patch
        now=datetime(2026,10,6,tzinfo=timezone.utc)
        checks={k:True for k in ('backend','landing','payment_api','checkout','pvz','metrika_goal',
            'paid_server_enabled','paid_dedupe_ready','no_failed_paid_uploads','no_mass_errors')}
        campaign={'Id':715029848,'State':'ENDED','Status':'ACCEPTED','EndDate':'2026-10-05',
            'UnifiedCampaign':{'BiddingStrategy':{'Search':{'BiddingStrategyType':'SERVING_OFF'},
            'Network':{'BiddingStrategyType':'PAY_FOR_CONVERSION','PayForConversion':{
                'Cpa':100000000,'GoalId':666936854,'BudgetType':'CUSTOM_PERIOD_BUDGET',
                'CustomPeriodBudget':{'EndDate':'2026-10-05'}}}},'CounterIds':{'Items':[112544007]}}}
        saved=copy.deepcopy(campaign);saved.update(State='ON',EndDate=None)
        saved['UnifiedCampaign']['BiddingStrategy']['Network']['PayForConversion'].update(
            Cpa=200000000,BudgetType='WEEKLY_BUDGET',WeeklySpendLimit=1000000000,CustomPeriodBudget=None)
        worker=Controller(None,None);worker.state=AsyncMock(side_effect=[
            {'state':'BLOCKED','reason':'campaign_changed_or_owner_paused_no_auto_resume'},
            None,{'health_checks':checks,'checked_at':now.isoformat()}])
        worker.put=AsyncMock();worker.notify=AsyncMock();worker.cpa_balance=AsyncMock(return_value=Decimal(2800))
        worker.api=AsyncMock(side_effect=[{'Campaigns':[campaign]},{'UpdateResults':[{'Id':715029848}]},{'Campaigns':[saved]}])
        db=AsyncMock();db.fetchval.return_value=1
        with patch.dict(os.environ,{'RSYA_CONTINUE_AFTER_20261005':'true','PROFIT_CONTROLLER_LIVE':'true'}):
            await worker.rsya_continuation(db,now)
        self.assertEqual(worker.api.call_count,3)
        update=worker.api.call_args_list[1].args[2]['Campaigns'][0]
        self.assertEqual(update['Id'],715029848)
        self.assertIsNone(update['EndDate'])
        pay=update['UnifiedCampaign']['BiddingStrategy']['Network']['PayForConversion']
        self.assertEqual(pay['Cpa'],200000000)
        self.assertEqual(pay['GoalId'],666936854)
        self.assertEqual(pay['WeeklySpendLimit'],1000000000)
        self.assertTrue(any(call.args[1]=='rsya_continuation_applied' for call in worker.put.call_args_list))

    async def test_current_accepted_period_is_not_mutated(self):
        from profit_controller import Controller
        from unittest.mock import AsyncMock,patch
        import os
        worker=Controller(None,None);worker.state=AsyncMock(return_value=None);worker.put=AsyncMock();worker.api=AsyncMock()
        with patch.dict(os.environ,{'RSYA_CONTINUE_AFTER_20261005':'true'}):
            await worker.rsya_continuation(None,datetime(2026,10,4,tzinfo=timezone.utc))
        worker.api.assert_not_called()
        self.assertEqual(worker.put.call_args.args[2]['state'],'READY')
        self.assertEqual(worker.put.call_args.args[2]['weekly_budget_ex_vat_rub'],1000)
    async def test_owner_pause_prevents_scheduled_continuation(self):
        from profit_controller import Controller
        from unittest.mock import AsyncMock,patch
        import os
        worker=Controller(None,None);worker.state=AsyncMock(side_effect=[{'state':'READY'},True]);worker.api=AsyncMock()
        with patch.dict(os.environ,{'RSYA_CONTINUE_AFTER_20261005':'true'}):
            await worker.rsya_continuation(None,datetime(2026,10,6,tzinfo=timezone.utc))
        worker.api.assert_not_called()
