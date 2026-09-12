#!/usr/bin/env bash
# local_rss 定时刷新入口（给 launchd / cron / pm2 用）。
#
# 和直接跑 `python3 rss.py` 的区别：
#   1. 补上 cron/launchd 里缺失的 PATH
#   2. 先确认 WebBridge daemon 在跑，没跑就拉起来
#   3. 浏览器扩展没连上时给出明确原因（而不是一句看不懂的报错）
#
# 标签页清理由 rss.py 自己负责（结束时 close_session），这里不用管。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"
WEBBRIDGE_BIN="${HOME}/.kimi-webbridge/bin/kimi-webbridge"

# cron/launchd 的 PATH 极简，python3 可能找不到
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

if [ ! -x "$WEBBRIDGE_BIN" ]; then
  log "错误：找不到 $WEBBRIDGE_BIN"
  log "请先安装 Kimi WebBridge：https://www.kimi.com/products/kimi-webbridge"
  exit 1
fi

wb_status() { "$WEBBRIDGE_BIN" status 2>/dev/null || echo '{"running":false}'; }

ensure_webbridge() {
  local status
  status=$(wb_status)

  if ! printf '%s' "$status" | grep -q '"running":true'; then
    log "WebBridge daemon 未运行，正在启动..."
    "$WEBBRIDGE_BIN" start >/dev/null 2>&1 || true
    local i
    for i in $(seq 1 15); do
      sleep 1
      wb_status | grep -q '"running":true' && break
    done
  fi

  status=$(wb_status)
  if ! printf '%s' "$status" | grep -q '"running":true'; then
    log "错误：WebBridge daemon 启动失败"
    return 1
  fi
  if ! printf '%s' "$status" | grep -q '"extension_connected":true'; then
    log "错误：浏览器扩展未连接 —— 浏览器没打开，或扩展未启用"
    return 1
  fi
  return 0
}

ensure_webbridge || exit 1

# 透传参数：run.sh zhihu-kvxjr369f 等
"$PYTHON" "$SCRIPT_DIR/rss.py" "$@"
ec=$?

if [ "$ec" -ne 0 ]; then
  log "刷新失败（退出码 ${ec}）"
fi
exit "$ec"
