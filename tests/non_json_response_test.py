"""0917-1：非 JSON 响应（XML/HTML/纯文本）在「断言 / 取值 / 提取」三条链路上的行为。

背景：XML 响应的 `body` 是 **bytes**（`ResponseObject.__getattr__` 走 `resp.content`），
本次修复把三类「静默或难看」的行为变成可读、可操作：

1. `contains: ["body", "0000"]` 曾抛**原始 TypeError**（pytest 报 error）→ 现在转成可读失败 + hint；
2. `body.code` 曾**静默得到 None** → 现在直接报错并给出三条替代路径；
3. check 位置的值被 `parse_string_value` 转型（`"0000"` → `0`）→ 现在失败信息里带 hint；
4. `text` 前缀（`requests.Response.text`）提为一等公民，出现在 supported prefixes 里。

不起服务：直接构造 `requests.Response` 再包成 `ResponseObject`（与 `tests/response_test.py` 同源手法）。
"""

import unittest

import requests

from interfacetester.exceptions import ParamsError, ValidationFailure
from interfacetester.parser import Parser
from interfacetester.response import ResponseObject, uniform_validator

XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    "<soap:Body>"
    '<ns1:QueryResponse xmlns:ns1="http://demo.example.com/svc">'
    "<ns1:code>0000</ns1:code><ns1:message>成功</ns1:message>"
    "</ns1:QueryResponse>"
    "</soap:Body></soap:Envelope>"
)

JSON_BODY = '{"code": "0000"}'


def make_response(body=XML, content_type="text/xml; charset=utf-8", status=200):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode("utf-8") if isinstance(body, str) else body
    resp.headers["Content-Type"] = content_type
    resp.encoding = "utf-8"
    resp.url = "http://127.0.0.1/svc/query"
    return resp


def validate(body, check_item, expect_value, method="contains", variables=None, **kwargs):
    """跑一条断言：返回 None 表示通过，否则返回 (异常类型, 消息)。

    NOTICE: `uniform_validator` 只接受**一个 dict**（format2：`{"contains": [检查项, 期望值]}`）。
    """
    parser = Parser(functions_mapping={"noop": lambda *a: None})
    resp_obj = ResponseObject(make_response(body, **kwargs), parser)
    validator = uniform_validator({method: [check_item, expect_value]})
    try:
        resp_obj.validate([validator], variables or {})
    except Exception as ex:  # noqa: BLE001
        return type(ex), str(ex)
    return None


class TestStringAssertionsOnBytes(unittest.TestCase):
    """① 原始 TypeError → 可读失败（P1-a 白名单补了 bytes 相关的两类）。"""

    def test_contains_on_bytes_is_readable_failure_with_text_hint(self):
        failure = validate(XML, "body", "0000", "contains")
        self.assertIsNotNone(failure)
        kind, message = failure
        self.assertIs(kind, ValidationFailure)  # 不再是原始 TypeError（pytest 的 error）
        self.assertIn("bytes", message)
        self.assertIn("text", message)  # hint 必须指出正确写法

    def test_contained_by_on_bytes_is_readable_failure(self):
        failure = validate(XML, "body", XML, "contained_by")
        self.assertIsNotNone(failure)
        self.assertIs(failure[0], ValidationFailure)

    def test_text_prefix_makes_string_assertions_work(self):
        self.assertIsNone(validate(XML, "text", "<ns1:code>0000</ns1:code>", "contains"))
        self.assertIsNone(validate(XML, "text", str, "type_match"))
        self.assertIsNone(validate(XML, "text", "<?xml", "startswith"))
        # body 仍是 bytes（老用例不受影响）
        self.assertIsNone(validate(XML, "body", bytes, "type_match"))

    def test_text_prefix_is_listed_in_supported_prefixes(self):
        parser = Parser(functions_mapping={})
        resp_obj = ResponseObject(make_response(), parser)
        with self.assertRaises(ParamsError) as ctx:
            resp_obj._search_jmespath("bodyx")
        self.assertIn("'text'", str(ctx.exception))


class TestNonJsonPathLookup(unittest.TestCase):
    """② `body.code` 在非 JSON 响应上曾是静默 None。"""

    def test_body_path_on_xml_raises_with_actionable_message(self):
        parser = Parser(functions_mapping={})
        resp_obj = ResponseObject(make_response(), parser)
        with self.assertRaises(ParamsError) as ctx:
            resp_obj._search_jmespath("body.code")
        message = str(ctx.exception)
        self.assertIn("非 JSON", message)
        self.assertIn("text", message)

    def test_extract_body_path_on_xml_raises(self):
        parser = Parser(functions_mapping={})
        resp_obj = ResponseObject(make_response(), parser)
        with self.assertRaises(ParamsError):
            resp_obj.extract({"code": "body.code"})

    def test_json_response_still_supports_body_paths(self):
        parser = Parser(functions_mapping={})
        resp_obj = ResponseObject(make_response(JSON_BODY, "application/json"), parser)
        self.assertEqual(resp_obj._search_jmespath("body.code"), "0000")

    def test_bare_body_still_returns_bytes(self):
        parser = Parser(functions_mapping={})
        resp_obj = ResponseObject(make_response(), parser)
        self.assertIsInstance(resp_obj._search_jmespath("body"), bytes)


class TestCheckItemCoercion(unittest.TestCase):
    """③ check 位置的值被 ast.literal_eval 转型（"0000" → 0）。"""

    def test_coercion_hint_is_attached_to_failure(self):
        failure = validate(
            XML, "$code", "0000", "equal", variables={"code": "0000"}
        )
        self.assertIsNotNone(failure)
        kind, message = failure
        self.assertIs(kind, ValidationFailure)
        self.assertIn("parse_string_value", message)
        self.assertIn("expect", message)  # 提示要指向正确写法

    def test_no_hint_when_no_coercion(self):
        """普通字符串变量（非纯数字）不转型 → 不该出现这条 hint。"""
        failure = validate(
            XML, "$code", "0000", "equal", variables={"code": "abcd"}
        )
        self.assertIsNotNone(failure)
        self.assertNotIn("parse_string_value", failure[1])


class TestExtractGuard(unittest.TestCase):
    """④ extract 的取值表达式若不是字符串，此前抛 `'dict' object has no attribute 'split'`。"""

    def test_non_string_extractor_field_raises_clear_error(self):
        parser = Parser(functions_mapping={"as_dict": lambda: {"a": 1}})
        resp_obj = ResponseObject(make_response(JSON_BODY, "application/json"), parser)
        with self.assertRaises(ParamsError) as ctx:
            resp_obj.extract({"x": "${as_dict()}"})
        self.assertIn("必须是字符串", str(ctx.exception))
        self.assertIn("teardown_hooks", str(ctx.exception))

    def test_normal_extract_still_works(self):
        parser = Parser(functions_mapping={})
        resp_obj = ResponseObject(make_response(JSON_BODY, "application/json"), parser)
        self.assertEqual(resp_obj.extract({"code": "body.code"}), {"code": "0000"})


if __name__ == "__main__":
    unittest.main()
