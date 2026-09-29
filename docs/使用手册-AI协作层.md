# 使用手册：AI 协作层（`haify`）

> **给谁看**：拿到这个包、要把它用在**真实接口文档**上的人——**不要求读源码**。
> **一句话**：`haify` 把「接口文档 → 可跑的用例」这条链路里**机械的部分**做掉；
> 但**判定通过/失败永远留在内核**（模型只负责生成与分析，见 §6 第一条硬边界）。
> **口径**：本手册讲"**怎么装、怎么配、怎么跑、怎么判断跑对了**"；
> **所有基线数字以 `README.md`「六、测试与质量」为唯一来源**——这里不抄第二份
> （抄一份就多一个会过期的副本，这是本仓的明文纪律）。
> **配套**：配置细节 `deploy.md` §5.1；部署/验收 `deploy.md` §4；CI 与验证清单 `docs/ci/README.md`；
> 模块职责 `docs/架构与调用链.md` §2.2。

---

## 1. 装

| 场景 | 怎么做 |
| --- | --- |
| 有网、要跑测试 | `pip install -e ".[test]"`（内核）+ `pip install -e interfacetester_ai`（**AI 层**，见下 ★） |
| 有网、只要跑起来 | `pip install -e .` + `pip install -e interfacetester_ai`（AI 层**零三方依赖**，见下） |
| **内网/离线** | 把**两个** wheel（内核 + `interfacetester_ai`）+ `requirements.txt` 一起带过去，用 `--no-index --find-links=./wheels` 安装（步骤与实测见 `deploy.md`；离线打轮也已实测） |

> ★**`haify` 这个命令从哪来**（2026-09-27 实测踩过，写在这里免得再踩）：
> `haify` 声明在 **AI 包自己的** `pyproject.toml`（`[project.scripts]`），**不在**根 `pyproject.toml`
> —— 根上只有 `hrun` / `hmake` / `hconvert` / `interfacetester`。所以只跑 `pip install -e .`
> （只装内核）时：**`hrun` 有、`haify` 没有**，敲 `haify` 会报"无法将 `haify` 项识别为 cmdlet /
> 命令 / 脚本文件…"。两条路都行：
>
> - **装 AI 包**（推荐，装完就能像 `hrun` 一样直接敲）：
>   `python -m pip install -e interfacetester_ai --no-build-isolation`（离线也可，见下表）；
> - **不装也能跑**（等价入口，同一份 `cli.main`，不存在两套行为）：
>   `python -m interfacetester_ai gen <文档.md>`。
>
> 装完**自查一句**：`haify -V` → 应当打印 **AI 层版本 + 内核版本**（两个都报：
> 内核版本取自 `interfacetester/__init__.py` 的 `__version__`，AI 层版本取自
> `interfacetester_ai/__init__.py`）

- **AI 层只依赖内核的公开面**（`converters` / `loader` / `make` / `models`），**不 import 运行内核**；
  反过来**内核永不 import AI 层**——所以「`pip uninstall` AI 层」就是**回滚**。
- AI 包本身**零三方依赖**（顶层 import 只许 stdlib 与本仓）；它复用的少数几个第三方（如 `PyYAML`、`requests`）
  **已在内核的 `requirements.txt` 里**，且只在**函数体内** import（没装它本包仍可导入）。

## 2. 配（★最容易踩的一处）

**配置一律写在环境变量里。AI 层不读项目 `.env`。**

| 变量 | 必填？ | 默认 | 说明 |
| --- | --- | --- | --- |
| `INTERFACETESTER_AI_BASE_URL` | ✅ | — | OpenAI 兼容端点，如 `http://127.0.0.1:11434/v1` |
| `INTERFACETESTER_AI_MODEL` | ✅ | — | 模型名，如 `gemma4:latest` |
| `INTERFACETESTER_AI_API_KEY` | 非回环必填 | — | **回环地址（`127.0.0.1`）免 key**——本机 Ollama 不用配 |
| `INTERFACETESTER_AI_TIMEOUT` | 建议 | **`180`** | 秒。**本地大模型要调大**（300 起、慢的到 1800；默认值已于 2026-09-25 由 60 调到 180）；★超时报错文案会直接把这一条告诉你 |
| `INTERFACETESTER_AI_SANDBOX` | 真出网时 ✅ | 关 | `on` 才允许真发请求（L3 闸门）；**默认关闭**，不会静默出网 |
| `INTERFACETESTER_AI_ALLOW_HOST` | 非回环时 ✅ | 空 | 显式列名的主机（逗号分隔），如 `api.example.com` |
| `INTERFACETESTER_AI_RETRIES` | 否 | `2` | 5xx / 网络异常重试次数（**4xx 不重试**：那是配置或请求问题，重试只会重复付费） |

