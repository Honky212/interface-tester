# -*- coding: utf-8 -*-
"""静默陷阱闸门（Silent-Trap Gates）—— 把「已知会静默假通过的形态」做成生成期硬闸门。

## 为什么要有这个文件

interfacetester 5.0.1 的内核已经把**大量**静默陷阱消灭在源码里（见 `bench/SILENT_TRAPS.md`
的完整清单）。但内核能消灭的只是「**运行期可判定**」的那一类：

    `comparators._ensure_string_shaped` —— string_equals/startswith/… 取到 None 时
    **直接判失败**，因为「None 不可能是字符串」是运行期就能确定的。

而另一类**永远无法在运行期判定**：

    `response._warn_value_not_found` 的 docstring 写得很清楚：
    `not_equal: ["body.data.access_token", ""]` 这类「字段存在且非空」的断言，
    在字段**根本不存在**时**通过**；框架**只能告警**——
    因为 jmespath 对「字段缺失」与「值为 null」都返回 None，两者**无法区分**，
    硬判失败会破坏 `eq: [body.x, null]` 这类合法写法。

**但生成期能判定。** 生成期看得到 YAML 原文，也就看得到**意图**：
`not_equal: [body.x, ""]` 的意图是"存在且非空"，这在静态上可识别。
所以「生成期闸门」是消灭这一类陷阱的**唯一位置**——这正是本文件存在的理由。

## 设计纪律（继承内核口径）

内核 `comparators.py` 有一句话定义了整个仓库的取向：

> 本仓的取向是「**只拦能精确判定的形态**」，假报会让真报被无视。

本模块严格遵守：**每一条闸门都必须配反例自检**（合法写法必须被放行）。
`run_selftest()` 里 `MUST_REJECT` 与 `MUST_ALLOW` 两组样本同时跑，
任何一条不满足都算闸门自身失效（比"漏拦"更严重——它会教用户忽略真报）。

## 与内核的关系

- **复用**内核既有护栏（不重造）：`ensure_known_comparators`、`ensure_json_schema_not_inline`、
  `ensure_generatable_teststeps`、`normalize_module_segment`、`uniform_validator`。
- **新写**的是内核**结构上做不到**的：S1/S2/S3/S4/S5/S7（意图级判定）。

## 用法

    python -m interfacetester_ai.gates              # 跑自检（含反例）
    python -m interfacetester_ai.gates --selftest -v  # 逐条打印
    python bench/silent_traps.py                    # 兼容壳入口（转发，等价）
    from interfacetester_ai.gates import check_testcase  # 接入 gen/fix 流程

NOTICE（T6 迁移注记，2026-09-23）：本模块已从 `bench/silent_traps.py`
整体迁入 `interfacetester_ai` 包（v8 §十 T6；单一来源——bench 路径保留
**兼容壳**转发，两处 import 得到的是同一批对象，不产生双源漂移）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Text, Tuple

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------

# 严重级别：
#   REJECT  —— 必须拦下（形态可精确判定为"意图与实现不符"）
#   PENDING —— 不拦，但要求人工确认（进 *.pending.json）
REJECT = "reject"
PENDING = "pending"


@dataclass
class Finding:
    """一条闸门发现。`code` 是稳定的机器可读编号（S1/S2/...）。"""

    code: Text
    severity: Text
    where: Text
    message: Text
    hint: Text = ""

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "where": self.where,
            "message": self.message,
            "hint": self.hint,
        }


# ---------------------------------------------------------------------------
# L2 格式化（black）显式化 —— v8 §3.4 / S-8 / T13
# ---------------------------------------------------------------------------

# 为什么需要：`make.format_pytest_with_black` 的 docstring 明写「超时/失败都**只告警**、
# 不阻断 hmake」，于是「L2 通过」既可能是「格式化成功」，也可能是「black 根本没跑」。
# 两者的**正确性相同、风险不同**：后者意味着"未格式化的产物照样落盘"，
# 而「L2 通过率」这个指标会因此含噪声（§0.3-⑦）。
#
# `reason` 取值（前四个是 v8 §3.4 的原规格，后三个是实现补充）：
#   ok / timeout / failed / missing_black —— black 的四种**执行结局**；
#   no_files   —— 没有产物可格式化（**不是**"没装 black"）；
#   not_probed —— 调用方没探针（报告里始终有该键，消费者不必判空）；
#   unknown    —— 传入了无法识别的状态（降级留痕，不静默当已知）。
#
# NOTICE（口径钉死，写进文档并配自检）：`ran` = 「black 进程**被成功启动**」。
# 于是 `missing_black` 是唯一 `ran=False` 的**执行结局**；
# `no_files` / `not_probed` / `unknown` 属「根本没走到 black」——
# 它们与 `missing_black` 的区别正是运维动作不同：**该装依赖** vs **该查调用链**。
FORMATTING_OK: Text = "ok"
FORMATTING_TIMEOUT: Text = "timeout"
FORMATTING_FAILED: Text = "failed"
FORMATTING_MISSING_BLACK: Text = "missing_black"
FORMATTING_NO_FILES: Text = "no_files"
FORMATTING_NOT_PROBED: Text = "not_probed"
FORMATTING_UNKNOWN: Text = "unknown"

FORMATTING_REASONS: Tuple[Text, ...] = (
    FORMATTING_OK,
    FORMATTING_TIMEOUT,
    FORMATTING_FAILED,
    FORMATTING_MISSING_BLACK,
    FORMATTING_NO_FILES,
    FORMATTING_NOT_PROBED,
    FORMATTING_UNKNOWN,
)

# 「没有到 black 那一步」的三种取值（与"执行结局"的区分见上方 NOTICE）
FORMATTING_NOT_EXECUTED_REASONS = frozenset(
    {FORMATTING_NO_FILES, FORMATTING_NOT_PROBED, FORMATTING_UNKNOWN}
)


def _formatting_status(ran: bool, reason: Text) -> Dict[Text, Any]:
    """构造规范化后的格式化状态（唯一构造点，避免各处手写键名）。"""
    return {"ran": ran, "reason": reason}


def not_probed_formatting() -> Dict[Text, Any]:
    """未被探针时的默认值。"""
    return _formatting_status(False, FORMATTING_NOT_PROBED)


def normalize_formatting(status: Any) -> Dict[Text, Any]:
    """把外部传入的格式化状态归一化；非法 → `unknown`（不猜、不崩）。

    与 `diagnosis.normalize_diagnosis` 同一取向：**降级要留痕**——
    静默把非法输入当成 `ok`，等于让"L2 通过"这个信号再次退化成噪声。
    """
    if not isinstance(status, dict):
        return _formatting_status(False, FORMATTING_UNKNOWN)

    reason = status.get("reason")
    if reason not in FORMATTING_REASONS:
        return _formatting_status(False, FORMATTING_UNKNOWN)

    ran = status.get("ran")
    if not isinstance(ran, bool):
        # `ran` 缺失/非布尔 → 由 reason 推（唯一 `ran=False` 的执行结局是 missing_black）
        ran = (
            reason not in FORMATTING_NOT_EXECUTED_REASONS
            and reason != FORMATTING_MISSING_BLACK
        )
    return _formatting_status(bool(ran), reason)


@dataclass
class GateReport:
    findings: List[Finding] = field(default_factory=list)
    # L2 格式化（black）结论（v8 §3.4 / S-8 / T13）。
    #
    # NOTICE（为什么闸门不自己填）：`make.run_black` 是**写盘**操作（black 就地重写文件），
    # 而闸门的纪律是**零副作用**（S-6 落地注记）。所以状态必须由调用方**显式**探针后
    # 经 `with_formatting()` 挂上；未挂 → 报告里是 `not_probed`
    # （"没人问过"，**不是**"问过且没跑"——两者在报告里必须可分）。
    formatting: Optional[Dict[Text, Any]] = None

    @property
    def rejects(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == REJECT]

    @property
    def pendings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == PENDING]

    @property
    def ok(self) -> bool:
        return not self.rejects

    def codes(self) -> List[Text]:
        return sorted({f.code for f in self.findings})

    def formatting_status(self) -> Dict[Text, Any]:
        """报告里的 `formatting` 取值（未被探针 → `not_probed`）。"""
        if self.formatting is None:
            return not_probed_formatting()
        return normalize_formatting(self.formatting)

    def with_formatting(self, status: Any) -> "GateReport":
        """把 L2 的格式化结论挂到报告上（返回自身，便于链式调用）。

        传入非法状态不会抛：会归一化成 `unknown` 并在报告里留下痕迹
        （见 `normalize_formatting`）——闸门不该因为"报告字段填错"而中断生成。
        """
        self.formatting = normalize_formatting(status)
        return self

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "ok": self.ok,
            "rejects": len(self.rejects),
            "pendings": len(self.pendings),
            "codes": self.codes(),
            "findings": [f.to_dict() for f in self.findings],
            "formatting": self.formatting_status(),
        }


class SilentTrapRejected(Exception):
    """闸门拦下了产物（硬拒）。

    NOTICE: 与内核 `bench/ai_guardrails.py::L3Refused` 同一取向——
    这**必须**是异常而不是"跳过"。静默降级会让用户以为"校验过了"，
    而实际上产物带着已知缺陷落盘，正是本仓最忌讳的假通过形态。
    """

    def __init__(self, report: GateReport):
        self.report = report
        detail = "\n".join(
            f"  - [{f.code}] {f.where}\n      {f.message}"
            + (f"\n      {f.hint}" if f.hint else "")
            for f in report.rejects
        )
        super().__init__(
            f"产物被**静默陷阱闸门**拦下（{len(report.rejects)} 条）：\n{detail}\n"
            f"  说明：这些都是「会静默假通过」的可精确判定形态，"
            f"不修就会产出「跑了但没校验」的用例。"
        )


# ---------------------------------------------------------------------------
# 复用内核护栏（不重造）
# ---------------------------------------------------------------------------


def _kernel():
    """按需导入内核（让本模块在"内核没装"的环境里仍可被静态检查）。"""
    from interfacetester import exceptions  # noqa: PLC0415
    from interfacetester import make  # noqa: PLC0415
    from interfacetester import response  # noqa: PLC0415

    return exceptions, make, response


def kernel_comparator_names() -> Tuple[Text, ...]:
    """内核内置算子名（唯一来源：`make.BUILTIN_COMPARATOR_NAMES`）。"""
    _, make, _ = _kernel()
    return tuple(make.BUILTIN_COMPARATOR_NAMES)


def probe_formatting(*python_paths: Text) -> Dict[Text, Any]:
    """**显式**跑一次 black，返回 `{"ran": bool, "reason": str}`。

    NOTICE（为什么必须显式、不自动）：它会**写盘**（black 就地重写文件），
    所以刻意不做成"跑闸门时顺手探针"——那会给闸门带上副作用（S-6 口径）。
    调用方（生成闭环 / 故障诊断）在 L2 之后主动调它，再用
    `GateReport.with_formatting` 把结论挂进报告。

    复用内核 `make.run_black` / `make.get_black_timeout`（**不重造**），
    四条异常分支与 `make.format_pytest_with_black` 的 except 分支**逐条对应**：
    `TimeoutExpired` → timeout、`CalledProcessError` → failed、`OSError` → missing_black。
    """
    if not python_paths:
        return _formatting_status(False, FORMATTING_NO_FILES)

    _, make, _ = _kernel()
    try:
        make.run_black(make.get_black_timeout(), *python_paths)
    except subprocess.TimeoutExpired:
        return _formatting_status(True, FORMATTING_TIMEOUT)
    except subprocess.CalledProcessError:
        return _formatting_status(True, FORMATTING_FAILED)
    except OSError:
        return _formatting_status(False, FORMATTING_MISSING_BLACK)
    return _formatting_status(True, FORMATTING_OK)


# ---------------------------------------------------------------------------
# 判定辅助
# ---------------------------------------------------------------------------

# 数值形态的字符串：`100` / `9` / `-3.14` / `3.0`
# 刻意**不**匹配 `2026-09-20`（日期串）、`1.2.3`（语义版本）、`v1.2`——
# 那些的字典序恰好等于时间序/版本序，是**合法**用法（内核已有同样说明）。
_NUMERIC_STRING_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

# 函数参数带引号：`${f('a')}` / `${f("a")}`
_QUOTED_FUNC_RE = re.compile(r"\$\{[a-zA-Z_]\w*\([^}]*['\"]")

# 响应取值表达式的裸前缀（与内核 `response._leading_identifier` 同口径）
_PATH_HEAD_RE = re.compile(r"[A-Za-z_]\w*")

# `type_match` 允许的类型名（与内核 `type_match.get_type` 的白名单一致）
_TYPE_NAMES = frozenset(
    {"int", "str", "list", "dict", "float", "bool", "set", "tuple", "bytes", "None", "NoneType"}
)
_TYPE_OBJECTS = (int, str, list, dict, float, bool, set, tuple, bytes, type(None))


def _is_numeric_operator(name: Text) -> bool:
    return name in (
        "greater_than",
        "less_than",
        "greater_or_equals",
        "less_or_equals",
    )


def _is_path_expression(expr: Any) -> bool:
    """是不是「前缀 + 路径/下标」形式（裸前缀 `body` 不算）——与内核同口径。"""
    if not isinstance(expr, str):
        return False
    m = _PATH_HEAD_RE.match(expr)
    if not m:
        return False
    return expr != m.group(0)


def _is_empty_value(value: Any) -> bool:
    """空值真空：`""` / `[]` / `{}` / 纯空白串。"""
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _iter_strings(node: Any):
    """递归遍历出全部字符串（含 dict 的键与值）。"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(node, (list, tuple, set)):
        for item in node:
            yield from _iter_strings(item)


