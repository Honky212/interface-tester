"""
Built-in validate comparators.
"""

import builtins
import json
import os
import re
from typing import Any, Dict, Optional, Text, Tuple, Union

from interfacetester import exceptions

# JSON Schema 的 `$` 前缀关键字（`$schema`/`$ref`/`$id`/`$defs`/`$comment`/...）
JSON_SCHEMA_KEYWORD_PREFIX = "$"

# 校验失败时的展示上限：只报前 N 条，避免超大报文把报告刷爆
JSONSCHEMA_MAX_ERRORS = 5
_JSONSCHEMA_VALUE_LIMIT = 200


def equal(check_value: Any, expect_value: Any, message: Text = ""):
    assert check_value == expect_value, message


def _warn_if_both_operands_are_strings(
    operator: Text, check_value: Any, expect_value: Any
) -> None:
    """数值比较算子碰到「两侧都是字符串」时告警（批次 C / L11）。

    NOTICE（为什么只告警、不做硬类型校验）：CPython 对两个 `str` 的 `>`/`<` **不报错**，
    而是按**字典序**比较 —— 于是 `greater_than: ["body.price", "100"]` 在接口把数字
    返回成字符串（Java 后端常见）时会得到 `"9" > "100"` = **True**：用例**假通过**。
    而反方向（一侧是 int）会抛 `TypeError`，被 `response.validate` 转成**带 hint 的可读失败**
    —— 也就是说「提示只覆盖了失败方向，通过方向静默」。

    这里刻意**不做**「两侧都必须是数字」的硬校验：字典序对 ISO 日期串
    （`"2026-09-20" > "2026-01-01"`）恰好等于时间序，是**合法用法**，硬拦会制造假报
    （本仓的取向是「只拦能精确判定的形态」，假报会让真报被无视）。
    所以：行为一个字不改（仍然按 Python 语义比较），只把「这是字典序比较」说清楚，
    并指出改法（把期望值写成数字，或在 check 侧用 `${int(...)}` / 取数值字段）。
    """
    if isinstance(check_value, str) and isinstance(expect_value, str):
        # NOTICE（批次 C 实测踩到）：本模块**没有**模块级 `logger` —— 它一直是按需导入的
        # （见 `_warn_unenforceable_formats` 里的 `from loguru import logger  # noqa: PLC0415`）。
        # 上面那段告警第一次跑就 `NameError: name 'logger' is not defined`，
        # 而**只在「两侧都是字符串」这条分支上**炸 —— 正是护栏用例抓到的。
        from loguru import logger  # noqa: PLC0415 - 与本模块既有口径一致（按需导入）

        logger.warning(
            f"`{operator}` 的两侧**都是字符串**，按 Python 语义这是**字典序**比较"
            f"（不是数值比较）：\n"
            f"  check_value: {check_value!r}\n"
            f"  expect_value: {expect_value!r}\n"
            f"  实测反例：`greater_than: [\"9\", \"100\"]` 判**通过**（字典序里 '9' > '1'），"
            f"而数值上 9 < 100 —— 接口把数字返回成字符串时，这类断言可能**静默通过**。\n"
            f"  若本来就想比数值：把期望值写成数字（`100` 而不是 `\"100\"`），"
            f"并确认 check 侧取到的也是数字（可用 `${{int($var)}}` 或在接口侧修正类型）。\n"
            f"  若确实在比**日期串/版本串**（字典序恰好等于你要的序），忽略本条即可。"
        )


def greater_than(
    check_value: Union[int, float], expect_value: Union[int, float], message: Text = ""
):
    _warn_if_both_operands_are_strings("greater_than", check_value, expect_value)
    assert check_value > expect_value, message


def less_than(
    check_value: Union[int, float], expect_value: Union[int, float], message: Text = ""
):
    _warn_if_both_operands_are_strings("less_than", check_value, expect_value)
    assert check_value < expect_value, message


def greater_or_equals(
    check_value: Union[int, float], expect_value: Union[int, float], message: Text = ""
):
    _warn_if_both_operands_are_strings("greater_or_equals", check_value, expect_value)
    assert check_value >= expect_value, message


def less_or_equals(
    check_value: Union[int, float], expect_value: Union[int, float], message: Text = ""
):
    _warn_if_both_operands_are_strings("less_or_equals", check_value, expect_value)
    assert check_value <= expect_value, message


def not_equal(check_value: Any, expect_value: Any, message: Text = ""):
    assert check_value != expect_value, message


# 0918-8 / H8：字符串形态算子的**取值守卫**（`string_equals` / `startswith` / `endswith`）。
#
# 修复前这三个算子直接 `str(check_value)`，于是两类「根本不是字符串」的取值会被硬转后参与比较：
#   1) **None**（字段不存在 / 下标越界；jmespath 对「缺字段」与「值为 null」都返回 None）
#      → `str(None)` = `"None"`，因此 `startswith: ["body.data.access_token", "N"]`、
#      `string_equals: [..., "None"]` 会**通过**——最常用的「字段存在且非空」断言被静默满足；
#   2) **bytes**（非 JSON 响应体，如 XML/SOAP）→ `str(b"<code>0000</code>")` 等于
#      `"b'<code>0000</code>'"`，比较的是这个 repr：`startswith: ["body", "b'"]` 同样会通过
#      （这一条已在 docs/能力清单.md 登记，与 1) 是同一处根因，本次一并收口）。
#
# NOTICE（边界为什么只到这里）：刻意**不**改成 `isinstance(check_value, str)`——
# 实测 `string_equals: ["status_code", "200"]`（数字被 str() 后比较）是仓库内与现场都在用的写法，
# 一刀切会把它变成失败。这里只拦「无论如何都不可能是字符串比较」的 None / bytes。
_STRING_SHAPE_HINT = (
    "hint: 这类算子只对**字符串**取值有意义。取值为 None 通常意味着**字段不存在或下标越界**"
    "（jmespath 对「缺字段」与「值为 null」都返回 None，框架无法区分）；"
    "若响应体不是 JSON（XML/HTML 时 `body` 是 bytes），请断在 `text` 上"
    '（如 `startswith: ["text", "<?xml"]`），或改用 xpath_match / xml_schema_match。\n'
    "  使用本守卫的算子：string_equals / startswith / endswith / regex_match。"
)


def _ensure_string_shaped(operator: Text, check_value: Any) -> None:
    """字符串形态算子的取值守卫：None / bytes 直接判**断言失败**并给出可操作提示。"""
    if check_value is None:
        raise AssertionError(
            f"{operator}: 取值是 None，不能做字符串形态断言。\n{_STRING_SHAPE_HINT}"
        )
    if isinstance(check_value, (bytes, bytearray)):
        raise AssertionError(
            f"{operator}: 取值是 bytes（响应体不是 JSON），"
            f"比较的会是它的 repr 而不是内容。\n{_STRING_SHAPE_HINT}"
        )


def string_equals(check_value: Text, expect_value: Any, message: Text = ""):
    _ensure_string_shaped("string_equals", check_value)
    assert str(check_value) == str(expect_value), message


