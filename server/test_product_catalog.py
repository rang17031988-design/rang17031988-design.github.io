import unittest
from product_catalog import read_catalog,frozen_unit_price,PRODUCT_ID,VARIANT_ID
from unittest.mock import AsyncMock,Mock

class PriceTests(unittest.TestCase):
    def test_new_quantities_and_history(self):
        for q in (1,2,3,4,5):
            self.assertEqual(frozen_unit_price({'quantity':q,'amount':1200*q},{'unit_price':1200}),1200)
        self.assertEqual(frozen_unit_price({'quantity':1,'amount':800},{}),800)
    def test_mismatch_fails_closed(self):
        for row in ({'quantity':1,'amount':800},{'quantity':3,'amount':1200},{'quantity':0,'amount':0}):
            with self.assertRaises(ValueError):frozen_unit_price(row,{'unit_price':1200})

class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_catalog_price_and_stock(self):
        response=Mock();response.json.return_value={'product':{'id':PRODUCT_ID,'variants':[{'id':VARIANT_ID,'price':'1200.0','quantity':867,'available':True}]}}
        client=Mock();client.get=AsyncMock(return_value=response)
        result=await read_catalog(client)
        self.assertEqual((result['unit_price'],result['quantity']),(1200,867))
        response.json.return_value['product']['variants'][0]['quantity']=None
        with self.assertRaises(ValueError):await read_catalog(client)
