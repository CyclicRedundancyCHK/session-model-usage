from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from functools import wraps
from concurrent.futures import ThreadPoolExecutor

import psutil
from PySide6.QtCore import Qt, QThread, Signal, QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap, QPen
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QMenu, QPushButton,
    QSystemTrayIcon, QScrollArea, QStackedWidget, QButtonGroup, QGraphicsDropShadowEffect,
    QVBoxLayout, QHBoxLayout, QWidget, QSizePolicy)

from .accounting import UsageService
from .diagnostics import record
from .runtime_io import atomic_json
from .cdp import (Inspector, anchor_physical, bind_window, compact_tokens,
                  details_rectangle, toolbar_gap_physical)
from .platform_win import (alive, client_geometry, foreground_info, position_without_focus,
    read_state, set_no_activate, state_directory, write_state, visible_app_windows, attach_to_window)


def icon(dark: bool = True) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#0A84FF")); painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(6, 6, 52, 52, 14, 14)
    painter.setPen(QColor("#ffffff")); painter.setFont(QFont("Segoe UI", 23, QFont.Weight.DemiBold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "Σ")
    painter.end()
    return QIcon(pixmap)


PALETTES = {
    True: {"surface": "#242426", "card": "#2F2F32", "text": "#F5F5F7",
           "muted": "#A1A1A8", "edge": "#414145", "accent": "#0A84FF", "segment": "#37373B"},
    False: {"surface": "#FBFBFD", "card": "#FFFFFF", "text": "#1D1D1F",
            "muted": "#6E6E73", "edge": "#DEDEE3", "accent": "#007AFF", "segment": "#EFEFF3"},
}


def label(text: str = "", name: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def model_name(value: str) -> str:
    if value == "unattributed":
        return "未归属模型"
    parts = value.split("-")
    return "-".join(parts[:2]).upper() + " " + "-".join(parts[2:]).title() if value.startswith("gpt-") and len(parts) > 2 else value


def effort_name(value: str | None) -> str:
    return {"none": "None", "minimal": "Minimal", "low": "Low", "medium": "Medium",
            "high": "High", "xhigh": "XHigh", "max": "Max", "ultra": "Ultra"}.get(value, value or "未记录")


def number(widget: QLabel, value: int | None) -> None:
    widget.setText(compact_tokens(value))
    widget.setToolTip(f"{value:,} tokens" if value is not None else "等待用量记录")


class Badge(QPushButton):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Codex 会话用量")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._dark = True
        self.snapshot = None
        self.display = ""
        self.update_usage(None)
        self.fit_to_gap(400, 28)
        set_no_activate(int(self.winId()))

    def theme(self, dark: bool) -> None:
        if self._dark != dark:
            self._dark = dark
            self.update()

    def update_usage(self, snapshot: dict | None) -> None:
        self.snapshot = snapshot
        total = snapshot.get("totals") if snapshot else None
        state = " · 统计不完整" if snapshot and snapshot.get("status") == "partial" else ""
        self.setToolTip(f"{total['total_tokens']:,} tokens{state}\n点击查看模型、推理强度和智能体明细" if total else "等待请求用量记录 · 点击查看状态")

    def fit_to_gap(self, width: int, height: int) -> bool:
        if height < 20:
            return False
        height = min(28, height)
        font = QFont("Segoe UI Variable Text")
        font.setPixelSize(12 if height < 25 else 13)
        self.setFont(font)
        total = self.snapshot.get("totals") if self.snapshot else None
        value = compact_tokens(total["total_tokens"] if total else None)
        options = [f"本会话  {value} tokens", f"{value} tokens"]
        for text in options:
            desired = self.fontMetrics().horizontalAdvance(text) + 46
            if desired <= width:
                self.display = text
                self.setText(text)
                self.setAccessibleName(f"本会话 {value} tokens，点击查看模型与推理强度")
                self.setFixedSize(desired, height)
                self.update()
                return True
        return False

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = PALETTES[self._dark]
        bg = colors["segment"] if self.underMouse() else colors["card"]
        p.setBrush(QColor(bg)); p.setPen(QPen(QColor(colors["edge"]), 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), self.height()/2, self.height()/2)
        pending = not self.snapshot or not self.snapshot.get("totals")
        partial = self.snapshot and self.snapshot.get("status") == "partial"
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(colors["muted"] if pending else "#FF9F0A" if partial else colors["accent"]))
        p.drawEllipse(QPointF(13, self.height()/2), 2.5, 2.5)
        p.setFont(self.font()); p.setPen(QColor(colors["text"]))
        p.drawText(self.rect().adjusted(24, 0, -22, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.display)
        p.setPen(QPen(QColor(colors["muted"]), 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        x, y = self.width()-13, self.height()/2
        p.drawLine(QPointF(x-3, y-1), QPointF(x, y+2))
        p.drawLine(QPointF(x, y+2), QPointF(x+3, y-1))

    def enterEvent(self, event) -> None:
        super().enterEvent(event); self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event); self.update()


class ContributionBar(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(3)
        self.share = 1.0

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#BF5AF2"))
        p.drawRoundedRect(self.rect(), 1.5, 1.5)
        p.setBrush(QColor("#0A84FF"))
        p.drawRoundedRect(QRectF(0, 0, self.width()*self.share, self.height()), 1.5, 1.5)


class ModelCard(QFrame):
    def __init__(self, data: dict):
        super().__init__()
        self.setObjectName("modelCard")
        self.setFixedHeight(140)
        box = QVBoxLayout(self); box.setContentsMargins(16, 12, 16, 12); box.setSpacing(8)
        head = QHBoxLayout(); head.setSpacing(8)
        self.name, self.effort, self.total = label(name="modelName"), label(name="effort"), label(name="cardTotal")
        self.speed = label(name='speed')
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.effort.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(self.name, 1); head.addWidget(self.effort); head.addWidget(self.speed); head.addStretch(1)
        head.addWidget(self.total); head.addWidget(label("tokens", "muted"))
        box.addLayout(head)
        metrics = QHBoxLayout(); metrics.setSpacing(16)
        self.values = {}
        for key, title in (("input_tokens", "输入"), ("cached_input_tokens", "缓存输入"),
                           ("output_tokens", "输出"), ("reasoning_output_tokens", "推理输出")):
            column = QVBoxLayout(); column.setSpacing(2)
            value = label(name="metricValue"); self.values[key] = value
            column.addWidget(label(title, "muted")); column.addWidget(value)
            metrics.addLayout(column, 1)
        box.addLayout(metrics)
        sources = QHBoxLayout()
        self.main, self.child = label(name="mainSource"), label(name="childSource")
        sources.addWidget(self.main); sources.addStretch(); sources.addWidget(self.child)
        box.addLayout(sources)
        self.bar = ContributionBar(); box.addWidget(self.bar)
        self.update_usage(data)

    def update_usage(self, data: dict) -> None:
        self.name.setText(model_name(data["model"]))
        self.name.setMaximumWidth(min(190, self.name.fontMetrics().horizontalAdvance(self.name.text()) + 2))
        self.name.setToolTip(data["model"])
        level = data.get("reasoning_effort")
        self.effort.setText(effort_name(level))
        self.effort.setToolTip(f"推理强度：{level}" if level is not None else "本地记录没有推理强度，保留为未记录")
        tier, fast = data.get('service_tier'), data.get('fast_mode')
        self.speed.setText('Fast' if fast is True else '普通' if fast is False else
                           f'其他：{tier}' if tier is not None else '未记录')
        self.speed.setToolTip(f'日志记录的请求服务等级：{tier or "未记录"}；不代表服务端实际采用的等级')
        self.speed.setStyleSheet('color:#0A84FF;' if fast is True else '')
        number(self.total, data["totals"]["total_tokens"])
        for key, widget in self.values.items():
            number(widget, data["totals"].get(key, 0))
        main, child = data["main"]["total_tokens"], data["subagents"]["total_tokens"]
        self.main.setText(f"主会话  {compact_tokens(main)}")
        self.child.setText(f"子智能体  {compact_tokens(child)}")
        self.main.setToolTip(f"主会话贡献 {main:,} tokens")
        self.child.setToolTip(f"子智能体贡献 {child:,} tokens")
        self.bar.share = main / max(1, main+child)
        self.bar.update()


class ThreadCard(QFrame):
    def __init__(self, data: dict):
        super().__init__(); self.setObjectName("threadCard"); self.setFixedHeight(72)
        box = QVBoxLayout(self); box.setContentsMargins(16, 10, 16, 10); box.setSpacing(4)
        top, bottom = QHBoxLayout(), QHBoxLayout()
        self.name, self.total, self.identity, self.status = label(name="modelName"), label(name="cardTotal"), label(name="muted"), label(name="muted")
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(self.name, 1); top.addWidget(self.total); top.addWidget(label("tokens", "muted"))
        bottom.addWidget(self.identity); bottom.addStretch(); bottom.addWidget(self.status)
        box.addLayout(top); box.addLayout(bottom)
        self.update_usage(data)

    def update_usage(self, data: dict) -> None:
        self.name.setText("主会话" if data["role"] == "main" else data.get("agent_path") or "子智能体")
        self.name.setToolTip(self.name.text())
        self.identity.setText(data["thread_id"][:8]); self.identity.setToolTip(data["thread_id"])
        number(self.total, data["totals"]["total_tokens"] if data.get("totals") else None)
        self.status.setText({"complete": "完整记录", "partial": "统计不完整", "pending": "等待记录"}.get(data["status"], "未知"))


class CardList(QScrollArea):
    def __init__(self, factory):
        super().__init__(); self.setWidgetResizable(True); self.setFrameShape(QFrame.Shape.NoFrame)
        self.factory, self.keys, self.cards = factory, [], []
        self.content = QWidget(); self.content.setObjectName("cardList")
        self.box = QVBoxLayout(self.content); self.box.setContentsMargins(0, 0, 4, 0)
        self.box.setSpacing(8); self.box.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.empty = label("请求完成并写入日志后，用量会显示在这里。", "muted")
        self.empty.setWordWrap(True); self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.box.addWidget(self.empty)
        self.setWidget(self.content)

    def update_usage(self, entries: list[dict], keys: list) -> None:
        if keys != self.keys:
            position = self.verticalScrollBar().value()
            for card in self.cards:
                self.box.removeWidget(card); card.deleteLater()
            self.cards = [self.factory(data) for data in entries]
            for card, data in zip(self.cards, entries):
                self.box.addWidget(card)
                card.ensurePolished()
                card.update_usage(data)
            self.keys = keys
            self.verticalScrollBar().setValue(position)
        else:
            for card, data in zip(self.cards, entries):
                card.update_usage(data)
        self.empty.setVisible(not entries)


class Details(QFrame):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Codex 会话用量明细")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFont(QFont("Segoe UI Variable Text", 9))
        self.resize(760, 510)
        self._dark = None
        outer = QVBoxLayout(self); outer.setContentsMargins(8, 8, 8, 8)
        self.panel = QFrame(); self.panel.setObjectName("panel"); outer.addWidget(self.panel)
        shadow = QGraphicsDropShadowEffect(self.panel); shadow.setBlurRadius(18); shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 55)); self.panel.setGraphicsEffect(shadow)
        layout = QVBoxLayout(self.panel); layout.setContentsMargins(18, 14, 18, 14); layout.setSpacing(12)
        header = QVBoxLayout(); header.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(label("会话用量", "title"))
        self.meta = label(name="muted"); top.addStretch(); top.addWidget(self.meta)
        close = QPushButton("×"); close.setFixedSize(26, 26); close.setObjectName("close")
        close.setToolTip("关闭明细"); close.setFocusPolicy(Qt.FocusPolicy.NoFocus); close.clicked.connect(self.hide)
        top.addWidget(close); header.addLayout(top)
        total_row = QHBoxLayout(); total_row.setSpacing(8)
        self.total, self.unit, self.record_state = label("—", "total"), label("tokens", "unit"), label("等待记录", "recordState")
        total_row.addWidget(self.total); total_row.addWidget(self.unit, alignment=Qt.AlignmentFlag.AlignBottom)
        total_row.addStretch(); total_row.addWidget(self.record_state)
        header.addLayout(total_row); layout.addLayout(header)
        segments = QFrame(); segments.setObjectName("segments"); segments.setFixedHeight(34)
        segmented = QHBoxLayout(segments); segmented.setContentsMargins(3, 3, 3, 3); segmented.setSpacing(3)
        self.group = QButtonGroup(self); self.group.setExclusive(True)
        self.pages = QStackedWidget()
        self.models, self.threads = CardList(ModelCard), CardList(ThreadCard)
        for index, (title, widget) in enumerate((("模型、推理强度与 Fast", self.models), ("智能体", self.threads))):
            button = QPushButton(title); button.setObjectName("segment"); button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus); self.group.addButton(button, index); segmented.addWidget(button)
            self.pages.addWidget(widget)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(self.pages.setCurrentIndex)
        layout.addWidget(segments); layout.addWidget(self.pages, 1)
        self.notice = label("总量 = 输入 + 输出，分项不重复累加。Fast 为日志中的请求设置，未验证服务端实际等级。", "notice")
        self.notice.setWordWrap(True); layout.addWidget(self.notice)
        self.warning = label(name="warning"); self.warning.setWordWrap(True); self.warning.hide()
        layout.addWidget(self.warning)
        self.theme(True)
        set_no_activate(int(self.winId()))

    def theme(self, dark: bool) -> None:
        if self._dark == dark:
            return
        self._dark = dark
        c = PALETTES[dark]
        self.setStyleSheet(f"""
            QWidget {{ color:{c['text']}; font-family:'Segoe UI Variable Text','Microsoft YaHei UI'; font-size:12px; }}
            QFrame#panel {{ background:{c['surface']}; border:1px solid {c['edge']}; border-radius:18px; }}
            QLabel {{ background:transparent; border:0; }}
            QLabel#title {{ font-size:14px; font-weight:600; }}
            QLabel#total {{ font-family:'Segoe UI Variable Display'; font-size:32px; font-weight:600; }}
            QLabel#unit {{ color:{c['muted']}; font-size:14px; padding-bottom:6px; }}
            QLabel#muted, QLabel#notice {{ color:{c['muted']}; font-size:11px; }}
            QLabel#speed {{ background:{c['segment']}; color:{c['muted']}; border-radius:5px; padding:3px 6px; font-size:11px; }}
            QLabel#recordState {{ color:{c['muted']}; font-size:11px; }}
            QLabel#warning {{ color:{'#FFB340' if dark else '#965B00'}; font-size:11px; }}
            QFrame#modelCard, QFrame#threadCard {{ background:{c['card']}; border:1px solid {c['edge']}; border-radius:13px; }}
            QLabel#modelName {{ font-size:14px; font-weight:600; }}
            QLabel#effort {{ background:{c['segment']}; color:{c['text']}; border:0; border-radius:7px; padding:3px 9px; font-size:11px; font-weight:600; }}
            QLabel#cardTotal {{ font-size:19px; font-weight:600; }}
            QLabel#metricValue {{ font-size:18px; font-weight:500; }}
            QLabel#mainSource {{ color:{c['accent']}; font-size:11px; }}
            QLabel#childSource {{ color:{'#D494FA' if dark else '#9047BC'}; font-size:11px; }}
            QFrame#segments {{ background:{c['segment']}; border:0; border-radius:9px; }}
            QPushButton#segment {{ background:transparent; color:{c['muted']}; border:0; border-radius:6px; padding:5px; }}
            QPushButton#segment:checked {{ background:{c['card']}; color:{c['text']}; font-weight:600; }}
            QPushButton#segment:hover {{ color:{c['text']}; }}
            QPushButton#close {{ background:{c['segment']}; color:{c['muted']}; border:0; border-radius:13px; font-size:18px; }}
            QPushButton#close:hover {{ color:{c['text']}; }}
            QScrollArea, QStackedWidget, QWidget#cardList {{ background:transparent; border:0; }}
            QScrollBar:vertical {{ background:transparent; width:5px; margin:2px 0; }}
            QScrollBar::handle:vertical {{ background:{c['edge']}; border-radius:2px; min-height:20px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
        """)

    def update_usage(self, snapshot: dict | None, thread_id: str | None) -> None:
        total = snapshot.get("totals") if snapshot else None
        self.total.setText(f"{total['total_tokens']:,}" if total else "—")
        status = snapshot.get("status", "pending") if snapshot else "pending"
        self.record_state.setText({"complete": "在本机统计", "partial": "统计不完整", "pending": "等待用量记录"}.get(status, "等待用量记录"))
        children = max(0, len(snapshot.get("threads", [])) - 1) if snapshot else 0
        self.meta.setText(f"主会话 · {children} 个子智能体")
        self.meta.setToolTip(thread_id or "")
        entries = []
        for model in snapshot.get("models", []) if snapshot else []:
            for group in model.get('configurations') or model.get("reasoning_efforts") or [{**model, "reasoning_effort": None}]:
                entries.append({**group, "model": model["model"]})
        self.models.update_usage(entries, [(e["model"], e["reasoning_effort"], e.get('service_tier')) for e in entries])
        threads = snapshot.get("threads", []) if snapshot else []
        self.threads.update_usage(threads, [e["thread_id"] for e in threads])
        warnings = snapshot.get("warnings", []) if snapshot else []
        self.warning.setText(warnings[0] if warnings else "")
        self.warning.setToolTip("\n".join(warnings))
        self.warning.setVisible(bool(warnings))


class Observer(QThread):
    observation = Signal(object)
    snapshot = Signal(object)
    state_changed = Signal(str)

    def __init__(self, port: int, app_pid: int, run_id: str, attachment_id: str | None = None):
        super().__init__()
        self.port, self.app_pid, self.run_id = port, app_pid, run_id
        self.attachment_id = attachment_id
        self.stop_event = threading.Event()
        self.last_target = None
        self.last_hwnd = 0
        self.placement = {"visible": False, "thread_id": None, "message": ""}

    def report(self, state):
        state.update(heartbeat=time.time(), heartbeat_monotonic=time.monotonic(),
                     run_id=self.run_id, attachment_id=self.attachment_id)
        if self.attachment_id:
            atomic_json(state_directory() / f'observer-{self.attachment_id}.json', state)
        else:
            write_state(state)

    def run(self) -> None:
        inspector = pool = None
        state = {"status": "starting", "message": "正在连接", "thread_id": None,
                 "overlay_visible": False, "badge_rect": None}
        try:
            inspector, service = Inspector(self.port), UsageService()
            app = psutil.Process(self.app_pid)
            app_created = app.create_time()
            self.observe_loop(inspector, service, app_created, state)
        except Exception as error:
            record('observer_failed', error)
            state.update(last_error=type(error).__name__)
        finally:
            if inspector is not None:
                inspector.close()
            state.update(status="stopped", message="悬浮条观察已停止", thread_id=None,
                         overlay_visible=False, badge_rect=None)
            self.report(state)

    def observe_loop(self, inspector, service, app_created, state):
        own_pid = os.getpid()
        state.update(overlay_pid=own_pid, overlay_created=psutil.Process(own_pid).create_time())
        last_usage, last_thread, last_write = 0.0, None, 0.0
        family = {self.app_pid}
        last_message = ""
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-token-reader")
        pending = None
        next_connect, connection_attempt = 0.0, 0
        try:
            while not self.stop_event.is_set() and alive(self.app_pid, app_created):
                start = time.monotonic()
                try:
                    if pending is not None and pending.done():
                        try:
                            self.snapshot.emit(pending.result())
                        except Exception as error:
                            self.snapshot.emit({"thread_id": last_thread, "status": "partial", "totals": None,
                                "models": [], "threads": [], "warnings": [f"读取用量失败：{type(error).__name__}"]})
                        pending = None
                    request = state_directory() / "stop.request"
                    if request.exists() and request.read_text(encoding="utf-8") == self.run_id:
                        self.stop_event.set(); break
                    # Electron's GUI process owns its BrowserWindows. Tool
                    # programs launched by Codex may also be descendants, but
                    # their windows must never become a conversation's host.
                    hwnd, pid = foreground_info()
                    state.update(foreground_pid=pid)
                    if start < next_connect:
                        self.report(state)
                        self.stop_event.wait(min(0.5, next_connect - start))
                        continue
                    observations = inspector.observe()
                    if not observations and inspector.connection_failed:
                        raise RuntimeError('界面调试目标暂时断开')
                    connection_attempt = 0
                    windows = visible_app_windows(family)
                    binding = bind_window(observations, windows, hwnd, self.last_target, self.last_hwnd)
                    if binding:
                        current, window = binding
                        self.last_hwnd = window["hwnd"]
                        geometry = client_geometry(self.last_hwnd)
                        if geometry:
                            thread_id = current.data["threadId"]
                            self.last_target = current.target_id
                            data = {**current.data, "client_origin": geometry[0], "client_size": geometry[1],
                                    "native_scale": window["scale"], "host_hwnd": self.last_hwnd}
                            state.update(overlay_visible=False, badge_rect=None)
                            self.observation.emit(data)
                            state.update(thread_id=thread_id, status="connected", host_hwnd=self.last_hwnd,
                                         message="自动跟随当前会话" if hwnd == self.last_hwnd else "Codex 在后台，继续跟随当前会话")
                            placement = self.placement
                            if placement.get("thread_id") == thread_id:
                                state.update(overlay_visible=placement["visible"],
                                             badge_rect=placement.get("rect"))
                                if placement.get("message"):
                                    state.update(status="placement_unavailable", message=placement["message"])
                            if pending is None and (thread_id != last_thread or start - last_usage >= 1):
                                pending = pool.submit(service.query, thread_id)
                                last_thread, last_usage = thread_id, start
                        else:
                            self.observation.emit(None)
                            state.update(thread_id=None, status="hidden", message="Codex 窗口不可见")
                    else:
                        self.observation.emit(None)
                        message = ("Codex 窗口已最小化、隐藏或位于其他桌面" if not windows else
                                   "无法唯一对应 Codex 窗口与当前会话界面" if observations else inspector.problem)
                        state.update(thread_id=None, status="hidden" if observations else "waiting_for_view", message=message)
                except Exception as error:
                    record('observer_connection_failed', error)
                    inspector.close()
                    inspector = Inspector(self.port)
                    next_connect = start + (1, 2, 4, 8, 15)[min(connection_attempt, 4)]
                    connection_attempt += 1
                    self.observation.emit(None)
                    state.update(thread_id=None, status="connection_error", message=f"无法自动跟随：{type(error).__name__}。请检查启动方式和版本兼容。")
                state["heartbeat"] = time.time()
                if state["status"] not in ("connected", "placement_unavailable"):
                    state.update(overlay_visible=False, badge_rect=None)
                if state["message"] != last_message:
                    self.state_changed.emit(state["message"])
                    last_message = state["message"]
                if start - last_write >= 1:
                    self.report(state); last_write = start
                self.stop_event.wait(max(0, 0.25 - (time.monotonic() - start)))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            inspector.close()
            state.update(status="stopped", message="悬浮条已停止", thread_id=None, heartbeat=time.time())
            state.update(overlay_visible=False, badge_rect=None)
            self.report(state)


def guarded_ui(method):
    @wraps(method)
    def invoke(self, *arguments):
        try:
            return method(self, *arguments)
        except Exception as error:
            record('overlay_callback_failed', error, callback=method.__name__)
            self.data = self.current = self.usage = None
            self.badge.hide()
            self.details.hide()
            self.worker.placement = {'visible': False, 'thread_id': None, 'message': '界面暂不可用，正在重新识别'}
    return invoke


class Overlay:
    def __init__(self, app: QApplication, port: int, app_pid: int, run_id: str, attachment_id: str | None = None):
        self.app = app
        self.badge, self.details = Badge(), Details()
        self.current = None
        self.usage = None
        self.data = None
        self.badge.clicked.connect(self.toggle_details)
        self.tray = QSystemTrayIcon(icon(), app)
        menu = QMenu()
        self.state_action = menu.addAction("正在连接 Codex…"); self.state_action.setEnabled(False)
        menu.addSeparator()
        details = menu.addAction("查看当前会话明细"); details.triggered.connect(self.toggle_details)
        quit_action = menu.addAction("退出悬浮条"); quit_action.triggered.connect(app.quit)
        self.tray.setContextMenu(menu); self.tray.setToolTip("Codex 会话用量")
        if not attachment_id:
            self.tray.show()
        self.worker = Observer(port, app_pid, run_id, attachment_id)
        self.worker.observation.connect(self.observe)
        self.worker.snapshot.connect(self.update_usage)
        self.worker.state_changed.connect(self.update_state)
        self.worker.finished.connect(app.quit)
        app.aboutToQuit.connect(self.close)
        self.worker.start()

    def update_state(self, message: str) -> None:
        self.tray.setToolTip(message)
        self.state_action.setText(message)

    @guarded_ui
    def observe(self, data: dict | None) -> None:
        self.data = data
        if not data:
            self.current = self.usage = None
            self.worker.placement = {"visible": False, "thread_id": None, "message": ""}
            self.badge.hide(); self.details.hide(); return
        if self.current != data["threadId"]:
            self.current = data["threadId"]
            self.usage = None
            self.details.hide()
            self.badge.update_usage(None)
            self.details.update_usage(None, self.current)
        self.badge.theme(data["dark"]); self.details.theme(data["dark"])
        scale = data["native_scale"]
        gap = toolbar_gap_physical(data, data["client_origin"], data["client_size"])
        problem = ("无法识别权限与背景信息控件的位置，已暂停悬浮条定位" if not gap else
                   "输入栏控件之间的空间不足，请增大窗口宽度")
        if not gap or not self.badge.fit_to_gap(int(gap[2] / scale), int(gap[3] / scale)):
            self.worker.placement = {"visible": False, "thread_id": self.current, "message": problem}
            self.tray.setToolTip(problem)
            self.badge.hide(); self.details.hide(); return
        size = (round(self.badge.width() * scale), round(self.badge.height() * scale))
        position = anchor_physical(data, data["client_origin"], data["client_size"], size)
        if position is None or position[1] < data["client_origin"][1] or size[0] > data["client_size"][0]:
            self.worker.placement = {"visible": False, "thread_id": self.current, "message": problem}
            self.badge.hide(); self.details.hide(); return
        attach_to_window(int(self.badge.winId()), data["host_hwnd"])
        if not self.badge.isVisible():
            self.badge.show()
        position_without_focus(int(self.badge.winId()), *position, *size, owner=data["host_hwnd"])
        self.worker.placement = {"visible": True, "thread_id": self.current, "message": "",
                                 "rect": [*position, *size]}
        if self.details.isVisible():
            self.place_details(position, scale)

    @guarded_ui
    def update_usage(self, usage: dict) -> None:
        if usage["thread_id"] != self.current:
            return
        self.usage = usage
        self.badge.update_usage(usage)
        self.details.update_usage(usage, self.current)
        if self.data:
            self.observe(self.data)

    def place_details(self, badge_position: tuple[int, int], scale: float) -> None:
        rectangle = details_rectangle(self.data, badge_position, scale)
        if rectangle is None:
            self.details.hide()
            self.tray.setToolTip("窗口高度不足以在输入栏上方展开明细，请增大窗口或使用 query 查看")
            return
        position_without_focus(int(self.details.winId()), *rectangle, owner=self.data["host_hwnd"])

    @guarded_ui
    def toggle_details(self) -> None:
        if self.details.isVisible():
            self.details.hide()
        elif self.data:
            self.details.update_usage(self.usage, self.current)
            attach_to_window(int(self.details.winId()), self.data["host_hwnd"])
            self.details.show()
            self.observe(self.data)

    def close(self) -> None:
        self.worker.stop_event.set()
        self.worker.wait(10000)
        self.badge.hide(); self.details.hide(); self.tray.hide()


def run_overlay(port: int, app_pid: int, run_id: str, attachment_id: str | None = None) -> int:
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(icon())
    overlay = Overlay(app, port, app_pid, run_id, attachment_id)
    return app.exec()


def preview(path: Path, dark: bool = True) -> None:
    """Render this widget itself; does not capture the user's desktop."""
    app = QApplication.instance() or QApplication([])
    details = Details(); details.theme(dark)
    sample = {"status": "complete", "totals": {"total_tokens": 164000}, "warnings": [],
      "models": [{"model": model, "totals": {"total_tokens": total, "input_tokens": inp,
          "cached_input_tokens": cached, "output_tokens": out, "reasoning_output_tokens": reason},
          "main": {"total_tokens": main}, "subagents": {"total_tokens": total-main},
          "reasoning_efforts": [{"reasoning_effort": level, "totals": {"total_tokens": total,
              "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
              "reasoning_output_tokens": reason}, "main": {"total_tokens": main},
              "subagents": {"total_tokens": total-main}}]}
          for model,level,total,inp,cached,out,reason,main in (
              ("gpt-6-sol","max",126000,116000,86000,10000,4000,98000),
              ("gpt-6-luna","high",38000,34000,24000,4000,2000,0))],
      "threads": [{"thread_id": "demo-main", "role": "main", "agent_path": None,
                   "totals": {"total_tokens": 98000}, "status": "complete"},
                  {"thread_id": "demo-child", "role": "subagent", "agent_path": "/root/review",
                   "totals": {"total_tokens": 66000}, "status": "complete"}]}
    for model, tier in zip(sample['models'], ('priority', 'default')):
        model['configurations'] = [{**group, 'service_tier': tier, 'fast_mode': tier == 'priority'}
                                   for group in model['reasoning_efforts']]
    details.update_usage(sample, "示例会话")
    details.show(); app.processEvents()
    path.parent.mkdir(parents=True, exist_ok=True)
    details.grab().save(str(path))
    details.hide()
    badge = Badge(); badge.theme(dark); badge.update_usage(sample); badge.fit_to_gap(400, 28)
    badge.show(); app.processEvents()
    badge.grab().save(str(path.with_stem(path.stem + "-badge")))
    badge.hide()
