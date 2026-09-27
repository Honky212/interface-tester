import os
import unittest
from unittest import mock

import requests

from examples.postman_echo.request_methods.request_with_functions_test import (
    TestCaseRequestWithFunctions,
)
from interfacetester import Config, InterfaceTester, RunRequest, Step
from interfacetester.step_request import StepRequestValidation
from interfacetester.utils import DEFAULT_TIMEOUT_SECONDS, HTTP_BIN_URL


class TestRunRequest(unittest.TestCase):
    def test_run_request(self):
        runner = TestCaseRequestWithFunctions().test_start()
        summary = runner.get_summary()
        self.assertTrue(summary.success)
        self.assertEqual(summary.name, "request methods testcase with functions")
        self.assertEqual(len(summary.step_results), 3)
        self.assertEqual(summary.step_results[0].name, "get with params")
        self.assertEqual(summary.step_results[1].name, "post raw text")
        self.assertEqual(summary.step_results[2].name, "post form data")


def _make_timeout_case(name, config_chain, step):
    """按需拼一个 InterfaceTester 子类（类名不叫 Test* → 不会被 pytest 收集）。"""

    class _TimeoutCase(InterfaceTester):
        config = config_chain
        teststeps = [step]

    _TimeoutCase.__name__ = name
    return _TimeoutCase


class _RequestCaptured(Exception):
    """spy 捕获到请求参数后用它中断用例执行（不真发请求）。"""


def _capture_request_kwargs(testcase_cls):
    """跑一次用例，返回实际传给 requests.Session.request 的 kwargs。

    spy 拿到 kwargs 后立刻中断，因此既不依赖网络，也不受该用例断言成败影响。
    """
    captured = {}

    def spy(session_self, method, url, **kwargs):
        captured.update(kwargs)
        raise _RequestCaptured()

    with mock.patch.object(requests.Session, "request", spy):
        try:
            testcase_cls().test_start()
        except _RequestCaptured:
            pass

    return captured


def _capture_timeout(testcase_cls):
    """返回实际传给 requests 的 timeout。

    「真实请求链路上默认值是否是 120」由 tests/client_test.py::TestTimeoutResolution 覆盖。
    """
    return _capture_request_kwargs(testcase_cls).get("timeout")


class TestRequestTimeoutFallback(unittest.TestCase):
    """请求超时的回落顺序：request.timeout → config.timeout → 默认值（120s）。

    NOTICE: 这三层都只把值「覆盖成数字」；任何一层都没有值时交给 HttpSession 的默认超时。
    绝不能把 None 传给 requests（requests 里 timeout=None 的语义是「无限等待」，
    比保留 120s 默认值危险得多），因此下面每种组合都断言拿到的是具体数字。
    """

    def test_step_timeout_wins_over_config(self):
        """step 级显式设置 > config 级设置。"""
        step = Step(
            RunRequest("step timeout")
            .get("/get")
            .set_timeout(1)
            .validate()
            .assert_equal("status_code", 200)
        )
        testcase_cls = _make_timeout_case(
            "StepTimeoutCase",
            Config("step timeout wins").base_url(HTTP_BIN_URL).timeout(5),
            step,
        )

        self.assertEqual(_capture_timeout(testcase_cls), 1.0)

    def test_config_timeout_applies_when_step_not_set(self):
        """step 未设置时用 config 级配置（修复前 config 无此能力，只能逐个 step 写）。"""
        step = Step(
            RunRequest("config timeout")
            .get("/get")
            .validate()
            .assert_equal("status_code", 200)
        )
        testcase_cls = _make_timeout_case(
            "ConfigTimeoutCase",
            Config("config timeout").base_url(HTTP_BIN_URL).timeout(7),
            step,
        )

        self.assertEqual(_capture_timeout(testcase_cls), 7.0)

    def test_falls_back_to_session_default(self):
        """两层都没设置 → 交给 HttpSession 默认值（120s），且传下去的不是 None。"""
        step = Step(
            RunRequest("no timeout")
            .get("/get")
            .validate()
            .assert_equal("status_code", 200)
        )
        testcase_cls = _make_timeout_case(
            "DefaultTimeoutCase", Config("default timeout").base_url(HTTP_BIN_URL), step
        )

        timeout = _capture_timeout(testcase_cls)
        self.assertIsNotNone(timeout)
        self.assertEqual(timeout, float(DEFAULT_TIMEOUT_SECONDS))

    def test_zero_timeout_is_not_swallowed(self):
        """`timeout: 0`（立即超时）是合法值，不能被 `or` 之类的真值判断吞掉。"""
        step = Step(
            RunRequest("zero timeout")
            .get("/get")
            .set_timeout(0)
            .validate()
            .assert_equal("status_code", 200)
        )
        testcase_cls = _make_timeout_case(
            "ZeroTimeoutCase", Config("zero timeout").base_url(HTTP_BIN_URL), step
        )

        # 这里只关心「0 被原样传下去」而不是被替换成默认值
        self.assertEqual(_capture_timeout(testcase_cls), 0.0)

    def test_config_timeout_supports_env_reference(self):
        """config.timeout 支持 ${ENV(...)}（解析出来是字符串，需规范成数字）。"""
        step = Step(
            RunRequest("env timeout")
            .get("/get")
            .validate()
            .assert_equal("status_code", 200)
        )
        testcase_cls = _make_timeout_case(
            "EnvTimeoutCase",
            Config("env timeout").base_url(HTTP_BIN_URL).timeout("${ENV(TEST_TIMEOUT)}"),
            step,
        )

        with mock.patch.dict(os.environ, {"TEST_TIMEOUT": "3"}):
            self.assertEqual(_capture_timeout(testcase_cls), 3.0)


