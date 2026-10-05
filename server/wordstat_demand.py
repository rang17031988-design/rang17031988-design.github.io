"""Official Wordstat signal in the existing controller; no advertising writes.

Representative query counts overlap and MUST NOT be summed or interpreted as
available advertising impressions. Missing provider days remain unknown.
"""
import json, os, re, copy
from datetime import date, timedelta
from zoneinfo import ZoneInfo

BASE = 'https://searchapi.api.cloud.yandex.net/v2/wordstat'
MSK = ZoneInfo('Europe/Moscow')
CLUSTERS = (
    ('buy', 'Купить съёмную ручку', 'купить съемную ручку для сковороды', 1.0),
    ('universal', 'Универсальная ручка', 'универсальная ручка для сковороды', .95),
    ('replacement', 'Сменная ручка', 'сменная ручка для сковороды', .9),
    ('broken', 'Сломанная ручка', 'сломалась ручка сковороды', .85),
    ('lost', 'Потерянная ручка', 'потерялась ручка сковороды', .8),
    ('chapelnik', 'Чапельник', 'чапельник для сковороды', .8),
    ('holder', 'Сковорододержатель', 'сковорододержатель', .8),
    ('ukhvat', 'Ухват', 'ухват для сковороды', .8),
)


def normalized_history(response):
    result = {}
    for row in response.get('results', []):
        # Provider daily labels are calendar dates, not instants to shift to MSK.
        day = str(row.get('date', ''))[:10]
        # Protobuf JSON omits default-valued scalar fields: omitted count is 0.
        count = int(row.get('count', 0))
        if count < 0: raise ValueError('negative_count')
        result[day] = count
    return result


def demand_metrics(history, today):
    def sample(n, end):
        days = [(end-timedelta(days=x)).isoformat() for x in range(n)]
        return sum(history[d] for d in days)/n if all(d in history for d in days) else None
    latest = max(history, default=None)
    yesterday = today-timedelta(days=1)
    anchor=date.fromisoformat(latest) if latest else yesterday
    avg7, avg30 = sample(7, anchor), sample(30, anchor)
    prior7 = sample(7, anchor-timedelta(days=7))
    a, b = history.get(yesterday.isoformat()), history.get((yesterday-timedelta(days=1)).isoformat())
    return {'wordstat_today': history.get(today.isoformat()), 'wordstat_yesterday': a,
        'avg7': avg7, 'avg30': avg30, 'trend1': (a/b-1) if a is not None and b else None,
        'trend7': (avg7/prior7-1) if avg7 is not None and prior7 else None,
        'latest_provider_date': latest, 'average_window_end':anchor.isoformat(),
        'complete': avg30 is not None and a is not None}


def coverage_proxy(demand, impressions, clicks, healthy):
    # Dimensionless delivery diagnostic, NOT a count of lost impressions.
    if not demand.get('complete') or not healthy:
        return {'coverage_proxy': None, 'coverage_status': 'UNKNOWN'}
    avg = demand['avg7']
    proxy = impressions/(avg*7) if avg else None
    return {'coverage_proxy': round(proxy, 4) if proxy is not None else None,
        'coverage_status': 'UNDERDELIVERY' if avg and impressions < 100 and clicks < 10 else 'OBSERVED',
        'interpretation': 'DEMAND COVERAGE PROXY; overlapping searches are not Direct inventory'}


def cluster_for_term(term):
    """Exclusive representative intent, never infer a query from a landing visit."""
    term = re.sub(r'[^а-яa-z0-9 ]', ' ', str(term or '').lower().replace('ё', 'е'))
    for cid, pattern in (('chapelnik', r'чапельник'), ('holder', r'сковорододержател'),
            ('ukhvat', r'ухват'), ('broken', r'слом|полом'), ('lost', r'потер'),
            ('replacement', r'сменн|запасн|замен'), ('universal', r'универсальн')):
        if re.search(pattern, term): return cid
    if re.search(r'куп(ить|лю|и)|покуп', term) and re.search(r'ручк', term): return 'buy'
    return None


