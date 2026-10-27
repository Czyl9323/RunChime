"""Stall detection tests with synthetic metrics and controlled real processes."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from runchime import activity, watch


def process(ticks=0, counters=(0, 0, 0, 0), ppid=1, start=10):
    return {"ticks": ticks, "io": counters, "ppid": ppid, "start": start, "state": "S"}


class ActivityTests(unittest.TestCase):
    def measure(self, first, second, gpu=None, log=None, between=None):
        sampler = activity.Sampler(100, 10, log)
        sampler.hz = 100
        with patch.object(activity, 'process_tree', side_effect=[first, second]), \
             patch.object(activity, 'gpu_activity', return_value=gpu or {'status': 'not_used'}), \
             patch.object(activity.time, 'monotonic', side_effect=[0, 1]):
            self.assertEqual(sampler.sample()['state'], 'unknown')
            if between:
                between()
            return sampler.sample()

    def test_quiet_requires_baseline_and_multiple_signals(self):
        tree = {(100, 10): process()}
        sample = self.measure(tree, tree)
        self.assertEqual(sample['state'], 'quiet')
        self.assertEqual(sample['cpu_percent'], 0)
        self.assertFalse(sample['io_changed'])

    def test_cpu_busy_without_logs_is_active(self):
        sample = self.measure({(100, 10): process()}, {(100, 10): process(ticks=100)})
        self.assertEqual(sample['state'], 'active')

    def test_worker_cpu_counts_when_parent_is_idle(self):
        before = {(100, 10): process(), (101, 11): process(ppid=100, start=11)}
        after = copy.deepcopy(before)
        after[(101, 11)]['ticks'] = 100
        self.assertEqual(self.measure(before, after)['state'], 'active')

    def test_io_activity_resets_window_even_without_cpu_or_logs(self):
        sample = self.measure({(100, 10): process()}, {(100, 10): process(counters=(4096, 0, 0, 0))})
        self.assertEqual(sample['state'], 'active')

    def test_gpu_busy_without_cpu_or_logs_is_active(self):
        tree = {(100, 10): process()}
        self.assertEqual(self.measure(tree, tree, gpu={'status': 'active'})['state'], 'active')

    def test_unknown_gpu_is_not_zero(self):
        tree = {(100, 10): process()}
        self.assertEqual(self.measure(tree, tree, gpu={'status': 'unknown'})['state'], 'unknown')

    def test_log_write_and_rotation_count_as_activity(self):
        tree = {(100, 10): process()}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'task.log'
            path.write_text('first')
            self.assertEqual(self.measure(tree, tree, log=path, between=lambda: path.write_text('second'))['state'], 'active')
            replacement = Path(temp)/'new.log'
            replacement.write_text('second')
            self.assertEqual(self.measure(tree, tree, log=path, between=lambda: replacement.replace(path))['state'], 'active')

    def test_missing_log_and_io_permissions_are_unknown(self):
        with patch.object(activity, 'process_tree', side_effect=PermissionError()):
            self.assertEqual(activity.Sampler(100, 10).sample()['state'], 'unknown')
        with patch.object(activity, 'process_tree', return_value={(100, 10): process()}):
            with tempfile.TemporaryDirectory() as temp:
                self.assertEqual(activity.Sampler(100, 10, Path(temp)/'missing').sample()['state'], 'unknown')

    def test_new_worker_is_activity_not_quiet(self):
        before = {(100, 10): process()}
        after = dict(before)
        after[(101, 11)] = process(ppid=100, start=11)
        self.assertEqual(self.measure(before, after)['state'], 'active')

    def test_default_30_minutes_and_opt_in(self):
        args = watch.parse_args(['--name', 'test', '--stall-after', '--', 'true'])
        self.assertEqual(args.stall_after, 1800)
        self.assertIsNone(watch.parse_args(['--name', 'test', '--', 'true']).stall_after)
        for value in ['0', '-1', 'nan', 'inf']:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                watch.parse_args(['--name', 'test', '--stall-after', value, '--', 'true'])

    def test_30_minute_window_dedup_recovery_and_second_episode(self):
        window = activity.QuietWindow(1800, 10)
        for timestamp in range(0, 1800, 10):
            self.assertFalse(window.update({'time': timestamp, 'state': 'quiet'}))
        self.assertTrue(window.update({'time': 1800, 'state': 'quiet'}))
        window.warned = True
        self.assertFalse(window.update({'time': 1810, 'state': 'quiet'}))
        self.assertFalse(window.update({'time': 1820, 'state': 'active'}))
        for timestamp in range(1830, 3630, 10):
            self.assertFalse(window.update({'time': timestamp, 'state': 'quiet'}))
        self.assertTrue(window.update({'time': 3630, 'state': 'quiet'}))

    def test_unknown_or_missing_samples_cannot_accumulate_quiet_time(self):
        window = activity.QuietWindow(10, 1)
        window.update({'time': 0, 'state': 'quiet'})
        window.update({'time': 3, 'state': 'unknown'})
        self.assertFalse(window.update({'time': 5, 'state': 'quiet'}))
        self.assertFalse(window.update({'time': 30, 'state': 'quiet'}))

    def test_gpu_mapping_only_checks_target_devices(self):
        def reply(text):
            return subprocess.CompletedProcess([], 0, stdout=text, stderr='')
        with patch.object(activity.shutil, 'which', return_value='/bin/nvidia-smi'), \
             patch.object(activity.subprocess, 'run', side_effect=[reply('100, GPU-target\n999, GPU-other\n'), reply('GPU-target, 0\nGPU-other, 99\n')]) as run:
            self.assertEqual(activity.gpu_activity({100})['status'], 'idle')
            self.assertTrue(all(call.kwargs['timeout'] == 2 for call in run.call_args_list))
        with patch.object(activity.shutil, 'which', return_value='/bin/nvidia-smi'), \
             patch.object(activity.subprocess, 'run', side_effect=[reply('100, GPU-shared\n999, GPU-shared\n'), reply('GPU-shared, 80\n')]):
            self.assertEqual(activity.gpu_activity({100})['status'], 'active')

    def test_gpu_na_or_timeout_is_unknown(self):
        for failure in [subprocess.TimeoutExpired('nvidia-smi', 2), subprocess.CalledProcessError(1, 'nvidia-smi')]:
            with patch.object(activity.shutil, 'which', return_value='/bin/nvidia-smi'), \
                 patch.object(activity.subprocess, 'run', side_effect=failure):
                self.assertEqual(activity.gpu_activity({100})['status'], 'unknown')
        with patch.object(activity.shutil, 'which', return_value='/bin/nvidia-smi'), \
             patch.object(activity.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 0, stdout='100, GPU-a'), subprocess.CompletedProcess([], 0, stdout='GPU-a, N/A')]):
            self.assertEqual(activity.gpu_activity({100})['status'], 'unknown')

    def test_proc_tree_includes_children_and_checks_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp)
            for pid, parent in [(100, 1), (101, 100), (102, 101), (999, 1)]:
                folder = proc/str(pid)
                folder.mkdir()
                fields = ['S', str(parent)]+['0']*17+[str(pid)]
                fields[11] = '50'
                (folder/'stat').write_text('%s (worker) name) %s' % (pid, ' '.join(fields)))
                (folder/'io').write_text('rchar: 0\nwchar: 0\nread_bytes: 0\nwrite_bytes: 0\n')
            self.assertEqual({pid for pid, _ in activity.process_tree(100, 100, proc)}, {100, 101, 102})
            with self.assertRaises(ValueError):
                activity.process_tree(100, 200, proc)
            (proc/'101/io').unlink()
            with self.assertRaises(OSError):
                activity.process_tree(100, 100, proc)

    def test_warning_payload_does_not_claim_task_exited_or_upload_logs(self):
        result = {'name': 'test', 'host': 'host', 'pid': 100, 'log_path': '/tmp/task.log', 'private_log': 'SECRET'}
        alert = {'detected_at': 'now', 'quiet_seconds': 1800, 'metrics': {'cpu_percent': 0, 'io_changed': False, 'log_changed': False, 'gpu': {'status': 'idle'}}}
        with patch.object(watch, 'urlopen', return_value=io.BytesIO(b'{"code":0}')) as send:
            self.assertEqual(watch.notify_stall('SCTfake', result, alert)['status'], 'accepted')
        data = parse_qs(send.call_args.args[0].data.decode())
        self.assertIn('疑似停滞', data['title'][0])
        self.assertIn('任务仍在运行', data['desp'][0])
        self.assertNotIn('SECRET', data['desp'][0])
        self.assertNotIn('退出码', data['desp'][0])


class StallIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.key = self.root/'test.key'
        self.key.write_text('SCTfakeIntegration')

    def args(self, code, *extra):
        return watch.parse_args(['--name', 'stall-test', '--key-file', str(self.key), '--log-dir', str(self.root/'runs'),
                                 '--stall-after', '0.2', '--stall-interval', '0.05', *extra,
                                 '--', sys.executable, '-u', '-c', code])

    def run_task(self, args, stall_sender=None):
        with patch.object(activity, 'gpu_activity', return_value={'status': 'not_used'}), \
             patch.object(watch, 'notify_stall', side_effect=stall_sender or (lambda *a: {'status': 'accepted'})) as warning, \
             patch.object(watch, 'notify', return_value={'status': 'accepted'}) as final, \
             contextlib.redirect_stdout(io.StringIO()):
            code = watch.run(args)
        result = json.loads(next((self.root/'runs').glob('*/result.json')).read_text())
        return code, result, warning, final

    def test_one_warning_then_final_exit_notification(self):
        code, result, warning, final = self.run_task(self.args('import time; time.sleep(0.9)'))
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(result['stall_monitor']['alerts']), 1)
        self.assertEqual(result['stall_monitor']['alerts'][0]['notification']['status'], 'accepted')
        warning.assert_called_once()
        final.assert_called_once()

    def test_warning_failure_does_not_retry_or_change_exit_code(self):
        code, result, warning, final = self.run_task(self.args('import time; time.sleep(0.9); raise SystemExit(7)'), lambda *a: {'status': 'unknown'})
        self.assertEqual(code, 7)
        warning.assert_called_once()
        final.assert_called_once()
        self.assertEqual(result['stall_monitor']['alerts'][0]['notification']['status'], 'unknown')

    def test_activity_rearms_warning_for_a_new_episode(self):
        source = 'import time; time.sleep(0.7); print("progress",flush=True); time.sleep(0.7)'
        code, result, warning, final = self.run_task(self.args(source))
        self.assertEqual(code, 0)
        self.assertEqual(warning.call_count, 2)
        self.assertEqual(len(result['stall_monitor']['alerts']), 2)
        self.assertIn('activity_resumed_at', result['stall_monitor']['alerts'][0])
        final.assert_called_once()

    def test_active_cpu_prevents_warning_without_logs(self):
        code, result, warning, _ = self.run_task(self.args('import time\nend=time.monotonic()+0.9\nwhile time.monotonic()<end: pass'))
        self.assertEqual(code, 0)
        warning.assert_not_called()
        self.assertEqual(result['stall_monitor']['alerts'], [])

    def test_no_notify_still_records_warning(self):
        code, result, warning, final = self.run_task(self.args('import time; time.sleep(0.9)', '--no-notify'))
        self.assertEqual(code, 0)
        self.assertEqual(result['stall_monitor']['alerts'][0]['notification']['status'], 'disabled')
        warning.assert_not_called()
        final.assert_not_called()

    def test_slow_http_does_not_delay_timeout_signal(self):
        marker = self.root/'signal-time'
        source = ('import signal,time,os\nfrom pathlib import Path\n'
                  'def stop(*a):\n Path(%r).write_text(str(time.monotonic()))\n os._exit(0)\n'
                  'signal.signal(signal.SIGTERM,stop)\ntime.sleep(30)' % str(marker))
        start = time.monotonic()
        def slow(*args):
            time.sleep(1.5)
            return {'status': 'accepted'}
        code, result, warning, _ = self.run_task(self.args(source, '--timeout', '0.7'), slow)
        self.assertEqual(code, 124)
        warning.assert_called_once()
        self.assertLess(float(marker.read_text()) - start, 1.3)
        self.assertLess(result['duration_seconds'], 1.3)

    def test_attach_warning_keeps_target_alive_and_final_result_unknown(self):
        target = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            args = watch.parse_args(['--name', 'attach', '--pid', str(target.pid), '--key-file', str(self.key),
                                     '--log-dir', str(self.root/'runs'), '--stall-after', '0.2', '--stall-interval', '0.05', '--timeout', '0.9'])
            code, result, warning, final = self.run_task(args)
            self.assertEqual(code, 124)
            self.assertEqual(result['status'], 'monitor_timed_out')
            self.assertIsNone(result['returncode'])
            self.assertIsNone(target.poll())
            warning.assert_called_once()
            final.assert_called_once()
        finally:
            target.terminate()
            target.wait()


if __name__ == '__main__':
    unittest.main()
