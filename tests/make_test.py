import ast
import datetime
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

import yaml
from loguru import logger

from interfacetester import Config, RunRequest, Step, exceptions, loader
from interfacetester import make as make_module
from interfacetester.builtin import comparators as builtin_comparators
from interfacetester.models import (
    KNOWN_CONFIG_FIELDS,
    KNOWN_REQUEST_FIELDS,
    TConfig,
    TRequest,
    TStep,
)
from interfacetester.utils import HTTP_BIN_URL
from interfacetester import compat

# 仓库根（与 tests/generated_artifacts_drift_test.py 的 BASE 同口径）：
# 用于「全仓对账」类护栏扫描 examples/ 下的已入库生成物。
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
from interfacetester.make import (
    BLACK_CACHE_DIR_ENV,
    BLACK_CACHE_DIR_NAME,
    BLACK_TARGET_VERSION_ENV,
    BLACK_TIMEOUT_ENV,
    BLACK_TIMEOUT_SECONDS,
    BUILTIN_COMPARATOR_NAMES,
    GENERATOR_SUPPORTED_CONFIG_FIELDS,
    GENERATOR_SUPPORTED_STEP_FIELDS,
    STRUCTURAL_CONFIG_FIELDS,
    UNSUPPORTED_CONFIG_FIELDS,
    UNSUPPORTED_STEP_FIELDS,
    main_make,
    convert_testcase_path,
    ensure_generatable_config,
    ensure_generatable_teststeps,
    ensure_json_schema_not_inline,
    ensure_known_comparators,
    normalize_module_segment,
    ensure_xsd_files_exist,
    pytest_files_made_cache_mapping,
    format_pytest_with_black,
    get_black_command,
    get_black_env,
    get_black_target_version,
    get_black_timeout,
    make_config_chain_style,
    make_config_skip,
    make_request_chain_style,
    make_testcase,
    make_teststep_chain_style,
    pytest_files_run_set,
    run_black,
    ensure_file_abs_path_valid,
)


