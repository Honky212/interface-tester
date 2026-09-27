"""OpenAPI / Swagger → IR 适配器（P3-c）：**契约 → 骨架用例**。

产物形态：一个 tag 一个用例文件；每个 operation 一个步骤；
断言 = **状态码 + 响应 JSON Schema**（走 P2-b 的 `jsonschema_match` 文件引用）。

**支持子集**（详见 `docs/convert/README.md`）
--------------------------------------------
| 契约里的东西 | 处理 |
| --- | --- |
| `openapi: 3.x` | 完整走下面的规则 |
| `swagger: "2.0"` | 先**归一化**成 3.0 形状（`host`+`basePath`+`schemes` → server、`definitions` → schemas、`parameters(in: body)` → requestBody、`responses[].schema` → content、`securityDefinitions` → securitySchemes），归一化中的损失会告警 |
| `servers[].url` + `variables[].default` | → `config.base_url`（变量用 default 代入；数组 `servers` 取第一个并告警） |
| `$ref`（本地 `#/components/...`） | **解析并内联**（生成的 schema 文件里不能再指回原文档）；**外部文件 `$ref` 不支持** → 告警并保留原样 |
| `allOf` | 合并（属性取并集，`required` 取并集） |
| `oneOf` / `anyOf` | 保留为 `oneOf`/`anyOf`（JSON Schema 直接支持） |
| `nullable: true`（3.0 写法，**不是** JSON Schema 关键字） | 转成 `type: [T, "null"]`，否则断言语义会偏严 |
| `security` + `components.securitySchemes` | `http/bearer` → `Bearer ${AUTH_TOKEN}`；`http/basic` → `Basic ${AUTH_BASIC}`；`apiKey` → 头/查询占位；`oauth2`/`openIdConnect` → 告警并指向内置 `config.oauth2` |
| `parameters`（path/query/header/cookie） | `required` 的必带；可选的仅当有 `example`/`default`/`enum` 时带上，其余**汇总告警**（不塞一堆无意义参数） |
| 路径参数 `{petId}` | 有 `example`/`default`/`enum` 用它；否则**按类型造值**并告警；值放进 `config.variables`（便于统一改） |
| `requestBody`（application/json） | 有 `example`/`examples` 直接用；否则按 schema **造骨架**并告警；`x-www-form-urlencoded` → `data`；`multipart/form-data` → `data`+`upload`；其它 content-type 告警 |
| `responses` | 取**最小的 2xx**（没有则取最小数字码）→ `eq [status_code, N]`；有 `application/json` schema → 生成 schema 文件 + `jsonschema_match`；无 content（如 204）→ 只断状态码 |
| `deprecated: true` | 仍然导入，但告警 |

设计取向与 curl/HAR/Postman 一致：**能确定的照搬，不能确定的造一个显式可改的值并告警**，
绝不静默产出「看起来能跑但语义是错的」用例。
"""

import copy
import json
import os
import re
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Set, Text, Tuple

from interfacetester.converters.har_adapter import infer_schema
from interfacetester.converters.ir import (
    IRCase,
    IRStep,
    merge_variables,
    safe_dict_entries,
    safe_file_stem,
    sanitize_cases,
    split_url,
    warn_source_expressions,
)

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

# `$ref` 里允许的本地前缀
LOCAL_REF_PREFIX = "#/"

# 造骨架值时的默认值（按 JSON Schema type/format）
FORMAT_SAMPLES: Dict[Text, Any] = {
    "date-time": "2024-01-01T00:00:00Z",
    "date": "2024-01-01",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "email": "user@example.com",
    "uri": "https://example.com/callback",
    "hostname": "example.com",
    "ipv4": "127.0.0.1",
    "password": "${ENV(DEMO_PASSWORD)}",
    "byte": "ZXhhbXBsZQ==",
    "binary": "example.bin",
}
TYPE_SAMPLES: Dict[Text, Any] = {
    "string": "string",
    "integer": 0,
    "number": 0,
    "boolean": False,
    "object": {},
    "array": [],
}

# schema 内联/展开的上限（防止巨型契约产出不可读的用例）
MAX_SCHEMA_DEPTH = 8
MAX_PROPERTIES = 60

# Swagger 2.0 未声明 `consumes`/`produces` 时的默认媒体类型（唯一口径，别处不要再写字面量）
DEFAULT_MEDIA_TYPE = "application/json"


def _warn(warnings: List[Text], message: Text) -> None:
    if message not in warnings:
        warnings.append(message)


# --------------------------------------------------------------------------- $ref 解析
def resolve_ref(node: Any, document: Dict[str, Any], warnings: List[Text], depth: int = 0) -> Any:
    """解析本地 `$ref`（`#/a/b/c`）；外部引用或找不到时告警并原样返回。"""
    if depth > 20 or not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node

    if not ref.startswith(LOCAL_REF_PREFIX):
        _warn(
            warnings,
            f"不支持外部 `$ref`（{ref}）→ 已原样保留，请在生成的用例里手工替换"
            "（或把被引用文件合并进同一份契约）",
        )
        return node

    target: Any = document
    for part in ref[len(LOCAL_REF_PREFIX) :].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict) and part in target:
            target = target[part]
        else:
            _warn(warnings, f"`$ref` 指向的节点不存在：{ref} → 已原样保留")
            return node
    return target


def _merge_all_of(
    schema: Dict[str, Any],
    document: Dict[str, Any],
    warnings: List[Text],
    depth: int = 0,
) -> Dict[str, Any]:
    """把 `allOf` 合并成一个 object schema（属性并集 + required 并集）。

    NOTICE（批次 E / **L4**）：`$ref` 的循环有 `resolve_ref` 的 `depth > 20` 兜底，
    但 `allOf` 的**递归**（第 129 行那句 `_merge_all_of(resolved, ...)`）**没有** ——
    实测 `{"allOf": [{"$ref": "#/components/schemas/Self"}]}` 这种**自引用**会一路递归到
    `RecursionError: maximum recursion depth exceeded`（裸栈溢出，报错里看不出是哪份契约、
    哪个 schema）。现在补上同一口径的深度上限：超限就**停止下钻并告警**，
    与 `build_assertion_schema` 的「截断必须可见」完全一致。
    """
    branches = schema.get("allOf")
    if not isinstance(branches, list):
        return schema

    if depth > MAX_SCHEMA_DEPTH:
        _warn(
            warnings,
            f"`allOf` 合并超过 {MAX_SCHEMA_DEPTH} 层（很可能是**自引用**的 allOf）→ "
            f"已停止下钻，该层的断言可能比契约更宽松。请检查契约里的 `allOf` 是否成环。",
        )
        return {key: value for key, value in schema.items() if key != "allOf"}

    merged: Dict[str, Any] = {key: value for key, value in schema.items() if key != "allOf"}
    properties: Dict[Text, Any] = dict(merged.get("properties") or {})
    required: List[Text] = list(merged.get("required") or [])

    for branch in branches:
        resolved = resolve_ref(branch, document, warnings)
        if not isinstance(resolved, dict):
            continue
        resolved = _merge_all_of(resolved, document, warnings, depth + 1)
        properties.update(resolved.get("properties") or {})
        for name in resolved.get("required") or []:
            if name not in required:
                required.append(name)
        if resolved.get("type") == "object" and "type" not in merged:
            merged["type"] = "object"

    if properties:
        merged["properties"] = properties
    if required:
        merged["required"] = required
    return merged


