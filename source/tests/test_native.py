import os
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from session_model_usage.native import Accessibility, Routes, NativeInspector, compose_geometry, log_roots
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
    def test_new_desktop_non_conversation_shell_does_not_block_primary(self):
        r=Routes(1,CREATED,[]);r.consume(loaded());r.consume(route())
        r.consume(loaded(window=3));r.consume(loaded('detached',4))
        self.assertEqual(r.for_appearance('primary')['route'],'/local/'+FIRST)
        self.assertIsNone(r.for_appearance('detached'))
    def test_second_shell_becomes_ambiguous_when_it_publishes_a_route(self):
        r=Routes(1,CREATED,[]);r.consume(loaded());r.consume(route())
        r.consume(loaded(window=3));r.consume(route('/local/'+SECOND,window=3))
        self.assertIsNone(r.for_appearance('primary'))
    def test_quoted_lifecycle_text_cannot_supply_a_route(self):
        r=Routes(1,CREATED,[]);r.consume(loaded())
        r.consume('2026-01-01T00:00:02.000Z info [other] quoted '+route())
        self.assertFalse(r.routes)
    def test_route_without_verified_appearance_is_not_used(self):
        r=Routes(1,CREATED,[]);r.consume(route())
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
    def test_scrolled_selected_row_is_read_without_using_offscreen_composer_controls(self):
        reader=Accessibility.__new__(Accessibility)
        reader.client=Mock();reader.module=Mock();reader.condition=Mock();reader.cache=Mock()
        document=Mock()
        reader.client.ElementFromHandle.return_value.FindFirst.return_value=document
        document.GetCurrentPattern.return_value.QueryInterface.return_value.CurrentValue='app://-/index.html'
        document.GetRuntimeId.return_value=(42,1)
        controls=[]
        for data in nodes()+[{'type':50000,'name':'selected row','rect':(20,-60,100,-30)}]:
            left,top,right,bottom=data['rect']
            control=Mock(CachedControlType=data['type'],CachedName=data['name'],
                CachedIsOffscreen=top<0,CachedAriaProperties='current=page' if top<0 else '',
                CachedBoundingRectangle=SimpleNamespace(left=left,top=top,right=right,bottom=bottom))
            control.GetCachedPropertyValue.return_value=(3,4)
            controls.append(control)
        elements=document.FindAllBuildCache.return_value
        elements.Length=len(controls);elements.GetElement.side_effect=controls
        result=reader.read({'hwnd':101})
        self.assertEqual(result['selection'],((3,4),))
        self.assertEqual(result['gap'],compose_geometry(nodes())[1])
        controls[-1].GetCurrentPattern.assert_not_called()
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
        self.bindings=Mock();self.bindings.resolve.return_value=None
        self.n=NativeInspector(os.getpid(),reader=self.reader,routes=self.r,bindings=self.bindings)
        self.window={'hwnd':101,'client_origin':(100,50),'client_size':(800,600)}
        self.win=patch('session_model_usage.native.visible_app_windows',return_value=[self.window]);self.win.start()
        self.fg=patch('session_model_usage.native.foreground_info',return_value=(0,0));self.fg.start()
    def tearDown(self):self.n.close();self.win.stop();self.fg.stop()
    def test_start_after_codex_reads_existing_explicit_route(self):
        views=self.n.observe();self.assertEqual(views[0].data['threadId'],FIRST)
        self.assertEqual(views[0].data['nativeHwnd'],101)
    def test_micro_draft_attaches_without_selected_sidebar_row_or_thread_id(self):
        self.r.consume(route('/local/client-new-thread:'+SECOND,stamp='2026-01-01T00:00:03.000Z'))
        self.view.update(selection=(),selection_required=True)
        data=self.n.observe()[0].data
        self.assertIsNone(data['threadId'])
        self.assertEqual(data['viewKey'],'client-new-thread:'+SECOND)
        self.assertEqual(data['nativeHwnd'],101)
    def test_micro_draft_becomes_real_session_without_a_sidebar_change(self):
        self.r.consume(route('/local/client-new-thread:'+FIRST,stamp='2026-01-01T00:00:03.000Z'))
        self.view.update(selection=(),selection_required=False)
        self.assertIsNone(self.n.observe()[0].data['threadId'])
        self.r.consume(route('/local/'+SECOND,stamp='2026-01-01T00:00:04.000Z'))
        self.assertEqual(self.n.observe()[0].data['threadId'],SECOND)
    def test_micro_saved_draft_resolves_explicit_binding_without_route_change(self):
        self.r.consume(route('/local/client-new-thread:'+FIRST,stamp='2026-01-01T00:00:03.000Z'))
        self.view.update(selection=(),selection_required=True)
        self.assertIsNone(self.n.observe()[0].data['threadId'])
        self.bindings.resolve.return_value=SECOND
        data=self.n.observe()[0].data
        self.assertEqual(data['threadId'],SECOND)
        self.assertEqual(data['viewKey'],SECOND)
        self.assertEqual(data['threadSource'],'desktop_client_binding')
        self.bindings.resolve.assert_called_with('client-new-thread:'+FIRST)
    def test_micro_binding_loss_clears_formal_identity(self):
        self.r.consume(route('/local/client-new-thread:'+FIRST,stamp='2026-01-01T00:00:03.000Z'))
        self.view.update(selection=(),selection_required=False)
        self.bindings.resolve.return_value=SECOND
        self.assertEqual(self.n.observe()[0].data['threadId'],SECOND)
        self.bindings.resolve.return_value=None
        self.assertIsNone(self.n.observe()[0].data['threadId'])
    def test_micro_draft_switch_clears_real_identity_and_tracks_each_draft(self):
        self.assertEqual(self.n.observe()[0].data['threadId'],FIRST)
        for index,owner in enumerate((FIRST,SECOND)):
            self.r.consume(route('/local/client-new-thread:'+owner,
                stamp=f'2026-01-01T00:00:0{index+3}.000Z'))
            data=self.n.observe()[0].data
            self.assertIsNone(data['threadId'])
            self.assertEqual(data['viewKey'],'client-new-thread:'+owner)
    def test_micro_draft_and_main_window_bind_their_own_routes(self):
        self.r.consume(loaded('detached',window=4))
        self.r.consume(route('/local/client-new-thread:'+SECOND,window=4))
        detached={**self.view,'appearance':'detached','selection':(),'selection_required':False}
        self.reader.read.side_effect=[self.view,detached]
        with patch('session_model_usage.native.visible_app_windows',return_value=[
                self.window,{**self.window,'hwnd':102}]):
            views=self.n.observe()
        self.assertEqual([(v.data['nativeHwnd'],v.data['threadId']) for v in views],
                         [(101,FIRST),(102,None)])
    def test_stale_coordinates_during_window_move_hide_until_consistent(self):
        self.assertTrue(self.n.observe())
        self.window['client_origin']=(900,50)
        self.assertFalse(self.n.observe())
        self.window['client_origin']=(100,50)
        self.assertTrue(self.n.observe())
    def test_new_selection_hides_old_route_until_notification_arrives(self):
        self.n.observe();self.view['selection']=((3,4),)
        self.assertFalse(self.n.observe())
        self.r.consume(route('/local/'+SECOND,stamp='2026-01-01T00:00:03.000Z'))
        self.assertEqual(self.n.observe()[0].data['threadId'],SECOND)
    def test_micro_then_scrolling_duplicate_selected_rows_keeps_confirmed_route(self):
        self.r.consume(route('/local/client-new-thread:'+FIRST,stamp='2026-01-01T00:00:03.000Z'))
        self.bindings.resolve.return_value=FIRST
        self.n.observe()
        self.r.consume(route('/local/'+FIRST,stamp='2026-01-01T00:00:04.000Z'))
        self.view['selection']=((3,4),(5,6))
        self.assertEqual(self.n.observe()[0].data['threadId'],FIRST)
        for selected in (((3,4),),((3,4),(5,6)),((5,6),)):
            self.view['selection']=selected
            self.assertEqual(self.n.observe()[0].data['threadId'],FIRST)
        self.view['selection']=((7,8),)
        self.assertFalse(self.n.observe())
        self.r.consume(route('/local/'+SECOND,stamp='2026-01-01T00:00:05.000Z'))
        self.assertEqual(self.n.observe()[0].data['threadId'],SECOND)
    def test_route_before_selection_waits_for_matching_sidebar_render(self):
        self.n.observe()
        self.r.consume(route('/local/'+SECOND,stamp='2026-01-01T00:00:03.000Z'))
        self.assertFalse(self.n.observe())
        self.view['selection']=((3,4),)
        self.assertEqual(self.n.observe()[0].data['threadId'],SECOND)
        self.assertEqual(self.n.observe()[0].data['threadId'],SECOND)
    def test_home_to_local_does_not_bind_half_rendered_sidebar(self):
        self.r.consume(route('/',stamp='2026-01-01T00:00:03.000Z'))
        self.view.update(selection=(),selection_required=True)
        self.assertFalse(self.n.observe())
        self.r.consume(route('/local/'+SECOND,stamp='2026-01-01T00:00:04.000Z'))
        self.assertFalse(self.n.observe())
        self.view['selection']=((3,4),)
        self.assertEqual(self.n.observe()[0].data['threadId'],SECOND)
    def test_sidebar_collapse_and_expand_do_not_require_a_new_route(self):
        self.n.observe()
        self.view.update(selection=(),selection_required=False)
        self.assertEqual(self.n.observe()[0].data['threadId'],FIRST)
        self.view.update(selection=((3,4),),selection_required=True)
        self.assertEqual(self.n.observe()[0].data['threadId'],FIRST)
    def test_source_not_caught_up_hides_existing_view(self):
        self.n.observe();self.r.caught_up=False
        self.assertFalse(self.n.observe())
    def test_remote_view_hides_old_local_usage(self):
        self.n.observe();self.r.consume(route('/dots/'+SECOND,stamp='2026-01-01T00:00:03.000Z'))
        self.assertFalse(self.n.observe())
    def test_two_same_appearance_windows_are_not_paired_by_title(self):
        with patch('session_model_usage.native.visible_app_windows',return_value=[self.window,{**self.window,'hwnd':102}]):
            self.assertFalse(self.n.observe())
    def test_micro_opens_second_primary_without_losing_confirmed_main(self):
        self.n.observe()
        self.r.consume(loaded(window=3))
        self.r.consume(route('/local/'+SECOND,window=3))
        second={**self.view,'selection_required':False,'selection':()}
        self.reader.read.side_effect=[self.view,second]
        with patch('session_model_usage.native.visible_app_windows',return_value=[
                self.window,{**self.window,'hwnd':102}]):
            views=self.n.observe()
        self.assertEqual([(v.data['nativeHwnd'],v.data['threadId']) for v in views],
                         [(101,FIRST),(102,SECOND)])
    def test_micro_second_primary_switches_and_survives_main_minimizing(self):
        self.n.observe(); self.r.consume(loaded(window=3)); self.r.consume(route('/local/'+SECOND,window=3))
        second={**self.view,'selection_required':False,'selection':()}
        self.reader.read.side_effect=[self.view,second]
        window={**self.window,'hwnd':102}
        with patch('session_model_usage.native.visible_app_windows',return_value=[self.window,window]):
            self.n.observe()
        self.r.consume(route('/local/client-new-thread:'+FIRST,window=3,stamp='2026-01-01T00:00:03.000Z'))
        self.reader.read.side_effect=None; self.reader.read.return_value=second
        with patch('session_model_usage.native.visible_app_windows',return_value=[window]):
            data=self.n.observe()[0].data
        self.assertEqual(data['nativeHwnd'],102)
        self.assertIsNone(data['threadId'])
        self.assertEqual(data['viewKey'],'client-new-thread:'+FIRST)
    def test_initial_document_route_disambiguates_two_primary_windows_on_start(self):
        self.r.consume(loaded(window=3)); self.r.consume(route('/local/client-new-thread:'+SECOND,window=3))
        second={**self.view,'initial_route':'/local/client-new-thread:'+SECOND,'selection':()}
        self.reader.read.side_effect=[self.view,second]
        with patch('session_model_usage.native.visible_app_windows',return_value=[
                self.window,{**self.window,'hwnd':102}]):
            views=self.n.observe()
        self.assertEqual([(v.data['nativeHwnd'],v.data['viewKey']) for v in views],
                         [(101,FIRST),(102,'client-new-thread:'+SECOND)])
    def test_duplicate_initial_routes_cannot_disambiguate_same_type_windows(self):
        self.r.consume(loaded(window=3)); self.r.consume(route('/local/'+SECOND,window=3))
        self.view['initial_route']='/local/'+SECOND
        with patch('session_model_usage.native.visible_app_windows',return_value=[
                self.window,{**self.window,'hwnd':102}]):
            self.assertFalse(self.n.observe())
    def test_recycled_hwnd_document_cannot_reuse_another_window_binding(self):
        self.view['document_key']=(42,100,4,6,1,3)
        self.n.observe()
        self.r.consume(loaded(window=3)); self.r.consume(route('/local/'+SECOND,window=3))
        self.view['document_key']=(42,200,4,7,1,3)
        self.assertFalse(self.n.observe())
    def test_close_is_idempotent(self):
        self.n.close();self.n.close();self.reader.close.assert_called_once()
    def test_transient_snapshot_failure_keeps_incremental_route_reader(self):
        self.n.observe()
        self.reader.read.side_effect=[OSError('invalidated element'),self.view]
        with patch('session_model_usage.diagnostics.record') as record:
            self.assertFalse(self.n.observe())
            record.assert_called_once()
        self.assertIs(self.n.routes,self.r)
        self.assertFalse(self.n.connection_failed)
        self.assertEqual(self.n.observe()[0].data['threadId'],FIRST)
        self.reader.close.assert_not_called()
    def test_auxiliary_snapshot_failure_does_not_block_confirmed_host(self):
        self.reader.read.side_effect=[OSError('auxiliary element'),self.view]
        with patch('session_model_usage.native.visible_app_windows',return_value=[{**self.window,'hwnd':102},self.window]),patch('session_model_usage.diagnostics.record'):
            self.assertEqual(self.n.observe()[0].data['nativeHwnd'],101)
    def test_missing_geometry_has_specific_reason_and_hides_previous_view(self):
        self.n.observe();self.reader.read.return_value=None
        self.assertFalse(self.n.observe())
        self.assertIn('输入栏辅助功能',self.n.problem)


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
