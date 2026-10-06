"""Controls for the existing external agent, through the existing owner bot."""
import json
import re

COMMANDS = ['posting_status', 'posting_pause', 'posting_resume', 'posting_blacklist']


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
        return ('Внешний агент приостановлен. Новые резервации и отправки запрещены. Уже отправленный запрос Telegram отменить нельзя.' if paused else
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
            '\nПауза здесь не означает статус активации n8n. SUCCESS учитывается только с фактической датой публикации.')
