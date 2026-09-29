import json
import unittest

import requests
from loguru import logger

from interfacetester import compat
from interfacetester.exceptions import FunctionNotFound, ParamsError, ValidationFailure
from interfacetester.parser import Parser
from interfacetester.response import ResponseObject, uniform_validator
from interfacetester.utils import HTTP_BIN_URL


class TestBatch0920ExtractScalarShape(unittest.TestCase):
    """0920 批次 4 / **N26**：`extract` 的取值写成标量时必须给可读报错。

    修复前 `extract` 里第一句就是 `if "$" in field:`，而类型检查
    （`isinstance(field, Text)`，0917-1 加的）在它**之后**。于是 `extract: {n: 1}`
    （int/float/bool/None —— Python API / 直接构造 `TStep.extract` 时可达）抛的是

    ```text
    TypeError: argument of type 'int' is not iterable
    ```

    —— 消息里没有 extract 名、没有 step 名、没有用例名。
    而同一文件里 `validate` 的同类形态早就被 L1 收口成可读报错了
    （`check` 位置写标量 → 可读的断言失败），两处口径不一致。
    """

    def setUp(self):
        resp = requests.post(f"{HTTP_BIN_URL}/anything", json={"ok": True})
        self.resp_obj = ResponseObject(resp, Parser())

    def test_scalar_extract_expression_raises_readable_error(self):
        for value in (1, 1.5, True, None):
            with self.subTest(value=value):
                with self.assertRaises(ParamsError) as ctx:
                    self.resp_obj.extract({"got": value}, {})

                message = str(ctx.exception)
                self.assertIn("必须是字符串", message)
                self.assertIn("got", message, "报错要点名是哪一个 extract")
                self.assertIn(type(value).__name__, message)

    def test_container_extract_expression_also_readable(self):
        """容器形态本来就走得到那条检查 —— 反向护栏：别在重构里弄丢它。"""
        for value in ({"a": 1}, ["x"]):
            with self.subTest(value=value):
                with self.assertRaises(ParamsError) as ctx:
                    self.resp_obj.extract({"got": value}, {})
                self.assertIn("必须是字符串", str(ctx.exception))

    def test_valid_string_expression_still_works(self):
        """反向护栏：正常的 jmespath 表达式照旧。"""
        mapping = self.resp_obj.extract({"flag": "body.json.ok"}, {})

        self.assertEqual(mapping, {"flag": True})


class TestResponse(unittest.TestCase):
    """响应解析/断言用例：请求打向本地 mock 的 ``/anything``（见 tests/mock_server.py）。

    NOTICE: mock 会把 POST 的 json body 原样回显到响应的 ``json`` 字段，
    因此这些用例不再依赖 docker httpbin 或外网。
    """

    def setUp(self) -> None:
        resp = requests.post(
            f"{HTTP_BIN_URL}/anything",
            json={
                "locations": [
                    {"name": "Seattle", "state": "WA"},
                    {"name": "New York", "state": "NY"},
                    {"name": "Bellevue", "state": "WA"},
                    {"name": "Olympia", "state": "WA"},
                ]
            },
        )
        parser = Parser(
            functions_mapping={"get_name": lambda: "name", "get_num": lambda x: x}
        )
        self.resp_obj = ResponseObject(resp, parser)

    def test_extract(self):
        variables_mapping = {"body": "body"}
        extract_mapping = self.resp_obj.extract(
            {
                "var_1": "body.json.locations[0]",
                "var_2": "body.json.locations[3].name",
                "var_3": "$body.json.locations[3].name",
                "var_4": "$body.json.locations[3].${get_name()}",
            },
            variables_mapping=variables_mapping,
        )
        self.assertEqual(extract_mapping["var_1"], {"name": "Seattle", "state": "WA"})
        self.assertEqual(extract_mapping["var_2"], "Olympia")
        self.assertEqual(extract_mapping["var_3"], "Olympia")
        self.assertEqual(extract_mapping["var_4"], "Olympia")

    def test_validate(self):
        self.resp_obj.validate(
            [
                {"eq": ["body.json.locations[0].name", "Seattle"]},
                {"eq": ["body.json.locations[0]", {"name": "Seattle", "state": "WA"}]},
            ],
        )

    def test_validate_variables(self):
        variables_mapping = {"index": 1, "var_empty": ""}
        self.resp_obj.validate(
            [
                {"eq": ["body.json.locations[$index].name", "New York"]},
                {"eq": ["$var_empty", ""]},
            ],
            variables_mapping=variables_mapping,
        )

    def test_validate_functions(self):
        variables_mapping = {"index": 1}
        self.resp_obj.validate(
            [
                {"eq": ["${get_num(0)}", 0]},
                {"eq": ["${get_num($index)}", 1]},
            ],
            variables_mapping=variables_mapping,
        )

    def test_uniform_validator(self):
        validators = [
            {
                "check": "status_code",
                "comparator": "eq",
                "expect": 201,
                "message": "test",
            },
            {"check": "status_code", "assert": "eq", "expect": 201, "msg": "test"},
            {"eq": ["status_code", 201, "test"]},
        ]
        expected = {
            "check": "status_code",
            "assert": "equal",
            "expect": 201,
            "message": "test",
        }
        for validator in validators:
            self.assertEqual(uniform_validator(validator), expected)


