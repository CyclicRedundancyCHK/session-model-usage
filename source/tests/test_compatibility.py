"""Sanitized regressions for changing rollout/index/UIA capabilities."""
import json
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from session_model_usage.accounting import Rollout, UsageService
from session_model_usage.native import automation_client, document_appearance, compose_geometry, sidebar_selection
from test_accounting import ROOT, CHILD, GRANDCHILD, meta, context, record, event, usage, totals
from test_native import nodes


@contextmanager
def connection(path):
    with closing(sqlite3.connect(path)) as con:
        with con:
            yield con


class CounterCompatibilityTests(unittest.TestCase):
    def parse(self, rows):
        parser = Rollout(Path('unused.jsonl'), ROOT)
        for row in rows:
            parser.feed(row)
        return parser

    def test_paginated_history_base_excludes_parent_counter_in_both_streams(self):
        header = meta()
        header['payload'].update(history_mode='paginated', history_base={
            'thread_id': CHILD, 'end_ordinal_exclusive': 100, 'end_byte_offset': 2000})
        parser = self.parse([header, context(),
            record(cumulative=usage(1100, 110, 550, 44)),
            event(cumulative=usage(900, 90, 450, 36)),
            record(response='r2', cumulative=usage(1200, 120, 600, 48), second=3),
            event(cumulative=usage(1000, 100, 500, 40), second=3)])
        entries, total, warnings = totals(parser)
        self.assertEqual(total.total_tokens, 220)
        self.assertEqual(len(entries), 2)
        self.assertFalse(warnings)

    def test_parent_thread_metadata_filters_inherited_legacy_rows(self):
        header = meta(second=10)
        header['payload']['parent_thread_id'] = CHILD
        parser = self.parse([header, context(second=1), event(second=2),
            context(second=11), record(second=12), event(second=12)])
        self.assertEqual(totals(parser)[1].total_tokens, 110)
        self.assertFalse(totals(parser)[2])

    def test_missing_modern_request_without_mirrors_preserves_known_total(self):
        parser = self.parse([meta(), context(), record(),
            record(response='r3', cumulative=usage(300, 30, 150, 12), second=4)])
        entries, total, warnings = totals(parser)
        self.assertEqual(total.total_tokens, 330)
        self.assertEqual(entries[-1].usage.total_tokens, 110)
        self.assertEqual(entries[-1].source, 'request_counter_gap')
        self.assertIsNone(entries[-1].model)
        self.assertTrue(warnings)
        self.assertEqual(totals(parser)[1], total)  # Repeat query never adds twice.
        parser.feed(record(response='r2', second=5, cumulative=usage(300, 30, 150, 12)))
        self.assertEqual(totals(parser)[1], total)  # Late detail replaces the gap.
        self.assertFalse(totals(parser)[2])

    def test_gap_covered_by_legacy_mirror_is_not_counted_twice(self):
        parser = self.parse([meta(), context(), record(), event(),
            record(response='r3', cumulative=usage(300, 30, 150, 12), second=4),
            event(cumulative=usage(300, 30, 150, 12), second=4)])
        entries, total, warnings = totals(parser)
        self.assertEqual(total.total_tokens, 330)
        self.assertEqual(sum(e.source.endswith('gap') for e in entries), 1)
        self.assertTrue(warnings)

    def test_absent_intermediate_counter_does_not_count_advancement_twice(self):
        middle = record(response='r2', second=3)
        del middle['payload']['thread_token_usage']
        parser = self.parse([meta(), context(), record(), middle,
            record(response='r3', cumulative=usage(300, 30, 150, 12), second=4)])
        self.assertEqual(totals(parser)[1].total_tokens, 330)
        self.assertFalse(totals(parser)[2])

    def test_late_lower_counter_replaces_gap_without_inventing_reset_usage(self):
        parser = self.parse([meta(), context(), record(),
            record(response='r3', cumulative=usage(300, 30, 150, 12), second=4),
            record(response='late-r2', cumulative=usage(200, 20, 100, 8), second=5)])
        self.assertEqual(totals(parser)[1].total_tokens, 330)
        self.assertFalse(totals(parser)[2])

    def test_late_detail_without_counter_also_replaces_gap(self):
        late = record(response='late-r2', second=5)
        del late['payload']['thread_token_usage']
        parser = self.parse([meta(), context(), record(),
            record(response='r3', cumulative=usage(300, 30, 150, 12), second=4), late])
        self.assertEqual(totals(parser)[1].total_tokens, 330)
        self.assertFalse(totals(parser)[2])

    def test_first_modern_counter_recovers_prefix_without_mirror(self):
        parser = self.parse([meta(), context(), record(cumulative=usage(300, 30, 150, 12))])
        self.assertEqual(totals(parser)[1].total_tokens, 330)
        self.assertIsNone(totals(parser)[0][-1].model)

    def test_modern_counter_reset_does_not_erase_requests_or_make_gap(self):
        parser = self.parse([meta(), context(), record(),
            record(response='r2', second=3), record(response='r3', second=4)])
        self.assertEqual(totals(parser)[1].total_tokens, 330)
        self.assertFalse(totals(parser)[2])

    def test_unknown_model_and_effort_still_preserve_total(self):
        request = record()
        request['payload'].update(model='future-model', reasoning_effort='new-effort')
        parser = self.parse([meta(), request])
        self.assertEqual(totals(parser)[0][0].model, 'future-model')
        self.assertEqual(totals(parser)[0][0].reasoning_effort, 'new-effort')
        self.assertFalse(totals(parser)[2])


class IndexCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.database = self.home / 'state_5.sqlite'
        with connection(self.database) as con:
            con.executescript('CREATE TABLE threads(id TEXT, rollout_path TEXT, source TEXT);'
                              'CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT);')

    def tearDown(self):
        self.temp.cleanup()

    def log(self, owner=ROOT, parent=None, folder='sessions'):
        path = self.home / folder / f'rollout-{owner}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        header = meta(owner)
        if parent:
            header['payload']['parent_thread_id'] = parent
        rows = [header, context(), record(owner)]
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
        return path

    def indexed(self, owner=ROOT, parent=None):
        path = self.log(owner, parent)
        with connection(self.database) as con:
            con.execute('INSERT INTO threads VALUES(?,?,?)', (owner, str(path), 'vscode'))
            if parent:
                con.execute('INSERT INTO thread_spawn_edges VALUES(?,?)', (parent, owner))
        return path

    def test_newest_corrupt_index_falls_back_without_losing_usage(self):
        self.indexed()
        (self.home / 'state_6.sqlite').write_bytes(b'broken')
        snapshot = UsageService(self.home).query(ROOT)
        self.assertEqual(snapshot['totals']['total_tokens'], 110)
        self.assertEqual(snapshot['status'], 'partial')

    def test_new_schema_missing_path_uses_older_index(self):
        self.indexed()
        with connection(self.home / 'state_6.sqlite') as con:
            con.execute('CREATE TABLE threads(id TEXT, unknown_path TEXT)')
        self.assertEqual(UsageService(self.home).query(ROOT)['totals']['total_tokens'], 110)

    def test_migration_empty_index_uses_older_index(self):
        self.indexed()
        with connection(self.home / 'state_6.sqlite') as con:
            con.execute('CREATE TABLE threads(id TEXT, rollout_path TEXT)')
        snapshot = UsageService(self.home).query(ROOT)
        self.assertEqual(snapshot['totals']['total_tokens'], 110)
        self.assertTrue(snapshot['warnings'])

    def test_no_index_recovers_recursive_archived_subagents_from_headers(self):
        self.database.unlink()
        self.log(); self.log(CHILD, ROOT); self.log(GRANDCHILD, CHILD, 'archived_sessions')
        snapshot = UsageService(self.home).query(ROOT)
        self.assertEqual(snapshot['totals']['total_tokens'], 330)
        self.assertEqual(len(snapshot['threads']), 3)
        self.assertEqual(snapshot['status'], 'partial')
        self.assertEqual(UsageService(self.home).query(ROOT, False)['totals']['total_tokens'], 110)

    def test_unreadable_index_recovers_log_and_child(self):
        self.log(); self.log(CHILD, ROOT)
        self.database.write_bytes(b'broken')
        snapshot = UsageService(self.home).query(ROOT)
        self.assertEqual(snapshot['totals']['total_tokens'], 220)
        self.assertEqual(snapshot['status'], 'partial')

    def test_stale_archived_path_recovers_real_file(self):
        path = self.indexed()
        archived = self.home / 'archived_sessions' / path.name
        archived.parent.mkdir(); path.rename(archived)
        snapshot = UsageService(self.home).query(ROOT)
        self.assertEqual(snapshot['totals']['total_tokens'], 110)
        self.assertEqual(snapshot['status'], 'complete')

    def test_thread_source_column_recovers_children_without_edge_table(self):
        self.indexed(); self.indexed(CHILD)
        with connection(self.database) as con:
            con.execute('DROP TABLE thread_spawn_edges')
            con.execute('ALTER TABLE threads RENAME COLUMN source TO thread_source')
            source = json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': ROOT}}})
            con.execute('UPDATE threads SET thread_source=? WHERE id=?', (source, CHILD))
        snapshot = UsageService(self.home).query(ROOT)
        self.assertEqual(snapshot['totals']['total_tokens'], 220)

    def test_edge_schema_change_is_partial_but_keeps_main(self):
        self.indexed()
        with connection(self.database) as con:
            con.execute('DROP TABLE thread_spawn_edges')
            con.execute('CREATE TABLE thread_spawn_edges(new_parent TEXT, new_child TEXT)')
        snapshot = UsageService(self.home).query(ROOT)
        self.assertEqual(snapshot['totals']['total_tokens'], 110)
        self.assertEqual(snapshot['status'], 'partial')

    def test_fallback_never_trusts_filename_over_metadata_owner(self):
        self.database.unlink()
        path = self.log(CHILD)
        path.rename(path.with_name(f'rollout-{ROOT}.jsonl'))
        self.assertIsNone(UsageService(self.home).query(ROOT, False)['totals'])

    def paginated(self, old, second=10, model='future-model'):
        data = old.read_bytes()
        header = meta(second=second)
        header['payload']['history_base'] = {'thread_id': ROOT,
            'end_byte_offset': len(data), 'end_ordinal_exclusive': data.count(b'\n')}
        path = self.home / 'sessions' / f'page-{second}-{ROOT}.jsonl'
        rows = [header, context(model, 'next-turn', second+1),
                record(turn='next-turn', response=f'page-{second}', second=second+2,
                       cumulative=usage(200, 20, 100, 8)),
                event(cumulative=usage(200, 20, 100, 8), second=second+2)]
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
        with connection(self.database) as con:
            con.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(path), ROOT))
        return path

    def test_same_thread_history_prefix_restores_old_model_and_counts_once(self):
        old = self.indexed()
        page = self.paginated(old)
        # A request after the referenced boundary belongs to another branch of
        # the source file and must not leak into the paginated continuation.
        with old.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record(response='outside-prefix', second=20)) + '\n')
        service = UsageService(self.home)
        snapshot = service.query(ROOT, False)
        self.assertEqual(snapshot['status'], 'complete')
        self.assertEqual(snapshot['totals']['total_tokens'], 220)
        self.assertEqual({m['model'] for m in snapshot['models']}, {'gpt-6-sol', 'future-model'})
        self.assertEqual(service.query(ROOT, False)['totals'], snapshot['totals'])
        parser = next(iter(service.parsers.values()))
        self.assertEqual(len(parser.history), 1)
        self.assertEqual(parser.path, page)

    def test_chained_pages_restore_all_request_metadata(self):
        old = self.indexed()
        first = self.paginated(old)
        second = self.paginated(first, second=20, model='third-model')
        # The modern counter includes the complete prefix, not only page one.
        rows = [json.loads(line) for line in second.read_text(encoding='utf-8').splitlines()]
        rows[2]['payload']['thread_token_usage'] = usage(300, 30, 150, 12)
        rows[3]['payload']['info']['total_token_usage'] = usage(300, 30, 150, 12)
        second.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
        snapshot = UsageService(self.home).query(ROOT, False)
        self.assertEqual(snapshot['totals']['total_tokens'], 330)
        self.assertEqual(snapshot['status'], 'complete')
        self.assertEqual(len(snapshot['models']), 3)

    def test_missing_history_is_partial_and_does_not_invent_unattributed_baseline(self):
        old = self.indexed()
        self.paginated(old); old.unlink()
        snapshot = UsageService(self.home).query(ROOT, False)
        self.assertEqual(snapshot['totals']['total_tokens'], 110)
        self.assertEqual(snapshot['status'], 'partial')
        self.assertEqual(len(snapshot['models']), 1)

    def test_history_ordinal_mismatch_does_not_read_unverified_prefix(self):
        old = self.indexed(); page = self.paginated(old)
        rows = [json.loads(line) for line in page.read_text(encoding='utf-8').splitlines()]
        rows[0]['payload']['history_base']['end_ordinal_exclusive'] += 1
        page.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
        snapshot = UsageService(self.home).query(ROOT, False)
        self.assertEqual(snapshot['totals']['total_tokens'], 110)
        self.assertEqual(snapshot['status'], 'partial')

    def test_ambiguous_history_files_do_not_choose_most_recent_source(self):
        old = self.indexed(); self.paginated(old)
        old.with_name('copy-' + old.name).write_bytes(old.read_bytes())
        snapshot = UsageService(self.home).query(ROOT, False)
        self.assertEqual(snapshot['totals']['total_tokens'], 110)
        self.assertEqual(snapshot['status'], 'partial')

    def test_foreign_metadata_cannot_attach_another_threads_prefix(self):
        old = self.indexed(); page = self.paginated(old)
        history, warnings = UsageService(self.home)._history(page, CHILD)
        self.assertFalse(history)
        self.assertTrue(warnings)

    def test_invalid_history_thread_reference_is_partial(self):
        old = self.indexed(); page = self.paginated(old)
        rows = [json.loads(line) for line in page.read_text(encoding='utf-8').splitlines()]
        rows[0]['payload']['history_base']['thread_id'] = None
        page.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
        self.assertEqual(UsageService(self.home).query(ROOT, False)['status'], 'partial')


