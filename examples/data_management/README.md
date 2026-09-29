# 测试数据管理约定（T1~T5 示例工程）

> **一句话**：这是一份**可运行**的「测试数据管理」约定 —— 数据从哪来（T3）、用例之间怎么隔离（T3）、
> 跑完谁清理（T2）、环境怎么切（T5）、数据库怎么造数校验（T4）。
> 全部**离线可跑**（本地有状态 mock + sqlite），不依赖 MySQL、不依赖外网。

```
# 一分钟跑通（在仓库根执行）
python -m interfacetester run examples/data_management
# 期望：4 passed, 1 skipped（skip 的是 SQL 示例，见 T4）
```

| 文件 | 对应 | 作用 |
| --- | --- | --- |
| `conftest.py` | **T1 + T2** | 会话级准备（起数据服务 + 登录写 `os.environ`）；用例级隔离与回收；危险操作/缺依赖的跳过规则 |
| `debugtalk.py` | **T3** | 唯一 ID、运行/用例前缀、**造数与回收成对登记**、清理失败只告警 |
| `data_store.py` | 支撑 | 有状态本地「数据服务」（扮演真实项目里的数据库/被测服务），让约定能离线演示 |
| `01_create_and_read.yml` | T3 | 造数 → 回读 → 断言；顺带验证鉴权链路 |
| `02_isolation_check.yml` | T2 + T3 | **同一用例跑两轮**，第二轮开跑前库里必须为空 → 证明用例级回收生效 |
| `03_dangerous_ops.yml` | T2 | 危险操作（全库清空）的闸门；生产环境会被 conftest 直接跳过 |
| `sql/04_sql_data_management_test.py` | **T4** | SQL 造数/落库校验/回收（**手写 pytest 用例**，见 T4 说明） |
| `sql/schema.sql` | T4 | MySQL DDL（真连 MySQL 时用；默认走 sqlite） |
| `.env.test` / `.env.prod` | **T5** | 环境矩阵模板；`cp .env.test .env` 即可用（框架只认 `.env`） |

---

## 一、三条硬约束（决定「只能这么写」，都有实测依据）

| # | 约束 | 依据 | 带来的写法 |
| --- | --- | --- | --- |
| 1 | **生成的用例方法签名固定**：`def test_start(self)` / `(self, param)` | `make.py` 模板 | 普通 fixture **注入不进用例**，只有 `autouse=True` 的夹具才生效 —— 所以本工程所有夹具都是 autouse |
| 2 | **autouse 夹具不能给用例传值**（用例不接受参数） | 同上 | 传值只能：夹具写 `os.environ` → `debugtalk.py` 函数读（本工程做法）；或 YAML 里直接 `${func()}` |
| 3 | **用例执行顺序不保证** | `make.main_make` 结尾返回 `list(pytest_files_run_set)`（**set**），`hrun` 把它的顺序交给 pytest。实测：CLI 传 `01.yml 02.yml`，pytest 先跑 `02` | 任何「依赖上一条用例留下的状态」的断言都不可靠 → 隔离必须靠**唯一前缀**，验收要靠**自包含**的用例（本例用 `parameters` 让同一用例跑两轮） |

> 附加约束（P2-a 实测踩到，写 YAML 时必看）：
> **`request.headers` 在 load 期就被要求是 dict**，所以不能写 `headers: ${auth_header()}`（拿一个 dict 替换整个字段）
> → 会报 `TestCaseFormatError: Input should be a valid dictionary`。
> 正确写法是**逐字段**注入：`config.variables: {token: "${ENV(DM_TOKEN)}"}` + `headers: {Authorization: "Bearer $token"}`。

---

## 二、数据标识口径（T3 的核心）

一条测试数据要同时带三个标识，缺一不可：

