"""P2-b：第 19 个内置算子 `jsonschema_match` 的直调用例（不依赖 HTTP）。

覆盖评估文档 3.1「验证方式」里列出的清单：
通过 / 字段缺失 / 类型错 / 嵌套对象 / 数组元素 schema / `oneOf` / 非法 schema 报错可读 /
含 `$schema`、`$ref` 的 schema 走 `${func()}` 与文件路径两条引用路。
端到端（YAML → hmake → pytest）在 `tests/cli_test.py::TestJsonschemaMatchViaCli`。

NOTICE（0919-14）：本文件**绝大部分用例不依赖 HTTP**，唯一例外是
`TestSchemaPathUsesRunningProjectRoot` 里那条真跑用例的端到端用例（走本地 mock 服务）。

NOTICE: `jsonschema` 自 P2-b 起是**主依赖**；这里仍用 `importorskip` 兜底，
避免「老版本安装/离线环境缺这个包」时整个测试文件报一堆 error（那会掩盖真正的问题）。
"""

import importlib.util
import json
import os
import shutil
import sys
import unittest
import uuid
from unittest import mock

import pytest
from loguru import logger

pytest.importorskip("jsonschema", reason="jsonschema 未安装（P2-b 起为主依赖）")

from interfacetester import exceptions  # noqa: E402
from interfacetester import loader  # noqa: E402
from interfacetester.builtin import comparators as builtin_comparators  # noqa: E402
from interfacetester.builtin.comparators import (  # noqa: E402
    _json_schema_has_keyword_keys,
    _load_schema_file,
    _resolve_schema_path,
    contains,
    endswith,
    jsonschema_match,
    regex_match,
    startswith,
    string_equals,
    type_match,
)

USER_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["code", "data"],
    "properties": {
        "code": {"type": "integer"},
        "data": {
            "type": "object",
            "required": ["id", "tags"],
            "properties": {
                "id": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

GOOD_PAYLOAD = {"code": 0, "data": {"id": "u-1", "tags": ["a", "b"]}}


def _tmp_dir() -> str:
    """临时目录放在 logs/ 下（已被 .gitignore 覆盖）。

    NOTICE: 不用 `tempfile.mkdtemp()`——它按 0o700 建目录，受限环境下后续 `open()` 会 PermissionError
    （与 tests/make_test.py、tests/cli_test.py 同一套做法）。
    """
    path = os.path.join(os.getcwd(), "logs", f"tmp_js_{uuid.uuid4().hex[:8]}")
    os.makedirs(path)
    return path


class TestJsonschemaMatch(unittest.TestCase):
    """算子本身的直调行为（含失败信息的可读性）。"""

    def assert_fails(self, check_value, schema, *expect_fragments):
        """断言校验失败，并检查失败信息里包含期望的片段（失败信息才是这个算子的主要价值）。"""
        with self.assertRaises(AssertionError) as ctx:
            jsonschema_match(check_value, schema)
        message = str(ctx.exception)
        for fragment in expect_fragments:
            self.assertIn(fragment, message)
        return message

    # ------------------------------------------------------------------ 通过
    def test_pass_returns_none(self):
        self.assertIsNone(jsonschema_match(GOOD_PAYLOAD, USER_SCHEMA))

    def test_pass_with_dollar_keys_in_schema(self):
        """含 `$schema`/`$ref` 的 schema 只要**不是**内联（这里等价于函数返回值）就能用。"""
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"data": {"$ref": "#/$defs/data"}},
            "$defs": {"data": {"type": "object", "required": ["id"]}},
        }
        self.assertIsNone(jsonschema_match({"data": {"id": "x"}}, schema))

    # ------------------------------------------------------------------ 失败信息
    def test_missing_required_field_is_readable(self):
        message = self.assert_fails(
            {"code": 0}, USER_SCHEMA, "JSON Schema 校验失败", "required=", "data"
        )
        # 路径用 json_path（`$` 开头），测试人员一眼能定位到报文的哪一层
        self.assertIn("$", message)

    def test_type_error_shows_expected_and_actual(self):
        self.assert_fails(
            {"code": "0", "data": {"id": "u-1", "tags": []}},
            USER_SCHEMA,
            "type=integer",
            '"0"(str)',
        )

    def test_nested_object_error_path(self):
        payload = {"code": 0, "data": {"tags": ["a"]}}  # 缺 data.id
        self.assert_fails(payload, USER_SCHEMA, "$.data", "required=", "id")

    def test_array_item_schema_error_path(self):
        payload = {"code": 0, "data": {"id": "u-1", "tags": ["a", 2]}}
        self.assert_fails(payload, USER_SCHEMA, "tags", "type=string", "2(int)")

    def test_oneof_error_is_reported(self):
        schema = {
            "type": "object",
            "properties": {
                "kind": {"oneOf": [{"const": "a"}, {"const": "b"}]},
            },
        }
        self.assert_fails({"kind": "c"}, schema, "oneOf", '"c"(str)')

    def test_error_description_from_schema_is_included(self):
        schema = {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "金额（分为单位，整数）"}
            },
        }
        self.assert_fails({"amount": "12"}, schema, "金额（分为单位，整数）")

    def test_invalid_schema_itself_is_readable(self):
        """schema 写错时，报错必须说「schema 自己有问题」，而不是含糊的断言失败。"""
        self.assert_fails(
            {"a": 1},
            {"type": "object", "properties": {"a": {"type": "nope"}}},
            "schema 本身不合法",
        )

    def test_many_errors_are_truncated(self):
        schema = {
            "type": "object",
            "properties": {f"f{index}": {"type": "integer"} for index in range(9)},
        }
        payload = {f"f{index}": "x" for index in range(9)}
        message = self.assert_fails(payload, schema, "共 9 处不符合", "已省略")
        # 只展示前 5 条 + 1 条省略提示
        self.assertEqual(len(message.splitlines()), 7)

    def test_custom_message_is_prepended(self):
        with self.assertRaises(AssertionError) as ctx:
            jsonschema_match({"code": 0}, USER_SCHEMA, "响应契约不满足")
        message = str(ctx.exception)
        self.assertTrue(message.startswith("响应契约不满足"))
        # 详细信息不能被自定义 message 挤掉
        self.assertIn("JSON Schema 校验失败", message)

    def test_draft_from_schema_is_named_in_message(self):
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["a"],
        }
        self.assert_fails({}, schema, "Draft7Validator")

    def test_default_draft_is_2020_12_when_schema_has_no_dollar_schema(self):
        self.assert_fails({}, {"type": "object", "required": ["a"]}, "Draft202012Validator")

    # ------------------------------------------------------------------ 非法入参
    def test_inline_json_string_gets_actionable_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            jsonschema_match({"a": 1}, '{"type": "object"}')
        message = str(ctx.exception)
        self.assertIn("内联 schema", message)
        self.assertIn("${func()}", message)
        self.assertIn("文件路径", message)

    def test_unsupported_expect_type_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            jsonschema_match({"a": 1}, 123)
        self.assertIn("必须是 schema（dict）或 schema 文件路径（str）", str(ctx.exception))

    def test_missing_jsonschema_dependency_gets_install_hint(self):
        # sys.modules[name] = None 会让 `import name` 抛 ImportError
        with mock.patch.dict(sys.modules, {"jsonschema": None}):
            with self.assertRaises(RuntimeError) as ctx:
                jsonschema_match({"a": 1}, {"type": "object"})
        self.assertIn("jsonschema 未安装", str(ctx.exception))
        self.assertIn("pip install jsonschema", str(ctx.exception))


