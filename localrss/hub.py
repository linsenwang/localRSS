"""一个极简的 WebSub hub（发布端和订阅端之间的中转站）。

为什么要自己跑一个：WebSub 里 hub 必须**同时够得到发布端和订阅端**。公共 hub
（websubhub.com、pubsubhubbub.appspot.com）都在公网上，抓不到只绑在 tailnet 上的
feed，所以这套环境只能自建。

三方关系（hub 跑在本机时）：

    FreshRSS ──①POST hub.mode=subscribe──▶ 本机 hub
       ▲                                      │
       │                              ②GET 回调地址验签（回显 challenge）
       │                                      │
       └──③POST 推送 feed 内容─────────────────┘

只实现规范里必须的三件事：

  hub.mode=publish       发布端通知「这个 feed 更新了」→ 抓 feed，内容有变化就推给订阅者
  hub.mode=subscribe     订阅端要订阅 → 同步验签（FreshRSS 发 hub.verify=sync）→ 记下订阅，
                         并按惯例立刻推一次当前内容
  hub.mode=unsubscribe   退订 → 同样验签 → 删掉订阅

订阅关系存在 state/hub.json 里，重启不丢。只用标准库，不需要任何第三方包。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8667
DEFAULT_HOST = "127.0.0.1"
DEFAULT_STATE = "state/hub.json"
DEFAULT_LEASE = 7 * 24 * 3600  # 订阅租期，7 天
FETCH_TIMEOUT = 20.0
VERIFY_TIMEOUT = 10.0
UA = "local-rss-hub"


def _log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _request(url: str, *, data: bytes | None = None, headers: dict | None = None,
             method: str | None = None, timeout: float = FETCH_TIMEOUT):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def verify_callback(callback: str, mode: str, topic: str, verify_token: str) -> bool:
    """意图验证：把 challenge 发给回调地址，对方必须原样回显才算数。

    这一步是防「别人替你订阅」的 —— 订阅动作必须是回调地址的主人自己同意的。
    FreshRSS 的 p/api/pshb.php 就是收到这个 GET 后把 hub_challenge 原样返回。
    """
    challenge = secrets.token_hex(20)
    params = {"hub.mode": mode, "hub.topic": topic, "hub.challenge": challenge}
    if verify_token:
        params["hub.verify_token"] = verify_token
    sep = "&" if "?" in callback else "?"
    url = callback + sep + urllib.parse.urlencode(params)

    try:
        with _request(url, headers={"User-Agent": UA}, timeout=VERIFY_TIMEOUT) as r:
            body = r.read(4096).decode("utf-8", "replace").strip()
            ok = 200 <= r.status < 300 and body == challenge
    except urllib.error.HTTPError as e:
        _log(f"验签  {callback} -> HTTP {e.code} {e.reason}，拒绝")
        return False
    except (urllib.error.URLError, OSError) as e:
        _log(f"验签  {callback} 连不上：{e}，拒绝")
        return False

    _log(f"验签  {callback} -> {'通过' if ok else 'challenge 回显不对，拒绝'}")
    return ok


class Hub:
    """订阅关系 + 每个 topic 最近一次推送过的内容摘要（用来跳过无变化的推送）。"""

    def __init__(self, state_path: Path, lease_seconds: int = DEFAULT_LEASE):
        self.state_path = state_path
        self.lease_seconds = lease_seconds
        self._lock = threading.Lock()
        self._subs: dict[str, list[dict]] = {}  # topic -> [{callback, secret, lease_end}]
        self._digests: dict[str, str] = {}  # topic -> sha256(上次推的内容)
        self._load()

    # ---- 持久化 ----------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            _log(f"警告：{self.state_path} 读不了（{e}），先当空状态跑")
            return
        self._subs = raw.get("subscriptions") or {}
        self._digests = raw.get("digests") or {}

    def _save_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {"subscriptions": self._subs, "digests": self._digests},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.state_path)  # 原子替换，别让半截文件被读走

    # ---- 订阅 ------------------------------------------------------------

    def subscribe(self, topic: str, callback: str, secret: str) -> None:
        with self._lock:
            subs = [s for s in self._subs.get(topic, []) if s["callback"] != callback]
            subs.append({
                "callback": callback,
                "secret": secret,
                "lease_end": time.time() + self.lease_seconds,
            })
            self._subs[topic] = subs
            self._save_locked()
        _log(f"订阅  {topic}\n      -> {callback}（租期 {self.lease_seconds}s）")

    def unsubscribe(self, topic: str, callback: str) -> None:
        with self._lock:
            subs = [s for s in self._subs.get(topic, []) if s["callback"] != callback]
            if subs:
                self._subs[topic] = subs
            else:
                self._subs.pop(topic, None)
                self._digests.pop(topic, None)
            self._save_locked()
        _log(f"退订  {topic} -> {callback}")

    def live_subscriptions(self, topic: str) -> list[dict]:
        """该 topic 还没过期的订阅，顺手清掉过期的。"""
        with self._lock:
            all_subs = self._subs.get(topic, [])
            now = time.time()
            alive = [s for s in all_subs if s.get("lease_end", 0) > now]
            if len(alive) != len(all_subs):
                if alive:
                    self._subs[topic] = alive
                else:
                    self._subs.pop(topic, None)
                self._save_locked()
            return list(alive)

    def snapshot(self) -> dict[str, list[dict]]:
        with self._lock:
            return {topic: list(subs) for topic, subs in self._subs.items()}

    # ---- 发布 ------------------------------------------------------------

    def publish(self, topic: str, force: bool = False) -> None:
        """抓一次 topic，内容有变化就推给所有订阅者。force 用来无视「没变化」。"""
        subs = self.live_subscriptions(topic)
        if not subs:
            _log(f"通知  {topic} —— 没有订阅者，忽略")
            return

        body = self._fetch(topic)
        if body is None:
            return

        digest = hashlib.sha256(body).hexdigest()
        if not force:
            with self._lock:
                if self._digests.get(topic) == digest:
                    _log(f"通知  {topic} —— 内容和上次推的一样，不推")
                    return

        pushed = 0
        for sub in subs:
            if self._push(sub, body):
                pushed += 1
        if pushed == 0:
            _log(f"错误  {topic} —— {len(subs)} 个订阅者一个都没推成功")
            return

        with self._lock:
            self._digests[topic] = digest
            self._save_locked()
        _log(f"通知  {topic} —— 推给 {pushed}/{len(subs)} 个订阅者（{len(body)} 字节）")

    def _fetch(self, url: str) -> bytes | None:
        try:
            with _request(url, headers={"User-Agent": UA}) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            _log(f"错误  抓 feed {url} 失败：HTTP {e.code} {e.reason}")
        except (urllib.error.URLError, OSError) as e:
            _log(f"错误  抓 feed {url} 失败：{e}")
        return None

    def _push(self, sub: dict, body: bytes) -> bool:
        headers = {
            "Content-Type": "application/rss+xml; charset=utf-8",
            "User-Agent": UA,
        }
        secret = sub.get("secret") or ""
        if secret:
            mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()
            headers["X-Hub-Signature"] = f"sha1={mac}"
        try:
            with _request(sub["callback"], data=body, headers=headers, method="POST") as r:
                _log(f"推送  {sub['callback']} -> HTTP {r.status}")
                return True
        except urllib.error.HTTPError as e:
            _log(f"错误  推送 {sub['callback']} 失败：HTTP {e.code} {e.reason}")
        except (urllib.error.URLError, OSError) as e:
            _log(f"错误  推送 {sub['callback']} 失败：{e}")
        return False


def _pairs(query: str) -> list[tuple[str, str]]:
    return urllib.parse.parse_qsl(query, keep_blank_values=True)


def make_handler(hub: Hub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = UA
        protocol_version = "HTTP/1.1"

        def _reply(self, code: int, text: str = "") -> None:
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            """给个能直接看的页面，方便确认订阅有没有登记上。"""
            path, _, query = self.path.partition("?")
            if path not in ("/", "/index.html"):
                return self._reply(404, "not found\n")

            subs = hub.snapshot()
            now = time.time()
            lines = [
                "local_rss WebSub hub",
                "",
                f"订阅 {sum(len(v) for v in subs.values())} 个：",
            ]
            for topic, items in subs.items():
                lines.append(f"  {topic}")
                for sub in items:
                    left = int(sub.get("lease_end", 0) - now)
                    lines.append(f"    -> {sub['callback']}（租期还剩 {left}s）")
            self._reply(200, "\n".join(lines) + "\n")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            _, _, query = self.path.partition("?")
            pairs = _pairs(raw) + _pairs(query)  # body 是标准的 form，query 一起兼容掉

            p = dict(pairs)
            mode = p.get("hub.mode", "")

            if mode == "publish":
                # 发布端通知：hub.url 可以重复出现，一次通知多个 topic
                topics = [v for k, v in pairs if k == "hub.url" and v]
                if not topics:
                    return self._reply(400, "缺少 hub.url\n")
                self._reply(204)
                for topic in topics:
                    threading.Thread(
                        target=hub.publish, args=(topic,), daemon=True
                    ).start()
                return

            if mode in ("subscribe", "unsubscribe"):
                topic = p.get("hub.topic", "")
                callback = p.get("hub.callback", "")
                if not topic or not callback:
                    return self._reply(400, "缺少 hub.topic 或 hub.callback\n")
                # FreshRSS 发的是 hub.verify=sync：验证要在这次请求里做完，
                # 响应码本身就代表订阅成没成。
                if not verify_callback(callback, mode, topic, p.get("hub.verify_token", "")):
                    return self._reply(422, "callback 验证失败\n")

                if mode == "subscribe":
                    hub.subscribe(topic, callback, p.get("hub.secret", ""))
                    self._reply(204)
                    # 订阅成功后立刻推一次当前内容。FreshRSS 在验签时会把这条订阅
                    # 标成 error，直到收到第一次成功推送才算 WebSub 生效。
                    threading.Thread(
                        target=hub.publish, args=(topic,), kwargs={"force": True}, daemon=True
                    ).start()
                else:
                    hub.unsubscribe(topic, callback)
                    self._reply(204)
                return

            self._reply(400, f"不认识的 hub.mode：{mode!r}\n")

        def log_message(self, fmt: str, *args) -> None:
            _log(f"HTTP  {self.address_string()} {fmt % args}")

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rss-hub.py",
        description="local_rss 自用的极简 WebSub hub（标准库实现，无第三方依赖）",
    )
    parser.add_argument(
        "-b", "--bind", default=os.environ.get("HUB_HOST") or DEFAULT_HOST,
        help=f"绑定地址，默认 {DEFAULT_HOST}（只本机；给 tailnet 用见 hub.sh）",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=int(os.environ.get("HUB_PORT") or DEFAULT_PORT),
        help=f"端口，默认 {DEFAULT_PORT}",
    )
    parser.add_argument(
        "-s", "--state", default=os.environ.get("HUB_STATE") or DEFAULT_STATE,
        help=f"订阅状态文件，默认 {DEFAULT_STATE}",
    )
    parser.add_argument(
        "--lease", type=int, default=DEFAULT_LEASE,
        help=f"订阅租期秒数，默认 {DEFAULT_LEASE}（过期自动清理）",
    )
    args = parser.parse_args(argv)

    hub = Hub(Path(args.state), lease_seconds=args.lease)
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(hub))

    subs = hub.snapshot()
    _log(f"WebSub hub 监听 http://{args.bind}:{args.port}/（状态文件 {args.state}）")
    _log(f"已加载 {sum(len(v) for v in subs.values())} 个订阅，覆盖 {len(subs)} 个 feed")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("收到停止信号，退出")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
