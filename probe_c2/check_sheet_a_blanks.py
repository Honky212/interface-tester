# -*- coding: utf-8 -*-
"""只读：查出新表 A 里**判定列为空**的行 + 统计已填标记。"""

import pathlib
import re

path = pathlib.Path(
    r"D:\tester\probe_p0a_llm\review_sheets\gemma4-latest_mergedfix\A_r11_assertion_review.md"
)
lines = path.read_text(encoding="utf-8").splitlines()
rows = []
for line in lines:
    if not re.match(r"^\|\s*\d+\s*\|", line):
        continue
    # ★引用句里有**转义竖线** `\|`（字段表整行），必须用"前面没有反斜杠的竖线"切
    cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
    rows.append(cells)

print(f"表 A 数据行 = {len(rows)} 行（每行单元格数应为 8）")
widths = {len(cells) for cells in rows}
print("单元格数分布:", widths)

blank = [cells for cells in rows if len(cells) >= 7 and cells[6] not in ("✓", "✗", "?")]
print(f"判定列为空 = {len(blank)} 行")
for cells in blank:
    print(f"    #{cells[0]}  {cells[3][:46]}  引用句={cells[4][:40]}")

marks = {}
for cells in rows:
    mark = cells[6] if len(cells) >= 7 else "(行结构异常)"
    marks[mark] = marks.get(mark, 0) + 1
print("\n标记分布:", marks)

print("\n--- 第 24~28 行原样 ---")
for line in lines:
    if re.match(r"^\|\s*2[4-8]\s*\|", line):
        print("  ", line[:150])
