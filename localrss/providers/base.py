"""Provider 基类。

新增一个网站 = 在 providers/ 下新建一个模块，实现一个 Provider 子类并用
@register 装饰；`config.yaml` 里把该源的 type 写成对应的 type 即可。
"""

from __future__ import annotations

import html
import re

from ..bridge import Bridge
from ..models import Item


class Provider:
    #: 配置里 `type:` 的取值
    type: str = ""
    #: 该 provider 的说明，用于 `rss.py --list-types`
    description: str = ""

    def __init__(self, options: dict | None = None, common: dict | None = None):
        self.options = dict(options or {})
        # 与具体站点无关的公共配置（目前只有全局关键词过滤）
        self.common = dict(common or {})

    def opt(self, key: str, default=None):
        value = self.options.get(key)
        return default if value is None else value

    def require(self, key: str):
        value = self.options.get(key)
        if value in (None, ""):
            raise ValueError(f"{self.type}: options.{key} 为必填项")
        return value

    # ---------- 需要子类实现 ----------

    def page_url(self) -> str:
        """抓取时浏览器需要停留的页面。

        fetch 必须与该页面同源（或对方允许跨域），并且能带上登录 cookie。
        """
        raise NotImplementedError

    def fetch(self, bridge: Bridge, known_ids: set[str]) -> list[Item]:
        """抓取条目。

        known_ids 是本地已有的 ID 集合，用于增量：某页全部已存在时即可停止翻页。
        """
        raise NotImplementedError

    # ---------- 可选 ----------

    def setup(self, bridge: Bridge) -> None:
        """切换到正确的标签页；默认实现足够，一般不用改。"""
        bridge.ensure_tab(self.page_url(), group_title=self.opt("group_title"))

    def postprocess(self, items: list[Item], bridge: Bridge) -> list[Item]:
        """对条目列表做过滤/去重/补全。

        传入的是「历史状态 + 本次新增」合并后的完整列表，
        所以调整规则后，存量条目也会被重新处理一遍。

        这里实现的是所有站点通用的关键词过滤；子类覆盖本方法时
        请先调用 super().postprocess(items, bridge) 再叠加自己的规则。
        """
        return exclude_by_keywords(items, self.common.get("exclude_keywords"))


def plain_text(item: Item) -> str:
    """条目的可见文本（标题 + 正文去掉 HTML 标签），用于关键词匹配。"""
    raw = f"{item.title or ''}\n{item.content or ''}"
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def exclude_by_keywords(items: list[Item], keywords) -> list[Item]:
    """丢掉标题或正文命中任一关键词的条目（大小写不敏感的子串匹配）。"""
    words = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]
    if not words:
        return items
    return [i for i in items if not any(w in plain_text(i).lower() for w in words)]
