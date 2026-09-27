# SOAP/XML 接口怎么测（一页说明）

> 这一页只讲**怎么用**；为什么这么设计、以及实测证据在
> `docs/接口自动化框架升级优化评估.md` 第二章与 `docs/SOAP阶段开发记录.md`；
> 可运行的示例工程在 `examples/soap/`（A1 项目级算子）与 `examples/soap_xpath/`（A2-1/A2-2 内核算子）。
> **A2 两期均已交付**：XPath 断言（`xpath_match`/`xpath_count`）与 **XSD 契约校验**（`xml_schema_match`）。

## 一、现状一句话

**发没问题，断要靠算子**：SOAP 请求用 `data`（XML 字符串）+ `headers` 就能发；
XML 响应的 `body` 是 **bytes**，直接把内置字符串算子用在 `body` 上会报类型错
（`contains` 会抛 `TypeError`）。五条可用路径：

1. **内置 `xpath_match` / `xpath_count`**（A2-1，需 `pip install -e ".[xml]"`，**完整 XPath 1.0**）；
2. **内置 `xml_schema_match`**（A2-2，同样需 extra）：**拿 XSD 契约校验响应**（推荐先定位业务元素，
   例如 `{"xpath": "QueryResponse", "xsd": "schemas/query.xsd"}`）——「契约里写过的约束」不必在用例里重复；
3. **`xpath_match` / `xpath_count` / `soap_fault`**（`examples/soap/debugtalk.py` 的项目级算子，
   **零新增依赖**的退路；项目里有同名算子时会**遮蔽**上面那两份内核实现）；
4. **`text` 前缀**：`contains: ["text", "<ns1:code>0000</ns1:code>"]`（`text` 是 `requests.Response.text`，
   解好的字符串）——适合简单片段断言，但不解决前缀漂移；
5. **`teardown_hooks`（⚠️ 复数！）** 里写 Python（`xml.etree`/`lxml`）。
   写成单数 `teardown_hook: ` 会被**静默忽略**、钩子根本不执行（生成代码里的方法名才是单数
   `.teardown_hook()`）——详见「XML 取值与多步链路」一节。

## 二、请求模板（SOAP 1.1 与 1.2 的差别只在头与 Content-Type）

```yaml
config:
  name: "SOAP 查询"
  base_url: https://host:port
  variables:
    envelope: |
      <?xml version="1.0" encoding="utf-8"?>
      <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
        <soap:Body>
          <ns1:QueryRequest xmlns:ns1="http://your.ns/svc">
            <ns1:userId>U1001</ns1:userId>
          </ns1:QueryRequest>
        </soap:Body>
      </soap:Envelope>
teststeps:
-
  name: SOAP 1.1
  request:
    method: POST
    url: /svc/query
    headers:
      Content-Type: "text/xml; charset=utf-8"   # 1.1
      SOAPAction: "urn:your#Query"              # 1.1 的动作头
    data: "$envelope"
  validate:
    - equal: ["status_code", 200]
    - xpath_match: ["body", ["code", "0000"]]
-
  name: SOAP 1.2
  request:
    method: POST
    url: /svc/query
    headers:
      # 1.2 不用 SOAPAction 头，动作写在 Content-Type 的 action 参数里
      Content-Type: "application/soap+xml; charset=utf-8; action=\"urn:your#Query\""
    data: "$envelope"
  validate:
    - equal: ["status_code", 200]
    - xpath_match: ["body", ["code", "0000"]]
```

其他请求侧细节都是现成能力，不需要框架改动：

| 场景 | 怎么写 |
| --- | --- |
| 需要 `X-Path` 头 | `request.x_path`（框架原生字段） |
| MTOM/附件（`multipart/related`） | `upload` + `requests-toolbelt` extra |
| WS-Security/加签 | 在 `debugtalk.py` 里生成签名头 → `headers`/`data` 注入 |
| 变量化 | XML 里直接写 `${var}`（模板串会被解析） |

## 三、最大的坑：**业务错误也是 HTTP 500**