def joined_metrics(demand, direct, sessions, orders, start, end, healthy=False):
    """Join an explicit observed window. This function cannot write ads or goals.

    Organic search terms are unavailable in our privacy-limited instrumentation;
    keep those sessions unassigned instead of inventing a cluster or conversion.
    """
    import traffic_attribution
    result = copy.deepcopy(demand)
    totals = {cid: {'impressions': 0, 'clicks': 0, 'spend_rub': 0.,
        'paid_sessions': 0, 'verified_paid': 0, 'revenue_rub': 0., 'margins': []} for cid, *_ in CLUSTERS}
    unassigned = {'direct_clicks': 0, 'paid_sessions': 0, 'organic_sessions': 0, 'verified_paid': 0}
    def attr(row):
        a = row.get('attribution') or {}
        return json.loads(a) if isinstance(a, str) else a
    for row in direct:
        cid = cluster_for_term(row.get('Criterion'))
        if not cid:
            unassigned['direct_clicks'] += row['Clicks']; continue
        item = totals[cid]
        for dest, key in (('impressions','Impressions'), ('clicks','Clicks'), ('spend_rub','Cost')):
            item[dest] += row[key]
    for row in sessions:
        if row.get('is_test') or row.get('is_internal') or row.get('traffic_class','customer') != 'customer': continue
        a = attr(row); source = traffic_attribution.classify(a, a.get('referrer_host',''))
        if source['primary_attribution'] == 'ORGANIC_SEARCH': unassigned['organic_sessions'] += 1
        if source['primary_attribution'] != 'PAID_SEARCH': continue
        cid = cluster_for_term(a.get('utm_term'))
        if cid: totals[cid]['paid_sessions'] += 1
        else: unassigned['paid_sessions'] += 1
    seen = set()
    for row in orders:
        oid = row.get('order_id')
        if oid is None or oid in seen or row.get('is_test') or row.get('is_internal') or row.get('payment_status') != 'succeeded': continue
        seen.add(oid); a = attr(row)
        if traffic_attribution.classify(a, a.get('referrer_host',''))['primary_attribution'] != 'PAID_SEARCH': continue
        cid = cluster_for_term(a.get('utm_term'))
        if not cid: unassigned['verified_paid'] += 1; continue
        item = totals[cid]; item['verified_paid'] += 1; item['revenue_rub'] += float(row['amount'])
        margin = None
        if row.get('ozon_status') == 'delivered' and not row.get('return_pending') and not row.get('return_received') and all(row.get(k) is not None for k in ('yookassa','ozon','returns_other')):
            margin = float(row['amount'])*.94 - row['quantity']*230 - sum(float(row[k]) for k in ('yookassa','ozon','returns_other'))
        item['margins'].append(margin)
    for cluster in result.get('clusters', []):
        item = totals[cluster['cluster_id']]; margins = item.pop('margins')
        paid, visits = item['verified_paid'], item['paid_sessions']
        item.update(paid_cr=paid/visits if visits else None, cac_paid_rub=item['spend_rub']/paid if paid else None,
            contribution_before_ads_rub=sum(margins) if margins and all(x is not None for x in margins) else None,
            organic_sessions=None, organic_reason='Search query unavailable; aggregate remains unassigned')
        # Wordstat historical week and observed Direct window must actually match.
        anchor = cluster.get('average_window_end')
        aligned = bool(anchor and start.date() == date.fromisoformat(anchor)-timedelta(days=6)
            and end.date() == date.fromisoformat(anchor)+timedelta(days=1)
            and start.hour == end.hour == 0 and start.minute == end.minute == 0
            and start.second == end.second == 0 and start.microsecond == end.microsecond == 0)
        cluster.update(joined=item, observed_window={'start':start.isoformat(),'end_exclusive':end.isoformat(),
            'wordstat_week_aligned':aligned}, **coverage_proxy(cluster,item['impressions'],item['clicks'],healthy and aligned))
        missing = []
        if not cluster.get('complete') or not aligned: missing.append('fresh_aligned_demand_window')
        if not healthy: missing.append('health')
        if visits < 20 or paid < 3: missing.append('conversion_sample')
        if item['contribution_before_ads_rub'] is None: missing.append('confirmed_margin')
        # Landing quality/ranking is not proven merely because the URL exists.
        missing.append('landing_quality')
        cluster.update(missing_score_inputs=missing, opportunity_score=None,
            confidence='LOW_SAMPLE' if visits < 20 else 'INSUFFICIENT_CONFIRMED_INPUTS', action='RECOMMEND_ONLY')
    result.update(joined_at=end.isoformat(), joined_unassigned=unassigned, mode='DRY_RUN',
        score_reason='Score withheld until fresh aligned demand, conversion, margin and landing evidence are confirmed')
    return result


