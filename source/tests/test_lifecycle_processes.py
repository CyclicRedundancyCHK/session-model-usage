"""Recovery acceptance against disposable OS processes; never touches user Codex."""
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest

import psutil

from session_model_usage.supervisor import Backend, Manager

# Windows venv python.exe can be a redirector with a different PID from its
# interpreter. Use the real interpreter plus this environment's dependencies.
FIXTURE_PYTHON = getattr(sys, '_base_executable', sys.executable)
FIXTURE_ENV = {**os.environ, 'PYTHONPATH':sysconfig.get_path('purelib') + os.pathsep + os.environ.get('PYTHONPATH','')}
FIXTURE_ENV.pop('__PYVENV_LAUNCHER__',None)


CHILD = '''import json,sys,time
from pathlib import Path
p=Path(sys.argv[1]); run_id=sys.argv[2]; attachment_id=sys.argv[3]
while True:
 value={'run_id':run_id,'attachment_id':attachment_id,'heartbeat':time.time(),
        'heartbeat_monotonic':time.monotonic(),'status':'connected','message':'fixture',
        'thread_id':'00000000-0000-0000-0000-000000000001','overlay_visible':True}
 t=p.with_suffix('.tmp'); t.write_text(json.dumps(value)); t.replace(p)
 time.sleep(.05)
'''

HOST = '''import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QWidget
app=QApplication([]); app.setQuitOnLastWindowClosed(False)
window=QWidget(); window.setWindowTitle('Session usage isolated acceptance')
Path(sys.argv[1]).write_text(str(int(window.winId())))
QTimer.singleShot(120000,app.quit)
app.exec()
'''


class NativeBackend(Backend):
    def __init__(self, folder):
        self.folder=folder; self.host=None; self.children=[]; self.starts=0
    def apps(self):
        return [psutil.Process(self.host.pid)] if self.host and self.host.poll() is None else []
    def start(self):
        self.starts+=1
        raise AssertionError('Acceptance must not launch the user application')
    def open_fixture(self):
        marker=self.folder/'host-window.txt'; marker.unlink(missing_ok=True)
        self.host=subprocess.Popen([FIXTURE_PYTHON,'-c',HOST,str(marker),
                                    '--remote-debugging-port=1234'],env=FIXTURE_ENV,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:
            time.sleep(.02)
        assert marker.exists() and int(marker.read_text())>0, 'Native test window not created'
        return self.host
    def probe(self, app, port):
        assert app.pid==self.host.pid and port==1234
    def spawn(self, app, port, run_id, attachment_id):
        path=self.folder/f'observer-{attachment_id}.json'
        child=subprocess.Popen([FIXTURE_PYTHON,'-c',CHILD,str(path),run_id,attachment_id],env=FIXTURE_ENV,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.children.append(child)
        return child,psutil.Process(child.pid).create_time()
    def cleanup(self):
        for child in [*self.children,self.host]:
            if child and child.poll() is None:
                child.terminate(); child.wait(5)


class LifecycleProcessTests(unittest.TestCase):
    def wait(self, predicate):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if predicate(): return
            time.sleep(.02)
        self.fail('Disposable process did not become ready')

    def test_twenty_close_reopen_and_crash_recovery_cycles(self):
        with tempfile.TemporaryDirectory() as d:
            backend=NativeBackend(Path(d))
            manager=Manager('fixture',folder=Path(d),backend=backend)
            try:
                for cycle in range(20):
                    backend.open_fixture(); manager.tick()
                    self.wait(lambda:manager.report().get('status')=='connected')
                    manager.tick(); self.assertTrue(manager.state['overlay_visible'])
                    old_worker=manager.worker
                    if cycle%2==0:
                        old_worker.terminate(); old_worker.wait(5)
                        manager.tick(); self.assertFalse(manager.state['overlay_visible'])
                        self.assertIsNone(manager.state['thread_id'])
                        # Advance the manager clock past backoff without sleeping the test.
                        manager.clock=lambda:time.monotonic()+16
                        manager.tick()
                        self.wait(lambda:manager.report().get('status')=='connected')
                        manager.clock=time.monotonic
                        manager.tick()
                        self.assertNotEqual(manager.worker.pid,old_worker.pid)
                    backend.host.terminate(); backend.host.wait(5)
                    manager.tick()
                    self.assertEqual(manager.state['status'],'waiting_for_codex')
                    self.assertIsNone(manager.state['thread_id'])
                    self.assertFalse(manager.state['overlay_visible'])
                    # Each fresh host represents an independent normal run.
                    manager.failures.clear(); manager.attempt=0
                self.assertEqual(backend.starts,0)
                self.assertFalse(manager.stop_event.is_set())
                manager.stop_event.set()
                manager.tick()
                self.assertEqual(manager.state['status'],'waiting_for_codex')
            finally:
                manager.detach(); manager.pool.shutdown(wait=True,cancel_futures=True); backend.cleanup()


if __name__=='__main__': unittest.main()