SOAP 用 `Fault` 表达业务错误，而 HTTP 状态码同样是 500。照 REST 模板写
`equal: ["status_code", 200]` 的后果是：「参数校验失败」看起来像「服务挂了」，
测试人员要去翻日志才知道是业务错误。

```yaml
  validate:
    - equal: ["status_code", 500]                 # ① 先按 SOAP 语义断
    - soap_fault: ["body", "Invalid parameter"]   # ② 再断 Fault 内容
    - xpath_match: ["body", ["faultcode", "soap:Client"]]
```

> NOTICE：这类用例跑起来会打一条 `ERROR | 500 Server Error ...`（框架对 `status_code >= 400`
> 的响应统一记 error 级别，`client.py` 的 `raise_for_status` 分支），**用例是通过的**。
> 如果日志告警按 ERROR 抓，需要排除这类预期 500 的用例。

## 四、断言方案与边界

| 方案 | 做法 | 适用 | 限制 |
| --- | --- | --- | --- |
| **内核算子（A2-1，推荐）** | 直接用内置 `xpath_match` / `xpath_count`（需 `pip install -e ".[xml]"`） | **完整 XPath 1.0**：`text()` / `count()` / `string()` / 位置谓词 / 轴 / 多级路径 | 需 lxml（二进制依赖）；项目 `debugtalk.py` 里有同名算子时**会被遮蔽**（项目优先） |
| **XSD 契约校验（A2-2，推荐）** | 内置 `xml_schema_match`：`["body", {"xpath": "QueryResponse", "xsd": "schemas/query.xsd"}]` | 「契约即用例」：`maxOccurs`、必填属性、取值 pattern、元素顺序**一次写进 XSD** | 必须先定位子节点（客户 XSD 只描述 Body 里那层）；只收文件路径；`xpath` 要唯一命中；XSD 与报文命名空间必须严格一致 |
| **A1 算子**（零依赖退路） | 复制 `examples/soap/debugtalk.py` 的四段算子到项目 `debugtalk.py` | 装不了二进制 wheel 的现场；只需「前缀漂移也能断住」 | 选择器是简化子集（无轴/`text()`/多级路径）；每项目一份；**无 XSD** |
| **hook** | `teardown_hooks`（**复数**）里用 `xml.etree`/`lxml` 写任意 Python | 极特殊校验（自定义聚合、跨报文比对） | 用例里混 Python，非技术同事写不了；**写单数键会被静默忽略** |
| WSDL 生成用例 | —— | —— | **不做**（属独立项目量级，SOAP 客户量不足） |

> 内核算子与 A1 的算子**YAML 写法一致**（都是 `xpath_match` / `xpath_count`）：先按 A1 起步，等能装 lxml 时把项目
> `debugtalk.py` 里的同名算子删掉/改名，就自动升级到内核实现（既有断言语义不变）。
> **原始 XPath 的前缀是字面量**（`//ns1:code` 在报文声明为 `abc:` 时会报未声明前缀），
> 要「不管前缀」就用简化写法或 `//*[local-name()='code']`；**默认命名空间**只能用 `local-name()`。
> **XSD 校验则相反**：它按命名空间 **URI** 匹配，所以前缀漂移不影响校验，但 XSD 必须声明对应的 `targetNamespace`。

## 五、XML 取值与多步链路（照抄即可）

SOAP 链路通常是「第一步拿 token/流水号 → 后面步骤带着它发请求」。但 **XML 没有 extract 通道**：

| 写法 | 结果 |
| --- | --- |
| `extract: {code: body.code}` | ❌ **静默得到 `None`**（`body` 是 bytes，jmespath 对 bytes 做子字段访问） |
| `extract: {code: "${func()}"}` | ❌ 报 `invalid check/extract expression: 0000`（`${func()}` 的**字符串**返回值会被当取值表达式再解析） |
| `teardown_hooks` 里取（本节方案） | ✅ 可用 |

**四条实测规则**（每条都踩过；示例见 `examples/soap/chain.yml`）：

1. **YAML 键必须写复数** `teardown_hooks:` / `setup_hooks:`。写成单数 `teardown_hook:` 会被**静默忽略**、
   钩子根本不执行，用例会因为「什么都没做」而通过（生成代码里的方法名才是单数 `.teardown_hook()`）；
