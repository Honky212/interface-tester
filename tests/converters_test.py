"""P3-a：导入器（IR + curl + HAR → 标准 YAML）的测试。

三块：
1. **适配器**：解析正确性 + 支持子集/告警（用仓库里的真实素材当输入）；
2. **黄金文件**：`examples/data/**` 的素材 → 生成的 YAML 快照对比（`UPDATE_GOLDEN=1` 可重写）；
3. **导出即验证**：生成物必须能被 `load_testcase()` + `hmake` 处理，并且能**真跑通**
   （用本地 mock 造一份 curl/HAR 素材，端到端跑到 `main_run` 退出码 0；不依赖外网）。
"""

import base64
import io
import json
import os
import re
import shutil
import sys
import unittest
import uuid
from unittest import mock

import pytest
import yaml
from loguru import logger

from interfacetester import exceptions, loader
from interfacetester.cli import main, main_convert_alias, main_run
from interfacetester.converters import (
    curl_adapter,
    emit_yaml,
    har_adapter,
    ir,
    openapi_adapter,
    postman_adapter,
)
from interfacetester.converters.ir import (
    IRCase,
    IRStep,
    duplicated_names,
    sanitize_body,
)
from interfacetester.make import pytest_files_made_cache_mapping, pytest_files_run_set
from interfacetester.models import KNOWN_REQUEST_FIELDS
from interfacetester.utils import HTTP_BIN_URL

REPO = os.getcwd()
CURL_ASSET = os.path.join("examples", "data", "curl", "curl_examples.txt")
HAR_ASSET = os.path.join("examples", "data", "har", "demo.har")
# 写进生成物头部注释的「来源」用正斜杠固定写法：黄金文件不能随平台变形（Windows 反斜杠 vs POSIX 斜杠）
CURL_ASSET_POSIX = "examples/data/curl/curl_examples.txt"
HAR_ASSET_POSIX = "examples/data/har/demo.har"
GOLDEN_DIR = os.path.join("tests", "golden", "converters")

# 素材里真实存在的凭据（用来断言「绝不出现在生成物里」）
CURL_ASSET_TOKEN = "b7d03a6947b217efb6f3ec3bd3504582"


def _tmp_dir(prefix: str = "tmp_conv") -> str:
    """临时目录放 logs/ 下（已被 .gitignore 覆盖；不用 mkdtemp 的 0o700）。"""
    path = os.path.join(REPO, "logs", f"{prefix}_{uuid.uuid4().hex[:8]}")
    os.makedirs(path)
    return path


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fp:
        return fp.read()