def build_assertion_schema(
    schema: Any, document: Dict[str, Any], warnings: List[Text], depth: int = 0
) -> Any:
    """把契约里的 schema 变成**自包含**的 JSON Schema（供 `jsonschema_match` 用）。

    NOTICE:
    - 必须内联 `$ref`：schema 会被写成**独立文件**，`#/components/...` 指不回原契约；
    - 必须处理 OpenAPI 3.0 的 `nullable`：它不是 JSON Schema 关键字，
      不转换会让断言比契约更严（`nullable: true` 的字段实际允许 null）。

    NOTICE（批次 9-1 / 截断必须可见）：修复前下面两处 `return {}` 是**完全静默**的，
    而空 schema 在 JSON Schema 里等价于「任意值」—— 也就是**断言比契约更宽松**：
    嵌套超过 `MAX_SCHEMA_DEPTH` 层的那部分，用例会「断过了」而实际上什么都没断。
    这是本仓最忌讳的一类**假通过**，所以现在两处都发告警（与 `MAX_PROPERTIES`
    截断处同一口径），由 `_warn` 汇总进 `--report` 与 stdout。
    """
    if depth > MAX_SCHEMA_DEPTH:
        # 消息刻意**不带 depth 数字**：`_warn` 按整串去重，带上数字会让一层一条
        # （深层 schema 会刷出十几行同样含义的告警）。
        _warn(
            warnings,
            f"schema 嵌套超过 {MAX_SCHEMA_DEPTH} 层，超出部分已**截断**"
            f"（该处的断言退化成「任意值」，比契约更宽松）。"
            f"需要完整校验请手工补写那一层的 schema 文件。",
        )
        return {}

    resolved = resolve_ref(schema, document, warnings, depth)
    if not isinstance(resolved, dict):
        # 例：`schema: true`（JSON Schema 布尔型）、`schema: [..]`、`$ref` 解析失败后拿到 None
        _warn(
            warnings,
            f"schema 的形态不是对象（{type(resolved).__name__}）→ 该层**没有生成断言**"
            f"（等价于「任意值」）。契约本身可能用了布尔型 schema 或引用了无法解析的 `$ref`。",
        )
        return {}

    resolved = _merge_all_of(resolved, document, warnings)
    result: Dict[str, Any] = {}

    # nullable → type 联合
    nullable = resolved.get("nullable") is True
    schema_type = resolved.get("type")
    if nullable and isinstance(schema_type, str):
        result["type"] = [schema_type, "null"]
    elif schema_type is not None:
        result["type"] = schema_type

    for keyword in ("enum", "const", "format", "pattern", "minimum", "maximum",
                    "minLength", "maxLength", "minItems", "maxItems", "minProperties",
                    "maxProperties", "uniqueItems", "multipleOf", "title", "description", "default"):
        if keyword in resolved:
            result[keyword] = resolved[keyword]

    if "required" in resolved:
        result["required"] = list(resolved["required"])

    properties = resolved.get("properties")
    if isinstance(properties, dict):
        trimmed = list(properties.items())[:MAX_PROPERTIES]
        result["properties"] = {
            name: build_assertion_schema(value, document, warnings, depth + 1)
            for name, value in trimmed
        }
        if len(properties) > MAX_PROPERTIES:
            _warn(
                warnings,
                f"schema 属性过多（{len(properties)} 个），断言只取前 {MAX_PROPERTIES} 个属性",
            )

    for keyword in ("items", "additionalProperties", "not"):
        if keyword in resolved:
            value = resolved[keyword]
            if isinstance(value, (dict, list)):
                result[keyword] = build_assertion_schema(value, document, warnings, depth + 1)
            else:
                result[keyword] = value

    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = resolved.get(keyword)
        if isinstance(branches, list):
            result[keyword] = [
                build_assertion_schema(branch, document, warnings, depth + 1)
                for branch in branches
            ]

    return result


# --------------------------------------------------------------------------- 造值
def sample_from_schema(
    schema: Any,
    document: Dict[str, Any],
    name: Text = "",
    depth: int = 0,
    warnings: Optional[List[Text]] = None,
) -> Any:
    """按 schema **造一个骨架值**（example/default/enum 优先，其次按 type/format）。

    NOTICE（批次 E / **L3**）：下面两处原来写的是 `warnings or []` —— 空列表是 **falsy**，
    于是调用方传进来的那个**空列表**会被换成一个**临时列表**，里面追加的告警全部丢失。
    后果是「`$ref` 解析不到」这类告警**是否出现，取决于该步骤此前有没有别的告警**：
    前面已经攒了一条告警的步骤会报，干净的步骤反而不报 —— 同一份素材两次运行/两种顺序
    得到的告警不一样。现在统一用 `is not None` 判空。
    """
    if warnings is None:
        warnings = []
    resolved = resolve_ref(schema, document, warnings, depth)
    if not isinstance(resolved, dict):
        return "string"

    for key in ("example", "default"):
        if key in resolved and resolved[key] is not None:
            return resolved[key]

    enum_values = resolved.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]

    for keyword in ("oneOf", "anyOf"):
        branches = resolved.get(keyword)
        if isinstance(branches, list) and branches:
            return sample_from_schema(branches[0], document, name, depth + 1, warnings)

    resolved = _merge_all_of(resolved, document, warnings)
    schema_type = resolved.get("type")
    if isinstance(schema_type, list):  # `type: [string, "null"]`
        schema_type = next((item for item in schema_type if item != "null"), "string")

    if schema_type is None and isinstance(resolved.get("properties"), dict):
        schema_type = "object"

    if schema_type == "object" or isinstance(resolved.get("properties"), dict):
        sample: Dict[Text, Any] = {}
        for prop_name, prop_schema in list((resolved.get("properties") or {}).items())[:MAX_PROPERTIES]:
            if depth >= MAX_SCHEMA_DEPTH:
                break
            sample[prop_name] = sample_from_schema(
                prop_schema, document, prop_name, depth + 1, warnings
            )
        return sample

    if schema_type == "array":
        items = resolved.get("items")
        if isinstance(items, (dict, list)) and depth < MAX_SCHEMA_DEPTH:
            return [sample_from_schema(items, document, name, depth + 1, warnings)]
        return []

    fmt = resolved.get("format")
    if fmt in FORMAT_SAMPLES:
        return FORMAT_SAMPLES[fmt]

    return TYPE_SAMPLES.get(str(schema_type), "string")


