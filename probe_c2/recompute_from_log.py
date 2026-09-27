# -*- coding: utf-8 -*-
"""从 `gen_all_<slug>.log` **离线重算**片级账（不重跑模型）。

## 背景（为什么需要它）

`measure_p0a_l2.py` 的 `_SLICES_RE` 把 CLI 那行的 `片产出用例` 误写成了 `片产出**一**用例`
（多一个「一」）→ 2026-09-26 那轮 15 篇的 `slices_total` / `slices_produced` 全为 `null`，
聚合出来的 `slice_success_rate` 是 `-0.0`（假数）。
**注意：正则两轮日志都是 0 匹配**——所以 `..._preC2.json` 里的 70/55/15 是**更早一版脚本**留下的，
不是"这次才坏"。

## 做法与边界

- 按**同一条口径**（CLI 那行 + 另跳过 + 片失败）从日志重算，并**先自检**：日志里必须能解析出
  ≥1 行、且每篇的 produced ≤ total（否则说明口径又漂了，宁可不报）。
- 结果写到 **`out_l2_baseline_<slug>.recomputed.json`**（**不覆盖**机器原始记录——那才是证据；
  原文件里 `slices_*` 仍是 null，可对照）。
"""

import json
import pathlib
import re

BASE = pathlib.Path(r"D:\tester\probe_p0a_llm")
SLUG = "gemma4-latest"
LOG = BASE / f"gen_all_{SLUG}.log"
RAW_JSON = BASE / f"out_l2_baseline_{SLUG}.json"
OUT_JSON = BASE / f"out_l2_baseline_{SLUG}.recomputed.json"

_SLICES = re.compile(r"(\d+)/(\d+)\s*片产出用例")
_SKIPPED = re.compile(r"另有\s*(\d+)\s*片")
_FAILED = re.compile(r"(\d+)\s*片失败")


def main() -> int:
    text = LOG.read_text(encoding="utf-8", errors="replace")
    produced = total = skipped = failed = 0
    hits = 0
    per_doc = []
    for match in _SLICES.finditer(text):
        got, all_slices = int(match.group(1)), int(match.group(2))
        if got > all_slices:
            print(f"口径漂了：{got}/{all_slices} 出现 produced > total")
            return 2
        produced += got
        total += all_slices
        hits += 1
        per_doc.append((got, all_slices))
    skipped = sum(int(m.group(1)) for m in _SKIPPED.finditer(text))
    failed = sum(int(m.group(1)) for m in _FAILED.finditer(text))

    if hits == 0:
        print("日志里解析不出任何片级行 —— 判据失效，不报数")
        return 2

    interfaces = total - skipped
    raw = json.loads(RAW_JSON.read_text(encoding="utf-8"))
    report = {
        "model": raw.get("model"),
        "docs": raw.get("docs"),
        "docs_with_cases": raw.get("docs_with_cases"),
        "cases_produced": raw.get("cases_produced"),
        "l2_pass_rate": raw.get("l2_pass_rate"),
        "source_tree_touched": raw.get("source_tree_touched"),
        "slices_total": total,
        "slices_skipped_non_interface": skipped,
        "slices_with_interface": interfaces,
        "slices_produced": produced,
        "slices_failed": failed,
        "slice_success_rate": round(produced / interfaces, 4) if interfaces else None,
        "recomputed_from": LOG.name,
        "recomputed_rows": hits,
        "note": (
            "由 recompute_from_log.py 从 CLI stdout 日志重算（原 json 的 slices_* 为 null："
            "measure_p0a_l2.py 的正则曾把「片产出用例」误写成「片产出一用例」）"
        ),
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"解析到片级行 {hits} 条（逐篇 produced/total 前 5：{per_doc[:5]}）")
    for key, value in report.items():
        if not isinstance(value, (list, dict)):
            print(f"  {key} = {value}")
    print(f"\n已写 {OUT_JSON.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