def _write(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(content)
    return path


def _assert_golden(testcase: unittest.TestCase, actual: str, golden_name: str) -> None:
    """黄金文件对比（`UPDATE_GOLDEN=1` 时改写快照）。"""
    golden_path = os.path.join(GOLDEN_DIR, golden_name)
    if os.environ.get("UPDATE_GOLDEN") == "1" or not os.path.isfile(golden_path):
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        with open(golden_path, "w", encoding="utf-8") as fp:
            fp.write(actual)
        testcase.skipTest(f"已写入黄金文件 {golden_path}（UPDATE_GOLDEN=1 或首次生成）")

    expected = _read(golden_path)
    testcase.assertEqual(
        actual,
        expected,
        f"生成结果与黄金文件不一致：{golden_path}\n"
        f"（确认是预期变更后，用 UPDATE_GOLDEN=1 重跑以更新快照）",
    )


class TestCurlAdapter(unittest.TestCase):
    """curl 解析：仓库素材里那 6 条「最脏写法」逐条核对。"""

    def setUp(self):
        self.tmp_dir = _tmp_dir("tmp_curl")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def convert_asset(self) -> IRCase:
        return curl_adapter.convert_curl_file(
            _read(CURL_ASSET), case_name="curl_examples", source=CURL_ASSET_POSIX
        )

    def test_repo_asset_parses_all_six_commands(self):
        case = self.convert_asset()

        self.assertEqual(len(case.steps), 6)
        methods = [step.method for step in case.steps]
        self.assertEqual(methods, ["GET", "GET", "POST", "POST", "POST", "POST"])

    def test_bare_domain_gets_http_scheme(self):
        case = self.convert_asset()
        # `curl httpbin.org` → 补 http://（与 curl 行为一致），并留下告警
        self.assertEqual(case.base_url, "http://httpbin.org")
        self.assertTrue(
            any("没有协议头" in warning for warning in case.steps[0].warnings), case.steps[0].warnings
        )
        # 第一条命令的 origin 就是 base_url → 该步骤自身也写成相对路径
        self.assertEqual(case.steps[0].url, "/")

    def test_query_string_stays_verbatim(self):
        case = self.convert_asset()
        # 第 2 条：查询串原样留在 URL 里（不拆成 params，避免重名键与编码被改写）
        self.assertIn("key1=value1&key2=value2", case.steps[1].url)

    def test_data_at_file_is_flagged_not_silent(self):
        """0918-7 / L9：`-d @file` 的 curl 语义是「读文件内容作为请求体」。

        导入器不读文件（与 `-F` 的 `<file` 同一原则：宁可显式告警，也不猜），
        所以字面值保守保留，但**必须告警**——修复前 `@data.json` 被当字面量
        静默塞进 request.data，生成的用例会把字符串 "@data.json" 原样发给服务端。
        """
        case = curl_adapter.convert_curl_file(
            "curl -X POST https://api.test/upload -d @data.json",
            case_name="curl_at_file",
            source="t.txt",
        )
        step = case.steps[0]

        self.assertEqual(step.data, "@data.json")
        self.assertTrue(any("@file" in w for w in step.warnings), step.warnings)

    def test_json_body_becomes_structured_json(self):
        case = self.convert_asset()
        step = case.steps[2]
        self.assertEqual(step.headers["Content-Type"], "application/json")
        self.assertEqual(step.json_body["type"], "A")
        self.assertEqual(step.json_body["name"], "www")

    def test_authorization_is_placeholder_only(self):
        case = self.convert_asset()
        step = case.steps[2]

        self.assertEqual(step.headers["Authorization"], "Bearer ${AUTH_TOKEN}")
        self.assertEqual(case.variables["AUTH_TOKEN"], "${ENV(AUTH_TOKEN)}")

    def test_secret_never_appears_in_output(self):
        """合规红线：源里的明文 token 绝不能出现在生成的 YAML 里。"""
        case = self.convert_asset()
        emitted = emit_yaml.dump_case_yaml(case, "curl_examples.yml")

        self.assertNotIn(CURL_ASSET_TOKEN, emitted)
        self.assertIn("${AUTH_TOKEN}", emitted)

    def test_multipart_maps_fields_and_files(self):
        case = self.convert_asset()
        step = case.steps[3]

        self.assertEqual(step.data, {"dummyName": "dummyFile"})
        self.assertEqual(step.upload, {"file1": "file1.txt", "file2": "file2.txt"})
        self.assertTrue(any("upload" in warning for warning in step.warnings))

    def test_nested_bracket_form_stays_verbatim(self):
        case = self.convert_asset()
        body = case.steps[4].data

        self.assertIsInstance(body, str)
        self.assertIn("shipment[to_address][id]=adr_HrBKVA85", body)

    def test_percent_encoded_body_not_reencoded(self):
        case = self.convert_asset()
        # `--data "key1=value+1&key2=value%3A2"`：编码必须原样保留
        self.assertEqual(case.steps[5].data, "key1=value+1&key2=value%3A2")

    def test_mixed_hosts_keep_absolute_urls(self):
        case = self.convert_asset()
        # 素材里 http/https/httpbing.org 混用 → 首条成为 base_url（写成相对路径），
        # 其余 host 不同的步骤保留绝对 URL 并各自告警（不猜、不静默改写）
        self.assertEqual(case.base_url, "http://httpbin.org")
        self.assertEqual(case.steps[0].url, "/")
        self.assertTrue(case.steps[1].url.startswith("https://httpbin.org/"))
        self.assertTrue(case.steps[5].url.startswith("https://httpbing.org/"))
        self.assertTrue(any("与用例 `base_url`" in warning for warning in case.steps[1].warnings))

    # ---------------------------------------------------------------- 语法糖
    def test_line_continuation_and_comments(self):
        content = (
            "# 一段说明\n"
            "curl -X POST 'http://example.com/api' \\\n"
            '  -H "Content-Type: application/json" \\\n'
            "  -d '{\"a\": 1}'\n"
            "\n"
            "curl http://example.com/health\n"
        )
        case = curl_adapter.convert_curl_file(content, case_name="t")

        self.assertEqual(len(case.steps), 2)
        self.assertEqual(case.base_url, "http://example.com")
        self.assertEqual(case.steps[0].method, "POST")
        self.assertEqual(case.steps[0].json_body, {"a": 1})
        # 同 origin 的后续步骤写成相对路径（base_url 已在 config 里）
        self.assertEqual(case.steps[1].url, "/health")

    def test_get_flag_moves_data_into_query(self):
        case = curl_adapter.convert_curl_file(
            "curl -G http://example.com/search -d 'q=abc' -d 'page=2'", case_name="t"
        )
        self.assertEqual(case.base_url, "http://example.com")
        self.assertEqual(case.steps[0].url, "/search?q=abc&page=2")
        self.assertIsNone(case.steps[0].data)

    def test_basic_auth_becomes_placeholder_header(self):
        case = curl_adapter.convert_curl_file(
            "curl -u user:secret http://example.com/private", case_name="t"
        )
        step = case.steps[0]

        self.assertEqual(step.headers["Authorization"], "Basic ${AUTH_BASIC}")
        self.assertEqual(case.variables["AUTH_BASIC"], "${ENV(AUTH_BASIC)}")
        self.assertNotIn("secret", emit_yaml.dump_case_yaml(case, "t.yml"))

    def test_insecure_and_proxy_flags(self):
        case = curl_adapter.convert_curl_file(
            "curl -k -x http://proxy:8080 https://example.com/api", case_name="t"
        )
        step = case.steps[0]

        self.assertFalse(step.verify)
        self.assertEqual(step.proxies, {"http": "http://proxy:8080", "https": "http://proxy:8080"})

    def test_unsupported_flags_warn_with_hint(self):
        case = curl_adapter.convert_curl_file(
            "curl -o out.bin --cert client.pem --http2 http://example.com/x", case_name="t"
        )
        warnings = " ".join(case.all_warnings())

        self.assertIn("request.cert", warnings)  # 给出替代写法
        self.assertIn("保存文件" if "保存文件" in warnings else "响应存文件", warnings)
        self.assertIn("HTTP/2", warnings)

    def test_command_without_url_warns(self):
        case = curl_adapter.convert_curl_file("curl -H 'X: 1'", case_name="t")

        self.assertEqual(case.steps, [])
        self.assertTrue(any("没有解析出 URL" in warning for warning in case.all_warnings()))

    def test_non_curl_line_is_skipped_with_warning(self):
        case = curl_adapter.convert_curl_file(
            "wget http://example.com/a\ncurl http://example.com/b", case_name="t"
        )

        self.assertEqual(len(case.steps), 1)
        self.assertTrue(any("不是 curl 命令" in warning for warning in case.all_warnings()))

    def test_cookie_values_are_placeholders(self):
        case = curl_adapter.convert_curl_file(
            "curl -b 'sid=abc123; theme=dark' http://example.com/x", case_name="t"
        )
        step = case.steps[0]

        self.assertEqual(step.cookies, {"sid": "${COOKIE_SID}", "theme": "${COOKIE_THEME}"})
        self.assertNotIn("abc123", emit_yaml.dump_case_yaml(case, "t.yml"))


class TestBatch0920HarResourceTypeVocabularies(unittest.TestCase):
    """0920 批次 7 / **N43**：`_resourceType` 不止 Chrome 一套词汇。

    `_resourceType` **不是 HAR 规范的一部分**（Chrome 的扩展字段），
    各工具各写各的：Chrome/Firefox 用 `stylesheet`/`script`/`image`…，
    Fiddler 用首字母大写（`.lower()` 可覆盖），**Charles Proxy 按扩展名给**
    （`css`/`js`/`png`…），Insomnia/Postman 用 `XHR`/`Document`/`Other`。

    修复前只认 Chrome 那 8 个词 —— 于是 **Charles 录制的 CSS/JS/图片全部漏进用例**
    （实测 `.tmp_audit/n43_check.py`）。

    NOTICE（**刻意不**收 `json`/`xml`/`text`/`document`/`xhr`）：前三个是 Charles
    按**响应类型**给的，而接口返回的正是它们；后两个是**真正的接口请求**
    （Chrome 把 fetch/XHR 标成 `xhr`/`fetch`）。"是不是静态资源"交给**扩展名**规则判断。
    """

    @staticmethod
    def _should_skip(resource_type: str):
        entry = {
            "_resourceType": resource_type,
            # URL **不带**静态扩展名 → 只能靠 `_resourceType` 判断
            "request": {"method": "GET", "url": "https://cdn.test/assets/abc123", "headers": []},
            "response": {"status": 200, "headers": [], "content": {}},
        }
        return har_adapter._should_skip_entry(entry, None, None)

    def test_charles_style_extension_resource_types_are_filtered(self):
        """Charles 按扩展名给的静态类型必须被过滤（修复前全部漏过）。"""
        for resource_type in ("css", "js", "png", "gif", "ico", "jpeg", "woff2", "mp4"):
            with self.subTest(resource_type=resource_type):
                self.assertIsNotNone(
                    self._should_skip(resource_type),
                    f"{resource_type!r} 没被过滤（Charles 录制会漏进用例）",
                )

    def test_chrome_and_fiddler_types_still_filtered(self):
        """Chrome 与 Fiddler（首字母大写）的既有行为不变。"""
        for resource_type in (
            "stylesheet", "script", "image", "font", "media", "websocket", "manifest", "other",
            "Stylesheet", "Script", "Image", "Font", "Other",
        ):
            with self.subTest(resource_type=resource_type):
                self.assertIsNotNone(self._should_skip(resource_type))

    def test_interface_like_types_are_kept(self):
        """反向护栏：**真正的接口**类型不许被过滤。

        `xhr`/`fetch` 是接口请求；`json`/`xml`/`text` 是 Charles 按响应类型给的
        —— 而接口返回的正是它们。误杀它们等于丢掉真用例。
        """
        for resource_type in ("xhr", "fetch", "XHR", "document", "Document",
                              "json", "xml", "text", "binary", ""):
            with self.subTest(resource_type=resource_type):
                self.assertIsNone(
                    self._should_skip(resource_type),
                    f"{resource_type!r} 被误杀了（那是接口请求）",
                )


class TestBatch0920CurlCookieFileForm(unittest.TestCase):
    """0920 批次 7 / **N44**：`curl -b cookies.txt` 是**从文件读** cookie。

    curl 的 `-b/--cookie` 有双语义：**含 `=` 当内联串，不含 `=` 当文件名**
    （Netscape 格式的 cookie 文件）。修复前一律按"cookie 串"解析 ——
    文件形态里没有 `=`，于是解析出空 dict，**cookie 全部消失且零告警**
    （实测 `.tmp_audit/n44_check.py`：`cookies={}`、`warnings=[]`）。
    回放时少了整个会话凭据，表现为"莫名的 401/未登录"。

    导入器**刻意不读文件**（读文件会把宿主机状态写进产物，且文件未必存在），
    所以口径是**响亮告警 + 给改法**，而不是静默丢掉。
    """

    def test_cookie_file_form_warns_and_names_the_file(self):
        step = curl_adapter.parse_curl_command(
            "curl https://api.test/x -b cookies.txt"
        )

        self.assertEqual(step.cookies, {}, "文件形态本来就不该猜出 cookie")
        warnings = "\n".join(step.warnings)
        self.assertIn("cookie 文件", warnings)
        self.assertIn("cookies.txt", warnings)
        self.assertIn("没有进入用例", warnings)
        self.assertIn("debugtalk.py", warnings, "要给出可行的替代写法")

    def test_long_form_cookie_file_also_warns(self):
        step = curl_adapter.parse_curl_command(
            "curl https://api.test/x --cookie /tmp/ck.txt"
        )

        self.assertIn("cookie 文件", "\n".join(step.warnings))

    def test_inline_cookie_string_is_unchanged(self):
        """反向护栏：内联 cookie 串照旧解析 + 占位化（不许被文件判据误伤）。"""
        step = curl_adapter.parse_curl_command(
            "curl https://api.test/x -b 'sid=abc; theme=dark'"
        )

        self.assertEqual(
            step.cookies, {"sid": "${COOKIE_SID}", "theme": "${COOKIE_THEME}"}
        )
        self.assertNotIn("cookie 文件", "\n".join(step.warnings))

    def test_single_pair_cookie_string_is_not_treated_as_a_file(self):
        """单键值也是内联串（含 `=`），不能因为有 `.` 或斜杠就误判成文件。"""
        step = curl_adapter.parse_curl_command(
            "curl https://api.test/x -b 'token=abc.txt'"
        )

        self.assertEqual(step.cookies, {"token": "${COOKIE_TOKEN}"})
        self.assertNotIn("cookie 文件", "\n".join(step.warnings))


class TestBatch0920HarDedupeKeyCoversStructuredBody(unittest.TestCase):
    """0920 批次 4 / **N20**：去重键必须覆盖结构化的 `postData.params`。

    ## 修复前的现场（`.tmp_audit/n20_check.py`）

    去重键只取 `postData.text`，而**表单/multipart 的录制常常只有 `params`、没有 `text`**
    —— 于是键退化成 `(method, url, "")`，**两条内容完全不同**的请求被判"重复"，
    第二条整条被丢，而汇总告警还写着"跳过了 N 条『方法+URL+请求体』**完全相同**的重复录制"：

    ```text
    /up 两条 multipart 录制（doc=a.pdf note=1 / doc=b.pdf note=2）
      -> dedupe=True  只剩 1 步，uploads 只剩 {'doc': 'a.pdf'}   ← 丢了一条真实录制
      -> dedupe=False 正常 2 步
    ```

    告警文本把用户引向"确实是重复录制"这个**错误结论**，比丢数据本身更难查。
    """

    @staticmethod
    def _entry(url, post_data):
        return {
            "request": {
                "method": "POST",
                "url": url,
                "headers": [
                    {"name": "Content-Type", "value": post_data["mimeType"]}
                ],
                "postData": post_data,
            },
            "response": {
                "status": 200,
                "headers": [],
                "content": {"mimeType": "application/json", "text": "{}"},
            },
        }

    @staticmethod
    def _multipart(file_name, note):
        return {
            "mimeType": "multipart/form-data",
            "params": [
                {"name": "doc", "fileName": file_name},
                {"name": "note", "value": note},
            ],
        }

    def _convert(self, entries, dedupe=True):
        return har_adapter.convert_har(
            {"log": {"version": "1.2", "entries": entries}},
            case_name="dedupe probe",
            assertion_mode="status",
            dedupe=dedupe,
        )

    def test_different_multipart_recordings_are_both_kept(self):
        """两条只有 `params` 的 multipart 录制内容不同 → 必须都保留。"""
        url = "https://api.test/up"
        case = self._convert(
            [
                self._entry(url, self._multipart("a.pdf", "1")),
                self._entry(url, self._multipart("b.pdf", "2")),
            ]
        )

        self.assertEqual(
            len(case.steps), 2, "内容不同的两条录制被判成重复丢掉了（N20）"
        )
        self.assertEqual(
            [step.upload for step in case.steps],
            [{"doc": "a.pdf"}, {"doc": "b.pdf"}],
        )
        self.assertEqual(
            [w for w in case.all_warnings() if "去重" in w], [], case.all_warnings()
        )

    def test_different_urlencoded_recordings_are_both_kept(self):
        url = "https://api.test/f"
        case = self._convert(
            [
                self._entry(
                    url,
                    {
                        "mimeType": "application/x-www-form-urlencoded",
                        "params": [{"name": "action", "value": "create"}],
                    },
                ),
                self._entry(
                    url,
                    {
                        "mimeType": "application/x-www-form-urlencoded",
                        "params": [{"name": "action", "value": "delete"}],
                    },
                ),
            ]
        )

        self.assertEqual(len(case.steps), 2)
        self.assertEqual(
            [step.data for step in case.steps],
            [{"action": "create"}, {"action": "delete"}],
        )

    def test_truly_identical_recordings_are_still_deduped(self):
        """反向护栏：**逐项相同**的录制照旧去重（不许把去重功能改坏）。"""
        url = "https://api.test/up"
        case = self._convert(
            [
                self._entry(url, self._multipart("a.pdf", "1")),
                self._entry(url, self._multipart("a.pdf", "1")),
            ]
        )

        self.assertEqual(len(case.steps), 1)
        self.assertTrue(any("去重" in warning for warning in case.all_warnings()))

    def test_text_only_recordings_are_unchanged(self):
        """既有行为不变：只有 `text` 的录制按 text 区分。"""
        url = "https://api.test/j"

        def js(payload):
            return {"mimeType": "application/json", "text": payload}

        case = self._convert(
            [self._entry(url, js('{"a":1}')), self._entry(url, js('{"a":2}'))]
        )

        self.assertEqual(len(case.steps), 2)

    def test_same_params_in_different_order_is_not_collapsed(self):
        """顺序不同 → 判成两条（"少去重"无害，"误去重"才会丢录制）。"""
        url = "https://api.test/up"
        first = {
            "mimeType": "multipart/form-data",
            "params": [
                {"name": "a", "value": "1"},
                {"name": "b", "value": "2"},
            ],
        }
        second = {
            "mimeType": "multipart/form-data",
            "params": [
                {"name": "b", "value": "2"},
                {"name": "a", "value": "1"},
            ],
        }
        case = self._convert([self._entry(url, first), self._entry(url, second)])

        self.assertEqual(len(case.steps), 2)


class TestHarAdapter(unittest.TestCase):
    """HAR 解析：仓库素材 + 过滤/去重/断言模式。"""

    def setUp(self):
        self.tmp_dir = _tmp_dir("tmp_har")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def convert_asset(self, **kwargs) -> IRCase:
        return har_adapter.convert_har_file(
            HAR_ASSET, case_name="demo", case_stem="demo", **kwargs
        )

    def test_repo_asset_parses_three_entries(self):
        case = self.convert_asset()

        self.assertEqual(len(case.steps), 3)
        self.assertEqual(case.base_url, "https://postman-echo.com")
        self.assertEqual([step.method for step in case.steps], ["GET", "POST", "POST"])

    def test_query_string_becomes_params(self):
        case = self.convert_asset()

        self.assertEqual(case.steps[0].params, {"foo1": "HDnY8", "foo2": "34.5"})
        self.assertEqual(case.steps[0].url, "/get")

    def test_json_and_form_bodies(self):
        case = self.convert_asset()

        self.assertEqual(case.steps[1].json_body, {"foo1": "HDnY8", "foo2": 12.3})
        self.assertEqual(case.steps[2].data, {"foo1": "HDnY8", "foo2": "12.3"})

    def test_base64_response_body_still_yields_schema(self):
        """素材里的响应体是 base64 —— 必须先解码，否则形状断言会白白丢掉（实测踩过）。"""
        case = self.convert_asset()
        step = case.steps[0]

        self.assertIsNotNone(step.schema_file)
        self.assertIn("schemas/", step.schema_file)
        schema = case.extra_files[step.schema_file]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"]["args"]["type"], "object")

    def test_compressed_base64_response_still_yields_schema(self):
        """0920 批次 7 / **N42**：`content.compression` 必须先解压再解析。

        HAR 规范的 `content.compression` 是"这具响应体压缩了多少字节"
        （社区约定**非 0 即 gzip**），而 Chrome/Firefox 导出的常见组合就是
        `encoding: base64` + 压缩 + `compression: <size>`。

        修复前**完全不看这个字段** —— 解出来的 gzip 字节按 UTF-8 解失败，
        被当成"二进制内容"**静默丢掉形状断言**（`jsonschema_match` 整条消失），
        而告警还写着"base64 二进制/非 UTF-8"，把用户引向
        "这接口返回二进制"的**错误结论**。
        """
        import gzip  # noqa: PLC0415

        body = json.dumps({"code": 0, "data": {"id": 1, "name": "alice"}})
        for label, payload, compression in (
            ("gzip+数字 compression（Chrome 形态）", gzip.compress(body.encode()), None),
            ("gzip+字符串 compression", gzip.compress(body.encode()), "gzip"),
        ):
            with self.subTest(label=label):
                encoded = base64.b64encode(payload).decode("ascii")
                har = {
                    "log": {
                        "entries": [
                            {
                                "request": {
                                    "method": "GET",
                                    "url": "https://api.test/v1/user",
                                    "headers": [],
                                },
                                "response": {
                                    "status": 200,
                                    "headers": [
                                        {"name": "Content-Type", "value": "application/json"}
                                    ],
                                    "content": {
                                        "mimeType": "application/json",
                                        "text": encoded,
                                        "encoding": "base64",
                                        "compression": (
                                            len(encoded) if compression is None else compression
                                        ),
                                    },
                                },
                            }
                        ]
                    }
                }

                case = har_adapter.convert_har(har, case_name="n42", case_stem="n42")
                step = case.steps[0]

                self.assertIsNotNone(
                    step.schema_file, f"{label}: 形状断言被丢掉了（N42）"
                )
                schema = case.extra_files[step.schema_file]
                self.assertEqual(schema["type"], "object")
                self.assertIn("code", schema["properties"])

    def test_zlib_deflate_without_algorithm_name_is_handled(self):
        """数字形态没说算法名 —— 实测导出工具也会给 zlib/deflate，必须也能解。"""
        import zlib  # noqa: PLC0415

        body = json.dumps({"code": 0, "ok": True})
        encoded = base64.b64encode(zlib.compress(body.encode())).decode("ascii")
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://api.test/v1/x",
                            "headers": [],
                        },
                        "response": {
                            "status": 200,
                            "headers": [],
                            "content": {
                                "mimeType": "application/json",
                                "text": encoded,
                                "encoding": "base64",
                                "compression": len(encoded),
                            },
                        },
                    }
                ]
            }
        }

        case = har_adapter.convert_har(har, case_name="n42z", case_stem="n42z")

        self.assertIsNotNone(case.steps[0].schema_file)

    def test_no_compression_is_unchanged(self):
        """反向护栏：没有 `compression` 字段时行为逐字不变（既有路径）。"""
        body = json.dumps({"code": 0})
        encoded = base64.b64encode(body.encode()).decode("ascii")
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://api.test/v1/plain",
                            "headers": [],
                        },
                        "response": {
                            "status": 200,
                            "headers": [],
                            "content": {
                                "mimeType": "application/json",
                                "text": encoded,
                                "encoding": "base64",
                            },
                        },
                    }
                ]
            }
        }

        case = har_adapter.convert_har(har, case_name="n42p", case_stem="n42p")

        self.assertIsNotNone(case.steps[0].schema_file)

    def test_broken_compression_warns_instead_of_silently_dropping(self):
        """声明了压缩但实为明文 → 告警点名 `compression`，而不是说"二进制"。"""
        body = json.dumps({"code": 0})
        encoded = base64.b64encode(body.encode()).decode("ascii")
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://api.test/v1/broken",
                            "headers": [],
                        },
                        "response": {
                            "status": 200,
                            "headers": [],
                            "content": {
                                "mimeType": "application/json",
                                "text": encoded,
                                "encoding": "base64",
                                # 明明是明文，却声明压缩过
                                "compression": 123,
                            },
                        },
                    }
                ]
            }
        }

        case = har_adapter.convert_har(har, case_name="n42b", case_stem="n42b")

        warnings = "\n".join(case.all_warnings())
        self.assertIn("compression", warnings, warnings)
        self.assertIn("解压失败", warnings)

    def test_redirect_entries_are_skipped_with_a_clear_warning(self):
        """0920 / **缺陷 3**：录制的 3xx 是重定向中间态，必须整条跳过并告警。

        修复前会**忠实**生成 `eq: [status_code, 302]`，而框架默认
        `allow_redirects=True`，requests 会跟到最终响应 → 回放必然是
        `assert status_code equal 302 ==> fail`（实测）。
        """
        def entry(status, url, location=None):
            return {
                "request": {"method": "GET", "url": url, "headers": []},
                "response": {
                    "status": status,
                    "headers": [{"name": "Location", "value": location}] if location else [],
                    "content": {"mimeType": "application/json", "text": "{}"},
                    "redirectURL": location or "",
                },
            }

        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    entry(302, "https://api.test/redirect-to?url=/get", location="/get"),
                    entry(200, "https://api.test/get"),
                ],
            }
        }
        case = har_adapter.convert_har(har, case_name="redirect", assertion_mode="status")

        # 302 那条被跳过，只剩最终响应那一条
        self.assertEqual(len(case.steps), 1)
        self.assertEqual(case.steps[0].url, "/get")
        self.assertEqual(case.steps[0].assertions, [{"eq": ["status_code", 200]}])
        # 跳过必须是**可见**的，且要说明原因
        warnings = "\n".join(case.all_warnings())
        self.assertIn("重定向", warnings)
        self.assertIn("302", warnings)
        self.assertIn("allow_redirects", warnings)

    def test_all_redirect_statuses_are_skipped(self):
        """301/302/303/307/308 一个都不能漏（307/308 还会重发请求体）。"""
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                har = {
                    "log": {
                        "version": "1.2",
                        "entries": [
                            {
                                "request": {"method": "GET", "url": f"https://api.test/r{status}"},
                                "response": {
                                    "status": status,
                                    "headers": [{"name": "Location", "value": "/x"}],
                                    "content": {"mimeType": "text/plain", "text": ""},
                                },
                            }
                        ],
                    }
                }
                case = har_adapter.convert_har(
                    har, case_name="r", assertion_mode="status"
                )
                self.assertEqual(case.steps, [], f"{status} 没有被跳过")
                self.assertIn("重定向", "\n".join(case.all_warnings()))

    def test_normal_statuses_are_not_skipped(self):
        """反向护栏：2xx/4xx/5xx 都是真实结果，照旧忠实生成断言（不许误伤）。"""
        for status in (200, 201, 401, 404, 500):
            with self.subTest(status=status):
                har = {
                    "log": {
                        "version": "1.2",
                        "entries": [
                            {
                                "request": {"method": "GET", "url": f"https://api.test/s{status}"},
                                "response": {
                                    "status": status,
                                    "headers": [],
                                    "content": {"mimeType": "text/plain", "text": ""},
                                },
                            }
                        ],
                    }
                }
                case = har_adapter.convert_har(
                    har, case_name="s", assertion_mode="status"
                )
                self.assertEqual(len(case.steps), 1)
                self.assertEqual(case.steps[0].assertions, [{"eq": ["status_code", status]}])

    def test_headers_subtree_is_type_only(self):
        """服务端/网关生成的头不能写成 required（否则回放必挂）。"""
        case = self.convert_asset()
        schema = case.extra_files[case.steps[0].schema_file]

        self.assertEqual(schema["properties"]["headers"], {"type": "object"})

    def test_derived_headers_are_dropped(self):
        case = self.convert_asset()
        headers = case.steps[1].headers

        for dropped in ("Host", "Content-Length", "Cookie", "Accept-Encoding"):
            self.assertNotIn(dropped, headers)
        self.assertEqual(headers["Content-Type"], "application/json; charset=UTF-8")
        self.assertTrue(
            any("派生的请求头" in warning for warning in case.steps[1].warnings)
        )

    def test_cookies_are_placeholders_and_warned(self):
        case = self.convert_asset()

        self.assertEqual(case.steps[1].cookies, {"sails.sid": "${COOKIE_SAILS_SID}"})
        self.assertEqual(case.variables["COOKIE_SAILS_SID"], "${ENV(COOKIE_SAILS_SID)}")

    def test_assertions_are_status_plus_schema(self):
        case = self.convert_asset()
        assertions = case.steps[0].assertions

        self.assertEqual(assertions[0], {"eq": ["status_code", 200]})
        self.assertEqual(
            assertions[1], {"jsonschema_match": ["body", case.steps[0].schema_file]}
        )

    def test_status_only_mode(self):
        case = self.convert_asset(assertion_mode="status")

        self.assertEqual(case.steps[0].assertions, [{"eq": ["status_code", 200]}])
        self.assertEqual(case.extra_files, {})

    def test_no_assertions_mode(self):
        case = self.convert_asset(assertion_mode="none")

        self.assertEqual(case.steps[0].assertions, [])

    def test_static_assets_and_preflight_are_filtered(self):
        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {"method": "GET", "url": "https://api.test/app.js"},
                        "response": {"status": 200},
                    },
                    {
                        "request": {"method": "GET", "url": "https://api.test/logo.png"},
                        "response": {"status": 200},
                    },
                    {
                        "request": {"method": "OPTIONS", "url": "https://api.test/orders"},
                        "response": {"status": 204},
                    },
                    {
                        "request": {"method": "GET", "url": "https://api.test/aborted"},
                        "response": {"status": 0},
                    },
                    {
                        "_resourceType": "script",
                        "request": {"method": "GET", "url": "https://api.test/bundle"},
                        "response": {"status": 200},
                    },
                    {
                        "request": {"method": "GET", "url": "https://api.test/orders"},
                        "response": {"status": 200},
                    },
                ],
            }
        }
        case = har_adapter.convert_har(har, case_name="filtered")

        self.assertEqual([step.url for step in case.steps], ["/orders"])
        summary = " ".join(case.warnings)
        self.assertIn("已过滤 5 条非业务请求", summary)
        self.assertIn("静态资源", summary)
        self.assertIn("预检请求", summary)

    def test_include_hosts_filters(self):
        har = {
            "log": {
                "entries": [
                    {
                        "request": {"method": "GET", "url": "https://api.a.com/x"},
                        "response": {"status": 200},
                    },
                    {
                        "request": {"method": "GET", "url": "https://api.b.com/y"},
                        "response": {"status": 200},
                    },
                ]
            }
        }
        case = har_adapter.convert_har(har, include_hosts=["api.b.com"])

        self.assertEqual(len(case.steps), 1)
        # 被选中的那条成为 base_url，步骤写成相对路径
        self.assertEqual(case.base_url, "https://api.b.com")
        self.assertEqual(case.steps[0].url, "/y")
        self.assertTrue(any("不在 --include-hosts" in warning for warning in case.warnings))

    def test_duplicates_are_deduped(self):
        entry = {
            "request": {"method": "GET", "url": "https://api.test/orders"},
            "response": {"status": 200},
        }
        har = {"log": {"entries": [entry, dict(entry)]}}
        case = har_adapter.convert_har(har)

        self.assertEqual(len(case.steps), 1)
        self.assertTrue(any("去重" in warning for warning in case.warnings))

    def test_duplicates_kept_when_dedupe_disabled(self):
        entry = {
            "request": {"method": "GET", "url": "https://api.test/orders"},
            "response": {"status": 200},
        }
        case = har_adapter.convert_har(
            {"log": {"entries": [entry, dict(entry)]}}, dedupe=False
        )

        self.assertEqual(len(case.steps), 2)

    def test_duplicated_query_keys_fall_back_to_url(self):
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://api.test/list?tag=a&tag=b",
                            "queryString": [
                                {"name": "tag", "value": "a"},
                                {"name": "tag", "value": "b"},
                            ],
                        },
                        "response": {"status": 200},
                    }
                ]
            }
        }
        case = har_adapter.convert_har(har)
        step = case.steps[0]

        self.assertEqual(step.params, {})
        self.assertIn("tag=a&tag=b", step.url)
        self.assertTrue(any("重名键" in warning for warning in step.warnings))

    def test_duplicated_query_keys_are_rebuilt_when_url_has_no_query(self):
        """0920 批次 4 / **N22**：URL **不带**查询串时，重名键必须用 `queryString` 重建。

        修复前这里只看 `parsed.query`：`params` 已被清空、URL 又没补上查询串
        → **参数全部消失**，而告警还写着"已把查询串原样留在 URL 里"。
        """
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            # NOTICE: URL **不带**查询串，参数只在 queryString 里
                            "url": "https://api.test/items",
                            "queryString": [
                                {"name": "ids", "value": "1"},
                                {"name": "ids", "value": "2"},
                            ],
                        },
                        "response": {"status": 200},
                    }
                ]
            }
        }
        case = har_adapter.convert_har(har)
        step = case.steps[0]

        self.assertEqual(step.params, {})
        self.assertIn("ids=1", step.url, "重名查询参数凭空消失了（N22）")
        self.assertIn("ids=2", step.url)
        self.assertTrue(any("重名键" in warning for warning in step.warnings))

    def test_rebuilt_query_string_escapes_values(self):
        """重建时要转义：值里的 `&`/`=` 不能拼成**额外的参数**（比丢参数更糟）。"""
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://api.test/items",
                            "queryString": [
                                {"name": "q", "value": "a&b=1"},
                                {"name": "q", "value": "c d"},
                            ],
                        },
                        "response": {"status": 200},
                    }
                ]
            }
        }
        step = har_adapter.convert_har(har).steps[0]

        self.assertIn("q=a%26b%3D1", step.url, step.url)
        self.assertNotIn("&b=1", step.url, "值里的 & 没转义 → 会多出一个参数")

    def test_non_duplicated_query_still_uses_params(self):
        """反向护栏：无重名时照旧拆成 `params`（不许一律写进 URL）。"""
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://api.test/items",
                            "queryString": [
                                {"name": "page", "value": "2"},
                                {"name": "size", "value": "10"},
                            ],
                        },
                        "response": {"status": 200},
                    }
                ]
            }
        }
        step = har_adapter.convert_har(har).steps[0]

        self.assertEqual(step.params, {"page": "2", "size": "10"})
        self.assertEqual(step.url, "/items")

    def test_empty_har_warns(self):
        case = har_adapter.convert_har({"log": {"entries": []}})

        self.assertEqual(case.steps, [])
        self.assertTrue(any("没有 log.entries" in warning for warning in case.warnings))

    def test_curl_case_warns_that_no_assertion_is_generated(self):
        """0917-1：curl 命令没有响应体 → 生成物里没有任何断言，此前是静默的。"""
        case = curl_adapter.convert_curl_file(
            "curl https://api.test/orders -H 'Accept: application/json'",
            case_name="curl demo",
        )
        warnings = " ".join(case.all_warnings())

        self.assertTrue(case.steps, warnings)
        self.assertIn("没有响应内容", warnings)
        self.assertIn("没有生成任何断言", warnings)
        self.assertIn("hconvert --from har", warnings)  # 给出替代源

    def test_non_json_response_warns_instead_of_silent_skip(self):
        """0917-1：响应是 XML/纯文本时此前**静默**只断状态码，用户会以为已校验过。"""
        har = {
            "log": {
                "entries": [
                    {
                        "request": {"method": "POST", "url": "https://api.test/svc/query"},
                        "response": {
                            "status": 200,
                            "content": {
                                "mimeType": "text/xml; charset=utf-8",
                                "text": '<?xml version="1.0"?><r><code>0000</code></r>',
                            },
                        },
                    }
                ]
            }
        }
        case = har_adapter.convert_har(har)
        step = case.steps[0]
        warnings = " ".join(step.warnings)

        self.assertIn("不是 JSON", warnings)
        self.assertIn("只断状态码", warnings)
        self.assertIn("xpath_match", warnings)  # 给出补救方向，而不是只说"跳过了"
        # 断言里确实只有状态码（保持既有行为：不擅自造内容断言）
        self.assertEqual(len(step.assertions), 1)
        self.assertIn("status_code", str(step.assertions[0]))

    def test_plain_text_response_warns_too(self):
        har = {
            "log": {
                "entries": [
                    {
                        "request": {"method": "GET", "url": "https://api.test/health"},
                        "response": {
                            "status": 200,
                            "content": {"mimeType": "text/plain", "text": "OK"},
                        },
                    }
                ]
            }
        }
        case = har_adapter.convert_har(har)
        warnings = " ".join(case.steps[0].warnings)

        self.assertIn("不是 JSON", warnings)
        self.assertIn("普通文本", warnings)

    def test_json_response_does_not_get_the_non_json_warning(self):
        har = {
            "log": {
                "entries": [
                    {
                        "request": {"method": "GET", "url": "https://api.test/orders"},
                        "response": {
                            "status": 200,
                            "content": {
                                "mimeType": "application/json",
                                "text": '{"id": 1}',
                            },
                        },
                    }
                ]
            }
        }
        case = har_adapter.convert_har(har)
        warnings = " ".join(case.steps[0].warnings)

        self.assertNotIn("不是 JSON", warnings)
        self.assertTrue(any("形状断言" in warning for warning in case.steps[0].warnings))

    def test_dynamic_values_are_warned_not_rewritten(self):
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://api.test/orders",
                            "queryString": [{"name": "ts", "value": "1758112233445"}],
                        },
                        "response": {"status": 200},
                    }
                ]
            }
        }
        case = har_adapter.convert_har(har)
        step = case.steps[0]

        # 值保持原样（不自动改写），但必须提示
        self.assertEqual(step.params, {"ts": "1758112233445"})
        self.assertTrue(any("时间戳" in warning for warning in step.warnings))


