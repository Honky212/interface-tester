import base64
import contextlib
import importlib
import json
import platform
import sys
import types
import unittest
from unittest import mock

from loguru import logger

from interfacetester import (
    Config,
    InterfaceTester,
    RunThriftRequest,
    Step,
    exceptions,
)
from interfacetester import step_thrift_request
from interfacetester.models import TConfigThrift, TThriftRequest
from interfacetester.step_thrift_request import ensure_thrift_ready

try:
    from interfacetester.thrift.thrift_client import ThriftClient

    THRIFT_CLIENT_IMPORTABLE = True
    THRIFT_CLIENT_IMPORT_ERROR = ""
except Exception as ex:  # thriftpy2/thrift 未安装（Windows 上属正常情况）
    THRIFT_CLIENT_IMPORTABLE = False
    THRIFT_CLIENT_IMPORT_ERROR = f"{type(ex).__name__}: {ex}"

IS_WINDOWS = platform.system() == "Windows"


class _FakeThriftClient(object):
    """不依赖 thrift 依赖的替身 client。

    既用于验证 runner 是否释放（0918 之前的用例），也用于 0918-4 批次观察
    `ThriftClient(...)` 的构造参数（M2/L5）与请求内容。
    """

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.close_called = 0
        self.requests = []
        _FakeThriftClient.instances.append(self)

    def send_request(self, params, method):
        self.requests.append((params, method))
        return {"ok": True, "method": method}

    def close(self):
        self.close_called += 1


class _ThriftReleaseCase(InterfaceTester):
    config = Config("thrift client release")
    teststeps = []


class TestThriftFailFast(unittest.TestCase):
    """thrift step 的 fail fast：Windows 上必须「一构造就报错」。

    NOTICE: 修复前平台检查只在真正发起请求时才执行，构造 thrift step 完全看不出来，
    问题会拖到用例跑到该 step 时才暴露（此时前序步骤已经白跑了）。
    """

    @unittest.skipUnless(IS_WINDOWS, "非 Windows 平台不适用本用例")
    def test_fails_fast_on_windows(self):
        with self.assertRaises(RuntimeError) as cm:
            RunThriftRequest("thrift step")

        message = str(cm.exception)
        self.assertIn("Windows", message)
        # 提示里要给出可执行的下一步，而不是只报「不支持」
        self.assertIn("Linux/macOS", message)

    @unittest.skipIf(IS_WINDOWS, "Windows 上 thrift 不可用")
    def test_constructible_on_supported_platform(self):
        step = RunThriftRequest("thrift step")
        self.assertEqual(step.name(), "thrift step")
        self.assertIsNotNone(step.struct().thrift_request)

    @unittest.skipIf(
        (not IS_WINDOWS) and THRIFT_READY,
        "平台支持且依赖齐全时不适用本用例",
    )
    def test_ensure_thrift_ready_raises_instead_of_exit(self):
        """依赖缺失（或平台不支持）时抛异常，而不是 sys.exit(1) 杀掉整个 pytest 会话。"""
        with self.assertRaises(RuntimeError):
            ensure_thrift_ready()


class TestThriftClientRelease(unittest.TestCase):
    """thrift 连接的释放：与 db_engine 一样在 test_start 的 finally 里关掉。"""

    def test_test_start_releases_injected_thrift_client(self):
        fake_client = _FakeThriftClient()
        runner = _ThriftReleaseCase().with_thrift_client(fake_client)

        runner.test_start()

        self.assertEqual(fake_client.close_called, 1)
        self.assertIsNone(runner.thrift_client)

    def test_test_start_tolerates_client_without_close(self):
        """外部注入的对象不一定有 close()，不能因此让用例收尾阶段报错。"""
        runner = _ThriftReleaseCase().with_thrift_client(object())

        runner.test_start()  # 不应抛异常

        self.assertIsNone(runner.thrift_client)

    def test_test_start_is_noop_without_thrift_client(self):
        runner = _ThriftReleaseCase()

        runner.test_start()

        self.assertIsNone(runner.thrift_client)


@unittest.skipUnless(
    THRIFT_CLIENT_IMPORTABLE,
    f"thrift/thriftpy2 未安装，跳过 ThriftClient 自身用例（{THRIFT_CLIENT_IMPORT_ERROR}）",
)
class TestThriftClientClose(unittest.TestCase):
    """ThriftClient.close() / __del__ 的健壮性（需 thrift 依赖，Linux/macOS 上执行）。"""

    def test_close_is_safe_when_init_failed(self):
        """初始化失败（client 为 None）时 close()/__del__ 都不能抛异常。

        NOTICE（0919-8 / §三.2）：`__init__` 现在**失败即抛**（原来只记日志后返回），
        所以「client 为 None 的半成品」不再能由构造函数产生；这里手工构造该状态
        （`__new__` + 显式置 None），继续守住 close()/__del__ 的健壮性 ——
        它是最后一个可能对 None 调 close 的入口（如解释器退出期）。
        """
        client = ThriftClient.__new__(ThriftClient)
        client.thrift_module = None
        client.thrift_service_obj = None
        client.client = None

        client.close()
        client.close()  # 幂等
        del client  # 触发 __del__

    def test_missing_idl_raises_instead_of_returning_half_built_client(self):
        """§三.2（这条需要真实 thrift 依赖）：IDL 不存在时必须报出根因。

        修复前：构造函数静默返回，随后 `send_request` 抛
        `AttributeError: 'NoneType' object has no attribute 'ping_args'`。
        与 `TestThriftClientInitSurfacesRootCause` 是同一个契约，
        区别只在「用真实 thriftpy2」还是「用替身」（后者在本机能真实执行）。
        """
        with self.assertRaises(exceptions.ParamsError) as ctx:
            ThriftClient(
                thrift_file="not_exist.thrift",
                service_name="NotExistService",
                ip="127.0.0.1",
                port=1,
            )

        message = str(ctx.exception)
        self.assertIn("not_exist.thrift", message)
        self.assertIn("NotExistService", message)
        self.assertIsInstance(ctx.exception.__cause__, FileNotFoundError)

    def test_close_releases_underlying_client(self):
        client = ThriftClient.__new__(ThriftClient)  # 绕过构造函数，避免真实连接
        underlying = mock.Mock()
        client.client = underlying

        client.close()

        underlying.close.assert_called_once()
        self.assertIsNone(client.client)


class _FakeTType(object):
    """thrift.Thrift.TType 的最小替身（只为在未安装 thrift 的环境导入 data_convertor）。"""

    STRUCT = 12
    LIST = 15
    SET = 14
    MAP = 13
    STRING = 11
    DOUBLE = 4
    I64 = 10
    I32 = 8
    I16 = 6
    BYTE = 3
    BOOL = 2


def _import_data_convertor():
    """导入 data_convertor（必要时注入 TType 替身）。

    NOTICE: 该模块顶部有 `from thrift.Thrift import TType`，未安装 thrift 时无法直接导入。
    这里按需注入替身，导入完成后立刻把替身从 sys.modules 移除，避免影响其它模块。
    """
    injected = []
    if "thrift" not in sys.modules:
        thrift_pkg = types.ModuleType("thrift")
        thrift_mod = types.ModuleType("thrift.Thrift")
        thrift_mod.TType = _FakeTType
        thrift_pkg.Thrift = thrift_mod
        sys.modules["thrift"] = thrift_pkg
        sys.modules["thrift.Thrift"] = thrift_mod
        injected = ["thrift", "thrift.Thrift"]

    try:
        from interfacetester.thrift import data_convertor

        return data_convertor
    finally:
        for module_name in injected:
            sys.modules.pop(module_name, None)