class TestSchemaFileReference(unittest.TestCase):
    """第二条引用路：schema 文件路径（相对项目根目录解析）。"""

    def setUp(self):
        self.tmp_dir = _tmp_dir()
        self.schema_dir = os.path.join(self.tmp_dir, "schemas")
        os.makedirs(self.schema_dir)
        self.json_path = os.path.join(self.schema_dir, "user.json")
        with open(self.json_path, "w", encoding="utf-8") as fp:
            json.dump(USER_SCHEMA, fp, ensure_ascii=False)

        self.yaml_path = os.path.join(self.schema_dir, "user.yml")
        with open(self.yaml_path, "w", encoding="utf-8") as fp:
            fp.write(
                "type: object\n"
                "required:\n"
                "  - code\n"
                "  - data\n"
                "properties:\n"
                "  code:\n"
                "    type: integer\n"
                "  data:\n"
                "    type: object\n"
                "    required:\n"
                "      - id\n"
                "      - tags\n"
            )

        # 让 `_project_root_dir()` 指向这个临时项目（RootDir 语义）
        self._original_meta = loader.project_meta
        loader.project_meta = type("_Meta", (), {"RootDir": self.tmp_dir})()

    def tearDown(self):
        loader.project_meta = self._original_meta
        # NOTICE（0918-4）：修复前 tearDown 只还原 project_meta，**从不删除**临时目录，
        # 于是每跑一次测试就往 logs/ 里累积一批 `tmp_js_*`（本类有十几个用例，
        # 一次全量运行就多 14 个目录）。属于测试自身的卫生问题，不影响产品行为。
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_json_schema_file_relative_to_root_dir(self):
        self.assertIsNone(jsonschema_match(GOOD_PAYLOAD, "schemas/user.json"))

    def test_yaml_schema_file_relative_to_root_dir(self):
        self.assertIsNone(jsonschema_match(GOOD_PAYLOAD, "schemas/user.yml"))

    def test_absolute_path_works(self):
        self.assertIsNone(jsonschema_match(GOOD_PAYLOAD, self.json_path))

    def test_file_backed_schema_failure_is_readable(self):
        with self.assertRaises(AssertionError) as ctx:
            jsonschema_match({"code": 0}, "schemas/user.json")
        self.assertIn("required=", str(ctx.exception))

    def test_missing_schema_file_lists_attempted_paths(self):
        with self.assertRaises(RuntimeError) as ctx:
            jsonschema_match(GOOD_PAYLOAD, "schemas/nope.json")
        message = str(ctx.exception)
        self.assertIn("schema 文件不存在", message)
        # 必须列出尝试过的路径，否则用户只能靠猜
        self.assertIn(self.tmp_dir, message)
        self.assertIn("nope.json", message)
        self.assertIn("相对路径按「项目根目录", message)

    def test_broken_schema_file_reports_file_path(self):
        broken = os.path.join(self.schema_dir, "broken.json")
        with open(broken, "w", encoding="utf-8") as fp:
            fp.write("{not json")
        with self.assertRaises(RuntimeError) as ctx:
            jsonschema_match(GOOD_PAYLOAD, "schemas/broken.json")
        self.assertIn("schema 文件解析失败", str(ctx.exception))
        self.assertIn("broken.json", str(ctx.exception))

    def test_resolve_schema_path_prefers_root_dir(self):
        self.assertEqual(
            os.path.normcase(_resolve_schema_path("schemas/user.json")),
            os.path.normcase(self.json_path),
        )


