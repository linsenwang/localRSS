"""WebSub 发布端：内容更新后通知 hub。

我们是 publisher（发布端），只做两件事：

1. 在 RSS 里声明 hub 地址（`<atom:link rel="hub">`，见 feed.py）；
2. 内容有更新时向 hub 发一个 publish 通知，hub 再来抓 feed 并推给所有订阅者
   （FreshRSS 等阅读器）。

要不要配 hub 是可选功能，`hub_url` 留空就等于关掉。

⚠️ hub 必须**能自己抓到 feed**：它收到通知后会去 fetch feed URL 来取内容。
所以 feed 的订阅地址对 hub 要可达 —— 公共 hub 意味着 feed 得能从公网访问，
自建 hub 才可能走内网/tailnet。详见 README 的「WebSub」一节。
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request


class WebSubError(Exception):
    pass


def ping(hub_url: str, feed_url: str, timeout: float = 15.0) -> int:
    """通知 hub：feed_url 有更新。返回 hub 的 HTTP 状态码（正常情况下是 2xx）。

    WebSub 规范里发布端的通知就是一次 form 表单 POST：
    `hub.mode=publish` + `hub.url=<feed 地址>`。hub 收到后返回 204 No Content
    居多，也有返回 202 / 200 的实现。
    """
    body = urllib.parse.urlencode(
        [("hub.mode", "publish"), ("hub.url", feed_url)]
    ).encode("utf-8")
    req = urllib.request.Request(
        hub_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
            "User-Agent": "local_rss",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        raise WebSubError(f"hub 返回 HTTP {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise WebSubError(f"连不上 hub：{e.reason}") from e
    except OSError as e:
        raise WebSubError(f"通知 hub 失败：{e}") from e
