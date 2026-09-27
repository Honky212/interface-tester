"""0919-2 / M34：`testcase:` 引用**成环**必须在生成期拦下，而不是无限递归。

## 修复前的现场

`a.yml` 引用 `b.yml`、`b.yml` 又引用 `a.yml`：

```text
RecursionError: maximum recursion depth exceeded
```

**根因**：`make_testcase` 在处理 `testcase:` 引用时递归调用自身，而用来防重入的
`pytest_files_made_cache_mapping` 要到**文件写盘之后**才登记。于是环上的文件永远命不中缓存，
每一轮都重新加载、重新递归，直到撞上 Python 的递归上限。
报错（`RecursionError`）与根因（引用成环）完全对不上，现场还会打印上千行交错日志
（`start to make testcase: a.yml` / `b.yml` 交替刷屏）。

## 修法：把「当前调用链」当参数沿递归往下传

不用模块级全局变量记录「正在生成」集合，而是给 `make_testcase` 加一个内部参数
`_chain`，每次递归时把当前路径追加进去传下去。这样：

- **异常安全是天然的**：链随调用栈增长，异常展开时自动消失，不需要 `try/finally` 清理；
- 因此**不存在**「失败一次就残留脏状态、下次把无关失败误报成循环引用」那类问题
  （这正是「用哨兵值占位缓存」那种做法会踩的坑——见下面的
  `test_failed_generation_does_not_poison_later_attempts`）。

## 本文件同时钉住「不能误报」

环检测很容易写成「见过就报错」，那样合法的**菱形引用**（A→B、A→C、B→D、C→D，
D 被访问两次但不在环上）和线性链都会被误伤。所以正向用例和负向用例一样重要。
"""

import os
import shutil
import unittest
import uuid

import yaml

from interfacetester import exceptions, loader
from interfacetester.make import make_testcase

_DEBUGTALK = "def get_base_url():\n    return 'http://127.0.0.1:1'\n"

_OWN_STEP = {
    "name": "own step",
    "request": {"url": "/get", "method": "GET"},
    "validate": [{"eq": ["status_code", 200]}],
}


def _tmp_project_dir() -> str:
    """临时项目目录放 logs/ 下（已被 .gitignore 覆盖，也不会被 pytest 收集）。"""
    path = os.path.join(os.getcwd(), "logs", f"tmp_cycle_{uuid.uuid4().hex[:8]}")
    os.makedirs(path)
    return path


class _CycleCaseMixin:
    """建一个临时项目：写 debugtalk.py + 若干用例 YAML。"""

    def _make_project(self, cases: dict) -> str:
        work_dir = _tmp_project_dir()
        self.addCleanup(shutil.rmtree, work_dir, ignore_errors=True)

        with open(os.path.join(work_dir, "debugtalk.py"), "w", encoding="utf-8") as f:
            f.write(_DEBUGTALK)

        for rel_path, content in cases.items():
            abs_path = os.path.join(work_dir, rel_path.replace("/", os.sep))
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(content, f, allow_unicode=True, sort_keys=False)
        return work_dir

    def _make(self, work_dir: str, rel_path: str):
        """按 `hmake` 的方式生成指定用例（返回生成物路径）。"""
        abs_path = os.path.join(work_dir, rel_path.replace("/", os.sep))
        content = loader.load_test_file(abs_path)
        content.setdefault("config", {})["path"] = abs_path
        return make_testcase(content)


def _case(name: str, refs) -> dict:
    """一个 config + 若干 `testcase:` 引用步骤的用例。"""
    steps = [{"name": f"call {ref}", "testcase": ref} for ref in refs]
    steps.append(dict(_OWN_STEP))
    return {
        "config": {"name": name, "base_url": "${get_base_url()}"},
        "teststeps": steps,
    }


class TestReferenceCycleIsRejected(_CycleCaseMixin, unittest.TestCase):
    """成环必须报「循环引用」，而不是 `RecursionError`。"""

    def test_two_case_cycle_is_rejected(self):
        work_dir = self._make_project(
            {"a.yml": _case("a", ["b.yml"]), "b.yml": _case("b", ["a.yml"])}
        )

        with self.assertRaises(exceptions.TestCaseFormatError) as ctx:
            self._make(work_dir, "a.yml")

        message = str(ctx.exception)
        self.assertIn("循环引用", message)
        # 报错要指出环本身，否则用户不知道该断开谁
        self.assertIn("a.yml", message)
        self.assertIn("b.yml", message)

    def test_three_case_cycle_is_rejected(self):
        work_dir = self._make_project(
            {
                "a.yml": _case("a", ["b.yml"]),
                "b.yml": _case("b", ["c.yml"]),
                "c.yml": _case("c", ["a.yml"]),
            }
        )

        with self.assertRaises(exceptions.TestCaseFormatError) as ctx:
            self._make(work_dir, "a.yml")

        self.assertIn("循环引用", str(ctx.exception))

    def test_self_reference_is_rejected(self):
        """`a.yml` 引用自己——最短的环。"""
        work_dir = self._make_project({"self.yml": _case("self", ["self.yml"])})

        with self.assertRaises(exceptions.TestCaseFormatError) as ctx:
            self._make(work_dir, "self.yml")

        self.assertIn("循环引用", str(ctx.exception))

    def test_cycle_is_not_reported_as_recursion_error(self):
        """把「修复前的失败形态」钉死：不能再是 RecursionError。"""
        work_dir = self._make_project(
            {"a.yml": _case("a", ["b.yml"]), "b.yml": _case("b", ["a.yml"])}
        )

        try:
            self._make(work_dir, "a.yml")
        except RecursionError:
            self.fail(
                "仍然抛 RecursionError——环检测没有生效（报错与根因对不上，正是 M34 要修的）"
            )
        except exceptions.TestCaseFormatError:
            pass

    def test_entering_the_cycle_from_either_end_reports_it(self):
        """从环上任一点进入都必须报出来（不是只在某一个入口生效）。"""
        work_dir = self._make_project(
            {"a.yml": _case("a", ["b.yml"]), "b.yml": _case("b", ["a.yml"])}
        )

        for entry in ("a.yml", "b.yml"):
            with self.subTest(entry=entry):
                with self.assertRaises(exceptions.TestCaseFormatError):
                    self._make(work_dir, entry)


