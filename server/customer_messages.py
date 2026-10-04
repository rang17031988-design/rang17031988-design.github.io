import os, hashlib, httpx
from html import escape
from customer_tracking import STATUS_LABELS
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr

def customer_message(kind,tracking,details=None):
    details=details or {}
    number=str(details.get('order_number',''))
    heading=('Ваш заказ №'+number+' прибыл в пункт выдачи Ozon и готов к получению.'
             if kind=='ready_email' else 'Оплата заказа №'+number+' прошла успешно.')
    if kind == 'ready_email':
        lines=['Здравствуйте!',heading]
    elif kind == 'paid_email':
        lines=['Здравствуйте!',heading,
               'Количество: '+str(details.get('quantity',1))+' шт.',
               'Оплачено: '+str(details.get('amount',''))+' ₽',
               'Доставка в ПВЗ Ozon — бесплатно.']
        if not tracking:lines.append('Заказ готовится к доставке. Номер отправления появится на странице отслеживания.')
    else:
        raise ValueError('Unsupported customer email kind')
    if details.get('pickup_address'):lines.append('ПВЗ Ozon: '+details['pickup_address'])
    if tracking:lines.append('Номер отслеживания: '+str(tracking))
    if kind=='ready_email':
        lines.append('Для получения откройте заказ в приложении Ozon под тем же номером телефона и покажите штрихкод сотруднику пункта выдачи.')
    if details.get('tracking_url'):lines += ['Отследить заказ:',details['tracking_url'],
       'Статус доставки можно посмотреть на сайте Посуда163 без входа в Ozon.']
    return '\n\n'.join(lines+['Спасибо за заказ!'])

def customer_email_html(kind,tracking,details):
    text=customer_message(kind,tracking,details)
    url=details.get('tracking_url','')
    parts=[escape(p).replace('\n','<br>') for p in text.split('\n\n') if p!=url]
    button=('<p><a href="'+escape(url,quote=True)+'" style="display:inline-block;background:#21835c;'
            'color:#fff;padding:16px 25px;border-radius:9px;text-decoration:none;font-weight:700">'
            'Отследить заказ</a></p>') if url else ''
    return ('<!doctype html><html lang="ru"><body style="margin:0;background:#f4f5f3;font-family:Arial,sans-serif;'
            'color:#172a20"><main style="max-width:580px;margin:24px auto;padding:26px;background:#fff;'
            'border-radius:14px">'+''.join('<p>'+p+'</p>' for p in parts)+button+'</main></body></html>')

def production_email_allowed(row):
    # Enabled only after the owner test. A cutoff excludes historical orders.
    if os.getenv('CUSTOMER_EMAIL_ENABLED', '').lower() != 'true':
        return False
    try:
        cutoff = datetime.fromisoformat(os.environ['CUSTOMER_EMAIL_START_AT'].replace('Z','+00:00'))
        created = row['created_at']
        if isinstance(created, str):created=datetime.fromisoformat(created.replace('Z','+00:00'))
        if cutoff.tzinfo is None or created.tzinfo is None:return False
        return created.astimezone(timezone.utc) >= cutoff.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False

class EmailDeliveryRejected(Exception):
    """Explicit API rejection before Resend accepted a message; no secret detail."""

def _sender():
    name,address=parseaddr(os.getenv('EMAIL_FROM',''))
    local,separator,domain=address.rpartition('@')
    if not separator:raise ValueError('Sender not configured')
    ascii_address=local+'@'+domain.encode('idna').decode('ascii')
    if ascii_address.lower()!='order@xn--163-5cdt3dgrs.xn--p1ai':
        raise ValueError('Unexpected sender')
    return (name+' <'+ascii_address+'>') if name else ascii_address

def email_ready():
    try:return bool(os.getenv('RESEND_API_KEY') and _sender())
    except (ValueError,UnicodeError):return False

def _resend_send(message):
    # Durable worker claims protect indefinitely; provider key adds protection.
    key='posuda163-'+hashlib.sha256(str(message['Message-ID']).encode()).hexdigest()
    with httpx.Client(timeout=25,follow_redirects=False) as client:
        body=message.get_body(preferencelist=('plain',)) if message.is_multipart() else message
        payload={'from':_sender(),'to':[str(message['To'])],
                'subject':str(message['Subject']),'text':body.get_content()}
        html=message.get_body(preferencelist=('html',)) if message.is_multipart() else None
        if html:payload['html']=html.get_content()
        response=client.post('https://api.resend.com/emails',
            headers={'Authorization':'Bearer '+os.environ['RESEND_API_KEY'],'Idempotency-Key':key},
            json=payload)
        if response.status_code in (400,401,403,404,422,429):
            raise EmailDeliveryRejected('Resend send rejected')
        if response.status_code not in (200,201):raise RuntimeError('Resend delivery unconfirmed')
        message_id=response.json().get('id')
        if not message_id:raise RuntimeError('Resend delivery unconfirmed')
        return message_id

def send_customer_email(email,number,kind,tracking,details=None):
    details=dict(details or {},order_number=number)
    message=EmailMessage()
    message['From']=os.environ['EMAIL_FROM'];message['To']=email
    message['Subject']=('Ваш заказ прибыл в пункт выдачи Ozon' if kind=='ready_email'
                        else 'Оплата заказа прошла успешно')
    identity=details.get('message_key',kind)
    message['Message-ID']=f'<posuda-order-{number}-{identity}@xn--163-5cdt3dgrs.xn--p1ai>'
    message.set_content(customer_message(kind,tracking,details))
    message.add_alternative(customer_email_html(kind,tracking,details),subtype='html')
    return _resend_send(message)

def send_resend_test():
    """One owner-only UTF-8 probe; never announces a fictitious paid order."""
    message=EmailMessage()
    message['From']=os.environ['EMAIL_FROM']
    message['To']='rang17031988@gmail.com'
    message['Subject']='Посуда163 — тест сервисного письма'
    message['Message-ID']='<posuda163-railway-resend-test-20261002@xn--163-5cdt3dgrs.xn--p1ai>'
    message.set_content('Здравствуйте!\n\nЭто единственное тестовое сервисное письмо с backend Railway. '
                        'Проверка русскоязычного текста: оплата, доставка, пункт выдачи. '
                        'Новый заказ и платёж не создавались.\n\nПосуда163')
    result={'state':'unknown','transport':'resend','utf8':True,'from':os.environ['EMAIL_FROM']}
    try:
        result['message_id']=_resend_send(message)
        result['state']='sent'
    except EmailDeliveryRejected:
        result['state']='rejected'
    except Exception as exc:
        # Do not include HTTP responses, authorization headers or credentials.
        result['error_type']=type(exc).__name__
    return result
