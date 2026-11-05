#!/usr/bin/env bash
# local_rss 定时刷新入口（给 launchd / cron / pm2 用）。
#
# 和直接跑 `python3 rss.py` 的区别：
#   1. 补上 cron/launchd 里缺失的 PATH
#   2. 先确认 WebBridge daemon 在跑，没跑就拉起来
#
# 浏览器没窗口（扩展会掉线）不在这里管 —— rss.py 的 check_bridge 会按
# config.yaml 的 bridge.browser 自己把窗口开出来。
# 标签页清理由 rss.py 自己负责（结束时 close_session），这里不用管。
#
# 同一时间只跑一个实例：pm2 的 cron_restart 偶尔会在分钟边界连发两次
# （:01:59 和 :02:00），第二次会和第一次抢 WebBridge 的标签页，整轮都失败。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"
WEBBRIDGE_BIN="${HOME}/.kimi-webbridge/bin/kimi-webbridge"

# cron/launchd 的 PATH 极简，python3 可能找不到
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# 单实例锁。flock 挂在 fd 9 上，锁会跟着这个 shell 活到脚本退出；重复触发
# 的那次拿不到锁就直接走人，别去抢已经在跑的那次开出来的标签页。
LOCK_FILE="${TMPDIR:-/tmp}/local-rss-run.lock"
exec 9>"$LOCK_FILE"
lock_rc=0
"$PYTHON" -c 'import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(75)' 2>/dev/null || lock_rc=$?
if [ "$lock_rc" -eq 75 ]; then
  log "已有一次刷新在跑，跳过本次重复触发"
  exit 0
fi

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
  # 扩展没连上不在这里拦：浏览器没窗口时扩展会掉线，rss.py 会自己开窗口再等它连上
  # （见 config.yaml 的 bridge.browser）。在这里拦住等于不给它修的机会。
  return 0
}

ensure_webbridge || exit 1

# local_rss 写完 feed 并通知 hub 后，FreshRSS 还需要一点时间拉取。
# 等几秒后写一个请求文件，由 SwiftBar 插件去触发 NetNewsWire 刷新。
# 这样定时任务不需要自己拿辅助功能权限，只要 SwiftBar 有权限即可。
#
# SwiftBar 偶尔会把主线程卡死：插件不再执行、连 AppleEvent 退出都不处理，
# 请求文件就永远没人消费。所以这里等一会儿，发现没被消费就重启 SwiftBar 再试。
refresh_netnewswire() {
  local delay="${NNW_REFRESH_DELAY:-0}"
  local plugin_name="${NNW_PLUGIN_NAME:-netnewswire_unread.1m.py}"
  local request_file="${HOME}/Library/Caches/swiftbar-netnewswire.request"

  if ! pgrep -x NetNewsWire >/dev/null 2>&1; then
    log "NetNewsWire 未运行，跳过刷新"
    return 0
  fi

  log "等待 ${delay}s 让 FreshRSS 处理更新..."
  sleep "$delay"

  mkdir -p "$(dirname "$request_file")"

  write_request() { date +%s > "$request_file"; }
  ask_swiftbar() {
    open "swiftbar://refreshplugin?name=${plugin_name}" >/dev/null 2>&1 || true
  }
  # 插件读到请求文件会立刻删掉；文件还在 = SwiftBar 没把插件跑起来。
  wait_consumed() {
    local i
    for i in $(seq 1 "$1"); do
      [ -f "$request_file" ] || return 0
      sleep 1
    done
    return 1
  }

  write_request
  ask_swiftbar
  if wait_consumed 15; then
    log "已请求 SwiftBar 刷新 NetNewsWire"
    return 0
  fi

  log "SwiftBar 15 秒内没有处理刷新请求，可能卡住了，重启一次"
  pkill -x SwiftBar 2>/dev/null || true
  local i
  for i in $(seq 1 10); do
    pgrep -x SwiftBar >/dev/null || break
    sleep 0.5
  done
  if pgrep -x SwiftBar >/dev/null; then
    # 卡死的进程未必响应 SIGTERM，等 5 秒还不退就强杀
    pkill -9 -x SwiftBar 2>/dev/null || true
    sleep 1
  fi
  open -a SwiftBar >/dev/null 2>&1 || true
  sleep 3

  write_request
  ask_swiftbar
  if wait_consumed 15; then
    log "SwiftBar 已重启，NetNewsWire 刷新请求已处理"
  else
    log "重启 SwiftBar 后仍没处理刷新请求，本轮跳过 NetNewsWire 刷新"
  fi
}

# 透传参数：run.sh zhihu-kvxjr369f 等
"$PYTHON" "$SCRIPT_DIR/rss.py" "$@"
ec=$?

# 退出码 1 = 部分源失败，但其余源已经写完、hub 也通知过了。这种也要让阅读器
# 刷新 —— 否则一个源抽风就会把整轮更新挡在阅读器外面。
if [ "$ec" -eq 0 ] || [ "$ec" -eq 1 ]; then
  refresh_netnewswire
else
  log "抓取未完成（退出码 ${ec}），跳过 NetNewsWire 刷新"
fi
exit "$ec"
