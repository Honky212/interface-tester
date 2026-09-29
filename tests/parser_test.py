import builtins
import os
import time
import unittest
from unittest import mock

from loguru import logger

from interfacetester import exceptions, loader, parser
from interfacetester.exceptions import FunctionNotFound, VariableNotFound
from interfacetester.loader import load_project_meta
from interfacetester.parser import get_mapping_function


def _capture_warnings(func, *args, **kwargs):
    """执行 func 并返回 loguru 捕获到的 WARNING 文本列表（与 tests/loader_test.py 同源手法）。"""
    messages = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        func(*args, **kwargs)
    finally:
        logger.remove(sink_id)
    return messages


class TestFunctionArgQuoting(unittest.TestCase):
    """0917-1：`${func(...)}` 的参数**不能带引号**——带了就整段不执行、静默留下字面量。

    根因：`parser.py` 的函数正则参数字符集是 `[$\\w\\.\\-/\\s=,]`，不含引号。
    修复方式：对「未被解析、原样保留的 `${...}`」给出 WARNING（不报错，避免误伤
    真的想断言字面量 `${...}` 的场景）。
    """

    def setUp(self):
        self.parser = parser.Parser(
            functions_mapping={"echo": lambda *args: f"echo{args}", "g": lambda: "G"}
        )

    def test_unquoted_identifier_is_a_string_literal(self):
        messages = _capture_warnings(self.parser.parse_data, "${echo(abc)}", {})
        self.assertEqual(self.parser.parse_data("${echo(abc)}", {}), "echo('abc',)")
        self.assertEqual(messages, [])

    def test_quoted_argument_is_not_executed_and_warns(self):
        messages = _capture_warnings(self.parser.parse_data, "${echo('abc')}", {})
        self.assertEqual(self.parser.parse_data("${echo('abc')}", {}), "${echo('abc')}")
        self.assertTrue(any("引号" in message for message in messages), messages)

    def test_double_quoted_argument_warns_too(self):
        messages = _capture_warnings(self.parser.parse_data, '${echo("abc")}', {})
        self.assertTrue(any("引号" in message for message in messages), messages)

    def test_nested_call_warns_with_generic_message(self):
        messages = _capture_warnings(self.parser.parse_data, "${echo(${g()})}", {})
        self.assertEqual(self.parser.parse_data("${echo(${g()})}", {}), "${echo(G)}")
        self.assertTrue(any("未被解析" in message for message in messages), messages)

    def test_escaped_dollar_does_not_warn(self):
        """`$$` 是转义写法，结果是字面量 `${echo('abc')}`，不该告警。"""
        messages = _capture_warnings(self.parser.parse_data, "$${echo('abc')}", {})
        self.assertEqual(self.parser.parse_data("$${echo('abc')}", {}), "${echo('abc')}")
        self.assertEqual(messages, [])

    def test_variable_argument_works(self):
        messages = _capture_warnings(self.parser.parse_data, "${echo($v)}", {"v": "V"})
        self.assertEqual(self.parser.parse_data("${echo($v)}", {"v": "V"}), "echo('V',)")
        self.assertEqual(messages, [])


class TestUnsupportedVariableNameWarning(unittest.TestCase):
    """批次 C / **L12**：`$中文` 这类**非标识符开头**的变量名不能被静默当成字面量。

    `variable_regex_compile` 要求变量名以 `[a-zA-Z_]` 开头，所以 `$中文` / `$1abc`
    **不会被解析**、原样留在结果里 —— 而它**连告警都没有**（`${中文}` 至少还会命中
    「含 `${`」那条告警，`$中文` 不会）。

    实测后果（`probe_e2e_regex_var.py`，真 CLI + 自测服务端）：用例里写
    `url: /api/$路径`（`路径` 是 `config.variables` 里定义过的），**服务端收到的是
    字面量 `/api/$路径`**，用例照旧 exit 0 —— 静默错值。

    NOTICE（判据收紧成「名字确实在变量表里」）：纯字面量（`价格 $100`、正文里的 `$abc`）
    **不该告警**（假报会让真报被无视），所以只有「用户真的定义过这个名字」才说。
    """

    @staticmethod
    def _warnings_for(raw_string, variables):
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            parser.Parser({}).parse_string(raw_string, variables)
        finally:
            logger.remove(sink_id)
        return messages

    def test_cjk_variable_name_is_reported(self):
        messages = self._warnings_for("/api/$路径", {"路径": "/ping"})

        self.assertEqual(len(messages), 1, messages)
        self.assertIn("路径", messages[0])
        self.assertIn("不被支持", messages[0])
        # 必须给出可操作改法（改成 ASCII 名）
        self.assertIn("ASCII", messages[0])

    def test_value_is_still_the_literal_behaviour_unchanged(self):
        """反向护栏：**行为一个字不改** —— 仍然是字面量（只加告警，不改替换规则）。

        改替换规则风险太大：`$` 在正文/断言模板里合法出现（甚至要先写 `$$` 转义）。
        """
        self.assertEqual(
            parser.Parser({}).parse_string("/api/$路径", {"路径": "/ping"}),
            "/api/$路径",
        )

    def test_digit_leading_variable_name_is_reported_too(self):
        messages = self._warnings_for("$1abc", {"1abc": "V"})

        self.assertEqual(len(messages), 1, messages)
        self.assertIn("1abc", messages[0])

    def test_plain_literal_dollar_is_not_warned(self):
        """反向护栏：纯字面量（**不在变量表里**）零告警。

        NOTICE: `$abc` 这种**标识符形状**的名字不在此列 —— 它会被当成变量引用，
        未定义时抛 `VariableNotFound`（响亮报错，不是静默字面量）。
        这里只放「正则本来就匹配不上、且用户也没定义过」的那些（`$100` / `$中文` / 裸 `$`）。
        """
        for raw, variables in (
            ("价格 $100", {}),
            ("$中文", {}),
            ("正文里的 $ 符号", {}),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self._warnings_for(raw, variables), [])

    def test_normal_variables_still_work(self):
        """反向护栏：合法变量照旧解析（且不告警）。"""
        self.assertEqual(
            parser.Parser({}).parse_string("/api/$path", {"path": "/ping"}),
            "/api//ping",
        )
        self.assertEqual(self._warnings_for("/api/$path", {"path": "/ping"}), [])