def _ensure_length_expect(operator: Text, expect_value: Any) -> Union[int, float]:
    """`length_*` 的 expect_value 守卫：把「变量 / ENV 来的数字字符串」转成数字。

    NOTICE（0921-3 / **轻微项 1**）：这是 L2 / `sleep` 那一族的**同族遗漏**。
    `.env` / CSV / `${ENV(...)}` 出来的值**永远是字符串**，修复前

        length_equal: [body, ${ENV(LEN)}]     # LEN="4"
            -> AssertionError: expect_value should be int type

    只是**英文硬 assert**，既没说是"length 的期望值"，更没点出"值是字符串"这个根因
    —— 而 `sleep` / `gen_random_string` / `get_timestamp` 早就改成了带根因的可读报错。
    本仓的口径是"变量来的数字要转"（`utils.ensure_int_value` / `ensure_timeout_value`）。

    为什么**不**直接用 `ensure_int_value`：`length_greater_than` 等四个算子的
    expect_value 本来就接受 **float**（`assert isinstance(expect_value, (int, float))`），
    只有 `length_equal` 要求 int。照 L2 字面口径套 `ensure_int_value` 会把
    `length_greater_than: [body, 4.5]` 这种既有合法写法判死 —— 与 `sleep` 那次的
    坑是同一个（当时也不能用 `ensure_int_value`）。

    所以按算子的**真实契约**分档：
    - `length_equal`：必须 int（`"4"` → 4；`4.5` / `"4.5"` 报错，与既有判据一致）；
    - 其余四个：int 或 float 皆可（`"4"` → 4，`"4.5"` → 4.5）。
    `bool` 一律拒绝（`true` 是笔误，不是 1），与 `ensure_int_value` 同口径。
    """
    if isinstance(expect_value, bool):
        raise exceptions.ParamsError(
            f"{operator} 的 expect_value 不能是 bool：{expect_value!r}"
            f"（`true` 通常是笔误，不是 1）"
        )

    if isinstance(expect_value, (int, float)):
        numeric: Union[int, float] = expect_value
    elif isinstance(expect_value, str):
        text = expect_value.strip()
        try:
            numeric = int(text)
        except ValueError:
            if operator == "length_equal":
                raise exceptions.ParamsError(
                    f"length_equal 的 expect_value 应为整数：{expect_value!r}"
                    f"（来自变量 / ENV 的值是字符串，请确认真的是纯整数）"
                )
            try:
                numeric = float(text)
            except ValueError:
                raise exceptions.ParamsError(
                    f"{operator} 的 expect_value 应为数字：{expect_value!r}"
                    f"（来自变量 / ENV 的值是字符串，请确认真的是数字）"
                )
    else:
        raise exceptions.ParamsError(
            f"{operator} 的 expect_value 应为数字，收到 {type(expect_value).__name__}："
            f"{expect_value!r}"
        )

    if operator == "length_equal" and not isinstance(numeric, int):
        raise exceptions.ParamsError(
            f"length_equal 的 expect_value 应为整数：{expect_value!r}"
            f"（长度相等是精确判断，非整数会被截断，已拒绝）"
        )
    return numeric


def length_equal(check_value: Text, expect_value: int, message: Text = ""):
    expect_value = _ensure_length_expect("length_equal", expect_value)
    assert len(check_value) == expect_value, message


def length_greater_than(
    check_value: Text, expect_value: Union[int, float], message: Text = ""
):
    expect_value = _ensure_length_expect("length_greater_than", expect_value)
    assert len(check_value) > expect_value, message


def length_greater_or_equals(
    check_value: Text, expect_value: Union[int, float], message: Text = ""
):
    expect_value = _ensure_length_expect("length_greater_or_equals", expect_value)
    assert len(check_value) >= expect_value, message


def length_less_than(
    check_value: Text, expect_value: Union[int, float], message: Text = ""
):
    expect_value = _ensure_length_expect("length_less_than", expect_value)
    assert len(check_value) < expect_value, message


def length_less_or_equals(
    check_value: Text, expect_value: Union[int, float], message: Text = ""
):
    expect_value = _ensure_length_expect("length_less_or_equals", expect_value)
    assert len(check_value) <= expect_value, message


def contains(check_value: Any, expect_value: Any, message: Text = ""):
    assert isinstance(
        check_value, (list, tuple, dict, str, bytes)
    ), "check_value should be list/tuple/dict/str/bytes type"
    assert expect_value in check_value, message


def contained_by(check_value: Any, expect_value: Any, message: Text = ""):
    assert isinstance(
        expect_value, (list, tuple, dict, str, bytes)
    ), "expect_value should be list/tuple/dict/str/bytes type"
    assert check_value in expect_value, message


def type_match(check_value: Any, expect_value: Any, message: Text = ""):
    def get_type(name):
        if isinstance(name, type):
            return name
        elif isinstance(name, str):
            # 批次 9-0（M9 同根因）：修复前是 `__builtins__[name]` —— 把**任意**内置名
            # 当成「类型」返回，于是 `type_match: [body.x, eval]` 会拿到 `eval` 函数，
            # 再去做 `type(check_value) == eval`（恒为假）→ 表面上是「类型断言失败」，
            # 实际是用法错误（也意味着算子能触达整个内置命名空间）。
            # 现在只接受**真的是类型对象**的内置名；名字不是类型就明确报错。
            #
            # NOTICE（0921-3 / **轻微项 2**）：这里抛的从 `ValueError` 改成 `ParamsError`。
            # 原因：`ValueError` 不在 `response.validate` 的捕获面内（它只捕
            # `AssertionError` / `TypeError`），于是 `type_match: [body, "eval"]`
            # 会冒泡成 pytest 的 **error** —— 而"用户写错用法"按本仓既有划分应当记
            # **failed**（信息本身是对的，是**分类**错位）。
            # `ParamsError` 是本仓「用户误用」的显式标记，`validate` 现在会把它转成
            # 可读断言失败，且**不会**掩盖算子内部的真 `ValueError`（那些仍原样抛出）。
            # 与 `timedelta`（不是内置名）走同一条路径。
            candidate = getattr(builtins, name, None)
            if isinstance(candidate, type):
                return candidate
            raise exceptions.ParamsError(
                f"{name} 不是类型名：type_match 的 expect_value 只能是类型对象"
                f'（如 int）或 Python 内置类型名（如 "int" / "str" / "list" / "dict"），'
                f'以及 None / "None" / "NoneType"。'
            )
        else:
            raise exceptions.ParamsError(
                f"type_match 的 expect_value 应为类型对象或类型名字符串，"
                f"收到 {type(name).__name__}：{name!r}"
            )

    if expect_value in ["None", "NoneType", None]:
        assert check_value is None, message
    else:
        assert type(check_value) == get_type(expect_value), message


def regex_match(check_value: Text, expect_value: Any, message: Text = ""):
    """正则匹配。

    NOTICE（0921-3 / **轻微项 3**）：`check_value` 改走同族的
    `_ensure_string_shaped()`。修复前这里是裸 `assert isinstance(check_value, str)`，
    于是在非 JSON 响应（XML/HTML，`body` 是 bytes）上得到的是英文
    `check_value should be Text type` —— 而 `string_equals` / `startswith` /
    `endswith` 早就换成了那套中文提示（"断在 text 上"、或改用 xpath_match）。
    同族算子、两套报错质量，属于口径不齐。

    NOTICE（无效正则）：`re.match` 对写错的正则会抛 `re.error`
    （如 `regex_match: [body, "["]`）。它原先会冒泡成 pytest 的 **error**，
    而"用户把正则写错了"属于用法问题 → 现在转成 `ParamsError`，
    由 `response.validate` 记为可读的 **failed**（与 `type_match` 同口径）。
    """
    if not isinstance(expect_value, str):
        raise exceptions.ParamsError(
            f"regex_match 的 expect_value 应为正则字符串，"
            f"收到 {type(expect_value).__name__}：{expect_value!r}"
        )
    _ensure_string_shaped("regex_match", check_value)
    try:
        compiled = re.compile(expect_value)
    except re.error as ex:
        raise exceptions.ParamsError(
            f"regex_match 的 expect_value 不是合法正则：{expect_value!r}\n"
            f"  正则引擎报错: {ex}"
        )
    assert compiled.match(check_value), message


def startswith(check_value: Any, expect_value: Any, message: Text = ""):
    _ensure_string_shaped("startswith", check_value)
    assert str(check_value).startswith(str(expect_value)), message


def endswith(check_value: Text, expect_value: Any, message: Text = ""):
    _ensure_string_shaped("endswith", check_value)
    assert str(check_value).endswith(str(expect_value)), message


# --------------------------------------------------------------------------- JSON Schema
# 第 19 个内置算子（P2-b）：结构/契约级校验。
#
# 为什么 expect_value 只支持「函数返回值 / 文件路径」两种引用方式：
#   `parser.parse_data` 会连 dict 的 **key** 一起解析（`parser.py:423-428`），而 JSON Schema
#   必带的 `$schema`/`$ref`/`$id`/`$defs` 都是 dict key → 内联 schema 会在**运行期**抛
#   `VariableNotFound: schema not found in {}`（实测）。所以：
#     1) 首选：schema 放 debugtalk.py，YAML 写 `${get_user_schema()}`（返回值不再被递归解析）；
#     2) 次选：写 schema 文件路径（.json/.yaml/.yml），相对项目根目录（含 debugtalk.py 的那层）解析；
#     3) 内联 schema 只在不含 `$` 键时才安全 —— 带 `$` 键的会在 **hmake 阶段**被直接拦住
#        （见 `make.ensure_json_schema_not_inline`），不会拖到运行期。


