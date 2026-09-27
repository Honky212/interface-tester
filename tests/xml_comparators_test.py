"""XML/XPath 与 XSD 算子的单测（A2-1：`xpath_match` / `xpath_count`；A2-2：`xml_schema_match`）。

覆盖：简化写法语义（与 A1 **逐条对齐**）/ 原始 XPath 1.0 / 命名空间（全树收集、`{URI}`、前缀漂移）/
bytes 与 str（含 GBK 声明）/ 非法 XML / XPath 语法错与未声明前缀 / 标量与集合两类文本断言 /
个数断言 / 与 A1 标准库实现的对照 / 缺 lxml 的报错文案 / 白名单与分发链路；
以及 **XSD 契约校验**：两条引用路（整根 / 先定位子节点）/ 多文件 `xs:include` / 违例的节点路径与原因 /
「多节点必须收窄」/ XSD 自身不合法与文件不存在 / 编译缓存。

**关于 lxml**：这两个算子在可选 extra `xml` 里（`pip install -e ".[xml]"`）。
没装 lxml 的机器上语义用例整体 skip，但 `test_lxml_is_present_in_ci` 会在 CI 环境
（`CI=true`，GitHub/GitLab 都会设）下把「忘了装 extra」变成**失败** —— 否则整组用例被静默跳过，
等于这些算子没人测（评估文档 2.6.6 的明确要求）。
"""

import builtins
import importlib.util
import os
import shutil
import unittest
import uuid
from unittest import mock

from interfacetester import RunRequest
from interfacetester.builtin import comparators as builtin_comparators
from interfacetester.builtin.comparators import (
    _xml_selector_to_xpath,
    _xpath_literal,
    xml_schema_match,
    xpath_count,
    xpath_match,
)
from interfacetester.make import BUILTIN_COMPARATOR_NAMES
from interfacetester.step_request import StepRequestValidation

HAVE_LXML = importlib.util.find_spec("lxml") is not None
NEEDS_LXML = unittest.skipUnless(HAVE_LXML, '需要 lxml：pip install -e ".[xml]"')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A1_OPERATORS_PATH = os.path.join(REPO_ROOT, "examples", "soap", "debugtalk.py")

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SVC_NS = "http://demo.example.com/svc"

# 同一份契约、三种前缀写法（含「无前缀 + 默认命名空间」）：SOAP 现场的头号坑
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

# 前缀从 ns1: 换成 abc:：**契约相同，只是前缀漂移**
QUERY_ABC = QUERY_NS1.replace("ns1:", "abc:").replace("xmlns:ns1=", "xmlns:abc=")

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

# 属性带命名空间前缀：DSL 的属性匹配按 local-name（前缀漂移也命中）
ATTR_PREFIXED = '<r xmlns:n="urn:n"><item n:id="7"/></r>'

# 批次 C / L13-②：**根元素就是业务元素**（非 SOAP 包裹的普通 XML 接口）。
# 修复前简化写法编译成 `.//*[...]`（**后代**搜索、不含上下文节点自身）→ 永远选不中根元素，
# 而 `xml_schema_match` 文档推荐的 `{"xpath": "QueryResponse"}` 写法恰好踩这个坑。
ROOT_IS_BUSINESS = """<?xml version="1.0" encoding="UTF-8"?>
<QueryResponse xmlns="{svc}">
  <code>0000</code>
  <item id="1"><name>甲</name></item>
</QueryResponse>
""".format(svc=SVC_NS)

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


