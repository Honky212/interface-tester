# -*- coding: utf-8 -*-
"""按**模型**统计 `haify analyze` 能不能跑通 —— §9.24 的可观测口径。

## 为什么要有它（真实事故）

2026-09-27 实测：**同一份真 summary、同一台 ollama**，`haify analyze` 的结果**只取决于模型** ——

| 模型 | 结果 |
| --- | --- |
| `qwen3.8:27b` | ✅ 成功：6 条证据包 → 6 条结论，未降级 6/6 = 100%，引用逐字可溯源 |
| `gemma4:latest` | ❌ 返回 `<unused50>`×N（**不是 JSON**）→ 整次失败、**无报告** |
| `gemma4:12b` | ❌ 同上 |

而这件事当时**没有任何东西可观测** ✗ —— 单测用 `FakeTransport`（"Fake 全绿 ≠ 真模型能用"）。
契约 + 修正环落地之后，用本探针**复测并留下证据**：哪个模型一次就过、哪个靠修正环救回来、
哪个连修正环也救不了。

    python probe_p2_prep/probe_analyze_models.py                  # 默认三个模型
    python probe_p2_prep/probe_analyze_models.py --models gemma4:latest --timeout 900

它对每个模型：在**独立临时工作区**里跑真实的 `haify analyze`（真出网、真调用），记录
退出码 / 报告是否产出 / 未降级占比 / **修正环轮数** / 首条错误行 → 写 `out_analyze_models.txt`。
★不起服务、不写产品目录（每个模型一个临时工作区）、不改任何既有产物。
"""

import argparse
import io
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_SUMMARY = os.path.join(BASE, "project-two", "logs", "probe_silent.summary.json")
DEFAULT_DOC = os.path.join(BASE, "project-three", "统一文件服务平台-接入应用接口文档.md")
DEFAULT_MODELS = ("gemma4:latest", "gemma4:12b", "qwen3.8:27b")

KEPT_RATIO_RE = re.compile(r"\*\*未降级占比：(\d+)%\*\*")
ROUNDS_RE = re.compile(r"修正环：\*\*(\d+)\*\* 轮")


def run_one(model, summary, doc, timeout, workspace):
    """跑一个模型 → `(退出码, 报告是否有, 未降级占比, 修正环轮数, 关键输出行)`。"""
    env = dict(os.environ)
    env["INTERFACETESTER_AI_BASE_URL"] = "http://127.0.0.1:11434/v1"
    env["INTERFACETESTER_AI_MODEL"] = model
    env["INTERFACETESTER_AI_SANDBOX"] = "on"
    env["PYTHONPATH"] = BASE
    os.makedirs(workspace, exist_ok=True)

    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "interfacetester_ai.cli",
        "analyze",
        summary,
    ]
    if doc:
        command += ["--doc", doc]
    try:
        done = subprocess.run(
            command,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, False, "", "", f"超时（>{timeout}s）"

    report = os.path.join(workspace, "reports", "analysis.md")
    ratio = rounds = ""
    if os.path.isfile(report):
        text = io.open(report, encoding="utf-8").read()
        match = KEPT_RATIO_RE.search(text)
        ratio = f"{match.group(1)}%" if match else "?"
        match = ROUNDS_RE.search(text)
        rounds = match.group(1) if match else "?"
    head = ""
    for line in ((done.stdout or "") + (done.stderr or "")).splitlines():
        if re.search(r"乱码|不是合法 JSON|DiagnosisFailed|无法产出分析|连续 \d+ 轮", line):
            head = line.strip()[:110]
            break
    return done.returncode, os.path.isfile(report), ratio, rounds, head


def main():
    parser = argparse.ArgumentParser(description="按模型统计 analyze 的可用性（§9.24）")
    parser.add_argument("--summary", default=DEFAULT_SUMMARY)
    parser.add_argument("--doc", default=DEFAULT_DOC)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--out", default=os.path.join(BASE, "probe_p2_prep", "out_analyze_models.txt")
    )
    args = parser.parse_args()

    models = [item.strip() for item in args.models.split(",") if item.strip()]
    lines = [
        "=" * 78,
        "按模型统计 `haify analyze` 可用性（★真出网、真调用；每个模型独立工作区）",
        f"输入：{args.summary}",
        f"文档：{args.doc or '（未给）'}",
        "=" * 78,
    ]
    ok = 0
    for model in models:
        workspace = os.path.join(
            os.environ.get("TEMP", "/tmp"), "ai_an_probe_" + re.sub(r"[^\w]+", "_", model)
        )
        code, has_report, ratio, rounds, head = run_one(
            model, args.summary, args.doc, args.timeout, workspace
        )
        passed = code == 0 and has_report
        ok += 1 if passed else 0
        mark = "PASS" if passed else "FAIL"
        lines.append(
            f"\n[{mark}] {model}｜退出码={code}｜报告={'有' if has_report else '无'}"
            f"｜未降级={ratio or '—'}｜修正环={rounds or '—'} 轮"
        )
        lines.append(f"       工作区：{workspace}")
        if head:
            lines.append(f"       关键行：{head}")
        print(lines[-2] if not head else "\n".join(lines[-3:]))
        # ★每个模型一落盘：这样"卡在某个模型上"时前面的结论**已经**能看（探针自己也要可观测）
        io.open(args.out, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")

    lines.append(
        f"\n■ 结论：**{ok}/{len(models)}** 个模型跑通"
        "（★『跑通』= 退出码 0 且产出 `reports/analysis.md`；"
        "靠修正环救回来的也算跑通——但轮数会写在上面，一眼能看出是谁在救场）"
    )
    text = "\n".join(lines) + "\n"
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(text)
    print(f"\nwritten {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
