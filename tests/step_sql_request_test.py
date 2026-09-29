import sys
import types
import unittest

from loguru import logger
from unittest import mock
from urllib.parse import unquote

from interfacetester import Config, InterfaceTester, RunSqlRequest, Step
from interfacetester import exceptions, step_sql_request
from interfacetester.exceptions import ValidationFailure
from interfacetester.parser import Parser
from interfacetester.response import SqlResponseObject

_SQL_STEP = Step(RunSqlRequest("sql step").fetchall("select 1"))


def _db_uri_userinfo(db_uri):
    """按**消费者（SQLAlchemy）的解码方式**还原连接串里的 userinfo。

    SQLAlchemy 只对 `username` / `password` 调 `unquote`（`engine/url.py`），
    库名等 path 段**不解码** —— 所以这里也只还原 userinfo，与生产端的编码口径一一对应。
    """
    userinfo = db_uri.split("://", 1)[1].split("@", 1)[0]
    user, _, password = userinfo.partition(":")
    return unquote(user), unquote(password)


# 账号/口令里的特殊字符全表（userinfo 两侧都要编码）：修复前只有**空格**会坏
# （被 `quote_plus` 编成 `+`），其余字符修复前后都应往返一致 —— 留着是为了防止「修 A 坏 B」。
_CREDENTIALS_ROUND_TRIP_CASES = (
    ("tester", "plain"),
    ("tester", "p@ss word"),
    ("test er", "p@ss word"),
    ("tester", "a b c"),
    ("tester", "p@ss/w%rd"),
    ("tester", "a+b"),
    ("tester", "p:q@r"),
    ("tester", "#frag?ment"),
    ("tester", "中文密码"),
)


class _SqlFailFastCase(InterfaceTester):
    config = Config("sql fail fast")
    teststeps = [_SQL_STEP]


class _FakeDBEngine(object):
    """替身引擎：让 SQL step 的 extract/validate 链路可以完全离线跑（不需要 sqlalchemy/MySQL）。"""

    instances = []

    def __init__(self, db_uri):
        self.db_uri = db_uri
        self.closed = 0
        self.calls = []
        _FakeDBEngine.instances.append(self)

    def fetchall(self, sql):
        self.calls.append(("fetchall", sql))
        return [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]

    def fetchone(self, sql):
        self.calls.append(("fetchone", sql))
        return {"id": 1, "name": "alice"}

    def close(self):
        self.closed += 1


class TestSqlFailFast(unittest.TestCase):
    """SQL 扩展在依赖缺失时的行为：抛异常，而不是 sys.exit(1)。

    NOTICE: 修复前 `ensure_sql_ready()` 直接 `sys.exit(1)`，在 pytest 进程内会把整个测试
    会话杀掉——其余用例的结果全部丢失，用户只看到一个「进程退出」。修复后只让当前用例
    报错（与 step_thrift_request.ensure_thrift_ready、loader.load_debugtalk_functions 一致）。
    """

    def test_ensure_sql_ready_raises_instead_of_exit(self):
        """依赖缺失 → RuntimeError（带可执行的安装提示），而不是 SystemExit。"""
        with mock.patch.object(step_sql_request, "SQL_READY", False):
            with self.assertRaises(RuntimeError) as cm:
                step_sql_request.ensure_sql_ready()

        self.assertIn("interfacetester[sql]", str(cm.exception))

    def test_ensure_sql_ready_is_noop_when_deps_installed(self):
        with mock.patch.object(step_sql_request, "SQL_READY", True):
            self.assertIsNone(step_sql_request.ensure_sql_ready())

    def test_sql_step_error_does_not_exit_process(self):
        """端到端：依赖缺失时跑 SQL step 只会让该用例报错，进程不会被杀掉。

        这里只 patch `SQL_READY`，不 patch `DBEngine`——因为 `ensure_sql_ready()` 在建立
        连接之前就会抛错，因此该用例不需要真实数据库。
        """
        with mock.patch.object(step_sql_request, "SQL_READY", False):
            with self.assertRaises(RuntimeError):
                _SqlFailFastCase().test_start()


