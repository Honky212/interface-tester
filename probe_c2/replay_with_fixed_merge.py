# -*- coding: utf-8 -*-
"""验证"按接口段+子节"的归属修正：用**那一轮的缓存**回放重生成，不跑模型。

做法：对 15 篇 golden 的隔离工作区（`.tmp_p0a_run_gemma4-latest/<tag>/`）逐个跑
`haify gen <doc> --fake` —— `--fake` **只回放 `.ai/cache/`**，所以模型响应就是**那一轮的原始响应**
（不是重新问模型）。修正后的归属口径会把字段表子节算进本接口段 → 落盘产物应该出现**字段级断言**。
"""

import json
import os
import pathlib
import subprocess

REPO = pathlib.Path(r"D:\tester")
RUN = REPO / ".tmp_p0a_run_gemma4-latest"
PY = REPO / ".venv" / "Scripts" / "python.exe"
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

baseline = json.loads(
    (REPO / "probe_p0a_llm" / "out_l2_baseline_gemma4-latest.json").read_text(encoding="utf-8")
)
rows = []
for entry in baseline["entries"]:
    tag = entry["tag"]
    tag_dir = RUN / tag
    doc = REPO / entry["doc"]
    if not tag_dir.is_dir() or not doc.exists():
        print(f"跳过 {tag}（目录或文档不存在）")
        continue
    proc = subprocess.run(
        [str(PY), "-m", "interfacetester_ai.cli", "gen", str(doc), "--fake"],
        cwd=tag_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ENV,
    )
    cases_dir = tag_dir / "cases"
    cases = sorted(cases_dir.glob("*.yml")) if cases_dir.is_dir() else []
    rows.append((tag, proc.returncode, len(cases), (proc.stderr or "").strip().splitlines()[-1:] ))
    print(f"{tag:26} exit={proc.returncode}  重生成用例={len(cases)}")

print("\n汇总：", f"{sum(1 for r in rows if r[1] == 0)} / {len(rows)} 篇 exit=0；",
      f"重生成用例合计 {sum(r[2] for r in rows)} 条")
