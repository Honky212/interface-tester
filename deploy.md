# InterfaceTester 部署文档

> InterfaceTester 是一个 HTTP(S)/HTTPS 接口测试一站式工具，
> 包名 `interfacetester`、类名 `InterfaceTester`、遥测已禁用。
> 本文档说明如何在全新机器上安装部署本项目。

---

## 1. 环境要求

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| Python | `>= 3.8`（实测 `3.11.0`） | 推荐 3.9 ~ 3.12 |
| 操作系统 | Windows / macOS / Linux | 三者均支持 |
| 构建工具 | `setuptools >= 77.0` + `wheel` | 构建后端为 setuptools（PEP 621 / PEP 639） |
| 网络 | 可访问 PyPI（或清华镜像源） | 用于下载依赖 |

> 提示：本项目使用 **setuptools** 构建（原 poetry 已移除），不需要安装 poetry。
> 项目元数据、依赖、命令入口全部声明在根目录 `pyproject.toml`，它是**唯一事实来源**；
> 根目录 `requirements.txt` 只是给离线/复现部署用的锁版本清单。

---

## 2. 目录结构

```
interface-tester/
├── interfacetester/          # 主包源码（唯一修改入口）
│   ├── cli.py                # 命令行入口（run / make / convert）
│   ├── make.py               # hmake（YAML → pytest 用例）
│   ├── runner.py             # InterfaceTester 运行器
│   ├── parser.py             # YAML/JSON 解析
│   ├── utils.py              # 工具函数（mock 端口/超时等环境变量入口）
│   ├── builtin/              # 内置函数 + 22 个断言算子（comparators.py）
│   ├── converters/           # 导入器：curl / HAR / Postman / OpenAPI → YAML（hconvert）
│   ├── database/             # SQL 请求支持（namespace 包）
│   ├── thrift/               # thrift 请求支持（namespace 包）
│   └── ext/                  # 上传等扩展工具
├── tests/                    # 单元测试 + 本地 mock httpbin（80/443，可用环境变量改）+ 导入器黄金文件
├── pyproject.toml            # 构建后端 + 元数据 + 依赖 + 命令入口（唯一事实来源）
├── requirements.txt          # 锁版本依赖清单（离线/复现部署用）
├── LICENSE                   # Apache-2.0
├── README.md                 # 入口说明（能力总览 / 快速开始 / 文档地图）
├── 使用教程.md / 小白入门指南….txt   # 教程（系统学习 / 照着抄）
├── deploy.md                 # 本文档
├── docs/                     # 文档：能力清单、评估文档、P0~P3 开发记录、convert/、ci/
├── .github/workflows/test.yml / .gitlab-ci.yml   # CI 模板
├── examples/                 # 示例用例（httpbin / postman_echo / unifsp / data / data_management / openapi）
├── dist/                     # 构建产物（python -m build 输出）
└── .venv/                    # 虚拟环境（本机已验证环境：Python 3.12）
```

---

## 3. 快速部署

### 3.1 获取代码

```bash
# 方式一：直接使用现有目录（如已拷贝/解压到本地）
cd /path/to/interfacetester

# 方式二：从 Git 仓库克隆（如有）
git clone <仓库地址> interfacetester
cd interfacetester
```

### 3.2 创建虚拟环境

> 强烈建议使用虚拟环境，避免污染系统 Python。

**Windows（PowerShell）：**

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**Linux / macOS：**

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3.3 升级构建工具并安装依赖（重要）

> 说明：依赖声明在 `pyproject.toml` 的 `[project.dependencies]`，
> 因此 `pip install -e .` / `pip install .` / 安装 wheel 时会**自动安装运行时依赖**。
> 下方手动安装命令作为网络受限或需要指定镜像源时的备选方案；
> 也可直接用根目录 `requirements.txt`（锁版本）离线安装。

先升级 pip / setuptools / wheel：

```bash
python -m pip install -U pip setuptools wheel
```

