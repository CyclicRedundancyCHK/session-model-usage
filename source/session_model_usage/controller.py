from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import psutil

from .cdp import Inspector
from .platform_win import (alive, debugging_port, find_app, free_port, hidden_startup,
                           read_state, running_apps, start_app, state_directory, verify_listener, write_state)


class RestartRequired(RuntimeError):
    pass


def own_command(mode: str, *arguments: str) -> list[str]:
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).parent
        executable = root / "CodexSessionUsage.exe" if mode in ("overlay", "recovery") else root / "session-usage.exe"
        return [str(executable), mode, *arguments]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py"), mode, *arguments]


def status() -> dict:
    state = read_state()
    running = alive(state.get("overlay_pid"), state.get("overlay_created"))
    return {**state, "running": running,
            **({"message": "悬浮条未运行，请打开“Codex 会话用量”启动器",
                "overlay_visible": False, "badge_rect": None} if not running else {}),
            "thread_id": state.get("thread_id") if running else None,
            "status": state.get("status", "stopped") if running else "stopped"}


def stop() -> dict:
    state = status()
    if state["running"]:
        folder = state_directory()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "stop.request").write_text(state.get("run_id", ""), encoding="utf-8")
        return {"status": "stop_requested", "message": "已请求关闭悬浮条，Codex 将继续运行"}
    return {"status": "stopped", "message": "悬浮条未运行"}


def recovery_ready(apps: list[psutil.Process]) -> bool:
    """Wait for a manual exit, or attach to one properly launched instance."""
    return not apps or (len(apps) == 1 and debugging_port(apps[0]) is not None)


def launch(*, recovery_run_id: str | None = None) -> dict:
    if os.name != "nt":
        raise RuntimeError("悬浮条仅支持 Windows；query 命令可独立使用")
    existing = status()
    def occupied(value: dict) -> bool:
        resuming = (recovery_run_id is not None and value.get("run_id") == recovery_run_id
                    and value.get("mode") == "recovery" and value.get("overlay_pid") == os.getpid())
        return value["running"] and not resuming

    if occupied(existing):
        return existing
    # A concurrent launcher must not start two observers or two Codex instances.
    folder = state_directory()
    folder.mkdir(parents=True, exist_ok=True)
    import msvcrt
    lock = (folder / "launch.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0"); lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        return {"status": "launching", "message": "另一个启动器正在连接"}
    try:
        existing = status()
        if occupied(existing):
            return existing
        apps = running_apps()
        started = False
        if apps:
            if len(apps) != 1:
                raise RestartRequired("检测到多个 Codex 实例。请退出这些实例，再使用本启动器打开 Codex。")
            app = apps[0]
            port = debugging_port(app)
            if port is None:
                if recovery_run_id is not None:
                    return {**existing, "status": "waiting_for_restart"}
                run_id = uuid.uuid4().hex
                state = {"schema_version": 1, "run_id": run_id, "mode": "recovery",
                         "app_pid": app.pid, "app_created": app.create_time(), "port": None,
                         "status": "waiting_for_restart", "thread_id": None,
                         "message": "Codex 未启用本机调试连接；请手动退出，启动器将自动打开新版并恢复悬浮条",
                         "started_app": False, "overlay_visible": False, "badge_rect": None}
                write_state(state)
                recovery = subprocess.Popen(own_command("recovery", "--run-id", run_id),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=hidden_startup())
                state.update(overlay_pid=recovery.pid,
                             overlay_created=psutil.Process(recovery.pid).create_time())
                write_state(state)
                return {**state, "running": True}
        else:
            executable = find_app()
            port = free_port()
            arguments = [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"]
            # Keep the installed runtime's existing profile. Never create a new login profile.
            if (executable.parent / "resources/owl-app.ini").exists():
                profile = Path(os.environ.get("APPDATA", "")) / "Codex/web/Codex"
                if profile.is_dir():
                    arguments.append(f"--user-data-dir={profile}")
            app = start_app(executable, arguments)
            started = True
        inspector = Inspector(port)
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    inspector.get_json("/json/version")
                    verify_listener(port, app)
                    break
                except Exception as error:
                    if time.monotonic() >= deadline or not app.is_running():
                        raise RuntimeError("未能建立仅本机的 Codex 调试连接。请确认版本兼容；无需修改客户端文件。") from error
                    time.sleep(0.25)
        finally:
            inspector.close()
        run_id = uuid.uuid4().hex
        state = {"schema_version": 1, "run_id": run_id, "mode": "overlay", "app_pid": app.pid,
                 "app_created": app.create_time(), "port": port, "status": "starting",
                 "thread_id": None, "message": "正在识别当前会话", "started_app": started}
        write_state(state)
        overlay = subprocess.Popen(own_command("overlay", "--port", str(port), "--app-pid", str(app.pid),
                                               "--run-id", run_id), stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, startupinfo=hidden_startup())
        state.update(overlay_pid=overlay.pid, overlay_created=psutil.Process(overlay.pid).create_time())
        write_state(state)
        return {**state, "running": True}
    finally:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()
