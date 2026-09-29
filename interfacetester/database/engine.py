# -*- coding: utf-8 -*-
import datetime
import decimal
import json

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from interfacetester.exceptions import ParamsError
from interfacetester.utils import json_safe_bytes


def format_mysql_time(value: datetime.timedelta) -> str:
    """`datetime.timedelta` → MySQL `TIME` 的文本表示：`[±]HH:MM:SS[.ffffff]`。

    NOTICE（0919-11 / §三.6a）：pymysql 把 MySQL 的 `TIME` 列映射成 `datetime.timedelta`
    （`DECIMAL`/`NUMERIC` → `decimal.Decimal`），修复前 `value_decode` 不认这两种类型，
    于是一列 TIME / DECIMAL 会**原样带着原生对象**流进报告与断言：
      ① 报告不崩，但静默降级成 `repr`：`"datetime.timedelta(seconds=3661)"`；
      ② 提取出来的值再塞进后续请求的 JSON body → `TypeError: Object of type ... is not JSON serializable`；
      ③ 用**字符串字面量**断言必然失败（值是对的、断言却红）。

    为什么不直接用 `str(timedelta)`（实测过）：
      - `str(timedelta(seconds=3661))` = `'1:01:01'`，而 MySQL 与 DB 客户端显示 `'01:01:01'`
        —— 用户抄的是后者，断言就该能用后者；
      - 超过 24 小时会变成 `'1 day, 1:00:00'`（MySQL `TIME` 上限是 `838:59:59`，
        `str()` 的形态根本不可断言）；
      - 负数会变成 `'−1 day, 23:00:00'` 这种归一化形态，而 MySQL 是 `'-23:00:00'` 一类。
    所以这里按 MySQL 的形态自己格式化：小时**不截断到 24**、负数带前导 `-`、
    微秒非 0 时补 `.ffffff`（与既有的 `datetime`/`date` 一样是**秒级精度之上的补充**，
    不丢信息）。
    """
    negative = value < datetime.timedelta(0)
    if negative:
        value = -value
    # 用 days/seconds 而不是 total_seconds()：后者是 float，大值会有精度风险
    total_seconds = value.days * 86400 + value.seconds
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    if value.microseconds:
        text = f"{text}.{value.microseconds:06d}"
    return f"-{text}" if negative else text


