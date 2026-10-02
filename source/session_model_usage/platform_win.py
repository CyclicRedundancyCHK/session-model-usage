from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import xml.etree.ElementTree as ET

import psutil


def state_directory() -> Path:
    override = os.environ.get("SESSION_USAGE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexSessionUsage"


def read_state(directory: Path | None = None) -> dict:
    from .runtime_io import read_json
    return read_json((directory or state_directory()) / "runtime.json")


def write_state(value: dict, directory: Path | None = None) -> bool:
    from .runtime_io import atomic_json
    return atomic_json((directory or state_directory()) / "runtime.json", value)


def alive(pid: int | None, created: float | None) -> bool:
    try:
        return pid is not None and created is not None and abs(psutil.Process(pid).create_time() - created) < 0.1
    except psutil.Error:
        return False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def hidden_startup() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def running_apps() -> list[psutil.Process]:
    found = []
    for process in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            name = (process.info["name"] or "").lower()
            path = Path(process.info["exe"] or "")
            command = process.info["cmdline"] or []
            if name not in ("chatgpt.exe", "codex.exe") or any(x.startswith("--type=") for x in command):
                continue
            if (path.parent / "resources/app.asar").exists():
                found.append(process)
        except (psutil.Error, OSError):
            continue
    return found


def find_app() -> Path:
    processes = running_apps()
    if processes:
        return Path(processes[0].exe())
    if os.name != "nt":
        raise RuntimeError("悬浮条仅支持 Windows")
    command = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
               "Get-AppxPackage '*Codex*' | Sort-Object Version -Descending | "
               "Select-Object -First 1 -ExpandProperty InstallLocation")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, encoding="utf-8", timeout=15, startupinfo=hidden_startup())
    root = Path(result.stdout.strip())
    for candidate in (root / "app/ChatGPT.exe", root / "app/Codex.exe"):
        if candidate.is_file():
            return candidate
    for root in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Codex",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/app"):
        for name in ("Codex.exe", "ChatGPT.exe"):
            candidate = root / name
            if candidate.is_file() and (root / "resources/app.asar").exists():
                return candidate
    raise RuntimeError("未找到已安装的 Codex 桌面应用")


def start_app(executable: Path, arguments: list[str], environment: dict | None = None) -> psutil.Process:
    """MSIX needs its package identity; direct execution can fail at bootstrap."""
    manifest = executable.parent.parent / "AppxManifest.xml"
    if manifest.is_file():
        tree = ET.parse(manifest).getroot()
        namespace = "{http://schemas.microsoft.com/appx/manifest/foundation/windows10}"
        identity = tree.find(namespace + "Identity")
        relative = executable.relative_to(manifest.parent).as_posix().lower()
        application = next((a for a in tree.findall(f"{namespace}Applications/{namespace}Application")
                            if a.get("Executable", "").replace("\\", "/").lower() == relative), None)
        if identity is None or application is None:
            raise RuntimeError("安装包中未找到 Codex 的应用身份")
        family = identity.attrib["Name"] + "_" + manifest.parent.name.rsplit("_", 1)[-1]
        # Quote PowerShell literals independently; never interpolate as shell code.
        def literal(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"
        command = ("$ErrorActionPreference='Stop'; Invoke-CommandInDesktopPackage "
                   f"-PackageFamilyName {literal(family)} -AppId {literal(application.attrib['Id'])} "
                   f"-Command {literal(str(executable))} -Args {literal(subprocess.list2cmdline(arguments))}")
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                   env=environment, capture_output=True, timeout=15,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        if completed.returncode:
            raise RuntimeError("Windows 未能在安装包身份下启动 Codex")
        deadline = time.monotonic() + 8
        expected = next((x for x in arguments if x.startswith("--remote-debugging-port=")), None)
        while time.monotonic() < deadline:
            for process in running_apps():
                if expected and expected in process.cmdline() and Path(process.exe()) == executable:
                    return process
            time.sleep(0.1)
        raise RuntimeError("已发出启动请求，但没有发现带指定调试端口的 Codex 进程")
    process = subprocess.Popen([str(executable), *arguments], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return psutil.Process(process.pid)


def debugging_port(process: psutil.Process) -> int | None:
    try:
        args = process.cmdline()
        for i, arg in enumerate(args):
            if arg.startswith("--remote-debugging-port="):
                value = arg.split("=", 1)[1]
            elif arg == "--remote-debugging-port" and i + 1 < len(args):
                value = args[i + 1]
            else:
                continue
            port = int(value)
            return port if 1 <= port <= 65535 else None
    except (psutil.Error, ValueError):
        pass
    return None


def verify_listener(port: int, process: psutil.Process) -> None:
    family = {process.pid}
    family.update(p.pid for p in process.children(recursive=True))
    listeners = [c for c in psutil.net_connections(kind="tcp")
                 if c.status == psutil.CONN_LISTEN and c.laddr.port == port]
    if not listeners or any(c.laddr.ip not in ("127.0.0.1", "::1") or c.pid not in family for c in listeners):
        raise RuntimeError("调试端口未确认仅由 Codex 在本机监听")


def foreground_info() -> tuple[int, int]:
    if os.name != "nt":
        return 0, 0
    user = ctypes.windll.user32
    user.GetForegroundWindow.restype = wintypes.HWND
    window = user.GetForegroundWindow()
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(window, ctypes.byref(pid))
    return int(window or 0), pid.value


def client_geometry(hwnd: int) -> tuple[tuple[int, int], tuple[int, int]] | None:
    if os.name != "nt" or not hwnd:
        return None
    user = ctypes.windll.user32
    rect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
    if user.IsIconic(wintypes.HWND(hwnd)) or not user.IsWindowVisible(wintypes.HWND(hwnd)):
        return None
    if not user.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    user.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(origin))
    return (origin.x, origin.y), (rect.right - rect.left, rect.bottom - rect.top)


def visible_app_windows(pids: set[int]) -> list[dict]:
    """Enumerate windows in the verified Codex process family, without focus."""
    if os.name != "nt":
        return []
    user = ctypes.windll.user32
    result = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _) -> bool:
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        if pid.value not in pids:
            return True
        client = client_geometry(int(hwnd))
        rect = window_geometry(int(hwnd))
        if not client or not rect or client[1][0] < 100 or client[1][1] < 100:
            return True
        # A window on another virtual desktop may retain WS_VISIBLE but be
        # cloaked by DWM. Its owned overlay must not appear on this desktop.
        cloaked = wintypes.DWORD()
        try:
            status = ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14,
                ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            if status == 0 and cloaked.value:
                return True
        except AttributeError:
            pass
        result.append({"hwnd": int(hwnd), "pid": pid.value,
                       "client_origin": client[0], "client_size": client[1],
                       "rectangles": [rect, (*client[0], *client[1])],
                       "scale": window_scale(int(hwnd)), "work_origin": monitor_work_origin(int(hwnd))})
        return True

    user.EnumWindows(callback_type(collect), 0)
    return result


