# -*- coding: utf-8 -*-
"""跨源语义对照不变量（0918-8 §六.2 的收尾，§15.3 的遗留项）。

## 为什么需要这个文件

三个导入器（curl / HAR / Postman）各自都有一整套测试，但**没有任何测试把它们放在一起比**——
于是「同一个请求、三种源、三种产物」可以长期共存：用户拿到哪个素材，就得到哪个行为。
本文件把这条做成不变量：**一份语义描述 → 生成三种源素材 → 转出来的 `request:` 段必须逐字段相等。**

## 它当场抓到的两条真实缺陷（修完才写得出这个夹具）

| 缺陷 | 现象（同一请求三种源） | 根因 |
|---|---|---|
| **H12**（高·凭据明文） | curl/HAR 的 JSON 体结构化并占位，**Postman 的体是字符串**：`data: '{"username":"alice","password":"s3cr3t-pw"}'` —— 口令/token **明文写进 YAML** | `_body_from_request` 只看 `body.options.raw.language`（可选提示，导出时常缺失），不看 `Content-Type` |
| **M33**（中·静默丢字段） | curl/HAR 都保住了 cookie，**Postman 的 cookie 凭空消失**（生成的用例发的是另一个请求） | `Cookie` 在 `DERIVED_HEADERS` 里（那条规则是为 HAR/curl 写的），Postman 复用它却没有第二个 cookie 载体 |

## 判据与「刻意不同的地方」

- **强等价（S1）**：`config.base_url` / `config.variables` / `request` 段**逐字段相等**。
- **表单体（S2）表示有意不同**：curl 留 `a=1&b=2` 字符串，HAR/Postman 给 dict。
  在框架里等价（`data` 是 dict 时 requests 编码成 form-urlencoded；三源都保留了
  `Content-Type`），因此按**归一化后的键值对**比较，并把这个差异显式钉住。
- **断言有意不同**：curl 素材里没有响应 → 不生成任何断言（且在告警里明确说明）；
  HAR/Postman 有录制的响应 → 生成 `status_code` 断言。夹具显式断言这个差异，
  这样"哪天 curl 也开始生成断言"不会无声发生。
- **OpenAPI 不在对照范围内**：它是**契约**（没有"那一次请求"），
  「同一请求多源表达」对契约源没有意义，硬凑只会造出假等价。
"""

import json
import os
import unittest
from unittest import mock

from interfacetester import parser
from interfacetester.converters import (
    build_case_dict,
    curl_adapter,
    har_adapter,
    postman_adapter,
)
from interfacetester.converters.emit_yaml import dump_case_yaml
from interfacetester.exceptions import FunctionNotFound

ORIGIN = "https://api.example.com"
PATH = "/v1/orders"
QUERY = {"page": "2", "size": "10"}

# 这三个值是"明文 canary"：任何一处出现在生成物里都算失败
PLAIN_PASSWORD = "s3cr3t-pw"
PLAIN_BODY_TOKEN = "tok-123"
PLAIN_COOKIE = "abc123"
PLAIN_BEARER = "t0ken-value"

EXPECTED_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-Trace-Id": "trace-123",
    "Authorization": "Bearer ${AUTH_TOKEN}",
}
EXPECTED_COOKIES = {"session": "${COOKIE_SESSION}"}
EXPECTED_JSON_BODY = {
    "username": "alice",
    "password": "${BODY_PASSWORD}",
    "token": "${BODY_TOKEN}",
}

JSON_SPEC = {
    "case_name": "orders",
    "method": "POST",
    "path": PATH,
    "query": dict(QUERY),
    "headers": {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Trace-Id": "trace-123",
        "Authorization": f"Bearer {PLAIN_BEARER}",
    },
    "cookies": {"session": PLAIN_COOKIE},
    "body_kind": "json",
    "body": {"username": "alice", "password": PLAIN_PASSWORD, "token": PLAIN_BODY_TOKEN},
    "response_body": {"code": 0, "data": {"id": 1}},
}

FORM_SPEC = {
    "case_name": "form",
    "method": "POST",
    "path": PATH,
    "query": {"from": "form"},
    "headers": {"Content-Type": "application/x-www-form-urlencoded"},
    "cookies": {},
    "body_kind": "form",
    "body": {"order_no": "A-1", "amount": "12.5"},
    "response_body": {"code": 0},
}

UPLOAD_FILE = "data/file_to_upload.bin"
UPLOAD_SPEC = {
    "case_name": "upload",
    "method": "POST",
    "path": "/v1/upload",
    "query": {},
    "headers": {},
    "cookies": {},
    "body_kind": "multipart",
    "body": {"note": "hello"},
    "upload": {"file": UPLOAD_FILE},
    "response_body": {"code": 0},
}

