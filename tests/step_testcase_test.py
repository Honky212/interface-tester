import unittest
from unittest import mock

from loguru import logger

from interfacetester import Config, InterfaceTester, RunRequest, RunTestCase, Step
from interfacetester import runner as runner_module
from interfacetester.step_testcase import RunTestCase as _RunTestCase  # noqa: F401
from interfacetester.utils import HTTP_BIN_URL
from examples.postman_echo.request_methods.request_with_functions_test import (
    TestCaseRequestWithFunctions,
)


class TestRunTestCase(unittest.TestCase):
    def setUp(self):
        self.runner = TestCaseRequestWithFunctions()
        self.runner.test_start()

    def test_run_testcase_by_path(self):

        step_result = (
            RunTestCase("run referenced testcase")
            .call(TestCaseRequestWithFunctions)
            .run(self.runner)
        )
        self.assertTrue(step_result.success)
        self.assertEqual(step_result.name, "run referenced testcase")
        self.assertEqual(len(step_result.data), 3)
        self.assertEqual(step_result.data[0].name, "get with params")
        self.assertEqual(step_result.data[1].name, "post raw text")
        self.assertEqual(step_result.data[2].name, "post form data")


# ---------------------------------------------------------------------------
# 0918-8 / M30：引用用例与父用例共用 .run.log → 子用例每行日志写两遍
# ---------------------------------------------------------------------------


class _ChildCase(InterfaceTester):
    """被引用的子用例（2 个 step → 日志里有多条可辨认的行）。"""

    config = Config("child case").base_url(HTTP_BIN_URL).verify(False)
    teststeps = [
        Step(
            RunRequest("child step A")
            .get("/status/200")
            .validate()
            .assert_equal("status_code", 200)
        ),
        Step(
            RunRequest("child step B")
            .get("/status/201")
            .validate()
            .assert_equal("status_code", 201)
        ),
    ]


class _ParentCase(InterfaceTester):
    """父用例：先引用子用例，再跑一个自己的 step。"""

    config = Config("parent case").base_url(HTTP_BIN_URL).verify(False)
    teststeps = [
        Step(RunTestCase("run child case").call(_ChildCase)),
        Step(
            RunRequest("parent step")
            .get("/status/200")
            .validate()
            .assert_equal("status_code", 200)
        ),
    ]


class TestReferencedCaseLogNotDuplicated(unittest.TestCase):
    """M30：父子共用 `.run.log` 时，每一行只能出现一次。

    修复前子 runner 用 `runner.case_id`，而 `.run.log` 路径由 case_id 拼出 —— 两个 loguru
    sink 写同一个文件，子用例每行写两遍（实测 115 行、`run step begin: child step`
    出现两次、`status_code: 200` 出现四次）。读日志的人会以为步骤跑了两次，
    Allure 里同一个文件也被挂两次。
    """

    def _run_parent(self):
        added = []
        original_add = runner_module.logger.add

        def _counting_add(*args, **kwargs):
            sink_id = original_add(*args, **kwargs)
            added.append(args[0] if args else kwargs.get("sink"))
            return sink_id

        with mock.patch.object(runner_module.logger, "add", _counting_add):
            runner = _ParentCase()
            runner.test_start()

        return runner, added

    def _log_text(self, runner):
        log_path = runner.get_summary().log
        with open(log_path, encoding="utf-8") as f:
            return log_path, f.read()

    def test_child_step_lines_appear_exactly_once(self):
        runner, _added = self._run_parent()

        _log_path, text = self._log_text(runner)

        for marker in (
            "run step begin: child step A",
            "run step begin: child step B",
            "run step begin: parent step",
        ):
            with self.subTest(marker=marker):
                self.assertEqual(text.count(marker), 1)

    def test_only_one_log_sink_is_added_for_parent_and_child(self):
        """父用例 + 引用的子用例只允许新增**一个** sink。"""
        _runner, added = self._run_parent()

        self.assertEqual(len(added), 1, f"新增了 {len(added)} 个 sink: {added}")

    def test_child_log_goes_into_parent_log_file(self):
        """子用例的日志必须落在父用例那个文件里（不能丢，也不能另起文件）。"""
        runner, _added = self._run_parent()

        log_path, text = self._log_text(runner)

        self.assertIn(runner.case_id, log_path)
        self.assertIn("run step begin: child step A", text)
        self.assertIn("run step end: child step A", text)

    def test_log_sink_registry_is_released_after_run(self):
        """跑完必须归还 sink：登记表非空就意味着文件句柄/处理器泄漏。"""
        self._run_parent()

        self.assertEqual(runner_module._run_log_sinks, {})

    def test_sibling_cases_do_not_share_log_files(self):
        """回归（0918 更早的修复）：两个先后跑的顶层用例不能互相写进对方的日志。"""
        first = _ParentCase()
        first.test_start()
        second = _ParentCase()
        second.test_start()

        first_path, first_text = self._log_text(first)
        second_path, second_text = self._log_text(second)

        self.assertNotEqual(first_path, second_path)
        self.assertEqual(runner_module._run_log_sinks, {})
        # 保证两次运行确实都写了日志（否则「不串台」是空洞的）
        self.assertIn("run step begin: parent step", first_text)
        self.assertIn("run step begin: parent step", second_text)

    def test_allure_attaches_run_log_once(self):
        """同一个 `.run.log` 不能挂两次 Allure 附件（父子各挂一次 = 报告里两份）。"""
        attached = []
        # MagicMock：`ALLURE.step(...)` 在 runner 里是当作上下文管理器用的
        fake_allure = mock.MagicMock()
        fake_allure.attach.file.side_effect = lambda path, **kwargs: attached.append(
            (path, kwargs.get("name"))
        )
        fake_allure.attachment_type = mock.Mock(TEXT="text")

        with mock.patch.object(runner_module, "ALLURE", fake_allure):
            runner = _ParentCase()
            runner.test_start()

        log_path, _text = self._log_text(runner)
        log_attachments = [
            (path, name) for path, name in attached if name == "all log"
        ]
        self.assertEqual(len(log_attachments), 1)
        self.assertEqual(log_attachments[0][0], log_path)


