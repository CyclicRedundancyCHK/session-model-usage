from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any


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

    def __init__(self, path: Path, thread_id: str):
        self.path, self.thread_id = path, thread_id
        self.offset = 0
        self.pending = b""
        self.identity: tuple[int, int] | None = None
        self.created: datetime | None = None
        self.forked = False
        self.current_turn: str | None = None
        self.current_model: str | None = None
        self.current_effort: str | None = None
        self.turn_models: dict[str, str] = {}
        self.turn_efforts: dict[str, str | None] = {}
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
            self.__init__(path, owner)
        self.identity = identity
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
                self.forked = bool(data.get("forked_from_id"))
                if self.forked and self.created is None:
                    self.warning_set.add("派生会话缺少创建时间，无法完整核对复制的历史")
            return
        inherited = self.forked and (time is None or self.created is None or time < self.created)
        if kind == "turn_context":
            if inherited:
                return
            turn, model = data.get("turn_id"), data.get("model")
            if isinstance(turn, str):
                self.current_turn = turn
            if isinstance(model, str) and model:
                self.current_model = model
                if turn:
                    self.turn_models[turn] = model
            else:
                self.current_model = None
            self.current_effort = recorded_effort(data)
            if isinstance(turn, str):
                self.turn_efforts[turn] = self.current_effort
            return
        if kind == "token_usage_record":
            if data.get("thread_id") != self.thread_id:
                return
            usage = Usage.parse(data.get("usage"))
            if usage is None:
                self.warning_set.add("存在缺失或无效的请求用量")
                return
            turn = data.get("turn_id")
            response = data.get("response_id")
            key = response or (turn, row.get("timestamp"), usage.vector())
            if not response:
                self.warning_set.add("部分请求缺少响应 ID，去重证据不完整")
            model = data.get("model") or (self.current_model if turn == self.current_turn else self.turn_models.get(turn))
            if not isinstance(model, str) or not model:
                model = None
            level = (recorded_effort(data) if "reasoning_effort" in data or "effort" in data else
                     self.current_effort if turn == self.current_turn else self.turn_efforts.get(turn))
            entry = Entry(usage, model, turn, "token_usage_record", response, level)
            previous = self.records.get(key)
            if previous and previous.usage != usage:
                self.warning_set.add("同一响应 ID 出现冲突用量，仅保留第一条")
            else:
                self.records.setdefault(key, entry)
            cumulative = Usage.parse(data.get("thread_token_usage"))
            if previous is None:
                self.primary_signals.append(PrimarySignal(entry, cumulative, self.position, time))
            if cumulative:
                self.latest_total = cumulative
            return
        if kind != "event_msg" or inherited:
            return
        event = data.get("type")
        if event == "task_started":
            if isinstance(data.get("turn_id"), str):
                self.current_turn = data["turn_id"]
                # Do not carry a model from another turn before its context arrives.
                self.current_model = self.turn_models.get(self.current_turn)
                self.current_effort = self.turn_efforts.get(self.current_turn)
            return
        if event in ("model_rerouted", "model/rerouted"):
            model = data.get("to_model") or data.get("toModel")
            if isinstance(model, str) and model:
                self.current_model = model
                if self.current_turn:
                    self.turn_models[self.current_turn] = model
            if "reasoning_effort" in data or "effort" in data:
                self.current_effort = recorded_effort(data)
                if self.current_turn:
                    self.turn_efforts[self.current_turn] = self.current_effort
            return
        if event == "thread_settings_applied":
            settings = data.get("thread_settings") or data
            if isinstance(settings, dict) and isinstance(settings.get("model"), str):
                self.current_model = settings["model"]
                self.current_effort = recorded_effort(settings)
                if self.current_turn:
                    self.turn_models[self.current_turn] = self.current_model
                    self.turn_efforts[self.current_turn] = self.current_effort
        if event != "token_count":
            return
        info = data.get("info")
        if not isinstance(info, dict):
            return  # Rate-limit-only notifications do not represent new requests.
        cumulative = Usage.parse(info.get("total_token_usage"))
        if cumulative is None:
            return
        last = Usage.parse(info.get("last_token_usage"))
        snap = SnapshotEvent(cumulative, last, self.current_turn, self.current_model,
                             self.position, time, self.current_effort)
        self.snapshots.append(snap)
        self.latest_total = cumulative

    def entries(self) -> tuple[list[Entry], list[str]]:
        entries = list(self.records.values())
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
                entries.append(Entry(delta, None, event.turn_id, "legacy_gap"))
            else:
                entries.append(Entry(delta, event.model, event.turn_id, "token_count_delta",
                                     reasoning_effort=event.reasoning_effort))
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