def _jsonschema_module():
    """懒导入 jsonschema：缺依赖时给**明确的安装提示**（而不是当成断言失败）。"""
    try:
        import jsonschema  # noqa: PLC0415
    except ImportError as ex:  # pragma: no cover - 依赖已进主依赖，这里只是兜底
        raise RuntimeError(
            f"jsonschema 未安装，无法执行 jsonschema_match：{ex}\n"
            '安装：pip install jsonschema（离线环境：pip install -e ".[jsonschema]"）'
        ) from ex
    return jsonschema


def _shorten(value: Any, limit: int = _JSONSCHEMA_VALUE_LIMIT) -> Text:
    """把值渲染成一行、限长，用于失败信息（大报文只截断展示，不整个打出来）。"""
    try:
        rendered = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(value)
    if len(rendered) > limit:
        rendered = rendered[:limit] + f"...(共 {len(rendered)} 字符)"
    return rendered


def _render_expectation(value: Any) -> Text:
    """渲染「期望什么」：字符串裸着写（`type=integer`），其余用 JSON 表示。"""
    if isinstance(value, str):
        return value
    return _shorten(value)


def _render_actual(value: Any) -> Text:
    """渲染「实际是什么」：值 + Python 类型名。

    NOTICE: 类型名必须带上 —— `"0"` 与 `0` 在 JSON 里长得像，但一个是 str 一个是 int，
    而这正是 `type` 类校验失败的原因；只给值会让测试人员看不出问题在哪。
    """
    return f"{_shorten(value)}({type(value).__name__})"


def _json_schema_has_keyword_keys(schema: Any) -> bool:
    """schema 里是否存在会被 `parse_data` 当变量解析的 `$` 前缀键（内联时会炸）。

    递归检查 dict/list：`$schema`、`$ref`、`$defs` 可能出现在任何深度。
    """
    if isinstance(schema, dict):
        for key, value in schema.items():
            if isinstance(key, str) and key.startswith(JSON_SCHEMA_KEYWORD_PREFIX):
                return True
            if _json_schema_has_keyword_keys(value):
                return True
        return False
    if isinstance(schema, list):
        return any(_json_schema_has_keyword_keys(item) for item in schema)
    return False


def _project_root_dir() -> Text:
    """项目根目录（含 debugtalk.py 的那层）：**运行期优先「本用例所属项目」**。

    NOTICE（0919-14 / 登记项①）：schema / XSD 的相对路径在**断言期**解析，这里拿不到
    runner，所以按下面的顺序取：

    1. `loader.current_run_root_dir()` —— **正在执行的那个用例所属项目**的根
       （由 `SessionRunner.test_start` 用 `use_run_root_dir()` 声明，可嵌套可恢复）；
    2. 退回全局 `loader.project_meta.RootDir` —— 「**当前已加载**」的项目。

    为什么必须优先第 1 条：跨项目引用时全局可能已经指向**别的项目**，于是会拿
    **別的项目的同名 schema** 去校验 —— 严格的那个方向表现为「莫名失败」，
    宽松的那个方向表现为「**莫名通过**」（后者是 0919-3 最忌讳的那种静默）。

    第 2 条保留是为了**不在用例执行范围内的调用方**（CLI 加载、单元测试直接调 comparator）
    行为不变；有反向护栏用例钉住这条。
    """
    try:
        from interfacetester import loader  # noqa: PLC0415

        run_root = loader.current_run_root_dir()
        if run_root:
            return run_root
        return getattr(loader.project_meta, "RootDir", "") or ""
    except Exception:  # noqa: BLE001 - 探测失败不应影响校验本身
        return ""


def _resolve_schema_path(path: Text, kind: Text = "schema", hint: Text = "") -> Text:
    """把 schema / XSD 文件路径解析成绝对路径：**相对项目根目录（RootDir）解析**。

    规则定死（避免"到底按谁解析"的争论）：
    1. 绝对路径直接用；
    2. 相对路径先按项目根目录（`debugtalk.py` 所在目录）拼；
    3. 再退化为当前工作目录；
    4. 都找不到 → 报错并列出**已尝试过的全部路径**（否则用户只能靠猜）。

    NOTICE: `kind` / `hint` 只是**措辞**参数（A2-2 起 JSON Schema 与 XSD 共用这份解析）：
    默认值保持原样，`jsonschema_match` 的既有报错文案一字不变。
    """
    candidates = []
    if os.path.isabs(path):
        candidates.append(path)
    else:
        root_dir = _project_root_dir()
        if root_dir:
            candidates.append(os.path.join(root_dir, path))
        cwd = os.getcwd()
        if not root_dir or os.path.normcase(cwd) != os.path.normcase(root_dir):
            candidates.append(os.path.join(cwd, path))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    raise RuntimeError(
        f"{kind} 文件不存在：{path}\n"
        f"已尝试：\n" + "\n".join(f"  - {candidate}" for candidate in candidates) + "\n"
        f"提示：相对路径按「项目根目录（含 debugtalk.py 的那层）」解析；"
        f"{hint or '也可以把 schema 放 debugtalk.py 里用 ${func()} 返回（见《能力清单》3.3）'}"
    )


def _load_schema_file(path: Text) -> Any:
    """读取 schema 文件：`.yaml/.yml` 按 YAML 解析，其余按 JSON 解析（UTF-8）。"""
    resolved = _resolve_schema_path(path)
    with open(resolved, mode="r", encoding="utf-8") as fp:
        content = fp.read()
    try:
        if resolved.lower().endswith((".yaml", ".yml")):
            import yaml  # noqa: PLC0415

            return yaml.safe_load(content)
        return json.loads(content)
    except Exception as ex:  # noqa: BLE001 - 解析失败要说清是哪个文件
        raise RuntimeError(f"schema 文件解析失败：{resolved}\n{type(ex).__name__}: {ex}") from ex


def _format_validation_errors(errors, draft_name: Text) -> Text:
    """把 jsonschema 的校验错误转成「测试人员看得懂」的多行文本。

    每条包含：**出错路径 + 期望（哪个关键字、期望什么）+ 实际值**，
    有 `description` 时一并带上（schema 作者写的说明往往比关键字更好懂）。
    """
    lines = [f"JSON Schema 校验失败：共 {len(errors)} 处不符合（draft={draft_name}）"]
    for error in errors[:JSONSCHEMA_MAX_ERRORS]:
        path = getattr(error, "json_path", None) or "$"
        expected = f"{error.validator}={_render_expectation(error.validator_value)}"
        actual = _render_actual(getattr(error, "instance", None))
        line = f"- {path}: 期望 {expected}，实际 {actual}"
        schema = error.schema if isinstance(error.schema, dict) else {}
        if schema.get("description"):
            line += f"（说明：{schema['description']}）"
        lines.append(line)

    if len(errors) > JSONSCHEMA_MAX_ERRORS:
        lines.append(f"- ……另有 {len(errors) - JSONSCHEMA_MAX_ERRORS} 处不符合（已省略）")
    return "\n".join(lines)


# JSON Schema **规范定义**的 format 名（json-schema.org 的 format 词汇表）。
#
# 用途（0918-8 / H7）：判断「schema 里出现的 format 是不是**本该被强制**的规范 format」。
# 为什么需要这份清单：jsonschema 在 **import 期**就把「可选依赖没装」的 checker **摘掉**了
# （实测 4.26：`_draft_checkers["draft202012"]` 与 `FORMAT_CHECKER.checkers` 都只剩
#  date / email / idn-email / idn-hostname / ipv4 / ipv6 / regex / uuid 八个），
# 所以运行时已经无法从库里问出「哪些 format 本来该强制」——只能拿规范清单反推。
# 而 `binary` / `int32` / `byte` 这类 **OpenAPI 自定义标注**不在规范里、本就不由 JSON Schema 强制，
# 告警只会变成噪声（实测 OpenAPI 导入器会产出 `format: binary`），因此刻意不列入。
JSON_SCHEMA_STANDARD_FORMATS = frozenset(
    {
        "date-time",
        "time",
        "uri",
        "uri-reference",
        "iri",
        "iri-reference",
        "hostname",
        "duration",
        "json-pointer",
        "relative-json-pointer",
        "uri-template",
        "color",
    }
)