async def sync(controller, c, now):
    today = now.astimezone(MSK).date()
    previous = await controller.state(c, 'wordstat_demand') or {}
    if previous.get('attempt_date') == today.isoformat() and previous.get('schema_version')==1:
        if any('average_window_end' not in x for x in previous.get('clusters',[])):
            for cluster in previous['clusters']:
                rows=await c.fetch('SELECT observed_date,query_count FROM profit_wordstat_history WHERE cluster_id=$1',cluster['cluster_id'])
                cluster.update(demand_metrics({str(x['observed_date']):x['query_count'] for x in rows},today))
            await controller.put(c,'wordstat_demand',previous)
        return previous
    state = {'attempt_date': today.isoformat(), 'checked_at': now.isoformat(),
        'state': 'WORDSTAT_DATA_DEGRADED', 'mode': 'DRY_RUN', 'schema_version':1, 'clusters': [],
        'max_calls_per_day': len(CLUSTERS), 'max_request_cost_rub_per_day': .16,
        'overlapping_counts_not_additive': True, 'automatic_semantic_writes': False}
    # Persist the daily attempt before external calls: no retry storm after failure.
    await controller.put(c, 'wordstat_demand', state)
    key, folder = os.getenv('YANDEX_WORDSTAT_API_KEY'), os.getenv('YANDEX_FOLDER_ID')
    if not key or not folder:
        state['error'] = 'missing_existing_wordstat_credentials'
        await controller.put(c, 'wordstat_demand', state)
        return state
    await c.execute('''CREATE TABLE IF NOT EXISTS profit_wordstat_history (
        cluster_id TEXT NOT NULL, observed_date DATE NOT NULL, query_count BIGINT NOT NULL,
        request_scope JSONB NOT NULL, raw_response JSONB NOT NULL, fetched_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY(cluster_id, observed_date));''')
    for cid, name, phrase, intent in CLUSTERS:
        try:
            body = {'folderId': folder, 'phrase': phrase, 'period': 'PERIOD_DAILY',
                'fromDate': str(today-timedelta(days=35))+'T00:00:00Z',
                'toDate': str(today)+'T00:00:00Z', 'regions': ['225'], 'devices': ['DEVICE_ALL']}
            r = await controller.http.post(BASE+'/dynamics', headers={'Authorization': 'Api-key '+key}, json=body, timeout=30)
            if r.status_code != 200:
                state['error'] = 'http_'+str(r.status_code)
                break  # Quota/auth/provider errors do not consume all remaining calls.
            raw = r.json(); history = normalized_history(raw)
            if not history: raise ValueError('empty_history')
            scope = {'phrase': phrase, 'regions': ['225'], 'devices': ['DEVICE_ALL'], 'period': 'PERIOD_DAILY'}
            for day, count in history.items():
                await c.execute('''INSERT INTO profit_wordstat_history VALUES($1,$2::date,$3,$4::jsonb,$5::jsonb,$6)
                    ON CONFLICT(cluster_id,observed_date) DO UPDATE SET query_count=EXCLUDED.query_count,
                    raw_response=EXCLUDED.raw_response,request_scope=EXCLUDED.request_scope,fetched_at=EXCLUDED.fetched_at''',
                    cid, __import__('datetime').date.fromisoformat(day), count, json.dumps(scope), json.dumps(raw), now)
            metrics = demand_metrics(history, today)
            state['clusters'].append({'cluster_id': cid, 'cluster_name': name, 'phrases': [phrase],
                'commercial_intent': intent, 'relevance': 1, **metrics,
                'opportunity_score': None, 'confidence': 'INSUFFICIENT_JOINED_DATA',
                'action': 'RECOMMEND_ONLY', 'missing_score_inputs': ['cluster_direct', 'conversion', 'margin', 'landing']})
        except Exception as exc:
            state['error'] = type(exc).__name__
            break  # No response/secret/contacts are logged.
    if len(state['clusters']) == len(CLUSTERS) and all(x['complete'] for x in state['clusters']):
        state['state'] = 'FRESH'
    state['demand_exists'] = state['state']=='FRESH' and any((x['avg7'] or 0)>0 for x in state['clusters'])
    await controller.put(c, 'wordstat_demand', state)
    return state
