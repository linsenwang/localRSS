"""命令行入口：读 config.yaml，逐个源抓取并生成 RSS。"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import feed as feed_mod
from . import notify
from . import providers
from . import websub
from .bridge import Bridge, BridgeError, StopRequested, check_bridge
from .config import Config, load_config
from .store import Store

DEFAULT_CONFIG = "config.yaml"


def _setup_signals(stop: threading.Event) -> None:
    """SIGINT/SIGTERM → 先记个标记，等当前这次 WebBridge 调用收尾再退出。

    不直接在 handler 里抛 KeyboardInterrupt，是因为信号往往正好打在某个调用
    中途（定时任务最明显：pm2 的 cron_restart 正好在进程刚起来那一下发信号）。
    那次调用被丢下不管，浏览器那边照样会把标签页开出来，但 daemon 没把它记进
    session —— close_session 之后永远够不着，就成了得手动关的残留页面。
    详见 README「标签页清理」。

    连发两次信号才强制退出（恢复系统默认行为），免得哪次调用真卡住时杀不掉。
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(signum, _frame):
        if stop.is_set():
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
            return
        stop.set()
        print(
            f"[停止] 收到信号 {signum}：等当前这步 WebBridge 调用做完就收尾"
            "（再发一次信号可立即强制退出）",
            file=sys.stderr,
        )

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def _mute_signals() -> None:
    """收尾期间忽略 SIGINT/SIGTERM：清理就几秒，不能被第二个信号打断（标签页会漏）。"""
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_IGN)


def _tab_label(tab: dict) -> str:
    """标签页在日志里怎么显示：优先 URL，没有就退到标题。"""
    return str(tab.get("url") or tab.get("title") or "?")


def _feed_url(cfg: Config, feed_cfg) -> str:
    """feed 的订阅地址 —— 阅读器订阅用它，WebSub 通知 hub 时也用同一个。"""
    if cfg.base_url:
        return f"{cfg.base_url}/{feed_cfg.filename}"
    return feed_cfg.filename


def _notify_hub(feed_cfg, cfg: Config) -> bool:
    """ping 一次 hub，告诉它这个 feed 更新了。返回是否通知成功。

    没配 hub 就什么都不做（返回 True，不算失败）。通知失败也**不影响**抓取结果，
    只是打条日志 —— hub 挂了不该让整个定时任务算失败。
    """
    hub = feed_cfg.hub_url or cfg.hub_url
    if not hub:
        return True

    url = _feed_url(cfg, feed_cfg)
    if "://" not in url:
        print(
            f"[{feed_cfg.id}] WebSub 未通知：feed 地址不是完整 URL，"
            "请在 config.yaml 的 output.base_url 里填订阅地址",
            file=sys.stderr,
        )
        return False

    try:
        code = websub.ping(hub, url)
    except websub.WebSubError as e:
        print(f"[{feed_cfg.id}] WebSub 通知失败：{e}", file=sys.stderr)
        return False

    print(f"[{feed_cfg.id}] 已通知 hub（HTTP {code}）：{url}")
    return True


