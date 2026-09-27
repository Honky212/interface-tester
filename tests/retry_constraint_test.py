"""0919-1 / L33：`retry_times` / `retry_interval` 必须拒绝负值。

## 修复前实测（两条路径的报错都与根因无关）

```
retry_times: -1   ->  UnboundLocalError: cannot access local variable 'step_result'
                      where it is not associated with a value      (runner.py:440)
retry_interval: -5 ->  ValueError: sleep length must be non-negative (runner.py:434)
```

根因：`range(step.retry_times + 1)` 在负数下是**空循环**（`runner.py:419`），循环体
一次都不执行，于是 `step_result` 从未绑定，一直拖到
`self.__session_variables.update(step_result.export_vars)` 才炸——报错指向的是框架内部
变量名，跟「写了个负的重试次数」完全看不出关系。

## 两条路径都要拦住（这是本批最容易修漏的地方）

1. **YAML 路径**：`loader.load_testcase` 走 pydantic 校验 → 只加 `ge=0` 即可拦住；
2. **生成物路径**：`hmake` 生成 `.with_retry(retry_times=-1, ...)`，而
   `RunRequest.__init__` 建的是 `TStep(...)` **实例**、`with_retry` 是**直接赋值**
   （`self.__step.retry_times = retry_times`）——pydantic v2 默认**不校验赋值**，
   所以只加 `ge=0` **拦不住它**（实测仍是 UnboundLocalError）。
   必须同时打开 `TStep.model_config` 的 `validate_assignment=True`（见 models.py 的 NOTICE）。
"""

import os
import shutil
import unittest
import uuid

import yaml
from pydantic import ValidationError

from interfacetester import RunRequest, Step, exceptions, loader, models
from interfacetester.models import TStep

# NOTICE: 不要写 `from interfacetester.models import TestCase`——那个名字以 `Test` 开头，
# 会被 pytest 当成待收集的测试类，于是每次跑测试都多出一条
# `PytestCollectionWarning: cannot collect test class 'TestCase' because it has a __init__ constructor`。
# 本仓的基线是 0 条 collection warning，所以这里改成走模块属性 `models.TestCase`，
# 让那个名字不进本模块的命名空间。


def _tmp_dir(prefix: str) -> str:
    """临时目录放 logs/ 下（已被 .gitignore 覆盖，也不会被 pytest 收集）。"""
    path = os.path.join(os.getcwd(), "logs", f"tmp_{prefix}_{uuid.uuid4().hex[:8]}")
    os.makedirs(path)
    return path


def _yaml_case(retry_times=0, retry_interval=0):
    return {
        "config": {"name": "retry constraint probe", "base_url": "http://127.0.0.1:1"},
        "teststeps": [
            {
                "name": "step",
                "retry_times": retry_times,
                "retry_interval": retry_interval,
                "request": {"url": "/get", "method": "GET"},
                "validate": [{"eq": ["status_code", 200]}],
            }
        ],
    }


class TestRetryConstraintAtModelLevel(unittest.TestCase):
    """约束本身：`TStep` 的 `ge=0`。"""

    def test_negative_retry_times_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            models.TestCase.model_validate(_yaml_case(retry_times=-1))

        message = str(ctx.exception)
        self.assertIn("retry_times", message)
        self.assertIn("greater_than_equal", message)

    def test_negative_retry_interval_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            models.TestCase.model_validate(
                _yaml_case(retry_times=1, retry_interval=-5)
            )

        message = str(ctx.exception)
        self.assertIn("retry_interval", message)
        self.assertIn("greater_than_equal", message)

    def test_zero_and_positive_values_still_accepted(self):
        """`retry_times` 不写、写 0、写正数都必须照旧可用（`ge=0` 不能把默认值弄坏）。"""
        for retry_times, retry_interval in ((0, 0), (3, 2), (1, 0)):
            with self.subTest(retry_times=retry_times, retry_interval=retry_interval):
                case_obj = models.TestCase.model_validate(
                    _yaml_case(retry_times=retry_times, retry_interval=retry_interval)
                )
                step = case_obj.teststeps[0]
                self.assertEqual(step.retry_times, retry_times)
                self.assertEqual(step.retry_interval, retry_interval)