class TestCustomComparator(unittest.TestCase):
    """自定义断言算子：debugtalk.py 里定义的函数可以直接当比较方式用。

    NOTICE: `ResponseObject.validate` 用 `parser.get_mapping_function(assert_method)`
    查断言函数，而该查找顺序是 debugtalk → 框架内置 → Python 内置；`uniform_validator`
    对未知比较方式也会原样返回。因此「JSON Schema 校验」「XML 结构校验」这类
    框架没内置的断言，可以在 debugtalk.py 里实现后用 `validate:` 调用，无需改框架核心。
    """

    @staticmethod
    def _build(parser_functions, response_json):
        resp = requests.post(f"{HTTP_BIN_URL}/anything", json=response_json)
        parser = Parser(functions_mapping=parser_functions)
        return ResponseObject(resp, parser)

    def test_custom_comparator_passes(self):
        received = {}

        def has_keys(check_value, expect_value, message=""):
            received["check_value"] = check_value
            assert set(expect_value) <= set(check_value), message or "缺少字段"

        resp_obj = self._build({"has_keys": has_keys}, {"alpha": 1, "beta": 2})

        # 形状与内置比较器完全一致：{比较方式: [检查项, 期望值, 可选消息]}
        resp_obj.validate([{"has_keys": ["body.json", ["alpha", "beta"]]}])

        self.assertEqual(received["check_value"], {"alpha": 1, "beta": 2})
        self.assertEqual(
            resp_obj.validation_results["validate_extractor"][0]["check_result"],
            "pass",
        )

    def test_custom_comparator_failure_raises_validation_failure(self):
        def has_keys(check_value, expect_value, message=""):
            assert set(expect_value) <= set(check_value), message or "缺少字段"

        resp_obj = self._build({"has_keys": has_keys}, {"alpha": 1})

        with self.assertRaises(ValidationFailure):
            resp_obj.validate([{"has_keys": ["body.json", ["alpha", "beta"]]}])

    # ===== 0918-1（H3）：算子分发**不得**回落到 Python 内置函数 =====

    def test_python_builtin_name_is_rejected_and_never_passes(self):
        """H3 运行期护栏：内置名当算子必须报错，**绝不能静默判 pass**。

        修复前 `validate: - print: [...]` 会解析到 Python 的 `print`，被调用为
        `print(check_value, expect_value, message)`——`print` 恰好可变参，
        于是不抛异常、断言判 **pass**（还顺手往 stdout 打了日志）。
        即「算子名拼错成某个内置名」= 静默假通过。

        NOTICE: 这条护栏覆盖的正是**不走 hmake** 的场景（例如手写的
        `examples/data_management/sql/04_sql_data_management_test.py`）：
        `.assert_xxx()` 的动态分发只把算子名塞进 `step.validators`，
        真正的函数解析发生在运行期，所以生成期白名单拦不住它。
        """
        resp_obj = self._build({}, {"alpha": 1})

        with self.assertRaises(FunctionNotFound) as ctx:
            resp_obj.validate([{"print": ["body.json", 1]}])

        self.assertIn("builtin", str(ctx.exception))
        # 最关键的一条：绝不能出现「判为通过」的结果
        self.assertNotEqual(
            resp_obj.validation_results.get("validate_extractor", []),
            [{"check_result": "pass"}],
        )
        for record in resp_obj.validation_results.get("validate_extractor", []):
            self.assertNotEqual(record.get("check_result"), "pass")

    def test_debugtalk_comparator_named_like_builtin_still_works(self):
        """反向护栏：项目在 debugtalk.py 里显式定义的同名算子必须仍然生效。

        收紧的范围是「**回落到 Python 内置**」这条路，不是「禁止用这些名字」——
        重名函数由项目自己负责，框架不该拦。
        """
        def print(check_value, expect_value, message=""):  # noqa: A001 - 故意重名
            assert check_value == expect_value, message or "not equal"

        resp_obj = self._build({"print": print}, {"alpha": 1})

        resp_obj.validate([{"print": ["body.json.alpha", 1]}])

        self.assertEqual(
            resp_obj.validation_results["validate_extractor"][0]["check_result"], "pass"
        )