2. **`${...}` 里调函数时参数不能带引号**：常量写裸标识符（`${xml_field($response, code)}`），
   或用变量（`${xml_field($response, $code_path)}`）。带引号（`${f('abc')}`）的调用**不会被识别为函数**，
   只会原样留下字符串——框架现在会告警（0917-1 起）；
3. **hook 赋值的变量只在本步骤可见**：`- code: "${xml_field($response, code)}"` 之后，
   同一 `validate` 里可以用 `$code`，但**下一个步骤会 `VariableNotFound`**；
   跨步骤要用 `capture_xml($response)` 写 `os.environ`，下一步 `${ENV(SOAP_CAPTURED_CODE)}` 读；
4. **变量只放在「值」位置，不要放在 check 位置**：`- equal: ["$code", "0000"]` 里那个 `$code`
   会被 `parse_string_value` 强制转型（`"0000"` → `int 0`），失败信息变成看不懂的 `assert 0 equal 0000`。
   写成 `- contains: ["text", "<ns1:code>$code</ns1:code>"]` 或 `- xpath_match: ["body", ["code", "$code"]]`。

```yaml
config:
  variables:
    soap_capture: {SOAP_CAPTURED_CODE: code}     # 要抓多个字段时在这里加
teststeps:
-
  name: 步骤1：抓字段
  request: {method: POST, url: /svc/query, headers: {...}, data: "$envelope"}
  teardown_hooks:                                # ← 复数
    - code: "${xml_field($response, code)}"      # ← 裸标识符当字符串常量；本步骤内可用 $code
    - ${capture_xml($response, $soap_capture)}   # ← 写 os.environ，供后续步骤用
  validate:
    - xpath_match: ["body", ["code", "0000"]]
    - contains: ["text", "<ns1:code>$code</ns1:code>"]
-
  name: 步骤2：带着上一步的值发请求
  request:
    method: POST
    url: /svc/query?code=${ENV(SOAP_CAPTURED_CODE)}
    headers:
      # NOTICE: HTTP 头值只能是 latin-1 → 中文值（如 message）**不能**放 header，
      # 否则抛 UnicodeEncodeError；中文放 url/query/data 或断言里。
      X-From-Env: "${ENV(SOAP_CAPTURED_CODE)}"
    data: "$envelope"
  validate:
    - xpath_match: ["body", ["code", "${ENV(SOAP_CAPTURED_CODE)}"]]
```

> 另一个坑（与本主题相关）：`regex_match` 用的是 `re.match`——**从头匹配**，且 `.` 不跨行。
> 报文带换行/缩进时写 `".*<ns1:code>…"` 也会失败，建议改用 `contains` 或 `xpath_match`。

## 六、存量资产迁移（SoapUI / Postman / HAR）

- **HAR 能录 SOAP 请求**：`hconvert --from har --in xxx.har --out out/` 会把 XML 请求体放 `data`、
  把 `Content-Type`/`SOAPAction` 原样保留（实测）。**但**响应是 XML 时，导入器只会生成
  `eq: [status_code, 200]` 这一条断言（形状断言只对 JSON 响应生成）——
  **迁移了请求、没迁移校验**，需要人工用算子补断言；
- **Postman 集合**：SOAP 请求通常是 `raw` + `text/xml`，导入行为同上；
- **SoapUI 工程**：没有直接导入器（`.xml` 工程格式不在四个源里），只能录 HAR 或手工搬。

## 七、去哪看更细

| 想看什么 | 去哪 |
| --- | --- |
| 内核算子（A2-1/A2-2）用法、XSD 六条规则、两种写法对照、可运行示例 | `examples/soap_xpath/README.md`、`docs/A2-1开发记录.md`、`docs/A2-2开发记录.md` |
| A1 项目级算子用法、四个离线演示 | `examples/soap/README.md` |
| A1/A2 的取舍、A2 分期与实测证据 | `docs/接口自动化框架升级优化评估.md` 第二章 |
| 框架支持哪些算子与请求字段 | `docs/能力清单.md` |
| 把这套搬到自己项目、离线安装 | `deploy.md` |
