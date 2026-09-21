"""macOS 通知：抓完之后有新内容就提醒一声。

用系统的 osascript 发（`display notification`），不依赖第三方库。
两个前提：进程要跑在用户的 GUI 会话里（pm2 是登录后拉起来的，没问题），
以及系统通知权限没被关掉。发不出去（被专注模式挡掉、没有 GUI 会话等）
只打一行日志就完事 —— 通知失败不该让整个抓取算失败。
"""

from __future__ import annotations

import subprocess
import sys

# 用 argv 传文本，省得手工转义引号/反斜杠（标题正文里有引号是常事）
SCRIPT = (
    "on run argv\n"
    "display notification (item 1 of argv) with title (item 2 of argv)"
    " subtitle (item 3 of argv)\n"
    "end run"
)


def send(title: str, body: str, subtitle: str = "") -> bool:
    """发一条通知，返回是否发成功。"""
    try:
        proc = subprocess.run(
            ["osascript", "-e", SCRIPT, body, title, subtitle],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"macOS 通知发送失败：{e}", file=sys.stderr)
        return False

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"退出码 {proc.returncode}"
        print(f"macOS 通知发送失败：{detail}", file=sys.stderr)
        return False
    return True
