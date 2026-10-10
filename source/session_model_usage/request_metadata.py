"""Bounded, read-only fallback for tiers omitted from modern rollout JSONL.

Only a structured TurnInput submission with the exact thread and turn IDs is
evidence. User text, current preferences and unrelated tracing are never used.
No raw log bodies are retained in the accounting service or bridge.
"""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import re
import sqlite3
import json
import time

from .runtime_io import atomic_json


UUID = r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}'
PREFIX = re.compile(r'^session_loop\{thread_id=(' + UUID + r')\}: Submission sub=')
IDENTIFIER = re.compile(r'"(' + UUID + r')"\Z')
TIER = re.compile(r'Some\((?:Some\()?"([a-zA-Z0-9_-]{1,32})"\)(?:\))?\Z')
MAX_BODY = 262144
MAX_ROWS = 256
MAX_BYTES = 2 * 1024 * 1024
MAX_CACHED = 8192
TARGET = 'codex_core::session::handlers'


def debug_fields(value, name, wanted):
    """Split only top-level Rust Debug fields; quoted nested text is opaque."""
    prefix = name + ' {'
    if not value.startswith(prefix) or not value.endswith('}'):
        return {}
    result, stack = {}, []
    quoted = escaped = False
    start = len(prefix)
    content = value[start:-1]
    field_start = 0
    for index, char in enumerate(content + ','):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in '{[(':
            stack.append(char)
        elif char in '}])':
            if not stack or stack.pop() != {'}': '{', ']': '[', ')': '('}[char]:
                return {}
        elif char == ',' and not stack:
            field = content[field_start:index].strip()
            key, separator, data = field.partition(':')
            if separator and key.strip() in wanted:
                key = key.strip()
                if key in result:
                    return {}
                result[key] = data.strip()
            field_start = index + 1
    return {} if quoted or stack else result


def tier_value(value):
    if value == 'Some(None)':
        return 'default'  # An explicit reset, rather than an omitted override.
    if not value:
        return None
    # Require balanced Option wrappers, not an arbitrary string match.
    match = TIER.fullmatch(value)
    if match and value.count('(') == value.count(')'):
        return match[1].lower()
    return None


def submission_tier(body, owner):
    if not isinstance(body, str) or len(body) > MAX_BODY:
        return None
    prefix = PREFIX.match(body)
    if not prefix or prefix[1].lower() != owner.lower():
        return None
    submission = debug_fields(body[prefix.end():], 'Submission', ('id', 'op'))
    identifier = IDENTIFIER.fullmatch(submission.get('id', ''))
    operation = debug_fields(submission.get('op', ''), 'TurnInput', ('request', 'mode'))
    # Steering IDs identify an input message, not the running turn.
    if not identifier or operation.get('mode') not in ('StartOrSteer', 'Start'):
        return None
    request = debug_fields(operation.get('request', ''), 'TurnInputRequest', ('thread_settings', 'start'))
    settings = debug_fields(request.get('thread_settings', ''), 'ThreadSettingsOverrides', ('service_tier',))
    start = debug_fields(request.get('start', ''), 'TurnStartOptions', ('service_tier',))
    # A present, unsupported start override cannot fall back to another tier.
    override = start.get('service_tier')
    tier = tier_value(override if override not in (None, 'None') else settings.get('service_tier'))
    return (identifier[1].lower(), tier) if tier else None


