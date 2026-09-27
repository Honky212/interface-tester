import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
import xml.etree.ElementTree as ElementTree
from unittest import mock

import pytest
import yaml

from interfacetester import loader
from interfacetester.cli import main, main_run
from interfacetester.make import (
    main_make,
    pytest_files_made_cache_mapping,
    pytest_files_run_set,
)
from interfacetester.utils import HTTP_BIN_URL


def _make_smoke_dir() -> str:
    """建一个临时工作目录（放在 logs/ 下：已被 .gitignore 覆盖，也不会被 pytest 收集）。

    NOTICE: 不用 tempfile.mkdtemp —— 受限环境下它建出的目录可能不可写（见 0916-8 记录）。
    """
    work_dir = os.path.join(os.getcwd(), "logs", f"smoke_{uuid.uuid4().hex[:8]}")
    os.makedirs(work_dir)
    return work_dir


def _write_smoke_testcase(work_dir: str, name: str) -> str:
    """写一个只依赖本地 mock 的最小用例（不访问外网），返回 yml 路径。"""
    yml_path = os.path.join(work_dir, f"{name}.yml")
    testcase = {
        "config": {"name": f"{name} smoke", "base_url": HTTP_BIN_URL, "verify": False},
        "teststeps": [
            {
                "name": "get",
                "request": {"method": "GET", "url": "/get"},
                "validate": [{"eq": ["status_code", 200]}],
            }
        ],
    }
    with open(yml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(testcase, f, allow_unicode=True)
    return yml_path


def _run_pytest_in_subprocess(target: str, junit_path: str, extra_args=()):
    """用独立 pytest 进程跑目标（模拟 CI），返回 CompletedProcess。

    NOTICE（0918-9 顺手修）：这里**必须**显式 `encoding="utf-8"` —— 子进程是
    `hrun` 链路里的 pytest，stdout 按 0918-8 / L27 的约定是 UTF-8，而 `text=True`
    单独用会按 locale（本机 GBK）解码：子进程只要输出一个非 ASCII 字符（例如
    `.pytest_cache` 拒绝访问的中文告警），读线程就抛 `UnicodeDecodeError`
    —— pytest 9 会把它报成 `PytestUnhandledThreadExceptionWarning`，
    于是**整份回归带着一条 warning 变绿**（这正是 L28 文档里提醒消费方的那一条）。
    """
    command = [
        sys.executable,
        "-m",
        "pytest",
        target,
        "-q",
        f"--junitxml={junit_path}",
        *extra_args,
    ]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        cwd=os.getcwd(),
    )


def _parse_junit(junit_path: str) -> ElementTree.Element:
    """解析 JUnit 报告并返回第一个 `<testsuite>` 节点。

    NOTICE: pytest 产出的根节点是 `<testsuites>`（复数），用例只写了单个 suite 时
    也可能直接是 `<testsuite>`；两种都要兼容。
    """
    root = ElementTree.parse(junit_path).getroot()
    if root.tag == "testsuite":
        return root

    suites = root.findall("testsuite")
    if not suites:
        raise AssertionError(f"JUnit 报告里没有 testsuite 节点，根节点是 {root.tag!r}")
    return suites[0]


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestCli(unittest.TestCase):
    def setUp(self):
        self.captured_output = io.StringIO()
        sys.stdout = self.captured_output

    def tearDown(self):
        sys.stdout = sys.__stdout__  # Reset redirect.

    def test_show_version(self):
        sys.argv = ["hrun", "-V"]

        with self.assertRaises(SystemExit) as cm:
            main()

        self.assertEqual(cm.exception.code, 0)

        from interfacetester import __version__

        self.assertIn(__version__, self.captured_output.getvalue().strip())

    def test_show_help(self):
        sys.argv = ["hrun", "-h"]

        with self.assertRaises(SystemExit) as cm:
            main()

        self.assertEqual(cm.exception.code, 0)

        from interfacetester import __description__

        self.assertIn(__description__, self.captured_output.getvalue().strip())

    def test_debug_pytest(self):
        cwd = os.getcwd()
        try:
            os.chdir(os.path.join(cwd, "examples", "postman_echo"))
            exit_code = pytest.main(
                ["-s", "request_methods/request_with_testcase_reference_test.py"]
            )
            self.assertEqual(exit_code, 0)
        finally:
            os.chdir(cwd)

    def test_run_testcase_with_abnormal_path(self):
        loader.project_meta = None
        exit_code = main_run(["examples/data/a-b.c/2 3.yml"])
        self.assertEqual(exit_code, 0)
        self.assertTrue(os.path.exists("examples/data/a_b_c/__init__.py"))
        self.assertTrue(os.path.exists("examples/data/debugtalk.py"))
        self.assertTrue(os.path.exists("examples/data/a_b_c/T1_test.py"))
        self.assertTrue(os.path.exists("examples/data/a_b_c/T2_3_test.py"))


