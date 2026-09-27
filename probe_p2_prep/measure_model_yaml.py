# -*- coding: utf-8 -*-
"""核对：**模型原始草案**的断言 vs **最终 YAML** 里剩下的断言（只读）。

★为什么要核：分析脚本读的是 `.draft.json`（模型原始输出），而 T28 会在装配时把
"引用句不在文档里 / 字段名不在引用句里"的断言**降级掉**。只看原始草案会把"已被拦下的"
当成"产出的"——那是**读中间产物**的错（本仓踩过不止一次）。
"""

from __future__ import annotations

import glob
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester.loader import load_test_file  # noqa: E402


def read(path):
    with io.open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read()


def main(argv):
    workspace = argv[0]
    draft_dir = os.path.join(workspace, ".ai", "draft")
    lines = ["# 原始草案 vs 最终 YAML（断言条数）", ""]

    raw_total, final_total = 0, 0
    for name in sorted(os.listdir(draft_dir)):
        if not name.endswith(".draft.json"):
            continue
        payload = json.loads(read(os.path.join(draft_dir, name)))
        case = payload.get("case", name[:-11])
        steps = (payload.get("draft") or {}).get("steps") or []
        raw = len((steps[0] if steps else {}).get("validate") or [])
        raw_total += raw

        yaml_path = os.path.join(draft_dir, case + ".yml")
        if not os.path.exists(yaml_path):
            lines.append(f"- `{case}`：原始 **{raw}** 条；**无 YAML**（没走到落盘）")
            continue
        try:
            case_dict = load_test_file(yaml_path)
            final = sum(
                len(step.get("validate") or []) for step in case_dict.get("teststeps") or []
            )
        except Exception as error:  # noqa: BLE001
            lines.append(f"- `{case}`：原始 **{raw}** 条；YAML 读不了（{type(error).__name__}）")
            continue
        final_total += final
        mark = "  ← 被 T28 降级了 " + str(raw - final) + " 条" if final < raw else ""
        lines.append(f"- `{case}`：原始 **{raw}** 条 → 最终 **{final}** 条{mark}")

    lines += [
        "",
        f"合计：原始 **{raw_total}** 条 → 最终 **{final_total}** 条"
        f"（T28 拦下 **{raw_total - final_total}** 条）",
        "",
        "> 对照：确定性通路（① ② 之后）产出约 **24** 条断言（16 段 × 每段 1~3 条）。",
    ]

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_model_yaml.txt")
    with io.open(out, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    print("已写出 out_model_yaml.txt（" + str(len(lines)) + " 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