class DBEngine(object):
    def __init__(self, db_uri):
        """
        db_uri = f'mysql+pymysql://{username}:{password}@{host}:{port}/{database}?charset=utf8mb4'

        """
        self.engine = create_engine(db_uri)
        self.session = sessionmaker(bind=self.engine)()

    @staticmethod
    def value_decode(row: dict):
        """
        Try to decode value of table
        datetime.datetime-->string
        datetime.date-->string
        datetime.time-->string
        datetime.timedelta-->string（MySQL TIME）
        decimal.Decimal-->string
        json str-->dict/list
        :param row:
        :return:

        NOTICE（0918-8 / M27）：**只接受**「解析结果是 dict/list」的字符串，其余一律保持原样。
        修复前只要 `json.loads` 不抛异常就整体替换，于是普通字符串列被静默改类型：
        `"123"→123`（int）、`"null"→None`、`"1.10"→1.1`（float）、`"true"→True`。
        后果不是「显示不好看」，而是**断言与引用被污染**：库里存的是字符串 `"123"`
        （例如订单号/手机号），`extract` 出来却变成 int，`eq: [order_no, "123"]` 直接失败；
        反之 `"1.10"` 与 `"1.1"` 会被判为相等。带单引号的文本（如 `'{'a': 1}'`）则本来就不是合法 JSON，
        行为不变。

        NOTICE（0919-11 / §三.6a）：补齐 `time` / `timedelta`（MySQL `TIME`）与
        `decimal.Decimal`（`DECIMAL`/`NUMERIC`）三种类型，口径统一为**字符串化**
        （与上面 `datetime`/`date` 的既有做法一致）。三种类型的失败形态见
        `format_mysql_time` 的 NOTICE。

        ⚠️ **口径变更与代价（如实登记）**：`Decimal` 字符串化之后，
        `eq: [amount, "1.10"]` 这类**字符串字面量**断言由「必然失败」变成「可用」，
        但**精确可表示**的浮点字面量（`0.5` / `1.5` / `2.25` 这类）会由「通过」变成「失败」
        —— 修复前 `Decimal('0.5') == 0.5` 恰好为真。之所以仍然选字符串化：
        金额/小数列本就该按**精确值**比较（`0.1`、`9.99` 这类浮点字面量修复前后都是失败的），
        而且字符串化后失败信息里会直接显示 `'1.10' vs 1.1`，一眼能看出该加引号。
        若要改成「与浮点字面量兼容」，把 `str(v)` 换成 `float(v)` 即可（会丢精度/标度，不推荐）。
        """
        for k, v in row.items():
            if isinstance(v, datetime.datetime):
                # NOTICE（批次 8 / **L9**）：**微秒非 0 才补 `.ffffff`** —— 与同文件
                # `format_mysql_time`（timedelta）的口径一致。修复前固定
                # `strftime("%Y-%m-%d %H:%M:%S")`：`datetime(…, 500000)` 出来是
                # `'2024-01-01 10:00:00'`，**微秒被静默丢掉**（实测 `.tmp_report/probe_l8_l10.py`）
                # → 断言用 DB 里看到的 `'…10:00:00.500000'` 必然失败，而"值明明是对的"。
                # 不带微秒时**保持原格式**（不给 `DATETIME` 列凭空加 `.000000`）。
                row[k] = (
                    v.strftime("%Y-%m-%d %H:%M:%S.%f")
                    if v.microsecond
                    else v.strftime("%Y-%m-%d %H:%M:%S")
                )
            elif isinstance(v, datetime.date):
                row[k] = v.strftime("%Y-%m-%d")
            elif isinstance(v, datetime.time):
                # L9 同上：MySQL 的 `TIME(6)` / `DATETIME` 取出的 time 部分都可能带微秒
                row[k] = (
                    v.strftime("%H:%M:%S.%f")
                    if v.microsecond
                    else v.strftime("%H:%M:%S")
                )
            elif isinstance(v, datetime.timedelta):
                row[k] = format_mysql_time(v)
            elif isinstance(v, decimal.Decimal):
                # str(Decimal) 保留标度：Decimal('1.10') -> '1.10'（不是 '1.1'），
                # 与 DB 里 DECIMAL(10,2) 显示的一致，断言可以直接抄。
                #
                # NOTICE（批次 8 / **L10**）：但 `str()` 在**小值**上会变成科学计数法 ——
                # `Decimal('1E-7')` → `'1E-7'`、`Decimal('0.0000001')` → `'1E-7'`
                # （实测同上探针），而 DB 客户端显示的是 `0.0000001`：
                # 用户照 DB 抄的断言必然失败，与"和 DB 显示一致、断言可以直接抄"的承诺相反。
                # 改用 `format(v, "f")`（定点表示）：**保留标度**、**不用指数**，
                # `'1.10'`/`'5.00'` 逐字不变，只有原本带指数的形态变成定点。
                # 代价（如实登记）：scale 极大时字符串会很长（MySQL `DECIMAL` 上限 30 位，
                # PG `numeric` 可以更大）—— 换来的是"与 DB 显示一致"，这里选后者。
                row[k] = format(v, "f")
            elif isinstance(v, (bytes, bytearray, memoryview)):
                # NOTICE（0919-15 / 登记项③）：`BLOB` / `BINARY` 列返回 bytes，与
                # TIME / DECIMAL 是**同一类**问题（原生对象进报告/断言/JSON：实测
                # `json.dumps({'blob': bytes})` → `TypeError`，报告侧靠
                # `ExtendJSONEncoder` 兜底成 repr）。
                # 口径**与 thrift 的 `binary` 字段完全一致**（同一个 `utils.json_safe_bytes`，
                # 有身份断言钉住「只有一份」）：能按 UTF-8 解码 → 文本；不能 → base64（无损）。
                # 修复前这里没有分支，bytes 原样流向报告与断言。
                #
                # NOTICE（批次 8 / **L8**）：判据补上 **`memoryview`** —— psycopg2 在 py3 下
                # 的 `bytea` / `BLOB` 返回的正是 memoryview，而 `json_safe_bytes` 本来就用
                # `bytes(value)` 实现（天然支持它）。修复前只有 `bytes`/`bytearray` 被接住，
                # memoryview 原样流向报告（显示 `<memory at 0x…>`）且
                # `json.dumps` 直接 `TypeError`（实测同上探针）——同一个缺陷家族只修了一半。
                row[k] = json_safe_bytes(v)
            elif isinstance(v, str):
                try:
                    decoded = json.loads(v)
                except ValueError:
                    continue
                if isinstance(decoded, (dict, list)):
                    row[k] = decoded

    def _fetch(self, query, size=-1, commit=True, single=False, dml=False):
        """执行一条 SQL，并按「是否有结果集」分别处理。

        NOTICE（0918-8 / M26）：**不能再按 SQL 前 6 个字符分类**。修复前用的是
        `query.upper()[:6] == "SELECT"`，于是这些语句既不返回结果、也不提交事务，
        直接 `return None`：
          - `WITH ... AS (...) SELECT ...`（CTE）→ 查询结果被吞掉；
          - `REPLACE INTO ...` / `TRUNCATE ...` / `CREATE ...` / `SET ...` → **写入被回滚**
            （session 从未 commit，连接归还连接池时丢弃事务）；
          - 前导注释/换行（如 `/* x */ SELECT 1`，strip 后仍以 `/` 开头）→ 同样被吞。
        现在统一用 SQLAlchemy 自己的 `result.returns_rows` 判断；其余语句（写库/DDL）
        按 `commit` 参数提交（默认提交）。

        NOTICE（0918-8 / L23）：行集路径统一返回 **行列表**（`fetchone` 才是单行 dict）。
        修复前 `size == 1` 走 `fetchone()` 分支返回 dict，`size == 0` 走 `fetchmany(0)`
        返回 None——同一个 `fetchmany` 的形状随参数而变，而 `使用教程.md` 明确写的是
        「行列表，用 `[0].字段` 提取」。`size < 1` 属于写法错误，由 `fetchmany()` 直接拒绝。
        """
        query = query.strip()
        # NOTICE（0920 批次 5 / **N30**）：**不能用 `text(query)`**。
        #
        # SQLAlchemy 的 `text()` 会把 `(?<![:\w\\]):(\w+)` 当成**绑定参数**，
        # 而这个框架的 SQL step **不支持传参**（`_fetch`/`fetchone`/`fetchall`
        # 都没有参数入口），于是任何"值里带冒号"的**合法 SQL** 都会在执行前抛：
        #
        # ```text
        # SELECT '{"a":1}'      -> StatementError: A value is required for bind parameter '1'
        # SELECT '/user/:id'    -> StatementError: A value is required for bind parameter 'id'
        # SELECT ':x'           -> StatementError: A value is required for bind parameter 'x'
        # ```
        #
        # 最典型的踩法是**JSON 字面量**（`'{"a":1}'` 这种键值对里带冒号）
        # 与**URL/路径字面量**（`'/user/:id'`）—— 报错点名的绑定参数
        # 是用户**从来没有写过**的东西，完全无法定位。
        #
        # 改用 `exec_driver_sql`：把 SQL **原样**交给 DBAPI 驱动，
        # 不做 SQLAlchemy 层的绑定参数解析 —— 这正是"不传参"场景该用的入口
        # （对照 `text(...).bindparams()` 那条路：它要求显式声明参数，
        # 而我们没有参数可声明）。
        #
        # NOTICE: `exec_driver_sql` 返回的 `CursorResult` 同样有
        # `returns_rows` / `fetchone` / `fetchall` / `fetchmany` / `rowcount` / `_mapping`，
        # 所以下面所有既有分支**一行都不用改**。
        # NOTICE: `exec_driver_sql` 是 **`Connection` 的方法**（SQLAlchemy 2.0 里
        # 模块级没有同名函数，第一版 `from sqlalchemy.sql import exec_driver_sql`
        # 直接 ImportError，实测踩到），所以先取底层 connection 再执行。
        #
        # NOTICE（**回退分支**）：`session.connection()` 是 SQLAlchemy 的正式 API，
        # 但本仓的单测用脚本化的 `_FakeSession`（只实现 `execute`/`commit`/`close`）
        # 来验证分支与形状 —— 那是**有意的测试接缝**。这里在拿不到 connection 时
        # 退回 `session.execute(text(...))`（修复前的形态）：测试接缝继续可用，
        # 而**真实 session 永远走 `exec_driver_sql`**（N30 的正解）。
        connection = getattr(self.session, "connection", None)
        if callable(connection):
            result = connection().exec_driver_sql(query)
        else:  # pragma: no cover - 仅脚本化替身走这里
            result = self.session.execute(text(query))


        if result.returns_rows:
            if single:
                # NOTICE（批次 6 / M5）：`single` 分支原先**提前 return**（`row is None` 与
                # 正常取值两处），于是 `fetchone("INSERT … RETURNING id")` 也从不提交。
                # 现在把取值与提交分开：先取值，再按统一口径提交，最后返回。
                row = result.fetchone()
                on = dict(row._mapping) if row is not None else None
                if on:
                    self.value_decode(on)
                if commit:
                    self.session.commit()
                return on or None

            if size < 0:
                raw_rows = result.fetchall()
            else:
                raw_rows = result.fetchmany(size)
            row_list = [dict(el._mapping) for el in raw_rows]
            for el in row_list:
                self.value_decode(el)

            # NOTICE（批次 6 / **M5**）：**提交不能只挂在 `dml` 上**。
            #
            # 修复前这里是 `if dml:` 才提交，而只有 `insert()/update()/delete()` 会传
            # `dml=True` —— `fetchone/fetchmany/fetchall` 都是 `dml=False`。于是
            # **带结果集的写语句**（PostgreSQL 的 `INSERT … RETURNING`、SQL Server 的
            # `OUTPUT`、MySQL 的 `CALL` 返回行集）用 `fetch*` 写时**从不提交**：
            # 拿到 id、断言通过，数据却在 `session.close()` 时回滚。
            # 更糟的是"有时丢、有时不丢"——同一用例后续任何一条 DML 会把它**顺带提交**。
            #
            # 现在口径统一成一句话：**`commit=True`（默认）就提交**，与下面非行集分支一致；
            # `dml` 只决定**返回形状**（`{"rowcount", "rows"}` vs 行列表）。
            # 纯 `SELECT` 也会因此结束事务——这是**有意接受**的：读事务提交无害，
            # 还顺带释放读取期间的锁；而"读到一半把上一句的写丢了"是不可接受的。
            # 需要手工管事务的调用方用 `commit=False`（该语义原样保留，见
            # `test_commit_false_never_commits`）——注意此时**后续的读也要显式传
            # `commit=False`**，否则那次读会替你提交。
            if commit:
                self.session.commit()

            if dml:
                # 写库语句**又**带结果集的情形：PostgreSQL 的 `INSERT ... RETURNING`、
                # SQL Server 的 `OUTPUT`。此时既不能丢 rowcount，也不能吞掉返回行
                # （提交已在上面的统一口径里做过）。
                return {"rowcount": result.rowcount, "rows": row_list}

            # NOTICE（0920 批次 5 / **N31**）：空结果必须返回 **`[]`**，不能返回 `None`。
            #
            # 修复前是 `return row_list or None` —— 空列表是假值，于是"查不到数据"与
            # "取不到结果"在返回值上**无法区分**：
            #
            # ```text
            # 有数据 -> [{'id': 1}]
            # 无数据 -> None          ← 契约（本函数 docstring、使用教程.md）写的是"行列表"
            # ```
            #
            # 后果（实测 `.tmp_audit/n31_check.py`）：
            #   · jmespath `@[0].name` 打向 `None` 会**静默得到 None**（不报错、不告警）；
            #   · `length_equal: ["@", 0]` 对 `[]` 通过、对 `None` **失败** ——
            #     也就是"**这张表是空的**"这个再正常不过的断言**没法写**。
            #
            # 返回 `[]` 之后契约才自洽：行集路径**永远**是列表（`fetchone` 才是单行/None）。
            return row_list

        # 非行集语句：DML（UPDATE/DELETE/INSERT/REPLACE）/DDL（CREATE/TRUNCATE/ALTER/DROP）
        if commit:
            self.session.commit()
        return {"rowcount": result.rowcount}

    def fetchone(self, query, commit=True):
        return self._fetch(query, size=1, commit=commit, single=True)

    def fetchmany(self, query, size, commit=True):
        # L23（0918-8）：显式拒绝 `size < 1`。修复前 `size=0`（YAML 里 `method: fetchmany`
        # 忘了写 `size`，模型默认就是 0）会静默返回 None，看起来像「查不到数据」，
        # 实际是参数写错——按「宁可报错也不静默」直接报错并给出正确写法。
        if not isinstance(size, int) or size < 1:
            raise ParamsError(
                f"fetchmany 的 size 必须是 >= 1 的整数，实际是 {size!r}（{type(size).__name__}）\n"
                "hint: `size` 表示「最多取多少行」；取单行请用 fetchone，取全部请用 fetchall。"
            )
        return self._fetch(query=query, size=size, commit=commit)

    def fetchall(self, query, commit=True):
        return self._fetch(query=query, size=-1, commit=commit)

    def insert(self, query, commit=True):
        return self._fetch(query=query, commit=commit, dml=True)

    def delete(self, query, commit=True):
        return self._fetch(query=query, commit=commit, dml=True)

    def update(self, query, commit=True):
        return self._fetch(query=query, commit=commit, dml=True)

    def close(self):
        """关闭 session 并释放底层连接池，避免数据库连接泄漏。"""
        try:
            self.session.close()
        except Exception:
            pass
        try:
            self.engine.dispose()
        except Exception:
            pass


if __name__ == "__main__":
    # db = DBEngine("mysql+pymysql://xxxxx:xxxxx@10.0.0.1:3306/dbname?charset=utf8mb4")
    db = DBEngine("sqlite:////Users/xxx/InterfaceTester/examples/data/sqlite.db")
    print(db.fetchmany("""
    select* from student""", 5))
    print(db.fetchmany("select* from student", 5))
