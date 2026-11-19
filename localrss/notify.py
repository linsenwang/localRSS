"""macOS 通知：抓完之后有新内容就提醒一声。

优先用 terminal-notifier 发 —— 它能把「点击通知」绑成一条命令（`-execute`），
所以点通知可以直接 `open -a NetNewsWire`。没装 terminal-notifier 就退回系统的
osascript `display notification`；那条通知归「脚本编辑器」所有，点了会把它打开，
`open_app` 也就不起作用了。

两个前提：进程要跑在用户的 GUI 会话里（pm2 是登录后拉起来的，没问题），
以及系统通知权限没被关掉。发不出去（被专注模式挡掉、没有 GUI 会话等）
只打一行日志就完事 —— 通知失败不该让整个抓取算失败。
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# 用 argv 传文本，省得手工转义引号/反斜杠（标题正文里有引号是常事）
SCRIPT = (
    "on run argv\n"
    "display notification (item 1 of argv) with title (item 2 of argv)"
    " subtitle (item 3 of argv)\n"
    "end run"
)

# pm2 拉起的进程未必带着 homebrew 的 PATH，所以除了 which 再认两个常见装法。
_TERMINAL_NOTIFIER_CANDIDATES = (
    "/opt/homebrew/bin/terminal-notifier",
    "/usr/local/bin/terminal-notifier",
)


def _terminal_notifier() -> str:
    """找到 terminal-notifier 就返回路径，没有返回空串。"""
    for path in _TERMINAL_NOTIFIER_CANDIDATES:
        if Path(path).exists():
            return path
    return shutil.which("terminal-notifier") or ""


def _run(cmd: list[str]) -> tuple[bool, str]:
    """跑命令；成功返回 (True, "")，失败返回 (False, 原因)。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, detail or f"退出码 {proc.returncode}"
    return True, ""


def _send_terminal_notifier(
    exe: str, title: str, body: str, subtitle: str, open_app: str
) -> tuple[bool, str]:
    cmd = [exe, "-title", title, "-message", body, "-group", "local_rss"]
    if subtitle:
        cmd += ["-subtitle", subtitle]
    if open_app:
        # 点通知时执行；open -a 在应用没运行时也会把它拉起来。
        cmd += ["-execute", f"/usr/bin/open -a {shlex.quote(open_app)}"]
    return _run(cmd)


def _send_osascript(title: str, body: str, subtitle: str) -> tuple[bool, str]:
    return _run(["osascript", "-e", SCRIPT, body, title, subtitle])


def send(title: str, body: str, subtitle: str = "", open_app: str = "") -> bool:
    """发一条通知，返回是否发成功。

    `open_app` 是点击通知时要打开的应用（应用名或 .app 路径，如 "NetNewsWire"）。
    只有 terminal-notifier 在的时候生效；留空 = 点击时不做事。
    """
    exe = _terminal_notifier()
    if exe:
        ok, detail = _send_terminal_notifier(exe, title, body, subtitle, open_app)
        if ok:
            return True
        print(f"terminal-notifier 发送失败（{detail}），改用 osascript", file=sys.stderr)

    ok, detail = _send_osascript(title, body, subtitle)
    if ok:
        return True
    print(f"macOS 通知发送失败：{detail}", file=sys.stderr)
    return False
