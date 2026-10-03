"""Offline paid conversions. No browser redirect or client amount is payment proof."""
import csv
import io
from datetime import datetime
from decimal import Decimal
from payment_validation import validate_payment
from product_catalog import frozen_unit_price


def paid_conversion(payment, order, snapshot, shop_id):
    error = validate_payment(payment, order, shop_id)
    if error:
        raise ValueError(error)
    attribution = snapshot.get('attribution') or {}
    client_id = attribution.get('client_id') or ''
    yclid = attribution.get('yclid') or ''
    if not client_id and not yclid:
        raise ValueError('missing_attribution')
    captured = payment.get('captured_at')
    if not captured:
        raise ValueError('missing_capture_time')
    timestamp = int(datetime.fromisoformat(captured.replace('Z', '+00:00')).timestamp())
    quantity = int(order['quantity'])
    frozen_unit_price(order,snapshot)
    return dict(order_id=int(order['order_id']), payment_id=payment['id'],
                target='payment_success', timestamp=timestamp,
                revenue=format(Decimal(str(order['amount'])), '.2f'), currency='RUB',
                product='Съёмная ручка для сковороды', quantity=quantity,
                client_id=client_id, yclid=yclid, attribution=attribution)


def conversion_csv(conversion):
    # Product, quantity and unique native order_id remain in the persistent queue.
    # Only officially supported offline-conversion columns are transmitted.
    out = io.StringIO(newline='')
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(['ClientId', 'Yclid', 'Target', 'DateTime', 'Price', 'Currency'])
    writer.writerow([conversion['client_id'], conversion['yclid'], conversion['target'],
                     conversion['timestamp'], conversion['revenue'], conversion['currency']])
    return out.getvalue().encode('utf-8')