class TestEmitAndValidate(unittest.TestCase):
    """IR → YAML → 导出即验证。"""

    def setUp(self):
        self.tmp_dir = _tmp_dir("tmp_emit")
        loader.project_meta = None

    def tearDown(self):
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_request_keys_stay_within_known_request_fields(self):
        """IR 字段必须与 `TRequest` 对齐 —— 否则会生成「框架不认」的 YAML（pydantic 静默忽略）。"""
        step = IRStep(
            name="all fields",
            method="POST",
            url="/everything",
            params={"a": 1},
            headers={"X-Test": "1"},
            cookies={"sid": "${COOKIE_SID}"},
            json_body={"a": 1},
            data=None,
            upload={"file": "x.txt"},
            verify=False,
            proxies={"http": "http://p:1"},
        )
        request = emit_yaml.build_request_dict(step)

        self.assertTrue(set(request) <= KNOWN_REQUEST_FIELDS, set(request) - KNOWN_REQUEST_FIELDS)
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["verify"], False)

    def test_emit_writes_yaml_and_schemas(self):
        case = IRCase(
            name="emit demo",
            base_url="http://127.0.0.1",
            steps=[
                IRStep(
                    name="step",
                    url="/get",
                    assertions=[{"jsonschema_match": ["body", "schemas/a.json"]}],
                    schema_file="schemas/a.json",
                    schema_content={"type": "object"},
                )
            ],
        )
        case.extra_files["schemas/a.json"] = {"type": "object"}

        written = emit_yaml.emit_case(case, self.tmp_dir)

        self.assertTrue(os.path.isfile(written["yaml"]))
        self.assertIn("schemas/a.json", written["extra"])
        self.assertTrue(os.path.isfile(written["extra"]["schemas/a.json"]))

    def test_upload_and_dict_data_are_normalized_into_upload(self):
        """0920 / **缺陷 1a**：`data`（dict）+ `upload` → 并进 `upload`，不再输出 `data`。

        修复前这里会同时输出 `data` 与 `upload`，而运行期 `prepare_upload_step`
        把 `step.request.data` 整个换成 `$m_encoder` → 普通字段**静默丢失**。
        """
        step = IRStep(
            name="mixed multipart",
            method="POST",
            url="/upload",
            data={"note": "hello"},
            upload={"file": "a.txt"},
        )
        request = emit_yaml.build_request_dict(step)

        self.assertNotIn("data", request)
        self.assertEqual(request["upload"], {"file": "a.txt", "note": "hello"})

    def test_upload_file_field_wins_over_same_named_data_field(self):
        """字段冲突时 `upload` 里的（文件）值优先，不被 `data` 顶掉。"""
        step = IRStep(
            name="conflict",
            method="POST",
            url="/upload",
            data={"file": "not-a-file", "note": "hello"},
            upload={"file": "a.txt"},
        )
        request = emit_yaml.build_request_dict(step)

        self.assertEqual(request["upload"], {"file": "a.txt", "note": "hello"})

    def test_upload_and_string_data_is_rejected(self):
        """`data` 是字符串（raw 体）+ `upload` → 无法等价表达，必须响亮报错。"""
        step = IRStep(
            name="impossible",
            method="POST",
            url="/upload",
            data="raw-body",
            upload={"file": "a.txt"},
        )

        with self.assertRaises(exceptions.ParamsError) as ctx:
            emit_yaml.build_request_dict(step)

        message = str(ctx.exception)
        self.assertIn("upload", message)
        self.assertIn("静默丢弃", message)

    def test_data_without_upload_is_unchanged(self):
        """反向护栏：没有 `upload` 时 `data` 两种形态都照旧输出。"""
        for data in ({"a": 1}, "a=1&b=2"):
            with self.subTest(data=data):
                step = IRStep(name="s", method="POST", url="/post", data=data)
                request = emit_yaml.build_request_dict(step)
                self.assertEqual(request["data"], data)
                self.assertNotIn("upload", request)

    def test_validate_accepts_generated_case(self):
        case = IRCase(
            name="valid case",
            base_url=HTTP_BIN_URL,
            steps=[IRStep(name="s", url="/get", assertions=[{"eq": ["status_code", 200]}])],
        )
        written = emit_yaml.emit_case(case, self.tmp_dir)

        source = emit_yaml.validate_emitted_case(written["yaml"])

        self.assertIn("class TestCaseValidCase", source)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp_dir, "valid_case_test.py")))

    def test_validate_rejects_bad_operator(self):
        """导出即验证必须真的能挡住坏产物（这里：算子名写错）。"""
        case = IRCase(
            name="bad case",
            base_url=HTTP_BIN_URL,
            steps=[IRStep(name="s", url="/get", assertions=[{"eqq": ["status_code", 200]}])],
        )
        written = emit_yaml.emit_case(case, self.tmp_dir)

        with self.assertRaises(Exception) as ctx:
            emit_yaml.validate_emitted_case(written["yaml"])
        self.assertIn("unknown assert comparator", str(ctx.exception))

    def test_validate_rejects_inline_schema(self):
        """内联 schema 也会被 hmake 拦住（P2-b 的护栏在导入链路上同样生效）。"""
        case = IRCase(
            name="inline schema case",
            base_url=HTTP_BIN_URL,
            steps=[
                IRStep(
                    name="s",
                    url="/get",
                    assertions=[
                        {"jsonschema_match": ["body", {"$schema": "https://x", "type": "object"}]}
                    ],
                )
            ],
        )
        written = emit_yaml.emit_case(case, self.tmp_dir)

        with self.assertRaises(Exception) as ctx:
            emit_yaml.validate_emitted_case(written["yaml"])
        self.assertIn("不支持内联 schema", str(ctx.exception))


class TestGoldenFiles(unittest.TestCase):
    """黄金文件快照：素材 → 生成物必须稳定（也顺带锁住「不泄露凭据」这件事）。"""

    def setUp(self):
        self.tmp_dir = _tmp_dir("tmp_golden")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_curl_asset_golden(self):
        case = curl_adapter.convert_curl_file(
            _read(CURL_ASSET), case_name="curl_examples", source=CURL_ASSET_POSIX
        )
        actual = emit_yaml.dump_case_yaml(case, "curl_examples.yml")

        self.assertNotIn(CURL_ASSET_TOKEN, actual)
        _assert_golden(self, actual, "curl_examples.yml")

    def test_har_asset_golden(self):
        case = har_adapter.convert_har_file(
            HAR_ASSET, case_name="demo", case_stem="demo", source=HAR_ASSET_POSIX
        )
        actual = emit_yaml.dump_case_yaml(case, "demo.yml")

        _assert_golden(self, actual, "har_demo.yml")

        # schema 文件按扩展名写成**真 JSON**（运行期按扩展名选解析器）
        written = emit_yaml.emit_case(case, self.tmp_dir)
        schema_path = written["extra"]["schemas/demo_01_get_get.json"]
        _assert_golden(self, _read(schema_path), "har_demo_schema.json")
        json.loads(_read(schema_path))  # 必须能被 JSON 解析器读回


