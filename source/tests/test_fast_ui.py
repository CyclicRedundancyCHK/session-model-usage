import unittest
from PySide6.QtWidgets import QApplication
from session_model_usage.overlay import Details
from test_accounting import usage


class FastUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def test_configuration_cards_show_fast_standard_and_unknown(self):
        groups = [{'reasoning_effort':'max','service_tier':tier,'fast_mode':fast,
                   'totals':usage(),'main':usage(),'subagents':usage(0,0,0,0)}
                  for tier, fast in (('priority',True),('default',False),(None,None))]
        snapshot={'totals':usage(300,30,150,12),'status':'complete','threads':[],
                  'models':[{'model':'gpt-6-sol','configurations':groups}], 'warnings':[]}
        details=Details()
        try:
            for dark in (True,False):
                details.theme(dark); details.update_usage(snapshot,'fixture'); self.app.processEvents()
                self.assertEqual([card.speed.text() for card in details.models.cards],['Fast','普通','未记录'])
                self.assertEqual(len(details.models.keys),3)
                self.assertIn('请求设置',details.notice.text())
                for card in details.models.cards:
                    self.assertIn('服务端',card.speed.toolTip())
            snapshot['models'][0]['configurations'] = groups[:1]
            details.update_usage(snapshot,'fixture')
            self.assertEqual(len(details.models.cards),1)
        finally:
            details.close(); details.deleteLater(); self.app.processEvents()
