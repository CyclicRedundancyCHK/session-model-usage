import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock
from types import SimpleNamespace
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from session_model_usage.overlay import Badge, Details, Overlay
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

    def test_details_outside_press_dismisses_without_focus_or_reopening(self):
        details, badge, blank = Details(), Badge(), QWidget()
        details.dismiss_anchor = badge
        with patch('session_model_usage.overlay.mouse_press_state', return_value=(0, 0)):
            try:
                badge.show(); details.show(); self.app.processEvents()
                details.outside_click_sample(1, int(details.winId()))
                self.assertTrue(details.isVisible())
                details.outside_click_sample(0, 0)
                details.outside_click_sample(1, int(badge.winId()))
                self.assertTrue(details.isVisible())
                details.outside_click_sample(0, 0)
                details.outside_click_sample(2, 0)
                self.assertFalse(details.isVisible())
                self.assertFalse(details.dismiss_timer.isActive())
                details.show(); self.app.processEvents()
                QTest.mouseClick(details.group.button(1), Qt.MouseButton.LeftButton)
                self.assertTrue(details.isVisible())
                self.assertEqual(details.pages.currentIndex(), 1)
                blank.show(); QTest.mouseClick(blank, Qt.MouseButton.LeftButton)
                self.assertFalse(details.isVisible())
            finally:
                for widget in (details, badge, blank): widget.close(); widget.deleteLater()
                self.app.processEvents()

    def test_badge_repeated_press_toggles_details_once_per_click(self):
        overlay = Overlay.__new__(Overlay)
        overlay.badge, overlay.details = Badge(), Details()
        overlay.details.dismiss_anchor = overlay.badge
        overlay.data = {'host_hwnd': 0}
        overlay.current = overlay.usage = None
        overlay.worker = SimpleNamespace(placement=None)
        overlay.observe = Mock()
        overlay.badge.pressed.connect(overlay.toggle_details)
        with patch('session_model_usage.overlay.attach_to_window'), patch('session_model_usage.overlay.mouse_press_state', return_value=(0, 0)):
            try:
                overlay.badge.show()
                for expected in (True, False, True, False):
                    QTest.mouseClick(overlay.badge, Qt.MouseButton.LeftButton)
                    self.app.processEvents()
                    self.assertEqual(overlay.details.isVisible(), expected)
                self.assertTrue(overlay.details.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
            finally:
                for widget in (overlay.details, overlay.badge): widget.close(); widget.deleteLater()
                self.app.processEvents()

    def test_existing_card_tracks_follow_a_theme_change_without_changing_appearance(self):
        with tempfile.TemporaryDirectory() as name, patch.dict(os.environ, {'SESSION_USAGE_STATE_DIR': name}):
            settings = Path(name)/'settings.ini'
            settings.write_text('Theme=cyan\nAppearance=dark\nGlassEnabled=False\n')
            details = Details()
            snapshot = {'status':'complete', 'totals':usage(), 'warnings':[], 'threads':[],
                'models':[{'model':'gpt-6-sol','configurations':[{'reasoning_effort':'max',
                    'totals':usage(), 'main':usage(), 'subagents':usage(0,0,0,0)}]}]}
            try:
                details.update_usage(snapshot,'fixture')
                card=details.models.cards[0]
                before=(card.bar.main_color.name(),card.bar.child_color.name())
                settings.write_text('Theme=aurora\nAppearance=dark\nGlassEnabled=False\n')
                details.refresh_material()
                self.assertIs(card, details.models.cards[0])
                self.assertNotEqual(before,(card.bar.main_color.name(),card.bar.child_color.name()))
                self.assertIn(card.bar.child_color.name().upper(), details.styleSheet().upper())
            finally:
                details.close(); details.deleteLater(); self.app.processEvents()

    def test_micro_pending_badge_fits_measured_narrow_gap_without_showing_zero(self):
        badge=Badge()
        try:
            badge.update_usage(None)
            self.assertTrue(badge.fit_to_gap(94,28))
            self.assertLessEqual(badge.width(),94)
            self.assertIn('—',badge.text())
            self.assertIn('等待',badge.toolTip())
        finally:badge.close(); badge.deleteLater(); self.app.processEvents()

    def test_micro_large_total_fits_narrow_gap_and_keeps_exact_tooltip(self):
        badge=Badge()
        try:
            badge.update_usage({'totals':{'total_tokens':249731837},'status':'complete'})
            self.assertTrue(badge.fit_to_gap(94,28))
            self.assertLessEqual(badge.width(),94)
            self.assertIn('249,731,837 tokens',badge.toolTip())
            self.assertIn('tokens',badge.accessibleName())
        finally:badge.close(); badge.deleteLater(); self.app.processEvents()

    def test_micro_badge_still_hides_if_no_safe_clickable_space(self):
        badge=Badge()
        try:
            self.assertFalse(badge.fit_to_gap(30,28))
            self.assertFalse(badge.fit_to_gap(94,18))
        finally:badge.close(); badge.deleteLater(); self.app.processEvents()
