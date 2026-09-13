#!/usr/bin/env bash
# 常驻一个自用的 WebSub hub，让 FreshRSS 能立刻收到 feed 更新（不用等它轮询）。
#
# 端口/绑定地址用环境变量覆盖（pm2 在 ecosystem.config.js 里设）：
#   HUB_PORT  默认 8667
#   HUB_HOST  默认 127.0.0.1（只本机）
#
# 和 feed 服务是同一套路：本地绑 127.0.0.1，再用
#   tailscale serve --bg --tcp=8667 8667
# 转发到 tailnet —— 这样 Tailscale 没连上时也不会因为 bind 失败而起不来。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# cron/launchd/pm2 的 PATH 极简，python3 可能找不到
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

export HUB_PORT="${HUB_PORT:-8667}"
export HUB_HOST="${HUB_HOST:-127.0.0.1}"

# exec：让 python 直接接管这个进程，pm2 的信号才能正确传下去
exec python3 "$SCRIPT_DIR/rss-hub.py"
