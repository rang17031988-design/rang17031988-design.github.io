import gzip,json,sys,unittest
from pathlib import Path
from starlette.requests import Request
sys.path.insert(0,str(Path(__file__).parent/'server'))
import pvz_diagnostics as pvz

class PublicResponseTests(unittest.TestCase):
    def test_gzip_preserves_public_points_and_timing(self):
        request=Request({'type':'http','method':'GET','path':'/api/ozon/points','query_string':b'city=x','headers':[(b'accept-encoding',b'gzip')],'scheme':'https','server':('api.example',443)})
        payload={'count':1,'items':[{'delivery_point_id':2660975,'full_address':'Самара','latitude':53.2,'longitude':50.2}]}
        pvz.start(request);pvz.stage(request,'points_query');response=pvz.response(request,payload)
        result=json.loads(gzip.decompress(response.body))
        self.assertEqual(result['items'],payload['items'])
        self.assertEqual(result['count'],payload['count'])
        self.assertEqual(result['pvz_request_id'],response.headers['x-pvz-request-id'])
        self.assertEqual(response.headers['content-encoding'],'gzip')
        self.assertIn('total;dur=',response.headers['server-timing'])
        self.assertEqual(response.headers['cache-control'],'no-store')
    def test_uncompressed_empty_result(self):
        request=Request({'type':'http','method':'GET','path':'/api/ozon/points','query_string':b'','headers':[],'scheme':'https','server':('api.example',443)})
        pvz.start(request);response=pvz.response(request,{'count':0,'items':[]})
        self.assertEqual(json.loads(response.body)['items'],[])
        self.assertNotIn('content-encoding',response.headers)