class TestResponseMetaAssertions(unittest.TestCase):
    """P1-a：响应元信息检查项（`elapsed_ms` / `elapsed_s` / `response_size` / `reason`）。

    NOTICE: 修复前这三个新名字**根本不被识别**（会落到 `hasattr(resp_obj, ...)` 分支并抛
    `ParamsError: invalid check/extract expression`），而 `elapsed` 虽然能取到值
    （`datetime.timedelta`），却不能与任何内置数值算子一起用（`TypeError`）——
    于是「接口变慢」这类回归完全没有自动化手段。
    """

    def setUp(self) -> None:
        self.raw_resp = requests.get(f"{HTTP_BIN_URL}/get")
        self.parser = Parser(functions_mapping={})
        self.resp_obj = ResponseObject(self.raw_resp, self.parser)

    def _check_values(self) -> dict:
        return {
            item["check"]: item["check_value"]
            for item in self.resp_obj.validation_results["validate_extractor"]
        }

    def test_elapsed_ms_and_elapsed_s_are_numeric_and_consistent(self):
        self.resp_obj.validate(
            [
                {"greater_than": ["elapsed_ms", 0]},
                {"less_than": ["elapsed_ms", 60000]},
                {"less_than": ["elapsed_s", 60]},
            ]
        )

        values = self._check_values()
        self.assertIsInstance(values["elapsed_ms"], float)
        self.assertIsInstance(values["elapsed_s"], float)
        self.assertAlmostEqual(
            values["elapsed_ms"], values["elapsed_s"] * 1000, places=3
        )
        self.assertAlmostEqual(
            values["elapsed_s"], self.raw_resp.elapsed.total_seconds(), places=6
        )

    def test_elapsed_ms_supports_fast_and_slow_thresholds(self):
        # 慢：阈值取 -1（任何非负耗时都必然超）→ 失败
        with self.assertRaises(ValidationFailure) as ctx:
            self.resp_obj.validate([{"less_than": ["elapsed_ms", -1]}])
        self.assertIn("elapsed_ms", str(ctx.exception))

        # 快：阈值放大到 60s → 通过
        self.resp_obj.validate([{"less_than": ["elapsed_ms", 60000]}])

    def test_response_size_matches_decoded_body_bytes(self):
        expected = len(self.raw_resp.content)

        self.resp_obj.validate(
            [
                {"equal": ["response_size", expected]},
                {"greater_than": ["response_size", 0]},
            ]
        )

        self.assertEqual(self._check_values()["response_size"], expected)

    def test_reason_is_a_string_check_item(self):
        self.resp_obj.validate(
            [
                {"equal": ["reason", "OK"]},
                {"string_equals": ["reason", self.raw_resp.reason]},
            ]
        )

        self.assertEqual(self._check_values()["reason"], "OK")

    def test_elapsed_keeps_timedelta_semantics(self):
        """`elapsed` 的原始语义（`timedelta`）必须保留：不破坏已有用例。"""
        self.assertEqual(
            self.resp_obj._search_jmespath("elapsed"), self.raw_resp.elapsed
        )

    def test_meta_values_can_be_extracted(self):
        """`extract` 与 `validate` 共用 `_search_jmespath`，元信息同样可以提取。"""
        extracted = self.resp_obj.extract({"ms": "elapsed_ms", "size": "response_size"})

        self.assertAlmostEqual(
            extracted["ms"], self.raw_resp.elapsed.total_seconds() * 1000, places=3
        )
        self.assertEqual(extracted["size"], len(self.raw_resp.content))


