# -*- coding: utf-8 -*-
"""校准两处可疑数字（只读）：

1. 第 5 节报的"可选字段 `filename` 却被断言" —— 到底是**误报**，
   还是"同名不同表"（跨接口同名 → 段落归属缺失）？
2. 第 7 节我把人工用例数成 8 条，而文档里写"总计 92" —— 我数错在哪？
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

PROJ = os.path.join(BASE, "project-three")
DOC_IFACE = os.path.join(PROJ, "统一文件服务平台-接入应用接口文档.md")
DOC_CASES = os.path.join(PROJ, "统一文件服务平台-接口测试用例.md")


def read(path):
    with io.open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read()


def main():
    iface, cases = read(DOC_IFACE), read(DOC_CASES)
    lines = []

    def w(text=""):
        lines.append(text)

    w("# 校准（只读）")
    w()

    # ---- ① filename 到底是哪种情况 ----
    w("## 1. `filename` 的行级取证")
    w()
    rows = [r for r in A.read_field_rows(iface) if r.name == "filename"]
    w(f"文档里名为 `filename` 的字段行共 **{len(rows)}** 条（每条的必填列原文与说明）：")
    w()
    for row in rows:
        required, optional = A._truthy_required(row.required_text)
        w(f"- 第 {row.line_no} 行：必填列={row.required_text!r} → (required={required}, optional={optional})")
        w(f"  · 原文：`{row.raw[:110]}`")
    w()
    derived = [d for d in A.derive_assertions(iface)[0] if d.check == "body.filename"]
    w(f"推出的 `body.filename` 断言 {len(derived)} 条：")
    for item in derived:
        w(f"- `{item.comparator} {item.check} = {item.expect!r}`")
        w(f"  · 引用句：`{item.source_quote[:110]}`")
        w(f"  · 行号：{item.line_no}")
    w()
    w("★判定：如果**引用句/行号指向的是标 `是` 的那一行**，那就不是误报，")
    w("而是**跨接口同名字段**（段落归属缺失）——同一个 `body.filename` 路径被两个接口共用。")
    w()

    # ---- ② 人工用例条数怎么数才对 ----
    w("## 2. 人工用例条数的正确数法")
    w()
    for pattern, label in (
        (r"^###\s+TC-", "行首 `### TC-`"),
        (r"^#{2,4}\s+TC-", "行首 `##`~`####` + TC-"),
        (r"TC-[A-Z]+-\d+", "任意位置的 `TC-XXX-000`"),
        (r"\|\s*\*\*用例编号\*\*\s*\|\s*(TC-[^|\s]+)", "表格里的 `**用例编号**` 单元格"),
    ):
        w(f"- {label}：**{len(re.findall(pattern, cases, re.M))}** 个")
    w()
    ids = re.findall(r"\|\s*\*\*用例编号\*\*\s*\|\s*(TC-[^|\s]+)", cases)
    w(f"用例编号去重后：**{len(set(ids))}** 个；前 5：{sorted(set(ids))[:5]}")
    w(f"文档里写\"总计\"的那行：{[l.strip() for l in cases.split(chr(10)) if '总计' in l][:3]}")
    w()

    # ---- ③ 端点在表格里的真实形态（为什么 ENDPOINT_RE 没认出来） ----
    w("## 3. 表格形态端点为什么没被认出来")
    w()
    samples = [
        l.strip()
        for l in iface.split("\n")
        if "**URL**" in l
    ][:3]
    for line in samples:
        w(f"- 原文：`{line}`")
    from interfacetester_ai.gen import ENDPOINT_RE  # noqa: PLC0415

    w(f"- `ENDPOINT_RE` 的表达式：`{ENDPOINT_RE.pattern}`")
    for line in samples:
        w(f"  对上面那一行匹配结果：{ENDPOINT_RE.search(line)}")
    w()

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_calibrate.txt")
    with io.open(out, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    print("已写出 out_calibrate.txt（" + str(len(lines)) + " 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
