# -*- coding: utf-8 -*-
"""第二档立项前的**需求探明**（b）主测量：真实文档上"断言缺失/误判"的量化。

输入（`project-three/`，客户真实文档）：
- `统一文件服务平台-接入应用接口文档.md`（接口文档，37.6KB）
- `统一文件服务平台-接口测试用例.md`（**人工写的 92 条用例** = 本次对照的 oracle，47.8KB）

只读；报告落 `probe_p2_prep/out_measure.txt`。不调模型（确定性通路）。
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
from interfacetester_ai import doc_quality as D  # noqa: E402
from interfacetester_ai.gen import ENDPOINT_RE, extract_endpoint  # noqa: E402

PROJ = os.path.join(BASE, "project-three")
DOC_IFACE = os.path.join(PROJ, "统一文件服务平台-接入应用接口文档.md")
DOC_CASES = os.path.join(PROJ, "统一文件服务平台-接口测试用例.md")

TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
SECTION = re.compile(r"^###\s+(.*)$", re.M)
URL_CELL = re.compile(r"\*\*URL\*\*")
STOPWORDS = {
    "true", "false", "null", "and", "or", "not", "the", "http", "https", "bearer",
    "application", "json", "content", "type", "id", "url", "get", "post", "put", "delete",
    "yyyy", "mm", "dd", "hh", "ss", "utf", "z", "e", "g", "p", "tc",
}


def read(path):
    with io.open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read()


def tables(text):
    """把文档切成"表格块"，每块返回 `(表头行, [数据行])`。"""
    blocks = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if TABLE_ROW.match(lines[i]) and not SEPARATOR.match(lines[i]):
            head, body = lines[i], []
            j = i + 1
            if j < len(lines) and SEPARATOR.match(lines[j]):
                j += 1
                while j < len(lines) and TABLE_ROW.match(lines[j]):
                    body.append(lines[j])
                    j += 1
                blocks.append((head, body))
                i = j
                continue
        i += 1
    return blocks


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def classify(skip_reasons):
    """跳过原因归类（按词表）。"""
    buckets = {"字段可选": 0, "必填列没写清": 0, "类型不明确/认不出": 0, "条件返回": 0,
               "枚举没给全": 0, "列表[]路径": 0, "其他": 0}
    for reason in skip_reasons:
        if "字段可选" in reason:
            buckets["字段可选"] += 1
        elif "必填列没写清" in reason:
            buckets["必填列没写清"] += 1
        elif "不断言类型" in reason:
            buckets["类型不明确/认不出"] += 1
        elif "条件返回" in reason:
            buckets["条件返回"] += 1
        elif "没给全" in reason:
            buckets["枚举没给全"] += 1
        elif "[]" in reason:
            buckets["列表[]路径"] += 1
        else:
            buckets["其他"] += 1
    return buckets
def main():
    iface, cases = read(DOC_IFACE), read(DOC_CASES)
    lines = []

    def w(text=""):
        lines.append(text)

    w("# 需求探明（b）：真实文档上的「断言缺失 / 误判」量化")
    w()
    w("> 只读测量，不调模型。样本 = `project-three/` 的客户真实文档。")
    w()

    # ① 端点识别（真实形态：URL 写在表格单元格里）
    w("## 1. 端点识别（★真实文档把 URL 写在表格里）")
    w()
    method, url = extract_endpoint(iface)
    w(f"- 整篇 `extract_endpoint()` → `{method or '(空)'} {url or '(空)'}`")
    table_urls = ENDPOINT_RE.findall(
        "".join(line for line in iface.split("\n") if URL_CELL.search(line))
    )
    w(f"- 表格形态 `| **URL** | \\`METHOD /path\\` |` 的端点共 **{len(table_urls)}** 个")
    sections = list(SECTION.finditer(iface))
    found, missing = 0, []
    for index, match in enumerate(sections):
        end = sections[index + 1].start() if index + 1 < len(sections) else len(iface)
        body = iface[match.end():end]
        if ENDPOINT_RE.search(body):
            found += 1
        elif len(body) > 400:
            missing.append(match.group(1).strip())
    w(f"- 逐 `###` 小节（共 {len(sections)} 节）：**{found} 节能取到端点**")
    w(f"  {len(missing)} 节取不到（>400 字符且无端点）：{missing[:8]}")
    w()

    # ② 体检闸
    w("## 2. 体检闸（`doc_quality.audit_document`）")
    w()
    report = D.audit_document(DOC_IFACE)
    w(f"- findings 共 {len(report.findings)} 条；REJECT {len(report.rejects)} 条")
    for finding in report.findings[:12]:
        message = getattr(finding, "message", str(finding))
        severity = getattr(finding, "severity", "")
        w(f"  · [{severity}] {message[:120]}")
    w()

    # ③ 断言推导
    w("## 3. 断言推导（`assertions.derive_assertions`，确定性通路）")
    w()
    derived, skipped = A.derive_assertions(iface)
    comparators = sorted({d.comparator for d in derived})
    w(f"- 推出断言 **{len(derived)}** 条；跳过/不断言 **{len(skipped)}** 条")
    w(f"- 算子分布：{ {c: sum(1 for d in derived if d.comparator == c) for c in comparators} }")
    w(f"- 覆盖字段：{sorted({d.check for d in derived})[:20]}")
    w(f"- 跳过原因分类：{classify(skipped)}")
    w()
    w("跳过原因样本（前 10 条）：")
    w()
    for item in skipped[:10]:
        w(f"- {item[:150]}")
    w()

    # ④ "缺必填列"的后果（真实文档的响应字段表就是这种）
    w("## 4. ★「必填列缺失」的后果（响应字段表全中招）")
    w()
    no_required, with_required, per_table = 0, 0, []
    for head, body in tables(iface):
        header_cells = cells(head)
        if not any(A._FIELD_COL_RE.search(h) for h in header_cells) or not body:
            continue
        if A._column_index(header_cells, A._REQUIRED_COL_RE) is not None:
            with_required += 1
        else:
            no_required += 1
            per_table.append((header_cells, len(body)))
    w(f"- 字段类表格：**{with_required} 张有必填列**、**{no_required} 张没有**")
    w("- 没有必填列的表 → `_truthy_required('')` = (False, False) → **整表不生成任何断言**：")
    for header_cells, rows in per_table[:12]:
        w(f"  · `{' | '.join(header_cells)}`（{rows} 行数据）")
    w()
    # ⑤ 误报风险（断言了不该断言的）
    w("## 5. 误报风险扫描（不该断言却被断言的）")
    w()
    optional_rows = []
    for row in A.read_field_rows(iface):
        required, optional = A._truthy_required(row.required_text)
        if optional and not required:
            optional_rows.append(row.name)
    asserted = {d.check.split(".")[-1] for d in derived}
    bad = sorted(set(optional_rows) & asserted)
    w(f"- 文档里标 `否`（可选）的字段 {len(set(optional_rows))} 个")
    w(f"- 其中**被断言**的：{bad if bad else '（无 —— 这条风险当前没有发生）'}")
    bad_paths = [d.check for d in derived if "`" in d.check or "*" in d.check]
    w(f"- 断言里路径含反引号/星号的（会指错）：{bad_paths or '（无）'}")
    w()

    # ⑥ 两模块口径差异（这份真实文档上）
    w("## 6. 两套解析口径在这份真实文档上的差异")
    w()
    a_names = {r.name for r in A.read_field_rows(iface)}
    d_names = {s.name for s in D.parse_field_tables(iface)[0]}
    w(f"- assertions 认出字段 {len(a_names)} 个；doc_quality 认出 {len(d_names)} 个")
    w(f"- 名字集合对称差（前 12）：{sorted(a_names ^ d_names)[:12]}")
    both = a_names | d_names
    only_ticks = sorted(n for n in (a_names ^ d_names) if n.strip("`* ") in both)
    w(f"- 其中**只差反引号**（规范化差异）：{len(only_ticks)} 个，例：{only_ticks[:8]}")
    w()

    # ⑦ 与人工 oracle 对照
    w("## 7. 与**人工用例**对照（最关键的一节）")
    w()
    case_blocks = re.split(r"^###\s+", cases, flags=re.M)[1:]
    case_ids = [b.split("\n", 1)[0].strip()[:30] for b in case_blocks if b.strip().startswith("TC-")]
    urls = re.findall(r"\*\*请求URL\*\*\s*\|\s*`?([^`|\n]+)`?", cases)
    tokens = set()
    for match in re.finditer(r"\*\*验证点\*\*\s*\|\s*([^\n]+)", cases):
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}", match.group(1)):
            if token.lower() not in STOPWORDS:
                tokens.add(token)
    w(f"- 人工用例条数：**{len(case_ids)}**（例：{case_ids[:3]}）")
    w(f"- 人工用例出现的请求 URL：**{len(set(u.strip() for u in urls))}** 个")
    w(f"- 人工「验证点」提到的字段名：**{len(tokens)}** 个")
    w(f"- AI 从接口文档推出的字段：**{len(asserted)}** 个")
    w(f"- ★**人工提到、AI 没推**（覆盖缺口，前 20）：{sorted(tokens - asserted)[:20]}")
    w(f"- AI 推了、人工没提（前 20）：{sorted(asserted - tokens)[:20]}")
    w()
    w("> 人工用例含**负例/边界**（错 client_id、缺参数、超限…），而确定性通路")
    w("> **只产正常路径**断言 —— 差集**不等于缺陷**，但它量化了「当前通路覆盖人工用例的多少」。")
    w()

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_measure.txt")
    with io.open(out, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    print("已写出 out_measure.txt（" + str(len(lines)) + " 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