class TestRequestNetworkOptions(unittest.TestCase):
    """代理 / 客户端证书 / stream 的透传（修复前这三个字段会被静默丢弃）。

    NOTICE: `TRequest` 原先没有 proxies/cert/stream 字段，而 pydantic 默认 extra="ignore"，
    于是 YAML 里写了也会被丢掉——用户以为配了代理，实际直连。现在补成正式字段，
    这里锁定「真的传给了 requests」以及「未设置时不传 None」。
    """

    def _make_case(self, name, step):
        return _make_timeout_case(name, Config(name).base_url(HTTP_BIN_URL), step)

    def test_proxies_cert_stream_are_forwarded(self):
        step = Step(
            RunRequest("network options")
            .get("/get")
            .with_proxies(http="http://127.0.0.1:8888")
            .set_cert(["/tmp/client.crt", "/tmp/client.key"])
            .set_stream(False)
            .validate()
            .assert_equal("status_code", 200)
        )

        kwargs = _capture_request_kwargs(self._make_case("NetworkOptionsCase", step))

        self.assertEqual(kwargs.get("proxies"), {"http": "http://127.0.0.1:8888"})
        # 两元素列表统一转成元组，与 requests 的约定一致
        self.assertEqual(kwargs.get("cert"), ("/tmp/client.crt", "/tmp/client.key"))
        self.assertIs(kwargs.get("stream"), False)

    def test_unset_network_options_are_not_passed_as_none(self):
        """未设置时不能把 None 传下去。

        NOTICE: 若 step_request 把 `stream=None` 传下去，client 里的
        `kwargs.setdefault("stream", True)` 就不会生效，而 requests 会把 None 当成 False
        ——响应体被立刻读完，`client.py` 取底层 socket 地址（client/server IP:Port）就失效。
        这里断言：stream 拿到的是 client 的兜底值 True（而不是 None）。
        """
        step = Step(
            RunRequest("no network options")
            .get("/get")
            .validate()
            .assert_equal("status_code", 200)
        )

        kwargs = _capture_request_kwargs(self._make_case("NoNetworkOptionsCase", step))

        self.assertIs(kwargs.get("stream"), True)
        self.assertNotIn("proxies", kwargs)
        self.assertNotIn("cert", kwargs)


