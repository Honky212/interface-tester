# 示例：内核 XML/XPath 断言与 XSD 契约校验（A2-1 + A2-2，需要 `xml` extra）

本示例演示**框架内置**的三个 XML 算子：

- `xpath_match` / `xpath_count`（A2-1）：**完整 XPath 1.0**（`text()` / `count()` / `string()` / 位置谓词 / 轴 /
  `local-name()`），既能「前缀漂移照样断住」，也能写精确路径与列表细节断言；
- `xml_schema_match`（A2-2）：**拿 XSD 契约校验响应**（可先定位子节点），把「契约里写过的约束」交给契约，
  用例里不必逐条重复。

```bash
# 0) 装可选依赖（lxml 是二进制依赖，所以放在 extra 里，不进主依赖）
pip install -e ".[xml]"

# 1) 跑本示例（自带本地 mock，不依赖外网）
python -m interfacetester run examples/soap_xpath

# 2) 看两个对照用例的真实后果（对照用例默认 skip）
#    Windows PowerShell:
$env:SOAP_RUN_CONTRAST=1
python -m interfacetester run examples/soap_xpath/contrast_prefix_literal.yml   # 原始 XPath 的前缀是字面量
python -m interfacetester run examples/soap_xpath/contrast_xsd_violation.yml    # 业务码违反 XSD 约束
#    Linux / macOS 同理：SOAP_RUN_CONTRAST=1 python -m interfacetester run <yml>
```

预期结果：`2 passed, 2 skipped`（skip 的是两个 `contrast_*` 对照用例）。

| 文件 | 演示什么 |
| --- | --- |
| `xpath.yml` | 两种选择器写法混用、前缀漂移、`count()`、位置谓词、多级路径、属性取值、轴 |
| `xsd.yml` | XSD 契约校验：整根校验 vs **先定位再校验**、前缀漂移不影响校验、`xs:include` 多文件、单独校验一条明细 |
| `contrast_prefix_literal.yml` | 对照（**故意失败**）：原始 XPath 的前缀是字面量 |
| `contrast_xsd_violation.yml` | 对照（**故意失败**）：业务码违反 XSD 的 `pattern` → 失败信息指到具体节点 |
| `schemas/` | `common.xsd`（被 include 的公共类型）+ `query.xsd` + `items.xsd` |

## 一、与 `examples/soap/`（A1 标准库方案）的分工

| | `examples/soap/`（A1） | `examples/soap_xpath/`（A2-1 + A2-2，本目录） |
| --- | --- | --- |
| 算子来自 | 项目 `debugtalk.py`（**标准库**实现，可整段复制走） | **框架内置**（`interfacetester/builtin/comparators.py`） |
| 依赖 | **零新增依赖** | 需要 `pip install -e ".[xml]"`（lxml） |
| 选择器 | 简化写法：`code` / `.//code` / `{URI}code` / `item[@id='1']` | 简化写法 **+ 完整 XPath 1.0** |
| 轴 / `count()` / `text()` / 多级路径 | ❌ 明确报「不支持」 | ✅ 直接写 |
| **XSD 契约校验** | ❌ | ✅ `xml_schema_match`（含 `xs:include` 多文件、出错节点路径） |
| 适用 | 现场装不了二进制 wheel、只想把「前缀漂移」断住 | 需要精确路径、列表细节、**契约即用例** |

> **为什么必须分成两个目录**：算子查找顺序是「项目 `debugtalk.py` → 框架内置」。
> `examples/soap/debugtalk.py` 里定义了同名算子，**会遮蔽内核算子**，所以「用内核完整 XPath / XSD」
> 只能在一个**不定义同名函数**的项目里演示。
> 真实项目里的选择：装了 `xml` extra 就把自己 `debugtalk.py` 里的同名算子删掉（或改名），
> 让它落到内核；装不了就保留 A1 那份，行为与现在完全一致。

## 二、两种选择器写法（同一个算子，按写法自动分流）

| 写法 | 例子 | 编译 / 求值方式 | 什么时候用 |
| --- | --- | --- | --- |
| **简化写法**（推荐） | `code`、`.//code`、`ns1:code`、`{http://…/svc}code`、`item[@id='1']` | 编译成 `local-name()`（`{URI}` 用 `namespace-uri()`），**前缀无关** | 默认选择：网元换前缀不会假失败 |
| **原始 XPath 1.0** | `//ns1:code/text()`、`count(//ns1:item)`、`(//ns1:item)[2]/ns1:name`、`ancestor::soap:Fault` | 原样交给 lxml，**前缀按报文声明映射** | 需要精确路径 / 函数 / 轴 / 位置谓词 |

> **批次 C / L13 的两处修正**（简化写法的命中集合因此变大，属**修 bug**不是放宽）：
> ① **带前缀的名字**（`ns1:code`、`.//ns1:code`）以前会被整串当成节点名编译成
> `local-name()='ns1:code'` —— 而真实的 `local-name()` 是 `code`，**永远不命中**
> （明明节点在，却报「未选中任何节点」）。现在**丢掉前缀**按 local-name 比对，
> 才符合「前缀漂移也能命中」这个写法的本意。
> ② 编译结果从 `.//*[...]` 改成 `descendant-or-self::*[...]` —— 前者是**后代**搜索、
> **不含上下文节点本身**，于是「**响应根元素就是业务元素**」时简化写法永远选不中根元素
> （而下面推荐的 `{"xpath": "QueryResponse"}` 恰好踩这个坑）。现在根元素也参与匹配。
> 项目级 A1 算子（`examples/soap/debugtalk.py`）**同步修了这两处**，两边命中集合继续一致
> （护栏：`tests/xml_comparators_test.py::TestParityWithA1StdlibOperators`）。

