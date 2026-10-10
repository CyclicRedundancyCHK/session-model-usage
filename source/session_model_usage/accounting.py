from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from .metadata import TurnMetadata, fields, fast_mode
from .request_metadata import RequestMetadata


TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens")


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def parse(cls, value: Any) -> Usage | None:
        if not isinstance(value, dict):
            return None
        # A missing total is unknown, never a zero-token request.
        if not all(k in value for k in ("input_tokens", "output_tokens", "total_tokens")):
            return None
        values = [value.get(k, 0) for k in TOKEN_FIELDS]
        if any(type(v) is not int or v < 0 for v in values):
            return None
        return cls(*values)

    def vector(self) -> tuple[int, ...]:
        return tuple(getattr(self, key) for key in TOKEN_FIELDS)

    def __add__(self, other: Usage) -> Usage:
        return Usage(*(a + b for a, b in zip(self.vector(), other.vector())))

    def minus(self, other: Usage) -> Usage | None:
        values = tuple(a - b for a, b in zip(self.vector(), other.vector()))
        return Usage(*values) if all(v >= 0 for v in values) else None


@dataclass
class Entry:
    usage: Usage
    model: str | None
    turn_id: str | None
    source: str
    response_id: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    position: int = 0
    explicit_metadata: dict = field(default_factory=dict, repr=False)
    active_turn: str | None = None
    recorded_at: datetime | None = None

    @property
    def fast_mode(self):
        return fast_mode(self.service_tier)


@dataclass
class SnapshotEvent:
    cumulative: Usage
    last: Usage | None
    turn_id: str | None
    model: str | None
    position: int
    recorded_at: datetime | None
    reasoning_effort: str | None = None


@dataclass
class PrimarySignal:
    entry: Entry
    cumulative: Usage | None
    position: int
    recorded_at: datetime | None


def timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def effort(value: Any) -> str | None:
    """Keep explicit levels, including 'none'; absent metadata stays unknown."""
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def recorded_effort(data: dict) -> str | None:
    return effort(data.get("reasoning_effort", data.get("effort")))


