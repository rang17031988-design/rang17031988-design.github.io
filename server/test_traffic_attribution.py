import unittest
from traffic_attribution import classify,source_report

class AttributionTests(unittest.TestCase):
    def test_paid_evidence_and_placement(self):
        self.assertFalse(classify({'utm_campaign':'714566814'})['paid_evidence'])
        self.assertEqual(classify({'yclid':'123','utm_campaign':'715029848'})['primary_attribution'],'PAID_RSYA')
        self.assertEqual(classify({'yclid':'123'})['primary_attribution'],'UNKNOWN')
        self.assertTrue(classify({'yclid':'123'})['paid_evidence'])
    def test_organic_is_not_paid_or_lookalike(self):
        self.assertEqual(classify({},'www.yandex.ru')['primary_attribution'],'ORGANIC_SEARCH')
        self.assertEqual(classify({},'yandex.ru.evil.example')['primary_attribution'],'REFERRAL_OTHER')
        self.assertFalse(classify({'utm_source':'yandex','utm_medium':'organic'})['paid_evidence'])
    def test_owned_external_and_owner(self):
        for campaign,expected in [('content_engine','TELEGRAM_OWNED'),('external_groups','TELEGRAM_EXTERNAL')]:
            self.assertEqual(classify({'utm_source':'telegram','utm_medium':'organic','utm_campaign':campaign})['primary_attribution'],expected)
        self.assertEqual(classify({'yclid':'123'},traffic_class='owner')['primary_attribution'],'OWNER')
    def test_single_payment_one_channel_and_unknown_revenue(self):
        a={'yclid':'123','utm_campaign':'714566814'}
        groups=source_report([{'attribution':a}], [{'attribution':a,'payment_status':'succeeded','amount':1200},{'attribution':{},'payment_status':'pending','amount':1200}])
        self.assertEqual(sum(e['paid'] for e in groups.values()),1)
        self.assertEqual(groups['PAID_SEARCH']['revenue'],1200)
        self.assertEqual(groups['UNKNOWN']['revenue'],0)
