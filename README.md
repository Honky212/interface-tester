# InterfaceTester

> **一站式 HTTP(S) 接口测试工具**：写 YAML/JSON 用例 → 自动转成 pytest 用例 → 发真实请求 → 提取 / 断言 → 报告与日志。
> 当前版本 **5.0.1**，是 [HttpRunner](https://github.com/httprunner/httprunner) v4.3.5 的二次开发分支（见文末「许可与来源」）。

```bash
# 装依赖（Python >= 3.8，本仓库在 3.12 上验证）
python -m pip install -e .

# 跑一个自带示例：数据管理示例会自己起本地 mock，零外部依赖
hrun examples/data_management --html=reports/report.html --self-contained-html
```

> `hmake`/`hrun` 结束后会调 `black` 格式化生成的代码，**请先激活虚拟环境**，否则报 `missing dependency tool: black`。
> `examples/httpbin/**` 这类示例需要本机有 httpbin——仓库自带 mock：
> `python tests/mock_server.py`（常驻，默认 80/443，可用 `INTERFACETESTER_HTTP_BIN_PORT` 改）。

---

## 一、它能做什么

| 能力 | 说明 |
| --- | --- |
| **HTTP(S) 全方法** | GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS；`params`/`headers`/`cookies`/`json`/`data`/`upload`/`proxies`/`cert`/`stream` 等请求字段 |
| **变量与函数** | `$var`、`${func()}`、`${ENV(名)}`、`${parameterize(x.csv)}`；项目 `debugtalk.py` 里任意 Python 函数 |
| **提取与断言** | `extract` + jmespath；**22 个内置算子**（相等/数值/长度/包含/字符串形态/**结构契约 `jsonschema_match`**/**XML `xpath_match`、`xpath_count`、`xml_schema_match`**） |
| **SOAP/XML** | 请求照发（`data` 传 XML + `headers` 配 `Content-Type`/`SOAPAction`）；XML 响应体是 **bytes**（字符串算子不可用）→ 用**内置** `xpath_match`/`xpath_count`（完整 XPath 1.0）与 `xml_schema_match`（**XSD 契约校验**，均需 `xml` extra）或 [`examples/soap/`](examples/soap/README.md) 的项目级算子（零依赖退路），见 [`docs/soap/README.md`](docs/soap/README.md) |
| **数据驱动** | `config.parameters`（列表 / CSV / 函数），多组参数做笛卡尔积 |
| **用例复用** | `testcase: 路径` + `export`；步骤级 hooks；步骤级重试 |
| **认证** | OAuth2 Client Credentials 内置；Basic/签名/加密等在 `debugtalk.py` 里实现 |
| **SQL** | 内置 `RunSqlRequest`（仅 MySQL，需 `sql` extra） |
| **报告与日志** | `--html`（单文件可交互报告）、`--junitxml`（CI 聚合）、`logs/{case_id}.run.log` |
| **CI** | 现成模板：`.github/workflows/test.yml`、`.gitlab-ci.yml`（见 `docs/ci/README.md`） |

判断「某项能力到底行不行」请以 **[`docs/能力清单.md`](docs/能力清单.md)** 为准（逐项标注了依据与验证状态）。

---

## 二、本轮（P0~P3）新增了什么

落地了 4 个批次：

| 批次 | 内容 | 用户可见变化 |
| --- | --- | --- |
| **P0** | 修 `StepRequestValidation` 算子分发 + `hmake` 期算子名白名单 | **`debugtalk.py` 里自定义断言算子现在能在 YAML 里直接用**（修复前整文件收集失败）；算子名拼错在 `hmake` 阶段就报错退出 |
| **P1-a** | 响应元信息断言 + 可读报错 | 新增检查项 `elapsed_ms`/`elapsed_s`/`response_size`/`reason`；类型不匹配的断言从「error」变成**可读的 failed** |
| **P1-b** | 文档订正 9 处 | 「自定义算子不用改框架」「`elapsed` 可断言」等误导性结论已订正 |
| **P1-c** | CI 模板 + mock 端口可配置 | 两个平台的 CI 模板开箱可用；非 root 环境可用环境变量改 mock 端口 |
| **P2-a** | 测试数据管理约定（T1~T5）+ 可跑示例 | [`examples/data_management/`](examples/data_management/README.md)：造数/隔离/回收/环境矩阵，离线可跑 |
| **P2-b** | 第 19 个内置算子 `jsonschema_match` | **契约级断言**：`jsonschema` 进主依赖，schema 走 `${func()}` 或文件路径（内联**仅限不含 `$` 键**的极简 schema） |
| **P3** | 导入器 `hconvert`：**curl / HAR / Postman / OpenAPI** | 存量资产一键转成标准 YAML 用例（见下） |
| **A2-1** | **内置** XML/XPath 算子 `xpath_match` / `xpath_count`（可选 extra `xml`） | XML 断言从「简化选择器」升级到**完整 XPath 1.0**（`text()`/`count()`/`string()`/位置谓词/轴/多级路径）；A1 的项目级算子仍可作**零依赖退路** |
| **A2-2** | **内置** XSD 契约算子 `xml_schema_match` | 「**契约即用例**」：`maxOccurs`/必填属性/取值 pattern 一次写进 XSD，用例里不必重复；支持**先定位业务元素再校验**、`xs:include` 多文件、出错节点路径；`hmake` 阶段还检查 XSD 文件是否存在 |

### 导入器：把已有资产变成用例

```bash
hconvert --from curl    --in examples/data/curl/curl_examples.txt --out converted/
hconvert --from har     --in demo.har                            --out converted/
hconvert --from postman --in postman_collection.json             --out converted/
hconvert --from openapi --in petstore_demo.yaml                  --out converted/
```

- 生成的是**标准 YAML**（走现有 `hmake`，不改运行内核），并默认执行**导出即验证**（`load_testcase` + 渲染干跑 + 语法检查）；
- 敏感信息（Cookie / Authorization / token）**一律占位**成 `${...}`，明文绝不写进 YAML；
- 能自动生成断言：HAR/Postman 用**录制/示例响应**、OpenAPI 用**契约 schema** → 状态码 + JSON Schema（文件引用）；
- 迁移不了的写法（Postman 的 JS 脚本、外部 `$ref`、未知动态变量…）会**明确告警**，可用 `--report` 导出清单。

支持子集（**冻结**，逐条写明处理方式）见 **[`docs/convert/README.md`](docs/convert/README.md)**。

### `haify`（AI 协作层）：先装、再配

> **这个命令从哪来**：`haify` 在 **AI 包自己的** `pyproject.toml` 里声明，**不在**根 `pyproject.toml`
> （根上只有 `hrun` / `hmake` / `hconvert` / `interfacetester`）。所以 `pip install -e .`（只装内核）之后
> **`haify` 不存在** —— 要么装 AI 包，要么用等价入口：
>
> ```powershell
> python -m pip install -e interfacetester_ai --no-build-isolation   # 装 AI 包（零三方依赖）
> haify -V      # 两个版本都报（AI 层版本 + 内核版本）；与 hrun --version 同一习惯
> # 不装也能用（等价入口，同一份 cli.main）：python -m interfacetester_ai gen <文档.md>
> ```
>
> 装法/离线/回滚的完整口径见 `deploy.md` §3.4；逐步用法见 `docs/使用手册-AI协作层.md` §1。

`haify` 要调模型时必须**显式**配三样 —— 都是**环境变量**（**不是** `.env`）：

```powershell
$env:INTERFACETESTER_AI_BASE_URL = "http://127.0.0.1:11434/v1"   # OpenAI 兼容根地址
$env:INTERFACETESTER_AI_MODEL    = "qwen2.5:14b"
$env:INTERFACETESTER_AI_SANDBOX  = "on"                          # L3 闸门，默认关
```

- 云端再加 `INTERFACETESTER_AI_API_KEY`（**回环地址可留空**——Ollama 类本来就没有 key）；
- **不读项目 `.env`** —— 那份 `.env` 是**用例**里 `${ENV(...)}` 的来源，属于**被测项目**；AI 配置是**本工具的运行期配置**，只从环境变量取（凭据不落文件、不被用例读到）；
- 非回环主机还要在 `INTERFACETESTER_AI_ALLOW_HOST` 里**显式列名**（L3 只放行回环 + 白名单）；
- **没配就一定不会出网**：退出码 **2** 并打印配置指引。

完整变量表、验证命令与三种典型用法见 **`deploy.md` §5.1**。
想先不调模型试跑一遍：`haify gen <doc.md> --assertions-only`。
★它**按接口段逐片**：一份文档里 N 个接口 → **N 条草稿**（每条只带**自己那一段**的字段断言；
段外断言丢弃并写进该条的 `.ai/draft/<用例>.unknowns.json`——"静默错位"变成"可见"）。
★**响应数据表**（段内、**没有「必填」列**的字段表）会变成一条 `jsonschema_match`：
**字段缺失不算失败、存在但类型错才算**（这是"响应字段"的本义，也避免误报）。

★**产物默认落在 `.ai/draft/`**（不是 `cases/`）：退出码 `0` 只表示"**草稿产出完成**"。
要进 `cases/`（CI 会跑的地方）必须**人工确认转正**：

```powershell
haify review --list                                   # 看有哪些草稿 + 质量状态 + blocker
haify review --approve <草稿名> --approver <姓名>      # 确认后转正 + 留痕（.ai/reviews/）
# 若该草稿有 blocker（仅协议断言 / 引用对不上 / TODO 没填），要逐条点名处置：
haify review --approve <草稿名> --approver <姓名> --resolve-blocker protocol-only
```

每条结果都会打印**质量状态**（`static_valid` / `needs_review` / …）：只有 `auto_promotable`
才算"无需审核可直接用"，而它**在没有隔离执行与变异证据之前不会出现**（当前一律不出现）。

### 补充：SOAP/XML 接口的结构化断言

存量系统里仍有大量 SOAP/XML 接口。请求侧框架本来就能发；缺的是断言——XML 响应的 `body` 是 **bytes**，
`contains`/`regex_match`/`startswith` 在它上面**用不了**（`contains` 会直接抛 `TypeError`）。
补齐分三步走，两种形态都在仓库里可跑：

- **A1（项目级算子，零新增依赖）**：一份可复制的 `debugtalk.py` + 示例工程。装不了二进制 wheel 的现场用这个；
- **A2-1（内核算子）**：`xpath_match` / `xpath_count` 做成内置，支持**完整 XPath 1.0**；
- **A2-2（内核算子）**：`xml_schema_match` 做成内置，支持 **XSD 契约校验**（**先定位业务元素再校验**、
  `xs:include` 多文件、出错节点路径；`hmake` 阶段还会检查 XSD 文件是否存在）。
  lxml 是二进制依赖，所以两个内核批次都放在可选 extra `xml` 里（没装时给可操作的安装提示，并指出 A1 退路）。

```bash
python -m interfacetester run examples/soap         # A1：项目级算子（零依赖），期望 3 passed, 1 skipped
pip install -e ".[xml]"                             # A2-1/A2-2：内核算子需要 lxml（二进制依赖，故放 extra）
python -m interfacetester run examples/soap_xpath   # 内核算子，期望 2 passed, 2 skipped
```

```yaml
  validate:
    - equal: ["status_code", 500]                 # SOAP 的业务错误也是 HTTP 500
    - soap_fault: ["body", "Invalid parameter"]   # 断言 Fault 内容，而不是「服务挂了」
    - xpath_match: ["body", ["code", "0000"]]     # 简化写法：命名空间前缀漂移（ns1:/abc:）都能断
    - xpath_match: ["body", "//ns1:code/text()"]  # 原始 XPath（内核算子支持，含 count()/string()/轴）
    - xml_schema_match: ["body", {"xpath": "QueryResponse", "xsd": "schemas/query.xsd"}]  # XSD 契约
```

- 内核算子用法、XSD 六条规则、两种写法对照：`examples/soap_xpath/README.md`；
- 两个批次的实现与验证记录：`docs/A2-1开发记录.md`、`docs/A2-2开发记录.md`
  （均需用 `git show docs-before-batch-cleanup:<路径>` 取回）；
- 项目级算子（A1，零依赖退路）：[`examples/soap/README.md`](examples/soap/README.md)；
- SOAP 1.1/1.2 模板、Fault 陷阱、存量资产迁移：[`docs/soap/README.md`](docs/soap/README.md)；
- **未做**：WSDL 驱动生成用例（属独立项目量级，不做）。

---

## 三、快速上手

新建一个 `login.yml`：

```yaml
config:
    name: 登录
    base_url: https://your-api.example.com
teststeps:
    - name: 登录并断言
      request:
          method: POST
          url: /login
          json: {username: demo, password: "${ENV(DEMO_PWD)}"}
      extract:
          token: body.data.token
      validate:
          - eq: ["status_code", 200]
          - contains: ["body", "data"]
```

跑起来：

```powershell
hrun login.yml --html=reports/login.html --self-contained-html   # 转换 + 运行 + 出报告
hmake login.yml                                                  # 只想先看生成的 pytest 代码
```

**再加一层「契约断言」**（契约断言算子 `jsonschema_match`，把响应结构交给 JSON Schema 管；schema 可以放
`debugtalk.py` 里用 `${get_login_schema()}` 返回，也可以写成文件路径。**内联 schema 仅当不含
`$` 键时才可用**——`$schema`/`$ref` 这类 `$` 开头的键会被框架当变量解析，`hmake` 阶段会直接报错）：

```yaml
    validate:
        - eq: ["status_code", 200]
        - jsonschema_match: ["body", "schemas/login.json"]     # 相对项目根目录解析
```

系统学习路径：**[`docs/使用教程-项目级.md`](docs/使用教程-项目级.md)**（四步入门 + 进阶）；
零基础可直接看 **[`docs/小白入门指南：用interfacetester做接口自动化测试.txt`](docs/小白入门指南：用interfacetester做接口自动化测试.txt)**。

---

## 四、文档地图

| 文档 | 什么时候看 |
| --- | --- |
| [`docs/使用教程-项目级.md`](docs/使用教程-项目级.md) | 想系统学：写用例 → 命令行 → 转换机制 → 报告 → 进阶（debugtalk / 参数化 / hook / 超时 / 上传 / SQL / 契约断言 / 数据管理 / CI） |
| [`docs/小白入门指南：用interfacetester做接口自动化测试.txt`](docs/小白入门指南：用interfacetester做接口自动化测试.txt) | 第一次接触，要「照着抄就能跑」 |
| [`docs/新手快速上手.md`](docs/新手快速上手.md) | **不会写 YAML、想先跑通一条**：一条**照做清单**（跑零依赖示例 → 抄最小骨架 → 迁移/生成 → 自查 → 出报告）+ 五个新手必踩坑 + 自查脚本 |
| [`docs/能力清单.md`](docs/能力清单.md) | 判断某能力能不能做、有什么限制（逐项给依据） |
| [`docs/架构与调用链.md`](docs/架构与调用链.md) | **想读源码 / 要改框架**：模块地图、五条主链路（加载/生成/执行/汇总/导入）、数据模型、**防线清单**（假通过 + 假失败，逐条给「曾经的形态 → 谁拦 → 哪个用例守着」）、扩展点与常见误解 |
| [`docs/convert/README.md`](docs/convert/README.md) | 用 `hconvert` 导入 curl / HAR / Postman / OpenAPI（含支持子集） |
| [`docs/ci/README.md`](docs/ci/README.md) | 接 CI：模板用法 + 九个坑（端口、外网用例、离线依赖…） |
| [`examples/quickstart/README.md`](examples/quickstart/README.md) | 想**直接跑一个最小用例工程**：照 [`docs/使用说明-用例级.md`](docs/使用说明-用例级.md) 第二节「从零搭」做出来的样子（零配置即可跑通一条） |
| [`examples/data_management/README.md`](examples/data_management/README.md) | 测试数据管理约定（造数/隔离/回收）与可跑示例 |
| [`docs/soap/README.md`](docs/soap/README.md) | 测 SOAP/XML 接口：请求模板（1.1/1.2）、Fault 陷阱、断言方案与限制 |
| [`examples/soap/README.md`](examples/soap/README.md) | SOAP 结构化断言的算子用法与五个可离线复现的演示（含 XML 取值/多步链路） |
| [`deploy.md`](deploy.md) | 部署、离线安装、extras、FAQ |
| [`docs/使用说明-用例级.md`](docs/使用说明-用例级.md) | **用例工程侧**的文件与文件夹：`debugtalk.py` / `conftest.py` / `.env` / CSV / schemas / IDL 各自做什么、哪些必须哪些可选、**每个项目要不要单独配**；第二节是**从零搭一个用例工程的 6 步示例** |
| [`docs/使用说明-非技术版.md`](docs/使用说明-非技术版.md) | **给不写代码的同事**：那些文件是干嘛的、哪些该动哪些别动、常见问题速查与名词解释（全文不含需要你敲的代码） |

---

## 五、目录结构

```
interfacetester/            # 主包（cli / make / runner / parser / response / step_* / builtin / converters …）
tests/                      # 单元测试 + 本地 mock httpbin（80/443，可配置）+ 导入器黄金文件
examples/                   # 可跑示例：httpbin、postman_echo、unifsp、data、data_management、openapi、soap、body_forms
docs/                       # 能力清单、评估文档、各阶段开发记录、convert/、ci/、soap/
使用教程.md / 小白入门指南…txt / deploy.md / README.md
pyproject.toml              # 依赖与命令入口（唯一事实来源）
requirements.txt            # 锁版本依赖清单（离线/复现部署用）
LICENSE                     # Apache-2.0
```

---

## 六、测试与质量

```bash
# 全量（其中 6 个用例依赖公网 postman-echo.com，网络抖动会失败；见 docs/ci/README.md 坑 5）
python -m pytest tests -q

# 确定性口径（CI 主 job 用的就是这条：排除那 6 个外网用例）
python -m pytest tests -q \
  --deselect tests/step_request_test.py::TestRunRequest::test_run_request \
  --deselect tests/step_request_test.py::TestCaseRequestWithFunctions::test_start \
  --deselect tests/step_testcase_test.py::TestCaseRequestWithFunctions::test_start \
  --deselect tests/step_testcase_test.py::TestRunTestCase::test_run_testcase_by_path \
  --deselect tests/cli_test.py::TestCli::test_debug_pytest \
  --deselect tests/cli_test.py::TestCli::test_run_testcase_with_abnormal_path

# examples 冒烟（本地 mock，不依赖外网）
python tests/mock_server.py &          # 前台常驻；Ctrl-C 退出
hrun examples/httpbin/basic.yml examples/httpbin/hooks.yml \
     examples/httpbin/validate.yml examples/httpbin/load_image.yml
hrun examples/data_management          # 数据管理示例（自带本地 mock）
hrun examples/soap                     # SOAP/XML：A1 项目级算子（零依赖）
pip install -e ".[xml]"                # A2-1/A2-2 内核算子需要 lxml（二进制依赖，故进 extra）
hrun examples/soap_xpath               # SOAP/XML：内核算子（完整 XPath 1.0 + XSD 契约校验）
```

当前基线：**确定性口径 2493 passed / 173 skipped / 6 deselected**（全量 2672 个用例、**1804** 个 subtest）。★本仓是**外发版**：随仓库分发的只有框架本体（内核 + AI 协作层 + `tests/` + `bench/` + `examples/` + `docs/`）；只在**内网工作区**存在的资料（客户接口文档 `project-three/`、探针工作区 `probe_p2_prep/`、内网方案文档）**不随仓库分发**，依赖它们的判据在资料缺席时**按设计 skip**（不是报错、也不假装通过）——173 skipped 里有 47 条属于这一类；在内网工作区里这些判据照旧全程真跑。另有 1 条 `docs_consistency_test` 的"记录总数 vs 实际收集数"会在本行未更新时变红——它正是**基线联动护栏**，更新本行即转绿。

---

## 七、许可与来源

- 本仓库是 **[HttpRunner](https://github.com/httprunner/httprunner) v4.3.5 的二次开发分支**，
  保留了上游 `LICENSE`（**Apache License 2.0**，见 [`LICENSE`](LICENSE)），
  衍生工作同样以 **Apache-2.0** 发布；`pyproject.toml` 已用 SPDX 声明（`license = "Apache-2.0"` + `license-files`）。
- **对外分发/宣传时请注明「基于 HttpRunner (Apache 2.0) 二次开发」，不得声称完全自研，也不要使用 HttpRunner 商标做宣传。**
- 框架内的 `interfacetester` 包名与命令（`hrun`/`hmake`/`hconvert`）为本分支的命令入口；
  上游能力与本分支新增能力的对应关系见 `docs/接口自动化框架升级优化评估.md`（已从工作区删除，
  用 `git show docs-before-batch-cleanup:docs/接口自动化框架升级优化评估.md` 取回）。
