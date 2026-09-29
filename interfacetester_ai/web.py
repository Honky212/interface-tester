# -*- coding: utf-8 -*-
r"""web —— **只读看板**（v8 §2.5 / §5.8 行 1001；**P3a**，2026-09-25）。

## 它是什么（以及**不是**什么）

§2.5 的结论：**CLI 是唯一真相源与执行面**；Web 只是"薄渲染层 + 人工确认台"，
**只读先行、可永远不做**。所以本模块的全部职责是**把已经躺在磁盘上的产物读给人看**：

| 页 | 内容 | 数据来源 |
| --- | --- | --- |
| `/` | 运行列表 | `logs/*.summary.json` |
| `/run?path=…` | 失败明细 | ★§5.5 的正确指针（见下） |
| `/file?path=…` | 产物下载 | 白名单根内**只读** |
| `/metrics` | 度量 | `summary.json` 统计 + `.ai/` 清单 + `reports/*.md` + **`.ai/manifest/` 调用账本** |

## ★四条硬约束（§5.8 行 1001 的验收，逐条都有机器判据）

1. **零写盘** —— 只实现 `do_GET`/`do_HEAD`，其余方法一律 **405**；模块里**不存在**
   任何 `open(…, "w")` / `makedirs` / `os.remove`。判据不是"我说不写"，而是
   **取工作树快照 → 把四个页面全跑一遍 → 再对账**（复用 `workdir.snapshot_tree`）。
2. **零执行** —— **不 import `pytest` / `make` / `llm`**，不跑任何用户代码。
   判据：AST 扫本模块的 import，白名单外一律不许（`_FORBIDDEN_IMPORTS`）。
3. **目录穿越全被拒** —— `safe_rel_path()`：`realpath` 解析后**必须在根内**；
   ★前缀比较用 `os.path.commonpath` 而**不是** `str.startswith`
   （否则 `logs_evil/` 会冒充 `logs/`——这是真实存在的目录名形态，不是假想）。
4. **只监听回环** —— 默认 `127.0.0.1`；`--host` 给非回环值时才对外，且**必须显式**。

## ★§5.5 那个指针（v4 在这里错过一次）

失败明细的字段路径是：

    details[i].records[j].data.validators.validate_extractor[k]

**注意那个 `.data.`**。v4 把它写成 `details[].records[].validators`（**缺一层**），
于是看板永远取不到断言、页面上全是空——**静默的空比报错更难发现**，
这正是本仓反复吃亏的那种形态。本模块用 `dig()` 逐层取，任一层缺就
**明确标注"取不到"**，绝不把"取不到"伪装成"没有断言"。

## ★R8：失败**不得**用"警告"色渲染（行 1114）

Web 天然把失败柔化成 UI 提示（HTTP 200 + 一个红点）。本仓的可信度建立在
"响亮报错 + 明确退出码"上，所以看板的失败态**必须**：
① 用 `error`（红），**不用** `warning`（黄）；② **原文显示** `success`、断言明细与
报告路径，不做措辞缓和（"未能通过"这类软化说法一律不写）。
机器判据：渲染出的 HTML 里**不出现** `warning` / `warn` / `orange` / `yellow`。

## 选型（§2.5 行 350）

stdlib `http.server` + 静态 HTML，**零三方依赖**（本包 `dependencies = []`；
先例是 `tests/mock_server.py` 的 stdlib 写法）。
理由：客户环境是**内网 + 离线镜像**——"离线一键起服务"（P3c）在零依赖下最省事，
而多一个常驻框架就是多一份部署 / 端口 / 证书 / 值守成本（§2.5 表格第 1 条）。

## 用法

    haify serve                      # 只监听 127.0.0.1:8765
    haify serve --host 0.0.0.0       # 对外：必须显式（并会打印放行理由）
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from interfacetester_ai import audit as audit_mod
from interfacetester_ai import confirm as confirm_mod
from interfacetester_ai import jobs as jobs_mod
from interfacetester_ai import reviews as reviews_mod
from typing import Any, Dict, Iterator, List, Optional, Sequence, Text, Tuple

# 白名单根（§2.5 行 350 原文：`.ai/`、`reports/`、`logs/`）——**只读**，不含 `cases/`
ALLOWED_ROOTS: Tuple[Text, ...] = (".ai", "reports", "logs")

# 默认只监听回环（§5.8 行 1001 的验收第 4 条：只监听回环）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# 运行摘要的默认扫描根（`logs/*.summary.json`）
SUMMARY_SUFFIX = ".summary.json"

# ★零执行的自查清单：这些模块一旦被 import，就意味着"读页面"可能变成"跑东西"
_FORBIDDEN_IMPORTS = frozenset(
    {"pytest", "interfacetester.make", "interfacetester_ai.llm", "interfacetester_ai.gen",
     "subprocess", "multiprocessing", "importlib", "runpy", "ctypes", "socket"}
)

# 只允许的写方法：**没有**。这四个名字只是为了给出**明确的 405**，不是能力。
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


class PathRefused(Exception):
    """路径越界（目录穿越 / 绝对路径 / 符号链接逃逸 / 前缀混淆）。

    ★必须**响亮**：静默返回 404 会让人以为"文件不存在"，
    而真实情况是"你试图越过白名单根"——两者的处置完全不同。
    """


def safe_rel_path(root: Text, rel: Text) -> Text:
    """把 `rel` 解析到白名单根 `root` 内，返回**真实绝对路径**；越界抛 `PathRefused`。

    五条判据（每条对应一种真实攻击面，不是假想清单）：

    1. **空值 / 绝对路径** → 拒（`/etc/passwd`、`C:\\Windows\\win.ini`）；
    2. **`..` 穿越** → 拒（`../interfacetester/parser.py`）；
    3. **URL 编码穿越** → 由调用方 `unquote` 一次后再进本函数（`%2e%2e%2f`）；
       ★**只解一次**：反复解码会把"字面量里的 `%2e`"也变成穿越源；
    4. **符号链接逃逸** → `os.path.realpath` 解析后**仍**要落在根内；
    5. **前缀混淆** → 用 `os.path.commonpath` 判，**不用** `str.startswith`：
       `startswith("logs")` 会让 `logs_evil/x` 通过，而它是**另一个目录**。
    """
    raw = (rel or "").replace("\\", "/").strip()
    if not raw:
        raise PathRefused("路径为空")

    # ③ URL 编码只解一次（调用方可能已解；再解一次会放过二次编码的穿越）
    from urllib.parse import unquote  # noqa: PLC0415

    decoded = unquote(raw)

    # ① 绝对路径（POSIX 与 Windows 两种写法）
    if decoded.startswith("/") or re.match(r"^[a-zA-Z]:", decoded):
        raise PathRefused(f"绝对路径不在白名单根内：{decoded}")

    # ② `..` 段落（先按段拆，再判——这样 `a/../..` 也能拦住）
    segments = [seg for seg in decoded.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in segments):
        raise PathRefused(f"路径包含 `..`：{decoded}")

    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root, *segments))

    # ④⑤ realpath 之后**再**判前缀：符号链接指向根外、或 `logs_evil` 这类同前缀目录
    try:
        inside = os.path.commonpath([root_abs, target]) == root_abs
    except ValueError:  # 不同盘符（Windows）
        inside = False
    if not inside:
        raise PathRefused(f"路径解析后落在根外：{decoded}（根 {root_abs}）")

    return target


def resolve_read_path(rel: Text, roots: Sequence[Text] = ALLOWED_ROOTS) -> Text:
    """把**相对工作区根**的路径解析成绝对路径；不在任何读根内 → 抛 `PathRefused`。

    两层判据，顺序不能反：

    1. **路径安全**（`safe_rel_path(".", rel)`）：绝对路径 / `..` / 符号链接逃逸全拒；
    2. **是否落在某个读根内**：用 `commonpath` 判（不是 `startswith`）。

    ★为什么不写成"逐个 `join(root, rel)` 试着打开"：`rel` 是**相对工作区根**的完整
    路径（`logs/x.summary.json`），读根（`logs`）也是相对工作区根的——两者一 join 就
    拼成 `logs/logs/x.summary.json`，**双重前缀**，于是"所有文件都不存在"。
    而这个错误的表现形式极其危险：页面显示"还没有运行记录"，
    **和真的没跑过用例一模一样**。
    """
    target = safe_rel_path(".", rel)
    for root in roots:
        try:
            root_abs = os.path.realpath(root)
        except OSError:
            continue
        try:
            if os.path.commonpath([root_abs, target]) == root_abs:
                return target
        except ValueError:  # 不同盘符（Windows）
            continue
    raise PathRefused(
        f"路径不在任何读根内：{rel}（读根：{', '.join(roots) or '（空）'}）"
    )


def dig(payload: Any, *keys: Any) -> Tuple[Any, Text]:
    """逐层取值，返回 `(值, 失败原因)`——**任一层缺失都给得出原因**。

    ★为什么不用 `reduce(operator.getitem)` + `except KeyError` 一把抓：
    那样只能得到"取不到"，**说不出缺在哪一层**；而看板要显示的正是"缺在哪一层"
    （v4 的错指针 `details[].records[].validators` 就是**缺 `.data.` 那一层**，
    它表现为"断言全是空"，没有任何提示）。
    """
    cursor = payload
    for index, key in enumerate(keys):
        where = ".".join(str(item) for item in keys[: index + 1])
        if isinstance(key, int):
            if not isinstance(cursor, (list, tuple)):
                return None, f"`{where}`：上一层不是列表（实际 {type(cursor).__name__}）"
            if not -len(cursor) <= key < len(cursor):
                return None, f"`{where}`：下标越界（长度 {len(cursor)}）"
            cursor = cursor[key]
            continue
        if not isinstance(cursor, dict):
            return None, f"`{where}`：上一层不是对象（实际 {type(cursor).__name__}）"
        if key not in cursor:
            return None, f"`{where}`：缺少该字段"
        cursor = cursor[key]
    return cursor, ""


# ---------------------------------------------------------------------------
# 数据读取（**只读**）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunRef:
    """运行列表的一行（一份 `*.summary.json` 的摘要）。"""

    rel_path: Text
    name: Text
    success: bool
    case_total: int = 0
    case_fail: int = 0
    step_total: int = 0
    step_fail: int = 0
    duration: float = 0.0
    mtime: float = 0.0

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "rel_path": self.rel_path,
            "name": self.name,
            "success": self.success,
            "case_total": self.case_total,
            "case_fail": self.case_fail,
            "step_total": self.step_total,
            "step_fail": self.step_fail,
            "duration": self.duration,
            "mtime": self.mtime,
        }


@dataclass(frozen=True)
class AssertionRow:
    """一条断言明细（★§5.5 正确指针取出来的那一格）。"""

    case: Text
    step: Text
    step_type: Text
    step_success: bool
    check: Text = ""
    check_result: Text = ""
    check_value: Any = None
    comparator: Text = ""
    expect: Any = None
    message: Text = ""
    address: Text = ""

    @property
    def passed(self) -> bool:
        """★只看 `check_result == "pass"`。

        不写 `!= "fail"`：那样**任何**没见过的值（`None`、拼错的、新版本加的状态）
        都会被算成通过——正是"假通过"的标准长相。
        """
        return self.check_result == "pass"

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "case": self.case,
            "step": self.step,
            "step_type": self.step_type,
            "step_success": self.step_success,
            "check": self.check,
            "check_result": self.check_result,
            "check_value": self.check_value,
            "comparator": self.comparator,
            "expect": self.expect,
            "message": self.message,
            "address": self.address,
        }


def read_json(abs_path: Text) -> Tuple[Any, Text]:
    """读 JSON，返回 `(对象, 失败原因)`。**读不到就说读不到**，不拿空壳冒充成功。"""
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as fp:
            return json.load(fp), ""
    except FileNotFoundError:
        return None, "文件不存在"
    except OSError as error:
        return None, f"读取失败：{type(error).__name__}: {error}"
    except ValueError as error:
        return None, f"JSON 解析失败：{error}"


def stat_counts(summary: Any) -> Tuple[int, int, int, int]:
    """取 `(用例总数, 用例失败, 步骤总数, 步骤失败)`（字段名照实测结构，逐层 dig）。"""
    numbers: List[int] = []
    for path in (
        ("stat", "testcases", "total"),
        ("stat", "testcases", "fail"),
        ("stat", "teststeps", "total"),
        ("stat", "teststeps", "failures"),
    ):
        value, _why = dig(summary, *path)
        numbers.append(int(value) if isinstance(value, int) else 0)
    return numbers[0], numbers[1], numbers[2], numbers[3]


def iter_runs(roots: Sequence[Text] = ALLOWED_ROOTS) -> List[RunRef]:
    """扫读根下的 `*.summary.json`，按 **mtime 倒序**（最近跑的在前）。

    ★为什么读根是**参数**而不是写死 `logs/`：本仓的真实产物分布在 `logs/`（内核直接
    产出）与**项目子目录**（如 `project-two/logs/`）。写死一个前缀的结果是"看板空空如也、
    而磁盘上明明有产物"——那是**最容易被误判成'这个功能没做'的失败形态**。
    但也不能放宽成通配 `*/logs/`（那等于给路径穿越开后门）：所以改成**显式追加读根**，
    每个读根仍然要过 `safe_rel_path` 的 commonpath 校验。
    """
    found: List[RunRef] = []
    for root in roots:
        abs_root = os.path.realpath(root)
        if not os.path.isdir(abs_root):
            continue
        for dirpath, dirnames, filenames in os.walk(abs_root):
            dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
            for filename in sorted(filenames):
                if not filename.endswith(SUMMARY_SUFFIX):
                    continue
                abs_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(abs_path, os.getcwd()).replace(os.sep, "/")
                try:
                    mtime = os.path.getmtime(abs_path)
                except OSError:
                    mtime = 0.0

                payload, why = read_json(abs_path)
                if why:
                    # ★读不到 **不跳过**：跳过等于"这份运行不存在"，
                    # 而事实是"它存在但坏了"——后者必须出现在列表里（红色）。
                    found.append(
                        RunRef(
                            rel_path=rel_path,
                            name=filename[: -len(SUMMARY_SUFFIX)],
                            success=False,
                            mtime=mtime,
                        )
                    )
                    continue

                case_total, case_fail, step_total, step_fail = stat_counts(payload)
                duration, _ = dig(payload, "time", "duration")
                success, _ = dig(payload, "success")
                found.append(
                    RunRef(
                        rel_path=rel_path,
                        name=filename[: -len(SUMMARY_SUFFIX)],
                        success=bool(success),
                        case_total=case_total,
                        case_fail=case_fail,
                        step_total=step_total,
                        step_fail=step_fail,
                        duration=(
                            float(duration) if isinstance(duration, (int, float)) else 0.0
                        ),
                        mtime=mtime,
                    )
                )
    found.sort(key=lambda item: (-item.mtime, item.rel_path))
    return found


def extract_assertions(summary: Any) -> Tuple[List[AssertionRow], List[Text]]:
    """按 ★§5.5 的正确指针抽出全部断言，返回 `(行, 取不到的原因)`。

    指针（逐字照抄，**注意 `.data.`**）：

        details[i].records[j].data.validators.validate_extractor[k]

    **为什么返回值里必须带"原因"**：v4 把这一路径**漏了一层 `.data.`**，表现是
    "页面上断言全是空"——而"空"看起来和"这份用例本来就没写断言"**一模一样**。
    把每一层的缺失原因带出来，页面才能说清"是没断言，还是我取不到"。
    """
    rows: List[AssertionRow] = []
    problems: List[Text] = []

    details, why = dig(summary, "details")
    if why:
        return rows, [f"`details`：{why}"]
    if not isinstance(details, (list, tuple)):
        return rows, [f"`details`：不是列表（实际 {type(details).__name__}）"]

    for i, detail in enumerate(details):
        case, _ = dig(detail, "name")
        records, rec_why = dig(detail, "records")
        if rec_why:
            problems.append(f"details[{i}].records：{rec_why}")
            continue
        if not isinstance(records, (list, tuple)):
            problems.append(f"details[{i}].records：不是列表")
            continue

        for j, record in enumerate(records):
            step, _ = dig(record, "name")
            step_type, _ = dig(record, "step_type")
            step_success, _ = dig(record, "success")
            address, _ = dig(record, "data", "address")

            # ★★ 就是下面这两行：`data` 那一层**不能漏**（v4 漏的就是它）
            validators, v_why = dig(record, "data", "validators")
            if v_why:
                problems.append(f"details[{i}].records[{j}].data.validators：{v_why}")
                continue

            items, item_why = dig(validators, "validate_extractor")
            if item_why:
                problems.append(
                    f"details[{i}].records[{j}].data.validators.validate_extractor：{item_why}"
                )
                continue
            if not isinstance(items, (list, tuple)):
                problems.append(f"details[{i}].records[{j}]…validate_extractor：不是列表")
                continue

            for item in items:
                rows.append(
                    AssertionRow(
                        case=str(case or ""),
                        step=str(step or ""),
                        step_type=str(step_type or ""),
                        step_success=bool(step_success),
                        check=str(dig(item, "check")[0] or ""),
                        check_result=str(dig(item, "check_result")[0] or ""),
                        check_value=dig(item, "check_value")[0],
                        comparator=str(dig(item, "comparator")[0] or ""),
                        expect=dig(item, "expect")[0],
                        message=str(dig(item, "message")[0] or ""),
                        address=str(address or ""),
                    )
                )
    return rows, problems


def load_summary(
    rel_path: Text, roots: Sequence[Text] = ALLOWED_ROOTS
) -> Tuple[Any, Text, Text]:
    """按**相对路径**读一份摘要，返回 `(对象, 命中的读根, 失败原因)`。

    ★为什么不在 handler 里直接 `open(用户给的 path)`：`?path=` 是**用户输入**，
    必须在读根集合里**逐个** `safe_rel_path` 校验；全部不命中就明确拒绝，
    而不是"碰巧能打开就打开"。
    """
    try:
        target = resolve_read_path(rel_path, roots)
    except PathRefused as refused:
        return None, "", str(refused)
    hit_root = rel_path.replace("\\", "/").split("/")[0]
    payload, why = read_json(target)
    return (payload, hit_root, "") if not why else (None, hit_root, why)


# ---------------------------------------------------------------------------
# 度量（行 348：降级率**必须显示在看板上**，不能只写日志）
# ---------------------------------------------------------------------------

# `reports/analysis.md` 里那一行的口径（由 `analyze.render_analysis_report` 产出）：
#   - **未降级占比：XX%**（§5.3 的验收线是 ≥70%；…）
KEPT_RATIO_RE = re.compile(r"未降级占比[：:]\s*\**\s*(\d+(?:\.\d+)?)\s*%")
KEPT_MARK = "未降级占比"

# 度量页会去看的报告（**只读**；不存在就如实说"没有"）
ANALYSIS_REL = "reports/analysis.md"
SELF_HEAL_REL = "reports/self_heal.md"

NO_SOURCE = "无数据源"

# ★`.ai/manifest/` 是**已经存在**的调用账本（`manifest.py` 是**登记在册**的写入者，
#   只写 `.ai/`）——它每条记录都带 `cached` / `attempts` / `usage` / `model`。
#   所以"缓存命中率 / token 用量 / 花费估算"这三个数字**本来就有来源**，
#   不需要在 LLM 层新开写入者（红线③：不新增写盘点；§0.4-② 的同一取向）。
MANIFEST_REL = ".ai/manifest"
MANIFEST_SUFFIX = ".json"

# 花费估算需要**单价**，而单价是项目/供应商的事——**本仓不猜**（猜出来的数字会被当成事实引用）。
# 按 §六 的环境变量命名惯例留两个口子；没配 → 「未读到」+ 点名缺哪个变量。
PRICE_PROMPT_ENV = "INTERFACETESTER_AI_PRICE_PROMPT_PER_MTOK"
PRICE_COMPLETION_ENV = "INTERFACETESTER_AI_PRICE_COMPLETION_PER_MTOK"


@dataclass(frozen=True)
class MetricLine:
    """度量页的一行。★**每一行都必须带 `source`**（来源）或显式标注"无数据源"。

    这是"每个数字都要能说清出处"的机器判据：一个来源不明的数字上了看板，
    比没有这个数字更坏——它会被当成事实引用。
    """

    label: Text
    value: Text
    source: Text
    note: Text = ""

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "source": self.source,
            "note": self.note,
        }


def read_kept_ratio() -> MetricLine:
    """从 `reports/analysis.md` 取"未降级占比"（§5.3 的核心验收指标）。

    ★读不到 → `value="未读到"`，**绝不显示 `0%`**：`0%` 会被读成
    "所有结论都被降级了"（一个刺眼的**假**信号），而事实很可能只是
    "还没跑过 `haify analyze`"。这两种情况的处置完全不同。
    """
    try:
        abs_path = resolve_read_path(ANALYSIS_REL)
    except PathRefused as refused:
        return MetricLine(
            label="未降级占比", value="未读到", source=ANALYSIS_REL, note=str(refused)
        )
    if not os.path.exists(abs_path):
        return MetricLine(
            label="未降级占比",
            value="未读到",
            source=NO_SOURCE,
            note=f"{ANALYSIS_REL} 不存在（还没跑过 `haify analyze`）",
        )

    with open(abs_path, encoding="utf-8", errors="replace") as fp:
        text = fp.read()
    for number, line in enumerate(text.splitlines(), start=1):
        if KEPT_MARK not in line:
            continue
        match = KEPT_RATIO_RE.search(line)
        if match:
            return MetricLine(
                label="未降级占比",
                value=f"{match.group(1)}%",
                source=f"{ANALYSIS_REL}:{number}",
                note="§5.3 验收线 ≥70%；低于它说明提示或校验该收紧，而不是「模型不行」",
            )
        return MetricLine(
            label="未降级占比",
            value="未读到",
            source=f"{ANALYSIS_REL}:{number}",
            note="该行里没有可解析的百分数",
        )
    return MetricLine(
        label="未降级占比",
        value="未读到",
        source=ANALYSIS_REL,
        note="报告里没有「未降级占比」行",
    )


def count_ai_items() -> Dict[Text, int]:
    """统计 `.ai/` 下的清单规模（**只读**）：草稿 / 待确认 / 已确认留痕。"""
    counts = {"draft": 0, "pending": 0, "reviews": 0}
    for name in counts:
        try:
            abs_dir = safe_rel_path(".ai", name)
        except PathRefused:
            continue
        if not os.path.isdir(abs_dir):
            continue
        counts[name] = len(
            [
                item
                for item in os.listdir(abs_dir)
                if os.path.isfile(os.path.join(abs_dir, item))
            ]
        )
    return counts


def _as_int(value: Any) -> int:
    """把 `usage` 里的计数转成**非负整数**（缺/坏/负数一律 0，且由调用方单独记账）。"""
    if value is None or isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _env_price(
    name: Text, env: Optional[Dict[Text, Text]] = None
) -> Tuple[Optional[float], Text]:
    """读一个"单价"环境变量，返回 `(值, 问题说明)`。

    ★三种情况必须**分开**（合成一个"未读到"会丢掉"该修哪里"的信息）：
    没配 / 配了但不是数字 / 配了负数——后两种是**配错了**，不是"没有这个数据"。
    """
    raw = token_from_env(name, env).strip()
    if not raw:
        return None, f"未配置 `{name}`"
    try:
        value = float(raw)
    except ValueError:
        return None, f"`{name}` 不是数字（值：{raw!r}）"
    if value < 0:
        return None, f"`{name}` 是负数（值：{raw!r}）"
    return value, ""


def read_manifest_stats() -> Dict[Text, Any]:
    """汇总 `.ai/manifest/*.json` 这个**既有**调用账本（**只读**）。

    ★口径三条（都是被"看起来正常的假数字"逼出来的）：
    1. **分母只用"可读的条数"**（`readable`）——把坏文件算进分母会**静默压低**命中率，
       而真相是"另有文件读不到"。坏文件单独计数（`unreadable`），页面照实说；
    2. **`cached` 只认 `is True`**——字符串 `"true"` / 缺字段都不算命中（宁可少算，不可多算）；
    3. **token 只在 `usage` 真存在时累加**，并单独记 `without_usage` 条数
       ——命中缓存那次本来就没有 `usage`（没有真实花销），把它当坏账是错的。
    """
    stats: Dict[Text, Any] = {
        "files": 0,
        "readable": 0,
        "unreadable": 0,
        "cached": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "usage_entries": 0,
        "without_usage": 0,
        "models": [],
        "missing": False,
        "refused": False,
    }

    try:
        abs_dir = safe_rel_path(".ai", "manifest")
    except PathRefused:
        stats["refused"] = True
        return stats
    if not os.path.isdir(abs_dir):
        stats["missing"] = True
        return stats

    models = set()
    for name in sorted(os.listdir(abs_dir)):
        if not name.endswith(MANIFEST_SUFFIX):
            continue
        path = os.path.join(abs_dir, name)
        if not os.path.isfile(path):
            continue
        stats["files"] += 1
        try:
            with open(path, encoding="utf-8", errors="replace") as fp:
                payload = json.load(fp)
        except (OSError, ValueError):
            stats["unreadable"] += 1
            continue
        if not isinstance(payload, dict):
            stats["unreadable"] += 1
            continue

        stats["readable"] += 1
        if payload.get("cached") is True:
            stats["cached"] += 1
        if payload.get("model"):
            models.add(str(payload["model"]))

        usage = payload.get("usage")
        if isinstance(usage, dict) and (
            usage.get("prompt_tokens") is not None
            or usage.get("completion_tokens") is not None
        ):
            stats["prompt_tokens"] += _as_int(usage.get("prompt_tokens"))
            stats["completion_tokens"] += _as_int(usage.get("completion_tokens"))
            stats["usage_entries"] += 1
        else:
            stats["without_usage"] += 1

    stats["models"] = sorted(models)
    return stats


def _manifest_absent_reason(stats: Mapping[Text, Any]) -> Text:
    """`.ai/manifest/` 读不到时，**把原因说清**（而不是笼统一句"无数据源"）。"""
    if stats.get("refused"):
        return f"{MANIFEST_REL}/ 的读根被拒（路径校验没过）"
    if stats.get("missing"):
        return f"{MANIFEST_REL}/ 不存在（还没跑过 `haify gen` / `haify analyze`）"
    return (
        f"{MANIFEST_REL}/ 里有 {stats['files']} 个文件，但**一个都读不到**"
        "（JSON 损坏或不是对象）——先看审计留痕，别把这个当成「没有调用」"
    )


def _usage_note(stats: Mapping[Text, Any]) -> Text:
    """token 口径的脚注：**带 usage 的条数**与"没有 usage 的条数"分开说。"""
    parts = [f"带 `usage` 的留痕 {stats['usage_entries']} 条"]
    if stats["without_usage"]:
        parts.append(
            f"另有 {stats['without_usage']} 条没有 `usage`"
            "（命中缓存的那次本来就不花 token；未命中却缺 `usage`，是网关没返回）"
        )
    return "；".join(parts)


def read_cache_hit_rate() -> MetricLine:
    """缓存命中率（来源：`.ai/manifest/*.json` 的 `cached` 字段）。"""
    stats = read_manifest_stats()
    if not stats["readable"]:
        return MetricLine(
            label="缓存命中率",
            value="未读到",
            source=NO_SOURCE,
            note=_manifest_absent_reason(stats),
        )

    rate = stats["cached"] / stats["readable"] * 100
    note = [
        f"分子＝{stats['cached']} 条 `cached: true`，分母＝{stats['readable']} 条**可读**留痕",
        "命中＝回放（不花 token、不抖动）；命中率高不等于便宜，只说明输入重复",
    ]
    if stats["unreadable"]:
        note.append(f"另有 {stats['unreadable']} 条读不到（**未计入分母**，不是 0 命中）")
    if stats["models"]:
        note.append("模型：" + "、".join(stats["models"]))
    return MetricLine(
        label="缓存命中率",
        value=f"{rate:.1f}%",
        source=f"{MANIFEST_REL}/*.json 的 `cached` 字段（{stats['readable']} 条可读留痕）",
        note="；".join(note),
    )


def read_token_usage() -> MetricLine:
    """token 用量合计（来源：`.ai/manifest/*.json` 的 `usage`；**花费估算的依据**）。"""
    stats = read_manifest_stats()
    if not stats["readable"] or not stats["usage_entries"]:
        note = _manifest_absent_reason(stats)
        if stats["readable"]:
            note = (
                f"{stats['readable']} 条留痕里**一条都没带 `usage`**"
                "（网关不返回 usage 时就是这样）——所以 token 与花费都无从算起"
            )
        return MetricLine(
            label="token 用量（留痕合计）",
            value="未读到",
            source=NO_SOURCE,
            note=note,
        )
    return MetricLine(
        label="token 用量（留痕合计）",
        value=f"prompt {stats['prompt_tokens']} / completion {stats['completion_tokens']}",
        source=f"{MANIFEST_REL}/*.json 的 `usage`（{stats['usage_entries']} 条）",
        note=_usage_note(stats),
    )


def read_cost_estimate(env: Optional[Dict[Text, Text]] = None) -> MetricLine:
    """花费估算（来源：`.ai/manifest/*.json` 的 `usage` × **你配的单价**）。

    ★**本仓不内置任何价格表**：单价随供应商/合同/时间变，写死在代码里就是
    一个**会过期的假数字**。所以只留两个环境变量口子；没配就是「未读到」，
    并**点名缺哪个变量**——而不是编一个 `0`（§2.5 度量纪律：没有来源的数字不上板）。
    """
    stats = read_manifest_stats()
    prompt_price, prompt_why = _env_price(PRICE_PROMPT_ENV, env)
    completion_price, completion_why = _env_price(PRICE_COMPLETION_ENV, env)

    if not stats["readable"] or not stats["usage_entries"]:
        note = _manifest_absent_reason(stats)
        if stats["readable"]:
            note = (
                f"{stats['readable']} 条留痕里没有 `usage` → 没有可乘的 token 数"
                "（缺 `usage` 时**不按 0 算**，那样会给出一个偏小的假花费）"
            )
        return MetricLine(
            label="花费估算",
            value="未读到",
            source=NO_SOURCE,
            note=note,
        )

    if prompt_price is None or completion_price is None:
        missing = [why for why in (prompt_why, completion_why) if why]
        return MetricLine(
            label="花费估算",
            value="未读到",
            source=(
                f"{MANIFEST_REL}/*.json 的 `usage` × "
                f"`{PRICE_PROMPT_ENV}`/`{PRICE_COMPLETION_ENV}`"
            ),
            note=(
                "缺单价："
                + "；".join(missing)
                + "（单价填**每 100 万 token** 的数，币种随你——本仓不猜；"
                f"手算口径：{stats['prompt_tokens']} prompt + "
                f"{stats['completion_tokens']} completion）"
            ),
        )

    cost = (
        stats["prompt_tokens"] / 1_000_000 * prompt_price
        + stats["completion_tokens"] / 1_000_000 * completion_price
    )
    return MetricLine(
        label="花费估算",
        value=f"{cost:.4f}",
        source=(
            f"{MANIFEST_REL}/*.json 的 `usage` × "
            f"`{PRICE_PROMPT_ENV}={prompt_price}` / "
            f"`{PRICE_COMPLETION_ENV}={completion_price}`"
        ),
        note=(
            "单位＝你填的单价单位（本仓不猜币种）；"
            f"{stats['prompt_tokens']} prompt + {stats['completion_tokens']} completion；"
            + _usage_note(stats)
        ),
    )


def metrics_payload(roots: Sequence[Text] = ALLOWED_ROOTS) -> Dict[Text, Any]:
    """度量页的**全部**数据。★返回的每一行都是 `MetricLine`（**带来源**）。

    ★"缓存命中率 / token 用量 / 花费估算"这三行**不再是"无数据源"**（第十三批，2026-09-25）：
    它们的来源是 `.ai/manifest/*.json`——**已经存在的**调用账本（`manifest.py` 是登记在册的
    写入者，每条记录都带 `cached` / `attempts` / `usage` / `model`）。所以**不需要在 LLM 层
    新开写入者**（红线③：不新增写盘点），只要把它们读出来。

    ★三条纪律照旧：
    - **每一步都要说清来源**（`source`），或者显式写「未读到」；
    - **「未读到」绝不写成 `0`**：`0%` 会被读成"一次都没命中"，`0` 花费会被读成"白跑"；
    - **加总的假数字比缺失的数字更坏**：口径不确定的地方（单价、缺 `usage` 的条数、
      读不到的坏文件）都**写进 `note`**，让人能自己复核，而不是替人拍一个数。
    """
    runs = iter_runs(roots)
    counts = count_ai_items()
    root_text = "、".join(roots) or "（空）"

    lines: List[MetricLine] = [
        MetricLine(
            label="运行数",
            value=str(len(runs)),
            source=f"{root_text} 下的 *{SUMMARY_SUFFIX}",
        ),
        MetricLine(
            label="失败运行数",
            value=str(sum(1 for item in runs if not item.success)),
            source="同上，逐份取 `success` 字段",
        ),
        read_kept_ratio(),
        MetricLine(
            label="待人工确认（PENDING）",
            value=str(counts["pending"]),
            source=".ai/pending/*.pending.json",
            note="清单条目数 = 文件数（每个用例一份）",
        ),
        MetricLine(
            label="已留痕确认",
            value=str(counts["reviews"]),
            source=".ai/reviews/*.json",
            note="T24 的确认留痕（含 manifest 内容哈希）",
        ),
        MetricLine(
            label="待确认草稿",
            value=str(counts["draft"]),
            source=".ai/draft/",
            note="带 `TODO_` 的占位草稿——**转正必须经人工确认**",
        ),
        read_cache_hit_rate(),
        read_token_usage(),
        read_cost_estimate(),
    ]
    return {
        "lines": [line.to_dict() for line in lines],
        "runs": [item.to_dict() for item in runs],
    }


def failure_trend(roots: Sequence[Text] = ALLOWED_ROOTS) -> List[Dict[Text, Any]]:
    """失败分布**趋势**（§2.5 价值 3：CLI 每次只打印一次，沉淀不了）。

    按 mtime 升序给出每次运行的 `(时间, 用例失败/总数)`——纯文本表格即可，
    **不引图表库**（离线环境不能多一个前端依赖，§5.8 行 1004）。
    """
    trend: List[Dict[Text, Any]] = []
    for item in sorted(iter_runs(roots), key=lambda run: run.mtime):
        trend.append(
            {
                "rel_path": item.rel_path,
                "name": item.name,
                "mtime": item.mtime,
                "success": item.success,
                "case_fail": item.case_fail,
                "case_total": item.case_total,
                "step_fail": item.step_fail,
                "step_total": item.step_total,
            }
        )
    return trend


# ---------------------------------------------------------------------------
# 渲染（纯静态 HTML + 内联 CSS；★R8：失败用 error 色）
# ---------------------------------------------------------------------------

# ★这四个词是 R8 的**机器判据**：渲染产物里一个都不许出现（见 run_selftest）。
# 详细理由写在模块 docstring（**不写进 HTML**——注释也会进产物，一样会命中判据）。
FORBIDDEN_STYLE_WORDS: Tuple[Text, ...] = ("warning", "warn", "orange", "yellow")

# 状态文案：★失败就叫"失败"（不做措辞缓和：不叫"未通过"、不叫"注意"）
STATE_PASS = "通过"
STATE_ERROR = "失败"
STATE_NA = "无断言"

_CSS = """\
:root { color-scheme: light dark; --fg:#1f2328; --bg:#fff; --mut:#57606a;
        --line:#d0d7de; --ok:#1a7f37; --err:#c62828; --na:#6e7781; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e6edf3; --bg:#0d1117; --mut:#8b949e; --line:#30363d;
          --ok:#3fb950; --err:#f85149; --na:#8b949e; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.55 ui-sans-serif,system-ui,"Microsoft YaHei",sans-serif; }
header { display:flex; gap:16px; align-items:center; padding:10px 20px;
         border-bottom:1px solid var(--line); }
header .brand { font-weight:700; }
header nav a { margin-right:12px; }
main { padding:16px 20px 40px; max-width:1200px; }
h1 { font-size:20px; margin:8px 0 4px; }
h2 { font-size:16px; margin:22px 0 6px; }
a { color:inherit; }
.subtitle { color:var(--mut); margin:0 0 12px; }
code, .mono { font-family:ui-monospace,Consolas,monospace; font-size:12.5px; }
table { border-collapse:collapse; width:100%; margin-top:8px; }
th, td { border:1px solid var(--line); padding:5px 8px; text-align:left;
         vertical-align:top; }
th { background:rgba(127,127,127,.09); }
.state-pass { color:var(--ok); font-weight:700; }
.state-error { color:var(--err); font-weight:700; }
.state-na { color:var(--na); }
.banner { border:1px solid var(--line); border-left:5px solid var(--err);
          padding:10px 14px; margin:12px 0; }
.banner.pass { border-left-color:var(--ok); }
.banner.na { border-left-color:var(--na); }
.muted { color:var(--mut); }
.editor { width:100%; min-height:6em; font-family:ui-monospace,Consolas,monospace;
          font-size:12.5px; line-height:1.45; white-space:pre; }
button { font:inherit; padding:4px 12px; cursor:pointer; }
footer { border-top:1px solid var(--line); padding:12px 20px; color:var(--mut); }
"""


def state_span(ok: bool, yes: Text = STATE_PASS, no: Text = STATE_ERROR) -> Text:
    """状态标记（★只有一个入口，所以"失败用什么颜色"不可能两处不一致）。"""
    css = "state-pass" if ok else "state-error"
    return f'<span class="{css}">{html.escape(yes if ok else no)}</span>'


def page(title: Text, body: Text, *, subtitle: Text = "") -> Text:
    """统一外壳。★**零外部资源**：不引 CDN / 字体 / JS 框架（离线可用，§5.8 行 1004）。"""
    head = (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
    )
    nav = (
        '<header><a class="brand" href="/">haify 看板（只读）</a>'
        '<nav><a href="/">运行列表</a><a href="/metrics">度量</a>'
        '<a href="/audit">审计</a><a href="/pending">待确认</a>'
        '<a href="/jobs">作业</a></nav></header>\n'
    )
    main = f"<main>\n<h1>{html.escape(title)}</h1>\n"
    if subtitle:
        main += f'<p class="subtitle">{subtitle}</p>\n'
    footer = (
        "<footer>只读看板（§5.8 P3a）：<b>零写盘、零执行</b>、只监听回环；"
        "页面不做判定——判定以 CLI 退出码与产物文件为准。</footer>\n"
    )
    return head + nav + main + body + "\n</main>\n" + footer + "</body>\n</html>\n"


def render_index(runs: Sequence[RunRef], metrics: Dict[Text, Any]) -> Text:
    """① 运行列表（首页）+ 度量摘要。

    ★降级率（未降级占比）**放在首页**而不只在度量页：行 348 要求"必须显示在
    看板上，不能只写日志"——首页才是"看板"的默认落点。
    """
    rows: List[Text] = []
    for item in runs:
        link = f'<a class="mono" href="/run?path={html.escape(item.rel_path)}">{html.escape(item.rel_path)}</a>'
        rows.append(
            "<tr>"
            f"<td>{link}</td>"
            f"<td>{state_span(item.success)}</td>"
            f"<td>{item.case_fail} / {item.case_total}</td>"
            f"<td>{item.step_fail} / {item.step_total}</td>"
            f"<td>{item.duration:.3f}s</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>产物</th><th>success</th><th>用例失败/总数</th>"
        "<th>步骤失败/总数</th><th>耗时</th></tr></thead><tbody>"
        + ("".join(rows) or '<tr><td colspan="5" class="state-na">没有找到任何 *'
           + SUMMARY_SUFFIX + "（还没跑过用例，或读根不对）</td></tr>")
        + "</tbody></table>"
    )

    lines = metrics.get("lines", [])
    metric_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td class=\"mono muted\">{}</td><td class=\"muted\">{}</td></tr>".format(
            html.escape(str(line.get("label", ""))),
            html.escape(str(line.get("value", ""))),
            html.escape(str(line.get("source", ""))),
            html.escape(str(line.get("note", ""))),
        )
        for line in lines
    )
    metric_table = (
        "<table><thead><tr><th>指标</th><th>值</th><th>来源</th><th>说明</th></tr></thead>"
        "<tbody>" + metric_rows + "</tbody></table>"
    )
    body = (
        f"<p>读根：<span class=\"mono\">{html.escape('、'.join(ALLOWED_ROOTS))}</span>；"
        f"共 <b>{len(runs)}</b> 份运行摘要。</p>"
        + table
        + "<h2>度量</h2>"
        + metric_table
        + "<p class=\"muted\">每个数字都带<b>来源</b>；没有来源的项写「未读到」，"
        "不编 0（编出来的 0 会被当成事实引用）。</p>"
    )
    return page("运行列表", body, subtitle="只读：本页不改任何文件、不执行任何用例")


def render_run(
    rel_path: Text,
    summary: Any,
    rows: Sequence[AssertionRow],
    problems: Sequence[Text],
    *,
    load_error: Text = "",
) -> Text:
    """② 失败明细。

    ★R8：`success` 的**原文**摆在最上面，用 error 色；不写"未能通过"这类缓和说法。
    ★§5.5：`problems`（**取不到的层**）单独成块——它和"这份用例没有断言"是两件事，
    页面上必须分开说，否则 v4 那种"漏一层 `.data.` → 断言全空"会再次看起来像"正常"。
    """
    success, _ = dig(summary, "success")
    case_total, case_fail, step_total, step_fail = stat_counts(summary or {})

    parts: List[Text] = []
    if load_error:
        parts.append(
            f'<div class="banner"><b>读不到这份摘要</b>：{html.escape(load_error)}</div>'
        )
    elif success:
        parts.append(
            '<div class="banner pass"><b>success = true</b>'
            f"（用例 {case_total} 全过 / 步骤 {step_total} 全过）</div>"
        )
    else:
        parts.append(
            '<div class="banner"><b>success = false</b>'
            f"——用例 <b>{case_fail} / {case_total}</b> 失败；"
            f"步骤 <b>{step_fail} / {step_total}</b> 失败</div>"
        )

    parts.append(
        f'<p>产物：<span class="mono">{html.escape(rel_path)}</span>；'
        '下载：<a class="mono" href="/file?path='
        + html.escape(rel_path)
        + '">原样下载</a>；看这些失败的自愈建议：<a class="mono" href="/fix?path='
        + html.escape(rel_path)
        + '">/fix</a></p>'
    )

    if problems:
        items = "".join(f"<li class=\"mono\">{html.escape(item)}</li>" for item in problems)
        parts.append(
            '<h2>取不到的层</h2>'
            '<div class="banner"><b>下面这些字段取不到</b>——页面会因此显示为空，'
            '但<b>空不等于「没有断言」</b>，两者必须分开看：</div>'
            f"<ul>{items}</ul>"
        )

    if not rows:
        parts.append(
            f'<p class="state-na">{STATE_NA}：这份摘要里没有任何断言明细'
            + ("（因为上面那些层取不到）" if problems else "（用例确实没写断言）")
            + "。</p>"
        )
    else:
        ordered = sorted(rows, key=lambda item: (item.passed, item.case, item.step))
        body_rows = "".join(
            "<tr>"
            f'<td class="mono">{html.escape(item.case)}</td>'
            f'<td class="mono">{html.escape(item.step)}</td>'
            f'<td class="mono">{html.escape(item.step_type)}</td>'
            f"<td>{html.escape(item.check)}</td>"
            f"<td>{state_span(item.passed, item.check_result, item.check_result or STATE_ERROR)}</td>"
            f'<td class="mono">{html.escape(str(item.check_value))}</td>'
            f"<td>{html.escape(item.comparator)}</td>"
            f'<td class="mono">{html.escape(str(item.expect))}</td>'
            f"<td>{html.escape(item.message)}</td>"
            "</tr>"
            for item in ordered
        )
        failed = sum(1 for item in rows if not item.passed)
        parts.append(
            f"<h2>断言明细（{len(rows)} 条，其中 <b>{failed}</b> 条未通过）</h2>"
            "<table><thead><tr><th>用例</th><th>步骤</th><th>类型</th><th>check</th>"
            "<th>结果</th><th>实际值</th><th>比较器</th><th>期望值</th><th>message</th>"
            f"</tr></thead><tbody>{body_rows}</tbody></table>"
        )
        parts.append(
            '<p class="muted">字段路径（★缺一层就取不到）：'
            '<span class="mono">details[i].records[j].data.validators.'
            "validate_extractor[k]</span></p>"
        )

    return page(
        f"运行明细 · {rel_path}",
        "".join(parts),
        subtitle="只读：本页只解析已存在的 JSON，不执行用例、不调模型",
    )


def render_metrics(payload: Dict[Text, Any], trend: Sequence[Dict[Text, Any]]) -> Text:
    """④ 度量页：指标（带来源）+ 失败分布趋势（纯文本，不引图表库）。"""
    lines = payload.get("lines", [])
    rows = "".join(
        "<tr><td>{}</td><td><b>{}</b></td><td class=\"mono muted\">{}</td>"
        "<td class=\"muted\">{}</td></tr>".format(
            html.escape(str(line.get("label", ""))),
            html.escape(str(line.get("value", ""))),
            html.escape(str(line.get("source", ""))),
            html.escape(str(line.get("note", ""))),
        )
        for line in lines
    )
    table = (
        "<table><thead><tr><th>指标</th><th>值</th><th>来源</th><th>说明</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )

    trend_rows = "".join(
        "<tr><td class=\"mono\">{}</td><td>{}</td><td>{}/{}</td><td>{}/{}</td></tr>".format(
            html.escape(str(item.get("rel_path", ""))),
            state_span(bool(item.get("success"))),
            item.get("case_fail", 0),
            item.get("case_total", 0),
            item.get("step_fail", 0),
            item.get("step_total", 0),
        )
        for item in trend
    )
    trend_table = (
        "<table><thead><tr><th>运行</th><th>success</th><th>用例失败/总数</th>"
        "<th>步骤失败/总数</th></tr></thead><tbody>"
        + (trend_rows or '<tr><td colspan="4" class="state-na">没有运行记录</td></tr>')
        + "</tbody></table>"
    )

    body = (
        table
        + "<h2>失败分布趋势（按运行时间升序）</h2>"
        + trend_table
        + '<p class="muted">趋势用纯文本表格呈现——离线环境不引图表库（§5.8 行 1004）。</p>'
    )
    return page("度量", body, subtitle="每个数字都带来源；没有来源的写「未读到」")


def confirm_form(default_payload: Dict[Text, Any], *, label: Text = "提交确认") -> Text:
    """确认门表单（**原生 JS，无框架**——§5.8 行 1004 的要求）。

    ★为什么用 JS 而不是 `<form>`：确认门收的是 **JSON**（含嵌套的 `decisions`），
    而 `application/x-www-form-urlencoded` **表达不了它**。硬用 `<form>` 会逼我们在
    服务端再造一套"表单 → JSON"的转换规则——那等于给同一件事造**第二个真相源**，
    而两个真相源迟早会不一致（本仓已经吃过两次这类亏）。
    """
    body = html.escape(json.dumps(default_payload, ensure_ascii=False, indent=2))
    return (
        f'<textarea class="editor" id="cf-body" rows="14">{body}</textarea>'
        f"<p><button onclick=\"haifyConfirm()\">{html.escape(label)}</button> "
        '<span id="cf-msg" class="muted"></span></p>'
        '<p class="muted">★本页**不会自动刷新**：刷新或切页会<b>清空</b>你正在改的内容'
        "——所以请一次填完再提交，或先把答案复制到别处。</p>"
        "<script>"
        "function haifyConfirm(){"
        "var msg=document.getElementById('cf-msg');"
        "msg.textContent='提交中…';"
        "fetch('/confirm',{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:document.getElementById('cf-body').value})"
        ".then(function(r){return r.text().then(function(t){"
        "document.open();document.write(t);document.close();});})"
        ".catch(function(e){msg.textContent='提交失败：'+e;});}"
        "</script>"
    )


def render_draft_quality(drafts: Sequence[Dict[Text, Any]]) -> Text:
    """草稿质量状态表（**只读**；`drafts` 来自 `reviews.drafts_overview()`）。

    ★为什么必须出现在**看板**上（评审 §五 的"报告并列展示"）：只在 CLI 打印时，
    **只读交付物看不到**质量状态——而看板与离线导出（`export`）正是交付物。
    ★口径只有一个来源：本函数**不算**任何东西，`reviews.drafts_overview()` 给什么就显示什么
    （三处各自复算迟早不一致；不一致的看板等于没有看板）。
    """
    parts: List[Text] = ["<h2>草稿与质量状态</h2>"]

    if not drafts:
        parts.append(
            f'<p class="state-na">{STATE_NA}：<span class="mono">.ai/draft/</span> 下没有草稿'
            "——要么还没生成过，要么已经转正/清掉了。</p>"
        )
        return "".join(parts)

    rows: List[Text] = []
    for row in drafts:
        path = html.escape(str(row.get("path", "")))
        if row.get("error"):
            # ★读不到**不跳过**：跳过等于"这份不存在"，而事实是"它存在但读不了"
            rows.append(
                '<tr><td class="mono">{}</td><td colspan="4">读不到：{}</td></tr>'.format(
                    path, html.escape(str(row.get("error", "")))
                )
            )
            continue
        blockers = "、".join(str(item) for item in (row.get("blockers") or ())) or "—"
        resolved = "、".join(str(item) for item in (row.get("resolved_blockers") or ())) or "—"
        rows.append(
            '<tr><td class="mono">{}</td><td>{}</td><td>{}</td>'
            '<td class="mono">{}</td><td class="mono">{}</td></tr>'.format(
                path,
                "已留痕且哈希一致" if row.get("reviewed") else "待确认",
                html.escape(str(row.get("quality_render", ""))),
                html.escape(blockers),
                html.escape(resolved),
            )
        )

    parts.append(
        "<table><thead><tr><th>草稿</th><th>留痕</th><th>质量状态</th>"
        "<th>blocker</th><th>已点名处置</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    parts.append(
        '<p class="muted">★<span class="mono">rejected</span> 是**静态闸门的硬结论**'
        "（`--resolve-blocker` 覆盖不了，要改文档后重新生成）；"
        "每条 blocker 的原文与建议动作在 "
        '<span class="mono">.ai/draft/&lt;用例&gt;.unknowns.json</span>。'
        "转正：<span class=\"mono\">haify review --approve &lt;草稿&gt; --approver &lt;姓名&gt;"
        " [--resolve-blocker &lt;码&gt;]</span></p>"
    )
    return "".join(parts)


def render_pending_page(
    lists: Sequence[Dict[Text, Any]], drafts: Sequence[Dict[Text, Any]] = ()
) -> Text:
    """`GET /pending`：草稿质量状态 + 待确认清单 → 表单。

    ★页面**只给模板**，提交由人工点按钮触发：**不做自动提交**——
    留痕要带人的名字，自动提交等于把"谁确认的"降级成"谁碰过这个页面"。
    """
    parts: List[Text] = [
        render_draft_quality(drafts),
        '<div class="banner na"><b>待确认项是「语义判断」</b>——闸门不替用户下结论（§3.4）。'
        "填好后点「提交确认」，留痕会写进 <span class=\"mono\">.ai/reviews/</span>；"
        "回填内容也会原样列出来，**由你复制进文档或用例**。</div>",
    ]

    if not lists:
        parts.append(
            f'<p class="state-na">{STATE_NA}：<span class="mono">.ai/pending/</span> 下没有清单'
            "——要么还没生成过 PENDING，要么它们已经被处理掉了。</p>"
        )
        return page("待确认项", "".join(parts), subtitle="只读列出 + 一个提交模板；提交才落痕")

    for entry in lists:
        parts.append(f'<h2 class="mono">{html.escape(str(entry["path"]))}</h2>')
        if entry["error"]:
            # ★读不到**不跳过**：否则"没人要确认"与"我看不到要确认的东西"长得一样
            parts.append(
                f'<div class="banner"><b>读不到这份清单</b>：'
                f'{html.escape(str(entry["error"]))}</div>'
            )
            continue
        if not entry["items"]:
            parts.append(f'<p class="state-na">{STATE_NA}：清单是空的（count = 0）。</p>')
            continue

        rows = "".join(
            "<tr><td class=\"mono\">{}</td><td>{}</td><td class=\"mono\">{}</td><td>{}</td></tr>".format(
                html.escape(str(item.get("code", ""))),
                html.escape(str(item.get("severity", ""))),
                html.escape(str(item.get("where", ""))),
                html.escape(str(item.get("message", ""))),
            )
            for item in entry["items"]
            if isinstance(item, dict)
        )
        parts.append(
            "<table><thead><tr><th>code</th><th>severity</th><th>位置</th><th>说明</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
        parts.append(
            f'<p class="muted">用例名：<span class="mono">{html.escape(str(entry["case"]))}</span>'
            f"（{entry['count']} 条）</p>"
        )

        parts.append(
            confirm_form(
                {
                    "case": entry["case"],
                    "approver": "",
                    "decisions": [
                        {
                            "target": f"pending:{item.get('code', '?')}:{index}",
                            "action": "accept",
                            "reason": "",
                            "fill": {"expect": ""},
                        }
                        for index, item in enumerate(entry["items"])
                        if isinstance(item, dict)
                    ],
                    "notes": "",
                    "target_path": entry["path"],
                    "target_text": json.dumps(entry["items"], ensure_ascii=False, sort_keys=True),
                },
                label=f"提交 {entry['case']} 的确认",
            )
        )

    return page(
        "待确认项",
        "".join(parts),
        subtitle="只读列出 + 一个提交模板：**填完你的名字**，点提交才落痕",
    )


def render_fix_page(rel_path: Text, items: Sequence[Dict[Text, Any]]) -> Text:
    """`GET /fix?path=…`：把失败转成**逐条可裁决**的建议。

    ★页面把 `suggested` 与 **refused** 都摆出来——`fix` 拒绝给建议的那些条目
    （尤其"疑似真实缺陷"）是**结论**，不是"没做"：把它们藏起来会让人以为
    "这些失败没什么可看的"。
    """
    parts: List[Text] = []
    suggested = [item for item in items if item.get("suggested")]
    refused = [item for item in items if not item.get("suggested")]

    parts.append(
        f'<p>摘要：<span class="mono">{html.escape(rel_path)}</span>；'
        f"共 <b>{len(items)}</b> 条，其中给建议 <b>{len(suggested)}</b> 条、"
        f'<b>{len(refused)}</b> 条**拒绝给改**。</p>'
    )
    parts.append(
        '<div class="banner na">确认门**只记录**你的 accept / reject，'
        "**不落盘、不改用例**——要落地请自己贴 diff 并走 MR（§2.5）。"
        "<b>reject 必须写理由</b>。</div>"
    )

    if not items:
        parts.append(
            f'<p class="state-na">{STATE_NA}：这份摘要没有产生任何建议'
            "（要么没有失败断言，要么失败都不属于可自愈的类别）。</p>"
        )
        return page(f"自愈建议 · {rel_path}", "".join(parts))

    rows = "".join(
        "<tr><td class=\"mono\">{}</td><td>{}</td><td class=\"mono\">{}</td>"
        "<td class=\"mono\">{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(item.get("case", ""))),
            html.escape(str(item.get("step", ""))),
            html.escape(str(item.get("check", ""))),
            html.escape(str(item.get("kind", ""))),
            state_span(
                bool(item.get("suggested")), "给建议", "拒绝给改"
            ),
            html.escape(str(item.get("reason", ""))),
            html.escape(str(item.get("pointer", ""))),
        )
        for item in items
    )
    parts.append(
        "<table><thead><tr><th>用例</th><th>步骤</th><th>check</th><th>类别</th>"
        "<th>结论</th><th>理由</th><th>§5.8 指针</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )

    for item in suggested:
        if item.get("diff"):
            parts.append(f'<h2 class="mono">{html.escape(str(item.get("target")))}</h2>')
            parts.append(f'<pre class="editor">{html.escape(str(item["diff"]))}</pre>')
            if item.get("risk"):
                parts.append(f'<p class="muted">风险提示：{html.escape(str(item["risk"]))}</p>')

    parts.append(
        confirm_form(
            {
                "case": suggested[0].get("case", "") if suggested else "",
                "approver": "",
                "decisions": [
                    {"target": str(item.get("target")), "action": "accept", "reason": ""}
                    for item in items
                ],
                "notes": "",
                "target_path": rel_path,
                "target_text": json.dumps(
                    [{k: item.get(k) for k in ("target", "status", "check")} for item in items],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
            label="提交逐条裁决",
        )
    )
    return page(
        f"自愈建议 · {rel_path}",
        "".join(parts),
        subtitle="只读呈现建议；accept 也只是**记录**，不落盘",
    )


def render_confirm_result(result: Any) -> Text:
    """确认门的结果页。★**校验不过用 error 态**（422 的页面不装成成功）。"""
    ok = bool(getattr(result, "ok", False))
    parts: List[Text] = []

    if ok:
        parts.append(
            '<div class="banner pass"><b>留痕已写入</b>：'
            f'<span class="mono">{html.escape(str(getattr(result, "record_path", "")))}</span></div>'
        )
    else:
        parts.append(
            '<div class="banner"><b class="state-error">拒绝落痕（422）</b>——'
            "下面每一条都要先解决；**校验不过就一行都不写**（半份证据比没有更坏）。</div>"
        )
        items = "".join(
            f'<li>{html.escape(str(item))}</li>'
            for item in getattr(result, "errors", [])
        )
        parts.append(f"<ul>{items}</ul>")
        codes = getattr(result, "codes", [])
        if codes:
            parts.append(
                '<p class="muted">原因码：<span class="mono">'
                + html.escape(", ".join(str(item) for item in codes))
                + "</span></p>"
            )

    accepted = getattr(result, "accepted", [])
    rejected = getattr(result, "rejected", [])
    applied = getattr(result, "applied", [])

    parts.append(
        f"<h2>裁决</h2><p>接受 <b>{len(accepted)}</b> 条 / 拒绝 <b>{len(rejected)}</b> 条</p>"
    )
    for label, entries in (("接受", accepted), ("拒绝", rejected)):
        if entries:
            items = "".join(f'<li class="mono">{html.escape(str(x))}</li>' for x in entries)
            parts.append(f"<p>{label}：</p><ul>{items}</ul>")

    if applied:
        parts.append(
            "<h2>回填内容（**请自己复制**——看板不写进 `cases/`）</h2>"
            '<pre class="editor">'
            + html.escape("\n".join(str(line) for line in applied))
            + "</pre>"
        )

    parts.append(
        '<p class="muted">留痕落在 <span class="mono">.ai/reviews/</span>，'
        "并绑定到**那一版内容的哈希**（T24）：改一行 → 留痕失效，需要重新确认。</p>"
    )
    return page(
        "确认结果",
        "".join(parts),
        subtitle="留痕是**证据**：它记的是「谁、什么时候、对哪一版、同意了/拒绝了什么、为什么」",
    )


def render_jobs_page(jobs: Sequence[Dict[Text, Any]]) -> Text:
    """`GET /jobs`：作业列表（只读）+ 触发模板。

    ★页面把**退出码**当一列显示出来（R8）：触发运行的结果不能用"成功/失败"一个
    词概括——退出码才是能与 CLI 对齐的那个东西。
    """
    parts: List[Text] = [
        '<div class="banner na">这是本包**唯一**会执行代码的地方：闸门在'
        "**子进程之前**（运行域 → L3 → 超时），被拒时**零副作用**。"
        "★「执行了但用例失败」→ 页面仍是 **200**、退出码**显式展示**"
        "（「用例失败」不是「服务失败」）；只有**闸门拒绝**才是 **422**。</div>"
    ]

    if not jobs:
        parts.append(
            f'<p class="state-na">{STATE_NA}：还没有任何触发运行记录'
            '（<span class="mono">.ai/jobs/</span> 为空或不存在）。</p>'
        )
    else:
        rows = "".join(
            "<tr>"
            f'<td class="mono"><a href="/jobs?job={html.escape(str(item.get("job_id", "")))}">'
            f'{html.escape(str(item.get("job_id", "")))}</a></td>'
            f"<td>{state_span(bool(item.get('ok')), '成功', str(item.get('status') or '失败'))}</td>"
            f'<td class="mono">{html.escape(str(item.get("exit_code")))}</td>'
            f'<td class="mono">{html.escape(str(item.get("code", "")))}</td>'
            f"<td>{html.escape(str(item.get('triggered_by', '')))}</td>"
            f'<td class="mono">{html.escape(str(item.get("duration", "")))}</td>'
            "</tr>"
            for item in jobs
        )
        parts.append(
            "<table><thead><tr><th>作业号</th><th>状态</th><th>退出码</th><th>拒因码</th>"
            f"<th>触发者</th><th>耗时(s)</th></tr></thead><tbody>{rows}</tbody></table>"
        )

    parts.append(
        confirm_form(
            {
                "paths": ["cases/"],
                "base_url": "http://127.0.0.1:80",
                "label": "",
                "timeout": jobs_mod.DEFAULT_TIMEOUT,
                "triggered_by": "",
            },
            label="触发运行",
        )
    )
    parts.append(
        '<p class="muted">表单提交到 <span class="mono">POST /jobs</span>（'
        "<b>triggered_by 必填</b>）；跑完会把结果写进 "
        '<span class="mono">.ai/jobs/&lt;job_id&gt;/result.json</span> 并留痕。</p>'
    )
    return page("作业", "".join(parts), subtitle="只读列出 + 一个触发模板；闸门在真正执行之前")


def render_job_detail(job_id: Text, entry: Dict[Text, Any]) -> Text:
    """`GET /jobs?job=<id>`：单个作业的详情（**只读**，数据来自 `result.json`）。"""
    parts: List[Text] = [
        f'<p>作业号：<span class="mono">{html.escape(job_id)}</span></p>'
    ]

    if entry.get("error"):
        parts.append(
            f'<div class="banner"><b>读不到这份结果</b>：'
            f'{html.escape(str(entry["error"]))}</div>'
        )
        return page(f"作业 · {job_id}", "".join(parts))

    refused = bool(entry.get("refused"))
    parts.append(
        f'<p>状态：{state_span(bool(entry.get("ok")), "成功", str(entry.get("status") or "失败"))}'
        f"；退出码：<b class=\"mono\">{html.escape(str(entry.get('exit_code')))}</b>"
        f"；触发者：<b>{html.escape(str(entry.get('triggered_by', '') or '（未记）'))}</b></p>"
    )
    if refused:
        parts.append(
            f'<div class="banner"><b class="state-error">闸门拒绝</b>：'
            f'<span class="mono">{html.escape(str(entry.get("code", "")))}</span> —— '
            f'{html.escape(str(entry.get("reason", "")))}</div>'
        )
    if entry.get("l3_reason"):
        parts.append(f'<p class="muted">L3 闸门：{html.escape(str(entry["l3_reason"]))}</p>')

    if entry.get("cmd"):
        parts.append(
            f'<p>命令：<span class="mono">'
            f"{html.escape(' '.join(str(x) for x in entry['cmd']))}</span></p>"
        )

    for label, key in (("stdout", "stdout"), ("stderr", "stderr")):
        text = str(entry.get(key) or "")
        if not text:
            continue
        mark = "（**截尾**）" if entry.get(f"{key}_truncated") else ""
        parts.append(
            f"<h2>{label}{mark}</h2><pre class=\"editor\">{html.escape(text)}</pre>"
        )

    parts.append(
        '<p class="muted">原样下载：'
        f'<a class="mono" href="/file?path=.ai/jobs/{html.escape(job_id)}/result.json">'
        f".ai/jobs/{html.escape(job_id)}/result.json</a></p>"
    )
    return page(f"作业 · {job_id}", "".join(parts), subtitle="只读：本页只解析已存在的 result.json")


def render_job_result(result: Any, *, record_path: Text = "") -> Text:
    """`POST /jobs` 的结果页。★**闸门拒绝用 error 态**（422 的页面不装成成功）。"""
    refused = bool(getattr(result, "refused", False))
    ok = bool(getattr(result, "ok", False))
    parts: List[Text] = []

    if refused:
        parts.append(
            '<div class="banner"><b class="state-error">闸门拒绝（422）——**没有执行**</b>：'
            f'<span class="mono">{html.escape(str(getattr(result, "code", "")))}</span></div>'
            f"<p>{html.escape(str(getattr(result, 'reason', '')))}</p>"
            '<p class="muted">没建作业目录、没起子进程、连作业号都没分配。</p>'
        )
    else:
        # ★先算成变量再进 f-string：`f'...{"x" if ok else "y"}...'` 里嵌套引号
        #   在 3.12（PEP 701）才合法，而本包声明 `requires-python >= 3.8`。
        banner_class = "banner pass" if ok else "banner"
        state_class = "state-pass" if ok else "state-error"
        parts.append(
            f'<div class="{banner_class}">'
            f'<b class="{state_class}">退出码 '
            f"{html.escape(str(getattr(result, 'exit_code', '')))}</b>"
            f" —— {html.escape(str(result.status_text()))}</div>"
        )
        if getattr(result, "l3_reason", ""):
            parts.append(
                f'<p class="muted">L3 闸门：{html.escape(str(result.l3_reason))}</p>'
            )
        if getattr(result, "job_dir", ""):
            parts.append(
                f'<p>作业目录：<span class="mono">{html.escape(str(result.job_dir))}</span></p>'
            )

    if record_path:
        parts.append(f'<p>留痕：<span class="mono">{html.escape(record_path)}</span></p>')

    parts.append(
        "<h2>报告原文</h2><pre class=\"editor\">"
        + html.escape(jobs_mod.render_job_report(result))
        + "</pre>"
    )
    return page(
        "触发运行结果",
        "".join(parts),
        subtitle="退出码是能与 CLI 对齐的那个东西——所以它必须**写在页面上**（R8）",
    )


def render_audit_page(
    timeline: Any, summary: Dict[Text, Any], *, generated_at: Text
) -> Text:
    """`GET /audit`：审计时间线（**投影自既有证据**）。

    ★两条口径**必须写在页面上**（不是只写在代码注释里）：

    1. 这是**投影**，不是第二份日志 —— 每条都指向一个证据文件（带来源）；
    2. **时间未知的不排进时间轴** —— 它单独一栏。否则 `(无时间戳)` 的字典序
       比任何数字都小，混排会让未知**冒充"最早发生的事"**。

    ★`generated_at` 是**页面生成时刻**，与"证据里的时间"是两件事——
    页面上分开写，免得读者把"我现在看到的"当成"当时发生的"。
    """
    parts: List[Text] = [
        '<div class="banner na">本页是**投影**：每条都指向一个**既有证据文件**'
        "（`.ai/reviews/` · `.ai/jobs/` · `.ai/pending/`）。"
        "**不另写审计日志**——双源迟早不一致，而**不一致的审计等于没有审计**。"
        f'<br>本页读取时刻：<span class="mono">{html.escape(generated_at)}</span>'
        "（与下面每条的**证据时间**是两件事）。</div>"
    ]

    parts.append(
        "<table><thead><tr><th>事件总数</th><th>有时间戳</th><th>时间未知</th>"
        "<th>读不到</th><th>待确认未处理</th></tr></thead><tbody><tr>"
        f'<td>{summary.get("total", 0)}</td>'
        f'<td>{summary.get("timed", 0)}</td>'
        f'<td>{summary.get("untimed", 0)}</td>'
        f'<td>{summary.get("unreadable", 0)}</td>'
        f'<td>{summary.get("open_pending", 0)}</td>'
        "</tr></tbody></table>"
    )

    if summary.get("unreadable"):
        parts.append(
            '<div class="banner"><b class="state-error">有读不到的证据</b>'
            f'（{summary["unreadable"]} 条）——它**不是「没有」**，是**「异常」**，'
            "所以要单独列出来。</div>"
        )

    def _rows(events: Sequence[Any]) -> Text:
        if not events:
            return ""

        def _quality_cell(event: Any) -> Text:
            """质量列：有就显示；**审批类没有**就如实说"未记录"（不拿"没问题"填空）。"""
            recorded = str(getattr(event, "quality", "") or "")
            if recorded:
                return html.escape(recorded)
            if str(getattr(event, "kind", "")) == audit_mod.KIND_REVIEW:
                return "**未记录**（2026-09-26 之前的留痕还没有质量字段）"
            return "—"

        body = "".join(
            "<tr>"
            f'<td class="mono">{html.escape(str(event.when))}</td>'
            f"<td>{html.escape(str(event.kind))}/{html.escape(str(event.action))}</td>"
            f"<td>{html.escape(str(event.who))}</td>"
            f'<td class="mono">{html.escape(str(event.target))}</td>'
            f'<td class="mono">{html.escape(str(event.exit_code) if event.exit_code is not None else "")}</td>'
            f'<td class="mono">{html.escape(str(event.source))}</td>'
            f"<td>{_quality_cell(event)}</td>"
            f"<td>{html.escape(event.reason.replace(chr(10), ' / ')[:160])}</td>"
            "</tr>"
            for event in events
        )
        return (
            "<table><thead><tr><th>时间</th><th>来源/动作</th><th>谁</th><th>目标</th>"
            "<th>退出码</th><th>证据文件</th><th>质量</th><th>理由</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
        )

    untimed = _rows(list(getattr(timeline, "untimed", []) or []))
    if untimed:
        parts.append("<h2>时间未知</h2>")
        parts.append(
            '<p class="muted">★这一栏**刻意不排进时间轴**：`(无时间戳)` 的字典序比任何'
            "数字都小，混排会让它**冒充「最早发生的事」**。"
            "（清单类证据本来就没有时间字段——这里**不编一个「现在」**去填空。）</p>"
        )
        parts.append(untimed)

    timed = _rows(list(getattr(timeline, "timed", []) or []))
    if timed:
        parts.append("<h2>时间线（升序）</h2>")
        parts.append(timed)

    if not timed and not untimed:
        parts.append(
            f'<p class="state-na">{STATE_NA}：没读到任何证据'
            '（<span class="mono">.ai/</span> 下既没有确认留痕、也没有作业记录）。</p>'
        )

    return page(
        "审计时间线",
        "".join(parts),
        subtitle="投影自既有证据；每条都能指回它来自哪个文件",
    )


def render_error_page(status: int, headline: Text, detail: Text) -> Text:
    """错误页。★同样**响亮**：写清状态码与原因，不用一句"出错了"糊过去。"""
    body = (
        f'<div class="banner"><b class="state-error">{status} · {html.escape(headline)}</b></div>'
        f"<p>{html.escape(detail)}</p>"
        '<p class="muted">看板是只读的：它不会为了"看起来更好"而把拒绝渲染成空页面。</p>'
    )
    return page(f"{status} · {headline}", body)


# ---------------------------------------------------------------------------
# HTTP 层（stdlib `http.server`：零三方依赖，§2.5 行 350）
# ---------------------------------------------------------------------------

def check_host(host: Text) -> Tuple[bool, Text]:
    """`(是否回环, 说明)`——★对外监听必须**显式**，且必须把理由说出来。"""
    normalized = (host or "").strip().lower()
    if normalized in LOOPBACK_HOSTS:
        return True, f"{host} 是回环地址（默认值）"
    return False, (
        f"{host} **不是回环地址**——本服务自身没有鉴权，"
        "请确认前面有内网反向代理 + IP 白名单（§2.5 安全面 2：密钥与入口都在服务端）"
    )


# ---------------------------------------------------------------------------
# 加固（P3c）：IP 白名单 + Bearer token
# ---------------------------------------------------------------------------

TOKEN_HEADER = "Authorization"
TOKEN_SCHEME = "bearer"


def client_ip_of(handler: Any, *, trust_proxy: bool = False) -> Tuple[Text, Text]:
    """取**用于鉴权**的客户端地址 → `(ip, 说明)`。

    ★默认用 `client_address`（**真实对端**）。`X-Forwarded-For` 是**客户端能随便写**的
    一个头——拿它当白名单依据，等于**没有白名单**（`X-Forwarded-For: 127.0.0.1`
    就是一句话的事）。

    ★只有**显式** `trust_proxy=True` 才读那个头，而且只取**最后一跳（最右）**：
    反向代理通常往**末尾追加**，所以最右那格是"**反代看到的对端**"；
    最左那格是客户端**自称**的，可以伪造——它只对"反代自己写的第一格"有意义。
    """
    peer = ""
    try:
        peer = str(handler.client_address[0])
    except (AttributeError, IndexError, TypeError):
        peer = ""

    if not trust_proxy:
        return peer, f"真实对端 {peer}（**未**信任反代头）"

    raw = ""
    try:
        raw = str(handler.headers.get("X-Forwarded-For") or "")
    except AttributeError:
        raw = ""

    hops = [item.strip() for item in raw.split(",") if item.strip()]
    if not hops:
        return peer, f"真实对端 {peer}（`X-Forwarded-For` 缺失 → 回退到真实对端）"
    return hops[-1], f"`X-Forwarded-For` 最后一跳 {hops[-1]}（**已显式信任反代**）"


def ip_allowed(ip: Text, allowed: Sequence[Text]) -> bool:
    """IP 是否在白名单内。**空清单 = 不启用白名单**（放行）。

    ★"空 = 全放行"是有意为之，但它必须**看得见**：`serve()` 会把白名单的实际状态
    打印出来。一个**静默**生效的白名单与一个**不存在**的白名单，在出问题时
    是**分不清**的——而这两件事的处置完全不同。

    ★只做**精确匹配**，不做 CIDR：网段应该在**反向代理**那一层做
    （那里才知道真实的网络拓扑），在这里再实现一遍只会多一个"两处不一致"。
    """
    if not allowed:
        return True
    if not ip:
        return False
    return ip in allowed


def token_from_env(name: Text, env: Optional[Dict[Text, Text]] = None) -> Text:
    """从**环境变量**读 token。

    ★为什么不从命令行参数读：命令行会进 `ps` / 进 shell 历史 / 进 CI 日志。
    环境变量至少可以只读地注入（§2.5 安全面 2 的"只读密钥文件"精神）。
    """
    if not name:
        return ""
    source = os.environ if env is None else env
    return str(source.get(name, "") or "")


def extract_token(headers: Any) -> Text:
    """从请求头取 bearer token（`Authorization: Bearer <token>`）。"""
    raw = ""
    try:
        raw = str(headers.get(TOKEN_HEADER) or "")
    except AttributeError:
        return ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == TOKEN_SCHEME:
        return parts[1].strip()
    return ""


def check_client(
    ip: Text,
    provided: Text,
    *,
    allowed_ips: Sequence[Text] = (),
    expected_token: Text = "",
) -> Tuple[bool, Text]:
    """`(是否放行, 理由)`。★判据顺序：**IP 白名单 → token**。

    ★两个都用 `hmac.compare_digest` 比 token：普通 `==` 会在第一个不同字符处提前返回，
    时间差异可以被用来**逐字符猜**出 token（时序攻击）。这不是"过度设计"——
    代价只有一行 import。
    """
    import hmac  # noqa: PLC0415

    if not ip_allowed(ip, allowed_ips):
        return False, f"IP `{ip}` 不在白名单内（{', '.join(allowed_ips)}）"

    if expected_token:
        if not provided:
            return False, "缺少 Bearer token（`Authorization: Bearer …`）"
        if not hmac.compare_digest(provided, expected_token):
            return False, "Bearer token 不匹配"
    return True, ""



# 下载上限（§5.8 行 1001 的"只读"精神：不为"顺手能下载"而冒内存风险）
MAX_FILE_BYTES = 8 * 1024 * 1024


# 确认门提交体的上限（**只读**看板突然接受一块 body，必须先给自己划线）
MAX_CONFIRM_BYTES = 256 * 1024

# 校验不过的状态码：★**不是 200**（R8：失败不得被柔化成一个"成功的页面"）
CONFIRM_REFUSED_STATUS = 422


_CONTENT_TYPES = {
    ".json": "application/json; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
}


class WebHandler(BaseHTTPRequestHandler):
    """只读 handler：**只有 `do_GET` / `do_HEAD`**。

    ★为什么还要**显式**实现 `do_POST`/`do_PUT`/`do_PATCH`/`do_DELETE` 去回 405：
    `BaseHTTPRequestHandler` 对没实现的方法会自己回 **501 Not Implemented**。
    501 读起来是"这个服务**不认得**这个方法"（像缺功能，像待开发），
    而我们要说的是"这个服务**不接受写**"（是**设计**，不是缺失）。
    两句话对使用者意味着完全不同的下一步，所以必须是 405 + `Allow: GET, HEAD`。
    """

    server_version = "haify-readonly"
    sys_version = ""

    # 覆盖掉默认实现，避免把每个请求都打到 stderr（看板是常驻的）
    def log_message(self, fmt: Text, *args: Any) -> None:  # noqa: A003
        return

    # ---- 响应助手（唯一出口，保证每个响应都带同样的安全头）------------------
    def _respond(
        self,
        status: int,
        body: bytes,
        content_type: Text,
        *,
        extra: Sequence[Tuple[Text, Text]] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(
        self, status: int, text: Text, *, extra: Sequence[Tuple[Text, Text]] = ()
    ) -> None:
        self._respond(
            status, text.encode("utf-8"), "text/html; charset=utf-8", extra=extra
        )

    def _plain(self, status: int, text: Text) -> None:
        self._respond(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _refuse_write(self) -> None:
        """写方法一律 405（**明确拒绝**，不是 501）。"""
        self._respond(
            405,
            render_error_page(
                405,
                "本服务不接受写",
                f"收到 {self.command}；看板只实现 GET / HEAD。"
                "任何产物变更都必须走 CLI 与人工确认（§2.5：CLI 是唯一执行面）",
            ).encode("utf-8"),
            "text/html; charset=utf-8",
            extra=(("Allow", "GET, HEAD"),),
        )

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        # ★`/confirm` 与 `/jobs` 是**唯一**接受写的两条路径；其余一律 405。
        # 注意：即使在这里，`WebHandler` 自己**也不写盘**——落痕委托
        # `confirm.confirm` → `reviews.write_review_record`（T18 在册的写入者），
        # 作业落盘委托 `jobs.write_job_record`（同样在册）。
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if route == "/confirm":
            self._route_confirm()
            return
        if route == "/jobs":
            self._route_jobs_post()
            return
        self._refuse_write()

    def do_PUT(self) -> None:  # noqa: N802
        self._refuse_write()

    def do_PATCH(self) -> None:  # noqa: N802
        self._refuse_write()

    def do_DELETE(self) -> None:  # noqa: N802
        self._refuse_write()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._respond(
            204, b"", "text/plain; charset=utf-8", extra=(("Allow", "GET, HEAD"),)
        )

    # ---- 加固（P3c）：IP 白名单 + Bearer token ------------------------------
    def _authorized(self) -> bool:
        """鉴权。**默认全放行**（回环 + 未配鉴权 = 本地开发形态）；配了才生效。

        ★"默认不拦"是有依据的：默认只监听回环，且默认**没有配**任何鉴权项——
        此时拦住所有请求只会让"在本地看一眼产物"变成麻烦事，而它本来就不是对外服务。

        ★失败时**区分 401 / 403**（本仓一贯"响亮"）：
        - **缺凭据**（没有 token）→ **401** + `WWW-Authenticate: Bearer`；
        - **有凭据但不够**（IP 不在白名单 / token 不匹配）→ **403**。
        这两种情况的下一步完全不同：前者去补凭据，后者去要权限。
        """
        server = self.server
        ip, ip_note = client_ip_of(
            self, trust_proxy=bool(getattr(server, "trust_proxy", False))
        )
        allowed, why = check_client(
            ip,
            extract_token(self.headers),
            allowed_ips=getattr(server, "allowed_ips", ()),
            expected_token=getattr(server, "expected_token", ""),
        )
        if allowed:
            return True

        status = 401 if ("token" in why and "缺少" in why) else 403
        extra: Tuple[Tuple[Text, Text], ...] = (
            (("WWW-Authenticate", "Bearer"),) if status == 401 else ()
        )
        body = page(
            f"{status} · 拒绝访问",
            f'<div class="banner"><b class="state-error">{status} · 拒绝访问</b></div>'
            f"<p>{html.escape(why)}</p>"
            f'<p class="muted">判定依据：{html.escape(ip_note)}</p>',
        )
        self._respond(
            status,
            body.encode("utf-8"),
            "text/html; charset=utf-8",
            extra=extra,
        )
        return False

    # ---- 路由 -------------------------------------------------------------
    def _read_roots(self) -> Sequence[Text]:
        return tuple(getattr(self.server, "read_roots", ALLOWED_ROOTS))

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        roots = self._read_roots()

        if route == "/":
            self._html(
                200,
                render_index(iter_runs(roots), metrics_payload(roots)),
            )
            return
        if route == "/metrics":
            self._html(200, render_metrics(metrics_payload(roots), failure_trend(roots)))
            return
        if route == "/run":
            self._route_run(query, roots)
            return
        if route == "/file":
            self._route_file(query, roots)
            return
        if route == "/pending":
            # ★口径只此一份：`reviews.drafts_overview()`（CLI / 看板 / 离线导出共用）
            self._html(
                200,
                render_pending_page(
                    confirm_mod.load_pending_lists(), reviews_mod.drafts_overview()
                ),
            )
            return
        if route == "/fix":
            self._route_fix(query, roots)
            return
        if route == "/jobs":
            self._route_jobs(query)
            return
        if route == "/audit":
            timeline = audit_mod.audit_timeline()
            self._html(
                200,
                render_audit_page(
                    timeline,
                    audit_mod.audit_summary(timeline),
                    generated_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                ),
            )
            return
        self._html(
            404,
            render_error_page(
                404,
                "没有这个页面",
                f"路径 {parsed.path}。可用：`/`、`/metrics`、`/run?path=…`、`/file?path=…`、"
                "`/pending`、`/fix?path=…`、`/jobs[?job=…]`，以及 `POST /confirm`、`POST /jobs`",
            ),
        )

    # ★HEAD 与 GET 同一条路：`_respond` 里只在非 HEAD 时写 body，
    #   所以头（含 Content-Length）与 GET 完全一致——这正是 HEAD 的语义。
    do_HEAD = do_GET

    def _route_run(self, query: Dict[Text, List[Text]], roots: Sequence[Text]) -> None:
        rel = (query.get("path") or [""])[0]
        if not rel:
            self._html(
                400,
                render_error_page(
                    400, "缺少 path 参数", "用法：`/run?path=logs/xxx.summary.json`"
                ),
            )
            return
        payload, _root, why = load_summary(rel, roots)
        rows, problems = ([], []) if why else extract_assertions(payload)
        # ★读不到 → **404**，但页面照常渲染（顶部写着读不到的原因）：
        #   不把它渲染成一个"空的正常页"，因为那会让人以为"这份用例没问题"。
        self._html(404 if why else 200, render_run(rel, payload, rows, problems, load_error=why))

    def _route_file(self, query: Dict[Text, List[Text]], roots: Sequence[Text]) -> None:
        rel = (query.get("path") or [""])[0]
        if not rel:
            self._html(
                400,
                render_error_page(400, "缺少 path 参数", "用法：`/file?path=reports/analysis.md`"),
            )
            return

        # ★「越界」与「不存在」**分开报**——两者的下一步完全不同：
        #   越界 = 你在要一个不该给的东西（别再试了）；不存在 = 路径合规但产物没生成
        try:
            target = resolve_read_path(rel, roots)
        except PathRefused as refused:
            self._html(
                404,
                render_error_page(
                    404,
                    "拒绝这个路径（不在只读读根内）",
                    f"{refused}。白名单根之外的文件一概不提供——"
                    "**包括同前缀的兄弟目录**（`logs_evil/` 不是 `logs/`）。",
                ),
            )
            return
        if not os.path.isfile(target):
            self._html(
                404,
                render_error_page(
                    404,
                    "文件不存在",
                    f"`{rel}` 的路径是**合规**的，但磁盘上没有这个文件"
                    "（很可能这个产物还没生成）。",
                ),
            )
            return

        try:
            size = os.path.getsize(target)
        except OSError as error:
            self._plain(404, f"读取失败：{error}")
            return
        if size > MAX_FILE_BYTES:
            self._html(
                413,
                render_error_page(
                    413,
                    "文件太大，看板不发送",
                    f"{rel} 有 {size} 字节，超过上限 {MAX_FILE_BYTES}。"
                    "请到磁盘上直接取——看板不为「顺手能下载」而冒内存风险。",
                ),
            )
            return

        with open(target, "rb") as fp:
            data = fp.read()
        suffix = os.path.splitext(target)[1].lower()
        self._respond(
            200,
            data,
            _CONTENT_TYPES.get(suffix, "application/octet-stream"),
            extra=(("Content-Disposition", "inline"),),
        )


    def _route_fix(self, query: Dict[Text, List[Text]], roots: Sequence[Text]) -> None:
        """`GET /fix?path=…`：把一份摘要的失败转成**逐条可裁决**的建议（只读）。"""
        rel = (query.get("path") or [""])[0]
        if not rel:
            self._html(
                400,
                render_error_page(
                    400, "缺少 path 参数", "用法：`/fix?path=logs/xxx.summary.json`"
                ),
            )
            return
        payload, _root, why = load_summary(rel, roots)
        if why:
            self._html(404, render_error_page(404, "读不到这份摘要", why))
            return
        self._html(200, render_fix_page(rel, confirm_mod.fix_suggestions(payload)))

    def _route_confirm(self) -> None:
        """`POST /confirm`：确认门（**唯一**接受写的路径）。

        ★三种结果的状态码**刻意不同**：
        - 校验不过 → **422**（不是 200——R8 说失败不得被柔化成一个"成功的页面"）；
        - body 超过上限 → **413**；
        - 落痕成功 → **200**。

        ★本方法**不写盘、不执行**：落痕委托 `confirm.confirm` → `reviews`（已登记的写入者）。
        """
        raw, replied = self._read_body()
        if replied:
            return

        result = confirm_mod.confirm(raw or b"")
        self._html(
            200 if result.ok else CONFIRM_REFUSED_STATUS,
            render_confirm_result(result),
        )


    def _read_body(self) -> Tuple[Optional[bytes], bool]:
        """读请求体。返回 `(body, 是否已回复且应停止)`。

        ★超限时**先把 body 读掉再回**（分块丢弃，不占内存）：若直接关连接，
        客户端还在写 body，它会拿到 `ConnectionResetError` 而不是 413——
        那是"响亮报错"的**反面**：我们明明有话说，却说不出。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0

        if length > MAX_CONFIRM_BYTES:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._html(
                413,
                render_error_page(
                    413,
                    "提交体太大",
                    f"{length} 字节超过上限 {MAX_CONFIRM_BYTES}（body 已被读掉丢弃）。"
                    "看板不为「一次提交一整棵树」而放开这件事。",
                ),
                extra=(("Connection", "close"),),
            )
            return None, True

        return (self.rfile.read(length) if length > 0 else b""), False

    def _route_jobs(self, query: Dict[Text, List[Text]]) -> None:
        """`GET /jobs[?job=<id>]`：作业列表 / 单个作业详情（**只读**）。"""
        job_id = (query.get("job") or [""])[0]
        if job_id:
            entry = jobs_mod.read_job(job_id)
            if entry is None:
                self._html(
                    404,
                    render_error_page(
                        404,
                        "没有这个作业",
                        f"job_id：`{job_id}`（只接受 `[\\w.-]` 形态；找不到就是找不到）",
                    ),
                )
                return
            self._html(200, render_job_detail(job_id, entry))
            return
        self._html(200, render_jobs_page(jobs_mod.list_jobs()))

    def _route_jobs_post(self) -> None:
        """`POST /jobs`：触发一次运行（**第二条**接受写的路径）。

        ★流程顺序（**执行之后再留痕**，与确认门**相反**）：
        ① 解析 body → `JobRequest`；② `jobs.run_job`（闸门在**子进程之前**，
        被拒则零副作用）；③ 把**结果**（退出码 / 拒因 / job_id）写进留痕。

        ★为什么这次留痕在**后**：确认门的留痕对象是「**人的裁决**」——裁决在动作
        之前就完整了，所以先记；触发运行的留痕对象是「**这次运行**」——运行结束前它
        是不完整的。若先记后跑，遇到"闸门拒绝"就会留下一条"确认过了"而**什么都
        没发生**的留痕——那比不记更坏（它会让人以为跑过）。
        ★"跑了但没记"这个窗口由 `result.json` 兜底：里面有 `triggered_by`，
        两条记录互为备份。

        ★状态码口径：
        - **闸门拒绝** → **422**（输入不合法：路径不在运行域 / 沙盒没开 / 超时越界）；
        - **执行了**（**无论退出码是不是 0**）→ **200**：服务端完成了它的工作，
          退出码在页面上**显式展示**（与 CLI 的语义一致——"用例失败"不是"服务失败"）。
        """
        raw, replied = self._read_body()
        if replied:
            return

        try:
            payload = json.loads((raw or b"").decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError) as error:
            self._html(
                CONFIRM_REFUSED_STATUS,
                render_error_page(
                    CONFIRM_REFUSED_STATUS, "提交体不是合法 JSON", str(error)
                ),
            )
            return

        request = jobs_mod.JobRequest.from_dict(payload)
        if not request.triggered_by.strip():
            # ★复用确认门的纪律：**没有触发者就不跑**（跑了也没人可问）
            self._html(
                CONFIRM_REFUSED_STATUS,
                render_error_page(
                    CONFIRM_REFUSED_STATUS,
                    "缺少 triggered_by",
                    "触发运行同样是**人工动作**：没有「谁触发的」，结果就没有责任人。"
                    "（这与确认门要求 `approver` 是同一条纪律。）",
                ),
            )
            return

        result = jobs_mod.run_job(request)

        record_path = ""
        if result.job_id:
            record = confirm_mod.confirm(
                {
                    "case": result.job_id,
                    "approver": request.triggered_by,
                    "decisions": [
                        {
                            "target": "job:" + ",".join(request.paths),
                            "action": confirm_mod.ACTION_ACCEPT,
                            "reason": request.label,
                        }
                    ],
                    "notes": (
                        f"触发运行：{'成功' if result.ok else '失败'} / "
                        f"{result.status_text()}；退出码 {result.exit_code}"
                        + (f"；L3：{result.l3_reason}" if result.l3_reason else "")
                    ),
                    "target_path": result.job_dir or "(未执行)",
                    "target_text": json.dumps(
                        result.to_dict(), ensure_ascii=False, sort_keys=True
                    ),
                }
            )
            record_path = record.record_path

        self._html(
            CONFIRM_REFUSED_STATUS if result.refused else 200,
            render_job_result(result, record_path=record_path),
        )


class ReadOnlyServer(ThreadingHTTPServer):
    """带**读根清单 + 加固配置**的只读服务（handler 从 `server.*` 取，不自己猜）。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Tuple[Text, int],
        handler_class: Any,
        roots: Sequence[Text],
        *,
        allowed_ips: Sequence[Text] = (),
        expected_token: Text = "",
        trust_proxy: bool = False,
    ) -> None:
        self.read_roots: Tuple[Text, ...] = tuple(roots)
        self.allowed_ips: Tuple[Text, ...] = tuple(allowed_ips)
        self.expected_token: Text = expected_token
        self.trust_proxy: bool = bool(trust_proxy)
        super().__init__(server_address, handler_class)


def make_server(
    host: Text = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    roots: Sequence[Text] = ALLOWED_ROOTS,
    *,
    allowed_ips: Sequence[Text] = (),
    expected_token: Text = "",
    trust_proxy: bool = False,
) -> ReadOnlyServer:
    """建服务（**不阻塞**）。★读根在**启动时**就校验：违规的读根启动即失败。

    为什么放在启动时：读根是"白名单"，白名单里混进一条越界项（如 `../etc`），
    等到某个请求恰好命中它才暴露——那时已经晚了。启动即拒，比运行时才发现好。
    """
    for root in roots:
        safe_rel_path(".", root)  # 越界读根 → PathRefused（启动即失败）
    return ReadOnlyServer(
        (host, port),
        WebHandler,
        roots,
        allowed_ips=allowed_ips,
        expected_token=expected_token,
        trust_proxy=trust_proxy,
    )


def serve(
    host: Text = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    roots: Sequence[Text] = ALLOWED_ROOTS,
    *,
    allowed_ips: Sequence[Text] = (),
    token_env: Text = "",
    trust_proxy: bool = False,
) -> int:
    """前台常驻（供 `haify serve`）。Ctrl-C 退出。

    ★`token_env` 是**环境变量的名字**，不是 token 本身——token 走命令行会进
    `ps` / shell 历史 / CI 日志。**服务端读它，且从不把它发给前端**。
    """
    is_loopback, why = check_host(host)
    print(f"[haify serve] {why}", flush=True)

    token = token_from_env(token_env)
    print(
        "[haify serve] 加固 · IP 白名单："
        + (f"**已启用** {list(allowed_ips)}" if allowed_ips else "**未启用**"),
        flush=True,
    )
    print(
        "[haify serve] 加固 · Bearer token："
        + (
            f"**已启用**（来自环境变量 `{token_env}`；值不会出现在任何页面上）"
            if token
            else ("**未启用**" if not token_env else f"**未启用**（环境变量 `{token_env}` 是空的）")
        ),
        flush=True,
    )
    if trust_proxy:
        print(
            "[haify serve] 加固 · 反代头：**已信任** `X-Forwarded-For` 的**最后一跳**"
            "（只有确认「前面确实是我们的反代」时才该这么开）",
            flush=True,
        )
    else:
        print(
            "[haify serve] 加固 · 反代头：**不信任**（用真实对端地址；"
            "`X-Forwarded-For` 是客户端能随便写的）",
            flush=True,
        )

    if not is_loopback and not (allowed_ips or token):
        print(
            "[haify serve] ★★ 警告：正在**对外监听**，却**既没有 IP 白名单、也没有 token**"
            "——请确认前面确实有反向代理 + 鉴权，否则任何人都能读你的产物",
            flush=True,
        )

    httpd = make_server(
        host,
        port,
        roots,
        allowed_ips=allowed_ips,
        expected_token=token,
        trust_proxy=trust_proxy,
    )
    print(f"[haify serve] 只读看板：http://{host}:{port}/（Ctrl-C 退出）", flush=True)
    print(
        f"[haify serve] 读根：{', '.join(roots)}；"
        "**零写盘、零执行**（GET/HEAD + `/confirm` · `/jobs` 两条受控写入）",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[haify serve] 收到 Ctrl-C，正在关闭", flush=True)
    finally:
        httpd.server_close()
    return 0


# ---------------------------------------------------------------------------
# 自检（**不写盘、不起常驻服务**：临时工作区里起一个短命服务，测完即关）
# ---------------------------------------------------------------------------

# 目录穿越样本（**每一条对应一种真实攻击面**，不是凑数）
TRAVERSAL_SAMPLES: Tuple[Text, ...] = (
    "../interfacetester/parser.py",
    "../../etc/passwd",
    "/etc/passwd",
    "C:/Windows/win.ini",
    "..%2finterfacetester%2fparser.py",
    "a/../../outside.txt",
    "..\\..\\outside.txt",
)

# 自检夹具（★**内存夹具，不落盘**——产线模块里不出现写盘调用，T18 纪律）：
# 字段路径**逐字照实测的 `summary.json`**，含 `.data.validators.validate_extractor`（§5.5 那个指针）
_FIXTURE_SUMMARY: Dict[Text, Any] = {
    "success": False,
    "stat": {
        "testcases": {"fail": 1, "success": 1, "total": 2},
        "teststeps": {"failures": 1, "successes": 1, "total": 2},
    },
    "time": {"duration": 0.5, "start_at": 1.0},
    "platform": {
        "interfacetester_version": "5.0.1",
        "platform": "fixture",
        "python_version": "3",
    },
    "details": [
        {
            "name": "用例A",
            "success": True,
            "case_id": "c1",
            "time": {},
            "in_out": {},
            "log": "",
            "records": [
                {
                    "name": "步骤1",
                    "step_type": "request",
                    "success": True,
                    "data": {
                        "success": True,
                        "req_resps": [],
                        "stat": {},
                        "address": "http://127.0.0.1:80/get",
                        "validators": {
                            "validate_extractor": [
                                {
                                    "check": "status_code",
                                    "check_result": "pass",
                                    "check_value": 200,
                                    "comparator": "equal",
                                    "expect": 200,
                                    "expect_value": 200,
                                    "message": "",
                                }
                            ]
                        },
                    },
                    "elapsed": 1,
                    "content_size": 0,
                    "export_vars": {},
                    "attachment": "",
                }
            ],
        },
        {
            "name": "用例B",
            "success": False,
            "case_id": "c2",
            "time": {},
            "in_out": {},
            "log": "",
            "records": [
                {
                    "name": "步骤2",
                    "step_type": "request",
                    "success": False,
                    "data": {
                        "success": False,
                        "req_resps": [],
                        "stat": {},
                        "address": "http://127.0.0.1:80/get",
                        "validators": {
                            "validate_extractor": [
                                {
                                    "check": "body.user",
                                    "check_result": "fail",
                                    "check_value": "alice",
                                    "comparator": "equal",
                                    "expect": "bob",
                                    "expect_value": "bob",
                                    "message": "值不相等",
                                }
                            ]
                        },
                    },
                    "elapsed": 2,
                    "content_size": 0,
                    "export_vars": {},
                    "attachment": "",
                }
            ],
        },
    ],
}


def fetch(
    port: int,
    path: Text,
    method: Optional[Text] = None,
    body: Optional[bytes] = None,
    headers: Optional[Dict[Text, Text]] = None,
) -> Tuple[int, bytes]:
    """打一个**本机回环**请求（自检用——只在 `127.0.0.1` 上，绝不打外部地址）。

    ★`method` 默认 **`None`**（而不是 `"GET"`），然后按"有没有 body"推：
    有 body → POST，没有 → GET。这不是"贴心"，是在堵一个**很难看出来的坑**：
    如果默认写死 `"GET"`，那 `fetch(..., body=…)` 会发出一个「**带 body 的 GET**」——
    urllib 照发、服务端照收，但 POST 路由**根本没被碰到**，现象是 **404**。
    本次自检就踩了这一下：`/confirm` 明明实现了，却一直报"期望 422，实得 404"，
    而单跑一次 POST 又是好的。
    """
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    if method is None:
        method = "POST" if body is not None else "GET"

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method=method
    )
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def fetch_status_headers(
    port: int,
    path: Text,
    method: Optional[Text] = None,
    body: Optional[bytes] = None,
    headers: Optional[Dict[Text, Text]] = None,
) -> Tuple[int, Dict[Text, Text]]:
    """同 `fetch`，但**连响应头一起返回**。

    ★鉴权判据要验 `WWW-Authenticate`——那是**响应头**，只看 body 是验不到的
    （body 里写一句"该带 token"很容易，而"真的发了那个头"才是 HTTP 契约）。
    """
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    if method is None:
        method = "POST" if body is not None else "GET"

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method=method
    )
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, {key: value for key, value in response.headers.items()}
    except urllib.error.HTTPError as error:
        return error.code, {key: value for key, value in (error.headers or {}).items()}


def snapshot_all(root: Text) -> Dict[Text, Tuple[int, int]]:
    """**全量**快照：`相对路径 → (size, mtime_ns)`，**含 `.ai/`、`reports/`、`logs/`**。

    ★为什么不复用 `workdir.snapshot_tree`（它就在隔壁，而且已经有自检）：

    那个函数是为「**L2 作业污染源码树**」设计的，所以它**故意**把 `.ai/`、`logs/`
    这类运行期产物目录从快照里排除（它自己的自检里写着"`.ai/` 与 `logs/` 等运行期
    产物目录不参与快照比对"，因为那里"本来就该变"）。

    而看板的"零写盘"要监测的**恰恰就是这些目录**——看板能碰到的只有 `.ai/`、
    `reports/`、`logs/`。若用它的忽略清单，判据就把**全部可触碰的地方**排除在外，
    测出来的"零写盘"是**空的**（一个永远为真的护栏，比没有护栏更坏）。

    所以：目的相反 → 快照口径必须不同。**这是另一件事，不是重造**。
    本函数只忽略 `__pycache__`（字节码缓存与看板无关）。
    """
    root_abs = os.path.abspath(root)
    snapshot: Dict[Text, Tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            try:
                info = os.stat(full)
            except OSError:
                continue  # 快照期间被删掉的文件不算差异
            rel = os.path.relpath(full, root_abs).replace(os.sep, "/")
            snapshot[rel] = (info.st_size, info.st_mtime_ns)
    return snapshot


def diff_all(
    before: Dict[Text, Tuple[int, int]], after: Dict[Text, Tuple[int, int]]
) -> List[Text]:
    """全量快照的差异（新增 / 删除 / 修改），按路径排序便于定位。"""
    problems: List[Text] = []
    for path in sorted(set(after) - set(before)):
        problems.append(f"新增 {path}")
    for path in sorted(set(before) - set(after)):
        problems.append(f"删除 {path}")
    for path in sorted(set(before) & set(after)):
        if before[path] != after[path]:
            problems.append(f"修改 {path}")
    return problems


def run_selftest(verbose: bool = False) -> int:
    """web 自检。★**不写盘**——产线模块里**不出现**任何写盘调用（T18 纪律）。

    ★为什么自检**不自己造夹具**：本模块纪律之一就是"零写盘"，而"造一份
    `logs/x.summary.json` 当夹具"恰恰是写盘。**产线模块里出现写调用就会被 T18
    扫描器判违规**——它无法静态区分"写到临时目录"与"写到工作区"，所以从严。
    于是自检只用**内存数据** + **已存在的产物**（有就验、没有就如实说跳过）；
    需要真文件夹具的用例统一放 `tests/web_readonly_test.py`（**夹具写入归 tests/**）。

    ★零写盘的判据反而因此更硬：它跑在**真实工作区**上，而不是"我们自己造的那几个文件"。
    """
    import ast as ast_module  # noqa: PLC0415
    import threading  # noqa: PLC0415
    from urllib.parse import quote  # noqa: PLC0415

    failures: List[Text] = []
    notes: List[Text] = []
    server: Optional[ReadOnlyServer] = None
    thread: Optional[Any] = None

    # ===== ① 零写盘 + ⑦ HTTP 契约：取快照 → 跑遍页面 → 再对账 ==============
    before = snapshot_all(".")
    if not before:
        failures.append("[零写盘] 全量快照是空的——这条判据会变成永远为真")

    try:
        server = make_server(DEFAULT_HOST, 0, ALLOWED_ROOTS)  # 端口 0 = 系统分配
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        for page in ("/", "/metrics", "/nope", "/run", "/file"):
            fetch(port, page)

        if fetch(port, "/nope")[0] != 404:
            failures.append("[HTTP] 未知路径应回 404")
        if fetch(port, "/run")[0] != 400:
            failures.append("[HTTP] `/run` 缺 path 应回 400")
        if fetch(port, "/file")[0] != 400:
            failures.append("[HTTP] `/file` 缺 path 应回 400")
        # 合规路径但文件不存在 → 404（两种情况都必须"响亮"，见 `_route_file`）
        if fetch(port, "/run?path=logs/definitely_not_here.summary.json")[0] != 404:
            failures.append("[HTTP] 合规路径但文件不存在，应回 404")

        # ★写方法必须**明确拒绝**（405 + Allow），不是让默认实现回 501
        for method in _WRITE_METHODS:
            status, body = fetch(port, "/", method=method)
            if status != 405:
                failures.append(f"[HTTP] {method} 期望 405（不接受写），实得 {status}")
            if status == 405 and b"405" not in body:
                failures.append(f"[HTTP] {method} 的 405 页面没写清状态码")

        status, body = fetch(port, "/", method="HEAD")
        if status != 200 or body != b"":
            failures.append(
                f"[HTTP] HEAD 应回 200 且 body 为空，实得 {status} / {len(body)} 字节"
            )

        # ===== P3b-1 确认门：`/confirm` 是**唯一**接受写的路径 ================
        # ★证明那个例外**没有扩散**：其余路径的写方法仍必须 405
        for path in ("/", "/metrics", "/pending", "/fix", "/run", "/file"):
            for method in _WRITE_METHODS:
                status, _body = fetch(port, path, method=method)
                if status != 405:
                    failures.append(
                        f"[写边界] {method} {path} 期望 405，实得 {status}"
                        "——`/confirm` 的例外不该扩散到别的路径"
                    )

        # ★坏 body → **422**（不是 200：失败不得被柔化成"成功的页面"）。
        #   自检**只用坏 body**：合规提交会落痕（写盘），那归 tests/。
        for bad_body in (b"", b"{ not json", b"\xff\xfe"):
            status, body = fetch(port, "/confirm", body=bad_body)
            if status != CONFIRM_REFUSED_STATUS:
                failures.append(
                    f"[确认门] 坏 body {bad_body!r} 期望 {CONFIRM_REFUSED_STATUS}，实得 {status}"
                )
            if "拒绝落痕".encode("utf-8") not in body:
                failures.append("[确认门] 422 的页面没写清「拒绝落痕」")

        # ★缺 approver 的"看起来合规"的 body → 也必须 422（不是 200）
        status, _body = fetch(
            port,
            "/confirm",
            body=json.dumps(
                {"case": "x", "approver": "", "decisions": [{"target": "t", "action": "accept"}]}
            ).encode("utf-8"),
        )
        if status != CONFIRM_REFUSED_STATUS:
            failures.append(f"[确认门] 缺 approver 期望 {CONFIRM_REFUSED_STATUS}，实得 {status}")

        # `GET /pending`：清单不存在也要 200（空清单是**正常状态**）
        status, body = fetch(port, "/pending")
        if status != 200:
            failures.append(f"[待确认] `/pending` 期望 200（空清单也正常），实得 {status}")
        if "待确认项".encode("utf-8") not in body:
            failures.append("[待确认] `/pending` 没有渲染出页面标题")

        # `GET /fix` 缺 path → 400
        if fetch(port, "/fix")[0] != 400:
            failures.append("[自愈] `/fix` 缺 path 应回 400")

        # ===== P3b-2 触发运行：`/jobs` 的只读面 + 写边界 ====================
        status, _body = fetch(port, "/jobs")
        if status != 200:
            failures.append(f"[作业] `/jobs` 期望 200（空列表也正常），实得 {status}")

        if fetch(port, "/jobs?job=run-19700101-000000-1-0001")[0] != 404:
            failures.append("[作业] 不存在的 job_id 应回 404")

        # ★缺 triggered_by → 422（与确认门要求 approver 是**同一条**纪律）
        status, body = fetch(
            port, "/jobs", body=json.dumps({"paths": ["cases/x.yml"]}).encode("utf-8")
        )
        if status != CONFIRM_REFUSED_STATUS:
            failures.append(
                f"[作业] 缺 triggered_by 期望 {CONFIRM_REFUSED_STATUS}，实得 {status}"
            )
        if b"triggered_by" not in body:
            failures.append("[作业] 缺 triggered_by 的页面没点明缺的是它")

        status, _body = fetch(port, "/jobs", body=b"{ not json")
        if status != CONFIRM_REFUSED_STATUS:
            failures.append(f"[作业] 坏 JSON 期望 {CONFIRM_REFUSED_STATUS}，实得 {status}")

        # ★★ 闸门拒绝 → 422，且**零副作用**（`.ai/jobs/` 前后必须不变）
        before_jobs = set(os.listdir(".ai/jobs")) if os.path.isdir(".ai/jobs") else set()
        status, body = fetch(
            port,
            "/jobs",
            body=json.dumps(
                {"paths": ["cases/x.yml"], "triggered_by": "selfcheck"}
            ).encode("utf-8"),
        )
        if status != CONFIRM_REFUSED_STATUS:
            failures.append(
                f"[作业] 沙盒未开时触发期望 {CONFIRM_REFUSED_STATUS}（闸门拒绝），实得 {status}"
            )
        if "没有执行".encode("utf-8") not in body:
            failures.append("[作业] 被拒的页面没写清「没有执行」")
        after_jobs = set(os.listdir(".ai/jobs")) if os.path.isdir(".ai/jobs") else set()
        if after_jobs != before_jobs:
            failures.append(
                f"[作业·零副作用] 被拒之后 `.ai/jobs/` 变了：{sorted(after_jobs - before_jobs)}"
            )

        # ===== ③ 目录穿越（函数级 + 真实 HTTP 各来一遍）=====================
        for sample in TRAVERSAL_SAMPLES:
            try:
                resolve_read_path(sample, ALLOWED_ROOTS)
            except PathRefused:
                pass
            else:
                failures.append(f"[穿越] `{sample}` 竟然被解析成功了")
            status, _body = fetch(port, "/file?path=" + quote(sample, safe=""))
            if status != 404:
                failures.append(f"[穿越] `{sample}` 期望被拒（404），实得 {status}")

        # ★前缀混淆：`logs_evil/` 是**另一个目录**，不是 `logs/` 的子目录。
        #   用**函数级**判据（不依赖文件是否存在）：`startswith("logs")` 会放行它，
        #   `commonpath` 不会。
        try:
            resolve_read_path("logs_evil/x", ALLOWED_ROOTS)
        except PathRefused:
            pass
        else:
            failures.append(
                "[前缀混淆] `logs_evil/x` 被放行了——前缀比较用了 startswith 而不是 commonpath"
            )

        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        server = None

        after = snapshot_all(".")

        leaks = diff_all(before, after)
        if leaks:
            failures.append(f"[零写盘] 看板动了工作区里的文件：{leaks}")

        # ===== ② 零执行：AST 扫本模块的 import ==============================
        with open(__file__, encoding="utf-8") as fp:
            tree = ast_module.parse(fp.read(), filename=__file__)
        imported: set = set()
        for node in ast_module.walk(tree):
            if isinstance(node, ast_module.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast_module.ImportFrom) and node.module:
                imported.add(node.module)
        banned = sorted(name for name in imported if name in _FORBIDDEN_IMPORTS)
        if banned:
            failures.append(f"[零执行] 本模块 import 了禁止清单里的模块：{banned}")

        # ===== ④ 只监听回环 =================================================
        if DEFAULT_HOST != "127.0.0.1":
            failures.append(f"[回环] 默认监听地址应当是 127.0.0.1，实为 {DEFAULT_HOST}")
        for host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            if not check_host(host)[0]:
                failures.append(f"[回环] `{host}` 应判为回环")
        for host in ("0.0.0.0", "192.168.1.10", "example.com"):
            is_loop, why = check_host(host)
            if is_loop:
                failures.append(f"[回环] `{host}` 不该判为回环——对外必须显式且给出理由")
            elif "反向代理" not in why:
                failures.append(f"[回环] `{host}` 的说明里没提反向代理：{why}")

        # 违规读根必须在**启动时**就被拒（不是等某个请求恰好命中才暴露）
        for bad_root in ("../outside", "/etc", "a/../../b"):
            try:
                make_server(DEFAULT_HOST, 0, (bad_root,))
            except PathRefused:
                continue
            failures.append(f"[读根] `{bad_root}` 作为读根竟然建起了服务")

        # ===== ⑤ R8：失败**不得**用「提示」色（渲染产物里禁用词必须零出现）==
        # ★自检用**内存夹具**（不落盘）：`_FIXTURE_SUMMARY` 的字段路径逐字照实测结构
        index_html = render_index(iter_runs(ALLOWED_ROOTS), metrics_payload(ALLOWED_ROOTS))
        rows, problems = extract_assertions(_FIXTURE_SUMMARY)
        run_html = render_run("logs/fixture.summary.json", _FIXTURE_SUMMARY, rows, problems)
        metrics_html = render_metrics(
            metrics_payload(ALLOWED_ROOTS), failure_trend(ALLOWED_ROOTS)
        )
        error_html = render_error_page(405, "本服务不接受写", "x")

        for label, text in (
            ("首页", index_html),
            ("明细页", run_html),
            ("度量页", metrics_html),
            ("错误页", error_html),
        ):
            hit = [word for word in FORBIDDEN_STYLE_WORDS if word in text.lower()]
            if hit:
                failures.append(f"[R8] {label}里出现了禁用词 {hit}——失败不得用「提示」色渲染")

        # ★`state-error` 只在**必然含失败态**的页面上要求：
        #   首页 / 度量页反映的是**真实工作区**的现状——那里可能一份失败都没有，
        #   此时不出现红色是**正确**的（"必须有红色"的判据会逼实现在没失败时也画红，
        #   那才是真的把失败信息弄脏）。
        for label, text in (("明细页", run_html), ("错误页", error_html)):
            if 'class="state-error"' not in text:
                failures.append(f"[R8] {label}里没有 `state-error`（失败态的唯一样式入口）")

        # ★反向也要钉住（用**内存数据**构造一次"确有失败"的渲染，不依赖工作区现状）：
        #   有失败运行时，首页**必须**出现 error 态——否则"静默柔化"就没人管了。
        failed_run = RunRef(
            rel_path="logs/x.summary.json",
            name="x",
            success=False,
            case_total=1,
            case_fail=1,
            step_total=1,
            step_fail=1,
        )
        html_failed = render_index([failed_run], {"lines": []})
        if 'class="state-error"' not in html_failed:
            failures.append("[R8] 有失败运行时首页没有 error 态——失败被静默柔化了")
        if any(word in html_failed.lower() for word in FORBIDDEN_STYLE_WORDS):
            failures.append("[R8] 有失败运行时首页出现了禁用词")

        if "state-pass" not in run_html:
            failures.append("[R8] 明细页缺少通过态（夹具里有一通过一失败）")
        if 'class="state-pass"' not in state_span(True):
            failures.append("[R8] `state_span(True)` 没走 state-pass")
        if 'class="state-error"' not in state_span(False):
            failures.append("[R8] `state_span(False)` 没走 state-error")

        # 降级率必须在**看板**上（行 348：不能只写日志）——首页就得看得见。
        # ★自检**不造** `reports/analysis.md`（那正是写盘），所以：有报告就验"数字真的上了板"，
        #   没有就**如实说明跳过**——而不是给一条恒真的假判据。
        kept = read_kept_ratio()
        if kept.value == "未读到":
            notes.append(f"未降级占比：未读到（{kept.note}）→ 已跳过「数字上板」判据")
        elif "未降级占比" not in index_html:
            failures.append("[降级可见] 首页没显示「未降级占比」这一项（§2.5 行 348）")
        elif kept.value not in index_html:
            failures.append(f"[降级可见] 首页有标签但数字 `{kept.value}` 没上板")

        # ===== ⑥ §5.5 指针：取得到，且取不到时要说清 ========================
        if not rows:
            failures.append("[§5.5] 从夹具里没取到任何断言（指针可能漏了一层）")
        if problems:
            failures.append(f"[§5.5] 夹具是合规结构，不该有取不到的原因：{problems}")
        if not [item for item in rows if not item.passed]:
            failures.append("[§5.5] 夹具里有一条失败断言，取出来却全是通过")

        # ★元护栏：**故意**把指针漏一层 `.data.` → 必须变成「取不到」，
        #   而不是静默变成「没有断言」（v4 的错就是这个，且它看起来像"正常"）
        without_data = {
            "details": [
                {
                    "name": "x",
                    "success": False,
                    "records": [
                        {
                            "name": "s",
                            "step_type": "request",
                            "success": False,
                            "validators": {"validate_extractor": []},
                        }
                    ],
                }
            ]
        }
        rows_bad, problems_bad = extract_assertions(without_data)
        if rows_bad:
            failures.append("[元护栏] 漏掉 `.data.` 后竟然还能取到断言")
        if not problems_bad or "data.validators" not in problems_bad[0]:
            failures.append(f"[元护栏] 漏掉 `.data.` 后没说清缺在哪一层：{problems_bad}")

        # ★元护栏：`snapshot_all` 必须**真的看见东西**（否则「零写盘」这条判据是空的
        #   ——那正是不能直接复用 `snapshot_tree` 的原因）。
        #   这里只要求"看得见文件、且递归到了子目录"：`.ai/`、`logs/` 里的产物
        #   **不保证**存在，所以不拿它们当自检的前提（真夹具的覆盖在
        #   `tests/web_readonly_test.py` 里——**夹具写入归 tests/**）。
        seen = set(snapshot_all("."))
        if not seen:
            failures.append("[元护栏] snapshot_all 什么都没看见——零写盘判据会是空的")
        if not any("/" in path for path in seen):
            failures.append("[元护栏] snapshot_all 只看见顶层文件，没递归子目录？")

        # ★元护栏 ③：把 commonpath 换成 startswith → `logs_evil` 必须立刻被放行
        #   （证明"前缀混淆"这条判据真的挂在 commonpath 上）
        import posixpath  # noqa: PLC0415

        if posixpath.commonpath(["logs", "logs_evil"]) == "logs":
            failures.append("[元护栏] commonpath 的行为与预期不符（本机实现变了）")

    finally:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except OSError:
                pass
        if thread is not None:
            thread.join(timeout=5)

    # ===== P3c 加固：IP 白名单 + Bearer token =============================
    # ★这里**自己起自己的服务**（主服务已经关了），所以不依赖上面的 `server` 变量。
    secured = make_server(
        DEFAULT_HOST,
        0,
        ALLOWED_ROOTS,
        allowed_ips=("10.0.0.1",),  # ★故意**不含**回环地址
        expected_token="s3cr3t",
    )
    secured_port = secured.server_address[1]
    threading.Thread(target=secured.serve_forever, daemon=True).start()
    token_only = make_server(DEFAULT_HOST, 0, ALLOWED_ROOTS, expected_token="s3cr3t")
    token_port = token_only.server_address[1]
    threading.Thread(target=token_only.serve_forever, daemon=True).start()
    try:
        # ① IP 不在白名单 → **403**（有凭据也不够）
        status, _body = fetch(secured_port, "/")
        if status != 403:
            failures.append(f"[加固] IP 不在白名单应回 403，实得 {status}")

        # ② ★★ 默认**不信任** `X-Forwarded-For`：伪造它**不能**骗过白名单
        #    （这是本批最要紧的一条——反代头是客户端能随便写的）
        status, _body = fetch(secured_port, "/", headers={"X-Forwarded-For": "10.0.0.1"})
        if status != 403:
            failures.append(
                f"[加固] 伪造 `X-Forwarded-For` 骗过了白名单（实得 {status}）"
                "——说明判定用的是头而不是**真实对端**"
            )

        # ③ 只配 token 时：缺 token → **401** + `WWW-Authenticate: Bearer`
        status, resp_headers = fetch_status_headers(token_port, "/")
        if status != 401:
            failures.append(f"[加固] 缺 token 应回 401，实得 {status}")
        if "bearer" not in str(resp_headers.get("WWW-Authenticate", "")).lower():
            failures.append(
                f"[加固] 401 没带 `WWW-Authenticate: Bearer`：{resp_headers}"
            )

        # ④ 错 token → **403**（有凭据但不够）
        status, _body = fetch(token_port, "/", headers={"Authorization": "Bearer wrong"})
        if status != 403:
            failures.append(f"[加固] 错 token 应回 403，实得 {status}")

        # ⑤ 对 token → **200**，且 ★**页面里绝不出现 token 本身**
        status, _headers = fetch_status_headers(
            token_port, "/", headers={"Authorization": "Bearer s3cr3t"}
        )
        if status != 200:
            failures.append(f"[加固] 对 token 应回 200，实得 {status}")
        status, body = fetch(token_port, "/", headers={"Authorization": "Bearer s3cr3t"})
        if b"s3cr3t" in body:
            failures.append("[加固] ★**页面里出现了 token**——密钥绝不透传给前端")

        # ⑥ 元护栏：**显式**信任反代时，`X-Forwarded-For` 才生效
        #    （证明 ② 的"不信任"是真的在起作用，而不是"这个头根本没被读"）
        proxied = make_server(
            DEFAULT_HOST,
            0,
            ALLOWED_ROOTS,
            allowed_ips=("10.0.0.1",),
            trust_proxy=True,
        )
        proxied_port = proxied.server_address[1]
        threading.Thread(target=proxied.serve_forever, daemon=True).start()
        try:
            status, _body = fetch(
                proxied_port, "/", headers={"X-Forwarded-For": "10.0.0.1"}
            )
            if status != 200:
                failures.append(
                    f"[元护栏] 显式 `trust_proxy` 后反代头仍未生效（实得 {status}）"
                    "——那 ② 的判据就是空转的"
                )
        finally:
            proxied.shutdown()
            proxied.server_close()
    finally:
        secured.shutdown()
        secured.server_close()
        token_only.shutdown()
        token_only.server_close()

    # ===== P3d 前端：导航可达 + 审计页 ====================================
    probe = make_server(DEFAULT_HOST, 0, ALLOWED_ROOTS)
    probe_port = probe.server_address[1]
    threading.Thread(target=probe.serve_forever, daemon=True).start()
    try:
        status, body = fetch(probe_port, "/audit")
        if status != 200:
            failures.append(f"[前端] `/audit` 期望 200，实得 {status}")
        if "审计时间线".encode("utf-8") not in body:
            failures.append("[前端] `/audit` 没有渲染出页面标题")

        status, body = fetch(probe_port, "/")
        nav = re.search(rb"<nav>(.*?)</nav>", body, re.S)
        if not nav:
            failures.append("[前端] 首页没有 `<nav>`")
        else:
            # ★"导航里列出的每一项都必须真能到"——加导航项却忘了实现路由，
            #   表现是"点进去 404"；把它变成一条判据。
            for href in re.findall(rb'href="([^"]+)"', nav.group(1)):
                code, _body = fetch(probe_port, href.decode("utf-8"))
                if code != 200:
                    failures.append(
                        f"[前端] 导航里的 `{href.decode('utf-8')}` 到不了（{code}）"
                    )
    finally:
        probe.shutdown()
        probe.server_close()

    for note in notes:
        print(f"[web 自检] 说明：{note}")

    print("=" * 66)
    if failures:
        print(f"web 只读看板自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("web 只读看板自检全部通过（§5.8 行 1001，**P3a**）：")
    print("  零写盘：**全量快照**对账，含 .ai/、reports/、logs/")
    print("          （不沿用 `workdir.snapshot_tree`：它为「别污染源码树」而刻意忽略")
    print("            这些目录——用它会得到一个永远为真的空护栏）")
    print("  零执行：只有 GET/HEAD；写方法**明确** 405（不是 501：501 是「不认得」，405 是「不允许」）")
    print("  穿越  ：7 类样本经**真实 HTTP** 全被拒 + ★`logs_evil/` 前缀混淆被 commonpath 拦下")
    print("  回环  ：默认 127.0.0.1；对外必须显式 `--host`，并打印反代 / 白名单理由")
    print("  R8    ：失败态用 state-error（红）；warning/warn/orange/yellow 四词**零出现**")
    print("  §5.5  ：指针 details[].records[j].data.validators.validate_extractor[k]")
    print("          （元护栏：**漏一层 `.data.`** 必须报「取不到」，不能静默成「没有断言」）")
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

    parser = argparse.ArgumentParser(description="只读看板自检（P3a）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "ALLOWED_ROOTS",
    "ANALYSIS_REL",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "FORBIDDEN_STYLE_WORDS",
    "LOOPBACK_HOSTS",
    "MAX_FILE_BYTES",
    "NO_SOURCE",
    "SELF_HEAL_REL",
    "STATE_ERROR",
    "STATE_NA",
    "STATE_PASS",
    "SUMMARY_SUFFIX",
    "TRAVERSAL_SAMPLES",
    "AssertionRow",
    "MetricLine",
    "PathRefused",
    "ReadOnlyServer",
    "RunRef",
    "WebHandler",
    "check_host",
    "count_ai_items",
    "confirm_form",
    "diff_all",
    "dig",
    "extract_assertions",
    "failure_trend",
    "fetch",
    "iter_runs",
    "load_summary",
    "make_server",
    "metrics_payload",
    "page",
    "read_json",
    "read_kept_ratio",
    "render_error_page",
    "render_audit_page",
    "render_confirm_result",
    "render_fix_page",
    "render_index",
    "render_job_detail",
    "render_job_result",
    "render_jobs_page",
    "render_metrics",
    "render_pending_page",
    "render_draft_quality",
    "render_run",
    "run_selftest",
    "safe_rel_path",
    "serve",
    "snapshot_all",
    "state_span",
    "stat_counts",
]


if __name__ == "__main__":
    raise SystemExit(main())













