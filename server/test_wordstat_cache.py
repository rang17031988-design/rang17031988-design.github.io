import unittest,json,asyncio,os
from datetime import datetime,timezone
from unittest.mock import patch
from wordstat_cache import request,scope

class Tx:
    def __init__(self,lock):self.lock=lock
    async def __aenter__(self):await self.lock.acquire()
    async def __aexit__(self,*args):self.lock.release()
class State:
    def __init__(self):self.lock=asyncio.Lock();self.cache={};self.usage=[]
class Conn:
    def __init__(self,state):self.state=state
    def transaction(self):return Tx(self.state.lock)
    async def execute(self,sql,*args):
        if 'INSERT INTO wordstat_api_cache' in sql:
            k,op,body,status,raw,at,until=args;self.state.cache[k]={'status_code':status,'raw_response':raw,'cache_until':until}
        if 'INSERT INTO wordstat_api_usage' in sql:self.state.usage.append({'status':args[2],'at':args[3]})
    async def fetchrow(self,sql,key,at):
        v=self.state.cache.get(key)
        return v if v and v['cache_until']>at else None
    async def fetchval(self,sql,at):
        if 'EXISTS' in sql:return any(v['status'] in (401,403,429,502) and v['at']>at for v in self.state.usage)
        return len(self.state.usage)
class Response:
    def __init__(self,status=200):self.status_code=status
    def json(self):return {'results':[{'date':'2026-10-02','count':7}]}
class Http:
    def __init__(self,status=200):self.calls=0;self.status=status
    async def post(self,*args,**kwargs):self.calls+=1;await asyncio.sleep(0);return Response(self.status)

class CacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'YANDEX_WORDSTAT_API_KEY':'local-test-key','YANDEX_FOLDER_ID':'local-folder'})
        self.env.start();self.addCleanup(self.env.stop)
        self.now=datetime(2026,10,5,22,tzinfo=timezone.utc)
        self.body={'phrase':'купить ручку','period':'PERIOD_DAILY','fromDate':'2026-09-01T00:00:00Z','toDate':'2026-10-05T00:00:00Z'}
    async def test_concurrent_existing_callers_share_one_provider_request(self):
        state=State();http=Http()
        a,b=await asyncio.gather(request(Conn(state),http,'dynamics',self.body,'controller',self.now),request(Conn(state),http,'dynamics',self.body,'n8n',self.now))
        self.assertEqual(http.calls,1);self.assertEqual(a.json(),b.json());self.assertTrue(a.cache_hit or b.cache_hit)
    async def test_budget_cap_does_not_fabricate_zero_demand(self):
        state=State();state.usage=[{'status':200,'at':self.now}]*64;http=Http()
        r=await request(Conn(state),http,'dynamics',self.body,'worker',self.now)
        self.assertEqual(http.calls,0);self.assertEqual(r.status_code,429);self.assertNotIn('results',r.json())
    async def test_provider_quota_opens_shared_circuit_without_repeated_charge(self):
        state=State();http=Http(429)
        first=await request(Conn(state),http,'dynamics',self.body,'worker',self.now)
        second=await request(Conn(state),http,'topRequests',{'phrase':'другой запрос'},'n8n',self.now)
        self.assertEqual(first.status_code,429);self.assertEqual(second.status_code,503);self.assertEqual(http.calls,1)
    def test_scope_never_merges_different_regions_periods_or_windows(self):
        key=scope('dynamics',self.body,'local-folder')[1]
        for changes in ({'regions':['225']},{'period':'PERIOD_WEEKLY'},{'fromDate':'2026-09-02T00:00:00Z'}):
            self.assertNotEqual(key,scope('dynamics',self.body|changes,'local-folder')[1])
        with self.assertRaises(ValueError):scope('dynamics',self.body|{'folderId':'other-account'},'local-folder')
