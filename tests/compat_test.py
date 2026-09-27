import os
import unittest

from interfacetester import compat, exceptions, loader, make
from interfacetester.utils import HTTP_BIN_URL


class TestCompat(unittest.TestCase):
    def setUp(self):
        loader.project_meta = None

    def test_convert_variables(self):
        raw_variables = {"var1": 1, "var2": "val2"}
        self.assertEqual(
            compat.convert_variables(raw_variables, "examples/data/a-b.c/1.yml"),
            {"var1": 1, "var2": "val2"},
        )
        raw_variables = "${get_variables()}"
        self.assertEqual(
            compat.convert_variables(raw_variables, "examples/data/a-b.c/1.yml"),
            {"foo1": "session_bar1"},
        )

        with self.assertRaises(exceptions.TestCaseFormatError):
            raw_variables = [{"var1": 1}, {"var2": "val2", "var3": 3}]
            compat.convert_variables(raw_variables, "examples/data/a-b.c/1.yml")
        with self.assertRaises(exceptions.TestCaseFormatError):
            compat.convert_variables(None, "examples/data/a-b.c/1.yml")

    def test_convert_request(self):
        request_with_json_body = {
            "method": "POST",
            "url": "https://postman-echo.com/post",
            "headers": {"Content-Type": "application/json"},
            "body": {"k1": "v1", "k2": "v2"},
        }
        self.assertEqual(
            compat._convert_request(request_with_json_body),
            {
                "method": "POST",
                "url": "https://postman-echo.com/post",
                "headers": {"Content-Type": "application/json"},
                "json": {"k1": "v1", "k2": "v2"},
            },
        )

        request_with_text_body = {
            "method": "POST",
            "url": "https://postman-echo.com/post",
            "headers": {"Content-Type": "text/plain"},
            "body": "have a nice day",
        }
        self.assertEqual(
            compat._convert_request(request_with_text_body),
            {
                "method": "POST",
                "url": "https://postman-echo.com/post",
                "headers": {"Content-Type": "text/plain"},
                "data": "have a nice day",
            },
        )

    def test_convert_jmespath(self):
        self.assertEqual(compat._convert_jmespath("content.abc"), "body.abc")
        self.assertEqual(compat._convert_jmespath("json.abc"), "body.abc")
        self.assertEqual(
            compat._convert_jmespath("headers.Content-Type"), 'headers."Content-Type"'
        )
        self.assertEqual(
            compat._convert_jmespath("headers.User-Agent"), 'headers."User-Agent"'
        )
        self.assertEqual(
            compat._convert_jmespath('headers."Content-Type"'), 'headers."Content-Type"'
        )
        self.assertEqual(
            compat._convert_jmespath("body.users[-1]"),
            "body.users[-1]",
        )
        # NOTICE（0918-8 / M32）：这一条的**期望值被改写过**，理由是原期望钉住了一个
        # jmespath 根本不接受的表达式（实测取证）：
        #   jmespath.search("body.result.WorkNode_-1", {...})   → ParseError（Unexpected token: -1）
        #   jmespath.search('body.result."WorkNode_-1"', {...}) → 7（正确取值）
        # 加引号的判据已从「content-* / user-agent 两个硬编码名字」扩到「任何含 `-` 的裸名字」
        # （`X-Trace` / `X-Request-Id` 这类真实头名同样受益），见 `compat._needs_jmespath_quotes`。
        self.assertEqual(
            compat._convert_jmespath("body.result.WorkNode_-1"),
            'body.result."WorkNode_-1"',
        )
        # 0918-7 / L2：只有**完整前缀**（`json`/`content` 本身，或其后紧跟 `.` / `[`）才改写；
        # 碰巧以这两个词开头的字段名不得被误改（修复前 `json_str.id` → `body_str.id`）
        self.assertEqual(compat._convert_jmespath("json"), "body")
        self.assertEqual(compat._convert_jmespath("content"), "body")
        self.assertEqual(compat._convert_jmespath("json_str.id"), "json_str.id")
        self.assertEqual(compat._convert_jmespath("content_type.x"), "content_type.x")
        # 取下标写法（合法前缀形态）仍须正常改写
        self.assertEqual(compat._convert_jmespath("content[0].name"), "body[0].name")

    def test_convert_extractors(self):
        self.assertEqual(
            compat._convert_extractors(
                [{"varA": "content.varA"}, {"varB": "json.varB"}]
            ),
            {"varA": "body.varA", "varB": "body.varB"},
        )
        self.assertEqual(
            compat._convert_extractors([{"varA": "content[0].varA"}]),
            {"varA": "body[0].varA"},
        )
        self.assertEqual(
            compat._convert_extractors({"varA": "content[0].varA"}),
            {"varA": "body[0].varA"},
        )

    def test_convert_validators(self):
        self.assertEqual(
            compat._convert_validators(
                [{"check": "content.abc", "assert": "eq", "expect": 201}]
            ),
            [{"check": "body.abc", "assert": "eq", "expect": 201}],
        )
        self.assertEqual(
            compat._convert_validators([{"eq": ["content.abc", 201]}]),
            [{"eq": ["body.abc", 201]}],
        )
        self.assertEqual(
            compat._convert_validators([{"eq": ["content[0].name", 201]}]),
            [{"eq": ["body[0].name", 201]}],
        )

    def test_ensure_testcase_v4_api(self):
        api_content = {
            "name": "get with params",
            "request": {
                "method": "GET",
                "url": "/get",
                "params": {"foo1": "bar1", "foo2": "bar2"},
                "headers": {"User-Agent": "InterfaceTester/3.0"},
            },
            "extract": [{"varA": "content.varA"}, {"user_agent": "headers.User-Agent"}],
            "validate": [{"eq": ["content.varB", 200]}, {"lt": ["json[0].varC", 0]}],
        }
        self.assertEqual(
            compat.ensure_testcase_v4_api(api_content),
            {
                "config": {
                    "name": "get with params",
                    "export": ["varA", "user_agent"],
                },
                "teststeps": [
                    {
                        "name": "get with params",
                        "request": {
                            "method": "GET",
                            "url": "/get",
                            "params": {"foo1": "bar1", "foo2": "bar2"},
                            "headers": {"User-Agent": "InterfaceTester/3.0"},
                        },
                        "extract": {
                            "varA": "body.varA",
                            "user_agent": 'headers."User-Agent"',
                        },
                        "validate": [
                            {"eq": ["body.varB", 200]},
                            {"lt": ["body[0].varC", 0]},
                        ],
                    }
                ],
            },
        )

    def test_ensure_testcase_v4(self):
        testcase_content = {
            "config": {"name": "xxx", "base_url": HTTP_BIN_URL},
            "teststeps": [
                {
                    "name": "get with params",
                    "request": {
                        "method": "GET",
                        "url": "/get",
                        "params": {"foo1": "bar1", "foo2": "bar2"},
                        "headers": {"User-Agent": "InterfaceTester/3.0"},
                    },
                    "extract": [
                        {"varA": "content.varA"},
                        {"user_agent": "headers.User-Agent"},
                    ],
                    "validate": [
                        {"eq": ["content.varB", 200]},
                        {"lt": ["json[0].varC", 0]},
                    ],
                }
            ],
        }
        self.assertEqual(
            compat.ensure_testcase_v4(testcase_content),
            {
                "config": {"name": "xxx", "base_url": HTTP_BIN_URL},
                "teststeps": [
                    {
                        "name": "get with params",
                        "request": {
                            "method": "GET",
                            "url": "/get",
                            "params": {"foo1": "bar1", "foo2": "bar2"},
                            "headers": {"User-Agent": "InterfaceTester/3.0"},
                        },
                        "extract": {
                            "varA": "body.varA",
                            "user_agent": 'headers."User-Agent"',
                        },
                        "validate": [
                            {"eq": ["body.varB", 200]},
                            {"lt": ["body[0].varC", 0]},
                        ],
                    }
                ],
            },
        )

    def test_ensure_cli_args(self):
        # NOTICE（0920 批次 5 / **N32** 订正）：`--failfast` 原先被**静默丢弃**，
        # 现在会被**翻译成 `-x`**（pytest 只认 `-x`/`--exitfirst`）——
        # 否则用户要的"首败即停"会被悄悄降级成"整份跑完"。
        args1 = ["examples/postman_echo/request_methods/hardcode.yml", "--failfast"]
        self.assertEqual(
            compat.ensure_cli_args(args1),
            ["examples/postman_echo/request_methods/hardcode.yml", "-x"],
        )

        args2 = ["examples/postman_echo/request_methods/hardcode.yml", "--save-tests"]
        self.assertEqual(
            compat.ensure_cli_args(args2),
            ["examples/postman_echo/request_methods/hardcode.yml"],
        )
        self.assertTrue(os.path.isfile("examples/postman_echo/conftest.py"))

        args3 = [
            "examples/postman_echo/request_methods/hardcode.yml",
            "--report-file",
            "report.html",
        ]
        self.assertEqual(
            compat.ensure_cli_args(args3),
            [
                "examples/postman_echo/request_methods/hardcode.yml",
                "--html",
                "report.html",
                "--self-contained-html",
            ],
        )

        args4 = [
            "examples/postman_echo/request_methods/hardcode.yml",
            "--failfast",
            "--save-tests",
            "--report-file",
            "report.html",
        ]
        self.assertEqual(
            compat.ensure_cli_args(args4),
            [
                "examples/postman_echo/request_methods/hardcode.yml",
                "--html",
                "report.html",
                "-x",
                "--self-contained-html",
            ],
        )

    def test_failfast_is_translated_to_exitfirst(self):
        """N32：`--failfast` → `-x`（保住用户要的失败模式），而不是丢掉。"""
        args = ["case.yml", "--failfast"]
        result = compat.ensure_cli_args(args)

        self.assertNotIn("--failfast", result)
        self.assertIn("-x", result, "诉求被丢掉了：用户要的首败即停没有生效")

    def test_failfast_does_not_duplicate_an_existing_exitfirst(self):
        """已经写了 `-x` / `--exitfirst` 时不重复添加。"""
        for existing in ("-x", "--exitfirst"):
            with self.subTest(existing=existing):
                args = ["case.yml", existing, "--failfast"]
                result = compat.ensure_cli_args(args)

                self.assertNotIn("--failfast", result)
                self.assertEqual(
                    sum(result.count(flag) for flag in ("-x", "--exitfirst")),
                    1,
                    result,
                )

    def test_no_failfast_means_no_exitfirst_is_added(self):
        """反向护栏：没写 `--failfast` 时不许擅自加 `-x`（会改变既有 CI 行为）。"""
        args = ["case.yml", "-q"]
        result = compat.ensure_cli_args(args)

        self.assertNotIn("-x", result)
        self.assertNotIn("--exitfirst", result)

    def test_ensure_file_path(self):
        self.assertEqual(
            compat.ensure_path_sep("demo\\test.yml"), os.sep.join(["demo", "test.yml"])
        )
        self.assertEqual(
            compat.ensure_path_sep(os.path.join(os.getcwd(), "demo\\test.yml")),
            os.path.join(os.getcwd(), os.sep.join(["demo", "test.yml"])),
        )
        self.assertEqual(
            compat.ensure_path_sep("demo/test.yml"), os.sep.join(["demo", "test.yml"])
        )
        self.assertEqual(
            compat.ensure_path_sep(os.path.join(os.getcwd(), "demo/test.yml")),
            os.path.join(os.getcwd(), os.sep.join(["demo", "test.yml"])),
        )


