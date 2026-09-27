# -*- coding: utf-8 -*-
"""闸门误伤率审计 —— 把**闸门**挂到真实 golden 样本上（§3.5 / **T10**）。

## 为什么需要它

`bench/golden_harness.py` 量的是「L2 导出即验证」的通过率；但 §3.5 还有一条更早的纪律：

> **所以 golden 扩容到 15 份时，必须把闸门挂上去统计真实误伤率，并把结果写回 MUST_ALLOW 集。**

理由是"**假报会让真报被无视**"：闸门只要误伤一个**合法**形态，用户就会整体关掉它——
那时它等于不存在。所以闸门必须在**真实样本**上证明自己**不误伤**，而不只是在
自己那 11 条 MUST_REJECT / 17 条 MUST_ALLOW 夹具上。

## 判据

- **误伤率必须为 0**：15 份 golden 的**期望产物**全部是人工确认过的合法写法，
  任何一份被 `REJECT` 都是误伤 → 退出码非 0；
- **PENDING 不算误伤**（它是"请人工确认"，S4 的设计意图），但**必须列出来**
  ——PENDING 太多说明判据还不够精确，值得下一轮收紧；
- 任何误伤都必须**点名**：哪一份、哪个编号、消息是什么（否则没法修）。

## 用法

    python bench/gate_false_positive_audit.py            # 记分卡
    python bench/gate_false_positive_audit.py --json     # 供 CI / 测试对账
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from bench.golden_harness import load_golden_cases  # noqa: E402
from interfacetester_ai.gates import check_testcase  # noqa: E402


def audit(verbose: bool = False) -> Dict[str, Any]:
    """对每份 golden 的**期望产物**跑闸门，统计误伤（REJECT）与 PENDING。"""
    from interfacetester.loader import load_test_file  # noqa: PLC0415

    entries: List[Dict[str, Any]] = []
    for case in load_golden_cases():
        expected = case["expected_yaml"]
        entry: Dict[str, Any] = {"name": case["name"], "has_expected": bool(expected)}
        if not expected:
            entry.update({"ok": False, "reason": "缺少配对的 .yml 期望产物"})
            entries.append(entry)
            continue

        try:
            suite = load_test_file(expected)
        except BaseException as error:  # noqa: BLE001 - 加载失败也要计入并点名
            entry.update({"ok": False, "reason": f"加载失败：{type(error).__name__}: {error}"})
            entries.append(entry)
            continue

        report = check_testcase(suite, path=expected)
        entry.update(
            {
                "ok": report.ok,
                "codes": report.codes(),
                "rejects": [
                    {"code": finding.code, "where": finding.where, "message": finding.message}
                    for finding in report.rejects
                ],
                "pendings": [
                    {"code": finding.code, "where": finding.where, "message": finding.message}
                    for finding in report.pendings
                ],
            }
        )
        entries.append(entry)
        if verbose:
            mark = "REJECT" if not report.ok else ("PENDING" if report.pendings else "PASS")
            print(f"[{mark:7}] {case['name']}" + (f"  {entry['codes']}" if entry["codes"] else ""))

    total = len(entries)
    injured = [e for e in entries if not e["ok"]]
    pending = [e for e in entries if e["ok"] and e.get("pendings")]

    return {
        "total": total,
        "false_positives": len(injured),
        "false_positive_rate": round(len(injured) / total, 4) if total else 0.0,
        "pending_cases": len(pending),
        "entries": entries,
        "injured": injured,
    }


def main() -> int:
    # T26（方案 v8 §12.8-①）：重定向/CI 下 Windows 默认 GBK，✓/→ 会抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="闸门误伤率审计（T10 / §3.5）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()

    report = audit(verbose=args.verbose and not args.json)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["false_positives"] else 0

    print()
    print("=" * 62)
    print("闸门误伤率审计（真实 golden 样本，§3.5）")
    print("=" * 62)
    print(f"  样本数          : {report['total']}")
    print(f"  误伤（REJECT）  : {report['false_positives']}")
    print(f"  误伤率          : {report['false_positive_rate'] * 100:.1f}%")
    print(f"  带 PENDING 样本 : {report['pending_cases']}")
    if report["injured"]:
        print("  误伤清单（修判据，或把该合法形态写回 MUST_ALLOW）：")
        for entry in report["injured"]:
            print(f"    - {entry['name']}")
            for finding in entry.get("rejects", []):
                print(
                    f"        [{finding['code']}] {finding['where']}"
                    f"\n            {finding['message'][:160]}"
                )
    print("=" * 62)
    return 1 if report["false_positives"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
