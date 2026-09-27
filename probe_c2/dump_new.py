# -*- coding: utf-8 -*-
"""只读：把新 json 的原始标量与三个特殊篇目原样打印（不做任何对照计算）。"""

import json
import pathlib

path = pathlib.Path(r"D:\tester\probe_p0a_llm\out_l2_baseline_gemma4-latest.json")
data = json.loads(path.read_text(encoding="utf-8"))

print("== 顶层标量（原样）==")
for key, value in data.items():
    if not isinstance(value, (list, dict)):
        print(f"  {key} = {value!r}")

print("\n== entries[0] 的键 ==")
print(" ", sorted(data["entries"][0].keys()))

print("\n== 特殊篇目（原样）==")
for row in data["entries"]:
    if row.get("tag") in ("G04_delete_session", "G06_legacy_gbk_doc", "G14_utf8_bom_doc"):
        print(" ", json.dumps(row, ensure_ascii=False)[:600])
