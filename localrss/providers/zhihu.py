"""知乎 provider，两种 mode：

- `activities`（默认）：https://www.zhihu.com/people/<token> 的「动态」页，
  直接调用 api/v3/moments/<token>/activities，在浏览器里带 cookie 请求；
- `notifications`：https://www.zhihu.com/notifications 通知中心，
  调用 api/v4/notifications/v2/recent（见下）。
"""

from __future__ import annotations

import hashlib
import html
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from ..bridge import Bridge, same_page
from ..models import Item, sort_key
from . import register
from .base import Provider, retry_call

API = "https://www.zhihu.com/api/v3/moments/{token}/activities"
PROFILE = "https://www.zhihu.com/people/{token}"

# ---------- 通知中心 ----------
#
# https://www.zhihu.com/notifications 就是「通知中心」：谁回复/评论了你、
# 谁赞同喜欢了你、谁关注了你、以及各种邀请和站务通知。页面自己也调这个接口：
#
#   api/v4/notifications/v2/recent?limit=20&offset=0&entry_name=all
#
# offset 是个**时间戳**（上一页最后一条的 create_time），不是页码——
# 接口在 paging.next 里回下一个地址，照它翻就行。注意它给的是 http:// 的地址，
# 在 https 页面里 fetch 会被当混合内容拦掉，所以下面统一改回 https。
MODE_ACTIVITIES = "activities"
MODE_NOTIFICATIONS = "notifications"
MODES = (MODE_ACTIVITIES, MODE_NOTIFICATIONS)

NOTIFICATIONS_PAGE = "https://www.zhihu.com/notifications"
NOTIFICATIONS_API = "https://www.zhihu.com/api/v4/notifications/v2/recent"

#: 通知分类（接口的 entry_name）。名字和页面 JS 里的映射表一一对应，
#: 页面上那几个 tab（全部通知 / 关注我的 / 赞同与喜欢 / …）就是它。
#: **写错了接口不会报错，只会静默返回空列表**，所以这里先校验一遍。
NOTIFICATION_ENTRIES = {
    "all": "全部通知",
    "follow": "关注我的",
    "like": "赞同与喜欢",
    "comment": "评论与回复",
    "mention": "提到我的",
    "invite": "邀请",
    "community": "站务通知",
    "system": "系统通知",
    "follow_question_add_answer": "关注的问题",
}

VERB_ACTIONS = {
    "MEMBER_ANSWER_QUESTION": "回答了问题",
    "MEMBER_CREATE_PIN": "发布了想法",
    "MEMBER_CREATE_ARTICLE": "发布了文章",
    "MEMBER_COLLECT_ANSWER": "收藏了回答",
    "MEMBER_COLLECT_ARTICLE": "收藏了文章",
    "MEMBER_VOTEUP_ANSWER": "赞同了回答",
    "MEMBER_VOTEUP_ARTICLE": "赞同了文章",
    "MEMBER_FAVORITE_ANSWER": "喜欢了回答",
    "MEMBER_FOLLOW_QUESTION": "关注了问题",
}

# 列表接口都是在「当前标签页」里 fetch 的，带的是这个页面的 cookie。请求没带上
# 登录 cookie 时，知乎不回 401，只回 `403 {"code":10003}` +「请求参数异常，请升级
# 客户端后重试。」—— 跟真正的风控响应是同一句，光看报错分不出是谁的问题。实测
# 在 `credentials:'omit'` 和「在 t.bilibili.com 页面上 fetch」两种情况下，响应体
# 一模一样，所以这里先自检，把「cookie 根本没带上」和「知乎真的拦了」分开：
#
#   not_on_page  页面不在 www.zhihu.com 上（上一轮留下的 B 站标签页、刚开出来还没
#                commit 的新标签页）。知乎的登录 cookie 是 SameSite=Lax，跨站发的
#                请求一个都不带 —— 切回目标页面立刻重来就好（见 _fetch_page）。
#   no_cookie    页面是对的，但浏览器这会儿没把登录 cookie 交给页面：冷启动、机器
#                刚唤醒、登录态过期都会这样，通常一两分钟自己就好了（实测线上的
#                失败窗口 80~125 秒）。这种等着才有用，切页面、换接口都没用。
PAGE_CHECK_JS = r"""
    if (location.hostname !== 'www.zhihu.com') {
      return { ok: false, not_on_page: true,
               error: '当前标签页不在知乎页面上（' + location.href + '）' };
    }
    if (document.cookie.indexOf('SESSIONID=') < 0) {
      return { ok: false, no_cookie: true,
               error: '浏览器里没有知乎的登录 cookie（' + location.href + '）' };
    }
"""

EXTRACT_JS = r"""
(async () => {
  try {
__PAGE_CHECK__
    const r = await fetch('__URL__', { credentials: 'include' });
    const j = await r.json();
    if (!j.data) {
      const msg = (j.error && j.error.message) || ('HTTP ' + r.status);
      return { ok: false, error: msg };
    }
    const p = j.paging || {};
    return { ok: true, is_end: !!p.is_end, next: p.next || null, items: j.data.map(pack) };
  } catch (e) {
    return { ok: false, error: String(e) };
  }

  function clean(s) {
    return String(s || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  }

  function pack(a) {
    const t = a.target || {};
    const q = t.question || {};
    let preview = t.excerpt || '';
    if (!preview) {
      const c = t.content;
      if (typeof c === 'string') {
        preview = clean(c);
      } else if (Array.isArray(c)) {
        preview = c.map(function (b) {
          if (!b || typeof b !== 'object') { return ''; }
          if (b.type === 'image') { return '[图片]'; }
          return clean(b.content || '');
        }).join(' ').trim();
      }
    }
    return {
      id: a.id || '',
      verb: a.verb || '',
      action: a.action_text || '',
      created: a.created_time || t.created_time || 0,
      // 内容最后一次被编辑的时间。回答/文章是 updated_time，想法（pin）是
      // updated（没有 _time 后缀）；问题类动态的 target 不带这个字段，取到 0。
      updated: t.updated_time || t.updated || 0,
      target_type: t.type || '',
      target_id: t.id || '',
      title: t.title || q.title || '',
      preview: preview.slice(0, 800),
      link: t.url || '',
      question_id: q.id || '',
      author: (t.author && t.author.name) || (a.actor && a.actor.name) || ''
    };
  }
})()
"""

