"""IT之家（博客版）provider（直连站点，不需要 WebBridge）。

列表页就是 https://www.ithome.com/blog/：首屏 50 条，翻页靠「加载更多」：

    POST https://www.ithome.com/category/blogpage?page=<n>&ot=<最后一条的毫秒时间戳>
    -> {"success": true, "content": {"html": "<li>…</li>…", "count": 50}}

ot 取自当前页最后一条的 `data-ot`（ISO 时间），是服务端用来接着往更早翻的游标；
page 只是序号。返回空 count 就到头了。

IT之家发稿量很大，单条进 RSS 会很吵，所以默认把攒够的 `digest_size` 条
（20）合成一条「合集」：一条里列出这 20 条的标题、时间、摘要和链接。
单条本身仍然留在 state / archive 里；`digest_size: 0` 可以关掉合集。
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime

from ..bridge import Bridge
from ..models import Item, sort_key
from . import register
from .base import Provider, plain_text, retry_call

BASE = "https://www.ithome.com"
LIST_URL = f"{BASE}/blog/"
MORE_URL = f"{BASE}/category/blogpage?page={{page}}&ot={{ot}}"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

#: 合集条目的 extra.kind，用来把它和真实文章区分开
DIGEST_KIND = "ithome_digest"

#: 广告条目：列表里混着的「值得买」推广，链接都落在 lapin.ithome.com
DEFAULT_AD_HOSTS = ("lapin.ithome.com",)

#: 额外屏蔽的关键词（和用户脚本里那份保持一致）
DEFAULT_BLOCK_KEYWORDS = ("追觅", "云鲸")

_RE_LIST = re.compile(r'<ul class="bl">(.*?)</ul>', re.S)
_RE_LI = re.compile(r"<li>(.*?)</li>", re.S)
_RE_TITLE_A = re.compile(r'<a\b(?=[^>]*\bclass="title")[^>]*>(.*?)</a>', re.S)
_RE_ATTR = re.compile(r'\b(href|title|data-original|src)="([^"]*)"', re.I)
_RE_OT = re.compile(r'data-ot="([^"]+)"')
_RE_SUMMARY = re.compile(r'<div class="m">(.*?)</div>', re.S)
_RE_TAGS = re.compile(r'<div class="tags">(.*?)</div>', re.S)
_RE_TAG = re.compile(r'<a\b[^>]*>(.*?)</a>', re.S)
_RE_TAGS_STRIP = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"\s+")


def _squash(text: str) -> str:
    """去标签、反转义、压空白。"""
    text = html.unescape(_RE_TAGS_STRIP.sub(" ", text or ""))
    return _RE_WS.sub(" ", text).strip()


def _abs_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url


def _attrs(tag: str) -> dict[str, str]:
    return {k.lower(): v for k, v in _RE_ATTR.findall(tag)}


def _parse_time(raw: str) -> datetime | None:
    """`data-ot` 是 ISO 时间，小数秒有 7 位；Python 只稳定支持到 6 位。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    raw = re.sub(r"(\.\d{6})\d+", r"\1", raw)
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _url_id(link: str, fallback: str = "") -> str:
    m = re.search(r"/(\d+)\.htm", link or "")
    if m:
        return m.group(1)
    return re.sub(r"\W+", "", link or fallback)[-40:]


def _item_is_article(item: Item) -> bool:
    return item.extra.get("kind") != DIGEST_KIND and bool(item.extra.get("ithome_id"))


def _digest_title(batch: list[Item]) -> str:
    first, last = batch[0].published, batch[-1].published
    if first and last:
        return (f"IT之家 {len(batch)} 条"
                f"（{first:%m-%d %H:%M} ~ {last:%m-%d %H:%M}）")
    return f"IT之家 {len(batch)} 条"