class TestStepAttachmentKeepsModelFields(unittest.TestCase):
    """0918-1（M11 根因）：`_ensure_step_attachment` 是**按白名单重建** step 的。

    所以白名单漏掉哪个字段，那个字段就在这一步被**静默删掉**——用户只会看到一句
    「存在无法识别的字段」的告警，而字段其实在 `models.TStep` 里有定义。
    修复前漏的就是 `retry_times`/`retry_interval`/`sql_request`/`thrift_request`/
    `validators` 这 5 个，其中 `retry_times` 还是 `docs/能力清单.md` 标注为 ✅ 的能力。
    """

    def setUp(self):
        loader.project_meta = None

    def test_retry_fields_survive_rebuild(self):
        """M11 回归：retry 配置必须活着穿过转换层。"""
        step = compat._ensure_step_attachment(
            {
                "name": "retry step",
                "request": {"method": "GET", "url": "/get"},
                "retry_times": 3,
                "retry_interval": 2,
            }
        )

        self.assertEqual(step["retry_times"], 3)
        self.assertEqual(step["retry_interval"], 2)

    def test_retry_fields_survive_ensure_testcase_v4(self):
        """端到端穿过 `ensure_testcase_v4`（`make_testcase` 的第一步）。"""
        testcase_content = {
            "config": {"name": "retry", "base_url": HTTP_BIN_URL},
            "teststeps": [
                {
                    "name": "retry step",
                    "request": {"method": "GET", "url": "/get"},
                    "retry_times": 2,
                    "retry_interval": 1,
                }
            ],
        }

        converted = compat.ensure_testcase_v4(testcase_content)
        step = converted["teststeps"][0]

        self.assertEqual(step["retry_times"], 2)
        self.assertEqual(step["retry_interval"], 1)

    def test_validators_alias_is_normalized_to_validate(self):
        """`validators` 是模型字段名，必须与 YAML 别名 `validate` 等价。

        修复前只认 `validate`，写 `validators` 会被丢弃——而 pydantic 明明接受它，
        属于「模型认、转换层丢」的静默失败。
        """
        step = compat._ensure_step_attachment(
            {
                "name": "alias step",
                "request": {"method": "GET", "url": "/get"},
                "validators": [{"eq": ["status_code", 200]}],
            }
        )

        self.assertIn("validate", step)
        self.assertNotIn("validators", step)

    def test_validate_alias_still_works_and_takes_priority(self):
        """`validate` 仍然可用；两者并存时以 `validate` 为准（不歧义、不报错）。"""
        step = compat._ensure_step_attachment(
            {
                "name": "both",
                "request": {"method": "GET", "url": "/get"},
                "validate": [{"eq": ["status_code", 200]}],
                "validators": [{"eq": ["status_code", 500]}],
            }
        )

        self.assertEqual(step["validate"], [{"eq": ["status_code", 200]}])

    def test_validate_must_be_a_list(self):
        with self.assertRaises(exceptions.TestCaseFormatError):
            compat._ensure_step_attachment(
                {
                    "name": "bad",
                    "request": {"method": "GET", "url": "/get"},
                    "validate": {"eq": ["status_code", 200]},
                }
            )

    def test_step_known_fields_covers_model(self):
        """不变量：已知清单必须覆盖模型字段，不能再出现漂移。"""
        from interfacetester.models import TStep

        self.assertEqual(set(TStep.model_fields) - compat.STEP_KNOWN_FIELDS, set())