class TestBatch0919_23PostmanSchemaAndVariables(unittest.TestCase):
    r"""批次 5 / H5 + H6：Postman 导入器的两处「生成物看起来没问题、跑起来不对」。

    ## H5：非 ASCII（中文）folder 名 → 两个用例的 schema 文件同名互相覆盖

    ```text
    两个中文 folder「用户管理」「订单管理」各含同名 item「list」
      日志：两个用例都打印「1 个 schema 文件」        → 声称 2 个
      磁盘：schemas/ 下**只有 1 个**文件，内容是**后一个**用例的
      两个 YAML：引用**同一个**路径 → 前一个用例断到别人的响应形状
    ```

    根因：`_case_stem` 写的是 `re.sub(r"[^0-9A-Za-z]+", "_", name)` —— 中文被**整段抹掉**，
    两个用例名归一到同一个词干。而用例自身的 `.yml` 文件名一直是**保留中文**的
    （`..._用户管理.yml`），所以"保留中文"在本仓本来就被证明可用。

    ## H6：只出现在 URL host 里的变量被从 `config.variables` 里过滤掉

    ```text
    生成物   base_url: https://${baseUrl}
    variables 段：整个消失（连定义都没有）
    告警     变量 baseUrl 在集合里没有定义 → 已按 `${ENV(baseUrl)}` 占位   ← 说了要登记，实际没登记
    运行期   VariableNotFound: baseUrl not found in {}
    ```

    根因：`_used_variable_names` 只扫 `case.steps`，**不扫 `case.base_url`**；
    而 `{{baseUrl}}`/`{{host}}` 只出现在 host 上是 Postman 最常见的形态。
    """

    def setUp(self):
        self.tmp_dir = _tmp_dir("tmp_batch5_pm")
        loader.project_meta = None
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        self._original_argv = sys.argv
        # 护栏的报错走 `logger.error` —— 想断言"报错文案点名了 schema 文件"就得自己收。
        # NOTICE: `hconvert` 会调 `cli.init_logger()`，而它是 `logger.remove()`（**清掉所有
        # 现有 handler**）+ 重新初始化 → 我们的 sink 会被无声地拆掉（然后 `logger.remove`
        # 在清理时抛 `ValueError: There is no existing handler with id N`）。
        # 所以 `_run_cli` 里把 `init_logger` 打桩成 no-op（与本文件既有的 `_run_cli_with_logs` 同款）。
        self._errors: list = []
        self._sink_id = logger.add(self._errors.append, level="ERROR", format="{message}")
        self.addCleanup(self._cleanup_log_sink)

    def _cleanup_log_sink(self):
        try:
            logger.remove(self._sink_id)
        except ValueError:
            pass

    def tearDown(self):
        sys.argv = self._original_argv
        loader.project_meta = None
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _run_cli(self, *args) -> int:
        sys.argv = ["hconvert", *args]
        # NOTICE: 打桩 `init_logger`，否则它内部的 `logger.remove()` 会把上面那个
        # ERROR sink 拆掉（护栏的报错就收不到了）。与 `_run_cli_with_logs` 同款处理。
        with mock.patch("interfacetester.cli.init_logger", lambda level: None):
            try:
                main_convert_alias()
            except SystemExit as ex:
                return int(ex.code or 0)
        return 0

    @staticmethod
    def _item(name: str, example: dict) -> dict:
        """带一条保存示例响应的请求（这样才会生成 schema 断言）。"""
        return {
            "name": name,
            "request": {
                "method": "GET",
                "url": "https://api.example.com/list",
                "header": [{"key": "Accept", "value": "application/json"}],
            },
            "response": [
                {
                    "name": "ok",
                    "code": 200,
                    "header": [{"key": "Content-Type", "value": "application/json"}],
                    "body": json.dumps(example),
                }
            ],
        }

    def _collection(self, items: list, name: str = "col", variables=None) -> str:
        payload = {"info": {"name": name, "schema": "x"}, "item": items}
        if variables is not None:
            payload["variable"] = variables
        return _write(
            os.path.join(self.tmp_dir, f"{name}.postman.json"),
            json.dumps(payload, ensure_ascii=False),
        )

    def _convert(self, collection: str, out_dir: str, extra=()):
        code = self._run_cli(
            "--from", "postman", "--in", collection, "--out", out_dir,
            "--assertions", "status+schema", *extra,
        )
        yamls, schemas = [], []
        for dirpath, _dirs, files in os.walk(out_dir):
            for fn in files:
                if fn.endswith(".yml"):
                    yamls.append(os.path.join(dirpath, fn))
                elif fn.endswith(".json"):
                    schemas.append(os.path.join(dirpath, fn))
        return code, sorted(yamls), sorted(schemas)

    # ------------------------------------------------------------------ H5
    def test_cjk_folders_get_distinct_schema_files(self):
        """中文 folder 的用例必须各拿各的 schema 文件（修复前是同一个）。"""
        collection = self._collection(
            [
                {"name": "用户管理", "item": [self._item("list", {"id": 1, "name": "bob"})]},
                {"name": "订单管理", "item": [self._item("list", {"orderNo": "A1", "total": 9})]},
            ]
        )
        out_dir = os.path.join(self.tmp_dir, "h5")

        code, yamls, schemas = self._convert(collection, out_dir)

        self.assertEqual(code, 0)
        self.assertEqual(len(yamls), 2)
        self.assertEqual(
            len(schemas), 2, "两个用例应当各有一份 schema 文件（修复前只有 1 份、互相覆盖）"
        )
        # 每份 schema 的内容必须是**自己那个用例**的响应形状
        by_name = {os.path.basename(p): json.load(open(p, encoding="utf-8")) for p in schemas}
        texts = {name: json.dumps(payload, ensure_ascii=False) for name, payload in by_name.items()}
        self.assertTrue(
            any("用户管理" in name for name in by_name), f"schema 文件名里应当保留中文：{list(by_name)}"
        )
        self.assertTrue(any("id" in t for t in texts.values()))
        self.assertTrue(any("orderNo" in t for t in texts.values()))

        # 每个 YAML 引用的是**自己**那份
        for yaml_path in yamls:
            content = open(yaml_path, encoding="utf-8").read()
            refs = set(re.findall(r"schemas/[^\s'\"]+\.json", content))
            self.assertEqual(len(refs), 1, f"{yaml_path} 引用的 schema 不唯一：{refs}")
            referenced = next(iter(refs))
            stem = os.path.basename(yaml_path).replace(".yml", "").split("_", 1)[-1]
            self.assertIn(
                stem,
                referenced,
                "用例引用的 schema 文件名里没有自己的名字 —— 很可能又撞到别人的了",
            )

    def test_ascii_names_keep_the_previous_schema_file_name(self):
        """反向护栏：纯 ASCII 名字的 schema 文件名与修复前**逐字节一致**（不做无谓改名）。"""
        collection = self._collection(
            [{"name": "user admin", "item": [self._item("list users", {"id": 1})]}],
            name="asciidemo",
        )
        out_dir = os.path.join(self.tmp_dir, "h5_ascii")

        _code, _yamls, schemas = self._convert(collection, out_dir)

        self.assertEqual(
            [os.path.basename(p) for p in schemas],
            # NOTICE: 用例名是 `{集合名} / {folder}`，而**集合名取的是文件名去掉 .json**
            # （`asciidemo.postman`）——所以词干里会有 `_postman`。第一版我把期望写成
            # `asciidemo_user_admin_...`，是我对集合名的来源想当然了。
            ["asciidemo_postman_user_admin_01_list_users.json"],
        )

    def test_schema_file_collision_is_refused_before_writing(self):
        """两个用例名归一成**同一个词干**时必须响亮报错（修复前静默覆盖）。

        构造：folder 名 `用户.管理` 与 `用户 管理` ——
        `.yml` 文件名分别是 `..._用户.管理.yml` / `..._用户_管理.yml`（**不同**，所以
        `.yml` 那层护栏拦不住），但 schema 词干都会把 `.`/空格折成 `_` → 同一个词干。
        """
        collection = self._collection(
            [
                {"name": "用户.管理", "item": [self._item("list", {"id": 1})]},
                {"name": "用户 管理", "item": [self._item("list", {"id": 2})]},
            ]
        )
        out_dir = os.path.join(self.tmp_dir, "h5_collide")

        code, yamls, schemas = self._convert(collection, out_dir)

        self.assertEqual(code, 1, "schema 文件冲突必须让导入失败，而不是静默覆盖")
        # NOTICE: 护栏在**循环里**逐条用例检查（与既有的 `.yml` 护栏同一形态），
        # 所以冲突被发现的**那一刻之前**已经写出去的用例会留在磁盘上。
        # 真正有意义的断言是：**冲突的那个用例没有被写**（否则就是静默覆盖）。
        self.assertEqual(len(yamls), 1, f"冲突用例不该被写出，实际写出：{yamls}")
        self.assertEqual(len(schemas), 1, f"冲突用例的 schema 不该被写出，实际：{schemas}")
        self.assertIn(
            "同一个 schema 文件",
            "\n".join(self._errors),
            # NOTICE: `self._errors` 是**列表**，`assertIn` 在列表上是"元素相等"而不是子串包含
            # —— 必须 join 成字符串再断言（这里踩过一次，与"`_warnings()` 返回的是字符串"同类错误）。
            "护栏没有点明是 schema 文件冲突",
        )

    # ------------------------------------------------- 批次 9-1：模块名护栏
    def test_module_name_collision_is_refused_before_writing(self):
        r"""`用户-管理` 与 `用户 管理`：前两层护栏都拦不住，必须由**模块名**护栏拦。

        构造的依据是三层的归一集合**互不相同**（这不是巧合，是本用例要钉住的边界）：

        ```text
        folder 名          .yml 名(slugify)       schema 词干(safe_file_stem)  模块名(normalize_module_segment)
        用户-管理          用户-管理             用户-管理                    用户_管理
        用户 管理          用户_管理             用户_管理                    用户_管理
                          ↑ 不同                ↑ 不同（`-` 被保留）          ↑ **相同** → 必须在这里报
        ```

        修复前这一对素材会一路走到 `make_testcase` 的生成物撞名检查才失败：报错说的是
        「生成物撞名」，而用户刚做的是「导入」——定位成本高，而且 `--no-validate` 下
        连这一步都不会发生（`hconvert` exit 0，直到 `hrun` 才炸）。
        """
        collection = self._collection(
            [
                {"name": "用户-管理", "item": [self._item("list", {"id": 1})]},
                {"name": "用户 管理", "item": [self._item("list", {"id": 2})]},
            ]
        )
        out_dir = os.path.join(self.tmp_dir, "b91_module")

        code, yamls, schemas = self._convert(collection, out_dir)

        self.assertEqual(code, 1, "会落到同一个 `*_test.py` 的两个用例必须在写盘前拦下")
        self.assertEqual(len(yamls), 1, f"冲突用例不该被写出，实际写出：{yamls}")

        errors = "\n".join(self._errors)
        self.assertIn("同一个 `*_test.py`", errors, "护栏没有点明是生成物模块名冲突")
        # 报错必须能定位到两个用例名（否则用户不知道改哪个）
        self.assertIn("用户-管理", errors)
        self.assertIn("用户 管理", errors)

    def test_ascii_step_name_keeps_the_previous_schema_file_name(self):
        """反向护栏（批次 9-1）：纯 ASCII 步骤名的 schema 文件名与修复前**逐字节一致**。"""
        collection = self._collection(
            [{"name": "asciistep", "item": [self._item("list users", {"id": 1})]}],
            name="asciistep",
        )
        out_dir = os.path.join(self.tmp_dir, "b91_ascii_step")

        code, _yamls, schemas = self._convert(collection, out_dir)

        self.assertEqual(code, 0)
        self.assertEqual(
            [os.path.basename(p) for p in schemas],
            ["asciistep_postman_asciistep_01_list_users.json"],
        )

    def test_cjk_step_name_is_not_folded_away(self):
        """批次 9-1：HAR/OpenAPI/Postman 的**步骤**词干统一保留非 ASCII（H5 的遗留口子）。

        修复前 Postman 用 `ir.safe_file_stem`（保留中文），而 HAR/OpenAPI 的步骤词干仍是
        `re.sub(r"[^0-9A-Za-z]+", "_", ...)` —— 中文 item 名会整段退化成 `step`
        （文件名读不出业务含义），同一子系统里两套规则。
        """
        from interfacetester.converters.har_adapter import _schema_file_name

        # HAR：步骤词干保留中文，且两个不同中文步骤名不再退化成同一个 `step`
        self.assertEqual(
            _schema_file_name("demo", 1, "查询用户"),
            "schemas/demo_01_查询用户.json",
        )
        self.assertNotEqual(
            _schema_file_name("demo", 1, "查询用户"),
            _schema_file_name("demo", 1, "查询订单"),
        )
        # 纯 ASCII 逐字节不变（与 H5 的反向护栏同一口径）
        self.assertEqual(
            _schema_file_name("demo", 1, "GET /get"),
            "schemas/demo_01_get_get.json",
        )

        # OpenAPI：同一个函数、同一个规则 —— 直接调它的调用点（`_responses_assertions`），
        # 钉住「步骤词干」这一处不再退化成 ASCII。
        # NOTICE: 刻意**不**用 `inspect.getsource` 扫源码文本 —— 本文件与被测模块的注释里
        # 都引用了那行旧写法（作为"修复前是什么"的说明），扫文本会扫到注释本身而误报。
        # 钉行为、不钉文本。
        from interfacetester.converters.openapi_adapter import _responses_assertions

        operation = {
            "responses": {
                "200": {
                    "description": "ok",
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "properties": {"id": {"type": "integer"}}}
                        }
                    },
                }
            }
        }
        warnings: list = []
        assertions, schema_file, _content = _responses_assertions(
            operation,
            {},
            warnings,
            "pets",
            1,
            "GET /pets — 查询宠物列表",
            "status+schema",
        )

        self.assertEqual(schema_file, "schemas/pets_01_get_pets_查询宠物列表.json")
        self.assertEqual(assertions[-1]["jsonschema_match"], ["body", schema_file])

    # ------------------------------------------------------------------ H6
    def test_host_only_variable_is_registered_in_config_variables(self):
        """只出现在 URL host 的 `{{baseUrl}}` 必须真的登记到 `config.variables`。"""
        collection = self._collection(
            [
                {
                    "name": "get-pets",
                    "request": {
                        "method": "GET",
                        "url": "https://{{baseUrl}}/pets?page=1",
                        "header": [{"key": "Accept", "value": "application/json"}],
                    },
                    "response": [],
                }
            ],
            name="baseurl",
        )
        out_dir = os.path.join(self.tmp_dir, "h6")

        code, yamls, _schemas = self._convert(collection, out_dir)

        self.assertEqual(code, 0)
        content = open(yamls[0], encoding="utf-8").read()
        self.assertIn("base_url: https://${baseUrl}", content)
        self.assertIn(
            "baseUrl: ${ENV(baseUrl)}",
            content,
            "变量只出现在 host 上时被过滤掉了 —— 运行期会 VariableNotFound，"
            "而告警却声称已按 ${ENV(baseUrl)} 占位",
        )

    def test_unused_item_level_variable_is_still_filtered_out(self):
        """反向护栏：**没被引用**的变量仍要被过滤（这条过滤本身是对的）。

        NOTICE: 修复 H6 只是"补扫 `base_url`"，不是把过滤器拆掉 ——
        否则每次转换派生出来的一堆变量会灌进用例文件。

        NOTICE: 这里必须用**item 级**变量。**集合级**（`variable[]`）变量按设计**永远保留**
        （`name in base_variables` 那一支）——我第一版拿集合级变量当反例，结果它本来就在
        生成物里，测试白红了一次。
        """
        item = self._item("list", {"id": 1})
        item["variable"] = [{"key": "UNUSED_ITEM_TOKEN", "value": "x"}]
        collection = self._collection([item], name="unusedvar")
        out_dir = os.path.join(self.tmp_dir, "h6_unused")

        _code, yamls, _schemas = self._convert(collection, out_dir)

        content = open(yamls[0], encoding="utf-8").read()
        self.assertNotIn(
            "UNUSED_ITEM_TOKEN",
            content,
            "没被任何步骤引用的变量被写进了 config.variables（过滤器被拆掉了）",
        )


class TestBatch0919_24ImporterQuietFailures(unittest.TestCase):
    r"""批次 6：五个源各自的"生成物看着没问题、跑起来不对"。

    每条一个根因，逐条钉住（都是**静默**类：零告警或零信号）：

    | # | 源 | 修复前 |
    | --- | --- | --- |
    | M9 | HAR | `queryString` 缺失但 URL 带查询串 → **整个查询串被丢掉**（回放的是另一个请求） |
    | M10 | OpenAPI | 相对 `servers[].url`（`/v1`）→ `base_url: /v1`，**零告警**，运行期必失败 |
    | M11 | OpenAPI | 未声明的路径模板变量 → 原样进 URL，实跑发出 `GET /pets/%7BpetId%7D` |
    | M12 | Postman | 第一条示例是 404 时，断言就断 404（接口正常反而红灯） |
    | M13 | curl | 多主机告警把 `base_url` 的 userinfo **口令明文**抄进 stdout / `--report` |
    """

    # ------------------------------------------------------------------ M9
    @staticmethod
    def _har(url, query_string=None):
        request = {"method": "GET", "url": url, "headers": []}
        if query_string is not None:
            request["queryString"] = query_string
        return {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": request,
                        "response": {
                            "status": 200,
                            "content": {
                                "mimeType": "application/json",
                                "text": '{"ok":true}',
                            },
                        },
                    }
                ],
            }
        }

    def test_har_url_query_is_kept_when_querystring_is_missing(self):
        """M9：`queryString` 缺失时**必须**从 URL 兜底（修复前整个查询串消失）。"""
        case = har_adapter.convert_har(
            self._har("https://api.example.com/items?page=2&ids=1,2")
        )
        step = case.steps[0]

        self.assertEqual(step.url, "/items", "查询串应当从 URL 上摘下来放进 params")
        self.assertEqual(
            step.params,
            {"page": "2", "ids": "1,2"},
            "URL 里的查询参数被丢掉了（回放的是另一个请求）",
        )
        self.assertIn("queryString", "\n".join(case.all_warnings()), "兜底必须可见（有告警）")

    def test_har_url_query_with_duplicates_stays_in_the_url(self):
        """兜底同样遵守既有的"重名键装不进 dict"口径：查询串**原样留在 URL 里**。

        NOTICE: 第一版我拿 `ids=1,2&ids=3`（重名）当主用例，断言 `step.url == "/items"`，
        结果红了 —— 那是**另一条分支**（重名 → 保留整串）。两条分支各测一条才是对的。
        """
        case = har_adapter.convert_har(
            self._har("https://api.example.com/items?ids=1&ids=2")
        )
        step = case.steps[0]

        self.assertEqual(step.url, "/items?ids=1&ids=2")
        self.assertEqual(step.params, {})

    def test_har_querystring_still_wins_when_present(self):
        """反向护栏：`queryString` 存在时口径不变（仍以它为准）。"""
        case = har_adapter.convert_har(
            self._har(
                "https://api.example.com/items?page=2",
                query_string=[{"name": "page", "value": "9"}],
            )
        )

        self.assertEqual(case.steps[0].params, {"page": "9"})

    # ----------------------------------------------------------------- M10
    @staticmethod
    def _openapi(server_url):
        return {
            "openapi": "3.0.3",
            "info": {"title": "t", "version": "1"},
            "servers": [{"url": server_url}],
            "paths": {"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}},
        }

    def test_relative_server_url_warns_loudly(self):
        """M10：相对 `servers[].url` 必须告警（修复前零告警 + 打印"导出即验证通过"）。"""
        case = openapi_adapter.convert_openapi(self._openapi("/v1"), case_name="rel")[0]

        warnings = "\n".join(case.all_warnings())
        self.assertIn("相对地址", warnings)
        self.assertIn("/v1", warnings)

    def test_absolute_server_url_has_no_relative_warning(self):
        """反向护栏：绝对地址不该出现这条告警。"""
        case = openapi_adapter.convert_openapi(
            self._openapi("https://api.example.com/v1"), case_name="abs"
        )[0]

        self.assertNotIn("相对地址", "\n".join(case.all_warnings()))

    # ----------------------------------------------------------------- M11
    def test_undeclared_path_template_variable_is_bound_and_warned(self):
        """M11：契约没声明的 `{petId}` 不能原样进 URL（实跑会发出 `%7BpetId%7D`）。"""
        document = {
            "openapi": "3.0.3",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/pets/{petId}": {
                    "get": {"responses": {"200": {"description": "ok"}}}
                }
            },
        }

        case = openapi_adapter.convert_openapi(document, case_name="undeclared")[0]
        step = case.steps[0]

        # NOTICE: 判据要写成"裸 `{petId}` 不在 URL 里" —— 直接 `assertNotIn("{petId}", url)`
        # 会被 `${petId}` 里那段子串命中（第一版就是这么写错的）。
        self.assertEqual(
            step.url,
            "/pets/${petId}",
            "模板变量没有换成变量引用（实跑会发出 %7BpetId%7D）",
        )
        self.assertIn("petId", case.variables, "造出来的值要登记进 config.variables")
        self.assertIn("没有声明", "\n".join(case.all_warnings()))

    def test_declared_path_parameter_is_unchanged(self):
        """反向护栏：**已声明**的路径参数口径不变（值来自 example、无"没有声明"告警）。"""
        document = {
            "openapi": "3.0.3",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/pets/{petId}": {
                    "get": {
                        "parameters": [
                            {
                                "in": "path",
                                "name": "petId",
                                "required": True,
                                "example": "pet-001",
                            }
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }

        case = openapi_adapter.convert_openapi(document, case_name="declared")[0]

        self.assertEqual(case.variables["petId"], "pet-001")
        self.assertNotIn("没有声明", "\n".join(case.all_warnings()))

    # ----------------------------------------------------------------- M12
    @staticmethod
    def _postman_with_examples(examples):
        return {
            "info": {"name": "col", "schema": "x"},
            "item": [
                {
                    "name": "login",
                    "request": {
                        "method": "POST",
                        "url": "https://api.example.com/login",
                        "header": [],
                    },
                    "response": [
                        {
                            "name": name,
                            "code": code,
                            "header": [{"key": "Content-Type", "value": "application/json"}],
                            "body": json.dumps({"ok": True}),
                        }
                        for name, code in examples
                    ],
                }
            ],
        }

    def test_error_example_first_does_not_become_the_expected_status(self):
        """M12：第一条示例是 404、第二条是 200 时，断言必须断 **200**。"""
        case = postman_adapter.convert_postman(
            self._postman_with_examples([("unauthorized", 401), ("ok", 200)]),
            case_name="pm12",
            assertion_mode="status",
        )[0]

        assertions = case.steps[0].assertions
        self.assertEqual(assertions, [{"eq": ["status_code", 200]}])

    def test_all_error_examples_warn_loudly(self):
        """M12 的另一半：**只有**错误示例时必须告警说明"把错误分支当成了期望"。

        NOTICE（0920 / 缺陷 3）：判据从 `code < 400` 收紧成 `200 <= code < 300`，
        告警文案随之从"全是错误响应"改成"没有 2xx"（404 这类错误响应仍然是"没有 2xx"
        的子集，回退行为与断言**逐字不变**）。这里钉住的是**告警这件事**与回退结果。
        """
        case = postman_adapter.convert_postman(
            self._postman_with_examples([("not found", 404)]),
            case_name="pm12b",
            assertion_mode="status",
        )[0]

        warnings = "\n".join(case.all_warnings())
        self.assertIn("没有 2xx", warnings)
        self.assertEqual(case.steps[0].assertions, [{"eq": ["status_code", 404]}])

    def test_redirect_only_examples_do_not_become_the_baseline(self):
        """0920 / 缺陷 3：**只有 3xx** 的示例不能当成功基准。

        修复前 `code < 400` 把 302 判成"成功响应"并优先选中它，生成的
        `eq: [status_code, 302]` 在回放时**必然失败**（框架默认跟随重定向 → 实际 200）。
        修复后：没有 2xx → 回退第一条并**告警说明 3xx 也会失败**。
        """
        case = postman_adapter.convert_postman(
            self._postman_with_examples([("redirect", 302), ("moved", 301)]),
            case_name="pm_redirect_only",
            assertion_mode="status",
        )[0]

        warnings = "\n".join(case.all_warnings())
        self.assertIn("没有 2xx", warnings)
        self.assertIn("3xx", warnings, "必须说清 3xx 也不能当基准")
        # 回退到第一条（302），但用户已被明确告知这条断言会失败
        self.assertEqual(case.steps[0].assertions, [{"eq": ["status_code", 302]}])

    def test_2xx_wins_over_earlier_3xx_example(self):
        """0920 / 缺陷 3 主判据：3xx 排在前面时，基准仍必须是 2xx。"""
        case = postman_adapter.convert_postman(
            self._postman_with_examples([("redirect", 302), ("ok", 200)]),
            case_name="pm_redirect_then_ok",
            assertion_mode="status",
        )[0]

        self.assertEqual(case.steps[0].assertions, [{"eq": ["status_code", 200]}])
        self.assertNotIn("没有 2xx", "\n".join(case.all_warnings()))

    def test_success_example_alone_is_unchanged(self):
        """反向护栏：只有成功示例时行为不变。"""
        case = postman_adapter.convert_postman(
            self._postman_with_examples([("ok", 200)]), case_name="pm12c",
            assertion_mode="status",
        )[0]

        self.assertEqual(case.steps[0].assertions, [{"eq": ["status_code", 200]}])
        self.assertNotIn("没有 2xx", "\n".join(case.all_warnings()))

    # ----------------------------------------------------------------- M13
    def test_multi_host_warning_does_not_leak_userinfo(self):
        """M13：告警文本不得出现 `base_url` 里的 userinfo 口令（会进 stdout 与 --report）。"""
        password = "S3cr3t-CANARY-9f3a"
        content = (
            f"curl 'https://alice:{password}@a.example.com/x'\n"
            f"curl 'https://b.example.com/y'\n"
        )

        case = curl_adapter.convert_curl_file(content, "curl_multi", "test")
        warnings = "\n".join(case.all_warnings())

        self.assertNotIn(password, warnings, "告警把口令明文抄进 stdout / --report 了")
        self.assertIn("***@", warnings, "仍然要能看出'这个 host 是带凭据的那个'")
        # 生成物本身一直是干净的（这条 M13 只泄漏在告警这一路）
        self.assertNotIn(password, emit_yaml.dump_case_yaml(case, "curl_multi.yml"))


class TestConvertCli(unittest.TestCase):
    """CLI 与「生成物真能跑」的端到端验证（用本地 mock，不依赖外网）。"""

    def setUp(self):
        self.tmp_dir = _tmp_dir("tmp_cli_conv")
        loader.project_meta = None
        # NOTICE: make 的模块级缓存是**进程级**的，且 `main_make` 会对缓存里的**所有**路径跑 black；
        # 上一条测试的临时目录已被删除 → black 会对不存在的路径报错（噪音）。
        # 这是框架既有的坑，测试里按惯例清掉（与 tests/make_test.py 一致）。
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        self._original_argv = sys.argv

    def tearDown(self):
        sys.argv = self._original_argv
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _run_cli(self, *args) -> int:
        """走真实入口 `hconvert`（= `interfacetester convert`）。"""
        sys.argv = ["hconvert", *args]
        try:
            main_convert_alias()
        except SystemExit as ex:
            return int(ex.code or 0)
        return 0

    def test_unknown_source_is_reported(self):
        """`--from postman` 还没实现 → 退出码 2 且给出路线图提示。"""
        code = self._run_cli(
            "--from", "postman", "--in", CURL_ASSET, "--out", self.tmp_dir
        )
        self.assertEqual(code, 2)

    def test_curl_end_to_end_against_local_mock(self):
        """curl → 生成 YAML → hmake → pytest 跑通（本地 mock，离线）。"""
        curl_file = _write(
            os.path.join(self.tmp_dir, "mock_case.txt"),
            f"curl {HTTP_BIN_URL}/get -H 'Accept: application/json'",
        )
        out_dir = os.path.join(self.tmp_dir, "out")

        code = self._run_cli(
            "--from", "curl", "--in", curl_file, "--out", out_dir, "--name", "mock_smoke"
        )
        self.assertEqual(code, 0)

        yaml_path = os.path.join(out_dir, "mock_smoke.yml")
        self.assertTrue(os.path.isfile(yaml_path))
        # 导出即验证已经生成了 *_test.py
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "mock_smoke_test.py")))

        exit_code = main_run([yaml_path, "--import-mode=importlib"])
        self.assertEqual(exit_code, 0)

    def test_har_end_to_end_against_local_mock(self):
        """HAR（含 base64 响应体）→ 生成 YAML + schema → pytest 跑通。"""
        body = json.dumps({"args": {}, "url": f"{HTTP_BIN_URL}/get"}).encode("utf-8")
        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": f"{HTTP_BIN_URL}/get",
                            "headers": [{"name": "Accept", "value": "application/json"}],
                            "queryString": [],
                        },
                        "response": {
                            "status": 200,
                            "content": {
                                "mimeType": "application/json; charset=utf-8",
                                "encoding": "base64",
                                "text": base64.b64encode(body).decode("ascii"),
                            },
                        },
                    }
                ],
            }
        }
        har_path = _write(os.path.join(self.tmp_dir, "mock_case.har"), json.dumps(har))
        out_dir = os.path.join(self.tmp_dir, "out_har")

        code = self._run_cli("--from", "har", "--in", har_path, "--out", out_dir, "--name", "har_smoke")
        self.assertEqual(code, 0)

        yaml_path = os.path.join(out_dir, "har_smoke.yml")
        generated = yaml.safe_load(_read(yaml_path))
        # 形状断言走文件引用（不能内联）
        self.assertIn("jsonschema_match", json.dumps(generated["teststeps"][0]["validate"]))
        self.assertTrue(os.path.isdir(os.path.join(out_dir, "schemas")))

        exit_code = main_run([yaml_path, "--import-mode=importlib"])
        self.assertEqual(exit_code, 0)

    def test_report_is_written(self):
        curl_file = _write(os.path.join(self.tmp_dir, "c.txt"), f"curl {HTTP_BIN_URL}/get")
        report_path = os.path.join(self.tmp_dir, "report.md")

        code = self._run_cli(
            "--from", "curl", "--in", curl_file, "--out", os.path.join(self.tmp_dir, "o"),
            "--report", report_path,
        )

        self.assertEqual(code, 0)
        report = _read(report_path)
        self.assertIn("# 导入报告", report)
        self.assertIn("需要人工确认的点", report)


