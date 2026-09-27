"""批次 7（0919-25）：M6 / M7 / M2 / L15 的护栏。

四条都属于「配置 / 环境 / 参数流」，且都会让用户看到**错误归因**或**错环境/错文件**：

| # | 修复前 | 现在 |
| --- | --- | --- |
| M6 | 请求**构造期**错误（header 值类型不对）被吞成 `status_code: 0` 的假响应 → 与"连不上"同形，无断言的 step 还会判成功 | 直接抛 `ParamsError`，附字段/类型/改法；`status_code: 0` 只留给**真的**网络失败 |
| M7 | 多项目调用时，命中 `project_meta_cache` 的快路径**不重新应用该项目 `.env`** → 换回 A 项目时环境里还是 B 的 | 快路径也重新应用 `.env` |
| M2 | `run case.yml -c pytest.ini` 里**选项的值**是已存在的路径 → 被当成用例路径摘走（pytest exit 4、一个用例都不跑） | 按"上一个 token 是不是取值的 pytest 选项"判定 |
| L15 | `_setup_runner` 的"每 runner 一份函数表拷贝"在**调用方传入共享表**时不成立 → 绑定被顶掉，A 用例按 B 项目的根解析上传路径 | 无条件拷贝一份 |
"""

import os
import shutil
import unittest
import uuid
from unittest import mock

from interfacetester import Config, RunRequest, Step, exceptions, loader
from interfacetester.cli import main_run
from interfacetester.client import HttpSession
from interfacetester.parser import Parser
from interfacetester.runner import SessionRunner
from interfacetester.utils import HTTP_BIN_URL


def _tmp_dir(prefix: str) -> str:
    path = os.path.join(os.getcwd(), "logs", f"{prefix}_{uuid.uuid4().hex[:8]}")
    os.makedirs(path, exist_ok=True)
    return path


class TestBatch0919_25ConstructionErrorIsNotAFakeResponse(unittest.TestCase):
    """M6：构造期错误与网络错误必须分开（前者直接抛，后者才是 status_code=0）。"""

    def test_header_type_error_raises_instead_of_faking_a_response(self):
        session = HttpSession()

        with self.assertRaises(exceptions.ParamsError) as ctx:
            session.request("get", f"{HTTP_BIN_URL}/get", headers={"X-Count": 123})

        message = str(ctx.exception)
        self.assertIn("构造阶段", message)
        self.assertIn("InvalidHeader", message, "必须保留原始异常的类型与内容")
        self.assertIn("一次都没发出去", message, "必须说清'请求没发出去'，否则会被当成连不上")

    def test_network_failure_still_returns_status_code_zero(self):
        """反向护栏：**真的**连不上时行为不变（仍是 status_code=0 的兜底响应）。

        NOTICE: 这条很重要——M6 只把"构造期"错误摘出来，不能顺手把网络错误也改成抛，
        否则既有的一大批"连不上→status_code 0→断言失败"的用例会变成 error。
        """
        session = HttpSession()
        # 取一个刚释放的本机端口，几乎不可能有人在听
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()

        response = session.request("get", f"http://127.0.0.1:{dead_port}/x")

        self.assertEqual(response.status_code, 0)


class TestBatch0919_25CrossProjectEnvIsReapplied(unittest.TestCase):
    """M7：换回先前加载过的项目时，必须把**那个项目**的 `.env` 重新应用一遍。"""

    def setUp(self):
        self._env_backup = dict(os.environ)
        self.addCleanup(self._restore_env)
        loader.reset_project_meta()
        self.addCleanup(loader.reset_project_meta)
        self.root = _tmp_dir("tmp_b7_multi")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.projects = {}
        for name in ("projA", "projB"):
            project = os.path.join(self.root, name)
            os.makedirs(project, exist_ok=True)
            with open(os.path.join(project, "debugtalk.py"), "w", encoding="utf-8") as f:
                f.write("")
            with open(os.path.join(project, ".env"), "w", encoding="utf-8") as f:
                f.write(f"SHARED_ENV_KEY={name}\n")
            with open(os.path.join(project, "case.yml"), "w", encoding="utf-8") as f:
                f.write("config:\n    name: x\nteststeps: []\n")
            self.projects[name] = project

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _load(self, name, **kwargs):
        return loader.load_project_meta(
            os.path.join(self.projects[name], "case.yml"), **kwargs
        )

    def test_switching_back_reapplies_that_projects_env(self):
        self._load("projA", reload=True)
        self.assertEqual(os.environ.get("SHARED_ENV_KEY"), "projA")

        self._load("projB", reload=True)
        self.assertEqual(os.environ.get("SHARED_ENV_KEY"), "projB")

        # 换回 A：这次会命中 `project_meta_cache` 的快路径 —— 修复前直接 return，
        # 环境里留着的仍是 **B** 的值。
        self._load("projA")

        self.assertEqual(
            os.environ.get("SHARED_ENV_KEY"),
            "projA",
            "换回 A 项目后环境里还是 B 项目的 `.env` —— A 的用例会静默跑在错环境上（M7）",
        )

    def test_meta_env_matches_the_reapplied_file(self):
        self._load("projA", reload=True)
        meta_b = self._load("projB", reload=True)
        self.assertEqual(meta_b.env.get("SHARED_ENV_KEY"), "projB")

        meta_a = self._load("projA")

        self.assertEqual(meta_a.env.get("SHARED_ENV_KEY"), "projA")