class TestSchemaKeywordKeyDetection(unittest.TestCase):
    """`_json_schema_has_keyword_keys`：hmake 期拦截内联 schema 的判定依据。"""

    def test_detects_top_level_keywords(self):
        for keyword in ("$schema", "$ref", "$id", "$defs", "$comment"):
            self.assertTrue(_json_schema_has_keyword_keys({keyword: "x"}))

    def test_detects_nested_keywords(self):
        schema = {
            "type": "object",
            "properties": {"data": {"$ref": "#/$defs/data"}},
            "allOf": [{"items": {"$id": "x"}}],
        }
        self.assertTrue(_json_schema_has_keyword_keys(schema))

    def test_plain_schema_has_none(self):
        self.assertFalse(_json_schema_has_keyword_keys({"type": "object", "required": ["a"]}))

    def test_non_dict_values_are_ignored(self):
        self.assertFalse(_json_schema_has_keyword_keys([1, "two", None, {"a": "b"}]))


class TestOperatorRegistration(unittest.TestCase):
    """第 19 个算子要能被白名单/分发链路认出来。"""

    def test_jsonschema_match_is_in_builtin_comparators(self):
        self.assertIn("jsonschema_match", vars(builtin_comparators))

    def test_helpers_are_private_so_they_are_not_treated_as_operators(self):
        """只有算子本身是模块级公开函数（白名单是按 `isfunction + 非 _ 前缀` 推导的）。

        如果辅助函数漏了下划线前缀，它们会被当成「内置算子」混进白名单与错误提示里 ——
        所以这里把「谁是公开函数」钉死。
        """
        public_functions = {
            name
            for name, value in vars(builtin_comparators).items()
            if callable(value)
            and not isinstance(value, type)
            and not name.startswith("_")
            and not hasattr(value, "__path__")  # 排除模块对象
        }
        self.assertIn("jsonschema_match", public_functions)
        for helper in (
            "_json_schema_has_keyword_keys",
            "_resolve_schema_path",
            "_load_schema_file",
            "_format_validation_errors",
            "_json_schema_validate",
            # 0918-8 / H7 新增
            "_supported_formats",
            "_unenforceable_formats",
            "_warn_unenforceable_formats",
            # 0918-8 / H8 新增
            "_ensure_string_shaped",
        ):
            self.assertNotIn(helper, public_functions)
            self.assertTrue(hasattr(builtin_comparators, helper), f"{helper} 应当存在（私有）")


# ---------------------------------------------------------------------------
# 0918-8 / H7：`format` 必须真的被强制，而且要说清「哪些强制不了」
# ---------------------------------------------------------------------------


def _capture_warnings(func, *args, **kwargs):
    """执行 func 并返回 loguru 捕获到的 WARNING 文本列表（与 tests/loader_test.py 同手法）。"""
    messages = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        func(*args, **kwargs)
    finally:
        logger.remove(sink_id)
    return messages