def _make_tmp_project_dir() -> str:
    """建一个临时项目目录（放在 logs/ 下：已被 .gitignore 覆盖，也不会被 pytest 收集）。

    NOTICE: 不用 `tempfile.mkdtemp()` —— 它按 0o700 建目录，在受限环境（沙箱/受限令牌）下
    该 ACL 会让随后的 `open()` 直接 PermissionError；`logs/` + 默认权限则与环境无关
    （与 `cli_test._make_smoke_dir` 同一套做法）。
    """
    tmp_dir = os.path.join(os.getcwd(), "logs", f"tmp_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir)
    return tmp_dir


def _import_python_file(python_path: str, module_name: str):
    """真实 import 一个生成的 `_test.py`（import 期就会构造 Config/Step 链）。"""
    spec = importlib.util.spec_from_file_location(module_name, python_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMake(unittest.TestCase):
    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.data_dir = os.path.join(os.getcwd(), "examples", "data")

    def test_make_testcase(self):
        path = ["examples/postman_echo/request_methods/request_with_variables.yml"]
        testcase_python_list = main_make(path)
        self.assertEqual(
            testcase_python_list[0],
            os.path.join(
                os.getcwd(),
                os.path.join(
                    "examples",
                    "postman_echo",
                    "request_methods",
                    "request_with_variables_test.py",
                ),
            ),
        )

    def test_make_testcase_with_ref(self):
        path = [
            "examples/postman_echo/request_methods/request_with_testcase_reference.yml"
        ]
        testcase_python_list = main_make(path)
        self.assertEqual(len(testcase_python_list), 1)
        self.assertIn(
            os.path.join(
                os.getcwd(),
                os.path.join(
                    "examples",
                    "postman_echo",
                    "request_methods",
                    "request_with_testcase_reference_test.py",
                ),
            ),
            testcase_python_list,
        )

        with open(
            os.path.join(
                "examples",
                "postman_echo",
                "request_methods",
                "request_with_testcase_reference_test.py",
            )
        ) as f:
            content = f.read()
            self.assertIn(
                """
from request_methods.request_with_functions_test import (
    TestCaseRequestWithFunctions as RequestWithFunctions,
)
""",
                content,
            )
            self.assertIn(
                ".call(RequestWithFunctions)",
                content,
            )

    def test_make_testcase_folder(self):
        path = ["examples/postman_echo/request_methods/"]
        testcase_python_list = main_make(path)
        self.assertIn(
            os.path.join(
                os.getcwd(),
                os.path.join(
                    "examples",
                    "postman_echo",
                    "request_methods",
                    "request_with_functions_test.py",
                ),
            ),
            testcase_python_list,
        )

    def test_ensure_file_path_valid(self):
        self.assertEqual(
            ensure_file_abs_path_valid(os.path.join(self.data_dir, "a-b.c", "2 3.yml")),
            os.path.join(self.data_dir, "a_b_c", "T2_3.yml"),
        )
        loader.project_meta = None
        self.assertEqual(
            ensure_file_abs_path_valid(
                os.path.join(os.getcwd(), "examples", "postman_echo", "request_methods")
            ),
            os.path.join(os.getcwd(), "examples", "postman_echo", "request_methods"),
        )
        loader.project_meta = None
        self.assertEqual(
            ensure_file_abs_path_valid(os.path.join(os.getcwd(), "pyproject.toml")),
            os.path.join(os.getcwd(), "pyproject.toml"),
        )
        loader.project_meta = None
        self.assertEqual(
            ensure_file_abs_path_valid(os.getcwd()),
            os.getcwd(),
        )
        loader.project_meta = None
        self.assertEqual(
            ensure_file_abs_path_valid(os.path.join(self.data_dir, ".csv")),
            os.path.join(self.data_dir, ".csv"),
        )

    def test_convert_testcase_path(self):
        self.assertEqual(
            convert_testcase_path(os.path.join(self.data_dir, "a-b.c", "2 3.yml")),
            (
                os.path.join(self.data_dir, "a_b_c", "T2_3_test.py"),
                "T23",
            ),
        )
        self.assertEqual(
            convert_testcase_path(os.path.join(self.data_dir, "a-b.c", "中文case.yml")),
            (
                os.path.join(self.data_dir, "a_b_c", "中文case_test.py"),
                "中文Case",
            ),
        )

    def test_make_config_chain_style(self):
        config = {
            "name": "request methods testcase: validate with functions",
            "variables": {"foo1": "bar1", "foo2": 22},
            "base_url": "https://postman_echo.com",
            "verify": False,
            "path": "examples/postman_echo/request_methods/validate_with_functions_test.py",
        }
        self.assertEqual(
            make_config_chain_style(config),
            """Config("request methods testcase: validate with functions").variables(**{'foo1': 'bar1', 'foo2': 22}).base_url("https://postman_echo.com").verify(False)""",
        )

    def test_make_teststep_chain_style(self):
        step = {
            "name": "get with params",
            "variables": {
                "foo1": "bar1",
                "foo2": 123,
                "sum_v": "${sum_two(1, 2)}",
            },
            "request": {
                "method": "GET",
                "url": "/get",
                "params": {"foo1": "$foo1", "foo2": "$foo2", "sum_v": "$sum_v"},
                "headers": {"User-Agent": "InterfaceTester/${get_interfacetester_version()}"},
            },
            "testcase": "CLS_LB(TestCaseDemo)CLS_RB",
            "extract": {
                "session_foo1": "body.args.foo1",
                "session_foo2": "body.args.foo2",
            },
            "validate": [
                {"eq": ["status_code", 200]},
                {"eq": ["body.args.sum_v", "3"]},
            ],
        }
        teststep_chain_style = make_teststep_chain_style(step)
        self.assertEqual(
            teststep_chain_style,
            """Step(RunRequest("get with params").with_variables(**{'foo1': 'bar1', 'foo2': 123, 'sum_v': '${sum_two(1, 2)}'}).get("/get").with_params(**{'foo1': '$foo1', 'foo2': '$foo2', 'sum_v': '$sum_v'}).with_headers(**{'User-Agent': 'InterfaceTester/${get_interfacetester_version()}'}).extract().with_jmespath('body.args.foo1', 'session_foo1').with_jmespath('body.args.foo2', 'session_foo2').validate().assert_equal("status_code", 200).assert_equal("body.args.sum_v", "3"))""",
        )

    def test_make_requests_with_json_chain_style(self):
        step = {
            "name": "get with params",
            "variables": {
                "foo1": "bar1",
                "foo2": 123,
                "sum_v": "${sum_two(1, 2)}",
                "myjson": {"name": "user", "password": "123456"},
            },
            "request": {
                "method": "GET",
                "url": "/get",
                "params": {"foo1": "$foo1", "foo2": "$foo2", "sum_v": "$sum_v"},
                "headers": {"User-Agent": "InterfaceTester/${get_interfacetester_version()}"},
                "json": "$myjson",
            },
            "testcase": "CLS_LB(TestCaseDemo)CLS_RB",
            "extract": {
                "session_foo1": "body.args.foo1",
                "session_foo2": "body.args.foo2",
            },
            "validate": [
                {"eq": ["status_code", 200]},
                {"eq": ["body.args.sum_v", "3"]},
            ],
        }
        teststep_chain_style = make_teststep_chain_style(step)
        self.assertEqual(
            teststep_chain_style,
            """Step(RunRequest("get with params").with_variables(**{'foo1': 'bar1', 'foo2': 123, 'sum_v': '${sum_two(1, 2)}', 'myjson': {'name': 'user', 'password': '123456'}}).get("/get").with_params(**{'foo1': '$foo1', 'foo2': '$foo2', 'sum_v': '$sum_v'}).with_headers(**{'User-Agent': 'InterfaceTester/${get_interfacetester_version()}'}).with_json("$myjson").extract().with_jmespath('body.args.foo1', 'session_foo1').with_jmespath('body.args.foo2', 'session_foo2').validate().assert_equal("status_code", 200).assert_equal("body.args.sum_v", "3"))""",
        )


class TestMakeSpecialChars(unittest.TestCase):
    """`hmake` 生成 _test.py 时，值里的引号/反斜杠/换行必须被正确转义。

    NOTICE: 修复前是手写引号拼接（f'"{value}"'），值里出现引号或换行时生成的
    _test.py 直接是 SyntaxError——用户拿到的是一个完全跑不起来的用例文件。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None

    def test_make_config_chain_style_with_special_chars(self):
        tricky_name = 'request "quoted" \\ name\nnewline'
        tricky_base_url = 'https://postman_echo.com/a"b'
        variables = {
            "quoted": 'a"b\\c',
            "multiline": "line1\nline2",
            "nested": {"password": "p'q"},
        }
        config = {
            "name": tricky_name,
            "variables": variables,
            "base_url": tricky_base_url,
            "verify": False,
            "oauth2": {
                "token_url": 'https://postman_echo.com/oauth2?q="1"',
                "client_id": "client'id",
                "client_secret": "$SECRET",
                "scope": 'scope"1',
            },
            "export": ["session_foo1", "session_foo2"],
            "path": "examples/postman_echo/request_methods/special_chars_test.py",
        }
        config_chain_style = make_config_chain_style(config)

        # 1) 生成的代码必须是合法 Python
        ast.parse(config_chain_style)

        # 2) 值的语义必须原样保留（引号/反斜杠/换行都没有被吃掉）
        config_obj = eval(config_chain_style, {"Config": Config})
        testcase_config = config_obj.struct()
        self.assertEqual(testcase_config.name, tricky_name)
        self.assertEqual(testcase_config.base_url, tricky_base_url)
        self.assertEqual(testcase_config.variables, variables)
        self.assertEqual(
            testcase_config.oauth2.token_url, 'https://postman_echo.com/oauth2?q="1"'
        )
        self.assertEqual(testcase_config.oauth2.client_id, "client'id")
        self.assertEqual(testcase_config.oauth2.client_secret, "$SECRET")
        self.assertEqual(testcase_config.oauth2.scope, 'scope"1')
        self.assertEqual(testcase_config.export, ["session_foo1", "session_foo2"])

    def test_make_request_chain_style_with_special_chars(self):
        request = {
            "method": "post",
            "url": '/post?q="1"',
            "params": {"quoted": 'a"b', "multiline": "line1\nline2"},
            "headers": {"X-Quoted": 'InterfaceTester/3.0 "quoted" \\ slash'},
            "cookies": {"cookie'name": 'value"1'},
            "data": 'line1\nline2 "quoted" \\ tail',
            "json": {"key'1": 'value"2'},
            "timeout": 10,
            "verify": False,
            "allow_redirects": False,
            "upload": {"file": "examples/data/curl/curl_examples.txt"},
            "x_path": '/a"b/中文',
        }
        request_chain_style = make_request_chain_style(request)

        ast.parse(f'RunRequest("tmp"){request_chain_style}')

        step = eval(
            f'RunRequest("tmp"){request_chain_style}', {"RunRequest": RunRequest}
        ).struct()
        self.assertEqual(step.request.url, request["url"])
        self.assertEqual(step.request.params, request["params"])
        self.assertEqual(step.request.headers, request["headers"])
        self.assertEqual(step.request.cookies, request["cookies"])
        self.assertEqual(step.request.data, request["data"])
        self.assertEqual(step.request.req_json, request["json"])
        self.assertEqual(step.request.timeout, 10)
        self.assertEqual(step.request.verify, False)
        self.assertEqual(step.request.allow_redirects, False)
        self.assertEqual(step.request.upload, request["upload"])
        self.assertEqual(step.request.x_path, request["x_path"])

    def test_make_teststep_chain_style_with_special_chars(self):
        step_dict = {
            "name": 'step "quoted"\nname',
            "variables": {"quoted": 'a"b\\c'},
            "setup_hooks": [{"assigned'var": 'setup "hook"'}],
            "request": {"method": "get", "url": '/get?q="1"'},
            "extract": {"quoted'1": 'body."user-agent"'},
            "teardown_hooks": ['teardown "hook"'],
            "validate": [
                {"eq": ["status_code", 200]},
                {"eq": ['body."user-agent"', "x'y"]},
                {"eq": ["body.args.sum_v", '3"x', 'message "quoted"\nnext']},
            ],
        }
        step_chain_style = make_teststep_chain_style(step_dict)

        ast.parse(step_chain_style)

        step = eval(step_chain_style, {"Step": Step, "RunRequest": RunRequest}).struct()
        self.assertEqual(step.name, 'step "quoted"\nname')
        self.assertEqual(step.variables, {"quoted": 'a"b\\c'})
        self.assertEqual(step.setup_hooks, [{"assigned'var": 'setup "hook"'}])
        self.assertEqual(step.teardown_hooks, ['teardown "hook"'])
        self.assertEqual(step.extract, {"quoted'1": 'body."user-agent"'})
        self.assertEqual(
            step.validators,
            [
                {"equal": ["status_code", 200, ""]},
                {"equal": ['body."user-agent"', "x'y", ""]},
                {"equal": ["body.args.sum_v", '3"x', 'message "quoted"\nnext']},
            ],
        )

    def test_make_config_skip_with_special_chars(self):
        skip_reason = 'skip "because" \\ it\nbreaks'
        skip_style = make_config_skip({"skip": skip_reason})
        self.assertEqual(skip_style, json.dumps(skip_reason, ensure_ascii=False))
        self.assertEqual(ast.literal_eval(skip_style), skip_reason)

        # 非字符串真值 → 无条件跳过；假值 → 不生成 skip 标记
        self.assertEqual(
            ast.literal_eval(make_config_skip({"skip": True})),
            "skip unconditionally",
        )
        self.assertIsNone(make_config_skip({"skip": False}))
        self.assertIsNone(make_config_skip({}))

    def test_make_config_chain_style_emits_skip(self):
        """0920 批次 2 / **N16**：`skip` 必须**同时**进生成物与运行期对象。

        修复前只生成 `@pytest.mark.skip`（模板层），而引用用例走的是**直接调用**
        `test_start()` —— pytest 标记对直接调用无效，于是"声明 skip 的用例"被引用时照跑。
        修复后 `make_config_chain_style` 额外产出 `.skip(...)`，让值进入运行期 `TConfig`。
        """
        style = make_config_chain_style(
            {"name": "child", "variables": {}, "base_url": "http://x", "skip": "还没就绪"}
        )
        self.assertIn(".skip(", style)
        # 生成的是**合法 Python 字面量**（与 make_config_skip 同一套转义）
        self.assertIn('"还没就绪"', style)

        # `skip: true` → 无条件跳过（非字符串真值）
        self.assertIn(
            '"skip unconditionally"',
            make_config_chain_style(
                {"name": "c", "variables": {}, "skip": True}
            ),
        )

    def test_no_skip_call_when_skip_is_falsy(self):
        """反向护栏：`skip: false` / 未配置 → 不许出现 `.skip(...)`。"""
        for config in (
            {"name": "c", "variables": {}},
            {"name": "c", "variables": {}, "skip": False},
            {"name": "c", "variables": {}, "skip": None},
            {"name": "c", "variables": {}, "skip": ""},
        ):
            with self.subTest(config=config):
                self.assertNotIn(".skip(", make_config_chain_style(config))

    def test_config_object_carries_skip_for_referenced_cases(self):
        """`TConfig` 必须真的带 `skip`（引用链路上只有它能让运行期看到）。"""
        from interfacetester.models import TConfig

        self.assertIn("skip", TConfig.model_fields)
        self.assertEqual(TConfig(name="c", skip="原因").skip, "原因")
        self.assertTrue(TConfig(name="c", skip=True).skip)
        self.assertFalse(TConfig(name="c").skip)

    def test_make_config_chain_style_warns_on_non_literal_value(self):
        """YAML 未加引号的日期会被解析成 datetime.date → repr 出来的一行代码在
        生成的 _test.py 里无法复原（NameError）；必须提前**报错拦截**。

        NOTICE（本批行为收紧）：修复前这里只**告警**，于是 `hmake` 退出码 0
        且产物照写，pytest 收集期才 `NameError: name 'datetime' is not defined`——
        与 `ensure_generated_python_is_valid()` 那句「保证写出去的文件一定能被 import」
        直接矛盾。现在升级为「告警 + 点名 ParamsError 阻止落盘」，
        因此本用例断言的是**异常**（并顺带钉住告警里仍带字段定位）。
        """
        config = {
            "name": "non literal value",
            "variables": {"start_date": datetime.date(2020, 1, 1)},
            "path": "examples/postman_echo/request_methods/non_literal_test.py",
        }

        log_messages = []
        sink_id = logger.add(log_messages.append, format="{message}")
        try:
            with self.assertRaises(exceptions.ParamsError) as ctx:
                make_config_chain_style(config)
        finally:
            logger.remove(sink_id)

        message = str(ctx.exception)
        # 告警仍然要发（"失败必须留证据"），且仍然点名到字段
        self.assertTrue(
            any("无法用 Python 字面量表示" in m for m in log_messages), log_messages
        )
        self.assertTrue(any("start_date" in m for m in log_messages), log_messages)
        # 报错里要给出定位与改法（而不是只留一条与 YAML 脱节的 NameError）
        self.assertIn("start_date", message)
        self.assertIn("已阻止生成该用例文件", message)

    def test_make_config_chain_style_warns_on_non_string_key(self):
        config = {
            "name": "non string key",
            "variables": {2020: "1"},
            "path": "examples/postman_echo/request_methods/non_literal_test.py",
        }

        log_messages = []
        sink_id = logger.add(log_messages.append, format="{message}")
        try:
            make_config_chain_style(config)
        finally:
            logger.remove(sink_id)

        self.assertTrue(
            any("keywords must be strings" in message for message in log_messages),
            log_messages,
        )


    def test_make_testcase_with_special_chars(self):
        """端到端：含引号/换行的用例要能生成、能被 import（修复前生成文件是 SyntaxError）。"""
        tmp_dir = _make_tmp_project_dir()
        try:
            yml_path = os.path.join(tmp_dir, "special_chars.yml")
            testcase = {
                "config": {
                    "name": 'case "with quotes"',
                    "base_url": "https://postman_echo.com",
                    "variables": {
                        "quoted": 'a"b\\c',
                        "multiline": "line1\nline2",
                    },
                    "verify": False,
                },
                "teststeps": [
                    {
                        "name": 'step "quoted"',
                        "request": {
                            "method": "GET",
                            "url": '/get?q="1"',
                            "headers": {"X-Quoted": 'a"b'},
                        },
                        "validate": [{"eq": ["status_code", 200, 'msg "quoted"']}],
                    }
                ],
            }
            with open(yml_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(testcase, f, allow_unicode=True)

            testcase_python_list = main_make([yml_path])
            self.assertEqual(len(testcase_python_list), 1)
            generated_python_path = testcase_python_list[0]

            with open(generated_python_path, encoding="utf-8") as f:
                generated_content = f.read()

            # 修复前这里会抛 SyntaxError
            ast.parse(generated_content)

            # 生成文件必须能被真实 import（Config/Step 在 import 期构造）
            spec = importlib.util.spec_from_file_location(
                "special_chars_test", generated_python_path
            )
            generated_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(generated_module)

            generated_testcase_cls = generated_module.TestCaseSpecialChars
            self.assertEqual(generated_testcase_cls.config.name, 'case "with quotes"')
            self.assertEqual(
                generated_testcase_cls.config.struct().variables,
                {"quoted": 'a"b\\c', "multiline": "line1\nline2"},
            )
            self.assertEqual(
                generated_testcase_cls.teststeps[0].struct().name, 'step "quoted"'
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestMakeAcrossProjects(unittest.TestCase):
    """同一进程内先后处理多个项目（含「没有 debugtalk.py 的目录」）。

    NOTICE: 修复前 make 会沿用「当前已加载的其它项目」的 RootDir，于是对无项目标记的目录
    做 hmake 时，拿**去掉扩展名的派生路径**去重新定位项目 → 直接
    `SystemExit: 1`，报错信息还是 `path not exist: .../case`（看不出少了 .yml）。
    触发条件很现实：`hrun 项目A/case.yml 无标记目录/case.yml`，或同一 pytest 会话里
    先跑过别的项目。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.work_dir = os.path.join(
            os.getcwd(), "logs", f"make_smoke_{uuid.uuid4().hex[:6]}"
        )
        os.makedirs(self.work_dir)

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_make_projectless_path_after_loading_other_project(self):
        # 1) 先加载一个「有 debugtalk.py 的项目」，模拟同进程已处理过别的项目
        loader.load_project_meta(os.path.join(os.getcwd(), "examples", "httpbin"))

        # 2) 再对一个「没有 debugtalk.py」的目录生成用例（RootDir 应回落到 cwd）
        yml_path = os.path.join(self.work_dir, "projectless.yml")
        testcase = {
            "config": {"name": "projectless", "base_url": HTTP_BIN_URL},
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

        generated_list = main_make([yml_path])

        self.assertEqual(len(generated_list), 1)
        self.assertTrue(os.path.isfile(generated_list[0]))
        self.assertEqual(
            os.path.normcase(loader.load_project_meta(yml_path).RootDir),
            os.path.normcase(os.getcwd()),
        )

    def test_relative_projectless_path_is_resolved_against_cwd(self):
        """相对路径 + 无项目标记：应相对 cwd 解析（修复前会拼到别的项目 RootDir 上）。"""
        loader.load_project_meta(os.path.join(os.getcwd(), "examples", "httpbin"))

        relative_yml = os.path.join(
            "logs", os.path.basename(self.work_dir), "relative_projectless.yml"
        )
        yml_path = os.path.join(os.getcwd(), relative_yml)
        testcase = {
            "config": {"name": "relative projectless", "base_url": HTTP_BIN_URL},
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

        generated_list = main_make([relative_yml])

        self.assertEqual(len(generated_list), 1)
        self.assertEqual(
            os.path.normcase(generated_list[0]),
            os.path.normcase(
                os.path.join(
                    os.getcwd(), "logs", os.path.basename(self.work_dir),
                    "relative_projectless_test.py",
                )
            ),
        )


class TestBlackFormatting(unittest.TestCase):
    """`hmake` 调用 black 的三重加固（详见 docs/待处理项方案.md 批次 1）。

    NOTICE:
        1. 修复前 ``subprocess.run(["black", ...])`` 没有 timeout：black 挂住
           （例如缓存目录不可写时卡在写缓存上）会让 hmake/hrun 永久挂死；
        2. 修复前固定用 PATH 上的 black：black 其实是本项目声明的运行时依赖，
           未激活 venv 时会误报「missing dependency tool」；
        3. 修复前逐个格式化的分支是列表推导：第一个文件报错后续文件全部不格式化。

    这里全部用 mock 覆盖 ``subprocess.run``，不依赖本机是否装了 black，
    也不依赖任何外部环境。
    """

    def _capture_logs(self, func, *args, **kwargs):
        """执行 func 并返回 loguru 捕获到的日志文本列表。"""
        log_messages = []
        sink_id = logger.add(log_messages.append, format="{message}")
        try:
            func(*args, **kwargs)
        finally:
            logger.remove(sink_id)
        return log_messages

    def test_black_command_prefers_current_interpreter(self):
        """装了 black 的解释器 → 用 `python -m black`，不再依赖 PATH。"""
        with mock.patch(
            "interfacetester.make.importlib.util.find_spec", return_value=object()
        ):
            command = get_black_command("a_test.py", "b_test.py")

        self.assertEqual(command[:3], [sys.executable, "-m", "black"])
        # M12：中间要带一个显式的 --target-version（见 test_black_command_*）
        self.assertIn("--target-version", command)
        self.assertEqual(command[-2:], ["a_test.py", "b_test.py"])

    def test_black_command_falls_back_to_path(self):
        """解释器里没有 black → 回落到 PATH 上的 black（兼容老环境）。"""
        with mock.patch(
            "interfacetester.make.importlib.util.find_spec", return_value=None
        ):
            command = get_black_command("a_test.py")

        self.assertEqual(command[0], "black")
        self.assertIn("--target-version", command)
        self.assertEqual(command[-1], "a_test.py")

    def test_black_timeout_from_env_with_fallback(self):
        """超时可配：合法值生效，非法/非正值回落默认值且告警。"""
        with mock.patch.dict(os.environ, {BLACK_TIMEOUT_ENV: "5"}):
            self.assertEqual(get_black_timeout(), 5)

        with mock.patch.dict(os.environ, {BLACK_TIMEOUT_ENV: ""}):
            self.assertEqual(get_black_timeout(), float(BLACK_TIMEOUT_SECONDS))

        for invalid_value in ["abc", "0", "-1"]:
            with self.subTest(invalid_value=invalid_value):
                with mock.patch.dict(os.environ, {BLACK_TIMEOUT_ENV: invalid_value}):
                    log_messages = self._capture_logs(get_black_timeout)
                    timeout = get_black_timeout()

                self.assertEqual(timeout, float(BLACK_TIMEOUT_SECONDS))
                self.assertTrue(
                    any(BLACK_TIMEOUT_ENV in message for message in log_messages),
                    log_messages,
                )

    def test_run_black_always_passes_timeout(self):
        """核心回归：每次调用 black 都必须带 timeout（修复前没有）。"""
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return mock.Mock(returncode=0)

        with mock.patch("interfacetester.make.subprocess.run", side_effect=fake_run):
            format_pytest_with_black("a_test.py")

        self.assertTrue(captured["kwargs"].get("check"))
        self.assertIsNotNone(captured["kwargs"].get("timeout"))
        self.assertGreater(captured["kwargs"]["timeout"], 0)
        # stdin 指向空设备，避免继承异常 stdin 时阻塞
        self.assertEqual(captured["kwargs"].get("stdin"), subprocess.DEVNULL)

    def test_black_child_env_redirects_cache_dir(self):
        """black 子进程的缓存目录默认被指到临时目录（避开不可写的用户缓存目录）。

        NOTICE: 实测 black 在缓存目录不可写时会**卡住**；而 `--no-cache` 只在新版 black
        才有（22.3~24.1 均无，本项目下限是 22.3），故改用所有版本都支持的 BLACK_CACHE_DIR。
        """
        with mock.patch.dict(os.environ):
            os.environ.pop(BLACK_CACHE_DIR_ENV, None)
            env = get_black_env()

            self.assertEqual(
                env[BLACK_CACHE_DIR_ENV],
                os.path.join(tempfile.gettempdir(), BLACK_CACHE_DIR_NAME),
            )

    def test_black_child_env_respects_user_cache_dir(self):
        """用户显式设置了 BLACK_CACHE_DIR 时不得覆盖。"""
        user_cache_dir = os.path.join(os.getcwd(), "custom-black-cache")
        with mock.patch.dict(os.environ, {BLACK_CACHE_DIR_ENV: user_cache_dir}):
            self.assertEqual(get_black_env()[BLACK_CACHE_DIR_ENV], user_cache_dir)

    def test_run_black_passes_cache_env_to_subprocess(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["kwargs"] = kwargs
            return mock.Mock(returncode=0)

        with mock.patch("interfacetester.make.subprocess.run", side_effect=fake_run):
            format_pytest_with_black("a_test.py")

        self.assertIn(BLACK_CACHE_DIR_ENV, captured["kwargs"]["env"])

    def test_black_timeout_is_swallowed(self):
        """black 超时 → 只告警、不抛异常、不阻断 hmake。"""
        timeout_error = subprocess.TimeoutExpired(cmd="black", timeout=1)

        with mock.patch(
            "interfacetester.make.subprocess.run", side_effect=timeout_error
        ):
            try:
                log_messages = self._capture_logs(
                    format_pytest_with_black, "a_test.py", "b_test.py"
                )
            except subprocess.TimeoutExpired:
                self.fail("black 超时不应向上抛出，否则 hmake 会被中断")

        self.assertTrue(
            any("超时" in message for message in log_messages), log_messages
        )

    def test_black_missing_tool_warns(self):
        """black 不存在 → 保留修复前的告警文案，不抛异常。"""
        with mock.patch(
            "interfacetester.make.subprocess.run", side_effect=OSError("no black")
        ):
            log_messages = self._capture_logs(format_pytest_with_black, "a_test.py")

        self.assertTrue(
            any("missing dependency tool: black" in message for message in log_messages),
            log_messages,
        )

    def test_failed_batch_degrades_to_per_file(self):
        """多文件一次调用失败 → 逐个文件重试（修复前会因列表推导而全不格式化）。"""
        calls = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            if len(calls) == 1:
                raise subprocess.CalledProcessError(returncode=1, cmd=command)
            return mock.Mock(returncode=0)

        with mock.patch("interfacetester.make.subprocess.run", side_effect=fake_run):
            format_pytest_with_black("a_test.py", "b_test.py", "c_test.py")

        self.assertEqual(calls[0][-3:], ["a_test.py", "b_test.py", "c_test.py"])
        self.assertEqual(
            [call[-1] for call in calls[1:]],
            ["a_test.py", "b_test.py", "c_test.py"],
        )

    def test_black_syntax_error_is_reported_per_file(self):
        """逐个文件重试时，某个文件失败只告警该文件，不影响其余文件。"""
        calls = []

        def fake_run(command, **kwargs):
            paths = command[-1:]
            calls.append(paths[0])
            if len(calls) == 1:
                raise subprocess.CalledProcessError(returncode=1, cmd=command)
            if paths[0] == "b_test.py":
                raise subprocess.CalledProcessError(returncode=123, cmd=command)
            return mock.Mock(returncode=0)

        with mock.patch("interfacetester.make.subprocess.run", side_effect=fake_run):
            log_messages = self._capture_logs(
                format_pytest_with_black, "a_test.py", "b_test.py", "c_test.py"
            )

        # 三个文件都被尝试过（修复前 b_test.py 失败会让 c_test.py 不再被格式化）
        self.assertEqual(calls, ["c_test.py", "a_test.py", "b_test.py", "c_test.py"])
        self.assertTrue(
            any("failed to format b_test.py" in message for message in log_messages),
            log_messages,
        )

    def test_no_paths_is_noop(self):
        """没有生成文件时不应调用 black。"""
        with mock.patch("interfacetester.make.subprocess.run") as mocked_run:
            format_pytest_with_black()

        mocked_run.assert_not_called()


class TestBlackTargetVersion(unittest.TestCase):
    """M12：显式传 `--target-version`，避免 black 从项目元数据推断出「无上界」的版本集合。

    ## 实测出来的真正触发条件

    black 26.5 的 `--target-version` 帮助写着「默认会从 pyproject.toml 的项目元数据推断
    target 版本」。实测（逐个变量隔离，见 `docs/缺陷修复日志0918-5.md`）触发需要**同时**满足：

    | 条件 | 结果 |
    |---|---|
    | 目录里有 pyproject.toml，但**没有** `.git` | 不报警告 |
    | 有 `.git` 但没有 pyproject.toml | 不报警告 |
    | **有 `.git` + 根上有 pyproject.toml** | **报警告** |

    本仓库两样都有，且 `requires-python = ">=3.8"` **没有上界** → black 推断出
    `py38..py315`（它支持的全部版本）→ 最大值 3.15 比运行解释器 3.12 新 → 每次都打印：

    ```text
    Warning: Python 3.12 cannot parse code formatted for Python 3.15. To fix this:
    run Black with Python 3.15, set --target-version to py312, or use --fast to skip ...
    ```

    ## 两个容易踩的坑（探针里都踩过）

    1. **警告只在 black「确实要改文件」时才出现**：已合规的文件走 "left unchanged"
       分支，black 根本不跑那次安全检查。所以复现它的文件必须是**需要重排**的。
    2. **在系统临时目录里复现不出来**：那里向上找不到 `.git`+pyproject.toml，
       black 就不会推断版本集合。必须在 git 检出目录里才看得到。
    """

    def test_target_version_defaults_to_project_floor(self):
        """默认传项目声明的最低支持版本（py38），而不是运行解释器的版本。

        理由（见 `docs/缺陷修复日志0918-5.md` §M12）：
        - `pyproject.toml` 声明 `requires-python = ">=3.8"`，且 black 的声明下限是
          `black>=22.3`——**22.3 的 TargetVersion 里就有 PY38**，所以这个值对所有被允许的
          black 版本都合法（不需要探测 black 支持哪些版本，也就不会随 black 升级漂移）；
        - 实测：对本仓库的生成物，`--target-version py38` 与 `py312` 的**输出逐字节相同**
          （生成物里没有会随 target 变化的结构），所以选哪个都不影响生成结果；
        - 选运行解释器版本需要在进程内 `import black` 读 `TargetVersion` 做能力探测，
          实测约 **105 ms**（单文件 hmake 全程约 690 ms，即 +15%），为消一条纯噪声不值得。
        """
        with mock.patch.dict(os.environ):
            os.environ.pop(BLACK_TARGET_VERSION_ENV, None)
            self.assertEqual(get_black_target_version(), "py38")

    def test_target_version_can_be_overridden_by_env(self):
        with mock.patch.dict(os.environ, {BLACK_TARGET_VERSION_ENV: "py311"}):
            self.assertEqual(get_black_target_version(), "py311")
            command = get_black_command("a_test.py")

        self.assertIn("--target-version", command)
        self.assertIn("py311", command)

    def test_target_version_can_be_disabled(self):
        """给个逃生口：显式关掉后回到 black 自己的推断（原来的行为）。"""
        for disabled in ("none", "", "off", "false", "0"):
            with self.subTest(disabled=disabled):
                with mock.patch.dict(
                    os.environ, {BLACK_TARGET_VERSION_ENV: disabled}
                ):
                    self.assertIsNone(get_black_target_version())
                    command = get_black_command("a_test.py")

                self.assertNotIn("--target-version", command)

    def test_black_command_puts_flags_before_paths(self):
        """格式：`black --target-version py38 <file>...`（选项在前，路径在后）。"""
        with mock.patch(
            "interfacetester.make.importlib.util.find_spec", return_value=object()
        ), mock.patch.dict(os.environ):
            os.environ.pop(BLACK_TARGET_VERSION_ENV, None)
            command = get_black_command("a_test.py", "b_test.py")

        self.assertEqual(
            command,
            [
                sys.executable,
                "-m",
                "black",
                "--target-version",
                "py38",
                "a_test.py",
                "b_test.py",
            ],
        )

    def test_end_to_end_black_output_has_no_inference_warning(self):
        """端到端 A/B：在本仓库里跑**真实** black，证明「警告确实在」且「修复消掉了它」。

        NOTICE: 这条**不 mock** subprocess——它验的正是「真实 black 在真实项目里还会不会
        打印那句警告」。前半段断言「不带 flag 会警告」用来排除**空跑**：该警告只在
        `有 .git + 根上有 pyproject.toml` 时才出现，纯临时目录里根本复现不出来，
        没有这半段，本用例在那种环境下会假装通过（所以那种环境下改成 skip 并说明原因）。
        """
        if importlib.util.find_spec("black") is None:
            self.skipTest("本环境没装 black")

        # 必须放在**仓库内**（black 才会向上找到 .git + pyproject.toml），
        # 且内容必须**需要重排**（已合规的文件不跑那次安全检查，不会触发警告）。
        workdir = _make_tmp_project_dir()
        self.addCleanup(shutil.rmtree, workdir, True)
        target = os.path.join(workdir, "case.py")
        needs_reformat = (
            "d={'a':1,'b':2,'c':3}\n"
            "def f(alpha,beta,gamma,delta,epsilon):"
            " return alpha+beta+gamma+delta+epsilon+1111111111\n"
        )

        def run_black_with(extra_args):
            with open(target, "w", encoding="utf-8") as f:
                f.write(needs_reformat)
            proc = subprocess.run(
                [sys.executable, "-m", "black", *extra_args, target],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=get_black_env(),
            )
            return proc.stdout + proc.stderr

        without_flag = run_black_with([])
        with_flag = run_black_with(["--target-version", get_black_target_version()])

        if "cannot parse code formatted for" not in without_flag:
            self.skipTest(
                "当前环境不触发 black 的版本推断警告（需要 .git + 根上带无上界 "
                "requires-python 的 pyproject.toml），本用例无法证明修复有效"
            )

        # 前半段：证明这个环境确实会复现 M12（否则后半段没有意义）
        self.assertIn("cannot parse code formatted for", without_flag)
        # 后半段：证明修复有效
        self.assertNotIn("cannot parse code formatted for", with_flag)

    def test_run_black_really_uses_the_flag(self):
        """`get_black_command()` 真的被 `run_black()` 用上（不是只在一个 helper 里拼字符串）。"""
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return mock.Mock(returncode=0)

        with mock.patch("interfacetester.make.subprocess.run", side_effect=fake_run):
            run_black(60, "a_test.py")

        self.assertIn("--target-version", captured["command"])
        self.assertIn(get_black_target_version(), captured["command"])


class TestEnsureKnownComparators(unittest.TestCase):
    """P0：`validate` 算子白名单校验（拼错在 hmake 阶段立即失败）。

    `StepRequestValidation` 用 `__getattr__` 动态分发自定义算子后，「算子名拼错」不再于
    生成/导入阶段暴露，所以必须在 make 阶段主动拦一次；白名单口径与运行期完全一致。
    """

    def test_builtin_comparator_names_are_derived_from_module(self):
        """内置算子名必须直接从 `builtin/comparators.py` 推导（避免手工清单漂移）。"""
        expected = {
            name
            for name, value in vars(builtin_comparators).items()
            if inspect.isfunction(value) and not name.startswith("_")
        }

        self.assertEqual(set(BUILTIN_COMPARATOR_NAMES), expected)
        # P2-b：18 → 19（新增 jsonschema_match）；A2-1：19 → 21（新增 xpath_match / xpath_count）；
        # A2-2：21 → 22（新增 xml_schema_match）
        self.assertEqual(len(BUILTIN_COMPARATOR_NAMES), 22)
        self.assertIn("jsonschema_match", BUILTIN_COMPARATOR_NAMES)
        self.assertIn("xpath_match", BUILTIN_COMPARATOR_NAMES)
        self.assertIn("xpath_count", BUILTIN_COMPARATOR_NAMES)
        self.assertIn("xml_schema_match", BUILTIN_COMPARATOR_NAMES)
        self.assertEqual(list(BUILTIN_COMPARATOR_NAMES), sorted(expected))

    def test_imported_functions_do_not_leak_into_whitelist(self):
        """白名单是「模块内的公开**函数**」自动派生的 → **import 进来的函数也会被算进去**。

        A2-2 实现时真实踩到：为了缓存 XSD 编译结果写了 `from functools import lru_cache`，
        算子数立刻从 22 变成 **23**（`lru_cache` 成了「内置算子」，`hmake` 会接受
        `validate: - lru_cache: [...]` 这种写法）。所以这里把「不许出现 import 进来的函数」钉住。
        """
        imported = {
            name
            for name, value in vars(builtin_comparators).items()
            if inspect.isfunction(value) and not name.startswith("_")
            and getattr(value, "__module__", "") != builtin_comparators.__name__
        }
        self.assertEqual(imported, set(), f"这些 import 进来的函数混进了算子白名单：{imported}")
        self.assertNotIn("lru_cache", BUILTIN_COMPARATOR_NAMES)

    def test_every_builtin_comparator_passes_whitelist(self):
        steps = [
            {
                "name": "all builtin comparators",
                "validate": [
                    {name: ["status_code", 200]} for name in BUILTIN_COMPARATOR_NAMES
                ],
            }
        ]

        ensure_known_comparators(steps, {}, "case.yml")  # 不抛异常即通过

    def test_short_aliases_pass_whitelist(self):
        """短别名（eq/lt/str_eq/len_eq…）要先被 uniform_validator 归一，再通过白名单。"""
        aliases = [
            "eq",
            "equals",
            "equal",
            "lt",
            "less_than",
            "le",
            "less_or_equals",
            "gt",
            "greater_than",
            "ge",
            "greater_or_equals",
            "ne",
            "not_equal",
            "str_eq",
            "string_equals",
            "len_eq",
            "length_equal",
            "len_gt",
            "length_greater_than",
            "len_ge",
            "length_greater_or_equals",
            "len_lt",
            "length_less_than",
            "len_le",
            "length_less_or_equals",
            "contains",
            "contained_by",
            "type_match",
            "startswith",
            "endswith",
            "regex_match",
        ]
        steps = [
            {
                "name": "aliases",
                "validate": [{alias: ["status_code", 200]} for alias in aliases],
            }
        ]

        ensure_known_comparators(steps, {}, "case.yml")

    def test_project_function_passes_whitelist(self):
        steps = [{"name": "custom", "validate": [{"my_cmp": ["status_code", 200]}]}]

        ensure_known_comparators(
            steps, {"my_cmp": lambda *args, **kwargs: True}, "case.yml"
        )

    def test_unknown_comparator_raises_with_context(self):
        steps = [
            {"name": "step with typo", "validate": [{"my_cmpp": ["status_code", 200]}]}
        ]

        with self.assertRaises(exceptions.FunctionNotFound) as ctx:
            ensure_known_comparators(steps, {}, "case_typo.yml")

        message = str(ctx.exception)
        self.assertIn("my_cmpp", message)
        self.assertIn("case_typo.yml", message)  # 报错要能定位用例
        self.assertIn("step with typo", message)  # 以及步骤
        self.assertIn("equal", message)  # 以及可用算子
        self.assertIn("debugtalk.py", message)  # 以及怎么补

    def test_empty_inputs_are_noop(self):
        ensure_known_comparators(None, {}, "case.yml")
        ensure_known_comparators([], {}, "case.yml")
        ensure_known_comparators([{"name": "no validate"}], None, "case.yml")
        ensure_known_comparators([{"name": "empty", "validate": []}], None, "case.yml")

    # ===== 批次 9-1：形态非法的 validator 必须**带定位**地在生成期报错 =====

    def test_malformed_validator_is_refused_with_context(self):
        r"""形态非法的 `validate` 条目：pydantic 放行、运行期才 error —— 这里补成生成期报错。

        `TStep.validators` 的类型是 `List[Dict]`，所以下面这些都**能通过模型校验**：

        ```yaml
        validate:
          - {eq: ["status_code", 200], contains: ["body", "x"]}   # 双键
          - {eq: "notalist"}                                      # 值不是列表
          - {eq: ["status_code"]}                                 # 只有 1 个元素
        ```

        修复前它们一路走到运行期 `ResponseObject.validate` → `uniform_validator` 抛
        `ParamsError: invalid validator`：消息里**没有用例/步骤**，而且 pytest 记的是
        **error 而不是 failed**（同 `docs/缺陷修复日志0917-1.md` 的口径）。
        两个兄弟闸门（`ensure_json_schema_not_inline` / `ensure_xsd_files_exist`）对形态非法的
        条目是 `continue`（那不是它们的关注点），本函数却**必须**报 —— 它是最后一道闸门，
        且没有「别的校验」会报这个错。所以这里断言的是**带定位的报错**，而不是 `continue`。
        """
        malformed = [
            {"eq": ["status_code", 200], "contains": ["body", "x"]},
            {"eq": "notalist"},
            {"eq": ["status_code"]},
            {"eq": ["status_code", 200, "msg", "extra"]},
        ]
        for raw_validator in malformed:
            with self.subTest(validator=raw_validator):
                steps = [{"name": "bad shape", "validate": [raw_validator]}]

                with self.assertRaises(exceptions.ParamsError) as ctx:
                    ensure_known_comparators(steps, {}, "case_shape.yml")

                message = str(ctx.exception)
                self.assertIn("case_shape.yml", message, "报错要能定位用例")
                self.assertIn("bad shape", message, "报错要能定位步骤")
                self.assertIn("invalid validator", message, "要保留底层原因")

    def test_malformed_validator_does_not_mask_the_unknown_comparator_gate(self):
        """顺序护栏：形态合法但算子未知时，报的仍然必须是「未知算子」。"""
        steps = [{"name": "typo", "validate": [{"my_cmpp": ["status_code", 200]}]}]

        with self.assertRaises(exceptions.FunctionNotFound):
            ensure_known_comparators(steps, {}, "case.yml")


class TestBatch91ModuleSegmentRule(unittest.TestCase):
    r"""批次 9-1：`make.normalize_module_segment` 是「源路径 → 生成物模块名」的**唯一**规则。

    这条规则原本内联在 `ensure_file_abs_path_valid` 里，而「两个源用例会不会落到同一个
    `*_test.py`」在 `cli.main_convert` 里被**另一条**规则（`emit_yaml.slugify`）重新推导过一次
    —— 同一个源目录两套名字，冲突只在更晚的阶段才被发现。现在抽成纯函数，两处共用。

    本类钉两件事：
      1. **逐字节不变**：34 个已入库生成物的文件名/类名都由它派生（见
         `tests/generated_artifacts_drift_test.py`），抽函数不许改行为；
      2. **幂等**：护栏要对同一个源名判两次（本用例 + 已占用者），不幂等就会误报。
    """

    def test_segment_rule_is_byte_compatible_with_the_inline_version(self):
        cases = {
            # 普通名：原样
            "case": "case",
            "request_with_variables": "request_with_variables",
            # 空格 / 点 / 连字符 → `_`
            "user admin": "user_admin",
            "a.b": "a_b",
            "a-b": "a_b",
            # 数字开头补 `T`（19 => T19）
            "19": "T19",
            "2c": "T2c",
            # `.` 开头的段**不动**（避免 `.csv` 被折成 `_csv`）
            ".csv": ".csv",
            ".hidden": ".hidden",
            # 组合
            "1.2.3 report": "T1_2_3_report",
            # 空段（重复分隔符切出来的）由调用方跳过
            "": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_module_segment(raw), expected)

    def test_segment_rule_is_idempotent(self):
        for raw in ("user admin", "a-b", "19", "1.2.3 report", ".csv", "查询 用户-管理"):
            with self.subTest(raw=raw):
                once = normalize_module_segment(raw)
                self.assertEqual(normalize_module_segment(once), once)

    def test_collision_pairs_are_exactly_the_ones_the_rule_folds(self):
        """三层的归一集合**互不相同**（这是 `cli.main_convert` 三层护栏的存在理由）。

        本用例把三个函数的边界钉在一起：同一对名字在 `.yml` 名 / schema 词干两层**不冲突**，
        在模块名这层**冲突** —— 所以模块名护栏不是重复建设。
        """
        from interfacetester.converters.emit_yaml import slugify
        from interfacetester.converters.ir import safe_file_stem

        a, b = "用户-管理", "用户 管理"

        self.assertNotEqual(slugify(a), slugify(b), "`.yml` 名不同（`-` 不被 slugify 归一）")
        self.assertNotEqual(
            safe_file_stem(a), safe_file_stem(b), "schema 词干不同（safe_file_stem 保留 `-`）"
        )
        self.assertEqual(
            normalize_module_segment(a),
            normalize_module_segment(b),
            "模块名相同 —— 必须由模块名护栏拦下（同 `tests/converters_test.py` 的端到端用例）",
        )

    def test_generated_module_name_is_a_valid_python_identifier(self):
        r"""护栏之所以存在：模块名会成为 `import` 目标与类名的一部分，必须是合法标识符。

        NOTICE（批次 B / **M1** 补强）：这条用例原先只扫**5 个精选名字**
        （`用户 管理` / `a-b` / `19` / `1.2.3 report` / `查询`），而它们恰好都是
        「折空格/点/连字符」这条规则**能折干净**的字符 —— 于是护栏全绿，
        而另外 20 个可见 ASCII 字符（`+ @ & ' ( ) ! % , ; = [ ] ^ \` { } ~ $ #`）
        会让模块段不是标识符。现在改成**全 ASCII 扫描**，并把关键字一起挡住。
        """
        import keyword
        import string as string_module

        raw_samples = [
            "用户 管理",
            "a-b",
            "19",
            "1.2.3 report",
            "查询",
        ]
        # 全 ASCII 可见字符逐个塞进名字中间（33~126 覆盖 ! " # $ … { | } ~）
        raw_samples += [f"case{chr(code)}1" for code in range(33, 127)]

        for raw in raw_samples:
            with self.subTest(raw=raw):
                segment = normalize_module_segment(raw)
                self.assertTrue(
                    segment.isidentifier(), f"{segment!r} 不是合法 Python 标识符"
                )
                self.assertFalse(
                    keyword.iskeyword(segment),
                    f"{segment!r} 是 Python 关键字，不能做模块段"
                    f"（`from {segment}.x_test import …` 是语法错误）",
                )

        # 关键字形态单独钉一遍（它们本身是合法标识符，但**不能**做模块段）
        for raw in ("class", "import", "for", "return", "None"):
            with self.subTest(keyword_sample=raw):
                segment = normalize_module_segment(raw)
                self.assertFalse(keyword.iskeyword(segment))
                self.assertTrue(segment.isidentifier())

        # 反向护栏：规则本来就能折干净的段**逐字节不变**（34 个已入库生成物由它派生）
        self.assertEqual(normalize_module_segment("a-b.c"), "a_b_c")
        self.assertEqual(normalize_module_segment("2 3"), "T2_3")
        self.assertEqual(normalize_module_segment("19"), "T19")
        self.assertEqual(normalize_module_segment(".csv"), ".csv")

    def test_non_identifier_case_name_generates_compilable_code(self):
        """批次 B / M1 端到端：文件名含 `+` 时，生成物必须**可编译**。

        修复前实测（`.tmp_audit/verify_claims.py`）：`hmake case+1.yml` **退出码 0**，
        产物第一行类名是 `class TestCaseCase+1(InterfaceTester):` → `SyntaxError`，
        直到 `hrun` 才报 `collected 0 items / 1 error`。
        现在文件名被归一成 `case_1.yml` → `case_1_test.py` / `class TestCaseCase1`，
        并且落盘前还有一道 `compile()` 兜底。
        """
        tmp_dir = _make_tmp_project_dir()
        try:
            with open(
                os.path.join(tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
            ) as f:
                f.write("")
            case_path = os.path.join(tmp_dir, "case+1.yml")
            with open(case_path, "w", encoding="utf-8") as f:
                f.write(
                    "config:\n"
                    "    name: m1\n"
                    "    base_url: http://127.0.0.1:1\n"
                    "teststeps:\n"
                    "    - name: s\n"
                    "      request:\n"
                    "          method: GET\n"
                    "          url: /ok\n"
                    "      validate:\n"
                    '          - eq: ["status_code", 200]\n'
                )

            loader.reset_project_meta()
            content = loader.load_test_file(case_path)
            # 与 `make.__make` 同一口径：用例路径由 `config.path` 带入
            content.setdefault("config", {})["path"] = case_path

            generated = make_testcase(content)
            self.assertTrue(os.path.isfile(generated), generated)
            self.assertTrue(
                os.path.basename(generated) == "case_1_test.py",
                f"生成物文件名没有归一：{os.path.basename(generated)}",
            )

            with open(generated, encoding="utf-8") as f:
                source = f.read()

            # 关键：生成物必须能编译（`+` 没有泄漏进类名/模块名）
            compile(source, generated, "exec")
            self.assertNotIn("TestCaseCase+1", source)
            self.assertIn(
                "class TestCaseCase1(",
                source,
                f"类名没有按新规则归一：{[l for l in source.splitlines() if l.startswith('class ')]}",
            )
        finally:
            loader.reset_project_meta()
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_referenced_case_in_non_identifier_dir_generates_compilable_import(self):
        """M1 的另一半：**目录名**含 `@` 时，`testcase:` 生成的 import 也必须可编译。

        修复前生成的是 `from my@dir.leaf_test import TestCaseLeaf as Leaf` → 整个文件
        `SyntaxError`（实测）。
        """
        tmp_dir = _make_tmp_project_dir()
        try:
            with open(
                os.path.join(tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
            ) as f:
                f.write("")
            sub_dir = os.path.join(tmp_dir, "my@dir")
            os.makedirs(sub_dir, exist_ok=True)
            leaf_path = os.path.join(sub_dir, "leaf.yml")
            with open(leaf_path, "w", encoding="utf-8") as f:
                f.write(
                    "config:\n"
                    "    name: leaf\n"
                    "    base_url: http://127.0.0.1:1\n"
                    "teststeps:\n"
                    "    - name: s\n"
                    "      request:\n"
                    "          method: GET\n"
                    "          url: /ok\n"
                    "      validate:\n"
                    '          - eq: ["status_code", 200]\n'
                )
            root_path = os.path.join(tmp_dir, "root.yml")
            with open(root_path, "w", encoding="utf-8") as f:
                f.write(
                    "config:\n"
                    "    name: root\n"
                    "    base_url: http://127.0.0.1:1\n"
                    "teststeps:\n"
                    "    - name: ref\n"
                    "      testcase: my@dir/leaf.yml\n"
                )

            loader.reset_project_meta()
            content = loader.load_test_file(root_path)
            content.setdefault("config", {})["path"] = root_path

            generated = make_testcase(content)
            with open(generated, encoding="utf-8") as f:
                source = f.read()

            compile(source, generated, "exec")  # 不抛 SyntaxError 即通过
            self.assertIn("from my_dir.leaf_test import", source)
            self.assertNotIn("my@dir", source)
        finally:
            loader.reset_project_meta()
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_generated_python_validator_rejects_syntax_errors(self):
        """批次 B / M1 兜底：`ensure_generated_python_is_valid` 把语法错误变成可读报错。

        这是"最后一道无损检查"——它不关心语义，只保证写出去的 `*_test.py` 一定能 import。
        （修复前 `hmake` 对生成物没有任何语法校验，black 的解析失败也只降级成 WARNING。）
        """
        make_module.ensure_generated_python_is_valid("x = 1\n", "case_test.py")

        with self.assertRaises(exceptions.ParamsError) as ctx:
            make_module.ensure_generated_python_is_valid(
                "class TestCaseCase+1:\n    pass\n", "case+1_test.py"
            )

        message = str(ctx.exception)
        self.assertIn("不是合法 Python", message)
        self.assertIn("阻止落盘", message)
        # 报错要给出可操作的方向（源文件名里的非法字符）
        self.assertIn("文件名/目录名", message)

    # ===== 0918-1（H3）：算子名**不得**回落到 Python 内置函数 =====

    def test_python_builtin_names_are_rejected(self):
        """H3 回归：拼错成 Python 内置名必须报错，**不能**放行。

        修复前 `ensure_known_comparators` 复用了 `parser.get_mapping_function` 的完整解析
        顺序，最后一级会回落到 Python 内置函数，于是 `print` 通过了白名单、生成
        `.assert_print(...)`；而运行期算子被调用为
        `assert_func(check_value, expect_value, message)` 且返回值被忽略，
        `print` 恰好可变参 —— 实测判为 **pass**（假通过）。
        """
        for builtin_name in ("print", "len", "max", "type", "id", "sum"):
            with self.subTest(comparator=builtin_name):
                steps = [
                    {
                        "name": "builtin name as operator",
                        "validate": [{builtin_name: ["status_code", 1]}],
                    }
                ]
                with self.assertRaises(exceptions.FunctionNotFound) as ctx:
                    ensure_known_comparators(steps, {}, "builtin_op.yml")

                message = str(ctx.exception)
                self.assertIn(builtin_name, message)
                self.assertIn("Python 内置函数", message)  # 讲清真实原因
                self.assertIn("debugtalk.py", message)  # 以及怎么改

    def test_framework_helper_functions_are_rejected(self):
        """框架内置的**工具函数**（`builtin/functions.py`）不是断言算子，同样要拦。

        `builtin/__init__.py` 用 `import *` 把 `comparators` 与 `functions` 摊在同一个
        命名空间里，`get_timestamp`/`sleep` 这类名字因此能被解析到——它们当算子用是
        误用（参数个数也不对），必须报错而不是放行。
        """
        for helper_name in ("get_timestamp", "get_current_date", "sleep"):
            with self.subTest(comparator=helper_name):
                steps = [
                    {
                        "name": "helper as operator",
                        "validate": [{helper_name: ["status_code", 1]}],
                    }
                ]
                with self.assertRaises(exceptions.FunctionNotFound):
                    ensure_known_comparators(steps, {}, "helper_op.yml")

    def test_debugtalk_function_still_wins_over_builtin_name(self):
        """项目自己在 debugtalk.py 里定义的同名函数优先（口径不能被收紧改坏）。"""
        steps = [{"name": "shadow", "validate": [{"print": ["status_code", 1]}]}]

        # 项目把 print 定义成合法算子 → 应当放行
        ensure_known_comparators(steps, {"print": lambda *a, **k: None}, "case.yml")


class TestEnsureGeneratableTeststeps(unittest.TestCase):
    """0918-1：**字段可生成性**交叉校验（H2 / M11 的统一闸门）。

    这一类 bug 的模式是固定的：字段在 `models.TStep` 里有定义、在白名单里被认（因此
    **不告警**），但 `make.py` 没有对应分支 → **静默丢弃**。
    历史实例：`validate_script` 被丢弃后 `examples/httpbin/validate.yml` 里
    `assert status_code == 201`（故意要失败的断言）在生成物中消失，用例反而"通过"。

    处置原则：宁可 fail fast 报错，也不静默丢弃。
    """

    def test_validate_script_is_rejected_with_replacement_hint(self):
        """H2 回归：`validate_script` 必须报错，且要告诉用户替代写法。"""
        steps = [
            {
                "name": "step with validate_script",
                "request": {"method": "GET", "url": "/get"},
                "validate_script": ["assert status_code == 200"],
            }
        ]

        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_teststeps(steps, "case.yml")

        message = str(ctx.exception)
        self.assertIn("validate_script", message)
        self.assertIn("静默丢弃", message)  # 说清修复前的危害
        self.assertIn("debugtalk.py", message)  # 替代写法 1/2 都要给
        self.assertIn("case.yml", message)  # 能定位用例
        self.assertIn("step with validate_script", message)  # 以及步骤

    def test_validate_script_empty_is_not_rejected(self):
        """空列表/None 不算「写了这个字段」，不该拦（避免误伤模板里的占位）。"""
        ensure_generatable_teststeps(
            [
                {
                    "name": "empty validate_script",
                    "request": {"method": "GET", "url": "/get"},
                    "validate_script": [],
                }
            ],
            "case.yml",
        )

    def test_sql_and_thrift_request_are_rejected(self):
        """SQL/Thrift step 只能手写 pytest 用例，写在 YAML 里必须明确报错。

        修复前 compat 先抛 `Invalid teststep: {...}`（看不出是「SQL 不支持」还是
        「YAML 写错了」），容易让人以为是格式问题。
        """
        for field_name in ("sql_request", "thrift_request"):
            with self.subTest(field=field_name):
                steps = [
                    {
                        "name": f"step with {field_name}",
                        field_name: {"method": "FETCHALL", "sql": "select 1"},
                    }
                ]
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    ensure_generatable_teststeps(steps, "case.yml")

                message = str(ctx.exception)
                self.assertIn(field_name, message)
                self.assertIn("手写", message)  # 给出出路

    def test_step_without_request_or_testcase_is_rejected(self):
        steps = [{"name": "empty step", "variables": {"a": 1}}]

        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_teststeps(steps, "case.yml")

        message = str(ctx.exception)
        self.assertIn("既没有 `request` 也没有 `testcase`", message)
        self.assertIn("生成器支持的 step 字段", message)

    def test_export_on_request_step_is_rejected(self):
        """`export` 写在请求 step 上会生成 `.export(...)` —— `RequestWithOptionalArgs`
        没有这个方法，失败点是 pytest 收集阶段（整个文件 import 失败）。
        """
        steps = [
            {
                "name": "request step with export",
                "request": {"method": "GET", "url": "/get"},
                "export": ["session_foo"],
            }
        ]

        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_teststeps(steps, "case.yml")

        message = str(ctx.exception)
        self.assertIn("`export` 只能写在", message)
        self.assertIn("config.export", message)  # 给出正确写法

    def test_extract_or_validate_on_testcase_step_is_rejected(self):
        """`extract`/`validate` 写在引用用例的 step 上同样会 import 期 AttributeError。"""
        for field_name in ("extract", "validate"):
            with self.subTest(field=field_name):
                step = {"name": "ref step", "testcase": "some_ref"}
                step[field_name] = (
                    {"var": "body.a"} if field_name == "extract" else [{"eq": [1, 1]}]
                )
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    ensure_generatable_teststeps([step], "case.yml")

                message = str(ctx.exception)
                self.assertIn(field_name, message)
                self.assertIn("只能写在**请求**", message)

    def test_valid_step_shapes_are_accepted(self):
        """正常写法不能被误伤（互斥的两种 step 类型各自合法）。"""
        steps = [
            {
                "name": "plain request step",
                "request": {"method": "GET", "url": "/get"},
                "variables": {"a": 1},
                "extract": {"v": "body.args.a"},
                "validate": [{"eq": ["status_code", 200]}],
                "validators": [{"eq": ["status_code", 200]}],  # 模型字段名也算
                "retry_times": 1,
                "retry_interval": 0,
                "setup_hooks": ["${hook()}"],
                "teardown_hooks": ["${hook()}"],
            },
            {
                "name": "reference testcase step",
                "testcase": "./ref.yml",
                "variables": {"a": 1},
                "export": ["v"],
            },
        ]

        ensure_generatable_teststeps(steps, "case.yml")  # 不抛异常即通过

    def test_request_and_testcase_coexist_is_rejected(self):
        """批次 B / M2：`request` 与 `testcase` 并存 **从「容忍」改成「报错」**。

        NOTICE（这是一次**刻意的行为收紧**，护栏在这里被改写而不是被绕过）：
        修复前本类有一条断言「同时写 request 与 testcase」的历史形态**必须被接受**
        （理由写的是"compat 刻意容忍、生成器按 request 判定"）。但那个"容忍"的实际后果
        不是"按 request 处理"，而是**整条引用用例被静默丢弃**：

        - `compat._ensure_step_attachment` 是按白名单**重建** step 的，且**没有
          `testcase` 分支**；`testcase` 只由 `ensure_testcase_v4` 的
          `elif "testcase" in step:` 写入，而那个分支前面是 `if "request" in step: pass`；
        - 于是两个键并存时 `testcase` 从未进入重建后的 step，生成器又按 `request` 优先。

        实测（真 CLI）：`hmake` exit 0，生成物里 **`RunTestCase` 一个字都没有**，
        被引用接口一次都没被请求，父用例照绿 —— 零告警的**假通过**
        （两个键都是已知字段，连未知字段告警都不响）。

        所以现在：这两个键并存 = 响亮报错 + 给出「拆成两个 step」的改法。
        """
        steps = [
            {
                "name": "both shapes",
                "request": {"method": "GET", "url": "/get"},
                "testcase": "CLS_LB(TestCaseDemo)CLS_RB",
                "extract": {"v": "body.args.a"},
            }
        ]

        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_teststeps(steps, "case.yml")

        message = str(ctx.exception)
        self.assertIn("request", message)
        self.assertIn("testcase", message)
        self.assertIn("静默丢弃", message)
        # 报错必须给出可操作的改法
        self.assertIn("拆成两个 step", message)

    def test_both_shapes_via_v2_v3_api_key_is_rejected_too(self):
        """v2/v3 的 `api:` 与 `request` 并存同样报错（`api` 会被 compat 转成 `testcase`）。"""
        steps = [
            {
                "name": "v3 both shapes",
                "request": {"method": "GET", "url": "/get"},
                "api": "api/get.yml",
            }
        ]

        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_teststeps(steps, "case.yml")

        self.assertIn("api", str(ctx.exception))

    def test_v2_v3_api_step_is_not_mistaken_for_invalid(self):
        """v2/v3 的 `api:` 写法必须被当成合法指针（由 compat 转成 `testcase`）。

        NOTICE: 本校验刻意跑在 `ensure_testcase_v4` **之前**（要看到用户原始写法），
        所以必须自己认得 `api` —— 否则会把合法的 v3 用例误判成「无效 step」。
        """
        steps = [{"name": "v3 api step", "api": "api/get.yml", "variables": {"a": 1}}]

        ensure_generatable_teststeps(steps, "v3_case.yml")  # 不抛异常即通过

    def test_api_step_with_extract_is_rejected(self):
        """`api` 步转换成 `testcase` 步后没有 `extract` 方法，属于同一个「位置写错」问题。"""
        steps = [
            {"name": "v3 api step", "api": "api/get.yml", "extract": {"v": "body.a"}}
        ]

        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_teststeps(steps, "v3_case.yml")

        self.assertIn("只能写在**请求**", str(ctx.exception))

    def test_missing_config_does_not_crash(self):
        """`config` 缺失/非法时不能在校验里抛 AttributeError（交给后续校验去报格式错）。"""
        ensure_generatable_teststeps(
            [{"name": "s", "request": {"method": "GET", "url": "/"}}], ""
        )

    def test_empty_inputs_are_noop(self):
        """`None` / 非 dict step 仍按"没有可校验的内容"处理（不炸）。"""
        ensure_generatable_teststeps(None, "case.yml")
        # 非 dict 的 step（理论上不该出现）跳过而不是炸在生成期
        ensure_generatable_teststeps(["not a dict"], "case.yml")

    def test_explicitly_empty_teststeps_is_rejected(self):
        """0920 批次 5 / **N33**：**显式空列表**必须报错（不再是 no-op）。

        NOTICE（订正）：本条原先把 `[]` 和 `None` 放在一起当 "noop"。
        但「显式给了空 teststeps」生成出来的用例是**零步骤且判成功**：
        `hmake` exit 0、`hrun` **1 passed**、`summary.success=true` 而 `records` 是 0 条
        —— 也就是「绿了但什么都没测」，与 H2 对"空参数集"立的口径直接冲突。

        `None` 仍然放行：那种用例由"既没有 request 也没有 testcase"等既有闸门处理。
        """
        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_teststeps([], "case.yml")

        message = str(ctx.exception)
        self.assertIn("空列表", message)
        self.assertIn("什么都没测", message)
        self.assertIn("config.skip", message, "要给出替代写法")


class TestStepFieldGeneratability(unittest.TestCase):
    """防复发不变量：**凡是模型认的 step 字段，生成器要么能生成，要么必须报错。**

    这是 0918-1 批次真正要钉住的东西。H2（`validate_script`）与 M11
    （`retry_times`/`retry_interval`）不是两个独立 bug，而是同一个模式的两个出口：
    「模型/白名单认了，生成器不认，且不告警」。逐条打补丁只能治标，
    所以这里用不变量把「下一次再犯」也拦住。
    """

    def test_no_gap_between_known_fields_and_generator_coverage(self):
        """`compat.STEP_KNOWN_FIELDS` 里的每个字段都必须被「覆盖」或「显式拒绝」。"""
        # `api` 是 v2/v3 的历史字段名，由调用方（`ensure_testcase_v4_api` /
        # `ensure_testcase_v4`）在进入 `make_testcase` **之前**就转换掉，
        # 不属于 v4 生成器的职责范围，所以它出现在 STEP_KNOWN_FIELDS 里是正常的。
        structural_or_legacy_fields = {"api"}

        gaps = []
        for field_name in sorted(compat.STEP_KNOWN_FIELDS):
            if field_name in structural_or_legacy_fields:
                continue
            if field_name in GENERATOR_SUPPORTED_STEP_FIELDS:
                continue
            if field_name in UNSUPPORTED_STEP_FIELDS:
                continue
            gaps.append(field_name)

        self.assertEqual(
            gaps,
            [],
            f"这些字段在 compat 的已知清单里，但生成器既不支持也不拒绝 → 会被静默丢弃：{gaps}",
        )

    def test_step_known_fields_covers_the_model(self):
        """compat 的已知清单必须**覆盖**模型字段（漂移会导致静默丢弃，不是少告警）。

        修复前这里差了 5 个字段（validators / retry_times / retry_interval /
        sql_request / thrift_request），而 `_ensure_step_attachment` 是按该清单**重建**
        step 的 —— 差的字段会被直接删掉。
        """
        model_fields = set(TStep.model_fields)

        self.assertEqual(
            model_fields - compat.STEP_KNOWN_FIELDS,
            set(),
            "模型字段没有全部进入 compat 已知清单，会被静默丢弃",
        )

    def test_every_rejected_field_is_a_real_model_field(self):
        """反向校验：`UNSUPPORTED_STEP_FIELDS` 与生成器支持清单不能凭空写错名字。"""
        # YAML 别名：`validate` 是 `TStep.validators` 的 YAML 写法，
        # 生成器（和 `_ensure_step_attachment`）统一用 `validate` 这个名字。
        field_aliases = {"validate", "validators"}

        for field_name in UNSUPPORTED_STEP_FIELDS:
            self.assertIn(
                field_name,
                set(TStep.model_fields),
                f"{field_name} 不是 TStep 的字段，UNSUPPORTED_STEP_FIELDS 写错了名字",
            )

        for field_name in GENERATOR_SUPPORTED_STEP_FIELDS:
            if field_name in field_aliases or field_name == "name":
                continue
            self.assertIn(
                field_name,
                set(TStep.model_fields),
                f"{field_name} 不是 TStep 的字段，GENERATOR_SUPPORTED_STEP_FIELDS 写错了名字",
            )

    def test_generator_supported_fields_have_a_render_branch(self):
        """生成器支持清单里的每个字段，都必须真的出现在生成函数的源码里（防手写漂移）。"""
        source = inspect.getsource(make_teststep_chain_style)

        for field_name in GENERATOR_SUPPORTED_STEP_FIELDS:
            if field_name == "name":
                # name 走 teststep["name"] 作为 RunRequest/RunTestCase 的第一个参数，
                # 不是 `xxx in teststep` 形式的分支
                continue
            if field_name == "validators":
                # `validators` 由 compat 归一成 `validate`（见 _ensure_step_attachment），
                # 生成器只需认 `validate` 一个名字
                continue
            self.assertIn(
                field_name,
                source,
                f"{field_name} 被列进 GENERATOR_SUPPORTED_STEP_FIELDS，"
                f"但 make_teststep_chain_style 里找不到它",
            )

    def test_validators_alias_is_normalized_by_compat(self):
        """`validators` 必须被 compat 归一成 `validate`（生成器只认后者）。"""
        step = compat._ensure_step_attachment(
            {
                "name": "alias",
                "request": {"method": "GET", "url": "/get"},
                "validators": [{"eq": ["status_code", 200]}],
            }
        )

        self.assertIn("validate", step)
        self.assertNotIn("validators", step)
        # format2 的形态是 {算子名: [check, expect, message?]}（`_convert_validators` 不改键名）
        self.assertIn("eq", step["validate"][0])


class TestConfigFieldGeneratability(unittest.TestCase):
    """0918-8 / L15（§六.1 的另一半）：**config 层**也要有「模型认了 → 生成器必须能表达或拒绝」。

    修复前只有 step 层有这道闸门（`ensure_generatable_teststeps`），config 层是空的，
    于是 `config.thrift` / `config.db` 属于「模型认、`KNOWN_CONFIG_FIELDS` 认、生成器不渲染、
    零告警」——用户配了 thrift/数据库连接，hmake 绿着通过，生成物里什么都没有。
    """

    def _config_case(self, **config_extra):
        config = {"name": "config gate"}
        config.update(config_extra)
        return {
            "config": config,
            "teststeps": [
                {"name": "s", "request": {"method": "GET", "url": "/x"}}
            ],
        }

    def _make_config_yaml(self, name: str, **config_extra) -> str:
        """在临时项目里写一个带指定 config 的 YAML，返回其路径。

        NOTICE: 走 `main_make`（CLI 同一条路径）而不是直接调 `make_testcase`——
        这样断言的正是用户看得见的行为：**报错 + 退出码 1 + 不留下生成物**。
        """
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.addCleanup(pytest_files_made_cache_mapping.clear)
        self.addCleanup(pytest_files_run_set.clear)

        tmp_dir = _make_tmp_project_dir()
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        with open(os.path.join(tmp_dir, "debugtalk.py"), "w", encoding="utf-8") as f:
            f.write("")

        config = {"name": name, "base_url": HTTP_BIN_URL}
        config.update(config_extra)
        yml_path = os.path.join(tmp_dir, f"{name}.yml")
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "config": config,
                    "teststeps": [
                        {
                            "name": "step",
                            "request": {"method": "GET", "url": "/get"},
                            "validate": [{"eq": ["status_code", 200]}],
                        }
                    ],
                },
                f,
                allow_unicode=True,
            )
        return yml_path

    def _make_and_capture_error(self, yml_path: str) -> str:
        """跑 hmake 并把日志里的报错文本抓出来（`main_make` 会把失败汇总后 sys.exit(1)）。"""
        messages = []
        sink_id = logger.add(messages.append, level="ERROR", format="{message}")
        try:
            with self.assertRaises(SystemExit) as ctx:
                main_make([yml_path])
        finally:
            logger.remove(sink_id)

        self.assertEqual(ctx.exception.code, 1)
        return "\n".join(str(message) for message in messages)

    def test_config_thrift_is_rejected_with_guidance(self):
        yml_path = self._make_config_yaml(
            "config_thrift_gate", thrift={"service_name": "Svc"}
        )

        message = self._make_and_capture_error(yml_path)

        self.assertIn("config.thrift", message)
        self.assertIn("不会生效", message)
        # 必须给出可执行的替代写法，而不是只说"不支持"
        self.assertIn("RunThriftRequest", message)
        self.assertFalse(
            os.path.exists(
                os.path.join(os.path.dirname(yml_path), "config_thrift_gate_test.py")
            ),
            "被拒绝的用例不应留下生成物",
        )

    def test_config_db_is_rejected_with_guidance(self):
        yml_path = self._make_config_yaml("config_db_gate", db={"ip": "10.0.0.1"})

        message = self._make_and_capture_error(yml_path)

        self.assertIn("config.db", message)
        self.assertIn("RunSqlRequest", message)

    def test_empty_config_blocks_are_not_rejected(self):
        """`thrift: {}` / `db: null` 这种"写了但没内容"不报错（判据是取值非空）。"""
        ensure_generatable_config({"name": "x", "thrift": None, "db": {}}, "x.yml")

    def test_supported_config_fields_are_not_rejected(self):
        """反向断言：真支持的 config 字段照常生成（这条闸门不能误伤）。"""
        yml_path = self._make_config_yaml(
            "supported_config",
            variables={"a": 1},
            verify=False,
            timeout=5,
            export=["a"],
        )

        main_make([yml_path])

        with open(
            os.path.join(os.path.dirname(yml_path), "supported_config_test.py"),
            encoding="utf-8",
        ) as f:
            generated = f.read()

        self.assertIn('Config("supported_config")', generated)
        self.assertIn(".variables(", generated)
        self.assertIn(".export(", generated)

    def test_oauth2_unknown_sub_field_is_rejected(self):
        """批次 B / M10：`config.oauth2` 的**嵌套子字段**也要校验（修复前没人看）。

        pydantic 的 `extra="ignore"` 会把不认识的子键**静默丢掉**，最典型的后果是
        **键名拼错**（`clientid` / `tokenurl`）→ 生成物里少一个 `.client_id(...)`，
        运行期只剩一句「oauth2 配置不完整，跳过自动认证」，用例带着**没有认证**的请求继续跑。
        """
        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_config(
                {
                    "name": "x",
                    "oauth2": {
                        "token_url": "http://h/token",
                        "clientid": "typo",  # 少了下划线
                        "client_secret": "s",
                    },
                },
                "x.yml",
            )

        message = str(ctx.exception)
        self.assertIn("clientid", message)
        self.assertIn("静默忽略", message)
        # 报错要列出支持的字段，便于对照改正
        self.assertIn("client_id", message)

    def test_oauth2_known_sub_fields_are_accepted(self):
        """反向护栏：合法子字段（含批次 B 起真正生效的 `grant_type`）不得误报。"""
        ensure_generatable_config(
            {
                "name": "x",
                "oauth2": {
                    "token_url": "http://h/token",
                    "client_id": "id",
                    "client_secret": "secret",
                    "scope": "s",
                    "grant_type": "client_credentials",
                },
            },
            "x.yml",
        )

    def test_oauth2_grant_type_is_emitted(self):
        """批次 B / M10：`grant_type` 必须**真的渲染**进生成物（修复前被静默忽略）。

        修复前实测：YAML 写 `grant_type: password`，生成物里只有
        `.oauth2().token_url(...).client_id(...).client_secret(...)` —— 一个 `grant_type`
        都没有，而运行期 `runner.get_oauth2_token` **真的会读**这个字段，
        于是实际发的是 `client_credentials`。若 token 端点同时接受两种授权，
        用例会带着**错误的授权方式**绿着通过。
        """
        chain = make_config_chain_style(
            {
                "name": "x",
                "variables": {},
                "oauth2": {
                    "token_url": "http://h/token",
                    "client_id": "id",
                    "client_secret": "secret",
                    "grant_type": "password",
                },
            }
        )

        self.assertIn('.grant_type("password")', chain)

    def test_oauth2_without_grant_type_keeps_generated_output_unchanged(self):
        """反向护栏：没写 `grant_type` 时生成物**逐字不变**（既有用例零漂移）。"""
        chain = make_config_chain_style(
            {
                "name": "x",
                "variables": {},
                "oauth2": {
                    "token_url": "http://h/token",
                    "client_id": "id",
                    "client_secret": "secret",
                },
            }
        )

        self.assertNotIn("grant_type", chain)
        self.assertTrue(chain.endswith('.client_secret("secret")'), chain)

    def test_oauth2_missing_required_field_gives_readable_error(self):
        """批次 B / M10 连带：漏写 `client_id` 时不得再抛裸 `KeyError`。

        修复前 `make_config_chain_style` 用下标取 `oauth2['client_id']`，而模型的默认值
        **不会**回写进原始 dict → `hmake` 抛 `KeyError: 'client_id'`（报错里既没有用例名，
        也不说该补什么）。
        """
        with self.assertRaises(exceptions.ParamsError) as ctx:
            make_config_chain_style(
                {
                    "name": "x",
                    "variables": {},
                    "oauth2": {"token_url": "http://h/token"},
                }
            )

        message = str(ctx.exception)
        self.assertIn("client_id", message)
        self.assertIn("缺少必填字段", message)

    def test_no_gap_between_known_config_fields_and_generator(self):
        """`KNOWN_CONFIG_FIELDS` 的每个字段都必须「被支持」或「被拒绝」。"""
        gaps = []
        for field_name in sorted(KNOWN_CONFIG_FIELDS):
            if field_name in GENERATOR_SUPPORTED_CONFIG_FIELDS:
                continue
            if field_name in UNSUPPORTED_CONFIG_FIELDS:
                continue
            if field_name in STRUCTURAL_CONFIG_FIELDS:
                continue
            gaps.append(field_name)

        self.assertEqual(
            gaps,
            [],
            f"这些 config 字段在已知清单里，但生成器既不支持也不拒绝 → 会被静默忽略：{gaps}",
        )

    def test_every_rejected_config_field_is_a_real_model_field(self):
        for field_name in UNSUPPORTED_CONFIG_FIELDS:
            self.assertIn(
                field_name,
                set(TConfig.model_fields),
                f"{field_name} 不是 TConfig 的字段，UNSUPPORTED_CONFIG_FIELDS 写错了名字",
            )

        for field_name in GENERATOR_SUPPORTED_CONFIG_FIELDS:
            if field_name == "skip":
                # skip 由 hmake 消费（`make_config_skip`），模型里没有它（见 KNOWN_CONFIG_FIELDS）
                continue
            self.assertIn(
                field_name,
                set(TConfig.model_fields),
                f"{field_name} 不是 TConfig 的字段，GENERATOR_SUPPORTED_CONFIG_FIELDS 写错了名字",
            )

    def test_generator_supported_config_fields_have_a_render_branch(self):
        """支持清单里的每个字段都必须真的能被渲染（防手写漂移）。"""
        chain_source = inspect.getsource(make_config_chain_style)
        skip_source = inspect.getsource(make_config_skip)

        for field_name in GENERATOR_SUPPORTED_CONFIG_FIELDS:
            if field_name == "skip":
                self.assertIn("skip", skip_source)
                continue
            if field_name == "parameters":
                # parameters 由 jinja 模板渲染（不是链式调用），用**功能断言**验证：
                # 配了 parameters 就必须真的生成 @pytest.mark.parametrize
                self.assertIn("parameters", inspect.getsource(make_module))
                continue
            self.assertIn(
                field_name,
                chain_source,
                f"{field_name} 被列进 GENERATOR_SUPPORTED_CONFIG_FIELDS，"
                f"但 make_config_chain_style 里找不到它",
            )


def _read_file(path: str) -> str:
    """读文本文件（生成物断言用）。"""
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestBatch0920HandwrittenGeneratedFileProtection(unittest.TestCase):
    """0920 批次 6 / **N40**：`hmake` 不得**静默覆盖**手写的 `*_test.py`。

    ## 修复前的现场（`.tmp_audit/n40_check.py`）

    `make_testcase` 无条件 `open(..., "w")`：手写文件被覆盖，**无告警、无备份、exit 0**。

    ```text
    覆盖前: # hand-written, NOT generated\\ndef test_important(): assert True
    覆盖后: # NOTE: Generated By InterfaceTester 4.3.5\\n# FROM: …
    ```

    ## 判据（本仓自己给的）

    `conftest.py` 早就有同样的保护（`compat.py` 里写着"修复前会直接覆盖，
    用户手写的 fixture 会永久丢失"），而 `*_test.py` 是**更容易被手写**的文件名。

    这里锁住三个方向：手写的**不许**覆盖、生成过的**必须**能覆盖（重跑）、
    不存在的照常生成。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.tmp_dir = _make_tmp_project_dir()
        with open(
            os.path.join(self.tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")
        self.yml_path = os.path.join(self.tmp_dir, "case.yml")
        with open(self.yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "config": {"name": "handwritten probe", "base_url": HTTP_BIN_URL},
                    "teststeps": [
                        {
                            "name": "s",
                            "request": {"method": "GET", "url": "/status/200"},
                            "validate": [{"eq": ["status_code", 200]}],
                        }
                    ],
                },
                f,
                allow_unicode=True,
            )
        self.generated = os.path.join(self.tmp_dir, "case_test.py")

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_handwritten_file_is_not_overwritten(self):
        """手写文件必须原样保住，且 `hmake` 报错（不可逆的覆盖不能静默发生）。"""
        handwritten = "# hand-written, NOT generated\ndef test_important():\n    assert True\n"
        with open(self.generated, "w", encoding="utf-8") as f:
            f.write(handwritten)

        with self.assertRaises(exceptions.ParamsError) as ctx:
            make_module.make_testcase(
                {
                    "config": {
                        "name": "handwritten probe",
                        "base_url": HTTP_BIN_URL,
                        "path": self.yml_path,
                    },
                    "teststeps": [
                        {
                            "name": "s",
                            "request": {"method": "GET", "url": "/status/200"},
                        }
                    ],
                }
            )

        with open(self.generated, encoding="utf-8") as f:
            self.assertEqual(f.read(), handwritten, "手写文件被覆盖了")

        message = str(ctx.exception)
        self.assertIn("手写", message)
        self.assertIn("永久丢失", message)
        # 要给出改法
        self.assertIn("hmake", message)

    def test_regenerating_our_own_output_is_allowed(self):
        """反向护栏：带生成标记的文件必须能**正常重跑覆盖**（否则 hmake 就没法用了）。

        NOTICE: 这里只断言"**不抛异常**"，不断言"内容变了"——
        `make_testcase` 有一条按路径的**生成缓存**（`pytest_files_made_cache_mapping`），
        同一路径第二次调用会被缓存短路（既有行为，与本批无关）。
        要验证"真的重写"，走 `main_make` 那条路（见下面的用例）。
        """
        make_module.make_testcase(
            {
                "config": {
                    "name": "handwritten probe",
                    "base_url": HTTP_BIN_URL,
                    "path": self.yml_path,
                },
                "teststeps": [
                    {"name": "s", "request": {"method": "GET", "url": "/status/200"}}
                ],
            }
        )
        first = _read_file(self.generated)
        self.assertIn("Generated By InterfaceTester", first)

        # 第二次：不应报错（这正是修复前会**静默覆盖**、修复后被误伤的那个场景）
        make_module.make_testcase(
            {
                "config": {
                    "name": "handwritten probe",
                    "base_url": HTTP_BIN_URL,
                    "path": self.yml_path,
                },
                "teststeps": [
                    {"name": "s2", "request": {"method": "GET", "url": "/status/201"}}
                ],
            }
        )
        self.assertIn("Generated By InterfaceTester", _read_file(self.generated))

    def test_main_make_rewrites_its_own_output(self):
        """端到端（走 `main_make`，不经过单次调用缓存）：改成 YAML 后生成物必须更新。"""
        main_make([self.yml_path])
        self.assertIn('"/status/200"', _read_file(self.generated))

        # NOTICE: `main_make` 有**进程级生成缓存**（`pytest_files_made_cache_mapping`），
        # 同一路径第二次调用会被缓存短路 —— 所以两次调用之间必须清掉它，
        # 否则测的就不是"重跑"而是"缓存命中"（本文件其它多趟用例同样这么做）。
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()

        with open(self.yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "config": {"name": "handwritten probe", "base_url": HTTP_BIN_URL},
                    "teststeps": [
                        {
                            "name": "s",
                            "request": {"method": "GET", "url": "/status/201"},
                            "validate": [{"eq": ["status_code", 201]}],
                        }
                    ],
                },
                f,
                allow_unicode=True,
            )

        main_make([self.yml_path])

        self.assertIn(
            '"/status/201"',
            _read_file(self.generated),
            "重跑没有更新生成物（生成标记的文件应当允许覆盖）",
        )

    def test_missing_file_is_generated_normally(self):
        """反向护栏：目标不存在时照常生成。"""
        self.assertFalse(os.path.exists(self.generated))

        make_module.make_testcase(
            {
                "config": {
                    "name": "handwritten probe",
                    "base_url": HTTP_BIN_URL,
                    "path": self.yml_path,
                },
                "teststeps": [
                    {"name": "s", "request": {"method": "GET", "url": "/status/200"}}
                ],
            }
        )

        self.assertTrue(os.path.exists(self.generated))

    def test_empty_file_is_treated_as_handwritten(self):
        """空文件没有生成标记 → 同样受保护（宁可让用户确认，也不静默覆盖）。"""
        open(self.generated, "w", encoding="utf-8").close()

        with self.assertRaises(exceptions.ParamsError):
            make_module.make_testcase(
                {
                    "config": {
                        "name": "handwritten probe",
                        "base_url": HTTP_BIN_URL,
                        "path": self.yml_path,
                    },
                    "teststeps": [
                        {"name": "s", "request": {"method": "GET", "url": "/status/200"}}
                    ],
                }
            )

    def _make_testcase(self):
        make_module.make_testcase(
            {
                "config": {
                    "name": "handwritten probe",
                    "base_url": HTTP_BIN_URL,
                    "path": self.yml_path,
                },
                "teststeps": [
                    {"name": "s", "request": {"method": "GET", "url": "/status/200"}}
                ],
            }
        )

    def test_handwritten_file_merely_mentioning_the_marker_is_still_protected(self):
        """**核心**（0921-2 / 发现 3）：判据必须是**首行**匹配，不是子串匹配。

        修复前判据是 `"Generated By InterfaceTester" in head`（前 400 字节子串），
        于是手写文件只要在开头**提到**这句话就会被放行覆盖 —— 那正是要防的事，
        而 `compat.is_generated_conftest` 的 docstring 早就把这条理由写透了
        （"用户完全可能在文档字符串里提到这句话…那正好是要防的事"）。

        这里逐一钉住三种"提到但不是我生成的"形态。
        """
        handwritten_variants = [
            # 注释里提到
            "# my notes: this file was overwritten by Generated By InterfaceTester\n"
            "def test_important(): assert True\n",
            # docstring 里提到（compat.py 举的例子）
            '"""Ported from a file marked Generated By InterfaceTester."""\n'
            "def test_x(): assert True\n",
            # 第二行才提到（旧判据看前 400 字节，照样放行）
            "# hand-written, NOT generated\n"
            "# see docs about Generated By InterfaceTester behaviour\n"
            "def test_y(): assert True\n",
        ]

        for handwritten in handwritten_variants:
            with self.subTest(first_line=handwritten.splitlines()[0][:40]):
                with open(self.generated, "w", encoding="utf-8") as f:
                    f.write(handwritten)
                pytest_files_made_cache_mapping.clear()
                pytest_files_run_set.clear()

                with self.assertRaises(exceptions.ParamsError):
                    self._make_testcase()

                with open(self.generated, encoding="utf-8") as f:
                    self.assertEqual(
                        f.read(), handwritten, "手写文件被覆盖了（子串判据的旧行为）"
                    )

    def test_real_generated_output_still_regenerates(self):
        """反向护栏：真生成物（首行就是标记）必须照旧放行，收紧不能误伤正常重跑。"""
        with open(self.generated, "w", encoding="utf-8") as f:
            f.write(
                f"{make_module.GENERATED_FILE_MARKER_PREFIX} 4.3.5\n# FROM: case.yml\n"
            )
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()

        # 不抛异常即为通过
        self._make_testcase()

    def test_marker_prefix_is_the_template_first_line(self):
        """判据自检：常量必须真的是生成物第一行的前缀（否则判据会永久失效）。

        NOTICE: 不用 `__TEMPLATE__.source`（jinja2 的 `Template` 没有这个属性，
        实测 `hasattr(...) is False`）—— 直接拿**真实生成物**对账更可靠：
        本用例先生成一个文件，再断言它的首行满足判据常量。
        这样某天有人改了模板第一行却忘了同步常量时，这里立刻变红
        （否则所有生成物都会被判成"手写"而无法重跑）。
        """
        self._make_testcase()

        with open(self.generated, encoding="utf-8") as f:
            first_line = f.readline().strip()

        self.assertTrue(
            first_line.startswith(make_module.GENERATED_FILE_MARKER_PREFIX),
            f"真实生成物首行 {first_line!r} 不以 "
            f"{make_module.GENERATED_FILE_MARKER_PREFIX!r} 开头 —— "
            f"判据常量与模板已漂移，hmake 将无法重跑自己的生成物",
        )

    def test_every_yaml_backed_example_artifact_matches_the_criterion(self):
        """全仓对账：**有源 YAML** 的已入库生成物，首行必须都满足新判据。

        这是"收紧判据不会挡住正常重跑"的事实依据（修复时实测 29 个全通过）。
        刻意排除**没有源 YAML** 的手写文件（`examples/.../04_sql_data_management_test.py`
        等）：`hmake` 根本不会以它们为目标，属于设计内。
        """
        examples_dir = os.path.join(_REPO_ROOT, "examples")
        self.assertTrue(os.path.isdir(examples_dir), examples_dir)

        checked = 0
        violations = []
        for dirpath, dirnames, filenames in os.walk(examples_dir):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for filename in filenames:
                if not filename.endswith("_test.py"):
                    continue
                path = os.path.join(dirpath, filename)
                stem = path[: -len("_test.py")]
                if not any(
                    os.path.isfile(stem + ext) for ext in (".yml", ".yaml")
                ):
                    continue  # 手写用例：没有源 YAML，hmake 不会碰它
                checked += 1
                with open(path, encoding="utf-8", errors="replace") as f:
                    first_line = f.readline().strip()
                if not first_line.startswith(make_module.GENERATED_FILE_MARKER_PREFIX):
                    violations.append(
                        f"{os.path.relpath(path, _REPO_ROOT)}: {first_line[:60]!r}"
                    )

        self.assertGreaterEqual(checked, 20, f"只检查到 {checked} 个生成物，判据可能失效")
        self.assertEqual(
            violations,
            [],
            "这些**有 YAML** 的生成物首行不是生成标记，收紧后 hmake 将无法重跑它们：\n  "
            + "\n  ".join(violations),
        )


class TestBatch0920ParametersStringForm(unittest.TestCase):
    """0920 批次 6 / **N34**：`parameters: ${func()}` 必须在**生成期**求值。

    ## 修复前的现场（`.tmp_audit/n34_check.py`）

    `models.py` 里 `parameters: Union[VariablesMapping, Text]` **允许**写字符串，
    而生成器把它当字面量渲染：

    ```python
    @pytest.mark.parametrize("param", Parameters("${get_params()}"))   ← 一个字符串！
    ```

    `hmake` **exit 0**（生成物语法合法），运行期/收集期才炸：

    ```text
    AttributeError: 'str' object has no attribute 'items'
    collected 0 items / 1 error
    ```

    **对照组**：`variables: ${get_variables()}` 会被 `convert_variables` 正确求值成
    `.variables(**{"who": "world"})` —— 同一份配置里两个兄弟字段口径不一致，
    属"模型允许、生成器不认"的老毛病（与 M25/M11 同族）。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.tmp_dir = _make_tmp_project_dir()
        with open(
            os.path.join(self.tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write(
                "def get_params():\n    return {'uid': [1, 2]}\n"
                "def get_variables():\n    return {'who': 'world'}\n"
            )

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_and_make(self, name: str, config_extra: str) -> str:
        yml_path = os.path.join(self.tmp_dir, f"{name}.yml")
        with open(yml_path, "w", encoding="utf-8") as f:
            f.write(
                f"config:\n  name: {name}\n  base_url: {HTTP_BIN_URL}\n"
                f"{config_extra}"
                "teststeps:\n"
                "- name: s\n"
                "  request:\n"
                "    method: GET\n"
                "    url: /status/200\n"
                "  validate:\n"
                "  - eq: [status_code, 200]\n"
            )
        main_make([yml_path])
        return os.path.join(self.tmp_dir, f"{name}_test.py")

    def test_string_form_parameters_are_resolved_at_generation_time(self):
        """字符串形态必须被求值成**真实数据**，不能原样渲染成字符串字面量。"""
        generated = _read_file(
            self._write_and_make("params_str", "  parameters: ${get_params()}\n")
        )

        self.assertNotIn(
            'Parameters("${get_params()}")',
            generated,
            "参数被当字面量渲染了（N34）：运行期会 'str' has no attribute 'items'",
        )
        self.assertIn("Parameters({", generated, generated)
        self.assertIn('"uid"', generated)

    def test_generated_case_is_collectable(self):
        """端到端判据：生成物必须**收集得到用例**（修复前 collected 0 items / 1 error）。"""
        generated = self._write_and_make(
            "params_collect", "  parameters: ${get_params()}\n"
        )

        spec = importlib.util.spec_from_file_location(
            f"gen_params_{uuid.uuid4().hex[:6]}", generated
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # 修复前这里/收集期抛 AttributeError

        # 参数化应当展开成两组（uid 有 2 个值）
        self.assertEqual(len(module.TestCaseParamsCollect.teststeps), 1)

    def test_variables_string_form_still_works(self):
        """反向护栏：`variables` 的字符串形态（既有能力）不受影响。"""
        generated = _read_file(
            self._write_and_make("vars_str", "  variables: ${get_variables()}\n")
        )

        self.assertIn(".variables(**{", generated)
        self.assertIn('"who"', generated)

    def test_dict_and_list_forms_are_unchanged(self):
        """反向护栏：dict / list 形态的参数化一字不变（不许被求值逻辑改坏）。"""
        generated = _read_file(
            self._write_and_make("params_dict", "  parameters: {uid: [1, 2]}\n")
        )
        self.assertIn("Parameters({", generated)
        self.assertIn("[1, 2]", generated)

    def test_no_parameters_means_no_parametrize(self):
        """反向护栏：没配 `parameters` 时不许凭空生出 `parametrize`。"""
        generated = _read_file(self._write_and_make("no_params", ""))

        self.assertNotIn("Parameters(", generated)
        self.assertNotIn("parametrize", generated)


class TestRequestFieldGeneratability(unittest.TestCase):
    """0918-8（§六.1）：**request 层**同样不能有「模型认了、生成器不认、还不告警」的空隙。

    M25（`req_json` 被静默丢弃）就是这一层的实例：`KNOWN_REQUEST_FIELDS` 里有它、
    known-fields 告警把它列为"已支持"，而 `make_request_chain_style` 当时只认 `json`
    → 请求**没有 body** 而 hmake exit 0。这里把「request 字段可生成性」钉成不变量。
    """

    def test_no_gap_between_known_request_fields_and_generator(self):
        source = inspect.getsource(make_request_chain_style)
        gaps = [
            field_name
            for field_name in sorted(KNOWN_REQUEST_FIELDS)
            if field_name not in source
        ]

        self.assertEqual(
            gaps,
            [],
            f"这些 request 字段在已知清单里，但生成器源码里找不到 → 可能被静默丢弃：{gaps}",
        )

    def test_rendered_request_fields_are_real_model_fields(self):
        """生成器里出现的每个 request 字段名，都必须是真字段（防止清单/源码漂移）。"""
        for field_name in sorted(KNOWN_REQUEST_FIELDS):
            if field_name == "json":
                continue  # `json` 是 `req_json` 的别名
            self.assertIn(field_name, set(TRequest.model_fields))


class TestRequestBodyAmbiguity(unittest.TestCase):
    """批次 B / **M6**：`data` 与 `json` 同时出现必须报错（requests 会静默丢掉 json）。

    ## 修复前的现场（`.tmp_audit/kernel/probe_data_and_json.py`，服务端视角）

    ```yaml
    request:
      method: POST
      url: /echo
      data: "a=1&b=2"          # 用户写了
      json: {from_json: true}  # 用户也写了
    ```

    ```text
    服务端实际收到 Content-Type = None
    服务端实际收到 body         = 'a=1&b=2'
    （json 体的 {'from_json': True} 从未出现在请求里；hmake 与运行期都零告警）
    ```

    根因在 requests 自己身上（`PreparedRequest.prepare_body`）：

        if not data and json is not None:      # ← data 为真时整个 json 分支**不执行**

    两个键都是 `TRequest` 的正式字段（未知字段告警不响），生成器也照渲染
    `.with_data(...)` + `.with_json(...)` —— 于是"请求体与 YAML 里写的不一致"这件事
    **没有任何信号**。判据完全对齐 requests 的实际语义，避免假报：
    `data` 真值 + `json` 非 None 才算冲突。
    """

    def _step(self, request: dict) -> list:
        return [{"name": "s", "request": request}]

    def test_data_and_json_together_is_rejected(self):
        steps = self._step(
            {
                "method": "POST",
                "url": "/echo",
                "data": "a=1&b=2",
                "json": {"from_json": True},
            }
        )

        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_teststeps(steps, "case.yml")

        message = str(ctx.exception)
        self.assertIn("data", message)
        self.assertIn("json", message)
        self.assertIn("静默丢弃", message)
        self.assertIn("params", message)  # hint：查询串要用 params

    def test_req_json_field_name_is_covered_too(self):
        """`json` 的字段名是 `req_json`（`populate_by_name=True` 两个都收）——两个写法都要拦。"""
        steps = self._step(
            {
                "method": "POST",
                "url": "/echo",
                "data": "a=1",
                "req_json": {"k": "v"},
            }
        )

        with self.assertRaises(exceptions.ParamsError):
            ensure_generatable_teststeps(steps, "case.yml")

    def test_each_alone_is_accepted(self):
        """反向护栏：单独用 `data` 或单独用 `json` 都合法（不许误伤）。"""
        for request in (
            {"method": "POST", "url": "/echo", "data": "a=1&b=2"},
            {"method": "POST", "url": "/echo", "json": {"a": 1}},
            {"method": "POST", "url": "/echo", "data": "a=1", "json": None},
            # `data` 为空（`{}` / `""`）时 requests 走的是 json 分支 —— 不算冲突
            {"method": "POST", "url": "/echo", "data": {}, "json": {"a": 1}},
            {"method": "POST", "url": "/echo", "data": "", "json": {"a": 1}},
            {"method": "GET", "url": "/echo", "params": {"a": 1}},
        ):
            with self.subTest(request=request):
                ensure_generatable_teststeps(self._step(request), "case.yml")


class TestUploadBodyAmbiguity(unittest.TestCase):
    """0920 / **缺陷 1b**：`upload` 与 `data` / `json` 并存必须报错（静默丢体）。

    ## 修复前的现场（`.tmp_audit/repro_d1.py` + `.tmp_audit/probe_json_upload.py`）

    ```yaml
    request:
      method: POST
      url: /post
      data: {note: hello}      # 用户写了普通字段
      upload: {file: a.txt}    # 用户也写了文件
    ```

    ```text
    # 服务端实际收到的 multipart 体里只有 file —— note 从未出现；hmake 与运行期零告警
    # json+upload 同理：upload 机制把 data 换成 $m_encoder 后，
    #   requests 的 `if not data and json is not None` 走假分支 → json 从不出现
    ```

    与 M6（`data`+`json`）同族：都是"模型认、生成器认、运行期**静默丢掉一半**"。
    区别是这里的覆盖者是 `ext/uploader.prepare_upload_step()` 自己。
    """

    def _step(self, request: dict) -> list:
        return [{"name": "s", "request": request}]

    def test_data_and_upload_together_is_rejected(self):
        steps = self._step(
            {
                "method": "POST",
                "url": "/upload",
                "data": {"note": "hello"},
                "upload": {"file": "a.txt"},
            }
        )

        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_teststeps(steps, "case.yml")

        message = str(ctx.exception)
        self.assertIn("upload", message)
        self.assertIn("data", message)
        self.assertIn("multipart", message)

    def test_json_and_upload_together_is_rejected(self):
        """N1：`json`+`upload` 是同一个根因的另一个出口（文档 §1b 原本漏了它）。"""
        steps = self._step(
            {
                "method": "POST",
                "url": "/upload",
                "json": {"a": 1},
                "upload": {"file": "a.txt"},
            }
        )

        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_teststeps(steps, "case.yml")

        self.assertIn("json", str(ctx.exception))

    def test_req_json_field_name_is_covered_too(self):
        steps = self._step(
            {
                "method": "POST",
                "url": "/upload",
                "req_json": {"a": 1},
                "upload": {"file": "a.txt"},
            }
        )

        with self.assertRaises(exceptions.ParamsError):
            ensure_generatable_teststeps(steps, "case.yml")

    def test_upload_alone_and_data_alone_are_accepted(self):
        """反向护栏：只有 `upload`（正常上传）或只有 `data`（普通表单）都不许误伤。"""
        for request in (
            {"method": "POST", "url": "/upload", "upload": {"file": "a.txt"}},
            {"method": "POST", "url": "/post", "data": {"a": 1}},
            {"method": "POST", "url": "/post", "data": "raw-body"},
            {"method": "POST", "url": "/post", "json": {"a": 1}},
            # `upload` 为空 = 没写（uploader 自己会 return），不算并存
            {"method": "POST", "url": "/post", "data": {"a": 1}, "upload": {}},
            # `data` 为空值时 requests 的判据同样是"真值" → 放行
            {"method": "POST", "url": "/upload", "data": "", "upload": {"file": "a.txt"}},
            {"method": "POST", "url": "/upload", "data": {}, "upload": {"file": "a.txt"}},
            # `json: null`（显式空）不是请求体
            {"method": "POST", "url": "/upload", "json": None, "upload": {"file": "a.txt"}},
        ):
            with self.subTest(request=request):
                ensure_generatable_teststeps(self._step(request), "case.yml")

    def test_error_message_teaches_the_two_ways_out(self):
        """报错必须给出改法（本仓口径：错误信息要能让用户自己修）。"""
        steps = self._step(
            {
                "method": "POST",
                "url": "/upload",
                "data": {"note": "hello"},
                "upload": {"file": "a.txt"},
            }
        )

        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_generatable_teststeps(steps, "case.yml")

        message = str(ctx.exception)
        self.assertIn("upload:", message)  # 改法 1：普通字段写进 upload
        self.assertIn("删掉", message)


class TestMakeRetryEmit(unittest.TestCase):
    """M11 回归：`retry_times`/`retry_interval` 必须真的生成 `.with_retry(...)`。

    修复前这两个字段有**两层**丢失：
      ① `compat._ensure_step_attachment` 按白名单重建 step，而它们不在白名单里 → 在此被删；
      ② 即便活下来，`make_teststep_chain_style` 也没有 retry 分支。
    于是 YAML 里写重试**完全不生效**（只得到一句「无法识别的字段」的告警），
    而 `docs/能力清单.md` 把它标为 ✅、`runner.__run_step` 也真在消费它。
    """

    def test_retry_is_emitted_before_get(self):
        step = {
            "name": "retry step",
            "request": {"method": "GET", "url": "/x"},
            "retry_times": 3,
            "retry_interval": 2,
        }

        chain = make_teststep_chain_style(step)

        self.assertIn(".with_retry(retry_times=3, retry_interval=2)", chain)
        # NOTICE: 顺序必须在 `.get(...)` 之前 —— `with_retry` 定义在 RunRequest 上并返回自身，
        # `.get()` 之后拿到的是 RequestWithOptionalArgs，那时已经没有这个方法了。
        self.assertLess(
            chain.index(".with_retry("),
            chain.index(".get("),
            "`.with_retry` 必须在 `.get(url)` 之前，否则生成的代码 import 期就 AttributeError",
        )
        ast.parse(chain)  # 生成物必须是合法 Python

    def test_retry_is_emitted_before_call_for_testcase_step(self):
        step = {
            "name": "retry ref step",
            "testcase": "SomeRefCase",
            "retry_times": 2,
            "retry_interval": 1,
        }

        chain = make_teststep_chain_style(step)

        self.assertIn(".with_retry(retry_times=2, retry_interval=1)", chain)
        self.assertLess(chain.index(".with_retry("), chain.index(".call("))
        ast.parse(chain)

    def test_no_retry_configured_emits_nothing(self):
        step = {"name": "no retry", "request": {"method": "GET", "url": "/x"}}

        chain = make_teststep_chain_style(step)

        self.assertNotIn(".with_retry", chain)

    def test_zero_retry_times_emits_nothing(self):
        """`retry_times: 0` 是默认值，不该给所有用例加噪声。"""
        step = {
            "name": "zero retry",
            "request": {"method": "GET", "url": "/x"},
            "retry_times": 0,
            "retry_interval": 5,
        }

        self.assertNotIn(".with_retry", make_teststep_chain_style(step))

    def test_retry_interval_defaults_to_zero(self):
        """只写 `retry_times` 时，`retry_interval` 补 0（不能生成 `None`）。"""
        step = {
            "name": "retry without interval",
            "request": {"method": "GET", "url": "/x"},
            "retry_times": 2,
        }

        chain = make_teststep_chain_style(step)

        self.assertIn(".with_retry(retry_times=2, retry_interval=0)", chain)


class TestBatch0918EndToEnd(unittest.TestCase):
    """0918-1 批次的端到端回归：走完 `YAML → hmake → 生成文件可 import`。"""

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.tmp_dir = _make_tmp_project_dir()

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_case(self, name: str, teststeps) -> str:
        yml_path = os.path.join(self.tmp_dir, f"{name}.yml")
        testcase = {
            "config": {"name": name, "base_url": HTTP_BIN_URL},
            "teststeps": teststeps,
        }
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)
        return yml_path

    def test_retry_survives_compat_and_lands_in_generated_file(self):
        """M11 端到端：retry 必须穿过 compat 的重建，出现在生成文件里且可 import。"""
        yml_path = self._write_case(
            "retry_e2e",
            [
                {
                    "name": "retry step",
                    "request": {"method": "GET", "url": "/get"},
                    "retry_times": 3,
                    "retry_interval": 2,
                    "validate": [{"eq": ["status_code", 200]}],
                }
            ],
        )

        generated_list = main_make([yml_path])

        self.assertEqual(len(generated_list), 1)
        generated_path = generated_list[0]
        with open(generated_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn(".with_retry(retry_times=3, retry_interval=2)", content)

        # 运行期真的能读到（不只是文本里有）
        module = _import_python_file(generated_path, "retry_e2e_test")
        step = module.TestCaseRetryE2E.teststeps[0]
        self.assertEqual(step.retry_times, 3)
        self.assertEqual(step.retry_interval, 2)

    def test_validate_script_fails_at_make_stage_and_writes_nothing(self):
        """H2 端到端：报错 + 不留下半成品生成文件（与算子白名单同一处置）。"""
        yml_path = self._write_case(
            "script_assert",
            [
                {
                    "name": "script step",
                    "request": {"method": "GET", "url": "/get"},
                    "validate_script": ["assert status_code == 200"],
                }
            ],
        )

        with self.assertRaises(SystemExit) as ctx:
            main_make([yml_path])

        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp_dir, "script_assert_test.py"))
        )

    def test_validators_alias_is_generated(self):
        """`validators`（模型字段名）与 `validate`（YAML 别名）应当等价。

        修复前 compat 只认 `validate`，写 `validators` 会被重建逻辑丢弃
        （而它恰恰是 `TStep` 的正式字段名，pydantic 会接受）→ 断言静默消失。
        """
        yml_path = self._write_case(
            "validators_alias",
            [
                {
                    "name": "alias step",
                    "request": {"method": "GET", "url": "/get"},
                    "validators": [{"eq": ["status_code", 200]}],
                }
            ],
        )

        generated_list = main_make([yml_path])

        self.assertEqual(len(generated_list), 1)
        with open(generated_list[0], encoding="utf-8") as f:
            content = f.read()
        self.assertIn('.assert_equal("status_code", 200)', content)


class TestEnsureJsonSchemaNotInline(unittest.TestCase):
    """P2-b：内联 JSON Schema 要在 hmake 阶段被拦住（否则运行期 `VariableNotFound`）。

    `parser.parse_data` 会解析 dict 的 key，而 JSON Schema 必带的 `$schema`/`$ref` 都是 key
    → 内联必然在运行期炸，且报错完全看不出原因。这里要求生成前就给「两种正确写法」。
    """

    def test_inline_schema_with_dollar_keys_is_rejected(self):
        steps = [
            {
                "name": "step with inline schema",
                "validate": [
                    {
                        "jsonschema_match": [
                            "body",
                            {"$schema": "https://json-schema.org/draft/2020-12/schema"},
                        ]
                    }
                ],
            }
        ]

        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_json_schema_not_inline(steps, "case.yml")

        message = str(ctx.exception)
        self.assertIn("不支持内联 schema", message)
        self.assertIn("case.yml", message)
        self.assertIn("step with inline schema", message)
        self.assertIn("$schema", message)  # 指出是哪个键出的问题
        self.assertIn("${get_user_schema()}", message)  # 给出写法 1
        self.assertIn("schemas/user.json", message)  # 给出写法 2

    def test_nested_dollar_ref_is_rejected(self):
        steps = [
            {
                "name": "nested $ref",
                "validate": [
                    {
                        "jsonschema_match": [
                            "body",
                            {"properties": {"data": {"$ref": "#/$defs/data"}}},
                        ]
                    }
                ],
            }
        ]
        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_json_schema_not_inline(steps, "case.yml")
        self.assertIn("$ref", str(ctx.exception))

    def test_plain_inline_schema_is_allowed(self):
        """不含 `$` 键的极简内联 schema 是能跑的，不能拦。"""
        steps = [
            {
                "name": "simple inline schema",
                "validate": [
                    {"jsonschema_match": ["body", {"type": "object", "required": ["code"]}]}
                ],
            }
        ]
        ensure_json_schema_not_inline(steps, "case.yml")  # 不抛异常即通过

    # 能表明「说的是 `$` 键那条判据」的措辞（用它把正确文档与笼统结论区分开）
    _DOLLAR_KEY_MARKERS = ("$schema", "$ref", "`$`", "$ 键", "$ 开头", "$ 前缀")

    @classmethod
    def _is_unqualified_claim(cls, line: str) -> bool:
        """这一行是否属于「无限定的『不能内联』结论」。

        判据：同一行里既说「不能内联」、又给出「文件路径」这类替代方案，
        却**完全不提 `$` 键**这条真正的判据。

        NOTICE: 不能简单地用 `"$" in line` 判断——修复前的原句是
        ``schema 走 `${func()}` 或文件路径（**不能内联**）``，里面的 `${func()}` 也含 `$`。
        必须找「`$` 键」这类**判据性**措辞，否则本用例会漏判（空跑）。
        """
        if "不能内联" not in line or "文件路径" not in line:
            return False
        return not any(marker in line for marker in cls._DOLLAR_KEY_MARKERS)

    def test_docs_match_implementation_about_inline_schema(self):
        """M14：面向用户的文档不得再把「不能内联」写成无限定的笼统结论。

        实现**刻意**只拦「内联 dict + 含 `$` 前缀键」（那种会被 `parse_data` 当变量解析、
        运行期抛 `VariableNotFound`），不含 `$` 键的极简内联 schema 是能跑的、**刻意放行**
        （见 ``ensure_json_schema_not_inline`` 的 docstring 与
        ``test_plain_inline_schema_is_allowed``）。

        修复前 README `:50`/`:135` 与另外两处文档写成笼统的「**不能内联**」——比实现严，
        用户会误以为自己合法可跑的写法不被支持。

        NOTICE: 刻意**不检查** `docs/接口自动化框架升级优化评估.md`——那是历史记录，
        里面「schema 不能内联」是当时的实测结论，不该被追溯改写。
        """
        # 非空跑自检：修复前的原始措辞必须被判为「不合格」。
        # 没有这几条，判据写松了（例如用 `"$" in line`）本用例会永远通过。
        for old_phrasing in [
            "schema 走 `${func()}` 或文件路径（**不能内联**）",
            "schema 走 `${func()}` 或文件路径，不能内联",
            "内置算子 `jsonschema_match`；schema 走 `${func()}` 或文件路径，**不能内联**（见 3.3）",
        ]:
            self.assertTrue(
                self._is_unqualified_claim(old_phrasing),
                f"判据太松，漏判了修复前的原句：{old_phrasing}",
            )

        # 限定语境的正确措辞不得被误判
        for ok_phrasing in [
            "schema 走 `${func()}` 或文件路径（内联**仅限不含 `$` 键**的极简 schema）",
            "- **带 `$` 键的 schema 不能内联**：`$schema`/`$ref` 这些 `$` 开头的键会被框架当成变量解析",
        ]:
            self.assertFalse(
                self._is_unqualified_claim(ok_phrasing),
                f"判据过严，误判了正确表述：{ok_phrasing}",
            )

        repo_root = os.getcwd()
        user_facing_docs = [
            "README.md",
            "使用教程.md",
            os.path.join("docs", "能力清单.md"),
            "小白入门指南：用interfacetester做接口自动化测试.txt",
        ]

        offenders = []
        for relative_path in user_facing_docs:
            path = os.path.join(repo_root, relative_path)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    if self._is_unqualified_claim(line):
                        offenders.append(f"{relative_path}:{line_no}")

        self.assertEqual(
            offenders,
            [],
            "文档里出现了无限定的「不能内联」结论（同句只给『文件路径』替代方案、不提 `$` 键判据），"
            "与实现不符（实现允许不含 `$` 键的极简内联 schema）：" + ", ".join(offenders),
        )

    def test_function_and_file_references_are_allowed(self):
        steps = [
            {
                "name": "reference styles",
                "validate": [
                    {"jsonschema_match": ["body", "${get_user_schema()}"]},
                    {"jsonschema_match": ["body", "schemas/user.json"]},
                ],
            }
        ]
        ensure_json_schema_not_inline(steps, "case.yml")

    def test_other_comparators_with_dict_expect_are_untouched(self):
        """别的算子传 dict 期望值（如 `eq` 比整个 body）不受影响。"""
        steps = [
            {
                "name": "eq with dict",
                "validate": [{"eq": ["body", {"$schema": "not-a-schema"}]}],
            }
        ]
        ensure_json_schema_not_inline(steps, "case.yml")

    def test_empty_inputs_are_noop(self):
        ensure_json_schema_not_inline(None, "case.yml")
        ensure_json_schema_not_inline([], "case.yml")
        ensure_json_schema_not_inline([{"name": "no validate"}], "case.yml")


class TestEnsureXsdFilesExist(unittest.TestCase):
    """A2-2：`xml_schema_match` 引用的 XSD 文件要在 hmake 阶段就检查存在。

    口径与 `TestEnsureJsonSchemaNotInline` 一致（生成期报错 + 报错带用例/步骤/validator），
    但有两条自己的硬约束：
    ① **只查能静态判定的值** —— 含 `$` 的（`${func()}` / 变量）必须**跳过**，否则会假报错；
    ② **不编译 XSD** —— 生成阶段不能依赖 `xml` extra（没装 lxml 也要能 `hmake`）。
    """

    def setUp(self) -> None:
        self.tmp_dir = _make_tmp_project_dir()
        with open(os.path.join(self.tmp_dir, "query.xsd"), "w", encoding="utf-8") as fp:
            fp.write("<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'/>")
        self._previous_meta = loader.project_meta
        # 相对路径按 RootDir 解析 → 把「项目根」指到临时目录，测试与 cwd 无关
        loader.project_meta = mock.Mock(RootDir=self.tmp_dir)

    def tearDown(self) -> None:
        loader.project_meta = self._previous_meta
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @staticmethod
    def _steps(expect):
        return [
            {
                "name": "step with xsd",
                "validate": [{"xml_schema_match": ["body", expect]}],
            }
        ]

    def test_existing_file_passes(self):
        ensure_xsd_files_exist(self._steps("query.xsd"), "case.yml")
        ensure_xsd_files_exist(
            self._steps({"xpath": "QueryResponse", "xsd": "query.xsd"}), "case.yml"
        )

    def test_missing_file_is_rejected_with_context(self):
        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_xsd_files_exist(self._steps("nope.xsd"), "case.yml")

        message = str(ctx.exception)
        self.assertIn("XSD 文件不存在", message)
        self.assertIn("hmake 阶段提前报错", message)
        self.assertIn("case.yml", message)
        self.assertIn("step with xsd", message)  # 报错要能定位到步骤
        self.assertIn("nope.xsd", message)
        self.assertIn("已尝试", message)  # 列出尝试过的路径

    def test_missing_file_in_dict_form_is_rejected(self):
        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_xsd_files_exist(
                self._steps({"xpath": "QueryResponse", "xsd": "nope.xsd"}), "case.yml"
            )
        self.assertIn("XSD 文件不存在", str(ctx.exception))

    def test_function_and_variable_paths_are_skipped(self):
        """`${func()}` / `$var` 的路径只有运行期才知道 → 必须跳过（宁可漏报也不假报）。"""
        for expect in (
            "${get_xsd()}",
            "$xsd_path",
            {"xpath": "QueryResponse", "xsd": "${get_xsd()}"},
        ):
            with self.subTest(expect=expect):
                ensure_xsd_files_exist(self._steps(expect), "case.yml")  # 不抛异常即通过

    def test_inline_xsd_string_is_rejected_at_make_stage(self):
        """内联 XSD 是**错误的引用形态**（`xs:include` 需要「文件所在目录」）→ 生成期就拦。"""
        with self.assertRaises(exceptions.ParamsError) as ctx:
            ensure_xsd_files_exist(self._steps("<xs:schema/>"), "case.yml")

        message = str(ctx.exception)
        self.assertIn("不支持内联 XSD 字符串", message)
        self.assertIn("step with xsd", message)
        self.assertIn('"xpath": "//svc:QueryResponse"', message)  # 给出正确写法

    def test_other_comparators_are_untouched(self):
        steps = [
            {
                "name": "other comparators",
                "validate": [
                    {"xpath_match": ["body", "code"]},
                    {"jsonschema_match": ["body", "schemas/nope.json"]},
                ],
            }
        ]
        ensure_xsd_files_exist(steps, "case.yml")  # 不抛异常即通过

    def test_empty_inputs_are_noop(self):
        ensure_xsd_files_exist(None, "case.yml")
        ensure_xsd_files_exist([], "case.yml")
        ensure_xsd_files_exist([{"name": "no validate"}], "case.yml")


class TestMakeCustomComparator(unittest.TestCase):
    """P0：自定义断言算子要走完 `YAML → hmake → 生成文件可 import` 的链路。

    `test_custom_comparator_generates_importable_case` 就是零章那个 AttributeError 的回归测试：
    修复前 `import` 生成文件会直接炸，一条用例都收集不到。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.tmp_dir = _make_tmp_project_dir()
        with open(
            os.path.join(self.tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write(
                "def my_cmp(check_value, expect_value, message=\"\"):\n"
                "    assert check_value == expect_value, message\n"
                "    return True\n"
            )

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_case(self, name: str, validators) -> str:
        yml_path = os.path.join(self.tmp_dir, f"{name}.yml")
        testcase = {
            "config": {"name": name, "base_url": HTTP_BIN_URL},
            "teststeps": [
                {
                    "name": "custom comparator step",
                    "request": {"method": "GET", "url": "/get"},
                    "validate": validators,
                }
            ],
        }
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)
        return yml_path

    def test_custom_comparator_generates_importable_case(self):
        yml_path = self._write_case(
            "custom_cmp",
            [
                {"equal": ["status_code", 200]},
                {"my_cmp": ["status_code", 200, "custom comparator"]},
            ],
        )

        generated_list = main_make([yml_path])

        self.assertEqual(len(generated_list), 1)
        generated_path = generated_list[0]
        with open(generated_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn('.assert_my_cmp("status_code", 200, "custom comparator")', content)

        # 修复前：import 期 AttributeError: 'StepRequestValidation' object has no attribute
        module = _import_python_file(generated_path, "custom_cmp_test")
        generated_cls = module.TestCaseCustomCmp
        self.assertEqual(
            generated_cls.teststeps[0].struct().validators,
            [
                {"equal": ["status_code", 200, ""]},
                {"my_cmp": ["status_code", 200, "custom comparator"]},
            ],
        )

    def test_unknown_comparator_fails_at_make_stage_and_writes_nothing(self):
        yml_path = self._write_case(
            "typo_cmp", [{"my_cmpp": ["status_code", 200]}]
        )

        with self.assertRaises(SystemExit) as ctx:
            main_make([yml_path])

        self.assertEqual(ctx.exception.code, 1)
        # 校验在写盘之前完成：不留下半成品生成文件
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp_dir, "typo_cmp_test.py"))
        )


class TestBatch0918_4MakeConsistency(unittest.TestCase):
    r"""0918-4 批次：H1（`.\` 前缀 off-by-one）与 L8（批量 hmake 的失败粒度）。

    NOTICE: L8 的观测点是「**排在其后的文件有没有被生成**」，所以这些用例必须保证
    坏文件不在遍历顺序的末尾——否则「中止整批」和「继续处理」看起来一样，
    用例会假通过。为此这里显式断言 `load_folder_files` 的返回顺序（L3 已把它改成
    排序，顺序因此可复现），坏文件名也刻意取在中间。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.tmp_dir = _make_tmp_project_dir()
        with open(
            os.path.join(self.tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_case(self, name: str, teststeps, folder: str = None) -> str:
        folder = folder or self.tmp_dir
        yml_path = os.path.join(folder, f"{name}.yml")
        testcase = {
            "config": {"name": name, "base_url": HTTP_BIN_URL},
            "teststeps": teststeps,
        }
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)
        return yml_path

    @staticmethod
    def _good_step(name: str = "good step"):
        return {
            "name": name,
            "request": {"method": "GET", "url": "/get"},
            "validate": [{"eq": ["status_code", 200]}],
        }

    @staticmethod
    def _bad_step(name: str = "bad step"):
        # 算子名拼错 → 生成期 FunctionNotFound（非「格式类」异常）
        return {
            "name": name,
            "request": {"method": "GET", "url": "/get"},
            "validate": [{"eqq": ["status_code", 200]}],
        }

    # ---------------------------------------------------------------- H1
    def test_windows_style_dot_slash_reference_resolves(self):
        r"""H1：`testcase: .\ref.yml` 必须解析到 `ref.yml`。

        修复前 `__ensure_absolute` 的 Windows 分支写的是 `path[3:]`，而 `.\` 只有 **2**
        个字符 → `ref.yml` 被截成 `ef.yml`，然后以
        `ERROR | path not exist: ef.yml` 退出——报错里的文件名和用户写的完全对不上。
        """
        self._write_case("ref", [self._good_step("ref step")])
        main_path = self._write_case(
            "main", [{"name": "call ref", "testcase": ".\\ref.yml"}]
        )

        generated_list = main_make([main_path])

        names = {os.path.basename(path) for path in generated_list}
        self.assertIn("main_test.py", names)
        # 被引用的用例会被生成，但**不进 run set**（它由引用方 import 后调用，
        # 不单独作为用例运行），所以要查磁盘而不是查返回值
        self.assertTrue(
            os.path.exists(os.path.join(self.tmp_dir, "ref_test.py")),
            f"被引用用例没有被生成：{sorted(os.listdir(self.tmp_dir))}",
        )

    def test_posix_style_dot_slash_reference_still_resolves(self):
        """H1 的反向断言：`./` 分支本来就是对的（2 个字符），不能被改坏。"""
        self._write_case("ref2", [self._good_step("ref step")])
        main_path = self._write_case(
            "main2", [{"name": "call ref", "testcase": "./ref2.yml"}]
        )

        generated_list = main_make([main_path])

        self.assertIn("main2_test.py", {os.path.basename(p) for p in generated_list})

    # ---------------------------------------------------------------- L8
    def test_batch_directory_does_not_stop_at_first_failure(self):
        """L8：目录批量 hmake 时，一个坏文件不能吞掉排在其后的文件。

        实测修复前：`a_good.yml` 生成成功 → `b_bad.yml` 报错 → **`c_good.yml` 从未被处理**，
        生成物里没有 `c_good_test.py`，用户只看到「exit 1」和一个与 c 无关的错误。
        """
        batch_dir = os.path.join(self.tmp_dir, "batch")
        os.makedirs(batch_dir)
        self._write_case("a_good", [self._good_step("a")], batch_dir)
        self._write_case("b_bad", [self._bad_step("b")], batch_dir)
        self._write_case("c_good", [self._good_step("c")], batch_dir)

        # 先锁死遍历顺序：坏文件必须在中间，否则本用例测不到东西
        order = [
            os.path.basename(path) for path in loader.load_folder_files(batch_dir)
        ]
        self.assertEqual(order, ["a_good.yml", "b_bad.yml", "c_good.yml"])

        with self.assertRaises(SystemExit) as ctx:
            main_make([batch_dir])

        # 退出码仍然要非 0（失败不能被「继续处理」掩盖）
        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(os.path.exists(os.path.join(batch_dir, "a_good_test.py")))
        self.assertTrue(os.path.exists(os.path.join(batch_dir, "c_good_test.py")))
        self.assertFalse(os.path.exists(os.path.join(batch_dir, "b_bad_test.py")))

    def test_multiple_paths_do_not_stop_at_first_failure(self):
        """L8：`hmake 坏的.yml 好的.yml` —— 第 1 个失败不能让第 2 个被跳过。"""
        bad_path = self._write_case("bad_first", [self._bad_step()])
        good_path = self._write_case("good_second", [self._good_step()])

        with self.assertRaises(SystemExit) as ctx:
            main_make([bad_path, good_path])

        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp_dir, "good_second_test.py")))

    def test_all_good_batch_does_not_exit(self):
        """L8 的反向断言：全部成功时不能因为「收集了失败」而误报失败。"""
        batch_dir = os.path.join(self.tmp_dir, "all_good")
        os.makedirs(batch_dir)
        self._write_case("ok_a", [self._good_step("a")], batch_dir)
        self._write_case("ok_b", [self._good_step("b")], batch_dir)

        generated_list = main_make([batch_dir])  # 不应抛 SystemExit

        self.assertEqual(len(generated_list), 2)

    def test_unexpected_exception_is_isolated_too(self):
        """L8：非 `MyBaseError` 的意外异常同样不能让整批静默丢掉其余文件。

        框架不希望出现这类异常，但「一个文件炸掉整批」比「报错 + 继续」更糟：
        前者会让用户以为什么都没生成。这里用 monkeypatch 制造一个非 MyBaseError 异常。
        """
        batch_dir = os.path.join(self.tmp_dir, "unexpected")
        os.makedirs(batch_dir)
        self._write_case("x_bad", [self._good_step("x")], batch_dir)
        self._write_case("y_good", [self._good_step("y")], batch_dir)

        original = make_module.make_testcase

        def exploding(testcase):
            if testcase["config"]["name"] == "x_bad":
                raise RuntimeError("boom")
            return original(testcase)

        with mock.patch.object(make_module, "make_testcase", side_effect=exploding):
            with self.assertRaises(SystemExit) as ctx:
                main_make([batch_dir])

        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(os.path.exists(os.path.join(batch_dir, "y_good_test.py")))


class TestBatch0919_19MakeExitCode(unittest.TestCase):
    r"""0919-19 / H4：`hmake` 的退出码口径（显式点名 vs 目录扫描）。

    ## 修复前的现场（实测）

    ```text
    make broken.yml   （YAML 缩进错）        → EXIT 0   ← 只打印解析错误
    make empty.yml    （0 字节）             → EXIT 0
    make unknownop.yml（未知算子）           → EXIT 1   ← 同一批里却是 1，口径不一致
    make nope.yml     （路径不存在）         → EXIT 1
    ```

    于是 CI 里 `hmake cases/` 会"全绿"，而解析失败的用例一个都没生成、一个都没跑。

    ## 修复后的三条规则（逐条钉在下面）

    ① **命令行显式点名**的文件：任何失败都记失败（→ 末尾统一 exit 1）；
    ② **目录扫描**：能解析、看得出用例意图（含 `config`/`teststeps`）却不合法 → 记失败；
       完全不像用例的文件（`schemas/*.json`、集合 JSON、契约 yaml）→ 只告警；
       **解析失败但源文本看得出用例意图**（有行首的 `config:`/`teststeps:`）→ 记失败；
       连文本都看不出意图的 `*.yml/*.yaml` → 只告警（计入 ③）；
    ③ 末尾**汇总**写出"跳过 N 个文件（其中 M 个是 .yml/.yaml）"，让"全绿但什么都没跑"可见。

    NOTICE: 规则②的第二个子句是**承重**的——`hconvert` 生成的 `converted/` 目录里就带着
    `schemas/*.json`，无差别记失败会把 `hrun converted/`（完全合法）改红。

    NOTICE（本批行为收紧，`_looks_like_testcase_text`）：规则②的第三子句原本是
    「解析失败 → **一律**只告警（不计入失败）」，理由是"解析失败就判不出意图"。
    这个保守取向把**真正的坏用例**一起放过了：一个缩进写错的用例 YAML、
    或一个值构造不出来的 YAML（超长整数 → CPython 4300 位上限），
    都只留下一条"已跳过"告警 + **退出码 0**，CI 全绿而用例凭空消失。
    现在按**源文本**再判一次意图（判据与 `_looks_like_testcase_intent` 同一：
    只看行首的映射键 `config:` / `teststeps:`），因此：
      - 坏用例 → **记失败**（CI 会红，用例不会凭空消失）；
      - 契约/模板类文件（OpenAPI 的 `paths:`、Postman 的 `"item":`、JSON Schema 的
        `$schema:`）→ 仍**只告警**（它们的行首键不是 `config`/`teststeps`）。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        make_module.make_failures.clear()
        make_module.make_skipped_files.clear()
        self.tmp_dir = _make_tmp_project_dir()
        with open(
            os.path.join(self.tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        make_module.make_failures.clear()
        make_module.make_skipped_files.clear()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------- 夹具
    def _write_case(self, name: str, teststeps, folder: str = None) -> str:
        folder = folder or self.tmp_dir
        yml_path = os.path.join(folder, f"{name}.yml")
        testcase = {
            "config": {"name": name, "base_url": HTTP_BIN_URL},
            "teststeps": teststeps,
        }
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)
        return yml_path

    @staticmethod
    def _good_step(name: str = "good step"):
        return {
            "name": name,
            "request": {"method": "GET", "url": "/get"},
            "validate": [{"eq": ["status_code", 200]}],
        }

    def _write_broken_yaml(self, name: str, folder: str = None) -> str:
        """写一个**真的解析不了**的 YAML（缩进错一格）。"""
        folder = folder or self.tmp_dir
        path = os.path.join(folder, f"{name}.yml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "config:\n"
                "    name: broken\n"
                "teststeps:\n"
                "    -\n"
                "        name: s\n"
                "        request:\n"
                "            method: GET\n"
                "            url: /get\n"
                "       validate:\n"
                "            - eq: [status_code, 200]\n"
            )
        return path

    def _write_json(self, name: str, payload, folder: str = None) -> str:
        folder = folder or self.tmp_dir
        path = os.path.join(folder, f"{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return path

    def _capture_logs(self, func, *args, level="WARNING"):
        messages = []
        sink_id = logger.add(messages.append, level=level, format="{message}")
        try:
            func(*args)
        finally:
            logger.remove(sink_id)
        return messages

    # ------------------------------------------------------- 规则①
    def test_explicitly_named_broken_yaml_exits_nonzero(self):
        broken = self._write_broken_yaml("broken")

        with self.assertRaises(SystemExit) as ctx:
            main_make([broken])

        self.assertEqual(ctx.exception.code, 1, "显式点名的坏 YAML 不能返回 0")
        self.assertEqual([item.path for item in make_module.make_failures], [broken])

    def test_explicitly_named_zero_byte_file_exits_nonzero(self):
        empty = os.path.join(self.tmp_dir, "empty.yml")
        open(empty, "w", encoding="utf-8").close()

        with self.assertRaises(SystemExit) as ctx:
            main_make([empty])

        self.assertEqual(ctx.exception.code, 1, "0 字节文件以前静默返回 0")
        self.assertEqual([item.path for item in make_module.make_failures], [empty])

    def test_explicitly_named_non_case_file_exits_nonzero(self):
        """规则①的边界：显式点名一个"不像用例"的文件也算失败（用户明确要求把它当用例）。"""
        schema = self._write_json("user", {"type": "object"})

        with self.assertRaises(SystemExit) as ctx:
            main_make([schema])

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual([item.path for item in make_module.make_failures], [schema])
        self.assertEqual(
            make_module.make_skipped_files,
            [],
            "显式点名不该走「跳过」分支",
        )

    # ------------------------------------------------------- 规则②
    def test_directory_scan_tolerates_legit_non_case_files(self):
        """承重用例：`converted/` 里的 `schemas/*.json` 与集合 JSON 不能被判失败。"""
        batch_dir = os.path.join(self.tmp_dir, "converted")
        os.makedirs(os.path.join(batch_dir, "schemas"))
        self._write_case("ok_case", [self._good_step()], batch_dir)
        self._write_json(
            "user", {"type": "object", "required": ["id"]}, os.path.join(batch_dir, "schemas")
        )
        self._write_json(
            "collection",
            {
                "info": {
                    "name": "c",
                    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
                },
                "item": [],
            },
            batch_dir,
        )

        generated = main_make([batch_dir])  # 不应抛 SystemExit

        self.assertEqual(len(generated), 1)
        self.assertEqual(
            sorted(os.path.basename(path) for path in make_module.make_skipped_files),
            ["collection.json", "user.json"],
        )

    def test_directory_scan_records_case_intent_but_invalid(self):
        """规则②第一子句：有 `teststeps` 却没有 `config` → 那是坏用例，必须记失败。"""
        batch_dir = os.path.join(self.tmp_dir, "intent")
        os.makedirs(batch_dir)
        self._write_case("ok_case", [self._good_step()], batch_dir)
        no_config = os.path.join(batch_dir, "no_config.yml")
        with open(no_config, "w", encoding="utf-8") as f:
            yaml.safe_dump({"teststeps": [self._good_step()]}, f, allow_unicode=True)

        with self.assertRaises(SystemExit) as ctx:
            main_make([batch_dir])

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual([item.path for item in make_module.make_failures], [no_config])
        self.assertTrue(os.path.exists(os.path.join(batch_dir, "ok_case_test.py")))

    def test_directory_scan_broken_yaml_is_only_skipped(self):
        """规则②第三子句（**本批收紧**）：解析失败，但源文本看得出用例意图 → 记**失败**。

        NOTICE（行为收紧）：修复前这一格断言的是"只跳过（不判失败）"，理由是
        "解析失败就判不出意图"。后果是一个缩进写错的用例 YAML 只留一条告警 +
        退出码 0 —— 用例在 CI 里**凭空消失**而全绿。
        现在按源文本（行首 `config:` / `teststeps:`）判意图，坏用例必须变红。
        真正"不像用例"的文件仍只跳过，见下一条反向护栏。
        """
        batch_dir = os.path.join(self.tmp_dir, "broken_scan")
        os.makedirs(batch_dir)
        self._write_case("ok_case", [self._good_step()], batch_dir)
        broken = self._write_broken_yaml("broken", batch_dir)

        with self.assertRaises(SystemExit) as ctx:
            main_make([batch_dir])

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(len(make_module.make_failures), 1)
        self.assertIn("broken", make_module.make_failures[0].path)
        self.assertEqual(make_module.make_skipped_files, [])
        # 同目录里的好用例**仍然要生成**（一个坏文件不能拖垮整批）
        self.assertTrue(
            os.path.exists(os.path.join(batch_dir, "ok_case_test.py")),
            "坏的用例记失败，但不能连累同目录里合法的用例",
        )
        self.assertTrue(os.path.exists(broken))

    def test_directory_scan_non_case_file_is_still_only_skipped(self):
        """反向护栏：源文本看不出用例意图的文件（契约/模板类）仍**只跳过**。

        NOTICE: 这是「不许假报」那一侧 —— 收紧后的判据不能把
        `schemas/*.json`、OpenAPI 契约 yaml、Postman 集合这类文件误判成坏用例，
        否则 `hrun converted/`（完全合法）会变红（见类文档里"承重"那条 NOTICE）。
        """
        batch_dir = os.path.join(self.tmp_dir, "non_case_scan")
        os.makedirs(batch_dir)
        self._write_case("ok_case", [self._good_step()], batch_dir)

        # ① 解析不了的 OpenAPI 契约（行首是 paths:，不是 config:/teststeps:）
        contract = os.path.join(batch_dir, "openapi.yml")
        with open(contract, "w", encoding="utf-8") as f:
            f.write("openapi: 3.0.0\npaths:\n  /get:\n   broken_indent\n")
        # ② 解析不了的 JSON Schema（行首是 $schema）
        schema = os.path.join(batch_dir, "schema.json")
        with open(schema, "w", encoding="utf-8") as f:
            f.write('{"$schema": "http://json-schema.org/draft-07/schema#", "broken": }')

        generated = main_make([batch_dir])  # 不应抛 SystemExit

        self.assertEqual(len(generated), 1)
        self.assertEqual(make_module.make_failures, [])
        self.assertEqual(
            sorted(make_module.make_skipped_files), sorted([contract, schema])
        )

    # ------------------------------------------------------- 规则③
    def test_skip_summary_is_logged_with_counts(self):
        """规则③：末尾必须有一行"跳过 N 个（其中 M 个是 .yml/.yaml）"。

        NOTICE（行为收紧）：`broken.yml`（源文本有 `config:`/`teststeps:`）现在
        **记失败**并让 `main_make` 以 exit 1 收尾 —— 失败与跳过的汇总因此
        分属**两次调用**：本用例先单跑"全是跳过"的那一次，把 ③ 的计数钉住；
        含坏用例时的 exit 1 由 `test_directory_scan_broken_yaml_is_only_skipped` 守。
        """
        batch_dir = os.path.join(self.tmp_dir, "summary")
        os.makedirs(batch_dir)
        self._write_case("ok_case", [self._good_step()], batch_dir)
        self._write_json("user", {"type": "object"}, batch_dir)
        # 解析不了、且看不出用例意图的 yaml → 仍只跳过（会计入"其中 M 个"）
        with open(os.path.join(batch_dir, "notes.yml"), "w", encoding="utf-8") as f:
            f.write("title: 说明文档\n   bad_indent: 1\n")

        messages = self._capture_logs(main_make, [batch_dir])

        summary_lines = [m for m in messages if "已跳过" in m]
        self.assertEqual(len(summary_lines), 1, f"跳过的汇总行应当正好一条：{messages}")
        summary = summary_lines[0]
        self.assertIn("已跳过 2 个", summary)
        self.assertIn("1 个是 .yml/.yaml", summary)
        self.assertIn("notes.yml", summary)
        self.assertIn("user.json", summary)

    def test_broken_case_is_reported_as_failure_not_skip(self):
        """规则②第三子句（**本批收紧**）：坏用例走"失败"通道，且**不再**计入跳过汇总。

        这一条与上一条互补：同一份素材（一个好用例 + 一个坏用例 + 一个非用例 json），
        坏那个必须出现在"生成失败"里、**不能**出现在"已跳过"里 ——
        否则「用例凭空消失而 CI 全绿」这个失效模式会从 ③ 的计数里被掩盖过去。
        """
        batch_dir = os.path.join(self.tmp_dir, "mixed")
        os.makedirs(batch_dir)
        self._write_case("ok_case", [self._good_step()], batch_dir)
        broken = self._write_broken_yaml("broken", batch_dir)
        self._write_json("user", {"type": "object"}, batch_dir)

        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            with self.assertRaises(SystemExit) as ctx:
                main_make([batch_dir])
        finally:
            logger.remove(sink_id)

        # 坏用例必须以 exit 1 收尾，并出现在"生成失败"里
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(
            [item.path for item in make_module.make_failures], [broken]
        )
        # 跳过汇总里只应剩那个非用例 json（坏用例已改走失败通道）
        summary_lines = [m for m in messages if "已跳过" in m]
        self.assertEqual(len(summary_lines), 1, messages)
        self.assertIn("已跳过 1 个", summary_lines[0])
        self.assertNotIn("broken.yml", summary_lines[0])
        self.assertIn("user.json", summary_lines[0])

    def test_no_skip_summary_when_nothing_is_skipped(self):
        """反向断言：全部正常时不能凭空打一条"已跳过"。"""
        batch_dir = os.path.join(self.tmp_dir, "clean")
        os.makedirs(batch_dir)
        self._write_case("ok_case", [self._good_step()], batch_dir)

        messages = self._capture_logs(main_make, [batch_dir])

        self.assertEqual([m for m in messages if "已跳过" in m], [])

    def test_zero_output_path_is_visible(self):
        """规则③的延伸：整条路径零产出（空目录）时也要有一行，不能静默 exit 0。"""
        empty_dir = os.path.join(self.tmp_dir, "empty_dir")
        os.makedirs(empty_dir)

        messages = self._capture_logs(main_make, [empty_dir])  # 不应抛 SystemExit

        self.assertTrue(
            [m for m in messages if "没有从这些路径生成任何用例" in m],
            f"空目录必须有可见提示：{messages}",
        )


class TestBatch0918_8ReferenceNaming(unittest.TestCase):
    r"""0918-8 批次：H9（被引用用例的 import 别名撞名）与 M16（两个用例落到同一个生成物）。

    NOTICE: 这两条**修复前都是静默的**，所以观测点必须落在**生成物与真实运行**上，
    只看退出码是照不出来的（实测两种 bug 都是 exit 0）：
    - **H9**：`a/login.yml` 与 `b/login.yml` 的类名都是 `Login`，生成物里后一条
      `from b.login_test import TestCaseLogin as Login` 会**遮蔽**前一条，
      两个 step 都调用同一个类 → 前面的用例被静默替换（"call A" 一步都没跑，用例反而全绿）；
    - **M16**：`login-v2.yml` 与 `login_v2.yml` 归一化后落到同一个 `login_v2_test.py`，
      第二个被生成物缓存短路 → 不生成也不执行。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.tmp_dir = _make_tmp_project_dir()
        with open(
            os.path.join(self.tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_case(self, name: str, teststeps, folder: str = None) -> str:
        folder = folder or self.tmp_dir
        yml_path = os.path.join(folder, f"{name}.yml")
        testcase = {
            "config": {"name": name, "base_url": HTTP_BIN_URL},
            "teststeps": teststeps,
        }
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)
        return yml_path

    @staticmethod
    def _step(name: str = "step") -> dict:
        return {
            "name": name,
            "request": {"method": "GET", "url": "/get"},
            "validate": [{"eq": ["status_code", 200]}],
        }

    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _write_named_case(self, folder: str, file_name: str, case_name: str) -> str:
        """写一个「**文件名与用例名不同**」的用例。

        H9 的触发条件正是「文件名相同、用例不同」——所以这两个名字必须能分开设置。
        """
        yml_path = os.path.join(folder, file_name)
        testcase = {
            "config": {"name": case_name, "base_url": HTTP_BIN_URL},
            "teststeps": [self._step(f"{case_name} step")],
        }
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)
        return yml_path

    # ---------------------------------------------------------------- H9
    def test_same_basename_references_get_distinct_aliases(self):
        """H9：同名不同目录的两个被引用用例，必须拿到**不同**的 import 别名。

        观测点取「别名唯一 + 别名与模块配对正确」——这正是修复前被破坏的性质
        （修复前两行都是 `as Login`，第 2 行遮蔽第 1 行）。
        """
        # 目录名带 uuid，避免与其它测试的模块缓存同名
        suffix = uuid.uuid4().hex[:6]
        dir_a = os.path.join(self.tmp_dir, f"moda{suffix}")
        dir_b = os.path.join(self.tmp_dir, f"modb{suffix}")
        os.makedirs(dir_a)
        os.makedirs(dir_b)

        self._write_case("login", [self._step("A step")], dir_a)
        self._write_case("login", [self._step("B step")], dir_b)
        main_path = self._write_case(
            "main",
            [
                {"name": "call A", "testcase": f"moda{suffix}/login.yml"},
                {"name": "call B", "testcase": f"modb{suffix}/login.yml"},
            ],
        )

        main_make([main_path])
        generated = self._read(os.path.join(self.tmp_dir, "main_test.py"))

        # 1) 两条 import 的别名不能相同
        aliases = re.findall(r"import TestCaseLogin as (\w+)", generated)
        self.assertEqual(len(aliases), 2, generated)
        self.assertNotEqual(
            aliases[0],
            aliases[1],
            f"两个被引用用例拿到了同一个 import 别名，后一条会遮蔽前一条：\n{generated}",
        )

        # 2) 别名必须与各自的模块配对（`from moda… import … as X` ↔ `.call(X)`）
        pairs = re.findall(r"from ([\w.]+) import TestCaseLogin as (\w+)", generated)
        self.assertEqual(len(pairs), 2, generated)
        calls = re.findall(r'RunTestCase\("call (\w)"\)\.call\((\w+)\)', generated)
        self.assertEqual(calls, [("A", pairs[0][1]), ("B", pairs[1][1])], generated)
        self.assertIn(f"moda{suffix}.login_test", pairs[0][0])
        self.assertIn(f"modb{suffix}.login_test", pairs[1][0])

    def test_distinct_alias_actually_points_to_its_own_testcase(self):
        """H9 的运行期证据：**导入生成物**，断言两个 step 引用的是**两个不同**的用例。

        只看生成物文本还不够——真正的 bug 是「两个 step 执行同一个用例」，
        所以这里把生成物当模块导入，比较引用到的用例名（`Config(name)`）。
        """
        suffix = uuid.uuid4().hex[:6]
        dir_a = os.path.join(self.tmp_dir, f"refa{suffix}")
        dir_b = os.path.join(self.tmp_dir, f"refb{suffix}")
        os.makedirs(dir_a)
        os.makedirs(dir_b)

        # 文件名相同（触发条件），用例名不同（断言依据）
        self._write_named_case(dir_a, "login.yml", "A login case")
        self._write_named_case(dir_b, "login.yml", "B login case")
        main_path = self._write_case(
            "main",
            [
                {"name": "call A", "testcase": f"refa{suffix}/login.yml"},
                {"name": "call B", "testcase": f"refb{suffix}/login.yml"},
            ],
        )

        main_make([main_path])
        generated_main = os.path.join(self.tmp_dir, "main_test.py")

        sys.path.insert(0, self.tmp_dir)
        try:
            spec = importlib.util.spec_from_file_location(
                f"gen_main_{suffix}", generated_main
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            referenced = [
                step.struct().testcase.config.name
                for step in module.TestCaseMain.teststeps
            ]
        finally:
            sys.path.remove(self.tmp_dir)
            for mod in (f"refa{suffix}.login_test", f"refb{suffix}.login_test"):
                sys.modules.pop(mod, None)

        self.assertEqual(
            referenced,
            ["A login case", "B login case"],
            "两个 step 引用到了同一个被引用用例（这正是修复前的静默替换）",
        )

    def test_same_reference_twice_keeps_one_alias(self):
        """反向：**同一个**被引用用例引用两次不是冲突，别名必须复用（不能误判/误消歧）。"""
        self._write_case("ref", [self._step("ref step")])
        main_path = self._write_case(
            "main_twice",
            [
                {"name": "call ref 1", "testcase": "ref.yml"},
                {"name": "call ref 2", "testcase": "ref.yml"},
            ],
        )

        main_make([main_path])
        generated = self._read(os.path.join(self.tmp_dir, "main_twice_test.py"))

        # 只应有一条 import（同一用例复用同一别名），且两个 step 都用它
        self.assertEqual(generated.count("import TestCaseRef as Ref\n"), 1, generated)
        self.assertEqual(generated.count('.call(Ref)'), 2, generated)

    def test_ordinary_single_reference_alias_is_unchanged(self):
        """反向：**不撞名**时别名与修复前**逐字一致**（保证既有 34 个生成物零漂移）。"""
        self._write_case("ref_plain", [self._step("ref step")])
        main_path = self._write_case(
            "main_plain", [{"name": "call ref", "testcase": "ref_plain.yml"}]
        )

        main_make([main_path])
        generated = self._read(os.path.join(self.tmp_dir, "main_plain_test.py"))

        self.assertIn(
            "from ref_plain_test import TestCaseRefPlain as RefPlain\n", generated
        )
        self.assertIn('RunTestCase("call ref").call(RefPlain)', generated)

    # ---------------------------------------------------------------- M16
    def test_two_cases_mapping_to_same_generated_file_is_reported(self):
        """M16：`login-v2.yml` + `login_v2.yml` → 必须**报错**，不能静默只生成一个。"""
        batch_dir = os.path.join(self.tmp_dir, "collide")
        os.makedirs(batch_dir)
        self._write_case("login-v2", [self._step("dash step")], batch_dir)
        self._write_case("login_v2", [self._step("underscore step")], batch_dir)

        log_messages = []
        sink_id = logger.add(log_messages.append, level="ERROR", format="{message}")
        try:
            with self.assertRaises(SystemExit) as ctx:
                main_make([batch_dir])
        finally:
            logger.remove(sink_id)

        self.assertEqual(ctx.exception.code, 1)

        # 报错必须点名**两个**源文件（否则用户不知道改哪一个）
        joined = "\n".join(log_messages)
        self.assertIn("同一个", joined)
        self.assertIn("login-v2.yml", joined)
        self.assertIn("login_v2.yml", joined)

        # 只生成了一个（第一个），且不是静默覆盖出来的
        self.assertTrue(os.path.exists(os.path.join(batch_dir, "login_v2_test.py")))

    def test_single_case_with_dash_still_works(self):
        """反向：只有**一个**用例时，连字符归一化照旧（不能把合法用法判成冲突）。"""
        batch_dir = os.path.join(self.tmp_dir, "single")
        os.makedirs(batch_dir)
        self._write_case("login-v2", [self._step("dash step")], batch_dir)

        generated_list = main_make([batch_dir])

        self.assertEqual(len(generated_list), 1)
        self.assertTrue(os.path.exists(os.path.join(batch_dir, "login_v2_test.py")))


class TestBatch0920ReferenceAliasMustNotShadowTemplateNames(unittest.TestCase):
    r"""0920 批次 5 / **N28**：被引用用例的别名不能撞上**模板自己用到的名字**。

    ## 修复前的现场（`.tmp_audit/n28_check.py`）

    被引用用例叫 `sub/config.yml` → 类名 `Config` → 别名 `Config`，于是生成物：

    ```python
    from interfacetester import InterfaceTester, Config, Step, RunRequest
    from sub.config_test import TestCaseConfig as Config      # ← 遮蔽上面那个
    config = Config("parent").base_url("http://127.0.0.1:8000")   # ← 调用的是被引用的类
    ```

    生成物**语法合法**（`ensure_generated_python_is_valid` 过）、`hmake` **exit 0**、
    日志说"generated testcase"，而 pytest 收集期直接
    `TypeError: TestCaseConfig() takes no arguments`（exit 2）——
    报错指向被引用的类，看不出根因是"别名撞了框架名字"。

    修法：把 `_GENERATED_TEMPLATE_RESERVED_NAMES` 预置进 `used_aliases`，
    让消歧分支生效（→ `ConfigRef`）。**只有真的撞上才改名**，既有生成物零漂移。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.tmp_dir = _make_tmp_project_dir()
        with open(
            os.path.join(self.tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _write_child(self, folder: str, file_stem: str, case_name: str) -> str:
        directory = os.path.join(self.tmp_dir, folder)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "debugtalk.py"), "w", encoding="utf-8") as f:
            f.write("")
        path = os.path.join(directory, f"{file_stem}.yml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "config": {"name": case_name, "base_url": HTTP_BIN_URL},
                    "teststeps": [
                        {
                            "name": "child step",
                            "request": {"method": "GET", "url": "/status/200"},
                            "validate": [{"eq": ["status_code", 200]}],
                        }
                    ],
                },
                f,
                allow_unicode=True,
            )
        return path

    def _make_parent(self, refs) -> str:
        main_path = os.path.join(self.tmp_dir, "main.yml")
        with open(main_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "config": {"name": "parent", "base_url": HTTP_BIN_URL},
                    "teststeps": [
                        {"name": name, "testcase": path} for name, path in refs
                    ],
                },
                f,
                allow_unicode=True,
            )
        return main_path

    def test_referenced_config_yml_does_not_shadow_framework_config(self):
        """`config.yml` → 别名不能是 `Config`（否则把框架的 Config 顶掉）。"""
        self._write_child("sub", "config", "child config case")
        main_path = self._make_parent([("call child", "sub/config.yml")])

        main_make([main_path])
        generated = self._read(os.path.join(self.tmp_dir, "main_test.py"))

        self.assertNotIn(
            "as Config\n",
            generated,
            "被引用用例的别名遮蔽了框架的 Config（N28）：\n" + generated,
        )
        # 框架的 Config 必须仍然可用（模板第一行 import 的它）
        self.assertIn("from interfacetester import InterfaceTester, Config,", generated)
        self.assertIn('Config("parent")', generated)

    def test_generated_file_is_importable_when_reference_name_collides(self):
        """端到端判据：撞名时生成物**仍然可导入**（修复前收集期就 TypeError）。

        NOTICE（本轮修复：必须**隔离进程内残留**，不能只清自己这一轮的键）。
        生成物 import 的是 `from sub.config_test import ...`，`sub` 是**顶层包名**；
        而本仓有**多个**测试都会造一个叫 `sub` 的目录（本类下一条、以及
        `TestBatch0920*` 等）。裸包名 `sub` 一旦被先跑的测试加载过，
        `sys.modules["sub"].__path__` 就钉在**那个已被 rmtree 的目录**上 ——
        于是本方法在整仓运行时拿到的是「指向已删除目录」的旧包对象：
            ModuleNotFoundError: No module named 'sub.config_test'
        （实测：单独跑绿；整仓跑红 —— 典型的**顺序相关假失败**，
        危害与 §7 第 57 条同族：会让真红灯被当成"又抽风了"。）

        修法：进入时快照 `sys.path` 与相关 `sys.modules` 键，退出时逐字还原。
        """
        self._write_child("sub", "config", "child config case")
        main_path = self._make_parent([("call child", "sub/config.yml")])

        main_make([main_path])
        generated_main = os.path.join(self.tmp_dir, "main_test.py")

        suffix = uuid.uuid4().hex[:6]
        path_snapshot = list(sys.path)
        leftover = {
            key: sys.modules.pop(key)
            for key in list(sys.modules)
            if key.split(".")[0] == "sub"
        }
        sys.path.insert(0, self.tmp_dir)
        try:
            # 生成物自己会 sys.path.insert（N8），但目录列表缓存可能来自更早的调用
            importlib.invalidate_caches()
            spec = importlib.util.spec_from_file_location(
                f"gen_alias_{suffix}", generated_main
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # 修复前这里抛 TypeError

            self.assertEqual(module.TestCaseMain.config.name, "parent")
        finally:
            if self.tmp_dir in sys.path:
                sys.path.remove(self.tmp_dir)
            sys.path[:] = path_snapshot
            # 裸包名 `sub` **必须**一起清（见上面 NOTICE：只清 `sub.` 前缀不够）
            for mod in list(sys.modules):
                if mod.split(".")[0] == "sub":
                    sys.modules.pop(mod, None)
            sys.modules.update(leftover)

    def test_other_template_names_are_also_protected(self):
        """`step` / `run_request` / `parameters` / `run_test_case` 同样要被保护。"""
        for stem, expected_conflict in (
            ("step", "Step"),
            ("run_request", "RunRequest"),
            ("run_test_case", "RunTestCase"),
            ("parameters", "Parameters"),
        ):
            with self.subTest(stem=stem):
                tmp_dir = _make_tmp_project_dir()
                try:
                    with open(
                        os.path.join(tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
                    ) as f:
                        f.write("")
                    directory = os.path.join(tmp_dir, "sub")
                    os.makedirs(directory, exist_ok=True)
                    with open(
                        os.path.join(directory, "debugtalk.py"), "w", encoding="utf-8"
                    ) as f:
                        f.write("")
                    with open(
                        os.path.join(directory, f"{stem}.yml"), "w", encoding="utf-8"
                    ) as f:
                        yaml.safe_dump(
                            {
                                "config": {"name": f"child {stem}", "base_url": HTTP_BIN_URL},
                                "teststeps": [
                                    {
                                        "name": "s",
                                        "request": {"method": "GET", "url": "/status/200"},
                                        "validate": [{"eq": ["status_code", 200]}],
                                    }
                                ],
                            },
                            f,
                            allow_unicode=True,
                        )
                    main_path = os.path.join(tmp_dir, "main.yml")
                    with open(main_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(
                            {
                                "config": {"name": "parent", "base_url": HTTP_BIN_URL},
                                "teststeps": [
                                    {"name": "call", "testcase": f"sub/{stem}.yml"}
                                ],
                            },
                            f,
                            allow_unicode=True,
                        )

                    pytest_files_made_cache_mapping.clear()
                    pytest_files_run_set.clear()
                    loader.project_meta = None
                    main_make([main_path])
                    generated = self._read(os.path.join(tmp_dir, "main_test.py"))

                    self.assertNotIn(
                        f"as {expected_conflict}\n",
                        generated,
                        f"别名遮蔽了模板的 {expected_conflict}（N28）",
                    )
                finally:
                    pytest_files_made_cache_mapping.clear()
                    pytest_files_run_set.clear()
                    loader.project_meta = None
                    shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_ordinary_reference_alias_is_still_unchanged(self):
        """反向护栏：不撞名的引用照旧用类名做别名（既有生成物零漂移）。"""
        self._write_child("sub", "plain_child", "plain child")
        main_path = self._make_parent([("call child", "sub/plain_child.yml")])

        main_make([main_path])
        generated = self._read(os.path.join(self.tmp_dir, "main_test.py"))

        self.assertIn("from sub.plain_child_test import TestCasePlainChild as PlainChild\n", generated)

    def test_reserved_names_match_the_generated_template(self):
        """对账：预留名清单必须覆盖**生成物真正使用的**框架名字。

        这条防的是"改了模板却忘了改清单"——那种情况下保护会**静默失效**，
        而失效的表现又是延迟到运行期的怪异 `TypeError`。

        NOTICE: 判据刻意**不解析模板源码里的 import 语句** ——
        第一版那么写，结果把 `make.py` 自己的 import（`Any`/`Dict`/`logger`…）
        也算进来了，直接假报。改为**渲染一份典型生成物**、再从产物里抽
        `from interfacetester import …` 与 `import …` 的名字：那是**真正**会
        进入生成物命名空间的东西，与实际生效范围完全一致。
        """
        # 渲染一份"带参数化 / 带引用"的生成物（覆盖模板的所有条件 import 分支）
        rendered = make_module.__TEMPLATE__.render(
            version="0.0.0",
            testcase_path="main.yml",
            class_name="TestCaseProbe",
            imports_list=["from sub.child_test import TestCaseChild as Child"],
            config_chain_style='Config("probe")',
            skip='"reason"',
            parameters='{"uid": [1]}',
            reference_testcase=True,
            teststeps_chain_style=['Step(RunTestCase("c").call(Child))'],
        )

        imported = set()
        for match in re.finditer(
            r"^from\s+[\w.]+\s+import\s+([^\n]+)$", rendered, re.MULTILINE
        ):
            for part in match.group(1).split(","):
                name = part.strip().split(" as ")[-1].strip()
                if name:
                    imported.add(name)
        for match in re.finditer(r"^import\s+(\w+)", rendered, re.MULTILINE):
            imported.add(match.group(1))

        # 排除我们**自己注入**的那条被引用用例 import（`Child` 是引用别名，
        # 不是模板名 —— 它本来就该由 `__ref_testcase_alias` 管理，不属于本清单）
        imported.discard("Child")

        self.assertTrue(imported, "没能从渲染结果里解析出 import（判据失效）")
        missing = imported - make_module._GENERATED_TEMPLATE_RESERVED_NAMES
        self.assertEqual(
            missing,
            set(),
            f"生成物用了但预留名清单里没有：{sorted(missing)} —— "
            f"这些名字会被被引用用例的别名遮蔽",
        )


class TestBatch0920CrossProjectReference(unittest.TestCase):
    r"""0920 批次 3 / **N7 + N8**：跨项目引用同名用例文件。

    H9（上一个类）修的是「**同一项目内**不同目录的同名文件」——它把 import **别名**消歧了。
    但**模块名**本身仍然由 `convert_relative_project_root_dir` 派生，而那个函数算的是
    「相对**被引用用例自己那个项目**的 RootDir」——于是**两个不同项目**里的 `child.yml`
    **都**得到 `child_test`：

    ```python
    from child_test import TestCaseChild as Child
    from child_test import TestCaseChild as ChildRef   # ← 同一个模块！
    ```

    两条后果（都实测于 `.tmp_audit/verify_xproj_collision.py`）：

    - **N7**：第 2 条 import 命中 `sys.modules` 里已加载的同一个模块，两个别名绑定
      **同一个类** → 名为 `call childB` 的步骤实际跑的是 **childC 的用例**
      （取决于激活顺序，另一个方向表现为**假通过**）；
    - **N8**：生成物只有在 `hrun`（**同进程**跑过 hmake、ref 项目根还留在 `sys.path`）
      里才能 import；换一个进程 `pytest <生成物>` →
      `ModuleNotFoundError: No module named 'child_test'` —— 而"hmake 然后 pytest"
      正是 CI 的两步法。

    修法：被引用用例**不在本用例所属项目**时，模块名前缀上项目目录
    （`projB/child.yml` → `projB.child_test`），并把该项目根加进生成物的 `sys.path`。
    **同项目引用零漂移**（34 个已入库生成物不受影响，见 `generated_artifacts_drift_test`）。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        loader.project_meta_cache.clear()
        self.tmp_dir = _make_tmp_project_dir()

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        loader.project_meta_cache.clear()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_project(self, name: str, case_name: str, status_code: int) -> str:
        """造一个**独立项目**（自带 debugtalk.py），里面一个 `child.yml`。"""
        project_dir = os.path.join(self.tmp_dir, name)
        os.makedirs(project_dir, exist_ok=True)
        with open(
            os.path.join(project_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")
        with open(
            os.path.join(project_dir, "child.yml"), "w", encoding="utf-8"
        ) as f:
            yaml.safe_dump(
                {
                    "config": {"name": case_name, "base_url": HTTP_BIN_URL},
                    "teststeps": [
                        {
                            "name": f"{case_name} step",
                            "request": {"method": "GET", "url": "/status/200"},
                            "validate": [{"eq": ["status_code", status_code]}],
                        }
                    ],
                },
                f,
                allow_unicode=True,
            )
        return project_dir

    def _make_parent(self, project_name: str, refs) -> str:
        project_dir = os.path.join(self.tmp_dir, project_name)
        os.makedirs(project_dir, exist_ok=True)
        with open(
            os.path.join(project_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")
        main_path = os.path.join(project_dir, "main.yml")
        with open(main_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "config": {"name": "main", "base_url": HTTP_BIN_URL},
                    "teststeps": [
                        {"name": name, "testcase": path} for name, path in refs
                    ],
                },
                f,
                allow_unicode=True,
            )
        return main_path

    def _read(self, path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_cross_project_references_get_distinct_module_names(self):
        """N7 的静态判据：两个项目的同名文件必须映射到**不同的模块名**。"""
        self._make_project("projB", "B child case", 200)
        self._make_project("projC", "C child case", 999)
        main_path = self._make_parent(
            "projA",
            [
                ("call B", "../projB/child.yml"),
                ("call C", "../projC/child.yml"),
            ],
        )

        main_make([main_path])
        generated = self._read(os.path.join(self.tmp_dir, "projA", "main_test.py"))

        modules = re.findall(r"from ([\w.]+) import TestCaseChild as \w+", generated)
        self.assertEqual(len(modules), 2, generated)
        self.assertNotEqual(
            modules[0],
            modules[1],
            "两个项目的同名用例拿到了**同一个模块名** → sys.modules 里会互相覆盖（N7）：\n"
            + generated,
        )

    def test_generated_file_injects_the_referenced_project_root(self):
        """N8：生成物必须自己把被引用项目根加进 `sys.path`（不能靠 hmake 的副作用）。"""
        proj_b = self._make_project("projB2", "B child case", 200)
        main_path = self._make_parent(
            "projA2", [("call B", "../projB2/child.yml")]
        )

        main_make([main_path])
        generated = self._read(os.path.join(self.tmp_dir, "projA2", "main_test.py"))

        normalized = generated.replace("\\\\", "\\")
        self.assertIn(
            "sys.path.insert(0,",
            normalized,
            "生成物没有插入被引用项目的根目录：\n" + generated,
        )
        self.assertIn(
            os.path.basename(proj_b),
            normalized,
            "插入的路径里看不出被引用项目的目录：\n" + generated,
        )

    def test_runtime_reference_points_to_its_own_case(self):
        """N7 的运行期判据：导入生成物，断言两个 step 引用的是**两个不同**的用例。

        修复前两个 step 会引用到**同一个**用例（后者覆盖前者）。

        NOTICE（本轮修复：必须**隔离 sys.path / sys.modules 的进程内残留**）：
        本类里 `test_same_project_reference_module_name_is_unchanged` 会先跑一次
        `main_make`，而 `loader.load_project_meta()` 会把**当时的项目根**
        （`.../sameproj`）留在 `sys.path` 上、并把 `debugtalk` 塞进 `sys.modules`。
        于是本方法导入生成物时，`sys.path` 上既有自己的临时目录、又有一条**指向
        上一次测试目录**的残留项；而 `projB3`/`projC3` 是**裸包名**，
        一旦被某个先跑的方法以别的物理路径加载过，`sys.modules` 里那个
        **指向已删除目录**的包对象就会顶替本次的查找 →
        `ModuleNotFoundError: No module named 'projB3.child_test'`

        （实测：单独跑本方法绿；`sameproj THEN rt` 必红；整类跑必红。
        根因与 §7 第 57 条是同一族——**模块身份被缓存复用**。）

        修法：进入时记下 `sys.path` 快照与相关 `sys.modules` 键，退出时**逐字还原**，
        这样本方法对"之前跑过谁"完全免疫。
        """
        self._make_project("projB3", "B child case", 200)
        self._make_project("projC3", "C child case", 999)
        main_path = self._make_parent(
            "projA3",
            [
                ("call B", "../projB3/child.yml"),
                ("call C", "../projC3/child.yml"),
            ],
        )

        main_make([main_path])
        generated_main = os.path.join(self.tmp_dir, "projA3", "main_test.py")

        suffix = uuid.uuid4().hex[:6]
        # 快照：本方法要能看到**自己**的临时目录，且不受前面测试残留的干扰
        path_snapshot = list(sys.path)
        module_keys = ("projA3", "projB3", "projC3")
        leftover = {
            key: sys.modules.pop(key)
            for key in list(sys.modules)
            if key.split(".")[0] in module_keys
        }
        sys.path.insert(0, self.tmp_dir)
        try:
            # NOTICE: 生成物自己会 `sys.path.insert(0, <共同父目录>)`（N8 的修复），
            # 但它的 `__pycache__` / 目录列表缓存可能来自更早的调用，这里显式作废。
            importlib.invalidate_caches()
            spec = importlib.util.spec_from_file_location(
                f"gen_xproj_{suffix}", generated_main
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            referenced = [
                step.struct().testcase.config.name
                for step in module.TestCaseMain.teststeps
            ]
        finally:
            if self.tmp_dir in sys.path:
                sys.path.remove(self.tmp_dir)
            # 逐字还原 sys.path（去掉本方法/前面测试可能加进来的项）
            sys.path[:] = path_snapshot
            # 清掉本次导入留下的（含**裸包名**，见下）
            for mod in list(sys.modules):
                if mod.split(".")[0] in module_keys:
                    sys.modules.pop(mod, None)
            # 还原进入前就存在的（正常情况下为空，防御性处理）
            sys.modules.update(leftover)

        self.assertEqual(
            referenced,
            ["B child case", "C child case"],
            "两个 step 引用到了同一个被引用用例（N7：前面的用例被静默替换）",
        )

    def test_same_project_reference_module_name_is_unchanged(self):
        """反向护栏：**同项目**引用不许加前缀（否则 34 个已入库生成物全部漂移）。"""
        project_dir = os.path.join(self.tmp_dir, "sameproj")
        os.makedirs(project_dir, exist_ok=True)
        with open(
            os.path.join(project_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")
        with open(
            os.path.join(project_dir, "child.yml"), "w", encoding="utf-8"
        ) as f:
            yaml.safe_dump(
                {
                    "config": {"name": "child", "base_url": HTTP_BIN_URL},
                    "teststeps": [
                        {
                            "name": "s",
                            "request": {"method": "GET", "url": "/status/200"},
                            "validate": [{"eq": ["status_code", 200]}],
                        }
                    ],
                },
                f,
                allow_unicode=True,
            )
        main_path = os.path.join(project_dir, "main.yml")
        with open(main_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "config": {"name": "main", "base_url": HTTP_BIN_URL},
                    "teststeps": [{"name": "call child", "testcase": "child.yml"}],
                },
                f,
                allow_unicode=True,
            )

        main_make([main_path])
        generated = self._read(os.path.join(project_dir, "main_test.py"))

        self.assertIn(
            "from child_test import TestCaseChild as Child\n", generated
        )


class TestBatch0918_8PathNormalization(unittest.TestCase):
    r"""0918-8 / M17：输入路径必须先归一化，否则生成物会落到**错目录**。

    NOTICE（修复前实测，根因在 `loader.relative_to_root_dir`）：归属判断用**规范化**路径、
    取值却切**原始字符串**（`abs_path[len(RootDir) + 1:]`），于是 `.\\` / `..` / 重复分隔符
    会让偏移量错位——返回的相对路径从中间截断：

        hmake .\nested\case.yml   → 生成物落到 nested\d\case_test.py（exit 0，日志说"成功"）
        hmake nested//case.yml    → IndexError: string index out of range

    而且错位产物会**留在磁盘上**：之后 `hrun nested` 会把同一个用例收两遍（副作用双跑）。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.tmp_dir = _make_tmp_project_dir()
        # 嵌套项目：子目录里也有 debugtalk.py（这才是让 RootDir ≠ cwd 的触发条件）
        self.nested_dir = os.path.join(self.tmp_dir, "nested")
        os.makedirs(self.nested_dir)
        for folder in (self.tmp_dir, self.nested_dir):
            with open(
                os.path.join(folder, "debugtalk.py"), "w", encoding="utf-8"
            ) as f:
                f.write("")

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_nested_case(self) -> None:
        testcase = {
            "config": {"name": "nested", "base_url": HTTP_BIN_URL},
            "teststeps": [
                {
                    "name": "step",
                    "request": {"method": "GET", "url": "/get"},
                    "validate": [{"eq": ["status_code", 200]}],
                }
            ],
        }
        with open(
            os.path.join(self.nested_dir, "case.yml"), "w", encoding="utf-8"
        ) as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)

    def _assert_generated_in_place(self) -> None:
        expected = os.path.join(self.nested_dir, "case_test.py")
        self.assertTrue(
            os.path.exists(expected),
            f"生成物不在预期位置：{expected}\n实际："
            f"{[os.path.relpath(p, self.tmp_dir) for p in _walk_files(self.tmp_dir)]}",
        )
        # 不能出现 `nested\d\` 这种错位目录
        stray = [
            path
            for path in _walk_files(self.tmp_dir)
            if os.path.basename(path).endswith("_test.py") and path != expected
        ]
        self.assertEqual(stray, [], f"出现了错位生成物：{stray}")

    def test_dot_slash_prefix_keeps_generated_file_in_place(self):
        r"""`hmake .\nested\case.yml` 必须落在 `nested\case_test.py`。

        修复前会落到 `nested\d\case_test.py`（`.\` 的两个字符把切片偏移顶偏）。
        """
        self._write_nested_case()

        main_make([os.path.join(self.tmp_dir, ".", "nested", "case.yml")])

        self._assert_generated_in_place()

    def test_duplicate_separator_does_not_crash(self):
        """`nested//case.yml` 修复前抛 `IndexError`（空路径段取 `name[0]`）。"""
        self._write_nested_case()

        main_make([self.tmp_dir + os.sep + "nested" + os.sep + os.sep + "case.yml"])

        self._assert_generated_in_place()

    def test_parent_dir_component_is_normalized(self):
        """`nested\\..\\nested\\case.yml` 同样要落到正确位置。"""
        self._write_nested_case()

        main_make(
            [
                os.path.join(
                    self.tmp_dir, "nested", "..", "nested", "case.yml"
                )
            ]
        )

        self._assert_generated_in_place()

    def test_folder_run_collects_case_exactly_once(self):
        """错位产物会留在磁盘上 → `hrun <目录>` 会把同一个用例收两遍（副作用双跑）。

        这里直接断言「目录批量 hmake 后，收集到的 `_test.py` 只有 1 个」。
        """
        self._write_nested_case()
        main_make([os.path.join(self.tmp_dir, ".", "nested")])

        collected = [
            path
            for path in _walk_files(self.tmp_dir)
            if os.path.basename(path).endswith("_test.py")
        ]

        self.assertEqual(
            [os.path.relpath(path, self.tmp_dir) for path in collected],
            [os.path.join("nested", "case_test.py")],
        )

    def test_clean_path_result_is_unchanged(self):
        """反向：干净路径的行为与修复前完全一致（零生成物漂移）。"""
        self._write_nested_case()

        main_make([os.path.join(self.tmp_dir, "nested", "case.yml")])

        self._assert_generated_in_place()


def _walk_files(root: str):
    """列出目录下所有文件（测试内部用，避免依赖 pytest 的收集顺序）。"""
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            yield os.path.join(dirpath, filename)


class TestBatch0918_8ScalarCoercion(unittest.TestCase):
    r"""0918-8 / M18：生成期渲染必须用**模型强转过的值**，不能用原始 dict 里的字符串。

    NOTICE（修复前实测）：`make_testcase` 把 `load_testcase()` 只当**校验**用，渲染仍读原始 dict，
    于是 pydantic 的强转结果被丢掉：

        config: {verify: "false"}   → 生成 `.verify("false")`
            （**非空字符串 = 真值** → TLS 校验反而被打开，与用户意图相反，且全程无告警）
        request: {timeout: "30"}    → 生成 `.set_timeout("30")`（requests 运行期报错）
        retry_times: "3"            → 生成 `.with_retry(retry_times="3")`

    修法：把「写成字符串的标量」换回模型强转后的值——**只在类型真的不对时回写**，
    所以本来写对的 YAML 一个字符都不会变（零生成物漂移）。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.tmp_dir = _make_tmp_project_dir()
        with open(
            os.path.join(self.tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _generate(self, name: str, config: dict, request: dict, step_extra=None) -> str:
        testcase = {
            "config": {"name": name, "base_url": HTTP_BIN_URL, **config},
            "teststeps": [
                {
                    "name": "step",
                    "request": {"method": "GET", "url": "/get", **request},
                    "validate": [{"eq": ["status_code", 200]}],
                    **(step_extra or {}),
                }
            ],
        }
        yml_path = os.path.join(self.tmp_dir, f"{name}.yml")
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(testcase, f, allow_unicode=True)

        main_make([yml_path])
        with open(
            os.path.join(self.tmp_dir, f"{name}_test.py"), encoding="utf-8"
        ) as f:
            return f.read()

    def test_quoted_scalars_are_coerced(self):
        """写成字符串的标量必须按模型类型渲染（否则语义反向 / 运行期报错）。"""
        generated = self._generate(
            "quoted",
            config={"verify": "false", "timeout": "30"},
            request={
                "timeout": "15",
                "verify": "false",
                "allow_redirects": "false",
                "stream": "true",
            },
            step_extra={"retry_times": "3", "retry_interval": "2"},
        )

        self.assertIn(".verify(False)", generated)
        self.assertIn(".timeout(30.0)", generated)
        self.assertIn(".set_timeout(15.0)", generated)
        self.assertIn(".set_verify(False)", generated)
        self.assertIn(".set_allow_redirects(False)", generated)
        self.assertIn(".set_stream(True)", generated)
        self.assertIn("with_retry(retry_times=3, retry_interval=2)", generated)
        # 字符串形态必须**不再出现**
        for bad in ('verify("false")', 'timeout("30")', 'set_timeout("15")'):
            self.assertNotIn(bad, generated)

    def test_plain_scalars_are_unchanged(self):
        """反向（零漂移）：本来写对的 YAML，生成物必须与修复前**逐字一致**。

        注意 `timeout: 15`（int）保持 int、`timeout: "15"`（str）才变 float——
        这正是「只在类型真的不对时回写」的判据。
        """
        generated = self._generate(
            "plain",
            config={"verify": False, "timeout": 30},
            request={
                "timeout": 15,
                "verify": False,
                "allow_redirects": False,
                "stream": True,
            },
            step_extra={"retry_times": 3, "retry_interval": 2},
        )

        self.assertIn(".verify(False)", generated)
        self.assertIn(".timeout(30)", generated)
        self.assertIn(".set_timeout(15)", generated)
        self.assertIn(".set_allow_redirects(False)", generated)
        self.assertIn("with_retry(retry_times=3, retry_interval=2)", generated)

    def test_string_fields_are_not_touched(self):
        """字符串字段（name / base_url）不受影响，哪怕写成数字样式的字符串。"""
        generated = self._generate(
            "strings", config={}, request={}
        )
        self.assertIn('Config("strings")', generated)
        self.assertIn(f'.base_url("{HTTP_BIN_URL}")', generated)

    def test_coercion_set_is_derived_from_the_model(self):
        """防复发：哪些字段要回写**由模型注解推导**，不能是第二份手写清单。

        这里钉住「标量注解的判定」本身（含 `Optional[...]` / `Union`），以及 TRequest 上
        确实覆盖了这次踩坑的四个字段。
        """
        from typing import Dict, Optional, Text, Union

        is_scalar = getattr(make_module, "__annotation_is_scalar")

        for annotation in (bool, int, float, Optional[bool], Optional[float]):
            with self.subTest(annotation=annotation):
                self.assertTrue(is_scalar(annotation), f"{annotation} 应当是标量")

        for annotation in (type(None), None, Dict, Text, Optional[Text], list):
            with self.subTest(annotation=annotation):
                self.assertFalse(is_scalar(annotation), f"{annotation} 不该被判成标量")

        self.assertTrue(is_scalar(Union[Text, float]))

        scalar_names = {
            name
            for name, field in make_module.TRequest.model_fields.items()
            if is_scalar(field.annotation)
        }
        for name in ("timeout", "verify", "allow_redirects", "stream"):
            self.assertIn(name, scalar_names, f"TRequest.{name} 应当是标量字段")
        for name in ("url", "method", "headers", "cookies", "params"):
            self.assertNotIn(name, scalar_names, f"TRequest.{name} 不该被回写")


class TestBatch0918_8NonLiteralValues(unittest.TestCase):
    r"""0918-8 / M19：**所有**渲染路径都要做「字面量可复原性」检查。

    NOTICE（修复前实测）：检查只挂在「以 `**kwargs` 落盘」的那几处
    （`variables`/`params`/`headers`/`cookies`/`proxies`/`upload`），
    而 `json` / `data` / `validate` 期望值**全都漏了**：

        request: {json: {when: 2020-01-01}}   → `.with_json({"when": datetime.date(2020, 1, 1)})`
        validate: - eq: [body.when, 2020-01-01] → `.assert_equal("body.when", datetime.date(2020, 1, 1))`

    而 hmake **exit 0、零告警**，生成的文件在 pytest 收集期 `NameError: name 'datetime' is not defined`。
    修法：检查放进渲染唯一漏斗 `ensure_python_literal()`，因此**每个**渲染点天然覆盖。

    NOTICE（本批行为收紧，`_rendered_literal_is_usable`）：M19 只做到「告警」，
    而产物**照样落盘**、`hmake` **退出码 0** —— 也就是同一个失败模式的尾巴还在：
    值层面告警了，用户没看到（或看到了也没法定位），文件已经写出去且导入必炸。
    现在按「渲染文本里有没有**非内建名字**」精确判定：
      - `datetime.date(2020, 1, 1)` / 裸 `nan` → 生成物必然 NameError
        → **告警 + 点名 ParamsError 阻止落盘**（本类下面几条用例断言的就是它）；
      - `frozenset({...})` 这类**内建**调用 → 复不原但能正常求值 → 维持「只告警」，
        不改动任何现在能跑的用例（见 `test_usable_builtin_rendering_still_warns_only`）。
    """

    def setUp(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        self.tmp_dir = _make_tmp_project_dir()
        with open(
            os.path.join(self.tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")

    def tearDown(self) -> None:
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _step(self, request_extra=None, validate=None):
        return {
            "name": "step",
            "request": {"method": "POST", "url": "/post", **(request_extra or {})},
            "validate": validate or [{"eq": ["status_code", 200]}],
        }

    def test_json_body_date_warns_and_names_location(self):
        """`json` 体里的日期必须**报错**，且要说清是哪个步骤的哪个字段。

        NOTICE（行为收紧）：M19 只要求「告警 + 定位」，本批升级成「阻止落盘」——
        值层面已精确判定出 `datetime.date` 是非内建名字，写出去必然 NameError。
        """
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            with self.assertRaises(exceptions.ParamsError) as ctx:
                make_teststep_chain_style(
                    self._step({"json": {"when": datetime.date(2020, 1, 1)}})
                )
        finally:
            logger.remove(sink_id)

        joined = "\n".join(messages)
        self.assertIn("无法用 Python 字面量表示", joined)
        self.assertIn("request.json", joined)
        self.assertIn("when", joined)
        # 报错本身也要带定位（否则用户只知道"某处有个值不行"）
        self.assertIn("request.json", str(ctx.exception))
        self.assertIn("when", str(ctx.exception))

    def test_data_body_date_names_location(self):
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            with self.assertRaises(exceptions.ParamsError) as ctx:
                make_teststep_chain_style(
                    self._step({"data": {"d": datetime.date(2020, 1, 1)}})
                )
        finally:
            logger.remove(sink_id)

        self.assertTrue(
            any("request.data" in message for message in messages), messages
        )
        self.assertIn("request.data", str(ctx.exception))

    def test_validate_expect_date_names_location(self):
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            with self.assertRaises(exceptions.ParamsError) as ctx:
                make_teststep_chain_style(
                    self._step(validate=[{"eq": ["body.when", datetime.date(2020, 1, 1)]}])
                )
        finally:
            logger.remove(sink_id)

        self.assertTrue(
            any("validate" in message for message in messages), messages
        )
        self.assertIn("validate", str(ctx.exception))

    def test_kwargs_path_still_errors_exactly_once(self):
        """回归 + 防重复告警：kwargs 路径（`variables`）仍要告警，且**只告警一次**。

        检查上移到漏斗后，如果忘了删掉原来那处调用，同一个坏值会打两条告警。
        """
        step = self._step({"json": {"x": 1}}, validate=None)
        step["variables"] = {"start": datetime.date(2020, 1, 1)}

        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            with self.assertRaises(exceptions.ParamsError):
                make_teststep_chain_style(step)
        finally:
            logger.remove(sink_id)

        hits = [message for message in messages if "无法用 Python 字面量表示" in message]
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("start", hits[0])

    def test_usable_builtin_rendering_still_warns_only(self):
        """反向护栏：`literal_eval` 复原不了、但渲染进生成物**能用**的值 → 只告警、不拦截。

        NOTICE: 这是「不许假报」那一侧。`bytes(...)` 这类**内建构造调用**的 `repr`
        在生成物里能正常求值（`bytes` 是内建名字），所以本批的收紧必须放过它——
        否则会误伤现在能跑的用例。

        判据来源（与实现逐一对照过）：`bytes(b"x")` 的 `repr` 是 `b'x'`，
        它**是**合法字面量 → 走 `__is_python_literal` 提前返回、**完全不告警**；
        而 `frozenset`/`set` 这类容器会先被 `__ensure_value_literal` 拆成元素递归，
        元素 `1` 是字面量 → 同样不告警。真正落到「告警但不拦截」这条分支的是
        **既非容器、`literal_eval` 又复原不了、但 repr 里没有非内建名字**的形态。
        这里用 `bytes` 的**非字面量表示**验证「不拦截」这一核心不变量：
        无论走哪条分支，都**不允许**抛异常。
        """
        for usable_value in (b"x", frozenset({1}), {1, 2}, complex(1, 2)):
            with self.subTest(value=repr(usable_value)):
                messages = []
                sink_id = logger.add(messages.append, level="WARNING", format="{message}")
                try:
                    # 不抛异常即为通过：这些值渲染进生成物都能求值
                    rendered = make_teststep_chain_style(
                        self._step({"json": {"s": usable_value}})
                    )
                finally:
                    logger.remove(sink_id)

                # 渲染结果必须仍是合法 Python（产物可用）
                ast.parse(rendered)

    def test_unusable_rendering_is_blocked_not_just_warned(self):
        """对照面：**非内建名字**的渲染（`nan` / `datetime.date`）必须被拦截。

        与上一条构成一对照：同样是「不可字面量化」，结局必须不同——
        能求值的放过，不能求值的拦住。这条守住「收紧没有打偏」。
        """
        for blocking_value in (float("nan"), datetime.date(2020, 1, 1)):
            with self.subTest(value=repr(blocking_value)):
                with self.assertRaises(exceptions.ParamsError):
                    make_teststep_chain_style(
                        self._step({"json": {"v": blocking_value}})
                    )

    def test_literal_values_do_not_warn(self):
        """反向：正常的字面量（字符串/数字/列表/嵌套 dict）不能产生噪声。"""
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            make_teststep_chain_style(
                self._step(
                    {
                        "json": {"a": 1, "b": "x", "c": [1, 2], "d": {"e": None}},
                    },
                    validate=[{"eq": ["body.a", 1]}],
                )
            )
        finally:
            logger.remove(sink_id)

        self.assertEqual(messages, [])

    def test_hmake_level_evidence(self):
        """端到端：`hmake` 面对日期字面量必须**报错并拒绝落盘**。

        NOTICE（行为收紧）：修复前只要求「有告警」，而 `main_make` 会 `sys.exit(1)`
        之外**照样把文件写出去**。现在值层面直接阻止落盘，因此这里断言：
          ① `main_make` 抛 `SystemExit(1)`；
          ② 生成物**不存在**（这是"阻止落盘"的实质）；
          ③ 日志里留下可定位的告警（失败必须留证据）。
        """
        yml_path = os.path.join(self.tmp_dir, "date_literal.yml")
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "config": {"name": "date literal", "base_url": HTTP_BIN_URL},
                    "teststeps": [self._step({"json": {"when": datetime.date(2020, 1, 1)}})],
                },
                f,
                allow_unicode=True,
            )

        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            with self.assertRaises(SystemExit) as ctx:
                main_make([yml_path])
        finally:
            logger.remove(sink_id)

        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(
            any("无法用 Python 字面量表示" in message for message in messages), messages
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp_dir, "date_literal_test.py")),
            "值不可字面量化时必须**阻止落盘**（否则留下一个导入即 NameError 的产物）",
        )


class TestBatch0918_8RequestStepAliases(unittest.TestCase):
    r"""0918-8 / M25：正式字段名（`req_json` / `validators`）必须真的生效。

    NOTICE（修复前实测）：`TRequest.req_json` 的别名是 `json`、`TStep.validators` 的别名是
    `validate`，而 pydantic v2 **默认只认别名**：

        request: {req_json: {...}}   → 模型静默忽略（extra="ignore"）
                                     → `KNOWN_REQUEST_FIELDS` 里有 `req_json`，所以**也不告警**
                                     → 生成物里没有 `.with_json(...)`：请求**静默没有 body**
                                     （而告警文本还把 `req_json` 列在"已支持的字段"里）

    修法两侧一起收口：模型加 `populate_by_name=True`（字段名也能赋值）＋ 生成器接受两个名字。
    CLI 路径上 `validators` 本来由 `compat` 兜住，这里额外钉住**生成器直接调用**的路径。
    """

    def test_req_json_is_rendered(self):
        chain = make_request_chain_style(
            {"method": "POST", "url": "/x", "req_json": {"a": 1}}
        )

        # NOTICE: 这里比的是 `make_request_chain_style` 的原始输出（`repr` → 单引号）；
        # 生成文件里的双引号是随后 black 格式化的结果（端到端用例见下面那条）。
        self.assertIn(".with_json({'a': 1})", chain)

    def test_json_and_req_json_produce_the_same_chain(self):
        """反向：两个名字必须生成**完全相同**的链式调用（不能出现两套语义）。"""
        by_alias = make_request_chain_style(
            {"method": "POST", "url": "/x", "json": {"a": 1}}
        )
        by_field = make_request_chain_style(
            {"method": "POST", "url": "/x", "req_json": {"a": 1}}
        )

        self.assertEqual(by_alias, by_field)

    def test_validators_is_rendered(self):
        chain = make_teststep_chain_style(
            {
                "name": "step",
                "request": {"method": "GET", "url": "/get"},
                "validators": [{"eq": ["status_code", 200]}],
            }
        )

        self.assertIn(".validate()", chain)
        self.assertIn('.assert_equal("status_code", 200)', chain)

    def test_req_json_works_end_to_end(self):
        """端到端：`req_json` 写法的 YAML 必须生成带 body 的用例。"""
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        tmp_dir = _make_tmp_project_dir()
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        with open(os.path.join(tmp_dir, "debugtalk.py"), "w", encoding="utf-8") as f:
            f.write("")

        yml_path = os.path.join(tmp_dir, "req_json_case.yml")
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "config": {"name": "req json case", "base_url": HTTP_BIN_URL},
                    "teststeps": [
                        {
                            "name": "step",
                            "request": {
                                "method": "POST",
                                "url": "/post",
                                "req_json": {"a": 1},
                            },
                            "validate": [{"eq": ["status_code", 200]}],
                        }
                    ],
                },
                f,
                allow_unicode=True,
            )

        try:
            main_make([yml_path])
            with open(
                os.path.join(tmp_dir, "req_json_case_test.py"), encoding="utf-8"
            ) as f:
                generated = f.read()
        finally:
            pytest_files_made_cache_mapping.clear()
            pytest_files_run_set.clear()
            loader.project_meta = None

        self.assertIn('.with_json({"a": 1})', generated)


class TestBatchEHugeIntegerLiteral(unittest.TestCase):
    """批次 E / **L8**：超大整数的**告警路径自己崩**（`repr()` 被二次求值，且在 `try` 外）。

    修复前实测：`ensure_python_literal(10**5000)` 里 `__is_python_literal` 的
    `ast.literal_eval(repr(value))` 被 `try` 兜住了，但**告警那一行**又求了一次
    `repr(value)` —— CPython 3.11+ 对 int→str 有 4300 位上限，于是
    「本该告诉用户这个值不能用字面量表示」的告警自己抛
    `ValueError: Exceeds the limit (4300 digits) for integer string conversion`，
    报错里还看不出是哪个用例、哪个字段。
    """

    def _huge(self) -> int:
        # NOTICE: 用 `10 ** 5000` 造（`int("9" * 5000)` 在 py3.11+ **自己就会抛**，
        # 没法用来取证 —— 这个坑我在写取证脚本时踩过一次）。
        return 10**5000

    def test_huge_integer_raises_a_named_error(self):
        with self.assertRaises(exceptions.ParamsError) as ctx:
            make_module.ensure_python_literal(self._huge(), where="用例 l8 的 count")

        message = str(ctx.exception)
        self.assertIn("用例 l8 的 count", message, "要点名位置")
        self.assertIn("4300", message, "根因要保留（这样用户才知道为什么）")
        self.assertIsInstance(ctx.exception.__cause__, ValueError)

    def test_safe_repr_never_raises_and_truncates(self):
        huge_repr = make_module._safe_repr(self._huge())

        self.assertIn("无法 repr", huge_repr)
        self.assertLess(len(huge_repr), 400, "展示文本必须被截断")

        long_text = make_module._safe_repr("x" * 1000)
        self.assertLess(len(long_text), 300)
        # `repr("x"*1000)` 是 1002 个字符（**带两侧引号**）
        self.assertIn("共 1002 字符", long_text)

        # 正常值不受影响（逐字不变）
        self.assertEqual(make_module._safe_repr({"a": 1}), "{'a': 1}")
        self.assertEqual(make_module._safe_repr("s"), "'s'")

    def test_warning_is_emitted_before_the_error(self):
        """告警本身也必须发得出来（修复前它在告警那一行就崩了）。"""
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            with self.assertRaises(exceptions.ParamsError):
                make_module.ensure_python_literal(
                    self._huge(), where="用例 l8 的 count"
                )
        finally:
            logger.remove(sink_id)

        self.assertEqual(len(messages), 1, messages)
        self.assertIn("无法用 Python 字面量表示", messages[0])
        self.assertIn("无法 repr", messages[0], "告警里的值要用安全 repr")

    def test_normal_values_are_unaffected(self):
        """反向护栏：正常值仍然逐字渲染成字面量。"""
        self.assertEqual(make_module.ensure_python_literal(7), "7")
        self.assertEqual(make_module.ensure_python_literal("a"), '"a"')
        self.assertEqual(
            make_module.ensure_python_literal({"a": [1, 2]}), "{'a': [1, 2]}"
        )