# 0919-2 / 缺陷 8：**重名查询键**（批量删除接口的常见形态）。
# 用 `query_pairs` 而不是 `query`（dict 装不下重名），见 `_spec_query_pairs`。
DUPLICATE_QUERY_SPEC = {
    "case_name": "batch_delete",
    "method": "DELETE",
    "path": "/v1/orders",
    "query_pairs": [("ids", "1"), ("ids", "2"), ("ids", "3")],
    "headers": {"Content-Type": "application/json"},
    "cookies": {},
    "body_kind": "json",
    "body": {"reason": "cleanup"},
    "response_body": {"code": 0},
}


# --------------------------------------------------------------------------- 素材构造
def _body_text(spec) -> str:
    return json.dumps(spec["body"]) if spec["body_kind"] == "json" else _form_text(spec)


def _form_text(spec) -> str:
    return "&".join(f"{key}={value}" for key, value in spec["body"].items())


def _spec_query_pairs(spec) -> list:
    """规格里的查询参数 → **有序键值对**（允许重名）。

    NOTICE（0919-2 / 缺陷 8）：`query` 原本是 dict，**表达不了** `?ids=1&ids=2`
    ——而重名查询串恰恰是这个缺陷的输入形态（批量删除/批量查询）。
    这里新增可选的 `query_pairs`（列表形态，保留重名与顺序），dict 形态照旧，
    两种写法都归一成键值对。既有规格的行为逐字不变。
    """
    if "query_pairs" in spec:
        return [(str(key), str(value)) for key, value in spec["query_pairs"]]
    return [(str(key), str(value)) for key, value in (spec.get("query") or {}).items()]


def _url(spec) -> str:
    query = "&".join(f"{key}={value}" for key, value in _spec_query_pairs(spec))
    return f"{ORIGIN}{spec['path']}" + (f"?{query}" if query else "")


def curl_material(spec, drop_cookie: bool = False) -> str:
    """把语义描述渲染成一条 curl 命令。"""
    parts = [f"curl -X {spec['method']}", f"'{_url(spec)}'"]
    for name, value in spec["headers"].items():
        parts.append(f"-H '{name}: {value}'")
    if spec["cookies"] and not drop_cookie:
        cookie_text = "; ".join(f"{k}={v}" for k, v in spec["cookies"].items())
        parts.append(f"-b '{cookie_text}'")
    if spec["body_kind"] == "multipart":
        for name, path in spec["upload"].items():
            parts.append(f"-F '{name}=@{path}'")
        for name, value in spec["body"].items():
            parts.append(f"-F '{name}={value}'")
    elif spec["body_kind"] == "json":
        parts.append(f"--data-raw '{_body_text(spec)}'")
    else:
        parts.append(f"--data '{_body_text(spec)}'")
    return " ".join(parts)


def har_material(spec, drop_cookie: bool = False) -> dict:
    """把语义描述渲染成一份 HAR 1.2（浏览器 DevTools 导出的形态）。"""
    headers = [{"name": name, "value": value} for name, value in spec["headers"].items()]
    headers += [
        {"name": "Host", "value": "api.example.com"},
        {"name": "Content-Length", "value": str(len(_body_text(spec)))},
    ]
    if spec["cookies"] and not drop_cookie:
        headers.append(
            {
                "name": "Cookie",
                "value": "; ".join(f"{k}={v}" for k, v in spec["cookies"].items()),
            }
        )

    if spec["body_kind"] == "multipart":
        # 浏览器录下来的 multipart：文件字段用 `fileName`，其余是 `value`
        headers.append(
            {"name": "Content-Type", "value": "multipart/form-data; boundary=----har"}
        )
        post_data = {
            "mimeType": "multipart/form-data",
            "params": [
                {"name": name, "fileName": path}
                for name, path in spec["upload"].items()
            ]
            + [
                {"name": name, "value": value}
                for name, value in spec["body"].items()
            ],
        }
    else:
        post_data = {
            "mimeType": spec["headers"]["Content-Type"],
            "text": _body_text(spec),
        }
        if spec["body_kind"] == "form":
            post_data["params"] = [
                {"name": name, "value": value} for name, value in spec["body"].items()
            ]

    return {
        "log": {
            "version": "1.2",
            "entries": [
                {
                    "request": {
                        "method": spec["method"],
                        "url": _url(spec),
                        "headers": headers,
                        "cookies": [
                            {"name": name, "value": value}
                            for name, value in spec["cookies"].items()
                        ],
                        "queryString": [
                            {"name": name, "value": value}
                            for name, value in _spec_query_pairs(spec)
                        ],
                        "postData": post_data,
                    },
                    "response": {
                        "status": 200,
                        "headers": [
                            {"name": "Content-Type", "value": "application/json"}
                        ],
                        "content": {
                            "mimeType": "application/json",
                            "text": json.dumps(spec["response_body"]),
                        },
                    },
                }
            ],
        }
    }


