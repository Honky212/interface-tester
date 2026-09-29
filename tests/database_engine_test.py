"""批次 8-4（0918-8）：`database/engine.py` 的 SQL 语义修复。

覆盖：
- **M26**：语句分类不能再靠 SQL 前 6 个字符（`WITH ... SELECT` 丢结果、`REPLACE INTO`
  不提交、前导注释同样被吞）→ 改用 `result.returns_rows`；
- **M27**：`value_decode` 只接受「解析结果是 dict/list」的字符串，普通字符串列不再被静默改类型；
- **L23**：`fetchmany` 形状固定为行列表，`size < 1` 直接报错。

NOTICE: 本机（以及绝大多数 CI）没有 sqlalchemy/pymysql，而 `engine.py` 顶部就
`from sqlalchemy import ...`。这里按 `step_thrift_request_test.py::_import_data_convertor`
同款做法，**按需注入替身**；构造 `DBEngine` 时走 `__new__` 绕过 `__init__`，因此不会真的去连数据库。

NOTICE: 本文件有**两层**替身，覆盖的重点不同：
1. `_FakeSession` / `_FakeResult`（脚本化结果）：验证**分支与形状**（返回 list 还是 dict、
   哪个分支提交、`size` 校验……），完全确定、不碰磁盘；
2. `TestSqlAgainstRealDatabase`（真 `sqlite3` 连接）：验证**事务语义**——
   「提交了没有」用**另一个连接**能不能看见来判定，这比在同一个 session 里自查更硬。
   这一层只有 `returns_rows` 来自替身，且用的是与 SQLAlchemy **同源**的
   `cursor.description is not None`（见 `_SqliteBackedResult`）。
"""

import base64
import datetime
import decimal
import json
import os
import shutil
import sqlite3
import sys
import types
import unittest
import uuid

from interfacetester.builtin.comparators import equal
from interfacetester.exceptions import ParamsError

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 临时库文件放 logs/ 下（已被 .gitignore 覆盖；不用 mkdtemp 的 0o700，受限环境下不可用）
TMP_DB_DIR = os.path.join(REPO, "logs")


class _FakeResult(object):
    """替身 SQLAlchemy Result：只需要 `returns_rows` / `rowcount` / fetch* 三件套。"""

    def __init__(self, rows=None, rowcount=-1, returns_rows=True):
        self._rows = [dict(row) for row in (rows or [])]
        self.rowcount = rowcount
        self.returns_rows = returns_rows
        self.fetch_calls = []

    @staticmethod
    def _wrap(row):
        return _FakeRow(row)

    def fetchall(self):
        self.fetch_calls.append(("fetchall", None))
        return [self._wrap(row) for row in self._rows]

    def fetchone(self):
        self.fetch_calls.append(("fetchone", None))
        if not self._rows:
            return None
        return self._wrap(self._rows[0])

    def fetchmany(self, size):
        self.fetch_calls.append(("fetchmany", size))
        return [self._wrap(row) for row in self._rows[:size]]


class _FakeRow(object):
    """替身 Row：`dict(el._mapping)` 是 engine 里唯一的用法。"""

    def __init__(self, mapping):
        self._mapping = mapping


class _FakeSession(object):
    def __init__(self, results):
        # results: 依次返回的 _FakeResult / 异常实例
        self._results = list(results)
        self.executed = []
        self.commit_count = 0
        self.close_count = 0

    def execute(self, statement):
        self.executed.append(statement)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def commit(self):
        self.commit_count += 1

    def close(self):
        self.close_count += 1


def _install_fake_sqlalchemy():
    """注入替身 sqlalchemy（仅在未安装时；`engine.py` 顶层 import 需要它）。

    返回被注入的模块名列表，导入完成后由调用方移除——避免整个测试会话里
    一直存在一个假的 sqlalchemy（那会让别的用例误判「依赖已安装」）。
    """
    if "sqlalchemy" in sys.modules:
        return []

    sqlalchemy_module = types.ModuleType("sqlalchemy")
    sqlalchemy_module.create_engine = lambda db_uri, **kwargs: _FakeEngine(db_uri)
    # text() 只做「把 SQL 串包起来」这一件事，替身 session 里不解析
    sqlalchemy_module.text = lambda query: _TextQuery(query)
    orm_module = types.ModuleType("sqlalchemy.orm")
    orm_module.sessionmaker = lambda bind: (lambda: bind)
    sqlalchemy_module.orm = orm_module

    sys.modules["sqlalchemy"] = sqlalchemy_module
    sys.modules["sqlalchemy.orm"] = orm_module
    return ["sqlalchemy", "sqlalchemy.orm"]


class _FakeEngine(object):
    def __init__(self, db_uri):
        self.db_uri = db_uri
        self.dispose_count = 0

    def dispose(self):
        self.dispose_count += 1


class _TextQuery(object):
    def __init__(self, query):
        self.query = query

    def __str__(self):
        return self.query


_INJECTED_SQLALCHEMY_MODULES = _install_fake_sqlalchemy()

try:
    from interfacetester.database.engine import DBEngine, format_mysql_time
finally:
    # 替身只服务于本次导入：engine.py 已经把 text/create_engine 绑进自己的命名空间，
    # 之后不再需要假的 sqlalchemy 存在于整个会话里。
    for _module_name in _INJECTED_SQLALCHEMY_MODULES:
        sys.modules.pop(_module_name, None)


