"""Deterministic regression tests for batched stall-monitor samples."""
import contextlib
import io
import json
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runchime import watch


class StallMonitorBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.result_path = self.root / "result.json"
        self.samples = queue.Queue()
        args = SimpleNamespace(stall_after=10, stall_interval=1, pid=None,
                               no_notify=True)
        self.result = {"pid": 123, "log_path": str(self.root / "output.log")}
        collector = SimpleNamespace(samples=self.samples)
        with patch.object(watch, "Sampler"), \
             patch.object(watch, "Collector", return_value=collector):
            self.monitor = watch.StallMonitor(args, self.result, self.result_path,
                                              None, None, start_ticks=10)
        self.first_alert = {"detected_at": "2026-01-01T00:00:00+00:00",
                            "notification": {"status": "disabled"}}
        self.result["stall_monitor"]["alerts"].append(self.first_alert)
        self.monitor.window.warned = True

    def poll_samples(self, samples, observed_at="2026-01-01T00:01:00+00:00"):
        for sample in samples:
            self.samples.put(sample)
        with patch.object(watch.time, "monotonic", return_value=samples[-1]["time"]), \
             patch.object(watch, "now", return_value=observed_at), \
             patch.object(watch, "read_process", return_value={"start_ticks": 10, "state": "S"}), \
             patch.object(watch, "notify_stall") as send, \
             contextlib.redirect_stdout(io.StringIO()):
            self.monitor.poll()
            send.assert_not_called()
        return json.loads(self.result_path.read_text(encoding="utf-8"))

    def test_intermediate_activity_records_recovery_and_rearms_warning(self):
        recovered_at = "2026-01-01T00:01:00+00:00"
        persisted = self.poll_samples([
            {"time": 10, "state": "active"},
            {"time": 11, "state": "quiet"},
        ], recovered_at)
        alerts = persisted["stall_monitor"]["alerts"]
        self.assertEqual(alerts[0].get("activity_resumed_at"), recovered_at)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(persisted["stall_monitor"]["state"], "quiet")

        persisted = self.poll_samples([
            {"time": timestamp, "state": "quiet"}
            for timestamp in range(12, 22)
        ])
        alerts = persisted["stall_monitor"]["alerts"]
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[0]["activity_resumed_at"], recovered_at)
        self.assertNotIn("activity_resumed_at", alerts[1])
        self.assertEqual(alerts[1]["notification"]["status"], "disabled")

        second_recovery = "2026-01-01T00:02:00+00:00"
        persisted = self.poll_samples([
            {"time": 22, "state": "active"},
            {"time": 23, "state": "quiet"},
        ], second_recovery)
        alerts = persisted["stall_monitor"]["alerts"]
        self.assertEqual(alerts[0]["activity_resumed_at"], recovered_at)
        self.assertEqual(alerts[1]["activity_resumed_at"], second_recovery)

    def test_latest_active_sample_suppresses_earlier_due_warning(self):
        self.result["stall_monitor"]["alerts"].clear()
        self.monitor.window.warned = False
        samples = [{"time": timestamp, "state": "quiet"}
                   for timestamp in range(11)]
        samples.append({"time": 11, "state": "active"})
        persisted = self.poll_samples(samples)
        self.assertEqual(persisted["stall_monitor"]["alerts"], [])
        self.assertEqual(persisted["stall_monitor"]["state"], "active")

    def test_unknown_samples_do_not_claim_activity_resumed(self):
        persisted = self.poll_samples([
            {"time": 10, "state": "unknown"},
            {"time": 11, "state": "quiet"},
        ])
        alerts = persisted["stall_monitor"]["alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertNotIn("activity_resumed_at", alerts[0])
        self.assertTrue(self.monitor.window.warned)


if __name__ == "__main__":
    unittest.main()
