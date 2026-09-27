# -*- coding: utf-8 -*-
"""(6b) P0a 验收 ①/⑦ 取证：golden 15 份 → **真模型** `haify gen`（≤2 轮）→ L2 通过率 + 零污染。

口径**刻意复用仓库自己的** `bench/golden_harness.evaluate_expected_yaml`（= 内核
`validate_emitted_case`：`load_testcase_file` + `make_testcase` 干跑 + `compile()`），
这样"模型产物"与"人工 golden 基线（100%）"是**同一把尺子**。

用法：
    $env:INTERFACETESTER_AI_BASE_URL="http://127.0.0.1:11434/v1"
    $env:INTERFACETESTER_AI_MODEL="gemma4:latest"; $env:INTERFACETESTER_AI_SANDBOX="on"
    .\\.venv\\Scripts\\python.exe probe_p0a_llm\\measure_p0a_l2.py          # 全 15 份
    $env:P0A_ONLY="login.md,user_info.md"; ... measure_p0a_l2.py            # 先跑试点
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from bench.golden_harness import GOLDEN_DIR, evaluate_expected_yaml  # noqa: E402

RUN_ROOT = os.path.join(BASE, ".tmp_p0a_run")
L2_ROOT = os.path.join(BASE, ".tmp_p0a_l2")
OUT_JSON = os.path.join(BASE, "probe_p0a_llm", "out_l2_baseline.json")
MODEL = os.environ.get("P0A_MODEL", "gemma4:latest")
_MODEL_SLUG = re.sub(r"[^A-Za-z0-9._-]+", "-", MODEL).strip("-")
# ★按模型分开命名：换模型做对照时**不覆盖**上一轮的取证（否则"对照"就只剩一份了）
RUN_ROOT = os.path.join(BASE, f".tmp_p0a_run_{_MODEL_SLUG}")
L2_ROOT = os.path.join(BASE, f".tmp_p0a_l2_{_MODEL_SLUG}")
OUT_JSON = os.path.join(BASE, "probe_p0a_llm", f"out_l2_baseline_{_MODEL_SLUG}.json")
LOG_PATH = os.path.join(BASE, "probe_p0a_llm", f"gen_all_{_MODEL_SLUG}.log")

# 源码树快照要排除的东西（运行期产物 / 取证目录）——用**前缀**判，免得每加一个临时目录都要改这里
_SKIP_NAMES = {"__pycache__", ".ai", "cases", "logs", "reports", "probe_p0a_llm"}


def _skipped(name: str) -> bool:
    return name in _SKIP_NAMES or name.startswith(".tmp_")

# `haify gen` 汇总行（用来算"送进模型的片"这一层，见 main 里的口径说明）
_SLICES_RE = re.compile(r"(\d+)/(\d+)\s*片产出(?:用例|草稿)")
_FAILED_RE = re.compile(r"(\d+)\s*片失败")
_SKIPPED_RE = re.compile(r"另有\s*(\d+)\s*片")


def _snapshot(root=BASE):
    """源码树快照（`相对路径 → 大小`）——排除运行期目录，用于"零污染"判据。"""
    found = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not _skipped(d)]
        for name in names:
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                found[rel] = os.path.getsize(path)
            except OSError:
                found[rel] = -1
    return found


def _child_env():
    """给 `gen` 子进程的环境：**只放它需要的**（回环 Ollama：不需要 API key）。"""
    env = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
        if name in os.environ
    }
    env.update(
        {
            "PYTHONPATH": BASE,
            "PYTHONIOENCODING": "utf-8",
            "INTERFACETESTER_AI_BASE_URL": os.environ.get(
                "INTERFACETESTER_AI_BASE_URL", "http://127.0.0.1:11434/v1"
            ),
            "INTERFACETESTER_AI_MODEL": MODEL,
            "INTERFACETESTER_AI_SANDBOX": "on",
            "INTERFACETESTER_AI_TIMEOUT": os.environ.get(
                "INTERFACETESTER_AI_TIMEOUT", "300"
            ),
            "INTERFACETESTER_AI_RETRIES": os.environ.get("INTERFACETESTER_AI_RETRIES", "1"),
        }
    )
    return env


def _gen_one(index, doc_path, log_handle):
    """在一份**独立工作区**里对一篇 golden 文档跑真模型 `gen`（≤2 轮）。"""
    name = os.path.basename(doc_path)[: -len(".md")]
    tag = f"G{index:02d}_{name}"
    workspace = os.path.join(RUN_ROOT, tag)
    shutil.rmtree(workspace, ignore_errors=True)
    os.makedirs(workspace, exist_ok=True)

    started = time.time()
    try:
        proc = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "interfacetester_ai",
                "gen",
                doc_path,
                "--case-name-prefix",
                tag + "__",
                "--max-rounds",
                "2",
            ],
            cwd=workspace,
            env=_child_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            check=False,
        )
        exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        exit_code, stdout, stderr = -1, "", "TIMEOUT 3600s"
    elapsed = round(time.time() - started, 1)

    # ★2026-09-26（§九 第一档②）：产物默认落 `.ai/draft/`（不再是 `cases/`）。
    # 两处都收：**老轮次**的取证目录里产物在 `cases/`，**新轮次**在 `.ai/draft/`。
    # 只收一处会让重跑基线得到"0 条产出"这种**假结果**（而它看起来像个结论）。
    cases = sorted(
        glob.glob(os.path.join(workspace, "cases", "*.yml"))
        + glob.glob(os.path.join(workspace, ".ai", "draft", "*.yml"))
    )
    manifests = glob.glob(os.path.join(workspace, ".ai", "manifest", "*.json"))
    failed = sorted(glob.glob(os.path.join(workspace, ".ai", "failed", "*", "NEEDS_HUMAN.md")))
    slices_match = _SLICES_RE.search(stdout or "")
    failed_match = _FAILED_RE.search(stdout or "")
    skipped_match = _SKIPPED_RE.search(stdout or "")
    log_handle.write(f"\n===== {tag} ===== exit={exit_code} elapsed={elapsed}s\n")
    log_handle.write(stdout[-6000:])
    if stderr.strip():
        log_handle.write("\n--- stderr ---\n" + stderr[-2000:])
    log_handle.flush()
    return {
        "tag": tag,
        "doc": os.path.relpath(doc_path, BASE).replace(os.sep, "/"),
        "exit": exit_code,
        "elapsed_s": elapsed,
        "cases": cases,
        "manifests": len(manifests),
        "failed_dirs": len(failed),
        # 片层面（模型能力的真实信号）：送进模型的片数 / 产出的片数 / 失败的片数 / 非接口跳过
        "slices_total": int(slices_match.group(2)) if slices_match else None,
        "slices_produced": int(slices_match.group(1)) if slices_match else None,
        "slices_failed": int(failed_match.group(1)) if failed_match else None,
        "slices_skipped": int(skipped_match.group(1)) if skipped_match else None,
        "stdout_tail": stdout[-1200:],
    }


def _measure_l2(entry):
    """对某篇产出的用例跑 **L2**（同 golden harness 口径）；单独复制到临时目录里跑。"""
    results = []
    for yml in entry["cases"]:
        case_dir = os.path.join(L2_ROOT, entry["tag"], os.path.basename(yml)[: -len(".yml")])
        shutil.rmtree(case_dir, ignore_errors=True)
        os.makedirs(case_dir, exist_ok=True)
        local = os.path.join(case_dir, os.path.basename(yml))
        shutil.copy2(yml, local)
        # 与 golden harness 同款：把 golden 目录里的旁挂文件（schemas/debugtalk 等）一起复制，
        # 保证相对引用可用（否则会因为"缺文件"误判，而不是因为产物本身不合格）
        for extra in os.listdir(GOLDEN_DIR):
            src = os.path.join(GOLDEN_DIR, extra)
            dst = os.path.join(case_dir, extra)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
        outcome = evaluate_expected_yaml(local)
        outcome.update(
            {"yml": os.path.basename(yml), "name": os.path.basename(yml)[: -len(".yml")]}
        )
        results.append(outcome)
    return results


def main() -> int:
    docs = sorted(glob.glob(os.path.join(GOLDEN_DIR, "*.md")))
    only = [item.strip() for item in (os.environ.get("P0A_ONLY") or "").split(",") if item.strip()]
    if only:
        docs = [path for path in docs if os.path.basename(path) in only]

    shutil.rmtree(RUN_ROOT, ignore_errors=True)
    shutil.rmtree(L2_ROOT, ignore_errors=True)
    os.makedirs(RUN_ROOT, exist_ok=True)
    os.makedirs(L2_ROOT, exist_ok=True)

    before = _snapshot()
    print(f"模型 {MODEL}；文档 {len(docs)} 份；工作区 {RUN_ROOT}")

    log_path = LOG_PATH
    entries = []
    with open(log_path, "w", encoding="utf-8") as log_handle:
        for index, doc_path in enumerate(docs, start=1):
            entry = _gen_one(index, doc_path, log_handle)
            print(
                f"[{index}/{len(docs)}] {entry['tag']}: exit={entry['exit']} "
                f"{entry['elapsed_s']}s 用例={len(entry['cases'])} 留痕={entry['manifests']}"
            )
            entry["l2"] = _measure_l2(entry)
            entry["l2_pass"] = sum(1 for item in entry["l2"] if item["ok"])
            entry["l2_total"] = len(entry["l2"])
            print(f"        L2: {entry['l2_pass']}/{entry['l2_total']} 通过")
            entries.append(entry)

    after = _snapshot()
    touched = sorted(set(after.items()) ^ set(before.items()))

    produced = sum(entry["l2_total"] for entry in entries)
    passed = sum(entry["l2_pass"] for entry in entries)
    with_cases = sum(1 for entry in entries if entry["cases"])
    slices_total = sum(entry["slices_total"] or 0 for entry in entries)
    slices_produced = sum(entry["slices_produced"] or 0 for entry in entries)
    slices_failed = sum(entry["slices_failed"] or 0 for entry in entries)
    slices_skipped = sum(entry["slices_skipped"] or 0 for entry in entries)
    interfaces = slices_total - slices_skipped  # 真正"有接口形态"的片（非接口片按设计跳过）
    report = {
        "model": MODEL,
        "docs": len(entries),
        "docs_with_cases": with_cases,
        "cases_produced": produced,
        "l2_passed": passed,
        "l2_pass_rate": round(passed / produced, 4) if produced else None,
        # ★口径说明（不写清就会把重言式当判据）：
        #   `cases/` 里的产物**已经**过了装配器的写盘前置闸门（S-4 闸门在前），
        #   所以"对最终产物量 L2"**恒为 100%**——它是一条**重言式**，只能证明
        #   "落盘的产物是可用的"，不能证明"模型有多能过闸"。
        #   真正随模型能力变化的是**片层面**：送进模型的接口片里有多少最终产出用例。
        "l2_note": (
            "L2 对最终产物恒为 100%（装配器闸门在前），是重言式；"
            "有效信号是 slice_success_rate（送进模型的接口片 → 产出用例）"
        ),
        "slices_total": slices_total,
        "slices_skipped_non_interface": slices_skipped,
        "slices_with_interface": interfaces,
        "slices_produced": slices_produced,
        "slices_failed": slices_failed,
        "slice_success_rate": round(slices_produced / interfaces, 4) if interfaces else None,
        "source_tree_touched": touched,
        "entries": [
            {key: value for key, value in entry.items() if key != "stdout_tail"}
            for entry in entries
        ],
    }
    with open(OUT_JSON, "w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2)

    print("=" * 66)
    print(f"模型           : {report['model']}")
    print(f"文档           : {report['docs']} 份（有产出 {with_cases} 份）")
    print(f"片（送进模型） : {interfaces} 片有接口形态（另跳过 {slices_skipped} 片非接口）")
    print(
        f"片 → 产出用例  : {slices_produced}/{interfaces} = {report['slice_success_rate']}"
        f"（失败 {slices_failed} 片）"
    )
    print(f"产出用例       : {produced}")
    print(
        f"L2 通过        : {passed}/{produced} = {report['l2_pass_rate']}"
        "   ← ★装配器闸门在前，这一项**恒为 100%**（重言式）；有效信号是上一行"
    )
    print(f"源码树被动过   : {touched if touched else '（没有——零污染）'}")
    print(f"报告           : {os.path.relpath(OUT_JSON, BASE)}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

