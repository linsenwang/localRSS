"""B 站消息中心 provider（收到的赞 / 回复我的 / @我的）。

对应这几个页面：
  - https://message.bilibili.com/#/love   收到的赞
  - https://message.bilibili.com/#/reply  回复我的
  - https://message.bilibili.com/#/at     @我的

和 `bilibili` 的「关注动态」不同，这里抓的是**你自己收到的消息**。三个 tab 用的是
同一族接口 `api.bilibili.com/x/msgfeed/<entry>`，形状略有差别：

  - love  : data = {latest: {items, last_view_at}, total: {cursor, items}}
            latest 是「上次看过之后的新赞」，total 是全部；两边合起来按 id 去重。
  - reply : data = {cursor, items, last_view_at}
  - at    : 同 reply

翻页参数也不是页码，而是上一页最后一条的游标：
  - love  : id + like_time
  - reply : id + reply_time
  - at    : id + at_time

（这些都是 message.bilibili.com 页面自己的 JS 在用的参数，见页面里的
`this.offset={id:r.id.toString(),reply_time:r.time}` 之类。）
"""

from __future__ import annotations

import html
import sys
import time
from datetime import datetime
from urllib.parse import urlencode

from ..bridge import Bridge
from ..models import Item
from . import register
from .base import Provider, retry_call

API = "https://api.bilibili.com/x/msgfeed/{entry}"
MESSAGE_PAGE = "https://message.bilibili.com/#/{tab}"
MESSAGE_HOST = "message.bilibili.com"

#: entry_name 可选值 -> 页面上的名字。all 是三种合并，不是接口自己的分类。
ENTRIES = {
    "all": "全部通知",
    "love": "收到的赞",
    "reply": "回复我的",
    "at": "@我的",
}
#: all 展开成这三个分类
ALL_ENTRIES = ("love", "reply", "at")
#: 分类 -> 接口路径。注意页面的 hash 是 `#/love`，接口却叫 `like` —— 写成 love
#: 会得到一个 404 HTML 页面（`x/msgfeed/love` 不存在）。
API_ENTRY = {"love": "like", "reply": "reply", "at": "at"}
#: 翻页游标里时间戳那个参数的名字
CURSOR_PARAM = {"love": "like_time", "reply": "reply_time", "at": "at_time"}
#: 没有 business 时按内容类型给个中文名（赞的是评论还是动态）
TYPE_LABEL = {
    "reply": "评论",
    "danmu": "弹幕",
    "dynamic": "动态",
    "video": "视频",
    "article": "专栏",
    "audio": "音频",
    "live": "直播",
}

#: 标签页落到 message.bilibili.com 的等待上限（navigate 是异步的）
PAGE_SETTLE_S = 4.0

EXTRACT_JS = r"""
(async () => {
  try {
    if (location.hostname !== 'message.bilibili.com') {
      return { ok: false, not_on_page: true,
               error: '当前标签页不在 B 站消息中心（' + location.href + '）' };
    }
    const r = await fetch('__URL__', { credentials: 'include' });
    const text = await r.text();
    let j = null;
    try {
      j = JSON.parse(text);
    } catch (e) {
      // 路径写错（比如 x/msgfeed/love）或风控时会回一个 HTML 错误页。
      return { ok: false, status: r.status,
               error: 'HTTP ' + r.status + ' 返回的不是 JSON：'
                      + text.slice(0, 80).replace(/\s+/g, ' ') };
    }
    if (j.code !== 0) {
      return { ok: false, code: j.code, error: j.message || ('HTTP ' + r.status) };
    }
    const d = j.data || {};
    const kind = '__KIND__';
    let raw = [];
    let cursor = null;
    if (kind === 'love') {
      const total = d.total || {};
      // latest 是「上次看过之后的新赞」，total 是全部；重叠的部分在本地按 id 去重。
      raw = ((d.latest || {}).items || []).concat(total.items || []);
      cursor = total.cursor || null;
    } else {
      raw = d.items || [];
      cursor = d.cursor || null;
    }
    return {
      ok: true,
      items: raw.map(function (x) { return pack(x, kind); }),
      is_end: !!(cursor && cursor.is_end),
      cursor: cursor ? { id: String(cursor.id || ''), time: cursor.time || 0 } : null
    };
  } catch (e) {
    return { ok: false, error: String(e) };
  }

  // love 给的是 users（一次点赞可能合并了好几个人），reply / at 给的是单个 user。
  function users(x) {
    const list = x.users || (x.user ? [x.user] : []);
    return list.map(function (u) {
      return { name: u.nickname || '', avatar: u.avatar || '', mid: u.mid || 0 };
    });
  }

  function pack(x, kind) {
    const it = x.item || {};
    const ts = kind === 'love' ? (x.like_time || 0)
             : kind === 'at' ? (x.at_time || x.reply_time || 0)
             : (x.reply_time || 0);
    return {
      id: String(x.id || ''),
      kind: kind,
      users: users(x),
      counts: Number(x.counts || 0),
      ts: Number(ts || 0),
      type: it.type || '',
      business: it.business || '',
      title: it.title || '',
      uri: it.uri || '',
      // reply / at：对方说的话（source_content）；love 没有这段
      source: it.source_content || '',
      // reply：被回复的那条（你的原评论）；at / love 一般没有
      root: it.root_reply_content || '',
      target: it.target_reply_content || ''
    };
  }
})()
"""


