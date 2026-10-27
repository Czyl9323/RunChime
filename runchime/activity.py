"""Conservative Linux process-tree activity sampling; no task signals or log reads.

CPU fields: https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html
I/O fields: https://man7.org/linux/man-pages/man5/proc_pid_io.5.html
GPU queries: https://docs.nvidia.com/deploy/nvidia-smi/index.html
"""
import csv
import os
from pathlib import Path
import queue
import shutil
import stat
import subprocess
import threading
import time


def process_stat(path):
    text = path.read_text()
    closing = text.rfind(")")
    if closing < 0:
        raise ValueError("invalid process stat")
    fields = text[closing + 1:].split()
    return {"ppid": int(fields[1]), "state": fields[0],
            "ticks": int(fields[11]) + int(fields[12]), "start": int(fields[19])}


def process_tree(pid, start, proc=Path("/proc")):
    processes = {}
    for directory in proc.iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            processes[int(directory.name)] = process_stat(directory / "stat")
        except (OSError, ValueError, IndexError):
            continue  # Unrelated, inaccessible processes do not become targets.
    root = processes.get(pid)
    if root is None or root["start"] != start or root["state"] in ("Z", "X", "x"):
        raise ValueError("target identity unavailable")
    selected = {pid}
    while True:
        children = {p for p, item in processes.items() if item["ppid"] in selected}
        expanded = selected | children
        if expanded == selected:
            break
        selected = expanded
    result = {}
    for p in selected:
        item = processes[p]
        if item["state"] in ("Z", "X", "x"):
            continue
        data = dict(line.split(":", 1) for line in (proc / str(p) / "io").read_text().splitlines())
        item["io"] = tuple(int(data[k]) for k in ("rchar", "wchar", "read_bytes", "write_bytes"))
        # Ensure I/O and CPU belong to the same process incarnation.
        if process_stat(proc / str(p) / "stat")["start"] != item["start"]:
            raise ValueError("process identity changed while sampling")
        result[(p, item["start"])] = item
    return result


def gpu_activity(pids):
    executable = shutil.which("nvidia-smi")
    if executable is None:
        # A driver without the monitoring utility cannot be assumed idle.
        return {"status": "unknown" if Path("/proc/driver/nvidia/gpus").exists() else "no_nvidia"}

    def query(fields, kind):
        result = subprocess.run([executable, "--query-" + kind + "=" + fields,
                                 "--format=csv,noheader,nounits"], capture_output=True,
                                text=True, timeout=2, check=True)
        return list(csv.reader(result.stdout.splitlines(), skipinitialspace=True))
    try:
        devices = set()
        for row in query("pid,gpu_uuid", "compute-apps"):
            if not row:
                continue
            if len(row) != 2:
                raise ValueError("invalid GPU process response")
            if int(row[0]) in pids:
                devices.add(row[1].strip())
        if not devices:
            return {"status": "not_used"}
        values = {}
        for row in query("uuid,utilization.gpu", "gpu"):
            if row and row[0].strip() in devices:
                percent = float(row[1])
                if not 0 <= percent <= 100:
                    raise ValueError("unknown GPU utilization")
                values[row[0].strip()] = percent
        if set(values) != devices:
            raise ValueError("GPU utilization unavailable")
        return {"status": "active" if any(values.values()) else "idle",
                "device_percent": values, "scope": "device"}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {"status": "unknown"}


class Sampler:
    def __init__(self, pid, start, log_path=None):
        self.pid, self.start = pid, start
        self.log_path = Path(log_path) if log_path is not None else None
        self.previous = None
        self.hz = os.sysconf("SC_CLK_TCK")

    def sample(self):
        try:
            processes = process_tree(self.pid, self.start)
            log_signature = None
            if self.log_path is not None:
                info = self.log_path.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("log is not a regular file")
                log_signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            gpu = gpu_activity({pid for pid, _ in processes})
            current_time = time.monotonic()
            snapshot = (current_time, processes, log_signature)
            previous, self.previous = self.previous, snapshot
            metrics = {"time": current_time, "process_count": len(processes), "gpu": gpu,
                       "cpu_percent": None, "io_changed": None, "log_changed": None,
                       "state": "unknown"}
            if previous is None:
                metrics["reason"] = "collecting_baseline"
                return metrics
            old_time, old_processes, old_log = previous
            if set(processes) != set(old_processes):
                metrics.update(state="active", reason="process_tree_changed")
                return metrics
            elapsed = current_time - old_time
            if elapsed <= 0:
                raise ValueError("invalid sample interval")
            ticks = sum(item["ticks"] - old_processes[p]["ticks"] for p, item in processes.items())
            if ticks < 0:
                raise ValueError("CPU counters decreased")
            metrics.update(cpu_percent=round(100 * ticks / self.hz / elapsed, 3),
                           io_changed=any(item["io"] != old_processes[p]["io"] for p, item in processes.items()),
                           log_changed=(log_signature != old_log) if self.log_path else None)
            if metrics["cpu_percent"] >= 1 or metrics["io_changed"] or metrics["log_changed"] or gpu["status"] == "active":
                metrics["state"] = "active"
            elif gpu["status"] == "unknown":
                metrics["reason"] = "gpu_metrics_unavailable"
            else:
                metrics["state"] = "quiet"
            return metrics
        except (OSError, ValueError, KeyError, IndexError):
            self.previous = None
            return {"time": time.monotonic(), "state": "unknown", "reason": "process_or_log_metrics_unavailable"}


class QuietWindow:
    """One warning per quiet episode; unknown samples cannot prove a stall."""
    def __init__(self, threshold, interval):
        self.threshold, self.interval = threshold, interval
        self.since = self.last_time = None
        self.warned = False

    def update(self, sample):
        timestamp = sample["time"]
        if self.last_time is not None and timestamp - self.last_time > max(3 * self.interval, 5):
            self.since = None
        self.last_time = timestamp
        if sample["state"] != "quiet":
            self.since = None
            if sample["state"] == "active":
                self.warned = False
            return False
        if self.since is None:
            self.since = timestamp
        return not self.warned and timestamp - self.since >= self.threshold


class Collector:
    """Slow GPU subprocesses must never delay task timeouts or signal handling."""
    def __init__(self, sampler, interval):
        self.samples = queue.Queue()
        self.stopped = threading.Event()
        self.sampler, self.interval = sampler, interval
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                self.samples.put(self.sampler.sample())
            except Exception:
                self.samples.put({"time": time.monotonic(), "state": "unknown", "reason": "sampling_failed"})
            self.stopped.wait(self.interval)

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=5)