class TestComparisonTypeErrorIsReadable(unittest.TestCase):
    """P1-a：比较类 `TypeError` 要变成**可读的断言失败**，且不能吞掉真正的 `TypeError`。

    修复前它会直接冒泡：pytest 里表现为 error（而不是 failed），且只有一行
    `'<' not supported between instances of ...`，看不出是哪条断言、哪个检查项。
    """

    def setUp(self) -> None:
        self.raw_resp = requests.post(
            f"{HTTP_BIN_URL}/anything", json={"args": {"sum_v": "4"}}
        )
        self.parser = Parser(functions_mapping={})
        self.resp_obj = ResponseObject(self.raw_resp, self.parser)

    def test_elapsed_with_numeric_comparator_fails_readably(self):
        with self.assertRaises(ValidationFailure) as ctx:
            self.resp_obj.validate([{"less_than": ["elapsed", 2]}])

        message = str(ctx.exception)
        self.assertIn("elapsed", message)  # 哪个检查项
        self.assertIn("less_than", message)  # 哪个算子
        self.assertIn("timedelta", message)  # check_value 类型
        self.assertIn("TypeError", message)  # 原始异常类型（信息不丢）
        self.assertIn("elapsed_ms", message)  # 可操作建议

    def test_string_number_vs_int_fails_readably(self):
        """字符串数字与整数比较：`examples/postman_echo/...request_with_parameters.yml:33`
        留了很久的那条 FIXME 属同一类问题。"""
        with self.assertRaises(ValidationFailure) as ctx:
            self.resp_obj.validate([{"less_than": ["body.json.args.sum_v", 4]}])

        message = str(ctx.exception)
        self.assertIn("str", message)
        self.assertIn("int", message)
        self.assertIn("type_error", message)

    def test_length_comparator_on_non_sized_value_fails_readably(self):
        with self.assertRaises(ValidationFailure) as ctx:
            self.resp_obj.validate([{"length_equal": ["status_code", 3]}])

        self.assertIn("has no len()", str(ctx.exception))

    def test_operator_internal_type_error_is_not_swallowed(self):
        """算子内部主动抛的 TypeError 仍必须是**错误**，不能被降级成「断言失败」。"""

        def broken(check_value, expect_value, message=""):
            raise TypeError("boom: 算子内部真实错误")

        resp_obj = ResponseObject(
            self.raw_resp, Parser(functions_mapping={"broken": broken})
        )

        with self.assertRaises(TypeError) as ctx:
            resp_obj.validate([{"broken": ["status_code", 200]}])

        self.assertIn("boom", str(ctx.exception))

    def test_readable_failure_is_recorded_as_fail(self):
        with self.assertRaises(ValidationFailure):
            self.resp_obj.validate([{"less_than": ["elapsed", 2]}])

        self.assertEqual(
            self.resp_obj.validation_results["validate_extractor"][0]["check_result"],
            "fail",
        )

    def test_contains_list_in_dict_fails_readably(self):
        """0918-8 / L18：`contains: ["body.headers", ["a","b"]]` 不能逃成裸 TypeError。

        根因：对 **dict** 做 `in` 判断的是**键**（键必须可哈希），而期望值这里写成了列表
        → CPython 抛 `unhashable type: 'list'`，消息与"哪条断言、哪个检查项"毫无关系，
        在 pytest 里还表现为 **error**（不是 failed）。
        """
        with self.assertRaises(ValidationFailure) as ctx:
            self.resp_obj.validate(
                [{"contains": ["body.headers", ["a", "b"]]}]
            )

        message = str(ctx.exception)
        self.assertIn("contains", message)  # 哪个算子
        self.assertIn("body.headers", message)  # 哪个检查项
        self.assertIn("type_error", message)  # 原始异常（信息不丢）
        self.assertIn("dict", message)  # 可操作建议：说清是 dict 的语义
        self.assertEqual(
            self.resp_obj.validation_results["validate_extractor"][0]["check_result"],
            "fail",
        )

    def test_not_iterable_operand_fails_readably(self):
        """同一族的另一条：`argument of type 'int' is not iterable`。"""

        def bad_contains(check_value, expect_value, message=""):
            return 1 in check_value  # check_value 是 int → TypeError

        resp_obj = ResponseObject(
            self.raw_resp, Parser(functions_mapping={"bad_contains": bad_contains})
        )
        with self.assertRaises(ValidationFailure) as ctx:
            resp_obj.validate([{"bad_contains": ["status_code", 200]}])

        self.assertIn("not iterable", str(ctx.exception))


class TestCheckExpressionExactness(unittest.TestCase):
    """P1-a：检查项的第一段必须**精确**命中前缀。

    NOTICE: 修复前用的是 `expr.startswith(tuple(resp_obj_meta.keys()))`，
    `bodyx`、`status_code2` 这类拼错的表达式会命中前缀 → 走 jmespath → 静默返回 `None`，
    最终表现为「None != 期望值」的莫名失败，而不是「表达式非法」。
    """

    def setUp(self) -> None:
        self.raw_resp = requests.post(
            f"{HTTP_BIN_URL}/anything", json={"args": {"x": "1"}}
        )
        self.resp_obj = ResponseObject(self.raw_resp, Parser(functions_mapping={}))

    def test_typo_prefix_is_rejected_instead_of_silent_none(self):
        for expr in ["bodyx", "status_code2", "headerss", "cookiesx", "reason2"]:
            with self.subTest(expr=expr):
                with self.assertRaises(ParamsError) as ctx:
                    self.resp_obj.validate([{"equal": [expr, None]}])
                self.assertIn("invalid check/extract expression", str(ctx.exception))

    def test_legit_prefixes_and_response_attributes_still_work(self):
        self.resp_obj.validate(
            [
                {"equal": ["status_code", 200]},
                {"equal": ["body.json.args.x", "1"]},
                {"equal": ["url", self.raw_resp.url]},
                {"equal": ["reason", "OK"]},
                # 含特殊字符的响应头要用引号形式（jmespath 里 `-` 不是合法标识符字符）
                {"equal": ['headers."Content-Type"', "application/json"]},
            ]
        )


