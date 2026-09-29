# -*- coding: utf-8 -*-
r"""批次 9-1 收尾：**生成完之后必须作废 import 缓存**，否则 `--save-tests` 会间歇性整轮白跑。

## 现象（实测）

`hrun <用例> --save-tests` 有时会这样失败 —— 而且**没有 summary.json**：

```text
ImportError while loading conftest '<项目>/conftest.py'.
ModuleNotFoundError: No module named '<项目目录名>.conftest'
（pytest exit 4：一个用例都没跑）
```

复现率（`.tmp_report/probe_conftest_loop.py`，每次都是新进程）：4/25、1/30、1/40、2/40，
即**单次 CLI 调用 3%~8%**；外层全量测试因此每轮红 1~3 条不等（看起来像"随机"）。

## 根因（每一环都有取证）

```text
① compat._generate_conftest_for_summary 第 558 行先 load_project_meta()
     → 项目根进 sys.path + import debugtalk
     → CPython 为「项目目录」建 FileFinder 并**缓存当时的目录列表**
        （当时只有：__pycache__ / case.yml / debugtalk.py）
② 之后才写 conftest.py；main_make 再写 case_test.py 与 __init__.py
③ FileFinder.find_spec 只在**目录 mtime 变了**时才重新列目录：
        if mtime != self._path_mtime: self._fill_cache()
   而那一刻两者相等 → 沿用 ① 的旧列表 → conftest 在缓存里"不存在"
④ 因为 __init__.py 存在，conftest 必须按包限定名 `<dir>.conftest` 导入
     → 走的正是这条带缓存的查找路径 → 报「子模块不存在」
```

失败现场的铁证（同一个进程里打印）：

```text
finder._path_cache = ['__pycache__', 'case.yml', 'debugtalk.py']   # 缺 conftest.py 等 3 个文件
os.listdir(项目目录) = ['__init__.py','__pycache__','case.yml','case_test.py','conftest.py','debugtalk.py']
finder._path_mtime == os.stat(目录).st_mtime                        # 相等 → 缓存被认为新鲜
importlib.invalidate_caches() 之后，**同一个 import 立刻成功**
```

顺带确认的 Python 语义（`.tmp_report/probe_mnfe_semantics2.py`）：父包**不存在**时报文是
`'<dir>'`；只有「父包找到了、但里面的子模块找不到」才报 `'<dir>.conftest'` —— 所以这个
错误从一开始就说明父包没问题、是 conftest 被缓存挡住了。

## 本文件怎么把「随机复现」变成「确定性回归」

真实触发依赖目录 mtime 有没有及时更新（时序），没法在单测里等运气。所以这里
**直接伪造那个时序**：把 `importlib._bootstrap_external._path_stat` 打桩成「项目目录的 mtime
永远是缓存里记的那个值」，于是 `FileFinder` 永远不会重新列目录 —— 旧列表 100% 被沿用。

断言的是**后果**而不是实现细节：pytest 启动时（用 mock 替身模拟 pytest 对 conftest 的
导入）必须能导入到 conftest。没有修复时这条**必然红**（见提交信息里的注入验证）。
"""

import importlib
import importlib._bootstrap_external as bootstrap_external
import importlib.machinery
import os
import shutil
import sys
import unittest
from unittest import mock

from interfacetester import cli, loader, make

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _write_project(project_dir: str) -> None:
    """建一个最小项目：只需 `debugtalk.py`（项目标记）+ 一个用例。

    NOTICE: **不**预先写 `conftest.py` / `__init__.py` —— 它们由被执行的 CLI 生成，
    这正是复现所必需的前提（缓存在它们出现之前就被填了）。
    """
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "debugtalk.py"), "w", encoding="utf-8") as fp:
        fp.write("")
    with open(os.path.join(project_dir, "case.yml"), "w", encoding="utf-8") as fp:
        fp.write(
            "config:\n"
            "    name: import cache probe\n"
            "    base_url: http://127.0.0.1:80\n"
            "teststeps:\n"
            "    - name: one\n"
            "      request: {method: GET, url: /get}\n"
            "      validate:\n"
            "          - eq: [status_code, 200]\n"
        )


