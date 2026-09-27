"""T4：SQL 造数 / 落库校验 / 回收（**必须手写 pytest 风格用例**，不能写 YAML）。

为什么是手写而不是 YAML
-----------------------
`hmake` 只识别 `request` / `testcase` 两种 step，**YAML 里写 SQL step 不会被渲染**
（见《使用教程》5.6 与 `interfacetester/make.py` 的渲染分支）。所以 SQL 用例只有两条路：
在 `_test.py` 里直接写 `Step(RunSqlRequest(...))`，或者用 `debugtalk.py` + `sqlalchemy` 自己查。
本文件演示前者（框架原生路径），并把 T3 的「唯一 ID + 前缀 + 成对回收」照搬过来。

怎么跑
------
    pip install -e ".[sql]"        # sqlalchemy + pymysql（缺了本文件会被 skip，不会报错）
    hrun examples/data_management/sql/04_sql_data_management_test.py

默认用 **sqlite**（`sqlite:///./dm_demo.sqlite3`，用 `WithDbEngine` 注入），所以**不需要 MySQL**；
要连真实 MySQL，把 `DM_SQL_URI` 指过去并按 `schema.sql` 建表即可：

    set DM_SQL_URI=mysql+pymysql://user:pass@127.0.0.1:3306/dm_demo?charset=utf8mb4

注意（能力清单 5.2 的记录）：框架**自建**连接时 URI 是写死的 `mysql+pymysql://...`；
只有「自己注入 engine」这一条路能换库类型 —— 这正是本文件用 `with_db_engine()` 的原因。
"""

import os
import sys
import time
import uuid

import pytest

# NOTICE: 缺 sql extra 时**主动 skip**：框架本身的 `ensure_sql_ready()` 是抛异常/报错，
# 直接跑会得到一个「看起来像功能坏了」的 error，而不是「环境没装依赖」的 skip。
pytest.importorskip("sqlalchemy", reason='SQL 示例需要 sql extra：pip install -e ".[sql]"')

from interfacetester import Config, InterfaceTester, RunSqlRequest, Step  # noqa: E402
from interfacetester.database.engine import DBEngine  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_URI = os.environ.get(
    "DM_SQL_URI",
    "sqlite:///" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "dm_demo.sqlite3"),
)
TABLE = "dm_records"

# T3 约定的三个「数据标识」（与 YAML 用例保持同一套口径，见 ../debugtalk.py）
RUN_PREFIX = os.environ.get("DM_RUN_PREFIX") or f"dm-norun-{uuid.uuid4().hex[:8]}"
CASE_PREFIX = os.environ.get("DM_CASE_PREFIX") or f"{RUN_PREFIX}-sql-manual"
BIZ_ID = f"sql-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


class TestCaseSqlDataManagement(InterfaceTester):
    """造数 → 校验落库 → 回收（只删自己造的那一行）。"""

    config = Config("04 SQL 数据管理：造数 / 落库校验 / 回收（sqlite 演示）")

    teststeps = [
        Step(
            RunSqlRequest("建表（幂等）").insert(
                f"CREATE TABLE IF NOT EXISTS {TABLE} ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "biz_id VARCHAR(64), run_prefix VARCHAR(64), case_prefix VARCHAR(128), "
                "name VARCHAR(128), amount INTEGER)"
            )
        ),
        Step(
            RunSqlRequest("造数：插入一行（biz_id 唯一 + 前缀隔离）").insert(
                f"INSERT INTO {TABLE} (biz_id, run_prefix, case_prefix, name, amount) "
                f"VALUES ('{BIZ_ID}', '{RUN_PREFIX}', '{CASE_PREFIX}', 'alpha', 100)"
            )
        ),
        Step(
            RunSqlRequest("校验：这一行确实落库、且金额正确")
            .fetchall(f"SELECT biz_id, amount, case_prefix FROM {TABLE} WHERE biz_id = '{BIZ_ID}'")
            .extract()
            .with_jmespath("[0].biz_id", "checked_biz_id")
            .validate()
            # NOTICE: SQL 结果的断言路径是相对**结果集**的（[0] 是第一行），
            # 与 HTTP step 的 body.xxx 不同；`@` 表示整个结果集。
            .assert_length_equal("[0]", 3)
            .assert_equal("[0].biz_id", BIZ_ID)
            .assert_equal("[0].amount", 100)
            .assert_equal("[0].case_prefix", CASE_PREFIX)
        ),
        Step(
            RunSqlRequest("回收：只删自己造的那一行（按 biz_id 精确回收）").delete(
                f"DELETE FROM {TABLE} WHERE biz_id = '{BIZ_ID}'"
            )
        ),
        Step(
            RunSqlRequest("回收后的确认：应当查不到了")
            .fetchall(f"SELECT biz_id FROM {TABLE} WHERE biz_id = '{BIZ_ID}'")
            .validate()
            .assert_length_equal("@", 0)
        ),
    ]

    def test_start(self):
        # 注入自己的 engine：这样既能用 sqlite（离线可跑），也演示了「换库类型」的唯一途径
        self.with_db_engine(DBEngine(db_uri=DB_URI))
        super().test_start()
