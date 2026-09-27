# -*- coding: utf-8 -*-
"""analyze —— 失败诊断的**端到端装配**（v8 §5.3 `haify analyze`；**P1a**，2026-09-25）。

## 形状（与 §5.3 的表一一对应）

    ① evidence.build_evidence_packs(summary)   纯代码：失败断言 → 证据包（输入装配）
    ② prompts.render_diagnosis_*               纯代码：包进 `<untrusted-data id=n>`
    ③ llm.complete(...)                        **一次调用**，逐条要引用
    ③' ★§9.24 **修正环**：结构不合格 → 把"哪里不合格"回喂，重试 ≤ `MAX_ROUNDS` 轮
    ④ diagnosis.normalize_diagnosis(...)       纯代码：证据纪律（不合格 → 降级"未知"）
    ⑤ 渲染 `reports/analysis.md`（+ `doc_defects.md` 回流）

## 三个"为什么这么接"

1. **一次调用**（§5.3"批量与成本"）：所有证据包放进**同一个请求**。逐包调用会让成本
   随失败条数线性涨，而且模型看不到彼此——同类失败本可以互相参照。
2. **降级在代码里做，不在提示里做**：模型被要求带引用，但**是否合格由
   `diagnosis.validate_diagnosis` 判**。把校验寄希望于模型自觉，就等于没有校验。
3. **断网不写半个文件**：先渲染成文本、**再**原子写；传输异常直接冒泡成
   `LLMTransportError`（CLI 退出码 3）。"写了一半的 `analysis.md`"比不写更坏——
   人会以为分析已经做完了。

## ★§9.24 为什么要补"输出契约 + 修正环"（真实事故，2026-09-27）

同一份真 summary、同一台 ollama 上跑 `haify analyze`：

| 模型 | 结果 |
| --- | --- |
| `qwen3.8:27b` | ✅ 成功：6 条证据包 → 6 条结论，未降级 6/6 = 100%，引用逐字可溯源 |
| `gemma4:latest` | ❌ 返回 `<unused50>`×N（**不是 JSON**）→ `LLMResponseFormatError` → **无报告** |
| `gemma4:12b` | ❌ 同上 |

两个真缺陷（与 §9.15"gen 没给契约"是同一类事故）：

1. **system prompt 没给输出形状** —— 只说"输出符合给定 JSON 结构的一个 JSON 对象"，
   而那份结构**从来没给过**（`response_format` 是 `json_object`，但形状仍要模型猜）；
2. **没有修正环** —— `gen` 有 `MAX_ROUNDS=2`，analyze 一次坏响应就**整次失败** ✗；
   而 `llm.complete` 的重试只覆盖**传输层**（5xx），**格式错不在其内**。

所以这里补两样（缺一样都不解决问题）：**契约**（`diagnosis.diagnosis_contract_text()`
→ 渲染进 system prompt，与 `parse_diagnoses(strict=True)` 的判据**同源**）+
**修正环**（回喂"哪里不合格"，轮数与回喂文本长度**复用 `gen` 的口径**，不另立一套）。
"能不能跑"从此是**可观测**的：报告里记 `rounds`，探针
（`probe_p2_prep/probe_analyze_models.py`）按模型统计成功率。

## 未降级率（§5.3 的验收指标）

`kept_ratio = 未降级条数 / 总条数`，**写进报告本身**。它是"模型的证据纪律好不好"的温度计：
太低说明提示或校验该收紧，而不是一句"模型不行"——所以它必须**可见**。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

from interfacetester_ai.diagnosis import (
    CAUSE_TYPES,
    CAUSE_UNKNOWN,
    DIAGNOSIS_ITEM_REQUIRED_FIELDS,
    DIAGNOSIS_QUOTE_FIELDS,
    DIAGNOSIS_TOP_FIELDS,
    DOC_DEFECTS_REL_PATH,
    Diagnosis,
    normalize_diagnosis,
    render_analysis_section,
    write_doc_defects,
)
from interfacetester_ai.evidence import EvidencePack, build_evidence_packs
# ★§9.24 修正环的**轮数与回喂长度**复用 `gen` 的口径（`Round` / `truncate_feedback`）——
#   "回喂有界""重试必须改变输入"这些都是同一套规矩，各写一份必然漂移。
from interfacetester_ai.gen import MAX_ROUNDS as GEN_MAX_ROUNDS, Round, truncate_feedback
from interfacetester_ai.llm import (
    FakeTransport,
    LLMRequest,
    LLMResponseFormatError,
    LLMTransportError,
    Transport,
    complete,
    parse_json_response,
)
from interfacetester_ai.prompts import (
    PROMPT_VERSION,
    render_diagnosis_system_prompt,
    render_diagnosis_user_prompt,
)

# 分析报告落点（与 `diagnosis` 同根 `reports/`；T18 注册表登记项）
ANALYSIS_REL_PATH = "reports/analysis.md"

# 诊断默认采样参数（温度 0：归因要稳，不要"创意"）
DIAGNOSIS_TEMPERATURE = 0.0
DIAGNOSIS_MAX_TOKENS = 2048

# ★§9.24 修正环轮数：**从 gen 取**（不手抄一个 2 —— 抄了必然漂移，同 `SLICE_ENDPOINT_RE` 的处置）
MAX_ROUNDS = GEN_MAX_ROUNDS


class DiagnosisFailed(Exception):
    """修正环**用尽**仍拿不到合格结构 → 响亮失败（CLI 退出码 4）。

    ★为什么宁可失败也不产半成品：一份基于"没解析出来的东西"的报告，读者会以为分析过了。
    所以这条异常在**任何写盘之前**抛出——`reports/analysis.md` 一个字节都不落。
    """

    def __init__(self, rounds: Sequence["Round"]) -> None:
        detail = "\n".join(f"  - 第 {item.index} 轮 [{item.kind}] {item.message}" for item in rounds)
        super().__init__(
            f"连续 {len(rounds)} 轮都没拿到合格的诊断结构（模型一直给不出契约要求的 JSON）：\n"
            f"{detail}\n\n"
            "  处置建议：换一个更强的模型（实测 27B 级可用、8B 级会输出乱码 token），"
            "或先用 `--fake` 回放既有缓存。"
        )
        self.rounds = list(rounds)


class DiagnosisContractError(Exception):
    """诊断输出**不符合契约**（结构层）——由 `parse_diagnoses(strict=True)` 抛出。

    ★只判**形状**，不判**证据**：证据是否可溯源由 `diagnosis.validate_diagnosis` 判并降级
    （"未知待人工确认"是合法结论 ✓）。两者分工不能混——混了就会把"模型守规矩地说不知道"
    当成结构失败去重试，白烧钱。
    """

    def __init__(self, problems: Sequence[Text]) -> None:
        self.problems = list(problems)
        super().__init__("；".join(self.problems))


def classify_diagnosis_error(error: BaseException) -> Tuple[Text, Text]:
    """把异常翻成 `(类别, 回喂文本)`——与 `gen.classify_error` 同取向（按报错类别选模板）。"""
    if isinstance(error, LLMResponseFormatError):
        return "l1-format", truncate_feedback(
            f"{error}\n\n你上一轮的回复**不是合法 JSON**（可能是残缺、夹了解释文字、"
            "或混进了模型特殊 token）。请**只输出一个 JSON 对象**：不要解释性文字、"
            "不要 markdown 围栏、不要把自己写的注释留在 JSON 里。"
        )
    if isinstance(error, DiagnosisContractError):
        problems = "\n".join(f"  - {item}" for item in error.problems)
        return "l1-schema", truncate_feedback(
            f"你的 JSON **结构不符合输出契约**（{len(error.problems)} 处）：\n{problems}\n\n"
            "逐条修正上面每一项后重新输出**完整**的 JSON（不要只输出被改的部分）。"
        )
    return "l1-other", truncate_feedback(f"上一轮被拦下：{type(error).__name__}: {error}")


@dataclass
class AnalyzeResult:
    """一次分析的结果（含"证据纪律"的统计口径）。"""

    packs: int = 0
    diagnoses: List[Diagnosis] = field(default_factory=list)
    kept: int = 0  # 未降级条数（§5.3 的"未降级占比"分子）
    degraded: int = 0
    cached: bool = False
    attempts: int = 0
    rounds: List[Round] = field(default_factory=list)  # ★§9.24 修正环轨迹（每轮被拦下的原因）
    analysis_path: Text = ""
    doc_defects_path: Text = ""

    def kept_ratio(self) -> float:
        """未降级占比（无诊断时返回 0.0——**没有比 0 更好看的默认值**）。"""
        total = self.kept + self.degraded
        return (self.kept / total) if total else 0.0

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "packs": self.packs,
            "diagnoses": len(self.diagnoses),
            "kept": self.kept,
            "degraded": self.degraded,
            "kept_ratio": round(self.kept_ratio(), 4),
            "cached": self.cached,
            "attempts": self.attempts,
            "rounds": [
                {"index": item.index, "kind": item.kind, "message": item.message}
                for item in self.rounds
            ],
            "analysis_path": self.analysis_path,
            "doc_defects_path": self.doc_defects_path,
        }


# ---------------------------------------------------------------------------
# 请求装配与解析（纯代码）
# ---------------------------------------------------------------------------

def build_diagnosis_request(
    packs: Sequence[EvidencePack],
    *,
    model: Text,
    base_url: Text,
    doc_text: Text = "",
    temperature: float = DIAGNOSIS_TEMPERATURE,
    max_tokens: int = DIAGNOSIS_MAX_TOKENS,
    seed: int = 0,
    feedback: Optional[Tuple[Text, Text]] = None,
) -> LLMRequest:
    """组装诊断请求（所有证据包在同一个 user prompt 里，§5.3"批量与成本"）。

    - `expected` 由 `len(packs)` 传进 system prompt 的契约（"必须 N 条"）；
    - `feedback` = 上一轮被拦下的原因（**§9.24 修正环**；首轮为 `None`）。
    """
    return LLMRequest(
        system=render_diagnosis_system_prompt(len(packs)),
        user=render_diagnosis_user_prompt(packs, doc_text, feedback),
        model=model,
        base_url=base_url,
        prompt_version=PROMPT_VERSION,
        temperature=temperature,
        seed=seed,
        max_tokens=max_tokens,
        response_format="json_object",
    )


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _diagnosis_item_problems(item: Any, index: int) -> List[Text]:
    """一条诊断的**结构**问题（§9.24 契约的判据；**不判证据真伪**）。"""
    if not isinstance(item, dict):
        return [f"第 {index} 条不是 JSON 对象（实际 {type(item).__name__}）"]
    problems: List[Text] = []
    for key in DIAGNOSIS_ITEM_REQUIRED_FIELDS:
        if key not in item:
            problems.append(f"第 {index} 条缺必填键 `{key}`")
            continue
        if key == "evidence_quotes":
            continue  # ★允许空数组：模型守规矩地说"没有可引用的原文"是**合法结论** ✓
        if item.get(key) in (None, ""):
            problems.append(f"第 {index} 条的 `{key}` 为空")
    quotes = item.get("evidence_quotes")
    if quotes is not None and not isinstance(quotes, list):
        problems.append(f"第 {index} 条的 `evidence_quotes` 必须是数组")
    elif isinstance(quotes, list):
        for quote_index, quote in enumerate(quotes, 1):
            if not isinstance(quote, dict):
                problems.append(f"第 {index} 条的第 {quote_index} 个引用不是对象")
                continue
            extra = [key for key in quote if key not in DIAGNOSIS_QUOTE_FIELDS]
            if extra:
                problems.append(
                    f"第 {index} 条的第 {quote_index} 个引用多出键 {extra}"
                    f"（只许 {list(DIAGNOSIS_QUOTE_FIELDS)}）"
                )
            if not str(quote.get("source") or "").strip():
                problems.append(f"第 {index} 条的第 {quote_index} 个引用缺 `source`")
            if not str(quote.get("text") or "").strip():
                problems.append(f"第 {index} 条的第 {quote_index} 个引用缺 `text`")
    return problems


def parse_diagnoses(text: Text, *, strict: bool = False, expected: int = 0) -> List[Diagnosis]:
    """解析模型输出 → `Diagnosis` 列表。

    - `strict=False`（默认）：**宽容解析**（字段缺失用默认值，绝不因此崩）。
      ★为什么宽容：模型少给一个 `confidence` 不该让整次分析失败——那条诊断**合格与否**
      由证据纪律（`normalize_diagnosis`）说。**解析层宽容 + 校验层严格**，比"解析层就崩"
      有用得多：前者能告诉用户"哪一条有问题"，后者只给一个异常。
    - `strict=True`（**§9.24 修正环**用）：结构不合格直接抛 `DiagnosisContractError`，
      由调用方把问题**回喂**重试。★只判**形状**（顶层键 / 必填键 / 引用项键 / 条数），
      不判证据真伪、不判类别合法性——那两件事的出口是**降级**，不是重试。
      （混起来会把"模型守规矩地说不知道"当结构失败去重试，白烧钱 ✗）
    """
    payload = parse_json_response(text)  # 非 JSON → LLMResponseFormatError（同样被回喂）

    problems: List[Text] = []
    if strict and not isinstance(payload, dict):
        problems.append(f"顶层必须是 JSON 对象，实际是 {type(payload).__name__}")
    if strict and isinstance(payload, dict):
        extra = [key for key in payload if key not in DIAGNOSIS_TOP_FIELDS]
        if extra:
            problems.append(
                f"顶层多出契约之外的键 {extra}（只许 {list(DIAGNOSIS_TOP_FIELDS)}）"
            )

    items = payload.get("diagnoses") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        if strict:
            raise DiagnosisContractError(problems + ['缺少 "diagnoses"（必须是一个数组）'])
        return []
    if strict:
        if expected and len(items) != expected:
            problems.append(
                f"证据包 {expected} 条，但 `diagnoses` 给了 {len(items)} 条（必须一一对应）"
            )
        for index, item in enumerate(items, 1):
            problems.extend(_diagnosis_item_problems(item, index))
        if problems:
            raise DiagnosisContractError(problems)

    out: List[Diagnosis] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        quotes = item.get("evidence_quotes")
        out.append(
            Diagnosis(
                case=str(item.get("case", "")),
                step=str(item.get("step", "")),
                cause_type=str(item.get("cause_type", "")),
                reason=str(item.get("reason", "")),
                action=str(item.get("action", "")),
                confidence=_as_float(item.get("confidence")),
                evidence_quotes=(
                    [dict(q) for q in quotes if isinstance(q, dict)]
                    if isinstance(quotes, list)
                    else []
                ),
            )
        )
    return out


def haystack_for(diag: Diagnosis, packs: Sequence[EvidencePack]) -> Text:
    """找**这条诊断对应的**证据包 → 返回它的可引用原文（§5.3："对应切片的逐字子串"）。

    ★为什么"按包"而不是"所有包拼一起"：拼起来会让 **A 用例的结论引用 B 用例的响应**
    也通过校验——那叫"引用存在"，但不叫"引用有据"，正是本条判据要防的形态。
    匹配 `case` + `step`；匹配不到 → 空串 → 这条必然降级。
    **这也是对的**：模型说了个不存在的用例，本身就是无据。
    """
    for pack in packs:
        if pack.case == diag.case and pack.step == diag.step:
            return pack.haystack()
    return ""


def diagnose_packs(
    diagnoses: Sequence[Diagnosis], packs: Sequence[EvidencePack], *, doc_text: Text = ""
) -> Tuple[List[Diagnosis], int, int]:
    """逐条做证据纪律归一 → `(归一后的诊断, 未降级数, 降级数)`。

    ★`未降级` 与 "说未知" 是**两件事**（这个区分决定了指标是否可信）：
    - 模型**自己**判断"无证据 → 未知待人工确认"：那是它**遵守了规则**，算**未降级**；
    - 结论被**代码**判为不合法而改写（`reason` 里带"已降级："）：那才是**降级**。
    混为一谈的话，模型越守规矩、指标越难看——正好把信号读反。
    """
    normalized: List[Diagnosis] = []
    kept = degraded = 0
    for diag in diagnoses:
        fixed = normalize_diagnosis(diag, doc_text, haystack_for(diag, packs))
        normalized.append(fixed)
        # ★判定看**类别变化**，不看 `reason` 里的标记（标记只是给人看的说明文字，
        #   拿它当判据会把"措辞变了"变成"指标变了"）：
        #   - 原本就是「未知待人工确认」→ 模型**遵守了规则**，算未降级；
        #   - 原本给了类别、被代码改成未知 → 那才是**降级**。
        if diag.cause_type == CAUSE_UNKNOWN or fixed.cause_type != CAUSE_UNKNOWN:
            kept += 1
        else:
            degraded += 1
    return normalized, kept, degraded


# 降级标记（由 `diagnosis.normalize_diagnosis` 写入 `reason`）；
# 用它区分"**被代码**降级"与"模型自报未知"——两者的意义完全不同（见 diagnose_packs）
DEGRADED_MARK = "（已降级："


# ---------------------------------------------------------------------------
# 渲染与落盘（`reports/analysis.md`，**原子写**）
# ---------------------------------------------------------------------------

def render_analysis_report(result: AnalyzeResult, *, source: Text = "") -> Text:
    """渲染 `reports/analysis.md`：**证据纪律统计** + 六分类分布 + 逐条结论。"""
    from interfacetester_ai.diagnosis import _fmt_quotes  # noqa: PLC0415 - 同包复用，避免两套引用格式

    lines = [
        "# 失败分析（由 interfacetester_ai.analyze 生成）",
        "",
        "> 每条结论都必须引用**原文片段**（响应体 / 日志 / 文档原句）。",
        "> 无证据或引用对不上的结论已被降级为「未知待人工确认」——**降级不是失败，是纪律**（§5.3）：",
        "> 一个编出来的原因会让人往错的方向排查，代价远大于「这条需要人工看」。",
        "",
    ]
    if source:
        lines += [f"- 输入：`{source}`", ""]

    lines += [
        "## 证据纪律",
        "",
        f"- 证据包：**{result.packs}** 条失败断言",
        f"- 结论：**{len(result.diagnoses)}** 条"
        f"（未降级 **{result.kept}** / 降级 **{result.degraded}**）",
        # ★§9.24：修正环轮数**必须可见** —— "这次跑了几轮才拿到合法结构"是模型可用性的一手证据
        f"- 修正环：**{len(result.rounds)}** 轮被拦下"
        + (
            "（"
            + "；".join(f"第 {item.index} 轮 {item.kind}" for item in result.rounds)
            + "）"
            if result.rounds
            else "（首发即合格）"
        ),
        f"- **未降级占比：{result.kept_ratio():.0%}**（§5.3 的验收线是 ≥70%；"
        "低于它说明**提示或校验该收紧**，而不是「模型不行」——所以这个数字必须写在报告里）",
        "",
    ]

    lines.append(render_analysis_section(result.diagnoses))

    if result.diagnoses:
        lines += ["## 逐条结论", ""]
        for diag in result.diagnoses:
            flag = "（**已降级**）" if DEGRADED_MARK in diag.reason else ""
            lines.append(f"### {diag.case} / {diag.step} — **{diag.cause_type}**{flag}")
            lines.append("")
            lines.append(f"- 理由：{diag.reason}")
            lines.append(f"- 建议：{diag.action}")
            lines.append(f"- 置信度：{diag.confidence}")
            if diag.evidence_quotes:
                lines.append("- 证据：")
                lines.append(_fmt_quotes(diag.evidence_quotes))
            lines.append("")

    return "\n".join(lines)


def write_analysis_report(text: Text) -> Text:
    """**原子写** `reports/analysis.md`（先 `.tmp` 再 `os.replace`）。返回相对路径。

    ★为什么要原子写：§5.3 的验收里有一条"**断网退出码 3 且无部分写**"。
    半份报告比没有报告更坏——人会以为分析已经做完了，而它其实在写到一半时就断了。
    """
    os.makedirs("reports", exist_ok=True)  # 内联字面量（T18 静态可判）
    with open("reports/analysis.md.tmp", "w", encoding="utf-8") as fp:
        fp.write(text)
    os.replace("reports/analysis.md.tmp", "reports/analysis.md")
    return ANALYSIS_REL_PATH


def analyze_summary(
    summary: Any,
    *,
    transport: Transport,
    config: Any = None,
    doc_text: Text = "",
    cache: Optional[Any] = None,
    audit: Optional[Any] = None,
    write: bool = True,
    max_rounds: int = MAX_ROUNDS,
) -> AnalyzeResult:
    """`summary.json` → 证据包 → 模型调用（**≤ max_rounds+1 轮**）→ 证据纪律归一 → `reports/analysis.md`。

    返回 `AnalyzeResult`；**没有失败断言时不调模型、不落盘**（"空报告"会被误读成
    "分析过了"——没有内容就不该留下工件）。

    ★`transport` / `cache` / `audit` 都是**注入**的（与 `llm.complete` 同取向）：
    CI 用 `FakeTransport` 回放，真跑用 `HttpTransport`。**断网时**
    `transport.complete` 抛 `LLMTransportError` → 本函数**一个文件都没写**——
    这不是碰巧，是"先渲染、后落盘"的顺序决定的。

    ★§9.24 **修正环**：结构不合格（非 JSON / 不合契约）→ 把"哪里不合格"回喂重试，
    轮数上限 `max_rounds`（与 `gen` 同口径）。**用尽仍不合格 → 抛 `DiagnosisFailed`**
    （CLI 退出码 4），且**在任何写盘之前**——半份报告比没有报告更坏。
    """
    packs = build_evidence_packs(summary)
    result = AnalyzeResult(packs=len(packs))
    if not packs:
        return result

    model = getattr(config, "model", "") or ""
    base_url = getattr(config, "base_url", "") or ""
    feedback: Optional[Tuple[Text, Text]] = None
    diagnoses: List[Diagnosis] = []
    result.cached = True  # ★逐轮 AND：只要有一轮真调了模型，就不是"缓存回放"
    for index in range(1, max_rounds + 2):
        request = build_diagnosis_request(
            packs, model=model, base_url=base_url, doc_text=doc_text, feedback=feedback
        )
        # ★下面是唯一会出网的一行；它抛异常时下方所有写盘都还没发生（"无部分写"）
        response = complete(request, transport=transport, config=config, cache=cache, audit=audit)
        result.cached = result.cached and bool(response.cached)
        result.attempts += response.attempts
        try:
            diagnoses = parse_diagnoses(response.text, strict=True, expected=len(packs))
        except (LLMResponseFormatError, DiagnosisContractError) as error:
            kind, text = classify_diagnosis_error(error)
            result.rounds.append(Round(index, kind, text))
            feedback = (kind, text)  # ★重试必须**改变输入**（回喂上一轮的原因）
            continue
        break
    else:
        raise DiagnosisFailed(result.rounds)

    diagnoses, kept, degraded = diagnose_packs(diagnoses, packs, doc_text=doc_text)
    result.diagnoses = diagnoses
    result.kept = kept
    result.degraded = degraded

    if write:
        result.analysis_path = write_analysis_report(render_analysis_report(result))
        result.doc_defects_path = write_doc_defects(diagnoses) or ""
    return result


# ---------------------------------------------------------------------------
# 自检（**不写盘**：`write=False`，回放用 `FakeTransport`）
# ---------------------------------------------------------------------------

_SELFTEST_DOC = """# 下单