class TestGeneratedModulesAreVisibleToPytest(unittest.TestCase):
    """核心不变量：**生成物落盘后、pytest 启动前，import 缓存必须被作废**。"""

    def setUp(self):
        self.tmp_dir = os.path.join(BASE, "logs", f"tmp_b91cache_{os.getpid()}")
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        # 项目目录名必须是合法标识符（它同时是包名）
        self.project_dir = os.path.join(self.tmp_dir, "b91cacheproj")
        _write_project(self.project_dir)
        self.pkg_name = os.path.basename(self.project_dir)

        loader.project_meta = None
        make.pytest_files_made_cache_mapping.clear()
        make.pytest_files_run_set.clear()
        self._cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._cwd)
        loader.project_meta = None
        make.pytest_files_made_cache_mapping.clear()
        make.pytest_files_run_set.clear()
        sys.path[:] = [p for p in sys.path if p != self.project_dir]
        sys.path[:] = [p for p in sys.path if p != os.path.dirname(self.project_dir)]
        sys.path_importer_cache.pop(self.project_dir, None)
        sys.modules.pop(self.pkg_name, None)
        sys.modules.pop(f"{self.pkg_name}.conftest", None)
        importlib.invalidate_caches()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ 工具
    def _fill_stale_directory_cache(self):
        """填一次「项目目录」的 FileFinder 缓存 —— 与 `_generate_conftest_for_summary` 同因。

        做法就是真实路径：项目根进 `sys.path`，再 import 那个目录里的 `debugtalk`
        （框架在写 conftest.py 之前干的就是这件事），此时目录里还没有 conftest.py。
        返回被填的 finder。

        NOTICE: 两个必要的保险 ——
          - 先 `sys.modules.pop("debugtalk")`：本进程里别的用例可能已经 import 过
            **另一个项目**的 `debugtalk`，命中 `sys.modules` 就不会去扫目录，缓存也就填不上；
          - 再故意 import 一个不存在的顶层模块：强制 PathFinder 把 `sys.path` 每一项都过一遍，
            项目目录的 FileFinder 因此**必定**被创建并填入当前列表。
        """
        importlib.invalidate_caches()
        if self.project_dir not in sys.path:
            sys.path.insert(0, self.project_dir)

        sys.modules.pop("debugtalk", None)
        for name in ("debugtalk", "b91_cache_scan_probe_missing_module"):
            try:
                importlib.import_module(name)
            except ModuleNotFoundError:
                pass

        finder = sys.path_importer_cache.get(self.project_dir)
        self.assertIsNotNone(
            finder,
            "没能为项目目录建立 FileFinder 缓存 —— 用例前提失效（无法伪造那次过期缓存）",
        )
        cached = sorted(finder._path_cache)
        self.assertNotIn(
            "conftest.py",
            cached,
            f"前提不成立：填缓存时 conftest.py 就存在了，本用例失去意义（{cached}）",
        )
        return finder

    def _poison_mtime_so_cache_is_never_refreshed(self, finder):
        """把目录 mtime 的比较打桩成「永远不变」→ `FileFinder` 永不重新列目录。

        这是把「时序运气」换成「确定性」的关键：真实触发靠 mtime 恰好在同一刻，
        这里直接让 `_path_stat` 永远返回缓存里记的那个 mtime。
        """
        frozen_mtime = finder._path_mtime
        real_path_stat = bootstrap_external._path_stat

        def fake_path_stat(path):
            try:
                same_dir = os.path.abspath(os.fspath(path)) == self.project_dir
            except TypeError:  # pragma: no cover - 非常规 path 类型
                same_dir = False
            stat_result = real_path_stat(path)
            if not same_dir:
                return stat_result
            # 只改 mtime 字段，其余保持真实值
            values = list(stat_result)
            values[8] = frozen_mtime  # st_mtime
            return os.stat_result(values)

        return mock.patch.object(
            bootstrap_external, "_path_stat", side_effect=fake_path_stat
        )

    def _run_cli_with_pytest_spy(self):
        """在 cwd=项目目录 下跑真 `cli.main_run --save-tests`，并把 `pytest.main` 换成替身。

        替身做的事就是 pytest 启动时对 conftest 做的事（`_pytest/pathlib.py:import_path`
        的 prepend 分支）：① 把**包根**（项目目录的父目录）插到 `sys.path[0]`；
        ② 按**包限定名**导入 conftest（`__init__.py` 已由生成器写好，所以名字是
        `<项目目录名>.conftest`）。导入失败会被记进返回值，交给用例断言。
        """
        observed = {}
        pkg_root = os.path.dirname(self.project_dir)

        def pytest_spy(args):
            observed["args"] = list(args)
            if str(pkg_root) != sys.path[0]:
                sys.path.insert(0, str(pkg_root))
            try:
                mod = importlib.import_module(f"{self.pkg_name}.conftest")
            except BaseException as ex:  # noqa: BLE001 - 失败即本用例要抓的现象
                observed["import_error"] = f"{type(ex).__name__}: {ex}"
            else:
                observed["import_error"] = None
                observed["conftest_module"] = mod.__name__
            return 0

        os.chdir(self.project_dir)
        sys.argv = ["interfacetester", "run", "case.yml", "--save-tests"]
        with mock.patch.object(cli.pytest, "main", side_effect=pytest_spy):
            cli.main_run(["--save-tests", "case.yml"])
        os.chdir(self._cwd)
        return observed

    # ------------------------------------------------------------------ 用例
    def test_pytest_can_import_the_generated_conftest_after_a_stale_cache(self):
        r"""**核心回归**：缓存在 conftest.py 出现之前就被填、且永不失效时，pytest 仍须能导入它。

        没有 `cli.main_run` 里那次 `importlib.invalidate_caches()` 时，这条**必然红**：

        ```text
        ModuleNotFoundError: No module named 'b91cacheproj.conftest'
        ```

        与线上那条间歇性失败的报错**逐字相同**（只是目录名不同）。
        """
        finder = self._fill_stale_directory_cache()
        stale_names = sorted(finder._path_cache)

        with self._poison_mtime_so_cache_is_never_refreshed(finder):
            observed = self._run_cli_with_pytest_spy()

        # ① pytest 确实被调用了（否则下面的断言会变成"什么都没发生"式的假通过）
        self.assertIn("args", observed, "pytest.main 没有被调用，用例前提失效")
        # ② 生成物都真的落盘了
        for name in ("conftest.py", "case_test.py", "__init__.py"):
            with self.subTest(generated=name):
                self.assertTrue(
                    os.path.isfile(os.path.join(self.project_dir, name)),
                    f"生成物 {name} 不存在 —— 前提失效，本用例不能证明任何事",
                )
        # ③ 关键断言：pytest 启动时能按包限定名导入到 conftest
        self.assertIsNone(
            observed.get("import_error"),
            "pytest 启动时导不到生成的 conftest —— 正是那条间歇性假失败的形状：\n"
            f"  导入失败: {observed.get('import_error')}\n"
            f"  填缓存时的目录列表: {stale_names}\n"
            f"  现在的目录内容: {sorted(os.listdir(self.project_dir))}\n"
            "  修法：生成物落盘后、pytest 启动前必须 `importlib.invalidate_caches()`"
            "（见 cli.main_run 里的 NOTICE）。",
        )
        self.assertEqual(observed.get("conftest_module"), f"{self.pkg_name}.conftest")

    def test_main_run_invalidates_import_caches_before_starting_pytest(self):
        """契约层护栏：作废缓存必须发生在 `pytest.main` **之前**（顺序不能反）。"""
        calls = []

        def fake_invalidate():
            calls.append("invalidate_caches")

        def fake_pytest_main(args):
            calls.append("pytest.main")
            return 0

        os.chdir(self.project_dir)
        sys.argv = ["interfacetester", "run", "case.yml", "--save-tests"]
        with mock.patch.object(
            cli.importlib, "invalidate_caches", side_effect=fake_invalidate
        ), mock.patch.object(cli.pytest, "main", side_effect=fake_pytest_main):
            cli.main_run(["--save-tests", "case.yml"])
        os.chdir(self._cwd)

        self.assertIn("pytest.main", calls, "pytest.main 没有被调用，用例前提失效")
        self.assertIn(
            "invalidate_caches",
            calls,
            "生成之后没有作废 import 缓存 —— `--save-tests` 会间歇性导不到生成的 conftest",
        )
        self.assertLess(
            calls.index("invalidate_caches"),
            calls.index("pytest.main"),
            "作废缓存必须发生在 pytest 启动**之前**（之后再作废已经晚了）",
        )
