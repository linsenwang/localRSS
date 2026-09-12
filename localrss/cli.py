"""命令行入口：读 config.yaml，逐个源抓取并生成 RSS。"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from . import feed as feed_mod
from . import providers
from .bridge import Bridge, BridgeError, check_bridge
from .config import Config, load_config
from .store import Store

DEFAULT_CONFIG = "config.yaml"


def _run_feed(feed_cfg, cfg: Config, bridge: Bridge, force: bool = False) -> tuple[str, int, int, int]:
    provider = providers.create(
        feed_cfg.type, feed_cfg.options, {"exclude_keywords": cfg.exclude_keywords}
    )

    print(f"[{feed_cfg.id}] 打开页面 {provider.page_url()}")
    provider.setup(bridge)

    store = Store(cfg.state_dir / f"{feed_cfg.id}.json", history=cfg.history)
    # --force：假装本地什么都没有，翻满 max_pages 重抓一遍。
    # 但仍然合并进原有 state（不是清空），所以知乎全文这类补全过的内容不会丢。
    known = set() if force else store.known_ids()

    items = provider.fetch(bridge, known)
    merged, added = store.merge(items)

    # 过滤/去重/补全放在合并之后：改规则时存量条目也会被重新筛一遍
    visible = provider.postprocess(merged, bridge)
    dropped = len(merged) - len(visible)

    # postprocess 会就地补全条目（比如知乎全文），要把结果落盘，
    # 否则下次运行还得重抓一遍。注意存的是 merged（全量），不是 visible。
    store.save(merged)

    output_path = cfg.output_dir / feed_cfg.filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    self_url = (
        f"{cfg.base_url}/{feed_cfg.filename}" if cfg.base_url else feed_cfg.filename
    )
    output_path.write_text(
        feed_mod.build_rss(
            title=feed_cfg.title,
            link=feed_cfg.site_url or provider.page_url(),
            description=feed_cfg.description,
            items=visible,
            self_url=self_url,
        ),
        encoding="utf-8",
    )
    return str(output_path), len(visible), added, dropped


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
    args = parser.parse_args(argv)

    # 关机 / pm2 stop / kill 默认发 SIGTERM，Python 默认会直接终止、
    # 不执行 finally，标签页就漏了。转成 KeyboardInterrupt 走统一的清理路径。
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

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
        bridge = Bridge(url=cfg.bridge_url, session=cfg.session)
        try:
            check_bridge(bridge)
        except BridgeError as e:
            print(str(e), file=sys.stderr)
            return 3
        closed = bridge.close_session()
        print(f"[清理] session {cfg.session!r} 关闭了 {closed} 个标签页")
        return 0

    try:
        feeds = cfg.enabled_feeds(args.feeds)
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        return 2

    if not feeds:
        print("没有需要处理的源。", file=sys.stderr)
        return 1

    bridge = Bridge(url=cfg.bridge_url, session=cfg.session)
    try:
        check_bridge(bridge)
    except BridgeError as e:
        print(str(e), file=sys.stderr)
        return 3

    close_tabs = cfg.close_session and not args.keep_tabs
    failed = 0
    interrupted = False
    try:
        for feed_cfg in feeds:
            try:
                path, total, added, dropped = _run_feed(
                    feed_cfg, cfg, bridge, force=args.force
                )
                suffix = f"，过滤掉 {dropped} 条" if dropped else ""
                print(f"[{feed_cfg.id}] 新增 {added} 条，共 {total} 条{suffix} -> {path}")
            except Exception as e:
                failed += 1
                print(f"[{feed_cfg.id}] 失败: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        # 被 pm2 restart / Ctrl-C 打断（关机、手动重跑等）：不打 traceback 污染日志，
        # 但仍然走下面的清理。
        interrupted = True
        print("[中断] 收到停止信号，正在清理本次打开的标签页...", file=sys.stderr)
    finally:
        # 收尾：只关掉本 session（本次运行）打开的标签页，用户自己的页面不受影响；
        # daemon 不在线时静默跳过。清理期间再收到信号也照做（吞掉，别让 finally 中途退出）。
        if close_tabs:
            try:
                closed = bridge.close_session()
            except BaseException:
                closed = 0
            if closed:
                print(f"[清理] 已关闭本次打开的 {closed} 个浏览器标签页")

    if interrupted:
        return 130
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
