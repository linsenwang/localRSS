"""B 站动态 provider。

两种模式（options.mode）：
  - feed  : 关注动态（对应 https://t.bilibili.com/ 首页），默认
  - space : 指定 UP 主的空间动态（需要 options.uid）

接口走 api.bilibili.com 的 polymer 动态接口，在浏览器里带 cookie 请求。
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from ..bridge import Bridge
from ..models import Item
from . import register
from .base import Provider

API_FEED = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
API_SPACE = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
DYNAMIC_URL = "https://t.bilibili.com/"
VIDEO_URL = "https://www.bilibili.com/video/"
PLAYER_URL = "https://player.bilibili.com/player.html"
# 移动版播放页：用另一套播放器（mplayer.js），喂渐进式 mp4，iOS 能播；
# 代价是它在 <video> 上写死了 playsinline，iOS 拿不到原生全屏入口。
MOBILE_PLAYER_URL = "https://www.bilibili.com/blackboard/html5mobileplayer.html"
# 网页版播放器（MSE/DASH）。不带 playsinline，但 iOS 对 MSE 支持不全。
#   实测 html5player 能完整加载（readyState 4、拿到时长）
HTML5_PLAYER_URL = "https://www.bilibili.com/blackboard/html5player.html"
# 实测这个卡在「播放器初始化...」出不来，留作对照
NEW_PLAYER_URL = "https://www.bilibili.com/blackboard/player.html"

#: embed_player 可选值 -> URL 模板
#: desktop 严格按官方文档的参数来（https://player.bilibili.com/player.html）：
#: 只使用文档列出的 bvid / p / autoplay / danmaku / poster，不用未文档化的 high_quality
PLAYER_STYLES = {
    "mobile": MOBILE_PLAYER_URL + "?bvid={bvid}&page=1&as_wide=1&high_quality=1&danmaku=0",
    "desktop": PLAYER_URL + "?bvid={bvid}&p=1&autoplay=0&danmaku=0&poster=1",
    "html5player": HTML5_PLAYER_URL + "?bvid={bvid}&page=1&as_wide=1&high_quality=1&danmaku=0",
    "newplayer": NEW_PLAYER_URL + "?bvid={bvid}&page=1&as_wide=1&high_quality=1&danmaku=0&autoplay=0",
}

# 在页面里把接口返回的动态压成扁平结构，避免把上百 KB 的原始 JSON 搬回来。
EXTRACT_JS = r"""
(async () => {
  try {
    const r = await fetch('__URL__', { credentials: 'include' });
    const j = await r.json();
    if (j.code !== 0) { return { ok: false, code: j.code, message: j.message }; }
    const d = j.data || {};
    const out = (d.items || []).map(pack);
    return { ok: true, has_more: !!d.has_more, offset: d.offset || null, items: out };
  } catch (e) {
    return { ok: false, error: String(e) };
  }

  function pack(x) {
    const mods = x.modules || {};
    const a = mods.module_author || {};
    const dyn = mods.module_dynamic || {};
    const major = dyn.major || {};
    const o = {
      id: x.id_str || '',
      type: x.type || '',
      name: a.name || '',
      mid: a.mid || 0,
      face: a.face || '',
      ts: parseInt(a.pub_ts || '0', 10),
      text: (dyn.desc && dyn.desc.text) || '',
      mt: major.type || '',
      archive: null, draw: null, article: null, opus: null, common: null, orig: null
    };
    if (major.archive) {
      o.archive = {
        title: major.archive.title || '',
        bvid: major.archive.bvid || '',
        cover: major.archive.cover || '',
        desc: major.archive.desc || ''
      };
    }
    if (major.draw && major.draw.items) {
      o.draw = major.draw.items.map(function (i) { return i.src || ''; }).filter(Boolean);
    }
    if (major.article) {
      o.article = {
        title: major.article.title || '',
        desc: major.article.desc || '',
        covers: major.article.covers || [],
        jump: major.article.jump_url || ''
      };
    }
    if (major.opus) {
      o.opus = {
        title: major.opus.title || '',
        summary: (major.opus.summary && major.opus.summary.text) || '',
        jump: major.opus.jump_url || '',
        pics: (major.opus.pics || []).map(function (p) { return p.url || ''; }).filter(Boolean)
      };
    }
    if (major.common) {
      o.common = {
        title: major.common.title || '',
        desc: major.common.desc || '',
        cover: major.common.cover || '',
        jump: major.common.jump_url || ''
      };
    }
    o.orig = x.orig ? pack(x.orig) : null;
    return o;
  }
})()
"""


def _abs_url(u: str) -> str:
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://www.bilibili.com" + u
    if u.startswith("http://"):
        # 阅读器页面多半是 https，http 图片会被浏览器当混合内容拦掉
        return "https://" + u[len("http://"):]
    return u


def _img_tag(url: str) -> str:
    return f'<img src="{_esc(_abs_url(url))}">'


def _player_style(value) -> str:
    """把 embed_player 配置归一成 PLAYER_STYLES 里的键，或 '' 表示关闭。"""
    if value is False or value is None:
        return ""
    text = str(value).strip().lower()
    if text in ("false", "off", "none", "0", ""):
        return ""
    if text in ("true", "on", "1"):
        return "mobile"
    alias = {"player": "desktop", "html5": "html5player", "new": "newplayer"}
    text = alias.get(text, text)
    return text if text in PLAYER_STYLES else "mobile"


def _player_iframe(bvid: str, style: str = "mobile") -> str:
    """B 站官方嵌入式播放器，让阅读器里能直接播。

    allow 里显式声明 fullscreen：跨域 iframe 要用 Fullscreen API 必须有这个权限策略，
    只靠老式的 allowfullscreen 属性在 WKWebView 下不一定生效。
    """
    template = PLAYER_STYLES.get(style) or PLAYER_STYLES["mobile"]
    src = template.format(bvid=bvid)
    return (
        f'<iframe src="{_esc(src)}" width="640" height="360" frameborder="0"'
        f' allow="autoplay; fullscreen; encrypted-media; picture-in-picture"'
        f' allowfullscreen="true" scrolling="no"></iframe>'
    )


def _esc(s: str) -> str:
    return html.escape(s or "")


def _text_html(s: str) -> str:
    return _esc(s).replace("\n", "<br>")


def _avatar_html(o: dict) -> str:
    """UP 主头像。RSS 没有「每条一个图标」的字段，只能放进正文里。"""
    face = o.get("face")
    if not face:
        return ""
    return (
        f'<p><img src="{_esc(_abs_url(face))}" width="32" height="32"'
        f' alt="{_esc(o.get("name", ""))}"></p>'
    )


def _bvids(o: dict) -> list[str]:
    """条目里出现的所有视频 BV 号（含转发原动态里的）。"""
    out = []
    a = (o.get("archive") or {}).get("bvid")
    if a:
        out.append(a)
    if o.get("orig"):
        out.extend(_bvids(o["orig"]))
    return out


def _bvids_of(item: Item) -> list[str]:
    if item.extra.get("bvids"):
        return list(item.extra["bvids"])
    # 兼容早期状态文件：从正文里的视频链接认（iframe 的 URL 不含 /video/，不会误match）
    return re.findall(r"bilibili\.com/video/(BV[A-Za-z0-9]+)", item.content or "")


# 已经渲染过的头像段 / 播放器 iframe，重复处理时先摘掉再按当前开关重新加
_AVATAR_RE = re.compile(r'<p><img src="https://i\d\.hdslb\.com/bfs/face/[^"]*"[^>]*></p>')
_PLAYER_RE = re.compile(r"<iframe [^>]*(?:player\.bilibili\.com|html5mobileplayer)[^>]*></iframe>")


def _apply_avatar(item: Item, enabled: bool) -> None:
    content = _AVATAR_RE.sub("", item.content or "")
    if enabled and item.extra.get("face"):
        content = _avatar_html({"face": item.extra["face"], "name": item.author}) + content
    item.content = content


def _apply_player(item: Item, style: str) -> None:
    content = _PLAYER_RE.sub("", item.content or "")
    if style:
        for bvid in _bvids_of(item):
            pat = re.compile(
                r'(<p>投稿视频：<a href="https://www\.bilibili\.com/video/'
                + re.escape(bvid) + r'">.*?</a></p>)', re.S
            )
            if pat.search(content):
                content = pat.sub(lambda m: m.group(1) + _player_iframe(bvid, style),
                                  content, count=1)
    item.content = content


def _is_meaningful(o: dict) -> bool:
    if o.get("text"):
        return True
    if any(o.get(k) for k in ("archive", "draw", "article", "opus", "common")):
        return True
    return _is_meaningful(o["orig"]) if o.get("orig") else False


def _is_image_only(o: dict) -> bool:
    """纯图动态：只有图片，没有正文，也没有视频/文章/转发等其他内容。"""
    if o.get("text"):
        return False
    if not o.get("draw"):
        return False
    return not any(o.get(k) for k in ("archive", "article", "opus", "common", "orig"))


def _item_is_image_only(item: Item) -> bool:
    flag = item.extra.get("image_only")
    if flag is not None:
        return bool(flag)
    # 兼容早期状态文件：正文里除了图片段没有别的留下来的东西
    content = item.content or ""
    if "<img " not in content:
        return False
    return not re.sub(r"<p><img [^>]*></p>", "", content).strip()


def _is_self_repost(o: dict) -> bool:
    """判断是不是「转发自 @自己」的回环转发。"""
    orig = o.get("orig")
    if not orig:
        return False
    mid, orig_mid = o.get("mid"), orig.get("mid")
    if mid and orig_mid:
        return str(mid) == str(orig_mid)
    name = o.get("name")
    return bool(name) and name == orig.get("name")


def _item_is_self_repost(item: Item) -> bool:
    flag = item.extra.get("self_repost")
    if flag is not None:
        return bool(flag)
    # 兼容早期状态文件：当时没存这个标记，就从渲染好的正文里认
    m = re.search(r"转发自 @([^<]*)", item.content or "")
    return bool(m) and bool(item.author) and m.group(1).strip() == item.author.strip()


def _content_html(o: dict) -> str:
    parts: list[str] = []
    if o.get("text"):
        parts.append(f"<p>{_text_html(o['text'])}</p>")

    if o.get("archive"):
        a = o["archive"]
        link = VIDEO_URL + a["bvid"] if a.get("bvid") else ""
        title = _esc(a.get("title", ""))
        parts.append(f'<p>投稿视频：<a href="{link}">{title}</a></p>' if link
                     else f"<p>投稿视频：{title}</p>")
        if a.get("desc"):
            parts.append(f"<p>{_text_html(a['desc'])}</p>")
        if a.get("cover"):
            parts.append(f"<p>{_img_tag(a['cover'])}</p>")

    if o.get("draw"):
        parts.extend(f"<p>{_img_tag(src)}</p>" for src in o["draw"])

    if o.get("article"):
        a = o["article"]
        link = _abs_url(a.get("jump", ""))
        title = _esc(a.get("title", ""))
        parts.append(f'<p>文章：<a href="{link}">{title}</a></p>' if link
                     else f"<p>文章：{title}</p>")
        if a.get("desc"):
            parts.append(f"<p>{_text_html(a['desc'])}</p>")
        parts.extend(f"<p>{_img_tag(c)}</p>" for c in a.get("covers") or [])

    if o.get("opus"):
        a = o["opus"]
        if a.get("title"):
            link = _abs_url(a.get("jump", ""))
            title = _esc(a["title"])
            parts.append(f'<p><a href="{link}">{title}</a></p>' if link else f"<p>{title}</p>")
        if a.get("summary"):
            parts.append(f"<p>{_text_html(a['summary'])}</p>")
        parts.extend(f"<p>{_img_tag(p)}</p>" for p in a.get("pics") or [])

    if o.get("common"):
        a = o["common"]
        link = _abs_url(a.get("jump", ""))
        title = _esc(a.get("title", ""))
        if title:
            parts.append(f'<p><a href="{link}">{title}</a></p>' if link else f"<p>{title}</p>")
        if a.get("desc"):
            parts.append(f"<p>{_text_html(a['desc'])}</p>")
        if a.get("cover"):
            parts.append(f"<p>{_img_tag(a['cover'])}</p>")

    if o.get("orig"):
        name = _esc(o["orig"].get("name", ""))
        inner = _content_html(o["orig"])
        parts.append(
            f"<blockquote><p>转发自 @{name}</p>{inner}</blockquote>"
        )
    return "".join(parts)


def _first_line(s: str, limit: int = 80) -> str:
    for line in (s or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _title(o: dict) -> str:
    archive = o.get("archive") or {}
    article = o.get("article") or {}
    opus = o.get("opus") or {}
    common = o.get("common") or {}
    if archive.get("title"):
        # 投稿视频：标题直接是「作者 + 空格 + 视频标题」，不带动作前缀
        return f"{o.get('name', '')} {archive['title']}".strip()
    if article.get("title"):
        return "发布了文章：" + article["title"]
    if opus.get("title"):
        return opus["title"]
    if common.get("title"):
        return common["title"]
    line = _first_line(o.get("text", ""))
    if line:
        return line
    if o.get("draw"):
        return f"发布了 {len(o['draw'])} 张图片"
    if o.get("orig"):
        if _first_line(o["orig"].get("text", "")):
            return "转发：" + _first_line(o["orig"]["text"])
        return "转发自 @" + (o["orig"].get("name") or "未知")
    return "动态"


@register
class BilibiliProvider(Provider):
    type = "bilibili"
    description = "B 站动态（关注动态 / UP 主空间）"

    def page_url(self) -> str:
        if self.opt("mode", "feed") == "space":
            return f"https://space.bilibili.com/{self.require('uid')}/dynamic"
        return "https://t.bilibili.com/"

    def _page_url(self, page: int, offset: str | None) -> str:
        if self.opt("mode", "feed") == "space":
            url = f"{API_SPACE}?host_mid={self.require('uid')}&timezone_offset=-480"
            if offset:
                url += f"&offset={offset}"
            return url
        url = f"{API_FEED}?timezone_offset=-480&type=all&page={page}"
        if offset:
            url += f"&offset={offset}"
        return url

    def fetch(self, bridge: Bridge, known_ids: set[str]) -> list[Item]:
        max_pages = int(self.opt("max_pages", 3))
        items: list[Item] = []
        offset: str | None = None

        for page in range(1, max_pages + 1):
            result = bridge.evaluate(EXTRACT_JS.replace("__URL__", self._page_url(page, offset)))
            if not result or not result.get("ok"):
                detail = (result or {}).get("message") or (result or {}).get("error") or result
                raise RuntimeError(f"B 站接口返回异常（第 {page} 页）: {detail}")

            raw_items = result.get("items") or []
            if not raw_items:
                break

            page_items = [self._to_item(o) for o in raw_items if _is_meaningful(o)]
            items.extend(page_items)

            # 增量：整页都已存在 → 后面不会再有新内容
            if known_ids and page_items and all(i.id in known_ids for i in page_items):
                break
            if not result.get("has_more"):
                break
            offset = result.get("offset")
            if not offset:
                break

        return items

    def _to_item(self, o: dict) -> Item:
        ts = int(o.get("ts") or 0)
        published = datetime.fromtimestamp(ts) if ts else None
        return Item(
            id=f"bilibili:{o.get('id')}",
            title=_title(o),
            link=DYNAMIC_URL + str(o.get("id", "")),
            author=o.get("name", ""),
            published=published,
            content=_content_html(o),
            extra={
                "type": o.get("type", ""),
                "mid": o.get("mid", 0),
                "face": o.get("face", ""),
                "bvids": _bvids(o),
                "self_repost": _is_self_repost(o),
                "image_only": _is_image_only(o),
            },
        )

    def postprocess(self, items: list[Item], bridge: Bridge) -> list[Item]:
        items = super().postprocess(items, bridge)
        if self.opt("filter_self_repost", True):
            items = [i for i in items if not _item_is_self_repost(i)]
        if self.opt("filter_image_only", True):
            items = [i for i in items if not _item_is_image_only(i)]

        # 头像和播放器在这里加/摘，而不是抓取时定死 —— 这样改开关立刻生效，
        # 不用重抓（两个操作都是幂等的：先摘掉旧的，再按当前开关加回去）。
        show_avatar = bool(self.opt("show_avatar", True))
        style = _player_style(self.opt("embed_player", "mobile"))
        for item in items:
            _apply_avatar(item, show_avatar)
            _apply_player(item, style)
        return items
