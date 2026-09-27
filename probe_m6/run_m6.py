# -*- coding: utf-8 -*-
"""M-6：`analyze` / `summary` / `fix` / `debugtalk` 四条命令**各跑一次真模型**。

口径（照 `docs/未做完的待决策项.md` §四 M-6）：用**真实失败用例**的 summary.json（`project-two/logs/probe_silent.summary.json`，
`success=False`、4 用例 2 失败），确认**退出码**与**产物**。

★工作区零污染：全程 `cwd=仓外临时目录`、`PYTHONPATH=仓根`（包在别处不可导入，M-4 实测过），
产物（`reports/*.md`、`.ai/*`）都落在临时目录。
"""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(r"D:\tester")
PY = REPO / ".venv" / "Scripts" / "python.exe"
SUMMARY = REPO / "project-two" / "logs" / "probe_silent.summary.json"

ENV = {
    **os.environ,
    "PYTHONPATH": str(REPO),
    "PYTHONIOENCODING": "utf-8",
    "INTERFACETESTER_AI_BASE_URL": "http://127.0.0.1:11434/v1",
    "INTERFACETESTER_AI_MODEL": "gemma4:latest",
    "INTERFACETESTER_AI_SANDBOX": "on",
    "INTERFACETESTER_AI_TIMEOUT": "300",
    "INTERFACETESTER_AI_RETRIES": "1",
}


def run(args, workdir, label):
    proc = subprocess.run(
        [str(PY), "-m", "interfacetester_ai.cli", *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ENV,
    )
    products = sorted(
        str(p.relative_to(workdir)).replace("\\", "/")
        for p in pathlib.Path(workdir).rglob("*")
        if p.is_file() and "spec.json" not in p.name
    )
    print(f"\n{'=' * 78}\n[{label}] 命令: haify {' '.join(args)}\n退出码: {proc.returncode}")
    tail_out = [line for line in (proc.stdout or "").splitlines() if line.strip()][-6:]
    tail_err = [line for line in (proc.stderr or "").splitlines() if line.strip()][-6:]
    print("stdout 尾 6 行:")
    for line in tail_out:
        print("   ", line[:170])
    print("stderr 尾 6 行:")
    for line in tail_err:
        print("   ", line[:170])
    print("临时目录产物:", products)
    return proc


def main() -> int:
    if not SUMMARY.exists():
        print(f"缺真实失败用例：{SUMMARY}")
        return 2

    with tempfile.TemporaryDirectory(prefix="m6_") as root:
        workdir = pathlib.Path(root)

        # ① analyze：失败诊断 → reports/analysis.md（+ doc_defects.md）
        run(["analyze", str(SUMMARY)], workdir, "P1a analyze")
        analysis = workdir / "reports" / "analysis.md"
        if analysis.exists():
            text = analysis.read_text(encoding="utf-8", errors="replace")
            print(f"   analysis.md：{len(text)} 字符；前 3 行 = {text.splitlines()[:3]}")

        # ② summary：结果摘要 → reports/summary.md
        run(["summary", str(SUMMARY)], workdir, "P1b summary")
        summary_md = workdir / "reports" / "summary.md"
        if summary_md.exists():
            text = summary_md.read_text(encoding="utf-8", errors="replace")
            print(f"   summary.md：{len(text)} 字符；含'用例' = {'用例' in text}")

        # ③ fix：自愈建议（只出 diff、零写盘）
        run(["fix", str(SUMMARY), "--cases", str(REPO / "project-two")], workdir, "P2a fix")

        # ④ debugtalk：签名函数草稿（带**真**已知答案向量 + L3 沙盒）
        canonical = "a=1&b=2"
        spec = {
            "name": "sign_params",
            "args": ["params"],
            "body": "把 params 按 key 排序拼成 `k=v&...` 查询串，再取 md5 十六进制小写",
            "vectors": [{"args": [{"b": 2, "a": 1}], "expect": hashlib.md5(canonical.encode()).hexdigest()}],
        }
        spec_path = workdir / "spec.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        run(["debugtalk", str(spec_path), "--sandbox"], workdir, "P2b debugtalk（--sandbox）")

        print(f"\n{'=' * 78}\n临时目录（跑完即删）：{workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