class TestLexicographicNumericComparisonWarning(unittest.TestCase):
    """批次 C / **L11**：数值算子两侧都是字符串时，必须让「这是字典序比较」可见。

    ## 修复前的现场（`probe_numcmp.py`，真响应级）

    ```text
    greater_than('9', '100')  -> PASS        ← 9 竟然"大于"100
    greater_than(9, 100)      -> FAIL        ← 一侧是 int 时行为正确
    greater_than('9', 100)    -> TypeError   ← 两侧类型不一致会抛错（并被转成可读失败）
    ```

    也就是说：**提示只覆盖了「类型不一致」这个失败方向，通过方向是静默的**。
    接口把数字返回成字符串（Java 后端常见）时，`greater_than: ["body.price", "100"]`
    会因字典序 `'9' > '1'` 而**假通过**。

    NOTICE（为什么只告警、不做硬类型校验）：字典序对 ISO 日期串
    （`"2026-09-20" > "2026-01-01"`）恰好等于时间序，是**合法用法**；
    硬拦会制造假报（本仓的取向是「只拦能精确判定的形态」，假报会让真报被无视）。
    """

    @staticmethod
    def _warnings_of(func, *args) -> list:
        """调用算子并返回告警列表。

        NOTICE: 本类只关心「**有没有告警**」，而算子对不满足的值会抛
        `AssertionError`（还有类型不一致时的 `TypeError`）—— 所以这里吞掉它们，
        否则拿不到 `_capture_warnings` 收集到的那条告警（它在 `finally` 里清 sink）。
        """
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            func(*args)
        except (AssertionError, TypeError):
            pass
        finally:
            logger.remove(sink_id)
        return messages

    def test_both_strings_warns_and_explains_lexicographic_order(self):
        for operator in (
            builtin_comparators.greater_than,
            builtin_comparators.less_than,
            builtin_comparators.greater_or_equals,
            builtin_comparators.less_or_equals,
        ):
            with self.subTest(operator=operator.__name__):
                messages = self._warnings_of(operator, "9", "100")
                self.assertEqual(len(messages), 1, messages)
                self.assertIn("字典序", messages[0])
                # 必须给出可操作改法
                self.assertIn("期望值写成数字", messages[0])

    def test_behaviour_is_unchanged_only_a_warning(self):
        """反向护栏：**行为一个字不改** —— `greater_than('9', '100')` 仍然通过。

        （修复的定位是「让它可见」，不是「改变判定」：那条判定是 Python 语义，
        硬改会把日期串比较一并打死。）
        """
        messages = self._warnings_of(builtin_comparators.greater_than, "9", "100")

        self.assertEqual(len(messages), 1, "字典序比较必须告警")
        # 不抛异常即通过（与修复前完全一致）
        builtin_comparators.greater_than("9", "100")

    def test_numeric_operands_do_not_warn(self):
        """反向护栏：真正的数值比较**零告警**（不许在正常路径上刷噪声）。"""
        for args in ((9, 100), (9.5, 10), ("9", 100), (9, "100")):
            with self.subTest(args=args):
                messages = self._warnings_of(builtin_comparators.greater_than, *args)
                self.assertEqual(messages, [], f"数值比较不该告警：{args}")

    def test_string_operators_do_not_warn(self):
        """反向护栏：字符串形态算子（`string_equals` 等）两侧都是字符串，天然不该告警。"""
        messages = self._warnings_of(builtin_comparators.string_equals, "abc", "abc")

        self.assertEqual(messages, [])


