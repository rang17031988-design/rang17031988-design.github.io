from product_catalog import read_catalog
"""Own-campaign controller. Provider approval and live-write switch are separate gates.

API contracts: official Direct Reports, KeywordBids and Campaigns services.
No credentials or customer contact data are written to the controller tables.
"""
import asyncio, csv, io, json, os, re
import wordstat_demand
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

CAMPAIGN = 714566814
MOSCOW = ZoneInfo('Europe/Moscow')
MIN_BID, MAX_BID, STEP = Decimal('10'), Decimal('20'), Decimal('.5')
LOCK = 714566814
CPA_CAMPAIGNS={714566814:'Search',715029848:'Network'}
CPA_MIN,CPA_MAX,CPA_STEP=Decimal(100),Decimal(350),Decimal(25)
PAID_GOAL=666936854
COGS_UNIT_RUB = 230  # Handle, packaging, handling and labor included; owner confirmed.


def value_json(v):
    return json.loads(v) if isinstance(v, str) else v


def normalized(term):
    # Direct's {keyword} may omit attached minus words. They are targeting rules,
    # not part of the phrase the buyer clicked.
    positive = re.sub(r'(?<!\w)-[^\s]+', '', term.lower().replace('ё', 'е'))
    return ' '.join(sorted(re.findall(r'[а-яa-z0-9]+', positive)))


def auction_items(item):
    # Official KeywordBids can return null when auction estimates are absent.
    # Missing estimates are unknown, not evidence for increasing a bid.
    return ((item.get('Search') or {}).get('AuctionBids') or {}).get('AuctionBidItems') or []


def decision(bid, data, now, last_change=None):
    """Pure decision function; incomplete data can never justify a raise."""
    bid = Decimal(str(bid))
    if not MIN_BID <= bid <= MAX_BID:
        return ('review', None, 'outside_allowed_range', True)
    clicks, impressions = data.get('clicks', 0), data.get('impressions', 0)
    paid, spend = data.get('paid', 0), Decimal(str(data.get('spend', 0)))
    if clicks >= 20 and spend / clicks > MAX_BID:
        return ('suspend', None, 'anomalous_cpc', True)
    if data.get('irrelevant_clicks', 0) >= 10:
        return ('suspend', None, 'explicitly_irrelevant_queries', True)
    if clicks >= 100 and paid == 0 and data.get('attribution_complete'):
        return ('suspend', None, '100_clicks_without_paid', True)
    if last_change and now - last_change < timedelta(hours=2):
        return ('hold', None, 'cooldown', False)
    if clicks >= 30 and paid == 0:
        return ('set', max(MIN_BID, bid - STEP), 'clicks_without_paid', False)
    if impressions >= 100 and clicks / impressions < .03:
        return ('review', None, 'low_ctr_check_queries_and_copy', False)
    if paid and data.get('contribution') is not None:
        contribution = Decimal(str(data['contribution']))
        if contribution <= 0:
            return ('set', max(MIN_BID, bid - STEP), 'unprofitable_paid', False)
        if spend / paid <= 200 and data.get('auction_limited'):
            return ('set', min(MAX_BID, bid + STEP), 'profitable_auction_limited', False)
    if (clicks == 0 and impressions < 20 and data.get('commercial_verified')
            and data.get('auction_limited') and data.get('observation_days', 0) >= 2):
        return ('set', min(MAX_BID, bid + STEP), 'verified_commercial_auction_limited', False)
    if data.get('lower_bid_preserved_volume') and paid:
        return ('set', max(MIN_BID, bid - STEP), 'lower_bid_preserves_volume', False)
    return ('hold', None, 'insufficient_evidence', False)


def actual_cost_totals(rows):
    paid = [r for r in rows if r['payment_status'] == 'succeeded']
    return {k: sum(float(r[k]) for r in paid) if all(r.get(k) is not None for r in paid) else None
            for k in ('yookassa', 'ozon', 'returns_other')}

def technical_paid_health(checks):
    required=('backend','landing','payment_api','checkout','pvz','metrika_goal',
              'paid_server_enabled','paid_dedupe_ready','no_failed_paid_uploads','no_mass_errors')
    return all(checks.get(k) is True for k in required) and all(v for k,v in checks.items() if k!='paid_delivery_proven')


def cpa_decision(current,data,now,last_change=None):
    """Separate a bounded delivery probe from proven profitable scaling."""
    current=Decimal(str(current))
    if not CPA_MIN<=current<=CPA_MAX:return 'REVIEW',None,'outside_owner_range'
    if data.get('paused'):return 'PAUSED',None,'owner_pause'
    if not data.get('strategy_verified'):return 'BLOCKED',None,'paid_strategy_or_goal_changed'
    if data.get('balance') is None:return 'BLOCKED',None,'balance_unknown'
    if Decimal(str(data['balance']))<current:return 'WAITING_FOR_FUNDS',None,'no_automatic_funding'
    if not data.get('health'):return 'BLOCKED_FUNNEL',None,'health_or_attribution_unverified'
    if last_change and now-last_change<timedelta(hours=24):return 'HOLD',None,'24h_cooldown'
    cap=data.get('economic_max')
    if cap is not None:
        cap=min(CPA_MAX,Decimal(str(cap)))
        if cap<CPA_MIN:return 'SUSPEND',None,'economic_cap_below_minimum'
        if current>cap:return 'SUSPEND',None,'economic_cap_below_current'
    paid=data.get('paid_7d',0);clicks=data.get('clicks_7d',0)
    if clicks>=30 and paid==0:return 'HOLD',None,'traffic_without_verified_payments'
    # Owner permits pre-PAID delivery discovery, not a claim of profitability.
    # Use two full healthy calendar days in the current verified CPA regime;
    # older CPC reports and an unobserved cost cannot be treated as evidence.
    if paid==0 and data.get('prepaid_probe_ready'):
        if not data.get('demand_exists'):return 'HOLD',None,'demand_unknown_or_absent'
        if cap is None:return 'HOLD',None,'economics_unknown'
        target=min(CPA_MAX,current+CPA_STEP)
        if target>current and target<=cap and Decimal(str(data['balance']))>=target:
            return 'SET',target,'prepaid_delivery_probe'
        return 'HOLD',None,'prepaid_probe_cap_or_balance'
    if cap is None:return 'HOLD',None,'economics_unknown'
    if paid>=3 and data.get('cac_paid') is not None and Decimal(str(data['cac_paid']))>cap:
        return ('SET',max(CPA_MIN,current-CPA_STEP),'cac_above_margin') if current>CPA_MIN else ('SUSPEND',None,'cac_above_margin_at_minimum')
    if data.get('observed_days',0)>=2 and data.get('delivery_limited') and paid>=3 and data.get('attribution_complete'):
        target=min(CPA_MAX,current+CPA_STEP)
        if current<target<=cap:return 'SET',target,'verified_profitable_volume_limited'
    if data.get('lower_cpa_preserves_paid') and paid>=3 and current>CPA_MIN:return 'SET',current-CPA_STEP,'lower_cost_preserves_verified_paid'
    return 'HOLD',None,'insufficient_scaling_evidence'


def prepaid_delivery_window(previous,now,healthy,stats):
    """Two complete Moscow days, excluding the transition day and today's partial day."""
    since=previous.get('delivery_probe_since') if previous.get('delivery_probe_policy_version')==1 else None
    if not since or not healthy:since=now.isoformat()
    first=datetime.fromisoformat(since).astimezone(MOSCOW).date()+timedelta(days=1)
    today=now.astimezone(MOSCOW).date()
    days=max(0,(today-first).days)
    start=max(first,today-timedelta(days=2))
    rows=[r for r in stats if start.isoformat()<=r['Date']<today.isoformat()]
    impressions=sum(r['Impressions'] for r in rows);clicks=sum(r['Clicks'] for r in rows)
    return {'delivery_probe_policy_version':1,'delivery_probe_since':since,
            'complete_days':days,'window_start':start.isoformat(),'window_end_exclusive':today.isoformat(),
            'impressions':impressions,'clicks':clicks,
            'ready':bool(healthy and days>=2 and impressions<100 and clicks<10)}


