# -*- coding: utf-8 -*-
"""fix —— 用例**自愈建议**（v8 §5.8 `haify fix`；**P2a**，2026-09-25）。

## 三条"不接受"（这个模块的立场，先写在最前面）

1. **只建议、不落地**：产物是 unified diff + 理由 + 风险提示；**没有 `--apply`，永远不会有**
   （§5.8 原文）。因为自愈改错会**掩盖真实缺陷**（R3）——那比一次失败更坏：
   失败会被人看见，"修好了"不会。
2. **`status_code` / 业务码类失败一律「疑似真实缺陷、不给改」**——这条**写进代码，不靠 prompt**
   （§5.8 的"触发条件"原文）。`status_code` 从 200 变 500、业务码从 0 变 1001，
   **更可能**是接口真的改错了行为，而不是用例该跟着改。给这类失败出"改断言"的建议，
   等于教人**把 bug 当特性**。
3. **补丁必须过双前置**（`load_testcase_file` + S 系列闸门，红线⑦）：
   最典型的场景就是"字段改名导致断言失败"，而最省事的"修法"是把
   `type_match: [body.data.token, "str"]` 改成 `not_equal: [body.data.token, ""]`
   ——**从能判定改成不能判定**：自愈反而制造了一个**假通过**。

## 零写盘的口径（判据：**目标 YAML 的 mtime 不变**）

双前置里 `load_testcase_file` **只收文件路径**（"重复键"是 YAML **文本层**的事，
dict 层根本看不到），所以"一个字节都不落盘"做不到、也不该那么做。
正确口径是：**不写目标文件**——临时补丁写进 `.ai/work/<job_id>/`
（`workdir.JobWorkspace`，T18 已登记的写入者），退出即删。
**`fix.py` 自己不写盘**（登记表里刻意没有它；与 `evidence` 同处理）。

## ★§9.25 扩触发面：**在 §5.8 的两族之内把覆盖做全**，且每扩一分配一道更硬的闸

§5.8 原文把触发条件钉死在两族：「**`check_value is None` + jmespath 路径类断言**」与
「**`type_match`/字段名类**失败」，并明写 `status_code`/业务码类**一律判疑似真实缺陷、不给改**。
所以"扩面"**不是自创新族**，而是把这两族的**覆盖补全**（改前有两个真窟窿）：

| # | 改动 | 为什么（改前会怎样） | 同步加的**更硬的闸** |
| --- | --- | --- | --- |
| ① | **容器约束**：`check` 的根 ∈ `body`/`headers`/`cookies`，候选**只在同一容器**里找，`new_check` **保留原根** | 改前无论根是什么，都只在**响应体**里找候选、并拼成 `body.*` → `headers.X-Foo` 取不到值会被"修"成 `body.XFoo` ✗（**改错容器 = 改语义**） | 根不在三者内（`status_code`/`text`/`elapsed*`/写错的根）→ **一律拒**；容器不匹配 → 一律拒 |
| ② | **新族 `var_missing`**：`check` 形如 `${var}` 且取不到值 → 在**本 step 的 `extract`** 里找唯一相近变量，建议改 `${new}` | 改前 `${token}` 走的是"路径失效"分支 → 会把它改成 `body.token` ✗✗（**把变量引用换成响应字段**，语义彻底变了） | ①候选**唯一**；②**新名字的取值路径必须在响应里能取到值**（否则只是换个同样取不到的名字 ✗）；③`${fn(...)}` 这类函数调用**仍不碰**（归 `other`，不越界）；④变量名本身是业务码语义（`${code}`）→ **按业务码拒** |

**刻意仍然不给改的（边界，写在这里防"顺手扩"）**：**值语义类**——期望值与实际值不符
（含"字符串数字 ↔ 数字"这种 S 闸门点名的写法 ✗）、状态码/业务码、响应结构与文档冲突。
它们的共同点是"**可能是接口真的改了行为**"：给这类失败出"改断言"的建议，等于教人把 bug 当特性
（R3）。判据见 `tests/fix_test.py::TestTriggerScopeBoundary`（成对：该给的两族给、越界的一律拒）。
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

# 判定结果（只有前三个"可建议"，后两个"不给改"）
FIXABLE_PATH_MISSING = "path_missing"  # `check_value is None`：路径取不到值（字段改名场景）
FIXABLE_VAR_MISSING = "var_missing"  # ★§9.25 新增：`${var}` 变量引用取不到值（extract 名字失效）
FIXABLE_TYPE_MISMATCH = "type_mismatch"  # 类型不符
REFUSE_REAL_DEFECT = "real_defect_suspect"  # ★疑似真实缺陷，不给改
REFUSE_OTHER = "other"  # 不在 §5.8 的触发条件内

# `check` 允许的**容器根**（§9.25）：字段名类失败的改法**只在同一容器内**做。
# ★为什么必须限定：`headers.X-Foo` 取不到值时若去响应体里找候选，会给出 `body.XFoo` ——
#   那不是"修名字"，是**换了断言的语义** ✗（改前就是这么干的）。
# ★为什么**没有** `cookies`：summary 里没有"响应 cookies"这个事实（只有 `Set-Cookie` 头，
#   解析它属于另一件事）→ **判不了就不给**（与"判不了就点名"同取向），而不是猜。
CONTAINER_ROOTS = ("body", "headers")
# `${var}`（**简单变量**；`${fn(...)}` 是函数调用，归 `other` —— 不越界替人改函数）
VARIABLE_REF_RE = re.compile(r"^\$\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}$")

# ★"业务码"的判定：`check` 的**末段**是这些语义名 → 值语义是"结果码"（不是普通字段）
BUSINESS_CODE_SEGMENTS = frozenset(
    {"code", "status", "state", "errno", "error_code", "ret", "result_code", "returncode"}
)
# HTTP 层检查项（另算，但同样"不给改"）
PROTOCOL_CHECK_EXACT = frozenset({"status_code", "reason"})

# 字段路径的分段（`.` 与 `[]` 都算边界——`body.data.token` / `body.items[0].id`）
PATH_SEGMENT_RE = re.compile(r"[.\[\]]+")


def _path_segments(check: Text) -> List[Text]:
    return [seg for seg in PATH_SEGMENT_RE.split(str(check or "").strip()) if seg]


def is_business_code_check(check: Text) -> bool:
    """`check` 是否指向"结果码"类字段（**末段**语义名判定）。

    ★为什么看**末段**而不是整串：`body.data.code` / `data.result.ret` 这类都该被拦住，
    而 `body.code_name`（末段是 `code_name`）不该——只看末段才分得开。
    """
    segments = _path_segments(check)
    if not segments:
        return False
    return segments[-1].lower().replace("-", "_") in BUSINESS_CODE_SEGMENTS


def variable_name(check: Text) -> Text:
    """`check` 是不是**简单变量引用** `${name}` → 返回名字；否则 `""`。

    ★`${fn(a, b)}` 这类**函数调用**刻意不算（归 `other`）：本命令只改"字段名/变量名"，
    不替人改函数调用——那是另一个语义层级，改错了没人看得出来。
    """
    match = VARIABLE_REF_RE.match(str(check or "").strip())
    return match.group(1) if match else ""


def container_root(check: Text) -> Text:
    """`check` 的**容器根**（`body` / `headers`）；不是这两者 → `""`。

    ★为什么要有它（§9.25 修的真窟窿）：改前"字段改名"的候选一律去**响应体**里找、
    并且一律拼成 `body.*` —— 于是 `headers.X-Foo` 取不到值时会被"修"成 `body.XFoo` ✗。
    那不是改名字，是**换语义**。现在候选只在**同一容器**里找，且 `new_check` 保留原根。
    """
    segments = _path_segments(check)
    if not segments:
        return ""
    head = segments[0].lower()
    return head if head in CONTAINER_ROOTS else ""


def classify_failure(validator: Dict[Text, Any]) -> Text:
    """**触发条件（写进代码，不靠 prompt）**：这条失败该给建议、还是该拒绝。

    ★顺序很重要：**先判"不给改"，再判"可建议"**。反过来的话 `body.code`（业务码，
    且 `check_value` 为 None）会先命中"路径失效"从而拿到一条建议——
    而它恰恰是本模块**最不该**碰的那一类。`${code}` 同理（§9.25）：变量名就是结果码语义。
    """
    check = str(validator.get("check", "") or "")
    # ★业务码判定的输入要**归一**：`${code}` 的语义名在**变量名**上（去掉 `${}`），
    #   否则 `${code}` 会绕过业务码闸、落到"变量改名"里 ✗（改名字就能把 bug 写成特性）
    name = variable_name(check)
    business_target = name or check
    if check in PROTOCOL_CHECK_EXACT or is_business_code_check(business_target):
        return REFUSE_REAL_DEFECT
    if name:
        # `${var}`：**取不到值**才算"名字写错"；取到了值却对不上 → 值语义，不给改
        return FIXABLE_VAR_MISSING if validator.get("check_value") is None else REFUSE_OTHER
    if str(check).strip().startswith("${"):
        # `${fn(...)}` 这类**函数调用**：取不到值也**不给改**（不替人改函数调用的参数）
        return REFUSE_OTHER
    if validator.get("check_value") is None:
        return FIXABLE_PATH_MISSING
    comparator = str(validator.get("comparator", "") or "")
    if "type_match" in comparator:
        return FIXABLE_TYPE_MISMATCH
    return REFUSE_OTHER


def actual_type_name(value: Any) -> Text:
    """`check_value` 的 Python 类型 → 内核 `type_match` 认的类型名。"""
    if isinstance(value, bool):  # ★bool 必须排在 int 之前（bool 是 int 的子类）
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


# ---------------------------------------------------------------------------
# YAML 定位（**行号**：用 `yaml.compose` 拿节点 mark，不做 round-trip）
# ---------------------------------------------------------------------------

def _yaml():
    """内核的硬依赖 PyYAML（本包不新增依赖；同 `interfacetester.utils` 的用法）。"""
    import yaml  # noqa: PLC0415

    return yaml


def _mapping_get(node: Any, key: Text) -> Any:
    """从 `MappingNode` 里取某个键对应的**值节点**（找不到 → None）。"""
    for pair in getattr(node, "value", None) or []:
        if isinstance(pair, tuple) and len(pair) == 2 and getattr(pair[0], "value", None) == key:
            return pair[1]
    return None


def _sequence_items(node: Any) -> List[Any]:
    """`SequenceNode` 的元素列表（None → 空表）。

    ⚠️ 只在**明确是序列**的位置调用：`MappingNode.value` 是 `(key, value)` 对，
    与序列的形状不同——调用点必须自己清楚这里该是哪种。
    """
    if node is None:
        return []
    return list(getattr(node, "value", None) or [])


@dataclass(frozen=True)
class AssertionSpot:
    """一条断言在 YAML **文本**里的位置（1-based 行、0-based 列）。

    列区间来自 `ScalarNode` 的 `start_mark` / `end_mark`，所以替换能**只动那个字面量**，
    连同一行里的引号与逗号都不碰。

    `expect_*` 可能缺失（`- eq: ["body.x"]` 这种只有一个元素的写法），用 **-1** 表示"没有"。
    """

    step_index: int
    step_name: Text
    operator: Text  # YAML 里写的算子名（`type_match` / `eq` …）
    check_line: int  # 1-based
    check_col_start: int  # 0-based
    check_col_end: int  # 0-based（同一行才有意义）
    expect_line: int = -1
    expect_col_start: int = -1
    expect_col_end: int = -1

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "step_index": self.step_index,
            "step": self.step_name,
            "operator": self.operator,
            "check_line": self.check_line,
        }


def locate_assertion(
    yaml_text: Text, *, step_name: Text, check: Text, operator: Text = ""
) -> Optional[AssertionSpot]:
    """在用例文本里定位那条断言 → `AssertionSpot`（定位不到 → `None`）。

    ★为什么用 `yaml.compose` 拿 mark，而不是 round-trip（load → dump）：
    round-trip 会**丢注释、重排格式、抹掉引号风格**，产出的 diff 会变成"整文件重写"，
    人根本看不出改了什么。而 §5.8 要的产物是"unified diff（**含 YAML 行号**）"——
    **"行号"这个要求本身就说明是文本级编辑**。
    """
    yaml = _yaml()
    try:
        root = yaml.compose(yaml_text)
    except Exception:  # noqa: BLE001 - 解析不了 → 定位不到（调用方会给"无法定位"的说明）
        return None
    if root is None or not hasattr(root, "value"):
        return None

    steps = _mapping_get(root, "teststeps")
    if steps is None:
        return None

    for index, step_node in enumerate(_sequence_items(steps)):
        if not hasattr(step_node, "value"):
            continue
        name_node = _mapping_get(step_node, "name")
        name = str(getattr(name_node, "value", "") or "")
        validate_node = _mapping_get(step_node, "validate")
        if validate_node is None:
            continue

        for item in _sequence_items(validate_node):
            pairs = getattr(item, "value", None) or []
            if not pairs or not isinstance(pairs[0], tuple):
                continue
            op_node, args_node = pairs[0]
            op = str(getattr(op_node, "value", ""))
            if operator and op != operator:
                continue
            args = _sequence_items(args_node)
            if not args:
                continue
            check_node = args[0]
            if str(getattr(check_node, "value", "")) != str(check):
                continue
            # ★step 名也要对上：同一个 check 路径可能出现在多个 step 里
            if step_name and name != step_name:
                continue
            start, end = check_node.start_mark, check_node.end_mark
            if start.line != end.line:  # 跨行标量 → 不敢按行内列区间替换
                continue
            expect_line, expect_col_start, expect_col_end = -1, -1, -1
            if len(args) > 1:
                expect_node = args[1]
                e_start, e_end = expect_node.start_mark, expect_node.end_mark
                if e_start.line == e_end.line:
                    expect_line = e_start.line + 1
                    expect_col_start = e_start.column
                    expect_col_end = e_end.column
            return AssertionSpot(
                step_index=index,
                step_name=name,
                operator=op,
                check_line=start.line + 1,  # mark 是 0-based
                check_col_start=start.column,
                check_col_end=end.column,
                expect_line=expect_line,
                expect_col_start=expect_col_start,
                expect_col_end=expect_col_end,
            )
    return None


def build_patched_text(
    yaml_text: Text,
    spot: AssertionSpot,
    *,
    new_check: Optional[Text] = None,
    new_expect: Optional[Text] = None,
) -> Optional[Text]:
    """按需替换 `check` / `expect` 字面量（**只动它们**）。定位失效 → `None`。

    ★两处纪律：

    1. **"只动它"不是洁癖**：自愈建议的**最小区分度越高，人越能判断它改了什么**。
       顺手把引号换成另一种、或重排格式，会让人分不清"哪些是它改的、哪些本来就这样"。
       所以这里**不做 round-trip**（那会丢注释），只做文本级列区间替换。
    2. **同一行上两处替换要从右往左**：先改左边的会**挪动右边列号**，
       于是第二处替换会切在错的位置——这类错很隐蔽（diff 看起来"改了但改歪了"）。
    """
    lines = yaml_text.splitlines()

    edits: List[Tuple[int, int, int, Text]] = []  # (行号, 列起, 列止, 新文本)
    if new_check is not None:
        edits.append((spot.check_line, spot.check_col_start, spot.check_col_end, new_check))
    if new_expect is not None and spot.expect_line > 0:
        edits.append((spot.expect_line, spot.expect_col_start, spot.expect_col_end, new_expect))

    for line_no, col_start, col_end, replacement in sorted(edits, key=lambda e: -e[1]):
        index = line_no - 1
        if not (0 <= index < len(lines)):
            return None
        line = lines[index]
        if col_end < col_start or col_end > len(line):
            return None
        # ★**保留引号风格**（§9.25 实测）：`yaml.compose` 的 mark 覆盖**含引号**的那一段，
        #   直接替换会把 `["${token}", "abc"]` 变成 `[${accessToken}, "abc"]` ——
        #   而流式序列里 `{` 是**语法字符** → 补丁根本解析不过（双前置会拦下它，
        #   于是"该给的建议一条也给不出来" ✗）。同一坑也影响 `body.items[0].id` 这种带 `[` 的路径。
        quote = line[col_start : col_start + 1]
        if quote in ('"', "'") and line[col_end - 1 : col_end] == quote:
            replacement = f"{quote}{replacement}{quote}"
        lines[index] = line[:col_start] + replacement + line[col_end:]

    return "\n".join(lines) + ("\n" if yaml_text.endswith("\n") else "")


def unified_diff(before: Text, after: Text, *, path: Text) -> Text:
    """产 unified diff（**带 YAML 行号**，§5.8 的产物形态）。"""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=2,
        )
    )


# ---------------------------------------------------------------------------
# 双前置（红线⑦）：补丁在**输出 diff 之前**必须过
# ---------------------------------------------------------------------------

def validate_patch(yaml_text: Text) -> Tuple[bool, Text]:
    """① `load_testcase_file`（**重复键硬闸门**）② S 系列闸门 → `(是否通过, 说明)`。

    ★两个都要，因为管的是**两件事**：

    - **①** 管"这份 YAML 还能不能被内核读进来"：**重复键**这类问题只在 YAML **文本层**
      可见（`yaml.safe_load` 之后已经被合并掉了），所以必须**写文件、走真 loader**；
    - **②** 管"补丁有没有把**可判定**改成**不可判定**"（红线⑦）。
      典型：`type_match: [body.data.token, "str"]` → `not_equal: [body.data.token, ""]`——
      后者在取不到值时**静默通过**，自愈制造了一个**假通过**，比原来的失败危险得多。

    ⚠️ 临时文件写进 `.ai/work/<job_id>/`（`workdir.JobWorkspace` 是**已登记**的写入者），
    退出即删；**目标用例文件全程没被碰**——这才是"零写盘"的正确口径（见模块 docstring）。
    """
    from interfacetester.loader import load_testcase_file  # noqa: PLC0415

    from interfacetester_ai.gates import check_testcase  # noqa: PLC0415
    from interfacetester_ai.workdir import JobWorkspace  # noqa: PLC0415

    yaml = _yaml()
    try:
        document = yaml.safe_load(yaml_text)
    except Exception as error:  # noqa: BLE001 - 解析失败要变成"前置未过"，不是崩
        return False, f"YAML 解析失败：{error}"
    if not isinstance(document, dict):
        return False, "补丁后不是 `config/teststeps` 映射"

    with JobWorkspace() as job:
        written = job.write("patched.yml", yaml_text)
        try:
            load_testcase_file(written)  # ① 重复键硬闸门（文本层）
        except Exception as error:  # noqa: BLE001
            return False, f"前置①（load_testcase_file）未过：{error}"
        report = check_testcase(document, written)  # ② S 系列闸门

    if not report.ok:
        problems = [f"{finding.code} {finding.message}" for finding in report.rejects]
        return False, "前置②（S 系列闸门）未过：" + "；".join(problems or ["（无明细）"])
    return True, "双前置已过（load_testcase_file + S 系列）"


def _normalize_segment(text: Text) -> Text:
    """末段归一（去分隔符、小写）——`accessToken` / `access-token` / `ACCESS_TOKEN` 归一为同一个。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text).lower())


