"""config.yaml 的解析。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .bridge import DEFAULT_URL


@dataclass
class FeedConfig:
    id: str
    type: str
    title: str = ""
    description: str = ""
    site_url: str = ""
    file: str = ""
    enabled: bool = True
    history: int | None = None  # 覆盖 output.history；None 表示用全局默认
    hub_url: str = ""  # 覆盖 output.hub_url；空字符串表示用全局默认
    options: dict = field(default_factory=dict)
    #: 该源额外的关键词过滤，叠加在顶层 exclude_keywords 之上（只对本源生效）
    exclude_keywords: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return self.file or f"{self.id}.xml"


@dataclass
class Config:
    path: Path
    base_dir: Path
    bridge_url: str
    session: str
    group_title: str
    output_dir: Path
    state_dir: Path
    log_dir: Path
    history: int
    base_url: str
    hub_url: str
    close_session: bool
    exclude_keywords: list[str]
    #: 把每轮被过滤掉的条目写一份清单（logs/<feed-id>.dropped.log，覆盖写）
    dropped_log: bool
    feeds: list[FeedConfig]

    def enabled_feeds(self, only: list[str] | None = None) -> list[FeedConfig]:
        feeds = [f for f in self.feeds if f.enabled]
        if not only:
            return feeds
        wanted = set(only)
        selected = [f for f in feeds if f.id in wanted]
        missing = wanted - {f.id for f in selected}
        if missing:
            raise ValueError(f"config 里没有这些源: {', '.join(sorted(missing))}")
        return selected


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"找不到配置文件: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base_dir = path.parent

    bridge_cfg = raw.get("bridge") or {}
    output_cfg = raw.get("output") or {}

    def resolve(value: str, default: str) -> Path:
        p = Path(value or default)
        return p if p.is_absolute() else (base_dir / p)

    feeds: list[FeedConfig] = []
    for i, entry in enumerate(raw.get("feeds") or []):
        if not isinstance(entry, dict):
            raise ValueError(f"feeds[{i}] 必须是映射")
        feed_id = entry.get("id")
        if not feed_id:
            raise ValueError(f"feeds[{i}] 缺少 id")
        type_name = entry.get("type")
        if not type_name:
            raise ValueError(f"feeds[{i}] ({feed_id}) 缺少 type")
        feeds.append(
            FeedConfig(
                id=str(feed_id),
                type=str(type_name),
                title=entry.get("title") or str(feed_id),
                description=entry.get("description") or "",
                site_url=entry.get("site_url") or "",
                file=entry.get("file") or "",
                enabled=bool(entry.get("enabled", True)),
                history=int(entry["history"]) if entry.get("history") else None,
                hub_url=str(entry.get("hub") or "").rstrip("/"),
                options=dict(entry.get("options") or {}),
                exclude_keywords=[
                    str(k)
                    for k in (entry.get("exclude_keywords") or [])
                    if str(k).strip()
                ],
            )
        )

    return Config(
        path=path,
        base_dir=base_dir,
        bridge_url=bridge_cfg.get("url") or DEFAULT_URL,
        session=bridge_cfg.get("session") or "local-rss",
        group_title=bridge_cfg.get("group_title") or "本地 RSS",
        output_dir=resolve(output_cfg.get("dir"), "output"),
        state_dir=resolve(output_cfg.get("state_dir"), "state"),
        log_dir=resolve(output_cfg.get("log_dir"), "logs"),
        history=int(output_cfg.get("history", 300)),
        base_url=str(output_cfg.get("base_url") or "").rstrip("/"),
        hub_url=str(output_cfg.get("hub_url") or "").rstrip("/"),
        close_session=bool(bridge_cfg.get("close_session", True)),
        dropped_log=bool(output_cfg.get("dropped_log", False)),
        exclude_keywords=[
            str(k) for k in (raw.get("exclude_keywords") or []) if str(k).strip()
        ],
        feeds=feeds,
    )
