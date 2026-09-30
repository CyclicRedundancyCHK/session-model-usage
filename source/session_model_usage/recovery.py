"""Keep an explicit recovery launch alive without interrupting the user's Codex."""
from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .controller import launch, recovery_ready
from .platform_win import read_state, running_apps, state_directory, write_state


class RecoveryWorker(QThread):
    message_changed = Signal(str)

    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id
        self.stop_event = threading.Event()

    def run(self) -> None:
        previous = None
        try:
            while not self.stop_event.is_set():
                state = read_state()
                if state.get("run_id") != self.run_id:
                    return
                request = state_directory() / "stop.request"
                if request.exists() and request.read_text(encoding="utf-8") == self.run_id:
                    break
                try:
                    apps = running_apps()
                    if recovery_ready(apps):
                        result = launch(recovery_run_id=self.run_id)
                        if result.get("mode") == "overlay" and result.get("running"):
                            return
                        message = result.get("message", "正在恢复悬浮条")
                    elif len(apps) > 1:
                        message = "请手动退出普通 Codex 实例；启动器将在退出后自动恢复悬浮条"
                    else:
                        message = "Codex 未启用本机调试连接；请手动退出，启动器将自动打开新版并恢复悬浮条"
                except Exception as error:
                    message = f"恢复连接失败：{error}；将继续等待，可从托盘退出"
                state.update(status="waiting_for_restart", mode="recovery", message=message,
                             thread_id=None, overlay_visible=False, badge_rect=None, heartbeat=time.time())
                if read_state().get("run_id") != self.run_id:
                    return
                write_state(state)
                if message != previous:
                    self.message_changed.emit(message)
                    previous = message
                self.stop_event.wait(1)
        finally:
            state = read_state()
            if state.get("run_id") == self.run_id:
                state.update(status="stopped", message="恢复等待已停止", thread_id=None,
                             overlay_visible=False, badge_rect=None, heartbeat=time.time())
                write_state(state)


def run_recovery(run_id: str) -> int:
    from .overlay import icon
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    tray = QSystemTrayIcon(icon(), app)
    menu = QMenu()
    state_action = menu.addAction("等待手动退出 Codex 后恢复悬浮条")
    state_action.setEnabled(False)
    menu.addSeparator()
    menu.addAction("取消恢复等待").triggered.connect(app.quit)
    tray.setContextMenu(menu)
    tray.setToolTip("Codex 会话用量：等待恢复")
    worker = RecoveryWorker(run_id)
    worker.message_changed.connect(state_action.setText)
    worker.message_changed.connect(tray.setToolTip)
    worker.finished.connect(app.quit)
    def close():
        worker.stop_event.set()
        worker.wait(25000)
        tray.hide()
    app.aboutToQuit.connect(close)
    tray.show()
    worker.start()
    return app.exec()
