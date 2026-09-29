from datetime import timedelta
import re
from typing import Dict, Text, Any

import jmespath
from jmespath.exceptions import JMESPathError
from loguru import logger

from interfacetester import exceptions, utils
from interfacetester.exceptions import ValidationFailure, ParamsError
from interfacetester.models import VariablesMapping, Validators
from interfacetester.parser import parse_string_value, Parser


"""「操作数类型不匹配」型 TypeError 的识别特征。

P1-a：`elapsed`（timedelta）与数值算子、或 YAML 里 `"4"`（字符串）与 `4`（整数）比较时，
CPython 抛的是 `TypeError`；修复前它会直接冒泡成 pytest 的 **error**（且只有一行
`'<' not supported between instances of ...`，看不出是哪条断言、哪个检查项）。
现在只把下面五类「操作数类型不匹配」转成可读的断言失败，其余 TypeError 一律按真实错误抛出
（即：**不掩盖算子内部的 bug**，例如自定义算子里写错解包）。

五类特征都自带两侧类型信息，转换后不会丢信息：
1. 富比较：``'<' not supported between instances of 'timedelta' and 'int'``
2. 长度类算子作用在无长度对象上：``object of type 'int' has no len()``
3. 算术运算：``unsupported operand type(s) for -: 'str' and 'int'``
4. **（0917-1 新增）容器包含运算遇上 bytes**：``a bytes-like object is required, not 'str'``
   —— `contains: ["body", "0000"]` 在非 JSON 响应（`body` 是 bytes）上就是这一类，
   修复前它是**原始 TypeError**（pytest 报 error，不是可读的 failed）
5. **（0917-1 新增）`contained_by` 的左右操作数反了**：``'in <string>' requires string as left operand, not bytes``
6. **（0918-8 / L18 新增）容器包含运算遇上不可哈希的期望值**：``unhashable type: 'list'``
   —— `contains: ["body.headers", ["a", "b"]]`（检查项是 dict、期望值写成列表）就是这一类：
   `list in dict` 需要哈希，CPython 抛的是**与断言毫无关系**的 `unhashable type: 'list'`
7. **（0918-8 / L18 新增）对不可迭代对象做 `in`**：``argument of type 'int' is not iterable``
   —— 自定义算子/`contained_by` 把非容器当容器用时会出现
"""
_OPERAND_TYPE_ERROR_PATTERNS = (
    r"not supported between instances of",
    r"object of type .+ has no len\(\)",
    r"unsupported operand type\(s\) for",
    r"a bytes-like object is required",
    r"requires string as left operand",
    r"unhashable type",
    r"argument of type .+ is not iterable",
)
_OPERAND_TYPE_ERROR_RE = re.compile("|".join(_OPERAND_TYPE_ERROR_PATTERNS))


def _is_operand_type_error(ex: TypeError) -> bool:
    """判断 TypeError 是否属于「操作数类型不匹配」（见上面的说明）。"""
    return bool(_OPERAND_TYPE_ERROR_RE.search(str(ex)))


