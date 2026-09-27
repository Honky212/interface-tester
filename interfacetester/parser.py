import ast
import builtins
import inspect
import os
import re
from typing import Any, Callable, Dict, List, Set, Text
from urllib.parse import urlparse

from loguru import logger

from interfacetester import exceptions, loader, utils
from interfacetester.models import FunctionsMapping, VariablesMapping

# use $$ to escape $ notation
dollar_regex_compile = re.compile(r"\$\$")
# variable notation, e.g. ${var} or $var
# variable should start with a-zA-Z_
variable_regex_compile = re.compile(r"\$\{([a-zA-Z_]\w*)\}|\$([a-zA-Z_]\w*)")
# function notation, e.g. ${func1($var_1, $var_3)}
function_regex_compile = re.compile(r"\$\{([a-zA-Z_]\w*)\(([\$\w\.\-/\s=,]*)\)\}")

# 2026-09-17 新增：识别「看起来像函数调用、但因为参数带引号而没被上面的正则匹配」的写法。
# 上面的参数字符集 `[\$\w\.\-/\s=,]` **不含引号**，于是 `${f('abc')}` 不会被当作函数调用，
# 而是退化成「变量替换 + 原样字符串」（`$` 后面的部分原封不动留下）——不报错，静默产出垃圾值。
# 这里只做告警（不报错）：字符串里字面量包含 `${...}` 的场景（例如断言模板接口的响应）不该被拦。
_function_with_quote_regex_compile = re.compile(r"\$\{[a-zA-Z_]\w*\([^}]*['\"]")

# 批次 C / L12：`$` 后面跟着一个**词**（`\w` 含中日韩等非 ASCII 字母）。
# 用途只有一个：把「用户确实定义了这个名字、但写法框架不支持」的形态找出来告警 ——
# 判据是**名字在变量表里**，因此 `价格 $100`、`token: $abc$def` 这类纯字面量不会被误报。
_dollar_word_regex_compile = re.compile(r"\$(\w+)")