class TestBatch0918_8ConvertCli(unittest.TestCase):
    r"""0918-8：导入器 CLI 批次——M21（生成物撞名）/ M23（BOM 与诊断可见性）。

    NOTICE（修复前实测）：
    - **M21**：`_slug` 把 `/`、空白等统一换成 `_`，于是 Postman 里 `user admin` 与 `user/admin`
      两个 folder 都生成 `col_user_admin.yml`——后者**整体覆盖**前者（前一个用例的步骤凭空消失），
      而日志两行都打印同一个路径、末尾还写「共 2 个用例文件」，退出码 0；
    - **M23**：带 BOM 的素材（PowerShell 5.1 的 `Set-Content -Encoding UTF8` 就这么写）会让
      curl 解析出**零条命令**，而真实诊断（"第 1 行的内容不是 curl 命令，已跳过：\ufeffcurl …"）
      被一段**永不执行**的循环吞掉，用户只剩一句泛泛的「请检查输入内容」。
    """

    def setUp(self):
        self.tmp_dir = _tmp_dir("tmp_cli_83")
        loader.project_meta = None
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        self._original_argv = sys.argv

    def tearDown(self):
        sys.argv = self._original_argv
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _run_cli_with_logs(self, *args):
        """跑真实 `hconvert` 入口并捕获 loguru 日志。

        NOTICE: `cli.main()` 会调 `init_logger()`，而它内部是 `logger.remove()` +
        重新 add —— 会把本测试挂的 sink **整个删掉**（表现为 `ValueError: There is no
        existing handler with id …`）。所以这里把 `init_logger` 打桩成 no-op，
        让 sink 活到调用结束（本测试只关心告警文本，不关心日志格式）。
        """
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            with mock.patch("interfacetester.cli.init_logger", lambda level: None):
                sys.argv = ["hconvert", *args]
                try:
                    main_convert_alias()
                    code = 0
                except SystemExit as ex:
                    code = int(ex.code or 0)
        finally:
            try:
                logger.remove(sink_id)
            except ValueError:
                pass
        return code, messages

    # ---------------------------------------------------------------- M23
    def test_bom_prefixed_curl_asset_is_read(self):
        """带 BOM 的 curl 素材必须能正常导入（修复前：0 步骤、exit 1、报错误导）。"""
        bom_path = os.path.join(self.tmp_dir, "bom.txt")
        with open(bom_path, "w", encoding="utf-8-sig") as fp:
            fp.write(f"curl {HTTP_BIN_URL}/get")

        out_dir = os.path.join(self.tmp_dir, "out_bom")
        code, _messages = self._run_cli_with_logs(
            "--from", "curl", "--in", bom_path, "--out", out_dir, "--name", "bom_case"
        )

        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "bom_case.yml")))

    def test_unparsable_asset_reports_the_real_reason(self):
        """解析不出请求时，**转换器攒下的诊断必须打出来**（修复前被死代码循环吞掉）。"""
        not_curl = _write(
            os.path.join(self.tmp_dir, "not_curl.txt"),
            "wget https://api.example.com/v1/ping",
        )

        code, messages = self._run_cli_with_logs(
            "--from", "curl", "--in", not_curl, "--out", os.path.join(self.tmp_dir, "o")
        )

        self.assertEqual(code, 1)
        joined = "\n".join(messages)
        self.assertIn("不是 curl 命令", joined)
        self.assertIn("没有解析出任何 curl 命令", joined)

    # ---------------------------------------------------------------- M21
    @staticmethod
    def _colliding_postman_collection() -> dict:
        """两个 folder 名归一化后同名：`user admin` 与 `user/admin` → 都是 `col_user_admin.yml`。"""
        return {
            "info": {
                "name": "col",
                "schema": (
                    "https://schema.getpostman.com/json/collection/"
                    "v2.1.0/collection.json"
                ),
            },
            "item": [
                {
                    "name": "user admin",
                    "item": [
                        {
                            "name": "r1",
                            "request": {
                                "method": "GET",
                                "url": "https://api.example.com/users",
                            },
                            "response": [],
                        }
                    ],
                },
                {
                    "name": "user/admin",
                    "item": [
                        {
                            "name": "r2",
                            "request": {
                                "method": "GET",
                                "url": "https://api.example.com/orders",
                            },
                            "response": [],
                        }
                    ],
                },
            ],
        }

    def test_colliding_case_names_are_reported_not_silently_overwritten(self):
        pm_path = _write(
            os.path.join(self.tmp_dir, "collide.postman.json"),
            json.dumps(self._colliding_postman_collection(), ensure_ascii=False),
        )
        out_dir = os.path.join(self.tmp_dir, "out_collide")

        code, messages = self._run_cli_with_logs(
            "--from", "postman", "--in", pm_path, "--out", out_dir, "--name", "col"
        )

        self.assertEqual(code, 1, "撞名必须失败，不能静默只留一个用例")

        joined = "\n".join(messages)
        self.assertIn("同一个文件", joined)
        self.assertIn("user admin", joined)
        self.assertIn("user/admin", joined)
        self.assertIn("--single-file", joined, "要给出可操作的替代做法")

        # 只生成了第一个（没有把两者悄悄合并/覆盖）
        self.assertEqual(
            sorted(
                name
                for name in os.listdir(out_dir)
                if name.endswith(".yml")
            ),
            ["col_user_admin.yml"],
        )

    def test_single_file_workaround_actually_works(self):
        """反向：报错里推荐的 `--single-file` 必须真的能用（否则那条建议就是空话）。"""
        pm_path = _write(
            os.path.join(self.tmp_dir, "collide2.postman.json"),
            json.dumps(self._colliding_postman_collection(), ensure_ascii=False),
        )
        out_dir = os.path.join(self.tmp_dir, "out_single")

        code, _messages = self._run_cli_with_logs(
            "--from",
            "postman",
            "--in",
            pm_path,
            "--out",
            out_dir,
            "--name",
            "col",
            "--single-file",
        )

        self.assertEqual(code, 0)
        yml_files = [name for name in os.listdir(out_dir) if name.endswith(".yml")]
        self.assertEqual(yml_files, ["col.yml"])
        case_text = _read(os.path.join(out_dir, "col.yml"))
        self.assertIn("/users", case_text)
        self.assertIn("/orders", case_text)

    def test_distinct_names_do_not_trigger_the_check(self):
        """反向：名字本来就不同的两个用例照旧各自生成（不能把正常情况判成冲突）。"""
        collection = self._colliding_postman_collection()
        collection["item"][1]["name"] = "user/orders"

        pm_path = _write(
            os.path.join(self.tmp_dir, "distinct.postman.json"),
            json.dumps(collection, ensure_ascii=False),
        )
        out_dir = os.path.join(self.tmp_dir, "out_distinct")

        code, _messages = self._run_cli_with_logs(
            "--from", "postman", "--in", pm_path, "--out", out_dir, "--name", "col"
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(name for name in os.listdir(out_dir) if name.endswith(".yml")),
            ["col_user_admin.yml", "col_user_orders.yml"],
        )


class TestBatch0920CurlFormFieldMetadata(unittest.TestCase):
    r"""0920 批次 3 / **N18**：`-F 'k=@file;type=...'` 的附加参数必须被剥离。

    ## 修复前的现场（`.tmp_audit/verify_sub_f1_f3.py` 实测）

    `_parse_form_field` 的剥离条件是 `";" in remainder and not remainder.startswith("@")`
    —— 也就是**值是文件时恰恰不剥**：

    ```text
    -F 'file=@./photo.jpg;type=image/jpeg'
      -> upload={'file': './photo.jpg;type=image/jpeg'}   ← 路径里带着 ;type=
      -> warnings=[]                                       ← 零告警
    ```

    运行期按这个"路径"去找文件必然失败（用例起不来），而导入期没有任何信号。
    同函数里 `-F 'k=v;type=text/plain'` 却会正常告警 —— 同一种附加参数、两种口径。
    更要紧的是 Postman / Insomnia 的「Copy as cURL」导出的正是
    `--form 'file=@"/path/x.png";type=image/png'`，属**默认会踩**。
    """

    def _step(self, form_arg: str):
        case = curl_adapter.convert_curl_file(
            f"curl -X POST https://api.example.com/upload {form_arg}",
            case_name="form_meta",
            source="t.txt",
        )
        return case.steps[0]

    def test_type_metadata_is_stripped_from_the_upload_path(self):
        step = self._step("-F 'file=@./photo.jpg;type=image/jpeg'")

        self.assertEqual(
            step.upload["file"],
            "./photo.jpg",
            "附加参数被留在了文件路径里（运行期会去找一个不存在的文件）",
        )

    def test_stripping_is_not_silent(self):
        """剥离必须**可见**：告警里要点出被忽略的附加参数与实际采用的路径。"""
        step = self._step("-F 'file=@./photo.jpg;type=image/jpeg'")

        warnings = "\n".join(step.warnings)
        self.assertIn("type=image/jpeg", warnings)
        self.assertIn("./photo.jpg", warnings)

    def test_filename_metadata_is_stripped_too(self):
        step = self._step("-F 'k=@a.txt;type=text/plain;filename=b.txt'")

        self.assertEqual(step.upload["k"], "a.txt")
        self.assertIn("filename=b.txt", "\n".join(step.warnings))

    def test_quoted_file_path_form_is_handled(self):
        """Postman「Copy as cURL」的典型写法：路径带引号。"""
        step = self._step("""-F 'file=@"/tmp/x.png";type=image/png'""")

        self.assertEqual(step.upload["file"], "/tmp/x.png")

    def test_plain_value_metadata_still_warns_as_before(self):
        """反向护栏：非文件字段（`k=v;type=`）的既有行为不变。"""
        step = self._step("-F 'k=v;type=text/plain'")

        self.assertEqual(step.data, {"k": "v"})
        self.assertIn("type=text/plain", "\n".join(step.warnings))

    def test_file_without_metadata_is_unchanged(self):
        """反向护栏：没有附加参数时不许产生新告警。"""
        step = self._step("-F 'file=@./plain.png'")

        self.assertEqual(step.upload["file"], "./plain.png")
        self.assertEqual(
            [w for w in step.warnings if "附加参数" in w], [], step.warnings
        )


class TestBatch0918_8UploadPathNotMasked(unittest.TestCase):
    """0918-8 / M22：`upload` 里放的是**文件路径**，不是凭据 → 不得占位化。

    NOTICE（修复前实测）：`sanitize_case` 对 `step.upload` 也调了 `sanitize_body`，
    于是命中凭据关键词的**字段名**会把路径换成占位符：

        -F 'private_key=@/home/alice/key.pem' → upload: {private_key: ${BODY_PRIVATE_KEY}}

    路径彻底丢失，生成的用例运行期必然失败（会去打开一个"名字等于环境变量值"的文件），
    而文件名不是凭据、占位化没有任何安全收益——本模块 docstring 也一直是这么写的。
    """

    def test_upload_file_path_is_kept_verbatim(self):
        case = curl_adapter.convert_curl_file(
            "curl -X POST https://api.example.com/cert "
            "-F 'private_key=@/home/alice/key.pem' "
            "-F 'cert_file=@/home/alice/cert.pem' "
            "-F 'note=hi'",
            case_name="upload_path",
            source="t.txt",
        )
        step = case.steps[0]

        self.assertEqual(step.upload["private_key"], "/home/alice/key.pem")
        self.assertEqual(step.upload["cert_file"], "/home/alice/cert.pem")
        # 普通字段照旧进 data，且不受影响
        self.assertEqual(step.data["note"], "hi")

        emitted = emit_yaml.dump_case_yaml(case, "upload_path.yml")
        self.assertNotIn("BODY_PRIVATE_KEY", emitted)
        self.assertIn("/home/alice/key.pem", emitted)

    def test_form_field_named_like_a_credential_is_still_masked(self):
        """反向：真正的**表单字段**（不是文件路径）命中凭据名字时仍必须占位化。"""
        case = curl_adapter.convert_curl_file(
            f"curl -X POST https://api.example.com/login "
            f"-H 'Content-Type: application/x-www-form-urlencoded' "
            f"-d 'user=bob&password={CURL_ASSET_TOKEN}'",
            case_name="form_pwd",
            source="t.txt",
        )

        emitted = emit_yaml.dump_case_yaml(case, "form_pwd.yml")

        self.assertNotIn(CURL_ASSET_TOKEN, emitted, "表单里的口令明文入库")


def _har_entry(post_data=None, url="https://api.test/batch/delete", method="POST"):
    request = {"method": method, "url": url}
    if post_data is not None:
        request["headers"] = [
            {"name": "Content-Type", "value": post_data["mimeType"]}
        ]
        request["postData"] = post_data
    return {
        "log": {
            "version": "1.2",
            "entries": [
                {
                    "request": request,
                    "response": {
                        "status": 200,
                        "content": {"mimeType": "application/json", "text": "{}"},
                    },
                }
            ],
        }
    }


