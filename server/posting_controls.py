"""Controls for the existing external agent, through the existing owner bot."""
import json
import re

COMMANDS = ['posting_status', 'posting_pause', 'posting_resume', 'posting_blacklist']

# Read-only last-touch reporting over the existing worker and commerce tables.
# Both succeeded statuses are written only by app.py's API-validated paid handler.
METRICS_SQL = """WITH successful AS (
 SELECT platform_id,MIN(posted_at) AS posted_at FROM handle_autopost_posts
 WHERE status='SUCCESS' AND posted_at IS NOT NULL
 AND external_message_id IS NOT NULL AND external_url IS NOT NULL GROUP BY platform_id
), sessions AS (
 SELECT s.session_id,p.platform_id FROM profit_funnel_sessions s JOIN successful p
 ON s.attribution->>'utm_campaign'='external_groups'
 AND s.attribution->>'utm_source'='telegram'
 AND s.attribution->>'utm_content'='group_'||p.platform_id::text
 WHERE NOT s.is_test AND NOT s.is_internal AND s.traffic_class='customer'
 AND s.started_at>=p.posted_at AND s.started_at>=NOW()-interval '28 days'
), visits AS (
 SELECT platform_id,COUNT(DISTINCT session_id) AS sessions FROM sessions GROUP BY platform_id
), events AS (
 SELECT s.platform_id,COUNT(DISTINCT s.session_id) FILTER(WHERE e.name='BUY_BUTTON_CLICK') AS buy,
 COUNT(DISTINCT s.session_id) FILTER(WHERE e.name='CHECKOUT_OPEN') AS checkout
 FROM sessions s LEFT JOIN profit_funnel_events e USING(session_id) GROUP BY s.platform_id
), verified AS (
 SELECT p.platform_id,o.order_id,o.amount,
 CASE WHEN k.yookassa IS NOT NULL AND k.ozon IS NOT NULL AND k.returns_other IS NOT NULL
 AND cfg.value->>'cogs_unit_rub' IS NOT NULL AND cfg.value->>'managerial_tax_rate' IS NOT NULL
 THEN o.amount-o.quantity*(cfg.value->>'cogs_unit_rub')::numeric
 -o.amount*(cfg.value->>'managerial_tax_rate')::numeric-k.yookassa-k.ozon-k.returns_other END AS contribution
 FROM commerce_pending_orders o JOIN insales_yookassa_payments y
 ON y.payment_id=o.payment_id AND y.order_id=o.order_id
 JOIN successful p ON o.snapshot->'attribution'->>'utm_campaign'='external_groups'
 AND o.snapshot->'attribution'->>'utm_source'='telegram'
 AND o.snapshot->'attribution'->>'utm_content'='group_'||p.platform_id::text
 LEFT JOIN profit_controller_costs k ON k.order_id=o.order_id
 LEFT JOIN profit_controller_state cfg ON cfg.key='economics_config'
 WHERE o.payment_status='succeeded' AND y.status='succeeded' AND y.error_code IS NULL
 AND o.amount=y.amount AND o.quantity=y.quantity AND o.amount>0 AND o.quantity>0
 AND NOT o.is_test AND NOT o.is_internal AND o.traffic_class='customer'
 AND o.created_at>=p.posted_at AND o.created_at>=NOW()-interval '28 days'
), paid AS (
 SELECT platform_id,COUNT(DISTINCT order_id) AS paid,SUM(amount) AS revenue,
 COUNT(*) FILTER(WHERE contribution IS NULL) AS unknown_cost_orders,
 CASE WHEN COUNT(contribution)=COUNT(*) THEN SUM(contribution) END AS contribution
 FROM verified GROUP BY platform_id
)
SELECT g.username,p.platform_id,COALESCE(v.sessions,0) AS sessions,
 COALESCE(e.buy,0) AS buy,COALESCE(e.checkout,0) AS checkout,
 COALESCE(d.paid,0) AS paid,COALESCE(d.revenue,0) AS revenue,
 COALESCE(d.unknown_cost_orders,0) AS unknown_cost_orders,
 CASE WHEN d.paid IS NULL THEN 0 ELSE d.contribution END AS contribution
FROM successful p JOIN handle_autopost_platforms g ON g.id=p.platform_id
LEFT JOIN visits v USING(platform_id) LEFT JOIN events e USING(platform_id)
LEFT JOIN paid d USING(platform_id) ORDER BY p.platform_id"""


def metric_decision(row):
    if row['sessions'] < 20 or row['paid'] < 3:
        return 'LOW SAMPLE — без оптимизации'
    if row['unknown_cost_orders'] or row['contribution'] is None:
        return 'UNKNOWN costs — без оптимизации'
    if row['contribution'] <= 0:
        return 'Неположительный вклад — проверить вручную, не расширять'
    return 'Достаточная выборка для ручного анализа; правила группы обязательны'


