# CI 集成（模板 + 必须知道的坑）

> **一句话**：仓库里现在有 **GitHub Actions** 与 **GitLab CI** 两份可直接用的模板，
> 本文件写清「每一条为什么这么写」以及**内网 CI 一定会踩的 9 个坑**。
>
> 目标不是「能跑起来」，而是「**稳定地绿**、失败能定位、报告能当交付凭证」。

---

## 一、交付物与平台选择

| 文件 | 用途 | 状态 |
| --- | --- | --- |
| `.github/workflows/test.yml` | GitHub Actions：`unit-test`（全量单测，**排除外网用例**）+ `guardrails`（**护栏自检门禁**：十二条自检脚本，v8/S-7）+ `package`（**交付物校验**：构建**内核与旁路包**两个 wheel 并校验其内容与源码一致）+ `network-tests`（只跑外网用例，`continue-on-error`）+ `examples-smoke`（examples 冒烟：httpbin 4 个 yml + `examples/data_management` + `examples/soap` + `examples/soap_xpath`） | P1-c 新增；P2-a 扩了数据管理示例；SOAP 阶段扩了 `examples/soap`；**A2-1 扩了 `examples/soap_xpath` 并把 `unit-test`/`examples-smoke` 的安装行改为 `".[dev,xml]"`；A2-2 只扩充该示例的用例（`xsd.yml` + 对照），CI 无需再改**；**0918-6 新增 `package` job（H5/坑 6）**；**v8 新增 `guardrails` job（S-7：此前护栏只在本地绿）**；**P3c 第十二批把旁路包 wheel 也纳入 `package` job** |
| `.gitlab-ci.yml` | GitLab CI：同五个 job（客户内网**以 GitLab 为主**；**v8 同步 `guardrails`**） | P1-c 新增；P2-a / SOAP 阶段 / A2-1 同步；**0918-6 同步 `package` job**；**v8 同步 `guardrails`** |
| `docs/ci/README.md` | 本文件 | 新增（P1-c） |
| `tests/mock_server.py` | **支持单独启动**（`python tests/mock_server.py`），供 examples 冒烟后台常驻 | P1-c 新增入口 |
| `interfacetester/utils.py` | mock 端口可用环境变量配置（默认仍是 80/443） | P1-c 改动 |

> **为什么还要一个 `guardrails` job（v8 新增，S-7）**：本仓有一批**自检式护栏**——
> `bench/` 的六个脚本（含 T10 的误伤率审计）+ 包内五个入口（`python -m interfacetester_ai.{gates,doc_quality,diagnosis,normalize,workdir}`）。
> 它们此前**只在本地绿**：CI 只跑 `pytest`，于是"有人改了闸门/扫描器、自检不再命中"
> 这件事**没有任何人会看见**（pytest 覆盖的是判据行为，不是"脚本自身还能跑通"）。
> 本 job 把十二条自检变成门禁：任一条退出码非 0 → job 红。
> **安装集刻意只装 `test`**：`xml`/`upload` 与护栏无关，装上去反而会掩盖
> 「护栏对 extra 的隐式依赖」——本机（**未装任何 extra**）实测十二条全绿即为此口径的取证。
> 另：十二个入口都已按 T26 纪律做 `reconfigure(encoding="utf-8")`，
> 重定向/CI（非交互 + GBK locale）下不会因输出编码**假红**。
> **★扩容纪律**：新增护栏自检时必须同时加进本 job——
> 否则新护栏从第一天起就"只在本地绿"，等于把 S-7 要堵的洞重新挖开。

> **为什么把「外网用例」单独拆成非阻塞 job**：那 6 个用例打的是公网 `postman-echo.com`，
> P1-c 收尾时**实测又红了一次**（`3 failed, 184 passed`，全是 120s 读超时）。
> 放在主 job 里就是「随缘变红」；删掉又丢信号 —— 所以主 job 排除它、它自己单独跑且 `allow_failure`。