# 通知中心的列表接口。返回的字段和动态那边完全不同，所以在浏览器里就压平成
# 一条只带展示所需字段的扁平结构（原始数据一条好几 KB，整个搬回来太重）。
NOTIFICATIONS_EXTRACT_JS = r"""
(async () => {
  try {
__PAGE_CHECK__
    const r = await fetch('__URL__', { credentials: 'include' });
    const j = await r.json();
    if (!j.data) {
      const msg = (j.error && j.error.message) || ('HTTP ' + r.status);
      return { ok: false, error: msg };
    }
    const p = j.paging || {};
    return { ok: true, is_end: !!p.is_end, next: p.next || null, items: j.data.map(pack) };
  } catch (e) {
    return { ok: false, error: String(e) };
  }

  // actors 可能是单个对象（一位发起人）也可能是数组（合并通知：多人点赞）。
  // name 带「（作者）」后缀，是平台自己标的，保留。
  function actorName(a) {
    if (!a) return '';
    if (Array.isArray(a)) {
      return a.map(function (x) { return (x && x.name) || ''; }).filter(Boolean).join('、');
    }
    return a.name || a.url_token || '';
  }

  function pack(n) {
    const c = n.content || {};
    const t = c.target || {};
    const ext = c.extend || {};
    return {
      id: n.id || '',
      verb: c.verb || '',
      created: n.create_time || 0,
      merge_count: n.merge_count || 0,
      actor: actorName(c.actors),
      // 通知指向的内容（问题标题、回答标题…）。内容被删掉时是「该内容被删除」，
      // link 为空 —— 那种退回通知中心页面（见 _notification_link）。
      target_text: t.text || '',
      target_link: t.link || '',
      // 回复/评论的正文；「喜欢」「关注」这类没有正文，是空串
      text: typeof ext.text === 'string' ? ext.text : ''
    };
  }
})()
"""


def _short_hash(s: str, length: int = 6) -> str:
    return hashlib.md5(str(s).encode("utf-8")).hexdigest()[:length]


# ---------- 全文抓取 ----------
#
# 逻辑照搬 kvxjr369f/zhihu_fulltext.py：导航到内容页、等渲染、从 DOM 里取正文。
# 回答/文章页面的 API 拿不到内容（article 直接 403「请求参数异常」），
# 想法页又会被随机重定向，所以页面抓取是最省事的办法。

# 每种类型的标题 / 正文选择器，按顺序 fallback
FULLTEXT_SELECTORS: dict[str, tuple[list[str], list[str]]] = {
    "answer": (
        [".QuestionHeader-title", "h1.QuestionHeader-title"],
        [
            ".AnswerCard .RichText",
            ".RichContent .RichText",
            ".AnswerItem .RichText",
            ".ContentItem .RichText",
            '[data-za-module="AnswerContent"] .RichText',
        ],
    ),
    "article": (
        [".Post-Title"],
        [".Post-RichTextContainer .RichText", ".Post-RichText"],
    ),
    "pin": (
        ["title"],
        [
            ".RichContent .RichText",
            ".RichContent",
            ".PinItem .RichText",
            ".Pin-content .RichText",
            ".PinsDetailPage .RichText",
            ".PinsDetailContent .RichText",
            ".ContentItem .RichText",
        ],
    ),
    # 「添加了问题」这类动态指向的是问题页：正文就是问题描述（补充说明）。
    # 它在 .QuestionRichText 里，折叠时只给半截 + 「显示全部」按钮（展开由
    # _extract_js 负责）。问题可以没有描述，那时页面上没这个节点，见
    # OPTIONAL_CONTENT_TYPES。
    "question": (
        ["h1.QuestionHeader-title"],
        [
            ".QuestionRichText span[itemprop='text']",
            ".QuestionRichText .RichText",
            ".QuestionRichText",
        ],
    ),
}

#: 这些类型「页面上没有正文」是正常情况（问题可以没有描述），不算抓取失败。
OPTIONAL_CONTENT_TYPES = {"question"}

ITEM_DELAY_MS = 3000     # 两次抓取之间的间隔（再叠随机抖动）
MIN_CONTENT_LEN = 50

#: 导航（navigate）之后最多等多久让新页面真的落地，见 ZhihuProvider._settle。
#: navigate 是异步的，返回时新文档未必 commit；这段时间内还没落地就重新导航一次。
PAGE_SETTLE_S = 4.0

#: 发现「页面在知乎上、但浏览器没把登录 cookie 交出来」时等多久、隔多久再看一次。
#: 这是浏览器侧的临时状态（冷启动 / 刚唤醒最典型），实测线上失败窗口 80~125 秒，
#: 所以给 3 分钟耐心；到时候还没有就是要重新登录了，照常报错。同一轮里这份耐心是
#: 共享的（记在 bridge 上），不会每个源都从头等一遍。
COOKIE_WAIT_S = 180.0
COOKIE_RETRY_S = 20.0

# 正文的等待策略。长回答是**分阶段**渲染的：页面先给一小段（约 2 KB HTML，
# 几百字），随后整篇才替换进来 —— 固定等 3 秒会稳定地抓到那个半截版本，
# 并且因为 extra.fulltext 只抓一次，半截版会被永久锁进 state（实测踩过）。
# 所以在页面里轮询，等内容长度稳定下来再取；顺手点掉「阅读全文」。
CONTENT_POLL_MS = 400        # 轮询间隔
CONTENT_MIN_WAIT_MS = 3000   # 至少等这么久（内容一直稳定也不会更早返回）
CONTENT_STABLE_MS = 1500     # 长度连续这么久没变才算渲染完
CONTENT_MAX_WAIT_MS = 20000  # 上限，别把一次抓取拖太久


def _extract_js(title_selectors: list[str], content_selectors: list[str]) -> str:
    # json.dumps 出来的就是合法 JS 字面量（双引号）
    titles = json.dumps(list(title_selectors))
    contents = json.dumps(list(content_selectors))
    return f"""
    (async () => {{
        const titleSelectors = {titles};
        const contentSelectors = {contents};
        const minLen = {MIN_CONTENT_LEN};
        const pollMs = {CONTENT_POLL_MS};
        const minWaitMs = {CONTENT_MIN_WAIT_MS};
        const stableMs = {CONTENT_STABLE_MS};
        const maxWaitMs = {CONTENT_MAX_WAIT_MS};
        const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

        function pick() {{
            let title = document.title;
            for (const sel of titleSelectors) {{
                const el = document.querySelector(sel);
                if (el && el.innerText.trim()) {{ title = el.innerText.trim(); break; }}
            }}

            let node = null;
            let contentHtml = '';
            for (const sel of contentSelectors) {{
                const el = document.querySelector(sel);
                if (el && el.innerHTML.length > minLen) {{
                    node = el;
                    contentHtml = el.innerHTML;
                    break;
                }}
            }}
            return {{ title, content: contentHtml, hasContent: !!node, node }};
        }}

        // 折叠的正文（只剩前半段 + 按钮）：点一下让剩下的渲染出来。
        // 回答/文章是「阅读全文」，问题描述是「显示全部」。
        // 只在按钮文字匹配时点，已经展开时按钮就没了，不会误触。
        function expandCollapsed(node) {{
            if (!node) return false;
            const scope = node.closest(
                '.QuestionRichText, .AnswerCard, .AnswerItem, .ContentItem') || document;
            const btns = scope.querySelectorAll(
                '.ContentItem-expandButton, .ContentItem-more button, .QuestionRichText-more');
            for (const btn of btns) {{
                const text = (btn.innerText || '').trim();
                if (text.indexOf('阅读全文') === -1 && text.indexOf('展开') === -1
                    && text.indexOf('显示全部') === -1) continue;
                btn.click();
                return true;
            }}
            return false;
        }}

        const started = Date.now();
        let best = pick();
        let expanded = false;
        let lastLen = -1;
        let stableSince = started;

        while (true) {{
            const cur = pick();
            // 留最长的那次结果：万一展开反而让 DOM 变短，也不会把正文弄丢
            if (cur.content.length > best.content.length) best = cur;

            if (cur.content.length !== lastLen) {{
                lastLen = cur.content.length;
                stableSince = Date.now();
            }}

            if (!expanded && expandCollapsed(cur.node)) {{
                expanded = true;
                lastLen = -1;
                await sleep(pollMs);
                continue;
            }}

            const elapsed = Date.now() - started;
            if (elapsed >= maxWaitMs) break;
            // 一直取不到正文（被风控/页面异常）不用耗到上限；取到了则要等它稳定
            if (elapsed >= minWaitMs
                && (!best.hasContent || Date.now() - stableSince >= stableMs)) break;
            await sleep(pollMs);
        }}

        // collapsed 是给调用方的一道保险：万一「显示全部」没点上，取到的就是
        // 半截正文（还带个「…」），那种宁可报失败重来，也别锁进 state。
        const last = pick();
        return {{
            title: best.title,
            content: best.content,
            hasContent: best.hasContent,
            collapsed: !!(last.node && last.node.closest('.QuestionRichText--collapsed')),
        }};
    }})()
    """


