#!/usr/bin/env python3
"""Run a foreground training/evaluation command and notify WeChat on exit.

Examples (Linux, Python >= 3.8; standard library only):
  python3 scripts/watch_run.py --name train -- python -u train.py
  python3 scripts/watch_run.py --name eval -- bash -o pipefail evaluate.sh
  python3 scripts/watch_run.py --name existing-train --pid 12345 --task-log train.log
  nohup python3 scripts/watch_run.py --name train -- python -u train.py >watch.log 2>&1 &

Each invocation creates a private run directory containing output.log and
result.json. Use tail -f on output.log for live output. Commands must stay in
the foreground: do not pass a shell script that backgrounds work and exits.
For shell pipelines use bash -o pipefail. A zero exit code means process
success, not a guarantee of model quality or complete output artifacts.

Attach mode (--pid, Linux only) watches an existing process using /proc and
its start time to detect PID reuse. It never signals or kills that process.
Exit/zombie detection reports "exited_unknown", with returncode=null; monitor
exit code 0 means observation completed, NOT that the task succeeded. --timeout
or interrupt stops only the monitor. --task-log records an existing log path,
without reading, changing, or inferring success from its contents. Monitor the
foreground launcher PID (e.g. torchrun), not an individual distributed worker.

Configure your own SendKey with `runchime configure`; see README for key precedence.
Notifications contain only name, host, status, time, exit code and log path;
command arguments and log contents are never sent. Each notification has one HTTP attempt,
with no retries. Server acceptance does not guarantee WeChat delivery.
Host outages or SIGKILL of this monitor require external heartbeat monitoring.

--stall-after enables advisory low-activity warnings (1800 seconds if no value
is supplied). Process-tree CPU/I/O, log metadata, and associated NVIDIA GPU
activity are checked together. Unknown metrics suppress warnings. One warning
is attempted per quiet episode, in addition to the final exit notification.
"""

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import key_path, read_key
from .activity import Collector, QuietWindow, Sampler


LABELS = {
    "running": "运行中", "succeeded": "正常结束", "failed": "异常退出",
    "terminated": "信号终止", "interrupted": "监控收到终止信号",
    "timed_out": "超时终止", "launch_failed": "启动失败",
    "exited_unknown": "进程已退出，结果未知",
    "attach_failed": "附加监控失败", "observation_failed": "进程状态读取失败",
    "monitor_stopped": "附加监控已停止", "monitor_timed_out": "附加监控超时停止",
}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def save_result(path, result):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def notify(key, result):
    label = LABELS[result["status"]]
    fields = [
        ("任务", result["name"]), ("服务器", result["host"]),
        ("状态", label), ("监控开始" if result.get("mode") == "attach" else "开始", result["started_at"]),
        ("结束", result["finished_at"]),
        ("监控时长（秒）" if result.get("mode") == "attach" else "耗时（秒）", result["duration_seconds"]),
        ("进程退出码", result["returncode"] if result["returncode"] is not None else "未知"),
        ("脚本退出码", result["exit_code"]),
        ("信号", "未知" if result.get("mode") == "attach" else result.get("signal") or "无"),
        ("日志", result["log_path"]),
    ]
    if result.get("mode") == "attach":
        fields.extend([
            ("监控 PID", result["pid"]),
            ("说明", "附加模式无法取得任务退出码；脚本退出码不代表任务成功。停止监控不会终止任务。"),
        ])
        if result.get("task_log_path"):
            fields.append(("任务日志", result["task_log_path"]))
    return send_message(key, "任务%s：%s" % (label, result["name"]), fields)


