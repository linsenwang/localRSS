"""Kimi WebBridge 客户端。

所有抓取都在用户真实的浏览器里执行（通过本地 daemon 控制），
因此可以直接复用登录态，并且不受跨域以外的限制。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:10086/command"
#: 默认自动打开的浏览器（macOS 应用名），见 check_bridge
DEFAULT_BROWSER = "Google Chrome"
#: 开完浏览器后等扩展连上的上限（秒）
CONNECT_TIMEOUT_S = 30


class BridgeError(RuntimeError):
    """WebBridge 调用失败。"""


class Bridge:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        session: str = "local-rss",
        timeout: int = 120,
        browser: str = "",
    ):
        self.url = url
        self.session = session
        self.timeout = timeout
        #: 浏览器没窗口（扩展掉线）时自动打开的应用名，空字符串 = 不自动开
        self.browser = browser

    # ---------- 底层调用 ----------

    def try_call(self, action: str, args: dict | None = None) -> dict | None:
        """调用失败（含浏览器侧报错）返回 None。"""
        payload = json.dumps(
            {"action": action, "args": args or {}, "session": self.session}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise BridgeError(f"无法连接 WebBridge daemon ({self.url}): {e}") from e

        if not body.get("ok"):
            return None
        return body.get("data") or {}

    def call(self, action: str, args: dict | None = None) -> dict:
        data = self.try_call(action, args)
        if data is None:
            raise BridgeError(f"WebBridge 调用失败: {action}")
        return data

    # ---------- 常用动作 ----------

    def ping(self) -> bool:
        """daemon 可用**且**扩展连着。"""
        try:
            return self.try_call("list_tabs") is not None
        except BridgeError:
            return False

    def daemon_status(self) -> dict | None:
        """daemon 的 /status（含 extension_connected）；连不上或不是这个 daemon 时返回 None。"""
        url = self.url.rsplit("/", 1)[0] + "/status"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def open_browser(self, app: str) -> bool:
        """打开浏览器窗口（macOS 的 `open -a`），返回是否成功。

        浏览器进程在「最后一个窗口被关掉」之后还会留在后台，但扩展的连接跟着
        窗口一起没了、且不会自己重连 —— 开个窗口是让它回来的唯一办法。
        浏览器根本没在跑时，这一下也会把它启动起来。
        """
        try:
            proc = subprocess.run(
                ["open", "-a", app], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError) as e:
            print(f"打开浏览器 {app!r} 失败：{e}", file=sys.stderr)
            return False
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip() or f"退出码 {proc.returncode}"
            print(f"打开浏览器 {app!r} 失败：{detail}", file=sys.stderr)
            return False
        return True

    def ensure_tab(self, url: str, group_title: str | None = None) -> None:
        """确保当前标签页是 url；本 session 已有该标签页则复用，否则新开。

        single-tab 类工具（evaluate/snapshot/...）都作用于“当前标签页”，
        所以每次在某个站点上执行 JS 前都必须先把标签页切过去。
        """
        data = self.try_call("find_tab", {"url": url})
        if data is not None:
            return
        args: dict = {"url": url, "newTab": True}
        if group_title:
            args["group_title"] = group_title
        self.call("navigate", args)

    def evaluate(self, code: str):
        """在当前标签页执行 JS，返回其结果。"""
        data = self.call("evaluate", {"code": code})
        return data.get("value")

    def fetch_json(self, url: str, timeout_ms: int = 45000) -> dict:
        """在页面里 fetch 一个 JSON 接口（带 cookie）。"""
        safe = url.replace("\\", "\\\\").replace("'", "\\'")
        code = f"""
        (async () => {{
            try {{
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), {timeout_ms});
                const r = await fetch('{safe}', {{ credentials: 'include', signal: controller.signal }});
                clearTimeout(timer);
                const text = await r.text();
                let body = null;
                try {{ body = JSON.parse(text); }} catch (e) {{ body = null; }}
                return {{ ok: r.ok, status: r.status, body: body, text: body ? '' : text.slice(0, 300) }};
            }} catch (e) {{
                return {{ ok: false, error: String(e) }};
            }}
        }})()
        """
        return self.evaluate(code) or {"ok": False, "error": "空返回"}

    def close_session(self) -> int:
        """关掉本 session 打开的全部标签页（不影响用户自己的标签）。

        daemon 不在线时静默跳过，返回被关闭的数量。
        """
        try:
            data = self.try_call("close_session")
        except BridgeError:
            return 0
        return int((data or {}).get("closed") or 0)


def check_bridge(bridge: Bridge) -> None:
    """确保 WebBridge 可用；浏览器没窗口（扩展掉线）时先自己开一个。

    扩展的连接是随浏览器「最后一个窗口」一起消失的：macOS 上关掉全部窗口后
    浏览器进程还在后台跑，`kimi-webbridge status` 也照旧说 running，但每个调用
    都会回 "no extension connected" —— 定时任务最容易踩这个，没人手动开窗口的话
    那一轮就白跑了。所以这里补一步：打开窗口，再等扩展自己连回来。
    """
    if bridge.ping():
        return

    if bridge.daemon_status() is None:
        raise BridgeError(
            f"连不上 Kimi WebBridge daemon ({bridge.url})。请先启动它：\n"
            "  ~/.kimi-webbridge/bin/kimi-webbridge start\n"
            "详情见 https://www.kimi.com/products/kimi-webbridge"
        )

    # daemon 在，那就是扩展没连上：多半是浏览器没窗口
    hint = "浏览器扩展未连接"
    if bridge.browser:
        print(f"浏览器扩展未连接，打开 {bridge.browser} ...", file=sys.stderr)
        if bridge.open_browser(bridge.browser):
            deadline = time.monotonic() + CONNECT_TIMEOUT_S
            while time.monotonic() < deadline:
                time.sleep(1)
                if bridge.ping():
                    print("浏览器扩展已连接，继续。", file=sys.stderr)
                    return
            hint = f"打开 {bridge.browser} 后等了 {CONNECT_TIMEOUT_S} 秒，扩展仍未连接"
        else:
            hint = f"打开 {bridge.browser} 失败"

    raise BridgeError(
        f"Kimi WebBridge 未就绪：{hint}。请确认：\n"
        "  1. daemon 已启动（~/.kimi-webbridge/bin/kimi-webbridge start）\n"
        "  2. 浏览器已打开且 Kimi Browser Extension 已连接\n"
        "     （浏览器没窗口时扩展会掉线；config.yaml 的 bridge.browser 配成\n"
        "      浏览器应用名就会自动开窗口，留空则不自动开）\n"
        "详情见 https://www.kimi.com/products/kimi-webbridge"
    )