def _build_type_mismatch_hint(check_value: Any, expect_value: Any) -> Text:
    """给「操作数类型不匹配」补一句可操作的建议（拼在失败信息末尾）。"""
    check_type = type(check_value).__name__
    expect_type = type(expect_value).__name__

    if isinstance(check_value, timedelta):
        return (
            "\nhint: `elapsed` 是 datetime.timedelta，不能直接与数值比较；"
            "耗时断言请用 elapsed_ms / elapsed_s"
            "（`type_match: [elapsed, timedelta]` 也不行——timedelta 不在 Python 内置名里）"
        )

    # 0917-1 新增：非 JSON 响应（XML/HTML/纯文本）的 body 是 bytes，字符串断言要断在 text 上
    if isinstance(check_value, (bytes, bytearray)) or isinstance(expect_value, (bytes, bytearray)):
        return (
            f"\nhint: 这里有一侧是 bytes（{check_type} vs {expect_type}）——非 JSON 响应的 `body` "
            "就是 bytes。字符串断言请把它作用在 **text** 上（`text` 是 requests 解好的字符串），"
            "例如 `contains: [\"text\", \"<ns1:code>0000</ns1:code>\"]`、"
            "`type_match: [\"text\", str]`；需要按结构断言请用 xpath_match 这类算子"
            "（见 docs/soap/README.md）。"
        )

    # 0918-8 / L18 新增：容器包含运算的「期望值不可哈希」——`contains: ["body.headers", ["a","b"]]`
    # 会在 `list in dict` 上抛 `unhashable type: 'list'`（检查项是 dict 时，
    # `in` 走的是**键**查找，而键必须可哈希）。
    if isinstance(check_value, dict) and isinstance(expect_value, (list, set, dict)):
        return (
            f"\nhint: 检查项是 **dict**（{check_type}），而 `contains` 的期望值是 "
            f"{expect_type} —— 对 dict 做 `in` 判断的是**键**，键必须可哈希，"
            "所以期望值不能写成列表/字典。\n"
            "  正确写法（三选一）：\n"
            "  1) 判单个键：`contains: [\"body.headers\", \"a\"]`；\n"
            "  2) 判键的个数：`contains: [\"length(body.headers)\", 2]`（JMESPath 取值后再比）；\n"
            "  3) 要判「都在」就拆成多条断言（一条一个键）。"
        )

    if check_type != expect_type:
        return (
            f"\nhint: check_value 与 expect_value 类型不一致（{check_type} vs {expect_type}）"
            f"——期望值要写成与响应字段同类型（YAML 里 `\"200\"` 是字符串、`200` 才是整数）"
        )

    return ""


# --------------------------------------------------------------------------- 取值未命中（0918-8 / H8）
def _leading_identifier(expr: Text) -> Text:
    """取表达式的前导标识符（`body[0].id` → `body`）；取不到（如 `[0].id`）返回空串。

    NOTICE（0918-8 / H6）：`ResponseObject._search_jmespath` 用它做前缀**精确**匹配——
    不能按 `.` 切第一段，那样会把合法的 `body[0].id`（首段带下标）判成非法表达式。
    """
    match = re.match(r"[A-Za-z_]\w*", expr)
    return match.group(0) if match else ""


def _is_path_expression(expr: Any) -> bool:
    """是不是「前缀 + 路径/下标」形式的取值表达式（裸前缀 `body` 不算）。"""
    if not isinstance(expr, Text):
        return False
    head = _leading_identifier(expr)
    return bool(head) and expr != head


def _assertion_expects_null(assert_method: Text, expect_value: Any) -> bool:
    """这条断言是不是**明确在期望 null**（明确期望 null 时不该报「取值未命中」）。

    覆盖两种写法：`eq: [body.x, null]`（expect 本身是 None）
    与 `type_match: [body.x, "None"]` / `"NoneType"`。
    """
    if expect_value is None:
        return True
    if assert_method == "type_match" and expect_value in ("None", "NoneType"):
        return True
    return False


def _ensure_extract_expression_is_text(key: Text, field: Any) -> None:
    """`extract` 的取值表达式必须是字符串，否则给**可读报错**（0920 批次 4 / N26）。

    NOTICE: 这个检查原本就存在（0917-1 加的），但它在 `"$" in field` **之后** ——
    于是 `extract: {n: 1}`（int/float/bool/None）在够到它之前就先抛了裸
    `TypeError: argument of type 'int' is not iterable`：消息里既没有 extract 名，
    也没有 step/用例信息。现在把它提成函数、在**两个位置**都调用：

    - 解析**前**：用户/Python API 直接给了非字符串；
    - 解析**后**：`${func()}` 的返回值不是字符串（dict/list 等，0917-1 的现场）。
    """
    if isinstance(field, Text):
        return

    raise exceptions.ParamsError(
        f"extract 的取值表达式必须是字符串，实际是 {type(field).__name__}: {field!r}\n"
        f"（extractor: {key}）\n"
        "hint: extract 只接受 jmespath 取值表达式（如 `body.code`）；"
        "`${func()}` 的返回值若是字符串，会被继续当作表达式解析；"
        "要「把 XML/非 JSON 响应里的值取出来」，请在 teardown_hooks（**复数**）里取值，"
        "见 docs/soap/README.md「XML 取值与多步链路」。"
    )