> **为什么还要一个 `package` job（0918-6 新增，坑 10）**：单测跑的是**源码树**
> （`pip install -e .`），所以 **wheel 内容错了它一点都看不出来**。实测 `dist/` 里那份
> wheel 是 09/16 构建的，缺 `converters/` **整目录**（P3 的四个导入器）、缺
> `jsonschema_match`/`xpath_match`/`xml_schema_match` 三个算子，而**单测全绿** ——
> 照 README/deploy 的发布流程（`python -m build --wheel → dist/*.whl`）交付就会发出旧包。
> 所以单独一个 job：构建 wheel → `tests/wheel_content_test.py` 校验**产物内容**。
> 判据刻意与**源码对比**（源码树里每个 `.py` 都要在 wheel 里、算子清单要一一对应、
> 入口点要与 `pyproject.toml` 一致、版本要与 `__version__` 一致），
> 不硬编码清单——下次新增子包/算子会自动被覆盖。
>
> **★旁路包同理（P3c 第十二批，2026-09-25）**：`interfacetester_ai` 是**另一个包、
> 另一份产物**，所以本 job 现在打**两个** wheel：
> `python -m build --wheel --outdir dist_ai interfacetester_ai` →
> `INTERFACETESTER_AI_WHEEL_PATH=dist_ai/*.whl pytest tests/packaging_test.py -q`。
> 判据同样是「产物 ↔ 源码树」（模块集合 / 零依赖 / 入口 / RECORD 完整性 / 无包外文件），
> 再加一条**离线**的「打轮 → 装 → 入口」round-trip（`--no-index`，不碰网络）。
> 为什么这条重要：P3c 要的是"离线环境一键起服务"，而源码树单测**永远看不出**
> 轮子少了模块或声明了三方依赖。

**平台怎么选**

| 平台 | 建议 | 理由 |
| --- | --- | --- |
| **GitLab CI** | 客户内网交付**首选** | 企业内网自建 GitLab 最普遍；模板已给，且不需要额外权限就能跑 |
| **GitHub Actions** | 自用 / 开源分支 | 免费额度够用、artifact 下载方便；但客户内网通常没有 |
| **Jenkins** | 兜底（**本批次未交付 Jenkinsfile**） | 存量 Jenkins 多，但写法五花八门；照下面「三、验证清单」里的命令抄进 pipeline 即可，注意 Jenkins agent 的工作目录 = 项目根 |

---

## 二、快速开始（推 CI 之前，先在本地把这两条命令跑通）

```bash
# 0) 依赖（pytest / pytest-html 已在主依赖里，dev 只是构建工具）
pip install -e ".[dev]"

# 1) 全量单测 + 报告（本地会跑 5 分钟上下；CI 上同理）
mkdir -p reports
python -m interfacetester run tests/ \
  --html=reports/report.html --self-contained-html \
  --junitxml=reports/junit.xml

# 2) examples 冒烟（先拉本地 mock，再跑；不依赖外网）
python tests/mock_server.py &                     # 常驻；Ctrl-C 退出
python -m interfacetester run \
  examples/httpbin/basic.yml \
  examples/httpbin/hooks.yml \
  examples/httpbin/validate.yml \
  examples/httpbin/load_image.yml \
  --junitxml=reports/examples-junit.xml

# 3) 数据管理示例冒烟（P2-a；自带本地有状态 mock，不需要上面那个 httpbin mock）
python -m interfacetester run examples/data_management \
  --junitxml=reports/data-management-junit.xml

# 4) SOAP/XML 示例冒烟（各自带本地 mock）
python -m interfacetester run examples/soap \
  --junitxml=reports/soap-junit.xml
# A2-1 的内核算子需要 xml extra（lxml）；不装的话这一步会报「未安装 lxml」并给出安装命令
pip install -e ".[xml]"
python -m interfacetester run examples/soap_xpath \
  --junitxml=reports/soap-xpath-junit.xml
```

**本机实测（Windows / Python 3.12.10 / pytest 9.1.1）**