安装运行时必需依赖（建议用清华镜像加速）：

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    "pydantic>=2.0,<3" \
    "loguru>=0.4.1" \
    "jmespath>=0.9.5" \
    "Jinja2>=3.0.3" \
    "PyYAML>=6.0.1" \
    "requests>=2.31" \
    "urllib3>=1.26,<3" \
    "toml>=0.10.2" \
    "Brotli>=1.0.9" \
    "black>=22.3" \
    "pytest>=7.1" \
    "pytest-html>=3.1"
```

> 说明：
> - `urllib3` 约束为 `>=1.26,<3`（兼容 1.x 与 2.x，实测 2.5.0）。
> - `black` 用于 `hmake` 生成用例后格式化代码；缺失时仅告警并跳过格式化，不阻断运行。
>   调用时优先使用**当前解释器**里的 black（`python -m black`），当前解释器没有才回落到
>   PATH，因此不再要求 black 必须在 PATH 中；单次调用默认 60s 超时（可用环境变量
>   `INTERFACETESTER_BLACK_TIMEOUT` 调整），超时同样只告警不阻断。
>   子进程的缓存目录默认被指到临时目录（除非你已显式设置 `BLACK_CACHE_DIR`），
>   避免受限环境下（只读 HOME / 锁定机器 / CI 只读缓存盘）black 卡在写缓存上。
> - `pytest` / `pytest-html` 是 `hrun` 运行用例、生成 HTML 报告所必需。
> - 遥测依赖 `sentry-sdk` 已无需安装（代码中已移除相关 import，遥测为空操作）。

### 3.4 安装项目

```bash
# 可编辑安装（开发模式，推荐：改动源码立即生效）
python -m pip install -e . --no-build-isolation

# 或普通安装（生成副本到 site-packages）
python -m pip install . --no-build-isolation
```

★**AI 协作层（可选，得到 `haify` 命令）**：`haify` 由**独立发行版** `interfacetester-ai` 提供 ——
它有自己的 `pyproject.toml`（`[project.scripts]` **只在那儿**），所以上面那句 **只装内核**，
装完 `hrun` / `hmake` / `hconvert` 有、**`haify` 没有**（敲了会报"无法识别为 cmdlet / 命令"）。

```bash
# 装 AI 层（零三方依赖；`--no-build-isolation` 需要本机已有 setuptools/wheel）
python -m pip install -e interfacetester_ai --no-build-isolation
haify -V
# 期望：打印 **AI 层版本 + 内核版本**（两个都报）——`-V` / `--version` 都认，与 `hrun --version` 同一习惯；
#       版本号**不写死在这里**：内核版本 = interfacetester/__init__.py 的 __version__，
#       AI 层版本 = interfacetester_ai/__init__.py 的 __version__（两者必须配套）。

# 不装也能用（等价入口，同一份 `cli.main`）：python -m interfacetester_ai gen <文档.md>
# 回滚 AI 层（内核 0 改动，删包即回滚）：python -m pip uninstall interfacetester-ai
```

> 离线提示：本机实测 `setuptools` / `wheel` 可**从 pip 本地缓存**装上
> （`pip install --no-input setuptools wheel`），装完即可用 `--no-build-isolation` 离线打轮/安装。

### 3.5 开发与重新打包（改源码 → 生效）

> **原则：只改工作区 `interfacetester/` 下的源码。**
> 不要再直接修改 `.venv/Lib/site-packages/interfacetester/`——那是构建产物，
> 下次安装/重新打包就会被覆盖，改动会丢失。

```bash
# 1) 改源码，例如 interfacetester/make.py
# 2) 让改动生效：editable 安装只需装一次，之后改源码立即生效；
#    若此前是非 editable 安装，执行一次：
python -m pip install -e . --no-build-isolation

# 3) 确认命令确实指向工作区源码
python -c "import interfacetester; print(interfacetester.__file__)"
# 期望输出：...\interface-tester\interfacetester\__init__.py

# 4) 跑单元测试（tests/ 内置本地 mock httpbin：80 + 443，见 tests/mock_server.py，
#    不需要 docker httpbin，也不依赖外网）
python -m pytest tests -q

# 5) 需要交付/分发时重新打包（**两个** wheel：内核 + AI 层）
python -m build --wheel
# 产物：dist/interfacetester-<版本>-py3-none-any.whl
#   <版本> 取自 interfacetester/__init__.py 的 __version__（唯一事实来源），
#   想知道具体文件名就直接看目录：Get-ChildItem dist/*.whl

