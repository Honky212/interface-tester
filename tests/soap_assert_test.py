"""A1：SOAP/XML 结构化断言算子的单测（算子本体在 `examples/soap/debugtalk.py`）。

**为什么测的是 examples 里的文件**：A1 的定位就是「项目级 `debugtalk.py` 交付件」——它**不进内核**，
所以仓库里的权威实现就是那份示例（客户照它抄）。这里按路径加载它并逐条锁住行为。

覆盖：命名空间前缀漂移 / `{URI}` 精确限定 / 属性选择器 / 文本断言 / 个数断言 / `soap_fault` /
bytes 与 str 两种输入 / 非法 XML 与非法选择器的报错可读性。

端到端（YAML → hmake → pytest）由 CI 的 examples 冒烟覆盖：`python -m interfacetester run examples/soap`。
"""

import importlib.util
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPERATORS_PATH = os.path.join(REPO_ROOT, "examples", "soap", "debugtalk.py")

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SVC_NS = "http://demo.example.com/svc"


def _load_operators():
    spec = importlib.util.spec_from_file_location("soap_example_debugtalk", OPERATORS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ops = _load_operators()
xpath_match = ops.xpath_match
xpath_count = ops.xpath_count
xpath_text = ops.xpath_text
soap_fault = ops.soap_fault

# 同一份契约、四种前缀写法（含「无前缀 + 默认命名空间」）
QUERY_NS1 = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{soap}">
  <soap:Body>
    <ns1:QueryResponse xmlns:ns1="{svc}">
      <ns1:code>0000</ns1:code>
      <ns1:message>成功</ns1:message>
      <ns1:amount>1234.50</ns1:amount>
    </ns1:QueryResponse>
  </soap:Body>
</soap:Envelope>
""".format(soap=SOAP_NS, svc=SVC_NS)

QUERY_ABC = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{soap}">
  <soap:Body>
    <abc:QueryResponse xmlns:abc="{svc}">
      <abc:code>0000</abc:code>
      <abc:message>成功</abc:message>
      <abc:amount>1234.50</abc:amount>
    </abc:QueryResponse>
  </soap:Body>
</soap:Envelope>
""".format(soap=SOAP_NS, svc=SVC_NS)

QUERY_DEFAULT_NS = """<?xml version="1.0" encoding="UTF-8"?>
<Envelope>
  <Body>
    <QueryResponse xmlns="{svc}">
      <code>0000</code>
      <message>成功</message>
    </QueryResponse>
  </Body>
</Envelope>
""".format(svc=SVC_NS)

ITEMS = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{soap}">
  <soap:Body>
    <ns1:ItemsResponse xmlns:ns1="{svc}">
      <ns1:item id="1"><ns1:name>甲</ns1:name></ns1:item>
      <ns1:item id="2"><ns1:name>乙</ns1:name></ns1:item>
      <ns1:item id="3"><ns1:name>丙</ns1:name></ns1:item>
    </ns1:ItemsResponse>
  </soap:Body>
</soap:Envelope>
""".format(soap=SOAP_NS, svc=SVC_NS)

FAULT = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{soap}">
  <soap:Body>
    <soap:Fault>
      <faultcode>soap:Client</faultcode>
      <faultstring>Invalid parameter: userId</faultstring>
      <detail><ns1:code xmlns:ns1="{svc}">SVC-1002</ns1:code></detail>
    </soap:Fault>
  </soap:Body>
</soap:Envelope>
""".format(soap=SOAP_NS, svc=SVC_NS)

# 元素用 abc: 前缀、声明却写着 ns1: → **不是合法 XML**
BROKEN = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{soap}">
  <soap:Body>
    <abc:QueryResponse xmlns:ns1="{svc}">
      <abc:code>0000</abc:code>
    </abc:QueryResponse>
  </soap:Body>
</soap:Envelope>
""".format(soap=SOAP_NS, svc=SVC_NS)


class TestPrefixAgnostic(unittest.TestCase):
    """前缀漂移是 SOAP 现场最常见的坑，也是这套算子存在的头号理由。"""

    def test_same_contract_with_two_different_prefixes(self):
        for payload in (QUERY_NS1, QUERY_ABC):
            xpath_match(payload, ["code", "0000"])
            xpath_match(payload, ["amount", "1234.50"])

    def test_works_with_default_namespace_without_prefix(self):
        # 无前缀 + 默认命名空间时，ElementTree 里 tag 是 `{URI}name`，local-name 匹配照样命中
        xpath_match(QUERY_DEFAULT_NS, ["code", "0000"])

    def test_exact_namespace_uri_is_enforced(self):
        xpath_match(QUERY_NS1, ["{{{}}}code".format(SVC_NS), "0000"])
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, ["{http://other.example.com/svc}code"])
        self.assertIn("未选中任何节点", str(ctx.exception))

    def test_bytes_and_text_inputs_are_equivalent(self):
        xpath_match(QUERY_NS1.encode("utf-8"), ["code", "0000"])
        xpath_match(QUERY_NS1, ["code", "0000"])

    def test_non_text_input_is_rejected_with_type_name(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match({"code": "0000"}, ["code"])
        self.assertIn("dict", str(ctx.exception))


class TestValueAssertions(unittest.TestCase):

    def test_text_mismatch_reports_actual_values(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, ["code", "9999"])
        message = str(ctx.exception)
        self.assertIn("0000", message)  # 实际值必须出现在报错里
        self.assertIn("9999", message)

    def test_existence_only_form_accepts_plain_string_spec(self):
        self.assertTrue(xpath_match(QUERY_NS1, "message"))

    def test_missing_node_reports_selector(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, ["notExist"])
        self.assertIn("notExist", str(ctx.exception))

    def test_attribute_selector(self):
        xpath_match(ITEMS, "item[@id='2']")
        with self.assertRaises(AssertionError):
            xpath_match(ITEMS, "item[@id='9']")

    def test_attribute_selector_ignores_attribute_prefix(self):
        payload = '<r xmlns:n="urn:x"><item n:id="7"/></r>'
        xpath_match(payload, "item[@id='7']")  # 属性前缀漂移也按 local-name 匹配

    def test_selector_can_be_prefixed_with_double_slash(self):
        xpath_match(QUERY_NS1, ".//code")

    def test_message_is_appended_on_failure(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, ["code", "9999"], "业务码不对，找开发确认")
        self.assertIn("业务码不对，找开发确认", str(ctx.exception))


class TestCountAssertion(unittest.TestCase):

    def test_count_matches(self):
        self.assertTrue(xpath_count(ITEMS, ["item", 3]))

    def test_count_mismatch_reports_both_numbers(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_count(ITEMS, ["item", 2])
        self.assertIn("选中 3 个", str(ctx.exception))
        self.assertIn("期望 2 个", str(ctx.exception))

    def test_count_accepts_numeric_string(self):
        # YAML 里写成字符串也能用（例如从变量拼出来的值）
        xpath_count(ITEMS, ["item", "3"])

    def test_count_rejects_bad_spec(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_count(ITEMS, ["item"])
        self.assertIn("[选择器, 个数]", str(ctx.exception))

    def test_count_rejects_non_numeric_expectation(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_count(ITEMS, ["item", "三条"])
        self.assertIn("必须是整数", str(ctx.exception))


class TestSoapFault(unittest.TestCase):
    """业务错误也是 HTTP 500：断言必须打在 Fault 内容上。"""

    def test_fault_substring_matches(self):
        self.assertTrue(soap_fault(FAULT, "Invalid parameter"))

    def test_fault_string_mismatch_is_reported(self):
        with self.assertRaises(AssertionError) as ctx:
            soap_fault(FAULT, "timeout")
        self.assertIn("Invalid parameter: userId", str(ctx.exception))

    def test_normal_response_is_rejected(self):
        with self.assertRaises(AssertionError) as ctx:
            soap_fault(QUERY_NS1, "anything")
        self.assertIn("没有 soap:Fault 节点", str(ctx.exception))

    def test_business_code_inside_detail(self):
        xpath_match(FAULT, ["{{{}}}code".format(SVC_NS), "SVC-1002"])


class TestTextHelper(unittest.TestCase):

    def test_returns_text(self):
        self.assertEqual(xpath_text(QUERY_NS1, "code"), "0000")

    def test_returns_none_when_absent(self):
        self.assertIsNone(xpath_text(QUERY_NS1, "notExist"))


class TestErrorMessages(unittest.TestCase):

    def test_invalid_xml_is_reported_loudly(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(BROKEN, ["code", "0000"])
        self.assertIn("不是合法 XML", str(ctx.exception))
        self.assertIn("unbound prefix", str(ctx.exception))

    def test_unsupported_selectors_are_rejected(self):
        # 轴、多级路径、position、通配、无引号的属性值 —— 一律明确报错，而不是静默断不了
        for selector in ("a/b", "..", "/x", "item[@id=2]", "*", "text()"):
            with self.subTest(selector=selector):
                with self.assertRaises(AssertionError) as ctx:
                    xpath_match(QUERY_NS1, [selector])
                self.assertIn("不支持", str(ctx.exception))

    def test_incomplete_namespace_is_rejected(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, ["{urn:x"])
        self.assertIn("命名空间写法不完整", str(ctx.exception))

    def test_empty_expectation_is_rejected(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, [])
        self.assertIn("不能是空列表", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