def _serialize_query_param(
    name: Text,
    value: Any,
    parameter: Dict[str, Any],
    warnings: List[Text],
    where: Text,
) -> Optional[Dict[Text, Any]]:
    """按契约的 `style` / `explode` 把参数序列化成查询串键值对。

    返回 ``None`` 表示"这个形态没法用 `params:` 表达"，已告警（调用方跳过）。

    NOTICE（0920 批次 3 / **N19**）：修复前**完全不看** `style`/`explode`
    （全文件 grep 零命中），例值被原样塞进 `step.params[name] = value`，
    再用 `str()` 交给 requests 编码。于是契约说 A、请求发的是 B：

    ```text
    ids    (style=form, explode=false, example=[1,2,3])  契约 ids=1,2,3  实发 ids=1&ids=2&ids=3
    filter (style=deepObject,        example={a: 1})     契约 filter[a]=1 实发 filter=a（值 1 被吞）
    ```

    这与本模块 docstring 的承诺（"不能确定的造值**并告警**，绝不静默产出语义错的用例"）
    直接冲突：以前是**静默**产出语义错的查询串。现在的口径：

    | style / explode | 处置 |
    | --- | --- |
    | `form` + `explode=true`（默认） | 数组 → 重复键（`ids=1&ids=2`）在 `params` 里**表达不了**，告警 |
    | `form` + `explode=false` | 逗号连接 → `{"ids": "1,2,3"}`（**契约语义，可表达**） |
    | `spaceDelimited` / `pipeDelimited` | 空格 / `|` 连接（默认 `explode=false`） |
    | `deepObject` | `filter[a]=1` 这种带括号的键 —— `params` 能装，**直接用展开后的键** |
    | 标量值 | 一律直接放，`style` 对标量没有影响 |
    """
    style = str(parameter.get("style") or "form")
    explode = parameter.get("explode")
    if explode is None:
        # 契约默认值：form 默认 explode=true，其余 style 默认 false
        explode = style == "form"

    # 标量：style/explode 都不改变编码结果
    if not isinstance(value, (list, tuple, dict)):
        return {name: value}

    # 数组
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
        if style == "form" and explode:
            _warn(
                warnings,
                f"{where} 参数 {name!r} 是 `style=form, explode=true` 的数组 → 契约语义是"
                f"**重复键**（{name}={items[0]}&{name}={items[1] if len(items) > 1 else '...'}），"
                f"而框架的 `params:` 是字典、装不下重名键 → 已按 `{name}={','.join(items)}` 导入，"
                f"**请按契约手工改成重复键写法**（或用 `data:` 原始查询串）",
            )
            return {name: ",".join(items)}
        if style == "spaceDelimited":
            return {name: " ".join(items)}
        if style == "pipeDelimited":
            return {name: "|".join(items)}
        # form + explode=false（默认）以及未知 style → 逗号连接
        return {name: ",".join(items)}

    # 对象
    if style == "deepObject":
        # `filter[a]=1` / `filter[b]=2`：展开成带括号的键，`params` 能表达
        return {f"{name}[{key}]": item for key, item in value.items()}
    if style == "form" and explode:
        # 契约语义是 `a=1&b=2`（**丢掉参数名**）—— `params` 里表达不了，
        # 只能退化成 `name=a,1,b,2` 并告警
        flattened = ",".join(
            f"{key},{item}" for key, item in value.items()
        )
        _warn(
            warnings,
            f"{where} 参数 {name!r} 是 `style=form, explode=true` 的对象 → 契约语义是把键值"
            f"平铺进查询串（丢掉参数名），`params:` 表达不了 → 已按 `{name}={flattened}` 导入，"
            f"请按契约手工调整",
        )
        return {name: flattened}
    # form + explode=false（默认）→ `name=a,1,b,2`
    flattened = ",".join(f"{key},{item}" for key, item in value.items())
    return {name: flattened}


def _param_value(
    parameter: Dict[str, Any], document: Dict[str, Any], warnings: List[Text], where: Text
) -> Tuple[Any, bool]:
    """取参数值；返回 ``(值, 是否是「造出来的」)``。"""
    for key in ("example", "default"):
        if key in parameter and parameter[key] is not None:
            return parameter[key], False

    schema = parameter.get("schema") or {}
    resolved = resolve_ref(schema, document, warnings)
    if isinstance(resolved, dict):
        for key in ("example", "default"):
            if key in resolved and resolved[key] is not None:
                return resolved[key], False
        enum_values = resolved.get("enum")
        if isinstance(enum_values, list) and enum_values:
            return enum_values[0], False

    value = sample_from_schema(schema, document, str(parameter.get("name")), 0, warnings)
    if not isinstance(value, (str, int, float, bool)):
        value = json.dumps(value, ensure_ascii=False)
    _warn(
        warnings,
        f"{where} 参数 {parameter.get('name')!r} 没有 example/default/enum → "
        f"已按类型造值 {value!r}，请按业务替换",
    )
    return value, True


# --------------------------------------------------------------------------- 认证
def _security_headers(
    security: Optional[List[Dict[str, Any]]],
    schemes: Dict[Text, Any],
    variables: Dict[Text, Any],
    warnings: List[Text],
    where: Text,
) -> Dict[Text, Any]:
    """`security` + `securitySchemes` → 请求头占位（不落明文凭据）。"""
    if not security:
        return {}

    # NOTICE（批次 E / **L2**）：`security` 必须是**「对象列表」**（每个对象里是 scheme 名）。
    # 写成对象（漏了 `-`）时修复前 `security[0]` 抛裸 `KeyError: 0`；写成字符串列表
    # （`security: [bearerAuth]`）时 `requirement` 是**字符串**，下面 `for scheme_name in
    # requirement` 会**逐字符**迭代 → 认证头一个都不生成，而用例照常导入（**静默无认证**）。
    if not isinstance(security, list):
        _warn(
            warnings,
            f"{where} 的 `security` 应该是数组（元素是 `{{scheme名: []}}`），实际是 "
            f"{type(security).__name__} → 已忽略认证，请手工补请求头。\n"
            f"  正确写法：`security:\\n    - bearerAuth: []`",
        )
        return {}

    requirement = security[0]
    if not isinstance(requirement, dict):
        _warn(
            warnings,
            f"{where} 的 `security[0]` 应该是对象（`{{scheme名: []}}`，注意前面要有 `-`），"
            f"实际是 {type(requirement).__name__}：{requirement!r} → 已忽略认证，"
            f"请手工补请求头",
        )
        return {}

    if len(security) > 1:
        _warn(
            warnings,
            f"{where} 声明了 {len(security)} 组可选认证（security 是「或」关系）→ "
            f"只按第一组 {list(requirement)} 生成，其余请按需补充",
        )

    headers: Dict[Text, Any] = {}
    for scheme_name in requirement:
        scheme = resolve_ref(schemes.get(scheme_name) or {}, {}, warnings)
        if not isinstance(scheme, dict):
            continue
        scheme_type = str(scheme.get("type") or "").lower()

        if scheme_type == "http":
            http_scheme = str(scheme.get("scheme") or "").lower()
            if http_scheme == "bearer":
                variables.setdefault("AUTH_TOKEN", "${ENV(AUTH_TOKEN)}")
                headers["Authorization"] = "Bearer ${AUTH_TOKEN}"
                _warn(warnings, f"{where} 使用 bearerAuth → `Authorization: Bearer ${{AUTH_TOKEN}}`")
            elif http_scheme == "basic":
                variables.setdefault("AUTH_BASIC", "${ENV(AUTH_BASIC)}")
                headers["Authorization"] = "Basic ${AUTH_BASIC}"
                _warn(warnings, f"{where} 使用 basicAuth → `Authorization: Basic ${{AUTH_BASIC}}`")
            else:
                _warn(warnings, f"{where} 的 http 认证方案 {http_scheme!r} 未支持 → 请手工补请求头")
        elif scheme_type == "apikey":
            key_name = str(scheme.get("name") or "X-API-Key")
            location = str(scheme.get("in") or "header").lower()
            variable_name = "API_KEY_" + re.sub(r"[^0-9A-Za-z]+", "_", key_name).strip("_").upper()
            variables.setdefault(variable_name, "${ENV(" + variable_name + ")}")
            if location == "query":
                _warn(
                    warnings,
                    f"{where} 的 apiKey 在 query 里 → 请在 request.params 手工加 "
                    f"`{key_name}: ${{{variable_name}}}`（导入器不擅自改 URL）",
                )
            elif location == "cookie":
                _warn(
                    warnings,
                    f"{where} 的 apiKey 在 cookie 里 → 请在 request.cookies 手工加 "
                    f"`{key_name}: ${{{variable_name}}}`",
                )
            else:
                headers[key_name] = "${" + variable_name + "}"
                _warn(warnings, f"{where} 使用 apiKeyAuth → 请求头 `{key_name}: ${{{variable_name}}}`")
        elif scheme_type in ("oauth2", "openidconnect"):
            _warn(
                warnings,
                f"{where} 使用 {scheme_type} 认证 → **未自动迁移**；框架内置 Client Credentials，"
                "请在 YAML 补 `config.oauth2`（token_url/client_id/client_secret）或用 debugtalk 取 token",
            )
        else:
            _warn(warnings, f"{where} 的认证类型 {scheme_type!r} 未支持 → 请手工补")
    return headers