# AI 层（独立发行版，装它才有 `haify`；它有**自己的** pyproject.toml）
cd interfacetester_ai
python -m build --wheel
# 产物：interfacetester_ai/dist/interfacetester_ai-0.1.0-py3-none-any.whl
cd ..

# ★**没有 `build` 前端也能打**（用 setuptools 自己的构建后端，离线可用；本机实测）：
#   python -c "import setuptools.build_meta as b; print(b.build_wheel('dist'))"
#   （AI 层同理：在 interfacetester_ai/ 目录里执行；前提：本机已有 setuptools/wheel）
```

> 注意：
> - 第 4 步请先**激活虚拟环境**（用例会用 venv 里的 `black`；`hmake` 优先调用当前解释器的
>   `black`，当前解释器没有才回落到 PATH）。`hmake` 会调用 `black` 格式化生成的 pytest
>   文件；black 缺失或超时（默认 60s）时只告警并跳过格式化，但断言格式化输出的用例会失败。
> - 打包产物统一放 `dist/`；仓库根目录不再手工放置 whl。
> - ★**打轮别污染包目录**：源码目录里**不许**留下 `build/` 与 `*.egg-info/`
>   （`tests/packaging_test.py` 盯着这条；本机实测踩过一次 —— 原地打轮会留下这两样）。
> - ★**验产物内容**（可选，2×5 条判据，本机实测 **41 passed**）：
>   `$env:INTERFACETESTER_WHEEL_PATH=<内核 wheel>`、`$env:INTERFACETESTER_AI_WHEEL_PATH=<AI wheel>`，
>   然后跑 `python -m pytest tests/wheel_content_test.py tests/packaging_test.py -q`；
>   不设这两个变量时它们按设计 **skip**（普通单测不依赖"本机恰好打过轮"）。
> - 历史产物已归档在 `dist/prev/`（重构前手工安装用的那份）。
> - **不要把版本号写死**在文档里（例如把 `4.3.5` 直接嵌进产物名或
>   `--version` 的期望输出）：版本只在 `interfacetester/__init__.py` 维护，
>   写死后升级版本就会漂移。`tests/docs_consistency_test.py` 会把这条做成不变量。

---

## 4. 验证安装

```bash
# 1) 查看版本（期望输出 = interfacetester/__init__.py 里的 __version__）
hrun --version
interfacetester --version

# 2) 验证导入
python -c "import interfacetester; print(interfacetester.__version__)"

# 3) 端到端：用示例生成 pytest 用例（hmake 需要 black 已在 PATH）
hmake examples/httpbin_demo.yml
# 期望生成 examples/httpbin_demo_test.py

# 4) 跑通一个用例（需可访问被测服务）
hrun examples/httpbin_demo_test.py

# 5) 不依赖任何外部服务：数据管理示例自带本地 mock（期望 4 passed, 1 skipped）
hrun examples/data_management -q

# 5b) 同样离线：SOAP/XML 结构化断言示例（期望 3 passed, 1 skipped）
hrun examples/soap -q

# 6) 导入器自检：契约 → 骨架用例（会写 converted/，可删）
hconvert --from openapi --in examples/data/openapi/petstore_demo.yaml --out converted/
# 期望：2 个用例文件 / 6 个步骤 / 11 条断言，且打印「导出即验证通过」

# 7) 契约断言自检（算子 jsonschema_match，确认 jsonschema 依赖就位）
python -c "from interfacetester.builtin.comparators import jsonschema_match; jsonschema_match({'a': 1}, {'type': 'object', 'required': ['a']}); print('jsonschema_match OK')"

# 7b) XML/XPath 断言自检（A2-1 的内核算子，确认 xml extra 就位；期望打印 xpath_match OK）
#     没装 extra 时这条会报 RuntimeError 并提示 pip install -e ".[xml]"（不是 ModuleNotFoundError 堆栈）
python -c "from interfacetester.builtin.comparators import xpath_match; xpath_match('<r><code>0000</code></r>', ['code', '0000']); print('xpath_match OK')"

