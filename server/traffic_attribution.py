"""Single primary channel; evidence distinguishes paid traffic from organic."""
import re, json
from urllib.parse import urlsplit

PAID_MEDIA={'cpc','ppc','paid','paidsearch','paid_search'}

def touch_fields(a):
    result={}
    for side in ('first','last'):
        for field in ('source','medium','campaign','content','term'):
            result[side+'_'+field]=str(a.get(side+'_'+field) or a.get('utm_'+field) or '')[:250] or None
        value=str(a.get(side+'_yclid') or '')
        result[side+'_yclid']=value if re.fullmatch(r'\d{1,100}',value) else None
        host=str(a.get(side+'_referrer_host') or '')
        result[side+'_referrer_host']=host if re.fullmatch(r'[a-zA-Z0-9.-]{1,120}',host) else None
    result.update(classify(a,a.get('referrer_host','')))
    return result

def classify(a=None, referrer='', traffic_class='customer'):
    a=a or {}
    if traffic_class in ('owner','internal_test','unknown'):
        return {'primary_attribution':{'owner':'OWNER','internal_test':'INTERNAL_TEST','unknown':'UNKNOWN'}[traffic_class], 'paid_evidence':False,'search_engine':None}
    source,medium,campaign=(str(a.get(k) or '').lower() for k in ('utm_source','utm_medium','utm_campaign'))
    paid=bool(re.fullmatch(r'\d{1,100}',str(a.get('yclid') or ''))) or source in ('yandex','ya') and medium in PAID_MEDIA
    name='UNKNOWN';engine=None
    if paid:
        name={'714566814':'PAID_SEARCH','715029848':'PAID_RSYA'}.get(campaign,'UNKNOWN')
    elif source in ('telegram','tg'):
        name={'content_engine':'TELEGRAM_OWNED','external_groups':'TELEGRAM_EXTERNAL'}.get(campaign,'UNKNOWN')
    elif source in ('dzen','zen','pinterest','ok','odnoklassniki','bluesky','bsky') and medium=='organic':
        name={'dzen':'DZEN_ORGANIC','zen':'DZEN_ORGANIC','pinterest':'PINTEREST_ORGANIC',
            'ok':'OK_ORGANIC','odnoklassniki':'OK_ORGANIC','bluesky':'BLUESKY_ORGANIC','bsky':'BLUESKY_ORGANIC'}[source]
    else:
        host=urlsplit(referrer if '://' in referrer else '//'+referrer).hostname or ''
        host=host.lower().rstrip('.')
        for family,domains in [('yandex',('yandex.ru','yandex.com','ya.ru')),('google',('google.com','google.ru')),('other',('bing.com','duckduckgo.com'))]:
            if any(host==d or host.endswith('.'+d) for d in domains):engine=family;break
        if medium=='organic' and source in ('google','yandex','ya','bing'):
            engine={'ya':'yandex','bing':'other'}.get(source,source)
        if engine:name='ORGANIC_SEARCH'
        elif not source and not host and not medium and not campaign:name='DIRECT'
        elif host or medium=='referral':name='REFERRAL_OTHER'
    return {'primary_attribution':name,'paid_evidence':paid,'search_engine':engine}

def legacy_channel(a=None,referrer=''):
    c=classify(a,referrer)
    return {'PAID_SEARCH':'YANDEX_SEARCH','PAID_RSYA':'YANDEX_RSYA','ORGANIC_SEARCH':'SEO_ORGANIC',
        'TELEGRAM_OWNED':'TELEGRAM','TELEGRAM_EXTERNAL':'TELEGRAM','DZEN_ORGANIC':'DZEN',
        'PINTEREST_ORGANIC':'PINTEREST','OK_ORGANIC':'ODNOKLASSNIKI','BLUESKY_ORGANIC':'BLUESKY',
        'REFERRAL_OTHER':'OTHER'}.get(c['primary_attribution'],
        'OTHER_YANDEX_PAID' if c['paid_evidence'] else c['primary_attribution'])

def source_report(sessions,orders):
    groups={}
    def entry(name):return groups.setdefault(name,{'sessions':0,'orders':0,'paid':0,'revenue':0})
    for s in sessions:
        if s.get('is_test') or s.get('is_internal') or s.get('traffic_class','customer')!='customer':continue
        a=s.get('attribution') or {}
        if isinstance(a,str):a=json.loads(a)
        entry(classify(a,a.get('referrer_host',''))['primary_attribution'])['sessions']+=1
    for r in orders:
        if r.get('is_test') or r.get('is_internal'):continue
        a=r.get('attribution') or {}
        if isinstance(a,str):a=json.loads(a)
        e=entry(classify(a,a.get('referrer_host',''))['primary_attribution'] if a else 'UNKNOWN');e['orders']+=1
        if r.get('payment_status')=='succeeded':e['paid']+=1;e['revenue']+=float(r['amount'])
    for e in groups.values():e['paid_cr']=100*e['paid']/e['sessions'] if e['sessions'] else None
    return groups
