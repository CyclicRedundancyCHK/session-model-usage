from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .platform_win import alive, hidden_startup, read_state, state_directory
from .runtime_io import ProcessLock, atomic_json


class RestartRequired(RuntimeError):
    pass


def own_command(mode: str, *arguments: str) -> list[str]:
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).parent
        executable = root / ("CodexSessionUsage.exe" if mode in ("overlay", "recovery", "supervise") else "session-usage.exe")
        return [str(executable), mode, *arguments]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py"), mode, *arguments]


def status() -> dict:
    from . import __version__
    state = read_state()
    managed = state.get("schema_version") == 2
    running = alive(state.get("supervisor_pid") if managed else state.get("overlay_pid"),
                    state.get("supervisor_created") if managed else state.get("overlay_created"))
    heartbeat = state.get("heartbeat", 0)
    healthy = running and isinstance(heartbeat, (int, float)) and 0 <= time.time() - heartbeat < 10
    if not running:
        state.update(status="stopped", message="悬浮条未运行，请打开“Codex 会话用量”启动器",
                     thread_id=None, overlay_visible=False, badge_rect=None)
    elif not healthy:
        state.update(status="unresponsive", message="管理进程心跳已过期，请查看 diagnose",
                     thread_id=None, overlay_visible=False, badge_rect=None)
    from .runtime_io import read_json
    frontend = read_json(state_directory() / 'frontend.json')
    if frontend.get('run_id') == state.get('run_id'):
        heartbeat = frontend.get('heartbeat', 0)
        frontend['healthy'] = (alive(frontend.get('pid'), frontend.get('created'))
                               and isinstance(heartbeat, (int, float)) and 0 <= time.time() - heartbeat < 10)
        state['frontend'] = frontend
    return {**state, "version": state.get("version", __version__), "running": running, "healthy": healthy}


def request(action: str, run_id: str) -> bool:
    if action not in ("stop", "retry", "cancel_retry"):
        raise ValueError("Unknown manager action")
    return atomic_json(state_directory() / "commands" / (uuid.uuid4().hex + ".json"),
                       {"action": action, "run_id": run_id})


def stop() -> dict:
    state = status()
    if not state["running"]:
        return {"status": "stopped", "message": "悬浮条未运行"}
    if state.get("schema_version") == 2:
        sent = request("stop", state["run_id"])
    else:
        try:
            (state_directory() / "stop.request").write_text(state.get("run_id", ""), encoding="utf-8")
            sent = True
        except OSError:
            sent = False
    return {"status": "stop_requested" if sent else "error", "message": "已请求退出插件，Codex 将继续运行" if sent else "无法写入退出请求，请从托盘退出"}


def recovery_ready(apps) -> bool:
    from .platform_win import debugging_port
    return not apps or (len(apps) == 1 and debugging_port(apps[0]) is not None)


def launch(*, recovery_run_id=None) -> dict:
    if os.name != "nt":
        raise RuntimeError("悬浮条仅支持 Windows；query 命令可独立使用")
    state = status()
    if state["running"]:
        if state.get("schema_version") == 2:
            request("retry", state["run_id"])
        return state
    lock = ProcessLock(state_directory() / "launch.lock")
    if not lock.acquire():
        return {"status": "launching", "message": "另一个启动器正在连接"}
    try:
        state = status()
        if state["running"]:
            request("retry", state["run_id"])
            return state
        run_id = uuid.uuid4().hex
        process = subprocess.Popen(own_command("supervise", "--run-id", run_id),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=hidden_startup())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = status()
            if state["running"] and state.get("schema_version") == 2:
                return state
            if process.poll() is not None:
                raise RuntimeError("管理进程启动失败，请运行 diagnose 查看本地诊断")
            time.sleep(0.05)
        return {"status": "launching", "message": "管理进程正在启动，请稍后运行 status"}
    finally:
        lock.close()