class TestBatch0919_2FormDuplicateKeys(unittest.TestCase):
    """0919-2 / 缺陷 8：HAR 表单体的重名字段不得被静默压成一个。

    ## 现场（实测）

    `_apply_post_data` 的 `x-www-form-urlencoded` 分支用**字典推导**建 `data`：

    ```python
    step.data = {str(item.get("name", "")): item.get("value", "") for item in params ...}
    ```

    `a=1&a=2` 于是只留下最后一个值，且 `step.warnings` 是**空的**
    （实测 `data == {'ids': '3'}`、零重名告警）。只有 `text` 形态的分支
    走 `dict(parse_qsl(...))`，同样静默压掉。

    而**同一个文件**里 HAR 的 `queryString` 走的是另一条口径（M33 的正面示范）：
    **告警 + 把原始查询串留在 URL 里**。同一类输入、两种口径 —— 这才是缺陷的形态。

    ## 修法

    统一到 M33 口径：**字典装不下重名，就不拆成字典** —— 告警 + 把**原始表单串**
    留在 `data` 里（`data` 允许是字符串，requests 会原样发出）。
    判重走共用的 `ir.duplicated_names`（三个转换点同一份判据）。
    """

    def test_duplicate_form_fields_keep_the_raw_form_string(self):
        har = _har_entry(
            {
                "mimeType": "application/x-www-form-urlencoded",
                "text": "ids=1&ids=2&ids=3",
                "params": [
                    {"name": "ids", "value": "1"},
                    {"name": "ids", "value": "2"},
                    {"name": "ids", "value": "3"},
                ],
            }
        )
        step = har_adapter.convert_har(har).steps[0]

        self.assertEqual(
            step.data,
            "ids=1&ids=2&ids=3",
            "重名字段被压成了字典 —— 回放会少发字段（批量接口上等于少删/少查）",
        )
        self.assertTrue(
            any("重名" in warning for warning in step.warnings),
            f"重名必须告警（修复前这里是零信号）：{step.warnings}",
        )

    def test_duplicate_fields_in_text_only_body_are_kept_too(self):
        """只有 `text`、没有结构化 `params` 时同样不得静默压掉（`parse_qsl` 也会压）。"""
        har = _har_entry(
            {
                "mimeType": "application/x-www-form-urlencoded",
                "text": "ids=1&ids=2&ids=3",
            }
        )
        step = har_adapter.convert_har(har).steps[0]

        self.assertEqual(step.data, "ids=1&ids=2&ids=3")
        self.assertTrue(any("重名" in warning for warning in step.warnings))

    def test_raw_text_is_preferred_over_reconstruction(self):
        """有原始 `text` 时用它（录到的就是它），不做无谓的重新编码。"""
        har = _har_entry(
            {
                "mimeType": "application/x-www-form-urlencoded",
                "text": "ids=1&ids=2",
                "params": [{"name": "ids", "value": "1"}, {"name": "ids", "value": "2"}],
            }
        )
        step = har_adapter.convert_har(har).steps[0]

        self.assertEqual(step.data, "ids=1&ids=2")

    def test_raw_form_is_reconstructed_when_text_is_absent(self):
        """没有 `text`（只有结构化 params）时按 `(名, 值)` 序列重建，信息同样不丢。"""
        har = _har_entry(
            {
                "mimeType": "application/x-www-form-urlencoded",
                "params": [
                    {"name": "ids", "value": "1"},
                    {"name": "ids", "value": "2"},
                ],
            }
        )
        step = har_adapter.convert_har(har).steps[0]

        self.assertEqual(step.data, "ids=1&ids=2")

    def test_unique_form_fields_still_become_a_dict(self):
        """反向护栏：没有重名时行为**完全不变**（照旧给结构化 dict）。"""
        har = _har_entry(
            {
                "mimeType": "application/x-www-form-urlencoded",
                "text": "foo1=HDnY8&foo2=12.3",
                "params": [
                    {"name": "foo1", "value": "HDnY8"},
                    {"name": "foo2", "value": "12.3"},
                ],
            }
        )
        step = har_adapter.convert_har(har).steps[0]

        self.assertEqual(step.data, {"foo1": "HDnY8", "foo2": "12.3"})
        self.assertFalse(
            [warning for warning in step.warnings if "重名" in warning], step.warnings
        )

    def test_repo_asset_form_body_is_unchanged(self):
        """回归：仓库自带素材里的表单体（无重名）逐字不变。"""
        case = har_adapter.convert_har_file(
            HAR_ASSET, case_name="demo", case_stem="demo"
        )

        self.assertEqual(case.steps[2].data, {"foo1": "HDnY8", "foo2": "12.3"})

    def test_multipart_duplicate_fields_warn(self):
        """multipart 的重复字段：**只做可见化**（结构上无法保留，如实登记）。

        `upload` 是 `Dict`（一个字段名只能对一个文件），IR 表达不了「同名两个文件」，
        重建 multipart 报文体也不是本导入器的职责 —— 所以这里只告警，
        但**不能**继续静默压掉（修复前就是静默的）。
        """
        har = _har_entry(
            {
                "mimeType": "multipart/form-data",
                "params": [
                    {"name": "file", "fileName": "a.bin"},
                    {"name": "file", "fileName": "b.bin"},
                    {"name": "note", "value": "hi"},
                ],
            }
        )
        step = har_adapter.convert_har(har).steps[0]

        self.assertTrue(
            any("重名" in warning for warning in step.warnings), step.warnings
        )
        self.assertEqual(step.upload, {"file": "b.bin"})  # 结构所限，登记而非假装解决

    def test_multipart_unique_fields_do_not_warn(self):
        """反向护栏：multipart 没有重名时不得凭空告警（假报会让维护者无视告警）。"""
        har = _har_entry(
            {
                "mimeType": "multipart/form-data",
                "params": [
                    {"name": "file", "fileName": "a.bin"},
                    {"name": "note", "value": "hi"},
                ],
            }
        )
        step = har_adapter.convert_har(har).steps[0]

        self.assertFalse(
            [warning for warning in step.warnings if "重名" in warning], step.warnings
        )


class TestDuplicatedNamesHelper(unittest.TestCase):
    """`ir.duplicated_names` 是三个转换点共用的判据（收口点），单独钉住它。"""

    def test_reports_each_duplicated_name_once_in_first_seen_order(self):
        pairs = [("b", 1), ("a", 1), ("b", 2), ("c", 1), ("a", 2), ("b", 3)]

        self.assertEqual(duplicated_names(pairs), ["b", "a"])

    def test_unique_pairs_report_nothing(self):
        self.assertEqual(duplicated_names([("a", 1), ("b", 2)]), [])

    def test_empty_input_is_safe(self):
        self.assertEqual(duplicated_names([]), [])


class TestBatch0919_22StringBodySanitize(unittest.TestCase):
    r"""批次 4 / H8 + N1：字符串态请求体必须先判「是不是 JSON」。

    `ir.sanitize_string_body` 是这一层的**唯一入口**（`sanitize_body` 的 `Text` 分支转给它）。
    修复前字符串体一律交给 `sanitize_urlencoded_text` 按 `k=v` 切分，两类输入各错一种（都实测过）：

    ```text
    {"username":"bob","password":"S3cr3t!","token":"abc123"}   值里没有 `=` → 整段原样入库、零告警（H8）
    {"password": "S3cr3t="}                                    值内部被下刀 → 非法 JSON + 前缀明文（N1）
    ```

    本类把入口的三个契约逐条钉住：**结构化脱敏** / **结果仍是字符串** / **没命中就不动一个字节**。
    """

    def _sanitize(self, body):
        return ir.sanitize_string_body(body)

    def test_json_body_without_equals_is_sanitized(self):
        """H8：JSON 体（值里没有 `=`）不再整段原样入库。"""
        cleaned, variables, warnings = self._sanitize(
            '{"username":"bob","password":"S3cr3t!","token":"abc123"}'
        )

        self.assertNotIn("S3cr3t!", cleaned)
        self.assertNotIn("abc123", cleaned)
        self.assertEqual(json.loads(cleaned)["username"], "bob")
        self.assertEqual(
            set(variables), {"BODY_PASSWORD", "BODY_TOKEN"}, "占位变量必须逐字段生成"
        )
        self.assertEqual(len(warnings), 2, "每个被占位化的字段都该有一条告警")

    def test_equals_inside_value_does_not_corrupt_the_body(self):
        """N1：值尾 `=` 不得把体切成非法 JSON，占位符名也不能是从损坏键名派生的垃圾。"""
        cleaned, variables, warnings = self._sanitize('{"password": "S3cr3t="}')

        self.assertEqual(cleaned, '{"password":"${BODY_PASSWORD}"}')
        self.assertEqual(json.loads(cleaned), {"password": "${BODY_PASSWORD}"})
        self.assertEqual(list(variables), ["BODY_PASSWORD"])
        self.assertNotIn("S3cr3t", cleaned, "值的前半段仍然明文可见")
        self.assertEqual(len(warnings), 1)

    def test_placeholder_name_comes_from_the_real_key(self):
        """N1 的现场特征之一：占位符名曾经是 `BODY_<损坏键名>`（如 `BODY_PASSWORD_S3CR3T`）。"""
        for body, expected_var in (
            ('{"password": "S3cr3t="}', "BODY_PASSWORD"),
            ('{"token": "YWJj=="}', "BODY_TOKEN"),
            ('[{"user":"bob","password":"S3cr3t="}]', "BODY_PASSWORD"),
        ):
            with self.subTest(body=body):
                _cleaned, variables, _warnings = self._sanitize(body)
                self.assertEqual(list(variables), [expected_var])

    def test_result_is_still_a_string_so_the_request_is_unchanged(self):
        """**结果必须仍是 `data:` 里的字符串**：改成 `json:` 会让 requests 自己加
        `Content-Type: application/json`，那就是**改请求**（原 curl 可能压根没有这个头）。"""
        cleaned, _variables, _warnings = self._sanitize('{"password": "S3cr3t="}')

        self.assertIsInstance(cleaned, str)

    def test_json_without_secrets_is_not_reformatted(self):
        """反向护栏：没有命中任何凭据时**一个字节都不动**（`{"a": 1}` 不该变成 `{"a":1}`）。"""
        for body in ('{"a": 1}', '{"a":1}', '[1, 2, 3]'):
            with self.subTest(body=body):
                cleaned, variables, warnings = self._sanitize(body)
                self.assertEqual(cleaned, body)
                self.assertEqual(variables, {})
                self.assertEqual(warnings, [])

    def test_form_string_and_plain_text_paths_are_unchanged(self):
        """反向护栏：真表单串 / 普通文本 / 标量 JSON 的既有口径完全不变。"""
        cleaned, variables, _warnings = self._sanitize("user=bob&password=S3cr3t!&page=2")
        self.assertEqual(cleaned, "user=bob&password=${BODY_PASSWORD}&page=2")
        self.assertEqual(list(variables), ["BODY_PASSWORD"])

        for body in ("just plain text", "123", "", None):
            with self.subTest(body=body):
                self.assertEqual(self._sanitize(body)[0], body)

    def test_sanitize_body_dispatches_string_bodies_to_the_json_aware_entry(self):
        """**转交本身**也要钉住：`sanitize_body` 必须把字符串体交给 `sanitize_string_body`。

        NOTICE（注入验证发现的覆盖缺口）：本类只钉 `sanitize_string_body` 自身时，
        把 `sanitize_body` 的 `Text` 分支改回 `sanitize_urlencoded_text`（= **H8/N1 复活**）
        不会让本类任何用例变红 —— 只有端到端那几条会红。而各 adapter 与
        `sanitize_case` 调的**都是 `sanitize_body`**，所以这条转交是承重的，必须单独钉。
        """
        # H8 形态：值里没有 `=`（老路径会整段原样返回）
        h8_body = '{"username":"bob","password":"S3cr3t!","token":"abc123"}'
        cleaned, variables, _warnings = sanitize_body(h8_body)
        self.assertNotIn("S3cr3t!", cleaned)
        self.assertNotIn("abc123", cleaned)
        self.assertEqual(set(variables), {"BODY_PASSWORD", "BODY_TOKEN"})

        # N1 形态：值尾 `=`（老路径会把体切坏）
        n1_body = '{"password": "S3cr3t="}'
        cleaned, variables, _warnings = sanitize_body(n1_body)
        self.assertEqual(cleaned, '{"password":"${BODY_PASSWORD}"}')
        self.assertEqual(json.loads(cleaned), {"password": "${BODY_PASSWORD}"})
        self.assertEqual(list(variables), ["BODY_PASSWORD"])

    def test_nested_string_value_is_not_guessed(self):
        """**边界（有意）**：转换层只看**字段名**，不看字符串**值**的内部。

        `{"outer": "password=abc"}` 里的凭据**不会**被占位化 —— 这不是漏网，
        而是 `sanitize_body` docstring 写死的口径：「只按**字段名**判定，不做值猜谜；
        误伤业务字段比漏掉一个字段更麻烦」。包装一层字符串就能让任意值被改写，
        等于把这条口径推翻。

        NOTICE（层次差异，别误判成 bug）：**runtime 展示层更激进** ——
        `utils._mask_sensitive_value` 会对**任何**字符串套用 `_mask_query_pairs`。
        两层目标不同，所以松紧不同：

        | 层 | 作用对象 | 误伤的代价 |
        | --- | --- | --- |
        | 转换层（改生成物） | 只按字段名 | 生成的用例**跑不起来**（值被换成占位符） |
        | 展示层（只改日志副本） | 字符串值也看 | 日志里少看到一个值，**无害** |

        这个不对称已登记（见 `新发现缺陷记录0919.md` 的 L20 家族说明）。
        """
        cleaned, variables, warnings = sanitize_body({"outer": '{"password": "S3cr3t="}'})

        self.assertEqual(cleaned, {"outer": '{"password": "S3cr3t="}'})
        self.assertEqual(variables, {})
        self.assertEqual(warnings, [])

    def test_nested_string_value_under_a_secret_key_is_fully_replaced(self):
        """反向：**键名**敏感时整块值照样被替换（字符串值也不例外）。"""
        cleaned, variables, _warnings = sanitize_body(
            {"password_blob": '{"password": "S3cr3t="}'}
        )

        self.assertEqual(cleaned, {"password_blob": "${BODY_PASSWORD_BLOB}"})
        self.assertEqual(list(variables), ["BODY_PASSWORD_BLOB"])


class TestBatchA_ContainerVariableIsSanitized(unittest.TestCase):
    """批次 A / **H4**：用例级变量的**容器值**内部也要按名脱敏。

    ## 修复前的现场（实测，真 `hconvert`：`Postman variable[].value` 是对象）

    ```text
    Postman:   variable = [{key: "loginPayload",
                            value: {"username": "bob", "password": "S3cr3tC0ntainer"}}]
    生成 YAML: variables: {loginPayload: {username: bob, password: S3cr3tC0ntainer}}   ← 明文入库
    同一份字典当**请求体**时: {"username": "bob", "password": "${BODY_PASSWORD}"}      ← 正确占位
    ```

    根因：`sanitize_case` 的用例级变量循环只按**变量名**判定（`is_secret_name(name)`）、
    只处理标量，容器值既**不递归也无告警**；而 `sanitize_body` 早就实现了
    「按键名递归 dict/list」——所以这是**漏用既有机制**，不是机制做不到
    （不属 L20「转换层不看字符串值内部」那条有意的边界：这里字段名就叫 `password`）。
    """

    def _sanitize_case(self, variables):
        case = ir.IRCase(name="batchA / 容器变量脱敏", source="unittest")
        case.variables = dict(variables)
        return ir.sanitize_case(case)

    def test_container_value_gets_placeholder_inside(self):
        case = self._sanitize_case(
            {
                "loginPayload": {
                    "username": "bob",
                    "password": "S3cr3tC0ntainer",
                }
            }
        )

        payload = case.variables["loginPayload"]
        self.assertEqual(payload["username"], "bob", "非凭据字段不该被动")
        self.assertNotIn(
            "S3cr3tC0ntainer",
            json.dumps(case.variables),
            f"容器值里的明文口令仍在（H4）：{case.variables}",
        )
        self.assertEqual(payload["password"], "${BODY_PASSWORD}")
        # 占位变量本身要登记进 variables（否则生成物引用一个未定义变量）
        self.assertEqual(case.variables["BODY_PASSWORD"], "${ENV(BODY_PASSWORD)}")

    def test_list_container_value_is_sanitized_too(self):
        """列表形态（批量接口常见）同样要覆盖。"""
        case = self._sanitize_case(
            {
                "accounts": [
                    {"user": "u1", "token": "T-1"},
                    {"user": "u2", "token": "T-2"},
                ]
            }
        )

        rendered = json.dumps(case.variables)
        self.assertNotIn("T-1", rendered)
        self.assertNotIn("T-2", rendered)
        self.assertEqual(case.variables["accounts"][0]["token"], "${BODY_TOKEN}")
        self.assertEqual(case.variables["accounts"][1]["token"], "${BODY_TOKEN}")

    def test_scalar_secret_variable_still_uses_the_name_rule(self):
        """反向护栏：**标量**凭据变量仍走「按变量名换成 ENV 引用」的老口径，一个字不变。"""
        case = self._sanitize_case({"apiPassword": "plain-secret"})

        self.assertEqual(case.variables["apiPassword"], "${ENV(apiPassword)}")

    def test_harmless_container_is_untouched(self):
        """反向护栏：没有任何凭据字段的容器值**逐字不动**（不制造假报）。"""
        original = {"config": {"host": "h", "port": 8080}, "tags": ["a", "b"]}
        case = self._sanitize_case({"payload": json.loads(json.dumps(original))})

        self.assertEqual(case.variables["payload"], original)

    def test_warning_names_the_variable_and_field(self):
        """告警必须点名「哪个变量的哪个字段」，而不是笼统的「请求体字段」。"""
        case = self._sanitize_case({"loginPayload": {"password": "S3cr3tC0ntainer"}})

        warnings = "\n".join(case.all_warnings())
        self.assertIn("loginPayload", warnings)
        self.assertIn("loginPayload.password", warnings)


