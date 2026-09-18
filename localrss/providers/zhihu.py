"""知乎动态 provider。

对应 https://www.zhihu.com/people/<token> 的「动态」页，
直接调用 api/v3/moments/<token>/activities，在浏览器里带 cookie 请求。
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
from urllib.parse import urlsplit

from ..bridge import Bridge
from ..models import Item, sort_key
from . import register
from .base import Provider, retry_call

API = "https://www.zhihu.com/api/v3/moments/{token}/activities"
PROFILE = "https://www.zhihu.com/people/{token}"

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

EXTRACT_JS = r"""
(async () => {
  try {
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

PAGE_WAIT_MS = 3000      # 导航后等页面渲染
ITEM_DELAY_MS = 3000     # 两次抓取之间的间隔（再叠随机抖动）
MIN_CONTENT_LEN = 50

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


def _same_page(final_url: str, expected_url: str) -> bool:
    """页面最终 URL 与目标是否还是同一个内容页（忽略 query/hash）。"""
    try:
        f, e = urlsplit(final_url), urlsplit(expected_url)
        if f.netloc != e.netloc:
            return False
        return f.path.rstrip("/") == e.path.rstrip("/")
    except Exception:
        return final_url.split("?")[0] == expected_url.split("?")[0]


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


def _clean_zhihu_html(content: str) -> str:
    """清理知乎 DOM 里的正文 HTML。

    主要是图片：知乎是懒加载的，真实地址在 data-original / data-actualsrc 上，
    src 里是个占位图，直接搬过来会全是空白。
    """
    if not content:
        return ""

    content = _strip_noscript(content)

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


@register
class ZhihuProvider(Provider):
    type = "zhihu"
    description = "知乎个人主页动态"

    def page_url(self) -> str:
        return PROFILE.format(token=self.require("token"))

    def fetch(self, bridge: Bridge, known_ids: set[str]) -> list[Item]:
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

    def _fetch_page(self, bridge: Bridge, url: str, page: int) -> dict:
        """抓一页动态列表。失败抛异常，由 retry_call 决定要不要重来。"""
        result = bridge.evaluate(EXTRACT_JS.replace("__URL__", url))
        if not result or not result.get("ok"):
            detail = (result or {}).get("error") or result
            raise RuntimeError(f"知乎接口返回异常（第 {page} 页）: {detail}")
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
        # 在当前标签页里导航（不是新开标签），抓完由 session 统一清理
        bridge.call("navigate", {"url": url})
        # 先等导航落定，再确认没被重定向；正文什么时候算渲染好由下面的
        # _extract_js 在页面里轮询判断（固定 sleep 会抓到半截）
        time.sleep(PAGE_WAIT_MS / 1000)

        final_url = bridge.evaluate("location.href") or ""
        if final_url and not _same_page(final_url, url):
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