# ===== 批次 9-0（M9）：`${...}` 插值允许回落的 Python 内置**正向白名单** =====
# 修复前 `allow_builtins=True` 那一支是 `getattr(builtins, name)`，等于把**全部**内置名
# 开放给 YAML/JSON 里的表达式。函数参数虽然不能带引号（见上面的参数字符集），
# 但**变量可以**，于是 `${eval($payload)}` 完全合法：
#   实测 `hconvert` 导入一份 Postman 集合（变量里放 payload、URL 里放 `${eval($p)}`）
#   → 生成 YAML（零告警）→ `hrun` 直接执行了 `os.system(...)`，用例还报 passed/exit 0。
# 取证与修复过程见 `docs/缺陷修复日志0918-9.md`。
#
# 白名单只放「纯计算 / 类型构造 / 序列工具」，明确排除五类：
#   ① 执行与导入：eval / exec / compile / __import__ / breakpoint
#   ② 文件与交互：open / input / help / exit / quit
#   ③ 内省与对象操纵：globals / locals / vars / dir / getattr / setattr / delattr /
#      type / object / super / memoryview / id
#   ④ 模块内务：__builtins__ / __loader__ / __spec__ / __build_class__ / __debug__
#   ⑤ 标准输出：print（在插值里只会把 `None` 写进结果字符串，是纯垃圾值）
# 需要白名单外的内置函数：写进项目 `debugtalk.py`（查找顺序里它**优先于**内置），
# 或显式设 `INTERFACETESTER_ALLOW_ALL_BUILTINS=1` 回到修复前行为。
ALLOWED_BUILTINS_IN_INTERPOLATION = frozenset(
    {
        "abs",
        "all",
        "any",
        "ascii",
        "bin",
        "bool",
        "bytearray",
        "bytes",
        "chr",
        "complex",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "format",
        "frozenset",
        "hash",
        "hex",
        "int",
        "isinstance",
        "issubclass",
        "iter",
        "len",
        "list",
        "map",
        "max",
        "min",
        "next",
        "oct",
        "ord",
        "pow",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
)

# 逃生口：白名单必然有遗漏，漏掉的用户需要一个**不用改框架**的出路——
# 但必须是显式的（默认关），不能默默放开。
ALLOW_ALL_BUILTINS_ENV = "INTERFACETESTER_ALLOW_ALL_BUILTINS"


def allow_all_builtins() -> bool:
    """逃生口是否打开：`INTERFACETESTER_ALLOW_ALL_BUILTINS=1/true/yes/on`。"""
    return os.environ.get(ALLOW_ALL_BUILTINS_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def parse_string_value(str_value: Text) -> Any:
    """parse string to number if possible
    e.g. "123" => 123
         "12.2" => 12.2
         "abc" => "abc"
         "$var" => "$var"

    NOTICE（残留缺陷：对**非字符串**求值 → 与用户写法毫无关系的报错）：
    函数名与用途都只针对字符串，但修复前它会把任何输入交给 `ast.literal_eval`。
    `literal_eval` 拿到「非 str、非 AST」对象时会在内部探测 `lineno` 之类的属性，
    而 `ResponseObject.__getattr__` 对不存在的属性**抛 ParamsError**（不是 AttributeError），
    于是 `validate: - eq: ["$response", "x"]` 报出来的是
    `ParamsError: ResponseObject does not have attribute: lineno`
    （日志里还多一条误导性的 ERROR），用户完全看不出自己写错在哪。
    现在：非字符串原样返回（对 str/bytes/int/dict/... 的可观察行为逐字不变——
    那些类型本来也是 `literal_eval` 抛 ValueError/SyntaxError 后原样返回）。
    """
    if not isinstance(str_value, Text):
        return str_value

    try:
        return ast.literal_eval(str_value)
    except ValueError:
        return str_value
    except SyntaxError:
        # e.g. $var, ${func}
        return str_value


def build_url(base_url, step_url):
    """prepend url with base_url unless it's already an absolute URL

    NOTICE（0920 批次 6 / **N38**）：`base_url` 自带的**查询串**必须保留。

    修复前只取 `o_base_url` 的 scheme/netloc/path，`query` **整段丢掉**：

    ```text
    base_url: http://host/api?sig=SECRETSIG   +   url: /final
      -> 实际请求 /api/final          ← sig 消失
      -> 用例照旧跑（若接口不校验则全绿）
    ```

    网关类鉴权参数写在 `base_url` 的查询串上是常见写法（`?sig=` / `?tenant=`），
    丢掉之后表现为"莫名的 401/404"，而 YAML 里明明写着。

    合并口径（与"step URL 自己的查询串优先"保持一致）：

    - `step_url` **带**查询串 → 以它为准（显式覆盖，符合直觉），
      `base_url` 的查询串**不**静默混进来；
    - `step_url` **不带**查询串 → 沿用 `base_url` 的（这样网关参数不会丢）。
    """
    o_step_url = urlparse(step_url)
    if o_step_url.netloc != "":
        # step url is absolute url
        return step_url

    # step url is relative, based on base url
    o_base_url = urlparse(base_url)
    if o_base_url.netloc == "":
        # missed base url
        raise exceptions.ParamsError("base url missed!")

    path = o_base_url.path.rstrip("/") + "/" + o_step_url.path.lstrip("/")
    # NOTICE（N38）：step URL 自带查询串时它优先；否则沿用 base_url 的。
    query = o_step_url.query or o_base_url.query
    o_step_url = (
        o_step_url._replace(scheme=o_base_url.scheme)
        ._replace(netloc=o_base_url.netloc)
        ._replace(path=path)
        ._replace(query=query)
    )
    return o_step_url.geturl()



def regex_findall_variables(raw_string: Text) -> List[Text]:
    """extract all variable names from content, which is in format $variable

    Args:
        raw_string (str): string content

    Returns:
        list: variables list extracted from string content

    Examples:
        >>> regex_findall_variables("$variable")
        ["variable"]

        >>> regex_findall_variables("/blog/$postid")
        ["postid"]

        >>> regex_findall_variables("/$var1/$var2")
        ["var1", "var2"]

        >>> regex_findall_variables("abc")
        []

    """
    try:
        match_start_position = raw_string.index("$", 0)
    except ValueError:
        return []

    vars_list = []
    while match_start_position < len(raw_string):

        # Notice: notation priority
        # $$ > $var

        # search $$
        dollar_match = dollar_regex_compile.match(raw_string, match_start_position)
        if dollar_match:
            match_start_position = dollar_match.end()
            continue

        # search variable like ${var} or $var
        var_match = variable_regex_compile.match(raw_string, match_start_position)
        if var_match:
            var_name = var_match.group(1) or var_match.group(2)
            vars_list.append(var_name)
            match_start_position = var_match.end()
            continue

        curr_position = match_start_position
        try:
            # find next $ location
            match_start_position = raw_string.index("$", curr_position + 1)
        except ValueError:
            # break while loop
            break

    return vars_list


def regex_findall_functions(content: Text) -> List[Text]:
    """extract all functions from string content, which are in format ${fun()}

    Args:
        content (str): string content

    Returns:
        list: functions list extracted from string content

    Examples:
        >>> regex_findall_functions("${func(5)}")
        ["func(5)"]

        >>> regex_findall_functions("${func(a=1, b=2)}")
        ["func(a=1, b=2)"]

        >>> regex_findall_functions("/api/1000?_t=${get_timestamp()}")
        ["get_timestamp()"]

        >>> regex_findall_functions("/api/${add(1, 2)}")
        ["add(1, 2)"]

        >>> regex_findall_functions("/api/${add(1, 2)}?_t=${get_timestamp()}")
        ["add(1, 2)", "get_timestamp()"]

    """
    try:
        return function_regex_compile.findall(content)
    except TypeError as ex:
        logger.error(f"regex findall functions error: {ex}")
        return []


def extract_variables(content: Any) -> Set:
    """extract all variables in content recursively."""
    if isinstance(content, (list, set, tuple)):
        variables = set()
        for item in content:
            variables = variables | extract_variables(item)
        return variables

    elif isinstance(content, dict):
        variables = set()
        for key, value in content.items():
            # NOTICE（0918-8 / L21）：**key 也要扫**。`parse_data` 对 dict 会
            # `parse_data(key, ...)`（键里的变量同样会被解析），而这里原先只看 value——
            # 依赖集合因此比实际解析的少一项：
            #   - 未定义的 key 变量漏过了 `not_defined_variables` 的提前报错，
            #     一路走到 `parse_data` 抛 VariableNotFound 被 `continue` 吞掉，
            #     最后统一报成"变量之间存在循环依赖"（**误报，且指不出真正缺的名字**）；
            #   - 已定义的 key 变量不影响收敛，但会让「引用自己」的检查漏判。
            # 扫 key 之后，依赖集合与 `parse_data` 真正解析的内容一一对应。
            variables = variables | extract_variables(key)
            variables = variables | extract_variables(value)
        return variables

    elif isinstance(content, str):
        return set(regex_findall_variables(content))

    return set()


def parse_function_params(params: Text) -> Dict:
    """parse function params to args and kwargs.

    Args:
        params (str): function param in string

    Returns:
        dict: function meta dict

            {
                "args": [],
                "kwargs": {}
            }

    Examples:
        >>> parse_function_params("")
        {'args': [], 'kwargs': {}}

        >>> parse_function_params("5")
        {'args': [5], 'kwargs': {}}

        >>> parse_function_params("1, 2")
        {'args': [1, 2], 'kwargs': {}}

        >>> parse_function_params("a=1, b=2")
        {'args': [], 'kwargs': {'a': 1, 'b': 2}}

        >>> parse_function_params("1, 2, a=3, b=4")
        {'args': [1, 2], 'kwargs': {'a':3, 'b':4}}

    """
    function_meta = {"args": [], "kwargs": {}}

    params_str = params.strip()
    if params_str == "":
        return function_meta

    args_list = params_str.split(",")
    for arg in args_list:
        arg = arg.strip()
        if "=" in arg:
            # NOTICE（0918-8 / L19）：必须 `split("=", 1)`。
            # 修复前用不带 maxsplit 的 `split("=")`，值里再出现 `=` 就直接炸解包：
            # `"${f(token=YWJj=)}"`（base64 值的尾部 `=` 是**常态**）→
            # `ValueError: too many values to unpack (expected 2)`，与调用点毫无关系的报错。
            # 现在按 Python 关键字参数的语义**只切第一个 `=`**：`k=a=1` → `{"k": "a=1"}`。
            # 只有「连名字都没有」（`=1`）才是真的写错，包成带原文的 ParamsError。
            key, value = arg.split("=", 1)
            if not key.strip():
                raise exceptions.ParamsError(
                    f"函数参数里的关键字参数缺少名字：{arg!r}\n"
                    f"完整参数列表：{params!r}\n"
                    "hint: 关键字参数要写成 `名字=值`（例如 `size=10`）；"
                    "值里可以包含 `=`（如 base64 的 `token=YWJj=`），只有第一个 `=` 起分隔作用。"
                )
            function_meta["kwargs"][key.strip()] = parse_string_value(value.strip())
        else:
            function_meta["args"].append(parse_string_value(arg))

    return function_meta


def get_mapping_variable(
    variable_name: Text, variables_mapping: VariablesMapping
) -> Any:
    """get variable from variables_mapping.

    Args:
        variable_name (str): variable name
        variables_mapping (dict): variables mapping

    Returns:
        mapping variable value.

    Raises:
        exceptions.VariableNotFound: variable is not found.

    """
    # TODO: get variable from debugtalk module and environ
    try:
        return variables_mapping[variable_name]
    except KeyError:
        raise exceptions.VariableNotFound(
            f"{variable_name} not found in {variables_mapping}"
        )


def get_mapping_function(
    function_name: Text,
    functions_mapping: FunctionsMapping,
    allow_builtins: bool = True,
) -> Callable:
    """get function from functions_mapping,
        if not found, then try to check if builtin function.

    Args:
        function_name (str): function name
        functions_mapping (dict): functions mapping
        allow_builtins (bool): 是否允许回落到 **Python 内置函数**。
            默认 True —— `$var` / `${func()}` 插值依赖它，`${len($x)}`、`${int($x)}`
            是文档化的能力（见 docs/能力清单.md 的「函数查找顺序」）。

            **但 True 现在只放开 `ALLOWED_BUILTINS_IN_INTERPOLATION` 这份正向白名单**
            （批次 9-0 / M9）：`eval` / `exec` / `compile` / `__import__` / `open` /
            `getattr` / `input` 这类一律拒绝，报错里给出两条出路
            （写进 debugtalk.py，或显式设 `INTERFACETESTER_ALLOW_ALL_BUILTINS=1`）。
            修复前是 `getattr(builtins, name)`——全部内置可达，`${eval($变量)}`
            即代码执行（实测可从导入的 Postman 集合一路走到 RCE，见
            docs/缺陷修复日志0918-9.md）。

            **断言算子分发必须传 False**（见 `ResponseObject.validate`）：算子被调用为
            `assert_func(check_value, expect_value, message)` 且**返回值被忽略**，
            于是名字撞上 `print` 这类可变参内置函数时会**静默通过**——
            实测 `validate: - print: ["status_code", 1]` 在旧实现下判为 pass，
            这是最难查的一类假通过（拼错的名字恰好等于某个内置名即可触发）。

    Returns:
        mapping function object.

    Raises:
        exceptions.FunctionNotFound: function is neither defined in debugtalk.py nor builtin.

    """
    if function_name in functions_mapping:
        return functions_mapping[function_name]

    elif function_name in ["parameterize", "P"]:
        return loader.load_csv_file

    elif function_name in ["environ", "ENV"]:
        return utils.get_os_environ

    elif function_name in ["multipart_encoder", "multipart_content_type"]:
        # extension for upload test
        from interfacetester.ext import uploader

        return getattr(uploader, function_name)

    try:
        # check if InterfaceTester builtin functions
        built_in_functions = loader.load_builtin_functions()
        return built_in_functions[function_name]
    except KeyError:
        pass

    if allow_builtins:
        # 批次 9-0（M9）：只放开白名单内的内置；逃生口打开时才回到「全部内置」。
        if function_name in ALLOWED_BUILTINS_IN_INTERPOLATION or allow_all_builtins():
            try:
                # check if Python builtin functions
                return getattr(builtins, function_name)
            except AttributeError:
                pass
        elif hasattr(builtins, function_name):
            # 名字确实是 Python 内置，但不在插值白名单里。
            # NOTICE: 报错必须同时说清「为什么不行」和「我要的写法怎么写」——
            # 只说 "is not found" 会让人以为是自己拼错了名字。
            raise exceptions.FunctionNotFound(
                f"{function_name!r} is a Python builtin, but it is not allowed in "
                f"`${{...}}` interpolation. Only "
                f"{len(ALLOWED_BUILTINS_IN_INTERPOLATION)} pure-calculation builtins are "
                f"whitelisted (len/int/str/float/bool/list/dict/set/tuple/sorted/sum/"
                f"min/max/abs/round/ord/chr/...), because `${{func()}}` executes whatever "
                f"it resolves to and the expression often comes from an imported artifact.\n"
                f"  what to do:\n"
                f"  1) write the logic as a function in the project `debugtalk.py` and call it "
                f"via `${{my_func($x)}}` (debugtalk functions win over builtins, so the name "
                f"you wanted is free for you to define);\n"
                f"  2) if you really need every Python builtin, set "
                f"{ALLOW_ALL_BUILTINS_ENV}=1 to restore the pre-0918-9 behaviour explicitly."
            )
    elif hasattr(builtins, function_name):
        # 名字确实是 Python 内置，但不允许在这一场景（断言算子）使用它。
        # NOTICE: 不能只说 "is not found"——那会让人以为是自己拼错了，
        # 而真实原因是「内置函数不能当算子用」，必须把这一点讲明。
        raise exceptions.FunctionNotFound(
            f"{function_name!r} is a Python builtin, but builtins are not allowed here. "
            f"Assert comparators must be framework built-ins or functions defined in "
            f"the project debugtalk.py, with signature "
            f'(check_value, expect_value, message="").'
        )

    raise exceptions.FunctionNotFound(f"{function_name} is not found.")


def parse_string(
    raw_string: Text,
    variables_mapping: VariablesMapping,
    functions_mapping: FunctionsMapping,
) -> Any:
    """parse string content with variables and functions mapping.

    Args:
        raw_string: raw string content to be parsed.
        variables_mapping: variables mapping.
        functions_mapping: functions mapping.

    Returns:
        str: parsed string content.

    Examples:
        >>> raw_string = "abc${add_one($num)}def"
        >>> variables_mapping = {"num": 3}
        >>> functions_mapping = {"add_one": lambda x: x + 1}
        >>> parse_string(raw_string, variables_mapping, functions_mapping)
            "abc4def"

    """
    try:
        match_start_position = raw_string.index("$", 0)
        parsed_string = raw_string[0:match_start_position]
    except ValueError:
        parsed_string = raw_string
        return parsed_string

    while match_start_position < len(raw_string):

        # Notice: notation priority
        # $$ > ${func($a, $b)} > $var

        # search $$
        dollar_match = dollar_regex_compile.match(raw_string, match_start_position)
        if dollar_match:
            match_start_position = dollar_match.end()
            parsed_string += "$"
            continue

        # search function like ${func($a, $b)}
        func_match = function_regex_compile.match(raw_string, match_start_position)
        if func_match:
            func_name = func_match.group(1)
            func = get_mapping_function(func_name, functions_mapping)

            func_params_str = func_match.group(2)
            function_meta = parse_function_params(func_params_str)
            args = function_meta["args"]
            kwargs = function_meta["kwargs"]
            parsed_args = parse_data(args, variables_mapping, functions_mapping)
            parsed_kwargs = parse_data(kwargs, variables_mapping, functions_mapping)

            try:
                func_eval_value = func(*parsed_args, **parsed_kwargs)
            except Exception as ex:
                logger.error(
                    f"call function error:\n"
                    f"func_name: {func_name}\n"
                    f"args: {parsed_args}\n"
                    f"kwargs: {parsed_kwargs}\n"
                    f"{type(ex).__name__}: {ex}"
                )
                raise

            func_raw_str = "${" + func_name + f"({func_params_str})" + "}"
            if func_raw_str == raw_string:
                # raw_string is a function, e.g. "${add_one(3)}", return its eval value directly
                return func_eval_value

            # raw_string contains one or many functions, e.g. "abc${add_one(3)}def"
            parsed_string += str(func_eval_value)
            match_start_position = func_match.end()
            continue

        # search variable like ${var} or $var
        var_match = variable_regex_compile.match(raw_string, match_start_position)
        if var_match:
            var_name = var_match.group(1) or var_match.group(2)
            var_value = get_mapping_variable(var_name, variables_mapping)

            if f"${var_name}" == raw_string or "${" + var_name + "}" == raw_string:
                # raw_string is a variable, $var or ${var}, return its value directly
                return var_value

            # raw_string contains one or many variables, e.g. "abc${var}def"
            parsed_string += str(var_value)
            match_start_position = var_match.end()
            continue

        curr_position = match_start_position
        try:
            # find next $ location
            match_start_position = raw_string.index("$", curr_position + 1)
            remain_string = raw_string[curr_position:match_start_position]
        except ValueError:
            remain_string = raw_string[curr_position:]
            # break while loop
            match_start_position = len(raw_string)

        # NOTICE（2026-09-17）：这段文本里若仍留着 `${`，说明它**没被解析成函数/变量**，
        # 会被原样写进结果——静默产出垃圾值最难查，所以这里显式告警。
        # 两种已知成因：① 函数参数带引号（上面的参数字符集不含引号）；
        # ② 嵌套调用（`${f(${g()})}` 不支持嵌套）。
        if "${" in remain_string:
            if _function_with_quote_regex_compile.search(remain_string):
                logger.warning(
                    f"检测到疑似「参数带引号的函数调用」，它不会被当作函数执行：\n"
                    f"  原文片段：{remain_string.strip()}\n"
                    f"原因：函数参数只允许出现 [$\\w\\.\\-/\\s=,]，**引号不在其中**——"
                    f"带引号的调用会被原样保留成字符串（不报错）。\n"
                    f"写法：常量用裸标识符（${{f(abc)}} 等价于 f('abc')），"
                    f"或先用变量（${{f($var)}}），或把常量写进 debugtalk.py 的函数体。"
                )
            else:
                logger.warning(
                    f"字符串里存在**未被解析**的 `${{...}}`，它会原样保留在结果里：\n"
                    f"  原文片段：{remain_string.strip()}\n"
                    f"支持的写法只有：`$var` / `${{var}}` / `${{func(...)}}`"
                    f"（函数参数不能带引号，也不支持嵌套 `${{f(${{g()}})}}`）。"
                )

        # NOTICE（批次 C / L12）：**`$` 后面跟非标识符开头的名字**（`$中文` / `$1abc`）
        # 不会被解析 —— `variable_regex_compile` 要求 `[a-zA-Z_]` 开头，于是它被**原样发出**。
        # 与上面两条告警的区别：`${{中文}}` 至少还会命中「含 `${{`」那条，**`$中文` 连告警都没有**。
        #
        # 判据刻意收紧成「**这个名字确实在变量表里**」：
        #   - 用户真的定义过它（`extract` 出来的中文名尤其自然）→ 他显然是想要变量值，
        #     却被静默当成字面量 → 告警；
        #   - 纯字面量（`价格 $100`、正文里的 `$abc`）不在变量表里 → **不告警**（零假报）。
        # 只告警不报错：字符串里字面量含 `$` 是合法用法（断言模板响应文本时很常见）。
        if "$" in remain_string:
            unsupported_names = sorted(
                {
                    matched.group(1)
                    for matched in _dollar_word_regex_compile.finditer(remain_string)
                    if matched.group(1) in (variables_mapping or {})
                }
            )
            if unsupported_names:
                logger.warning(
                    f"`$` 后面的变量名写法**不被支持**，它会被原样发出（不是变量值）：\n"
                    f"  原文片段：{remain_string.strip()}\n"
                    f"  涉及的名字：{unsupported_names}\n"
                    f"  原因：变量名必须以**字母或下划线**开头（`$中文` / `$1abc` 不匹配），"
                    f"你确实在变量表里定义过这些名字，但框架认不出来 —— 请求里发出去的是"
                    f"字面量 `$中文`，而不是它的值。\n"
                    f"  改法（二选一）：① 把变量名改成 ASCII（`$path` / `$code`）并同步改"
                    f"`extract` / `variables` 里的名字；② 若它本来就是字面量，忽略本条即可"
                    f"（也可以写成 `$$` 转义）。"
                )

        parsed_string += remain_string

    return parsed_string


def parse_data(
    raw_data: Any,
    variables_mapping: VariablesMapping = None,
    functions_mapping: FunctionsMapping = None,
) -> Any:
    """parse raw data with evaluated variables mapping.
    Notice: variables_mapping should not contain any variable or function.
    """
    if isinstance(raw_data, str):
        # content in string format may contains variables and functions
        variables_mapping = variables_mapping or {}
        functions_mapping = functions_mapping or {}
        # only strip whitespaces and tabs, \n\r is left because they maybe used in changeset
        #
        # NOTICE（0920 批次 6 / **N39**）：这个 strip 是**上游 httprunner 的既有行为**
        # （注释也写明只剥空格/制表符），本批**不改语义** —— 改它会影响所有既有用例。
        # 但它确实会**静默改写用户数据**：`params: {q: "  padded  "}` 发出去是
        # `q=padded`、口令/签名里带首尾空格时表现为"认证失败，可我 YAML 里写的明明是对的"。
        # 口径与 N35 一致：**不静默改写**做不到，就**让改写可见**。
        stripped = raw_data.strip(" \t")
        if stripped != raw_data:
            # 只在"确实被改短了、且剥掉的是有意义的空白"时告警。
            # `"\n".join(...)` 这类多行文本体**原样**保留（strip 不碰 \n\r），不误报。
            logger.warning(
                f"字符串值的**首尾空格/制表符**被剥掉了（parse_data 的既有行为）：\n"
                f"  原始: {raw_data!r}\n"
                f"  实际: {stripped!r}\n"
                f"  影响：发出去的**不是你在 YAML 里写的那个值**。"
                f"口令/签名/HMAC 输入/定宽文本体里带首尾空白时，"
                f"表现为「认证失败，但用例里写的值明明是对的」。\n"
                f"  改法：确需保留首尾空白时，用 `${...}` 从变量/函数取值"
                f"（变量值不会被剥），或把值放进 `data:` 的多行文本（`\\n` 不受影响）。"
            )
        return parse_string(stripped, variables_mapping, functions_mapping)

    elif isinstance(raw_data, (list, set, tuple)):
        return [
            parse_data(item, variables_mapping, functions_mapping) for item in raw_data
        ]

    elif isinstance(raw_data, dict):
        parsed_data = {}
        for key, value in raw_data.items():
            parsed_key = parse_data(key, variables_mapping, functions_mapping)
            parsed_value = parse_data(value, variables_mapping, functions_mapping)
            parsed_data[parsed_key] = parsed_value

        return parsed_data

    else:
        # other types, e.g. None, int, float, bool
        return raw_data


def parse_variables_mapping(
    variables_mapping: VariablesMapping, functions_mapping: FunctionsMapping = None
) -> VariablesMapping:

    parsed_variables: VariablesMapping = {}

    # NOTICE: 每成功解析一个变量就至少收敛一步，因此最多 len(variables_mapping) 轮即可全部解析完，
    # 这里再多给一轮用于「刚好全部解析完」的收尾判断。
    # 原实现是 while len(parsed) != len(mapping) 且没有轮数上限：当变量之间形成依赖环
    # （如 {"a": "$b", "b": "${f($a)}"}）时，两侧解析都抛 VariableNotFound 被 continue 吞掉，
    # len(parsed) 永远不变 → 死循环挂死进程。这里改为有限轮数 + 未收敛时显式报错。
    max_round = len(variables_mapping) + 1
    for _ in range(max_round):
        if len(parsed_variables) == len(variables_mapping):
            return parsed_variables

        for var_name in variables_mapping:

            if var_name in parsed_variables:
                continue

            var_value = variables_mapping[var_name]
            variables = extract_variables(var_value)

            # check if reference variable itself
            if var_name in variables:
                # e.g.
                # variables_mapping = {"token": "abc$token"}
                # variables_mapping = {"key": ["$key", 2]}
                raise exceptions.VariableNotFound(var_name)

            # check if reference variable not in variables_mapping
            not_defined_variables = [
                v_name for v_name in variables if v_name not in variables_mapping
            ]
            if not_defined_variables:
                # e.g. {"varA": "123$varB", "varB": "456$varC"}
                # e.g. {"varC": "${sum_two($a, $b)}"}
                raise exceptions.VariableNotFound(not_defined_variables)

            try:
                parsed_value = parse_data(
                    var_value, parsed_variables, functions_mapping
                )
            except exceptions.VariableNotFound:
                continue

            parsed_variables[var_name] = parsed_value

    unresolved_variables = [
        var_name for var_name in variables_mapping if var_name not in parsed_variables
    ]
    raise exceptions.ParamsError(
        f"failed to parse variables: {unresolved_variables}\n"
        f"variables_mapping: {variables_mapping}\n"
        "hint: 变量之间存在循环依赖，或依赖了无法解析的变量/函数"
    )


def parse_parameters(
    parameters: Dict,
) -> List[Dict]:
    """parse parameters and generate cartesian product.

    Args:
        parameters (Dict) parameters: parameter name and value mapping
            parameter value may be in three types:
                (1) data list, e.g. ["iOS/10.1", "iOS/10.2", "iOS/10.3"]
                (2) call built-in parameterize function, "${parameterize(account.csv)}"
                (3) call custom function in debugtalk.py, "${gen_app_version()}"

    Returns:
        list: cartesian product list

    Examples:
        >>> parameters = {
            "user_agent": ["iOS/10.1", "iOS/10.2", "iOS/10.3"],
            "username-password": "${parameterize(account.csv)}",
            "app_version": "${gen_app_version()}",
        }
        >>> parse_parameters(parameters)

    """
    parsed_parameters_list: List[List[Dict]] = []

    # load project_meta functions
    # NOTICE: Parameters 在生成代码的 import 期执行，原先固定用 os.getcwd() 定位项目根，
    # 用例不在 cwd 项目内时会加载到错误的 debugtalk 函数集；这里改为按调用方文件定位
    # （与 Config 的实现保持一致），定位失败再回退 cwd。
    try:
        caller_path = inspect.stack()[1].filename
    except (IndexError, ValueError) as ex:
        logger.warning(f"failed to locate caller path, fallback to cwd: {ex}")
        caller_path = os.getcwd()

    try:
        project_meta = loader.load_project_meta(caller_path)
    except exceptions.FileNotFound as ex:
        logger.warning(
            f"failed to load project meta from {caller_path}, fallback to cwd: {ex}"
        )
        project_meta = loader.load_project_meta(os.getcwd())
    functions_mapping = project_meta.functions

    for parameter_name, parameter_content in parameters.items():
        parameter_name_list = parameter_name.split("-")

        if isinstance(parameter_content, List):
            # (1) data list
            # e.g. {"app_version": ["2.8.5", "2.8.6"]}
            #       => [{"app_version": "2.8.5", "app_version": "2.8.6"}]
            # e.g. {"username-password": [["user1", "111111"], ["test2", "222222"]}
            #       => [{"username": "user1", "password": "111111"}, {"username": "user2", "password": "222222"}]
            parameter_content_list: List[Dict] = []
            for parameter_item in parameter_content:
                if not isinstance(parameter_item, (list, tuple)):
                    # "2.8.5" => ["2.8.5"]
                    parameter_item = [parameter_item]

                # ["app_version"], ["2.8.5"] => {"app_version": "2.8.5"}
                # ["username", "password"], ["user1", "111111"] => {"username": "user1", "password": "111111"}
                #
                # NOTICE（0918-8 / L20）：**长度必须校验**。`dict(zip(...))` 会把多余的值
                # 直接丢掉：`{"a": [[1, 2]]}` → `{"a": 1}`（2 静默消失），
                # 而参数化的语义是「给这些名字各取一个值」，静默截断出来的用例
                # 与用户写的**不是同一个用例**、却照样全绿。
                # 姐妹分支（`${func()}`，下面的 else-if）本来就有同样的校验，
                # 这里补齐，两个入口口径一致。
                if len(parameter_name_list) != len(parameter_item):
                    raise exceptions.ParamsError(
                        f"parameter names length are not equal to value length.\n"
                        f"parameter names: {parameter_name_list}\n"
                        f"parameter values: {parameter_item}\n"
                        f"hint: 参数名用 `-` 分隔（`username-password`），"
                        f"每个取值也要给出同样多个（`[\"user1\", \"111111\"]`）；"
                        f"若某个名字只有一个值，就不要写成列表。"
                    )
                parameter_content_dict = dict(zip(parameter_name_list, parameter_item))
                parameter_content_list.append(parameter_content_dict)

        elif isinstance(parameter_content, Text):
            # (2) & (3)
            parsed_parameter_content: List = parse_data(
                parameter_content, {}, functions_mapping
            )
            if not isinstance(parsed_parameter_content, List):
                raise exceptions.ParamsError(
                    f"parameters content should be in List type, got {parsed_parameter_content} for {parameter_content}"
                )

            parameter_content_list: List[Dict] = []
            for parameter_item in parsed_parameter_content:
                if isinstance(parameter_item, Dict):
                    # get subset by parameter name
                    # {"app_version": "${gen_app_version()}"}
                    # gen_app_version() => [{'app_version': '2.8.5'}, {'app_version': '2.8.6'}]
                    # {"username-password": "${get_account()}"}
                    # get_account() => [
                    #       {"username": "user1", "password": "111111"},
                    #       {"username": "user2", "password": "222222"}
                    # ]
                    # NOTICE（批次 B / M9 连带）：缺列时必须给可读报错，不能裸 `KeyError`。
                    #
                    # CSV 的三种常见形态都在这里汇合：带 BOM（列名成 `\ufeffusername`，已在
                    # `loader.load_csv_file` 用 `utf-8-sig` 修掉）、**列数不一致**（少一个逗号
                    # → 该行的名字缺失，见 `csv.DictReader` 的 restkey 行为），
                    # 以及 `${func()}` 返回的字典里没给全参数名。
                    missing_keys = [
                        key for key in parameter_name_list if key not in parameter_item
                    ]
                    if missing_keys:
                        raise exceptions.ParamsError(
                            f"参数化取值里缺少这些名字：{missing_keys}\n"
                            f"  参数名: {parameter_name_list}\n"
                            f"  这一组取值: {parameter_item}\n"
                            f"  常见原因：CSV 某一行**列数不一致**（少了一个逗号），"
                            f"或函数返回的字典没有覆盖全部参数名；"
                            f"CSV 带 UTF-8 BOM 会让第一列列名变成 `\\ufeff名字`"
                            f"（框架已按 `utf-8-sig` 读取，若仍出现请检查文件本身）。\n"
                            f"  改法：补齐该行的列，或修正参数名写法（多名字用 `-` 分隔）。"
                        )
                    parameter_dict: Dict = {
                        key: parameter_item[key] for key in parameter_name_list
                    }
                elif isinstance(parameter_item, (List, tuple)):
                    if len(parameter_name_list) == len(parameter_item):
                        # {"username-password": "${get_account()}"}
                        # get_account() => [("user1", "111111"), ("user2", "222222")]
                        parameter_dict = dict(zip(parameter_name_list, parameter_item))
                    else:
                        raise exceptions.ParamsError(
                            f"parameter names length are not equal to value length.\n"
                            f"parameter names: {parameter_name_list}\n"
                            f"parameter values: {parameter_item}"
                        )
                elif len(parameter_name_list) == 1:
                    # {"user_agent": "${get_user_agent()}"}
                    # get_user_agent() => ["iOS/10.1", "iOS/10.2"]
                    # parameter_dict will get: {"user_agent": "iOS/10.1", "user_agent": "iOS/10.2"}
                    parameter_dict = {parameter_name_list[0]: parameter_item}
                else:
                    raise exceptions.ParamsError(
                        f"Invalid parameter names and values:\n"
                        f"parameter names: {parameter_name_list}\n"
                        f"parameter values: {parameter_item}"
                    )

                parameter_content_list.append(parameter_dict)

        else:
            raise exceptions.ParamsError(
                f"parameter content should be List or Text(variables or functions call), got {parameter_content}"
            )

        # NOTICE（批次 A / H2）：**空数据集必须响亮报错**，不能交给 pytest 去静默 skip。
        #
        # 修复前的形态（实测 `.tmp_audit/verify_params.py`）：
        #   `parameters: {uid: []}`、只有表头的 CSV、`${func()}` 返回 `[]`
        #     → 这里返回 `[]` → 生成物里是 `@pytest.mark.parametrize("param", [])`
        #     → pytest 把空参数集判为 **skip**（不是 failed）→ `hrun` **exit 0**、零告警。
        #   「数据集为空」几乎总是环境/数据问题的征兆（查库为空、CSV 还没填、ENV 指错），
        #   而它被伪装成「跑过了、没问题」——正是本仓 §7 最忌讳的「绿了但什么都没测」。
        #
        # 与本仓既有口径的关系：`parse_parameters` 对**长度不匹配**早就是响亮报错（L20），
        # 唯独「长度 = 0」静默跳过 —— 两个口径自相矛盾，这里补齐。
        #
        # 为什么不能只告警：告警在 `-q` / CI 日志里看不见，假绿依旧存在。
        # 为什么不连带拦 `parameters={}`（真值假）：make 模板只在 `parameters` 为真时才生成
        # `@pytest.mark.parametrize`，也就是「没有参数化」根本走不到这里；
        # 这里只拦「**声明了**参数名、却一个取值都没解析出来」这一种可精确判定的形态。
        if not parameter_content_list:
            raise exceptions.ParamsError(
                f"参数化的数据集是**空的**：参数 {parameter_name!r} 一个取值都没有。\n"
                f"  参数名: {parameter_name}（取值名: {parameter_name_list}）\n"
                f"  取值来源: {parameter_content!r}\n"
                f"  为什么会这样：CSV **只有表头**/是空文件、`${{func()}}` 返回了 `[]`、"
                f"或参数列表本身就是 `[]`（例如 config 里写了 `parameters: {{uid: []}}`）。\n"
                f"  为什么要报错：pytest 对空参数集判 **skip**（不是 failed），"
                f"退出码是 0 —— 用例一步都不会跑，却看起来是绿的。\n"
                f"  改法：补齐数据（CSV 至少一行；函数不要返回空列表），"
                f"或先去掉这个参数化。\n"
                f"  NOTICE: 修复前这里返回空列表 → `@pytest.mark.parametrize(\"param\", [])` "
                f"→ 静默 skip、hrun exit 0、零告警。"
            )

        parsed_parameters_list.append(parameter_content_list)

    return utils.gen_cartesian_product(*parsed_parameters_list)


class Parser(object):
    def __init__(self, functions_mapping: FunctionsMapping = None) -> None:
        self.functions_mapping = functions_mapping

    def parse_string(
        self, raw_string: Text, variables_mapping: VariablesMapping
    ) -> Any:
        return parse_string(raw_string, variables_mapping, self.functions_mapping)

    def parse_variables(self, variables_mapping: VariablesMapping) -> VariablesMapping:
        return parse_variables_mapping(variables_mapping, self.functions_mapping)

    def parse_data(
        self, raw_data: Any, variables_mapping: VariablesMapping = None
    ) -> Any:
        return parse_data(raw_data, variables_mapping, self.functions_mapping)

    def get_mapping_function(
        self, func_name: Text, allow_builtins: bool = True
    ) -> Callable:
        return get_mapping_function(func_name, self.functions_mapping, allow_builtins)