def notify_stall(key, result, alert):
    sample = alert["metrics"]
    gpu = sample.get("gpu", {})
    gpu_text = {"idle": "关联 NVIDIA GPU 的设备利用率为 0%（整卡指标）",
                "not_used": "未发现目标进程树使用 NVIDIA GPU",
                "no_nvidia": "未检测到 NVIDIA 驱动/工具，未检测其他加速器",
                "active": "GPU 仍有活动", "unknown": "未知"}.get(gpu.get("status"), "未知")
    fields = [
        ("任务", result["name"]), ("服务器", result["host"]), ("PID", result["pid"]),
        ("预警时间", alert["detected_at"]), ("持续低活动（秒）", alert["quiet_seconds"]),
        ("进程树 CPU（单核百分比）", sample.get("cpu_percent")),
        ("I/O 活动", "无新增读写" if sample.get("io_changed") is False else "未知"),
        ("日志活动", "未变化" if sample.get("log_changed") is False else "未提供日志"),
        ("GPU 活动", gpu_text),
        ("日志位置", result.get("task_log_path") or result["log_path"]),
        ("说明", "任务仍在运行，但持续低活动，疑似停滞；正常等待也可能出现此状态。仅提醒，未终止任务。"),
    ]
    return send_message(key, "任务疑似停滞：" + result["name"], fields)


def send_message(key, title, fields):
    body = urlencode({
        "title": title[:80],
        "desp": "\n\n".join("%s：%s" % pair for pair in fields),
    }).encode("utf-8")
    request = Request("https://sctapi.ftqq.com/%s.send" % key, data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urlopen(request, timeout=10) as response:
            reply = json.load(response)
    except Exception:
        # Never expose exceptions that may contain the SendKey in the URL.
        return {"status": "unknown", "message": "请求失败或响应无效，是否已受理不确定；不重试"}
    if not isinstance(reply, dict) or type(reply.get("code")) is not int or reply["code"] != 0:
        return {"status": "rejected", "message": "Server酱未返回 code=0；不重试"}
    return {"status": "accepted", "message": "Server酱已受理"}


def positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的有限秒数")
    return number


def positive_pid(value):
    pid = int(value)
    if pid <= 0:
        raise argparse.ArgumentTypeError("PID 必须是正整数")
    return pid


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True, help="任务名称，会出现在微信中")
    parser.add_argument("--log-dir", type=Path, default=Path("logs/runchime"), help="日志根目录（默认 logs/runchime）")
    parser.add_argument("--key-file", type=Path, help="Server酱密钥文件，不要把密钥本身放进命令行")
    parser.add_argument("--no-notify", action="store_true", help="仅记录结果，不发送微信")
    parser.add_argument("--pid", type=positive_pid, help="附加监控现有 PID，不启动或终止任务")
    parser.add_argument("--task-log", type=Path, help="附加模式下已有任务日志的路径，仅记录位置")
    parser.add_argument("--interval", type=positive_number, default=1, help="附加模式检查间隔秒数（默认 1）")
    parser.add_argument("--stall-after", nargs="?", type=positive_number, const=1800, metavar="SECONDS",
                        help="启用多指标低活动预警；省略秒数时为 1800 秒（30 分钟），不终止任务")
    parser.add_argument("--stall-interval", type=positive_number, default=10,
                        help="滞留指标采样间隔秒数（默认 10）")
    parser.add_argument("--timeout", type=positive_number, help="最长秒数；启动模式终止任务，附加模式仅停止监控")
    parser.add_argument("--kill-after", type=positive_number, help="启动模式终止宽限秒数，之后 SIGKILL（默认 10）")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- 后的前台任务命令")
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if bool(args.command) == (args.pid is not None):
        parser.error("请选择 --pid 或 -- 后的任务命令，不能同时使用")
    if args.pid is not None:
        if not sys.platform.startswith("linux"):
            parser.error("--pid 附加模式需要 Linux /proc")
        if args.pid == os.getpid():
            parser.error("不能监控脚本自身的 PID")
        if args.kill_after is not None:
            parser.error("--pid 不支持 --kill-after：附加模式不会终止任务")
    elif args.task_log is not None:
        parser.error("--task-log 仅用于 --pid 附加模式")
    if args.kill_after is None:
        args.kill_after = 10
    if not args.name.strip():
        parser.error("任务名称不能为空")
    if os.name != "posix":
        parser.error("本脚本需要 Linux/POSIX 进程组支持")
    return args


