"""RSS 2.0 生成。"""

from __future__ import annotations

from datetime import datetime
from email.utils import format_datetime
from xml.sax.saxutils import escape

from .models import Item


def _cdata(text: str) -> str:
    # CDATA 里不能出现 "]]>"，拆开拼接即可
    return "<![CDATA[" + (text or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _rfc822(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 视作本地时间
    return format_datetime(dt)


def _updated_at(item: Item) -> datetime | None:
    """条目内容的最后编辑时间（provider 塞在 `extra.updated` 的 unix 时间戳）。

    只有比发布时间**晚**才算数：回答被编辑过之后又被赞同/收藏，那条动态的时间比
    编辑时间还晚（先编辑、后点赞），pubDate 用编辑时间会往回跳，所以那种情况返回
    None，让调用方退回 `item.published`。没编辑过时两者相等，同样返回 None。

    目前只有知乎会填这个字段，别的站点没有，取到 None。
    """
    ts = item.extra.get("updated") if isinstance(item.extra, dict) else None
    if not ts:
        return None
    try:
        ts = int(ts)
        if item.published and ts <= int(item.published.timestamp()):
            return None
        return datetime.fromtimestamp(ts).astimezone()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def build_rss(
    title: str,
    link: str,
    description: str = "",
    items: list[Item] | None = None,
    self_url: str = "",
    hub_url: str = "",
    language: str = "zh-CN",
) -> str:
    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/">',
        "<channel>",
        f"<title>{escape(title)}</title>",
        f"<link>{escape(link)}</link>",
        f"<description>{escape(description)}</description>",
        f"<language>{escape(language)}</language>",
        f"<lastBuildDate>{format_datetime(datetime.now().astimezone())}</lastBuildDate>",
        "<generator>local_rss</generator>",
    ]
    if self_url:
        out.append(
            f'<atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml"/>'
        )
    # WebSub：告诉阅读器「这个 feed 的更新由哪个 hub 转发」。阅读器订阅时会把
    # 自己的回调地址注册到这个 hub，之后我们 ping 一次 hub，它就来抓 feed 并推给订阅者。
    if hub_url:
        out.append(f'<atom:link href="{escape(hub_url)}" rel="hub"/>')

    for item in items or []:
        out.append("<item>")
        out.append(f"<title>{escape(item.title or '(无标题)')}</title>")
        if item.link:
            out.append(f"<link>{escape(item.link)}</link>")
        if item.author:
            out.append(f"<dc:creator>{escape(item.author)}</dc:creator>")
        out.append(f'<guid isPermaLink="false">{escape(item.id)}</guid>')
        # pubDate 取「原发布时间」和「内容最后编辑时间」里较晚的那个：阅读器大多按
        # 它排序和提醒，编辑过的条目还用原发布时间的话，新版本收下来也埋在原来的
        # 位置里，等于白收。注意 feed 里条目的**顺序**、`history` 裁剪、去重
        # 「留早的那条」仍然按 `item.published` 走，两者不必一致 —— 见 models.sort_key。
        published = _rfc822(_updated_at(item) or item.published)
        if published:
            out.append(f"<pubDate>{published}</pubDate>")
        out.append(f"<description>{_cdata(item.content)}</description>")
        out.append("</item>")

    out.append("</channel>")
    out.append("</rss>")
    return "\n".join(out) + "\n"
