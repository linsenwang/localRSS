#!/usr/bin/env bash
# local_rss 本地手动入口。
#
#   ./main.sh                    增量刷新（全部源）
#   ./main.sh force              强制刷新：忽略增量缓存，翻满 max_pages 重抓重渲
#   ./main.sh zhihu-kvxjr369f    只跑指定源（缺省跑 config.yaml 里全部启用的源）
#   ./main.sh force bilibili-follow   两者组合
#   ./main.sh clean              关掉遗留的浏览器标签页（进程被 kill -9 后可能残留）
#   ./main.sh status             看 pm2 进程 / 订阅地址 / 数据概览
#   ./main.sh log [行数]         看最近抓取日志（默认 20 行）
#   ./main.sh restart|stop|start 控制 pm2 里的定时抓取任务
#
# force 和普通刷新的区别：
#   普通刷新是增量的 —— 从第 1 页开始，整页都是已有记录就停止翻页，通常 1 秒结束。
#   force 假装本地什么都没有，翻满 max_pages 把所有页重抓一遍并重新渲染，
#   用于「改了标题/头像/播放器这类渲染逻辑，想让存量条目也更新」的场景。
#   force **不会清空 state**，所以知乎已经抓好的全文不会被冲掉。
#   想看不同过滤器/关键词的效果不需要 force —— 那些规则每次都作用在完整列表上。
#
# 彻底重来（会丢知乎全文，需要重新分批补齐）：
#   rm state/*.json && ./main.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

PORT="$(grep -oE "PORT: *'[0-9]+'" ecosystem.config.js 2>/dev/null | grep -oE '[0-9]+' | head -1)"
PORT="${PORT:-8666}"
APPS="local-rss local-rss-http"
LOG_FILE="$SCRIPT_DIR/logs/pm2-out.log"

# 跟 pm2 相关但只在需要时才要求它存在
need_pm2() {
  command -v pm2 >/dev/null 2>&1 || { echo "找不到 pm2，请先安装：npm i -g pm2"; exit 1; }
}

cmd_status() {
  need_pm2
  echo "== pm2 进程 =="
  pm2 list 2>/dev/null | grep -E "^\│ id|local-rss" || echo "  （没有 local-rss 相关进程，跑 ./main.sh 或 pm2 start ecosystem.config.js）"

  local tsip
  tsip="$(tailscale ip -4 2>/dev/null | head -1)"
  echo
  echo "== 订阅地址 =="
  echo "  本机    http://127.0.0.1:$PORT/<feed-id>.xml"
  [ -n "$tsip" ] && echo "  tailnet http://$tsip:$PORT/<feed-id>.xml"
  if command -v tailscale >/dev/null 2>&1; then
    tailscale serve status 2>/dev/null | head -1 | sed 's/^/  转发    /'
  fi

  echo
  echo "== 输出 =="
  local f n ts
  for f in "$SCRIPT_DIR"/output/*.xml; do
    [ -e "$f" ] || { echo "  （还没有生成任何 feed）"; break; }
    n="$(grep -c '<item>' "$f")"
    ts="$(stat -f '%Sm' -t '%m-%d %H:%M' "$f")"
    printf "  %-28s %4s 条   %s   %s\n" "$(basename "$f")" "$n" "$ts" "$(du -h "$f" | cut -f1)"
  done

  echo
  echo "== 状态 =="
  for f in "$SCRIPT_DIR"/state/*.json; do
    [ -e "$f" ] || break
    python3 - "$f" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding='utf-8'))
items = d.get('items', [])
locked = sum(1 for i in items if (i.get('extra') or {}).get('content_locked'))
extra = f"   全文 {locked} 条" if locked else ""
print(f"  {p.split('/')[-1]:<28} {len(items):>4} 条{extra}   updated {d.get('updated','?')}")
PY
  done

  echo
  echo "== 关键配置 =="
  grep -E "^\s+(cron_restart|PORT|HOST):" ecosystem.config.js | sed 's/^/  /'
}

cmd_log() {
  local n="${1:-20}"
  [ -f "$LOG_FILE" ] || { echo "还没有日志（$LOG_FILE）"; return; }
  tail -n "$n" "$LOG_FILE"
}

cmd_clean() {
  python3 "$SCRIPT_DIR/rss.py" --clean
}

usage() {
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
}

case "${1:-}" in
  "" )        exec "$SCRIPT_DIR/run.sh" ;;
  force)      shift; exec "$SCRIPT_DIR/run.sh" --force "$@" ;;
  clean)      shift; cmd_clean "$@" ;;
  status)     shift; cmd_status "$@" ;;
  log)        shift; cmd_log "$@" ;;
  restart|stop|start)
              need_pm2
              for a in $APPS; do pm2 "$1" "$a" >/dev/null 2>&1 || true; done
              echo "pm2 $1 完成：$APPS"
              pm2 list 2>/dev/null | grep -E "^\│ id|local-rss"
              ;;
  -h|--help|help) usage ;;
  * )         exec "$SCRIPT_DIR/run.sh" "$@" ;;
esac