class TestSqlResponseObject(unittest.TestCase):
    """SQL 结果的提取与断言（`SqlResponseObject` 继承 `ResponseObjectBase`，与 HTTP 侧同一套算子）。"""

    def setUp(self):
        self.parser = Parser(functions_mapping={})

    def test_extract_from_dict_and_list(self):
        row_obj = SqlResponseObject({"id": 1, "name": "alice"}, self.parser)
        self.assertEqual(row_obj.extract({"student_id": "id", "who": "name"}), {
            "student_id": 1,
            "who": "alice",
        })

        rows_obj = SqlResponseObject(
            [{"name": "alice"}, {"name": "bob"}], self.parser
        )
        self.assertEqual(
            rows_obj.extract({"first": "[0].name", "second": "[1].name"}),
            {"first": "alice", "second": "bob"},
        )

    def test_validate_pass_and_fail(self):
        rows_obj = SqlResponseObject([{"id": 1, "name": "alice"}], self.parser)

        rows_obj.validate(
            [
                {"eq": ["[0].name", "alice"]},
                # jmespath 的 "@" 表示当前节点，即整个结果集
                {"length_equal": ["@", 1]},
            ]
        )
        self.assertEqual(
            rows_obj.validation_results["validate_extractor"][0]["check_result"], "pass"
        )

        with self.assertRaises(ValidationFailure):
            rows_obj.validate([{"eq": ["[0].name", "bob"]}])