def _supported_formats(validator_class) -> set:
    """当前环境**真正能强制**的 format 名（jsonschema 已按装了的可选依赖裁剪过）。"""
    checker = getattr(validator_class, "FORMAT_CHECKER", None)
    return set(getattr(checker, "checkers", {}) or {})


def _unenforceable_formats(schema: Any, validator_class) -> list:
    """收集 schema 里「出现了、但当前环境强制不了」的规范 format 名（去重排序）。"""
    supported = _supported_formats(validator_class)
    found = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            fmt = node.get("format")
            if isinstance(fmt, Text) and fmt not in supported:
                if fmt in JSON_SCHEMA_STANDARD_FORMATS:
                    found.add(fmt)
            for child in node.values():
                walk(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(schema)
    return sorted(found)


def _warn_unenforceable_formats(formats: list, validator_class) -> None:
    """对「规范里有、本环境强制不了」的 format 显式告警（只告警不报错）。

    NOTICE（0918-8 / H7）：**不能只加 `format_checker` 就完事**。加了之后，本环境能强制的
    8 个 format 会真断言，而 `date-time`/`uri`/`hostname` 这些**仍然是静默通过**——
    那只是把「完全没强制」换成「部分强制 + 用户以为全强制了」，假信心换了个位置。
    所以必须把「这批 format 没参与断言」说出来，并给出安装提示。
    """
    from loguru import logger  # noqa: PLC0415 - 与 _jsonschema_module 同为按需导入

    logger.warning(
        f"jsonschema_match：schema 使用了 {formats}，但当前环境**无法强制**这些 format"
        f"（缺可选依赖，jsonschema 在导入期已把对应 checker 摘掉）——"
        f"它们不会参与断言，契约里这几项等于没写。\n"
        f"  当前可强制的 format：{sorted(_supported_formats(validator_class))}\n"
        f"  要强制它们：pip install \"jsonschema[format]\""
        f"（会带上 rfc3339-validator / rfc3987 / fqdn 等）；"
        f"或把这几项改写成等价的内置算子（如 regex_match / length_*）。"
    )


def _json_schema_validate(check_value: Any, schema: Any) -> Text:
    """用 schema 校验 check_value；不通过时返回**可读的多行失败信息**，通过返回空串。

    抽成独立函数是为了让 `jsonschema_match` 保持「算子」的窄接口，
    同时让「错误信息格式化」可以被单测直接覆盖。
    """
    jsonschema = _jsonschema_module()
    from jsonschema import exceptions as jsonschema_exceptions  # noqa: PLC0415

    # draft 选择：schema 里写了 `$schema` 就按它来，否则显式用 Draft 2020-12
    # （显式声明 draft，避免"同一份 schema 在不同版本下行为不同"这种难解释的情况）
    validator_class = jsonschema.validators.validator_for(
        schema, default=jsonschema.Draft202012Validator
    )

    try:
        validator_class.check_schema(schema)
    except jsonschema_exceptions.SchemaError as ex:
        raise AssertionError(
            f"schema 本身不合法（draft={validator_class.__name__}）：{ex.message}\n"
            f"出错位置：{list(ex.absolute_schema_path)}\n"
            f"schema：{_shorten(schema)}"
        ) from ex

    # 0918-8 / H7：**必须**带 format_checker，否则 schema 里的 `format` 完全不参与断言。
    # 修复前这里是 `validator_class(schema)`：实测 `{"email": "not-an-email"}` 对
    # `{"format": "email"}` **通过**——契约断言静默退化成「只看类型」。
    # 而本项目的 OpenAPI 导入器**会把 format 带进断言 schema**（openapi_adapter.build_assertion_schema），
    # 于是「导入契约 → 生成断言 → 跑用例」这条推荐链路上，date-time/uri/uuid/email 全都形同虚设。
    unenforceable = _unenforceable_formats(schema, validator_class)
    if unenforceable:
        _warn_unenforceable_formats(unenforceable, validator_class)

    format_checker = getattr(validator_class, "FORMAT_CHECKER", None)
    errors = sorted(
        validator_class(schema, format_checker=format_checker).iter_errors(check_value),
        key=lambda error: (list(error.absolute_path), str(error.validator)),
    )
    if not errors:
        return ""
    return _format_validation_errors(errors, validator_class.__name__)


def jsonschema_match(check_value: Any, expect_value: Any, message: Text = ""):
    """第 19 个内置算子：断言 check_value 符合 JSON Schema。

    用法（两种引用方式，**不支持内联**）：

        # 1) 首选：schema 放 debugtalk.py，函数返回（$schema/$ref 原样存活）
        validate:
            - jsonschema_match: ["body", "${get_user_schema()}"]

        # 2) 次选：schema 文件路径（相对项目根目录）
        validate:
            - jsonschema_match: ["body", "schemas/user.json"]

    NOTICE: 第三个参数 `message` 会**前置**在详细失败信息之前（详细路径/期望/实际照旧保留），
    所以自定义说明不会把最有用的信息挤掉。
    """
    if isinstance(expect_value, dict):
        schema = expect_value
    elif isinstance(expect_value, str):
        if expect_value.lstrip().startswith(("{", "[")):
            raise RuntimeError(
                "jsonschema_match 的第二个参数看起来是**内联 schema**（以 { 或 [ 开头）：\n"
                f"  {_shorten(expect_value)}\n"
                "内联 schema 里的 $schema/$ref 会被框架当变量解析（运行期 VariableNotFound），"
                "请改成：① debugtalk.py 里返回 schema 的 ${func()}；② schema 文件路径。"
            )
        schema = _load_schema_file(expect_value)
    else:
        raise RuntimeError(
            f"jsonschema_match 的第二个参数必须是 schema（dict）或 schema 文件路径（str），"
            f"实际是 {type(expect_value).__name__}: {_shorten(expect_value)}"
        )

    if not isinstance(schema, dict):
        raise RuntimeError(
            f"schema 必须是 object（JSON Schema 的根通常是 object 或 boolean），"
            f"实际是 {type(schema).__name__}: {_shorten(schema)}"
        )

    failure = _json_schema_validate(check_value, schema)
    if failure:
        raise AssertionError(f"{message}\n{failure}" if message else failure)


# --------------------------------------------------------------------------- XML / XPath（A2-1）
# 第 20、21 个内置算子：XML 响应体（SOAP 等）的结构化断言。
#
# **为什么不用标准库的 ElementTree**（A1 的 `examples/soap/debugtalk.py` 走的是那条路）：
#   1) 完整 XPath 1.0：ElementTree 只支持 XPath 的一个子集（没有 `local-name()` / `count()` /
#      `string()` / 轴），而 SOAP 现场这些都常用；
#   2) XSD 契约校验：标准库**没有**验证器（`xml.etree.ElementTree.XMLSchema` 不存在）→ A2-2 要靠 lxml。
# 因此 lxml 放在**可选 extra `xml`** 里（决策见《接口自动化框架升级优化评估》2.6.3）：
# 二进制依赖不进主依赖，缺依赖时给出**可操作的安装提示**（而不是 ModuleNotFoundError 堆栈）。
#
# **项目 debugtalk.py 优先**：查找顺序是 `debugtalk → 内置`（`parser.get_mapping_function`），
# 所以 A1 用户（项目里已有标准库版 `xpath_match`）装了 extra 也**不受影响**，行为仍是自己那份。
#
# 选择器两种写法（同一个算子，按写法自动分流，**只做加法、不改既有语义**）：
#   1) **简化写法（推荐，前缀无关）**：`code` / `.//code` / `{URI}code` / `item[@id='1']`
#      —— 编译成 `local-name()` 版 XPath，命名空间前缀漂移不影响命中（= A1 的语义，一字不改）；
#   2) **原始 XPath 1.0**：其余写法原样交给 lxml 求值（`//svc:code/text()`、`count(//item)`、
#      `ancestor::`、`*`、`[2]` …）。前缀按**报文里的声明**收集（全树 nsmap），
#      也可以用 `local-name()` 完全绕开前缀。
#
# 报错分界（与 P2-b 同一套口径）：
#   - **被测对象（响应数据）的问题 → AssertionError**（用例失败）：非法 XML、未选中节点、文本/个数不符；
#   - **用例写法 / 环境的问题 → RuntimeError**（用例错误）：XPath 语法错、前缀未声明、缺 lxml。
#
# NOTICE: 这个类型区分是为了**人能一眼看出该改用例还是该找开发**；落到 pytest 的 JUnit 报告上
# 两者都是 `<failure>`（pytest 对 call 阶段的异常一律记 failure，`<error>` 只用于 setup/teardown），
# 所以别指望靠报告计数把它们分开。

# 简化写法：`name`，可带**一个**属性谓词 `[@attr='value']`（与 A1 的语法逐字一致）
_XML_SELECTOR_RE = re.compile(
    r"^(?P<name>[^\[\]]+?)(?:\[@(?P<attr>[\w:.\-]+)\s*=\s*'(?P<value>[^']*)'\])?$"
)
# 已经是 str 的报文里，XML 声明的 encoding 与实际编码不符 → 摘掉它（见 `_xml_bytes`）
_XML_DECL_ENCODING_RE = re.compile(
    r"^(<\?xml[^>]*?)\s+encoding\s*=\s*(?P<quote>['\"])[^'\"]*(?P=quote)", re.IGNORECASE
)
# 非法 XML 时回显的原文长度（够看清开头，又不会把报告刷爆）
_XML_PREVIEW_LIMIT = 200


def _lxml_etree():
    """懒导入 lxml：缺依赖时给**可操作的安装提示**（而不是当成断言失败）。"""
    try:
        from lxml import etree  # noqa: PLC0415
    except ImportError as ex:  # pragma: no cover - 由 tests/xml_comparators_test.py 用 monkeypatch 覆盖
        raise RuntimeError(
            f"未安装 lxml，无法执行 XML/XPath 断言：{ex}\n"
            '安装：pip install lxml（本项目：pip install -e ".[xml]"）\n'
            "说明：lxml 是二进制依赖，因此放在可选 extra `xml` 里；"
            "若目标机器装不了 wheel，可改用标准库版算子（见 examples/soap/debugtalk.py 的 A1 实现，零依赖）"
        ) from ex
    return etree


def _xml_bytes(check_value: Any) -> bytes:
    """把 check_value 统一成**交给 lxml 的 bytes**，顺带给出可读的类型错误。

    NOTICE: 统一成 bytes 而不是 str，两个理由：
    - 非 JSON 响应（含 XML）框架给的是 `resp.content` → bytes，直接交给 lxml 能按报文**自己的
      encoding 声明**解码（GBK 报文也正确，而先解码成 str 再交给 lxml 会变成乱码）；
    - 已经是 str（例如 check 写 `text`，或单测直接传字符串）说明**已被解码过**，
      此时声明里的 encoding 与实际字节不再一致 → 必须先摘掉声明里的 encoding 再按 UTF-8 编码，
      否则 lxml 会照声明去解码我们编出来的 UTF-8 字节（轻则乱码，重则 `XMLSyntaxError`）。
    """
    if isinstance(check_value, (bytes, bytearray)):
        return bytes(check_value)
    if isinstance(check_value, str):
        # NOTICE（0919-2 / 缺陷 7）：**先剥 BOM，再做判断与替换**。
        # 修复前是 `text[:5].lstrip("\ufeff")` —— 只把 BOM 从「判断用的 5 字窗口」里剥掉，
        # 于是**两个独立原因**都让 encoding 声明活了下来（实测两处都验过）：
        #   ① 5 字窗口剥掉 BOM 只剩 4 字 `<?xm`，永远 `startswith` 不了 5 字的 `<?xml`
        #      → 那个 `if` 根本不成立，`sub` 一次都没跑；
        #   ② 就算它成立，正则也是 `^(<\?xml…)` 锚定的，而 `^` 处的字符是 BOM → 仍不匹配。
        # 而真正被 `.encode("utf-8")` 送出去的，始终是**带 BOM 且带声明**的原串。
        # 后果（实测）：声明写 `encoding="GBK"` 的报文，lxml 拿声明去解码我们的 UTF-8 字节
        # → `XMLSyntaxError: Growing input buffer, line 1, column 37`（或乱码）。
        #
        # 修法就是一句：**先 `lstrip` 掉 BOM**，判断与替换都作用在同一个（已剥 BOM 的）串上。
        # 顺带去掉了那个 5 字窗口——它正是这个 bug 的成因，而 `startswith` 本身只看前 5 个字符，
        # 开窗没有任何收益。
        text = check_value.lstrip("\ufeff")
        if text.lower().startswith("<?xml"):
            text = _XML_DECL_ENCODING_RE.sub(r"\1", text, count=1)
        return text.encode("utf-8")
    raise AssertionError(
        f"XML 算子只能作用在报文文本上，实际收到 {type(check_value).__name__}"
        "（check 写 `body`——XML 响应的 body 是 bytes；或写 `text`；"
        "JSON 响应请用 jsonschema_match）"
    )


def _xml_root(check_value: Any):
    """解析报文，返回 lxml 根元素；非法 XML → AssertionError（**响应数据的问题 = 用例失败**）。

    NOTICE: **先做类型校验再导入 lxml** —— check 写错（例如 JSON 响应的 `body` 是 dict）时
    要报「类型错」，不该被「缺 lxml」这种环境问题掩盖（诊断顺序错了，用户会去装没用的依赖）。
    """
    data = _xml_bytes(check_value)
    etree = _lxml_etree()
    try:
        return etree.fromstring(data)
    except etree.XMLSyntaxError as ex:
        preview = data[:_XML_PREVIEW_LIMIT].decode("utf-8", errors="replace")
        raise AssertionError(f"响应体不是合法 XML：{ex}\n原文前 {_XML_PREVIEW_LIMIT} 字：{preview!r}") from ex


def _xml_namespaces(root) -> Dict[Optional[Text], Text]:
    """**全树**收集前缀 → URI 映射。

    NOTICE: `root.nsmap` 只含**根节点上声明**的前缀（实测：SOAP 报文只收得到 `soap`），
    内层元素上的 `ns1:` / `abc:` 收不到 → 原始 XPath 里写内层前缀就会报 undefined namespace prefix。
    lxml 每个元素的 `nsmap` 都包含继承来的声明，所以遍历一次全树即可；同一前缀多处声明时**以先出现的为准**。

    NOTICE: **默认命名空间（前缀为 `None`）必须丢掉**：XPath 1.0 没法给默认命名空间编前缀，
    lxml 会直接抛 `TypeError: empty namespace prefix is not supported in XPath`（实测）；
    命中默认命名空间要走 `local-name()`，也就是简化写法本身。
    """
    namespaces: Dict[Optional[Text], Text] = {}
    for element in root.iter():
        for prefix, uri in (getattr(element, "nsmap", None) or {}).items():
            if prefix is None:
                continue
            if prefix not in namespaces:
                namespaces[prefix] = uri
    return namespaces


def _xpath_literal(value: Text) -> Text:
    """把值渲染成 XPath 1.0 的**字符串字面量**（XPath 没有转义符，含引号只能用 `concat()`）。"""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = [f"'{part}'" if part else None for part in value.split("'")]
    pieces = []
    for index, part in enumerate(parts):
        if index:  # 被 split 掉的引号本身
            pieces.append("\"'\"")
        if part:
            pieces.append(part)
    return "concat({})".format(", ".join(pieces))


def _xml_selector_to_xpath(selector: Any) -> Optional[Text]:
    """把**简化写法**编译成前缀无关的 XPath；不是简化写法时返回 `None`（按原始 XPath 处理）。

    编译规则（三条都是为了「命名空间前缀漂移也能命中」）：

        name            → descendant-or-self::*[local-name()='name']
        prefix:name     → 同 name（**前缀被丢掉**，按 local-name 比对 —— 前缀本来就会漂移）
        {URI}name       → descendant-or-self::*[local-name()='name' and namespace-uri()='URI']
        name[@a='v']    → descendant-or-self::*[local-name()='name'][@*[local-name()='a']='v']

    NOTICE: lxml **不支持** Clark 记法（把 `{URI}name` 直接丢给 lxml 会报 Invalid expression），
    所以必须自己翻译成 `local-name()` + `namespace-uri()`。

    NOTICE（批次 C / **L13**，两处修复，都是"节点明明存在却报未命中"的**假失败**）：

    1. **带前缀的名字**（`svc:code`、`.//svc:code`）修复前被整串当成节点名编译成
       `local-name()='svc:code'` —— 而真实的 `local-name()` 是 `code`，**永远不命中**
       （实测：`xpath_match: ["body", ["svc:code", "0000"]]` 对一份正常报文报
       「未选中任何节点：svc:code」）。现在**丢掉前缀**按 local-name 比对，
       与「前缀漂移也能命中」这个简化写法的本意一致。
    2. 编译出的路径修复前是 `.//*[...]` —— **后代**搜索，**不包含上下文节点本身**；
       于是「响应根元素就是业务元素」时（非 SOAP 包裹的普通 XML 接口）
       简化写法**永远选不中根元素**，而 `xml_schema_match` 文档**推荐**的
       `{{"xpath": "QueryResponse", ...}}` 写法恰好踩这个坑。现在改用
       `descendant-or-self::*[...]`（对既有 SOAP 报文——根是 `Envelope`——行为完全不变，
       因为根不叫那些名字）。

    NOTICE（与 A1 的一致性）：`examples/soap/debugtalk.py` 的项目级算子（零依赖退路）是
    同一套语义的复制品，**本次同步修了它的这两处**，两边命中集合继续一致。
    """
    sel = str(selector).strip()
    if sel.startswith(".//"):
        sel = sel[3:]
    if not sel or sel in (".", ".."):
        return None

    want_ns = None
    if sel.startswith("{"):
        if "}" not in sel:
            return None
        want_ns, sel = sel[1:].split("}", 1)

    if sel.startswith("/") or "/" in sel or "{" in sel or "}" in sel:
        return None

    matched = _XML_SELECTOR_RE.match(sel)
    if not matched:
        return None

    name = (matched.group("name") or "").strip()
    if not name or any(char in name for char in "*()"):
        return None

    # 批次 C / L13-①：带前缀的名字丢掉前缀（`svc:code` → `code`）
    if ":" in name:
        name = name.rsplit(":", 1)[1].strip()
        if not name:
            return None

    conditions = [f"local-name()={_xpath_literal(name)}"]
    if want_ns is not None:
        conditions.append(f"namespace-uri()={_xpath_literal(want_ns)}")
    # 批次 C / L13-②：`descendant-or-self` 让**上下文节点自己**也能命中
    expression = "descendant-or-self::*[{}]".format(" and ".join(conditions))

    attr, value = matched.group("attr"), matched.group("value")
    if attr is not None:
        # NOTICE: 属性前缀同样会漂移，所以按 local-name 比对（A1 的 `_attr_matches` 是同一策略）。
        # 唯一的语义差别：同一元素上「同 local-name、不同命名空间」的两个属性，A1 只看原名那个，
        # 这里按 XPath 的存在量词语义（任一命中即算命中）——这种报文本身就有歧义。
        attr = attr.rsplit(":", 1)[1] if ":" in attr else attr
        expression += "[@*[local-name()={}]={}]".format(
            _xpath_literal(attr), _xpath_literal(value)
        )
    return expression


def _xml_xpath(check_value: Any, selector: Any) -> Tuple[Any, Text, bool]:
    """求值选择器 → `(XPath 结果, 实际表达式, 是否简化写法)`。"""
    return _xml_xpath_on_root(_xml_root(check_value), selector)


def _xml_xpath_on_root(root: Any, selector: Any) -> Tuple[Any, Text, bool]:
    """在**已解析好的根元素**上求值选择器（`xml_schema_match` 要复用同一棵树，避免重复解析）。"""
    etree = _lxml_etree()

    compiled = _xml_selector_to_xpath(selector)
    expression = compiled if compiled is not None else str(selector).strip()
    if not expression:
        raise AssertionError("选择器（XPath 表达式）不能为空")

    try:
        result = root.xpath(expression, namespaces=_xml_namespaces(root))
    except etree.XPathEvalError as ex:
        hint = (
            "提示：前缀必须在报文里有声明（否则报 undefined namespace prefix）。"
            "若希望「前缀漂移也命中」，改用简化写法：`code` / `{URI}code` / `item[@id='1']`"
            if compiled is None
            else f"提示：这是简化写法编译出来的表达式：{expression}"
        )
        raise RuntimeError(
            f"XPath 表达式不合法：{expression}\n{type(ex).__name__}: {ex}\n{hint}"
        ) from ex
    return result, expression, compiled is not None


def _xml_node_text(node: Any) -> Text:
    """取节点文本：元素取 `.text`，文本/属性节点本身就是 str（lxml 用 str 子类表示）。"""
    if isinstance(node, str):
        return str(node)
    text = getattr(node, "text", None)
    return "" if text is None else str(text)


def _xml_scalar_text(value: Any) -> Text:
    """把标量求值结果渲染成文本：整数值的 float 去掉 `.0`（`count()` 返回的是 float）。

    NOTICE（0918-8 / L16）：**布尔按 XPath 规范渲染成 `true` / `false`**。
    修复前用 `str(value)`，得到的是 Python 的 `True` / `False`：
    用户在 YAML 里写 `["boolean(//code)", "false"]`（XPath 字面量）永远对不上，
    而写 `"False"` 才能过——那是 Python 的拼写，不是 XPath 的。
    """
    if isinstance(value, bool):  # 先判 bool：它是 int 的子类
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _xml_matched(result: Any) -> bool:
    """判定「选中了 / 条件成立」：节点集合非空、布尔为真、数字非 0、字符串非空。"""
    if isinstance(result, bool):  # 先判 bool：它是 int 的子类
        return result
    if isinstance(result, list):
        return bool(result)
    if isinstance(result, (int, float)):
        return result != 0
    return bool(result)


def _xml_empty_hint(expression: Text, is_dsl: bool) -> Text:
    """未命中时的补充提示：原始 XPath 容易栽在「前缀对不上 / 路径不精确」上。"""
    if is_dsl:
        return ""
    return (
        f"\n（原始 XPath：{expression}；想「前缀漂移也命中」请用简化写法 `code` / `{{URI}}code`，"
        "或把表达式写成 `local-name()` 形式）"
    )


def _xml_spec(expect_value: Any) -> Tuple[Text, Optional[Text]]:
    """`"code"` → 只断存在；`["code", "0000"]` → 再断文本（与 A1 的 `_spec` 一致）。"""
    if isinstance(expect_value, (list, tuple)):
        if not expect_value:
            raise AssertionError("expect_value 不能是空列表")
        path = expect_value[0]
        expect_text = expect_value[1] if len(expect_value) > 1 else None
    else:
        path, expect_text = expect_value, None
    return str(path), (None if expect_text is None else str(expect_text))


def xpath_match(check_value: Any, expect_value: Any, message: Text = ""):
    """第 20 个内置算子（A2-1）：XML 响应体的 XPath 断言。需要可选依赖 `xml`（lxml）。

    用法（`check` 写 `body`；两种选择器写法可混用）：

        validate:
          # 简化写法（推荐）：前缀无关，ns1: / abc: / 默认命名空间都能命中
          - xpath_match: ["body", ["code", "0000"]]
          - xpath_match: ["body", ["{http://demo.example.com/svc}code", "0000"]]
          - xpath_match: ["body", "item[@id='2']"]
          # 原始 XPath 1.0：轴、text()、count()、string()、位置谓词……
          - xpath_match: ["body", "//soap:Body/svc:QueryResponse/svc:code"]
          - xpath_match: ["body", ["string(//*[local-name()='code'])", "0000"]]

    `expect_value` 形态：`"选择器"`（只断**存在**）或 `["选择器", "期望文本"]`（再断文本）；
    文本断言对节点集合的语义是「**任一**选中节点的文本等于期望值」（与 A1 一致）。

    NOTICE（0918-8 / L16）：**存在性门禁只对「节点集合」生效**。
    修复前对任何结果都先跑 `_xml_matched`，于是这些**完全合法**的断言写不出来：
      - `xpath_match: ["body", ["count(//item)", "0"]]`（个数为 0）
      - `xpath_match: ["body", ["boolean(//flag)", "false"]]`（条件为假）
      - `xpath_match: ["body", ["string(//code)", ""]]`（文本为空）
    它们统统被报成「未选中任何节点」——诊断与事实相反（表达式求值成功了，
    只是值是 `0` / `false` / 空串）。现在：节点集合空 → 未选中；标量 → 按文本比较，
    布尔按 XPath 规范渲染成 `true` / `false`（见 `_xml_scalar_text`）。

    NOTICE: 与 A1（`examples/soap/debugtalk.py`）的关系——简化写法的语义**完全一致**；
    额外支持原始 XPath（A1 会明确拒绝的那些写法）。项目 `debugtalk.py` 里有同名函数时**以项目版本为准**。
    """
    path, expect_text = _xml_spec(expect_value)
    result, expression, is_dsl = _xml_xpath(check_value, path)

    if isinstance(result, list):
        # 只有节点集合才有「选中了没有」这回事
        if not result:
            raise AssertionError(
                f"未选中任何节点：{path}"
                + _xml_empty_hint(expression, is_dsl)
                + (f"\n{message}" if message else "")
            )
    elif expect_text is None and not _xml_matched(result):
        # 标量结果 + 只写「存在性」：这是个写法错误（对标量谈不上"选中"），
        # 给一条能直接照抄的正确写法，而不是说"未选中任何节点"。
        raise AssertionError(
            f"{path} 的求值结果是 {_xml_scalar_text(result)!r}"
            f"（{type(result).__name__}），不是节点集合。\n"
            f"「只断存在」对标量不适用：请写成 `[表达式, 期望值]` 两元素形式，"
            f'例如 `["boolean(//code)", "false"]`、`["count(//item)", "0"]`。'
            + (f"\n{message}" if message else "")
        )

    if expect_text is not None:
        if isinstance(result, list):
            actuals = [_xml_node_text(node) for node in result]
            if expect_text not in actuals:
                raise AssertionError(
                    f"{path} 选中 {len(actuals)} 个节点，但文本都不是 {expect_text!r}；实际={actuals}"
                    + (f"\n{message}" if message else "")
                )
        else:
            actual = _xml_scalar_text(result)
            if actual != expect_text:
                raise AssertionError(
                    f"{path} 的求值结果不是 {expect_text!r}；实际={actual!r}（表达式：{expression}）"
                    + (f"\n{message}" if message else "")
                )
    return True


def xpath_count(check_value: Any, expect_value: Any, message: Text = ""):
    """第 21 个内置算子（A2-1）：XML 节点**个数**断言。需要可选依赖 `xml`（lxml）。

        validate:
          - xpath_count: ["body", ["item", 3]]                  # 简化写法：前缀无关
          - xpath_count: ["body", ["//svc:item", 3]]            # 原始 XPath
          - xpath_count: ["body", ["count(//svc:item)", 3]]     # 直接写 count() 也行

    `expect_value` 必须是 `[选择器, 个数]`；个数接受数字或数字字符串（与 A1 一致）。
    """
    if not isinstance(expect_value, (list, tuple)) or len(expect_value) != 2:
        raise AssertionError("xpath_count 的 expect_value 必须是 [选择器, 个数]")
    path = str(expect_value[0])
    try:
        expect_count = int(expect_value[1])
    except (TypeError, ValueError):
        raise AssertionError(f"xpath_count 期望个数必须是整数，实际 {expect_value[1]!r}") from None

    result, expression, _is_dsl = _xml_xpath(check_value, path)
    if isinstance(result, list):
        actual = len(result)
    elif isinstance(result, bool):
        actual = int(result)
    elif isinstance(result, (int, float)):
        # NOTICE（0918-8 / L17）：**不能 `int(result)` 截断**。
        # `sum(//n)` 返回 3.5 时 `int(3.5) == 3`，于是「期望 3」会**通过**——
        # 个数断言变成了假通过（而被测接口的数值明显不对）。
        # 非整数直接判失败，并指出该用哪个算子。
        if isinstance(result, float) and not result.is_integer():
            raise AssertionError(
                f"xpath_count 的选择器求出了小数 {result}（不是整数个数）：{path}\n"
                f"hint: 需要断言数值本身（如 `sum(...)`）请改用 `xpath_match: [\"body\", [\"sum(//n)\", \"3.5\"]]`。"
                + (f"\n{message}" if message else "")
            )
        actual = int(result)
    else:
        raise RuntimeError(
            f"xpath_count 的选择器必须求出「节点集合」或「数字」，实际是 "
            f"{type(result).__name__}：{_xml_scalar_text(result)!r}（表达式：{expression}）"
        )

    if actual != expect_count:
        raise AssertionError(
            f"{path} 选中 {actual} 个节点，期望 {expect_count} 个"
            + (f"\n{message}" if message else "")
        )
    return True


# --------------------------------------------------------------------------- XSD（A2-2）
# 第 22 个内置算子：`xml_schema_match` —— 拿 XSD 契约校验 XML 响应体（SOAP 的「契约即用例」）。
#
# **为什么必须先定位子节点**（A2 原计划漏掉的必需项，实测）：客户 XSD 通常只描述 **Body 里的业务元素**，
# 直接校验整个 Envelope 必然失败：
#   Element '{http://schemas.xmlsoap.org/soap/envelope/}Envelope':
#   No matching global declaration available for the validation root
# 先 `//svc:QueryResponse`（或简化写法 `QueryResponse`）选到业务元素再校验 → 通过。
#
# **XSD 只接受文件路径**：`xs:include` / `xs:import` 的相对路径按 **XSD 文件所在目录**解析，
# 内联/字符串形态解析不了（实测）；也不支持把 XSD 字符串写进 YAML。
#
# **命名空间是严格的**（这正是契约的意义）：XSD 按 **URI** 匹配，所以报文**前缀漂移不影响校验**
# （`ns1:` 与 `abc:` 声明同一个 URI → 都通过，实测）；但**没有 targetNamespace 的 XSD** 去校验
# 带命名空间的报文会报 `No matching global declaration`。
#
# 失败分界（与 P2-b 完全对齐）：
#   - XSD 文件找不到 / 不是合法 XML / 自身不合法（编译失败）→ **RuntimeError**（用例 / 环境问题）；
#   - 文档不符合 XSD → **AssertionError**（用例失败，最多 5 条，带 `error.path`）。

XMLSCHEMA_MAX_ERRORS = 5

# 已编译 XSD 的缓存：**绝对路径 → XMLSchema**（见 `_load_xsd` 的性能说明）
_XSD_SCHEMA_CACHE: Dict[Text, Any] = {}


def _load_xsd(resolved_path: Text):
    """按**绝对路径**缓存编译好的 `XMLSchema`。

    NOTICE: 为什么要缓存 —— 实测带 `xs:include` 的 XSD **首次编译 14.6 ms**（比《评估》2.6.1 里
    单独 `XMLSchema` 的 0.18 ms 贵得多，因为要把被 include 的文件一起读进来解析）；
    一个用例里几十条断言每条都重编译就是几百毫秒。缓存键用**解析后的绝对路径**（与 RootDir 解析天然对齐）。
    副作用：同一进程内改动 XSD 文件**不会**生效（要热改就重开进程）。

    NOTICE: 这里没有用 `functools.lru_cache` 装饰器 —— `BUILTIN_COMPARATOR_NAMES` 是按
    「模块内的公开函数」自动派生的（`make.py:46`），`from functools import lru_cache` 会把
    `lru_cache` 本身算成一个「内置算子」混进白名单（实测：算子数会从 22 变成 23）。
    用显式字典缓存既避开这个陷阱，也让 `tests/xml_comparators_test.py` 能直接清缓存来验证「只编译一次」。
    """
    cached = _XSD_SCHEMA_CACHE.get(resolved_path)
    if cached is not None:
        return cached

    etree = _lxml_etree()
    try:
        document = etree.parse(resolved_path)
    except etree.XMLSyntaxError as ex:
        raise RuntimeError(
            f"XSD 文件不是合法 XML：{resolved_path}\n{ex}"
        ) from ex
    except OSError as ex:
        raise RuntimeError(f"XSD 文件无法读取：{resolved_path}\n{ex}") from ex

    try:
        schema = etree.XMLSchema(document)
    except etree.XMLSchemaParseError as ex:
        details = "\n".join(
            f"  - {entry.message}" for entry in (ex.error_log or [])[:XMLSCHEMA_MAX_ERRORS]
        )
        raise RuntimeError(
            f"XSD 自身不合法，无法编译：{resolved_path}\n"
            + (details + "\n" if details else "")
            + "常见原因：引用了未定义的 type/元素、include/import 的文件缺失、"
            "targetNamespace 与引用的 QName 不一致"
        ) from ex

    _XSD_SCHEMA_CACHE[resolved_path] = schema
    return schema


def _xsd_spec(expect_value: Any) -> Tuple[Text, Optional[Text]]:
    """解析第二个参数 → `(XSD 文件路径, 定位用选择器 or None)`。

    两种形态（**dict 传参是刻意的**：`xpath` 与 `xsd` 的顺序不该靠记忆）：

        xml_schema_match: ["body", "schemas/query.xsd"]                                     # 整个报文就是那个元素
        xml_schema_match: ["body", {"xpath": "QueryResponse", "xsd": "schemas/query.xsd"}]  # 推荐：先定位再校验

    `xpath` 与 `xpath_match` 的选择器写法**完全一致**（简化写法前缀无关，也可以写完整 XPath）。
    """
    if isinstance(expect_value, str):
        path = expect_value.strip()
        if not path:
            raise RuntimeError("xml_schema_match 的第二个参数不能是空字符串（要写 XSD 文件路径）")
        if path.startswith("<"):
            raise RuntimeError(
                "xml_schema_match 只接受 **XSD 文件路径**，不支持内联 XSD 字符串：\n"
                f"  {_shorten(path)}\n"
                "原因：`xs:include` / `xs:import` 的相对路径要按 XSD 文件所在目录解析，"
                "内联字符串没有「所在目录」。正确写法：把 XSD 存成文件，例如 "
                '{"xpath": "//svc:QueryResponse", "xsd": "schemas/query.xsd"}'
            )
        return path, None

    if isinstance(expect_value, dict):
        unknown = sorted(str(key) for key in expect_value if key not in ("xpath", "xsd"))
        if unknown:
            raise RuntimeError(
                f"xml_schema_match 的 dict 只支持 `xpath` 与 `xsd` 两个键，多出来的键：{unknown}\n"
                f"你写的是：{_shorten(expect_value)}"
            )
        xsd = expect_value.get("xsd")
        if not isinstance(xsd, str) or not xsd.strip():
            raise RuntimeError(
                "xml_schema_match 的 dict 必须带 `xsd`（XSD 文件路径，字符串），"
                f"你写的是：{_shorten(expect_value)}"
            )
        xpath = expect_value.get("xpath")
        if xpath is not None and not isinstance(xpath, str):
            raise RuntimeError(
                f"xml_schema_match 的 `xpath` 必须是字符串，实际是 {type(xpath).__name__}："
                f"{_shorten(xpath)}"
            )
        selector = xpath.strip() if isinstance(xpath, str) and xpath.strip() else None
        return xsd.strip(), selector

    raise RuntimeError(
        "xml_schema_match 的第二个参数必须是 XSD 文件路径（str）或 "
        '{"xpath": ..., "xsd": ...}（dict），'
        f"实际是 {type(expect_value).__name__}：{_shorten(expect_value)}"
    )


def _xml_schema_target(root: Any, selector: Optional[Text]) -> Any:
    """定位要校验的节点：没给选择器 → 文档根元素；给了 → 必须**唯一命中一个元素**。

    NOTICE: 命中多个节点时报错而不是「校验第一个」—— 静默挑一个是歧义行为，
    真需要逐条校验时应把选择器收窄（例如加位置谓词 `(//svc:item)[1]`）。
    """
    if selector is None:
        return root

    result, expression, is_dsl = _xml_xpath_on_root(root, selector)
    if not isinstance(result, list):
        raise RuntimeError(
            f"xml_schema_match 的 `xpath` 必须选出**元素节点**，实际求出 "
            f"{type(result).__name__}：{_xml_scalar_text(result)!r}（表达式：{expression}）"
        )
    if not result:
        raise AssertionError(
            f"未选中任何节点：{selector}"
            + _xml_empty_hint(expression, is_dsl)
            + "\n（XSD 校验要先定位到业务元素：客户 XSD 通常只描述 Body 里的那层元素，"
            '例如 {"xpath": "//svc:QueryResponse", "xsd": "schemas/query.xsd"}）'
        )
    if len(result) > 1:
        raise RuntimeError(
            f"xml_schema_match 的 `xpath` 选中了 {len(result)} 个节点，"
            "XSD 校验需要**唯一**节点；请把选择器收窄（例如 `(//svc:item)[1]`）或按节点分别断言\n"
            f"（表达式：{expression}）"
        )

    node = result[0]
    # NOTICE: lxml 用 str 子类表示文本/属性节点；注释/PI 的 `.tag` 不是字符串 —— 都不是可校验的元素。
    if isinstance(node, str) or not isinstance(getattr(node, "tag", None), str):
        raise RuntimeError(
            f"xml_schema_match 的 `xpath` 选中的不是元素节点（文本/属性/注释？）：{selector}\n"
            f"（表达式：{expression}）"
        )
    return node


def _xml_schema_errors(
    schema: Any, node: Any, resolved_path: Text, selector: Optional[Text]
) -> Text:
    """校验节点；不通过时返回**可读的多行失败信息**（通过返回空串）。

    `error_log` 实测**每次 `validate()` 都会重置**，所以可以放心紧跟在校验之后读取；
    `path` 用的是**报文自己的前缀**（例如 `/ns1:QueryResponse/ns1:code`），对排查最友好。
    """
    if schema.validate(node):
        return ""

    seen = set()
    entries = []
    for entry in schema.error_log:
        key = (entry.path or "", entry.message)
        if key in seen:
            continue
        seen.add(key)
        entries.append(key)

    located = f"（已定位：{selector}）" if selector else "（整个响应体）"
    lines = [f"XSD 校验失败：共 {len(entries)} 处不符合{located}", f"XSD：{resolved_path}"]
    for path, text in entries[:XMLSCHEMA_MAX_ERRORS]:
        lines.append(f"- {path}: {text}" if path else f"- {text}")
    if len(entries) > XMLSCHEMA_MAX_ERRORS:
        lines.append(f"- ……另有 {len(entries) - XMLSCHEMA_MAX_ERRORS} 处不符合（已省略）")
    return "\n".join(lines)


def xml_schema_match(check_value: Any, expect_value: Any, message: Text = ""):
    """第 22 个内置算子（A2-2）：用 XSD 契约校验 XML 响应体。需要可选依赖 `xml`（lxml）。

    用法（**推荐先定位业务元素**，因为客户 XSD 通常只描述 Body 里那层）：

        validate:
          - xml_schema_match: ["body", {"xpath": "QueryResponse", "xsd": "schemas/query.xsd"}]
          - xml_schema_match: ["body", {"xpath": "//svc:QueryResponse", "xsd": "schemas/query.xsd"}]
          # 少数情况：整个响应体就是那个元素
          - xml_schema_match: ["body", "schemas/query.xsd"]

    - XSD 路径**相对项目根目录**（含 `debugtalk.py` 的那层）解析，可写 `${func()}` 返回路径；
    - `xpath` 的写法与 `xpath_match` 一致（简化写法**前缀无关**，也可以是完整 XPath）；
    - **前缀漂移不影响校验**（XSD 按命名空间 URI 匹配），但 XSD 必须声明对应的 `targetNamespace`；
    - 失败信息给出 XSD 路径 + 出错节点路径 + 自然语言原因（最多 5 条，去重），
      用户自定义 `message` **前置**，不会把最有用的信息挤掉。
    """
    xsd_path, selector = _xsd_spec(expect_value)
    resolved_path = _resolve_schema_path(
        xsd_path,
        kind="XSD",
        hint="XSD 只能用**文件路径**（`xs:include` 的相对路径要按文件所在目录解析）",
    )
    schema = _load_xsd(resolved_path)

    # NOTICE: 先解析报文（类型错/非法 XML 直接报出来），再定位 —— 与 A2-1 的诊断顺序一致。
    root = _xml_root(check_value)
    node = _xml_schema_target(root, selector)

    failure = _xml_schema_errors(schema, node, resolved_path, selector)
    if failure:
        raise AssertionError(f"{message}\n{failure}" if message else failure)
    return True
