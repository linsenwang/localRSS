#!/usr/bin/env python3
"""微信库专用 helper 的入口：先解密数据库，再导出 webview 的分享缓存。

有些公众号推送只在 SessionTable 里留下摘要、消息行没落盘；文章被打开时
webview 的 Share Data 里会有标题/链接。这里把两个微信账号的 Share Data 复制到
<解密目录>/_webview/，provider 只用它补“标题在会话摘要里”的那些推送。

由 root-owned helper 用 /usr/bin/python3 调用；自身和 decrypt_db.py 都是
root-owned，读取微信容器的权限来自 helper 的 FDA。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DECRYPT = HERE / "decrypt_db.py"
HOME = Path.home()
PROFILE_DIRS = [
    HOME / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium/web/profiles",
    HOME / "Library/Containers/com.tencent.xinWeChat2/Data/Documents/app_data/radium/web/profiles",
]


def output_dir(args: list[str]) -> Path | None:
    for i, arg in enumerate(args):
        if arg in ("-o", "--output") and i + 1 < len(args):
            return Path(args[i + 1]).expanduser()
    return None


def export_webview(out: Path) -> int:
    dest = out / "_webview"
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for base in PROFILE_DIRS:
        if not base.is_dir():
            continue
        for src in sorted(base.glob("*/Share Data")):
            if not src.is_file():
                continue
            name = hashlib.md5(str(src).encode()).hexdigest()
            try:
                shutil.copy2(src, dest / f"{name}.db")
                for suffix in ("-journal", "-wal"):
                    extra = src.with_name(src.name + suffix)
                    if extra.exists():
                        shutil.copy2(extra, dest / f"{name}.db{suffix}")
                copied += 1
            except OSError as e:
                print(f"[webview] 复制失败 {src}: {e}", file=sys.stderr)
    if copied:
        print(f"[webview] 导出 {copied} 个 Share Data 缓存 -> {dest}")
    return copied


def main() -> int:
    args = sys.argv[1:]
    rc = subprocess.run(
        ["/usr/bin/python3", str(DECRYPT), *args], check=False
    ).returncode
    out = output_dir(args)
    if out is not None:
        try:
            export_webview(out)
        except Exception as e:
            print(f"[webview] 导出失败（不影响解密）：{e}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
