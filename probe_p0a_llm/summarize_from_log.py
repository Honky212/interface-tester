# -*- coding: utf-8 -*-
"""后处理：从 `gen_all.log` 重算"片"层面的口径，合并进 `out_l2_baseline.json`。

★为什么要单独一个后处理：第一次跑用的正则是 `片产出一用例`，而实际文案是
  `片产出用例`（多了一个"一"）→ 那一项全 None。**不重跑模型**（那是钱和时间），
  只从已存日志里重算——日志是同一份证据，解析器错了就修解析器。
"""
import json
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.environ.get("P0A_MODEL", "gemma4:latest")
SLUG = re.sub(r"[^A-Za-z0-9._-]+", "-", MODEL).strip("-")
LOG = os.path.join(BASE, "probe_p0a_llm", f"gen_all_{SLUG}.log")
JSON = os.path.join(BASE, "probe_p0a_llm", f"out_l2_baseline_{SLUG}.json")

SLICES = re.compile(r"(\d+)/(\d+)\s*片产出(?:用例|草稿)")
SKIPPED = re.compile(r"另有\s*(\d+)\s*片")
FAILED = re.compile(r"(\d+)\s*片失败")

with open(LOG, encoding="utf-8") as fp:
    text = fp.read()

# 按 `===== <tag> =====` 切段（每段 = 一篇文档的一次 `gen`）
sections = re.split(r"\n===== (\S+) ===== ", text)
parsed = {}
for index in range(1, len(sections), 2):
    tag = sections[index]
    body = sections[index + 1]
    slices_match = None
    for candidate in SLICES.finditer(body):
        slices_match = candidate  # 取最后一条（汇总行在末尾）
    parsed[tag] = {
        "slices_produced": int(slices_match.group(1)) if slices_match else None,
        "slices_total": int(slices_match.group(2)) if slices_match else None,
        "slices_skipped": int(SKIPPED.search(body).group(1)) if SKIPPED.search(body) else None,
        "slices_failed": int(FAILED.search(body).group(1)) if FAILED.search(body) else None,
    }

with open(JSON, encoding="utf-8") as fp:
    report = json.load(fp)

for entry in report["entries"]:
    patch = parsed.get(entry["tag"]) or {}
    for key, value in patch.items():
        if value is not None:
            entry[key] = value

total = sum(entry["slices_total"] or 0 for entry in report["entries"])
skipped = sum(entry["slices_skipped"] or 0 for entry in report["entries"])
produced = sum(entry["slices_produced"] or 0 for entry in report["entries"])
failed = sum(entry["slices_failed"] or 0 for entry in report["entries"])
interfaces = total - skipped
report.update(
    {
        "slices_total": total,
        "slices_skipped_non_interface": skipped,
        "slices_with_interface": interfaces,
        "slices_produced": produced,
        "slices_failed": failed,
        "slice_success_rate": round(produced / interfaces, 4) if interfaces else None,
    }
)
with open(JSON, "w", encoding="utf-8") as fp:
    json.dump(report, fp, ensure_ascii=False, indent=2)

print(f"片总数 {total}；非接口跳过 {skipped}；接口片 {interfaces}")
print(f"接口片产出用例 {produced}/{interfaces} = {report['slice_success_rate']}（失败 {failed} 片）")
print(f"用例 {report['cases_produced']} 条；L2 通过 {report['l2_passed']}"
      f"/{report['cases_produced']} = {report['l2_pass_rate']}")
print(f"耗时合计 {sum(entry['elapsed_s'] for entry in report['entries']):.0f}s；"
      f"源码树被动过：{report['source_tree_touched'] or '（没有）'}")
print("按文档：")
for entry in report["entries"]:
    print(
        f"  {entry['tag']:<26} exit={entry['exit']:<2} {entry['elapsed_s']:>5}s "
        f"片 {entry['slices_produced']}/{entry['slices_total']}"
        f"（跳过 {entry['slices_skipped']}） 用例 {len(entry['cases'])} "
        f"L2 {entry['l2_pass']}/{entry['l2_total']} 留痕 {entry['manifests']}"
    )
