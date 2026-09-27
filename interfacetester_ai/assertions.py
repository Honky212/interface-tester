# -*- coding: utf-8 -*-
r"""assertions —— **确定性断言推导**（§5.2 的"确定性映射先做（不调模型）"；**P0b**）。

## 为什么先做确定性映射

§5.2 的第一句就是它：**"确定性映射先做（不调模型）"**。理由不是"省钱"而是**可靠性**：

- 字段表里写着 `| username | string | 是 |` —— 这条**没有歧义**，代码能 100% 读对；
  交给模型，它可能读成 `body.user_name`（**少一个下划线**）或把必填判成可选；
- 而且这一步**可复现**：同一份文档跑两次得到**逐字节相同**的断言，
  而模型调用（哪怕 `temperature=0`）在服务端也不保证这一点。

所以流程是：**先确定性映射出能确定的那部分** → 剩下的（真正的语义理解）才交给模型/人工。
§5.2 的四条映射（必填 → `type_match`/`contains`、类型 → `type_match`、
范围 → `ge`/`le`、枚举 → `contained_by`）在本模块实现。

## 四条**刻意不做**的事（每条都有理由）

1. **可选字段不生成存在性/类型断言**：文档说"否"（可选）时值可能不存在，
   断言"它必须是 str"会**误报失败**——这与 T28 的「'可选'↔必填断言 → REJECT」同一条道理。
   可选字段照样进**覆盖矩阵**的"未覆盖"，让人看见"这里我们没断言"。
2. **条件返回字段也不断言**（★2026-09-26 新增，§九 第一档④）：文档写"仅当 `withAvatar=true`
   时返回"时，字段在条件不满足时**不出现**——断言它会误报失败。而"把条件变成前置条件"
   （请求参数 + 前置步骤 + 断言绑定到该场景）**超出确定性映射的范围** → **不猜**，
   写进跳过说明交人工。实证：评审 §四 P1 的抽检表 A #18 记的正是这个缺陷。
3. **`number`/`数字` 不映射成 `int`**：文档没说是整数还是浮点（很可能两者都接受）。
   硬猜一个 = 造一条会误报的断言；正确做法是**期望值缺失** → `${ENV(TODO_<字段>_TYPE)}`
   （§5.2）并进 `pending` 交人工确认。同理，类型列写着 `-`/`?`/空 也一样。
4. **业务规则里的错误码不进正常用例**：`- 库存不足：\`code: 1001\`` 描述的是**负例场景**，
   把它断言进"正常下单"的用例里，会让一条正常用例永远失败。它们进 `unknowns`
   （"需人工确认是否单列负例用例"）。

## 响应数据表 → 一条 `jsonschema_match`（口径 **B2**，2026-09-26 决策）

真实客户文档的**响应字段表**是 `字段名｜类型｜说明`（**没有「必填」列**）——按"必填列没写清 → 不断言"
的纪律，实测那份文档 **62 行响应字段一条断言都产不出来**（占其声明字段的 ~78%）。
但响应字段的语义是"**返回了就得类型对**"：

- 用 `type_match` **会误报**（字段可能不返回：分页、条件返回、可空）；
- 用 JSON Schema 的 `properties` 且**不写 `required`** 正好：**缺失不算失败、存在但类型错才算**。

**判定规则**：表头有「字段列」+「类型列」、且**没有「必填」列** → 视为该接口的**响应数据表**
（真实文档里请求表都写必填列；错误码表的首列不匹配"字段列"，不会被误判）。
字段名带 `.` 的按层级挂；**裸字段名挂 `data` 下**（公共响应格式把它作为业务数据容器）；
**类型不明确的不猜进 schema**（同 `number` 的处理），整表都做不到就**不生成**（空 schema 是 S1 的伪存在性形态）。
归属由 `gen` 的**归属闸门**按行号过滤——段外的表（如"公共响应格式"）不进任何用例。

## 列头**同义**（★2026-09-26 新增，§九 第一档④）
约束文本可能写在 `说明` 列，也可能写在 `取值范围` / `允许值` / `约束` 列——这些都是**同一种语义**。
改造前只认 `说明`，于是同一句 `枚举：pending / paid / cancelled` 在不同文档里
**一种生成 `contained_by`、另一种什么都不生成**（★而且**不会报错**，只是少一条断言——
少一条断言是最难发现的那种覆盖缺口）。现在四种列头等价处理。

★`示例` **刻意不认**：示例列是"举例"不是约束，把它当约束读会直接撞上 T28 的
「示例值硬编码」陷阱（R16）。

## ★`TODO_` 落点规则（§5.2 的 v7 规则，硬约束）

带 `${ENV(TODO_...)}` 的用例属「**已知未定**」：它能过 L1/S/L2，但**运行期必然 `EnvNotFound`**。
所以**只允许落 `.ai/draft/`，绝不进 `cases/`**——否则 CI 变红的原因会是"还没人确认期望值"，
等于**把人工待办伪装成质量信号**。本模块只负责**标出** `needs_human`，拦截由装配器做。

## 边界

本模块**不写盘**、**不调模型**（纯函数）。表格识别复用 `doc_quality` 的正则
（**刻意复用**：两份"什么样的行算表格"必须只有一个口径）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from urllib.parse import unquote
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

from interfacetester_ai.doc_quality import (  # noqa: PLC0415 - 同包复用，避免表格口径双源
    _SEPARATOR_ROW_RE,
    _TABLE_ROW_RE,
    _split_row,
)
from interfacetester_ai.doc_quality import ALWAYS_SENT_COL_RE as _ALWAYS_SENT_COL_RE
from interfacetester_ai.doc_quality import COOKIE_COL_RE as _COOKIE_COL_RE
from interfacetester_ai.doc_quality import EXAMPLE_COL_RE as _EXAMPLE_COL_RE
from interfacetester_ai.doc_quality import FIELD_COL_RE as _FIELD_COL_RE
from interfacetester_ai.doc_quality import HEADER_COL_RE as _HEADER_COL_RE
from interfacetester_ai.doc_quality import HEADER_VALUE_COL_RE as _HEADER_VALUE_COL_RE
from interfacetester_ai.doc_quality import NOTE_COL_RE as _NOTE_COL_RE
from interfacetester_ai.doc_quality import PARAM_COL_RE as _PARAM_COL_RE
from interfacetester_ai.doc_quality import REQUIRED_COL_RE as _REQUIRED_COL_RE
from interfacetester_ai.doc_quality import TYPE_COL_RE as _TYPE_COL_RE
from interfacetester_ai.doc_quality import classify_table_location as _classify_table_location

# `${ENV(TODO_<语义名>)}`：`ENV` 映射到内核 `utils.get_os_environ`，缺变量会抛 `EnvNotFound`
# **响亮报错**（这正是我们要的：没填的期望值不该静默通过）。本 fork **没有** `??` 语法。
TODO_PREFIX = "TODO_"


def todo_expect(semantic_name: Text) -> Text:
    """生成 `${ENV(TODO_<语义名>)}` 占位（§5.2）。"""
    return "${ENV(" + TODO_PREFIX + semantic_name + ")}"


def has_todo(value: Any) -> bool:
    """值里是否含 `TODO_` 占位（含列表/嵌套 dict 里的）。"""
    if isinstance(value, str):
        return TODO_PREFIX in value and "${ENV(" in value
    if isinstance(value, (list, tuple)):
        return any(has_todo(item) for item in value)
    if isinstance(value, dict):
        return any(has_todo(item) for item in value.values())
    return False


@dataclass(frozen=True)
class DerivedAssertion:
    """一条**推导出来**的断言（带着"为什么生成它"，供报告与人工复核）。"""

    comparator: Text
    check: Text
    expect: Any
    source_quote: Text
    reason: Text
    needs_human: bool = False  # True = 含 TODO_ 占位 → **只许落 `.ai/draft/`**
    # ★行号（1-based，指向 `source_quote` 在**文档**里的那一行）：
    # C-2 的"按行号 ∈ 片的行号范围"绑定靠它——没有它就只能靠文本搜索（会撞重复行）。
    # 默认 0 = "未知"（老调用点不受影响；新调用点必须给）。
    line_no: int = 0
    # ★这条断言**覆盖了哪几个文档字段**（2026-09-26，口径 B2）：
    #   普通断言留空（覆盖字段 = `check` 的末段）；而**一条 `jsonschema_match` 覆盖整张表**——
    #   若仍按"末段"统计，它会被算成 1 个字段（`body`），覆盖矩阵的分子会**虚低**。
    covered_fields: Tuple[Text, ...] = ()

    def to_validate_dict(self) -> Dict[Text, Any]:
        return {self.comparator: [self.check, self.expect]}

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "comparator": self.comparator,
            "check": self.check,
            "expect": self.expect,
            "source_quote": self.source_quote,
            "reason": self.reason,
            "needs_human": self.needs_human,
            "line_no": self.line_no,
            "covered_fields": list(self.covered_fields),
        }


# ---------------------------------------------------------------------------
# 类型名映射（文档写法 → 内核 `type_match` 认的 builtins 类型名）
# ---------------------------------------------------------------------------
#
# NOTICE（为什么是 builtins 名而不是中文/文档原词）：`type_match` 实测走
# `getattr(builtins, name)`，只接受**真的是类型对象**的名字（`int`/`str`/`list`/`dict`…），
# 别的名字会抛 `ParamsError`（"用户误用"）。所以映射表必须落到 builtins 名上。

TYPE_ALIASES: Tuple[Tuple[Text, Text], ...] = (
    ("字符串", "str"),
    ("string", "str"),
    ("str", "str"),
    ("整数", "int"),
    ("integer", "int"),
    ("int", "int"),
    ("浮点数", "float"),
    ("浮点", "float"),
    ("float", "float"),
    ("小数", "float"),
    ("布尔", "bool"),
    ("boolean", "bool"),
    ("bool", "bool"),
    ("数组", "list"),
    ("列表", "list"),
    ("array", "list"),
    ("list", "list"),
    ("对象", "dict"),
    ("object", "dict"),
    ("dict", "dict"),
    ("map", "dict"),
)

# ★**刻意不映射** number / 数字 / 数值：它可能是 int 也可能是 float，文档没说清——
# 猜一个就是造一条会误报的断言。这类（以及类型列写 `-`/`?`/空的）走 TODO_ 占位。
AMBIGUOUS_TYPE_TOKENS = ("number", "数字", "数值")
TYPE_MISSING_TOKENS = ("", "-", "—", "–", "?", "？", "/", "any", "未知", "待定", "n/a", "na")

# 「必填」的写法（真值）：`是` / `Y` / `必填` / `required` …
REQUIRED_TRUE_TOKENS = ("是", "y", "yes", "true", "必填", "必须", "required", "mandatory", "1")
REQUIRED_FALSE_TOKENS = ("否", "n", "no", "false", "可选", "选填", "optional", "0")


# ---------------------------------------------------------------------------
# 说明列里的**限定词**（范围 / 枚举）—— §5.2 的另外两条映射
# ---------------------------------------------------------------------------

# `数量，1 ~ 99` / `1-99` / `1 到 99` / `1至99`
RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:~|～|至|到|—|–|-)\s*(\d+(?:\.\d+)?)")
# `从 1 开始` / `>= 1` / `不少于 1` / `最小 1`
MIN_RE = re.compile(r"(?:从|不低于|不少于|大于等于|>=|最小)\s*(\d+(?:\.\d+)?)")
# `最多 100` / `<= 100` / `不超过 100` / `最大 100`
MAX_RE = re.compile(r"(?:最多|不超过|小于等于|<=|最大)\s*(\d+(?:\.\d+)?)")
# `枚举：a / b / c` / `取值：a,b,c`
ENUM_RE = re.compile(r"(?:枚举|取值|可选值|允许值)\s*[：:=]\s*(.+)")
# 「说了枚举但值没给」的形态（如 `枚举：见错误码表`）：值解析不出来时走 TODO_
ENUM_HINT_RE = re.compile(r"枚举|其中之一|取值之一|其中一个|其中一种")
ENUM_SEPARATOR_RE = re.compile(r"\s*(?:[/、,，|]|\bor\b)\s*")

# ★**条件返回**的写法（2026-09-26，§九 第一档④；评审 §四 P1 的实证缺陷）。
#
# 实证：`bench/golden/get_user_profile.md` 写的是
#     - `data.avatarUrl`：字符串，仅当 `withAvatar=true` 时返回
# 而改造前它会生成 `type_match(body.data.avatarUrl, 'str')` —— **无条件断言**。
# 该字段在 `withAvatar=false` 时**不出现** → 这条断言**会误报失败**（把"文档说的条件"
# 变成"我们的假缺陷"）。抽检表 A #18 就是这么记下来的（`⚠ 条件返回的字段被无条件断言`）。
#
# 处置（与"可选字段不断言"同一条道理，都是**宁缺勿编**）：
# **不生成任何断言**，并把"这里有个条件、工具没建模"写进跳过说明（→ `unknowns`），
# 交人工判断前置条件。★**条件不许猜**：`withAvatar=true` 需要请求参数、需要一条前置
# 步骤、还需要把断言绑定到"该场景"——这三件都没做，就不该断言字段必然存在。
CONDITIONAL_RETURN_RE = re.compile(
    r"仅当|只有当|仅在"
    r"|当[^，,。；;]{0,24}时(?:才)?(?:返回|出现|存在|有值|下发)"
    r"|如果[^，,。；;]{0,24}(?:则|就|才)"
    r"|若[^，,。；;]{0,24}(?:则|就|才)"
    r"|视[^，,。；;]{0,12}而定|可能不返回|不一定返回|条件返回|取决于"
)

# 表头列名（**唯一来源在 `doc_quality`**：`FIELD_COL_RE`/`TYPE_COL_RE`/`REQUIRED_COL_RE`/
#   `NOTE_COL_RE`/`HEADER_COL_RE`/`PARAM_COL_RE`/`EXAMPLE_COL_RE` + `classify_table_location()`）——
#   本模块在文件顶部 import 它们（§9.19 合并双源：改造前两套词表**已经不一致**）。
# ★2026-09-26（§九 第一档④）：`取值`/`允许值`/`约束` 三种**同义列头**（见 `NOTE_COL_RE`）。
#   依据（实证，评审 §四 P1）：`bench/golden/order_create.md` 的枚举列头写的是 **`取值范围`**，
#   而原先只认 `说明` —— 于是同一句 `枚举：pending / paid / cancelled`：
#   `webhook_notify.md`（列头 `说明`）生成了 `contained_by`，
#   `order_create.md`（列头 `取值范围`）**只生成了 `type_match`**。
#   同一表达形式两种结果 = 覆盖缺口，且**看不出来**（断言少一条不会报错）。
#   NOTICE：刻意**不**把 `示例` 收进"约束列" —— 示例列是"举例"，不是约束；
#   它另有用途（只喂产物的请求值，见 §9.16）。
# ★§9.16 落点判据（先判头 → 再判参数 → 其余算请求体）由 `classify_table_location()` 唯一提供。
REQUEST_SIDE_LOCATIONS = frozenset({"headers", "params"})
# 脱敏：名字看起来像凭据的，**一律**写成 `${ENV(...)}`——绝不把文档里的 token 抄进产物
_SECRET_NAME_RE = re.compile(
    r"password|passwd|pwd|token|secret|authorization|cookie|api[-_]?key|access[-_]?key|credential",
    re.I,
)
# ★§9.28 **凭据的"去哪拿"提示**（登记项 §9.27 第 5 节第 1 条）
#   问题：产物里写 `${ENV(AUTHORIZATION)}` ✓（脱敏硬线），但**没人告诉用户该填什么** ✗ ——
#   "一个合法产物 + 一句运行期会报错"并不够用，人还是得回去翻文档。
#   ★纪律不变：提示**必须出自文档**（该行的「说明」列原文 / 文档里讲认证的那一节），
#     **不许**由工具编一句"请填你的 token"；两样都取不到就**如实说"文档没写"**并交人工。
_AUTH_SECTION_RE = re.compile(
    r"认证|鉴权|授权|登录|令牌|token|auth|credential|签名|signature", re.IGNORECASE
)
# 取值时的"**没有这个键**"哨兵（用它区分"curl 里给了空串"与"curl 里没有"）
_MISSING = object()


def _env_var_name(name: Text) -> Text:
    """字段名 → 环境变量名（`Authorization` → `AUTHORIZATION`）。

    ★**单一口径**：凭据占位（`${ENV(...)}`）与"该填哪个环境变量"的提示必须用同一份换算，
      否则提示会指向一个产物里根本不存在的变量名（那比没有提示更坏）。
    """
    return re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").upper()


def _env_placeholder(name: Text) -> Text:
    """`Authorization` → `${ENV(AUTHORIZATION)}`（凭据只从环境读，不落明文）。"""
    return "${ENV(" + _env_var_name(name) + ")}"


def todo_placeholder(name: Text) -> Text:
    """`xPath` → `${ENV(TODO_XPATH)}`（**值确实存在、只是文档没给** → 等人填，不猜）。"""
    return todo_expect(re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").upper())


@dataclass(frozen=True)
class FieldRow:
    """字段表的一行（**保留整行原文**，供 `source_quote` 溯源——T28 的①就靠它）。"""

    name: Text
    type_text: Text
    required_text: Text
    note: Text
    raw: Text
    location: Text = "body"
    # ★这行在**文档里的行号**（1-based）：C-2 的片级绑定靠它（见 `DerivedAssertion.line_no`）。
    line_no: int = 0
    # ★§9.16 **示例列**的值（`示例` / `示例值` / `example`）：**只用来喂产物的 `request`**，
    #   绝不进断言（T28 ④ 只管 `expect`）——见 `_EXAMPLE_COL_RE` 的注释。
    example_text: Text = ""

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "name": self.name,
            "type": self.type_text,
            "required": self.required_text,
            "note": self.note,
            "location": self.location,
            "example": self.example_text,
        }


def _column_index(header: Sequence[Text], pattern: "re.Pattern") -> Optional[int]:
    for position, title in enumerate(header):
        if pattern.search(title or ""):
            return position
    return None


def _truthy_required(text: Text) -> Tuple[bool, bool]:
    """返回 `(明确必填, 明确可选)`；两者都 False = **文档没写清**（既不猜必填也不猜可选）。"""
    normalized = (text or "").strip().lower()
    if not normalized:
        return False, False
    required = any(token in normalized for token in REQUIRED_TRUE_TOKENS)
    optional = any(token in normalized for token in REQUIRED_FALSE_TOKENS)
    if required and optional:
        # `必填` 同时命中了 `否`？ 不可能；但 `可选/required` 这种混写要保守处理
        return True, False
    return required, optional


def read_field_rows(doc_text: Text, *, query_scopes: Sequence[Tuple[int, int]] = ()) -> List[FieldRow]:
    """读文档里的字段表（**表格识别复用 `doc_quality`**，避免"什么样的行算表格"两套口径）。

    ★`query_scopes`（§9.16，可选）：这些行号区间里的"请求体表"按**查询参数**看待 ——
    由调用方把 **GET / HEAD 接口段**的范围传进来。理由与请求载荷那边同一条：**GET 没有请求体**
    （协议事实），而真实文档常把查询参数写成 `| 字段名 | 类型 | 必填 | 说明 |`（首列不是「参数」），
    于是这些**请求侧**字段会被当成响应体字段去断言 → 实测产出必红的 `body.path` ✗。
    ★默认 `()` = 老行为一字不变（成对判据）。
    """
    lines = (doc_text or "").split("\n")
    rows: List[FieldRow] = []
    position = 0
    while position < len(lines):
        if not _TABLE_ROW_RE.match(lines[position]):
            position += 1
            continue
        block: List[Text] = []
        block_start = position  # ★块首行的下标（0-based）——行号账要从它算起
        while position < len(lines) and _TABLE_ROW_RE.match(lines[position]):
            block.append(lines[position])
            position += 1
        if len(block) < 2 or not _SEPARATOR_ROW_RE.match(block[1]):
            continue  # 不是 markdown 表格（缺分隔行）

        header = _split_row(block[0])
        field_idx = _column_index(header, _FIELD_COL_RE)
        # ★§9.27 头表的**首列就是「头」**（golden 与示例文档都这么写：`| 头 | 值 | 必填 | 说明 |`）——
        #   它**不**命中 `_FIELD_COL_RE`（字段/参数/名称/name/…），于是整张头表被**静默跳过** ✗。
        #   实测代价（2026-09-27，转换后的客户文档）：文档里明明写了必填的 `X-Path`（头表 + curl 各一处），
        #   产物却**一个头都不带** → 打真实服务必被拒（`FILE_1018`）✗；而且**不报错、不点名** ✗✗。
        #   口径：首列命中 `_HEADER_COL_RE` 的表就是**头表**，字段列即第 0 列（落点仍由
        #   `classify_table_location` 判，故一定是 `headers`；头表**不产断言**，见 `derive_from_field`）。
        if field_idx is None and header and _HEADER_COL_RE.search(header[0] or ""):
            field_idx = 0
        if field_idx is None:
            continue  # 不是字段表（例如"错误码 | 含义"这类表）
        type_idx = _column_index(header, _TYPE_COL_RE)
        required_idx = _column_index(header, _REQUIRED_COL_RE)
        note_idx = _column_index(header, _NOTE_COL_RE)
        example_idx = _column_index(header, _EXAMPLE_COL_RE)
        # ★§9.27 头表的取值列写作「值」（`| 头 | 值 | 必填 | 说明 |`）——它不是「示例值」，
        #   但**是文档给出的值**，同样有出处；不认它就取不到值 → 只能落 `${ENV(TODO_…)}`，
        #   把"照着文档就能跑"变成"人工必须补"。**只在头表里认**（见 `HEADER_VALUE_COL_RE` 注释）。
        if example_idx is None and field_idx == 0 and _HEADER_COL_RE.search(header[0] or ""):
            example_idx = _column_index(header, _HEADER_VALUE_COL_RE)
        # ★§9.16 落点三态（**顺序即口径**）：头表 → 查询参数 → 请求体。
        #   头表必须排在前面：`| 头名称 | 必填 | 示例值 |` 的首列同时命中 `_FIELD_COL_RE`（"名称"）
        #   与 `_HEADER_COL_RE`（"头名称"）——先判头，Authorization/X-Path 才不会被当成请求体字段。
        location = _classify_table_location(header)

        for offset, line in enumerate(block[2:], start=2):  # 跳过表头与分隔行
            cells = _split_row(line)
            if len(cells) <= field_idx:
                continue
            name = cells[field_idx].strip("`* ")
            if not name:
                continue
            line_no = block_start + offset + 1  # ← 1-based 文档行号
            row_location = location
            if row_location == "body" and any(
                low <= line_no <= high for low, high in query_scopes
            ):
                # GET/HEAD 段里没有请求体 → 这些字段是**查询参数**（见 docstring）
                row_location = "params"
            rows.append(
                FieldRow(
                    name=name,
                    type_text=cells[type_idx].strip() if type_idx is not None and len(cells) > type_idx else "",
                    required_text=(
                        cells[required_idx].strip()
                        if required_idx is not None and len(cells) > required_idx
                        else ""
                    ),
                    note=cells[note_idx].strip() if note_idx is not None and len(cells) > note_idx else "",
                    raw=line.strip(),
                    location=row_location,
                    line_no=line_no,
                    example_text=(
                        cells[example_idx].strip()
                        if example_idx is not None and len(cells) > example_idx
                        else ""
                    ),
                )
            )
    return rows


# 定位**接口段 / 节**的跨度（子树含子标题）——与 `gen.interface_sections()`、切片器同源，
# ★不在本模块另立一套"怎么算一节"的口径（本仓为双源口径吃过亏）。
from interfacetester_ai.slicer import heading_spans  # noqa: E402


# ---------------------------------------------------------------------------
# 推导：字段表 → 断言（§5.2 的四条映射）
# ---------------------------------------------------------------------------
# ★§9.14 归属（响应侧）的两条口径常量：**一处定义，两处用**（本模块产 + `assembler` 拦）。
#   ① `NOTE_RESPONSE_OWNERSHIP`：跳过原因与 CLI 统计**共用同一句话**，不许各写一份；
#   ② `KERNEL_CHECK_ROOTS`：内核真能把哪几种东西当断言对象（实测自 `interfacetester/response.py`
#      的报错提示："`status_code` / `body.data.id` / `headers.Content-Type` / `text`"）。
#      `params.X` **不在**其中——请求参数**不是**内核的断言对象，断言它必然取不到值。
NOTE_RESPONSE_OWNERSHIP = "响应侧没有这个字段"
KERNEL_CHECK_ROOTS = frozenset(
    {"status_code", "body", "headers", "text", "cookies", "elapsed", "elapsed_ms", "elapsed_s"}
)


def _normalize_type(type_text: Text) -> Tuple[Optional[Text], Text]:
    """文档类型写法 → builtins 类型名。返回 `(类型名 or None, 人话说明)`。"""
    raw = (type_text or "").strip()
    low = raw.lower()
    if low in TYPE_MISSING_TOKENS:
        return None, f"类型列写的是 {raw!r}（没给类型）"
    for token, py_name in TYPE_ALIASES:
        if token in low:
            return py_name, f"类型列写明 {raw!r}"
    if any(token in low for token in AMBIGUOUS_TYPE_TOKENS):
        return None, f"类型 {raw!r} 不明确（可能是整数也可能是浮点）"
    return None, f"类型 {raw!r} 认不出来"


def _num(text: Text) -> Any:
    """数字字面量（**整数就给 int**）：§5.2 明写"期望写数字字面量，防字典序坑"。"""
    try:
        return int(text)
    except ValueError:
        return float(text)


def field_path(row: FieldRow) -> Text:
    """断言里的 check 路径（`body.amount` / `params.page` / `headers.X-Path`）。

    ★请求侧那两种（`params` / `headers`）**不会**变成断言（见 `derive_from_field` 的第一道判），
      这里保留真实前缀只为让**跳过说明**读起来对得上文档。
    """
    return f"{row.location}.{row.name}"


def derive_from_field(
    row: FieldRow, response_names: Optional[set] = None
) -> Tuple[List[DerivedAssertion], List[Text]]:
    """从**一行字段**推导断言；第二个返回值是"跳过原因"（供 `unknowns`）。

    规则（§5.2 的四条映射）与**取舍**（每条都写在模块 docstring 里）：

    - **必填 + 类型明确** → `type_match`（可判定形态，不是伪存在性断言）；
    - **必填 + 类型缺失/不明确**（`number`、`-`、`?`）→ `type_match` + `${ENV(TODO_.._TYPE)}`
      （**期望值缺失**的正确处置）并标 `needs_human`；
    - **范围**（说明列的 `1 ~ 99` / `从 1 开始` / `最多 100`）→ `greater_or_equals` / `less_or_equals`；
    - **枚举**（说明列的 `枚举：a / b / c`）→ `contained_by`；
    - **可选字段**：①②③ **都不做**（值可能不存在，断言会误报）——只回一条"跳过原因"，
      让覆盖矩阵与 `unknowns` 都能看见"这里我们没断言"。

    ★`response_names`（§9.14，2026-09-26 端到端真跑量出来的）——**该字段所属接口段的响应侧字段名**：

    - 传了它、字段**不在**其中 → **一条断言都不产**，只回原因（`NOTE_RESPONSE_OWNERSHIP`）。
      理由：断言 `body.path` 时**响应里得有 `path`**，而请求参数表里的字段服务端**不一定会回**
      （实测：文档「成功响应」写着 `"data": null`，于是确定性通路唯一的断言必失败）；
      ★这里只按**名字**筛（足够便宜）；"名字对得上、**层级**不对"由装配器闸门按**路径**兜底（§9.32）；
    - 传 `None`（**默认**）→ **不判**：拿不到文档上下文时不许擅自摘，保持老行为。
    """
    out: List[DerivedAssertion] = []
    skipped: List[Text] = []
    check = field_path(row)

    # ★§9.16 **请求侧（请求头 / 查询参数）无条件不产断言**（2026-09-27，顺序即口径）：
    #   内核的断言对象是**响应**，而这两类字段只出现在**请求**里（`Authorization` / `X-Path` / `page`…）。
    #   ★为什么必须放在最前面：`headers.X` 的根 `headers` **在** `KERNEL_CHECK_ROOTS` 里 ——
    #     只靠后面的归属闸门，"请求头表"会被断言成**响应头**，凭空造出一条必失败的断言 ✗。
    #   ★它们并没有被丢掉：值已经写进产物的 `request`（`headers` / `params`），由**请求**保证。
    if (row.location or "body") in REQUEST_SIDE_LOCATIONS:
        skipped.append(
            f"{check}：这是**请求侧**字段（`{row.location}`）→ 不做响应断言："
            "请求由产物的 `request` 保证（已按文档把值写进 `headers` / `params`），断言只对**响应**侧"
        )
        return out, skipped

    # ★§9.14 归属：先判"这个断言对象取不取得到"，再谈怎么断言（顺序很重要：类型/范围/枚举
    #   写到一半才被摘，会让报告自相矛盾）。
    #   ★本层是**按名字**的早筛（老口径不变）：字段名不在响应侧就**一条断言都不产**；
    #     而"名字对得上、**层级**却不对"（文档 `data.fileName` vs 断言 `body.fileName`）由装配器的
    #     归属闸门按**路径**兜底（§9.32）——两层分工：这里省事、那里判准。
    if response_names is not None:
        root = (row.location or "body").split(".")[0]
        if root not in KERNEL_CHECK_ROOTS:
            skipped.append(
                f"{check}：检查项根 `{root}` **不是内核的断言对象**（它只认 "
                f"{'、'.join(sorted(KERNEL_CHECK_ROOTS)[:4])} 这类**响应侧**检查项）→ 不做这条断言"
            )
            return out, skipped
        if len(check.split(".")) > 1 and check.split(".")[-1] not in response_names:
            skipped.append(
                f"{check}：**{NOTE_RESPONSE_OWNERSHIP}**（本接口段的响应示例 / 响应数据表 / "
                "响应说明句里都没有它）→ 不做响应断言：拿响应当请求，照文档实现的服务必然失败；"
                "要么请文档作者补**成功响应**的数据结构，要么人工按业务改断言"
            )
            return out, skipped

    # ★**条件返回优先于"必填/可选"判断**（§九 第一档④）：即使必填列写 `是`，
    # 说明列写着"仅当…时返回"也**不许**无条件断言——字段在条件不满足时**不出现**，
    # 断言它会误报失败（把"文档说的条件"变成"我们的假缺陷"）。
    conditional = CONDITIONAL_RETURN_RE.search(row.note or "")
    if conditional:
        skipped.append(
            f"{check}：说明列写明**条件返回**（{conditional.group(0)}）→ "
            "不做存在性/类型/范围/枚举断言（条件不满足时字段不出现，断言会误报失败）；"
            "条件未建模，交人工补前置条件"
        )
        return out, skipped

    required, optional = _truthy_required(row.required_text)

    if not required:
        reason = (
            "字段可选" if optional else "**必填列没写清**（既非必填也非可选）"
        )
        skipped.append(f"{check}：{reason} → 不生成存在性/类型断言（断言它存在会误报失败）")
        return out, skipped

    # ① 类型（必填才断言）
    py_type, type_note = _normalize_type(row.type_text)
    if py_type:
        out.append(
            DerivedAssertion(
                comparator="type_match",
                check=check,
                expect=py_type,
                source_quote=row.raw,
                reason=f"必填 + {type_note} → 断言类型（可判定形态，不是伪存在性断言）",
            )
        )
    else:
        # ★类型不明确/缺失 → **不生成断言**，只记一条跳过原因。
        #
        # 为什么不是"写 `${ENV(TODO_x)}` 占位"（实测踩到）：`type_match` 的 expect
        # **必须是类型名**——S7 硬闸门会拦下 `type_match: [x, "${ENV(...)}"]`，
        # 而且它拦得**对**（类型名位置放一个占位符不是"待确认"，是用法错）。
        #
        # 更根本的理由：文档**没说类型**，本来就不该由我们"补一条类型断言"——
        # 那是在猜"这里应该有个类型契约"。`TODO_` 的正当用途是"**值确实存在、只待人工填**"
        # （模型通路会用），而"我们不该断言"与它是两件事。
        skipped.append(
            f"{check}：{type_note} → **不断言类型**（S7 要求 expect 是类型名；我们也不猜）"
        )

    # ② 范围（说明列）
    range_hit = RANGE_RE.search(row.note or "")
    if range_hit:
        low_text, high_text = range_hit.group(1), range_hit.group(2)
        out.append(
            DerivedAssertion(
                comparator="greater_or_equals",
                check=check,
                expect=_num(low_text),
                source_quote=row.raw,
                reason=f"说明列写明范围 {low_text} ~ {high_text} → 下界（**数字字面量**，防字典序坑）",
            )
        )
        out.append(
            DerivedAssertion(
                comparator="less_or_equals",
                check=check,
                expect=_num(high_text),
                source_quote=row.raw,
                reason=f"说明列写明范围 {low_text} ~ {high_text} → 上界",
            )
        )
    else:
        min_hit = MIN_RE.search(row.note or "")
        max_hit = MAX_RE.search(row.note or "")
        if min_hit:
            out.append(
                DerivedAssertion(
                    comparator="greater_or_equals",
                    check=check,
                    expect=_num(min_hit.group(1)),
                    source_quote=row.raw,
                    reason=f"说明列写明下界（{min_hit.group(0).strip()}）",
                )
            )
        if max_hit:
            out.append(
                DerivedAssertion(
                    comparator="less_or_equals",
                    check=check,
                    expect=_num(max_hit.group(1)),
                    source_quote=row.raw,
                    reason=f"说明列写明上界（{max_hit.group(0).strip()}）",
                )
            )

    # ③ 枚举（说明列）
    enum_hit = ENUM_RE.search(row.note or "")
    if enum_hit:
        tail = enum_hit.group(1).strip()
        values = [item.strip("`* ") for item in ENUM_SEPARATOR_RE.split(tail) if item.strip("`* ")]
        # 值里还出现"枚举/见…表"这类词 → 说明**值没给全**（如 `枚举：见错误码表`）
        looks_like_pointer = bool(ENUM_HINT_RE.search(tail)) or len(values) < 2
        if values and not looks_like_pointer:
            out.append(
                DerivedAssertion(
                    comparator="contained_by",
                    check=check,
                    expect=values,
                    source_quote=row.raw,
                    reason=f"说明列写明枚举 {values} → 断言取值在集合内",
                )
            )
        else:
            # 枚举值没给全（`枚举：见错误码表`）→ 同样**不断言**：`contained_by` 的 expect
            # 是**集合**，塞一个 `${ENV(...)}` 占位既不是集合、也没法判定。
            skipped.append(
                f"{check}：说明提到枚举但值没给全（{tail!r}）→ **不断言**，交人工补全取值"
            )
    elif ENUM_HINT_RE.search(row.note or ""):
        skipped.append(f"{check}：说明提到「其中之一」但没列出取值 → **不断言**，交人工补全")

    return out, skipped


# ---------------------------------------------------------------------------
# 推导：响应字段说明句（`- \`data.total\`：整数，总条数`）
# ---------------------------------------------------------------------------

# 形如 `- data.total：整数，总条数`（容忍反引号与 `- `/`* ` 前缀）
RESPONSE_NOTE_RE = re.compile(r"^\s*[-*+]\s*`?([A-Za-z_][A-Za-z0-9_.]*)`?\s*[：:]\s*(.+?)\s*$")


def derive_from_response_notes(doc_text: Text) -> Tuple[List[DerivedAssertion], List[Text]]:
    """从"响应字段说明句"推导 `type_match`。

    ★依据（写清楚，免得被当成幻觉）：`- data.total：整数，总条数` 是**契约陈述**
    ——文档在告诉读者这个字段是什么类型，不是"举例"。所以对它生成 `type_match` 是**有据**的，
    而且引用句就用**这一整行**（溯源可过）。

    **不处理**路径里带 `[]` 的（如 `data.list[].sku`）：那要 jmespath 的通配写法，
    超出"确定性映射"的范围 → 进 `unknowns` 交人工，**不硬猜**。
    """
    out: List[DerivedAssertion] = []
    skipped: List[Text] = []
    for index, line in enumerate((doc_text or "").split("\n"), start=1):
        hit = RESPONSE_NOTE_RE.match(line)
        if not hit:
            continue
        path, rest = hit.group(1), hit.group(2)
        if "[" in path:
            skipped.append(
                f"body.{path}：路径含 `[]`（列表元素），需 jmespath 通配写法 → 交人工确认"
            )
            continue
        # ★条件返回**不做断言**（§九 第一档④；实证：`data.avatarUrl` 明写"仅当
        # `withAvatar=true` 时返回"，改造前却被无条件断言 → 字段不出现时误报失败）。
        conditional = CONDITIONAL_RETURN_RE.search(rest)
        if conditional:
            skipped.append(
                f"body.{path}：说明句写明**条件返回**（{conditional.group(0)}）→ **不断言**"
                "（条件未建模：字段不出现时断言会误报失败），交人工确认前置条件"
            )
            continue
        head = rest.split("，")[0].split(",")[0]
        py_type, type_note = _normalize_type(head)
        if py_type:
            out.append(
                DerivedAssertion(
                    comparator="type_match",
                    check=f"body.{path}",
                    expect=py_type,
                    source_quote=line.strip(),
                    reason=f"响应字段说明句写明 {type_note} → 断言类型",
                    line_no=index,
                )
            )
        else:
            skipped.append(f"body.{path}：说明句里的{type_note} → 交人工确认")
    return out, skipped


# ---------------------------------------------------------------------------
# 推导：**响应数据表** → 一条 `jsonschema_match`（口径 **B2**，2026-09-26 决策，见 §9.8）
# ---------------------------------------------------------------------------
# 三句话说完口径：
#   1. **识别**：表头有「字段列」+「类型列」、且**没有「必填」列** → 视为该接口的**响应数据表**
#      （真实文档的响应表就是 `字段名｜类型｜说明`，而请求表都写必填列）；
#   2. **语义**：响应字段的意思是"**返回了就得类型对**"——字段可能不返回（分页/条件/可空），
#      所以**不能**用 `type_match`（字段缺失会误报失败）；改用 `jsonschema_match`，
#      schema 里写 `properties` 而**不写 `required`** → 字段缺失不算失败、存在但类型错才失败；
#   3. **归属**：靠 `line_no`（表头行号）交给 `gen` 的**归属闸门**按接口段过滤——
#      段外的表（如"公共响应格式"）不会进任何用例。
RESPONSE_TABLE_ROOT = "data"
_SCHEMA_TYPE_NAMES = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "dict": "object",
    "list": "array",
}


def _table_blocks(
    doc_text: Text,
) -> List[Tuple[int, List[Text], List[List[Text]], List[int], Text]]:
    """切出 markdown 表块：`(表头行号, 表头 cells, 数据行 cells 列表, 数据行行号列表, 原表文本)`。

    ★行号口径**必须与 `read_field_rows` 一致**（0-based 块首 → 表头行 = `+1`、数据行从 `+3` 起），
    否则"按行号过滤响应表的数据行"会错位——**坐标系统一**这件事本仓已经踩过好几次。
    """
    lines = (doc_text or "").split("\n")
    blocks: List[Tuple[int, List[Text], List[List[Text]], List[int], Text]] = []
    position = 0
    while position < len(lines):
        if not _TABLE_ROW_RE.match(lines[position]):
            position += 1
            continue
        start = position
        block: List[Text] = []
        while position < len(lines) and _TABLE_ROW_RE.match(lines[position]):
            block.append(lines[position])
            position += 1
        if len(block) < 2 or not _SEPARATOR_ROW_RE.match(block[1]):
            continue  # 不是 markdown 表格（缺分隔行）
        header = _split_row(block[0])
        body = [_split_row(line) for line in block[2:]]
        row_lines = [start + offset + 1 for offset in range(2, len(block))]
        blocks.append((start + 1, header, body, row_lines, "\n".join(block)))
    return blocks


def _put_nested(properties: Dict[Text, Any], path: List[Text], value: Any) -> None:
    """把 `a.b.c` 挂成 `{"a": {"properties": {"b": {"properties": {"c": value}}}}}`。

    ★逐层用 `properties` 展开、**不写 `required`**：路径上任何一层缺失都**不算失败**
    （挂错层级的后果是**漏检**，不是误报——这正是 B2 选的取舍）。
    """
    node = properties
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {"type": "object", "properties": {}}
            node[part] = child
        node = child.setdefault("properties", {})
    node[path[-1]] = value


def _response_table_path(name: Text) -> Tuple[Text, ...]:
    """响应表里的**字段名 → 完整路径**（**唯一一处**口径：裸名挂在 `data` 下，带点的按原样）。

    ★为什么必须是唯一一处（§9.32）：归属判据从"按名字"改成"**按路径**"之后，
      `derive_from_response_tables`（产 schema）与 `response_field_paths`（判归属）
      **都必须**用同一条换算 —— 各写一份的话，"文档说 `data.fileName`"与"归属认 `fileName`"
      会再次错位，那就等于没修。
    """
    path = tuple(part for part in str(name or "").split(".") if part)
    if not path:
        return ()
    return path if len(path) > 1 else (RESPONSE_TABLE_ROOT, path[0])


def _put_nested_required(schema: Dict[Text, Any], path: List[Text], leaf: Text) -> None:
    """把 `leaf` 记进 `path` 那一层的 `required`（`path` 是**容器路径**）。

    ★**为什么必须逐层建**：`required` 是**容器级**关键字 —— 全挂在根上就变成
      "顶层必须依次有 `data`、`a`、`b`"，而文档说的是"`data` 里**有 `a` 时**要有 `b`" ✗
      （挂错层级 = 误报，正是本仓最忌的"文档没错、用例却红"）。
    ★与 `_put_nested` 同源：容器的 `{"type": "object", "properties": {}}` 骨架是同一套，
      两处各写一份迟早漂移。
    """
    node = schema
    for part in path:
        properties = node.setdefault("properties", {})
        child = properties.get(part)
        if not isinstance(child, dict):
            child = {"type": "object", "properties": {}}
            properties[part] = child
        node = child
    names = node.setdefault("required", [])
    if leaf not in names:
        names.append(leaf)


# ---------------------------------------------------------------------------
# §9.17 响应数据表 vs **本段自己的成功响应示例**：形状冲突（文档自相矛盾 → 回流）
# ---------------------------------------------------------------------------
# ★为什么要有这一节（2026-09-27 实测）：真实文档里有 4 个接口的**响应数据表**列的是"列表元素字段"
#   （`fileName` / `version` / … 裸名），而它们自己的**成功响应示例**写着 `"data": [ {...} ]` ——
#   两条都是文档说的，却互相矛盾：按表推出来的 schema 是 `data` **是对象**，而示例里是**数组** ✗。
#   后果：这条 `jsonschema_match` **必然失败**，且失败看起来像"接口坏了"（分叉型静默）。
#
# ★处置（两条，缺一不可）：
#   ① **不产**这条必然失败的 schema 断言（少一条断言 ≠ 假通过；零断言有 S3 兜底）；
#   ② 把冲突**回流**成一份可交给文档作者的单子（`reports/doc_defects.md`，由 `gen` 写）。
#
# ★判不了的**不判**：本段没有成功响应示例（或示例里没有 `data` 键）→ 按老行为照产 schema。
DOC_DEFECT_PREFIX = "[文档缺陷]"
# 与"响应数据表推出 data 是对象"相冲突的示例形状
_CONFLICTING_DATA_SHAPES = ("array", "null", "scalar")
_DATA_SHAPE_LABELS = {
    "array": "数组 `[…]`",
    "null": "`null`",
    "scalar": "标量（字符串/数字/布尔）",
}


def _success_example_data_shape(section_text: Text) -> Optional[Tuple[Text, Text]]:
    """本段**成功响应示例**里 `data` 的形状 → `(形状, 该围栏正文原文)`；判不了给 `None`。

    ★口径与 `derive_from_response_examples` 一致（"围栏前最后一个非空行含 成功/正常/success"）。
    """
    text = section_text or ""
    for match in _FENCE_RE.finditer(text):
        head = text[: match.start()]
        marker = ""
        for line in reversed(head[-200:].split("\n")):
            if line.strip():
                marker = line.strip()
                break
        if not SUCCESS_HEAD_RE.search(marker):
            continue
        raw = match.group(1).strip()
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or "data" not in payload:
            continue
        value = payload["data"]
        if isinstance(value, list):
            return "array", raw
        if isinstance(value, dict):
            return "object", raw
        if value is None:
            return "null", raw
        return "scalar", raw
    return None


def _section_at(doc_text: Text, line_no: int) -> Tuple[int, int, Text]:
    """行号落点**最内层**那个标题的 `(起, 止, 标题)`。

    ★**为什么不用 `_section_tree()`**（实测踩到，2026-09-27）：`section_tree()` 内部会调
    `derive_from_response_tables()`（去收集"响应侧字段名"），而本判据又在**它里面**被调用 ——
    用 `_section_tree()` 就是**无限递归** ✗（`RecursionError`）。`heading_spans()` 是**切片器**里的
    纯结构函数（不回调本模块），所以这里直接用它 ✓。
    """
    hits = [span for span in heading_spans(doc_text or "") if span.start_line <= line_no <= span.end_line]
    if not hits:
        return 0, len((doc_text or "").split("\n")), ""
    span = min(hits, key=lambda item: item.end_line - item.start_line)
    return span.start_line, span.end_line, (span.title or "")


@dataclass(frozen=True)
class DocDefect:
    """一条**文档缺陷**（回流用）：哪里矛盾、为什么危险、怎么改、**双方的原文证据**。"""

    where: Text
    message: Text
    action: Text
    table_quote: Text
    example_quote: Text

    def to_note(self) -> Text:
        """一行版（进 `unknowns` / 跳过说明）。"""
        return f"{DOC_DEFECT_PREFIX} {self.message} → {self.action}"


_SHAPE_CONFLICT_ACTION = (
    "改法（二选一）：① 把「成功响应」示例改成**对象**形态（如包一层 `{\"records\": [ … ]}`）；"
    "② 或在响应数据表里**只声明外层容器字段**（如 `records` 声明为 `array`），"
    "元素字段改用**响应说明句**或示例表达。"
)


def _doc_defect_for_table(
    *, head_line: int, header: Sequence[Text], body: Sequence[Sequence[Text]], raw: Text,
    doc_text: Text,
) -> Optional[DocDefect]:
    """**单一判据**：这张响应数据表与本节示例冲突吗？（产 schema 那边与回流那边都调它）"""
    field_idx = _column_index(header, _FIELD_COL_RE)
    type_idx = _column_index(header, _TYPE_COL_RE)
    if field_idx is None or type_idx is None or not body:
        return None
    if _column_index(header, _REQUIRED_COL_RE) is not None:
        return None  # 有「必填」列 = 请求表，不归这条判据
    start, stop, title = _section_at(doc_text, head_line)
    section_text = _span_text(doc_text, start, stop)
    found = _success_example_data_shape(section_text)
    if not found:
        return None  # 判不了 → 不判（按老行为照产 schema）
    shape, example_raw = found
    if shape not in _CONFLICTING_DATA_SHAPES:
        return None
    names = [cells[field_idx].strip("`* ") for cells in body if len(cells) > field_idx]
    names = [name for name in names if name]
    sample = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
    return DocDefect(
        where=title or f"第 {head_line} 行",
        message=(
            f"第 {head_line} 行的**响应数据表**"
            + (f"（{title}）" if title else "")
            + f"与**它自己的「成功响应」示例**冲突：表里列了 {len(names)} 个字段"
            f"（{sample}）→ 推出的是 `data` **是对象**，"
            f"而示例里 `data` 是 {_DATA_SHAPE_LABELS.get(shape, shape)}；"
            "两边只能有一个对（照表实现的服务不会返回示例那种数据）"
        ),
        action=_SHAPE_CONFLICT_ACTION,
        table_quote=raw,
        example_quote=example_raw,
    )


def find_doc_defects(doc_text: Text) -> List[DocDefect]:
    """**文档缺陷回流**：整篇文档 → `[DocDefect]`（当前只含"响应表 vs 成功响应示例"的形状冲突）。

    判据（**判不了就不判**）：响应数据表（**无「必填」列**）推出的 schema 是"`data` 是对象"，
    而**本节自己的**成功响应示例里 `data` 是数组 / `null` / 标量 → 文档**自相矛盾**。

    ★纯函数（不写盘）：写盘由 `gen` 走**已登记的写入者** `diagnosis.write_doc_defects` 做（红线③）。
    """
    text = doc_text or ""
    defects: List[DocDefect] = []
    for head_line, header, body, _row_lines, raw in _table_blocks(text):
        found = _doc_defect_for_table(
            head_line=head_line, header=header, body=body, raw=raw, doc_text=text
        )
        if found is not None:
            defects.append(found)
    return defects


# ★§9.29 **响应 Cookie 表**（登记项 §9.25 第 7 节 / §9.28 第 5 节："cookies 需先成为可查事实"）
#   内核**一直**支持 `cookies.*`（`response.py`：`resp_obj.cookies.get_dict()` → **字符串字典**），
#   缺的是**"文档里怎么声明响应 cookie"这一环**：文档写了也没人认 → cookie 断言只能手工写，或者干脆没有 ✗。
#
#   ★★**为什么"一律 string 类型 + 一条 schema"是错的**（本轮自查纠正，2026-09-27）：
#     cookie 值在内核里**恒为字符串**（HTTP 协议如此）→ `type: string` 这条断言**永远为真、零信息量** ✗，
#     正是本仓最反对的"看起来在断言、其实什么都没判"。
#   因此本组判据改成：**只在文档给出了可判据的事实时才产断言** ——
#     · 说明列标了**固定值** → `equal: [cookies.<名>, <值>]`（有牙 ✓）
#     · 说明列给了**枚举取值** → `contained_by: [cookies.<名>, [...]]`（有牙 ✓）
#     · 都没有 → **不产断言**，但**认下这个事实**并**点名**（可见，不静默）✓
_COOKIE_PROTOCOL_NOTE = (
    "★cookie 值在内核里**恒为字符串**（`resp.cookies.get_dict()`）→ 枚举取值一律按**字符串**比较；"
    "文档里若写 `1 / 2` 这种数字，产物里会是 `\"1\"`/`\"2\"`（否则必红）"
)
#   ★说明列里的两个**标记词**（与「固定值」同一套写法，不新立列）：
#     · `_FIXED_VALUE_RE`  → 这个 cookie 的值是固定的（有牙的 `equal`）
#     · `_ALWAYS_SENT_RE`  → 这个 cookie **一定会下发**（服务端必带 → `required`）
_ALWAYS_SENT_RE = re.compile(r"必带|必下发|总是下发|总会下发|一定会下发|always", re.IGNORECASE)
_FIXED_VALUE_RE = re.compile(r"固定值|固定为|恒为|固定", re.IGNORECASE)


def cookie_declarations(doc_text: Text) -> List[Tuple[Text, Text, Text, bool]]:
    """文档里**响应 Cookie 表**的每一行 → `[(名字, 值列原文, 说明列原文, 是否必带)]`（去重、保序）。

    ★判据（与"响应数据表"同一条）：首列命中 `_COOKIE_COL_RE`（`Cookie` / `Set-Cookie` /
      「Cookie 名」…），且**没有「必填」列** —— 带必填列的表是**请求**侧
      （列的是"要发出去的 Cookie"，那走请求头表 + 凭据脱敏，不是这里）。

    ★**"必带"怎么表达**（§9.30）：两种都认，都是**文档明写**才算 ——
      ① 表里有「必带 / 必下发 / 总是下发」列且值为「是」；
      ② 说明列里写了「必带 / 总是下发」这类标记词。
      ★**不能**用「必填」：那个词是**请求侧**判据（带它的表会被当成请求表）—— 方向反了就会把
        "服务端一定会给"读成"客户端必须发" ✗。

    ★**给断言与 mock 共用的唯一解析**（两处各写一份解析＝本仓最忌的双源口径）。
    """
    rows: List[Tuple[Text, Text, Text, bool]] = []
    seen: set = set()
    for _head_line, header, body, _row_lines, _raw in _table_blocks(doc_text or ""):
        if not header or not _COOKIE_COL_RE.search(header[0] or ""):
            continue
        if _column_index(header, _REQUIRED_COL_RE) is not None:
            continue  # 请求侧（见 docstring）
        value_idx = _column_index(header, _EXAMPLE_COL_RE)
        if value_idx is None:
            value_idx = _column_index(header, _HEADER_VALUE_COL_RE)
        note_idx = _column_index(header, _NOTE_COL_RE)
        always_idx = _column_index(header, _ALWAYS_SENT_COL_RE)
        for cells in body:
            if not cells:
                continue
            name = cells[0].strip("`* ")
            if not name or name in seen:
                continue
            seen.add(name)
            value = cells[value_idx].strip() if value_idx is not None and len(cells) > value_idx else ""
            note = cells[note_idx].strip() if note_idx is not None and len(cells) > note_idx else ""
            always_cell = (
                cells[always_idx].strip()
                if always_idx is not None and len(cells) > always_idx
                else ""
            )
            always = bool(_truthy_required(always_cell)[0]) or bool(_ALWAYS_SENT_RE.search(note))
            rows.append((name, value.strip("`"), note, always))
    return rows


def cookie_table_names(doc_text: Text) -> List[Text]:
    """文档声明的响应 cookie 名（`cookie_declarations` 的薄封装，给 mock 用）。"""
    return [name for name, _value, _note, _always in cookie_declarations(doc_text)]


def _cookie_enum_values(note: Text) -> List[Text]:
    """说明列里的**枚举取值**（`ENUM_RE` + 分隔符，取值按**字符串**）→ `[]` 表示没有。"""
    found = ENUM_RE.search(note or "")
    if not found:
        return []
    return [
        part.strip().strip("`\"'")
        for part in ENUM_SEPARATOR_RE.split(found.group(1))
        if part.strip().strip("`\"'")
    ]


def cookie_mock_values(doc_text: Text) -> Dict[Text, Text]:
    """文档声明的响应 cookie → **mock 该下发的值**（固定值 → 用它；枚举 → 取第一个；否则 `mock-<名>`）。

    ★**为什么这个"取值决定"必须放在断言侧**（而不是让 mock 自己各写一份）：它必须与**断言的期望同源**
      —— mock 用一个断言不认的值（例如枚举断言 `A / B` 而 mock 下发 `mock-variant`），
      就会造出"文档没错、mock 也没错、用例却红"的**假失败** ✗。这是本仓"双源口径"教训的又一处。
    ★值里不要带 `;`（它是 `Set-Cookie` 的分隔符）——本函数不改写文档给的值，只在**没值**时才自己造一个。
    """
    values: Dict[Text, Text] = {}
    for name, value, note, _always in cookie_declarations(doc_text):
        if _FIXED_VALUE_RE.search(note or "") and value:
            values[name] = value
            continue
        enum = _cookie_enum_values(note)
        if enum:
            values[name] = enum[0]  # 取**第一个合法值**（断言是 `contained_by`，一定落在集合内 ✓）
            continue
        values[name] = f"mock-{name}"
    return values


# ---------------------------------------------------------------------------
# ★§9.33 **响应头表**（把 §9.29/§9.30 那套"让文档能声明事实"延续到**响应头**）
# ---------------------------------------------------------------------------
# 判据（**两条一起看**，缺一不可）：
#   ① 首列命中 `_HEADER_COL_RE`（`头` / `头名称` / `header`）**且没有「必填」列**
#      —— 带「必填」列的是**请求头**表（要发出去的，走 `request` + 凭据脱敏，见 §9.16/§9.27）；
#   ② 表**附近**出现过"响应/返回"字样（前 15 行窗口，与 `_note_field_paths` 同一道闸）
#      —— 否则一份"请求头表恰好忘写必填列"的文档会被**误当**响应头表，凭空多出 `headers.X` 断言 ✗。
#
# ★为什么值语义**只**认「固定值」与「枚举」（与 §9.29 的自查纠正同源）：响应头的类型**恒为字符串**
#   （协议如此）→"断言它是字符串"永远为真、零信息量 ✗；而 `Date` / `X-Request-Id` 这类值**本来就易变** ✗
#   → 所以只断"固定值"或"取值落在集合内"，其余**点名**（认下事实、不硬凑）。
_RESPONSE_HEADER_WINDOW = 15


def _table_window(lines: Sequence[Text], head_line: int) -> Text:
    """表头行**之前**的一段上下文（判"这张表说的是哪一侧"用）。"""
    return "\n".join(lines[max(0, head_line - 1 - _RESPONSE_HEADER_WINDOW) : head_line - 1])


def _is_response_header_table(header: Sequence[Text], lines: Sequence[Text], head_line: int) -> bool:
    """`头` 打头 + **无「必填」列** + 附近有"响应"字样 → 视为**响应头表**（见上面的两条判据）。"""
    if not header or not _HEADER_COL_RE.search(header[0] or ""):
        return False
    if _column_index(header, _REQUIRED_COL_RE) is not None:
        return False  # 请求头表
    return bool(RESPONSE_HEAD_RE.search(_table_window(lines, head_line)))


def _header_rows(header: Sequence[Text], body: Sequence[Sequence[Text]]) -> List[Tuple[Text, Text, Text]]:
    """**一张**响应头表的行 → `[(头名, 值列原文, 说明列原文)]`（**唯一取行口径**）。

    ★为什么单独抽出来（本轮踩到的坑）：`response_header_declarations()` 内部会**重跑**
      "表附近有没有『响应』字样"这道窗口判据 —— 推导侧若把**孤立的表块原文**再喂给它，
      窗口是空的 → 一行都取不到 ✗（实测：断言全空）。所以两处共用**这个**只负责取行的函数。
    """
    value_idx = _column_index(header, _EXAMPLE_COL_RE)
    if value_idx is None:
        value_idx = _column_index(header, _HEADER_VALUE_COL_RE)
    note_idx = _column_index(header, _NOTE_COL_RE)
    rows: List[Tuple[Text, Text, Text]] = []
    for cells in body:
        if not cells:
            continue
        name = cells[0].strip("`* ")
        if not name:
            continue
        value = cells[value_idx].strip() if value_idx is not None and len(cells) > value_idx else ""
        note = cells[note_idx].strip() if note_idx is not None and len(cells) > note_idx else ""
        rows.append((name, value.strip("`"), note))
    return rows


def response_header_declarations(doc_text: Text) -> List[Tuple[Text, Text, Text]]:
    """文档里**响应头表**的每一行 → `[(头名, 值列原文, 说明列原文)]`（去重、保序）。

    ★**给断言与 mock 共用的唯一解析**（双源口径在本仓吃过亏，见 §9.29）。
    """
    lines = (doc_text or "").split("\n")
    rows: List[Tuple[Text, Text, Text]] = []
    seen: set = set()
    for head_line, header, body, _row_lines, _raw in _table_blocks(doc_text or ""):
        if not _is_response_header_table(header, lines, head_line):
            continue
        for name, value, note in _header_rows(header, body):
            if name in seen:
                continue
            seen.add(name)
            rows.append((name, value, note))
    return rows


def _header_enum_values(note: Text) -> List[Text]:
    """说明列里的**枚举取值**（与响应 Cookie 那条同一套：`ENUM_RE` + 分隔符，取值按**字符串**）。"""
    found = ENUM_RE.search(note or "")
    if not found:
        return []
    return [
        part.strip().strip("`\"'")
        for part in ENUM_SEPARATOR_RE.split(found.group(1))
        if part.strip().strip("`\"'")
    ]


def response_header_values(doc_text: Text) -> Dict[Text, Text]:
    """文档声明的响应头 → **mock 该下发的值**（固定值 → 用它；枚举 → 第一个；否则 `mock-<名>`）。

    ★与断言的期望**同一份判据**（§9.29 的教训：mock 自己造一个断言不认的值 → 假失败 ✗）。
    """
    values: Dict[Text, Text] = {}
    for name, value, note in response_header_declarations(doc_text):
        if _FIXED_VALUE_RE.search(note or "") and value:
            values[name] = value
            continue
        enum = _header_enum_values(note)
        if enum:
            values[name] = enum[0]
            continue
        values[name] = f"mock-{name}"
    return values


def derive_from_response_headers(
    doc_text: Text,
) -> Tuple[List[DerivedAssertion], List[Text], set]:
    """**响应头表** → 逐行产**可判据**的 `headers.<名>` 断言（无据的**点名**、不硬凑）。

    判据（**顺序即口径**，与 §9.29/§9.30 一致）：

    1. 说明列标**固定值**（`固定值/固定为/恒为`）+ 值列给了值 → `equal: [headers.<名>, <值>]`；
    2. 说明列给了**枚举** → `contained_by: [headers.<名>, [...]]`（按**字符串**比）；
    3. 都没有 → 不产断言 → 收进**一条**点名说明（响应头值多半**易变**，不硬凑）。
    """
    out: List[DerivedAssertion] = []
    skipped: List[Text] = []
    covered_lines: set = set()
    undecidable: List[Text] = []
    lines = (doc_text or "").split("\n")

    for head_line, header, body, row_lines, raw in _table_blocks(doc_text or ""):
        if not _is_response_header_table(header, lines, head_line):
            continue
        covered_lines.update(row_lines)
        covered_lines.add(head_line)
        for name, value, note in _header_rows(header, body):
            check = f"headers.{name}"
            if _FIXED_VALUE_RE.search(note or "") and value:
                out.append(
                    DerivedAssertion(
                        comparator="equal",
                        check=check,
                        expect=value,
                        source_quote=raw,
                        reason=(
                            f"第 {head_line} 行的响应头表：`{name}` 的说明列标了**固定值**，"
                            f"值列给了 `{value}` → 按文档断言（内核的检查项就是 `headers.<名>`，"
                            "响应头字典大小写不敏感）"
                        ),
                        line_no=head_line,
                    )
                )
                continue
            enum = _header_enum_values(note or "")
            if enum:
                out.append(
                    DerivedAssertion(
                        comparator="contained_by",
                        check=check,
                        expect=enum,
                        source_quote=raw,
                        reason=(
                            f"第 {head_line} 行的响应头表：`{name}` 的说明列给了**枚举取值**"
                            f"（{' / '.join(enum)}）→ 断言取值落在集合内"
                            "（响应头值恒为字符串，按字符串比）"
                        ),
                        line_no=head_line,
                    )
                )
                continue
            undecidable.append(name)

    if undecidable:
        skipped.append(
            "以下响应头文档**声明了但没有可判据事实**（既没标「固定值」、也没给「枚举取值」）→ "
            "**不产断言**（认下这个事实）："
            + "、".join(dict.fromkeys(undecidable))
            + " —— 响应头值多半**易变**（`Date` / `X-Request-Id` 这类），"
            "要断请让文档补「固定值」或「取值」"
        )
    return out, skipped, covered_lines


def derive_from_cookie_tables(
    doc_text: Text,
) -> Tuple[List[DerivedAssertion], List[Text], set]:
    """**响应 Cookie 表** → 逐行产**可判据**的 `cookies.<名>` 断言（无据的**点名**、不硬凑）。

    判据（**顺序即口径**，与 `derive_from_field` 的枚举/固定值同一套词表）：

    1. 说明列标了**固定值**（`固定值/固定为/恒为`）→ `equal: [cookies.<名>, <值>]`；
       值列为空 → **不产**（不编），改点名；
    2. 说明列给了**枚举**（`ENUM_RE`）→ `contained_by: [cookies.<名>, [取值…]]`（按**字符串**比，见协议说明）；
    3. 都没有 → 不产断言 → 收进**一条**点名说明（"文档声明了、但没给可判据事实"）。
    """
    out: List[DerivedAssertion] = []
    skipped: List[Text] = []
    covered_lines: set = set()
    undecidable: List[Text] = []

    for head_line, header, body, row_lines, raw in _table_blocks(doc_text or ""):
        if not header or not _COOKIE_COL_RE.search(header[0] or ""):
            continue
        covered_lines.update(row_lines)
        covered_lines.add(head_line)
        # ★**必须带行号**：归属闸门（§9.14）的唯一口径是"引用句行号落在本接口段内"——
        #   行号 `0` 一律算**段外**（= 不替它编归属），实测代价：cookie 断言被整条丢弃 ✗
        #   （本探针真跑时抓到：产物里断言数 = 0，unknowns 里写着"2 条断言的引用句不属于本段"）。
        line_no = head_line
        if _column_index(header, _REQUIRED_COL_RE) is not None:
            skipped.append(
                f"第 {head_line} 行的 Cookie 表带**「必填」列** → 视为**请求**侧（列的是要发出去的 Cookie），"
                "不产响应 cookie 断言；请求 Cookie 请写成请求头表里的一行 `Cookie`"
                "（工具会按凭据脱敏，只写 `${ENV(名字)}`）"
            )
            continue
        declarations = cookie_declarations(raw)
        # ★§9.30 **必带**（服务端一定会下发）→ `required`。★这里写 `required` 是**合法**的：
        #   与响应数据表"不写 required"的差别就在**文档有没有建模** —— 文档明写了「必带」，
        #   服务端不给就是**文档被违反**（真缺陷）；没写就不能替它假定（会误报）。
        required_names = [name for name, _value, _note, always in declarations if always]
        for name, value_text, note, _always in declarations:
            check = f"cookies.{name}"
            if _FIXED_VALUE_RE.search(note or "") and value_text:
                out.append(
                    DerivedAssertion(
                        comparator="equal",
                        check=check,
                        expect=value_text,
                        source_quote=raw,
                        reason=(
                            f"第 {head_line} 行的 Cookie 表：`{name}` 的说明列标了**固定值**，"
                            f"值列给了 `{value_text}` → 按文档断言（`cookies` 在内核里是"
                            "`resp.cookies.get_dict()` 的字符串字典）"
                        ),
                        line_no=line_no,
                    )
                )
                continue
            enum = _cookie_enum_values(note or "")
            if enum:
                out.append(
                    DerivedAssertion(
                        comparator="contained_by",
                        check=check,
                        expect=enum,
                        source_quote=raw,
                        reason=(
                            f"第 {head_line} 行的 Cookie 表：`{name}` 的说明列给了**枚举取值**"
                            f"（{' / '.join(enum)}）→ 断言取值落在集合内。{_COOKIE_PROTOCOL_NOTE}"
                        ),
                        line_no=line_no,
                    )
                )
                continue
            undecidable.append(name)

        # ★§9.30「必带」→ 一条 `jsonschema_match`（`cookies` 的 `required`）
        if required_names:
            out.append(
                DerivedAssertion(
                    comparator="jsonschema_match",
                    check="cookies",
                    expect={"type": "object", "required": required_names},
                    source_quote=raw,
                    reason=(
                        f"第 {head_line} 行的 Cookie 表标了**必带**（{'、'.join(required_names)}）→ "
                        "生成一条 `required` 断言：**服务端不给就是文档被违反**。"
                        "★响应数据表默认**不写** `required`（那里文档没建模\"什么时候会返回\"），"
                        "这里写是因为**文档明写了必带**——判据差别就在这一点"
                    ),
                    line_no=line_no,
                )
            )
        elif _column_index(header, _ALWAYS_SENT_COL_RE) is not None:
            skipped.append(
                f"第 {head_line} 行的 Cookie 表有**「必带」列**，但没有任何一行写清（是/否）→ "
                "本轮**不产** `required` 断言（不替文档假定）"
            )

    if undecidable:
        skipped.append(
            "以下响应 cookie 文档**声明了但没有可判据事实**（既没标「固定值」、也没给「枚举取值」）→ "
            "**不产断言**（认下这个事实，不硬凑一条永远为真的断言）："
            + "、".join(dict.fromkeys(undecidable))
            + " —— 要断言请让文档补「固定值」或「取值」"
        )
    return out, skipped, covered_lines


def derive_from_response_tables(
    doc_text: Text,
) -> Tuple[List[DerivedAssertion], List[Text], set]:
    """**响应数据表** → 一条 `jsonschema_match`（覆盖整表）。

    第三个返回值是**已覆盖的行号集合**，给 `derive_assertions` 用：这些表的数据行**不再**逐行
    走"必填列没写清 → 不断言"——否则报告里会留下几十条**自相矛盾**的"没断言"说明
    （字段其实已被 schema 覆盖）。

    ★§9.17：若本表推出来的 schema 与**本节自己的成功响应示例**冲突（示例里 `data` 是数组/null/标量，
    而表推出 `data` 是对象）→ **不产**这条必然失败的断言，改按**文档缺陷**回流（见 `find_doc_defects`）。
    """
    out: List[DerivedAssertion] = []
    skipped: List[Text] = []
    covered_lines: set = set()

    for head_line, header, body, row_lines, raw in _table_blocks(doc_text):
        field_idx = _column_index(header, _FIELD_COL_RE)
        type_idx = _column_index(header, _TYPE_COL_RE)
        if field_idx is None or type_idx is None or not body:
            continue
        if _column_index(header, _REQUIRED_COL_RE) is not None:
            continue  # 有「必填」列 = 请求表 → 仍走 `derive_from_field`

        # ★§9.17 **先判冲突，再谈构造**（顺序即口径）：与本节示例形状冲突的表**不产** schema
        #   —— 那条断言必然失败，而失败看起来像"接口坏了"（分叉型静默）；判据与回流共用同一个
        #   `_doc_defect_for_table`（**单一判据**），说明里带 `DOC_DEFECT_PREFIX` 便于回流认领。
        defect = _doc_defect_for_table(
            head_line=head_line, header=header, body=body, raw=raw, doc_text=doc_text
        )
        if defect is not None:
            skipped.append(defect.to_note())
            covered_lines.update(row_lines)
            covered_lines.add(head_line)
            continue

        properties: Dict[Text, Any] = {}
        covered: List[Text] = []
        unknown_type: List[Text] = []
        # ★§9.31 **必带**（服务端一定会给）：`[(容器路径, 字段名, 原名)]` + 与"条件返回"冲突的
        required_paths: List[Tuple[List[Text], Text, Text]] = []
        required_conflicts: List[Text] = []
        note_idx = _column_index(header, _NOTE_COL_RE)
        always_idx = _column_index(header, _ALWAYS_SENT_COL_RE)
        for cells in body:
            if len(cells) <= field_idx:
                continue
            name = cells[field_idx].strip("`* ")
            if not name:
                continue
            path = list(_response_table_path(name))
            if not path:
                continue
            # ★§9.31 **必带与类型无关**：类型判不了也要断"有没有"，所以这一段放在类型检查**之前**。
            note_text = (
                cells[note_idx].strip() if note_idx is not None and len(cells) > note_idx else ""
            )
            always_cell = (
                cells[always_idx].strip()
                if always_idx is not None and len(cells) > always_idx
                else ""
            )
            always = bool(_truthy_required(always_cell)[0]) or bool(
                _ALWAYS_SENT_RE.search(note_text)
            )
            conditional = CONDITIONAL_RETURN_RE.search(note_text)
            if always and conditional:
                # ★**矛盾输入不硬判**：同一行既说"一定会给"又说"仅当…时返回" → 不写 `required`，
                #   点名交人工（替文档拍板会造出必红断言 ✗）
                required_conflicts.append(f"{name}（同一行还写着「{conditional.group(0)}」）")
            elif always:
                required_paths.append((path[:-1], path[-1], name))
            type_text = cells[type_idx].strip() if len(cells) > type_idx else ""
            py_type, _note = _normalize_type(type_text)
            if not py_type:
                unknown_type.append(name)  # 类型不明确 → **不猜**（同 `number` 的处理）
                continue
            _put_nested(properties, path, {"type": _SCHEMA_TYPE_NAMES.get(py_type, py_type)})
            covered.append(name)

        covered_lines.update(row_lines)
        covered_lines.add(head_line)

        if not properties:
            skipped.append(
                f"第 {head_line} 行的响应表：**整表字段类型都不明确** → 不生成 schema 断言"
                "（空 schema 是 S1 的「伪存在性形态」，会被闸门拦）；请文档补「类型」列"
            )
            continue

        # ★注意：`properties` **已经**含了 `data` 这一层（裸字段名在 `_put_nested` 前被加了
        #   `RESPONSE_TABLE_ROOT` 前缀）——这里**不能再套一层** `{"properties": {"data": …}}`，
        #   否则 schema 会变成 `data.data.<字段>`（实测踩到：多一层容器 → 那些字段永远校验不到）。
        schema = {"type": "object", "properties": properties}
        # ★§9.31「必带」→ `required`（**挂在文档说的那一层容器上**，不是全挂根上）
        for container, leaf, _name in required_paths:
            _put_nested_required(schema, container, leaf)
        if required_conflicts:
            skipped.append(
                f"第 {head_line} 行的响应表：以下字段同时标了**必带**与**条件返回**（自相矛盾）→ "
                "**不写** `required`（替文档拍板会造出必红断言），交文档作者确认："
                + "；".join(required_conflicts)
            )
        elif always_idx is not None and not required_paths:
            skipped.append(
                f"第 {head_line} 行的响应表有**「必带」列**，但没有任何一行写清（是/否）→ "
                "本轮**不产** `required`（不替文档假定）"
            )
        note = (
            f"；另有 {len(unknown_type)} 个字段**类型不明确**未纳入：{'、'.join(unknown_type[:5])}"
            if unknown_type
            else ""
        )
        required_summary = (
            "；★其中 "
            + "、".join(f"`{name}`" for _c, _l, name in required_paths)
            + " 标了**必带** → 写进 `required`（**挂在文档说的那一层容器上**）"
            if required_paths
            else "；★**不写 `required`**：字段缺失不算失败、存在但类型错才算（响应字段的本义）"
        )
        out.append(
            DerivedAssertion(
                comparator="jsonschema_match",
                check="body",
                expect=schema,
                source_quote=raw,
                reason=(
                    f"第 {head_line} 行的字段表**没有「必填」列** → 视为该接口的**响应数据表**，"
                    f"生成一条 schema 断言覆盖 {len(covered)} 个字段（挂在 `body.{RESPONSE_TABLE_ROOT}` 下）"
                    + required_summary
                    + note
                ),
                line_no=head_line,
                covered_fields=tuple(covered),
            )
        )
    return out, skipped, covered_lines


# ---------------------------------------------------------------------------
# 归属（§9.14）：**响应侧**字段名 —— 判 `body.X` 的 `X` 到底会不会出现在响应里
# ---------------------------------------------------------------------------
# ★为什么需要它（2026-09-26 端到端真跑量出来的根因）：`read_field_rows()` 只看"这张表有没有
#   字段列"，**不看这张表是请求还是响应** —— 于是请求参数表里的 `path` 被断言成
#   `type_match: [body.path, str]`，而文档的「成功响应」写着 `"data": null`：
#   **照文档实现的服务必然让这条断言失败**（17 份真产物里 15 份跑不绿，≥10 条出自这里）。
#
# 判据（**只看文档，不猜**）：`body.X` 合法 ⟺ 文档在**本接口段**里把 `X` 写在响应侧：
#   ① 无「必填」列的字段表（B2 口径：那是**响应数据表**）；
#   ② 响应说明句（``- `data.total`：整数，总条数``）；
#   ③ **响应示例** JSON 块（围栏前 200 字符出现"响应/返回/response"）里的键（含嵌套）。
#   `X` 只出现在请求侧 → **摘掉**（不是"标一下"，见 assembler 的 NOTICE）。
#
# ★**粒度是"节"，不是"整篇"**（探针实测的教训，`probe_p2_prep/out_response_ownership.txt`）：
#   按整篇取并集时，`POST /api/directory/create` 的 `body.path` 会被**别的节**的响应示例
#   （元数据接口里也有 `path` 这个键）**救活** ✗ —— 于是"归属闸门"一条都拦不住。
RESPONSE_HEAD_RE = re.compile(r"响应|返回|response|result", re.IGNORECASE)
# ★公共节：文档里**明说的**跨接口契约（统一响应格式 / 公共请求头 / 错误码表）。
#   只有这些节的内容允许跨接口复用；其余节只认**自己**那一节（否则又回到"整篇并集"的坑）。
#   ★`统一` 必须**跟格式类词**（响应/返回/格式/规范/说明）——实测踩到：文档标题
#   `# 统一文件服务平台-接入应用接口文档` 里的"统一"把它自己判成了公共节，
#   而根节的跨度是**全篇** → 于是 `body.path` 被别的接口的响应示例救活 ✗。
PUBLIC_SECTION_RE = re.compile(
    r"公共|通用|统一\s*(响应|返回|格式|规范|说明)|全局|common|global|envelope", re.IGNORECASE
)
_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.S)


def _all_json_keys(payload: Any, out: Optional[set] = None) -> set:
    """示例 JSON 里的**所有键**（含嵌套）：`body.data.userId` 的末段要能对上 `userId`。"""
    if out is None:
        out = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            out.add(str(key))
            _all_json_keys(value, out)
    elif isinstance(payload, list):
        for item in payload:
            _all_json_keys(item, out)
    return out


def _all_json_paths(payload: Any, prefix: Tuple[Text, ...] = (), out: Optional[set] = None) -> set:
    """JSON 里**每个键的完整路径**（含嵌套；数组元素沿用父路径，如 `data.items.sku`）。

    ★与 `_all_json_keys` 的差别就是"带不带层级" —— 归属判据（§9.32）要的正是层级。
    """
    if out is None:
        out = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = prefix + (str(key),)
            out.add(path)
            _all_json_paths(value, path, out)
    elif isinstance(payload, list):
        for item in payload:
            _all_json_paths(item, prefix, out)
    return out


def response_example_paths(text: Text) -> set:
    """**响应示例**里每个键的完整路径（围栏前 200 字符出现"响应/返回/response"才认）。"""
    paths: set = set()
    text = text or ""
    for match in _FENCE_RE.finditer(text):
        head = text[: match.start()][-200:]
        if not RESPONSE_HEAD_RE.search(head):
            continue
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            continue
        paths |= _all_json_paths(payload)
    return paths


def response_example_keys(text: Text) -> set:
    """（旧接口）响应示例里的 JSON **键名** —— `response_example_paths` 的投影。"""
    return {path[-1] for path in response_example_paths(text or "")}


# 响应说明句的**宽容**版（与 `RESPONSE_NOTE_RE` 的差别：容许 `data.list[].sku` 这种**列表路径**）。
# ★为什么宽容：`RESPONSE_NOTE_RE` 出于"列表路径交人工"的取舍**不产断言**，但**文档确实在响应侧
#   说过这个字段** —— 归属闸门该认它，否则 `body.sku` 会被误摘（自检 ①-b 的成对判据盯这个）。
_NOTE_LINE_RE = re.compile(r"^\s*[-*+]\s*`?([^`：:]+?)`?\s*[：:]\s*(.+)$")
_FIELD_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\[\]\.]*$")


def _note_field_paths(text: Text) -> set:
    """响应说明句（``- `data.x`：类型，说明``）里的**字段路径**。

    ★为什么要"附近出现过响应字样"这道闸：说明句也可能在描述**请求**字段
    （`说明：\\n- amount：整数，单位分`）——那**不是**响应侧证据，认了就会把该摘的断言放过去。
    """
    paths: set = set()
    lines = (text or "").split("\n")
    for index, line in enumerate(lines):
        match = _NOTE_LINE_RE.match(line)
        if not match:
            continue
        raw = match.group(1).strip()
        if not _FIELD_PATH_RE.match(raw):
            continue  # 左边的不是字段路径（如 `注意`、`请求体`）→ 不算
        window = "\n".join(lines[max(0, index - 15) : index])
        if not RESPONSE_HEAD_RE.search(window):
            continue
        path = tuple(part.replace("[]", "") for part in raw.split(".") if part)
        if path:
            paths.add(path)
    return paths


def _note_field_names(text: Text) -> set:
    """（旧接口）响应说明句里的字段**名** —— `_note_field_paths` 的投影。"""
    return {path[-1] for path in _note_field_paths(text)}


def _response_note_assertion_paths(text: Text) -> set:
    """**响应说明句派生出来的断言**的路径（`check = body.data.total` → `("data", "total")`）。"""
    paths: set = set()
    for item in derive_from_response_notes(text or "")[0]:
        parts = tuple(part for part in (item.check or "").split(".")[1:] if part)
        if parts:
            paths.add(parts)
    return paths


def response_field_paths(text: Text) -> set:
    """**一段文本**里响应侧字段的**完整路径** —— 归属判据的**唯一事实来源**（§9.32）。

    四条来源（与旧的"名字集合"一一对应，只是**带上层级**）：

    ① 响应数据表：按 `_response_table_path` 展开（裸名 → `data.<名>`，带点的按原样）；
    ② 说明句派生的断言：`- \\`data.total\\`：整数` → `("data", "total")`；
    ③ 说明句（**宽容版**，含 `data.list[].sku` 这种列表路径）；
    ④ 成功响应示例：每个键的完整路径（含嵌套）。

    ★`response_field_names()` 现在只是它的**投影** —— 名字与路径不再各算一份（改一处旧口径漂移一次）。
    """
    paths: set = set()
    for item in derive_from_response_tables(text or "")[0]:
        for name in item.covered_fields:
            path = _response_table_path(name)
            if path:
                paths.add(path)
    paths |= _response_note_assertion_paths(text or "")
    paths |= _note_field_paths(text)
    paths |= response_example_paths(text or "")
    return {path for path in paths if path}


def response_field_names(text: Text) -> set:
    """（旧接口）响应侧字段**名** —— `response_field_paths` 的投影。"""
    return {path[-1] for path in response_field_paths(text or "")}


def section_tree(doc_text: Text) -> List[Tuple[int, int, Text, set]]:
    """每个标题子树：`(起, 止, 标题, 本级响应侧字段**路径**集合)`（按文档顺序，含嵌套）。

    ★**公开**给装配器用：`check_response_ownership()` 逐条判断言，逐条重算节表会明显变慢
    （真文档上 ≈ 每算一次要扫 17 个节），所以算一次、传下去。
    ★§9.32：集合里装的是**路径**（`("data", "total")`）而不是名字 —— 归属判据要的是层级；
      需要名字的地方请用 `{path[-1] for path in paths}` 投影（**不要再各算一份**）。
    """
    tree = []
    for span in heading_spans(doc_text or ""):
        tree.append(
            (span.start_line, span.end_line, span.title or "", response_field_paths(span.text))
        )
    return tree


def _section_tree(doc_text: Text) -> List[Tuple[int, int, Text, set]]:
    return section_tree(doc_text)


def _spans_containing(tree: Sequence, text: Text, quote: Text) -> List[Tuple[int, int, set]]:
    """含引用句的节里，**最内层**的那些（祖先节的名字/文本是整棵子树的并集，不能用）。"""
    matched = [
        (start, stop, names)
        for (start, stop, _title, names) in tree
        if quote and quote in _span_text(text, start, stop)
    ]
    return [
        (start, stop, names)
        for (start, stop, names) in matched
        if not any(
            other_start >= start
            and other_stop <= stop
            and (other_start, other_stop) != (start, stop)
            for (other_start, other_stop, _names) in matched
        )
    ]


def section_text_for(doc_text: Text, *, quote: Text = "", tree: Optional[Sequence] = None) -> Text:
    """引用句所在**最内层节**的原文（判"**本节**文档说了什么"，例如响应是不是纯文本/二进制）。

    ★为什么必须是"本节"而不是"整篇"（实测踩到）：真实文档里只要有**一个**接口是二进制下载，
    整篇判定就会把"整包当字符串比"在**所有**节都放行 → 闸门形同虚设 ✗。
    """
    text = doc_text or ""
    tree = list(_section_tree(text) if tree is None else tree)
    matched = _spans_containing(tree, text, quote)
    if not matched:
        return ""
    start, stop = min(matched, key=lambda item: item[1] - item[0])[:2]
    return _span_text(text, start, stop)


def response_field_paths_for(
    doc_text: Text, *, line_no: int = 0, quote: Text = "", tree: Optional[Sequence] = None
) -> Optional[set]:
    """`body.<路径>` 该拿哪一节的响应侧**路径**去对 —— 由内向外找**第一个有响应证据**的节。

    定位方式二选一：`line_no`（确定性通路有行号）或 `quote`（模型通路只有引用句）。
    `tree` 可传**已算好**的节表（`section_tree` 的结果）——逐行/逐条调用时省掉重复扫描。

    返回值 `None` = **定位不到**（引用句在文档里找不到 / 没有节）→ 调用方**不许判**（宽容），
    这比"拿整篇并集去判"安全：拿不准的时候不乱摘。
    """
    text = doc_text or ""
    tree = list(_section_tree(text) if tree is None else tree)
    if not tree:
        return None
    public: set = set()
    for (start, stop, title, _names) in tree:
        # ★跳过**文档标题**那一节（`start_line == 1`）：它的跨度是**全篇**，
        #   当成公共节就等于"拿整篇并集判"（实测踩到，见 `PUBLIC_SECTION_RE` 的注释）。
        if start <= 1:
            continue
        if PUBLIC_SECTION_RE.search(title):
            public |= response_field_paths(_span_text(text, start, stop))

    if line_no:
        # ① 行号定位（确定性通路）：行号唯一 → 由内向外找**第一个有响应证据**的节
        scope = [
            (stop - start, names)
            for (start, stop, _title, names) in tree
            if start <= line_no <= stop
        ]
        if not scope:
            return None
        scope.sort(key=lambda item: item[0])
        for _size, names in scope:
            if names:
                return names | public
        # 本节链上一条响应证据都没有 → 整篇并集（宽容：宁可不判也不误摘）
        whole = response_field_paths(text) | public
        return whole or None

    if not quote:
        return None
    # ② 引用句定位（模型通路）：★取**所有**含该引用句的节的并集——引用句可能**指多节**
    #    （`请求体`/`成功响应` 这种标题在多节里都有），此时只在"**每一节都没有**"时才敢判它
    #    不在响应侧。实测教训：原先"挑跨度最小的那一节"，于是在 `请求体` 歧义时挑到了**另一个
    #    接口**的请求体，把 `body.amount` 误摘了（正是 T10 误伤率审计要防的形态）。
    matched = _spans_containing(tree, text, quote)
    if not matched:
        return None
    minimal = [names for (_start, _stop, names) in matched]
    union: set = set(public)
    for names in minimal:
        union |= names
    if union:
        return union
    # ★§9.32：最内层那一节**本身没有**响应证据（实测形态：引用句正是「请求体」小节里的一行 ——
    #   请求表有「必填」列 → 不算响应证据）→ 由内向外逐层找**第一个有响应证据**的节，
    #   与上面 `line_no` 分支**同一条宽容线**；一层都没有才退回整篇并集。
    #   ⚠️ 不做这一步的话，属于本接口的合法断言会因为"引用句落在请求体小节"而被**误判成判不了**，
    #     于是 §9.32 的路径判据在确定性通路上**根本不生效**（实测：撞名那条照样被放行 ✗）。
    for _start, _stop, names in sorted(matched, key=lambda item: item[1] - item[0]):
        if names:
            return names | public
    whole = response_field_paths(text) | public
    return whole or None


def response_field_names_for(
    doc_text: Text, *, line_no: int = 0, quote: Text = "", tree: Optional[Sequence] = None
) -> Optional[set]:
    """（旧接口）`body.X` 该拿哪一节的响应侧**名字**去对 —— `response_field_paths_for` 的投影。

    ★保留它是因为**有一处真的需要名字**：`assembler` 判 `contains: [body, "<值>"]` 里的
      `<值>` 是不是**响应里的键**（那次判据与层级无关）——见那里的调用点注释。
    """
    paths = response_field_paths_for(doc_text, line_no=line_no, quote=quote, tree=tree)
    if paths is None:
        return None
    return {path[-1] for path in paths}


def _span_text(doc_text: Text, start_line: int, stop_line: int) -> Text:
    lines = doc_text.split("\n")
    return "\n".join(lines[max(0, start_line - 1) : stop_line])


# ---------------------------------------------------------------------------
# 推导：**成功响应示例** → 信封断言（§9.14 的"正面半"）
# ---------------------------------------------------------------------------
# ★为什么这一半必须有（否则归属闸门会让确定性通路"白跑"）：把请求侧字段的断言摘掉之后，
#   像「统一文件服务平台」这类**只写信封、成功响应里 `data` 为 `null`** 的文档会一条断言都不剩。
#   而信封**是**文档承诺的事实（`success: true` / `errorCode: null` / `code: 0`）——
#   照文档实现的服务**必然满足**它，且它真的能测出"业务码/成功标志错"这类缺陷。
SUCCESS_HEAD_RE = re.compile(r"成功|success|正常", re.IGNORECASE)
FAILURE_HEAD_RE = re.compile(r"失败|错误|异常|error|fail", re.IGNORECASE)


def derive_from_response_examples(doc_text: Text) -> Tuple[List[DerivedAssertion], List[Text]]:
    """从**成功响应示例**的**顶层标量**推导 `equal` 断言（只取 bool / null / 数字）。

    三条**刻意不做**（每条都在跳过说明里点名，进 `unknowns` 让人看见）：

    - **字符串不断言**：`message` 这类文案可变（`"操作成功"` 不等于"契约就是这个串"）；
    - **容器不断言**：对象/数组由「响应数据表 → schema」（口径 B2）负责，这里不重复；
    - **失败响应不断言**：它要靠**非法请求**才触发，而本用例发的是文档给的正常请求——
      把错误码断言进正常用例，正是实测里"跑不绿"的一大类（`body contains FILE_1018`）✗。
    """
    out: List[DerivedAssertion] = []
    skipped: List[Text] = []
    text = doc_text or ""
    for match in _FENCE_RE.finditer(text):
        head = text[: match.start()]
        # ★`line_no` 取**围栏内第一行内容**（不是围栏那一行）：本模块自检 ⑧ 要求
        #   "`line_no` 指到的那一行必须被引用句包含"——引用句是**整块正文**，所以指向
        #   正文首行才对得上（指向 ```` ```json ```` 那行会被判"行号对不上"）。
        line_no = head.count("\n") + 2
        marker = ""
        for line in reversed(head[-200:].split("\n")):
            if line.strip():
                marker = line.strip()
                break
        if FAILURE_HEAD_RE.search(marker):
            skipped.append(
                f"第 {line_no} 行的**失败响应**示例：要靠非法请求才触发 → 不产断言"
                "（本用例发的是文档给的正常请求；负例该单列用例）"
            )
            continue
        if not SUCCESS_HEAD_RE.search(marker):
            continue
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            check = f"body.{key}"
            if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
                out.append(
                    DerivedAssertion(
                        comparator="equal",
                        check=check,
                        expect=value,
                        source_quote=match.group(1).strip(),
                        reason=(
                            f"第 {line_no} 行的**成功响应**示例里 `{key}` 是标量"
                            f"（{value!r}）→ 按文档断言（统一响应格式是文档承诺的事实）"
                        ),
                        line_no=line_no,
                    )
                )
            elif isinstance(value, str):
                # ★字符串：只断言**类型**，不按值断言 —— 文案（`"操作成功"`）、token、订单号
                #   这类值本来就会变，按值断言会误报；但"它必须是字符串"是**契约** ✓。
                out.append(
                    DerivedAssertion(
                        comparator="type_match",
                        check=check,
                        expect="str",
                        source_quote=match.group(1).strip(),
                        reason=(
                            f"第 {line_no} 行的**成功响应**示例里 `{key}` 是字符串 → "
                            "只断言**类型**（示例值可能只是样例，按值断言会误报）"
                        ),
                        line_no=line_no,
                    )
                )
            else:
                skipped.append(
                    f"第 {line_no} 行的成功响应示例：`{key}` 是**结构**（对象/数组）→ "
                    "不按值断言（结构由响应数据表/schema 覆盖）"
                )
    return out, skipped


def derive_assertions(
    doc_text: Text, *, query_scopes: Sequence[Tuple[int, int]] = ()
) -> Tuple[List[DerivedAssertion], List[Text]]:
    """对外主函数：整篇文档 → `(断言清单, 未生成说明)`。

    **纯函数**：不调模型、不写盘（§5.2 的"确定性映射先做"）。
    第二个返回值给 `unknowns` 用——**"我们没断言什么"和"我们断言了什么"一样重要**。

    ★`query_scopes`（§9.16，可选）：**GET / HEAD 接口段**的行号范围（由调用方按段给）。
    这些段里没有请求体 → 那些"请求体表"其实就是**查询参数** → 不产响应断言（见 `read_field_rows`）。
    ★默认 `()` = 老行为一字不变。
    """
    assertions: List[DerivedAssertion] = []
    skipped: List[Text] = []

    # ★响应数据表（口径 B2）：**一条 `jsonschema_match` 覆盖整表**；它的数据行**不再**逐行
    #   产出"必填列没写清 → 不断言"——否则报告里会有几十条自相矛盾的说明（字段其实已被覆盖）。
    table_assertions, table_skipped, table_lines = derive_from_response_tables(doc_text)
    # ★§9.29 **响应 Cookie 表** → 一条 `jsonschema_match` 挂在 `cookies` 上（口径与响应数据表**完全一致**）
    cookie_assertions, cookie_skipped, cookie_lines = derive_from_cookie_tables(doc_text)
    table_lines |= cookie_lines
    # ★§9.33 **响应头表** → 逐行产 `headers.<名>` 断言（固定值 → `equal`；枚举 → `contained_by`；无据点名）
    header_assertions, header_skipped, header_lines = derive_from_response_headers(doc_text)
    table_lines |= header_lines

    # ★§9.14：节表只算一次，逐行复用（逐行重算是 O(行数 × 节数 × 每节扫描)，真文档上会明显变慢）
    tree = _section_tree(doc_text)
    for row in read_field_rows(doc_text, query_scopes=query_scopes):
        if row.line_no in table_lines:
            continue
        derived, notes = derive_from_field(
            row, response_field_names_for(doc_text, line_no=row.line_no, tree=tree)
        )
        # ★补行号（C-2 的"按行号 ∈ 片的行号范围"绑定靠它）：`derive_from_field` 不知道
        # 自己在第几行，由这里**一处记账**盖上去——避免在 6 个构造点重复写。
        assertions.extend(replace(item, line_no=item.line_no or row.line_no) for item in derived)
        skipped.extend(notes)

    derived_notes, notes_skipped = derive_from_response_notes(doc_text)
    assertions.extend(table_assertions)
    skipped.extend(table_skipped)
    assertions.extend(cookie_assertions)
    skipped.extend(cookie_skipped)
    assertions.extend(header_assertions)
    skipped.extend(header_skipped)
    assertions.extend(derived_notes)
    skipped.extend(notes_skipped)

    # ★§9.14 的"正面半"：**成功响应示例的信封标量**（`success`/`errorCode`/`code` …）。
    #   归属闸门摘掉请求侧字段之后，这一半保证"文档只写信封"的接口**仍有可跑绿的断言**。
    example_assertions, example_skipped = derive_from_response_examples(doc_text)
    assertions.extend(example_assertions)
    skipped.extend(example_skipped)

    if not assertions and not skipped:
        # ★**"什么都没说"是最坏的一种**：用户看到一片空白，不知道是"文档没法确定断言"
        # 还是"工具坏了"。所以这里显式说明，并指出下一步该走哪条路。
        skipped.append(
            "整篇没有可确定性推导的要素（没有字段表，也没有带类型的说明句）——"
            "这条通路**不猜**；请人工补断言，或改用 `haify gen`（让模型读全文）"
        )

    return assertions, skipped


# ---------------------------------------------------------------------------
# §9.16 产物的 `request`：把**请求字段表**装成 `headers` / `params` / `json` / `data`
# ---------------------------------------------------------------------------
# ★为什么要有这一段（2026-09-27，实测依据）：改造前 `to_draft_payload` 只写 `method`/`url`
#   —— **文档写明的请求参数一个都没发出去** ✗。用例"能生成、能加载、能断言"，
#   但对真实服务来说它是**空请求**（要么 400，要么测的根本不是那个场景）。
#
# ★三条纪律（写在代码里，避免下一个人误判）：
#   ① **示例值当"请求值"合法，当"期望值"不合法** —— T28 ④ 管的是 `expect`；
#      请求值就是"照文档示例发一次"，与"把举例当契约"是两件事（后者在断言侧仍被拦 ✓）。
#   ② **拿不到值不猜**：写 `${ENV(TODO_<字段>)}`（运行期 `EnvNotFound` **响亮报错**）+ 登记 pending。
#   ③ **凭据不进产物**：`Authorization`/`token`/`password` 这类名字一律 `${ENV(名字)}`，
#      连文档示例里的 token 也不抄（脱敏是硬线）。
_SAMPLE_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.S)
# 「请求体示例」的上下文词（与 `_iter_sample_blocks` 同口径，但**只认请求侧**）
_REQUEST_SAMPLE_HEAD_RE = re.compile(r"请求|示例|request|example", re.I)
_FORM_CONTENT_RE = re.compile(r"x-www-form-urlencoded", re.I)
_MULTIPART_RE = re.compile(r"multipart/form-data", re.I)
# ★§9.16 说明列里的**跨字段一致性要求**（实测原文：「必须与请求参数中的 `path` 字段完全相同」）：
#   这类字段的值**不能只看自己那一格**——按文档示例值填是"有出处"的，但值对不对要人工确认，
#   所以命中就**记一条 note**（可见），而不是自作聪明地去改另一个字段的值（那是猜 ✗）。
_CONSISTENCY_HINT_RE = re.compile(
    r"必须.{0,40}(相同|一致)|需.{0,40}(相同|一致)|与.{0,30}(相同|一致)|保持一致"
)


def _coerce_example(text: Text, type_text: Text) -> Any:
    """按文档声明的类型把**示例值**转成合适的字面量（`"2"` + `integer` → `2`）。

    ★判不了就原样返回字符串（**不猜**）：类型列写 `number` 这种歧义词时，保持文档原文最安全。
    """
    value = (text or "").strip().strip("`")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    canon, _note = _normalize_type(type_text)
    if canon == "int":
        try:
            return int(value)
        except ValueError:
            return value
    if canon == "float":
        try:
            return float(value)
        except ValueError:
            return value
    if canon == "bool":
        lowered = value.lower()
        if lowered in ("true", "false"):
            return lowered == "true"
        if value in ("是", "否"):
            return value == "是"
        return value
    return value


# ---------------------------------------------------------------------------
# §9.18 **curl 示例当取值来源**（「复制为 cURL」是接口文档里最普遍的一档"能跑的请求"）
# ---------------------------------------------------------------------------
# ★为什么值得单独做（登记项 §9.16 第六节第 1 项）：真实客户文档里有 **16 处 curl**，而且是
#   **一次完整调用**——请求头、请求体、查询串都在里面（实测：`-H "X-Path: /app1/documents/"`）；
#   比"只给一格的示例值列"更接近真实请求。
# ★纪律**不变**：curl 只作**取值来源**，**不**凭空新增字段（表里没有的名字不放进去）；
#   凭据仍走脱敏（`Authorization: Bearer …` 绝不落产物）。
_CURL_HEADER_RE = re.compile(
    r"-H\s+(?:\"([^\"]*)\"|'([^']*)')"  # ★允许内层出现另一种引号（`-d '{"a": 1}'` 这种很常见）
)
_CURL_DATA_RE = re.compile(
    r"(?:--data-raw|--data-binary|--data|-d)\s+(?:\"([^\"]*)\"|'([^']*)')"
)
_CURL_URL_RE = re.compile(r"curl\b[^\n]*?(https?://[^\s\"']+|(?<![\w/])(/[A-Za-z0-9_\-./{}$]+))")


def _curl_quote_value(groups: Tuple[Optional[Text], ...]) -> Optional[Text]:
    """从 `字面量` 的两个可选捕获组里取非空的那个。"""
    for text in groups:
        if text:
            return text
    return None


def _curl_blocks(section_text: Text) -> List[Text]:
    """取出**本段**的 curl 命令（先把 `\\` 续行拼成一行 —— curl 基本都这么换行）。"""
    joined = re.sub(r"\\\s*\n\s*", " ", section_text or "")
    return [line for line in joined.split("\n") if re.match(r"^\s*curl\b", line, re.I)]


def curl_request_values(section_text: Text) -> Dict[Text, Dict[Text, Any]]:
    """本段 curl 示例里的 `{"headers": …, "json": …, "params": …}`（每个值都有出处）。

    - `-H "K: V"` → `headers`（**名字**与表里的一致才用得上，见 `request_payload_from_rows`）；
    - `-d/--data* '{"a": 1}'` → `json`（**能解析成 JSON 对象**才算；不是 JSON 的不猜）；
    - URL 里的 `?a=1&b=2` → `params`（值做 URL 解码）。
    """
    out: Dict[Text, Dict[Text, Any]] = {}
    for block in _curl_blocks(section_text):
        for raw_header in _CURL_HEADER_RE.findall(block):
            header = _curl_quote_value(raw_header) or ""
            name, separator, value = header.partition(":")
            if not separator:
                continue
            out.setdefault("headers", {})[name.strip("` ").strip()] = value.strip()
        for raw_body in _CURL_DATA_RE.findall(block):
            body = (_curl_quote_value(raw_body) or "").strip()
            if not body:
                continue
            try:
                payload = json.loads(body)
            except ValueError:
                continue  # 非 JSON 的 `-d`（表单串/XML）**不猜**
            if isinstance(payload, dict):
                out.setdefault("json", {}).update(
                    {str(key): value for key, value in payload.items()}
                )
        found = _CURL_URL_RE.search(block)
        if not found:
            continue
        _, _, query = found.group(1).partition("?")
        for pair in (query or "").split("&"):
            if "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            out.setdefault("params", {})[key] = unquote(value)
    return out


def _request_sample_values(doc_text: Text, *, start_line: int, end_line: int) -> Dict[Text, Any]:
    """本段「请求体示例」里的 `字段名 → 值`（只取**围栏内能解析的 JSON 对象**的顶层标量）。

    ★为什么按段取：跨段取会张冠李戴（§9.6 测过"打认证接口的用例带着创建目录的字段"）——
    归属判据只有"行号落在本段内"这一条唯一口径。
    """
    text = doc_text or ""
    found: Dict[Text, Any] = {}
    for match in _SAMPLE_FENCE_RE.finditer(text):
        line_no = text[: match.start()].count("\n") + 2  # 围栏内首行的 1-based 行号
        if not (start_line <= line_no <= end_line):
            continue
        head = text[: match.start()][-120:]
        if not _REQUEST_SAMPLE_HEAD_RE.search(head):
            continue
        try:
            payload = json.loads(match.group(1).strip())
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                found.setdefault(str(key), value)
    return found


def _auth_section_ref(doc_text: Text) -> Optional[Tuple[Text, int, int]]:
    """文档里"讲怎么取得凭据"的那一节（标题命中 `_AUTH_SECTION_RE`）→ `(标题, 起行, 止行)`。

    ★取**文档顺序里的第一节**：实测形态是「认证方式」写在概述里、各接口节之前。
    ★跳过**文档标题那一节**（`start_line <= 1`）：它的跨度是**全篇**，当成"某一节"等于没指。
    """
    for span in heading_spans(doc_text or ""):
        if span.start_line <= 1:
            continue
        if _AUTH_SECTION_RE.search(span.title or ""):
            return (span.title or "", span.start_line, span.end_line)
    return None


def _credential_hint(row: FieldRow, doc_text: Text) -> Text:
    """凭据值的**取证提示** —— 两部分都来自文档；两样都没有就如实说"文档没写"。

    ① 该行「说明」列**原文**（最贴近：常常直接写着 `OAuth2.0 Bearer Token` 这种）；
    ② 文档里讲认证的那一节（给**标题 + 行号**，人可以直接跳过去看怎么换 token）。
    """
    parts: List[Text] = []
    note = (row.note or "").strip()
    if note:
        parts.append(f"文档「说明」列写着：{note}")
    section = _auth_section_ref(doc_text)
    if section:
        parts.append(f"取证方式见文档『{section[0]}』节（第 {section[1]}~{section[2]} 行）")
    if not parts:
        return "★文档**没有**说明这个值怎么取得 → 交人工确认（工具不编）"
    return "；".join(parts)


def request_payload_from_rows(
    rows: Sequence[FieldRow],
    doc_text: Text,
    *,
    start_line: int = 0,
    end_line: int = 0,
    public_scopes: Sequence[Tuple[int, int]] = (),
    method: Text = "",
) -> Tuple[Dict[Text, Any], List[Text]]:
    """把**请求字段表**装成产物的 `request`（`headers` / `params` / `json` / `data`）。

    返回 `(payload, notes)`；`notes` 是**必须让人看见**的说明（由调用方写进 `unknowns`）：
    可选字段为什么没发、哪些值成了 `TODO_` 占位、凭据是怎么处理的、内容类型怎么定的。

    取值顺序（**每一档都有出处，不编**）：

        ① 表格「示例值」列             ← 文档对该字段的直接示例
        ② 本段「请求体示例」的同名字段   ← 整块示例里给的值
        ③ `${ENV(TODO_<字段>)}`         ← 文档确实没给 → 等人填（运行期响亮报错）

    两类**无条件特殊处理**：名字像凭据的 → `${ENV(名字)}`（不抄文档里的 token）；
    「可选」字段 → **默认不发**（与"可选字段不断言"同一精神：不发就不会制造假失败）。
    ★**响应数据表不会漏进来**：它们没有「必填」列 → 判不出"必填" → 不进请求（这条是免费的 ✓）。
    """
    payload: Dict[Text, Any] = {}
    notes: List[Text] = []
    sample_values = _request_sample_values(doc_text, start_line=start_line, end_line=end_line)
    lines = (doc_text or "").split("\n")
    section_text = "\n".join(lines[max(start_line - 1, 0) : end_line or None])
    # ★§9.18 curl 示例（本段）——**取值优先级最高**（它是一次完整可跑的调用）
    curl_values = curl_request_values(section_text)

    in_scope = [
        row
        for row in rows
        if (start_line and start_line <= row.line_no <= end_line)
        # ★公共节只继承**请求头**（「公共请求头」是唯一常见形态）：把公共的**请求体/响应数据表**
        #   也套给每个接口，正是"错位"要防的事 ✗。
        or (
            row.location == "headers"
            and any(low <= row.line_no <= high for low, high in public_scopes)
        )
    ]
    form_encoded = bool(_FORM_CONTENT_RE.search(section_text))
    multipart = bool(_MULTIPART_RE.search(section_text))
    # ★§9.16 **GET/HEAD 没有请求体**（协议事实）：这类方法下"请求体表"里的字段按**查询参数**发。
    #   ★实测踩到：真实文档的 `GET /api/directory/query` 请求字段表写「字段」，直接落 `json`
    #     就成了"GET 带 JSON 体"——服务端多半不认 ✗。这是协议级判断，且**记进 notes**（可见）。
    body_fields_go_query = (method or "").strip().upper() in ("GET", "HEAD")
    skipped_optional = 0
    todo_names: List[Text] = []
    secret_names: List[Text] = []
    # ★§9.28：凭据名 → 取证提示（只从文档取；见 `_credential_hint`）
    secret_hints: Dict[Text, Text] = {}
    consistency_names: List[Text] = []
    curl_names: List[Text] = []
    # ★§9.27：从**公共请求头**节继承来的头（跨接口契约，按段归属继承）——要**看得见**，不静默
    inherited_headers: List[Text] = []

    for row in in_scope:
        required, _optional = _truthy_required(row.required_text)
        if not required:
            skipped_optional += 1
            continue
        name = row.name
        # ★先算**目标落点（桶）**，再谈取值 —— **顺序即口径**，且 curl 的取值也要按同一套桶去查
        #   （改造前 curl 的键是"桶"、行上是"location"，body ↔ json 对不上 → curl 的值永远取不到 ✗）。
        if row.location == "headers":
            bucket = "headers"
        elif row.location == "params":
            bucket = "params"
        elif multipart:
            bucket = "upload"
        elif body_fields_go_query:
            bucket = "params"
        else:
            bucket = "data" if form_encoded else "json"

        from_curl = (curl_values.get(bucket) or {}).get(name, _MISSING)
        if _SECRET_NAME_RE.search(name):
            value: Any = _env_placeholder(name)
            secret_names.append(name)
            # ★§9.28：告诉人"这个占位该填什么、去哪拿"——提示**只从文档取**（不编）
            secret_hints.setdefault(name, _credential_hint(row, doc_text))
        elif from_curl is not _MISSING:
            # ★§9.18：curl 是一次**完整可跑的调用** → 它的值优先于"只给一格的示例值列"
            value = from_curl
            curl_names.append(name)
        elif row.example_text:
            value = _coerce_example(row.example_text, row.type_text)
            if _CONSISTENCY_HINT_RE.search(row.note or ""):
                consistency_names.append(name)
        elif name in sample_values:
            value = _coerce_example(str(sample_values[name]), row.type_text)
        else:
            value = todo_placeholder(name)
            todo_names.append(name)

        if bucket == "upload":
            # ★§9.20：内核侧支持 `upload`（`IRStep.upload` → YAML `upload:`），草案契约本轮也补上了；
            #   但**文件路径编不出来** → 一律 `${ENV(TODO_<字段>)}` 占位（人工/环境给），并点名。
            payload.setdefault("upload", {})[name] = todo_placeholder(name)
            notes.append(
                f"{name}：本节是 `multipart/form-data`（文件上传）→ 已写进请求的 `upload`，"
                f"但**值是 ${todo_placeholder(name)} 占位**（文件路径编不出来，必须人工/环境给）"
            )
        else:
            payload.setdefault(bucket, {})[name] = value
        if row.location == "headers" and any(
            low <= row.line_no <= high for low, high in public_scopes
        ):
            inherited_headers.append(name)

    for key in ("json", "params", "headers"):
        if isinstance(payload.get(key), dict) and not payload[key]:
            payload.pop(key)
    if body_fields_go_query and payload.get("params"):
        notes.append(
            f"`{(method or '').strip().upper()}` **没有请求体** → 本节的请求字段已按**查询参数**"
            "（`params`）发出（协议级判断；若该接口确实要 body，请人工改）"
        )
    if "json" in payload:
        # 发了 JSON 体却没有内容类型 → 补一个**协议级**默认（`bench/golden/*.yml` 也是这么写的）；
        # ★它是协议事实、不是业务声明，而且**记进 notes**（可见，不静默）。
        # ★实测（2026-09-27，被自己的用例抓到）：原来写成"headers 已存在才补"——于是**没有头表**的
        #   接口（很常见）发 JSON 体时**不带 Content-Type** ✗，请求会被服务端按别的类型解。
        headers = payload.setdefault("headers", {})
        if not any(str(key).lower() == "content-type" for key in headers):
            headers["Content-Type"] = "application/json"
            notes.append("请求体是 JSON → 已补 `Content-Type: application/json`（文档未写该头）")
    if skipped_optional:
        notes.append(
            f"有 {skipped_optional} 个**可选**字段按保守口径**没有发**"
            "（与「可选字段不断言」同一精神：不发就不会制造假失败；需要时人工补）"
        )
    todo_names = list(dict.fromkeys(todo_names))  # 同名只点一次（公共节 + 本节都可能有一行 ✓）
    if todo_names:
        notes.append(
            "以下字段文档没给值 → 已写 `${ENV(TODO_…)}` 占位："
            + "、".join(todo_names)
            + "（运行期 `EnvNotFound` **响亮报错**；填好再转正）"
        )
    secret_names = list(dict.fromkeys(secret_names))
    if secret_names:
        detail = "\n".join(
            f"      · `{name}` → 环境变量 `{_env_var_name(name)}`"
            f"（{secret_hints.get(name) or '（没有提示）'}）"
            for name in secret_names
        )
        notes.append(
            "以下字段是凭据 → 只写 `${ENV(名字)}`，**没有**抄文档里的示例值：\n"
            + detail
            + "\n      （运行期 `EnvNotFound` **响亮报错**；按上面的出处把值填进环境变量再转正）"
        )
    consistency_names = list(dict.fromkeys(consistency_names))
    curl_names = list(dict.fromkeys(curl_names))
    inherited_headers = list(dict.fromkeys(inherited_headers))
    if inherited_headers:
        notes.append(
            "以下请求头取自**公共请求头**节（跨接口契约，已按段归属继承到本接口）："
            + "、".join(inherited_headers)
            + " —— 若本接口其实不要这些头，请把该节拆开或在本节显式说明"
        )
    if curl_names:
        notes.append(
            "以下请求值取自**本段的 curl 示例**（它是一次完整可跑的调用，优先于「示例值」列）："
            + "、".join(curl_names)
        )
    if consistency_names:
        notes.append(
            "以下字段的值**必须与别的字段保持一致**（说明列有「…相同 / 一致」的要求）："
            + "、".join(consistency_names)
            + " —— 已按文档示例值填入，**请人工确认它与对应字段对得上**"
        )
    return payload, notes


def to_draft_payload(
    doc_text: Text,
    *,
    case_name: Text,
    url: Text,
    method: Text = "POST",
    step_name: Text = "",
    assertions: Optional[Sequence[DerivedAssertion]] = None,
    request: Optional[Dict[Text, Any]] = None,
) -> Dict[Text, Any]:
    """把推导结果装成 **CaseDraft 的 JSON 形态**（`schema.parse_draft` 能直接吃）。

    ★为什么要有这个工厂：`--assertions-only` 必须走**与模型产物完全相同的下游**
    （`parse_draft` → 装配器 → T28 对账 → 闸门 → 落盘）。**新增一条通路，不新增一条链路**
    ——否则"确定性映射产的东西"和"模型产的东西"会各自演化（本仓吃过双源的亏）。

    ★`assertions`（可选，2026-09-26 补）：**已经推导好**的断言清单——逐片产出时由调用方
    **按接口段过滤**后传入（见 `gen.generate_assertions_only_all` 的归属闸门）。
    传了就不再自己推导：既避免"同一份文档推导两次"，也保证"过滤口径"只有一处。

    ★`request`（可选，§9.16，2026-09-27 补）：**请求侧载荷**（`headers`/`params`/`json`/`data`），
    由 `request_payload_from_rows()` 从**本段**的请求字段表装配。**默认 `None` = 老行为一字不变**
    （成对判据：不传时产物仍然只有 method/url，外部调用方不会被这次改动影响）。

    没有任何断言时写 `validate: []` 是**合法**的（§3.3 的 S3 三态之一：显式声明不做断言）——
    它会触发 S3 的 PENDING（提请人工），而不是被当成错误。
    """
    if assertions is None:
        assertions, _skipped = derive_assertions(doc_text)
    step: Dict[Text, Any] = {
        "name": step_name or case_name,
        "method": method,
        "url": url,
        "validate": [
            {
                "comparator": item.comparator,
                "check": item.check,
                "expect": item.expect,
                "source_quote": item.source_quote,
            }
            for item in assertions
        ],
    }
    for key in ("headers", "params", "json", "data", "upload"):
        value = (request or {}).get(key)
        if value:  # 空 dict / 空串不写（`{}` 落进产物会多一个"看起来有内容"的空壳）
            step[key] = value
    return {"case_name": case_name, "steps": [step]}


_SELFTEST_DOC = """# 下单接口