class RequestMetadata:
    def __init__(self, home):
        self.path = home/'logs_2.sqlite'
        self.cache_path = home/'cache/session-model-usage/request-tiers.json'
        self.last_id = 0
        self.identity = None

    def cached(self):
        """Accept only bounded, metadata-only rows from the private cache."""
        try:
            with self.cache_path.open('rb') as stream:
                raw = stream.read(MAX_BYTES+1)
            if len(raw) > MAX_BYTES:
                return [], ['Fast 请求设置缓存超限，保留未记录']
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('entries'), list):
                return [], ['Fast 请求设置缓存无效，保留未记录']
            rows = []
            for row in data['entries'][:MAX_CACHED]:
                if not isinstance(row, list) or len(row) != 4:
                    continue
                owner, turn, tier, seconds = row
                if not all(isinstance(v, str) for v in (owner, turn, tier)):
                    continue
                if not re.fullmatch(UUID, owner) or not re.fullmatch(UUID, turn) or not re.fullmatch(r'[a-z0-9_-]{1,32}', tier):
                    continue
                if type(seconds) not in (int, float) or not 0 <= seconds <= time.time()+120:
                    continue
                rows.append([owner.lower(), turn.lower(), tier, seconds])
            return rows, []
        except FileNotFoundError:
            return [], []
        except (OSError, ValueError):
            return [], ['Fast 请求设置缓存不可读取，保留未记录']

    def capture(self):
        """Collect all models' explicit turn tiers before backend log eviction."""
        if not self.path.is_file():
            return {'status': 'unavailable'}
        stat = self.path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity != self.identity:
            self.identity, self.last_id = identity, 0
        retained, cache_warnings = self.cached()
        if cache_warnings or not self.cache_path.is_file():
            self.last_id = 0
        # The cache stores IDs, service tier and submission time only.
        unique = {tuple(row): row for row in retained}
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)) as con:
                latest = con.execute('SELECT max(id) FROM logs').fetchone()[0] or 0
                if latest < self.last_id:
                    self.last_id = 0
                if latest == self.last_id:
                    return {'status': 'complete', 'retained': len(unique)}
                initial = self.last_id == 0
                rows = con.execute('SELECT id,thread_id,ts,ts_nanos,substr(feedback_log_body,1,?) FROM logs '
                    'WHERE id>? AND id<=? AND target=? ORDER BY id '+('DESC' if initial else 'ASC')+' LIMIT ?',
                    (MAX_BODY+1, self.last_id, latest, TARGET, MAX_ROWS+1))
                consumed, cursor, exhausted = 0, self.last_id, True
                for index, (ident, owner, seconds, nanos, body) in enumerate(rows):
                    consumed += len(body.encode('utf-8')) if isinstance(body, str) else 0
                    if index == MAX_ROWS or consumed > MAX_BYTES:
                        exhausted = False
                        break
                    cursor = max(cursor, ident)
                    if not isinstance(owner, str) or not re.fullmatch(UUID, owner):
                        continue
                    parsed = submission_tier(body, owner)
                    if parsed is None or type(seconds) is not int or type(nanos) is not int or not 0 <= nanos < 1_000_000_000:
                        continue
                    stamp = seconds+nanos/1_000_000_000
                    if not 0 <= stamp <= time.time()+120:
                        continue
                    row = [owner.lower(), parsed[0], parsed[1], stamp]
                    unique[tuple(row)] = row
                next_id = latest if initial or exhausted else cursor
        except (sqlite3.Error, OSError):
            return {'status': 'unavailable'}
        # Never keep only half of a conflicting turn at the eviction boundary.
        groups = defaultdict(list)
        for row in sorted(unique.values(), key=lambda row: row[3], reverse=True):
            groups[tuple(row[:2])].append(row)
        kept = []
        for group in groups.values():
            if len(kept)+len(group) <= MAX_CACHED:
                kept.extend(group)
        if kept != retained:
            if not atomic_json(self.cache_path, {'schema_version': 1, 'entries': kept}):
                return {'status': 'unavailable'}
        self.last_id = next_id
        return {'status': 'complete' if exhausted else 'partial', 'retained': len(kept)}

    def fill(self, owner, entries):
        pending = [e for e in entries if e.service_tier is None and isinstance(e.turn_id, str) and e.turn_id and e.recorded_at
                   and 'service_tier' not in e.explicit_metadata
                   and e.source in ('token_usage_record', 'token_count_delta')]
        if not pending:
            return []
        turns = {e.turn_id.lower() for e in pending}
        evidence = defaultdict(list)
        cached, cache_warnings = self.cached()
        warnings = set(cache_warnings)
        for cached_owner, turn, tier, seconds in cached:
            if cached_owner == owner.lower() and turn in turns:
                evidence[turn].append((datetime.fromtimestamp(seconds, timezone.utc), tier))
        try:
            # The thread/time index keeps this independent of the full log size.
            # Each body, the row count and the total bytes have separate limits.
            if not self.path.is_file():
                raise FileNotFoundError
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)) as con:
                rows = con.execute('SELECT ts,ts_nanos,substr(feedback_log_body,1,?),id FROM logs '
                    'WHERE thread_id=? AND target=? ORDER BY ts DESC,ts_nanos DESC,id DESC LIMIT ?',
                    (MAX_BODY+1, owner, TARGET, MAX_ROWS+1))
                consumed = 0
                for index, (seconds, nanos, body, _) in enumerate(rows):
                    consumed += len(body.encode('utf-8')) if isinstance(body, str) else 0
                    if index == MAX_ROWS or consumed > MAX_BYTES:
                        warnings.add('Fast 请求设置读取达到本地日志上限，部分记录可能缺少归属')
                        break
                    parsed = submission_tier(body, owner)
                    if parsed is None or parsed[0] not in turns:
                        continue
                    if type(seconds) is not int or type(nanos) is not int or not 0 <= nanos < 1_000_000_000:
                        continue
                    try:
                        stamp = datetime.fromtimestamp(seconds+nanos/1_000_000_000, timezone.utc)
                    except (ValueError, OverflowError, OSError):
                        continue
                    evidence[parsed[0]].append((stamp, parsed[1]))
        except FileNotFoundError:
            pass
        except (sqlite3.Error, OSError):
            warnings.add('Fast 请求设置日志不可读取，缺失的设置保留为未记录')
        for entry in pending:
            tiers = {tier for stamp, tier in evidence[entry.turn_id.lower()] if stamp <= entry.recorded_at}
            if len(tiers) == 1:
                entry.service_tier = tiers.pop()
            elif len(tiers) > 1:
                warnings.add('同一轮次的 Fast 请求设置存在冲突，缺失的设置保留为未记录')
        return sorted(warnings)
