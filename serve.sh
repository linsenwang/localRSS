#!/usr/bin/env bash
# 常驻一个只读 HTTP 服务，把 output/ 里的 RSS 暴露给阅读器。
#
# 端口/绑定地址用环境变量覆盖（pm2 在 ecosystem.config.js 里设）：
#   PORT  默认 8666
#   HOST  默认 127.0.0.1（只本机可访问；想让手机/平板订阅就改成 0.0.0.0）
#
# 输出目录里的 XML 是每次抓取时覆盖重写的，这里直接读同一个目录，
# 所以刷新后订阅者拿到的是最新内容，不需要重启这个服务。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8666}"
HOST="${HOST:-127.0.0.1}"
DIR="${RSS_OUTPUT_DIR:-$SCRIPT_DIR/output}"

# cron/launchd/pm2 的 PATH 极简，python3 可能找不到
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

mkdir -p "$DIR"

echo "[serve] http://${HOST}:${PORT}/  ->  ${DIR}"
ls -1 "$DIR"/*.xml 2>/dev/null | while read -r f; do
  echo "[serve]   http://${HOST}:${PORT}/$(basename "$f")"
done

# exec：让 python 直接接管这个进程，pm2 的信号才能正确传下去
exec python3 -m http.server "$PORT" --bind "$HOST" --directory "$DIR"