class TestDataConvertorPureFunctions(unittest.TestCase):
    """data_convertor 里不依赖真实 thrift 连接的纯函数（本次清理涉及的部分）。

    覆盖点：删掉不可达分支、去掉内置 `str` 遮蔽、把裸 `except:` 改成 `except Exception:`
    之后，函数行为必须与清理前一致（或更严格）。
    """

    @classmethod
    def setUpClass(cls):
        cls.data_convertor = _import_data_convertor()

    def test_unicode_2_utf8_keep_native_keeps_values(self):
        convert = self.data_convertor.unicode_2_utf8_keep_native

        self.assertEqual(convert("中文"), "中文")
        self.assertEqual(convert([1, "a"]), [1, "a"])
        self.assertEqual(convert({"k": ["v"]}), {"k": ["v"]})
        self.assertEqual(convert((1, 2)), (1, 2))
        self.assertIsNone(convert(None))
        self.assertEqual(convert(3), 3)

    def test_dumper_serializes_object(self):
        class _Obj(object):
            def __init__(self):
                self.x = 1

        self.assertEqual(json.loads(self.data_convertor.dumper({"a": 1})), {"a": 1})
        self.assertEqual(json.loads(self.data_convertor.dumper(_Obj())), {"x": 1})

    def test_dumper_does_not_swallow_base_exceptions(self):
        """裸 `except:` 修好后，KeyboardInterrupt/SystemExit 不再被吞掉。

        修复前 `except:` 会把 Ctrl+C（KeyboardInterrupt）一起吃掉，然后回落到
        `return obj.__dict__`；对没有 `__dict__` 的对象会再抛一个与真实原因无关的错误。
        """

        class _SlotsOnly(object):
            __slots__ = ()

        with mock.patch.object(
            self.data_convertor.json, "dumps", side_effect=KeyboardInterrupt()
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.data_convertor.dumper(_SlotsOnly())

    def test_thrift2dict_returns_dict(self):
        # 修复前这里用 `str` 当变量名遮蔽内置 str，行为相同但容易踩坑
        result = self.data_convertor.thrift2dict({"a": [1, 2], "b": "x"})
        self.assertEqual(result, {"a": [1, 2], "b": "x"})


class TestDataConvertorNonStructReturns(unittest.TestCase):
    """0918-8 / M28：非 struct 返回值不能崩。

    修复前 `ThriftJSONEncoder.encode` 里还留着 python2 的 `self.encoding` 分支，
    而 py3 的 `json.JSONEncoder` 没有该属性；字符串走的就是那个分支，于是
    「thrift 方法返回字符串」（`string ping()`）时 `thrift2dict()` 必然抛
    `AttributeError: 'ThriftJSONEncoder' object has no attribute 'encoding'`。
    奇怪的是 `int`/`bool`/`list` 返回值一直正常——它们走 C 编码器，绕开了该分支，
    这也是这个坑长期没被发现的原因（回归用例把这条差异也钉住）。
    """

    @classmethod
    def setUpClass(cls):
        cls.data_convertor = _import_data_convertor()

    def test_string_return_is_serializable(self):
        """`string ping()` 的返回值必须能序列化/反序列化。"""
        self.assertEqual(self.data_convertor.thrift2json("pong"), '"pong"')
        self.assertEqual(self.data_convertor.thrift2dict("pong"), "pong")

    def test_non_ascii_string_return_keeps_native_text(self):
        """ensure_ascii=False：中文不能被转成 \\uXXXX（报告里要能直接看）。"""
        self.assertEqual(self.data_convertor.thrift2dict("中文响应"), "中文响应")

    def test_scalar_returns_do_not_crash(self):
        for value, expected in [
            ("pong", "pong"),
            (5, 5),
            (True, True),
            (None, None),
            ([1, 2], [1, 2]),
            (b"pong", "pong"),
            (bytearray(b"pong"), "pong"),
            ((1, 2), [1, 2]),
        ]:
            with self.subTest(value=value):
                self.assertEqual(self.data_convertor.thrift2dict(value), expected)

    def test_set_return_becomes_list(self):
        """`set<...>` 返回值：json 不认识 set，按 struct 内 SET 字段的既有口径转数组。"""
        self.assertEqual(
            sorted(self.data_convertor.thrift2dict({"a", "b"})), ["a", "b"]
        )

    def test_struct_return_unchanged(self):
        class _Pong(object):
            thrift_spec = {0: (11, "message", None, None)}

        pong = _Pong()
        pong.message = "pong"

        self.assertEqual(self.data_convertor.thrift2dict(pong), {"message": "pong"})

    def test_unserializable_object_still_raises_type_error(self):
        """真正无法序列化的对象仍要报 TypeError（不能为了「宽容」把它吞掉）。"""
        with self.assertRaises(TypeError):
            self.data_convertor.thrift2dict(object())


# ---------------------------------------------------------------------------
# 0918-4 批次：thrift step 的一致性修复
#   M1  字符串形式的 thrift_client 从不解析 → 运行期裸 AttributeError
#   M2  同用例换服务/换 idl 时静默复用旧 client（db 侧已修，thrift 漏修）
#   L5  用 `or` 做默认值回退：显式 0 被吞、且 config 值实际不可达
#   L4  Allure「response details」附件贴的是**请求**文本（复制粘贴错变量）
#
# NOTICE: thrift step 只能手写 pytest 代码（`make.py` 的生成期闸门明确不支持
# `thrift_request` 写在 YAML 里），所以这一组缺陷的实际影响面正是 Python API。
# ---------------------------------------------------------------------------

THRIFT_CLIENT_MODULE = "interfacetester.thrift.thrift_client"


@contextlib.contextmanager
def _thrift_offline_environment():
    """让 thrift step 在 Windows / 无 thrift 依赖的环境下也能走完整条链路。

    NOTICE: `RunThriftRequest.__init__` 有 fail fast 平台检查（Windows 上直接报错），
    `run_step_thrift_request` 又会调 `ensure_thrift_ready()`。两者都是本模块的模块级
    函数，patch 掉即可离线覆盖，不必真的安装 thrift / thriftpy2。
    """
    with mock.patch.object(
        step_thrift_request, "ensure_thrift_platform_supported", lambda: None
    ), mock.patch.object(step_thrift_request, "ensure_thrift_ready", lambda: None):
        yield


@contextlib.contextmanager
def _fake_thrift_client_ctor():
    """把「用到才 import」的 `ThriftClient` 换成替身，以便观察构造参数。

    NOTICE: `run_step_thrift_request` 里是
    `from interfacetester.thrift.thrift_client import ThriftClient`（函数内 import），
    因此替换 `sys.modules` 即可生效（与 step_sql_request_test 的 DBEngine 替身同款）。
    """
    original = sys.modules.get(THRIFT_CLIENT_MODULE)
    fake_module = types.ModuleType(THRIFT_CLIENT_MODULE)
    fake_module.ThriftClient = _FakeThriftClient
    sys.modules[THRIFT_CLIENT_MODULE] = fake_module
    try:
        yield fake_module
    finally:
        if original is None:
            sys.modules.pop(THRIFT_CLIENT_MODULE, None)
        else:
            sys.modules[THRIFT_CLIENT_MODULE] = original


class TestThriftStepConsistency(unittest.TestCase):
    """thrift step 的 client 解析、复用与默认值口径（M1/M2/L5/L4）。"""

    def setUp(self):
        _FakeThriftClient.instances = []

    @staticmethod
    def _config(**thrift_fields):
        config = Config("thrift consistency case")
        builder = config.thrift()
        for key, value in thrift_fields.items():
            getattr(builder, key)(value)
        return config

    @classmethod
    def _case(cls, steps, **thrift_fields):
        class _Case(InterfaceTester):
            pass

        _Case.config = cls._config(**thrift_fields)
        _Case.teststeps = steps
        return _Case()

    @staticmethod
    def _request_step(name, idl="a.thrift", ip="10.0.0.1", port=1111, teardown_hook=None):
        request = (
            RunThriftRequest(name).with_idl_path(idl, "/idl").with_ip(ip).with_port(port)
        )
        if teardown_hook:
            request.teardown_hook(teardown_hook)
        return Step(request)

    @classmethod
    def _case_capturing_request(cls, steps, captured, **thrift_fields):
        """跑用例并通过 **teardown hook** 把 `$thrift_request`（step 变量）抓出来。

        NOTICE: `StepResult` 里**没有** step_variables，所以不能从 summary 里取；
        hook 是框架公开的取值途径（`call_hooks` 会把 step_variables 传给函数），
        用它观察「step 最终解析出来的请求字典」最贴近真实用法。
        """

        class _Case(InterfaceTester):
            pass

        _Case.config = cls._config(**thrift_fields)
        _Case.teststeps = steps
        original_setup = _Case._setup_runner

        def _setup_with_recorder(self):
            original_setup(self)
            self.parser.functions_mapping["_capture_thrift_request"] = (
                lambda request: captured.update(request)
            )

        _Case._setup_runner = _setup_with_recorder
        return _Case()

    # ---------------------------------------------------------------- M1
    def test_string_thrift_client_raises_actionable_error(self):
        """M1：`with_thrift_client("名字")` 不能变成裸 AttributeError。

        修复前：字符串被当成 client 直接赋给 `runner.thrift_client`（**真值**），
        于是既跳过建连、也跳过 `ensure_thrift_ready()` 的平台/依赖检查，
        直到 `runner.thrift_client.send_request(...)` 才炸出
        `AttributeError: 'str' object has no attribute 'send_request'`——
        此时 setup_hooks 已经执行过了，错误位置离真正原因很远。
        """
        with _thrift_offline_environment():
            request = RunThriftRequest("string client").with_thrift_client("my_client")
            request.with_idl_path("a.thrift", "/idl")
            runner = self._case([])
            runner._setup_runner()

            with self.assertRaises(exceptions.ParamsError) as cm:
                step_thrift_request.run_step_thrift_request(runner, request.struct())

        message = str(cm.exception)
        self.assertIn("my_client", message)
        # 必须给出可执行的下一步，而不是只说「错了」
        self.assertIn("with_thrift_client", message)
        # 没有被半应用：既没塞进 runner，也没触发建连
        self.assertIsNone(runner.thrift_client)
        self.assertEqual(_FakeThriftClient.instances, [])

    def test_expression_thrift_client_still_resolves(self):
        """M1 的反向断言：`${...}` 形式本来就支持（parse_data 会解析），不能被误伤。

        这是 thrift client 唯一真正可用的字符串写法：在 debugtalk.py 里定义
        一个返回 client 的函数，然后在用例里写成 `${func_name()}`。
        """
        injected_client = _FakeThriftClient()

        with _thrift_offline_environment():
            request = RunThriftRequest("expression client").with_thrift_client(
                "${get_thrift_client()}"
            )
            request.with_idl_path("a.thrift", "/idl")
            runner = self._case([])
            runner._setup_runner()
            runner.parser.functions_mapping["get_thrift_client"] = lambda: injected_client

            step_thrift_request.run_step_thrift_request(runner, request.struct())

        self.assertIs(runner.thrift_client, injected_client)
        # 表达式解析出来的对象**不经过** ThriftClient 构造函数：
        # 列表里只应该有上面手动构造的那一个
        self.assertEqual(len(_FakeThriftClient.instances), 1)
        self.assertIs(_FakeThriftClient.instances[0], injected_client)

    # ---------------------------------------------------------------- M2
    def test_client_is_rebuilt_when_target_changes(self):
        """M2：同一用例内第二个 step 换服务/idl/ip/port 时必须重建连接。

        修复前只判 `if not runner.thrift_client`，第二个 step 的配置被**静默忽略**，
        请求全部打到第一个服务上（db 侧同类 bug 已在 0916-7 修掉，thrift 漏修）。
        """
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            case = self._case(
                [
                    self._request_step("svc A", idl="a.thrift", ip="10.0.0.1", port=1111),
                    self._request_step("svc B", idl="b.thrift", ip="10.0.0.2", port=2222),
                ]
            )
            case.test_start()

        self.assertEqual(len(_FakeThriftClient.instances), 2)
        self.assertEqual(_FakeThriftClient.instances[0].kwargs["ip"], "10.0.0.1")
        self.assertEqual(_FakeThriftClient.instances[1].kwargs["ip"], "10.0.0.2")
        self.assertEqual(_FakeThriftClient.instances[1].kwargs["thrift_file"], "b.thrift")
        # 旧连接要被关掉，否则换服务只是「泄漏一个连接 + 多建一个」
        self.assertEqual(_FakeThriftClient.instances[0].close_called, 1)

    def test_same_target_reuses_client(self):
        """M2 的反向断言：目标没变时必须复用（不能每次 step 都重连）。"""
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            case = self._case(
                [
                    self._request_step("svc A #1"),
                    self._request_step("svc A #2"),
                ]
            )
            case.test_start()

        self.assertEqual(len(_FakeThriftClient.instances), 1)
        # 两个 step 都由同一个 client 发出请求 = 真正复用了连接
        # （NOTICE: 不能用 close_called==0 判断——用例收尾时 runner 会释放它）
        self.assertEqual(len(_FakeThriftClient.instances[0].requests), 2)

    def test_externally_injected_client_is_reused(self):
        """M2 的边界：外部注入的 client 配置未知 → 沿用「一直复用」语义。

        与 db 侧 `with_db_engine()` 的口径一致：注入的对象无法比对配置，
        因此不因 step 里的 db_config/thrift 配置不同而重建。
        """
        injected_client = _FakeThriftClient()

        with _thrift_offline_environment():
            case = self._case(
                [self._request_step("injected", ip="10.0.0.9", port=9999)]
            )
            case.with_thrift_client(injected_client)
            case.test_start()

        self.assertEqual(len(_FakeThriftClient.instances), 1)
        self.assertIs(_FakeThriftClient.instances[0], injected_client)

    # ---------------------------------------------------------------- L5
    def test_explicit_zero_port_and_timeout_are_not_swallowed(self):
        """L5：显式 `port: 0` / `timeout: 0` 不能被 `or` 回退吞掉。

        HTTP 侧（step_request）已统一用 `is None` 并留了注释说明 `timeout: 0`
        是合法值；thrift/sql 两侧漏改。
        """
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            request = (
                RunThriftRequest("explicit zero")
                .with_idl_path("a.thrift", "/idl")
                .with_ip("10.0.0.1")
                .with_port(0)
            )
            request.struct().thrift_request.timeout = 0
            # config 给非 0 值，才能把「显式 0 生效」与「回退到 config」区分开
            case = self._case([Step(request)], port=9000, timeout=10)
            case.test_start()

        kwargs = _FakeThriftClient.instances[-1].kwargs
        self.assertEqual(kwargs["port"], 0)
        self.assertEqual(kwargs["timeout"], 0)

    def test_config_timeout_is_reachable_when_step_leaves_it_unset(self):
        """L5 的另一半（同一行代码）：`config.thrift.timeout` 必须是**可达**的。

        `TThriftRequest.timeout` 默认值是 10（真值），于是
        `parsed["timeout"] or config.thrift.timeout` 里的回退分支**永远不会执行**——
        config 里写的 timeout 被静默忽略。修复方式是让 step 侧默认「未设置」。
        """
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            case = self._case([self._request_step("no explicit timeout")], timeout=30)
            case.test_start()

        self.assertEqual(_FakeThriftClient.instances[-1].kwargs["timeout"], 30)

    def test_config_port_is_reachable_when_step_leaves_it_unset(self):
        """L5 的对照组：`port` 是 `Optional[int] = None`，config 本来就可达。

        留这条用例是为了锁定「同一次修复不能把这一半弄坏」。
        """
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            request = (
                RunThriftRequest("no explicit port")
                .with_idl_path("a.thrift", "/idl")
                .with_ip("10.0.0.1")
            )
            request.struct().thrift_request.port = None
            case = self._case([Step(request)], port=9000)
            case.test_start()

        self.assertEqual(_FakeThriftClient.instances[-1].kwargs["port"], 9000)

    # ---------------------------------------------------------------- L13
    def test_config_env_and_cluster_are_reachable(self):
        """L13：`config.thrift.env` / `config.thrift.cluster` 必须是**可达**的。

        修复前 `TThriftRequest.env` 默认 `"prod"`、`cluster` 默认 `"default"` —— 都是**真值**，
        于是 `parsed[...] or config.thrift.env` 的回退分支**永远不执行**，
        config 里配的值被静默忽略（与 L5 的 timeout 同一根因，只是这两个字段只进日志文本）。

        做法同 L5：step 侧默认改成 `None`（未设置），config 侧保留 `prod`/`default`，
        因此「两边都不配」的结果与修复前一致，而「只在 config 里配」终于生效。
        """
        captured = {}
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            case = self._case_capturing_request(
                [
                    self._request_step(
                        "no explicit env/cluster",
                        teardown_hook="${_capture_thrift_request($thrift_request)}",
                    )
                ],
                captured,
                env="test",
                cluster="my-cluster",
            )
            case.test_start()

        self.assertEqual(captured["env"], "test")
        self.assertEqual(captured["cluster"], "my-cluster")

    def test_env_and_cluster_defaults_are_unchanged_when_nothing_is_set(self):
        """L13 的反向断言：两边都不配时结果必须还是 `prod` / `default`（修复前行为）。"""
        captured = {}
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            case = self._case_capturing_request(
                [
                    self._request_step(
                        "no env config at all",
                        teardown_hook="${_capture_thrift_request($thrift_request)}",
                    )
                ],
                captured,
            )
            case.test_start()

        self.assertEqual(captured["env"], "prod")
        self.assertEqual(captured["cluster"], "default")

    def test_step_level_env_wins_over_config(self):
        """L13 的边界：step 显式给了值 → 优先于 config。"""
        captured = {}
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            step = self._request_step(
                "explicit env",
                teardown_hook="${_capture_thrift_request($thrift_request)}",
            )
            step.struct().thrift_request.env = "staging"
            case = self._case_capturing_request(
                [step], captured, env="test", cluster="my-cluster"
            )
            case.test_start()

        self.assertEqual(captured["env"], "staging")
        # 没显式给的那个仍然从 config 取
        self.assertEqual(captured["cluster"], "my-cluster")

    # ---------------------------------------------------------------- L4
    def test_allure_response_attachment_uses_response_text(self):
        """L4：Allure 的「response details」附件贴的必须是**响应**文本。

        修复前这里传的是 `thrift_request_print`（复制粘贴错变量），
        于是报告里两个附件内容一样，排查时看不到任何响应信息。
        """
        attachments = []
        fake_allure = mock.Mock()
        fake_allure.attach.side_effect = lambda body, **kwargs: attachments.append(
            (kwargs.get("name"), body)
        )
        fake_allure.attachment_type = mock.Mock(TEXT="text")

        with _thrift_offline_environment(), _fake_thrift_client_ctor(), mock.patch.object(
            step_thrift_request, "ALLURE", fake_allure
        ):
            case = self._case([self._request_step("allure attachment")])
            case.test_start()

        by_name = dict(attachments)
        response_text = by_name["thrift response details"]
        # 替身 client 的响应体是 {"ok": True, "method": ...}
        self.assertIn("ok", response_text)
        # `psm` 只出现在请求文本里，用来证明贴的确实不是请求
        self.assertNotIn("psm", response_text)


# ---------------------------------------------------------------------------
# 0918-8 批次：M28 thrift 非 struct 返回值
#   `data_convertor.ThriftJSONEncoder.encode` 的 py2 `self.encoding` 分支
#   （字符串返回值必崩）+ step 里 `resp.items()` 对非 dict 响应必崩。
# ---------------------------------------------------------------------------


class _ScalarThriftClient(object):
    """替身 client：返回**非 struct** 结果（str/int/list...），模拟 `string ping()`。"""

    def __init__(self, response):
        self.response = response
        self.requests = []
        self.close_called = 0

    def send_request(self, params, method):
        self.requests.append((params, method))
        return self.response

    def close(self):
        self.close_called += 1


class TestThriftNonStructResponse(unittest.TestCase):
    """M28 的第二半：step 自己也不能假设 thrift 响应是 dict。

    `run_step_thrift_request` 里 `for k, v in resp.items()` 在响应是标量时抛
    `AttributeError: 'str' object has no attribute 'items'`——错误信息完全看不出
    问题出在「thrift 方法返回的是标量」。修复后标量响应照常打印、断言走 `@` 取整体。
    """

    @staticmethod
    def _case(response, expect=None):
        request = RunThriftRequest("scalar response").with_idl_path(
            "a.thrift", "/idl"
        )
        request.with_thrift_client(_ScalarThriftClient(response))
        validation = request.validate()
        if expect is not None:
            validation.assert_equal("@", expect)

        class _Case(InterfaceTester):
            config = Config("thrift scalar response")
            teststeps = [Step(request)]

        return _Case()

    def test_string_response_does_not_crash_step(self):
        with _thrift_offline_environment():
            case = self._case("pong", expect="pong")

            case.test_start()

        summary = case.get_summary()
        self.assertTrue(summary.success)

    def test_int_response_does_not_crash_step(self):
        with _thrift_offline_environment():
            case = self._case(5, expect=5)

            case.test_start()

        self.assertTrue(case.get_summary().success)

    def test_scalar_response_is_logged_with_type(self):
        """报告里必须看得出「这是个标量响应」，而不是一行空内容或干脆没有。"""
        attachments = self._attachments_for("pong", expect="pong")

        response_text = dict(attachments)["thrift response details"]
        self.assertIn("<str>", response_text)
        self.assertIn("pong", response_text)

    def test_struct_response_still_prints_fields(self):
        """回归：struct 响应的逐字段打印行为不变。"""
        attachments = self._attachments_for({"ok": True}, expect={"ok": True})

        response_text = dict(attachments)["thrift response details"]
        self.assertIn("ok: True", response_text)

    @classmethod
    def _attachments_for(cls, response, expect):
        """跑一个 thrift step 并抓出 Allure 附件（响应文本的唯一可见出口）。"""
        attachments = []
        fake_allure = mock.Mock()
        fake_allure.attach.side_effect = lambda body, **kwargs: attachments.append(
            (kwargs.get("name"), body)
        )
        fake_allure.attachment_type = mock.Mock(TEXT="text")

        with _thrift_offline_environment(), mock.patch.object(
            step_thrift_request, "ALLURE", fake_allure
        ):
            cls._case(response, expect=expect).test_start()

        return attachments

    def test_failed_assertion_on_scalar_still_reports_details(self):
        """断言失败时仍要打印详情（修复前是 AttributeError，压根到不了这一步）。"""
        with _thrift_offline_environment():
            case = self._case("pong", expect="pang")

            with self.assertRaises(exceptions.ValidationFailure):
                case.test_start()


class TestThriftMissingConfigBlock(unittest.TestCase):
    """M31（0918-8 新发现）：`config.thrift` 未配置时不能抛裸 AttributeError。

    `TConfig.thrift` 的类型是 `Optional[TConfigThrift] = None`，而 Python API 里
    「只在 step 上写齐 idl/ip/port、不调 `config.thrift()`」是完全合法的写法；
    修复前这种用例第一步就抛
    `AttributeError: 'NoneType' object has no attribute 'psm'`——
    错误信息完全不涉及「配置块缺失」，用户只能靠猜。
    """

    @staticmethod
    def _case(captured, client):
        request = (
            RunThriftRequest("step-only thrift")
            .with_idl_path("a.thrift", "/idl")
            .with_ip("10.0.0.9")
            .with_port(1234)
        )
        request.with_thrift_client(client)
        request.teardown_hook("${_capture_thrift_request($thrift_request)}")
        request.validate().assert_equal("@", {"ok": True})

        class _Case(InterfaceTester):
            # NOTICE: 刻意**不**调 config.thrift()
            config = Config("no config.thrift block")
            teststeps = [Step(request)]

        original_setup = _Case._setup_runner

        def _setup_with_recorder(self):
            original_setup(self)
            self.parser.functions_mapping["_capture_thrift_request"] = (
                lambda request: captured.update(request)
            )

        _Case._setup_runner = _setup_with_recorder
        return _Case()

    def test_missing_config_thrift_block_falls_back_to_defaults(self):
        captured = {}
        with _thrift_offline_environment():
            case = self._case(captured, _ScalarThriftClient({"ok": True}))

            case.test_start()

        self.assertTrue(case.get_summary().success)
        # 回落到 TConfigThrift() 的模型默认值（等价于「config 里什么都不配」）
        defaults = TConfigThrift()
        self.assertEqual(captured["timeout"], defaults.timeout)
        self.assertEqual(captured["env"], defaults.env)
        self.assertEqual(captured["cluster"], defaults.cluster)
        # step 上显式给的值仍然优先
        self.assertEqual(captured["ip"], "10.0.0.9")
        self.assertEqual(captured["port"], 1234)
        self.assertEqual(captured["idl_path"], "a.thrift")


# ---------------------------------------------------------------------------
# 0919-8 批次 A：thrift 失败/字节处理的两处「根因脱节」
#   §三.6b  非 UTF-8 的 binary 字段让整个 step 抛 UnicodeDecodeError
#   §三.2   ThriftClient.__init__ 吞掉初始化异常，报错与根因完全脱节
#
# NOTICE: 这两条的**真实**触发环境是 Linux + 已装 thrift 依赖；本机（Windows、
# 无 thriftpy2）靠替身把链路跑通，因此新用例**大部分在本机能真实执行**，
# 不是 skip（见 `_import_thrift_client_with_fake_thriftpy2`）。
# ---------------------------------------------------------------------------


class TestPyEncodeBasestringAsciiPy3Fallback(unittest.TestCase):
    """0919-17 / ①：`py_encode_basestring_ascii` 的 py2 解码分支残留。

    修复前：`isinstance(s, str) and HAS_UTF8.search(s)` 命中时执行 `s.decode("utf-8")`。
    py3 的 str 没有 `.decode` —— 崩溃条件是「无 `_json` C 模块的解释器（此时本函数
    才被选为 `encode_basestring_ascii`）+ 值含 U+0080~U+00FF 码点」。
    本机 CPython 上它是死代码（C 加速器恒被选中），但这是**替身可验**的纯函数：
    直接调 `py_encode_basestring_ascii` 就能复现，不需要真的在 PyPy 上跑。
    """

    @classmethod
    def setUpClass(cls):
        cls.dc = _import_data_convertor()

    def test_latin1_range_string_no_longer_crashes(self):
        """é（U+00E9）命中旧判据 → 修复前 AttributeError，修复后正确转义成 \\uXXXX。"""
        self.assertEqual(self.dc.py_encode_basestring_ascii("café"), '"caf\\u00e9"')

    def test_matches_stdlib_oracle(self):
        """与 py3 标准库同名函数**逐字节一致**（本实现就是从标准库抄的）。

        oracle 用 `json.encoder.py_encode_basestring_ascii`——标准库自己那份
        py3 实现早就没有 py2 的解码分支，是这条修复的对齐目标。
        """
        import json.encoder

        samples = [
            "café",
            "naïve",
            "plain ascii",
            'quote " backslash \\ tab\tnewline\n',
            "中文",  # U+4E2D 不命中旧判据（>0xFF），但转义结果必须照旧正确
            "emoji \U0001F600",
            "",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(
                    self.dc.py_encode_basestring_ascii(sample),
                    json.encoder.py_encode_basestring_ascii(sample),
                )

    def test_ascii_and_control_chars_unchanged(self):
        """反向护栏：纯 ASCII 与控制字符的既有输出逐字不变。"""
        self.assertEqual(self.dc.py_encode_basestring_ascii("abc"), '"abc"')
        self.assertEqual(self.dc.py_encode_basestring_ascii("a\nb"), '"a\\nb"')


class TestDataConvertorBinaryPayload(unittest.TestCase):
    """§三.6b：bytes 的 JSON 口径收口在 `json_safe_bytes`，任何形状都不再崩。

    修复前的两个问题（同一个根因的两面）：
      1) `ThriftJSONEncoder.default` 写的是 `str(o, encoding="utf-8")`，非 UTF-8 的
         binary 载荷直接抛 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff ...`
         —— 报错里既没有字段名，也没有「这是 binary」的线索；
      2) struct 分支里两段「把 binary 字段 base64 编码」的代码**永远不可能执行**：
         判据 `type(val) in [str] and not istext(val)` 在 py3 恒假（`istext` 是 py2 语义），
         而 ttype 判据用的 `TType.BYTE`(3) 也匹配不上 binary —— thriftpy2 给 binary 的
         `thrift_spec` 打的是 **`TType.BINARY = 18`**（我在真机上用 thriftpy2 0.7.1 +
         真实 IDL 验证过：`Blob.thrift_spec == {1: (18, 'payload', False), ...}`，
         且 `read_val` 对 18 直接 `return bytes`）。
    """

    RAW = b"\xff\xfe\x00\x01not-utf8"  # 非空跑自检见 test_payload_really_is_not_utf8

    @classmethod
    def setUpClass(cls):
        cls.data_convertor = _import_data_convertor()

    @staticmethod
    def _spec(name, ttype, info=None):
        """thrift_spec 的条目形状：{tag: (ttype, field_name, ttype_info, default)}"""
        return {1: (ttype, name, info, None)}

    def _struct(self, name, ttype, value, info=None):
        class _Struct(object):
            pass

        obj = _Struct()
        obj.thrift_spec = self._spec(name, ttype, info)
        setattr(obj, name, value)
        return obj

    # ---------------------------------------------------------- 非空跑自检
    def test_payload_really_is_not_utf8(self):
        """自检：下面的载荷必须**真的**不是合法 UTF-8，否则那批断言等于白跑。"""
        with self.assertRaises(UnicodeDecodeError):
            self.RAW.decode("utf-8")
        self.assertEqual(
            self.data_convertor.json_safe_bytes(self.RAW),
            base64.b64encode(self.RAW).decode("ascii"),
        )

    # ---------------------------------------------------------- 不再崩
    def test_binary_field_ttype_18_does_not_raise(self):
        """真机上 binary 字段就是 ttype 18 —— 修复前这里是 UnicodeDecodeError。"""
        obj = self._struct("payload", 18, self.RAW)

        self.assertEqual(
            self.data_convertor.thrift2dict(obj),
            {"payload": base64.b64encode(self.RAW).decode("ascii")},
        )

    def test_bare_scalar_bytes_does_not_raise(self):
        """裸返回值（`binary get_blob()`）同样不能崩。"""
        self.assertEqual(
            self.data_convertor.thrift2dict(self.RAW),
            base64.b64encode(self.RAW).decode("ascii"),
        )

    def test_every_bytes_bearing_shape_is_json_safe(self):
        """一处口径覆盖所有形状：struct 字段 / list / set / 嵌套 dict / 裸标量。"""
        encoded = base64.b64encode(self.RAW).decode("ascii")
        cases = [
            ("struct field (18)", self._struct("p", 18, self.RAW), {"p": encoded}),
            ("struct field (11)", self._struct("p", 11, self.RAW), {"p": encoded}),
            ("struct field (3)", self._struct("p", 3, self.RAW), {"p": encoded}),
            (
                "bytearray field",
                self._struct("p", 18, bytearray(self.RAW)),
                {"p": encoded},
            ),
            (
                "list<binary>",
                self._struct("p", 15, [self.RAW], info=18),
                {"p": [encoded]},
            ),
            (
                "set<binary>",
                self._struct("p", 14, {self.RAW}, info=18),
                {"p": [encoded]},
            ),
            (
                "nested dict value",
                self._struct("p", 11, {"k": self.RAW}),
                {"p": {"k": encoded}},
            ),
            ("bare bytes", self.RAW, encoded),
            ("bare bytearray", bytearray(self.RAW), encoded),
        ]
        for label, value, expected in cases:
            with self.subTest(shape=label):
                self.assertEqual(self.data_convertor.thrift2dict(value), expected)

    # ---------------------------------------------------------- 无损 + 不回归
    def test_bytes_helper_is_the_shared_one(self):
        """闸门（0919-15 / 登记项③）：bytes 的 JSON 口径**只有一份实现**。

        实现收口在 `utils.json_safe_bytes`（数据库 `BLOB` 列共用），本模块只是它的**别名**。
        数据库侧 `tests/database_engine_test.py::TestBytesHelperIsSharedWithThrift` 钉另一头。
        """
        from interfacetester import utils

        self.assertIs(self.data_convertor.json_safe_bytes, utils.json_safe_bytes)

    def test_base64_is_lossless(self):
        """口径的硬要求：base64 必须能还原原始字节（不是「看起来像」）。"""
        obj = self._struct("payload", 18, self.RAW)
        encoded = self.data_convertor.thrift2dict(obj)["payload"]

        self.assertEqual(base64.b64decode(encoded), self.RAW)

    def test_utf8_decodable_bytes_stay_text(self):
        """口径的另一半：能按 UTF-8 解码的 bytes 仍然给文本。

        这一条同时是**回归护栏**：0918-8 / M28 已经把
        `thrift2dict(b"pong") == "pong"` 钉住了，binary 口径不能把它改红
        （否则「顺手统一成一律 base64」会静默改掉已有行为）。
        """
        self.assertEqual(self.data_convertor.json_safe_bytes(b"pong"), "pong")
        self.assertEqual(self.data_convertor.thrift2dict(b"pong"), "pong")
        self.assertEqual(
            self.data_convertor.thrift2dict(self._struct("p", 18, "中文".encode())),
            {"p": "中文"},
        )

    # ---------------------------------------------------------- 死代码闸门
    def test_module_works_with_ttype_that_has_no_binary_attribute(self):
        """闸门：本模块的 TType 来自 **Apache `thrift`** 包，它**没有** `BINARY`。

        这既是「原实现为什么是死代码」的说明，也是一个能真实变红的护栏：
        谁要是把判据写回 `TType.BINARY`，本文件里所有用例都会在这个环境
        （Windows / 未装 thrift）直接 `AttributeError` —— 不需要源码级 grep。
        真正的 binary ttype 是 **thriftpy2 的 18**（真机实测：真实 IDL 的
        `Blob.thrift_spec == {1: (18, 'payload', False), ...}`），
        改成按值类型收口之后，代码不再依赖任何一个 TType 常量。
        """
        # 非空跑自检：这个 TType 替身确实没有 BINARY，否则本用例没有信息量
        self.assertFalse(hasattr(self.data_convertor.TType, "BINARY"))
        # 真机上 binary 字段的 ttype 值 18 必须能正常处理
        self.assertEqual(
            self.data_convertor.thrift2dict(self._struct("p", 18, self.RAW)),
            {"p": base64.b64encode(self.RAW).decode("ascii")},
        )


# ---------------------------------------------------------------------------
# §三.2：ThriftClient 初始化失败必须在**构造点**说清楚根因
# ---------------------------------------------------------------------------
_THRIFT_STUB_MODULES = (
    "thrift",
    "thrift.Thrift",
    "thriftpy2",
    "thriftpy2.parser",
    "thriftpy2.parser.parser",
    "thriftpy2.protocol",
    "thriftpy2.transport",
    "thriftpy2.rpc",
    THRIFT_CLIENT_MODULE,
)


@contextlib.contextmanager
def _import_thrift_client_with_fake_thriftpy2(load_result=None, load_exc=None):
    """在未安装 thrift 依赖的环境里导入**真实**的 `ThriftClient`（thriftpy2 走替身）。

    NOTICE: 与 `_import_data_convertor` 同款做法（替换 sys.modules → 导入 → 还原）。
    `ThriftClient.__init__` 的平台检查在 `step_thrift_request` 里而**不在**本模块，
    所以装上 thriftpy2 替身之后，`ThriftClient` 在 Windows 上也能真实跑。
    """
    saved = {name: sys.modules.get(name) for name in _THRIFT_STUB_MODULES}
    parent_pkg = importlib.import_module("interfacetester.thrift")
    saved_parent_attr = getattr(parent_pkg, "thrift_client", _MISSING)

    class _TType(object):
        STRUCT, LIST, SET, MAP, STRING = 12, 15, 14, 13, 11
        DOUBLE, I64, I32, I16, BYTE, BOOL = 4, 10, 8, 6, 3, 2

    thrift_mod = types.ModuleType("thrift.Thrift")
    thrift_mod.TType = _TType
    thrift_pkg = types.ModuleType("thrift")
    thrift_pkg.Thrift = thrift_mod

    def _load(path, module_name=None, include_dirs=None):
        # NOTICE（0920 批次 5 / N27）：记录每次 load 的 (path, module_name) ——
        # N27 的判据是「不同的 IDL 必须拿到不同的 module_name」，
        # 而那正是 thriftpy2 的**缓存键**（`cache_key = module_name or path`）。
        _FAKE_THRIFT_LOAD_CALLS.append((path, module_name, include_dirs))
        if load_exc is not None:
            raise load_exc
        return load_result

    tp = types.ModuleType("thriftpy2")
    tp.load = _load
    parser_mod = types.ModuleType("thriftpy2.parser")
    parser_inner = types.ModuleType("thriftpy2.parser.parser")
    parser_inner.thrift_stack = []
    parser_mod.parser = parser_inner
    tp.parser = parser_mod
    proto = types.ModuleType("thriftpy2.protocol")
    trans = types.ModuleType("thriftpy2.transport")
    for name in (
        "TBinaryProtocolFactory",
        "TCompactProtocolFactory",
        "TCyBinaryProtocolFactory",
        "TJSONProtocolFactory",
    ):
        setattr(proto, name, lambda *a, **k: None)
    for name in (
        "TBufferedTransportFactory",
        "TCyBufferedTransportFactory",
        "TCyFramedTransportFactory",
        "TFramedTransportFactory",
    ):
        setattr(trans, name, lambda *a, **k: None)
    rpc = types.ModuleType("thriftpy2.rpc")
    rpc.make_client = _FAKE_MAKE_CLIENT
    tp.protocol, tp.transport, tp.rpc = proto, trans, rpc

    sys.modules.update(
        {
            "thrift": thrift_pkg,
            "thrift.Thrift": thrift_mod,
            "thriftpy2": tp,
            "thriftpy2.parser": parser_mod,
            "thriftpy2.parser.parser": parser_inner,
            "thriftpy2.protocol": proto,
            "thriftpy2.transport": trans,
            "thriftpy2.rpc": rpc,
        }
    )
    sys.modules.pop(THRIFT_CLIENT_MODULE, None)
    # NOTICE: 必须用 importlib.import_module（点号全路径），不能用
    # `from interfacetester.thrift import thrift_client` —— 后者在
    # sys.modules 被清掉之后仍会命中父包上的**旧属性**，拿到上一次注入时构造的
    # 那个（绑着旧 thriftpy2 替身的）模块对象，测试之间会互相串味。
    if saved_parent_attr is not _MISSING:
        delattr(parent_pkg, "thrift_client")
    try:
        module = importlib.import_module(THRIFT_CLIENT_MODULE)

        yield module
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        if saved_parent_attr is _MISSING:
            if getattr(parent_pkg, "thrift_client", None) is not None:
                delattr(parent_pkg, "thrift_client")
        else:
            parent_pkg.thrift_client = saved_parent_attr


_MISSING = object()

class _FakeThriftModuleWithService(object):
    """替身 thriftpy2 模块：按 service 名挂着服务对象（`getattr(module, service_name)`）。

    NOTICE（0920 批次 5 / N27）：`ThriftClient.__init__` 会
    `getattr(self.thrift_module, self.service_name)`，所以替身**必须有同名属性**，
    否则在建 client 阶段就抛 `AttributeError: '_Module' object has no attribute 'Foo'`，
    测不到我们真正关心的 `module_name`。
    """

    def __init__(self, *service_names):
        for name in service_names:
            setattr(self, name, _FakeServiceObject(name))


class _FakeServiceObject(object):
    def __init__(self, name):
        self.__name__ = name
        self.__thrift_service__ = name


_FAKE_MAKE_CLIENT_CALLS = []
# 记录真正被交给底层 client 的请求对象（用于断言「解析出来的 struct 类是对的」）
_FAKE_SENT_REQUESTS = []
# 记录每次 `thriftpy2.load(path, module_name=...)` 的入参（0920 批次 5 / N27）
_FAKE_THRIFT_LOAD_CALLS = []


def _FAKE_MAKE_CLIENT(service, ip, port, **kwargs):
    _FAKE_MAKE_CLIENT_CALLS.append(
        {"ip": ip, "port": port, "service": service, **kwargs}
    )
    return _FakeUnderlyingClient()


class _FakeUnderlyingClient(object):
    def get_blob(self, req):
        _FAKE_SENT_REQUESTS.append(req)
        return {"echo": req.message, "ok": True}

    def close(self):
        pass


class _FakeRequestStruct(object):
    """thrift 方法**唯一那个 struct 参数**的类型。

    NOTICE: 替身的形状是按 `send_request` 的真实契约造的，不是随手写的：
    `ThriftClient.send_request` 取的是
    `getattr(service, method + "_args").thrift_spec[1][2]`，也就是「方法第 1 个参数
    的嵌套类型」。真机实测（thriftpy2 0.7.1 + 真实 IDL）：
      - `Blob get_blob(1: BlobReq req)` -> `{1: (12, 'req', <class BlobReq>, False)}`
        → `[1][2]` 就是 `BlobReq` 类（可用）；
      - `string ping(1: string message)` -> `{1: (11, 'message', False)}`
        → `[1][2]` 是 **`False`**，于是 `json2thrift(..., False)` 会抛
        `AttributeError: 'bool' object has no attribute 'thrift_spec'`；
      - `string ping()`（无参）-> `thrift_spec == {}` → `[1]` 直接 `KeyError: 1`。
    也就是说本框架的 thrift step **只支持「方法唯一参数是 struct」**的 IDL，
    这一点此前没有文档、也没有测试覆盖（已登记为 0919-8 的新发现，本批不修）。
    """

    thrift_spec = {1: (11, "message", None, False)}

    def __init__(self):
        self.message = None


class _FakeServiceClass(object):
    """`service BlobSvc { ... }` 在加载出来的模块里的那个对象。"""

    class get_blob_args(object):
        thrift_spec = {1: (12, "req", _FakeRequestStruct, False)}


class _FakeLoadedThriftModule(object):
    """`thriftpy2.load()` 的返回值：里面按 service 名字挂着服务对象。"""

    BlobSvc = _FakeServiceClass


class TestBatch0920ThriftModuleCacheKey(unittest.TestCase):
    """0920 批次 5 / **N27**：`module_name` 必须按 **IDL 文件**唯一，不能只用 service 名。

    ## 修复前的现场（`.tmp_audit/n27_verify2.py`，thriftpy2 0.7.1 真机）

    thriftpy2 的缓存键是 `cache_key = module_name or os.path.normpath(path)`
    （`thriftpy2/parser/parser.py`），而 `ThriftClient` 传的是
    `str(self.service_name) + "_thrift"` —— **两个不同项目/版本的 `.thrift`
    只要声明的服务同名，第二次 `load` 就命中缓存、拿到第一个 IDL 的模块**，
    第二个文件**从未被解析**：

    ```text
    one.thrift: struct Req {1: string name, 2: i32 count}   service Foo
    two.thrift: struct Req {1: i64 amount, 2: string label} service Foo

    load(one, module_name="Foo_thrift").Req -> ['count', 'name']
    load(two, module_name="Foo_thrift").Req -> ['count', 'name']   ← 错，应是 amount/label
    两个模块对象是同一个吗: True
    ```

    后果：字段名相同时**静默用错类型编码**（int 写进 string 字段）；
    字段名不同时用户拿到的是**另一个 IDL** 的字段列表，报错完全无法定位。
    """

    def setUp(self):
        _FAKE_THRIFT_LOAD_CALLS.clear()
        _FAKE_MAKE_CLIENT_CALLS.clear()

    def _client_inits(self):
        """用替身 thriftpy2 建两个 client，返回 (paths, module_names)。"""
        fake_module = _FakeThriftModuleWithService("Foo")

        with _import_thrift_client_with_fake_thriftpy2(
            load_result=fake_module
        ) as module:
            for path in ("a/one.thrift", "b/two.thrift"):
                module.ThriftClient(
                    thrift_file=path,
                    service_name="Foo",  # ← 同名服务，正是触发条件
                    ip="127.0.0.1",
                    port=9090,
                )

        paths = [call[0] for call in _FAKE_THRIFT_LOAD_CALLS]
        names = [call[1] for call in _FAKE_THRIFT_LOAD_CALLS]
        return paths, names

    def test_different_idls_get_different_module_names(self):
        """核心判据：不同 IDL ⇒ 不同 `module_name`（否则 thriftpy2 会命中同一缓存）。"""
        paths, names = self._client_inits()

        self.assertEqual(paths, ["a/one.thrift", "b/two.thrift"])
        self.assertNotEqual(
            names[0],
            names[1],
            "两个不同 IDL 拿到了**同一个 module_name** —— thriftpy2 会按它命中缓存，"
            "第二个 IDL 永不解析（N27）",
        )

    def test_same_idl_gets_a_stable_module_name(self):
        """反向护栏：同一个 IDL 必须**稳定**命中同一个键（否则每次都重新解析、白费开销）。"""
        with _import_thrift_client_with_fake_thriftpy2(
            load_result=_FakeThriftModuleWithService("Foo")
        ) as module:
            for _ in range(2):
                module.ThriftClient(
                    thrift_file="same.thrift",
                    service_name="Foo",
                    ip="127.0.0.1",
                    port=9090,
                )

        names = [call[1] for call in _FAKE_THRIFT_LOAD_CALLS]
        self.assertEqual(names[0], names[1], "同一个 IDL 的键不稳定，缓存形同虚设")

    def test_module_name_satisfies_thriftpy2_requirements(self):
        """`module_name` 必须以 `_thrift` 结尾（thriftpy2 的硬要求）。

        NOTICE: 第一版修复用了 `thrift_<hash>` 前缀 —— 真机立刻报
        `ThriftParserError: thriftpy2 can only generate module with '_thrift' suffix`。
        这条用例就是那次踩坑留下的护栏。
        """
        _paths, names = self._client_inits()

        for name in names:
            with self.subTest(module_name=name):
                self.assertTrue(
                    name.endswith("_thrift"),
                    f"{name!r} 不以 `_thrift` 结尾，thriftpy2 会拒绝（实测）",
                )
                self.assertRegex(
                    name,
                    r"^[A-Za-z_][0-9A-Za-z_]*$",
                    f"{name!r} 不是合法的 Python 模块名",
                )

    def test_include_dirs_participate_in_the_key(self):
        """`include_dirs` 会影响解析结果 → 必须进键（同一 IDL 配不同 include 是两个模块）。"""
        with _import_thrift_client_with_fake_thriftpy2(
            load_result=_FakeThriftModuleWithService("Foo")
        ) as module:
            module.ThriftClient(
                thrift_file="x.thrift",
                service_name="Foo",
                ip="127.0.0.1",
                port=9090,
                include_dirs=["/idl_a"],
            )
            module.ThriftClient(
                thrift_file="x.thrift",
                service_name="Foo",
                ip="127.0.0.1",
                port=9090,
                include_dirs=["/idl_b"],
            )

        names = [call[1] for call in _FAKE_THRIFT_LOAD_CALLS]
        self.assertNotEqual(
            names[0], names[1], "include_dirs 没进键，同一 IDL 的不同 include 会互相覆盖"
        )


class TestThriftClientInitSurfacesRootCause(unittest.TestCase):
    """§三.2：修复前 `__init__` 只 `logger.exception` 就返回，于是

    1) 得到的是一个 `client is None` 的半成品对象；
    2) step 继续往下跑，**setup hooks 先被执行**；
    3) 直到 `send_request` 才炸出
       `AttributeError: 'NoneType' object has no attribute 'ping_args'`
       —— IDL 路径、service_name、真实根因全都不在这条信息里。
    """

    def setUp(self):
        _FAKE_MAKE_CLIENT_CALLS.clear()

    def test_missing_idl_raises_at_construction_with_root_cause(self):
        with _import_thrift_client_with_fake_thriftpy2(
            load_exc=FileNotFoundError(
                2, "No such file or directory", "no_such_idl.thrift"
            )
        ) as module:
            with self.assertRaises(exceptions.ParamsError) as ctx:
                module.ThriftClient(
                    thrift_file="no_such_idl.thrift",
                    service_name="BlobSvc",
                    ip="10.0.0.1",
                    port=1234,
                    include_dirs=["/idl"],
                )

        message = str(ctx.exception)
        # 配了什么
        self.assertIn("no_such_idl.thrift", message)
        self.assertIn("BlobSvc", message)
        self.assertIn("include_dirs", message)
        # 根因是什么（修复前这里一个字都没有）
        self.assertIn("FileNotFoundError", message)
        # 原始异常留在链上，真实类型不丢
        self.assertIsInstance(ctx.exception.__cause__, FileNotFoundError)

    def test_service_name_mismatch_is_named(self):
        """IDL 能加载但 `service_name` 写错时，报错也要点名 service。"""

        class _ModuleWithoutService(object):
            pass

        with _import_thrift_client_with_fake_thriftpy2(
            load_result=_ModuleWithoutService()
        ) as module:
            with self.assertRaises(exceptions.ParamsError) as ctx:
                module.ThriftClient(
                    thrift_file="ok.thrift", service_name="Typo", ip="127.0.0.1", port=1
                )

        self.assertIn("Typo", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, AttributeError)

    def test_success_path_still_builds_and_sends(self):
        """成功路径不受影响：构造拿到 client，`send_request` 正常返回 dict。"""
        with _import_thrift_client_with_fake_thriftpy2(
            load_result=_FakeLoadedThriftModule()
        ) as module:
            client = module.ThriftClient(
                thrift_file="ok.thrift",
                service_name="BlobSvc",
                ip="10.0.0.1",
                port=1234,
                timeout=7,
            )
            self.assertIsNotNone(client.get_client())

            result = client.send_request({"message": "hi"}, "get_blob")

        self.assertEqual(result, {"echo": "hi", "ok": True})
        self.assertEqual(_FAKE_MAKE_CLIENT_CALLS[0]["ip"], "10.0.0.1")
        self.assertEqual(_FAKE_MAKE_CLIENT_CALLS[0]["port"], 1234)
        # NOTICE（批次 3 / H2）：这里原先断言 == 7（把秒原样透传给 thriftpy2，
        # 而它按毫秒解释 → 7 秒实际是 7 毫秒）。现在边界处 ×1000，故是 7000 毫秒。
        self.assertEqual(_FAKE_MAKE_CLIENT_CALLS[0]["timeout"], 7000)

    def test_step_fails_before_running_hooks(self):
        """端到端的核心收益：**构造即失败**，setup/teardown hooks 都不会执行。"""
        hook_log = []
        config = Config("thrift bad idl")
        builder = config.thrift()
        builder.service_name("BlobSvc")
        builder.ip("10.0.0.1")
        builder.port(1234)

        with _import_thrift_client_with_fake_thriftpy2(
            load_exc=FileNotFoundError(
                2, "No such file or directory", "no_such_idl.thrift"
            )
        ), _thrift_offline_environment():
            # NOTICE: RunThriftRequest 的构造里有平台 fail fast，所以 step 也必须在
            # `_thrift_offline_environment()` 里建（否则 Windows 上先抛 RuntimeError）。
            request = (
                RunThriftRequest("bad idl")
                .with_idl_path("no_such_idl.thrift", "/idl")
                .with_method("ping")
                .setup_hook("${_note()}")
            )
            request.teardown_hook("${_note()}")

            class _Case(InterfaceTester):
                pass

            _Case.config = config
            _Case.teststeps = [Step(request)]
            runner = _Case()
            runner._setup_runner()
            runner.parser.functions_mapping["_note"] = lambda: hook_log.append(
                "hook ran"
            )

            with self.assertRaises(exceptions.ParamsError) as ctx:
                runner.test_start()

        self.assertIn("no_such_idl.thrift", str(ctx.exception))
        self.assertEqual(hook_log, [], "构造失败后不应再执行任何 hook")
        # 失败后不能留下半成品 client（否则后续 step 会复用它）
        self.assertIsNone(runner.thrift_client)
        self.assertIsNone(runner.thrift_config)


# ---------------------------------------------------------------------------
# 0919-9 批次 B（§三.3）：`StepResult.data` 必须**每步一份**
#
# 口径来源（照抄 HTTP 已经做对的事）：`HttpSession.request` 第 233 行
# `self.data = SessionData()` —— 每发一次请求就换一个新对象。thrift/SQL 修复前
# 直接复用 `runner.session.data` 上现有的那个，于是同用例内互相污染。
# ---------------------------------------------------------------------------
def _build_thrift_case(steps, base_url=None):
    config = Config("thrift data isolation case")
    config.thrift()
    if base_url:
        config.base_url(base_url)

    class _Case(InterfaceTester):
        pass

    _Case.config = config
    _Case.teststeps = steps
    return _Case


class TestThriftStepResultDataIsolation(unittest.TestCase):
    """thrift step 的 `StepResult.data` 不能与别的 step 共用同一个 `SessionData`。"""

    @staticmethod
    def _thrift_step(name, with_validator=False):
        request = (
            RunThriftRequest(name)
            .with_idl_path("a.thrift", "/idl")
            .with_ip("10.0.0.1")
            .with_port(1111)
            .with_method("ping")
        )
        if with_validator:
            return Step(request.validate().assert_equal("ok", True))
        return Step(request)

    def test_two_thrift_steps_do_not_share_step_data(self):
        """修复前：`step_results[0].data is step_results[1].data` 为真。

        后果是**前一步的报告里显示后一步的断言结果**（实测：第 1 步根本没声明断言，
        却在 summary.json 的 `records[0].data.validators` 里看到第 2 步的结果）。
        """
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            case = _build_thrift_case(
                [self._thrift_step("thrift 1"), self._thrift_step("thrift 2", True)]
            )
            summary = case().test_start().get_summary()

        first, second = summary.step_results[0].data, summary.step_results[1].data
        self.assertIsNot(first, second)
        # 第 1 步没有断言 → 它的记录里必须是空的（这是「不被污染」的直接判据）
        self.assertEqual(first.validators, {})
        self.assertEqual(first.success, True)
        # 第 2 步的断言结果只属于它自己
        self.assertTrue(second.validators)

    def test_thrift_step_after_http_step_has_no_http_leftover(self):
        """修复前：thrift 紧跟 HTTP step 时，thrift 的记录里带着上一个 HTTP 请求的
        `req_resps` / `stat` / `address`（实测 req_resps=1、server_ip='127.0.0.1'）。"""
        import mock_server  # noqa: F401  (conftest 已启动，这里只取 URL)
        from interfacetester import RunRequest
        from interfacetester.utils import HTTP_BIN_URL

        http_step = Step(RunRequest("http step").get("/get"))
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            case = _build_thrift_case(
                [http_step, self._thrift_step("thrift step")], base_url=HTTP_BIN_URL
            )
            summary = case().test_start().get_summary()

        http_data, thrift_data = (
            summary.step_results[0].data,
            summary.step_results[1].data,
        )
        self.assertIsNot(http_data, thrift_data)
        self.assertTrue(http_data.req_resps, "HTTP step 自己应该记录 1 条 req_resp")
        # thrift step 不是 HTTP 请求 → 三个 HTTP 专属字段都必须干净
        self.assertEqual(thrift_data.req_resps, [])
        self.assertEqual(thrift_data.stat.elapsed_ms, 0)
        self.assertEqual(thrift_data.address.server_ip, "N/A")


# ---------------------------------------------------------------------------
# 0919-10 批次 C：thrift 死配置收口（§三.1 / §三.4 / §三.5 / §六.1）
#
# 拍板口径（用户已确认）：保留 thrift、**不实现服务发现**；对「设了但完全不生效」的
# 字段沿用 `make.py` 的 `UNSUPPORTED_*` 闸门风格——**非默认取值就响亮报错**；
# 纯冗余的字段直接删。
# ---------------------------------------------------------------------------
class TestThriftUnwiredFieldsGate(unittest.TestCase):
    """`target`（config/step 级）与 config 级 `thrift_client` 非默认即报错。

    0919-3 复核（`docs/仍然存在的问题0919-3-复核结论.md` §三.1/§三.4）确认这三处是
    **死配置**：配了框架一个字都不读，请求静默落到 `ip:port` 兜底
    （默认 `127.0.0.1:9000`）——「静默打错目标」正是本仓最忌讳的一类。
    """

    @staticmethod
    def _step():
        return (
            RunThriftRequest("gate step")
            .with_idl_path("a.thrift", "/idl")
            .with_ip("10.0.0.1")
            .with_port(1111)
            .with_method("ping")
        )

    def _run(self, config_attrs=None, mutate_step=None):
        """跑一个 thrift step；返回 (异常或 None, 框架建出来的 client 列表)。"""
        _FakeThriftClient.instances = []
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            request = self._step()
            if mutate_step:
                mutate_step(request)
            case = _build_thrift_case([Step(request)])
            for key, value in (config_attrs or {}).items():
                setattr(case.config.struct().thrift, key, value)
            try:
                case().test_start()
                return None, list(_FakeThriftClient.instances)
            except Exception as ex:
                return ex, list(_FakeThriftClient.instances)

    # ------------------------------------------------------------ 该报错的
    def test_config_target_is_rejected(self):
        err, clients = self._run({"target": "sd://svcA?cluster=probe&env=staging"})

        self.assertIsInstance(err, exceptions.ParamsError)
        self.assertIn("target", str(err))
        self.assertIn("sd://svcA", str(err), "报错必须点名用户配的那个值")
        self.assertEqual(clients, [], "报错发生在建连之前，不该真的去连")

    def test_step_target_is_rejected(self):
        """step 级 `target` 没有 `with_target()`，唯一入口是直接赋属性。"""

        def mutate(request):
            request.struct().thrift_request.target = "sd://svcB"

        err, _ = self._run(mutate_step=mutate)

        self.assertIsInstance(err, exceptions.ParamsError)
        self.assertIn("sd://svcB", str(err))

    def test_config_level_thrift_client_is_rejected(self):
        """config 级 `thrift_client` 从未被读取（step 级才是有效入口）。"""
        injected = _FakeThriftClient(probe_marker="config-level")
        err, clients = self._run({"thrift_client": injected})

        self.assertIsInstance(err, exceptions.ParamsError)
        self.assertIn("thrift_client", str(err))
        self.assertEqual(injected.requests, [])
        self.assertEqual(clients, [], "不该退化成「框架自己又建一个 client」")

    # ------------------------------------------------ 假报检查（同样重要）
    def test_psm_env_cluster_are_not_rejected(self):
        """`psm`/`env`/`cluster` **确实被读取**（请求日志文本 + `type()` 名字），
        只是不参与连接目标解析——所以它们不能报错，口径写在 NOTICE 与能力清单里。"""
        err, clients = self._run({"psm": "svcA", "env": "staging", "cluster": "probe"})

        self.assertIsNone(err)
        self.assertEqual(len(clients), 1, "应该正常建出一个 client")

    def test_step_level_thrift_client_is_not_rejected(self):
        """step 级 `with_thrift_client(...)` 是**唯一有效**的注入入口，不能被闸门误伤。"""
        injected = _FakeThriftClient(probe_marker="step-level")

        def mutate(request):
            request.with_thrift_client(injected)

        err, clients = self._run(mutate_step=mutate)

        self.assertIsNone(err)
        self.assertEqual(clients, [], "外部注入时框架不该自己再建一个")
        self.assertEqual(len(injected.requests), 1, "注入的 client 必须真的被用上")

    # ------------------------------------------------------- 删除的冗余字段
    def test_transport_field_is_deleted(self):
        """`transport` 与 `trans_type` 是同一件事的两个平行开关，纯冗余 → 批次 C 删除。

        非空跑自检：先确认它**真的不在**模型里，再确认误用是**响亮**的。
        （修复前它不影响任何行为，只在请求日志里多打一行，用户按名字很容易挑错。）
        """
        import interfacetester.models as models_module

        self.assertNotIn("transport", TThriftRequest.model_fields)
        self.assertFalse(hasattr(models_module, "TransportEnum"))
        with self.assertRaises(ValueError):
            TThriftRequest().transport = "framed"


class TestThriftStepSettersAreEffective(unittest.TestCase):
    """0919-15 / 登记项②：step 侧补齐的 setter 必须**真正生效**，且 step 优先于 config。

    补的是「合并逻辑真的会读」的三个：`service_name` / `timeout` / `include_dirs`
    （`run_step_thrift_request` 里都是「step 优先，否则回落 config」）。
    刻意**不**补 `env` / `cluster` / `psm`：它们只用于日志文本与 `type()` 名字，
    不参与寻址，而 config 侧已有 setter（见 `RunThriftRequest` 的 NOTICE）。
    """

    def _ctor_kwargs(self, config_attrs=None, build=None, with_idl_path=True):
        _FakeThriftClient.instances = []
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            request = RunThriftRequest("setter step")
            if with_idl_path:
                # NOTICE: `with_idl_path(idl, root)` **顺带**会把 include_dirs 设成 [root]，
                # 所以「验证 config 级 include_dirs 的回落」时必须绕开它（见下面那条用例）。
                request.with_idl_path("a.thrift", "/idl")
            else:
                request.struct().thrift_request.idl_path = "a.thrift"
            request.with_ip("10.0.0.1").with_port(1111).with_method("ping")
            if build:
                build(request)
            case = _build_thrift_case([Step(request)])
            for key, value in (config_attrs or {}).items():
                setattr(case.config.struct().thrift, key, value)
            case().test_start()
            return _FakeThriftClient.instances[0].kwargs

    def test_with_service_name_reaches_the_client(self):
        kwargs = self._ctor_kwargs(build=lambda r: r.with_service_name("StepSvc"))

        self.assertEqual(kwargs["service_name"], "StepSvc")

    def test_with_timeout_reaches_the_client(self):
        kwargs = self._ctor_kwargs(build=lambda r: r.with_timeout(3))

        self.assertEqual(kwargs["timeout"], 3)

    def test_with_include_dirs_replaces_the_list(self):
        kwargs = self._ctor_kwargs(build=lambda r: r.with_include_dirs("/a", "/b"))

        self.assertEqual(kwargs["include_dirs"], ["/a", "/b"])

    def test_step_level_wins_over_config_for_all_three(self):
        def build(request):
            request.with_service_name("StepSvc")
            request.with_timeout(3)
            request.with_include_dirs("/step_idl")

        kwargs = self._ctor_kwargs(
            config_attrs={
                "service_name": "ConfigSvc",
                "timeout": 30,
                "include_dirs": ["/config_idl"],
            },
            build=build,
        )

        self.assertEqual(kwargs["service_name"], "StepSvc")
        self.assertEqual(kwargs["timeout"], 3)
        self.assertEqual(kwargs["include_dirs"], ["/step_idl"])

    def test_config_still_used_when_step_does_not_set_them(self):
        """反向护栏：step 不设时仍然回落 config（新 setter 不能把回落改坏）。

        NOTICE: 这里必须 `with_idl_path=False` —— `with_idl_path(idl, root)` 会**顺带**
        把 step 级 `include_dirs` 设成 `[root]`，用它就测不到 config 级的回落。
        """
        kwargs = self._ctor_kwargs(
            config_attrs={
                "service_name": "ConfigSvc",
                "timeout": 30,
                "include_dirs": ["/config_idl"],
            },
            with_idl_path=False,
        )

        self.assertEqual(kwargs["service_name"], "ConfigSvc")
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["include_dirs"], ["/config_idl"])

    def test_display_only_fields_deliberately_have_no_step_setter(self):
        """闸门：不要给「只用于日志」的字段补 step setter。

        它们（`env` / `cluster` / `psm`）不参与连接目标解析，config 侧已有 setter；
        给它们加 step setter 会让人以为能影响连接——与批次 C 的收口方向相反。
        """
        for name in ("with_env", "with_cluster", "with_psm", "with_target"):
            with self.subTest(setter=name):
                self.assertFalse(hasattr(RunThriftRequest, name))


class TestConfigThriftSettersAreEffective(unittest.TestCase):
    """批次 C / §三.5：补的 setter 必须是**真正生效**的那两个。

    `run_step_thrift_request` 的合并逻辑一直在读 config 级的
    `idl_path` / `include_dirs`，但 `ConfigThrift` 从来没提供过对应方法——
    「读得到、设不进去」。刻意**不**补 `target()` / `thrift_client()`：
    那两个字段是死的，补 setter 等于主动提供一个「设置了但没用」的 API。
    """

    def _ctor_kwargs(self, config_builder, step_idl="a.thrift"):
        _FakeThriftClient.instances = []
        with _thrift_offline_environment(), _fake_thrift_client_ctor():
            request = (
                RunThriftRequest("setter step")
                .with_ip("10.0.0.1")
                .with_port(1111)
                .with_method("ping")
            )
            if step_idl:
                request.with_idl_path(step_idl, "/step_idl")
            case = _build_thrift_case([Step(request)])
            config_builder(case.config.thrift())
            case().test_start()
            return _FakeThriftClient.instances[0].kwargs

    def test_config_idl_path_and_include_dirs_reach_the_client(self):
        def build(builder):
            builder.idl_path("from_config.thrift").include_dirs(["/cfg_idl"])

        kwargs = self._ctor_kwargs(build, step_idl=None)

        self.assertEqual(kwargs["thrift_file"], "from_config.thrift")
        self.assertEqual(kwargs["include_dirs"], ["/cfg_idl"])

    def test_step_level_still_wins_over_config(self):
        def build(builder):
            builder.idl_path("from_config.thrift")

        kwargs = self._ctor_kwargs(build, step_idl="from_step.thrift")

        self.assertEqual(kwargs["thrift_file"], "from_step.thrift")

    def test_dead_field_setters_were_deliberately_not_added(self):
        """闸门：不要给死字段补 setter（否则「字段是死的」升级成「方法骗人」）。"""
        from interfacetester.config import ConfigThrift

        self.assertTrue(hasattr(ConfigThrift, "idl_path"))
        self.assertTrue(hasattr(ConfigThrift, "include_dirs"))
        self.assertFalse(hasattr(ConfigThrift, "target"))
        self.assertFalse(hasattr(ConfigThrift, "thrift_client"))


# ---------------------------------------------------------------------------
# 批次 C / §六.1：`send_request` 只支持「方法唯一参数是 struct」
#
# 真机实测（thriftpy2 0.7.1 + 真实 IDL）四种非法形态各给一条点名原因的错误，
# 而不是修复前的 `'bool' object has no attribute 'thrift_spec'` / `KeyError: 1` /
# 参数个数不匹配 / `has no attribute 'nope_args'`。
# 这里的替身形状**照抄真机的 thrift_spec**（见 `.tmp_report/probe_batchC.py` 的实测）。
# ---------------------------------------------------------------------------
class _FakeArgsStruct(object):
    """方法唯一那个 struct 参数的类型。"""

    thrift_spec = {1: (11, "message", None, False)}


class _ServiceWithStructParam(object):
    class get_blob_args(object):
        thrift_spec = {1: (12, "req", _FakeArgsStruct, False)}


class _ServiceWithScalarParam(object):
    class notify_args(object):
        thrift_spec = {1: (11, "msg", False)}


class _ServiceWithNoParam(object):
    class ping_args(object):
        thrift_spec = {}


class _ServiceWithTwoParams(object):
    class add_args(object):
        thrift_spec = {1: (8, "a", False), 2: (8, "b", False)}


class _ShapeThriftModule(object):
    """`thriftpy2.load()` 的返回值：按 service 名字挂不同的方法形态。"""

    StructSvc = _ServiceWithStructParam
    ScalarSvc = _ServiceWithScalarParam
    NoParamSvc = _ServiceWithNoParam
    TwoParamSvc = _ServiceWithTwoParams


class TestThriftRequestStructShapeGate(unittest.TestCase):
    """`send_request` 取 `<method>_args.thrift_spec[1][2]`，只有单 struct 参数成立。

    NOTICE: 这里**全部走公开入口 `send_request`**，不直接调 `_resolve_request_struct_class`。
    原因很实际：注入验证（C2）把 `send_request` 里的调用点退回旧写法时，
    「直接调私有方法」的用例仍然全绿——它测的是那个 helper，而不是这条链路。
    走公开入口之后，旧写法会抛 `KeyError: 1` / `AttributeError` 而不是 `ParamsError`，
    用例才会真的变红。
    """

    def setUp(self):
        _FAKE_SENT_REQUESTS.clear()

    def _client(self, service_name):
        cm = _import_thrift_client_with_fake_thriftpy2(load_result=_ShapeThriftModule())
        module = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        return module.ThriftClient(
            thrift_file="shapes.thrift",
            service_name=service_name,
            ip="10.0.0.1",
            port=1234,
        )

    def test_supported_shape_resolves_and_sends(self):
        """唯一支持的形态：`send_request` 照常工作，且解析出的类**就是**那个 struct。"""
        client = self._client("StructSvc")

        result = client.send_request({"message": "hi"}, "get_blob")

        self.assertEqual(result, {"echo": "hi", "ok": True})
        self.assertEqual(len(_FAKE_SENT_REQUESTS), 1)
        self.assertIsInstance(
            _FAKE_SENT_REQUESTS[0],
            _FakeArgsStruct,
            "交给底层 client 的必须是按 thrift_spec[1][2] 解析出来的那个 struct 类实例",
        )

    def test_method_without_params_is_rejected_with_reason(self):
        # 非空跑自检：确认替身形状与真机一致（无参 → thrift_spec 为空）
        self.assertEqual(_ServiceWithNoParam.ping_args.thrift_spec, {})

        with self.assertRaises(exceptions.ParamsError) as ctx:
            self._client("NoParamSvc").send_request({}, "ping")

        message = str(ctx.exception)
        self.assertIn("没有参数", message)
        self.assertIn("struct", message)

    def test_method_with_multiple_params_is_rejected_with_reason(self):
        self.assertEqual(len(_ServiceWithTwoParams.add_args.thrift_spec), 2)

        with self.assertRaises(exceptions.ParamsError) as ctx:
            self._client("TwoParamSvc").send_request({}, "add")

        message = str(ctx.exception)
        self.assertIn("2 个参数", message)
        self.assertIn("'a'", message)
        self.assertIn("'b'", message)

    def test_scalar_param_is_rejected_with_reason(self):
        """真机上 `string ping(1: string message)` 的 `[1][2]` 是 `False`。"""
        self.assertIs(_ServiceWithScalarParam.notify_args.thrift_spec[1][2], False)

        with self.assertRaises(exceptions.ParamsError) as ctx:
            self._client("ScalarSvc").send_request({}, "notify")

        message = str(ctx.exception)
        self.assertIn("不是 struct", message)
        self.assertIn("ttype=11", message)

    def test_unknown_method_lists_available_methods(self):
        with self.assertRaises(exceptions.ParamsError) as ctx:
            self._client("StructSvc").send_request({}, "nope")

        message = str(ctx.exception)
        self.assertIn("nope", message)
        self.assertIn("get_blob", message, "必须列出可用方法，否则用户只能猜")


# ---------------------------------------------------------------------------
# 批次 8 / L11 + L12（0919-28）
# ---------------------------------------------------------------------------
class _ServiceWithFieldIdTwo(object):
    """合法 IDL：唯一那个 struct 参数用了 field id **2**。

        Blob get_blob(2: BlobReq req)
    """

    class get_blob_args(object):
        thrift_spec = {2: (12, "req", _FakeArgsStruct, False)}


class _ServiceWithScalarFieldIdThree(object):
    class notify_args(object):
        thrift_spec = {3: (11, "msg", False)}


class _FieldIdThriftModule(object):
    IdTwoSvc = _ServiceWithFieldIdTwo
    IdThreeScalarSvc = _ServiceWithScalarFieldIdThree


class TestBatch0919_28FieldIdAndClosedClient(unittest.TestCase):
    """批次 8 / **L11**（field id ≠ 1）+ **L12**（`close()` 之后再 `send_request`）。

    ## 现场（实测，修复前，`.tmp_report/probe_l11_l12.py`）

    ```text
    Blob get_blob(2: BlobReq req)      → KeyError: 1        ← 合法 thrift，id 由 IDL 作者指定
    string notify(3: string msg)       → KeyError: 1        ← 本该给「不是 struct」的点名错误
    client.close(); client.send_request(...)
                                       → AttributeError: 'NoneType' object has no attribute 'get_blob'
    ```

    两条都是「与根因完全脱节的报错」——与 0919-10 要消灭的那一类同族：

    - **L11**：`_resolve_request_struct_class` 写死了 `spec[1]`。它上面的 `len(spec) > 1`
      已经拦掉多参，所以这里要的是「**唯一**那个参数」，按 id 取本身就是多余的假设。
    - **L12**：`close()` 会把 `self.client` 置空（"重复 close 安全"的实现方式），
      而 `send_request` 没判空。最典型的现场是 **runner 在每个用例结束时关闭外部注入的
      client**（`runner.py` 释放长连接那段）→ 下一个用例复用同一个对象就炸。

    ## 修法

    - L11：按**唯一字段**取（`next(iter(spec.values()))`），不关心 id；错误信息用真实字段名。
    - L12：`send_request` 开头判空 → 点名的 `ParamsError`（说清"被关了"以及 runner 的生命周期）。
    """

    def _client(self, service_name, module=None):
        cm = _import_thrift_client_with_fake_thriftpy2(
            load_result=_FieldIdThriftModule() if module is None else module
        )
        mod = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        return mod.ThriftClient(
            thrift_file="shapes.thrift",
            service_name=service_name,
            ip="10.0.0.1",
            port=1234,
        )

    def test_single_param_with_field_id_two_is_supported(self):
        """**核心**：field id ≠ 1 是合法 IDL，必须照常解析并发送。"""
        self.assertEqual(list(_ServiceWithFieldIdTwo.get_blob_args.thrift_spec), [2])

        client = self._client("IdTwoSvc")

        result = client.send_request({"message": "hi"}, "get_blob")

        self.assertEqual(result, {"echo": "hi", "ok": True})
        self.assertIsInstance(_FAKE_SENT_REQUESTS[-1], _FakeArgsStruct)

    def test_scalar_param_with_non_one_id_gets_the_point_naming_error(self):
        """非 1 的**标量**参数同样要给「不是 struct」的点名错误（而不是 KeyError）。"""
        client = self._client("IdThreeScalarSvc")

        with self.assertRaises(exceptions.ParamsError) as ctx:
            client.send_request({}, "notify")

        message = str(ctx.exception)
        self.assertIn("不是 struct", message)
        self.assertIn("'msg'", message, "要点名真实字段名")
        self.assertIn("ttype=11", message)

    def test_field_id_one_is_unaffected(self):
        """反向护栏：最常见的 id = 1 形态逐字不变。"""
        client = self._client("StructSvc", module=_ShapeThriftModule())

        self.assertEqual(client.send_request({"message": "hi"}, "get_blob"), {"echo": "hi", "ok": True})

    def test_closed_client_raises_a_readable_error(self):
        """**核心**：`close()` 之后再用必须点名"已经关闭"，而不是裸 AttributeError。"""
        client = self._client("StructSvc", module=_ShapeThriftModule())
        client.close()

        with self.assertRaises(exceptions.ParamsError) as ctx:
            client.send_request({"message": "hi"}, "get_blob")

        message = str(ctx.exception)
        self.assertIn("已经关闭", message)
        self.assertIn("close()", message)
        self.assertIn("每个用例", message, "要说清 runner 的生命周期，否则用户查不到原因")

    def test_close_is_idempotent_and_client_is_none(self):
        """反向护栏：重复 `close()` 不抛异常（既有不变量）。"""
        client = self._client("StructSvc", module=_ShapeThriftModule())

        client.close()
        client.close()

        self.assertIsNone(client.client)

    def test_a_fresh_client_is_unaffected_by_a_closed_one(self):
        """反向护栏：关掉一个 client 不影响新构造的 client。"""
        old = self._client("StructSvc", module=_ShapeThriftModule())
        old.close()

        fresh = self._client("StructSvc", module=_ShapeThriftModule())

        self.assertEqual(fresh.send_request({"message": "hi"}, "get_blob"), {"echo": "hi", "ok": True})


class TestBatch0919_21ThriftTimeoutUnit(unittest.TestCase):
    r"""批次 3 / H2：thrift `timeout` 的单位换算必须发生在**库边界**上。

    ## 修复前的现场（真机实测，thriftpy2 0.7.1 + 「接受连接但永不回数据」的 TCP 服务）

    ```text
    ThriftClient(..., timeout=10)    ->  0.018s 就 TimeoutError（若单位是秒，应在 10s 之后）
    ThriftClient(..., timeout=3000)  ->  3.002s 后 TimeoutError
    TSocket(socket_timeout=10).socket_timeout   == 0.01      （秒）
    TSocket(socket_timeout=3000).socket_timeout == 3.0
    ```

    也就是说底层按**毫秒**解释，而本框架（模型注释、`with_timeout` 文档、构造函数默认值）
    一律写「秒」。默认 `10` 于是成了 **10 毫秒** —— 任何回包超过 10 ms 的服务都失败，
    且 `TimeoutError: timed out` 里**没有任何 timeout 数值**，排查时看不出是配置问题。

    ## 根因链（三处都在 thriftpy2 里，可查）

    ```text
    thriftpy2/rpc.py:24,52-53   make_client(..., timeout: int = 3000) -> TSocket(socket_timeout=timeout)
    thriftpy2/transport/socket.py:33   "@param socket_timeout  socket timeout in ms"
    thriftpy2/transport/socket.py:51   self.socket_timeout = socket_timeout / 1000
    ```

    ## 修法与**层次**（这批最容易改错的地方）

    ×1000 只加在 `ThriftClient.__init__` 调用 `make_client` 那一处。
    `TConfigThrift.timeout` / `TThriftRequest.timeout` / `with_timeout()` /
    `RunThriftRequest` 传给 `ThriftClient(...)` 的值**全部仍然是秒** ——
    所以 step 侧的既有断言（`kwargs["timeout"] == 3 / 30 / 0`）一条都不用改，
    这一类的用例（`test_with_timeout_reaches_the_client` 等）继续钉住「秒」的契约。

    下面第一个用例走**替身**（本机没装 thriftpy2 也能真跑，见 `_import_thrift_client_with_fake_thriftpy2`）：
    直接断言交给 `make_client` 的是毫秒数。第二个用例在**装了真 thriftpy2** 时才会跑，
    它按库自己的换算读回 `TSocket.socket_timeout`，用来证明「×1000 之后底层拿到的是秒」。
    """

    def setUp(self):
        _FAKE_MAKE_CLIENT_CALLS.clear()

    def tearDown(self):
        _FAKE_MAKE_CLIENT_CALLS.clear()

    def _client(self, **kwargs):
        with _import_thrift_client_with_fake_thriftpy2(
            load_result=_FakeLoadedThriftModule()
        ) as module:
            module.ThriftClient(
                thrift_file="ok.thrift",
                service_name="BlobSvc",
                ip="127.0.0.1",
                port=9000,
                **kwargs,
            )
        return _FAKE_MAKE_CLIENT_CALLS[-1]

    def test_timeout_is_converted_from_seconds_to_milliseconds(self):
        """秒 → 毫秒：底层 `make_client(timeout=)` 收到的必须是 `秒数 × 1000`。"""
        for seconds, milliseconds in (
            (1, 1000),
            (7, 7000),
            (10, 10000),
            (30, 30000),
            (3000, 3000000),
        ):
            with self.subTest(seconds=seconds):
                call = self._client(timeout=seconds)
                self.assertEqual(
                    call["timeout"],
                    milliseconds,
                    f"timeout={seconds}（秒）必须换算成 {milliseconds} 毫秒交给 thriftpy2；"
                    f"实际给的是 {call['timeout']}——若等于 {seconds}，就是又把秒当毫秒透传了",
                )

    def test_default_timeout_is_ten_seconds(self):
        """不传 timeout 时的默认值：10 **秒**（= 10000 毫秒），与 `TConfigThrift.timeout` 一致。"""
        call = self._client()

        self.assertEqual(call["timeout"], 10 * 1000)
        self.assertEqual(
            TConfigThrift().timeout,
            10,
            "config 侧默认值应当仍是 10（单位：秒）——改数值属于改产品行为，本批只修单位",
        )

    def test_zero_timeout_means_no_limit_and_is_not_scaled_into_a_value(self):
        """边界：`timeout: 0` 表示**不限时**，换算后仍是 0（不能变成"0 秒超时"）。

        thriftpy2 的 `TSocket` 用 `if socket_timeout` 判空，0 会被当成 None → 不设超时。
        这是修复前后一致的行为，本用例把它钉住，避免下一个人"顺手"改成 `or 默认值`。
        """
        call = self._client(timeout=0)

        self.assertEqual(call["timeout"], 0)


@unittest.skipUnless(
    THRIFT_CLIENT_IMPORTABLE,
    f"thriftpy2 未安装，无法核对库自己的单位约定（{THRIFT_CLIENT_IMPORT_ERROR}）",
)
class TestBatch0919_21Thriftpy2UnitContract(unittest.TestCase):
    """装了真 thriftpy2 时，把**修复所依据的那条前提**钉成不变量。

    NOTICE: 上面那组替身用例只能证明"我们乘了 1000"，**证明不了"乘 1000 是对的"**。
    后者只能由库本身回答：`TSocket.__init__` 做的是 `socket_timeout / 1000`
    （docstring 写着 "socket timeout in ms"），而 `make_client(timeout=...)`
    把这个参数**原样**交给 `TSocket(socket_timeout=...)`。

    这条前提值得单独钉住，是因为 `pyproject.toml` 的 thrift extra 写的是
    `thriftpy2>=0.4.14`（**没有上界**）：万一将来它把单位改成秒，
    这里的 `/1000` 就会变成"多乘了 1000 倍"，而**不会有任何别的用例发现**。
    """

    def test_tsocket_treats_socket_timeout_as_milliseconds(self):
        from thriftpy2.transport import TSocket  # noqa: PLC0415

        sock = TSocket("127.0.0.1", 9000, socket_timeout=1000)
        self.assertEqual(
            sock.socket_timeout,
            1.0,
            "thriftpy2 的 TSocket 不再按毫秒解释 socket_timeout —— "
            "那么 thrift_client 里 make_client 的 `timeout * 1000` 必须同步改掉",
        )
        self.assertEqual(
            TSocket("127.0.0.1", 9000, socket_timeout=3000).socket_timeout, 3.0
        )


# ---------------------------------------------------------------------------
# 批次 D / T1 + T2：thrift **请求方向**（`json2thrift` → `_convert`）的两处「静默」
#
# 替身形状**照抄真机**（thriftpy2 0.7.1 + `.tmp_audit/idl/t1t2.thrift`，
# 原始输出见 `.tmp_audit/out_t1t2_specs.txt`）：
#
#     Req.thrift_spec == {
#         1:  (18, 'payload', False),                      # binary（**3 元组**，无 ttype_info）
#         2:  (15, 'blobs', 18, False),                    # list<binary>（元素类型是**裸 int**）
#         3:  (11, 'message', False),                      # string
#         4:  (2,  'flag', False),                         # bool
#         5:  (8,  'count', False),                        # i32
#         6:  (4,  'ratio', False),                        # double
#         7:  (10, 'big', False),                          # i64
#         8:  (15, 'tags', 11, False),                     # list<string>
#         9:  (13, 'codes', (8, 11), False),               # map<i32, string>
#         10: (15, 'items', (12, Inner), False),           # list<struct>
#         11: (12, 'inner', Inner, False),                 # struct
#     }
#
# 为什么替身要照抄：T1 的修复依据是「binary 的 ttype 是 18」且「3 元组形态」，
# 这两条都只能由真 thriftpy2 回答（Apache thrift 的 `TType` 里**没有** `BINARY`，
# 见 `test_ttype_18_is_a_literal_not_TType_dot_BINARY`）。
# ---------------------------------------------------------------------------
class _Inner(object):
    thrift_spec = {1: (11, "name", False), 2: (8, "n", False)}

    def __init__(self, name=None, n=None):
        self.name = name
        self.n = n


class _Req(object):
    thrift_spec = {
        1: (18, "payload", False),
        2: (15, "blobs", 18, False),
        3: (11, "message", False),
        4: (2, "flag", False),
        5: (8, "count", False),
        6: (4, "ratio", False),
        7: (10, "big", False),
        8: (15, "tags", 11, False),
        9: (13, "codes", (8, 11), False),
        10: (15, "items", (12, _Inner), False),
        11: (12, "inner", _Inner, False),
    }

    def __init__(self):
        # 与 thriftpy2 生成的类同款：无参构造，字段先都是 None
        for field in self.thrift_spec.values():
            setattr(self, field[1], None)


class TestBatchDThriftRequestConversion(unittest.TestCase):
    """批次 D / T1：请求方向的 `binary` 字段（ttype=18）；T2：请求参数里的未知字段。

    T1：修复前 `_convert` 没有 ttype 18 的分支 → `TypeError: Unrecognized thrift
        field type: 18`（无字段名，且发生在**建连与 setup hooks 之后**）。
    T2：修复前 STRUCT 只按 IDL 的 spec 遍历 → JSON 里多出来的键被**静默丢弃**，
        服务端收到 None、用例可能照样「通过」（假通过）。
    """

    @classmethod
    def setUpClass(cls):
        cls.dc = _import_data_convertor()

    def _convert(self, payload):
        return self.dc.json2thrift(json.dumps(payload), _Req)

    # ---------------------------- T1：binary ----------------------------
    def test_binary_string_becomes_utf8_bytes(self):
        obj = self._convert({"payload": "中文"})

        self.assertEqual(obj.payload, "中文".encode("utf8"))
        self.assertIsInstance(obj.payload, bytes)

    def test_binary_absent_or_null_stays_none(self):
        """反向护栏：没写 / 写 null 的 binary 字段仍是 None（不会被"填空")。"""
        self.assertIsNone(self._convert({}).payload)
        self.assertIsNone(self._convert({"payload": None}).payload)

    def test_list_of_binary_converts_each_element(self):
        """`list<binary>` 的元素类型在真机 spec 里是**裸 int 18**（不是元组）。"""
        obj = self._convert({"blobs": ["a", "中文"]})

        self.assertEqual(obj.blobs, [b"a", "中文".encode("utf8")])

    def test_binary_rejects_non_text_values_naming_the_field(self):
        """非字符串/bytes 一律点名报错，**不**像 string 分支那样 `str(val)` 兜底。"""
        for bad in (123, {"$base64": "AA=="}, ["a"], True):
            with self.subTest(bad=bad):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    self._convert({"payload": bad})

                message = str(ctx.exception)
                self.assertIn("payload", message, "报错必须点名是哪个字段")
                self.assertIn("binary", message)
                # 修复前那句「只有类型号、没有字段名」的报错不该再出现在**报错正文**里
                # （NOTICE 段作为"修复前是什么样"提到它是可以的）
                self.assertNotIn(
                    "Unrecognized thrift field type", message.split("NOTICE")[0]
                )
                # 「真正的二进制载荷」这条限制要如实写出来
                self.assertIn("json.dumps", message)

    def test_binary_accepts_real_bytes_when_called_directly(self):
        """直接调 `_convert` 的调用方可以传 bytes（`json.dumps` 路径传不进来，故单测这一支）。"""
        decoder = self.dc.ThriftJSONDecoder(thrift_class=_Req)

        obj = decoder._convert({"payload": b"\x00\xff"}, self.dc.TType.STRUCT, _Req)

        self.assertEqual(obj.payload, b"\x00\xff")

    def test_binary_accepts_bytearray_and_memoryview(self):
        decoder = self.dc.ThriftJSONDecoder(thrift_class=_Req)

        for raw in (bytearray(b"ab"), memoryview(b"ab")):
            with self.subTest(raw=type(raw).__name__):
                obj = decoder._convert({"payload": raw}, self.dc.TType.STRUCT, _Req)
                self.assertEqual(obj.payload, b"ab")
                self.assertIsInstance(obj.payload, bytes)

    def test_binary_conversion_returns_none_when_struct_is_none(self):
        """反向护栏：整个 struct 为 None 时（可选 struct 字段）不该被当成非法 dict。"""
        decoder = self.dc.ThriftJSONDecoder(thrift_class=_Req)

        obj = decoder._convert({"inner": None}, self.dc.TType.STRUCT, _Req)

        self.assertIsNone(obj.inner)

    def test_ttype_18_is_a_literal_not_TType_dot_BINARY(self):
        """修复依据：Apache thrift 的 `TType` **没有** `BINARY`，所以只能写字面量 18。

        NOTICE: 这条不是形式主义 —— 原作者注释里写的正是 `TType.BINARY`，撞了
        `AttributeError` 之后被改成 `TType.BYTE`(3)，于是 binary 字段永远匹配不到。
        """
        self.assertFalse(
            hasattr(self.dc.TType, "BINARY"),
            "TType 上出现了 BINARY —— 若上游真的加了它，本模块的字面量 18 也应改成引用它",
        )
        self.assertEqual(self.dc._THRIFT_BINARY_TTYPE, 18)

    # ---------------------------- T2：未知字段 ----------------------------
    def test_unknown_param_field_is_rejected_with_legal_fields(self):
        with self.assertRaises(exceptions.ParamsError) as ctx:
            self._convert({"messag": "hi", "count": 7})

        message = str(ctx.exception)
        self.assertIn("messag", message, "要点名写错的键")
        self.assertIn("message", message, "要给出 IDL 里的合法字段（含最可能的那个）")
        self.assertIn("_Req", message, "要点名是哪个 struct")
        self.assertIn("静默丢弃", message, "要说明修复前的后果（假通过）")

    def test_known_fields_are_all_still_converted(self):
        """反向护栏：闸门不能误伤合法输入 —— 11 个字段逐个转对。"""
        obj = self._convert(
            {
                "payload": "p",
                "blobs": ["b"],
                "message": "m",
                "flag": True,
                "count": 7,
                "ratio": 1.5,
                "big": 9007199254740993,
                "tags": ["t"],
                "codes": {1: "one"},
                "items": [{"name": "a", "n": 2}],
                "inner": {"name": "n", "n": 1},
            }
        )

        self.assertEqual(obj.payload, b"p")
        self.assertEqual(obj.blobs, [b"b"])
        self.assertEqual(obj.message, b"m")
        self.assertIs(obj.flag, True)
        self.assertEqual(obj.count, 7)
        self.assertEqual(obj.ratio, 1.5)
        self.assertEqual(obj.big, 9007199254740993)
        self.assertEqual(obj.tags, [b"t"])
        self.assertEqual(obj.codes, {1: b"one"})
        self.assertEqual([(i.name, i.n) for i in obj.items], [(b"a", 2)])
        self.assertEqual((obj.inner.name, obj.inner.n), (b"n", 1))

    def test_unknown_field_in_nested_struct_reports_the_path(self):
        with self.assertRaises(exceptions.ParamsError) as ctx:
            self._convert({"inner": {"nam": "x"}})

        message = str(ctx.exception)
        self.assertIn("inner", message)
        self.assertIn("nam", message)
        self.assertIn("name", message)

    def test_unknown_field_in_list_of_struct_reports_the_index(self):
        with self.assertRaises(exceptions.ParamsError) as ctx:
            self._convert({"items": [{"name": "a", "n": 1}, {"nam": "x"}]})

        message = str(ctx.exception)
        self.assertIn("items[1]", message, "嵌套位置要精确到下标，否则还是找不到")

    def test_struct_field_must_be_a_json_object(self):
        """取值形态写错（给字符串/数字）也要点名，而不是让 `in` 去做子串判断。"""
        for bad in ("not-a-struct", 5):
            with self.subTest(bad=bad):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    self._convert({"inner": bad})

                message = str(ctx.exception)
                self.assertIn("inner", message)
                self.assertIn("JSON 对象", message)

    # ------------------ 端到端：走真实的 send_request ------------------
    def test_send_request_converts_binary_and_keeps_every_field(self):
        sent = _run_t1t2_send_request(
            {
                "payload": "中文",
                "message": "hi",
                "count": 7,
                "blobs": ["a"],
            }
        )

        self.assertEqual(len(sent), 1, "请求必须真的发给底层 client")
        self.assertEqual(sent[0].payload, "中文".encode("utf8"))
        self.assertEqual(sent[0].message, b"hi")
        self.assertEqual(sent[0].count, 7)
        self.assertEqual(sent[0].blobs, [b"a"])

    def test_send_request_rejects_a_typo_in_params(self):
        """T2 的端到端形态：`with_params(messag=...)` 这类写错名字，必须在发出去之前报错。

        修复前：请求照发、服务端收到 message=None、用例只要没断这个字段就「通过」，
        而请求日志打印的是**用户自己写的 params**，看上去一切正常。
        """
        with self.assertRaises(exceptions.ParamsError) as ctx:
            _run_t1t2_send_request({"messag": "hi"})

        message = str(ctx.exception)
        self.assertIn("messag", message)
        self.assertIn("message", message)
        self.assertEqual(_T1T2_SENT_REQUESTS, [], "报错后**不能**把请求发出去")


_T1T2_SENT_REQUESTS = []


class _EchoUnderlyingClient(object):
    """`make_client()` 的替身：只记录被交给底层的那一个 struct 实例。"""

    def echo(self, req):
        _T1T2_SENT_REQUESTS.append(req)
        return {"ok": True}

    def close(self):
        pass


class _EchoService(object):
    class echo_args(object):
        thrift_spec = {1: (12, "req", _Req, False)}


class _EchoThriftModule(object):
    EchoSvc = _EchoService


def _make_echo_client(service, ip, port, **kwargs):
    return _EchoUnderlyingClient()


def _run_t1t2_send_request(params):
    """走**真实的** `ThriftClient.send_request`（thriftpy2 用替身，见模块内既有 helper）。"""
    _T1T2_SENT_REQUESTS.clear()
    with _import_thrift_client_with_fake_thriftpy2(
        load_result=_EchoThriftModule()
    ) as module:
        with mock.patch.object(module, "make_client", _make_echo_client):
            client = module.ThriftClient(
                thrift_file="ok.thrift",
                service_name="EchoSvc",
                ip="127.0.0.1",
                port=1,
            )
            client.send_request(params, "echo")
    return list(_T1T2_SENT_REQUESTS)


def _capture_warnings(func, *args, **kwargs):
    """跑一段逻辑并把 loguru 的 WARNING 收集起来（口径与 `comparators_test` 同款）。"""
    messages = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        result = func(*args, **kwargs)
    finally:
        logger.remove(sink_id)
    return messages, result


class TestBatchDThriftScalarCoercionWarnings(unittest.TestCase):
    """批次 D / **T3**：标量强转「过宽」的可见化 —— **只告警，行为一个字不改**。

    ## 修复前的现场（真机，`.tmp_audit/out_t3_shapes.txt`）

    ```text
    {"flag": "false"}  -> 服务端收到 flag=True     ← 「关掉开关」变成「打开」
    {"count": 3.9}     -> 服务端收到 count=3       ← 小数被**静默截断**
    {"count": "3.7"}   -> 裸 ValueError / 现在点名报错
    {"message": {"a": 1}} -> 服务端收到 "{'a': 1}" ← Python repr 直接当文本发出
    ```

    ## 口径（用户拍板：只告警）

    硬类型校验会打断「`count: \"7\"`」这类很常见的写法（变量插值/CSV 出来就是字符串），
    所以**判定不变**，只在「能精确判定是笔误」的形态上告警：

    | 形态 | 是否告警 | 理由 |
    | --- | --- | --- |
    | `flag: "false"` / `"no"` / `"0"` | ✅ | 看起来是假、Python 结果是 **True**（与直觉相反） |
    | `flag: "abc"` | ✅ | 根本不是布尔词汇，想表达什么无从判断 |
    | `flag: "true"` / `"1"` / `""` | ❌ | Python 的结果与直觉一致 —— 告警只会变成噪声（假报会让真报被无视） |
    | `flag: 2` | ✅ | 只有 0/1 才像布尔 |
    | `count: 3.9` | ✅ | 小数被截断（丢信息） |
    | `count: 3.0` / `"7"` | ❌ | 无损 |
    | `count: "3.7"` / `[1]` / `ratio: "abc"` | 报错（点名） | 根本转不出来；修复前是**裸** `ValueError`/`TypeError` |
    """

    @classmethod
    def setUpClass(cls):
        cls.dc = _import_data_convertor()

    def _convert(self, payload):
        return _capture_warnings(lambda: self.dc.json2thrift(json.dumps(payload), _Req))

    # ------------------------- bool 字段 -------------------------
    def test_falsey_string_warns_and_behaviour_is_unchanged(self):
        """`"false"` 这类写法现在会告警 —— 但**行为不变**（仍然是 True）。"""
        for text in ("false", "False", "no", "off", "0", " false "):
            with self.subTest(text=text):
                messages, obj = self._convert({"flag": text})

                self.assertEqual(len(messages), 1, messages)
                self.assertIn("flag", messages[0])
                self.assertIn("bool", messages[0])
                self.assertIn("True", messages[0])
                self.assertIn("不要加引号", messages[0], "必须给出可操作的改法")
                # 这两句是「看起来是假、实际为真」这条分支**专属**的解释 ——
                # 没有它，「false 会变成 True」这件事虽然报了警、却没说清为什么。
                self.assertIn("关掉开关", messages[0])
                # 行为不变：仍然是 Python 的「非空即真」
                self.assertIs(obj.flag, True)

    def test_non_boolean_text_warns(self):
        messages, obj = self._convert({"flag": "abc"})

        self.assertEqual(len(messages), 1, messages)
        self.assertIn("非空即真", messages[0])
        self.assertNotIn(
            "关掉开关", messages[0], "这不是「看起来是假」的形态，解释该用另一句"
        )
        self.assertIs(obj.flag, True)

    def test_intention_matching_values_do_not_warn(self):
        """反向护栏（最重要的一条）：**不许假报** —— 结果与直觉一致的形态不告警。"""
        cases = {
            "true": True,
            "TRUE": True,
            "yes": True,
            "on": True,
            "1": True,
            "": False,
            "   ": True,  # 纯空白也是非空字符串 → True，但用户写空白本身就是无意义，不额外告警
            True: True,
            False: False,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                messages, obj = self._convert({"flag": value})
                self.assertEqual(messages, [], f"{value!r} 不该告警：{messages}")
                self.assertIs(obj.flag, expected)

    def test_non_binary_number_warns_but_zero_and_one_do_not(self):
        for value in (2, -1, 3.5):
            with self.subTest(value=value):
                messages, obj = self._convert({"flag": value})
                self.assertEqual(len(messages), 1, messages)
                self.assertIn("非零数字", messages[0])
                self.assertIs(obj.flag, True)

        for value in (0, 1, 0.0, 1.0):
            with self.subTest(value=value):
                messages, _obj = self._convert({"flag": value})
                self.assertEqual(messages, [], messages)

    # ------------------------- 数字字段 -------------------------
    def test_float_truncation_warns(self):
        messages, obj = self._convert({"count": 3.9})

        self.assertEqual(len(messages), 1, messages)
        self.assertIn("count", messages[0])
        self.assertIn("i32", messages[0], "要点名 IDL 类型，而不是只说一个数字")
        self.assertIn("截断", messages[0])
        self.assertEqual(obj.count, 3, "行为不变：仍然是 int(3.9) == 3")

    def test_lossless_number_forms_do_not_warn(self):
        """反向护栏：`3.0`、`7`、`"7"`（变量插值/CSV 的常见形态）都不告警。"""
        for value in (3.0, 7, "7", 2**62):
            with self.subTest(value=value):
                messages, _obj = self._convert({"count": value, "big": value})
                self.assertEqual(messages, [], messages)

    def test_unconvertible_value_raises_a_named_error_instead_of_naked_valueerror(self):
        """修复前是裸 `ValueError: invalid literal for int() with base 10: '3.7'`（无字段名）。"""
        cases = (
            ({"count": "3.7"}, "count", "i32"),
            ({"count": [1]}, "count", "i32"),
            ({"big": {}}, "big", "i64"),
            ({"ratio": "abc"}, "ratio", "double"),
            ({"ratio": [1]}, "ratio", "double"),
        )
        for payload, field, type_name in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    self.dc.json2thrift(json.dumps(payload), _Req)

                message = str(ctx.exception)
                self.assertIn(field, message, "报错必须点名是哪个字段")
                self.assertIn(type_name, message, "报错必须点名 IDL 类型")
                self.assertIn(
                    repr(list(payload.values())[0]), message, "要把原值打出来"
                )

    # ------------------------- string 字段 -------------------------
    def test_non_text_value_for_string_field_warns_and_keeps_str_coercion(self):
        """行为不变：仍然是 `str(val)`（所以 dict 会变成 Python repr 发出去）—— 只是现在看得见。"""
        messages, obj = self._convert({"message": 7})
        self.assertEqual(len(messages), 1, messages)
        self.assertIn("message", messages[0])
        self.assertEqual(obj.message, "7")

        messages, obj = self._convert({"message": {"a": 1}})
        self.assertEqual(len(messages), 1, messages)
        self.assertIn("Python repr", messages[0])
        self.assertEqual(obj.message, "{'a': 1}")

    # ------------------------- 去重与端到端 -------------------------
    def test_same_kind_of_warning_is_emitted_once_per_request(self):
        """`list<string>` 里三个数字只报一条（否则大列表会刷屏）。"""
        messages, obj = self._convert({"tags": [1, 2, 3]})

        self.assertEqual(len(messages), 1, messages)
        self.assertEqual(obj.tags, ["1", "2", "3"], "行为不变：逐元素 str()")

    def test_a_fully_valid_payload_emits_no_warnings(self):
        """反向护栏：类型都对时一条告警都不能有。"""
        payload = {
            "payload": "p",
            "blobs": ["b"],
            "message": "m",
            "flag": True,
            "count": 7,
            "ratio": 1.5,
            "big": 9007199254740993,
            "tags": ["t"],
            "codes": {1: "one"},
            "items": [{"name": "a", "n": 2}],
            "inner": {"name": "n", "n": 1},
        }

        messages, _obj = self._convert(payload)

        self.assertEqual(messages, [], f"对一个完全正确的请求告警了：{messages}")

    def test_send_request_warns_and_still_sends_the_coerced_value(self):
        """端到端：告警 + **行为不变**（请求照发，`"false"` 仍然是 True）。

        NOTICE: 这条正是「只告警」这个口径的守门人 —— 如果哪天有人把它改成硬校验，
        这里会因为「请求没发出去」而变红，提醒他先确认口径变更。
        """
        messages, sent = _capture_warnings(
            _run_t1t2_send_request, {"flag": "false", "message": "hi"}
        )

        self.assertEqual(len(messages), 1, messages)
        self.assertIn("flag", messages[0])
        self.assertEqual(len(sent), 1, "行为不变：请求必须照发")
        self.assertIs(sent[0].flag, True)
        self.assertEqual(sent[0].message, b"hi")