def prepare_run(args, mode):
    key, key_error = None, None
    if not args.no_notify:
        try:
            key = read_key(key_path(args.key_file))
        except ValueError as exc:
            key_error = str(exc)
            print("[watch_run] 微信通知不可用：" + key_error, file=sys.stderr, flush=True)

    args.log_dir.expanduser().mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=str(args.log_dir.expanduser()))).resolve()
    log_path, result_path = directory / "output.log", directory / "result.json"
    started = time.monotonic()
    result = {
        "name": args.name, "host": socket.gethostname(), "status": "running",
        "started_at": now(), "monitor_pid": os.getpid(), "pid": args.pid,
        "mode": mode,
        "log_path": str(log_path), "notification": {"status": "pending"},
    }
    if args.task_log is not None:
        result["task_log_path"] = str(args.task_log.expanduser().resolve())
    print("[watch_run] 日志：%s\n[watch_run] 结果：%s" % (log_path, result_path), flush=True)
    return result, result_path, log_path, started, key, key_error


def finish_run(args, result, result_path, started, key, key_error):
    result.setdefault("finished_at", now())
    result.setdefault("duration_seconds", round(time.monotonic() - started, 3))
    if args.no_notify:
        result["notification"] = {"status": "disabled"}
    elif key_error:
        result["notification"] = {"status": "configuration_error", "message": key_error}
    else:
        # Persist before submission; an interrupted request is never retried.
        result["notification"] = {"status": "submitting", "attempted_at": now()}
        save_result(result_path, result)
        result["notification"].update(notify(key, result))
    save_result(result_path, result)
    print("[watch_run] %s，脚本退出码 %s；通知：%s" %
          (LABELS[result["status"]], result["exit_code"], result["notification"]["status"]), flush=True)
    return result["exit_code"]


class StallMonitor:
    """Collect metrics and send advisory warnings without blocking the task loop."""
    def __init__(self, args, result, result_path, key, key_error, start_ticks):
        self.args, self.result, self.result_path = args, result, result_path
        self.key, self.key_error = key, key_error
        self.start_ticks = start_ticks
        self.window = QuietWindow(args.stall_after, args.stall_interval)
        task_log = result.get("task_log_path") if args.pid else result["log_path"]
        self.collector = Collector(Sampler(result["pid"], start_ticks, task_log), args.stall_interval)
        self.responses = queue.Queue()
        self.sender = None
        result["stall_monitor"] = {"threshold_seconds": args.stall_after, "sample_interval_seconds": args.stall_interval,
                                   "state": "collecting_baseline", "alerts": []}
        save_result(result_path, result)

    def _deliver(self, alert):
        try:
            response = notify_stall(self.key, self.result, alert)
        except Exception:
            response = {"status": "unknown", "message": "预警请求失败；不自动重试"}
        self.responses.put((alert, response))

    def _drain_responses(self):
        while True:
            try:
                alert, response = self.responses.get_nowait()
            except queue.Empty:
                return
            alert["notification"].update(response)
            save_result(self.result_path, self.result)

    def poll(self):
        self._drain_responses()
        record = self.result["stall_monitor"]
        # Only the most recent metrics may trigger a notification. Discarding
        # intermediate samples would hide activity, so feed all into the window.
        sample, due = None, False
        while True:
            try:
                sample = self.collector.samples.get_nowait()
            except queue.Empty:
                break
            due = self.window.update(sample)
            # Recovery may occur before a later quiet sample in this batch.
            if sample["state"] == "active" and record["alerts"]:
                record["alerts"][-1].setdefault("activity_resumed_at", now())
        if sample is None:
            return
        if time.monotonic() - sample["time"] > max(3 * self.args.stall_interval, 5):
            sample = {"time": time.monotonic(), "state": "unknown", "reason": "stale_sample"}
            due = self.window.update(sample)
        record.update(state=sample["state"], last_sample_at=now(), metrics=sample)
        if due and (self.sender is None or not self.sender.is_alive()):
            try:
                current = read_process(self.result["pid"])
                if process_end_reason({"start_ticks": self.start_ticks}, current):
                    return
            except (OSError, ValueError, IndexError):
                return
            self.window.warned = True
            alert = {"detected_at": now(), "quiet_seconds": round(sample["time"] - self.window.since, 3),
                     "metrics": sample, "notification": {"status": "submitting"}}
            if self.args.no_notify:
                alert["notification"] = {"status": "disabled"}
            elif self.key_error:
                alert["notification"] = {"status": "configuration_error", "message": self.key_error}
            record["alerts"].append(alert)
            # Save the attempt before starting HTTP; never retry this episode.
            save_result(self.result_path, self.result)
            print("[watch_run] 任务仍在运行，持续低活动 %.1f 秒，疑似停滞（仅提醒）。" % alert["quiet_seconds"], flush=True)
            if alert["notification"]["status"] == "submitting":
                self.sender = threading.Thread(target=self._deliver, args=(alert,), daemon=True)
                self.sender.start()
        save_result(self.result_path, self.result)

    def close(self):
        self.collector.close()
        # This happens after task exit/monitor stop, never before enforcing a
        # timeout or forwarding a signal. Give in-flight HTTP a bounded finish.
        if self.sender is not None:
            self.sender.join(timeout=11)
        self._drain_responses()
        record = self.result["stall_monitor"]
        for alert in record["alerts"]:
            if alert["notification"]["status"] == "submitting":
                alert["notification"] = {"status": "unknown", "message": "监控已结束，请求结果不明确；不重试"}
        record["stopped_at"] = now()
        save_result(self.result_path, self.result)


