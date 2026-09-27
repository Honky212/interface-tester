# -*- coding: utf-8 -*-
"""演示"判错怎么办"的**可执行流程**：判 → 打分 → 改一格 → 再打分 → 历史追加（旧结论不重写）。

★这是一个**流程自测**：它不替人判定，只是把"填表 → 打分 → 修正"这条链路跑通并留痕。
   标记语义：`✓` 判对 / `✗` 判错 / **`?` 无法判定**（不计入分子分母）。
用法：.\\\\.venv\\\\Scripts\\\\python.exe probe_p0a_llm\\\\demo_score_flow.py
"""

import os
import re
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.environ.get("P0A_MODEL", "gemma4:latest")
SLUG = re.sub(r"[^A-Za-z0-9._-]+", "-", MODEL).strip("-")
SRC = os.path.join(BASE, "probe_p0a_llm", "review_sheets", SLUG)
DEMO = os.path.join(BASE, ".tmp_sheet_demo")
MARKS = ["✓", "✓", "✗", "?"]  # 循环注入：判对 / 判错 / 无法判定


def _unwrap(line):
    """按**未转义的** `|` 切单元格（同 `score_review_sheets.py`：摘录列里有 `\\|`）。"""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def _fill(path, pattern=MARKS):
    """把判定列填上演示标记（按表头里含"人工填"的那一列定位）。"""
    lines = open(path, encoding="utf-8").read().splitlines()
    judge = None
    start = 0
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = _unwrap(line)
        position = next((i for i, cell in enumerate(cells) if "人工填" in cell), None)
        if position is not None:
            judge = position
            start = index + 2
            break
    if judge is None:
        return 0
    filled = 0
    for number in range(start, len(lines)):
        if not lines[number].startswith("|"):
            continue
        cells = _unwrap(lines[number])
        cells[judge] = f" {pattern[filled % len(pattern)]} "
        lines[number] = "|" + "|".join(cells) + "|"
        filled += 1
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return filled


def _run_scorer(sheet_dir):
    proc = subprocess.run(
        [sys.executable, os.path.join("probe_p0a_llm", "score_review_sheets.py"),
         "--sheet-dir", sheet_dir],
        cwd=BASE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "P0A_MODEL": MODEL},
        check=False,
    )
    return proc.returncode, proc.stdout


def main() -> int:
    if not os.path.isdir(SRC):
        print(f"没有抽检表目录：{SRC}", file=sys.stderr)
        return 2
    shutil.rmtree(DEMO, ignore_errors=True)
    os.makedirs(DEMO, exist_ok=True)
    for name in os.listdir(SRC):
        if name.endswith(".md"):
            shutil.copy2(os.path.join(SRC, name), os.path.join(DEMO, name))

    filled = {name: _fill(os.path.join(DEMO, name)) for name in sorted(os.listdir(DEMO))}
    print(f"注入演示标记（判定列）：{filled}")

    code, out = _run_scorer(DEMO)
    print("=== 第一次打分 ===")
    print(out.strip())
    if code != 0:
        return code

    # ★模拟"人发现自己判错了一格"：把表 B 的第一条 ✗ 改成 ✓，再打一次分
    target = os.path.join(DEMO, "B_unknowns_review.md")
    text = open(target, encoding="utf-8").read()
    changed = text.replace(" ✗ ", " ✓ ", 1)
    open(target, "w", encoding="utf-8").write(changed)
    print("\n（人工发现表 B 第 1 条判错 → 改一格 ✗→✓）")

    code, out = _run_scorer(DEMO)
    print("=== 第二次打分（改一格后重跑）===")
    print(out.strip())

    history = os.path.join(DEMO, "SCORES_HISTORY.md")
    print("\n=== 历史（★只追加，旧结论不重写）===")
    print(open(history, encoding="utf-8").read().strip())
    print(f"\n演示目录：{os.path.relpath(DEMO, BASE)}（可删）")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
