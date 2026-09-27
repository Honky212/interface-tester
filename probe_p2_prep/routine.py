# -*- coding: utf-8 -*-
"""**例行任务**：一条命令把探针跑一遍，产物 = `out_routine.txt`（§9.33 登记项：探针别只手工跑）。

    python probe_p2_prep/routine.py                 # 确定性探针（不调模型、不出网）
    python probe_p2_prep/routine.py --with-models   # 再加「按模型统计 analyze 可用性」（真出网）

## 为什么要有它（登记在案的坑）

**「哪个模型能用」曾经失忆过**：同一份 summary、同一台 ollama，`haify analyze` 的结果**只取决于模型**
（`qwen3.8:27b` 通、`gemma4:*` 通不了），而这件事当时**没有任何东西可观测** —— 因为单测全是
`FakeTransport`。本仓的硬纪律是「**Fake 全绿 ≠ 真模型能用**」，所以模型可用性**必须按期真量**，
而且要有一个**记得住**的地方去量（靠人记 = 迟早失忆）。

## 与 `tests/` 的分工（别混）

| | `tests/` | 本脚本 |
| --- | --- | --- |
| 性质 | **离线判据** | **例行实测** |
| 何时跑 | 每次提交都跑 | 人手/例行（换模型、换文档、换环境时） |
| 能不能出网 | **不许** | 要（`--with-models` 时真出网） |
| 判什么 | 逻辑对不对 | **在这台机器/这个模型上到底能不能用** |

## 分类纪律（机器可查）

每一个 `probe_*.py` **都必须被分类**：进 `ROUTINE`（确定性、每次例行都跑）、进 `MODEL_STEPS`
（要真出网）、或进 `EXCLUDED`（附**一句理由**）。判据见
`tests/docs_consistency_test.py::TestProbeRoutineCoversEveryProbe` ——
新增探针忘了分类，那条会红（「忘掉一件事」从此是**响亮**的，不是静默的）。
"""

import argparse
import datetime
import io
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

#: 例行步骤：`(脚本, 说明, 要代起 mock 吗)`。全部**确定性**，退出码必须为 0。
ROUTINE = (
    ("probe_error_rules.py", "错误条件引擎：分族条数 / 绑定 / 不模拟点名（离线）", False),
    ("probe_response_ownership.py", "响应侧归属：段外断言丢弃 + 点名（离线）", False),
    ("probe_upload_path.py", "上传路径：真发 multipart vs 只给文件名", False),
    ("probe_required_fact.py", "响应数据表「必带」→ required + 牙齿对照", False),
    ("probe_cookie_fact.py", "响应 Cookie → 断言 + mock 同源下发 + 牙齿对照", False),
    ("probe_response_header_fact.py", "响应头表 → 断言 + mock 同源下发 + 牙齿对照", False),
    ("probe_negative_cases.py", "五族**成对**能力证明（★需先起 mock，本脚本代起）", True),
)

#: 模型步骤：**要真出网**，退出码**不参与**本脚本成败（断网 / 没配模型都算正常环境）。
MODEL_STEPS = (
    ("probe_analyze_models.py", "按模型统计 `haify analyze` 可用性（真出网）"),
)

#: 不例行（**必附理由**）：一次性测量，结论已经写进文档；留着是证据，不必每次重跑。
EXCLUDED = {
    "probe_no_required_tables.py": (
        "一次性测量：统计「文档里没有『必带』列」的规模。结论已写进 "
        "`docs/未做完的待决策项.md` §9.31；重跑只能看数字变没变，不属例行"
    ),
}

MOCK_SCRIPT = os.path.join(HERE, "mock_file_service.py")
MOCK_PORT = 8899


def _env():
    return {**os.environ, "PYTHONPATH": BASE}


def _run(script, timeout=600):
    """跑一个探针脚本（`cwd` = 仓库根，与手工跑一致）。"""
    return subprocess.run(
        [sys.executable, "-u", "-X", "utf8", os.path.join(HERE, script)],
        cwd=BASE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=_env(),
    )


def _start_mock(doc):
    return subprocess.Popen(
        [sys.executable, "-u", "-X", "utf8", MOCK_SCRIPT, "--port", str(MOCK_PORT), "--doc", doc],
        cwd=BASE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
    )


def _stop(server):
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
        server.kill()


def _keep(line):
    """只留「一眼能看出成败」的行（别把探针全文抄进报告）。"""
    stripped = line.strip()
    return bool(stripped) and (
        "成对结果" in stripped
        or "结论" in stripped
        or stripped.startswith(("★", "PASS", "FAIL", "[PASS]", "[FAIL]"))
    )


def main():
    parser = argparse.ArgumentParser(description="例行任务：跑一遍探针并留证据")
    parser.add_argument("--with-models", action="store_true", help="连模型可用性一起量（真出网）")
    parser.add_argument("--out", default=os.path.join(HERE, "out_routine.txt"), help="报告落点")
    args = parser.parse_args()

    from mock_file_service import DOC_DEFAULT  # noqa: PLC0415

    lines = [
        "=" * 78,
        "例行任务：探针一遍跑（§9.33 登记项）",
        f"时间：{datetime.datetime.now().isoformat(timespec='seconds')}",
        f"文档（mock 用）：{os.path.relpath(DOC_DEFAULT, BASE)}",
        f"模型步骤：{'跑' if args.with_models else '跳过（加 --with-models 才跑）'}",
        "=" * 78,
    ]
    failures = []

    for script, note, needs_mock in ROUTINE:
        lines.append(f"\n【例行】{script} —— {note}")
        server = _start_mock(DOC_DEFAULT) if needs_mock else None
        if server is not None:
            time.sleep(6)  # 等 mock 起来（它要先解析文档、建路由与错误规则）
        try:
            done = _run(script)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            lines.append("  ★超时（>600s）")
            failures.append(script)
            continue
        finally:
            if server is not None:
                _stop(server)
        lines.append(f"  退出码 = {done.returncode}")
        for raw in ((done.stdout or "") + (done.stderr or "")).splitlines():
            if _keep(raw):
                lines.append(f"    {raw.strip()[:120]}")
        if done.returncode != 0:
            failures.append(script)

    if args.with_models:
        for script, note in MODEL_STEPS:
            lines.append(f"\n【模型】{script} —— {note}")
            try:
                done = _run(script, timeout=3600)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
                lines.append("  ★超时（>3600s）")
                continue
            lines.append(
                f"  退出码 = {done.returncode}"
                "（★**不参与**本脚本成败：断网 / 没配模型都算正常环境）"
            )
            for raw in (done.stdout or "").splitlines():
                if _keep(raw):
                    lines.append(f"    {raw.strip()[:120]}")

    lines.append("\n" + "-" * 78)
    if failures:
        lines.append(f"■ 结果：**{len(failures)} 个例行步骤没跑绿**：{'、'.join(failures)}")
    else:
        lines.append(f"■ 结果：**{len(ROUTINE)}/{len(ROUTINE)} 个例行步骤全绿**")
    lines.append("（模型步骤不参与成败；它的结论在 `out_analyze_models.txt`）")

    text = "\n".join(lines) + "\n"
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(text)
    print(text)
    print(f"written {args.out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
