"""Russian, mobile-first presentation of existing reports; no financial writes."""
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MSK=ZoneInfo('Europe/Moscow')
UNKNOWN='🟡 Пока нет данных'
LOW='🟡 Мало данных для надёжного сравнения'
SEP='──────────────'
MENU=[('today','📊 Сегодня'),('yesterday','📅 Вчера'),('week','📆 Неделя'),
      ('funnel','🧭 Воронка'),('profit','💰 Прибыль'),('ads','📣 Реклама'),
      ('devices','📱 Устройства'),('browsers','🌐 Браузеры'),('speed','⚡ Скорость'),
      ('orders','📦 Заказы'),('returns','↩️ Возвраты'),('stock','🏪 Склад'),
      ('errors','🛠 Ошибки'),('status','❤️ Система')]
TITLES=dict(MENU)|{'menu':'📊 ПУЛЬТ ВЛАДЕЛЬЦА','behavior':'⏱ Поведение','attention':'🎯 Требует внимания'}
STAGES={'SITE_SESSION':'👥 Посетили сайт','PRODUCT_VIEW':'👁 Посмотрели товар',
        'BUY_BUTTON_CLICK':'🛒 Нажали «Купить»','CHECKOUT_OPEN':'📝 Открыли оформление',
        'CONTACTS_STARTED':'📝 Начали ввод данных','CONTACTS_COMPLETED':'📝 Заполнили данные',
        'PVZ_PICKER_OPEN':'📍 Открыли выбор ПВЗ','PVZ_LOADED':'📍 Пункты загрузились',
        'PVZ_SELECTED':'✅ Выбрали ПВЗ','PAYMENT_STARTED':'💳 Начали оплату',
        'PAYMENT_SUCCESS':'💰 Оплатили','ORDER_RECEIVED':'📦 Получили заказ'}
STATUSES={'ORDERED':'📦 Оформлено','PAID':'💳 Оплачено','IN_TRANSIT':'🚚 В пути',
          'READY':'📍 В ПВЗ','RECEIVED':'✅ Получено','CANCELLED':'❌ Отменено',
          'NOT_PICKED_UP':'↩️ Не забрали','RETURNED_RESELLABLE':'✅ Вернулось целым',
          'RETURNED_DAMAGED':'⚠️ Вернулось повреждённым'}
MONTHS=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']

def number(v,digits=0,suffix=''):
    if v is None:return UNKNOWN
    try:
        n=float(v)
        if not math.isfinite(n):return UNKNOWN
        return (f'{n:,.{digits}f}'.replace(',',' ').replace('.',',')+suffix)
    except (ValueError,TypeError):return UNKNOWN

def rub(v):return number(v,2,' ₽')
def percent(v):return number(v,1,'%')
def date_text(value):
    d=datetime.fromisoformat(value).astimezone(MSK)
    return f'{d.day} {MONTHS[d.month-1]}'

def period_text(r):
    start=r['start_msk'];end=datetime.fromisoformat(r['end_msk_exclusive'])-timedelta(days=1)
    when=date_text(start)
    if end.date()!=datetime.fromisoformat(start).date():when+=' — '+date_text(end.isoformat())
    updated=datetime.fromisoformat(r['generated_at']).astimezone(MSK).strftime('%H:%M')
    return f'📅 {when} • данные на {updated} МСК'

def status_line(e,key):
    v=e['statuses'][key]
    return f'{STATUSES[key]}: {number(v["orders"])}\n{number(v["units"])} шт. • {rub(v["rub"])}'

def sales(r):
    e=r['economics'];lines=['💰 ПРОДАЖИ']
    for k in ('PAID','RECEIVED','IN_TRANSIT','READY'):
        if e['statuses'][k]['orders'] or k=='PAID':lines.append(status_line(e,k))
    if e['statuses']['CANCELLED']['orders']:lines.append(status_line(e,'CANCELLED'))
    if not e['statuses']['ORDERED']['orders']:lines.append('📦 За этот период новых заказов нет')
    lines.append('Статусы — по заказам, созданным в этом периоде.')
    return lines

