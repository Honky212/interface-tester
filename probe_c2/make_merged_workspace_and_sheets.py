# -*- coding: utf-8 -*-
"""在**独立工作区**里生成"合并修正版"产物，并出**另一套表**（不碰原始产物/已填表）。不跑模型。

背景：装配器有"查重拒绝覆盖"——在原始产物目录里重放**写不进去**（实测：表 A 仍 13 条）。
所以：① 复制各 tag 的 `.ai/cache` 到新工作区（`--fake` 才能命中）；② 在新工作区里重放；
③ 用 `P0A_RUN_ROOT` / `P0A_SHEET_DIR` 让 `make_review_sheets.py` 出到**另一套表**。
"""

import json
import os
import pathlib
import shutil
import subprocess

REPO = pathlib.Path(r"D:\tester")
SRC = REPO / ".tmp_p0a_run_gemma4-latest"
DST = REPO / ".tmp_p0a_run_gemma4-latest_merged"
SHEETS = REPO / "probe_p0a_llm" / "review_sheets" / "gemma4-latest_mergedfix"
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
    "P0A_RUN_ROOT": str(DST),
    "P0A_SHEET_DIR": str(SHEETS),
}

baseline = json.loads(
    (REPO / "probe_p0a_llm" / "out_l2_baseline_gemma4-latest.json").read_text(encoding="utf-8")
)
for entry in baseline["entries"]:
    tag = entry["tag"]
    src, dst, doc = SRC / tag, DST / tag, REPO / entry["doc"]
    if not src.is_dir() or not doc.exists():
        continue
    dst.mkdir(parents=True, exist_ok=True)
    cache_src, cache_dst = src / ".ai" / "cache", dst / ".ai" / "cache"
    if cache_src.is_dir():
        shutil.copytree(cache_src, cache_dst, dirs_exist_ok=True)
    proc = subprocess.run(
        [str(PY), "-m", "interfacetester_ai.cli", "gen", str(doc), "--fake",
         "--case-name-prefix", tag + "__"],
        cwd=dst, capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
    )
    cases = sorted((dst / "cases").glob("*.yml")) if (dst / "cases").is_dir() else []
    print(f"{tag:26} exit={proc.returncode}  用例={len(cases)}")

print("\n现在出表（独立目录）：")
proc = subprocess.run(
    [str(PY), "probe_p0a_llm/make_review_sheets.py"],
    cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
)
print((proc.stdout or "")[-900:], "exit:", proc.returncode)