#: 「浏览器暂时交不出知乎登录 cookie」这份耐心记在 bridge 上，同一次运行里的所有
#: 源共用：一个源等到了，后面的源就不用从头再等一遍；真等不到时也不会每个源都
#: 各花 3 分钟 —— 第一个等完还没好，后面的立刻按失败处理。
_COOKIE_DEADLINE_ATTR = "zhihu_cookie_deadline"


def _cookie_deadline(bridge: Bridge) -> float:
    """本轮「再等一会儿」的截止时刻（monotonic）。

    还没等过就开一个窗口；已经等过的（哪怕是已经超时的那次）原样返回，
    这样同一次运行里只花一份时间，不会每个源都从头再等一遍。
    """
    deadline = getattr(bridge, _COOKIE_DEADLINE_ATTR, None)
    if deadline is None:
        deadline = time.monotonic() + COOKIE_WAIT_S
        setattr(bridge, _COOKIE_DEADLINE_ATTR, deadline)
    return float(deadline)


def _clear_cookie_deadline(bridge: Bridge) -> None:
    """拿到数据了：这回登录态是好的，下次真出问题就重新给一份耐心。"""
    setattr(bridge, _COOKIE_DEADLINE_ATTR, None)


#: 「知乎源共用同一页」这件事每轮只说一遍，别每个源都打一行。
_REUSE_NOTED_ATTR = "zhihu_page_reuse_noted"


def _note_page_reuse(bridge: Bridge, page: str) -> None:
    if getattr(bridge, _REUSE_NOTED_ATTR, False):
        return
    setattr(bridge, _REUSE_NOTED_ATTR, True)
    print(f"    [页面] 知乎各源共用 {page}（列表接口按 token 取数，不必每个源切页）",
          file=sys.stderr)


def _id_from_link(link: str) -> str:
    """从条目链接里取内容 id。

    顺序要紧：回答的链接长的就是 `question/<qid>/answer/<aid>`，得先匹配 answer，
    否则会取成问题 id。`/questions?/` 兼顾 `api.zhihu.com/questions/<id>` 这种接口地址。
    """
    for pattern in (
        r"/answer/(\d+)",
        r"zhuanlan\.zhihu\.com/p/(\d+)",
        r"/pin/(\d+)",
        r"/questions?/(\d+)",
    ):
        m = re.search(pattern, link or "")
        if m:
            return m.group(1)
    return ""


def _web_link(link: str) -> str:
    """把 `api.zhihu.com` 的接口地址换成能打开的网页地址。

    动态接口给「添加了问题」这类条目返回的是 `https://api.zhihu.com/questions/<id>`，
    在阅读器里点开是一坨 JSON。ID 用的是原始链接（见 `_item_id`），所以这里改显示
    用的链接不会让老条目的 id 变化。
    """
    m = re.match(r"https?://api\.zhihu\.com/(questions|answers|pins)/(\d+)", link or "")
    if not m:
        return link
    path = {"questions": "question", "answers": "answer", "pins": "pin"}[m.group(1)]
    return f"https://www.zhihu.com/{path}/{m.group(2)}"


def _strip_noscript(content: str) -> str:
    """去掉 <noscript> 兜底块（主要是里面的重复图片）。

    知乎正文的每个 <figure> 里都有两张同样的图：一张是 <noscript> 里的
    无 JS 兜底，另一张是 RichText-ConditionalImagePortal 里的真实图。
    阅读器一般会忽略 <noscript> 标签本身、却照常渲染里面的 <img>，
    同一张图就渲染两次（实测 248 张兜底图全都能在块外找到同一个地址）。

    兜底块里只有图，所以整块丢掉；含别的内容的，拆掉标签、保留内容。
    """
    def repl(m: re.Match) -> str:
        inner = m.group(1)
        return "" if re.search(r"<img\b", inner, re.I) else inner

    return re.sub(r"<noscript\b[^>]*>(.*?)</noscript>", repl, content,
                  flags=re.S | re.I)


# ---------- 公式 ----------
#
# 正文里的公式是 `<span class="ztext-math" data-tex="...">`：页面上靠 MathJax 在浏览器里
# 把它渲染成内联 <svg>，而 feed 里是一份死 HTML，两种形态都不行：
#
# - 抓的时候 MathJax 还没渲染完（常见）：span 里只剩一段原始 TeX 源码（在
#   `.math-holder` 里），阅读器拿它当普通文字显示 —— 整篇公式变成
#   `\mathrm{RCA}_0 \subsetneq \mathrm{WKL}_0 ...` 这种谁也看不懂的东西；
# - 抓的时候已经渲染完：搬过去的 <svg> 里全是 `<use xlink:href="#E1-...">`，
#   而那些字形定义在页面别处一个隐藏 <svg> 的 <defs> 里，没跟着搬过来 ——
#   公式多半渲染成空白。
#
# 所以统一换成知乎自己的公式图接口（老版知乎的正文就是这么发图的，
# `data-eeimg="1"` 是行内、`"2"` 是独立成行的那类）：
#
#   <img class="eeimg" src="https://www.zhihu.com/equation?tex=<URL 编码的 TeX>" alt="<TeX>">
#
# 实测这个接口不要 cookie、不看 Referer，返回的 SVG 字形定义全在同一个文件里，
# 阅读器直接取就能画。alt 留原始 TeX：图片挂了或者阅读器不显示图片时至少还认得出。
_MATH_SPAN_OPEN_RE = re.compile(
    r'<span\b[^>]*class="[^"]*\bztext-math\b[^"]*"[^>]*>', re.I
)
_MATH_TEX_ATTR_RE = re.compile(r'data-tex="([^"]*)"', re.I)
_MATH_EEIMG_ATTR_RE = re.compile(r'data-eeimg="\s*(\d+)')
_MATH_TEX_SCRIPT_RE = re.compile(
    r'<script\b[^>]*type="\s*math/tex[^"]*"[^>]*>(.*?)</script>', re.S | re.I
)
_MATH_HOLDER_RE = re.compile(
    r'<span\b[^>]*class="[^"]*\bmath-holder\b[^"]*"[^>]*>(.*?)</span>', re.S | re.I
)
_EQUATION_BASE = "https://www.zhihu.com/equation?tex="
_EQUATION_SRC_RE = re.compile(
    r'src="(?:(?:https?:)?//[^"]*?/equation\?tex=|/equation\?tex=)([^"]*)"', re.I
)