def advertising(r,detail=False):
    a=r['ads'];lines=['📣 РЕКЛАМА']
    if a.get('quality')!='CONFIRMED':return lines+[UNKNOWN]
    channels=a.get('channels',{});imps=sum(v['impressions'] for v in channels.values());clicks=sum(v['clicks'] for v in channels.values());spend=sum(v['spend'] for v in channels.values())
    if not detail:
        lines += [f'👁 Показы: {number(imps)}',f'👆 Клики: {number(clicks)}',f'💸 Расход: {rub(spend)}',f'💰 Средний клик: {rub(spend/clicks if clicks else None)}']
    for key,title in [('YANDEX_SEARCH','🔎 Яндекс Поиск'),('YANDEX_RSYA','📣 РСЯ')]:
        v=channels.get(key)
        if not v:lines += [title,UNKNOWN];continue
        if not detail:lines.append(f'{title}: {number(v["clicks"])} клика • {rub(v["spend"])}');continue
        lines += ['',title,f'👁 Показы: {number(v["impressions"])}',f'👆 Клики: {number(v["clicks"])}',f'📊 CTR: {percent(v["ctr"])}',f'💸 Расход: {rub(v["spend"])}',f'💰 Средний клик: {rub(v["cpc"])}']
        attributed=r.get('channel_attribution',{}).get(key)
        if attributed:
            lines += [f'📦 Связанные заказы: {number(attributed["orders"])}',f'💳 Связанные оплаты: {number(attributed["paid"])}',f'🎯 Стоимость связанной оплаты: {rub(attributed["cac"])}']
        else:lines.append('🏷 Связь клика с оплатой: пока нет данных')
    if detail:
        actions=r.get('controller_actions',[])
        def count(names):return sum(v['count'] for v in actions if v['action'] in names and v['state'] in ('applied','success','succeeded'))
        lines += ['', '🤖 КОНТРОЛЛЕР СТАВОК',f'🔧 Изменил ставки: {count(("set",))}',
                  '📈 Повышения / 📉 снижения: пока нет отдельной детализации.',
                  f'⏸ Приостановил: {count(("suspend",))}',
                  '🔎 Оставил без изменений: '+number(sum(v['count'] for v in actions if v['action']=='hold'))]
    return lines+['Расход по данным Direct, включая НДС.']

def economy(r,detail=False):
    e=r['economics'];lines=['💰 ЭКОНОМИКА']
    received=e['statuses']['RECEIVED']['rub']
    if detail:
        lines += [f'💵 Полученная выручка: {rub(received)}',f'📦 Себестоимость: {rub(e["cogs_received"])}','230 ₽/шт. уже включает упаковку, обработку и труд.',f'🧾 YooKassa: {rub(e["yookassa_actual"])}',f'🚚 Доставка покупателям: {rub(e["ozon_outbound_actual"])}']
        if e.get('ozon_return_actual') or e.get('return_condition_unknown'):lines.append(f'↩️ Обратная доставка: {rub(e["ozon_return_actual"])}')
        lines += [f'📣 Реклама: {rub(e["advertising"])}',f'💰 Налог с полученных заказов: {rub(e["tax_received"])}']
        if e['writeoff']:lines.append(f'❌ Списания: {rub(e["writeoff"])}')
        if e['extra_confirmed']:lines.append(f'🔎 Дополнительные расходы: {rub(e["extra_confirmed"])}')
        if e.get('refunds_actual'):lines.append(f'↩️ Возвращено покупателям: {rub(e["refunds_actual"])}')
    if e['final_net_profit'] is not None:lines.append(f'💰 Чистая прибыль: {rub(e["final_net_profit"])}')
    else:
        lines += [f'💰 Предварительная прибыль: {rub(e["profit_before_unknown"])}','🟡 Финальная прибыль пока не подтверждена.']
        if e['ozon_outbound_actual'] is None:lines.append('🚚 Ожидаем фактическую стоимость доставки Ozon.')
        if e['ozon_return_actual'] is None:lines.append('↩️ Ожидаем фактическую стоимость обратной доставки.')
        if e.get('refunds_actual') is None:lines.append('↩️ Сумма возврата денег пока не подтверждена.')
        if e.get('advertising') is None:lines.append('📣 Фактический расход рекламы пока не получен.')
    if detail:
        lines += [f'🎯 Расход рекламы на оплату по периоду: {rub(e["cac"].get("PAID"))}',f'📊 Оплаченная выручка / реклама: {number(e["roas_paid"],2)}',f'📊 Возврат на рекламные расходы: {percent(e["romi"])}','🟡 Эти показатели по периоду не доказывают рекламную атрибуцию.']
    return lines