def _schema_is_vacuous(schema: Any) -> bool:
    """schema 是不是「空真空」——通过一切输入，等于没校验。

    实测：`jsonschema_match(None, {})` 与 `jsonschema_match("x", {})` **都通过**。
    `{"type": "object"}` 之类则有实际约束，不算真空。
    """
    if isinstance(schema, dict):
        return len(schema) == 0
    return False


# ---------------------------------------------------------------------------
# 各条闸门
# ---------------------------------------------------------------------------


def _normalize_validators(validators: Any, where: Text, report: GateReport) -> List[Dict]:
    """把 validate 的两种写法归一（复用内核 `uniform_validator`）。"""
    _, _, response = _kernel()
    out: List[Dict] = []
    if validators is None:
        return out
    if not isinstance(validators, list):
        report.findings.append(
            Finding(
                "S3",
                REJECT,
                where,
                f"`validate` 必须是列表，实际是 {type(validators).__name__}",
                "改法：`validate: [ {eq: [status_code, 200]} ]`",
            )
        )
        return out
    for idx, v in enumerate(validators):
        try:
            u = response.uniform_validator(v)
        except Exception as ex:  # noqa: BLE001 - 内核对非法 validator 抛 ParamsError
            report.findings.append(
                Finding(
                    "S6",
                    REJECT,
                    f"{where}.validate[{idx}]",
                    f"validator 形态非法：{type(ex).__name__}: {ex}",
                    "改法：用 `{算子: [check, expect]}` 或 "
                    "`{check:…, comparator:…, expect:…}`，且长度必须是 2 或 3。",
                )
            )
            continue
        out.append(u)
    return out