# 7c) XSD 契约校验自检（A2-2）：直接跑示例里的 XSD 用例（自带本地 mock，不依赖外网）
#     XSD 只接受**文件路径**（xs:include 要按文件所在目录解析），所以用示例里的契约做自检最省事
#     期望：1 passed（这一步内部含 3 条 xml_schema_match：先定位再校验 / 前缀漂移 / xs:include 多文件）
python -m interfacetester run examples/soap_xpath/xsd.yml

# 7d) 两个 XML 示例冒烟（各自带本地 mock；期望 3 passed/1 skipped 与 2 passed/2 skipped）
python -m interfacetester run examples/soap
python -m interfacetester run examples/soap_xpath

# 8) 全量单测（CI 确定性口径；排除 6 个依赖公网的用例）
python -m pytest tests -q \
  --deselect tests/step_request_test.py::TestRunRequest::test_run_request \
  --deselect tests/step_request_test.py::TestCaseRequestWithFunctions::test_start \
  --deselect tests/step_testcase_test.py::TestCaseRequestWithFunctions::test_start \
  --deselect tests/step_testcase_test.py::TestRunTestCase::test_run_testcase_by_path \
  --deselect tests/cli_test.py::TestCli::test_debug_pytest \
  --deselect tests/cli_test.py::TestCli::test_run_testcase_with_abnormal_path