def _write_dropped_log(
    log_path: Path,
    feed_cfg,
    total: int,
    visible: int,
    records: list[tuple],
) -> None:
    """把本轮被过滤掉的条目写成一份清单，**覆盖**上一轮的（不追加）。

    每次运行都写，哪怕一条没丢 —— 否则上一轮的内容会一直留在文件里冒充本轮结果。
    条目是按「原因 | 时间 | 标题 | 链接」一行一条，方便 grep 和 diff。
    """
    lines = [
        f"# 过滤清单：每次运行覆盖写（生成于 {datetime.now():%Y-%m-%d %H:%M:%S}）",
        f"# feed = {feed_cfg.id} ({feed_cfg.type})  "
        f"本轮 {total} 条 → 可见 {visible} 条，过滤掉 {len(records)} 条",
        "# 原因 | 时间 | 标题 | 链接",
    ]
    for item, reason in records:
        when = item.published.strftime("%Y-%m-%d %H:%M") if item.published else "-"
        title = " ".join((item.title or "").split()) or "(无标题)"
        lines.append(f"{reason} | {when} | {title} | {item.link or '-'}")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_feed(feed_cfg, cfg: Config, bridge: Bridge, force: bool = False) -> dict:
    provider = providers.create(
        feed_cfg.type,
        feed_cfg.options,
        {
            # 全局关键词 + 该源自己的关键词（feed 级的只影响这个源）
            "exclude_keywords": cfg.exclude_keywords + feed_cfg.exclude_keywords,
        },
    )

    print(f"[{feed_cfg.id}] 打开页面 {provider.page_url()}")
    provider.setup(bridge)

    store = Store(cfg.state_dir / f"{feed_cfg.id}.json",
                  history=feed_cfg.history or cfg.history)
    # --force：假装本地什么都没有，翻满 max_pages 重抓一遍。
    # 但仍然合并进原有 state（不是清空），所以知乎全文这类补全过的内容不会丢。
    # before_ids 是本轮之前 state 里已有的 id，用来算「筛选之后真正新增了几条」
    # （见下面 new_visible）—— force 时也要留着，不然通知里会谎报全量。
    before_ids = store.known_ids()
    known = set() if force else before_ids

    items = provider.fetch(bridge, known)
    merged, added = store.merge(items)

    # 跨窗口的去重指纹：窗口内的对比靠条目本身，窗口外的靠这份表
    # （条目会被 history 裁掉，表不会 —— 见 providers/zhihu.py 的 _dedupe_by_content）
    provider.seen_content = store.seen_content()
    # 剔掉被新版本取代的旧条目（知乎的编辑，见 Provider.prune_superseded）。
    # 放在 postprocess 之前：这些条目在站点上已经不存在了，不该再走一遍过滤/补全。
    merged = provider.prune_superseded(merged)
    # 过滤/去重/补全放在合并之后：改规则时存量条目也会被重新筛一遍
    visible = provider.postprocess(merged, bridge)
    # provider 填了新表才覆盖（None = 这个 provider 不玩跨窗口去重）
    if provider.published_content is not None:
        store.set_seen_content(provider.published_content)
    # 「过滤掉」只算真被规则丢掉的：被收进图片合集的条数不单条出现，但不是丢了信息
    grouped = provider.grouped_items
    dropped = len(merged) - len(visible) - grouped

    # 通知里要报的数字：筛选/合集之后**真正进了 RSS 的新条目数**。
    # 不能拿 added（那是抓到的原始条数）：被关键词过滤掉的不算，被并进合集的
    # 单条也不单算（合集自己是一条新条目，会正常算进来）。
    new_visible = sum(1 for item in visible if item.id not in before_ids)

    # 过滤清单：丢了哪些、为什么丢，写一份到 logs/<id>.dropped.log（覆盖写）。
    # 这份文件只为人看，跟 RSS/state 无关，失败也不该让整轮算失败。
    dropped_log_path = ""
    if cfg.dropped_log:
        dropped_log_path = str(cfg.log_dir / f"{feed_cfg.id}.dropped.log")
        try:
            _write_dropped_log(
                Path(dropped_log_path), feed_cfg, len(merged), len(visible),
                provider.dropped_items,
            )
        except OSError as e:
            dropped_log_path = ""
            print(f"[{feed_cfg.id}] 过滤清单写入失败：{e}", file=sys.stderr)

    # postprocess 会就地补全条目（比如知乎全文），要把结果落盘，
    # 否则下次运行还得重抓一遍。注意存的是 merged（全量），不是 visible。
    store.save(merged)

    output_path = cfg.output_dir / feed_cfg.filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # feed 的订阅地址：既是 RSS 里的 <atom:link rel="self">，也是 WebSub 通知 hub 的 topic
    feed_url = _feed_url(cfg, feed_cfg)
    output_path.write_text(
        feed_mod.build_rss(
            title=feed_cfg.title,
            link=feed_cfg.site_url or provider.page_url(),
            description=feed_cfg.description,
            items=visible,
            self_url=feed_url,
            hub_url=feed_cfg.hub_url or cfg.hub_url,
        ),
        encoding="utf-8",
    )
    return {
        "path": str(output_path),
        "total": len(visible),
        "added": added,
        "new_visible": new_visible,
        "dropped": dropped,
        "grouped": grouped,
        "derived": provider.derived_items,
        "dropped_log": dropped_log_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rss.py",
        description="用 Kimi WebBridge 抓取动态并生成 RSS",
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="配置文件路径")
    parser.add_argument("feeds", nargs="*", help="只处理这些源 id（默认全部）")
    parser.add_argument("--list", action="store_true", help="列出配置里的源")
    parser.add_argument("--list-types", action="store_true", help="列出已支持的源类型")
    parser.add_argument(
        "--keep-tabs",
        action="store_true",
        help="运行结束后保留浏览器标签页（默认会关掉本次打开的）",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="只清理本 session 遗留的浏览器标签页，不抓取",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制刷新：忽略本地缓存翻满 max_pages 重抓重渲（不清空 state）",
    )
    parser.add_argument(
        "--ping",
        action="store_true",
        help="只 ping hub 通知这些源有更新，不抓取（不占用浏览器，用来验证 WebSub 链路）",
    )
    args = parser.parse_args(argv)

    # 停止信号先只置位，等当前这次 WebBridge 调用收尾再中断（见 _setup_signals）。
    stop = threading.Event()
    _setup_signals(stop)

    if args.list_types:
        for name, cls in sorted(providers.available_types().items()):
            print(f"{name:12} {cls.description}")
        return 0

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2

    if args.list:
        for f in cfg.feeds:
            mark = " " if f.enabled else "x"
            print(f"[{mark}] {f.id:24} type={f.type:10} {f.title}")
        return 0

    if args.clean:
        bridge = Bridge(
            url=cfg.bridge_url, session=cfg.session, browser=cfg.browser,
            stop=stop.is_set,
        )
        try:
            check_bridge(bridge)
        except BridgeError as e:
            print(str(e), file=sys.stderr)
            return 3
        except StopRequested:
            print("[停止] 清理刚开始就被中断信号打断，本次什么也没关。", file=sys.stderr)
            return 130
        closed, left = bridge.close_session()
        print(f"[清理] session {cfg.session!r} 关闭了 {closed} 个标签页")
        # 关不掉的（没记进 session 的孤儿标签页）得让用户知道，别以为清干净了
        for tab in left:
            print(f"[清理] 没关掉，需要手动关：{_tab_label(tab)}", file=sys.stderr)
        return 0

    try:
        feeds = cfg.enabled_feeds(args.feeds)
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        return 2

    if not feeds:
        print("没有需要处理的源。", file=sys.stderr)
        return 1

    # --ping：只通知 hub，不抓取。不需要 WebBridge，所以放在创建 bridge 之前。
    if args.ping:
        if not any(f.hub_url or cfg.hub_url for f in feeds):
            print(
                "没有配置 hub（config.yaml 的 output.hub_url），无从通知。",
                file=sys.stderr,
            )
            return 2
        failed = 0
        for feed_cfg in feeds:
            output_path = cfg.output_dir / feed_cfg.filename
            if not output_path.exists():
                # feed 文件都没有，hub 抓过去只会 404，别白白发通知
                print(f"[{feed_cfg.id}] 跳过：{output_path} 还不存在", file=sys.stderr)
                continue
            if not _notify_hub(feed_cfg, cfg):
                failed += 1
        return 1 if failed else 0

    bridge = Bridge(
        url=cfg.bridge_url, session=cfg.session, browser=cfg.browser,
        stop=stop.is_set,
    )
    try:
        check_bridge(bridge)
    except BridgeError as e:
        print(str(e), file=sys.stderr)
        return 3
    except StopRequested:
        print("[停止] 启动阶段收到停止信号，本次不抓取。", file=sys.stderr)
        return 130

    close_tabs = cfg.close_session and not args.keep_tabs
    failed = 0
    interrupted = False
    #: 本轮有新内容的源，抓完汇总成一条 macOS 通知（见文件末尾）
    news: list[str] = []
    try:
        for feed_cfg in feeds:
            # 停止信号在两次调用之间生效：要么整源跳过，要么由 bridge 在源内部抛出
            if stop.is_set():
                raise StopRequested()
            try:
                r = _run_feed(feed_cfg, cfg, bridge, force=args.force)
                notes = f"，过滤掉 {r['dropped']} 条" if r["dropped"] else ""
                if r["grouped"]:
                    notes += f"，{r['grouped']} 条并入合集"
                print(f"[{feed_cfg.id}] 新增 {r['added']} 条，共 {r['total']} 条{notes} -> {r['path']}")
                if r["dropped_log"] and r["dropped"]:
                    print(f"[{feed_cfg.id}] 过滤清单（覆盖写）-> {r['dropped_log']}")
                # 有新条目才通知 hub —— hub 收到就来抓 feed 并推给订阅者。
                # 没有新内容那种常见运行就不打扰它了（通知失败不影响抓取结果）。
                # derived：本轮没抓到新动态，但 postprocess 自己造了条目（比如攒够
                # 或过期后兜底合成的图片合集），RSS 也确实变了，同样要通知。
                new_count = r["added"] + r["derived"]
                if new_count:
                    _notify_hub(feed_cfg, cfg)
                    # 通知里报的是筛选/合集之后真正进 RSS 的条数（new_visible），
                    # 不是抓到的原始条数（added）—— 上面那行「新增」是原始条数
                    if r["new_visible"]:
                        name = feed_cfg.title or feed_cfg.id
                        news.append(f"{name} +{r['new_visible']}")
            except StopRequested:
                raise
            except Exception as e:
                failed += 1
                print(f"[{feed_cfg.id}] 失败: {e}", file=sys.stderr)
    except (KeyboardInterrupt, StopRequested):
        # 被 pm2 cron_restart / Ctrl-C / kill 打断（关机、手动重跑等）：不打 traceback
        # 污染日志，但仍然走下面的清理。KeyboardInterrupt 还留着兜底 —— 信号 handler
        # 之外（比如非主线程）抛出来的中断也得收干净。
        interrupted = True
        print("[中断] 收到停止信号，正在清理本次打开的标签页...", file=sys.stderr)
    finally:
        # 收尾：只关掉本 session（本次运行）打开的标签页，用户自己的页面不受影响；
        # daemon 不在线时静默跳过。清理期间忽略信号 —— 第二个信号（pm2 等不及时的
        # SIGKILL 之前那一发）不该把清理打断，否则标签页就漏在浏览器里了。
        if close_tabs:
            _mute_signals()
            try:
                closed, left = bridge.close_session()
            finally:
                # 收完把 handler 装回去：此时 stop 已置位，再收到信号就是强制退出 ——
                # 别让后面发通知（osascript，最长 15s）这段没人杀得动。
                _setup_signals(stop)
            if closed:
                print(f"[清理] 已关闭本次打开的 {closed} 个浏览器标签页")
            for tab in left:
                print(f"[清理] 没关掉，需要手动关：{_tab_label(tab)}", file=sys.stderr)

    if interrupted:
        return 130
    # 有新内容就发一条 macOS 通知（桌面上一眼看到，不用翻日志）。
    # 汇总成一条：五个源各发一条会刷屏。中断的那次不发 —— 结果不完整。
    if cfg.notify and news:
        subtitle = f"{len(news)} 个源有新内容"
        body = " · ".join(news)
        if notify.send("local_rss", body, subtitle):
            print(f"[通知] 已弹出 macOS 通知：{subtitle}（{body}）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