class TestBatch0918_8ParserFixes(unittest.TestCase):
    """批次 8-5（0918-8）：parser 的三项修复。

    - **L19**：`parse_function_params` 的 `arg.split("=")` 缺 `maxsplit`
      → base64 值（尾部 `=`）直接抛 `ValueError: too many values to unpack`；
    - **L20**：`parse_parameters` 的列表分支 `dict(zip(...))` **静默截断**；
    - **L21**：`extract_variables` 不扫字典 **key** → 键里的未定义变量被吞，
      最后误报成「变量之间存在循环依赖」。
    """

    # ---------------------------------------------------------------- L19
    def test_kwarg_value_may_contain_equals(self):
        """base64 值（`YWJj=`）与 `k=a=1` 都必须按「只切第一个 `=`」解析。"""
        self.assertEqual(
            parser.parse_function_params("token=YWJj="),
            {"args": [], "kwargs": {"token": "YWJj="}},
        )
        self.assertEqual(
            parser.parse_function_params("k=a=1"), {"args": [], "kwargs": {"k": "a=1"}}
        )

    def test_kwarg_parsing_unchanged_for_normal_cases(self):
        self.assertEqual(
            parser.parse_function_params("1, 2, a=3, b=4"),
            {"args": [1, 2], "kwargs": {"a": 3, "b": 4}},
        )
        self.assertEqual(parser.parse_function_params(""), {"args": [], "kwargs": {}})

    def test_kwarg_without_name_raises_actionable_error(self):
        """`=1` 是真的写错：报 ParamsError 并带上原文，而不是 ValueError。"""
        with self.assertRaises(exceptions.ParamsError) as cm:
            parser.parse_function_params("=1")

        message = str(cm.exception)
        self.assertIn("=1", message)
        self.assertIn("名字", message)

    def test_base64_kwarg_works_end_to_end(self):
        """端到端：`${f(token=YWJj=)}` 要真的执行（修复前是 ValueError）。"""
        received = {}

        def capture(token=None):
            received["token"] = token
            return "ok"

        p = parser.Parser(functions_mapping={"capture": capture})
        self.assertEqual(p.parse_data("${capture(token=YWJj=)}", {}), "ok")
        self.assertEqual(received["token"], "YWJj=")

    # ---------------------------------------------------------------- L20
    def test_parameter_list_length_mismatch_is_rejected(self):
        """`{"a": [[1, 2]]}` 修复前静默取到 `{"a": 1}`（2 消失）——必须报错。"""
        with self.assertRaises(exceptions.ParamsError) as cm:
            parser.parse_parameters({"a": [[1, 2]]})

        message = str(cm.exception)
        self.assertIn("parameter names", message)
        self.assertIn("a", message)

    def test_parameter_multi_name_still_works(self):
        self.assertEqual(
            parser.parse_parameters({"username-password": [["u1", "p1"], ["u2", "p2"]]}),
            [
                {"username": "u1", "password": "p1"},
                {"username": "u2", "password": "p2"},
            ],
        )

    def test_single_name_parameter_list_still_works(self):
        """回归：最常见的 `{"app_version": ["2.8.5", "2.8.6"]}` 不受长度校验影响。"""
        self.assertEqual(
            parser.parse_parameters({"app_version": ["2.8.5", "2.8.6"]}),
            [{"app_version": "2.8.5"}, {"app_version": "2.8.6"}],
        )

    def test_parameter_short_value_is_rejected(self):
        with self.assertRaises(exceptions.ParamsError):
            parser.parse_parameters({"a-b": [[1]]})

    # ---------------------------------------------------------------- 批次 A / H2
    def test_empty_parameter_list_is_rejected(self):
        """批次 A / H2：`parameters: {uid: []}` 修复前会让用例**静默 skip**、exit 0。

        pytest 对空参数集判 **skip**（不是 failed）——用例一步都不跑却看起来是绿的。
        这与本仓 L20「长度不匹配必须响亮报错」是同一处口径，唯独「长度 = 0」被漏掉了。
        """
        with self.assertRaises(exceptions.ParamsError) as cm:
            parser.parse_parameters({"uid": []})

        message = str(cm.exception)
        self.assertIn("uid", message)
        self.assertIn("空的", message)
        # 必须说清后果与改法（否则用户只看到「参数化报错」）
        self.assertIn("skip", message)

    def test_one_empty_parameter_among_others_is_rejected(self):
        """笛卡尔积里只要**任一**参数解析为空，用例就是「一步都不跑」，必须报错并点名它。"""
        with self.assertRaises(exceptions.ParamsError) as cm:
            parser.parse_parameters({"uid": [1, 2], "env": []})

        self.assertIn("env", str(cm.exception))

    def test_empty_parameter_set_from_csv_is_rejected(self):
        """函数/CSV 入口（`Text` 分支）解析出空集合时同样必须报错，并指出取值来源。

        两种「没数据」的形态各来一次：**只有表头**（现场最常见）与 **0 字节空文件**。
        手法与 `test_parse_parameters_testcase` 一致：临时项目 + `reset_project_meta()`
        + `load_project_meta(...)`（`parse_parameters` 在 tests/ 下定位不到项目标记，
        会沿用「当前已加载的 meta」）。结束时清掉，避免影响后续用例。
        """
        tmp_project = os.path.join(os.getcwd(), "logs", "tmp_empty_params_project")
        os.makedirs(tmp_project, exist_ok=True)
        with open(
            os.path.join(tmp_project, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")

        cases = (
            ("header_only.csv", "username,password\n"),
            ("zero_bytes.csv", ""),
        )

        try:
            for csv_name, content in cases:
                with self.subTest(csv=csv_name):
                    with open(
                        os.path.join(tmp_project, csv_name),
                        "w",
                        encoding="utf-8",
                        newline="",
                    ) as f:
                        f.write(content)

                    loader.reset_project_meta()
                    load_project_meta(tmp_project)

                    with self.assertRaises(exceptions.ParamsError) as cm:
                        parser.parse_parameters(
                            {"username": "${parameterize(%s)}" % csv_name}
                        )

                    message = str(cm.exception)
                    self.assertIn(csv_name, message)
                    self.assertIn("空的", message)
        finally:
            loader.reset_project_meta()

    def test_short_csv_header_gives_readable_error_not_keyerror(self):
        """批次 B / M9 连带：CSV 表头短于参数名时不得再抛裸 `KeyError`。

        修复前 `parse_parameters` 用 `parameter_item[key]` 裸取键 → `KeyError: 'password'`，
        报错里既没有参数名列表、也不说该补什么。现在给可读的 `ParamsError` 并点名缺哪些名字。
        （同一处也覆盖「CSV 带 BOM → 第一列列名被污染成 `\\ufeffusername`」那种形态：
        它的根因已在 `loader.load_csv_file` 用 `utf-8-sig` 修掉。）
        """
        tmp_project = os.path.join(os.getcwd(), "logs", "tmp_short_header_project")
        os.makedirs(tmp_project, exist_ok=True)
        with open(
            os.path.join(tmp_project, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")
        with open(
            os.path.join(tmp_project, "short.csv"), "w", encoding="utf-8", newline=""
        ) as f:
            f.write("username\nu1\n")  # 只有 username 一列

        try:
            loader.reset_project_meta()
            load_project_meta(tmp_project)

            with self.assertRaises(exceptions.ParamsError) as cm:
                parser.parse_parameters(
                    {"username-password": "${parameterize(short.csv)}"}
                )

            message = str(cm.exception)
            self.assertIn("password", message)
            self.assertIn("缺少这些名字", message)
            self.assertIn("列数不一致", message)  # 给出常见原因
        finally:
            loader.reset_project_meta()

    def test_no_parameters_still_returns_empty_list(self):
        """反向护栏：`parse_parameters({})` 表示「**没有**参数化」，不是「数据集为空」。

        make 模板只在 `parameters` 为真时才生成 `@pytest.mark.parametrize`，
        所以这条路径根本不会走到 pytest 的空参数集；不能连带把它变成报错。
        """
        self.assertEqual(parser.parse_parameters({}), [])

    def test_non_empty_parameters_are_unaffected(self):
        """反向护栏：非空数据集逐字不变（含多名字与函数来源）。"""
        self.assertEqual(
            parser.parse_parameters({"user_agent": ["iOS/10.1", "iOS/10.2"]}),
            [{"user_agent": "iOS/10.1"}, {"user_agent": "iOS/10.2"}],
        )

    # ---------------------------------------------------------------- L21
    def test_extract_variables_scans_dict_keys(self):
        self.assertEqual(parser.extract_variables({"$k": 1}), {"k"})
        self.assertEqual(parser.extract_variables({"a": {"$k": 1}}), {"k"})
        self.assertEqual(parser.extract_variables({"a": [{"$k": 1}]}), {"k"})

    def test_undefined_variable_in_dict_key_is_named(self):
        """修复前：键里的未定义变量被吞 → 误报「循环依赖」且指不出名字。"""
        with self.assertRaises(VariableNotFound) as cm:
            parser.parse_variables_mapping({"obj": {"$idx": 1}})

        self.assertIn("idx", str(cm.exception))

    def test_variable_in_dict_key_is_resolved(self):
        """已定义的键变量照常解析（`parse_data` 本来就解析键）。"""
        self.assertEqual(
            parser.parse_variables_mapping({"idx": "1", "obj": {"$idx": "x"}}),
            {"idx": "1", "obj": {"1": "x"}},
        )

    def test_self_reference_is_still_rejected(self):
        """回归：自引用与真环仍要报错（不能被这次扫描改动弄丢）。"""
        with self.assertRaises(VariableNotFound):
            parser.parse_variables_mapping({"token": "abc$token"})

        with self.assertRaises(Exception):
            parser.parse_variables_mapping({"a": "$b", "b": "$a"})


class TestParserBasic(unittest.TestCase):
    def test_build_url(self):
        url = parser.build_url("https://postman-echo.com", "/get")
        self.assertEqual(url, "https://postman-echo.com/get")
        url = parser.build_url("https://postman-echo.com", "get")
        self.assertEqual(url, "https://postman-echo.com/get")
        url = parser.build_url("https://postman-echo.com/", "/get")
        self.assertEqual(url, "https://postman-echo.com/get")

        url = parser.build_url("https://postman-echo.com/abc/", "/get?a=1&b=2")
        self.assertEqual(url, "https://postman-echo.com/abc/get?a=1&b=2")
        url = parser.build_url("https://postman-echo.com/abc/", "get?a=1&b=2")
        self.assertEqual(url, "https://postman-echo.com/abc/get?a=1&b=2")

        # NOTICE（0920 批次 6 / N38 订正）：这里原本注释是"omit query string in base url"，
        # 但那是**当时的错误行为**——base_url 的查询串是**被整段丢掉的**，
        # 而不是"step 自带查询串时以 step 为准"这个正当语义。
        # 现在两种情况分得很清楚（见下面的 `test_base_url_query_string_is_preserved`）。
        url = parser.build_url("https://postman-echo.com/abc?x=6&y=9", "/get?a=1&b=2")
        self.assertEqual(url, "https://postman-echo.com/abc/get?a=1&b=2")

        url = parser.build_url("", "https://postman-echo.com/get")
        self.assertEqual(url, "https://postman-echo.com/get")

        # notice: step request url > config base url
        url = parser.build_url("https://postman-echo.com", "https://httpbin.org/get")
        self.assertEqual(url, "https://httpbin.org/get")

    def test_base_url_query_string_is_preserved(self):
        """0920 批次 6 / **N38**：`base_url` 自带的查询串**不能丢**。

        修复前 `build_url` 只取 base_url 的 scheme/netloc/path，`query` 整段丢掉：

        ```text
        base_url: http://host/api?sig=SIG   +   url: /final
          -> 实际请求 /api/final          ← sig 消失，用例照旧跑
        ```

        网关类鉴权参数（`?sig=` / `?tenant=`）写在 base_url 上很常见，
        丢掉之后表现为"莫名的 401/404"，而 YAML 里明明写着。
        """
        # step 没有查询串 → 沿用 base_url 的（这正是修复前丢掉的那一段）
        self.assertEqual(
            parser.build_url("http://host/api?sig=SIG", "/final"),
            "http://host/api/final?sig=SIG",
        )
        # step 自带查询串 → 以 step 为准（显式覆盖，符合直觉）
        self.assertEqual(
            parser.build_url("http://host/api?sig=SIG", "/final?own=1"),
            "http://host/api/final?own=1",
        )

    def test_build_url_without_any_query_is_unchanged(self):
        """反向护栏：两边都没有查询串时逐字不变。"""
        self.assertEqual(
            parser.build_url("http://host/api", "/final"),
            "http://host/api/final",
        )

    def test_parse_data_strips_whitespace_but_warns(self):
        """0920 批次 6 / **N39**：首尾空白**照旧被剥**（不改既有语义），但必须**可见**。

        `strip(" \\t")` 是上游 httprunner 的既有行为，改它会影响所有既有用例；
        而它确实会静默改写用户数据（口令/签名里带首尾空格 → "认证失败，
        可我 YAML 里写的明明是对的"）。所以口径是**告警**：
        既不静默改写（做不到），也不让改写无声（做得到）。
        """
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            result = parser.parse_data("  padded  ", {}, {})
        finally:
            logger.remove(sink_id)

        # 行为不变
        self.assertEqual(result, "padded")
        # 但要说出来
        text = "\n".join(str(m) for m in messages)
        self.assertIn("首尾空格", text)
        self.assertIn("padded", text)

    def test_parse_data_does_not_warn_for_clean_values(self):
        """反向护栏：没有首尾空白时不许告警（噪音会让真告警被忽略）。"""
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            parser.parse_data("clean", {}, {})
            parser.parse_data({"a": "b"}, {}, {})
        finally:
            logger.remove(sink_id)

        self.assertEqual([m for m in messages if "首尾空格" in str(m)], [], messages)

    def test_parse_variables_mapping(self):
        variables = {"varA": "$varB", "varB": "$varC", "varC": "123", "a": 1, "b": 2}
        parsed_variables = parser.parse_variables_mapping(variables)
        print(parsed_variables)
        self.assertEqual(parsed_variables["varA"], "123")
        self.assertEqual(parsed_variables["varB"], "123")

    def test_parse_variables_mapping_exception(self):
        variables = {"varA": "$varB", "varB": "$varC", "a": 1, "b": 2}
        with self.assertRaises(VariableNotFound):
            parser.parse_variables_mapping(variables)

    def test_parse_string_value(self):
        self.assertEqual(parser.parse_string_value("123"), 123)
        self.assertEqual(parser.parse_string_value("12.3"), 12.3)
        self.assertEqual(parser.parse_string_value("a123"), "a123")
        self.assertEqual(parser.parse_string_value("$var"), "$var")
        self.assertEqual(parser.parse_string_value("${func}"), "${func}")

    def test_regex_findall_variables(self):
        self.assertEqual(parser.regex_findall_variables("$variable"), ["variable"])
        self.assertEqual(parser.regex_findall_variables("${variable}123"), ["variable"])
        self.assertEqual(parser.regex_findall_variables("/blog/$postid"), ["postid"])
        self.assertEqual(
            parser.regex_findall_variables("/$var1/$var2"), ["var1", "var2"]
        )
        self.assertEqual(parser.regex_findall_variables("abc"), [])
        self.assertEqual(parser.regex_findall_variables("Z:2>1*0*1+1$a"), ["a"])
        self.assertEqual(parser.regex_findall_variables("Z:2>1*0*1+1$$a"), [])
        self.assertEqual(parser.regex_findall_variables("Z:2>1*0*1+1$$$a"), ["a"])
        self.assertEqual(parser.regex_findall_variables("Z:2>1*0*1+1$$$$a"), [])
        self.assertEqual(parser.regex_findall_variables("Z:2>1*0*1+1$$a$b"), ["b"])
        self.assertEqual(parser.regex_findall_variables("Z:2>1*0*1+1$$a$$b"), [])
        # variable should not start with digit
        self.assertEqual(parser.regex_findall_variables("$1a"), [])
        self.assertEqual(parser.regex_findall_variables("${1a}"), [])

    def test_extract_variables(self):
        self.assertEqual(parser.extract_variables("$var"), {"var"})
        self.assertEqual(parser.extract_variables("$var123"), {"var123"})
        self.assertEqual(parser.extract_variables("$var_name"), {"var_name"})
        self.assertEqual(parser.extract_variables("var"), set())
        self.assertEqual(parser.extract_variables("a$var"), {"var"})
        self.assertEqual(parser.extract_variables("$v ar"), {"v"})
        self.assertEqual(parser.extract_variables(" "), set())
        self.assertEqual(parser.extract_variables("$abc*"), {"abc"})
        self.assertEqual(parser.extract_variables("${func()}"), set())
        self.assertEqual(parser.extract_variables("${func(1,2)}"), set())
        self.assertEqual(
            parser.extract_variables("${gen_md5($TOKEN, $data, $random)}"),
            {"TOKEN", "data", "random"},
        )
        self.assertEqual(parser.extract_variables("Z:2>1*0*1+1$$1"), set())

    def test_parse_function_params(self):
        self.assertEqual(parser.parse_function_params(""), {"args": [], "kwargs": {}})
        self.assertEqual(parser.parse_function_params("5"), {"args": [5], "kwargs": {}})
        self.assertEqual(
            parser.parse_function_params("1, 2"), {"args": [1, 2], "kwargs": {}}
        )
        self.assertEqual(
            parser.parse_function_params("a=1, b=2"),
            {"args": [], "kwargs": {"a": 1, "b": 2}},
        )
        self.assertEqual(
            parser.parse_function_params("a= 1, b =2"),
            {"args": [], "kwargs": {"a": 1, "b": 2}},
        )
        self.assertEqual(
            parser.parse_function_params("1, 2, a=3, b=4"),
            {"args": [1, 2], "kwargs": {"a": 3, "b": 4}},
        )
        self.assertEqual(
            parser.parse_function_params("$request, 123"),
            {"args": ["$request", 123], "kwargs": {}},
        )
        self.assertEqual(parser.parse_function_params("  "), {"args": [], "kwargs": {}})
        self.assertEqual(
            parser.parse_function_params("hello world, a=3, b=4"),
            {"args": ["hello world"], "kwargs": {"a": 3, "b": 4}},
        )
        self.assertEqual(
            parser.parse_function_params("$request, 12 3"),
            {"args": ["$request", "12 3"], "kwargs": {}},
        )

    def test_extract_functions(self):
        self.assertEqual(parser.regex_findall_functions("${func()}"), [("func", "")])
        self.assertEqual(parser.regex_findall_functions("${func(5)}"), [("func", "5")])
        self.assertEqual(
            parser.regex_findall_functions("${func(a=1, b=2)}"), [("func", "a=1, b=2")]
        )
        self.assertEqual(
            parser.regex_findall_functions("${func(1, $b, c=$x, d=4)}"),
            [("func", "1, $b, c=$x, d=4")],
        )
        self.assertEqual(
            parser.regex_findall_functions("/api/1000?_t=${get_timestamp()}"),
            [("get_timestamp", "")],
        )
        self.assertEqual(
            parser.regex_findall_functions("/api/${add(1, 2)}"), [("add", "1, 2")]
        )
        self.assertEqual(
            parser.regex_findall_functions("/api/${add(1, 2)}?_t=${get_timestamp()}"),
            [("add", "1, 2"), ("get_timestamp", "")],
        )
        self.assertEqual(
            parser.regex_findall_functions("abc${func(1, 2, a=3, b=4)}def"),
            [("func", "1, 2, a=3, b=4")],
        )

    def test_parse_data_string_with_variables(self):
        variables_mapping = {
            "var_1": "abc",
            "var_2": "def",
            "var_3": 123,
            "var_4": {"a": 1},
            "var_5": True,
            "var_6": None,
        }
        self.assertEqual(parser.parse_data("$var_1", variables_mapping), "abc")
        self.assertEqual(parser.parse_data("${var_1}", variables_mapping), "abc")
        self.assertEqual(parser.parse_data("var_1", variables_mapping), "var_1")
        self.assertEqual(parser.parse_data("$var_1#XYZ", variables_mapping), "abc#XYZ")
        self.assertEqual(
            parser.parse_data("${var_1}#XYZ", variables_mapping), "abc#XYZ"
        )
        self.assertEqual(
            parser.parse_data("/$var_1/$var_2/var3", variables_mapping), "/abc/def/var3"
        )
        self.assertEqual(parser.parse_data("$var_3", variables_mapping), 123)
        self.assertEqual(parser.parse_data("$var_4", variables_mapping), {"a": 1})
        self.assertEqual(parser.parse_data("$var_5", variables_mapping), True)
        self.assertEqual(parser.parse_data("abc$var_5", variables_mapping), "abcTrue")
        self.assertEqual(
            parser.parse_data("abc$var_4", variables_mapping), "abc{'a': 1}"
        )
        self.assertEqual(parser.parse_data("$var_6", variables_mapping), None)

        with self.assertRaises(VariableNotFound):
            parser.parse_data("/api/$SECRET_KEY", variables_mapping)

        self.assertEqual(
            parser.parse_data(["$var_1", "$var_2"], variables_mapping), ["abc", "def"]
        )
        self.assertEqual(
            parser.parse_data({"$var_1": "$var_2"}, variables_mapping), {"abc": "def"}
        )

        # format: $var
        value = parser.parse_data("ABC$var_1", variables_mapping)
        self.assertEqual(value, "ABCabc")

        value = parser.parse_data("ABC$var_1$var_3", variables_mapping)
        self.assertEqual(value, "ABCabc123")

        value = parser.parse_data("ABC$var_1/$var_3", variables_mapping)
        self.assertEqual(value, "ABCabc/123")

        value = parser.parse_data("ABC$var_1/", variables_mapping)
        self.assertEqual(value, "ABCabc/")

        value = parser.parse_data("ABC$var_1$", variables_mapping)
        self.assertEqual(value, "ABCabc$")

        value = parser.parse_data("ABC$var_1/123$var_1/456", variables_mapping)
        self.assertEqual(value, "ABCabc/123abc/456")

        value = parser.parse_data("ABC$var_1/$var_2/$var_1", variables_mapping)
        self.assertEqual(value, "ABCabc/def/abc")

        value = parser.parse_data("func1($var_1, $var_3)", variables_mapping)
        self.assertEqual(value, "func1(abc, 123)")

        # format: ${var}
        value = parser.parse_data("ABC${var_1}", variables_mapping)
        self.assertEqual(value, "ABCabc")

        value = parser.parse_data("ABC${var_1}${var_3}", variables_mapping)
        self.assertEqual(value, "ABCabc123")

        value = parser.parse_data("ABC${var_1}/${var_3}", variables_mapping)
        self.assertEqual(value, "ABCabc/123")

        value = parser.parse_data("ABC${var_1}/", variables_mapping)
        self.assertEqual(value, "ABCabc/")

        value = parser.parse_data("ABC${var_1}123", variables_mapping)
        self.assertEqual(value, "ABCabc123")

        value = parser.parse_data("ABC${var_1}/123${var_1}/456", variables_mapping)
        self.assertEqual(value, "ABCabc/123abc/456")

        value = parser.parse_data("ABC${var_1}/${var_2}/${var_1}", variables_mapping)
        self.assertEqual(value, "ABCabc/def/abc")

        value = parser.parse_data("func1(${var_1}, ${var_3})", variables_mapping)
        self.assertEqual(value, "func1(abc, 123)")

    def test_parse_data_multiple_identical_variables(self):
        variables_mapping = {
            "var_1": "abc",
            "var_2": "def",
        }
        self.assertEqual(
            parser.parse_data("/$var_1/$var_2/$var_1", variables_mapping),
            "/abc/def/abc",
        )

        variables_mapping = {"userid": 100, "data": 1498}
        content = "/users/$userid/training/$data?userId=$userid&data=$data"
        self.assertEqual(
            parser.parse_data(content, variables_mapping),
            "/users/100/training/1498?userId=100&data=1498",
        )

        variables_mapping = {"user": 100, "userid": 1000, "data": 1498}
        content = "/users/$user/$userid/$data?userId=$userid&data=$data"
        self.assertEqual(
            parser.parse_data(content, variables_mapping),
            "/users/100/1000/1498?userId=1000&data=1498",
        )

    def test_parse_data_string_with_functions(self):
        import random
        import string

        functions_mapping = {
            "gen_random_string": lambda str_len: "".join(
                random.choice(string.ascii_letters + string.digits)
                for _ in range(str_len)
            )
        }
        result = parser.parse_data(
            "${gen_random_string(5)}", functions_mapping=functions_mapping
        )
        self.assertEqual(len(result), 5)

        functions_mapping["add_two_nums"] = lambda a, b=1: a + b
        self.assertEqual(
            parser.parse_data(
                "${add_two_nums(1)}", functions_mapping=functions_mapping
            ),
            2,
        )
        self.assertEqual(
            parser.parse_data(
                "${add_two_nums(1, 2)}", functions_mapping=functions_mapping
            ),
            3,
        )
        self.assertEqual(
            parser.parse_data(
                "/api/${add_two_nums(1, 2)}", functions_mapping=functions_mapping
            ),
            "/api/3",
        )

        with self.assertRaises(FunctionNotFound):
            parser.parse_data("/api/${gen_md5(abc)}")

        variables_mapping = {
            "var_1": "abc",
            "var_2": "def",
            "var_3": 123,
            "var_4": {"a": 1},
            "var_5": True,
            "var_6": None,
        }
        functions_mapping = {"func1": lambda x, y: str(x) + str(y)}

        value = parser.parse_data(
            "${func1($var_1, $var_3)}", variables_mapping, functions_mapping
        )
        self.assertEqual(value, "abc123")

        value = parser.parse_data(
            "ABC${func1($var_1, $var_3)}DE", variables_mapping, functions_mapping
        )
        self.assertEqual(value, "ABCabc123DE")

        value = parser.parse_data(
            "ABC${func1($var_1, $var_3)}$var_5", variables_mapping, functions_mapping
        )
        self.assertEqual(value, "ABCabc123True")

        value = parser.parse_data(
            "ABC${func1($var_1, $var_3)}DE$var_4", variables_mapping, functions_mapping
        )
        self.assertEqual(value, "ABCabc123DE{'a': 1}")

        value = parser.parse_data(
            "ABC$var_5${func1($var_1, $var_3)}", variables_mapping, functions_mapping
        )
        self.assertEqual(value, "ABCTrueabc123")

        value = parser.parse_data(
            "ABC${ord(a)}DEF${len(abcd)}", variables_mapping, functions_mapping
        )
        self.assertEqual(value, "ABC97DEF4")

    def test_parse_data_func_var_duplicate(self):
        variables_mapping = {
            "var_1": "abc",
            "var_2": "def",
            "var_3": 123,
            "var_4": {"a": 1},
            "var_5": True,
            "var_6": None,
        }
        functions_mapping = {"func1": lambda x, y: str(x) + str(y)}
        value = parser.parse_data(
            "ABC${func1($var_1, $var_3)}--${func1($var_1, $var_3)}",
            variables_mapping,
            functions_mapping,
        )
        self.assertEqual(value, "ABCabc123--abc123")

        value = parser.parse_data(
            "ABC${func1($var_1, $var_3)}$var_1", variables_mapping, functions_mapping
        )
        self.assertEqual(value, "ABCabc123abc")

        value = parser.parse_data(
            "ABC${func1($var_1, $var_3)}$var_1--${func1($var_1, $var_3)}$var_1",
            variables_mapping,
            functions_mapping,
        )
        self.assertEqual(value, "ABCabc123abc--abc123abc")

    def test_parse_data_func_abnormal(self):
        variables_mapping = {
            "var_1": "abc",
            "var_2": "def",
            "var_3": 123,
            "var_4": {"a": 1},
            "var_5": True,
            "var_6": None,
        }
        functions_mapping = {"func1": lambda x, y: str(x) + str(y)}

        # {
        value = parser.parse_data("ABC$var_1{", variables_mapping, functions_mapping)
        self.assertEqual(value, "ABCabc{")

        value = parser.parse_data(
            "{ABC$var_1{}a}", variables_mapping, functions_mapping
        )
        self.assertEqual(value, "{ABCabc{}a}")

        value = parser.parse_data(
            "AB{C$var_1{}a}", variables_mapping, functions_mapping
        )
        self.assertEqual(value, "AB{Cabc{}a}")

        # }
        value = parser.parse_data("ABC$var_1}", variables_mapping, functions_mapping)
        self.assertEqual(value, "ABCabc}")

        # $$
        value = parser.parse_data("ABC$$var_1{", variables_mapping, functions_mapping)
        self.assertEqual(value, "ABC$var_1{")

        # $$$
        value = parser.parse_data("ABC$$$var_1{", variables_mapping, functions_mapping)
        self.assertEqual(value, "ABC$abc{")

        # $$$$
        value = parser.parse_data("ABC$$$$var_1{", variables_mapping, functions_mapping)
        self.assertEqual(value, "ABC$$var_1{")

        # ${
        value = parser.parse_data("ABC$var_1${", variables_mapping, functions_mapping)
        self.assertEqual(value, "ABCabc${")

        value = parser.parse_data("ABC$var_1${a", variables_mapping, functions_mapping)
        self.assertEqual(value, "ABCabc${a")

        # $}
        value = parser.parse_data("ABC$var_1$}a", variables_mapping, functions_mapping)
        self.assertEqual(value, "ABCabc$}a")

        # }{
        value = parser.parse_data("ABC$var_1}{a", variables_mapping, functions_mapping)
        self.assertEqual(value, "ABCabc}{a")

        # {}
        value = parser.parse_data("ABC$var_1{}a", variables_mapping, functions_mapping)
        self.assertEqual(value, "ABCabc{}a")

    def test_parse_data_request(self):
        content = {
            "request": {
                "url": "/api/users/$uid",
                "method": "$method",
                "headers": {"token": "$token"},
                "data": {
                    "null": None,
                    "true": True,
                    "false": False,
                    "empty_str": "",
                    "value": "abc${add_one(3)}def",
                },
            }
        }
        variables_mapping = {"uid": 1000, "method": "POST", "token": "abc123"}
        functions_mapping = {"add_one": lambda x: x + 1}
        result = parser.parse_data(content, variables_mapping, functions_mapping)
        self.assertEqual("/api/users/1000", result["request"]["url"])
        self.assertEqual("abc123", result["request"]["headers"]["token"])
        self.assertEqual("POST", result["request"]["method"])
        self.assertIsNone(result["request"]["data"]["null"])
        self.assertTrue(result["request"]["data"]["true"])
        self.assertFalse(result["request"]["data"]["false"])
        self.assertEqual("", result["request"]["data"]["empty_str"])
        self.assertEqual("abc4def", result["request"]["data"]["value"])

    def test_parse_data_testcase(self):
        variables = {
            "uid": "1000",
            "random": "A2dEx",
            "authorization": "a83de0ff8d2e896dbd8efb81ba14e17d",
            "data": {"name": "user", "password": "123456"},
        }
        functions = {
            "add_two_nums": lambda a, b=1: a + b,
            "get_timestamp": lambda: int(time.time() * 1000),
        }
        testcase_template = {
            "url": "http://127.0.0.1:5000/api/users/$uid/${add_two_nums(1,2)}",
            "method": "POST",
            "headers": {
                "Content-Type": "application/json",
                "authorization": "$authorization",
                "random": "$random",
                "sum": "${add_two_nums(1, 2)}",
            },
            "body": "$data",
        }
        parsed_testcase = parser.parse_data(testcase_template, variables, functions)
        self.assertEqual(
            parsed_testcase["url"], "http://127.0.0.1:5000/api/users/1000/3"
        )
        self.assertEqual(
            parsed_testcase["headers"]["authorization"], variables["authorization"]
        )
        self.assertEqual(parsed_testcase["headers"]["random"], variables["random"])
        self.assertEqual(parsed_testcase["body"], variables["data"])
        self.assertEqual(parsed_testcase["headers"]["sum"], 3)

    def test_parse_parameters_testcase(self):
        parameters = {
            "user_agent": ["iOS/10.1", "iOS/10.2"],
            "username-password": "${parameterize(request_methods/account.csv)}",
            "sum": "${calculate_two_nums(1, 2)}",
        }
        # NOTICE: 必须先清掉全局 project_meta。`load_project_meta` 有一条「已有 meta 且其
        # RootDir 覆盖当前路径就直接复用」的快路径（loader.py:536-570），所以本用例的结果
        # 取决于**之前**哪个用例加载过项目：文件级 `pytest tests` 恰好顺序合适，
        # 而 `python -m interfacetester run tests/`（make 后把 `_test.py` 列表交给 pytest，
        # 顺序不同）会让这里拿到别的项目的 functions_mapping，
        # `${calculate_two_nums(1, 2)}` 与 `${parameterize(...)}` 随之解析失败（P1-c 实测：
        # 同一套用例，`pytest tests` 全绿，而 `run tests/` 出现 2 个 failed，本用例就是其中之一）。
        # 手法与 tests/cli_test.py::TestCustomComparatorViaCli 一致。
        loader.reset_project_meta()
        load_project_meta(
            os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "examples",
                "postman_echo",
                "request_methods",
            ),
        )
        parsed_params = parser.parse_parameters(parameters)
        self.assertEqual(len(parsed_params), 2 * 3 * 2)

        self.assertIn(
            {
                "username": "test1",
                "password": "111111",
                "user_agent": "iOS/10.1",
                "sum": 3,
            },
            parsed_params,
        )
        self.assertIn(
            {
                "username": "test1",
                "password": "111111",
                "user_agent": "iOS/10.1",
                "sum": 1,
            },
            parsed_params,
        )
        self.assertIn(
            {
                "username": "test1",
                "password": "111111",
                "user_agent": "iOS/10.2",
                "sum": 3,
            },
            parsed_params,
        )
        self.assertIn(
            {
                "username": "test1",
                "password": "111111",
                "user_agent": "iOS/10.2",
                "sum": 1,
            },
            parsed_params,
        )
        self.assertIn(
            {
                "username": "test2",
                "password": "222222",
                "user_agent": "iOS/10.1",
                "sum": 3,
            },
            parsed_params,
        )
        self.assertIn(
            {
                "username": "test2",
                "password": "222222",
                "user_agent": "iOS/10.1",
                "sum": 1,
            },
            parsed_params,
        )
        self.assertIn(
            {
                "username": "test2",
                "password": "222222",
                "user_agent": "iOS/10.2",
                "sum": 3,
            },
            parsed_params,
        )
        self.assertIn(
            {
                "username": "test2",
                "password": "222222",
                "user_agent": "iOS/10.2",
                "sum": 1,
            },
            parsed_params,
        )


class TestGetMappingFunctionBuiltinsSwitch(unittest.TestCase):
    """0918-1（H3）：`get_mapping_function` 的 `allow_builtins` 开关。

    同一个查找函数被两种场景复用，两者的安全性要求**刚好相反**：

    - **变量/函数插值**（`${func()}`）需要 Python 内置函数：`${len($x)}`、`${int($x)}`
      是文档化的能力，所以默认 `allow_builtins=True`；
    - **断言算子分发**必须关掉内置回落：算子被调用为
      `assert_func(check_value, expect_value, message)` 且返回值被忽略，
      名字撞上 `print` 这类可变参内置函数会**静默通过**（假通过）。

    所以开关默认值必须是 True（保持既有能力），而 `ResponseObject.validate`
    显式传 False。这里把两侧都钉住。
    """

    def test_builtins_allowed_by_default(self):
        """默认行为不能变：插值场景照旧能用 Python 内置函数。"""
        self.assertIs(get_mapping_function("len", {}), len)
        self.assertIs(get_mapping_function("int", {}), int)
        self.assertIs(get_mapping_function("str", {}), str)

    def test_builtins_can_be_disabled(self):
        """关掉后，内置名一律拒绝（这是 H3 的核心）。"""
        for builtin_name in ("print", "len", "max", "id"):
            with self.subTest(func=builtin_name):
                with self.assertRaises(exceptions.FunctionNotFound) as ctx:
                    get_mapping_function(builtin_name, {}, allow_builtins=False)

                # 报错必须讲清「是内置函数不允许」而不是「找不到」
                self.assertIn("builtin", str(ctx.exception))

    def test_framework_builtins_survive_when_builtins_disabled(self):
        """关掉的是 **Python** 内置，框架内置（`builtin/` 里的算子与函数）必须仍然可用。"""
        for framework_name in ("equal", "contains", "jsonschema_match", "sleep"):
            with self.subTest(func=framework_name):
                self.assertTrue(callable(
                    get_mapping_function(framework_name, {}, allow_builtins=False)
                ))

    def test_debugtalk_function_takes_priority_over_builtin(self):
        """项目 debugtalk.py 的同名函数优先于 Python 内置（两条路径都要保持）。"""
        marker = lambda *args, **kwargs: None

        self.assertIs(
            get_mapping_function("print", {"print": marker}, allow_builtins=False), marker
        )
        self.assertIs(
            get_mapping_function("print", {"print": marker}, allow_builtins=True), marker
        )

    def test_unknown_name_still_raises_not_found(self):
        with self.assertRaises(exceptions.FunctionNotFound) as ctx:
            get_mapping_function("no_such_func_xyz", {}, allow_builtins=False)

        self.assertNotIn("builtin", str(ctx.exception))


class TestBatch0918_9InterpolationBuiltinsWhitelist(unittest.TestCase):
    """批次 9-0（M9）：`${...}` 插值的 Python 内置**正向白名单**。

    修复前插值路径是 `getattr(builtins, name)`——全部内置可达。函数参数不能带引号
    **挡不住**它：参数可以是变量，而变量可以来自 `config.variables`，也可以来自
    被导入的素材本身。实测（`docs/缺陷修复日志0918-9.md` §1）：

        Postman 集合（变量 `p` = payload、URL = `${eval($p)}`）
        → `hconvert` 零告警 → `hrun` **真的执行了** `os.system(...)`，用例还报 passed/exit 0。

    所以这一批的判据是两条，缺一不可：
    1. 文档化能力（`${len($x)}` / `${int($x)}`）与项目 `debugtalk.py` **不受影响**；
    2. 危险内置在插值路径**必须被拒绝**，且报错要给出两条出路（而不是 "not found"）。
    """

    # 修复前实测可达、现在必须被拒的内置名（每类取代表）
    DANGEROUS_BUILTINS = (
        # ① 执行与导入
        "eval",
        "exec",
        "compile",
        "__import__",
        "breakpoint",
        # ② 文件与交互
        "open",
        "input",
        "help",
        "exit",
        "quit",
        # ③ 内省与对象操纵
        "globals",
        "locals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "type",
        "id",
        "memoryview",
        # ④ 模块内务
        "__loader__",
        "__spec__",
        # ⑤ 标准输出（插值里只会得到 `None` 字符串，是垃圾值）
        "print",
    )

    # NOTICE（实测）：`__builtins__` **不是** `builtins` 模块的属性（它由导入机制注入到
    # 模块全局里），所以它走的是「名字找不到」那条分支而不是白名单分支——
    # 一样被拒，只是报错文案不同。单独钉住，避免以后有人"顺手"把它当属性处理。
    NON_ATTRIBUTE_BUILTIN_NAMES = ("__builtins__",)

    # 文档/仓库里真实在用的内置（`docs/能力清单.md` 的「函数查找顺序」）
    DOCUMENTED_BUILTINS = (
        "len",
        "int",
        "str",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "sorted",
        "sum",
        "min",
        "max",
        "abs",
        "round",
        "ord",
        "chr",
    )

    def test_documented_builtins_still_work(self):
        """判据 1：白名单必须覆盖文档与仓库里真实在用的内置（否则就是破坏兼容）。"""
        for name in self.DOCUMENTED_BUILTINS:
            with self.subTest(builtin=name):
                self.assertIs(get_mapping_function(name, {}), getattr(builtins, name))

        # 端到端（不是只看函数对象）：整体串、拼接场景都要照旧
        self.assertEqual(parser.parse_string("${len($x)}", {"x": "abcd"}, {}), 4)
        self.assertEqual(parser.parse_string("ab${len($x)}cd", {"x": "abcd"}, {}), "ab4cd")
        self.assertEqual(parser.parse_string("${int($n)}", {"n": "12"}, {}), 12)
        self.assertEqual(parser.parse_string("${ord($c)}", {"c": "a"}, {}), 97)

    def test_dangerous_builtins_are_refused_in_interpolation(self):
        """判据 2：修复前可达的内置名，现在在插值路径一律拒绝，且报错可操作。"""
        for name in self.DANGEROUS_BUILTINS:
            with self.subTest(builtin=name):
                with self.assertRaises(exceptions.FunctionNotFound) as ctx:
                    get_mapping_function(name, {})

                message = str(ctx.exception)
                # 必须讲清「是内置但被白名单拒绝」，而不是「名字找不到」
                self.assertIn("builtin", message)
                self.assertIn("whitelisted", message)
                self.assertIn("debugtalk.py", message)
                self.assertIn(parser.ALLOW_ALL_BUILTINS_ENV, message)

    def test_non_attribute_builtin_names_are_refused(self):
        """`__builtins__` 不是 `builtins` 模块的属性 → 走「找不到」分支，同样被拒。"""
        for name in self.NON_ATTRIBUTE_BUILTIN_NAMES:
            with self.subTest(builtin=name):
                with self.assertRaises(exceptions.FunctionNotFound):
                    get_mapping_function(name, {})
                with self.assertRaises(exceptions.FunctionNotFound):
                    parser.parse_string("${" + name + "($x)}", {"x": "1"}, {})

    def test_eval_exec_open_import_no_longer_execute(self):
        """端到端：这四条以前会**执行**，现在必须抛错（不是在结果里留个字符串）。"""
        cases = (
            ("${eval($p)}", {"p": "40+2"}),
            ("${exec($p)}", {"p": "x = 1"}),
            ("${__import__($m)}", {"m": "os"}),
            ("${compile($s,$f,$mode)}", {"s": "1+1", "f": "x", "mode": "eval"}),
            ("${getattr($o,$n)}", {"o": "abc", "n": "upper"}),
            ("${open($path)}", {"path": "pyproject.toml"}),
        )
        for raw, variables in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(exceptions.FunctionNotFound):
                    parser.parse_string(raw, dict(variables), {})

    def test_escape_hatch_restores_old_behaviour(self):
        """非空洞自检：打开逃生口后**同一个表达式必须恢复执行**。

        这条同时证明「上面的拒绝」来自白名单，而不是语法/夹具本身跑不到那一步。
        """
        payload = "40+2"
        with mock.patch.dict(
            os.environ, {parser.ALLOW_ALL_BUILTINS_ENV: "1"}, clear=False
        ):
            self.assertIs(get_mapping_function("eval", {}), builtins.eval)
            self.assertEqual(parser.parse_string("${eval($p)}", {"p": payload}, {}), 42)
            # 白名单外的其它内置也一并放开（回到修复前行为）
            self.assertIs(get_mapping_function("open", {}), builtins.open)

        # 关掉逃生口后立刻恢复拒绝（不能有进程级残留）
        with self.assertRaises(exceptions.FunctionNotFound):
            get_mapping_function("eval", {})

    def test_debugtalk_function_still_wins_over_builtin_name(self):
        """项目函数优先：即使名字与危险内置同名，`debugtalk.py` 里的实现照旧可用。"""
        marker = lambda *args, **kwargs: "mine"

        self.assertIs(get_mapping_function("eval", {"eval": marker}), marker)
        self.assertEqual(parser.parse_string("${eval($x)}", {"x": "1"}, {"eval": marker}), "mine")

    def test_whitelist_is_a_positive_list_without_dangerous_names(self):
        """防复发不变量：白名单是**正向列举**，且任何时候都不许把危险名加回去。"""
        whitelist = parser.ALLOWED_BUILTINS_IN_INTERPOLATION

        # ① 里面每个名字都必须真的存在（拼错的名字会让用户拿到 "not found"）
        for name in whitelist:
            with self.subTest(name=name):
                self.assertTrue(hasattr(builtins, name), f"{name} 不是 Python 内置")

        # ② 与危险集合完全不相交（将来有人手滑加回 eval，这条会红）
        overlap = whitelist.intersection(self.DANGEROUS_BUILTINS)
        self.assertEqual(overlap, set(), f"白名单里不允许出现这些名字：{sorted(overlap)}")

        # ③ 插值路径的拒绝集合 = 未列入白名单的内置（不是"另有一份黑名单"）
        self.assertNotIn("eval", whitelist)
        self.assertIn("len", whitelist)

    def test_comparator_path_message_is_unchanged(self):
        """`allow_builtins=False`（断言算子）那条路径的语义与文案不受本批次影响。"""
        with self.assertRaises(exceptions.FunctionNotFound) as ctx:
            get_mapping_function("len", {}, allow_builtins=False)

        message = str(ctx.exception)
        self.assertIn("Assert comparators", message)
        self.assertNotIn("whitelisted", message)

    def test_framework_builtins_unaffected_by_whitelist(self):
        """框架内置（`builtin/`）与 `${ENV()}` / `${parameterize()}` 走的不是内置回落分支。"""
        for name in ("sleep", "gen_random_string", "get_timestamp", "ENV", "environ", "P", "parameterize"):
            with self.subTest(name=name):
                self.assertTrue(callable(get_mapping_function(name, {})))


class TestBatch0919_27BuiltinLengthCoercion(unittest.TestCase):
    """批次 8 / **L2**：内置函数的"长度"参数必须接受**变量来的数字字符串**。

    ## 现场（实测，修复前，`.tmp_report/probe_l1_l2.py`）

    `.env` / CSV / `${ENV(...)}` 出来的值**永远是字符串**，而两个内置函数直接把它当 int 用：

    ```text
    ${gen_random_string($len)}  len="8"  → TypeError: 'str' object cannot be interpreted as an integer
    ${get_timestamp($len)}      len="8"  → ParamsError: timestamp length can only between 0 and 16.
    ${get_timestamp($len)}      len="16" → ParamsError: timestamp length can only between 0 and 16.
    ```

    第二条最坑：**16 明明合法**（判据是 `0 < n < 17`），文案却写 "between 0 and 16"，
    用户会去改长度，而真正的病因是"这个值是字符串"。
    框架别处已经确立"变量来的数字要转"（`utils.ensure_timeout_value`、
    `make.__coerce_scalar_fields_into`），只有这里漏网。

    ## 修法

    共用 `utils.ensure_int_value`（与 `ensure_timeout_value` 同一套口径）：
    收数字字符串（含首尾空白）与整数值 float；拒绝 `bool`、拒绝 `8.5` 这类**会被截断**的小数；
    越界时给出**实际值 + 真实区间**的可读报错。顺带把 `gen_random_string` 的负数
    从"静默返回空串"改成响亮报错（`0` 仍返回空串，既有行为不变）。
    """

    def setUp(self):
        # 空表 = "项目没有 debugtalk 函数"；框架内置由 load_builtin_functions 解析
        self.parser = parser.Parser({})

    def test_random_string_accepts_numeric_string_length(self):
        """**核心**：`${gen_random_string($len)}` 在 `len` 是字符串时必须能跑。"""
        for length in ("8", " 8 ", 8):
            with self.subTest(length=length):
                value = self.parser.parse_data(
                    "${gen_random_string($len)}", {"len": length}
                )

                self.assertEqual(len(value), 8)

    def test_timestamp_accepts_numeric_string_length(self):
        """**核心**：`"16"` 以前被拒（而 16 合法），现在必须可用。

        NOTICE: 必须**冻结时间**再断言长度 —— `str(time.time()).replace(".", "")` 的位数
        取决于这次浮点实际打印出几位小数（小数末位是 0 时 repr 会短一位），
        于是"请求 16 位"偶尔只能取到 15 位。第一版没冻结，**注入复跑时同一组注入
        两次跑出不同的红灯集合**，才发现是用例本身不稳定（不是修复不稳定）。
        """
        with mock.patch("time.time", return_value=1789822285.699777):
            for length in ("8", "16", " 10 "):
                with self.subTest(length=length):
                    value = self.parser.parse_data(
                        "${get_timestamp($len)}", {"len": length}
                    )

                    self.assertEqual(len(value), int(length))
                    self.assertTrue(value.isdigit(), value)

    def test_default_and_literal_calls_are_unchanged(self):
        """反向护栏：无参默认 13、字面量 int 调用都不变（同样要冻结时间，理由同上）。"""
        with mock.patch("time.time", return_value=1789822285.699777):
            self.assertEqual(len(self.parser.parse_data("${get_timestamp()}", {})), 13)
            self.assertEqual(
                len(self.parser.parse_data("${get_timestamp(10)}", {})), 10
            )
            self.assertEqual(
                len(self.parser.parse_data("${gen_random_string(4)}", {})), 4
            )

    def test_zero_length_still_returns_an_empty_string(self):
        """反向护栏：`0` 的既有行为（空串）不变 —— 本批只把**负数**改成响亮报错。"""
        self.assertEqual(
            self.parser.parse_data("${gen_random_string($len)}", {"len": "0"}), ""
        )

    def test_illegal_values_raise_with_the_actual_value_and_bounds(self):
        """失败面：每种非法形态都要说清"实际收到了什么"与"合法区间是多少"。"""
        cases = [
            (
                "${gen_random_string($len)}",
                {"len": "abc"},
                ["random string length", "'abc'"],
            ),
            (
                "${gen_random_string($len)}",
                {"len": -3},
                ["random string length", "-3", "不小于 0"],
            ),
            (
                "${get_timestamp($len)}",
                {"len": "20"},
                ["timestamp length", "20", "不大于 16"],
            ),
            (
                "${get_timestamp($len)}",
                {"len": "0"},
                ["timestamp length", "0", "不小于 1"],
            ),
            (
                "${get_timestamp($len)}",
                {"len": "true"},
                ["timestamp length", "'true'"],
            ),
            ("${get_timestamp($len)}", {"len": 8.5}, ["timestamp length", "8.5"]),
        ]
        for expression, variables, fragments in cases:
            with self.subTest(expression=expression, variables=variables):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    self.parser.parse_data(expression, variables)

                message = str(ctx.exception)
                for fragment in fragments:
                    self.assertIn(fragment, message)


class TestBatch0921SleepHardening(unittest.TestCase):
    """0921：`sleep()` 是批次 8 / L2 的**漏网之鱼**，同样必须收变量来的数字字符串。

    ## 现场（实测，修复前，`.tmp_audit/verify0921/`）

    `.env` / CSV / `${ENV(...)}` 出来的值**永远是字符串**，而 `sleep` 直接把它
    交给 `time.sleep`：

    ```text
    ${sleep($wait)}  wait='2'  → TypeError: 'str' object cannot be interpreted as an integer
    ${sleep($wait)}  wait=2    → 正常
    ```

    这正是 L2 想消灭的那类"报错看不出根因"：写的是"等待时间"，报的是 Python
    内部的 `TypeError`，用户会去查变量名而不是看"值是字符串"。

    ## 为什么修法与 L2 的两个兄弟函数**不同**（这是本组用例的重点）

    `gen_random_string` / `get_timestamp` 用的是 `ensure_int_value`，但 `sleep`
    **不能**照抄：`time.sleep` 本来就接受 **float**，而本仓有用例写 `${sleep(0.05)}`
    （`tests/save_tests_observability_test.py::test_failing_case_duration_is_not_zero`
    靠它造可测耗时）。`ensure_int_value` 会拒绝 `0.05`（"非整数的小数会被截断"），
    照字面口径改会把那条既有用例**直接弄红** —— 所以 `sleep` 用的是
    `ensure_timeout_value`（收 int/float/数字串、拒 bool）+ 负数检查。
    `test_float_seconds_still_accepted` 就是钉住这个差异的护栏。
    """

    def setUp(self):
        self.parser = parser.Parser({})

    def test_wait_from_variables_is_accepted(self):
        """**核心**：`${sleep($wait)}` 在 `wait` 是字符串（.env/CSV/ENV 形态）时必须能跑。"""
        for wait in ("0", "0.05", " 0 ", 0, 0.05):
            with self.subTest(wait=wait):
                # 不抛出即为通过（sleep 返回 None）
                self.assertIsNone(
                    self.parser.parse_data("${sleep($wait)}", {"wait": wait})
                )

    def test_float_seconds_still_accepted(self):
        """反向护栏：**float 必须继续可用**（这是 `sleep` 与 L2 兄弟函数的差异所在）。

        `ensure_int_value` 会拒绝 `0.05`，所以这里用 `ensure_timeout_value`。
        本仓 `tests/save_tests_observability_test.py` 依赖 `${sleep(0.05)}` 造耗时，
        若有人"顺手"把实现改回 `ensure_int_value`，本用例立刻变红。
        """
        self.assertIsNone(self.parser.parse_data("${sleep(0.05)}", {}))
        self.assertIsNone(self.parser.parse_data("${sleep($w)}", {"w": "0.05"}))

    def test_illegal_values_raise_a_readable_error(self):
        """失败面：非法值必须报 `ParamsError`（而不是裸 `TypeError`/`ValueError`）。

        报错里要能看出是「sleep 的秒数」写错了 —— 修复前是
        `TypeError: 'str' object cannot be interpreted as an integer`，
        完全看不出根因。
        """
        cases = [
            ("${sleep($w)}", {"w": "abc"}, ["sleep seconds", "'abc'"]),
            ("${sleep($w)}", {"w": "-1"}, ["sleep seconds", "不小于 0"]),
            ("${sleep($w)}", {"w": "true"}, ["sleep seconds", "'true'"]),
            ("${sleep($w)}", {"w": None}, ["sleep seconds"]),
        ]
        for expression, variables, fragments in cases:
            with self.subTest(variables=variables):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    self.parser.parse_data(expression, variables)

                message = str(ctx.exception)
                for fragment in fragments:
                    self.assertIn(fragment, message)


class TestBatch0921TimestampFixedWidth(unittest.TestCase):
    """0921：`get_timestamp()` 的**长度静默缩水**。

    ## 现场（实测，修复前，`.tmp_audit/verify0921/`）

    源串是 `str(time.time())` —— 浮点的 `repr`，**位数会浮动**。实测 40 万次采样：

    ```text
    17 位 74.0% / 16 位 24.4% / 15 位 1.6%
    ```

    小数末位为 0 时更短（`str(1789996932.0)` 只有 11 位）。于是
    `get_timestamp(16)` 会**静默返回更短的串**，实测真实墙钟抓到：

    ```text
    str(time.time()) = '1789996932.4137'  → get_timestamp(16) 只返回 14 位
    ```

    ## 为什么这不是"边界"而是抽风源

    `tests/parser_test.py` 的 L2 用例当年就踩到过（原文注释："第一版没冻结，注入复跑时
    同一组注入两次跑出不同的红灯集合"），当时靠 `mock.patch("time.time")` 把**用例**
    糊过去了，**根因一直留着**：任何依赖"固定长度时间戳"的用户用例（拼 ID、拼 length
    断言）都会以约 1~2% 的概率随机变红。

    ## 修法

    源串改成定宽的 `f"{time.time():.6f}"`（10 位整数秒 + 6 位微秒 = 恒 16 位），
    `[:length]` 因此对任何 `length <= 16` 都**保证**返回请求的长度。
    """

    def setUp(self):
        self.parser = parser.Parser({})

    def test_every_length_is_exact_even_when_repr_is_short(self):
        """**核心**：在"repr 很短"的真实时间点上，每个长度都必须**精确**。

        这三个时间点是本机墙钟实测到的真实值（修复前分别返回 14 / 11 / 11 位）。
        """
        short_repr_moments = [1789996932.4137, 1789996932.0, 1789996932.1]
        for moment in short_repr_moments:
            with self.subTest(moment=moment):
                with mock.patch("time.time", return_value=moment):
                    for length in range(1, 17):
                        value = self.parser.parse_data(
                            "${get_timestamp($len)}", {"len": length}
                        )
                        self.assertEqual(
                            len(value),
                            length,
                            f"时间戳 {moment} 下请求 {length} 位却拿到 {value!r}",
                        )
                        self.assertTrue(value.isdigit(), value)

    def test_old_source_string_really_was_short(self):
        """判据自检：证明上面那个时间点**确实**能暴露旧实现的缺陷。

        没有这条，`test_every_length_is_exact_even_when_repr_is_short` 可能在
        "源串本来就够长"的假前提下空过（护栏自己骗自己）。
        """
        self.assertEqual(len(str(1789996932.4137).replace(".", "")), 14)
        self.assertEqual(len(str(1789996932.0).replace(".", "")), 11)
        # 修复前的表达式在这两个时间点上分别只能取到 14 / 11 位
        self.assertLess(len(str(1789996932.4137).replace(".", "")[:16]), 16)
        self.assertLess(len(str(1789996932.0).replace(".", "")[:16]), 16)

    def test_limit_is_still_sixteen(self):
        """反向护栏：长度上限仍是 16（本次只改"源串定宽"，不动区间口径）。"""
        for expression, variables in (
            ("${get_timestamp($len)}", {"len": "17"}),
            ("${get_timestamp(17)}", {}),
            ("${get_timestamp(0)}", {}),
        ):
            with self.subTest(expression=expression, variables=variables):
                with self.assertRaises(exceptions.ParamsError):
                    self.parser.parse_data(expression, variables)

    def test_numeric_string_length_still_coerced(self):
        """反向护栏：L2 的"变量来的数字字符串要转"口径不变。"""
        for length in ("8", " 10 ", 8):
            with self.subTest(length=length):
                value = self.parser.parse_data(
                    "${get_timestamp($len)}", {"len": length}
                )
                self.assertEqual(len(value), int(length))
                self.assertTrue(value.isdigit(), value)