def _matching_span_end(content: str, start: int) -> int:
    """`start` 是紧跟在 `<span ...>` 之后的位置，返回配平的那个 `</span>` 之后的位置。

    span 里套着 span，正则的 `.*?</span>` 会在第一个 `</span>` 就收手，所以只能数层数。
    """
    depth = 1
    for m in re.finditer(r"</?span\b", content[start:], re.I):
        if m.group(0).startswith("</"):
            depth -= 1
            if depth == 0:
                gt = content.find(">", start + m.end())
                return gt + 1 if gt != -1 else len(content)
        else:
            depth += 1
    return len(content)


def _absolute_equation_src(content: str) -> str:
    """公式图地址补成绝对地址：接口给的正文里是 `//www.zhihu.com/equation?...`。"""
    return _EQUATION_SRC_RE.sub(lambda m: f'src="{_EQUATION_BASE}{m.group(1)}"', content)


def _fix_math(content: str) -> str:
    """把 `ztext-math` 公式换成公式图（见上面那段说明）。幂等，重复跑没有副作用。"""
    if not content or "ztext-math" not in content:
        return _absolute_equation_src(content)

    out: list[str] = []
    pos = 0
    for m in _MATH_SPAN_OPEN_RE.finditer(content):
        if m.start() < pos:  # 已经跟着上一个 span 一起处理掉了
            continue
        tag = m.group(0)
        end = _matching_span_end(content, m.end())
        inner = content[m.end():end]

        tex = _MATH_TEX_ATTR_RE.search(tag)
        if tex is not None:
            raw = tex.group(1)
        else:
            # 没有 data-tex 的两种老写法：MathJax 的 script，或者兜底文字
            script = _MATH_TEX_SCRIPT_RE.search(inner)
            if script is not None:
                raw = script.group(1)
            else:
                holder = _MATH_HOLDER_RE.search(inner)
                raw = re.sub(r"<[^>]+>", "", holder.group(1)) if holder else None

        out.append(content[pos:m.start()])
        text = html.unescape(raw).strip() if raw is not None else ""
        if text:
            eeimg = _MATH_EEIMG_ATTR_RE.search(tag)
            src = _EQUATION_BASE + quote(text, safe="")
            if eeimg is None or eeimg.group(1) == "1":  # 1 = 行内公式
                src += "&amp;inline=true"
            alt = html.escape(text, quote=True)
            out.append(f'<img class="eeimg" src="{src}" alt="{alt}">')
        else:
            out.append(content[m.start():end])  # 认不出来，原样留着，别把内容弄丢
        pos = end
    out.append(content[pos:])
    return _absolute_equation_src("".join(out))


def _clean_zhihu_html(content: str) -> str:
    """清理知乎 DOM 里的正文 HTML。

    主要是图片：知乎是懒加载的，真实地址在 data-original / data-actualsrc 上，
    src 里是个占位图，直接搬过来会全是空白。

    公式要在这里先换掉：下面会把 <script> 整段删掉，公式的 script 就在里面。
    """
    if not content:
        return ""

    content = _strip_noscript(content)
    content = _fix_math(content)

    def fix_img(m: re.Match) -> str:
        tag = m.group(0)
        real = re.search(r'data-(?:original|actualsrc)="([^"]+)"', tag)
        if not real:
            return tag
        url = real.group(1)
        if 'src="' in tag:
            return re.sub(r'src="[^"]*"', f'src="{url}"', tag, count=1)
        return tag[:-1] + f' src="{url}">'

    content = re.sub(r"<img\b[^>]*>", fix_img, content)
    content = re.sub(r"<(script|style)\b.*?</\1>", "", content, flags=re.S | re.I)
    return content.strip()


def _rebuild_content(content: str, body: str) -> str:
    """把正文段落换成新的内容，保留开头的动作行和结尾的链接行。

    注意 body 是页面/API 给的原始 HTML，本身就是块级元素（`<p>` 序列），
    不能再套一层 `<p>`，否则就是非法的嵌套 `<p>`。
    """
    content = content or ""
    m = re.match(r"\s*(<p>.*?</p>)", content, re.S)
    head, rest = (m.group(1), content[m.end():]) if m else ("", content)
    tm = re.search(r"(<p><a [^>]*>.*?</a></p>)\s*$", rest, re.S)
    tail = tm.group(1) if tm else ""
    return head + (body or "").strip() + tail


def _link(o: dict) -> str:
    if o.get("target_type") == "answer" and o.get("question_id") and o.get("target_id"):
        return f"https://www.zhihu.com/question/{o['question_id']}/answer/{o['target_id']}"
    url = o.get("link") or ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.zhihu.com" + url
    return url


def _action(o: dict) -> str:
    return o.get("action") or VERB_ACTIONS.get(o.get("verb", ""), o.get("verb") or "动态")


def _item_id(o: dict) -> str:
    """时间戳 + 编辑时间 + 内容哈希：稳定、可排序，且不受平台换 ID 影响。

    `updated` 是有意编进来的：内容被编辑后 guid 跟着变，阅读器才会把新版本
    当成新条目收下来 —— guid 不变的话它不会去刷新已经收过的条目（见 README
    的「阅读器那一侧的缓存」）。拿不到 updated 的类型（目前只有问题）退回老形式。
    """
    raw = f"{_action(o)}|{o.get('title', '')}|{_link(o)}"
    created = int(o.get("created") or 0)
    updated = int(o.get("updated") or 0)
    if not created:
        return f"zhihu:{_short_hash(raw)}"
    if updated:
        return f"zhihu:{created}_{updated}_{_short_hash(raw)}"
    return f"zhihu:{created}_{_short_hash(raw)}"


def _https(url: str) -> str:
    """接口给的下一页地址是 http:// 的，在 https 页面里 fetch 会被当混合内容拦掉。"""
    return "https:" + url[len("http:"):] if url.startswith("http://") else url


def _as_block(content: str) -> str:
    """把一段正文放进 content 时的包装：本身已经是块级元素就别再套 `<p>`。

    通知里那段回复正文有时是纯文本（`自然淘汰不就可以了…`），有时是带 `<p>` 的
    HTML —— 后者再套一层 `<p>` 就是非法嵌套（`_rebuild_content` 那边同理）。
    """
    content = (content or "").strip()
    if not content:
        return ""
    if re.match(r"<(p|div|blockquote|figure|ul|ol|h[1-6])\b", content, re.I):
        return content
    return f"<p>{content}</p>"


