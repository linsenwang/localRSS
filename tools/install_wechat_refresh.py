#!/usr/bin/env python3
"""安装「微信库专用解密器」（root-owned + FDA 版）。

为什么不是普通 LaunchAgent 里跑一个用户目录下的脚本：
macOS 对 CLI 的 App Data 授权不持久，每轮都会重新弹窗。可靠的做法是给一个
**行为固定、不可被用户改写**的 helper 完全磁盘访问权限（FDA）。这个脚本会：

1. 把 decrypt_db.py / 密钥 / sqlcipher / libcrypto 复制到
   /usr/local/libexec/wechat-refresh/，全部 root:wheel，用户不可写
2. 编译并安装 helper（同样 root-owned），它只调用复制过去的脚本和 sqlcipher
3. 写用户级 LaunchAgent（每小时 :25/:55 兜底）
4. 打印 FDA 授权步骤；授权后运行 `--enable` 才真正启用

    python3 tools/install_wechat_refresh.py            # 安装文件（会弹一次管理员密码）
    # 系统设置 → 隐私与安全性 → 完全磁盘访问权限 → + →
    #   /usr/local/libexec/wechat-refresh/wechat-refresh
    python3 tools/install_wechat_refresh.py --enable   # 恢复定时 + 立即跑一次
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
LABEL = "com.localrss.wechat-refresh"
DEST = Path("/usr/local/libexec/wechat-refresh")
HELPER = DEST / "wechat-refresh"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = REPO / "logs" / "wechat-refresh.log"
SOURCE = REPO / "tools" / "wechat_refresh.c"
SQLCIPHER = Path("/opt/homebrew/opt/sqlcipher/bin/sqlcipher")
LIBCRYPTO = Path("/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib")


def load_paths() -> dict[str, str]:
    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8")) or {}
    feed = next(
        (f for f in (cfg.get("feeds") or []) if f.get("id") == "wechat-mp"), None
    )
    if feed is None:
        raise SystemExit("config.yaml 里没有 wechat-mp 源")
    opts = feed.get("options") or {}
    refresh = opts.get("refresh_db") or {}
    missing = [k for k in ("script", "keys") if not refresh.get(k)]
    if missing or not opts.get("db_dir"):
        raise SystemExit("wechat-mp 的 options 缺少 db_dir 或 refresh_db.")
    return {
        "script": str(Path(refresh["script"]).expanduser()),
        "keys": str(Path(refresh["keys"]).expanduser()),
        "out": str(Path(opts["db_dir"]).expanduser()),
        "timeout": str(int(refresh.get("timeout") or 60)),
    }


def c_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_helper(paths: dict[str, str], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "/usr/bin/clang", "-O2", "-Wall", "-Wextra", "-o", str(dest), str(SOURCE),
        f"-DPYTHON_PATH={c_string('/usr/bin/python3')}",
        f"-DSCRIPT_PATH={c_string(str(DEST / 'refresh_run.py'))}",
        f"-DKEYS_PATH={c_string(str(DEST / 'wechat_keys.json'))}",
        f"-DOUT_DIR={c_string(paths['out'])}",
        f"-DTIMEOUT_S={paths['timeout']}",
    ]
    subprocess.run(cmd, check=True)


def root_script(paths: dict[str, str], helper_build: Path) -> str:
    q = shlex.quote
    return f"""set -euo pipefail
DEST={q(str(DEST))}
STAGE="${{DEST}}.new.$$"
rm -rf "$STAGE"
mkdir -p "$STAGE/bin" "$STAGE/lib"

sed 's#/opt/homebrew/opt/sqlcipher/bin/sqlcipher#/usr/local/libexec/wechat-refresh/bin/sqlcipher#' \
    {q(paths['script'])} > "$STAGE/decrypt_db.py"
cp {q(paths['keys'])} "$STAGE/wechat_keys.json"
cp {q(str(SQLCIPHER))} "$STAGE/bin/sqlcipher"
cp {q(str(LIBCRYPTO))} "$STAGE/lib/libcrypto.3.dylib"
install_name_tool -change {q(str(LIBCRYPTO))} \
    @loader_path/../lib/libcrypto.3.dylib "$STAGE/bin/sqlcipher"
codesign --force --sign - "$STAGE/lib/libcrypto.3.dylib" >/dev/null 2>&1
codesign --force --sign - "$STAGE/bin/sqlcipher" >/dev/null 2>&1
cp {q(str(helper_build))} "$STAGE/wechat-refresh"
cp {q(paths['wrapper'])} "$STAGE/refresh_run.py"

