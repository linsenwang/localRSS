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


def build_rss(
    title: str,
    link: str,
    description: str = "",
    items: list[Item] | None = None,
    self_url: str = "",
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

    for item in items or []:
        out.append("<item>")
        out.append(f"<title>{escape(item.title or '(无标题)')}</title>")
        if item.link:
            out.append(f"<link>{escape(item.link)}</link>")
        if item.author:
            out.append(f"<dc:creator>{escape(item.author)}</dc:creator>")
        out.append(f'<guid isPermaLink="false">{escape(item.id)}</guid>')
        published = _rfc822(item.published)
        if published:
            out.append(f"<pubDate>{published}</pubDate>")
        out.append(f"<description>{_cdata(item.content)}</description>")
        out.append("</item>")

    out.append("</channel>")
    out.append("</rss>")
    return "\n".join(out) + "\n"
