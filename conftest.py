# -*- coding: utf-8 -*-
"""pytest 也支持从 src/ 导入（与 unittest 用法保持一致）."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