class TestRunLogSinkRegistry(unittest.TestCase):
    """登记表的边界：外部 `logger.remove()` 之后不能复用一个已死的 sink。"""

    def tearDown(self):
        runner_module._run_log_sinks.clear()

    def test_dead_sink_is_not_reused(self):
        """`utils.init_logger()` 会 `logger.remove()` 掉全部 sink。

        此时登记表里的 sink id 已经失效，若还照旧复用，子用例的日志会**静默丢失**——
        这里要求重新挂一个（宁可多一个 handler，也不能丢日志）。
        """
        runner = _ParentCase()
        runner._setup_runner()
        first_sink_id = runner._SessionRunner__acquire_run_log_sink()
        self.assertIsNotNone(first_sink_id)

        # 模拟 init_logger：清掉所有 sink（登记表里的那条随之失效）
        logger.remove()

        second_sink_id = runner._SessionRunner__acquire_run_log_sink()

        self.assertIsNotNone(second_sink_id, "死 sink 被复用了，子用例日志会静默丢失")
        self.assertNotEqual(first_sink_id, second_sink_id)

        logger.remove(second_sink_id)
        runner_module._run_log_sinks.clear()


# ---------------------------------------------------------------------------
# 0920 批次 2 / N16：被引用用例的 config.skip 必须真的生效
# ---------------------------------------------------------------------------


class TestReferencedCaseSkipIsHonored(unittest.TestCase):
    """N16：`config.skip` 此前只生成 `@pytest.mark.skip`，而**引用**是直接调用
    `test_start()` —— pytest 标记对直接调用无效，运行期也没人读 `skip`，
    于是"声明未就绪"的用例被引用时会**照常执行**。

    修复前实测（`.tmp_audit/verify_aud3.py`）：

    ```text
    hrun child.yml    → 1 skipped        （pytest 标记生效）
    hrun parent.yml   → 1 failed         （标记无效，子用例里的步骤真的跑了）
    ```

    现在：`hmake` 额外生成 `.skip(...)` → 值进入运行期 `TConfig` →
    `runner.test_start` 在**被引用**时跳过步骤并**响亮告警**（不是静默跳过）。
    """

    def _child_case(self, name, skip_value, *, marker_reason=None):
        chain = Config(name).base_url(HTTP_BIN_URL)
        if skip_value is not None:
            chain = chain.skip(skip_value)

        class _Child(InterfaceTester):
            config = chain
            teststeps = [
                Step(
                    # 故意失败：只要它被执行，父用例就会红
                    RunRequest("a step that must not run when referenced")
                    .get("/status/200")
                    .validate()
                    .assert_equal("status_code", 599)
                )
            ]

        _Child.__name__ = marker_reason or "TestCaseChild"
        return _Child

    def _parent_case(self, child):
        class _Parent(InterfaceTester):
            config = Config("parent").base_url(HTTP_BIN_URL)
            teststeps = [Step(RunTestCase("call the skipped child").call(child))]

        return _Parent

    def test_referenced_child_with_skip_does_not_run_its_steps(self):
        """核心判据：子用例声明 skip → 被引用时**不许**执行它的步骤。"""
        child = self._child_case("skipped child", "还没就绪")
        parent = self._parent_case(child)

        summary = parent().test_start().get_summary()

        self.assertTrue(
            summary.success,
            "被引用用例声明了 `config.skip`，它的步骤却照常执行（N16 未修）",
        )

    def test_referenced_child_with_skip_true_is_also_honored(self):
        """`skip: true`（无条件跳过）同样生效。"""
        child = self._child_case("skipped child bool", True)
        summary = self._parent_case(child)().test_start().get_summary()

        self.assertTrue(summary.success)

    def test_skip_is_not_silent(self):
        """跳过必须**可见**：日志里要说明"因为声明了 skip 才没跑"。"""
        child = self._child_case("skipped child logged", "还没就绪")
        parent = self._parent_case(child)

        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            parent().test_start()
        finally:
            logger.remove(sink_id)

        text = "\n".join(str(m) for m in messages)
        self.assertIn("config.skip", text)
        self.assertIn("已跳过", text)
        self.assertIn("还没就绪", text, "要把用户写的原因带出来")

    def test_child_without_skip_still_runs(self):
        """反向护栏：没声明 skip 的被引用用例照旧执行（不许把引用功能改坏）。"""
        child = self._child_case("normal child", None)

        class _PassingChild(InterfaceTester):
            config = Config("normal child").base_url(HTTP_BIN_URL)
            teststeps = [
                Step(
                    RunRequest("child step runs")
                    .get("/status/200")
                    .validate()
                    .assert_equal("status_code", 200)
                )
            ]

        summary = self._parent_case(_PassingChild)().test_start().get_summary()
        self.assertTrue(summary.success)

    def test_standalone_case_with_skip_still_runs_at_runtime(self):
        """边界：**独立**用例的 skip 由 pytest 标记负责，运行期**不**拦。

        这不是缺陷，而是刻意划的边界：若运行期把独立用例也跳过，
        pytest 侧会把它记成 `passed` 而不是 `skipped`（报告口径回退）。
        本用例用"里面的必失败步骤真的执行了"来证明运行期没有拦它。
        """
        child = self._child_case("standalone skipped", "还没就绪")

        with self.assertRaises(Exception) as ctx:
            child().test_start()

        self.assertIn("status_code", str(ctx.exception))

