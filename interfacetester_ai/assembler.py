# -*- coding: utf-8 -*-
r"""assembler —— **装配器**（§5.1 的"装配"行；**P0a** D 阶段）。

## 它是**唯一**写 `cases/` 的角色 —— 但**默认不写**（决策清单 §九 第一档②，2026-09-26）

    CaseDraft（模型产物，结构已被 schema.py 卡死）
      → 溯源校验（source_quote 必须真的出现在输入切片里）
      → **T28 四类确定性对账**（字段名 / expect 字面量 / 限定词↔算子 / 示例值）
      → IRCase/IRStep（纯代码）
      → sanitize_case（凭据占位兜底）→ emit_case → validate_emitted_case
      → 闸门前置（S-4：assert_testcase_passes；REJECT → 不落 cases/）
      → 覆盖矩阵 + "断言数 vs 覆盖字段数"（T28 ⑥）
      → **质量状态**（`quality.assess` → `AssemblyResult.quality`）
      → 产物**只落 `.ai/draft/`**；进 `cases/` **必须**经 `promote_draft()`（人工确认 + 留痕）

★**为什么默认不写 `cases/`**（2026-09-26 实证，见 `docs/未做完的待决策项.md` §九 9.4）：
改造前"过闸门且无 `TODO_`"就**直接复制**进 `cases/`，于是一份**只断言 `status_code`** 的薄用例
（`cases/T1_2_认证方式.yml`）躺在正式用例目录里，而 `.ai/reviews/` 里**零留痕**——
它和"人工复核过的用例"在调用方看来**没有区别**。
现在"装配"与"发布"分成两步（评审 §五 的落点规则）：**默认交付物是草稿**，
而 `ok()` 只表示"装配流程完成"，**不再**表示"可用"（可用性看 `quality`）。

**LLM 无权摸文件**（§5.1）：模型只到 `CaseDraft` 为止，从 `IRCase` 开始全是代码。

## 为什么"过了溯源校验"还不等于"意思对"（R16 / T28 的由来）

`source_quote` 命中只证明**文档里真有这句话**，不证明**这句话被理解对了**：

    quote: "单价 price：可选，整数"
    ✗ 生成 `type_match: [body.data.amount, "str"]`   ← 字段名错、类型也反

更麻烦的是这种产物**跑绿**（§9.2 R16「分叉型静默」）——R1（编造）会响亮失败，
分叉不会；而 `source_quote` 可能**仍然命中**（引用的是相邻那句）。所以 T28 把
"意思对不对"里**能用字符串判定的部分**判掉（四类规则），**判不了的不硬判**：
一律进 `unknowns` 交人工——**绝不假装判过**。

## 两级处置（§十 T28 的两个口径不冲突）

| 级别 | 触发 | 处置 |
| --- | --- | --- |
| **REJECT 级** | ③ 限定词↔算子**直接冲突**（文档说"可选"，却生成必填/存在性断言） | 该条断言**不进用例**（文档明说可选，断言必填 → 一定会误报失败），进 `unknowns` 并标 `rejected` |
| **DEGRADE 级** | ① 字段名不在引用句 ② expect 字面量不在引用句 ④ 硬编码示例值（文档未标"固定值"） | 该条断言**不进用例**，进 `unknowns` |

两级都**不拦整份用例**（形态可能合法，只是无据）——但报告里**分别标注**，
因为它们的"人去干什么"完全不同：REJECT 是"这里判错了"，DEGRADE 是"这里没证据"。

## 写盘边界（红线③）

登记为 `("roots", ("cases/", ".ai/"))`（`bench/write_boundary_scanner.py`）：

- **默认**：产物全部落 `.ai/` —— 草稿 YAML `.ai/draft/<case>.yml`、模型草案记录
  `.ai/draft/<case>.draft.json`、`unknowns` 清单 `.ai/draft/<case>.unknowns.json`、
  被拒草稿 `.ai/failed/<case>/`（含 `NEEDS_HUMAN.md`）；
- **只有人工转正**（`promote_draft()`，T24：`haify review --approve` + `.ai/reviews/` 留痕）
  才写 `cases/`（用例 YAML；`validate_emitted_case` 同目录产出的 `*_test.py`
  是 hmake 的正常产物，与本仓既有行为一致）。

**★S-4 的落点细化（诚实登记）**：§8.1 S-4 写"REJECT → `failed/` + `NEEDS_HUMAN.md`"。
把 `failed/` 放工作区根会**越出写盘白名单**（白名单按根判定，扩它要改注册表与
`tests/write_boundary_test.py` 的钉死断言）。而"**带陷阱的产物不落 `cases/`**"
这条实质判据与落在 `.ai/failed/` 完全一致（也符合"AI 产物进 `.ai/`"的边界口径）。
故细化为 `.ai/failed/<case>/NEEDS_HUMAN.md`。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

from interfacetester_ai.assertions import (
    KERNEL_CHECK_ROOTS,
    NOTE_RESPONSE_OWNERSHIP,
    has_todo,
    section_tree,
)
from interfacetester_ai.assertions import response_field_names_for
from interfacetester_ai.assertions import response_field_paths_for
from interfacetester_ai.assertions import section_text_for
from interfacetester_ai.citation import KIND_NONE as CITATION_NONE
from interfacetester_ai.citation import QuoteBinding
from interfacetester_ai.citation import bind as bind_quote_evidence
from interfacetester_ai.normalize import normalize_text
from interfacetester_ai.quality import (
    BLOCKER_STRUCTURE,
    QualityAssessment,
    assess as assess_quality,
    missing_blockers,
)
from interfacetester_ai.reviews import quality_of_draft, write_review_record
from interfacetester_ai.schema import CaseDraft, DraftAssertion, DraftStep

# 登记在册的写入根（相对工作区根；目标行必须**内联字面量前缀**，T18 静态可判纪律）
CASES_DIR = "cases"
DRAFT_DIR = ".ai/draft"
FAILED_DIR = ".ai/failed"

# unknowns 的类别（报告与后续人工确认台按它分组）
UNKNOWN_QUOTE_MISS = "quote-miss"
UNKNOWN_FIELD_NOT_IN_QUOTE = "field-not-in-quote"
UNKNOWN_EXPECT_NOT_IN_QUOTE = "expect-not-in-quote"
UNKNOWN_QUALIFIER_CONFLICT = "qualifier-conflict"
UNKNOWN_SAMPLE_VALUE = "sample-value-hardcoded"
UNKNOWN_ENDPOINT_MISS = "endpoint-miss"
UNKNOWN_OTHER = "other"
# ★§9.14 响应侧归属（2026-09-26 端到端真跑量出来的头号失败原因）：`body.X` 的 `X` 在文档里
#   只写在**请求侧** → 服务端不一定会回它 → 断言必然失败。它属于"**形态能过闸、但跑起来必红**"
#   那一类（R16 分叉型静默的另一种形态）→ 与其它 T28 降级同档：**摘掉 + 进 unknowns 交人工**。
UNKNOWN_RESPONSE_OWNERSHIP = "response-ownership"
# ★§9.15 期望值语义（2026-09-26，读**最终产物**才看清的一类）：断言**形态合法**（过 T28 四类）
#   但**语义上永远不成立**。两条**可证**的：
#   ① 算子只吃字符串、expect 却不是字符串（内核守卫当场报错）；
#   ② 拿**整包**（`body`/`headers`）跟一个字符串比 —— 整包是 JSON 对象/字典，比对永远不成立。
UNKNOWN_EXPECT_SEMANTICS = "expect-semantics"
# ★§9.17（装配器侧）：**模型产的** schema 与文档示例自相矛盾 → 摘掉（同一判据，守另一条通路）
UNKNOWN_DOC_DEFECT_SCHEMA = "doc-schema-conflict"
# ★**证据绑定**（WP2.2，2026-09-26）：这条断言的引用是**机器绑定**的（模型引歪了，工具按引用
#   解析到文档里真正承载该事实的那一节）。★它是 `degrade` 级但**按本仓口径算 blocker**——
#   走 `quality._blockers_from_unknowns()`：只有"合并过程说明"（`merge-note`）免拦，
#   其余 kind 一律升级为 **`unknown-critical`**。所以转正时要点名处置的是那个码：
#   `haify review --approve … --resolve-blocker unknown-critical`。
#   为什么该拦："证据是工具绑的、不是模型引的"这一点**必须被人看见并点头**。
UNKNOWN_QUOTE_BOUND = "quote-bound"

# REJECT 级类别（其余为 DEGRADE 级）
REJECT_KINDS = frozenset({UNKNOWN_QUALIFIER_CONFLICT})


def todo_findings(draft: CaseDraft) -> List[Any]:
    """把带 `${ENV(TODO_...)}` 占位的断言翻成 **PENDING 条目**（§5.2 的"登记 pending.json"）。

    ★为什么它是 PENDING 而不是错误：`TODO_` 表示"**已知未定**"——文档没给值，人工还没填。
    它**能过 L1/S/L2**（语法与结构都对），但运行期必然 `EnvNotFound`。
    所以处置是**提请人工**（进 `.ai/pending/<case>.pending.json`），不是判它错。
    """
    from interfacetester_ai.gates import PENDING, Finding  # noqa: PLC0415

    items: List[Any] = []
    for index, step in enumerate(draft.steps):
        for position, assertion in enumerate(step.validate):
            if not has_todo(assertion.expect):
                continue
            items.append(
                Finding(
                    code="TODO",
                    severity=PENDING,
                    where=f"steps[{index}].validate[{position}]",
                    message=(
                        f"{assertion.comparator}: [{assertion.check}, {assertion.expect}]"
                        " 的期望值**文档没给**，已写 TODO_ 占位"
                    ),
                    hint="填好环境变量后由装配器转入 cases/，并留痕 .ai/reviews/（红线⑥）",
                )
            )
        # ★§9.16（2026-09-27）：**请求值**里的 `TODO_` 同样要登记 —— 否则"请求参数还没填"
        #   这件事只躲在草稿文本里（`promote_draft` 会拒，但报告与 pending 清单看不见 ✗）。
        for field in ("headers", "params", "json", "data"):
            payload = getattr(step, field, None)
            if payload and has_todo(payload):
                items.append(
                    Finding(
                        code="TODO",
                        severity=PENDING,
                        where=f"steps[{index}].{field}",
                        message=(
                            f"请求的 `{field}` 里还有**文档没给的值**（`${{ENV(TODO_…)}}` 占位）"
                            "—— 用例能加载，但运行期会因 `EnvNotFound` 响亮报错"
                        ),
                        hint="填好环境变量（或让文档作者补「示例值」列）后再转正",
                    )
                )
    return items


@dataclass(frozen=True)
class Unknown:
    """一条"没能落到用例里"的东西（§5.2 的 `unknowns` 清单条目）。"""

    kind: Text
    where: Text
    message: Text
    hint: Text = ""

    @property
    def level(self) -> Text:
        return "reject" if self.kind in REJECT_KINDS else "degrade"

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "kind": self.kind,
            "level": self.level,
            "where": self.where,
            "message": self.message,
            "hint": self.hint,
        }


# ---------------------------------------------------------------------------
# ① 溯源校验（§5.1）：引用必须**逐字**出现在输入里（归一化后比对）
# ---------------------------------------------------------------------------

def quote_hits(quote: Text, source_text: Text) -> bool:
    """引用是否是 `source_text` 的子串（**两侧都归一化**后比对）。

    为什么要归一化：文档里是 CRLF、全角引号、NBSP 时，**逐字相同**的引文也会
    "看起来不命中"——那会让合规产物被误判降级。归一化管道是 T7 的
    `normalize.normalize_text`（顺序按 §5.3 钉死），这里只调用、不重写。
    """
    if not quote or not source_text:
        return False
    return normalize_text(quote) in normalize_text(source_text)


def _word_boundary_hit(needle: Text, haystack: Text) -> bool:
    """`needle` 是否以**路径段/词边界**形式出现在 `haystack` 里——**不许子串**。

    ★这条口径有血债：§0.5-③ / T21 实测过 `token` ⊂ `token_type` 导致的**静默漏报**
    （提取到的 token 从未被校验，却因为字符串包含关系被判"已引用"）。
    所以字段名的判定必须整段相等：`body.data.amount` 的**末段** `amount` 要能在引用句里
    作为独立词出现，`token` 不能因为 `token_type` 而算命中。
    """
    if not needle:
        return False
    haystack = normalize_text(haystack)
    needle = normalize_text(needle)
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        before = haystack[index - 1] if index > 0 else ""
        after = haystack[index + len(needle)] if index + len(needle) < len(haystack) else ""
        # ★`_`/`$` 是**标识符内**字符，`.` 是**路径分隔符**——后者必须算边界。
        # 实测教训：把 `.` 也算词内字符后，`userId` 在引用句 `- data.userId：字符串` 里
        # **不命中**（前面是 `.`）→ 响应说明句推出来的断言**全部被降级**。
        # 而 `_` 不能放：`token` ⊄ `token_type` 正是 T21 那笔血债要防的。
        ok_before = (not before) or not (before.isalnum() or before in "_$")
        ok_after = (not after) or not (after.isalnum() or after in "_$")
        if ok_before and ok_after:
            return True
        start = index + 1


def field_name_of(check: Text) -> Text:
    """从 `check` 取出**末段字段名**（`body.data.amount` → `amount`；`status_code` → 它自己）。

    判据用末段而不是整串，是因为引用句是**人话**（"单价 price：可选"），
    里面几乎不会出现 `body.data.amount` 这种完整路径——拿整串判会把合规产物全降级。
    而末段正是"这句话在讲哪个字段"的机器形态。
    """
    text = (check or "").strip().strip("'\"")
    for prefix in ("body.", "headers.", "cookies.", "text."):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    if text.startswith("body[") or text.startswith("headers["):
        text = text.split("[", 1)[1].rstrip("]").strip("'\"")
    segment = text.rsplit(".", 1)[-1].strip().strip("'\"")
    return segment or text


# ---------------------------------------------------------------------------
# ② T28 四类**确定性**对账（§十 T28）—— ③ 限定词 ↔ 算子（REJECT 级）
# ---------------------------------------------------------------------------

_OPTIONAL_RE = re.compile(r"可选|非必填|选填|optional", re.I)
_INTEGER_RE = re.compile(r"整数|整型|int\b", re.I)
_STRING_RE = re.compile(r"字符串|string|str\b", re.I)
_STATUS_CODE_RE = re.compile(r"\b([1-5]\d{2})\b")
_FIXED_VALUE_RE = re.compile(r"固定值|固定为|固定是|恒为|always", re.I)

# 存在性/必填类算子：文档说"可选"时，这些**不许**出现在该字段上
_EXISTENCE_COMPARATORS = frozenset({"type_match", "contains", "equal", "not_equal"})
_ABSENCE_EXPECTS = (None, "", "null", "None")


def check_optional_conflict(
    assertion: DraftAssertion, quote: Text, where: Text
) -> Optional[Unknown]:
    """文档说"可选"→ 不许写存在性/必填断言（**REJECT 级**）。

    §十 T28 的验收原句："'可选'↔必填断言 → 拒"。理由很硬：文档明说该字段可能不存在，
    而我们断言它存在——**这条用例一定会误报失败**，且失败原因看起来像"接口坏了"。
    """
    if not _OPTIONAL_RE.search(quote or ""):
        return None
    if assertion.comparator not in _EXISTENCE_COMPARATORS:
        return None
    if assertion.expect in _ABSENCE_EXPECTS:
        # `type_match: [x, "null"]` / `equal: [x, null]` 表达的是"可以没有"——不冲突
        return None
    return Unknown(
        kind=UNKNOWN_QUALIFIER_CONFLICT,
        where=where,
        message=(
            f"引用句说该字段**可选**（值可能不存在），却生成了 {assertion.comparator} 断言"
            f"（expect={assertion.expect!r}）——文档明说可选时断言必填，用例会误报失败"
        ),
        hint="要么改写成可缺失形态（如 `type_match: [x, \"null\"]`），要么向文档作者确认真实约定",
    )


def check_type_qualifier_conflict(
    assertion: DraftAssertion, quote: Text, where: Text
) -> Optional[Unknown]:
    """文档说"整数/字符串"，断言却断言成另一种类型（**REJECT 级**）。"""
    if assertion.comparator != "type_match":
        return None
    expect = assertion.expect
    if not isinstance(expect, str):
        return None
    expect_norm = expect.strip().strip("'\"").lower()
    if _INTEGER_RE.search(quote or "") and expect_norm in {"str", "string", "list", "dict"}:
        return Unknown(
            kind=UNKNOWN_QUALIFIER_CONFLICT,
            where=where,
            message=f"引用句说该字段是**整数**，却断言 `type_match: [..., {expect!r}]`",
            hint="按文档写 `int`；若你确信是字符串，说明文档与实现不符 → 走文档缺陷回流（R15）",
        )
    if _STRING_RE.search(quote or "") and expect_norm in {"int", "float", "list", "dict", "bool"}:
        return Unknown(
            kind=UNKNOWN_QUALIFIER_CONFLICT,
            where=where,
            message=f"引用句说该字段是**字符串**，却断言 `type_match: [..., {expect!r}]`",
            hint="按文档写 `str`；若你确信是数字，说明文档与实现不符 → 走文档缺陷回流（R15）",
        )
    return None


def check_status_code_conflict(
    assertion: DraftAssertion, quote: Text, where: Text
) -> Optional[Unknown]:
    """文档明写了某个状态码，断言却写另一个（**REJECT 级**）。

    只在引用句里**恰好**出现一个 3 位状态码、且断言是在判 `status_code` 时才判——
    否则 `200` 这种数字到处都是（字段长度、数量），会假报。
    """
    if field_name_of(assertion.check) != "status_code":
        return None
    codes = set(_STATUS_CODE_RE.findall(quote or ""))
    if len(codes) != 1:
        return None
    documented = int(codes.pop())
    try:
        actual = int(assertion.expect)
    except (TypeError, ValueError):
        return Unknown(
            kind=UNKNOWN_QUALIFIER_CONFLICT,
            where=where,
            message=f"引用句写明状态码 {documented}，断言里却写 {assertion.expect!r}（不是数字）",
            hint=f"按文档写 `{documented}`",
        )
    if actual != documented:
        return Unknown(
            kind=UNKNOWN_QUALIFIER_CONFLICT,
            where=where,
            message=f"引用句写明状态码 **{documented}**，断言却期望 {actual}",
            hint=f"按文档写 `{documented}`；若实际返回 {actual}，说明文档与实现不符（R15）",
        )
    return None


# ---------------------------------------------------------------------------
# ② T28 四类确定性对账 —— ① 字段名 / ② expect 字面量 / ④ 示例值（DEGRADE 级）
# ---------------------------------------------------------------------------

# 协议级检查项：它们**不是文档里的字段**，而是框架提供的请求/响应元信息。
# 例如"返回 200 表示成功"这句话讲的是**状态码语义**，里面本来就不会出现 `status_code`
# 这个词——所以 ① 字段名判定必须跳过它们，否则**合规产物会被误伤**（实测踩到过）。
# 它们的正确性由 ③ 的限定词规则（状态码冲突）与 S 系列闸门负责。
_PROTOCOL_CHECKS = frozenset(
    {"status_code", "elapsed_ms", "elapsed_s", "response_size", "reason", "text"}
)

# `type_match` 的 expect 是**类型名**，不是"期望值"：类型名的出处是结构约定
# （文档写"字符串"/"整数"就够，不必字面出现 `str`），而 ③ 的限定词规则正是管这件事的
# （"整数"↔`str` 会 REJECT）。所以 ② 的"字面量必须有出处"对类型名**不适用**——
# 但对**其它算子**照样要判（实测：不跳过就会把 `type_match: [x, "null"]` 误伤成无据）。
_TYPE_NAMES = frozenset(
    {
        "str", "string", "int", "integer", "float", "number", "bool", "boolean",
        "list", "array", "dict", "object", "null", "none",
    }
)


def _is_type_name(value: Any) -> bool:
    return isinstance(value, str) and value.strip().strip("'\"").lower() in _TYPE_NAMES


def has_actual_path(url: Text) -> bool:
    """`url` 里有没有**具体的路径段**（去掉占位符与 base 之后是否还剩 `/<非空>`）。

    - `"/"` → False（`assertions.generate_assertions_only` 在文档取不到端点时的**兜底值**）；
    - `"${ENV(BASE_URL)}"` / `"${ENV(BASE_URL)}/"` → False；
    - `"/api/status"` / `"${ENV(BASE_URL)}/api/status"` / `"https://x.y/api/a"` → True。

    ★为什么需要它（2026-09-25 实测）：不加这条时，`--assertions-only` 在"文档里没有
    端点"的情形会把兜底的 `/` 判成"编造的端点"丢掉 → 产物变成零 step → 异常从**更靠后的
    L2 出口校验**抛出、**抛穿 CLI**（退出码从"闸门拒 → 1"变成未捕获异常）。
    界线是**危险度**：`/` 不指向任何具体终点，而 `/api/status` 会打到不存在的路径上。
    """
    import re  # noqa: PLC0415

    stripped = re.sub(r"\$\{[^}]*\}", "", url or "")
    stripped = re.sub(r"\$[A-Za-z_][\w.]*", "", stripped)
    stripped = re.sub(r"^[a-zA-Z][\w+.-]*://[^/]*", "", stripped)
    return bool([segment for segment in stripped.split("/") if segment.strip()])


def endpoint_candidates(url: Text) -> List[Text]:
    """`url` 的**候选匹配串**：从最严到最松，命中一个即算"有出处"。

    刻意宽容：误伤合规产物的代价比漏放一个大得多（本仓在 `_PROTOCOL_CHECKS`
    上吃过一次"判太严误伤"的亏）。顺序：

    1. 原样；
    2. 去掉 `${ENV(...)}` / `$var` 占位符（它们本来就不是文档里的字面量）；
    3. 从（2）里取路径部分——`${ENV(BASE_URL)}/api/status` → `/api/status`；
    4. `urlsplit` 出来的 path（完整 URL 形态：`https://x.y/api/a` → `/api/a`）。
    """
    import re  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415

    raw = (url or "").strip()
    if not raw:
        return []

    out: List[Text] = [raw]
    stripped = re.sub(r"\$\{[^}]*\}", "", raw).strip()
    stripped = re.sub(r"\$[A-Za-z_][\w.]*", "", stripped).strip()
    if stripped and stripped != raw:
        out.append(stripped)

    # 从（去占位符后的）串里取 `/` 开头到尾的部分——**仅当它不是完整 URL**。
    # 完整 URL 的 path 由下面的 `urlsplit` 给出；两条都留会让候选里出现
    # `//host/path` 这种畸形串：无害，但"候选串"应当一眼能看懂。
    if "/" in stripped and "://" not in stripped:
        tail = stripped[stripped.find("/") :]
        if tail.startswith("/") and tail not in out:
            out.append(tail)

    try:
        path = (urllib.parse.urlsplit(raw).path or "").strip()
    except ValueError:
        path = ""
    if path and path not in out:
        out.append(path)

    return [item for item in out if item]


def check_endpoint_in_doc(step: DraftStep, doc_text: Text, where: Text) -> Optional[Unknown]:
    """⑤ 端点的 `method`/`url` 必须在文档里有出处（DEGRADE 级）。

    ★为什么必须有这一条（2026-09-25 真模型实测）：T28 的前四类对账**全是断言级**
    （字段名 / expect 字面量 / 限定词冲突 / 示例值），**没有一条管 step 的端点** ——
    于是一个**完全编造的接口**能一路进 `cases/`：实测里模型为「1.1 服务地址」那段
    （只有环境 URL 表、**没有任何接口**）造出了

        method: GET
        url: ${ENV(BASE_URL)}/api/status        ← `/api/status` 文档里从来没有

    而它的 `source_quote` 照样命中（引的是「服务地址」那句**真话**）——
    **"引用句真"不等于"端点真"**。这一条补的正是这个缝。

    ★为什么是 DEGRADE 而不是 REJECT：url 不在**文档原文里**，不等于它错——
    文档可能把 base 写在别处、把路径写成模板。判据只说"**这里没证据**"，
    与 ①②④ 同类，处置也同类（该 step 不进用例，进 `unknowns` 交人工）。

    ★口径说明（2026-09-25 实测纠正）：这里比的是**整篇文档**（`doc_text`），
    不是"本切片"——与 `source_quote` 的校验口径**一致**（那条也是全文档范围）。
    实测证据：`--no-skip-non-interface` 强制把文档标题片送去调模型时，模型给的是
    指向 `/api/order` 的草稿；标题片原文里没有它，但**整篇文档里有**，于是它被放行。
    （所以"跳过非接口片"的价值不是"避免失败"，而是**避免白花调用 + 产出重复用例**。）

    ★`method` 只做**弱检查**（"这个动词词在原文里出现过"）：凭一个 `POST` 词
    判不出端点对不对，但"文档里连 DELETE 都没提却生成 DELETE"能拦住。
    弱检查不假报——这是它唯一的优点，也是它刻意不做强的理由。
    """
    import re  # noqa: PLC0415

    url = (step.url or "").strip()
    if not url or not has_actual_path(url):
        # ★"没有具体路径段"的 url **不在这里判**（2026-09-25 修正）。
        #
        # 两种情形都属于"**没写**"而不是"编造"：
        # - 空 url：其实轮不到这里 —— `parse_draft`（L1）就会拒它（"url 必须是非空字符串"）；
        # - `"/"`：`assertions.generate_assertions_only` 在文档取不到端点时的**兜底值**；
        # - 纯占位符（`"${ENV(BASE_URL)}"`）：只知道 base、不知道终点。
        #
        # 它们是"已知未定"（属 `TODO_` / S3 断言真空那一类），该由**闸门**去说那句话。
        # 在这里丢掉会让产物变成"零 step"，而"零 step"会在**更靠后的 L2 出口校验**
        # 抛 `ParamsError` **抛穿 CLI** —— 退出码从"闸门拒 → 1"变成未捕获异常
        # （实测：`assertions_test::test_cli_reports_a_refusal_when_nothing_is_derivable`
        # 正是这么被打红的）。
        #
        # 本判据只管**写了、带具体路径、但文档里没有**的那一种 —— 那个才是
        # "打到不存在的路径上"的隐患。
        return None

    if any(candidate in (doc_text or "") for candidate in endpoint_candidates(url)):
        # url 有出处 → 再弱判 method（不假报）
        verb = (step.method or "").strip().upper()
        if verb and not re.search(rf"\b{re.escape(verb)}\b", doc_text or ""):
            return Unknown(
                kind=UNKNOWN_ENDPOINT_MISS,
                where=where,
                message=f"方法的动词 {verb!r} 在文档里从未出现过（url 有出处，方法没有）。",
                hint="交人工确认这个端点到底用哪个方法",
            )
        return None

    return Unknown(
        kind=UNKNOWN_ENDPOINT_MISS,
        where=where,
        message=(
            f"端点 `{step.method} {url}` 在**文档原文里找不到出处**"
            f"（试过：{endpoint_candidates(url)}）。"
            "★这类 step 会被整体丢弃——一个编造的 url 会让用例**打到不存在的路径上**，"
            "而它的失败看起来和真缺陷一模一样。"
        ),
        hint="要么让文档写出这个端点（`METHOD /path`），要么把该 step 交人工确认",
    )


def _schema_property_names(schema: Any) -> List[Text]:
    """递归取 JSON Schema 里的 `properties` 键（口径 B2 的字段名对账用）。

    ★为什么要递归：B2 的 schema 是嵌套的（`properties.data.properties.<字段>`）——
    只取顶层会漏掉真正的字段名（顶层只有 `data`）。
    """
    found: List[Text] = []
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, value in properties.items():
                found.append(str(key))
                found.extend(_schema_property_names(value))
        for key, value in schema.items():
            if key == "properties":
                continue
            if isinstance(value, (dict, list)):
                found.extend(_schema_property_names(value))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(_schema_property_names(item))
    return found


def _leaf_schema_property_names(schema: Any) -> List[Text]:
    """取 schema 里**非容器**的 `properties` 键（= 真正的字段名）。

    ★为什么要滤掉容器层：B2 的 schema 是 `properties: {data: {type: object, properties: {...}}}`——
    `data` 是**容器**（它的值是带 `properties` 的对象），不是文档里被断言的"字段"。
    把它算进覆盖矩阵会虚高（同 `status_code` 那条教训：**不是字段的东西不算覆盖字段**）。
    """
    found: List[Text] = []
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, value in properties.items():
                if isinstance(value, dict) and isinstance(value.get("properties"), dict):
                    found.extend(_leaf_schema_property_names(value))  # 容器 → 往下取
                else:
                    found.append(str(key))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(_leaf_schema_property_names(item))
    return found


def _covered_field_names(assertion: Any) -> Tuple[Text, ...]:
    """这条断言**覆盖了哪几个文档字段**（口径 B2，2026-09-26）。

    - 普通断言 → 空元组（覆盖字段由 `check` 的末段决定，见调用点）；
    - `jsonschema_match` → 从它自己的 schema **派生**（非容器 `properties` 键）。

    ★为什么不从 `DerivedAssertion.covered_fields` 取：那条信息在
    `to_draft_payload → parse_draft` 这一跳**会丢**（模型的 JSON 契约里没有这个字段，
    而且**不该**有——否则模型就能自己声明"我覆盖了哪些字段"）。从 schema 派生则两跳都成立。
    """
    if getattr(assertion, "comparator", "") == "jsonschema_match":
        return tuple(_leaf_schema_property_names(getattr(assertion, "expect", None)))
    return tuple(getattr(assertion, "covered_fields", ()) or ())


# ★**容器检查项**（`body` / `data` / `headers` / `params`…）：这些名字指"**在哪一层找**"，
#   而**不是文档里的某个字段**。① 的立法目的（§十 T28）是"**断言的那个字段**得在引用句里"——
#   对 `contains: [body, 'access_token']` 来说，要证的字段是 `access_token`（它在 `expect` 里，
#   由 ② 负责），容器名 `body` 在**人话**引用句里几乎不会出现。
#
#   实测（2026-09-25 gemma4:latest 的真实留痕，`probe_p2_prep/out_quote_loss.txt`）：
#   这类断言被 ① 摘掉时的理由是
#       "断言字段 'body'（来自 check='body'）没有出现在引用句里：'response_example'"
#   —— 读起来像"**模型编了个字段 `body`**"，而真正的问题是**引用句没说到 `access_token`**
#   （或引用句本身是自造锚点）。**把病因指错比不报还坏**：人会去改一个没错的地方。
#   → ① 对容器名**不适用**；证据责任转由 ②（`expect` 字面量必须有出处）承担，
#     所以**判据没有放松，只是搬到了该在的地方**。
_CONTAINER_CHECKS = frozenset({"body", "data", "headers", "params", "json", "cookies"})


def check_field_in_quote(
    assertion: DraftAssertion, quote: Text, where: Text
) -> Optional[Unknown]:
    """① 断言字段名必须在引用句里以**词边界**出现（**不许子串**）。

    协议级检查项（`status_code` 等）不适用——理由见 `_PROTOCOL_CHECKS`。
    **容器名**（`body` 等）也不适用——理由见 `_CONTAINER_CHECKS`。

    ★`jsonschema_match`（口径 B2，2026-09-26）**特判**：它一条覆盖整表，`check` 是 `body`
    （**不是字段名**），所以判据换成"**schema 里至少有一个字段名出现在引用句里**"：
    既挡得住"引用句其实来自别的段"（那句里不会有这些字段名），
    也不会因为"引用句只提到部分字段"而把整条降级（对模型产物更友好）。
    """
    if assertion.comparator == "jsonschema_match":
        names = _schema_property_names(assertion.expect)
        if not names:
            return None  # 空 schema 由 S1 拦（不在这里重复判）
        if any(_word_boundary_hit(name, quote) for name in names):
            return None
        return Unknown(
            kind=UNKNOWN_FIELD_NOT_IN_QUOTE,
            where=where,
            message=(
                "schema 断言里的字段名（"
                + "、".join(sorted(names)[:5])
                + "…）**都没有**出现在引用句里："
                f"{quote.strip()[:80]!r}"
            ),
            hint="要么换一条真正讲到这些字段的引用句，要么把这条断言交人工确认",
        )

    name = field_name_of(assertion.check)
    if name in _PROTOCOL_CHECKS or name in _CONTAINER_CHECKS:
        return None
    if _word_boundary_hit(name, quote):
        return None
    return Unknown(
        kind=UNKNOWN_FIELD_NOT_IN_QUOTE,
        where=where,
        message=(
            f"断言字段 {name!r}（来自 check={assertion.check!r}）没有出现在引用句里："
            f"{quote.strip()[:80]!r}"
        ),
        hint="要么换一条真正讲到该字段的引用句，要么把这条断言交人工确认",
    )


def check_expect_in_quote(
    assertion: DraftAssertion, quote: Text, where: Text
) -> Optional[Unknown]:
    """② `expect` 的**字面量**必须出现在引用句里（DEGRADE 级）。

    §十 T28 的验收原句："引用句只说'金额'而 expect 硬编码 `99.9` → 降级"。
    这就是"**期望值从哪来**"的机器形态：凭据可以是别的句子给的，但**期望值不能凭空**。

    不判的情形（刻意放宽，避免假报）：
    - `expect` 是 `None` / `bool`：`true`/`null` 这类词在文档里到处都有，判了等于噪声；
    - `expect` 是**内联 schema**（dict）：它的依据是整体结构，逐键判会误伤（结构合法性
      已由 S7/S8 闸门负责）；
    - `status_code`：状态码常写在"成功/失败"字样旁边而不是引用句里，且 ③ 已单独判冲突。
    """
    if field_name_of(assertion.check) in _PROTOCOL_CHECKS:
        return None
    if assertion.comparator == "type_match" and _is_type_name(assertion.expect):
        return None
    if has_todo(assertion.expect):
        # ★`TODO_` 是"**这里没值**"的记账，不是编造出来的期望值——② 的立法目的是
        # "禁止凭空造值"，而它**恰恰声明了"值未知"**。摘掉它等于把"待人工确认"
        # 这条信息抹掉（装配器的 `todo_findings` 已经在记 PENDING 了）。
        return None
    expect = assertion.expect

    if isinstance(expect, dict) or expect is None or isinstance(expect, bool):
        return None

    if isinstance(expect, (list, tuple)):
        # 枚举：**每个**值都要有出处——少一个就是编的（这正是要抓的形态）
        missing = [
            item
            for item in expect
            if isinstance(item, (str, int, float)) and not _literal_in_quote(item, quote)
        ]
        if not missing:
            return None
        return Unknown(
            kind=UNKNOWN_EXPECT_NOT_IN_QUOTE,
            where=where,
            message=f"枚举期望值 {missing!r} 在引用句里找不到出处（引用句：{quote.strip()[:80]!r}）",
            hint="枚举值必须逐个来自文档；编造出来的枚举会让用例在合法返回值上误报失败",
        )

    if isinstance(expect, (str, int, float)) and not _literal_in_quote(expect, quote):
        return Unknown(
            kind=UNKNOWN_EXPECT_NOT_IN_QUOTE,
            where=where,
            message=(
                f"期望值 {expect!r} 在引用句里找不到出处（引用句只说：{quote.strip()[:80]!r}）"
            ),
            hint="期望值必须能从文档读出来；读不出来就写 `${ENV(TODO_<语义名>)}` 并交人工确认",
        )
    return None


def _literal_in_quote(value: Any, quote: Text) -> bool:
    """字面量是否出现在引用句里（数字按字符串形式判；字符串按词边界判）。"""
    quote_norm = normalize_text(quote or "")
    if isinstance(value, str):
        return _word_boundary_hit(value, quote_norm)
    return str(value) in quote_norm


# --------------------------------------------------------------------------- ④ 示例值

_SAMPLE_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.S)
_SAMPLE_SECTION_RE = re.compile(r"示例|样例|example|请求体|响应体", re.I)


def _iter_sample_blocks(doc_text: Text) -> List[Text]:
    """取出文档里的**示例块**（围栏代码块，且上下文提到"示例/样例/请求体/响应体"）。"""
    blocks: List[Text] = []
    for match in _SAMPLE_FENCE_RE.finditer(doc_text or ""):
        head = (doc_text or "")[: match.start()]
        context = head[-120:]
        if _SAMPLE_SECTION_RE.search(context):
            blocks.append(match.group(1))
    return blocks


def find_sample_values(doc_text: Text) -> Dict[Text, Text]:
    """示例段里的**标量值** → 它出现的示例块（`{"99.9": "示例块前 40 字符…"}`）。

    用途只有一个：把"**把示例里的数值/字符串当成接口契约**"这种硬编码抓出来。
    §十 T28 的验收："文档明写'固定值'时用示例段数值 → 放行"，所以调用方还要过
    `has_fixed_value_marker`。
    """
    found: Dict[Text, Text] = {}
    for block in _iter_sample_blocks(doc_text):
        values: List[Any] = []
        try:
            parsed = json.loads(block)
        except ValueError:
            # 示例块不一定是合法 JSON（可能是 XML / 片段）——退化为"取引号里的字符串"
            values = [item.strip("'\"") for item in re.findall(r"[\"']([^\"']{1,40})[\"']", block)]
        else:
            values = _flatten_scalars(parsed)
        for value in values:
            key = str(value)
            if len(key) > 40:
                continue
            found.setdefault(key, block.strip().replace("\n", " ")[:40] + "…")
    return found


def _flatten_scalars(payload: Any) -> List[Any]:
    """把示例 JSON 里的**标量**摊平（只取叶子值，不取键名）。"""
    if isinstance(payload, dict):
        out: List[Any] = []
        for value in payload.values():
            out.extend(_flatten_scalars(value))
        return out
    if isinstance(payload, list):
        out = []
        for item in payload:
            out.extend(_flatten_scalars(item))
        return out
    return [payload]


def check_sample_value_hardcoded(
    assertion: DraftAssertion, doc_text: Text, sample_values: Dict[Text, Text], where: Text
) -> Optional[Unknown]:
    """④ 禁用**示例值**当期望值（除非文档标了"固定值"）——DEGRADE 级。

    判据成对（§十 T28）：文档明写"固定值"时用示例段数值 → **放行**。
    为什么默认要禁：示例段里的 `99.9` 是**举例**，不是契约；把它硬编码进断言，
    接口一改数据用例就红，而红的原因看起来像"接口坏了"——**静默的错误信号**。
    """
    expect = assertion.expect
    if isinstance(expect, (dict, list, tuple, bool)) or expect is None:
        return None

    key = str(expect)
    if key not in sample_values:
        return None

    quote = assertion.source_quote or ""
    if _FIXED_VALUE_RE.search(doc_text or "") and _literal_in_quote(expect, quote):
        # 文档明确说了"固定值/恒为"，且引用句也带着它 → 这是**契约**，不是举例
        return None

    return Unknown(
        kind=UNKNOWN_SAMPLE_VALUE,
        where=where,
        message=(
            f"期望值 {expect!r} 来自**示例段**（{sample_values[key]}），而不是契约描述；"
            "文档没有把它标成\"固定值\"，所以它只是举例"
        ),
        hint="写 `${ENV(TODO_<语义名>)}` 交人工确认，或请文档作者把该值标成\"固定值\"",
    )


# ---------------------------------------------------------------------------
# 汇总：一条断言的完整对账（溯源 + 四类）
# ---------------------------------------------------------------------------

def audit_against_quote(
    assertion: DraftAssertion,
    *,
    quote: Text,
    doc_text: Text,
    sample_values: Dict[Text, Text],
    where: Text,
) -> List[Unknown]:
    """**只按这句话**判（§5.1 溯源 + §十 T28 四类）——不做证据绑定。

    ★它是"**模型自己的引用站不站得住**"的机器形态：`probe_p2_prep/measure_quote_loss.py`
    正是拿它测"引用质量"，与 `audit_assertion()`（带绑定）对比出绑定的收益。
    """
    found: List[Unknown] = []

    # 溯源先行：引用本身不在文档里 → **直接返回**，不再跑后面四类。
    # NOTICE（为什么提前返回）：引用都假的时候，四类对账会跟着报一串"字段不在引用里"，
    # 真正的病因只有一条。级联噪声会把"一眼能定的错"淹没。
    if not quote_hits(quote, doc_text):
        found.append(
            Unknown(
                kind=UNKNOWN_QUOTE_MISS,
                where=where,
                message=f"source_quote 不是文档里的原句（逐字比对失败）：{quote.strip()[:80]!r}",
                hint="引用必须是文档**原句**（逐字）；这是「不臆造」的机器检查点",
            )
        )
        return found

    for checker in (
        check_optional_conflict,
        check_type_qualifier_conflict,
        check_status_code_conflict,
        check_field_in_quote,
        check_expect_in_quote,
    ):
        hit = checker(assertion, quote, where)
        if hit is not None:
            found.append(hit)

    sample_hit = check_sample_value_hardcoded(assertion, doc_text, sample_values, where)
    if sample_hit is not None:
        found.append(sample_hit)

    return found


def _evidence_tokens(assertion: DraftAssertion) -> List[Text]:
    """定位"这条断言的事实出处"的 token（**字段名优先**，容器名/类型名不算）。

    ★顺序有意义：`citation.fact_span()` 按 token 逐级放宽——先试"字段名 + 期望值都在同一节"，
    再退到"只有字段名在的那一节"。字段名比期望值可信（期望值可能是 `true`/`200` 这种到处都有的字面量）。
    """
    tokens: List[Text] = []
    name = field_name_of(assertion.check)
    if name and name not in _PROTOCOL_CHECKS and name not in _CONTAINER_CHECKS:
        tokens.append(name)
    expect = "" if assertion.expect is None else str(assertion.expect)
    if expect and expect not in _TYPE_NAMES and len(expect) >= 2 and expect not in tokens:
        tokens.append(expect)
    return tokens


def audit_assertion(
    assertion: DraftAssertion,
    *,
    quote: Text,
    doc_text: Text,
    sample_values: Dict[Text, Text],
    where: Text,
    scope: Optional[Tuple[int, int]] = None,
    bound: Optional[List[Any]] = None,
) -> List[Unknown]:
    """一条断言的全部对账（§5.1 溯源 + §十 T28 四类）。返回 unknowns（空 = 通过）。

    ★**证据绑定**（**WP2.2**，2026-09-26）：`bound` 非空时启用。引用选歪了
    （引标题 / 引自造锚点 / 引字段说明单元格）**不立刻降级**，而是先按 `citation.bind()`
    把它解析成**文档里的某一节**，再对账一遍：

    - 对上了 → **不降级**，并把这次绑定追加进 `bound`（由调用方写进
      `coverage.bound_assertions` 与 `unknowns` 的 `quote-bound` 条目）——
      于是"这条断言的证据是工具绑的、不是模型引的"**在人眼前是可见的**；
    - 仍对不上（含"绑到了别的接口那一段"被拒）→ 照老行为降级，理由**仍是句级原因**
      （病因不因为绑定而改变）。

    `scope` = 该用例所属**片的行号范围**：绑定窗口落在片外的**接口段**里 → 拒绝
    （片外的**共享节**，如统一响应格式，允许并标 `shared`）。
    """
    found = audit_against_quote(
        assertion,
        quote=quote,
        doc_text=doc_text,
        sample_values=sample_values,
        where=where,
    )
    if not found or bound is None:
        return found

    window = bind_quote_evidence(
        assertion.source_quote or quote,
        _evidence_tokens(assertion),
        doc_text,
        where=where,
        scope=scope,
    )
    if not window.ok():
        return found
    retry = audit_against_quote(
        assertion,
        quote=window.text,
        doc_text=doc_text,
        sample_values=sample_values,
        where=where,
    )
    if retry:
        return found
    bound.append(window)
    return []


# ---------------------------------------------------------------------------
# 覆盖矩阵（T28 ⑤⑥）：断言数 vs 覆盖字段数
# ---------------------------------------------------------------------------

# markdown 字段表的一行：`| username | string | 是 |`
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+?)\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")


_TABLE_ROW_RE = re.compile(r"^\s*\|(.+?)\|\s*$")


def check_response_ownership(
    assertion: Any, doc_text: Text, *, where: Text, tree: Optional[Sequence] = None
) -> Optional[Unknown]:
    """§9.14 **响应侧归属**：这条断言的**对象**在响应里取得到吗？

    两类拦下（都是"形态看着对、跑起来必红"）：

    1. `body.X`：`X` 在**本接口段**的响应侧没有据（响应示例 / 响应数据表 / 响应说明句都没有）——
       它只写在**请求参数表**里。服务端**不一定会回**它（实测：文档「成功响应」写着 `"data": null`），
       断言必然失败 → 摘掉；
    2. 检查项**根**不在内核支持的集合里（如 `params.page`）：内核根本不把请求参数当断言对象
       （`interfacetester/response.py` 的报错提示只认 `status_code` / `body.*` / `headers.*` / `text`）。

    ★**判不了就不判**：引用句在文档里定位不到（`response_field_names_for` 返回 `None`）→ 放行。
    "拿不准就摘"会让闸门变成噪声源（T10 的误伤率审计就是守这个）。

    ★为什么摘掉而不是"留着标一下"：与 T28 其它降级同一条道理——无据的断言留在用例里，
    它的失败**看起来和真缺陷一模一样**（R16 分叉型静默）。
    """
    check = (assertion.check or "").strip()
    if not check:
        return None
    parts = check.split(".")
    root = parts[0]
    if root not in KERNEL_CHECK_ROOTS:
        return Unknown(
            kind=UNKNOWN_RESPONSE_OWNERSHIP,
            where=where,
            message=(
                f"检查项根 `{root}` **不是内核的断言对象**（内核只认 "
                f"{'、'.join(sorted(KERNEL_CHECK_ROOTS)[:5])} 这类响应侧检查项）→ 摘掉："
                "它取不到值，留在用例里只会让一条正常用例永远失败"
            ),
            hint=(
                "请求参数该由**请求**保证（写进用例的 `params`/`json`），不是断言对象；"
                "要验证服务端行为，请断言响应侧字段"
            ),
        )
    if root != "body" or len(parts) < 2:
        return None  # `status_code`/`headers.*`/`text` 与"整包 body"不在本闸门职责内
    leaf = parts[-1]
    # ★沿用本仓既有的**容器名**口径（`_CONTAINER_CHECKS`，与 T28 ① 同一份）：`body.data`
    #   说的是"响应里有个 data 容器"，**不是**"文档声明了 data 这个字段"——不归本闸门判。
    #   实测教训（陷阱夹具）：把它也摘掉之后，`jsonschema_match: [body.data, {}]`（空 schema，
    #   本该由 S1 硬拦）就消失了，一条**该被拒**的用例变成"零断言但合法"✗。
    if leaf in _CONTAINER_CHECKS:
        return None
    # ★§9.32：判据从"**按名字**"升级为"**按路径**"（实测根因：请求字段名与响应字段**末段撞名**时，
    #   文档里只有 `data.fileName`，`body.fileName` 却因为"名字对得上"被放行 → 照文档实现的服务
    #   必让这条断言取不到值 → **必红**，而且看起来像接口坏了 ✗）。
    #   ⚠️ 这是把"宁可不判"往"敢判"的方向挪了一格，因此**保留两条宽容线**：
    #     ① 定位不到（`None`）→ 放行；② 本节链上没有响应证据 → 退回**整篇并集**（见 `_for` 实现）。
    paths = response_field_paths_for(doc_text, quote=assertion.source_quote or "", tree=tree)
    if paths is None:
        return None  # 定位不到 → 不判（宽容）
    want = tuple(parts[1:])
    if want in paths:
        return None
    # ★**"层级取错"与"根本没有"要分开报**：前者给得出正确写法（人一眼能改），后者只能交人工。
    others = sorted(path for path in paths if path and path[-1] == leaf)
    if others:
        where = "、".join(".".join(item) for item in others[:3])
        return Unknown(
            kind=UNKNOWN_RESPONSE_OWNERSHIP,
            where=where,
            message=(
                f"`{check}` 的**层级取错了**：文档里 `{leaf}` 确实在响应侧，但在 `{where}`"
                f"（不在 `{'.'.join(want)}`）→ 摘掉：照文档实现的服务会让这条断言取不到值"
            ),
            hint=(
                f"改断言对象为文档里那一层（`body.{'.'.join(others[0])}`）；"
                "若该字段确实在顶层，请让文档作者把它声明在顶层 —— "
                "**请求表字段名与响应字段末段撞名**是最常见的成因（见 §9.32）"
            ),
        )
    return Unknown(
        kind=UNKNOWN_RESPONSE_OWNERSHIP,
        where=where,
        message=(
            f"`body.{leaf}` 的 `{leaf}` **在文档的响应侧没有据**（{NOTE_RESPONSE_OWNERSHIP}）："
            "本接口段的响应示例 / 响应数据表 / 响应说明句里都没有它，"
            "它只出现在**请求**参数表里 → 服务端不一定会回它，照文档实现的服务会让这条断言失败"
        ),
        hint=(
            "三条路：① 改断言对象为**响应侧**字段（见该接口的「成功响应」/响应数据表）；"
            "② 请文档作者把**成功响应**写成可断言的数据结构（本仓 `reports/doc_defects.md` 是这条路）；"
            "③ 人工确认该字段确实会被回、且文档漏写 → 手工改产物（转正后归人工维护）"
        ),
    )


# 内核给这四个算子加了**字符串守卫**（实测报错原文："使用本守卫的算子：string_equals /
# startswith / endswith / regex_match"）——expect 不是字符串时**当场报错**，不是"断言失败"。
STRING_ONLY_COMPARATORS = frozenset({"string_equals", "startswith", "endswith", "regex_match"})
# 整包/容器：跟一个**字符串**做 `string_equals` 永远不成立（容器是 JSON 对象/字典）。
CONTAINER_CHECKS = frozenset({"body", "headers", "cookies", "params", "json"})
# 文档明说响应是**纯文本/二进制**时，"整包当字符串比"是**说得通**的 → 不摘（成对判据的另一半）。
PLAIN_TEXT_RESPONSE_RE = re.compile(
    r"二进制|octet-stream|text/plain|纯文本|文本流|binary", re.IGNORECASE
)


def _section_has_json_object(doc_text: Text, quote: Text) -> bool:
    """本节有没有**JSON 对象**形态的响应示例（决定"整包 contains"能不能成立）。"""
    from interfacetester_ai.assertions import _FENCE_RE as json_fence_re  # noqa: PLC0415

    section = section_text_for(doc_text, quote=quote)
    for match in json_fence_re.finditer(section or ""):
        try:
            payload = json.loads(match.group(1).strip())
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            return True
    return False


def _looks_like_a_json_object_member(value: Text, names: Optional[set]) -> bool:
    """`expect` 是**该容器中的一个键**吗（`contains` 对字典只认键/元素）。

    ★判不了（拿不到文档化的字段名名单）→ 返回 True（**偏放行**：宁可不判，也不误摘）。
    """
    if names is None:
        return True
    return value in names


# ---------------------------------------------------------------------------
# ★§9.17（扩展到装配器）：**模型产的** `jsonschema_match` 也要与文档示例对账
# ---------------------------------------------------------------------------
# ★为什么必须有（实测，2026-09-27）：§9.17 的判据原先是落在**确定性推导**那条路上的
#   （`derive_from_response_tables`），于是**模型**自己写的 schema 照样过关 ——
#   端到端里 `_api_version_getList` / `_api_metadata_list` 就是这么红的：文档的**响应数据表**
#   说 `data` 是对象、而**它自己的示例**写着 `data: [...]`，模型跟着表写 schema → 断言必然失败，
#   而失败看起来像"接口坏了"（分叉型静默）。
_JSON_TYPE_TO_PY: Dict[Text, Tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _example_payload(doc_text: Text, quote: Text) -> Optional[Dict[Text, Any]]:
    """本节的**响应示例**（成功或失败任一，JSON 对象）——判 schema 与示例是否矛盾用。"""
    from interfacetester_ai.assertions import _FENCE_RE as json_fence_re  # noqa: PLC0415

    section = section_text_for(doc_text, quote=quote)
    for match in json_fence_re.finditer(section or ""):
        try:
            payload = json.loads(match.group(1).strip())
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def check_schema_vs_example(assertion: Any, doc_text: Text, *, where: Text) -> Optional[Unknown]:
    """schema 与**文档自己**的响应示例矛盾 → 摘掉（与 §9.17 同一判据，守模型产的那一侧）。

    判据（只看文档，**判不了就不判**）：`jsonschema_match` 里**顶层属性**声明的 `type`
    与本段示例里同名属性的实际形状不符 → 文档自相矛盾，不该由用例背。
    """
    if (getattr(assertion, "comparator", "") or "").strip() != "jsonschema_match":
        return None
    schema = getattr(assertion, "expect", None)
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    example = _example_payload(doc_text, getattr(assertion, "source_quote", "") or "")
    if not example:
        return None
    for name, rule in properties.items():
        if not isinstance(rule, dict):
            continue
        declared = str(rule.get("type") or "").lower()
        expected = _JSON_TYPE_TO_PY.get(declared)
        if not expected or name not in example:
            continue
        value = example[name]
        if isinstance(value, bool):
            matched = bool in expected or int in expected  # 布尔与整数互认（避免误伤）
        elif isinstance(value, int) and bool in expected:
            matched = True  # ★示例写 `1`、schema 写 `boolean` → 互认（实测：不互认会误摘）
        else:
            matched = isinstance(value, expected)
        if matched:
            continue
        return Unknown(
            kind=UNKNOWN_DOC_DEFECT_SCHEMA,
            where=where,
            message=(
                f"`jsonschema_match` 说 `{name}` 是 `{rule.get('type')}`，而**本节文档自己的"
                f"响应示例**里它是 `{type(value).__name__}` → schema 与文档示例**自相矛盾**"
                "（照示例实现的服务必然让这条断言失败）"
            ),
            hint=(
                "这不是用例的问题，是**文档自相矛盾**（要么改示例、要么改字段表）——"
                "按「文档缺陷」处置：确定性通路会把同一处记进 `reports/doc_defects.md`（§9.17）"
            ),
        )
    return None


def check_expect_semantics(
    assertion: Any, doc_text: Text, *, where: Text, tree: Optional[Sequence] = None
) -> Optional[Unknown]:
    """§9.15 **期望值语义**：这条断言**讲不讲得通**？（T28 四类只管"有没有出处"）

    两条**可证**的形态（其余一律不判——"判不了就不判"是本仓纪律）：

    1. **算子吃字符串、expect 不是字符串**：`string_equals: [body.success, true]` ——
       内核的字符串守卫**当场报错**（跑起来必红，且红得不是"断言失败"而是"用法错"）；
    2. **拿整包跟字符串比**：`string_equals: [body, 'true']`、`string_equals: [headers, 'a; b']`
       —— 整包是 JSON 对象/字典，比对永远不成立。★**例外**：文档明说响应是**纯文本/二进制**
       （`Body: 文件二进制流` / `application/octet-stream`）时，整包当字符串比是**说得通**的 → 放行。

    ★刻意**不**覆盖的（不是漏了，是判不了）：`contains: [body, 'FILE_1018']`（对整包做子串包含，
    可能真的成立）、`equal: [body.success, false]`（负例步骤里完全正当）、schema 形状与示例冲突
    （那属于"文档缺陷回流"）。这些一律留给人工——**误摘比漏摘更坏**（T10 误伤率审计守这个）。
    ★**2026-09-27 更正**（`contains` 那一条）：本文件早先写着"`contains: [body, 'FILE_1018']`
    属**刻意不判**（理由是"对整包做子串包含可能真的成立"）——**这个前提是错的** ✗：
    内核 `comparators.contains()` 的实现就是 `assert expect_value in check_value`，
    对**字典**而言 `in` 测的是**键**（不是序列化文本的子串）→ 文档里的**取值**（如错误码）
    **永远不可能**被 `contains` 命中。实测证据：mock 按文档规则返回了
    `{"success": false, "errorCode": "FILE_1018", ...}`，而 `contains: [body, 'FILE_1018']` 仍然红 ✗。
    ⇒ 现在归到第 ③ 类一起判（见下），并把"正确写法"写进提示：`equal: [body.errorCode, "FILE_1018"]`。
    """
    comparator = (getattr(assertion, "comparator", "") or "").strip()
    check = (getattr(assertion, "check", "") or "").strip()
    expect = getattr(assertion, "expect", None)
    quote = getattr(assertion, "source_quote", "") or ""

    # ★③-b（2026-09-27 新增）：`contains` 拿**取值**去查**字典** → 永不命中（见 docstring 的更正）
    if comparator in ("contains", "not_contains") and check in CONTAINER_CHECKS:
        if isinstance(expect, str) and not _looks_like_a_json_object_member(
            expect, response_field_names_for(doc_text, quote=quote, tree=tree)
        ):
            if _section_has_json_object(doc_text, quote):
                return Unknown(
                    kind=UNKNOWN_EXPECT_SEMANTICS,
                    where=where,
                    message=(
                        f"`{comparator}` 对 `{check}`（**JSON 对象**）找的是**键**"
                        f"（内核实现就是 `expect in check_value`），而 `{expect[:24]!r}` "
                        "在文档里是**取值**而不是键 → 永远不可能命中"
                    ),
                    hint=(
                        "要断取值请写字段路径 + 正确算子："
                        f"`equal: [{check}.<字段>, {expect[:16]!r}]`（如 `body.errorCode`）；"
                        "要断「整包里有没有这个键」才用 `contains`"
                    ),
                )

    if comparator in STRING_ONLY_COMPARATORS and not isinstance(expect, str):
        return Unknown(
            kind=UNKNOWN_EXPECT_SEMANTICS,
            where=where,
            message=(
                f"`{comparator}` 只比较**字符串**，而这里的期望值是 {expect!r}（{type(expect).__name__}）"
                " → 内核的字符串守卫会**当场报错**，跑起来必红（而且红得不是\"断言失败\"，是\"用法错\"）"
            ),
            hint=(
                "要么把期望值改成字符串（`\"true\"` 不是 `true`），要么换算子"
                "（布尔/数字用 `equal`，类型用 `type_match`）"
            ),
        )

    if (
        comparator == "contains"
        and check in CONTAINER_CHECKS
        and isinstance(expect, str)
        and _looks_like_a_json_fragment(expect)
    ):
        # ★实测（2026-09-26）：`contains: [body, '"success": true']` 跑红 —— 内核的 `contains`
        #   对**字典**找的是**键/元素**，不是序列化文本的子串，所以"从示例 JSON 里剪下来的片段"
        #   永远不可能命中。判据只认**带 JSON 结构标记**的 expect（`":` / 花括号 / 方括号开头），
        #   像 `FILE_1018` 这种正常取值**照样放行**（它真可能出现在响应里）。
        return Unknown(
            kind=UNKNOWN_EXPECT_SEMANTICS,
            where=where,
            message=(
                f"`contains` 对 `{check}`（整包）找的是**键/元素**，而期望值 `{expect[:24]!r}` "
                "看起来是**从示例 JSON 里剪下来的片段** → 永远不可能命中"
            ),
            hint=(
                "要验字段取字段路径（`body.message`）；要验整包里的**取值**请把片段换成"
                "真正的键名或用 `jsonschema_match`"
            ),
        )

    if (
        comparator == "string_equals"
        and check in CONTAINER_CHECKS
        and isinstance(expect, str)
        and not _documents_a_plain_text_body(
            doc_text, check, getattr(assertion, "source_quote", "") or ""
        )
    ):
        return Unknown(
            kind=UNKNOWN_EXPECT_SEMANTICS,
            where=where,
            message=(
                f"`{check}` 是**整包**（JSON 对象/字典），拿它跟字符串 `{expect[:24]!r}` 比"
                "**永远不成立** —— 要么是拿**片段**当整包（从示例 JSON 里剪下来的），"
                "要么断言对象写错了（该写 `body.<字段>`）"
            ),
            hint=(
                "要验字段就写字段路径（`body.message`）；要验整包形态用 `type_match: [body, dict]`"
                "或 `jsonschema_match`；文档若写的是**纯文本/二进制**响应，本判据会放行"
            ),
        )
    return None


def _looks_like_a_json_fragment(value: Text) -> bool:
    """期望值看起来是"从示例 JSON 里剪下来的片段"吗（`success": true` / `{"a": 1}` / `["x"]`）。"""
    text = value.strip()
    if not text:
        return False
    return '":' in text or text[:1] in "{["


def _documents_a_plain_text_body(doc_text: Text, check: Text, quote: Text = "") -> bool:
    """**本节**是否明说响应是**纯文本/二进制**（决定"整包当字符串比"说得通不说得通）。

    ★必须是"本节"（实测踩到）：真实文档里只要有**一个**接口是二进制下载（`Body: 文件二进制流`），
    按整篇判就会把闸门在**所有**节都放行 ✗。
    """
    if check != "body":
        return False  # `headers`/`cookies` 这种整包永远是 key-value，没有"纯文本"一说
    section = section_text_for(doc_text, quote=quote)
    return bool(PLAIN_TEXT_RESPONSE_RE.search(section))


def documented_fields(doc_text: Text) -> Tuple[Text, ...]:
    """从文档的**字段表**提取字段名（表头行/markdown 分隔行不算）。
    为什么不扫正文里的裸词：那会把"成功""示例"之类全当成字段名，覆盖矩阵立刻变成噪声
    （而噪声版的矩阵没人看，等于没有矩阵）。字段表的**第一列**才是"文档声明的字段"。

    ★**2026-09-26 修正**（口径 B2 让分母可见之后暴露的既有缺陷）：原来只按一个**词表**
    （`字段`/`参数`/`名称`/`field`/`name`）排头行，于是表头写成 **`字段名`** 的文档会把
    `字段名` **当成一个字段**算进分母（实测：分母 4 → 5，覆盖率被压低）。
    现在改成**按表块跳过真正的表头行**（与 `read_fields_rows` 同一套块判据），词表只作二道保险。
    """
    lines = (doc_text or "").splitlines()
    fields: List[Text] = []
    position = 0
    while position < len(lines):
        if not _TABLE_ROW_RE.match(lines[position]):
            position += 1
            continue
        block: List[Text] = []
        while position < len(lines) and _TABLE_ROW_RE.match(lines[position]):
            block.append(lines[position])
            position += 1
        if len(block) < 2 or not _TABLE_SEPARATOR_RE.match(block[1]):
            continue  # 不是 markdown 表格（缺分隔行）
        for line in block[2:]:  # 跳过表头与分隔行
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not cells:
                continue
            name = cells[0].strip("`* ")
            if not name or name in {"字段", "参数", "名称", "field", "name", "字段名", "参数名"}:
                continue
            if name not in fields:
                fields.append(name)
    return tuple(fields)


@dataclass(frozen=True)
class Coverage:
    """覆盖矩阵（T28 ⑤⑥）：**断言数 vs 覆盖字段数**并列。

    为什么必须并列展示：只有"断言数"时，"一屏 `status_code, 200`"看起来也像"断言很足"
    （数量漂亮、覆盖面为零）；把"覆盖字段数"摆在旁边，这种东西一眼就露。
    """

    assertion_count: int = 0
    covered_fields: Tuple[Text, ...] = ()
    uncovered_fields: Tuple[Text, ...] = ()
    dropped_assertions: Tuple[Text, ...] = ()
    # ★协议级检查项（`status_code` 等）产出的断言：**不算覆盖文档字段**，但必须如实单列。
    #   为什么（2026-09-25 真模型实测）：真模型给「2.1 创建目录」产的两条用例里，唯一的
    #   断言就是 `status_code 200` —— 而**文档里根本没有 `200`**（它是靠 `_PROTOCOL_CHECKS`
    #   豁免进来的），报告却显示"覆盖字段 1 个（文档声明 46 个）"，读起来像"覆盖了一点"，
    #   **实际是覆盖了 0 个文档事实**（"绿了但什么都没测"的另一种形态）。
    #   只报"覆盖 0"会被读成"什么都没做"，报"覆盖 1"则是**虚高** —— 两个都失真，
    #   所以并列展示。
    protocol_assertions: Tuple[Text, ...] = ()
    # ★分母的唯一来源：**文档声明的字段**（`documented_fields(doc_text)`）。
    #   为什么不拿 `covered + uncovered` 反推（原实现如此）：那两者**不同源** ——
    #   `uncovered` 来自文档字段表，`covered` 来自断言；只要 `covered` 里混进一个
    #   **不是文档字段**的名字（协议级检查项就是），分母就虚高。实测那份文档声明 45 个
    #   字段，报告却印"46 个"（虚高的 1 正好是 `status_code`）。**分母要量，不要算。**
    documented_fields: Tuple[Text, ...] = ()
    # ★**降级/拒绝的按原因分类**（(c)④-②，2026-09-26）：让"引用质量"**可观测**——
    #   实测真模型在**真实文档**上原始 75 条断言有 **30 条（40%）** 被 T28 拦下，而在 15 份
    #   golden 上是 0 降级；没有这个数字，那类退化**只能靠人去 grep 原始草案**才发现。
    #   口径：`(kind, 命中项数)`，按项数降序；★计的是**命中项**（一条断言可能命中多条对账），
    #   而"降级条数"看 `dropped_assertions`（**按断言**记）——两者分开报，别混。
    dropped_kinds: Tuple[Tuple[Text, int], ...] = ()
    # ★**证据绑定**（WP2.2，2026-09-26）：引用是**机器绑定**的断言（模型引歪了，工具按引用
    #   解析到文档里承载该事实的那一节）。★它**不是**"降级"——断言活着、四类对账一条没少，
    #   只是**证据来源**从"模型引的"变成"工具绑的"，所以必须**与人并列展示**（评审 §五的口径）。
    bound_assertions: Tuple[Text, ...] = ()

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "assertion_count": self.assertion_count,
            "covered_field_count": len(self.covered_fields),
            "covered_fields": list(self.covered_fields),
            "documented_field_count": len(self.documented_fields),
            "uncovered_field_count": len(self.uncovered_fields),
            "uncovered_fields": list(self.uncovered_fields),
            "dropped_assertion_count": len(self.dropped_assertions),
            "dropped_assertions": list(self.dropped_assertions),
            "dropped_kinds": [list(item) for item in self.dropped_kinds],
            "drop_rate": self.drop_rate(),
            "bound_assertion_count": len(self.bound_assertions),
            "bound_assertions": list(self.bound_assertions),
            "protocol_assertion_count": len(self.protocol_assertions),
            "protocol_assertions": list(self.protocol_assertions),
        }

    def drop_rate(self) -> Optional[float]:
        """T28 降级率 = 被拦下的断言 / **提议的断言总数**。

        ★分母取 `assertion_count`——它的语义就是"**模型（或映射）提议的条数，含后面被降级的**"
        （`draft_to_ircase` 有一条不变量在守：`assertion_count - len(dropped) == 进用例条数`，
        `run_selftest` 也钉着它）。**不要**写成 `assertion_count + len(dropped)`：那是**双重计数**
        （实测：1 条断言被丢时算出 50% 而不是 100%）。
        ★**分母为 0 → `None`**（不把"没有断言可评"算成 0%，同 `RuntimeEvidence.mutation_rate`）。
        """
        if self.assertion_count <= 0:
            return None
        return len(self.dropped_assertions) / self.assertion_count

    def dropped_summary(self, limit: int = 3) -> Text:
        """按原因的分类摘要（`quote-miss×3、field-not-in-quote×5`）——★只给前 `limit` 类。"""
        if not self.dropped_kinds:
            return ""
        shown = "、".join(f"{kind}×{count}" for kind, count in self.dropped_kinds[:limit])
        if len(self.dropped_kinds) > limit:
            shown += "…"
        return shown

    def render(self) -> Text:
        """给报告/CLI 的一行话（§十 T28 ⑥ 的口径 + (c)④-② 的降级率与原因）。"""
        line = (
            f"断言 {self.assertion_count} 条 / 覆盖字段 {len(self.covered_fields)} 个"
            f"（文档声明字段 {len(self.documented_fields)} 个）"
            f"；本次降级/拒掉 {len(self.dropped_assertions)} 条"
        )
        rate = self.drop_rate()
        if rate is not None and self.dropped_assertions:
            line += f"（占**提议的** {self.assertion_count} 条断言的 {rate:.0%}）"
        summary = self.dropped_summary()
        if summary:
            line += f"　原因：{summary}"
        if self.bound_assertions:
            line += (
                f"；其中 {len(self.bound_assertions)} 条**证据是工具绑定的**"
                "（模型引用引歪了，工具按引用解析到文档里承载该事实的那一节，"
                "四类对账一条没少 —— 见 `unknowns` 的 `quote-bound`）"
            )
        if self.protocol_assertions:
            line += (
                f"；另有**协议级断言** {len(self.protocol_assertions)} 条"
                f"（{', '.join(self.protocol_assertions)} —— 不计入文档字段覆盖）"
            )
        return line


# --------------------------------------------------------------------------- ⑥ type_match 的类型名
#
# ★为什么必须在这里归一（实测，2026-09-26）：`type_match` 的期望值**必须是内置类型名**，
#   否则 **S7 会 REJECT 整条用例**（不是只摘那一条断言）。而模型照抄文档"类型"列时
#   常写成人话词 —— 真实客户文档的字段表就写 `number`，中文文档会写 `字符串`。
#   实测（`gemma4:latest` 那一轮）：18 片里**有 1 片的失败正是它**。
#
#   处置分两类（本仓口径：**能确定的确定下来，不能确定的响亮点名**）：
#     - **无歧义别名** → 归一成内置名（`string`/`字符串`→`str`、`整数`→`int`、`对象`→`dict`…）；
#     - **有歧义**（`number` 到底是 `int` 还是 `float`）→ **不许猜**，摘掉该条断言并写进
#       `unknowns`（`type-name-unusable`）交人工，**但用例照常产出**
#       —— 一条断言的**名字**不该把整条用例打死（覆盖率就是这么丢的）。
#
# ★合法名单的**单一来源是闸门**（`gates._TYPE_NAMES`）：抄一份到这里迟早与 S7 漂移，
#   而漂移的表现是"assembler 说合法、闸门说非法"这种**自相矛盾**的产物。
_TYPE_ALIASES: Dict[Text, Text] = {
    "string": "str",
    "文本": "str",
    "字符串": "str",
    "integer": "int",
    "整数": "int",
    "boolean": "bool",
    "布尔": "bool",
    "布尔值": "bool",
    "object": "dict",
    "对象": "dict",
    "array": "list",
    "数组": "list",
    "null": "None",
    "空": "None",
    "double": "float",
    "小数": "float",
    "浮点数": "float",
}
# 有歧义（真实文档里最常见）：`number` 既可能 `int` 也可能 `float` → **不猜**
_TYPE_AMBIGUOUS = frozenset({"number", "数字", "数值", "num"})
UNKNOWN_TYPE_NAME = "type-name-unusable"


def normalize_type_match(
    assertion: DraftAssertion,
) -> Tuple[DraftAssertion, Optional[Unknown]]:
    """把 `type_match` 的期望值归一到**闸门认的类型名**（或明确交人工）。

    返回 `(断言, 交人工说明)`：说明非空 = 这条断言**不许进用例**（但用例本身照常产出）。
    """
    from interfacetester_ai.gates import _TYPE_NAMES as LEGAL  # noqa: PLC0415 - 单一来源

    if assertion.comparator != "type_match" or not isinstance(assertion.expect, str):
        return assertion, None
    raw = assertion.expect.strip()
    if raw in LEGAL or raw in ("None", "NoneType"):
        return assertion, None
    lowered = raw.lower()
    if lowered in LEGAL:
        return replace(assertion, expect=lowered), None
    if lowered in _TYPE_AMBIGUOUS:
        return assertion, Unknown(
            kind=UNKNOWN_TYPE_NAME,
            where="assertion",
            message=(
                f"`type_match` 的期望值 {raw!r} 是**文档里的类型说法**，不是合法类型名"
                f"（闸门只认 {'/'.join(sorted(LEGAL))}），而它**到底对应哪个类型是歧义的**"
                "（`number` 可能是 `int`，也可能是 `float`）—— 本仓**不猜**，这条断言交人工。"
            ),
            hint=(
                "人工二选一：把 `expect` 改成具体类型（`int` / `float`），"
                "或请文档作者把类型列写成确定的名字。★注意：**用例的其他断言不受影响**，"
                "本条只是被摘下来记账。"
            ),
        )
    if lowered in _TYPE_ALIASES:
        return replace(assertion, expect=_TYPE_ALIASES[lowered]), None
    return assertion, Unknown(
        kind=UNKNOWN_TYPE_NAME,
        where="assertion",
        message=(
            f"`type_match` 的期望值 {raw!r} 既不是合法类型名，也不在已知别名表里"
            f"（闸门只认 {'/'.join(sorted(LEGAL))}）—— **用例照常产出**，这条断言交人工。"
        ),
        hint="把 `expect` 改成具体类型名，或确认这是不是模型编出来的用法。",
    )


# ---------------------------------------------------------------------------
# 装配：CaseDraft → IRCase/IRStep（纯代码；**模型到此为止**）
# ---------------------------------------------------------------------------

@dataclass
class AssemblyOutcome:
    """一次装配的**内存**结果（落盘由 `assemble()` 的两条路径负责）。"""

    case_name: Text
    ir_case: Any  # IRCase（延迟 import，避免包在"内核没装"时不可导入）
    unknowns: List[Unknown] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    rejected: List[Text] = field(default_factory=list)

    def has_rejects(self) -> bool:
        return bool(self.rejected)


def draft_to_ircase(
    draft: CaseDraft,
    *,
    doc_text: Text,
    base_url: Text = "",
    extra_unknowns: Sequence[Unknown] = (),
    scope: Optional[Tuple[int, int]] = None,
) -> AssemblyOutcome:
    """把 `CaseDraft` 装成 `IRCase`（**纯代码**）。

    - 通过对账的断言 → 进 `IRStep.assertions`（形态 `{comparator: [check, expect]}`，与 YAML 一致）；
    - 被降级/拒的断言 → **不进用例**，只进 `unknowns`（并计入覆盖矩阵的"被丢掉"）。

    NOTICE（为什么"丢掉"而不是"留着但标一下"）：一条无据/冲突的断言留在用例里，
    它的失败**看起来和真缺陷一样**——那正是 R16「分叉型静默」要防的东西。
    `unknowns` 是"已知未定"的容器（§5.2），它的消费者是人，不是 pytest。

    `scope` = **本用例所属片的行号范围**（WP2.2 证据绑定的边界）：绑定窗口落在片外的
    **接口段**里 → 拒绝；片外的**共享节**（统一响应格式 / 公共请求头 / 错误码表）允许并标 `shared`。
    """
    from interfacetester.converters.ir import IRCase, IRStep  # noqa: PLC0415

    sample_values = find_sample_values(doc_text)
    # ★§9.14：节表（每节的**响应侧字段名**）算一次、逐条断言复用；定位不到时闸门放行。
    section_table = section_tree(doc_text)
    # ★`extra_unknowns`（C-2 用）：调用方（`gen`）把**合并**的冲突/跳过记进来——
    # 让"我们做了合并、以及哪几条没并进来"留在同一个 `unknowns` 容器里（单一出口，
    # 消费者是人不是 pytest）。
    unknowns: List[Unknown] = list(extra_unknowns)
    dropped: List[Text] = []
    rejected: List[Text] = []
    covered: List[Text] = []
    protocol: List[Text] = []
    # ★WP2.2：**证据绑定**记账（只记**活下来**的那些断言——绑了但仍被别的原因拦下的不记，
    #   否则会出现"产物里没有这条断言，报告却说它绑过"的悬空记录）。
    bound_kept: List[Any] = []
    assertion_count = 0
    steps: List[Any] = []

    for step_index, step in enumerate(draft.steps):
        where_step = f"steps[{step_index}]"
        # ★⑤ 端点溯源（2026-09-25 新增）：url/method 在原文里找不到 → **该 step 整体丢弃**。
        #   为什么丢弃而不是"留着标一下"：一个编造的 url 会让用例**打到不存在的路径上**，
        #   而它的失败看起来和真缺陷一模一样（同 R16「分叉型静默」的病灶）。
        endpoint_unknown = check_endpoint_in_doc(step, doc_text, where_step)
        if endpoint_unknown:
            unknowns.append(endpoint_unknown)
            dropped.append(f"{where_step} {step.method} {step.url}")
            continue

        kept: List[Dict[Text, Any]] = []
        for a_index, assertion in enumerate(step.validate):
            where = f"steps[{step_index}].validate[{a_index}]"
            # ★这里数的是**模型提议的**条数（含后面被降级的），配合报告里的"降级/拒掉 N 条"，
            #   读者可推出进用例的条数。这是个**有据的不变量**（`run_selftest` 在守它：
            #   `断言数 - 被丢数 == 进用例条数`）——别为了"报告好看"把它挪到降级判定之后，
            #   那样"降级 N 条"就变成悬空数字了（我 2026-09-25 试过，被自检当场拦下）。
            assertion_count += 1
            label = (
                f"{where} {assertion.comparator}:"
                f" [{assertion.check}, {assertion.expect!r}]"
            )

            # ★⑥ 类型名归一/交人工（`normalize_type_match`）：`type_match` 的期望值必须过 S7，
            #   否则**整条用例**被闸门 REJECT（不是只摘那一条断言）——真实文档写 `number` 时
            #   模型照抄就会踩。能确定的归一（`string`→`str`），不能确定的摘下来交人工。
            assertion, type_problem = normalize_type_match(assertion)
            if type_problem is not None:
                unknowns.append(type_problem)
                dropped.append(label)
                continue

            # ★WP2.2：`bound` 非空 → 启用证据绑定；逐条收集，**只有活下来的才记账**
            local_binding: List[Any] = []
            found = audit_assertion(
                assertion,
                quote=assertion.source_quote,
                doc_text=doc_text,
                sample_values=sample_values,
                where=where,
                scope=scope,
                bound=local_binding,
            )
            if found:
                unknowns.extend(found)
                dropped.append(label)
                if any(item.level == "reject" for item in found):
                    rejected.append(label)
                continue
            if local_binding:
                bound_kept.extend(local_binding)

            # ★§9.14 响应侧归属（2026-09-26 端到端真跑量出来的头号失败原因）：`body.X` 的 `X`
            #   只在请求侧有据 → 服务端不一定会回它 → 摘掉（形态能过闸但跑起来必红，
            #   与 R16「分叉型静默」同病灶）。
            #   ★**顺序有意放在 T28 之后**：REJECT 级判据必须先出结论——实测教训是陷阱夹具的
            #   `jsonschema_match: [body.data, {}]`（空 schema，归 S1 硬拦）若先被归属闸门摘掉，
            #   S1 就再也看不到它 → 一条**本该被拒**的用例变成"零断言但合法"✗。
            ownership = check_response_ownership(
                assertion, doc_text, where=where, tree=section_table
            )
            if ownership is not None:
                unknowns.append(ownership)
                dropped.append(label)
                continue

            # ★§9.15 期望值语义：形态合法但**讲不通**的（`string_equals` 配布尔、整包跟字符串比）。
            #   与上面两条同一档（DEGRADE：摘掉 + 进 unknowns 交人工）。
            semantics = check_expect_semantics(assertion, doc_text, where=where)
            if semantics is not None:
                unknowns.append(semantics)
                dropped.append(label)
                continue

            # ★§9.17（装配器侧）：**模型产的** schema 与文档示例自相矛盾 → 摘掉。
            #   与确定性通路的 `_doc_defect_for_table` 是同一个判断意图（文档的锅，不该用例背）。
            conflict = check_schema_vs_example(assertion, doc_text, where=where)
            if conflict is not None:
                unknowns.append(conflict)
                dropped.append(label)
                continue

            kept.append({assertion.comparator: [assertion.check, assertion.expect]})
            name = field_name_of(assertion.check)
            # ★协议级检查项**不算覆盖文档字段**（2026-09-25 修正）：`_PROTOCOL_CHECKS` 的
            #   定义自己就写着"它们**不是文档里的字段**"——把它算进 `covered_fields` 既
            #   自相矛盾又虚高（实测：两条真模型产物的唯一断言是 `status_code 200`，
            #   而文档里没有 `200`，报告却说"覆盖字段 1 个"）。单列进 `protocol`。
            extras = _covered_field_names(assertion)
            if extras:
                # ★口径 B2：`jsonschema_match` **一条覆盖整表**，覆盖字段从它的 schema 派生。
                #   以它为准，**不**再把 `check` 的末段算成一个"字段"——`check=body` 时
                #   那会往覆盖矩阵里塞一个叫 `body` 的假字段（同 `status_code` 那条教训）。
                for extra in extras:
                    if extra and extra not in covered:
                        covered.append(extra)
            elif assertion.comparator == "jsonschema_match":
                pass  # schema 断言本身不贡献字段名（`check` 是 `body`，不是字段）
            elif name in _PROTOCOL_CHECKS:
                if name not in protocol:
                    protocol.append(name)
            elif name and name not in covered:
                covered.append(name)

        steps.append(
            IRStep(
                name=step.name,
                method=step.method,
                url=step.url,
                params=dict(step.params or {}),
                headers=dict(step.headers or {}),
                json_body=step.json,
                data=step.data,
                upload=dict(step.upload or {}),
                assertions=kept,
                extracts=dict(step.extract or {}),
                source=draft.case_name,
            )
        )

    doc_fields = documented_fields(doc_text)
    # ★WP2.2（证据绑定）：把绑定**摊到人眼前**——两条出口（`unknowns` 逐条 + 覆盖矩阵里的计数）。
    #   为什么必须是 unknown 而不是只写进覆盖矩阵：`unknowns.json` 是**交付物**，
    #   而"这条断言的证据是工具绑的"正是人工转正时最需要知道的事；
    #   它还**默认算 blocker**（见 `UNKNOWN_QUOTE_BOUND` 的注释）→ 转正需逐条点名处置。
    for binding in bound_kept:
        unknowns.append(
            Unknown(
                kind=UNKNOWN_QUOTE_BOUND,
                where=getattr(binding, "where", "") or "case",
                message=(
                    f"这条断言的引用是**机器绑定**的：{binding.render()}。"
                    "模型原来的引用句站不住（引了标题 / 自造锚点 / 只引了字段说明），"
                    "工具按引用把它绑到文档里真正承载该事实的那一节，四类对账**一条都没少**。"
                ),
                hint=(
                    "人工确认两件事：① 那一节确实是**本接口**的契约（不是别的接口的）；"
                    "② 断言本身是你要测的。确认后 `--resolve-blocker unknown-critical` 点名处置"
                    "（`quote-bound` 在本仓口径里升级成那个稳定码，见 `quality.py`）。"
                ),
            )
        )
    # ★"断言全是协议级"这条事实要**跟着产物走**（2026-09-25 实测新增）：控制台的覆盖率
    #   那行已经说了，但**只读交付物**（`cases/*.yml` + `unknowns.json`）的人看不到 ——
    #   而那两个文件才是交付物。实测真模型产的两条用例正是这种形态：唯一的断言是
    #   `status_code 200`（**文档里根本没有 `200`**，它是靠 `_PROTOCOL_CHECKS` 豁免进来的）
    #   —— "绿了但什么都没测"。
    #   它是 DEGRADE（`level` 由 `kind` 派生，新 kind 不在 `REJECT_KINDS` 里）→ **不挡落盘**：
    #   这类用例能挡 4xx/5xx，**有用，只是薄**，所以是"交人工"而不是"拒"。
    if protocol and not covered:
        unknowns.append(
            Unknown(
                kind="protocol-only",
                where="case",
                message=(
                    "这份用例的断言**全部是协议级检查项**"
                    f"（{', '.join(protocol)}），没有一条验证文档里写的事实"
                    f"（文档声明字段 {len(doc_fields)} 个）"
                ),
                hint=(
                    "协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，"
                    "或请文档作者把成功响应写成可断言的事实"
                ),
            )
        )
    # ★(c)④-②：把"被 T28/溯源拦下的项"**按原因**统计出来（口径见 `Coverage.dropped_kinds`）。
    #   从 `unknowns` 派生（而不是在每个 `dropped.append` 处记账）：一处口径、也不会漏掉
    #   `endpoint-miss` 这类 **step 级**的拦下项。
    _accounting_kinds = (
        UNKNOWN_QUOTE_MISS,
        UNKNOWN_FIELD_NOT_IN_QUOTE,
        UNKNOWN_EXPECT_NOT_IN_QUOTE,
        UNKNOWN_QUALIFIER_CONFLICT,
        UNKNOWN_SAMPLE_VALUE,
        UNKNOWN_ENDPOINT_MISS,
        # ★类型名不可用（`number` 这类歧义说法）也是"被摘掉的理由"，必须计入分类，
        #   否则报告会说"降级 1 条"却不给原因（§9.11）。
        UNKNOWN_TYPE_NAME,
        # ★响应侧归属（§9.14）：同上——它是本轮真跑量出来的**最大一类**降级，必须可见。
        UNKNOWN_RESPONSE_OWNERSHIP,
        # ★期望值语义（§9.15）：同样必须可见（否则报告说"降级 3 条"却不说为什么）。
        UNKNOWN_EXPECT_SEMANTICS,
        # ★schema 与文档示例冲突（§9.17 装配器侧）：必须可见（它指向"文档自相矛盾"）。
        UNKNOWN_DOC_DEFECT_SCHEMA,
    )
    dropped_kinds = tuple(
        sorted(
            (
                (kind, sum(1 for item in unknowns if item.kind == kind))
                for kind in _accounting_kinds
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
    )
    dropped_kinds = tuple(pair for pair in dropped_kinds if pair[1])

    coverage = Coverage(
        assertion_count=assertion_count,
        covered_fields=tuple(covered),
        uncovered_fields=tuple(item for item in doc_fields if item not in covered),
        dropped_assertions=tuple(dropped),
        dropped_kinds=dropped_kinds,
        bound_assertions=tuple(item.render() for item in bound_kept),
        protocol_assertions=tuple(protocol),
        documented_fields=tuple(doc_fields),
    )
    return AssemblyOutcome(
        case_name=draft.case_name,
        ir_case=IRCase(name=draft.case_name, base_url=base_url, steps=steps, source="haify gen"),
        unknowns=unknowns,
        coverage=coverage,
        rejected=rejected,
    )


# ---------------------------------------------------------------------------
# 落盘（登记根：`cases/` 与 `.ai/`）
# ---------------------------------------------------------------------------
#
# ★策略：**一律先在 `.ai/draft/` 里落地并过闸门**；`cases/` 只由 `promote_draft()` 写。
#
# 为什么不是"直接写 cases/，被拒再删"：那样 `cases/` 里会**短暂出现**带陷阱的产物，
# 而且"删干净"这件事本身没法用"文件不存在"来证明（删除失败 / 半删都看不见）。
# 现在的形态让判据变成一句可断言的话：**被拒的用例在 `cases/` 里不存在**（且从不曾存在）。
#
# ★2026-09-26（§九 第一档②）：这条策略从"过闸门就复制进 `cases/`"收紧为
# **"只有人工转正才进"**。依据是实证（§九 9.4）：一份只断言 `status_code` 的薄用例
# 在 `.ai/reviews/` **零留痕**的情况下进了 `cases/`，与"人工复核过的用例"无法区分。
# 于是"默认落点 = `.ai/draft/`"变成**装配器的行为**，而不是调用方的自觉。

def safe_stem(case_name: Text) -> Text:
    """用例名 → 可落盘的模块段（§5.1 的命名口径）。**不查重**（查重见 `case_file_stem`）。"""
    from interfacetester.make import normalize_module_segment  # noqa: PLC0415

    return normalize_module_segment(case_name or "") or "case"


def case_file_stem(case_name: Text, *, out_dir: Text) -> Text:
    """预算落盘名（§5.1 的"命名"行）：`safe_stem` + **查重**。

    查重为什么是硬要求：`cases/` 里可能是人手写的用例。同名覆盖会把人的工作**静默抹掉**
    ——这类损失不可逆，所以宁可报错让人改名。
    """
    stem = safe_stem(case_name)
    target = os.path.join(out_dir, f"{stem}.yml")
    if os.path.exists(target):
        raise FileExistsError(
            f"目标用例已存在，不覆盖：{target}\n"
            "  请改名（`--case-name`）或先移走既有文件——静默覆盖会抹掉人工工作。"
        )
    return stem


def write_unknowns(case_name: Text, unknowns: Sequence[Unknown]) -> Optional[Text]:
    """有 `unknowns` 才落 `.ai/draft/<case>.unknowns.json`；无则**不创建**（同 pending 口径）。"""
    if not unknowns:
        return None
    payload = {
        "case": case_name,
        "count": len(unknowns),
        "note": "以下条目**没有落入用例**：或是无据（降级），或是与文档表述冲突（拒绝）。"
        "请人工确认后回填；确认留痕见 .ai/reviews/（红线⑥）",
        "items": [item.to_dict() for item in unknowns],
    }
    os.makedirs(".ai/draft", exist_ok=True)  # 写入根字面量（T18 静态可判）
    relative = ".ai/draft/" + case_name + ".unknowns.json"
    # 写盘行必须**内联字面量前缀**（T18：目标写成中间变量会被判「不可静态判定」从严违规
    # ——diagnosis / doc_quality / workdir 都踩过一次，这是第四次照面）
    with open(".ai/draft/" + case_name + ".unknowns.json", "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    return relative


def write_draft_record(case_name: Text, draft: CaseDraft, coverage: Coverage) -> Text:
    """把模型原始草案 + 覆盖矩阵落 `.ai/draft/<case>.draft.json`（可复盘 + 覆盖口径）。"""
    os.makedirs(".ai/draft", exist_ok=True)
    relative = ".ai/draft/" + case_name + ".draft.json"
    with open(".ai/draft/" + case_name + ".draft.json", "w", encoding="utf-8") as fp:
        json.dump(
            {"case": case_name, "coverage": coverage.to_dict(), "draft": draft.to_dict()},
            fp,
            ensure_ascii=False,
            indent=2,
        )
        fp.write("\n")
    return relative


def clear_stale_needs_human(case_name: Text) -> Text:
    """这份用例**这一轮通过**了 → 清掉**早先轮次**留下的 `.ai/failed/<case>/`。

    ★为什么必须有这一步（实测，2026-09-26 的**真实复跑**里发现）：修正环里**每一轮**被闸门拒
    都会写一份 `NEEDS_HUMAN.md`；某一轮修好后，那份文件**留在原地**。于是同一份工作区里
    出现两处**互相矛盾**的结论——报告说"**0 片失败**"、`haify review` 说草稿可用，
    而 `.ai/failed/` 里却挂着一份「# 需要人工处理：<用例>」。人只会看到其中一处。

    留痕的价值在于"**它说的是现在的结论**"，所以：通过即清；**拒了照旧写**（`write_needs_human`）。
    删除失败**不改变本轮结论**（结论由闸门给），返回空串只是"没清成、下次再试"——
    绝不把它伪装成"清过了"。
    """
    import shutil  # noqa: PLC0415 - 只用一次，不值得抬到模块级

    target = os.path.join(FAILED_DIR, case_name)
    if not os.path.isdir(target):
        return ""
    try:
        shutil.rmtree(target)
    except OSError:
        return ""
    return target.replace(os.sep, "/")


def write_needs_human(
    case_name: Text,
    *,
    gate_codes: Sequence[Text],
    findings: Sequence[Dict[Text, Any]],
    unknowns: Sequence[Unknown],
    coverage: Coverage,
) -> Text:
    """被闸门拒的草稿 → `.ai/failed/<case>/`（`NEEDS_HUMAN.md` + 覆盖矩阵）。

    §8.1 S-4 的落点细化见模块 docstring（`failed/` → `.ai/failed/`）。
    `NEEDS_HUMAN.md` 的内容刻意不是"失败了"三个字，而是**人接着要干什么**：
    闸门编号 + 逐条发现 + 被降级的断言 + 覆盖口径。
    """
    os.makedirs(".ai/failed", exist_ok=True)
    os.makedirs(".ai/failed/" + case_name, exist_ok=True)
    relative = ".ai/failed/" + case_name + "/NEEDS_HUMAN.md"

    lines = [
        f"# 需要人工处理：{case_name}",
        "",
        "这份草稿**没有进入 `cases/`**——它没通过写盘前置闸门（§8.1 S-4）。",
        "下面是人接着要做的判断，不是「用例失败了」。",
        "",
        f"- 闸门编号：{', '.join(gate_codes) or '（无编号）'}",
        f"- 覆盖口径：{coverage.render()}",
        "",
        "## 闸门发现",
        "",
    ]
    for finding in findings:
        lines.append(
            f"- **{finding.get('code', '?')}**（{finding.get('severity', '?')}）"
            f" {finding.get('where', '')}：{finding.get('message', '')}"
        )
    lines.extend(["", "## 未能落入用例的断言（unknowns）", ""])
    if unknowns:
        for item in unknowns:
            lines.append(f"- `{item.kind}`（{item.level}）{item.where}：{item.message}")
            if item.hint:
                lines.append(f"  - 建议：{item.hint}")
    else:
        lines.append("- （无）")

    lines.extend(
        [
            "",
            "## 原始草案",
            "",
            f"见 `.ai/draft/{case_name}.draft.json`；模型原始响应见 `.ai/manifest/`（§六 审计行）。",
            "",
        ]
    )

    with open(".ai/failed/" + case_name + "/NEEDS_HUMAN.md", "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines))
    return relative


# ---------------------------------------------------------------------------
# 编排：装 → 草稿区 emit/validate → 闸门 → 通过才进 cases/ → pending
# ---------------------------------------------------------------------------

@dataclass
class AssemblyResult:
    """`assemble()` 的最终结果（含落盘路径、闸门结论与**质量状态**）。"""

    case_name: Text
    coverage: Coverage = field(default_factory=Coverage)
    unknowns: List[Unknown] = field(default_factory=list)
    # ★2026-09-26（§九 第一档②）：默认落点改为 `.ai/draft/` → 本字段**只在人工转正后**才有值
    #   （由 `promote_draft()` 填）。装配产出**不再**自动填它 —— 这正是本轮要改的行为。
    yaml_path: Text = ""  # 非空 = 已进 cases/（**只有人工转正才会有**）
    draft_path: Text = ""
    # ★草稿 YAML 的落点（`.ai/draft/<case>.yml`）：CLI 与人工要看的**就是它**
    #   （`draft_path` 是模型原始草案的 JSON，两者不是一回事）
    draft_yaml_path: Text = ""
    unknowns_path: Text = ""
    pending_path: Text = ""
    needs_human_path: Text = ""
    # ★本轮**通过**时清掉的陈旧 `NEEDS_HUMAN`（`.ai/failed/<case>/`，早先轮次留下的）：
    #   非空 = 清掉了一个会与报告**自相矛盾**的留痕（见 `clear_stale_needs_human`）。
    cleared_needs_human: Text = ""
    gate_codes: Tuple[Text, ...] = ()
    rejected: bool = False
    warnings: Tuple[Text, ...] = ()
    # ★`TODO_` 落点规则（§5.2）+ §九 第一档②：True = **产物只在 `.ai/draft/`**（未转正）。
    #   改造后它**恒为真**（除非经 `promote_draft()`）—— 这正是"默认交付草稿"的机器形态。
    draft_only: bool = False
    todo_items: Tuple[Text, ...] = ()
    # ★质量状态（评审 §五）：`ok()` 不再回答"能不能用"，改由这里回答
    quality: QualityAssessment = field(default_factory=QualityAssessment)

    def ok(self) -> bool:
        """**装配流程**是否完成（产物落在草稿区、且没被闸门拒）。

        ★**它不是"可用"**（评审 §五 / §九 第一档②）：`ok()` 为真**只**说明
        "schema + L2 + 静态闸门走完了，产物在 `.ai/draft/`"。
        "能不能直接用"看 `self.quality.is_usable()`；"为什么不能自动晋级"看
        `quality.promotion_gaps(self.quality)`。
        ★命名保留是为了不破坏既有调用方（评审 §7.1 的兼容面）——但**新代码不得**用它决定晋级。
        """
        return bool(self.draft_yaml_path) and not self.rejected

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "case": self.case_name,
            "ok": self.ok(),
            "yaml": self.yaml_path,
            "draft": self.draft_path,
            "draft_yaml": self.draft_yaml_path,
            "unknowns_path": self.unknowns_path,
            "pending_path": self.pending_path,
            "needs_human": self.needs_human_path,
            "cleared_needs_human": self.cleared_needs_human,
            "gate_codes": list(self.gate_codes),
            "coverage": self.coverage.to_dict(),
            "warnings": list(self.warnings),
            "unknowns": [item.to_dict() for item in self.unknowns],
            "draft_only": self.draft_only,
            "todo_items": list(self.todo_items),
            "quality": self.quality.to_dict(),
        }


def assemble(
    draft: CaseDraft,
    doc_text: Text,
    *,
    base_url: Text = "",
    run_gate: bool = True,
    extra_unknowns: Sequence[Unknown] = (),
    scope: Optional[Tuple[int, int]] = None,
) -> AssemblyResult:
    """一次完整装配（§5.1 的装配链 + §8.1 S-4 的写盘前置）。

    ★`out_dir` 固定为 `cases/`（**没有参数**）：写盘白名单登记的就是这个根。
    想换目录就得改 `bench/write_boundary_scanner.py` 的注册表——那正是"扩白名单要签字"
    的形态，不该由调用方在运行期随手决定。

    `scope` = 本用例所属**片的行号范围**（WP2.2 证据绑定的边界，见 `draft_to_ircase`）。
    """
    from interfacetester.converters.emit_yaml import emit_case, validate_emitted_case  # noqa: PLC0415
    from interfacetester.converters.ir import sanitize_case  # noqa: PLC0415
    from interfacetester.loader import load_test_file  # noqa: PLC0415
    from interfacetester_ai.gates import check_testcase  # noqa: PLC0415

    from interfacetester_ai.pending import write_pending_list  # noqa: PLC0415

    outcome = draft_to_ircase(
        draft,
        doc_text=doc_text,
        base_url=base_url,
        extra_unknowns=extra_unknowns,
        scope=scope,
    )
    case_name = draft.case_name
    result = AssemblyResult(
        case_name=case_name,
        coverage=outcome.coverage,
        unknowns=outcome.unknowns,
        draft_path=write_draft_record(case_name, draft, outcome.coverage),
    )
    result.unknowns_path = write_unknowns(case_name, outcome.unknowns) or ""

    # ★⑤ 端点的**连带处置不在这里**（2026-09-25 修正）。
    #
    # 曾经在这里写的是 `result.draft_only = True; return` —— **层次错了**：
    # `draft_only` 的语义是"**已知未定**"（`TODO_` 那条：结构完整、只是期望值等人填），
    # 而"端点全被丢弃"是"**这份文档生成不出东西**"。用前者表达后者，会让 CLI
    # **悄悄成功（退出码 0）**——正是 `assertions_test` 那条
    # 「文档里没有可确定的东西 → **闸门拒**（退出码 1），而不是产出一条空白用例」
    # 要防的形态（那条判据当场把我这个实现打红了）。
    #
    # 所以这里**什么都不做**：产物照常往下走，让**闸门**（S3 断言真空）去说
    # 那句"零断言不算通过"。端点全丢 ⇒ 该用例零 step ⇒ 零断言 —— 同一条判据管得着。
    # （实证：删掉这段之后 `assertions_test` 的拒绝用例自己转绿，`cases/` 保持干净。）

    # ① 先落到**草稿区**（登记根 `.ai/` 内）：这一步之后 cases/ 仍然是干净的
    os.makedirs(".ai/draft", exist_ok=True)
    ir_case = sanitize_case(outcome.ir_case)
    emitted = emit_case(ir_case, ".ai/draft", yaml_name=case_name + ".yml")
    draft_yaml = emitted["yaml"]
    result.draft_yaml_path = draft_yaml  # ★草稿落点（CLI/人工要看的就是它）
    result.warnings = tuple(emitted.get("warnings") or ())

    # ② 出口校验（导出即验证）：加载 + hmake 渲染干跑 + black（内核既有能力，不重造）
    validate_emitted_case(draft_yaml)

    # ③ 写盘前置闸门（S-4）：**带陷阱的产物不落 `cases/`**
    if run_gate:
        case_dict = load_test_file(draft_yaml)
        report = check_testcase(case_dict)
        # ★用 `to_dict()` 读闸门结论：`codes` / `findings` 在 `GateReport` 上是**方法**，
        # 直接当属性用会 `TypeError: 'method' object is not iterable`（实测踩到）。
        # `to_dict()` 是稳定的公开面（S-8 已钉住它的键集）。
        snapshot = report.to_dict()
        if not snapshot["ok"]:
            result.rejected = True
            result.gate_codes = tuple(snapshot.get("codes") or ())
            result.needs_human_path = write_needs_human(
                case_name,
                gate_codes=result.gate_codes,
                findings=list(snapshot.get("findings") or []),
                unknowns=outcome.unknowns,
                coverage=outcome.coverage,
            )
            result.quality = assess_quality(
                rejected=True, gate_codes=result.gate_codes, unknowns=outcome.unknowns
            )
            return result

    # ④ ★落点规则（§九 第一档②，2026-09-26 改；原 §5.2 硬规则的**加强版**）：
    #   产物**只落 `.ai/draft/`**；进 `cases/` 必须经 `promote_draft()`（人工确认 + 留痕）。
    #
    # 为什么从"只有 `TODO_` 才留在草稿区"改成"**一律**留在草稿区"（实证见 §九 9.4）：
    # 原规则只拦住了"没填值的用例"，而**只断言 `status_code` 的薄用例**照样进了 `cases/`
    # （实测 `cases/T1_2_认证方式.yml`，且 `.ai/reviews/` 里零留痕）——于是"没人看过的东西"
    # 与"人工复核过的东西"在调用方看来**没有区别**。这是**会冒充已审核**的形态，
    # 必须从默认行为上关掉，而不是靠人在报告角落里看见一行提示。
    from interfacetester_ai.gates import GateReport  # noqa: PLC0415

    todo_items = todo_findings(draft)
    result.todo_items = tuple(item.to_dict() for item in todo_items)
    result.draft_only = True  # 产物只在草稿区（未转正）
    if todo_items:
        # 带未确认期望值的用例**额外**登记 pending（`code: TODO`）——转正时必被拒（T24）
        result.pending_path = (
            write_pending_list(GateReport(findings=list(todo_items)), safe_stem(case_name)) or ""
        )
    elif run_gate:
        # ⑥ PENDING 清单（闸门保持零副作用：落盘由**调用方**显式做，S-6 口径）
        result.pending_path = write_pending_list(report, safe_stem(case_name)) or ""

    # ⑦ ★质量状态（评审 §五）：`ok()` 不再回答"能不能用"，由它回答
    result.quality = assess_quality(
        rejected=False, unknowns=outcome.unknowns, todo_items=todo_items
    )
    # ⑧ ★清掉**早先轮次**留下的 `NEEDS_HUMAN`（本轮通过了 —— 陈旧留痕会与报告**自相矛盾**，
    #   实测见 `clear_stale_needs_human` 的 docstring）。
    result.cleared_needs_human = clear_stale_needs_human(case_name)
    return result


# ---------------------------------------------------------------------------
# T24：草稿转正（`.ai/draft/` → `cases/`，**必须经 `.ai/reviews/` 留痕**）
# ---------------------------------------------------------------------------


@dataclass
class PromotionResult:
    """草稿转正的结果（T24）。"""

    case_name: Text
    draft_path: Text = ""
    promoted: bool = False
    reason: Text = ""  # 未转正时的人话原因（可直接打给用户）
    yaml_path: Text = ""
    review_path: Text = ""
    # ★WP1.2：审核当时复算出来的质量状态（未转正时也带上——CLI 要能打印"为什么拒"）
    quality: QualityAssessment = field(default_factory=QualityAssessment)

    def ok(self) -> bool:
        return self.promoted

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "case": self.case_name,
            "draft": self.draft_path,
            "promoted": self.promoted,
            "reason": self.reason,
            "yaml": self.yaml_path,
            "review": self.review_path,
            "quality": self.quality.to_dict(),
        }


def promote_draft(
    draft_path: Text,
    *,
    approver: Text,
    approved_at: Text = "",
    notes: Text = "",
    resolved_blockers: Sequence[Text] = (),
) -> PromotionResult:
    """把 `.ai/draft/` 里的草稿**转正**进 `cases/`（T24：必须经留痕）。

    ★为什么"转正"是单独一个入口（而不是让 `assemble` 再来一遍）：`assemble` 的输入是
    **刚产出的** `CaseDraft`，而转正的输入是**人已经改过期望值的那份 YAML**——
    重新装配会丢掉人在 YAML 里的编辑（重跑模型还可能顺手改别的字段）。
    所以转正的语义是"**搬运 + 留痕 + 过门**"，不是"重做"。

    **五步**，每步都有理由：

    1. **谁确认的**（`approver` 非空）——留痕的全部意义就是回答这个；
    2. **读草稿**（读盘目标由调用方给；本模块受 T18 约束的是**写盘**）；
    3. **草稿里不许再有 `TODO_`** → 有则拒（"还没人填值"就转正，CI 会因 `EnvNotFound` 变红，
       而变红的原因**不是缺陷**）；
    4. ★**未处置的 blocker 不许转正**（WP1.2，2026-09-26）：`TODO_` 只是 blocker 的**一种**，
       `protocol-only`（断言全是协议级）与 `unknown-critical`（引用/端点对不上）同样是
       ——它们原先**不进这道门**（实证见 `docs/未做完的待决策项.md` §九 第一档 A）。
       处置必须**逐条点名**（`resolved_blockers`），**自由文本 `notes` 不算处置**；
    5. **查重 + 先留痕、后复制**：`cases/` 已有同名 → 拒（不覆盖人手写的用例）；
       先留痕后复制，万一复制失败留痕还在（重试可用）。
    """
    case_name = os.path.splitext(os.path.basename(draft_path))[0]
    result = PromotionResult(case_name=case_name, draft_path=draft_path)

    # ⓪ 谁确认的？—— 留痕的全部意义就是回答这个（没签名的单子不算留痕）
    if not (approver or "").strip():
        result.reason = (
            "必须用 `--approver <姓名>` 写清**谁**确认的——"
            "留痕的全部意义就是回答这个问题，没签名的单子不算留痕"
        )
        return result

    # ① 草稿必须在
    if not os.path.exists(draft_path):
        result.reason = f"草稿不存在：{draft_path}（先跑 `haify gen` 产出草稿）"
        return result
    with open(draft_path, encoding="utf-8") as fp:
        draft_text = fp.read()

    # ② 还没填值 → 拒（T24 存在的理由）
    if has_todo(draft_text):
        result.reason = (
            "草稿里还有 `${ENV(TODO_...)}` 占位——**先把期望值填上**再转正："
            "没填就进 `cases/`，CI 会因 `EnvNotFound` 变红，而那不是缺陷"
        )
        return result

    # ②′ ★WP1.2：**未处置的 blocker 不许转正**（从既有产物**复算**当前那一版的质量状态；
    #     传 `draft_path` 会**重跑静态闸门**——判的是"当前这一版"，不是过期的留痕）
    assessment = quality_of_draft(case_name, draft_text, draft_path=draft_path)
    result.quality = assessment

    # ②″ ★闸门结论是**硬结论**：`--resolve-blocker` **不能**覆盖它。
    #     理由：S 系列管的是"危险静默形态"（伪存在性断言、断言真空…），
    #     让人用一个开关把它点掉，等于把闸门变成建议——那正是本仓红线③要防的事。
    #     正确动作是**改草稿/改文档后重新生成**，而不是"确认一下放它过去"。
    if BLOCKER_STRUCTURE in assessment.blockers:
        codes = "、".join(assessment.blockers)
        result.reason = (
            f"草稿过不了**静态闸门**（当前这一版）：`{codes}`\n"
            "  · 闸门结论**不能用 `--resolve-blocker` 覆盖**——它管的是"
            "「看起来通过、其实什么都没测」这类危险形态\n"
            "  · 先看 `.ai/failed/<用例>/NEEDS_HUMAN.md`（闸门拒时留下的\"人接着要做什么\"），"
            "改文档后**重新生成**草稿"
        )
        return result

    pending = missing_blockers(assessment.blockers, resolved_blockers)
    if pending:
        result.reason = (
            f"质量状态 `{assessment.status}`：还有 {len(pending)} 条 blocker **没处置**"
            f"（{'、'.join(pending)}）\n"
            "  · 处置要**逐条点名**：`--resolve-blocker " + pending[0] + "`（可重复）\n"
            "  · ★自由文本 `--notes` **不算处置**——否则「我看了」是一句没有对象的话\n"
            "  · 每条 blocker 的原文与建议动作在 `.ai/draft/<用例>.unknowns.json` 里"
        )
        return result

    # ③ 查重（不覆盖人手写的用例）
    try:
        stem = case_file_stem(case_name, out_dir=CASES_DIR)
    except Exception as error:  # noqa: BLE001 - 查重失败要变成人话原因，不是崩
        result.reason = str(error)
        return result

    # ④ 留痕 → 复制（顺序有讲究：先留痕后复制）
    result.review_path = write_review_record(
        case_name,
        draft_path=draft_path,
        draft_text=draft_text,
        approver=approver,
        notes=notes,
        approved_at=approved_at,
        resolved_blockers=tuple(resolved_blockers),
        quality=assessment,
    )
    os.makedirs("cases", exist_ok=True)  # 内联字面量（T18 静态可判）
    with open("cases/" + stem + ".yml", "w", encoding="utf-8") as target:
        target.write(draft_text)
    result.yaml_path = "cases/" + stem + ".yml"
    result.promoted = True
    return result


# ---------------------------------------------------------------------------
# 自检（**纯逻辑、不写盘**）
# ---------------------------------------------------------------------------
#
# NOTICE：装配链的**写盘**部分（emit / validate / 闸门 / 复制进 cases/）不在自检里跑——
# 生产模块只该留**自己的**写盘点（T27 的教训：`doc_quality` 的自检曾因**写夹具**被判 3 处违规）。
# 那部分判据在 `tests/assembler_test.py` 里用**临时工作区**跑（cwd 切走，不污染本仓）。

_SELFTEST_DOC = """# 下单接口

