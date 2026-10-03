import os
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

from session_model_usage.native import Routes, NativeInspector, compose_geometry, log_roots
from session_model_usage.cdp import Observation, bind_window, toolbar_gap_physical
from session_model_usage.supervisor import Manager
from test_recovery import FakeBackend

FIRST = '00000000-0000-0000-0000-000000000001'
SECOND = '00000000-0000-0000-0000-000000000002'
CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def loaded(appearance='primary', window=1, stamp='2026-01-01T00:00:01.000Z'):
    return f'{stamp} info [window-manager] window main frame finished load appearance={appearance} windowId={window}\n'


def route(path=None, window=1, stamp='2026-01-01T00:00:02.000Z'):
    return (f'{stamp} info [electron-message-handler] IAB_LIFECYCLE received browser sidebar owner sync '
            f'conversationId=client-new-thread:ignored ownerRoutePath={path or "/local/"+FIRST} windowId={window}\n')


class RoutesTests(unittest.TestCase):
    def test_only_explicit_window_route_identifies_thread(self):
        r=Routes(1,CREATED,[]); r.consume(loaded())
        r.consume(f'2026-01-01T00:00:02.000Z info maybe_resume_started threadId={FIRST} windowId=1')
        self.assertIsNone(r.for_appearance('primary'))
        r.consume(route()); self.assertEqual(r.for_appearance('primary')['route'],'/local/'+FIRST)
    def test_old_pid_reuse_records_are_ignored(self):
        r=Routes(1,CREATED+5,[]); r.consume(loaded());r.consume(route())
        self.assertFalse(r.routes);self.assertFalse(r.appearances)
    def test_out_of_order_rotation_does_not_rewrite_latest(self):
        r=Routes(1,CREATED,[]);r.consume(loaded())
        r.consume(route('/local/'+SECOND,stamp='2026-01-01T00:00:03.000Z'))
        r.consume(route());self.assertEqual(r.for_appearance('primary')['route'],'/local/'+SECOND)
    def test_home_and_remote_navigation_clear_local_route(self):
        for path in ['/', '/settings', '/dots/'+SECOND, '/local/new']:
            r=Routes(1,CREATED,[]);r.consume(loaded());r.consume(route())
            r.consume(route(path,stamp='2026-01-01T00:00:03.000Z'))
            self.assertEqual(r.for_appearance('primary')['route'],path)
    def test_multiple_appearance_ids_are_ambiguous(self):
        r=Routes(1,CREATED,[]);r.consume(loaded());r.consume(route())
        r.consume(loaded(window=2));r.consume(route(window=2))
        self.assertIsNone(r.for_appearance('primary'))
    def test_primary_and_avatar_routes_stay_separate(self):
        r=Routes(1,CREATED,[]);r.consume(loaded());r.consume(route())
        r.consume(loaded('avatarOverlay',2));r.consume(route('/dots/'+SECOND,2))
        self.assertEqual(r.for_appearance('primary')['route'],'/local/'+FIRST)
    def test_half_line_is_not_used_until_complete(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d); p=folder/'2026/01/01';p.mkdir(parents=True)
            f=p/'codex-desktop-test-101-t0-i1-0.log'
            f.write_bytes((loaded()+route()[:-1]).encode())
            r=Routes(101,CREATED,[folder]);r.refresh()
            self.assertFalse(r.caught_up);self.assertFalse(r.routes)
            with f.open('ab') as out:out.write(b'\n')
            r.refresh();self.assertTrue(r.caught_up)
            self.assertEqual(r.for_appearance('primary')['route'],'/local/'+FIRST)
    def test_different_pid_cannot_supply_a_route(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'2026/01/01';p.mkdir(parents=True)
            (p/'codex-desktop-test-102-t0-i1-0.log').write_text(loaded()+route())
            r=Routes(101,CREATED,[Path(d)]);r.refresh()
            self.assertFalse(r.routes);self.assertFalse(r.caught_up)
    def test_unreadable_log_hides_and_rereads_after_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'route.log';f.write_text(loaded()+route())
            r=Routes(101,CREATED,[]);r.files[f]={'offset':0,'pending':b''};r.refresh()
            with patch.object(Path,'open',side_effect=PermissionError('fixture')):
                r.refresh()
            self.assertFalse(r.routes);self.assertFalse(r.caught_up)
            r.refresh();self.assertTrue(r.caught_up);self.assertTrue(r.routes)
    def test_large_log_catchup_cannot_display_earlier_route(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'route.log'
            f.write_bytes((loaded()+route()).encode()+b'x'*(4*1024*1024)+b'\n'+
                          route('/local/'+SECOND,stamp='2026-01-01T00:00:03.000Z').encode())
            r=Routes(101,CREATED,[]);r.files[f]={'offset':0,'pending':b''};r.refresh()
            self.assertFalse(r.caught_up)
            r.refresh();self.assertTrue(r.caught_up)
            self.assertEqual(r.for_appearance('primary')['route'],'/local/'+SECOND)
    def test_log_truncation_invalidates_old_view(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'route.log';f.write_text(loaded()+route())
            r=Routes(101,CREATED,[]);r.files[f]={'offset':0,'pending':b''};r.refresh()
            f.write_text('');r.refresh();self.assertFalse(r.routes)
    def test_deleted_rotation_does_not_block_current_log(self):
        with tempfile.TemporaryDirectory() as d:
            old=Path(d)/'old.log';current=Path(d)/'current.log'
            old.write_text(loaded()+route())
            r=Routes(101,CREATED,[]);r.files[old]={'offset':0,'pending':b''};r.refresh()
            old.unlink();current.write_text(route('/local/'+SECOND,stamp='2026-01-01T00:00:03.000Z'))
            r.files[current]={'offset':0,'pending':b''};r.refresh()
            self.assertTrue(r.caught_up)
            self.assertEqual(r.for_appearance('primary')['route'],'/local/'+SECOND)
    def test_date_boundary_accepts_local_directory_with_utc_records(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'2026/01/02';p.mkdir(parents=True)
            (p/'codex-desktop-test-101-t0-i1-0.log').write_text(loaded()+route())
            r=Routes(101,CREATED,[Path(d)]);r.refresh()
            self.assertTrue(r.caught_up);self.assertTrue(r.routes)
    def test_package_log_root_is_version_independent(self):
        with patch.dict(os.environ,{'LOCALAPPDATA':'C:/local'}):
            roots=log_roots(Path('C:/Program Files/WindowsApps/OpenAI.Codex_1.2.3.4_x64__publisher/app/ChatGPT.exe'))
        self.assertEqual(roots[0].as_posix(),'C:/local/Packages/OpenAI.Codex_publisher/LocalCache/Local/Codex/Logs')


def nodes():
    return [{'type':50004,'name':'Ask anything','rect':(120,100,620,144)},
            {'type':50000,'name':'Change permissions','rect':(125,148,155,176)},
            {'type':50006,'name':'Context usage: 25%','rect':(440,152,460,172)}]


class NativeGeometryTests(unittest.TestCase):
    def test_geometry_uses_only_gap_between_permissions_and_context(self):
        composer,gap=compose_geometry(nodes())
        self.assertEqual(gap,{'left':161,'top':148,'width':273,'height':28})
        self.assertEqual(composer['top'],90)
    def test_ambiguous_composers_hide_instead_of_guessing(self):
        self.assertIsNone(compose_geometry(nodes()+nodes()))
    def test_missing_context_never_overlaps_model_controls(self):
        self.assertIsNone(compose_geometry(nodes()[:2]))
    def test_native_hwnd_wins_without_dpi_geometry_guess(self):
        view=Observation('native',{'nativeHwnd':202,'composer':{},'focused':True})
        view.data['composer']={'top':1}
        windows=[{'hwnd':201},{'hwnd':202}]
        self.assertEqual(bind_window([view],windows,202)[1]['hwnd'],202)
    def test_native_physical_coordinates_are_not_scaled_twice(self):
        data={'viewport':{'width':1000,'height':700},'toolbarGap':{'left':161,'top':148,'width':273,'height':28}}
        self.assertEqual(toolbar_gap_physical(data,(-1800,400),(1000,700)),(-1639,548,273,28))


class NativeObservationTests(unittest.TestCase):
    def setUp(self):
        self.r=Routes(os.getpid(),0,[]);self.r.consume(loaded());self.r.consume(route())
        self.r.caught_up=True;self.r.refresh=Mock()
        geometry=compose_geometry(nodes())
        self.view={'appearance':'primary','composer':geometry[0],'gap':geometry[1],'selection':((1,2),)}
        self.reader=Mock();self.reader.read.return_value=self.view
        self.n=NativeInspector(os.getpid(),reader=self.reader,routes=self.r)
        self.window={'hwnd':101,'client_origin':(100,50),'client_size':(800,600)}
        self.win=patch('session_model_usage.native.visible_app_windows',return_value=[self.window]);self.win.start()
        self.fg=patch('session_model_usage.native.foreground_info',return_value=(0,0));self.fg.start()
    def tearDown(self):self.n.close();self.win.stop();self.fg.stop()
    def test_start_after_codex_reads_existing_explicit_route(self):
        views=self.n.observe();self.assertEqual(views[0].data['threadId'],FIRST)
        self.assertEqual(views[0].data['nativeHwnd'],101)
    def test_new_selection_hides_old_route_until_notification_arrives(self):
        self.n.observe();self.view['selection']=((3,4),)
        self.assertFalse(self.n.observe())
        self.r.consume(route('/local/'+SECOND,stamp='2026-01-01T00:00:03.000Z'))
        self.assertEqual(self.n.observe()[0].data['threadId'],SECOND)
    def test_source_not_caught_up_hides_existing_view(self):
        self.n.observe();self.r.caught_up=False
        self.assertFalse(self.n.observe())
    def test_remote_view_hides_old_local_usage(self):
        self.n.observe();self.r.consume(route('/dots/'+SECOND,stamp='2026-01-01T00:00:03.000Z'))
        self.assertFalse(self.n.observe())
    def test_two_same_appearance_windows_are_not_paired_by_title(self):
        with patch('session_model_usage.native.visible_app_windows',return_value=[self.window,{**self.window,'hwnd':102}]):
            self.assertFalse(self.n.observe())
    def test_close_is_idempotent(self):
        self.n.close();self.n.close();self.reader.close.assert_called_once()


class IndependentLaunchTests(unittest.TestCase):
    def test_both_startup_orders_and_twenty_official_reopens(self):
        for app_first in (True,False):
            with tempfile.TemporaryDirectory() as d:
                backend=FakeBackend();backend.native_available=lambda app:True
                m=Manager('fixture',folder=Path(d),backend=backend)
                try:
                    if not app_first:
                        m.tick();m.retry();m.tick()
                        self.assertEqual(backend.starts,0)
                        self.assertEqual(m.state['status'],'waiting_for_codex')
                    for i in range(20):
                        app=backend.app(pid=101+i,created=123.+i,port=None);m.tick()
                        self.assertIsNotNone(m.worker)
                        self.assertEqual(m.state['connection_mode'],'windows_accessibility')
                        self.assertFalse(m.restart_armed)
                        backend.current=[];m.tick()
                        self.assertIsNone(m.state['thread_id'])
                        self.assertEqual(m.state['status'],'waiting_for_codex')
                        app.terminate.assert_not_called();app.kill.assert_not_called()
                    self.assertEqual(backend.starts,0)
                    self.assertEqual(len(backend.spawned),20)
                finally:m.detach();m.pool.shutdown(wait=True,cancel_futures=True)
    def test_native_to_cdp_handoff_replaces_worker(self):
        with tempfile.TemporaryDirectory() as d:
            backend=FakeBackend();backend.native_available=lambda app:True
            m=Manager('fixture',folder=Path(d),backend=backend)
            try:
                backend.app(port=None);m.tick();first=m.worker
                backend.app(port=4321);m.tick()
                self.assertIsNot(m.worker,first);self.assertEqual(m.state['connection_mode'],'cdp')
                self.assertIsNone(m.state['thread_id'])
            finally:m.detach();m.pool.shutdown(wait=True,cancel_futures=True)
