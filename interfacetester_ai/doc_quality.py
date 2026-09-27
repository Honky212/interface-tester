# -*- coding: utf-8 -*-
"""doc_quality —— 生成前的文档体检闸（v8 §8.1 阶段行 **S-10** / §十 **T27**，2026-09-23）。

## 为什么需要这个文件

"**文档不准 → 用例不准**"这条链上有三处失效（§9.2）：R1 管"模型编"、R11 管"模型意会错"、
R15/R16 管"**文档本身错/缺**"。T29 已把归因六分类与文档缺陷**回流**做掉（事后），
本模块补的是**事前**：**先把"这份文档配不配生成"判掉**，不合格的**拒生成**并给出缺项清单
（交给文档作者），**而不是让模型硬猜缺的东西**。

这也是本仓取向的延续——"宁可响亮报错，不要静默假通过"：
文档缺 URL/字段表时，模型最可能的动作就是**编一个**，那正是 R1。

## 四类判据（D1~D4；与 §十 T27 行一一对应）

| 码 | 判据 | REJECT（必红） | WARN（放行但提示） |
| --- | --- | --- | --- |
| **D1 要素完整性** | URL / method / 字段表（必需）；类型 / 必填 / 枚举 / 状态码 / 示例（可选） | 缺 URL、缺 method、缺字段表 | 缺可选要素 |
| **D2 内部矛盾** | 同字段两种类型；示例与字段表冲突 | 命中即拒（**矛盾输入必产出矛盾用例**） | — |
| **D3 可判定率** | 有类型字段数 / 字段总数 | —（只降级） | 低于阈值 → "只生成骨架 + `unknowns`" |
| **D4 编码可解析** | 复用 `gate_s11_document_encoding`（S11 先例） | 坏编码 / 空文件 | BOM 残留 / HTML 残留 / 表格残缺 |

**为什么 warn 一律不拒**：本仓口径是"**假报会让真报被无视**"（`comparators.py`）——
体检闸一旦误伤，用户会整体关掉它，等于没有。所以只有"**缺了就没法生成**"和
"**自相矛盾**"才拒；"信息不全但能生成骨架"只提示。

## 切片

按 markdown 标题层级切块（§5.1 的切片口径）；无标题则整篇一片。逐切片判 D1~D3，
D4 在整篇读入时判一次（编码是文件级事实）。

## 写盘边界（红线③）

本模块是**登记在册的写入者**，写入根固定 `reports/`（缺项清单 `reports/NEEDS_DOC_FIX.md`；
`bench/write_boundary_scanner.py` 登记为 `("roots", ("reports/",))`）。
**只在该拒才落盘**（放行时不产出任何文件）；写盘行内联字面量前缀（T18 静态可判）。

## 用法

    python -m interfacetester_ai.doc_quality <doc.md>   # 体检：exit 0 放行 / 2 拒；无参数则跑自检
    from interfacetester_ai.doc_quality import audit_document, write_needs_doc_fix
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

from interfacetester_ai.gates import (
    REJECT,
    GateReport,
    gate_s11_document_encoding,
)

# ---------------------------------------------------------------------------
# 结果模型（严重级别与 S 系列一致：reject / warn）
# ---------------------------------------------------------------------------

WARN = "warn"

# 体检码 ↔ 判据（§十 T27 的四类）
D1_INCOMPLETE = "D1"  # 要素完整性
D2_CONTRADICTION = "D2"  # 内部矛盾
D3_ADJUDICABILITY = "D3"  # 可判定率
D4_ENCODING = "D4"  # 编码/可解析

# 可判定率阈值（有类型字段 / 字段总数；低于此值只建议降级，不拒）
ADJUDICABILITY_THRESHOLD = 0.5

# 缺项清单落点（write_needs_doc_fix 的固定相对根；红线③ 登记项）
NEEDS_DOC_FIX_REL_PATH = "reports/NEEDS_DOC_FIX.md"


@dataclass
class Finding:
    """一条体检发现（`code` 为 D1~D4；`where` 指向切片/字段）。"""

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


@dataclass
class DocReport:
    """整篇文档的体检报告。"""

    path: Text = "<text>"
    findings: List[Finding] = field(default_factory=list)

    @property
    def rejects(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == REJECT]

    @property
    def warns(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def ok(self) -> bool:
        """放行 = 无 REJECT（warn 不影响放行——见模块 docstring 的"为什么 warn 不拒"）。"""
        return not self.rejects

    def codes(self) -> List[Text]:
        return sorted({f.code for f in self.findings})

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "rejects": len(self.rejects),
            "warns": len(self.warns),
            "codes": self.codes(),
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# 切片与字段表解析（纯代码）
# ---------------------------------------------------------------------------


@dataclass
class Slice:
    name: Text
    text: Text


@dataclass
class FieldSpec:
    name: Text
    type_text: Text
    required_text: Text
    desc: Text


_HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.*)$")
# ---------------------------------------------------------------------------
# ★★ 列头**词表**与**落点判据**的唯一来源（2026-09-27，§9.19）
# ---------------------------------------------------------------------------
# ★为什么归到这里（`doc_quality` = "什么样的表算字段表"的既有单一来源）：本仓吃过"双源口径"的亏
#   —— 改造前 `doc_quality` 与 `assertions` **各有一套**列头词表，且**已经不一致**：
#     · `_FIELD_COL`：doc_quality 有 `field`、assertions 有 `property`；
#     · `_REQUIRED_COL`：doc_quality 有 `必需`、assertions 有 `必选/是否/选填`；
#     · `_NOTE_COL`：**只有** assertions 有 `取值/允许值/约束`（§九 第一档④ 的修复没同步过来）。
#   于是"同一张表在体检闸里算不算字段表"与"产不产断言"可能**得出不同结论** ✗。
#   现在两边共用下面这一份（并集，取各自更宽的一侧 —— 判据从宽的那一侧已经在真文档上验过）。
FIELD_COL_RE = re.compile(r"字段|参数|名称|name|field|key|property", re.I)
TYPE_COL_RE = re.compile(r"类型|type", re.I)
REQUIRED_COL_RE = re.compile(r"必填|必需|必选|是否|required|optional|可选|选填", re.I)
NOTE_COL_RE = re.compile(r"说明|描述|备注|含义|取值|允许值|约束|desc|note", re.I)
# ★§9.16 **请求头表**：首列词是「头」/「头名称」/`header`（必须在判字段列**之前**判落点）
HEADER_COL_RE = re.compile(r"^\s*`?\s*头|请求头|header", re.I)
# ★§9.16 **查询参数表**：首列词是「参数」/`query`/`param`
PARAM_COL_RE = re.compile(r"参数|query|param", re.I)
# ★§9.29 **响应 Cookie 表**：首列是 `Cookie` / `Set-Cookie`（大小写不敏感，含「Cookie 名 / 名称」）。
#   ★为什么归到这张词表（`doc_quality` = "什么样的表算哪种表"的既有单一来源）：它与
#     `HEADER_COL_RE`（请求头表）/ `PARAM_COL_RE`（查询参数表）同级——都决定"这张表说的是**哪一部分**"。
#   ★它**不是** `_FIELD_COL_RE` 的成员：以 `Cookie` 打头的表**不产字段级断言**，
#     只用来认"响应侧声明了哪些 cookie"（见 `assertions.derive_from_cookie_tables`）。
COOKIE_COL_RE = re.compile(r"^\s*`?\s*(set-cookie|cookie)", re.IGNORECASE)
# ★§9.30/§9.31 **响应侧「必带」列**：`必带` / `必下发` / `总是下发` / `总会下发` / `always`。
#   ★为什么**不能**用「必填」：`REQUIRED_COL_RE` 里的「必填」是**请求侧**判据 ——
#     带「必填」列的表一律当**请求表**（落点进 `request`、不产响应断言）。
#     响应侧要声明"服务端**一定会给**"，必须用**另一个词**；认错了方向就反了 ✓。
#   ★§9.31 起它同时适用于 **响应 Cookie 表**与**响应数据表**（原先叫 `COOKIE_ALWAYS_SENT_COL_RE`，
#     名字里的 `COOKIE_` 让人以为只管 cookie —— 判据本身与容器无关，遂改名）。
ALWAYS_SENT_COL_RE = re.compile(r"必带|必下发|总是下发|总会下发|always", re.IGNORECASE)
# ★§9.16 **示例列**：`示例` / `示例值` / `example`（**只喂请求值，不产断言**）
EXAMPLE_COL_RE = re.compile(r"示例|example", re.I)
# ★§9.27 **头表的取值列**：习惯写作「值」（golden 与示例文档都是 `| 头 | 值 | 必填 | 说明 |`）。
#   ★为什么单列一条、不并进 `EXAMPLE_COL_RE`：`示例值` 的语义是"举例"，而头表的「值」是
#     **这一行头该带什么值**的唯一直述；两者都只喂请求值，但词形不同。而且「值」**只在头表里认**
#     ——body / params 表里的「值」没有这种约定，认了会误伤（例如「取值范围」这类列）。
HEADER_VALUE_COL_RE = re.compile(r"^\s*`?\s*(值|value)\s*`?\s*$", re.IGNORECASE)


def classify_table_location(header: Sequence[Text]) -> Text:
    """一张字段表的**落点**：`headers`（请求头）/ `params`（查询参数）/ `body`（请求体）。

    ★判据只有首列词，**顺序即口径**：头 → 参数 → 其余算请求体。★先判头是实测出来的（§9.16）：
    真实文档的头表首列写「**头名称**」，它同时命中字段列的"名称"，顺序反了就会把
    `Authorization` / `X-Path` / `Content-Type` 当成**请求体字段**（补请求体时写进 JSON ✗）。
    """
    first = (header[0] if header else "") or ""
    if HEADER_COL_RE.search(first):
        return "headers"
    if PARAM_COL_RE.search(first):
        return "params"
    return "body"


# 兼容别名：模块内既有的私有名指向同一批正则（避免两套词表）
_FIELD_COL = FIELD_COL_RE
_TYPE_COL = TYPE_COL_RE
_REQUIRED_COL = REQUIRED_COL_RE
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEPARATOR_ROW_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def split_slices(text: Text) -> List[Slice]:
    """按 markdown 标题层级切片；无标题则整篇一片（§5.1 切片口径）。"""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [Slice(name="document", text=text)]
    slices: List[Slice] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        name = m.group(2).strip() or f"h{m.start()}"
        slices.append(Slice(name=name, text=text[start:end]))
    return slices


def _split_row(line: Text) -> List[Text]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_field_tables(text: Text) -> Tuple[List[FieldSpec], List[Text]]:
    """解析 markdown 字段表：返回 (字段列表, 表格残缺说明)。

    识别表头列：字段/参数/名称/name/key、类型/type、必填/可选/required、说明/描述/desc。
    """
    lines = text.split("\n")
    fields: List[FieldSpec] = []
    broken: List[Text] = []
    i = 0
    while i < len(lines):
        if not _TABLE_ROW_RE.match(lines[i]):
            i += 1
            continue
        block: List[Text] = []
        while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
            block.append(lines[i])
            i += 1
        if len(block) < 2 or not _SEPARATOR_ROW_RE.match(block[1]):
            continue  # 不是 markdown 表格（缺分隔行）
        header = _split_row(block[0])
        if len({len(_split_row(r)) for r in block}) > 1:
            broken.append(header[0] if header else "<table>")
        idx_field = next((n for n, h in enumerate(header) if _FIELD_COL.search(h)), None)
        if idx_field is None:
            continue
        idx_type = next((n for n, h in enumerate(header) if _TYPE_COL.search(h)), None)
        idx_req = next((n for n, h in enumerate(header) if _REQUIRED_COL.search(h)), None)
        for row in block[2:]:
            cells = _split_row(row)
            if idx_field >= len(cells):
                continue
            name = cells[idx_field]
            if not name or set(name) <= {"-", ":"}:
                continue
            fields.append(
                FieldSpec(
                    name=name,
                    type_text=(
                        cells[idx_type] if idx_type is not None and idx_type < len(cells) else ""
                    ),
                    required_text=(
                        cells[idx_req] if idx_req is not None and idx_req < len(cells) else ""
                    ),
                    desc=cells[-1] if len(cells) > 1 else "",
                )
            )
    return fields, broken


# ---------------------------------------------------------------------------
# D1~D4 判据
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"(https?://\S+)|((?<![\w.])/[\w\-.{}$/]*)")
_URL_HINT_RE = re.compile(r"接口地址|请求地址|请求路径|URL|接口路径|path", re.I)
_METHOD_RE = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b")
_STATUS_HINT_RE = re.compile(r"状态码|返回码|HTTP 状态|\b[1-5]\d\d\b")
_ENUM_HINT_RE = re.compile(r"枚举|取值|可选值|one of|∈")
_EXAMPLE_HINT_RE = re.compile(r"示例|例子|Example|```", re.I)
_TYPE_WORDS = (
    "int", "integer", "long", "float", "double", "number", "decimal",
    "string", "str", "char", "text", "bool", "boolean",
    "array", "list", "object", "dict", "map",
    "整数", "数字", "字符串", "布尔", "数组", "对象", "列表", "文本",
)
_HTML_RESIDUE_RE = re.compile(r"<\s*(br|p|div|td|tr|table|span|b)\b|&nbsp;|&lt;|&gt;")

# 类型词 → 示例 JSON 里的 Python 类型（用于 D2 的"示例与字段表冲突"）
_TYPE_TO_PY: Dict[Text, Tuple[type, ...]] = {
    "int": (int,), "integer": (int,), "long": (int,),
    "float": (float, int), "double": (float, int), "number": (float, int), "decimal": (float, int),
    "string": (str,), "str": (str,), "char": (str,), "text": (str,),
    "bool": (bool,), "boolean": (bool,),
    "array": (list,), "list": (list,),
    "object": (dict,), "dict": (dict,), "map": (dict,),
    "整数": (int,), "数字": (int, float), "字符串": (str,), "布尔": (bool,),
    "数组": (list,), "列表": (list,), "对象": (dict,), "文本": (str,),
}


def _canon_type(type_text: Text) -> Optional[Text]:
    """把类型单元格归一到一个已知类型词（未知 → None，不计入可判定率、不参与冲突判定）。"""
    low = str(type_text).strip().lower()
    if not low:
        return None
    for word in _TYPE_WORDS:
        if re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", low) or word in low:
            return word
    return None


def _extract_example_json(text: Text) -> Optional[Any]:
    """取第一个能解析的 ```json 代码块（D2 的示例侧证据）。"""
    for m in re.finditer(r"```(?:json)?\s*\n(.*?)```", text, re.S):
        body = m.group(1).strip()
        try:
            return json.loads(body)
        except (ValueError, TypeError):
            continue
    return None


def _iter_example_values(node: Any, key: Text) -> List[Any]:
    """在示例 JSON 里递归找 key（含嵌套对象/数组），返回其所有取值。"""
    out: List[Any] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k) == key:
                out.append(v)
            out.extend(_iter_example_values(v, key))
    elif isinstance(node, list):
        for item in node:
            out.extend(_iter_example_values(item, key))
    return out


def _py_kind(value: Any) -> Text:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return "unknown"


def _example_value_fits(value: Any, expect: Tuple[type, ...]) -> bool:
    """示例值与字段表声明的类型是否兼容（数字/布尔互认，避免误伤）。"""
    if isinstance(value, bool):
        return bool in expect or int in expect
    if isinstance(value, int):
        return int in expect or float in expect
    if isinstance(value, float):
        return float in expect or int in expect
    return isinstance(value, expect)


def _looks_like_interface_slice(slice_text: Text, fields: Sequence[FieldSpec]) -> bool:
    """该切片是否"在描述一个接口"——只有接口切片才判 D1 的必需项（避免误伤概述章节）。"""
    if fields:
        return True
    return bool(_URL_RE.search(slice_text) or _METHOD_RE.search(slice_text))


def audit_text(text: Text, path: Text = "<text>") -> DocReport:
    """对**已读入**的文档文本跑 D1~D3。

    ★落地口径修正（相对 §十 T27 行"逐切片算四类判据"）：**D1 的必需项按整篇判**、
    D1 的可选要素与 D2/D3 按切片判。原因：切片是 **LLM 输入单位**，而
    "这份文档能不能生成"是**文档级**问题——实测常见形态是"`## 请求` 一节写 URL+method、
    `## 字段` 一节放表格"，若按切片判必需项，会给每个只有 URL 的切片都报"缺字段表"（**误伤**）。
    判据本身不变（缺 URL / 缺 method / 缺字段表 → 拒），只是判的**粒度**落到文档级。
    """
    rep = DocReport(path=path)
    slices = split_slices(text)
    per_slice: List[Tuple[Slice, List[FieldSpec], List[Text]]] = []
    all_fields: List[FieldSpec] = []

    for s in slices:
        fields, broken = parse_field_tables(s.text)
        all_fields.extend(fields)
        per_slice.append((s, fields, broken))
        if broken:
            rep.findings.append(
                Finding(
                    D4_ENCODING, WARN, s.name,
                    f"表格列数不齐（{', '.join(broken[:3])}）——解析可能漏字段",
                    "补齐该表格的列分隔符（每行的 | 数保持一致）",
                )
            )

    # ---- D1 必需项（**文档级**：缺了就没法生成 → REJECT） ----
    doc_has_url = bool(_URL_RE.search(text))
    doc_has_method = bool(_METHOD_RE.search(text))
    doc_has_table = bool(all_fields)
    if doc_has_url or doc_has_method or doc_has_table:  # 是"接口文档"才判必需项
        if not doc_has_url:
            rep.findings.append(
                Finding(
                    D1_INCOMPLETE, REJECT, "<document>",
                    "全篇缺 URL / 请求路径——模型只能编一个（R1）",
                    "补「请求 URL」或「路径」一行（如 `POST /api/order`）",
                )
            )
        if not doc_has_method:
            rep.findings.append(
                Finding(
                    D1_INCOMPLETE, REJECT, "<document>",
                    "全篇缺 HTTP method——无法生成请求",
                    "补 method（GET / POST / PUT / DELETE …）",
                )
            )
        if not doc_has_table:
            rep.findings.append(
                Finding(
                    D1_INCOMPLETE, REJECT, "<document>",
                    "全篇缺字段表（字段 / 参数表）——无法生成可判定断言",
                    "补一张 markdown 表格：`| 字段 | 类型 | 必填 | 说明 |`",
                )
            )

    # ---- D1 可选要素之"文档属性"三项（**文档级**：跨节文档若按切片判会误报） ----
    if doc_has_url or doc_has_method or doc_has_table:
        if not _ENUM_HINT_RE.search(text):
            rep.findings.append(
                Finding(
                    D1_INCOMPLETE, WARN, "<document>",
                    "全篇未标枚举 / 取值范围——边界断言只能进 unknowns",
                    "对枚举字段写清取值（如 `1=已支付, 2=已发货`）",
                )
            )
        if not _STATUS_HINT_RE.search(text):
            rep.findings.append(
                Finding(
                    D1_INCOMPLETE, WARN, "<document>",
                    "全篇未写状态码——`status_code` 断言只能进 unknowns",
                    "补期望状态码（如「成功返回 200，参数错返回 400」）",
                )
            )
        if not _EXAMPLE_HINT_RE.search(text):
            rep.findings.append(
                Finding(
                    D1_INCOMPLETE, WARN, "<document>",
                    "全篇无请求 / 响应示例（**可选要素**，不影响放行）",
                    "补一个 ```json 示例（注意：示例值不得当期望值硬编码，见 T28）",
                )
            )

    for s, fields, _broken in per_slice:
        if not _looks_like_interface_slice(s.text, fields):
            continue  # 非接口切片（概述 / 变更记录等）不判 D1 可选要素

        # ---- D1 可选要素（缺了只提示 → WARN，绝不拒） ----
        typed = [f for f in fields if _canon_type(f.type_text)]
        if fields and not typed:
            rep.findings.append(
                Finding(
                    D1_INCOMPLETE, WARN, s.name,
                    "字段表无「类型」列或其值为空——只能生成骨架，断言受限",
                    "补类型列（int / string / bool / array / object …）",
                )
            )
        if fields and not any(f.required_text.strip() for f in fields):
            rep.findings.append(
                Finding(
                    D1_INCOMPLETE, WARN, s.name,
                    "字段表无「必填 / 可选」列——必填断言无法判定",
                    "补「必填」列（是 / 否）",
                )
            )
        # 枚举 / 状态码 / 示例三项是**文档属性**，在文档级判（见上文），此处不重复

        # ---- D2 同字段两种类型（矛盾输入必产出矛盾用例 → REJECT） ----
        by_name: Dict[Text, set] = {}
        for f in fields:
            canon = _canon_type(f.type_text)
            if canon:
                by_name.setdefault(f.name, set()).add(canon)
        for name, kinds in sorted(by_name.items()):
            if len(kinds) > 1:
                rep.findings.append(
                    Finding(
                        D2_CONTRADICTION, REJECT, f"{s.name} / {name}",
                        "同一字段出现两种类型：" + "、".join(sorted(kinds)),
                        "以真实接口为准统一类型（矛盾输入必然产出矛盾用例）",
                    )
                )

        # ---- D3 可判定率（只降级，不拒） ----
        if fields:
            rate = len(typed) / len(fields)
            if rate < ADJUDICABILITY_THRESHOLD:
                rep.findings.append(
                    Finding(
                        D3_ADJUDICABILITY, WARN, s.name,
                        f"可判定率 {rate:.0%}（{len(typed)}/{len(fields)} 个字段有类型）"
                        f"低于阈值 {ADJUDICABILITY_THRESHOLD:.0%}",
                        "建议**只生成骨架 + unknowns 清单**，待文档补齐类型后再生成断言",
                    )
                )

    # ---- D2 示例与字段表冲突（整篇一次：示例块可能跨切片） ----
    example = _extract_example_json(text)
    if example is not None:
        for f in all_fields:
            canon = _canon_type(f.type_text)
            expect = _TYPE_TO_PY.get(canon or "")
            if not expect:
                continue
            for value in _iter_example_values(example, f.name):
                if _example_value_fits(value, expect):
                    continue
                rep.findings.append(
                    Finding(
                        D2_CONTRADICTION, REJECT, f"示例 / {f.name}",
                        f"示例值与字段表冲突：表里声明 `{f.type_text}`，示例里是 {_py_kind(value)}",
                        "以真实接口为准统一（**示例片段也算文档**，冲突必须修）",
                    )
                )
    return rep


def _decode_document(path: Text) -> Tuple[Optional[Text], List[Finding]]:
    """读文档：复用 S11 的编码探测（utf-8-sig / utf-8 / gbk / gb18030），返回 (文本, D4 findings)。"""
    findings: List[Finding] = []
    s11 = GateReport()
    gate_s11_document_encoding(path, s11)
    if s11.rejects:
        for f in s11.rejects:
            findings.append(
                Finding(
                    D4_ENCODING, REJECT, os.path.basename(path),
                    f"文档不可读（S11）：{f.message}",
                    "另存为 UTF-8（含 BOM 亦可）后重试；空文件请补内容",
                )
            )
        return None, findings

    with open(path, "rb") as fp:
        raw = fp.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        findings.append(
            Finding(
                D4_ENCODING, WARN, os.path.basename(path),
                "文档带 UTF-8 BOM（能读，但部分工具会把它当正文首字符）",
                "建议另存为「UTF-8 无 BOM」（S11 的探测顺序已兼容）",
            )
        )
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if _HTML_RESIDUE_RE.search(text):
            findings.append(
                Finding(
                    D4_ENCODING, WARN, os.path.basename(path),
                    "疑似 HTML 残留（<br>/&nbsp;/<td> 等）——切片与表格解析会失真",
                    "转成纯 markdown（去掉 HTML 标签与实体）",
                )
            )
        return text, findings
    return None, findings  # pragma: no cover - S11 已拦


def audit_document(path: Text) -> DocReport:
    """体检一份文档文件（D4 编码 → D1~D3 逐切片）。"""
    rep = DocReport(path=os.path.basename(path))
    text, d4_findings = _decode_document(path)
    rep.findings.extend(d4_findings)
    if text is None:
        return rep
    rep.findings.extend(audit_text(text, path=rep.path).findings)
    return rep


def render_needs_doc_fix(report: DocReport) -> Text:
    """渲染缺项清单（交付文档作者：必改 + 建议改，逐条点名切片与改法）。"""
    out = [
        "# 文档体检未通过 —— 请先修文档（由 `interfacetester_ai.doc_quality` 生成）",
        "",
        f"> 体检对象：`{report.path}`；REJECT **{len(report.rejects)}** 条、WARN {len(report.warns)} 条。",
        "> **生成没有开始**——不是模型不行，而是这份文档**缺了必须有的东西**或**自相矛盾**。",
        "> 修完后重跑：`python -m interfacetester_ai.doc_quality <doc.md>`（放行退出码 0）。",
        "",
    ]
    rejects = report.rejects
    warns = report.warns
    if rejects:
        out.append("## 一、必改（REJECT：不改就不生成）")
        out.append("")
        for f in rejects:
            out.append(f"- **[{f.code}] {f.where}**：{f.message}")
            if f.hint:
                out.append(f"  - 建议：{f.hint}")
        out.append("")
    if warns:
        out.append("## 二、建议改（WARN：不阻断生成，但会限制断言质量）")
        out.append("")
        for f in warns:
            out.append(f"- **[{f.code}] {f.where}**：{f.message}")
            if f.hint:
                out.append(f"  - 建议：{f.hint}")
        out.append("")
    return "\n".join(out)


def write_needs_doc_fix(report: DocReport) -> Optional[Text]:
    """**只有该拒才落盘** `reports/NEEDS_DOC_FIX.md`；返回相对路径或 None。

    - 放行时不产出任何文件（不给人留一份"看起来有活要干"的清单）；
    - cwd 必须是工作区根：`reports/` 是固定相对根（§6 写盘边界，红线③ 登记项）；
    - 写盘行**内联字面量前缀**（T18 白名单纪律）。
    """
    if report.ok:
        return None
    os.makedirs("reports", exist_ok=True)  # 写入根字面量（T18 静态可判）
    with open("reports/NEEDS_DOC_FIX.md", "w", encoding="utf-8") as fp:
        fp.write(render_needs_doc_fix(report))
    return NEEDS_DOC_FIX_REL_PATH


__all__ = [
    "ADJUDICABILITY_THRESHOLD",
    "D1_INCOMPLETE",
    "D2_CONTRADICTION",
    "D3_ADJUDICABILITY",
    "D4_ENCODING",
    "DocReport",
    "FieldSpec",
    "Finding",
    "NEEDS_DOC_FIX_REL_PATH",
    "Slice",
    "WARN",
    "audit_document",
    "audit_text",
    "parse_field_tables",
    "render_needs_doc_fix",
    "run_selftest",
    "split_slices",
    "write_needs_doc_fix",
]

# ---------------------------------------------------------------------------
# 自检（红绿成对：违规必红 / 合规必绿）
# ---------------------------------------------------------------------------

# 健康文档：URL + method + 字段表（类型/必填）+ 枚举 + 状态码 + 示例 → 应 **0 finding**
GOOD_DOC = (
    "# 创建订单\n"
    "\n"
    "请求：`POST /api/order`\n"
    "\n"
    "状态码：成功返回 200，参数错误返回 400。\n"
    "\n"
    "## 字段\n"
    "\n"
    "| 字段 | 类型 | 必填 | 说明 |\n"
    "| --- | --- | --- | --- |\n"
    "| userId | int | 是 | 用户 ID |\n"
    "| amount | number | 是 | 金额，枚举：1=全额 2=部分 |\n"
    "\n"
    "```json\n"
    '{"userId": 7, "amount": 1}\n'
    "```\n"
)

# 只缺**可选要素**（示例）→ 必须放行（合规必绿，防误伤关闸）
GOOD_DOC_NO_EXAMPLE = GOOD_DOC.split("```json")[0]

# 违规夹具：{标签: (文档文本, 期望命中的体检码)}
BAD_DOCS: Dict[Text, Tuple[Text, Text]] = {
    "缺 URL / method": (GOOD_DOC.replace("`POST /api/order`", "创建订单接口"), D1_INCOMPLETE),
    "缺字段表": (
        "# 创建订单\n\n请求：`POST /api/order`\n\n状态码：成功返回 200。\n",
        D1_INCOMPLETE,
    ),
    "同字段两种类型": (
        "# 订单\n\n请求：`POST /api/order`\n\n| 字段 | 类型 | 必填 | 说明 |\n"
        "| --- | --- | --- | --- |\n| status | int | 是 | 状态 |\n"
        "| status | string | 否 | 状态（第二处） |\n",
        D2_CONTRADICTION,
    ),
    "示例与字段表冲突": (
        GOOD_DOC.replace('"amount": 1', '"amount": "many"'),
        D2_CONTRADICTION,
    ),
}


def run_selftest(verbose: bool = False) -> int:
    """跑自检。返回 0=全绿；非 0=失败项数。"""
    failures: List[Text] = []

    # ① 合规必绿：健康文档 0 finding
    good = audit_text(GOOD_DOC, "good.md")
    if not good.ok:
        failures.append(
            f"[误伤] 健康文档被判拒：{[(f.code, f.where) for f in good.rejects]}"
        )
    if good.findings:
        failures.append(
            f"[误伤] 健康文档仍有 {len(good.findings)} 条提示（期望 0）："
            f"{[(f.code, f.message[:24]) for f in good.findings]}"
        )

    # ② 违规必红：四类夹具各命中对应码
    for label, (text, code) in BAD_DOCS.items():
        rep = audit_text(text, "bad.md")
        if rep.ok:
            failures.append(f"[漏判] {label}：没有被拒（应 REJECT）")
        elif code not in rep.codes():
            failures.append(f"[错码] {label}：拒了但编码是 {rep.codes()}，期望含 {code}")

    # ③ 合规必绿：只缺可选要素（示例）→ 放行 + 提示
    only_warn = audit_text(GOOD_DOC_NO_EXAMPLE, "warn.md")
    if not only_warn.ok:
        failures.append(
            f"[误伤] 只缺可选要素（示例）却拒了："
            f"{[(f.code, f.where) for f in only_warn.rejects]}"
        )
    if not any("示例" in f.message for f in only_warn.warns):
        failures.append("[漏判] 缺示例（可选要素）没有给出提示")

    # ④ 元护栏：往健康文档里删掉 URL/method 行，必须从"放行"变"拒"
    injected = audit_text(GOOD_DOC.replace("`POST /api/order`", ""), "injected.md")
    if injected.ok:
        failures.append("[元护栏] 删除 URL/method 后竟然仍放行——体检没在比对要素")

    # ⑤ 写盘形态：用"该拒"的样本驱动（**自检本身不创建夹具文件**——生产模块里
    #    只应有 write_needs_doc_fix 自己的写盘点，T18 会把多出来的判违规）；
    #    0 字节文档（D4 复用 S11）的用例在 tests/doc_quality_test.py 里用临时目录做。
    import tempfile

    bad_report = audit_text(BAD_DOCS["缺字段表"][0], "bad.md")
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="docq_selftest_") as tmp:
        try:
            os.chdir(tmp)
            rel = write_needs_doc_fix(bad_report)
            if rel is None:
                failures.append("[写盘] 该拒时没有落盘 reports/NEEDS_DOC_FIX.md")
            elif not os.path.isfile("reports/NEEDS_DOC_FIX.md"):
                failures.append("[写盘] 落盘路径不对")
            else:
                body = open(
                    os.path.join("reports", "NEEDS_DOC_FIX.md"), encoding="utf-8"
                ).read()
                if "必改" not in body or "REJECT" not in body:
                    failures.append("[清单] 缺项清单没有分区 / 没有点名 REJECT")
                if "缺字段表" not in body and "字段表" not in body:
                    failures.append("[清单] 缺项清单没有点名缺失项")
            if write_needs_doc_fix(audit_text(GOOD_DOC, "good.md")) is not None:
                failures.append("[写盘] 放行的文档却落盘了清单（应不产出）")
        finally:
            os.chdir(old_cwd)

    print("=" * 66)
    if failures:
        print(f"文档体检闸自检**失败** {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        print("=" * 66)
        return len(failures)

    print("文档体检闸自检全部通过（四类判据 D1~D4）：")
    print("  合规必绿          ：健康文档 0 finding；只缺可选要素（示例）→ 放行 + 提示")
    print(
        f"  违规必红          ：{len(BAD_DOCS)} 类夹具全部被拒且编码正确"
        f"（{sorted({c for _, c in BAD_DOCS.values()})}）"
    )
    print("  元护栏            ：删掉 URL/method 后必须从放行变拒（体检真的在比对要素）")
    print("  编码（D4）        ：0 字节文档复用 S11 被拒；BOM / HTML 残留 / 表格残缺只提示")
    print("  缺项清单          ：该拒才落盘 reports/NEEDS_DOC_FIX.md，含「必改」分区")
    print("=" * 66)
    return 0


def main(argv: Optional[Sequence[Text]] = None) -> int:
    """CLI：`python -m interfacetester_ai.doc_quality <doc.md>`——放行 0 / 拒 2；无参数跑自检。"""
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("--selftest", "-s"):
        return run_selftest()

    path = args[0]
    if not os.path.isfile(path):
        print(f"文档不存在：{path}")
        return 2
    report = audit_document(path)
    print(
        f"体检对象：{report.path}（ok={report.ok}，REJECT={len(report.rejects)}，"
        f"WARN={len(report.warns)}）"
    )
    for f in report.findings:
        mark = "拒" if f.severity == REJECT else "提示"
        print(f"  [{mark}][{f.code}] {f.where}：{f.message}")
    if report.ok:
        print("体检通过（无 REJECT）——可以进入生成；上述提示不影响放行。")
        return 0
    rel = write_needs_doc_fix(report)
    print(f"体检**未通过**：缺项清单已写入 {rel}（修好后重跑本命令）")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
