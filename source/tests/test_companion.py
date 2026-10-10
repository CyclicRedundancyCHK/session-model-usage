from datetime import datetime, timezone
from contextlib import closing
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from session_model_usage.companion import ActivityService, Companion, publish_usage, task_titles, recent_threads
from test_accounting import ROOT, CHILD, meta, context, record, event, usage


class TaskTitleTests(unittest.TestCase):
    def test_codex_display_name_takes_priority_over_initial_message(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(sqlite3.connect(Path(folder) / 'state_5.sqlite')) as db, db:
                db.execute('CREATE TABLE threads (id TEXT, title TEXT, name TEXT)')
                db.executemany('INSERT INTO threads VALUES (?, ?, ?)', [
                    (ROOT, '# Files mentioned by the user', '添加最近会话弹出与打开'),
                    (CHILD, '旧索引标题', '  ')])
            self.assertEqual(task_titles([ROOT, CHILD], folder), {ROOT: '添加最近会话弹出与打开', CHILD: '旧索引标题'})

    def test_titles_are_read_only_exact_requested_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state_5.sqlite'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)')
                db.executemany('INSERT INTO threads VALUES (?, ?)', [(ROOT, '目标会话'), (CHILD, '其他会话')])
            before = path.read_bytes()
            self.assertEqual(task_titles([ROOT, ROOT, "' OR 1=1 --", '../settings'], folder), {ROOT: '目标会话'})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(task_titles([], folder), {})

    def test_missing_or_broken_index_has_no_guessed_title(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(task_titles([ROOT], folder), {})
            self.assertEqual(list(Path(folder).iterdir()), [])
            (Path(folder) / 'state_5.sqlite').write_bytes(b'broken')
            self.assertEqual(task_titles([ROOT], folder), {})

    def test_input_count_and_title_length_are_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(sqlite3.connect(Path(folder) / 'state_5.sqlite')) as db, db:
                db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)')
                db.execute('INSERT INTO threads VALUES (?, ?)', (ROOT, 'x' * 1000))
            self.assertEqual(task_titles([ROOT], folder), {ROOT: 'x' * 160})
            self.assertEqual(task_titles(['bad'] * 128 + [ROOT], folder), {})


