"""SOAP/XML 结构化断言算子（A1 方案交付件：**标准库实现、零新依赖、零内核改动**）。

把你需要的部分**整段复制**到自己项目的 `debugtalk.py` 里即可；框架不要求改动任何源码
（自定义算子的前置修复即 P0，已在 4.3.5 落地：`assert_<算子名>` 会被动态分发，
`hmake` 阶段会用「内置算子 + 项目函数表」校验算子名）。

**为什么需要它**：XML 响应的 `body` 不是 JSON，框架走的是 `resp.content` → 是 **bytes**，
所以 `contains` / `regex_match` / `startswith` 这些字符串算子在它上面不可用
（`contains` 会抛 `TypeError: a bytes-like object is required, not 'str'`）。
用这里的算子做结构化断言，顺带解决三件事：

1. **命名空间前缀漂移**：按 local-name 选节点 → `ns1:` / `abc:` / `soap:` 都不影响；
2. **报文不合法要立刻报错**：非法 XML 会以「响应体不是合法 XML」失败，而不是静默放过；
3. **SOAP 的业务错误也是 HTTP 500**：`soap_fault` 让「参数校验失败」不会被误判成「服务挂了」。

**支持的断言写法**（`check_value` 统一写响应体路径 `body`）

```yaml
validate:
  - xpath_match: ["body", "code"]                              # 选中节点即可
  - xpath_match: ["body", ["code", "0000"]]                    # 再断文本
  - xpath_match: ["body", ["item[@id='2']"]]                   # 属性选择器
  - xpath_match: ["body", ["{http://demo.example.com/svc}code", "0000"]]  # 按命名空间 URI 精确限定
  - xpath_count: ["body", ["item", 3]]                         # 个数断言
  - soap_fault:  ["body", "Invalid parameter"]                 # Fault 内容（业务错误）
```

**已知限制**（超出范围就在 `teardown_hooks`（**复数**，见 README「取值与多步链路」）里写 Python）

- 选择器是**简化子集**：只支持 `name` / `.//name` / `{URI}name` / `[@属性='值']`；
  不支持轴（`ancestor::`）、`position()`、`text()`、多级路径（`a/b`）、`*` 通配；
- 属性选择器只认**单引号**包裹的值；
- 不做 XSD 校验（标准库没有 XSD 验证器，需要 `lxml` → 那是评估文档里的 A2 方案）。
"""

import re
import xml.etree.ElementTree as ET

# 选择器：`name`，可带一个属性谓词 `[@attr='value']`（命名空间由调用方先剥掉）
_SELECTOR_RE = re.compile(
    r"^(?P<name>[^\[\]]+?)" r"(?:\[@(?P<attr>[\w:.\-]+)\s*=\s*'(?P<value>[^']*)'\])?$"
)

_UNSUPPORTED = (
    "选择器写法不支持：{selector}"
    "（只支持 name / .//name / {{URI}}name / [@attr='value']；"
    "复杂 XPath 请在 teardown_hook 里写 Python）"
)


def _as_text(check_value):
    """XML 响应体是 bytes（非 JSON 响应走 `resp.content`），这里统一成文本。

    NOTICE: **不能**把函数返回值写成「解码后的字符串」再去当 `check` 用——框架对
    `${func()}` 的返回值里 `Text` 会继续当作取值表达式（见 `response.py` 的 validate），
    所以断言必须由算子自己解析 `body`。
    """
    if isinstance(check_value, (bytes, bytearray)):
        return check_value.decode("utf-8", errors="replace")
    if isinstance(check_value, str):
        return check_value
    raise AssertionError(
        f"XML 算子只能作用在报文文本上，实际收到 {type(check_value).__name__}"
        "（check 写 `body`；JSON 响应请用 jsonschema_match）"
    )


def _root(check_value):
    text = _as_text(check_value)
    try:
        return ET.fromstring(text)
    except ET.ParseError as ex:
        raise AssertionError(f"响应体不是合法 XML：{ex}\n原文前 200 字：{text[:200]!r}") from ex