def _warn_value_not_found(where: Text, expr: Text, extra: Text = "") -> None:
    """取值未命中（None）时告警：**不**改变断言结论，只让「静默假通过」变得可见。

    NOTICE（0918-8 / H8）：修复前这里完全静默——`extract` 直接记下 None，
    `validate` 把它交给算子，于是 `not_equal: ["body.data.access_token", ""]` 这类
    「字段存在且非空」的断言在字段**根本不存在**时**通过**。
    为什么只告警、不报错：`None` 有两种来源（字段缺失 / 字段值真的是 null），
    而 jmespath 对两者都返回 None，框架**无法区分**——直接判失败会破坏
    `eq: [body.x, null]` 这类合法写法（那两种写法已在 `_assertion_expects_null` 里放过）。
    """
    logger.warning(
        f"{where}：取值表达式 {expr!r} 的结果是 None——字段不存在、下标越界，"
        f"或字段值就是 null（jmespath 对三者都返回 None，框架无法区分）。\n"
        f"  若这条断言本意是「字段存在且非空」，它可能因此被**静默满足**；"
        f"请核对字段名与响应结构（响应详情见同目录的 .run.log）。{extra}"
    )


def get_uniform_comparator(comparator: Text):
    """convert comparator alias to uniform name"""
    if comparator in ["eq", "equals", "equal"]:
        return "equal"
    elif comparator in ["lt", "less_than"]:
        return "less_than"
    elif comparator in ["le", "less_or_equals"]:
        return "less_or_equals"
    elif comparator in ["gt", "greater_than"]:
        return "greater_than"
    elif comparator in ["ge", "greater_or_equals"]:
        return "greater_or_equals"
    elif comparator in ["ne", "not_equal"]:
        return "not_equal"
    elif comparator in ["str_eq", "string_equals"]:
        return "string_equals"
    elif comparator in ["len_eq", "length_equal"]:
        return "length_equal"
    elif comparator in [
        "len_gt",
        "length_greater_than",
    ]:
        return "length_greater_than"
    elif comparator in [
        "len_ge",
        "length_greater_or_equals",
    ]:
        return "length_greater_or_equals"
    elif comparator in ["len_lt", "length_less_than"]:
        return "length_less_than"
    elif comparator in [
        "len_le",
        "length_less_or_equals",
    ]:
        return "length_less_or_equals"
    else:
        return comparator


def uniform_validator(validator):
    """unify validator

    Args:
        validator (dict): validator maybe in two formats:

            format1: this is kept for compatibility with the previous versions.
                {"check": "status_code", "comparator": "eq", "expect": 201, "message": "test"}
                {"check": "status_code", "assert": "eq", "expect": 201, "msg": "test"}
            format2: recommended new version, {assert: [check_item, expected_value, msg]}
                {'eq': ['status_code', 201, "test"]}

    Returns
        dict: validator info

            {
                "check": "status_code",
                "expect": 201,
                "assert": "equal",
                "message": "test
            }

    """
    if not isinstance(validator, dict):
        raise ParamsError(f"invalid validator: {validator}")

    if "check" in validator and "expect" in validator:
        # format1
        check_item = validator["check"]
        expect_value = validator["expect"]

        if "assert" in validator:
            comparator = validator.get("assert")
        else:
            comparator = validator.get("comparator", "eq")

        if "msg" in validator:
            message = validator.get("msg")
        else:
            message = validator.get("message", "")

    elif len(validator) == 1:
        # format2
        comparator = list(validator.keys())[0]
        compare_values = validator[comparator]

        if not isinstance(compare_values, list) or len(compare_values) not in [2, 3]:
            raise ParamsError(f"invalid validator: {validator}")

        check_item = compare_values[0]
        expect_value = compare_values[1]
        if len(compare_values) == 3:
            message = compare_values[2]
        else:
            # len(compare_values) == 2
            message = ""

    else:
        raise ParamsError(f"invalid validator: {validator}")

    # uniform comparator, e.g. lt => less_than, eq => equals
    assert_method = get_uniform_comparator(comparator)

    return {
        "check": check_item,
        "expect": expect_value,
        "assert": assert_method,
        "message": message,
    }


