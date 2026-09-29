# -*- coding: utf-8 -*-
"""兼容壳：实现已整体迁入 `interfacetester_ai.projection`（v8 §十 T6，2026-09-23）。

保留两个入口的兼容：
  1. `from bench.normalize_projection import …`（tests 既有 import 不动）；
  2. `python bench/normalize_projection.py`（CLI 转发，与
     `python -m interfacetester_ai.projection` 等价）。
单一来源：两处 import 得到的是同一批对象（同一性断言见
`tests/interfacetester_ai_pkg_test.py::TestBenchShims`）。
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.projection import *  # noqa: F401,F403
from interfacetester_ai.projection import (  # noqa: E402,F401  # 显式补齐（* 不含下划线名）
    _sample_summary,
    project_summary,
    run_selftest,
)

if __name__ == "__main__":
    raise SystemExit(run_selftest())