def _make_engine(results):
    """构造一个绕过 __init__ 的 DBEngine（不建真连接），session 为替身。"""
    db_engine = DBEngine.__new__(DBEngine)
    db_engine.engine = _FakeEngine("sqlite://")
    db_engine.session = _FakeSession(results)
    return db_engine


class TestSqlStatementClassification(unittest.TestCase):
    """M26：分类靠 `result.returns_rows`，而不是 SQL 的前 6 个字符。"""

    def test_cte_select_returns_rows(self):
        """`WITH ... SELECT` 必须返回结果集（修复前 `query.upper()[:6] == "WITH T"` → None）。"""
        db_engine = _make_engine([_FakeResult(rows=[{"n": 1}])])

        rows = db_engine.fetchall(
            "WITH t AS (SELECT 1 AS n) SELECT * FROM t"
        )

        self.assertEqual(rows, [{"n": 1}])

    def test_select_with_leading_comment_returns_rows(self):
        """前导注释（`/* c */ SELECT`）同样不能被当成非查询语句。"""
        db_engine = _make_engine([_FakeResult(rows=[{"n": 1}])])

        rows = db_engine.fetchall("/* batch 8-4 */ SELECT 1 AS n")

        self.assertEqual(rows, [{"n": 1}])

    def test_replace_into_is_committed(self):
        """`REPLACE INTO` 必须落库：修复前既没有 commit，返回的也是 None。"""
        db_engine = _make_engine([_FakeResult(rows=None, rowcount=1, returns_rows=False)])

        result = db_engine.insert("REPLACE INTO t_user (id, name) VALUES (1, 'alice')")

        self.assertEqual(result, {"rowcount": 1})
        self.assertEqual(db_engine.session.commit_count, 1)

    def test_ddl_is_committed(self):
        """`TRUNCATE` / `CREATE` 这类 DDL 修复前完全不提交（会话一归还就丢）。"""
        for statement in (
            "TRUNCATE TABLE t_user",
            "CREATE TABLE t_tmp (id INT)",
            "SET SESSION sql_mode = ''",
        ):
            with self.subTest(statement=statement):
                db_engine = _make_engine(
                    [_FakeResult(rows=None, rowcount=-1, returns_rows=False)]
                )

                db_engine.fetchall(statement)

                self.assertEqual(db_engine.session.commit_count, 1)

    def test_dml_without_result_set_keeps_rowcount_shape(self):
        """回归：普通 UPDATE/DELETE/INSERT 的返回形状必须不变。"""
        db_engine = _make_engine(
            [
                _FakeResult(rows=None, rowcount=3, returns_rows=False),
                _FakeResult(rows=None, rowcount=1, returns_rows=False),
                _FakeResult(rows=None, rowcount=2, returns_rows=False),
            ]
        )

        self.assertEqual(db_engine.update("UPDATE t SET a = 1"), {"rowcount": 3})
        self.assertEqual(db_engine.delete("DELETE FROM t WHERE a = 1"), {"rowcount": 1})
        self.assertEqual(db_engine.insert("INSERT INTO t VALUES (1)"), {"rowcount": 2})

    def test_dml_with_returning_returns_rows_and_commits(self):
        """`INSERT ... RETURNING`（PG）/ `OUTPUT`（SQL Server）既不能丢行也不能不提交。"""
        db_engine = _make_engine(
            [_FakeResult(rows=[{"id": 7}], rowcount=1, returns_rows=True)]
        )

        result = db_engine.insert("INSERT INTO t (name) VALUES ('a') RETURNING id")

        self.assertEqual(result, {"rowcount": 1, "rows": [{"id": 7}]})
        self.assertEqual(db_engine.session.commit_count, 1)

    def test_commit_false_never_commits(self):
        """`commit=False` 的既有语义必须保留（调用方自己控制事务）。"""
        db_engine = _make_engine([_FakeResult(rows=None, rowcount=1, returns_rows=False)])

        db_engine.insert("INSERT INTO t VALUES (1)", commit=False)

        self.assertEqual(db_engine.session.commit_count, 0)