def postman_material(spec, drop_cookie: bool = False) -> dict:
    """把语义描述渲染成一份 Postman Collection v2.1（导出形态）。"""
    header = [{"key": name, "value": value} for name, value in spec["headers"].items()]
    if spec["cookies"] and not drop_cookie:
        header.append(
            {
                "key": "Cookie",
                "value": "; ".join(f"{k}={v}" for k, v in spec["cookies"].items()),
            }
        )

    if spec["body_kind"] == "json":
        body = {"mode": "raw", "raw": _body_text(spec)}
    elif spec["body_kind"] == "multipart":
        body = {
            "mode": "formdata",
            "formdata": [
                {"key": name, "value": path, "type": "file", "src": path}
                for name, path in spec["upload"].items()
            ]
            + [
                {"key": name, "value": value, "type": "text"}
                for name, value in spec["body"].items()
            ],
        }
    else:
        body = {
            "mode": "urlencoded",
            "urlencoded": [
                {"key": name, "value": value} for name, value in spec["body"].items()
            ],
        }

    return {
        "info": {
            "name": spec["case_name"],
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": [
            {
                "name": spec["case_name"],
                "request": {
                    "method": spec["method"],
                    "header": header,
                    "url": {
                        "raw": _url(spec),
                        "protocol": "https",
                        "host": ["api", "example", "com"],
                        "path": spec["path"].strip("/").split("/"),
                        "query": [
                            {"key": name, "value": value}
                            for name, value in _spec_query_pairs(spec)
                        ],
                    },
                    "body": body,
                },
                "response": [
                    {
                        "code": 200,
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": json.dumps(spec["response_body"]),
                    }
                ],
            }
        ],
    }


def convert_all(spec, drop_cookie: bool = False) -> dict:
    """三种素材 → `{源名: IRCase}`。"""
    return {
        "curl": curl_adapter.convert_curl_file(
            curl_material(spec, drop_cookie), case_name=spec["case_name"]
        ),
        "har": har_adapter.convert_har(
            har_material(spec, drop_cookie),
            case_name=spec["case_name"],
            assertion_mode="status",
        ),
        "postman": postman_adapter.convert_postman(
            postman_material(spec, drop_cookie),
            case_name=spec["case_name"],
            assertion_mode="status",
        )[0],
    }


def request_dict(case) -> dict:
    return build_case_dict(case)["teststeps"][0]["request"]


def config_dict(case) -> dict:
    return build_case_dict(case)["config"]


def _query_pairs(url: str, params) -> list:
    """把查询串与 `params` 归一化成有序键值对（无论源把它放在哪一侧）。"""
    pairs = []
    path, _, query = str(url or "").partition("?")
    for chunk in query.split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        pairs.append((key, value))
    for key, value in (params or {}).items():
        pairs.append((str(key), str(value)))
    return sorted(pairs)


def _normalized_form(data) -> list:
    """把 `data` 的两种表示（字符串 / dict）归一化成有序键值对。

    NOTICE（有意的表示差异）：curl 源把 `-d 'a=1&b=2'` 原样留在 `data` 字符串里，
    HAR/Postman 走结构化 dict。两者在框架里等价——`data` 是 dict 时 requests 会编码成
    form-urlencoded，而三源都保留了 `Content-Type`，因此线上字节一致。
    """
    if data is None:
        return []
    if isinstance(data, str):
        pairs = []
        for chunk in data.split("&"):
            if not chunk:
                continue
            key, _, value = chunk.partition("=")
            pairs.append((key, value))
        return sorted(pairs)
    if isinstance(data, dict):
        return sorted((str(key), str(value)) for key, value in data.items())
    raise AssertionError(f"表单体既不是字符串也不是 dict：{data!r}")


