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
# 定时触发撞上在跑的那次就跳过；手动触发（LOCAL_RSS_TAKEOVER=1，由 main.sh 设）
# 则把在跑的那次干掉接管 —— 详见下面的单实例锁。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"
WEBBRIDGE_BIN="${HOME}/.kimi-webbridge/bin/kimi-webbridge"

# cron/launchd 的 PATH 极简，python3 可能找不到
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# 单实例锁。flock 挂在 fd 9 上，锁会跟着这个 shell 活到脚本退出；锁文件第一
# 行记着拿锁的那个 shell 的 pid（lsof 看不到时用它兜底找在跑的那次）。
#
# 撞上「已经在跑」时怎么办，看是谁触发的：
#   定时触发（pm2 cron_restart / launchd，默认）：跳过。那多半是上一轮还没跑完，
#     硬抢会把它开出来的标签页搅乱。
#   手动触发（main.sh → rss，LOCAL_RSS_TAKEOVER=1）：接管，把在跑的那次干掉。
#     手动多半是因为后台那次卡住了才想起手动跑，不该排在它后面等。
LOCK_FILE="${TMPDIR:-/tmp}/local-rss-run.lock"
TAKEOVER="${LOCAL_RSS_TAKEOVER:-0}"

# 用 >> 打开：不带截断，否则会在拿到锁之前就抹掉在跑那次的 pid。
exec 9>>"$LOCK_FILE"

try_lock() {
  "$PYTHON" -c 'import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(75)' 2>/dev/null
}

# 先子后父地发信号：只杀 run.sh 会把 rss.py 留成孤儿，继续写 state。
kill_tree() {
  local pid="$1" sig="$2" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_tree "$child" "$sig"
  done
  kill -"$sig" "$pid" 2>/dev/null || true
}

# pid 是不是我们自己 fork 出去的：走近路查 ppid 链。lsof 那条路会把「取 pids
# 时 fork 的子 shell」也算成持有者 —— 别回头杀自己人。
is_self_descendant() {
  local p="$1" pp
  while [ "$p" -gt 1 ]; do
    pp="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
    [ -z "$pp" ] && return 1
    [ "$pp" = "$$" ] && return 0
    p="$pp"
  done
  return 1
}

# 占着锁的是谁：所有还活着、打开着锁文件的进程。flock 挂在「打开的文件描述」
# 上，谁的 fd 没关谁就在替那次刷新占着锁 —— run.sh 自己、它 fork 的 rss.py，
# 还有继承了 fd 9 的 WebBridge daemon（见下面 wb_status 的 9>&-）。
# 只认锁文件里记的 pid 不行：shell 一死，rss.py 就被 reparent 到 1，杀父进程
# 根本碰不到它，锁照样收不回来。
lock_holders() {
  local pid found
  found=""
  for pid in $(lsof -t "$LOCK_FILE" 2>/dev/null | sort -u); do
    [ "$pid" = "$$" ] && continue
    # lsof 常把已经退出的临时进程（它自己、sort）也列出来，只留还活着的
    ps -p "$pid" -o command= >/dev/null 2>&1 || continue
    is_self_descendant "$pid" && continue
    found="$found $pid"
  done
  if [ -z "$found" ]; then
    # lsof 看不到时退回锁文件里记的 pid（新版本 run.sh 会写第一行）
    pid="$(head -n1 "$LOCK_FILE" 2>/dev/null | tr -dc '0-9')"
    if [ -n "$pid" ] && ps -p "$pid" -o command= 2>/dev/null | grep -q 'run\.sh'; then
      found=" $pid"
    fi
  fi
  echo "${found# }"
}

# 持有者里还有没有「真的在刷新」的进程。只剩 daemon 这类长命进程时说明锁漏了：
# 它们不会自己退出，守着等就是永远跳过 —— 定时刷新曾经就是这样被堵死的。
has_live_refresh() {
  local pid
  for pid in $1; do
    ps -p "$pid" -o command= 2>/dev/null | grep -qE 'run\.sh|rss\.py' && return 0
  done
  return 1
}