# ---------------------------------------------------------------------------
# 0918-8 / H6：head 必须是「前导标识符」，不能按 `.` 切第一段
# ---------------------------------------------------------------------------


def make_response(body, content_type="application/json", status=200, headers=None):
    """构造一个响应对象（不起服务；与 tests/non_json_response_test.py 同源手法）。

    **为什么要专门造「JSON 根是数组」的响应**：`body[0].id` 这类表达式的**首段**就带下标，
    而仓库里既有的 httpbin 用例走的都是 `body.json.locations[0]`（下标在后面几段，
    head 仍然是 `body`）——所以它们**照不出** H6 这个 bug。这也是它能在 7 轮修复里活下来的原因。
    """
    resp = requests.Response()
    resp.status_code = status
    if isinstance(body, bytes):
        resp._content = body
    elif isinstance(body, str):
        resp._content = body.encode("utf-8")
    else:
        resp._content = json.dumps(body, ensure_ascii=False).encode("utf-8")
    resp.headers["Content-Type"] = content_type
    for name, value in (headers or {}).items():
        resp.headers[name] = value
    resp.encoding = "utf-8"
    resp.url = "http://127.0.0.1/svc/items"
    return resp


class TestSearchJmespathHeadIsLeadingIdentifier(unittest.TestCase):
    """0918-8 / H6：`body[0].id` 这类**首段带下标**的合法 jmespath 必须能用。

    NOTICE: 修复前 `head = expr.split(".", 1)[0]`——它假设「第一段后面一定是 `.`」，
    于是 `body[0].id` 的 head 变成 `body[0]`（不在 `resp_obj_meta` 里）→ 直接判「表达式非法」。
    两层后果：① 列表接口（JSON 根是数组）**没有任何合法写法**；
    ② 与 0918-7 / L2 的修复互相矛盾（`compat` 把 `content[0].x` 改写成 `body[0].x`，
    运行期却必然报错）。详见 docs/缺陷修复日志0918-8.md 的 H6。
    """

    def setUp(self) -> None:
        self.raw_resp = make_response([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}])
        self.resp_obj = ResponseObject(self.raw_resp, Parser(functions_mapping={}))

    def test_index_on_first_segment(self):
        """断言与取值两条路径都要通（extract 与 validate 共用 _search_jmespath）。"""
        self.assertEqual(self.resp_obj._search_jmespath("body[0].id"), 1)
        self.resp_obj.validate([{"equal": ["body[0].id", 1]}])
        self.assertEqual(
            self.resp_obj.extract({"first_id": "body[0].id"}), {"first_id": 1}
        )

    def test_slice_wildcard_and_filter_on_first_segment(self):
        self.assertEqual(self.resp_obj._search_jmespath("body[0:2].id"), [1, 2])
        self.assertEqual(self.resp_obj._search_jmespath("body[*].id"), [1, 2])
        self.assertEqual(self.resp_obj._search_jmespath("body[?name=='a'].id"), [1])

    def test_bare_prefix_and_later_segment_index_still_work(self):
        """反向：裸前缀、以及「下标在后面几段」的老写法都不能被改坏。"""
        self.assertEqual(self.resp_obj._search_jmespath("body"), self.raw_resp.json())
        self.assertEqual(self.resp_obj._search_jmespath("status_code"), 200)
        self.assertEqual(self.resp_obj._search_jmespath("body[1].name"), "b")

    def test_misspelled_prefix_is_still_rejected(self):
        """反向断言：H6 不许把 0917-1 的收益一起放宽掉。

        与 TestCheckExpressionExactness（P1-a）同口径：`bodyx`/`status_code2` 必须仍然报错，
        而且是**同一个**报错（含 supported prefixes）。
        """
        for expr in ["bodyx", "bodyx[0].id", "status_code2", "Body[0].id"]:
            with self.subTest(expr=expr):
                with self.assertRaises(ParamsError) as ctx:
                    self.resp_obj._search_jmespath(expr)
                self.assertIn("invalid check/extract expression", str(ctx.exception))

    def test_expression_without_leading_identifier_is_rejected_not_crashed(self):
        """取不到前导标识符的表达式（`[0].id` / `*` / 空串）仍走「表达式非法」，
        不能因为新写法变成 `AttributeError: 'NoneType' object has no attribute 'group'`。"""
        for expr in ["[0].id", "*", "", "@"]:
            with self.subTest(expr=expr):
                with self.assertRaises(ParamsError):
                    self.resp_obj._search_jmespath(expr)

    def test_bytes_body_with_subscript_gets_readable_hint(self):
        """判据由「含 `.`」放宽为「不是裸前缀」后，`body[0]` 在非 JSON 响应上也要给同一句提示。"""
        resp_obj = ResponseObject(
            make_response("<r><n>1</n></r>", content_type="text/xml"),
            Parser(functions_mapping={}),
        )

        with self.assertRaises(ParamsError) as ctx:
            resp_obj._search_jmespath("body[0]")

        self.assertIn("非 JSON 响应体", str(ctx.exception))


