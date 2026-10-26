"""Configuration and installer regression tests; never send real notifications."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from runchime import cli, config, hooks

ROOT = Path(__file__).resolve().parents[1]


class SetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'codex with spaces'
        self.home.mkdir()
        environment = patch.dict(os.environ, {
            'HOME': str(self.root), 'CODEX_HOME': str(self.home),
            'RUNCHIME_HOME': str(self.root/'config'), 'RUNCHIME_KEY_FILE': '',
            'XDG_CONFIG_HOME': str(self.root/'xdg'),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_key_permissions_no_clobber_and_replacement(self):
        destination = config.config_home()/'serverchan.key'
        config.write_key(destination,'SCTfake123')
        self.assertEqual(destination.stat().st_mode & 0o777,0o600)
        self.assertEqual(destination.parent.stat().st_mode & 0o777,0o700)
        with self.assertRaises(FileExistsError):
            config.write_key(destination,'SCTfake456')
        self.assertEqual(config.read_key(destination),'SCTfake123')
        config.write_key(destination,'SCTfake456',force=True)
        self.assertEqual(config.read_key(destination),'SCTfake456')
        self.assertEqual(list(destination.parent.glob('.serverchan-*')),[])

    def test_key_precedence_and_legacy_compatibility(self):
        legacy=self.home/'serverchan.key'
        config.write_key(legacy,'SCTlegacy')
        self.assertEqual(config.key_path(),legacy)
        preferred=config.config_home()/'serverchan.key'
        config.write_key(preferred,'SCTpreferred')
        self.assertEqual(config.key_path(),preferred)
        with patch.dict(os.environ,{'RUNCHIME_KEY_FILE':str(self.root/'environment.key')}):
            self.assertEqual(config.key_path(),self.root/'environment.key')
            self.assertEqual(config.key_path(self.root/'explicit.key'),self.root/'explicit.key')
            with self.assertRaises(ValueError):
                config.read_key(config.key_path())

    def test_invalid_key_never_written(self):
        destination=self.root/'invalid.key'
        with self.assertRaises(ValueError):
            config.write_key(destination,'not-a-sendkey')
        self.assertFalse(destination.exists())

    def test_cli_configure_doctor_and_secret_not_echoed(self):
        completed=subprocess.run([sys.executable,'-m','runchime','configure','--key-stdin'],input='SCTfakeSetup123\n',text=True,capture_output=True,cwd=str(ROOT),timeout=5,check=True)
        self.assertNotIn('SCTfakeSetup123',completed.stdout+completed.stderr)
        completed=subprocess.run([sys.executable,'-m','runchime','doctor'],text=True,capture_output=True,cwd=str(ROOT),timeout=5,check=True)
        self.assertNotIn('SCTfakeSetup123',completed.stdout+completed.stderr)
        self.assertIn('未访问',completed.stdout)

    def test_hook_merge_idempotency_uninstall_and_backup(self):
        path=self.home/'hooks.json'
        original={'description':'keep metadata','hooks':{'Stop':[{'matcher':'','hooks':[{'type':'command','command':'echo unrelated'}]}],'SubagentStop':[{'hooks':[{'type':'command','command':'echo child'}]}]}}
        path.write_text(json.dumps(original))
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.update_hooks()
            first=path.read_text()
            hooks.update_hooks()
        self.assertEqual(path.read_text(),first)
        self.assertEqual(len(list(self.home.glob('hooks.json.before-runchime-*'))),1)
        installed=json.loads(first)
        self.assertEqual(installed['hooks']['Stop'][0],original['hooks']['Stop'][0])
        self.assertEqual(installed['hooks']['SubagentStop'],original['hooks']['SubagentStop'])
        command=installed['hooks']['Stop'][-1]['hooks'][0]['command']
        completed=subprocess.run(shlex.split(command),input='{"hook_event_name":"SubagentStop"}',text=True,capture_output=True,cwd=str(self.root),check=True,timeout=5)
        self.assertEqual(completed.stdout,'{}\n')
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.update_hooks(remove=True)
        self.assertEqual(json.loads(path.read_text()),original)

    def test_legacy_migration_preserves_unrelated_settings(self):
        path=self.home/'config.toml'
        path.write_text('model = "example"\nnotify = ["python3", "/example/notify_wechat.py"]\n[features]\nhooks = true\n')
        hook_path=self.home/'hooks.json'
        hook_path.write_text(json.dumps({'hooks':{'SubagentStop':[{'hooks':[{'type':'command','command':'python3 /example/notify_wechat.py'},{'type':'command','command':'echo keep'}]}]}}))
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.update_hooks(migrate_legacy=True)
        text=path.read_text()
        self.assertIn('model = "example"',text)
        self.assertIn('[features]\nhooks = true',text)
        self.assertNotIn('notify =',text)
        self.assertEqual(len(list(self.home.glob('config.toml.before-runchime-*'))),1)
        data=json.loads(hook_path.read_text())
        self.assertEqual(data['hooks']['SubagentStop'][0]['hooks'],[{'type':'command','command':'echo keep'}])

    def test_malformed_hook_is_not_overwritten(self):
        path=self.home/'hooks.json'
        for value in ['not-json','{"hooks":{"Stop":42}}']:
            path.write_text(value)
            with self.assertRaises(ValueError):
                hooks.update_hooks()
            self.assertEqual(path.read_text(),value)

    def test_multiline_legacy_fails_without_touching_configs(self):
        path=self.home/'config.toml'
        original='notify = [\n  "python3",\n  "/example/notify_wechat.py"\n]\n'
        path.write_text(original)
        with self.assertRaises(ValueError):
            hooks.update_hooks(migrate_legacy=True)
        self.assertEqual(path.read_text(),original)
        self.assertFalse((self.home/'hooks.json').exists())

    def test_cli_modes_reject_mismatches_without_launching(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['run','--name','test','--pid','123']),2)
            self.assertEqual(cli.main(['attach','--name','test','--','nonexistent-command']),2)

    def test_codex_cli_legacy_payload_never_sends(self):
        completed=subprocess.run([sys.executable,'-m','runchime','codex-hook','{"type":"agent-turn-complete"}'],text=True,capture_output=True,cwd=str(ROOT),check=True,timeout=5)
        self.assertEqual(completed.stdout,'{}\n')


if __name__ == '__main__':
    unittest.main()
