"""P3-c：OpenAPI / Swagger → 骨架用例（导入器）的测试。

覆盖：`$ref`/`allOf`/`oneOf`/`nullable`、servers 变量、securitySchemes、参数（path/query/header/cookie）、
requestBody（example / 骨架 / 表单 / multipart / 不支持的类型）、responses（2xx 选择、无 content、
非 JSON、只声明 default）、tag 分组与过滤、Swagger 2.0 归一化、黄金文件，以及**端到端真跑**（本地 mock）。
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
from interfacetester.converters import emit_yaml, openapi_adapter
from interfacetester.make import pytest_files_made_cache_mapping, pytest_files_run_set
from interfacetester.utils import HTTP_BIN_URL

REPO = os.getcwd()
OPENAPI_ASSET = os.path.join("examples", "data", "openapi", "petstore_demo.yaml")
OPENAPI_ASSET_POSIX = "examples/data/openapi/petstore_demo.yaml"
GOLDEN_DIR = os.path.join("tests", "golden", "converters")


def _tmp_dir(prefix: str = "tmp_oa") -> str:
    path = os.path.join(REPO, "logs", f"{prefix}_{uuid.uuid4().hex[:8]}")
    os.makedirs(path)
    return path


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fp:
        return fp.read()


def _load_asset() -> dict:
    return yaml.safe_load(_read(OPENAPI_ASSET))


def _doc(paths, **extra) -> dict:
    document = {"openapi": "3.0.3", "info": {"title": "inline demo", "version": "1"}, "paths": paths}
    document.update(extra)
    return document


def _op(responses=None, **extra) -> dict:
    operation = {"responses": responses or {"200": {"description": "ok"}}}
    operation.update(extra)
    return operation


class TestBatch0918_8RefRequestBody(unittest.TestCase):
    """0918-8 / M20：**`$ref` 形态与内联形态的请求体必须一致**（跨层"语义对照"不变量）。

    NOTICE（修复前实测）：form/multipart 两个分支拿**未解析**的 `schema.get("properties")`
    去造值，而 `$ref: '#/components/schemas/X'` 形态下它是空的 —— 于是：
      - 每个字段都退化成字面量字符串 `"string"`（example/default/enum 全丢）；
      - `format: binary` 判不出来 → **multipart 变成 urlencoded**（请求语义直接错），`upload` 段消失。
    而 `$ref` 指向 `components/schemas` 恰恰是真实契约的主流写法。

    本类用「同一份 schema 写两遍（内联 vs `$ref`）」来钉住等价性——这正是 §六.2 里
    「导入器缺语义对照不变量」那条建议的落地。
    """

    FORM_SCHEMA = {
        "type": "object",
        "properties": {
            "file": {"type": "string", "format": "binary"},
            "note": {"type": "string", "example": "hello"},
            "size": {"type": "integer", "default": 42},
        },
    }

    def _convert(self, *, ref: bool, media_type: str):
        if media_type == "multipart/form-data":
            schema = (
                {"$ref": "#/components/schemas/UploadForm"}
                if ref
                else self.FORM_SCHEMA
            )
        else:
            schema = (
                {"$ref": "#/components/schemas/UploadForm"} if ref else self.FORM_SCHEMA
            )

        document = _doc(
            {
                "/upload": {
                    "post": _op(
                        requestBody={
                            "content": {media_type: {"schema": schema}}
                        }
                    )
                }
            }
        )
        if ref:
            document["components"] = {"schemas": {"UploadForm": self.FORM_SCHEMA}}

        return openapi_adapter.convert_openapi(document, case_name="refbody")[0].steps[0]

    def test_ref_multipart_equals_inline_multipart(self):
        inline = self._convert(ref=False, media_type="multipart/form-data")
        by_ref = self._convert(ref=True, media_type="multipart/form-data")

        self.assertEqual(by_ref.data, inline.data, "$ref 与内联的 data 不一致")
        self.assertEqual(by_ref.upload, inline.upload, "$ref 与内联的 upload 不一致")
        # 具体取值也钉住（否则两边一起错也能"相等"）
        self.assertEqual(inline.data, {"note": "hello", "size": 42})
        self.assertEqual(inline.upload, {"file": "file.bin"})

    def test_ref_form_equals_inline_form(self):
        inline = self._convert(ref=False, media_type="application/x-www-form-urlencoded")
        by_ref = self._convert(ref=True, media_type="application/x-www-form-urlencoded")

        self.assertEqual(by_ref.data, inline.data)
        self.assertEqual(inline.data["note"], "hello")
        self.assertEqual(inline.data["size"], 42)
        self.assertNotIn("string", [str(value) for value in inline.data.values()],
                         "字段退化成字面量 'string' 了")


class TestBatch0919_23Swagger2RefBodyParameter(unittest.TestCase):
    r"""批次 5 / H7：**`$ref` 引用的 `in: body` 参数**不能让请求体静默丢掉。

    ## 现场（修复前实测，真 `hconvert`）

    ```text
    Swagger 2.0 契约：
      parameters:                       # 顶层可复用参数段（规范内的常规写法）
        UserBody:
          in: body
          name: body
          schema: {type: object, properties: {password: {...}}}
      paths:
        /users:
          post:
            parameters:
              - $ref: '#/parameters/UserBody'      # ← 引用它

    生成物：method: POST + url: /users          ← grep 不到任何 json:/data:
            零告警                              ← 连"body 参数已转成 requestBody"都没有
    对照组（把同一个契约改成内联 in: body）：
            正常生成 json: + ${BODY_PASSWORD} 占位
    ```

    一个"一个 `$ref` 之差"的契约写法 → POST 变成**无请求体**。接口宽容时服务端会写入空对象。

    ## 根因：`$ref` 解析**时序**

    `in: body` → `requestBody` 的归一化循环直接读 `parameter.get("in")`，而那时
    `$ref` **还没解析**（`in` 为空）→ body 参数被当成"其它参数"塞回 `remaining_parameters`。
    两处前提必须同时成立才能修好：

    1. 分流前先 `resolve_ref`；
    2. `_rewrite_refs`（把 2.0 的 `#/parameters/X` 改成 3.0 的 `#/components/parameters/X`）
       必须**已经跑过** —— 原先它在函数**末尾**才执行，所以本批把它提前到分流之前。
    """

    BODY_SCHEMA = {
        "type": "object",
        "properties": {
            "username": {"type": "string", "example": "bob"},
            "password": {"type": "string", "example": "S3cr3t!"},
        },
    }

    def _swagger2(self, *, use_ref: bool, with_query_ref: bool = False) -> dict:
        """同一份 2.0 契约，`$ref` 版与内联版各生成一次（语义对照）。"""
        parameter = {
            "in": "body",
            "name": "body",
            "required": True,
            "schema": self.BODY_SCHEMA,
        }
        operation_parameters = []
        if use_ref:
            document_parameters = {"UserBody": parameter}
            operation_parameters.append({"$ref": "#/parameters/UserBody"})
        else:
            document_parameters = {}
            operation_parameters.append(parameter)

        if with_query_ref:
            page_param = {
                "in": "query",
                "name": "page",
                "type": "integer",
                "required": False,
                "example": 1,
            }
            if use_ref:
                document_parameters["PageParam"] = page_param
                operation_parameters.append({"$ref": "#/parameters/PageParam"})
            else:
                operation_parameters.append(page_param)

        document = {
            "swagger": "2.0",
            "info": {"title": "ref body demo", "version": "1"},
            "basePath": "/v1",
            "schemes": ["https"],
            "host": "api.example.com",
            "paths": {
                "/users": {
                    "post": {
                        "operationId": "createUser",
                        "tags": ["users"],
                        "parameters": operation_parameters,
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
        if document_parameters:
            document["parameters"] = document_parameters
        return document

    def _convert(self, **kwargs):
        case = openapi_adapter.convert_openapi(
            self._swagger2(**kwargs), case_name="swagger2ref", assertion_mode="status"
        )[0]
        return case, case.steps[0]

    # ------------------------------------------------------------------ 主体
    def test_ref_body_parameter_is_converted_to_a_request_body(self):
        """`$ref` 版必须和内联版**生成同一个请求体**（这条就是 H7 的现场）。"""
        _inline_case, inline_step = self._convert(use_ref=False)
        _ref_case, ref_step = self._convert(use_ref=True)

        self.assertIsNotNone(
            ref_step.json_body,
            "`$ref` 引用的 in: body 参数被静默丢掉了 —— 生成物里没有请求体",
        )
        self.assertEqual(
            ref_step.json_body,
            inline_step.json_body,
            "`$ref` 版与内联版的请求体不一致（同一条契约的两种等价写法）",
        )
        # 具体取值也钉住（否则"两边一起错"也能相等）
        # NOTICE: `password` 字段带 `example: "S3cr3t!"`，而它**命中凭据判定** → 生成物里
        # 已经是 `${BODY_PASSWORD}` 而不是字面量（我第一版按字面量断言，白红一次）。
        # 非敏感字段 `username` 才是"取值真的传下来了"的证据。
        self.assertEqual(inline_step.json_body["username"], "bob")
        self.assertEqual(
            ref_step.json_body["password"],
            "${BODY_PASSWORD}",
            "凭据仍要被占位化（脱敏链不能因为 `$ref` 而绕过）",
        )
        # 两个字段都要在（而不是整段被丢掉后"恰好相等"）
        self.assertEqual(set(ref_step.json_body), {"username", "password"})

    def test_ref_body_parameter_produces_the_conversion_warning(self):
        """对照信号：内联版会打的那条"body 参数已转成 requestBody"告警，`$ref` 版也要有。"""
        _inline_case, _inline_step = self._convert(use_ref=False)

        ref_case, _ref_step = self._convert(use_ref=True)

        warnings = "\n".join(ref_case.all_warnings())
        self.assertIn("body 参数已转成 requestBody", warnings)

    def test_ref_body_parameter_disappears_from_remaining_parameters(self):
        """body 参数不能被留在 `parameters` 里（否则会被当成 query/header 处理）。"""
        _case, step = self._convert(use_ref=True)

        self.assertNotIn("body", step.params or {})
        self.assertNotIn("body", step.headers or {})

    # -------------------------------------------------------- 反向护栏（时序）
    def test_ref_query_parameter_still_works(self):
        """反向护栏：**提前 `_rewrite_refs` 不能把 `$ref` 的 query 参数弄坏**。

        NOTICE: 本批把 `_rewrite_refs` 从函数末尾挪到了 parameters 分流之前 ——
        这是修 H7 的前提（否则 2.0 前缀的 `#/parameters/X` 解析不到）。
        这条用例保证那次搬动没有波及"其它位置的 `$ref` 参数"。
        """
        _inline_case, inline_step = self._convert(use_ref=False, with_query_ref=True)
        _ref_case, ref_step = self._convert(use_ref=True, with_query_ref=True)

        self.assertEqual(
            inline_step.params,
            {"page": 1},
            "前提不成立：内联版都没造出 query 参数值，这条对照会变成「空 == 空」",
        )
        self.assertEqual(
            ref_step.params, inline_step.params, "`$ref` 的 query 参数与内联版不一致"
        )
        self.assertIsNotNone(ref_step.json_body, "加了 query `$ref` 之后 body 又丢了")

    def test_ref_body_with_no_dangling_ref_warning(self):
        """不该出现"`$ref` 指向的节点不存在"的告警（时序改对之后必须能解析到）。"""
        ref_case, _step = self._convert(use_ref=True)

        warnings = "\n".join(ref_case.all_warnings())
        self.assertNotIn("指向的节点不存在", warnings)
        self.assertNotIn("不支持外部 `$ref`", warnings)

    # ------------------------------------------------- H5 同类根因（中文 tag）
    def test_cjk_tag_keeps_its_name_in_the_schema_file(self):
        """同批的 H5 同类根因：**中文 tag** 不能把 schema 词干退化成 `case`。

        修复前 `re.sub(r"[^0-9A-Za-z]+", "_", tag) or "case"` 会把中文 tag 一律变成 `case`
        → 多个中文 tag 的 schema 文件同名互相覆盖（与 Postman 侧同一个根因）。
        """
        document = self._swagger2(use_ref=False)
        document["paths"]["/users"]["post"]["tags"] = ["用户管理"]
        document["paths"]["/users"]["post"]["responses"] = {
            "200": {
                "description": "ok",
                "schema": {"type": "object", "properties": {"id": {"type": "integer"}}},
            }
        }

        case = openapi_adapter.convert_openapi(
            document, case_name="cjk", assertion_mode="status+schema"
        )[0]
        schema_file = case.steps[0].schema_file

        self.assertIsNotNone(schema_file)
        self.assertIn("用户管理", schema_file, f"中文 tag 的词干被抹掉了：{schema_file}")


class TestOpenApiAsset(unittest.TestCase):
    """仓库示例契约（刻意覆盖各种坑）逐项核对。"""

    def convert(self, **kwargs):
        kwargs.setdefault("case_name", "petstore_demo")
        return openapi_adapter.convert_openapi(_load_asset(), **kwargs)

    def test_splits_by_tag(self):
        cases = self.convert()

        names = [case.name for case in cases]
        self.assertEqual(names, ["petstore_demo / pets", "petstore_demo / store"])
        self.assertEqual(sum(len(case.steps) for case in cases), 6)

    def test_single_file_mode(self):
        cases = self.convert(split_tags=False)

        self.assertEqual(len(cases), 1)
        self.assertEqual(len(cases[0].steps), 6)

    def test_server_variable_becomes_base_url(self):
        cases = self.convert()

        self.assertEqual(cases[0].base_url, "https://api.petstore.test/v1")

    def test_required_query_params_use_default_and_enum(self):
        case = self.convert()[0]
        step = next(s for s in case.steps if s.name.startswith("GET /pets "))

        self.assertEqual(step.params, {"limit": 10, "status": "available"})  # default / enum[0]
        # 可选且无例子的参数被跳过并告警
        warnings = " ".join(step.warnings)
        self.assertIn("可选参数 tags(query)", warnings)
        self.assertIn("可选参数 X-Trace-Id(header)", warnings)

    def test_response_schema_becomes_jsonschema_assertion(self):
        case = self.convert()[0]
        step = next(s for s in case.steps if s.name.startswith("GET /pets "))

        self.assertEqual(step.assertions[0], {"eq": ["status_code", 200]})
        self.assertEqual(
            step.assertions[1], {"jsonschema_match": ["body", step.schema_file]}
        )
        self.assertIn(step.schema_file, case.extra_files)

    def test_ref_is_inlined_and_oneof_kept(self):
        case = self.convert()[0]
        step = next(s for s in case.steps if "查询单个宠物" in s.name)
        schema = case.extra_files[step.schema_file]

        self.assertIn("oneOf", schema)
        self.assertEqual(len(schema["oneOf"]), 2)
        # `$ref` 必须已内联（schema 是独立文件，指不回原契约）
        self.assertNotIn("$ref", json.dumps(schema))
        self.assertIn("id", schema["oneOf"][0]["properties"])

    def test_nullable_is_converted_for_json_schema(self):
        case = next(c for c in self.convert() if c.name.endswith("store"))
        schema = case.extra_files[case.steps[0].schema_file]

        self.assertEqual(schema["properties"]["status"]["type"], ["string", "null"])

    def test_path_parameter_example_is_used(self):
        case = self.convert()[0]
        step = next(s for s in case.steps if s.name.startswith("GET /pets/{petId}"))

        self.assertEqual(step.url, "/pets/${petId}")
        self.assertEqual(case.variables["petId"], "pet-001")

    def test_path_parameter_without_example_is_generated_and_warned(self):
        case = self.convert()[0]
        step = next(s for s in case.steps if s.name.startswith("DELETE /pets/{petId}"))

        # 覆盖了路径级参数（integer、无 example）→ 造值 0，并存进 config.variables
        variable = step.url.split("/pets/")[1]
        self.assertTrue(variable.startswith("${") and variable.endswith("}"))
        name = variable[2:-1]
        self.assertEqual(case.variables[name], 0)
        self.assertTrue(any("没有 example/default/enum" in w for w in step.warnings))
        # 204 无响应体 → 只断状态码
        self.assertEqual(step.assertions, [{"eq": ["status_code", 204]}])

    def test_request_body_example_is_used_verbatim(self):
        case = self.convert()[0]
        step = next(s for s in case.steps if s.name.startswith("POST /pets "))

        self.assertEqual(step.json_body, {"name": "旺财", "tag": "dog", "tags": ["cute", "small"]})

    def test_request_body_skeleton_when_no_example(self):
        case = self.convert()[0]
        step = next(s for s in case.steps if s.name.startswith("PATCH "))

        self.assertEqual(step.json_body, {"name": "string", "status": "available"})
        self.assertTrue(any("造骨架" in w for w in step.warnings))

    def test_form_body_becomes_data(self):
        case = next(c for c in self.convert() if c.name.endswith("store"))

        self.assertEqual(case.steps[0].data, {"petId": "pet-001", "quantity": 1})

    def test_security_global_and_operation_override(self):
        case = self.convert()[0]
        get_step = next(s for s in case.steps if s.name.startswith("GET /pets "))
        post_step = next(s for s in case.steps if s.name.startswith("POST /pets "))

        self.assertEqual(get_step.headers["Authorization"], "Bearer ${AUTH_TOKEN}")
        # 操作级 security 覆盖全局 → 只有 apiKey，没有 bearer
        self.assertNotIn("Authorization", post_step.headers)
        self.assertEqual(post_step.headers["X-API-Key"], "${API_KEY_X_API_KEY}")
        self.assertEqual(case.variables["AUTH_TOKEN"], "${ENV(AUTH_TOKEN)}")
        self.assertEqual(case.variables["API_KEY_X_API_KEY"], "${ENV(API_KEY_X_API_KEY)}")

    def test_no_secrets_in_output(self):
        cases = self.convert()
        emitted = "\n".join(
            emit_yaml.dump_case_yaml(case, "x.yml") for case in cases
        )
        self.assertIn("${AUTH_TOKEN}", emitted)
        self.assertNotIn("Bearer ", emitted.replace("Bearer ${AUTH_TOKEN}", ""))

    def test_status_only_mode(self):
        cases = self.convert(assertion_mode="status")

        for case in cases:
            self.assertEqual(case.extra_files, {})
            for step in case.steps:
                self.assertEqual(step.assertions[0]["eq"][0], "status_code")
                self.assertFalse(any("jsonschema_match" in json.dumps(a) for a in step.assertions))

    def test_tag_and_method_filters(self):
        only_store = self.convert(include_tags=["store"])
        self.assertEqual([c.name for c in only_store], ["petstore_demo / store"])

        without_store = self.convert(exclude_tags=["store"])
        self.assertEqual([c.name for c in without_store], ["petstore_demo / pets"])

        only_get = self.convert(include_methods=["get"])
        self.assertEqual(
            [s.method for s in only_get[0].steps], ["GET", "GET"]
        )

    def test_deprecated_operation_is_warned(self):
        document = _doc({"/old": {"get": _op(deprecated=True)}})
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertTrue(any("deprecated" in w for w in case.all_warnings()))


class TestSchemaHelpers(unittest.TestCase):
    """`$ref` / `allOf` / `oneOf` / `nullable` 的转换细节。"""

    def setUp(self):
        self.warnings = []

    def test_allof_is_merged(self):
        document = {
            "components": {
                "schemas": {
                    "Base": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}},
                    "Extended": {
                        "allOf": [
                            {"$ref": "#/components/schemas/Base"},
                            {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
                        ]
                    },
                }
            }
        }
        built = openapi_adapter.build_assertion_schema(
            {"$ref": "#/components/schemas/Extended"}, document, self.warnings
        )

        self.assertEqual(built["type"], "object")
        self.assertEqual(set(built["required"]), {"id", "name"})
        self.assertEqual(set(built["properties"]), {"id", "name"})
        self.assertNotIn("$ref", json.dumps(built))

    def test_external_ref_warns_and_keeps_node(self):
        built = openapi_adapter.build_assertion_schema(
            {"$ref": "./other.yaml#/Pet"}, {}, self.warnings
        )

        self.assertEqual(built, {})
        self.assertTrue(any("外部 `$ref`" in w for w in self.warnings))

    def test_missing_local_ref_warns(self):
        built = openapi_adapter.build_assertion_schema(
            {"$ref": "#/components/schemas/Nope"}, {"components": {"schemas": {}}}, self.warnings
        )

        self.assertEqual(built, {})
        self.assertTrue(any("不存在" in w for w in self.warnings))

    def test_sample_uses_example_then_default_then_enum_then_format(self):
        document = {}
        self.assertEqual(
            openapi_adapter.sample_from_schema({"type": "string", "example": "x"}, document), "x"
        )
        self.assertEqual(
            openapi_adapter.sample_from_schema({"type": "integer", "default": 3}, document), 3
        )
        self.assertEqual(
            openapi_adapter.sample_from_schema({"type": "string", "enum": ["a", "b"]}, document), "a"
        )
        self.assertEqual(
            openapi_adapter.sample_from_schema({"type": "string", "format": "uuid"}, document),
            "00000000-0000-0000-0000-000000000000",
        )
        self.assertEqual(openapi_adapter.sample_from_schema({"type": "integer"}, document), 0)
        self.assertEqual(openapi_adapter.sample_from_schema({"type": "boolean"}, document), False)

    def test_sample_for_nested_object_and_array(self):
        schema = {
            "type": "object",
            "properties": {
                "names": {"type": "array", "items": {"type": "string"}},
                "inner": {"type": "object", "properties": {"n": {"type": "integer"}}},
            },
        }
        self.assertEqual(
            openapi_adapter.sample_from_schema(schema, {}),
            {"names": ["string"], "inner": {"n": 0}},
        )

    def test_swagger2_normalization(self):
        document = {
            "swagger": "2.0",
            "info": {"title": "legacy", "version": "1"},
            "host": "api.legacy.test",
            "basePath": "/api",
            "schemes": ["https"],
            "securityDefinitions": {"basicAuth": {"type": "basic"}},
            "security": [{"basicAuth": []}],
            "paths": {
                "/users": {
                    "post": {
                        "parameters": [
                            {"name": "body", "in": "body", "required": True, "schema": {"$ref": "#/definitions/User"}},
                            {"name": "q", "in": "query", "type": "string", "default": "x"},
                        ],
                        "responses": {"200": {"description": "ok", "schema": {"$ref": "#/definitions/User"}}},
                    }
                }
            },
            "definitions": {
                "User": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
            },
        }
        cases = openapi_adapter.convert_openapi(document, case_name="legacy")
        case = cases[0]

        self.assertEqual(case.base_url, "https://api.legacy.test/api")
        step = case.steps[0]
        self.assertEqual(step.method, "POST")
        self.assertEqual(step.params, {"q": "x"})
        self.assertEqual(step.headers["Authorization"], "Basic ${AUTH_BASIC}")
        # body 参数 → requestBody → 无 example → 造骨架
        self.assertEqual(step.json_body, {"id": 0})
        # responses[].schema → content → 契约断言
        self.assertIn("jsonschema_match", json.dumps(step.assertions))
        warnings = " ".join(case.all_warnings())
        self.assertIn("Swagger 2.0", warnings)

    def test_swagger2_formdata_normalization(self):
        document = {
            "swagger": "2.0",
            "info": {"title": "f", "version": "1"},
            "host": "api.test",
            "paths": {
                "/submit": {
                    "post": {
                        "consumes": ["multipart/form-data"],
                        "parameters": [
                            {"name": "file", "in": "formData", "type": "file", "required": True},
                            {"name": "note", "in": "formData", "type": "string", "default": "hi"},
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
        case = openapi_adapter.convert_openapi(document)[0]
        step = case.steps[0]

        self.assertEqual(step.data.get("note"), "hi")
        self.assertIn("file", step.upload)


class TestBatch0918_9ContractExpressions(unittest.TestCase):
    """批次 9-0（M9）：契约里的 `example` 也可能是**表达式**，同样要告警。

    NOTICE：契约源的 `example`/`default` 常被当成"给人看的文字"，
    但它们会被写进生成的 YAML，运行期照样是表达式——所以四个源一视同仁。
    """

    def test_example_with_expression_warns(self):
        document = _doc(
            {
                "/x": {
                    "get": _op(
                        parameters=[
                            {
                                "name": "q",
                                "in": "query",
                                "schema": {"type": "string", "example": "${eval($p)}"},
                            }
                        ]
                    )
                }
            }
        )
        case = openapi_adapter.convert_openapi(document)[0]

        warnings = case.all_warnings()
        self.assertTrue(
            any("表达式" in warning and "eval($p)" in warning for warning in warnings),
            f"契约里的 `${...}` 没有被告警：{warnings}",
        )

    def test_contract_without_expression_is_not_noisy(self):
        """反向断言：正常契约（`${AUTH_TOKEN}` 这类**由导入器自己生成**的占位符）不该触发告警。

        NOTICE：判据必须扫**原始契约**而不是产物——产物里到处是导入器自己写的
        `${ENV(NAME)}` 占位符，扫产物会把正常用例全报一遍。
        """
        document = _doc(
            {"/x": {"get": _op()}},
            components={
                "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}
            },
            security=[{"bearer": []}],
        )
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertTrue(case.steps[0].headers)  # 确实生成了 Authorization 占位
        self.assertFalse(
            any("表达式" in warning for warning in case.all_warnings()),
            f"正常契约被误报：{case.all_warnings()}",
        )


class TestOpenApiEdgeCases(unittest.TestCase):
    """各种「契约写法不标准」的情况必须告警而不是静默出错。"""

    def test_no_servers_warns(self):
        case = openapi_adapter.convert_openapi(_doc({"/x": {"get": _op()}}))[0]

        self.assertEqual(case.base_url, "")
        self.assertTrue(any("没有 servers" in w for w in case.all_warnings()))

    def test_multiple_servers_warns(self):
        document = _doc(
            {"/x": {"get": _op()}},
            servers=[{"url": "https://a.test"}, {"url": "https://b.test"}],
        )
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertEqual(case.base_url, "https://a.test")
        self.assertTrue(any("servers" in w and "只取第一个" in w for w in case.all_warnings()))

    def test_server_variable_without_default(self):
        document = _doc(
            {"/x": {"get": _op()}},
            servers=[{"url": "https://{env}.test", "variables": {"env": {}}}],
        )
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertEqual(case.base_url, "https://{env}.test")
        self.assertTrue(any("没有 default" in w for w in case.all_warnings()))

    def test_default_only_responses_warn(self):
        document = _doc({"/x": {"get": _op({"default": {"description": "any"}})}})
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertEqual(case.steps[0].assertions, [])
        self.assertTrue(any("`default`" in w for w in case.all_warnings()))

    def test_no_2xx_warns_and_uses_smallest(self):
        document = _doc({"/x": {"get": _op({"400": {"description": "bad"}})}})
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertEqual(case.steps[0].assertions, [{"eq": ["status_code", 400]}])
        self.assertTrue(any("没有 2xx" in w for w in case.all_warnings()))

    def test_multiple_2xx_warns(self):
        document = _doc(
            {"/x": {"get": _op({"200": {"description": "a"}, "206": {"description": "b"}})}}
        )
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertEqual(case.steps[0].assertions[0], {"eq": ["status_code", 200]})
        self.assertTrue(any("多个 2xx" in w for w in case.all_warnings()))

    def test_non_json_response_content_warns(self):
        document = _doc(
            {"/x": {"get": _op({"200": {"description": "ok", "content": {"text/plain": {"schema": {"type": "string"}}}}})}}
        )
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertEqual(len(case.steps[0].assertions), 1)
        self.assertTrue(any("非 JSON" in w for w in case.all_warnings()))

    def test_unsupported_request_body_content_type_warns(self):
        document = _doc(
            {
                "/x": {
                    "post": _op(
                        requestBody={
                            "content": {"application/xml": {"schema": {"type": "object"}}}
                        }
                    )
                }
            }
        )
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertIsNone(case.steps[0].json_body)
        self.assertTrue(any("未支持" in w for w in case.all_warnings()))

    def test_oauth2_security_warns_with_config_hint(self):
        document = _doc(
            {"/x": {"get": _op()}},
            components={"securitySchemes": {"oauth": {"type": "oauth2"}}},
            security=[{"oauth": []}],
        )
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertTrue(any("config.oauth2" in w for w in case.all_warnings()))

    def test_multiple_security_requirements_warn(self):
        document = _doc(
            {"/x": {"get": _op()}},
            components={"securitySchemes": {"a": {"type": "http", "scheme": "bearer"}, "b": {"type": "apiKey", "name": "K", "in": "header"}}},
            security=[{"a": []}, {"b": []}],
        )
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertTrue(any("可选认证" in w for w in case.all_warnings()))

    def test_cookie_parameter(self):
        """cookie 参数属于凭据 → 必须占位化。

        NOTICE（0918-2）：修复前这里断言的是 `{"sid": "abc"}` —— 即**把明文写进 YAML
        当成正确行为**（`abc` 是契约里的 example 值）。cookie 会随请求发送，
        OpenAPI 又是四个源里唯一"一次 sanitize 都没调用"的那条链路，
        所以这条断言实际上把泄漏锁死了。现在改为断言占位化结果，
        对抗性覆盖见 `tests/sensitive_data_leak_test.py`。
        """
        document = _doc(
            {
                "/x": {
                    "get": _op(
                        parameters=[
                            {"name": "sid", "in": "cookie", "required": True, "schema": {"type": "string", "example": "abc"}}
                        ]
                    )
                }
            }
        )
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertEqual(case.steps[0].cookies, {"sid": "${COOKIE_SID}"})
        self.assertEqual(case.variables["COOKIE_SID"], "${ENV(COOKIE_SID)}")

    def test_multipart_request_body_maps_binary_to_upload(self):
        document = _doc(
            {
                "/upload": {
                    "post": _op(
                        requestBody={
                            "content": {
                                "multipart/form-data": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "file": {"type": "string", "format": "binary"},
                                            "note": {"type": "string", "default": "hi"},
                                        },
                                    }
                                }
                            }
                        }
                    )
                }
            }
        )
        case = openapi_adapter.convert_openapi(document)[0]
        step = case.steps[0]

        self.assertEqual(step.upload, {"file": "file.bin"})
        self.assertEqual(step.data, {"note": "hi"})

    def test_not_a_contract_raises(self):
        tmp = _tmp_dir()
        try:
            path = os.path.join(tmp, "na.json")
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({"foo": 1}, fp)
            with self.assertRaises(ValueError) as ctx:
                openapi_adapter.convert_openapi_file(path)
            self.assertIn("OpenAPI", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_paths_warns(self):
        case = openapi_adapter.convert_openapi(_doc({}))[0]

        self.assertEqual(case.steps, [])
        self.assertTrue(any("没有 paths" in w for w in case.warnings))


class TestOpenApiGoldenAndE2E(unittest.TestCase):
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
        cases = openapi_adapter.convert_openapi(_load_asset(), case_name="petstore_demo")
        actual = {
            emit_yaml.slugify(case.name) + ".yml": emit_yaml.dump_case_yaml(
                case, emit_yaml.slugify(case.name) + ".yml"
            )
            for case in cases
        }
        # 契约断言用的 schema 也纳入快照（防止 $ref 内联/nullable 转换静默变化）
        for case in cases:
            for relative_path, content in case.extra_files.items():
                actual[relative_path] = json.dumps(content, ensure_ascii=False, indent=2) + "\n"

        golden_path = os.path.join(GOLDEN_DIR, "openapi_petstore.yml")
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
        """契约指向本地 mock，响应用 mocked 契约断言 → 生成 → 真跑通。"""
        document = {
            "openapi": "3.0.3",
            "info": {"title": "local mock", "version": "1"},
            "servers": [{"url": HTTP_BIN_URL}],
            "paths": {
                "/get": {
                    "get": {
                        "tags": ["smoke"],
                        "summary": "echo",
                        "parameters": [
                            {
                                "name": "q",
                                "in": "query",
                                "required": True,
                                "schema": {"type": "string", "example": "hello"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "required": ["args", "url"],
                                            "properties": {
                                                "args": {"type": "object"},
                                                "url": {"type": "string"},
                                            },
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
        }
        contract_path = os.path.join(self.tmp_dir, "local_mock.openapi.yaml")
        with open(contract_path, "w", encoding="utf-8") as fp:
            yaml.safe_dump(document, fp, allow_unicode=True)

        out_dir = os.path.join(self.tmp_dir, "out")
        code = self._run_cli(
            "--from", "openapi", "--in", contract_path, "--out", out_dir, "--name", "oa_smoke"
        )
        self.assertEqual(code, 0)

        yaml_path = os.path.join(out_dir, "oa_smoke_smoke.yml")
        self.assertTrue(os.path.isfile(yaml_path))
        generated = yaml.safe_load(_read(yaml_path))
        self.assertEqual(generated["config"]["base_url"], HTTP_BIN_URL)
        self.assertEqual(generated["teststeps"][0]["request"]["params"], {"q": "hello"})
        self.assertIn("jsonschema_match", json.dumps(generated["teststeps"][0]["validate"]))

        exit_code = main_run([yaml_path, "--import-mode=importlib"])
        self.assertEqual(exit_code, 0)

    def test_cli_report_written(self):
        out_dir = os.path.join(self.tmp_dir, "out")
        report_path = os.path.join(self.tmp_dir, "report.md")

        code = self._run_cli(
            "--from", "openapi", "--in", OPENAPI_ASSET, "--out", out_dir, "--report", report_path
        )
        self.assertEqual(code, 0)
        report = _read(report_path)
        self.assertIn("# 导入报告", report)
        self.assertIn("`openapi`", report)  # 报告头部写明源格式
        self.assertIn("需要人工确认的点", report)
        self.assertIn("petstore_demo / pets", report)  # 每个用例一节告警

    def test_generated_schema_files_are_valid_json(self):
        cases = openapi_adapter.convert_openapi(_load_asset(), case_name="petstore_demo")
        out_dir = os.path.join(self.tmp_dir, "out_schemas")

        written = [emit_yaml.emit_case(case, out_dir) for case in cases]
        files = [path for item in written for path in item["extra"].values()]
        self.assertTrue(files, "至少应生成一个 schema 文件")
        for path in files:
            json.loads(_read(path))


class TestBatch0919_2Swagger2Consumes(unittest.TestCase):
    """0919-2 / 缺陷 6：Swagger 2.0 的 `consumes` 必须从 **Operation/顶层** 取。

    ## 修复前的现场

    ```python
    if body_parameter:
        content_type = (body_parameter.get("consumes") or consumes_default)[0]   # ← 恒为 None
    elif form_parameters:
        content_type = str((operation.get("consumes") or consumes_default)[0])   # ← 这个是对的
    ```

    按 Swagger 2.0 规范，`consumes` 是 **Operation 对象**或**顶层 Swagger 对象**的字段，
    `in: body` 的 parameter 上**没有**它 —— 所以 `body_parameter.get("consumes")` 恒为 `None`，
    operation 级声明的 `application/xml` 被整个忽略，接口被错标成 JSON。

    NOTICE: 这条缺陷的形态不是「写错一个键」，而是**同一个函数里两套口径**——
    formData 分支写的是 `operation.get("consumes")`，body 分支写的是 parameter 上的。
    所以修法不是把 L445 改成 `operation.get(...)` 就完事，而是让两个分支**共用同一个取用函数**
    （`openapi_adapter._first_media_type`），取用顺序只此一份。
    """

    def _swagger_doc(self, operation, **extra):
        document = {
            "swagger": "2.0",
            "info": {"title": "legacy", "version": "1"},
            "host": "api.legacy.test",
            "paths": {"/x": {"post": operation}},
        }
        document.update(extra)
        return document

    def _body_operation(self, consumes=None, **extra):
        operation = {
            "parameters": [
                {
                    "name": "body",
                    "in": "body",
                    "schema": {
                        "type": "object",
                        "properties": {"a": {"type": "string", "default": "v"}},
                    },
                }
            ],
            "responses": {"200": {"description": "ok"}},
        }
        if consumes is not None:
            operation["consumes"] = consumes
        operation.update(extra)
        return operation

    def _normalized_content(self, document):
        warnings = []
        normalized = openapi_adapter.normalize_swagger2(document, warnings)
        return normalized["paths"]["/x"]["post"]["requestBody"]["content"], warnings

    def test_body_parameter_takes_operation_level_consumes(self):
        """缺陷 6 的核心现场：operation 级 `consumes` 必须生效。"""
        content, _warnings = self._normalized_content(
            self._swagger_doc(self._body_operation(consumes=["application/xml"]))
        )

        self.assertEqual(list(content), ["application/xml"])

    def test_body_and_formdata_branches_agree(self):
        """收口验证：同一份 operation 声明下，body 与 formData 分支取到的 media type 必须相同。"""
        document = self._swagger_doc(
            self._body_operation(consumes=["application/xml"]),
        )
        body_content, _ = self._normalized_content(document)

        form_document = self._swagger_doc(
            {
                "consumes": ["application/xml"],
                "parameters": [{"name": "f", "in": "formData", "type": "string"}],
                "responses": {"200": {"description": "ok"}},
            }
        )
        form_content, _ = self._normalized_content(form_document)

        self.assertEqual(list(body_content), list(form_content))
        self.assertEqual(list(body_content), ["application/xml"])

    def test_top_level_consumes_is_used_when_operation_is_silent(self):
        """兜底链不能断：operation 没声明时用顶层的（这是既有能力，别修丢）。"""
        document = self._swagger_doc(
            self._body_operation(),  # 不带 consumes
            consumes=["application/xml"],
        )
        content, _ = self._normalized_content(document)

        self.assertEqual(list(content), ["application/xml"])

    def test_default_is_json_when_nothing_declares_it(self):
        """顶层也没声明时回落 JSON（`DEFAULT_MEDIA_TYPE` 的唯一口径）。"""
        content, _ = self._normalized_content(self._swagger_doc(self._body_operation()))

        self.assertEqual(list(content), ["application/json"])

    def test_operation_level_overrides_top_level(self):
        """operation 级优先于顶层（Swagger 2.0 的覆盖语义）。"""
        document = self._swagger_doc(
            self._body_operation(consumes=["application/xml"]),
            consumes=["application/json"],
        )
        content, _ = self._normalized_content(document)

        self.assertEqual(list(content), ["application/xml"])

    def test_xml_body_is_not_silently_mislabeled_as_json(self):
        """端到端：XML 接口不再被静默错标成 JSON（生成物 + 明确告警）。

        修复前这里会生成一个 JSON 体（`application/json` → 段落骨架），
        而真实接口收的是 XML —— 生成的用例发出去的报文形态是错的。
        """
        document = self._swagger_doc(self._body_operation(consumes=["application/xml"]))
        case = openapi_adapter.convert_openapi(document)[0]
        step = case.steps[0]

        self.assertIsNone(step.json_body)
        self.assertIsNone(step.data)
        warnings = " ".join(case.all_warnings())
        self.assertIn("application/xml", warnings)
        self.assertIn("未支持", warnings)

    def test_json_body_case_is_unchanged(self):
        """反向护栏：最常见的 `consumes: [application/json]` 行为零变化。"""
        document = self._swagger_doc(
            self._body_operation(consumes=["application/json"])
        )
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertEqual(case.steps[0].json_body, {"a": "v"})

    def test_formdata_case_is_unchanged(self):
        """反向护栏：formData 分支（修复前就对的那一半）行为零变化。"""
        document = self._swagger_doc(
            {
                "consumes": ["application/x-www-form-urlencoded"],
                "parameters": [
                    {"name": "n", "in": "formData", "type": "string", "default": "hi"}
                ],
                "responses": {"200": {"description": "ok"}},
            }
        )
        case = openapi_adapter.convert_openapi(document)[0]

        self.assertEqual(case.steps[0].data, {"n": "hi"})

    def test_string_consumes_is_warned_not_silently_truncated(self):
        """顺带收口：`consumes` 写成字符串（不合规范）时，不得静默取到首字符。

        修复前的 `[0]` 会让 `consumes: "application/xml"` 变成 media type `"a"` ——
        生成物里带一个荒谬的 content-type，且**零告警**。
        """
        content, warnings = self._normalized_content(
            self._swagger_doc(self._body_operation(consumes="application/xml"))
        )

        self.assertEqual(list(content), ["application/xml"])
        self.assertTrue(
            any("consumes" in w and "字符串" in w for w in warnings), warnings
        )

    def test_string_produces_is_warned_not_silently_truncated(self):
        """同族字段 `produces` 一起收口（取用顺序本来就对，只有字符串形态问题）。"""
        document = self._swagger_doc(
            self._body_operation(
                consumes=["application/json"],
                produces="application/xml",
                responses={"200": {"description": "ok", "schema": {"type": "object"}}},
            )
        )
        warnings = []
        normalized = openapi_adapter.normalize_swagger2(document, warnings)

        response = normalized["paths"]["/x"]["post"]["responses"]["200"]
        self.assertEqual(list(response["content"]), ["application/xml"])
        self.assertTrue(
            any("produces" in w and "字符串" in w for w in warnings), warnings
        )

    def test_empty_consumes_list_falls_back(self):
        """反向护栏：空数组是"没声明"，不是"取不到就崩"。"""
        content, _ = self._normalized_content(
            self._swagger_doc(self._body_operation(consumes=[]))
        )

        self.assertEqual(list(content), ["application/json"])

    def test_unrecognized_consumes_shape_warns_instead_of_crashing(self):
        """形态无法识别时**不崩也不静默**：走默认值 + 明确告警。

        NOTICE: 修复前这里会 `declared[0]` → `KeyError: 0` **响亮崩掉**。
        新实现选择不崩，那就必须留痕 —— 把「响亮崩溃」换成「静默默认」
        是本批明确要消灭的方向。
        """
        content, warnings = self._normalized_content(
            self._swagger_doc(self._body_operation(consumes={"mediaType": "application/xml"}))
        )

        self.assertEqual(list(content), ["application/json"])
        self.assertTrue(
            any("形态无法识别" in w and "consumes" in w for w in warnings), warnings
        )


class TestBatch0920QueryParameterStyle(unittest.TestCase):
    """0920 批次 3 / **N19**：查询参数必须按契约的 `style` / `explode` 序列化。

    ## 修复前的现场（`.tmp_audit/n19_check.py`，真 `requests` 编码）

    ```text
    ids    (style=form, explode=false, example=[1,2,3])  契约 ids=1,2,3   实发 ids=1&ids=2&ids=3
    filter (style=deepObject,        example={a: 1})      契约 filter[a]=1  实发 filter=a（值 1 被吞）
    ```

    修复前 `style`/`explode` 在整个文件里 **grep 零命中** —— 例值被原样塞进
    `step.params[name]`，再由 requests 用默认规则编码。这与本模块 docstring 的承诺
    （"不能确定的造值**并告警**，绝不静默产出语义错的用例"）直接冲突：
    契约用例发出的查询串跟契约不是一回事，却仍被当成契约用例。
    """

    def _convert_query_params(self, parameters):
        document = _doc({"/items": {"get": _op(parameters=parameters)}})
        case = openapi_adapter.convert_openapi(document, case_name="style demo")[0]
        return case.steps[0].params, "\n".join(case.all_warnings())

    def test_form_explode_false_array_is_comma_joined(self):
        """`style=form, explode=false` → 逗号连接（契约语义，`params` 能表达）。"""
        params, _warnings = self._convert_query_params(
            [
                {
                    "name": "ids",
                    "in": "query",
                    "style": "form",
                    "explode": False,
                    "schema": {"type": "array", "items": {"type": "integer"}},
                    "example": [1, 2, 3],
                }
            ]
        )

        self.assertEqual(params, {"ids": "1,2,3"})

    def test_deep_object_is_expanded_into_bracketed_keys(self):
        """`style=deepObject` → `filter[a]=1`（修复前值会被整个吞掉）。"""
        params, _warnings = self._convert_query_params(
            [
                {
                    "name": "filter",
                    "in": "query",
                    "style": "deepObject",
                    "explode": True,
                    "schema": {"type": "object"},
                    "example": {"a": 1, "b": 2},
                }
            ]
        )

        self.assertEqual(params, {"filter[a]": 1, "filter[b]": 2})

    def test_pipe_delimited_array(self):
        params, _warnings = self._convert_query_params(
            [
                {
                    "name": "tags",
                    "in": "query",
                    "style": "pipeDelimited",
                    "explode": False,
                    "schema": {"type": "array", "items": {"type": "string"}},
                    "example": ["x", "y"],
                }
            ]
        )

        self.assertEqual(params, {"tags": "x|y"})

    def test_form_explode_true_array_warns_about_duplicate_keys(self):
        """`explode=true` 的数组契约语义是重复键 —— 字典装不下，必须**告警**。"""
        params, warnings = self._convert_query_params(
            [
                {
                    "name": "ids",
                    "in": "query",
                    "style": "form",
                    "explode": True,
                    "schema": {"type": "array", "items": {"type": "integer"}},
                    "example": [1, 2, 3],
                }
            ]
        )

        self.assertIn("ids", params)
        self.assertIn("重复键", warnings, warnings)
        self.assertIn("ids=1&ids=2", warnings, "告警要写清契约的真实语义")

    def test_scalar_is_unaffected_by_style(self):
        """反向护栏：标量值不受 `style`/`explode` 影响（不许误伤）。"""
        for style in ("form", "simple"):
            with self.subTest(style=style):
                params, _warnings = self._convert_query_params(
                    [
                        {
                            "name": "q",
                            "in": "query",
                            "style": style,
                            "explode": False,
                            "schema": {"type": "string"},
                            "example": "hello",
                        }
                    ]
                )
                self.assertEqual(params, {"q": "hello"})

