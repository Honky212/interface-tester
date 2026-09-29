# -*- coding: utf-8 -*-
"""兼容壳：实现已整体迁入 `interfacetester_ai.gates`（v8 §十 T6，2026-09-23）。

本文件保留两个入口的兼容：
  1. `from bench.silent_traps import …`（tests / probe 既有 import 不动）；
  2. `python bench/silent_traps.py`（CLI 转发，与 `python -m interfacetester_ai.gates` 等价）。

单一来源：两处 import 得到的是**同一批对象**（同一性断言见
`tests/interfacetester_ai_pkg_test.py::TestBenchShims`），不产生双源漂移。
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.gates import *  # noqa: F401,F403
from interfacetester_ai.gates import (  # noqa: E402,F401  # 显式补齐 * 之外的常用入口
    GateReport,
    MUST_ALLOW,
    MUST_REJECT,
    PENDING,
    REJECT,
    SilentTrapRejected,
    assert_testcase_passes,
    check_testcase,
    gate_s1_pseudo_presence,
    gate_s2_lexicographic,
    gate_s3_assertion_vacuum,
    gate_s4_extract_without_assertion,
    gate_s5_quoted_function,
    gate_s6_unknown_comparators,
    gate_s7_type_match_expect,
    gate_s8_inline_schema,
    gate_s9_generatable_and_body,
    gate_s10_module_collision,
    gate_s11_document_encoding,
    kernel_comparator_names,
    main,
    normalize_formatting,
    probe_formatting,
    run_selftest,
)

if __name__ == "__main__":
    raise SystemExit(main())
