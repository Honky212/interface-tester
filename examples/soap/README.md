# SOAP/XML 接口示例（A1：`debugtalk.py` 算子，零新依赖、零内核改动）

给「存量 SOAP 接口」用的最小可用方案：**请求照发，断言用结构化算子**。
算子本体就在同目录的 `debugtalk.py` 里，整段复制到自己项目即可。

```powershell
# 一分钟跑通（在仓库根执行；mock 由本目录 conftest.py 自动拉起，离线）
python -m interfacetester run examples/soap
# 期望：3 passed, 1 skipped
#   query.yml → 正常响应 / 前缀漂移 / 明细条数 共 3 个步骤
#   fault.yml → soap:Fault（HTTP 500）共 1 个步骤
#   contrast_string_assert.yml → skipped（**故意**演示字符串断言的假通过，见下）
```

## 一、为什么不能只用字符串断言（实测，4.3.5）

XML 响应的 `body` 不是 JSON，框架走的是 `resp.content` → 是 **bytes**。于是内置字符串算子在它上面：

| 写法 | 实测结果 |
| --- | --- |
| `contains: ["body", "0000"]` | ❌ `TypeError: a bytes-like object is required, not 'str'`（**原始异常**，不是可读的断言失败） |
| `regex_match: ["body", "code"]` | ❌ 断言失败：`check_value should be Text type` |
| `startswith: ["body", "<"]` | ❌ 失败——它内部写的是 `str(check_value)`，比的是 `"b'<?xml…"` |
| `startswith: ["body", "b'"]` | ✅ **通过**（← 就是这么荒谬：一个毫无意义的前缀反而过了） |
| `length_equal` | ⚠️ 能跑，但语义是**字节数** |

结论：**直接把字符串断言用在 `body` 上不行**——但**换成 `text` 前缀就行**：

```yaml
validate:
  - contains: ["text", "<ns1:code>0000</ns1:code>"]   # text = requests.Response.text（已解码字符串）
  - type_match: ["body", bytes]                       # body 才是原始 bytes
```

`text` 适合"断简单片段"，但**不解决前缀漂移**（`ns1:` 换成 `abc:` 它照样挂）——
要按结构断就用本目录的算子，或在 `teardown_hooks`（**复数**）里写 Python。

## 二、算子用法（`check` 统一写 `body`）

```yaml
validate:
  - xpath_match: ["body", "code"]                                        # 选中节点即可
  - xpath_match: ["body", ["code", "0000"]]                              # 再断文本
  - xpath_match: ["body", ["{http://demo.example.com/svc}code", "0000"]] # 按命名空间 URI 精确限定
  - xpath_match: ["body", "item[@id='2']"]                               # 属性选择器
  - xpath_count: ["body", ["item", 3]]                                   # 个数断言
  - soap_fault:  ["body", "Invalid parameter"]                           # Fault 内容（业务错误）
```

三条设计要点，正好对上 SOAP 现场最常见的三个坑：

1. **命名空间前缀漂移**（`ns1:` / `abc:` / `soap:`）——按 local-name 选节点，前缀变了也能断；
2. **报文不合法要立刻报错**——非法 XML 会以「响应体不是合法 XML：unbound prefix」失败，而不是静默放过；
3. **业务错误也是 HTTP 500**——`soap_fault` 让「参数校验失败」不会被误判成「服务挂了」。

> **批次 C / L13 的两处修正**（与内核算子同步，选择器写法本身不变）：
> ① **带前缀的选择器**（`ns1:code`）以前在 A1 里会被整串当成节点名比对
> （`local-name()` 是 `code`，永远不相等）→ 恒不命中；现在**丢掉前缀**按 local-name 比对。
> ② 遍历时**不再跳过根元素** —— 以前「响应根元素就是业务元素」时简化写法永远选不中它。
> 内核算子（`xpath_match` / `xpath_count` / `xml_schema_match`）同步修了同样的两处，
> 两边命中集合仍然**完全一致**（护栏：`tests/xml_comparators_test.py::TestParityWithA1StdlibOperators`）。

## 三、三个可离线复现的演示

```powershell
# 演示 1：前缀漂移。query.yml 的第 2 步打的是 /svc/query-drift（同一份契约、前缀 ns1→abc）
python -m interfacetester run examples/soap/query.yml

# 演示 2：Fault。fault.yml 断 status_code == 500 + faultstring（照 REST 模板写 200 就会失败）
python -m interfacetester run examples/soap/fault.yml

# 演示 3：对照（**故意**）。字符串断言在「不是合法 XML」的响应上照样通过 = 假通过
#   注意：这个文件默认会被 conftest 跳过，必须带环境变量才跑得起来
$env:SOAP_RUN_CONTRAST=1; python -m interfacetester run examples/soap/contrast_string_assert.yml
# 期望：1 passed —— 但那份报文根本不合法，这就是「假通过」

# 演示 4：把结构化断言指向同一份不合法报文（把 query.yml 的 url 临时改成 /svc/query-broken）
# 期望：失败，且信息是「响应体不是合法 XML：unbound prefix: line 4, column 4」
```

> NOTICE：Fault 用例（HTTP 500）跑起来会打一条 `ERROR | 500 Server Error ...` 日志——
> 这是框架对 `status_code >= 400` 的响应统一记 error 级别（`client.py` 的 `raise_for_status` 分支），
> **用例本身是通过的**。如果你的日志告警按 ERROR 级别抓，记得把这类用例排除。

## 四、搬到自己项目里

1. 把 `debugtalk.py` 里 `xpath_match` / `xpath_count` / `xpath_text` / `soap_fault` 四段（含 `_as_text`、
   `_root`、`_local_name`、`_namespace_of`、`_parse_selector`、`_attr_matches`、`_select`、`_spec`）
   复制到项目的 `debugtalk.py`；`soap_base_url()` 是示例工程专用的，不需要；
2. 用例里 `check` 写 `body`（不要写 `${...}` 去取报文——`${func()}` 的返回值若是字符串，框架会继续
   把它当取值表达式解析，见算子文件里的 NOTICE）；
3. 命名空间不确定就**别写** `{URI}`，直接写节点名（local-name 匹配）；
4. 请求侧的模板与 SOAP 1.1/1.2 差异见 `docs/soap/README.md`。

## 五、已知限制

- 选择器是**简化子集**：`name` / `.//name` / `{URI}name` / `[@attr='value']`；不支持轴、`position()`、
  `text()`、多级路径（`a/b`）、`*` 通配；属性值只认单引号。复杂校验请在 `teardown_hooks`（**复数**）里写 Python。
- **不做 XSD 校验**：标准库没有 XSD 验证器，需要 `lxml`（评估文档里的 A2 方案，未做）。
- 只断「选中集合里存在某个值」，不支持「第 2 个 `item` 的 `name` 必须是乙」这类路径级断言。
- 算子住在**项目**的 `debugtalk.py` 里（这正是 A1 的定位）：好处是零依赖零侵入，代价是每个项目一份。

更细的说明（SOAP 1.1/1.2 模板、Fault 模板、存量资产迁移、限制清单）见 `docs/soap/README.md`；
断言的取舍与实测证据见 `docs/接口自动化框架升级优化评估.md` 第二章与 `docs/SOAP阶段开发记录.md`。