| 命令 | 结果 |
| --- | --- |
| `pytest tests` | **187 passed / 4 skipped**（47~83s）；网络不好时会看到 **3 failed**（全是那 6 个外网用例，见坑 5） |
| `pytest tests` + 排除那 6 个 | **181 passed / 4 skipped / 6 deselected**（11~22s，**全程无外网**） |
| `python -m interfacetester run tests/` + 排除那 6 个 | **181 passed / 4 skipped / 6 deselected**（11s，**全程无外网**） |
| examples 冒烟（4 个 yml，mock 在 8000/8443） | **4 passed / 退出码 0**，`reports/examples-junit.xml` 正常生成 |
| 数据管理示例冒烟（`run examples/data_management`） | **4 passed, 1 skipped**（skip = `sql/` 缺 `sql` extra，按设计 skip 而非失败） |
| SOAP/XML 示例冒烟（`run examples/soap`） | **3 passed, 1 skipped**（自带本地 mock，用系统分配端口；skip = 对照演示用例 `contrast_*` 按设计跳过） |
| 内核 XML 算子示例冒烟（`run examples/soap_xpath`，A2-1/A2-2，需 `xml` extra） | **2 passed, 2 skipped**（skip = `contrast_prefix_literal.yml` 与 `contrast_xsd_violation.yml` 两个对照演示） |
| 报告链（`--html --self-contained-html --junitxml`） | HTML **约 230~255 KB**（单文件、内嵌全部用例明细）、JUnit XML **约 23~30 KB**（大小随用例数与失败详情波动，均正常生成） |

> 4 个 skip 的原因：`pytest-xdist` 未装（1）、Windows 上 thrift 不可用（1）、`thriftpy2` 未装（2）。
>
> **NOTICE：上面的用例数是 P1-c 交付当时的快照**（187 / 181）。此后每个批次都会加用例
> （P2-b、P3 四个导入器、SOAP 示例…），**当前基线以 `README.md` 的「六、测试与质量」为准**；
> 本文表格保留原值，是为了让「同一条命令在不同机器上的相对差异」仍然可比。
> 这些都是**按设计 skip**，不是失败；容器里跑时第一、二条会随环境变化。

---

## 三、四条基础坑（评估文档 3.4，含本轮实测订正）

### 坑 1：工作目录必须能在用例路径上找到 `debugtalk.py`

- **正确规则**（实测，见《接口自动化框架升级优化评估》3.3/5.9）：`loader` 是沿**用例路径向上**找
  `debugtalk.py`，找不到才退化为 `os.getcwd()`；真正失败的是「**向上找不到** `debugtalk.py`
  **且** 用例路径不在 cwd 之下」，此时报
  `failed to convert absolute path to relative path based on project_meta.RootDir`（`loader.py:604-621`）。
- **动作不变**：CI 里把工作目录设为**项目根**（含 `debugtalk.py` 的那层）。本仓库的模板用的是仓库根，
  所以 GitHub/GitLab 默认行为即可；**monorepo / Jenkins** 里必须显式设置（GitHub: `defaults.run.working-directory`；
  Jenkins: `dir('path/to/project') { ... }`）。

### 坑 2：80/443 是**特权端口**，非 root 的 runner 绑不上

- 单测依赖的本地 mock 默认监听 **80/443**（`tests/mock_server.py`），`examples/httpbin` 也打这个地址。
  GitHub Actions 的 hosted runner、k8s 里 `runAsNonRoot` 的 runner 都**没有**绑定权限。
- **解法（P1-c 已实现）**：端口从环境变量读，默认不变：

  ```yaml
  env:
    INTERFACETESTER_HTTP_BIN_PORT: "8000"
    INTERFACETESTER_HTTPS_BIN_PORT: "8443"
  ```

  这两个变量由 `interfacetester.utils` 统一解析，`HTTP_BIN_URL` 与 `tests/mock_server.py`
  会自动跟着变，**不需要改任何用例**；端口值非法时会在导入期直接报
  `invalid INTERFACETESTER_HTTP_BIN_PORT: ...`（不做静默回退——静默用 80 会表现为「CI 上莫名 skip 一片」）。
