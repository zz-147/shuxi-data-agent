# -*- coding: utf-8 -*-
"""测试包：把 src/ 加入 sys.path，保证不安装也能 `python -m unittest tests.test_xxx`."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
