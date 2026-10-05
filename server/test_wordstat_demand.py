import unittest
from unittest.mock import AsyncMock, patch
from datetime import date,timedelta,datetime,timezone
from wordstat_demand import normalized_history,demand_metrics,coverage_proxy,cluster_for_term,joined_metrics

class DemandTests(unittest.TestCase):
    def test_recent_lag_permits_only_observed_demand_without_filling_gaps(self):
        from wordstat_demand import recent_demand_evidence,CLUSTERS
        today=date(2026,10,6)
        clusters=[{'cluster_id':c[0],'latest_provider_date':'2026-10-03',
                   'avg7':10,'avg30':12,'wordstat_yesterday':None,'complete':False} for c in CLUSTERS]
        state={'clusters':clusters,'state':'WORDSTAT_DATA_DEGRADED'}
        self.assertEqual(recent_demand_evidence(state,today),(True,'RECENT_OBSERVED_PROVIDER_LAG'))
        self.assertTrue(all(x['wordstat_yesterday'] is None for x in clusters))
        self.assertEqual(state['state'],'WORDSTAT_DATA_DEGRADED')
        self.assertFalse(recent_demand_evidence(state,today+timedelta(days=1))[0])
        self.assertFalse(recent_demand_evidence({'clusters':clusters[:-1]},today)[0])
        clusters[0]['avg30']=None
        self.assertFalse(recent_demand_evidence(state,today)[0])
    def test_provider_labels_are_not_timezone_shifted(self):
        self.assertEqual(normalized_history({'results':[{'date':'2026-10-02T00:00:00Z','count':'9'}]}),{'2026-10-02':9})
    def test_missing_day_is_unknown_not_zero(self):
        today=date(2026,10,4)
        rows={(today-timedelta(days=i)).isoformat():10 for i in range(1,36)}
        rows.pop('2026-10-03')
        m=demand_metrics(rows,today)
        self.assertIsNone(m['wordstat_yesterday']);self.assertEqual(m['avg7'],10);self.assertFalse(m['complete'])
        self.assertEqual(m['average_window_end'],'2026-10-02')
        self.assertEqual(coverage_proxy(m,10,1,True)['coverage_status'],'UNKNOWN')
    def test_complete_average_and_growth(self):
        today=date(2026,10,4)
        rows={(today-timedelta(days=i)).isoformat():20 if i<=7 else 10 for i in range(1,36)}
        m=demand_metrics(rows,today)
        self.assertEqual(m['avg7'],20);self.assertEqual(m['trend7'],1)
        self.assertIsNone(m['wordstat_today']);self.assertTrue(m['complete'])
        p=coverage_proxy(m,14,1,True)
        self.assertEqual(p['coverage_proxy'],.1);self.assertEqual(p['coverage_status'],'UNDERDELIVERY')
        self.assertNotIn('lost_impressions',p)
    def test_zero_and_negative(self):
        self.assertEqual(normalized_history({'results':[{'date':'2026-10-01T00:00:00Z'}]}),{'2026-10-01':0})
        with self.assertRaises(ValueError):normalized_history({'results':[{'date':'2026-10-01','count':'-1'}]})

    def test_exclusive_cluster_and_unknown_query(self):
        self.assertEqual(cluster_for_term('купить универсальную ручку'),'universal')
        self.assertEqual(cluster_for_term('сменная ручка сковороды'),'replacement')
        self.assertEqual(cluster_for_term('купить чапельник'),'chapelnik')
        self.assertIsNone(cluster_for_term('сковорода'))

    def test_join_dedupe_paid_evidence_and_unknown_costs(self):
        demand={'clusters':[{'cluster_id':'buy','complete':True,'avg7':100,'average_window_end':'2026-10-03'}]}
        a={'utm_source':'yandex','utm_medium':'cpc','utm_campaign':'714566814','utm_term':'купить ручку'}
        sessions=[{'attribution':a},{'attribution':a,'is_internal':True},
            {'attribution':{'utm_source':'google','utm_medium':'organic'}}]
        order={'order_id':5,'payment_status':'succeeded','amount':1200,'quantity':1,'attribution':a}
        rows=[order,dict(order),{**order,'order_id':6,'payment_status':'pending'},
            {**order,'order_id':7,'attribution':{'utm_campaign':'714566814','utm_term':'купить ручку'}}]
        result=joined_metrics(demand,[{'Criterion':'купить ручку','Clicks':4,'Impressions':10,'Cost':25}],sessions,rows,
            datetime(2026,9,27,tzinfo=timezone.utc),datetime(2026,10,4,tzinfo=timezone.utc),True)
        item=result['clusters'][0]['joined']
        self.assertEqual(item['verified_paid'],1);self.assertEqual(item['revenue_rub'],1200)
        self.assertEqual(item['paid_sessions'],1);self.assertEqual(item['cac_paid_rub'],25)
        self.assertIsNone(item['contribution_before_ads_rub']);self.assertIsNone(item['organic_sessions'])
        self.assertEqual(result['joined_unassigned']['organic_sessions'],1)
        self.assertIsNone(result['clusters'][0]['opportunity_score'])
        self.assertEqual(result['mode'],'DRY_RUN')
        self.assertNotIn('joined',demand['clusters'][0])

    def test_mismatched_period_withholds_coverage(self):
        result=joined_metrics({'clusters':[{'cluster_id':'buy','complete':True,'avg7':100,'average_window_end':'2026-10-01'}]},[],[],[],
            datetime(2026,9,29,tzinfo=timezone.utc),datetime(2026,10,5,tzinfo=timezone.utc),True)
        self.assertEqual(result['clusters'][0]['coverage_status'],'UNKNOWN')
        self.assertIn('fresh_aligned_demand_window',result['clusters'][0]['missing_score_inputs'])

class RuntimeJoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_join_persists_and_contains_failure(self):
        from profit_controller import Controller
        controller = Controller.__new__(Controller)
        controller.put = AsyncMock()
        controller.order_cohort = AsyncMock(return_value=[])
        connection = AsyncMock()
        connection.fetch.return_value = []
        now = datetime(2026,10,5,tzinfo=timezone.utc)
        with patch('profit_controller.wordstat_demand.joined_metrics', return_value={'joined_at':now.isoformat()}) as join:
            await controller.join_wordstat(connection, {}, [], now, now, {})
            join.assert_called_once()
            self.assertEqual(controller.put.await_args_list[-1].args[1], 'wordstat_join_health')
            self.assertEqual(controller.put.await_args_list[-1].args[2]['state'], 'OBSERVED')
        connection.fetch.side_effect = RuntimeError('private response must not be logged')
        await controller.join_wordstat(connection, {}, [], now, now, {})
        self.assertEqual(controller.put.await_args.args[2]['error'], 'RuntimeError')
        self.assertNotIn('private', str(controller.put.await_args.args[2]))

    def test_active_cpa_path_calls_join(self):
        import inspect
        from profit_controller import Controller
        source = inspect.getsource(Controller.cpa_monitor)
        self.assertIn('await self.join_wordstat(c, demand, stats, cost_start, now, checks)', source)
        self.assertIn("if side == 'Search':", source)