- **写死 `127.0.0.1` 的断言要改**：`examples/httpbin/basic.yml` 原先把 Host 头写成
  `"127.0.0.1"`，端口一变就会假失败；现已改用 `${get_httpbin_host()}`
  （`examples/httpbin/debugtalk.py`，会正确处理「默认端口不出现在 Host 头里」这件事）。
- **验证方式**：

  ```bash
  INTERFACETESTER_HTTP_BIN_PORT=8000 INTERFACETESTER_HTTPS_BIN_PORT=8443 pytest tests \
    --deselect <上面那 6 个 node id>
  # 本机实测：结果与默认端口**完全一致**（用例数见 README.md「六、测试与质量」，本文件不重复抄）
  ```

### 坑 3：内网离线依赖

```bash
# 有网环境预下 wheel（版本清单见 requirements.txt）
pip download -r requirements.txt -d ./wheels
# 离线环境安装
pip install --no-index --find-links=./wheels -r requirements.txt
pip install --no-index --find-links=./wheels --no-build-isolation -e .   # 装本项目本身
```

- 只用内网镜像源时：`pip config set global.index-url https://<内网源>/simple`
  （模板里留了注释位：GitHub 用 `PIP_INDEX_URL`，GitLab 用 `variables.PIP_INDEX_URL`）。
- **可选 extra 按需补**：`sql`（SQL 步骤）、`upload`（文件上传）、`allure`、`thrift`（**仅 Linux/macOS**）、
  `xml`（A2-1 的 XML/XPath 断言，lxml **二进制依赖**）。
  `examples/httpbin/upload.yml` 不装 `upload` 会**直接失败**（见坑 6）。
  **`xml` 与 `upload` 是例外，两个 job 都必须装**：`unit-test` 与 `examples-smoke` 的安装行是
  `pip install -e ".[dev,xml,upload]"`。
  - `xml`：`tests/xml_comparators_test.py::TestLxmlPresence` 会在 `CI=true` 时把「忘了装 extra」
    直接判失败 —— 否则那 50 多个 XML 用例会被静默 skip，等于这两个算子没人测。
  - `upload`（**0920 批次 3 / N13 订正**）：本行此前写的是"不装不会让单测红（相关用例会 skip）"，
    **那句话是错的**。实测（`.tmp_audit/n13_block_plugin.py`，模拟 `.[dev,xml]` 的依赖集）：
    `tests/uploader_test.py` **16 failed**，全部是
    `NameError: name 'filetype' is not defined`（`ext/uploader/__init__.py` 的 `get_filetype`）
    —— 因为那些用例把 `UPLOAD_READY` mock 成 `True`，绕过了"依赖未安装"的清晰报错，
    于是撞上一个与依赖毫无关系的 `NameError`。也就是说：**`unit-test` job 此前必红**，
    "必须绿"的任务一直红着，等于没有红灯信号。
    代码侧已修（依赖缺失时不再裸抛 `NameError`，而是走既有兜底 + 可读提示），
    但**依赖仍必须装**：不装就等于上传这条最复杂的链路没人真测。
  - 离线环境记得给 `xml` 与 `upload` 都带 wheel（`lxml`/`rpds-py`/`filetype`/`requests-toolbelt`）。

### 坑 4：总是产出两类报告

| 报告 | 参数 | 给谁看 |
| --- | --- | --- |
| JUnit XML | `--junitxml=reports/junit.xml` | **CI 平台**（GitHub/GitLab 的测试面板、趋势、失败列表） |
| HTML | `--html=reports/report.html --self-contained-html` | **人**：单文件、无外部依赖，可直接当交付凭证 |