def find_candidate_paths(
    response_body: Any, missing_segment: Text, *, allow_contains: bool = True
) -> List[Text]:
    """在响应 JSON 里找候选字段 → 路径列表（jmespath 风格，`[]` 表示数组元素）。

    **两级匹配，精确优先**：

    1. **归一化后完全同名**（`body.data.token` → 响应里的 `data.token`）；
    2. **末段包含原末段**（`token` ⊂ `access_token`、`id` ⊂ `userId`）——**仅当第 1 级没有命中时**。

    ★为什么需要第 2 级：**真实的字段改名大多不是换一个毫不相干的名字**，而是加/去前缀
    （`token` ↔ `access_token`）、改连接符（`user_name` → `userName`）、加后缀（`id` → `userId`）。
    **方向是双向的**——加前缀（`token` → `access_token`）与去前缀（`access_token` → `token`）
    一样常见，只认一个方向会让一半场景一声不响。只认"完全同名"更不行：
    那会让 `haify fix` 在**最常见**的场景里不吭声，而那正是 §5.8 点名的典型触发场景。

    ★误命中怎么办：调用方要求候选**唯一**才给建议（多个候选 → **不替人挑**）。
    这一条比"把匹配写得更聪明"更可靠：宁可不给，也不改歪。
    """
    target = _normalize_segment(missing_segment)
    exact: List[Text] = []
    contains: List[Text] = []

    def walk(node: Any, prefix: Text) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                normalized = _normalize_segment(key)
                if normalized and normalized == target:
                    exact.append(path)
                elif allow_contains and target and normalized and (
                    target in normalized or normalized in target
                ):
                    contains.append(path)
                walk(value, path)
        elif isinstance(node, list):
            for item in node:
                walk(item, f"{prefix}[]")

    walk(response_body, "")
    return exact if exact else contains