class TestBatch0919_24RowsetReturningWriteCommits(unittest.TestCase):
    r"""批次 6 / **M5**：带结果集的写语句走 `fetch*` 也**必须提交**。

    ## 现场（修复前，控制流实测）

    ```text
    fetchall("INSERT INTO t (name) VALUES ('a') RETURNING id")
      -> 返回 [{'id': 1}]          ✅ 拿到了 id、断言能过
      -> commit 次数 = 0            ❌ 从未提交
      -> 另一个连接看到 0 行         （session.close() 时回滚）
    同一语句改用 insert()  -> commit 次数 = 1   ✅
    ```

    更糟的是"有时丢、有时不丢"：同一用例后面任何一条 DML 会把它**顺带提交**。

    ## 根因

    `_fetch` 的提交挂在 `if dml:` 上，而只有 `insert()/update()/delete()` 传 `dml=True`
    —— `fetchone/fetchmany/fetchall` 全是 `dml=False`。于是 PostgreSQL 的
    `INSERT … RETURNING`、SQL Server 的 `OUTPUT`、MySQL 的 `CALL` 返回行集
    这些**最自然的写法**全都漏过了提交。

    ## 修法

    口径统一成一句话：**`commit=True`（默认）就提交**（与非行集分支一致），
    `dml` 只决定**返回形状**（`{"rowcount", "rows"}` vs 行列表）。
    纯 `SELECT` 也会因此结束事务 —— 这是**有意接受**的（读事务提交无害、还释放读锁），
    要手工管事务就用 `commit=False`。
    """

    def test_fetchall_rowset_returning_write_commits(self):
        db_engine = _make_engine(
            [_FakeResult(rows=[{"id": 1}], rowcount=1, returns_rows=True)]
        )

        rows = db_engine.fetchall("INSERT INTO t (name) VALUES ('a') RETURNING id")

        self.assertEqual(rows, [{"id": 1}], "返回行不能丢")
        self.assertEqual(
            db_engine.session.commit_count,
            1,
            "带结果集的写语句没有提交 —— 数据会在 session.close() 时回滚（M5）",
        )

    def test_fetchone_rowset_returning_write_commits(self):
        db_engine = _make_engine(
            [_FakeResult(rows=[{"id": 2}], rowcount=1, returns_rows=True)]
        )

        row = db_engine.fetchone("INSERT INTO t (name) VALUES ('b') RETURNING id")

        self.assertEqual(row, {"id": 2})
        self.assertEqual(db_engine.session.commit_count, 1)

    def test_plain_select_uses_the_same_rule(self):
        """口径统一后的**有意后果**：纯读也提交（不再是"读不提交、写才提交"两套规则）。"""
        db_engine = _make_engine([_FakeResult(rows=[{"id": 3}], rowcount=1)])

        db_engine.fetchall("SELECT id FROM t")

        self.assertEqual(db_engine.session.commit_count, 1)

    def test_commit_false_still_never_commits_for_rowset_paths(self):
        """反向护栏：`commit=False` 在**行集路径**上同样不许提交（手工事务的出口）。"""
        db_engine = _make_engine(
            [_FakeResult(rows=[{"id": 4}], rowcount=1, returns_rows=True)]
        )

        db_engine.fetchall(
            "INSERT INTO t (name) VALUES ('c') RETURNING id", commit=False
        )

        self.assertEqual(db_engine.session.commit_count, 0)

    def test_unknown_statement_does_not_silently_return_none(self):
        """关键回归：任何语句都必须有明确结果，不能再出现「隐式 return None」。"""
        statements = [
            ("WITH t AS (SELECT 1) SELECT * FROM t", _FakeResult(rows=[{"a": 1}])),
            ("REPLACE INTO t VALUES (1)", _FakeResult(rowcount=1, returns_rows=False)),
            ("EXPLAIN SELECT 1", _FakeResult(rows=[{"plan": "x"}])),
        ]
        for statement, result in statements:
            with self.subTest(statement=statement):
                db_engine = _make_engine([result])

                self.assertIsNotNone(db_engine.fetchall(statement))