def _capture_warnings(func, *args, **kwargs):
    """执行 func 并返回 loguru 捕获到的 WARNING 文本列表（与 tests/loader_test.py 同手法）。"""
    messages = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        func(*args, **kwargs)
    finally:
        logger.remove(sink_id)
    return messages


class TestCompatRewriteIsAcceptedByRuntime(unittest.TestCase):
    """**跨层不变量**（0918-8 / H6 的配套产物）：转换层改出来的表达式，运行期必须真的能跑。

    NOTICE: 这是本轮最该补的一类测试。0918-7 / L2 让 `compat._convert_jmespath` 支持把
    `content[0].x` 改写成 `body[0].x`，并在 tests/compat_test.py 里配了断言；
    `response._search_jmespath` 也有自己的用例。**但没有任何测试把这两段连起来**——
    于是「改写正确 + 取值拒绝」这个组合整整活过一轮，两处修复各自都是绿的。

    做法：走**真实的** `compat.ensure_testcase_v4`（不手工拼表达式），把取出的表达式交给
    **真实的** `ResponseObject` 去取值。以后任何一侧单独改动，这条测试都会红。
    """

    def _run_v2_extractor(self, v2_extractor):
        testcase = {
            "config": {"name": "cross-layer"},
            "teststeps": [
                {
                    "name": "step",
                    "request": {"method": "GET", "url": "/items"},
                    # v2/v3 的 extract 是「列表套单键字典」
                    "extract": [{"first_id": v2_extractor}],
                }
            ],
        }
        v4_testcase = compat.ensure_testcase_v4(testcase)
        expr = v4_testcase["teststeps"][0]["extract"]["first_id"]

        resp_obj = ResponseObject(
            make_response([{"id": 1}]), Parser(functions_mapping={})
        )
        return expr, resp_obj._search_jmespath(expr)

    def test_content_subscript_rewrite_runs(self):
        expr, value = self._run_v2_extractor("content[0].id")
        self.assertEqual(expr, "body[0].id")
        self.assertEqual(value, 1)

    def test_json_subscript_rewrite_runs(self):
        expr, value = self._run_v2_extractor("json[0].id")
        self.assertEqual(expr, "body[0].id")
        self.assertEqual(value, 1)

    def test_plain_v2_rewrites_still_run(self):
        """顺带覆盖 L2 的另一半：`content.x` / `json.x` 改写后同样要能取值。"""
        resp_obj = ResponseObject(
            make_response({"code": "0000"}), Parser(functions_mapping={})
        )
        for v2_extractor in ["content.code", "json.code"]:
            with self.subTest(extractor=v2_extractor):
                expr, _ = self._run_v2_extractor(v2_extractor)
                self.assertEqual(expr, "body.code")
                self.assertEqual(resp_obj._search_jmespath(expr), "0000")

    def test_header_quoting_rewrite_is_accepted_by_runtime(self):
        """§六.2 的补齐（含 0918-8 / M32）：compat 给特殊头**加引号**之后，运行期必须能取值。

        `_convert_jmespath` 把 `headers.Content-Type` 改写成 `headers."Content-Type"`。
        「改对了」与「运行期认不认」是两件事——H6 就是这么活过一轮的。
        M32 又把加引号的判据从两个硬编码名字扩到「任何含 `-` 的裸名字」，
        所以这里连 `X-Trace` 一起钉住。
        """
        resp_obj = ResponseObject(
            make_response(
                {"a": 1},
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "it/1.0",
                    "X-Trace": "t-1",
                },
            ),
            Parser(functions_mapping={}),
        )

        for raw, expected, value in (
            ("headers.Content-Type", 'headers."Content-Type"', "application/json"),
            ("headers.User-Agent", 'headers."User-Agent"', "it/1.0"),
            ("headers.X-Trace", 'headers."X-Trace"', "t-1"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(compat._convert_jmespath(raw), expected)
                self.assertEqual(resp_obj._search_jmespath(expected), value)

    def test_every_compat_rewrite_in_the_matrix_is_accepted(self):
        """把 compat 会改写的形态做成矩阵，逐条验证「改写 → 运行期可取值」。"""
        resp_obj = ResponseObject(
            make_response(
                [{"id": 1}],
                headers={"X-Trace": "t-1", "Content-Type": "application/json"},
            ),
            Parser(functions_mapping={}),
        )

        matrix = [
            "content[0].id",
            "json[0].id",
            "content.code",
            "json.code",
            "body[0].id",
            "status_code",
            "headers.Content-Type",
            "headers.X-Trace",
            "headers.X-Request-Id",
        ]
        for raw in matrix:
            with self.subTest(raw=raw):
                rewritten = compat._convert_jmespath(raw)
                # 运行期接受 = 不抛 ParamsError / JMESPathError（取不到值不算失败）
                resp_obj._search_jmespath(rewritten)

    def test_lexer_level_expression_error_is_readable(self):
        """M32 的另一半：词法级错误（名字里带 `-`）要转成带改法的 ParamsError。"""
        resp_obj = ResponseObject(
            make_response({"a": 1}, headers={"X-Trace": "t-1"}),
            Parser(functions_mapping={}),
        )

        with self.assertRaises(ParamsError) as cm:
            resp_obj._search_jmespath("headers.X-Trace")  # 未加引号

        message = str(cm.exception)
        self.assertIn("X-Trace", message)
        self.assertIn("引号", message)


class TestMissingValueIsNotSilent(unittest.TestCase):
    """0918-8 / H8：字段缺失/下标越界取到的 `None` 不能再静默。

    NOTICE（修复前实测）：`not_equal: ["body.data.access_token", ""]`（最常用的
    「字段存在且非空」写法）在字段**根本不存在**时**通过**，且日志里没有任何异常信号。

    两条修法、两种强度：
    1. **字符串形态算子**（`startswith`/`endswith`/`string_equals`）→ 直接判**失败**：
       它们只对字符串有意义，`str(None)` = `"None"` 这种"能满足"本身就是错的；
    2. **其余算子**（`not_equal`/`type_match`…）→ 只**告警**：因为 `None` 有两种来源
       （字段缺失 / 字段值真的是 null），jmespath 对两者都返回 None、框架无法区分，
       直接判失败会破坏 `eq: [body.x, null]` 这类合法写法——
       所以「明确期望 null」的写法**刻意放过**（按本项目「宁可漏报也不假报」的口径）。
    """

    def setUp(self) -> None:
        # access_token 刻意不存在
        self.resp_obj = ResponseObject(
            make_response({"data": {"other": 1}}), Parser(functions_mapping={})
        )

    def test_extract_miss_warns(self):
        messages = _capture_warnings(
            self.resp_obj.extract, {"token": "body.data.access_token"}
        )
        self.assertEqual(self.resp_obj.extract({"t2": "body.data.nope"}), {"t2": None})
        joined = "\n".join(messages)
        self.assertIn("body.data.access_token", joined)
        self.assertIn("None", joined)

    def test_extract_bare_prefix_does_not_warn(self):
        """`body` 这种裸前缀不算「路径未命中」，不该产生噪声。"""
        messages = _capture_warnings(self.resp_obj.extract, {"body": "body"})
        self.assertEqual(messages, [])

    def test_existing_path_does_not_warn(self):
        messages = _capture_warnings(
            self.resp_obj.validate, [{"not_equal": ["body.data.other", ""]}]
        )
        self.assertEqual(messages, [])

    def test_lenient_operator_passes_but_warns(self):
        """`not_equal` 遇到缺失字段仍然 pass（设计如此），但**必须**告警。"""
        messages = _capture_warnings(
            self.resp_obj.validate, [{"not_equal": ["body.data.access_token", ""]}]
        )
        joined = "\n".join(messages)
        self.assertIn("access_token", joined)
        self.assertIn("not_equal", joined)
        self.assertIn("静默满足", joined)

    def test_explicit_null_expectation_is_exempt_by_design(self):
        """明确「期望 null」的写法刻意放过（**包括字段真的缺失**的情况）。

        取舍：`None` 分不清「字段缺失」与「值为 null」，而 `eq: [body.errorCode, null]`、
        `type_match: [body.json, None]` 是仓库里**真实在用**的合法写法（examples 下 4 处），
        对它们告警属于**假报**——按本项目「宁可漏报也不假报」的口径，放过。
        代价：**路径写错 + 期望 null** 时会静默通过（残余假通过，已记在登记表里）。
        """
        resp_obj = ResponseObject(
            make_response({"nul": None, "data": {"other": 1}}),
            Parser(functions_mapping={}),
        )

        for check_item in ("body.nul", "body.data.nope"):
            for validator in (
                {"eq": [check_item, None]},
                {"type_match": [check_item, "None"]},
                {"type_match": [check_item, None]},
            ):
                with self.subTest(check=check_item, validator=validator):
                    messages = _capture_warnings(resp_obj.validate, [validator])
                    self.assertEqual(messages, [], "明确期望 null 时不该报「取值未命中」")

    def test_string_shape_operator_now_fails_on_miss(self):
        """字符串形态算子从「静默通过」变成「可读失败」。"""
        for validator in (
            {"startswith": ["body.data.access_token", "N"]},
            {"endswith": ["body.data.access_token", "one"]},
            {"string_equals": ["body.data.access_token", "None"]},
        ):
            with self.subTest(validator=validator):
                with self.assertRaises(ValidationFailure) as ctx:
                    self.resp_obj.validate([validator])

                message = str(ctx.exception)
                self.assertIn("取值是 None", message)
                self.assertIn("hint", message)


def _plain_response(status=200, body=b'{"a": 1}', content_type="application/json"):
    """不依赖 mock server 的 ResponseObject 素材（本类只关心 validate 的构造期行为）。"""
    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    resp.headers["Content-Type"] = content_type
    resp.encoding = "utf-8"
    return resp


class TestBatch0919_27LiteralCheckItem(unittest.TestCase):
    """批次 8 / **L1**：`check` 位置写成**字面量**不得崩成裸 `TypeError`。

    ## 现场（实测，修复前，`.tmp_report/probe_l1_l2.py`）

    ```python
    check_item = u_validator["check"]
    if "$" in check_item:            # ← check_item 是 int/None 时 TypeError
    ```

    ```text
    validate: - equal: [200, 200]   → TypeError: argument of type 'int' is not iterable
    validate: - equal: [None, None] → TypeError: argument of type 'NoneType' is not iterable
    ```

    这个 `if` 在下面那个 `try`（把比较类 TypeError 转成**可读断言失败**）**之前**，
    所以它冒到 pytest 里是 **error 而不是 failed**，消息里既没有算子、也没有检查项 ——
    与本文件 `test_not_iterable_operand_fails_readably` 想消灭的那一类问题同族，
    只是位置更靠前。

    ## 修法

    判据补类型：`isinstance(check_item, (Text, list, tuple, dict)) and "$" in check_item`。
    字面量于是落到**本来就有的**「不是文本 → 不当路径/表达式解析」分支，
    直接按字面量比较（`{"equal": [200, 200]}` → pass；`{"equal": [200, 999]}` → 一条可读的失败）。
    **容器留在判据里**是为了逐字保留原语义（对 list/dict 而言 `"$" in x` 是元素/键查找，
    它们今天并不崩）—— 即这是纯崩溃修复，没有任何"以前能跑"的写法被改语义。
    """

    def _validate(self, validators, variables_mapping=None):
        obj = ResponseObject(_plain_response(), Parser({}))
        try:
            obj.validate(validators, variables_mapping)
        except ValidationFailure as ex:
            return obj, ex
        return obj, None

    def test_literal_int_check_does_not_crash(self):
        """**核心**：记录里的 `{'equal': [200, 200]}` 从 error 变成 pass。"""
        obj, failure = self._validate([{"equal": [200, 200]}])

        self.assertIsNone(failure, f"字面量 check 仍然崩：{failure}")
        self.assertEqual(
            obj.validation_results["validate_extractor"][0]["check_result"], "pass"
        )

    def test_literal_int_check_fails_readably_when_it_should(self):
        """同一形态下的真失败：必须是**可读结论**（算子 + 检查项 + 两侧值），不是 error。"""
        obj, failure = self._validate([{"equal": [200, 999]}])

        message = str(failure)
        self.assertIn("equal", message)
        self.assertIn("check_item: 200", message)
        self.assertIn("check_value: 200(int)", message)
        self.assertIn("expect_value: 999(int)", message)
        self.assertIn("fail", str(obj.validation_results["validate_extractor"][0]))

    def test_literal_none_check_does_not_crash(self):
        obj, failure = self._validate([{"equal": [None, None]}])

        self.assertIsNone(failure)
        self.assertEqual(
            obj.validation_results["validate_extractor"][0]["check_result"], "pass"
        )

    def test_container_checks_keep_their_old_meaning(self):
        """特异性对照：list 检查项（含**裸 `$` 元素**）走原来的语义，一个字不变。"""
        obj, failure = self._validate([{"contains": [["a", "b"], "a"]}])
        self.assertIsNone(failure)
        self.assertEqual(
            obj.validation_results["validate_extractor"][0]["check_result"], "pass"
        )

        # 含裸 `$` 的列表：修复前后都会走 parse_data（这里没有可解析的变量 → 仍是同一个列表）
        obj, failure = self._validate([{"contains": [["$", "b"], "$"]}])
        self.assertIsNone(failure)
        self.assertEqual(
            obj.validation_results["validate_extractor"][0]["check_result"], "pass"
        )

    def test_string_and_variable_checks_are_unchanged(self):
        """特异性对照：正常路径表达式与 `$var` 两种既有写法照旧。"""
        obj, failure = self._validate([{"eq": ["status_code", 200]}])
        self.assertIsNone(failure)

        obj, failure = self._validate([{"eq": ["$code", 200]}], {"code": 200})
        self.assertIsNone(failure, f"变量化 check 被改坏了：{failure}")
        self.assertEqual(
            obj.validation_results["validate_extractor"][0]["check_result"], "pass"
        )