class ResponseObjectBase(object):
    def __init__(self, resp_obj, parser: Parser):
        """initialize with a response object

        Args:
            resp_obj (instance): requests.Response instance

        """
        self.resp_obj = resp_obj
        self.parser = parser
        self.validation_results: Dict = {}

    def extract(
        self,
        extractors: Dict[Text, Text],
        variables_mapping: VariablesMapping = None,
    ) -> Dict[Text, Any]:
        if not extractors:
            return {}

        extract_mapping = {}
        for key, field in extractors.items():
            # NOTICE（0920 批次 4 / **N26**）：类型兜底必须**在** `"$" in field` **之前**。
            #
            # 修复前顺序是反的：`"$" in field` 先执行，而 `extract: {n: 1}`
            # （int/float/bool/None —— Python API 或直接构造 `TStep.extract` 时可达）
            # 会抛**裸** `TypeError: argument of type 'int' is not iterable`：
            # 消息里没有 extract 名、没有 step 名、也没有用例名。
            # 而同一文件里 `validate` 的同类形态早就被 L1 收口成可读报错了
            # （`check` 位置写标量 → 可读的断言失败）—— 两处口径不一致。
            #
            # 两道检查都保留（位置不同、语义不同）：
            #   ① 解析**前**：用户/API 直接给了非字符串（N26 的现场）；
            #   ② 解析**后**（0917-1）：`${func()}` 返回了 dict/list 之类的非字符串。
            _ensure_extract_expression_is_text(key, field)
            if "$" in field:
                # field contains variable or function
                field = self.parser.parse_data(field, variables_mapping)
                # 0917-1：`${func()}` 的返回值若不是字符串（例如 dict/list），这里会一路走到
                # `expr.split(".", 1)` 抛 `'dict' object has no attribute 'split'`（原始 AttributeError）。
                _ensure_extract_expression_is_text(key, field)
            field_value = self._search_jmespath(field)
            # 0918-8 / H8：取值未命中（None）不能静默——后续步骤会拿到 None 而毫无提示
            if field_value is None and _is_path_expression(field):
                _warn_value_not_found(
                    f"extract 的 {key!r}",
                    field,
                    "  后续步骤引用该变量时会拿到 None。",
                )
            extract_mapping[key] = field_value

        # NOTICE（0919-3 / L36）：这条日志必须以**脱敏副本**打印。
        #
        # 它有两条泄漏链路：`.run.log`（sink 是硬编码 `level="DEBUG"`，与 `--log-level`
        # 无关，所以**每个用例都会无条件写进文件**）与 stdout（`--log-level` 控制）。
        # 而「登录后提取 token」恰恰是本框架最标准的用法，于是 token 明文直接落盘。
        # 更糟的是它紧跟在响应体后面，属于「响应侧唯一被漏掉的那条汇总」。
        #
        # 注意脱敏的是**展示副本**：下面 `return extract_mapping` 必须返回**真值**——
        # 它会被写进 `StepResult.export_vars`，再由
        # `self.__session_variables.update(step_result.export_vars)`（runner.py:440）
        # 参与后续步骤与被引用用例的变量传递。取成脱敏值会让下游拿到 `******`。
        logger.info(
            f"extract mapping: {utils.mask_sensitive_variables(extract_mapping)}"
        )
        return extract_mapping

    def _search_jmespath(self, expr: Text) -> Any:
        try:
            check_value = jmespath.search(expr, self.resp_obj)
        except JMESPathError as ex:
            logger.error(
                f"failed to search with jmespath\n"
                f"expression: {expr}\n"
                f"data: {self.resp_obj}\n"
                f"exception: {ex}"
            )
            raise
        return check_value

    def validate(
        self,
        validators: Validators,
        variables_mapping: VariablesMapping = None,
    ):

        variables_mapping = variables_mapping or {}

        self.validation_results = {}
        if not validators:
            return

        validate_pass = True
        failures = []

        for v in validators:

            if "validate_extractor" not in self.validation_results:
                self.validation_results["validate_extractor"] = []

            u_validator = uniform_validator(v)

            # check item
            check_item = u_validator["check"]
            # 0917-1：记录「check 位置的值被类型转换过」这一事实，失败时给出可操作提示。
            # `parse_string_value` 走 ast.literal_eval，`"0000"`（SOAP 成功码！）会被转成 int 0，
            # 失败信息因此变成看不懂的 `assert 0 equal 0000`。
            coerced_from = None
            # 用户**原文**（后面 `check_item` 会被替换结果覆盖，报错文案要拿得回原话）
            original_check_item = check_item
            # 批次 8 / **L1**：`check` 位置写成**字面量**（不可迭代的标量）时不能崩。
            # 修复前是裸 `if "$" in check_item:` —— `int` / `float` / `bool` / `None`
            # 直接抛 `TypeError: argument of type 'int' is not iterable`，而且是在
            # 下面那个 `try`（把比较类 TypeError 转成可读断言失败）**之前**，
            # 所以冒到 pytest 里是 **error 而不是 failed**，消息里既没有算子也没有检查项
            # （实测 `.tmp_report/probe_l1_l2.py`：`{"equal": [200, 200]}` → 本文件 363 行；
            # `{"equal": [200, 999]}` 修后变成一条**可读的失败结论**）。
            # 判据里保留**容器**类型，是为了**逐字保留**原语义：对 list/tuple/dict 而言
            # `"$" in x` 是"元素/键里有裸 `$`"，今天并不崩，行为一个字不改。
            if isinstance(check_item, (Text, list, tuple, dict)) and "$" in check_item:
                # check_item is variable or function
                parsed_check_item = self.parser.parse_data(check_item, variables_mapping)
                check_item = parse_string_value(parsed_check_item)
                if isinstance(parsed_check_item, str) and not isinstance(check_item, str):
                    coerced_from = parsed_check_item

            if check_item and isinstance(check_item, Text):
                try:
                    check_value = self._search_jmespath(check_item)
                except exceptions.ParamsError as ex:
                    # NOTICE（残留缺陷：报错里**没有用户写的那句话**）：
                    # 检查项里的 `$变量` 会先被替换成「变量的值」，再送去取值。
                    # 当变量是一个**对象**（`$response` / 存了 dict 的变量）而检查项又是
                    # 「变量 + 后缀」的写法（`$response.status_code`、`$request.url`）时，
                    # 替换出来的是 `<interfacetester.response.ResponseObject object at 0x1CDC3EAFED0>.status_code`
                    # 这种带内存地址的串，于是报错把地址当成「表达式」打给用户，
                    # 而他写的 `$response.status_code` 一个字都没出现——完全无法定位。
                    # 这里在「解析前后文本不同」时补一条点名报错（原表达式 + 替换结果 + 正确写法）。
                    if not isinstance(original_check_item, Text) or original_check_item == check_item:
                        raise
                    raise exceptions.ParamsError(
                        f"检查项 {original_check_item!r} 取不到值：`$变量` 先被替换成了它的值，"
                        f"替换后变成 {check_item!r}，已经不是合法的响应取值表达式。\n"
                        f"  根因: {ex}\n"
                        f"  响应取值请直接写字段路径（不要加 `$`）："
                        f"`status_code` / `body.data.id` / `headers.Content-Type` / `text`；"
                        f"`$response` / `$request` 只能在 setup_hooks / teardown_hooks 里用"
                        f"（那里传的是对象本身，可以在 debugtalk.py 里访问 `.status_code`、`.body`）。"
                    ) from None
            else:
                # variable or function evaluation result is "" or not text
                check_value = check_item

            # comparator
            assert_method = u_validator["assert"]
            # H3（0918-1）：算子解析**不允许回落到 Python 内置函数**。
            # 算子被调用为 `assert_func(check_value, expect_value, message)` 且返回值被忽略，
            # 于是名字撞上 `print` 这类可变参内置函数时旧实现会**静默通过**
            # （实测 `validate: - print: [...]` 判为 pass）——拼错的名字恰好等于某个内置名
            # 就会产生假通过。这里与生成期白名单 `make.ensure_known_comparators` 口径一致：
            # 只认「项目 debugtalk.py 函数」与「框架内置算子」两类。
            assert_func = self.parser.get_mapping_function(
                assert_method, allow_builtins=False
            )

            # expect item
            expect_item = u_validator["expect"]
            # parse expected value with config/teststep/extracted variables
            expect_value = self.parser.parse_data(expect_item, variables_mapping)

            # message
            message = u_validator["message"]
            # parse message with config/teststep/extracted variables
            message = self.parser.parse_data(message, variables_mapping)

            # 0918-8 / H8：check 位置取值未命中（None）时告警——不改变结论，只让假通过可见。
            # 明确「期望 null」的两种写法（`eq: [x, null]` / `type_match: [x, "None"]`）刻意放过。
            if (
                check_value is None
                and _is_path_expression(check_item)
                and not _assertion_expects_null(assert_method, expect_value)
            ):
                _warn_value_not_found(
                    f"{assert_method} 的 check 位置",
                    check_item,
                    f"  这条断言在「取值未命中」时可能被判为**通过**"
                    f"（如 `not_equal: [{check_item}, \"\"]`）。",
                )

            validate_msg = f"assert {check_item} {assert_method} {expect_value}({type(expect_value).__name__})"

            validator_dict = {
                "comparator": assert_method,
                "check": check_item,
                "check_value": check_value,
                "expect": expect_item,
                "expect_value": expect_value,
                "message": message,
            }

            # 失败信息上下文（断言失败与「操作数类型不匹配」两种失败共用）
            validator_context = (
                f"\n"
                f"check_item: {check_item}\n"
                f"check_value: {check_value}({type(check_value).__name__})\n"
                f"assert_method: {assert_method}\n"
                f"expect_value: {expect_value}({type(expect_value).__name__})"
            )

            # 0917-1：check 位置被类型转换过 → 失败时补一条 hint（否则信息是 `assert 0 equal 0000`）
            if coerced_from is not None:
                validator_context += (
                    f"\nhint: check 位置的值来自变量/函数，被 `parse_string_value` 转成了 "
                    f"{type(check_item).__name__}（原文 {coerced_from!r} → {check_item!r}）。"
                    f"要按字符串比较，请把它放在 **expect 侧**"
                    f"（如 contains: [\"text\", \"…{coerced_from}…\"]）或改用 xpath_match 这类算子。"
                )

            try:
                assert_func(check_value, expect_value, message)
                validate_msg += "\t==> pass"
                logger.info(validate_msg)
                validator_dict["check_result"] = "pass"
            except AssertionError as ex:
                validate_pass = False
                validator_dict["check_result"] = "fail"
                validate_msg += "\t==> fail"
                validate_msg += validator_context
                message = str(ex)
                if message:
                    validate_msg += f"\nmessage: {message}"

                logger.error(validate_msg)
                failures.append(validate_msg)
            except TypeError as ex:
                # P1-a：比较类 TypeError（两侧操作数类型不匹配）转成**可读的断言失败**；
                # 其它 TypeError 原样抛出——不能把它降级成「断言失败」，
                # 否则自定义算子/内置算子内部的真实 bug 会被掩盖（见 3.2 实现要点 3）。
                if not _is_operand_type_error(ex):
                    raise

                validate_pass = False
                validator_dict["check_result"] = "fail"
                validate_msg += "\t==> fail"
                validate_msg += validator_context
                validate_msg += f"\ntype_error: {type(ex).__name__}: {ex}"
                validate_msg += _build_type_mismatch_hint(check_value, expect_value)

                logger.error(validate_msg)
                failures.append(validate_msg)

            except ParamsError as ex:
                # NOTICE（0921-3 / **轻微项 2**）：算子自己抛的 `ParamsError` 是
                # **「用户误用」的显式标记**（`type_match: [body, "eval"]` 的
                # `ValueError` 原本会冒泡成 pytest 的 **error**，而"用户写错用法"按本仓
                # 既有划分应当记 **failed**）。现在统一转成可读断言失败。
                #
                # 为什么只认 `ParamsError`、**不**catch 裸 `ValueError`：
                # 这正是本条最初只捕获 `AssertionError`/`TypeError` 的原因 ——
                # 裸 `ValueError` 也可能是算子内部的**真 bug**（如解包错、索引错），
                # 一并吞掉会把 bug 伪装成"你的断言写错了"。
                # `ParamsError` 是**主动声明**"这是用法问题"的窄类型，转换它不会掩盖任何
                # 内部错误；算子要享受这个待遇，就必须显式抛出它（见 `comparators.get_type`）。
                validate_pass = False
                validator_dict["check_result"] = "fail"
                validate_msg += "\t==> fail"
                validate_msg += validator_context
                validate_msg += f"\nparams_error: {ex}"

                logger.error(validate_msg)
                failures.append(validate_msg)

            self.validation_results["validate_extractor"].append(validator_dict)

        if not validate_pass:
            failures_string = "\n".join([failure for failure in failures])
            raise ValidationFailure(failures_string)