def _digest_entry_html(item: Item) -> str:
    """一条合集成员：缩略图在左，标题 + 时间/摘要在右（和网页列表一致）。"""
    when = item.published.strftime("%m-%d %H:%M") if item.published else ""
    title = html.escape(item.title or "(无标题)", quote=True)
    link = html.escape(_abs_url(item.link), quote=True)
    summary = _squash(item.extra.get("summary") or "")
    text = f'<a href="{link}"><strong>{title}</strong></a>'
    meta = when
    if summary:
        meta += f" · {html.escape(summary)}"
    body = (
        f'<p style="margin:0 0 4px 0;line-height:1.45">{text}</p>'
        + (
            f'<p style="margin:0;color:#666;font-size:90%;line-height:1.45">'
            f"{meta}</p>"
            if meta else ""
        )
    )

    thumb = item.extra.get("thumb") or ""
    if not thumb:
        return f'<div style="margin:0 0 16px 0">{body}</div>'
    src = html.escape(_abs_url(thumb), quote=True)
    # 表格比 flex/float 稳：阅读器就算把内联样式洗掉，width/valign 也还能撑住左右两栏。
    return (
        '<table cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;margin:0 0 16px 0;width:100%">'
        "<tr>"
        '<td width="132" valign="top" '
        'style="width:132px;padding:0 12px 0 0;vertical-align:top">'
        f'<a href="{link}"><img src="{src}" width="120" alt="" '
        'style="width:120px;height:auto;border-radius:4px;display:block"></a>'
        "</td>"
        '<td valign="top" style="vertical-align:top">'
        f"{body}"
        "</td>"
        "</tr></table>"
    )


def _build_digest(batch: list[Item]) -> Item:
    """把一批文章（batch 按时间从旧到新）合成一条合集。"""
    newest = batch[-1]
    for i in batch:
        i.extra["digest_member"] = True
    return Item(
        id=f"ithome:digest:{_url_id(batch[0].link, batch[0].id)}",
        title=_digest_title(batch),
        link=LIST_URL,
        author="IT之家",
        published=newest.published,
        content="".join(_digest_entry_html(i) for i in reversed(batch)),
        extra={
            "kind": DIGEST_KIND,
            "digest_ids": [i.id for i in batch],
        },
    )


def _digested_ids(items: list[Item]) -> set[str]:
    out: set[str] = set()
    for i in items:
        if i.extra.get("kind") == DIGEST_KIND:
            out.update(i.extra.get("digest_ids") or [])
    return out


