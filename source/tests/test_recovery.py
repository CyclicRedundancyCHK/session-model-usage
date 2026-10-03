import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from session_model_usage import controller
from session_model_usage.supervisor import Manager
from session_model_usage.runtime_io import atomic_json


class Clock:
    value = 100.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class FakeBackend:
    def __init__(self):
        self.current = []
        self.spawned = []
        self.stopped = []
        self.probe_error = None
        self.starts = 0
    def app(self, pid=101, created=123., port=1234):
        value = Mock(pid=pid)
        value.create_time.return_value = created
        value.cmdline.return_value = [] if port is None else [f'--remote-debugging-port={port}']
        self.current = [value]
        return value
    def apps(self): return self.current
    def start(self): self.starts += 1
    def probe(self, app, port):
        if self.probe_error: raise self.probe_error
    def spawn(self, app, port, run_id, attachment_id):
        value = Mock(pid=200 + len(self.spawned))
        value.poll.return_value = None
        self.spawned.append(value)
        return value, 456.
    def stop_worker(self, process, created):
        self.stopped.append(process)
        process.poll.return_value = 0


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.backend, self.clock = FakeBackend(), Clock()
        self.manager = Manager('test', folder=self.folder, backend=self.backend, clock=self.clock)
    def tearDown(self):
        self.manager.detach()
        self.manager.pool.shutdown(wait=True, cancel_futures=True)
        self.temp.cleanup()
    def report(self, **values):
        value = {'attachment_id':self.manager.attachment_id, 'run_id':'test',
                 'heartbeat_monotonic':self.clock(), 'heartbeat':time.time(), **values}
        atomic_json(self.folder/f'observer-{self.manager.attachment_id}.json', value)

    def test_codex_close_retains_manager_without_reopening(self):
        self.backend.app(); self.manager.tick()
        self.backend.current = []; self.manager.tick()
        self.assertEqual(self.manager.state['status'], 'waiting_for_codex')
        self.assertEqual(self.backend.starts, 0)
        self.assertFalse(self.manager.stop_event.is_set())

    def test_new_pid_and_port_clear_old_view_and_reattach(self):
        self.backend.app(); self.manager.tick()
        self.report(status='connected', thread_id='old', overlay_visible=True)
        self.manager.tick()
        self.backend.app(pid=102, port=4321); self.manager.tick()
        self.assertIsNone(self.manager.state['thread_id'])
        self.assertEqual(self.manager.state['port'], 4321)
        self.assertEqual(len(self.backend.spawned), 2)

    def test_reused_pid_is_a_new_host(self):
        self.backend.app(); self.manager.tick()
        self.backend.app(created=999.); self.manager.tick()
        self.assertEqual(len(self.backend.spawned), 2)

    def test_same_pid_changed_port_replaces_worker(self):
        self.backend.app(); self.manager.tick()
        self.backend.app(port=4321); self.manager.tick()
        self.assertEqual(len(self.backend.spawned), 2)

    def test_crashed_worker_recovers_after_backoff(self):
        self.backend.app(); self.manager.tick()
        self.backend.spawned[-1].poll.return_value = 7
        self.manager.tick(); self.manager.tick()
        self.assertEqual(len(self.backend.spawned), 1)
        self.clock.advance(1); self.manager.tick()
        self.assertEqual(len(self.backend.spawned), 2)
        self.assertEqual(self.manager.state['recovery_count'], 1)

    def test_heartbeat_freeze_is_recovered(self):
        self.backend.app(); self.manager.tick()
        self.clock.advance(16); self.manager.tick()
        self.assertEqual(self.manager.state['last_error']['reason'], 'worker_heartbeat_expired')
        self.assertFalse(self.manager.state['overlay_visible'])

    def test_alive_heartbeat_does_not_restart_slow_usage_reader(self):
        self.backend.app(); self.manager.tick()
        for _ in range(30):
            self.clock.advance(1); self.report(status='connected'); self.manager.tick()
        self.assertEqual(len(self.backend.spawned), 1)

    def test_five_failures_pause_until_manual_retry(self):
        self.backend.app()
        for _ in range(5):
            self.manager.tick()
            self.backend.spawned[-1].poll.return_value = 1
            self.manager.tick(); self.clock.advance(15)
        # Keep all failures inside one minute for the circuit breaker.
        self.manager.failures.clear(); self.clock.value = 200
        for _ in range(5): self.manager.failure('fixture_failure'); self.clock.advance(1)
        self.assertTrue(self.manager.paused)
        count = len(self.backend.spawned); self.manager.tick()
        self.assertEqual(len(self.backend.spawned), count)
        self.manager.retry(); self.manager.tick()
        self.assertFalse(self.manager.paused)
        self.assertEqual(len(self.backend.spawned), count+1)

    def test_more_than_one_host_never_guesses(self):
        first=self.backend.app(); second=self.backend.app(pid=102)
        self.backend.current=[first,second]; self.manager.tick()
        self.assertEqual(self.manager.state['status'], 'ambiguous_host')
        self.assertFalse(self.backend.spawned)

    def test_no_debug_port_never_attaches_or_stops_codex(self):
        host=self.backend.app(port=None); self.manager.tick()
        self.assertEqual(self.manager.state['status'], 'waiting_for_restart')
        self.assertFalse(self.backend.spawned)
        host.terminate.assert_not_called()
        host.kill.assert_not_called()

    def test_retry_does_not_reopen_codex_after_user_closes_it(self):
        self.backend.app(port=None); self.manager.retry(); self.manager.tick()
        self.backend.current=[]; self.manager.tick()
        self.assertIsNone(self.manager.start_future)
        self.assertEqual(self.backend.starts, 0)

    def test_cancel_recovery_keeps_idle_after_codex_closes(self):
        self.backend.app(port=None); self.manager.retry(); self.manager.tick()
        atomic_json(self.folder/'commands/cancel.json', {'action':'cancel_retry','run_id':'test'})
        self.manager.tick(); self.manager.publish()
        self.assertFalse(self.manager.state['restart_armed'])
        self.assertIn('正在待机', self.manager.state['message'])
        self.backend.current=[]; self.manager.tick()
        self.assertEqual(self.backend.starts,0)

    def test_successful_connection_consumes_restart_arrangement(self):
        self.backend.app(port=None); self.manager.retry(); self.manager.tick()
        self.backend.app(pid=102,port=4321); self.manager.tick(); self.manager.publish()
        self.assertFalse(self.manager.state['restart_armed'])
        self.backend.current=[]; self.manager.tick()
        self.assertEqual(self.backend.starts,0)

    def test_cold_start_retries_after_ninety_seconds(self):
        self.backend.app(); self.backend.probe_error=OSError('fixture')
        self.manager.tick(); self.clock.advance(91); self.manager.tick()
        self.assertEqual(self.manager.state['status'], 'connection_error')
        self.assertFalse(self.manager.stop_event.is_set())
        self.backend.probe_error=None; self.clock.advance(15); self.manager.tick()
        self.assertEqual(len(self.backend.spawned), 1)

    def test_foreign_attachment_report_cannot_supply_usage(self):
        self.backend.app(); self.manager.tick()
        self.report(attachment_id='wrong', status='connected', thread_id='wrong')
        self.manager.tick()
        self.assertIsNone(self.manager.state['thread_id'])

    def test_stop_command_ends_manager_before_any_restart(self):
        path=self.folder/'commands/test.json'
        atomic_json(path, {'action':'stop','run_id':'test'})
        self.manager.open_requested=True; self.manager.tick()
        self.assertTrue(self.manager.stop_event.is_set())
        self.assertEqual(self.backend.starts,0)

    def test_stale_stop_command_does_not_stop_new_run(self):
        atomic_json(self.folder/'commands/test.json', {'action':'stop','run_id':'old'})
        self.manager.tick()
        self.assertFalse(self.manager.stop_event.is_set())

    def test_legacy_cached_stop_request_is_honored(self):
        (self.folder/'stop.request').write_text('test')
        self.manager.tick()
        self.assertTrue(self.manager.stop_event.is_set())

    def test_idle_state_prevents_legacy_launcher_from_starting_duplicate(self):
        self.manager.tick()
        self.assertEqual(self.manager.state['overlay_pid'],self.manager.state['supervisor_pid'])
        self.assertIsNone(self.manager.state['worker_pid'])
        self.assertFalse(self.manager.state['overlay_visible'])

    def test_duplicate_launcher_requests_existing_manager(self):
        value={'running':True,'schema_version':2,'run_id':'test'}
        with patch.object(controller,'status',return_value=value), patch.object(controller,'request') as request, \
                patch.object(controller.subprocess,'Popen') as popen:
            self.assertEqual(controller.launch(),value)
        request.assert_called_once_with('retry','test'); popen.assert_not_called()

    def test_dead_manager_clears_old_view(self):
        with patch.object(controller,'read_state',return_value={'schema_version':2,'overlay_visible':True,'thread_id':'old'}), \
                patch.object(controller,'alive',return_value=False):
            value=controller.status()
        self.assertFalse(value['overlay_visible']); self.assertIsNone(value['thread_id'])

    def test_idle_manager_is_running_and_healthy(self):
        with patch.object(controller,'read_state',return_value={'schema_version':2,'status':'waiting_for_codex','heartbeat':time.time()}), \
                patch.object(controller,'alive',return_value=True):
            value=controller.status()
        self.assertTrue(value['running']); self.assertTrue(value['healthy'])

    def test_stale_heartbeat_is_not_reported_as_visible(self):
        with patch.object(controller,'read_state',return_value={'schema_version':2,'heartbeat':0,'overlay_visible':True,'thread_id':'old'}), \
                patch.object(controller,'alive',return_value=True):
            value=controller.status()
        self.assertEqual(value['status'],'unresponsive'); self.assertIsNone(value['thread_id'])


if __name__ == '__main__': unittest.main()