class TestStepFieldWhitelistSingleSource(unittest.TestCase):
    r"""批次 9-1：step 字段白名单只能有**一条派生链**。

    修复前 `models.KNOWN_STEP_FIELDS`（loader 的未知字段告警用）与
    `compat.STEP_KNOWN_FIELDS`（转换层按它**重建** teststep）各自写了一遍
    `set(TStep.model_fields) | {...}`，两份只差一个字面量 —— 于是「往模型加字段」
    要同时改两处，漏掉任何一处都会退回 0918-1 那种**静默丢弃**
    （`retry_times` 就是这么丢的：模型有、运行期消费、生成器却拿不到）。

    现在的派生链是单向的：

    ```text
    TStep.model_fields ──► models.KNOWN_STEP_FIELDS ──► compat.STEP_KNOWN_FIELDS（+api）
    ```

    本类钉住这条链的**形状**（差集恰好是 `{"api"}`），以及「模型字段一定在 compat 那份里」。
    """

    def test_derivation_chain_is_exactly_one_extra_field(self):
        from interfacetester import compat as compat_module
        from interfacetester.models import KNOWN_STEP_FIELDS, TStep

        # ① compat 那份必须**包含** models 那份（派生链存在）
        self.assertEqual(
            KNOWN_STEP_FIELDS - compat_module.STEP_KNOWN_FIELDS,
            set(),
            "compat.STEP_KNOWN_FIELDS 漏掉了 models.KNOWN_STEP_FIELDS 里的字段",
        )
        # ② 多出来的**只有** v2/v3 专有的 `api`（多一个都说明有人又抄了一份清单）
        self.assertEqual(
            compat_module.STEP_KNOWN_FIELDS - KNOWN_STEP_FIELDS,
            {"api"},
            "compat 相对 models 多出了预期之外的字段 —— 白名单又分叉了",
        )
        # ③ 任何一个模型字段都必须同时出现在两份里（新增字段不许只进其中一份）
        for field_name in TStep.model_fields:
            with self.subTest(field=field_name):
                self.assertIn(field_name, KNOWN_STEP_FIELDS)
                self.assertIn(field_name, compat_module.STEP_KNOWN_FIELDS)