# 期望：2545 passed, 121 skipped, 6 deselected   （本机：无外网；已装 filetype；★已 `git init`，
#   所以 `git ls-files` 与 black 版本推断那两条护栏**会真跑**（此前因无 .git 而 skip））
# ★本机当前**没有"环境遗留 failed"**：上传依赖的核心 `filetype` 已装（走 pip 本地缓存，
#   **不需要外网**）→ 早先那条 `…test_requests_toolbelt_is_not_a_hard_prerequisite` 已转绿；
#   文档死链检查（`docs_consistency_test::TestDocumentationLinks`）当前也是绿的。
#   ★仍未装的可选依赖：`lxml`（xml）/ `trustme` / `thrift*` / `pytest-xdist`（test）——
#   相关用例按设计 **skip**（124 条），不是失败。★`test_requests_toolbelt_is_not_a_hard_prerequisite`
#   这条判据的意义与"装没装包"无关：它钉的是「**toolbelt 不是硬前置**」（上传编码器自研，
#   缺它也不该让上传功能整块不可用）——缺的**只有** `filetype` 时会红，装它就绿。
#   NOTICE（2026-09-22 实测）：本行改为**当前这台机器**的口径 —— 可选依赖全缺
#   （lxml / trustme / thrift / thriftpy2 / pytest-xdist），相关用例按设计 skip。
#   总数已含《智能化改造方案2》的 25 条护栏与《智能化改造方案 v6~v8》新增护栏
#   （S-2b 4 + 归一化投影 9 + 元护栏 5 + v8 三批收尾 T18/T6/S-6 的 36 条 + 第六批 T29 的 18 条
#    + 第七批 T27 的 21 条 + 第八批 S-7/S-8 的 19 条 + 第九批 T7/T8 的 26 条 + 第十批 T9/T10 的 5 条 + 第十一批 T4/T5/T15 的 22 条 + P0a 阶段 A/B 的 34 条 + P0a 阶段 C 的 28 条 + P0a 阶段 D 的 12 条 + P0a 阶段 E 的 17 条 + P0b 的 17 条 + T24 的 18 条 + P1a 的 27 条 + P1b 的 25 条 + P2a 的 28 条 + P2b 的 30 条 + P3a 的 56 条 + P3b-1 的 31 条 + P3b-2 的 27 条 + P3c 的 36 条 + P3d 的 13 条 + 配置指引纠正的 1 条 + 失败指引纠正的 2 条 + 输出契约纠正的 2 条 + 端点溯源纠正的 6 条 + 非接口片处置的 5 条 + 覆盖口径纠正的 4 条 + P3c 离线 wheel 的 5 条 + 度量来源的 7 条 + 离线导出的 12 条 + L3 沙盒的 11 条），全量 1712 → 1744 → 1780 → 1798 → 1819 → 1838 → 1864 → 1869 → 1891 → 1925 → 1953 → 1965 → 1982 → 1999 → 2017 → 2044 → 2069 → 2097 → 2127 → 2183 → 2214 → 2241 → 2277 → 2290 → 2291 → 2293 → 2295 → 2302 → 2307 → 2311 → 2316 → 2323 → 2335 → 2346 → 2548 → 2573 → 2584 → 2601 → 2607 → 2612 → 2617 → 2624 → 2630 → 2637 → 2644 已与 --collect-only 重新对账（2026-09-27）。
#   ★另：若跑的是**全量**（第 8 条命令去掉那 6 个 `--deselect`），还会多出 6 个打公网
#     `postman-echo.com` 的用例（断网即失败，见 docs/ci/README.md 坑 5）——
#     那正是这条命令要排除它们的原因。
#   NOTICE：这是装了 `test` extra（pytest-randomly + pytest-xdist）之后的口径；
#   `TestXdistParallelRun` 是 skipUnless(xdist)，装了它就从 skip 变成真跑。
#   未装该 extra 时为 1669 passed / 10 skipped。
#   NOTICE（批次 9-1）：这里**只**保留一份便于交付验收的期望值，
#   唯一的「当前基线」事实来源是 README.md「六、测试与质量」——
#   两处数字不一致时以 README 为准，并请同步本行。
#   历史（装齐 extra 时的口径）：1670/9；491（A2-2 后）→ 443 → 387 → 360；
#   skip 数随可选依赖（xdist/thrift/thriftpy2/lxml/trustme）变化。
#   0920 批：1476/10 → 1495/11（批1）→ 1524/11（批2）→ 1541/11（批3）→ 1565/12（批4）
#              → 1588/8（批5）→ 1617/8（批6）→ 1628/8（批7）→ 1630/8（收尾：Allure 核对）
#              → 1644/12（0921：sleep 加固 / get_timestamp 定宽 / verify setter 校验，+18 个用例）
#              → 1653/12（0921-2：覆盖判据收紧 / Config 调用帧兜底 / 拼写残留，+9 个用例）
#              → 1663/12（0921-3：length_* 数字转换 / type_match 归为 failed / regex_match 守卫，+10 个用例）。
#   skip 数随可选依赖变化（本机补装 allure-pytest / thrift 后，相应用例不再 skip）。
```

---

## 5. 命令行工具

安装后会在虚拟环境的 `Scripts`（Windows）/ `bin`（Linux/macOS）下生成 4 个命令：

| 命令 | 作用 | 对应入口 |
| --- | --- | --- |
| `interfacetester` | 主命令（`run` / `make` / `convert` 三个子命令） | `interfacetester.cli:main` |
| `hrun` | 转换用例并直接运行（最常用） | `interfacetester.cli:main_hrun_alias` |
| `hmake` | 仅转换用例为 pytest（不运行） | `interfacetester.cli:main_make_alias` |
| `hconvert` | 导入器：curl / HAR / Postman / OpenAPI → 标准 YAML 用例 | `interfacetester.cli:main_convert_alias` |

常用示例：

```bash
hrun test.yml                       # 转换 + 运行单个用例
hrun testcases/                     # 转换 + 运行整个目录
hmake test.yml                      # 只生成 test_test.py，不运行
hrun test.yml --html=report.html    # 生成 HTML 报告
hrun test.yml --junitxml=report.xml # 生成 JUnit 报告（CI 聚合用）

