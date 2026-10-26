"""Credential files shared by task monitoring and Codex notifications."""
import os
from pathlib import Path
import re
import tempfile


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def config_home():
    if os.environ.get("RUNCHIME_HOME"):
        return Path(os.environ["RUNCHIME_HOME"]).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    return base / "runchime"


def key_path(explicit=None):
    if explicit is not None:
        return Path(explicit).expanduser()
    if os.environ.get("RUNCHIME_KEY_FILE"):
        return Path(os.environ["RUNCHIME_KEY_FILE"]).expanduser()
    preferred = config_home() / "serverchan.key"
    legacy = codex_home() / "serverchan.key"
    return preferred if preferred.exists() or not legacy.exists() else legacy


def validate_key(key):
    if not re.fullmatch(r"SCT[A-Za-z0-9]+", key):
        raise ValueError("需要 Server酱 Turbo 的 SCT 开头 SendKey；不要填写 URL 或其他版本的密钥")
    return key


def read_key(path):
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise ValueError("无法读取密钥；请先运行 runchime configure，或指定 --key-file") from None
    return validate_key(key)


def write_key(path, key, force=False):
    validate_key(key)
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".serverchan-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, path)
        else:
            # Atomic no-clobber install. The private temporary file has mode 0600.
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path
