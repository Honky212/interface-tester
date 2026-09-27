# -*- coding: utf-8 -*-
"""第二档立项前的**需求探明**（b）：真实文档结构探查。

只读：不改任何产物；报告落 `probe_p2_prep/out_inspect.txt`（本仓 probe_* 目录是取证惯例）。

★`.env` **只列变量名**，绝不打印值（凭据纪律）。
"""

from __future__ import annotations

import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

PROJ = os.path.join(BASE, "project-three")
DOC_IFACE = os.path.join(PROJ, "统一文件服务平台-接入应用接口文档.md")
DOC_CASES = os.path.join(PROJ, "统一文件服务平台-接口测试用例.md")
ENV_FILE = os.path.join(PROJ, ".env")

TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def read(path):
    with io.open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read()


def headers(text):
    """所有表格的表头（跳过分隔行）。"""
    out = []
    for line in text.split("\n"):
        if TABLE_ROW.match(line) and not SEPARATOR.match(line):
            out.append([c.strip() for c in line.strip().strip("|").split("|")])
    return out


def report():
    lines = []

    def w(text=""):
        lines.append(text)

    w("# 真实文档结构探查（第二档需求探明 · b）")
    w()
    w(f"- 接口文档：`{os.path.relpath(DOC_IFACE, BASE)}`（{os.path.getsize(DOC_IFACE)} 字节）")
    w(f"- 人工用例：`{os.path.relpath(DOC_CASES, BASE)}`（{os.path.getsize(DOC_CASES)} 字节）")
    w()

    iface = read(DOC_IFACE)
    cases = read(DOC_CASES)

    # ---- 接口文档：标题骨架 ----
    w("## 1. 接口文档的标题骨架（前 40 条）")
    w()
    w("```")
    count = 0
    for line in iface.split("\n"):
        if re.match(r"^#{1,6}\s", line):
            w(line.strip())
            count += 1
            if count >= 40:
                break
    w("```")
    w()

    # ---- 接口文档：所有表头 ----
    w("## 2. 接口文档里的**所有表头**（盲区是否触发的直接证据）")
    w()
    hs = headers(iface)
    w(f"共 {len(hs)} 张表。逐张列出（★标出含反引号或落到词表盲区的表头）：")
    w()
    for index, header in enumerate(hs, 1):
        joined = " | ".join(header)
        flags = []
        if "`" in joined:
            flags.append("★反引号")
        if any(re.search(r"必选|是否|选填", h) for h in header):
            flags.append("★assertions 认、doc_quality 不认的必填词")
        if any(re.search(r"必需|optional", h, re.I) for h in header):
            flags.append("★doc_quality 认、assertions 不认的必填词")
        mark = ("　" + "，".join(flags)) if flags else ""
        w(f"{index:>2}. {joined}{mark}")
    w()

    # ---- 接口文档：端点行 ----
    w("## 3. 接口文档里的端点行（`METHOD /path`）")
    w()
    endpoints = [
        line.strip()
        for line in iface.split("\n")
        if re.search(r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b\s+[`/]", line)
    ]
    w(f"共 {len(endpoints)} 行；前 30 条：")
    w()
    for line in endpoints[:30]:
        w(f"- {line[:120]}")
    w()

    # ---- 人工用例文档：结构 ----
    w("## 4. 人工用例文档的结构（它是这次对照的**人工 oracle**）")
    w()
    w("```")
    count = 0
    for line in cases.split("\n"):
        if re.match(r"^#{1,6}\s", line) or line.strip().startswith("| "):
            w(line.strip()[:140])
            count += 1
            if count >= 45:
                break
    w("```")
    w()

    w("### 4b. 人工用例文档里的表头")
    w()
    for index, header in enumerate(headers(cases), 1):
        w(f"{index:>2}. {' | '.join(header)}")
    w()

    # ---- .env：只列变量名 ----
    w("## 5. `.env` 的**变量名**（★值一律不打印——凭据纪律）")
    w()
    if os.path.exists(ENV_FILE):
        names = []
        for line in read(ENV_FILE).split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            names.append(stripped.split("=", 1)[0].strip())
        w(f"共 {len(names)} 个变量：")
        for name in names:
            w(f"- `{name}`")
    else:
        w("（文件不存在）")
    w()

    with io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_inspect.txt"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    print("已写出 out_inspect.txt（" + str(len(lines)) + " 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(report())
