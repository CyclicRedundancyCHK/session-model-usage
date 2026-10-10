import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from session_model_usage.identity import ClientBindings

FIRST = '00000000-0000-0000-0000-000000000001'
SECOND = '00000000-0000-0000-0000-000000000002'
CLIENT = 'client-new-thread:' + FIRST


class ClientBindingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.json'
        self.reader = ClientBindings(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, atoms):
        self.path.write_text(json.dumps({'electron-persisted-atom-state': atoms}), encoding='utf-8')

    def test_direct_client_binding_resolves_saved_draft(self):
        self.write({'client-thread-bindings-v1': {CLIENT: SECOND}})
        self.assertEqual(self.reader.resolve(CLIENT), SECOND)

    def test_local_inverse_binding_resolves_when_direct_map_is_absent(self):
        self.write({'thread-client-id-v1:local%3A' + SECOND: CLIENT})
        self.assertEqual(self.reader.resolve(CLIENT), SECOND)

    def test_conflicting_bindings_do_not_choose_a_thread(self):
        self.write({'client-thread-bindings-v1': {CLIENT: FIRST},
                    'thread-client-id-v1:local%3A' + SECOND: CLIENT})
        self.assertIsNone(self.reader.resolve(CLIENT))

    def test_nonlocal_malformed_and_unrelated_keys_cannot_bind(self):
        for key in ('thread-client-id-v1:remote%3A' + SECOND,
                    'thread-client-id-v1:local%3A' + SECOND + '-suffix',
                    'some-other-field:' + SECOND):
            self.write({key: CLIENT})
            self.assertIsNone(self.reader.resolve(CLIENT))
        self.write({'client-thread-bindings-v1': {CLIENT: SECOND + '-suffix'}})
        self.assertIsNone(self.reader.resolve(CLIENT))

    def test_unsaved_draft_is_pending_and_its_uuid_is_not_used_as_thread(self):
        self.write({'client-thread-bindings-v1': {}})
        self.assertIsNone(self.reader.resolve(CLIENT))

    def test_same_client_refreshes_after_binding_arrives(self):
        self.write({})
        self.assertIsNone(self.reader.resolve(CLIENT))
        self.write({'client-thread-bindings-v1': {CLIENT: SECOND}})
        self.assertEqual(self.reader.resolve(CLIENT), SECOND)

    def test_partial_write_and_deletion_discard_previous_binding(self):
        self.write({'client-thread-bindings-v1': {CLIENT: SECOND}})
        self.assertEqual(self.reader.resolve(CLIENT), SECOND)
        self.path.write_text('{', encoding='utf-8')
        self.assertIsNone(self.reader.resolve(CLIENT))
        self.write({'client-thread-bindings-v1': {CLIENT: SECOND}})
        self.assertEqual(self.reader.resolve(CLIENT), SECOND)
        self.path.unlink()
        self.assertIsNone(self.reader.resolve(CLIENT))

    def test_oversize_and_unsupported_state_cannot_reuse_previous_binding(self):
        self.write({'client-thread-bindings-v1': {CLIENT: SECOND}})
        self.assertEqual(self.reader.resolve(CLIENT), SECOND)
        with patch.object(self.reader, 'LIMIT', 8):
            self.assertIsNone(self.reader.resolve(CLIENT))
        self.path.write_text('[]', encoding='utf-8')
        self.assertIsNone(self.reader.resolve(CLIENT))

    def test_missing_binding_file_is_not_an_error(self):
        self.assertIsNone(self.reader.resolve(CLIENT))

    def test_invalid_client_does_not_read_state(self):
        with patch.object(Path, 'stat', side_effect=AssertionError('unexpected read')):
            for client in (None, FIRST, 'client-new-thread:bad', CLIENT + '-suffix'):
                self.assertIsNone(self.reader.resolve(client))
