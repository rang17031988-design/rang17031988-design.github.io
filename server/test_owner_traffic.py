import unittest,uuid,os
from unittest.mock import patch
from owner_traffic import mint,valid,classify,ACTIVATION_TTL
from profit_funnel import safe_client,funnel

class OwnerTrafficTests(unittest.TestCase):
    def test_activation_scope_expiry_and_tampering(self):
        token=mint('test-key','activate',1000)
        self.assertTrue(valid(token,'test-key','activate',1001))
        self.assertFalse(valid(token,'wrong-key','activate',1001))
        self.assertFalse(valid(token,'test-key','owner',1001))
        self.assertFalse(valid(token+'x','test-key','activate',1001))
        self.assertFalse(valid(token,'test-key','activate',1001+ACTIVATION_TTL))
    def test_owner_and_synthetic_are_separate_and_excluded(self):
        marker=mint('test-key','owner')
        batch={'session_id':str(uuid.uuid4()),'owner_marker':marker,'events':[]}
        with patch.dict(os.environ,{'PII_INTERNAL_KEY':'test-key'}):
            owner=safe_client(batch,'Chrome')
            synthetic=safe_client(batch|{'is_test':True},'Chrome')
            normal=safe_client(batch|{'owner_marker':'forged'},'Chrome')
        self.assertEqual(owner['traffic_class'],'owner')
        self.assertTrue(owner['is_internal']);self.assertFalse(owner['is_test'])
        self.assertEqual(synthetic['traffic_class'],'internal_test')
        self.assertEqual(normal['traffic_class'],'customer')
        self.assertNotIn('owner_marker',str(owner))
        self.assertEqual(funnel([], [owner,synthetic])['sessions'],0)
    def test_marker_cannot_activate_or_grant_access(self):
        marker=mint('test-key','owner')
        self.assertFalse(valid(marker,'test-key','activate'))
        self.assertEqual(classify({'traffic_class':'owner'},'test-key'),'customer')
