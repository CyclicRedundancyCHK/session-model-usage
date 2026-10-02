from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

from .diagnostics import record


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_json(path: Path, value: dict, *, attempts: int = 4, delay: float = 0.02) -> bool:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.stem + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
        for attempt in range(attempts):
            try:
                os.replace(temporary, path)
                return True
            except OSError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(delay)
    except (OSError, ValueError, TypeError) as error:
        record("state_write_failed", error, folder=path.parent)
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class ProcessLock:
    """A Windows kernel-backed byte lock held for the process lifetime."""
    def __init__(self, path: Path):
        self.path, self.stream = path, None

    def acquire(self) -> bool:
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0)
        # Reading a byte locked by another process is itself forbidden on Windows.
        if self.path.stat().st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self.stream.close()
            self.stream = None
            return False

    def close(self) -> None:
        if self.stream is not None:
            import msvcrt
            self.stream.seek(0)
            try:
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.stream.close()
                self.stream = None
