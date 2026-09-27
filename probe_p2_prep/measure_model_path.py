# -*- coding: utf-8 -*-
"""分析**模型通路**（`haify gen`）在真实文档上的产出（(c) 的测量）。

用法：
    python probe_p2_prep/measure_model_path.py <工作区> <接口文档>

只读。要回答的问题：
1. 模型通路会不会**跳过**那些"端点写在表格里"的接口片？（`SLICE_ENDPOINT_RE` 本来就宽松）
2. 产出几条、每条**是不是自己那一段的字段**？
3. 与确定性通路（① ② 之后：17 段 / 16 条）相比如何？

★归属判据用**引用句逐字比对**（`source_quote in 该接口段文本`），与 T28 同一口径——
不用"字段名集合"（本仓已两次踩到"名字级交叉 ≠ 语义级判定"）。
"""

from __future__ import annotations

import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.gen import interface_sections  # noqa: E402


def read(path):
    with io.open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read()


def main(argv):
    workspace = argv[0] if argv else os.getcwd()
    doc_path = argv[1] if len(argv) > 1 else os.path.join(workspace, "iface.md")
    doc = read(doc_path)
    lines = []

    def w(text=""):
        lines.append(text)

    sections = interface_sections(doc)
    by_url = {item.url: item for item in sections}

    w("# 模型通路在真实文档上的产出（(c) 实测）")
    w()
    w(f"- 接口文档：`{os.path.basename(doc_path)}`（{len(doc)} 字符）")
    w(f"- **按分段口径**该文档有 **{len(sections)}** 个接口段（① ② 之后的确定性通路识别的就是这些）")
    w()

    draft_dir = os.path.join(workspace, ".ai", "draft")
    yamls = sorted(n for n in os.listdir(draft_dir) if n.endswith(".yml")) if os.path.isdir(draft_dir) else []
    jsons = sorted(n for n in os.listdir(draft_dir) if n.endswith(".draft.json")) if os.path.isdir(draft_dir) else []
    w(f"- 产物：草稿 YAML **{len(yamls)}** 条；模型原始草案 JSON **{len(jsons)}** 份")
    w()

    man_dir = os.path.join(workspace, ".ai", "manifest")
    manifests = os.listdir(man_dir) if os.path.isdir(man_dir) else []
    w(f"- 模型调用留痕（`.ai/manifest/`）：**{len(manifests)}** 条")
    w()

    w("## 逐条：端点 / 断言数 / 引用句归属")
    w()
    clean, foreign, unknown_url = 0, 0, 0
    for name in jsons:
        payload = json.loads(read(os.path.join(draft_dir, name)))
        draft = payload.get("draft") or {}
        case = payload.get("case", name)
        steps = draft.get("steps") or []
        step = steps[0] if steps else {}
        url = str(step.get("url", "") or "")
        assertions = step.get("validate") or []
        section = by_url.get(url)
        if section is None:
            unknown_url += 1
            w(f"- `{case}`：url=`{url}` **不在**任何识别出的接口段里")
            w()
            continue
        section_text = "\n".join(doc.split("\n")[section.start_line - 1:section.end_line])
        inside = [a for a in assertions if not a.get("source_quote") or a["source_quote"] in section_text]
        outside = [a for a in assertions if a.get("source_quote") and a["source_quote"] not in section_text]
        if outside:
            foreign += 1
        else:
            clean += 1
        w(f"- `{case}`：`{url}`（段 {section.start_line}~{section.end_line}）")
        w(f"  · 断言 {len(assertions)} 条；引用句**在本段内** {len(inside)} 条")
        if outside:
            w(f"  · ⚠ **段外引用 {len(outside)} 条**（例：`{outside[0]['source_quote'][:70]}`）")
        w()

    w("## 小结")
    w()
    w(f"- 端点能对上接口段的：**{clean + foreign}** 条；对上但是**引用跨段**的：**{foreign}** 条")
    w(f"- 端点**对不上**任何接口段的：**{unknown_url}** 条")
    w()
    w("对照（确定性通路，① ② 之后）：**17 段 → 16 条产出 + 1 条被 S3 拒**。")
    w()

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_model_path.txt")
    with io.open(out, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    print("已写出 out_model_path.txt（" + str(len(lines)) + " 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
