# -*- coding: utf-8 -*-
"""(6b 续) **给三张抽检表打分**：把人工判定折算成比例，并把每次打分**追加**进历史。

## 为什么要有这个脚本（而不是"人自己数"）

"人判错了怎么办"的答案不是"判得更仔细"，而是**判错的成本可回滚、可追溯**：
改一格 → **重跑一次** → 新结论**追加**到历史（旧结论**不重写**）。所以需要三样东西：
1. **判定列可解析**（本脚本按表头里含"人工填"的那一列定位，列序变了也不怕）；
2. **三种标记都认**：`✓` 正确 / `✗` 错误 / **`?` 无法判定**（"证据不足就输出未知"——`?` 不算错也不算对，**单列统计**）；
3. **每次都追加历史**（`SCORES_HISTORY.md`），并写机读结果 `out_review_scores_<模型>.json`。

用法：
    $env:P0A_MODEL="gemma4:latest"
    .\\.venv\\Scripts\\python.exe probe_p0a_llm\\score_review_sheets.py            # 打分（表没填就报"未判"）
    .\\.venv\\Scripts\\python.exe probe_p0a_llm\\score_review_sheets.py --sheet-dir <目录>
"""

import argparse
import datetime
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL = os.environ.get("P0A_MODEL", "gemma4:latest")
SLUG = re.sub(r"[^A-Za-z0-9._-]+", "-", MODEL).strip("-")
SHEET_DIR = os.path.join(BASE, "probe_p0a_llm", "review_sheets", SLUG)

# 三种标记（大小写/全角都要认；`?` 与空 = 未判定）
_OK = {"✓", "√", "v", "y", "yes", "是", "对", "正确", "ok", "pass"}
_BAD = {"✗", "×", "x", "n", "no", "否", "错", "错误", "不正确", "fail"}
_UNKNOWN = {"?", "？", "无法判定", "unknown", "n/a", "na", "不知道"}

_LABEL = re.compile(r"\((A|B|C)\)|#\s*表\s*(A|B|C)")


def _cells(line):
    """按**未转义的** `|` 切单元格。

    ★为什么不能直接 `split("|")`：表 C 的"原文摘录"列里**自己就是 markdown 表格**，
      里面必须写成转义竖线 `\\|`——naive split 会把一列切成好几列，判定列就此错位
      （实测踩到：显示出来的"判定"其实是摘录里的 `类型 \\| 必填`）。所以必须按
      "前面不是反斜杠的 `|`"切。
    """
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def _classify(cell):
    text = (cell or "").strip().strip("`*").strip().lower()
    if not text:
        return "unjudged"
    if text in _UNKNOWN:
        return "unknown"
    if text in _OK:
        return "ok"
    if text in _BAD:
        return "bad"
    # 容错：单元格里夹了别的字（例如 `✓ 但需人工确认`）
    if any(mark in text for mark in ("✓", "√")):
        return "ok"
    if any(mark in text for mark in ("✗", "×")):
        return "bad"
    return "unjudged"


def _rows(path):
    """把一张表里"带判定列"的数据行拆成 `(判定, 整行)`。按**表头**定位判定列。"""
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    header_index = None
    judge_index = None
    for index, line in enumerate(lines):
        if line.startswith("|") and "人工填" in line:
            header_index = index
            cells = _cells(line)
            for position, cell in enumerate(cells):
                if "人工填" in cell:
                    judge_index = position
            break
    if header_index is None or judge_index is None:
        return []
    out = []
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            continue
        cells = _cells(line)
        if len(cells) <= judge_index:
            continue
        out.append((_classify(cells[judge_index]), line))
    return out


def _score(name, path):
    rows = _rows(path)
    counts = {"ok": 0, "bad": 0, "unknown": 0, "unjudged": 0}
    for verdict, _line in rows:
        counts[verdict] += 1
    judged = counts["ok"] + counts["bad"]
    return {
        "sheet": name,
        "rows": len(rows),
        **counts,
        "judged": judged,
        "pass_rate": round(counts["ok"] / judged, 4) if judged else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="给三张抽检表打分（判定列由人填）")
    parser.add_argument("--sheet-dir", default=SHEET_DIR)
    args = parser.parse_args()

    if not os.path.isdir(args.sheet_dir):
        print(f"没有这个抽检表目录：{args.sheet_dir}", file=sys.stderr)
        return 2

    results = []
    for name in ("A", "B", "C"):
        matches = [
            entry
            for entry in sorted(os.listdir(args.sheet_dir))
            if entry.startswith(name + "_") and entry.endswith(".md")
        ]
        if not matches:
            continue
        results.append(_score(name, os.path.join(args.sheet_dir, matches[0])))

    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"模型 {MODEL}；抽检表目录 {os.path.relpath(args.sheet_dir, BASE)}")
    for item in results:
        rate = "—" if item["pass_rate"] is None else f"{item['pass_rate'] * 100:.1f}%"
        print(
            f"  表 {item['sheet']}：共 {item['rows']} 条 | 判对 {item['ok']} / 判错 {item['bad']}"
            f" / 无法判定 {item['unknown']} / **未判 {item['unjudged']}** → 正确率 {rate}"
        )
        if item["sheet"] == "B" and item["judged"]:
            passed = item["ok"] >= 8 and item["judged"] >= 10
            print(
                f"      P0b 口径（抽 10 条、判对 ≥8）：已判 {item['judged']} 条、判对 {item['ok']} 条"
                f" → {'**达标**' if passed else '未达标（或不足 10 条）'}"
            )

    json_path = os.path.join(BASE, "probe_p0a_llm", f"out_review_scores_{SLUG}.json")
    with open(json_path, "w", encoding="utf-8") as fp:
        json.dump({"model": MODEL, "scored_at": stamp, "sheets": results}, fp,
                  ensure_ascii=False, indent=2)

    history = os.path.join(args.sheet_dir, "SCORES_HISTORY.md")
    new_file = not os.path.isfile(history)
    with open(history, "a", encoding="utf-8") as fp:
        if new_file:
            fp.write(
                "# 抽检打分历史（★**只追加，不重写**：判错的处置是「再打一次分」，不是「改上一次」）\n\n"
                "| 打分时刻 | 表 | 条数 | 判对 | 判错 | 无法判定 | 未判 | 正确率 |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            )
        for item in results:
            rate = "—" if item["pass_rate"] is None else f"{item['pass_rate'] * 100:.1f}%"
            fp.write(
                f"| {stamp} | {item['sheet']} | {item['rows']} | {item['ok']} | {item['bad']} | "
                f"{item['unknown']} | {item['unjudged']} | {rate} |\n"
            )
    print(f"机读结果：{os.path.relpath(json_path, BASE)}")
    print(f"历史（追加）：{os.path.relpath(history, BASE)}")
    print(
        "提示：判定列可填 `✓` / `✗` / **`?`（无法判定）**；"
        "`?` 不计入分子分母，单列统计（「证据不足就输出未知」）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