class TestSqlStepExtractAndValidate(unittest.TestCase):
    """SQL step 的完整链路：建连 → 执行 → 提取 → 断言（用替身引擎，离线可跑）。

    NOTICE: `run_step_sql_request` 里是「用完再 import」DBEngine 的
    （`from interfacetester.database.engine import DBEngine`），因此把该模块替换成替身
    即可在不装 sqlalchemy、没有 MySQL 的情况下覆盖整条链路，包括 0916-7 修掉的
    「db_config 变化后重建连接」。
    """

    def setUp(self):
        _FakeDBEngine.instances = []
        self._original_engine_module = sys.modules.get(
            "interfacetester.database.engine"
        )
        fake_module = types.ModuleType("interfacetester.database.engine")
        fake_module.DBEngine = _FakeDBEngine
        sys.modules["interfacetester.database.engine"] = fake_module
        self._sql_ready_patch = mock.patch.object(
            step_sql_request, "SQL_READY", True
        )
        self._sql_ready_patch.start()

    def tearDown(self):
        self._sql_ready_patch.stop()
        if self._original_engine_module is None:
            sys.modules.pop("interfacetester.database.engine", None)
        else:
            sys.modules["interfacetester.database.engine"] = (
                self._original_engine_module
            )

    @staticmethod
    def _fetch_students_step(
        name="fetch students",
        user="tester",
        password="p@ss word",
        database="school",
    ):
        return (
            RunSqlRequest(name)
            .with_db_config(
                ip="127.0.0.1",
                user=user,
                password=password,
                database=database,
            )
            .fetchall("select * from student")
        )

    def _run_case(self, steps):
        class _Case(InterfaceTester):
            config = Config("sql step case")
            teststeps = steps

        return _Case().test_start()

    def test_step_extracts_and_validates(self):
        step = Step(
            self._fetch_students_step()
            .extract()
            .with_jmespath("[0].name", "first_student")
            .validate()
            .assert_equal("[1].name", "bob")
        )

        runner = self._run_case([step])
        summary = runner.get_summary()

        self.assertTrue(summary.success)
        self.assertEqual(
            summary.step_results[0].export_vars, {"first_student": "alice"}
        )
        self.assertEqual(len(_FakeDBEngine.instances), 1)
        # 连接串里的账号密码做了 URL 编码，密码含空格也要能连。
        # NOTICE（批次 D / T4）：断言的是 `p%40ss%20word`（`quote(x, safe='')`）——
        # 修复前这里钉的是 `p%40ss+word`：`quote_plus` 把空格编成 `+`，而 SQLAlchemy 用
        # `unquote` 解（**不**还原 `+`），驱动实收 `'p@ss+word'`。也就是说这条注释写着
        # 「密码含空格也要能连」、断言却把**错误编码**钉住了 —— 全仓唯一一处
        # 「测试在守护 bug」（替身 engine 不真连，所以「能连」从未被端到端验证过）。
        # 端到端口径见下面的 `test_db_uri_credentials_round_trip`。
        self.assertIn(
            "tester:p%40ss%20word@127.0.0.1:3306/school",
            _FakeDBEngine.instances[0].db_uri,
        )
        # 用例结束后由 runner 的 finally 释放连接
        self.assertEqual(_FakeDBEngine.instances[0].closed, 1)

    def _run_and_get_userinfo(self, user, password):
        step = Step(
            self._fetch_students_step(user=user, password=password)
            .validate()
            .assert_equal("[0].name", "alice")
        )
        before = len(_FakeDBEngine.instances)
        self._run_case([step])
        self.assertEqual(
            len(_FakeDBEngine.instances), before + 1, "应当只新建了一条连接"
        )
        return _db_uri_userinfo(_FakeDBEngine.instances[-1].db_uri)

    def test_db_uri_credentials_round_trip(self):
        """T4：连接串里的账号/口令必须能被消费者**原样解回来**（含空格等特殊字符）。

        NOTICE（批次 D / T4）：修复前 `step_sql_request` 用 `quote_plus` 编码 userinfo，
        空格被编成 `+`；而连接串的消费者 SQLAlchemy 解析 userinfo 用的是 `unquote`
        （不还原 `+`）→ 驱动收到的是**被改过的口令**，形态是「认证失败，且用例里写的
        口令明明是对的」，除了「口令里有空格」没有任何线索。实测（真 sqlalchemy 2.0.54）
        `password='p@ss word'` → `make_url().password == 'p@ss+word'`。

        这里不 import sqlalchemy（它不在基础依赖里，装了才有的用例会被 skip，
        而这条护栏必须**总是**跑），而是直接用消费者同款的 `unquote` 断言往返一致；
        两者的口径已逐例核对相同（`.tmp_audit/probe_t4_fix.py`，见
        `docs/架构与调用链.md` §7 的注入验证记录）。
        """
        for user, password in _CREDENTIALS_ROUND_TRIP_CASES:
            with self.subTest(user=user, password=password):
                got_user, got_password = self._run_and_get_userinfo(user, password)
                self.assertEqual(
                    got_user,
                    user,
                    f"账号往返不一致：用例里写的是 {user!r}，驱动收到的是 {got_user!r}",
                )
                self.assertEqual(
                    got_password,
                    password,
                    f"口令往返不一致：用例里写的是 {password!r}，"
                    f"驱动收到的是 {got_password!r}",
                )

    def test_db_uri_database_path_is_not_encoded(self):
        """T4 连带：**只**编码 userinfo —— 库名（path 段）与 query 原样透传。

        这条守的是「修复过头」：SQLAlchemy **不解码** path 段
        （实测 `make_url("…//my%20db").database == 'my%20db'`），若顺手把库名也
        `quote()`，带空格的库名会变成字面量 `my%20db` —— 从「能连」变成「连不上」。
        """
        step = Step(
            self._fetch_students_step(database="my db")
            .validate()
            .assert_equal("[0].name", "alice")
        )
        self._run_case([step])

        db_uri = _FakeDBEngine.instances[0].db_uri
        self.assertTrue(
            db_uri.endswith("/my db?charset=utf8mb4"),
            f"库名必须原样透传（不能编码）：{db_uri!r}",
        )

    def test_validation_failure_is_raised(self):
        step = Step(
            self._fetch_students_step()
            .validate()
            .assert_equal("[0].name", "not-alice")
        )

        with self.assertRaises(ValidationFailure):
            self._run_case([step])

    def test_failed_step_is_recorded_with_its_own_type(self):
        """0919-18 / H1 连带：失败记录要拿 `step.type()`，而 SQL step 的那个曾经是坏的。

        修复前 `StepSqlRequestValidation.type()` 继承自请求侧基类，读的是
        `self.__step.request.method`——SQL step 上 `request` 是 None，于是
        `AttributeError: 'NoneType' object has no attribute 'method'`。
        该错误此前**潜伏**（没有调用方读 `type()`），H1 的失败记录第一次读它就在 SQL
        用例上炸了，而且是在 `except` 里炸：原本的「断言失败」被替换成「用例 error」，
        失败记录反而没写进报告（等于 H1 又变回假通过）。
        """

        class _FailingSqlCase(InterfaceTester):
            config = Config("sql step type probe")
            teststeps = [
                Step(
                    self._fetch_students_step()
                    .validate()
                    .assert_equal("[0].name", "not-alice")
                )
            ]

        case = _FailingSqlCase()
        with self.assertRaises(ValidationFailure):
            case.test_start()

        summary = case.get_summary()
        self.assertFalse(summary.success)
        self.assertEqual(len(summary.step_results), 1, "失败的 SQL step 必须留在报告里")
        self.assertEqual(summary.step_results[0].name, "fetch students")
        self.assertTrue(
            summary.step_results[0].step_type.startswith("sql-request-"),
            "SQL step 的 type() 必须是 `sql-request-…`（修复前抛 AttributeError，"
            f"兜底后会是 'unknown'）：{summary.step_results[0].step_type!r}",
        )

    def test_engine_is_rebuilt_when_db_config_changes(self):
        """同一用例内换库/换主机时重建连接（0916-7 修复点）。"""
        first_step = Step(
            self._fetch_students_step("first db")
            .validate()
            .assert_equal("[0].id", 1)
        )
        second_step = Step(
            RunSqlRequest("second db")
            .with_db_config(
                ip="10.0.0.2", user="other", password="pwd", database="another"
            )
            .fetchall("select * from teacher")
            .validate()
            .assert_equal("[0].id", 1)
        )

        self._run_case([first_step, second_step])

        self.assertEqual(len(_FakeDBEngine.instances), 2)
        self.assertIn("10.0.0.2:3306/another", _FakeDBEngine.instances[1].db_uri)
        # 旧连接被关闭，新连接在用例结束时关闭
        self.assertEqual(_FakeDBEngine.instances[0].closed, 1)
        self.assertEqual(_FakeDBEngine.instances[1].closed, 1)

    def test_sql_step_hooks_can_read_sql_response(self):
        """setup/teardown hook 里可以用 $sql_response 读结果。

        NOTICE: `$sql_response` 是 `SqlResponseObject` 包装对象，取原始结果用 `.resp_obj`
        （或 `.extract()`）；它不像 HTTP 侧的 `$response` 那样带 `__getattr__` 属性代理，
        因此 `$sql_response.status_code` 之类的写法在 SQL 场景下不适用。
        """
        seen = {}

        class _SqlCase(InterfaceTester):
            config = Config("sql hooks case")
            teststeps = [
                Step(
                    RunSqlRequest("fetch with hook")
                    .with_db_config(ip="127.0.0.1", user="u", password="p", database="d")
                    .fetchall("select * from student")
                    .teardown_hook("${record_sql_response($sql_response)}")
                    .validate()
                    .assert_equal("[0].name", "alice")
                )
            ]

        original_setup = _SqlCase._setup_runner

        def _setup_with_function(self):
            original_setup(self)
            # hook 函数从 runner 的 parser.functions_mapping 里查（等价于项目 debugtalk.py 里定义）
            self.parser.functions_mapping["record_sql_response"] = (
                lambda sql_response: seen.update({"rows": sql_response.resp_obj})
            )

        with mock.patch.object(_SqlCase, "_setup_runner", _setup_with_function):
            _SqlCase().test_start()

        self.assertEqual(seen["rows"][0]["name"], "alice")