if ! try_lock; then
  holders="$(lock_holders)"

  if [ -z "$holders" ]; then
    log "锁被占用（${LOCK_FILE}）但查不到持有它的进程，跳过本次"
    exit 0
  fi

  if [ "$TAKEOVER" != "1" ] && has_live_refresh "$holders"; then
    log "已有一次刷新在跑（pid ${holders}），跳过本次重复触发"
    exit 0
  fi

  if has_live_refresh "$holders"; then
    log "手动触发，接管正在跑的那次刷新（pid ${holders}）"
  else
    log "锁漏了：占着它的只是继承了 fd 9 的长进程（pid ${holders}），收回"
  fi
  for pid in $holders; do kill_tree "$pid" TERM; done

  # rss.py 收到第一个 SIGTERM 会把手上这步 WebBridge 调用做完再收尾（等一个
  # 45s 的 fetch 也不奇怪），给它 10 秒；还不退就再补一次 SIGTERM —— 第二次
  # 会恢复默认行为立刻退出 —— 然后 SIGKILL。手动触发不该被卡住的那次一直拖着。
  acquired=0
  for _ in $(seq 1 10); do
    try_lock && { acquired=1; break; }
    sleep 1
  done
  if [ "$acquired" -ne 1 ]; then
    # 再扫一遍：这十秒里可能又冒出继承了 fd 9 的进程
    holders="$(lock_holders)"
    log "上次刷新 10 秒内没退出，强制结束（pid ${holders:-?}；它开的标签页可能要 ./main.sh clean 才清得掉）"
    for pid in $holders; do
      kill_tree "$pid" TERM
      kill_tree "$pid" KILL
    done
    for _ in $(seq 1 5); do
      try_lock && { acquired=1; break; }
      sleep 1
    done
  fi
  if [ "$acquired" -ne 1 ]; then
    log "错误：接管失败，锁仍被占用（${LOCK_FILE}）"
    exit 1
  fi
  log "已接管，开始本次刷新"
fi

printf '%s\n' "$$" > "$LOCK_FILE"

if [ ! -x "$WEBBRIDGE_BIN" ]; then
  log "错误：找不到 $WEBBRIDGE_BIN"
  log "请先安装 Kimi WebBridge：https://www.kimi.com/products/kimi-webbridge"
  exit 1
fi

# 调 webbridge 一律带 9>&-：daemon 是长命进程，继承了 fd 9 就会把单实例锁
# 一直占着（它 fork 自己跑 run 子命令，ppid 变 1，锁再也不释放），之后每次都
# 被判成「已有一次刷新在跑」。
wb_status() { "$WEBBRIDGE_BIN" status 2>/dev/null 9>&- || echo '{"running":false}'; }

ensure_webbridge() {
  local status
  status=$(wb_status)

  if ! printf '%s' "$status" | grep -q '"running":true'; then
    log "WebBridge daemon 未运行，正在启动..."
    "$WEBBRIDGE_BIN" start >/dev/null 2>&1 9>&- || true
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
# SwiftBar 偶尔会把主线程卡死，插件不再执行、请求文件就没人消费。这里等 15 秒
# 确认是否处理；没处理就本轮跳过，不再重启 SwiftBar（请求文件会留着，插件恢复
# 后的下一轮定时执行时会读到）。
refresh_netnewswire() {
  local delay="${NNW_REFRESH_DELAY:-0}"
  # SwiftBar 2.1.x 的 refreshplugin 认插件 id（完整路径）或 name（文件名第一个
  # 点之前那段），不认带扩展名的文件名；2.0.1 也认 name，所以这里用 name，
  # 两个版本都能命中。插件改名的话用 NNW_PLUGIN_NAME 覆盖。
  local plugin_name="${NNW_PLUGIN_NAME:-netnewswire_unread}"
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
  else
    log "SwiftBar 没有处理刷新请求，本轮跳过 NetNewsWire 刷新"
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