def _notification_link(o: dict) -> str:
    """通知指向的那个内容的网页地址；没有就退回通知中心页面。

    内容被删除时接口只给一句「该内容被删除」、`link` 是空的 —— 那种至少让条目
    还能点回通知中心，别留一个空链接。
    """
    return _web_link((o.get("target_link") or "").strip()) or NOTIFICATIONS_PAGE


def _notification_title(o: dict) -> str:
    """通知的标题：`谁 + 干了什么 + ：+ 相关内容`。

    动词本身就带了场景（「回复了回答下你的评论」「喜欢了你的评论」），所以动作
    当标题主体；后面缀上相关内容的标题（问题/文章标题），否则列表里一排
    「某某 喜欢了你的评论」根本看不出是哪条。

    动词有的以空格开头（邀请那条是「 的提问等你来答」、前面接的就是人名），
    所以这里先把整串按空白归一化再拼。
    """
    head = " ".join(f"{o.get('actor', '')} {o.get('verb', '')}".split())
    target = " ".join((o.get("target_text") or "").split())
    if target and target not in head:
        return f"{head}：{target}" if head else target
    return head or "通知"


def _notification_item(o: dict, entry: str) -> Item:
    created = int(o.get("created") or 0)
    published = datetime.fromtimestamp(created) if created else None
    link = _notification_link(o)
    verb = " ".join((o.get("verb") or "").split())
    target_text = (o.get("target_text") or "").strip()

    # 正文结构和动态那边一致：[动作, (正文), (相关内容), 链接]（见 _body_of）。
    # 动作单独一段是给去重/过滤用的锚点，也省得阅读器列表里只剩标题。
    parts = [f"<p>{html.escape(verb)}</p>"]
    body = _as_block(_clean_zhihu_html(o.get("text") or ""))
    if body:
        parts.append(body)
    if target_text:
        parts.append(f"<p>{html.escape(target_text)}</p>")
    if link:
        parts.append(f'<p><a href="{html.escape(link)}">{html.escape(link)}</a></p>')

    return Item(
        id=f"zhihu-notif:{o.get('id', '')}",
        title=_notification_title(o),
        link=link,
        author=o.get("actor", ""),
        published=published,
        content="".join(parts),
        extra={
            "kind": "notification",
            "entry": entry,
            "verb": o.get("verb", ""),
            # 平台会把同类通知合并（多人点赞同一条评论就合成一条）。合并之后
            # 条目的 actors 会变多，但 id 不变 —— 按 id 去重、阅读器不重复提醒，
            # 代价是已收下的那条不会跟着刷新（见 README「阅读器那一侧的缓存」）。
            "merge_count": int(o.get("merge_count") or 0),
        },
    )