def attention(r):
    f=r['instrumented_funnel'];drop=f.get('biggest_drop');lines=['🎯 ГЛАВНОЕ ВНИМАНИЕ']
    if not drop:return lines+[LOW,'🎯 Главная задача: накопить данные, не делать выводы по нескольким визитам.']
    lines += [STAGES.get(drop['from'],'Начало этапа')+' → '+STAGES.get(drop['to'],'Завершение этапа'),f'📉 Не дошли дальше: {number(drop["lost"])} • {percent(drop["drop_percent"])}']
    compare=r.get('comparison',{})
    if compare.get('quality')!='CONFIRMED':lines.append('📆 Для сравнения с обычной неделей пока мало данных.')
    else:
        baseline=next((v for v in compare.get('drop_comparison',[]) if v['from']==drop['from'] and v['to']==drop['to']),None)
        if baseline:
            lines += ['📆 Обычная потеря за 7 дней: '+percent(baseline['seven_day_percent']),
                      '📊 Разница: '+number(baseline['difference_percentage_points'],1,' п.п.')]
    estimate=r.get('estimated_lost_revenue')
    if estimate:lines += ['💸 Возможная недополученная выручка: ≈ '+rub(estimate['rub']),'🟡 Оценка по обычной конверсии, не фактический убыток.']
    return lines

def funnel_view(r,detail=False):
    f=r['instrumented_funnel'];lines=['🧭 ВОРОНКА']
    names=['SITE_SESSION','BUY_BUTTON_CLICK','CHECKOUT_OPEN','PVZ_PICKER_OPEN','PVZ_SELECTED','PAYMENT_STARTED','PAYMENT_SUCCESS']
    for name in names:
        n=f['counts'].get(name) if f['sessions'] else None
        lines.append(f'{STAGES[name]}: {number(n)}')
    if f['quality']!='CONFIRMED':lines.append(LOW)
    lines.append('Подробные события собираются с момента подключения; прошлые пробелы не считаются нулями.')
    if detail:
        lines += ['',SEP]+attention(r)
        for d in f['drops']:
            if d['start'] and d['from'] in names and d['to'] in names:
                lines += ['',STAGES[d['from']]+' → '+STAGES[d['to']],f'📊 Дошли: {d["end"]} из {d["start"]} • {percent(d["conversion_percent"])}',f'📉 Потеря: {d["lost"]} • {percent(d["drop_percent"])}']
    return lines

def readable_segment(key):
    parts=key.split('/');device,os,browser=(parts+['']*3)[:3]
    icon='🖥' if device=='DESKTOP' else '🍎' if os in ('iOS','iPadOS') else '🤖' if os=='Android' else '📱'
    label='Компьютер' if device=='DESKTOP' else 'iPhone / iOS' if os=='iOS' else 'iPad' if os=='iPadOS' else os or 'Мобильное устройство'
    browsers={'Yandex':'Яндекс.Браузер','Other':'Другой браузер','Chrome':'Chrome','Safari':'Safari','Edge':'Edge','Firefox':'Firefox'}
    return icon+' '+label+((' / '+browsers.get(browser,'Другой браузер')) if browser else '')

def devices(r,browsers=False):
    lines=['🌐 БРАУЗЕРЫ' if browsers else '📱 УСТРОЙСТВА'];f=r['instrumented_funnel']
    grouped={}
    for key,v in f['segments'].items():
        k=key if browsers else '/'.join(key.split('/')[:2])
        g=grouped.setdefault(k,{'sessions':0,'buy':0,'pvz':0,'paid':0})
        g['sessions']+=v['sessions']
        for label,event in [('buy','BUY_BUTTON_CLICK'),('pvz','PVZ_SELECTED'),('paid','PAYMENT_SUCCESS')]:g[label]+=v['stages'][event]
    if grouped:
        lines.append('По сессиям с полной цепочкой событий:')
        for k,v in sorted(grouped.items(),key=lambda x:-x[1]['sessions']):
            lines += ['',readable_segment(k),f'👥 Визиты: {v["sessions"]}',f'🛒 Купить: {v["buy"]}',f'📍 Выбрали ПВЗ: {v["pvz"]}',f'💰 Оплатили: {v["paid"]}',f'📊 Конверсия: {percent(v["paid"]*100/v["sessions"])}']
            if v['sessions']<20:lines.append(LOW)
    else:lines.append('🟡 Полная цепочка по устройствам пока не накоплена.')
    rows=r.get('metrika',{}).get('devices',{}).get('rows',[])
    if rows:
        lines += ['',SEP,'📊 Визиты по данным Метрики:']
        for row in rows[:8]:
            dims=[x.get('name','') for x in row['dimensions']]
            label=' / '.join(x.replace('Smartphones','Смартфоны').replace('PC','Компьютер').replace('Google Android','Android').replace('Yandex Browser','Яндекс.Браузер').replace('YandexBrowserCorp','Яндекс.Браузер').replace('Mobile Safari','Safari') for x in dims if x)
            lines.append('👥 '+label+': '+number(row['metrics'][0]))
        lines.append('Эти визиты не подставляются в неполную воронку оплат.')
    return lines

