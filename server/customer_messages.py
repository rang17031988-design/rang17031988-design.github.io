import os, hashlib, httpx
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr

STATUS_LABELS = {'created':'создан','forming':'создан','ready_for_shipping':'создан',
    'in_container':'создан','acceptance_in_progress':'передан Ozon','on_way':'доставляется',
    'in_delivery_point':'готов к получению','in_courier_service':'доставляется',
    'delivered':'получен','canceled':'отменён','forming_failed':'проблема',
    'not_accepted_to_delivery':'проблема','unknown':'уточняется'}

def customer_message(kind,tracking):
    if kind == 'ready_email':
        return ('Здравствуйте!\n\nВаш заказ прибыл в пункт выдачи Ozon и ждёт получения.'
                '\n\nНомер отправления:\n'+tracking+
                '\n\nОткройте приложение Ozon → раздел «Ozon Доставка», чтобы посмотреть заказ и данные ПВЗ.'
                '\n\nЕсли приложения Ozon нет:\nустановите его и войдите по тому же номеру телефона, '
                'который вы указали при оформлении заказа.')
    if kind != 'paid_email':
        raise ValueError('Unsupported customer email kind')
    return ('Здравствуйте!\n\nВаш заказ успешно оплачен и передан в Ozon Доставку.'
            '\n\nНомер отправления:\n'+tracking+
            '\n\nКак отслеживать заказ:\n\nОткройте приложение Ozon → раздел «Ozon Доставка».'
            '\n\nЕсли приложения Ozon нет:\nустановите приложение Ozon и войдите по тому же номеру '
            'телефона, который вы указали при оформлении заказа.'
            '\n\nМы также сообщим вам, когда заказ прибудет в пункт выдачи.\n\nСпасибо за заказ!')

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
        response=client.post('https://api.resend.com/emails',
            headers={'Authorization':'Bearer '+os.environ['RESEND_API_KEY'],'Idempotency-Key':key},
            json={'from':_sender(),'to':[str(message['To'])],
                'subject':str(message['Subject']),'text':message.get_content()})
        if response.status_code in (400,401,403,404,422,429):
            raise EmailDeliveryRejected('Resend send rejected')
        if response.status_code not in (200,201):raise RuntimeError('Resend delivery unconfirmed')
        message_id=response.json().get('id')
        if not message_id:raise RuntimeError('Resend delivery unconfirmed')
        return message_id

def send_customer_email(email,number,kind,tracking):
    message=EmailMessage()
    message['From']=os.environ['EMAIL_FROM'];message['To']=email
    message['Subject']=('Ваш заказ прибыл в пункт выдачи Ozon' if kind=='ready_email'
                        else 'Заказ оплачен и передан в Ozon Доставку')
    message['Message-ID']=f'<posuda-order-{number}-{kind}@xn--163-5cdt3dgrs.xn--p1ai>'
    message.set_content(customer_message(kind,tracking))
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
