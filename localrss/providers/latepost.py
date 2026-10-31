"""晚点LatePost provider（直连站点，不需要 WebBridge）。

列表来自站点自己的接口：

    POST https://www.latepost.com/news/get-news-data
    page / limit / programa（0 全部，1 晚点独家，2 人物对话，4 长报道）

详情页是服务端渲染的，正文就在 `#select-main` 里，不需要登录，也不依赖 JS。
每条只抓一次详情，全文随条目存进 state；之后增量只看列表里有没有新 id。

注意这里**不靠 id 递增去猜**：文章 id 看起来是连续递增的，但按 id 扫得先知道
最新 id，遇到已删除/未发布的空号还得跳过；列表接口直接给时间序、标题和摘要，
翻页也更稳。
"""

from __future__ import annotations

import html
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from ..bridge import Bridge
from ..models import Item
from . import register
from .base import Provider, retry_call

BASE = "https://www.latepost.com"
NEWS_PAGE = f"{BASE}/news"
LIST_API = f"{BASE}/news/get-news-data"
DETAIL_URL = f"{BASE}/news/dj_detail?id={{}}"

#: 晚点按北京时间发稿；站点没带时区，按 +08:00 解释。
CST = timezone(timedelta(hours=8))

#: 站点对普通 urllib UA 也放行，但带上浏览器 UA 更稳。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

#: programa 的取值和页面上的栏目一一对应（0 = 不传，也就是全部）。
PROGRAMAS = {
    0: "全部",
    1: "晚点独家",
    2: "人物对话",
    4: "长报道",
}

_RE_TAGS = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"\s+")
_RE_TITLE = re.compile(
    r'<div class="article-header-title">(.*?)</div>', re.S
)
_RE_DATE = re.compile(r'<div class="article-header-date">(.*?)</div>', re.S)
_RE_RELEASE = re.compile(r"var\s+release_time\s*=\s*'([^']+)'", re.S)
_RE_DETAIL_DATE = re.compile(
    r"(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?"
)
_RE_LABEL = re.compile(
    r'<(?:span|a)\b[^>]*class="[^"]*\blabel\b[^"]*"[^>]*>(.*?)</(?:span|a)>',
    re.S,
)
_RE_IMG = re.compile(r"<img\b[^>]*>", re.I)
_RE_SRC = re.compile(r'\bsrc="([^"]*)"', re.I)
_RE_ALT = re.compile(r'\balt="([^"]*)"', re.I)


def _squash(text: str) -> str:
    """去标签、反转义、把连续空白压成一个空格。"""
    text = html.unescape(_RE_TAGS.sub(" ", text or ""))
    return _RE_WS.sub(" ", text).strip()


def _abs_url(url: str) -> str:
    """把站点里的相对地址补成绝对地址；http 的图片统一提到 https。"""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _div_inner(page: str, marker: str) -> str:
    """取包含 marker 的那层 <div> 的内部 HTML（按 div 配平，能处理嵌套）。"""
    pos = page.find(marker)
    if pos < 0:
        return ""
    start = page.rfind("<div", 0, pos)
    if start < 0:
        return ""
    depth = 0
    for m in re.finditer(r"<(/?)div\b[^>]*>", page[start:], re.I):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            outer = page[start:start + m.end()]
            return outer[outer.find(">") + 1:outer.rfind("</div>")]
    return ""


def _parse_ymd(raw: str) -> datetime | None:
    """2026/10/19 这种详情页里的日期。"""
    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", (raw or "").strip())
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=CST)


def _parse_list_time(raw: str, now: datetime | None = None) -> datetime | None:
    """列表里的「今天 / 昨天 / 10月29日 / 2025年09月29日」。"""
    text = _squash(raw)
    if not text:
        return None
    now = now or datetime.now(CST)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text in ("今天", "刚刚"):
        return midnight
    if text == "昨天":
        return midnight - timedelta(days=1)

    m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        tzinfo=CST)

    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year
        # 列表里只有「月日」：如果这个日期比今天还晚，说明是去年发的
        # （比如 1 月看到「12月30日」）。跨年后的新文章会带「2027年01月05日」。
        if datetime(year, month, day, tzinfo=CST) > now + timedelta(days=1):
            year -= 1
        return datetime(year, month, day, tzinfo=CST)
    return None


def _parse_detail_time(page: str, fallback: datetime | None) -> datetime | None:
    """详情页的精确时间：日期取 release_time，时分取页头的「10月19日 19:10」。

    页头有时是绝对日期，有时是「今天 00:10 / 昨天 21:10」。日期始终以
    release_time 为准，页头只负责补时分。
    """
    base = fallback
    m = _RE_RELEASE.search(page)
    if m:
        base = _parse_ymd(m.group(1)) or base

    header = _RE_DATE.search(page)
    text = _squash(header.group(1)) if header else _squash(page)

    m = re.search(r"(?:今天|昨天)?\s*(\d{1,2}):(\d{2})", text)
    if m and base is not None:
        try:
            return base.replace(
                hour=int(m.group(1)), minute=int(m.group(2)),
                second=0, microsecond=0,
            )
        except ValueError:
            return base

    m = _RE_DETAIL_DATE.search(text)
    if m and base is not None:
        hour = int(m.group(3) or 0)
        minute = int(m.group(4) or 0)
        try:
            return base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return base
    return base