@register
class ZhihuProvider(Provider):
    type = "zhihu"
    description = "知乎个人主页动态 / 通知中心（options.mode）"

    @property
    def mode(self) -> str:
        mode = str(self.opt("mode", MODE_ACTIVITIES) or MODE_ACTIVITIES).strip()
        if mode not in MODES:
            raise ValueError(
                f"zhihu: 未知的 mode={mode!r}，可选：{'、'.join(MODES)}"
            )
        return mode

    def page_url(self) -> str:
        if self.mode == MODE_NOTIFICATIONS:
            return NOTIFICATIONS_PAGE
        return PROFILE.format(token=self.require("token"))

    def setup(self, bridge: Bridge) -> None:
        """确保当前标签页在知乎上 —— 同站里已经有页面就直接复用，不切页。

        列表接口的 token 在 URL 里（`api/v3/moments/<token>/activities`），页面是
        哪个答主的、是不是通知中心，都不影响返回什么 —— 只要页面在 `www.zhihu.com`
        上，请求就是同站的、带得上登录 cookie。所以四个动态源共用一页就够了，
        不必为了「打开页面 X」这句日志好看每轮多加载四次整页。

        真正要防的只有跨站那一种：上一条源是 B 站时当前标签页还停在 `t.bilibili.com`
        上（后台标签页，脚本刚把新标签页开出来就跑过来了），那时 fetch 属于跨站，
        `SameSite=Lax` 的登录 cookie 一个都不带，接口只会回「请求参数异常，请升级
        客户端后重试。」（见 PAGE_CHECK_JS 的说明）。所以只有页面不在知乎上时才导航，
        并且等它真的落地 —— navigate 是异步的，返回时新文档未必 commit。
        """
        expected = self.page_url()
        page = bridge.evaluate("({href: location.href, host: location.hostname})") or {}
        current = str(page.get("href") or "")
        if page.get("host") == "www.zhihu.com":
            if not same_page(current, expected):
                _note_page_reuse(bridge, current)
            return
        bridge.ensure_tab(expected, group_title=self.opt("group_title"))
        if self._settle(bridge, expected, PAGE_SETTLE_S) is None:
            print(f"    [标签页] 当前标签页还没落到 {expected}，重新打开", file=sys.stderr)
            self._goto(bridge, expected)

    def _settle(self, bridge: Bridge, expected: str, timeout_s: float) -> str | None:
        """等当前标签页的 location.href 变成 expected；等到了返回它，超时返回 None。"""
        deadline = time.monotonic() + timeout_s
        while True:
            current = bridge.evaluate("location.href") or ""
            if same_page(current, expected):
                return current
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.4)

    def _goto(self, bridge: Bridge, url: str) -> str:
        """把标签页导航到 url，等它落地后返回落地时的 URL。

        第一次沿用当前标签页（调用方刚确认过它是本 session 自己的标签页）；
        没落到 url 上就换新标签页再来一次 —— 当前标签页偶尔会和浏览器里真正在
        跑的那份文档对不上，重开一个能绕开卡住的那个。
        """
        for attempt in range(2):
            args: dict = {"url": url}
            if attempt:
                args["newTab"] = True
            else:
                args["group_title"] = self.opt("group_title")
            bridge.call("navigate", args)
            current = self._settle(bridge, url, PAGE_SETTLE_S)
            if current is not None:
                return current
        return bridge.evaluate("location.href") or ""

    def fetch(self, bridge: Bridge, known_ids: set[str]) -> list[Item]:
        if self.mode == MODE_NOTIFICATIONS:
            return self._fetch_notifications(bridge, known_ids)
        return self._fetch_activities(bridge, known_ids)

    # ---------- 个人主页动态 ----------

    def _fetch_activities(self, bridge: Bridge, known_ids: set[str]) -> list[Item]:
        max_pages = int(self.opt("max_pages", 5))
        url = API.format(token=self.require("token")) + "?offset=0&page_num=1"
        items: list[Item] = []
        attempts = int(self.opt("list_retries", 2)) + 1
        delay_s = float(self.opt("list_retry_delay_s", 15))

        for page in range(1, max_pages + 1):
            result = retry_call(
                lambda u=url, p=page: self._fetch_page(bridge, u, p),
                attempts=attempts, delay_s=delay_s, label=f"知乎动态第 {page} 页",
            )

            raw_items = result.get("items") or []
            if not raw_items:
                break

            page_items = [self._to_item(o) for o in raw_items]
            items.extend(page_items)

            if known_ids and page_items and all(i.id in known_ids for i in page_items):
                break
            if result.get("is_end") or not result.get("next"):
                break
            url = result["next"]

        return items

    # ---------- 通知中心 ----------

    def _fetch_notifications(self, bridge: Bridge, known_ids: set[str]) -> list[Item]:
        entry = self.entry_name()
        limit = int(self.opt("limit", 20))
        max_pages = int(self.opt("max_pages", 5))
        url = f"{NOTIFICATIONS_API}?limit={limit}&offset=0&entry_name={entry}"
        items: list[Item] = []
        attempts = int(self.opt("list_retries", 2)) + 1
        delay_s = float(self.opt("list_retry_delay_s", 15))

        for page in range(1, max_pages + 1):
            result = retry_call(
                lambda u=url, p=page: self._fetch_page(
                    bridge, u, p, template=NOTIFICATIONS_EXTRACT_JS,
                ),
                attempts=attempts, delay_s=delay_s,
                label=f"知乎通知（{NOTIFICATION_ENTRIES[entry]}）第 {page} 页",
            )

            raw_items = result.get("items") or []
            if not raw_items:
                break

            page_items = [_notification_item(o, entry) for o in raw_items]
            items.extend(page_items)

            if known_ids and page_items and all(i.id in known_ids for i in page_items):
                break
            if result.get("is_end"):
                break
            # 接口回的 next 是 http:// 的地址，在 https 页面里 fetch 会被当混合内容
            # 拦掉（实测直接 Failed to fetch），这里补成 https。
            next_url = _https(result.get("next") or "")
            if not next_url or next_url == url:
                break
            url = next_url

        return items

    def entry_name(self) -> str:
        """通知分类（`entry_name`），默认全部通知。"""
        entry = str(self.opt("entry_name", "all") or "all").strip()
        if entry not in NOTIFICATION_ENTRIES:
            known = "、".join(f"{k}（{v}）" for k, v in NOTIFICATION_ENTRIES.items())
            raise ValueError(f"zhihu: 未知的通知分类 entry_name={entry!r}，可选：{known}")
        return entry

    def _fetch_page(self, bridge: Bridge, url: str, page: int,
                    template: str = EXTRACT_JS,
                    label: str = "知乎动态") -> dict:
        """抓一页列表。失败抛异常，由 retry_call 决定要不要重来。"""
        code = template.replace("__URL__", url).replace("__PAGE_CHECK__", PAGE_CHECK_JS)
        deadline = _cookie_deadline(bridge)
        repairs = 0
        result = None
        while True:
            result = bridge.evaluate(code)
            if not result or result.get("ok"):
                _clear_cookie_deadline(bridge)
                break
            if result.get("not_on_page") and repairs < 2:
                # 页面不在知乎上：跟接口本身没关系，等多久都是同一句「请求参数异常」。
                # 切回目标页面立刻重来一次，别让它白占 retry_call 那 15 秒的重试。
                repairs += 1
                print(f"    [标签页] {result.get('error')}，切回 {self.page_url()} 后重试",
                      file=sys.stderr)
                self.setup(bridge)
                continue
            if result.get("no_cookie") and time.monotonic() < deadline:
                # 页面没问题，是浏览器这边暂时交不出登录 cookie（冷启动、刚唤醒最
                # 典型）。在原地快重试没用（线上就是这么三连失败的），这里拉长间隔
                # 慢慢等 —— 什么都不用做，时间到了它自己就好。
                # 注意别在这里 reload 页面：那是用户正在看的窗口，反复刷新很难受，
                # 而且也没有证据说刷新能加快恢复。
                print(f"    [登录态] {result.get('error')}，{COOKIE_RETRY_S:.0f}s 后再看一次"
                      f"（等浏览器把 cookie 交出来，最多等到 {deadline - time.monotonic():.0f}s 后）",
                      file=sys.stderr)
                time.sleep(COOKIE_RETRY_S)
                continue
            break
        if not result or not result.get("ok"):
            detail = (result or {}).get("error") or result
            if result and result.get("no_cookie"):
                detail = f"{detail}；请在浏览器里确认知乎还登着（或用 ./main.sh clean 后重跑）"
            raise RuntimeError(f"知乎接口返回异常（{label}第 {page} 页）: {detail}")
        return result

    def _to_item(self, o: dict) -> Item:
        created = int(o.get("created") or 0)
        published = datetime.fromtimestamp(created) if created else None
        # 接口对「添加了问题」这类动态给的是 api.zhihu.com 的链接，换成能打开的网页地址。
        # 条目 id 由 _item_id(o) 用原始链接算（见那边的说明），所以这里不影响去重。
        link = _web_link(_link(o))
        title = o.get("title") or _first_line(o.get("preview", "")) or _action(o)
        preview = o.get("preview", "")

        parts = [f"<p>{html.escape(_action(o))}</p>"]
        if preview:
            parts.append(f"<p>{html.escape(preview)}</p>")
        if link:
            parts.append(f'<p><a href="{html.escape(link)}">{html.escape(link)}</a></p>')

        return Item(
            id=_item_id(o),
            # 标题不带动作前缀，直接是「作者 + 空格 + 标题」，和 B 站那边保持一致。
            # 动作（收藏了回答/回答了问题/…）没有丢，还在正文第一段里。
            title=f"{o.get('author', '')} {title}".strip(),
            link=link,
            author=o.get("author", ""),
            published=published,
            content="".join(parts),
            extra={
                "type": o.get("target_type", ""),
                "verb": o.get("verb", ""),
                # 内容的当前版本（0 = 这个类型拿不到）。id 和「旧版本该不该清掉」
                # 都靠它判断，见 _item_id / _identity。
                "updated": int(o.get("updated") or 0),
            },
        )

    def postprocess(self, items: list[Item], bridge: Bridge) -> list[Item]:
        items = super().postprocess(items, bridge)
        if self.mode == MODE_NOTIFICATIONS:
            # 通知是平台生成的一条条记录：没有正文可补、也没有「同一条内容换个
            # 动作又出现一次」的孪生（那套去重是给动态流的），所以到这里就够了。
            return items
        # 存量条目的 content 在抓下来那一刻就存进 state 了，光修抓取逻辑清不掉
        # 里面的 <noscript> 兜底图（这类条目 extra.fulltext 已标记，不会再重抓），
        # 所以每次跑都统一过一遍。幂等，重复跑没有副作用。
        for item in items:
            item.content = _strip_noscript(item.content)
        if self.opt("dedupe_by_content", True):
            items, self.published_content = _dedupe_by_content(
                items,
                seen=self.seen_content,
                memory_days=self.opt("dedupe_memory_days", 45),
                on_drop=self.note_drop,
                # 被新版本取代的旧版本不要再拦新版本（见 _dedupe_by_content）
                superseded=self.superseded_ids,
            )
        if self.opt("fulltext", True):
            items = self._enrich_fulltext(items, bridge)
        # 公式换成公式图（见 _fix_math 的说明）。存量条目和全文一样，改抓取逻辑清不掉
        # 老内容里的 `ztext-math`，所以每轮统一过一遍，幂等。
        # 放在最后是有意的：正文指纹按摘要算、算出来就粘住（见 _dedup_key），
        # 这里动 content 不该影响去重结果。
        for item in items:
            item.content = _fix_math(item.content)
        return items

    # ---------- 编辑 ----------

    def prune_superseded(self, items: list[Item]) -> list[Item]:
        """丢掉被新版本取代的旧条目。

        动态接口只返回内容的**当前版本**（带 updated），所以一条回答被编辑之后，
        state 里那份旧版本再也不会被刷新 —— 标题和链接还跟新版本一模一样，
        留着只会白占 history 的位置、在 RSS 里显示成一条重复条目。

        判据是「同一条内容的身份」（见 `_identity`）里 updated 最大的那份才留。
        同身份的其它条目分两种，都丢：

        - 手里有 updated 但更小的：真·旧版本；
        - 手里没有 updated 的：加这个字段之前存下来的条目。同身份既然已经有当前
          版本，它多半就是那条的历史遗留（id 形式换过），一并清掉。

        问题类动态的 target 本来就不带 updated，取到 0，不会命中，原样保留。
        """
        if self.mode == MODE_NOTIFICATIONS:
            # 通知不带 updated（extra.updated 一律 0），这套「留最新版本」的逻辑
            # 对它没有意义。
            return items
        newest: dict[tuple[str, str], int] = {}
        for item in items:
            key = _identity(item)
            updated = int(item.extra.get("updated") or 0)
            if key and updated > newest.get(key, 0):
                newest[key] = updated
        if not newest:
            return items

        kept: list[Item] = []
        for item in items:
            key = _identity(item)
            top = newest.get(key, 0) if key else 0
            if top and int(item.extra.get("updated") or 0) < top:
                self.superseded_ids.add(item.id)
                continue
            kept.append(item)

        if self.superseded_ids:
            print(f"    [编辑] 清掉 {len(self.superseded_ids)} 条被新版本取代的旧条目")
        return kept

    # ---------- 全文 ----------

    def _enrich_fulltext(self, items: list[Item], bridge: Bridge) -> list[Item]:
        """给还没有全文的条目补上正文。

        全文存在 state 里（extra.fulltext 标记），所以每条只会抓一次：
        新增几条就只抓几篇。首次启用时历史条目会按 max_fulltext_per_run
        分批补齐，不会让单次运行卡太久。
        """
        pending = [i for i in items if not i.extra.get("fulltext")]
        budget = int(self.opt("max_fulltext_per_run", 10))

        if pending:
            print(f"    [全文] 待补 {len(pending)} 篇，本次最多处理 {budget} 篇")
        todo = pending[:budget]
        for n, item in enumerate(todo, 1):
            item_type = item.extra.get("type", "")
            item_id = _id_from_link(item.link)
            # 连页面都定位不了的：type 不在选择器表里（知乎又出了新类型），或者根本
            # 没有链接 / 从链接里取不出 id —— 早期抓下来的条目里有 type、link 都空着的。
            # 这种重试多少次都一样。直接按「没有正文可抓」记下，别占着每轮的配额，
            # 也别让它一直挂在「待补 N 篇」里（否则下面那行失败会每轮打一次）。
            if item_type not in FULLTEXT_SELECTORS or not item_id:
                item.extra["fulltext"] = True
                print(f"    [全文] 跳过 {item_type or '(无类型)'} "
                      f"{item_id or '(无链接)'}：没有可用的页面选择器")
                continue
            try:
                # 「添加了问题」这类条目的链接是 api.zhihu.com 的接口地址，
                # 浏览器要去网页版才有内容（见 _web_link）。
                body = self._fulltext_with_retry(
                    bridge, item_type, _web_link(item.link), item_id
                )
            except Exception as e:
                print(f"    [全文] 失败 {item_type}/{item_id}: {e}", file=sys.stderr)
            else:
                if body:
                    item.content = _rebuild_content(item.content, body)
                # 抓到空正文也算处理过了（问题本来就可能没有描述），
                # 否则这条会一直挂在「待补」里，每轮重抓一遍。
                item.extra["fulltext"] = True
                # 告诉 Store：这条的 content 已经是最终版，别被重新渲染的摘要覆盖
                item.extra["content_locked"] = True
                got = f"{len(body)} 字符" if body else "页面没有正文"
                print(f"    [全文] {n}/{len(todo)} {item_type} {item_id} ({got})")
            if n < len(todo):
                time.sleep(random.randint(ITEM_DELAY_MS, ITEM_DELAY_MS + 2000) / 1000)
        return items

    def _fulltext_with_retry(self, bridge: Bridge, item_type: str, url: str,
                             item_id: str) -> str:
        # 文章页更容易被风控随机重定向，多给几次机会
        max_retries = 4 if item_type == "article" else 2
        retry_sleep = 12 if item_type == "article" else 10
        for attempt in range(max_retries + 1):
            try:
                return self._fulltext(bridge, item_type, url, item_id)
            except RuntimeError:
                if attempt >= max_retries:
                    raise
                time.sleep(retry_sleep)

    def _fulltext(self, bridge: Bridge, item_type: str, url: str, item_id: str) -> str:
        if item_type == "pin":
            # 想法优先走 API：页面会随机重定向到别的想法，而 API 返回结构化内容块
            try:
                return self._pin_api(bridge, item_id)
            except RuntimeError:
                pass
        return self._from_page(bridge, item_type, url)

    def _from_page(self, bridge: Bridge, item_type: str, url: str) -> str:
        selectors = FULLTEXT_SELECTORS.get(item_type)
        if selectors is None:
            # 知乎新出的动态类型：还没配选择器，明说而不是悄悄当成功
            raise RuntimeError(f"还没有 {item_type!r} 这类页面的选择器（FULLTEXT_SELECTORS）")
        title_sels, content_sels = selectors
        # 在当前标签页里导航（不是新开标签），抓完由 session 统一清理。
        # _goto 会回读确认真的到了目标页，没到就（换新标签页）再来一次 ——
        # 否则这里会拿着上一页的 DOM 当正文，或者被下面的重定向检查误判成风控。
        final_url = self._goto(bridge, url)
        if final_url and not same_page(final_url, url):
            raise RuntimeError(f"页面被重定向到 {final_url}（内容不可用或触发风控）")

        val = bridge.evaluate(_extract_js(title_sels, content_sels))
        if not val or not val.get("hasContent"):
            if item_type in OPTIONAL_CONTENT_TYPES:
                # 问题可以没有描述（很常见），页面上就没有那块 —— 不是抓取失败
                return ""
            raise RuntimeError("未找到页面正文（可能被风控拦截）")
        if val.get("collapsed"):
            # 「显示全部」没点上，手里这份是带「…」的半截；与其把它锁进 state，
            # 不如报失败让上层重来一次
            raise RuntimeError("正文仍是折叠状态（没能点开「显示全部」）")

        return _clean_zhihu_html(val.get("content", ""))

    def _pin_api(self, bridge: Bridge, pin_id: str) -> str:
        # fetch 只带当前标签页的 cookie：页面不在知乎上时这个接口必定回
        # 「请求参数异常」，那就别试了，直接交给 _from_page 去导航到想法页。
        host = bridge.evaluate("location.hostname") or ""
        if host != "www.zhihu.com":
            raise RuntimeError(f"当前标签页不在知乎上（{host or '(空)'}）")
        r = bridge.fetch_json(f"https://www.zhihu.com/api/v4/pins/{pin_id}")
        if not r.get("ok"):
            raise RuntimeError(f"pin API 请求失败: status={r.get('status')} error={r.get('error')}")
        body = r.get("body") or {}
        if "error" in body:
            raise RuntimeError(f"pin API 返回错误: {body['error']}")

        blocks = body.get("content")
        if not isinstance(blocks, list):
            raise RuntimeError("pin API 返回内容为空")

        parts = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("content"):
                parts.append(block["content"])
            elif block.get("type") == "image" and block.get("url"):
                parts.append(f'<img src="{block["url"]}">')
            elif block.get("type") == "link_card" and block.get("url"):
                title = block.get("data_draft_title") or block["url"]
                parts.append(f'<a href="{block["url"]}">{title}</a>')
            elif block.get("url"):
                parts.append(f'<a href="{block["url"]}">{block["url"]}</a>')

        if not "".join(parts).strip():
            raise RuntimeError("pin API 返回内容为空")
        return _clean_zhihu_html("\n".join(parts))


