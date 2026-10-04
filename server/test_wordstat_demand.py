import unittest
from datetime import date,timedelta
from wordstat_demand import normalized_history,demand_metrics,coverage_proxy

class DemandTests(unittest.TestCase):
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
