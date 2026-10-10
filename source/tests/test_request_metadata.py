from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from session_model_usage.accounting import UsageService
from test_accounting import ROOT, CHILD, meta, context, record, event, usage


TURN = '00000000-0000-0000-0000-000000000011'
NEXT = '00000000-0000-0000-0000-000000000012'
START = 1790640000  # 2026-09-29 00:00:00 UTC
TARGET = 'codex_core::session::handlers'


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as con:
        with con:
            yield con


def submission(owner=ROOT, turn=TURN, tier='priority', text='', start_tier='None', mode='StartOrSteer'):
    preference = 'None' if tier is None else 'Some(None)' if tier == 'clear' else f'Some(Some({json.dumps(tier)}))'
    return (f'session_loop{{thread_id={owner}}}: Submission sub=Submission {{ id: "{turn}", '
            'op: TurnInput { request: TurnInputRequest { input: UserInput { content: '
            f'[Text {{ text: {json.dumps(text)}, text_elements: [] }}], client_id: None }}, '
            f'thread_settings: ThreadSettingsOverrides {{ service_tier: {preference} }}, '
            f'start: TurnStartOptions {{ service_tier: {start_tier} }}, additional_context: {{}} }}, '
            f'mode: {mode}, reply: None }}, turn_extension_init: None }}')


class RequestMetadataTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = Path(self.folder.name)
        with database(self.home/'state_5.sqlite') as con:
            con.executescript('CREATE TABLE threads(id TEXT,rollout_path TEXT); '
                             'CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);')
        with database(self.home/'logs_2.sqlite') as con:
            con.executescript('CREATE TABLE logs(id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, '
                             'target TEXT, feedback_log_body TEXT, thread_id TEXT); '
                             'CREATE INDEX idx_logs_thread_id_ts ON logs(thread_id,ts DESC,ts_nanos DESC,id DESC);')
        self.write([meta(), context(turn=TURN, effort='xhigh'), record(turn=TURN)])
        self.service = UsageService(self.home)

    def write(self, rows, owner=ROOT):
        path = self.home/(owner+'.jsonl')
        path.write_text('\n'.join(json.dumps(r) for r in rows)+'\n', encoding='utf-8')
        with database(self.home/'state_5.sqlite') as con:
            con.execute('DELETE FROM threads WHERE id=?', (owner,))
            con.execute('INSERT INTO threads VALUES(?,?)', (owner, str(path)))
        if hasattr(self, 'service'):
            self.service = UsageService(self.home)

    def log(self, body=None, owner=ROOT, second=1, target=TARGET):
        with database(self.home/'logs_2.sqlite') as con:
            con.execute('INSERT INTO logs(ts,ts_nanos,target,feedback_log_body,thread_id) VALUES(?,?,?,?,?)',
                        (START+second, 0, target, body or submission(owner), owner))

    def config(self, service=None):
        return (service or self.service).query(ROOT)['models'][0]['configurations'][0]

    def test_new_fast_turn_uses_exact_submission_and_preserves_totals(self):
        before = self.service.query(ROOT)
        self.log()
        after = self.service.query(ROOT)
        self.assertEqual(after['totals'], before['totals'])
        self.assertEqual(after['totals']['total_tokens'], 110)
        self.assertEqual(after['models'][0]['configurations'][0]['service_tier'], 'priority')
        self.assertTrue(after['models'][0]['configurations'][0]['fast_mode'])
        self.assertEqual(after['status'], 'complete')

    def test_explicit_default_and_clear_are_ordinary(self):
        for tier in ('default', 'clear'):
            with self.subTest(tier=tier):
                with database(self.home/'logs_2.sqlite') as con:
                    con.execute('DELETE FROM logs')
                self.log(submission(tier=tier))
                self.assertEqual(self.config()['service_tier'], 'default')
                self.assertFalse(self.config()['fast_mode'])

    def test_fast_to_ordinary_switch_keeps_each_turn(self):
        self.write([meta(), context(turn=TURN, effort='xhigh'), record(turn=TURN),
                    context(turn=NEXT, second=3, effort='xhigh'),
                    record(response='r2', turn=NEXT, second=4, cumulative=usage(200,20,100,8))])
        self.log()
        self.log(submission(turn=NEXT,tier='default'), second=3)
        result = self.service.query(ROOT)
        groups = result['models'][0]['configurations']
        self.assertEqual({g['service_tier']:g['totals']['total_tokens'] for g in groups},
                         {'priority':110, 'default':110})
        self.assertEqual(result['totals']['total_tokens'], 220)

    def test_explicit_request_and_context_tiers_take_precedence(self):
        for source in ('request', 'context'):
            with self.subTest(source=source):
                rows = [meta(), context(turn=TURN), record(turn=TURN)]
                rows[2 if source=='request' else 1]['payload']['service_tier'] = 'default'
                self.write(rows)
                self.log()
                self.assertFalse(self.config()['fast_mode'])

    def test_explicit_null_request_stays_unknown(self):
        request = record(turn=TURN)
        request['payload']['service_tier'] = None
        self.write([meta(),context(turn=TURN),request])
        self.log()
        self.assertIsNone(self.config()['fast_mode'])

    def test_other_thread_turn_target_and_body_identity_are_ignored(self):
        self.log(submission(CHILD), owner=CHILD)
        self.log(submission(turn=NEXT))
        self.log(target='feedback_tags')
        self.log(submission(CHILD))
        self.assertIsNone(self.config()['fast_mode'])

    def test_quoted_user_content_cannot_inject_tier_or_submission(self):
        fake = submission(tier='priority')+' thread_settings: ThreadSettingsOverrides { service_tier: Some(Some("priority")) }'
        self.log(submission(tier='default',text=fake))
        self.assertFalse(self.config()['fast_mode'])

    def test_later_submission_is_not_used_for_earlier_request(self):
        self.log(second=3)
        self.assertIsNone(self.config()['fast_mode'])

    def test_unset_tier_does_not_inherit_current_configuration(self):
        (self.home/'config.toml').write_text('service_tier = "priority"\n')
        self.log(submission(tier=None))
        self.assertIsNone(self.config()['fast_mode'])

    def test_start_override_wins_over_thread_preference(self):
        self.log(submission(tier='priority',start_tier='Some("default")'))
        self.assertFalse(self.config()['fast_mode'])

    def test_unknown_tier_is_preserved(self):
        self.log(submission(tier='flex'))
        self.assertEqual(self.config()['service_tier'], 'flex')
        self.assertIsNone(self.config()['fast_mode'])

    def test_conflicting_submissions_remain_unknown(self):
        self.log()
        self.log(submission(tier='default'))
        result = self.service.query(ROOT)
        self.assertIsNone(result['models'][0]['configurations'][0]['fast_mode'])
        self.assertTrue(any('冲突' in warning for warning in result['warnings']))
        self.assertEqual(result['totals']['total_tokens'], 110)

    def test_duplicate_metadata_and_restart_do_not_duplicate_usage(self):
        self.log()
        self.log()
        before = self.service.query(ROOT)
        after = UsageService(self.home).query(ROOT)
        self.assertEqual(before['models'], after['models'])
        self.assertTrue(self.config()['fast_mode'])
        self.assertEqual(after['totals']['total_tokens'], 110)

    def test_legacy_request_can_be_filled_but_cumulative_gap_cannot(self):
        self.write([meta(), context(turn=TURN), event(),
                    event(cumulative=usage(300,30,150,12), second=3)])
        self.log()
        result = self.service.query(ROOT)
        known = next(m for m in result['models'] if m['model']=='gpt-6-sol')
        gap = next(m for m in result['models'] if m['model']=='unattributed')
        self.assertTrue(known['configurations'][0]['fast_mode'])
        self.assertIsNone(gap['configurations'][0]['fast_mode'])
        self.assertEqual(result['totals']['total_tokens'], 330)

    def test_child_submission_does_not_copy_parent_tier(self):
        self.write([meta(CHILD,10,ROOT),context(turn=TURN,second=11),
                    record(CHILD,turn=TURN,second=12)], CHILD)
        with database(self.home/'state_5.sqlite') as con:
            con.execute('INSERT INTO thread_spawn_edges VALUES(?,?)',(ROOT,CHILD))
        self.log()
        self.log(submission(CHILD,tier='default'),owner=CHILD,second=11)
        result = self.service.query(ROOT)
        groups = result['models'][0]['configurations']
        self.assertEqual({g['service_tier']:g['totals']['total_tokens'] for g in groups},
                         {'priority':110,'default':110})
        self.assertEqual(next(g for g in groups if g['fast_mode'])['subagents']['total_tokens'],0)

    def test_truncated_oversize_and_non_turn_submissions_are_ignored(self):
        self.log(submission()[:-10])
        self.log(submission(text='x'*300000))
        self.log(submission().replace('op: TurnInput', 'op: ThreadSettings'))
        self.log(submission(mode=f'Steer {{ expected_turn_id: "{NEXT}" }}'))
        self.assertIsNone(self.config()['fast_mode'])

    def test_corrupt_database_preserves_usage_and_reports_gap(self):
        (self.home/'logs_2.sqlite').write_bytes(b'not a database')
        result = self.service.query(ROOT)
        self.assertEqual(result['totals']['total_tokens'],110)
        self.assertIsNone(result['models'][0]['configurations'][0]['fast_mode'])
        self.assertTrue(any('Fast' in warning for warning in result['warnings']))

    def test_missing_database_is_not_created(self):
        (self.home/'logs_2.sqlite').unlink()
        self.assertIsNone(self.config()['fast_mode'])
        self.assertFalse((self.home/'logs_2.sqlite').exists())

    def test_lookup_is_read_only_and_retains_no_user_text(self):
        secret = 'private message fixture'
        self.log(submission(text=secret))
        path = self.home/'logs_2.sqlite'
        before = path.read_bytes()
        self.assertTrue(self.config()['fast_mode'])
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(secret, repr(self.service.__dict__))
        self.assertNotIn(secret, repr(self.service.request_metadata.__dict__))

    def test_lookup_row_limit_preserves_unknown_usage(self):
        self.log()
        for _ in range(256):
            self.log(submission(turn=NEXT))
        result = self.service.query(ROOT)
        self.assertIsNone(result['models'][0]['configurations'][0]['fast_mode'])
        self.assertTrue(any('上限' in warning for warning in result['warnings']))
        self.assertEqual(result['totals']['total_tokens'],110)

    def test_absent_timestamp_cannot_supply_request_attribution(self):
        request = record(turn=TURN)
        request.pop('timestamp')
        self.write([meta(),context(turn=TURN),request])
        self.log()
        self.assertIsNone(self.config()['fast_mode'])

    def test_invalid_turn_identifier_does_not_break_usage_query(self):
        self.write([meta(),context(turn=TURN),record(turn=42)])
        self.log()
        result = self.service.query(ROOT)
        self.assertEqual(result['totals']['total_tokens'],110)
        self.assertIsNone(result['models'][0]['configurations'][0]['fast_mode'])

    def test_all_models_and_efforts_support_fast_and_ordinary(self):
        models = ('gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-luna',
                  'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'future-model')
        for model in models:
            for effort in ('max', 'xhigh', 'high', 'low', 'none', None):
                for tier in ('priority', 'default'):
                    with self.subTest(model=model, effort=effort, tier=tier):
                        self.write([meta(),context(model=model,turn=TURN,effort=effort),record(turn=TURN)])
                        with database(self.home/'logs_2.sqlite') as con:
                            con.execute('DELETE FROM logs')
                        self.log(submission(tier=tier))
                        result = self.service.query(ROOT)
                        group = result['models'][0]['configurations'][0]
                        self.assertEqual(result['models'][0]['model'],model)
                        self.assertEqual(group['reasoning_effort'],effort)
                        self.assertEqual(group['fast_mode'],tier=='priority')
                        self.assertEqual(result['totals']['total_tokens'],110)

    def test_capture_retains_astra_max_after_backend_log_eviction_and_restart(self):
        self.write([meta(),context(model='gpt-6-astra',turn=TURN,effort='max'),record(turn=TURN)])
        self.log()
        result = self.service.request_metadata.capture()
        self.assertEqual(result['retained'],1)
        with database(self.home/'logs_2.sqlite') as con:
            con.execute('DELETE FROM logs')
        restored = UsageService(self.home).query(ROOT)
        group = restored['models'][0]['configurations'][0]
        self.assertEqual((restored['models'][0]['model'],group['reasoning_effort'],group['fast_mode']),
                         ('gpt-6-astra','max',True))
        self.assertEqual(restored['totals']['total_tokens'],110)

    def test_collector_captures_other_threads_without_opening_them(self):
        self.write([meta(CHILD,10,ROOT),context(model='gpt-6-astra',turn=TURN,second=11,effort='max'),
                    record(CHILD,turn=TURN,second=12)], CHILD)
        self.log(submission(CHILD),owner=CHILD,second=11)
        self.service.request_metadata.capture()
        with database(self.home/'logs_2.sqlite') as con:
            con.execute('DELETE FROM logs')
        result = UsageService(self.home).query(CHILD,False)
        self.assertTrue(result['models'][0]['configurations'][0]['fast_mode'])
        self.assertIsNone(self.config()['fast_mode'])

    def test_capture_is_incremental_and_does_not_store_body_or_model(self):
        secret = 'private prompt not for the cache'
        self.log(submission(text=secret))
        collector = self.service.request_metadata
        collector.capture()
        first = collector.cache_path.read_bytes()
        collector.capture()
        self.assertEqual(first,collector.cache_path.read_bytes())
        self.log(submission(turn=NEXT,tier='default'))
        collector.capture()
        data = json.loads(collector.cache_path.read_text())
        self.assertEqual(len(data['entries']),2)
        self.assertTrue(all(len(row)==4 for row in data['entries']))
        self.assertNotIn(secret,collector.cache_path.read_text())
        self.assertNotIn('gpt-',collector.cache_path.read_text())

    def test_capture_write_failure_is_retried_without_skipping_the_submission(self):
        self.log()
        collector = self.service.request_metadata
        with patch('session_model_usage.request_metadata.atomic_json',return_value=False):
            self.assertEqual(collector.capture()['status'],'unavailable')
        self.assertEqual(collector.last_id,0)
        self.assertEqual(collector.capture()['retained'],1)

    def test_cache_does_not_cross_turns_or_override_explicit_metadata(self):
        self.log(submission(turn=NEXT))
        self.service.request_metadata.capture()
        self.assertIsNone(self.config()['fast_mode'])
        request = record(turn=NEXT)
        request['payload']['service_tier']='default'
        self.write([meta(),context(turn=NEXT),request])
        self.assertFalse(self.config()['fast_mode'])

    def test_cache_is_bounded_and_conflicts_are_not_hidden(self):
        self.log()
        self.log(submission(tier='default'))
        collector = self.service.request_metadata
        collector.capture()
        with database(self.home/'logs_2.sqlite') as con:
            con.execute('DELETE FROM logs')
        result = self.service.query(ROOT)
        self.assertIsNone(result['models'][0]['configurations'][0]['fast_mode'])
        self.assertTrue(any('冲突' in warning for warning in result['warnings']))
        self.log(submission(turn=NEXT),second=3)
        with patch('session_model_usage.request_metadata.MAX_CACHED',1):
            self.assertEqual(collector.capture()['retained'],1)
            self.assertEqual(len(json.loads(collector.cache_path.read_text())['entries']),1)

    def test_unreadable_cache_does_not_break_query_or_create_zero_usage(self):
        collector = self.service.request_metadata
        collector.cache_path.parent.mkdir(parents=True)
        collector.cache_path.write_bytes(b'bad')
        result = self.service.query(ROOT)
        self.assertEqual(result['totals']['total_tokens'],110)
        self.assertIsNone(result['models'][0]['configurations'][0]['fast_mode'])
        self.assertTrue(any('缓存' in warning for warning in result['warnings']))

    def test_cache_boundary_cannot_turn_conflicting_metadata_into_fast(self):
        self.log()
        self.log(submission(tier='default'))
        with patch('session_model_usage.request_metadata.MAX_CACHED',1):
            self.service.request_metadata.capture()
        with database(self.home/'logs_2.sqlite') as con:
            con.execute('DELETE FROM logs')
        self.assertIsNone(self.config()['fast_mode'])


if __name__ == '__main__':
    unittest.main()