## POST /api/order

返回 200 表示成功。

| 字段 | 类型 | 必填 |
| --- | --- | --- |
| amount | number | 是 |
| remark | string | 可选 |
| token | string | 否 |

说明：

- amount：整数（int），单位分
- remark：可选字符串
- token：字符串，登录令牌

示例（请求体）：

```json
{"code": 0, "amount": 99.9, "token": "abc"}
```
"""


def _a(comparator: Text, check: Text, expect: Any, quote: Text) -> DraftAssertion:
    return DraftAssertion(comparator=comparator, check=check, expect=expect, source_quote=quote)


def run_selftest(verbose: bool = False) -> int:
    """跑装配器自检（对账规则 + 覆盖矩阵；**不写盘**）。返回 0=全绿；非 0=失败项数。"""
    failures: List[Text] = []
    doc = _SELFTEST_DOC
    samples = find_sample_values(doc)

    def audit(assertion: DraftAssertion) -> List[Unknown]:
        return audit_assertion(
            assertion,
            quote=assertion.source_quote,
            doc_text=doc,
            sample_values=samples,
            where="t",
        )

    def expect_clean(label: Text, assertion: DraftAssertion) -> None:
        found = audit(assertion)
        if found:
            failures.append(f"[误伤] {label}：{found[0].kind} {found[0].message}")
        elif verbose:
            print(f"  [放行 ok] {label}")

    def expect_unknown(label: Text, assertion: DraftAssertion, kind: Text) -> None:
        found = audit(assertion)
        if not found:
            failures.append(f"[漏放] {label}：应报 {kind}，却放行了")
        elif not any(item.kind == kind for item in found):
            failures.append(f"[类别错] {label}：期望 {kind}，实为 {[item.kind for item in found]}")
        elif verbose:
            print(f"  [拦下 ok] {label} → {kind}")

    # ① 溯源：引用必须在文档里（逐字；**归一化后**比对）
    if not quote_hits("返回 200 表示成功", doc):
        failures.append("[溯源错] 文档原句没被判为命中")
    if quote_hits("这句话文档里没有", doc):
        failures.append("[溯源漏] 编造的引用被判为命中")
    if not quote_hits("返回\u00a0200 表示成功", doc):
        failures.append("[溯源误伤] NBSP 噪声应当被归一化后命中（T7 管道）")

    # ② 字段名必须**词边界**命中——`token` 不许因 `token_type` 而算命中（§0.5-③ / T21 血债）
    if _word_boundary_hit("token", "返回 token_type 字段"):
        failures.append("[子串漏] `token` 因 `token_type` 被判命中——这正是 T21 踩过的坑")
    if not _word_boundary_hit("token", "token 是字符串"):
        failures.append("[词边界误伤] 独立出现的 `token` 应当命中")
    if not _word_boundary_hit("amount", "字段 amount（number）"):
        failures.append("[词边界误伤] `amount` 后跟中文括号应当命中")

    # ③ 期望值必须有出处
    expect_unknown(
        "期望值凭空",
        _a("equal", "body.remark", "none", "remark：可选字符串"),
        UNKNOWN_EXPECT_NOT_IN_QUOTE,
    )
    expect_unknown(
        "枚举里编了一个值",
        _a("contained_by", "body.amount", ["amount", "看不到的值"], "amount：整数（int），单位分"),
        UNKNOWN_EXPECT_NOT_IN_QUOTE,
    )
    expect_unknown(
        "字段名不在引用句里",
        _a("equal", "body.price", 1, "amount：整数（int），单位分"),
        UNKNOWN_FIELD_NOT_IN_QUOTE,
    )

    # ④ 限定词 ↔ 算子（REJECT 级）
    expect_unknown(
        "可选字段却断言必填",
        _a("type_match", "body.remark", "str", "remark：可选字符串"),
        UNKNOWN_QUALIFIER_CONFLICT,
    )
    expect_clean(
        "可选字段断言\"可缺失\"（不冲突）",
        _a("type_match", "body.remark", "null", "remark：可选字符串"),
    )
    expect_unknown(
        "文档说整数却断言 str",
        _a("type_match", "body.amount", "str", "amount：整数（int），单位分"),
        UNKNOWN_QUALIFIER_CONFLICT,
    )
    expect_clean(
        "文档说整数、断言 int",
        _a("type_match", "body.amount", "int", "amount：整数（int），单位分"),
    )
    expect_unknown(
        "状态码写错（文档 200，断言 201）",
        _a("equal", "status_code", 201, "返回 200 表示成功"),
        UNKNOWN_QUALIFIER_CONFLICT,
    )
    expect_clean(
        "状态码写对",
        _a("equal", "status_code", 200, "返回 200 表示成功"),
    )

    # ⑤ 示例值硬编码（成对：文档标了"固定值" → 放行）
    expect_unknown(
        "把示例里的数值当契约",
        _a("equal", "body.amount", 99.9, "示例（请求体）："),
        UNKNOWN_SAMPLE_VALUE,
    )
    fixed_quote = "amount 固定值为 99.9（业务约定）"
    fixed_doc = doc + "\n" + fixed_quote + "\n"
    found = audit_assertion(
        _a("equal", "body.amount", 99.9, fixed_quote),
        quote=fixed_quote,
        doc_text=fixed_doc,
        sample_values=find_sample_values(fixed_doc),
        where="t",
    )
    if any(item.kind == UNKNOWN_SAMPLE_VALUE for item in found):
        failures.append("[误伤] 文档明说「固定值」时不该因示例值而降级（§十 T28 的成对判据）")

    # ⑥ 覆盖矩阵的输入：文档字段表
    doc_fields = documented_fields(doc)
    for want in ("amount", "remark", "token"):
        if want not in doc_fields:
            failures.append(f"[字段表错] 文档字段表里的 {want!r} 没被提取出来：{doc_fields}")
    if "字段" in doc_fields:
        failures.append("[字段表错] 表头行「字段」被当成字段名了")

    # ⑦ 装配行为：对账失败的断言**不许进用例**，且要计入覆盖口径
    from interfacetester_ai.schema import parse_draft  # noqa: PLC0415

    draft = parse_draft(
        {
            "case_name": "下单",
            "steps": [
                {
                    "name": "下单",
                    "method": "post",
                    "url": "/api/order",
                    "json": {"amount": 99.9},
                    "validate": [
                        {
                            "comparator": "equal",
                            "check": "status_code",
                            "expect": 200,
                            "source_quote": "返回 200 表示成功",
                        },
                        {
                            "comparator": "type_match",
                            "check": "body.amount",
                            "expect": "int",
                            "source_quote": "amount：整数（int），单位分",
                        },
                        {
                            "comparator": "type_match",
                            "check": "body.remark",
                            "expect": "str",
                            "source_quote": "remark：可选字符串",
                        },
                        {
                            "comparator": "equal",
                            "check": "body.price",
                            "expect": 1,
                            "source_quote": "amount：整数（int），单位分",
                        },
                    ],
                }
            ],
        }
    )
    outcome = draft_to_ircase(draft, doc_text=doc)
    cov = outcome.coverage
    kept = sum(len(step.assertions) for step in outcome.ir_case.steps)

    if cov.assertion_count != 4:
        failures.append(f"[覆盖口径错] assertion_count 应为 4，实为 {cov.assertion_count}")
    if len(cov.dropped_assertions) != 2:
        failures.append(f"[覆盖口径错] 应有 2 条被丢，实为 {cov.dropped_assertions}")
    if kept != 2:
        failures.append(f"[装配错] 进用例的断言应为 2 条，实为 {kept}")
    if kept != cov.assertion_count - len(cov.dropped_assertions):
        failures.append("[口径不一致] 「断言数 - 被丢数」应等于进用例的条数")
    if len(outcome.rejected) != 1:
        failures.append(f"[REJECT 分级错] 应恰有 1 条 REJECT（可选↔必填），实为 {outcome.rejected}")
    # ★协议级检查项（status_code）**不算覆盖文档字段**（2026-09-25 判据修正 —— 原来这里
    #   要求它在 `covered_fields` 里）。理由：`_PROTOCOL_CHECKS` 的定义自己就写着"它们
    #   **不是文档里的字段**"，把它算进覆盖，会让"覆盖字段"与"文档声明字段"两个数字
    #   **互相污染**：实测那份真文档声明 45 个字段，报告却印"46 个"（虚高的 1 正好是它）。
    if cov.covered_fields != ("amount",):
        failures.append(f"[覆盖错] 进用例的断言里只有 amount 是文档字段：{cov.covered_fields}")
    if cov.protocol_assertions != ("status_code",):
        failures.append(f"[覆盖错] status_code 应单列为协议级断言：{cov.protocol_assertions}")
    if "price" in cov.covered_fields:
        failures.append("[覆盖错] 被降级的断言不该计入覆盖")
    # ★分母要**量**、不要**算**：它必须等于直接从文档抽出的字段数。用 `covered + uncovered`
    #   反推会在 covered 混进非文档字段时虚高 —— 这条判据专治那个（与上面两条成对：
    #   上面管"谁进 covered"，这条管"分母从哪来"）。
    if len(cov.documented_fields) != len(documented_fields(doc)):
        failures.append(
            "[覆盖口径错] 「文档声明字段」必须直接取自文档："
            f"{len(cov.documented_fields)} != {len(documented_fields(doc))}"
        )
    if f"文档声明字段 {len(cov.documented_fields)} 个" not in cov.render():
        failures.append("[报告口径错] render() 的分母必须用 documented_fields")
    if "协议级断言" not in cov.render():
        failures.append("[报告口径错] 有协议级断言时 render() 应单列它")

    print("=" * 66)
    if failures:
        print(f"装配器自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("装配器自检全部通过（T28 四类对账 + 溯源 + 覆盖矩阵）：")
    print("  溯源    ：引用必须逐字来自文档；NBSP/全角等噪声归一化后仍命中（不误伤）")
    print("  ①字段名 ：**词边界**判定——`token` 不会因 `token_type` 算命中（T21 血债）")
    print("  ②期望值 ：字面量/枚举值必须逐条有出处（凭空 → 降级）")
    print("  ③限定词 ：可选↔必填 / 整数↔str / 状态码不符 → **REJECT**（判据成对，写对就放行）")
    print("  ④示例值 ：示例段数值当契约 → 降级；文档标「固定值」→ 放行（成对）")
    print("  覆盖矩阵：断言数 vs 覆盖字段数并列；被丢的断言不计入覆盖、也不进用例")
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

    parser = argparse.ArgumentParser(description="装配器（P0a D）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "CASES_DIR",
    "DRAFT_DIR",
    "FAILED_DIR",
    "REJECT_KINDS",
    "UNKNOWN_EXPECT_NOT_IN_QUOTE",
    "UNKNOWN_FIELD_NOT_IN_QUOTE",
    "UNKNOWN_OTHER",
    "UNKNOWN_QUALIFIER_CONFLICT",
    "UNKNOWN_QUOTE_BOUND",
    "UNKNOWN_QUOTE_MISS",
    "UNKNOWN_SAMPLE_VALUE",
    "UNKNOWN_TYPE_NAME",
    "AssemblyOutcome",
    "AssemblyResult",
    "Coverage",
    "Unknown",
    "assemble",
    "audit_against_quote",
    "audit_assertion",
    "case_file_stem",
    "check_expect_in_quote",
    "clear_stale_needs_human",
    "check_field_in_quote",
    "check_optional_conflict",
    "check_sample_value_hardcoded",
    "check_status_code_conflict",
    "check_type_qualifier_conflict",
    "documented_fields",
    "draft_to_ircase",
    "field_name_of",
    "find_sample_values",
    "normalize_type_match",
    "quote_hits",
    "run_selftest",
    "safe_stem",
    "todo_findings",
    "write_draft_record",
    "write_needs_human",
    "write_unknowns",
    "UNKNOWN_ENDPOINT_MISS",
    "check_endpoint_in_doc",
    "endpoint_candidates",
    "has_actual_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