- 单用例细节在 `logs/{case_id}.run.log`（框架自带），artifact 里一并带上。
- **artifact 必须 `if: always()` / `when: always`**：失败那次的报告才是最有用的。
- 退出码：`main_run` 返回 **pytest 的退出码**（`0` 全过、`1` 有用例失败）；
  若一个有效用例都没找到，框架自己 `sys.exit(1)`（`cli.py:40-45`）。

---

## 四、P1-c 实测发现的坑（内网 CI 最容易踩）

### 坑 5：`tests/` 里有 **6 个 node id 依赖公网** `postman-echo.com`

它们借用 `examples/postman_echo/` 的用例，`base_url` 是 `https://postman-echo.com`：

```
tests/step_request_test.py::TestRunRequest::test_run_request
tests/step_request_test.py::TestCaseRequestWithFunctions::test_start
tests/step_testcase_test.py::TestCaseRequestWithFunctions::test_start
tests/step_testcase_test.py::TestRunTestCase::test_run_testcase_by_path   # setUp 里跑的就是那个外网用例
tests/cli_test.py::TestCli::test_debug_pytest
tests/cli_test.py::TestCli::test_run_testcase_with_abnormal_path          # examples/data/a-b.c/2 3.yml
```

网络抖动时表现为 **120s 读超时**（默认超时）→ 整个 job 慢 4 分钟且随机变红。P1-c 收尾实测就红过一次
（`3 failed, 184 passed / 4 skipped`，三条全是它们）。**内网 CI 必须排除**，
模板的做法是「主 job 排除 + 单独一个非阻塞 job 跑它们」：

```bash
# 主 job（必须绿）
pytest tests \
  --deselect tests/step_request_test.py::TestRunRequest::test_run_request \
  --deselect tests/step_request_test.py::TestCaseRequestWithFunctions::test_start \
  --deselect tests/step_testcase_test.py::TestCaseRequestWithFunctions::test_start \
  --deselect tests/step_testcase_test.py::TestRunTestCase::test_run_testcase_by_path \
  --deselect tests/cli_test.py::TestCli::test_debug_pytest \
  --deselect tests/cli_test.py::TestCli::test_run_testcase_with_abnormal_path
# 本机实测：全程无外网、退出码 0（确切用例数见 README.md「六、测试与质量」）

# 外网 job（allow_failure / continue-on-error，别删——删了就丢信号）
pytest <上面这 6 个 node id>
```

> ⚠️ **用 `hrun`/`run` 时，`--deselect` 的值必须写到「节点 id」级别（带 `::类::方法`）**：
> `main_run` 会把**真实存在的路径**当成「用例路径」收走（`cli.py:29-40`），
> 于是 `--deselect tests/step_testcase_test.py` 这种写法会让 `--deselect` 后面**没有值**，
> 直接报 `pytest.main(): error: argument --deselect: expected one argument`（P1-c 实测踩到）。
> `pytest tests --deselect <存在的路径>` 没有这个问题。

> **后续建议**：这 6 个用例要么改成打本地 mock，要么打上 `@pytest.mark.network` 并默认不选。
> 属于「测试自身改造」，不在 P1-c 范围内，已记进 `docs/P1阶段开发记录.md` 的遗留项。

### 坑 6：examples 冒烟必须**自己拉起 mock**，且 `upload.yml` 跑不了

- `tests/conftest.py` 只在跑 `tests/` 时生效（pytest 的 conftest 只覆盖自己的目录及子目录），
  所以 `hrun examples/httpbin` **不会有** mock —— 必须先 `python tests/mock_server.py`（P1-c 新增入口）
  并等它就绪（模板里用 `curl` 轮询）。
- **`examples/httpbin/upload.yml` 从冒烟里排除**，原因两条：
  1. 它需要 `upload` extra（`requests-toolbelt`/`filetype`），没装时 `ensure_upload_ready()` 直接
     `sys.exit(1)`（**是 error 不是 skip**，整个用例报错）；
  2. 它断言 `body.files.file`，依赖**真实 httpbin 的 multipart 解析**，而本地 mock 的 `files` 字段恒为空。
  想让它进冒烟：`pip install -e ".[upload]"` + 用真实 httpbin（docker）而不是本地 mock。
