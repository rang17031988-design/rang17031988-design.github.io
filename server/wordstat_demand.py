"""Official Wordstat signal in the existing controller; no advertising writes.

Representative query counts overlap and MUST NOT be summed or interpreted as
available advertising impressions. Missing provider days remain unknown.
"""
import json, os
from datetime import timedelta
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
    avg7, avg30 = sample(7, yesterday), sample(30, yesterday)
    prior7 = sample(7, yesterday-timedelta(days=7))
    a, b = history.get(yesterday.isoformat()), history.get((yesterday-timedelta(days=1)).isoformat())
    return {'wordstat_today': history.get(today.isoformat()), 'wordstat_yesterday': a,
        'avg7': avg7, 'avg30': avg30, 'trend1': (a/b-1) if a is not None and b else None,
        'trend7': (avg7/prior7-1) if avg7 is not None and prior7 else None,
        'latest_provider_date': latest, 'complete': avg30 is not None and a is not None}


def coverage_proxy(demand, impressions, clicks, healthy):
    # Dimensionless delivery diagnostic, NOT a count of lost impressions.
    if not demand.get('complete') or not healthy:
        return {'coverage_proxy': None, 'coverage_status': 'UNKNOWN'}
    avg = demand['avg7']
    proxy = impressions/(avg*7) if avg else None
    return {'coverage_proxy': round(proxy, 4) if proxy is not None else None,
        'coverage_status': 'UNDERDELIVERY' if avg and impressions < 100 and clicks < 10 else 'OBSERVED',
        'interpretation': 'DEMAND COVERAGE PROXY; overlapping searches are not Direct inventory'}


async def sync(controller, c, now):
    today = now.astimezone(MSK).date()
    previous = await controller.state(c, 'wordstat_demand') or {}
    if previous.get('attempt_date') == today.isoformat() and previous.get('schema_version')==1: return previous
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
