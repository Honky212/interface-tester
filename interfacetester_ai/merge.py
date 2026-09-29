# -*- coding: utf-8 -*-
r"""merge —— **模型草稿 × 确定性映射** 的合并（决策清单 §三 **C-2**，2026-09-26 立项）。

## 要解决的问题（实测，不是设想）

模型只产**协议级**断言（P0a 那轮 13 条全是 `equal(status_code)`），而确定性映射能产**字段级**
断言（类型/范围/枚举）——两者本该互补，原先却是**两条独立命令，要人手工跑两遍**。
本模块把"装配前跑一遍确定性映射并合并"落成一步。

## 三处口径（**已定**，不是这里临时发明）

1. **只补不覆盖**：模型已经对某个 `check` 断言过 → **保留模型的**；确定性映射对同一 `check`
   若内容不同 → 记一条**冲突**（`conflicts`），**不自动改**、也不静默丢。
   为什么不让确定性优先覆盖：覆盖会**改掉模型给的证据**，而"谁对"要看语义——那不是代码该猜的；
   但"有分歧"必须**看得见**（进 `unknowns` → 报告 → 人工）。
2. **多步用例不合并**：多步用例里"这条断言属于哪个 step"要靠 method/url 对齐（易错）。
   宁可不做，并**明说没做**（进 `skipped`）——静默少做才是真风险。
3. **按行号绑定**：确定性映射**整篇文档跑一次**，只有 `line_no ∈ 该片的行号范围` 的断言才并进
   "这个片产出的用例"。为什么不是拿片文本直接映射：**字段表常在独立章节里**，而独立章节会被
   判成"非接口片"跳过 → 按片文本映射会得到 0 条。绑不上的断言进 `skipped`（**可见**）。

## 边界

- **纯函数**：不写盘、不调模型（`unknowns` 由调用方落盘——与 `assertions.py` 同款纪律）。
- **继承 `assertions.py` 的三条自律**：可选字段不断言、`number` 不猜类型、业务错误码不进正常用例；
  本模块**只做加法**，不放松任何一条。
- 若推导结果带 `TODO_`（`needs_human=True`）：**照合**。合进去以后该用例自然只能落 `.ai/draft/`
  （由装配器的落点规则负责）——这正是"宁缺勿编"，不是缺陷。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, List, Mapping, Optional, Sequence, Text, Tuple

from interfacetester_ai.assertions import DerivedAssertion, derive_assertions, has_todo
from interfacetester_ai.schema import CaseDraft, DraftAssertion, DraftStep

# 回滚开关（两条，成对判据见 tests/merge_test.py）
MERGE_ENV = "INTERFACETESTER_AI_MERGE_ASSERTIONS"
_FALSE_TOKENS = ("0", "off", "false", "no", "n", "disable", "disabled")


def merge_enabled(cli_flag: bool = True, env: Optional[Mapping[Text, Text]] = None) -> bool:
    """默认**开**；`--no-merge-assertions`（`cli_flag=False`）或 env 显式关 → 关。

    两条回滚都留：环境变量管"我这台机器先别合"，CLI 开关管"这一条命令先别合"。
    """
    if not cli_flag:
        return False
    raw = (env if env is not None else os.environ).get(MERGE_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSE_TOKENS


@dataclass(frozen=True)
class MergeOutcome:
    """合并结果：**改过的**草稿 + 三类可汇报信息。"""

    draft: CaseDraft
    added: Tuple[Text, ...] = ()
    conflicts: Tuple[Text, ...] = ()
    skipped: Tuple[Text, ...] = ()

    def touched(self) -> bool:
        return bool(self.added)

    def notes(self) -> Tuple[Text, ...]:
        """给 `unknowns` 用的人话（冲突与跳过都要让人看见）。"""
        out: List[Text] = []
        out.extend(f"[冲突·保留模型的] {item}" for item in self.conflicts)
        out.extend(f"[未合并] {item}" for item in self.skipped)
        return tuple(out)


def _belongs(
    item: DerivedAssertion,
    owner_ranges: Optional[Sequence[Tuple[int, int]]],
    start: Optional[int],
    end: Optional[int],
) -> bool:
    """这条推导断言**属不属于当前这个接口段**。

    ★★ 绑定口径（2026-09-26 修正，**A 方案**）——**"接口段 + 它名下的子节"，不是"接口片本身"**。
    为什么必须改（**真模型实测踩出来的**）：golden 文档的写法是

        ## POST /api/login      ← 接口片只到这一行的下一节
        ### 请求体（JSON）
        | 字段 | 类型 | 必填 | 说明 |   ← 字段表在这里（**子节**，且它被判为"非接口片"跳过）
        ### 响应

    于是"行号 ∈ 接口片范围"**必然绑不上**字段表 → 2026-09-26 那一轮 15 篇产出里
    **129 条全走"未合并"**、落盘产物仍是清一色 `equal(status_code)`（合并净效果 0）。
    正确归属就是文档作者的意图：**子节归它的父接口段**（用 `Slice.title_path` 前缀判）。
    调用方（`gen`）把"本片 + 本片名下所有子节"的行号区间算好传进来。

    边界（都保留"可见"）：
    - `owner_ranges` 给了 → 只认这些区间；
    - 只给 `start/end`（旧口径）→ 等价于单个区间（**向后兼容**，老判据继续有效）；
    - 两者都不给 → 不绑定（整篇都用）；
    - `line_no <= 0`（行号未知）→ **不合并**（宁可少做，不可绑错）。
    """
    if item.line_no <= 0:
        return False
    if owner_ranges:
        ranges = [(lo, hi) for lo, hi in owner_ranges]
    elif start is not None and end is not None:
        ranges = [(start, end)]
    else:
        return True
    return any(lo <= item.line_no <= hi for lo, hi in ranges)


def _describe(item: DerivedAssertion) -> Text:
    return f"{item.check} [{item.comparator} {item.expect!r}]"


def merge_derived_assertions(
    draft: CaseDraft,
    doc_text: Text,
    *,
    slice_start: Optional[int] = None,
    slice_end: Optional[int] = None,
    owner_ranges: Optional[Sequence[Tuple[int, int]]] = None,
) -> MergeOutcome:
    """把确定性映射的结果合并进模型草稿（**只补不覆盖**）。

    参数 `owner_ranges` = 本接口段**及其名下所有子节**的行号区间（`gen` 按 `Slice.title_path`
    前缀算好传进来）——这是**主口径**；只给 `slice_start/slice_end` 时退化成"行号 ∈ 本片"
    （旧口径，**向后兼容**）；都不给则按整篇处理。
    """
    derived, _why_not = derive_assertions(doc_text or "")
    if not derived:
        return MergeOutcome(draft=draft)

    skipped: List[Text] = []
    if len(draft.steps) != 1:
        skipped.append(
            f"多步用例（{len(draft.steps)} 步）不做自动合并——避免绑错步骤；"
            f"确定性映射另产 {len(derived)} 条字段级断言，请人工或 `--assertions-only` 处理"
        )
        return MergeOutcome(draft=draft, skipped=tuple(skipped))

    in_slice: List[DerivedAssertion] = []
    for item in derived:
        if _belongs(item, owner_ranges, slice_start, slice_end):
            in_slice.append(item)
        else:
            where = "行号未知" if item.line_no <= 0 else f"引用行在第 {item.line_no} 行"
            scope = (
                "、".join(f"{lo}~{hi}" for lo, hi in owner_ranges)
                if owner_ranges
                else f"{slice_start}~{slice_end}"
            )
            skipped.append(f"{_describe(item)}：{where}，不在本接口段（{scope} 行）内")

    existing = draft.assertions()
    by_check: dict[Text, List[DraftAssertion]] = {}
    for item in existing:
        by_check.setdefault(item.check, []).append(item)

    added: List[Text] = []
    conflicts: List[Text] = []
    new_assertions: List[DraftAssertion] = []
    for item in in_slice:
        already = by_check.get(item.check)
        if already:
            same = any(
                other.comparator == item.comparator and other.expect == item.expect
                for other in already
            )
            if same:
                skipped.append(f"{_describe(item)}：模型已给出同一条断言，无需补")
            else:
                conflicts.append(
                    f"{_describe(item)}：模型写的是 "
                    + "、".join(f"[{other.comparator} {other.expect!r}]" for other in already)
                    + "，确定性映射给出不同值 → **保留模型的**"
                )
            continue
        note = "（含未确认期望值 `TODO_` → 只许落 `.ai/draft/`）" if (
            item.needs_human or has_todo(item.expect)
        ) else ""
        added.append(f"{_describe(item)}：{item.reason}{note}")
        new_assertions.append(
            DraftAssertion(
                comparator=item.comparator,
                check=item.check,
                expect=item.expect,
                source_quote=item.source_quote,
            )
        )

    if not new_assertions:
        return MergeOutcome(draft=draft, conflicts=tuple(conflicts), skipped=tuple(skipped))

    step = draft.steps[0]
    merged_step = replace(step, validate=step.validate + tuple(new_assertions))
    return MergeOutcome(
        draft=replace(draft, steps=(merged_step,)),
        added=tuple(added),
        conflicts=tuple(conflicts),
        skipped=tuple(skipped),
    )


__all__ = [
    "MERGE_ENV",
    "MergeOutcome",
    "merge_derived_assertions",
    "merge_enabled",
]