class NativeCapabilityTests(unittest.TestCase):
    def test_global_navigation_and_right_tabs_do_not_change_conversation_selection(self):
        icon = (8, 52, 44, 88)
        task = (60, 200, 317, 230)
        tab = (1000, 52, 1200, 88)
        controls = [{'type':50000,'rect':r} for r in (icon,task,tab)]
        selection, required = sidebar_selection(controls, [((1,),icon),((2,),task),((3,),tab)], 770)
        self.assertEqual(selection, ((2,),))
        self.assertTrue(required)
    def test_collapsed_sidebar_does_not_require_a_conversation_row(self):
        icon = (8, 52, 44, 88)
        selection, required = sidebar_selection([{'type':50000,'rect':icon}], [((1,),icon)], 770)
        self.assertEqual(selection, ())
        self.assertFalse(required)
    def test_desktop_query_parameters_do_not_block_primary_view(self):
        self.assertEqual(document_appearance('app://-/index.html?build=next#view'), 'primary')
        self.assertEqual(document_appearance('app://-/index.html?initialRoute=%2Flocal%2F' + ROOT), 'primary')

    def test_detached_route_is_decoded_and_external_auxiliary_urls_rejected(self):
        self.assertEqual(document_appearance('app://-/index.html?initialRoute=%2Fdetached-window%2Fone'), 'detached')
        for url in ('https://example.com/?initialRoute=%2Fdetached-window',
                    'app://other/index.html', 'app://-/index.html?initialRoute=%2Favatar',
                    'app://-/index.html?initialRoute=%2Fdetached-window-fake',
                    'app://-/index.html?initialRoute=%2F&initialRoute=%2Fdetached-window'):
            self.assertIsNone(document_appearance(url))
    def test_micro_local_draft_document_is_supported_but_other_drafts_are_rejected(self):
        self.assertEqual(document_appearance('app://-/index.html?initialRoute=%2Flocal%2Fclient-new-thread%3A'+ROOT), 'primary')
        for route in ('/local/client-new-thread:bad', '/remote/client-new-thread:'+ROOT,
                      '/local/client-new-thread:'+ROOT+'fake'):
            self.assertIsNone(document_appearance('app://-/index.html?initialRoute='+route))

    def test_context_button_role_and_split_permission_control_use_same_gap(self):
        original = nodes()
        changed = [dict(node) for node in original]
        changed[1]['type'] = 50031
        changed[2]['type'] = 50000
        self.assertEqual(compose_geometry(changed), compose_geometry(original))

    def test_layout_without_context_ring_uses_actual_right_toolbar_control(self):
        changed = nodes()[:2]
        changed.append({'type': 50000, 'name': 'Unknown future model', 'rect': (440, 148, 520, 176)})
        self.assertEqual(compose_geometry(changed)[1], compose_geometry(nodes())[1])

    def test_optional_left_toolbar_control_is_excluded_from_gap(self):
        changed = nodes()[:2]
        changed.extend([{'type': 50000, 'name': 'Plan', 'rect': (160, 148, 200, 176)},
                        {'type': 50000, 'name': 'Unknown model', 'rect': (440, 148, 520, 176)}])
        self.assertEqual(compose_geometry(changed)[1]['left'], 206)

    def test_uia_calls_finish_inside_worker_deadline(self):
        module, factory = Mock(), Mock()
        client = automation_client(module, factory)
        factory.assert_called_once_with(module.CUIAutomation8, interface=module.IUIAutomation2)
        self.assertEqual(client.ConnectionTimeout, 500)
        self.assertEqual(client.TransactionTimeout, 1500)

    def test_older_uia_reports_unavailable_timeout_capability(self):
        module, factory = Mock(), Mock(side_effect=[OSError('fixture'), Mock()])
        with patch('session_model_usage.diagnostics.record') as report:
            automation_client(module, factory)
        report.assert_called_once()
        self.assertEqual(factory.call_args.kwargs['interface'], module.IUIAutomation)
