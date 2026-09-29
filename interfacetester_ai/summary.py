# -*- coding: utf-8 -*-
"""summary —— **结果摘要**（v8 §5.4 `haify summary`；**P1b**，2026-09-25）。

## 一句话

**数字全部由代码从 `summary.json` 算**，模型只写 2~3 句导语；**没有模型也照常产出报告**。

## 为什么"数字必须由代码算"（这是本模块存在的全部理由）

让模型念数字听起来更"智能"，但那是把**唯一可靠的信号**交给一个会算错的部件：
`total=4 / success=2 / fail=2` 这种数字，代码算错是很难的、模型算错是很容易的，
而在报告里**两者长得一模一样**——读者无从分辨。所以：

- `stat.testcases` 是**权威来源**（内核自己统计的）；
- 模板版（`render_template_report`）**不依赖任何模型**；
- 导语里的数字要过**白名单复核**（`verify_lead`）：模板里没出现过的数字一律不许出现在
  导语里，**违规即弃用导语**（退回纯模板版）——不是"警告一下"，是**整段丢掉**：
  一句带错数字的导语比没有导语坏得多。

## 两个口径标签（P-8 的"数字必须带口径"）

1. **耗时的单位与来源**：取 `details[i].time.duration`（**用例级**秒数），不是
   `records[j].elapsed`（步骤级）——两者都能算"耗时 TopN"，但**混用就没法比**；
   报告里写明"用例级"。
2. **失败分组的前缀口径**：按用例名里**第一个 `_` 之前**的片段分组；
   名字里没有 `_` 的**各自成组**（中文用例名很常见，这条得写清，否则"分布"会变成
   一长串每条一组，看着像分组其实没分）。

## 无 LLM 时（§5.4 的"报告永不缺席"）

`transport=None` / 缺配置 / 传输失败 → **都退回模板版**，**不是错误**。
这与 `gen`/`analyze` 的"缺配置 → 退出码 2"**故意不同**：那两个任务的产物**必须**由模型
生成（没有模型就没有产物）；而摘要的产物**本来就是代码渲染的**，模型只是加一段话。
把"锦上添花"失败升级成"整件事失败"，是拿主产物给附加物陪葬。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Text, Tuple

from interfacetester_ai.llm import (
    LLMConfigMissing,
    LLMRequest,
    LLMTransportError,
    Transport,
    complete,
    strip_code_fence,
)
from interfacetester_ai.prompts import PROMPT_VERSION, render_summary_system_prompt

# 报告落点（与 `analyze`/`diagnosis` 同根 `reports/`；T18 注册表登记项）
SUMMARY_REL_PATH = "reports/summary.md"

# 耗时 TopN 的 N（§5.4 明写 TopN，未给数；取 5：够看，又不至于把报告变成榜单）
TOP_N = 5

# 导语里的数字识别（整数 / 小数 / 百分比里的数字都算）
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

# "没有 `_` 的用例名各自成组"时的组名（写清口径，别让它看着像分组其实没分）
UNGROUPED = "（未分组）"


# ---------------------------------------------------------------------------
# 数据层：`summary.json` → 统计（**纯代码**）
# ---------------------------------------------------------------------------

def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def case_prefix(name: Text) -> Text:
    """用例名的分组前缀（口径：**第一个 `_` 之前**；没有 `_` 则各自成组）。

    ★为什么把口径写在函数里：§5.4 只说"按用例名前缀分组的失败分布"，没说"前缀"怎么切。
    没有 `_` 的中文用例名（本仓真实产物里很常见）如果按"整名成组"，报告里会出现
    **每行一组**——看着像分组，其实什么也没分。所以这里给一个**可执行**的定义，
    并在报告里把口径写出来（P-8 的"数字必须带口径"）。
    """
    name = (name or "").strip()
    if "_" in name:
        head = name.split("_", 1)[0].strip()
        return head or UNGROUPED
    return name or UNGROUPED


@dataclass(frozen=True)
class SummaryStats:
    """`haify summary` 的全部数字（**每个字段都能在 `summary.json` 里指到出处**）。"""

    total: int = 0
    success: int = 0
    fail: int = 0
    steps_total: int = 0
    steps_fail: int = 0
    steps_success: int = 0
    duration: float = 0.0  # 整体耗时（秒；来源：顶层 `time.duration`）
    failures_by_prefix: Tuple[Tuple[Text, int], ...] = ()  # 失败分布（按用例名前缀）
    slowest: Tuple[Tuple[Text, float], ...] = ()  # 耗时 TopN（**用例级**）
    failed_cases: Tuple[Text, ...] = ()

    def duration_text(self) -> Text:
        """耗时的人话（秒 → 秒/分/时）。

        ★**只产出一种形态**：多一份"383.3 秒 ≈ 6.4 分"就会给导语白名单多开一个口子
        （两个数都对，但一个在模板里、一个不在 → 复核会把它判成违规）。
        口径越单一，白名单越可靠。
        """
        seconds = float(self.duration or 0.0)
        if seconds < 60:
            return f"{seconds:.1f} 秒"
        if seconds < 3600:
            return f"{seconds / 60:.1f} 分"
        return f"{seconds / 3600:.1f} 时"


def collect_stats(summary: Any) -> SummaryStats:
    """从 `summary.json` 算全部数字。**权威来源是 `stat` 与 `details`**，不是顶层 `success`。

    ★为什么不直接读顶层 `success`：那是个 bool，只能回答"有没有失败"，回答不了
    "几个"。而 `stat.testcases` 是**内核自己统计的**（`projection.py` 的遍历也依赖同一份
    结构）——报告的数字要能被人拿 `summary.json` 逐字对齐，就得用同一个来源。
    """
    if not isinstance(summary, dict):
        return SummaryStats()

    stat = summary.get("stat")
    stat = stat if isinstance(stat, dict) else {}
    cases = stat.get("testcases")
    cases = cases if isinstance(cases, dict) else {}
    steps = stat.get("teststeps")
    steps = steps if isinstance(steps, dict) else {}
    top_time = summary.get("time")
    top_time = top_time if isinstance(top_time, dict) else {}

    details = summary.get("details")
    details = details if isinstance(details, list) else []

    counters: Dict[Text, int] = {}
    slow: List[Tuple[Text, float]] = []
    failed: List[Text] = []

    for detail in details:
        if not isinstance(detail, dict):
            continue
        name = str(detail.get("name") or detail.get("case_id") or "")
        if detail.get("success") is False:
            prefix = case_prefix(name)
            counters[prefix] = counters.get(prefix, 0) + 1
            failed.append(name)
        time_block = detail.get("time")
        seconds = _as_float(time_block.get("duration")) if isinstance(time_block, dict) else 0.0
        slow.append((name, seconds))

    # ★同分按名字定序：报告要"同输入两次逐字节一致"，排序就不能依赖输入顺序的偶然
    slow.sort(key=lambda item: (-item[1], item[0]))

    return SummaryStats(
        # `stat` 缺失时用 `details` 兜底（异常输入不该让摘要整个空掉）
        total=_as_int(cases.get("total"), len(details)),
        success=_as_int(cases.get("success"), 0),
        fail=_as_int(cases.get("fail"), 0),
        steps_total=_as_int(steps.get("total"), 0),
        steps_fail=_as_int(steps.get("failures"), 0),
        steps_success=_as_int(steps.get("successes"), 0),
        duration=_as_float(top_time.get("duration")),
        failures_by_prefix=tuple(sorted(counters.items(), key=lambda kv: (-kv[1], kv[0]))),
        slowest=tuple(slow[:TOP_N]),
        failed_cases=tuple(failed),
    )


# ---------------------------------------------------------------------------
# 渲染层：模板版（**不依赖模型**）+ 导语 + 数字白名单复核
# ---------------------------------------------------------------------------

# 分组口径的说明文字（写进报告，读者才知道"前缀"是怎么切的）
PREFIX_NOTICE = "按用例名里**第一个 `_` 之前**的片段分组；名字里没有 `_` 的各自成组。"


def render_template_report(stats: SummaryStats, *, source: Text = "") -> Text:
    """**纯模板版**报告（§5.4："无 LLM 时模板版照常产出——报告永不缺席"）。"""
    lines = [
        "# 结果摘要（由 interfacetester_ai.summary 生成）",
        "",
        "> 本页**所有数字由代码从 `summary.json` 渲染**；模型只写导语，而导语里的数字",
        "> 要过**白名单复核**——模板里没出现过的数字一律不许出现，违规即**整段弃用**。",
        "",
    ]
    if source:
        lines += [f"- 输入：`{source}`", ""]

    lines += [
        "## 总览",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 用例总数 | {stats.total} |",
        f"| 成功 | {stats.success} |",
        f"| 失败 | {stats.fail} |",
        f"| 步骤总数 | {stats.steps_total} |",
        f"| 步骤成功 | {stats.steps_success} |",
        f"| 步骤失败 | {stats.steps_fail} |",
        f"| 整体耗时 | {stats.duration_text()} |",
        "",
        "> 耗时口径：**用例级**（顶层 `time.duration`，单位秒），不是步骤级 "
        "`records[j].elapsed`——两者都能算 TopN，但**混用就没法比**。",
        "",
    ]

    if stats.failures_by_prefix:
        lines += [
            "## 失败分布（按用例名前缀）",
            "",
            f"> 分组口径：{PREFIX_NOTICE}",
            "",
            "| 前缀 | 失败数 |",
            "| --- | --- |",
        ]
        lines += [f"| {prefix} | {count} |" for prefix, count in stats.failures_by_prefix]
        lines.append("")
    else:
        lines += ["## 失败分布", "", "**无失败用例。**", ""]

    if stats.slowest:
        lines += [
            "## 耗时 TopN（用例级）",
            "",
            "| # | 用例 | 耗时（秒） |",
            "| --- | --- | --- |",
        ]
        lines += [
            f"| {index} | {name} | {seconds:.3f} |"
            for index, (name, seconds) in enumerate(stats.slowest, 1)
        ]
        lines.append("")
    return "\n".join(lines)


def allowed_numbers(stats: SummaryStats) -> Set[Text]:
    """**导语允许出现的数字** = 模板里真的渲染出来的数字（"唯一来源"的可执行形态）。

    ★为什么不手工列一份字段清单：手工清单**必然**与模板漂移（模板加了一行、清单没跟上
    → 合规导语被误判违规；反过来则是漏检）。**扫同一份模板拿到的数字，才是它真的写出来的数字。**
    """
    return set(NUMBER_RE.findall(render_template_report(stats)))


def verify_lead(lead: Text, stats: SummaryStats) -> Tuple[bool, Text]:
    """导语数字白名单复核（§5.4）。返回 `(是否可用, 原因)`；违规 → **弃用导语**。

    ★为什么是"整段弃用"而不是"警告一下"：报告的读者不会去看警告，他会看那句导语。
    一份导语写着"共 3 个失败"而实际是 2 的报告，比没有导语坏得多——
    **错误的数字比缺失的数字更危险**，因为它看起来同样可信。
    """
    text = (lead or "").strip()
    if not text:
        return False, "导语为空"
    outside = sorted({n for n in NUMBER_RE.findall(text) if n not in allowed_numbers(stats)})
    if outside:
        return False, f"导语里的数字未在模板中出现：{'、'.join(outside)}（§5.4：违规即弃用）"
    return True, ""


def render_summary_report(
    stats: SummaryStats, lead: Text = "", *, source: Text = ""
) -> Tuple[Text, Text]:
    """拼最终报告 → `(正文, 导语状态说明)`。导语不合法 → **只用纯模板版**。

    返回状态说明是为了**留痕**：报告读不到"导语被丢过"这件事，但 CLI 与测试要能看到
    （否则"导语为什么没出现在报告里"会变成需要翻代码的悬案）。
    """
    body = render_template_report(stats, source=source)
    text = (lead or "").strip()
    if not text:
        return body, "导语：未提供 → 模板版（§5.4：报告永不缺席）"

    ok, why = verify_lead(text, stats)
    if not ok:
        return body, f"导语：**已弃用**（{why}）"

    marker = "## 总览"
    head, _, tail = body.partition(marker)
    return f"{head}## 导语\n\n{text}\n\n{marker}{tail}", "导语：采用（数字白名单复核通过）"


def write_summary_report(text: Text) -> Text:
    """**原子写** `reports/summary.md`（先 `.tmp` 再 `os.replace`）。返回相对路径。

    与 `analysis.md` 同款纪律：半份报告比没有报告更坏——人会以为摘要已经出好了。
    """
    os.makedirs("reports", exist_ok=True)  # 内联字面量（T18 静态可判）
    with open("reports/summary.md.tmp", "w", encoding="utf-8") as fp:
        fp.write(text)
    os.replace("reports/summary.md.tmp", "reports/summary.md")
    return SUMMARY_REL_PATH


# ---------------------------------------------------------------------------
# 主入口：`summary.json` → 统计 →（可选）导语 → `reports/summary.md`
# ---------------------------------------------------------------------------

# 导语只有 2~3 句，256 token 足够；给太多反而容易"复述整张表"
LEAD_MAX_TOKENS = 256


@dataclass
class SummaryResult:
    """一次摘要的结果（含**导语的状态**，方便调用方留痕）。"""

    stats: SummaryStats = field(default_factory=SummaryStats)
    lead: Text = ""
    lead_status: Text = ""
    report_path: Text = ""
    cached: bool = False

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "total": self.stats.total,
            "success": self.stats.success,
            "fail": self.stats.fail,
            "lead": self.lead,
            "lead_status": self.lead_status,
            "report_path": self.report_path,
            "cached": self.cached,
        }


def build_lead_request(
    stats: SummaryStats,
    *,
    model: Text,
    base_url: Text,
    temperature: float = 0.0,
    max_tokens: int = LEAD_MAX_TOKENS,
) -> LLMRequest:
    """组装**导语**请求：把**已经算好的模板版正文**交给模型，它只加一段话。"""
    return LLMRequest(
        system=render_summary_system_prompt(),
        user=render_template_report(stats),
        model=model,
        base_url=base_url,
        prompt_version=PROMPT_VERSION,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def summarize_summary(
    summary: Any,
    *,
    transport: Optional[Transport] = None,
    config: Any = None,
    cache: Optional[Any] = None,
    audit: Optional[Any] = None,
    write: bool = True,
    source: Text = "",
) -> SummaryResult:
    """`summary.json` → 统计 →（可选）导语 → `reports/summary.md`。

    ★**没有 `transport` 也照样出报告**（§5.4 的"报告永不缺席"）：`transport=None` /
    缺配置 / 传输失败 → **一律退回模板版**。理由：摘要的产物本来就是代码渲染的
    （**数字是主产物**），模型只加一段锦上添花的话。把"锦上添花"失败升级成"整件事失败"，
    等于拿主产物给附加物陪葬——而 §5.4 明写"报告永不缺席"。
    （对比 `gen`/`analyze`：那两件事**没有模型就没有产物**，所以缺配置必须报错。）
    """
    stats = collect_stats(summary)
    result = SummaryResult(stats=stats)
    lead = ""

    if transport is not None:
        request = build_lead_request(
            stats,
            model=getattr(config, "model", "") or "",
            base_url=getattr(config, "base_url", "") or "",
        )
        try:
            response = complete(
                request, transport=transport, config=config, cache=cache, audit=audit
            )
        except (LLMTransportError, LLMConfigMissing) as error:
            # ★不抛：降级成模板版（`lead_status` 留痕，报告照出）
            result.lead_status = f"导语：未生成（{error}）→ 模板版"
        else:
            result.cached = response.cached
            lead = strip_code_fence(response.text).strip()

    text, status = render_summary_report(stats, lead, source=source)
    if status.startswith("导语：采用"):
        result.lead = lead
    result.lead_status = result.lead_status or status
    if write:
        result.report_path = write_summary_report(text)
    return result


# ---------------------------------------------------------------------------
# 自检（**不写盘**：只渲染、不落盘；落盘在 `tests/` 的临时工作区里测）
# ---------------------------------------------------------------------------

_SELFTEST_SUMMARY: Dict[Text, Any] = {
    "success": False,
    "stat": {
        "testcases": {"total": 4, "success": 2, "fail": 2},
        "teststeps": {"total": 6, "failures": 2, "successes": 4},
    },
    "time": {"duration": 383.3},
    "details": [
        {"name": "login_flow", "success": True, "time": {"duration": 1.766}},
        {"name": "order_create", "success": False, "time": {"duration": 2.040}},
        {"name": "order_pay", "success": False, "time": {"duration": 120.557}},
        # 名字里没有 `_`（中文用例名很常见）→ 各自成组，不硬凑前缀
        {"name": "查询商品", "success": True, "time": {"duration": 3.992}},
    ],
}


def run_selftest(verbose: bool = False) -> int:
    """summary 自检。返回 0=全绿；非 0=失败项数。**不写盘**。"""
    failures: List[Text] = []
    stats = collect_stats(_SELFTEST_SUMMARY)

    # ① **数字与 `summary.json` 全等**（§5.4 的第一条验收）——逐个字段对齐 `stat`
    if (stats.total, stats.success, stats.fail) != (4, 2, 2):
        failures.append(f"[数字] 用例统计与 stat.testcases 不等：{stats.total}/{stats.success}/{stats.fail}")
    if (stats.steps_total, stats.steps_success, stats.steps_fail) != (6, 4, 2):
        failures.append(f"[数字] 步骤统计与 stat.teststeps 不等：{stats.steps_total}/{stats.steps_success}/{stats.steps_fail}")

    # ② 失败分布：按前缀分组（`order_*` 两条 → `order`；没有 `_` 的名字各自成组）
    if stats.failures_by_prefix != (("order", 2),):
        failures.append(f"[分布] 应当只有 order=2：{stats.failures_by_prefix}")

    # ③ 耗时 TopN：慢的在前（用例级 `time.duration`）
    order = [name for name, _seconds in stats.slowest]
    if order[:1] != ["order_pay"] or len(order) != 4:
        failures.append(f"[TopN] 耗时排序不对：{order}")

    # ④ 耗时的**口径标签**：秒 → 分（383.3 秒 = 6.4 分），且只产一种形态
    if stats.duration_text() != "6.4 分":
        failures.append(f"[耗时] 文案应当是 6.4 分：{stats.duration_text()}")

    # ⑤ **模板版不依赖模型**：没有 transport 也要有正文
    body, status = render_summary_report(stats)
    for token in ("## 总览", "用例总数", "失败分布（按用例名前缀）", "耗时 TopN"):
        if token not in body:
            failures.append(f"[模板] 正文缺 {token!r}")
    if "未提供" not in status:
        failures.append(f"[模板] 没导语时状态应当说明是模板版：{status}")

    # ⑥ **白名单复核**：只用模板里出现过的数字 → 采用；否则 → **弃用**
    good = "本次共 4 个用例，其中 2 个失败，集中在 order 前缀。"
    # ★选 7 而不是 3：`3` 其实**在**模板里——它是耗时 TopN 的**序号**（`| 3 | ... |`）。
    # 这类"看起来该违规、其实合规"的例子，正说明白名单必须**扫模板**而不是凭直觉列；
    # 凭直觉列清单时，正是这种数会被误判成违规（合规导语被丢弃）。
    bad = "本次共 7 个用例失败。"
    ok_good, why_good = verify_lead(good, stats)
    ok_bad, why_bad = verify_lead(bad, stats)
    if not ok_good:
        failures.append(f"[白名单] 合规导语被误判违规：{why_good}")
    if ok_bad:
        failures.append("[白名单] 模板里没有的数字竟然通过了复核")

    # ⑦ **弃用要落到正文**：不合规的导语不能在报告里出现（"弃用"不能只是嘴上说）
    merged_bad, status_bad = render_summary_report(stats, bad)
    if bad in merged_bad:
        failures.append("[弃用] 违规导语仍然出现在正文里")
    if "已弃用" not in status_bad:
        failures.append(f"[弃用] 状态里应当写明已弃用：{status_bad}")
    merged_good, status_good = render_summary_report(stats, good)
    if good not in merged_good or "采用" not in status_good:
        failures.append("[采用] 合规导语应当被采用并写进正文")

    # ⑧ **可复现**：同输入两次渲染逐字节相同（报告要能进 diff）
    if render_template_report(stats) != body:
        failures.append("[可复现] 两次渲染不一致")

    print("=" * 66)
    if failures:
        print(f"summary 自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("结果摘要自检全部通过（§5.4，**不需要模型**）：")
    print("  数字  ：全部由代码算，逐字段对齐 `stat.testcases` / `stat.teststeps`")
    print("  分布  ：按用例名前缀分组（口径写进报告：第一个 `_` 之前；没有则各自成组）")
    print("  TopN  ：用例级 `time.duration`，同分按名字定序（报告可进 diff）")
    print("  白名单：导语里的数字必须在模板里出现过，**违规即整段弃用**（正文里真的没有它）")
    print("  永不缺席：没有模型也照样出模板版（模型只加 2~3 句导语）")
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

    parser = argparse.ArgumentParser(description="结果摘要（P1b）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "LEAD_MAX_TOKENS",
    "NUMBER_RE",
    "PREFIX_NOTICE",
    "SUMMARY_REL_PATH",
    "TOP_N",
    "UNGROUPED",
    "SummaryResult",
    "SummaryStats",
    "allowed_numbers",
    "build_lead_request",
    "case_prefix",
    "collect_stats",
    "render_summary_report",
    "render_template_report",
    "run_selftest",
    "summarize_summary",
    "verify_lead",
    "write_summary_report",
]


if __name__ == "__main__":
    raise SystemExit(main())