# --------------------------------------------------------------------------- Swagger 2.0
def _first_media_type(
    declared: Any, default: Text, warnings: List[Text], where: Text, field: Text
) -> Text:
    """取 Swagger 2.0 媒体类型数组（`consumes` / `produces`）里的**第一个**。

    NOTICE（0919-2 / 缺陷 6）：`consumes` 在 Swagger 2.0 里是 **Operation 对象或顶层
    Swagger 对象**的字段，`in: body` 的 parameter 上**根本没有**它。修复前 body 分支写的是：

    ```python
    content_type = (body_parameter.get("consumes") or consumes_default)[0]   # ← 恒为 None
    ```

    于是 operation 级声明的 `application/xml` 被忽略，接口被错标成 JSON；
    而同一个函数里的 formData 分支用的却是 `operation.get("consumes")`——
    同一个函数两套口径，这正是缺陷的形态（不是「写错一个键」，是「没有唯一口径」）。
    现在两处都走本函数：**operation 级 → 调用方传入的兜底（顶层/默认）**，顺序只此一份。

    顺带收口：`consumes: "application/xml"`（字符串而非数组）是**不合规范**的写法，
    但第三方产物里出现过。修复前的 `[0]` 会静默取到首字符 `"a"` 当成 content-type，
    生成的用例带一个荒谬的媒体类型且零告警；这里按单元素处理并明确告警。
    """
    if isinstance(declared, Text):
        stripped = declared.strip()
        if stripped:
            _warn(
                warnings,
                f"{where}：Swagger 2.0 的 `{field}` 是字符串 {stripped!r}，"
                f"规范要求是数组 → 已按单元素处理（请修正契约）",
            )
            return stripped
        return default

    if isinstance(declared, list) and declared:
        return str(declared[0])

    if declared:
        # 既不是字符串也不是非空数组（例如写成对象）。修复前 `declared[0]` 会以
        # KeyError/TypeError **响亮崩掉**；本函数不崩，那就**必须留痕**——
        # 否则就是把「响亮崩溃」换成「静默默认」，正是本批要消灭的东西。
        _warn(
            warnings,
            f"{where}：Swagger 2.0 的 `{field}` 形态无法识别"
            f"（{type(declared).__name__}）→ 已按默认 {default} 处理（请修正契约）",
        )

    return default