def find_candidate_names(
    mapping: Any, missing_name: Text, *, allow_contains: bool = True
) -> List[Text]:
    """在**名字字典**（响应头 / `extract`）里找候选名 —— 与 `find_candidate_paths` 同口径。

    归一化后**完全同名优先**；没有才退到"末段包含"（`token` ⊂ `access_token` ✓ 双向 ✓）。
    """
    target = _normalize_segment(missing_name)
    exact: List[Text] = []
    contains: List[Text] = []
    for key in mapping or {}:
        normalized = _normalize_segment(key)
        if not normalized:
            continue
        if normalized == target:
            exact.append(str(key))
        elif allow_contains and target and (target in normalized or normalized in target):
            contains.append(str(key))
    return exact if exact else contains


def read_extract_map(yaml_text: Text, step_name: Text) -> Dict[Text, Text]:
    """读某个 step 的 `extract`（`{变量名: 取值表达式}`）——**只读**，用于变量改名建议。

    ★判不了就返回 `{}`（调用方据此**拒绝**，而不是猜）：YAML 解析失败、步骤名对不上、
    没有 `extract` —— 这三种都属于"**没有可查的事实**"。
    """
    yaml = _yaml()
    try:
        document = yaml.safe_load(yaml_text)
    except Exception:  # noqa: BLE001 - 读不到就当没有（不给建议 ≠ 崩）
        return {}
    steps = (document or {}).get("teststeps") if isinstance(document, dict) else None
    for step in steps or []:
        if not isinstance(step, dict) or str(step.get("name") or "") != str(step_name or ""):
            continue
        extract = step.get("extract")
        if isinstance(extract, dict):
            return {str(key): str(value) for key, value in extract.items()}
    return {}


