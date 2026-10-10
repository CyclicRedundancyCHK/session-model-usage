from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from session_model_usage.cdp import Inspector, Observation
from session_model_usage.identity import ClientBindings
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
    def test_cdp_saved_client_route_queries_its_explicit_formal_identity(self):
        import json
        client='client-new-thread:00000000-0000-0000-0000-000000000001'
        thread='00000000-0000-0000-0000-000000000002'
        path=Path(self.temp.name)/'state.json'
        path.write_text(json.dumps({'electron-persisted-atom-state':{
            'client-thread-bindings-v1':{client:thread}}}))
        inspector=Inspector(1234,bindings=ClientBindings(path))
        target={'id':'fixture','type':'page','url':'app://-/index.html',
                'webSocketDebuggerUrl':'ws://127.0.0.1:1234/devtools/page/fixture'}
        socket=Mock();socket.call.return_value={'result':{'value':{
            'threadId':None,'viewKey':client,'composer':{'top':1}}}}
        inspector.get_json=Mock(return_value=[target]);inspector.connections['fixture']=socket
        inspector._bounds=Mock(return_value=None)
        try:
            data=inspector.observe()[0].data
            self.assertEqual(data['threadId'],thread)
            self.assertEqual(data['viewKey'],thread)
            self.assertEqual(data['threadSource'],'desktop_client_binding')
        finally:inspector.close()
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
    def test_micro_draft_observer_never_queries_a_client_uuid(self):
        worker=Observer(0,os.getpid(),'test','attachment')
        inspector=Mock(); service=Mock()
        view=Observation('native:test',{'threadId':None,'viewKey':'client-new-thread:fixture',
                                      'composer':{},'focused':True})
        inspector.observe.side_effect=lambda: worker.stop_event.set() or [view]
        window={'hwnd':101,'scale':1}
        with patch('session_model_usage.overlay.alive',return_value=True), \
             patch('session_model_usage.overlay.foreground_info',return_value=(101,os.getpid())), \
             patch('session_model_usage.overlay.visible_app_windows',return_value=[window]), \
             patch('session_model_usage.overlay.bind_window',return_value=(view,window)), \
             patch('session_model_usage.overlay.client_geometry',return_value=((0,0),(800,600))):
            worker.observe_loop(inspector,service,0,{})
        service.query.assert_not_called()
    def test_micro_draft_switch_clears_usage_and_rejects_late_old_result(self):
        fake=Mock(); fake.current='old'; fake.view_key='old'; fake.usage={'old':True}
        fake.badge.width.return_value=100; fake.badge.height.return_value=28
        fake.badge.winId.return_value=102
        fake.badge.isVisible.return_value=True; fake.details.isVisible.return_value=False
        data={'threadId':None,'viewKey':'client-new-thread:first','dark':True,
              'native_scale':1,'client_origin':(0,0),'client_size':(800,600),
              'viewport':{'width':800,'height':600},
              'toolbarGap':{'left':200,'top':500,'width':300,'height':28},'host_hwnd':101}
        with patch('session_model_usage.overlay.attach_to_window'), \
             patch('session_model_usage.overlay.position_without_focus'):
            Overlay.observe(fake,data)
            self.assertIsNone(fake.current); self.assertIsNone(fake.usage)
            self.assertTrue(fake.worker.placement['visible'])
            Overlay.update_usage(fake,{'thread_id':'old','totals':{'total_tokens':123}})
            self.assertIsNone(fake.usage)
            fake.badge.update_usage.reset_mock()
            Overlay.observe(fake,{**data,'viewKey':'client-new-thread:second'})
            fake.badge.update_usage.assert_called_once_with(None)
            self.assertEqual(fake.worker.placement['view_key'],'client-new-thread:second')


if __name__=='__main__': unittest.main()
