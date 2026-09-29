# -*- coding: utf-8 -*-
"""diagnosis —— 失败归因的**六分类**与**文档缺陷回流**（v8 §5.3 / §十 T29，2026-09-23）。

## 为什么需要这个文件

§5.3 的归因维度原本是**五分类**：`真实缺陷` / `用例错误` / `环境问题` / `测试数据问题` /
`未知待人工确认`。问题出在：**接口文档本身写错 / 写缺**导致的失败，在这五类里
**没有归属**——它只能被塞进「用例错误」或「真实缺陷」。后果有两层（§9.2 的 **R15**）：

1. **文档永远不会被修**：没有缺陷清单产出，也就交不到文档维护者手里；
2. **同类错误下次继续污染**：没有回归样本，也没有"改回去会红"的护栏。

这是"**文档不准 → 用例不准**"这条链上唯一没有机器/流程承接的一环
（R1 管"模型编"、R11 管"模型意会错"、**这一类管"文档本身错"**）。

所以本模块做三件事：
**① 把分类扩到六类**（新增 `文档与实现不符`）；**② 用 §5.3 的证据纪律做硬校验**
（无证据 → **强制降级为"未知待人工确认"**，绝不强归因）；**③ 把文档缺陷渲染成
回流清单** `reports/doc_defects.md`（哪一句、与实际哪里不符、建议改法），
既交付文档维护者，又作为**回归输入**（文档修好后重跑）。

## 证据纪律（§5.3，本模块的核心判据）

- 每条结论**必须**引用日志 / 响应 / 断言失败点；**无证据时必须输出"未知待人工确认"**；
- `文档与实现不符` 这类**另有一条硬要求**：证据里必须**同时**出现
  **文档原句**（`source="doc"`）与**实际响应片段**（`source="response"`）——
  只喊"文档错了"而没有两侧原文，一律降级（判据见 `validate_diagnosis`）；
- 引用必须是**归一化后逐字子串**（`normalize_text`：完整管道见 `interfacetester_ai.normalize`，T7）。
  **★口径边界**：§5.3 的完整归一化管道还含「JSON 反向解码 / 引号族统一」，那部分归
  **T7**（归一化管道 + 4 组对抗样本）；本模块先用**最小可判子集**并显式标注，
  不假装完整（R14 纪律）。

## 写盘边界（红线③，§6 / §0.4-②）

本模块是**登记在册的写入者**，写入根固定 `reports/`（AI 层报告目录：
`reports/analysis.md` / `reports/doc_defects.md`）——`bench/write_boundary_scanner.py`
的 `MODULE_WRITE_BOUNDARIES` 登记为 `("roots", ("reports/",))`。
**无文档缺陷时不落盘**（不留空清单让人误以为有活要干，同 `pending.py` 口径）；
写盘行内联字面量前缀（T18 静态可判纪律）。

## 用法

    diags = [Diagnosis(case=..., step=..., cause_type=CAUSE_DOC_DEFECT, ...)]
    diags = [normalize_diagnosis(d, doc_text=doc, response_text=resp) for d in diags]
    rel = write_doc_defects(diags)   # None = 无文档缺陷，不落盘
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

from interfacetester_ai.normalize import normalize_text as normalize_pipeline

# ---------------------------------------------------------------------------
# 六分类枚举（§5.3 的归因维度；本模块是唯一来源）
# ---------------------------------------------------------------------------

CAUSE_REAL_DEFECT = "真实缺陷"
CAUSE_CASE_ERROR = "用例错误"
CAUSE_ENV_ISSUE = "环境问题"
CAUSE_TEST_DATA_ISSUE = "测试数据问题"
CAUSE_DOC_DEFECT = "文档与实现不符"  # ★T29 新增（第六类，见 R15）
CAUSE_UNKNOWN = "未知待人工确认"  # 无证据时的**降级出口**（§5.3 证据优先）

# 归因类别全集（**六分类，不合并**；顺序即文档 §5.3 的列举顺序）
CAUSE_TYPES: Tuple[Text, ...] = (
    CAUSE_REAL_DEFECT,
    CAUSE_CASE_ERROR,
    CAUSE_ENV_ISSUE,
    CAUSE_TEST_DATA_ISSUE,
    CAUSE_DOC_DEFECT,
    CAUSE_UNKNOWN,
)

# 需要"双侧证据"的类别（文档原句 + 实际响应），其余类别只要求"有证据"
CAUSE_TYPES_REQUIRING_BOTH_SIDES: Tuple[Text, ...] = (CAUSE_DOC_DEFECT,)

# ---------------------------------------------------------------------------
# ★§9.24 诊断输出的**紧凑契约**（给 prompt 用；**从上面的常量生成，禁止手抄**）
# ---------------------------------------------------------------------------
# 为什么必须有它（2026-09-27 实测，与 §9.15 那次"gen 没给契约"是同一类事故）：
#   真实跑 `haify analyze`（同一份真 summary、同一台 ollama）时——
#     · `gemma4:latest` / `gemma4:12b` → 返回 `<unused50>`×N（**不是 JSON**）→
#       `LLMResponseFormatError` → 整次分析失败、无报告；
#     · `qwen3.8:27b` → 成功，6/6 未降级、引用可溯源。
#   也就是说："能不能跑"当时**取决于模型**，而这件事没有任何东西**可观测** ✗。
#   与 gen 的处置一样：把**输出形状**明确写进 system prompt（模型不该靠猜形状），
#   再配一道**修正环**（格式错回喂、重试必须改变输入）—— 缺一样都不解决问题。
DIAGNOSIS_TOP_FIELDS: Tuple[Text, ...] = ("diagnoses",)
# item 里**必填**的键（缺一个就算结构不合格 → 回喂，不靠"宽容解析"掩盖）
DIAGNOSIS_ITEM_REQUIRED_FIELDS: Tuple[Text, ...] = (
    "case",
    "step",
    "cause_type",
    "reason",
    "evidence_quotes",
)
DIAGNOSIS_ITEM_OPTIONAL_FIELDS: Tuple[Text, ...] = ("confidence", "action")
# 引用项允许的键（多一个键就是"形状不对"——模型爱在这塞自己的字段名）
DIAGNOSIS_QUOTE_FIELDS: Tuple[Text, ...] = ("source", "text")
# 引用来源（doc = 文档原句；response = 响应体；log = 日志关键行）
DIAGNOSIS_QUOTE_SOURCES: Tuple[Text, ...] = ("doc", "response", "log")

# 契约里必须出现的关键串（供 `audit_diagnosis_contract` 对账；★被删掉要能被抓住）
DIAGNOSIS_CONTRACT_NEEDLES: Tuple[Text, ...] = (
    "diagnoses",
    "cause_type",
    "evidence_quotes",
    CAUSE_UNKNOWN,
)


def diagnosis_contract_text(expected: int = 0) -> Text:
    """渲染**诊断输出契约**（system prompt 里那段；`expected`=证据包条数，0=不写）。

    ★为什么是"紧凑文本"而不是真 JSON Schema（同 `schema.draft_contract_text()` 的理由）：
      模型要的只是**形状**（键名 / 嵌套 / 必填 / 枚举），不是 `$defs`/`pattern` 那些；
      而诊断的 system prompt 还要和证据包一起挤上下文预算。
    ★末两条（"每个包一条"与"没原文就别编"）来自真实事故：
      · 少给一条 → 报告里那条失败**凭空消失**（读者以为它没问题）；
      · 编引用 → 由 `validate_diagnosis` 降级（代码兜底），但**提示里先说清**能省一整轮。
    """
    causes = " / ".join(CAUSE_TYPES)
    lines = [
        "## 输出形状（**严格**：顶层**只能**有 \"diagnoses\" 这一个键）",
        "{",
        '  "diagnoses": [',
        "    {",
        '      "case": "<证据包里的用例名，逐字照抄>",',
        '      "step": "<证据包里的步骤名，逐字照抄>",',
        f'      "cause_type": "<六类之一：{causes}>",',
        '      "confidence": 0.0,',
        '      "reason": "<为什么这么判（要有依据）>",',
        '      "action": "<建议动作>",',
        '      "evidence_quotes": [{"source": "response", "text": "<原文片段，逐字>"}]',
        "    }",
        "  ]",
        "}",
        f"· 顶层键 ∈ {list(DIAGNOSIS_TOP_FIELDS)}（多一个键就会被拒）",
        f"· 每条 item 必填 ∈ {list(DIAGNOSIS_ITEM_REQUIRED_FIELDS)}；"
        f"可选 ∈ {list(DIAGNOSIS_ITEM_OPTIONAL_FIELDS)}",
        f"· cause_type 只许这六类：{causes}",
        f"· evidence_quotes 里每项**只许**有 {list(DIAGNOSIS_QUOTE_FIELDS)}；"
        f"source ∈ {list(DIAGNOSIS_QUOTE_SOURCES)}",
        "· **每个证据包都要给出一条 item**（哪怕结论是"
        f"「{CAUSE_UNKNOWN}」）——少一条，那条失败就会在报告里凭空消失",
        f"· 找不到可引用的原文时，**宁可写「{CAUSE_UNKNOWN}」也不要编引用**"
        "（编出来的引用会被代码降级，等于白跑）",
    ]
    if expected:
        lines.append(f"· 本轮证据包 **{expected}** 条 → `diagnoses` 就**必须**是 {expected} 条")
    return "\n".join(lines)


def audit_diagnosis_contract(prompt: Optional[Text] = None) -> List[Text]:
    """检查渲染出的诊断 prompt 里契约还在不在（被删/被改写 → 返回缺的关键串）。"""
    if prompt is None:
        from interfacetester_ai.prompts import render_diagnosis_system_prompt  # noqa: PLC0415

        prompt = render_diagnosis_system_prompt()
    return [needle for needle in DIAGNOSIS_CONTRACT_NEEDLES if needle not in prompt]

# 报告落点（write_doc_defects 的固定相对根；红线③ 登记项）
DOC_DEFECTS_REL_PATH = "reports/doc_defects.md"


@dataclass
class Diagnosis:
    """一条失败归因（§5.3 的输出契约）。

    `evidence_quotes` 的元素形状：`{"source": "doc"|"response"|"log", "text": <原文>}`。
    """

    case: Text
    step: Text
    cause_type: Text
    reason: Text = ""
    action: Text = ""
    confidence: float = 0.0
    evidence_quotes: List[Dict[Text, Text]] = field(default_factory=list)

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "case": self.case,
            "step": self.step,
            "cause_type": self.cause_type,
            "confidence": self.confidence,
            "reason": self.reason,
            "action": self.action,
            "evidence_quotes": list(self.evidence_quotes),
        }


# ---------------------------------------------------------------------------
# 归一化（**薄封装**：完整管道在 `interfacetester_ai.normalize`，T7 已落地）
# ---------------------------------------------------------------------------


def normalize_text(text: Text) -> Text:
    """归一化（引用校验的判据用）。

    NOTICE（T7 落地）：本模块**不再自带**一套最小实现——完整管道
    （去 ANSI → CRLF/CR → LF、空白折叠、NBSP→空格 → NFKC → JSON 反向解码
    → 引号族统一，且带偏移映射）在 `interfacetester_ai.normalize`。

    保留同名入口是为了让"证据校验"这段代码读起来自洽（`_quote_ok` 里两处都用它），
    但**实现只有一处**：两套归一化各长一点，是这类模块最典型的口径漂移——
    后果是"真证据被判成没证据"，而它在报告里看起来与"模型没找到证据"一模一样。
    """
    return normalize_pipeline(text)


# ---------------------------------------------------------------------------
# 分类对账 + 证据校验（判据：违规必红 / 合规必绿）
# ---------------------------------------------------------------------------


def validate_taxonomy(cause_types: Sequence[Text]) -> List[Text]:
    """归因类别对账：返回**缺失的类别**（空 = 齐全）。

    "分类枚举缺格"必须能被检出——五分类漏掉"文档与实现不符"时，本函数返回非空，
    对账用例即红（§9.2 R15 的机器形态，取向同 T20 的三方对账）。
    """
    given = set(cause_types)
    return [c for c in CAUSE_TYPES if c not in given]


def _quote_ok(quote: Dict[Text, Text], doc_text: Text, response_text: Text) -> bool:
    """一条证据是否是"归一化后逐字子串"（编出来的引用不算证据，§5.3）。

    ★两处口径说明（都是实测踩出来的）：

    1. **引用侧要 `strip()`**：模型从多行文本里挑一段时，**几乎总会**带上首尾空白/换行
       （`"  库存不足\\r\\n"`）。而 `normalize_text` 只折叠**内部**空白——它的投影口径
       要求"多一个空格也算变了"，所以**不能**去改它（改了会动 `projection` 的逐字节承诺）。
       于是"对等"的做法在这里：**引用侧 strip**，haystack 侧不动。
       不这么做，"真证据"会被判成"没证据"，而报告里看起来与"模型没找到证据"一模一样。
    2. **`log` 来源的强度取决于调用方给没给原文**（P1a 兑现那行 TODO）：`evidence` 落地后
       `analyze` 传进来的是 `pack.haystack()`——**响应体 + 日志关键行**，于是日志引用
       同样被逐字校验。而"**没提供原文**"（旧语境）时按放行处理：那不是"原文里没有"，
       是"**没有可查的原文**"，混为一谈会把「证据不足」伪造成「证据造假」。
    """
    src = str(quote.get("source", ""))
    text = normalize_text(str(quote.get("text", ""))).strip()
    if not text:
        return False
    if src == "doc":
        hay = normalize_text(doc_text)
    else:  # response / log：都用**这一条证据包**的可引用原文（`haystack` = body + 日志行）
        hay = normalize_text(response_text)
        # ★「**没有可查的原文**」与「**原文里没有**」是两件事——这条区别很重要：
        # 本函数的契约只声明了 doc 侧与 response 侧两个参数；调用方若**没提供**
        # 该来源的原文（例如 P1a 之前的语境只传 response），log 引用**无可比对**，
        # 此时不能凭"查不到"判它编造——那会把「证据不足」伪造成「证据造假」。
        # P1a 落地后 `analyze` 传的是 `pack.haystack()`（响应体 + 日志关键行），
        # 于是 log 引用**真的会被逐字校验**——那才是 §5.3 要的强度。
        if src != "response" and not hay.strip():
            return True
    return text in hay


def validate_diagnosis(
    diag: Diagnosis, doc_text: Text = "", response_text: Text = ""
) -> Tuple[bool, Text]:
    """证据纪律校验（§5.3）。返回 (是否合法, 不合法原因)。

    判据：
      ① `cause_type` 必须在六分类内，否则降级；
      ② **证据为空 → 降级**（无证据必须"未知待人工确认"，不得强行归因）；
      ③ 引用必须是**归一化后逐字子串**（编出来的引用不算证据，逐条剔除）；
      ④ `文档与实现不符` 另要求 **doc 侧 + response 侧双证据**（只喊"文档错了"不够）。
    """
    if diag.cause_type not in CAUSE_TYPES:
        return False, f"cause_type 不在六分类内：{diag.cause_type!r}"

    valid = [
        q for q in diag.evidence_quotes if _quote_ok(q, doc_text, response_text)
    ]
    if not valid:
        return False, "证据为空（或引用不是原文逐字子串）→ 按 §5.3 必须降级为未知待人工确认"

    if diag.cause_type in CAUSE_TYPES_REQUIRING_BOTH_SIDES:
        sides = {str(q.get("source", "")) for q in valid}
        missing = [s for s in ("doc", "response") if s not in sides]
        if missing:
            return False, (
                "文档缺陷类需**双侧证据**（文档原句 + 实际响应），缺："
                + "、".join(missing)
            )
    return True, ""


def normalize_diagnosis(
    diag: Diagnosis, doc_text: Text = "", response_text: Text = ""
) -> Diagnosis:
    """按证据纪律归一：不合法的一律**降级为"未知待人工确认"**（§5.3，100% 显式标未知）。

    ★两个出口的区别（`analyze` 的"未降级占比"指标依赖它）：

    - **已经是「未知待人工确认」** → 直接放行（只把置信度归 0）。
      它**本来就是最保守的结论**——"没有更低的地方可降"。若这里也打上"已降级"，
      模型越守规矩（无证据就说未知）、指标越难看，信号正好读反。
    - **给了类别但不合法**（引用不是原文 / 文档缺陷缺一侧证据）→ 改写成未知 + 标注原因。
      这才是"降级"的本义。
    """
    if diag.cause_type == CAUSE_UNKNOWN:
        return replace(diag, confidence=0.0) if diag.confidence else diag

    ok, why = validate_diagnosis(diag, doc_text, response_text)
    if ok:
        return diag
    return Diagnosis(
        case=diag.case,
        step=diag.step,
        cause_type=CAUSE_UNKNOWN,
        reason=(diag.reason + f"（已降级：{why}）").strip(),
        action=diag.action,
        confidence=0.0,
        evidence_quotes=list(diag.evidence_quotes),
    )


# ---------------------------------------------------------------------------
# 渲染（analysis.md 片段 + doc_defects.md 回流清单）
# ---------------------------------------------------------------------------


def _fmt_quotes(quotes: Sequence[Dict[Text, Text]]) -> Text:
    lines = []
    for q in quotes:
        src = str(q.get("source", ""))
        label = {"doc": "文档原句", "response": "实际响应", "log": "日志"}.get(src, src)
        lines.append(f"  - **{label}**：`{q.get('text', '')}`")
    return "\n".join(lines)


def render_analysis_section(diags: Sequence[Diagnosis]) -> Text:
    """渲染 `reports/analysis.md` 的归因分布段（六分类计数 + 文档缺陷条目）。"""
    counts = {c: 0 for c in CAUSE_TYPES}
    for d in diags:
        counts[d.cause_type] = counts.get(d.cause_type, 0) + 1
    lines = ["## 归因分布（六分类）", ""]
    lines.append("| 类别 | 条数 |")
    lines.append("| --- | --- |")
    for c in CAUSE_TYPES:
        lines.append(f"| {c} | {counts.get(c, 0)} |")
    lines.append("")
    doc_defects = [d for d in diags if d.cause_type == CAUSE_DOC_DEFECT]
    if doc_defects:
        lines.append(
            f"**文档与实现不符：{len(doc_defects)} 条**——已汇总进 "
            "`reports/doc_defects.md`（交付文档维护者；修好后请重跑对应用例）"
        )
        lines.append("")
        for d in doc_defects:
            lines.append(f"### {d.case} / {d.step}")
            lines.append(f"- 不符点：{d.reason}")
            lines.append(f"- 建议改法：{d.action}")
            lines.append(_fmt_quotes(d.evidence_quotes))
            lines.append("")
    return "\n".join(lines)


def render_doc_defects(diags: Sequence[Diagnosis]) -> Text:
    """渲染 `reports/doc_defects.md`（回流清单：哪一句 / 与实际哪里不符 / 建议改法）。"""
    items = [d for d in diags if d.cause_type == CAUSE_DOC_DEFECT]
    out = [
        "# 文档缺陷清单（由 interfacetester_ai.diagnosis 回流生成）",
        "",
        "> 每条都引用**文档原句**与**实际响应**片段（§5.3 证据优先；无证据的结论不会出现在这里）。",
        "> **用途**：交付接口文档维护者。修好文档后请重跑对应用例——本清单同时是回归输入（防改回去）。",
        "",
    ]
    for i, d in enumerate(items, 1):
        out.append(f"## {i}. {d.case} / {d.step}")
        out.append("")
        out.append(f"- **不符点**：{d.reason}")
        out.append(f"- **建议改法**：{d.action}")
        out.append("- **证据**：")
        out.append(_fmt_quotes(d.evidence_quotes))
        out.append("")
    return "\n".join(out)


def write_doc_defects(diags: Sequence[Diagnosis]) -> Optional[Text]:
    """有「文档与实现不符」才落盘 `reports/doc_defects.md`；返回相对路径或 None。

    - **无条目 → 不落盘**（不留空清单让人误以为有活要干，同 `pending.py` 口径）；
    - cwd 必须是工作区根：`reports/` 是固定相对根（§6 写盘边界，红线③ 登记项）；
    - 写盘行**内联字面量前缀**（T18 白名单纪律：变量目标会被判"不可静态判定"）。
    """
    items = [d for d in diags if d.cause_type == CAUSE_DOC_DEFECT]
    if not items:
        return None
    os.makedirs("reports", exist_ok=True)  # 写入根字面量（T18 静态可判）
    with open("reports/doc_defects.md", "w", encoding="utf-8") as fp:
        fp.write(render_doc_defects(items))
    return DOC_DEFECTS_REL_PATH


def clear_stale_doc_defects() -> bool:
    """清掉**早先轮次**留下的 `reports/doc_defects.md`（本轮没有缺陷时调它）。

    ★为什么必须清：陈旧清单会与现状**自相矛盾**（文档已经修好了，清单还在说它有问题）——
    与 `assembler.clear_stale_needs_human()` 同一条道理（本仓为"陈旧留痕"吃过亏）。
    """
    if not os.path.isfile(DOC_DEFECTS_REL_PATH):
        return False
    os.remove(DOC_DEFECTS_REL_PATH)  # 写入根字面量（T18 静态可判）
    return True


__all__ = [
    "CAUSE_CASE_ERROR",
    "CAUSE_DOC_DEFECT",
    "CAUSE_ENV_ISSUE",
    "CAUSE_REAL_DEFECT",
    "CAUSE_TEST_DATA_ISSUE",
    "CAUSE_TYPES",
    "CAUSE_TYPES_REQUIRING_BOTH_SIDES",
    "CAUSE_UNKNOWN",
    "DOC_DEFECTS_REL_PATH",
    "clear_stale_doc_defects",
    "Diagnosis",
    "normalize_diagnosis",
    "normalize_text",
    "render_analysis_section",
    "render_doc_defects",
    "run_selftest",
    "validate_diagnosis",
    "validate_taxonomy",
    "write_doc_defects",
]

# ---------------------------------------------------------------------------
# 自检（红绿成对：违规必红 / 合规必绿）
# ---------------------------------------------------------------------------

_DOC_SAMPLE = (
    "响应字段说明：\n"
    "| 字段 | 类型 | 说明 |\n"
    "| status | int | 订单状态，1=已支付 |\n"
)
_RESP_SAMPLE = 'HTTP 200\n{"status": "paid", "order_id": 1001}\n'


def _diag(cause_type: Text, quotes: List[Dict[Text, Text]], **kw: Any) -> Diagnosis:
    return Diagnosis(
        case=kw.get("case", "order_create"),
        step=kw.get("step", "create order"),
        cause_type=cause_type,
        reason=kw.get("reason", "文档写 status 是 int=1，实际返回字符串 paid"),
        action=kw.get("action", "把文档的 status 类型与取值改为 string 枚举"),
        confidence=kw.get("confidence", 0.9),
        evidence_quotes=quotes,
    )


def run_selftest(verbose: bool = False) -> int:
    """跑自检。返回 0=全绿；非 0=失败项数。"""
    failures: List[Text] = []

    # ① 六分类枚举齐全（缺格必红的前提）
    missing = validate_taxonomy(CAUSE_TYPES)
    if missing:
        failures.append(f"[缺格] CAUSE_TYPES 缺类别：{missing}")
    if CAUSE_DOC_DEFECT not in CAUSE_TYPES:
        failures.append("[缺格] 六分类里没有「文档与实现不符」（R15 的核心一格）")
    if len(CAUSE_TYPES) != 6:
        failures.append(f"[口径] 六分类应为 6 类，实测 {len(CAUSE_TYPES)} 类")

    # ② 缺格必红（元护栏：拿"旧五分类"喂进去，必须报缺）
    legacy_five = [
        CAUSE_REAL_DEFECT,
        CAUSE_CASE_ERROR,
        CAUSE_ENV_ISSUE,
        CAUSE_TEST_DATA_ISSUE,
        CAUSE_UNKNOWN,
    ]
    if validate_taxonomy(legacy_five) != [CAUSE_DOC_DEFECT]:
        failures.append("[元护栏] 旧五分类没有被检出缺「文档与实现不符」")

    # ③ 合规必绿：双侧证据齐全 → 保持文档缺陷
    good = _diag(
        CAUSE_DOC_DEFECT,
        [
            {"source": "doc", "text": "| status | int | 订单状态，1=已支付 |"},
            {"source": "response", "text": '"status": "paid"'},
        ],
    )
    if normalize_diagnosis(good, _DOC_SAMPLE, _RESP_SAMPLE).cause_type != CAUSE_DOC_DEFECT:
        failures.append("[误伤] 双侧证据齐全的文档缺陷被判不合法")

    # ④ 违规必红：缺 response 侧证据 → 降级
    only_doc = _diag(
        CAUSE_DOC_DEFECT,
        [{"source": "doc", "text": "| status | int | 订单状态，1=已支付 |"}],
    )
    if normalize_diagnosis(only_doc, _DOC_SAMPLE, _RESP_SAMPLE).cause_type != CAUSE_UNKNOWN:
        failures.append("[漏判] 只有文档侧证据的\"文档缺陷\"未被降级")

    # ⑤ 违规必红：编引用（原文里没有那句话）→ 降级
    fabricated = _diag(
        CAUSE_DOC_DEFECT,
        [
            {"source": "doc", "text": "文档第 9 节说明 status 是 int"},
            {"source": "response", "text": '"status": "paid"'},
        ],
    )
    if normalize_diagnosis(fabricated, _DOC_SAMPLE, _RESP_SAMPLE).cause_type != CAUSE_UNKNOWN:
        failures.append("[漏判] 编出来的引用（非原文逐字子串）未被降级")

    # ⑥ 违规必红：无证据 → 降级（§5.3 证据优先）
    no_evidence = _diag(CAUSE_REAL_DEFECT, [])
    if normalize_diagnosis(no_evidence).cause_type != CAUSE_UNKNOWN:
        failures.append("[漏判] 无证据的归因未被降级为未知待人工确认")

    # ⑦ 合规必绿：非文档缺陷类别只需"有证据"
    plain = _diag(CAUSE_CASE_ERROR, [{"source": "log", "text": "WARNING ..."}])
    if normalize_diagnosis(plain).cause_type != CAUSE_CASE_ERROR:
        failures.append("[误伤] 有证据的普通类别被判不合法")

    # ⑧ 渲染 + 写盘形态（无文档缺陷不落盘）
    if CAUSE_DOC_DEFECT not in render_analysis_section([good]):
        failures.append("[渲染] analysis 片段未含文档缺陷条目")
    if "文档原句" not in render_doc_defects([good]):
        failures.append("[渲染] doc_defects 清单未含文档原句")

    import tempfile

    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="diag_selftest_") as tmp:
        try:
            os.chdir(tmp)
            if write_doc_defects([good]) is None or not os.path.isfile(
                "reports/doc_defects.md"
            ):
                failures.append("[写盘] 有文档缺陷却没落盘 reports/doc_defects.md")
            elif not os.path.relpath("reports/doc_defects.md", ".").startswith("reports"):
                failures.append("[写盘] 落盘路径不在 reports/ 根下")
            if write_doc_defects([plain]) is not None:
                failures.append("[写盘] 无文档缺陷却返回了路径（应不落盘）")
        finally:
            os.chdir(old_cwd)

    print("=" * 66)
    if failures:
        print(f"归因六分类与文档缺陷回流自检**失败** {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        print("=" * 66)
        return len(failures)

    print("归因六分类与文档缺陷回流自检全部通过：")
    print(f"  分类枚举          ：{len(CAUSE_TYPES)} 类齐全（含「{CAUSE_DOC_DEFECT}」）")
    print("  缺格必红（元护栏）：旧五分类被检出缺「文档与实现不符」")
    print("  证据纪律          ：双侧证据必绿 / 缺一侧必降级 / 编引用必降级 / 无证据必降级")
    print("  回流清单          ：reports/doc_defects.md 有缺陷才落盘、根形态正确")
    print("=" * 66)
    return 0


def main() -> int:
    # NOTICE（T26 同款纪律）：重定向 + GBK locale 下也要字节级可读、退出码可靠
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    return run_selftest()


if __name__ == "__main__":
    raise SystemExit(main())