class TestBatch0919_25PytestOptionValueIsNotACasePath(unittest.TestCase):
    """M2：`-c pytest.ini` 的值不能被当成用例路径摘走。"""

    def setUp(self):
        self.tmp_dir = _tmp_dir("tmp_b7_m2")
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.yml = os.path.join(self.tmp_dir, "case.yml")
        with open(self.yml, "w", encoding="utf-8") as f:
            # NOTICE（0920 批次 5 / N33）：**不能再用 `teststeps: []` 当脚手架**。
            # 空 teststeps 生成的用例是"零步骤但判成功"（绿了但什么都没测），
            # 现在被 `ensure_generatable_teststeps` 拦成报错。
            # 本用例测的是 CLI 参数处理，给一个真实步骤即可（也更贴近真实用法）。
            f.write(
                "config:\n    name: m2\n"
                "teststeps:\n"
                "- name: probe\n"
                "  request:\n"
                "    method: GET\n"
                "    url: /status/200\n"
            )
        self.ini = os.path.join(self.tmp_dir, "pytest.ini")
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write("[pytest]\n")

    def _run(self, *args):
        with mock.patch("interfacetester.cli.pytest.main", return_value=0) as spy:
            code = main_run(list(args))
        return code, spy.call_args[0][0]

    def test_option_value_that_exists_is_passed_through_to_pytest(self):
        code, argv = self._run(self.yml, "-c", self.ini)

        self.assertEqual(code, 0)
        self.assertIn("-c", argv)
        self.assertEqual(
            argv[argv.index("-c") + 1],
            self.ini,
            "`-c` 的值被摘走了（修复前 pytest 会 exit 4、一个用例都不跑）",
        )
        self.assertEqual(
            argv.count(self.ini), 1, "选项的值被同时当成了用例路径"
        )

    def test_case_path_is_still_collected(self):
        """反向护栏：真正的用例路径仍要被收进 pytest 参数（生成物必须被跑）。"""
        _code, argv = self._run(self.yml)

        self.assertTrue(
            any(str(item).endswith("case_test.py") for item in argv),
            f"用例路径没有被收进去：{argv}",
        )


class TestBatch0919_25ConfigTimeoutAcceptsReferences(unittest.TestCase):
    """L16：`config.timeout` 允许写引用（加载期不再拒），且**数字样字符串仍照旧强转**。"""

    def test_reference_form_is_accepted_at_load_time(self):
        """`timeout: ${ENV(TIMEOUT)}` 必须能过**加载期**（修复前被 `float_parsing` 拒掉）。"""
        case = loader.load_testcase(
            {
                "config": {"name": "l16", "timeout": "${ENV(TIMEOUT)}"},
                "teststeps": [],
            }
        )

        self.assertEqual(case.config.timeout, "${ENV(TIMEOUT)}")

    def test_numeric_literal_is_still_coerced_to_float(self):
        """反向护栏：带引号的数字仍要变成 float —— 生成物是 `.timeout(30.0)` 而不是 `.timeout("30")`。"""
        from interfacetester.models import TConfig

        self.assertEqual(TConfig(name="x", timeout="30").timeout, 30.0)
        self.assertEqual(TConfig(name="x", timeout=30).timeout, 30.0)

    def test_illegal_literal_is_left_for_runtime_error(self):
        """非法字面量不在加载期炸，留给运行期的 `ensure_timeout_value` 给可读报错。"""
        from interfacetester.models import TConfig

        self.assertEqual(TConfig(name="x", timeout="abc").timeout, "abc")


class TestBatch0919_25FunctionTableIsAlwaysCopied(unittest.TestCase):
    """L15：调用方传入共享函数表时，`_setup_runner` 也必须拷一份再绑定。"""

    def test_shared_mapping_is_not_bound_in_place(self):
        shared = {"_shared_probe": lambda: 1}
        parser = Parser(shared)

        class _Probe(SessionRunner):
            config = Config("l15 probe").base_url(HTTP_BIN_URL)
            teststeps = [
                Step(
                    RunRequest("probe")
                    .get("/status/200")
                    .validate()
                    .assert_equal("status_code", 200)
                )
            ]

        _Probe.parser = parser
        try:
            runner = _Probe()
            runner.test_start()
            # NOTICE: 必须在 `finally` 把类属性还原**之前**取值 ——
            # `runner.parser` 解析到的是那个类属性（实例自己没有 parser 属性），
            # 第一版在 finally 之后才读，拿到的是 None。
            bound_mapping = runner.parser.functions_mapping
        finally:
            _Probe.parser = None

        self.assertIsNot(
            bound_mapping,
            shared,
            "共享函数表被就地绑定/改写 —— 下一个 runner 再绑一次就会顶掉它（L15）",
        )
        self.assertNotIn(
            "__interfacetester_root_bound__",
            str(shared),
            "绑定标记被写进了调用方传进来的那张表",
        )
        self.assertIn("_shared_probe", bound_mapping)