| 标识 | 例子 | 解决什么 |
| --- | --- | --- |
| **运行前缀** `DM_RUN_PREFIX` | `dm-test-53eaec32` | 「这次运行造的全部数据」——并行、多次运行、多人共用环境时区分批次 |
| **用例前缀** `DM_CASE_PREFIX` | `dm-test-53eaec32-test_start-9f2c11` | 「这一条用例造的数据」——**按用例回收**的抓手 |
| **唯一 ID** `unique_id()` | `id-1758112233445-a1b2c3` | 时间戳+随机，保证不撞车（但**只靠它无法回收**，必须配前缀） |

数据名字统一形如 `<用例前缀>-<业务后缀>-<随机串>`，因此：
**查询按前缀、回收按前缀**，永远不用「清空全库」这种危险操作（见 T2 闸门）。

**造数与回收成对发生**（结构保证，而不是靠人记得写 teardown）：

```yaml
variables:
    suffix: alpha
    record_id: "${create_record($suffix)}"   # 造数 → 内部同时把回收动作登记进 _CLEANUPS
```

`create_record()` 返回 id 的同时 `_CLEANUPS.append((描述, 回收函数))`；
`conftest.py` 的用例级夹具在用例结束后调用 `run_registered_cleanups()`：
**失败的只打印告警、绝不让用例判失败**，并再按前缀兜底扫一遍（防止有人绕过 debugtalk 直接发请求造数）。

---

## 三、环境矩阵（T5）

| 约定 | 做法 |
| --- | --- |
| 环境命名 | `.env`（本地实际使用，**不入库**）+ `.env.test` / `.env.prod`（模板，入库） |
| 切换方式 | `cp .env.test .env`；CI 里直接把值注入环境变量 |
| 用例命名 | `config.name` 前缀反映环境（如 `[prod] 订单查询`），报告里一眼能看出跑的是哪套环境 |
| 危险操作 | `DM_ENV_NAME=prod` → conftest **强制**把闸门设为 `off`，并跳过危险操作用例 |
| 报告/日志 | `--html/--junitxml` 落到 `reports/`（已 gitignore），用例细节在 `logs/{case_id}.run.log` |

### ⚠️ 两条必须知道的实测结论

1. **框架只加载 `<RootDir>/.env`**（`loader.load_dot_env_file`，`loader.py:582`），
   **没有** `.env.test` / `.env.prod` 的自动选择机制 —— 所以「环境矩阵」= 模板文件 + 手工/CI 切换。
2. **`.env` 里的值会覆盖同名环境变量**（实测）：

   ```
   shell: DM_ENV_NAME=from-shell  项目 .env: DM_ENV_NAME=from-env-file
   用例实际看到：env_name=from-env-file      ← .env 赢
   ```

   原因：`load_dot_env_file()` 会把值 `set_os_environ()`（直接写 `os.environ`）。
   → **CI 注入的变量会被项目里的 `.env` 覆盖**；如果 CI 上「注入不生效」，先查这里。
   反过来，**夹具里的赋值晚于 `.env` 加载，所以夹具能覆盖 `.env`**（本工程 `DM_BASE_URL` 就是这么定的）。
   实操建议：`.env` 只放**非机密的本地默认值**，机密与地址用 CI secret 注入并**不要**在 `.env` 里写同名键。

---

## 四、三个演示实验（都可离线复现）

### 实验 1：数据污染 → 自动回收（T2 的验收）

```bash
# 正常：同一用例跑两轮，第二轮开跑前库里是空的 → 4 passed, 1 skipped
python -m interfacetester run examples/data_management

# 关掉用例级回收：第二轮的「开跑前应当为空」直接看到第一轮留下的 1 条 → failed
set DM_DISABLE_CLEANUP=1        # Linux/macOS: export DM_DISABLE_CLEANUP=1
python -m interfacetester run examples/data_management/02_isolation_check.yml
```

实测输出（节选）：

```
assert body.count equal 0(int)  ==> pass     # 第一轮：[param0]
assert body.count equal 1(int)  ==> pass     # 第一轮：造数后
assert body.count equal 0(int)  ==> fail     # 第二轮：[param1]
check_value: 1(int)                          #  ← 第一轮留下的那条数据
1 failed, 1 passed
```