def _load_a1_operators():
    """加载 A1 的标准库实现（`examples/soap/debugtalk.py`）用于对照。"""
    spec = importlib.util.spec_from_file_location(
        "a1_soap_operators_for_parity", A1_OPERATORS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _outcome(func, *args):
    """把「通过 / 断言失败 / 用例错误」压成可比对的三元组（对照 A1 时用）。"""
    try:
        func(*args)
        return ("ok", "")
    except AssertionError as ex:
        return ("failed", str(ex))
    except RuntimeError as ex:
        return ("error", str(ex))


class TestLxmlPresence(unittest.TestCase):
    def test_lxml_is_present_in_ci(self):
        """CI 必须真的装上 extra：否则下面的用例全被 skip，等于没测。"""
        if os.environ.get("CI") and not HAVE_LXML:
            self.fail(
                'CI 环境缺少 lxml：XML 用例会被静默跳过。'
                '请把安装行改成 pip install -e ".[dev,xml]"（见 docs/ci/README.md）'
            )


@NEEDS_LXML
class TestSimplifiedSelector(unittest.TestCase):
    """简化写法 = A1 的语义（前缀无关），这部分**必须与 A1 完全一致**。"""

    def test_prefix_drift_does_not_matter(self):
        for payload in (QUERY_NS1, QUERY_ABC, QUERY_DEFAULT_NS):
            with self.subTest(payload=payload[:60]):
                self.assertTrue(xpath_match(payload, ["code", "0000"]))
                self.assertTrue(xpath_match(payload, ["message"]))

    def test_namespace_uri_can_be_pinned_with_clark_notation(self):
        xpath_match(QUERY_NS1, ["{{{}}}code".format(SVC_NS), "0000"])
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, ["{http://other.example.com/svc}code"])
        self.assertIn("未选中任何节点", str(ctx.exception))

    def test_attribute_predicate_ignores_attribute_prefix(self):
        self.assertTrue(xpath_match(ATTR_PREFIXED, "item[@id='7']"))
        self.assertTrue(xpath_match(ITEMS, "item[@id='2']"))
        with self.assertRaises(AssertionError):
            xpath_match(ITEMS, "item[@id='9']")

    def test_double_slash_prefix_is_equivalent(self):
        self.assertTrue(xpath_match(QUERY_NS1, ".//code"))
        self.assertTrue(xpath_match(QUERY_NS1, "code"))

    def test_bytes_and_text_inputs_are_equivalent(self):
        xpath_match(QUERY_NS1.encode("utf-8"), ["code", "0000"])
        xpath_match(QUERY_NS1, ["code", "0000"])

    def test_str_with_encoding_declaration_is_handled(self):
        """check 写 `text` 时拿到的是**已解码的 str**：声明里的 encoding 必须让路（否则 lxml 报错）。"""
        xpath_match(QUERY_NS1, ["code", "0000"])  # 声明写 UTF-8
        gbk_declared_str = '<?xml version="1.0" encoding="GBK"?><r><code>成功</code></r>'
        self.assertTrue(xpath_match(gbk_declared_str, ["code", "成功"]))

    def test_gbk_bytes_with_declaration_are_decoded_correctly(self):
        """bytes 直接交给 lxml → 按报文自己的 encoding 解码（A1 一律按 UTF-8 解码，这里更强）。"""
        gbk = '<?xml version="1.0" encoding="GBK"?><r><code>成功</code></r>'.encode("gbk")
        self.assertTrue(xpath_match(gbk, ["code", "成功"]))

    def test_non_text_input_is_rejected_with_type_name(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match({"code": "0000"}, ["code"])
        message = str(ctx.exception)
        self.assertIn("dict", message)
        self.assertIn("body", message)

    def test_text_assertion_on_collection_accepts_any_node(self):
        # 与 A1 一致：集合里**任一**节点的文本等于期望值即通过
        self.assertTrue(xpath_match(ITEMS, ["name", "乙"]))
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(ITEMS, ["name", "丁"])
        message = str(ctx.exception)
        self.assertIn("选中 3 个节点", message)
        self.assertIn("丁", message)


@NEEDS_LXML
class TestBomHandling(unittest.TestCase):
    """0919-2 / 缺陷 7：带 BOM 的报文必须与不带 BOM 的**等价**。

    ## 现场（实测）

    `_xml_bytes` 的「已解码 str」分支要摘掉 XML 声明里的 encoding
    （声明说 GBK、送出去的字节是 UTF-8 → lxml 会照声明解码 → 乱码 / `XMLSyntaxError`）。
    修复前那两行是：

    ```python
    if text[:5].lstrip("\\ufeff").lower().startswith("<?xml"):   # ① 判断（用的是窗口副本）
        text = _XML_DECL_ENCODING_RE.sub(r"\\1", text, count=1)    # ② 替换（作用于原串）
    ```

    `lstrip` **只作用在判断用的 5 字窗口**上，于是**两个独立原因**让声明活了下来：

    | # | 原因 | 实测证据 |
    |---|---|---|
    | ① | 5 字窗口剥掉 BOM 只剩 4 字 `<?xm`，永远 `startswith` 不了 5 字的 `<?xml` | `len("\\ufeff<?xml"[:5].lstrip("\\ufeff")) == 4` |
    | ② | 就算判断过了，正则也是 `^(<\\?xml…)` 锚定的，`^` 处是 BOM → 仍不匹配 | `sub` 对带 BOM 的串无任何改动 |

    后果：声明未被摘除 → lxml 报
    `XMLSyntaxError: Growing input buffer, line 1, column 37`（实测）。
    修法是一句：**先 `lstrip` 掉 BOM**，判断与替换作用在同一个已剥 BOM 的串上。
    """

    DECLARED = '<?xml version="1.0" encoding="GBK"?><r><code>成功</code></r>'
    BOM = "\ufeff"

    def test_bom_stripped_before_the_declaration_check(self):
        """两个根因一起钉住：剥 BOM 后必须认出「以 `<?xml` 开头」并摘掉 encoding。"""
        data = builtin_comparators._xml_bytes(self.BOM + self.DECLARED)

        self.assertNotIn(
            b"encoding=",
            data,
            "encoding 声明没有被摘除（缺陷 7）：声明说 GBK、送出去的字节是 UTF-8，"
            "lxml 会照声明解码 → 乱码或 XMLSyntaxError",
        )
        self.assertFalse(
            data.startswith(b"\xef\xbb\xbf"),
            "BOM 应当被剥掉（在已解码的 str 里它只是残留物）",
        )

    def test_bom_and_no_bom_produce_identical_bytes(self):
        """反向护栏：不带 BOM 的既有行为逐字节不变（这是绝大多数真实输入）。"""
        self.assertEqual(
            builtin_comparators._xml_bytes(self.DECLARED),
            builtin_comparators._xml_bytes(self.BOM + self.DECLARED),
        )

    def test_bom_payload_parses_and_asserts(self):
        """端到端：带 BOM 的报文能真的解析并通过断言（修复前这里是 XMLSyntaxError）。"""
        self.assertTrue(xpath_match(self.BOM + self.DECLARED, ["code", "成功"]))
        self.assertEqual(xpath_count(self.BOM + self.DECLARED, ["code", 1]), 1)

    def test_bom_bytes_are_untouched(self):
        """反向护栏：**bytes** 分支不剥 BOM —— 那是报文自己的字节，lxml 认它。"""
        raw = (self.BOM + self.DECLARED).encode("utf-8")
        self.assertEqual(builtin_comparators._xml_bytes(raw), raw)

    def test_bom_without_declaration_still_parses(self):
        """只有 BOM、没有声明：同样要能解析（BOM 不能留在交给 lxml 的串里）。"""
        self.assertTrue(
            xpath_match(self.BOM + "<r><code>0000</code></r>", ["code", "0000"])
        )

    def test_only_the_first_declaration_is_rewritten(self):
        """收口边界：只摘**首个**声明的 encoding，正文里的同名文本不得被误改。"""
        payload = (
            self.BOM + '<?xml version="1.0" encoding="GBK"?><r><t>encoding="GBK"</t></r>'
        )
        data = builtin_comparators._xml_bytes(payload)

        self.assertNotIn(b'<?xml version="1.0" encoding="GBK"?>', data)
        self.assertIn(b'encoding="GBK"', data)  # 正文里的那一份还在


@NEEDS_LXML
class TestRawXPath(unittest.TestCase):
    """原始 XPath 1.0：A1 明确拒绝的写法，这里**真的求值**。"""

    def test_element_prefix_from_document(self):
        self.assertTrue(xpath_match(QUERY_NS1, "//ns1:code"))
        self.assertTrue(xpath_match(QUERY_NS1, "//soap:Body/ns1:QueryResponse/ns1:code"))

    def test_inner_namespace_prefix_is_collected_from_full_tree(self):
        """`root.nsmap` 只有根上的 `soap`；`ns1` 声明在内层元素上 → 必须全树收集才收得到。"""
        self.assertTrue(xpath_match(QUERY_NS1, "//ns1:code/text()"))

    def test_text_and_string_functions(self):
        self.assertTrue(xpath_match(QUERY_NS1, ["//ns1:code/text()", "0000"]))
        self.assertTrue(xpath_match(QUERY_NS1, ["string(//ns1:code)", "0000"]))

    def test_count_function_in_both_operators(self):
        self.assertTrue(xpath_match(ITEMS, ["count(//ns1:item)", 3]))
        self.assertTrue(xpath_count(ITEMS, ["count(//ns1:item)", 3]))
        self.assertTrue(xpath_count(ITEMS, ["//ns1:item", "3"]))

    def test_axes_and_positional_predicates(self):
        self.assertTrue(xpath_match(FAULT, "//ns1:code/ancestor::soap:Fault"))
        self.assertTrue(xpath_match(ITEMS, ["(//ns1:item)[2]/ns1:name", "乙"]))

    def test_attribute_value_selection(self):
        self.assertTrue(xpath_match(ITEMS, ["//ns1:item[@id='2']/@id", "2"]))

    def test_local_name_escape_hatch_avoids_prefixes(self):
        for payload in (QUERY_NS1, QUERY_ABC, QUERY_DEFAULT_NS):
            with self.subTest(payload=payload[:40]):
                self.assertTrue(xpath_match(payload, "//*[local-name()='code']"))

    def test_raw_xpath_is_prefix_literal_not_prefix_agnostic(self):
        """原始 XPath 里前缀就是字面量：报文用 ns1 声明时 `//svc:code` 报未声明前缀。"""
        outcome, message = _outcome(xpath_match, QUERY_NS1, ["//svc:code"])
        self.assertEqual(outcome, "error")
        self.assertIn("Undefined namespace prefix", message)
        self.assertIn("简化写法", message)  # 提示要给出下一步

    def test_syntax_error_is_an_error_with_the_expression(self):
        with self.assertRaises(RuntimeError) as ctx:
            xpath_match(QUERY_NS1, "//ns1:code[")
        message = str(ctx.exception)
        self.assertIn("//ns1:code[", message)
        self.assertIn("XPath 表达式不合法", message)

    def test_default_namespace_needs_local_name(self):
        """XPath 1.0 没法给默认命名空间编前缀 → 无前缀的 `//code` 选不中，要用 local-name()。"""
        outcome, _ = _outcome(xpath_match, QUERY_DEFAULT_NS, ["//code"])
        self.assertEqual(outcome, "failed")
        self.assertTrue(xpath_match(QUERY_DEFAULT_NS, "//*[local-name()='code']"))
        self.assertTrue(xpath_match(QUERY_DEFAULT_NS, ["code", "0000"]))  # 简化写法不受影响

    def test_star_selects_children_of_context_node(self):
        # 原始语义：`*` 是「上下文节点的子元素」（根元素本身不算）
        self.assertTrue(xpath_match(QUERY_NS1, "*"))

    def test_empty_result_message_mentions_raw_expression(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, "//ns1:notExist")
        message = str(ctx.exception)
        self.assertIn("未选中任何节点", message)
        self.assertIn("原始 XPath", message)  # 只有原始写法才提示「改用简化写法」

    def test_simplified_failure_has_no_raw_xpath_hint(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, "notExist")
        self.assertNotIn("原始 XPath", str(ctx.exception))


@NEEDS_LXML
class TestCountAssertion(unittest.TestCase):

    def test_count_matches(self):
        self.assertTrue(xpath_count(ITEMS, ["item", 3]))
        self.assertTrue(xpath_count(ITEMS, ["//ns1:item", 3]))

    def test_count_mismatch_reports_both_numbers(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_count(ITEMS, ["item", 2])
        message = str(ctx.exception)
        self.assertIn("选中 3 个", message)
        self.assertIn("期望 2 个", message)

    def test_count_accepts_numeric_string(self):
        xpath_count(ITEMS, ["item", "3"])

    def test_count_rejects_bad_spec(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_count(ITEMS, ["item"])
        self.assertIn("[选择器, 个数]", str(ctx.exception))

    def test_count_rejects_non_numeric_expectation(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_count(ITEMS, ["item", "三条"])
        self.assertIn("必须是整数", str(ctx.exception))

    def test_count_rejects_non_node_non_number_expression(self):
        with self.assertRaises(RuntimeError) as ctx:
            xpath_count(ITEMS, ["string(//ns1:name)", 3])
        self.assertIn("节点集合", str(ctx.exception))


# 标量断言用的报文：`count()` 为 0、`boolean()` 为假、`string()` 为空串
EMPTY_ITEMS = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{soap}">
  <soap:Body>
    <ns1:ItemsResponse xmlns:ns1="{svc}">
      <ns1:flag>false</ns1:flag>
    </ns1:ItemsResponse>
  </soap:Body>
</soap:Envelope>
""".format(soap=SOAP_NS, svc=SVC_NS)

# `sum()` 会返回小数：用它验证 xpath_count 不再截断
SUM_ITEMS = """<?xml version="1.0" encoding="UTF-8"?>
<r>
  <n>1</n>
  <n>2</n>
  <n>0.5</n>
</r>
"""


@NEEDS_LXML
class TestScalarResultAssertions(unittest.TestCase):
    """0918-8 / L16：标量结果（`0` / `false` / 空串）必须能断言，且布尔按 XPath 拼写渲染。

    NOTICE: 修复前 `xpath_match` 对**任何**结果都先跑存在性门禁（`_xml_matched`），
    于是「个数为 0」「条件为假」「文本为空」这三类合法断言统统被报成
    「未选中任何节点」——表达式明明求值成功了，诊断却与事实相反。
    """

    def test_zero_count_can_be_asserted(self):
        self.assertTrue(xpath_match(EMPTY_ITEMS, ["count(//ns1:item)", "0"]))

    def test_false_boolean_can_be_asserted_with_xpath_spelling(self):
        """布尔按 XPath 规范渲染：期望值写 `false`（不是 Python 的 `False`）。"""
        self.assertTrue(xpath_match(EMPTY_ITEMS, ["boolean(//ns1:item)", "false"]))

    def test_true_boolean_is_rendered_as_lowercase(self):
        self.assertTrue(xpath_match(ITEMS, ["boolean(//ns1:item)", "true"]))

    def test_python_spelling_no_longer_matches(self):
        """反向断言：`False` 是 Python 拼写，不该再"恰好通过"。"""
        with self.assertRaises(AssertionError):
            xpath_match(EMPTY_ITEMS, ["boolean(//ns1:item)", "False"])

    def test_empty_string_result_can_be_asserted(self):
        self.assertTrue(xpath_match(EMPTY_ITEMS, ["string(//ns1:missing)", ""]))

    def test_nonzero_scalar_still_works(self):
        self.assertTrue(xpath_match(ITEMS, ["count(//ns1:item)", "3"]))

    def test_empty_node_set_still_fails_as_not_selected(self):
        """回归：**节点集合为空**仍然要报「未选中任何节点」（这条门禁是对的）。"""
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(ITEMS, ["missing"])

        self.assertIn("未选中任何节点", str(ctx.exception))

    def test_scalar_with_only_existence_expectation_gives_actionable_error(self):
        """标量 + 只写「存在性」是写法错误：要给出可照抄的两元素写法。"""
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(EMPTY_ITEMS, "count(//ns1:item)")

        message = str(ctx.exception)
        self.assertIn("不是节点集合", message)
        self.assertIn("[表达式, 期望值]", message)
        self.assertNotIn("未选中任何节点", message)


@NEEDS_LXML
class TestCountDoesNotTruncateFloats(unittest.TestCase):
    """0918-8 / L17：`xpath_count` 不能把小数截断成整数（3.5 → 3 会假通过）。"""

    def test_fractional_sum_is_rejected(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_count(SUM_ITEMS, ["sum(//n)", 3])

        message = str(ctx.exception)
        self.assertIn("3.5", message)
        self.assertIn("xpath_match", message)

    def test_integral_float_is_accepted(self):
        """`sum(//n)` = 4.0（整数值的 float，如 `number()`/`count()` 的返回类型）照常可比。"""
        self.assertTrue(xpath_count(SUM_ITEMS, ["sum(//n[position() < 3])", 3]))

    def test_integer_result_unchanged(self):
        self.assertTrue(xpath_count(ITEMS, ["item", 3]))

    def test_count_of_zero_still_works(self):
        self.assertTrue(xpath_count(EMPTY_ITEMS, ["item", 0]))


@NEEDS_LXML
class TestErrorMessages(unittest.TestCase):

    def test_invalid_xml_is_reported_loudly(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(BROKEN, ["code", "0000"])
        message = str(ctx.exception)
        self.assertIn("不是合法 XML", message)
        self.assertIn("原文前", message)

    def test_empty_expectation_is_rejected(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, [])
        self.assertIn("不能是空列表", str(ctx.exception))

    def test_empty_selector_is_rejected(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, "")
        self.assertIn("不能为空", str(ctx.exception))

    def test_message_is_appended_on_failure(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, ["code", "9999"], "业务码不对，找开发确认")
        self.assertIn("业务码不对，找开发确认", str(ctx.exception))

    def test_message_is_appended_on_count_failure(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_count(ITEMS, ["item", 9], "明细条数不对")
        self.assertIn("明细条数不对", str(ctx.exception))

    def test_scalar_mismatch_message_shows_expression(self):
        with self.assertRaises(AssertionError) as ctx:
            xpath_match(QUERY_NS1, ["string(//ns1:code)", "9999"])
        message = str(ctx.exception)
        self.assertIn("求值结果不是", message)
        self.assertIn("0000", message)


@NEEDS_LXML
class TestSelectorCompilation(unittest.TestCase):
    """编译表逐条钉死：这是「前缀漂移也能命中」的全部依据（lxml 不认 Clark 记法）。"""

    def test_compilation_table(self):
        """编译表（批次 C / L13 起改成 `descendant-or-self`，并**丢掉前缀**）。

        NOTICE（两处刻意的行为变更，护栏在这里被改写而不是被绕过）：
          1. `.//*[...]` → `descendant-or-self::*[...]`：修复前是**后代**搜索，
             **不含上下文节点本身**，于是「响应根元素就是业务元素」时简化写法
             **永远选不中根元素** —— 而 `xml_schema_match` 文档推荐的
             `{"xpath": "QueryResponse"}` 写法恰好踩这个坑（假失败）；
          2. 带前缀的名字（`svc:code`）修复前被整串当成节点名 →
             `local-name()='svc:code'`，而真实的 `local-name()` 是 `code` → **永远不命中**。
             现在丢掉前缀，与「前缀漂移也能命中」这个简化写法的本意一致。
        """
        cases = {
            "code": "descendant-or-self::*[local-name()='code']",
            ".//code": "descendant-or-self::*[local-name()='code']",
            # 批次 C / L13-②：带前缀 → 丢掉前缀按 local-name 比对（修复前恒不命中）
            "svc:code": "descendant-or-self::*[local-name()='code']",
            ".//svc:code": "descendant-or-self::*[local-name()='code']",
            "abc:item[@id='1']": (
                "descendant-or-self::*[local-name()='item']"
                "[@*[local-name()='id']='1']"
            ),
            "{urn:x}code": (
                "descendant-or-self::*[local-name()='code' and namespace-uri()='urn:x']"
            ),
            "{urn:x}svc:code": (
                "descendant-or-self::*[local-name()='code' and namespace-uri()='urn:x']"
            ),
            "item[@id='1']": (
                "descendant-or-self::*[local-name()='item']"
                "[@*[local-name()='id']='1']"
            ),
            "{urn:x}item[@id='1']": (
                "descendant-or-self::*[local-name()='item' and namespace-uri()='urn:x']"
                "[@*[local-name()='id']='1']"
            ),
        }
        for selector, expected in cases.items():
            with self.subTest(selector=selector):
                self.assertEqual(_xml_selector_to_xpath(selector), expected)

    def test_non_simplified_selectors_return_none(self):
        for selector in (
            "//s:code/text()",
            "a/b",
            "*",
            "text()",
            "item[@id=2]",
            "{urn:x",
            "..",
            "/x",
            "count(//a)",
            "",
        ):
            with self.subTest(selector=selector):
                self.assertIsNone(_xml_selector_to_xpath(selector))

    def test_literal_escaping(self):
        self.assertEqual(_xpath_literal("a"), "'a'")
        self.assertEqual(_xpath_literal("a'b"), '"a\'b"')
        self.assertEqual(_xpath_literal('a"b'), "'a\"b'")
        self.assertEqual(_xpath_literal("a'b\"c"), "concat('a', \"'\", 'b\"c')")


@NEEDS_LXML
class TestParityWithA1StdlibOperators(unittest.TestCase):
    """A1（标准库实现）与内核简化写法**同语义**的证据：同一批输入，结论必须一致。

    这是「把 A1 换成内核不改变既有用例行为」的回归依据（评估文档 2.6.2「A1 用户不受影响」）。
    唯一有意的分歧单列在 `TestIntentionalDivergence`。
    """

    @classmethod
    def setUpClass(cls):
        cls.a1 = _load_a1_operators()

    MATCH_CASES = [
        (QUERY_NS1, ["code", "0000"]),
        (QUERY_NS1, ["amount", "1234.50"]),
        (QUERY_NS1, "message"),
        (QUERY_NS1, "notExist"),
        (QUERY_NS1, ["code", "9999"]),
        (QUERY_NS1, []),
        (QUERY_ABC, ["code", "0000"]),
        (QUERY_DEFAULT_NS, ["code", "0000"]),
        (QUERY_NS1, "{{{}}}code".format(SVC_NS)),
        (QUERY_NS1, ["{http://other.example.com/svc}code"]),
        (ITEMS, "item[@id='2']"),
        (ITEMS, "item[@id='9']"),
        (ATTR_PREFIXED, "item[@id='7']"),
        (QUERY_NS1, ".//code"),
        # 批次 C / L13：**带前缀的选择器**（修复前编译成 local-name()='ns1:code' → 恒不命中）
        (QUERY_NS1, ["ns1:code", "0000"]),
        (QUERY_NS1, [".//ns1:code", "0000"]),
        (QUERY_NS1, "abc:message"),  # 前缀与报文实际声明不同，仍按 local-name 命中
        # 批次 C / L13：**根元素就是业务元素**（修复前 `.//*` 不含上下文节点 → 选不中）
        (ROOT_IS_BUSINESS, "QueryResponse"),
        (ROOT_IS_BUSINESS, ["QueryResponse", None]),
        (ROOT_IS_BUSINESS, "code"),
        (QUERY_NS1.encode("utf-8"), ["code", "0000"]),
        ({"code": "0000"}, ["code"]),
        (BROKEN, ["code", "0000"]),
    ]

    COUNT_CASES = [
        (ITEMS, ["item", 3]),
        (ITEMS, ["item", 2]),
        (ITEMS, ["item", "3"]),
        (ITEMS, ["item"]),
        (ITEMS, ["item", "三条"]),
    ]

    def test_match_outcomes_are_identical(self):
        for payload, spec in self.MATCH_CASES:
            with self.subTest(spec=spec):
                a1_outcome = _outcome(self.a1.xpath_match, payload, spec)
                kernel_outcome = _outcome(xpath_match, payload, spec)
                self.assertEqual(a1_outcome[0], kernel_outcome[0], kernel_outcome[1])

    def test_count_outcomes_are_identical(self):
        for payload, spec in self.COUNT_CASES:
            with self.subTest(spec=spec):
                a1_outcome = _outcome(self.a1.xpath_count, payload, spec)
                kernel_outcome = _outcome(xpath_count, payload, spec)
                self.assertEqual(a1_outcome[0], kernel_outcome[0], kernel_outcome[1])

    def test_failure_messages_keep_the_same_wording(self):
        """报错文案也要一致（客户的失败排查话术与文档截图都依赖它）。"""
        cases = [
            ("xpath_match", QUERY_NS1, ["code", "9999"]),
            ("xpath_match", QUERY_NS1, ["notExist"]),
            ("xpath_count", ITEMS, ["item", 2]),
        ]
        for func_name, payload, spec in cases:
            with self.subTest(func=func_name, spec=spec):
                a1_message = _outcome(getattr(self.a1, func_name), payload, spec)[1]
                kernel_message = _outcome(globals()[func_name], payload, spec)[1]
                self.assertEqual(a1_message.splitlines()[0], kernel_message.splitlines()[0])


@NEEDS_LXML
class TestIntentionalDivergence(unittest.TestCase):
    """A1 明确拒绝、内核升级为「真的求值」的写法（A2-1 的**全部**增量就在这里）。"""

    @classmethod
    def setUpClass(cls):
        cls.a1 = _load_a1_operators()

    DIVERGENT_SELECTORS = ["a/b", "..", "/x", "item[@id=2]", "*", "text()"]

    def test_a1_rejects_them_loudly(self):
        for selector in self.DIVERGENT_SELECTORS:
            with self.subTest(selector=selector):
                outcome, message = _outcome(self.a1.xpath_match, QUERY_NS1, [selector])
                self.assertEqual(outcome, "failed")
                self.assertIn("不支持", message)

    def test_kernel_evaluates_them_as_real_xpath(self):
        # `*` / `text()` 能选中（根的子元素、根元素的白空格子文本）；
        # `/x`、`a/b` 是合法 XPath 但选不中 → 断言失败（而不是报错）
        self.assertTrue(xpath_match(QUERY_NS1, "*"))
        self.assertTrue(xpath_match(QUERY_NS1, "text()"))
        for selector in ("/x", "a/b"):
            with self.subTest(selector=selector):
                outcome, message = _outcome(xpath_match, QUERY_NS1, [selector])
                self.assertEqual(outcome, "failed")
                self.assertIn("未选中任何节点", message)

    def test_unquoted_attribute_value_is_valid_raw_xpath(self):
        """A1 报「不支持」的 `[@id=2]`，在原始 XPath 里是**合法的数值比较**，真的会命中。"""
        self.assertTrue(xpath_match(ITEMS, "//ns1:item[@id=2]"))
        with self.assertRaises(AssertionError):
            xpath_match(ITEMS, "//ns1:item[@id=9]")

    def test_broken_predicate_is_an_error(self):
        outcome, message = _outcome(xpath_match, ITEMS, ["//ns1:item[@id=]"])
        self.assertEqual(outcome, "error")
        self.assertIn("XPath 表达式不合法", message)

    def test_incomplete_clark_notation_becomes_an_xpath_error(self):
        outcome, message = _outcome(xpath_match, QUERY_NS1, ["{urn:x"])
        self.assertEqual(outcome, "error")
        self.assertIn("XPath 表达式不合法", message)


# --------------------------------------------------------------------------- XSD（A2-2）
# 直接复用**示例工程里的 XSD**（`examples/soap_xpath/schemas/`）—— 交付件即回归样本，
# 与 `tests/soap_assert_test.py` 加载 `examples/soap/debugtalk.py` 同一思路。
EXAMPLES_XSD_DIR = os.path.join(REPO_ROOT, "examples", "soap_xpath", "schemas")
QUERY_XSD = os.path.join(EXAMPLES_XSD_DIR, "query.xsd")
ITEMS_XSD = os.path.join(EXAMPLES_XSD_DIR, "items.xsd")

QUERY_BAD_CODE = QUERY_NS1.replace("0000", "1002")  # 违反 common.xsd 的 pattern 0[0-9]{3}
QUERY_NO_AMOUNT = QUERY_NS1.replace("      <ns1:amount>1234.50</ns1:amount>\n", "")

FIELD_TEMPLATE = (
    '        <xs:element name="f{i}"><xs:simpleType>'
    '<xs:restriction base="xs:int"><xs:minInclusive value="10"/></xs:restriction>'
    "</xs:simpleType></xs:element>"
)
XSD_MANY_ERRORS = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="R">
    <xs:complexType>
      <xs:sequence>
{fields}
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
""".format(fields="\n".join(FIELD_TEMPLATE.format(i=i) for i in range(1, 8)))
MANY_ERRORS_XML = "<R>" + "".join(f"<f{i}>0</f{i}>" for i in range(1, 8)) + "</R>"


_XSD_FIXTURE_DIRS: list = []


def _make_xsd_fixture_dir() -> str:
    """建一个放「坏 XSD」的临时目录（放在 logs/ 下：已被 .gitignore 覆盖，也不会被 pytest 收集）。

    NOTICE（0918-5）：修复前这里**从不清理**，每跑一次测试就往 `logs/` 里累积一批
    `xsd_fixtures_*`（清理时实测已堆积 **100 个**）。与 `comparators_test.py` 的
    `tmp_js_*` 是同一类问题，所以同样在模块收尾时统一删除——用模块级记账 + `tearDownModule`
    覆盖全部调用点，不必逐个用例加 cleanup。
    """
    fixture_dir = os.path.join(os.getcwd(), "logs", f"xsd_fixtures_{uuid.uuid4().hex[:8]}")
    os.makedirs(fixture_dir, exist_ok=True)
    _XSD_FIXTURE_DIRS.append(fixture_dir)
    return fixture_dir


def tearDownModule():
    """本模块跑完统一清理临时 fixture 目录（unittest/pytest 都会调用它）。"""
    while _XSD_FIXTURE_DIRS:
        shutil.rmtree(_XSD_FIXTURE_DIRS.pop(), ignore_errors=True)


@NEEDS_LXML
class TestXmlSchemaSpec(unittest.TestCase):
    """第二个参数的两条引用路与形态校验（**dict 传参是刻意的**：`xpath` 与 `xsd` 的顺序不该靠记忆）。"""

    def test_string_form_validates_the_whole_response_root(self):
        payload = '<QueryResponse xmlns="{ns}"><code>0000</code></QueryResponse>'.format(
            ns=SVC_NS
        )
        xsd = os.path.join(_make_xsd_fixture_dir(), "root.xsd")
        with open(xsd, "w", encoding="utf-8") as fp:
            fp.write(
                '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
                'xmlns:tns="{ns}" targetNamespace="{ns}" elementFormDefault="qualified">'
                '<xs:element name="QueryResponse"><xs:complexType><xs:sequence>'
                '<xs:element name="code" type="xs:string"/>'
                "</xs:sequence></xs:complexType></xs:element></xs:schema>".format(ns=SVC_NS)
            )
        self.assertTrue(xml_schema_match(payload, xsd))

    def test_dict_form_locates_then_validates(self):
        self.assertTrue(
            xml_schema_match(QUERY_NS1, {"xpath": "QueryResponse", "xsd": QUERY_XSD})
        )

    def test_dict_form_accepts_raw_xpath_too(self):
        self.assertTrue(
            xml_schema_match(QUERY_NS1, {"xpath": "//ns1:QueryResponse", "xsd": QUERY_XSD})
        )

    def test_empty_string_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(QUERY_NS1, "")
        self.assertIn("不能是空字符串", str(ctx.exception))

    def test_inline_xsd_string_is_rejected_with_reason(self):
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(QUERY_NS1, '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>')
        message = str(ctx.exception)
        self.assertIn("不支持内联 XSD 字符串", message)
        self.assertIn("xs:include", message)  # 要说清「为什么只能文件」

    def test_dict_without_xsd_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(QUERY_NS1, {"xpath": "QueryResponse"})
        self.assertIn("必须带 `xsd`", str(ctx.exception))

    def test_dict_with_unknown_key_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(
                QUERY_NS1, {"xsd": QUERY_XSD, "xsdPath": "typo.xsd"}
            )
        message = str(ctx.exception)
        self.assertIn("只支持 `xpath` 与 `xsd`", message)
        self.assertIn("xsdPath", message)  # 指出是哪个键写错了

    def test_non_string_xpath_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(QUERY_NS1, {"xpath": ["//a"], "xsd": QUERY_XSD})
        self.assertIn("`xpath` 必须是字符串", str(ctx.exception))

    def test_wrong_expect_type_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(QUERY_NS1, ["schemas/query.xsd"])
        self.assertIn("必须是 XSD 文件路径（str）", str(ctx.exception))

    def test_relative_path_resolves_against_project_root(self):
        """相对路径按 **RootDir（含 debugtalk.py 的那层）** 解析 —— 复用 P2-b 的同一份逻辑。"""
        with mock.patch.object(
            builtin_comparators, "_project_root_dir", return_value=EXAMPLES_XSD_DIR
        ):
            self.assertTrue(
                xml_schema_match(QUERY_NS1, {"xpath": "QueryResponse", "xsd": "query.xsd"})
            )

    def test_missing_file_lists_every_attempted_path(self):
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(QUERY_NS1, "schemas/notExist.xsd")
        message = str(ctx.exception)
        self.assertIn("XSD 文件不存在", message)
        self.assertIn("已尝试", message)
        self.assertIn("notExist.xsd", message)
        self.assertIn("xs:include", message)  # XSD 专用提示（不是 JSON Schema 那句）


@NEEDS_LXML
class TestXmlSchemaLocating(unittest.TestCase):
    """「先定位子节点再校验」—— 客户 XSD 通常只描述 Body 里那层元素（A2 原计划漏掉的必需项）。"""

    def test_whole_envelope_fails_but_located_node_passes(self):
        whole = _outcome(
            xml_schema_match, QUERY_NS1, {"xsd": QUERY_XSD}
        )  # 无 xpath → 校验整个 Envelope
        self.assertEqual(whole[0], "failed")
        self.assertIn("No matching global declaration", whole[1])

        self.assertTrue(
            xml_schema_match(QUERY_NS1, {"xpath": "QueryResponse", "xsd": QUERY_XSD})
        )

    def test_selector_matching_nothing_is_a_failure_with_hint(self):
        with self.assertRaises(AssertionError) as ctx:
            xml_schema_match(QUERY_NS1, {"xpath": "notExist", "xsd": QUERY_XSD})
        message = str(ctx.exception)
        self.assertIn("未选中任何节点", message)
        self.assertIn("XSD 校验要先定位到业务元素", message)

    def test_multiple_nodes_are_rejected_instead_of_picking_the_first(self):
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(ITEMS, {"xpath": "//ns1:item", "xsd": ITEMS_XSD})
        message = str(ctx.exception)
        self.assertIn("选中了 3 个节点", message)
        self.assertIn("唯一", message)

    def test_positional_predicate_narrows_to_a_single_node(self):
        self.assertTrue(
            xml_schema_match(ITEMS, {"xpath": "(//ns1:item)[2]", "xsd": ITEMS_XSD})
        )

    def test_scalar_expression_is_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(ITEMS, {"xpath": "count(//ns1:item)", "xsd": ITEMS_XSD})
        self.assertIn("必须选出**元素节点**", str(ctx.exception))

    def test_attribute_node_is_rejected(self):
        # 收窄到**唯一**节点，才能走到「不是元素节点」这个分支（多节点会先被上面那条拦住）
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(ITEMS, {"xpath": "(//ns1:item)[1]/@id", "xsd": ITEMS_XSD})
        self.assertIn("不是元素节点", str(ctx.exception))

    def test_multiple_attribute_nodes_hit_the_uniqueness_check_first(self):
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(ITEMS, {"xpath": "//ns1:item/@id", "xsd": ITEMS_XSD})
        self.assertIn("选中了 3 个节点", str(ctx.exception))

    def test_bad_xpath_is_an_error_not_a_failure(self):
        outcome, message = _outcome(
            xml_schema_match, QUERY_NS1, {"xpath": "//svc:QueryResponse", "xsd": QUERY_XSD}
        )
        self.assertEqual(outcome, "error")
        self.assertIn("XPath 表达式不合法", message)
        self.assertIn("Undefined namespace prefix", message)

    def test_duplicated_selector_syntax_is_shared_with_xpath_match(self):
        """简化写法与 `xpath_match` **完全一致**（前缀无关）：同一份契约换个前缀照样命中。"""
        self.assertTrue(
            xml_schema_match(QUERY_ABC, {"xpath": "QueryResponse", "xsd": QUERY_XSD})
        )


@NEEDS_LXML
class TestXmlSchemaValidation(unittest.TestCase):
    """校验结果与失败信息：**通过**必须真通过，**失败**必须指到节点且给出原因。"""

    def test_passing_document(self):
        self.assertTrue(
            xml_schema_match(QUERY_NS1, {"xpath": "QueryResponse", "xsd": QUERY_XSD})
        )
        self.assertTrue(
            xml_schema_match(ITEMS, {"xpath": "ItemsResponse", "xsd": ITEMS_XSD})
        )

    def test_include_is_resolved_relative_to_the_xsd_file(self):
        """`common.xsd` 里的 `CodeType` 是通过 `<xs:include>` 引进来的 —— 违例能报出来就说明 include 生效了。"""
        with self.assertRaises(AssertionError) as ctx:
            xml_schema_match(QUERY_BAD_CODE, {"xpath": "QueryResponse", "xsd": QUERY_XSD})
        message = str(ctx.exception)
        self.assertIn("pattern", message)
        self.assertIn("1002", message)  # 违例的实际值

    def test_failure_reports_node_path_and_reason(self):
        with self.assertRaises(AssertionError) as ctx:
            xml_schema_match(QUERY_BAD_CODE, {"xpath": "QueryResponse", "xsd": QUERY_XSD})
        message = str(ctx.exception)
        self.assertIn("XSD 校验失败：共 1 处不符合（已定位：QueryResponse）", message)
        self.assertIn("XSD：", message)
        self.assertIn("query.xsd", message)
        # 出错节点的路径（用**报文自己的前缀**）+ 自然语言原因
        self.assertIn("/ns1:QueryResponse/ns1:code", message)
        self.assertIn("facet 'pattern'", message)

    def test_missing_child_element_points_at_the_parent(self):
        with self.assertRaises(AssertionError) as ctx:
            xml_schema_match(QUERY_NO_AMOUNT, {"xpath": "QueryResponse", "xsd": QUERY_XSD})
        message = str(ctx.exception)
        self.assertIn("Missing child element", message)
        self.assertIn("amount", message)
        self.assertIn("/ns1:QueryResponse", message)

    def test_prefix_drift_does_not_affect_validation(self):
        """XSD 按**命名空间 URI** 匹配 → 报文前缀从 ns1: 漂移成 abc: 照样通过。"""
        self.assertTrue(
            xml_schema_match(QUERY_ABC, {"xpath": "QueryResponse", "xsd": QUERY_XSD})
        )

    def test_namespace_must_be_declared_in_the_xsd(self):
        """没有 targetNamespace 的 XSD 去校验带命名空间的报文 → 失败（不是静默通过）。"""
        xsd = os.path.join(_make_xsd_fixture_dir(), "no_target_ns.xsd")
        with open(xsd, "w", encoding="utf-8") as fp:
            fp.write(
                '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
                '<xs:element name="QueryResponse" type="xs:string"/></xs:schema>'
            )
        with self.assertRaises(AssertionError) as ctx:
            xml_schema_match(QUERY_NS1, {"xpath": "QueryResponse", "xsd": xsd})
        self.assertIn("No matching global declaration", str(ctx.exception))

    def test_custom_message_is_prepended(self):
        with self.assertRaises(AssertionError) as ctx:
            xml_schema_match(
                QUERY_BAD_CODE,
                {"xpath": "QueryResponse", "xsd": QUERY_XSD},
                "业务码不符合契约",
            )
        lines = str(ctx.exception).splitlines()
        self.assertEqual(lines[0], "业务码不符合契约")  # 前置，不挤掉后面的详细信息
        self.assertIn("XSD 校验失败", str(ctx.exception))

    def test_many_errors_are_limited_and_counted(self):
        xsd = os.path.join(_make_xsd_fixture_dir(), "many.xsd")
        with open(xsd, "w", encoding="utf-8") as fp:
            fp.write(XSD_MANY_ERRORS)
        with self.assertRaises(AssertionError) as ctx:
            xml_schema_match(MANY_ERRORS_XML, xsd)
        message = str(ctx.exception)
        self.assertIn("共 7 处不符合", message)
        self.assertIn("另有 2 处不符合（已省略）", message)
        self.assertEqual(len([line for line in message.splitlines() if line.startswith("- ")]), 6)

    def test_non_xml_and_non_text_inputs_are_reported(self):
        outcome, message = _outcome(xml_schema_match, {"a": 1}, {"xsd": QUERY_XSD})
        self.assertEqual(outcome, "failed")
        self.assertIn("dict", message)

        outcome, message = _outcome(xml_schema_match, "<soap:Envelope", {"xsd": QUERY_XSD})
        self.assertEqual(outcome, "failed")
        self.assertIn("不是合法 XML", message)


@NEEDS_LXML
class TestXmlSchemaFileErrors(unittest.TestCase):
    """XSD 自身的问题 → **RuntimeError**（用例/环境问题），与 P2-b 的失败分界一致。"""

    def test_xsd_that_is_not_well_formed(self):
        xsd = os.path.join(_make_xsd_fixture_dir(), "broken.xsd")
        with open(xsd, "w", encoding="utf-8") as fp:
            fp.write('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element')
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(QUERY_NS1, {"xpath": "QueryResponse", "xsd": xsd})
        self.assertIn("不是合法 XML", str(ctx.exception))

    def test_xsd_that_is_itself_invalid(self):
        xsd = os.path.join(_make_xsd_fixture_dir(), "invalid.xsd")
        with open(xsd, "w", encoding="utf-8") as fp:
            fp.write(
                '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
                '<xs:element name="r" type="xs:notAType"/></xs:schema>'
            )
        with self.assertRaises(RuntimeError) as ctx:
            xml_schema_match(QUERY_NS1, {"xpath": "QueryResponse", "xsd": xsd})
        message = str(ctx.exception)
        self.assertIn("XSD 自身不合法，无法编译", message)
        self.assertIn("notAType", message)  # 具体原因（来自 error_log）
        self.assertIn("常见原因", message)  # 给下一步

    def test_missing_lxml_is_reported_with_install_hint(self):
        builtin_comparators._XSD_SCHEMA_CACHE.clear()  # 防止命中别的用例编译好的缓存
        with self._hide_lxml():
            with self.assertRaises(RuntimeError) as ctx:
                xml_schema_match(QUERY_NS1, {"xpath": "QueryResponse", "xsd": QUERY_XSD})
        message = str(ctx.exception)
        self.assertIn("未安装 lxml", message)
        self.assertIn(".[xml]", message)

    def _hide_lxml(self):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "lxml" or name.startswith("lxml."):
                raise ImportError("No module named 'lxml'")
            return real_import(name, *args, **kwargs)

        return mock.patch.object(builtins, "__import__", side_effect=fake_import)


@NEEDS_LXML
class TestXmlSchemaCaching(unittest.TestCase):
    """编译结果按**绝对路径**缓存：实测带 `xs:include` 的 XSD 首次编译要 ~15 ms。"""

    def setUp(self):
        builtin_comparators._XSD_SCHEMA_CACHE.clear()

    def tearDown(self):
        builtin_comparators._XSD_SCHEMA_CACHE.clear()

    def test_same_path_is_compiled_once(self):
        expect = {"xpath": "QueryResponse", "xsd": QUERY_XSD}
        xml_schema_match(QUERY_NS1, expect)
        first = list(builtin_comparators._XSD_SCHEMA_CACHE.values())[0]

        xml_schema_match(QUERY_NS1, expect)
        self.assertEqual(len(builtin_comparators._XSD_SCHEMA_CACHE), 1)
        self.assertIs(list(builtin_comparators._XSD_SCHEMA_CACHE.values())[0], first)

    def test_cache_key_is_the_resolved_absolute_path(self):
        xml_schema_match(QUERY_NS1, {"xpath": "QueryResponse", "xsd": QUERY_XSD})
        self.assertEqual(
            list(builtin_comparators._XSD_SCHEMA_CACHE), [os.path.abspath(QUERY_XSD)]
        )

    def test_different_files_are_cached_separately(self):
        xml_schema_match(QUERY_NS1, {"xpath": "QueryResponse", "xsd": QUERY_XSD})
        xml_schema_match(ITEMS, {"xpath": "ItemsResponse", "xsd": ITEMS_XSD})
        self.assertEqual(len(builtin_comparators._XSD_SCHEMA_CACHE), 2)


class TestMissingLxml(unittest.TestCase):
    """没装 extra 时必须给**可操作的安装提示**，而不是 ModuleNotFoundError 堆栈。"""

    def _hide_lxml(self):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "lxml" or name.startswith("lxml."):
                raise ImportError("No module named 'lxml'")
            return real_import(name, *args, **kwargs)

        return mock.patch.object(builtins, "__import__", side_effect=fake_import)

    def test_match_reports_install_hint(self):
        with self._hide_lxml():
            with self.assertRaises(RuntimeError) as ctx:
                xpath_match("<r><code>0000</code></r>", ["code"])
        message = str(ctx.exception)
        self.assertIn("未安装 lxml", message)
        self.assertIn(".[xml]", message)
        self.assertIn("examples/soap/debugtalk.py", message)  # 退路要说清

    def test_count_reports_install_hint(self):
        with self._hide_lxml():
            with self.assertRaises(RuntimeError) as ctx:
                xpath_count("<r/>", ["item", 0])
        self.assertIn("未安装 lxml", str(ctx.exception))

    def test_wrong_check_type_wins_over_missing_dependency(self):
        """check 写错（拿到 dict）时报类型错 —— 不该被「缺 lxml」掩盖成环境问题。"""
        with self._hide_lxml():
            with self.assertRaises(AssertionError) as ctx:
                xpath_match({"code": "0000"}, ["code"])
        self.assertIn("dict", str(ctx.exception))


class TestOperatorRegistration(unittest.TestCase):
    """两个新算子要能被白名单 / 生成期校验 / 运行期分发认出来。"""

    def test_names_are_derived_into_whitelist(self):
        self.assertIn("xpath_match", BUILTIN_COMPARATOR_NAMES)
        self.assertIn("xpath_count", BUILTIN_COMPARATOR_NAMES)
        self.assertIn("xml_schema_match", BUILTIN_COMPARATOR_NAMES)

    def test_helpers_stay_private(self):
        public = {
            name
            for name, value in vars(builtin_comparators).items()
            if callable(value)
            and not isinstance(value, type)
            and not name.startswith("_")
            and not hasattr(value, "__path__")
        }
        for helper in (
            "_lxml_etree",
            "_xml_bytes",
            "_xml_root",
            "_xml_namespaces",
            "_xpath_literal",
            "_xml_selector_to_xpath",
            "_xml_xpath",
            "_xml_xpath_on_root",
            "_xml_node_text",
            "_xml_scalar_text",
            "_xml_matched",
            "_xml_empty_hint",
            "_xml_spec",
            "_load_xsd",
            "_xsd_spec",
            "_xml_schema_target",
            "_xml_schema_errors",
        ):
            with self.subTest(helper=helper):
                self.assertTrue(hasattr(builtin_comparators, helper))
                self.assertNotIn(helper, public)

    def test_explicit_assert_methods_build_expected_validators(self):
        validation = RunRequest("xpath probe").get("/get").validate()
        self.assertIsInstance(validation, StepRequestValidation)

        validation.assert_xpath_match("body", ["code", "0000"]).assert_xpath_count(
            "body", ["item", 3]
        ).assert_xml_schema_match(
            "body", {"xpath": "QueryResponse", "xsd": "schemas/query.xsd"}
        )

        self.assertEqual(
            validation.struct().validators,
            [
                {"xpath_match": ["body", ["code", "0000"], ""]},
                {"xpath_count": ["body", ["item", 3], ""]},
                {
                    "xml_schema_match": [
                        "body",
                        {"xpath": "QueryResponse", "xsd": "schemas/query.xsd"},
                        "",
                    ]
                },
            ],
        )

    def test_explicit_methods_carry_their_own_docstrings(self):
        """显式方法的意义之一就是文档落点：动态分发那份的说明是通用的，这里必须是个性化的。"""
        for name in ("assert_xpath_match", "assert_xpath_count", "assert_xml_schema_match"):
            with self.subTest(method=name):
                doc = getattr(StepRequestValidation, name).__doc__
                self.assertIn("xml", doc)  # 提到 extra
                self.assertNotIn("dynamic assert method", doc)


if __name__ == "__main__":
    unittest.main()