class TestJsonSchemaFormatEnforcement(unittest.TestCase):
    """0918-8 / H7：`jsonschema_match` 必须强制 `format`，并**点名**当前强制不了的那些。

    NOTICE（修复前实测）：构造 validator 时没有传 `format_checker`，于是
    `jsonschema_match({"email": "definitely-not-an-email"}, {"format": "email"})` **通过**——
    契约断言静默退化成「只看类型」。而本项目的 **OpenAPI 导入器会把 `format` 带进断言 schema**
    （`openapi_adapter.build_assertion_schema`），所以「导入契约 → 生成断言 → 跑用例」
    这条推荐链路上，`date-time`/`uri`/`uuid`/`email` 全部形同虚设。

    第二半（同样重要）：本环境能强制的 format 只有 8 个，`date-time`/`uri`/`hostname` 等
    需要可选依赖、**仍然是静默通过**。只加 `format_checker` 等于把「完全没强制」
    换成「部分强制 + 用户以为全强制了」——所以必须把没参与的 format 告警出来。
    """

    # 基础 format：不依赖任何可选包，任何装了 jsonschema 的环境都必须能强制
    BASE_FORMATS = ("email", "date", "uuid", "ipv4", "regex")
    BAD_VALUES = {
        "email": "definitely-not-an-email",
        "date": "not-a-date",
        "uuid": "not-a-uuid",
        "ipv4": "999.1.1.1",
        "regex": "([",
    }
    GOOD_VALUES = {
        "email": "user@example.com",
        "date": "2020-01-01",
        "uuid": None,  # 运行时填（uuid4）
        "ipv4": "127.0.0.1",
        "regex": "^ab+c$",
    }

    def _enforceable(self):
        # NOTICE: 走 `_jsonschema_module()` 懒导入，而不是在文件顶部 `import jsonschema`——
        # 否则「缺 jsonschema 的离线/老环境」会整文件 ImportError，而不是被上面的 importorskip 跳过。
        jsonschema = builtin_comparators._jsonschema_module()
        validator_class = jsonschema.validators.validator_for({"type": "object"})
        return builtin_comparators._supported_formats(validator_class)

    @staticmethod
    def _schema_for(fmt: str) -> dict:
        return {
            "type": "object",
            "properties": {"f": {"type": "string", "format": fmt}},
            "required": ["f"],
        }

    def test_base_formats_are_enforceable_here(self):
        """护栏：防止本类用例因为环境把 checker 摘光而变成空跑。"""
        enforceable = self._enforceable()
        for fmt in self.BASE_FORMATS:
            self.assertIn(fmt, enforceable, f"{fmt} 应当无需可选依赖就能强制")

    def test_violating_values_fail_with_path_and_format(self):
        for fmt in self.BASE_FORMATS:
            with self.subTest(format=fmt):
                with self.assertRaises(AssertionError) as ctx:
                    jsonschema_match({"f": self.BAD_VALUES[fmt]}, self._schema_for(fmt))

                message = str(ctx.exception)
                self.assertIn("JSON Schema 校验失败", message)
                self.assertIn("$.f", message)  # 出错路径要能定位
                self.assertIn(f"format={fmt}", message)  # 期望里要写清是哪个 format

    def test_valid_values_pass_without_warning(self):
        for fmt in self.BASE_FORMATS:
            with self.subTest(format=fmt):
                good = self.GOOD_VALUES[fmt] or str(uuid.uuid4())
                messages = _capture_warnings(
                    jsonschema_match, {"f": good}, self._schema_for(fmt)
                )
                self.assertEqual(messages, [], "能强制的 format 不该产生告警噪声")

    def test_schema_file_route_also_enforces_format(self):
        """文件路径这条引用路（OpenAPI 导入器产出的就是它）同样必须强制。"""
        tmp_dir = _tmp_dir()
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        schema_path = os.path.join(tmp_dir, "email_schema.json")
        with open(schema_path, "w", encoding="utf-8") as fp:
            json.dump(self._schema_for("email"), fp)

        with self.assertRaises(AssertionError):
            jsonschema_match({"f": self.BAD_VALUES["email"]}, schema_path)
        self.assertIsNone(jsonschema_match({"f": "user@example.com"}, schema_path))

    def test_nested_format_is_also_enforced(self):
        """format 出现在 `items` / 嵌套 `properties` 里同样要强制（递归遍历）。"""
        schema = {
            "type": "object",
            "properties": {
                "users": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "email": {"type": "string", "format": "email"}
                        },
                        "required": ["email"],
                    },
                }
            },
            "required": ["users"],
        }

        with self.assertRaises(AssertionError) as ctx:
            jsonschema_match({"users": [{"email": "nope"}]}, schema)

        message = str(ctx.exception)
        self.assertIn("$.users[0].email", message)
        self.assertIn("format=email", message)

    def test_unenforceable_standard_format_warns_and_still_passes(self):
        """规范里有、本环境强制不了的 format：**告警**（不是静默通过），且不改变用例结论。

        `date-time` 需要 `rfc3339-validator`；这里断言的是「说出来」，不是「强制住」。
        """
        schema = {
            "type": "object",
            "properties": {"ts": {"type": "string", "format": "date-time"}},
            "required": ["ts"],
        }

        messages = _capture_warnings(jsonschema_match, {"ts": "not-a-date"}, schema)
        joined = "\n".join(messages)

        self.assertIn("date-time", joined, "必须点名是哪个 format 没被强制")
        self.assertIn("无法强制", joined)
        self.assertIn("jsonschema[format]", joined, "要给出可操作的安装提示")

    def test_openapi_annotation_formats_do_not_warn(self):
        """反向：`binary`/`int32`/`password` 是 **OpenAPI 自定义标注**，不归 JSON Schema 强制。

        实测 OpenAPI 导入器会产出 `format: binary`——对这些告警只会变成噪声，
        所以 `JSON_SCHEMA_STANDARD_FORMATS` 刻意不收录它们。
        """
        schema = {
            "type": "object",
            "properties": {
                "file": {"type": "string", "format": "binary"},
                "n": {"type": "integer", "format": "int32"},
                "p": {"type": "string", "format": "password"},
            },
        }

        messages = _capture_warnings(
            jsonschema_match, {"file": "x", "n": 1, "p": "y"}, schema
        )

        self.assertEqual(messages, [])

    def test_unenforceable_detection_is_recursive_and_deduped(self):
        """`_unenforceable_formats`：递归 + 去重 + 只挑规范 format（纯函数级断言）。"""
        jsonschema = builtin_comparators._jsonschema_module()
        validator_class = jsonschema.validators.validator_for({"type": "object"})
        schema = {
            "type": "object",
            "properties": {
                "a": {"type": "string", "format": "uri"},
                "b": {
                    "type": "array",
                    "items": {"type": "string", "format": "uri"},
                },
                "c": {"type": "string", "format": "date-time"},
                "d": {"type": "string", "format": "binary"},  # OpenAPI 标注 → 不算
                "e": {"type": "string", "format": "email"},  # 能强制 → 不算
            },
        }

        self.assertEqual(
            builtin_comparators._unenforceable_formats(schema, validator_class),
            ["date-time", "uri"],
        )


