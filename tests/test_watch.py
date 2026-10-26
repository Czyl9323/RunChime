"""Offline integration tests: python3 -m unittest discover -s scripts -p test_watch_run.py."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/watch_run.py"
from runchime import watch



class WatchRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def command(self, extra=(), code="print('hello')"):
        return [sys.executable, str(SCRIPT), '--name', 'offline-test', '--no-notify',
                '--log-dir', str(self.root), *extra, '--', sys.executable, '-c', code]

    def result(self):
        paths = list(self.root.glob('*/result.json'))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def test_success_and_merged_output(self):
        completed = subprocess.run(self.command(code="import sys; print('stdout'); print('stderr',file=sys.stderr)"), capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0)
        result = self.result()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['notification']['status'], 'disabled')
        log = Path(result['log_path'])
        self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn('stdout', log.read_text())
        self.assertIn('stderr', log.read_text())

    def test_error_exit_is_preserved(self):
        completed = subprocess.run(self.command(code="import sys; print('failure'); sys.exit(7)"), capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 7)
        self.assertEqual(self.result()['status'], 'failed')
        self.assertEqual(self.result()['returncode'], 7)

    def test_task_signal(self):
        completed = subprocess.run(self.command(code="import os,signal; os.kill(os.getpid(),signal.SIGTERM)"), capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 143)
        self.assertEqual(self.result()['status'], 'terminated')
        self.assertEqual(self.result()['signal'], 'SIGTERM')

    def test_timeout_escalates(self):
        completed = subprocess.run(self.command(extra=['--timeout','0.4','--kill-after','0.2'], code="import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"), capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(self.result()['status'], 'timed_out')
        self.assertEqual(self.result()['returncode'], -9)

    def test_signal_forwarded_to_child(self):
        process = subprocess.Popen(self.command(code="import time; print('ready',flush=True); time.sleep(30)"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                logs = list(self.root.glob('*/output.log'))
                if logs and 'ready' in logs[0].read_text():
                    break
                time.sleep(0.05)
            else:
                self.fail('child did not become ready')
            process.terminate()
            self.assertEqual(process.wait(timeout=5), 143)
            result = self.result()
            self.assertEqual(result['status'], 'interrupted')
            with self.assertRaises(ProcessLookupError):
                os.kill(result['pid'], 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_launch_failure(self):
        command = self.command()
        command = command[:command.index('--')+1] + ['/missing/watch-run-test-executable']
        completed = subprocess.run(command, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 127)
        self.assertEqual(self.result()['status'], 'launch_failed')

    def test_missing_key_does_not_change_task_exit(self):
        command = self.command()
        command.remove('--no-notify')
        command[command.index('--'):command.index('--')] = ['--key-file', str(self.root/'missing-key')]
        completed = subprocess.run(command, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.result()['notification']['status'], 'configuration_error')

    def test_notify_acceptance_failure_and_privacy(self):
        result = dict(name='test',host='host',status='failed',started_at='start',finished_at='end',duration_seconds=1,returncode=7,exit_code=7,log_path='/logs/output.log',command='SECRET_COMMAND',log='SECRET_LOG')
        with patch.object(watch,'urlopen',return_value=io.BytesIO(b'{"code":0}')) as send:
            self.assertEqual(watch.notify('SCTdummy',result)['status'], 'accepted')
            request = send.call_args.args[0]
            self.assertNotIn('SECRET',str(parse_qs(request.data.decode())))
            send.assert_called_once()
        with patch.object(watch,'urlopen',side_effect=TimeoutError('SCTdummy')) as send:
            response = watch.notify('SCTdummy',result)
            self.assertEqual(response['status'],'unknown')
            self.assertNotIn('SCTdummy',str(response))
            send.assert_called_once()
        with patch.object(watch,'urlopen',return_value=io.BytesIO(b'{"code":1}')):
            self.assertEqual(watch.notify('SCTdummy',result)['status'],'rejected')

    def test_no_shell_expansion(self):
        marker = self.root/'should-not-exist'
        command = self.command(code='import sys; print(sys.argv[1])') + ['$(touch %s)' % marker]
        completed = subprocess.run(command, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode,0)
        self.assertFalse(marker.exists())

    def start_attach_target(self, ending='pass'):
        trigger = self.root/'release-target'
        code = ("import time,os,signal,sys\nfrom pathlib import Path\n"
                "while not Path(sys.argv[1]).exists(): time.sleep(0.02)\n" + ending)
        process = subprocess.Popen([sys.executable,'-c',code,str(trigger)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait()
        self.addCleanup(cleanup)
        return process, trigger

    def attach_command(self, pid, *extra):
        return [sys.executable,str(SCRIPT),'--name','attach-test','--no-notify',
                '--log-dir',str(self.root),'--pid',str(pid),'--interval','0.05',*extra]

    def start_monitor(self, target, *extra):
        monitor = subprocess.Popen(self.attach_command(target.pid,*extra), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if monitor.poll() is None:
                monitor.kill()
            monitor.wait()
        self.addCleanup(cleanup)
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            results = list(self.root.glob('*/result.json'))
            if results and json.loads(results[0].read_text()).get('process_start_ticks'):
                return monitor
            if monitor.poll() is not None:
                self.fail('monitor exited before attaching')
            time.sleep(0.02)
        self.fail('monitor failed to attach')

    def check_attach_exit(self, ending, expected):
        target, trigger = self.start_attach_target(ending)
        monitor = self.start_monitor(target)
        trigger.touch()
        # Do not wait/reap target until the monitor has detected its zombie state.
        self.assertEqual(monitor.wait(timeout=5),0)
        result = self.result()
        self.assertEqual(result['status'],'exited_unknown')
        self.assertEqual(result['reason'],'process_exited')
        self.assertIsNone(result['returncode'])
        self.assertEqual(result['mode'],'attach')
        self.assertEqual(target.wait(timeout=5),expected)

    def test_attach_normal_exit_is_unknown(self):
        self.check_attach_exit('pass',0)

    def test_attach_error_exit_is_unknown(self):
        self.check_attach_exit('sys.exit(7)',7)

    def test_attach_signal_exit_is_unknown(self):
        self.check_attach_exit('os.kill(os.getpid(),signal.SIGTERM)',-15)

    def test_attach_timeout_does_not_kill_target(self):
        target, _ = self.start_attach_target()
        monitor = self.start_monitor(target,'--timeout','0.3')
        self.assertEqual(monitor.wait(timeout=5),124)
        self.assertEqual(self.result()['status'],'monitor_timed_out')
        self.assertIsNone(target.poll())

    def test_attach_interrupt_does_not_kill_target_or_modify_task_log(self):
        target, _ = self.start_attach_target()
        log = self.root/'task.log'
        log.write_text('Existing private training output')
        monitor = self.start_monitor(target,'--task-log',str(log))
        monitor.terminate()
        self.assertEqual(monitor.wait(timeout=5),143)
        result = self.result()
        self.assertEqual(result['status'],'monitor_stopped')
        self.assertEqual(result['task_log_path'],str(log))
        self.assertIsNone(target.poll())
        self.assertEqual(log.read_text(),'Existing private training output')

    def test_attach_already_reaped_pid_is_not_success(self):
        target = subprocess.Popen([sys.executable,'-c','pass'])
        target.wait(timeout=5)
        completed = subprocess.run(self.attach_command(target.pid),capture_output=True,timeout=5)
        self.assertEqual(completed.returncode,2)
        self.assertEqual(self.result()['status'],'attach_failed')
        self.assertIsNone(self.result()['returncode'])

    def test_pid_reuse_and_disappearance(self):
        initial = {'state':'R','start_ticks':123}
        self.assertEqual(watch.process_end_reason(initial,{'state':'R','start_ticks':124}),'pid_reused')
        self.assertEqual(watch.process_end_reason(initial,None),'process_missing')
        self.assertIsNone(watch.process_end_reason(initial,{'state':'S','start_ticks':123}))
        # Ensure the attach loop itself stops on PID reuse, not just the helper.
        args = watch.parse_args(['--name','reuse-test','--pid','123','--no-notify','--log-dir',str(self.root)])
        with patch.object(watch,'read_process',side_effect=[initial,{'state':'R','start_ticks':124}]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watch.run(args),0)
        self.assertEqual(self.result()['reason'],'pid_reused')
        self.assertEqual(self.result()['status'],'exited_unknown')

    def test_proc_stat_handles_special_process_names(self):
        fields = ['S']+['0']*18+['12345']+['0']*4
        text = '123 (train) worker\nname) '+' '.join(fields)
        with patch.object(Path,'read_text',return_value=text):
            self.assertEqual(watch.read_process(123),{'state':'S','start_ticks':12345})

    def test_attach_permission_error_is_not_exit(self):
        args = watch.parse_args(['--name','permission-test','--pid','123','--no-notify','--log-dir',str(self.root)])
        with patch.object(watch,'read_process',side_effect=PermissionError()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watch.run(args),2)
        self.assertEqual(self.result()['status'],'observation_failed')

    def test_attach_argument_conflicts(self):
        for extra in [['--pid','0'],['--pid','123','--','python','train.py'],['--pid','123','--kill-after','1'],['--task-log','train.log','--','python','train.py']]:
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                watch.parse_args(['--name','invalid',*extra])
            self.assertEqual(exc.exception.code,2)

    def test_attach_notification_reports_unknown(self):
        result = dict(name='attach',host='host',mode='attach',pid=123,status='exited_unknown',started_at='start',finished_at='end',duration_seconds=1,returncode=None,exit_code=0,log_path='/monitor/output.log',task_log_path='/task/train.log')
        with patch.object(watch,'urlopen',return_value=io.BytesIO(b'{"code":0}')) as send:
            self.assertEqual(watch.notify('SCTdummy',result)['status'],'accepted')
            data = parse_qs(send.call_args.args[0].data.decode())
        self.assertIn('结果未知',data['title'][0])
        self.assertIn('进程退出码：未知',data['desp'][0])
        self.assertIn('脚本退出码不代表任务成功',data['desp'][0])


if __name__ == '__main__':
    unittest.main()
