# -*- coding: utf-8 -*-
"""查证：#27/#42（G08/G12 的 body.status）为什么只生成了 type_match、没生成 contained_by？

对照 #46（G15 的 body.event）**同款枚举却生成了**。只读。
"""

import pathlib
import sys

BASE = pathlib.Path(r"D:\tester")
sys.path.insert(0, str(BASE))

from interfacetester_ai.assertions import derive_assertions  # noqa: E402

for name in ("order_create.md", "update_order_status.md", "webhook_notify.md"):
    text = (BASE / "bench" / "golden" / name).read_text(encoding="utf-8", errors="replace")
    derived, skipped = derive_assertions(text)
    print(f"\n=== {name} ===")
    hits = [item for item in derived if "status" in item.check or "event" in item.check]
    print("  推导出的相关断言:")
    for item in hits:
        print(f"    {item.check:24} {item.comparator:20} expect={item.expect!r}  引用句={item.source_quote[:46]!r}")
    print("  相关跳过原因:")
    for note in skipped:
        if "status" in note or "event" in note:
            print(f"    - {note[:150]}")
