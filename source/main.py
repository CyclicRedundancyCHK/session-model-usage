from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

from session_model_usage.accounting import UsageService
from session_model_usage.controller import RestartRequired, launch, status, stop


def main() -> int:
    gui = getattr(sys, "frozen", False) and Path(sys.executable).stem.lower() == "codexsessionusage"
    parser = argparse.ArgumentParser(description="Codex 会话模型用量 · 本机只读统计")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("launch", help="启动 Codex 和悬浮条；已有普通实例时提示手动退出")
    query = sub.add_parser("query", help="查询会话用量 JSON")
    query.add_argument("--thread-id", default=os.environ.get("CODEX_THREAD_ID"))
    query.add_argument("--codex-home", type=Path)
    query.add_argument("--no-descendants", action="store_true")
    sub.add_parser("status", help="查询悬浮条状态")
    sub.add_parser("stop", help="关闭悬浮条，不关闭 Codex")
    install = sub.add_parser("install", help="注册个人插件并创建开始菜单快捷方式")
    install.add_argument("--source-root", type=Path, required=True)
    overlay = sub.add_parser("overlay", help=argparse.SUPPRESS)
    overlay.add_argument("--port", type=int, required=True)
    overlay.add_argument("--app-pid", type=int, required=True)
    overlay.add_argument("--run-id", required=True)
    recovery = sub.add_parser("recovery", help=argparse.SUPPRESS)
    recovery.add_argument("--run-id", required=True)
    preview = sub.add_parser("preview", help="使用示例数据生成界面预览")
    preview.add_argument("--output", type=Path, required=True)
    preview.add_argument("--light", action="store_true")
    args = parser.parse_args()
    command = args.command or ("launch" if gui else "status")
    try:
        if command == "query":
            thread_id = args.thread_id
            if not thread_id:
                thread_id = status().get("thread_id")
            if not thread_id or not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", thread_id):
                raise ValueError("请提供有效的 --thread-id，或在 Codex 会话中运行 query")
            result = UsageService(args.codex_home).query(thread_id.lower(), not args.no_descendants)
        elif command == "launch":
            result = launch()
        elif command == "status":
            result = status()
        elif command == "stop":
            result = stop()
        elif command == "install":
            from session_model_usage.installer import install as register
            result = register(args.source_root)
        elif command == "overlay":
            from session_model_usage.overlay import run_overlay
            return run_overlay(args.port, args.app_pid, args.run_id)
        elif command == "recovery":
            from session_model_usage.recovery import run_recovery
            return run_recovery(args.run_id)
        elif command == "preview":
            from session_model_usage.overlay import preview as render
            render(args.output, not args.light)
            result = {"status": "rendered", "path": str(args.output)}
        else:
            raise ValueError("未知命令")
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        result = {"status": "restart_required" if isinstance(error, RestartRequired) else "error", "message": str(error)}
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if gui:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication([])
            QMessageBox.information(None, "Codex 会话用量", result["message"])
        return 2


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
