# -*- coding: utf-8 -*-
"""对**已填好的**表 B/C 做**机器交叉检查**（第二双眼睛，不替代人判）。

两道检查都是"能从产物里客观算出来"的，所以它们能**发现人判错**：
  ① 表 B 里判为 `protocol-only`（"断言全是协议级"）的条目 → 去对应用例的落盘 YAML 里数：
     它的断言**是不是真的全是协议级**（`status_code` 之类）？如果发现字段级断言，
     说明这条"protocol-only"判得不对（**误伤**）。
  ② 表 C 里判为"确实不含接口"的片 → 看它的**正文**里有没有 `METHOD /path` 形态；
     正文里有却判"不含接口"，就是**漏测**。
     （标题路径里有、正文里没有的，属"接口章节下的子表"，跳过是合理的——单独列出仅供确认。）
用法：
    $env:P0A_MODEL="gemma4:latest"
    .\\.venv\\Scripts\\python.exe probe_p0a_llm\\crosscheck_filled_sheets.py
"""

import glob
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import yaml  # noqa: E402

MODEL = os.environ.get("P0A_MODEL", "gemma4:latest")
SLUG = re.sub(r"[^A-Za-z0-9._-]+", "-", MODEL).strip("-")
SHEET_DIR = os.path.join(BASE, "probe_p0a_llm", "review_sheets", SLUG)
RUN_ROOT = os.path.join(BASE, f".tmp_p0a_run_{SLUG}")
OUT = os.path.join(BASE, "probe_p0a_llm", f"out_crosscheck_{SLUG}.json")

# 协议级检查项：优先用装配器自己的那份（单一来源），拿不到就退化成 status_code
try:
    from interfacetester_ai.assembler import _PROTOCOL_CHECKS as PROTOCOL_CHECKS  # noqa: PLC0415

    PROTOCOL = set(PROTOCOL_CHECKS)
except Exception:  # noqa: BLE001
    PROTOCOL = {"status_code"}

METHOD_RE = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/\S*", re.I)


def _cells(line):
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def _table_rows(path):
    """返回 `(表头, 数据行 cells 列表)`。"""
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    for index, line in enumerate(lines):
        if line.startswith("|") and "人工填" in line:
            header = _cells(line)
            rows = [
                _cells(item)
                for item in lines[index + 2 :]
                if item.startswith("|") and len(_cells(item)) == len(header)
            ]
            return header, rows
    return [], []


def _case_checks(tag, case_name):
    """从落盘 YAML 里取出某用例的全部断言检查项。

    ★两条容错都来自实测：
    1. 表里的用例名**可能被截断成 `…`**（生成表的脚本为控列宽曾把长名字截到 40 字符）→
       截断了就按**前缀**去 `cases/` 里匹配；
    2. 找不到产物**不等于判错**——被 S-4 闸门拒掉的用例本来就不落盘（那时应回"无法机检"）。
    """
    folder = os.path.join(RUN_ROOT, tag, "cases")
    exact = os.path.join(folder, case_name + ".yml")
    if os.path.isfile(exact):
        path = exact
    else:
        prefix = case_name.rstrip("…").rstrip(".")
        matches = sorted(glob.glob(os.path.join(folder, prefix + "*.yml")))
        if len(matches) != 1:
            return None
        path = matches[0]
    with open(path, encoding="utf-8", errors="replace") as fp:
        case = yaml.safe_load(fp) or {}
    checks = []
    for step in case.get("teststeps") or []:
        for item in step.get("validate") or []:
            if isinstance(item, dict):
                for pair in item.values():
                    if isinstance(pair, list) and pair:
                        checks.append(str(pair[0]))
    return checks


def main() -> int:
    report = {"model": MODEL, "protocol_checks": sorted(PROTOCOL), "b": [], "c": []}

    # ---------- 表 B：protocol-only 的条目，断言真的全是协议级吗 ----------
    b_path = os.path.join(SHEET_DIR, "B_unknowns_review.md")
    header_b, rows_b = _table_rows(b_path) if os.path.isfile(b_path) else ([], [])
    for cells in rows_b:
        case_name = cells[1].strip("`")
        kind = cells[2]
        verdict = cells[-2]
        tag = case_name.split("___")[0]
        checks = _case_checks(tag, case_name)
        if checks is None:
            report["b"].append({"case": case_name, "check": "⚠ 找不到落盘产物（可能该篇没产出）"})
            continue
        field_checks = [item for item in checks if item not in PROTOCOL]
        consistent = (kind == "protocol-only" and not field_checks) or (
            kind != "protocol-only" and bool(field_checks)
        )
        report["b"].append(
            {
                "case": case_name,
                "kind": kind,
                "checks": checks,
                "field_checks": field_checks,
                "judged": verdict,
                "machine_consistent": consistent,
            }
        )

    # ---------- 表 C：判"不含接口"的片，正文里真的没有 METHOD /path 吗 ----------
    c_path = os.path.join(SHEET_DIR, "C_non_interface_slices_review.md")
    header_c, rows_c = _table_rows(c_path) if os.path.isfile(c_path) else ([], [])
    for cells in rows_c:
        doc, title, lines_span, excerpt, verdict = (
            cells[1].strip("`"),
            cells[2],
            cells[3],
            cells[4],
            cells[-1],
        )
        in_body = METHOD_RE.search(excerpt or "")
        in_title = METHOD_RE.search(title or "")
        report["c"].append(
            {
                "doc": doc,
                "title": title,
                "lines": lines_span,
                "method_in_body": in_body.group(0) if in_body else "",
                "method_in_title": in_title.group(0) if in_title else "",
                "judged": verdict,
            }
        )

    with open(OUT, "w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2)

    bad_b = [item for item in report["b"] if item.get("machine_consistent") is False]
    missing_b = [item for item in report["b"] if "check" in item]
    bad_c = [item for item in report["c"] if item["method_in_body"]]
    title_only_c = [item for item in report["c"] if item["method_in_title"] and not item["method_in_body"]]

    print(f"模型 {MODEL}；协议级检查项（取自装配器）：{sorted(PROTOCOL)}")
    print(f"表 B：{len(report['b'])} 条；机检**不一致** {len(bad_b)} 条；找不到产物 {len(missing_b)} 条")
    for item in bad_b:
        print(
            f"  ⚠ {item['case']}：标为 {item['kind']}，但落盘断言里有**字段级** {item['field_checks']}"
            f" → 这条判定建议复核"
        )
    for item in missing_b:
        print(f"  · {item['case']}：{item['check']}（无产物可查，不作判断）")
    print(f"表 C：{len(report['c'])} 片；**正文里有 METHOD /path** 的 {len(bad_c)} 片（有则说明'漏测'）")
    for item in bad_c:
        print(f"  ⚠ {item['doc']} :: {item['title']}（{item['lines']}）→ 正文含 `{item['method_in_body']}`")
    print(
        f"  （另 {len(title_only_c)} 片的**标题**里带接口形态、正文没有——"
        "属'接口章节下的子表'，跳过合理，单独列出仅供确认）"
    )
    print(f"机读结果：{os.path.relpath(OUT, BASE)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