class ResponseObject(ResponseObjectBase):
    def __getattr__(self, key):
        if key in ["json", "content", "body"]:
            try:
                value = self.resp_obj.json()
            except ValueError:
                value = self.resp_obj.content
        elif key == "cookies":
            value = self.resp_obj.cookies.get_dict()
        else:
            try:
                value = getattr(self.resp_obj, key)
            except AttributeError:
                err_msg = "ResponseObject does not have attribute: {}".format(key)
                logger.error(err_msg)
                raise exceptions.ParamsError(err_msg)

        self.__dict__[key] = value
        return value

    def _get_resp_meta_values(self) -> Dict[Text, Any]:
        """响应元信息（供检查项的前缀名直接使用，P1-a 新增）。

        NOTICE：
        - `elapsed_ms`/`elapsed_s`/`response_size` 是**数值**，可直接与内置数值算子比较；
          `reason` 是字符串（与 `status_code` 一起断，兼容网关自定义 reason）；
        - `elapsed` 的原始语义（`datetime.timedelta`）**保持不变**，不破坏已有用例；
        - `response_size` 是**解码后响应体的字节数**（`len(resp.content)`），与日志里的
          `response_length` 不一定相等——后者优先取 `Content-Length`（压缩前的传输长度）；
        - 所有取值都走 `getattr(self.resp_obj, ...)` 而不是 `self.xxx`：`ResponseObject.__getattr__`
          在属性不存在时会抛 `ParamsError`，而这里缺失时应当安静地给出 None/0。
        """
        elapsed = getattr(self.resp_obj, "elapsed", None)
        if isinstance(elapsed, timedelta):
            elapsed_s = elapsed.total_seconds()
        elif isinstance(elapsed, (int, float)):
            # 兜底：非 requests.Response 的对象可能直接给「秒数」
            elapsed_s = float(elapsed)
        else:
            elapsed_s = None

        content = getattr(self.resp_obj, "content", None)

        return {
            "elapsed_ms": None if elapsed_s is None else round(elapsed_s * 1000, 3),
            "elapsed_s": elapsed_s,
            "response_size": len(content) if isinstance(content, (bytes, bytearray)) else 0,
            "reason": getattr(self.resp_obj, "reason", None),
        }

    def _search_jmespath(self, expr: Text) -> Any:
        resp_obj_meta = {
            "status_code": self.status_code,
            "headers": self.headers,
            "cookies": self.cookies,
            "body": self.body,
            # 0917-1：`text` 提为一等公民（`requests.Response.text`，已解码的字符串）。
            # 非 JSON 响应（XML/HTML/纯文本）的 `body` 是 **bytes**，字符串算子直接作用在
            # `body` 上会抛 TypeError；断在 `text` 上即可（原先靠 hasattr 兜底也能用，
            # 但不出现在下面的 supported prefixes 里，用户根本不知道有这个前缀）。
            "text": self.text,
            **self._get_resp_meta_values(),
        }

        # NOTICE: 这里按「**前导标识符**」做**精确**匹配。
        # 修复前用的是 `expr.startswith(tuple(resp_obj_meta.keys()))`：`bodyx`、`status_code2`
        # 这类拼错的表达式会命中前缀 → 走 jmespath → 静默返回 None，
        # 最终表现为「None != 期望值」的莫名失败，而不是「表达式非法」。
        #
        # NOTICE（0918-8 / H6）：head 必须取**前导标识符**，不能按 `.` 切第一段。
        # 中间那一版写的是 `expr.split(".", 1)[0]`——它假设「第一段后面一定是 `.`」，
        # 于是凡是「首段后面直接跟下标/过滤/切片」的**合法 jmespath** 全被误判为非法：
        #   `body[0].id` → head `body[0]`、`body[*].id` → head `body[*]`、`body[0:2]` → head `body[0:2]`
        # 后果有两层：
        #   ① JSON 根是数组的接口（列表接口）**没有任何合法写法**能取元素；
        #   ② `compat._convert_jmespath` 恰恰会把 v2/v3 的 `content[0].x` 改写成 `body[0].x`
        #      （0918-7 / L2 的修复）——转换层刚改出来的表达式，运行期必然抛 ParamsError。
        #      即两处修复互相打架，而它们各自都有测试、没有任何测试把「转换 → 运行」连起来跑。
        # 现在：`re.match` 取前导标识符；取不到（表达式不以标识符开头，如 `[]`）时给空串，
        # 走下面同一条「表达式非法」的报错路径——`bodyx`/`status_code2` 的收益保持不变。
        # 回归见 tests/response_test.py::TestSearchJmespathHeadIsLeadingIdentifier
        # 与其中的 TestCompatRewriteIsAcceptedByRuntime（跨层不变量）。
        head_match = re.match(r"[A-Za-z_]\w*", expr)
        head = head_match.group(0) if head_match else ""
        if head not in resp_obj_meta:
            if hasattr(self.resp_obj, expr):
                return getattr(self.resp_obj, expr)
            # NOTICE: 这里不能返回 expr 本身，否则写错的提取路径（如 Body.code）会
            # 「自比较」——把表达式字符串当作取到的值参与断言，造成假通过或极难排查的假失败。
            err_msg = (
                f"invalid check/extract expression: {expr}\n"
                f"supported prefixes: {list(resp_obj_meta.keys())}\n"
                f"（也可以直接写 requests.Response 的属性名，如 elapsed / reason / url）"
            )
            logger.error(err_msg)
            raise exceptions.ParamsError(err_msg)

        # 0917-1 新增：`body.code` / `content.code` 在**非 JSON 响应**上是静默 None
        # （jmespath 无法在 bytes 上取子字段），表现为「None != 0000」这种查不出根因的失败。
        # 这里直接报错并给出可操作建议（JSON 响应不受影响：那时 body 是 dict/list）。
        # NOTICE（0918-8 / H6）：判据由「含 `.`」放宽为「**不是裸前缀**」——`body[0]` 同属
        # 「在非 JSON 响应体上按路径取值」，而修复前它在上面就被判成「表达式非法」，压根走不到
        # 这个提示；现在它合法了，就必须也能拿到这条可读的提示。
        if expr != head and head in ("body", "content") and isinstance(
            resp_obj_meta[head], (bytes, bytearray)
        ):
            err_msg = (
                f"无法在非 JSON 响应体上按路径取值：{expr}\n"
                f"原因：`{head}` 是 bytes（响应不是 JSON），jmespath 取子字段只会得到 None。\n"
                "建议：① XML/SOAP 用 xpath_match / xpath_count / soap_fault 等算子断言；"
                "② 想断字符串片段用 `text` 前缀（已解码），如 contains: [\"text\", \"<code>0000</code>\"]；"
                "③ 需要把值取出来给后续步骤用，请在 teardown_hooks（**复数**）里取值，"
                "见 docs/soap/README.md「XML 取值与多步链路」。"
            )
            logger.error(err_msg)
            raise exceptions.ParamsError(err_msg)

        try:
            check_value = jmespath.search(expr, resp_obj_meta)
        except JMESPathError as ex:
            logger.error(
                f"failed to search with jmespath\n"
                f"expression: {expr}\n"
                f"data: {resp_obj_meta}\n"
                f"exception: {ex}"
            )
            # NOTICE（0918-8 / M32）：**词法级**错误（`LexerError`，如名字里的 `-`
            # 被当成运算符）原先会**原样冒泡**——pytest 里表现为 error（不是 failed），
            # 且不看日志就看不出是哪条断言、哪个检查项。这类错误 100% 是表达式写法问题，
            # 转成带原文与改法的 ParamsError（与"前缀非法"那条路径口径一致）。
            if isinstance(ex, jmespath.exceptions.LexerError):
                raise exceptions.ParamsError(
                    f"无效的取值表达式：{expr}\n"
                    f"jmespath 报错：{ex}\n"
                    "hint: 字段名里有特殊字符（`-`、空格、点）时必须加双引号，"
                    '例如 `headers."X-Trace"`、`body."user-agent"`；'
                    "下标/过滤写法（`body[0].id`、`items[?id=='1']`）不需要加引号。"
                ) from ex
            raise

        return check_value


class ThriftResponseObject(ResponseObjectBase):
    pass


class SqlResponseObject(ResponseObjectBase):
    pass