```powershell
# PowerShell（本会话有效）
$env:INTERFACETESTER_AI_BASE_URL = "http://127.0.0.1:11434/v1"
$env:INTERFACETESTER_AI_MODEL    = "gemma4:latest"
$env:INTERFACETESTER_AI_TIMEOUT  = "300"
$env:INTERFACETESTER_AI_SANDBOX  = "on"      # 真出网才需要
```

```bash
# Linux / macOS
export INTERFACETESTER_AI_BASE_URL=http://127.0.0.1:11434/v1
export INTERFACETESTER_AI_MODEL=gemma4:latest
export INTERFACETESTER_AI_TIMEOUT=300
export INTERFACETESTER_AI_SANDBOX=on
```

> ⚠ **为什么不读项目 `.env`**：项目 `.env` 是**被测用例**的环境变量来源（内核的 `${ENV(...)}`），
> 属于"被测项目"；工具自身的配置属"工具"。两者混在一个文件里，出问题时分不清
> 是"工具没配好"还是"被测项目没配好"——而且**模型配置缺项必须响亮报错**（退出码 2），
> 不能等到某条用例跑起来才发现。

## 3. 跑

| 命令 | 干什么 | 要模型吗 |
| --- | --- | --- |
| `haify gen <文档.md>` | 文档 → 用例草稿（内含**生成前文档体检闸 S-10**、切片、修正环 ≤2 轮、**装配前合并确定性断言**〔C-2〕） | 要 |
| `haify gen --assertions-only <文档.md>` | **确定性断言映射**（字段表 → 类型/范围/枚举断言，**不调模型、可复现**）；★**按接口段逐片**：一份文档里 N 个接口 → **N 条草稿**（段外断言不进本条，写进该条的 `unknowns`）；★**没有「必填」列的响应表** → 生成一条 `jsonschema_match`（**存在才校验类型**：字段缺失不算失败） | **不要** |
| `haify review --list` / `--approve <草稿>` | **所有** AI 草稿都要**人工确认后**才转正进 `cases/`（★默认落点是 `.ai/draft/`，不是 `cases/`）；`--list` 显示**质量状态与 blocker**，`--approve` 前先摊开"凭什么放行" | 不要 |
| `haify analyze` | 失败诊断 → `reports/analysis.md`（六分类归因 + 文档缺陷回流） | 要 |
| `haify summary` | 结果摘要 → `reports/summary.md`（**数字全由代码渲染**，模型只写导语） | 要（无模型时给模板版） |
| `haify fix` | 自愈建议：**只给 diff，不落地**（`--apply` 永不存在） | 要 |
| `haify debugtalk` | 签名/加密函数草稿（**只给追加块**；没有测试向量就拒产） | 要 |
| `haify serve` | **只读看板**（默认只监听回环；零写盘、零执行）★`/pending` 页显示**草稿质量状态与 blocker** | 不要 |
| `haify run` | 触发运行（**必过 L3 闸门**；作业串行、目录隔离） | 不要 |
| `haify export` | **离线静态导出**（`.ai/export/`，可直接 `file://` 打开） | 不要 |

**退出码**（脚本化时看这个，不要只看日志）：

| 码 | 含义 |
| --- | --- |
| `0` | 正常（★**草稿已产出**：产物在 `.ai/draft/`，**不等于**可以进 CI） |
| `1` | 生成失败（**产物不落 `cases/`**，提示在 `.ai/failed/<用例>/NEEDS_HUMAN.md`） |
| `2` | 配置缺项 / 闸门拒绝（**没有隐式出网**） |
| `3` | 传输失败（网络/超时；★报错文案会提示该调哪个变量） |
| `4` | 文档不合格（体检闸 S-10 拒了，必改项在 `reports/NEEDS_DOC_FIX.md`） |
| `5` | **保留码**：输入超预算。★2026-09-26 实测（决策清单 §四 M-4）：`gen` 路径上"预算超限"表现为**片级失败** → 退出码 **1**，本码**当前无端到端入口**（脚本化请按 `1` 判；判据见 `tests/ai_exit_codes_test.py`） |