def economics(rows, ad_spend, clicks, impressions, costs):
    """Current status of an order cohort; received date is not inferred from creation."""
    def select(predicate):
        chosen = [r for r in rows if predicate(r)]
        return {'orders': len(chosen), 'units': sum(r['quantity'] for r in chosen),
                'rub': float(sum(Decimal(str(r['amount'])) for r in chosen))}
    counts = {
        'ordered': select(lambda r: r.get('order_id') is not None),
        'paid': select(lambda r: r['payment_status'] == 'succeeded'),
        'in_transit': select(lambda r: r['ozon_status'] in ('on_way','in_courier_service','acceptance_in_progress')),
        'ready': select(lambda r: r['ozon_status'] == 'in_delivery_point'),
        'received': select(lambda r: r['ozon_status'] == 'delivered'),
        'canceled': select(lambda r: r['ozon_status'] == 'canceled' or r['payment_status'] == 'canceled'),
        'returned': select(lambda r: r.get('return_received') is True),
        'return_pending': select(lambda r: r.get('return_pending') is True),
    }
    ratio = lambda n, d: n / d if d else None
    spend = float(ad_spend)
    received = counts['received']
    cogs, tax = received['units'] * COGS_UNIT_RUB, received['rub'] * .06
    cost_names = ('yookassa','ozon','returns_other')
    received_rows = [r for r in rows if r['ozon_status'] == 'delivered']
    # In-transit fees are reported, but not charged to another received cohort.
    received_costs = {k: sum(float(r[k]) for r in received_rows)
                      if all(r.get(k) is not None for r in received_rows) else
                      costs.get(k) if len(received_rows) == len(rows) else None
                      for k in cost_names}
    complete = all(received_costs[k] is not None and costs.get(k) is not None for k in cost_names)
    known = sum(v for v in received_costs.values() if v is not None)
    provisional = received['rub'] - cogs - tax - known - spend
    contribution = received['rub'] - cogs - tax - sum(received_costs.values()) if complete else None
    net = contribution - spend if complete else None
    return {'cohort_basis': 'order_created_at; current fulfillment status',
            'counts': counts, 'impressions': impressions, 'clicks': clicks,
            'spend': spend, 'ctr': ratio(clicks * 100, impressions), 'cpc': ratio(spend, clicks),
            'cr': {k: ratio(counts[k]['orders'] * 100, clicks) for k in ('ordered','paid','received')},
            'cac': {k: ratio(spend, counts[k]['orders']) for k in ('ordered','paid','received')},
            'aov': ratio(counts['paid']['rub'], counts['paid']['orders']),
            'cogs_unit_rub': COGS_UNIT_RUB, 'cogs_received': cogs, 'tax_received': tax, 'costs': costs,
            'received_costs': received_costs, 'other_costs_default_rub': 0,
            'net_profit_before_unknown_costs': provisional,
            'net_profit_status': 'FINAL' if complete else 'PROVISIONAL',
            'missing_received_costs': [k for k in cost_names if received_costs[k] is None],
            'contribution': contribution, 'net_profit': net,
            'profit_per_received_order': ratio(net, received['orders']) if net is not None else None,
            'roas_paid': ratio(counts['paid']['rub'], spend),
            'roas_received': ratio(received['rub'], spend),
            'romi': ratio(net, spend) if net is not None else None}


class DirectError(Exception):
    def __init__(self, code):
        self.code = str(code)
        super().__init__('Direct API unavailable: ' + self.code)


