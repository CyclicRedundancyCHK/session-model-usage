"""Read the running desktop's route notifications and Windows accessibility.

No title lookup, recent-thread heuristic, debug port, input, or clipboard access.
Route files are scoped to the verified desktop PID and process creation time.
Only an unambiguous window appearance is attached; CDP remains available for
versions/windows that cannot provide this read-only observation.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import re
import time

import psutil

from .cdp import Observation
from .platform_win import alive, foreground_info, visible_app_windows

THREAD = re.compile(r'^/local/([a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})(?:[/?#]|$)')
FIELD = re.compile(r'(?:^|\s)(ownerRoutePath|windowId|appearance|webContentsId)=([^\s]+)')
EVENT = re.compile(r'^\S+\s+info\s+(?:'
                   r'\[electron-message-handler\] IAB_LIFECYCLE received browser sidebar owner sync(?:\s|$)|'
                   r'\[window-manager\] window main frame finished load(?:\s|$))')


def log_roots(executable: Path) -> list[Path]:
    local = Path(os.environ.get('LOCALAPPDATA', ''))
    roots = [local / 'Codex/Logs']
    package = executable.parent.parent.name
    if executable.parent.parent.parent.name.lower() == 'windowsapps':
        parts = package.split('_')
        if len(parts) >= 5:
            roots.insert(0, local / 'Packages' / (parts[0] + '_' + parts[-1]) /
                         'LocalCache/Local/Codex/Logs')
    return roots


class Routes:
    """Incremental, bounded reader for explicit window-route notifications."""
    def __init__(self, pid: int, created: float, roots: list[Path]):
        self.pid, self.created, self.roots = pid, created, roots
        self.files = {}
        self.routes = {}
        self.appearances = {}
        self.revision = 0
        self.last_scan = 0.0
        self.caught_up = False

    def consume(self, line: str) -> None:
        # Neither arbitrary thread activity nor resume messages identify a view.
        if not EVENT.match(line):
            return
        route_event = 'IAB_LIFECYCLE received browser sidebar owner sync' in line
        window_event = '[window-manager] window main frame finished load' in line
        if not route_event and not window_event:
            return
        try:
            timestamp = datetime.fromisoformat(line.split(' ', 1)[0].replace('Z', '+00:00')).timestamp()
        except (ValueError, IndexError):
            return
        if timestamp < self.created:
            return
        fields = dict(FIELD.findall(line))
        if not fields.get('windowId', '').isdigit():
            return
        key = int(fields['windowId'])
        if window_event:
            previous = self.appearances.get(key)
            if previous is None or timestamp >= previous[0]:
                self.appearances[key] = (timestamp, fields.get('appearance'))
            return
        route = fields.get('ownerRoutePath')
        if route is None:
            return
        previous = self.routes.get(key)
        if previous is None or timestamp >= previous['time']:
            self.revision += 1
            self.routes[key] = {'route': route, 'time': timestamp, 'revision': self.revision}

    def refresh(self) -> None:
        now = time.monotonic()
        if now - self.last_scan >= 1:
            self.last_scan = now
            # Current process logs only. A PID reused in an older app session
            # cannot supply a route because each notification is timestamped.
            # Log directory dates may follow local time while entries use UTC.
            start_day = datetime.fromtimestamp(self.created, timezone.utc).date() - timedelta(days=1)
            today = datetime.now(timezone.utc).date() + timedelta(days=1)
            for root in self.roots:
                for offset in range((today - start_day).days + 1):
                    day = (start_day + timedelta(days=offset)).strftime('%Y/%m/%d')
                    for path in (root / day).glob(f'codex-desktop-*-{self.pid}-t0-*.log'):
                        self.files.setdefault(path, {'offset': 0, 'pending': b''})
        lines = []
        self.caught_up = bool(self.files)
        for path, state in list(self.files.items()):
            try:
                size = path.stat().st_size
                if size < state['offset']:
                    # Truncation invalidates the evidence, including old views.
                    self.routes.clear(); self.appearances.clear()
                    state.update(offset=0, pending=b'')
                with path.open('rb') as stream:
                    stream.seek(state['offset'])
                    chunk = stream.read(4 * 1024 * 1024)
                    state['offset'] = stream.tell()
                parts = (state['pending'] + chunk).split(b'\n')
                state['pending'] = parts.pop()
                if state['offset'] < size or state['pending']:
                    self.caught_up = False
                if len(state['pending']) > 1024 * 1024:
                    state['pending'] = b''
                # Discard message content before decoding/storing a line.
                lines.extend(part.decode('utf-8', 'replace') for part in parts
                             if b'IAB_LIFECYCLE received browser sidebar owner sync' in part
                             or b'[window-manager] window main frame finished load' in part)
            except FileNotFoundError:
                self.files.pop(path, None)
            except OSError:
                # Do not display stale usage when the source cannot be read.
                self.routes.clear(); self.appearances.clear()
                state.update(offset=0, pending=b'')
                self.caught_up = False
        for line in sorted(lines):
            self.consume(line)
        if not self.files:
            self.caught_up = False
            self.routes.clear(); self.appearances.clear()

    def for_appearance(self, appearance: str) -> dict | None:
        # New desktop versions also load non-conversation shell windows with
        # appearance=primary. Only an explicit owner-route notification makes
        # a log window eligible. Never choose the most recently active route.
        keys = [key for key, value in self.appearances.items()
                if value[1] == appearance and key in self.routes]
        return self.routes.get(keys[0]) if len(keys) == 1 else None


def compose_geometry(nodes: list[dict]) -> tuple[dict, dict] | None:
    """Pair a visible edit with its permission and context controls, in pixels."""
    edits = [n for n in nodes if n['type'] == 50004]
    permissions = [n for n in nodes if n['type'] == 50000 and
                   ('权限' in n['name'] or 'permission' in n['name'].lower())]
    contexts = [n for n in nodes if n['type'] == 50006 and
                ('上下文' in n['name'] or '背景信息' in n['name'] or 'context' in n['name'].lower())]
    candidates = []
    for edit in edits:
        e = edit['rect']
        for permission in permissions:
            p = permission['rect']
            if not (e[0] - 40 <= p[0] <= e[2] and e[3] - 4 <= p[1] <= e[3] + 60):
                continue
            right = [n['rect'] for n in contexts if n['rect'][0] > p[2]
                     and p[1] <= n['rect'][1] < p[3] and n['rect'][2] <= e[2] + 20]
            if len(right) != 1:
                continue
            c = right[0]
            candidates.append(({'left': e[0] - 10, 'top': e[1] - 10, 'right': e[2] + 10,
                                'width': e[2] - e[0] + 20, 'height': p[3] - e[1] + 20},
                               {'left': p[2] + 6, 'top': p[1], 'width': c[0] - p[2] - 12,
                                'height': p[3] - p[1]}))
    return candidates[0] if len(candidates) == 1 else None


class Accessibility:
    def __init__(self):
        import comtypes
        import comtypes.client
        self.comtypes = comtypes
        comtypes.CoInitialize()
        self.module = comtypes.client.GetModule('UIAutomationCore.dll')
        self.client = comtypes.client.CreateObject(self.module.CUIAutomation,
                                                   interface=self.module.IUIAutomation)
        self.condition = self.client.CreateOrConditionFromArray([
            self.client.CreatePropertyCondition(30003, kind) for kind in (50000, 50004, 50006)])
        self.cache = self.client.CreateCacheRequest()
        for property_id in (30003, 30005, 30001, 30022, 30102, 30000):
            self.cache.AddProperty(property_id)
        self.cache.TreeScope = 1
        self.cache.AutomationElementMode = 0

    def read(self, window: dict) -> dict | None:
        root = self.client.ElementFromHandle(window['hwnd'])
        document = root.FindFirst(4, self.client.CreatePropertyCondition(30003, 50030))
        if not document:
            return None
        url = document.GetCurrentPattern(10002).QueryInterface(self.module.IUIAutomationValuePattern).CurrentValue
        # Avatar, browser, review, and auxiliary windows must never be attached.
        if url == 'app://-/index.html':
            appearance = 'primary'
        elif 'initialRoute=%2Fdetached-window' in url:
            appearance = 'detached'
        else:
            return None
        # Read one consistent cache instead of making an RPC for every field.
        elements = document.FindAllBuildCache(4, self.condition, self.cache)
        nodes, selected = [], []
        for i in range(elements.Length):
            node = elements.GetElement(i)
            if node.CachedIsOffscreen:
                continue
            rect = node.CachedBoundingRectangle
            if rect.right <= rect.left or rect.bottom <= rect.top:
                continue
            kind, name = node.CachedControlType, node.CachedName or ''
            if kind == 50000 and 'current=page' in (node.CachedAriaProperties or ''):
                selected.append(tuple(node.GetCachedPropertyValue(30000)))
            # Never request edit values, TextPattern, or message text.
            nodes.append({'type': kind, 'name': name, 'rect': (rect.left, rect.top, rect.right, rect.bottom)})
        geometry = compose_geometry(nodes)
        if not geometry:
            return None
        return {'appearance': appearance, 'composer': geometry[0], 'gap': geometry[1],
                'selection': tuple(sorted(selected))}

    def close(self):
        if self.client is not None:
            self.condition = self.cache = None
            self.client = None
            self.comtypes.CoUninitialize()


class NativeInspector:
    def __init__(self, pid: int, *, reader=None, routes=None):
        app = psutil.Process(pid)
        self.pid, self.created = pid, app.create_time()
        self.routes = routes or Routes(pid, self.created, log_roots(Path(app.exe())))
        self.reader = reader or Accessibility()
        self.problem = '正在读取官方 Codex 窗口与会话路由'
        self.connection_failed = False
        self.selections = {}
        self.themes = {}

    def observe(self) -> list[Observation]:
        if not alive(self.pid, self.created):
            self.connection_failed = True
            return []
        self.routes.refresh()
        if not self.routes.caught_up:
            self.problem = '正在同步当前进程的窗口路由，确认前隐藏旧用量'
            return []
        windows = visible_app_windows({self.pid})
        snapshots = []
        self.problem = '当前窗口未提供可确认的输入栏辅助功能，等待界面加载'
        for window in windows:
            try:
                snapshots.append((window, self.reader.read(window)))
            except Exception as error:
                # Chromium may invalidate cached elements during navigation.
                # A failed snapshot hides this HWND; it must not destroy the
                # incremental log reader or block other verified windows.
                from .diagnostics import record
                record('native_window_snapshot_failed', error)
                snapshots.append((window, None))
                self.problem = '窗口辅助功能暂不可用，正在重试；确认前隐藏用量'
        results = []
        foreground, _ = foreground_info()
        for window, view in snapshots:
            if view is None:
                continue
            # Appearance mapping must be unique on both sides, including
            # occluded windows. Titles are never used to resolve ambiguity.
            if sum(v is not None and v['appearance'] == view['appearance'] for _, v in snapshots) != 1:
                self.problem = '多个同类窗口无法唯一绑定；已隐藏用量以避免串账'
                continue
            route = self.routes.for_appearance(view['appearance'])
            match = THREAD.match(route['route']) if route else None
            if not match:
                self.problem = '当前窗口没有可确认的本地会话路由，等待打开本地会话'
                continue
            hwnd = window['hwnd']
            last = self.selections.get(hwnd)
            fingerprint = view['selection']
            if last and fingerprint != last[0] and route['revision'] <= last[1]:
                self.problem = '会话界面已切换，等待对应路由通知'
                continue
            self.selections[hwnd] = (fingerprint, route['revision'])
            origin, size = window['client_origin'], window['client_size']
            def relative(rect):
                result = dict(rect)
                result['left'] -= origin[0]; result['top'] -= origin[1]
                if 'right' in result: result['right'] -= origin[0]
                return result
            # UIA uses physical pixels, so this viewport has scale 1. Native
            # HWND binding avoids screen-coordinate ambiguity on mixed DPI.
            data = {'threadId': match[1].lower(), 'threadSource': 'desktop_window_route',
                    'nativeHwnd': hwnd, 'visible': True, 'focused': hwnd == foreground,
                    'dark': self.dark(window, view, foreground), 'composer': relative(view['composer']),
                    'toolbarGap': relative(view['gap']), 'viewport': {'width': size[0], 'height': size[1]}}
            results.append(Observation(f'native:{self.pid}:{hwnd}', data))
        return results

    def dark(self, window, view, foreground):
        # A single background pixel, only in the foreground owner, detects the
        # actual app theme. No capture or message pixels are retained.
        hwnd = window['hwnd']
        if hwnd == foreground:
            import ctypes
            from ctypes import wintypes
            user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
            user.GetDC.argtypes = [wintypes.HWND]; user.GetDC.restype = wintypes.HDC
            user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
            gdi.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
            gdi.GetPixel.restype = wintypes.DWORD
            dc = user.GetDC(wintypes.HWND(hwnd))
            try:
                x = view['composer']['left'] - window['client_origin'][0] + 2
                y = view['composer']['top'] - window['client_origin'][1] + 2
                pixel = gdi.GetPixel(dc, int(x), int(y))
                if pixel != 0xffffffff:
                    self.themes[hwnd] = sum((pixel >> shift) & 255 for shift in (0, 8, 16)) < 450
            finally:
                user.ReleaseDC(wintypes.HWND(hwnd), dc)
        return self.themes.get(hwnd, True)

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None
