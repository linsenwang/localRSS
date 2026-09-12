#!/usr/bin/env python3
"""local_rss 入口。

    python3 rss.py                 # 处理 config.yaml 里的全部源
    python3 rss.py zhihu-kvxjr369f # 只处理指定源
    python3 rss.py --list          # 列出源
    python3 rss.py --list-types    # 列出已支持的站点类型
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from localrss.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