def _local_name(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else tag


def _namespace_of(tag):
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else None


def _parse_selector(selector):
    """解析简化选择器 → ``(命名空间URI or None, 节点名, 属性名 or None, 属性值 or None)``。"""
    sel = str(selector).strip()
    if sel.startswith(".//"):
        sel = sel[3:]
    elif sel in (".", ".."):
        raise AssertionError(_UNSUPPORTED.format(selector=selector))

    # 命名空间只能写在最前面：`{URI}name`
    want_ns = None
    if sel.startswith("{"):
        if "}" not in sel:
            raise AssertionError(f"命名空间写法不完整（应为 {{URI}}name）：{selector}")
        want_ns, sel = sel[1:].split("}", 1)

    if sel.startswith("/") or "/" in sel or "{" in sel or "}" in sel:
        raise AssertionError(_UNSUPPORTED.format(selector=selector))

    matched = _SELECTOR_RE.match(sel)
    if not matched:
        raise AssertionError(_UNSUPPORTED.format(selector=selector))

    name, attr, value = matched.group("name").strip(), matched.group("attr"), matched.group("value")
    if not name:
        raise AssertionError(f"选择器缺少节点名：{selector}")
    if "*" in name or "(" in name or ")" in name:
        # XPath 函数（text()/local-name()…）与通配都不支持：**明确报错**，不要让它静默「选不中」
        raise AssertionError(_UNSUPPORTED.format(selector=selector))
    return want_ns, name, attr, value


def _attr_matches(element, attr, value):
    """属性匹配：优先按原名，退化为**按 local-name**（属性前缀同样会漂移）。"""
    if attr in element.attrib:
        return element.attrib[attr] == value
    for key, actual in element.attrib.items():
        if _local_name(key) == attr and actual == value:
            return True
    return False


def _select(root, selector):
    """按简化写法选节点（**前缀无关**；配对规则见模块 docstring）。

    NOTICE（批次 C / L13，与内核 `xpath_match` 同步的两处修复）：
      ① **丢掉前缀**：`svc:code` 修复前会被整串当成节点名 → 与真实的 `local-name()`
         （`code`）**永远不相等** → 恒不命中（假失败）。这既违背「前缀漂移也能命中」的
         本意，也与内核简化写法的行为不一致（内核那边已同步修好）。
      ② **包含上下文节点自身**：修复前 `for element in root.iter(): if element is root ... continue`
         把根元素排除在外，于是「响应根元素就是业务元素」时简化写法**永远选不中根元素**
         —— 而 `xml_schema_match` 推荐的 `{"xpath": "QueryResponse"}` 写法恰好踩这个坑。
         现在根元素也参与匹配（对既有 SOAP 报文——根是 `Envelope`——行为不变）。
    """
    want_ns, name, attr, value = _parse_selector(selector)
    if ":" in name:
        name = name.rsplit(":", 1)[1].strip()
        if not name:
            raise AssertionError(f"选择器缺少节点名：{selector}")

    found = []
    for element in root.iter():
        if _local_name(element.tag) != name:
            continue
        if want_ns is not None and _namespace_of(element.tag) != want_ns:
            continue
        if attr is not None and not _attr_matches(element, attr, value):
            continue
        found.append(element)
    return found


def _spec(expect_value):
    """`"code"` → 只断言存在；`["code", "0000"]` → 再断言文本。"""
    if isinstance(expect_value, (list, tuple)):
        if not expect_value:
            raise AssertionError("expect_value 不能是空列表")
        path = expect_value[0]
        expect_text = expect_value[1] if len(expect_value) > 1 else None
    else:
        path, expect_text = expect_value, None
    return str(path), (None if expect_text is None else str(expect_text))


def xpath_match(check_value, expect_value, message=""):
    """断言选择器选中了节点（可选：断言其文本）。**命名空间前缀无关**。"""
    path, expect_text = _spec(expect_value)
    nodes = _select(_root(check_value), path)
    if not nodes:
        raise AssertionError(f"未选中任何节点：{path}" + (f"\n{message}" if message else ""))
    if expect_text is not None:
        actuals = [(node.text if node.text is not None else "") for node in nodes]
        if expect_text not in actuals:
            raise AssertionError(
                f"{path} 选中 {len(nodes)} 个节点，但文本都不是 {expect_text!r}；实际={actuals}"
                + (f"\n{message}" if message else "")
            )
    return True


def xpath_count(check_value, expect_value, message=""):
    """断言选中节点**个数**（明细条数这类场景），expect_value 为 `[选择器, 个数]`。"""
    if not isinstance(expect_value, (list, tuple)) or len(expect_value) != 2:
        raise AssertionError("xpath_count 的 expect_value 必须是 [选择器, 个数]")
    path = str(expect_value[0])
    try:
        expect_count = int(expect_value[1])
    except (TypeError, ValueError):
        raise AssertionError(f"xpath_count 期望个数必须是整数，实际 {expect_value[1]!r}") from None
    actual = len(_select(_root(check_value), path))
    if actual != expect_count:
        raise AssertionError(
            f"{path} 选中 {actual} 个节点，期望 {expect_count} 个" + (f"\n{message}" if message else "")
        )
    return True


def xpath_text(check_value, expect_value, message=""):
    """非断言用法：取节点文本（给 hook / 日志用）。未选中返回 None。"""
    path, _ = _spec(expect_value)
    nodes = _select(_root(check_value), path)
    return nodes[0].text if nodes else None


def soap_fault(check_value, expect_value, message=""):
    """SOAP 专用：断言这是 `soap:Fault` 且 `faultstring` 含期望子串。

    存在的理由：SOAP 的**业务错误也是 HTTP 500**，照 REST 模板写
    `equal: ["status_code", 200]` 会把「参数校验失败」误报成「接口挂了」。
    """
    expect = str(expect_value)
    if not _select(_root(check_value), "Fault"):
        raise AssertionError("响应体里没有 soap:Fault 节点（不是 SOAP 错误报文）" + (f"\n{message}" if message else ""))
    faultstring = xpath_text(check_value, "faultstring") or ""
    if expect not in faultstring:
        raise AssertionError(
            f"faultstring={faultstring!r} 不含 {expect!r}" + (f"\n{message}" if message else "")
        )
    return True


def soap_base_url():
    """示例工程用：conftest 起了本地 mock 后把地址写进环境变量（autouse 夹具不能给用例传值）。"""
    import os

    return os.environ.get("SOAP_BASE_URL") or f"http://127.0.0.1:{os.environ.get('SOAP_MOCK_PORT') or 8907}"


# --------------------------------------------------------------------------- 取值 / 多步链路
# 为什么需要这两个函数：`extract` 只认 jmespath 路径，而 XML 响应体的 `body` 是 bytes →
# `body.code` 会**静默得到 None**（实测）。在 `teardown_hooks` 里取值是目前的靠谱做法。
#
# 三条实测规则（都在 `examples/soap/chain.yml` 里有对应写法）：
#   1. YAML 键必须写**复数** `teardown_hooks:` / `setup_hooks:`（单数会被静默忽略、钩子不执行）；
#   2. `${...}` 里调函数时**参数不能带引号**（`parser.py` 的参数字符集不含引号）——
#      常量写裸标识符（`${xml_field($response, code)}`）或走变量（`${xml_field($response, $path)}`）；
#   3. hook 赋值（`- code: "..."`）出来的变量**只在本步骤可见**；跨步骤要写 `os.environ`，
#      下一步用 `${ENV(名字)}` 读。


def xml_field(response, path):
    """hook 用：取一个 XML 字段的文本。入参可以给 `$response`，也可以给报文本身。

    典型用法（本步骤内直接用该值）：

        teardown_hooks:
          - code: "${xml_field($response, code)}"
        validate:
          - contains: ["text", "<ns1:code>$code</ns1:code>"]
    """
    body = getattr(response, "body", None)
    if body is None:
        body = response
    return xpath_text(body, path)


def capture_xml(response, mapping=None):
    """hook 用：把若干 XML 字段写进 `os.environ`，供**后续步骤**用 `${ENV(名字)}` 读取。

    不传 mapping 时默认抓 `{"SOAP_CAPTURED_CODE": "code"}`（对应本地 mock 的报文）。
    想抓多个字段就在 `config.variables` 里定义映射再传变量进来：

        config:
          variables:
            soap_capture:
              SOAP_ORDER_ID: "orderId"
              SOAP_TOKEN: "token"
        # 步骤里：
        teardown_hooks:
          - ${capture_xml($response, $soap_capture)}
    """
    import os

    mapping = mapping or {"SOAP_CAPTURED_CODE": "code"}
    if not isinstance(mapping, dict):
        raise AssertionError(f"capture_xml 的 mapping 必须是 dict，实际是 {type(mapping).__name__}")
    for env_name, path in mapping.items():
        os.environ[str(env_name)] = xpath_text(response.body, path) or ""
    return True
