import json
import re
from decimal import Decimal
from product_catalog import frozen_unit_price

def normalize_street(value):
    value = ' '.join(str(value or '').split()).casefold()
    return re.sub(r'^(?:улица|ул\.?)\s+', 'улица ', value)

def verify_native_proof(order, pending, order_key):
    """Validate native buyer JSON fetched server-side with its opaque order key."""
    if order.get('key') != order_key or order.get('account_id') != 6172981:
        raise ValueError('Native shop/key mismatch')
    snapshot = pending['snapshot']
    if isinstance(snapshot, str): snapshot = json.loads(snapshot)
    fields = []
    for field in order.get('order_fields', []):
        try:
            value = json.loads(field.get('value', ''))
            if isinstance(value, dict) and value.get('internal_order_token') == pending['internal_order_token']:
                fields.append(value)
        except (ValueError, TypeError): pass
    if len(fields) != 1 or str(fields[0].get('pickup_point_id')) != str(snapshot['pickup_point_id']):
        raise ValueError('Native pending token/pickup mismatch')
    if Decimal(str(order.get('full_total_price', 'NaN'))) != Decimal(str(pending['amount'])) or Decimal(str(order.get('delivery_price', 'NaN'))) != 0:
        raise ValueError('Native amount mismatch')
    items = order.get('order_lines', [])
    if len(items) != 1 or items[0].get('product_id') != 1825508753 or items[0].get('variant_id') != 2184195121 or items[0].get('quantity') != pending['quantity'] or Decimal(str(items[0].get('sale_price'))) != frozen_unit_price(pending,snapshot):
        raise ValueError('Native product/quantity mismatch')
    if order.get('payment_gateway', {}).get('id') != 14315601 or order.get('delivery_variant', {}).get('id') != 32292089:
        raise ValueError('Native payment/delivery mismatch')
    address = order.get('shipping_address', {})
    parts = [part.strip() for part in snapshot['pickup_address'].split(',')]
    # InSales stores the street/house separately and renders address as "street д. house".
    expected_street, expected_house = parts[-2], parts[-1]
    actual_address = ' '.join(str(address.get('address') or '').split()).casefold()
    full_address = ' '.join(snapshot['pickup_address'].split()).casefold()
    street_house = (expected_street + ' д. ' + expected_house).casefold()
    if actual_address not in (full_address, street_house):
        raise ValueError('Native street/house mismatch')
    if address.get('street') and normalize_street(address.get('street')) != normalize_street(expected_street):
        raise ValueError('Native street/house mismatch')
    if address.get('house') and str(address.get('house')).casefold() != expected_house.casefold():
        raise ValueError('Native street/house mismatch')
    for field, expected in [('city',snapshot['pickup_city']),('name',snapshot['recipient_name']),('phone',snapshot['phone']),('email',snapshot['email'])]:
        actual = ' '.join(str(address.get(field) or '').split()); expected = ' '.join(expected.split())
        # Native checkout retains the complete address and locality in kladr_json
        # for a non-KLADR delivery point. Only accept that representation when
        # the native full address exactly matches the server-side cached PVZ.
        if field == 'city' and not actual and actual_address == full_address:
            try:
                locality = json.loads(address.get('kladr_json') or '{}')
                actual = ' '.join(str(locality.get('city') or '').split())
            except (ValueError, TypeError):
                pass
        if field == 'phone':
            actual = ''.join(c for c in actual if c.isdigit()); expected = ''.join(c for c in expected if c.isdigit())
        if actual.casefold() != expected.casefold(): raise ValueError('Native recipient/address mismatch')
    order_id, number = int(order['id']), int(order['number'])
    if order_id <= 0 or number <= 0: raise ValueError('Invalid native order reference')
    return order_id, number
