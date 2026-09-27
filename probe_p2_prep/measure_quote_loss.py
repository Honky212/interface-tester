# -*- coding: utf-8 -*-
"""WP2.2（证据绑定）归因 + 修法预演 —— **离线，不调模型、不写盘**。

数据来源：`.ai/draft/*.draft.json`（**模型原始草案**）+ `*.unknowns.json`（为什么被拦）——
它们是 2026-09-25 那轮 `gemma4:latest` 在客户真实文档上运行的留痕（31 次调用 / 16 分钟）。
★**样本边界（诚实说明）**：`.ai/draft/` 里**仅存 8 片**（20 条断言）的草案记录，
而 §9.7 记的全量口径（原始 75 条 → 最终 45 条）来自**另一处留痕**（`measure_model_yaml.py` 读最终
YAML）。**两者不可混用**：本脚本回答的是"**引用长什么样、绑定能救回多少**"，
比例的绝对值受这 8 片样本影响。

★判"产出"必须读最终产物 `.yml`；本脚本读中间产物**只用来分析"为什么被拦"**（§9.7 的测量纪律）。

它回答三件事：
1. 被 T28 拦下的断言，引用句**长什么样**（形态直方图 + 去重计数）；
2. 两种"证据绑定"修法**各自能救回多少**（先量再改）：
   - **R1 引用是标题** → 绑到该标题下的整节正文；
   - **R2 引用是自造锚点** → 绑到"包含该断言字段/期望值的那一节"；
3. 仍救不回的是哪些 —— 那才是真正该**交人工**的。

用法：`python probe_p2_prep/measure_quote_loss.py > probe_p2_prep/out_quote_loss.txt`
"""

import glob
import io
import json
import os
import re
import sys
from collections import Counter
from difflib import SequenceMatcher

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.assembler import (  # noqa: E402
    audit_against_quote,
    audit_assertion,
    find_sample_values,
    quote_hits,
)
from interfacetester_ai.gen import _safe_case_name, _title_segment  # noqa: E402
from interfacetester_ai.schema import parse_draft  # noqa: E402
from interfacetester_ai.slicer import split_document  # noqa: E402

DOC_REL = os.path.join("project-three", "统一文件服务平台-接入应用接口文档.md")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def sections(doc):
    """`[(title, level, start_idx, end_idx, text)]`——标题行**含**在 text 里。

    ★只用于**报告的可读性**（形态分类时判断"引用是不是某个标题"）；
    真正的证据窗口解析在**产品代码** `interfacetester_ai/citation.py` 里（单一来源）。
    """
    lines = doc.splitlines()
    heads = []
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match:
            heads.append((index, len(match.group(1)), match.group(2).strip()))
    out = []
    for pos, (index, level, title) in enumerate(heads):
        end = heads[pos + 1][0] if pos + 1 < len(heads) else len(lines)
        out.append((title, level, index, end, "\n".join(lines[index:end])))
    return out


def nearest_heading(titles, quote):
    scored = [(SequenceMatcher(None, quote, title).ratio(), title) for title in titles]
    if not scored:
        return 0.0, ""
    return max(scored)


