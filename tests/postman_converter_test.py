"""P3-b：Postman Collection v2.1 导入器的测试。

覆盖：结构（嵌套 folder / URL 对象 / 路径变量 / disabled 字段）、四种 body 模式、
认证映射、`{{var}}` 与 Postman 动态变量、保存的示例响应 → 断言、脚本无法迁移的告警、
分组策略（每顶层 folder 一个用例 / `--single-file`）、黄金文件、以及**端到端真跑**（本地 mock）。
"""

import json
import os
import shutil
import sys
import unittest
import uuid

import pytest
import yaml

from interfacetester import loader
from interfacetester.cli import main_convert_alias, main_run
from interfacetester.converters import emit_yaml, postman_adapter
from interfacetester.converters.postman_adapter import DYNAMIC_VARIABLE_MAP
from interfacetester.make import pytest_files_made_cache_mapping, pytest_files_run_set
from interfacetester.utils import HTTP_BIN_URL

REPO = os.getcwd()
POSTMAN_ASSET = os.path.join("examples", "data", "postman", "postman_collection.json")
POSTMAN_ASSET_POSIX = "examples/data/postman/postman_collection.json"
GOLDEN_DIR = os.path.join("tests", "golden", "converters")
SCHEMA_DIR = os.path.join("examples", "data", "postman")


def _tmp_dir(prefix: str = "tmp_pm") -> str:
    path = os.path.join(REPO, "logs", f"{prefix}_{uuid.uuid4().hex[:8]}")
    os.makedirs(path)
    return path


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fp:
        return fp.read()


def _load_asset() -> dict:
    return json.loads(_read(POSTMAN_ASSET))


