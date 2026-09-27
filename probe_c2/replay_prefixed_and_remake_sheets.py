# -*- coding: utf-8 -*-
"""按原始轮次的口径重放（带 `--case-name-prefix`）+ 清理无前缀重复件 + 重出三张表。不跑模型。"""

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
cleaned = 0
for entry in baseline["entries"]:
    tag, tag_dir = entry["tag"], RUN / entry["tag"]
    doc = REPO / entry["doc"]
    if not tag_dir.is_dir() or not doc.exists():
        continue
    # ① 带前缀重放（与原始轮次同名 → **覆盖**那 13 个权威产物）
    subprocess.run(
        [str(PY), "-m", "interfacetester_ai.cli", "gen", str(doc), "--fake",
         "--case-name-prefix", tag + "__"],
        cwd=tag_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
    )
    # ② 删掉我早先重放时**命名少一个下划线**的产物（原始轮次的前缀是 `tag + "__"`）
    for path in (tag_dir / "cases").glob("*.yml"):
        if not path.name.startswith(tag + "__"):
            path.unlink()
            cleaned += 1

print(f"清掉无前缀重复件 {cleaned} 个")

# ③ 重出三张表（`_safe_write` 会保护已填的 B/C）
proc = subprocess.run(
    [str(PY), "probe_p0a_llm/make_review_sheets.py"],
    cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
)
print(proc.stdout[-1200:])
print("exit:", proc.returncode)
