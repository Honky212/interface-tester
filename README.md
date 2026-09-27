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

这一轮做过一次系统性评估（`docs/接口自动化框架升级优化评估.md`，已从工作区删除，需原文用
`git show docs-before-batch-cleanup:docs/接口自动化框架升级优化评估.md` 取回），据此落地了 4 个批次：

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
| [`docs/交付说明.md`](docs/交付说明.md) | **交付前 / 接手里最该先看的一页**：承诺边界（生成可复核初稿 ≠ 无人审核进 CI）、交付物构成、环境前提、**模型支持矩阵**、**本机没验过的面** |
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
| `docs/接口自动化框架升级优化评估.md`（已删除，见下） | 想知道「为什么这么改」：评估结论、优先级、落地状态 |
| `docs/SOAP阶段开发记录.md`、`docs/A2-1开发记录.md`、`docs/A2-2开发记录.md`、`docs/批次A-C修复验收清单.md`、`docs/批次D修复验收清单.md`、`docs/批次E修复验收清单.md`（**均已删除，见下**） | 各功能批次的实现细节、实测数据与踩坑记录。**前四个**（P0~P3 早期记录及 SOAP/A2 两份）用 `git show docs-before-batch-cleanup:<路径>` 取回；**最后三个验收清单的提交晚于该 tag**，要用 `git show f32dea2a:<路径>` 取回——**不要写 `HEAD`**：`HEAD` 会随提交移动，两者都取不回。**最后三份是修复验收清单**（A/B/C、D、E 三批，逐条给出护栏 node id 与「注掉修复即变红」的注入点，附复核命令速查）；批次 D 的那份还留了一处「**测试在守护 bug**」的直证，批次 E 的那份留了两条「**护栏打偏**」的自纠 |

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

