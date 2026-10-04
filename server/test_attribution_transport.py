import unittest,importlib.util,json,uuid
from pathlib import Path
spec=importlib.util.spec_from_file_location('analytics_inbox_touch',Path(__file__).parents[1]/'cloud/handles-pii/analytics_inbox.py')
transport=importlib.util.module_from_spec(spec);spec.loader.exec_module(transport)
class TransportTests(unittest.TestCase):
    def test_long_real_numeric_id_is_not_mistaken_for_phone(self):
        value='1234567890712345678901234567890'
        result,writes=self.send({'yclid':value,'first_yclid':value})
        self.assertEqual(result['statusCode'],202)
        self.assertEqual(writes[0]['attribution']['yclid'],value)
        self.assertEqual(writes[0]['attribution']['first_yclid'],value)
    def send(self,attr,event='SITE_SESSION'):
        payload={'session_id':str(uuid.uuid4()),'batch_id':str(uuid.uuid4()),'attribution':attr,'events':[
            {'event_id':str(uuid.uuid4()),'name':event,'timestamp':'2026-10-04T20:00:00Z'}],'is_test':True}
        writes=[]
        result=transport.ingest({'headers':{'origin':transport.ORIGIN},'body':json.dumps(payload)},lambda q,p:writes.append(json.loads(p['$payload'])))
        return result,writes
    def test_touches_survive_existing_inbox(self):
        result,writes=self.send({'first_source':'yandex','first_yclid':'123','last_source':'telegram','last_campaign':'external_groups','source_token':'source_test_123'})
        self.assertEqual(result['statusCode'],202)
        self.assertEqual(writes[0]['attribution']['first_yclid'],'123')
        self.assertEqual(writes[0]['attribution']['last_campaign'],'external_groups')
    def test_paid_and_private_fields_still_rejected(self):
        self.assertEqual(self.send({},'PAYMENT_SUCCESS')[0]['statusCode'],400)
        self.assertEqual(self.send({'phone':'+79879218327'})[0]['statusCode'],400)
        result,writes=self.send({'first_source':'user@example.com','first_referrer_host':'invalid/host'})
        self.assertEqual(result['statusCode'],202)
        self.assertEqual(writes[0]['attribution'],{})
