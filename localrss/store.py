"""每个源一份本地状态文件（JSON），用于去重和滚动的 RSS 窗口。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import Item, sort_key


class Store:
    def __init__(self, path: Path, history: int = 300):
        self.path = Path(path)
        self.history = max(1, int(history))

    def load(self) -> list[Item]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [Item.from_dict(d) for d in data.get("items", [])]

    def known_ids(self) -> set[str]:
        return {i.id for i in self.load()}

    def merge(self, new_items: list[Item]) -> tuple[list[Item], int]:
        """把新条目并入状态；返回（按时间倒序的完整列表，新增条数）。"""
        existing = {i.id: i for i in self.load()}
        added = 0
        for item in new_items:
            if not item.id:
                continue
            old = existing.get(item.id)
            if old is None:
                added += 1
            elif old.extra.get("content_locked") and not item.extra.get("content_locked"):
                # 老条目的 content 是花代价抓来的（比如知乎全文），
                # 别让本次重新渲染出来的裸摘要把它覆盖回去。
                item.content = old.content
                item.extra = {**old.extra, **item.extra}
            existing[item.id] = item

        items = sorted(existing.values(), key=sort_key, reverse=True)[: self.history]
        self.save(items)
        return items, added

    def save(self, items: list[Item]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated": datetime.now().isoformat(timespec="seconds"),
            "items": [i.to_dict() for i in items],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)