class Rollout:
    """Incremental parser. Discards prose and keeps only accounting metadata."""

    def __init__(self, path: Path, thread_id: str, history=()):
        self.path, self.thread_id = path, thread_id
        self.history = tuple(history)
        self.history_loaded = False
        self.offset = 0
        self.pending = b""
        self.identity: tuple[int, int] | None = None
        self.created: datetime | None = None
        self.forked = False
        self.metadata = TurnMetadata()
        self.records: dict[Any, Entry] = {}
        self.primary_signals: list[PrimarySignal] = []
        self.position = 0
        self.snapshots: list[SnapshotEvent] = []
        self.warning_set: set[str] = set()
        self.meta_seen = False
        self.latest_total: Usage | None = None

    def refresh(self) -> None:
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if (self.identity is not None and identity != self.identity) or stat.st_size < self.offset:
            path, owner = self.path, self.thread_id
            self.__init__(path, owner, self.history)
        self.identity = identity
        if not self.history_loaded:
            self.history_loaded = True
            for path, limit in self.history:
                try:
                    if path.stat().st_size < limit:
                        raise OSError("history prefix truncated")
                    pending = b""
                    with path.open("rb") as handle:
                        remaining = limit
                        while remaining:
                            chunk = handle.read(min(1024 * 1024, remaining))
                            if not chunk: raise OSError("history prefix truncated")
                            remaining -= len(chunk)
                            lines = (pending + chunk).split(b"\n")
                            pending = lines.pop()
                            for line in lines: self.feed_line(line)
                    if pending:
                        self.warning_set.add("分页历史边界不是完整记录，统计不完整")
                except OSError:
                    self.warning_set.add("分页历史前缀不可读取，统计不完整")
        if stat.st_size == self.offset:
            return
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            while chunk := handle.read(1024 * 1024):
                self.offset += len(chunk)
                lines = (self.pending + chunk).split(b"\n")
                self.pending = lines.pop()
                for line in lines:
                    self.feed_line(line)

    def feed_line(self, line: bytes) -> None:
        # Never retain message text, tool arguments, credentials, or full raw rows.
        if not any(term in line for term in (b'"session_meta"', b'"turn_context"',
                                             b'"event_msg"', b'"token_usage_record"')):
            return
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            self.warning_set.add("会话日志存在无法解析的完整记录")
            return
        if not isinstance(row, dict):
            self.warning_set.add("会话日志存在无法解析的完整记录")
            return
        self.feed(row)

    def feed(self, row: dict[str, Any]) -> None:
        self.position += 1
        kind, data = row.get("type"), row.get("payload")
        if not isinstance(data, dict):
            return
        time = timestamp(row.get("timestamp"))
        if kind == "session_meta":
            # Forked logs contain additional, copied session_meta rows.
            if not self.meta_seen and data.get("id") == self.thread_id:
                self.meta_seen = True
                self.created = timestamp(data.get("timestamp")) or time
                base = data.get("history_base")
                base_parent = base.get("thread_id") if isinstance(base, dict) else None
                # Paginated forks and subagents no longer use forked_from_id.
                # Their cumulative counters can still include the parent's usage.
                self.forked = bool(base_parent) or any(isinstance(parent, str) and parent and parent != self.thread_id
                                  for parent in (data.get("forked_from_id"),
                                                 data.get("parent_thread_id"), base_parent))
                if self.forked and self.created is None:
                    self.warning_set.add("派生会话缺少创建时间，无法完整核对复制的历史")
            return
        inherited = self.forked and (time is None or self.created is None or time < self.created)
        if kind == "turn_context":
            if inherited:
                return
            turn = data.get('turn_id')
            if isinstance(turn, str):
                self.metadata.context(turn, self.position, data)
            return
        if kind == "token_usage_record":
            if inherited or data.get("thread_id") != self.thread_id:
                return
            usage = Usage.parse(data.get("usage"))
            if usage is None:
                self.warning_set.add("存在缺失或无效的请求用量")
                return
            turn = data.get("turn_id")
            self.metadata.note_request(turn, self.position)
            response = data.get("response_id")
            key = response or (turn, row.get("timestamp"), usage.vector())
            if not response:
                self.warning_set.add("部分请求缺少响应 ID，去重证据不完整")
            explicit = fields(data)
            entry = Entry(usage, explicit.get('model'), turn, "token_usage_record", response,
                          explicit.get('reasoning_effort'), explicit.get('service_tier'),
                          self.position, explicit, self.metadata.current_turn, time)
            previous = self.records.get(key)
            if previous and previous.usage != usage:
                self.warning_set.add("同一响应 ID 出现冲突用量，仅保留第一条")
            else:
                self.records.setdefault(key, entry)
                if previous:
                    for name, value in explicit.items():
                        if name not in previous.explicit_metadata:
                            previous.explicit_metadata[name] = value
                        elif previous.explicit_metadata[name] != value:
                            self.warning_set.add('同一响应 ID 出现冲突配置，仅保留第一条明确记录')
            cumulative = Usage.parse(data.get("thread_token_usage"))
            if previous is None:
                self.primary_signals.append(PrimarySignal(entry, cumulative, self.position, time))
            if cumulative:
                self.latest_total = cumulative
            return
        if kind != "event_msg" or inherited:
            return
        event = data.get("type")
        if data.get('thread_id') not in (None, self.thread_id):
            return
        if event in ('task_complete', 'task_completed', 'turn_aborted'):
            self.metadata.complete(data.get('turn_id'))
            return
        if event == "task_started":
            if isinstance(data.get("turn_id"), str):
                self.metadata.begin(data['turn_id'], self.position)
            return
        if event in ("model_rerouted", "model/rerouted"):
            self.metadata.reroute(self.position, data, data.get('turn_id'))
            return
        if event == "thread_settings_applied":
            settings = data.get("thread_settings") or data
            if isinstance(settings, dict):
                self.metadata.apply_settings(self.position, settings, data.get('turn_id'))
            return
        if event != "token_count":
            return
        info = data.get("info")
        if not isinstance(info, dict):
            return  # Rate-limit-only notifications do not represent new requests.
        cumulative = Usage.parse(info.get("total_token_usage"))
        if cumulative is None:
            return
        last = Usage.parse(info.get("last_token_usage"))
        config = self.metadata.resolve(self.metadata.current_turn, self.position)
        snap = SnapshotEvent(cumulative, last, self.metadata.current_turn, config['model'],
                             self.position, time, config['reasoning_effort'])
        self.snapshots.append(snap)
        self.latest_total = cumulative

    def entries(self) -> tuple[list[Entry], list[str]]:
        entries = [replace(entry, **self.metadata.resolve(entry.turn_id, entry.position,
                    entry.explicit_metadata, entry.active_turn)) for entry in self.records.values()]
        warnings = set(self.warning_set)
        baseline = Usage()
        seen: set[Any] = set()
        primary_index = 0
        fingerprints = [(e.cumulative.vector(), e.last.vector() if e.last else None) for e in self.snapshots]
        next_distinct = [float("inf")] * len(self.snapshots)
        for i in range(len(self.snapshots) - 2, -1, -1):
            next_distinct[i] = (self.snapshots[i + 1].position if fingerprints[i] != fingerprints[i + 1]
                                else next_distinct[i + 1])
        for index, event in enumerate(self.snapshots):
            previous_baseline = baseline
            delta = event.cumulative.minus(baseline)
            first = baseline.total_tokens == 0
            baseline = event.cumulative
            if delta is None:
                seen.clear()  # The same values in a new counter epoch are new usage.
            fingerprint = fingerprints[index]
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            # Subtract unique primary requests in this counter interval, rather
            # than equating two cumulative counters. After compaction, the old
            # notification counter and thread_token_usage can diverge permanently.
            covered = Usage()
            covered_last = False
            while primary_index < len(self.primary_signals):
                signal = self.primary_signals[primary_index]
                if signal.position > event.position:
                    # Some versions write the mirror before the primary row.
                    # Pair only the immediately following accounting interval,
                    # never a matching-sized request elsewhere in the history.
                    next_position = next_distinct[index]
                    close = (event.recorded_at is not None and signal.recorded_at is not None
                             and abs((signal.recorded_at - event.recorded_at).total_seconds()) <= 2)
                    if not (covered.total_tokens == 0 and signal.position < next_position and event.last == signal.entry.usage
                            and (signal.cumulative == event.cumulative or
                                 (close and event.turn_id == signal.entry.turn_id and event.cumulative != previous_baseline))):
                        break
                covered += signal.entry.usage
                covered_last |= event.last == signal.entry.usage
                primary_index += 1
                if signal.position > event.position:
                    break
            # Context-size notifications have no request input/output but can
            # expose a positive total. They are not a newly billed request.
            if event.last and event.last.total_tokens and not (event.last.input_tokens or event.last.output_tokens):
                continue
            if first and self.forked and event.last and event.cumulative != event.last:
                delta = event.last
                if not covered_last:
                    warnings.add("旧版派生会话存在继承基数，已排除该基数")
            if delta is None:
                if covered_last:
                    continue  # Primary usage fully covers this reset notification.
                delta = event.last
                warnings.add("旧版累计计数发生重置，已采用请求增量；历史完整性待核对")
            else:
                residual = delta.minus(covered)
                if residual is None:
                    if covered_last:
                        continue  # Compaction removed a context baseline, not usage.
                    residual = event.last
                    warnings.add("旧版计数与请求记录无法完全对齐，统计可能不完整")
                delta = residual
            if delta is None or delta.total_tokens == 0:
                continue
            if covered.total_tokens or (event.last and delta != event.last):
                # The gap has a known amount, but no per-request model attribution.
                warnings.add("旧版用量有累计缺口，缺口列为未归属")
                entries.append(Entry(delta, None, event.turn_id, "legacy_gap", position=event.position))
            else:
                config = self.metadata.resolve(event.turn_id, event.position)
                entries.append(Entry(delta, config['model'], event.turn_id, "token_count_delta",
                                     reasoning_effort=config['reasoning_effort'], service_tier=config['service_tier'],
                                     position=event.position, recorded_at=event.recorded_at))
        # Modern records can survive without token_count mirrors. Reconcile their
        # cumulative lower bound as well, so a missing request is not silently lost.
        # Compare with *all* recovered entries to avoid adding a legacy gap twice.
        previous = None
        lower_bound = Usage()
        preceding = Usage()
        interval = Usage()
        inherited_baseline = Usage()
        counter_reversed = False
        for signal in self.primary_signals:
            preceding += signal.entry.usage
            interval += signal.entry.usage
            if signal.cumulative is None:
                continue
            if previous is None:
                lower_bound = preceding if self.forked else signal.cumulative
                if self.forked:
                    inherited_baseline = signal.cumulative.minus(preceding) or Usage()
            else:
                advancement = signal.cumulative.minus(previous)
                # A decrease could be compaction, a reset, or a late historical
                # record. Do not invent an epoch and add its counter again.
                if advancement is None:
                    counter_reversed = True
                if not counter_reversed:
                    lower_bound += advancement
                else:
                    candidate = signal.cumulative.minus(inherited_baseline)
                    if candidate is not None and candidate.minus(lower_bound) is not None:
                        lower_bound = candidate
                    if advancement is not None and advancement.minus(interval) not in (None, Usage()):
                        warnings.add("请求累计计数发生回退，部分明细缺口无法完整核对")
            previous = signal.cumulative
            interval = Usage()
        accounted = Usage()
        for entry in entries:
            accounted += entry.usage
        gap = lower_bound.minus(accounted)
        if gap is not None and gap.total_tokens > 0:
            entries.append(Entry(gap, None, None, "request_counter_gap"))
            warnings.add("请求累计计数存在明细缺口，已计入总量并列为未归属")
        if not self.meta_seen:
            warnings.add("未找到匹配当前会话的元数据")
        if any(e.model is None for e in entries):
            warnings.add("部分用量无法确认模型归属")
        for entry in entries:
            u = entry.usage
            if u.total_tokens != u.input_tokens + u.output_tokens:
                warnings.add("部分日志的总 token 与输入输出分项不一致，保留原始总量")
            if u.cached_input_tokens > u.input_tokens or u.reasoning_output_tokens > u.output_tokens:
                warnings.add("部分日志的缓存或推理分项超出对应输入输出，统计不完整")
        total = sum((e.usage.total_tokens for e in entries), 0)
        if self.latest_total and not self.forked and self.latest_total.total_tokens > total:
            warnings.add("请求明细少于日志累计总量，统计可能不完整")
        return entries, sorted(warnings)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def session_header(path: Path) -> dict | None:
    """Read only a bounded header and retain no instructions or message text."""
    with path.open("rb") as handle:
        for _ in range(3):
            line = handle.readline(262145)
            if len(line) > 262144:
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("type") != "session_meta":
                continue
            data = row.get("payload")
            if not isinstance(data, dict) or not isinstance(data.get("id"), str):
                return None
            parent = data.get("parent_thread_id")
            if not parent:
                source = data.get("source")
                try:
                    if isinstance(source, str): source = json.loads(source)
                    parent = source["subagent"]["thread_spawn"]["parent_thread_id"]
                except (ValueError, KeyError, TypeError):
                    parent = None
            base = data.get("history_base")
            base = {k: base[k] for k in ("thread_id", "end_ordinal_exclusive", "end_byte_offset")
                    if k in base} if isinstance(base, dict) else None
            created = timestamp(data.get("timestamp")) or timestamp(row.get("timestamp"))
            return {"id": data["id"], "rollout_path": str(path), "history_base": base,
                    "created": created, "parent": parent if isinstance(parent, str) else None}
    return None