async def metrics_report(connection):
    ready = await connection.fetchval("""SELECT bool_and(to_regclass(name) IS NOT NULL)
      FROM unnest(ARRAY['profit_funnel_sessions','profit_funnel_events','commerce_pending_orders',
      'insales_yookassa_payments','profit_controller_costs','profit_controller_state']) name""")
    if not ready:
        return '\nАналитика: UNKNOWN — рабочие таблицы метрик недоступны.'
    rows = await connection.fetch(METRICS_SQL)
    if not rows:
        return '\nАналитика: WAITING_FIRST_SUCCESS. Нет подтверждённой публикации; оптимизация запрещена.'
    lines=['\nАналитика за 28 дней, последнее касание:']
    for row in rows[:10]:
        contribution='UNKNOWN' if row['contribution'] is None else f"{row['contribution']:.2f} ₽"
        lines.append(f"@{row['username']}: визиты {row['sessions']}, Купить {row['buy']}, checkout {row['checkout']}, VERIFIED PAID {row['paid']}, выручка {row['revenue']:.2f} ₽, вклад {contribution}. {metric_decision(row)}")
    if len(rows)>10:lines.append(f'Показаны первые 10 из {len(rows)} групп.')
    lines.append('Owner/test/unknown исключены. Неизвестные расходы не равны нулю. Этот отчёт не меняет частоту/правила публикаций.')
    return '\n'.join(lines)


def blacklist_target(text):
    parts = text.strip().split(maxsplit=2)
    if len(parts) != 3:
        raise ValueError('Укажите username группы и конкретную причину жалобы.')
    username = parts[1].removeprefix('https://t.me/').lstrip('@').lower()
    reason = parts[2].strip()
    if not re.fullmatch(r'[a-z0-9_]{5,32}', username) or not 3 <= len(reason) <= 500:
        raise ValueError('Нужен корректный Telegram username и причина длиной 3–500 символов.')
    return username, reason


async def handle(connection, command, text):
    ready = await connection.fetchval("SELECT to_regclass('handle_autopost_platforms') IS NOT NULL AND to_regclass('handle_autopost_posts') IS NOT NULL AND to_regclass('content_projects') IS NOT NULL")
    if not ready:
        return 'Внешний агент недоступен: рабочие таблицы не найдены. Изменения не внесены.'
    if command in ('posting_pause', 'posting_resume'):
        paused = command == 'posting_pause'
        changed = await connection.fetchval("""UPDATE content_projects SET platform_config=jsonb_set(
            COALESCE(platform_config,'{}'::jsonb),'{external_telegram_paused}',$1::jsonb,true)
            WHERE id='handles_media' RETURNING id""", json.dumps(paused))
        if not changed:
            return 'Проект внешнего агента не найден. Изменения не внесены.'
        return ('Внешний агент приостановлен. Новый выбор групп и новые резервации запрещены. Уже зарезервированная или начатая отправка может завершиться; повторять её нельзя.' if paused else
                'Ручная пауза внешнего агента снята. Workflow не активируется этой командой; правила, blacklist и защита от дублей сохранены.')
    if command == 'posting_blacklist':
        try:
            username, reason = blacklist_target(text)
        except ValueError as error:
            return str(error)
        row = await connection.fetchrow("""UPDATE handle_autopost_platforms SET status='BLACKLISTED',
            reason=$2,lease_owner=NULL,lease_until=NULL,last_checked_at=NOW(),
            evidence=COALESCE(evidence,'{}'::jsonb)||jsonb_build_object('owner_complaint',$2,'blacklisted_at',NOW())
            WHERE lower(username)=$1 RETURNING id,name,username""", username, reason)
        if not row:
            return 'Эта группа не найдена в рабочей базе. Другие группы не изменены.'
        in_flight = await connection.fetchval("SELECT EXISTS(SELECT 1 FROM handle_autopost_posts WHERE platform_id=$1 AND status='SENDING')", row['id'])
        return f"Группа @{row['username']} внесена в постоянный BLACKLISTED. Повторное discovery не снимает блокировку." + (' Запрос Telegram уже мог быть отправлен; его результат нужно проверить, повтор запрещён.' if in_flight else '')
    if command != 'posting_status':
        return 'Неизвестная команда внешнего агента.'
    row = await connection.fetchrow("""SELECT
        (SELECT platform_config->>'external_telegram_paused' FROM content_projects WHERE id='handles_media')='true' AS paused,
        (SELECT count(*) FROM handle_autopost_platforms WHERE status='BLACKLISTED') AS blacklisted,
        (SELECT count(*) FROM handle_autopost_platforms WHERE status='NEED_ADMIN') AS need_admin,
        count(*) FILTER(WHERE status='SUCCESS' AND posted_at>=date_trunc('day',NOW() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow') AS today_success,
        count(DISTINCT platform_id) FILTER(WHERE status='SUCCESS' AND posted_at>=date_trunc('day',NOW() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow') AS today_groups,
        count(*) FILTER(WHERE status='SENDING' OR outcome_unknown) AS needs_review
        FROM handle_autopost_posts""")
    return ('ВНЕШНИЙ TELEGRAM-АГЕНТ\nРучная пауза: '+('ДА' if row['paused'] else 'НЕТ')+
            f"\nСегодня МСК: {row['today_success']} подтверждённых публикаций в {row['today_groups']} группах."
            f"\nNEED_ADMIN: {row['need_admin']}\nBLACKLISTED: {row['blacklisted']}\nНеясная/текущая отправка: {row['needs_review']}"
            '\nПауза здесь не означает статус активации n8n. SUCCESS учитывается только с фактической датой публикации.') + await metrics_report(connection)