# ---------------------------------------------------------------------------
# 建议构造
# ---------------------------------------------------------------------------

FIX_STATUS_SUGGESTED = "suggested"
FIX_STATUS_REFUSED = "refused"


@dataclass(frozen=True)
class FixSuggestion:
    """一条自愈**建议**（或一条**拒绝**）。"""

    case: Text
    step: Text
    pointer: Text  # ★§5.8 要求的 `summary.json` 指针
    check: Text
    kind: Text  # classify_failure 的结果
    status: Text = FIX_STATUS_REFUSED
    reason: Text = ""  # 为什么（人话，可直接打给用户）
    risk: Text = ""  # 风险提示
    new_check: Text = ""
    diff: Text = ""  # unified diff（只在 `suggested` 时非空）
    gate_status: Text = ""  # 双前置的结论

    def is_suggested(self) -> bool:
        return self.status == FIX_STATUS_SUGGESTED

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "case": self.case,
            "step": self.step,
            "pointer": self.pointer,
            "check": self.check,
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "risk": self.risk,
            "new_check": self.new_check,
            "has_diff": bool(self.diff),
            "gate_status": self.gate_status,
        }


def _read_case_text(case_dir: Text, case_name: Text) -> Tuple[Text, Text]:
    """找并读用例文件 → `(路径, 文本)`；找不到 → `("", "")`。**只读，不写**。"""
    for suffix in (".yml", ".yaml"):
        candidate = os.path.join(case_dir, case_name + suffix)
        if os.path.exists(candidate):
            with open(candidate, encoding="utf-8", errors="replace") as fp:
                return candidate, fp.read()
    return "", ""


def _parse_body(response_body: Any) -> Any:
    """响应体 → JSON 对象（内核把它存成**字符串**；解不开就当没有）。"""
    if isinstance(response_body, (dict, list)):
        return response_body
    if isinstance(response_body, str):
        try:
            return json.loads(response_body)
        except (ValueError, TypeError):
            return None
    return None


