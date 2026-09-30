from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
import urllib.parse
import urllib.request

import websocket


READ_COMPOSER = r"""(() => {
  const parseThread = path => typeof path === 'string' ?
    path.match(/(?:^|\/)local\/([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})(?:\/|$|\?)/i)?.[1]?.toLowerCase() ?? null : null;
  const candidates = Array.from(document.querySelectorAll('[data-codex-composer]'));
  const visible = candidates.map(el => ({el,
      rect: (el.closest('[data-codex-composer-root]') ?? el).getBoundingClientRect()}))
    .filter(x => x.rect.width > 80 && x.rect.height > 20 &&
      getComputedStyle(x.el).visibility !== 'hidden' && getComputedStyle(x.el).display !== 'none');
  const current = visible.length === 1 ? visible[0] :
    visible.find(x => x.el.contains(document.activeElement));
  // Recent desktop versions use an in-memory React router. Read the nearest
  // location provider above this composer; never infer from sidebar recency.
  let route = null, threadSource = 'url';
  let host = current?.el, fiber = null;
  for (let depth=0; host && depth<16; depth++, host=host.parentElement) {
    const key = Object.getOwnPropertyNames(host).find(k => k.startsWith('__reactFiber$'));
    if (key) { fiber=host[key]; break; }
  }
  for (let depth=0; fiber && depth<180; depth++, fiber=fiber.return) {
    const props = fiber.memoizedProps;
    const matches = props?.value?.matches;
    const match = Array.isArray(matches) ? matches.at(-1)?.pathname : null;
    if (typeof match === 'string') { route=match; threadSource='composer-route'; break; }
    const value = props?.value?.location ?? props?.location;
    if (typeof value?.pathname === 'string') { route=value.pathname; threadSource='composer-route'; break; }
  }
  const threadId = route !== null ? parseThread(route) :
    parseThread(location.pathname) ?? parseThread(location.hash.replace(/^#/, ''));
  const root = current?.el.closest('[data-codex-composer-root]');
  const rect = root?.getBoundingClientRect() ?? current?.rect;
  const packRect = el => {
    if (!el) return null;
    const r=el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 ? {left:r.left,top:r.top,right:r.right,
      bottom:r.bottom,width:r.width,height:r.height} : null;
  };
  const permissions = root?.querySelector('[data-composer-navigation-target="permissions"]');
  const intelligence = root?.querySelector('[data-codex-intelligence-trigger]') ??
    root?.querySelector('[data-composer-navigation-target="reasoning"]');
  // The nearest control groups include optional Plan buttons on the left and
  // the context-window ring on the right. Use their real edges, not text labels.
  const leftControls = packRect(permissions?.closest('div'));
  const rightControls = packRect(intelligence?.closest('div'));
  const permissionRect = packRect(permissions), modelRect = packRect(intelligence);
  const gapTop=Math.max(permissionRect?.top ?? 0, modelRect?.top ?? 0);
  const gapBottom=Math.min(permissionRect?.bottom ?? 0, modelRect?.bottom ?? 0);
  const toolbarGap = leftControls && rightControls && gapBottom > gapTop &&
    rightControls.left > leftControls.right + 16 ?
      {left:leftControls.right+8,right:rightControls.left-8,top:gapTop,bottom:gapBottom,
       width:rightControls.left-leftControls.right-16,height:gapBottom-gapTop} : null;
  const scheme = getComputedStyle(document.documentElement).colorScheme;
  const dark = document.documentElement.classList.contains('dark') ||
    document.documentElement.dataset.theme === 'dark' || scheme === 'dark' ||
    (scheme !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches);
  return {threadId, threadSource, focused: document.hasFocus(), dark,
    composer: rect ? {left:rect.left, top:rect.top, right:rect.right,
      bottom:rect.bottom, width:rect.width, height:rect.height} : null,
    toolbarGap, toolbarControls: {permissions:permissionRect,context:rightControls,model:modelRect},
    viewport: {width:innerWidth, height:innerHeight, dpr:devicePixelRatio},
    screen: {x:screenX, y:screenY, width:outerWidth, height:outerHeight,
      workX:screen.availLeft, workY:screen.availTop},
    visible: document.visibilityState === 'visible'};
})()"""


class ConnectionError(RuntimeError):
    pass