class UsageService:
    def __init__(self, home: Path | None = None):
        self.home = home or codex_home()
        self.parsers: dict[tuple[str, str], Rollout] = {}

    def _index(self, thread_id: str, descendants: bool) -> tuple[list[dict[str, Any]], list[str]]:
        databases = sorted(self.home.glob("state_*.sqlite"),
                           key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1)
        if not databases:
            return self._fallback(thread_id), ["未找到会话索引，无法确认全部子智能体"] if descendants else []
        with closing(sqlite3.connect(databases[-1].resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)) as con:
            con.execute("PRAGMA query_only=ON")
            con.row_factory = sqlite3.Row
            columns = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
            optional = [k for k in ("agent_path", "source", "model") if k in columns]
            select = "id,rollout_path" + ("," + ",".join(optional) if optional else "")
            graph: dict[str, list[str]] = defaultdict(list)
            warnings: list[str] = []
            if descendants:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "thread_spawn_edges" not in tables and "source" not in columns:
                    warnings.append("索引未提供父子关系，无法确认全部子智能体")
                if "thread_spawn_edges" in tables:
                    for parent, child in con.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"):
                        graph[parent].append(child)
                # Legacy indices may express the relationship only in source metadata.
                if "source" in columns:
                    for child, source in con.execute("SELECT id,source FROM threads WHERE source LIKE '%thread_spawn%'"):
                        try:
                            parent = json.loads(source)["subagent"]["thread_spawn"]["parent_thread_id"]
                            graph[parent].append(child)
                        except (ValueError, KeyError, TypeError):
                            pass
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

    def _fallback(self, thread_id: str) -> list[dict[str, Any]]:
        for folder in (self.home / "sessions", self.home / "archived_sessions"):
            if not folder.exists():
                continue
            for current, directories, files in os.walk(folder, followlinks=False):
                directories[:] = [d for d in directories if not (Path(current) / d).is_symlink()
                                  and not getattr(os.path, "isjunction", lambda _: False)(str(Path(current) / d))]
                for filename in files:
                    if thread_id in filename and filename.endswith(".jsonl"):
                        path = Path(current) / filename
                        # Verify ownership rather than trusting a filename containing two IDs.
                        with path.open("rb") as handle:
                            for _ in range(3):
                                try:
                                    row = json.loads(handle.readline())
                                except ValueError:
                                    continue
                                if row.get("type") == "session_meta":
                                    if row.get("payload", {}).get("id") == thread_id:
                                        return [{"id": thread_id, "rollout_path": str(path)}]
                                    break
        return []

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
                parser = self.parsers.setdefault((owner, str(path)), Rollout(path, owner))
                try:
                    parser.refresh()
                    entries, thread_warnings = parser.entries()
                except OSError as error:
                    thread_warnings.append(f"日志不可读取：{error.strerror or type(error).__name__}")
            else:
                thread_warnings.append("该会话日志不可用")
            for entry in entries:
                thread_total += entry.usage
                name = entry.model or "unattributed"
                model = per_model.setdefault(name, {"model": name, "totals": Usage(),
                                                    "main": Usage(), "subagents": Usage(), "usage_records": 0,
                                                    "reasoning_efforts": {}})
                model["totals"] += entry.usage
                model["main" if role == "main" else "subagents"] += entry.usage
                model["usage_records"] += 1
                group = model["reasoning_efforts"].setdefault(entry.reasoning_effort,
                    {"reasoning_effort": entry.reasoning_effort, "totals": Usage(),
                     "main": Usage(), "subagents": Usage(), "usage_records": 0})
                group["totals"] += entry.usage
                group["main" if role == "main" else "subagents"] += entry.usage
                group["usage_records"] += 1
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
            models.append({**serialise(model), "reasoning_efforts": levels})
        models.sort(key=lambda m: (-m["totals"]["total_tokens"], m["model"]))
        return {"schema_version": 1, "thread_id": thread_id, "include_descendants": include_descendants,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": "partial" if warnings else "complete" if models else "pending",
                "totals": asdict(totals) if models else None, "models": models, "threads": threads,
                "warnings": sorted(set(warnings)),
                "attribution": "模型和推理强度以本地请求、轮次及明确的设置或重路由记录为准；缺失强度保留为 null"}
