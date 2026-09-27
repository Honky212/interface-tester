# -*- coding: utf-8 -*-
"""细节：模型通路的失败原因 + "跨段引用"断言的具体内容（只读）。"""

from __future__ import annotations

import glob
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.gen import interface_sections  # noqa: E402


def read(path):
    with io.open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read()


def main(argv):
    workspace = argv[0]
    doc = read(os.path.join(workspace, "iface.md"))
    sections = {item.url: item for item in interface_sections(doc)}
    lines = []

    def w(text=""):
        lines.append(text)

    w("# 细节：失败原因 + 跨段引用断言")
    w()

    w("## 1. 闸门拒掉的片（`.ai/failed/*/NEEDS_HUMAN.md`）")
    w()
    for path in sorted(glob.glob(os.path.join(workspace, ".ai", "failed", "*", "NEEDS_HUMAN.md"))):
        text = read(path)
        codes = sorted(set(re.findall(r"\bS\d+\b", text)))
        case = os.path.basename(os.path.dirname(path))
        first = [line.strip() for line in text.split("\n") if line.strip()][:4]
        w(f"- `{case}`：闸门编号 {codes}")
        for line in first[2:]:
            w(f"  · {line[:120]}")
    w()

    w("## 2. 「引用句不在本段」的断言（逐条）")
    w()
    draft_dir = os.path.join(workspace, ".ai", "draft")
    for name in sorted(os.listdir(draft_dir)):
        if not name.endswith(".draft.json"):
            continue
        payload = json.loads(read(os.path.join(draft_dir, name)))
        draft = payload.get("draft") or {}
        case = payload.get("case", name)
        steps = draft.get("steps") or []
        step = steps[0] if steps else {}
        url = str(step.get("url", "") or "")
        section = sections.get(url)
        if section is None:
            continue
        section_text = "\n".join(doc.split("\n")[section.start_line - 1:section.end_line])
        for assertion in step.get("validate") or []:
            quote = assertion.get("source_quote") or ""
            if quote and quote not in section_text:
                w(f"- `{case}` / `{url}`：")
                w(f"  · 断言：`{assertion.get('comparator')} {assertion.get('check')} = {assertion.get('expect')!r}`")
                w(f"  · 引用句：`{quote[:100]}`")
                where = [n for n, line in enumerate(doc.split("\n"), start=1) if quote.strip() and quote.strip() in line]
                w(f"  · 该引用句在原文的行号：**{where[:3]}**（本段 {section.start_line}~{section.end_line}）")
    w()

    w("## 3. 断言丰富度对照（同一批接口段）")
    w()
    total = 0
    for name in sorted(os.listdir(draft_dir)):
        if not name.endswith(".draft.json"):
            continue
        payload = json.loads(read(os.path.join(draft_dir, name)))
        steps = (payload.get("draft") or {}).get("steps") or []
        total += len((steps[0] if steps else {}).get("validate") or [])
    w(f"- 模型通路（18 条草稿，含被拒的）断言合计：**{total}** 条")
    w("- 确定性通路（①② 之后）：**约 24 条**（16 段 × 每段 1~3 条）")
    w()

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_model_detail.txt")
    with io.open(out, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    print("已写出 out_model_detail.txt（" + str(len(lines)) + " 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
