import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from session_model_usage.accounting import Rollout, UsageService
from session_model_usage.metadata import fast_mode
from test_accounting import ROOT, CHILD, row, meta, context, record, event, totals


def settings(model='gpt-6-sol', level='high', tier='priority', second=1):
    return row('event_msg', {'type': 'thread_settings_applied', 'thread_settings': {
        'model': model, 'reasoning_effort': level, 'service_tier': tier}}, second)


def start(turn='turn-1', second=1):
    return row('event_msg', {'type': 'task_started', 'turn_id': turn}, second)


class MetadataTests(unittest.TestCase):
    def parse(self, rows, owner=ROOT):
        parser = Rollout(Path('unused.jsonl'), owner)
        for value in rows:
            parser.feed(value)
        return parser

    def test_usage_before_context_is_backfilled_without_changing_total(self):
        parser = self.parse([meta(), start(), record()])
        before = totals(parser)
        self.assertIsNone(before[0][0].model)
        parser.feed(context(effort='max', second=3))
        after = totals(parser)
        self.assertEqual(after[0][0].model, 'gpt-6-sol')
        self.assertEqual(after[0][0].reasoning_effort, 'max')
        self.assertEqual(before[1], after[1])
        self.assertFalse(after[2])

    def test_incremental_late_context_and_restart_match(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'rollout.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in [meta(), start(), record()])+'\n')
            parser = Rollout(path, ROOT); parser.refresh()
            with path.open('a') as stream:
                stream.write(json.dumps(context(effort='max', second=3))+'\n')
            parser.refresh(); parser.refresh()
            restored = Rollout(path, ROOT); restored.refresh()
            self.assertEqual(totals(parser), totals(restored))
            self.assertEqual(totals(parser)[0][0].model, 'gpt-6-sol')

    def test_request_metadata_overrides_later_context(self):
        request = record()
        request['payload'].update(model='gpt-6-astra', reasoning_effort='none', service_tier='default')
        parser = self.parse([meta(), start(), request, context(effort='max', second=3)])
        entry = totals(parser)[0][0]
        self.assertEqual((entry.model, entry.reasoning_effort, entry.fast_mode), ('gpt-6-astra', 'none', False))

    def test_explicit_null_is_not_replaced_by_future_context(self):
        request = record(); request['payload']['reasoning_effort'] = None
        parser = self.parse([meta(), start(), request, context(effort='max', second=3)])
        self.assertIsNone(totals(parser)[0][0].reasoning_effort)

    def test_model_cannot_be_backfilled_across_reroute(self):
        parser = self.parse([meta(), start(), record(),
            row('event_msg', {'type':'model_rerouted', 'to_model':'gpt-6-astra'}, 3),
            context('gpt-6-astra', effort='max', second=4)])
        self.assertIsNone(totals(parser)[0][0].model)

    def test_conflicting_initial_settings_keep_attribution_unknown(self):
        parser = self.parse([meta(), start(), record(), settings('gpt-6-astra', second=3),
                             context('gpt-6-sol', effort='max', second=4)])
        self.assertIsNone(totals(parser)[0][0].model)

    def test_completed_turn_is_not_changed_by_next_turn_settings(self):
        parser = self.parse([meta(), settings(), start(), context(effort='high'), record(),
            row('event_msg', {'type':'task_complete', 'turn_id':'turn-1'}, 3),
            settings('gpt-6-astra', 'max', 'default', 4), start('turn-2', 5),
            context('gpt-6-astra', 'turn-2', 5, 'max'),
            record(response='late', turn='turn-1', second=6)])
        entry = totals(parser)[0][-1]
        self.assertEqual((entry.model, entry.reasoning_effort, entry.service_tier), ('gpt-6-sol', 'high', 'priority'))

    def test_late_response_with_multiple_models_is_unattributed(self):
        parser = self.parse([meta(), context(),
            row('event_msg', {'type':'model_rerouted','to_model':'gpt-6-astra'}, 3),
            context('gpt-6-luna', 'turn-2', 4), record(turn='turn-1', second=5)])
        self.assertIsNone(totals(parser)[0][0].model)

    def test_late_old_context_does_not_steal_active_settings(self):
        parser = self.parse([meta(), record(), context('gpt-6-astra','turn-2',3,'max'),
            context('gpt-6-sol','turn-1',4,'high'), settings('gpt-6-astra','low','priority',5),
            record(response='r2',turn='turn-2',second=6)])
        first, second = totals(parser)[0]
        self.assertEqual(first.model,'gpt-6-sol')
        self.assertEqual((second.model,second.reasoning_effort,second.fast_mode),('gpt-6-astra','low',True))

    def test_context_without_tier_preserves_confirmed_fast_setting(self):
        parser = self.parse([meta(), settings(), start(), context(effort='high'), record()])
        entry = totals(parser)[0][0]
        self.assertEqual(entry.service_tier, 'priority')
        self.assertTrue(entry.fast_mode)

    def test_future_fast_toggle_does_not_fill_older_unknown_request(self):
        parser = self.parse([meta(), start(), record(), settings(second=3), context(effort='high', second=4)])
        self.assertIsNone(totals(parser)[0][0].service_tier)

    def test_fast_toggle_keeps_per_request_history(self):
        parser = self.parse([meta(), settings(), start(), context(effort='high'), record(),
            settings(tier='default', second=3), record(response='r2', second=4)])
        self.assertEqual([e.fast_mode for e in totals(parser)[0]], [True, False])

    def test_duplicate_notification_can_supply_missing_request_metadata(self):
        repeated = record(); repeated['payload'].update(model='gpt-6-astra', service_tier='priority')
        parser = self.parse([meta(), record(), repeated, repeated])
        entries, total, _ = totals(parser)
        self.assertEqual(len(entries), 1)
        self.assertEqual(total.total_tokens, 110)
        self.assertEqual(entries[0].model, 'gpt-6-astra')
        self.assertTrue(entries[0].fast_mode)

    def test_conflicting_duplicate_does_not_replace_explicit_metadata(self):
        first = record(); first['payload']['service_tier'] = 'default'
        repeated = record(); repeated['payload']['service_tier'] = 'priority'
        parser = self.parse([meta(), first, repeated])
        self.assertFalse(totals(parser)[0][0].fast_mode)
        self.assertTrue(any('冲突配置' in warning for warning in totals(parser)[2]))

    def test_copied_fork_settings_do_not_supply_fast_mode(self):
        parser = self.parse([meta(CHILD, 10, ROOT), settings(second=1),
            context('gpt-6-astra', 'child-turn', 11, 'high'),
            record(CHILD, turn='child-turn', second=12)], CHILD)
        self.assertIsNone(totals(parser)[0][0].service_tier)

    def test_legacy_request_has_tier_and_gap_stays_unknown(self):
        parser = self.parse([meta(), settings(), start(), context(effort='high'), event()])
        self.assertTrue(totals(parser)[0][0].fast_mode)

    def test_service_tier_mapping_and_other_tier_preservation(self):
        for tier in ('priority', 'fast'):
            self.assertTrue(fast_mode(tier))
        for tier in ('default', 'standard'):
            self.assertFalse(fast_mode(tier))
        for tier in (None, 'ultrafast', 'flex'):
            self.assertIsNone(fast_mode(tier))
        request = record(); request['payload']['service_tier'] = 'flex'
        self.assertEqual(totals(self.parse([meta(), request]))[0][0].service_tier, 'flex')

    def test_configurations_conserve_all_fields_and_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            con = sqlite3.connect(home/'state_5.sqlite')
            con.executescript('CREATE TABLE threads(id TEXT,rollout_path TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT);')
            for owner, tier in ((ROOT, 'priority'), (CHILD, 'default')):
                path = home/(owner+'.jsonl')
                request = record(owner); request['payload'].update(model='gpt-6-sol',reasoning_effort='high',service_tier=tier)
                path.write_text('\n'.join(json.dumps(r) for r in (meta(owner), request))+'\n')
                con.execute('INSERT INTO threads VALUES(?,?)',(owner,str(path)))
            con.execute('INSERT INTO thread_spawn_edges VALUES(?,?)',(ROOT,CHILD)); con.commit(); con.close()
            model = UsageService(home).query(ROOT)['models'][0]
            self.assertEqual(len(model['reasoning_efforts']),1)
            self.assertEqual(len(model['configurations']),2)
            for field in model['totals']:
                for role in ('totals','main','subagents'):
                    self.assertEqual(sum(g[role][field] for g in model['configurations']),model[role][field])


if __name__ == '__main__':
    unittest.main()
