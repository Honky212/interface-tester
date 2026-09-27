# -*- coding: utf-8 -*-
"""探：真实文档里"没有必填列"的字段表**分别挂在哪个章节下**（只读）。

用于设计"响应表"的识别规则——识别规则必须能**从文档结构**推出来，不能拍脑袋。
"""

from __future__ import annotations

import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai import assertions as A  # noqa: E402

DOC = os.path.join(BASE, "project-three", "统一文件服务平台-接入应用接口文档.md")
TITLE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def main():
    text = io.open(DOC, encoding="utf-8").read()
    lines = text.split("\n")
    lines_out = ["# 「无必填列」的字段表挂在哪个章节下", ""]

    # 每个表格块 → 它上方的最近标题链
    headings = [(i + 1, len(m.group(1)), m.group(2)) for i, line in enumerate(lines) if (m := TITLE.match(line))]

    def path_for(line_no):
        stack = []
        for number, level, title in headings:
            if number > line_no:
                break
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        return " > ".join(title for _lvl, title in stack)

    i = 0
    no_required = 0
    while i < len(lines):
        if TABLE_ROW.match(lines[i]) and not SEPARATOR.match(lines[i]):
            header = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            body_start = i + 1
            rows = []
            if body_start < len(lines) and SEPARATOR.match(lines[body_start]):
                j = body_start + 1
                while j < len(lines) and TABLE_ROW.match(lines[j]):
                    rows.append(lines[j])
                    j += 1
                if any(A._FIELD_COL_RE.search(h) for h in header) and rows:
                    has_required = A._column_index(header, A._REQUIRED_COL_RE) is not None
                    if not has_required:
                        no_required += 1
                        lines_out.append(
                            f"- 第 {i + 1} 行（{len(rows)} 行数据）：表头 `{' | '.join(header)}`"
                        )
                        lines_out.append(f"  · 所在章节：`{path_for(i + 1)}`")
                i = j
                continue
        i += 1

    lines_out += ["", f"共 **{no_required}** 张「无必填列」的字段表。"]
    lines_out += [
        "",
        "> 识别规则的设计依据：若它们**全部**落在'响应/返回'类章节下，则规则可以写成",
        "> 「所在标题路径含 响应/返回」；否则要靠别的结构线索（表头列名 / 说明语气）。",
    ]

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_no_required_tables.txt")
    with io.open(out, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines_out) + "\n")
    print("已写出 out_no_required_tables.txt（" + str(len(lines_out)) + " 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