class TestContentTypeLookupIsCaseInsensitive(unittest.TestCase):
    """0919-2 / 缺陷 4：`Content-Type` 的取用口径。

    HTTP 头名**大小写不敏感**，但修复前 `_convert_request` 是按精确键
    `request["headers"]["Content-Type"]` 查的。v2/v3 老用例（HTTP/2 录制产物常见）
    写小写 `content-type: application/json` 时取不到值 → dict body 落进 `data`、
    以 **form-urlencoded** 发出 —— 请求形态被静默改错，而用例照样能过。

    本类既钉住修复后的行为（大小写不敏感），也钉住**不能误伤**的反向面
    （大小写无关的 `text/plain` 仍走 `data`、没写头仍走 `data`）。
    """

    def setUp(self):
        loader.project_meta = None

    def _request(self, headers, body=None):
        return {
            "method": "POST",
            "url": "https://postman-echo.com/post",
            "headers": headers,
            "body": {"k1": "v1"} if body is None else body,
        }

    def test_lowercase_content_type_still_routes_body_to_json(self):
        """缺陷 4 的核心现场：小写头必须与小驼峰写法**路由结果一致**。"""
        canonical = compat._convert_request(
            self._request({"Content-Type": "application/json"})
        )
        lowercase = compat._convert_request(
            self._request({"content-type": "application/json"})
        )

        self.assertIn("json", canonical)
        self.assertIn("json", lowercase)
        self.assertNotIn("data", lowercase)

        # 只比「体被路由到哪」——**不比整个 dict**。
        # NOTICE: 修复只把**查找**做成大小写不敏感，**不重写用户的头名**：
        # 发出去的请求里仍然是用户写的那一个拼法（requests 在传输层本就大小写不敏感，
        # 替用户改写头名反而会让 `--save-tests` 产物与源文件出现无谓差异）。
        self.assertEqual(
            {k: v for k, v in canonical.items() if k != "headers"},
            {k: v for k, v in lowercase.items() if k != "headers"},
        )
        self.assertEqual(list(lowercase["headers"]), ["content-type"])

    def test_header_key_spelling_is_passed_through_verbatim(self):
        """反向钉住：修复**不得**顺手把用户的头名改写成规范大小写。"""
        for key in ("content-type", "CONTENT-TYPE", "cOnTeNt-TyPe"):
            with self.subTest(header=key):
                converted = compat._convert_request(
                    self._request({key: "application/json"})
                )
                self.assertEqual(list(converted["headers"]), [key])

    def test_every_plausible_casing_matches(self):
        """大小写不敏感是 HTTP 的语义，不是「多认几个拼法」——全大写也得认。"""
        for key in (
            "Content-Type",
            "content-type",
            "CONTENT-TYPE",
            "Content-type",
            "cOnTeNt-TyPe",
        ):
            with self.subTest(header=key):
                converted = compat._convert_request(
                    self._request({key: "application/json"})
                )
                self.assertIn("json", converted, f"{key} 未被识别为 JSON 体")

    def test_json_vendor_and_charset_suffixes_still_match(self):
        """`startswith("application/json")` 的既有语义不能被改窄。"""
        for value in (
            "application/json",
            "application/json; charset=utf-8",
            "application/json-patch+json",
        ):
            with self.subTest(value=value):
                converted = compat._convert_request(
                    self._request({"content-type": value})
                )
                self.assertIn("json", converted)

    def test_non_json_content_type_still_goes_to_data(self):
        """反向护栏：不能因为「大小写不敏感」就把所有 dict body 都当 JSON 发。"""
        converted = compat._convert_request(
            self._request({"content-type": "text/plain"})
        )
        self.assertIn("data", converted)
        self.assertNotIn("json", converted)

    def test_missing_header_still_goes_to_data(self):
        """没写 Content-Type 时行为不变（v2/v3 的既有语义，不动它）。"""
        converted = compat._convert_request(
            {"method": "POST", "url": "https://postman-echo.com/post", "body": {"a": 1}}
        )
        self.assertIn("data", converted)

    def test_non_string_content_type_is_a_loud_format_error(self):
        """顺带发现：头值非 str 时修复前是 `NoneType.startswith` 的裸 AttributeError。

        现在换成带可操作文案的 `TestCaseFormatError`。注意 `None`（YAML 里
        `Content-Type:` 留空）**不能**被当成「没写这个头」静默降级 —— 那正是本批要
        消灭的静默行为，所以这里必须报错。
        """
        for value in (None, 123, b"application/json", ["application/json"]):
            with self.subTest(value=value):
                with self.assertRaises(exceptions.TestCaseFormatError) as ctx:
                    compat._convert_request(self._request({"Content-Type": value}))
                self.assertIn("Content-Type", str(ctx.exception))

    def test_non_string_value_error_is_not_an_attribute_error(self):
        """钉住「失败类型」本身：修复前抛的是 AttributeError（说明性为零）。"""
        with self.assertRaises(exceptions.TestCaseFormatError):
            try:
                compat._convert_request(self._request({"content-type": None}))
            except AttributeError as ex:  # pragma: no cover - 只在回归时执行
                self.fail(f"又退化成 AttributeError 了：{ex}")

    def test_non_dict_headers_behavior_is_unchanged(self):
        """边界（刻意保留）：`headers` 不是 dict 时按「没有 Content-Type」处理。

        修复前 `"Content-Type" in "abc"` 本来就是 False，行为逐字一致 ——
        本批不扩大打击面，只把「写了但值非法」变响亮。
        """
        converted = compat._convert_request(
            {"method": "POST", "url": "/post", "headers": "abc", "body": {"a": 1}}
        )
        self.assertIn("data", converted)

    def test_case_insensitive_lookup_survives_the_v2_api_path(self):
        """端到端：小写头走 `ensure_testcase_v4_api`（v2/v3 的真正入口）也必须成 JSON。"""
        converted = compat.ensure_testcase_v4_api(
            {
                "name": "legacy api",
                "request": {
                    "method": "POST",
                    "url": "/post",
                    "headers": {"content-type": "application/json"},
                    "body": {"k1": "v1"},
                },
            }
        )

        request = converted["teststeps"][0]["request"]
        self.assertEqual(request["json"], {"k1": "v1"})
        self.assertNotIn("data", request)


