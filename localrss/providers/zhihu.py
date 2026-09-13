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
from datetime import datetime
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
}

PAGE_WAIT_MS = 3000      # 导航后等页面渲染
ITEM_DELAY_MS = 3000     # 两次抓取之间的间隔（再叠随机抖动）
MIN_CONTENT_LEN = 50


def _extract_js(title_selectors: list[str], content_selectors: list[str]) -> str:
    # json.dumps 出来的就是合法 JS 字面量（双引号）
    titles = json.dumps(list(title_selectors))
    contents = json.dumps(list(content_selectors))
    return f"""
    (() => {{
        const titleSelectors = {titles};
        const contentSelectors = {contents};
        const minLen = {MIN_CONTENT_LEN};

        let title = document.title;
        for (const sel of titleSelectors) {{
            const el = document.querySelector(sel);
            if (el && el.innerText.trim()) {{ title = el.innerText.trim(); break; }}
        }}

        let contentHtml = '';
        let hasContent = false;
        for (const sel of contentSelectors) {{
            const el = document.querySelector(sel);
            if (el && el.innerHTML.length > minLen) {{
                contentHtml = el.innerHTML;
                hasContent = true;
                break;
            }}
        }}

        return {{ title, content: contentHtml, hasContent }};
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
    for pattern in (r"/answer/(\d+)", r"zhuanlan\.zhihu\.com/p/(\d+)", r"/pin/(\d+)"):
        m = re.search(pattern, link or "")
        if m:
            return m.group(1)
    return ""


def _clean_zhihu_html(content: str) -> str:
    """清理知乎 DOM 里的正文 HTML。

    主要是图片：知乎是懒加载的，真实地址在 data-original / data-actualsrc 上，
    src 里是个占位图，直接搬过来会全是空白。
    """
    if not content:
        return ""

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
    """时间戳 + 内容哈希：稳定、可排序，且不受平台换 ID 影响。"""
    raw = f"{_action(o)}|{o.get('title', '')}|{_link(o)}"
    created = o.get("created") or 0
    return f"zhihu:{int(created)}_{_short_hash(raw)}" if created else f"zhihu:{_short_hash(raw)}"


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
        link = _link(o)
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
            extra={"type": o.get("target_type", ""), "verb": o.get("verb", "")},
        )

    def postprocess(self, items: list[Item], bridge: Bridge) -> list[Item]:
        items = super().postprocess(items, bridge)
        if self.opt("dedupe_by_content", True):
            items = _dedupe_by_content(items)
        if self.opt("fulltext", True):
            items = self._enrich_fulltext(items, bridge)
        return items

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
        todo = [i for i in pending[:budget]
                if i.extra.get("type") in FULLTEXT_SELECTORS and _id_from_link(i.link)]
        for n, item in enumerate(todo, 1):
            item_type = item.extra["type"]
            item_id = _id_from_link(item.link)
            try:
                body = self._fulltext_with_retry(bridge, item_type, item.link, item_id)
            except Exception as e:
                print(f"    [全文] 失败 {item_type}/{item_id}: {e}", file=sys.stderr)
            else:
                item.content = _rebuild_content(item.content, body)
                item.extra["fulltext"] = True
                # 告诉 Store：这条的 content 已经是最终版，别被重新渲染的摘要覆盖
                item.extra["content_locked"] = True
                print(f"    [全文] {n}/{len(todo)} {item_type} {item_id} "
                      f"({len(body)} 字符)")
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
        title_sels, content_sels = FULLTEXT_SELECTORS[item_type]
        # 在当前标签页里导航（不是新开标签），抓完由 session 统一清理
        bridge.call("navigate", {"url": url})
        time.sleep(PAGE_WAIT_MS / 1000)

        final_url = bridge.evaluate("location.href") or ""
        if final_url and not _same_page(final_url, url):
            raise RuntimeError(f"页面被重定向到 {final_url}（内容不可用或触发风控）")

        val = bridge.evaluate(_extract_js(title_sels, content_sels))
        if not val or not val.get("hasContent"):
            raise RuntimeError("未找到页面正文（可能被风控拦截）")

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


def _dedupe_by_content(items: list[Item]) -> list[Item]:
    """正文相同的只保留一条。

    同一条回答会同时以「回答了问题」和「收藏了回答」出现，正文完全一样，
    只有动作不同。保留**较早**的那条：单条内容的生命周期更长，
    RSS 阅读器不会因为动作换了就把它当成新条目重复提醒。
    """
    seen: set[str] = set()
    kept: list[Item] = []
    for item in sorted(items, key=sort_key):
        key = _dedup_key(item)
        if key is None:
            # 没有正文的（关注了问题等）不参与跨条目去重
            kept.append(item)
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return sorted(kept, key=sort_key, reverse=True)


def _first_line(s: str, limit: int = 60) -> str:
    for line in (s or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""