def semantic_request(case) -> dict:
    """`request:` 段的**语义**归一化形式（用于跨源比较）。

    NOTICE（两处刻意的表示差异，都在这里被抹平，并另有测试显式钉住）：
    1. **查询串位置**：curl 源把 `?page=2` 留在 `url` 上（`sanitize_url` 对它有专门处理），
       HAR/Postman 走 `params`。两者拼出来的最终 URL 相同；
    2. **表单体表示**：curl 留字符串，HAR/Postman 给 dict（见 `_normalized_form`）。
    """
    request = request_dict(case)
    path = str(request.get("url") or "").split("?", 1)[0]
    return {
        "method": request.get("method"),
        "path": path,
        "query": _query_pairs(request.get("url"), request.get("params")),
        "headers": request.get("headers") or {},
        "cookies": request.get("cookies") or {},
        "json": request.get("json"),
        "form": _normalized_form(request.get("data")),
        "upload": request.get("upload") or {},
    }


class TestCrossSourceEquivalence(unittest.TestCase):
    """同一请求的三种源表达必须产出**语义等价**的用例。"""

    def test_each_source_yields_one_step(self):
        """夹具自检：三种源都得真的解析出东西来（否则下面的"相等"是空洞的）。"""
        cases = convert_all(JSON_SPEC)

        for source, case in cases.items():
            with self.subTest(source=source):
                self.assertEqual(len(case.steps), 1, f"{source} 没解析出步骤：{case.all_warnings()}")
                self.assertEqual(case.steps[0].method, "POST")
                self.assertTrue(case.steps[0].json_body, f"{source} 没有解析出 JSON 体")

    def test_request_semantics_are_identical_across_sources(self):
        """**核心判据**：`request:` 段的语义归一化形式逐字段相等。

        （归一化抹平的两处表示差异见 `semantic_request`，另有专门用例钉住它们。）
        """
        semantics = {
            source: semantic_request(case) for source, case in convert_all(JSON_SPEC).items()
        }

        expected = {
            "method": "POST",
            "path": "/v1/orders",
            "query": [("page", "2"), ("size", "10")],
            "headers": EXPECTED_HEADERS,
            "cookies": EXPECTED_COOKIES,
            "json": EXPECTED_JSON_BODY,
            "form": [],
            "upload": {},
        }
        self.assertEqual(semantics["curl"], expected)
        self.assertEqual(semantics["har"], expected)
        self.assertEqual(semantics["postman"], expected)

    def test_representation_differences_are_pinned(self):
        """两处**刻意的表示差异**必须被显式钉住（不是"碰巧一致"）。

        1. curl 把查询串留在 `url` 上，HAR/Postman 走 `params`；
        2. curl 的表单体是字符串，HAR/Postman 是 dict（见 S2 用例）。
        """
        requests = {source: request_dict(case) for source, case in convert_all(JSON_SPEC).items()}

        self.assertIn("?", requests["curl"]["url"])
        self.assertNotIn("params", requests["curl"])
        for source in ("har", "postman"):
            with self.subTest(source=source):
                self.assertNotIn("?", requests[source]["url"])
                self.assertEqual(requests[source]["params"], {"page": "2", "size": "10"})

    def test_duplicate_query_keys_survive_in_every_source(self):
        """**缺陷 8 的判据**：重名查询键在三种源里都必须**一个不少**。

        NOTICE（0919-2 / 缺陷 8）：修复前这三种源对 `?ids=1&ids=2&ids=3` 给出**两种**结果 ——

        | 源 | 修复前 | 结果 |
        |---|---|---|
        | curl | 查询串留在 URL 上 | 3 个都在 ✅ |
        | HAR `queryString` | 告警 + 查询串留在 URL 上（M33） | 3 个都在 ✅ |
        | HAR `postData.params` | 静默留最后一个 | **只剩 1 个** ❌ |
        | Postman `url.query` | 静默留最后一个 | **只剩 1 个** ❌ |

        这正是跨源对照夹具存在的意义：每个适配器**各自的**测试都是绿的
        （「解析出了 params」看起来完全正常），只有把同一份输入喂给三个源才能发现
        「同一请求、两种结果」。批量删除接口少发两个 id = 少删两条数据，且零告警。
        """
        cases = convert_all(DUPLICATE_QUERY_SPEC)
        semantics = {
            source: semantic_request(case) for source, case in cases.items()
        }
        expected_query = [("ids", "1"), ("ids", "2"), ("ids", "3")]

        for source, semantic in semantics.items():
            with self.subTest(source=source):
                self.assertEqual(
                    semantic["query"],
                    expected_query,
                    f"{source} 源把重名查询键压掉了 —— 回放会**少发参数**（缺陷 8）",
                )

        self.assertEqual(semantics["curl"], semantics["har"])
        self.assertEqual(semantics["curl"], semantics["postman"])

    def test_duplicate_query_keys_warn_instead_of_silently_collapsing(self):
        """重名必须**可见**：压不住的源要告警，不能零信号。"""
        cases = convert_all(DUPLICATE_QUERY_SPEC)

        har_warnings = " ".join(cases["har"].all_warnings())
        postman_warnings = " ".join(cases["postman"].all_warnings())

        self.assertIn("重名", har_warnings)
        self.assertIn("重名", postman_warnings)

    def test_non_duplicate_query_keys_still_go_to_params(self):
        """反向护栏：没有重名时**行为完全不变**（HAR/Postman 照旧拆成 `params` dict）。

        这条防的是「为了修重名把正常路径也改成留字符串」——那会白白丢掉结构化表示。
        """
        requests = {
            source: request_dict(case)
            for source, case in convert_all(JSON_SPEC).items()
        }

        for source in ("har", "postman"):
            with self.subTest(source=source):
                self.assertEqual(
                    requests[source]["params"], {"page": "2", "size": "10"}
                )

    def test_config_is_equivalent(self):
        configs = {source: config_dict(case) for source, case in convert_all(JSON_SPEC).items()}
        for source, config in configs.items():
            with self.subTest(source=source):
                self.assertEqual(config["base_url"], ORIGIN)
                self.assertEqual(
                    config["variables"],
                    {
                        "AUTH_TOKEN": "${ENV(AUTH_TOKEN)}",
                        "COOKIE_SESSION": "${ENV(COOKIE_SESSION)}",
                        "BODY_PASSWORD": "${ENV(BODY_PASSWORD)}",
                        "BODY_TOKEN": "${ENV(BODY_TOKEN)}",
                    },
                )

    def test_no_plaintext_secrets_in_any_source_output(self):
        """**H12 的护栏**：三种源的生成物里都不得出现明文口令/token/cookie。

        NOTICE（修复前实测）：Postman 的 raw JSON 体没被结构化 → `data` 是字符串
        → `sanitize_body`（只认 dict/list）不生效 → `s3cr3t-pw` / `tok-123` **明文入库**。
        """
        for source, case in convert_all(JSON_SPEC).items():
            yaml_text = dump_case_yaml(case, f"{source}.yml")
            for secret in (PLAIN_PASSWORD, PLAIN_BODY_TOKEN, PLAIN_COOKIE, PLAIN_BEARER):
                with self.subTest(source=source, secret=secret):
                    self.assertNotIn(secret, yaml_text)

    def test_assertion_difference_is_intentional(self):
        """断言生成**有意不同**（curl 素材没有响应）——显式钉住，避免无声漂移。"""
        cases = convert_all(JSON_SPEC)

        self.assertEqual(cases["curl"].steps[0].assertions, [])
        self.assertTrue(
            any("没有生成任何断言" in warning for warning in cases["curl"].all_warnings()),
            "curl 不生成断言这件事必须在告警里说清楚",
        )
        for source in ("har", "postman"):
            with self.subTest(source=source):
                self.assertEqual(
                    cases[source].steps[0].assertions, [{"eq": ["status_code", 200]}]
                )

    def test_form_body_representations_are_semantically_equal(self):
        """S2：表单体的字符串/dict 两种表示归一化后必须相等（差异见 `_normalized_form`）。"""
        cases = convert_all(FORM_SPEC)
        semantics = {source: semantic_request(case) for source, case in cases.items()}

        expected = {
            "method": "POST",
            "path": "/v1/orders",
            "query": [("from", "form")],
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "cookies": {},
            "json": None,
            "form": [("amount", "12.5"), ("order_no", "A-1")],
            "upload": {},
        }
        for source, semantic in semantics.items():
            with self.subTest(source=source):
                self.assertEqual(semantic, expected)

        # 表示差异本身也要钉住（不是"碰巧一样"）
        requests = {source: request_dict(case) for source, case in cases.items()}
        self.assertIsInstance(requests["curl"]["data"], str)
        self.assertIsInstance(requests["har"]["data"], dict)
        self.assertIsInstance(requests["postman"]["data"], dict)
        self.assertIn("?", requests["curl"]["url"])
        self.assertNotIn("params", requests["curl"])
        for source in ("har", "postman"):
            with self.subTest(source=source):
                self.assertEqual(requests[source]["params"], {"from": "form"})

    def test_multipart_upload_is_equivalent_across_sources(self):
        """S3：multipart（文件 + 普通字段）三源等价——`upload` 必须完整。

        NOTICE（第三处有意的表示差异）：HAR 会把录到的
        `Content-Type: multipart/form-data; boundary=----har` 原样保留，curl/Postman 不带。
        这个差异**无害**：运行期 `ext/uploader.prepare_upload_step()` 会用请求自己的
        multipart 编码器**无条件覆盖** Content-Type（那个 boundary 属于录制时的那次连接），
        所以这里比较语义时把它摘掉，并单独钉住这个事实。

        NOTICE（0920 / **缺陷 1**，**修正被钉住的错误形态**）：修复前这里期望
        `form: [("note", "hello")]` + `upload: {"file": ...}` —— 也就是把普通字段留在
        `data` 里。那是**跑不通的形态**：运行期 `prepare_upload_step` 会把 `step.request.data`
        整个换成 `$m_encoder`，`note` 因此**静默丢失**（实测服务端只收到 `file`）。
        现在 `emit_yaml.build_request_dict` 在导出时把 `data`（dict）并进 `upload`，
        所以正确期望是 `form: []` + `upload` 里同时有 `file` 与 `note` —— 普通字段与文件
        都由 uploader 组进 multipart（与 `examples/body_forms/README.md` 的约定一致）。
        """
        cases = convert_all(UPLOAD_SPEC)
        semantics = {}
        for source, case in cases.items():
            semantic = semantic_request(case)
            semantic["headers"] = {
                name: value
                for name, value in semantic["headers"].items()
                if name.lower() != "content-type"
            }
            semantics[source] = semantic

        expected = {
            "method": "POST",
            "path": "/v1/upload",
            "query": [],
            "headers": {},
            "cookies": {},
            "json": None,
            # 普通字段不再留在 `data`（那一段会被 upload 机制覆盖掉）
            "form": [],
            "upload": {"file": UPLOAD_FILE, "note": "hello"},
        }
        for source, semantic in semantics.items():
            with self.subTest(source=source):
                self.assertEqual(semantic, expected)

        # 差异本身钉住：HAR 保留了录制时的 boundary，另两个源不带
        har_request = request_dict(cases["har"])
        self.assertIn("boundary=", har_request["headers"]["Content-Type"])
        for source in ("curl", "postman"):
            with self.subTest(source=source):
                self.assertNotIn("Content-Type", request_dict(cases[source]).get("headers", {}))

        # 非空洞自检：产物里**不能**再出现 `data`（一旦出现就说明又回到"会被覆盖"的形态）
        for source in semantics:
            with self.subTest(source=source, check="no_data_key"):
                self.assertNotIn("data", request_dict(cases[source]))

    def test_fixture_detects_a_dropped_cookie(self):
        """非空洞自检：把 Postman 素材的 Cookie 整个拿掉，等价判据**必须**看出差异。

        NOTICE: 这条同时是 **M33 的回归**——修复前 Postman 的 `Cookie` 头被当"派生头"
        丢弃且没有兜底，而「curl/HAR 有 cookie、Postman 没有」这种差异
        在修复前**不会被任何测试发现**（三个适配器各测各的）。
        """
        full = convert_all(JSON_SPEC)
        for source, case in full.items():
            with self.subTest(source=source):
                self.assertEqual(case.steps[0].cookies, EXPECTED_COOKIES)

        material = postman_material(JSON_SPEC)
        material["item"][0]["request"]["header"] = [
            entry
            for entry in material["item"][0]["request"]["header"]
            if entry["key"].lower() != "cookie"
        ]
        degraded = postman_adapter.convert_postman(
            material, case_name="orders", assertion_mode="status"
        )[0]

        self.assertEqual(degraded.steps[0].cookies, {})
        self.assertNotEqual(
            semantic_request(degraded),
            semantic_request(full["curl"]),
            "夹具没有区分度：cookie 丢了却看不出差异",
        )

    def test_fixture_detects_a_plaintext_body(self):
        """非空洞自检：canary 断言必须**真的会红**。

        NOTICE（0919-22 / **H8** 更新）：这条自检原先用的形态是「raw 体 + 不声明
        Content-Type」，理由是当时**字符串体一律不脱敏**、明文必然入 YAML ——
        因此它能证明 `test_no_plaintext_secrets_in_any_source_output` 不是空跑。
        0919-22 把字符串体也接上了结构化脱敏（H8），**该形态不再泄漏**，
        自检的前提随之消失（实测：同一个降级夹具现在生成的是
        `data: '{"username":"alice","password":"${BODY_PASSWORD}",...}'`）。

        所以这里拆成两半，各钉一件**不同**的事实：

        ① 那个降级夹具**现在也被脱敏** —— 这是 H8 在跨源层面的回归钉子；
        ② 自检换用一个脱敏**结构上确实看不见**的形态（纯文本体里裸着一个口令：
           既不是 JSON、也没有 `k=v` 可切）来证明 canary 断言仍有判别力 ——
           否则「① 变绿」有可能只是因为断言永远不会红。
           **这个盲区是有意保留并已登记的**（见 `新发现缺陷记录0919.md` 的 L20：
           脱敏按字段名/`k=v` 判定，无结构的裸凭据本就判不出来）。
        """
        # ① H8：不声明 Content-Type 的 JSON raw 体也必须脱敏
        material = postman_material(JSON_SPEC)
        # 复刻修复前的形态：raw 体 + language 缺失 + **不**声明 Content-Type
        material["item"][0]["request"]["body"] = {
            "mode": "raw",
            "raw": json.dumps(JSON_SPEC["body"]),
        }
        material["item"][0]["request"]["header"] = [
            entry
            for entry in material["item"][0]["request"]["header"]
            if entry["key"].lower() != "content-type"
        ]
        degraded = postman_adapter.convert_postman(
            material, case_name="orders", assertion_mode="status"
        )[0]

        yaml_text = dump_case_yaml(degraded, "degraded.yml")
        self.assertNotIn(
            PLAIN_PASSWORD,
            yaml_text,
            "不声明 Content-Type 的 JSON 字符串体又明文入库了（H8 回归）",
        )
        self.assertNotIn(PLAIN_BODY_TOKEN, yaml_text)
        self.assertIn("${BODY_PASSWORD}", yaml_text)
        # 也正因如此，它**不**等于另外两个源的语义形式（夹具仍能看出差异）
        self.assertNotEqual(
            semantic_request(degraded), semantic_request(convert_all(JSON_SPEC)["curl"])
        )

        # ② 自检：脱敏看不见的形态必须**照样漏**，证明上面那条断言不是恒真
        opaque = postman_material(JSON_SPEC)
        opaque["item"][0]["request"]["body"] = {
            "mode": "raw",
            # 纯文本、无 `=`、不是 JSON → 没有字段名可判，脱敏结构上够不着
            "raw": f"user alice says the password is {PLAIN_PASSWORD}",
        }
        opaque["item"][0]["request"]["header"] = [
            entry
            for entry in opaque["item"][0]["request"]["header"]
            if entry["key"].lower() != "content-type"
        ]
        opaque_case = postman_adapter.convert_postman(
            opaque, case_name="opaque", assertion_mode="status"
        )[0]

        self.assertIn(
            PLAIN_PASSWORD,
            dump_case_yaml(opaque_case, "opaque.yml"),
            "这条自检失效了：连「脱敏结构上够不着」的形态都没漏出来，"
            "说明 ① 里的 assertNotIn 不可能红，那条护栏等于没有",
        )


