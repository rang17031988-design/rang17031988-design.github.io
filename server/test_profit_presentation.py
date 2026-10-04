import copy
import os
import unittest
from unittest.mock import patch
from profit_funnel import ProfitFunnel
import profit_presentation as ui

class PresentationTests(unittest.TestCase):
    def test_all_menu_routes_are_small_valid_callbacks(self):
        buttons=[b for row in ui.keyboard()['inline_keyboard'] for b in row]
        self.assertEqual(len(buttons),16)
        self.assertEqual({b['callback_data'].split(':')[1] for b in buttons},{c for c,_ in ui.MENU})
        for b in buttons:self.assertLessEqual(len(b['callback_data'].encode()),64)
        self.assertNotIn('debug',str(buttons))

    def test_callback_requires_owner_private_destination(self):
        worker=ProfitFunnel(None,None)
        update={'callback_query':{'from':{'id':42},'message':{'chat':{'id':42,'type':'private'}}}}
        with patch.dict(os.environ,{'OWNER_CHAT_ID':'42','PROFIT_FUNNEL_OWNER_CHAT_ID':'42','PROFIT_FUNNEL_OWNER_USER_ID':'42'}):
            self.assertTrue(worker.owner_allowed(update))
            for place,value in [('id',9),('type','group')]:
                bad=copy.deepcopy(update);bad['callback_query']['message']['chat'][place]=value
                self.assertFalse(worker.owner_allowed(bad))
            bad=copy.deepcopy(update);bad['callback_query']['from']['id']=9
            self.assertFalse(worker.owner_allowed(bad))

    def test_unknowns_and_money_do_not_become_zeros(self):
        for value in (None,'UNKNOWN',float('nan')):self.assertEqual(ui.rub(value),ui.UNKNOWN)
        self.assertEqual(ui.rub(-74.51),'-74,51 ₽')
        self.assertEqual(ui.number(161000),'161 000')

    def test_actions_do_not_invent_raise_lower_counts(self):
        r={'ads':{'quality':'CONFIRMED','channels':{}},'controller_actions':[{'action':'set','state':'applied','count':3},{'action':'suspend','state':'applied','count':2}]}
        before=copy.deepcopy(r);text='\n'.join(ui.advertising(r,True))
        self.assertIn('Изменил ставки: 3',text)
        self.assertIn('Приостановил: 2',text)
        self.assertIn('пока нет отдельной детализации',text)
        self.assertEqual(before,r)

    def test_pagination_preserves_normal_report(self):
        text='\n'.join(['Строка отчёта']*600)
        pages=ui.pages(text)
        self.assertTrue(all(len(x)<=3300 for x in pages))
        self.assertEqual('\n'.join(pages),text)

    def test_alerts_hide_internal_segments(self):
        text=ui.alert_text('relative-device-MOBILE/Android/Yandex','WARNING')
        self.assertNotIn('MOBILE',text)
        self.assertIn('требует проверки',text)