class TestStringShapeGuards(unittest.TestCase):
    """0918-8 / H8：字符串形态算子的取值守卫（`string_equals`/`startswith`/`endswith`）。

    NOTICE（修复前实测）：这三个算子直接 `str(check_value)`，于是
    - 取值 `None`（字段缺失 / 越界）→ `"None"`，`startswith: ["body.x", "N"]` 会**通过**；
    - 取值 `bytes`（非 JSON 响应体）→ 比较的是 `b'...'` 的 repr，`startswith: ["body", "b'"]`
      同样会通过（这一条在 docs/能力清单.md 登记过，本次与 None 一并收口）。

    边界刻意只到「None / bytes」：**不动**「数字被 str() 后比较」的既有用法
    （`string_equals: ["status_code", "200"]` 仓库里在用），所以本类有反向用例钉住。
    """

    OPERATORS = (string_equals, startswith, endswith)

    def test_none_is_rejected_with_actionable_hint(self):
        for operator in self.OPERATORS:
            with self.subTest(operator=operator.__name__):
                with self.assertRaises(AssertionError) as ctx:
                    operator(None, "N")

                message = str(ctx.exception)
                self.assertIn(operator.__name__, message)  # 点名是哪个算子
                self.assertIn("取值是 None", message)
                self.assertIn("hint", message)
                self.assertIn("text", message)  # 提示要指向可用的替代写法

    def test_bytes_is_rejected_with_repr_hint(self):
        for operator in self.OPERATORS:
            with self.subTest(operator=operator.__name__):
                with self.assertRaises(AssertionError) as ctx:
                    operator(b"<code>0000</code>", "b'")

                message = str(ctx.exception)
                self.assertIn(operator.__name__, message)
                self.assertIn("bytes", message)
                self.assertIn("repr", message)

    def test_numeric_and_string_coercion_still_works(self):
        """反向断言：既有合法用法必须一字不变（数字 → str 后比较）。"""
        string_equals(200, "200")
        string_equals("hello", "hello")
        startswith(200, "2")
        startswith("hello", "he")
        endswith(200, "00")
        endswith("hello", "lo")
        # 边界值：空串参与比较仍按老语义
        string_equals("", "")
        startswith("abc", "")
        endswith("abc", "")

        # 第三个参数（YAML 里的自定义说明）仍然生效
        with self.assertRaises(AssertionError) as ctx:
            string_equals("hello", "world", "自定义说明")
        self.assertIn("自定义说明", str(ctx.exception))

    def test_other_operators_keep_their_own_guards(self):
        """同类其它算子本来就有守卫——钉住口径一致，防止将来又被改回去。"""
        for operator in (contains, regex_match):
            with self.subTest(operator=operator.__name__):
                with self.assertRaises(AssertionError):
                    operator(None, "x")