## POST /api/order

| 字段 | 类型 | 必填 |
| --- | --- | --- |
| sku | string | 是 |

- 库存不足：`code: 1001`，`message: 库存不足`
"""

# 模型回放：3 条结论，分别代表"**证据齐全**"、"**模型自报未知**"、"**编引用**"三种形态。
# 这三种的分野正是 §5.3 要区分的：前两种都算"未降级"（后者是模型**遵守了规则**），
# 只有第三种由**代码**判为不合法并改写——把它算进"降级"才是对的。
_SELFTEST_REPLAY = json.dumps(
    {
        "diagnoses": [
            {
                "case": "下单_正常",
                "step": "下单",
                "cause_type": "真实缺陷",
                "confidence": 0.8,
                "reason": "响应返回业务码 1001，而用例期望 0",
                "action": "核对库存逻辑",
                "evidence_quotes": [{"source": "response", "text": "库存不足"}],
            },
            {
                "case": "下单_边界",
                "step": "下单",
                "cause_type": "未知待人工确认",
                "confidence": 0.0,
                "reason": "证据不足，交人工",
                "action": "人工复核",
                "evidence_quotes": [],
            },
            {
                "case": "下单_超时",
                "step": "下单",
                "cause_type": "用例错误",
                "confidence": 0.9,
                "reason": "我认为断言写错了",
                "action": "改用例",
                "evidence_quotes": [{"source": "response", "text": "这段原文根本不存在"}],
            },
        ]
    },
    ensure_ascii=False,
)


def _selftest_summary() -> Dict[Text, Any]:
    """两个失败断言（两个用例）→ 两个证据包。"""

    def detail(name: str, expect: Any) -> Dict[Text, Any]:
        return {
            "name": name,
            "success": False,
            "log": "",
            "records": [
                {
                    "name": "下单",
                    "data": {
                        "validators": {
                            "validate": [
                                {
                                    "comparator": "equal",
                                    "check": "body.code",
                                    "expect": 0,
                                    "expect_value": expect,
                                    "check_result": "fail",
                                }
                            ]
                        },
                        "req_resps": [
                            {
                                "request": {"method": "POST", "url": "/api/order"},
                                "response": {
                                    "status_code": 200,
                                    "headers": {"Content-Type": "application/json"},
                                    "body": '{"code": 1001, "message": "库存不足"}',
                                },
                            }
                        ],
                    },
                }
            ],
        }

    return {
        "success": False,
        "details": [detail("下单_正常", 1001), detail("下单_边界", 1001), detail("下单_超时", 1001)],
    }


def run_selftest(verbose: bool = False) -> int:
    """analyze 自检（**不写盘**：全程 `write=False` + `FakeTransport`）。返回 0=全绿。

    ★为什么自检里敢跑"看起来像端到端"的流程：因为**断网**——
    `FakeTransport` 不发请求，所以这里既验证了真实装配顺序（包 → prompt → 调用 → 校验 →
    渲染），又不需要网络、不需要 key。真出网那条路由 `HttpTransport` 与 L3 闸门管。
    """
    from interfacetester_ai.llm import LLMConfig  # noqa: PLC0415

    failures: List[Text] = []
    summary = _selftest_summary()
    config = LLMConfig(base_url="http://127.0.0.1:11434", model="fake")

    # ① 三个失败断言 → 三个证据包
    packs = build_evidence_packs(summary)
    if len(packs) != 3:
        failures.append(f"[取包] 应当 3 包，实际 {len(packs)}")

    # ② 回放三条结论（与三个包**一一对应**，§9.24 契约里的"必须 N 条"）
    #    → **只调一次模型**；未降级 2 / 降级 1
    transport = FakeTransport({"*": _SELFTEST_REPLAY})
    result = analyze_summary(
        summary, transport=transport, config=config, doc_text=_SELFTEST_DOC, write=False
    )
    if transport.call_count != 1:
        failures.append(f"[一次调用] 应当只调 1 次模型（§5.3 批量），实际 {transport.call_count}")
    if len(result.diagnoses) != 3:
        failures.append(f"[解析] 应当 3 条结论，实际 {len(result.diagnoses)}")
    if (result.kept, result.degraded) != (2, 1):
        failures.append(
            f"[纪律] 未降级/降级应当是 (2, 1)，实际 ({result.kept}, {result.degraded})"
        )

    # ③ **编引用那条**必须被降级：类别落到"未知"、置信度归 0、reason 标出降级
    degraded = [d for d in result.diagnoses if DEGRADED_MARK in d.reason]
    if len(degraded) != 1:
        failures.append(f"[降级] 应当恰好 1 条被代码降级：{[d.reason for d in result.diagnoses]}")
    elif degraded[0].cause_type != CAUSE_UNKNOWN or degraded[0].confidence != 0.0:
        failures.append(f"[降级] 应当落到「未知待人工确认」且置信度归 0：{degraded[0].to_dict()}")

    # ④ ★**模型自报未知**算"未降级"——它遵守了规则，不该被记成失败
    self_reported = [
        d
        for d in result.diagnoses
        if d.cause_type == CAUSE_UNKNOWN and DEGRADED_MARK not in d.reason
    ]
    if len(self_reported) != 1:
        failures.append("[口径] 模型自报未知应当算未降级（否则模型越守规矩、指标越难看）")

    # ⑤ 报告里要有**未降级占比**与六分类分布（指标不可见 = 没有指标）
    report = render_analysis_report(result)
    for token in ("未降级占比", "真实缺陷", "未知待人工确认"):
        if token not in report:
            failures.append(f"[报告] 缺 {token!r}")

    # ⑥ **没有失败断言 → 不调模型**（"空报告"会被误读成"分析过了"）
    idle = FakeTransport({"*": _SELFTEST_REPLAY})
    empty = analyze_summary(
        {"details": [{"name": "全过", "success": True, "records": []}]},
        transport=idle,
        config=config,
        write=False,
    )
    if idle.call_count != 0 or empty.packs != 0:
        failures.append("[短路] 没有失败断言时不该调模型、不该产报告")

    # ⑦ **断网** → 异常冒泡（CLI 靠它给退出码 3；且因为"先渲染后落盘"，一个文件都没写）
    try:
        analyze_summary(
            summary, transport=FakeTransport({"*": ""}, failures=99), config=config, write=False
        )
    except LLMTransportError:
        pass
    else:
        failures.append("[断网] 传输异常应当冒泡（CLI 靠它给退出码 3）")

    # ⑧ ★§9.24 **修正环**：首轮乱码 → 次轮合格 → 产出成功；且**重试改变了输入**
    sequence = ["<unused50><unused50><unused50>", _SELFTEST_REPLAY]
    retry = FakeTransport()

    def _respond(position: int) -> Text:
        return sequence[min(position, len(sequence)) - 1]

    retry.responder = lambda request: _respond(len(retry.calls))  # noqa: ARG005
    recovered = analyze_summary(summary, transport=retry, config=config, write=False)
    if retry.call_count != 2:
        failures.append(f"[修正环] 应当 2 次调用（首发 + 1 次修正），实际 {retry.call_count}")
    if len(recovered.rounds) != 1 or recovered.rounds[0].kind != "l1-format":
        failures.append(
            f"[修正环] 应当记下 1 轮 l1-format 被拦：{[r.to_dict() for r in recovered.rounds]}"
        )
    if not recovered.diagnoses:
        failures.append("[修正环] 修正后应当拿到结论（模型第二轮改好了）")
    if retry.call_count == 2 and retry.calls[0].user == retry.calls[1].user:
        failures.append("[修正环] 两次请求的 user prompt 一样 → 重试没有改变输入（等于白重试）")

    # ⑨ 用尽 → **响亮失败**（`DiagnosisFailed`），而不是产出一份空报告
    stuck = FakeTransport(responder=lambda request: "<unused50>")  # noqa: ARG005
    try:
        analyze_summary(summary, transport=stuck, config=config, write=False, max_rounds=1)
    except DiagnosisFailed as error:
        if len(error.rounds) != 2:
            failures.append(f"[用尽] 应当记 2 轮（max_rounds=1 + 首发），实际 {len(error.rounds)}")
    else:
        failures.append("[用尽] 一直给不出合法结构时应当抛 DiagnosisFailed（不产半成品）")

    # ⑩ 契约必须在 prompt 里，且**与校验同源**（改 `CAUSE_TYPES`，契约跟着变）
    from interfacetester_ai.diagnosis import audit_diagnosis_contract  # noqa: PLC0415

    if audit_diagnosis_contract():
        failures.append(f"[契约] 诊断 prompt 里缺关键串：{audit_diagnosis_contract()}")

    print("=" * 66)
    if failures:
        print(f"analyze 自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("失败诊断装配自检全部通过（§5.3，**不调真模型**）：")
    print("  装配  ：失败断言 → 证据包（`evidence.py`）→ `<untrusted-data>` → 调用（≤ max_rounds+1 轮）")
    print("  纪律  ：编引用的结论被**代码**降级成「未知待人工确认」（置信度归 0、标出原因）")
    print("  口径  ：模型**自报未知**算**未降级**（它遵守了规则，不该被记成失败）")
    print("  短路  ：没有失败断言时不调模型、不产报告（空报告会被当成'分析过了'）")
    print("  断网  ：异常冒泡 → CLI 退出码 3；**先渲染后落盘**，所以没有半个文件")
    print("  修正环：结构不合格 → 回喂重试（★重试**改变输入**）；用尽 → 抛错、不产半成品")
    print("  契约  ：输出形状进 system prompt，与 `parse_diagnoses(strict=True)` **同源**")
    print("=" * 66)
    return 0


def main() -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="失败诊断装配（P1a）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "ANALYSIS_REL_PATH",
    "DEGRADED_MARK",
    "DIAGNOSIS_MAX_TOKENS",
    "DIAGNOSIS_TEMPERATURE",
    "AnalyzeResult",
    "analyze_summary",
    "build_diagnosis_request",
    "diagnose_packs",
    "haystack_for",
    "parse_diagnoses",
    "render_analysis_report",
    "run_selftest",
    "write_analysis_report",
]


if __name__ == "__main__":
    raise SystemExit(main())




