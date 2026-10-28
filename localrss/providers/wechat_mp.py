"""微信公众号推送 provider（读本地微信数据库，不需要浏览器）。

数据来自 WeChat 4.x 解密后的 `wechat_decrypted/message/biz_message_*.db`：
每个公众号在库里是一张 `Msg_<md5(username)>` 表，图文消息的 `message_content`
是 zstd 压缩的 appmsg XML，标题和文章链接都在里面。

默认还会用文章链接抓一次正文（`options.fulltext`），正文缓存在 state 里，
每条只抓一次。`options.db_dir` 指向解密的 wechat 目录。
"""

from __future__ import annotations

import hashlib
import html
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

from ..bridge import Bridge
from ..models import Item
from . import register
from .base import Provider

#: local_type 的低 32 位是消息大类，49 = appmsg（公众号图文/链接卡片）。
#: 高位是微信自己塞的标志位，直接比 49 会漏掉全部图文消息。
APP_MSG_TYPE = 49

_RE_APPMSG = re.compile(r"<appmsg\b.*?</appmsg>", re.S)
_RE_TITLE = re.compile(r"<title><!\[CDATA\[(.*?)\]\]></title>", re.S)
_RE_TITLE_PLAIN = re.compile(r"<title>(.*?)</title>", re.S)
_RE_URL = re.compile(
    r"<url><!\[CDATA\[(https?://mp\.weixin\.qq\.com/[^\]]+)\]\]>", re.S
)
_RE_WS = re.compile(r"\s+")
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

#: 抓正文时用的 UA：默认的 Python-urllib 会被微信拦。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

#: 正文里直接丢掉整个子树的标签（脚本、视频播放器这类阅读器也不认）。
_SKIP_TAGS = {
    "script", "style", "iframe", "video", "audio", "svg", "button",
    "form", "noscript", "mpvoice", "mpvideo", "qqmusic",
}
#: 保留的标签；其余（section/div/span 等排版壳）拆掉，只留里面的文字。
_KEEP_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "a", "img",
    "table", "thead", "tbody", "tr", "td", "th", "code", "pre", "hr",
    "sub", "sup",
}
_VOID_TAGS = {"br", "img", "hr"}
_BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr", "pre"}

#: 这些提示说明文章本身没了，重试也没用；"环境异常" 是风控，下一轮还会再试。
_GONE_HINTS = (
    "该内容已被发布者删除",
    "已被发布者删除",
    "此内容因违规无法查看",
    "帐号已被屏蔽",
    "该公众号已迁移",
)


class ContentUnavailable(Exception):
    """文章页明确说了内容没了（删除/违规），不是网络问题。"""