def behavior(r):
    f=r['instrumented_funnel'];d=f['duration'];lines=['⏱ ПОВЕДЕНИЕ',f'⏱ Среднее время: {number(d["mean"],1," сек")}',f'🎯 Медиана: {number(d["median"],1," сек")}']
    n=d['n']
    if n:
        for k,v in f['duration_buckets'].items():lines.append('⏱ '+({'<3':'Меньше 3 сек','3–10':'3–10 сек','10–30':'10–30 сек','30–60':'30–60 сек','>60':'Больше минуты'}.get(k,'Интервал наблюдения'))+': '+percent(v*100/n))
    if n<20:lines.append(LOW)
    labels={'SITE_SESSION→BUY_BUTTON_CLICK':'🛒 До «Купить»','SITE_SESSION→CHECKOUT_OPEN':'📝 До оформления','PVZ_PICKER_OPEN→PVZ_SELECTED':'📍 Выбор ПВЗ','PVZ_SELECTED→PAYMENT_STARTED':'💳 До начала оплаты'}
    lines += ['', '⏱ ДО ДЕЙСТВИЯ']
    for key,label in labels.items():lines.append(label+': '+number(f['time_to_action'].get(key,{}).get('median'),1,' сек'))
    lines += ['', '👁 ГЛУБИНА ПРОСМОТРА']
    for n in (25,50,75,90):lines.append(f'👁 До {n}% страницы: '+number(f['scroll'][str(n)] if f['sessions'] else None))
    for key,label in [('new','👥 Новые'),('returning','🔄 Вернувшиеся')]:lines.append(label+': '+number(f['visitor_split'][key]['sessions'] if f['sessions'] else None))
    return lines

def speed(r):
    f=r['instrumented_funnel'];lines=['⚡ СКОРОСТЬ САЙТА','Из реальных замеров в браузере.']
    for device,label in [('MOBILE','📱 Мобильные'),('DESKTOP','🖥 Компьютер'),('TABLET','📱 Планшет')]:
        metrics={name:f['speed'][name][device] for name in ('LCP','INP','CLS','TTFB')}
        if not any(v['n'] for v in metrics.values()):continue
        lines += ['',label]
        for name,v in metrics.items():
            val=v['p75'] if v['p75'] is not None else v['median']
            if name in ('LCP','TTFB'):val=val/1000 if val is not None else None
            suffix=' сек' if name in ('LCP','TTFB') else ' мс' if name=='INP' else ''
            label='Ответ сервера' if name=='TTFB' else name
            lines.append('⚡ '+label+': '+number(val,3 if name=='CLS' else 2,suffix))
        if any(v['n']<20 for v in metrics.values()):lines.append(LOW+'; показана наблюдаемая медиана.')
    if len(lines)==2:lines.append(UNKNOWN)
    lines += ['📱 Отдельных замеров Android / iPhone пока нет — общий мобильный замер не выдаётся за них.']
    for k,v in f['performance_conversion'].items():
        label={'<3s':'меньше 3 сек','3–5s':'3–5 сек','>5s':'больше 5 сек'}.get(k,'')
        lines.append('⏱ Загрузка '+label+': '+percent(v['paid_cr'])+' оплат')
        if v['quality']!='CONFIRMED':lines.append(LOW)
    return lines

def returns(r):
    e=r['economics'];s=e['statuses'];total=s['RETURNED_RESELLABLE']['orders']+s['RETURNED_DAMAGED']['orders']+e['return_condition_unknown']
    if not total and not s['NOT_PICKED_UP']['orders']:return ['↩️ ВОЗВРАТЫ','✅ Подтверждённых возвратов за этот период нет.']
    lines=['↩️ ВОЗВРАТЫ',f'📦 Всего: {total}']
    for k in ('NOT_PICKED_UP','RETURNED_RESELLABLE','RETURNED_DAMAGED'):
        if s[k]['orders']:lines.append(status_line(e,k))
    if e['return_condition_unknown']:lines.append('🟡 Состояние товара не подтверждено: '+number(e['return_condition_unknown']))
    lines += [f'↩️ Обратная доставка: {rub(e["ozon_return_actual"])}',f'❌ Списано: {rub(e["writeoff"])}']
    for v in e.get('not_picked_up_loss',[]):lines.append(f'📦 Заказ №{v["order_id"]} • потеря: {rub(v["loss_rub"])}')
    lines.append('Целый товар возвращается в доступный остаток после подтверждения. Повреждённый списывается по 230 ₽/шт.')
    return lines