def propose_fix_for(
    *,
    case_name: Text,
    step_name: Text,
    pointer: Text,
    validator: Dict[Text, Any],
    case_dir: Text = "cases",
    response_body: Any = None,
    response_headers: Any = None,
) -> FixSuggestion:
    """给一条失败断言出建议、或明确**拒绝**。**不写盘**。

    ★两种"拒绝"都是**正确的输出**，不是失败：

    - `real_defect_suspect`：状态码/业务码类失败 → §5.8 明写"一律判疑似真实缺陷、不给改"；
    - `other`：不在 §5.8 的两类触发条件内（本模块**不越界**去做通用改写）。

    `response_headers` 是 §9.25 新增的输入：`headers.*` 里取不到值时，候选**只能**在
    响应头里找（拿不到这个事实 → **拒绝**，而不是去响应体里猜）。
    """
    check = str(validator.get("check", "") or "")
    kind = classify_failure(validator)
    base: Dict[Text, Any] = {
        "case": case_name,
        "step": step_name,
        "pointer": pointer,
        "check": check,
        "kind": kind,
    }

    if kind == REFUSE_REAL_DEFECT:
        return FixSuggestion(
            **base,
            reason=f"`{check}` 是**状态码/业务码类**检查项——按 §5.8 一律判「疑似真实缺陷、不给改」",
            risk=(
                "它的值变化**更可能**意味着接口真的改了行为。给这类失败出「改断言」的建议，"
                "等于教人把 bug 当特性——请先跑 `haify analyze` 归因"
            ),
        )
    if kind == REFUSE_OTHER:
        return FixSuggestion(
            **base,
            reason=(
                "不在 §5.8 的触发条件内（只对「`check_value is None` 的路径失效」与"
                "「`type_match` 类型不符」给建议）——本命令**不越界**做通用改写"
            ),
            risk="需要人工判断；要归因请用 `haify analyze`",
        )

    case_path, case_text = _read_case_text(case_dir, case_name)
    if not case_text:
        return FixSuggestion(
            **base,
            reason=f"找不到用例文件（{case_dir}/{case_name}.yml）——定位不到 YAML 行，给不出补丁",
            risk="文件可能不在 `cases/`，或用例名与文件名不一致",
        )

    spot = locate_assertion(case_text, step_name=step_name, check=check)
    if spot is None:
        return FixSuggestion(
            **base,
            reason=f"在 `{case_path}` 里定位不到 `{step_name}` 的 `{check}` 断言——**不敢猜行号**",
            risk="可能是多行标量或变量展开的写法；请人工定位",
        )

    if kind == FIXABLE_PATH_MISSING:
        return _suggest_for_missing_path(
            base, check, spot, case_path, case_text, response_body, response_headers
        )
    if kind == FIXABLE_VAR_MISSING:
        return _suggest_for_variable_missing(
            base,
            check,
            spot,
            case_path,
            case_text,
            response_body,
            read_extract_map(case_text, step_name),
        )

    # `type_mismatch`：建议把 `type_match` 的**类型名**改成实际类型
    expected = actual_type_name(validator.get("check_value"))
    patched = build_patched_text(case_text, spot, new_expect=expected)
    if patched is None:
        return FixSuggestion(**base, reason="定位到了但替换失败（expect 可能跨行）")
    return _finish_suggestion(
        base,
        case_path,
        case_text,
        patched,
        "",
        reason=f"`{check}` 的实际类型是 `{expected}`，与断言里的期望类型不符",
        risk=(
            "类型变化**也可能是接口改了行为**（或文档写错）——先归因，"
            "再决定是改用例还是改接口（§8.1 P1a/P2a 的分工）"
        ),
    )


def _suggest_for_missing_path(
    base: Dict[Text, Any],
    check: Text,
    spot: AssertionSpot,
    case_path: Text,
    case_text: Text,
    response_body: Any,
    response_headers: Any = None,
) -> FixSuggestion:
    """路径失效（字段改名）的建议：在**同一个容器**里找**唯一**的相近字段名（§9.25）。

    ★两条硬闸（都是"扩触发面"的配套）：
      ① **根必须在 `CONTAINER_ROOTS` 里** —— `status_code`/`text`/`elapsed*`/写错的根
         都不是"字段名问题"，一律拒（拒也是正确输出）；
      ② **候选只在同一容器里找** —— 去别的容器里找出来的名字，改的是**语义**不是名字 ✗。
    """
    root = container_root(check)
    if not root:
        return FixSuggestion(
            **base,
            reason=(
                f"`{check}` 取不到值，但它的**根**不在 {list(CONTAINER_ROOTS)} 里——"
                "这不是字段名问题（本命令只在同一容器内改字段名，不越界改别的写法）"
            ),
            risk="要判断这类失败请用 `haify analyze`（它按证据归因，不猜改法）",
        )

    if root == "body":
        source: Any = _parse_body(response_body)
    else:
        source = response_headers if isinstance(response_headers, dict) else None
    if source is None:
        return FixSuggestion(
            **base,
            reason=(
                f"`{check}` 取不到值，但这次运行**没留下 `{root}` 的事实**"
                f"（{'响应体不是 JSON' if root == 'body' else 'summary 里没有响应头'}）→ "
                "判不了是不是改名，**不给建议**"
            ),
            risk="没有事实就不猜：猜出来的名字改上去，比不改更坏",
        )

    segment = (_path_segments(check) or ["", ""])[-1]
    if root == "body":
        # 响应体自带 `body` 那一层 → 剥掉前缀再拼（旧的 `new_check` 形态不变）
        observed = sorted(
            {
                (".".join(item.split(".")[1:]) if item.startswith("body.") else item)
                for item in find_candidate_paths(source, segment)
            }
            - {""}
        )
    else:
        observed = find_candidate_names(source, segment)

    if len(observed) != 1:
        detail = (
            f"`{root}` 里有 {len(observed)} 个末段相近的候选（{'、'.join(observed)}）——**不替人挑**"
            if observed
            else f"`{root}` 里**找不到**末段为 `{segment}` 的字段"
        )
        return FixSuggestion(
            **base,
            reason=f"`{check}` 取不到值（路径失效），但{detail}",
            risk="先看 `haify analyze` 的归因：是字段改名，还是接口真的删了这个字段",
        )

    new_check = f"{root}.{observed[0]}"
    patched = build_patched_text(case_text, spot, new_check=new_check)
    if patched is None:
        return FixSuggestion(**base, reason="定位到了但替换失败（行内容与 mark 不一致）")
    return _finish_suggestion(
        base,
        case_path,
        case_text,
        patched,
        new_check,
        reason=(
            f"`{check}` 取不到值，而 `{root}` 里**唯一**的相近字段是 `{new_check}`"
            "（疑似字段改名 / 层级调整）"
        ),
        risk="补丁**只调路径、不换容器**；若这是接口真的改了结构，改断言会掩盖问题——请对照 `haify analyze` 的归因",
    )