class UsageService:
    def __init__(self, home: Path | None = None):
        self.home = home or codex_home()
        self.parsers: dict[tuple, Rollout] = {}
        self.catalog: dict[str, dict] = {}
        self.catalog_files: dict[Path, tuple[tuple, dict | None]] = {}
        self.catalog_paths: dict[Path, dict] = {}
        self.prefix_checks: dict[tuple, bool] = {}
        self.request_metadata = RequestMetadata(self.home)

    def _history(self, path: Path, owner: str):
        try:
            header = session_header(path)
        except OSError:
            return [], []  # The regular reader will report the unreadable log.
        if not header or not header.get("history_base"):
            return [], []
        if header["id"] != owner:
            return [], ["分页日志元数据不属于当前会话，未连接历史前缀"]
        if not isinstance(header["history_base"].get("thread_id"), str):
            return [], ["分页历史缺少有效的来源会话，统计不完整"]
        # A different thread's prefix supplies context, not this thread's usage.
        if header["history_base"].get("thread_id") != owner:
            return [], []
        self._fallback(owner)  # Populate a catalog retaining all rotated paths.
        history, visited = [], {path}
        for _ in range(16):
            base = header.get("history_base")
            if not base or base.get("thread_id") != owner:
                return history, []
            limit, ordinal = base.get("end_byte_offset"), base.get("end_ordinal_exclusive")
            if type(limit) is not int or limit <= 0 or type(ordinal) is not int or ordinal <= 0:
                break
            candidates = []
            for candidate, old in self.catalog_paths.items():
                if candidate in visited or old["id"] != owner or not old["created"] or not header["created"]:
                    continue
                if old["created"] >= header["created"]:
                    continue
                try:
                    stat = candidate.stat()
                    if stat.st_size < limit: continue
                    key = (candidate, stat.st_ino, stat.st_size, stat.st_mtime_ns, limit, ordinal)
                    if key not in self.prefix_checks:
                        with candidate.open("rb") as stream:
                            stream.seek(limit - 1)
                            boundary = stream.read(1) == b"\n"
                            stream.seek(0)
                            remaining, count = limit, 0
                            while boundary and remaining:
                                chunk = stream.read(min(1024 * 1024, remaining))
                                if not chunk: break
                                remaining -= len(chunk)
                                count += chunk.count(b"\n")
                        self.prefix_checks[key] = boundary and remaining == 0 and count == ordinal
                    if self.prefix_checks[key]: candidates.append((candidate, old))
                except OSError:
                    continue
            if len(candidates) != 1:
                break
            candidate, header = candidates[0]
            visited.add(candidate)
            history.insert(0, (candidate, limit))
        return history, ["分页历史前缀无法唯一确认或边界无效，仅统计可确认记录"]

    def _index(self, thread_id: str, descendants: bool) -> tuple[list[dict[str, Any]], list[str]]:
        databases = sorted(self.home.glob("state_*.sqlite"),
                           key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1)
        warnings = []
        for database in reversed(databases):
            try:
                rows, gaps = self._read_index(database, thread_id, descendants)
                if rows:
                    return rows, warnings + gaps
                warnings.append("较新索引未包含目标会话，已尝试其他本地记录")
            except sqlite3.Error:
                warnings.append("会话索引不可读取或结构不兼容，已回退本地记录")
        if not databases and descendants:
            warnings.append("未找到会话索引，无法确认全部子智能体")
        rows = self._fallback(thread_id, descendants)
        if descendants and databases:
            warnings.append("索引不可用，子智能体仅按日志明确父子关系恢复，完整性待核对")
        return rows, warnings

    def _read_index(self, database: Path, thread_id: str, descendants: bool):
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)) as con:
            con.execute("PRAGMA query_only=ON")
            con.row_factory = sqlite3.Row
            # Keep graph and paths in one read snapshot, even while Codex writes.
            con.execute("BEGIN")
            columns = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
            if not {"id", "rollout_path"} <= columns:
                raise sqlite3.DatabaseError("unsupported threads schema")
            if con.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone() is None:
                return [], []
            optional = [k for k in ("agent_path", "source", "model") if k in columns]
            select = "id,rollout_path" + ("," + ",".join(optional) if optional else "")
            graph: dict[str, list[str]] = defaultdict(list)
            warnings: list[str] = []
            if descendants:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                sources = [name for name in ("source", "thread_source") if name in columns]
                if "thread_spawn_edges" not in tables and "parent_thread_id" not in columns:
                    warnings.append("索引未提供完整父子关系，无法确认全部子智能体")
                if "thread_spawn_edges" in tables:
                    edge_columns = {r[1] for r in con.execute("PRAGMA table_info(thread_spawn_edges)")}
                    if {"parent_thread_id", "child_thread_id"} <= edge_columns:
                        for parent, child in con.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"):
                            graph[parent].append(child)
                    else:
                        warnings.append("索引父子关系结构不兼容，子智能体统计可能不完整")
                if "parent_thread_id" in columns:
                    for child, parent in con.execute("SELECT id,parent_thread_id FROM threads WHERE parent_thread_id IS NOT NULL"):
                        graph[parent].append(child)
                # Legacy indices may express the relationship only in source metadata.
                for source_column in sources:
                    for child, source in con.execute(f"SELECT id,{source_column} FROM threads WHERE {source_column} LIKE '%thread_spawn%'"):
                        try:
                            parent = json.loads(source)["subagent"]["thread_spawn"]["parent_thread_id"]
                            graph[parent].append(child)
                        except (ValueError, KeyError, TypeError):
                            pass
                if warnings:
                    self._fallback(thread_id, True)
                    for child, header in self.catalog.items():
                        if header["parent"]:
                            graph[header["parent"]].append(child)
            queue, visited, rows = deque([thread_id]), set(), []
            while queue:
                current = queue.popleft()
                if current in visited:
                    continue
                visited.add(current)
                row = con.execute(f"SELECT {select} FROM threads WHERE id=?", (current,)).fetchone()
                if row:
                    rows.append(dict(row))
                else:
                    fallback = self._fallback(current)
                    rows.extend(fallback)
                    if not fallback:
                        rows.append({"id": current, "rollout_path": None})
                        warnings.append(f"会话 {current} 未找到本地记录")
                queue.extend(graph.get(current, []))
        return rows, warnings

    def _fallback(self, thread_id: str, descendants: bool = False) -> list[dict[str, Any]]:
        # Cache only bounded session headers. Never retain base instructions or
        # messages. A rescan still discovers newly created/archived child logs.
        catalog = {}
        catalog_paths = {}
        for folder in (self.home / "sessions", self.home / "archived_sessions"):
            if not folder.exists():
                continue
            for current, directories, files in os.walk(folder, followlinks=False):
                directories[:] = [d for d in directories if not (Path(current) / d).is_symlink()
                                  and not getattr(os.path, "isjunction", lambda _: False)(str(Path(current) / d))]
                for filename in files:
                    if filename.endswith(".jsonl"):
                        path = Path(current) / filename
                        if path.is_symlink():
                            continue
                        try:
                            stat = path.stat()
                            signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                            cached = self.catalog_files.get(path)
                            if cached and cached[0] == signature:
                                header = cached[1]
                            else:
                                header = session_header(path)
                                self.catalog_files[path] = (signature, header)
                            if header:
                                catalog_paths[path] = header
                                old = catalog.get(header["id"])
                                if old is None or (header["created"] and
                                        (not old["created"] or header["created"] > old["created"])):
                                    catalog[header["id"]] = header
                        except OSError:
                            continue
        self.catalog = catalog
        self.catalog_paths = catalog_paths
        graph = defaultdict(list)
        if descendants:
            for owner, header in catalog.items():
                if header["parent"]:
                    graph[header["parent"]].append(owner)
        queue, visited, rows = deque([thread_id]), set(), []
        while queue:
            current = queue.popleft()
            if current in visited: continue
            visited.add(current)
            if current in catalog:
                rows.append(catalog[current])
            elif current != thread_id or graph.get(current):
                rows.append({"id": current, "rollout_path": None})
            queue.extend(graph.get(current, []))
        return rows

    def query(self, thread_id: str, include_descendants: bool = True) -> dict[str, Any]:
        rows, warnings = self._index(thread_id, include_descendants)
        if not rows:
            rows = [{"id": thread_id, "rollout_path": None}]
            warnings.append("未找到该会话的本地日志")
        per_model: dict[str, dict[str, Any]] = {}
        threads = []
        totals = Usage()
        for row in rows:
            owner, raw_path = row["id"], row.get("rollout_path")
            role = "main" if owner == thread_id else "subagent"
            thread_total = Usage()
            entries: list[Entry] = []
            thread_warnings = []
            if raw_path:
                path = Path(raw_path)
                if not path.is_file():
                    # Archive moves and index updates are not atomic with reads.
                    fallback = self._fallback(owner)
                    if fallback:
                        path = Path(fallback[0]["rollout_path"])
                history, history_warnings = self._history(path, owner)
                thread_warnings.extend(history_warnings)
                parser = self.parsers.setdefault((owner, str(path), tuple(history)), Rollout(path, owner, history))
                try:
                    parser.refresh()
                    entries, parser_warnings = parser.entries()
                    thread_warnings.extend(parser_warnings)
                    thread_warnings.extend(self.request_metadata.fill(owner, entries))
                except OSError as error:
                    thread_warnings.append(f"日志不可读取：{error.strerror or type(error).__name__}")
            else:
                thread_warnings.append("该会话日志不可用")
            for entry in entries:
                thread_total += entry.usage
                name = entry.model or "unattributed"
                model = per_model.setdefault(name, {"model": name, "totals": Usage(),
                                                    "main": Usage(), "subagents": Usage(), "usage_records": 0,
                                                    "reasoning_efforts": {}, "configurations": {}})
                model["totals"] += entry.usage
                model["main" if role == "main" else "subagents"] += entry.usage
                model["usage_records"] += 1
                group = model["reasoning_efforts"].setdefault(entry.reasoning_effort,
                    {"reasoning_effort": entry.reasoning_effort, "totals": Usage(),
                     "main": Usage(), "subagents": Usage(), "usage_records": 0})
                group["totals"] += entry.usage
                group["main" if role == "main" else "subagents"] += entry.usage
                group["usage_records"] += 1
                configuration = model['configurations'].setdefault((entry.reasoning_effort, entry.service_tier),
                    {'reasoning_effort': entry.reasoning_effort, 'service_tier': entry.service_tier,
                     'fast_mode': entry.fast_mode, 'totals': Usage(), 'main': Usage(),
                     'subagents': Usage(), 'usage_records': 0})
                configuration['totals'] += entry.usage
                configuration['main' if role == 'main' else 'subagents'] += entry.usage
                configuration['usage_records'] += 1
            totals += thread_total
            threads.append({"thread_id": owner, "role": role, "agent_path": row.get("agent_path"),
                            "totals": asdict(thread_total) if entries else None,
                            "status": "partial" if thread_warnings else "complete" if entries else "pending",
                            "warnings": thread_warnings})
            warnings.extend(f"{owner}: {w}" for w in thread_warnings)
        def serialise(group: dict) -> dict:
            return {**group, **{key: asdict(group[key]) for key in ("totals", "main", "subagents")}}

        models = []
        for model in per_model.values():
            levels = [serialise(group) for group in model["reasoning_efforts"].values()]
            levels.sort(key=lambda group: (-group["totals"]["total_tokens"], group["reasoning_effort"] or ""))
            configurations = [serialise(group) for group in model['configurations'].values()]
            configurations.sort(key=lambda g: (-g['totals']['total_tokens'], g['reasoning_effort'] or '', g['service_tier'] or ''))
            models.append({**serialise(model), "reasoning_efforts": levels, 'configurations': configurations})
        models.sort(key=lambda m: (-m["totals"]["total_tokens"], m["model"]))
        return {"schema_version": 1, "thread_id": thread_id, "include_descendants": include_descendants,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": "partial" if warnings else "complete" if models else "pending",
                "totals": asdict(totals) if models else None, "models": models, "threads": threads,
                "warnings": sorted(set(warnings)),
                "attribution": "模型、推理强度与服务等级以本地请求、轮次及明确设置或重路由记录为准；JSONL 缺失的服务等级按相同会话和轮次的本地提交记录补齐；Fast 为请求设置，不代表服务端实际等级；缺失字段保留为 null"}