- 其余 4 个 yml（`basic`/`hooks`/`validate`/`load_image`）在本地 mock 上**全通过**（实测 4 passed）。
  其中两处是 P1-c 为了让它们跑通而修的 **mock 保真度**问题：
  - `/cookies/set`、`/cookies/delete` 原先只是回显当前 cookie，没有「改 cookie + 302 跳回 `/cookies`」的语义
    → `basic.yml` 的「set cookie → extract cookie」两步必挂；
  - `/spec.json` 原先只回 3 个顶层键，而 `basic.yml` 断言 `len_eq: [body, 9]`
    → 现按真实 httpbin 的 **Swagger 2.0 文档形状**补到 9 个顶层键。

### 坑 7：测试隔离——`loader.project_meta` 是**全局**的

- `load_project_meta` 有一条「已有 meta 且其 RootDir 覆盖当前路径就直接复用」的快路径
  （`loader.py:536-570`），所以**同一进程里按不同顺序跑**，结果可能不一样。
- 实测表现：同一套用例、同一种排除口径下，`pytest tests` **全绿**，而 `python -m interfacetester run tests/`
  出现 **2 failed**（`parser_test.py::TestParserBasic::test_parse_parameters_testcase` 等）。
  原因是 `run tests/` 会先 `hmake`，再把生成/收集到的 `_test.py` **列表**交给 pytest，顺序与目录收集不同。
- 已在 P1-c 修掉 `parser_test` 里漏掉的一处（加载前先 `loader.reset_project_meta()`）。
  **以后遇到「换个跑法就偶发失败」，先查这个全局**；自己写用例时照
  `tests/cli_test.py` / `tests/make_test.py` 的做法，在 `setUp`/`tearDown` 里重置。

### 坑 8：`hmake` 会重写 `examples/**/*_test.py`（**生成物是入库的**）

- 跑一次 examples 冒烟就会重写 `_test.py`。**要改就改 YAML，别手改生成物**（下次会被覆盖）。
- Windows + `core.autocrlf=true` 时还会出现「`git status` 显示 `M`、但 `git diff` 内容为空」的**行尾噪音**；
  这不是内容变更，CI 里**不要**用生成物做**逐字节** diff 校验。