def errors(r):
    f=r['instrumented_funnel'];t=r['technical'];counts={'📍 ПВЗ':0,'💳 Оплата':0,'🌐 JavaScript':0,'📦 Ozon':t['shipment_failures']+t['status_sync_stale'],'✉️ Письма':sum(x['count'] for x in t['service_messages'] if x['state']=='failed')}
    for k,v in f['errors'].items():
        label='📍 ПВЗ' if '/PVZ_' in k else '💳 Оплата' if '/PAYMENT_' in k else '🌐 JavaScript'
        counts[label]+=v
    counts['💳 Оплата']+=sum(x['count'] for x in t['verification_errors'] if x['error_code']!='not_paid')
    lines=['🛠 ОШИБКИ']
    if not any(counts.values()):lines += ['✅ В доступных источниках ошибок за этот период не зарегистрировано.']
    else:
        for k,v in counts.items():
            if v:lines.append(k+': '+number(v))
    if not f['sessions']:lines.append('🟡 Клиентских наблюдений пока нет. Это не подтверждение отсутствия ошибок у всех покупателей.')
    for k,v in list(f['errors'].items())[:4]:lines.append('⚠️ '+readable_segment('/'.join(k.split('/')[:3]))+': '+number(v)+' ошибок')
    return lines

def system(r,worker):
    lines=['❤️ СОСТОЯНИЕ СИСТЕМЫ']
    lines.append('🤖 Telegram-аналитика: '+('✅ Работает' if worker.get('state')=='running' else '🟡 Проверка состояния'))
    if worker.get('transport_error'):lines.append('🔄 Доставка событий: 🟡 Были перебои, выполняются повторные проверки')
    lines.append('📊 Метрика: '+('✅ Данные получены' if r.get('metrika',{}).get('devices',{}).get('quality')=='CONFIRMED' else UNKNOWN))
    lines.append('📣 Direct: '+('✅ Данные получены' if r['ads'].get('quality')=='CONFIRMED' else UNKNOWN))
    rsya=r['ads'].get('channels',{}).get('YANDEX_RSYA',{})
    lines.append('📣 РСЯ: '+('✅ Есть показы' if rsya.get('impressions') else '🟡 Пока без показов'))
    t=r['technical']
    lines += [f'📦 Ozon: '+('⚠️ Есть ошибки отправлений' if t['shipment_failures'] else '✅ Ошибок отправлений не зарегистрировано'),
              '🔄 Статусы Ozon: '+('⚠️ Есть задержка обновления' if t['status_sync_stale'] else '✅ Просроченных проверок нет')]
    email_failed=sum(x['count'] for x in t['service_messages'] if x['state']=='failed')
    pending=sum(x['count'] for x in t['service_messages'] if x['state']=='pending')
    lines.append('✉️ Письма: '+('⚠️ Есть ошибки доставки' if email_failed else '🟡 Есть ожидающие отправки' if pending else '✅ Ошибок доставки не зарегистрировано'))
    lines += ['💳 Оплата и 📍 ПВЗ: смотрите факты выбранного периода; отсутствие ошибок не заменяет проверку покупки.','📅 Отчёт: ежедневно в 09:00 МСК','📆 Неделя: понедельник, 09:10 МСК']
    return lines