def gate_s1_pseudo_presence(
    validators: List[Dict], where: Text, report: GateReport
) -> None:
    """S1：伪存在性断言（空值真空）——**最高危**。

    实测（comparator 级 + 端到端双层复现）：`not_equal: [body.data.nonexistent_field, ""]`
    在字段**完全不存在**时 `check_result=pass`、整用例 `success=True`、`hrun` 报 `1 passed`。

    为什么生成期拦得住：运行期拿到的是 `None`，歧义已无法消除；
    但生成期看得到**期望值是空值**这个事实，也就看得到"意图是存在且非空"。
    """
    for u in validators:
        name, check, expect = u["assert"], u["check"], u["expect"]

        # 1) `not_equal: [<路径>, <空值>]` —— 最常见也最危险的写法
        if name == "not_equal" and _is_empty_value(expect) and _is_path_expression(check):
            report.findings.append(
                Finding(
                    "S1",
                    REJECT,
                    where,
                    f"伪存在性断言：`not_equal: [{check!r}, {expect!r}]`。\n"
                    f"      它想表达「字段存在且非空」，但字段**不存在**时 jmespath 返回 None，\n"
                    f"      而 `None != ''` 为真 → 断言**静默通过**（实测：整用例判 success=true）。",
                    "改法：存在性用可判定形态 —— "
                    f'`type_match: [{check}, "str"]`（缺字段会判失败）；'
                    "若还要非空，再补 `length_greater_than` 或 `regex_match`。",
                )
            )

        # 2) `contains: [<路径>, ""]` —— 空串是任何字符串的子串，真空
        elif name == "contains" and _is_empty_value(expect):
            report.findings.append(
                Finding(
                    "S1",
                    REJECT,
                    where,
                    f"空串真空断言：`contains: [{check!r}, {expect!r}]`。\n"
                    f"      空串是**任何**字符串的子串，这条断言恒为真——等于没校验。",
                    "改法：给出要匹配的实际子串；"
                    "或改用 `regex_match` / `type_match` 表达真实意图。",
                )
            )

        # 3) `jsonschema_match: [<路径>, {}]` —— 空 schema 通过一切
        elif name == "jsonschema_match" and _schema_is_vacuous(expect):
            report.findings.append(
                Finding(
                    "S1",
                    REJECT,
                    where,
                    f"空 schema 真空断言：`jsonschema_match: [{check!r}, {expect!r}]`。\n"
                    f"      实测 `jsonschema_match(None, {{}})` 与 `(\"x\", {{}})` **都通过**"
                    f"——空 schema 不约束任何东西，契约断言静默退化。",
                    '改法：给出真实 schema（至少 `{"type": "object"}`），'
                    "或改为引用 schema 文件路径。",
                )
            )


def gate_s2_lexicographic(
    validators: List[Dict], where: Text, report: GateReport
) -> None:
    """S2：数值算子两侧都是字符串 → **字典序**比较。

    实测：`greater_than('9','100')` 判**通过**（`'9' > '1'`），而数值上 9 < 100。
    内核只**告警**（`_warn_if_both_operands_are_strings`），因为字典序对
    ISO 日期串是合法用法；所以本闸门只拦**像数字**的字符串，放行日期/版本串。
    """
    for u in validators:
        name, expect = u["assert"], u["expect"]
        if not _is_numeric_operator(name):
            continue
        if isinstance(expect, str) and _NUMERIC_STRING_RE.match(expect.strip()):
            report.findings.append(
                Finding(
                    "S2",
                    REJECT,
                    where,
                    f"数值算子 `{name}` 的期望值是**字符串数字** {expect!r}。\n"
                    f"      实测 `{name}('9', '100')` 判**通过**（字典序 '9' > '1'），"
                    f"而数值上 9 < 100 —— 接口把数字返回成字符串时这类断言**静默假通过**。",
                    f"改法：期望值写成数字字面量（`{expect.strip()}` 而不是 `\"{expect.strip()}\"`），"
                    "并确认 check 侧取到的也是数字（可用 `${int($var)}`）。",
                )
            )