def read_process(pid):
    """Return Linux process identity/state, or None if it no longer exists."""
    try:
        text = Path("/proc/%s/stat" % pid).read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # comm may contain spaces, parentheses or newlines. Fields after its final
    # ')' begin with field 3 (state); starttime is field 22, index 19 here.
    closing = text.rfind(")")
    if closing < 0:
        raise ValueError("invalid proc stat")
    fields = text[closing + 1:].split()
    if len(fields) < 20:
        raise ValueError("incomplete proc stat")
    return {"state": fields[0], "start_ticks": int(fields[19])}


def process_end_reason(initial, current):
    if current is None:
        return "process_missing"
    if current["start_ticks"] != initial["start_ticks"]:
        return "pid_reused"
    if current["state"] in ("Z", "X", "x"):
        return "process_exited"
    return None


def run_attached(args):
    result, result_path, log_path, started, key, key_error = prepare_run(args, "attach")
    result.update(returncode=None, exit_code=2)
    caught = []
    stall = None
    handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous = signal.getsignal(signum)
        if signum == signal.SIGHUP and previous == signal.SIG_IGN:
            continue
        handlers[signum] = signal.signal(signum, lambda number, frame: caught.append(number))
    try:
        with log_path.open("w", encoding="utf-8", buffering=1) as log:
            try:
                initial = read_process(args.pid)
                if initial is None or initial["state"] in ("Z", "X", "x"):
                    result.update(status="attach_failed", reason="not_running_at_attach")
                else:
                    result["process_start_ticks"] = initial["start_ticks"]
                    save_result(result_path, result)
                    if args.stall_after is not None:
                        stall = StallMonitor(args, result, result_path, key, key_error, initial["start_ticks"])
                    log.write("%s attached pid=%s start_ticks=%s\n" % (now(), args.pid, initial["start_ticks"]))
                    print("[watch_run] 已附加 PID %s；只观察进程，不发送终止信号。" % args.pid, flush=True)
                    next_check = time.monotonic()
                    while True:
                        if caught:
                            result.update(status="monitor_stopped", exit_code=128 + caught[0],
                                          monitor_signal=signal.Signals(caught[0]).name)
                            break
                        current_time = time.monotonic()
                        if current_time >= next_check:
                            reason = process_end_reason(initial, read_process(args.pid))
                            if reason:
                                result.update(status="exited_unknown", exit_code=0, reason=reason)
                                break
                            next_check = current_time + args.interval
                        if args.timeout is not None and current_time - started >= args.timeout:
                            result.update(status="monitor_timed_out", exit_code=124)
                            break
                        if stall is not None:
                            stall.poll()
                        # Short sleep keeps signals responsive even with a long interval.
                        time.sleep(min(0.1, max(0, next_check - time.monotonic())))
            except (OSError, ValueError) as exc:
                result.update(status="observation_failed", exit_code=2, error=type(exc).__name__)
            log.write("%s %s\n" % (now(), LABELS[result["status"]]))
        result.update(finished_at=now(), duration_seconds=round(time.monotonic() - started, 3))
        if stall is not None:
            stall.close()
            stall = None
        return finish_run(args, result, result_path, started, key, key_error)
    finally:
        if stall is not None:
            stall.close()
        for signum, previous in handlers.items():
            signal.signal(signum, previous)


