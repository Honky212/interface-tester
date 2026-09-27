# -*- coding: utf-8 -*-
"""批次 5 的实体证据：**C-2 前后产物的断言构成**（协议级 vs 字段级）。只读。"""

import json
import pathlib
import re

BASE = pathlib.Path(r"D:\tester")
PROTOCOL = {"status_code"}
PAIRS = (("C-2 之前", BASE / ".tmp_p0a_run_gemma4-latest_preC2"),
         ("C-2 之后", BASE / ".tmp_p0a_run_gemma4-latest"))
_CHECK_RE = re.compile(r"^\s*-\s*(\w+)\s*:\s*\[\s*([^,\]]+)")


def scan(root: pathlib.Path):
    files = sorted(path for path in root.rglob("*.yml") if path.parent.name == "cases")
    protocol = field = 0
    detail = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        checks = [m.group(2).strip().strip("'\"") for m in _CHECK_RE.finditer(text)]
        p = sum(1 for c in checks if c in PROTOCOL)
        f = len(checks) - p
        protocol += p
        field += f
        detail.append((path.name, len(checks), p, f))
    return len(files), protocol, field, detail


for label, root in PAIRS:
    if not root.exists():
        print(f"{label}: 目录不存在 {root}")
        continue
    count, protocol, field, detail = scan(root)
    print(f"\n=== {label} ===  用例 {count} 条 | 断言：协议级 {protocol} + 字段级 {field}")
    for name, total, p, f in detail:
        print(f"    {name:58} 断言 {total:>2}（协议 {p} / 字段 {f}）")

# C-2 之后每篇的审计留痕里有没有合并 note
manifest = BASE / ".ai" / "manifest"
print(f"\n（参考）工作区 .ai/manifest 留痕条数 = {len(list(manifest.glob('*.json'))) if manifest.exists() else 0}")