`haify gen` 的两个常用开关：
- `--fake`：**只许回放**（命中 `.ai/cache/` 才成功）——CI 不出网就用它；
- `--no-skip-non-interface`：不跳过"本就没有接口"的章节（默认跳过：标题/概述/字段表/附录这类片里
  没有 `METHOD /path`，送模型只会得到编造的端点）。★2026-09-26 起，**正文只有交叉引用**的片
  （如「……文件重命名接口（`POST /api/metadata/rename`）见 [3.5 文件重命名](#35-文件重命名)。」）
  也算「本就没有自己的接口」→ **默认跳过**（省一次调用；那个真端点会在它自己的章节里照常处理）。
- `--no-merge-assertions`：关掉「模型草稿 × 确定性映射」的**合并**（C-2，**默认开**）。
  合并的规矩是**只补不覆盖**：模型没断言的字段，用文档字段表/说明句给它补上**字段级**断言
  （类型/范围/枚举）；模型已断言的字段**保留模型的**，两边不同则记成**冲突**进
  `.ai/draft/<用例>.unknowns.json`。★它**不救**"模型零断言"的草稿——那属于"模型没干活"的
  质量信号，工具替它补上就等于把问题藏起来（该走 `haify gen --assertions-only` 或人工）。

`haify gen` **收尾会打一行 T28 降级统计**（`gen` 与 `--assertions-only` **两条通路都有**）：

```text
[haify gen] T28 降级统计：30/75 = 40%　原因：quote-miss×18、field-not-in-quote×9、endpoint-miss×3
```

★这条数字说的是**模型的引用质量**（引用句必须**逐字**出现在文档里才算数），不是工具坏了：
**40% 表示"这份文档上模型引了太多文档里根本没有的话"**。被拦下的断言**不会进用例**
（宁缺毋滥），所以它同时也是"这份用例为什么看起来薄"的解释。
口径两点：**分母是模型提议的条数**（含被拦下的那些）；分母为 0 时打
"没有可比对的断言"而**不报 0%**（"没断言可评"≠"全都没问题"）。

**★引用写歪时工具会做什么（WP2.2「证据绑定」，2026-09-26）**：模型常把引用写成
**标题**（`成功响应`）、**自造锚点**（`response_example`）或**字段说明那一格**（`操作是否成功`）
—— 三种坏法的共同点是"**引用选歪了，而事实在文档里**"。这时工具**不立刻降级**，而是把引用
**解析成文档里承载该事实的那一节**（再窄化到那一行），交给同一套 T28 对账；对上了就留下，
并在交付物里**逐条记账**：

- `unknowns` 里出现 `quote-bound`（**交付物可见**），说明"这条断言的证据是**工具绑的**、
  不是模型引的"，并给出**绑到了哪一行哪一节**；
- 它在质量状态里**升级成 blocker `unknown-critical`** → 转正时要点名处置
  （`--resolve-blocker unknown-critical`）—— "证据来源变了"这件事**必须被人看见并点头**。

实测（客户真实文档的模型留痕）：模型给的 20 条断言里，只按它自己那句话判要拦 **15 条（75%）**，
绑定后只拦 **3 条（15%）**，而那 3 条**全是"把示例值当契约"**（本该交人工）。
**★绑定的边界**：窗口落在**别的接口那一段**里 → **拒绝**（不许拿别的接口的字段表当证据）；
落在**全篇共享节**（统一响应格式 / 公共请求头 / 错误码表）里的 → 允许但要标 `shared`。

**★`type_match` 的类型名（对应闸门 S7）**：闸门只认**内置类型名**（`int`/`str`/`float`/`dict`/`None`…），
而文档常写 `number`、中文文档写 `字符串`。装配器会**先把无歧义别名归一**（`字符串`/`string`→`str`、
`整数`→`int`、`对象`→`dict`），**有歧义的**（`number` 到底是 `int` 还是 `float`？）**不猜** ——
摘掉那一条断言、写进 `unknowns` 的 `type-name-unusable`，而**用例照常产出**
（一条断言的**名字**错了不该把整条用例打死；闸门 S7 是 REJECT 级，会拒**整条**）。

**★产物要"跑得起来"，必须给目标地址**（2026-09-26 打通"生成 → 真跑"时实测）：

```bash
haify gen --base-url http://host:port <文档.md>        # 写进产物的 config.base_url
```

不写它 → 产物只有相对路径的 `url`，`hrun` **第一句就报** `ParamsError: base url missed!`；
CLI 会把**文档 §1.1 自己写着的服务地址**（开发/测试/生产）摊出来让你挑一个。
★注意 `base_url: ${ENV(BASE_URL)}` **不管用**：内核要求它是**字面量 URL**（变量引用没有 netloc）。

跑起来的两步（转正 → 运行，都要留痕）：

```bash
haify review --approve <草稿> --approver <你的名字> [--resolve-blocker <码>...]
haify run cases/<用例>.yml --approver <你的名字> --base-url http://host:port
```

`haify review` 的两个开关（转正那一步）：

- `--approver <姓名>`：**必填**（"谁确认的"是留痕的全部意义）；
- `--resolve-blocker <码>`：★**逐条点名处置**某个 blocker（可重复）。未点名的 blocker 会
  **拦住转正**——`protocol-only`（断言全是协议级）、`unknown-critical`（引用/端点对不上）、
  `todo-placeholder`（`TODO_` 没填值）都算。**自由文本 `--notes` 不算处置**：
  否则"我看了"是一句没有对象的话，闸门会退化成橡皮图章。

```powershell
haify review --list                                              # 看质量状态与 blocker
haify review --approve login --approver 张三 \
  --resolve-blocker protocol-only                                # 逐条点名处置后再转正
```

  等价的环境变量：`INTERFACETESTER_AI_MERGE_ASSERTIONS=off`。

## 4. 怎么判断"这次跑对了"

**别只看退出码**——本仓的判据是"**证据在哪**"：

| 你要确认的事 | 看哪里 |
| --- | --- |
| 产物真的落盘了吗、能不能被内核加载 | `cases/*.yml`（`haify review` 转正后才在这里） |
| 带未确认期望值的草稿 | `.ai/draft/`（**`TODO_` 草稿只许落这里**） |
| **草稿的质量状态与 blocker**（该不该放行） | `haify review --list`，或看板 `/pending` 页的「**草稿与质量状态**」栏 |
| 模型这次到底返回了什么（幻觉复盘） | `.ai/manifest/*.json`（**原始响应照录**，含命中回放那次） |
| 哪些期望值文档里没写 | `.ai/pending/<用例>.pending.json`（给人填的清单） |
| 被闸门拒了、人要接着干什么 | `.ai/failed/<用例>/NEEDS_HUMAN.md` |
| 缓存有没有生效 | `.ai/cache/`（密钥含模型/参数，**改任一采样参数必 miss**） |
| 谁确认过哪一版 | `.ai/reviews/<用例>.json`（含**内容哈希**：确认后又改一行 → 留痕失效） |

## 5. ★已知的 2 个"环境遗留"失败（**跑单测时会看到，不是你的问题**）

跑 `pytest tests`（README「六」的**确定性口径**那条命令）时，本机会有 **2 个失败**。
它们**不因改本仓代码而变化**，也与"交付物能不能用"无关 → **不计入基线**：

| 失败用例 | 原因 | 怎么让它变绿 |
| --- | --- | --- |
| `docs_consistency_test.py::TestDocumentationLinks::test_local_markdown_links_resolve` | 文档里有指向**本机不存在**的文件的链接：`docs/使用教程.md`、`docs/使用说明.md`（这两个文件在**上游真仓**里存在，本工作区没有），另含 0923 文档的 8 条 `.venv/site-packages` 死链 | 在**上游真仓**（文件齐）里跑，或把这两个文件的链接改成工作区里的实际文件名 |
| `dependency_consistency_test.py::...::test_requests_toolbelt_is_not_a_hard_prerequisite` | 需要 `upload` extra；本机**无外网**装不上 | `pip install -e ".[upload]"`（有网环境） |

- **判定口径**：如果失败**不是**上面这两个（名字对不上），那它**可能是真的回归**——按 §7 去查。
- **另**：若跑的是**全量**（去掉那条命令里的 6 个 `--deselect`），还会多出 6 个打公网
  `postman-echo.com` 的用例（断网即失败）——那正是确定性口径要排除它们的原因。
- **数字别抄**：期望值与逐条说明在 `deploy.md`「验证安装」第 8 条；**基线的唯一来源是 `README.md`「六、测试与质量」**。

## 6. 三条硬边界（知道就不会误用）

1. **模型永不判定"通过/失败"**：生成与分析归模型，**判定归内核**——所以"模型说没问题"不作为结论。
2. **写盘边界登记在册**：AI 层只写 `.ai/`（`cache`/`manifest`/`pending`/`reviews`/`export`/`work`/`draft`/`failed`）
   与 `reports/`；`llm`/`gen`/`web`/`confirm`/`audit` **一律不写盘**。可用 `python bench/write_boundary_scanner.py` 自查。
3. **沙盒与 L3 闸门**：真出网必须显式开 `INTERFACETESTER_AI_SANDBOX=on`（非回环还要列 `_ALLOW_HOST`）；
   **拒绝是响亮的**，不会静默降级成"看起来成功"。

## 7. 出问题了先看这三处

1. `deploy.md` §5.1（配置）+ §8（FAQ：找不到 black / 移动目录后命令全挂 / urllib3 报错 …）；
2. `docs/ci/README.md`（CI 两个平台怎么跑、11 条验证清单、6 个外网用例的坑）；
3. `docs/架构与调用链.md` §2.2（AI 层 31 个模块各自干什么——想改代码时从这张表进）。

> **本手册的维护约定**：**不写会过期的数字**（用例数/基线/文件行数），只写"去哪看"与"怎么判断"；
> 唯一事实来源见上。手册与 `deploy.md`/`README.md` 冲突时，以那两份为准，并请回头修正本手册。