def _extract_names(extracts: Any) -> List[Text]:
    """归一 extract 的键名（dict / list-of-dict 两种写法都支持）。"""
    names: List[Text] = []
    if isinstance(extracts, dict):
        names = list(extracts.keys())
    elif isinstance(extracts, list):
        for item in extracts:
            if isinstance(item, dict):
                names.extend(item.keys())
    return names


def gate_s3_assertion_vacuum(
    teststeps: Any, testcase: Dict, config: Dict, where: Text, report: GateReport
) -> None:
    """S3：断言真空——用例"跑绿"却**什么都没校验**（三态判定，§3.3 / T1~T3）。

    源码依据（多处，同一族）：
      - `client.py`：「**没有断言的 step 还会被判成功**（最坏的一种"假通过"）」
      - `har_adapter.py`：「`assertion_mode != "none"` 却**一条断言都没有** → 不能静默」
      - `make.py`：「空 `teststeps` 必须报错，不能生成一个「零步骤但判成功」的用例」

    三态判定（v6 定三态、v7 扩展、v8 T1 落地；实测依据 probe_s3_review/probe_s3_edge.py）：
      ① `validate` **缺失**（key 不存在或 None）：
         - 无 `extract` → **REJECT**（既不断言也不产出，无论返回什么都判成功）；
         - 有 `extract` → 交给 S4 做"产出有没有被用上"的覆盖判断，S3 不报
           （多步链路的正常中间步骤，其价值由下游承担，§3.3 表第 2/3 行）；
      ② `validate` **显式为空列表** → **PENDING**，如实标注"该步骤不做断言"
         （显式声明是有效的设计意图；旧实现 `not validates` 把②压成①，
         正是"提示的改法自己不可行"的自相矛盾根源，§12.7 甲2）；
      ③ `validate` 非空 → 通过，计为"本用例有真断言"。

    NOTICE（边界）：只对**请求类** step 要求断言。
    `testcase:` 引用型 step 是"调用另一个用例"，它自己不写 validate 是**合法**的。
    """
    if not isinstance(teststeps, list) or not teststeps:
        # 空/缺失 teststeps 已由内核 `ensure_generatable_teststeps` 硬拒，这里不重复
        return

    # `config.skip` 是显式的"暂时不跑"，如实上报为 skipped，不算真空
    if isinstance(config, dict) and config.get("skip"):
        return

    has_real_assertion = False
    has_explicit_empty = False
    for idx, step in enumerate(teststeps):
        if not isinstance(step, dict):
            continue
        step_where = f"{where}.teststeps[{idx}]"
        name = step.get("name", "")
        validates = step.get("validate")

        # 引用型 step：不要求自带断言
        if step.get("testcase") and not step.get("request"):
            continue

        if validates:  # 三态之③：有断言
            has_real_assertion = True
        elif validates == []:  # 三态之②：显式为空 → PENDING，如实标注
            has_explicit_empty = True
            report.findings.append(
                Finding(
                    "S3",
                    PENDING,
                    step_where,
                    f"请求步骤 {name!r} 显式声明**不做断言**（`validate: []`）。\n"
                    f"      这是有效的设计意图，已如实标注；"
                    f"该步骤无论接口返回什么都判成功。",
                    "若属疏漏，请补一条可判定断言（如 `eq: [status_code, 200]`）。",
                )
            )
        elif not step.get("request"):
            # 既非 request 也非 testcase 的畸形步骤交给 S9（可生成性）判
            continue
        elif not _extract_names(step.get("extract")):
            # 三态之①a：validate 缺失且无 extract → REJECT
            report.findings.append(
                Finding(
                    "S3",
                    REJECT,
                    step_where,
                    f"请求步骤 {name!r} **没有任何断言**，也没有 `extract`。\n"
                    f"      这类步骤无论接口返回什么都会被判成功——"
                    f"源码注释称其为「最坏的一种假通过」。",
                    "改法：至少补一条能判定的断言（通常是 `eq: [status_code, 200]`）；"
                    "该步骤只产出中间值时，请确认其 `extract` 的键被下游使用；"
                    "确实只想「跑一下」时，显式写 `validate: []` 声明意图"
                    "（闸门将如实标注 PENDING，不再硬拒）。",
                )
            )
        # 三态之①b：validate 缺失但有 extract → 交给 S4（覆盖判断），S3 不报

    # 整例级：所有请求步骤都零断言、连一处显式声明都没有、也无引用型 step 分担
    if (
        not has_real_assertion
        and not has_explicit_empty
        and not any(isinstance(s, dict) and s.get("testcase") for s in teststeps)
    ):
        report.findings.append(
            Finding(
                "S3",
                REJECT,
                where,
                "整份用例**零断言**：所有请求步骤都没有 `validate`。\n"
                "      它会「一路绿到底」，但什么都没测。",
                "改法：补断言；若这个用例暂时不想跑，请写 `config.skip`（如实上报为 skipped）。",
            )
        )