class TestSqlDbConfigDefaults(unittest.TestCase):
    """L5：SQL step 的 db_config 与 `config.db` 的合并口径（`or` vs `is None`）。

    NOTICE: HTTP 侧（`step_request`）早就统一成 `is None` 并留了注释说明
    「`timeout: 0` 是合法值，用 `or` 会被吞掉」；thrift/sql 两侧漏改，
    而且 sql 的 `port` 还有更严重的一半：默认值 3306 是**真值**，
    导致回退到 `config.db.port` 的分支实际是**死代码**（config 被静默忽略）。
    """

    def setUp(self):
        _FakeDBEngine.instances = []
        self._original_engine_module = sys.modules.get(
            "interfacetester.database.engine"
        )
        fake_module = types.ModuleType("interfacetester.database.engine")
        fake_module.DBEngine = _FakeDBEngine
        sys.modules["interfacetester.database.engine"] = fake_module
        self._sql_ready_patch = mock.patch.object(step_sql_request, "SQL_READY", True)
        self._sql_ready_patch.start()

    def tearDown(self):
        self._sql_ready_patch.stop()
        if self._original_engine_module is None:
            sys.modules.pop("interfacetester.database.engine", None)
        else:
            sys.modules["interfacetester.database.engine"] = (
                self._original_engine_module
            )

    @staticmethod
    def _run_case(steps, config):
        class _Case(InterfaceTester):
            pass

        _Case.config = config
        _Case.teststeps = steps
        return _Case().test_start()

    @staticmethod
    def _config(port):
        config = Config("sql db_config defaults")
        config.db().ip("127.0.0.1").user("u").password("p").database("d").port(port)
        return config

    def test_config_db_port_is_reachable_when_step_leaves_it_unset(self):
        """L5：step 不写 port 时必须用 `config.db.port`。

        实测修复前：config.db.port=3307、step 不给 port → 实际连的是 **3306**
        （`TSqlRequest.db_config` 沿用 `TConfigDB`，port 默认 3306 是真值，
        `... or config.db.port` 的回退分支永远不执行）。
        """
        step = Step(
            RunSqlRequest("no explicit port")
            .with_db_config(ip="10.0.0.9", user="u", password="p", database="d")
            .fetchall("select 1")
        )

        self._run_case([step], self._config(port=3307))

        self.assertIn(":3307/", _FakeDBEngine.instances[-1].db_uri)

    def test_explicit_zero_port_is_not_swallowed(self):
        """L5：显式 `port=0` 不能被 truthiness 判断吞掉。

        `RunSqlRequest.with_db_config()` 内部是 `if port:`，而 `or` 回退同理——
        两处都要按「未设置」而不是「假值」判断。
        """
        step = Step(
            RunSqlRequest("explicit zero port")
            .with_db_config(ip="10.0.0.9", user="u", password="p", database="d", port=0)
            .fetchall("select 1")
        )

        self._run_case([step], self._config(port=3307))

        self.assertIn(":0/", _FakeDBEngine.instances[-1].db_uri)


