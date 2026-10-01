import re
from decimal import Decimal, InvalidOperation

def validate_payment(payment, order, shop_id):
    if payment.get('id') != order['payment_id']:
        return 'payment_id_mismatch'
    if str(payment.get('recipient', {}).get('account_id')) != str(shop_id):
        return 'shop_mismatch'
    if payment.get('test') is not False:
        return 'test_payment'
    amount = payment.get('amount', {})
    try:
        valid_amount = Decimal(amount.get('value', 'NaN')) == Decimal(str(order['amount']))
    except (InvalidOperation, TypeError):
        valid_amount = False
    if amount.get('currency') != 'RUB' or not valid_amount:
        return 'amount_mismatch'
    number = str(order['order_number'])
    if not re.search(r'(?<!\d)' + re.escape(number) + r'(?!\d)', payment.get('description', '')):
        return 'order_reference_mismatch'
    if payment.get('status') != 'succeeded' or payment.get('paid') is not True:
        return 'not_paid'
    return None