class TestRetryConstraintOnYamlPath(unittest.TestCase):
    """YAML 路径：`loader.load_testcase`（它会把 ValidationError 包成 TestCaseFormatError）。"""

    def test_negative_retry_times_reports_root_cause(self):
        with self.assertRaises(exceptions.TestCaseFormatError) as ctx:
            loader.load_testcase(_yaml_case(retry_times=-1))

        message = str(ctx.exception)
        self.assertIn("retry_times", message)
        self.assertIn("greater than or equal to 0", message)
        # 关键回归点：报错不能再指向框架内部变量
        self.assertNotIn("step_result", message)
        self.assertNotIn("UnboundLocalError", message)

    def test_negative_retry_interval_reports_root_cause(self):
        with self.assertRaises(exceptions.TestCaseFormatError) as ctx:
            loader.load_testcase(_yaml_case(retry_times=1, retry_interval=-5))

        self.assertIn("retry_interval", str(ctx.exception))


class TestRetryConstraintOnGeneratedCodePath(unittest.TestCase):
    """生成物路径：`hmake` 生成的 `.with_retry(...)`（builder **直接赋值**）。"""

    def test_negative_retry_times_is_rejected_at_the_assignment_point(self):
        with self.assertRaises(ValidationError) as ctx:
            RunRequest("step").with_retry(retry_times=-1, retry_interval=0)

        self.assertIn("greater_than_equal", str(ctx.exception))

    def test_negative_retry_interval_is_rejected_at_the_assignment_point(self):
        with self.assertRaises(ValidationError) as ctx:
            RunRequest("step").with_retry(retry_times=1, retry_interval=-5)

        self.assertIn("greater_than_equal", str(ctx.exception))

    def test_step_builder_still_reads_back_values(self):
        """正向回归：`Step(...).retry_times` 必须仍是写进去的值（validate_assignment 不改语义）。"""
        step = Step(
            RunRequest("step").with_retry(retry_times=3, retry_interval=2).get("/get")
        )
        self.assertEqual(step.retry_times, 3)
        self.assertEqual(step.retry_interval, 2)

    def test_validate_assignment_is_actually_enabled(self):
        """把「为什么只加 `ge=0` 不够」钉成一条不变量。

        以后若有人为了性能关掉 `validate_assignment`，上面那条 builder 用例会**静默失效**
        （又变回 UnboundLocalError），而这条会先红，避免「看起来有约束、实际只有一半生效」。
        """
        self.assertTrue(
            TStep.model_config.get("validate_assignment"),
            "TStep 必须开启 validate_assignment，否则 ge=0 对生成物路径无效",
        )
        # 直接赋值也必须被校验——这才是生成物路径的真实形态
        step = TStep(name="step")
        with self.assertRaises(ValidationError):
            step.retry_times = -1


class TestNegativeRetryFailsAtGenerationTime(unittest.TestCase):
    """端到端：负值必须在**生成期**报出根因，而不是拖到运行期炸框架内部变量。

    这正是本项目「生成期闸门」的一贯取向：宁可 `hmake` 报错，也不要产出一个
    运行期才炸、且报错与根因无关的用例。
    """

    def test_make_testcase_reports_root_cause(self):
        from interfacetester.make import make_testcase

        work_dir = _tmp_dir("retryneg")
        self.addCleanup(shutil.rmtree, work_dir, ignore_errors=True)
        with open(os.path.join(work_dir, "debugtalk.py"), "w", encoding="utf-8") as f:
            f.write("def dummy():\n    return 1\n")

        yml_path = os.path.join(work_dir, "neg.yml")
        case = _yaml_case(retry_times=-1)
        case["config"]["path"] = yml_path
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(case, f, allow_unicode=True)

        with self.assertRaises(exceptions.TestCaseFormatError) as ctx:
            make_testcase(dict(case))

        message = str(ctx.exception)
        self.assertIn("retry_times", message)
        self.assertNotIn("step_result", message)
        self.assertNotIn("UnboundLocalError", message)
        # 不该产出任何生成物
        self.assertFalse(
            os.path.exists(os.path.join(work_dir, "neg_test.py")),
            "生成期已经报错，不应该留下生成物",
        )