class TestFrameworkHeaderCaseInsensitivity(unittest.TestCase):
    """0920 / **缺陷 2**：框架给"用户没写过的头"补默认值时，判据必须**大小写不敏感**。

    ## 修复前的现场（`.tmp_audit/repro_d2.py`，服务端视角）

    ```yaml
    config:
      oauth2: {token_url: ..., client_id: ..., client_secret: ...}
    teststeps:
    - request:
        url: /headers
        method: GET
        headers:
          authorization: Bearer USER-TOKEN     # 用户显式写的（小写）
    ```

    ```text
    交给 requests 的 headers: {'authorization': 'Bearer USER-TOKEN',
                              'Authorization': 'Bearer FRAMEWORK-TOKEN'}   ← 两个键
    服务端实际收到:            {'Authorization': 'Bearer FRAMEWORK-TOKEN'}   ← 用户的被吃掉
    ```

    HTTP 头名是**大小写不敏感**的（RFC 7230 §3.2），requests 的 `CaseInsensitiveDict`
    会把这两个键合并成**后写者优先** —— 框架静默替换了用户显式指定的凭据。
    """

    def _capture(self, header_name, oauth2_token="FRAMEWORK-TOKEN"):
        step = Step(
            RunRequest("header case")
            .get("/headers")
            .with_headers(**{header_name: "Bearer USER-TOKEN"})
            .validate()
            .assert_equal("status_code", 200)
        )
        testcase_cls = _make_timeout_case(
            "HeaderCaseCase", Config("header case").base_url(HTTP_BIN_URL), step
        )
        with mock.patch.object(
            InterfaceTester, "get_oauth2_token", lambda self: oauth2_token
        ):
            return _capture_request_kwargs(testcase_cls).get("headers") or {}

    def test_lowercase_user_authorization_is_not_overwritten(self):
        headers = self._capture("authorization")

        keys = [key for key in headers if key.lower() == "authorization"]
        self.assertEqual(keys, ["authorization"], f"框架又注入了一个重复头：{keys}")
        self.assertEqual(headers["authorization"], "Bearer USER-TOKEN")

    def test_other_casings_are_not_overwritten_either(self):
        """`Authorization` / `AUTHORIZATION` 同样不能被覆盖。"""
        for name in ("Authorization", "AUTHORIZATION", "AuThOrIzAtIoN"):
            with self.subTest(name=name):
                headers = self._capture(name)
                keys = [key for key in headers if key.lower() == "authorization"]
                self.assertEqual(keys, [name])
                self.assertEqual(headers[name], "Bearer USER-TOKEN")

    def test_token_is_injected_when_user_did_not_set_authorization(self):
        """反向护栏：用户没写 Authorization 时，框架**仍然**要自动注入 token。"""
        headers = self._capture("X-Trace-Id")

        self.assertEqual(headers.get("Authorization"), "Bearer FRAMEWORK-TOKEN")

    def test_user_request_id_is_not_overwritten(self):
        """同族：`interfacetester-Request-ID` 的 setdefault 也必须大小写不敏感。"""
        for name in ("interfacetester-Request-ID", "interfacETester-request-id"):
            with self.subTest(name=name):
                step = Step(
                    RunRequest("req id case")
                    .get("/headers")
                    .with_headers(**{name: "user-provided-id"})
                    .validate()
                    .assert_equal("status_code", 200)
                )
                testcase_cls = _make_timeout_case(
                    "RequestIdCase", Config("req id").base_url(HTTP_BIN_URL), step
                )
                headers = _capture_request_kwargs(testcase_cls).get("headers") or {}

                keys = [
                    key
                    for key in headers
                    if key.lower() == "interfacetester-request-id"
                ]
                self.assertEqual(keys, [name], f"框架又注入了一个重复的 Request-ID：{keys}")
                self.assertEqual(headers[name], "user-provided-id")


