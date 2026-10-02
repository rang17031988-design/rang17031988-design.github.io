import re
from decimal import Decimal, InvalidOperation

def can_replace_unpaid_attempt(previous, payment):
    # The native checkout can create several attempts for the same order.
    # Never replace a paid attempt or let an unpaid event displace one.
    return (previous.get('paid') is False and
            previous.get('status') in ('pending', 'canceled') and
            payment.get('status') == 'succeeded' and payment.get('paid') is True)

def native_order_number(payment):
    """Accept only the native shop's observed description and CMS marker."""
    if not isinstance(payment, dict):
        return None
    metadata = payment.get('metadata')
    description = payment.get('description')
    if not isinstance(metadata, dict) or metadata.get('cms_name') != 'insales_native' or not isinstance(description, str):
        return None
    match = re.fullmatch(r'Заказ №([1-9][0-9]{0,11}) на сайте посуда163\.рф',
                         description)
    return int(match.group(1)) if match else None

def validate_payment(payment, order, shop_id):
    if not isinstance(payment, dict):
        return 'invalid_payment'
    if payment.get('id') != order['payment_id']:
        return 'payment_id_mismatch'
    recipient = payment.get('recipient')
    if not isinstance(recipient, dict) or str(recipient.get('account_id')) != str(shop_id):
        return 'shop_mismatch'
    if payment.get('test') is not False:
        return 'test_payment'
    amount = payment.get('amount')
    if not isinstance(amount, dict):
        return 'amount_mismatch'
    try:
        valid_amount = Decimal(amount.get('value', 'NaN')) == Decimal(str(order['amount']))
    except (InvalidOperation, TypeError):
        valid_amount = False
    if amount.get('currency') != 'RUB' or not valid_amount:
        return 'amount_mismatch'
    if native_order_number(payment) != int(order['order_number']):
        return 'order_reference_mismatch'
    if payment.get('status') != 'succeeded' or payment.get('paid') is not True:
        return 'not_paid'
    return None
