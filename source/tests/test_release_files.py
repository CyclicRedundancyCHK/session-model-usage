import importlib.util
from pathlib import Path
import tempfile
import unittest
import uuid

spec=importlib.util.spec_from_file_location("public_release",Path(__file__).resolve().parents[2]/"scripts/build_release.py")
release=importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseFilesTests(unittest.TestCase):
    def test_authentication_files_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"auth.json"
            path.write_text("{}")
            with self.assertRaises(ValueError):release.inspect_public_file(path)

    def test_real_conversation_identifier_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"record.json"
            path.write_text(str(uuid.uuid4()))
            with self.assertRaises(ValueError):release.inspect_public_file(path)

    def test_synthetic_conversation_identifier_is_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"example.json"
            path.write_text("00000000-0000-0000-0000-000000000001")
            release.inspect_public_file(path)


if __name__ == "__main__":
    unittest.main()
