"""Small command-line front end; no runtime dependencies outside stdlib."""
import argparse
import getpass
import json
from pathlib import Path
import sys

from . import __version__, codex, hooks, watch
from .config import codex_home, config_home, key_path, read_key, write_key


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("run", "attach"):
        mode = argv.pop(0)
        args = watch.parse_args(argv)
        if mode == "run" and args.pid is not None:
            print("使用 runchime attach --pid PID 附加到现有进程。", file=sys.stderr)
            return 2
        if mode == "attach" and args.pid is None:
            print("attach 需要 --pid PID；启动新任务请使用 runchime run。", file=sys.stderr)
            return 2
        return watch.run(args)
    if argv and argv[0] == "codex-hook":
        codex.main(argv[1:])
        return 0

    parser = argparse.ArgumentParser(prog="runchime", description="训练、评估与 Codex 结束时，通过 Server酱 Turbo 通知微信。")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("run", help="启动并监控前台任务（run --help 查看参数）")
    sub.add_parser("attach", help="只读监控已有 PID（attach --help 查看参数）")
    configure = sub.add_parser("configure", help="隐藏输入自己的 SendKey，保存到用户配置目录")
    configure.add_argument("--key-file", type=Path, help="保存位置，默认 ~/.config/runchime/serverchan.key")
    configure.add_argument("--key-stdin", action="store_true", help="从标准输入读取密钥，适合秘密管理工具")
    configure.add_argument("--force", action="store_true", help="替换已有密钥文件")
    doctor = sub.add_parser("doctor", help="检查本地配置，不发送微信")
    doctor.add_argument("--key-file", type=Path)
    test = sub.add_parser("test", help="发送一条测试微信（消耗服务商额度）")
    test.add_argument("--key-file", type=Path)
    codex_parser = sub.add_parser("codex", help="安装或卸载 Codex Stop Hook")
    codex_sub = codex_parser.add_subparsers(dest="codex_action", required=True)
    install = codex_sub.add_parser("install", help="合并 Stop Hook，保留其他处理器")
    install.add_argument("--migrate-legacy", action="store_true", help="迁移识别出的 notify_wechat.py 旧入口")
    codex_sub.add_parser("uninstall", help="只移除 RunChime Hook，不删除密钥")
    args = parser.parse_args(argv)
    try:
        if args.action == "configure":
            destination = args.key_file or config_home() / "serverchan.key"
            if args.key_stdin:
                key = sys.stdin.read().strip()
            else:
                if not sys.stdin.isatty():
                    raise ValueError("请在交互终端运行 configure，或用 --key-stdin 从秘密管理工具传入")
                key = getpass.getpass("请输入你自己的 Server酱 Turbo SendKey（输入不可见）：").strip()
            path = write_key(destination, key, force=args.force)
            print("密钥已保存：%s（权限 0600）；未发送通知。" % path)
            print("使用 runchime test 发送一条测试通知。")
            return 0
        if args.action in ("doctor", "test"):
            path = key_path(args.key_file)
            key = read_key(path)
            if args.action == "doctor":
                print("SendKey 格式有效：%s（不显示密钥）" % path)
                print("密钥文件权限：%s" % oct(path.stat().st_mode & 0o777))
                print("Codex 配置目录：%s" % codex_home())
                hook_path = codex_home() / "hooks.json"
                if hook_path.exists():
                    data = json.loads(hook_path.read_text())
                    count = sum(hooks.owned(h) for groups in data.get("hooks", {}).values() for group in groups for h in group.get("hooks", []))
                    print("RunChime Hook 数量：%s（信任状态请在 Codex /hooks 中检查）" % count)
                else:
                    print("未发现用户 hooks.json；任务监控无需安装 Codex Hook。")
                print("本地检查完成；未访问 Server酱。")
                return 0
            result = {"name": "RunChime 配置测试", "host": "RunChime", "status": "succeeded",
                      "started_at": watch.now(), "finished_at": watch.now(), "duration_seconds": 0,
                      "returncode": 0, "exit_code": 0, "log_path": "无：这是手动测试通知"}
            sent = watch.notify(key, result)
            print(sent["message"])
            return 0 if sent["status"] == "accepted" else 1
        if args.action == "codex":
            hooks.update_hooks(remove=args.codex_action == "uninstall", migrate_legacy=getattr(args, "migrate_legacy", False))
            return 0
    except FileExistsError:
        print("密钥文件已存在；确认需要替换时添加 --force。", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        # No HTTP exception or credential is included in these local diagnostics.
        print("RunChime：%s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
