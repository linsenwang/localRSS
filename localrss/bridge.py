"""Kimi WebBridge 客户端。

所有抓取都在用户真实的浏览器里执行（通过本地 daemon 控制），
因此可以直接复用登录态，并且不受跨域以外的限制。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:10086/command"


class BridgeError(RuntimeError):
    """WebBridge 调用失败。"""


class Bridge:
    def __init__(self, url: str = DEFAULT_URL, session: str = "local-rss", timeout: int = 120):
        self.url = url
        self.session = session
        self.timeout = timeout

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
        """daemon 是否可用。"""
        try:
            return self.try_call("list_tabs") is not None
        except BridgeError:
            return False

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
    """检查 daemon 是否就绪，未就绪时抛出带提示的异常。"""
    if bridge.ping():
        return
    raise BridgeError(
        "Kimi WebBridge 未就绪。请确认：\n"
        "  1. daemon 已启动（~/.kimi-webbridge/bin/kimi-webbridge start）\n"
        "  2. 浏览器已打开且 Kimi Browser Extension 已连接\n"
        "详情见 https://www.kimi.com/products/kimi-webbridge"
    )