class _ArticleSanitizer(HTMLParser):
    """把 #js_content 里那坨带内联样式的 section 洗成阅读器能看的 HTML。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[tuple[str, str | None]] = []
        self.skip = 0

    def _img(self, attrs: dict) -> str:
        src = attrs.get("data-src") or attrs.get("src") or ""
        if not src:
            return ""
        out = f'<img src="{html.escape(src, quote=True)}"'
        if attrs.get("alt"):
            out += f' alt="{html.escape(attrs["alt"], quote=True)}"'
        return out + ">"

    def handle_starttag(self, tag, attrs):
        if self.skip:
            self.stack.append((tag, None))
            return
        if tag in _SKIP_TAGS:
            self.skip += 1
            self.stack.append((tag, None))
            return
        if tag == "img":
            out = self._img(dict(attrs))
            if out:
                self.out.append(out)
                self.stack.append((tag, "img"))
            else:
                self.stack.append((tag, None))
            return
        if tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if href.startswith(("http://", "https://")):
                self.out.append(f'<a href="{html.escape(href, quote=True)}">')
                self.stack.append((tag, "a"))
            else:
                self.stack.append((tag, None))
            return
        if tag in _KEEP_TAGS:
            self.out.append(f"<{tag}>")
            self.stack.append((tag, tag))
            return
        self.stack.append((tag, None))

    def handle_startendtag(self, tag, attrs):
        if self.skip:
            return
        if tag == "img":
            self.out.append(self._img(dict(attrs)))
        elif tag == "br":
            self.out.append("<br>")
        elif tag == "hr":
            self.out.append("<hr>")

    def handle_endtag(self, tag):
        # HTMLParser 偶尔会给出没配对的结束标签，按最近的同名开标签收尾。
        idx = next(
            (k for k in range(len(self.stack) - 1, -1, -1) if self.stack[k][0] == tag),
            None,
        )
        if idx is None:
            return
        while len(self.stack) > idx:
            orig, emitted = self.stack.pop()
            if orig in _SKIP_TAGS:
                self.skip = max(0, self.skip - 1)
            elif emitted and emitted not in _VOID_TAGS:
                self.out.append(f"</{emitted}>")
                if emitted in _BLOCK_TAGS:
                    self.out.append("\n")

    def handle_data(self, data):
        if self.skip or not data:
            return
        text = _RE_WS.sub(" ", data)
        if text:
            self.out.append(html.escape(text))


def _js_content(page: str) -> str | None:
    """取出 <div id="js_content"> 的内部 HTML（里面还有嵌套 div）。"""
    marker = re.search(r"""id=["']js_content["']""", page)
    if marker is None:
        return None
    start = page.rfind("<div", 0, marker.start())
    if start < 0:
        return None
    depth = 0
    for m in re.finditer(r"<(/?)div\b[^>]*>", page[start:], re.I):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            outer = page[start:start + m.end()]
            return outer[outer.find(">") + 1:outer.rfind("</div>")]
    return None


def _sanitize(fragment: str) -> str:
    parser = _ArticleSanitizer()
    parser.feed(fragment)
    parser.close()
    text = "".join(parser.out)
    text = re.sub(r"<p>\s*(?:<br>)?\s*</p>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _download(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "identity",
        },
    )
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def _article_html(url: str) -> str:
    """下载文章页并返回洗好的正文；文章被删/违规时抛 ContentUnavailable。"""
    page = _download(url)
    body = _js_content(page)
    if body is None:
        for hint in _GONE_HINTS:
            if hint in page:
                raise ContentUnavailable(hint)
        raise RuntimeError("页面里没有 js_content（可能触发了微信的环境异常验证）")
    return _sanitize(body)


def _zstd() -> str:
    return shutil.which("zstd") or "/opt/homebrew/bin/zstd"