def _esc(s: str) -> str:
    return html.escape(s or "")


def _text_html(s: str) -> str:
    return _esc(s).replace("\n", "<br>")


def _abs_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://www.bilibili.com" + u
    if u.startswith("http://"):
        return "https://" + u[len("http://"):]
    return u


def _entry_label(entry: str) -> str:
    return ENTRIES.get(entry, entry)


def _actors(o: dict) -> str:
    """谁做的这件事。合并通知（多人点赞）会有好几个名字。"""
    users = o.get("users") or []
    names = [str(u.get("name") or "").strip() for u in users]
    names = [n for n in names if n]
    counts = int(o.get("counts") or 0)
    if not names:
        return "有人"
    who = "、".join(names)
    if counts > len(names):
        who += f" 等 {counts} 人"
    return who


def _action(o: dict) -> str:
    """动作，不含人名（标题里由 _actors 带上）。"""
    kind = o.get("kind")
    business = str(o.get("business") or "").strip()
    if kind == "love":
        label = business or TYPE_LABEL.get(o.get("type") or "", "内容")
        return f"赞了你的{label}"
    if kind == "at":
        return f"在{business}中@了你" if business else "@了你"
    # reply：B 站只有「评论」「弹幕」这两种 business
    if business and business != "评论":
        return f"回复了你的{business}"
    return "回复了你"