# 导入器（P3）：把已有资产转成用例，默认带「导出即验证」
hconvert --from openapi --in examples/data/openapi/petstore_demo.yaml --out converted/
hconvert --from curl --in examples/data/curl/curl_examples.txt --out converted/ --report converted/import-report.md
hconvert --help                     # 全部参数（--assertions / --single-file / --include-tags …）
```

> `hconvert` 生成的是**标准 YAML**（走现有 `hmake` 链路），并会在输出目录自动写一个 `debugtalk.py`
> 作为「项目标记」（框架靠它确定项目根目录，schema 文件引用相对它解析）。
> 支持的写法与降级规则见 `docs/convert/README.md`。

---

### 5.1 `haify`（AI 协作层）的配置写在**环境变量**里

> **完整上手步骤**（装 / 配 / 跑 / **怎么判断跑对了** / **已知的 2 个环境遗留失败** / 三条硬边界）见
> `docs/使用手册-AI协作层.md`；本节只讲**配置**这一件事。

`haify` 的模型调用走 **OpenAI 兼容** `POST {base_url}/chat/completions`（`requests` 直连，**不引 LLM SDK**）。配置**只从环境变量读**。

```powershell
# PowerShell（当前会话有效；关掉窗口即失效）
$env:INTERFACETESTER_AI_BASE_URL = "http://127.0.0.1:11434/v1"   # 本地 Ollama 默认端口
$env:INTERFACETESTER_AI_MODEL    = "qwen2.5:14b"
$env:INTERFACETESTER_AI_SANDBOX  = "on"                          # ★L3 闸门：不发它一律拒
```

| 变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `INTERFACETESTER_AI_BASE_URL` | **是** | — | OpenAI 兼容根地址（自动补 `/chat/completions`） |
| `INTERFACETESTER_AI_MODEL` | **是** | — | 模型名 |
| `INTERFACETESTER_AI_API_KEY` | 云端必填 | 空 | **回环地址可留空**（Ollama 类本来就没有 key） |
| `INTERFACETESTER_AI_SANDBOX` | **真出网必填** | 关 | L3 闸门开关（`1/true/yes/on`）；**回环也要求开** |
| `INTERFACETESTER_AI_ALLOW_HOST` | 非回环必填 | 空 | 非回环主机的**显式白名单**（逗号/空白分隔），等价 `--allow-host` |
| `INTERFACETESTER_AI_TIMEOUT` | 否 | **`180`** | 秒。**★换模型时必须一起调**：本机实测 `qwen3.8:27b` 单次调用 **>600s**、`gemma4:12b` **>240s**（"思考型"模型还会把 completion 全用在推理上、正文为空）。**默认值已于 2026-09-25 由 60 → 180**（本地大模型友好；云端快模型可显式调回 `60`）——在那之前，默认 60s 会让本地大模型**全部以"传输超时"失败**，而症状看起来像"模型不行"。本地大模型建议 `300` 起、慢的给 `1800` |
| `INTERFACETESTER_AI_RETRIES` | 否 | `2` | 5xx/网络异常重试次数（**4xx 不重试**） |
| `INTERFACETESTER_AI_MAX_OUTPUT_CASES` | 否 | `20` | 单次最多产出用例数 |
| `INTERFACETESTER_AI_ENABLE` | 否 | `on` | 仅对**显式调用** `haify` 生效 |

**★不读项目 `.env`**（2026-09-25 实测确认）：项目 `.env` 是**用例**里 `${ENV(...)}` 的来源，属于**被测项目**的配置。`haify` 在 `LLMConfig.from_env()` 阶段还**没走**内核 loader，所以那里的 `INTERFACETESTER_AI_*` **不会**进到配置里。别写进 `.env` 等它生效 —— **直接用环境变量**（也让凭据不落文件、不被用例读到）。

**验证配置（一条命令，不发请求）**：

```powershell
.venv\Scripts\python.exe -c "from interfacetester_ai.llm import LLMConfig; c=LLMConfig.from_env(); print('model =', c.model); print('base_url =', c.base_url); print('is_local =', c.is_local())"
```

配好后的三种典型用法：

```powershell
# ① 不调模型：先看"光靠文档能确定多少断言"（不需要 BASE_URL/API_KEY）
.venv\Scripts\python.exe -m interfacetester_ai gen <doc.md> --assertions-only

# ② 真模型：文档 → 用例草稿（需要 BASE_URL + MODEL + SANDBOX）
.venv\Scripts\python.exe -m interfacetester_ai gen <doc.md>