class TestBatch0919_7ReservedDeviceNamesAreNotReachable(unittest.TestCase):
    """0919-2 / 缺陷 10：`_slug` 与 Windows 保留设备名 —— **实测推翻了报告的说法**。

    ## 报告的说法

    > Postman folder / OpenAPI tag 名恰为 `CON`/`NUL`/`COM1` 时生成 `con.yml`——
    > Windows 上 `open()` 落到设备而非文件，CLI 的同名冲突检测救不了单用例场景。

    ## 实测结论：**在本平台不成立**

    同一目录下三个 API 独立验证（`open()` / PowerShell `Set-Content` / `cmd echo >`）：

    | 名字 | 结果 |
    |---|---|
    | `CON.yml` / `NUL.yml` / `COM1.yml` / `LPT1.yml` | **普通文件**：写入成功、能读回内容、出现在目录列表里 |
    | 裸 `NUL`（不带扩展名） | **设备**：写入被静默丢弃、`isfile=False`、目录里不出现 |

    「裸 `NUL` 会落进设备」这一条正是本探针的**阳性对照** ——
    它证明设备重定向在本机确实能被观测到，所以「`CON.yml` 是普通文件」不是探针失灵。

    而本仓库生成的文件名**永远带 `.yml`**（`f"{slugify(name)}.yml"`），
    所以报告描述的那条路径**走不到**。故本批**不改代码**，只用一条行为用例把这个结论钉住
    （而不是加一段无法触发、也无法验证的防御代码）。

    本用例是**行为级**的：它不看文件系统的设备语义，只看「写进去的东西能不能原样读回来」。
    哪天真有人把命名改成不带扩展名、或某平台又开始重定向，它会红。
    """

    RESERVED = ("CON", "con", "NUL", "nul", "COM1", "LPT1", "AUX", "PRN")

    def setUp(self):
        self.tmp_dir = _tmp_dir("tmp_slug_device")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _emit_named(self, name):
        ir_case = IRCase(
            name=name,
            steps=[IRStep(method="GET", url="/get", name=name)],
        )
        return emit_yaml.emit_case(ir_case, self.tmp_dir)

    def test_slug_keeps_the_name_but_the_file_always_has_an_extension(self):
        """`_slug` 确实原样保留 `CON`，但落盘名是 `CON.yml` —— 这是结论的关键一步。"""
        for name in self.RESERVED:
            with self.subTest(name=name):
                self.assertEqual(emit_yaml.slugify(name), name)
                self.assertEqual(
                    f"{emit_yaml.slugify(name)}.yml", f"{name}.yml"
                )

    def test_reserved_device_name_lands_in_a_real_readable_file(self):
        """**核心**：用例名叫 `CON` 时，产物必须是能读回内容的真文件。"""
        for name in self.RESERVED:
            with self.subTest(name=name):
                written = self._emit_named(name)
                yaml_path = written["yaml"]

                self.assertEqual(os.path.basename(yaml_path), f"{name}.yml")
                self.assertTrue(
                    os.path.isfile(yaml_path),
                    f"{yaml_path} 不是普通文件——可能落到了设备上（缺陷 10 的场景）",
                )
                with open(yaml_path, encoding="utf-8") as fp:
                    content = fp.read()
                self.assertIn("/get", content, "写进去的内容读不回来")

    def test_every_generated_artifact_path_keeps_an_extension(self):
        """收口：**所有**产物路径（用例 + schema）都必须带扩展名，才谈得上「走不到设备」。"""
        written = self._emit_named("CON")

        paths = [written["yaml"], *written["extra"].values()]
        for path in paths:
            with self.subTest(path=os.path.basename(path)):
                self.assertNotEqual(
                    os.path.splitext(os.path.basename(path))[1],
                    "",
                    f"{path} 没有扩展名——裸名字才可能撞上 Windows 保留设备名",
                )


def _har_of(*entries):
    return {
        "log": {"version": "1.2", "creator": {"name": "t"}, "entries": list(entries)}
    }


def _har_raw_entry(
    status=200,
    *,
    url="https://api.test/thing",
    method="GET",
    headers=None,
    cookies=None,
    query_string=None,
    post_data=None,
    content=None,
):
    request = {"method": method, "url": url}
    if headers is not None:
        request["headers"] = headers
    if cookies is not None:
        request["cookies"] = cookies
    if query_string is not None:
        request["queryString"] = query_string
    if post_data is not None:
        request["postData"] = post_data
    response = {"status": status}
    response["content"] = (
        content
        if content is not None
        else {"mimeType": "application/json", "text": '{"ok": true}'}
    )
    return {"request": request, "response": response}


class TestBatch0919_26HarBadResponseData(unittest.TestCase):
    """批次 8 / **L5**：HAR 的 `response.status` 坏数据**不能杀掉整批导入**。

    ## 现场（实测，修复前）

    判据是 `if int(response.get("status") or 0) == 0:`（`_should_skip_entry`）
    与 `status = int(response.get("status") or 0)`（断言段）。手改/第三方工具的 HAR
    可能写 `"200 OK"`，于是：

    ```text
    ERROR | 解析 <文件> 失败：ValueError: invalid literal for int() with base 10: '200 OK'
    退出码 2；--out 目录**根本没有被创建**（一个用例都不产出）
    ```

    实测（`.tmp_report/probe_l5.py`）同一份 HAR 里**完好的条目也一起丢了**。
    Postman 侧同类输入早已是「告警 + 跳过坏条目」，两侧口径不一致。

    ## 修法（对齐 Postman 口径）

    - `status` 是数字/数字字符串 → 照旧生成 `eq: [status_code, N]`；
    - `status` **不是可用状态码**（`"200 OK"` / `abc` / `200.7` / `True` / 负数）→
      **条目保留**、不生成状态码断言、**逐条告警**（修复前 `int()` 会把 `200.7`→`200`、
      `True`→`1` 静默截断）；
    - `--assertions status` 下该步会一条断言都没有 → 再补一条**结果级**告警
      （回放时这种 step 会被判 success，是最贵的假绿；与 0919-17 的 Postman 口径一致）。
    """

    def _steps(self, *entries, **kwargs):
        case = har_adapter.convert_har(_har_of(*entries), **kwargs)
        return case.steps, case

    def test_non_numeric_status_does_not_kill_the_batch(self):
        """**核心**：坏 status 不再带走整份 HAR（修复前 exit 2 且 --out 不产出）。"""
        steps, _case = self._steps(
            _har_raw_entry("200 OK", url="https://api.test/bad"),
            _har_raw_entry(200, url="https://api.test/good"),
        )

        self.assertEqual(len(steps), 2, "坏条目把好条目一起带走了")
        self.assertEqual(steps[1].assertions[0], {"eq": ["status_code", 200]})

    def test_unusable_status_generates_no_status_assertion(self):
        steps, _case = self._steps(_har_raw_entry("200 OK"))
        assertions = json.dumps(steps[0].assertions)

        self.assertNotIn("status_code", assertions)
        self.assertIn("jsonschema_match", assertions, "形状断言不该被牵连丢掉")
        self.assertTrue(
            any("不是数字" in w and "200 OK" in w for w in steps[0].warnings),
            f"坏 status 必须点名告警：{steps[0].warnings}",
        )

    def test_usable_status_forms_keep_the_assertion(self):
        """反向护栏：数字、数字字符串、带空格、整数值浮点照旧可用。"""
        for status in (200, "200", " 200 ", 200.0):
            with self.subTest(status=status):
                steps, _case = self._steps(_har_raw_entry(status))

                self.assertIn({"eq": ["status_code", 200]}, steps[0].assertions)

    def test_unusable_status_forms_warn_and_skip_the_assertion(self):
        """`200.7` / `True` / 负数在修复前被 `int()` **静默截断**成 200 / 1 / -1。"""
        for status in ("200 OK", "abc", 200.7, True, -1):
            with self.subTest(status=status):
                steps, _case = self._steps(_har_raw_entry(status))

                self.assertNotIn(
                    "status_code",
                    json.dumps(steps[0].assertions),
                    f"status={status!r} 不该生成状态码断言",
                )
                self.assertTrue(
                    any("response.status" in w for w in steps[0].warnings),
                    f"status={status!r} 必须告警：{steps[0].warnings}",
                )

    def test_status_mode_without_any_assertion_warns_loudly(self):
        """`--assertions status` + 坏 status → 该步零断言，必须有一条结果级告警。"""
        steps, _case = self._steps(_har_raw_entry("200 OK"), assertion_mode="status")

        self.assertEqual(steps[0].assertions, [])
        self.assertTrue(
            any("没有任何断言" in w for w in steps[0].warnings),
            f"零断言必须可见（否则回放时假绿）：{steps[0].warnings}",
        )

    def test_aborted_requests_are_still_filtered(self):
        """反向护栏：`status: 0`（被中止）的既有口径不变——跳过该条目。"""
        for status in (0, "0"):
            with self.subTest(status=status):
                steps, case = self._steps(_har_raw_entry(status))

                self.assertEqual(steps, [])
                self.assertTrue(
                    any("被中止" in w for w in case.all_warnings()),
                    case.all_warnings(),
                )

    def test_missing_status_is_not_reported_as_aborted(self):
        """文案订正：**没有** status 与 **status=0** 是两回事（同 L7 的归因口径）。"""
        steps, case = self._steps(_har_raw_entry(None))
        warnings = "\n".join(case.all_warnings())

        self.assertEqual(steps, [])
        self.assertIn("response.status", warnings)


class TestBatch0919_26HarBadEntryShapes(unittest.TestCase):
    """批次 8 / **L21**（与 L5 同族根因）：条目/字段**形态不对**同样能杀掉整批。

    修复前 `.tmp_report/probe_l5b.py` 实测的崩溃清单（**同一份 HAR 里完好的条目一起丢**）：

    | 输入形态 | 修复前 |
    | --- | --- |
    | `entry` 是 str / None / int | `AttributeError: '…' object has no attribute 'get'` |
    | `entry.request` 是 str | 同上 |
    | `entry.response` 是 str | 同上 |
    | `response.content` 是 str | 同上 |
    | `response.content.text` 是 int | `AttributeError: 'int' object has no attribute 'lstrip'` |
    | `request.headers` 不是列表 / 含非对象项 | `AttributeError` |
    | `request.queryString` 含非对象项 | `AttributeError` |

    现在的口径按**粒度**分两层（这也是本类两条用例的分界）：

    - **对象级**（`entry` / `request` / `response` 不是对象，或 `request.url` 为空）
      → 跳过**该条目** + 逐条告警，其余条目照常导入；
    - **字段级**（`headers` / `cookies` / `queryString` / `postData` / `content` 里的坏数据）
      → 告警 + 跳过**那个字段**，请求本身照常导入。

    顺带订正了两处「静默坏值」：`request` 为 `None` 时修复前会**静默**生成一个
    `url: /` 的空步骤（零告警），现在按对象级坏数据跳过并告警。
    """

    def _steps(self, *entries, **kwargs):
        case = har_adapter.convert_har(_har_of(*entries), **kwargs)
        return case.steps, case

    def test_object_level_shapes_are_skipped_with_a_warning(self):
        """**核心**：坏条目被跳过、好条目照常导入，且告警点名了类型。"""
        good = _har_raw_entry(200, url="https://api.test/good")
        for bad in ("oops", None, 7, {"request": "oops"}, {"request": None}, {"request": {"url": "https://a/x"}, "response": "oops"}):
            with self.subTest(bad=bad):
                steps, case = self._steps(bad, good)
                warnings = "\n".join(case.all_warnings())

                self.assertEqual(len(steps), 1, f"坏条目带走了好条目：{bad!r}")
                self.assertEqual(steps[0].url, "/good")
                self.assertIn("条目形态不对", warnings)
                self.assertIn("entry #1", warnings)

    def test_entry_without_a_url_is_skipped_instead_of_silently_imported(self):
        """反向护栏（修复前的静默坏值）：没有 url 的条目不得生成 `url: /` 的空步骤。"""
        steps, case = self._steps(
            {"request": {"method": "GET"}, "response": {"status": 200}},
            _har_raw_entry(200, url="https://api.test/good"),
        )

        self.assertEqual(len(steps), 1)
        self.assertIn("request.url", "\n".join(case.all_warnings()))

    def test_field_level_shapes_keep_the_request(self):
        """字段级坏数据只丢那个字段：请求照常导入 + 告警。"""
        cases = [
            ({"headers": "oops"}, "`headers` 不是列表"),
            ({"headers": ["oops"]}, "headers[0] 不是对象"),
            ({"query_string": ["oops"]}, "queryString[0] 不是对象"),
            ({"cookies": ["oops"]}, "cookies[0] 不是对象"),
            ({"post_data": "oops"}, "`request.postData` 不是对象"),
            ({"content": "oops"}, "`content` 不是对象"),
            ({"content": {"text": 123}}, "`content.text` 不是字符串"),
            (
                {"post_data": {"mimeType": "application/json", "params": ["oops"]}},
                "postData.params",
            ),
        ]
        for overrides, fragment in cases:
            with self.subTest(fragment=fragment):
                entry = _har_raw_entry(**overrides)
                steps, case = self._steps(entry)
                warnings = "\n".join(case.all_warnings()) + "\n".join(
                    w for step in steps for w in step.warnings
                )

                self.assertEqual(len(steps), 1, f"字段级坏数据不该丢整条：{overrides!r}")
                self.assertIn(fragment, warnings)

    def test_healthy_har_produces_no_shape_warnings(self):
        """特异性对照：正常素材不得出现这两类告警（假报会让真报被无视）。"""
        steps, case = self._steps(_har_raw_entry(200))
        warnings = "\n".join(case.all_warnings()) + "\n".join(steps[0].warnings)

        for fragment in ("条目形态不对", "不是对象", "形态不对（"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, warnings)

    def test_repo_asset_still_imports(self):
        """回归：仓库自带 HAR 素材的导入结果不受形态检查影响。"""
        case = har_adapter.convert_har_file(HAR_ASSET, case_name="demo")

        self.assertTrue(case.steps, "仓库素材应当导入出步骤")
        self.assertFalse(
            [w for w in case.all_warnings() if "条目形态不对" in w], case.all_warnings()
        )


class TestBatch91SchemaTruncationIsVisible(unittest.TestCase):
    r"""批次 9-1：**schema 推断的截断必须可见**（HAR 与 OpenAPI 两侧）。

    截断出来的空 schema（`{}`）在 JSON Schema 里等价于「**任意值**」——
    于是"断过了"的那一层实际上什么都没断，这是最难查的一类**假通过**。
    修复前三条截断路径里只有一条（OpenAPI 的属性数上限）会告警：

    | 路径 | 修复前 |
    | --- | --- |
    | OpenAPI `build_assertion_schema`：嵌套 > `MAX_SCHEMA_DEPTH(8)` | **静默 return {}** |
    | OpenAPI `build_assertion_schema`：`$ref` 解析后不是 dict | **静默 return {}** |
    | HAR `infer_schema`：嵌套 >= `MAX_SCHEMA_DEPTH(6)` | **静默 return {}** |
    | HAR `infer_schema`：属性数 >= `MAX_SCHEMA_PROPERTIES(100)` | **静默 break** |
    | OpenAPI：属性数 > `MAX_PROPERTIES(60)` | ✅ 本来就告警（本次对齐到它） |
    """

    def test_har_depth_truncation_warns(self):
        from interfacetester.converters.har_adapter import (
            MAX_SCHEMA_DEPTH,
            infer_schema,
        )

        sample: dict = {"leaf": 1}
        for level in range(MAX_SCHEMA_DEPTH + 3):
            sample = {f"l{level}": sample}

        warnings: list = []
        infer_schema(sample, warnings=warnings)

        self.assertTrue(
            any("截断" in w for w in warnings),
            f"深层 schema 的截断必须告警（否则断言静默变宽松）：{warnings}",
        )
        # 去重：递归会命中同一个分支，不能一层刷一条
        self.assertEqual(len(warnings), len(set(warnings)))

    def test_har_property_truncation_warns(self):
        from interfacetester.converters.har_adapter import (
            MAX_SCHEMA_PROPERTIES,
            infer_schema,
        )

        sample = {f"k{index}": index for index in range(MAX_SCHEMA_PROPERTIES + 5)}

        warnings: list = []
        schema = infer_schema(sample, warnings=warnings)

        self.assertEqual(len(schema["properties"]), MAX_SCHEMA_PROPERTIES)
        self.assertTrue(
            any("属性过多" in w for w in warnings), f"属性截断必须告警：{warnings}"
        )

    def test_har_shallow_schema_stays_silent(self):
        """反向护栏：没截断就不许告警（假报会让真报被无视）。"""
        from interfacetester.converters.har_adapter import infer_schema

        warnings: list = []
        infer_schema({"id": 1, "tags": ["a"], "meta": {"ok": True}}, warnings=warnings)

        self.assertEqual(warnings, [])

    def test_openapi_depth_truncation_warns(self):
        from interfacetester.converters.openapi_adapter import (
            MAX_SCHEMA_DEPTH,
            build_assertion_schema,
        )

        schema: dict = {"type": "object"}
        node = schema
        for _level in range(MAX_SCHEMA_DEPTH + 3):
            node["properties"] = {"next": {"type": "object"}}
            node = node["properties"]["next"]

        warnings: list = []
        build_assertion_schema(schema, {}, warnings)

        self.assertTrue(
            any("截断" in w for w in warnings), f"深层契约的截断必须告警：{warnings}"
        )
        self.assertEqual(len(warnings), len(set(warnings)))

    def test_openapi_non_dict_schema_warns(self):
        """`schema: true`（JSON Schema 的布尔型）修复前静默产出「无断言」。"""
        from interfacetester.converters.openapi_adapter import build_assertion_schema

        warnings: list = []
        built = build_assertion_schema(True, {}, warnings)

        self.assertEqual(built, {})
        self.assertTrue(
            any("不是对象" in w for w in warnings), f"形态不可转换必须告警：{warnings}"
        )

    def test_openapi_plain_schema_stays_silent(self):
        from interfacetester.converters.openapi_adapter import build_assertion_schema

        warnings: list = []
        built = build_assertion_schema(
            {"type": "object", "properties": {"id": {"type": "integer"}}}, {}, warnings
        )

        self.assertEqual(warnings, [])
        self.assertEqual(built["properties"]["id"]["type"], "integer")


# ---------------------------------------------------------------------------
# 批次 E / L1 + L2 + L3 + L4 + L5：三个导入器的**形态护栏**与两条内部缺陷
#
# 修复前：素材里字段形态与规范不符时抛**裸** AttributeError / KeyError / RecursionError，
# 用户看到的是一屏 traceback，看不出是哪个字段；OpenAPI 的 `parameters` 写成对象时更糟 ——
# 它会**静默丢掉**全部参数（包括 `required: true` 的查询参数）。
# 修复后的实测输出：`.tmp_audit/out_batchE_shapes.txt`（可复跑 `.tmp_audit/probe_batchE_shapes.py`）。
# ---------------------------------------------------------------------------
def _postman_collection(request, *, name="col", extra_item=None, items=None):
    item = {"name": "step1", "request": request}
    if extra_item:
        item.update(extra_item)
    return {
        "info": {
            "name": name,
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/",
        },
        "item": items if items is not None else [item],
    }


def _shape_warnings(warnings) -> list:
    """只挑出「形态护栏」那几条告警（其它业务告警不参与断言）。"""
    return [
        w
        for w in warnings
        if "非对象条目" in w or "写成了对象" in w or "形态不对" in w or "应该是" in w
    ]


