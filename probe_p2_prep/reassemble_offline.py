# -*- coding: utf-8 -*-
"""**离线重装配**（不调模型）：拿上次真实复跑的草稿快照，用**当前**装配器再跑一遍。

    用法：python probe_p2_prep/reassemble_offline.py <快照目录> <目标工作区> [--doc <文档>]

为什么要有它（本轮 §9.14 的度量手段）：改了闸门口径（响应侧归属）之后，要看"同样的模型产物
在新口径下会变成什么样" —— 重跑模型要花钱、而且**换了随机性就不可比** ✗；
而 `.ai/draft/<用例>.draft.json` 里存着**模型原始草案**（`{"case","coverage","draft"}`）✓，
拿它过一遍 `assemble()` 就是**同一份输入、不同口径**的对照 ✓。

★它只写目标工作区的 `.ai/`（与产品同一条落盘路径 ✓），不碰 `cases/`。
"""

import argparse
import glob
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

DOC_DEFAULT = os.path.join(BASE, "project-three", "统一文件服务平台-接入应用接口文档.md")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_reassemble.txt")


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="用当前装配器离线重装配草稿快照")
    parser.add_argument("snapshots")
    parser.add_argument("workspace")
    parser.add_argument("--doc", default=DOC_DEFAULT)
    parser.add_argument("--base-url", default="http://127.0.0.1:8899")
    args = parser.parse_args()

    from interfacetester_ai.assembler import assemble  # noqa: PLC0415
    from interfacetester_ai.schema import parse_draft  # noqa: PLC0415

    doc_text = io.open(args.doc, encoding="utf-8", errors="replace").read()
    ws = os.path.abspath(args.workspace)
    os.makedirs(os.path.join(ws, ".ai", "draft"), exist_ok=True)
    os.chdir(ws)

    lines = ["=" * 74, f"快照源：{args.snapshots}", f"工作区：{ws}", f"文档  ：{args.doc}", "=" * 74]
    totals = {"cases": 0, "assertions": 0, "dropped": 0, "ownership": 0, "ok": 0}
    for path in sorted(glob.glob(os.path.join(args.snapshots, "*.draft.json"))):
        try:
            payload = json.load(io.open(path, encoding="utf-8"))
        except (OSError, ValueError) as error:
            lines.append(f"  ✗ {os.path.basename(path)}：读不了快照（{error}）")
            continue
        totals["cases"] += 1
        try:
            draft = parse_draft(payload["draft"])
        except Exception as error:  # noqa: BLE001
            lines.append(f"  ✗ {payload.get('case', '?')}：快照过不了 parse_draft（{error}）")
            continue
        result = assemble(draft, doc_text, base_url=args.base_url)
        coverage = result.coverage
        kinds = {name: count for name, count in dict(coverage.dropped_kinds).items() if count}
        totals["assertions"] += coverage.assertion_count
        totals["dropped"] += sum(kinds.values())
        totals["ownership"] += kinds.get("response-ownership", 0)
        totals["ok"] += 1 if result.ok else 0
        lines.append(
            f"  {'OK ' if result.ok else 'NO '} {draft.case_name:26}"
            f" 断言 {coverage.assertion_count:2} / 覆盖字段 {len(coverage.covered_fields):2}"
            f" / 降级 {sum(kinds.values()):2} {kinds or ''}"
        )
        for unknown in result.unknowns:
            if unknown.kind == "response-ownership":
                lines.append(f"       · {unknown.where} → {unknown.message[:110]}")
    lines.append("")
    lines.append(
        f"合计：{totals['cases']} 份快照 → 装配 {totals['ok']} 成功；"
        f"断言 {totals['assertions']} 条；降级 {totals['dropped']} 条"
        f"（其中 **响应侧归属 {totals['ownership']} 条**）"
    )
    text = "\n".join(lines) + "\n"
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(text)
    print(text)
    print(f"written {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
