# RunChime 🔔

**English** | [简体中文](README.zh-CN.md)

**Get a WeChat notification when your training job, evaluation, or Codex turn ends.**

RunChime is a lightweight Python CLI that sends notifications through **ServerChan Turbo (Server酱 Turbo)**. No changes to your training code and no third-party runtime dependencies are required.

| Use case | Command | What it detects |
| --- | --- | --- |
| Start a new training job, evaluation, or script | `runchime run` | Successful exit, nonzero exit, signal termination, and timeout |
| Watch a task that is already running | `runchime attach --pid PID` | The selected process has exited; its exit code is unavailable |
| Notify when a Codex main-thread turn ends | `runchime codex install` | `Stop` events; ignores subagent and legacy notification events |

Supports Linux and Python 3.8+. Attach mode uses Linux `/proc`; Windows is not currently supported. GPU workloads need no extra dependencies: RunChime watches processes without using the GPU.

## Notification examples

Notification detail pages opened from WeChat, delivered through ServerChan. The first two screenshots show short test tasks; the third shows a Codex main-conversation turn ending. The screenshots are in Chinese; click an image to enlarge it.

<table>
  <tr>
    <th>Normal exit</th>
    <th>Nonzero exit</th>
    <th>Codex turn ended</th>
  </tr>
  <tr>
    <td width="33%" valign="top" align="center">
      <a href="docs/images/task-success.png"><img src="docs/images/task-success.png" width="270" alt="WeChat notification showing a test task exiting normally with code 0, duration, and log path"></a>
    </td>
    <td width="33%" valign="top" align="center">
      <a href="docs/images/task-failure.png"><img src="docs/images/task-failure.png" width="270" alt="WeChat notification showing a test task exiting with code 1, duration, and log path"></a>
    </td>
    <td width="33%" valign="top" align="center">
      <a href="docs/images/codex-turn-ended.png"><img src="docs/images/codex-turn-ended.png" width="270" alt="WeChat notification stating that the current Codex main-conversation turn has ended"></a>
    </td>
  </tr>
  <tr>
    <td valign="top">Exit code <code>0</code>, with duration and log location.</td>
    <td valign="top">Exit code <code>1</code>, with duration and log location.</td>
    <td valign="top">One main-conversation turn has ended; this does not mean the whole project is complete.</td>
  </tr>
</table>

## Quick start

### 1. Install

```bash
git clone https://github.com/Czyl9323/RunChime.git
cd RunChime
python3 -m venv ~/.venvs/runchime
~/.venvs/runchime/bin/python -m pip install .
export PATH="$HOME/.venvs/runchime/bin:$PATH"
```

Add the last line to `~/.bashrc` or `~/.zshrc` to make the command available in future terminals. RunChime's environment does not need PyTorch: commands such as `python` and `torchrun` are resolved from your current shell environment. Activate your training environment before running RunChime.

If you already use pipx, you can install with `pipx install .`. The project is not published on PyPI yet; install from this repository.

### 2. Configure your own ServerChan key

