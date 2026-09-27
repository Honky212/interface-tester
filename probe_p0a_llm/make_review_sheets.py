# -*- coding: utf-8 -*-
"""(6b 续) 生成三张**人工抽检表**——判定列**留空**，由人填。

三条抽检对象（都是"机器判不了、只有人能判"的）：
  A. **R11 断言语义正确率**：每条落盘断言的 `expect` 是否真的符合文档语义（机检只保证"引用句逐字在文档里"）；
  B. **`unknowns` 抽检 ≥8/10**（P0b 验收）：被判"降级/拒掉"的那些条目，**判得对吗**（有没有误伤）；
  C. **55 片"非接口"跳过**的判定：被 CLI 按 `skip-static` 跳过的片，**确实不含接口**吗。

★本脚本**只做表**（读产物 + 读日志 + 重切文档拿行号与原文），不替代判定。
用法：
    $env:P0A_MODEL="gemma4:latest"          # 选哪一轮的产物（默认 gemma4-latest）
    .\\.venv\\Scripts\\python.exe probe_p0a_llm\\make_review_sheets.py
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

from interfacetester_ai.slicer import split_document  # noqa: E402

MODEL = os.environ.get("P0A_MODEL", "gemma4:latest")
SLUG = re.sub(r"[^A-Za-z0-9._-]+", "-", MODEL).strip("-")
# ★产物根 / 出表目录支持环境变量覆盖（2026-09-26 加）：验证"合并修正"时要在**独立工作区**
# （`.tmp_p0a_run_<slug>_merged/`）里重放产物，且**不能覆盖**已填好的表。
# 为什么需要：装配器有"查重拒绝覆盖"，在原始目录里重放**写不进去**（实测：表 A 仍 13 条）。
RUN_ROOT = os.environ.get("P0A_RUN_ROOT") or os.path.join(BASE, f".tmp_p0a_run_{SLUG}")
LOG_PATH = os.path.join(BASE, "probe_p0a_llm", f"gen_all_{SLUG}.log")
SHEET_DIR = os.environ.get("P0A_SHEET_DIR") or os.path.join(
    BASE, "probe_p0a_llm", "review_sheets", SLUG
)
GOLDEN_DIR = os.path.join(BASE, "bench", "golden")


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read()


def _unknown_rows(workspace):
    """一篇工作区里的 `unknowns` 条目（降级 / 拒掉的断言）。"""
    rows = []
    for path in sorted(
        glob.glob(os.path.join(workspace, ".ai", "draft", "*.unknowns.json"))
    ):
        payload = json.loads(_read(path))
        for item in payload.get("items") or []:
            rows.append(
                {
                    "case": str(payload.get("case") or os.path.basename(path)),
                    "kind": str(item.get("kind") or ""),
                    "level": str(item.get("level") or ""),
                    "where": str(item.get("where") or ""),
                    "message": str(item.get("message") or ""),
                    "hint": str(item.get("hint") or ""),
                }
            )
    return rows


_SLICE_LINE = re.compile(r"^- (?P<case>\S+)\s*（(?P<title>.+?)）\s*$")


def _log_slices(log_text):
    """从 `gen_all_*.log` 按文档段解析**片级**结果：`{tag: [(case, title, skipped, reason)]}`。"""
    parsed = {}
    sections = re.split(r"\n===== (\S+) ===== ", log_text)
    for index in range(1, len(sections), 2):
        tag = sections[index]
        blocks = []
        current = None
        for line in sections[index + 1].splitlines():
            match = _SLICE_LINE.match(line.strip())
            if match:
                current = {
                    "case": match.group("case"),
                    "title": match.group("title"),
                    "skipped": False,
                    "reason": "",
                }
                blocks.append(current)
                continue
            if current is not None and "已跳过" in line:
                current["skipped"] = True
                current["reason"] = line.split("：", 1)[-1].strip()
        parsed[tag] = blocks
    return parsed


def _doc_for_tag(tag):
    """`G07_login` → `bench/golden/login.md`。"""
    name = tag.split("_", 1)[1] if "_" in tag else tag
    return os.path.join(GOLDEN_DIR, name + ".md")


def _write(path, lines):
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")


# ★"别覆盖人工成果"的守卫：表里一旦有判定标记（`✓/✗/?`），重出时**不覆盖**，改写 `<name>.new`
_MARK_RE = re.compile(r"\|\s*[✓✗√×?？]\s*\|")


def _safe_write(path, lines):
    text = "\n".join(lines) + "\n"
    if os.path.isfile(path):
        existing = open(path, encoding="utf-8", errors="replace").read()
        if _MARK_RE.search(existing):
            alt = path + ".new"
            with open(alt, "w", encoding="utf-8") as fp:
                fp.write(text)
            print(
                f"⚠ {os.path.basename(path)} 里**已有判定**，不覆盖人工成果 → 新表写到 "
                f"{os.path.basename(alt)}（对比后自行取舍）"
            )
            return
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(text)


# ★每张表都带这一行：**怎么填**与**填错了怎么办**（判错不用慌，可重跑、可追溯）
_MARK_NOTE = (
    "> **标记**：`✓` 判对 ／ `✗` 判错 ／ **`?` 无法判定**（不懂业务就写 `?`——它**不计入分子分母**，"
    "单列统计，本仓立场是「证据不足就输出未知」，不许猜）。"
    "**判错不用慌**：改一格 → 重跑 `score_review_sheets.py` → 新结论**追加**进 "
    "`SCORES_HISTORY.md`，**旧结论不重写**。"
)


def main() -> int:
    if not os.path.isdir(RUN_ROOT):
        print(f"没有这一轮的产物目录：{RUN_ROOT}", file=sys.stderr)
        return 2
    os.makedirs(SHEET_DIR, exist_ok=True)

    workspaces = sorted(
        path for path in glob.glob(os.path.join(RUN_ROOT, "G*")) if os.path.isdir(path)
    )
    log_slices = _log_slices(_read(LOG_PATH)) if os.path.isfile(LOG_PATH) else {}

    # ---------- 表 A：R11 断言语义正确率 ----------
    assertion_rows = []
    for workspace in workspaces:
        assertion_rows.extend(
            _assertion_rows(workspace, _doc_for_tag(os.path.basename(workspace)))
        )
    lines_a = [
        f"# 表 A：R11 断言语义正确率抽检（模型 `{MODEL}`）",
        "",
        f"> 共 **{len(assertion_rows)}** 条**落盘断言**（落盘的才会被执行，所以逐条判）。",
        "> **要你判的那一列**：`expect` 的取值**是否符合引用句与文档的语义**——",
        "> 机检只能保证「引用句逐字在文档里」，**判不了「语义对不对」**（那是 R11 的范围）。",
        _MARK_NOTE,
        "",
        "| # | 用例 | 步骤 | 断言（算子/检查项/期望） | 引用句 source_quote | 引用句逐字在文档里（脚本独立复核） | **语义一致？人工填 ✓/✗** | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    # ★标识符列**不截断**：用例名/标题路径是**机器要拿来对账的键**，
    #   截成 `…` 会让下游（交叉检查脚本）找不到文件——实测踩到（表 B 有 7 条因此漏检）。
    for index, row in enumerate(assertion_rows, start=1):
        lines_a.append(
            f"| {index} | `{_esc(row['case'], 200)}` | {_esc(row['step'], 40)} "
            f"| `{row['comparator']}({row['check']}, {row['expect']!r})` "
            f"| {_esc(row['source_quote'], 70)} "
            f"| {'**是**' if row['quote_in_doc'] else '⚠ 否/缺'} | | |"
        )
    _safe_write(os.path.join(SHEET_DIR, "A_r11_assertion_review.md"), lines_a)

    # ---------- 表 B：unknowns 抽检（≥8/10） ----------
    unknown_rows = []
    for workspace in workspaces:
        unknown_rows.extend(_unknown_rows(workspace))
    enough = "够" if len(unknown_rows) >= 10 else "不足 10"
    lines_b = [
        f"# 表 B：`unknowns` 抽检（P0b 验收：≥8/10）（模型 `{MODEL}`）",
        "",
        f"> 共 **{len(unknown_rows)}** 条。验收口径是「抽 10 条、判对 ≥8 条」——本轮**总数 {enough}**，",
        "> 不足 10 时口径需人工确认（可否跨轮次/跨模型合并计数）。",
        "> **要你判的那一列**：这条「降级 / 拒掉」的判定**对不对**（有没有误伤、有没有漏网）。",
        _MARK_NOTE,
        "",
        "| # | 用例 | kind | level | where | 机器给的判定与理由 | 建议动作（机器给） | **判定正确？人工填 ✓/✗** | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, row in enumerate(unknown_rows, start=1):
        lines_b.append(
            f"| {index} | `{_esc(row['case'], 200)}` | {row['kind']} | {row['level']} "
            f"| {row['where']} | {_esc(row['message'], 120)} | {_esc(row['hint'], 90)} | | |"
        )
    _safe_write(os.path.join(SHEET_DIR, "B_unknowns_review.md"), lines_b)

    # ---------- 表 C：被跳过的"非接口"片 ----------
    lines_c = [
        f"# 表 C：被跳过的「非接口」片（`skip-static`）（模型 `{MODEL}`）",
        "",
        "> CLI 按「本节没有 `METHOD /path` 形态的端点」跳过这些片——**这批判定决定「是不是漏了接口」**。",
        "> **要你判的那一列**：这片**确实不含具体接口**吗？（对照右栏原文摘录）",
        _MARK_NOTE,
        "",
        "| # | 文档 | 片（标题路径） | 行号范围 | 原文摘录 | 跳过理由（机器给） | **确实不含接口？人工填 ✓/✗** |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    skipped_total = 0
    for workspace in workspaces:
        tag = os.path.basename(workspace)
        doc_path = _doc_for_tag(tag)
        if not os.path.isfile(doc_path):
            continue
        try:
            slices = split_document(_read_doc_text(doc_path))
        except Exception as error:  # noqa: BLE001
            lines_c.append(f"| - | `{tag}` | ⚠ 重切失败：{type(error).__name__} | | | | |")
            continue
        # ★匹配口径：CLI 就是**按 slicer 的顺序**遍历片的，所以"块数 == 片数"时直接按下标对齐；
        #   数量不一致才退化为按标题路径找（找不着再取下一个未用的）。
        #   ——第一版忘了给"未跳过的块"也占位，于是跳过的片整体**向前错位一格**（实测踩到）。
        blocks = log_slices.get(tag) or []
        positional = len(blocks) == len(slices)
        used = set()
        for position, block in enumerate(blocks):
            if not block["skipped"]:
                continue
            skipped_total += 1
            match = None
            if positional:
                match = (position, slices[position])
            else:
                for index, item in enumerate(slices):
                    if index not in used and item.title_path.strip() == block["title"].strip():
                        match = (index, item)
                        break
                if match is None:
                    for index, item in enumerate(slices):
                        if index not in used:
                            match = (index, item)
                            break
            if match is None:
                lines_c.append(
                    f"| {skipped_total} | `{tag}` | {_esc(block['title'], 60)} | ⚠ 未匹配到切片 "
                    f"| | {_esc(block['reason'], 90)} | |"
                )
                continue
            index, item = match
            used.add(index)
            lines_c.append(
                f"| {skipped_total} | `{tag}` | {_esc(item.title_path or block['title'], 60)} "
                f"| {item.start_line}-{item.end_line} | {_esc(item.text, 160)} "
                f"| {_esc(block['reason'], 90)} | |"
            )
    _safe_write(os.path.join(SHEET_DIR, "C_non_interface_slices_review.md"), lines_c)

    flagged = [row for row in assertion_rows if not row["quote_in_doc"]]
    print(f"模型 {MODEL}")
    print(f"表 A（断言）   ：{len(assertion_rows)} 条 → {SHEET_DIR}\\A_r11_assertion_review.md")
    print(f"表 B（unknowns）：{len(unknown_rows)} 条 → {SHEET_DIR}\\B_unknowns_review.md")
    print(f"表 C（跳过片） ：{skipped_total} 片 → {SHEET_DIR}\\C_non_interface_slices_review.md")
    print(
        f"脚本独立复核：引用句不在文档里的断言 {len(flagged)} 条"
        "（这些不用人再判语义，机检已经指出问题了）"
    )
    return 0


def _esc(text, limit=240):
    """表格单元格：压掉换行与竖线（markdown 表格里它们会破表）。"""
    flat = re.sub(r"\s+", " ", (text or "").strip()).replace("|", "\\|")
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _read_doc_text(doc_path):
    """按**归一化管道**读文档（GBK/BOM 都要能读）——与 gen 同一入口。"""
    from interfacetester_ai.normalize import _read_any_encoding  # noqa: PLC0415

    return _read_any_encoding(doc_path)


def _assertion_rows(workspace, doc_path):
    """一篇工作区里的全部落盘断言（**带引用句**；引用句是否逐字在文档里由本脚本**独立再验一遍**）。

    ★为什么本脚本还要自己验一遍引用：T28 的 `quote-miss` 是**生成期**判据，
      而"生成期过了"不等于"我现在读到的产物里那句还在文档里"（产物可能被人工改过、
      或判据本身有洞）。抽检表的价值一半在"人判语义"，另一半就在这条**独立复核**上。
    """
    rows = []
    doc_text = _read_doc_text(doc_path) if os.path.isfile(doc_path) else ""
    for yml_path in sorted(glob.glob(os.path.join(workspace, "cases", "*.yml"))):
        case_name = os.path.basename(yml_path)[: -len(".yml")]
        draft_path = os.path.join(workspace, ".ai", "draft", f"{case_name}.draft.json")
        quotes = {}
        coverage = {}
        if os.path.isfile(draft_path):
            payload = json.loads(_read(draft_path))
            coverage = payload.get("coverage") or {}
            for step in (payload.get("draft") or {}).get("steps") or []:
                for item in step.get("validate") or []:
                    key = (str(item.get("check")), str(item.get("expect")))
                    quotes[key] = str(item.get("source_quote") or "")
        with open(yml_path, encoding="utf-8", errors="replace") as fp:
            case = yaml.safe_load(fp) or {}
        for step in case.get("teststeps") or []:
            for item in step.get("validate") or []:
                if not isinstance(item, dict):
                    continue
                for comparator, pair in item.items():
                    check = str(pair[0]) if isinstance(pair, list) and pair else ""
                    expect = pair[1] if isinstance(pair, list) and len(pair) > 1 else None
                    quote = quotes.get((check, str(expect)), "")
                    rows.append(
                        {
                            "case": case_name,
                            "step": str(step.get("name") or ""),
                            "comparator": str(comparator),
                            "check": check,
                            "expect": expect,
                            "source_quote": quote,
                            "quote_in_doc": bool(quote) and quote in doc_text,
                            "coverage": coverage,
                        }
                    )
    return rows


if __name__ == "__main__":
    raise SystemExit(main())


