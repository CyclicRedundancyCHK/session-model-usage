"""Private, metadata-only bridge to the combined Windows tray frontend."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from .accounting import Rollout, UsageService
from .diagnostics import record
from .platform_win import hidden_startup, state_directory, running_apps
from .runtime_io import atomic_json, read_json


def task_titles(thread_ids, home=None) -> dict[str, str]:
    """Read only requested local titles; destinations always come from lifecycle IDs."""
    if not isinstance(thread_ids, list):
        return {}
    ids = []
    for value in thread_ids[:128]:
        if isinstance(value, str):
            try:
                if str(uuid.UUID(value)) == value and value not in ids:
                    ids.append(value)
            except ValueError:
                pass
    if not ids:
        return {}
    root = Path(home) if home is not None else Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    try:
        indexes = sorted(root.glob('state_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in indexes[:2]:
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.05)) as connection:
                    columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
                    title = "COALESCE(NULLIF(trim(name), ''), title)" if 'name' in columns else 'title'
                    rows = connection.execute('SELECT id, substr(' + title + ', 1, 160) FROM threads WHERE id IN (' +
                        ','.join('?' for _ in ids) + ')', ids).fetchall()
                return {owner: title for owner, title in rows if isinstance(title, str) and title.strip()}
            except sqlite3.Error:
                continue
    except OSError:
        pass
    return {}


def recent_threads(home=None) -> dict:
    """Read at most six unarchived main chats in the local sidebar's recency order."""
    unavailable = {'status': 'unavailable', 'threads': []}
    root = Path(home) if home is not None else Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    try:
        indexes = sorted(root.glob('state_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in indexes[:2]:
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.05)) as connection:
                    columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
                    if not {'id', 'title', 'archived', 'source'} <= columns:
                        continue
                    times = [f'NULLIF(t.{name}, 0)' + (' * 1000' if not name.endswith('_ms') else '')
                             for name in ('recency_at_ms', 'recency_at', 'updated_at_ms', 'updated_at')
                             if name in columns]
                    if not times:
                        continue
                    order = times[0] if len(times) == 1 else 'COALESCE(' + ','.join(times) + ')'
                    filters = ['t.archived = 0', "(t.source IS NULL OR (t.source NOT LIKE '%\"subagent\"%' AND t.source NOT LIKE 'subagent%'))"]
                    if 'parent_thread_id' in columns:
                        filters.append("(t.parent_thread_id IS NULL OR t.parent_thread_id = '')")
                    edges = {row[1] for row in connection.execute('PRAGMA table_info(thread_spawn_edges)')}
                    if 'child_thread_id' in edges:
                        filters.append('NOT EXISTS (SELECT 1 FROM thread_spawn_edges e WHERE e.child_thread_id = t.id)')
                    title = "COALESCE(NULLIF(trim(t.name), ''), t.title)" if 'name' in columns else 't.title'
                    rows = connection.execute('SELECT t.id, substr(' + title + ', 1, 160), ' + order +
                        ' FROM threads t WHERE ' + ' AND '.join(filters) +
                        ' ORDER BY 3 DESC, t.id ASC LIMIT 128').fetchall()
                threads, seen = [], set()
                for owner, title, updated in rows:
                    try:
                        if not isinstance(owner, str) or str(uuid.UUID(owner)) != owner or owner in seen:
                            continue
                    except ValueError:
                        continue
                    seen.add(owner)
                    threads.append({'thread_id': owner, 'title': title if isinstance(title, str) else '',
                                    'updated_at': updated / 1000 if isinstance(updated, (int, float)) else None})
                    if len(threads) == 6:
                        break
                return {'status': 'complete', 'threads': threads}
            except sqlite3.Error:
                continue
    except OSError:
        pass
    return unavailable


def frontend_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "CodexUsageTray.exe"
    return Path(__file__).resolve().parents[2] / ".build" / "quota" / "CodexUsageTray.exe"


def quota_executable() -> str:
    """Prefer the CLI shipped alongside a verified running desktop app."""
    for app in running_apps():
        try:
            root = Path(app.exe()).parent
            for path in (root / "resources" / "codex.exe", root / "resources" / "bin" / "codex.exe"):
                if path.is_file():
                    return str(path)
        except (OSError, RuntimeError):
            continue
    return ""  # The frontend also discovers an installed local CLI.


def publish_usage(snapshot: dict, run_id: str, attachment_id: str | None) -> bool:
    if not attachment_id:
        return False
    return atomic_json(state_directory() / ("usage-" + attachment_id + ".json"), {
        "schema_version": 1, "run_id": run_id, "attachment_id": attachment_id,
        "captured_at": time.time(), "usage": snapshot,
    })