class RecentThreadTests(unittest.TestCase):
    @staticmethod
    def owner(index):
        return f'00000000-0000-0000-0000-{index:012d}'

    def test_recent_names_match_codex_and_refresh_after_rename(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state_5.sqlite'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE threads (id TEXT, title TEXT, name TEXT, source TEXT, archived INTEGER, updated_at INTEGER)')
                db.executemany('INSERT INTO threads VALUES (?, ?, ?, ?, 0, ?)', [
                    (ROOT, '# Files mentioned by the user', '添加最近会话弹出与打开', 'vscode', 2),
                    (CHILD, '旧标题', None, 'vscode', 1)])
            before = path.read_bytes()
            names = recent_threads(folder)['threads']
            self.assertEqual([row['title'] for row in names], ['添加最近会话弹出与打开', '旧标题'])
            self.assertEqual(path.read_bytes(), before)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('UPDATE threads SET name=? WHERE id=?', ('重命名后的会话', ROOT))
            self.assertEqual(recent_threads(folder)['threads'][0]['title'], '重命名后的会话')

    def test_recent_six_follow_sidebar_recency_and_exclude_archived_children(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state_5.sqlite'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, source TEXT, archived INTEGER, '
                           'recency_at_ms INTEGER, updated_at INTEGER, first_user_message TEXT)')
                db.execute('CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)')
                db.executemany('INSERT INTO threads VALUES (?, ?, ?, 0, ?, ?, ?)',
                    [(self.owner(i), '会话' + str(i), 'vscode', i * 1000, 100 - i, '正文不能进入桥接') for i in range(1, 10)])
                db.execute('INSERT INTO threads VALUES (?, ?, ?, 1, 100000, 100, ?)', (self.owner(10), '归档', 'vscode', '正文'))
                db.execute('INSERT INTO threads VALUES (?, ?, ?, 0, 100000, 100, ?)',
                    (self.owner(11), '内部', '{ "subagent" : { "other" : "guardian" } }', '正文'))
                db.execute('INSERT INTO threads VALUES (?, ?, ?, 0, 100000, 100, ?)', (self.owner(12), '子任务', 'vscode', '正文'))
                db.execute('INSERT INTO thread_spawn_edges VALUES (?, ?)', (self.owner(1), self.owner(12)))
                db.execute('INSERT INTO threads VALUES (?, ?, ?, 0, 100000, 100, ?)', ('../settings', '无效', 'vscode', '正文'))
            before = path.read_bytes()
            result = recent_threads(folder)
            self.assertEqual(result['status'], 'complete')
            self.assertEqual([r['thread_id'] for r in result['threads']], [self.owner(i) for i in range(9, 3, -1)])
            self.assertEqual(result['threads'][0], {'thread_id': self.owner(9), 'title': '会话9', 'updated_at': 9})
            self.assertNotIn('正文', json.dumps(result, ensure_ascii=False))
            self.assertEqual(path.read_bytes(), before)

    def test_older_index_orders_by_updated_time_and_filters_explicit_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(sqlite3.connect(Path(folder) / 'state_5.sqlite')) as db, db:
                db.execute('CREATE TABLE threads (id TEXT, title TEXT, source TEXT, archived INTEGER, '
                           'updated_at INTEGER, parent_thread_id TEXT)')
                db.executemany('INSERT INTO threads VALUES (?, ?, ?, 0, ?, ?)', [
                    (ROOT, 'x' * 1000, 'vscode', 8, None), (CHILD, None, 'cli', 9, ''),
                    (self.owner(3), '子任务', 'vscode', 10, ROOT)])
            result = recent_threads(folder)
            self.assertEqual([r['thread_id'] for r in result['threads']], [CHILD, ROOT])
            self.assertEqual(result['threads'][0]['title'], '')
            self.assertEqual(result['threads'][1]['title'], 'x' * 160)
            self.assertEqual(result['threads'][0]['updated_at'], 9)

    def test_missing_and_unsupported_index_are_unavailable_without_creating_files(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(recent_threads(folder), {'status': 'unavailable', 'threads': []})
            self.assertEqual(list(Path(folder).iterdir()), [])
            with closing(sqlite3.connect(Path(folder) / 'state_5.sqlite')) as db, db:
                db.execute('CREATE TABLE threads (id TEXT, title TEXT)')
            self.assertEqual(recent_threads(folder)['status'], 'unavailable')

    def test_empty_supported_index_and_missing_recency_use_update_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(sqlite3.connect(Path(folder) / 'state_5.sqlite')) as db, db:
                db.execute('CREATE TABLE threads (id TEXT, title TEXT, source TEXT, archived INTEGER, '
                           'recency_at_ms INTEGER, recency_at INTEGER, updated_at_ms INTEGER, updated_at INTEGER)')
            self.assertEqual(recent_threads(folder), {'status': 'complete', 'threads': []})
            with closing(sqlite3.connect(Path(folder) / 'state_5.sqlite')) as db, db:
                db.executemany('INSERT INTO threads VALUES (?, ?, ?, 0, ?, ?, ?, ?)', [
                    (ROOT, '一', 'vscode', 0, None, 8000, 1), (CHILD, '二', 'vscode', None, 9, 5000, 5)])
            self.assertEqual([r['thread_id'] for r in recent_threads(folder)['threads']], [CHILD, ROOT])

    def test_broken_newer_index_falls_back_to_readable_index(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(sqlite3.connect(Path(folder) / 'state_4.sqlite')) as db, db:
                db.execute('CREATE TABLE threads (id TEXT, title TEXT, source TEXT, archived INTEGER, updated_at INTEGER)')
                db.execute('INSERT INTO threads VALUES (?, ?, ?, 0, 10)', (ROOT, '目标', 'vscode'))
            newer = Path(folder) / 'state_5.sqlite'
            newer.write_bytes(b'broken')
            os.utime(newer, (2000000000, 2000000000))
            self.assertEqual(recent_threads(folder)['threads'][0]['thread_id'], ROOT)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.folder = self.home / 'sessions'
        self.folder.mkdir()
        self.now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    def tearDown(self):
        self.temp.cleanup()
    def write(self, name, rows, folder=None):
        path = (folder or self.folder) / name
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
        return path
    def test_modern_and_mirror_activity_deduplicates_and_preserves_request_time(self):
        self.write('main.jsonl', [meta(), context(), record(), event(), record()])
        result = ActivityService(self.home).snapshot(self.now)
        self.assertEqual(sum(row['tokens'] for row in result['samples']), 110)
        self.assertEqual(result['samples'][0]['timestamp'], '2026-09-29T00:00:02+00:00')
        self.assertEqual(result['status'], 'complete')
    def test_gap_is_preserved_without_inventing_a_chart_time(self):
        self.write('main.jsonl', [meta(), context(), record(cumulative=usage(300, 30, 150, 12))])
        result = ActivityService(self.home).snapshot(self.now)
        self.assertEqual(sum(row['tokens'] for row in result['samples']), 110)
        self.assertEqual(result['unbucketed_tokens'], 220)
        self.assertEqual(result['status'], 'partial')
    def test_child_excludes_inherited_usage_and_archived_requests_are_included(self):
        archived = self.home / 'archived_sessions'; archived.mkdir()
        header = meta(CHILD, second=10); header['payload']['parent_thread_id'] = ROOT
        self.write('child.jsonl', [header, context(CHILD, second=1), record(CHILD, second=2),
            context(CHILD, second=11), record(CHILD, second=12)], archived)
        result = ActivityService(self.home).snapshot(self.now)
        self.assertEqual(sum(row['tokens'] for row in result['samples']), 110)
    def test_incremental_activity_replaces_duplicate_read_and_adds_new_request(self):
        path = self.write('main.jsonl', [meta(), context(), record()])
        service = ActivityService(self.home)
        first = service.snapshot(self.now)
        self.assertEqual(service.snapshot(self.now)['samples'], first['samples'])
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record(response='next', cumulative=usage(200, 20, 100, 8), second=4)) + '\n')
        self.assertEqual(sum(row['tokens'] for row in service.snapshot(self.now)['samples']), 220)
    def test_legacy_delta_is_dated_but_legacy_gap_is_not(self):
        self.write('main.jsonl', [meta(), context(), event()])
        result = ActivityService(self.home).snapshot(self.now)
        self.assertEqual(sum(row['tokens'] for row in result['samples']), 110)
        self.assertEqual(result['unbucketed_tokens'], 0)
    def test_window_filters_future_request_and_missing_time(self):
        row = record(); row['timestamp'] = '2026-10-30T00:00:00Z'
        self.write('main.jsonl', [meta(), context(), row])
        self.assertEqual(ActivityService(self.home).snapshot(self.now)['samples'], [])


