# -*- coding: utf-8 -*-
"""验证"低垂果实"是否真能修掉烟枪级缺陷（**只读原型，不是改动**）。

假设（待验证）：
- 现在 `--assertions-only` 是「整篇 → 1 条用例」（`extract_endpoint(全篇)` + `to_draft_payload(全篇)`）；
- 若改成「**按接口段落逐片** → 每段一条用例」，则：
  ① 端点不再取错（每段自己的 `METHOD /path`）；
  ② 断言不再跨段混装（每段只推导自己那段）。
- 前提：`ENDPOINT_RE` 要能认**表格单元格**里的端点（现在认不出，见 out_calibrate.txt §3）。

本脚本只**测量**这个假设成立与否，不改任何生产代码。
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
from interfacetester_ai.slicer import split_document  # noqa: E402

DOC = os.path.join(BASE, "project-three", "统一文件服务平台-接入应用接口文档.md")

# 「表格形态」的端点：`| **URL** | `POST /api/x` |`（现在 ENDPOINT_RE 认不出）
TABLE_ENDPOINT_RE = re.compile(
    r"\*\*URL\*\*\s*\|\s*[`*]*\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+([^\s`|*]+)",
    re.I,
)
PLAIN_ENDPOINT_RE = re.compile(
    r"^\s*[-*#>\s]*\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b\s+(\S+)", re.M
)


def read(path):
    with io.open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read()


def endpoint_of(text):
    hit = TABLE_ENDPOINT_RE.search(text) or PLAIN_ENDPOINT_RE.search(text)
    if not hit:
        return "", ""
    return hit.group(1).upper(), hit.group(2).strip("`* ")


def main():
    text = read(DOC)
    lines = []

    def w(t=""):
        lines.append(t)

    slices = split_document(text)
    w("# 逐片原型的量化验证（只读；不是改动）")
    w()
    w(f"- `split_document()` 切出 **{len(slices)}** 片")
    w()

    with_endpoint = []
    for item in slices:
        method, url = endpoint_of(item.text)
        if url:
            with_endpoint.append((item, method, url))

    w(f"## 1. 端点：**{len(with_endpoint)}** 片能取到端点（现状：整篇只取到 1 个）")
    w()
    for item, method, url in with_endpoint:
        w(f"- `{method} {url}`　←　片标题：{item.title_path}（行 {item.start_line}~{item.end_line}）")
    w()

    w("## 2. 断言归属：每片自己的字段断言（验证「不再混装」）")
    w()
    path_in = []
    files_in = []
    for item, method, url in with_endpoint:
        derived, skipped = A.derive_assertions(item.text)
        checks = sorted({d.check for d in derived})
        if any(c.endswith("path") for c in checks):
            path_in.append(url)
        if any(c.endswith("files") for c in checks):
            files_in.append(url)
        # ★行号**坐标系必须统一**：`derive_assertions(片文本)` 给的是**片内相对**行号，
        #   而 `Slice.start_line` 是**全篇**绝对行号 → 要加上 `start_line - 1` 才能比。
        #   （第一次测量我把两者直接比，得到清一色 False —— 那是**测量口径错**，不是缺陷。）
        offset = item.start_line - 1
        inside = [
            offset + d.line_no
            for d in derived
            if d.line_no and item.start_line <= offset + d.line_no <= item.end_line
        ]
        total_with_line = [d for d in derived if d.line_no]
        w(f"- `{method} {url}`：断言 **{len(derived)}** 条 / 字段 {len(checks)} 个"
          f"（跳过 {len(skipped)}）")
        w(f"  · 字段：{checks[:10]}")
        w(f"  · 带行号的断言 {len(total_with_line)} 条，其中**落在本片内** {len(inside)} 条")
    w()
    w("### 2b. 那两个字段现在落在哪几片")
    w()
    w(f"- `body.path` 出现在：**{len(path_in)}** 片 → {path_in}")
    w(f"- `body.files` 出现在：**{len(files_in)}** 片 → {files_in}")
    w()
    w("> ★注意：`body.path` 出现在 15 片**不是缺陷**——文档里 **15 个接口的请求体各自都有**")
    w("> 一个叫 `path` 的字段（它是每个接口自己的路径参数）。逐片之后它们各自归属正确；")
    w("> 整篇通路的问题恰恰是「把它们全塞进同一条用例」。")
    w("> （★测量教训：**名字级交叉 ≠ 语义级判定**——本报告已两次踩到同一坑。）")
    w()

    w("## 3. `/oauth2/token` 那一段自己有没有字段断言")
    w()
    token_slice = next((i for i in slices if "/oauth2/token" in i.text), None)
    if token_slice is None:
        w("- （文档里没有含 `/oauth2/token` 的片）")
    else:
        derived, skipped = A.derive_assertions(token_slice.text)
        w(f"- 片标题：{token_slice.title_path}；断言 **{len(derived)}** 条")
        w(f"- 字段：{sorted({d.check for d in derived})}")
    w()

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_prototype.txt")
    with io.open(out, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    print("已写出 out_prototype.txt（" + str(len(lines)) + " 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