class TestBatch0918_9TypeMatchNamespace(unittest.TestCase):
    """批次 9-0（M9 同根因）：`type_match` 的「类型名」查找不再触达整个内置命名空间。

    修复前 `get_type()` 是 `__builtins__[name]`：把**任意**内置名当成类型返回，于是
    `type_match: [body.x, eval]` 会拿到 `eval` 函数再去做 `type(x) == eval`（恒为假）
    ——表面上是「类型对不上」的正常断言失败（看起来像被测系统的问题），实际是用法错误；
    也意味着算子能触达整个内置命名空间。

    现在只接受**真的是类型对象**的内置名，与 `timedelta`（不是内置名）走同一条
    `ValueError` 路径，并且报错说明合法写法。
    """

    def test_type_names_still_work(self):
        """既有合法用法一字不变（对象形态 + 字符串形态都在仓库里真实使用）。"""
        for check_value, expect_value in (
            (580509390, int),
            (580509390, "int"),
            ([], list),
            ([], "list"),
            ({}, "dict"),
            ("abc", "str"),
            (1.5, "float"),
            (True, "bool"),
        ):
            with self.subTest(expect=expect_value):
                self.assertIsNone(type_match(check_value, expect_value))

        self.assertIsNone(type_match(None, "None"))
        self.assertIsNone(type_match(None, "NoneType"))
        self.assertIsNone(type_match(None, None))

    def test_non_type_builtin_names_are_refused(self):
        """`eval`/`__import__`/`open` … 不是类型名 → 明确的报错（不再是假断言失败）。

        NOTICE（0921-3 / 轻微项 2）：异常类型从 `ValueError` 改成 `ParamsError`。
        原因：`ValueError` 不在 `response.validate` 的捕获面内，于是
        `type_match: [body, "eval"]` 会冒泡成 pytest 的 **error**；而"用户写错类型名"
        按本仓划分应记 **failed**。`ParamsError` 是「用户误用」的显式标记，
        `validate` 会把它转成可读断言失败。断言内容（名字 / "不是类型名" / 合法示例）
        一字未变，只有类型收窄了。
        """
        for name in ("eval", "exec", "__import__", "open", "print", "globals", "getattr"):
            with self.subTest(name=name):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    type_match("anything", name)

                message = str(ctx.exception)
                self.assertIn(name, message)
                self.assertIn("不是类型名", message)
                self.assertIn("int", message)  # 给出合法示例

    def test_non_builtin_name_keeps_params_error(self):
        """`timedelta` 这类既不是内置也不是类型的名字，走同一条 `ParamsError` 路径。"""
        with self.assertRaises(exceptions.ParamsError) as ctx:
            type_match(1, "timedelta")
        self.assertIn("timedelta", str(ctx.exception))

    def test_non_string_non_type_still_params_error(self):
        """非字符串、非类型的 expect_value（如 `123`）同样是可读的用法错误。

        NOTICE（0921-3）：修复前这里抛裸 `ValueError(name)` —— 报错内容**只有** `123`
        一个数字，既不说哪个算子、也不说期望什么。现在给出完整说明。
        """
        with self.assertRaises(exceptions.ParamsError) as ctx:
            type_match(1, 123)
        self.assertIn("type_match", str(ctx.exception))
        self.assertIn("123", str(ctx.exception))

    def test_type_name_errors_are_not_swallowed_as_assertion_failures(self):
        """反向护栏：类型名写错**不是**断言失败（不能被当成"被测系统的问题"）。

        这正是批次 9-0 修掉的那个坑：修复前 `type_match: [body.x, eval]` 拿到 `eval`
        函数后做 `type(x) == eval`（恒为假）→ 看起来像正常的"类型对不上"。
        """
        with self.assertRaises(exceptions.ParamsError) as ctx:
            type_match("anything", "eval")
        self.assertNotIsInstance(ctx.exception, AssertionError)

    def test_type_mismatch_is_still_an_assertion_failure(self):
        """反向断言：真正的类型不匹配仍然必须是**断言失败**（可读的失败，不是 error）。"""
        with self.assertRaises(AssertionError):
            type_match("abc", "int")
        with self.assertRaises(AssertionError):
            type_match(123, "str", "自定义说明")