class TestCustomComparatorDispatch(unittest.TestCase):
    """P0：`StepRequestValidation` 对自定义断言算子（`assert_<算子名>`）的动态分发。

    背景：`make.py` 会把 YAML 里 `validate` 的算子名**无条件**渲染成
    `.assert_<算子名>(...)`，而该类原先只有 18 个硬编码 `assert_*` 方法，
    于是「写在 debugtalk.py 里的自定义算子」会在 pytest 收集阶段整文件
    `AttributeError` —— 一条用例都收集不到（评估文档零章）。
    这里锁定：未知识别名被还原成标准 validator、链式调用继续可用、
    非 `assert_` 前缀不污染属性探测、以及缺少 `__step` 时不会递归。
    """

    def _make_validation(self):
        validation = RunRequest("custom comparator probe").get("/get").validate()
        return validation, validation.struct()

    def test_unknown_operator_is_dispatched_to_validators(self):
        validation, step_struct = self._make_validation()

        returned = validation.assert_has_keys(
            "body", ["alpha", "beta"], "must have keys"
        )

        self.assertIs(returned, validation, "动态方法必须支持链式调用")
        self.assertEqual(
            step_struct.validators,
            [{"has_keys": ["body", ["alpha", "beta"], "must have keys"]}],
        )

    def test_default_message_is_empty_string(self):
        validation, step_struct = self._make_validation()

        validation.assert_my_cmp("status_code", 200)

        self.assertEqual(
            step_struct.validators, [{"my_cmp": ["status_code", 200, ""]}]
        )

    def test_dispatch_chains_with_builtin_assert_methods(self):
        """自定义算子与 18 个内置算子混用时，顺序与形态都要正确。"""
        validation, step_struct = self._make_validation()

        validation.assert_equal("status_code", 200).assert_my_cmp(
            "body.code", 0
        ).assert_not_equal("body.msg", "bad")

        self.assertEqual(
            step_struct.validators,
            [
                {"equal": ["status_code", 200, ""]},
                {"my_cmp": ["body.code", 0, ""]},
                {"not_equal": ["body.msg", "bad", ""]},
            ],
        )

    def test_non_assert_attribute_raises_attribute_error(self):
        """非 `assert_` 前缀必须按「属性不存在」处理，否则会污染探测/dunder/copy。"""
        validation, _ = self._make_validation()

        for name in ["get", "structs", "_repr_html_", "__deepcopy__", "assert_"]:
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    getattr(validation, name)

    def test_hasattr_distortion_is_expected_and_documented(self):
        """已知副作用：`hasattr(x, "assert_任意名")` 恒为 True（评估文档 3.1 已记录）。

        这是动态分发换取「一处改动解锁所有扩展算子」的代价，靠 make 阶段白名单兜住拼错。
        """
        validation, _ = self._make_validation()

        self.assertTrue(hasattr(validation, "assert_anything_at_all"))
        self.assertTrue(hasattr(validation, "assert_equal"))

    def test_missing_step_attribute_does_not_recurse(self):
        """未走 `__init__` 时取 `__step` 不能递归死循环（必须立刻 AttributeError）。"""
        orphan = StepRequestValidation.__new__(StepRequestValidation)

        with self.assertRaises(AttributeError):
            orphan._StepRequestValidation__step
        # hasattr 走同一条路径也不会递归
        self.assertFalse(hasattr(orphan, "_StepRequestValidation__step"))
        # 类上真实存在的内置方法不受动态分发影响
        self.assertTrue(hasattr(orphan, "assert_equal"))


class TestHttpStepResultDataIsolation(unittest.TestCase):
    """HTTP 侧的**对照组**：每步一份 `SessionData`。

    NOTICE（0919-9 / §三.3）：这不是「新发现的行为」，而是「thrift/SQL 该对齐的参照物」。
    `HttpSession.request` 第 233 行每发一次请求就 `self.data = SessionData()`，
    所以 HTTP step 天然不会互相污染——`docs/仍然存在的问题0919-3.txt` 把 thrift/SQL 的
    污染描述成「框架级共享设计」，正是因为没有这个对照组（复核结论 §四.2 已订正）。

    钉住它，是为了防止有人把三个协议「统一」成复用同一个 `runner.session.data`：
    那样 thrift/SQL 这次修复会被一起改回去，而且**报告失真**会扩散到 HTTP 主路径。
    """

    def test_http_steps_do_not_share_step_data(self):
        class _Case(InterfaceTester):
            config = Config("http data isolation").base_url(HTTP_BIN_URL)
            teststeps = [
                Step(RunRequest("http step 1").get("/get")),
                Step(RunRequest("http step 2").get("/get")),
            ]

        summary = _Case().test_start().get_summary()

        first, second = summary.step_results[0].data, summary.step_results[1].data
        self.assertIsNot(first, second)
        # 每一步各自记录自己的那一次请求，不是共用一个累加器
        self.assertEqual(len(first.req_resps), 1)
        self.assertEqual(len(second.req_resps), 1)