1. Visit [ServerChan Turbo](https://sct.ftqq.com/), follow the provider's instructions to configure a WeChat delivery channel, and obtain a **SendKey starting with `SCT`**.
2. Run the following **on the machine and under the account that will run your tasks or Codex**:

```bash
runchime configure
```

Enter your own SendKey at the prompt. Input is hidden. The key is saved to `~/.config/runchime/serverchan.key` by default, with file permissions `0600`. This repository does not provide a shared key, and you do not need to put your key in the source code.

```bash
runchime doctor   # Check local configuration without sending a notification
runchime test     # Send one test notification; uses your provider's quota
```

Existing key files are not silently overwritten. Use `runchime configure --force` to replace a key. Do not post keys in issues, commit them to Git, or include them directly in shell command history. `.gitignore` excludes keys, logs, runtime state, and backup files.

### 3. Run a task

```bash
runchime run --name "Model training" -- python -u train.py
runchime run --name "Model evaluation" -- bash -o pipefail evaluate.sh
```

Everything after `--` is your original command and its arguments. Task output is written to a log; the log and result paths are printed at startup. RunChime sends one notification when the task ends and returns the task's exit code.

## Common workflows

### Run in the background after disconnecting SSH

```bash
nohup runchime run --name "Experiment A" \
  --log-dir "$HOME/run-logs" \
  -- python -u train.py --epochs 100 \
  > monitor.log 2>&1 &

echo $!  # PID of the RunChime monitor
```

You can also run it inside `tmux`. The wrapped task must stay in the foreground: do not detach the actual work inside your script using `nohup`, `&`, or another mechanism. For distributed training, wrap the launcher, such as `torchrun`, rather than an individual worker.

For pipelines or redirection, invoke a shell explicitly and enable `pipefail`, so a successful final command does not hide an earlier training failure:

```bash
runchime run --name "Training pipeline" -- \
  bash -o pipefail -c 'python -u train.py | tee train.log'
```

### Attach to an existing process

Find the PID of your training launcher or evaluation process, then run:

```bash
nohup runchime attach --name "Existing training job" \
  --pid 12345 \
  --task-log /path/to/train.log \
  > monitor-12345.log 2>&1 &
```

Replace the PID and path with your own values. `--task-log` is optional: it records the existing log's location without reading or modifying its contents or inferring whether training succeeded.

- When the process exits, the notification reports **“Process exited; result unknown.”** The result's `returncode` is `null`.
- The monitor returns `0` to indicate that observation completed. **This does not mean the task succeeded.**
- Both the PID and process start time are checked to avoid accidentally following a different process after PID reuse.
- Stopping the monitor, sending it a termination signal, or reaching its timeout never sends a termination signal to the watched task.
- Only the selected process is watched. Children that outlive it are outside the scope of attach mode.

Use `runchime run` for your next task if you need an accurate success or failure result.

### Timeouts and offline checks

```bash
# Run mode: terminate the task after one hour; force termination after a 10-second grace period if needed
runchime run --name "Timed evaluation" --timeout 3600 --kill-after 10 -- python eval.py

# Attach mode: stop monitoring after one hour; leave the task running
runchime attach --name "Watch training" --pid 12345 --timeout 3600

# No ServerChan request and no key required
runchime run --name "Offline test" --no-notify -- python -c 'print("hello")'
```

In attach mode, use `--interval 5` to change the polling interval; the default is one second. See `runchime run --help` and `runchime attach --help` for all options.

## Codex completion notifications

### Install the Stop hook

Configure your key first, then run:

```bash
runchime codex install
```

The installer merges the RunChime handler into `$CODEX_HOME/hooks.json`, or `~/.codex/hooks.json` when `CODEX_HOME` is unset. It preserves other hooks and creates timestamped backups before changing existing files. Repeated installation does not add duplicate handlers. The hook uses absolute paths to the current Python interpreter and installation, so it does not depend on the Codex session's working directory or PATH.

**Restart Codex and use `/hooks` to review and trust the new hook.** Installing or updating the configuration does not automatically grant trust. Reinstall the hook if you move or remove RunChime's installation environment.

This integration follows the Stop hook workflow used with Codex CLI 0.163.0. Event routing may vary between versions or clients; verify it using the steps below. See the official [Codex Hooks documentation](https://learn.chatgpt.com/docs/hooks).

### Migrate from the old `notify_wechat.py` script

```bash
runchime codex install --migrate-legacy
```

This option:

1. Backs up and removes legacy handlers that invoke `notify_wechat.py` from the user's `hooks.json`.
2. Comments out a recognizable **single-line, top-level** legacy `notify = [...]` entry in the user's `config.toml`.
3. Installs only RunChime's `Stop` handler, without registering `SubagentStop`.

Complex or multiline legacy `notify` entries require manual migration; the installer does not guess how to edit them. Project-level hooks, inline `[hooks]` tables, and other plugins are outside automatic migration, so check them for duplicate notifications. Your existing `serverchan.key` can still be used; see the key lookup order below.

### Verify and uninstall

This check does not send a WeChat notification:

```bash
printf '%s' '{"hook_event_name":"SubagentStop","agent_id":"test-child"}' \
  | python -m runchime codex-hook
```

The expected output is `{}`. Then ask Codex to perform a task that explicitly uses a subagent: there should be no notification when the subagent finishes, and one when the main conversation ends its turn.

```bash
runchime codex uninstall
```

Uninstalling removes only the RunChime hook. It preserves keys, deduplication records, and other handlers, and does not re-enable a previously disabled legacy `notify` entry.

### Event semantics

- Only `Stop` is handled. `SubagentStop`, legacy `agent-turn-complete`, and all other events are ignored.
- Each `session_id + turn_id` pair is submitted at most once. Deduplication state lives in `$CODEX_HOME/wechat-notify-state/` and is compatible with the old script.
- `Stop` means **one main-conversation turn has ended**, not that the whole project succeeded. Goal continuations and multi-turn workflows may produce a notification per turn. Other Stop hooks that request more work can also cause notifications before the overall workflow finishes.
- A deduplication marker is written before sending. Network timeouts, interruptions, and provider rejections are not retried; a notification may be missed rather than submitted again for a repeated event.
- Do not delete deduplication records while the integration is running. Notification failures return only a `systemMessage`, without asking Codex to continue working.

## Configuration and results

Keys are resolved in this order. If the selected file is invalid, RunChime reports an error instead of silently switching to another account:

1. The current command's `--key-file /path/to/serverchan.key` option, available for task monitoring, `doctor`, and `test`.
2. The file specified by the `RUNCHIME_KEY_FILE` environment variable.
3. `$RUNCHIME_HOME/serverchan.key`; otherwise `$XDG_CONFIG_HOME/runchime/serverchan.key`, defaulting to `~/.config/runchime/serverchan.key`.
4. Legacy configuration: `$CODEX_HOME/serverchan.key`, defaulting to `~/.codex/serverchan.key`.

`configure --key-file PATH` specifies the **write destination**. To use that file afterward, also set `--key-file` or `RUNCHIME_KEY_FILE`. If you use custom environment variables when installing the Codex hook, keep those variables set when starting Codex. Keys are not automatically synchronized to new servers; configure each server separately.

Each monitoring invocation creates a separate directory under `logs/runchime/`, or the location specified by `--log-dir`:

```text
logs/runchime/<timestamp>-<random-suffix>/
├── output.log   # Run mode: task stdout/stderr; attach mode: monitor activity
└── result.json  # Status, exit codes, PID, duration, log paths, and notification status
```

| Result | Meaning |
| --- | --- |
| `succeeded` / `failed` | The launched task returned zero / nonzero |
| `terminated` / `interrupted` | The task was terminated by a signal / the run-mode monitor received a termination signal |
| `timed_out` | The launched task timed out; the monitor returns 124 |
| `launch_failed` | The task could not be started; the monitor returns 127 |
| `exited_unknown` | The attached process exited; its exit code is unavailable |
| `monitor_stopped` / `monitor_timed_out` | Attach monitoring stopped without terminating the original task |
| `attach_failed` / `observation_failed` | Attaching or reading process state failed; the monitor returns 2 |

Notification status `accepted` means only that ServerChan accepted the request. `unknown` means a network failure or an ambiguous response, and `rejected` means the provider did not return a success code. Task and notification statuses are recorded separately; a notification failure does not overwrite the task's exit code. Tasks still run when the key is missing, but the notification status becomes `configuration_error`. Run `runchime doctor` first to check your configuration.

## Privacy and limitations

- **Task notifications** include the task name, hostname, timestamps, status, exit codes, and log paths; attach mode also includes the target PID. Command arguments, source code, and log contents are not uploaded. Avoid putting sensitive information in task names.
- **Codex notifications** contain only a fixed message and a timestamp, without prompts, code, or the last assistant reply.
- Requests use HTTPS with no automatic network retries. Users manage their own provider quota, delivery channels, and WeChat notification settings.
- Notifications cannot be guaranteed after a host outage, `SIGKILL` of the monitor itself, or network loss. Detecting those cases requires heartbeat monitoring from another machine.
- For launched tasks, exit code zero indicates a normal process exit. It cannot reveal exceptions swallowed by the program or guarantee model quality or complete output artifacts.

## Development and testing

```bash
python -m unittest discover -s tests -v
```

Tests use temporary directories, fake keys, and mocked network calls. They do not read developers' real keys or send WeChat notifications. Coverage includes task exits, signals, timeouts, attach mode, PID reuse, Codex event filtering and concurrent deduplication, key configuration, and hook merging and migration. GitHub Actions runs the tests across multiple Python versions.

You can try the source tree without installing it: `python -m runchime --help`. Compatibility entry points remain available at `scripts/watch_run.py` and `scripts/notify_wechat.py`.

Report problems through [Issues](https://github.com/Czyl9323/RunChime/issues). Remove keys and private log content before attaching diagnostic information.

## License

[MIT](LICENSE)
