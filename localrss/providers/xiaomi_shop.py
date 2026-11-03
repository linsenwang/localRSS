"""小米商城 provider（直连接口，不需要 WebBridge）：上新日历 + 众筹。

上新日历: POST http://api.m.mi.com/v1/home/product_channel_get_list
鉴权: 无 sign、无登录态，只需要固定的一组客户端头 + 时间戳/随机数。

返回里的 `history_date_list` 是按日期分好组的上新日历（每页 20 个日期桶），
`new_list` 是最新 20 个新品（和日期桶有重叠，按 product_id 去重）。
翻页用 `page_index`（从 1 开始），`page_size` 是**日期桶**的个数，不是商品数。

众筹（`crowdfunding: true`，默认开）走 mtop venue 接口：
  GET  .../mtop/navi/venue/page?page_id=16044
  POST .../mtop/navi/venue/batch?page_id=16044
页面配置里 `mishop_crowd_list` 是众筹好物（开售），`mishop_crowd_reserve`
是众筹新品（预约）；列表参数（resolver / sign / parameter）从配置里现读。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from ..bridge import Bridge
from ..models import Item
from . import register
from .base import Provider, retry_call

LIST_URL = "http://api.m.mi.com/v1/home/product_channel_get_list"
PRODUCT_URL = "https://www.mi.com/item/{product_id}"
SITE_PAGE = "https://www.mi.com/"

# ---- 小米众筹（和上新日历同一个 feed）----
# 页面配置和列表都走 mtop venue 接口，不需要 sign 计算 / 登录态：
#   GET  .../mtop/navi/venue/page?page_id=16044
#   POST .../mtop/navi/venue/batch?page_id=16044
# 列表参数（resolver / sign / parameter）从页面配置的 floors 里现读，不写死。
CROWD_PAGE_ID = 16044
CROWD_SIGN = "4884c67062fa2f6859bac8d48934d483"
CROWD_PAGE_URL = (
    f"https://shop-api.retail.mi.com/mtop/navi/venue/page"
    f"?page_id={CROWD_PAGE_ID}&pdl=mishop&sign={CROWD_SIGN}"
)
CROWD_BATCH_URL = (
    f"https://shop-api.retail.mi.com/mtop/navi/venue/batch"
    f"?page_id={CROWD_PAGE_ID}&pdl=mishop&sign={CROWD_SIGN}"
)
CROWD_HOME = "https://m.mi.com/crowdfunding"
CROWD_DETAIL = "https://m.mi.com/crowdfunding/proddetail/{project_id}"

CROWD_HEADERS = {
    "x-user-agent": "channel/mishop platform/mishop.android",
    "d-model": "UmVkbWkgSzMw",  # base64("Redmi K30")
    "j-app-version": "5.55.0.20261010.r1",
    "Cookie": (
        "xmUuid=XMGUEST-d36fd1f4-7f4c-40d9-b857-2a404637a19b; "
        "Android_native=platform"
    ),
    "User-Agent": "okhttp/3.12.3",
}

#: 接口返回的时间都是北京时间
TZ = timezone(timedelta(hours=8))

#: 客户端头是固定的，接口不看登录态；换成别的 App 版本也能用。
HEADERS = {
    "Mishop-Client-Id": "180100031052",
    "Mishop-Client-VersionName": "5.55.0.20261010.r1",
    "Mishop-Client-VersionCode": "20261010",
    "Mishop-Model": "Redmi K30",
    "Uuid": "c575381a-c985-0c3d-0e4d-5426ce4e2bdf",
    "Device-Id": "00000000-0000-0000-0000-000000000000",
    "device": "P8Abbqf8TC1yddYrRFOIxQ==",
    "Android-Ver": "32",
    "Cookie": "xmuuid=XMGUEST-d36fd1f4-7f4c-40d9-b857-2a404637a19b;",
    "User-Agent": "okhttp/3.12.3",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


def _date_of(raw: str) -> datetime | None:
    """日期桶的 `YYYYMMDD` → 当天 0 点（北京时间）。"""
    raw = str(raw or "")
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return datetime(int(raw[:4]), int(raw[4:6]), int(raw[6:8]), tzinfo=TZ)
    except ValueError:
        return None


def _published(raw, fallback: datetime | None = None) -> datetime | None:
    """`start_time` 是上新当天 0 点的 unix 时间戳（秒）。"""
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return fallback
    if ts <= 0:
        return fallback
    try:
        return datetime.fromtimestamp(ts, TZ)
    except (OSError, OverflowError):
        return fallback


def _datetime_of(raw) -> datetime | None:
    """众筹接口的时间和上新日历不一样：可能给秒，也可能给毫秒。"""
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 10**11:
        ts /= 1000
    try:
        return datetime.fromtimestamp(ts, TZ)
    except (OSError, OverflowError, ValueError):
        return None


def _esc(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@register
class XiaomiShopProvider(Provider):
    type = "xiaomi_shop"
    description = "小米商城上新日历（直连接口，不需要浏览器）"
    needs_browser = False

    def __init__(self, options: dict | None = None, common: dict | None = None):
        super().__init__(options, common)
        self.max_pages = max(1, int(self.opt("max_pages", 3)))
        self.page_size = max(1, int(self.opt("page_size", 20)))
        self.timeout = max(1, int(self.opt("timeout", 15)))
        self.crowdfunding = str(self.opt("crowdfunding", True)).lower() not in (
            "false", "0", "no", "off",
        )
        # 绕开系统代理：接口是给手机 App 用的，走代理反而容易被拦。
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    # ---------- 抓取 ----------

    def page_url(self) -> str:
        return SITE_PAGE

    def setup(self, bridge: Bridge) -> None:
        # 直连接口，不需要切标签页。
        return None

    def _request(self, page: int) -> dict:
        body = urllib.parse.urlencode({
            "timestamp_mishop_client": int(time.time() * 1000),
            "page_index": page,
            "page_size": self.page_size,
            "nonce_mishop_client": uuid.uuid4().hex,
        }) + "&"
        req = urllib.request.Request(
            LIST_URL, data=body.encode(), headers=HEADERS
        )
        with self._opener.open(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        if payload.get("code") != 0:
            raise RuntimeError(f"小米商城接口返回异常: {payload}")
        return payload.get("data") or {}

    def _parse(self, data: dict) -> list[Item]:
        """把一页里的「未来新品 + 日期桶」压成 Item，按 product_id 去重。"""
        raw_items: list[tuple[dict, datetime | None]] = []

        # new_list 是最新 20 个新品，和日期桶有重叠；先收它，日期桶再覆盖一遍
        # start_time 也无所谓 —— 后面按 product_id 去重。
        for it in data.get("new_list") or []:
            raw_items.append((it, None))
        for bucket in data.get("history_date_list") or []:
            day = _date_of(bucket.get("date"))
            for it in bucket.get("product_list") or []:
                raw_items.append((it, day))

        items: list[Item] = []
        seen: set[str] = set()
        for it, day in raw_items:
            pid = str(it.get("product_id") or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            items.append(self._to_item(pid, it, day))
        return items

    def _to_item(self, pid: str, it: dict, day: datetime | None) -> Item:
        name = str(it.get("product_name") or "").strip() or f"新品 {pid}"
        link = PRODUCT_URL.format(product_id=pid)
        published = _published(it.get("start_time"), day)
        tags = [str(t).strip() for t in (it.get("custom_tag") or []) if str(t).strip()]

        price = str(it.get("price") or "").strip()
        market = str(it.get("market_price") or "").strip()
        price_text = f"¥{price}" if price else ""
        if price_text and it.get("is_multi_price"):
            price_text += " 起"
        if price_text and market and market != price:
            price_text += f"（原价 ¥{market}）"

        popnew = str(it.get("popnew_title") or "").strip()
        img = str(it.get("img") or "").strip()

        parts: list[str] = []
        if img:
            parts.append(
                f'<p><img src="{_esc(img)}" alt="" style="max-width:100%"></p>'
            )
        if price_text:
            parts.append(f"<p><strong>{_esc(price_text)}</strong></p>")
        if popnew:
            parts.append(f"<p>{_esc(popnew)}</p>")

        meta = list(tags)
        if published:
            meta.append(f"{published:%Y-%m-%d} 上架")
        if meta:
            parts.append(
                f'<p style="color:#666;font-size:90%">{_esc(" · ".join(meta))}</p>'
            )
        parts.append(f'<p><a href="{_esc(link)}">查看商品</a></p>')

        return Item(
            id=f"mi:{pid}",
            title=name,
            link=link,
            author="小米商城",
            published=published,
            content="".join(parts),
            extra={
                "product_id": pid,
                "price": price,
                "market_price": market,
                "tags": tags,
                "start_time": it.get("start_time"),
                "thumb": img,
            },
        )

    # ---------- 小米众筹（同一个 feed 里的另一段）----------

    def _crowd_page(self) -> dict:
        req = urllib.request.Request(CROWD_PAGE_URL, headers=CROWD_HEADERS)
        with self._opener.open(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        if payload.get("code") != 0:
            raise RuntimeError(f"小米众筹页面配置返回异常: {payload}")
        return payload.get("data") or {}

    def _crowd_batch(self, queries: list[dict]) -> list[dict]:
        body = json.dumps({
            "query_list": [{**q, "variable": {}} for q in queries],
        }).encode()
        req = urllib.request.Request(
            CROWD_BATCH_URL,
            data=body,
            headers={**CROWD_HEADERS, "Content-Type": "application/json"},
        )
        with self._opener.open(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        if payload.get("code") != 0:
            raise RuntimeError(f"小米众筹列表返回异常: {payload}")
        return (payload.get("data") or {}).get("result_list") or []

    def _crowd_items(self) -> list[Item]:
        """众筹好物（开售）+ 众筹新品（预约）。"""
        page = retry_call(
            self._crowd_page,
            attempts=3,
            delay_s=5.0,
            label="小米众筹页面配置",
        )
        goods_q: list[dict] = []
        reserve_q: list[dict] = []
        for floor in page.get("floors") or []:
            queries = floor.get("query_list") or []
            if floor.get("module_key") == "mishop_crowd_list":
                goods_q += queries
            elif floor.get("module_key") == "mishop_crowd_reserve":
                reserve_q += queries

        items: list[Item] = []
        if goods_q:
            for res in retry_call(
                lambda: self._crowd_batch(goods_q),
                attempts=3,
                delay_s=5.0,
                label="小米众筹好物",
            ):
                for entry in res.get("list") or []:
                    item = self._crowd_item(entry)
                    if item is not None:
                        items.append(item)
        if reserve_q:
            for res in retry_call(
                lambda: self._crowd_batch(reserve_q),
                attempts=3,
                delay_s=5.0,
                label="小米众筹预约",
            ):
                for comp in res.get("components") or []:
                    item = self._crowd_reserve_item(comp)
                    if item is not None:
                        items.append(item)
        return items

    def _crowd_item(self, entry: dict) -> Item | None:
        goods = entry.get("goodsInfo") or {}
        project = entry.get("projectInfo") or {}
        project_id = str(project.get("projectId") or "").strip()
        if not project_id:
            return None

        name = str(goods.get("name") or "").strip() or f"众筹项目 {project_id}"
        link = CROWD_DETAIL.format(project_id=project_id)
        published = _datetime_of(project.get("startTime"))
        price = str(goods.get("crowdPrice") or "").strip()
        price_text = f"众筹价 ¥{price}" if price else ""
        if price_text and goods.get("multiPrice"):
            price_text += " 起"

        summary = str(goods.get("summary") or "").strip()
        img = str(goods.get("img800s") or goods.get("imgTransparent") or "").strip()

        parts: list[str] = []
        if img:
            parts.append(
                f'<p><img src="{_esc(img)}" alt="" style="max-width:100%"></p>'
            )
        if price_text:
            parts.append(f"<p><strong>{_esc(price_text)}</strong></p>")
        if summary:
            parts.append(f"<p>{_esc(summary)}</p>")

        stats = [
            str(project.get("reaiseAmount") or "").strip(),
            (f"{project.get('supportNumber')}支持"
             if project.get("supportNumber") else ""),
            (f"进度 {project.get('progress')}%"
             if project.get("progress") else ""),
        ]
        stats = [s for s in stats if s]
        if stats:
            parts.append(
                f'<p style="color:#666;font-size:90%">{_esc(" · ".join(stats))}</p>'
            )

        end = _datetime_of(project.get("endTime"))
        if published or end:
            span = " ~ ".join(
                f"{d:%Y-%m-%d %H:%M}" for d in (published, end) if d
            )
            parts.append(
                f'<p style="color:#666;font-size:90%">众筹时间 {_esc(span)}</p>'
            )
        parts.append(f'<p><a href="{_esc(link)}">查看众筹项目</a></p>')

        return Item(
            id=f"mi:crowd:{project_id}",
            title=f"众筹 · {name}",
            link=link,
            author="小米商城",
            published=published,
            content="".join(parts),
            extra={
                "kind": "crowdfunding",
                "project_id": project_id,
                "item_id": str(goods.get("itemId") or ""),
                "price": price,
                "thumb": img,
            },
        )

    def _crowd_reserve_item(self, comp: dict) -> Item | None:
        goods = comp.get("goodsInfo") or {}
        act_id = str(comp.get("actId") or "").strip()
        if not act_id:
            return None

        name = (
            str(goods.get("productName") or "").strip()
            or str(comp.get("taskName") or "").strip()
            or f"众筹预约 {act_id}"
        )
        start = _datetime_of(comp.get("startTime"))
        end = _datetime_of(comp.get("endTime"))

        try:
            reserved = int(comp.get("reservedNumber") or 0)
        except (TypeError, ValueError):
            reserved = 0

        short = str(goods.get("shortTitle") or "").strip()
        img = str(goods.get("img800Url") or "").strip()

        parts: list[str] = []
        if img:
            parts.append(
                f'<p><img src="{_esc(img)}" alt="" style="max-width:100%"></p>'
            )
        if reserved:
            parts.append(f"<p><strong>已预约 {reserved:,} 人</strong></p>")
        if short:
            parts.append(f"<p>{_esc(short)}</p>")

        span = " ~ ".join(f"{d:%Y-%m-%d %H:%M}" for d in (start, end) if d)
        meta = [f"预约时间 {span}"] if span else []
        now = datetime.now(TZ)
        if start and now < start:
            meta.append("未开始")
        elif end and now > end:
            meta.append("已结束")
        else:
            meta.append("预约中")
        parts.append(
            f'<p style="color:#666;font-size:90%">{_esc(" · ".join(meta))}</p>'
        )
        parts.append(f'<p><a href="{_esc(CROWD_HOME)}">打开小米众筹</a></p>')

        return Item(
            id=f"mi:crowd-reserve:{act_id}",
            title=f"众筹预约 · {name}",
            link=CROWD_HOME,
            author="小米商城",
            published=start,
            content="".join(parts),
            extra={
                "kind": "crowd_reserve",
                "act_id": act_id,
                "product_id": str(goods.get("productId") or ""),
                "reserved": reserved,
                "thumb": img,
            },
        )

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

        pages = 0
        calendar_new = 0
        for page in range(1, self.max_pages + 1):
            data = retry_call(
                lambda p=page: self._request(p),
                attempts=3,
                delay_s=5.0,
                label="小米商城上新日历",
            )
            pages += 1
            page_items = self._parse(data)
            new_on_page = collect(page_items)
            calendar_new += new_on_page

            if not page_items:
                break
            # 整页都是已有记录就停：增量轮次通常第一页就结束。
            if known_ids and new_on_page == 0:
                break

        crowd_new = 0
        crowd_note = "已关闭"
        if self.crowdfunding:
            try:
                crowd_new = collect(self._crowd_items())
                crowd_note = f"{crowd_new} 条"
            except Exception as e:
                # 众筹是加在同一份 feed 里的第二段，它挂了不该让上新日历也发不出去
                crowd_note = f"抓取失败（{e}）"
                print(f"    [xiaomi] 众筹抓取失败：{e}", file=sys.stderr)

        print(
            f"    [xiaomi] 上新日历 {pages} 页 / {calendar_new} 个新品；"
            f"众筹 {crowd_note}",
            file=sys.stderr,
        )
        return out