def _parse_author(page: str) -> str:
    """从文章头部取「文 / 编辑」，拼成 dc:creator 用的一行。"""
    block = _div_inner(page, 'class="article-header-author"')
    if not block:
        return ""
    authors: list[str] = []
    editors: list[str] = []
    for seg in re.split(r'<div class="author-link">', block)[1:]:
        names = [
            _squash(x) for x in _RE_LABEL.findall(seg)
        ]
        names = [n for n in names if n]
        if not names:
            continue
        if re.search(r">\s*编辑\s*<", seg):
            editors.extend(names)
        else:
            authors.extend(names)
    out = "、".join(authors)
    if editors:
        suffix = "编辑：" + "、".join(editors)
        out = f"{out}（{suffix}）" if out else suffix
    return out


def _parse_abstract(page: str) -> str:
    for marker in ('class="abstract-pic-left"', 'class="abstract-txt"'):
        inner = _div_inner(page, marker)
        text = _squash(inner)
        if text:
            return text
    return ""


def _fix_img(match: re.Match) -> str:
    tag = match.group(0)
    src = _RE_SRC.search(tag)
    url = _abs_url(src.group(1)) if src else ""
    if not url:
        return ""
    alt = _RE_ALT.search(tag)
    alt_text = html.unescape(alt.group(1)) if alt else ""
    return (
        f'<img src="{html.escape(url, quote=True)}"'
        f' alt="{html.escape(alt_text, quote=True)}">'
    )