def _body_of(content: str) -> str:
    """从渲染好的 content 里取正文段落。

    结构固定为 [动作, (正文), (链接)]，动作段是纯文本、链接段以 <a 开头，
    所以去掉首段和链接段剩下的就是正文。
    """
    paras = re.findall(r"<p>(.*?)</p>", content or "", re.S)
    body = [p for p in paras[1:] if not p.lstrip().startswith("<a ")]
    return " ".join(body).strip()


def _dedup_key(item: Item) -> str | None:
    """条目正文指纹，第一次算出来就粘在 extra 上。

    必须粘住：全文补全会把 content 换成整篇 HTML，那时再按 content 算
    指纹就跟原来的摘要对不上了，同一对「回答了问题 / 收藏了回答」会重新
    变成两条。
    """
    key = item.extra.get("dedup_key")
    if key:
        return key
    body = _body_of(item.content)
    if not body:
        return None
    key = hashlib.md5(body.encode("utf-8")).hexdigest()
    item.extra["dedup_key"] = key
    return key


def _identity(item: Item) -> tuple[str, str] | None:
    """同一条内容的身份：链接 + 动作。

    编辑只会换版本、不会换动词；同一篇回答的「回答了问题 / 收藏了回答」动作不同，
    本来就是两条动态，所以这对孪生不算同一个身份、不会互相取代。
    """
    if not item.link:
        return None
    return (item.link, str(item.extra.get("verb") or ""))