class TestBatch0920DbPsmIsDeadConfig(unittest.TestCase):
    """0920 批次 5 / **N29**：`config.db.psm` 是**死配置**。

    ## 修复前的现场（`.tmp_audit/n29_e2e.py`）

    合并逻辑确实把 `psm` 读进了 `db_config`，但拼 DSN 时**只**用
    `user/password/ip/port/database` —— `psm` 在 `step_sql_request.py` 里
    **没有第二处读取**（对照 thrift 侧：那里的 `psm` 至少进请求日志与 `type()` 命名，
    见 `step_thrift_request.py:305,419`，所以那边刻意不拦）。

    只配 `psm`、不配 `ip` 时 DSN 变成：

    ```text
    mysql+pymysql://:SUPERSECRET@None:3306/None?charset=utf8mb4
    ```

    连接打向字面量主机 `None`，报错看起来像 **DNS/网络问题**，
    而 `docs/能力清单.md` 把 `psm` 列为受支持字段、限制说明里也没提它。
    """

    def setUp(self):
        _FakeDBEngine.instances = []
        self._original_engine_module = sys.modules.get(
            "interfacetester.database.engine"
        )
        fake_module = types.ModuleType("interfacetester.database.engine")
        fake_module.DBEngine = _FakeDBEngine
        sys.modules["interfacetester.database.engine"] = fake_module
        self._sql_ready_patch = mock.patch.object(step_sql_request, "SQL_READY", True)
        self._sql_ready_patch.start()

    def tearDown(self):
        self._sql_ready_patch.stop()
        if self._original_engine_module is None:
            sys.modules.pop("interfacetester.database.engine", None)
        else:
            sys.modules["interfacetester.database.engine"] = (
                self._original_engine_module
            )

    @staticmethod
    def _run_case(steps, config):
        class _Case(InterfaceTester):
            pass

        _Case.config = config
        _Case.teststeps = steps
        return _Case().test_start()

    def test_psm_without_ip_raises_actionable_error(self):
        """只配 `psm` → 必须在建连前响亮报错（而不是去打主机 `None`）。"""
        config = Config("psm only")
        config.db().psm("my.db.service").user("u").password("p").database("d")
        step = Step(RunSqlRequest("psm only").fetchall("select 1"))

        with self.assertRaises(exceptions.ParamsError) as ctx:
            self._run_case([step], config)

        message = str(ctx.exception)
        self.assertIn("psm", message)
        self.assertIn("my.db.service", message)
        self.assertIn("debugtalk.py", message, "要给出替代写法")
        # **不能**真的去建连（修复前会拿 None 当主机去连）
        self.assertEqual(
            _FakeDBEngine.instances, [], "报错前就已经拿 `None` 当主机去建连了"
        )

    def test_psm_with_ip_is_allowed_but_visible(self):
        """`psm` + `ip` 同时给 → 放行，但必须告警说明 `psm` 不参与连接。"""
        config = Config("psm and ip")
        config.db().psm("my.db.service").ip("10.1.2.3").user("u").password("p").database("d")
        step = Step(RunSqlRequest("psm and ip").fetchall("select 1"))

        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            self._run_case([step], config)
        finally:
            logger.remove(sink_id)

        dsn = _FakeDBEngine.instances[-1].db_uri
        self.assertIn("10.1.2.3", dsn, "连接目标应当由 ip 决定")
        self.assertNotIn("my.db.service", dsn, "psm 不该出现在 DSN 里")

        text = "\n".join(str(m) for m in messages)
        self.assertIn("psm", text)
        self.assertIn("不参与连接", text)

    def test_no_psm_means_no_extra_warning(self):
        """反向护栏：没配 `psm` 时不许产生噪音告警。"""
        config = Config("no psm")
        config.db().ip("10.9.9.9").user("u").password("p").database("d")
        step = Step(RunSqlRequest("no psm").fetchall("select 1"))

        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            self._run_case([step], config)
        finally:
            logger.remove(sink_id)

        self.assertEqual([m for m in messages if "psm" in str(m)], [], messages)