def _age_hours(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return (now - dt).total_seconds() / 3600


class _Http:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def _open(self, req: urllib.request.Request):
        return urllib.request.urlopen(req, timeout=self.timeout)

    def get(self, url: str) -> str:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Referer": LIST_URL,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        with self._open(req) as resp:
            return resp.read().decode("utf-8", "replace")

    def post_empty(self, url: str) -> bytes:
        req = urllib.request.Request(
            url,
            data=b"",
            headers={
                "User-Agent": UA,
                "Referer": LIST_URL,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            method="POST",
        )
        with self._open(req) as resp:
            return resp.read()


@register
class IthomeProvider(Provider):
    type = "ithome"
    description = "IT之家博客版（https://www.ithome.com/blog/，直连，不需要浏览器）"
    needs_browser = False

    def __init__(self, options: dict | None = None, common: dict | None = None):
        super().__init__(options, common)
        self.max_pages = max(1, int(self.opt("max_pages", 2)))
        self.digest_size = max(0, int(self.opt("digest_size", 20)))
        try:
            self.digest_max_age_h = max(0.0, float(
                self.opt("digest_max_age_hours", 24)
            ))
        except (TypeError, ValueError):
            self.digest_max_age_h = 24.0

        def _list_opt(key: str, default: tuple[str, ...]) -> list[str]:
            value = self.opt(key, list(default))
            if isinstance(value, str):
                value = [value]
            return [str(v).strip() for v in (value or []) if str(v).strip()]

        self.ad_hosts = [h.lower() for h in _list_opt(
            "block_ad_hosts", DEFAULT_AD_HOSTS
        )]
        self.block_keywords = _list_opt(
            "block_keywords", DEFAULT_BLOCK_KEYWORDS
        )
        self.http = _Http(timeout=int(self.opt("timeout", 30)))

    # ---------- 抓取 ----------

    def page_url(self) -> str:
        return LIST_URL

    def setup(self, bridge: Bridge) -> None:
        # 直连站点，不需要切标签页。
        return None

    def _parse_html(self, page: str) -> tuple[list[Item], str]:
        """从一段列表 HTML 里解析条目；第二个返回值是最后一条的 ot（翻页游标）。"""
        m = _RE_LIST.search(page or "")
        block = m.group(1) if m else page

        items: list[Item] = []
        last_ot = ""
        for li in _RE_LI.findall(block):
            am = _RE_TITLE_A.search(li)
            if not am:
                continue
            attrs = _attrs(am.group(0))
            link = _abs_url(attrs.get("href", ""))
            if not link:
                continue
            title = _squash(attrs.get("title") or am.group(1))
            title = title or "(无标题)"

            ot_m = _RE_OT.search(li)
            ot = ot_m.group(1) if ot_m else ""
            if ot:
                last_ot = ot
            published = _parse_time(ot)

            img_m = re.search(r"<img\b[^>]*>", li, re.I)
            img_attrs = _attrs(img_m.group(0)) if img_m else {}
            thumb = _abs_url(
                img_attrs.get("data-original") or img_attrs.get("src") or ""
            )

            summary_m = _RE_SUMMARY.search(li)
            summary = _squash(summary_m.group(1)) if summary_m else ""

            tags_m = _RE_TAGS.search(li)
            tags = [
                _squash(t) for t in _RE_TAG.findall(tags_m.group(1))
                if _squash(t)
            ] if tags_m else []

            content = ""
            if thumb:
                content += (
                    f'<p><img src="{html.escape(thumb, quote=True)}" alt=""></p>\n'
                )
            if summary:
                content += f"<p>{html.escape(summary)}</p>"
            if tags:
                content += f"<p>Tags：{html.escape('、'.join(tags))}</p>"

            items.append(Item(
                id=f"ithome:{link}",
                title=title,
                link=link,
                author="IT之家",
                published=published,
                content=content,
                extra={
                    "ithome_id": _url_id(link),
                    "summary": summary,
                    "thumb": thumb,
                    "tags": tags,
                },
            ))
        return items, last_ot

    def _more_page(self, page_no: int, ot: str) -> tuple[list[Item], str]:
        url = MORE_URL.format(
            page=page_no, ot=urllib.parse.quote(str(ot), safe="")
        )
        raw = retry_call(
            lambda: self.http.post_empty(url),
            attempts=3,
            delay_s=5.0,
            label="ithome 翻页",
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise RuntimeError(f"翻页接口返回的不是 JSON：{e}") from e
        content = payload.get("content") or {}
        if not payload.get("success") or not int(content.get("count") or 0):
            return [], ""
        return self._parse_html(content.get("html") or "")

    def fetch(self, bridge: Bridge | None, known_ids: set[str]) -> list[Item]:
        out: list[Item] = []
        seen: set[str] = set()

        def collect(items: list[Item]) -> int:
            added = 0
            for item in items:
                if item.id in seen:
                    continue
                seen.add(item.id)
                if item.id in known_ids:
                    continue
                out.append(item)
                added += 1
            return added

        first = retry_call(
            lambda: self.http.get(LIST_URL),
            attempts=3,
            delay_s=5.0,
            label="ithome 列表",
        )
        page_items, ot = self._parse_html(first)
        pages = 1
        new_on_page = collect(page_items)

        # 整页都是已有记录就停：增量轮次通常第一页就结束。
        #
        # page 必须从 1 开始，不能从 2（这里之前写错过，导致每轮固定漏掉
        # 紧挨着的一整页 50 条）：站点的「加载更多」是 page=1、2、3… 的
        # 偏移量，和 ot 游标一起用。page=2 配第一页的 ot 会直接跳到第二个
        # 50 条区块，把第一个区块整段跳过；随着首页窗口滑动，被跳过的内容
        # 会换一批，看起来就像早上的文章「晚几个小时才放出来」。
        # `max_pages` 是总页数（含首屏），所以这里只跑到 max_pages - 1。
        if not known_ids or new_on_page:
            for page_no in range(1, self.max_pages):
                if not ot:
                    break
                items, next_ot = self._more_page(page_no, ot)
                if not items:
                    break
                pages += 1
                ot = next_ot or ot
                new_on_page = collect(items)
                if known_ids and new_on_page == 0:
                    break

        print(
            f"    [ithome] 翻了 {pages} 页，抓到 {len(out)} 条新文章",
            file=sys.stderr,
        )
        return out

    # ---------- 后处理：广告/关键词过滤 + 20 条合成一条 ----------

    def _blocked_reason(self, item: Item) -> str:
        """对应网页端那个油猴脚本：广告域名 + 指定关键词。"""
        host = (urllib.parse.urlparse(_abs_url(item.link)).hostname or "").lower()
        for ad in self.ad_hosts:
            if host == ad or host.endswith("." + ad):
                return f"广告域名 {ad}"
        text = plain_text(item).lower()
        for keyword in self.block_keywords:
            if keyword.lower() in text:
                return f"关键词「{keyword}」"
        return ""

    def _apply_digest(
        self, items: list[Item], size: int, max_age_h: float
    ) -> tuple[list[Item], list[Item]]:
        digested = _digested_ids(items)
        for i in items:
            if i.id in digested:
                i.extra["digest_member"] = True

        articles = [i for i in items if _item_is_article(i)]
        others = [i for i in items if not _item_is_article(i)]
        alone = [i for i in articles if i.extra.get("digest_alone")]
        pending = sorted(
            (
                i for i in articles
                if i.id not in digested
                and not i.extra.get("digest_member")
                and not i.extra.get("digest_alone")
            ),
            key=sort_key,
        )

        new: list[Item] = []
        while len(pending) >= size:
            batch, pending = pending[:size], pending[size:]
            new.append(_build_digest(batch))

        lone: list[Item] = []
        if pending and max_age_h > 0 and _age_hours(pending[0].published) >= max_age_h:
            if len(pending) == 1:
                pending[0].extra["digest_alone"] = True
                lone.append(pending[0])
            else:
                new.append(_build_digest(pending))
            pending = []

        self.grouped_items = len(articles) - len(alone) - len(lone)
        for d in new:
            self.derived_items += 1
            print(f"    [合集] 合成 {len(d.extra['digest_ids'])} 条：{d.title}")

        if pending:
            note = f"在攒 {len(pending)}/{size} 条（还差 {size - len(pending)} 条）"
            note += f"；最旧一条 {_age_hours(pending[0].published):.1f}h 前"
            note += (f"，满 {max_age_h:g}h 也会照样发"
                     if max_age_h > 0 else "，不兜底（攒满才发）")
            print(f"    [合集] {note}")

        return others + alone + lone + new, new

    def postprocess(self, items: list[Item], bridge: Bridge) -> list[Item]:
        # 合集自己不过关键词：20 条里只要有一条命中，整条合集（20 条内容）
        # 都会被筛掉。先筛单条，再用筛过的条目合成。
        digests = [i for i in items if i.extra.get("kind") == DIGEST_KIND]
        articles = [i for i in items if i.extra.get("kind") != DIGEST_KIND]
        # 注意传副本：下面要的是「哪些能进合集」，而 cli 之后落盘的 merged
        # 还得保留被过滤的条目 —— 这样改规则才能立刻重新筛，汇总行也才算得出
        # 「过滤 N 条」。
        articles = super().postprocess(list(articles), bridge)

        # 网页端油猴脚本的两条规则：广告域名（lapin.ithome.com）+ 屏蔽关键词。
        # 必须放在合成合集之前，被屏掉的条目才不会进合集。
        kept: list[Item] = []
        for item in articles:
            reason = self._blocked_reason(item)
            if reason:
                self.note_drop(item, reason)
            else:
                kept.append(item)
        articles = kept

        if self.digest_size <= 0:
            return articles + digests

        visible, new = self._apply_digest(
            articles + digests, self.digest_size, self.digest_max_age_h
        )
        # 新建的合集要写进 state（store.save(merged)），否则下一轮看不到
        # digest_ids，成员被标成 digest_member 却再也合不出合集。
        items.extend(new)
        return visible

    def absorbed_ids(self, items: list[Item]) -> set[str]:
        """合集成员的正文已经在合集条目里了，滚出窗口时不用再单独归档。"""
        return _digested_ids(items) | {
            i.id for i in items if i.extra.get("digest_member")
        }
