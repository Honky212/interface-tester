from typing import Callable, Text

from loguru import logger

from interfacetester import exceptions
from interfacetester.models import IStep, StepResult, TStep, TestCaseSummary
from interfacetester.runner import InterfaceTester
from interfacetester.step_request import call_hooks
from interfacetester.utils import mask_sensitive_variables


def run_step_testcase(runner: InterfaceTester, step: TStep) -> StepResult:
    """run teststep: referenced testcase"""
    step_result = StepResult(name=step.name, step_type="testcase")
    step_variables = runner.merge_step_variables(step.variables)
    step_export = step.export

    # setup hooks
    if step.setup_hooks:
        call_hooks(runner, step.setup_hooks, step_variables, "setup testcase")

    # TODO: override testcase with current step name/variables/export

    # step.testcase is a referenced testcase, e.g. RequestWithFunctions
    ref_case_runner = step.testcase()
    ref_case_runner.set_referenced().with_session(runner.session).with_case_id(
        runner.case_id
    ).with_variables(step_variables).with_export(step_export).test_start()

    # teardown hooks
    if step.teardown_hooks:
        call_hooks(runner, step.teardown_hooks, step_variables, "teardown testcase")

    # NOTICE（0919-13 / 批次 E，§二.2）：**运行期取值直接问 runner，不再经 summary**。
    #
    # 修复前是 `step_result.export_vars = summary.in_out.export_vars` —— 于是
    # 「报告里的展示副本」与「运行期传递的值」是**同一个对象**，`get_summary()`
    # 就永远不能对它脱敏（一脱敏，被引用用例的 export 就变成 `******`，
    # 静默破坏变量传递）。现在拆开了：这里拿的是**未脱敏的真值**，
    # summary 里的那份只是给别人看的副本（见 `runner.get_summary` 的 NOTICE）。
    #
    # NOTICE（0918-3 / H4）：这里必须显式用**严格**模式。
    # `get_summary()` 的报告路径默认已放宽为"缺失只告警"（否则 --save-tests 会因为
    # 一个没导出的变量丢掉整份 summary）；但**变量传递**路径相反 —— 外层 step 声明了
    # `export`，被引用用例却没真的导出，契约就没被满足，必须响亮失败。
    step_result.export_vars = ref_case_runner.get_export_variables(strict=True)

    # 展示用：`success` 与每个 step 的记录（`data`）仍从 summary 取
    summary: TestCaseSummary = ref_case_runner.get_summary(strict_export=True)
    step_result.data = summary.step_results  # list of step data
    step_result.success = summary.success

    if step_result.export_vars:
        # 日志是**展示**通道，与 `client.py` 的请求侧脱敏同口径：只打脱敏副本，
        # 运行期值不受影响（0919-3 / L36 的 extract 日志同理）。
        logger.info(
            f"export variables: {mask_sensitive_variables(step_result.export_vars)}"
        )

    return step_result


class StepRefCase(IStep):
    def __init__(self, step: TStep):
        self.__step = step

    def teardown_hook(self, hook: Text, assign_var_name: Text = None) -> "StepRefCase":
        if assign_var_name:
            self.__step.teardown_hooks.append({assign_var_name: hook})
        else:
            self.__step.teardown_hooks.append(hook)

        return self

    def export(self, *var_name: Text) -> "StepRefCase":
        self.__step.export.extend(var_name)
        return self

    def struct(self) -> TStep:
        return self.__step

    def name(self) -> Text:
        return self.__step.name

    def type(self) -> Text:
        return "testcase"

    def run(self, runner: InterfaceTester):
        return run_step_testcase(runner, self.__step)


class RunTestCase(object):
    def __init__(self, name: Text):
        self.__step = TStep(name=name)

    def with_variables(self, **variables) -> "RunTestCase":
        self.__step.variables.update(variables)
        return self

    def with_retry(self, retry_times, retry_interval) -> "RunTestCase":
        self.__step.retry_times = retry_times
        self.__step.retry_interval = retry_interval
        return self

    def setup_hook(self, hook: Text, assign_var_name: Text = None) -> "RunTestCase":
        if assign_var_name:
            self.__step.setup_hooks.append({assign_var_name: hook})
        else:
            self.__step.setup_hooks.append(hook)

        return self

    def call(self, testcase: Callable) -> StepRefCase:
        if issubclass(testcase, InterfaceTester):
            # referenced testcase object
            self.__step.testcase = testcase
        else:
            raise exceptions.ParamsError(
                f"Invalid teststep referenced testcase: {testcase}"
            )

        return StepRefCase(self.__step)