def window_geometry(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wintypes.RECT()
    if os.name == "nt" and ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    return None


def monitor_work_origin(hwnd: int) -> tuple[int, int] | None:
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
    if os.name != "nt":
        return None
    user = ctypes.windll.user32
    user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user.MonitorFromWindow.restype = wintypes.HANDLE
    info = MonitorInfo(); info.size = ctypes.sizeof(info)
    monitor = user.MonitorFromWindow(wintypes.HWND(hwnd), 2)
    if user.GetMonitorInfoW(wintypes.HANDLE(monitor), ctypes.byref(info)):
        return info.work.left, info.work.top
    return None


def set_no_activate(hwnd: int) -> None:
    if os.name != "nt":
        return
    user = ctypes.windll.user32
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    style = user.GetWindowLongW(wintypes.HWND(hwnd), -20)
    user.SetWindowLongW(wintypes.HWND(hwnd), -20, style | 0x08000000 | 0x00000080)


def attach_to_window(hwnd: int, owner: int) -> None:
    """Make our top-level tool an owned window, preserving the host's z-order."""
    if os.name != "nt":
        return
    user = ctypes.windll.user32
    get_owner = user.GetWindow
    get_owner.argtypes = [wintypes.HWND, wintypes.UINT]
    get_owner.restype = wintypes.HWND
    if int(get_owner(wintypes.HWND(hwnd), 4) or 0) == owner:
        return
    set_long = user.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.SetWindowLongW
    set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_long.restype = ctypes.c_ssize_t
    ctypes.windll.kernel32.SetLastError(0)
    if not set_long(wintypes.HWND(hwnd), -8, owner):
        error = ctypes.windll.kernel32.GetLastError()
        if error:
            raise OSError(error, "无法将悬浮条关联到 Codex 窗口")


def position_without_focus(hwnd: int, x: int, y: int, width: int, height: int,
                           owner: int = 0) -> None:
    if os.name == "nt":
        user = ctypes.windll.user32
        flags = 0x0010 | 0x0040 | 0x0200  # NOACTIVATE | SHOWWINDOW | NOOWNERZORDER
        if owner:
            attach_to_window(hwnd, owner)
            host_topmost = bool(user.GetWindowLongW(wintypes.HWND(owner), -20) & 8)
            if not host_topmost and user.GetWindowLongW(wintypes.HWND(hwnd), -20) & 8:
                user.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(-2), 0, 0, 0, 0,
                                  0x0010 | 0x0200 | 0x0001 | 0x0002)
            get_window = user.GetWindow
            get_window.argtypes = [wintypes.HWND, wintypes.UINT]
            get_window.restype = wintypes.HWND
            # Insert immediately above the host, skipping our other owned
            # surfaces. Inserting *after* the host can put a cross-process
            # popup behind Electron despite its owner relationship.
            insert_after = int(get_window(wintypes.HWND(owner), 3) or 0)
            while insert_after:
                pid = wintypes.DWORD()
                user.GetWindowThreadProcessId(wintypes.HWND(insert_after), ctypes.byref(pid))
                related = int(get_window(wintypes.HWND(insert_after), 4) or 0) == owner
                visible = bool(user.IsWindowVisible(wintypes.HWND(insert_after)))
                if visible and insert_after != hwnd and not (related and pid.value == os.getpid()):
                    break
                insert_after = int(get_window(wintypes.HWND(insert_after), 3) or 0)
            # Inserting after a topmost HWND promotes this popup into that band.
            # HWND_TOP instead selects the top of the normal band when our host
            # is its first visible member, retaining normal application layering.
            if insert_after and not host_topmost and user.GetWindowLongW(wintypes.HWND(insert_after), -20) & 8:
                insert_after = 0
        else:
            insert_after, flags = 0, flags | 0x0004  # Retain z-order in previews.
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.SetWindowPos.restype = wintypes.BOOL
        if not user.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(insert_after),
                                 x, y, width, height, flags):
            raise OSError(ctypes.windll.kernel32.GetLastError(), "无法定位悬浮条")


def window_scale(hwnd: int) -> float:
    if os.name == "nt":
        try:
            return ctypes.windll.user32.GetDpiForWindow(wintypes.HWND(hwnd)) / 96.0 or 1.0
        except (OSError, AttributeError):
            pass
    return 1.0