## POST /api/order

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| sku | string | 是 | 商品编码 |
| quantity | integer | 是 | 数量，1 ~ 99 |
| amount | number | 是 | 金额 |
| sort | string | 否 | 枚举：price_asc / price_desc |
| note | string | 否 | 备注 |
| authToken | string | 是 | 鉴权串（**只在请求侧**：响应里没有它） |

**成功响应**：

```json
{"code": 0, "data": {"orderNo": "SO-1"}}
```

- data.orderNo：字符串，订单号
- data.list[].sku：字符串，商品编码
- data.quantity：整数，数量
- data.amount：数字，金额
- data.sort：字符串，排序方式（**可选**：响应里可能不出现）
"""


def run_selftest(verbose: bool = False) -> int:
    """assertions 自检（**纯逻辑、不写盘**）。返回 0=全绿；非 0=失败项数。

    NOTICE（为什么自检里用**内置小文档**而不是 15 份 golden）：包的自检不该依赖
    目录结构（`bench/golden` 不存在时它也得能跑）。"15 份 golden 全部推导成功 +
    伪存在性 0 条"的判据在 `tests/assertions_test.py` 里，那里读真文档。
    """
    from interfacetester.make import BUILTIN_COMPARATOR_NAMES  # noqa: PLC0415

    from interfacetester_ai.assembler import quote_hits  # noqa: PLC0415
    from interfacetester_ai.schema import parse_draft  # noqa: PLC0415

    failures: List[Text] = []
    doc = _SELFTEST_DOC
    assertions, skipped = derive_assertions(doc)

    # ① 四条映射都出得来（类型 / 范围两端 / TODO_ / 响应说明句）
    pairs = {(item.comparator, item.check) for item in assertions}
    for expected in (
        ("type_match", "body.sku"),
        ("type_match", "body.quantity"),
        ("greater_or_equals", "body.quantity"),
        ("less_or_equals", "body.quantity"),
        ("type_match", "body.data.orderNo"),
        # ★§9.14 的"正面半"：成功响应示例里的**信封标量**（`code`）也要出得来
        ("equal", "body.code"),
    ):
        if expected not in pairs:
            failures.append(f"[漏映射] 缺少 {expected}；实际 {sorted(pairs)}")

    # ①-b ★§9.14 响应侧归属（**成对判据**）：请求侧独有的字段（`authToken`）**不许**产响应断言
    #     —— 断言 `body.authToken` 时响应里根本没有它，照文档实现的服务必然让用例失败；
    #     而同一个字段**只要响应侧也有据**（`sku`/`quantity` 在响应说明句里）就必须照常断言。
    if any(item.check == "body.authToken" for item in assertions):
        failures.append("[归属闸门失效] 请求侧独有的 `authToken` 被断言在响应 `body` 上了")
    if not any("body.authToken" in note and NOTE_RESPONSE_OWNERSHIP in note for note in skipped):
        failures.append("[静默] 归属闸门摘掉 `authToken` 却没写原因（'没断言什么'也要能看见）")
    if not any(item.check == "body.sku" for item in assertions):
        failures.append("[归属闸门误伤] `sku` 在响应侧有据（响应说明句），不该被摘")

    # ② **算子名必须在内核白名单里**（写错名字会在 L2 才炸，离原因很远）
    unknown = sorted({item.comparator for item in assertions} - set(BUILTIN_COMPARATOR_NAMES))
    if unknown:
        failures.append(f"[算子名不在白名单] {unknown}")

    # ③ **伪存在性断言 0 条**（S1 硬拦的形态，我们绝不该生成）
    pseudo = [
        item.to_dict()
        for item in assertions
        if (item.comparator == "not_equal" and item.expect in ("", None))
        or (item.comparator == "contains" and item.expect in ("", None))
    ]
    if pseudo:
        failures.append(f"[伪存在性断言] 生成了 {pseudo}")

    # ④ 可选字段**不生成**任何断言（存在性/类型/范围/枚举都不做）
    optional_checks = {item.check for item in assertions} & {"body.sort", "body.note"}
    if optional_checks:
        failures.append(f"[误伤] 可选字段被断言了：{sorted(optional_checks)}")
    if not skipped:
        failures.append("[覆盖口径] 跳过原因不该为空（'没断言什么'也要能看见）")

    # ⑤ 类型不明确（`number`）→ **不断言类型**，只记跳过。
    #    为什么不是"写 TODO_ 占位"（实测踩到）：`type_match` 的 expect 必须是类型名，
    #    S7 硬闸门会拦 `${ENV(...)}`——**而且拦得对**：文档没说类型时，
    #    我们本来就不该补一条"类型契约"（那是猜）。
    amount = [item for item in assertions if item.check == "body.amount"]
    if amount:
        failures.append(f"[不该断言] amount 的类型不明确，不该生成断言：{amount}")
    if not any("body.amount" in note for note in skipped):
        failures.append("[静默] amount 被跳过却没说明原因（'没断言什么'也要能看见）")

    # ⑥ `TODO_` **机制**本身可用（确定性映射不用它，但模型通路与装配器的
    #    "只许落 .ai/draft/" 落点规则都依赖它——机制坏了那些人会静默失效）
    if todo_expect("X") != "${ENV(TODO_X)}" or not has_todo(todo_expect("X")):
        failures.append("[TODO_ 机制坏] todo_expect / has_todo 不一致")
    if has_todo("${ENV(USERNAME)}"):
        failures.append("[TODO_ 误判] 普通 `${ENV(...)}` 占位不该被当成 TODO_")

    # ⑥ **溯源可过**：每条断言的引用句都逐字来自文档（否则装配器会全降级）
    for item in assertions:
        if not quote_hits(item.source_quote, doc):
            failures.append(f"[溯源不过] {item.check} 的引用句不在文档里：{item.source_quote!r}")

    # ⑦ 产出的 JSON 能被 `schema.parse_draft` 吃下（**走与模型产物相同的下游**）
    payload = to_draft_payload(doc, case_name="下单", url="/api/order")
    try:
        draft = parse_draft(payload)
    except Exception as error:  # noqa: BLE001
        failures.append(f"[下游不兼容] to_draft_payload 产物过不了 parse_draft：{error}")
    else:
        if len(draft.assertions()) != len(assertions):
            failures.append(
                f"[条数不一致] 推导 {len(assertions)} 条，装进 draft 后 {len(draft.assertions())} 条"
            )

    # ⑧ ★行号账（C-2 的片级绑定靠它）：`line_no` 必须**真的**指到文档里那一行——
    #    只"有个正数"和没有一样（判据必须能自证，别写成形式检查）。
    doc_lines = _SELFTEST_DOC.split("\n")
    for item in assertions:
        if item.line_no <= 0:
            failures.append(f"[行号缺失] {item.check} 的 line_no={item.line_no}")
            continue
        if item.line_no > len(doc_lines):
            failures.append(
                f"[行号越界] {item.check} 的 line_no={item.line_no} > 文档 {len(doc_lines)} 行"
            )
            continue
        actual = doc_lines[item.line_no - 1].strip()
        # 引用句就是**那一行的原文**（字段表行 / 说明句）→ 两边必须互相包含
        if item.source_quote not in actual and actual not in item.source_quote:
            failures.append(
                f"[行号对不上] {item.check} 的 line_no={item.line_no} 指向 {actual[:40]!r}，"
                f"而引用句是 {item.source_quote[:40]!r}"
            )

    print("=" * 66)
    if failures:
        print(f"assertions 自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("断言推导自检全部通过（§5.2 确定性映射，**不调模型**）：")
    print(f"  映射  ：类型 / 范围两端 / 枚举 / TODO_ / 响应说明句 → 共 {len(assertions)} 条")
    print("  白名单：算子名全部在内核 BUILTIN_COMPARATOR_NAMES 里（写错会在 L2 才炸）")
    print("  负例  ：**伪存在性断言 0 条**（S1 硬拦的形态我们绝不生成）")
    print(f"  取舍  ：可选字段不断言、`number` 不猜类型、列表路径交人工（跳过 {len(skipped)} 条）")
    print("  溯源  ：每条断言的引用句都逐字来自文档（装配器的 T28 校验可过）")
    print(f"  行号  ：{len(assertions)} 条断言的 line_no 都能指回文档那一行（C-2 片级绑定用）")
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

    parser = argparse.ArgumentParser(description="确定性断言推导（P0b）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "AMBIGUOUS_TYPE_TOKENS",
    "ENUM_RE",
    "MAX_RE",
    "MIN_RE",
    "RANGE_RE",
    "REQUIRED_FALSE_TOKENS",
    "REQUIRED_TRUE_TOKENS",
    "RESPONSE_NOTE_RE",
    "TODO_PREFIX",
    "TYPE_ALIASES",
    "TYPE_MISSING_TOKENS",
    "DerivedAssertion",
    "FieldRow",
    "derive_assertions",
    "derive_from_field",
    "derive_from_response_notes",
    "field_path",
    "has_todo",
    "read_field_rows",
    "run_selftest",
    "to_draft_payload",
    "todo_expect",
]


if __name__ == "__main__":
    raise SystemExit(main())