def _suggest_for_variable_missing(
    base: Dict[Text, Any],
    check: Text,
    spot: AssertionSpot,
    case_path: Text,
    case_text: Text,
    response_body: Any,
    extract_map: Dict[Text, Text],
) -> FixSuggestion:
    """`${var}` 取不到值的建议（§9.25 新族）：在**本 step 的 `extract`** 里找唯一相近变量。

    ★为什么它是独立一族（而不是并进"路径失效"）：`${token}` 的取值来自 `extract`，
    **不是**响应里的字段名 —— 混在一起会给出"把变量引用改成 `body.token`"这种
    **换语义**的补丁 ✗✗（改前就是这样）。

    ★两道硬闸（比字段改名那族更硬，因为变量更容易改错语义）：
      ① 候选**唯一**（沿用"不替人挑"）；
      ② 新名字的**取值路径必须在响应里真的能取到值** —— 否则只是换了个同样取不到的名字 ✗。
    """
    old = variable_name(check)
    if not extract_map:
        return FixSuggestion(
            **base,
            reason=(
                f"`{check}` 取不到值，但本 step **读不到 `extract`**（没配 / 步骤名对不上 / "
                "用例文件找不到）→ 判不了是不是变量名写错，**不给建议**"
            ),
            risk="变量取不到值也可能是上游步骤没 `extract` 出来——那要改上游，不是改这一行",
        )

    candidates = find_candidate_names(extract_map, old)
    if len(candidates) != 1:
        detail = (
            f"`extract` 里有 {len(candidates)} 个相近变量（{'、'.join(candidates)}）——**不替人挑**"
            if candidates
            else f"`extract` 里没有与 `{old}` 相近的变量名"
        )
        return FixSuggestion(
            **base,
            reason=f"`{check}` 取不到值，但{detail}",
            risk="若上游真的改了字段名，该改的是 `extract` 的取值路径（或接口）——请先 `haify analyze`",
        )

    new_name = candidates[0]
    value_path = str(extract_map.get(new_name, ""))
    body = _parse_body(response_body)
    last = (_path_segments(value_path) or [""])[-1]
    resolvable = bool(body is not None and last and find_candidate_paths(body, last, allow_contains=False))
    if not resolvable:
        return FixSuggestion(
            **base,
            reason=(
                f"`extract` 里唯一的相近变量是 `{new_name}`（取值 `{value_path}`），"
                "但**它的取值路径在响应里也取不到值** → 改了也一样取不到，**不给建议**"
            ),
            risk="这说明问题不在变量名（可能上游没抓到值 / 接口真的改了结构）——请先 `haify analyze`",
        )

    new_check = "${" + new_name + "}"
    patched = build_patched_text(case_text, spot, new_check=new_check)
    if patched is None:
        return FixSuggestion(**base, reason="定位到了但替换失败（行内容与 mark 不一致）")
    return _finish_suggestion(
        base,
        case_path,
        case_text,
        patched,
        new_check,
        reason=(
            f"`{check}` 取不到值，而 `extract` 里唯一的相近变量是 `{new_check}`"
            f"（取值 `{value_path}`，该路径在响应里**能取到值**）——疑似变量名写错"
        ),
        risk="补丁**只改变量名、不动取值路径**；若上游真的删了字段，改名字会掩盖问题——请对照 `haify analyze` 的归因",
    )


def _finish_suggestion(
    base: Dict[Text, Any],
    case_path: Text,
    case_text: Text,
    patched: Text,
    new_check: Text,
    *,
    reason: Text,
    risk: Text,
) -> FixSuggestion:
    """过双前置之后才给 diff（§5.8："补丁在**输出 diff 之前**必须先过"）。

    ★顺序本身就是判据的一部分：先给 diff、再校验，等于把**没过闸门的补丁**交到人手上——
    而人一旦粘进用例，闸门就再也拦不住了。§5.8 写这段的原因正是如此。
    """
    passed, gate_status = validate_patch(patched)
    if not passed:
        return FixSuggestion(
            **base, reason=f"补丁未过双前置：{gate_status}", risk=risk, gate_status=gate_status
        )
    return FixSuggestion(
        **base,
        status=FIX_STATUS_SUGGESTED,
        reason=reason,
        risk=risk,
        new_check=new_check,
        diff=unified_diff(case_text, patched, path=case_path.replace(os.sep, "/")),
        gate_status=gate_status,
    )


# ---------------------------------------------------------------------------
# 批量入口
# ---------------------------------------------------------------------------

@dataclass
class FixResult:
    """一次自愈建议的结果。"""

    suggestions: List[FixSuggestion] = field(default_factory=list)

    def suggested(self) -> List[FixSuggestion]:
        return [item for item in self.suggestions if item.is_suggested()]

    def refused(self) -> List[FixSuggestion]:
        return [item for item in self.suggestions if not item.is_suggested()]

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "total": len(self.suggestions),
            "suggested": len(self.suggested()),
            "refused": len(self.refused()),
            "items": [item.to_dict() for item in self.suggestions],
        }