class Controller:
    def __init__(self, pool, http, ozon_read=None):
        self.pool, self.http = pool, http
        self.ozon_read = ozon_read
        self.status = {'state': 'starting', 'live_writes': False}

    @property
    def writes(self):
        return os.getenv('PROFIT_CONTROLLER_LIVE', '').lower() == 'true'

    async def schema(self, c):
        await c.execute('''
            SELECT pg_advisory_xact_lock(714566815);
            CREATE TABLE IF NOT EXISTS profit_controller_state (
                key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW());
            CREATE TABLE IF NOT EXISTS profit_controller_snapshots (
                hour TIMESTAMPTZ PRIMARY KEY, payload JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS profit_controller_actions (
                id BIGSERIAL PRIMARY KEY, keyword_id BIGINT, action TEXT NOT NULL,
                reason TEXT NOT NULL, before_bid NUMERIC, after_bid NUMERIC,
                state TEXT NOT NULL, emergency BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW());
            CREATE TABLE IF NOT EXISTS profit_controller_costs (
                order_id BIGINT PRIMARY KEY, yookassa NUMERIC, ozon NUMERIC,
                returns_other NUMERIC, source TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW());
            CREATE TABLE IF NOT EXISTS profit_controller_notifications (
                key TEXT PRIMARY KEY, state TEXT NOT NULL, message_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW());
            CREATE TABLE IF NOT EXISTS profit_controller_returns (
                return_number TEXT PRIMARY KEY,order_id BIGINT NOT NULL,
                return_type TEXT NOT NULL,status TEXT NOT NULL,
                checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
            CREATE TABLE IF NOT EXISTS profit_cpa_actions (
                id BIGSERIAL PRIMARY KEY,campaign_id BIGINT NOT NULL,action TEXT NOT NULL,
                reason TEXT NOT NULL,before_cpa NUMERIC,after_cpa NUMERIC,state TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),updated_at TIMESTAMPTZ DEFAULT NOW());
            ALTER TABLE commerce_pending_orders ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE commerce_pending_orders ADD COLUMN IF NOT EXISTS is_internal BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE commerce_pending_orders ADD COLUMN IF NOT EXISTS traffic_class TEXT NOT NULL DEFAULT 'customer';
            UPDATE commerce_pending_orders SET is_test=TRUE,is_internal=TRUE,traffic_class='internal_test'
                WHERE order_number IN (1008,1009,1010,1011,1012) AND traffic_class='customer';
        ''')

    async def state(self, c, key):
        data = await c.fetchval('SELECT value FROM profit_controller_state WHERE key=$1', key)
        return value_json(data) if data else None

    async def put(self, c, key, value):
        await c.execute('''INSERT INTO profit_controller_state(key,value) VALUES($1,$2::jsonb)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()''', key, json.dumps(value, default=str))

    def headers(self):
        token = os.getenv('YANDEX_DIRECT_TOKEN')
        if not token: raise DirectError('missing_token')
        return {'Authorization': 'Bearer ' + token, 'Client-Login': 'rang1703', 'Accept-Language': 'en'}

    async def api(self, resource, method, params):
        r = await self.http.post('https://api.direct.yandex.com/json/v501/' + resource,
                                headers=self.headers(), json={'method': method, 'params': params})
        if r.status_code != 200: raise DirectError('http_' + str(r.status_code))
        body = r.json()
        if body.get('error'): raise DirectError(body['error'].get('error_code', 'unknown'))
        if 'result' not in body: raise DirectError('invalid_result')
        return body['result']

    async def report(self, start, end, query=False,campaign_id=CAMPAIGN):
        fields = ['Date','CampaignId','AdGroupId','AdId','CriterionId','Criterion','Impressions','Clicks','Cost']
        if query: fields.append('Query')
        spec = {'SelectionCriteria': {'DateFrom': start.isoformat(), 'DateTo': end.isoformat(),
                    'Filter': [{'Field':'CampaignId','Operator':'EQUALS','Values':[str(campaign_id)]}]},
                'FieldNames': fields, 'ReportName': f'posuda-{campaign_id}-{start}-{end}-' + ('queries' if query else 'keywords'),
                'ReportType': 'SEARCH_QUERY_PERFORMANCE_REPORT' if query else 'CUSTOM_REPORT',
                'DateRangeType': 'CUSTOM_DATE', 'Format': 'TSV', 'IncludeVAT': 'YES', 'IncludeDiscount': 'YES'}
        headers = {**self.headers(), 'processingMode':'auto', 'returnMoneyInMicros':'false',
                   'skipReportHeader':'true','skipReportSummary':'true'}
        r = await self.http.post('https://api.direct.yandex.com/json/v501/reports', headers=headers, json={'params': spec})
        if r.status_code in (201,202): raise DirectError('report_pending')
        if r.status_code != 200: raise DirectError('report_http_' + str(r.status_code))
        reader = csv.DictReader(io.StringIO(r.text), delimiter='\t')
        if not reader.fieldnames or not set(fields).issubset(reader.fieldnames):
            raise DirectError('report_columns')
        rows = list(reader)
        if any(not set(fields).issubset(row) for row in rows): raise DirectError('report_columns')
        for row in rows:
            row['Cost'] = float(Decimal(row['Cost']))
            row['Clicks'], row['Impressions'] = int(row['Clicks']), int(row['Impressions'])
            if int(row['CampaignId']) != campaign_id: raise DirectError('campaign_mismatch')
        return rows

    async def notify(self, c, key, text):
        token, owner = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('OWNER_CHAT_ID')
        if not token or not owner: return
        claim = await c.fetchval('''INSERT INTO profit_controller_notifications(key,state)
            VALUES($1,'claimed') ON CONFLICT DO NOTHING RETURNING key''', key)
        if not claim: return
        state, mid = 'unknown', None
        try:
            r = await self.http.post('https://api.telegram.org/bot' + token + '/sendMessage',
                                    json={'chat_id': owner, 'text': text[:4000]})
            data = r.json()
            if r.status_code == 200 and data.get('ok'):
                state, mid = 'sent', data['result']['message_id']
        except Exception:
            pass  # An ambiguous send must never be retried blindly.
        await c.execute('UPDATE profit_controller_notifications SET state=$2,message_id=$3 WHERE key=$1', key, state, mid)

    async def mutate(self, c, keyword, action, reason, before=None, after=None, emergency=False):
        if after is not None:
            after = Decimal(str(after))
            if not MIN_BID <= after <= MAX_BID: raise ValueError('Bid outside range')
            if before is None or abs(after - Decimal(str(before))) > 1: raise ValueError('Step outside range')
        aid = await c.fetchval('''INSERT INTO profit_controller_actions
            (keyword_id,action,reason,before_bid,after_bid,state,emergency) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id''',
            keyword, action, reason, before, after, 'prepared' if self.writes else 'dry_run', emergency)
        if not self.writes: return False
        # Persist intent before API. An unknown write locks this object for review.
        try:
            if action == 'set':
                result = await self.api('keywordbids','set',{'KeywordBids':[{'KeywordId':keyword,'SearchBid':int(after * 1000000)}]})
                items = result.get('SetResults', [])
            else:
                resource = 'keywords' if keyword else 'campaigns'
                result = await self.api(resource,action,{'SelectionCriteria':{'Ids':[keyword or CAMPAIGN]}})
                items = result.get(action.title() + 'Results', [])
            if len(items) != 1 or items[0].get('Errors'): raise DirectError('write_rejected')
            object_id = items[0].get('KeywordId', items[0].get('Id'))
            if object_id != (keyword or CAMPAIGN): raise DirectError('write_identity')
        except Exception:
            await c.execute("UPDATE profit_controller_actions SET state='unknown',updated_at=NOW() WHERE id=$1", aid)
            raise
        await c.execute("UPDATE profit_controller_actions SET state='applied',updated_at=NOW() WHERE id=$1", aid)
        return True

    async def guard(self, c, campaign, spend, now):
        day = now.astimezone(MOSCOW).date().isoformat()
        paused = await self.state(c, 'paused')
        unresolved = await c.fetchval("SELECT EXISTS(SELECT 1 FROM profit_controller_actions WHERE keyword_id IS NULL AND state IN ('prepared','unknown'))")
        if unresolved:
            await self.notify(c, 'ambiguous_campaign_write', '⚠️ Контроллер Direct: результат изменения кампании неизвестен. Нужна проверка; автоматическое возобновление заблокировано.')
            return True
        if spend >= 10000:
            if campaign['State'] == 'ON' and not paused:
                if await self.mutate(c, None, 'suspend', 'daily_10000_moscow', emergency=True):
                    await self.put(c, 'paused', {'day':day,'reason':'daily_spend','resume_allowed':True})
                    await self.notify(c, 'daily-' + day, f'⚠️ Direct: расход {spend:.2f} ₽. Кампания остановлена до следующего дня (МСК).')
            return True
        if paused:
            if paused.get('resume_allowed') and paused['day'] != day and campaign['State'] == 'SUSPENDED':
                if await self.mutate(c, None, 'resume', 'next_moscow_day'):
                    await self.put(c, 'paused', None)
            return True
        return campaign['State'] != 'ON'

    async def service_guard(self, c, campaign, now):
        """Two independent failed checks pause spend; recovery requires owner review."""
        checks = {}
        try:
            landing = await self.http.get('https://xn--163-5cdt3dgrs.xn--p1ai/', follow_redirects=True)
            checks['landing'] = landing.status_code == 200 and 'data-offer-unit-price' in landing.text and 'Купить' in landing.text
        except Exception:
            checks['landing'] = False
        try:
            backend = await self.http.get('https://ozon-delivery-gateway-production.up.railway.app/health')
            body = backend.json()
            checks['backend'] = backend.status_code == 200 and all(body.get(k) for k in
                ('ok', 'db_configured', 'pii_configured', 'payment_reconciliation_running'))
        except Exception:
            checks['backend'] = False
        try:
            payment = await self.http.get('https://api.yookassa.ru/v3/me',
                auth=(os.getenv('YOOKASSA_SHOP_ID', ''), os.getenv('YOOKASSA_SECRET_KEY', '')))
            checks['payment_api'] = payment.status_code == 200
        except Exception:
            checks['payment_api'] = False
        previous = await self.state(c, 'service_checks') or {}
        failures = 0 if all(checks.values()) else int(previous.get('failures', 0)) + 1
        await self.put(c, 'service_checks', {'checked_at':now.isoformat(), 'checks':checks, 'failures':failures})
        if failures >= 2 and campaign['State'] == 'ON' and not await self.state(c, 'paused'):
            if await self.mutate(c, None, 'suspend', 'landing_or_payment_unavailable', emergency=True):
                await self.put(c, 'paused', {'day':now.astimezone(MOSCOW).date().isoformat(),
                    'reason':'service_unavailable', 'resume_allowed':False})
                await self.notify(c, 'service-failure-'+now.date().isoformat(),
                    '⚠️ Direct остановлен: две последовательные проверки магазина/платёжного backend не прошли. Проверьте сервисы перед возобновлением рекламы.')
        return failures > 0

    async def actual_payment_costs(self, c, start, end):
        """Read-only provider amounts; missing income_amount remains unknown."""
        rows = await c.fetch('''SELECT p.order_id,p.payment_id,p.amount FROM commerce_pending_orders p
            LEFT JOIN profit_controller_costs k USING(order_id)
            WHERE p.payment_status='succeeded' AND p.payment_id IS NOT NULL
            AND p.created_at >= $1 AND p.created_at < $2 AND k.yookassa IS NULL LIMIT 50''', start, end)
        for row in rows:
            try:
                import uuid
                pid = str(uuid.UUID(row['payment_id']))
                response = await self.http.get('https://api.yookassa.ru/v3/payments/'+pid,
                    auth=(os.getenv('YOOKASSA_SHOP_ID',''), os.getenv('YOOKASSA_SECRET_KEY','')))
                if response.status_code != 200: continue
                payment = response.json()
                gross, income = payment.get('amount',{}), payment.get('income_amount',{})
                if (payment.get('id') != pid or payment.get('status') != 'succeeded' or payment.get('paid') is not True
                        or gross.get('currency') != 'RUB' or income.get('currency') != 'RUB'
                        or Decimal(gross['value']) != Decimal(str(row['amount']))): continue
                fee = Decimal(gross['value']) - Decimal(income['value'])
                if not 0 <= fee <= Decimal(gross['value']): continue
                await c.execute('''INSERT INTO profit_controller_costs(order_id,yookassa,source)
                    VALUES($1,$2,'YooKassa amount minus income_amount, including provider tax')
                    ON CONFLICT(order_id) DO UPDATE SET yookassa=EXCLUDED.yookassa,updated_at=NOW()''',row['order_id'],fee)
            except Exception:
                # A cost read cannot interrupt payment polling or invent a fee.
                continue

    async def actual_returns(self, c, start):
        """Official read-only return API, linked only to our immutable parcel IDs."""
        if self.ozon_read is None:return {'available':False,'reason':'reader_missing'}
        cursor=None;seen=set();matched=0;unlinked=0
        try:
            for page in range(100):
                result=await self.ozon_read('/v1/return/search',{
                    'filters':{'created_at_from':start.isoformat()},
                    'pagination':{'limit':100,'cursor':cursor}})
                if not isinstance(result.get('returns'),list):raise ValueError('invalid_returns')
                for item in result['returns']:
                    identity=re.fullmatch(r'posuda-insales-(\d+)-(\d+)',item.get('return_external_id') or '')
                    if not identity or not item.get('return_number'):
                        unlinked+=1;continue
                    order_id,index=map(int,identity.groups())
                    quantity=await c.fetchval("SELECT quantity FROM commerce_pending_orders WHERE order_id=$1 AND payment_status='succeeded'",order_id)
                    if quantity is None or not 1<=index<=quantity:
                        unlinked+=1;continue
                    if item.get('return_type') not in ('client_return','cancellation') or item.get('status') not in ('unknown','moving','at_pickup_point','received','utilization','utilized','written_off','looking_for'):
                        unlinked+=1;continue
                    await c.execute('''INSERT INTO profit_controller_returns(return_number,order_id,return_type,status)
                        VALUES($1,$2,$3,$4) ON CONFLICT(return_number) DO UPDATE
                        SET status=EXCLUDED.status,checked_at=NOW()
                        WHERE profit_controller_returns.order_id=EXCLUDED.order_id''',
                        item['return_number'],order_id,item['return_type'],item['status'])
                    matched+=1
                cursor=result.get('next_cursor')
                if not cursor:
                    # A complete real returns read confirms no return charge for
                    # unaffected current paid orders. Preserve any recorded charge.
                    await c.execute('''UPDATE profit_controller_costs k SET returns_other=0,
                        updated_at=NOW() FROM commerce_pending_orders p
                        WHERE k.order_id=p.order_id AND k.returns_other IS NULL
                        AND p.payment_status='succeeded' AND p.created_at >= $1
                        AND coalesce(p.ozon_status,'') NOT IN ('canceled','returned')
                        AND NOT EXISTS(SELECT 1 FROM profit_controller_returns r WHERE r.order_id=p.order_id)''',start)
                    return {'available':True,'linked':matched,'unlinked':unlinked}
                if cursor in seen:raise ValueError('repeated_return_cursor')
                seen.add(cursor)
            return {'available':False,'reason':'pagination_limit','linked':matched,'unlinked':unlinked}
        except Exception as exc:
            return {'available':False,'reason':type(exc).__name__,'linked':matched,'unlinked':unlinked}

    async def order_cohort(self, c, start, end):
        return [dict(r) for r in await c.fetch('''SELECT p.order_id,p.order_number,p.quantity,p.amount,
            CASE WHEN p.payment_status='succeeded' AND y.status IS DISTINCT FROM 'succeeded' THEN 'unverified' ELSE p.payment_status END AS payment_status,p.ozon_status,p.shipment_id,p.tracking_number,p.created_at,
            p.snapshot->'attribution' AS attribution,k.yookassa,k.ozon,k.returns_other,
            EXISTS(SELECT 1 FROM profit_controller_returns r WHERE r.order_id=p.order_id
                AND r.return_type='client_return' AND r.status='received') AS return_received,
            EXISTS(SELECT 1 FROM profit_controller_returns r WHERE r.order_id=p.order_id
                AND r.return_type='client_return' AND r.status<>'received') AS return_pending
            FROM commerce_pending_orders p LEFT JOIN profit_controller_costs k USING(order_id) LEFT JOIN insales_yookassa_payments y ON y.payment_id=p.payment_id
            WHERE p.order_id IS NOT NULL AND NOT p.is_test AND NOT p.is_internal
            AND p.created_at >= $1 AND p.created_at < $2''',start,end)]

    async def metrika(self, start, end):
        token = os.getenv('METRIKA_OAUTH_TOKEN')
        if not token: return {'available':False,'reason':'missing_token'}
        r = await self.http.get('https://api-metrika.yandex.net/stat/v1/data',
            headers={'Authorization':'OAuth ' + token},params={'ids':112544007,'date1':start.isoformat(),
            'date2':end.isoformat(),'metrics':'ym:s:visits,ym:s:ecommercePurchases','accuracy':'full'})
        if r.status_code != 200: return {'available':False,'http':r.status_code}
        data = r.json()
        return {'available':True,'visits':data.get('totals',[None,None])[0],
                'ecommerce_orders':data.get('totals',[None,None])[1],
                'checkout':None,'checkout_reason':'no verified checkout goal identifier',
                'sampled':data.get('sampled')}

    async def hourly(self, c, now, paused):
        hour = now.replace(minute=0,second=0,microsecond=0)
        if await c.fetchval('SELECT EXISTS(SELECT 1 FROM profit_controller_snapshots WHERE hour=$1)',hour): return
        today = now.astimezone(MOSCOW).date()
        start_day = today - timedelta(days=6)
        stats = await self.report(start_day, today)
        queries = await self.report(start_day, today, True)
        start = datetime.combine(start_day, datetime.min.time(), MOSCOW)
        await self.actual_payment_costs(c, start, now)
        return_sync=await self.actual_returns(c, start)
        orders = await self.order_cohort(c, start, now)
        stock = await self.stock(c,now)
        if stock['estimated_units'] < 50:
            if not paused:
                if await self.mutate(c,None,'suspend','critical_estimated_stock',emergency=True):
                    await self.put(c,'paused',{'day':today.isoformat(),'reason':'critical_stock','resume_allowed':False})
            paused = True
        costs = actual_cost_totals(orders)
        econ = economics(orders,sum(r['Cost'] for r in stats),sum(r['Clicks'] for r in stats),
                         sum(r['Impressions'] for r in stats),costs)
        metric = await self.metrika(start_day,today)
        metric['backend_checkout_records'] = await c.fetchval('''SELECT count(*) FROM commerce_pending_orders
            WHERE created_at >= $1 AND created_at < $2''',start,now)
        metric['backend_checkout_basis'] = 'validated checkout preparation records; not Metrika goal or unique visits'
        strategy_result=await self.api('campaigns','get',{'SelectionCriteria':{'Ids':[CAMPAIGN]},'FieldNames':['Id'],'UnifiedCampaignFieldNames':['BiddingStrategy']})
        campaigns=strategy_result.get('Campaigns',[])
        if len(campaigns)!=1 or campaigns[0]['Id']!=CAMPAIGN:raise DirectError('strategy_campaign_mismatch')
        search_strategy=campaigns[0].get('UnifiedCampaign',{}).get('BiddingStrategy',{}).get('Search',{}).get('BiddingStrategyType')
        automatic_strategy=search_strategy not in ('MANUAL_CPC','HIGHEST_POSITION','LOWEST_COST')
        bid_result = {'KeywordBids':[]} if automatic_strategy else await self.api('keywordbids','get',{'SelectionCriteria':{'CampaignIds':[CAMPAIGN]},
            'FieldNames':['KeywordId','AdGroupId','CampaignId','ServingStatus'],
            'SearchFieldNames':['Bid','AuctionBids']})
        if bid_result.get('LimitedBy'): raise DirectError('incomplete_bids')
        all_bids = bid_result.get('KeywordBids',[])
        keyword_result = await self.api('keywords','get',{'SelectionCriteria':{'CampaignIds':[CAMPAIGN]},
            'FieldNames':['Id','Keyword','AdGroupId','State']})
        if keyword_result.get('LimitedBy'): raise DirectError('incomplete_keywords')
        keywords = {r['Id']:r for r in keyword_result.get('Keywords',[])}
        observed = await self.state(c,'first_observation')
        if not observed:
            observed = now.isoformat()
            await self.put(c,'first_observation',observed)
        observation_days = (now - datetime.fromisoformat(observed)).total_seconds() / 86400
        # Deliberately no per-keyword PAID inference from unattributed order totals.
        for item in all_bids:
            if item.get('CampaignId') != CAMPAIGN: raise DirectError('bid_campaign_mismatch')
            kid = item['KeywordId']
            selected = [r for r in stats if r.get('CriterionId') == str(kid)]
            meta = keywords.get(kid,{})
            phrase = meta.get('Keyword','')
            if meta.get('State') != 'ON' or not phrase or phrase.startswith('---'): continue
            matched = [r for r in orders if r['payment_status']=='succeeded' and
                       normalized((value_json(r['attribution']) or {}).get('utm_term','')) == normalized(phrase) and phrase]
            last = await c.fetchval("SELECT max(created_at) FROM profit_controller_actions WHERE keyword_id=$1 AND state='applied'",kid)
            unknown = await c.fetchval("SELECT EXISTS(SELECT 1 FROM profit_controller_actions WHERE keyword_id=$1 AND state IN ('prepared','unknown'))",kid)
            if unknown or paused or item.get('ServingStatus') != 'ELIGIBLE': continue
            bid = Decimal(str(item.get('Search',{}).get('Bid',0))) / 1000000
            auction = auction_items(item)
            required = [Decimal(str(a['Bid'])) / 1000000 for a in auction if a.get('Bid')]
            minimum_required = min(required) if required else None
            fully_costed = [r for r in matched if r['ozon_status']=='delivered' and
                           all(r[k] is not None for k in ('yookassa','ozon','returns_other'))]
            contribution = None
            if matched and len(fully_costed)==len(matched):
                contribution = sum(float(r['amount']) - r['quantity']*COGS_UNIT_RUB - float(r['amount'])*.06
                                   - sum(float(r[k]) for k in ('yookassa','ozon','returns_other')) for r in matched)
                contribution -= sum(r['Cost'] for r in selected)
            data = {'clicks':sum(r['Clicks'] for r in selected),'impressions':sum(r['Impressions'] for r in selected),
                    'spend':sum(r['Cost'] for r in selected),'paid':len(matched),
                    'contribution':contribution,'attribution_complete':False,
                    'commercial_verified':meta.get('AdGroupId')==5800551077,
                    'auction_limited':minimum_required is not None and bid < minimum_required <= MAX_BID,
                    'observation_days':observation_days}
            # Only unambiguous unrelated purchase intent, never repair/replacement intent.
            unrelated = re.compile(r'(дверн|мебельн|оконн|письменн|автомобильн|ручк.{0,12}сумк|3d|3д)',re.I)
            data['irrelevant_clicks'] = sum(r['Clicks'] for r in queries
                if r.get('CriterionId') == str(kid) and unrelated.search(r.get('Query','')))
            # Unknown economics and attribution block automatic profitable scaling/pause.
            action, target, reason, emergency = decision(bid,data,now,last)
            if action == 'set' and target == bid: action, target = 'hold',None
            if action in ('set','suspend'):
                await self.mutate(c,kid,action,reason,bid,target,emergency)
            else:
                await c.execute('''INSERT INTO profit_controller_actions(keyword_id,action,reason,before_bid,state)
                    VALUES($1,$2,$3,$4,'observed')''',kid,action,reason,bid)
        payload = {'direct':stats,'queries':queries,'metrika':metric,'economics':econ,'returns_sync':return_sync,
                   'attribution':{'real_yclid_orders':sum(bool((value_json(r['attribution']) or {}).get('yclid')) for r in orders)},
                   'live_writes':self.writes,'stock':{'units':None,'basis':'owner estimate 700; authoritative inventory not connected'}}
        payload['stock'] = stock
        # Reuse the already fetched Search report: no extra Wordstat/report calls.
        # A demand diagnostic failure must not stop the existing CPA controller.
        try:
            demand = await self.state(c, 'wordstat_demand') or {}
            sessions = [dict(r) for r in await c.fetch('''SELECT attribution,is_test,is_internal,traffic_class
                FROM profit_funnel_sessions WHERE started_at >= $1 AND started_at < $2''', start, now)]
            service = await self.state(c, 'service_checks') or {}
            demand = wordstat_demand.joined_metrics(demand, stats, sessions, orders, start, now,
                healthy=bool(service.get('checks')) and all(service['checks'].values()))
            await self.put(c, 'wordstat_demand', demand)
            payload['wordstat_join'] = {'mode': demand['mode'], 'joined_at': demand['joined_at'],
                'unassigned': demand['joined_unassigned']}
        except Exception as exc:
            payload['wordstat_join'] = {'mode': 'DRY_RUN', 'error': type(exc).__name__}
        if stock.get('estimated_units') is not None:
            for threshold in (300,150,100,50):
                if stock['estimated_units'] < threshold:
                    await self.notify(c,'stock-'+str(threshold),f"⚠️ Остаток ручек ниже {threshold}: оценка {stock['estimated_units']} шт. Проверьте фактический склад до масштабирования.")
        await c.execute('INSERT INTO profit_controller_snapshots(hour,payload) VALUES($1,$2::jsonb) ON CONFLICT DO NOTHING',hour,json.dumps(payload,default=str))
        # Paid shipments still requiring an owner action are queued once; never reconfirm on_way.
        for r in orders:
            if r['payment_status']=='succeeded' and r['shipment_id'] and r['ozon_status'] in ('created','forming','ready_for_shipping'):
                await self.notify(c,'ozon-confirm-'+str(r['order_id']),
                    f"📦 Заказ №{r['order_number']}: отправление {r['shipment_id']}, статус {r['ozon_status']}.\nНомер: {r['tracking_number'] or 'ожидается'}. Проверьте очередь сборки/этикетку и передайте посылку в Ozon; дубль не создавайте.")

    async def daily_shipments(self,c,now):
        local=now.astimezone(MOSCOW)
        if local.hour < 18: return
        rows=await c.fetch('''SELECT order_number,tracking_number,ozon_status FROM commerce_pending_orders
            WHERE payment_status='succeeded' AND ozon_status IN ('created','forming','forming_failed','ready_for_shipping')
            ORDER BY created_at''')
        if rows:
            lines=['📦 ОЧЕРЕДЬ ОТГРУЗКИ '+local.date().isoformat()]
            lines += [f"№{r['order_number']} — {r['tracking_number'] or 'номер ожидается'} — {r['ozon_status']}" for r in rows]
            lines.append('Проверьте сборку и этикетки в Ozon. Подтверждённые посылки передайте в пункт отгрузки. Повторные отправления не создавайте.')
            await self.notify(c,'daily-shipment-'+local.date().isoformat(),'\n'.join(lines))

    async def stock(self,c,now):
        catalog=await read_catalog(self.http)
        remaining=catalog['quantity']
        sales=await c.fetchrow("SELECT coalesce(sum(quantity) FILTER (WHERE payment_status='succeeded'),0) AS units,min(created_at) AS first_seen FROM commerce_pending_orders WHERE created_at>=$1",now-timedelta(days=30))
        elapsed=max(1,(now-sales['first_seen']).total_seconds()/86400) if sales and sales['first_seen'] else None
        velocity=float(sales['units'])/elapsed if elapsed and sales['units'] else None
        await self.put(c,'stock_native',{'units':remaining,'as_of':now.isoformat(),'source':catalog['source']})
        return {'estimated_units':remaining,'valuation_rub':remaining*COGS_UNIT_RUB,
                'cogs_unit_rub':COGS_UNIT_RUB,'as_of':now.isoformat(),'basis':'InSales native available stock; no additional paid subtraction',
                'coverage_at_target_days':remaining/50,'coverage_at_observed_velocity_days':remaining/velocity if velocity else None,
                'authoritative':True,'unit_price':catalog['unit_price']}

    async def weekly(self, c, now, preview=False):
        local = now.astimezone(MOSCOW)
        if not preview and (local.weekday() != 0 or local.hour < 9): return
        end = now if preview else datetime.combine(local.date(),datetime.min.time(),MOSCOW)
        start = end - timedelta(days=7)
        key = 'weekly-' + end.date().isoformat()
        if not preview and await c.fetchval('SELECT EXISTS(SELECT 1 FROM profit_controller_notifications WHERE key=$1)',key): return
        last_day = end.date() if preview else (end-timedelta(days=1)).date()
        stats = await self.report(start.date(),last_day)
        orders = await self.order_cohort(c,start,end)
        costs = actual_cost_totals(orders)
        e = economics(orders,sum(r['Cost'] for r in stats),sum(r['Clicks'] for r in stats),sum(r['Impressions'] for r in stats),costs)
        fmt = lambda v: 'неизвестно' if v is None else f'{v:.2f}'
        lines = [f'📊 ОТЧЁТ ЗА НЕДЕЛЮ {start.date()} — {last_day}',
                 'Когорта: заказы, созданные за неделю; текущий статус Ozon.',
                 f"Показы: {e['impressions']} | Клики: {e['clicks']} | CTR: {fmt(e['ctr'])}%",
                 f"Расход (с НДС): {fmt(e['spend'])} ₽ | CPC: {fmt(e['cpc'])} ₽"]
        names = {'ordered':'Заказано','paid':'Оплачено','in_transit':'В пути','ready':'В ПВЗ',
                 'received':'Получено','canceled':'Отменено','returned':'Возврат получен продавцом','return_pending':'Возврат в процессе'}
        for k,n in names.items():
            v=e['counts'][k]; lines.append(f"{n}: {v['units']} шт / {fmt(v['rub'])} ₽ ({v['orders']} заказов)")
        for k in ('ordered','paid','received'): lines.append(f"CR {k}: {fmt(e['cr'][k])}% | CAC: {fmt(e['cac'][k])} ₽")
        stock = await self.stock(c,now)
        lines += [f"COGS полученных: {e['cogs_received']} ₽ | Tax 6%: {fmt(e['tax_received'])} ₽",
                  f"YooKassa: {fmt(costs['yookassa'])} ₽ | Ozon: {fmt(costs['ozon'])} ₽ | Возвраты/прочее: {fmt(costs['returns_other'])} ₽",
                  f"NET PROFIT ({e['net_profit_status']}): {fmt(e['net_profit'])} ₽ | Profit/order: {fmt(e['profit_per_received_order'])} ₽",
                  f"До неизвестных расходов, только RECEIVED: {fmt(e['net_profit_before_unknown_costs'])} ₽; Ozon UNKNOWN не равен 0.",
                  'COGS 230 ₽/шт включает упаковку, обработку и труд; отдельные расходы учитываются только по подтверждению.',
                  f"ROAS paid: {fmt(e['roas_paid'])} | ROAS received: {fmt(e['roas_received'])} | ROMI: {fmt(e['romi'])}",
                  f"STOCK (оценка): {stock['estimated_units']} шт / {stock['valuation_rub']} ₽; покрытие по текущей скорости: {fmt(stock['coverage_at_observed_velocity_days'])} дней. При цели 50/день: {fmt(stock['coverage_at_target_days'])} дней."]
        message = '\n'.join(lines)
        if preview:
            return {'preview':True,'notification_sent':False,'message':message,'economics':e,'stock':stock}
        await self.notify(c,key,message)

    async def cpa_balance(self):
        response=await self.http.post('https://api.direct.yandex.ru/live/v4/json/',json={
            'method':'AccountManagement','token':os.environ['YANDEX_DIRECT_TOKEN'],
            'param':{'Action':'Get','SelectionCriteria':{'Logins':['rang1703']}}})
        if response.status_code!=200:return None
        accounts=response.json().get('data',{}).get('Accounts',[])
        row=next((a for a in accounts if a.get('Login')=='rang1703' and a.get('Currency')=='RUB'),None)
        return Decimal(str(row['Amount'])) if row else None

    async def cpa_write(self,c,campaign,action,reason,target=None):
        import copy
        cid=campaign['Id'];side=CPA_CAMPAIGNS[cid]
        strategy=copy.deepcopy(campaign['UnifiedCampaign']['BiddingStrategy'])
        current=Decimal(str(strategy.get(side,{}).get('PayForConversion',{}).get('Cpa',0)))/1000000
        if action=='SET':
            if target is None or not CPA_MIN<=target<=CPA_MAX or abs(target-current)>CPA_STEP:
                raise ValueError('CPA bounds')
            strategy[side]['PayForConversion']['Cpa']=int(target*1000000)
        aid=await c.fetchval('''INSERT INTO profit_cpa_actions(campaign_id,action,reason,before_cpa,after_cpa,state)
            VALUES($1,$2,$3,$4,$5,$6) RETURNING id''',cid,action,reason,current,target,'prepared' if self.writes else 'dry_run')
        if not self.writes:return False
        try:
            params={'Campaigns':[{'Id':cid,'UnifiedCampaign':{'BiddingStrategy':strategy}}]} if action=='SET' else {'SelectionCriteria':{'Ids':[cid]}}
            result=await self.api('campaigns','update' if action=='SET' else 'suspend',params)
            items=result.get('UpdateResults' if action=='SET' else 'SuspendResults',[])
            if len(items)!=1 or items[0].get('Id')!=cid or items[0].get('Errors'):raise DirectError('cpa_write_rejected')
            checked=await self.api('campaigns','get',{'SelectionCriteria':{'Ids':[cid]},'FieldNames':['Id','State'],'UnifiedCampaignFieldNames':['BiddingStrategy','CounterIds']})
            saved=checked['Campaigns'][0]
            if action=='SET' and saved['UnifiedCampaign']['BiddingStrategy']!=strategy:raise DirectError('cpa_readback_mismatch')
            if action=='SUSPEND' and saved['State']!='SUSPENDED':raise DirectError('suspend_readback_mismatch')
        except Exception:
            await c.execute("UPDATE profit_cpa_actions SET state='unknown',updated_at=NOW() WHERE id=$1",aid)
            raise
        await c.execute("UPDATE profit_cpa_actions SET state='applied',updated_at=NOW() WHERE id=$1",aid)
        return True

    async def rsya_continuation(self,c,now):
        # One explicit owner-authorized continuation, not automatic budget scaling.
        if os.getenv('RSYA_CONTINUE_AFTER_20261005','').lower()!='true':return
        key='rsya_continuation';plan=await self.state(c,key)
        if not plan:
            plan={'state':'READY','campaign_id':715029848,'after_msk':'2026-10-06T00:00:00+03:00',
                  'weekly_budget_ex_vat_rub':1000,'cpa_rub':100,'autofunding':False,'auto_budget_increase':False,
                  'provider_acceptance':'pending scheduled API read-back; never retry at a higher budget'}
            await self.put(c,key,plan)
        if plan['state']!='READY' or now.astimezone(MOSCOW).date().isoformat()<'2026-10-06':return
        if await self.state(c,'cpa_owner_paused'):return
        health=await self.state(c,'cpa:715029848') or {}
        if not technical_paid_health(health.get('health_checks',{})):return
        if not health.get('checked_at') or now-datetime.fromisoformat(health['checked_at'])>timedelta(hours=2):return
        balance=await self.cpa_balance()
        if balance is None or balance<100:return
        campaign=(await self.api('campaigns','get',{'SelectionCriteria':{'Ids':[715029848]},
            'FieldNames':['Id','State','Status','EndDate'],'UnifiedCampaignFieldNames':['BiddingStrategy','CounterIds']}))['Campaigns'][0]
        strategy=campaign['UnifiedCampaign']['BiddingStrategy'];pay=strategy.get('Network',{}).get('PayForConversion',{})
        if (campaign['State']!='ON' or campaign['Status']!='ACCEPTED' or strategy['Network']['BiddingStrategyType']!='PAY_FOR_CONVERSION'
            or pay.get('GoalId')!=PAID_GOAL or pay.get('Cpa')!=100000000 or campaign['EndDate']!='2026-10-05'):
            plan.update(state='BLOCKED',reason='campaign_changed_or_owner_paused_no_auto_resume')
            await self.put(c,key,plan)
            await self.notify(c,'rsya-continuation-blocked','⚠️ Продолжение РСЯ №715029848 требует проверки: исходные настройки изменились. Новый дубль и пополнение не создавались.')
            return
        import copy
        strategy=copy.deepcopy(strategy)
        strategy['Network']['PayForConversion'].update(BudgetType='WEEKLY_BUDGET',WeeklySpendLimit=1000000000,CustomPeriodBudget=None)
        aid=await c.fetchval("INSERT INTO profit_cpa_actions(campaign_id,action,reason,before_cpa,after_cpa,state) VALUES(715029848,'CONTINUE','owner_authorized_after_test',100,100,'prepared') RETURNING id")
        plan.update(state='CLAIMED',claimed_at=now.isoformat());await self.put(c,key,plan)
        try:
            if not self.writes:raise DirectError('live_writes_disabled')
            result=await self.api('campaigns','update',{'Campaigns':[{'Id':715029848,'EndDate':None,'UnifiedCampaign':{'BiddingStrategy':strategy}}]})
            items=result.get('UpdateResults',[])
            if len(items)!=1 or items[0].get('Errors'):raise DirectError('continuation_rejected_no_budget_raise')
            saved=(await self.api('campaigns','get',{'SelectionCriteria':{'Ids':[715029848]},'FieldNames':['Id','EndDate'],'UnifiedCampaignFieldNames':['BiddingStrategy']}))['Campaigns'][0]
            p=saved['UnifiedCampaign']['BiddingStrategy']['Network']['PayForConversion']
            if saved.get('EndDate') or p.get('WeeklySpendLimit')!=1000000000 or p.get('Cpa')!=100000000 or p.get('GoalId')!=PAID_GOAL:raise DirectError('continuation_readback_unknown')
            await c.execute("UPDATE profit_cpa_actions SET state='applied',updated_at=NOW() WHERE id=$1",aid)
            plan.update(state='APPLIED',applied_at=now.isoformat());await self.put(c,key,plan)
            await self.put(c,'rsya_continuation_applied',plan)
            await self.notify(c,'rsya-continuation-applied','📣 РСЯ №715029848 продолжена в той же кампании: VERIFIED payment_success, CPA 100 ₽, недельный лимит 1 000 ₽ без НДС. Автопополнение и увеличение бюджета выключены.')
        except Exception as exc:
            await c.execute("UPDATE profit_cpa_actions SET state='unknown',updated_at=NOW() WHERE id=$1",aid)
            plan.update(state='BLOCKED',reason=getattr(exc,'code',type(exc).__name__))
            await self.put(c,key,plan)
            await self.notify(c,'rsya-continuation-blocked','⚠️ Директ не подтвердил продолжение РСЯ №715029848 с лимитом 1 000 ₽. Бюджет не повышался; проверьте сохранённое состояние. Автопополнение выключено.')

    async def cpa_monitor(self,c,now):
        try:
            demand=await wordstat_demand.sync(self,c,now)
        except Exception:
            demand={'state':'WORDSTAT_DATA_DEGRADED','demand_exists':False}
            await self.put(c,'wordstat_demand',demand)
        hour=now.replace(minute=0,second=0,microsecond=0)
        monitor=await self.state(c,'cpa_monitor') or {}
        if monitor.get('hour')==hour.isoformat():return
        await self.rsya_continuation(c,now)
        campaigns=(await self.api('campaigns','get',{'SelectionCriteria':{'Ids':list(CPA_CAMPAIGNS)},
            'FieldNames':['Id','State','Status','EndDate'],'UnifiedCampaignFieldNames':['BiddingStrategy','CounterIds']}))['Campaigns']
        if {x['Id'] for x in campaigns}!=set(CPA_CAMPAIGNS):raise DirectError('cpa_campaigns_missing')
        balance=await self.cpa_balance()
        service=await self.state(c,'service_checks') or {}
        checks=dict(service.get('checks',{}))
        try:
            checkout=await self.http.get('https://xn--163-5cdt3dgrs.xn--p1ai/gocheckout',follow_redirects=True)
            checks['checkout']=checkout.status_code==200 and ('checkout' in checkout.text.lower())
            pvz=await self.http.get('https://ozon-delivery-gateway-production.up.railway.app/api/ozon/points',params={'city':'Самара','query':'Самара','limit':1})
            checks['pvz']=pvz.status_code==200 and bool(pvz.json().get('items'))
            metrika=await self.http.get('https://api-metrika.yandex.net/management/v1/counter/112544007/goals',headers={'Authorization':'OAuth '+os.getenv('METRIKA_OAUTH_TOKEN','')})
            checks['metrika_goal']=metrika.status_code==200 and any(g.get('id')==PAID_GOAL for g in metrika.json().get('goals',[]))
        except Exception:checks['external_health']=False
        checks['paid_server_enabled']=os.getenv('METRIKA_PAID_ENABLED','').lower()=='true'
        checks['paid_dedupe_ready']=bool(await c.fetchval('''SELECT count(*)=2 FROM pg_indexes WHERE schemaname='public' AND tablename='commerce_paid_conversions' AND indexdef LIKE 'CREATE UNIQUE INDEX%' AND (indexdef LIKE '%(order_id)%' OR indexdef LIKE '%(payment_id)%')'''))
        checks['paid_delivery_proven']=bool(await c.fetchval("SELECT EXISTS(SELECT 1 FROM commerce_paid_conversions m JOIN commerce_pending_orders p USING(order_id) WHERE m.state='processed' AND NOT p.is_test AND NOT p.is_internal)"))
        checks['no_failed_paid_uploads']=not await c.fetchval("SELECT EXISTS(SELECT 1 FROM commerce_paid_conversions WHERE state IN ('linkage_failure','unknown','failed') OR (state='claimed' AND updated_at<NOW()-INTERVAL '15 minutes'))")
        checks['no_mass_errors']=not await c.fetchval("SELECT count(*)>=10 FROM profit_funnel_events WHERE occurred_at>NOW()-INTERVAL '1 hour' AND name IN ('JS_ERROR','PAYMENT_ERROR','PVZ_TIMEOUT')")
        today=now.astimezone(MOSCOW).date();start=today-timedelta(days=6)
        cost_start=datetime.combine(start,datetime.min.time(),MOSCOW)
        await self.actual_payment_costs(c,cost_start,now)
        await self.actual_returns(c,cost_start)
        pause=bool(await self.state(c,'cpa_owner_paused'))
        summary=[]
        for campaign in campaigns:
            cid=campaign['Id'];side=CPA_CAMPAIGNS[cid];opposite='Network' if side=='Search' else 'Search'
            strategy=campaign['UnifiedCampaign']['BiddingStrategy'];pay=strategy.get(side,{}).get('PayForConversion',{})
            current=Decimal(str(pay.get('Cpa',0)))/1000000
            verified=(strategy.get(side,{}).get('BiddingStrategyType')=='PAY_FOR_CONVERSION' and pay.get('GoalId')==PAID_GOAL
                and strategy.get(opposite,{}).get('BiddingStrategyType')=='SERVING_OFF'
                and campaign['UnifiedCampaign'].get('CounterIds',{}).get('Items')==[112544007])
            last=await c.fetchval("SELECT max(created_at) FROM profit_cpa_actions WHERE campaign_id=$1 AND state='applied' AND action='SET'",cid)
            unresolved=await c.fetchval("SELECT EXISTS(SELECT 1 FROM profit_cpa_actions WHERE campaign_id=$1 AND state IN ('prepared','unknown'))",cid)
            stats=await self.report(start,today,campaign_id=cid)
            if side == 'Search':
                # The live loop uses cpa_monitor, not the retired CPC hourly path.
                # Reuse its Search report and isolate diagnostic failures from ads.
                await self.join_wordstat(c, demand, stats, cost_start, now, checks)
            spend=sum(Decimal(str(x['Cost'])) for x in stats);clicks=sum(x['Clicks'] for x in stats);impressions=sum(x['Impressions'] for x in stats)
            rows=await c.fetch('''SELECT p.amount,p.quantity,p.created_at,k.yookassa,k.ozon,k.returns_other,p.ozon_status
                FROM commerce_pending_orders p JOIN insales_yookassa_payments y ON y.payment_id=p.payment_id
                LEFT JOIN profit_controller_costs k ON k.order_id=p.order_id
                WHERE p.payment_status='succeeded' AND y.status='succeeded' AND NOT p.is_test AND NOT p.is_internal
                AND p.created_at >= $1 AND p.snapshot->'attribution'->>'utm_campaign'=$2''',datetime.combine(start,datetime.min.time(),MOSCOW),str(cid))
            paid=len(rows);paid24=sum(r['created_at']>=now-timedelta(hours=24) for r in rows)
            # No estimated transport or unobserved returns/write-offs become zero.
            economic_max=None
            risk=await self.state(c,'confirmed_return_writeoff_risk')
            if risk and risk.get('source') and risk.get('rub_per_order') is not None and len(rows)>=10 and all(r['ozon_status']=='delivered' and all(r[k] is not None for k in ('yookassa','ozon','returns_other')) for r in rows):
                margins=[Decimal(str(r['amount']))*Decimal('.94')-r['quantity']*COGS_UNIT_RUB-sum(Decimal(str(r[k])) for k in ('yookassa','ozon','returns_other')) for r in rows]
                economic_max=max(Decimal(0),min(margins)-Decimal(str(risk['rub_per_order'])))
            previous=await self.state(c,'cpa:'+str(cid)) or {}
            first=previous.get('first_observed_at',now.isoformat());observed=(now-datetime.fromisoformat(first)).total_seconds()/86400
            probe=prepaid_delivery_window(previous,now,verified and technical_paid_health(checks)
                and campaign['State']=='ON' and campaign['Status']=='ACCEPTED' and not pause and not unresolved,stats)
            data={'paused':pause,'strategy_verified':verified,'balance':balance,'health':technical_paid_health(checks),
                'economic_max':economic_max,'paid_7d':paid,'clicks_7d':clicks,'cac_paid':float(spend/paid) if paid else None,
                'observed_days':observed,'delivery_limited':impressions<100,'attribution_complete':paid>=3 and checks['paid_delivery_proven'],
                'prepaid_probe_ready':probe['ready'],
                'demand_exists':demand.get('demand_exists') is True}
            state,target,reason=cpa_decision(current,data,now,last)
            emergency=(not verified or service.get('failures',0)>=2 or (cid==715029848 and not (await self.state(c,'rsya_continuation_applied')) and spend>=Decimal(900)))
            if emergency and campaign['State']=='ON' and not unresolved and not pause:
                if await self.cpa_write(c,campaign,'SUSPEND','paid_strategy_health_or_period_budget_guard'):
                    state,target,reason='STOPPED',None,'emergency_guard'
                    await self.notify(c,'cpa-emergency-'+str(cid)+'-'+today.isoformat(),
                        '⚠️ Кампания '+str(cid)+' остановлена: проверка PAID-стратегии, работоспособности или лимита теста не прошла. Автоматического возобновления нет.')
            if unresolved:state,target,reason='BLOCKED',None,'ambiguous_previous_write'
            if campaign['State']!='ON':state,target,reason='HOLD',None,'campaign_not_running_no_auto_resume'
            if campaign.get('EndDate') and today.isoformat()>campaign['EndDate']:state,target,reason='STOPPED',None,'authorized_period_ended'
            evaluated=previous.get('evaluated_at');due=not evaluated or now-datetime.fromisoformat(evaluated)>=timedelta(hours=6)
            if due and state in ('SET','SUSPEND'):
                if await self.cpa_write(c,campaign,state,reason,target):
                    if target is not None:current=target
                    last=now
            agent_state=('ACTIVE' if state in ('HOLD','SET') and campaign['State']=='ON' else
                'WAITING' if state=='WAITING_FOR_FUNDS' else 'PAUSED' if state in ('PAUSED','STOPPED') or campaign['State']!='ON' else 'ERROR')
            if state=='WAITING_FOR_FUNDS':
                await self.notify(c,'cpa-funds-'+today.isoformat(),'💰 Яндекс Директ: баланс недостаточен для текущего CPA. Нужно пополнение владельца. Автопополнение выключено.')
            record={'campaign_id':cid,'channel':'Поиск' if side=='Search' else 'РСЯ','current_cpa':float(current),
                'agent_state':agent_state,'campaign_state':campaign['State'],'moderation':campaign['Status'],'impressions_7d':impressions,'clicks_7d':clicks,'sample':'LOW SAMPLE' if paid<3 else 'OBSERVED','paid_24h':paid24,'paid_7d':paid,'spend_7d':float(spend),'cac_paid':data['cac_paid'],'state':state,
                'reason':reason,'last_change':last.isoformat() if last else None,'checked_at':now.isoformat(),
                'evaluated_at':now.isoformat() if due else evaluated,'first_observed_at':first,
                'economic_max':float(economic_max) if economic_max is not None else None,'health_checks':checks,
                'delivery_probe_policy_version':probe['delivery_probe_policy_version'],
                'delivery_probe_since':probe['delivery_probe_since'],'delivery_probe_window':probe,
                'economics_status':'UNKNOWN' if economic_max is None else 'OBSERVED',
                'wordstat_state':demand.get('state'), 'demand_exists':data['demand_exists'],
                'balance':float(balance) if balance is not None else None,'budget_unchanged':True}
            await self.put(c,'cpa:'+str(cid),record);summary.append(record)
        await self.put(c,'cpa_monitor',{'hour':hour.isoformat(),'campaigns':summary,'min_cpa':100,'max_cpa':350,'step':25,'cooldown_hours':24,'no_funding':True,'state':'ACTIVE' if all(x['agent_state']=='ACTIVE' for x in summary) else 'ERROR' if any(x['agent_state']=='ERROR' for x in summary) else 'PAUSED' if any(x['agent_state']=='PAUSED' for x in summary) else 'WAITING'})

    async def join_wordstat(self, c, demand, stats, start, now, checks):
        try:
            sessions = [dict(r) for r in await c.fetch('''SELECT attribution,is_test,is_internal,traffic_class
                FROM profit_funnel_sessions WHERE started_at >= $1 AND started_at < $2''', start, now)]
            orders = await self.order_cohort(c, start, now)
            joined = wordstat_demand.joined_metrics(demand, stats, sessions, orders, start, now,
                healthy=technical_paid_health(checks))
            await self.put(c, 'wordstat_demand', joined)
            await self.put(c, 'wordstat_join_health', {'state': 'OBSERVED', 'joined_at': joined['joined_at']})
        except Exception as exc:
            await self.put(c, 'wordstat_join_health', {'state': 'DRY_RUN', 'error': type(exc).__name__,
                'checked_at': now.isoformat()})

    async def loop(self):
        while True:
            try:
                async with self.pool.acquire() as c:
                    locked = await c.fetchval('SELECT pg_try_advisory_lock($1)',LOCK)
                    if locked:
                        try:
                            await self.schema(c)
                            now = datetime.now(timezone.utc)
                            result = await self.api('campaigns','get',{'SelectionCriteria':{'Ids':[CAMPAIGN]},'FieldNames':['Id','State','Status']})
                            campaigns = result.get('Campaigns',[])
                            if len(campaigns)!=1 or campaigns[0]['Id']!=CAMPAIGN: raise DirectError('campaign_missing')
                            today = now.astimezone(MOSCOW).date()
                            daily = await self.report(today,today)
                            paused = await self.guard(c,campaigns[0],sum(r['Cost'] for r in daily),now)
                            service_bad = await self.service_guard(c,campaigns[0],now)
                            paused = paused or service_bad
                            await self.cpa_monitor(c,now)
                            if not (os.getenv('PROFIT_FUNNEL_ENABLED','').lower()=='true' and os.getenv('PROFIT_FUNNEL_BOT_TOKEN')):
                                await self.weekly(c,now)
                            await self.daily_shipments(c,now)
                            self.status={'state':'running','checked_at':now.isoformat(),'live_writes':self.writes,
                                         'campaigns':list(CPA_CAMPAIGNS),'mode':'VERIFIED_PAID_CPA',
                                         'min_cpa_rub':100,'max_cpa_rub':350,'step_rub':25,
                                         'daily_spend_cap_rub':10000,'ordinary_cooldown_hours':24}
                            await self.put(c,'health',self.status)
                        finally:
                            await c.fetchval('SELECT pg_advisory_unlock($1)',LOCK)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status={'state':'blocked','error':getattr(exc,'code',type(exc).__name__),'live_writes':self.writes}
                # Do not emit API responses, credentials, queries or customer data into logs.
                print('PROFIT_CONTROLLER_STATE=blocked code='+str(self.status['error']))
            await asyncio.sleep(300)  # Daily guard every five minutes; full collection once/hour.