def main():  # noqa: C901
    with io.open(os.path.join(BASE, DOC_REL), encoding="utf-8") as handle:
        doc = handle.read()
    all_sections = sections(doc)
    titles = [item[0] for item in all_sections]
    samples = find_sample_values(doc)
    slices = {}
    for item in split_document(doc):
        slices[_safe_case_name(_title_segment(item.title_path))] = item

    rows = []
    shape_counter = Counter()
    quote_counter = Counter()
    recovered = 0
    kept_total = 0
    bind_counter = Counter()
    shared_count = 0
    residual_kinds = Counter()
    residual = []

    for path in sorted(glob.glob(os.path.join(BASE, ".ai", "draft", "*.draft.json"))):
        with io.open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        case = payload.get("case", "")
        try:
            draft = parse_draft(payload["draft"])
        except Exception as error:  # noqa: BLE001
            rows.append((case, "parse-fail", str(error)[:60]))
            continue
        keep = drop = 0
        case_slice = slices.get(_safe_case_name(case))
        scope = (
            (case_slice.start_line, case_slice.end_line) if case_slice is not None else None
        )
        for step_index, step in enumerate(draft.steps):
            for assert_index, assertion in enumerate(step.validate):
                where = f"steps[{step_index}].validate[{assert_index}]"
                quote = assertion.source_quote or ""
                # ★**句级**（`audit_against_quote`）：模型自己的引用站不站得住 —— 引用质量
                strict = audit_against_quote(
                    assertion,
                    quote=quote,
                    doc_text=doc,
                    sample_values=samples,
                    where=where,
                )
                if not strict:
                    keep += 1
                    continue
                drop += 1
                quote_counter[quote.strip()] += 1

                # ---- 形态分类（句级为什么站不住）----
                normalized = quote.strip().rstrip(":：").strip()
                if quote_hits(quote, doc):
                    shape = "引用逐字在文档里（但字段/期望值不在其中）"
                elif normalized in titles:
                    shape = "引用=某标题（缺正文）"
                else:
                    ratio, near = nearest_heading(titles, normalized)
                    shape = "自造锚点（文档里没有）"
                    if ratio >= 0.6:
                        shape = f"自造锚点（最近的标题：{near}｜相似度 {ratio:.2f}）"
                shape_counter[shape] += 1

                # ---- 节级（**产品行为**：证据绑定 + 片边界）----
                binding = []
                final = audit_assertion(
                    assertion,
                    quote=quote,
                    doc_text=doc,
                    sample_values=samples,
                    where=where,
                    scope=scope,
                    bound=binding,
                )
                if not final:
                    recovered += 1
                    for item in binding:
                        bind_counter[item.kind] += 1
                        shared_count += 1 if item.shared else 0
                    continue
                for item in final:
                    residual_kinds[item.kind] += 1
                residual.append(
                    (
                        case,
                        where,
                        quote.strip()[:44],
                        f"{assertion.comparator}:{assertion.check}",
                        ("" if assertion.expect is None else str(assertion.expect))[:34],
                        "、".join(sorted({item.kind for item in final})),
                    )
                )
        rows.append((case, f"保留 {keep}", f"拦下 {drop}"))
        kept_total += keep

    total_drop = sum(quote_counter.values())
    total_all = total_drop + kept_total
    report = [
        "=" * 78,
        f"样本：{DOC_REL}",
        f"模型留痕：{len(rows)} 份 `.draft.json`（2026-09-25 gemma4:latest 那轮）",
        "生成：`probe_p2_prep/measure_quote_loss.py`（离线、不调模型、不写别的东西）",
        "口径：**句级** = `audit_against_quote()`（只按模型自己那句话判）；",
        "      **节级** = `audit_assertion(..., bound=…)`（**产品当前行为**：证据绑定 + 片边界）",
        "=" * 78,
        "",
        "## 一、逐片：句级 保留 / 拦下",
        "",
    ]
    for case, kept, dropped in rows:
        report.append(f"  {case:<30}{kept:<12}{dropped}")

    report += ["", "## 二、句级拦下的形态直方图（= 模型的**引用质量**）", ""]
    for shape, count in shape_counter.most_common():
        report.append(f"  {count:>3}  {shape}")

    rate = f"{total_drop / total_all:.0%}" if total_all else "n/a"
    back = f"{recovered / total_drop:.0%}" if total_drop else "n/a"
    report += [
        "",
        "## 三、句级 → 节级：降级率怎么变的",
        "",
        f"  模型提议的断言总数                ：{total_all}",
        f"  ① 句级保留                        ：{kept_total}（{1 - float(total_drop) / total_all:.0%}）"
        if total_all
        else "  ① 句级保留                        ：0",
        f"  ② 句级拦下（**引用质量**）        ：{total_drop}（降级率 {rate}）",
        f"  ③ 其中被**证据绑定**救回          ：{recovered}（占被拦下的 {back}）",
        f"  ④ 节级仍拦下（**该交人工**）      ：{len(residual)}"
        f"（最终降级率 {(total_drop - recovered) / total_all:.0%}）" if total_all else "",
        "",
        "  ★绑定的办法分布（`citation.KIND_*`）：",
    ]
    for kind, count in bind_counter.most_common():
        report.append(f"     {count:>3}  {kind}")
    if shared_count:
        report.append(
            f"     （其中 {shared_count} 条绑到了**片外的共享节**——统一响应格式/公共请求头/错误码表）"
        )
    report += [
        "",
        "## 四、节级仍拦下的明细（`用例 / 位置 / 引用 / 断言 / 期望值 / 原因`）",
        "",
    ]
    for item in residual:
        report.append("  " + " | ".join(str(part) for part in item))

    report += ["", "## 五、节级仍拦下的**原因**分布", ""]
    for kind, count in residual_kinds.most_common():
        report.append(f"  {count:>3}  {kind}")

    report += ["", "## 六、出现次数最多的引用句（Top 15）", ""]
    for quote, count in quote_counter.most_common(15):
        report.append(f"  {count:>3}  {quote[:70]!r}")

    report += ["", "## 七、该文档的切片名单（供对照：哪些片本来就不该调模型）", ""]
    for name, item in slices.items():
        report.append(
            f"  {name:<30} 行 {item.start_line}-{item.end_line}  {item.title_path[:60]}"
        )

    out = os.path.join(BASE, "probe_p2_prep", "out_quote_loss.txt")
    with io.open(out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(report) + "\n")
    print(f"written {out}")
    print(
        f"proposed={total_all} strict_kept={kept_total} strict_dropped={total_drop} "
        f"bound_back={recovered} final_dropped={len(residual)}"
    )


if __name__ == "__main__":
    main()