# ---------------------------------------------------------------------------
# 0919-9 批次 B（§三.3）：SQL step 的 `StepResult.data` 必须**每步一份**
#
# 与 thrift 侧、以及 HTTP 侧（`client.py:233`）是**同一条口径**：一个 step 的记录
# 不能与别的 step 共用同一个 `SessionData` 对象。
# ---------------------------------------------------------------------------
class TestSqlStepResultDataIsolation(unittest.TestCase):
    """修复前实测：

    - 两个 SQL step：`step_results[0].data is step_results[1].data` 为真，
      第 1 步（没声明断言）的记录里显示的是**第 2 步**的 validators；
    - `HTTP step` 后接 `SQL step`：SQL 那条记录里残留上一个 HTTP 请求的
      `req_resps` / `stat` / `address`（实测 req_resps=1、server_ip='127.0.0.1'）。
    """

    def setUp(self):
        _FakeDBEngine.instances = []
        self._original_engine_module = sys.modules.get(
            "interfacetester.database.engine"
        )
        fake_module = types.ModuleType("interfacetester.database.engine")
        fake_module.DBEngine = _FakeDBEngine
        sys.modules["interfacetester.database.engine"] = fake_module
        self._sql_ready_patch = mock.patch.object(step_sql_request, "SQL_READY", True)
        self._sql_ready_patch.start()

    def tearDown(self):
        self._sql_ready_patch.stop()
        if self._original_engine_module is None:
            sys.modules.pop("interfacetester.database.engine", None)
        else:
            sys.modules["interfacetester.database.engine"] = (
                self._original_engine_module
            )

    @staticmethod
    def _sql_step(name, with_validator=False):
        chain = (
            RunSqlRequest(name)
            .with_db_config(
                ip="127.0.0.1", user="tester", password="pw", database="school"
            )
            .fetchall("select * from student")
        )
        if with_validator:
            return Step(chain.validate().assert_equal("[0].name", "alice"))
        return Step(chain)

    @staticmethod
    def _case(steps, base_url=None):
        config = Config("sql data isolation")
        if base_url:
            config.base_url(base_url)

        class _Case(InterfaceTester):
            pass

        _Case.config = config
        _Case.teststeps = steps
        return _Case()

    def test_two_sql_steps_do_not_share_step_data(self):
        summary = (
            self._case([self._sql_step("sql 1"), self._sql_step("sql 2", True)])
            .test_start()
            .get_summary()
        )

        first, second = summary.step_results[0].data, summary.step_results[1].data
        self.assertIsNot(first, second)
        self.assertEqual(
            first.validators, {}, "第 1 步没有断言，记录里就不该有断言结果"
        )
        self.assertTrue(second.validators)

    def test_sql_step_after_http_step_has_no_http_leftover(self):
        from interfacetester import RunRequest
        from interfacetester.utils import HTTP_BIN_URL

        summary = (
            self._case(
                [Step(RunRequest("http step").get("/get")), self._sql_step("sql step")],
                base_url=HTTP_BIN_URL,
            )
            .test_start()
            .get_summary()
        )

        http_data, sql_data = summary.step_results[0].data, summary.step_results[1].data
        self.assertIsNot(http_data, sql_data)
        self.assertTrue(http_data.req_resps)
        self.assertEqual(sql_data.req_resps, [])
        self.assertEqual(sql_data.stat.elapsed_ms, 0)
        self.assertEqual(sql_data.address.server_ip, "N/A")