class TestBatchEConverterShapeGuards(unittest.TestCase):
    """批次 E / L1：Postman / HAR / OpenAPI 的形态护栏统一到 `ir.safe_dict_entries`。"""

    # ------------------------------------------------------------ Postman
    def test_postman_non_object_entries_are_skipped_with_a_warning(self):
        """条目不是对象（字符串/数字）→ 跳过该项并告警，**其余字段照常导入**。"""
        for field, request in (
            (
                "url.query",
                {"method": "GET", "url": {"raw": "https://h/x", "query": ["a=1"]}},
            ),
            (
                "request.header",
                {
                    "method": "GET",
                    "url": "https://h/x",
                    "header": [{"key": "A", "value": "1"}, "X: 1"],
                },
            ),
            (
                "body.urlencoded",
                {
                    "method": "POST",
                    "url": "https://h/x",
                    "body": {"mode": "urlencoded", "urlencoded": ["a=1"]},
                },
            ),
        ):
            with self.subTest(field=field):
                case = postman_adapter.convert_postman(
                    _postman_collection(request), case_name="t"
                )[0]

                text = "\n".join(case.all_warnings())
                self.assertIn(field, text, f"必须点名是哪个字段：{text}")
                self.assertIn("非对象条目", text)

    def test_postman_object_instead_of_list_explains_the_missing_dash(self):
        """漏了 `-` 时要把「列表写法」直接写进告警（这是最常见的根因）。"""
        for field, request in (
            (
                "url.variable",
                {
                    "method": "GET",
                    "url": {
                        "raw": "https://h/u/:id",
                        "variable": {"key": "id", "value": "7"},
                        "path": ["u", ":id"],
                        "host": ["h"],
                    },
                },
            ),
            (
                "body.formdata",
                {
                    "method": "POST",
                    "url": "https://h/x",
                    "body": {
                        "mode": "formdata",
                        "formdata": {"key": "a", "value": "1"},
                    },
                },
            ),
        ):
            with self.subTest(field=field):
                case = postman_adapter.convert_postman(
                    _postman_collection(request), case_name="t"
                )[0]

                text = "\n".join(case.all_warnings())
                self.assertIn(field, text)
                self.assertIn("写成了对象", text)
                self.assertIn("漏了列表符号", text, "必须给出根因与正确写法")

    def test_postman_malformed_event_and_body_options_do_not_crash(self):
        """`item.event` 写成对象、`body.options` 是字符串（修复前两处都抛裸 AttributeError）。"""
        case = postman_adapter.convert_postman(
            _postman_collection(
                {
                    "method": "POST",
                    "url": "https://h/x",
                    "body": {"mode": "raw", "raw": "{}", "options": "not-an-object"},
                },
                extra_item={
                    "event": {"listen": "test", "script": {"exec": ["pm.test('x')"]}}
                },
            ),
            case_name="t",
        )[0]

        text = "\n".join(case.all_warnings())
        self.assertIn("item.event", text)
        self.assertEqual(len(case.steps), 1, "坏 event 不该影响请求本身")

    def test_postman_well_formed_request_has_no_shape_warnings(self):
        """反向护栏：形态正确的请求**一条形态告警都不能有**（不许假报）。"""
        case = postman_adapter.convert_postman(
            _postman_collection(
                {
                    "method": "POST",
                    "url": {
                        "raw": "https://h/u/7?q=1",
                        "protocol": "https",
                        "host": ["h"],
                        "path": ["u", ":id"],
                        "variable": [{"key": "id", "value": "7"}],
                        "query": [{"key": "q", "value": "1"}],
                    },
                    "header": [{"key": "A", "value": "1"}],
                    "body": {
                        "mode": "urlencoded",
                        "urlencoded": [{"key": "a", "value": "1"}],
                    },
                },
                extra_item={
                    "event": [{"listen": "test", "script": {"exec": ["pm.test('x')"]}}]
                },
            ),
            case_name="t",
        )[0]

        self.assertEqual(_shape_warnings(case.all_warnings()), [])

    # ------------------------------------------------------------ HAR
    def test_har_root_and_log_shapes_are_named(self):
        """HAR 文档根 / `log` / `log.entries` 三处形态错误各给一条点名告警（修复前裸崩）。"""
        for label, har, expected in (
            ("根是列表", [], "文档根"),
            ("log 是字符串", {"log": "1.2"}, "`log`"),
            (
                "entries 是对象",
                {"log": {"version": "1.2", "entries": {"a": 1}}},
                "log.entries",
            ),
        ):
            with self.subTest(label=label):
                case = har_adapter.convert_har(har, case_name="t")

                text = "\n".join(case.all_warnings())
                self.assertIn(expected, text, text)
                self.assertEqual(case.steps, [], "坏 HAR 不该产出步骤")

    # ------------------------------------------------------------ OpenAPI
    def test_openapi_top_level_shapes_are_named(self):
        """`info` 写成字符串 / `paths` 写成列表（修复前裸 AttributeError）。"""
        from interfacetester.converters.openapi_adapter import convert_openapi

        base = {
            "openapi": "3.0.0",
            "info": {"title": "t"},
            "servers": [{"url": "https://api.test"}],
            "paths": {"/x": {"get": {"responses": {"200": {"description": "ok"}}}}},
        }

        doc_info = dict(base, info="t")
        case = convert_openapi(doc_info, case_name="t")[0]
        self.assertIn("`info`", "\n".join(case.all_warnings()))

        doc_paths = dict(base, paths=["/x"])
        case = convert_openapi(doc_paths, case_name="t")[0]
        text = "\n".join(case.all_warnings())
        self.assertIn("`paths`", text)
        self.assertEqual(case.steps, [])

    def test_openapi_object_instead_of_list_is_named(self):
        """`servers` / `security` 漏了 `-`（修复前裸 `KeyError: 0`）。"""
        from interfacetester.converters.openapi_adapter import convert_openapi

        base = {
            "openapi": "3.0.0",
            "info": {"title": "t"},
            "paths": {"/x": {"get": {"responses": {"200": {"description": "ok"}}}}},
        }

        case = convert_openapi(
            dict(base, servers={"url": "https://api.test"}), case_name="t"
        )[0]
        text = "\n".join(case.all_warnings())
        self.assertIn("`servers`", text)
        self.assertIn("漏了列表符号", text)

        case = convert_openapi(
            dict(
                base,
                servers=[{"url": "https://api.test"}],
                security={"bearerAuth": []},
                components={
                    "securitySchemes": {
                        "bearerAuth": {"type": "http", "scheme": "bearer"}
                    }
                },
            ),
            case_name="t",
        )[0]
        text = "\n".join(case.all_warnings())
        self.assertIn("security", text)
        self.assertNotIn(
            "Authorization", case.steps[0].headers or {}, "不该凭空生成认证头"
        )

        # 另一形态：`security` 是**字符串列表**（`security: [bearerAuth]`，漏了每项的 `{}`）。
        # 修复前这里会把 `"bearerAuth"` 当**可迭代对象逐字符**遍历 → 认证头一个都不生成，
        # 而且**零告警**（「静默无认证」比崩掉更危险）。
        case = convert_openapi(
            dict(
                base,
                servers=[{"url": "https://api.test"}],
                security=["bearerAuth"],
                components={
                    "securitySchemes": {
                        "bearerAuth": {"type": "http", "scheme": "bearer"}
                    }
                },
            ),
            case_name="t",
        )[0]
        text = "\n".join(case.all_warnings())
        self.assertIn("security[0]", text, f"必须点名 security[0]：{text}")
        self.assertNotIn("Authorization", case.steps[0].headers or {})

    def test_openapi_parameters_as_object_is_not_silently_dropped(self):
        """**L2 的核心**：`parameters` 写成对象时，修复前**静默丢掉**全部参数（含 required）。"""
        from interfacetester.converters.openapi_adapter import convert_openapi

        document = {
            "openapi": "3.0.0",
            "info": {"title": "t"},
            "servers": [{"url": "https://api.test"}],
            "paths": {
                "/x": {
                    "get": {
                        "parameters": {"name": "q", "in": "query", "required": True},
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }

        case = convert_openapi(document, case_name="t")[0]

        text = "\n".join(case.all_warnings())
        self.assertIn("parameters", text)
        self.assertIn("写成了对象", text)

    def test_openapi_all_of_self_reference_does_not_recurse_forever(self):
        """L4：`allOf` 自引用（修复前 `RecursionError`，普通 `$ref` 循环有深度兜底）。"""
        from interfacetester.converters.openapi_adapter import build_assertion_schema

        document = {
            "components": {
                "schemas": {
                    "Self": {
                        "allOf": [
                            {"$ref": "#/components/schemas/Self"},
                            {"type": "object", "properties": {"a": {"type": "string"}}},
                        ]
                    }
                }
            }
        }
        warnings: list = []

        built = build_assertion_schema(
            document["components"]["schemas"]["Self"], document, warnings
        )

        self.assertEqual(built.get("properties"), {"a": {"type": "string"}})
        self.assertTrue(
            any("allOf" in w and "自引用" in w for w in warnings),
            f"自引用必须告警（否则断言会比契约宽松）：{warnings}",
        )

    def test_openapi_sample_warnings_always_reach_the_caller_list(self):
        """L3：告警**是否出现不能取决于该步骤此前有没有别的告警**（`warnings or []` 的坑）。

        修复前：空列表是 falsy → 被换成临时列表 → 传空列表的调用方**收不到**任何告警；
        而传一个已经有一条告警的列表时反而能收到。
        """
        from interfacetester.converters.openapi_adapter import sample_from_schema

        empty: list = []
        sample_from_schema({"$ref": "#/components/schemas/Missing"}, {}, "", 0, empty)
        non_empty = ["已有告警"]
        sample_from_schema(
            {"$ref": "#/components/schemas/Missing"}, {}, "", 0, non_empty
        )

        self.assertEqual(len(empty), 1, f"空列表也必须收到告警：{empty}")
        self.assertEqual(len(non_empty), 2, f"非空列表要追加而不是丢弃：{non_empty}")
        self.assertIn("$ref", empty[0])

        # `warnings` 的默认值是 None（合法用法）→ 必须能跑，而不是在下游 `.append` 上崩。
        # NOTICE: 这里刻意用**会告警**的 schema（`$ref` 找不到）—— 只有走到 `_warn`
        # 才会碰 `warnings.append`，用普通 schema 根本覆盖不到这一行。
        sample_from_schema({"$ref": "#/components/schemas/Missing"}, {}, "q")

        # `allOf` 分支同样要让告警落到调用方的列表里（同一处 falsy 判据的第二处）
        all_of: list = []
        sample_from_schema(
            {"allOf": [{"$ref": "#/components/schemas/Missing"}]}, {}, "", 0, all_of
        )
        self.assertEqual(
            len(all_of), 1, f"allOf 分支的告警也必须到得了调用方：{all_of}"
        )

    def test_openapi_well_formed_document_has_no_shape_warnings(self):
        """反向护栏：一份正常契约不能出现任何形态告警。"""
        from interfacetester.converters.openapi_adapter import convert_openapi

        document = {
            "openapi": "3.0.0",
            "info": {"title": "t"},
            "servers": [{"url": "https://api.test"}],
            "components": {
                "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}
            },
            "security": [{"bearerAuth": []}],
            "paths": {
                "/x": {
                    "get": {
                        "parameters": [
                            {
                                "name": "q",
                                "in": "query",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }

        case = convert_openapi(document, case_name="t")[0]

        self.assertEqual(_shape_warnings(case.all_warnings()), [])
        self.assertEqual(
            case.steps[0].headers.get("Authorization"), "Bearer ${AUTH_TOKEN}"
        )

    # ------------------------------------------------------------ curl
    def test_curl_data_and_form_conflict_is_reported(self):
        """L5：`-d` 与 `-F` 同时出现（真实 curl 直接 exit 2）→ 必须点名说明丢弃了什么。

        NOTICE（0920 批次 4 / **N21** 订正）：本条原本断言告警里出现**被丢弃的原文**
        （`a=1&b=2`）。但那条告警会进 stdout 与 `hconvert --report` 的 markdown，
        而 `-d` 的体里经常就是凭据（`user=bob&password=…`）——
        "把丢掉的原文打出来"与"告警不得回显凭据"直接冲突。

        现在两全：告警仍说清**丢的是什么形状**（字段名逐个列出），但**值一律不出现**。
        非敏感的值（`a=1&b=2` 里的 `1`/`2`）固然无害，可判据是"按名判定"、
        不可能逐值甄别，所以统一不回显值 —— 与 `without_userinfo`（M13）同口径。
        """
        case = curl_adapter.convert_curl_file(
            "curl -X POST https://api.test/x -d 'a=1&b=2' -F 'c=3'", case_name="t"
        )

        text = "\n".join(case.all_warnings())
        self.assertIn("同时出现", text)
        self.assertIn("丢弃", text)
        # 形状可见（字段名在），但值不回显
        self.assertIn("a", text, "要能看出被丢弃的是什么字段")
        self.assertIn("b", text)
        self.assertNotIn("a=1&b=2", text, "被丢弃的原文不应回显（可能是凭据）")
        self.assertEqual(case.steps[0].data, {"c": "3"}, "行为：按 -F 处理")

    def test_curl_data_and_form_conflict_does_not_echo_credentials(self):
        """N21：同一条告警不得回显凭据值（会随 `--report` 外泄）。"""
        canary = "CANARY_SECRET_9f3ab21c"
        case = curl_adapter.convert_curl_file(
            f"curl -X POST https://api.test/x "
            f"-d 'user=bob&password={canary}&token=tok-{canary}' "
            f"-F 'c=3'",
            case_name="t",
        )

        text = "\n".join(case.all_warnings())
        self.assertNotIn(canary, text, "告警回显了凭据明文")
        self.assertIn("password=***", text, "敏感字段名要留着并标出已脱敏")
        self.assertIn("user", text)

    def test_curl_form_only_case_has_no_conflict_warning(self):
        """反向护栏：只用 `-F`（没有 `-d`）时不该出现这条告警。"""
        case = curl_adapter.convert_curl_file(
            "curl -X POST https://api.test/x -F 'c=3'", case_name="t"
        )

        self.assertNotIn("同时出现", "\n".join(case.all_warnings()))


class TestBatchECliWritePath(unittest.TestCase):
    """批次 E / **L6 + L7**：`hconvert` 的写盘路径。

    L6：写盘异常必须收口成**可读报错**（修复前裸 traceback，而 `try` 只包住了"解析"段）；
    L7：跨两次运行落到同一个 `.yml` 从**静默覆盖**变成**点名告警**（内容相同则静默，幂等重跑不打扰），
    并报告上一次留下的孤儿 `schemas/*.json`。
    """

    def setUp(self):
        self.tmp_dir = _tmp_dir("tmp_cli_batchE")
        loader.project_meta = None
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        self._original_argv = sys.argv

    def tearDown(self):
        sys.argv = self._original_argv
        pytest_files_made_cache_mapping.clear()
        pytest_files_run_set.clear()
        loader.project_meta = None
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _run_cli_with_logs(self, *args):
        """跑真实 `hconvert` 入口并捕获 loguru 告警（与批次 0918-8 的同类用例同款）。"""
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            with mock.patch("interfacetester.cli.init_logger", lambda level: None):
                sys.argv = ["hconvert", *args]
                try:
                    main_convert_alias()
                    code = 0
                except SystemExit as ex:
                    code = int(ex.code or 0)
        finally:
            try:
                logger.remove(sink_id)
            except ValueError:
                pass
        return code, messages

    def _curl_asset(self, name: str, url: str) -> str:
        return _write(os.path.join(self.tmp_dir, name), f"curl {url}")

    # ---------------------------------------------------------------- L6
    def test_out_pointing_at_an_existing_file_is_a_usage_error(self):
        """`--out` 是已存在的**文件** → exit 2 + 点名报错（修复前裸 `FileExistsError`）。"""
        out_file = _write(os.path.join(self.tmp_dir, "not_a_dir"), "x")

        code, messages = self._run_cli_with_logs(
            "--from",
            "curl",
            "--in",
            self._curl_asset("c.txt", "https://api.test/x"),
            "--out",
            out_file,
        )

        self.assertEqual(code, 2)
        self.assertTrue(
            any("--out" in m and "不是目录" in m for m in messages), messages
        )

    def test_report_pointing_at_a_directory_is_reported_and_cases_are_kept(self):
        """`--report` 是**目录** → 点名报错 + exit 1，而**用例文件仍然在**（修复前裸崩）。"""
        out_dir = os.path.join(self.tmp_dir, "out_report")

        code, messages = self._run_cli_with_logs(
            "--from",
            "curl",
            "--in",
            self._curl_asset("c.txt", "https://api.test/x"),
            "--out",
            out_dir,
            "--name",
            "report_case",
            "--report",
            self.tmp_dir,
        )

        self.assertEqual(code, 1)
        self.assertTrue(any("写导入报告失败" in m for m in messages), messages)
        self.assertTrue(
            os.path.isfile(os.path.join(out_dir, "report_case.yml")),
            "用例已经生成成功，不该因为报告写不出来而丢失",
        )

    # ---------------------------------------------------------------- L7
    def test_cross_run_overwrite_is_reported(self):
        """第二次运行用**不同内容**落到同名 `.yml` → 必须点名告警（修复前静默覆盖）。"""
        out_dir = os.path.join(self.tmp_dir, "out_overwrite")
        first = self._curl_asset("first.txt", "https://api.test/x")
        second = self._curl_asset("second.txt", "https://api.test/y")

        code_one, _ = self._run_cli_with_logs(
            "--from", "curl", "--in", first, "--out", out_dir, "--name", "same"
        )
        code_two, messages = self._run_cli_with_logs(
            "--from", "curl", "--in", second, "--out", out_dir, "--name", "same"
        )

        self.assertEqual((code_one, code_two), (0, 0))
        self.assertTrue(
            any("覆盖了上一次运行留下的文件" in m for m in messages), messages
        )

    def test_same_source_rerun_is_silent(self):
        """反向护栏：同源重跑（内容逐字相同）**不许打扰用户**（幂等重跑是常态）。"""
        out_dir = os.path.join(self.tmp_dir, "out_idempotent")
        asset = self._curl_asset("again.txt", "https://api.test/x")

        self._run_cli_with_logs(
            "--from", "curl", "--in", asset, "--out", out_dir, "--name", "same"
        )
        _code, messages = self._run_cli_with_logs(
            "--from", "curl", "--in", asset, "--out", out_dir, "--name", "same"
        )

        self.assertFalse([m for m in messages if "覆盖了上一次" in m], messages)

    def test_orphaned_schema_files_are_reported(self):
        """上一次运行留下的 `schemas/*.json` 只**报告**、不擅自删除。"""
        out_dir = os.path.join(self.tmp_dir, "out_orphan")
        asset = self._curl_asset("orphan.txt", "https://api.test/x")
        self._run_cli_with_logs(
            "--from", "curl", "--in", asset, "--out", out_dir, "--name", "same"
        )

        schemas_dir = os.path.join(out_dir, "schemas")
        os.makedirs(schemas_dir, exist_ok=True)
        orphan = os.path.join(schemas_dir, "same_01_orphan.json")
        _write(orphan, '{"type": "object"}')

        _code, messages = self._run_cli_with_logs(
            "--from", "curl", "--in", asset, "--out", out_dir, "--name", "same"
        )

        self.assertTrue(
            any(
                "上一次运行留下的 schema 文件" in m and "orphan" in m for m in messages
            ),
            messages,
        )
        self.assertTrue(os.path.isfile(orphan), "只报告，不许擅自删除")