def render(r,command,worker=None):
    worker=worker or {};header=[TITLES.get(command,'📊 ПУЛЬТ ВЛАДЕЛЬЦА').upper(),period_text(r)]
    if r['day_not_finished']:header.append('⏱ День ещё не завершён')
    if command=='menu':return '\n'.join(['📊 ПУЛЬТ ВЛАДЕЛЬЦА','Выберите период или раздел.','🔒 Доступ только владельцу.'])
    if command in ('today','yesterday','week'):
        lines=sales(r)+['',SEP]+advertising(r)+['',SEP]+economy(r)+['',SEP]+attention(r)
        totals=r.get('metrika',{}).get('devices',{}).get('totals') or []
        lines.insert(0,'👥 Визиты Метрики: '+number(totals[0] if totals else None))
        lines += ['', '🧭 Полная воронка: '+number(r['instrumented_funnel']['sessions'])+' измеренных сессий','🔎 Подробности — по кнопкам ниже.']
    elif command=='funnel':lines=funnel_view(r,True)
    elif command=='profit':lines=economy(r,True)
    elif command=='ads':lines=advertising(r,True)
    elif command in ('devices','browsers'):lines=devices(r,command=='browsers')
    elif command=='behavior':lines=behavior(r)
    elif command=='speed':lines=speed(r)
    elif command=='orders':
        lines=['📦 ЗАКАЗЫ']+[status_line(r['economics'],k) for k,v in r['economics']['statuses'].items() if v['orders']]
        if len(lines)==1:lines+=['✅ Новых заказов за этот период нет.']
        lines+=['Заказы относятся к периоду их создания.']
    elif command=='returns':lines=returns(r)
    elif command=='stock':
        s=r['stock'];lines=['🏪 СКЛАД',f'📦 Доступно, оценка: {number(s["estimated_units"])} шт.',f'💰 Себестоимость остатка: {rub(s["valuation_rub"])}','🟡 Остаток расчётный; сверяйте с фактическим складом.','📦 Себестоимость: 230 ₽/шт.']
    elif command=='errors':lines=errors(r)
    elif command=='status':lines=system(r,worker)
    else:lines=attention(r)
    return '\n'.join(header+['',SEP,'']+lines)

def pages(text,limit=3300):
    result=[];current=''
    for line in text.splitlines():
        if len(current)+len(line)+1>limit and current:result.append(current.rstrip());current=''
        current+=line[:limit]+'\n'
    if current:result.append(current.rstrip())
    return result or ['🟡 Пока нет данных']

def keyboard(command='menu',period='today',page=0,total=1):
    def button(cmd,label=None,p=None,idx=0):return {'text':label or TITLES[cmd],'callback_data':f'pf:{cmd}:{p or period}:{idx}'}
    rows=[]
    if total>1:
        nav=[]
        if page:nav.append(button(command,'‹ Назад',idx=page-1))
        if page+1<total:nav.append(button(command,'Далее ›',idx=page+1))
        rows.append(nav)
    rows.append([button(c,p=c) for c in ('today','yesterday','week')])
    if command=='menu':
        for i in range(3,len(MENU),3):rows.append([button(c,label) for c,label in MENU[i:i+3]])
    elif command in ('today','yesterday','week'):
        for group in [('funnel','profit'),('ads','devices'),('orders','errors')]:rows.append([button(c) for c in group])
        if command=='week':rows.append([button('attention'),button('speed')])
    elif command=='funnel':rows.append([button('behavior'),button('devices')])
    if command!='menu':rows.append([button('menu','☰ Все разделы')])
    return {'inline_keyboard':rows}

def alert_text(key,severity):
    icon='❌' if severity=='CRITICAL' else '⚠️'
    subjects={'paid-no-shipment':'Оплата подтверждена, но создание отправления Ozon завершалось ошибкой.',
              'ozon-stale':'Есть отправления без свежего статуса Ozon.',
              'email-failed':'Сервисное письмо по реальному заказу не отправлено.',
              'pvz-timeouts':'Повторяются превышения времени ожидания ПВЗ.',
              'stock':'Расчётный остаток товара требует проверки.'}
    subject=subjects.get(key)
    if not subject:
        if key.startswith(('device-','relative-device-')):subject='Конверсия выбора ПВЗ на одном из устройств требует проверки.'
        elif key.startswith('ad-no-buy-'):subject='Есть рекламные клики и измеренные визиты, но нет нажатий «Купить».'
        elif key.startswith('errors-'):subject='Повторяются технические ошибки в одном из сегментов.'
        elif key.startswith('PAYMENT_STARTED'):subject='Есть начала оплаты, но нет подтверждённых оплат.'
        elif key.startswith('PVZ_SELECTED'):subject='После выбора ПВЗ покупатели не начинают оплату.'
        else:subject='Покупатели не завершают выбор ПВЗ.'
    return f'{icon} ТРЕБУЕТ ВНИМАНИЯ\n\n{subject}\n\n🔎 Откройте воронку и ошибки. Сигнал требует проверки; это не готовый диагноз.'
