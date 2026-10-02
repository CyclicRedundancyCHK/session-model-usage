from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from session_model_usage.cdp import Inspector
from session_model_usage.overlay import Observer, Overlay
from session_model_usage.runtime_io import read_json


class ObserverRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.environment=patch.dict(os.environ,{'SESSION_USAGE_STATE_DIR':self.temp.name})
        self.environment.start()
    def tearDown(self):
        self.environment.stop(); self.temp.cleanup()
    def test_browser_disconnect_discards_stale_connection(self):
        inspector=Inspector(1234)
        socket=Mock(); socket.call.side_effect=OSError('fixture')
        inspector.browser=socket
        self.assertIsNone(inspector._bounds('fixture'))
        socket.close.assert_called_once(); self.assertIsNone(inspector.browser)
    def test_initialization_exception_is_reported_to_manager(self):
        with tempfile.TemporaryDirectory() as d, \
             patch('session_model_usage.overlay.state_directory',return_value=Path(d)), \
             patch('session_model_usage.overlay.UsageService',side_effect=OSError('fixture')):
            worker=Observer(1234,1,'test','attachment'); worker.run()
            report=read_json(Path(d)/'observer-attachment.json')
        self.assertEqual(report['status'],'stopped')
        self.assertEqual(report['last_error'],'OSError')
        self.assertFalse(report['overlay_visible'])
    def test_invalid_ui_geometry_hides_stale_view_without_raising(self):
        fake=Mock(); fake.current='old'; fake.usage={'old':True}
        fake.data={'old':True}
        Overlay.observe(fake,{'threadId':'new','dark':True})
        self.assertIsNone(fake.current)
        self.assertIsNone(fake.usage)
        fake.badge.hide.assert_called(); fake.details.hide.assert_called()
        self.assertFalse(fake.worker.placement['visible'])
    def test_lost_view_clears_current_session(self):
        fake=Mock(); fake.current='old'; fake.usage={'old':True}
        Overlay.observe(fake,None)
        self.assertIsNone(fake.current); self.assertIsNone(fake.usage)


if __name__=='__main__': unittest.main()
