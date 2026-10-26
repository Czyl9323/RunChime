"""Install/remove only RunChime hooks, preserving other lifecycle handlers."""
import ast
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile

from .config import codex_home

MARKER = "RunChime: WeChat notification"


def owned(handler):
    if handler.get("statusMessage") != MARKER or handler.get("type") != "command":
        return False
    try:
        return "codex-hook" in shlex.split(handler.get("command", ""))
    except ValueError:
        return False


def legacy(handler):
    if handler.get("type") != "command":
        return False
    try:
        return any(Path(part).name == "notify_wechat.py" for part in shlex.split(handler.get("command", "")))
    except ValueError:
        return False


def atomic_update(path, text):
    """Back up changed files and replace them without partial JSON/TOML writes."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return None
    backup = None
    mode = 0o600
    if path.exists():
        mode = path.stat().st_mode & 0o777
        backup = path.with_name(path.name + ".before-runchime-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        shutil.copy2(path, backup)
    fd, temporary = tempfile.mkstemp(prefix=".runchime-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if backup:
        print("备份：%s" % backup)
    return backup


def migrate_notify(path):
    """Only migrate a recognizable single-line legacy top-level notify array."""
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            break
        if not re.match(r"^\s*notify\s*=", line):
            continue
        try:
            value = ast.literal_eval(line.split("=", 1)[1].strip())
        except (ValueError, SyntaxError):
            raise ValueError("旧 notify 为多行或无法安全解析；请手动移除其微信通知入口后重试") from None
        if isinstance(value, list) and all(isinstance(item, str) for item in value) and any(Path(item).name == "notify_wechat.py" for item in value):
            lines[index] = "# RunChime migrated legacy WeChat notify to hooks.json (Stop).\n"
            atomic_update(path, "".join(lines))
        else:
            print("保留其他 notify 配置；请检查它是否也会发送微信。")
        break


def update_hooks(remove=False, migrate_legacy=False):
    home = codex_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = home / "hooks.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"hooks": {}}
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        raise ValueError("hooks.json 必须包含 hooks 对象；未修改")
    # Validate the full source before changing any file.
    for groups in data["hooks"].values():
        if not isinstance(groups, list):
            raise ValueError("Hook 事件必须对应数组；未修改")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list) or not all(isinstance(h, dict) for h in group["hooks"]):
                raise ValueError("Hook 处理器格式无效；未修改")
    command = " ".join(shlex.quote(part) for part in [sys.executable, str(Path(__file__).with_name("__main__.py").resolve()), "codex-hook"])
    for event, groups in list(data["hooks"].items()):
        kept = []
        for group in groups:
            handlers = [h for h in group["hooks"] if not owned(h) and not (migrate_legacy and legacy(h))]
            if handlers or not group["hooks"]:
                kept.append(dict(group, hooks=handlers))
        if kept:
            data["hooks"][event] = kept
        else:
            del data["hooks"][event]
    if not remove:
        data["hooks"].setdefault("Stop", []).append({"hooks": [{
            "type": "command", "command": command, "timeout": 15, "statusMessage": MARKER,
        }]})
    if migrate_legacy:
        migrate_notify(home / "config.toml")
    atomic_update(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print("已%s RunChime Hook：%s" % ("移除" if remove else "安装", path))
    if not remove:
        print("请重新启动 Codex，在 /hooks 中审查并信任新 Hook。")
        print("请同时检查项目、内联配置及其他插件中的重复微信通知入口。")