def _collection_with(items, **extra) -> dict:
    collection = {
        "info": {
            "name": "inline demo",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
    }
    collection.update(extra)
    return collection


def _request_item(name, method="GET", url="https://api.test/x", **request_extra) -> dict:
    request = {"method": method, "url": url}
    request.update(request_extra)
    return {"name": name, "request": request}


class TestPostmanAsset(unittest.TestCase):
    """仓库素材（真实 Collection v2.1）逐项核对。"""

    def setUp(self):
        self.tmp_dir = _tmp_dir()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def convert(self, **kwargs):
        kwargs.setdefault("case_name", "postman_collection")
        return postman_adapter.convert_postman(_load_asset(), **kwargs)

    def test_splits_by_top_level_folder(self):
        cases = self.convert()

        names = [case.name for case in cases]
        # folder1（含嵌套 folder2）、folder3、以及根级请求
        self.assertEqual(len(cases), 3)
        self.assertIn("postman_collection / folder1", names)
        self.assertIn("postman_collection / folder3", names)
        self.assertIn("postman_collection", names)
        self.assertEqual(sum(len(case.steps) for case in cases), 6)

    def test_single_file_mode_merges(self):
        cases = self.convert(split_folders=False)

        self.assertEqual(len(cases), 1)
        self.assertEqual(len(cases[0].steps), 6)
        step_names = [step.name for step in cases[0].steps]
        # 合并模式下步骤名带**完整** folder 路径（嵌套层级不丢）
        self.assertIn("folder1 / folder2 / Get with params", step_names)
        self.assertTrue(any(name.startswith("folder3 / ") for name in step_names))
        self.assertIn("Get request headers", step_names)  # 根级请求没有前缀

    def test_url_object_and_path_variable_resolution(self):
        case = next(c for c in self.convert() if c.name.endswith("folder1"))
        step = case.steps[0]

        # `:path` 用 url.variable 求值成 `get`；启用中的 query 变成 params
        self.assertEqual(case.base_url, "https://postman-echo.com")
        self.assertEqual(step.url, "/get")
        self.assertEqual(step.params, {"k1": "v1", "k2": "v2"})  # k3 是 disabled

    def test_disabled_and_derived_headers(self):
        case = next(c for c in self.convert() if c.name == "postman_collection")
        step = case.steps[0]

        self.assertEqual(step.headers, {"User-Agent": "interfacetester"})  # User-Name 被 disabled
        self.assertTrue(any("派生的请求头" in w for w in step.warnings))

    def test_body_modes(self):
        case = next(c for c in self.convert() if c.name.endswith("folder3"))
        by_name = {step.name: step for step in case.steps}

        form = by_name["Post form-data"]
        self.assertEqual(form.data, {"k1": "v1", "k2": "v2"})
        self.assertEqual(form.upload, {"intro_key": "intro.txt", "logo_key": "logo.jpeg"})

        self.assertEqual(by_name["Post x-www-form-urlencoded"].data, {"k1": "v1", "k2": "v2"})
        self.assertEqual(by_name["Post raw json"].json_body, {"k1": "v1", "k2": "v2"})
        self.assertEqual(by_name["Post raw text"].data, "have a nice day")

    def test_xml_example_response_warns_instead_of_silent_skip(self):
        """0917-1：示例响应是 XML/纯文本时此前**静默**只断状态码（用户以为校验过了）。"""
        item = _request_item("soap query", method="POST", url="https://api.test/svc/query")
        item["response"] = [
            {
                "name": "ok",
                "code": 200,
                "body": '<?xml version="1.0"?><r><code>0000</code></r>',
                "header": [{"key": "Content-Type", "value": "text/xml; charset=utf-8"}],
            }
        ]
        case = postman_adapter.convert_postman(_collection_with([item]))[0]
        warnings = " ".join(case.all_warnings())

        self.assertIn("不是 JSON", warnings)
        self.assertIn("只断状态码", warnings)
        self.assertIn("xpath_match", warnings)

    def test_json_example_response_does_not_get_the_non_json_warning(self):
        item = _request_item("json query", url="https://api.test/orders")
        item["response"] = [
            {"name": "ok", "code": 200, "body": '{"id": 1}', "header": []}
        ]
        case = postman_adapter.convert_postman(_collection_with([item]))[0]
        warnings = " ".join(case.all_warnings())

        self.assertNotIn("不是 JSON", warnings)

    def test_saved_responses_become_assertions(self):
        case = next(c for c in self.convert() if c.name.endswith("folder1"))
        step = case.steps[0]

        self.assertEqual(step.assertions[0], {"eq": ["status_code", 200]})
        self.assertIn("jsonschema_match", json.dumps(step.assertions))
        self.assertTrue(step.schema_file.startswith("schemas/"))
        # 两个示例响应（case1/case2）→ 提示只按第一条生成
        self.assertTrue(any("保存了 2 个示例响应" in w for w in step.warnings))

    def test_status_only_mode(self):
        cases = self.convert(assertion_mode="status")

        asserted = 0
        for case in cases:
            for step in case.steps:
                # 只有「保存过示例响应」的请求才会生成断言；有断言时必须是纯状态码
                if step.assertions:
                    asserted += 1
                    self.assertEqual(step.assertions, [{"eq": ["status_code", 200]}])
            self.assertEqual(case.extra_files, {})
        self.assertGreater(asserted, 0)

    def test_unresolved_path_variable_is_warned(self):
        collection = _collection_with(
            [
                {
                    "name": "unresolved",
                    "request": {
                        "method": "GET",
                        "url": {
                            "protocol": "https",
                            "host": ["api", "test"],
                            "path": [":id"],
                            "variable": [{"key": "id", "value": ""}],
                        },
                    },
                }
            ]
        )
        cases = postman_adapter.convert_postman(collection)

        self.assertTrue(any("路径变量 [':id']" in w for w in cases[0].all_warnings()))
        self.assertIn(":id", cases[0].steps[0].url)


class TestPostmanFeatures(unittest.TestCase):
    """真实集合里常见的写法：变量、动态变量、认证、脚本、分组。"""

    def test_collection_and_item_variables_become_placeholders(self):
        collection = _collection_with(
            [
                _request_item(
                    "with vars",
                    url="https://{{host}}/api/{{version}}/orders",
                    header=[{"key": "X-Tenant", "value": "{{tenant}}"}],
                )
            ],
            variable=[
                {"key": "host", "value": "api.test"},
                {"key": "version", "value": "v1"},
            ],
        )
        case = postman_adapter.convert_postman(collection)[0]
        step = case.steps[0]

        # 只做「写法翻译」，不内联值（值统一放 config.variables，便于按环境覆盖）
        # scheme 是字面量、host 是变量 → base_url 就是 `https://${host}`
        self.assertEqual(case.base_url, "https://${host}")
        self.assertEqual(step.url, "/api/${version}/orders")
        self.assertEqual(step.headers["X-Tenant"], "${tenant}")
        self.assertEqual(case.variables["host"], "api.test")
        self.assertEqual(case.variables["version"], "v1")
        # 集合里没定义 tenant → 占位 + 告警
        self.assertEqual(case.variables["tenant"], "${ENV(tenant)}")
        self.assertTrue(any("变量 tenant 在集合里没有定义" in w for w in case.all_warnings()))

    def test_secret_named_variable_is_not_inlined(self):
        collection = _collection_with(
            [_request_item("login", method="POST", url="https://api.test/login")],
            variable=[{"key": "api_token", "value": "super-secret-value"}],
        )
        case = postman_adapter.convert_postman(collection)[0]

        # 用到才登记；且凭据值必须占位化
        emitted = emit_yaml.dump_case_yaml(case, "x.yml")
        self.assertNotIn("super-secret-value", emitted)

    def test_dynamic_variables_are_mapped(self):
        # 结构化 url 的 query 才会被拆成 params（字符串形态的 URL 保持原样，见支持子集）
        collection = _collection_with(
            [
                {
                    "name": "dynamic",
                    "request": {
                        "method": "GET",
                        "url": {
                            "protocol": "https",
                            "host": ["api", "test"],
                            "path": ["x"],
                            "query": [
                                {"key": "id", "value": "{{$guid}}"},
                                {"key": "ts", "value": "{{$timestamp}}"},
                                {"key": "n", "value": "{{$randomInt}}"},
                                {"key": "o", "value": "{{$randomPhoneNumber}}"},
                            ],
                        },
                    },
                }
            ]
        )
        case = postman_adapter.convert_postman(collection)[0]
        params = case.steps[0].params

        self.assertEqual(params["id"], DYNAMIC_VARIABLE_MAP["$guid"])
        self.assertEqual(params["ts"], DYNAMIC_VARIABLE_MAP["$timestamp"])
        # 0918-7 / L7：Postman `{{$timestamp}}` 是 **10 位秒级**，不得映射成毫秒的 get_timestamp()
        self.assertEqual(DYNAMIC_VARIABLE_MAP["$timestamp"], "${get_timestamp(10)}")
        self.assertEqual(params["n"], DYNAMIC_VARIABLE_MAP["$randomint"])
        # 未映射的 → 占位 + 告警（不能静默产出跑不通的用例）
        self.assertEqual(params["o"], "${PM_RANDOMPHONENUMBER}")
        self.assertEqual(case.variables["PM_RANDOMPHONENUMBER"], "${ENV(PM_RANDOMPHONENUMBER)}")
        self.assertTrue(any("无对应实现" in w for w in case.all_warnings()))
        # 需要 helper 的用例会被识别出来（CLI 用它检查输出目录的 debugtalk.py）
        self.assertEqual(
            postman_adapter.required_helper_functions([case]), ["uuid4_str", "random_int"]
        )

    def test_step_source_is_readable_provenance(self):
        """0918-7 / L6：`source` 应是 `item: <名> / <folder 路径>` 的可读溯源串。

        修复前 f-string 写错位置（`.join` 作用在整个 f-string 上），
        产出形如 `FolderAitem: inner / ` 的拼接垃圾。
        """
        folder_item = {
            "name": "FolderA",
            "item": [_request_item("inner")],
        }
        case = postman_adapter.convert_postman(
            _collection_with([folder_item]), split_folders=True
        )[0]

        self.assertEqual(case.steps[0].source, "item: inner / FolderA")

    def test_bearer_and_basic_auth_become_placeholders(self):
        bearer = _collection_with(
            [_request_item("b", auth={"type": "bearer", "bearer": [{"key": "token", "value": "abc123"}]})]
        )
        case = postman_adapter.convert_postman(bearer)[0]
        self.assertEqual(case.steps[0].headers["Authorization"], "Bearer ${AUTH_TOKEN}")
        self.assertNotIn("abc123", emit_yaml.dump_case_yaml(case, "x.yml"))

        basic = _collection_with(
            [_request_item("b", auth={"type": "basic", "basic": [{"key": "password", "value": "pw"}]})]
        )
        case = postman_adapter.convert_postman(basic)[0]
        self.assertEqual(case.steps[0].headers["Authorization"], "Basic ${AUTH_BASIC}")
        self.assertNotIn("pw\"", emit_yaml.dump_case_yaml(case, "x.yml"))

    def test_collection_level_auth_applies_to_all(self):
        collection = _collection_with(
            [_request_item("a"), _request_item("b")],
            auth={"type": "bearer", "bearer": [{"key": "token", "value": "t"}]},
        )
        cases = postman_adapter.convert_postman(collection)

        for step in cases[0].steps:
            self.assertEqual(step.headers["Authorization"], "Bearer ${AUTH_TOKEN}")

    def test_apikey_and_oauth2_and_unknown_auth_warn(self):
        apikey = _collection_with(
            [
                _request_item(
                    "k",
                    auth={"type": "apikey", "apikey": [{"key": "key", "value": "X-Api-Key"}, {"key": "in", "value": "header"}]},
                )
            ]
        )
        case = postman_adapter.convert_postman(apikey)[0]
        self.assertIn("X-Api-Key", case.steps[0].headers)

        oauth2 = _collection_with([_request_item("o", auth={"type": "oauth2", "oauth2": []})])
        case = postman_adapter.convert_postman(oauth2)[0]
        self.assertTrue(any("oauth2" in w and "config.oauth2" in w for w in case.all_warnings()))

        unknown = _collection_with([_request_item("u", auth={"type": "digest", "digest": []})])
        case = postman_adapter.convert_postman(unknown)[0]
        self.assertTrue(any("未支持" in w for w in case.all_warnings()))

    def test_scripts_cannot_migrate_but_are_reported(self):
        item = _request_item("scripted")
        item["event"] = [
            {
                "listen": "prerequest",
                "script": {"type": "text/javascript", "exec": ["pm.environment.set('token', 'x');"]},
            },
            {
                "listen": "test",
                "script": {"type": "text/javascript", "exec": ["pm.test('ok', () => {});"]},
            },
        ]
        case = postman_adapter.convert_postman(_collection_with([item]))[0]

        warnings = case.all_warnings()
        self.assertTrue(any("无法无损迁移" in w for w in warnings))
        self.assertTrue(any("pm.environment.set" in w for w in warnings))  # 附首行便于人工改写
        self.assertTrue(any("共有 2 段 Postman 脚本" in w for w in warnings))

    def test_non_v21_schema_warns(self):
        collection = _collection_with([_request_item("a")])
        collection["info"]["schema"] = "https://schema.getpostman.com/json/collection/v2.0.0/collection.json"
        case = postman_adapter.convert_postman(collection)[0]

        self.assertTrue(any("v2.0.0" in w for w in case.all_warnings()))

    def test_empty_collection_warns(self):
        cases = postman_adapter.convert_postman(_collection_with([]))

        self.assertEqual(cases[0].steps, [])
        self.assertTrue(any("没有任何请求" in w for w in cases[0].warnings))

    def test_file_mode_and_graphql_warn(self):
        collection = _collection_with(
            [
                _request_item("bin", method="POST", body={"mode": "file", "file": {"src": "x.bin"}}),
                _request_item("gql", method="POST", body={"mode": "graphql", "graphql": {}}),
            ]
        )
        steps = postman_adapter.convert_postman(collection)[0].steps

        self.assertTrue(any("mode=file" in w for w in steps[0].warnings))
        self.assertTrue(any("graphql" in w for w in steps[1].warnings))

    def test_request_as_plain_string(self):
        collection = _collection_with([{"name": "old style", "request": "https://api.test/x"}])
        step = postman_adapter.convert_postman(collection)[0].steps[0]

        self.assertEqual(step.method, "GET")
        self.assertEqual(step.url, "/x")

    def test_not_a_collection_file_raises(self):
        tmp = _tmp_dir()
        try:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as fp:
                fp.write('{"foo": 1}')
            with self.assertRaises(ValueError) as ctx:
                postman_adapter.convert_postman_file(path)
            self.assertIn("Collection v2.1", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPostmanGoldenAndE2E(unittest.TestCase):
    """黄金文件 + 端到端真跑（本地 mock）。"""

    def setUp(self):
        self.tmp_dir = _tmp_dir()
        loader.project_meta = None
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        self._original_argv = sys.argv

    def tearDown(self):
        sys.argv = self._original_argv
        loader.project_meta = None
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_asset_golden(self):
        cases = postman_adapter.convert_postman(
            _load_asset(), case_name="postman_collection"
        )
        actual = {
            emit_yaml.slugify(case.name) + ".yml": emit_yaml.dump_case_yaml(
                case, emit_yaml.slugify(case.name) + ".yml"
            )
            for case in cases
        }
        golden_path = os.path.join(GOLDEN_DIR, "postman_collection.yml")
        if os.environ.get("UPDATE_GOLDEN") == "1" or not os.path.isfile(golden_path):
            os.makedirs(GOLDEN_DIR, exist_ok=True)
            with open(golden_path, "w", encoding="utf-8") as fp:
                fp.write(json.dumps(actual, ensure_ascii=False, indent=2) + "\n")
            self.skipTest(f"已写入黄金文件 {golden_path}")
        self.assertEqual(
            actual, json.loads(_read(golden_path)), "生成结果与黄金文件不一致（UPDATE_GOLDEN=1 可更新）"
        )

    def _run_cli(self, *args) -> int:
        sys.argv = ["hconvert", *args]
        try:
            main_convert_alias()
        except SystemExit as ex:
            return int(ex.code or 0)
        return 0

    def test_cli_end_to_end_against_local_mock(self):
        """构造一个指向本地 mock 的集合（含保存的示例响应）→ 生成 → 真跑通。"""
        probe = f"{HTTP_BIN_URL}/get"
        host, _, port = HTTP_BIN_URL.replace("http://", "").partition(":")
        body = json.dumps({"args": {}, "url": probe})
        collection = {
            "info": {
                "name": "local mock",
                "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            },
            "item": [
                {
                    "name": "get echo",
                    "request": {
                        "method": "GET",
                        "url": {
                            "raw": probe,
                            "protocol": "http",
                            "host": host.split("."),
                            "port": port or None,
                            "path": ["get"],
                        },
                        "header": [{"key": "Accept", "value": "application/json"}],
                    },
                    "response": [
                        {
                            "name": "ok",
                            "code": 200,
                            "body": body,
                            "header": [{"key": "Content-Type", "value": "application/json"}],
                        }
                    ],
                }
            ],
        }
        collection_path = os.path.join(self.tmp_dir, "local_mock.postman_collection.json")
        with open(collection_path, "w", encoding="utf-8") as fp:
            json.dump(collection, fp)

        out_dir = os.path.join(self.tmp_dir, "out")
        code = self._run_cli(
            "--from", "postman", "--in", collection_path, "--out", out_dir, "--name", "pm_smoke"
        )
        self.assertEqual(code, 0)

        yaml_path = os.path.join(out_dir, "pm_smoke.yml")
        self.assertTrue(os.path.isfile(yaml_path))
        generated = yaml.safe_load(_read(yaml_path))
        self.assertEqual(generated["config"]["base_url"], HTTP_BIN_URL)
        self.assertIn("jsonschema_match", json.dumps(generated["teststeps"][0]["validate"]))

        exit_code = main_run([yaml_path, "--import-mode=importlib"])
        self.assertEqual(exit_code, 0)

    def test_cli_report_and_debugtalk_helpers(self):
        collection = _collection_with(
            [_request_item("dynamic", url="https://api.test/x?id={{$guid}}")]
        )
        collection_path = os.path.join(self.tmp_dir, "dyn.json")
        with open(collection_path, "w", encoding="utf-8") as fp:
            json.dump(collection, fp)
        out_dir = os.path.join(self.tmp_dir, "out")
        report_path = os.path.join(self.tmp_dir, "report.md")

        code = self._run_cli(
            "--from", "postman", "--in", collection_path, "--out", out_dir, "--report", report_path
        )
        self.assertEqual(code, 0)

        debugtalk = _read(os.path.join(out_dir, "debugtalk.py"))
        self.assertIn("def uuid4_str", debugtalk)
        self.assertIn("def random_int", debugtalk)
        report = _read(report_path)
        self.assertIn("# 导入报告", report)
        self.assertIn("动态变量", report)

    def test_existing_debugtalk_without_helpers_is_not_rewritten(self):
        """输出目录已有 debugtalk.py 时**不改写**，只告警提示补函数。"""
        out_dir = os.path.join(self.tmp_dir, "out")
        os.makedirs(out_dir)
        existing = os.path.join(out_dir, "debugtalk.py")
        with open(existing, "w", encoding="utf-8") as fp:
            fp.write("def my_helper():\n    return 1\n")

        case = postman_adapter.convert_postman(
            _collection_with([_request_item("d", url="https://api.test/x?id={{$guid}}")])
        )[0]
        written = emit_yaml.emit_case(
            case, out_dir, yaml_name="d.yml", required_functions=["uuid4_str", "random_int"]
        )

        self.assertEqual(_read(existing), "def my_helper():\n    return 1\n")
        self.assertTrue(any("缺少生成物用到的函数" in w for w in written["warnings"]))

    def test_schema_files_are_valid_json(self):
        cases = postman_adapter.convert_postman(_load_asset(), case_name="postman_collection")
        out_dir = os.path.join(self.tmp_dir, "out_schemas")

        for case in cases:
            written = emit_yaml.emit_case(case, out_dir)
            for relative_path, absolute_path in written["extra"].items():
                self.assertTrue(relative_path.endswith(".json"))
                json.loads(_read(absolute_path))  # 必须是真 JSON


class TestBatch0920UrlProtocolAndNonAsciiVariables(unittest.TestCase):
    """0920 批次 4 / **N23 + N24**。

    **N23**：`protocol` 在 Postman v2.1 里是**可选**的（只有 `raw` 必填）。
    修复前是 `url.get("protocol") or "https"`，而"用 `raw` 回填"的分支只在
    `host`/`path` 缺失时触发 —— 于是 host/path 齐全、只缺 protocol 时，
    `raw` 里明明的 `http://` 被丢掉，生成的 `base_url` 变成 `https://`，**零告警**：

    ```text
    url={'raw':'http://127.0.0.1:18080/ok','host':[…],'path':['ok']}
      -> base_url='https://127.0.0.1:18080'   ← 打错协议（本地 http 服务被打死）
    ```

    **N24**：`_to_python_placeholder` 修复前保留 CJK（`\\u4e00-\\u9fff`），
    于是 `{{域名}}` → `${域名}` —— 而运行期变量正则只认 `[A-Za-z_][0-9A-Za-z_]*`，
    这个引用**根本不会被解析**；同时 `_used_variable_names` 的扫描正则同样只认 ASCII，
    于是变量被判"没被引用"、**定义也被剪枝删掉**。两条叠加 = 生成物必然跑不通，
    而导入期零告警（`validate_emitted_case` 按设计不解析变量）。
    """

    def test_protocol_falls_back_to_raw_scheme(self):
        """N23：只缺 `protocol` 时必须看 `raw` 的 scheme，而不是默认 https。"""
        collection = _collection_with(
            [
                _request_item(
                    "http raw",
                    url={
                        "raw": "http://127.0.0.1:18080/ok",
                        "host": ["127", "0", "0", "1"],
                        "port": "18080",
                        "path": ["ok"],
                    },
                )
            ]
        )

        case = postman_adapter.convert_postman(collection)[0]

        self.assertEqual(
            case.base_url,
            "http://127.0.0.1:18080",
            "`raw` 里的 http:// 被忽略、强行按 https 拼（N23）",
        )
        self.assertTrue(
            any("protocol" in warning for warning in case.all_warnings()),
            "回填 scheme 这件事必须可见",
        )

    def test_explicit_protocol_still_wins(self):
        """反向护栏：显式 `protocol` 照旧优先。"""
        collection = _collection_with(
            [
                _request_item(
                    "explicit",
                    url={
                        "raw": "http://127.0.0.1:18080/ok",
                        "protocol": "https",
                        "host": ["127", "0", "0", "1"],
                        "port": "18080",
                        "path": ["ok"],
                    },
                )
            ]
        )

        self.assertEqual(
            postman_adapter.convert_postman(collection)[0].base_url,
            "https://127.0.0.1:18080",
        )

    def test_https_raw_is_unchanged(self):
        """反向护栏：`raw` 是 https 时行为不变。"""
        collection = _collection_with(
            [
                _request_item(
                    "https raw",
                    url={
                        "raw": "https://api.test/ok",
                        "host": ["api", "test"],
                        "path": ["ok"],
                    },
                )
            ]
        )

        self.assertEqual(
            postman_adapter.convert_postman(collection)[0].base_url, "https://api.test"
        )

    def test_non_ascii_variable_becomes_a_resolvable_ascii_name(self):
        """N24：中文变量名必须变成**运行期可解析**的 ASCII 名，且定义不被剪掉。"""
        import re

        from interfacetester.parser import parse_string

        folder = {
            "name": "folder1",
            "variable": [{"key": "域名", "value": "https://api.test"}],
            "item": [
                _request_item(
                    "req",
                    url={
                        "raw": "{{域名}}/ok",
                        "protocol": "https",
                        "host": ["{{域名}}"],
                        "path": ["ok"],
                    },
                )
            ],
        }
        collection = _collection_with([folder])

        case = postman_adapter.convert_postman(collection)[0]

        match = re.search(r"\$\{([^}]+)\}", case.base_url)
        self.assertIsNotNone(match, case.base_url)
        name = match.group(1)

        self.assertRegex(
            name,
            r"^[A-Za-z_][0-9A-Za-z_]*$",
            f"变量名 {name!r} 不是运行期认得的标识符（N24）",
        )
        self.assertIn(
            name,
            case.variables,
            "变量的**定义被剪枝删掉了**（N24 的另一半）",
        )
        resolved = parse_string(case.base_url, {name: case.variables[name]}, {})
        self.assertNotIn("${", resolved, f"运行期没能解析：{resolved!r}")

    def test_ascii_variable_names_are_unchanged(self):
        """反向护栏：ASCII 变量名逐字不变（不许因为修 N24 而改名）。"""
        folder = {
            "name": "folder1",
            "variable": [{"key": "domain", "value": "https://api.test"}],
            "item": [
                _request_item(
                    "req",
                    url={
                        "raw": "{{domain}}/ok",
                        "protocol": "https",
                        "host": ["{{domain}}"],
                        "path": ["ok"],
                    },
                )
            ],
        }

        case = postman_adapter.convert_postman(_collection_with([folder]))[0]

        self.assertEqual(case.base_url, "https://${domain}")
        self.assertIn("domain", case.variables)


class TestBatch0918_8RawOnlyUrl(unittest.TestCase):
    """0918-8 / M24：`url` 只给 `raw` 时必须保住 host 与 path。

    NOTICE（修复前实测）：`url.raw` 是 Postman v2.1 schema 里**唯一必填**的字段，`host`/`path`
    都可选，而第三方导出的集合经常只给 `raw`。修复前 host 缺省成空串、path 缺省成 `/`，
    于是拼出 `https:///?page=2`——host 与 path **双双丢失**，告警只说
    「host（）与 base_url（空）不同」（信息量约等于零），hmake 还能过、exit 0，
    只有真跑时才以一个 InvalidURL 报错。
    """

    def test_raw_only_url_keeps_host_and_path(self):
        collection = _collection_with(
            [
                _request_item(
                    "raw only",
                    url={"raw": "https://api.example.com/v1/things?page=2"},
                )
            ]
        )

        case = postman_adapter.convert_postman(collection)[0]
        step = case.steps[0]

        self.assertEqual(case.base_url, "https://api.example.com")
        self.assertEqual(step.url, "/v1/things?page=2")
        self.assertTrue(
            any("回填" in warning for warning in step.warnings), step.warnings
        )

    def test_host_present_but_path_missing_also_falls_back(self):
        """host 有、path 缺：修复前会丢掉整段路径（只发到 `/`）。"""
        collection = _collection_with(
            [
                _request_item(
                    "host only",
                    url={
                        "raw": "https://api.example.com/v2/items",
                        "host": ["api", "example", "com"],
                    },
                )
            ]
        )

        step = postman_adapter.convert_postman(collection)[0].steps[0]

        self.assertEqual(step.url, "/v2/items")

    def test_fully_structured_url_is_unchanged(self):
        """反向：结构化字段齐全时一字不改（零回归），且**不产生**回填告警。"""
        collection = _collection_with(
            [
                _request_item(
                    "structured",
                    url={
                        "raw": "https://api.example.com/login?x=1",
                        "protocol": "https",
                        "host": ["api", "example", "com"],
                        "path": ["login"],
                        "query": [{"key": "x", "value": "1"}],
                    },
                )
            ]
        )

        step = postman_adapter.convert_postman(collection)[0].steps[0]

        # NOTICE: 结构化 `url.query` 存在时会被拆进 `params`（既有设计），
        # 所以这里断的是「路径正确 + 查询进了 params」，而不是把查询串留在 URL 里。
        self.assertEqual(step.url, "/login")
        self.assertEqual(step.params, {"x": "1"})
        self.assertFalse(
            [warning for warning in step.warnings if "回填" in warning], step.warnings
        )


class TestBatch0918_8PostmanFixes(unittest.TestCase):
    """批次 8-6（跨源语义对照夹具）带出的两条 Postman 缺陷。

    NOTICE: 这两条都是**同一请求、三种源、三种结果**——三个适配器各自都有测试，
    但没有任何测试把它们放在一起比，所以差异能长期共存。跨源夹具见
    `tests/converters_cross_source_test.py`。
    """

    def test_raw_json_body_without_language_is_structured(self):
        """H12（高·凭据明文）：`mode=raw` 的 JSON 判定不能只看 `options.raw.language`。

        `options.raw.language` 是 Postman 的**可选提示**，导出的集合里经常缺失或写成
        `text`/`javascript`；`Content-Type: application/json` 才是可靠信号（curl/HAR 都这么判）。
        修复前不看 Content-Type → JSON 体被当原始字符串放进 `data` → `sanitize_body`
        只认 dict/list、字符串体走的是 `a=b&c=d` 那条路 → 体里的 `password`/`token`
        **明文写进 YAML**。
        """
        collection = _collection_with(
            [
                _request_item(
                    "raw json without language",
                    method="POST",
                    url="https://api.example.com/login",
                    header=[{"key": "Content-Type", "value": "application/json"}],
                    body={
                        "mode": "raw",
                        "raw": json.dumps(
                            {"username": "alice", "password": "s3cr3t-pw", "token": "tok-123"}
                        ),
                    },
                )
            ]
        )

        case = postman_adapter.convert_postman(collection, case_name="login")[0]
        step = case.steps[0]

        self.assertEqual(
            step.json_body,
            {"username": "alice", "password": "${BODY_PASSWORD}", "token": "${BODY_TOKEN}"},
        )
        self.assertIsNone(step.data)
        self.assertTrue(
            any("Content-Type" in warning for warning in step.warnings),
            "按 Content-Type 判定这件事要告警说明（与 language 不一致时）",
        )
        yaml_text = emit_yaml.dump_case_yaml(case, "login.yml")
        self.assertNotIn("s3cr3t-pw", yaml_text)
        self.assertNotIn("tok-123", yaml_text)

    def test_language_json_still_structured(self):
        """回归：显式 `language=json`（仓库素材就是这种）行为不变，且不产生新告警。"""
        collection = _collection_with(
            [
                _request_item(
                    "declared json",
                    method="POST",
                    url="https://api.example.com/login",
                    body={
                        "mode": "raw",
                        "raw": json.dumps({"a": 1}),
                        "options": {"raw": {"language": "json"}},
                    },
                )
            ]
        )

        step = postman_adapter.convert_postman(collection)[0].steps[0]

        self.assertEqual(step.json_body, {"a": 1})
        self.assertIsNone(step.data)
        self.assertFalse(
            [warning for warning in step.warnings if "Content-Type" in warning],
            step.warnings,
        )

    def test_non_json_raw_body_stays_text(self):
        """反向：Content-Type 不是 JSON 时仍按原始字符串放进 `data`（不能被误结构化）。"""
        collection = _collection_with(
            [
                _request_item(
                    "raw text",
                    method="POST",
                    url="https://api.example.com/upload",
                    header=[{"key": "Content-Type", "value": "text/plain"}],
                    body={"mode": "raw", "raw": '{"a": 1}'},
                )
            ]
        )

        step = postman_adapter.convert_postman(collection)[0].steps[0]

        self.assertIsNone(step.json_body)
        self.assertEqual(step.data, '{"a": 1}')

    def test_cookie_header_becomes_cookies(self):
        """M33（中·静默丢字段）：Postman 的 cookie 唯一载体是 `Cookie` 头，不能被当派生头丢掉。

        修复前：`Cookie` 在 `DERIVED_HEADERS` 里（那条规则是为 HAR/curl 写的，它们另有
        `request.cookies` / `-b` 兜住）→ Postman 复用后 cookie **凭空消失**，
        生成的用例发的是另一个请求，且零报错。
        """
        collection = _collection_with(
            [
                _request_item(
                    "with cookie",
                    url="https://api.example.com/orders",
                    header=[
                        {"key": "Accept", "value": "application/json"},
                        {"key": "Cookie", "value": "session=abc123; theme=dark"},
                    ],
                )
            ]
        )

        step = postman_adapter.convert_postman(collection)[0].steps[0]

        self.assertEqual(
            step.cookies,
            {"session": "${COOKIE_SESSION}", "theme": "${COOKIE_THEME}"},
        )
        self.assertNotIn("Cookie", step.headers)
        self.assertTrue(
            any("Cookie" in warning for warning in step.warnings), step.warnings
        )

    def test_cookie_header_and_request_cookie_are_merged(self):
        """`Cookie` 头与 `request.cookie` 数组同时存在时**合并**，冲突时采用请求头的值并告警。"""
        collection = _collection_with(
            [
                _request_item(
                    "merged cookie",
                    url="https://api.example.com/orders",
                    header=[{"key": "Cookie", "value": "session=from-header"}],
                    cookie=[
                        {"name": "session", "value": "from-array"},
                        {"name": "extra", "value": "e-1"},
                    ],
                )
            ]
        )

        step = postman_adapter.convert_postman(collection)[0].steps[0]

        self.assertEqual(len(step.cookies), 2)
        self.assertIn("extra", step.cookies)
        self.assertEqual(step.cookies["session"], "${COOKIE_SESSION}")
        self.assertTrue(
            any("不一致" in warning for warning in step.warnings),
            f"两个载体冲突时要告警：{step.warnings}",
        )

    def test_unparsable_cookie_header_warns(self):
        collection = _collection_with(
            [
                _request_item(
                    "bad cookie",
                    url="https://api.example.com/orders",
                    header=[{"key": "Cookie", "value": "not-a-pair"}],
                )
            ]
        )

        step = postman_adapter.convert_postman(collection)[0].steps[0]

        self.assertEqual(step.cookies, {})
        self.assertTrue(
            any("无法解析" in warning for warning in step.warnings), step.warnings
        )


class TestBatch0919_2PathVariableSubstitution(unittest.TestCase):
    """0919-2 / 缺陷 3：Postman 路径变量替换必须**单趟**、有边界。

    ## 修复前的现场（实测）

    ```python
    for match in PATH_VARIABLE_PATTERN.finditer(path):
        ...
        path = path.replace(f":{name}", str(value).lstrip("/"))   # ← 全串无边界替换
    ```

    `str.replace` 的语义是「把整串里所有这个子串都换掉」，于是：

    | 输入 | 修复前产出 | 应该是什么 |
    |---|---|---|
    | `/users/:id/:ids`（id=5, ids=7） | `/users/5/5s` | `/users/5/7` |
    | `/things/:type/:types`（type=car, types=suv） | `/things/car/cars` | `/things/car/suv` |

    `:id` 的替换把 `:ids` 的**前半段**打掉了、尾巴 `s` 留在原地 —— 请求被发到**另一个路径**上，
    且 `warnings` 是空的（占位符「都被替换过」，看起来一切正常）。`:id`/`:ids` 这类
    前缀变量并存恰恰是批量操作接口的常见形态（`/x/:id` 单条、`/x/:ids` 批量）。

    ## 还有一个更隐蔽的后果

    替换进来的**值本身**会被后续轮次再替换：`a` 的值就是字面量 `":b"` 时，
    它在第 1 轮被写进 path，第 2 轮又被 `b` 的值覆盖掉。单趟替换之后，
    被插入的文本不再进入扫描面。

    NOTICE（判据本身没问题）：`PATH_VARIABLE_PATTERN` 匹配的是**完整标识符**
    （`[A-Za-z_][A-Za-z0-9_]*`），`:ids` 一直是作为**一个 token** 被识别出来的 ——
    错的是替换，不是识别。所以本批没有动那条正则。
    """

    def _path_of(self, path_segments, variables):
        collection = _collection_with(
            [
                _request_item(
                    "path vars",
                    url={
                        "protocol": "https",
                        "host": ["api", "example", "com"],
                        "path": path_segments,
                        "variable": variables,
                    },
                )
            ]
        )
        return postman_adapter.convert_postman(collection)[0].steps[0]

    def test_id_and_ids_do_not_contaminate_each_other(self):
        """缺陷 3 的核心现场：`:id` 不得吃掉 `:ids` 的前缀。"""
        step = self._path_of(
            ["users", ":id", ":ids"],
            [{"key": "id", "value": "5"}, {"key": "ids", "value": "7"}],
        )

        self.assertEqual(step.url, "/users/5/7")
        self.assertFalse(
            [warning for warning in step.warnings if "路径变量" in warning],
            f"两个变量都有值，不该产生路径变量告警：{step.warnings}",
        )

    def test_type_and_types_do_not_contaminate_each_other(self):
        """同形态的另一组（`:type`/`:types`），确认不是只修了 `id` 这一个名字。"""
        step = self._path_of(
            ["things", ":type", ":types"],
            [{"key": "type", "value": "car"}, {"key": "types", "value": "suv"}],
        )

        self.assertEqual(step.url, "/things/car/suv")

    def test_prefix_variable_order_does_not_matter(self):
        """长名在前、短名在后也必须正确（修复前「谁先出现」会改变被污染的对象）。"""
        step = self._path_of(
            ["batch", ":ids", "one", ":id"],
            [{"key": "id", "value": "5"}, {"key": "ids", "value": "7"}],
        )

        self.assertEqual(step.url, "/batch/7/one/5")

    def test_inserted_value_is_not_rescanned(self):
        """单趟语义：被插入的值即使形如 `:placeholder` 也不得被后续轮次二次替换。"""
        step = self._path_of(
            ["x", ":a", ":b"],
            [{"key": "a", "value": ":b"}, {"key": "b", "value": "BEE"}],
        )

        # `a` 的值是**字面量** ":b"，原样保留；只有原本就在 path 里的 `:b` 被替换。
        # 修复前这里会得到 "/x/BEE/BEE"（插入的 ":b" 被第 2 轮吃掉）。
        self.assertEqual(step.url, "/x/:b/BEE")

    def test_repeated_same_variable_is_replaced_everywhere(self):
        """反向护栏：同一个变量出现多次时，每一处都要替换（别修成「只换第一处」）。"""
        step = self._path_of(
            ["a", ":id", "b", ":id"],
            [{"key": "id", "value": "9"}],
        )

        self.assertEqual(step.url, "/a/9/b/9")

    def test_lone_variable_still_works(self):
        """反向护栏：最普通的单变量场景零变化（这是绝大多数真实集合的形态）。"""
        step = self._path_of(
            ["v1", "users", ":id"],
            [{"key": "id", "value": "42"}],
        )

        self.assertEqual(step.url, "/v1/users/42")

    def test_unresolved_placeholder_survives_intact(self):
        """未解析的占位符必须**原样保留**——告警让用户手工改，改的对象得还在。

        NOTICE: 修复前 `:ids` 无值时，`:id` 那一轮已经把它改成了 `5s`，
        于是用户按告警去文件里找 `:ids` 是**找不到的**（占位符已经被毁）。
        这条把「告警指出的东西确实还在产物里」钉住。
        """
        step = self._path_of(
            ["users", ":id", ":ids"],
            [{"key": "id", "value": "5"}],  # ids 没有值
        )

        self.assertEqual(step.url, "/users/5/:ids")
        self.assertTrue(
            any(":ids" in warning for warning in step.warnings), step.warnings
        )

    def test_leading_slash_in_value_is_still_stripped(self):
        """既有语义（`lstrip("/")`）不能在重写替换逻辑时丢掉。"""
        step = self._path_of(
            ["users", ":id", "detail"],
            [{"key": "id", "value": "/sub/path"}],
        )

        self.assertEqual(step.url, "/users/sub/path/detail")

    def test_query_array_is_not_touched_by_path_variable_logic(self):
        """反向护栏：查询串不参与路径变量替换（`:x` 出现在 query 里不得被换）。"""
        collection = _collection_with(
            [
                _request_item(
                    "query keeps colon",
                    url={
                        "protocol": "https",
                        "host": ["api", "example", "com"],
                        "path": ["search"],
                        "query": [{"key": "filter", "value": "time:12:30"}],
                        "variable": [{"key": "id", "value": "5"}],
                    },
                )
            ]
        )

        step = postman_adapter.convert_postman(collection)[0].steps[0]

        self.assertEqual(step.url, "/search")
        self.assertEqual(step.params, {"filter": "time:12:30"})


class TestBatch0919_2DuplicateQueryKeys(unittest.TestCase):
    """0919-2 / 缺陷 8：Postman `url.query` 的重名键不得被静默压成一个。

    ## 现场（实测）

    ```python
    for entry in url.get("query") or []:
        ...
        params[str(key)] = entry.get("value", "")   # ← 无条件覆盖
    ```

    `?ids=1&ids=2&ids=3`（批量删除接口的常见形态）于是只剩 `{'ids': '3'}`，
    而 `warnings` 是**空的**（实测）。回放时批量删除**少删两条**，零信号。

    与 HAR 的口径对照（同一个仓库、同一类输入）：HAR `queryString` 重名时
    **告警 + 把查询串留在 URL 里**（M33 的正面示范），`postData.params` 和这里的
    `url.query` 却是静默覆盖 —— 三种口径，其中两种是错的。
    修法就是统一到 M33 那条：**字典装不下重名，就不拆成字典**。
    """

    def _step(self, query, raw=None, path=None):
        url = {
            "protocol": "https",
            "host": ["api", "test"],
            "path": path or ["batch", "delete"],
            "query": query,
        }
        if raw is not None:
            url["raw"] = raw
        collection = _collection_with([_request_item("batch delete", url=url)])
        return postman_adapter.convert_postman(collection)[0].steps[0]

    def test_duplicate_query_keys_are_kept_in_the_url(self):
        step = self._step(
            [
                {"key": "ids", "value": "1"},
                {"key": "ids", "value": "2"},
                {"key": "ids", "value": "3"},
            ]
        )

        self.assertEqual(step.url, "/batch/delete?ids=1&ids=2&ids=3")
        self.assertEqual(step.params, {}, "重名键装不进 dict，不该假装装下了")
        self.assertTrue(
            any("重名" in warning for warning in step.warnings),
            f"重名必须告警（修复前这里是零信号）：{step.warnings}",
        )

    def test_raw_query_string_is_reused_verbatim_when_available(self):
        """有 `url.raw` 时用它（录到的就是它），不做无谓的重新编码。"""
        step = self._step(
            [{"key": "ids", "value": "1"}, {"key": "ids", "value": "2"}],
            raw="https://api.test/batch/delete?ids=1&ids=2",
        )

        self.assertEqual(step.url, "/batch/delete?ids=1&ids=2")

    def test_query_string_is_reconstructed_when_raw_is_absent(self):
        """没有 `raw`（只有结构化 query）时按顺序重建，信息同样不丢。"""
        step = self._step(
            [{"key": "ids", "value": "1"}, {"key": "ids", "value": "2"}],
            path=["v2", "batch"],
        )

        self.assertEqual(step.url, "/v2/batch?ids=1&ids=2")

    def test_unique_query_keys_still_become_params(self):
        """反向护栏：没有重名时行为**完全不变**（照旧拆成 `params` dict）。"""
        step = self._step(
            [{"key": "page", "value": "2"}, {"key": "size", "value": "10"}]
        )

        self.assertEqual(step.params, {"page": "2", "size": "10"})
        self.assertEqual(step.url, "/batch/delete")
        self.assertFalse(
            [warning for warning in step.warnings if "重名" in warning], step.warnings
        )

    def test_disabled_duplicate_entry_does_not_trigger_the_fallback(self):
        """反向护栏：`disabled: true` 的条目本来就被跳过，不该因此判定为重名。

        假报会让维护者整体无视这条告警 —— 那是闸门的头号死因。
        """
        step = self._step(
            [
                {"key": "ids", "value": "1"},
                {"key": "ids", "value": "2", "disabled": True},
            ]
        )

        self.assertEqual(step.params, {"ids": "1"})
        self.assertEqual(step.url, "/batch/delete")
        self.assertFalse(
            [warning for warning in step.warnings if "重名" in warning], step.warnings
        )

    def test_repo_asset_query_is_unchanged(self):
        """回归：仓库自带 Collection 的查询串（无重名）逐字不变。"""
        with open(POSTMAN_ASSET, encoding="utf-8") as fp:
            collection = json.load(fp)

        cases = postman_adapter.convert_postman(collection, case_name="postman_demo")
        params = [step.params for case in cases for step in case.steps]

        self.assertTrue(
            any(params), "素材里应当有带查询参数的请求（否则这条回归是空跑）"
        )
        self.assertTrue(
            all(isinstance(item, dict) for item in params),
            "无重名的查询串必须照旧拆成 dict",
        )


class TestBatch0920DuplicateFormBodyKeys(unittest.TestCase):
    """0920 批次 2 / **N2**：Postman **请求体**（urlencoded / formdata）的重名字段
    同样不能静默压成一个 —— 同包的 `duplicated_names` 早就存在，
    `url.query` / HAR `postData.params` 都接了，唯独请求体这两处漏了。

    ## 修复前的现场（`.tmp_audit/verify_sub_f1_f3.py`）

    ```text
    urlencoded: [{ids:1},{ids:2},{ids:3},{keep:x}]
      -> IR data = {'ids': '3', 'keep': 'x'}     （只留最后一个）
      -> requests 实际发出 ids=3&keep=x          （录制里是 ids=1&ids=2&ids=3&keep=x）
      -> warnings = []                           ← 零信号
    ```

    回放**少发字段**、用例照旧绿 —— 正是本仓要消灭的"静默丢数据"。
    """

    def _step(self, body):
        collection = _collection_with(
            [_request_item("form", method="POST", body=body)]
        )
        return postman_adapter.convert_postman(collection)[0].steps[0]

    def test_urlencoded_duplicate_keys_warn_and_keep_original_pairs(self):
        step = self._step(
            {
                "mode": "urlencoded",
                "urlencoded": [
                    {"key": "ids", "value": "1"},
                    {"key": "ids", "value": "2"},
                    {"key": "ids", "value": "3"},
                    {"key": "keep", "value": "x"},
                ],
            }
        )

        # 字典形态的既有行为不变（只有最后一个值），但必须**告警**
        self.assertEqual(step.data, {"ids": "3", "keep": "x"})
        warnings = "\n".join(step.warnings)
        self.assertIn("重名", warnings, f"重名字段必须告警：{step.warnings}")
        self.assertIn("ids", warnings)
        # 告警要给出原始键值对与可操作的改法
        self.assertIn("ids=1", warnings)
        self.assertIn("ids=2", warnings)
        self.assertIn("data:", warnings)

    def test_formdata_duplicate_keys_warn(self):
        step = self._step(
            {
                "mode": "formdata",
                "formdata": [
                    {"key": "tag", "value": "a", "type": "text"},
                    {"key": "tag", "value": "b", "type": "text"},
                ],
            }
        )

        self.assertEqual(step.data, {"tag": "b"})
        self.assertIn("重名", "\n".join(step.warnings))

    def test_formdata_duplicate_file_fields_warn(self):
        """`upload` 也是 dict —— 同名的多个文件字段同样只能留一个。"""
        step = self._step(
            {
                "mode": "formdata",
                "formdata": [
                    {"key": "doc", "type": "file", "src": "a.pdf"},
                    {"key": "doc", "type": "file", "src": "b.pdf"},
                ],
            }
        )

        self.assertEqual(step.upload, {"doc": "b.pdf"})
        self.assertIn("重名", "\n".join(step.warnings))

    def test_no_warning_without_duplicates(self):
        """反向护栏：没有重名时不许新增噪音告警。"""
        step = self._step(
            {
                "mode": "urlencoded",
                "urlencoded": [
                    {"key": "a", "value": "1"},
                    {"key": "b", "value": "2"},
                ],
            }
        )

        self.assertEqual(step.data, {"a": "1", "b": "2"})
        self.assertEqual(
            [w for w in step.warnings if "重名" in w], [], step.warnings
        )

    def test_disabled_entries_do_not_count_as_duplicates(self):
        """反向护栏：`disabled: true` 的条目本来就被跳过，不算重名。"""
        step = self._step(
            {
                "mode": "urlencoded",
                "urlencoded": [
                    {"key": "a", "value": "1"},
                    {"key": "a", "value": "2", "disabled": True},
                ],
            }
        )

        self.assertEqual(step.data, {"a": "1"})
        self.assertEqual(
            [w for w in step.warnings if "重名" in w], [], step.warnings
        )


def _collection_with_saved_responses(*responses_per_item):
    """每个参数是一个 item 的示例响应列表。"""
    items = []
    for index, responses in enumerate(responses_per_item):
        items.append(
            {
                "name": f"thing{index}",
                "request": {
                    "method": "GET",
                    "url": f"https://api.test/thing{index}",
                },
                "response": responses,
            }
        )
    return {
        "info": {
            "name": "codes",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
    }


class TestBatch0919_17NoUsableCodeAtAll(unittest.TestCase):
    """0919-17 / ②：「存了示例响应，但没有一条可用」→ 必须有一条结果级告警。

    0919-7 拍板的边界是「**单条** code 缺失不打『坏数据』告警」（部分缺失是常态，
    逐条告警会刷屏）。本批补的是另一面：**全部**不可用 → 这个请求一条断言都没生成，
    修复前零信号——用户以为导入"正常"，实际这个请求什么都没断。

    刻意**不**顺手自动生成形状断言（行为扩张，可能把通过的用例改红，
    见 docs/能力清单.md 存量资产导入边界表）：要形状断言请补 code 或手工加
    `jsonschema_match`。
    """

    def _steps(self, *responses_per_item):
        cases = postman_adapter.convert_postman(
            _collection_with_saved_responses(*responses_per_item)
        )
        return [step for case in cases for step in case.steps]

    def test_all_codes_missing_warns_and_generates_no_assertions(self):
        steps = self._steps([{"name": "s", "body": '{"a": 1}'}])
        step = steps[0]

        self.assertEqual(step.assertions, [])
        self.assertTrue(
            any("未生成任何断言" in warning for warning in step.warnings),
            f"全部不可用必须有结果级告警：{step.warnings}",
        )

    def test_no_saved_responses_stays_silent(self):
        """没存示例响应是常态（官方集合大量请求没有 saved example）→ 完全静默。"""
        steps = self._steps([])

        self.assertEqual(steps[0].assertions, [])
        self.assertFalse(
            [w for w in steps[0].warnings if "未生成任何断言" in w], steps[0].warnings
        )

    def test_no_duplicate_warning_when_malformed_already_warned(self):
        """code 是坏数据时 malformed 告警已覆盖「没生成断言」的语义，不重复打第二条。"""
        steps = self._steps([{"name": "weird", "code": "200 OK", "body": "{}"}])
        warnings = steps[0].warnings

        self.assertTrue(any("不是数字" in w for w in warnings))
        self.assertFalse([w for w in warnings if "未生成任何断言" in w], warnings)

    def test_partially_usable_only_warns_for_the_unusable_one(self):
        """一条缺失 + 一条可用：可用的照常生成断言；全缺失的那条才有结果级告警。"""
        steps = self._steps(
            [{"name": "no-code", "body": "{}"}],
            [{"name": "ok", "code": 200, "body": '{"a": 1}'}],
        )
        self.assertEqual(len(steps), 2)

        self.assertIn({"eq": ["status_code", 200]}, steps[1].assertions)
        self.assertFalse([w for w in steps[1].warnings if "未生成任何断言" in w])

        self.assertTrue(any("未生成任何断言" in w for w in steps[0].warnings))
        # 0919-7 的边界保持：单条缺失不打「坏数据」告警
        self.assertFalse([w for w in steps[0].warnings if "不是数字" in w])


class TestBatch0919_7SavedResponseCode(unittest.TestCase):
    """0919-2 / 缺陷 9：示例响应的 `code` 不是数字时，**不能杀掉整批导入**。

    ## 现场（实测，修复前）

    ```python
    usable = [r for r in responses if isinstance(r, dict) and int(r.get("code") or 0) > 0]
    ...
    assertions.append({"eq": ["status_code", int(primary.get("code"))]})
    ```

    官方导出给的是数字，但**手改/第三方工具**的产物可能是 `"200 OK"`。实测：

    ```text
    code="200 OK" -> ValueError: invalid literal for int() with base 10: '200 OK'
    走 hconvert     -> ERROR | 解析 <文件> 失败：ValueError: ...
                       SystemExit 2，--out 目录**根本没有被创建**（一个用例都不产出）
    ```

    一个坏示例响应 **杀掉整个文件**，连同一份集合里完好的请求一起丢了，
    而报错信息里完全没有「示例响应的 code」这个线索。

    ## 修法：区分「没有」与「坏了」

    - `code` **缺失/为空**（录制里本来就没有状态码）→ 静默跳过（**既有行为**，不动）；
    - `code` **有值但不是数字**（坏数据）→ 告警**并只跳过那一条**响应，其余照常。
    """

    def _steps(self, *responses_per_item):
        """展平所有用例的所有步骤（无 folder 时顶层 item 会归到**一个**用例里）。"""
        cases = postman_adapter.convert_postman(
            _collection_with_saved_responses(*responses_per_item)
        )
        return [step for case in cases for step in case.steps]

    def test_malformed_code_warns_instead_of_raising(self):
        steps = self._steps([{"name": "weird", "code": "200 OK", "body": '{"a": 1}'}])
        step = steps[0]

        self.assertEqual(step.assertions, [])
        self.assertTrue(
            any(
                "code" in warning and "不是数字" in warning for warning in step.warnings
            ),
            f"坏 code 必须告警：{step.warnings}",
        )

    def test_warning_names_the_response_and_the_raw_value(self):
        steps = self._steps([{"name": "weird", "code": "200 OK", "body": "{}"}])
        warnings = "\n".join(steps[0].warnings)

        self.assertIn("weird", warnings)
        self.assertIn("200 OK", warnings)

    def test_sibling_requests_survive_a_malformed_code(self):
        """**核心**：同一份集合里，坏示例响应不得带走其它请求。"""
        steps = self._steps(
            [{"name": "ok", "code": 200, "body": '{"a": 1}'}],
            [{"name": "weird", "code": "200 OK", "body": '{"a": 1}'}],
        )

        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].assertions[0], {"eq": ["status_code", 200]})
        self.assertEqual(steps[1].assertions, [])

    def test_malformed_second_response_keeps_the_first_one(self):
        steps = self._steps(
            [
                {"name": "good", "code": 201, "body": '{"a": 1}'},
                {"name": "weird", "code": "200 OK", "body": '{"a": 1}'},
            ]
        )
        step = steps[0]

        self.assertIn({"eq": ["status_code", 201]}, step.assertions)
        self.assertTrue(any("不是数字" in warning for warning in step.warnings))

    def test_numeric_string_code_still_works(self):
        """反向护栏：Postman 自己有时给数字字符串，`"200"` 必须照旧可用。"""
        steps = self._steps([{"name": "s", "code": "200", "body": '{"a": 1}'}])

        self.assertIn({"eq": ["status_code", 200]}, steps[0].assertions)

    def test_int_code_is_completely_unchanged(self):
        """反向护栏：最常见的 int code 行为零变化（状态码 + 形状断言都在）。"""
        steps = self._steps([{"name": "s", "code": 200, "body": '{"a": 1}'}])
        step = steps[0]

        self.assertIn({"eq": ["status_code", 200]}, step.assertions)
        self.assertTrue(
            any("jsonschema_match" in json.dumps(item) for item in step.assertions)
        )
        self.assertFalse(
            [warning for warning in step.warnings if "不是数字" in warning],
            step.warnings,
        )

    def test_missing_or_empty_code_is_still_skipped_silently(self):
        """反向护栏（0919-7 的边界，0919-17 细化）：**没有** code 与**坏** code 是两回事。

        单条响应的 code 缺失 / `None` / 空串 → **不打「不是数字」告警**
        （把缺失报成坏数据会在合法素材上刷假报）。
        0919-17 的细化：当**全部**示例响应都不可用、一条断言都没生成时，
        有一条「未生成任何断言」的**结果级**告警（见 TestBatch0919_17NoUsableCodeAtAll）
        ——它报的是「结果与预期不符」，不是把缺失报成坏数据，两者不冲突。
        没存示例响应（`response: []`）则完全静默（那是常态）。
        """
        for responses in (
            [],
            [{"name": "s", "body": "{}"}],
            [{"name": "s", "code": None}],
            [{"name": "s", "code": ""}],
        ):
            with self.subTest(responses=responses):
                steps = self._steps(responses)

                self.assertEqual(steps[0].assertions, [])
                self.assertFalse(
                    [w for w in steps[0].warnings if "不是数字" in w], steps[0].warnings
                )

    def test_zero_code_is_not_reported_as_malformed(self):
        """`code: 0` 是数字 → **不能**报成「不是数字」（0919-7 的边界仍成立）。

        0919-26 / L7 起它的处置变了（原先完全静默、现有一条「不是正数」告警，
        见 `TestBatch0919_26NonPositiveResponseCode`），但「是不是坏数据」这条判据没变：
        见 `probe_l7.py` 的汇总表（`code=0` 一行：`非数字告警=False`）。
        """
        steps = self._steps([{"name": "s", "code": 0, "body": "{}"}])

        self.assertEqual(steps[0].assertions, [])
        self.assertFalse(
            [w for w in steps[0].warnings if "不是数字" in w], steps[0].warnings
        )

    def test_cli_conversion_succeeds_and_writes_the_file(self):
        """端到端：坏 code 不再让 `hconvert` 整批失败（修复前 `--out` 目录都不会创建）。"""
        tmp_dir = _tmp_dir("tmp_pm_code")
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        source = os.path.join(tmp_dir, "codes.json")
        out_dir = os.path.join(tmp_dir, "out")
        with open(source, "w", encoding="utf-8") as fp:
            json.dump(
                _collection_with_saved_responses(
                    [{"name": "ok", "code": 200, "body": '{"a": 1}'}],
                    [{"name": "weird", "code": "200 OK", "body": '{"a": 1}'}],
                ),
                fp,
            )

        original_argv = sys.argv
        self.addCleanup(setattr, sys, "argv", original_argv)
        sys.argv = [
            "hconvert",
            "--from",
            "postman",
            "--in",
            source,
            "--out",
            out_dir,
        ]
        try:
            main_convert_alias()
        except SystemExit as ex:
            exit_code = int(ex.code or 0)
        else:
            exit_code = 0

        self.assertEqual(
            exit_code, 0, "坏 code 又让 hconvert 整批失败了（修复前是 SystemExit 2）"
        )
        generated = [name for name in os.listdir(out_dir) if name.endswith(".yml")]
        self.assertTrue(generated, "没有产出任何用例文件")


class TestBatch0919_26NonPositiveResponseCode(unittest.TestCase):
    """批次 8 / **L7**：`code` 是数字但**不是正数**的示例响应，修复前**完全静默**。

    ## 现场（实测，修复前）

    判据是 `int(r.get("code") or 0) > 0`（后改为 `numeric_code > 0`），于是 `code: 0` /
    `-1` / `"0"` 落进「既不算可用、也不算坏数据」的缝里：

    - 进不了 `usable` → 不生成任何断言；
    - 不满足 `raw_code in (None, "")` → 不走「缺失静默跳过」；
    - `int()` 成功 → 不进 `malformed` → 没有坏数据告警。

    结果是「零断言 + **零信号**」；而唯一的兜底告警（全部不可用那条）还会把它
    归因成「code **缺失/为空**」——用户照提示去补 code，可他的 code 明明写了。

    ## 修法（三处，各自可注入验证）

    1. 新增 `non_positive` 桶：`numeric_code > 0` 之外**单独**收，不再静默丢弃；
    2. 一条**点名**告警（列出响应名与实际的 `0` / `-1`），并说明「占位值不能当期望」；
    3. 结果级告警的文案订正为「缺失/为空、**不是正数**，或条目形态不对」，
       且抑制条件改**精确**——malformed 与非正数同时存在时，
       malformed 那句「断言只按剩余可用响应生成」就是**假承诺**（剩余可用 = 0 条），
       必须补发结果级告警。
    """

    def _steps(self, *responses_per_item):
        cases = postman_adapter.convert_postman(
            _collection_with_saved_responses(*responses_per_item)
        )
        return [step for case in cases for step in case.steps]

    def _non_positive_warnings(self, step):
        """只取**点名**那条非正数告警（`有 N 个示例响应的 code 不是正数 → 已跳过`）。

        注意不能只判 `"不是正数" in w`：结果级告警（`该请求保存了…`）的文案里
        **也有**这个词（这正是文案订正的另一半），两者会被混成一条。
        """
        return [w for w in step.warnings if "不是正数" in w and w.startswith("有 ")]

    def test_zero_code_is_named_in_a_warning(self):
        """**核心**：`code: 0` 不再静默——有一条点名告警（响应名 + 实际值）。"""
        steps = self._steps([{"name": "zero", "code": 0, "body": "{}"}])
        step = steps[0]

        self.assertEqual(step.assertions, [], "非正数不能生成状态码断言")
        warnings = self._non_positive_warnings(step)
        self.assertEqual(len(warnings), 1, f"必须有唯一的非正数告警：{step.warnings}")
        self.assertIn("zero", warnings[0], "告警要点名是哪条响应")
        self.assertIn("0", warnings[0], "告警要给出实际的值")

    def test_negative_and_numeric_string_values_take_the_same_path(self):
        """`-1` / `"-1"` / `"0"` 与 `0` 同桶（`int()` 之后统一判 `> 0`）。"""
        for code in (0, -1, "-1", "0"):
            with self.subTest(code=code):
                step = self._steps([{"name": "s", "code": code, "body": "{}"}])[0]

                self.assertEqual(step.assertions, [])
                self.assertEqual(
                    len(self._non_positive_warnings(step)),
                    1,
                    f"code={code!r} 必须落进非正数桶：{step.warnings}",
                )

    def test_valid_sibling_still_generates_its_assertions(self):
        """**核心**：一条非正数不得带走同请求里可用的那条（与 L5 同族口径）。"""
        step = self._steps(
            [
                {"name": "ok", "code": 200, "body": '{"a": 1}'},
                {"name": "zero", "code": 0, "body": "{}"},
            ]
        )[0]

        self.assertIn({"eq": ["status_code", 200]}, step.assertions)
        self.assertEqual(len(self._non_positive_warnings(step)), 1)
        # 有可用响应 → 结果级告警不该出现
        self.assertFalse(
            [w for w in step.warnings if "未生成任何断言" in w], step.warnings
        )

    def test_non_positive_only_also_reports_the_missing_assertions(self):
        """两层告警各自成立：点名告警（为什么跳过）+ 结果级告警（结果是什么）。"""
        step = self._steps([{"name": "zero", "code": 0, "body": "{}"}])[0]
        warnings = "\n".join(step.warnings)

        self.assertIn("不是正数", warnings)
        self.assertIn("未生成任何断言", warnings)
        # 文案订正的一半：结果级告警必须把「不是正数」列为一种原因，
        # 不能只说「缺失/为空」（L7 的归因错误）
        reason = [w for w in step.warnings if "未生成任何断言" in w][0]
        self.assertIn("不是正数", reason)

    def test_malformed_plus_non_positive_still_reports_no_assertions(self):
        """**L7-③**：坏数据 + 非正数 → malformed 说的「按剩余可用响应生成」是假承诺。

        修复前该组合下 `assertions == []`，而三条告警里**没有一条**说「未生成任何断言」
        （抑制条件是 `if not malformed`，被 malformed 挡住了）。
        """
        step = self._steps(
            [
                {"name": "weird", "code": "200 OK", "body": "{}"},
                {"name": "zero", "code": 0, "body": "{}"},
            ]
        )[0]
        warnings = "\n".join(step.warnings)

        self.assertEqual(step.assertions, [])
        self.assertIn("不是数字", warnings)
        self.assertIn("不是正数", warnings)
        self.assertIn(
            "未生成任何断言",
            warnings,
            "坏数据 + 非正数时「剩余可用」是 0 条，必须补发结果级告警",
        )

    def test_malformed_alone_is_still_not_doubled(self):
        """特异性对照：抑制条件是**收窄**而不是取消——只有坏数据时不重复打第二条。"""
        step = self._steps([{"name": "weird", "code": "200 OK", "body": "{}"}])[0]
        warnings = step.warnings

        self.assertTrue(any("不是数字" in w for w in warnings))
        self.assertFalse(
            [w for w in warnings if "未生成任何断言" in w],
            f"既有口径被改坏（会重复告警）：{warnings}",
        )

    def test_missing_code_is_not_reported_as_non_positive(self):
        """特异性对照：**没有** code 与**不是正数**是两回事，不能把缺失报成非正数。"""
        for responses in (
            [{"name": "s", "body": "{}"}],
            [{"name": "s", "code": None}],
            [{"name": "s", "code": ""}],
        ):
            with self.subTest(responses=responses):
                step = self._steps(responses)[0]

                self.assertFalse(
                    self._non_positive_warnings(step),
                    f"缺失被误报成非正数：{step.warnings}",
                )

    def test_warning_does_not_claim_the_code_was_missing(self):
        """文案订正的另一半：非正数告警不能说成「缺失/为空」。"""
        step = self._steps([{"name": "zero", "code": 0, "body": "{}"}])[0]

        self.assertNotIn("缺失/为空", self._non_positive_warnings(step)[0])


class TestBatch0919_26WrongShapeSavedResponse(unittest.TestCase):
    """批次 8 / **L7 末项**：示例响应里的非 dict 条目，修复前是**裸 `continue`**。

    ## 现场（实测，修复前）

    ```python
    for response in responses:
        if not isinstance(response, dict):
            continue          # ← 静默丢弃，一个字都不说
    ```

    `.tmp_report/probe_l7.py` ⑦（`["oops", {"name":"ok","code":200,...}]`）修复前的输出里
    **只有**「已按保存的示例响应生成形状断言」这一条——用户完全看不出有一条示例响应
    被丢掉了，也看不出 `len(responses)` 里算进去的那个条目根本没用上。

    与本批 L7 的主项（`code` 非正数）同族：都是「既不算可用、也没人报告」的缝。
    与 malformed 分开报：坏的是**条目本身**，不是它的 `code`。
    """

    def _steps(self, *responses_per_item):
        cases = postman_adapter.convert_postman(
            _collection_with_saved_responses(*responses_per_item)
        )
        return [step for case in cases for step in case.steps]

    def test_wrong_shape_entry_is_named_in_a_warning(self):
        """**核心**：非 dict 条目不再静默——告警给出下标与类型。"""
        step = self._steps(["oops", {"name": "ok", "code": 200, "body": '{"a": 1}'}])[0]

        shape = [w for w in step.warnings if "形态不对" in w]
        self.assertEqual(len(shape), 1, f"必须有唯一的形态告警：{step.warnings}")
        self.assertIn("[0]", shape[0], "要点名是哪个下标")
        self.assertIn("str", shape[0], "要点名实际类型")

    def test_usable_sibling_survives_a_wrong_shape_entry(self):
        """**核心**：形态不对的条目不得带走同请求里可用的那条。"""
        step = self._steps(["oops", {"name": "ok", "code": 200, "body": '{"a": 1}'}])[0]

        self.assertIn({"eq": ["status_code", 200]}, step.assertions)
        self.assertFalse(
            [w for w in step.warnings if "未生成任何断言" in w], step.warnings
        )

    def test_wrong_shape_alone_reports_no_assertions(self):
        """只有形态不对的条目时，结果级告警仍然成立（两条各说一层）。

        注意断言必须落在**点名**那条上（`有 N 个示例响应条目形态不对…`）：
        结果级告警的文案里也有「条目形态不对」这四个字，
        只判 `"形态不对" in warnings` 的话，**去掉点名告警这条用例仍然会绿**
        （注入 E 实测抓到的假绿，是本次修正的由来）。
        """
        step = self._steps(["oops"])[0]
        warnings = "\n".join(step.warnings)

        self.assertEqual(step.assertions, [])
        self.assertTrue(
            any(w.startswith("有 ") and "形态不对" in w for w in step.warnings),
            f"缺少点名的形态告警：{step.warnings}",
        )
        self.assertIn("未生成任何断言", warnings)

    def test_malformed_plus_wrong_shape_still_reports_no_assertions(self):
        """抑制条件对第三个桶同样适用：坏数据 + 形态不对 → 不能只剩假承诺。"""
        step = self._steps(
            [
                {"name": "weird", "code": "200 OK", "body": "{}"},
                "oops",
            ]
        )[0]
        warnings = "\n".join(step.warnings)

        self.assertEqual(step.assertions, [])
        self.assertIn("不是数字", warnings)
        self.assertIn("形态不对", warnings)
        self.assertIn("未生成任何断言", warnings)

    def test_dict_entries_never_trigger_the_shape_warning(self):
        """特异性对照：正常的 dict 条目（含可用与非正数）不得触发形态告警。"""
        step = self._steps(
            [
                {"name": "ok", "code": 200, "body": '{"a": 1}'},
                {"name": "zero", "code": 0, "body": "{}"},
            ]
        )[0]

        self.assertFalse(
            [w for w in step.warnings if "形态不对" in w], step.warnings
        )


class TestBatch0919_28NestedCollectionVariables(unittest.TestCase):
    """批次 8 / **L6**：集合变量**值里嵌套的 `{{...}}` 从不翻译**。

    ## 现场（实测，修复前，`.tmp_report/probe_l6.py` 真 CLI）

    修复前 `_collection_variables` / `_item_variables` 只是把 `value` 原样搬运：

    ```yaml
    hostBase: 127.0.0.1
    host: '{{hostBase}}'                       # ← {{...}} 不是框架语法，就是一段普通字符串
    apiRoot: '{{protocol}}://{{host}}/v1'
    ```

    三种素材形态的实际后果（**都实测过**）：

    | 素材 | 修复前运行期 |
    | --- | --- |
    | `{{apiRoot}}/things/1` | `ParamsError: base url missed!`（解析不出 netloc） |
    | `{{protocol}}://{{host}}/v1/things/1`（host 值里带端口） | `requests.InvalidURL: '{{hostBase}}:{{port}}' is not a valid host or port` |
    | `{{protocol}}://{{host}}:18102/…`（host 值只嵌套主机名） | `NameResolutionError` 被吞成 `status_code=0` → **零断言 → `1 passed`（假绿）** |

    最后一行正是记录里 L6 的原话，已在真 CLI 上复现。

    ## 修法

    变量**值**也过一遍 `substitute_variables`（与请求文本**同一条口径**：只翻译写法、
    **不内联值**；嵌套引用交给框架自己的变量收敛逻辑）。引到但未定义的名字按
    `${ENV(name)}` 占位并告警 —— 与请求文本里引到未定义变量完全一致。
    """

    def _case(self, **extra):
        return postman_adapter.convert_postman(
            _collection_with([_request_item("t", url="{{apiRoot}}/things/1")], **extra)
        )[0]

    def test_nested_reference_in_a_collection_variable_is_translated(self):
        """**核心**：`apiRoot` 的值必须变成 `${protocol}://${host}/v1`。"""
        case = self._case(
            variable=[
                {"key": "protocol", "value": "http"},
                {"key": "host", "value": "127.0.0.1:18102"},
                {"key": "apiRoot", "value": "{{protocol}}://{{host}}/v1"},
            ]
        )

        self.assertEqual(case.variables["apiRoot"], "${protocol}://${host}/v1")

    def test_no_postman_syntax_survives_anywhere_in_the_values(self):
        """**核心**：所有字符串值里都不该再残留 `{{`。"""
        case = self._case(
            variable=[
                {"key": "protocol", "value": "http"},
                {"key": "hostBase", "value": "127.0.0.1"},
                {"key": "port", "value": "18102"},
                {"key": "host", "value": "{{hostBase}}:{{port}}"},
                {"key": "apiRoot", "value": "{{protocol}}://{{host}}/v1"},
            ]
        )

        for name, value in case.variables.items():
            with self.subTest(name=name):
                if isinstance(value, str):
                    self.assertNotIn("{{", value, f"{name} 的值里还留着 Postman 语法")
        self.assertEqual(case.variables["host"], "${hostBase}:${port}")

    def test_item_level_variable_values_are_translated_too(self):
        """item 级 `variable[]` 与 collection 级同口径（它覆盖 collection 级）。

        NOTICE（批次 A / H5 补强）：这条用例**原先只断言中间值**
        （`case.variables["apiRoot"] == "${base}/v1"`），没有断言依赖项 `base`
        是否还在 —— 而 `base` **只被另一个变量的值引用**，正好落进剪枝的洞里：
        生成物会引用一个不存在的变量，`hrun` 直接 `VariableNotFound`，
        而 `hconvert` 还打印「导出即验证通过」。护栏只守住一半，等于没守住。
        现在补上「依赖项必须留下」这条断言。
        """
        collection = _collection_with(
            [
                {
                    "name": "t",
                    "request": {"method": "GET", "url": "{{apiRoot}}/things/1"},
                    "variable": [
                        {"key": "base", "value": "https://api.test"},
                        {"key": "apiRoot", "value": "{{base}}/v1"},
                    ],
                }
            ]
        )
        case = postman_adapter.convert_postman(collection)[0]

        self.assertEqual(case.variables["apiRoot"], "${base}/v1")
        # 依赖项必须在（H5：修复前这里会被剪掉，而 apiRoot 还引用着它）
        self.assertIn(
            "base",
            case.variables,
            f"被另一个变量的值引用的变量被剪掉了（H5）：{sorted(case.variables)}",
        )
        self.assertEqual(case.variables["base"], "https://api.test")

    def test_folder_level_variable_reference_chain_is_kept(self):
        """批次 A / H5：folder 级变量的**引用链**必须整条留下（真实 Postman 写法）。

        现场形态（`.tmp_audit/verify_prune.py`）：`baseHost` 只出现在 `apiRoot` 的值里，
        于是修复前 `variables` 只剩 `apiRoot: ${baseHost}/v1` —— 生成物**必然**跑不起来
        （`VariableNotFound: ['baseHost']`），而 `hconvert` 报 exit 0「导出即验证通过」。
        """
        collection = _collection_with(
            [
                {
                    "name": "Group",
                    "variable": [
                        {"key": "baseHost", "value": "https://api.example.test"},
                        {"key": "apiRoot", "value": "{{baseHost}}/v1"},
                    ],
                    "item": [
                        {
                            "name": "things",
                            "request": {"method": "GET", "url": "{{apiRoot}}/ok"},
                        }
                    ],
                }
            ]
        )
        case = postman_adapter.convert_postman(collection)[0]

        self.assertEqual(case.variables.get("apiRoot"), "${baseHost}/v1")
        self.assertIn(
            "baseHost",
            case.variables,
            "folder 级变量只被另一个变量的值引用时被剪掉了（H5）",
        )

    def test_step_scoped_variable_is_scanned_before_pruning(self):
        """H5 的另一半：**步骤级变量**（L25 之后会写进生成物）也是 `${...}` 的载体。"""
        collection = _collection_with(
            [
                {
                    "name": "Group",
                    "variable": [{"key": "shared", "value": "https://api.test"}],
                    "item": [
                        {
                            "name": "things",
                            "request": {"method": "GET", "url": "/things"},
                            "variable": [{"key": "apiRoot", "value": "{{shared}}/v1"}],
                        }
                    ],
                }
            ]
        )
        case = postman_adapter.convert_postman(collection)[0]

        self.assertIn(
            "shared",
            case.variables,
            f"步骤级变量引用到的名字被剪掉了（H5）：{sorted(case.variables)}",
        )

    def test_undefined_reference_gets_an_env_placeholder_and_a_warning(self):
        """引到未定义的变量：与请求文本里的同口径（`${ENV(name)}` 占位 + 告警点名）。"""
        case = self._case(
            variable=[
                {"key": "apiRoot", "value": "https://{{unknownHost}}/v1"},
            ]
        )
        warnings = "\n".join(case.all_warnings())

        self.assertEqual(case.variables["unknownHost"], "${ENV(unknownHost)}")
        self.assertIn("unknownHost", warnings)

    def test_defined_references_get_no_env_placeholder(self):
        """特异性对照：引到**已定义**的变量时不得产生 ENV 占位或"未定义"告警。"""
        case = self._case(
            variable=[
                {"key": "host", "value": "api.test"},
                {"key": "apiRoot", "value": "https://{{host}}/v1"},
            ]
        )
        warnings = "\n".join(case.all_warnings())

        self.assertEqual(case.variables["host"], "api.test")
        self.assertNotIn("${ENV(host)}", json.dumps(case.variables))
        self.assertNotIn("未定义", warnings)

    def test_dynamic_variable_inside_a_value_is_mapped(self):
        """值里的 `{{$guid}}` 也要走动态变量映射（并计入**集合级**提示）。

        NOTICE: 断言必须落在**用例级**那句 `集合用到了 Postman 动态变量…` 上，
        而不是只判"有『动态变量』字样"——翻译本身也会打一条逐条告警，
        只判字样的话，**去掉集合级统计这条用例仍然会绿**（注入 L6-D 实测抓到的假绿）。
        """
        case = self._case(
            variable=[{"key": "traceId", "value": "{{$guid}}"}]
        )
        warnings = "\n".join(case.all_warnings())

        self.assertEqual(case.variables["traceId"], "${uuid4_str()}")
        self.assertIn("集合用到了 Postman 动态变量", warnings)
        self.assertIn("$guid", warnings)

    def test_non_string_values_are_left_untouched(self):
        """反向护栏：Postman 的变量值可以是数字 —— 非字符串值原样保留。"""
        case = self._case(
            variable=[
                {"key": "port", "value": 18102},
                {"key": "flag", "value": True},
            ]
        )

        self.assertEqual(case.variables["port"], 18102)
        self.assertIs(case.variables["flag"], True)

    def test_values_without_nested_refs_are_byte_identical(self):
        """反向护栏：没有嵌套引用时逐字节不变（包括含单个 `{` 的值）。"""
        for value in ("127.0.0.1:18102", "a{b", "}}", "plain"):
            with self.subTest(value=value):
                case = self._case(variable=[{"key": "v", "value": value}])

                self.assertEqual(case.variables["v"], value)

    def test_repo_asset_variables_are_unchanged(self):
        """回归：仓库自带 Collection 的变量值不受影响（没有嵌套引用就不该变）。"""
        collection = _load_asset()
        expected = {
            entry.get("key"): entry.get("value")
            for entry in collection.get("variable") or []
        }
        before = postman_adapter.convert_postman(
            {**collection, "variable": []}
        )
        case = postman_adapter.convert_postman(collection)[0]

        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertEqual(case.variables.get(name), value)
        self.assertTrue(before, "空变量集合也应产出用例（这条回归不是空跑）")


class TestBatch0919_29FolderVariablesAndBaseUrlNoise(unittest.TestCase):
    """批次 8 收尾 / **L22 + L23 + L24 + L25**（四处都在 Postman 导入器的变量/URL 处理里）。

    ## 现场（实测）

    | # | 修复前 | 证据 |
    | --- | --- | --- |
    | **L22** | folder 级 `variable[]` **从不读取** → 引到它的请求退化成 `${ENV(name)}` + "变量未定义"告警，**录到的值被丢掉** | `.tmp_report/probe_l22_folder_vars.py` |
    | **L23** | URL 以变量开头时落到"host 与 base_url 不同"分支 → 输出「该请求的 host（）与用例 `base_url`（空）不同」，**信息量≈0、会被误读成配置错了，且每条请求一条** | 同上（+ `probe_l6.py` shape A） |
    | **L24** | `variable[]` 里混进非对象条目 / `variable` 不是列表 → 裸 `AttributeError`，**整份集合一个用例都不产出**（与 HAR 的 L21 同族） | 实测 `'oops'` / `123` / `None` / `"variable": "oops"` 四种全崩 |
    | **L25** | 跨 folder/item 重名的变量**静默按最后一个值**解析所有步骤（`config.variables` 是扁平的）——Postman 里那是作用域，框架里做不到 | 同上（修 L22 时暴露） |

    ## 修法与"能做到哪一步"

    - L22：`_iter_items` 沿路径继承 folder 的 `variable[]`（collection → 外层 folder → 内层 folder → item，
      **越具体越优先**），值同样走 L6 的翻译口径；
    - L24：形态不对的条目**跳过并告警**，其余变量照常（口径与 HAR 的 L21 一致）；
    - L23：URL 以变量开头时给一条**准确**说明（"无法静态确定 origin、原样保留、运行期由变量决定"），
      且**每个用例只提示一次**；
    - L25：**不假装支持作用域**——冲突时告警点名变量与几种值，并说明生成物只有最后一个值、
      以及三种可选做法（改名 / 按顶层 folder 拆文件 / 用步骤级 `variables:`）。
    """

    SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

    def _case(self, collection, **kwargs):
        return postman_adapter.convert_postman(collection, **kwargs)[0]

    def _folder_collection(self, folder_extra, items, collection_variables=None):
        folder = {"name": "Group", "item": items}
        folder.update(folder_extra)
        return _collection_with(
            [folder],
            variable=collection_variables or [],
            info={"name": "x", "schema": self.SCHEMA},
        )

    # ---------------- L22 ----------------

    def test_folder_level_variable_is_inherited(self):
        """**核心**：folder 级 `variable[]` 必须被继承（修复前值是丢的、退化成 ENV 占位）。"""
        case = self._case(
            self._folder_collection(
                {"variable": [{"key": "folderOnly", "value": "from-folder"}]},
                [
                    _request_item(
                        "t",
                        url="https://api.test/x",
                        header=[{"key": "X-Trace", "value": "{{folderOnly}}"}],
                    )
                ],
            )
        )
        warnings = "\n".join(case.all_warnings())

        self.assertEqual(case.variables["folderOnly"], "from-folder")
        self.assertNotIn("${ENV(folderOnly)}", json.dumps(case.variables))
        self.assertNotIn("在集合里没有定义", warnings)

    def test_folder_variable_overrides_collection_variable(self):
        """越具体越优先：folder 覆盖 collection。"""
        case = self._case(
            self._folder_collection(
                {"variable": [{"key": "who", "value": "folder"}]},
                [
                    _request_item(
                        "t",
                        url="https://api.test/x",
                        header=[{"key": "X-Trace", "value": "{{who}}"}],
                    )
                ],
                collection_variables=[{"key": "who", "value": "collection"}],
            )
        )

        self.assertEqual(case.variables["who"], "folder")

    def test_item_variable_overrides_folder_variable(self):
        """item 覆盖 folder（同一个 step 内，没有跨作用域冲突）。"""
        case = self._case(
            self._folder_collection(
                {"variable": [{"key": "who", "value": "folder"}]},
                [
                    {
                        **dict(
                            _request_item(
                                "t",
                                url="https://api.test/x",
                                header=[{"key": "X-Trace", "value": "{{who}}"}],
                            )
                        ),
                        "variable": [{"key": "who", "value": "item"}],
                    }
                ],
            )
        )

        self.assertEqual(case.variables["who"], "item")

    def test_nested_folder_inner_value_wins(self):
        """嵌套 folder：内层覆盖外层（本例只有内层用到该名字，所以取值无歧义）。"""
        collection = _collection_with(
            [
                {
                    "name": "Outer",
                    "variable": [{"key": "outerOnly", "value": "outer"}],
                    "item": [
                        {
                            "name": "Inner",
                            "variable": [{"key": "who", "value": "inner"}],
                            "item": [
                                _request_item(
                                    "deep",
                                    url="https://api.test/deep",
                                    header=[
                                        {"key": "X-Trace", "value": "{{who}}"},
                                        {"key": "X-Outer", "value": "{{outerOnly}}"},
                                    ],
                                )
                            ],
                        }
                    ],
                }
            ],
            info={"name": "x", "schema": self.SCHEMA},
        )
        case = self._case(collection)

        self.assertEqual(case.variables["who"], "inner")
        self.assertEqual(case.variables["outerOnly"], "outer")

    def test_folder_variable_value_with_nested_reference_is_translated(self):
        """folder 变量的**值**里嵌套 `{{...}}` 同样翻译（L6 口径）。"""
        case = self._case(
            self._folder_collection(
                {
                    "variable": [
                        {"key": "apiRoot", "value": "{{protocol}}://{{host}}/v1"}
                    ]
                },
                [_request_item("t", url="{{apiRoot}}/x")],
                collection_variables=[
                    {"key": "protocol", "value": "http"},
                    {"key": "host", "value": "127.0.0.1:18102"},
                ],
            )
        )

        self.assertEqual(case.variables["apiRoot"], "${protocol}://${host}/v1")

    # ---------------- L25 ----------------

    def _conflicting_collection(self, extra_fields=None):
        """外层 folder 与内层 folder 的 `who` 不同；两个步骤各在自己的作用域里。"""
        deep = _request_item(
            "deep",
            url="https://api.test/deep",
            header=[{"key": "X-Trace", "value": "{{who}}"}],
        )
        if extra_fields:
            deep.update(extra_fields)
        return _collection_with(
            [
                {
                    "name": "Outer",
                    "variable": [{"key": "who", "value": "outer"}],
                    "item": [
                        {
                            "name": "Inner",
                            "variable": [{"key": "who", "value": "inner"}],
                            "item": [deep],
                        },
                        _request_item(
                            "outer level",
                            url="https://api.test/outer",
                            header=[{"key": "X-Trace", "value": "{{who}}"}],
                        ),
                    ],
                }
            ],
            info={"name": "x", "schema": self.SCHEMA},
        )

    def test_conflicting_scopes_are_generated_as_step_level_variables(self):
        """**核心（L25 完整支持）**：同名不同值 → **每个步骤**取自己作用域里的值。

        修复前（`0919-29`）只告警、值仍按"最后出现的那个"解析所有步骤；
        生成物格式、IR 模型、运行期优先级其实早就支持步骤级变量，缺的只是导入器没用它。
        端到端实测见 `.tmp_report/probe_l25_e2e.py`（真 CLI：服务端分别收到 outer / inner）。
        """
        case = self._case(self._conflicting_collection())

        by_name = {step.name: step.variables for step in case.steps}

        self.assertEqual(
            by_name["deep"], {"who": "inner"}, "内层 folder 的值没落到步骤级"
        )
        self.assertEqual(
            by_name["outer level"], {"who": "outer"}, "外层 folder 的值没落到步骤级"
        )

    def test_conflict_warning_says_it_was_handled_not_that_it_is_broken(self):
        """告警要说明"已按作用域生成 + config 里留的是兜底值"，而不是"会按错值解析"。"""
        case = self._case(self._conflicting_collection())
        conflicts = [w for w in case.all_warnings() if "跨 folder / item 重名" in w]

        self.assertEqual(
            len(conflicts), 1, f"必须有且只有一条冲突告警：{case.all_warnings()}"
        )
        self.assertIn("步骤级", conflicts[0])
        self.assertIn("兜底值", conflicts[0])
        self.assertNotIn("静默按错值", conflicts[0])
        self.assertIn("who", conflicts[0])

    def test_config_keeps_a_fallback_value_for_steps_outside_any_scope(self):
        """反向护栏：`config.variables` 里仍保留一个兜底值（与修复前同口径，没变得更糟）。"""
        case = self._case(self._conflicting_collection())

        self.assertEqual(case.variables["who"], "outer")

    def test_scoped_secret_name_is_not_written_as_plaintext(self):
        """**新增防线**：步骤级变量同样是生成物里明文可见的位置 → 凭据要照旧占位化。"""
        case = self._case(
            _collection_with(
                [
                    {
                        "name": "Outer",
                        "variable": [{"key": "apiPassword", "value": "outer-plain"}],
                        "item": [
                            {
                                "name": "Inner",
                                "variable": [
                                    {"key": "apiPassword", "value": "inner-plain"}
                                ],
                                "item": [
                                    _request_item(
                                        "deep",
                                        url="https://api.test/deep",
                                        header=[
                                            {
                                                "key": "X-Pwd",
                                                "value": "{{apiPassword}}",
                                            }
                                        ],
                                    )
                                ],
                            },
                            _request_item(
                                "outer level",
                                url="https://api.test/outer",
                                header=[{"key": "X-Pwd", "value": "{{apiPassword}}"}],
                            ),
                        ],
                    }
                ],
                info={"name": "x", "schema": self.SCHEMA},
            )
        )
        dumped = json.dumps([step.variables for step in case.steps], ensure_ascii=False)
        self.assertNotIn("plain", dumped, f"步骤级变量里泄漏了明文：{dumped}")
        self.assertIn("${ENV(apiPassword)}", dumped)

    def test_no_conflict_means_no_step_level_variables(self):
        """**产物不变**的反向护栏：不冲突时步骤级 `variables` 必须是空的。"""
        case = self._case(
            self._folder_collection(
                {"variable": [{"key": "folderOnly", "value": "f"}]},
                [_request_item("t", url="https://api.test/x")],
                collection_variables=[{"key": "collectionOnly", "value": "c"}],
            )
        )

        for step in case.steps:
            with self.subTest(step=step.name):
                self.assertEqual(step.variables, {})

    def test_repo_asset_gets_no_step_level_variables(self):
        """回归：仓库自带 Collection 的产物不该变成"config + 步骤级"混合形态。"""
        for case in postman_adapter.convert_postman(_load_asset()):
            for step in case.steps:
                with self.subTest(case=case.name, step=step.name):
                    self.assertEqual(
                        step.variables,
                        {},
                        "既有产物形态被改变了（黄金文件与本条都会变）",
                    )

    def test_no_conflict_warning_when_names_are_unique(self):
        """特异性对照：名字不重复时不得报冲突（假报会让真报被无视）。"""
        case = self._case(
            self._folder_collection(
                {"variable": [{"key": "folderOnly", "value": "f"}]},
                [_request_item("t", url="https://api.test/x")],
                collection_variables=[{"key": "collectionOnly", "value": "c"}],
            )
        )

        self.assertFalse(
            [w for w in case.all_warnings() if "重名" in w], case.all_warnings()
        )

    # ---------------- L23 ----------------

    def test_variable_led_url_gets_one_accurate_warning(self):
        """**核心**：两条变量开头的请求 → 只有**一条**准确说明，且不再是"host 与 base_url 不同"。"""
        case = self._case(
            _collection_with(
                [
                    _request_item("one", url="{{apiRoot}}/v1/one"),
                    _request_item("two", url="{{apiRoot}}/v1/two"),
                ],
                variable=[{"key": "apiRoot", "value": "http://127.0.0.1:18102"}],
                info={"name": "x", "schema": self.SCHEMA},
            )
        )
        origin_warnings = [
            w for w in case.all_warnings() if "无法静态确定" in w or "base_url" in w
        ]

        self.assertEqual(
            len(origin_warnings), 1, f"应只提示一次：{case.all_warnings()}"
        )
        self.assertIn("无法静态确定", origin_warnings[0])
        self.assertIn("**不是**配置错误", origin_warnings[0])
        self.assertNotIn("host（）", origin_warnings[0])
        # 运行期需要的行为：URL 原样保留（变量解析后是绝对 URL，build_url 直接放行）
        self.assertEqual(
            [step.url for step in case.steps],
            ["${apiRoot}/v1/one", "${apiRoot}/v1/two"],
        )

    def test_normal_absolute_urls_do_not_get_the_variable_origin_warning(self):
        """特异性对照：正常绝对 URL 不得触发这条告警。"""
        case = self._case(
            _collection_with(
                [_request_item("a", url="https://api.test/x")],
                info={"name": "x", "schema": self.SCHEMA},
            )
        )

        self.assertFalse(
            [w for w in case.all_warnings() if "无法静态确定" in w], case.all_warnings()
        )
        self.assertEqual(case.steps[0].url, "/x")

    # ---------------- L24 ----------------

    def test_malformed_variable_entries_are_skipped_with_a_warning(self):
        """**核心**：`variable[]` 里的坏条目不再让整份集合崩（与 HAR 的 L21 同口径）。"""
        for bad in ("oops", 123, None):
            with self.subTest(bad=bad):
                case = self._case(
                    _collection_with(
                        [_request_item("i", url="https://api.test/x")],
                        variable=[bad, {"key": "ok", "value": "1"}],
                        info={"name": "x", "schema": self.SCHEMA},
                    )
                )
                warnings = "\n".join(case.all_warnings())

                self.assertEqual(case.variables["ok"], "1", "好条目必须照常收集")
                self.assertIn("形态不对", warnings)
                self.assertIn(type(bad).__name__, warnings)

    def test_non_list_variable_field_is_skipped_with_a_warning(self):
        case = self._case(
            _collection_with(
                [_request_item("i", url="https://api.test/x")],
                variable="oops",
                info={"name": "x", "schema": self.SCHEMA},
            )
        )

        self.assertIn("不是列表", "\n".join(case.all_warnings()))

    def test_malformed_folder_variable_entries_are_skipped_too(self):
        case = self._case(
            self._folder_collection(
                {"variable": ["oops"]},
                [_request_item("t", url="https://api.test/x")],
            )
        )
        warnings = "\n".join(case.all_warnings())

        self.assertIn("形态不对", warnings)
        self.assertIn("Group", warnings, "要点名是哪个 folder")

    def test_repo_asset_is_unaffected_by_the_new_warnings(self):
        """回归：仓库自带 Collection 不该新增这几类告警（无 folder 变量、无重名冲突）。"""
        case = postman_adapter.convert_postman(_load_asset())[0]
        warnings = "\n".join(case.all_warnings())

        for fragment in ("跨 folder / item 重名", "无法静态确定", "形态不对"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, warnings)