def propose_fixes(summary: Any, *, case_dir: Text = "cases") -> FixResult:
    """`summary.json` → 逐条失败断言的建议。**零写盘**（目标用例文件的 mtime 不变）。

    ★**退出码恒 0**（§5.8）：本命令的产物是**建议**，"给不出建议"不是错误——
    尤其"疑似真实缺陷、不给改"是**正确的输出**，不是失败。把"正常拒绝"报成非零退出码，
    会让人把它当故障处理（重试、绕过），而它本该是被认真读的一条结论。
    """
    result = FixResult()
    details = summary.get("details") if isinstance(summary, dict) else None
    if not isinstance(details, list):
        return result

    for detail_index, detail in enumerate(details):
        if not isinstance(detail, dict):
            continue
        case_name = str(detail.get("name") or "")
        records = detail.get("records")
        if not isinstance(records, list):
            continue

        for record_index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            step_name = str(record.get("name") or "")
            data = record.get("data")
            if not isinstance(data, dict):
                continue

            response_body = None
            response_headers = None
            req_resps = data.get("req_resps")
            if isinstance(req_resps, list) and req_resps and isinstance(req_resps[0], dict):
                response = req_resps[0].get("response")
                if isinstance(response, dict):
                    response_body = response.get("body")
                    # ★§9.25：`headers.*` 的候选**只能**从响应头里找（拿不到 → 拒，不去 body 里猜）
                    response_headers = response.get("headers")

            validators = data.get("validators")
            if not isinstance(validators, dict):
                continue

            for step_type, items in validators.items():
                if not isinstance(items, list):
                    continue
                for index, validator in enumerate(items):
                    if not isinstance(validator, dict):
                        continue
                    if validator.get("check_result") != "fail":
                        continue
                    # ★§5.8 要求的指针形态（点开 `summary.json` 就能找到这条断言）
                    pointer = (
                        f"details[{detail_index}].records[{record_index}]"
                        f".data.validators.{step_type}[{index}]"
                    )
                    result.suggestions.append(
                        propose_fix_for(
                            case_name=case_name,
                            step_name=step_name,
                            pointer=pointer,
                            validator=validator,
                            case_dir=case_dir,
                            response_body=response_body,
                            response_headers=response_headers,
                        )
                    )
    return result


def render_fix_report(result: FixResult) -> Text:
    """渲染给人看的建议清单。**只返回文本**——本命令不落盘（§5.8 的产物是"建议"）。"""
    lines = [
        "# 用例自愈建议（由 interfacetester_ai.fix 生成）",
        "",
        "> **本命令只建议、不落地**：没有 `--apply`，也不会写任何用例文件。",
        "> 补丁在**输出之前**已过双前置（`load_testcase_file` + S 系列闸门，红线⑦）——",
        "> 过不了的补丁**不会**出现在这里；请自行判断后手工粘贴。",
        "",
        f"- 共 **{len(result.suggestions)}** 条失败断言："
        f"**{len(result.suggested())}** 条给出补丁 / **{len(result.refused())}** 条不给改",
        "",
    ]
    for item in result.suggestions:
        flag = "✅ 建议" if item.is_suggested() else "⛔ 不给改"
        lines += [
            f"## {flag} · {item.case} / {item.step}",
            "",
            f"- 断言：`{item.check}`",
            f"- 指针：`{item.pointer}`",
            # ★§9.25：把**触发族**显示出来——"它按哪条规矩判的"是这条结论可信度的一部分
            f"- 触发族：`{item.kind}`",
            f"- 理由：{item.reason}",
        ]
        if item.is_suggested():
            lines += ["", "```diff", item.diff.rstrip(), "```"]
        if item.risk:
            lines += ["", f"> ⚠️ 风险：{item.risk}"]
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 自检（`fix.py` 自己**不写盘**；`validate_patch` 借 `workdir` 的登记根，退出即删）
# ---------------------------------------------------------------------------

_SELFTEST_CASE = """config:
  name: demo
teststeps:
  - name: 查询
    request:
      method: GET
      url: /api/user
    validate:
      - eq: ["body.data.token", "abc"]
      - eq: ["body.data.age", 18]
"""


def _validator(check: Text, check_value: Any = None, comparator: Text = "equal", **kw: Any) -> Dict[Text, Any]:
    payload: Dict[Text, Any] = {
        "comparator": comparator,
        "check": check,
        "check_value": check_value,
        "expect_value": None,
        "check_result": "fail",
    }
    payload.update(kw)
    return payload


