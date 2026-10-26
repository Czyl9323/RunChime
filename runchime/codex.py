#!/usr/bin/env python3
"""Notify Server酱 once per main-thread Stop; never retry a claimed turn.

Stop means a turn ended, not that a Goal or an entire project completed.
The durable claim precedes HTTP submission: an interrupted or failed request
may lose a notification, but a repeated event must not submit it again.
"""

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import codex_home, key_path, read_key


class NotificationError(Exception):
    """A safe, credential-free diagnostic for the hook UI."""


def claim_turn(home, session_id, turn_id):
    # Hash identifiers so event data can never become a filesystem path.
    session = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    turn = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    state = home / "wechat-notify-state"
    state.mkdir(mode=0o700, exist_ok=True)
    folder = state / session
    folder.mkdir(mode=0o700, exist_ok=True)
    try:
        fd = os.open(folder / (turn + ".claimed"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    # O_EXCL arbitrates concurrent processes. Keep even an empty claim after
    # failure; deleting it could duplicate a request already accepted remotely.
    with os.fdopen(fd, "w", encoding="utf-8") as claim:
        claim.write(datetime.now().astimezone().isoformat() + "\n")
        claim.flush()
        os.fsync(claim.fileno())
    for directory in (folder, state, home):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return True


def handle_event(event):
    if not isinstance(event, dict) or event.get("hook_event_name") != "Stop":
        return {}
    # Defensive guard for a misrouted subagent-shaped payload.
    if event.get("agent_id") or event.get("type") == "agent-turn-complete":
        return {}
    session_id, turn_id = event.get("session_id"), event.get("turn_id")
    if not all(isinstance(value, str) and value.strip()
               for value in (session_id, turn_id)):
        raise NotificationError("Stop 缺少有效的 session_id 或 turn_id，未发送。")

    home = codex_home()
    try:
        key = read_key(key_path())
    except ValueError as exc:
        raise NotificationError(str(exc)) from None
    home.mkdir(mode=0o700, parents=True, exist_ok=True)

    body = urlencode({
        "title": "Codex 本轮工作已结束",
        "desp": (
            "Codex 已结束本轮处理，请返回查看结果。\n\n"
            f"时间：{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}"
        ),
    }).encode("utf-8")
    request = Request(
        f"https://sctapi.ftqq.com/{key}.send",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    if not claim_turn(home, session_id, turn_id):
        return {}
    try:
        with urlopen(request, timeout=8) as response:
            result = json.load(response)
    except Exception:
        # Exception strings can contain the request URL and therefore SendKey.
        raise NotificationError(
            "请求失败或响应无效，是否已受理不确定；已保留去重标记，不自动重试。"
        ) from None
    if not isinstance(result, dict) or type(result.get("code")) is not int or result["code"] != 0:
        raise NotificationError("Server酱未返回 code=0；已保留去重标记，不自动重试。")
    return {}


def main(argv=None):
    # Legacy notify passes JSON as argv[1]. Ignore it without reading stdin.
    if (sys.argv[1:] if argv is None else argv):
        print("{}")
        return
    try:
        result = handle_event(json.load(sys.stdin))
    except NotificationError as exc:
        result = {"systemMessage": "微信通知：" + str(exc)}
    except (ValueError, UnicodeError):
        result = {"systemMessage": "微信通知：Hook 输入不是有效 JSON，未发送。"}
    except Exception:
        result = {"systemMessage": "微信通知：本地配置或去重状态读写失败，未发送。"}
    # Warnings are advisory. Never return decision:block or exit code 2.
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