# ---------------------------------------------------------------------------
# 0919-14（登记项①）：schema / XSD 的相对路径基准 = **本用例所属项目**
#
# 现场：两个项目下各有一个同名 `schemas/x.json`，「当前已加载」的项目是 B，
# 而正在跑的用例属于 A。
#   · 修复前：断言期读全局 `loader.project_meta.RootDir` → 用 **B 的 schema** 校验。
#     严格的那个方向表现为「莫名失败」，**宽松的那个方向表现为「莫名通过」**（后者最危险）；
#   · 修复后：`SessionRunner.test_start` 用 `loader.use_run_root_dir(runner.root_dir)`
#     声明作用域，`comparators._project_root_dir()` 优先读它。
# ---------------------------------------------------------------------------
class TestSchemaPathUsesRunningProjectRoot(unittest.TestCase):
    """相对路径必须按「正在执行的用例所属项目」解析，而不是「当前已加载」的项目。"""

    def setUp(self):
        self._sys_path_backup = list(sys.path)
        self._dirs = []
        # A：严格 schema（要求一个不存在的键）——本用例属于 A，就该用它
        self.a = self._make_project(
            "strict", {"type": "object", "required": ["nope_key"]}
        )
        # B：宽松 schema（httpbin 的 /get 响应满足）
        self.b = self._make_project("loose", {"type": "object", "required": ["url"]})
        loader.load_project_meta(os.path.join(self.b, "case.yml"))  # 全局切到 B

    def tearDown(self):
        sys.path[:] = self._sys_path_backup
        loader.reset_project_meta()
        loader.load_project_meta(
            os.path.join(os.getcwd(), "tests", "comparators_test.py")
        )
        for path in self._dirs:
            shutil.rmtree(path, ignore_errors=True)

    def _make_project(self, tag: str, schema: dict) -> str:
        root = os.path.join(
            os.getcwd(), "logs", f"schema_root_{tag}_{uuid.uuid4().hex[:8]}"
        )
        os.makedirs(os.path.join(root, "schemas"), exist_ok=True)
        with open(os.path.join(root, "debugtalk.py"), "w", encoding="utf-8") as f:
            f.write(f"# 临时项目标记（{tag}）\n")
        with open(os.path.join(root, "case.yml"), "w", encoding="utf-8") as f:
            f.write("config:\n  name: probe\n")
        with open(os.path.join(root, "schemas", "x.json"), "w", encoding="utf-8") as f:
            json.dump(schema, f)
        self._dirs.append(root)
        return root

    # ------------------------------------------------------------ 单元级
    def test_run_scope_decides_the_root(self):
        """作用域内 → 用作用域里的根；`_resolve_schema_path` 解析到**本项目**的文件。"""
        with loader.use_run_root_dir(self.a):
            self.assertEqual(
                os.path.normcase(builtin_comparators._project_root_dir()),
                os.path.normcase(self.a),
            )
            resolved = _resolve_schema_path("schemas/x.json")

        self.assertEqual(
            os.path.normcase(resolved),
            os.path.normcase(os.path.join(self.a, "schemas", "x.json")),
        )

    def test_scope_is_nested_and_restored(self):
        """嵌套作用域（被引用用例是另一个项目）退出后必须恢复父用例的根。"""
        with loader.use_run_root_dir(self.a):
            self.assertEqual(
                os.path.normcase(builtin_comparators._project_root_dir()),
                os.path.normcase(self.a),
            )
            with loader.use_run_root_dir(self.b):
                self.assertEqual(
                    os.path.normcase(builtin_comparators._project_root_dir()),
                    os.path.normcase(self.b),
                )
            self.assertEqual(
                os.path.normcase(builtin_comparators._project_root_dir()),
                os.path.normcase(self.a),
            )

    def test_outside_run_scope_falls_back_to_loaded_project(self):
        """反向护栏：**不在用例执行范围内**（CLI 加载、直接调 comparator）行为不变。"""
        self.assertEqual(loader.current_run_root_dir(), "")
        self.assertEqual(
            os.path.normcase(builtin_comparators._project_root_dir()),
            os.path.normcase(self.b),
        )

    def test_no_silent_fallback_to_another_project(self):
        """作用域里找不到文件时，**不能**退化去别的项目找——必须响亮报错并列出已试路径。"""
        with loader.use_run_root_dir(self.a):
            with self.assertRaises(RuntimeError) as ctx:
                _resolve_schema_path("schemas/missing.json")

        message = str(ctx.exception)
        self.assertIn(self.a, message, "报错里应当出现**本项目**的尝试路径")
        self.assertNotIn(
            self.b, message, "不得把别的项目也当候选（那正是修复前的静默错源）"
        )

    # ------------------------------------------------------------ 端到端
    def test_case_uses_its_own_project_schema(self):
        """真跑用例：用例属于 A（严格 schema）→ 必须**失败**（说明用的是 A 的 schema）。

        反事实同时验证「宽松方向 = 静默通过」这个最危险的后果：
        把作用域关掉（模拟修复前的全局解析）→ 用上 B 的宽松 schema → **用例通过了**。
        """
        import mock_server  # noqa: F401  (conftest 已启动，这里只取 URL)
        from interfacetester import (
            Config,
            InterfaceTester,
            RunRequest,
            Step,
            exceptions,
        )
        from interfacetester.utils import HTTP_BIN_URL

        class _CaseInA(InterfaceTester):
            root_dir = self.a
            config = Config("schema root e2e").base_url(HTTP_BIN_URL)
            teststeps = [
                Step(
                    RunRequest("get with schema")
                    .get("/get")
                    .validate()
                    .assert_equal("status_code", 200)
                    .assert_jsonschema_match("body", "schemas/x.json")
                )
            ]

        # 非空跑自检：两个项目的 schema 严格程度确实不同（否则本用例没有判别力）
        strict = json.loads(
            open(os.path.join(self.a, "schemas", "x.json"), encoding="utf-8").read()
        )
        loose = json.loads(
            open(os.path.join(self.b, "schemas", "x.json"), encoding="utf-8").read()
        )
        self.assertEqual(strict.get("required"), ["nope_key"])
        self.assertEqual(loose.get("required"), ["url"])

        # 修复后：用 A 的严格 schema → 断言失败（这正是**正确**的行为）
        # NOTICE: 断言算子内部抛的是 AssertionError，但 `response.validate` 会把它
        # 统一包成 `ValidationFailure`（用例失败，不是用例错误）。
        with self.assertRaises(exceptions.ValidationFailure) as ctx:
            _CaseInA().test_start()
        self.assertIn("nope_key", str(ctx.exception), "失败信息应当来自 A 的 schema")

        # 反事实：关掉运行期作用域（= 修复前的全局解析）→ 用 B 的宽松 schema → 静默通过
        with mock.patch.object(loader, "current_run_root_dir", lambda: ""):
            summary = _CaseInA().test_start().get_summary()
        self.assertTrue(
            summary.success,
            "反事实应当表现为「用别人的宽松 schema 静默通过」——若这里也让用例失败，"
            "说明本用例没测到那条路径",
        )
