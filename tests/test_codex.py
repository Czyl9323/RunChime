"""Offline regression checks; all HTTP calls are intercepted."""
import contextlib
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/notify_wechat.py"
from runchime import codex as notify
EVENT = {"hook_event_name": "Stop", "session_id": "session", "turn_id": "turn"}


def concurrent_delivery(home, ready, start, results):
    os.environ["CODEX_HOME"] = home
    os.environ["RUNCHIME_HOME"] = str(Path(home)/"config")
    os.environ["RUNCHIME_KEY_FILE"] = ""
    def fake_send(request, timeout):
        with open(Path(home) / "requests", "a") as log:
            log.write("submitted\n")
        return io.BytesIO(b'{"code":0}')
    with patch.object(notify, "urlopen", side_effect=fake_send):
        ready.put(True)
        start.wait(10)
        results.put(notify.handle_event(EVENT))


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "serverchan.key").write_text("SCTtestKey123")
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home), "RUNCHIME_HOME": str(self.home/"config"), "RUNCHIME_KEY_FILE": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.http = patch.object(notify, "urlopen", side_effect=lambda *a, **k: io.BytesIO(b'{"code":0}'))
        self.send = self.http.start()
        self.addCleanup(self.http.stop)

    def invoke(self, event):
        output = io.StringIO()
        with patch.object(sys, "argv", [str(SCRIPT)]), patch.object(sys, "stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stdout(output):
            notify.main()
        return json.loads(output.getvalue())

    def test_filters_before_key_or_state_access(self):
        (self.home / "serverchan.key").unlink()
        for event in [{"hook_event_name": "SubagentStop", "agent_id": "child"}, {"type": "agent-turn-complete"}, {"hook_event_name": "SessionEnd"}, dict(EVENT, agent_id="child"), {}, [], None]:
            self.assertEqual(self.invoke(event), {})
        self.send.assert_not_called()
        self.assertFalse((self.home / "wechat-notify-state").exists())

    def test_real_cli_legacy_and_subagent_output(self):
        for args, data in [([], '{"hook_event_name":"SubagentStop"}'), ([], '{"type":"agent-turn-complete"}'), (['{"type":"agent-turn-complete"}'], '')]:
            result = subprocess.run([sys.executable, str(SCRIPT), *args], input=data, text=True, capture_output=True, timeout=3, check=True)
            self.assertEqual(result.stdout, '{}\n')
            self.assertEqual(result.stderr, '')

    def test_duplicate_and_separate_turns_sessions(self):
        for event in [EVENT, EVENT, dict(EVENT, turn_id="second"), EVENT, dict(EVENT, session_id="other")]:
            self.assertEqual(self.invoke(event), {})
        self.assertEqual(self.send.call_count, 3)
        claims = list((self.home / "wechat-notify-state").glob('*/*.claimed'))
        self.assertEqual(len(claims), 3)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in claims))

    def test_private_payload_and_timeout(self):
        self.invoke(dict(EVENT, last_assistant_message="PRIVATE_REPLY", cwd="PRIVATE_PATH", prompt="PRIVATE_PROMPT"))
        request = self.send.call_args.args[0]
        data = parse_qs(request.data.decode())
        self.assertEqual(set(data), {"title", "desp"})
        self.assertNotIn("PRIVATE", str(data))
        self.assertEqual(request.full_url, 'https://sctapi.ftqq.com/SCTtestKey123.send')
        self.assertEqual(self.send.call_args.kwargs['timeout'], 8)

    def test_timeout_warns_without_leaking_or_retrying(self):
        self.send.side_effect = TimeoutError("https://sctapi.ftqq.com/SCTtestKey123.send")
        result = self.invoke(EVENT)
        self.assertEqual(set(result), {"systemMessage"})
        self.assertNotIn("SCTtestKey123", str(result))
        self.assertEqual(self.invoke(EVENT), {})
        self.send.assert_called_once()

    def test_server_rejections_and_bad_responses(self):
        for i, response in enumerate([b'{"code":1}', b'{}', b'[]', b'bad json', b'{"code":false}']):
            self.send.side_effect = lambda *a, **k: io.BytesIO(response)
            event = dict(EVENT, turn_id=str(i))
            self.assertEqual(set(self.invoke(event)), {"systemMessage"})
            self.assertEqual(self.invoke(event), {})
        self.assertEqual(self.send.call_count, 5)

    def test_bad_input_key_and_state_are_advisory(self):
        for event in [dict(EVENT, session_id=""), dict(EVENT, turn_id=None)]:
            self.assertEqual(set(self.invoke(event)), {"systemMessage"})
        (self.home / "serverchan.key").unlink()
        self.assertEqual(set(self.invoke(EVENT)), {"systemMessage"})
        (self.home / "serverchan.key").write_text('invalid')
        self.assertEqual(set(self.invoke(EVENT)), {"systemMessage"})
        (self.home / "serverchan.key").write_text('SCTtestKey123')
        (self.home / "wechat-notify-state").write_text('blocked')
        self.assertEqual(set(self.invoke(EVENT)), {"systemMessage"})
        self.send.assert_not_called()
        result = subprocess.run([sys.executable, str(SCRIPT)], input='{bad', text=True, capture_output=True, timeout=3, check=True)
        self.assertEqual(set(json.loads(result.stdout)), {"systemMessage"})
        self.assertEqual(result.stderr, '')

    def test_concurrent_processes_submit_once(self):
        ctx = multiprocessing.get_context('fork')
        ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=concurrent_delivery, args=(str(self.home), ready, start, results)) for _ in range(12)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                ready.get(timeout=10)
            start.set()
            for _ in processes:
                self.assertEqual(results.get(timeout=10), {})
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual((self.home / 'requests').read_text(), 'submitted\n')
            self.assertEqual(self.invoke(EVENT), {})
            self.send.assert_not_called()
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()


if __name__ == '__main__':
    unittest.main()