def _iter_request_strings(node: Any):
    """递归收集 request 子树里的全部字符串（`$var` 可能出现在任意模板位置）。"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _iter_request_strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _iter_request_strings(v)


def gate_s4_extract_without_assertion(
    teststeps: Any, where: Text, report: GateReport
) -> None:
    """S4：`extract` 取到的值没被校验过，也没被任何下游步骤使用。

    源码依据：`response.py`：「取值未命中（None）不能静默——
    **后续步骤会拿到 None 而毫无提示**」。

    「被使用」的判据（v7 扩展到跨步 + v8 T21 词边界加严，§3.3 表第 2/3 行）：
      a. 该 extract 的**取值路径**出现在**任何步骤**断言的 `check` 里（精确相等）；
      b. 变量名作为某个 `check` 的**完整路径段**（按 `.` 分段后逐段相等）；
      c. `$var` / `${var}` 以**词边界**出现在**任何步骤** request 子树的字符串里
         （headers / params / body / url 等模板位置；`$token` 后紧跟标识符字符
         视为**另一个变量名**（如 `$token_id`），不算 `token` 被使用）。
    ★v8（T21）：旧实现 `any(n in c for c in checked)` 是**子串匹配**——extract 键
    `token` 会被 check `body.data.token_type` 误判"已引用"而**静默漏报**（实测见
    方案 v8 §12.8-③）；上述 b/c 两支改为精确/词边界匹配后，漏报对抗样本
    「token vs token_type」恢复报出 PENDING。

    只报 PENDING 不硬拒：多步链路里确实存在"故意容忍 None"的权衡，
    这属于需要人来确认的语义，不该由闸门替用户下结论。
    """
    if not isinstance(teststeps, list):
        return

    # 先收集全用例的 check 集合与 request 模板文本（跨步引用是 v7 扩展的核心）
    checks: set = set()
    request_texts: List[Text] = []
    for step in teststeps:
        if not isinstance(step, dict):
            continue
        validators = _normalize_validators(
            step.get("validate"), f"{where}.teststeps[?]", GateReport()
        )
        checks.update(
            str(u["check"]) for u in validators if isinstance(u.get("check"), str)
        )
        request_texts.extend(_iter_request_strings(step.get("request")))

    for idx, step in enumerate(teststeps):
        if not isinstance(step, dict):
            continue
        names = _extract_names(step.get("extract"))
        if not names:
            continue
        # 归一 extract 的键 → 取值路径（dict 或 list-of-dict 两种写法都支持）
        path_of: Dict[Text, Optional[Text]] = {}
        extracts = step.get("extract")
        if isinstance(extracts, dict):
            path_of = {n: extracts.get(n) for n in names}
        elif isinstance(extracts, list):
            for item in extracts:
                if isinstance(item, dict):
                    for n in names:
                        if n in item:
                            path_of[n] = item[n]

        for n in names:
            path = path_of.get(n)
            # a. 取值路径精确出现在某条断言的 check 里
            used = isinstance(path, str) and path in checks
            # b. 变量名是某个 check 的完整路径段（非子串！）
            if not used:
                used = any(seg == n for c in checks for seg in str(c).split("."))
            # c. $var / ${var} 以词边界出现在任何 request 字符串里
            if not used and request_texts:
                var_re = re.compile(
                    r"\$\{" + re.escape(n) + r"\}|\$" + re.escape(n) + r"(?![A-Za-z0-9_])"
                )
                used = any(var_re.search(t) for t in request_texts)
            if not used:
                report.findings.append(
                    Finding(
                        "S4",
                        PENDING,
                        f"{where}.teststeps[{idx}]",
                        f"`extract` 提取的 {n!r} 没有被任何断言校验，"
                        f"也没有被任何 `$` 变量使用。\n"
                        f"      它取不到值时是 None，后续 `$" + n + "` 会**静默拿到 None**。",
                        f'改法：补一条可判定的断言，如 `type_match: [{path or n}, "str"]`；'
                        f"或让下游步骤使用 `$" + n + "`（headers/params/body/url 均可）；"
                        f"若确实容忍缺失，请在此处留注释说明。",
                    )
                )


def gate_s5_quoted_function(
    node: Any, where: Text, report: GateReport
) -> None:
    """S5：`${f('abc')}` 参数带引号 → **不被当作函数调用**，且只告警不报错。

    实测：`function_regex_compile` 的参数集 `[$\\w\\.\\-/\\s=,]` **不含引号**，
    于是 `${f('abc')}` 的 `findall` 为空（退化成字面量），只有一条 warning。
    """
    for s in _iter_strings(node):
        if _QUOTED_FUNC_RE.search(s):
            report.findings.append(
                Finding(
                    "S5",
                    REJECT,
                    where,
                    f"函数调用的参数**带了引号**：{s!r}\n"
                    f"      解析器的参数字符集不含引号，这段不会被当作函数执行，"
                    f"而是**原样保留成字符串**（不报错）——静默产出垃圾值。",
                    "改法：参数写**裸词**（`${f(abc)}` 等价于 `f('abc')`），"
                    "或把字符串放进 `config.variables` 后用 `$var` 传入。",
                )
            )


def gate_s6_unknown_comparators(
    teststeps: Any, where: Text, report: GateReport, functions_mapping: Optional[Dict] = None
) -> None:
    """S6：算子名白名单——**复用内核** `ensure_known_comparators`（不重造）。

    源码依据：`make.py`：「拼错的名字恰好等于某个内置名」= 静默假通过
    （实测 `validate: - print: [...]` 判 pass，因为算子返回值被忽略且 print 可变参）。
    """
    exceptions, make, _ = _kernel()
    try:
        make.ensure_known_comparators(
            teststeps, functions_mapping=functions_mapping or {}, testcase_path=where
        )
    except exceptions.MyBaseError as ex:
        report.findings.append(
            Finding(
                "S6",
                REJECT,
                where,
                f"算子白名单未通过：{ex}",
                "改法：算子名必须 ∈ 内置算子或项目 debugtalk.py 里定义的函数。",
            )
        )


def gate_s7_type_match_expect(
    validators: List[Dict], where: Text, report: GateReport
) -> None:
    """S7：`type_match` 的期望值必须是**类型名**。

    源码依据：`comparators.type_match.get_type()`：修复前用 `__builtins__[name]`，
    把**任意内置名**当类型，于是 `type_match: [body.x, eval]` 表面是"类型断言失败"、
    实际是用法错误（还意味着算子能触达整个内置命名空间）。
    """
    for u in validators:
        if u["assert"] != "type_match":
            continue
        expect = u["expect"]
        if isinstance(expect, type):
            continue
        if expect in (None, "None", "NoneType"):
            continue
        if isinstance(expect, str) and expect in _TYPE_NAMES:
            continue
        report.findings.append(
            Finding(
                "S7",
                REJECT,
                where,
                f"`type_match` 的期望值 {expect!r} 不是合法类型名。",
                '改法：只能是类型对象或内置类型名（"int" / "str" / "list" / "dict" / '
                '"float" / "bool" / "set" / "tuple" / "bytes" / "None" / "NoneType"）。',
            )
        )


def gate_s8_inline_schema(
    teststeps: Any, where: Text, report: GateReport
) -> None:
    """S8：内联 JSON Schema 含 `$` 键——**复用内核** `ensure_json_schema_not_inline`。

    源码依据：`parser.parse_data` 会解析 dict 的**键**，`$schema`/`$ref` →
    `VariableNotFound`（而且报错点远离本行，很难定位）。
    """
    exceptions, make, _ = _kernel()
    try:
        make.ensure_json_schema_not_inline(teststeps, testcase_path=where)
    except exceptions.MyBaseError as ex:
        report.findings.append(
            Finding(
                "S8",
                REJECT,
                where,
                f"内联 schema 未通过：{ex}",
                "改法：把 schema 放进文件并对路径引用，或改成不含 `$` 键的最简 schema。",
            )
        )


def gate_s9_generatable_and_body(
    teststeps: Any, where: Text, report: GateReport
) -> None:
    """S9：请求体并存 / 生成器不支持字段——**复用内核** `ensure_generatable_teststeps`。

    源码依据（`make.py` 表格）：
      | `upload` + `data`(dict) | 只有 `upload` 里的字段 | `data` 整段**静默消失** |
      | `data` + `json`        | 整个 json 分支不执行   | `json` 被**静默丢弃** |
    另有 `validate_script` / `sql_request` / `thrift_request` 三个
    「模型认、生成器不认」的字段（历史实例：`examples/httpbin/validate.yml`
    里故意要失败的断言凭空消失 → 用例假通过）。
    """
    exceptions, make, _ = _kernel()
    try:
        make.ensure_generatable_teststeps(teststeps, testcase_path=where)
    except exceptions.MyBaseError as ex:
        report.findings.append(
            Finding(
                "S9",
                REJECT,
                where,
                f"生成器可生成性未通过：{ex}",
                "改法：见报错正文（内核已给出逐字段的改法与可用字段清单）。",
            )
        )


def gate_s10_module_collision(
    cases: List[Tuple[Text, Dict]], report: GateReport
) -> None:
    """S10：归一化后撞同一个生成物模块名 / YAML 名。

    源码依据：`make.py`：「两个不同的 YAML 归一化后落到同一个 `_test.py`。
    修复前这种碰撞是**静默**的」（实测 `hmake`/`hrun` 全绿，但"call A"一步都没跑）。
    `cli.py::main_convert` 已有三层写盘护栏，这里在**批量生成**时前置同一判据。
    """
    _, make, _ = _kernel()
    seen: Dict[Text, Text] = {}
    for name, _case in cases:
        # 与内核 `cli.main_convert` 第三层护栏同口径
        module_seg = make.normalize_module_segment(name)
        if module_seg in seen and seen[module_seg] != name:
            report.findings.append(
                Finding(
                    "S10",
                    REJECT,
                    f"case:{name}",
                    f"用例名 {name!r} 与 {seen[module_seg]!r} 归一化后落到**同一个模块段** "
                    f"{module_seg!r} → 会覆盖同一个 `*_test.py`（静默覆盖）。",
                    "改法：给其中一个用例改名，使归一化后的模块段互不相同。",
                )
            )
        else:
            seen[module_seg] = name


def gate_s11_document_encoding(path: Text, report: GateReport) -> Optional[Text]:
    """S11：文档编码探测（BOM / GBK / CRLF）——复用导入器的探测顺序。

    源码依据：`cli.py` M23：「BOM 会让第一行变成 `\\ufeffcurl …`，
    于是**一条都解析不出来**（且原来的报错完全看不出原因）」。
    本仓历史取证目录有成对样例（`curl_bom.txt` / `curl_gbk.txt` / `har_gbk.json` /
    `openapi_bom.yaml`），说明这是长期存在的第一公里问题。

    返回探测到的编码；不可读时记 finding 并返回 None。
    """
    if not os.path.isfile(path):
        report.findings.append(
            Finding("S11", REJECT, f"doc:{path}", f"文档不存在：{path}", "改法：检查路径。")
        )
        return None

    with open(path, "rb") as fp:
        raw = fp.read()

    if not raw:
        report.findings.append(
            Finding(
                "S11",
                REJECT,
                f"doc:{path}",
                "文档是 0 字节——从它生成不出任何用例（而「空输入」极易被误当成「没有接口」）。",
                "改法：确认导出/拷贝是否完整。",
            )
        )
        return None

    # 与导入器同一套探测顺序
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue

    report.findings.append(
        Finding(
            "S11",
            REJECT,
            f"doc:{path}",
            "文档编码无法识别（utf-8-sig / utf-8 / gbk / gb18030 全部失败）。",
            "改法：转成 UTF-8 后重试；**不要**让它静默产出空用例。",
        )
    )
    return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def check_testcase(
    testcase: Dict,
    path: Text = "<inline>",
    functions_mapping: Optional[Dict] = None,
) -> GateReport:
    """对一份**用例字典**（已 load 或 LLM 装配产物）跑全部生成期闸门。

    Args:
        testcase: `{"config": {...}, "teststeps": [...]}`
        path: 仅用于报错定位
        functions_mapping: 项目 debugtalk.py 的函数表（自定义算子白名单）

    Returns:
        GateReport（`ok=False` 表示有 REJECT）
    """
    report = GateReport()
    if not isinstance(testcase, dict):
        report.findings.append(
            Finding("S0", REJECT, path, f"用例必须是字典，实际是 {type(testcase).__name__}")
        )
        return report

    config = testcase.get("config") or {}
    teststeps = testcase.get("teststeps")

    # 复用内核护栏（S8/S9/S6）
    gate_s9_generatable_and_body(teststeps, path, report)
    gate_s8_inline_schema(teststeps, path, report)
    gate_s6_unknown_comparators(teststeps, path, report, functions_mapping)

    # 新写的意图级闸门（S1/S2/S7/S5/S4/S3）
    if isinstance(teststeps, list):
        for idx, step in enumerate(teststeps):
            if not isinstance(step, dict):
                continue
            step_where = f"{path}.teststeps[{idx}]({step.get('name', '')})"
            validators = _normalize_validators(step.get("validate"), step_where, report)
            gate_s1_pseudo_presence(validators, step_where, report)
            gate_s2_lexicographic(validators, step_where, report)
            gate_s7_type_match_expect(validators, step_where, report)
            gate_s5_quoted_function(step, step_where, report)

    gate_s4_extract_without_assertion(teststeps, path, report)
    gate_s3_assertion_vacuum(teststeps, testcase, config, path, report)
    return report


def assert_testcase_passes(
    testcase: Dict,
    path: Text = "<inline>",
    functions_mapping: Optional[Dict] = None,
) -> GateReport:
    """`check_testcase` 的抛异常版本（供 gen/fix 流程直接调用）。"""
    report = check_testcase(testcase, path=path, functions_mapping=functions_mapping)
    if not report.ok:
        raise SilentTrapRejected(report)
    return report


# ---------------------------------------------------------------------------
# 自检：MUST_REJECT（拦得住）+ MUST_ALLOW（不误伤）
# ---------------------------------------------------------------------------

def _case(steps: List[Dict], config: Optional[Dict] = None) -> Dict:
    return {
        "config": config or {"name": "t", "base_url": "http://127.0.0.1:1", "verify": False},
        "teststeps": steps,
    }


def _req(name: str, validate: Any, **extra) -> Dict:
    step = {
        "name": name,
        "request": {"method": "GET", "url": "/a"},
        "validate": validate,
    }
    step.update(extra)
    return step


# 必须**拦下**（每一对都是"意图与实现不符"的可判定形态）
MUST_REJECT: List[Tuple[Text, Text, Dict]] = [
    (
        "S1",
        "伪存在性断言 not_equal/空串",
        _case([_req("s", [{"not_equal": ["body.data.token", ""]}])]),
    ),
    (
        "S1",
        "伪存在性断言 contains/空串",
        _case([_req("s", [{"contains": ["body.name", ""]}])]),
    ),
    (
        "S1",
        "空 schema 真空",
        _case([_req("s", [{"jsonschema_match": ["body", {}]}])]),
    ),
    (
        "S2",
        "数值算子比字符串数字",
        _case([_req("s", [{"greater_than": ["body.price", "100"]}])]),
    ),
    (
        "S3",
        "请求步骤零断言",
        _case([_req("s", None)]),
    ),
    (
        "S5",
        "函数参数带引号",
        _case([_req("s", [{"eq": ["status_code", 200]}], variables={"v": "${f('abc')}"})]),
    ),
    (
        "S7",
        "type_match 期望值不是类型名",
        _case([_req("s", [{"type_match": ["body.x", "notatype"]}])]),
    ),
    (
        "S6",
        "算子名拼错（撞内置名 print）",
        _case([_req("s", [{"print": ["status_code", 200]}])]),
    ),
    (
        "S9",
        "data 与 json 并存（静默丢体）",
        _case(
            [
                {
                    "name": "s",
                    "request": {"method": "POST", "url": "/a", "data": {"a": 1}, "json": {"b": 2}},
                    "validate": [{"eq": ["status_code", 200]}],
                }
            ]
        ),
    ),
    (
        "S9",
        "validate_script（生成器不支持的字段）",
        _case([_req("s", [{"eq": ["status_code", 200]}], validate_script="assert 1==1")]),
    ),
    (
        "S8",
        "内联 schema 含 $ 键",
        _case(
            [
                _req(
                    "s",
                    [{"jsonschema_match": ["body", {"$schema": "http://x", "type": "object"}]}],
                )
            ]
        ),
    ),
]

# 必须**放行**（内核明示的合法写法 —— 误伤它们就是"假报"，比漏拦更严重）
MUST_ALLOW: List[Tuple[Text, Text, Dict]] = [
    ("R1", "eq 明确期望 null", _case([_req("s", [{"eq": ["body.x", None]}])])),
    ("R2", "type_match 明确期望 None 类型", _case([_req("s", [{"type_match": ["body.x", "None"]}])])),
    ("R3", "数值算子比 ISO 日期串（字典序=时间序，合法）", _case([_req("s", [{"greater_than": ["body.date", "2026-01-01"]}])])),
    ("R3b", "数值算子比版本串", _case([_req("s", [{"greater_than": ["body.ver", "1.2.3"]}])])),
    ("R4", "string_equals 比数字串（仓库与现场都在用）", _case([_req("s", [{"string_equals": ["status_code", "200"]}])])),
    ("R5", "type_match 合法类型名", _case([_req("s", [{"type_match": ["body.data.token", "str"]}])])),
    ("S1-ok", "真正的存在性断言写法", _case([_req("s", [{"type_match": ["body.token", "str"]}])])),
    ("S2-ok", "数值算子比数字字面量", _case([_req("s", [{"greater_than": ["body.price", 100]}])])),
    ("S4-ok", "extract 且对该字段有断言", _case([_req("s", [{"type_match": ["body.data.token", "str"]}], extract={"token": "body.data.token"})])),
    ("S3-ok", "引用型 step 不必自带断言", _case([{"name": "call", "testcase": "child.yml"}])),
    ("S3-ok2", "config.skip 显式声明不跑", _case([_req("s", None)], config={"name": "t", "skip": "暂不跑"})),
    ("S9-ok", "正常 POST json", _case([{"name": "s", "request": {"method": "POST", "url": "/a", "json": {"b": 2}}, "validate": [{"eq": ["status_code", 200]}]}])),
    ("S5-ok", "函数参数裸词（合法）", _case([_req("s", [{"eq": ["status_code", 200]}], variables={"v": "${f(abc)}"})])),
    ("S6-ok", "算子别名 eq（合法）", _case([_req("s", [{"eq": ["status_code", 200]}])])),
    ("S8-ok", "不含 $ 键的内联 schema（合法）", _case([_req("s", [{"jsonschema_match": ["body", {"type": "object", "required": ["code"]}]}])])),
    # ★v8（T2）：S3 三态 + S4 词边界引用判定的 2 条 MUST_ALLOW（§3.3 表第 2/4 行）
    (
        "S3-ok3",
        "登录取 token、下游 header 用 $token（T21 判据①：通过且零 PENDING）",
        _case(
            [
                {
                    "name": "login",
                    "request": {"method": "POST", "url": "/login", "json": {"u": 1}},
                    "extract": {"token": "body.data.token"},
                },
                {
                    "name": "use",
                    "request": {
                        "method": "GET",
                        "url": "/me",
                        "headers": {"Authorization": "Bearer $token"},
                    },
                    "validate": [{"eq": ["status_code", 200]}],
                },
            ]
        ),
    ),
    ("S3-ok4", "显式 validate: [] 声明不做断言（三态之②，只应得 S3 PENDING）", _case([_req("s", [])])),
]


def _formatting_failures() -> List[Text]:
    """`formatting` 字段的判据（S-8 / T13）。

    NOTICE（可注入）：本函数**只依赖 `report.to_dict()` 的输出形态**，
    所以元护栏可以"把字段删掉"来验证它真的在检查东西
    （见 `run_selftest` 里的注入式验证）——否则这堆判断可能只是"恰好绿"。
    """
    from unittest import mock  # noqa: PLC0415  # 只在自检路径上需要

    failures: List[Text] = []
    _, make, _ = _kernel()

    # ① 未被探针 → not_probed（报告里必须**始终有**这个键）
    payload = GateReport().to_dict()
    if payload.get("formatting") != {"ran": False, "reason": FORMATTING_NOT_PROBED}:
        failures.append(
            f"[缺字段] 新报告的 formatting 应为 not_probed，实为 {payload.get('formatting')!r}"
        )

    # ② 四种**执行结局**逐条（mock 内核 run_black → 不依赖本机是否装了 black）
    scenarios: List[Tuple[Any, Dict[Text, Any], Text]] = [
        (None, {"ran": True, "reason": FORMATTING_OK}, "真跑成功"),
        (
            subprocess.TimeoutExpired(cmd="black", timeout=1),
            {"ran": True, "reason": FORMATTING_TIMEOUT},
            "超时",
        ),
        (
            subprocess.CalledProcessError(1, "black"),
            {"ran": True, "reason": FORMATTING_FAILED},
            "格式化失败",
        ),
        (OSError("no black"), {"ran": False, "reason": FORMATTING_MISSING_BLACK}, "black 缺失"),
    ]
    for side_effect, expected, label in scenarios:
        with mock.patch.object(make, "run_black", side_effect=side_effect):
            status = probe_formatting("a_test.py")
        if status != expected:
            failures.append(f"[判据错] {label}：期望 {expected}，实为 {status}")

    # ③ 空输入 → no_files（"没有产物"与"没装 black"必须可分：一个查调用链、一个装依赖）
    empty_status = probe_formatting()
    if empty_status != {"ran": False, "reason": FORMATTING_NO_FILES}:
        failures.append(f"[判据错] 空输入应为 no_files，实为 {empty_status}")

    # ④ 挂载透传 + 非法输入降级（不抛、留痕）
    report = GateReport().with_formatting({"ran": True, "reason": FORMATTING_OK})
    if report.to_dict().get("formatting") != {"ran": True, "reason": FORMATTING_OK}:
        failures.append("[透传错] with_formatting 的结论没有出现在报告里")
    if normalize_formatting({"ran": True, "reason": "nonsense"})["reason"] != FORMATTING_UNKNOWN:
        failures.append("[降级错] 非法 reason 应降级为 unknown")
    return failures


def run_selftest(verbose: bool = False) -> int:
    """跑闸门自检。返回 0=全绿；非 0=失败项数。

    NOTICE（判据自检）：MUST_ALLOW 与 MUST_REJECT 必须**同时**满足。
    只测 MUST_REJECT 会得到一道"宁可错杀"的闸门——那种闸门会被用户整体关掉，
    等于没有。本仓对此有明确口径：假报会让真报被无视。
    """
    failures: List[Text] = []

    for code, label, case in MUST_REJECT:
        report = check_testcase(case, path=f"<selftest:{label}>")
        if report.ok:
            failures.append(f"[漏拦] {code} {label}：闸门没有拦下这个已知陷阱")
        elif code not in report.codes():
            failures.append(
                f"[错码] {code} {label}：拦下了，但编号是 {report.codes()}，期望含 {code}"
            )
        if verbose and not report.ok:
            print(f"  [REJECT ok] {code:4} {label}")

    for code, label, case in MUST_ALLOW:
        report = check_testcase(case, path=f"<selftest:{label}>")
        if not report.ok:
            bad = ", ".join(f"{f.code}:{f.message.splitlines()[0][:60]}" for f in report.rejects)
            failures.append(f"[误伤] {code} {label}：合法写法被拦下 → {bad}")
        if verbose and report.ok:
            print(f"  [ALLOW  ok] {code:6} {label}")

    # S10 撞名（批量维度）
    dup_report = GateReport()
    gate_s10_module_collision([("order+create", {}), ("order create", {})], dup_report)
    if dup_report.ok:
        failures.append("[漏拦] S10 两个用例归一化后撞同一个模块段，未被拦下")

    # S11 空文件
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
    tmp.close()
    enc_report = GateReport()
    gate_s11_document_encoding(tmp.name, enc_report)
    if enc_report.ok:
        failures.append("[漏拦] S11 0 字节文档未被拦下")
    os.unlink(tmp.name)

    # L2 格式化字段（S-8 / T13）：四种执行结局 + 两种"没到 black" + 挂载透传
    failures.extend(_formatting_failures())

    # ★元护栏：把 formatting 从报告里删掉之后，上面的判据**必须**变红。
    # 没有这一步，上一条测试可能只是"恰好绿"（本仓把这类叫「护栏打偏」，
    # 见 docs/架构与调用链.md §7 的自纠记录）。
    _original_to_dict = GateReport.to_dict

    def _to_dict_without_formatting(self) -> Dict[Text, Any]:
        payload = _original_to_dict(self)
        payload.pop("formatting", None)
        return payload

    GateReport.to_dict = _to_dict_without_formatting  # type: ignore[method-assign]
    try:
        if not _formatting_failures():
            failures.append(
                "[护栏打偏] 把 formatting 字段从报告里删掉后，判据竟然还是绿的"
            )
    finally:
        GateReport.to_dict = _original_to_dict  # type: ignore[method-assign]

    print("=" * 66)
    if failures:
        print(f"静默陷阱闸门自检**失败** {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        print("=" * 66)
        return len(failures)

    print("静默陷阱闸门自检全部通过：")
    print(f"  拦得住（MUST_REJECT）：{len(MUST_REJECT)} 条对抗样本全部命中，编号正确")
    print(f"  不误伤（MUST_ALLOW） ：{len(MUST_ALLOW)} 条合法写法全部放行")
    print("  批量维度            ：S10 模块名撞车可检出")
    print("  文档维度            ：S11 空文档/坏编码可检出")
    print("  L2 格式化（S-8）    ：formatting 字段可分 ok/timeout/failed/missing_black/no_files")
    print("=" * 66)
    return 0


def main() -> int:
    # T26（方案 v8 §12.8-①）：重定向/管道/CI 环境下，Windows 默认把 stdout/stderr
    # 按 GBK 编码，✓/✗/→ 等字符会抛 UnicodeEncodeError → 退出码 1（假红）。
    # 统一钉为 UTF-8 + errors=replace，保证「实测通过四元组」跨环境可复现。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # 不支持 reconfigure 或流已关闭
            pass

    parser = argparse.ArgumentParser(description="静默陷阱闸门（生成期）")
    parser.add_argument("--selftest", action="store_true", help="跑自检（默认）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    parser.add_argument("--case", help="对一份 YAML/JSON 用例跑闸门")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    if args.case:
        from interfacetester.loader import load_test_file  # noqa: PLC0415

        case = load_test_file(args.case)
        report = check_testcase(case, path=args.case)
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"闸门结果：{'通过' if report.ok else '拦下'}")
            for f in report.findings:
                print(f"  [{f.severity}] {f.code} {f.where}\n      {f.message}")
        return 0 if report.ok else 1

    return run_selftest(verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
