#!/usr/bin/env python3
"""local_rss 自用的 WebSub hub 入口。

    ./hub.sh                       # 一般由 pm2 拉起（local-rss-hub）
    python3 rss-hub.py             # 手动起，默认 127.0.0.1:8667
    python3 rss-hub.py -p 9000 -b 0.0.0.0

订阅情况直接开 http://127.0.0.1:8667/ 就能看。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from localrss.hub import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
