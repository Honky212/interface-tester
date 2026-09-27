# -*- coding: utf-8 -*-
"""批次 5 对照：C-2 接线后（新 json） vs 接线前（preC2 备份）。只读。"""

import json
import pathlib

BASE = pathlib.Path(r"D:\tester\probe_p0a_llm")
NEW = BASE / "out_l2_baseline_gemma4-latest.json"
OLD = BASE / "out_l2_baseline_gemma4-latest_preC2.json"

SCALARS = (
    "model",
    "docs",
    "docs_with_cases",
    "cases_produced",
    "l2_passed",
    "l2_pass_rate",
    "slices_total",
    "slices_skipped_non_interface",
    "slices_with_interface",
    "slices_produced",
    "slices_failed",
    "slice_success_rate",
)

new = json.loads(NEW.read_text(encoding="utf-8"))
old = json.loads(OLD.read_text(encoding="utf-8"))

print(f"{'量':32} {'C-2 之前':>14} {'C-2 之后':>14}   变化")
for key in SCALARS:
    before, after = old.get(key), new.get(key)
    flag = "" if before == after else "  ★变了"
    print(f"{key:32} {str(before):>14} {str(after):>14}{flag}")

print("\nsource_tree_touched:")
print("  之前:", old.get("source_tree_touched"))
print("  之后:", new.get("source_tree_touched"))

print("\n逐篇对照（tag / exit / 用例 / 切片 / L2）:")
old_entries = {row.get("tag"): row for row in old.get("entries", [])}
for row in new.get("entries", []):
    tag = row.get("tag")
    prev = old_entries.get(tag, {})
    changed = ""
    if (prev.get("exit"), prev.get("cases") and len(prev["cases"]), prev.get("elapsed_s")) != (
        row.get("exit"),
        row.get("cases") and len(row["cases"]),
        row.get("elapsed_s"),
    ):
        changed = "  ★"
    print(
        f"  {tag:26} exit {str(prev.get('exit')):>4}→{str(row.get('exit')):<4} "
        f"用例 {len(prev.get('cases') or []):>2}→{len(row.get('cases') or []):<2} "
        f"{str(prev.get('elapsed_s')):>7}→{str(row.get('elapsed_s')):<7}s{changed}"
    )

print("\n审计留痕条数：之前", sum(row.get("manifests") or 0 for row in old.get("entries", [])),
      "→ 之后", sum(row.get("manifests") or 0 for row in new.get("entries", [])))
print("\n有产出的用例清单（之后）:")
for row in new.get("entries", []):
    for path in row.get("cases") or []:
        print("   ", pathlib.Path(path).name)