def _decode(raw, compression) -> str:
    """message_content 可能是明文 TEXT，也可能是 zstd 压缩的 BLOB。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if compression == 4 or raw[:4] == _ZSTD_MAGIC:
        proc = subprocess.run([_zstd(), "-d", "-c"], input=raw, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"zstd 解压失败：{proc.stderr.decode('utf-8', 'replace').strip()}"
            )
        return proc.stdout.decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def _title(text: str) -> str:
    return _RE_WS.sub(" ", html.unescape(text or "")).strip()


def _link(url: str) -> str:
    url = url.strip()
    if url.endswith("#rd"):
        url = url[:-3]
    return url


def _articles(content: str) -> list[tuple[str, str]]:
    """从一个 message_content 里取 (标题, 文章链接)。

    一条消息通常只有一个 <appmsg>，多图文推送会拆成多条消息；
    真遇到一条里塞了多个的，就逐个取。
    """
    blocks = _RE_APPMSG.findall(content) or [content]
    found: list[tuple[str, str]] = []

    for block in blocks:
        url_match = _RE_URL.search(block)
        if url_match is None:
            continue
        title_match = _RE_TITLE.search(block) or _RE_TITLE_PLAIN.search(block)
        title = _title(title_match.group(1)) if title_match else ""
        found.append((title, _link(url_match.group(1))))
    return found


@register
class WechatMpProvider(Provider):
    type = "wechat_mp"
    description = "微信公众号推送（本地微信数据库，标题 + 链接 + 正文）"
    needs_browser = False

    def page_url(self) -> str:
        return "https://mp.weixin.qq.com/"

    def setup(self, bridge: Bridge | None) -> None:
        self._refresh_db()

    def _refresh_db(self) -> None:
        """可选：先从微信原始库重新解密一遍，再读。

        `options.refresh_db` 里给出 decrypt_db.py 的 python / script / keys，
        用 --skip-unchanged 增量解密（源库没变的会直接跳过，通常 1~3 秒）。
        失败不致命 —— 继续用 db_dir 里现有的数据，下一轮再来。
        """
        cfg = self.opt("refresh_db")
        if not cfg:
            return
        if not isinstance(cfg, dict):
            print("    [微信] refresh_db 要写成 python/script/keys 三个键，跳过刷新",
                  file=sys.stderr)
            return
        python = str(cfg.get("python") or sys.executable)
        script = str(cfg.get("script") or "")
        keys = str(cfg.get("keys") or "")
        if not script or not keys:
            print("    [微信] refresh_db 缺少 script/keys，跳过刷新", file=sys.stderr)
            return

        cmd = [
            python, script, "--keys", keys,
            "-o", str(self._db_dir()), "--skip-unchanged",
        ]
        print("    [微信] 先刷新解密数据库...")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except Exception as e:
            print(f"    [微信] 刷新失败，继续用现有数据库：{e}", file=sys.stderr)
            return
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            print(f"    [微信] 刷新失败（退出码 {proc.returncode}），"
                  f"继续用现有数据库：{detail[-1] if detail else ''}", file=sys.stderr)
            return
        summary = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        print(f"    [微信] {summary[-1] if summary else '已刷新'}")

    def postprocess(self, items: list[Item], bridge: Bridge | None) -> list[Item]:
        items = super().postprocess(items, bridge)
        if not self.opt("fulltext", True):
            return items

        budget = int(self.opt("max_fulltext_per_run", 30))
        pending = [
            i for i in items
            if not i.extra.get("content_locked") and not i.extra.get("no_fulltext")
        ]
        if not pending or budget <= 0:
            return items

        todo = pending[:budget]
        print(f"    [全文] 待补 {len(pending)} 篇，本次抓 {len(todo)} 篇")
        held: set[str] = set()
        for n, item in enumerate(todo, 1):
            try:
                body = _article_html(item.link)
            except ContentUnavailable as e:
                # 文章被删/违规：重试也没用，标掉别再占配额，标题和链接照发
                item.extra["no_fulltext"] = True
                print(f"    [全文] {n}/{len(todo)} 原文不可用（{e}）：{item.title[:30]}")
            except Exception as e:
                # 网络/风控问题：这轮先不发，下一轮补上正文再放出去。
                # 发出去阅读器就按 guid 收下那条空的了，之后 feed 里补上正文也不会刷新
                # （见 README「阅读器那一侧的缓存」）。
                item.extra["fulltext_error"] = str(e)
                held.add(item.id)
                print(f"    [全文] {n}/{len(todo)} 失败，本轮先不发：{item.title[:30]}：{e}",
                      file=sys.stderr)
            else:
                item.content = body
                item.extra["fulltext"] = True
                # 告诉 Store：这条的 content 是最终版，别被下一轮重新渲染的空正文覆盖
                item.extra["content_locked"] = True
                item.extra.pop("fulltext_error", None)
                print(f"    [全文] {n}/{len(todo)} {item.author}（{len(body)} 字符）")
            if n < len(todo):
                time.sleep(random.uniform(0.4, 1.0))

        # 配额用完没轮到的也先扣住，等下一轮补上正文再发
        held.update(i.id for i in pending[budget:])
        for item in items:
            if item.id in held:
                # cli 靠这个标记识别「上一轮扣住、这一轮放出来」的条目
                item.extra["held"] = True
        self.held_ids = held
        if held:
            print(f"    [全文] {len(held)} 条正文还没到手，本轮先不发（补上再发）")
            items = [i for i in items if i.id not in held]
        return items

    def _cutoff(self):
        """只保留最近 `options.days` 天；days <= 0 表示不按时间截断。"""
        days = float(self.opt("days", 7) or 0)
        if days <= 0:
            return None
        return datetime.now().astimezone() - timedelta(days=days)

    def prune_superseded(self, items):
        """把滚出一周窗口的旧条目从 state 里清掉（它们会照常进 archive）。"""
        cutoff = self._cutoff()
        if cutoff is None:
            return items
        return [i for i in items if i.published is None or i.published >= cutoff]

    def _db_dir(self) -> Path:
        raw = self.require("db_dir")
        return Path(str(raw)).expanduser().resolve()

    def _accounts(self, db_dir: Path) -> dict[str, str]:
        """username -> 昵称。contact.db 是主来源，session.db 补已删联系人的公众号。"""
        names: dict[str, str] = {}
        contact = db_dir / "contact" / "contact.db"
        if contact.exists():
            with sqlite3.connect(f"file:{contact}?mode=ro", uri=True) as con:
                for username, nick in con.execute(
                    "select username, nick_name from contact"
                    " where substr(username, 1, 3) = 'gh_'"
                ):
                    names[username] = (nick or "").strip() or username

        session = db_dir / "session" / "session.db"
        if session.exists():
            with sqlite3.connect(f"file:{session}?mode=ro", uri=True) as con:
                for (username,) in con.execute(
                    "select user_name from Name2Id"
                    " where substr(user_name, 1, 3) = 'gh_'"
                ):
                    names.setdefault(username, username)
        return names

    def fetch(self, bridge: Bridge | None, known_ids: set[str]) -> list[Item]:
        db_dir = self._db_dir()
        message_dir = db_dir / "message"
        if not message_dir.is_dir():
            raise FileNotFoundError(f"找不到微信消息目录: {message_dir}")

        names = self._accounts(db_dir)
        by_hash = {hashlib.md5(u.encode()).hexdigest(): u for u in names}

        only = {str(a).strip() for a in (self.opt("accounts") or []) if str(a).strip()}
        if only:
            allowed = {
                u for u, nick in names.items() if u in only or nick in only
            }
            unknown = only - allowed - set(names)
            if unknown:
                print(
                    f"[{self.type}] 忽略未找到的公众号: {', '.join(sorted(unknown))}",
                    file=sys.stderr,
                )
        else:
            allowed = set(names)

        items: dict[str, Item] = {}
        cutoff = self._cutoff()
        min_ts = int(cutoff.timestamp()) if cutoff else None
        for db_path in sorted(message_dir.glob("biz_message_*.db")):
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
                tables = [
                    r[0]
                    for r in con.execute(
                        "select name from sqlite_master where type = 'table'"
                        " and name like 'Msg_%'"
                    )
                ]
                for table in tables:
                    username = by_hash.get(table[4:])
                    if username is None or username not in allowed:
                        continue
                    sql = (
                        f'select create_time, message_content,'
                        f" WCDB_CT_message_content from \"{table}\""
                        f" where (local_type & 4294967295) = {APP_MSG_TYPE}"
                    )
                    if min_ts is not None:
                        # 时间窗口以外的不解压（省下每条一次 zstd 的开销）
                        sql += f" and create_time >= {min_ts}"
                    for create_time, raw, compression in con.execute(sql):
                        for title, link in _articles(_decode(raw, compression)):
                            item_id = "mp-" + hashlib.md5(link.encode()).hexdigest()
                            old = items.get(item_id)
                            if old is not None and old.title:
                                continue
                            items[item_id] = Item(
                                id=item_id,
                                title=title,
                                link=link,
                                author=names[username],
                                published=datetime.fromtimestamp(
                                    int(create_time)
                                ).astimezone(),
                            )
        return list(items.values())
