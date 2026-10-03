import unittest, sys
from pathlib import Path
from copy import deepcopy
sys.path.insert(0, str(Path(__file__).parent / 'server'))
from payment_validation import validate_payment

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.order={'payment_id':'example','order_number':1002,'amount':'800.00'}
        self.payment={'id':'example','recipient':{'account_id':'1399141'},'test':False,
            'amount':{'value':'800.00','currency':'RUB'},'description':'Заказ №1002 на сайте посуда163.рф',
            'metadata':{'cms_name':'insales_native'},
            'status':'succeeded','paid':True}
    def test_valid(self):
        self.assertIsNone(validate_payment(self.payment,self.order,'1399141'))
    def test_reject_unpaid_and_non_succeeded(self):
        for patch in ({'paid':False},{'status':'pending'},{'status':'waiting_for_capture'},{'status':'canceled'}):
            p=deepcopy(self.payment);p.update(patch)
            self.assertEqual(validate_payment(p,self.order,'1399141'),'not_paid')
    def test_reject_wrong_amount_currency_or_invalid_number(self):
        for amount in ({'value':'0.00','currency':'RUB'},{'value':'1600.00','currency':'RUB'},
                       {'value':'800.00','currency':'USD'},{'value':'NaN','currency':'RUB'},
                       {'value':'invalid','currency':'RUB'}):
            p=deepcopy(self.payment);p['amount']=amount
            self.assertEqual(validate_payment(p,self.order,'1399141'),'amount_mismatch')
    def test_reject_other_shop(self):
        self.payment['recipient']['account_id']='wrong'
        self.assertEqual(validate_payment(self.payment,self.order,'1399141'),'shop_mismatch')
    def test_reject_test_mode(self):
        self.payment['test']=True
        self.assertEqual(validate_payment(self.payment,self.order,'1399141'),'test_payment')
    def test_reject_order_number_substring(self):
        self.payment['description']='Заказ №10020'
        self.assertEqual(validate_payment(self.payment,self.order,'1399141'),'order_reference_mismatch')
    def test_reject_other_payment(self):
        self.payment['id']='wrong'
        self.assertEqual(validate_payment(self.payment,self.order,'1399141'),'payment_id_mismatch')

if __name__ == '__main__': unittest.main()
