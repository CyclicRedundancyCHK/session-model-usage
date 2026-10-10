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
        self.cache_fail=False
        self.installed=True
        self.extra_entries=[]
        self.market_root=self.destination
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
            if self.cache_fail and version=='0.1.2':
                version='0.1.1'
            return {'installed':([{**self.entry,'version':version}] if self.installed else [])+self.extra_entries}
        if arguments[1:3]==('marketplace','list'):
            return {'marketplaces': ([{'name':'session-model-usage','root':str(self.market_root)}]
                                     if self.market_root is not None else [])}
        if self.register_fail and (self.destination/'runtime/session-usage.exe').read_bytes()==b'0.1.2':
            raise RuntimeError('fixture registration failed')
        if arguments[1:3]==('marketplace','add'):
            added=self.market_root is None
            self.market_root=self.destination
            return {'alreadyAdded':not added}
        if arguments[1]=='add':
            self.installed=True
        elif arguments[1]=='remove':
            self.installed=False
        return {'status':'installed'}
    def update(self):
        with patch.object(installer.Path,'home',return_value=self.home), \
             patch.object(installer,'find_cli',return_value='codex'), \
             patch.object(installer,'cli_json',side_effect=self.cli) as cli, \
             patch.object(installer,'stop_installed') as stop, \
             patch.object(installer,'create_shortcut',return_value=self.home/'usage.lnk') as shortcut, \
             patch.object(installer.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='0.1.2\n')):
            result=installer.upgrade(self.source)
        if self.entry['marketplaceName']!='session-model-usage':
            shortcut.assert_not_called()
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

    def prepare_pending_installation(self):
        self.installed=False
        self.entry={**self.entry,'pluginId':'session-model-usage@session-model-usage',
                    'marketplaceName':'session-model-usage'}

    def test_failed_first_installation_can_be_retried(self):
        self.prepare_pending_installation()
        result,cli,stop=self.update()
        self.assertEqual(result['status'],'repaired')
        self.assertEqual(result['shortcut'],str(self.home/'usage.lnk'))
        self.assertEqual((Path(result['backup'])/'original.txt').read_text(),'preserve existing installation')
        self.assertTrue(any(c.args[1:]==('plugin','marketplace','add',str(self.destination.resolve()),'--json')
                            for c in cli.call_args_list))
        self.assertEqual(json.loads((self.destination/'.agents/plugins/marketplace.json').read_text(encoding='utf-8'))
                         ['plugins'][0]['source']['path'],'./')
        stop.assert_called_once_with(self.destination.resolve())

    def test_pending_installation_without_registered_market_can_be_retried(self):
        self.prepare_pending_installation()
        self.market_root=None
        result,_,_=self.update()
        self.assertEqual(result['status'],'repaired')

    def test_retry_registration_failure_restores_previous_directory(self):
        self.prepare_pending_installation()
        self.register_fail=True
        with self.assertRaisesRegex(RuntimeError,'已恢复原安装'):
            self.update()
        self.assertEqual((self.destination/'runtime/session-usage.exe').read_bytes(),b'0.1.1')
        self.assertTrue((self.destination/'original.txt').exists())
        self.assertFalse(self.installed)

    def test_other_directory_registration_is_not_selected_for_upgrade(self):
        self.entry['source']['path']=str(self.home/'other-plugin')
        with self.assertRaisesRegex(RuntimeError,'目录与升级目标不一致'):
            self.update()
        self.assertTrue((self.destination/'original.txt').exists())

    def test_same_name_in_another_directory_does_not_block_unique_owned_registration(self):
        self.extra_entries=[{**self.entry,'pluginId':'session-model-usage@other',
                             'marketplaceName':'other','source':{'path':str(self.home/'other-plugin')}}]
        result,cli,_=self.update()
        self.assertEqual(result['marketplace'],'personal')
        self.assertFalse(any('session-model-usage@other' in c.args for c in cli.call_args_list))

    def test_pending_installation_does_not_replace_another_market_root(self):
        self.prepare_pending_installation()
        self.market_root=self.home/'other-plugin'
        with self.assertRaisesRegex(RuntimeError,'市场已指向其他目录'):
            self.update()
        self.assertTrue((self.destination/'original.txt').exists())

    def test_missing_source_path_does_not_assume_owned_directory(self):
        self.entry['source']={}
        with self.assertRaisesRegex(RuntimeError,'目录与升级目标不一致'):
            self.update()

    def test_malformed_plugin_list_does_not_touch_installation(self):
        with patch.object(installer.Path,'home',return_value=self.home), \
             patch.object(installer,'find_cli',return_value='codex'), \
             patch.object(installer,'cli_json',return_value={}):
            with self.assertRaisesRegex(RuntimeError,'注册列表'): installer.upgrade(self.source)
        self.assertTrue((self.destination/'original.txt').exists())

    def test_repair_cache_failure_removes_only_new_registration_and_market(self):
        self.prepare_pending_installation()
        self.market_root=None
        self.cache_fail=True
        original_cli=self.cli
        calls=[]
        def capture(*arguments):
            calls.append(arguments[1:])
            return original_cli(*arguments)
        self.cli=capture
        with self.assertRaisesRegex(RuntimeError,'已恢复原安装'):
            self.update()
        self.assertFalse(self.installed)
        self.assertIn(('plugin','remove','session-model-usage@session-model-usage','--json'),calls)
        self.assertIn(('plugin','marketplace','remove','session-model-usage','--json'),calls)
        self.assertTrue((self.destination/'original.txt').exists())

    def test_repair_rollback_preserves_preexisting_market(self):
        self.prepare_pending_installation()
        self.cache_fail=True
        original_cli=self.cli
        calls=[]
        def capture(*arguments):
            calls.append(arguments[1:])
            return original_cli(*arguments)
        self.cli=capture
        with self.assertRaisesRegex(RuntimeError,'已恢复原安装'):
            self.update()
        self.assertFalse(any(c[0:3]==('plugin','marketplace','remove') for c in calls))

    def test_disabled_registered_plugin_keeps_original_files(self):
        self.entry['enabled']=False
        with patch.object(installer.Path,'home',return_value=self.home), \
             patch.object(installer,'find_cli',return_value='codex'), \
             patch.object(installer,'cli_json',side_effect=self.cli) as cli, \
             patch.object(installer,'stop_installed') as stop:
            with self.assertRaisesRegex(RuntimeError,'已禁用，未执行升级'):
                installer.upgrade(self.source)
        stop.assert_not_called()
        self.assertFalse(any(c.args[1]=='plugin' and c.args[2]=='add' for c in cli.call_args_list))
        self.assertFalse((self.destination.parent/'.session-model-usage-backups').exists())
        self.assertTrue((self.destination/'original.txt').exists())

    def test_temporary_windows_staging_lock_retries_and_preserves_backup(self):
        original=installer.Path.rename
        attempts=[]
        def rename(path,target):
            if path.name.endswith('-new'):
                attempts.append(path)
                if len(attempts)<3:
                    raise PermissionError('fixture transient Windows file lock')
            return original(path,target)
        with patch.object(installer.Path,'rename',rename), patch.object(installer.time,'sleep') as sleep:
            result,_,_=self.update()
        self.assertEqual(len(attempts),3)
        self.assertEqual(sleep.call_count,2)
        self.assertTrue((Path(result['backup'])/'original.txt').exists())

    def test_persistent_windows_staging_lock_is_bounded_and_rolls_back(self):
        original=installer.Path.rename
        attempts=[]
        def rename(path,target):
            if path.name.endswith('-new'):
                attempts.append(path)
                raise PermissionError('fixture persistent Windows file lock')
            return original(path,target)
        with patch.object(installer.Path,'rename',rename), patch.object(installer.time,'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError,'已恢复原安装'):
                self.update()
        self.assertEqual(len(attempts),10)
        self.assertEqual(sleep.call_count,9)
        self.assertEqual((self.destination/'runtime/session-usage.exe').read_bytes(),b'0.1.1')
        self.assertTrue((self.destination/'original.txt').exists())


if __name__=='__main__': unittest.main()