### 实验 2：生产环境的危险操作闸门（T2）

```bash
set DM_ENV_NAME=prod
python -m interfacetester run examples/data_management -rs
# 实测：3 passed, 2 skipped
#   SKIPPED 危险操作用例：当前环境 env=prod、DM_ALLOW_DANGEROUS_OPS=off（生产环境强制跳过）
#   SKIPPED SQL 示例需要 sql extra
```

即使有人硬编码去调 `reset_store()`，服务端也会拒绝（实测）：

```
env=test gate=on  → {'deleted': 0, 'refused': False}
env=prod gate=off → {'deleted': 0, 'refused': True,
                     'detail': {'error': 'refusing to wipe the whole store',
                                'hint': '带 ?prefix= 只删自己造的数据；确实要清空请加 X-Allow-Dangerous: on'}}
```

### 实验 3：SQL 示例（T4）

```bash
pip install -e ".[sql]"          # sqlalchemy + pymysql
python -m interfacetester run examples/data_management/sql/04_sql_data_management_test.py
```

未安装 `sql` extra 时**是 skip 而不是报错**（实测 `SKIPPED ... SQL 示例需要 sql extra`）——
框架本身的 `ensure_sql_ready()` 是抛异常，直接跑会得到一个「看起来像功能坏了」的 error。

---

## 五、T4：SQL 造数/校验，为什么是手写用例

> **YAML 不支持 SQL step**：`hmake` 只识别 `request` / `testcase` 两种 step，
> 在 YAML 里写 `sql_request:` 不会被渲染（见《使用教程》5.6 与 `interfacetester/make.py`）。
> 所以 SQL 只能写在 `_test.py` 里（本工程的 `sql/04_..._test.py`）。

`sql/04_sql_data_management_test.py` 演示了完整闭环：**建表（幂等）→ 造数 → 校验落库 → 按 biz_id 回收 → 确认已删**，
并示范两件容易踩的事：

| 坑 | 说明 |
| --- | --- |
| 断言路径是**结果集**相对路径 | `assert_equal("[0].amount", 100)`；`@` 表示整个结果集（`assert_length_equal("@", 0)`） |
| 换数据库类型只能靠 `with_db_engine()` | 框架自建连接时 URI 写死 `mysql+pymysql://...`（`step_sql_request.py:111-115`）→ 想用 sqlite/PostgreSQL 必须自己注入 `DBEngine`（本工程就是这么做的，见《能力清单》5.2） |

---

## 六、搬到自己项目时的落地清单

1. 项目根放 `conftest.py`（含 `debugtalk.py` 的那层），夹具一律 `autouse=True`；
2. 需要传给用例的值 → 夹具写 `os.environ`，`debugtalk.py` 里提供同名函数读它；
3. 造数函数**必须同时登记回收**（照抄 `debugtalk.create_record` 的写法）；
4. 数据名字带 `<运行前缀>-<用例前缀>-<后缀>`，查询/回收都按前缀；
5. 危险操作（清库、删库、改共享配置）加**显式开关 + 生产环境强制关闭 + 用例级 skip**；
6. `.env` 只放本地默认值；机密走 CI secret，并注意 `.env` 会覆盖同名环境变量；
7. **别依赖用例执行顺序**（顺序不保证），隔离与验收都要自包含；
8. `headers` 逐字段注入，不要用函数返回值替换整个字段。

---

## 七、已知限制

- 本工程的「数据服务」是内存 mock（重启即清空），**不能**用来验证真实数据库的行为；
- SQL 示例默认走 sqlite，与 MySQL 的差异（类型、事务、`AUTO_INCREMENT`）未覆盖；
- `DM_DISABLE_CLEANUP` 是**刻意的演示开关**，真实项目里不该存在这种「关掉清理」的入口；
- 用例顺序不保证（见约束 3）：如果你需要固定顺序，请显式写成一个用例的多条 step，或用 pytest 的
  `--deselect`/文件级拆分自行编排。
