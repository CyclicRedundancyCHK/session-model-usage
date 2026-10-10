import ctypes
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from session_model_usage.glass import GlassSettings, _AccentPolicy, _CompositionData, apply_glass
from session_model_usage.overlay import Details


class GlassCornerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @unittest.skipUnless(os.name == 'nt', 'Windows composition API')
    def test_blur_is_enabled_only_with_system_rounding(self):
        for rounded in (True, False):
            for enabled in (True, False):
                with self.subTest(rounded=rounded, enabled=enabled):
                    dwm, user = Mock(), Mock()
                    preferences, states = [], []
                    def attribute(hwnd, key, value, size):
                        if key == 33:
                            preferences.append(ctypes.cast(value, ctypes.POINTER(ctypes.c_int)).contents.value)
                            return 0 if rounded else -1
                        return 0
                    def composition(hwnd, value):
                        data = ctypes.cast(value, ctypes.POINTER(_CompositionData)).contents
                        states.append(ctypes.cast(data.data, ctypes.POINTER(_AccentPolicy)).contents.state)
                        return True
                    dwm.DwmSetWindowAttribute.side_effect = attribute
                    user.SetWindowCompositionAttribute.side_effect = composition
                    with patch('session_model_usage.glass.ctypes.WinDLL', side_effect=[dwm, user]):
                        material = apply_glass(123, GlassSettings(enabled, 40))
                    self.assertEqual(preferences, [2])
                    self.assertEqual(states, [3 if rounded and enabled else 0])
                    self.assertEqual(material, ('rounded-blur-behind' if enabled else 'rounded-disabled') if rounded else
                        ('alpha-only' if enabled else 'disabled'))

    def test_native_rounded_backdrop_has_no_conflicting_window_region(self):
        with tempfile.TemporaryDirectory() as name, patch.dict(os.environ, {'SESSION_USAGE_STATE_DIR': name}), \
                patch.object(QApplication, 'platformName', return_value='windows'), \
                patch('session_model_usage.overlay.apply_glass', return_value='rounded-blur-behind') as material:
            settings = Path(name) / 'settings.ini'
            settings.write_text('GlassEnabled=True\n')
            details = Details()
            try:
                details.show()
                self.app.processEvents()
                for width, height in ((620,450),(880,540)):
                    details.resize(width,height)
                    self.app.processEvents()
                    self.assertTrue(details.mask().isEmpty())
                self.assertIn('border-radius:8px', details.styleSheet())
                settings.write_text('GlassEnabled=False\n')
                def disabled(hwnd, preferences):
                    self.assertTrue(details.mask().isEmpty())
                    return 'rounded-disabled'
                material.side_effect = disabled
                details.refresh_material()
                self.assertTrue(details.mask().isEmpty())
                self.assertEqual(details.native_material, 'rounded-disabled')
            finally:
                details.close()
                details.deleteLater()
                self.app.processEvents()

    def test_alpha_fallback_keeps_clear_corners_when_resized(self):
        with tempfile.TemporaryDirectory() as name, patch.dict(os.environ, {'SESSION_USAGE_STATE_DIR': name}), \
                patch.object(QApplication, 'platformName', return_value='windows'), \
                patch('session_model_usage.overlay.apply_glass', return_value='alpha-only'):
            (Path(name) / 'settings.ini').write_text('GlassEnabled=True\nGlassOpacityPercent=40\n')
            details = Details()
            try:
                details.show()
                self.app.processEvents()
                for width, height in ((760,510),(620,450),(880,540)):
                    details.resize(width,height)
                    self.app.processEvents()
                    self.assertEqual(details.mask().boundingRect(), details.panel.geometry())
                    image = details.grab().toImage()
                    for point in ((0,0),(width-1,0),(0,height-1),(width-1,height-1)):
                        self.assertFalse(details.mask().contains(QPoint(*point)))
                        self.assertEqual(image.pixelColor(*point).alpha(),0)
            finally:
                details.close()
                details.deleteLater()
                self.app.processEvents()
