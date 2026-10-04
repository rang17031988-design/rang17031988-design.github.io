import unittest
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from profit_controller import cpa_decision

class CPAGuards(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,4,tzinfo=timezone.utc)
        self.data={'strategy_verified':True,'balance':1000,'health':True,'economic_max':350,
            'paid_7d':3,'clicks_7d':10,'cac_paid':100,'observed_days':3,
            'delivery_limited':True,'attribution_complete':True}
    def decision(self,current=100,**changes):return cpa_decision(current,self.data|changes,self.now)
    def test_step_and_upper_bound(self):
        self.assertEqual(self.decision()[:2],('SET',Decimal(125)))
        self.assertEqual(self.decision(325)[:2],('SET',Decimal(350)))
        self.assertEqual(self.decision(350)[0],'HOLD')
        self.assertEqual(self.decision(99)[0],'REVIEW');self.assertEqual(self.decision(351)[0],'REVIEW')
    def test_unknown_economics_never_raise(self):self.assertEqual(self.decision(economic_max=None)[2],'economics_unknown')
    def test_funds_do_not_change_strategy(self):self.assertEqual(self.decision(balance=3.58)[:2],('WAITING_FOR_FUNDS',None))
    def test_pause_health_and_goal(self):
        for changes,state in [({'paused':True},'PAUSED'),({'health':False},'BLOCKED_FUNNEL'),({'strategy_verified':False},'BLOCKED')]:
            self.assertEqual(self.decision(**changes)[:2],(state,None))
    def test_cooldown(self):self.assertEqual(cpa_decision(100,self.data,self.now,self.now-timedelta(hours=23))[2],'24h_cooldown')
    def test_broken_funnel_not_cured_with_higher_cpa(self):self.assertEqual(self.decision(clicks_7d=30,paid_7d=0)[2],'traffic_without_verified_payments')
    def test_unprofitable_suspend_instead_of_gradual_loss(self):
        self.assertEqual(self.decision(250,economic_max=200)[:2],('SUSPEND',None))
        self.assertEqual(self.decision(economic_max=50)[:2],('SUSPEND',None))
    def test_attribution_required_for_scaling(self):self.assertEqual(self.decision(attribution_complete=False)[0],'HOLD')
    def test_empty_paid_does_not_raise(self):self.assertEqual(self.decision(paid_7d=0)[0],'HOLD')