class TestBatch0918_9SourceExpressionGuard(unittest.TestCase):
    """批次 9-0（M9）：**任何源**的产物都不允许变成「可执行的表达式」。

    这是 8-6 这个跨源夹具的自然延伸：判据从「同一个请求换源还是不是同一个用例」
    变成「同一个请求换源，能不能变成代码执行」。

    NOTICE（修复前实测，`docs/缺陷修复日志0918-9.md` §1）：一份 Postman 集合
    （变量 `p` = payload、URL/头里写 `${eval($p)}`）→ `hconvert` **零告警** →
    生成的 YAML 原样带着表达式 → `hrun` **真的执行**了 payload，用例还报 passed / exit 0。
    三个适配器的单测全是绿的：它们各测各的产物，"导入的东西会不会被执行"不是任何一条的判据。

    本类的两条判据：
    1. **导入期**：素材里出现 `${...}` 必须告警（不擅自改写，理由见 `ir.warn_source_expressions`）；
    2. **运行期**：这些表达式在插值路径**必须拒绝执行**（内置白名单，`parser.ALLOWED_BUILTINS_IN_INTERPOLATION`），
       并且第 3 条用例证明这个拒绝不是空跑（打开逃生口后同一个表达式真的会执行）。
    """

    EXPRESSION = "${eval($p)}"
    # 无副作用的 payload：真被执行时返回 42（用它证明"会执行"，而不是只断言"没报错"）
    HARMLESS_PAYLOAD = "40+2"

    def _expression_spec(self) -> dict:
        spec = dict(JSON_SPEC)
        spec["case_name"] = "expression"
        spec["headers"] = dict(JSON_SPEC["headers"])
        spec["headers"]["X-Expression"] = self.EXPRESSION
        # 让变量 `p` 被"正常引用"一次，这样它才会被保留进 config.variables
        spec["headers"]["X-Payload-Ref"] = "${p}"
        return spec

    def _cases_with_expression(self) -> dict:
        spec = self._expression_spec()
        postman_material_with_variable = postman_material(spec)
        postman_material_with_variable["variable"] = [
            {"key": "p", "value": self.HARMLESS_PAYLOAD}
        ]
        return {
            "curl": curl_adapter.convert_curl_file(
                curl_material(spec), case_name="expression"
            ),
            "har": har_adapter.convert_har(
                har_material(spec), case_name="expression", assertion_mode="status"
            ),
            "postman": postman_adapter.convert_postman(
                postman_material_with_variable, case_name="expression", assertion_mode="status"
            )[0],
        }

    def test_expression_reaches_the_generated_case_in_every_source(self):
        """夹具自检：三种源都要**真的**把表达式带进产物（否则下面的判据是空洞的）。"""
        for source, case in self._cases_with_expression().items():
            with self.subTest(source=source):
                headers = request_dict(case).get("headers") or {}
                self.assertEqual(
                    headers.get("X-Expression"),
                    self.EXPRESSION,
                    f"{source} 没把表达式带进产物：{case.all_warnings()}",
                )

    def test_every_source_warns_about_source_expressions(self):
        """判据 1：素材里有 `${...}` → 每个源都必须告警（修复前一个字都不提示）。"""
        for source, case in self._cases_with_expression().items():
            with self.subTest(source=source):
                warnings = case.all_warnings()
                self.assertTrue(
                    any("表达式" in warning and "eval($p)" in warning for warning in warnings),
                    f"{source} 没有对素材里的 `${...}` 告警：{warnings}",
                )

    def test_no_source_output_can_execute_the_expression(self):
        """判据 2：把产物里的表达式丢回运行期插值 → 必须抛错（而不是执行）。

        NOTICE：`p` 这个变量在 Postman 源里是**素材自带**的（payload 也是），
        所以这里连变量都一起喂进去——即「素材把一切都准备好了」的最坏情况。
        """
        for source, case in self._cases_with_expression().items():
            with self.subTest(source=source):
                variables = config_dict(case).get("variables") or {}
                if source == "postman":
                    self.assertIn("p", variables, "夹具前提不成立：payload 变量没被保留")

                with self.assertRaises(FunctionNotFound) as ctx:
                    parser.parse_string(self.EXPRESSION, variables, {})

                message = str(ctx.exception)
                self.assertIn("eval", message)
                self.assertIn("whitelisted", message)  # 报错要能指导用户怎么改

    def test_guard_is_not_vacuous_escape_hatch_would_execute_it(self):
        """非空洞自检：打开逃生口后，**同一个表达式 + 同一份变量**必须真的执行（→ 42）。

        没有这一条，"第 2 条判据通过"有可能只是因为夹具本身跑不到那一步。
        """
        cases = self._cases_with_expression()
        variables = config_dict(cases["postman"]).get("variables") or {}

        with mock.patch.dict(os.environ, {parser.ALLOW_ALL_BUILTINS_ENV: "1"}, clear=False):
            self.assertEqual(parser.parse_string(self.EXPRESSION, variables, {}), 42)

    def test_expression_text_is_preserved_not_rewritten(self):
        """刻意的决定：导入器**不**改写表达式（只告警），所以产物里仍是原文。

        NOTICE：不能靠 `$$` 转义来中和——`$${eval($p)}` 只挡住外层的 `$`，
        里面的 `$p` 照样被变量替换（实测结果是 `${eval(PAYLOAD)}`，请求语义已经坏了）。
        因此这条链的收口点在运行期（内置白名单），这里把这个事实钉住。
        """
        # `$$` 只转义一个 `$`：这里刻意拼出 `$${eval($p)}`（= 转义 + `{eval($p)}`）
        escaped = "$$" + self.EXPRESSION.lstrip("$")
        self.assertEqual(escaped, "$${eval($p)}")
        self.assertEqual(parser.parse_string(escaped, {"p": "PAYLOAD"}, {}), "${eval(PAYLOAD)}")

        for source, case in self._cases_with_expression().items():
            with self.subTest(source=source):
                yaml_text = dump_case_yaml(case, f"{source}.yml")
                self.assertIn("eval($p)", yaml_text)


if __name__ == "__main__":
    unittest.main()