def _first_line(s: str, limit: int = 100) -> str:
    for line in (s or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line if len(line) <= limit else line[:limit] + "…"
    return ""


def _subject(o: dict) -> str:
    """标题里跟在动作后面的那段内容。

    reply / at 用 `source_content`（对方回复的正文 / @ 你的那段），love 没有这段，
    用 `title`（被赞的那条内容）。都取第一行并截断，回复动辄几百字，标题里放不下。
    """
    if o.get("kind") == "love":
        return _first_line(o.get("title") or "")
    return _first_line(o.get("source") or "") or _first_line(o.get("title") or "")


def _title(o: dict) -> str:
    """标题：`谁 + 干了什么 + ： + 相关内容`（和知乎通知保持一致）。"""
    head = " ".join(f"{_actors(o)} {_action(o)}".split())
    target = _subject(o)
    if target and target not in head:
        return f"{head}：{target}" if head else target
    return head or "B站通知"


def _link(o: dict) -> str:
    """消息指向的内容页；内容被删了（uri 为空）就退回消息中心对应的 tab。"""
    link = _abs_url(o.get("uri") or "")
    return link or MESSAGE_PAGE.format(tab=o.get("kind") or "love")


def _content_html(o: dict) -> str:
    kind = o.get("kind")
    action = _action(o)
    source = str(o.get("source") or "").strip()
    title = str(o.get("title") or "").strip()
    root = str(o.get("root") or "").strip()
    link = _link(o)

    parts = [f"<p>{_esc(action)}</p>"]
    shown = {action}
    if source:
        parts.append(f"<p>{_text_html(source)}</p>")
        shown.add(source)
    if title and title not in shown:
        # reply 的 title 是「你被回复的那条评论」，love 的是「被赞的内容」，
        # at 的 title 是内容本身的标题（视频标题之类）。
        label = {"reply": "你的原评论：", "at": "相关内容："}.get(kind, "")
        parts.append(f"<p>{label}{_text_html(title)}</p>")
        shown.add(title)
    if root and root not in shown:
        parts.append(f"<p>你的原评论：{_text_html(root)}</p>")
    if link:
        parts.append(f'<p><a href="{_esc(link)}">{_esc(link)}</a></p>')
    return "".join(parts)


@register
class BilibiliNotifyProvider(Provider):
    type = "bilibili_notify"
    description = "B 站消息中心（收到的赞 / 回复我的 / @我的）"

    @property
    def entries(self) -> list[str]:
        entry = str(self.opt("entry_name", "all") or "all").strip()
        if entry not in ENTRIES:
            known = "、".join(f"{k}（{v}）" for k, v in ENTRIES.items())
            raise ValueError(f"bilibili_notify: 未知的通知分类 entry_name={entry!r}，可选：{known}")
        return list(ALL_ENTRIES) if entry == "all" else [entry]

    def page_url(self) -> str:
        return MESSAGE_PAGE.format(tab=self.entries[0])

    def setup(self, bridge: Bridge) -> None:
        """把标签页切到 message.bilibili.com。

        消息接口是同站请求（api.bilibili.com 和 message.bilibili.com 同一个注册域），
        所以只要页面停在消息中心就行，停在哪个 tab 都无所谓。真正要防的是跨站：
        上一条源留下的标签页还停在 t.bilibili.com 上时，fetch 会带上别的站的身份，
        所以这里等页面真的落到 message.bilibili.com 再往下走。
        """
        page = bridge.current_page()
        if page.get("host") == MESSAGE_HOST:
            return
        expected = self.page_url()
        bridge.ensure_tab(expected, group_title=self.opt("group_title"))
        deadline = time.monotonic() + PAGE_SETTLE_S
        while time.monotonic() < deadline:
            host = bridge.evaluate("location.hostname") or ""
            if host == MESSAGE_HOST:
                return
            time.sleep(0.4)
        print(f"    [标签页] 当前标签页还没落到 {expected}，继续尝试", file=sys.stderr)

    def _page_url(self, entry: str, cursor: dict | None) -> str:
        params = {"platform": "web", "build": "0", "mobi_app": "web"}
        if cursor and cursor.get("id"):
            params["id"] = str(cursor["id"])
            params[CURSOR_PARAM[entry]] = str(cursor.get("time") or 0)
        return f"{API.format(entry=API_ENTRY[entry])}?{urlencode(params)}"

    def fetch(self, bridge: Bridge, known_ids: set[str]) -> list[Item]:
        max_pages = int(self.opt("max_pages", 5))
        attempts = int(self.opt("list_retries", 2)) + 1
        delay_s = float(self.opt("list_retry_delay_s", 15))
        items: list[Item] = []
        seen: set[str] = set()

        for entry in self.entries:
            cursor: dict | None = None
            for page in range(1, max_pages + 1):
                result = retry_call(
                    lambda e=entry, c=cursor, p=page: self._fetch_page(bridge, e, c, p),
                    attempts=attempts, delay_s=delay_s,
                    label=f"B 站消息（{_entry_label(entry)}）第 {page} 页",
                )
                raw_items = result.get("items") or []
                if not raw_items:
                    break

                page_ids = {
                    self._item_id(o) for o in raw_items if o.get("id")
                }
                for o in raw_items:
                    item_id = self._item_id(o)
                    if not o.get("id") or item_id in seen:
                        continue
                    seen.add(item_id)
                    items.append(self._to_item(o))

                if known_ids and page_ids and page_ids <= known_ids:
                    break
                if result.get("is_end"):
                    break
                cursor = result.get("cursor")
                if not cursor or not cursor.get("id"):
                    break

        return items

    def _fetch_page(self, bridge: Bridge, entry: str, cursor: dict | None,
                    page: int) -> dict:
        url = self._page_url(entry, cursor)
        code = EXTRACT_JS.replace("__URL__", url).replace("__KIND__", entry)
        for attempt in range(2):
            result = bridge.evaluate(code)
            if result and result.get("ok"):
                return result
            if result and result.get("not_on_page") and attempt == 0:
                print(f"    [标签页] {result.get('error')}，切回消息中心后重试",
                      file=sys.stderr)
                self.setup(bridge)
                continue
            break
        detail = (result or {}).get("error") or result
        raise RuntimeError(
            f"B 站消息接口返回异常（{_entry_label(entry)}第 {page} 页）: {detail}"
        )

    def _item_id(self, o: dict) -> str:
        return f"bilibili-notif:{o.get('kind', '')}:{o.get('id', '')}"

    def _to_item(self, o: dict) -> Item:
        ts = int(o.get("ts") or 0)
        published = datetime.fromtimestamp(ts) if ts else None
        return Item(
            id=self._item_id(o),
            title=_title(o),
            link=_link(o),
            author=_actors(o),
            published=published,
            content=_content_html(o),
            extra={
                "kind": "notification",
                "entry": o.get("kind", ""),
                "business": o.get("business", ""),
                "type": o.get("type", ""),
                "counts": int(o.get("counts") or 0),
            },
        )
