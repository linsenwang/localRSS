"""统一的条目数据结构，所有 provider 都产出 Item。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Item:
    """一条动态，字段与具体站点无关。"""

    id: str  # 稳定唯一 ID（用于去重）
    title: str = ""
    link: str = ""
    author: str = ""
    published: datetime | None = None
    content: str = ""  # RSS description，允许内嵌 HTML
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "title": self.title,
            "link": self.link,
            "author": self.author,
            "content": self.content,
            "extra": self.extra,
        }
        d["published"] = self.published.isoformat() if self.published else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        raw = d.get("published")
        published = None
        if raw:
            try:
                published = datetime.fromisoformat(raw)
            except ValueError:
                published = None
        return cls(
            id=d.get("id", ""),
            title=d.get("title", ""),
            link=d.get("link", ""),
            author=d.get("author", ""),
            published=published,
            content=d.get("content", ""),
            extra=d.get("extra") or {},
        )


def sort_key(item: Item) -> float:
    """按发布时间倒序排序用的键；没有时间的排到最后。"""
    if item.published is None:
        return float("-inf")
    dt = item.published
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