class TestJunitXmlReport(unittest.TestCase):
    """CI 集成路径：`hrun --junitxml=...` 要产出可解析、计数正确的 JUnit 报告。"""

    def setUp(self):
        # NOTICE: make 的两个模块级全局会跨用例累积（`pytest_files_run_set` 里还留着
        # 前一个用例生成的文件），不清掉的话本用例的 hrun 会把别的项目的用例一起跑掉。
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        self.work_dir = _make_smoke_dir()
        self.junit_path = os.path.join(self.work_dir, "junit.xml")

    def tearDown(self):
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_cli_run_with_junitxml(self):
        """走 CLI 的 main_run（hrun 的等价路径），与本仓库其它 cli 用例一致。"""
        yml_path = _write_smoke_testcase(self.work_dir, "junitxml_demo")

        exit_code = main_run([yml_path, f"--junitxml={self.junit_path}"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(os.path.isfile(self.junit_path))

        suite = _parse_junit(self.junit_path)
        self.assertEqual(suite.get("tests"), "1")
        self.assertEqual(suite.get("failures"), "0")
        self.assertEqual(suite.get("errors"), "0")
        self.assertEqual(len(suite.findall("testcase")), 1)
        self.assertIn("TestCaseJunitxmlDemo", _read_text(self.junit_path))

    def test_subprocess_run_produces_junit_xml(self):
        """独立进程跑生成用例（更接近 CI 的真实用法）。

        这个用例同时为下面的 xdist 并行冒烟验证了同一套「子进程 + junitxml」基础设施
        （xdist 用例只是多传一个 `-n 2`）。
        """
        yml_path = _write_smoke_testcase(self.work_dir, "subprocess_demo")
        generated_python_list = main_make([yml_path])
        self.assertEqual(len(generated_python_list), 1)

        completed = _run_pytest_in_subprocess(
            generated_python_list[0], self.junit_path
        )

        self.assertEqual(
            completed.returncode, 0, f"stderr: {completed.stderr[-2000:]}"
        )
        suite = _parse_junit(self.junit_path)
        self.assertEqual(suite.get("tests"), "1")
        self.assertEqual(suite.get("failures"), "0")


@unittest.skipUnless(
    importlib.util.find_spec("xdist") is not None,
    "未安装 pytest-xdist（可选依赖），跳过并行冒烟；装上后本用例自动生效",
)
class TestXdistParallelRun(unittest.TestCase):
    """并行执行冒烟：生成的用例能被 pytest-xdist 正常分发（`-n 2`）。

    NOTICE: 并行场景最容易踩的是共享状态（同一个用例日志文件、全局项目缓存等），
    因此这里用两个**不同**用例跑 `-n 2`，断言两个都通过、且 JUnit 计数正确。
    """

    def setUp(self):
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        self.work_dir = _make_smoke_dir()
        self.junit_path = os.path.join(self.work_dir, "junit.xml")

    def tearDown(self):
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_parallel_run_with_two_workers(self):
        yml_paths = [
            _write_smoke_testcase(self.work_dir, "parallel_one"),
            _write_smoke_testcase(self.work_dir, "parallel_two"),
        ]
        generated_python_list = main_make(yml_paths)
        self.assertEqual(len(generated_python_list), 2)

        completed = _run_pytest_in_subprocess(
            self.work_dir, self.junit_path, extra_args=("-n", "2")
        )

        self.assertEqual(
            completed.returncode, 0, f"stderr: {completed.stderr[-2000:]}"
        )
        suite = _parse_junit(self.junit_path)
        self.assertEqual(suite.get("tests"), "2")
        self.assertEqual(suite.get("failures"), "0")
        self.assertEqual(suite.get("errors"), "0")


class TestCustomComparatorViaCli(unittest.TestCase):
    """P0 端到端：自定义断言算子走完整链路 `YAML → hmake → pytest`。

    零章记录的实际故障是「make 成功、pytest 收集阶段整文件 AttributeError」，
    因此这里必须走 CLI 的 `main_run`（= `hrun`），而不是只测 `ResponseObject.validate`。
    """

    def setUp(self):
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        # NOTICE: 必须重置项目 meta。本用例的项目目录（logs/smoke_xxx）嵌在仓库内，
        # 若同进程里别的用例曾把仓库根当作 projectless 项目加载，load_project_meta 会命中
        # 「已有 meta 的 RootDir 覆盖当前路径」的快路径，从而**跳过**本目录的 debugtalk.py
        # （表现为自定义算子查不到）。CLI 每次都是新进程，不存在这个状态。
        loader.project_meta = None
        self.work_dir = _make_smoke_dir()

    def tearDown(self):
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _write_project(self) -> str:
        """在临时项目里写 debugtalk.py（两个自定义算子）+ 一条混用内置/自定义算子的用例。"""
        with open(
            os.path.join(self.work_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write(
                'def my_cmp(check_value, expect_value, message=""):\n'
                "    assert check_value == expect_value, message\n"
                "    return True\n"
                "\n"
                'def has_keys(check_value, expect_value, message=""):\n'
                "    missing = [key for key in expect_value if key not in check_value]\n"
                '    assert not missing, f"{message} missing keys: {missing}"\n'
                "    return True\n"
            )

        yml_path = os.path.join(self.work_dir, "custom_comparator.yml")
        testcase = {
            "config": {
                "name": "custom comparator e2e",
                "base_url": HTTP_BIN_URL,
                "verify": False,
            },
            "teststeps": [
                {
                    "name": "builtin and custom comparators in one step",
                    "request": {"method": "GET", "url": "/get"},
                    "validate": [
                        {"equal": ["status_code", 200]},
                        {"my_cmp": ["status_code", 200, "my_cmp should be dispatched"]},
                        {
                            "has_keys": [
                                "body",
                                ["args", "headers", "url"],
                                "has_keys should be dispatched",
                            ]
                        },
                    ],
                }
            ],
        }
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)
        return yml_path

    def test_hrun_with_custom_comparators(self):
        yml_path = self._write_project()

        # NOTICE: 这里必须显式带 `--import-mode=importlib`。
        # 本仓库生成的 `_test.py` 所在目录会被 make.py 自动加上 `__init__.py`（成为包），
        # 而 `main_run` 是在**同一个进程里再开一个 pytest 会话**：默认的 prepend 模式会持续
        # 往 sys.path 里插目录，多次会话叠加后会出现
        # `ModuleNotFoundError: No module named '<smoke_xxx>.case_test'`（实测同一脚本
        # 8 轮里失败 7 轮）。importlib 模式不碰 sys.path、失败时退化为按文件路径导入 →
        # 同一脚本 8 轮全绿。这与框架代码无关，是「同进程嵌套 pytest 会话」的已知不稳。
        exit_code = main_run([yml_path, "--import-mode=importlib"])

        # 修复前：make 成功，但 pytest 收集阶段 AttributeError → 非 0 退出码
        self.assertEqual(exit_code, 0)

        generated_path = os.path.join(self.work_dir, "custom_comparator_test.py")
        self.assertTrue(os.path.isfile(generated_path))
        with open(generated_path, encoding="utf-8") as f:
            generated_content = f.read()
        self.assertIn(".assert_my_cmp(", generated_content)
        self.assertIn(".assert_has_keys(", generated_content)

    def test_hrun_fails_fast_on_unknown_comparator(self):
        """拼错算子名：应在 hmake 阶段（写盘前）失败，不留下生成文件。"""
        yml_path = os.path.join(self.work_dir, "typo_comparator.yml")
        testcase = {
            "config": {"name": "typo comparator", "base_url": HTTP_BIN_URL},
            "teststeps": [
                {
                    "name": "typo",
                    "request": {"method": "GET", "url": "/get"},
                    "validate": [{"my_cmpp": ["status_code", 200]}],
                }
            ],
        }
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)

        with self.assertRaises(SystemExit) as ctx:
            main_run([yml_path])

        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(
            os.path.exists(os.path.join(self.work_dir, "typo_comparator_test.py"))
        )


class TestResponseMetaAssertionsViaCli(unittest.TestCase):
    """P1-a 端到端：响应元信息断言与「可读失败」要走完 `YAML → hmake → pytest`。

    NOTICE: 带 `--import-mode=importlib` 的原因见 `TestCustomComparatorViaCli`
    （同进程嵌套 pytest 会话 + 生成目录是包时，默认 prepend 模式不稳）。
    """

    def setUp(self):
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.work_dir = _make_smoke_dir()

    def tearDown(self):
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _write_case(self, name: str, validators) -> str:
        yml_path = os.path.join(self.work_dir, f"{name}.yml")
        testcase = {
            "config": {"name": name, "base_url": HTTP_BIN_URL, "verify": False},
            "teststeps": [
                {
                    "name": "response meta assertions",
                    "request": {"method": "GET", "url": "/get"},
                    "validate": validators,
                }
            ],
        }
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)
        return yml_path

    def test_hrun_with_response_meta_assertions(self):
        yml_path = self._write_case(
            "meta_assertions",
            [
                {"less_than": ["status_code", 300]},
                {"less_than": ["elapsed_ms", 60000]},
                {"greater_than": ["elapsed_ms", 0]},
                {"greater_than": ["response_size", 0]},
                {"string_equals": ["reason", "OK"]},
            ],
        )

        exit_code = main_run([yml_path, "--import-mode=importlib"])

        self.assertEqual(exit_code, 0)
        generated_path = os.path.join(self.work_dir, "meta_assertions_test.py")
        generated_content = _read_text(generated_path)
        self.assertIn('.assert_less_than("elapsed_ms", 60000)', generated_content)
        self.assertIn('.assert_greater_than("response_size", 0)', generated_content)

    def test_elapsed_type_error_is_a_readable_failure_not_an_error(self):
        yml_path = self._write_case("elapsed_type_error", [{"less_than": ["elapsed", 2]}])
        junit_path = os.path.join(self.work_dir, "junit.xml")

        exit_code = main_run(
            [yml_path, f"--junitxml={junit_path}", "--import-mode=importlib"]
        )

        self.assertNotEqual(exit_code, 0)
        suite = _parse_junit(junit_path)
        # 关键：修复前这里是 error（TypeError 冒泡），现在是 failed（可读的断言失败）
        self.assertEqual(suite.get("failures"), "1")
        self.assertEqual(suite.get("errors"), "0")

        report = _read_text(junit_path)
        self.assertIn("TypeError", report)  # 原始异常类型没丢
        self.assertIn("elapsed_ms", report)  # 可操作建议也进了报告


class TestJsonschemaMatchViaCli(unittest.TestCase):
    """P2-b 端到端：`jsonschema_match` 要走完 `YAML → hmake → pytest`（不能只测直调）。

    零章的教训就是这个：`ResponseObject.validate()` 直调可用 ≠ 用户实际路径可用。
    """

    def setUp(self):
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.work_dir = _make_smoke_dir()

    def tearDown(self):
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _write_debugtalk(self, with_schema: bool = True) -> None:
        with open(
            os.path.join(self.work_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            if with_schema:
                f.write(
                    "USER_SCHEMA = {\n"
                    '    "$schema": "https://json-schema.org/draft/2020-12/schema",\n'
                    '    "type": "object",\n'
                    '    "required": ["args", "url"],\n'
                    "}\n"
                    # 故意要求两个不存在的东西：根上缺 `must_exist`、`args` 里缺 `must_be_there`
                    "STRICT_SCHEMA = {\n"
                    '    "$schema": "https://json-schema.org/draft/2020-12/schema",\n'
                    '    "type": "object",\n'
                    '    "required": ["args", "url", "must_exist"],\n'
                    '    "properties": {"args": {"type": "object", '
                    '"required": ["must_be_there"]}},\n'
                    "}\n"
                    "\n"
                    "\n"
                    "def get_user_schema():\n"
                    "    return USER_SCHEMA\n"
                    "\n"
                    "\n"
                    "def get_strict_schema():\n"
                    "    return STRICT_SCHEMA\n"
                )
            else:
                f.write("def noop():\n    return True\n")

    def _write_case(self, name: str, validators) -> str:
        yml_path = os.path.join(self.work_dir, f"{name}.yml")
        testcase = {
            "config": {"name": name, "base_url": HTTP_BIN_URL, "verify": False},
            "teststeps": [
                {
                    "name": "schema assertion",
                    "request": {"method": "GET", "url": "/get"},
                    "validate": validators,
                }
            ],
        }
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)
        return yml_path

    def test_schema_from_debugtalk_function(self):
        """引用方式 1（首选）：schema 由 debugtalk.py 的函数返回（`$schema` 原样存活）。"""
        self._write_debugtalk()
        yml_path = self._write_case(
            "schema_from_func", [{"jsonschema_match": ["body", "${get_user_schema()}"]}]
        )

        exit_code = main_run([yml_path, "--import-mode=importlib"])

        self.assertEqual(exit_code, 0)
        generated = _read_text(os.path.join(self.work_dir, "schema_from_func_test.py"))
        self.assertIn('.assert_jsonschema_match("body", "${get_user_schema()}")', generated)

    def test_schema_from_file_path(self):
        """引用方式 2（次选）：写 schema 文件路径，相对项目根目录（含 debugtalk.py 的那层）解析。"""
        self._write_debugtalk(with_schema=False)
        schema_dir = os.path.join(self.work_dir, "schemas")
        os.makedirs(schema_dir)
        with open(os.path.join(schema_dir, "user.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"type": "object", "required": ["args", "url"]}, f, ensure_ascii=False
            )
        yml_path = self._write_case(
            "schema_from_file", [{"jsonschema_match": ["body", "schemas/user.json"]}]
        )

        exit_code = main_run([yml_path, "--import-mode=importlib"])

        self.assertEqual(exit_code, 0)

    def test_schema_mismatch_is_a_readable_failure(self):
        self._write_debugtalk()
        yml_path = self._write_case(
            "schema_mismatch",
            [
                {
                    "jsonschema_match": [
                        "body",
                        "${get_strict_schema()}",
                        "响应契约不满足",
                    ]
                }
            ],
        )
        junit_path = os.path.join(self.work_dir, "junit.xml")

        exit_code = main_run(
            [yml_path, f"--junitxml={junit_path}", "--import-mode=importlib"]
        )

        self.assertNotEqual(exit_code, 0)
        suite = _parse_junit(junit_path)
        # 校验不通过应当是 failed（断言失败），而不是 error
        self.assertEqual(suite.get("failures"), "1")
        self.assertEqual(suite.get("errors"), "0")

        report = _read_text(junit_path)
        self.assertIn("响应契约不满足", report)  # 自定义 message 前置
        self.assertIn("JSON Schema 校验失败", report)  # 详细信息仍在
        self.assertIn("required", report)  # 哪个关键字不满足
        self.assertIn("must_exist", report)  # 缺哪个字段（根上）
        self.assertIn("$.args", report)  # 出错路径（嵌套层）与 jwt 路径格式

    def test_inline_schema_fails_fast_in_hmake(self):
        """内联 schema（带 `$` 键）必须在生成阶段退出，且**不留下生成文件**。"""
        self._write_debugtalk(with_schema=False)
        yml_path = self._write_case(
            "inline_schema",
            [
                {
                    "jsonschema_match": [
                        "body",
                        {"$schema": "https://json-schema.org/draft/2020-12/schema"},
                    ]
                }
            ],
        )

        with self.assertRaises(SystemExit) as ctx:
            main_run([yml_path])

        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(
            os.path.exists(os.path.join(self.work_dir, "inline_schema_test.py"))
        )


HAVE_LXML = importlib.util.find_spec("lxml") is not None


class TestXmlViaCli(unittest.TestCase):
    """A2-1 端到端：`xpath_match` / `xpath_count` 必须走完 `YAML → hmake → pytest`。

    零章的教训：直调可用 ≠ 用户路径可用 —— 这条链路上还有三个变量：`hmake` 的算子白名单、
    生成代码里的参数渲染、以及 `body` 在**真实响应**里的类型（非 JSON 响应是 bytes）。
    mock 的 `/xml/soap` 固定回一份带命名空间的 SOAP 报文，因此不依赖外网。
    """

    def setUp(self):
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.work_dir = _make_smoke_dir()

    def tearDown(self):
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _write_case(self, name: str, validators) -> str:
        yml_path = os.path.join(self.work_dir, f"{name}.yml")
        testcase = {
            "config": {"name": name, "base_url": HTTP_BIN_URL, "verify": False},
            "teststeps": [
                {
                    "name": "xml assertion",
                    "request": {"method": "GET", "url": "/xml/soap"},
                    "validate": validators,
                }
            ],
        }
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)
        return yml_path

    @unittest.skipUnless(HAVE_LXML, '需要 lxml：pip install -e ".[xml]"')
    def test_simplified_and_raw_xpath_via_cli(self):
        yml_path = self._write_case(
            "xml_ok",
            [
                {"xpath_match": ["body", ["code", "0000"]]},  # 简化写法（前缀无关）
                {"xpath_match": ["body", "//ns1:code/text()"]},  # 原始 XPath
                {"xpath_count": ["body", ["//ns1:item", 3]]},
                {"xpath_match": ["text", ["message", "成功"]]},  # 已解码文本通道
            ],
        )

        exit_code = main_run([yml_path, "--import-mode=importlib"])

        self.assertEqual(exit_code, 0)
        generated = _read_text(os.path.join(self.work_dir, "xml_ok_test.py"))
        self.assertIn(".assert_xpath_match(", generated)
        self.assertIn(".assert_xpath_count(", generated)

    @unittest.skipUnless(HAVE_LXML, '需要 lxml：pip install -e ".[xml]"')
    def test_xpath_mismatch_is_a_readable_failure(self):
        yml_path = self._write_case(
            "xml_fail", [{"xpath_match": ["body", ["code", "9999"], "业务码不对"]}]
        )
        junit_path = os.path.join(self.work_dir, "junit.xml")

        exit_code = main_run(
            [yml_path, f"--junitxml={junit_path}", "--import-mode=importlib"]
        )

        self.assertNotEqual(exit_code, 0)
        suite = _parse_junit(junit_path)
        # 断言不通过应当是 failed（断言失败），而不是 error
        self.assertEqual(suite.get("failures"), "1")
        self.assertEqual(suite.get("errors"), "0")

        report = _read_text(junit_path)
        self.assertIn("业务码不对", report)  # 自定义 message
        self.assertIn("0000", report)  # 实际值（否则只能靠猜）

    @unittest.skipUnless(HAVE_LXML, '需要 lxml：pip install -e ".[xml]"')
    def test_undefined_prefix_is_reported_not_silently_passed(self):
        """原始 XPath 里写了报文没有的前缀 → 必须报错（用例错误），绝不能静默放过。"""
        yml_path = self._write_case(
            "xml_bad_xpath", [{"xpath_match": ["body", "//svc:code"]}]
        )
        junit_path = os.path.join(self.work_dir, "junit.xml")

        exit_code = main_run(
            [yml_path, f"--junitxml={junit_path}", "--import-mode=importlib"]
        )

        self.assertNotEqual(exit_code, 0)
        suite = _parse_junit(junit_path)
        # NOTICE: 这里**不能**断言 `errors=1` —— pytest 的 JUnit 报告把 call 阶段的异常一律记
        # `<failure>`（`<error>` 只用于 setup/teardown 失败），所以 RuntimeError 与 AssertionError
        # 在计数上没区别；两者的区别在**异常类型与文案**（人读与工具分流），下面用文案钉住。
        self.assertEqual(suite.get("failures"), "1")
        self.assertEqual(suite.get("errors"), "0")

        report = _read_text(junit_path)
        self.assertIn("XPath 表达式不合法", report)
        self.assertIn("Undefined namespace prefix", report)
        self.assertIn("RuntimeError", report)  # 类型要能看出来是用例写错了

    # ------------------------------------------------------------------ XSD（A2-2）

    XSD_TEMPLATE = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"\n'
        '           xmlns:tns="http://demo.example.com/svc"\n'
        '           targetNamespace="http://demo.example.com/svc"\n'
        '           elementFormDefault="qualified">\n'
        "  <xs:element name=\"QueryResponse\">\n"
        "    <xs:complexType>\n"
        "      <xs:sequence>\n"
        "        <xs:element name=\"code\">\n"
        "          <xs:simpleType>\n"
        '            <xs:restriction base="xs:string">\n'
        '              <xs:pattern value="{pattern}"/>\n'
        "            </xs:restriction>\n"
        "          </xs:simpleType>\n"
        "        </xs:element>\n"
        '        <xs:element name="message" type="xs:string"/>\n'
        '        <xs:element name="item" minOccurs="0" maxOccurs="unbounded">\n'
        "          <xs:complexType>\n"
        '            <xs:attribute name="id" type="xs:positiveInteger" use="required"/>\n'
        "          </xs:complexType>\n"
        "        </xs:element>\n"
        "      </xs:sequence>\n"
        "    </xs:complexType>\n"
        "  </xs:element>\n"
        "</xs:schema>\n"
    )

    def _write_xsd(self, name: str = "soap.xsd", pattern: str = "0[0-9]{3}") -> str:
        """写一份匹配 mock `/xml/soap` 报文的 XSD（`pattern` 换掉即可造违例）。"""
        path = os.path.join(self.work_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.XSD_TEMPLATE.format(pattern=pattern))
        return path

    def _write_debugtalk_with_xsd(self) -> None:
        """项目 `debugtalk.py`：既让 work_dir 成为 RootDir，也提供 `${get_xsd()}` 引用路。"""
        with open(
            os.path.join(self.work_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write(
                "import os\n"
                "\n"
                "\n"
                "def get_xsd():\n"
                '    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "soap.xsd")\n'
            )

    @unittest.skipUnless(HAVE_LXML, '需要 lxml：pip install -e ".[xml]"')
    def test_xsd_via_cli(self):
        """A2-2 端到端：`xml_schema_match` 走完 `YAML → hmake → pytest`（相对路径按 RootDir 解析）。"""
        self._write_debugtalk_with_xsd()
        self._write_xsd()
        yml_path = self._write_case(
            "xml_xsd_ok",
            [
                {
                    "xml_schema_match": [
                        "body",
                        {"xpath": "QueryResponse", "xsd": "soap.xsd"},
                    ]
                }
            ],
        )

        exit_code = main_run([yml_path, "--import-mode=importlib"])

        self.assertEqual(exit_code, 0)
        generated = _read_text(os.path.join(self.work_dir, "xml_xsd_ok_test.py"))
        self.assertIn(".assert_xml_schema_match(", generated)

    @unittest.skipUnless(HAVE_LXML, '需要 lxml：pip install -e ".[xml]"')
    def test_xsd_from_debugtalk_function_path(self):
        """`${func()}` 返回路径：hmake 阶段**跳过**存在性检查（只有运行期才知道路径），运行期正常校验。"""
        self._write_debugtalk_with_xsd()
        self._write_xsd()
        yml_path = self._write_case(
            "xml_xsd_func",
            [
                {
                    "xml_schema_match": [
                        "body",
                        {"xpath": "QueryResponse", "xsd": "${get_xsd()}"},
                    ]
                }
            ],
        )

        exit_code = main_run([yml_path, "--import-mode=importlib"])

        self.assertEqual(exit_code, 0)

    @unittest.skipUnless(HAVE_LXML, '需要 lxml：pip install -e ".[xml]"')
    def test_xsd_violation_is_a_readable_failure(self):
        """不合契约 → **failed**（不是 error），报告里要有出错节点路径、原因与自定义 message。"""
        self._write_debugtalk_with_xsd()
        self._write_xsd(pattern="9[0-9]{3}")  # mock 回的是 0000 → 必违例
        yml_path = self._write_case(
            "xml_xsd_fail",
            [
                {
                    "xml_schema_match": [
                        "body",
                        {"xpath": "QueryResponse", "xsd": "soap.xsd"},
                        "响应不符合契约",
                    ]
                }
            ],
        )
        junit_path = os.path.join(self.work_dir, "junit.xml")

        exit_code = main_run(
            [yml_path, f"--junitxml={junit_path}", "--import-mode=importlib"]
        )

        self.assertNotEqual(exit_code, 0)
        suite = _parse_junit(junit_path)
        self.assertEqual(suite.get("failures"), "1")
        self.assertEqual(suite.get("errors"), "0")

        report = _read_text(junit_path)
        self.assertIn("响应不符合契约", report)  # 自定义 message
        self.assertIn("XSD 校验失败", report)
        self.assertIn("/ns1:QueryResponse/ns1:code", report)  # 出错节点路径
        self.assertIn("pattern", report)  # 具体原因

    def test_missing_xsd_fails_fast_in_hmake(self):
        """XSD 路径写错不该拖到跑用例才发现 —— 生成阶段就退出，且**不留下生成文件**。"""
        self._write_debugtalk_with_xsd()
        yml_path = self._write_case(
            "xml_xsd_missing",
            [{"xml_schema_match": ["body", {"xpath": "QueryResponse", "xsd": "nope.xsd"}]}],
        )

        with self.assertRaises(SystemExit) as ctx:
            main_run([yml_path])

        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(
            os.path.exists(os.path.join(self.work_dir, "xml_xsd_missing_test.py"))
        )

    def test_inline_xsd_fails_fast_in_hmake(self):
        """内联 XSD 字符串同样是「引用形态错误」→ 生成阶段拦住并给出正确写法。"""
        self._write_debugtalk_with_xsd()
        yml_path = self._write_case(
            "xml_xsd_inline",
            [{"xml_schema_match": ["body", "<xs:schema/>"]}],
        )

        with self.assertRaises(SystemExit) as ctx:
            main_run([yml_path])

        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(
            os.path.exists(os.path.join(self.work_dir, "xml_xsd_inline_test.py"))
        )


class TestCliArgumentHandling(unittest.TestCase):
    """M13 + L10：CLI 的「静默成功」与「三个别名口径不一致」（0918-5 批次）。

    NOTICE: 这两条都必须用**真实子进程**验——它们的症状就是进程退出码，
    在进程内调用会被 pytest 自己的异常/退出处理掩盖。
    """

    def _run_cli(self, argv: list, cwd: str = None):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, "-m", "interfacetester", *argv],
            cwd=cwd or os.getcwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    def test_unknown_subcommand_fails_loudly(self):
        """M13：未知子命令必须报错并非零退出。

        修复前 `cli.main()` 的 `len(sys.argv) == 2` 分支是一条**没有 else 的 elif 链**，
        未匹配时直接落到结尾的 `sys.exit(0)` → `interfacetester debug` **零输出、退出码 0**，
        用户会以为跑成功了（这是最难查的一类：看起来什么都没发生，但不是错误）。
        """
        proc = self._run_cli(["debug"])
        output = proc.stdout + proc.stderr

        self.assertNotEqual(proc.returncode, 0, output)
        self.assertIn("unknown command", output)
        self.assertIn("debug", output)
        # 报错之外还要给出可执行的信息（帮助）
        self.assertIn("usage:", output)

    def test_bare_path_is_routed_to_run(self):
        """M13 的另一半：`interfacetester <路径>` 不能被静默吞掉，且**带额外参数也要一致**。

        `hrun <路径>` 一直是插入 `run` 来支持的，但直接调 `interfacetester <路径>`
        在修复前落到 `sys.exit(0)` —— 静默什么都不做；而带上额外 pytest 参数时
        （`interfacetester <路径> -q`）又变成 argparse 的 `invalid choice`（exit 2）。

        这里用**空目录**：被路由到 run 之后会因为「找不到用例」而明确失败（exit 1），
        修复前两种写法则分别是 exit 0（静默）与 exit 2（argparse 报错）。空目录不依赖
        mock 服务/外网，因此适合做这条断言。
        """
        empty_dir = _make_smoke_dir()
        self.addCleanup(shutil.rmtree, empty_dir, True)

        for extra_args in ([], ["-q"], ["-q", "-p", "no:cacheprovider"]):
            with self.subTest(extra_args=extra_args):
                proc = self._run_cli([empty_dir, *extra_args])
                output = proc.stdout + proc.stderr

                self.assertNotEqual(
                    proc.returncode,
                    0,
                    f"裸路径被静默吞掉/被 argparse 拒绝（extra={extra_args}）：{output}",
                )
                self.assertIn("No valid testcases found", output)
                self.assertNotIn("invalid choice", output)

    def test_alias_version_flag_consistency(self):
        """L10：**每个**命令别名都必须支持 `-V`。

        修复前 `hrun -V` 打印版本、`hconvert -V` 也特判了，**唯独 `hmake -V` 漏了**
        → `error: unrecognized arguments: -V` + 退出码 2。
        典型的「改了一处漏了另一处」，所以这里做成**遍历所有别名**的不变量测试：
        以后再加新别名，漏了 `-V` 会直接失败。
        """
        from interfacetester import __version__

        aliases = {
            "hrun": "main_hrun_alias",
            "hmake": "main_make_alias",
            "hconvert": "main_convert_alias",
        }

        for alias_name, func_name in aliases.items():
            with self.subTest(alias=alias_name):
                # 直接以别名入口函数启动，等价于调用 hmake/hrun/hconvert 控制台脚本，
                # 但不依赖 .venv/Scripts 下是否装了对应的 exe（安装可能过期）。
                script = (
                    "import sys; "
                    "from interfacetester.cli import " + func_name + "; "
                    f"sys.argv = [{alias_name!r}, '-V']; "
                    + func_name
                    + "()"
                )
                env = dict(os.environ)
                env["PYTHONIOENCODING"] = "utf-8"
                proc = subprocess.run(
                    [sys.executable, "-c", script],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )

                self.assertEqual(
                    proc.returncode,
                    0,
                    f"{alias_name} -V 退出码应为 0，实际 {proc.returncode}："
                    f"{proc.stdout + proc.stderr}",
                )
                self.assertEqual(proc.stdout.strip(), __version__)

class TestBatch0918_8CliFixes(unittest.TestCase):
    """批次 8-5（0918-8）：L25（`--log-level=` 与子命令 `-V`）+ L26（`hrun -- <路径>`）。

    NOTICE: 这三条都必须用**真实子进程**验——症状分别是「日志级别没生效」
    （进程内看 loguru sink 也能验，但端到端更硬）与「退出码 / 一个用例都没跑」。
    """

    def _run_cli(self, argv: list, cwd: str = None):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("INTERFACETESTER_LOG_ENCODING", None)
        return subprocess.run(
            [sys.executable, "-m", "interfacetester", *argv],
            cwd=cwd or os.getcwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=300,
        )

    # ---------------------------------------------------------------- L25（版本）
    def test_version_flag_matrix_is_complete(self):
        """`-V` 的判据必须覆盖「顶层 + 三个子命令」；且不能拦 pytest 的透传参数。"""
        from interfacetester.cli import wants_version

        for argv in (
            ["interfacetester", "-V"],
            ["interfacetester", "--version"],
            ["interfacetester", "run", "-V"],
            ["interfacetester", "make", "--version"],
            ["interfacetester", "convert", "-V"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(wants_version(argv))

        for argv in (
            ["interfacetester"],
            ["interfacetester", "run"],
            # `hrun <用例> --version` 里的 `--version` 是 pytest 的透传参数，不能拦
            ["interfacetester", "run", "case.yml", "--version"],
            ["interfacetester", "run", "-V", "case.yml"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(wants_version(argv))

    def test_subcommand_version_flags_print_version(self):
        """L25：`interfacetester run -V` 修复前是 `No valid testcase path ... exit 1`。"""
        from interfacetester import __version__

        for argv in (["run", "-V"], ["make", "-V"], ["convert", "--version"]):
            with self.subTest(argv=argv):
                proc = self._run_cli(argv)
                output = proc.stdout + proc.stderr

                self.assertEqual(proc.returncode, 0, output)
                self.assertEqual(proc.stdout.strip(), __version__)

    # ------------------------------------------------- 批次 9-1（别名与子命令同源）
    def test_alias_version_flag_matrix_matches_the_subcommand_forms(self):
        """批次 9-1：三个别名与三个子命令对 `-V` 必须**完全等价**。

        L10 的教训是「三份拷贝必然漏一份」：`hmake -V` 曾因漏改而报
        `unrecognized arguments: -V`（退出码 2），而 `hrun -V` / `hconvert -V` 是对的。
        修复后三个别名共用 `rewrite_alias_to_subcommand`，本用例把「等价」钉成矩阵：
        每个别名 × 每个版本标志，结果必须与「顶层 `-V`」逐字节一致。
        """
        from interfacetester import __version__

        baseline = self._run_cli(["-V"])
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
        self.assertEqual(baseline.stdout.strip(), __version__)

        for alias in ("main_hrun_alias", "main_make_alias", "main_convert_alias"):
            for flag in ("-V", "--version"):
                with self.subTest(alias=alias, flag=flag):
                    proc = subprocess.run(
                        [sys.executable, "-c", f"from interfacetester.cli import {alias}; {alias}()", flag],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                        timeout=300,
                    )
                    output = proc.stdout + proc.stderr

                    self.assertEqual(proc.returncode, 0, output)
                    self.assertEqual(
                        proc.stdout.strip(),
                        __version__,
                        f"{alias} {flag} 与顶层 -V 不等价（L10 的漏改形态）",
                    )

    def test_alias_help_paths_stay_distinct(self):
        """反面护栏：`hrun -h` 是 **pytest** 的帮助，不能被并进 argparse 那条分支。"""
        hrun_proc = subprocess.run(
            [sys.executable, "-c", "from interfacetester.cli import main_hrun_alias; main_hrun_alias()", "-h"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=300,
        )
        output = hrun_proc.stdout + hrun_proc.stderr

        self.assertEqual(hrun_proc.returncode, 0, output)
        self.assertIn("pytest", output.lower())
        # 而 `hmake -h` 走 argparse，必须是 make 子命令自己的帮助
        hmake_proc = subprocess.run(
            [sys.executable, "-c", "from interfacetester.cli import main_make_alias; main_make_alias()", "-h"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=300,
        )
        hmake_output = hmake_proc.stdout + hmake_proc.stderr

        self.assertEqual(hmake_proc.returncode, 0, hmake_output)
        self.assertIn("usage", hmake_output.lower())
        self.assertNotIn("pytest", hmake_output.lower())

    # ---------------------------------------------------------------- L25（日志级别）
    def test_log_level_equals_form_is_honored(self):
        """`--log-level=DEBUG`（等号形式）修复前被静默忽略，日志停在 INFO。

        NOTICE: 必须带 `-s`（关掉 pytest 的 stdout 捕获）——DEBUG 内容是在
        pytest.main() 之后的用例执行期打出来的，不关捕获就看不到（用例通过时
        pytest 会把捕获到的 stdout 丢掉）。
        """
        work_dir = _make_smoke_dir()
        self.addCleanup(shutil.rmtree, work_dir, True)
        yml_path = _write_smoke_testcase(work_dir, "log_level_case")

        proc = self._run_cli(["run", yml_path, "--log-level=DEBUG", "-s"])
        output = proc.stdout + proc.stderr

        self.assertEqual(proc.returncode, 0, output)
        # DEBUG 级才有的内容（请求详情块由 client.get_req_resp_record 打）
        self.assertIn("request details", output)

    def test_log_level_space_form_still_works(self):
        """回归：分写形式 `--log-level DEBUG` 本来就生效，不能被这次改动弄坏。"""
        work_dir = _make_smoke_dir()
        self.addCleanup(shutil.rmtree, work_dir, True)
        yml_path = _write_smoke_testcase(work_dir, "log_level_space_case")

        proc = self._run_cli(["run", yml_path, "--log-level", "DEBUG", "-s"])
        output = proc.stdout + proc.stderr

        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("request details", output)

    def test_default_log_level_is_info(self):
        """反向断言：不给 `--log-level` 时仍是 INFO（DEBUG 内容不出现）。"""
        work_dir = _make_smoke_dir()
        self.addCleanup(shutil.rmtree, work_dir, True)
        yml_path = _write_smoke_testcase(work_dir, "log_level_default_case")

        proc = self._run_cli(["run", yml_path, "-s"])
        output = proc.stdout + proc.stderr

        self.assertEqual(proc.returncode, 0, output)
        self.assertNotIn("request details", output)

    # ---------------------------------------------------------------- L26
    def test_double_dash_before_path_still_runs_the_case(self):
        """`hrun -- <路径>`：framework flag 必须插在第一个 `--` **之前**。

        修复前 pytest 收到 `["--", "--tb=short", <生成物>]` —— `--tb=short` 被当成文件名，
        pytest 以 exit 4 退出，**一个用例都没跑**（而 `--` 本身是用户的合法写法：
        「后面都是文件参数」）。
        """
        work_dir = _make_smoke_dir()
        self.addCleanup(shutil.rmtree, work_dir, True)
        yml_path = _write_smoke_testcase(work_dir, "dash_dash_case")

        proc = self._run_cli(["run", "--", yml_path])
        output = proc.stdout + proc.stderr

        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("1 passed", output)
        self.assertNotIn("not found: --tb=short", output)

    def test_framework_flag_is_inserted_before_double_dash(self):
        """单元级：`main_run` 组的 pytest 参数里 `--tb=short` 必须在 `--` 之前。"""
        from interfacetester import cli
        from interfacetester import make as make_module

        captured = {}

        def fake_pytest_main(args):
            captured["args"] = list(args)
            return 0

        work_dir = _make_smoke_dir()
        self.addCleanup(shutil.rmtree, work_dir, True)
        yml_path = _write_smoke_testcase(work_dir, "dash_dash_unit_case")

        make_module.pytest_files_made_cache_mapping.clear()
        make_module.pytest_files_run_set.clear()
        loader.project_meta = None
        self.addCleanup(make_module.pytest_files_made_cache_mapping.clear)
        self.addCleanup(make_module.pytest_files_run_set.clear)

        with mock.patch.object(cli.pytest, "main", fake_pytest_main):
            cli.main_run(["--", yml_path])

        args = captured["args"]
        self.assertIn("--tb=short", args)
        self.assertLess(
            args.index("--tb=short"),
            args.index("--"),
            f"framework flag 必须在 `--` 之前，实际参数：{args}",
        )

    def test_installed_console_scripts_match_pyproject(self):
        """H5 相关：已安装的控制台脚本必须与 pyproject 的 `[project.scripts]` 一致。

        NOTICE: 实测本机 `.venv` 里**没有 `hconvert`**——`pip install -e .` 是在
        P3 批次把 `hconvert` 加进 pyproject **之前**跑的，所以 egg-info 的
        entry_points.txt 里只有 hmake/hrun/interfacetester，而 README 第 58-61 行
        却把 `hconvert` 写成了可用命令。属于「过期产物」，与 dist/build 同类。
        """
        import re

        pyproject_path = os.path.join(os.getcwd(), "pyproject.toml")
        if not os.path.isfile(pyproject_path):
            self.skipTest("不在仓库根运行")
        with open(pyproject_path, encoding="utf-8") as f:
            content = f.read()

        declared = set(
            re.findall(r"^([A-Za-z0-9_-]+)\s*=\s*\"interfacetester\.cli:", content, re.M)
        )
        self.assertTrue(declared, "没解析出 [project.scripts]")

        entry_points_path = os.path.join(
            os.getcwd(), "interfacetester.egg-info", "entry_points.txt"
        )
        if not os.path.isfile(entry_points_path):
            self.skipTest("没有 egg-info（非 editable 安装）")

        with open(entry_points_path, encoding="utf-8") as f:
            installed = set(
                re.findall(r"^([A-Za-z0-9_-]+)\s*=\s*interfacetester\.cli:", f.read(), re.M)
            )

        self.assertEqual(
            declared - installed,
            set(),
            "pyproject 声明了但当前安装里没有的控制台脚本（需要重新 pip install -e .）："
            f"{sorted(declared - installed)}",
        )


class TestBatch0918_9ImportedExpressionRefused(unittest.TestCase):
    """批次 9-0（M9）：**真实 CLI 端到端**——被导入素材里的表达式不能再执行。

    NOTICE（修复前实测，`docs/缺陷修复日志0918-9.md` §1）：同一份 Postman 集合
      `hconvert` **零告警**（还打印「导出即验证通过」）→ 生成的 YAML 原样带着
      `${eval($p)}` → `hrun` **真的执行了** payload → marker 文件被写出，
      而 pytest 报 **1 passed**、退出码 **0**（连接失败被 safe mode 吞掉，
      与「4xx/5xx 不自动判失败」同源）。

    修复后的判据（两条都要）：
      1. 导入期：`hconvert` 必须对素材里的 `${...}` 告警；
      2. 运行期：`hrun` 必须**拒绝执行**（退出码非 0、输出里点名 `eval`），且 marker **不存在**。
    另有一条**非空洞自检**：打开逃生口后同一份用例必须真的把 marker 写出来——
    否则"marker 不存在"可能只是因为这份用例本来就跑不到那一步。
    """

    def setUp(self):
        self.work_dir = _make_smoke_dir()
        self.marker = os.path.join(self.work_dir, "pwned.txt").replace("\\", "/")
        self.collection = os.path.join(self.work_dir, "evil.postman_collection.json")
        # payload 本身完全合法（无引号参数限制——它是**变量值**，不是函数参数）
        payload = f"open(r'{self.marker}', 'w').write('pwned')"
        collection = {
            "info": {
                "name": "evil-demo",
                "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            },
            "variable": [{"key": "p", "value": payload}],
            "item": [
                {
                    "name": "victim",
                    "request": {
                        "method": "GET",
                        "header": [
                            {"key": "X-Note", "value": "${eval($p)}"},
                            # 让变量 p 被"正常引用"一次，导入器才会把它保留进 config.variables
                            {"key": "X-P", "value": "${p}"},
                        ],
                        "url": {
                            "raw": "http://127.0.0.1:1/${eval($p)}",
                            "protocol": "http",
                            "host": ["127", "0", "0", "1"],
                            "port": "1",
                            "path": ["${eval($p)}"],
                        },
                    },
                }
            ],
        }
        with open(self.collection, "w", encoding="utf-8") as fp:
            json.dump(collection, fp, ensure_ascii=False)

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _run_cli(self, argv: list, extra_env: dict = None):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("INTERFACETESTER_LOG_ENCODING", None)
        env.update(extra_env or {})
        proc = subprocess.run(
            [sys.executable, "-m", "interfacetester", *argv],
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=300,
        )
        return proc, proc.stdout + proc.stderr

    def _convert(self) -> str:
        out_dir = os.path.join(self.work_dir, "out")
        proc, output = self._run_cli(
            ["convert", "--from", "postman", "--in", self.collection, "--out", out_dir]
        )
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("表达式", output, f"hconvert 没有对素材里的 `${{...}}` 告警：{output}")
        self.assertIn("eval($p)", output)

        generated = os.path.join(out_dir, "evil.postman_collection.yml")
        self.assertTrue(os.path.isfile(generated), output)
        # 刻意的决定：导入器不改写（只告警），所以产物里仍是原文
        self.assertIn("${eval($p)}", _read_text(generated))
        return generated

    def test_convert_warns_and_run_refuses_to_execute(self):
        """判据 1 + 2：告警有、拒绝有、marker 没有。"""
        generated = self._convert()
        self.assertFalse(os.path.exists(self.marker), "测试前提不成立：marker 一开始就存在")

        proc, output = self._run_cli(["run", generated])

        # 判据顺序刻意如此：**先**确认「payload 没有被执行」，再看报错文案与退出码。
        # 注入 HEAD 版本时，失败点就落在这一行（修复前它写出 marker 并且 1 passed / exit 0）。
        self.assertFalse(
            os.path.exists(self.marker),
            "payload 被执行了（marker 文件被写出）——内置白名单没挡住",
        )
        self.assertIn("not allowed in", output, f"没有看到内置白名单的拒绝信息：{output}")
        self.assertIn("eval", output)
        self.assertNotEqual(proc.returncode, 0, output)

    def test_escape_hatch_would_execute_it(self):
        """非空洞自检：打开逃生口后，同一份用例必须**真的**写出 marker。

        这条证明「上面的 marker 不存在」来自白名单，而不是这份用例本来就跑不到那一步。
        """
        generated = self._convert()

        proc, output = self._run_cli(
            ["run", generated], extra_env={"INTERFACETESTER_ALLOW_ALL_BUILTINS": "1"}
        )

        self.assertTrue(
            os.path.exists(self.marker),
            f"逃生口打开后 payload 仍没执行——夹具本身失效了：{output}",
        )
        with open(self.marker, encoding="utf-8") as fp:
            self.assertEqual(fp.read(), "pwned")
        # 逃生口是"回到修复前行为"：**表达式被执行**了（上面的 marker 就是证据）。
        #
        # ⚠️ 订正（批次 7 / M6）：这句注释原先写的是"连接失败的用例在旧行为下就是 passed/exit 0"
        # —— 前提说错了。本夹具的失败**不是连接失败**，而是 `X-Note: 5` 这种**头部类型错误**：
        # 修复前 `_send_request_safe_mode` 把它一起吞成 `status_code=0` 的假响应，而该用例
        # **没有断言** → 判 passed、exit 0。M6 之后构造期错误直接抛 `ParamsError`
        # （附字段/类型/改法），于是这里**必然** exit 1 —— 这正是那条修复要暴露的"假通过"。
        # 所以这条自检不再断言退出码（那是旧行为），只断言逃生口真的开了（marker 存在）。
        self.assertIn(
            "构造阶段",
            output,
            "逃生口打开后仍应看到 M6 的构造期报错（否则说明夹具的失败原因又变了）",
        )