def run(args):
    if args.pid is not None:
        return run_attached(args)
    result, result_path, log_path, started, key, key_error = prepare_run(args, "launch")
    save_result(result_path, result)
    caught = []
    def catch_signal(signum, frame):
        caught.append(signum)
    old_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous = signal.getsignal(signum)
        # Preserve nohup's ignored SIGHUP.
        if signum == signal.SIGHUP and previous == signal.SIG_IGN:
            continue
        old_handlers[signum] = signal.signal(signum, catch_signal)
    process = None
    stall = None
    timed_out, stopping, escalated = False, None, False
    def send_group(signum):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
    try:
        with log_path.open("wb", buffering=0) as log:
            try:
                env = os.environ.copy()
                env.setdefault("PYTHONUNBUFFERED", "1")
                process = subprocess.Popen(args.command, stdout=log, stderr=subprocess.STDOUT,
                                           env=env, start_new_session=True)
            except OSError as exc:
                result.update(status="launch_failed", returncode=None, exit_code=127,
                              error="%s (errno=%s)" % (type(exc).__name__, exc.errno))
                log.write((result["error"] + "\n").encode())
            if process is not None:
                result["pid"] = process.pid
                save_result(result_path, result)
                if args.stall_after is not None:
                    initial = read_process(process.pid)
                    if initial is not None:
                        stall = StallMonitor(args, result, result_path, key, key_error, initial["start_ticks"])
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    if stopping is None and (caught or (args.timeout is not None and elapsed >= args.timeout)):
                        timed_out = not caught
                        stopping = time.monotonic()
                        send_group(signal.SIGTERM if timed_out else caught[0])
                    if stopping is not None and not escalated and (time.monotonic() - stopping >= args.kill_after or len(caught) > 1):
                        send_group(signal.SIGKILL)
                        escalated = True
                    if stall is not None and stopping is None:
                        stall.poll()
                    time.sleep(0.1)
                code = process.wait()
                if stopping is not None:
                    # Do not leave children behind if the group leader exited first.
                    send_group(signal.SIGKILL)
                if timed_out:
                    status, exit_code = "timed_out", 124
                elif caught:
                    status, exit_code = "interrupted", 128 + caught[0]
                elif code < 0:
                    status, exit_code = "terminated", 128 - code
                else:
                    status, exit_code = ("succeeded" if code == 0 else "failed"), code
                result.update(status=status, returncode=code, exit_code=exit_code)
                if code < 0 or caught:
                    result["signal"] = signal.Signals(caught[0] if caught else -code).name
        result.update(finished_at=now(), duration_seconds=round(time.monotonic() - started, 3))
        if stall is not None:
            stall.close()
            stall = None
        return finish_run(args, result, result_path, started, key, key_error)
    finally:
        if process is not None and process.poll() is None:
            send_group(signal.SIGKILL)
            process.wait()
        if stall is not None:
            stall.close()
        for signum, previous in old_handlers.items():
            signal.signal(signum, previous)


def main(argv=None):
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