class TestBatchECompatPreGateCrashes(unittest.TestCase):
    """批次 E / **L9 + L10**：compat 在生成期闸门**之前**的两处裸崩。

    ## L9：`_convert_validators` 先崩 → 「带定位的报错」根本不可达

    修复前实测：

    | `validate` 里的形态 | 修复前 |
    |---|---|
    | `{"eq": "notalist"}` | `TypeError: 'str' object does not support item assignment` |
    | `{"eq": []}` | `IndexError: list index out of range` |
    | `["e"]`（字符串条目） | `AttributeError: 'str' object has no attribute 'keys'` |

    这些形态本该由**最后一道闸门** `make.ensure_known_comparators` 报出来
    （它能给出用例/步骤/原文），但它压根没机会执行。

    ## L10：step 缺 `name` → 裸 `KeyError: 'name'`
    """

    def setUp(self):
        loader.project_meta = None

    def test_malformed_validators_are_passed_through_untouched(self):
        """转换器**只做转换、不做校验** —— 形态不对的原样放过（交给闸门报）。"""
        for bad in ({"eq": "notalist"}, {"eq": []}, ["e"], "eq", 5):
            with self.subTest(bad=bad):
                self.assertEqual(compat._convert_validators([bad]), [bad])

    def test_make_gate_reports_the_bad_shape_with_location(self):
        """端到端：坏形态现在能走到闸门，并拿到**带用例/步骤/原文**的报错。"""
        teststeps = [
            {
                "name": "step1",
                "request": {"method": "GET", "url": "/get"},
                "validate": [{"eq": "notalist"}],
            }
        ]

        with self.assertRaises(exceptions.ParamsError) as ctx:
            make.ensure_known_comparators(teststeps, {}, "cases/bad_validate.yml")

        message = str(ctx.exception)
        self.assertIn("bad_validate.yml", message, "必须点名用例文件")
        self.assertIn("step1", message, "必须点名步骤")
        self.assertIn("notalist", message, "必须把原文打出来")

    def test_step_without_name_gives_a_named_error(self):
        with self.assertRaises(exceptions.TestCaseFormatError) as ctx:
            compat._ensure_step_attachment({"request": {"method": "GET", "url": "/x"}})

        message = str(ctx.exception)
        self.assertIn("name", message)
        self.assertIn("request", message, "要把该 step 里现有的字段列出来，便于定位")

    def test_step_with_name_still_works(self):
        """反向护栏：正常 step 不受影响。"""
        step = compat._ensure_step_attachment(
            {"name": "ok", "request": {"method": "GET", "url": "/x"}}
        )

        self.assertEqual(step["name"], "ok")
