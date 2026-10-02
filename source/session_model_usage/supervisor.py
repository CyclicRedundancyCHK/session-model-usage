"""Persistent tray manager and independently recoverable observer process."""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

import psutil

from . import __version__
from .cdp import Inspector
from .controller import own_command
from .diagnostics import record
from .platform_win import (alive, debugging_port, find_app, free_port, hidden_startup,
                           running_apps, start_app, state_directory, verify_listener, write_state)
from .runtime_io import ProcessLock, read_json

BACKOFF = (1, 2, 4, 8, 15)
REPORT_FIELDS = ("status", "message", "thread_id", "overlay_visible", "badge_rect", "host_hwnd", "foreground_pid")


class Backend:
    apps = staticmethod(running_apps)

    def start(self):
        executable = find_app()
        port = free_port()
        arguments = [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"]
        if (executable.parent / "resources/owl-app.ini").exists():
            profile = Path(os.environ.get("APPDATA", "")) / "Codex/web/Codex"
            if profile.is_dir():
                arguments.append(f"--user-data-dir={profile}")
        return start_app(executable, arguments)

    def probe(self, app, port):
        inspector = Inspector(port)
        try:
            inspector.get_json("/json/version")
            verify_listener(port, app)
        finally:
            inspector.close()

    def spawn(self, app, port, run_id, attachment_id):
        process = subprocess.Popen(own_command("overlay", "--port", str(port), "--app-pid", str(app.pid),
            "--run-id", run_id, "--attachment-id", attachment_id),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=hidden_startup())
        created = psutil.Process(process.pid).create_time()
        return process, created

    def stop_worker(self, process, created):
        if process.poll() is None and alive(process.pid, created):
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if alive(process.pid, created):
                    process.kill()
                    process.wait(timeout=3)


class Manager:
    def __init__(self, run_id: str, *, open_requested=False, folder=None, backend=None, clock=time.monotonic):
        self.run_id = run_id
        self.folder = folder or state_directory()
        self.backend, self.clock = backend or Backend(), clock
        self.stop_event = threading.Event()
        self.open_requested = open_requested
        self.restart_armed = False
        self.host = None
        self.worker = None
        self.worker_created = None
        self.attachment_id = None
        self.worker_started = 0.0
        self.next_attempt = 0.0
        self.attempt = 0
        self.failures = deque()
        self.paused = False
        self.start_future = None
        self.start_deadline = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="codex-start")
        self.state = {"schema_version": 2, "version": __version__, "run_id": run_id,
            "mode": "supervisor", "supervisor_pid": os.getpid(),
            "supervisor_created": psutil.Process().create_time(), "recovery_count": 0,
            "last_error": None, "status": "waiting_for_codex", "message": "Codex 未运行，托盘待机",
            "thread_id": None, "overlay_visible": False, "badge_rect": None,
            "overlay_pid": os.getpid(), "overlay_created": psutil.Process().create_time(),
            "worker_pid": None, "worker_created": None, "app_pid": None, "port": None}

    def clear_view(self):
        self.state.update(thread_id=None, overlay_visible=False, badge_rect=None, host_hwnd=None)

    def publish(self):
        self.state["heartbeat"] = time.time()
        self.state["worker_heartbeat"] = self.report().get("heartbeat") if self.worker else None
        write_state(self.state, self.folder)

    def report(self):
        if not self.attachment_id:
            return {}
        value = read_json(self.folder / f"observer-{self.attachment_id}.json")
        if value.get("attachment_id") != self.attachment_id or value.get("run_id") != self.run_id:
            return {}
        return value

    def detach(self):
        if self.worker is not None:
            try:
                self.backend.stop_worker(self.worker, self.worker_created)
            except Exception as error:
                record("worker_cleanup_failed", error, folder=self.folder)
            self.worker = None
        self.clear_view()
        # Legacy cached launchers used overlay_pid as the lifetime indicator.
        self.state.update(overlay_pid=self.state['supervisor_pid'], overlay_created=self.state['supervisor_created'],
                          worker_pid=None, worker_created=None, worker_heartbeat=None)
        if self.attachment_id:
            try:
                (self.folder / f"observer-{self.attachment_id}.json").unlink(missing_ok=True)
            except OSError:
                pass
        self.attachment_id = None

    def failure(self, reason, error=None):
        now = self.clock()
        self.detach()
        self.state.update(last_error={"reason": reason, "error_type": type(error).__name__ if error else None,
                                      "time": time.time()}, status="reconnecting", message="连接暂不可用，正在自动恢复")
        self.state["recovery_count"] += 1
        record(reason, error, folder=self.folder)
        self.failures.append(now)
        while self.failures and now - self.failures[0] > 60:
            self.failures.popleft()
        self.next_attempt = now + BACKOFF[min(self.attempt, len(BACKOFF) - 1)]
        self.attempt += 1
        if len(self.failures) >= 5:
            self.paused = True
            self.state.update(status="recovery_paused", message="一分钟内恢复失败五次，已暂停；请从托盘点击恢复连接")

    def retry(self):
        self.paused = False
        self.failures.clear()
        self.attempt = 0
        self.next_attempt = 0
        self.start_deadline = None
        self.restart_armed = True
        self.open_requested = True
        record("manual_retry", folder=self.folder)

    def commands(self):
        legacy = self.folder / 'stop.request'
        try:
            if legacy.read_text(encoding='utf-8') == self.run_id:
                self.stop_event.set()
        except OSError:
            pass
        for path in sorted((self.folder / "commands").glob("*.json")):
            if path.is_symlink():
                continue
            value = read_json(path)
            if not value:
                continue
            if value.get("run_id") == self.run_id:
                if value.get("action") == "stop":
                    self.stop_event.set()
                elif value.get("action") == "retry":
                    self.retry()
            try:
                path.unlink()
            except OSError:
                pass

    def tick(self):
        self.commands()
        if self.stop_event.is_set():
            return
        now = self.clock()
        if self.start_future is not None:
            if not self.start_future.done():
                self.state.update(status="starting", message="正在启动 Codex，等待本机连接")
                return
            try:
                self.start_future.result()
            except Exception as error:
                self.start_deadline = None
                self.failure("codex_start_failed", error)
                self.open_requested = not self.paused
            self.start_future = None
        apps = self.backend.apps()
        if not apps:
            self.detach()
            self.host = None
            self.state.update(app_pid=None, app_created=None, port=None)
            if not self.paused and (self.open_requested or self.restart_armed) and now >= self.next_attempt:
                self.open_requested = self.restart_armed = False
                self.start_deadline = now + 90
                self.start_future = self.pool.submit(self.backend.start)
                self.state.update(status="starting", message="正在启动 Codex，等待本机连接")
                record("codex_start_requested", folder=self.folder)
            elif self.start_deadline is not None and now < self.start_deadline:
                self.state.update(status="starting", message="正在等待 Codex 启动")
            elif not self.paused:
                self.start_deadline = None
                self.state.update(status="waiting_for_codex", message="Codex 已关闭，托盘待机；重新打开后自动连接")
            return
        if len(apps) != 1:
            self.detach()
            self.state.update(status="ambiguous_host", message="检测到多个 Codex 主进程，等待唯一实例；不会猜测会话")
            return
        app = apps[0]
        identity = (app.pid, app.create_time())
        if self.host != identity:
            self.detach()
            self.host = identity
            self.attempt = 0
            self.next_attempt = 0
            self.start_deadline = now + 90
            self.state.update(app_pid=identity[0], app_created=identity[1])
        port = debugging_port(app)
        if port != self.state.get('port') and self.worker is not None:
            self.detach()
            self.next_attempt = 0
        self.state["port"] = port
        if port is None:
            self.detach()
            self.restart_armed = self.restart_armed or self.open_requested
            self.open_requested = False
            self.state.update(status="waiting_for_restart", message="Codex 未启用本机调试；点击托盘恢复连接，再手动退出 Codex 一次")
            return
        self.open_requested = self.restart_armed = False
        if self.paused:
            self.clear_view()
            self.state.update(status="recovery_paused")
            return
        if self.worker is not None:
            report = self.report()
            if self.worker.poll() is not None:
                self.failure("worker_exited")
                return
            heartbeat = report.get("heartbeat_monotonic", self.worker_started)
            if not isinstance(heartbeat, (int, float)) or now - heartbeat > 15:
                self.failure("worker_heartbeat_expired")
                return
            if report:
                self.state.update({key: report[key] for key in REPORT_FIELDS if key in report})
                if report.get("status") == "stopped":
                    self.failure("worker_stopped")
            return
        if now < self.next_attempt:
            return
        try:
            self.backend.probe(app, port)
        except Exception as error:
            self.clear_view()
            self.state.update(status="connecting", message="正在等待 Codex 本机调试连接；将继续重试")
            self.next_attempt = now + BACKOFF[min(self.attempt, len(BACKOFF) - 1)]
            self.attempt += 1
            self.state["last_error"] = {"reason": "connection_unavailable", "error_type": type(error).__name__, "time": time.time()}
            if self.start_deadline is not None and now >= self.start_deadline:
                self.state.update(status="connection_error", message="启动等待超过 90 秒，托盘仍会重试；可点击恢复连接")
            return
        self.start_deadline = None
        self.attachment_id = uuid.uuid4().hex
        try:
            self.worker, self.worker_created = self.backend.spawn(app, port, self.run_id, self.attachment_id)
            self.worker_started = now
            self.state.update(overlay_pid=self.worker.pid, overlay_created=self.worker_created,
                              worker_pid=self.worker.pid, worker_created=self.worker_created,
                              status="starting", message="正在识别当前会话")
            record("worker_started", folder=self.folder)
        except Exception as error:
            self.failure("worker_start_failed", error)

    def run(self, changed=lambda state: None):
        previous = None
        self.publish()
        try:
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception as error:
                    self.clear_view()
                    self.state.update(status="connection_error", message="恢复过程遇到临时错误，托盘将继续重试")
                    record("manager_tick_failed", error, folder=self.folder)
                self.publish()
                marker = (self.state["status"], self.state["message"])
                if marker != previous:
                    changed(dict(self.state))
                    previous = marker
                self.stop_event.wait(0.5)
        finally:
            self.detach()
            self.state.update(status="stopped", message="插件已退出", thread_id=None)
            self.publish()
            self.pool.shutdown(wait=False, cancel_futures=True)
            record("manager_stopped", folder=self.folder)


def run_supervisor(run_id: str, open_requested=False) -> int:
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    from .overlay import icon
    from .controller import request
    lock = ProcessLock(state_directory() / "supervisor.lock")
    if not lock.acquire():
        return 0
    class Worker(QThread):
        changed = Signal(object)
        def run(self):
            manager.run(self.changed.emit)
    try:
        app = QApplication.instance() or QApplication([])
        app.setQuitOnLastWindowClosed(False)
        manager = Manager(run_id, open_requested=open_requested)
        tray = QSystemTrayIcon(icon(), app)
        menu = QMenu()
        state_action = menu.addAction("正在启动…")
        state_action.setEnabled(False)
        menu.addAction("恢复连接").triggered.connect(lambda: request("retry", run_id))
        menu.addAction("退出插件").triggered.connect(app.quit)
        tray.setContextMenu(menu)
        tray.show()
        worker = Worker()
        def update(value):
            state_action.setText(value["message"])
            tray.setToolTip(value["message"])
        worker.changed.connect(update)
        worker.finished.connect(app.quit)
        def close():
            manager.stop_event.set()
            worker.wait(8000)
            tray.hide()
        app.aboutToQuit.connect(close)
        worker.start()
        return app.exec()
    finally:
        lock.close()