class ActivityService:
    """Use the same request parser for every chart; never bucket unknown gaps."""
    def __init__(self, home=None):
        self.service = UsageService(home)
        self.cancelled = threading.Event()

    def snapshot(self, now=None) -> dict:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=31)
        service = self.service
        service._fallback("")  # Refresh the bounded-header catalog, including archives.
        samples, warnings, skipped, active = [], set(), 0, set()
        owners = list(service.catalog.items())
        for owner, header in owners:
            if self.cancelled.is_set():
                break
            path = Path(header["rollout_path"])
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                    continue
                history, gaps = service._history(path, owner)
                key = (owner, str(path), tuple(history))
                active.add(key)
                parser = service.parsers.setdefault(key, Rollout(path, owner, history))
                parser.refresh()
                entries, issues = parser.entries()
                warnings.update(gaps + issues)
                for entry in entries:
                    if entry.recorded_at is None:
                        skipped += entry.usage.total_tokens
                    elif cutoff <= entry.recorded_at <= now:
                        samples.append({"timestamp": entry.recorded_at.isoformat(),
                                        "tokens": entry.usage.total_tokens})
            except OSError:
                warnings.add("部分本地活动记录暂不可读")
        service.parsers = {key: value for key, value in service.parsers.items() if key in active}
        # Bound IPC size by aggregating identical timestamps without losing tokens.
        grouped = {}
        for sample in samples:
            key = sample["timestamp"]
            grouped[key] = grouped.get(key, 0) + sample["tokens"]
        if skipped:
            warnings.add("累计缺口没有明确请求时间，未分配到活动图表")
        return {"schema_version": 1, "captured_at": now.timestamp(),
                "status": "partial" if warnings else "complete",
                "samples": [{"timestamp": k, "tokens": v} for k, v in sorted(grouped.items())],
                "unbucketed_tokens": skipped, "warnings": sorted(warnings)}


class Companion:
    """Frontend lifecycle stays separate from the Codex observer heartbeat."""
    def __init__(self, run_id: str, folder=None, home=None):
        self.run_id, self.folder = run_id, folder or state_directory()
        self.process = None
        self.next_start = 0.0
        self.failures = 0
        self.closing = False
        self.frontend_started = 0.0
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-activity")
        self.activity = ActivityService(home)
        self.metadata_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="request-metadata")
        self.metadata_pending = None
        self.next_metadata = 0.0
        self.pending = None
        self.next_activity = 0.0
        self.next_titles = 0.0

    def tick(self) -> bool:
        now = time.monotonic()
        if self.closing:
            return False
        if self.metadata_pending is not None and self.metadata_pending.done():
            try:
                self.metadata_pending.result()
            except Exception as error:
                record('request_metadata_read_failed', error, folder=self.folder)
            self.metadata_pending = None
        if self.metadata_pending is None and now >= self.next_metadata:
            self.metadata_pending = self.metadata_pool.submit(self.activity.service.request_metadata.capture)
            self.next_metadata = now + 2
        if self.process is not None and self.process.poll() is None:
            heartbeat = read_json(self.folder / 'frontend.json')
            fresh = (heartbeat.get('run_id') == self.run_id and heartbeat.get('pid') == self.process.pid
                     and isinstance(heartbeat.get('heartbeat'), (int, float))
                     and 0 <= time.time() - heartbeat['heartbeat'] < 10)
            if fresh and now >= self.next_titles:
                atomic_json(self.folder / 'task-titles.json', {'run_id': self.run_id, 'captured_at': time.time(),
                    'titles': task_titles(heartbeat.get('task_thread_ids', []))})
                atomic_json(self.folder / 'recent-threads.json', {'run_id': self.run_id, 'captured_at': time.time(),
                    **recent_threads()})
                self.next_titles = now + 2
            if not fresh and now - self.frontend_started > 15:
                record('combined_frontend_heartbeat_expired', folder=self.folder)
                try:
                    self.process.terminate()
                    self.process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if self.process is not None and self.process.poll() is not None:
            record("combined_frontend_exited", folder=self.folder)
            self.process = None
            self.failures += 1
            self.next_start = now + (1, 2, 4, 8, 15)[min(self.failures - 1, 4)]
        if self.process is None and now >= self.next_start and frontend_path().is_file():
            import psutil
            environment = {**os.environ, "SESSION_USAGE_STATE_DIR": str(self.folder),
                           "SESSION_USAGE_QUOTA_CLI": quota_executable()}
            try:
                self.process = subprocess.Popen([str(frontend_path()), "--manager-pid", str(os.getpid()),
                    "--manager-created", str(psutil.Process().create_time()), "--run-id", self.run_id],
                    env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    startupinfo=hidden_startup())
                record("combined_frontend_started", folder=self.folder)
                self.frontend_started = now
            except OSError as error:
                record("combined_frontend_start_failed", error, folder=self.folder)
                self.next_start = now + 15
        if self.pending is not None and self.pending.done():
            try:
                atomic_json(self.folder / "activity.json", self.pending.result())
            except Exception as error:
                record("activity_read_failed", error, folder=self.folder)
                atomic_json(self.folder / "activity.json", {"schema_version": 1, "captured_at": time.time(),
                    "status": "partial", "samples": [], "warnings": ["活动读取失败，稍后自动重试"]})
            self.pending = None
            self.next_activity = now + 300
        if self.pending is None and now >= self.next_activity:
            self.pending = self.pool.submit(self.activity.snapshot)
        return self.process is not None and self.process.poll() is None

    def close(self):
        self.closing = True
        self.activity.cancelled.set()
        if self.process is not None and self.process.poll() is None:
            # Only the frontend child we created; never touch Codex.
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.metadata_pool.shutdown(wait=False, cancel_futures=True)
