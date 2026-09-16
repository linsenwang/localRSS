"""每个源一份本地状态文件（JSON），用于去重和滚动的 RSS 窗口。

除了条目本身，还存一份**跨窗口的正文指纹表**（`seen_content`）：
条目会被 `history` 裁掉，这份表不会。用它去重的理由是「这条正文已经发出去过」，
不能随着条目滚出窗口就忘掉 —— 详见 providers/zhihu.py 的 `_dedupe_by_content`。
Store 只负责读写这份表，谁往里登记、怎么用由 provider 决定。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import Item, sort_key


class Store:
    def __init__(self, path: Path, history: int = 300):
        self.path = Path(path)
        self.history = max(1, int(history))
        #: 跨窗口的正文指纹表 {指纹: 首次发布时间 ISO}，见 set_seen_content
        self._seen: dict[str, str] = {}
        self._seen_loaded = False

    def _read_payload(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _ensure_seen(self) -> None:
        """保证指纹表这份数据从文件里读过一遍。

        没读过就写盘会把它整份抹掉，所以 save() 之前也得确认一下
        （直接 new 一个 Store 然后 save(items) 的用法不能把表写没了）。
        """
        if not self._seen_loaded:
            self._seen = dict(self._read_payload().get("seen_content") or {})
            self._seen_loaded = True

    def seen_content(self) -> dict[str, str]:
        """跨窗口的正文指纹表（副本）。"""
        self._ensure_seen()
        return dict(self._seen)

    def set_seen_content(self, seen: dict[str, str]) -> None:
        """整份替换指纹表；落盘由 save() 负责。"""
        self._seen = dict(seen or {})
        self._seen_loaded = True

    def load(self) -> list[Item]:
        data = self._read_payload()
        self._seen = dict(data.get("seen_content") or {})
        self._seen_loaded = True
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
        self._ensure_seen()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated": datetime.now().isoformat(timespec="seconds"),
            # 跨窗口去重用的正文指纹：条目会被 history 裁掉，这份表不会被裁
            "seen_content": self._seen,
            "items": [i.to_dict() for i in items],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)
