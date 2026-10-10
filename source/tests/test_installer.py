import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from session_model_usage import installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base/"home"
        self.home.mkdir()
        self.source = self.base/"release"
        self.source.mkdir()
        self.seed(self.source)
        self.destination = self.home/"plugins/session-model-usage"

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, root):
        for name in (".codex-plugin", "skills", "scripts", "runtime", "source", "assets", "docs"):
            (root/name).mkdir(parents=True,exist_ok=True)
        (root/".codex-plugin/plugin.json").write_text(json.dumps({"name":"session-model-usage","version":"0.1.1"}))
        for name in ("session-usage.exe", "CodexSessionUsage.exe"):
            (root/"runtime"/name).write_bytes(b"test fixture")

    def run_install(self, source=None, returncode=0):
        with patch.object(installer.Path,"home",return_value=self.home), \
                patch.object(installer,"find_cli",return_value="codex.exe"), \
                patch.dict(os.environ,{"APPDATA":str(self.home/"AppData")}), \
                patch.object(installer.subprocess,"run",return_value=SimpleNamespace(returncode=returncode,stderr="fixture failure")) as run:
            result = installer.install(source or self.source)
        return result,run

    def test_fresh_install_registers_own_marketplace_and_copies_source(self):
        result,run = self.run_install()
        self.assertEqual(result["marketplace"],"session-model-usage")
        self.assertTrue((self.destination/"source").is_dir())
        self.assertTrue((self.destination/"assets").is_dir())
        self.assertTrue((self.destination/"docs").is_dir())
        self.assertEqual(run.call_args_list[0].args[0][:4],["codex.exe","plugin","marketplace","add"])
        self.assertEqual(run.call_args_list[1].args[0],["codex.exe","plugin","add","session-model-usage@session-model-usage"])
        entry=json.loads((self.destination/".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(entry["plugins"][0]["source"]["path"],"./")

    def test_unrelated_personal_marketplace_is_unchanged(self):
        path=self.home/".agents/plugins/marketplace.json"
        path.parent.mkdir(parents=True)
        original=b'{"name":"personal","plugins":[{"name":"other"}]}'
        path.write_bytes(original)
        self.run_install()
        self.assertEqual(path.read_bytes(),original)

    def test_existing_destination_is_not_overwritten(self):
        self.destination.mkdir(parents=True)
        marker=self.destination/"keep.txt"
        marker.write_text("keep")
        with self.assertRaisesRegex(RuntimeError,"目标已存在"):
            self.run_install()
        self.assertEqual(marker.read_text(),"keep")

    def test_missing_gui_binary_is_rejected(self):
        (self.source/"runtime/CodexSessionUsage.exe").unlink()
        with self.assertRaisesRegex(RuntimeError,"缺少组件"):
            self.run_install()
        self.assertFalse(self.destination.exists())

    def test_wrong_plugin_name_is_rejected(self):
        (self.source/".codex-plugin/plugin.json").write_text('{"name":"other"}')
        with self.assertRaisesRegex(RuntimeError,"名称不匹配"):
            self.run_install()

    def test_registration_failure_preserves_program_files(self):
        with self.assertRaisesRegex(RuntimeError,"注册本插件市场失败"):
            self.run_install(returncode=1)
        self.assertTrue((self.destination/"runtime/CodexSessionUsage.exe").is_file())

    def test_reinstall_from_owned_location(self):
        self.seed(self.destination)
        result,_=self.run_install(self.destination)
        self.assertEqual(result["status"],"installed")

    def test_local_marketplace_uses_exact_product_directory(self):
        path=installer.write_local_marketplace(self.source)
        value=json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(value["plugins"]),1)
        self.assertEqual((self.source/value["plugins"][0]["source"]["path"]).resolve(),self.source.resolve())

    def test_fresh_install_keeps_installation_entry_points(self):
        for name in ('Install.ps1', '安装插件.cmd', '启动悬浮条.cmd'):
            (self.source/name).write_text('fixture entry point', encoding='utf-8')
        self.run_install()
        for name in ('Install.ps1', '安装插件.cmd', '启动悬浮条.cmd'):
            self.assertEqual((self.destination/name).read_text(encoding='utf-8'), 'fixture entry point')


if __name__ == "__main__":
    unittest.main()
