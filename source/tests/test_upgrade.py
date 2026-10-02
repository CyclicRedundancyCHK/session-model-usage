import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from session_model_usage import installer


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.home=Path(self.temp.name)/'home'; self.home.mkdir()
        self.environment=patch.dict(os.environ,{'SESSION_USAGE_STATE_DIR':str(Path(self.temp.name)/'state')})
        self.environment.start()
        self.source=Path(self.temp.name)/'release'; self.source.mkdir()
        self.destination=self.home/'plugins/session-model-usage'
        self.seed(self.source,'0.1.2'); self.seed(self.destination,'0.1.1')
        (self.destination/'original.txt').write_text('preserve existing installation')
        self.register_fail=False
        self.entry={'name':'session-model-usage','pluginId':'session-model-usage@personal',
                    'marketplaceName':'personal','enabled':True,'source':{'path':str(self.destination)}}
    def tearDown(self):
        self.environment.stop(); self.temp.cleanup()
    def seed(self,root,version):
        for relative in ('.codex-plugin','skills','scripts','runtime','source'):
            (root/relative).mkdir(parents=True,exist_ok=True)
        (root/'.codex-plugin/plugin.json').write_text(json.dumps({'name':'session-model-usage','version':version}))
        for name in ('session-usage.exe','CodexSessionUsage.exe'):
            (root/'runtime'/name).write_bytes(version.encode())
    def cli(self,cli,*arguments):
        if arguments[1]=='list':
            version=json.loads((self.destination/'.codex-plugin/plugin.json').read_text())['version']
            return {'installed':[{**self.entry,'version':version}]}
        if self.register_fail and (self.destination/'runtime/session-usage.exe').read_bytes()==b'0.1.2':
            raise RuntimeError('fixture registration failed')
        return {'status':'installed'}
    def update(self):
        with patch.object(installer.Path,'home',return_value=self.home), \
             patch.object(installer,'find_cli',return_value='codex'), \
             patch.object(installer,'cli_json',side_effect=self.cli) as cli, \
             patch.object(installer,'stop_installed') as stop, \
             patch.object(installer.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='0.1.2\n')):
            result=installer.upgrade(self.source)
        return result,cli,stop
    def test_upgrade_preserves_marketplace_and_backs_up_old_version(self):
        result,cli,stop=self.update()
        self.assertEqual(result['marketplace'],'personal')
        self.assertEqual((self.destination/'runtime/session-usage.exe').read_bytes(),b'0.1.2')
        self.assertEqual((Path(result['backup'])/'original.txt').read_text(),'preserve existing installation')
        self.assertTrue(any(c.args[1:]==('plugin','add','session-model-usage@personal','--json') for c in cli.call_args_list))
        stop.assert_called_once_with(self.destination.resolve())
    def test_registration_failure_restores_original_files(self):
        self.register_fail=True
        with self.assertRaisesRegex(RuntimeError,'已恢复原安装'):
            self.update()
        self.assertEqual((self.destination/'runtime/session-usage.exe').read_bytes(),b'0.1.1')
        self.assertTrue((self.destination/'original.txt').exists())
    def test_unknown_product_does_not_touch_installation(self):
        (self.source/'.codex-plugin/plugin.json').write_text('{"name":"other"}')
        with self.assertRaisesRegex(RuntimeError,'来源不正确'): self.update()
        self.assertTrue((self.destination/'original.txt').exists())
    def test_missing_binary_does_not_stop_running_product(self):
        (self.source/'runtime/CodexSessionUsage.exe').unlink()
        with patch.object(installer,'stop_installed') as stop:
            with self.assertRaisesRegex(RuntimeError,'缺少程序'): self.update()
        stop.assert_not_called()
    def test_ambiguous_marketplace_does_not_modify_installation(self):
        with patch.object(installer.Path,'home',return_value=self.home), \
             patch.object(installer,'find_cli',return_value='codex'), \
             patch.object(installer,'cli_json',return_value={'installed':[self.entry,self.entry]}):
            with self.assertRaisesRegex(RuntimeError,'市场'): installer.upgrade(self.source)
        self.assertTrue((self.destination/'original.txt').exists())


if __name__=='__main__': unittest.main()