# ③ CI 不出网：只回放 .ai/cache/（先真跑一次填充缓存，再让 CI 用 --fake）
.venv\Scripts\python.exe -m interfacetester_ai gen <doc.md> --fake
```

`haify` 退出码：`0` 全部产出 / `1` 有切片失败 / `2` **配置缺失**（会打印配置指引）/ `3` 网络失败 / `4` 文档体检未过 / `5` 预算超限。
**没配就一定不会出网** —— 这是刻意的默认值。

## 6. 可选扩展（extras）

可选能力声明在 `pyproject.toml` 的 `[project.optional-dependencies]`，按需安装后即可启用。
推荐用 extras 语法一次性安装（依赖与版本约束由 `pyproject.toml` 统一维护）：

| 能力 | extra 名 | 安装命令（推荐） | 实际安装的包 |
| --- | --- | --- | --- |
| SQL 请求 | `sql` | `python -m pip install -e ".[sql]"` | `sqlalchemy`、`pymysql` |
| 文件上传 | `upload` | `python -m pip install -e ".[upload]"` | `requests-toolbelt`、`filetype` |
| allure 报告 | `allure` | `python -m pip install -e ".[allure]"` | `allure-pytest` |
| thrift 请求 | `thrift` | `python -m pip install -e ".[thrift]"` | `cython`、`thrift`、`thriftpy2` |
| XML/XPath/XSD 断言（A2-1 + A2-2） | `xml` | `python -m pip install -e ".[xml]"` | `lxml` |

同时安装多个（本例为「SQL 请求 + 文件上传」，是接口自动化最常用的两个扩展）：

```bash
python -m pip install -e ".[sql,upload]"
```

> 说明：
> - 也可逐个安装（如 `python -m pip install sqlalchemy pymysql` / `python -m pip install requests-toolbelt filetype`），
>   但 extras 语法更省事，且能随 `pyproject.toml` 的版本约束保持一致。
> - **thrift 请求仅支持 Linux/macOS**：Windows 上 `RunThriftRequest` 在构造时就会抛 `RuntimeError`
>   （见 `step_thrift_request.ensure_thrift_platform_supported`），安装前请先确认平台。
> - **`xml`（lxml）是二进制依赖**：离线部署要按目标平台多带 wheel（与 `rpds-py` 同类）；
>   不装也能用框架，只是 `xpath_match`/`xpath_count` 会给出安装提示（退路：标准库版项目级算子，
>   见 `examples/soap/debugtalk.py`）。

安装后验证扩展是否就绪（期望均输出 `True`）：

```bash
python -c "from interfacetester.ext.uploader import UPLOAD_READY; print('upload ready:', UPLOAD_READY)"
python -c "from interfacetester.step_sql_request import SQL_READY; print('sql ready:', SQL_READY)"
```

---

## 7. 卸载

```bash
# 卸载包（可编辑安装同样用 uninstall）
python -m pip uninstall interfacetester -y

# 如需彻底移除虚拟环境，直接删除目录即可
# Windows:  Remove-Item -Recurse -Force venv
# Linux:    rm -rf venv
```

---

## 8. 常见问题（FAQ）

### 8.1 移动/重命名了项目目录后，命令全部报错？

```
Fatal error in launcher: Unable to create process using '"D:\...\python.exe" ...'
```

**原因**：Windows 虚拟环境里的 `.exe` 启动器（`pip.exe`、`hrun.exe` 等）在创建时
**硬编码了当时的绝对路径**，目录被移动/重命名后路径失效。

**解决**（`python.exe` 本身通常仍可用，因为它是通过 `pyvenv.cfg` 定位基础 Python）：

```bash
# 1) 用 python -m pip 绕过失效的 pip.exe，重新可编辑安装
python -m pip install -e . --no-build-isolation

# 2) 修复 pip.exe 启动器
python -m pip install --force-reinstall pip