- 文本断言两种形态：`xpath_match: ["body", ["code", "0000"]]` 断文本；`xpath_match: ["body", "code"]` 只断**存在**。
  对**节点集合**的语义是「**任一**节点的文本等于期望值」；对**标量**（`string(...)` / `count(...)`）是直接比较。
- `xpath_count: ["body", ["item", 3]]`；选择器也可以直接写 `count(...)`（结果就是数字）。
- `xml_schema_match` 里的 `xpath` 用的就是**同一套选择器语法**（所以前缀漂移同样不影响定位）。

## 三、XSD 契约校验（A2-2）

```yaml
validate:
  # 推荐：先定位业务元素，再按 XSD 校验（客户 XSD 通常只描述 Body 里那层元素）
  - xml_schema_match: ["body", {"xpath": "QueryResponse", "xsd": "schemas/query.xsd"}]
  # 少数情况：整个响应体就是那个元素
  - xml_schema_match: ["body", "schemas/query.xsd"]
  # 只要某一条明细：把选择器收窄到唯一节点（多节点会被明确拒绝，不静默挑第一个）
  - xml_schema_match: ["body", {"xpath": "(//ns1:item)[2]", "xsd": "schemas/items.xsd"}]
```

六条要记住的规则：

1. **必须先定位子节点**（这是 A2 原计划漏掉的必需项）：直接校验整个 Envelope 会报
   `No matching global declaration available for the validation root` —— 因为客户 XSD 只描述 Body 里的业务元素；
2. **XSD 只能是文件路径**：`xs:include` / `xs:import` 的相对路径按 **XSD 文件所在目录**解析，
   内联字符串没有「所在目录」→ 内联 XSD 在 `hmake` 阶段就被拦住（给出正确写法）；
3. **路径相对项目根目录**（含 `debugtalk.py` 的那层）解析，也可以写 `${func()}` 返回路径
   （这种写法会**跳过** `hmake` 阶段的存在性检查，因为只有运行期才知道路径）；
4. **`hmake` 阶段会检查 XSD 文件存在**（写错路径不必等到跑用例才发现）；**不编译** XSD（生成阶段不依赖 lxml）；
5. **前缀漂移不影响校验**：XSD 按**命名空间 URI** 匹配，`ns1:` 换成 `abc:` 照样通过；
   但 XSD **必须声明对应的 `targetNamespace`**，否则报 `No matching global declaration`；
6. **多节点必须收窄**：`xpath` 命中多个节点时报错（不静默挑第一个），要用位置谓词或按节点分别断言。

**XSD 相对 XPath 断言多在哪**：把「条数上限、属性必填、子元素顺序、取值 pattern」这类规则写在契约里一次，
用例里不必重复（见 `schemas/items.xsd` 的 `maxOccurs="3"` 与 `use="required"`）；
失败信息还会给出**出错节点的路径**（用报文自己的前缀）与自然语言原因。

## 四、命名空间的三条规则（现场最容易踩）

1. **前缀按报文声明映射**，且是**全树收集**的 —— `ns1:` 声明在内层元素上也能用（`root.nsmap` 只有根上的前缀，不够用）；
2. **原始 XPath 里的前缀是字面量**：同一份契约换个前缀，`//ns1:code` 就会报
   `XPathEvalError: Undefined namespace prefix`（本目录的 `contrast_prefix_literal.yml` 演示这件事）；
   要「不管前缀」就写 `//*[local-name()='code']`，或直接用简化写法；
3. **默认命名空间没法用前缀**（XPath 1.0 的限制）：`<code xmlns="urn:x">` 既不能写 `//code` 也不能编前缀，
   只能用 `//*[local-name()='code']` 或简化写法 `code`。

## 五、报错怎么读（失败 vs 错误）

| 现象 | 类型 | 该找谁 |
| --- | --- | --- |
| 响应体不是合法 XML / 没选中节点 / 文本、个数不符 / **不符合 XSD 契约** | `AssertionError`（**用例失败**） | 找开发（接口行为变了） |
| XPath 语法错、前缀未声明、`check` 类型不对、没装 lxml、**XSD 文件不存在 / 自身不合法 / 内联 XSD** | `RuntimeError`（**用例错误**） | 改用例 / 装依赖 / 修 XSD |

NOTICE: 在 pytest 的 JUnit 报告里两者都落在 `<failure>`（pytest 对 call 阶段的异常一律记 failure），
区分靠**异常类型与文案**，不要靠报告计数。

## 六、已知边界

- **XSD 必须与报文命名空间严格一致**（这正是契约的意义）：`targetNamespace` 对不上就报
  `No matching global declaration`；报文里的元素**不能多也不能少**（`xs:any` 要自己写进契约）；
- **单个元素要能单独校验，XSD 里得把它声明成全局元素**（`<xs:element name="item" type="tns:ItemType"/>`），
  本地内联声明（`<xs:element name="item">` 嵌在 `xs:sequence` 里）无法作为校验根 —— `items.xsd` 演示了正确写法；
- `extract` 仍然是 jmespath，**不支持 XML**：XML 取值请用 `teardown_hooks`（见 `examples/soap/chain.yml` 的
  `xml_field` / `capture_xml` 写法）；
- **不做 WSDL 驱动生成用例**（属独立项目量级）；
- 本目录的 mock **复用** `../soap/mock_soap.py`（同一个被测服务只维护一份报文契约）；
  照抄到自己项目时把那份 mock 一起拿走，或把 `${soap_base_url()}` 换成真实地址。

更多阅读：`docs/soap/README.md`（SOAP/XML 全量说明）、`docs/能力清单.md`（算子表）、
`使用教程.md` 第 6.4 节、`docs/A2-1开发记录.md` 与 `docs/A2-2开发记录.md`（两批次的实现与验证记录）。
