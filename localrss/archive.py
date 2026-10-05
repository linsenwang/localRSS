"""持续归档：把滚出 RSS 窗口的条目永久留在本地。

`state/<id>.json` 只保留最近 `history` 条 —— 那是给阅读器的 RSS 窗口，条数上限
是它那边的量级。更老的条目本来会在下一次 merge 时被裁掉，内容再也找不回来。
归档把「本轮从 state 里消失的条目」追加进 `archive/<id>.jsonl`，一直留着：
RSS 还是原来那么多条，历史一条不丢。

单个文件涨到 `output.archive_rotate_mb`（默认 100 MB）就压成
`archive/<id>.<时间戳>.jsonl.gz`，另起一个空的 `.jsonl` 继续追加。`.gz` 是整份的
gzip，`gzip -dc` / `zgrep` 直接能读，攒多少份都不影响后面继续追加。
"""

from __future__ import annotations

import glob
import gzip
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from .models import Item

#: 一行一条记录：{"archived_at": "...", "item": {...Item.to_dict()...}}
Record = dict


class Archive:
    """一个源一份归档：`archive/<feed-id>.jsonl` + 若干个轮转出来的 `.gz`。"""

    def __init__(
        self,
        directory: Path,
        feed_id: str,
        rotate_bytes: int = 100 * 1024 * 1024,
    ):
        self.dir = Path(directory)
        self.feed_id = feed_id
        #: 单个文件超过这么多字节就压缩轮转；<= 0 表示不轮转（一直追加一个文件）
        self.rotate_bytes = max(0, int(rotate_bytes))
        self.live = self.dir / f"{feed_id}.jsonl"
        #: 压缩中途用：先把 live 改名到这里，压好再删。崩在这一步也能恢复。
        self.rotating = self.dir / f"{feed_id}.jsonl.rotating"

    # ---------- 写 ----------

    def append(self, items: list[Item]) -> int:
        """把条目追加进归档，返回写入条数。需要轮转时顺带压一份。"""
        if not items:
            return 0
        self.dir.mkdir(parents=True, exist_ok=True)
        self._recover()
        now = datetime.now().isoformat(timespec="seconds")
        with self.live.open("a", encoding="utf-8") as f:
            for item in items:
                rec: Record = {"archived_at": now, "item": item.to_dict()}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._rotate_if_needed()
        return len(items)

    def _recover(self) -> None:
        """上次压缩到一半留下的 `.rotating`：接着压完，别让它把这份数据带走。"""
        if not self.rotating.exists():
            return
        ts = datetime.fromtimestamp(self.rotating.stat().st_mtime)
        self._compress(self.rotating, self._rotated_name(ts))
        self.rotating.unlink()

    def _rotate_if_needed(self) -> Path | None:
        if self.rotate_bytes <= 0 or not self.live.exists():
            return None
        if self.live.stat().st_size < self.rotate_bytes:
            return None
        return self._rotate()

    def _rotate(self) -> Path:
        target = self._rotated_name(datetime.now())
        # 先改名再压：压缩途中崩了，下次 _recover() 还能接着压，不会丢也不会重复
        os.replace(self.live, self.rotating)
        self._compress(self.rotating, target)
        self.rotating.unlink()
        return target

    def _rotated_name(self, when: datetime) -> Path:
        # 名字里带序号，同一秒里连转两次也不会互相覆盖；序号保证按名字排序 = 按时间排序
        ts = when.strftime("%Y%m%d-%H%M%S")
        n = 1
        target = self.dir / f"{self.feed_id}.{ts}-{n}.jsonl.gz"
        while target.exists():
            n += 1
            target = self.dir / f"{self.feed_id}.{ts}-{n}.jsonl.gz"
        return target

    @staticmethod
    def _compress(src: Path, dst: Path) -> None:
        tmp = dst.with_name(dst.name + ".tmp")
        with src.open("rb") as fin, gzip.open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        os.replace(tmp, dst)

    # ---------- 读 ----------

    def files(self) -> list[Path]:
        """归档文件，从旧到新（轮转出来的 .gz 在前，当前 .jsonl 在最后）。"""
        pattern = f"{glob.escape(self.feed_id)}.*.jsonl.gz"
        rotated = sorted(self.dir.glob(pattern))
        return rotated + ([self.live] if self.live.exists() else [])

    def iter_records(self):
        """逐条读出 {"archived_at": ..., "item": {...}}，坏行跳过。"""
        for path in self.files():
            opener = gzip.open if path.suffix == ".gz" else open
            try:
                with opener(path, "rt", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(rec, dict) and isinstance(rec.get("item"), dict):
                            yield rec
            except (OSError, EOFError):  # 截断/损坏的 .gz 直接跳过，别让整份读不出来
                continue

    def iter_items(self):
        for rec in self.iter_records():
            yield Item.from_dict(rec["item"])

    def summary(self) -> dict:
        """只数文件和体积（不读内容），`main.sh status` 用它。"""
        files = self.files()
        return {
            "files": len(files),
            "bytes": sum(p.stat().st_size for p in files if p.exists()),
            "live_bytes": self.live.stat().st_size if self.live.exists() else 0,
        }

    def stats(self) -> dict:
        """文件/体积 + 条目数 + 发布时间范围（要解压读完，慢一点）。"""
        s = self.summary()
        count = 0
        first: datetime | None = None
        last: datetime | None = None
        for item in self.iter_items():
            count += 1
            if item.published is None:
                continue
            if first is None or item.published < first:
                first = item.published
            if last is None or item.published > last:
                last = item.published
        s.update({"items": count, "first": first, "last": last})
        return s


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