class TestValueDecodeTimeAndDecimal(unittest.TestCase):
    """0919-11 / §三.6a：MySQL 的 `TIME` / `DECIMAL` 列不能再以原生对象流出去。

    pymysql 的字段映射：`TIME` → `datetime.timedelta`、`DECIMAL`/`NUMERIC` →
    `decimal.Decimal`。修复前 `value_decode` 不认这两种（也不认 `datetime.time`），
    于是：
      ① 报告里静默降级成 `repr`（`"datetime.timedelta(seconds=3661)"` / `"Decimal('1.10')"`）；
      ② 提取出来的值再进后续请求的 JSON body → `TypeError: ... is not JSON serializable`；
      ③ 用**字符串字面量**断言必然失败（值是对的、断言却红）。

    口径：**字符串化**（与既有的 `datetime`/`date` 一致）。
    """

    def test_mysql_time_becomes_mysql_style_string(self):
        """`TIME` 的三个边界都要对：负数、超过 24 小时、带微秒。

        刻意**不**用 `str(timedelta)`——实测 `str(timedelta(hours=25))` 是
        `'1 day, 1:00:00'`（不可断言），而 MySQL 与 DB 客户端显示 `'25:00:00'`。
        """
        cases = [
            (datetime.timedelta(seconds=3661), "01:01:01"),
            (datetime.timedelta(0), "00:00:00"),
            (datetime.timedelta(seconds=-3661), "-01:01:01"),
            (datetime.timedelta(hours=25), "25:00:00"),
            (datetime.timedelta(seconds=1, microseconds=500000), "00:00:01.500000"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=str(raw)):
                row = {"t": raw}
                DBEngine.value_decode(row)
                self.assertEqual(row["t"], expected)
                self.assertIsInstance(row["t"], str)

    def test_python_time_becomes_string(self):
        row = {"t": datetime.time(13, 45, 6)}

        DBEngine.value_decode(row)

        self.assertEqual(row["t"], "13:45:06")

    def test_decimal_keeps_its_scale(self):
        """`str(Decimal)` 保留标度：`Decimal('1.10')` → `'1.10'`（不是 `'1.1'`），
        与 DB 里 `DECIMAL(10,2)` 显示的一致，断言可以直接抄。"""
        row = {"price": decimal.Decimal("1.10"), "whole": decimal.Decimal("5.00")}

        DBEngine.value_decode(row)

        self.assertEqual(row, {"price": "1.10", "whole": "5.00"})

    def test_decoded_values_are_json_serializable(self):
        """后果②的回归：修复前 `json.dumps` 会抛 TypeError。"""
        row = {
            "t": datetime.timedelta(seconds=1),
            "price": decimal.Decimal("9.99"),
        }

        DBEngine.value_decode(row)

        self.assertEqual(json.dumps(row), '{"t": "00:00:01", "price": "9.99"}')

    def test_string_literal_assertion_works(self):
        """后果③的回归：DB 客户端里看到的就是 `'1.10'`，断言应当能用它。"""
        row = {"price": decimal.Decimal("1.10")}
        DBEngine.value_decode(row)

        equal(row["price"], "1.10")  # 不抛 AssertionError 即通过

    def test_exact_float_literal_no_longer_matches(self):
        """⚠️ **口径变更的代价**，刻意钉住它，避免以后被当成「bug 修回去」。

        修复前 `Decimal('0.5') == 0.5` 恰好为真（0.5 可被二进制精确表示），
        字符串化之后这类**精确可表示的浮点字面量**会由「通过」变「失败」。
        仍然选字符串化的理由：金额/小数列本就该按精确值比较
        （`0.1`、`9.99` 这类浮点字面量**修复前后都是失败**），
        且失败信息里会直接显示 `'1.10' vs 1.1`，一眼能看出该加引号。
        若要改成与浮点字面量兼容，把 `str(v)` 换成 `float(v)` 即可（会丢精度/标度）。
        """
        row = {"amount": decimal.Decimal("0.5")}
        DBEngine.value_decode(row)

        self.assertEqual(row["amount"], "0.5")
        with self.assertRaises(AssertionError):
            equal(row["amount"], 0.5)

    def test_m27_boundaries_are_not_regressed(self):
        """M27 的口径不能被本批改动带偏：普通字符串照旧、JSON 字符串才转对象。"""
        row = {"order_no": "123", "price": "1.10", "extra": '{"a": 1}'}

        DBEngine.value_decode(row)

        self.assertEqual(row, {"order_no": "123", "price": "1.10", "extra": {"a": 1}})


class TestFormatMysqlTimeHelper(unittest.TestCase):
    """`format_mysql_time` 的独立用例（它是 §三.6a 的口径落点）。"""

    def test_matches_mysql_shape_not_python_str(self):
        # 非空跑自检：这个 helper 存在的意义就是「不等于 str(timedelta)」
        self.assertNotEqual(
            format_mysql_time(datetime.timedelta(seconds=3661)),
            str(datetime.timedelta(seconds=3661)),
        )
        self.assertEqual(format_mysql_time(datetime.timedelta(hours=25)), "25:00:00")
        self.assertIn("day", str(datetime.timedelta(hours=25)))

    def test_hours_are_not_wrapped_at_24(self):
        """MySQL `TIME` 上限是 `838:59:59`，小时不能按 24 取模。"""
        self.assertEqual(
            format_mysql_time(datetime.timedelta(hours=838, minutes=59, seconds=59)),
            "838:59:59",
        )

    def test_microseconds_only_when_present(self):
        self.assertEqual(format_mysql_time(datetime.timedelta(seconds=2)), "00:00:02")
        self.assertEqual(
            format_mysql_time(datetime.timedelta(seconds=2, microseconds=1)),
            "00:00:02.000001",
        )


class TestValueDecodeBlobColumns(unittest.TestCase):
    """0919-15 / 登记项③：`BLOB` / `BINARY` 列（`bytes`）的口径。

    `BLOB` 与 `TIME` / `DECIMAL` 是**同一类**问题——原生对象进报告 / 断言 / JSON：
    实测 `json.dumps({'blob': bytes})` → `TypeError`，报告侧靠 `ExtendJSONEncoder` 兜底成 repr。

    口径**与 thrift 的 `binary` 字段完全一致**（同一个 `utils.json_safe_bytes`）：
    能按 UTF-8 解码 → 文本；不能 → base64（无损）。
    """

    NON_UTF8 = b"\xff\xfe\x00binary"

    def test_blob_is_json_serializable(self):
        row = {"payload": self.NON_UTF8}

        DBEngine.value_decode(row)

        self.assertIsInstance(row["payload"], str)
        # 修复前：TypeError: Object of type bytes is not JSON serializable
        json.dumps(row)

    def test_utf8_decodable_blob_becomes_text(self):
        row = {"payload": "中文".encode("utf-8")}

        DBEngine.value_decode(row)

        self.assertEqual(row["payload"], "中文")

    def test_non_utf8_blob_becomes_base64_and_is_lossless(self):
        row = {"payload": self.NON_UTF8}

        DBEngine.value_decode(row)

        self.assertEqual(
            row["payload"], base64.b64encode(self.NON_UTF8).decode("ascii")
        )
        self.assertEqual(base64.b64decode(row["payload"]), self.NON_UTF8)

    def test_bytearray_is_handled_too(self):
        row = {"payload": bytearray(self.NON_UTF8)}

        DBEngine.value_decode(row)

        self.assertEqual(
            row["payload"], base64.b64encode(self.NON_UTF8).decode("ascii")
        )

    def test_blob_assertion_can_use_the_string_form(self):
        """下游可用：断言可以直接用 base64 文本（与 thrift binary 的用法一致）。"""
        row = {"payload": self.NON_UTF8}
        DBEngine.value_decode(row)

        equal(row["payload"], base64.b64encode(self.NON_UTF8).decode("ascii"))

    def test_text_column_that_looks_like_base64_is_not_re_encoded(self):
        """反向护栏：**字符串**列不会被当成 bytes 再编码一遍（M27 边界不受影响）。"""
        row = {"note": "QUJD"}

        DBEngine.value_decode(row)

        self.assertEqual(row["note"], "QUJD")


class TestBytesHelperIsSharedWithThrift(unittest.TestCase):
    """闸门：bytes 的 JSON 口径**只有一份实现**（数据库与 thrift 共用）。

    收口的理由：`data_convertor` 顶部要 `from thrift.Thrift import TType`，
    让 DB 路径去 import 它会平白拖进 Apache thrift 依赖，所以实现放在 `utils`。
    这里断言「本模块用的就是 `utils` 里那一个」——两侧各钉一次，防止将来有人
    在某一侧另写一份（口径漂移是本仓反复吃过亏的地方）。
    """

    def test_engine_uses_the_shared_helper(self):
        from interfacetester import utils
        from interfacetester.database import engine as engine_module

        self.assertIs(engine_module.json_safe_bytes, utils.json_safe_bytes)


class TestValueDecode(unittest.TestCase):
    """M27：`value_decode` 不能把普通字符串列静默改成别的类型。"""

    def test_numeric_string_stays_string(self):
        row = {"order_no": "123"}

        DBEngine.value_decode(row)

        self.assertEqual(row, {"order_no": "123"})
        self.assertIsInstance(row["order_no"], str)

    def test_decimal_like_string_is_not_reformatted(self):
        """`"1.10"` 修复前会变成 float 1.1，于是 `"1.10" == "1.1"` 反而判等。"""
        row = {"price": "1.10"}

        DBEngine.value_decode(row)

        self.assertEqual(row["price"], "1.10")

    def test_literal_strings_stay_strings(self):
        row = {"a": "null", "b": "true", "c": "false"}

        DBEngine.value_decode(row)

        self.assertEqual(row, {"a": "null", "b": "true", "c": "false"})

    def test_json_object_string_becomes_dict(self):
        row = {"extra": '{"a": 1}'}

        DBEngine.value_decode(row)

        self.assertEqual(row["extra"], {"a": 1})

    def test_json_array_string_becomes_list(self):
        row = {"tags": '["a", "b"]'}

        DBEngine.value_decode(row)

        self.assertEqual(row["tags"], ["a", "b"])

    def test_plain_text_is_untouched(self):
        row = {"name": "alice"}

        DBEngine.value_decode(row)

        self.assertEqual(row, {"name": "alice"})

    def test_datetime_and_date_are_still_formatted(self):
        """回归：日期/时间列的格式化行为不能变。"""
        row = {
            "created_at": datetime.datetime(2024, 5, 1, 13, 4, 5),
            "birthday": datetime.date(1990, 12, 31),
        }

        DBEngine.value_decode(row)

        self.assertEqual(
            row, {"created_at": "2024-05-01 13:04:05", "birthday": "1990-12-31"}
        )

    def test_value_decode_is_applied_to_query_results(self):
        """端到端（替身 session）：查询结果里的字符串列不被改类型，JSON 列才转对象。"""
        db_engine = _make_engine(
            [_FakeResult(rows=[{"order_no": "123", "extra": '{"a": 1}'}])]
        )

        rows = db_engine.fetchall("SELECT order_no, extra FROM t_order")

        self.assertEqual(rows, [{"order_no": "123", "extra": {"a": 1}}])


class TestFetchmanyShape(unittest.TestCase):
    """L23：`fetchmany` 的形状固定为行列表，`size < 1` 明确报错。"""

    def test_fetchmany_size_one_returns_list(self):
        """修复前 `size=1` 走 `fetchone()` 分支返回 **dict**，与文档的 `[0].字段` 矛盾。"""
        db_engine = _make_engine([_FakeResult(rows=[{"id": 1}, {"id": 2}])])

        rows = db_engine.fetchmany("SELECT id FROM t", 1)

        self.assertEqual(rows, [{"id": 1}])
        self.assertIsInstance(rows, list)

    def test_fetchmany_size_n_returns_list(self):
        db_engine = _make_engine([_FakeResult(rows=[{"id": 1}, {"id": 2}, {"id": 3}])])

        rows = db_engine.fetchmany("SELECT id FROM t", 2)

        self.assertEqual(rows, [{"id": 1}, {"id": 2}])

    def test_fetchmany_size_zero_is_rejected(self):
        """YAML 里 `method: fetchmany` 忘写 `size` 时模型默认是 0——必须报错而不是静默 None。"""
        db_engine = _make_engine([_FakeResult(rows=[{"id": 1}])])

        with self.assertRaises(ParamsError) as ctx:
            db_engine.fetchmany("SELECT id FROM t", 0)

        message = str(ctx.exception)
        self.assertIn("size", message)
        self.assertIn("fetchone", message)
        # 参数不合法时不允许偷偷执行 SQL
        self.assertEqual(db_engine.session.executed, [])

    def test_fetchmany_negative_size_is_rejected(self):
        db_engine = _make_engine([_FakeResult(rows=[{"id": 1}])])

        with self.assertRaises(ParamsError):
            db_engine.fetchmany("SELECT id FROM t", -5)

    def test_fetchmany_non_int_size_is_rejected(self):
        """未走模型校验（直接调 DBEngine）时传字符串也要报错，而不是拿去 fetchmany。"""
        db_engine = _make_engine([_FakeResult(rows=[{"id": 1}])])

        with self.assertRaises(ParamsError):
            db_engine.fetchmany("SELECT id FROM t", "2")

    def test_fetchone_still_returns_single_dict(self):
        """回归：`fetchone` 的单行 dict 契约不变（文档写的是「用 `字段名` 提取」）。"""
        db_engine = _make_engine([_FakeResult(rows=[{"id": 1, "name": "alice"}])])

        row = db_engine.fetchone("SELECT * FROM t WHERE id = 1")

        self.assertEqual(row, {"id": 1, "name": "alice"})

    def test_fetchone_returns_none_when_no_row(self):
        db_engine = _make_engine([_FakeResult(rows=[])])

        self.assertIsNone(db_engine.fetchone("SELECT * FROM t WHERE id = 999"))

    def test_empty_result_set_is_an_empty_list(self):
        """0920 批次 5 / **N31**：空结果 = `[]`（**不是** `None`）。

        NOTICE（订正）：本条原先叫 `test_empty_result_set_is_still_none`、断言
        `fetchall`/`fetchmany` 空结果返回 `None` —— 但那是**与文档矛盾**的行为：

        - `使用教程.md:741-742`：`fetchmany` / `fetchall` 都是「**行列表**，
          用 `[0].字段` 提取」；
        - 本函数 docstring（`_fetch`）也写着「行集路径统一返回**行列表**」。

        `None` 带来的实际麻烦（`.tmp_audit/n30_n31_check.py`）：
          · jmespath 打向 `None` 会**静默得到 None**（不报错、不告警）；
          · `length_equal: ["@", 0]` 对 `[]` 通过、对 `None` **失败** ——
            也就是「**这张表是空的**」这个再正常不过的断言**没法写**。

        `fetchone` 保持 `None`（单行契约，见下一条用例）—— 两者是**不同**的语义。
        """
        db_engine = _make_engine(
            [_FakeResult(rows=[]), _FakeResult(rows=[]), _FakeResult(rows=[])]
        )

        self.assertEqual(db_engine.fetchall("SELECT * FROM t"), [])
        self.assertEqual(db_engine.fetchmany("SELECT * FROM t", 3), [])
        self.assertIsNone(db_engine.fetchone("SELECT * FROM t"))


# ---------------------------------------------------------------------------
# 真数据库层：事务语义（提交了没有 → 用**另一个连接**能不能看见来判定）
# ---------------------------------------------------------------------------


class _SqliteBackedResult(object):
    """真 sqlite3 cursor 之上的 Result 替身。"""

    def __init__(self, cursor):
        self._cursor = cursor
        self.rowcount = cursor.rowcount
        # 与 SQLAlchemy 同源：`CursorResult.returns_rows` 取自驱动层的 description
        self.returns_rows = cursor.description is not None
        self._columns = [column[0] for column in (cursor.description or [])]

    def _row(self, raw):
        return _FakeRow(dict(zip(self._columns, raw)))

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def fetchone(self):
        row = self._cursor.fetchone()
        return None if row is None else self._row(row)

    def fetchmany(self, size):
        return [self._row(row) for row in self._cursor.fetchmany(size)]


class _SqliteBackedSession(object):
    """真 sqlite3 连接（默认 isolation_level → DML 前隐式 BEGIN，必须显式 commit 才对其它连接可见）。

    只替掉 SQLAlchemy 的 `Session`；`DBEngine` 的 SQL 分类、提交、取值、类型处理全部是真代码。
    """

    def __init__(self, db_path):
        self._connection = sqlite3.connect(db_path)
        self.commit_count = 0

    def execute(self, statement):
        cursor = self._connection.cursor()
        cursor.execute(str(statement))
        return _SqliteBackedResult(cursor)

    def commit(self):
        self.commit_count += 1
        self._connection.commit()

    def close(self):
        self._connection.close()


class TestSqlAgainstRealDatabase(unittest.TestCase):
    """M26 / M27 / L23 的**真库**证据：sqlite 文件库 + 另一个连接的可见性判定。"""

    def setUp(self):
        os.makedirs(TMP_DB_DIR, exist_ok=True)
        self.db_path = os.path.join(TMP_DB_DIR, f"tmp_engine_{uuid.uuid4().hex[:8]}.db")
        self.db_engine = DBEngine.__new__(DBEngine)
        self.db_engine.session = _SqliteBackedSession(self.db_path)
        # 建表：DDL 在 pysqlite 里是自动提交的，用它做前置条件不参与「提交与否」的判定
        self.db_engine.fetchall(
            "CREATE TABLE IF NOT EXISTS t_user "
            "(id INTEGER PRIMARY KEY, order_no TEXT, extra TEXT)"
        )

    def tearDown(self):
        try:
            self.db_engine.session.close()
        except Exception:
            pass
        shutil.rmtree(self.db_path, ignore_errors=True)
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def _rows_visible_from_another_connection(self):
        """另开一个连接读同一个库文件：只有**真的提交了**才看得见。"""
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute("SELECT COUNT(*) FROM t_user").fetchone()[0]
        finally:
            connection.close()

    def test_sql_literal_containing_colon_is_not_a_bind_parameter(self):
        """0920 批次 5 / **N30**：字面量里的 `:name` 不能被当绑定参数。

        修复前用 `text(query)` 执行，而 SQLAlchemy 的 `text()` 会把冒号后的词
        当成**绑定参数** —— 框架又不支持传参，于是任何"值里带冒号"的**合法 SQL**
        都在执行前抛：

        ```text
        SELECT '{"a":1}'    -> bind parameter '1'
        SELECT '/user/:id'  -> bind parameter 'id'
        SELECT ':x'         -> bind parameter 'x'
        ```

        最典型的踩法是 **JSON 字面量**（键值对里天然带冒号）与 **URL/路径字面量**，
        而报错点名的绑定参数是用户**从来没有写过**的东西。
        """
        for label, sql, expected in (
            ("JSON 字面量", "SELECT '{\"a\":1}' AS payload", {"payload": {"a": 1}}),
            ("路径字面量", "SELECT '/user/:id' AS p", {"p": "/user/:id"}),
            ("冒号开头字面量", "SELECT ':x' AS v", {"v": ":x"}),
        ):
            with self.subTest(label=label):
                rows = self.db_engine.fetchall(sql)
                self.assertEqual(rows, [expected])

    def test_json_literal_survives_the_full_step_path(self):
        """端到端：JSON 字面量既不能被杀掉，也不该被 `value_decode` 弄坏。"""
        self.db_engine.update(
            "INSERT INTO t_user (id, order_no, extra) VALUES (1, 'A-1', '{\"k\": 1}')"
        )

        rows = self.db_engine.fetchall(
            "SELECT id, '{\"note\": \":keep\"}' AS marker FROM t_user WHERE id = 1"
        )

        self.assertEqual(rows, [{"id": 1, "marker": {"note": ":keep"}}])

    def test_empty_result_is_an_empty_list_not_none(self):
        """N31：空结果必须是 `[]`（`fetchone` 仍是 `None`）——见 `_fetch` 的 NOTICE。"""
        self.assertEqual(
            self.db_engine.fetchall("SELECT * FROM t_user WHERE id = 999"), []
        )
        self.assertEqual(
            self.db_engine.fetchmany("SELECT * FROM t_user WHERE id = 999", 5), []
        )
        self.assertIsNone(
            self.db_engine.fetchone("SELECT * FROM t_user WHERE id = 999")
        )

    def test_replace_into_is_committed(self):
        """`REPLACE INTO` 必须真落库（修复前：既不返回结果、也从不 commit）。"""
        result = self.db_engine.insert(
            "REPLACE INTO t_user (id, order_no, extra) VALUES (1, '123', '{\"a\": 1}')"
        )

        self.assertEqual(result, {"rowcount": 1})
        self.assertEqual(
            self._rows_visible_from_another_connection(),
            1,
            "写入没有提交：另一个连接看不到这行（等于被测数据被丢掉）",
        )

    def test_insert_without_commit_is_invisible(self):
        """反向断言：`commit=False` 时**不该**提交（调用方自己控制事务的语义要保留）。"""
        self.db_engine.insert(
            "INSERT INTO t_user (id, order_no) VALUES (2, '456')", commit=False
        )

        self.assertEqual(self._rows_visible_from_another_connection(), 0)

    def test_cte_select_returns_rows(self):
        """`WITH ... SELECT` 必须返回结果集（修复前按前 6 个字符分类 → None）。"""
        self.db_engine.insert(
            "REPLACE INTO t_user (id, order_no) VALUES (1, '123')"
        )

        rows = self.db_engine.fetchall(
            "WITH one AS (SELECT * FROM t_user WHERE id = 1) SELECT order_no FROM one"
        )

        self.assertEqual(rows, [{"order_no": "123"}])

    def test_select_with_leading_comment_returns_rows(self):
        self.db_engine.insert(
            "REPLACE INTO t_user (id, order_no) VALUES (1, '123')"
        )

        rows = self.db_engine.fetchall("/* batch 8-4 */ SELECT order_no FROM t_user")

        self.assertEqual(rows, [{"order_no": "123"}])

    def test_string_column_type_is_preserved_but_json_column_decoded(self):
        """M27 的真库版：字符串列保持字符串，JSON 列仍然转成对象。"""
        self.db_engine.insert(
            "REPLACE INTO t_user (id, order_no, extra) VALUES (1, '123', '{\"a\": 1}')"
        )

        rows = self.db_engine.fetchall("SELECT order_no, extra FROM t_user")

        self.assertEqual(rows, [{"order_no": "123", "extra": {"a": 1}}])
        self.assertIsInstance(rows[0]["order_no"], str)

    def test_fetchmany_shape_on_real_result_set(self):
        """L23 的真库版：`fetchmany(sql, 1)` 也是**行列表**。"""
        self.db_engine.insert(
            "REPLACE INTO t_user (id, order_no) VALUES (1, '123')"
        )
        self.db_engine.insert(
            "REPLACE INTO t_user (id, order_no) VALUES (2, '456')"
        )

        rows = self.db_engine.fetchmany("SELECT id FROM t_user ORDER BY id", 1)

        self.assertEqual(rows, [{"id": 1}])

    def test_fetchmany_size_zero_on_real_engine_raises(self):
        with self.assertRaises(ParamsError):
            self.db_engine.fetchmany("SELECT id FROM t_user", 0)


class TestBatch0919_28ValueDecodeMissingCases(unittest.TestCase):
    """批次 8 / **L8 + L9 + L10**：`value_decode` 漏掉的三种形态（都在同一个函数里）。

    ## 现场（实测，修复前，`.tmp_report/probe_l8_l10.py`）

    | 输入 | 修复前输出 | 后果 |
    | --- | --- | --- |
    | `memoryview(b"…")`（psycopg2 py3 的 `bytea`/`BLOB`） | 原样是 `memoryview` | 报告里显示 `<memory at 0x…>`，`json.dumps` → `TypeError` |
    | `datetime.time(10, 0, 0, 500000)` | `'10:00:00'` | **微秒静默丢弃**，照 DB 抄的断言必然失败 |
    | `datetime.datetime(…, 500000)` | `'2024-01-01 10:00:00'` | 同上 |
    | `Decimal('1E-7')` / `Decimal('0.0000001')` | `'1E-7'` | 与 DB 显示的 `0.0000001` 不一致，照抄的断言失败 |

    三条的共同点：都是"**同一个函数里只修了一半**"——
    `bytes` 有分支而 `memoryview` 没有；`timedelta` 保住了微秒而 `time`/`datetime` 没保住；
    `Decimal` 保住了标度却在小值上换了记数法。

    ## 修法（三条都按"与 DB 显示一致、断言可以直接抄"这条既有承诺）

    - **L8**：判据补 `memoryview`（`json_safe_bytes` 本来就是 `bytes(value)` 实现，天然支持）；
    - **L9**：**微秒非 0 才补 `.ffffff`** —— 与同文件 `format_mysql_time`（timedelta）逐字同口径，
      不带微秒时保持原格式（不凭空加 `.000000`）；
    - **L10**：`str(v)` → `format(v, "f")`（定点表示：保留标度、不用指数）。
    """

    NON_UTF8 = b"\xff\xfe\x00binary"

    def test_memoryview_blob_is_decoded_like_bytes(self):
        """**核心**：psycopg2 的 `bytea` 返回 memoryview，必须与 bytes 同样收口。"""
        row = {"payload": memoryview(self.NON_UTF8)}

        DBEngine.value_decode(row)

        self.assertIsInstance(row["payload"], str, "memoryview 没被解码（报告里会是 <memory at …>）")
        self.assertEqual(row["payload"], base64.b64encode(self.NON_UTF8).decode("ascii"))
        json.dumps(row)  # 修复前：TypeError: Object of type memoryview is not JSON serializable

    def test_utf8_memoryview_becomes_text(self):
        row = {"payload": memoryview("中文".encode("utf-8"))}

        DBEngine.value_decode(row)

        self.assertEqual(row["payload"], "中文")

    def test_bytes_bytearray_memoryview_are_indistinguishable(self):
        """口径闸门：三种二进制容器必须产出**同一个**结果（只修一半就是缺陷）。"""
        results = []
        for value in (self.NON_UTF8, bytearray(self.NON_UTF8), memoryview(self.NON_UTF8)):
            with self.subTest(value=type(value).__name__):
                row = {"payload": value}
                DBEngine.value_decode(row)
                results.append(row["payload"])

        self.assertEqual(len(set(results)), 1, f"三种容器结果不一致：{results}")

    def test_time_and_datetime_keep_microseconds(self):
        """**核心**：带微秒的值必须保住 `.ffffff`（修复前被静默丢掉）。"""
        row = {
            "t": datetime.time(10, 0, 0, 500000),
            "dt": datetime.datetime(2024, 1, 1, 10, 0, 0, 500000),
            "t1": datetime.time(10, 0, 0, 1),
        }

        DBEngine.value_decode(row)

        self.assertEqual(row["t"], "10:00:00.500000")
        self.assertEqual(row["dt"], "2024-01-01 10:00:00.500000")
        self.assertEqual(row["t1"], "10:00:00.000001")

    def test_values_without_microseconds_keep_the_old_format(self):
        """反向护栏：微秒为 0 时格式**逐字不变**（不给 DATETIME 列凭空加 `.000000`）。"""
        row = {
            "t": datetime.time(10, 0, 0),
            "dt": datetime.datetime(2024, 1, 1, 10, 0, 0),
            "d": datetime.date(2024, 1, 1),
        }

        DBEngine.value_decode(row)

        self.assertEqual(row["t"], "10:00:00")
        self.assertEqual(row["dt"], "2024-01-01 10:00:00")
        self.assertEqual(row["d"], "2024-01-01")

    def test_microsecond_width_matches_the_timedelta_branch(self):
        """口径一致：与 `format_mysql_time`（timedelta）同为**6 位**微秒。"""
        row = {
            "delta": datetime.timedelta(seconds=1, microseconds=500000),
            "dt": datetime.datetime(2024, 1, 1, 0, 0, 1, 500000),
        }

        DBEngine.value_decode(row)

        self.assertTrue(row["delta"].endswith(".500000"), row["delta"])
        self.assertTrue(row["dt"].endswith(".500000"), row["dt"])

    def test_small_decimals_use_fixed_point_notation(self):
        """**核心**：`Decimal('1E-7')` 必须变成 `'0.0000001'`（与 DB 显示一致）。"""
        for text, expected in (
            ("1E-7", "0.0000001"),
            ("0.0000001", "0.0000001"),
            ("1E+20", "100000000000000000000"),
        ):
            with self.subTest(text=text):
                row = {"amount": decimal.Decimal(text)}

                DBEngine.value_decode(row)

                self.assertEqual(row["amount"], expected)

    def test_decimal_never_uses_scientific_notation(self):
        """收口：任何 Decimal 的字符串形态都不带指数（`E`）。"""
        for text in ("1E-7", "1E+20", "1.10", "5.00", "0.000001", "-2.5E-9"):
            with self.subTest(text=text):
                row = {"amount": decimal.Decimal(text)}

                DBEngine.value_decode(row)

                self.assertNotIn("E", row["amount"])
                self.assertNotIn("e", row["amount"])

    def test_decimal_scale_is_preserved(self):
        """反向护栏：`1.10` / `5.00` 的标度逐字不变（这是 0919-11 拍板的口径）。"""
        row = {"price": decimal.Decimal("1.10"), "whole": decimal.Decimal("5.00")}

        DBEngine.value_decode(row)

        self.assertEqual(row["price"], "1.10")
        self.assertEqual(row["whole"], "5.00")

    def test_small_decimal_assertion_can_use_the_db_form(self):
        """下游可用：断言可以直接抄 DB 显示的 `0.0000001`。"""
        row = {"amount": decimal.Decimal("1E-7")}
        DBEngine.value_decode(row)

        equal(row["amount"], "0.0000001")


if __name__ == "__main__":
    unittest.main()
