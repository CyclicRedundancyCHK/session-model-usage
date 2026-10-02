"""Local bounded diagnostics. Never log conversation content or exception values."""
from __future__ import annotations

import faulthandler
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import time
import traceback
import threading

_loggers: dict[str, logging.Logger] = {}
_fatal_file = None
_lock = threading.RLock()


def record(event: str, error: BaseException | None = None, *, folder: Path | None = None, **fields) -> None:
    try:
        if folder is None:
            from .platform_win import state_directory
            folder = state_directory()
        folder.mkdir(parents=True, exist_ok=True)
        key = str(folder.resolve())
        if key not in _loggers:
            logger = logging.getLogger("session-usage." + key)
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = RotatingFileHandler(folder / "diagnostics.log", maxBytes=1_048_576,
                                         backupCount=4, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            _loggers[key] = logger
        value = {"time": time.time(), "event": event, **fields}
        if error is not None:
            value.update(error_type=type(error).__name__, winerror=getattr(error, "winerror", None),
                frames=[f"{Path(f.filename).name}:{f.lineno}:{f.name}"
                        for f in traceback.extract_tb(error.__traceback__)[-8:]])
        with _lock:
            logger = _loggers[key]
            logger.info(json.dumps(value, ensure_ascii=False))
            # Release Windows file handles between events, including during upgrades.
            for handler in logger.handlers:
                handler.close()
    except Exception:
        pass  # Diagnostics must never bring down the product.


def install_hooks() -> None:
    global _fatal_file
    def exception_hook(kind, value, tb):
        record("unhandled_exception", value)
    sys.excepthook = exception_hook
    try:
        from .platform_win import state_directory
        path = state_directory() / "fatal.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_048_576:
            path.write_bytes(b"")
        _fatal_file = path.open("a", encoding="utf-8")
        faulthandler.enable(_fatal_file)
    except (OSError, RuntimeError):
        pass


def diagnose() -> dict:
    from .controller import status
    from .platform_win import state_directory
    from . import __version__
    folder = state_directory()
    events = []
    try:
        for line in (folder / "diagnostics.log").read_text(encoding="utf-8").splitlines()[-25:]:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return {"version": __version__, "state": status(), "recent_events": events,
            "logs_directory": str(folder), "logs_uploaded": False}
