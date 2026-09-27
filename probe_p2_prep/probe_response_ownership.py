# -*- coding: utf-8 -*-
"""探针：**响应侧归属**规则会摘掉哪些断言？（先量再改，避免改完才发现误伤 golden）

规则草案（本轮要落地的"归属闸门"）：

- **响应侧字段名** = ①无「必填」列的字段表里的字段（B2 口径：那是响应数据表）
  ∪ ②响应说明句（`- \\`data.total\\`：整数，总条数`）里的字段 ∪ ③**响应示例** JSON 块里的键；
- `body.X` 形式的断言：`X` 不在上面这个集合里 → **摘掉**（它只写在请求侧，照文档实现的服务不返回它）。

本探针只**读**、只**统计**，不改任何代码/产物。跑法：

    python probe_p2_prep/probe_response_ownership.py
"""

import glob
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.assertions import (  # noqa: E402
    derive_assertions,
    derive_from_response_notes,
    derive_from_response_tables,
)

RESPONSE_HEAD_RE = re.compile(r"响应|返回|response|result", re.IGNORECASE)
FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.S)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_response_ownership.txt")


def _all_keys(payload, out=None):
    """示例 JSON 里的**所有**键（含嵌套）——`body.data.userId` 的末段要能对上 `userId`。"""
    if out is None:
        out = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            out.add(str(key))
            _all_keys(value, out)
    elif isinstance(payload, list):
        for item in payload:
            _all_keys(item, out)
    return out


def response_example_keys(doc_text):
    """**响应示例**（围栏前 200 字符里出现"响应/返回/response"）里的 JSON 键。"""
    keys = set()
    for match in FENCE_RE.finditer(doc_text):
        head = doc_text[: match.start()][-200:]
        if not RESPONSE_HEAD_RE.search(head):
            continue
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            continue
        keys |= _all_keys(payload)
    return keys


def response_field_names(doc_text):
    names = set()
    for item in derive_from_response_tables(doc_text)[0]:
        names |= set(item.covered_fields)
    for item in derive_from_response_notes(doc_text)[0]:
        names.add(item.check.split(".")[-1])
    names |= response_example_keys(doc_text)
    return {name for name in names if name}


def survey(name, doc_text, assertions):
    """一份文档：哪些 body.X 保得住、哪些会被摘掉。"""
    response_names = response_field_names(doc_text)
    kept, dropped, other = [], [], []
    for item in assertions:
        parts = item.check.split(".")
        if parts[0] == "body" and len(parts) > 1:
            if parts[-1] in response_names:
                kept.append(item.check)
            else:
                dropped.append(item.check)
        else:
            other.append(item.check)
    return {
        "name": name,
        "response_names": sorted(response_names),
        "kept": kept,
        "dropped": dropped,
        "other": other,
    }


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    import glob as _glob

    lines = []
    rows = []

    docs = sorted(_glob.glob(os.path.join(BASE, "bench", "golden", "*.md")))
    docs.append(os.path.join(BASE, "project-three", "统一文件服务平台-接入应用接口文档.md"))
    for path in docs:
        if not os.path.isfile(path):
            continue
        doc_text = io.open(path, encoding="utf-8", errors="replace").read()
        assertions, _skipped = derive_assertions(doc_text)
        rows.append(survey(os.path.basename(path), doc_text, assertions))

    lines.append("=" * 78)
    lines.append("A. 确定性通路（`derive_assertions`）—— 归属闸门会摘掉哪些 `body.X`")
    lines.append("=" * 78)
    for row in rows:
        lines.append(
            f"\n{row['name']}"
            f"\n  响应侧字段名（{len(row['response_names'])}）：{', '.join(row['response_names'][:14])}"
            f"\n  保住 body.X（{len(row['kept'])}）：{', '.join(row['kept'][:10])}"
            f"\n  **摘掉**（{len(row['dropped'])}）：{', '.join(row['dropped'][:10])}"
            f"\n  其他（{len(row['other'])}）：{', '.join(row['other'][:6])}"
        )

    draft_dir = os.path.join(os.environ.get("TEMP", "/tmp"), "ai_real_run", ".ai", "draft")
    lines.append("\n" + "=" * 78)
    lines.append("B. 模型通路（那次真实复跑的 17 份 YAML）—— 逐条 `- check:` 归类")
    lines.append("=" * 78)
    doc_path = os.path.join(BASE, "project-three", "统一文件服务平台-接入应用接口文档.md")
    response_names = set()
    if os.path.isfile(doc_path):
        response_names = response_field_names(
            io.open(doc_path, encoding="utf-8", errors="replace").read()
        )
    lines.append(f"响应侧字段名（真实文档）：{', '.join(sorted(response_names)) or '（无）'}")
    total_kept = total_dropped = 0
    for path in sorted(_glob.glob(os.path.join(draft_dir, "*.yml"))):
        text = io.open(path, encoding="utf-8", errors="replace").read()
        checks = re.findall(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_.]*)\s*$", text, re.M)
        kept, dropped = [], []
        for check in checks:
            (kept if check.split(".")[-1] in response_names else dropped).append(check)
        total_kept += len(kept)
        total_dropped += len(dropped)
        lines.append(
            f"  {os.path.basename(path):28} 保住 {len(kept):2} / 摘掉 {len(dropped):2}"
            f"   摘掉者：{', '.join(sorted(set(dropped))[:6])}"
        )
    lines.append(f"\n  合计：保住 {total_kept} / 摘掉 {total_dropped}")

    text = "\n".join(lines) + "\n"
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(text)
    print(text)
    print(f"written {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