def _sanitize_body(raw: str) -> str:
    """正文里只留阅读器认的 HTML，顺手把图片/链接补成绝对地址。"""
    raw = re.sub(r"<script\b.*?</script>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<style\b.*?</style>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<iframe\b.*?</iframe>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    raw = _RE_IMG.sub(_fix_img, raw)
    raw = re.sub(r'\s+on\w+="[^"]*"', "", raw, flags=re.I)
    raw = re.sub(
        r'([Hh][Rr][Ee][Ff])="(/[^"]*)"',
        lambda m: f'{m.group(1)}="{html.escape(_abs_url(m.group(2)), quote=True)}"',
        raw,
    )
    return raw.strip()


class _Http:
    """带 TLS 兜底的极简 HTTP 客户端。

    晚点现在用的是 Let's Encrypt 新一代 YR2 中间证书，根证书 ISRG Root YR
    还不在很多本机 CA 库里（macOS 自带的、Python 用的都可能是旧的），标准校验会
    报 `unable to get local issuer certificate`。这里默认 `auto`：先正常校验，
    只有明确是证书链验不过时才退回不校验，并打一行警告 —— 只读公开新闻，
    不涉及登录态。装了根证书（或不想看到提示）就把 tls_verify 写成 true。
    """

    def __init__(self, timeout: int = 30, tls_verify="auto", ca_file: str = ""):
        self.timeout = timeout
        self.verify = tls_verify
        self._warned = False
        if tls_verify is False or str(tls_verify).lower() in ("false", "0", "no", "off"):
            self._ctx = ssl._create_unverified_context()
        else:
            self._ctx = ssl.create_default_context(cafile=ca_file or None)

    def _open(self, req: urllib.request.Request):
        try:
            return urllib.request.urlopen(
                req, timeout=self.timeout, context=self._ctx
            )
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            if (
                str(self.verify).lower() == "auto"
                and isinstance(reason, ssl.SSLCertVerificationError)
            ):
                if not self._warned:
                    print(
                        "    [latepost] TLS 证书校验失败（本机 CA 库可能还没有 "
                        "ISRG Root YR），本轮改用不校验证书直连。装好根证书后可把 "
                        "options.tls_verify 写成 true，或用 ca_file 指定 PEM。",
                        file=sys.stderr,
                    )
                    self._warned = True
                self._ctx = ssl._create_unverified_context()
                self.verify = False
                return urllib.request.urlopen(
                    req, timeout=self.timeout, context=self._ctx
                )
            raise

    def post_form(self, url: str, fields: dict) -> bytes:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        with self._open(req) as resp:
            return resp.read()

    def get(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with self._open(req) as resp:
            return resp.read().decode("utf-8", "replace")


@register
class LatepostProvider(Provider):
    type = "latepost"
    description = "晚点LatePost（https://www.latepost.com/news，直连，不需要浏览器）"
    needs_browser = False

    def __init__(self, options: dict | None = None, common: dict | None = None):
        super().__init__(options, common)
        try:
            self.programa = int(self.opt("programa", 0) or 0)
        except (TypeError, ValueError):
            raise ValueError("latepost: options.programa 必须是 0 / 1 / 2 / 4")
        if self.programa not in PROGRAMAS:
            raise ValueError(
                "latepost: options.programa 只能是 0（全部）、1（晚点独家）、"
                "2（人物对话）、4（长报道）"
            )
        self.limit = max(1, int(self.opt("limit", 20)))
        self.max_pages = max(1, int(self.opt("max_pages", 3)))
        self.fulltext = str(self.opt("fulltext", True)).lower() not in (
            "false", "0", "no", "off",
        )
        self.delay_s = max(0.0, float(self.opt("delay_s", 0.3)))
        self.http = _Http(
            timeout=int(self.opt("timeout", 30)),
            tls_verify=self.opt("tls_verify", "auto"),
            ca_file=str(self.opt("ca_file", "") or ""),
        )

    # ---------- 抓取 ----------

    def page_url(self) -> str:
        return NEWS_PAGE

    def setup(self, bridge: Bridge) -> None:
        # 直连站点，不需要切标签页。
        return None

    def _list_page(self, page: int) -> list[dict]:
        raw = self.http.post_form(
            LIST_API,
            {"page": page, "limit": self.limit, "programa": self.programa},
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise RuntimeError(f"列表接口返回的不是 JSON：{e}") from e
        if payload.get("code") != 1:
            raise RuntimeError(
                f"列表接口返回 code={payload.get('code')}："
                f"{payload.get('msg') or payload}"
            )
        return list(payload.get("data") or [])

    def _detail(self, news_id: str) -> str:
        return self.http.get(DETAIL_URL.format(news_id))

    def _from_row(self, row: dict) -> Item | None:
        news_id = str(row.get("id") or "").strip()
        if not news_id:
            return None
        link = _abs_url(row.get("detail_url") or f"/news/dj_detail?id={news_id}")
        title = _squash(row.get("title", ""))
        abstract = _squash(row.get("abstract") or row.get("intro") or "")
        cover = _abs_url(row.get("cover", ""))
        published = _parse_list_time(row.get("release_time", ""))
        return Item(
            id=f"latepost:{news_id}",
            title=title or f"晚点LatePost {news_id}",
            link=link,
            published=published,
            content=self._summary_html(cover, abstract),
            extra={"news_id": news_id},
        )

    @staticmethod
    def _summary_html(cover: str, abstract: str) -> str:
        parts: list[str] = []
        if cover:
            parts.append(
                f'<p><img src="{html.escape(cover, quote=True)}" alt=""></p>'
            )
        if abstract:
            parts.append(f"<p>{html.escape(abstract)}</p>")
        return "\n".join(parts)

    def _with_fulltext(self, item: Item) -> Item:
        news_id = item.extra["news_id"]
        page = self._detail(news_id)

        title_m = _RE_TITLE.search(page)
        if title_m:
            title = _squash(title_m.group(1))
            if title:
                item.title = title

        published = _parse_detail_time(page, item.published)
        if published:
            item.published = published

        author = _parse_author(page)
        if author:
            item.author = author

        body = _sanitize_body(_div_inner(page, 'id="select-main"'))
        if not body:
            abstract = _parse_abstract(page)
            print(
                f"    [latepost] {item.link} 没解析到正文，先发列表摘要",
                file=sys.stderr,
            )
            item.content = self._summary_html("", abstract) or item.content
            return item

        # 正文里没有图时，把列表封面放在开头；有图就不重复贴。
        if "<img" not in body.lower():
            cover = ""
            m = _RE_IMG.search(item.content or "")
            if m:
                src = _RE_SRC.search(m.group(0))
                cover = src.group(1) if src else ""
            if cover:
                body = (
                    f'<p><img src="{html.escape(cover, quote=True)}" alt=""></p>\n'
                    + body
                )

        item.content = body
        item.extra["fulltext"] = True
        return item

    def fetch(self, bridge: Bridge | None, known_ids: set[str]) -> list[Item]:
        items: list[Item] = []
        seen: set[str] = set()
        fetched_detail = 0

        for page in range(1, self.max_pages + 1):
            rows = retry_call(
                lambda p=page: self._list_page(p),
                attempts=3,
                delay_s=5.0,
                label="latepost 列表",
            )
            if not rows:
                break

            new_on_page = 0
            for row in rows:
                news_id = str(row.get("id") or "").strip()
                if not news_id or news_id in seen:
                    continue
                seen.add(news_id)
                if f"latepost:{news_id}" in known_ids:
                    continue
                item = self._from_row(row)
                if item is None:
                    continue

                if self.fulltext:
                    # 详情抓失败就这轮先不发；下一轮还在列表里，会再试。
                    try:
                        item = retry_call(
                            lambda it=item: self._with_fulltext(it),
                            attempts=2,
                            delay_s=5.0,
                            label=f"latepost 详情 {news_id}",
                        )
                    except Exception as e:
                        print(
                            f"    [latepost] 详情 {news_id} 抓取失败，本轮跳过：{e}",
                            file=sys.stderr,
                        )
                        continue
                    fetched_detail += 1
                    if self.delay_s:
                        time.sleep(self.delay_s)

                items.append(item)
                new_on_page += 1

            # 整页都是已有记录就停：增量轮次通常第一页就结束。
            if known_ids and new_on_page == 0:
                break
            if len(rows) < self.limit:
                break

        if fetched_detail:
            print(
                f"    [latepost] 补了 {fetched_detail} 篇全文",
                file=sys.stderr,
            )
        return items