chown -R root:wheel "$STAGE"
chmod 755 "$STAGE" "$STAGE/bin" "$STAGE/lib"
chmod 755 "$STAGE/wechat-refresh" "$STAGE/bin/sqlcipher" "$STAGE/decrypt_db.py" "$STAGE/refresh_run.py"
chmod 644 "$STAGE/wechat_keys.json" "$STAGE/lib/libcrypto.3.dylib"
rm -rf "$DEST"
mv "$STAGE" "$DEST"
"""


def cdhash(path: Path) -> str:
    r = subprocess.run(
        ["codesign", "-d", "--verbose=4", str(path)],
        capture_output=True, text=True,
    )
    for line in (r.stdout + r.stderr).splitlines():
        if line.startswith("CDHash="):
            return line.split("=", 1)[1].strip()
    return ""


def install_system(paths: dict[str, str]) -> None:
    if not SQLCIPHER.exists() or not LIBCRYPTO.exists():
        raise SystemExit("找不到 Homebrew 的 sqlcipher / libcrypto，先 brew install sqlcipher")
    old_hash = cdhash(HELPER)
    tmp = Path(tempfile.mkdtemp(prefix="wechat-refresh-install-"))
    helper_build = tmp / "wechat-refresh"
    build_helper(paths, helper_build)

    # 先以当前用户身份把脚本/密钥拷到临时目录：root 脚本不用去读 Downloads
    staged = dict(paths)
    staged["script"] = str(tmp / "decrypt_db.py")
    staged["keys"] = str(tmp / "wechat_keys.json")
    Path(staged["script"]).write_bytes(Path(paths["script"]).read_bytes())
    Path(staged["keys"]).write_bytes(Path(paths["keys"]).read_bytes())
    Path(staged["keys"]).chmod(0o600)
    staged["wrapper"] = str(tmp / "wechat_refresh_run.py")
    Path(staged["wrapper"]).write_bytes(
        (REPO / "tools" / "wechat_refresh_run.py").read_bytes()
    )

    script = tmp / "install_root.sh"
    script.write_text(root_script(staged, helper_build), encoding="utf-8")
    script.chmod(0o700)

    apple = f'do shell script "/bin/bash {shlex.quote(str(script))}" with administrator privileges'
    try:
        r = subprocess.run(["osascript", "-e", apple], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(
                "管理员授权被取消或失败，请重新运行安装脚本。\n"
                f"{r.stderr.strip()}"
            )
    finally:
        # 临时目录里有密钥副本，无论成败都删掉
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"已安装到 {DEST}（root:wheel，用户不可写）")
    new_hash = cdhash(HELPER)
    if new_hash:
        print(f"helper cdhash: {new_hash}")
    if old_hash and new_hash and old_hash != new_hash:
        print(
            "\n⚠ helper 二进制变了（cdhash {} → {}）。\n"
            "  完全磁盘访问权限里的旧条目绑定的是旧二进制，已经失效：\n"
            "  不重新添加的话，每次启动都会弹「wechat-refresh 想访问其他 App 的数据」，\n"
            "  点“允许”也只在那一轮有效。\n"
            "  去 系统设置 → 隐私与安全性 → 完全磁盘访问权限，删掉旧的 wechat-refresh\n"
            "  条目，再重新添加下面的文件：\n"
            "  {}".format(old_hash, new_hash, HELPER)
        )


def write_plist() -> None:
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [str(HELPER)],
        "StartCalendarInterval": [{"Minute": 25}, {"Minute": 55}],
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }
    PLIST.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML))


def bootstrap(uid: int) -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    r = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise SystemExit(f"launchctl bootstrap 失败：{r.stderr.strip()}")
    subprocess.run(["launchctl", "enable", f"gui/{uid}/{LABEL}"], capture_output=True)


def set_trigger(value: bool) -> None:
    cfg_path = REPO / "config.yaml"
    text = cfg_path.read_text(encoding="utf-8")
    old = "        trigger: false" if value else "        trigger: true"
    new = "        trigger: true" if value else "        trigger: false"
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"config.yaml 里找不到 `{old}`，请手动改成 `{new}`")
    cfg_path.write_text(text.replace(old, new, 1), encoding="utf-8")


def enable() -> None:
    set_trigger(True)
    uid = os.getuid()
    bootstrap(uid)
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"],
                   capture_output=True)
    print("已启用：定时 :25/:55，RSS 启动时也会先触发并等它跑完。")
    print(f"如果还没授权，去 系统设置 → 隐私与安全性 → 完全磁盘访问权限 添加：\n  {HELPER}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--enable", action="store_true",
                        help="授权完成后启用定时和触发（改 config.yaml + load + kickstart）")
    args = parser.parse_args()

    if args.enable:
        enable()
        return 0

    paths = load_paths()
    install_system(paths)
    write_plist()
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    print("\n下一步（只需一次）：")
    print("  系统设置 → 隐私与安全性 → 完全磁盘访问权限 → +")
    print(f"  {HELPER}")
    print("添加并勾选后运行：")
    print("  python3 tools/install_wechat_refresh.py --enable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