- **0918-6（L12）补充**：上面那条禁令针对的是**逐字节**对比。现在有一条**可以安全进 CI** 的等价物——
  `tests/generated_artifacts_drift_test.py` 把 `examples/` 复制到 `logs/` 下重新生成，
  再对每个「有对应 YAML 的生成物」断言 **AST 等价**（忽略注释、空白、行号、引号风格）。
  实测三种口径的差异数：逐字节 **8** / 去注释空行 **7** / **AST 0** ——
  前两种会被路径分隔符（本机 `\` vs 入库的 `/`）与 black 版本（`black>=22.3` 无上界）打中，
  只有 AST 既稳定又能抓住真问题（例如 H2 那个「断言被静默丢掉」的漂移）。

### 坑 9：HTTPS mock 的证书从哪来

- `tests/mock_server.py` 优先用 `trustme` 现签自签名证书，否则退回 CPython 自带
  `test/certdata/ssl_cert.pem`；两者都没有 → **https mock 不启动，依赖它的用例 skip**（不是失败）。
- 精简镜像里想确认：

  ```bash
  python -c "import os,sysconfig; p=os.path.join(sysconfig.get_paths()['stdlib'],'test','certdata','ssl_cert.pem'); print(p, os.path.exists(p))"
  ```

  不存在就 `pip install trustme`（或接受那几个用例 skip）。

---

## 五、验证清单（CI 绿了之后，人工确认这几条）

1. **报告存在且能下载**：artifact 里有 `reports/report.html`、`reports/junit.xml`、`logs/`；
2. **用例数对得上**：以 `README.md`「六、测试与质量」的**当前基线**为准
   （**本文件不重复那个数字** —— 批次 9-1 起该数字只有一处事实来源，见 README 该节的 NOTICE；
   本文上面那张表是 P1-c 当时的快照 187 / 181）；
   数量突变先查环境（mock 端口、extra、外网用例）；
3. **外网 job 的失败要有人看**：它 `allow_failure`，红了说明公网抖动或 `postman-echo.com` 不可用，
   不是框架坏了（这类失败的报告在 `reports/network-*.xml`）；
4. **失败能定位**：故意让一条断言失败，确认平台展示的是「断言消息 + check_item/check_value/expected」
   而不是一句 `AttributeError`；细节再看 `logs/{case_id}.run.log`；
5. **examples 冒烟真的是 4 passed**（少了说明 mock 没起来 —— 看 artifact 里的 `mock.log`）；
6. **数据管理示例冒烟是 4 passed / 1 skipped**（skip 的是 `sql/` 缺 `sql` extra，属预期）；
7. **SOAP/XML 示例冒烟是 3 passed / 1 skipped**（skip 的是对照演示用例 `contrast_*`，属预期）；
8. **内核 XML 算子示例冒烟是 2 passed / 2 skipped**（A2-1/A2-2，需 `xml` extra；skip 的是
   `contrast_prefix_literal.yml` 与 `contrast_xsd_violation.yml` 两个对照演示）；若看到「未安装 lxml」
   的报错，说明 extra 没装；
9. **`package` job 绿**（0918-6 新增）：它构建 wheel 并校验产物内容。本地等价命令：

   ```bash
   python -m build --wheel
   INTERFACETESTER_WHEEL_PATH=$(ls dist/*.whl | head -n 1) \
       python -m pytest tests/wheel_content_test.py -q
   ```

   想确认这条检查**真的有在起作用**：把 `INTERFACETESTER_WHEEL_PATH` 指向
   `dist/prev/` 里那份重构前的旧 wheel，应当看到 **3 条失败**并点名缺失的
   `converters/*` 与三个算子（本批次实测如此）；
10. **离线环境**：断网重跑第 1、2 步（用坑 5 的排除写法），确认不再有 120s 卡顿。
11. **`guardrails` job 绿**（v8 新增，S-7）：它把此前"只在本地绿"的十二条护栏变成门禁。
    本地等价命令（**十二条都必须 exit 0**）：

    ```bash
    python bench/silent_traps.py
    python bench/golden_harness.py
    python bench/ai_guardrails.py
    python bench/probe_silent_gate.py
    python bench/write_boundary_scanner.py
    python bench/gate_false_positive_audit.py
    python -m interfacetester_ai.gates
    python -m interfacetester_ai.doc_quality
    python -m interfacetester_ai.diagnosis
    python -m interfacetester_ai.normalize
    python -m interfacetester_ai.workdir
    python -m interfacetester_ai.export
    ```

    想确认这条门禁**真的有在起作用**：把任意一份自检夹具改坏（例如删掉一条
    `MUST_REJECT` 样本），对应脚本会以非 0 退出 —— job 变红，而不是
    "pytest 全绿、护栏其实已经失效"（这正是加这个 job 要防的形态）。

---

## 六、本批次**没有**做的事（避免误以为已覆盖）

- 没有交付 `Jenkinsfile`（见「一、平台选择」的用法说明）；
- 没有把这 6 个外网用例改成打本地 mock（属测试改造，已记为遗留项）；
- **`guardrails` job 也没在真实平台上跑过**：本机已验证的是**十条自检在"重定向 + 无
  `PYTHONIOENCODING`"下退出码全 0**（等价于 CI 的非交互执行形态）；首次接到真实 CI 时，
  按「五、验证清单」第 11 条确认；
- **模板没有在真实 GitHub/GitLab 上跑过**（本环境无法访问外网）：已验证的是
  **模板里的每一条命令在本机的等价执行结果**，以及端口/隔离/报告这些机制的实测行为。
  首次接到真实 CI 时，请按「五、验证清单」逐条确认。
