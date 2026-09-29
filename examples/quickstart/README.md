# quickstart —— 从零搭一个用例工程（最小可运行示例）

本目录是 [`docs/使用说明-用例级.md`](../../docs/使用说明-用例级.md) **第二节「从零搭一个用例工程」的可运行版本**：
照着那一节的 6 步做出来的东西，就是这里。

它故意做得**很小**——核心就 3 个「必须知道」的文件。看完这里，你就知道自己的项目该怎么搭了。

> 另外还有一组**默认跳过**的对照用例（`contrast_*.yml`），演示「用例红了长什么样」**以及
> 「假通过（绿了但什么都没测到）长什么样」**——见本文第五节。

---

## 一、目录里都有什么

| 文件 | 对应《使用说明》 | 说明 |
| --- | --- | --- |
| `debugtalk.py` | 第 2 步 / 第 6 步 | **「门牌号」**：告诉框架「这个目录是一个项目」；同时提供 `${base_url()}` |
| `.env.example` | 第 3 步 | 环境信息示例（复制成 `.env` 后生效） |
| `hello.yml` | 第 4 步 | 第一条用例，用 `${ENV(BASE_URL)}` 取地址 |
| `hello_func.yml` | 第 6 步 | 同一个用例，改用 `${base_url()}` |
| `hello_test.py` / `hello_func_test.py` | — | `hmake` **自动生成**的 pytest 用例（不用手改） |
| `conftest.py` | 第四节 | 让对照用例**默认跳过**（按环境变量打开才跑）；顺带演示「按条件 skip」怎么写 |
| `contrast_*.yml` × 4 | 第五节 | 对照演示：3 条**故意失败**（红）+ 1 条**故意假通过**（绿） |
| `contrast_*_test.py` × 4 | — | 上面 4 条的生成物 |
| `README.md` | — | 你正在看的这份 |

> `hello_test.py` / `hello_func_test.py` 是生成物，**不要手改**（改了下次生成会被覆盖）；
> 要改需求请改对应的 `.yml`。

---

## 二、怎么跑

### 方式 A：零配置（最快，不需要任何准备）

```bash
hrun hello_func.yml
```

预期：**`1 passed`**。

> 为什么不用准备？`hello_func.yml` 的地址来自 `debugtalk.py` 的 `base_url()`，
> 它在没有环境变量时会回落到内置的默认地址。

### 方式 B：用 `.env` 管理环境信息（推荐，也是《使用说明》第 3 步教的）

```bash
# 1) 把示例环境文件复制成真实使用的 .env
Copy-Item .env.example .env      # Windows PowerShell
cp .env.example .env             # macOS / Linux

# 2) 跑整个目录（两条用例都跑）
hrun .
```

预期：**`2 passed, 3 skipped`** —— 跳过的是 3 条对照用例（它们默认不跑，见第五节）。

> 改了 `.env` 里的 `BASE_URL` 再跑，请求就会打到新地址——用例一个字都不用动。
> 这正是「换环境只改一个文件」的做法。

### 想带上 HTML 报告

```bash
hrun . --html=reports/report.html --self-contained-html
```

生成的 `reports/report.html` 是**单文件**报告，浏览器直接打开即可。

---

## 三、没跑通？按症状查

| 终端上的现象 | 多半是 | 怎么办 |
| --- | --- | --- |
| `'hrun' 不是内部或外部命令` / `command not found` | 框架没装，或没激活虚拟环境 | 见 [`../../deploy.md`](../../deploy.md) |
| `EnvNotFound: BASE_URL` | 跑的是 `hello.yml`，但没有 `.env` | 按方式 B 第 1 步复制一份；或先跑方式 A |
| `ConnectionError` / 请求超时 | 网络不通（默认地址是公网服务） | 把 `.env` 的 `BASE_URL`（或 `debugtalk.py` 的 `DEFAULT_BASE_URL`）改成你能访问的地址 |
| `missing dependency tool: black` | 没在虚拟环境里跑 | 见 [`../../deploy.md`](../../deploy.md) |
| 提示 `hello_test.py` 不是本框架生成的、拒绝覆盖 | 你手改过生成物，且删掉了它开头的生成器标记 | 把生成物整个删掉再跑；要改需求请改 `.yml` |
| **用例是绿的，但你不确定断言真的执行了** | 断言字段名可能拼错了（如 `validates:`），框架只**告警**不报错 | 搜一下输出里有没有 `无法识别的字段`；对照看 [`contrast_false_pass.yml`](contrast_false_pass.yml) |

---

## 四、稍微改一改，看会发生什么

| 试一下 | 会学到 |
| --- | --- |
| 把 `debugtalk.py` **改名或删掉**，然后在**别的目录**里跑 `hrun examples/quickstart/hello_func.yml` | 「项目根」是怎么被定位的；为什么不能省这个文件（会报找不到文件） |
| 把 `.env` 里的 `BASE_URL` 改成别的可达地址 | 换环境为什么只要改一个文件 |
| 把 `hello.yml` 的 `expect` 改成 `eq: [status_code, 404]` | 断言失败长什么样（也可以直接看第五节现成的对照用例） |
| 把 `hello.yml` 复制成 `hello2.yml` 再跑 `hrun .` | 新增用例不需要改任何配置，丢进目录就算数 |

---

## 五、看「红了」与「假通过」长什么样（对照演示）

`contrast_*.yml` 是**故意**写成的用例（有的故意失败、有的故意「假通过」），默认被
`conftest.py` **跳过**——它们的存在意义是「演示」，不是「当样板」。要看效果就带上环境变量：