def _seen_time(value) -> str:
    """指纹表的值是「首次发布时间|发布它的条目 id」；老格式只有时间。"""
    return str(value or "").split("|", 1)[0]


def _seen_owner(value) -> str:
    """指纹表里记的发布者（条目 id）；老格式没有记，返回空串。"""
    raw = str(value or "")
    return raw.split("|", 1)[1] if "|" in raw else ""


def _prune_seen(seen: dict[str, str] | None, memory_days) -> dict[str, str]:
    """丢掉超出保留期的指纹（保留期 0 或负数 = 整份丢掉，即关掉跨窗口去重）。"""
    if not seen or not memory_days or float(memory_days) <= 0:
        return {}
    cutoff = datetime.now() - timedelta(days=float(memory_days))
    kept: dict[str, str] = {}
    for key, when in (seen or {}).items():
        try:
            t = datetime.fromisoformat(_seen_time(when))
        except ValueError:
            continue
        if t >= cutoff:
            kept[key] = when
    return kept


def _dedupe_by_content(
    items: list[Item],
    seen: dict[str, str] | None = None,
    memory_days=45,
    on_drop=None,
    superseded: set[str] | None = None,
) -> tuple[list[Item], dict[str, str]]:
    """正文相同的只保留一条；并把「这条正文已经发出去过」记进指纹表。

    同一条回答会同时以「回答了问题」和「收藏了回答」出现，正文完全一样，
    只有动作不同。保留**较早**的那条：单条内容的生命周期更长，
    RSS 阅读器不会因为动作换了就把它当成新条目重复提醒。

    但**光比窗口内的条目不够**：一对双胞胎里较早的那条（通常就是已经发出去、
    阅读器里已经有了的那条）会先被 `history` 裁掉，后一条随即成了孤家寡人 ——
    它的 guid 带着自己的动作和自己的时间（见 `_item_id`），和已发过的那条不同，
    于是同一篇文章被当成新条目又发了一遍。所以「已经发过哪些正文」得单独记一份，
    存在 state 的 `seen_content` 里，不跟着条目一起裁（见 Store）。

    seen 就是这份指纹表，值是 `首次发布时间|发布它的条目 id`：**必须记下发布者**。
    只按指纹去拦的话，窗口里那些早就发过、而且还在窗口里的条目，下一轮会全被
    当成「别人发过的同一份正文」丢掉 —— feed 每轮只剩新冒出来的那几条，越跑越空。

    返回（保留的条目, 更新后的指纹表）—— 后者由 cli 交回 Store 落盘。

    on_drop(item, reason) 每丢一条调一次，给过滤清单日志用。
    superseded：本轮刚被新版本取代的旧条目 id（见 ZhihuProvider.prune_superseded）。
    """
    registry = _prune_seen(seen, memory_days)
    now = datetime.now().isoformat(timespec="seconds")
    in_window: set[str] = set()  # 本轮窗口里已经见过的指纹
    published = dict(registry)  # 本轮发出去之后，「已发过」的完整名单
    superseded = superseded or set()
    kept: list[Item] = []

    for item in sorted(items, key=sort_key):
        key = _dedup_key(item)
        if key is None:
            # 没有正文的（关注了问题等）不参与跨条目去重
            kept.append(item)
            continue
        if key in in_window:
            if on_drop is not None:
                on_drop(item, "正文与另一条重复（dedupe_by_content）")
            continue
        in_window.add(key)
        first = _seen_time(registry.get(key))
        owner = _seen_owner(registry.get(key))
        # 只有「发布者是别人」才拦：发布者就是自己的，说明这条一直在窗口里、
        # 只是又走了一遍，得留着。老格式没记发布者（owner 为空）拿不准，宁可放行。
        # superseded 里的是刚被新版本取代的旧版本，它登记的指纹也拦不得 ——
        # 内容被编辑后新旧两版可能撞同一个指纹（编辑没动开头，摘要不变），
        # 那时拦下来就等于把新版本吞了。
        if owner and owner != item.id and owner not in superseded:
            # 双胞胎里已经发过的那条早被裁掉了，只剩这条 —— 别再发一遍
            if on_drop is not None:
                on_drop(
                    item,
                    f"正文早先已发布过（首次 {first[:16]}，跨窗口去重）",
                )
            continue
        published[key] = f"{first or now}|{item.id}"
        kept.append(item)

    return sorted(kept, key=sort_key, reverse=True), published


def _first_line(s: str, limit: int = 60) -> str:
    for line in (s or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""