class BridgeLifecycleTests(unittest.TestCase):
    def test_metadata_capture_runs_without_current_chat_or_fresh_frontend(self):
        with tempfile.TemporaryDirectory() as folder:
            companion = Companion('run', Path(folder), home=Path(folder))
            companion.next_activity = float('inf')
            companion.process = Mock(pid=123)
            companion.process.poll.return_value = None
            try:
                with patch.object(companion.activity.service.request_metadata, 'capture', return_value={'status':'complete'}) as capture, \
                     patch('session_model_usage.companion.read_json',return_value={}):
                    companion.tick()
                    companion.metadata_pending.result(timeout=2)
                    companion.tick()
                    self.assertEqual(capture.call_count,1)
            finally:
                companion.process.poll.return_value = 0
                companion.close()

    def test_closed_companion_does_not_capture_or_restart_workers(self):
        with tempfile.TemporaryDirectory() as folder:
            companion = Companion('run',Path(folder),home=Path(folder))
            companion.close()
            with patch.object(companion.activity.service.request_metadata,'capture') as capture:
                self.assertFalse(companion.tick())
                capture.assert_not_called()

    def test_recent_list_is_published_only_for_a_fresh_frontend(self):
        with tempfile.TemporaryDirectory() as folder:
            companion = Companion('run', Path(folder), home=Path(folder))
            companion.next_activity = float('inf')
            companion.process = Mock(pid=123)
            companion.process.poll.return_value = None
            heartbeat = {'run_id': 'run', 'pid': 123, 'heartbeat': time.time()}
            with patch('session_model_usage.companion.read_json', return_value=heartbeat), \
                 patch('session_model_usage.companion.task_titles', return_value={}), \
                 patch('session_model_usage.companion.recent_threads', return_value={'status': 'complete', 'threads': []}) as read:
                companion.tick()
                cached = json.loads((Path(folder) / 'recent-threads.json').read_text(encoding='utf-8'))
                self.assertEqual(cached['run_id'], 'run')
                self.assertEqual(cached['status'], 'complete')
                heartbeat['run_id'] = 'old-run'
                companion.next_titles = 0
                companion.tick()
                self.assertEqual(read.call_count, 1)
            companion.process.poll.return_value = 0
            companion.close()

    def test_bridge_envelope_contains_exact_run_attachment_and_metadata_only_usage(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'SESSION_USAGE_STATE_DIR': folder}):
            self.assertTrue(publish_usage({'thread_id': ROOT, 'totals': {'total_tokens': 10}}, 'run', 'attachment'))
            value = json.loads((Path(folder) / 'usage-attachment.json').read_text(encoding='utf-8'))
        self.assertEqual(value['run_id'], 'run')
        self.assertEqual(value['attachment_id'], 'attachment')
        self.assertEqual(value['usage']['thread_id'], ROOT)
    def test_unmanaged_overlay_does_not_write_a_shared_snapshot(self):
        self.assertFalse(publish_usage({}, 'run', None))
    def test_companion_restart_never_starts_or_terminates_codex(self):
        with tempfile.TemporaryDirectory() as folder, patch('session_model_usage.companion.frontend_path', return_value=Path(folder)/'missing.exe'):
            companion = Companion('run', Path(folder), home=Path(folder))
            companion.next_activity = float('inf')
            child = Mock(); child.poll.return_value = 1
            companion.process = child
            self.assertFalse(companion.tick())
            self.assertEqual(companion.failures, 1)
            child.terminate.assert_not_called()
            companion.close()

if __name__ == '__main__': unittest.main()