class TestValidReferenceGraphsAreNotRejected(_CycleCaseMixin, unittest.TestCase):
    """反向护栏：合法引用图**不能**被误报成环。

    环检测最容易写成「见过就报错」，那样下面这些正常用法会全被误伤。
    """

    def test_linear_chain_generates_all_levels(self):
        """A → B → C 线性链：三层都要生成出来。"""
        work_dir = self._make_project(
            {
                "a.yml": _case("a", ["b.yml"]),
                "b.yml": _case("b", ["c.yml"]),
                "c.yml": _case("c", []),
            }
        )

        generated = self._make(work_dir, "a.yml")

        for stem in ("a_test.py", "b_test.py", "c_test.py"):
            with self.subTest(stem=stem):
                self.assertTrue(
                    os.path.exists(os.path.join(work_dir, stem)),
                    f"{stem} 没有被生成：{sorted(os.listdir(work_dir))}",
                )
        self.assertEqual(
            os.path.basename(generated), "a_test.py"
        )

    def test_diamond_reference_generates_once_and_is_not_a_cycle(self):
        """菱形图 A→B、A→C、B→D、C→D：D 被访问两次但**不在环上**，必须放行。

        这是「用全局 visited 集合」那种实现方式的头号假报场景。
        """
        work_dir = self._make_project(
            {
                "a.yml": _case("a", ["b.yml", "c.yml"]),
                "b.yml": _case("b", ["d.yml"]),
                "c.yml": _case("c", ["d.yml"]),
                "d.yml": _case("d", []),
            }
        )

        self._make(work_dir, "a.yml")

        for stem in ("a_test.py", "b_test.py", "c_test.py", "d_test.py"):
            with self.subTest(stem=stem):
                self.assertTrue(
                    os.path.exists(os.path.join(work_dir, stem)),
                    f"{stem} 没有被生成：{sorted(os.listdir(work_dir))}",
                )

    def test_same_reference_twice_in_one_case_is_allowed(self):
        """同一个用例里**重复引用同一个**被引用用例（不是环）。"""
        work_dir = self._make_project(
            {
                "a.yml": _case("a", ["b.yml", "b.yml"]),
                "b.yml": _case("b", []),
            }
        )

        self._make(work_dir, "a.yml")
        self.assertTrue(os.path.exists(os.path.join(work_dir, "b_test.py")))


class TestCycleGuardLeavesNoStaleState(_CycleCaseMixin, unittest.TestCase):
    """异常安全：环检测失败之后，进程状态必须干净。

    这是「用哨兵值占位缓存」那类实现方式的典型回归：失败时哨兵没清掉，
    于是**同一个用例下次尝试**（甚至另一个无关用例）会被误报成循环引用。
    本实现把链当参数传递，天生没有这个问题——这两条用例就是把它钉住。
    """

    def test_failed_generation_does_not_poison_later_attempts(self):
        work_dir = self._make_project(
            {"a.yml": _case("a", ["b.yml"]), "b.yml": _case("b", ["a.yml"])}
        )

        first = None
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with self.assertRaises(exceptions.TestCaseFormatError) as ctx:
                    self._make(work_dir, "a.yml")
                if first is None:
                    first = str(ctx.exception)
                else:
                    # 两次报错应完全一致；若第二次冒出别的错（例如误报），这里会红
                    self.assertEqual(str(ctx.exception), first)

    def test_an_unrelated_generation_still_succeeds_after_a_cycle_failure(self):
        """环检测失败之后，另一个**正常**用例必须照样能生成。"""
        work_dir = self._make_project(
            {
                "a.yml": _case("a", ["b.yml"]),
                "b.yml": _case("b", ["a.yml"]),
                "clean.yml": _case("clean", []),
            }
        )

        with self.assertRaises(exceptions.TestCaseFormatError):
            self._make(work_dir, "a.yml")

        # 同一个进程、紧接着跑：不能被上一次的失败污染
        self._make(work_dir, "clean.yml")
        self.assertTrue(
            os.path.exists(os.path.join(work_dir, "clean_test.py")),
            f"clean_test.py 没有被生成：{sorted(os.listdir(work_dir))}",
        )

    def test_a_failure_for_an_unrelated_reason_is_not_reported_as_a_cycle(self):
        """无关原因失败（算子名拼错）不能被误报成循环引用。

        生成失败后再次生成同一个用例时，若实现里残留了「正在生成」的标记，
        第二次就会报出**错误的**「循环引用」。这里用一个不含任何引用、
        只是算子名拼错的用例来验证。
        """
        bad = _case("bad", [])
        bad["teststeps"][-1]["validate"] = [
            {"definitely_not_a_comparator": ["status_code", 200]}
        ]
        work_dir = self._make_project({"bad.yml": bad})

        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with self.assertRaises(Exception) as ctx:
                    self._make(work_dir, "bad.yml")
                self.assertNotIn(
                    "循环引用",
                    str(ctx.exception),
                    "无关原因的生成失败被误报成循环引用——说明残留了「正在生成」的脏状态",
                )
