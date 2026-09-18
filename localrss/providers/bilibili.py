"""B 站动态 provider。

两种模式（options.mode）：
  - feed  : 关注动态（对应 https://t.bilibili.com/ 首页），默认
  - space : 指定 UP 主的空间动态（需要 options.uid）

接口走 api.bilibili.com 的 polymer 动态接口，在浏览器里带 cookie 请求。
"""

from __future__ import annotations

import html
import re
import sys
from datetime import datetime

from ..bridge import Bridge
from ..models import Item, sort_key
from . import register
from .base import Provider

API_FEED = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
API_SPACE = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"

# 图文动态（MAJOR_TYPE_DRAW）现在有两套返回格式：不带这个参数时接口只回 major.draw
# （纯图片、没有正文），带上之后按网页版的新格式回 major.opus —— 正文在
# major.opus.summary.text 里，图片在 major.opus.pics 里。
# 不带参数的后果就是实测里那些「发布了 N 张图片」的条目：动态其实都有正文，全被接口吃掉了。
OPUS_FEATURES = "features=itemOpusStyle"
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
      season: '',  // 合集更新的徽标文字（major.ugc_season 才有）
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
    // 「更新了合集」（MAJOR_TYPE_UGC_SEASON）：结构就是一个刚更新的视频 + 合集徽标，
    // 所以并进 archive 走同一套渲染（标题取视频标题、正文带播放器），
    // 只额外记下徽标文字，正文里那行写成「合集更新：」。
    if (major.ugc_season) {
      const s = major.ugc_season;
      o.season = (s.badge && s.badge.text) || '合集';
      o.archive = {
        title: s.title || '',
        bvid: s.bvid || '',
        cover: s.cover || '',
        desc: s.desc || ''
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


# ---------- 全文 ----------
#
# 动态接口给的正文是**截断**的：opus 内容超过约 300 字就只回开头一段，末尾补一个
# "..."。全文在 opus 页里，而那个页面是服务端渲染的 —— 实测直接 fetch 就能拿到
# 54 KB HTML，`.opus-module-content` 里就是完整正文。所以不用像知乎那样开页面等渲染，
# 一个 fetch + DOMParser 就够（跨域也没问题，在 t.bilibili.com 上实测读得到）。

OPUS_URL = "https://www.bilibili.com/opus/"

#: 正文里嵌的 opus 链接：顶层就是它自己的，转发里嵌的是原动态的
_OPUS_HREF_RE = re.compile(r'href="[^"]*?/opus/(\d+)"')
#: 一个段落。摘要里的换行在接口那层已经变成 <br> 了，p 里没有嵌套的 p。
_PARA_RE = re.compile(r"<p>([\s\S]*?)</p>")
#: 被平台截断的段落：以 "..." 结尾（前面可能跟着一个 <br>）
_TRUNCATED_RE = re.compile(r"(?:<br\s*/?>)?\.\.\.\s*$")
#: 「这条动态自己就是一篇 opus」的类型。AV / 合集更新的正文是视频简介，
#: 接口给的就是全文，不需要补。
OPUS_TYPES = {"DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_ARTICLE"}

FULLTEXT_JS = r"""
(async () => {
  try {
    const r = await fetch('__URL__', { credentials: 'include' });
    if (!r.ok) return { ok: false, status: r.status, error: 'HTTP ' + r.status };
    const html = await r.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const el = doc.querySelector('.opus-module-content');
    if (!el || !el.innerHTML.trim()) {
      return { ok: false, status: r.status, error: '页面上没有 .opus-module-content' };
    }
    return { ok: true, status: r.status, html: el.innerHTML };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
})()
"""


def _text_of_html(s: str) -> str:
    """HTML 的可见文本：去标签、还原实体，**去掉所有空白**。

    去空白是为了能拿接口给的摘要去页面正文里对位置 —— 两边换行和缩进都不一样，
    只有字能对上。
    """
    return re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", s or "")))


def _drop_empty_styles(s: str) -> str:
    """去掉 `style="background:;color:;"` 这种空壳样式（opus 正文里到处都是）。"""
    def repl(m: re.Match) -> str:
        decls = [d for d in m.group(1).split(";") if d.strip()]
        if decls and all(d.split(":", 1)[-1].strip() == "" for d in decls):
            return ""
        return m.group(0)

    return re.sub(r'\s*style="([^"]*)"', repl, s)


def _clean_opus_html(s: str) -> str:
    """把 opus 页里取来的正文 HTML 收拾成能塞进 RSS 的样子。"""
    s = re.sub(r"<(script|style)\b.*?</\1>", "", s, flags=re.S | re.I)
    s = _drop_empty_styles(s)
    # 页面里的链接/图片地址有相对写法（`/opus/xxx`、`//i0.hdslb.com/...`），
    # 阅读器多半在别的域下打开，不补全就点不动、显示不出来。
    return re.sub(r'(src|href)="([^"]*)"',
                  lambda m: f'{m.group(1)}="{_abs_url(m.group(2))}"', s).strip()


def _truncated_para(item: Item) -> tuple[re.Match, str] | None:
    """找出正文里第一个被平台截断的段落，以及该去哪儿取它的全文。

    URL 的取法：段落**前面最近**的那个 opus 链接 —— 顶层是这条动态自己的，转发里
    嵌的那个是原动态的；没有链接时，如果这条动态自己就是一篇 opus（图文 / 文章），
    用 `opus/<动态号>`（实测 opus 号就是动态号）。

    返回 None = 没有可补的：要么没被截断，要么那个 "..." 是作者自己写的
    （视频简介里很常见），要么这条动态的正文根本不在 opus 页上。
    """
    content = item.content or ""
    link = ""
    for m in _PARA_RE.finditer(content):
        hrefs = _OPUS_HREF_RE.findall(m.group(1))
        if hrefs:
            link = OPUS_URL + hrefs[0]
        if not _TRUNCATED_RE.search(m.group(1)):
            continue
        if link:
            return m, link
        if item.extra.get("type") in OPUS_TYPES:
            return m, OPUS_URL + item.id.split(":", 1)[-1]
        return None
    return None


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
# 四种播放器都要能认出来：player.bilibili.com（desktop）+ blackboard/ 下的
# html5player / html5mobileplayer / player。之前只写了 player.bilibili.com 和
# html5mobileplayer，结果是配 html5player 时摘不掉旧的，每跑一次就多插一个 iframe。
_PLAYER_RE = re.compile(
    r'<iframe[^>]*\bsrc="[^"]*(?:player\.bilibili\.com|bilibili\.com/blackboard/)[^"]*"'
    r"[^>]*></iframe>"
)


def _apply_avatar(item: Item, enabled: bool) -> None:
    content = _AVATAR_RE.sub("", item.content or "")
    if enabled and item.extra.get("face"):
        content = _avatar_html({"face": item.extra["face"], "name": item.author}) + content
    item.content = content


def _apply_player(item: Item, style: str) -> None:
    content = _PLAYER_RE.sub("", item.content or "")
    if style:
        for bvid in _bvids_of(item):
            # 正文里那行说明：普通投稿是「投稿视频：」，合集更新是「合集更新：」
            pat = re.compile(
                r'(<p>(?:投稿视频|合集更新)：<a href="https://www\.bilibili\.com/video/'
                + re.escape(bvid) + r'">.*?</a></p>)', re.S
            )
            if pat.search(content):
                content = pat.sub(lambda m: m.group(1) + _player_iframe(bvid, style),
                                  content, count=1)
    item.content = content


def _dynamic_text(o: dict) -> str:
    """动态的正文。两套格式：老的在 module_dynamic.desc.text，新的（opus）在 major.opus.summary.text。"""
    return o.get("text") or (o.get("opus") or {}).get("summary", "") or ""


def _pic_urls(o: dict) -> list[str]:
    """动态里的图片。老的（major.draw）在 draw 里，新的（major.opus）在 opus.pics 里。"""
    if o.get("draw"):
        return list(o["draw"])
    return list((o.get("opus") or {}).get("pics") or [])


def _is_meaningful(o: dict) -> bool:
    if _dynamic_text(o):
        return True
    if any(o.get(k) for k in ("archive", "draw", "article", "opus", "common")):
        return True
    return _is_meaningful(o["orig"]) if o.get("orig") else False


def _is_image_only(o: dict) -> bool:
    """纯图动态：只有图片，没有正文，也没有视频/文章/转发等其他内容。

    注意 opus 格式下「有正文」是看 opus.summary，不是 desc —— 只认 desc 的话，
    带正文的图文动态会被误判成纯图。
    """
    if _dynamic_text(o):
        return False
    if not _pic_urls(o):
        return False
    if (o.get("opus") or {}).get("title"):
        return False  # 有标题的图文作品，不是随手发的图
    return not any(o.get(k) for k in ("archive", "article", "common", "orig"))


def _item_is_image_only(item: Item) -> bool:
    flag = item.extra.get("image_only")
    if flag is not None:
        return bool(flag)
    # 兼容早期状态文件：正文里除了图片段没有别的留下来的东西
    content = item.content or ""
    if "<img " not in content:
        return False
    return not re.sub(r"<p><img [^>]*></p>", "", content).strip()


def _item_is_digestable(item: Item) -> bool:
    """是不是要收进合集的动态：发图（`DYNAMIC_TYPE_DRAW`）+ 转发（`DYNAMIC_TYPE_FORWARD`）。

    判定用接口给的动态类型，所以 state 里存下来的老条目也认得出 —— 不看
    `image_only` 标记（那个只看「有没有正文」，带正文的图文会被漏掉；
    `--force` 重抓把它刷新一遍也不影响这里）。
    """
    kind = item.extra.get("type")
    if kind:
        return kind in DIGEST_DYNAMIC_TYPES
    # 早期状态文件可能没存 type，那就只认「纯图」这种最保守的情况
    return _item_is_image_only(item)


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


# ---------- 动态合集（image_digest_size）----------
#
# 「图片动态」和「转发」单发一条 RSS 提示太吵 —— 实测关注流两天 118 条里，
# DYNAMIC_TYPE_DRAW 41 条（每天约 19）、DYNAMIC_TYPE_FORWARD 14 条。攒够 N 条
# 合成一条「合集」，内容一条不落（作者 / 时间 / 正文 / 图片 / 原动态链接都在，
# 转发里的视频播放器也照样嵌），只是不再单条刷屏。
#
# 合集本身是个普通条目（id 稳定、内容生成时定死），会跟别的条目一样存进 state，
# 并且把「合成过哪几条」记在自己的 extra.digest_ids 里 —— 不需要额外的队列文件，
# 反复运行 / --force 重抓都不会重复合成。

#: 合集条目的 extra.kind，用来把它和真实动态区分开
IMAGE_DIGEST_KIND = "image_digest"

#: 会被收进合集的动态类型
DRAW_DYNAMIC_TYPE = "DYNAMIC_TYPE_DRAW"
FORWARD_DYNAMIC_TYPE = "DYNAMIC_TYPE_FORWARD"
DIGEST_DYNAMIC_TYPES = (DRAW_DYNAMIC_TYPE, FORWARD_DYNAMIC_TYPE)


def _age_seconds(dt: datetime | None) -> float:
    """距现在多少秒。published 是本地 naive 时间，别用 sort_key（它按 UTC 解释）。"""
    if dt is None:
        return 0.0
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return (now - dt).total_seconds()


def _digested_ids(items: list[Item]) -> set[str]:
    """已经并进合集的条目 id（从合集条目自己的 extra.digest_ids 推出来）。"""
    out: set[str] = set()
    for i in items:
        if i.extra.get("kind") == IMAGE_DIGEST_KIND:
            out.update(i.extra.get("digest_ids") or [])
    return out


def _digest_title(batch: list[Item]) -> str:
    """合集标题：`动态合集 <作者…>`（同一批发过言的作者全列出来，去重、按时间顺序）。"""
    names: list[str] = []
    for i in batch:
        if i.author and i.author not in names:
            names.append(i.author)
    return f"动态合集 {'、'.join(names)}" if names else "动态合集"


def _digest_entry_html(item: Item, show_avatar: bool) -> str:
    head: list[str] = []
    face = item.extra.get("face")
    if show_avatar and face:
        head.append(
            f'<img src="{_esc(_abs_url(face))}" width="32" height="32"'
            f' alt="{_esc(item.author)}">'
        )
    who = _esc(item.author)
    head.append(f'<a href="{_esc(item.link)}">{who}</a>' if item.link else who)
    if item.published:
        head.append(item.published.strftime("%Y-%m-%d %H:%M"))
    # content 就是这条动态渲染好的正文（文字 + 图片）；如果之前渲染过头像，先摘掉，
    # 合集里由头行统一表示。
    body = _AVATAR_RE.sub("", item.content or "")
    return "<p>" + " · ".join(h for h in head if h) + "</p>" + body


def _build_digest(batch: list[Item], show_avatar: bool) -> Item:
    """把一批图片动态（按时间从旧到新）合成一个条目。"""
    newest = batch[-1]
    local = batch[0].id.split(":", 1)[-1]  # bilibili:<动态id> -> <动态id>
    return Item(
        id=f"bilibili:imgdigest:{local}",
        title=_digest_title(batch),
        link=newest.link or DYNAMIC_URL,
        published=newest.published,
        content="".join(_digest_entry_html(i, show_avatar) for i in batch),
        extra={
            "kind": IMAGE_DIGEST_KIND,
            "digest_ids": [i.id for i in batch],
        },
    )


def _content_html(o: dict) -> str:
    parts: list[str] = []
    if o.get("text"):
        parts.append(f"<p>{_text_html(o['text'])}</p>")

    if o.get("archive"):
        a = o["archive"]
        link = VIDEO_URL + a["bvid"] if a.get("bvid") else ""
        title = _esc(a.get("title", ""))
        # 合集更新和普通投稿一样是个视频卡片，只是正文里那行说明不同
        label = "合集更新" if o.get("season") else "投稿视频"
        parts.append(f'<p>{label}：<a href="{link}">{title}</a></p>' if link
                     else f"<p>{label}：{title}</p>")
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
    line = _first_line(_dynamic_text(o))
    if line:
        return line
    pics = _pic_urls(o)
    if pics:
        return f"发布了 {len(pics)} 张图片"
    if o.get("orig"):
        orig_line = _first_line(_dynamic_text(o["orig"]))
        if orig_line:
            return "转发：" + orig_line
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
            url = f"{API_SPACE}?host_mid={self.require('uid')}&timezone_offset=-480&{OPUS_FEATURES}"
            if offset:
                url += f"&offset={offset}"
            return url
        url = f"{API_FEED}?timezone_offset=-480&type=all&page={page}&{OPUS_FEATURES}"
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

    def _digest_max_age_s(self) -> float:
        """图片动态攒不满 size 时的兜底有效期（小时），0 = 不兜底（一直攒着）。"""
        try:
            hours = float(self.opt("image_digest_max_age_hours", 24))
        except (TypeError, ValueError):
            hours = 24.0
        return max(0.0, hours) * 3600

    def _apply_image_digest(
        self, items: list[Item], size: int, max_age_s: float, show_avatar: bool
    ) -> tuple[list[Item], list[Item], int, list[Item]]:
        """把图片动态/转发攒成合集。

        返回（可见列表，本次新建的合集条目，收进合集的条数，还没发出去的存货）。

        - 这些条目自己不进 RSS（没并进合集的在里面等着，并过的也不单独出现）；
        - 攒够 size 条就出一批（先出旧的）；不足 size 但最旧那条超过 max_age_s 也出一批；
        - 第四个返回值是发完这批之后还在攒的那些，调用方用来报告队列状态。
        """
        digested = _digested_ids(items)
        image_items = [i for i in items if _item_is_digestable(i)]
        visible = [i for i in items if not _item_is_digestable(i)]
        pending = sorted((i for i in image_items if i.id not in digested), key=sort_key)

        new: list[Item] = []
        while len(pending) >= size:
            batch, pending = pending[:size], pending[size:]
            new.append(_build_digest(batch, show_avatar))
        if pending and max_age_s > 0 and _age_seconds(pending[0].published) >= max_age_s:
            new.append(_build_digest(pending, show_avatar))
            pending = []  # 这批发完了，不再是「在攒」
        return visible + new, new, len(image_items), pending

    # ---------- 全文 ----------

    def _enrich_fulltext(self, items: list[Item], bridge: Bridge) -> list[Item]:
        """把被平台截断的正文补成全文。

        只换掉那一**段**被截断的段落：标题链接、图片、转发包装都原样留着。图片继续
        用接口给的那几张 —— opus 页的 HTML 里反而不带图（实测那篇 2774 字的文章，
        正文里一张 img 都没有，图片是客户端再插进来的），所以别拿页面正文去覆盖图片。

        每条只补一次（extra.fulltext），补完顺手打上 content_locked：否则下一轮
        Store.merge 会用重新渲染出来的摘要把正文盖回去。
        """
        budget = int(self.opt("max_fulltext_per_run", 10))
        todo: list[tuple[Item, re.Match, str]] = []
        for item in items:
            if item.extra.get("fulltext") or item.extra.get("kind"):
                # kind = 图片合集：那是好多条动态拼出来的，正文里可能有好几段 opus
                continue
            found = _truncated_para(item)
            if found:
                todo.append((item, found[0], found[1]))
        if budget <= 0 or not todo:
            return items

        total = min(len(todo), budget)
        print(f"    [全文] 待补 {len(todo)} 篇，本次最多处理 {budget} 篇")
        for n, (item, para, url) in enumerate(todo[:budget], 1):
            result = bridge.evaluate(FULLTEXT_JS.replace("__URL__", url)) or {}
            if not result.get("ok"):
                detail = result.get("error") or result
                print(f"    [全文] 失败 {url}: {detail}", file=sys.stderr)
                if not result.get("status"):
                    continue  # 连响应都没拿到（网络/风控），留着下轮重试
                item.extra["fulltext"] = True  # 页面在，但没内容 —— 别再每轮重试
                got = f"取不到（{detail}）"
            else:
                body = _clean_opus_html(result.get("html") or "")
                # 摘要里用 `[图片]` 占位（那个位置在正文里是图），页面正文里没有这个
                # 标记、图片本身也不在 SSR 的 HTML 里，所以比对前先把它去掉。
                head = _text_of_html(para.group(1)).replace("[图片]", "")
                head = head[:-3] if head.endswith("...") else head
                if body and head and _text_of_html(body).startswith(head[:80]):
                    # 整段替换（body 本身就是一串 <p>，不能再套一层）
                    item.content = (item.content[:para.start()] + body
                                    + item.content[para.end():])
                    item.extra["fulltext"] = True
                    item.extra["content_locked"] = True
                    got = f"{len(body)} 字符"
                else:
                    # 不标记，下一轮还会再试：万一是选择器或比对规则的问题，改好之后
                    # 这些条目自己就补上了（标记了就永远补不上）。
                    got = "页面正文对不上，原样保留（下轮重试）"
            print(f"    [全文] {n}/{total} {item.id} ({got})")
        return items

    def postprocess(self, items: list[Item], bridge: Bridge) -> list[Item]:
        # items 是 cli 之后要落盘的那个列表（store.save(merged)），
        # 新建的合集必须留在里面，否则下次运行看不到 digest_ids，同一批图片会被重复合成。
        merged = items
        items = super().postprocess(items, bridge)
        if self.opt("filter_self_repost", True):
            items = self.drop_unless(
                items, lambda i: not _item_is_self_repost(i), "转发自本人（filter_self_repost）"
            )

        # 补全文要放在合集之前：合集是从成员条目拼出来的，先补好，拼进去的才是全文。
        if self.opt("fulltext", True):
            items = self._enrich_fulltext(items, bridge)

        show_avatar = bool(self.opt("show_avatar", True))
        try:
            digest_size = int(self.opt("image_digest_size", 0) or 0)
        except (TypeError, ValueError):
            digest_size = 0

        new_digests: list[Item] = []
        pending: list[Item] = []
        max_age_s = self._digest_max_age_s()
        if digest_size > 0:
            # 开了合集：图片动态和转发一律进合集，filter_image_only 不再参与
            items, new_digests, self.grouped_items, pending = self._apply_image_digest(
                items, digest_size, max_age_s, show_avatar
            )
        elif self.opt("filter_image_only", True):
            items = self.drop_unless(
                items, lambda i: not _item_is_image_only(i), "纯图动态（filter_image_only）"
            )

        if new_digests:
            merged.extend(new_digests)
            merged.sort(key=sort_key, reverse=True)
            self.derived_items += len(new_digests)
            for d in new_digests:
                print(f"    [动态合集] 合成 {len(d.extra['digest_ids'])} 条：{d.title}")

        if digest_size > 0:
            # 每次运行都报一下队列：合集是「攒够才发」，不报的话看着就像一直不更新。
            note = f"在攒 {len(pending)}/{digest_size} 条"
            if pending:
                note += f"（还差 {digest_size - len(pending)} 条）"
                hours = _age_seconds(pending[0].published) / 3600
                note += f"；最旧一条 {hours:.1f}h 前"
                note += (f"，满 {max_age_s / 3600:g}h 也会照样发"
                         if max_age_s > 0 else "，不兜底（攒满才发）")
            else:
                note += "，攒满就发"
            print(f"    [动态合集] {note}")

        # 头像和播放器在这里加/摘，而不是抓取时定死 —— 这样改开关立刻生效，
        # 不用重抓（两个操作都是幂等的：先摘掉旧的，再按当前开关加回去）。
        # 合集条目也照走一遍：它正文里那些「作者 · 时间」头行不会被 _AVATAR_RE 匹配
        # （那个正则要求 <p> 里只有头像），播放器则会按正文里的视频链接补进转发条目。
        style = _player_style(self.opt("embed_player", "mobile"))
        for item in items:
            _apply_avatar(item, show_avatar)
            _apply_player(item, style)
        return sorted(items, key=sort_key, reverse=True)
