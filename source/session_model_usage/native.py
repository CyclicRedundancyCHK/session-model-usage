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
from urllib.parse import urlsplit, parse_qs

import psutil

from .cdp import Observation
from .identity import ClientBindings
from .platform_win import alive, foreground_info, visible_app_windows

THREAD = re.compile(r'^/local/([a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})(?:[/?#]|$)')
DRAFT = re.compile(r'^/local/(client-new-thread:[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})(?:[/?#]|$)')
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
        keys = self.candidates(appearance)
        return self.routes.get(keys[0]) if len(keys) == 1 else None

    def candidates(self, appearance: str) -> list[int]:
        return [key for key, value in self.appearances.items()
                if value[1] == appearance and key in self.routes]


def compose_geometry(nodes: list[dict]) -> tuple[dict, dict] | None:
    """Pair a visible edit with its permission and context controls, in pixels."""
    edits = [n for n in nodes if n['type'] == 50004]
    permissions = [n for n in nodes if n['type'] in (50000, 50031) and
                   ('权限' in n['name'] or 'permission' in n['name'].lower())]
    contexts = [n for n in nodes if n['type'] in (50000, 50006) and
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
            left_edge = p[2]
            if not right:
                # Newer layouts omit the context ring. Use the measured space
                # between the permission group and right toolbar buttons. No
                # model-name/version allowlist and no guessed empty coordinates.
                controls = [n['rect'] for n in nodes if n['type'] in (50000, 50031)
                            and n['rect'][0] >= p[2] and n['rect'][2] <= e[2] + 20
                            and p[1] <= n['rect'][1] < p[3]]
                buttons = [rect for rect in controls if rect[0] > (e[0] + e[2]) / 2]
                if buttons:
                    boundary = min(rect[0] for rect in buttons)
                    left_edge = max([p[2]] + [rect[2] for rect in controls if rect[2] <= boundary
                                            and rect[0] < (e[0] + e[2]) / 2])
                    right = [min(buttons, key=lambda rect: rect[0])]
            if len(right) != 1:
                continue
            c = right[0]
            candidates.append(({'left': e[0] - 10, 'top': e[1] - 10, 'right': e[2] + 10,
                                'width': e[2] - e[0] + 20, 'height': p[3] - e[1] + 20},
                               {'left': left_edge + 6, 'top': p[1], 'width': c[0] - left_edge - 12,
                                'height': p[3] - p[1]}))
    return candidates[0] if len(candidates) == 1 else None


def document_appearance(url: str) -> str | None:
    """Accept the desktop document independently of version/query parameters."""
    try:
        parsed = urlsplit(url)
    except (ValueError, TypeError):
        return None
    if parsed.scheme != 'app' or parsed.netloc != '-' or parsed.path != '/index.html':
        return None
    routes = parse_qs(parsed.query).get('initialRoute', [])
    if not routes:
        return 'primary'
    if len(routes) != 1:
        return None
    route = routes[0]
    if re.match(r'^/detached-window(?:[/?#]|$)', route):
        return 'detached'
    if route == '/' or THREAD.match(route) or DRAFT.match(route):
        return 'primary'
    return None


def automation_client(module, factory):
    # The original CUIAutomation has a 20-second transaction timeout, longer
    # than the manager's 15-second worker deadline. CUIAutomation8 exposes
    # bounded calls so invalidating Chromium elements can be retried locally.
    try:
        client = factory(module.CUIAutomation8, interface=module.IUIAutomation2)
        client.ConnectionTimeout = 500
        client.TransactionTimeout = 1500
        return client
    except Exception as error:
        from .diagnostics import record
        record('native_uia_timeouts_unavailable', error)
        return factory(module.CUIAutomation, interface=module.IUIAutomation)


def sidebar_selection(nodes, selected, composer_left):
    # Global navigation icons and right-side tabs also use aria-current=page.
    # Only a visible, full-width row to the left of this composer describes a
    # conversation selection. Do not retain or compare conversation titles.
    def row(rect):
        return rect[2] - rect[0] >= 80 and rect[2] <= composer_left
    required = any(n['type'] == 50000 and row(n['rect']) for n in nodes)
    return tuple(sorted(runtime for runtime, rect in selected if row(rect))), required


class Accessibility:
    def __init__(self):
        import comtypes
        import comtypes.client
        self.comtypes = comtypes
        comtypes.CoInitialize()
        self.module = comtypes.client.GetModule('UIAutomationCore.dll')
        self.client = automation_client(self.module, comtypes.client.CreateObject)
        self.condition = self.client.CreateOrConditionFromArray([
            self.client.CreatePropertyCondition(30003, kind) for kind in (50000, 50004, 50006, 50031)])
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
        appearance = document_appearance(url)
        if appearance is None:
            return None
        # Read one consistent cache instead of making an RPC for every field.
        elements = document.FindAllBuildCache(4, self.condition, self.cache)
        nodes, selected = [], []
        for i in range(elements.Length):
            node = elements.GetElement(i)
            rect = node.CachedBoundingRectangle
            if rect.right <= rect.left or rect.bottom <= rect.top:
                continue
            kind = node.CachedControlType
            if kind == 50000 and 'current=page' in (node.CachedAriaProperties or ''):
                # A selected sidebar row can scroll out of view while its
                # conversation stays open. Its cached selection is still
                # evidence; offscreen controls cannot supply composer geometry.
                selected.append((tuple(node.GetCachedPropertyValue(30000)),
                                 (rect.left, rect.top, rect.right, rect.bottom)))
            if node.CachedIsOffscreen:
                continue
            # Never request edit values, TextPattern, or message text.
            nodes.append({'type': kind, 'name': node.CachedName or '',
                          'rect': (rect.left, rect.top, rect.right, rect.bottom)})
        geometry = compose_geometry(nodes)
        if not geometry:
            return None
        selection, required = sidebar_selection(nodes, selected, geometry[0]['left'] + 10)
        return {'appearance': appearance, 'composer': geometry[0], 'gap': geometry[1],
                'selection': selection, 'selection_required': required,
                'document_key': tuple(document.GetRuntimeId()),
                'initial_route': parse_qs(urlsplit(url).query).get('initialRoute', [None])[0]}

    def close(self):
        if self.client is not None:
            self.condition = self.cache = None
            self.client = None
            self.comtypes.CoUninitialize()


class NativeInspector:
    def __init__(self, pid: int, *, reader=None, routes=None, bindings=None):
        app = psutil.Process(pid)
        self.pid, self.created = pid, app.create_time()
        self.routes = routes or Routes(pid, self.created, log_roots(Path(app.exe())))
        self.reader = reader or Accessibility()
        self.problem = '正在读取官方 Codex 窗口与会话路由'
        self.problem_code = 'connecting'
        package = Path(app.exe()).parent.parent.name
        build = re.search(r'_(\d+(?:\.\d+){3})_', package)
        self.codex_version = build[1] if build else None
        self.window_count = 0
        self.connection_failed = False
        self.selections = {}
        self.themes = {}
        self.window_routes = {}
        self.client_bindings = bindings if bindings is not None else ClientBindings()

    def compatibility(self):
        return {'codex_version': self.codex_version, 'observer': 'windows_accessibility',
                'reason': self.problem_code, 'route_files': len(self.routes.files),
                'routes_caught_up': self.routes.caught_up, 'visible_windows': self.window_count}

    def bind_routes(self, snapshots):
        """Keep verified HWND/log-window identities across Micro window opens.

        A current explicit log route can match a document's initial route.
        Otherwise both sides must be unique after already verified pairs are
        removed. Never choose by title, foreground state, or route recency.
        """
        bindings = {}
        for appearance in {view['appearance'] for _, view in snapshots if view is not None}:
            views = [(window, view) for window, view in snapshots
                     if view is not None and view['appearance'] == appearance]
            keys = self.routes.candidates(appearance)
            for window, view in views:
                cached = self.window_routes.get(window['hwnd'])
                if (cached and cached[0] in keys and cached[1] == view.get('document_key') and
                        cached[2] == self.routes.appearances[cached[0]]):
                    bindings[window['hwnd']] = cached[0]
            # If evidence collides, discard all claimants rather than selecting
            # whichever HWND happened to be enumerated first.
            duplicates = {key for key in bindings.values() if list(bindings.values()).count(key) > 1}
            bindings = {hwnd: key for hwnd, key in bindings.items() if key not in duplicates}
            for window, view in views:
                initial = view.get('initial_route')
                matches = [key for key in keys if self.routes.routes[key]['route'] == initial]
                claimants = [v for _, v in views if v.get('initial_route') == initial]
                if (window['hwnd'] not in bindings and initial and len(matches) == 1 and
                        len(claimants) == 1 and matches[0] not in bindings.values()):
                    bindings[window['hwnd']] = matches[0]
            remaining_views = [(window, view) for window, view in views if window['hwnd'] not in bindings]
            remaining_keys = [key for key in keys if key not in bindings.values()]
            if len(remaining_views) == len(remaining_keys) == 1:
                bindings[remaining_views[0][0]['hwnd']] = remaining_keys[0]
            for window, view in views:
                key = bindings.get(window['hwnd'])
                if key is not None:
                    identity = (key, view.get('document_key'), self.routes.appearances[key])
                    if self.window_routes.get(window['hwnd']) != identity:
                        self.selections.pop(window['hwnd'], None)
                        self.themes.pop(window['hwnd'], None)
                    self.window_routes[window['hwnd']] = identity
        return bindings

    def observe(self) -> list[Observation]:
        if not alive(self.pid, self.created):
            self.connection_failed = True
            self.problem_code = 'host_exited'
            return []
        self.routes.refresh()
        if not self.routes.caught_up:
            self.problem = '正在同步当前进程的窗口路由，确认前隐藏旧用量'
            self.problem_code = 'route_sync_pending' if self.routes.files else 'route_logs_missing'
            return []
        windows = visible_app_windows({self.pid})
        self.window_count = len(windows)
        snapshots = []
        self.problem = '当前窗口未提供可确认的输入栏辅助功能，等待界面加载'
        self.problem_code = 'composer_layout_unavailable'
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
                self.problem_code = 'accessibility_retry'
        results = []
        foreground, _ = foreground_info()
        bindings = self.bind_routes(snapshots)
        for window, view in snapshots:
            if view is None:
                continue
            origin, size = window['client_origin'], window['client_size']
            gap = view['gap']
            if not (origin[0] <= gap['left'] and origin[1] <= gap['top'] and
                    gap['left'] + gap['width'] <= origin[0] + size[0] and
                    gap['top'] + gap['height'] <= origin[1] + size[1]):
                # UIA and HWND snapshots can straddle a window move/resize.
                # Wait for consistent coordinates instead of attaching outside.
                self.problem = '输入栏位置正在变化，等待窗口与控件坐标同步'
                self.problem_code = 'composer_layout_unavailable'
                continue
            key = bindings.get(window['hwnd'])
            if key is None:
                self.problem = '多个同类窗口无法唯一绑定；已隐藏用量以避免串账'
                self.problem_code = 'ambiguous_windows'
                continue
            route = self.routes.routes.get(key)
            match = THREAD.match(route['route']) if route else None
            draft = DRAFT.match(route['route']) if route else None
            thread_id = match[1].lower() if match else self.client_bindings.resolve(draft[1]) if draft else None
            hwnd = window['hwnd']
            last = self.selections.get(hwnd)
            fingerprint = view['selection']
            # Unsaved local drafts have no selected conversation sidebar row.
            # Their client UUID is never a thread ID. Codex can retain this
            # route after saving; resolve only an explicit persisted binding.
            required = view.get('selection_required', True) and not draft
            if not match and not draft:
                self.problem = '当前窗口没有可确认的本地会话路由，等待打开本地会话'
                self.problem_code = 'local_route_unavailable'
                # Keep the home/remote selection as a navigation baseline too.
                # Otherwise a route arriving before the sidebar can bind the
                # old render and then mistake the new render for another switch.
                if route:
                    self.selections[hwnd] = (fingerprint, route['revision'], route['route'], required)
                continue
            sidebar_pending = required and not fingerprint
            # Projects and Recents can both contain the selected conversation.
            # Scrolling may expose only one copy. A still-selected runtime ID
            # anchors the same view; changing which copies are visible is not
            # a navigation that needs another owner-route notification.
            same_selection = (last and (fingerprint == last[0] or
                                        bool(set(fingerprint).intersection(last[0]))))
            route_before_selection = (last and required and last[3] and
                route['route'] != last[2] and route['revision'] > last[1] and same_selection)
            selection_before_route = (last and required and last[3] and
                not same_selection and route['revision'] <= last[1])
            if sidebar_pending or route_before_selection or selection_before_route:
                self.problem = '会话界面已切换，等待对应路由通知'
                self.problem_code = 'navigation_pending'
                continue
            self.selections[hwnd] = (fingerprint, route['revision'], route['route'], required)
            def relative(rect):
                result = dict(rect)
                result['left'] -= origin[0]; result['top'] -= origin[1]
                if 'right' in result: result['right'] -= origin[0]
                return result
            # UIA uses physical pixels, so this viewport has scale 1. Native
            # HWND binding avoids screen-coordinate ambiguity on mixed DPI.
            data = {'threadId': thread_id,
                    'viewKey': thread_id or draft[1].lower(),
                    'threadSource': 'desktop_client_binding' if draft and thread_id else 'desktop_window_route',
                    'nativeHwnd': hwnd, 'visible': True, 'focused': hwnd == foreground,
                    'dark': self.dark(window, view, foreground), 'composer': relative(view['composer']),
                    'toolbarGap': relative(view['gap']), 'viewport': {'width': size[0], 'height': size[1]}}
            results.append(Observation(f'native:{self.pid}:{hwnd}', data))
        if results:
            self.problem_code = 'supported'
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
