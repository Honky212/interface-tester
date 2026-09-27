# -*- coding: utf-8 -*-
"""把 mock 的**错误条件引擎**推出来的东西全摊开看（§9.22 的口径自检）。

    python probe_p2_prep/probe_error_rules.py [文档.md] [--out 报告.txt]

为什么要有它：引擎的**覆盖**很容易"看起来变好"（数字变大），所以报告必须把三件事同时摊开——
① **每族的条数**（可模拟的是哪几族）、② **每条规则的绑定**（绑到哪个字段/阈值多少，肉眼可查）、
③ **不模拟的逐条点名**（含错误码却判不了的条件，一条都不许静默消失）。

★它只读文档、只调探针模块的**纯函数**，不起服务、不发请求、不写产品目录。
"""

import argparse
import io
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "probe_p2_prep"))
sys.path.insert(0, BASE)

import mock_file_service as mock  # noqa: E402


def describe(rule):
    """一条规则的**人话**（肉眼看绑定对不对，比看数字重要）。"""
    kind = rule.get("kind", "relation")
    if kind == "relation":
        return f"{kind}：{rule['left']} {rule['op']} {rule['right']} → {rule['code']}"
    if kind == "required":
        return f"{kind}：缺 `{rule['field']}`（必填）→ {rule['code']}"
    if kind == "limit":
        unit = "个" if rule["unit"] == "count" else "字节"
        return (
            f"{kind}：`{rule['field']}` 超过 {rule['threshold']:g}{unit}"
            f"{'（逐个）' if rule.get('per_item') else ''} → {rule['code']}"
        )
    if kind == "exist":
        return f"{kind}：{rule['fields']} 不在种子数据里 → {rule['code']}"
    if kind == "format":
        return f"{kind}：`{rule['field']}` 违反「{rule['check']}」→ {rule['code']}"
    return f"{kind}：{rule}"


def main():
    parser = argparse.ArgumentParser(description="错误条件引擎的口径自检")
    parser.add_argument("doc", nargs="?", default=mock.DOC_DEFAULT)
    parser.add_argument("--out", default=os.path.join(BASE, "probe_p2_prep", "out_error_rules.txt"))
    args = parser.parse_args()

    doc = io.open(args.doc, encoding="utf-8", errors="replace").read()
    rules_by_route, unbound = mock.build_error_rules(doc)
    known = mock.build_known_values(doc)

    lines = [
        "=" * 74,
        f"文档：{os.path.basename(args.doc)}",
        "=" * 74,
    ]
    by_kind = {}
    for route, items in sorted(rules_by_route.items()):
        for rule in items:
            by_kind.setdefault(rule.get("kind", "relation"), []).append(rule)
    total = sum(len(items) for items in by_kind.values())
    lines.append(f"\n① 可模拟：**{total} 条**，分族如下（通用性的边界在**族**，不在项目）")
    for kind, items in sorted(by_kind.items()):
        lines.append(f"  {kind:9} {len(items):3} 条")

    lines.append("\n② 每条规则的**绑定**（绑错字段/阈值就是 bug，一眼可见）")
    for kind, items in sorted(by_kind.items()):
        lines.append(f"  --- {kind} ---")
        for rule in items:
            lines.append(f"    · {describe(rule)}")
            lines.append(f"      原句：{rule.get('quote', '')[:88]}")

    statements = [item for item in unbound if mock.is_condition_statement(item[1])]
    fragments = [item for item in unbound if not mock.is_condition_statement(item[1])]
    lines.append(
        f"\n③ 不模拟：**{len(unbound)} 条** = **条件陈述 {len(statements)} 条**"
        f"（能力边界 —— 要状态/业务状态，或请求里给不出事实）+ 示例碎片 {len(fragments)} 条"
        "（JSON 里的错误码行，**不是**条件陈述）"
    )
    lines.append("  ③-a 条件陈述（逐条点名，判不了的原因只有两类：要状态 / 请求里给不出事实）")
    for where, sentence in statements:
        lines.append(f"    · {where}：{sentence[:88]}")
    lines.append("  ③-b 示例碎片（必然『不模拟』，列出来只为**不静默消失**）")
    for where, sentence in fragments[:6]:
        lines.append(f"    · {where}：{sentence[:60]}")
    if len(fragments) > 6:
        lines.append(f"    · …（共 {len(fragments)} 条）")

    lines.append(f"\n④ 种子数据（存在性族的判据）：{len(known)} 个")
    sample = sorted(known)[:12]
    lines.append("    " + "，".join(sample) + ("…" if len(known) > 12 else ""))

    text = "\n".join(lines) + "\n"
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(text)
    print(text)
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