def local_url(value: str, port: int, websocket_url: bool = False) -> str:
    parsed = urllib.parse.urlsplit(value)
    expected = ("ws",) if websocket_url else ("http",)
    if parsed.scheme not in expected or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.port != port:
        raise ConnectionError("调试目标不属于指定的本机端口")
    return value


class CDPConnection:
    def __init__(self, url: str, port: int):
        self.socket = websocket.create_connection(local_url(url, port, True),
                                                   timeout=0.8, suppress_origin=True,
                                                   http_no_proxy=["127.0.0.1", "localhost", "::1"])
        self.sequence = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.sequence += 1
        self.socket.send(json.dumps({"id": self.sequence, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            message = json.loads(self.socket.recv())
            if message.get("id") != self.sequence:
                continue
            if "error" in message:
                raise ConnectionError(f"调试协议 {method} 不可用")
            return message.get("result", {})
        raise ConnectionError("调试连接响应超时")

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:
            pass


@dataclass
class Observation:
    target_id: str
    data: dict
    bounds: dict | None = None


class Inspector:
    def __init__(self, port: int):
        self.port = port
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.connections: dict[str, CDPConnection] = {}
        self.targets = []
        self.last_discovery = 0.0
        self.browser: CDPConnection | None = None
        self.problem = "正在识别 Codex 界面"

    def get_json(self, path: str) -> dict | list:
        with self.opener.open(f"http://127.0.0.1:{self.port}{path}", timeout=0.8) as response:
            return json.load(response)

    def observe(self) -> list[Observation]:
        now = time.monotonic()
        if now - self.last_discovery >= 1:
            targets = self.get_json("/json/list")
            self.targets = [x for x in targets if x.get("type") in ("page", "other")
                            and x.get("webSocketDebuggerUrl") and x.get("url", "").startswith(("app://", "file://"))]
            present = {x["id"] for x in self.targets}
            for key in list(self.connections):
                if key not in present:
                    self.connections.pop(key).close()
            self.last_discovery = now
        result = []
        self.problem = "此版本未暴露应用界面调试目标" if not self.targets else "正在等待界面加载"
        for target in self.targets:
            key = target["id"]
            try:
                connection = self.connections.get(key)
                if connection is None:
                    connection = self.connections[key] = CDPConnection(target["webSocketDebuggerUrl"], self.port)
                reply = connection.call("Runtime.evaluate", {"expression": READ_COMPOSER,
                                           "returnByValue": True})
                data = reply.get("result", {}).get("value")
                if not isinstance(data, dict):
                    self.problem = "界面调试目标未返回可读取的数据"
                    continue
                if not data.get("composer"):
                    self.problem = "没有可见的输入栏，或此版本的输入栏标识已改变"
                    continue
                if not data.get("threadId"):
                    self.problem = "请打开本机 Codex 会话；当前输入栏没有可确认的 /local/ 会话路由"
                    continue
                result.append(Observation(key, data, self._bounds(key)))
            except Exception as error:
                self.problem = f"无法读取界面调试目标：{type(error).__name__}"
                connection = self.connections.pop(key, None)
                if connection:
                    connection.close()
        return result

    def _bounds(self, target_id: str) -> dict | None:
        try:
            if self.browser is None:
                version = self.get_json("/json/version")
                self.browser = CDPConnection(version["webSocketDebuggerUrl"], self.port)
            return self.browser.call("Browser.getWindowForTarget", {"targetId": target_id}).get("bounds")
        except Exception:
            return None

    def close(self) -> None:
        for connection in self.connections.values():
            connection.close()
        self.connections.clear()
        if self.browser:
            self.browser.close()


def compact_tokens(value: int | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return f"{value:,}"


def screen_matches(data: dict, native: tuple[int, int, int, int] | list[tuple[int, int, int, int]], scale: float,
                   work_origin: tuple[int, int] | None = None) -> bool:
    screen = data.get("screen", {})
    if not all(type(screen.get(k)) in (int, float) for k in ("x", "y", "width", "height")):
        return False
    x, y, width, height = (screen[k] for k in ("x", "y", "width", "height"))
    if work_origin and all(type(screen.get(k)) in (int, float) for k in ("workX", "workY")):
        x = work_origin[0] + (x - screen["workX"]) * scale
        y = work_origin[1] + (y - screen["workY"]) * scale
    elif scale != 1:
        return False
    expected = (x, y, width * scale, height * scale)
    tolerance = max(8, 12 * scale)
    # Chromium's outer screen rectangle can map to either the Win32 top-level
    # bounds or client rectangle, depending on the window frame and state.
    rectangles = [native] if len(native) == 4 and isinstance(native[0], (int, float)) else native
    return width > 0 and height > 0 and any(
        all(abs(a - b) <= tolerance for a, b in zip(rect, expected)) for rect in rectangles
    )


def choose_observation(observations: list[Observation], previous: str | None = None,
                       overlay_foreground: bool = False) -> Observation | None:
    candidates = [x for x in observations if x.data.get("visible") and x.data.get("composer")
                  and not (x.bounds and x.bounds.get("windowState") == "minimized")]
    focused = [x for x in candidates if x.data.get("focused")]
    if len(focused) == 1:
        return focused[0]
    if overlay_foreground and previous:
        matches = [x for x in candidates if x.target_id == previous]
        if len(matches) == 1:
            return matches[0]
    return None


def bind_window(observations: list[Observation], windows: list[dict],
                foreground_hwnd: int, previous_target: str | None = None,
                previous_hwnd: int = 0) -> tuple[Observation, dict] | None:
    """Match a real, unminimized Codex window even after it loses focus.

    Chromium may report an occluded page as hidden. Native window visibility
    decides whether to keep the attachment; route/geometry still decide identity.
    """
    pairs = [(view, window) for view in observations for window in windows
             if view.data.get("composer") and
             not (view.bounds and view.bounds.get("windowState") == "minimized") and
             screen_matches(view.data, window["rectangles"], window["scale"], window["work_origin"])]
    focused = [(view, window) for view, window in pairs
               if window["hwnd"] == foreground_hwnd and view.data.get("focused")]
    if len(focused) == 1:
        return focused[0]
    previous = [(view, window) for view, window in pairs
                if view.target_id == previous_target and window["hwnd"] == previous_hwnd]
    if len(previous) == 1:
        return previous[0]
    if len(pairs) == 1 and previous_target and previous_hwnd:
        view, window = pairs[0]
        if view.target_id == previous_target and window["hwnd"] != previous_hwnd:
            # A minimized host can leave stale Chromium geometry matching an
            # auxiliary window. Require focus evidence before transferring it.
            return None
    return pairs[0] if len(pairs) == 1 else None


def toolbar_gap_physical(data: dict, client_origin: tuple[int, int],
                         client_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Map CSS viewport pixels to native client pixels (DPI and zoom included)."""
    rect, viewport = data.get("toolbarGap"), data["viewport"]
    if not rect:
        return None
    scale_x = client_size[0] / max(1, viewport["width"])
    scale_y = scale_x
    top_inset = max(0, client_size[1] - round(viewport["height"] * scale_y))
    return (client_origin[0] + round(rect["left"] * scale_x),
            client_origin[1] + top_inset + round(rect["top"] * scale_y),
            round(rect["width"] * scale_x), round(rect["height"] * scale_y))


def anchor_physical(data: dict, client_origin: tuple[int, int],
                    client_size: tuple[int, int], badge_size: tuple[int, int]) -> tuple[int, int] | None:
    gap = toolbar_gap_physical(data, client_origin, client_size)
    if not gap or gap[2] < badge_size[0] or gap[3] < badge_size[1]:
        return None
    x = gap[0] + (gap[2] - badge_size[0]) // 2
    y = gap[1] + (gap[3] - badge_size[1]) // 2
    return x, y


def details_rectangle(data: dict, badge_position: tuple[int, int], scale: float) -> tuple[int, int, int, int] | None:
    origin, client = data["client_origin"], data["client_size"]
    margin = round(12 * scale)
    css_scale = client[0] / max(1, data["viewport"]["width"])
    inset = max(0, client[1] - round(data["viewport"]["height"] * css_scale))
    composer_top = origin[1] + inset + round(data["composer"]["top"] * css_scale)
    bottom = composer_top - round(10 * scale)
    available = bottom - origin[1] - margin
    if available < round(180 * scale) or client[0] < round(320 * scale):
        return None
    width = min(round(760 * scale), client[0] - 2 * margin)
    height = min(round(510 * scale), available)
    right = origin[0] + round(data["composer"]["right"] * (client[0] / data["viewport"]["width"]))
    x = max(origin[0] + margin, min(right - width, origin[0] + client[0] - margin - width))
    y = bottom - height
    return x, y, width, height
