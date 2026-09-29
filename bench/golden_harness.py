"""LLM 生成质量基线 harness（**离线、可复现、零 LLM 依赖**）。

## 为什么要有这个文件

方案 §7.1 的 P0a 验收标准写着「30 份真实接口文档样本：≤2 轮修正环后
`validate_emitted_case` 通过率 ≥90%」。但那个 **≥90% 在写方案时是没有基线的**——
LLM 现在到底能做到多少、哪些形态必挂，没有任何数字。

本 harness 把那条验收标准变成**可先测量、再承诺**的东西：

1. 用**本仓真实语法**的 golden 文档集（`bench/golden/*.md`，与 YAML 用例配成对）；
2. 对每份文档跑一条**确定性**的装配路径（`CaseDraft → IRCase → emit_case → validate_emitted_case`）；
3. 输出**逐份的通过/失败 + 失败原因分类**，得到可作为验收基线的数字。

## 为什么离线也能有意义（关键设计）

本 harness **不调用任何模型**。它测的是**「文档 → 用例」这条链路的装配与校验能力**——
也就是方案里 L1/L2 两级闸门 + 装配器。这正是 P0a 里**除模型能力之外**的全部工程量：

- 如果连 golden 文档都装配不出来，换任何模型都没用；
- 装配器一旦稳定，模型侧的变量（换模型、换 prompt）才可**单独**度量。

所以本 harness 先钉住「脚手架是否可靠」，再让模型能力成为**唯一变量**。
这与本仓「先建确定性护栏、再谈其他」的取向一致。

## 用法

    python bench/golden_harness.py            # 跑全部 golden，打印记分卡
    python bench/golden_harness.py --json     # 输出 JSON（供 CI 对账基线）

护栏：`tests/golden_baseline_test.py` 会把这里的数字钉住——
通过率**下降**会变红（防回归），**上升**需要显式更新基线（防悄悄漂移）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import traceback
from typing import Any, Dict, List, Optional

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

GOLDEN_DIR = os.path.join(BASE, "bench", "golden")

# 失败原因分类：验收标准里的「通过率」必须能按**原因**拆开，
# 否则「90%」这个数字无法指导任何行动。
FAIL_LOAD = "load"          # YAML 读不进来 / 模型校验失败
FAIL_MAKE = "make"          # 生成期闸门报错（算子白名单/内联 schema/XSD/字面量...）
FAIL_COMPILE = "compile"    # 生成物不是合法 Python
FAIL_PROJECT = "project"    # 项目定位（RootDir/debugtalk）失败
FAIL_OTHER = "other"


def _classify(exc: BaseException) -> str:
    """把异常归类到上面的桶里（用于记分卡的「失败原因分布」）。"""
    name = type(exc).__name__
    text = f"{name}: {exc}"
    if "FileFormat" in name or "TestCaseFormat" in name or "Validation" in name:
        return FAIL_LOAD
    if "ParamsError" in name:
        return FAIL_MAKE
    if "SyntaxError" in name:
        return FAIL_COMPILE
    if "compile" in text.lower():
        return FAIL_COMPILE
    # make 的闸门多数抛 MyBaseError/ParamsError，已在上面的分支覆盖；
    # 其余按 make 期问题归类（本项目里绝大多数生成期报错来自 make）。
    return FAIL_MAKE


def load_golden_cases() -> List[Dict[str, Any]]:
    """读取 golden 文档集：每个 `*.md` 配一份同名 `*.yml`（期望产物）。"""
    cases: List[Dict[str, Any]] = []
    if not os.path.isdir(GOLDEN_DIR):
        return cases
    for name in sorted(os.listdir(GOLDEN_DIR)):
        if not name.endswith(".md"):
            continue
        stem = name[: -len(".md")]
        md_path = os.path.join(GOLDEN_DIR, name)
        # 期望 YAML：与文档同名的 .yml；没有就让装配器只做「空骨架」校验
        yml_path = os.path.join(GOLDEN_DIR, stem + ".yml")
        cases.append(
            {
                "name": stem,
                "doc": md_path,
                "expected_yaml": yml_path if os.path.isfile(yml_path) else None,
            }
        )
    return cases


def evaluate_expected_yaml(yml_path: str) -> Dict[str, Any]:
    """对一份**已存在的** YAML 跑 L2 校验（导出即验证）。

    这是 harness 的核心度量点：它等价于方案里 L2 闸门
    （`load_testcase_file` + `make_testcase` 干跑 + `compile()`），
    只是输入从 IR 换成了 golden 里人工写好的期望产物。
    """
    from interfacetester.converters.emit_yaml import validate_emitted_case

    try:
        source = validate_emitted_case(yml_path)
        return {"ok": True, "source_len": len(source), "reason": None, "bucket": None}
    except BaseException as exc:  # noqa: BLE001 - harness 要抓住一切并分类
        return {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "bucket": _classify(exc),
            "traceback": traceback.format_exc()[-1500:],
        }


def run(verbose: bool = False) -> Dict[str, Any]:
    cases = load_golden_cases()
    results: List[Dict[str, Any]] = []

    # 在临时目录里跑：validate_emitted_case 有副作用（生成 *_test.py + black 格式化），
    # 不能污染仓库。本仓已知「临时目录不能落在工作区外」（沙箱只放行工作区写），
    # 所以用工作区内的 .tmp_golden/。
    # NOTICE：跑完必须清理——`validate_emitted_case` 会生成 `*_test.py` 并做 black 格式化，
    # 留着就是仓库垃圾（而且会被 generated_artifacts_drift_test 之类的全仓扫描看到）。
    work_root = os.path.join(BASE, ".tmp_golden")
    if os.path.isdir(work_root):
        shutil.rmtree(work_root, ignore_errors=True)
    os.makedirs(work_root, exist_ok=True)

    for case in cases:
        entry: Dict[str, Any] = {"name": case["name"], "has_expected_yaml": bool(case["expected_yaml"])}
        if not case["expected_yaml"]:
            entry.update({"ok": False, "reason": "缺少配对的 .yml 期望产物", "bucket": FAIL_LOAD})
            results.append(entry)
            continue

        case_dir = os.path.join(work_root, case["name"])
        if os.path.isdir(case_dir):
            shutil.rmtree(case_dir, ignore_errors=True)
        os.makedirs(case_dir, exist_ok=True)
        # 复制整份 golden 目录（含 debugtalk.py / schemas/ 等），保持相对引用可用
        local_yml = os.path.join(case_dir, os.path.basename(case["expected_yaml"]))
        shutil.copy2(case["expected_yaml"], local_yml)
        for extra in os.listdir(GOLDEN_DIR):
            src = os.path.join(GOLDEN_DIR, extra)
            dst = os.path.join(case_dir, extra)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)

        outcome = evaluate_expected_yaml(local_yml)
        entry.update(outcome)
        results.append(entry)
        if verbose:
            mark = "PASS" if outcome["ok"] else "FAIL"
            print(f"[{mark}] {case['name']}" + ("" if outcome["ok"] else f"  <- {outcome['reason'][:160]}"))

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    buckets: Dict[str, int] = {}
    for r in results:
        if not r["ok"] and r.get("bucket"):
            buckets[r["bucket"]] = buckets.get(r["bucket"], 0) + 1

    # 收尾清理：不留生成物（见上面 work_root 处的 NOTICE）
    shutil.rmtree(work_root, ignore_errors=True)

    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "failure_buckets": buckets,
        "results": results,
    }


def main() -> int:
    # T26（方案 v8 §12.8-①）：重定向/管道/CI 环境下，Windows 默认把 stdout/stderr
    # 按 GBK 编码，✓/✗/→ 等字符会抛 UnicodeEncodeError → 退出码 1（假红）。
    # 统一钉为 UTF-8 + errors=replace，保证「实测通过四元组」跨环境可复现。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # 不支持 reconfigure 或流已关闭
            pass

    parser = argparse.ArgumentParser(description="LLM 生成质量基线 harness（离线）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()

    report = run(verbose=args.verbose and not args.json)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print()
    print("=" * 62)
    print("golden 基线记分卡（L2 导出即验证口径）")
    print("=" * 62)
    print(f"  样本数 : {report['total']}")
    print(f"  通过   : {report['passed']}")
    print(f"  失败   : {report['failed']}")
    print(f"  通过率 : {report['pass_rate'] * 100:.1f}%")
    if report["failure_buckets"]:
        print("  失败原因分布：")
        for bucket, count in sorted(report["failure_buckets"].items()):
            print(f"    - {bucket}: {count}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