# 3) 其它 console 脚本（hrun/hmake/interfacetester）在步骤 1 重装时已自动重建
```

若仍异常，最稳妥的做法是**重建虚拟环境**：

```bash
# 删除旧 venv 后重来
python -m venv venv
# 再按第 3 节重新安装依赖 + 项目
```

### 8.2 `hmake` 提示找不到 `black`、格式化被跳过或卡住？

按下面的顺序排查（`hmake` 调用 black 失败/超时都只告警，不会中断用例生成）：

1. **先确认 black 装在当前解释器里**——`hmake` 优先用 `python -m black`（即当前解释器
   的那一份），只有当前解释器没有 black 时才去 PATH 找：

   ```bash
   python -c "import black; print(black.__version__)"   # 当前解释器是否有 black
   ```

2. 若当前解释器没有，安装它（black 是本项目声明的运行时依赖，正常安装不应缺失）：

   ```bash
   python -m pip install "black>=22.3"
   ```

3. **出现「black 超时（60s），已跳过格式化」**：说明 black 进程在 60s 内没结束。
   框架已经把 black 的缓存目录指到临时目录（避开「缓存目录不可写导致 black 卡住」这个最常见的
   原因，见 `make.get_black_env`），所以再遇到超时通常是别的阻塞（磁盘/杀软扫描、超大文件等）：

   ```bash
   # 调大超时上限（秒），先确认 black 到底卡在哪
   INTERFACETESTER_BLACK_TIMEOUT=180 hmake test.yml
   ```

   也可以自行指定缓存目录（用户显式设置的 `BLACK_CACHE_DIR` 会被尊重、不覆盖）：

   ```bash
   # Windows PowerShell
   $env:BLACK_CACHE_DIR = "$PWD\.black-cache"; hmake test.yml
   # Linux / macOS
   BLACK_CACHE_DIR="$PWD/.black-cache" hmake test.yml
   ```

   无论超时与否，生成的 `_test.py` 都是语法正确、可以直接跑的，只是没经过格式化。

### 8.3 `pip install .` 后 `import interfacetester` 报 ModuleNotFoundError？

多半是**依赖未手动安装**（见 3.3 节）。请确认已按 3.3 节安装依赖，再重新安装项目。

### 8.4 运行时报 `urllib3` 相关错误？

确认 urllib3 版本 `<2`：

```bash
python -m pip install "urllib3>=1.26,<3"
```

### 8.5 关于遥测

本项目已禁用官方遥测：Sentry 上报为空操作（相关 import 已移除），
Google Analytics（GA4）客户端为禁用状态，不会向官方服务器发送任何数据。

---

## 附录：依赖清单

### 运行时必需依赖

| 包 | 版本约束 | 实测版本（.venv） | 用途 |
| --- | --- | --- | --- |
| pydantic | `>=2.0,<3` | 2.13.5 | 数据模型校验 |
| loguru | `>=0.4.1` | 0.7.3 | 日志 |
| jmespath | `>=0.9.5` | 1.1.0 | JSON 路径提取 |
| Jinja2 | `>=3.0.3` | 3.1.6 | 模板渲染 |
| PyYAML | `>=6.0.1` | 6.0.3 | YAML 解析 |
| requests | `>=2.31` | 2.34.2 | HTTP 请求 |
| urllib3 | `>=1.26,<3` | 2.8.0 | HTTP 底层（兼容 1.x/2.x） |
| toml | `>=0.10.2` | 0.10.2 | TOML 解析（包内单测使用） |
| Brotli | `>=1.0.9` | 1.2.0 | 压缩算法（历史遗留声明） |
| black | `>=22.3` | 26.5.1 | hmake 代码格式化（缺失时降级跳过） |
| pytest | `>=7.1` | 9.1.1 | 用例运行 |
| pytest-html | `>=3.1` | 4.2.0 | HTML 报告 |
| **jsonschema** | `>=4.0,<5` | 4.26.0 | **内置算子 `jsonschema_match`（契约断言）**；纯 Python，唯一二进制传递依赖是 `rpds-py`（有全平台 wheel，离线部署别漏） |

### 可选依赖（extras）

| 包 | 用途 |
| --- | --- |
| allure-pytest | allure 报告 |
| requests-toolbelt / filetype | 文件上传 |
| sqlalchemy / pymysql | SQL 请求 |
| cython / thrift / thriftpy2 | thrift 请求 |
| **lxml** | **XML/XPath/XSD 断言（extra `xml`，内核 `xpath_match`/`xpath_count`/`xml_schema_match`）**；二进制包，离线要按目标平台带 wheel |

### requirements.txt

根目录 `requirements.txt` 是**已锁版本**的运行时依赖清单（版本取自 `.venv` 实测环境），
直接用于离线/复现部署。需要重新生成时：

```bash
python -m pip freeze > requirements.lock.txt
```

> 生成后请过滤掉 `interfacetester`、`pip`、`setuptools`、`wheel`、`build` 等本地/构建条目。