def normalize_swagger2(document: Dict[str, Any], warnings: List[Text]) -> Dict[str, Any]:
    """把 Swagger 2.0 归一化成 3.0 形状（能映射的映射，不能的告警）。"""
    normalized: Dict[str, Any] = {
        "openapi": "3.0.0",
        "info": document.get("info") or {},
        "paths": copy.deepcopy(document.get("paths") or {}),
        "components": {
            "schemas": document.get("definitions") or {},
            "securitySchemes": {},
        },
    }

    schemes = document.get("schemes") or ["https"]
    host = document.get("host") or ""
    base_path = document.get("basePath") or ""
    if host:
        normalized["servers"] = [{"url": f"{schemes[0]}://{host}{base_path}"}]
        _warn(
            warnings,
            f"Swagger 2.0 → 3.0：server 用 `{schemes[0]}://{host}{base_path}`"
            "（其余 schemes 请按需切换）",
        )
    else:
        _warn(warnings, "Swagger 2.0 契约没有 host → 无法确定 base_url，请手工填写")

    # securityDefinitions → components.securitySchemes
    for name, definition in (document.get("securityDefinitions") or {}).items():
        definition = dict(definition or {})
        kind = str(definition.get("type") or "").lower()
        if kind == "basic":
            normalized["components"]["securitySchemes"][name] = {
                "type": "http",
                "scheme": "basic",
            }
        elif kind == "apikey":
            normalized["components"]["securitySchemes"][name] = {
                "type": "apiKey",
                "in": definition.get("in", "header"),
                "name": definition.get("name", "X-API-Key"),
            }
        elif kind == "oauth2":
            normalized["components"]["securitySchemes"][name] = {"type": "oauth2"}
        else:
            _warn(warnings, f"Swagger 2.0 的 securityDefinitions.{name}（{kind}）未支持 → 请手工补")
    if document.get("security"):
        normalized["security"] = document["security"]

    # 2.0 的顶层复用段落 → components.*（否则 `#/parameters/...`、`#/responses/...` 会解析不到）
    for section in ("parameters", "responses"):
        if document.get(section):
            normalized["components"][section] = copy.deepcopy(document[section])

    # NOTICE（批次 5 / H7）：`$ref` 前缀重写**提前到这里**（原先在函数末尾 `return _rewrite_refs(...)`）。
    # 下面的 `parameters(in: body)` 分流现在会**先解析 `$ref`**，而 2.0 契约里的引用是
    # `#/parameters/X` 形态、归一化后实际位于 `#/components/parameters/X` ——
    # 不先重写，那个 `resolve_ref` 必然解析不到（会打一条"`$ref` 指向的节点不存在"的告警
    # 并把 body 又丢掉）。重写是幂等的：已经在 `components.*` 下的引用不再匹配 2.0 前缀。
    # 上面那段 deepcopy 已完成，所以被拷进 components 的段落里的 `$ref` 也会一并被重写。
    normalized = _rewrite_refs(normalized)

    consumes_default = document.get("consumes") or [DEFAULT_MEDIA_TYPE]

    # parameters(in: body/formData) → requestBody；responses[].schema → content
    for path, path_item in (normalized["paths"] or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue

            remaining_parameters = []
            body_parameter = None
            form_parameters = []
            for parameter in operation.get("parameters") or []:
                # NOTICE（批次 5 / **H7**）：**先解析 `$ref` 再按 `in` 分流**。
                #
                # Swagger 2.0 允许把可复用参数放在顶层 `parameters` 里按 `$ref` 引用：
                #     parameters: [{"$ref": "#/parameters/UserBody"}]     # 顶层 UserBody: {in: body, ...}
                # 修复前这里直接读 `parameter.get("in")` —— 而 `$ref` **尚未解析**，
                # `in` 是空的，于是这个 body 参数被当成"其它参数"塞回 `remaining_parameters`，
                # `body_parameter` 保持 None → 不生成 `requestBody` → **整个请求体静默丢掉**
                # （生成物只有 method/url，零告警；内联 `in: body` 的写法却正常）。
                #
                # 时序上还有一处前提：`_rewrite_refs` 必须已经跑过（把 2.0 的 `#/parameters/X`
                # 改成 3.0 的 `#/components/parameters/X`，而顶层段落已在上面拷进 components）。
                # 本批把那一步**提前**到本循环之前 —— 否则这里解析的会是 2.0 前缀、必然解析不到。
                resolved = resolve_ref(parameter, normalized, warnings)
                if not isinstance(resolved, dict):
                    remaining_parameters.append(parameter)
                    continue

                location = str(resolved.get("in") or "")
                if location == "body":
                    body_parameter = resolved
                elif location == "formData":
                    form_parameters.append(resolved)
                else:
                    # 其余参数**保留原样**（可能是 `$ref`）：下游 `_collect_parameters`
                    # 自己会解析，这里不改写形状，保持既有行为
                    remaining_parameters.append(parameter)
            if remaining_parameters:
                operation["parameters"] = remaining_parameters
            else:
                operation.pop("parameters", None)

            if body_parameter:
                # 缺陷 6 的修法：consumes 从 **operation** 取（其次顶层，最后默认）——
                # body parameter 上没有这个字段，修复前写 `body_parameter.get("consumes")`
                # 恒为 None，operation 级声明被整个忽略。
                content_type = _first_media_type(
                    operation.get("consumes") or consumes_default,
                    DEFAULT_MEDIA_TYPE,
                    warnings,
                    f"{method.upper()} {path}",
                    "consumes",
                )
                operation["requestBody"] = {
                    "required": bool(body_parameter.get("required")),
                    "content": {content_type: {"schema": body_parameter.get("schema") or {}}},
                }
                _warn(
                    warnings,
                    f"{method.upper()} {path}：Swagger 2.0 的 body 参数已转成 requestBody"
                    f"（content-type {content_type}）",
                )
            elif form_parameters:
                # 与 body 分支共用同一个取用口径（修复前两处写法不同，见 _first_media_type）
                content_type = _first_media_type(
                    operation.get("consumes") or consumes_default,
                    DEFAULT_MEDIA_TYPE,
                    warnings,
                    f"{method.upper()} {path}",
                    "consumes",
                )
                properties = {}
                required = []
                for parameter in form_parameters:
                    name = str(parameter.get("name"))
                    if str(parameter.get("type")) == "file":
                        # Swagger 2.0 的 `type: file` 等价于 3.0 的 `type: string, format: binary`
                        properties[name] = {"type": "string", "format": "binary"}
                    else:
                        properties[name] = parameter.get("schema") or {
                            key: parameter[key]
                            for key in ("type", "format", "enum", "default", "example")
                            if key in parameter
                        }
                    if parameter.get("required"):
                        required.append(name)
                operation["requestBody"] = {
                    "content": {
                        content_type: {
                            "schema": {"type": "object", "properties": properties, "required": required}
                        }
                    }
                }
                _warn(
                    warnings,
                    f"{method.upper()} {path}：Swagger 2.0 的 formData 参数已转成 "
                    f"requestBody（{content_type}）",
                )

            for status, response in (operation.get("responses") or {}).items():
                if isinstance(response, dict) and "schema" in response:
                    # 顺带收口（同缺陷 6 家族）：`produces` 与 `consumes` 是同一形态的字段，
                    # 取用顺序也一致（operation → 顶层 → 默认）。修复前这里已经是对的顺序，
                    # 但 `[0]` 的字符串形态问题与 consumes 完全相同（`produces: "application/xml"`
                    # 会静默取到首字符 "a"），既然两处必须同口径，就一起走 _first_media_type。
                    produces = _first_media_type(
                        operation.get("produces") or document.get("produces"),
                        DEFAULT_MEDIA_TYPE,
                        warnings,
                        f"{method.upper()} {path}",
                        "produces",
                    )
                    response["content"] = {produces: {"schema": response.pop("schema")}}

    _warn(
        warnings,
        "输入是 **Swagger 2.0**：已按等价规则归一化成 OpenAPI 3.0 再导入；"
        "未覆盖的字段（如 x-* 扩展、collectionFormat）请人工核对",
    )

    # `$ref` 重写：2.0 的 `#/definitions/X` 在归一化后位于 `#/components/schemas/X`
    # NOTICE: 不做这一步，2.0 契约里所有 `$ref` 都会解析不到（实测踩过：骨架值退化成 "string"）
    # NOTICE（批次 5 / H7）：这一步**已经在上面提前执行过**（那时 `parameters` 分流需要它），
    # 这里直接返回；保留这行是为了让"归一化的输出必须是 3.0 形态"这件事有个显式的落点。
    return normalized


_REF_REWRITES = (
    ("#/definitions/", "#/components/schemas/"),
    ("#/parameters/", "#/components/parameters/"),
    ("#/responses/", "#/components/responses/"),
    ("#/securityDefinitions/", "#/components/securitySchemes/"),
)


def _rewrite_refs(node: Any) -> Any:
    """递归把 Swagger 2.0 的 `$ref` 前缀改写成 3.0 的 `components.*`。"""
    if isinstance(node, dict):
        rewritten = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                for old, new in _REF_REWRITES:
                    if value.startswith(old):
                        value = new + value[len(old) :]
                        break
            rewritten[key] = _rewrite_refs(value)
        return rewritten
    if isinstance(node, list):
        return [_rewrite_refs(item) for item in node]
    return node


# --------------------------------------------------------------------------- 主体
def _base_url(document: Dict[str, Any], warnings: List[Text]) -> Text:
    servers = document.get("servers")
    if servers is None:
        servers = []
    # NOTICE（批次 E / **L2**）：`servers` 必须是**数组**。写成对象（漏了 `-`：
    # `servers: {url: https://x}`）时，修复前 `len(servers) > 1` 为假、下面
    # `servers[0]` 直接抛裸 `KeyError: 0` —— 报错完全看不出「servers 写成了对象」。
    if not isinstance(servers, list):
        _warn(
            warnings,
            f"`servers` 应该是**数组**（每个元素是 `{{url: ...}}`），实际是 "
            f"{type(servers).__name__} → 已忽略，base_url 留空。\n"
            f"  最常见的原因是漏了列表符号 `-`：\n"
            f"      servers:\n"
            f"        - url: https://host\n"
            f"  而不是 `servers: {{url: https://host}}`。",
        )
        return ""

    if len(servers) > 1:
        _warn(warnings, f"契约声明了 {len(servers)} 个 servers → 只取第一个，其余请按需切换")
    if not servers:
        _warn(warnings, "契约没有 servers → base_url 留空，请手工补被测环境地址")
        return ""

    server = servers[0] if isinstance(servers[0], dict) else {}
    if not isinstance(servers[0], dict):
        _warn(
            warnings,
            f"`servers[0]` 应该是对象（`{{url: ...}}`），实际是 "
            f"{type(servers[0]).__name__} → 已忽略，base_url 留空",
        )
    url = str(server.get("url") or "")
    variables = server.get("variables") or {}
    for name, definition in variables.items():
        default = (definition or {}).get("default")
        if default is None:
            _warn(warnings, f"server 变量 {name} 没有 default → 保留 `{{{name}}}`，请手工替换")
            continue
        url = url.replace("{" + str(name) + "}", str(default))

    leftovers = re.findall(r"\{([^{}]+)\}", url)
    if leftovers:
        _warn(warnings, f"server url 里仍有未替换的变量 {leftovers} → 请手工替换")

    # NOTICE（批次 6 / **M10**）：**相对 `servers[].url`**（`"url": "/v1"`，规范内的合法写法）
    # 推不出主机名 —— 按 OpenAPI 的定义它是"相对于契约文档所在的地址"。
    # 修复前这里**原样返回** `/v1`，生成 `base_url: /v1` 且**零告警**，导入还打印
    # "导出即验证通过"；运行期 100% 失败，报的是 `ParamsError: base url missed!`
    # —— 这条错误信息里完全看不出"契约里写的是相对地址"。
    # 这里改不了值（框架拿不到"契约文档的地址"这个上下文），但必须把它**说出来**。
    if url and not urlparse(url).netloc:
        _warn(
            warnings,
            f"契约的 `servers[].url` 是**相对地址**（{url!r}）→ 推导不出主机名，"
            f"已原样保留但**不能直接运行**：相对 server 表示「相对于契约文档所在的地址」，"
            f"框架没有这个上下文。\n"
            f"  hint: 请在契约里写成绝对地址（`https://host{url if url.startswith('/') else '/' + url}`），"
            f"或在生成的 YAML 里手工把 `config.base_url` 补成被测环境地址。"
        )

    return url


def _collect_parameters(
    path_item: Dict[str, Any], operation: Dict[str, Any], document: Dict[str, Any], warnings: List[Text], where: Text
) -> Dict[Text, Dict[str, Any]]:
    """路径级 + 操作级 parameters 合并（操作级同 name+in 覆盖路径级）。"""
    merged: Dict[Text, Dict[str, Any]] = {}
    for source in (path_item.get("parameters"), operation.get("parameters")):
        # NOTICE（批次 E / **L2**）：`parameters` 必须是**对象列表**。
        # 修复前是 `for parameter in source:` —— 写成对象（漏了 `-`）时迭代出来的是
        # **键名（字符串）**，`resolve_ref` 拿到字符串直接返回它、`isinstance(resolved, dict)`
        # 为假 → 全部参数被**静默丢掉**（包括 `required: true` 的查询参数），
        # 生成的用例少发参数、零告警；写成字符串时更糟（逐字符迭代）。
        for parameter in safe_dict_entries(source, f"{where} 的 parameters", warnings):
            resolved = resolve_ref(parameter, document, warnings)
            if not isinstance(resolved, dict):
                continue
            key = f"{resolved.get('in')}:{resolved.get('name')}"
            merged[key] = resolved
    return merged


def _responses_assertions(
    operation: Dict[str, Any],
    document: Dict[str, Any],
    warnings: List[Text],
    case_stem: Text,
    index: int,
    step_name: Text,
    assertion_mode: Text,
) -> Tuple[List[Dict[str, Any]], Optional[Text], Optional[Dict[str, Any]]]:
    responses = operation.get("responses") or {}
    if assertion_mode == "none" or not responses:
        return [], None, None

    numeric = []
    for status, response in responses.items():
        try:
            numeric.append((int(str(status)), str(status), response))
        except ValueError:
            continue  # `default` 之类
    if not numeric:
        _warn(warnings, "响应只声明了 `default` → 无法确定期望状态码，请手工补断言")
        return [], None, None

    successes = [item for item in numeric if 200 <= item[0] < 300]
    chosen = min(successes or numeric, key=lambda item: item[0])
    status_code, status_key, response = chosen
    if not successes:
        _warn(warnings, f"没有 2xx 响应 → 已按最小的 {status_key} 生成状态码断言，请确认是否符合预期")
    if len([item for item in numeric if 200 <= item[0] < 300]) > 1:
        _warn(
            warnings,
            f"声明了多个 2xx 响应（{[item[1] for item in numeric if 200 <= item[0] < 300]}）"
            f"→ 只按 {status_key} 生成",
        )

    assertions: List[Dict[str, Any]] = [{"eq": ["status_code", status_code]}]
    schema_file = None
    schema_content = None

    response = resolve_ref(response, document, warnings) or {}
    content = response.get("content") or {}
    if assertion_mode == "status+schema" and content:
        json_content = None
        for content_type, value in content.items():
            if "json" in str(content_type).lower():
                json_content = value
                break
        if json_content is None:
            _warn(
                warnings,
                f"响应 {status_key} 只声明了非 JSON 的 content-type（{list(content)}）→ 只断状态码",
            )
        else:
            schema = (json_content or {}).get("schema")
            if isinstance(schema, dict):
                built = build_assertion_schema(schema, document, warnings)
                if built:
                    # NOTICE（批次 9-1 / H5 收口）：步骤词干改走 `ir.safe_file_stem`
                    # （与上面 `case_stem` 用的是同一个函数）。修复前这里是手写的
                    # `re.sub(r"[^0-9A-Za-z]+", "_", step_name)`，中文 step 名会整段退化成
                    # `step` —— 与 HAR 的 `_schema_file_name` 是同一处遗留，一起收口。
                    safe_step = safe_file_stem(step_name, default="step")
                    schema_file = f"schemas/{case_stem}_{index:02d}_{safe_step}.json"
                    schema_content = built
                    assertions.append({"jsonschema_match": ["body", schema_file]})
                    _warn(warnings, f"已按契约生成响应契约断言 {schema_file}（JSON Schema，文件引用）")
                else:
                    _warn(warnings, f"响应 {status_key} 的 schema 无法转换 → 只断状态码")
    elif assertion_mode == "status+schema" and not content:
        _warn(warnings, f"响应 {status_key} 没有 content（如 204）→ 只断状态码")

    return assertions, schema_file, schema_content


def _request_body(
    operation: Dict[str, Any], document: Dict[str, Any], warnings: List[Text], where: Text
) -> Tuple[Any, Any, Dict[Text, Text]]:
    body = resolve_ref(operation.get("requestBody") or {}, document, warnings)
    if not isinstance(body, dict) or not body:
        return None, None, {}

    content = body.get("content") or {}
    if not content:
        _warn(warnings, f"{where} 的 requestBody 没有 content → 已跳过请求体")
        return None, None, {}

    json_type = next((key for key in content if "json" in str(key).lower()), None)
    form_type = next((key for key in content if "x-www-form-urlencoded" in str(key).lower()), None)
    multipart_type = next((key for key in content if "multipart/form-data" in str(key).lower()), None)

    if json_type:
        media = content[json_type] or {}
        if "example" in media:
            return media["example"], None, {}
        examples = media.get("examples")
        if isinstance(examples, dict) and examples:
            first = next(iter(examples.values()))
            if isinstance(first, dict) and "value" in first:
                _warn(warnings, f"{where} 的 requestBody 用的是 examples 的第一条")
                return first["value"], None, {}
        schema = media.get("schema")
        if isinstance(schema, dict):
            sample = sample_from_schema(schema, document, "", 0, warnings)
            _warn(
                warnings,
                f"{where} 的 requestBody 没有 example → 已按 schema **造骨架**，请按业务替换其中的值",
            )
            return sample, None, {}
        _warn(warnings, f"{where} 的 requestBody 既没有 example 也没有 schema → 已跳过请求体")
        return None, None, {}

    if form_type:
        media = content[form_type] or {}
        schema = media.get("schema") or {}
        # NOTICE（0918-8 / M20）：造值必须用**已解析 `$ref`/`allOf`** 的 properties。
        # 修复前这里取的是未解析的 `schema.get("properties")`——契约写成 `$ref: '#/components/schemas/X'`
        # 时它是**空的**，于是每个字段都退化成字面量字符串 `"string"`，`example`/`default`/`enum` 全丢。
        #
        # 为什么用 `resolve_ref + _merge_all_of` 而不用 `build_assertion_schema`：
        # 后者是**断言用的裁剪版**（只保留断言关键字，`example` 不在其中），
        # 拿它造值会把内联形态本来能取到的 `example: hello` 一起丢掉 —— 那是另一种回归。
        resolved_body = _merge_all_of(
            resolve_ref(schema, document, warnings), document, warnings
        )
        properties = resolved_body.get("properties") or {}
        if not properties:
            _warn(warnings, f"{where} 的表单 schema 没有 properties → 已跳过请求体")
            return None, None, {}
        fields = {
            name: sample_from_schema(prop_schema, document, name, 0, warnings)
            for name, prop_schema in list(properties.items())[:MAX_PROPERTIES]
        }
        _warn(warnings, f"{where} 的表单请求体已按 schema 生成（{len(fields)} 个字段），请核对取值")
        return None, fields, {}

    if multipart_type:
        media = content[multipart_type] or {}
        schema = media.get("schema") or {}
        # NOTICE（0918-8 / M20）：同 form 分支——用解析后的 properties，
        # 否则 `$ref` 形态下 `format: binary` 判不出来（文件字段被当普通表单字段发出去），
        # 而 `binary` 一旦丢失，整个 multipart 请求的**语义就变了**（不是 multipart 而是 urlencoded）。
        resolved_body = _merge_all_of(
            resolve_ref(schema, document, warnings), document, warnings
        )
        properties = resolved_body.get("properties") or {}
        fields: Dict[Text, Any] = {}
        uploads: Dict[Text, Text] = {}
        for name, prop_schema in list(properties.items())[:MAX_PROPERTIES]:
            if str((prop_schema or {}).get("format") or "") == "binary":
                uploads[name] = f"{name}.bin"
            else:
                fields[name] = sample_from_schema(prop_schema, document, name, 0, warnings)
        if uploads:
            _warn(
                warnings,
                f"{where} 的 multipart 二进制字段已映射到 `upload`：{uploads}"
                "（文件路径占位，请换成真实文件）",
            )
        _warn(warnings, f"{where} 的 multipart 请求体已按 schema 生成，请核对取值")
        return None, (fields or None), uploads

    _warn(
        warnings,
        f"{where} 的 requestBody content-type 是 {list(content)} → 未支持，已跳过请求体"
        "（可手工用 `data` 传原始报文）",
    )
    return None, None, {}


def convert_openapi(
    document: Dict[str, Any],
    case_name: Text = "",
    split_tags: bool = True,
    assertion_mode: Text = "status+schema",
    include_tags: Optional[List[Text]] = None,
    exclude_tags: Optional[List[Text]] = None,
    include_methods: Optional[List[Text]] = None,
) -> List[IRCase]:
    """OpenAPI（3.x / Swagger 2.0）→ `IRCase` 列表（默认一个 tag 一个用例）。"""
    global_warnings: List[Text] = []

    if not isinstance(document, dict) or not (
        document.get("openapi") or document.get("swagger")
    ):
        raise ValueError(
            "看起来不是 OpenAPI/Swagger 契约（缺少 openapi / swagger 字段）\n"
            "提示：Swagger 2.0 也可以直接导入（会先归一化）"
        )

    version = str(document.get("openapi") or document.get("swagger") or "")
    if document.get("swagger"):
        document = normalize_swagger2(document, global_warnings)
    elif not version.startswith("3."):
        _warn(global_warnings, f"契约版本是 {version}（本导入器按 OpenAPI 3.x 语义解析，未知字段会被忽略）")

    info = document.get("info")
    if not isinstance(info, dict):
        # NOTICE（批次 E / L1）：修复前这里直接 `info.get("title")` —— `info` 写成字符串
        # 时抛裸 `AttributeError: 'str' object has no attribute 'get'`。
        if info is not None:
            _warn(
                global_warnings,
                f"契约的 `info` 应该是对象（`{{title: ...}}`），实际是 "
                f"{type(info).__name__} → 已忽略，用例名回退成文件名/`openapi`",
            )
        info = {}
    title = case_name or str(info.get("title") or "openapi")
    base_url = _base_url(document, global_warnings)
    schemes = ((document.get("components") or {}).get("securitySchemes")) or {}
    global_security = document.get("security")

    paths = document.get("paths")
    if not isinstance(paths, dict):
        # NOTICE（批次 E / L1）：修复前 `paths.items()` 在 `paths` 是列表/字符串时裸崩。
        if paths is not None:
            _warn(
                global_warnings,
                f"契约的 `paths` 应该是对象（`{{/path: {{get: ...}}}}`），实际是 "
                f"{type(paths).__name__} → 无法导入任何接口",
            )
        paths = {}
    if not paths:
        return [IRCase(name=title, warnings=global_warnings + ["契约里没有 paths（没有任何接口）"])]

    groups: Dict[Text, List[Tuple[Text, Text, Dict[str, Any], Dict[str, Any]]]] = {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            if include_methods and method.upper() not in [m.upper() for m in include_methods]:
                continue
            tags = operation.get("tags") or []
            tag = str(tags[0]) if tags else "untagged"
            if include_tags and tag not in include_tags:
                continue
            if exclude_tags and tag in exclude_tags:
                continue
            key = tag if split_tags else "all"
            groups.setdefault(key, []).append((path, method, operation, path_item))

    if not groups:
        return [
            IRCase(
                name=title,
                warnings=global_warnings
                + [f"没有匹配到任何 operation（include_tags={include_tags}, exclude_tags={exclude_tags}）"],
            )
        ]

    cases: List[IRCase] = []
    for group_key, operations in groups.items():
        case = IRCase(
            name=f"{title} / {group_key}" if group_key != "all" else title,
            base_url=base_url,
            source=str(info.get("title") or "openapi"),
        )
        case.warnings.extend(global_warnings)
        case_variables: Dict[Text, Any] = {}
        dynamic_variables: List[Text] = []
        step_index = 0

        for path, method, operation, path_item in operations:
            step_index += 1
            warnings: List[Text] = []
            summary = str(operation.get("summary") or operation.get("operationId") or "")
            step_name = f"{method.upper()} {path}" + (f" — {summary}" if summary else "")
            if operation.get("deprecated"):
                _warn(warnings, "该接口在契约里标记为 `deprecated: true`（仍然导入了，请确认是否还需要）")

            parameters = _collect_parameters(path_item, operation, document, warnings, step_name)

            # ---- 路径参数：有 example 用之，否则造值（并集中放 config.variables）
            step_path = path

            def _bind_path_variable(raw_name: Text, parameter: Optional[Dict[str, Any]]) -> None:
                """把 `{name}` 换成 `${变量}` 并登记变量（**声明与未声明共用一份口径**）。"""
                nonlocal step_path
                if parameter is not None:
                    value, _generated = _param_value(parameter, document, warnings, "路径")
                else:
                    # 未声明的模板变量：没有 schema 可依，按字符串造一个值（下面会告警）
                    value, _generated = _param_value({}, document, warnings, "路径")
                variable_name = re.sub(r"[^0-9A-Za-z_]+", "_", raw_name) or "path_param"
                if variable_name in case_variables and case_variables[variable_name] != value:
                    variable_name = f"{variable_name}_{step_index}"
                case_variables.setdefault(variable_name, value)
                dynamic_variables.append(variable_name)
                step_path = step_path.replace("{" + raw_name + "}", "${" + variable_name + "}")

            for key, parameter in parameters.items():
                if parameter.get("in") != "path":
                    continue
                _bind_path_variable(str(parameter.get("name")), parameter)

            # NOTICE（批次 6 / **M11**）：契约**没声明**却出现在路径里的模板变量
            # （`/pets/{petId}` 而 `parameters` 里没有它）必须一并处理。
            # 修复前它**原样进 URL**、零告警 → 实跑发出 `GET /pets/%7BpetId%7D`
            # （花括号被 requests 百分号编码），服务端 404；而**宽松的服务端**可能照常返回 200
            # → 用例"通过"，但打的根本不是那条路径。
            # 口径与已声明的路径参数**完全一致**（造值 + 登记 `config.variables` + 可一处改），
            # 唯一的区别是多一条告警说明"契约里没声明"。
            #
            # NOTICE: 必须在**原始 `path`** 上找、并**排除已声明的名字**。
            # 拿替换后的 `step_path` 去找会命中 `${petId}` 里的 `{petId}` → 把已绑定的再绑一次
            # （变成 `$${petId}`）—— 第一版就是这么写的，一次性打红 33 条既有用例。
            declared_path_names = {
                str(parameter.get("name"))
                for parameter in parameters.values()
                if parameter.get("in") == "path"
            }
            for undeclared in re.findall(r"\{([^{}]+)\}", path):
                if undeclared in declared_path_names:
                    continue
                _warn(
                    warnings,
                    f"路径模板变量 `{undeclared}` 在契约的 `parameters` 里**没有声明** → "
                    f"已按类型造值并登记到 `config.variables`（请确认取值）。"
                    f"修复前它会原样留在 URL 里（实跑发出 `%7B{undeclared}%7D`）。",
                )
                _bind_path_variable(undeclared, None)

            step = IRStep(name=step_name, method=method.upper(), url=step_path, source=f"{method.upper()} {path}")

            # ---- query / header / cookie 参数
            for key, parameter in parameters.items():
                location = str(parameter.get("in") or "")
                name = str(parameter.get("name"))
                if location in ("path", "body"):
                    continue
                has_hint = any(
                    parameter.get(field) is not None for field in ("example", "default")
                ) or isinstance(resolve_ref(parameter.get("schema") or {}, document, []), dict) and bool(
                    (resolve_ref(parameter.get("schema") or {}, document, []) or {}).get("enum")
                )
                if not parameter.get("required") and not has_hint:
                    _warn(
                        warnings,
                        f"可选参数 {name}({location}) 没有 example/default/enum → 已跳过，"
                        "需要时请手工加进用例",
                    )
                    continue
                value, _generated = _param_value(parameter, document, warnings, f"{location}")
                if location == "query":
                    # NOTICE（0920 批次 3 / **N19**）：查询参数必须按契约的
                    # `style`/`explode` 序列化，否则契约说 A、请求发的是 B
                    # （详见 `_serialize_query_param` 的 NOTICE）。
                    step.params.update(
                        _serialize_query_param(
                            str(name), value, parameter, warnings, f"步骤 {step_name!r}"
                        )
                    )
                elif location == "header":
                    step.headers[name] = value
                elif location == "cookie":
                    step.cookies[name] = value
                else:
                    _warn(warnings, f"参数 {name} 的位置 {location!r} 未支持 → 已跳过")

            # ---- 认证（操作级覆盖全局）
            security = operation.get("security")
            if security is None:
                security = global_security
            step.headers.update(_security_headers(security, schemes, case_variables, warnings, f"步骤 {step_name!r}"))

            # ---- 请求体
            json_body, data, upload = _request_body(operation, document, warnings, f"步骤 {step_name!r}")
            step.json_body = json_body
            step.data = data
            step.upload = upload

            # ---- 断言
            assertions, schema_file, schema_content = _responses_assertions(
                operation,
                document,
                warnings,
                # NOTICE（批次 5 / H5 同类根因）：词干必须**保留非 ASCII**。
                # 修复前这里写的是 `re.sub(r"[^0-9A-Za-z]+", "_", ...) or "case"`，
                # 中文 tag 一律退化成 `case` → 多个中文 tag 的 schema 文件**同名互相覆盖**，
                # 断言断到别人的响应形状（与 Postman 侧同一个根因，共用一个口径）。
                safe_file_stem(group_key),
                step_index,
                step_name,
                assertion_mode,
            )
            step.assertions = assertions
            step.schema_file = schema_file
            step.schema_content = schema_content
            if schema_file and schema_content:
                case.extra_files[schema_file] = schema_content

            step.warnings.extend(warnings)
            case.steps.append(step)

        case.variables = merge_variables(case_variables)
        if dynamic_variables:
            case.add_warning(
                "路径参数的值放在 `config.variables` 里（"
                + "、".join(sorted(set(dynamic_variables)))
                + "），换成真实 id 时改这里即可"
            )
        cases.append(case)

    # M5（0918-2）：统一收口脱敏。
    # 修复前 OpenAPI 这条链路**一次 `sanitize_*` 都没调用**（grep 零命中）：
    # 参数示例、请求体示例里的凭据全部明文入库，是四个源里覆盖面最大的一处。
    result = sanitize_cases(
        cases or [IRCase(name=title, warnings=global_warnings + ["没有生成任何步骤"])]
    )
    # 批次 9-0（M9）：素材里若出现 `${...}` 字面量 → 告警（只告警，不擅自改写）。
    # 契约里的 example/description 常被当作"文字"，但它在运行期仍会被当表达式执行。
    warn_source_expressions(result, document, "OpenAPI 契约")
    return result


def convert_openapi_file(path: Text, **kwargs) -> List[IRCase]:
    """读取 OpenAPI/Swagger 文件（`.json` / `.yaml` / `.yml`）并转换。"""
    with open(path, mode="r", encoding="utf-8-sig") as fp:
        content = fp.read()

    if path.lower().endswith((".yaml", ".yml")):
        import yaml  # noqa: PLC0415

        try:
            document = yaml.safe_load(content)
        except yaml.YAMLError as ex:
            raise ValueError(f"契约 YAML 解析失败：{path}\n{ex}") from ex
    else:
        try:
            document = json.loads(content)
        except ValueError as ex:
            raise ValueError(f"契约 JSON 解析失败：{path}\n{ex}") from ex

    kwargs.setdefault("case_name", os.path.splitext(os.path.basename(path))[0])
    return convert_openapi(document, **kwargs)


def required_helper_functions(cases: List[IRCase]) -> List[Text]:
    """OpenAPI 导入不需要额外的 debugtalk helper（保留函数是为了 CLI 统一调用）。"""
    return []


__all__ = [
    "build_assertion_schema",
    "convert_openapi",
    "convert_openapi_file",
    "infer_schema",
    "normalize_swagger2",
    "required_helper_functions",
    "resolve_ref",
    "sample_from_schema",
]