def run_selftest(verbose: bool = False) -> int:
    """fix 自检。返回 0=全绿；非 0=失败项数。

    ⚠️ 会**短暂**在 `.ai/work/<job_id>/` 里写一个临时补丁（双前置要求真 loader 验重复键），
    退出即删——`fix.py` **自己**没有任何写盘点（T18 注册表里刻意没有它）。
    """
    failures: List[Text] = []

    # ① 触发条件（**写进代码，不靠 prompt**）：四类各一条，顺序也要对
    if classify_failure(_validator("status_code", 500)) != REFUSE_REAL_DEFECT:
        failures.append("[分类] HTTP 状态码类必须判「疑似真实缺陷、不给改」")
    if classify_failure(_validator("body.code", None)) != REFUSE_REAL_DEFECT:
        failures.append("[分类] 业务码类必须判「不给改」——**即使它 check_value 为 None**（顺序判据）")
    if classify_failure(_validator("body.data.token", None)) != FIXABLE_PATH_MISSING:
        failures.append("[分类] 路径取不到值应当可建议")
    if classify_failure(_validator("body.data.age", 18, "type_match")) != FIXABLE_TYPE_MISMATCH:
        failures.append("[分类] type_match 类型不符应当可建议")
    if classify_failure(_validator("body.name", "x")) != REFUSE_OTHER:
        failures.append("[分类] 触发条件外的失败不该给建议（不越界）")

    # ①′ ★§9.25 扩面的分类判据（容器根 / 变量族 / 业务码不能靠改变量名绕过）
    if container_root("headers.X-Api-Key") != "headers" or container_root("status_code") != "":
        failures.append("[容器] 容器根判定不对（`headers.*` 要认；`status_code` 不是容器）")
    if variable_name("${accessToken}") != "accessToken" or variable_name("${fn(1)}"):
        failures.append("[变量] 简单变量判定不对（`${fn(...)}` 不算变量名）")
    if classify_failure(_validator("${accessToken}", None)) != FIXABLE_VAR_MISSING:
        failures.append("[分类] `${var}` 取不到值应当归「变量改名」族")
    if classify_failure(_validator("${code}", None)) != REFUSE_REAL_DEFECT:
        failures.append("[分类] `${code}` 是**业务码语义**，必须按业务码拒（不能靠改变量名绕过）")
    if classify_failure(_validator("${accessToken}", "abc")) != REFUSE_OTHER:
        failures.append("[分类] 变量**取到了值**却对不上 → 值语义，不给改")

    # ② 业务码的**末段**判定：`body.code` 算、`body.code_name` 不算
    if not is_business_code_check("body.data.code") or is_business_code_check("body.code_name"):
        failures.append("[业务码] 末段判定不对（只看末段才分得开）")

    # ③ YAML **行号**定位（不做 round-trip，所以注释/格式都不动）
    spot = locate_assertion(_SELFTEST_CASE, step_name="查询", check="body.data.token")
    if spot is None or spot.check_line != 9:
        failures.append(f"[定位] `body.data.token` 应当在第 9 行：{spot.to_dict() if spot else None}")

    # ④ **只改那一处**：其余行**逐字不变**（自愈建议的最小区分度）
    if spot is not None:
        patched = build_patched_text(_SELFTEST_CASE, spot, new_check="body.data.access_token")
        original_lines = _SELFTEST_CASE.splitlines()
        patched_lines = (patched or "").splitlines()
        if len(original_lines) != len(patched_lines):
            failures.append("[最小编辑] 行数变了")
        else:
            changed = [i for i, (a, b) in enumerate(zip(original_lines, patched_lines)) if a != b]
            if changed != [spot.check_line - 1]:
                failures.append(f"[最小编辑] 应当只改第 {spot.check_line} 行，实际改了 {[i + 1 for i in changed]}")
            if "access_token" not in patched_lines[spot.check_line - 1]:
                failures.append("[最小编辑] 新路径没写进去")

        # ⑤ diff 带**行号**（§5.8 的产物形态）：要有 `---/+++` 头、有 hunk、且改的是那一行
        diff = unified_diff(_SELFTEST_CASE, patched or "", path="cases/demo.yml")
        for token in ("--- a/cases/demo.yml", "+++ b/cases/demo.yml", "@@", "-", "+", "access_token"):
            if token not in diff:
                failures.append(f"[diff] 缺 {token!r}：{diff!r}")
        # ★最小编辑的机器形态：diff 里**只有一行**被删、**只有一行**被加
        if sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")) != 1:
            failures.append(f"[diff] 应当只删一行：{diff!r}")
        if sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")) != 1:
            failures.append(f"[diff] 应当只加一行：{diff!r}")

    # ⑥ **双前置真跑**：合规补丁必须过
    if spot is not None:
        passed, why = validate_patch(
            build_patched_text(_SELFTEST_CASE, spot, new_check="body.data.access_token") or ""
        )
        if not passed:
            failures.append(f"[双前置] 合规补丁被拒：{why}")

    # ⑦ ★**业务码 100% 拒绝给改**：连 diff 都不该有
    summary = {
        "details": [
            {
                "name": "demo",
                "success": False,
                "records": [
                    {
                        "name": "查询",
                        "data": {
                            "validators": {
                                "validate": [
                                    _validator("status_code", 500),
                                    _validator("body.code", 1001),
                                ]
                            },
                            "req_resps": [{"response": {"body": '{"code": 1001}'}}],
                        },
                    }
                ],
            }
        ]
    }
    result = propose_fixes(summary)
    if len(result.refused()) != 2 or result.suggested():
        failures.append(f"[业务码] 两条都应当被拒：{result.to_dict()}")
    if any(item.diff for item in result.suggestions):
        failures.append("[业务码] 被拒的条目不该带 diff")
    if "不给改" not in render_fix_report(result):
        failures.append("[报告] 拒绝理由没有出现在报告里")

    # ⑧ 指针形态（§5.8 原文的路径）——点开 summary.json 就能找到那条断言
    expected_pointer = "details[0].records[0].data.validators.validate[0]"
    if result.suggestions and result.suggestions[0].pointer != expected_pointer:
        failures.append(f"[指针] 形态不对：{result.suggestions[0].pointer}")

    # ⑨ ★**字段改名命中**（§5.8 验收"字段改名注入样本建议命中 ≥8/10"的可测形态）
    #    - `token` → `access_token`：包含匹配（真实的改名大多长这样）
    #    - 同时有完全同名 → 必须**优先**取完全同名（别把精确的让给推断的）
    renamed = find_candidate_paths({"data": {"access_token": "xyz"}}, "token")
    if renamed != ["data.access_token"]:
        failures.append(f"[改名] `token` 应当命中 `access_token`：{renamed}")
    both = find_candidate_paths({"a": {"token": 1}, "b": {"access_token": 2}}, "token")
    if both != ["a.token"]:
        failures.append(f"[改名] 有完全同名时应当优先取它：{both}")
    if find_candidate_paths({"a": {"accessToken": 1}}, "access_token") != ["a.accessToken"]:
        failures.append("[改名] 归一化后同名（连接符差异）应当命中")

    print("=" * 66)
    if failures:
        print(f"fix 自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("用例自愈建议自检全部通过（§5.8，**只建议不落地**）：")
    print("  触发条件：写进代码——只对「路径取不到值」与「type_match 类型不符」给建议")
    print("  不给改  ：`status_code` / 业务码类**一律判疑似真实缺陷**（连 diff 都不给）")
    print("  最小编辑：`yaml.compose` 取行号列区间 → **只动那个字面量**（注释/格式/引号都不碰）")
    print("  双前置  ：补丁在**输出 diff 之前**先过 `load_testcase_file` + S 系列（红线⑦）")
    print("  零写盘  ：目标用例文件的 mtime 不变；临时补丁只落在 `.ai/work/`（workdir 的登记根）")
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

    parser = argparse.ArgumentParser(description="用例自愈建议（P2a）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "BUSINESS_CODE_SEGMENTS",
    "FIXABLE_PATH_MISSING",
    "FIXABLE_TYPE_MISMATCH",
    "FIX_STATUS_REFUSED",
    "FIX_STATUS_SUGGESTED",
    "PROTOCOL_CHECK_EXACT",
    "REFUSE_OTHER",
    "REFUSE_REAL_DEFECT",
    "AssertionSpot",
    "FixResult",
    "FixSuggestion",
    "actual_type_name",
    "build_patched_text",
    "classify_failure",
    "find_candidate_paths",
    "is_business_code_check",
    "locate_assertion",
    "propose_fix_for",
    "propose_fixes",
    "render_fix_report",
    "run_selftest",
    "unified_diff",
    "validate_patch",
]


if __name__ == "__main__":
    raise SystemExit(main())