```bash
# Windows PowerShell
$env:QUICKSTART_RUN_CONTRAST = "1"; hrun .
# macOS / Linux
QUICKSTART_RUN_CONTRAST=1 hrun .
```

预期：**`3 failed, 3 passed`**（退出码 1）。**留意那个多出来的 `passed`** —— 它就是 5.2 说的「假通过」。

### 5.1 三条「红了」的对照（下面的失败信息都是实测原样）

| 对照用例 | 教什么 | 真实的失败信息 |
| --- | --- | --- |
| `contrast_assert_fail.yml` | **接口返回与期望不符**——最典型的一种「红」 | `assert status_code equal 404(int) ==> fail`，逐项列出 `check_value: 200(int)` / `expect_value: 404(int)` |
| `contrast_type_mismatch.yml` | **期望值类型写错**（`"200"` 是字符串，`200` 是数字） | `assert status_code equal 200(str) ==> fail`，`check_value: 200(int)` / `expect_value: 200(str)` |
| `contrast_env_missing.yml` | **用例自己就没跑起来**（引用了不存在的环境变量）——排查方向完全不同 | `EnvNotFound: NOT_DEFINED_ON_PURPOSE`（请求**一次都没发出去**） |

三个看点：

1. 前两条的失败信息会**逐项列出** `check_item` / `check_value` / `assert_method` / `expect_value`
   —— 一眼能看出是「值不对」还是「类型不对」，不用去翻日志；
2. 第三条是**另一类**问题：**不是接口有问题，是用例写错了**（名字拼错 / 没建 `.env` / 没设环境变量）。
   见到这类失败，先查用例和配置，别去怀疑接口；
3. 单独跑一条也行（环境变量仍是前提，否则照旧被跳过）：
   `$env:QUICKSTART_RUN_CONTRAST = "1"; hrun contrast_type_mismatch.yml`

### 5.2 一条「假通过」的对照（**绿了，但它什么都没测到**）

`contrast_false_pass.yml` 演示的是自动化测试**最危险**的结果——**用例是绿的**。

它把断言字段名写错了一个字母：`validates:`（正确是 `validate`）。框架确实**告警了**：

```text
WARNING | teststep '想断 status_code 是不是 404——但断言字段名拼错了' 中存在无法识别的字段：
['validates']，它们会被忽略（不会写进生成的用例）。
```

然后**真的忽略**了。证据就在生成物里——断言链**根本不存在**：

```python
# contrast_false_pass_test.py（节选，实测）
teststeps = [
    Step(
        RunRequest("想断 status_code 是不是 404——但断言字段名拼错了").get(
            "${base_url()}/get"
        )
    ),                                  # ← 没有 .validate()，一个字都没有
]
```

**告警不是失败**：这条用例照样 `passed`、退出码照样 0。所以「有告警」这件事，**得有人去看才算数**。

**跟 5.1 的第一条并排看，反差最清楚**——它们断的是**同一个东西**：

| 文件 | 断言字段名 | 结果 |
| --- | --- | --- |
| `contrast_false_pass.yml` | `validates:`（**拼错**） | **passed** ← 假通过 |
| `contrast_assert_fail.yml` | `validate:`（写对） | **failed** ← 这才是你想要的 |

> 一句话结论：**绿 ≠ 测到了**。写完用例顺手确认「我要的断言真的执行了」，比多写十条弱断言管用。

> **这套「默认 skip + 按环境变量打开」是怎么来的**：框架提供的是 `config.skip`（**永远**跳过），
> 「有条件地跳过」得自己写——就是本目录那个 `conftest.py` 里的
> `pytest_collection_modifyitems`，正是 [`../../docs/使用说明-用例级.md`](../../docs/使用说明-用例级.md) 第四节讲的夹具写法。
> 你自己的项目要放「故意失败的演示用例」时，照抄它即可。

---

## 六、接下来

| 想做的事 | 去哪 |
| --- | --- |
| 完全没写过 YAML，想按清单一步步做 | [`../../docs/新手快速上手.md`](../../docs/新手快速上手.md) |
| 每个文件到底干什么、哪些必须哪些可选、每个项目要不要单独配 | [`../../docs/使用说明-用例级.md`](../../docs/使用说明-用例级.md) |
| 不写代码的同事想看懂这些文件 | [`../../docs/使用说明-非技术版.md`](../../docs/使用说明-非技术版.md) |
| 完整语法（hook / 参数化 / 上传 / SQL / 契约断言…） | [`../../docs/使用教程-项目级.md`](../../docs/使用教程-项目级.md) |
| 登录一次给所有用例复用、跑完清理数据 | [`../data_management/README.md`](../data_management/README.md) |
| 换台机器 / 交给同事部署 | [`../../deploy.md`](../../deploy.md) |

---

## 附：本示例与框架自带 mock 的关系

本示例默认指向**公网**测试服务（`https://httpbin.org`），所以**跑它需要能上外网**。
在框架仓库内、想离线跑的话，把地址改成本地 mock 即可：

```bash
python tests/mock_server.py            # 另开一个终端，常驻
# 然后把 .env 的 BASE_URL 改成 http://127.0.0.1:8000（端口可用
# INTERFACETESTER_HTTP_BIN_PORT 环境变量调整，默认 80）
```

> `examples/` 里的其它示例（如 `httpbin/`、`soap/`）都是**离线可跑**的：
> 它们在 CI 冒烟里用的是仓库自带的 mock。本示例刻意保持「最小 + 指向公网」，
> 因为它演示的是**你自己的项目**长什么样——真实项目本来就是指向真实服务。
