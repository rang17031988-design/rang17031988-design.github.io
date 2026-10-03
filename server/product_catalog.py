"""Native InSales catalog is the authority for new price and available stock."""
from decimal import Decimal

PRODUCT_ID=1825508753
VARIANT_ID=2184195121
CATALOG_URL='https://xn--163-5cdt3dgrs.xn--p1ai/product/ruchka-dlya-skovorody-semnaya.json'
PRODUCT_NAME='Оригинальная универсальная съёмная ручка для сковородок'

async def read_catalog(client):
    response=await client.get(CATALOG_URL,timeout=20)
    response.raise_for_status()
    product=response.json()['product']
    if product['id']!=PRODUCT_ID:raise ValueError('catalog_product_mismatch')
    variant=next(v for v in product['variants'] if v['id']==VARIANT_ID)
    price=Decimal(str(variant['price']))
    quantity=variant.get('quantity')
    if not price.is_finite() or price<=0 or price!=price.to_integral_value():raise ValueError('catalog_price_invalid')
    if not isinstance(quantity,int) or quantity<0:raise ValueError('catalog_stock_unknown')
    return {'unit_price':int(price),'quantity':quantity,'available':bool(variant['available']),'source':'InSales native variant'}

def frozen_unit_price(order,snapshot):
    # Historical orders retain the unit price frozen by our server at preparation.
    # Only legacy records without the field retain their historical 800 RUB price.
    quantity=int(order['quantity'])
    if quantity<=0:raise ValueError('quantity_amount_mismatch')
    price=Decimal(str(snapshot.get('unit_price',800)))
    if not price.is_finite() or price<=0 or Decimal(str(order['amount']))!=price*quantity:raise ValueError('quantity_amount_mismatch')
    return price