当前基线：**确定性口径 2545 passed / 121 skipped / 6 deselected**（全量 2672 个用例、**1826** 个 subtest）。★本机已装上传依赖的核心 `filetype`（`UPLOAD_READY=True`）——**因此早先那条"与代码无关的 failed"（`…test_requests_toolbelt_is_not_a_hard_prerequisite`）已经消失**；若在**未装** `filetype` 的环境里跑，它仍会红（那是环境项，不是代码项）。另有 1 条 `docs_consistency_test` 的"记录总数 vs 实际收集数"会在本行未更新时变红——它正是**基线联动护栏**，更新本行即转绿。
> NOTICE（本机实测口径，2026-09-22，Python 3.12.10 / pytest 9.1.1）：这一行是**未装任何可选
> extra**（无 `test`／`xml`／`upload`／`trustme`）时的实测值，**不是回归**：
>   - **skip 119**：可选依赖全缺，`lxml`（XML/XPath 算子）、`trustme`（HTTPS mock）、
>     `thrift`/`thriftpy2`、`pytest-xdist` 相关的用例**按设计 skip**，逐条原因见
>     `docs/ci/README.md` 的「验证清单」；
>   - 唯一一条与本行无关的红灯是
>     `tests/dependency_consistency_test.py::TestCiJobDependencySetsCoverTheTests::test_requests_toolbelt_is_not_a_hard_prerequisite`
>     —— 它需要 `upload` extra（`import filetype` 失败即报
>     `RuntimeError: uploader extension dependencies uninstalled`），**不是代码缺陷**。
>     ⚠️ 本机**无外网**，`pip install` 会挂起，所以暂时无法装齐 extra 复验；
>     在有网环境执行 `pip install -e ".[test,xml,upload]"` 后应回到此前记录的 1670/9 量级
>     并**更新本行**（本行是全仓唯一事实来源，改它时 `deploy.md` 的验收期望值要**一起改**，
>     否则 `TestBaselineNumbersHaveOneSource` 会变红）。
>
> NOTICE（本轮新增 25 条用例）：基线数字含**本次为《智能化改造方案2》新增的护栏**——
> `tests/golden_baseline_test.py`（5 条，golden 装配链路 + 判据自检）与
> `tests/ai_guardrails_test.py`（20 条，L3 沙盒闸门 / fix 补丁校验 / prompt 一致性对账）。
> 总数 1687 → 1712（+25，方案 2 护栏）→ 1744（+32，含《智能化改造方案 v6~v8》护栏新增：S-2b 判据级 4 条、归一化投影 9 条、元护栏 5 条等）→ 1780（+36，v8 三批收尾：T18 写盘边界扫描 20 条、T6 包形态自检 9 条、S-6 pending 落盘 7 条）→ 1798（+18，v8 第六批：T29 归因六分类与文档缺陷回流 17 条 + 写盘边界注册表对账 1 条）→ 1819（+21，v8 第七批：T27 文档体检闸 `tests/doc_quality_test.py`）→ 1838（+19，v8 第八批：S-7 CI 门禁 + S-8/T13 的 `formatting` 字段 `tests/l2_formatting_test.py`）→ 1864（+26，v8 第九批：T7 归一化管道 `tests/normalize_test.py` + T8 作业隔离 `tests/workdir_test.py`）→ 1869（+5，v8 第十批：T9 golden 3→15 的基线更新 + T10 闸门误伤率审计 `tests/gate_false_positive_audit_test.py`）→ 1891（+22，v8 第十一批：T4 输入预算字节口径 + T5 缓存键全字段 `tests/ai_budget_cache_test.py`，以及 T15 口径唯一化的机器判据 `tests/docs_consistency_test.py::TestDeprecatedWorkloadNumbersOnlyAppearAsHistory`）→ 1925（+34，**P0a 阶段 A/B**：CaseDraft 契约 `tests/case_draft_test.py` 22 条 + 文档切片器 `tests/slicer_test.py` 12 条）→ **1953**（+28，**P0a 阶段 C**：`tests/llm_transport_test.py` 27 条——L3 闸门接线（"拒绝发生在发请求之前"）、重试策略（5xx 重试／4xx 不重试）、缓存键字段与清单**恰好相等**、内容闸不误伤、模块不写盘元护栏；另 +1 为 `tests/interfacetester_ai_pkg_test.py` 的 "PROMPT_VERSION 只能有一个来源"）→ **1965**（+12，**P0a 阶段 D（装配器）**：`tests/assembler_test.py`——S-4「**带陷阱的产物不落 `cases/`**」、产物过内核 `load_testcase_file`、查重硬失败不覆盖、落点只在 `cases/`+`.ai/`、元护栏（掐掉限定词规则/把词边界换成子串 → 自检必红））→ **1982**（+17，**P0a 阶段 E（收口）**：`tests/gen_pipeline_test.py`——端到端 FakeLLM 离线跑通（① `cases/` 真的有用例且过内核加载、② 明文凭据送出去之前被替换、③ 陷阱产物不进 `cases/`、⑤ 预算挡在发请求之前且不重试、⑥ 第二次跑命中缓存不再调模型、⑦ 只写登记根）、修正环（结构错回喂后修好 / 用尽不产半成品）、CLI 退出码口径（2/4）、`--fake` 纯回放、缓存损坏按 miss）→ **1999**（+17，**P0b（断言补全）**：`tests/assertions_test.py`——**15 份 golden 逐份过**（不静默 / 伪存在性断言 0 条 / 算子全在白名单 / 引用句可溯源 / **真的过装配链且降级 0 条**）、可选字段不断言、`TODO_` 落点硬规则（只落 `.ai/draft/` + 登记 pending）、**可复现**（两次推导逐字节相同）、CLI 不需要模型配置）→ **2017**（+18，**T24（TODO_ 落盘纪律）**：`tests/reviews_test.py`——**带 `TODO_` 的草稿转正必被拒**、没写 `approver` 必被拒、填好后转正 + `.ai/reviews/<case>.json` 留痕、**确认后又改一行 → 留痕失效**（元护栏）、查重不覆盖人手写的用例、原子写无 `.tmp` 残留、`reviews` 在 T18 注册表里、两条自检元护栏）→ **2044**（+27，**P1a（失败诊断）**：`tests/analyze_pipeline_test.py`——证据包装配（只打失败包/`siblings` 不含自己/**脱敏保留键名**/body 截断带标注/日志缺失不算错）、**对抗样本**（ANSI/CRLF 归一化后仍命中 + 编的仍被拒）、端到端（**一次调用**/编引用降级"未知"/**自报未知算未降级**/**跨包引用被拒**/未降级占比可见/无失败不调模型）、**断网无部分写**、报告**逐字节稳定**、CLI 退出码 3、两条自检元护栏）→ **2069**（+25，**P1b（结果摘要）**：`tests/summary_test.py`——**数字与 `stat` 块逐字段相等**（含**真产物** `project-two/logs/*.summary.json` 对账）、失败分布按前缀分组（无 `_` 的名字各自成组）、耗时 TopN 同分按名字定序、**导语白名单**（合规采用 / 模板外数字**整段弃用** / **弃用必须落到正文** / TopN 序号算模板数字）、**无 LLM 时模板版仍产出**（`transport=None` + 断网 + 缺配置三种情形）、**CLI 缺配置退出码 0**（与 gen/analyze 的分野）、原子写、报告逐字节稳定、自检元护栏）→ **2097**（+28，**P2a（自愈建议）**：`tests/fix_test.py`——触发条件四类（含**顺序判据**：业务码即使 `check_value is None` 也不给改）、末段业务码判定、行号定位 + **只改一行** + diff **一删一增**、**双前置**（合规过 / **降级补丁必被拦** / 坏 YAML 拒）、**零写盘**（目标 mtime 与内容不变 + `.ai/work` 无残留）、**业务码 100% 拒绝**（6 样本批量 + 批量入口）、**字段改名 8/10**（10 个注入样本）、多候选**不替人挑**、`--apply` **不存在**、退出码恒 0、`fix` **不进** T18 注册表、自检元护栏）→ **2183**（+56，**P3a（只读看板）**：`tests/web_readonly_test.py`——**零写盘**（**全量快照**跑遍所有页面后对账；★元护栏钉住"不能复用 `workdir.snapshot_tree`，它会忽略 `.ai/`/`logs/`——那正是看板唯一能触碰的目录"）、**零执行**（AST 查模块 import + 只查 `WebHandler` 的调用名，不与自检的临时写盘混淆）、**穿越全拒**（7 类样本 × 函数级 + 真实 HTTP；★`logs_evil/` 前缀混淆用**真文件诱饵**，并有元护栏证明 `startswith` 与 `commonpath` 结论不同）、**只监听回环**（默认值 + 四类回环写法 + 对外必带理由 + 违规读根启动即拒）、**§5.5 指针**（含**漏一层 `.data.`** 的元护栏 + "真正的没有断言"必须与"取不到"分开 + `passed` 严格等于 `pass`）、**R8**（四禁词零出现 + 失败文案不缓和 + 元护栏）、**度量来源**（每行必有 source / 报告缺失→"未读到"而非 0% / 无来源项显式标注）、HTTP 契约（200/400/404/405/HEAD/坏 JSON 不静默）、CLI（`serve` 注册 + 默认回环 + 违规读根退出码 2）、两条自检元护栏）→ **2214**（+31，**P3b-1（确认门）**：`tests/confirm_test.py`——**三条硬纪律**（**reject 必填理由**：空/纯空白/全角空格全拦；approver 必填；空 `decisions` 拒收——"空表返回成功会造假证据"）、**坏 body 不猜**（6 种形态）、**真落痕**（`谁/什么时候/对什么/对哪一版/为什么` 五项都在 `notes` 里）+ **内容哈希绑定**（同一版放行 / 改一行失效，T24 联动）、**被拒时不留痕**（半份证据比没有更坏）、**`/confirm` 的写例外没有扩散**（其余路径 PUT/PATCH/DELETE 仍 405）、**422 而非 200**、`GET /pending` 表单、`/fix` 缺 path 400、超大 body 413、坏清单**不跳过**、AST 查 `confirm.py` **无写调用**且**不进** T18 注册表、自检绿）→ **2241**（+27，**P3b-2（触发运行）**：`tests/jobs_test.py`——**闸门四类**（运行域含 `cases_evil/` 前缀混淆 / **参数注入 `-` 开头一律拒且拒因码独立** / L3（沙盒未开、非回环即便沙盒开也拒、回环+沙盒开放行）/ 超时越界）；**★元护栏**：patch `l3.check_l3_allowed` → 本模块结论**必须**跟着变（证明用的是**同一个**闸门，不是另写一份）；**零副作用**（被拒时作业号/目录/命令全为空 + **`.ai/jobs/` 目录集合前后不变**）；`dry_run` 不执行；**★4 条真起子进程**（退出码非 0 落盘 / 作业目录隔离 + `result.json` 含触发者与截尾标记 / 闸门拒时零进程 / 列表按作业号倒序）；**`/jobs` 的 HTTP 契约**（页面 200 / 未知 job 404 / 缺 `triggered_by` 422 / 坏 JSON 422 / 被拒时零副作用）；**★`jobs` 在 T18 注册表里 vs `web`/`confirm` 刻意不在册**的对比）→ **2277**（+36，**P3c（加固）**：`tests/audit_test.py` **18** 条 + `tests/packaging_test.py` **11** 条 + `tests/web_readonly_test.py::TestHardening` **7** 条——**审计**（分段+排序、**元护栏**证明朴素混排会让 `(无时间戳)` 冒充"最早"、时间只来自证据（把 `approved_at` 设成 2020 就要读到 2020）、谁/为什么/内容哈希都带上、接受+拒绝同时出现标 `mixed` 不猜、**读不到单列**、待确认**不编时间**、**同一口径散在三处用判据绑住**、只读→不进 T18）；**加固**（默认回环全放行、IP 白名单 403、**★伪造 `X-Forwarded-For` 骗不过白名单** + **元护栏**证明 `trust_proxy` 之后它才生效、缺 token **401 + `WWW-Authenticate`**、错 token 403、对 token 200 且**页面里永不出现 token**）；**离线**（零依赖 + **顶层 import 只许 stdlib/本仓** + 复用内核依赖**两条件**（内核已声明 + 只在函数体内）+ **离线部署步骤就在 `requirements.txt`** + 入口 `main` 用 `is` 比身份 + **把"未实测打轮"写进代码**）→ **2290**（+13，**P3d（前端）**：`tests/frontend_test.py`——**★先看再改**（真的起服务 + 浏览器实看，而不是猜）：发现 **`/audit` 没页面**、**`/fix` 有页面没入口**并都补上；**导航可达**（导航里每一项都 200 + 元护栏式地钉住 `/audit` 在导航里 + 运行明细页给 `/fix` 入口）；**无构建链**（11 个构建链指纹 + 页面无 `import`/`type=module`）；**离线自包含**（每页外部引用 0 + `charset`/`lang`）；**可用性最低线**（每页恰好一个 `<h1>` / 表格都有 `<thead>` / 按钮都有文字）；**不做自动刷新**（无定时器 + 有表单页必须提示"刷新会清空" + **元护栏**：`/jobs` 必是那条款判据的非空样本）→ **2291**（+1，**配置指引纠正（实测）**：`tests/llm_transport_test.py::TestConfigStrictness::test_hint_gives_environment_variables_not_dot_env`——★起因是**用户问"在哪配置 BASE_URL/API_KEY"**，顺着问题去查代码，发现 `_CONFIG_HINT` 里那句"**写进项目 `.env` 即可被 loader 带起**"**不成立**：`cli.py` 的 `LLMConfig.from_env()` 跑在内核 `load_test_file` **之前**，项目 `.env` 里的 `INTERFACETESTER_AI_*` **永远**进不到配置里（该句是从方案 §6 抄进代码的，四份文档都有）。危害不是"报错"，而是**用户照指引做却反复失败**，之后很可能把 key 塞进全局环境变量或命令行——**凭据暴露面反而变大**；连带把 `deploy.md` 原本**完全没有**的 AI 配置章节补成 §5.1、README 补配置节。判据四条：必须给 PowerShell 写法 + 必须给 Linux/macOS 写法 + 必须点明 `SANDBOX`（否则配好 `BASE_URL/MODEL` 仍被 L3 拒且不知道为什么）+ **反向**断言"不许再声称 `.env` 会被带起" + 必须**主动**说"不读 `.env`"）→ **2293**（+2，**失败指引纠正（真模型实测）**：`tests/gen_pipeline_test.py::TestFailureGuidanceTellsTheTruth`——★**第一次真模型端到端**（Ollama `gemma4:latest`）跑 `gen` 时暴露：5 片**全部返回 `[]`**（空数组）→ L1 schema 反复不过 → 0/5 产出、退出码 1，而 CLI 打印"未产出的切片在 `.ai/failed/<用例>/NEEDS_HUMAN.md` 里写了人接着要做什么"——**那个文件根本不存在**（整个 `.ai/` 里没有任何 `NEEDS_HUMAN.md`）。根因：`NEEDS_HUMAN.md` **只在装配器的写盘前置闸门 REJECT 时产出**，而这类失败发生在**装配之前**（`assembly is None`）；真正有用的证据在 `.ai/manifest/` 的原始响应里（那条 `"response_text": "[]"`）。**成对判据**：装配前 → 必须指 `.ai/manifest/` 且**不许**出现 `<用例>/NEEDS_HUMAN.md`；装配后 → **仍然**指 NEEDS_HUMAN → **2295**（+2，**输出契约纠正（真模型实测）**：`tests/case_draft_test.py::TestOutputContractIsActuallyGiven`——★**用户拿 `project-three` 的真实接口文档试 `gemma4:latest`** 时暴露：system prompt 一直说"只输出符合**给定的** JSON Schema 的一个 JSON 对象"，但**那份 Schema 从来没给过**——`prompts` 里没注入它的代码、`schema` 里没导出它的函数、`response_format` 也是 `None`（manifest 记着 `null`）；**三处都没有** → 模型只能猜 → 15 片全部猜错形状（`{"case_name":…}` / `{"testcase":…}` / `{"type":"r…"}`），修正环又只报"字段不认识"、**不告诉它正确形状** → 45 次调用 **0 产出**。补上契约（`schema.draft_contract_text()`，**从字段白名单生成**，含"没有完整接口就输出 `[]`"的出口）后同一条链路产出 **2 条用例**（含 `POST /api/directory/create` 那条）。**判据两条**：契约键名集合与字段白名单**一一对应**（15 个 subTest）+ prompt 里真的有契约、且不再出现"符合给定 JSON Schema"这种自相矛盾的措辞。**★连锁修复**：① `INPUT_BUDGET_BYTES` **8000→12000**（契约让 system 从 2553→4094 字节，一个贴着 8000 边的切片立刻变 9541 → **整篇 gen 中断**；依据 system 4094 + 最长单段约 5100 + 模板 400，留 ~25% 余量；v8 里 5 处数字同步）；② `gen.generate_cases` **单片超预算不再中断整篇**（原行为与"单片失败不拖累其它片"的立论直接冲突）；③ 两处**硬编码旧值**的判据（`prompt_version` 写死 `v2.0`、预算夹具写死 7000）改为**跟常量走**——否则常量一改，判据会**静默测偏**（实测：夹具变成"第 2 轮才超"，`call_count==1`）；→ **2346**（+11，**第十五批（P2b 收口：L3 沙盒）**：`tests/debugtalk_draft_test.py::TestL3Sandbox` + `TestCliSandboxFlag`——口径 **允许执行 · 只跑已知答案测试 · 允许真实加密运算**：闸门在前（拒绝时**零子进程**）、真加密（`hashlib`/`hmac`/`base64`）的 KAT 必须过、出网/文件/动态加载必须被拦、超时如实报"没结论"、子进程 env **无凭据**、报告必须写明**在哪验的**、CLI 未开沙盒退出码 **2**）本行已与 `--collect-only` 重新对账，2026-09-25）→ **2348**（+2，**第十八批**：AI 旁路包进架构文档的模块护栏 `tests/docs_consistency_test.py::TestArchitectureDocCoversEveryModule`——§2.2 的 31 行模块地图 + "按切片判"防同名模块假绿 + 注入式元护栏）→ **2351**（+3，**第十九批**：超时文案 `tests/llm_transport_test.py::TestTimeoutHint` **3 条**——超时类失败必须点名 `INTERFACETESTER_AI_TIMEOUT` 与**当前上限值**、**非超时失败不许带**、提示值取自 `config` 而非写死）→ **2415**（+30，**《未做完的待决策项》§九 第一档①②③④**：`tests/quality_test.py` **23 条**（质量状态机 / 非法转换被拒 / 仅协议断言待人工 / ★**没有运行证据永不到 `runtime_verified`**（穷举参数组合的元护栏）/ 纯函数零写盘元护栏）+ `tests/assertions_test.py` **+7 条**（条件返回字段不断言、枚举列头 `说明`≡`取值范围`/`允许值`/`约束`、两条 golden 回归**成对**判据）；**同时**把"装配默认落 `cases/`"的旧断言改成"**只落 `.ai/draft/`**"——这是用户可见的行为变更）→ **2426**（+11，**WP1.2（审核侧接质量状态）**：`tests/reviews_test.py` 11 条——**未处置的 blocker → 拒** / 只处置一部分 → 拒 / 点名不相干的词 → 拒 / 逐条点名 → 放行且留痕含 `quality_status`·`blockers`·`resolved_blockers` / 干净草稿不用处置（成对判据）/ 复算入口**只读** / CLI 三面（`--list` 显示状态、`--approve` 先摊开摘要、`--resolve-blocker` 端到端）/ 元护栏：处置判据被改成恒放行则自检必红）→ **2430**（+4，**闸门拒的草稿：状态必须显示 `rejected`**——★**端到端实测暴露的缺陷**：`quality_of_draft` 一开始只看 `unknowns`，把一份被 S 闸门拒掉的草稿在 `review --list` 里显示成 `static_valid`（审核者会对着错状态签字）；修法 = 传 `draft_path` 就**重跑闸门**且**闸门优先**。判据四条：清单显示 `rejected` / `--approve` 拒闸门草稿 / `--resolve-blocker` **覆盖不了**闸门硬结论 / `gate_check` 返回闸门编号）→ **2443**（+13，**C：只读交付面接质量状态**——评审 §五 要求"报告**并列展示**已覆盖/未覆盖/协议级/降级/状态"，而**只在 CLI 打印时只读交付物看不到**。新增 `reviews.drafts_overview()` 作为**唯一口径**（CLI / 看板 / 离线导出共用），看板待确认页新增"草稿与质量状态"栏（含 blocker 与已点名处置），审计时间线新增"质量"列（**老留痕如实标"未记录"，不拿"没问题"填空**），离线快照随渲染器一并继承。判据：`reviews_test` +5（含 `with_gate=False` 必须标 `not_probed`——**不猜**）、`audit_test` +3、`web_readonly_test` +4（含"闸门拒的草稿不许显示成通过"）、`export_test` +1。★**`summary` 刻意未改**：它渲染的是"一次运行"的结果，草稿质量不属于运行结果——硬塞进去会造一个**没有来源的数字**）→ **2449**（+6，**§9.6 的 ①归属闸门 + ②逐片产出**：真实客户文档实测 **17 段全识别**（原 1）、**16 条产出 + 1 条被 S3 响亮拒绝**（认证段自身 0 断言）、**每条只带自己那一段的字段**、段外断言进 `unknowns` **可见**。判据：`tests/assertions_test.py::TestSectionWiseAssertions` **5 条** + `TestCliAssertionsOnly::test_cli_produces_one_draft_per_interface_section`（CLI 逐片产出 N 条且仍只落草稿区）。★实现时踩到：`slicer` 的片是**扁平**的（不含子标题）→ 按片取会把 `### 请求体` 的字段表切掉（"每个接口 0 条断言"），故 `interface_sections()` **按标题层级算跨度**）→ **2458**（+9，**§9.8 口径 B2：响应数据表 → 一条 `jsonschema_match`**——真实客户文档的响应表是 `字段名｜类型｜说明`（**没有「必填」列**），按旧纪律 **62 行响应字段一条断言都产不出来**；现在按结构识别为响应数据表，生成一条 schema 断言，**不写 `required`**（字段缺失不算失败、存在但类型错才算），实测**覆盖字段 9 → 75**、"必填列没写清"跳过 **62 → 0**。判据：`tests/assertions_test.py::TestResponseTableSchema` **8 条**（含"**没有 `required`**"/"无 `$` 键"/"引用句=整表且逐字"/"类型不明确只报告不猜"/"错误码表不误判"）+ `::TestResponseTableSchemaEndToEnd` **1 条**（覆盖按**叶子**展开、不含容器 `data` 与假字段 `body`、分母按段）。★顺带修掉两处既有缺陷：`documented_fields()` 把表头 `字段名` 当字段算进分母、以及逐片后"分子按段/分母按整篇"导致覆盖率被低估）。 → **2469**（+11，**§9.9 (c) 派生的两个小修**：`tests/gen_pipeline_test.py::TestCrossReferenceSlices` **3 条**（只有交叉引用的片跳过 + ★**成对**：标题式/表格式/curl/**混合片**一条都不许被跳 + 判据要两个信号同时成立）+ `::TestDropSummaryAcceptsBothShapes` **2 条**（★两条通路的对象形状不同：`GenResult.assembly.coverage` vs `AssemblyResult.coverage`；分母 0 时不许报 0%）+ `tests/assembler_test.py::TestT28DropAccounting` **5 条**（按 kind 分类 / 分母 0 → `None` / 渲染含率与原因 / ★**分母是"提议的条数"**——写成"留下的 + 被丢的"会**双重计数**（1 条被拦会算成 50%）/ 没有降级时不印比例）+ `tests/assertions_test.py::TestCliAssertionsOnly::test_cli_prints_the_t28_drop_summary` **1 条**（CLI 收尾必须打出 `T28 降级统计：x/y = z%`）。实测：真实文档含端点形态的片 **18 → 17**（被跳的正是 `## 5. 元数据服务接口` 那条**父段片**，其余 17 片**零误伤**）；确定性通路 **0/37 = 0%**） → **2489**（+20，**第二档 WP2.2「证据绑定」**：新增模块 `interfacetester_ai/citation.py`（**12 条**：三种坏引用——引标题/自造锚点/只引字段说明——的解析 + 尾随冒号 + **归属闸门**与**共享节**成对判据 + ★**扁平片比标题跨度少一行**的实测回归 + 绑不上时给最近标题候选 + 自检 + **纯函数零写盘**元护栏）+ `assembler` 侧 `TestEvidenceBinding` **6 条**（绑定后进用例且留痕 / **不绑定时老行为不变**（成对）/ 片外共享节标 `shared` / **别的接口段拒绝** / ★**绑了又被别的原因拦下 → 不留悬空记录** / `quote-bound` 按本仓口径**升级成 blocker `unknown-critical`**）+ `gen_pipeline_test` 端到端 **2 条**（弱引用**照样产出**且交付物 `.unknowns.json` 里有 `quote-bound` / **原句引用不许出现** `quote-bound`（成对））。★实测（同一份模型留痕）：模型给的 20 条断言只按**句级**判要拦 **15 条（75%）**，绑定后只拦 **3 条（15%）**，且那 3 条**全是"把示例值当契约"**（本该交人工）；同时修掉三处真实缺陷——`body` 这类**容器名被当文档字段**去要求出现在引用句里（病因指错）、**扁平片少一行**导致本节窗口被判成"别的接口"、**绑定窗口太宽**导致 ③ 限定词检查拿同表别的字段误判） → **2496**（+7，**§9.11 `type_match` 类型名处置**：`tests/assembler_test.py::TestTypeMatchExpectMustBeGateLegal` **4 条**（无歧义别名归一（8 个 subtest：`string`/`字符串`→`str`、`整数`→`int`、`对象`→`dict`…）/ ★**成对**：合法名一字不改 / 有歧义 `number` → 摘掉那一条而**另一条照常进用例** / ★**端到端：带 `type_match: number` 的草稿不再被 S7 拒**）+ **§9.12 陈旧留痕**：`TestStaleNeedsHumanIsClearedOnSuccess` **3 条**（通过 → 清掉早先轮次的 `.ai/failed/<case>/` 且 `cleared_needs_human` 有值 / ★**成对**：被拒 → 照旧写 `NEEDS_HUMAN` / 本来没有 → 字段为空）。★这两条都是**真实端到端复跑**（`gemma4:latest`，20 次调用）带出来的：复跑实测 **接口覆盖率 17/17 = 100%**（上一轮 12/18）、**`T28 降级统计 14/80 = 18%`**（上一轮 40%）、`quote-bound` **23 条**、**0 片失败**） → **2500**（+4，**§9.13 打通"生成 → 真跑一遍"**：`tests/assertions_test.py` **4 条**——`--base-url` 写进产物 `config.base_url`（给了就不该再警告）/ ★**成对**：不给就响亮提示（点名 `base url missed` + 把**文档 §1.1 自己写着的服务地址**摊出来）/ `gen.documented_base_urls()` 从真实文档捞出三个环境 / ★**成对**：`curl` 示例里的 URL **不许**混进候选。★配套产物：`probe_p2_prep/mock_file_service.py`（与文档同源的 mock）+ `e2e_run_generated.py`（转正 → `haify run` → 汇总）→ `out_e2e_run.txt`。★实测（那次复跑的 17 份真产物）：**17/17 转正、17/17 真跑起来、2/17 跑绿**——跑不绿的根因是**这份文档的 `成功响应` 全是 `data: null`**，而失败的断言**大多是在 `body` 上断言请求字段**（`path`/`sourceDirName`）：**在文档语义下本就不可满足**，且**确定性通路同样中招** → 登记为**下一档核心项（请求侧/响应侧字段归属）**）→ **2500**（±0，**§9.14 落地「响应侧归属」闸门**——上一行登记的下一档核心项，**本轮做完**：`body.X` 的 `X` 必须在**本接口段**的响应侧有据（响应数据表 / 响应说明句 / 成功响应示例的键），否则**摘掉**并计入 T28 降级（新 kind `response-ownership`）；同时给确定性通路补上"**正面半**"——`derive_from_response_examples()` 从**成功响应示例**推信封断言（bool/null/数字→`equal`，字符串→`type_match str`），否则"只写信封"的文档会被摘成**零断言**。**判据全部是成对/注入式的**，落在既有模块自检与既有测试里：`assertions.run_selftest()` 新增 ①-b（请求侧独有 `authToken` **不许**被断言 / 响应侧有据的 `sku` **不许**被误摘）+ ① 增 `equal body.code`（信封正面半）；`tests/merge_test.py` 夹具与真样本判据、`tests/assertions_test.py` 的 `TestResponseTableSchema`/`EndToEnd`/golden 回归、`tests/gen_pipeline_test.py` 的陷阱夹具**逐条更新到新口径**（陷阱那条正是抓出"摘掉会掩盖 S1 REJECT"的判据）。**实测（同一批真产物，只换口径）**：确定性通路 17 段各有可跑断言、`_oauth2_token` 段 0 断言**响亮拒绝**；模型通路**离线重装配**（`probe_p2_prep/reassemble_offline.py`，用 `.draft.json` 快照 = 同一份输入、不同口径）`response-ownership` 降级 **3 → 12 条**；**端到端真跑 2/17 → 4/17（12% → 24%）**，剩余 13 条失败**全是另一类**（期望值语义：`string_equals: [body, 'true']`、从示例 JSON 剪的片段、错误码断在成功路径）→ 登记为本轮的下一个核心项。★度量手段本身也是产出；★踩坑六条（文档级并集 / 祖先节并集 / 歧义引用句 / 文档标题"统一" / 容器名豁免 / L3 沙盒 `l3_refused`）逐条记在 §9.14）→ **2508**（+7，**§9.15 落地「期望值语义」闸门**——§9.14 剩的那一类，**本轮做完**：只做**可证**的三条——① 算子吃字符串而 expect 不是字符串（`string_equals: [body.success, true]`，内核字符串守卫**当场报错**）；② **整包**跟字符串比（`string_equals: [body, 'true']`、`[headers, …]`，字典比字符串永远不成立；★**例外**：**本节**文档明说响应是纯文本/二进制 → 放行）；③ `contains` 的 expect 是**JSON 片段**（对字典找的是键/元素，序列化文本的片段永远命中不了）。**刻意不判**：`contains: [body, 'FILE_1018']`（正常取值真可能命中）、`equal: [body.success, false]`（负例步骤完全正当）、schema 形状与示例冲突（属文档缺陷回流）——**误摘比漏摘更坏**。判据：`tests/assembler_test.py::TestExpectSemantics` **8 条**（3 条拦下 + 5 条成对/不判：字段级字符串断言放行、纯文本响应放行、正常取值 `contains` 放行、负例 `equal` 放行）。★**本轮还更正了 §9.14 的一处误判**：把"失败明细"读成"错误码断在成功路径上"**是错的**——按"读最终产物"重看 `cases/*.yml` 后发现模型**把负例拆成了独立 step**（`成功查询元数据列表` / `路径不匹配时返回错误码` / …），**这是对的**；上一轮只读了"内核输出尾部"（只显示最后失败的那一步）→ **同换一个读法结论就反了**，所以"读最终产物"还得**读全**（步骤名、多步结构）。**实测（同一批真产物）**：离线重装配 17/17、`expect-semantics` 降级 **0 → 16 条**；转正 **11/17**（另 6 份变"断言真空/只剩协议级" → **响亮拒绝**，不冒充跑绿）；真跑 **4/11 绿**，**剩余 7 条失败逐条分类后已无一条"必红"**——**负例步骤 5 条**（mock 只照文档的成功响应应答，不会模拟错误条件 ✗ harness 限制）+ **schema 形状与文档示例冲突 2 条**（文档自相矛盾 ✗）；端到端报告同步新增**步骤名**打印（好让人一眼分清"用例错"与"服务端没模拟"））→ **2532**（+24，**§9.16 让"文档写明的请求参数真的发出去"**——§9.14 第六节第 2 项 / §9.15 第四节第 3 项，**本轮做完**：`assertions.request_payload_from_rows()` 按**本段**的请求字段表装配 `headers`/`params`/`json`/`data`，`to_draft_payload(..., request=…)` 合并进产物（默认 `None` = 老行为一字不变）；**第 1 层落点识别**：头表（首列「头名称」）→ `headers`、参数表 → `params`、其余 → `json`（`x-www-form-urlencoded` → `data`、`multipart` → **不落**并点名）；**第 2 层取值**：表格「**示例值**」列 → 本段「请求体示例」同名字段 → 一律兜底 `${ENV(TODO_…)}`（**绝不编值**）；凭据名（`Authorization`/`token`/…）**只写 `${ENV(名字)}`**、可选字段**默认不发**、JSON 体自动补 `Content-Type`（每条都在 `unknowns` 里可见）；**`todo_findings` 补扫请求值**（否则"参数还没填"只在 `promote_draft` 的拒绝里看得见）。★**判据全是成对/注入式的**：`tests/assertions_test.py::TestRequestPayloadFromFieldTables` **21 条**（该发的 8 类 + 不该发的 7 类 + 接线 2 条 + 请求侧不产断言 + 说明列"必须与…一致"提示 + 公共节继承/不继承），`tests/assembler_test.py::TestRequestPlaceholdersAreAlsoRegistered` **2 条**，`assertions_test` 端到端 **1 条**（产物里真有 `json`）。★**实测（同一份真文档 + 与文档同源的 mock）**：确定性通路 17 段 → **6 段 `json` + 9 段 `params`**（改造前 **0** 段），**0 个 `TODO_`**（值全来自文档「示例值」列）；T28 降级 **1/62 = 0%**；端到端真跑 **4/11（36%）→ 11/15（73%）**，而**剩余 4 条失败全是"文档自相矛盾"**（响应数据表的形状/类型与它的示例冲突 → §9.14 第六节第 2 项）。★**新增"回读"证据链**（`probe_p2_prep/mock_file_service.py --record` + `e2e_run_generated.py --record/--out`）：mock 照录**收到的请求** → 本轮 **19/19 条请求都带文档声明的请求头、6 条带请求体**（模型通路对照：26/26 带请求头、仅 2 条带请求体）——★为什么必须回读：mock **不校验**参数，"没发参数"在跑分上**照样是绿的**（静默假通过）；★报告分两份存（`out_e2e_run.txt` = 模型通路 / `out_e2e_det_run.txt` = 确定性通路），避免换通路跑时互相冲掉。★过程中被**自己的用例**抓出两个真缺陷并修掉：① GET 段的「字段名」表其实是**查询参数**（协议事实）→ 原来会产出必红的 `body.path`；② `Content-Type` 只在"已有头表"时才补 → **没有头表**的接口发 JSON 体时不带头 ✗）→ **2543**（+31，**§9.17~§9.20 四项收口**：**§9.17** 响应数据表 vs 它自己的成功响应示例**形状冲突** → **不产**那条必败的 `jsonschema_match`，改按**文档缺陷回流**（`assertions.find_doc_defects()` 纯函数 → `gen` 走**已登记的写入者** `diagnosis.write_doc_defects()` 落 `reports/doc_defects.md`，**不新增写盘者**；无缺陷时 `clear_stale_doc_defects()` 清陈旧；★踩到并修掉 `section_tree()` ↔ `derive_from_response_tables()` 的**无限递归**，改用切片器的 `heading_spans()`）；**§9.18** curl 示例当**取值来源**（优先级：本段 curl → 表格「示例值」列 → 本段请求体示例 → `TODO_` 占位；★curl 只作取值来源、**不凭空新增字段**；★修掉 `-d '{"a": 1}'` 双引号嵌套**永远匹配不到**、以及 curl 的"桶"与行上"location"对不上导致取值取不到两个真缺陷）；**§9.19** 列头词表与落点判据**合一**到 `doc_quality`（改造前两套词表**已经不一致**）；**§9.20** 草案契约补 `upload`（内核早有、草案没有 → 上传类接口的字段只能整块丢）。★**判据**：`TestResponseTableShapeConflict` 6 条 + CLI 端到端 1 条（冲突→落回流清单；**修好后→清陈旧**）、curl 4 条、upload 1 条、词表合一的既有 120 条回归。★**实测（真文档 + 同源 mock）**：确定性通路 `jsonschema_match` 8 → **4 条（全部与示例一致）**；端到端真跑 **11/15（73%）→ 14/14（100%）**（剩余失败 **0**：4 条冲突转为回流清单、1 条上传类因"值未填不许转正"被正确拦在分母外）；回流清单 **4 条**、每条带**双方原文证据** + 改法；回读 **14/14 带文档声明的请求头、6 条带请求体**；上传类产物实测带 `upload: {path: ${ENV(TODO_PATH)}, files: ${ENV(TODO_FILES)}}`）→ **2548**（+5，**§9.21 让"负例步骤"可验证**：① **文档驱动的错误条件引擎**（`probe_p2_prep/mock_file_service.py`）：从**全篇含错误码的句子**（含公共错误码表）推导**可判定**的条件 —— 关系词→谓词、**反引号名字 / 字段说明**绑两个请求值、★**要求取反**（"必须一致"是要求，mock 要的是**违反条件**，不取反会"正例全红、负例全绿"）；命中就按该节失败响应示例的形状回失败体，并把"命中了哪条规则"写进回读文件；**判不了的点名**（真实文档：**可模拟 10 条 / 不模拟 25 条**，逐条列出）。② **端到端按正例步/负例步分开统计**（新增 ⑦ 节）。③ ★**连带更正 §9.15 的一条判据**：`contains: [body, 'FILE_1018']` 早先写"刻意不判（可能真的成立）"——内核 `comparators.contains()` 就是 `expect in check_value`，对**字典**测的是**键** ✓ → 取值**永远命中不了**；实测证据：mock 已回 `errorCode: FILE_1018` 而断言仍红 ✗ → 补判据 **③-b**（容器 + `expect` 不是文档化键名 + 本节响应是 JSON 对象 → 摘掉，提示里给正确写法 `equal: [body.errorCode, …]`）。④ **§9.17 的判据扩到装配器**（`check_schema_vs_example`）：**模型产的** `jsonschema_match` 也要与**文档自己的示例**对账（原先只守确定性通路）→ kind `doc-schema-conflict`，布尔/整数互认避免误伤。★判据：`TestExpectSemantics` ③-b 3 条（含成对）、`TestSchemaVsExampleConflict` **4 条**（含 3 条成对）。★**实测**：**负例可验证性能力证明** —— 手写正确算子的 3 步用例（正例 + `FILE_1018` + `FILE_8002`）对带规则引擎的 mock **全绿**（`1 passed, exit=0`）；模型通路真跑 **4/11（36%）→ 4/6（67%）**（**纯正例 4/4 = 100%**，含负例步 2 条**全部**落在"不模拟"清单里的条件 → **归因清楚**）；确定性通路仍 **14/14**；离线重装配降级 **50 → 57 条**。★探针纪律补一条（踩了两次）：**换口径后必须重新装配草稿，不许复用旧产物**（旧产物里还带着已被新判据摘掉的断言，结论会假））→ **2573**（+25，**§9.22 错误条件引擎从 1 族扩到 5 族** + **§9.23 提示词补通用正例**：① 引擎新增**非空/上限/存在性/格式**四族（全部**文档驱动**：`必需列` 决定要不要判"不能为空"、`类型列` 判列表字段、**示例值列**判"这条格式要求是不是针对这个字段"（实测：错误码表说"目录名称必须以/开头并以/结尾"，而 `dirName` 的示例值偏偏是 `doc` ✗——照绑会把**文档自己的例子**判错）、**文档显式豁免**要读（创建目录的 `path` 只需以 `X-Path` 开头 → 规则**放宽成前缀**而不是丢掉））；可模拟 **10 → 129 条**，判不了的条件陈述 **25 → 13 条**且逐条点名（另有 11 条是示例 JSON 里的错误码行，单列不混算）。② ★**成对能力证明**（`probe_p2_prep/probe_negative_cases.py`）：五族 × (正例不误报, 负例必命中) = **17/17** —— 只验负例会漏"误报"，而误报更坏（它把**正确的用例**判红）；③ 这一轮**被成对判据抓出 6 个真缺陷**并逐个修掉：**方向反了**（「不能相同」被绑成 `!=` → 正确请求被判错、违规请求反而放过 ✗）、**表格错误码单元格污染字段匹配** ✗、**`最多上传10个` 正则漏匹配**（连带暴露"全篇候选绑不上就**静默消失**"的记账漏洞 → 补 ④ 兜底点名）、**存在性族两码争绑**（不存在的**目录**被回成"文件不存在" ✗ → 按**字段说明的形状**（目录/文件）分流）、**同字段自比**、**同对字段方向冲突**（文档同时写"新文件名不能与原文件名相同"与"新文件后缀必须与原文件一致"，后者**绑错字段**且方向相反 → 留**硬证据**（整段说明原样命中）那条、丢前缀兜底的）；④ **提示词**：`render_system_prompt()` 补一条**通用**正例（`contains` 对**字典**测的是**键** → 断取值要写 `equal: [body.errorCode, 'FILE_1018']`），`PROMPT_VERSION` `v2.3 → v2.4`（它进**缓存键**，不换版本号等于"缓存里那份 prompt 是哪一版"说不清 ✗），并加**与渲染同源**的对账 `audit_operator_guidance()` + 元护栏（删掉关键串必须变红）——★**A/B 实测**（同文档同模型）：v2.3 那批产物有 **4 条** `contains: [body, 'FILE_xxxx']`（对容器断取值，**永远命中不了**）✗，v2.4 这次 **0 条**，改成 **13 条** `body.errorCode` + 比较算子 ✓；预算同时复核：system **4951** 字节 + 最大片 = **8936 < 12000** ✓（余量 26%））。★判据：`tests/error_rule_engine_test.py` **21 条**（每族一条**只含该族**的最小文档 + 正/负成对 + 元护栏（抹掉词表项该族必须少一条）+ 真文档冒烟（五族全绑、可模拟 ≥100、判不了 ≤14）+ 只允许 import 产品的两个函数）与 `tests/ai_guardrails_test.py::TestOperatorGuidance` **4 条**（在场 / 给出正确写法 / 元护栏 / **不许出现项目专名**）。★实测（真文档 + 同源 mock）：确定性通路 **14/14（100%）**、模型通路 **5/6（83%）**（**纯正例 4/4**；含负例步 2 条中 **1 条真绿** —— T4_2 的"版本不存在"现在被 mock 按文档回 `FILE_2005` ✓；剩 T3_1 是"**请求里没有『大小』这个事实**"（只发了文件名）→ 服务端判不了，**归因清楚**，不是用例错））→ **2584**（+11，**§9.24 给 `analyze` 补"输出契约 + 修正环"**（真模型事故的直接对策）：① 实测事故（同一份真 summary、同一台 ollama）—— `qwen3.8:27b` 成功（6 条证据包 → 6 条结论、未降级 6/6 = 100%、引用逐字可溯源），而 `gemma4:latest`/`gemma4:12b` 返回 `<unused50>`×N（**不是 JSON**）→ `LLMResponseFormatError` → **整次失败、无报告** ✗，且**这件事当时没有任何东西可观测** ✗（单测用 `FakeTransport`，"Fake 全绿 ≠ 真模型能用"）。② 两个真缺陷：**system prompt 没给输出形状**（只说"符合给定 JSON 结构"，而那份结构从来没给过）+ **没有修正环**（`gen` 有 `MAX_ROUNDS=2`；而 `llm.complete` 的重试只覆盖**传输层 5xx**，格式错不在其内）；③ 落地：`diagnosis.diagnosis_contract_text(expected)` **从 `CAUSE_TYPES`/字段清单生成**（禁止手抄）并渲染进诊断 system prompt，`analyze.parse_diagnoses(strict=True)` 用**同一份常量**判形状（顶层键 / item 必填键 / 引用项键 / **条数必须一一对应**），不合格 → `classify_diagnosis_error` 选模板**回喂**重试（≤ `MAX_ROUNDS`，轮数**从 `gen` 取**、回喂文本用 `gen.truncate_feedback` 有界 ✓ "重试必须改变输入" ✓），**用尽 → `DiagnosisFailed` → 退出码 4 且不产半成品**；④ **可观测**：报告与 CLI 都写"修正环 N 轮被拦（第几轮、哪类）"，新增探针 `probe_p2_prep/probe_analyze_models.py` **按模型**统计跑通率（独立工作区、真出网、每个模型一落盘）。⑤ ★**判据里最要紧的一条是"分工"**：只有**形状**错才重试，**类别不合法 / 引用编造仍然只降级**（混起来会把"模型守规矩地说不知道"当结构失败去重试，白烧钱 ✗）。★判据 **11 条**（`TestDiagnosisContractAndRefineLoop`）：契约在场 / 契约**随 `CAUSE_TYPES` 变**（元护栏）/ 抹掉关键串必红 / 坏 JSON 回喂后修好 / **重试改变了输入** / 少一条也回喂且回喂里说清 / 类别与证据**不触发重试** / 用尽抛错且 `reports/` 未创建 / strict 与宽容解析并存 / 轮数与 `gen` 相等 / CLI `--max-rounds` 与退出码 4。★**实测（真模型探针）**：`gemma4` 系模型**仍然跑不通**（契约 + 3 轮修正环也救不回它，但现在是**响亮的 4 号退出码 + 明确报错 + 无半成品** ✓），`qwen3.8:27b` 跑通 ✓ —— 结论从"看运气"变成**可测量的模型可用性**（见 `probe_p2_prep/out_analyze_models.txt`））→ **2601**（+17，**§9.25 给 `fix` 扩触发面（在 §5.8 的两族之内把覆盖做全）+ 同步加更硬的拒绝判据**：① ★先纠正一个前提——§5.8 原文把触发条件**钉死在两族**（「`check_value is None` 的 jmespath 路径类」与「`type_match`/字段名类」），所以"扩面"不是自创新族，而是**把这两族的覆盖补全**；② 补全时发现**两个真缺陷**（都是"改了语义而不是改名字"）：**(a) 容器被吃掉** —— 改前无论 `check` 的根是什么，候选都只在**响应体**里找并一律拼成 `body.*`，于是 `headers.X-Foo` 取不到值会被"修"成 `body.XFoo` ✗（**换语义**）；**(b) `${var}` 被当成路径** —— `${token}` 走的是"路径失效"分支，会被改成 `body.token` ✗✗（把变量引用换成响应字段）；③ 落地：**容器约束**（根 ∈ `body`/`headers`，候选只在同一容器找、`new_check` 保留原根、根不认识一律拒、拿不到该容器的事实也拒 —— **不去别的容器里猜**）+ **新族 `var_missing`**（在**本 step 的 `extract`** 里找唯一相近变量改 `${new}`，两道硬闸：候选唯一 + **新名字的取值路径必须在响应里真能取到值**）+ `propose_fixes` 补传响应头 + 报告的每条结论显示**触发族**；④ **刻意仍然不给**的边界（写进代码注释防"顺手扩"）：**值语义类**（期望值不符、含"字符串数字 ↔ 数字"这种 S 闸门点名的写法）——它们"可能是接口真改了行为"，给建议等于教人把 bug 当特性（R3）；`${fn(...)}` 与业务码语义的 `${code}`（不能靠改变量名绕过业务码闸）同样不改；⑤ 连带修掉第三个真缺陷：`build_patched_text` **保留引号风格**（`yaml.compose` 的 mark **含引号**，直接替换会把 `["${x}", "abc"]` 变成 `[${new}, "abc"]` —— 流式序列里 `{`/`[` 是**语法字符** → 补丁根本解析不过 → 双前置把它拦下 → **"该给的建议一条也给不出来"** ✗；这不仅影响变量族，也影响 `body.items[0].id` 这种带 `[` 的路径）；⑥ ★判据 `tests/fix_test.py::TestTriggerScopeBoundary` **17 条**，全是**成对**（该给的给 / 越界的拒）+ **2 条元护栏**（把 `container_root` 摘掉 → 一定会跨容器；把 `find_candidate_paths` 换成"永远有候选" → 取不到值的变量也会拿到补丁），另有 `fix.run_selftest` 新增 5 条分类判据（容器根 / 简单变量 / `${code}` 按业务码拒 / 取到值却对不上仍拒）；⑦ ★**实测（这回拿到 CLI 级正例了）**：`hrun cases/demo_login.yml --junitxml … --save-tests` 真跑（对同源 mock）产**真 summary** → `haify fix` 给出补丁 `- body.data.filename` / `+ body.data.fileName`（触发族 `path_missing`、理由写明"`body` 里唯一的相近字段"、附风险提示）✓；此前"CLI 级正例没打通"的缺口已闭合；判据 `tests/fix_test.py` **45 passed**（原 28 + 17）；全量 **2469 passed / 2601 collected / 1822 subtest**）→ **2607**（+6，**§9.26 上传路径真正可用：装 `filetype` + mock 能量 multipart 文件大小 + 探针把"事实在不在请求里"钉成分水岭**：① 环境侧 —— 上传路径解锁的**唯一**硬前置是 `filetype`（`UPLOAD_READY` 只由它决定；`requests_toolbelt` **不是**硬前置，那条判据本来就是钉这个的 ✓）；装上之后早先"与代码无关的那条 failed"**消失** ✓（本机走 **pip 本地缓存**即可，**不需要外网**），`deploy.md` 的口径同步改写；② mock 侧新增 `multipart_file_sizes()`（标准库 `email` 解析原始体 → 每个部件的 `(字段名, 文件名, 字节数)`）并接进 `limit` 族的大小判定 —— 于是文档写「单文件超过 50MB → FILE_1021」**在真发了文件时可判定** ✓，只给文件名时**没有事实 → 不触发** ✓（判不了就不乱报）；③ 判据 `TestUploadPathMultipartSize` **6 条**（成对：2KB 文件命中 / 512B 不命中 / **只给名字不命中**（T3_1 形态的固化 ✓）/ 数值型大小那一路没被带坏 / 非 multipart 体没有事实 / 助手逐部件量数）；④ ★**端到端探针** `probe_p2_prep/probe_upload_path.py`：**真发 multipart 文件** → mock 命中大小规则 → **用例 `1 passed`（负例步骤真绿）** ✓；同形的"只给文件名"用例 → `1 failed` 且**回读里没有任何命中** ✓ —— 这一对把"上传类负例步骤能不能被验证"钉成分水岭：**事实在不在请求里**，与"提示词写得好不好"无关；⑤ 连带：`_read_body` 保留**原始字节与 `Content-Type`**（multipart 的大小只能从原始字节量）。全量 **2476 passed / 2607 collected / 1823 subtest**、**本机 0 个环境遗留 failed**）→ **2612**（+5，**§9.27 修掉一个"静默"真缺陷：确定性通路读不到「头表」** —— 触发形态：把《…-测试1》按示例文档重排后，**接口节里明明写着必填的 `X-Path`（头表 + curl 两处）**，`gen --assertions-only` 的产物 `request` 里却**一个头都没有** ✗：打真实服务必被拒（`FILE_1018`），而且**不报错、不点名** ✗✗。① 根因：读表器要求**首列命中 `_FIELD_COL_RE`**（字段/参数/名称/name/…），而示例文档推荐的头表写法正是**首列写「头」** ✗ —— 于是整张头表被 `continue` 掉；`HEADER_COL_RE` 只在判落点时才用，救不了"根本没读进来" ✗✗。② 落地：`read_field_rows` 里**首列命中 `_HEADER_COL_RE` 的表 → 字段列就是第 0 列**（落点仍由 `classify_table_location` 判 → 必是 `headers`；头表**不产断言** ✓ 故不会多出"断言请求头"）+ 头表**取值列「值」**（列头词表的唯一来源仍是 `doc_quality`，新增 `HEADER_VALUE_COL_RE` 且**只在头表里认**）+ **公共请求头继承附一条可见 note**（不静默）。③ 实测（同一份文档，改前/改后对照）：产物从"零头"变成 `Authorization: ${ENV(AUTHORIZATION)}`（凭据占位、不抄 token）/ `X-Path: /app1/`（取自 curl）/ `Content-Type: application/json`；`hrun` **1 passed** ✓，mock 回读里**才第一次出现 `X-Path`** ✓。④ 零回归：转换文档的可模拟错误条件 **4 条不变**、成对能力证明 **17/17**、确定性通路真跑 **14/14**、goldens 与护栏**零波及**。⑤ 判据 +5（`TestRequestPayloadFromFieldTables`：首列「头」必读 / 取值列「值」认 / **必填头无值来源 → `${ENV(TODO_…)}` 占位 + 点名** / 公共头继承 + 可见说明 / 边界：「值」只在头表里认）。全量 **2482 passed / 2612 collected / 1823 subtest**、**本机 0 个环境遗留 failed**）→ **2617**（+5，**§9.28 凭据占位要"告诉人填什么"**（§9.27 登记项 1）：① 缺口是**真缺口**——产物只写 `${ENV(AUTHORIZATION)}` ✓（脱敏硬线），却**没有任何一句**告诉用户"该填哪个环境变量、这个值从哪来" ✗；一个合法产物 + 一句"运行期会报错"并不够用，人还得回去翻文档。② 落地（**提示必须出自文档，工具不编**）：`_credential_hint()` 拼两部分——该行**「说明」列原文**（最常见的一句话就是答案，如"OAuth2.0 的 Bearer Token"）+ 文档里**讲认证的那一节**（标题命中 认证/鉴权/授权/token/auth/… → 给**节名 + 行号**，跳过跨度是全篇的文档标题节）；两样都取不到 → 如实写「文档**没有**说明这个值怎么取得 → 交人工确认（工具不编）」；同时把**环境变量名**点出来（新增 `_env_var_name()`，与 `${ENV(...)}` 的换算**同一份口径** —— 否则提示会指向一个产物里根本不存在的变量名 ✗）。③ 实测（转换后的文档）：unknowns 里现在写着「`Authorization` → 环境变量 `AUTHORIZATION`（文档「说明」列写着：OAuth2.0 的 Bearer Token：`Bearer {access_token}`；取证方式见文档『认证方式（环境前提，不是待测接口）』节（第 32~45 行））」✓ ——**填哪个变量 / 值长什么样 / 去哪看**三件事一句话齐了，且每件都有出处。④ 判据 +5（`TestRequestPayloadFromFieldTables`：说明列原文 + 变量名 ✓ / 认证节名 + 行号 ✓ / **文档没写就如实说** ✓（成对）/ 非凭据字段**不**带这类提示 ✓（成对）/ ★**元护栏**：改说明列原文，提示必须跟着变 —— 防"写死一句话"）。全量 **2487 passed / 2617 collected / 1823 subtest**、**本机 0 个环境遗留 failed**）→ **2624**（+7，**§9.29 响应 Cookie 成为可查事实**（§9.28 登记项 2 / §9.25 第 7 节）：① 缺口：内核**一直**支持 `cookies.*`（`response.py`：`resp.cookies.get_dict()` → **字符串字典**），缺的是**"文档里怎么声明响应 cookie"这一环** —— 文档写了也没人认，cookie 断言只能手工写或干脆没有 ✗；② ★**一轮自查纠正（值得记）**：第一版想"一条 `jsonschema_match` 断言 cookie 类型是 string"——但 cookie 值在内核里**恒为字符串**（HTTP 协议如此）→ 那条断言**永远为真、零信息量** ✗，正是本仓最反对的"看起来在断言、其实什么都没判"。改成：**只在文档给出可判据事实时才产断言** —— 说明列标「固定值」→ `equal: [cookies.<名>, <值>]`；给了「枚举取值」→ `contained_by`（按**字符串**比，防必红）；**都没有 → 不产断言、逐条点名**（认下事实，不硬凑）✓；③ 落地：`doc_quality.COOKIE_COL_RE`（列头词表唯一来源，与 `HEADER_COL_RE`/`PARAM_COL_RE` 同级）+ `assertions.cookie_declarations()`（**断言与 mock 共用的唯一解析**，判据=首列是 `Cookie`/`Set-Cookie` 且**无「必填」列** —— 带必填列的是**请求**侧，走请求头表 + 凭据脱敏）+ `cookie_mock_values()`（**mock 该下发哪个值**与**断言的期望**同一份判据 —— 否则 mock 下发 `mock-variant` 而断言要 `A/B`，会造出"文档没错、mock 也没错、用例却红"的**假失败** ✗）；④ ★**探针真跑抓到两个真 bug**：**(a)** cookie 断言没带 `line_no` → 被归属闸门当"段外"**整条丢弃**（unknowns 里写着"2 条断言的引用句不属于本段" ✗）——`line_no` 是 §9.14 归属的**唯一口径**；**(b)** 探针自己在 `finally` 里**提前收掉 mock** → `hrun` 连不上、断言全是 `None` ✗（同时暴露：`promote_draft` 的写盘根是**相对路径**，探针没 `chdir` 就把用例写进了**仓库根** ✗，已清理并在探针里固化这条教训）；⑤ 实测（`probe_cookie_fact.py`）：文档三行 cookie（固定值 / 枚举 / 无据）→ 草稿产出 `equal: [cookies.sessionId, sess-1]` + `contained_by: [cookies.variant, ['A','B']]` + **无据那条只点名** ✓；mock 启动即打印「按文档下发 3 个（sessionId=sess-1、variant=A、theme=mock-theme）」✓；`hrun` **`1 passed`**，run.log 里两条 cookie 断言**逐条 pass** ✓。全量 **2494 passed / 2624 collected / 1823 subtest**）→ **2630**（+6，**§9.30 让文档能声明"必带" → 安全的 presence 断言**（§9.29 登记项 1）：① 缺口：§9.29 只能断"值是什么 / 落在哪个集合"，**断不了"这个 cookie 一定会下发"** ✗ ——因为响应侧默认**不写 `required`**（文档没建模"什么时候会 set"，写了就是误报）。② 落地：给"必带"一个**文档写法**（两种，都必须是文档**明写**）：表里有「**必带 / 必下发 / 总是下发**」列且值为「是」，或说明列里写这类**标记词**；命中 → 产一条 `jsonschema_match: [cookies, {type: object, required: [...]}]` ✓。★**为什么这里写 `required` 是合法的**：与响应数据表"不写 required"的差别就在**文档有没有建模** —— 明写了必带，服务端不给就是**文档被违反**（真缺陷）✓。★**词必须换**：不能借「必填」✗（那是**请求侧**判据：带它的表会被当请求表、落点进 `request`，方向就反了）——新增 `COOKIE_ALWAYS_SENT_COL_RE`（列头词表唯一来源在 `doc_quality`，与 §9.29 同一条纪律）。③ 成对与点名：没标必带 → **不产** `required`（不误报 ✓）；有「必带」列却**一行都没写清** → 不产 + **点名**（不替文档假定 ✓）；`required` 列表**跟着文档变**（元护栏 ✓）。④ ★**牙齿对照**（本轮新增的验证形态，最要紧）：同一份用例跑两台 mock —— 按文档下发 cookie 的 → **`1 passed`** ✓；换成**不下发** cookie 的（文档里没有那张表）→ **`1 failed`**，失败的正是 `equal` / `contained_by` / **`required`** 三条 ✗✓ —— **证明这三条判据有牙**，不是"永远为真、零信息量"（这正是 §9.29 自查纠正要防的形态）。⑤ 判据 +6（`TestResponseCookieTables`：必带列 → `required` / 说明标记词同样认 / 必带+固定值 → **两条**断言各查各的 / 不标 → 无 `required`（成对）/ 列存在但空白 → 点名（成对）/ **元护栏**：加一行必带 → 列表跟着变）。全量 **2500 passed / 2630 collected / 1823 subtest**）→ **2637**（+7，**§9.31 把「必带」推广到响应数据表**（§9.30 登记项 1）：① 缺口：§9.30 的「必带」**只在响应 Cookie 表**生效；**响应数据表**（`body`）仍刻意**不写 `required`**（口径 B2：文档没建模"什么时候会返回"）。要断"这个字段服务端一定会给"，同样**只能让文档先建模** —— 本轮把同一套判据推广过去，**风险点是层级**：裸字段名挂在 `data` 下，`required` 必须写进 **`data` 那一层**，写到根上就变成"顶层必须有 `fileName`"（语义完全变了 → 误报 ✗）。② 落地：新增 `_put_nested_required()`（**逐层建**，与 `_put_nested` 同源）→ 把 `required` 挂在文档说的**那一层容器**上（`data.a.b` 标必带 → 挂 `a` 层）；「必带」词表**通用化**（`COOKIE_ALWAYS_SENT_COL_RE` → **`ALWAYS_SENT_COL_RE`** —— 判据本身与容器无关，名字里的 `COOKIE_` 会误导）；★**矛盾输入不硬判**：同一行既标「必带」又写「仅当…时返回」→ **不写** `required` + 点名（替文档拍板会造出必红断言 ✗）；★**必带与类型无关**：类型判不了（如 `number`）也要断"有没有" → 必带那段放在类型检查**之前**。③ 与老行为的关系（**成对**）：没标必带 → **一字不变**（不写 `required`，见 `TestResponseTableSchema::test_schema_never_requires_a_field`）✓；标了 → 必须写 ✓。④ ★**牙齿对照**（延续 §9.30 的验证形态）：同一份用例跑两台 mock —— 响应里有必带字段 → **`1 passed`** ✓；**去掉该字段** → **`1 failed`**，内核报错原文 **`$.data: 期望 required=["fileName"]，实际 {"fileVersion": 3}`** ✗✓（既证明**层级对**、又证明**有牙**）。⑤ ★**探针又抓到一个真缺陷（登记，未修）**：夹具里请求字段也叫 `fileName` 时，草稿会多产一条 `type_match: [body.fileName, str]`（响应里其实在 `body.data.fileName`）→ **必红** ✗ —— 根因是**归属闸门按"名字"对、不按"路径"对**（示例文档 §3 第 6 行把它写成"别这么写"，但**工具不该产出这种取不到值的断言**）；探针把现场**可复现地**留在 ⑤ 一节。⑥ 判据 +7（`TestResponseTableAlwaysSentFields`：必带列 → `required` **在正确的容器层级** / 全路径 → 挂在**它自己的容器**上 / 说明标记词同样认 / 必带**压过类型不明确** / 必带×条件返回 → 不写 + 点名 / 必带列空白 → 点名 / **元护栏**：列表跟着文档变）。全量 **2507 passed / 2637 collected / 1823 subtest**）→ **2644**（+7，**§9.32 归属从"按名字"升级为"按路径"**（§9.31 登记项 1）：① 实测缺口（§9.31 探针 ⑤ 抓到）：请求表字段名与响应字段**末段撞名**时（都叫 `fileName`），文档里其实只有 `data.fileName`，而 `body.fileName` 因为"名字对得上"被**放行** → 照文档实现的服务必让这条断言取不到值（`check_value: None`）→ **必红**，且失败看起来像接口坏了（分叉型静默）；② 落地（**只在闸门层**，推导层的名字早筛保持原口径 —— 那是"够便宜的粗筛"，改它会误伤 4 处既有判据）：`assembler.check_response_ownership` 改按**路径**判（`want = tuple(parts[1:])` 必须 ∈ 文档的响应侧**路径集合**）+ **把"层级取错"与"根本没有"分开报**（前者直接给出正确写法 `body.data.fileName` 并点名"请求字段与响应字段末段撞名是最常见成因"）；为支撑它，`assertions.section_tree` 改存**路径**，新增 `response_field_paths()`（名字降级为它的**投影** —— 不再各算一份）与 `_response_table_path()`（"裸名挂 `data` 下"这条换算的**唯一一处**）；③ ★**一个"不补就白改"的坑**：闸门在**引用句**分支只取**最内层**那一节，而实测里引用句正是**「请求体」小节里的一行**（该节路径集为空）→ 于是**任何**断言都会被判"判不了"而放行 ✗，§9.32 的路径判据在确定性通路上**根本不生效** ✗ —— 补上"由内向外逐层找第一个有响应证据的节"（与 `line_no` 分支**同一条宽容线**）后才真正生效；④ 连带修**两份 golden**（它们自己就违反了示例文档 §3 第 6 行的"别撞名"）：`order_create.md` 请求侧 `orderId` → `clientOrderId` ✓ + 响应示例把 `status` 提到**顶层**（于是请求表里的枚举断言 `body.status` 层级正确、判据与意图都不用改 ✓）；`update_order_status.md` 同样把 `status` 提到顶层 ✓（两份 `.yml` 同步）；⑤ 实测（`probe_required_fact.py` ⑤ 从"已知边界"翻成**回归**）：草稿里取错层级的断言 **（没有 ✓）**、unknowns 点名「`body.fileName` 的**层级取错了**：…但在 `data.fileName`（不在 `fileName`）」✓、用例 `hrun` 退出码 **0**（摘掉那条后**真跑绿** ✓；改之前是 1 ✗）；零回归：成对能力证明 **17/17**、确定性通路 **14/14**、golden 相关判据（`TestEveryGoldenDocumentDerivesLegalAssertions` / T10 误伤率审计 / L2）全绿 ✓；⑥ 判据 +7（`TestOwnershipIsByPath`：顶层断言打在 `data.X` → **摘掉并点名** / 写对层级 → 放行 / ★**同名不同层级 → 结论不同**（元护栏）/ 完全没有该字段 → 仍用老消息（成对）/ 说明句路径也按层级比（成对）/ 定位不到 → **仍宽容** ✓ / ★元护栏：把"裸名挂 `data` 下"掐掉 → 撞名那条**必被放行**（证明正是这条规则在拦））。全量 **2514 passed / 2644 collected / 1823 subtest**）
> 另两条红灯 `cli_test.py::TestCli::test_run_testcase_with_abnormal_path` 与
> `step_request_test.py::TestCaseRequestWithFunctions::test_start` 属 README「六」里
> 点名的**外网用例**（`postman-echo.com`），本机断网即失败，**不是回归**。

> NOTICE（装了 `test` extra 之后）：上面这一行是**装了 `pip install -e ".[test]"`**
> （pytest-randomly + pytest-xdist）测得的口径。装与不装有两个可见差异，都**不是**回归：
>   - **skip 数 10 → 9**：`tests/cli_test.py::TestXdistParallelRun` 是
>     `skipUnless(xdist 已安装)`，装了它这条并行冒烟就**自动生效**（源码里写明的设计）；
>   - 该用例在**受限沙箱**里会失败（xdist 需要 `os.scandir` 自己的 basetemp、
>     pytest-randomly 需要写 `.pytest_cache`，两者都可能被拒）。
>     这不是代码缺陷：把 `-p no:randomly` 加上、或在正常环境/CI 里跑即通过。
> 未装 `test` extra 时基线回到 **1669 passed / 10 skipped**。
> 其它 skip 随可选依赖（thrift / thriftpy2 / lxml / trustme / wheel 相关）变化。
> `docs/` 下历史修复记录被删除后，`git show` 指令类引用会消失 —— 那条护栏会**自动 skip**
> 而不是判失败（见 `docs_consistency_test.TestGitShowReferences` 的 NOTICE）。

> **NOTICE（批次 9-1 / 基线口径收口）：本行是全仓**唯一**的「当前基线」事实来源。**
> 其它文档（`deploy.md`、`docs/ci/README.md`）**不要再抄一遍数字**——它们只指向本节，
> 抄一份就多一个会过期的副本（修复前 `491 passed / 4 skipped` 这句在 3 处各写了一份，
> 而真实值是 1328+，谁都没发现）。
> 回归护栏：`tests/docs_consistency_test.py::TestBaselineNumbersHaveOneSource`
> （全仓只允许一条「当前基线」行；任何文档重新抄一个**不同**的数字都会变红；
> 这条护栏还会用 `--collect-only` 与**实际收集到的用例数**对账 ——
> 加了用例却没更新这一行会立刻变红）。
>
> 历史链（**这些是各批次的快照，不是当前值**）：360 → 387 → 443 → 491（A2-2）→ …
> → 1323（0919-31）→ 1340（批次 9-1）→ 1349 → 1373（批次 A：加载层/参数化/汇总层/导入器
> 四组修复，共 +24 个用例）→ 1395（批次 B：用例名归一/step 类型互斥/请求体互斥/
> oauth2 子字段/`--save-tests`路径与落点/CSV 编码，共 +22 个用例）→ 1418（批次 C：
> TLS 默认可见化/字典序比较告警/`$中文` 告警/XML 选择器两处修正/上传头转义与流式，共 +23 个用例）
> → 1420（批次 D 第一步：SQL 连接串 userinfo 的编码口径修正 —— 口令里的空格不再变成 `+`，
> 并改掉那条把错误编码钉住的断言，共 +2 个用例 / +9 个 subtest）
> → **1439**（批次 D 第二步：thrift **请求方向**的两处静默 —— `binary` 字段（ttype=18）不再让整个
> step 报 `Unrecognized thrift field type: 18`、参数里字段名写错不再被静默丢弃；
> 另加**算子数一致性护栏**（D1 遗留），共 +19 个用例 / +8 个 subtest）
> → **1450**（批次 D 第三步：thrift 标量强转的可见化 —— `"false"` 不再悄悄变成 `True`、
> `3.9` 不再悄悄截断成 `3`，口径是**只告警、行为不变**；转不出来的值改为点名报错，共 +11 个用例 / +31 个 subtest）
> → **1476**（批次 E：**报错质量族 L1~L10** —— 四个导入器的形态护栏收口到 `ir.safe_dict_entries`
> （坏字段只跳过它自己那一层，不再让整批崩）、OpenAPI 三个「漏了 `-`」、`allOf` 自引用递归、
> curl `-d`+`-F` 冲突、`hconvert` 写盘异常与跨次覆盖、超大整数的告警路径、compat 的两处前置崩溃，
> 共 +26 个用例 / +13 个 subtest）
> → …（0919-31 / 0920 各批次：1328 → 1340 → 1349 → 1373 → 1395 → 1418 → 1420 → 1439 →
> 1450 → 1476 → 1495 → 1524 → 1541 → 1565 → 1588 → 1617 → 1628 → 1630，明细见
> `docs/缺陷修复日志0919-31.md` 与 `docs/缺陷修复日志0920*.md`）
> → **1644**（0921 / 缺陷收口三件：**`sleep()` 补上 L2 漏掉的加固**（收 `.env`/CSV/`${ENV()}`
> 来的数字字符串；**刻意不用 `ensure_int_value`**，否则会拒绝本仓在用的 `${sleep(0.05)}`）、
> **`get_timestamp()` 改定宽源串**（旧实现 `str(time.time())` 位数浮动，`get_timestamp(16)`
> 有约 1~2% 概率静默返回更短的串）、**`verify` 链式 setter 补校验**（`TRequest`/`TConfig` 无
> `validate_assignment`，字符串 `'false'` 会被 requests 当成 CA 证书路径、且吞掉 TLS 告警），
> 共 +18 个用例 / +41 个 subtest）
> → **1653**（0921-2 / 三处残留收口：**`make.py` 的覆盖保护判据从"子串"收紧成"首行"**
> （手写文件只要开头提到标记就会被放行覆盖，而 `compat.is_generated_conftest` 早已论证
> 子串匹配是错的）、**`Config.__init__` 的 `inspect.stack()` 补上回退 cwd 兜底**
> （与 `parser.parse_parameters` 对齐，原先拿不到调用帧会裸抛 `IndexError`）、
> **`client.py` 注释里残留的 `charactors` 拼写**，共 +9 个用例 / +3 个 subtest）
> → **1663**（0921-3 / 断言算子三处「同族遗漏」收口：**`length_*` 接上 L2 的数字转换**
> （`${ENV(LEN)}` 永远是字符串，原先只给英文硬 assert；刻意**不**用 `ensure_int_value`，
> 因为 `length_equal` 只收整数、其余四个本就接受 float）、**`type_match` 的类型名错误
> 从裸 `ValueError` 改成 `ParamsError`**（原先冒泡成 pytest 的 **error**，而"用户写错用法"
> 按本仓划分应记 **failed**；`validate` 只转换这一窄类型，算子内部的真 `ValueError`
> 仍原样抛出）、**`regex_match` 接上 `_ensure_string_shaped`** 并顺带把无效正则的
> `re.error` 转成可读报错，共 +10 个用例 / +32 个 subtest）。
> skip 数（现在 10 个）随**装了哪些可选依赖**变化：`pytest-xdist` / Windows 上的 thrift /
> `thriftpy2` / `lxml` / `trustme`——它们全是**按设计 skip**，不是失败，
> 逐条原因见 `docs/ci/README.md` 的「验证清单」。

---

## 七、许可与来源

- 本仓库是 **[HttpRunner](https://github.com/httprunner/httprunner) v4.3.5 的二次开发分支**，
  保留了上游 `LICENSE`（**Apache License 2.0**，见 [`LICENSE`](LICENSE)），
  衍生工作同样以 **Apache-2.0** 发布；`pyproject.toml` 已用 SPDX 声明（`license = "Apache-2.0"` + `license-files`）。
- **对外分发/宣传时请注明「基于 HttpRunner (Apache 2.0) 二次开发」，不得声称完全自研，也不要使用 HttpRunner 商标做宣传。**
- 框架内的 `interfacetester` 包名与命令（`hrun`/`hmake`/`hconvert`）为本分支的命令入口；
  上游能力与本分支新增能力的对应关系见 `docs/接口自动化框架升级优化评估.md`（已从工作区删除，
  用 `git show docs-before-batch-cleanup:docs/接口自动化框架升级优化评估.md` 取回）。
