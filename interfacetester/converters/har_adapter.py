"""HAR → IR 适配器（P3-a）。

**支持子集**
------------
| HAR 字段 | 处理 |
| --- | --- |
| `log.entries[].request.method/url` | → `method` / URL（origin 进 `base_url`，path 进步骤 url） |
| `request.queryString[]` | → `params`（有重名键时退回把查询串留在 URL 里并告警 —— 字典装不下重名） |
| `request.headers[]` | → `headers`（跳过 HTTP/2 伪头 `:authority`/`:method`…；敏感头占位化） |
| `request.cookies[]` | → `cookies`（**值全部占位化**） |
| `request.postData`（json / form / multipart / 纯文本） | → `json` / `data` / `upload` |
| `response.status` | → `eq: [status_code, N]`（忠实回放录到的状态码） |
| `response.content.text`（JSON） | → **`jsonschema_match`** + 生成 `schemas/*.json`（形状断言：type + required） |
| `_resourceType` / 扩展名 | 过滤静态资源（js/css/图片/字体/媒体）—— 可用 `--include-hosts` 等参数进一步收窄 |
| `response.status == 0` | 跳过（请求中止/未完成） |
| `response.status` ∈ `{301,302,303,307,308}` | **跳过该条目并告警**（重定向中间态：框架默认跟随重定向，照录制断 3xx 必然失败）—— 0920 / 缺陷 3 |
| `response.status` 不是数字（如 `"200 OK"`） | **条目保留**，但不生成状态码断言 → 逐条告警（`--assertions status` 下该步会一条断言都没有，另有一条结果级告警）—— 批次 8 / L5：修复前 `int()` 直接抛 `ValueError`，**整批导入 exit 2、`--out` 不产出** |
| 条目 / `request` / `response` 不是对象，或 `request.url` 缺失 | **跳过该条条目并逐条告警**，其余条目照常导入（批次 8 / L21：修复前同样整批崩） |
| `headers` / `queryString` / `cookies` / `postData.params` 里混进非对象条目 | 跳过那些条目并告警（请求本身照常导入）—— 字段级坏数据不牵连整条 |
| `OPTIONS` | 跳过（预检请求，不是业务接口） |

**不支持的**：Cookie/凭据的**明文保留**（永远占位化）、JS 注入、`postData` 为二进制时只能跳过、
`allOf/oneOf` 这类契约语义（HAR 只是录制，没有契约）。

设计要点
--------
1. **形状断言（schema）而不是逐字段断言**：录制到的响应是「某一次真实结果」，
   逐字段断言会把随机数据固化成期望；用 JSON Schema 断「结构 + 类型 + 必填」更稳，也不易误报；
   生成的 schema 走**文件引用**（P2-b 的硬约定：内联 schema 会被 `parse_data` 当变量解析）。
2. **敏感信息默认占位**（Cookie/Authorization/token 字段），明文绝不写进 YAML。
3. **动态值只提示不自动改写**（时间戳/UUID 等），自动改写会让用例失去原来的语义。
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Text, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse

from interfacetester.converters.ir import (
    IRCase,
    IRStep,
    duplicated_names,
    merge_variables,
    origin_of,
    safe_file_stem,
    sanitize_body,
    sanitize_case,
    sanitize_cookies,
    sanitize_headers,
    split_url,
    warn_dynamic_values,
    warn_source_expressions,
    without_userinfo,
)

# 静态资源扩展名（浏览器录制里必然混进来）
STATIC_EXTENSIONS = {
    ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf", ".map", ".mp4", ".mp3", ".webm",
    ".avif", ".bmp", ".pdf",
}

# `_resourceType` 里明确**不是业务接口**的类型（0920 批次 7 / **N43**）。
#
# NOTICE（为什么不止 Chrome 的词汇）：这个字段**不是 HAR 规范的一部分**
# （是 Chrome 的扩展字段），所以别的工具各写各的：
#
# | 工具 | 取值形态 |
# | --- | --- |
# | Chrome / Firefox | `document` / `stylesheet` / `script` / `image` / `font` / `media` / `websocket` / `manifest` / `other` |
# | Fiddler | 首字母大写（`Stylesheet` / `Script` / `Image` …）—— 已由 `.lower()` 覆盖 |
# | Charles Proxy | **按扩展名**给（`css` / `js` / `png` / `gif` / `html` …） |
# | Insomnia / Postman | `XHR` / `Document` / `Other`（`.lower()` 后可覆盖） |
# | 部分工具 | 直接没有这个字段（`""`）→ 只能靠扩展名过滤 |
#
# 修复前只认 Chrome 那 8 个词 —— 于是 **Charles 录制的 CSS/JS/图片全部漏进用例**
# （实测 `.tmp_audit/n43_check.py`：`css`/`js`/`png`/`gif`/`html` 都被当成接口）。
#
# NOTICE（**刻意不**把 `document` / `xhr` / `fetch` / `json` / `xml` / `text` 放进来）：
#   - `xhr` / `fetch` 是**真正的接口请求**（Chrome 把 fetch/XHR 标成它们）；
#   - `document` 可能是 HTML 页面（静态），但也可能是**返回 JSON 的接口**
#     （取决于 Accept/内容），放进来会误杀真接口；
#   - `json` / `xml` / `text` 是 Charles 按**响应类型**给的 —— 而接口返回的正是它们。
# 判断"是不是静态资源"交给下面的**扩展名**规则（那才是精确判据），
# 本集合只收"确定不可能是业务接口"的类型。
NON_API_RESOURCE_TYPES = {
    # Chrome / Firefox
    "stylesheet", "script", "image", "font", "media", "websocket", "manifest", "other",
    # Charles Proxy 的扩展名式取值（静态的那几个；json/xml/text/html 见上面的 NOTICE）
    "css", "js", "png", "jpg", "jpeg", "gif", "ico", "svg", "webp", "woff", "woff2",
    "ttf", "eot", "otf", "mp4", "mp3", "webm", "avif", "bmp",
}

# HTTP/2 伪头（HAR 里会出现，框架不认也不能写进 headers）
PSEUDO_HEADER_PREFIX = ":"

# 由客户端/传输层**派生**的头：录下来的值属于那一次连接，回放时必须丢掉
# （`Host`/`Content-Length` 尤其危险：写死会让请求打到错误虚拟主机、或长度不符导致服务端挂起）
DERIVED_HEADERS = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "accept-encoding",
    "cookie",  # Cookie 走 request.cookies（占位化），不再重复放请求头
    "upgrade-insecure-requests",
}
DERIVED_HEADER_PREFIXES = ("sec-fetch-", "sec-ch-ua")

# 响应里这些**服务端/网关生成**的字段不适合做形状断言（每次都可能不同）
VOLATILE_SUBTREES = {"headers"}

# 不参与用例的请求方法
SKIPPED_METHODS = {"OPTIONS"}

# NOTICE（0920 / 缺陷 3）：**重定向中间态**的状态码。
# 框架的 `TRequest.allow_redirects` 默认 True，requests 会一路跟到最终响应 ——
# 于是"忠实回放录到的 302"必然失败：断言 `eq: [status_code, 302]` 打的是**最终**的
# 200（实测 `assert status_code equal 302 ==> fail`）。这类条目不是"业务接口的期望"，
# 而是重定向链的中间态，因此**整条跳过**并告警（用户要断 3xx 就显式关掉
# allow_redirects 手写这一步）。
REDIRECT_STATUSES = {301, 302, 303, 307, 308}

# schema 推断上限（避免把整个大报文摊平成巨型 schema）
MAX_SCHEMA_DEPTH = 6
MAX_SCHEMA_PROPERTIES = 100
MAX_BODY_CHARS = 200_000


def infer_schema(
    sample: Any,
    depth: int = 0,
    key: Text = "",
    warnings: Optional[List[Text]] = None,
) -> Dict[str, Any]:
    """从真实响应推断「形状」schema（type + required + properties/items）。

    NOTICE:
    - 这是**结构断言**，不是契约：类型与必填来自这一次录制，数组只按第一个元素推断
      （空数组只给 `type: array`），不做枚举/边界值约束；
    - **`headers` 这类服务端生成的子树只断类型**：网关/追踪头（`x-amzn-trace-id`、
      `date`…）每次都可能不同，把它们写成 `required` 会让回放必然失败（实测踩过）。

    NOTICE（批次 9-1 / 截断必须可见）：`warnings` 是**可选**参数，传入时会在两处截断
    （嵌套超过 `MAX_SCHEMA_DEPTH`、属性数超过 `MAX_SCHEMA_PROPERTIES`）发出告警。
    修复前这两处都是**静默**的，而截断出来的空 schema 等价于「任意值」——
    断言比录制到的形状更宽松，用例照样绿。OpenAPI 侧的同类截断（`MAX_PROPERTIES`）
    一直是告警的，这里补齐成同一口径。
    """
    if depth >= MAX_SCHEMA_DEPTH:
        if warnings is not None:
            _warn_once(
                warnings,
                f"响应体嵌套超过 {MAX_SCHEMA_DEPTH} 层，超出部分已**截断**"
                f"（该处的形状断言退化成「任意值」）。",
            )
        return {}

    if key.lower() in VOLATILE_SUBTREES:
        return {"type": "object"} if isinstance(sample, dict) else {}

    if isinstance(sample, bool):
        return {"type": "boolean"}
    if isinstance(sample, int):
        return {"type": "integer"}
    if isinstance(sample, float):
        return {"type": "number"}
    if isinstance(sample, str):
        return {"type": "string"}
    if sample is None:
        return {}  # 无法从 null 判断类型

    if isinstance(sample, list):
        schema: Dict[str, Any] = {"type": "array"}
        if sample:
            item_schema = infer_schema(sample[0], depth + 1, warnings=warnings)
            if item_schema:
                schema["items"] = item_schema
        return schema

    if isinstance(sample, dict):
        properties: Dict[str, Any] = {}
        for index, (child_key, value) in enumerate(sample.items()):
            if index >= MAX_SCHEMA_PROPERTIES:
                if warnings is not None:
                    _warn_once(
                        warnings,
                        f"响应体属性过多（{len(sample)} 个 > {MAX_SCHEMA_PROPERTIES}），"
                        f"形状断言只保留前 {MAX_SCHEMA_PROPERTIES} 个属性",
                    )
                break
            properties[str(child_key)] = infer_schema(
                value, depth + 1, str(child_key), warnings
            )
        schema = {"type": "object"}
        if properties:
            schema["properties"] = properties
            schema["required"] = sorted(properties)
        return schema

    return {}


def _warn_once(warnings: List[Text], message: Text) -> None:
    """按整串去重地追加告警（与 openapi 适配器的 `_warn` 同口径）。

    NOTICE: 去重是必要的 —— `infer_schema` 是递归的，深层/宽报文会在每一层命中同一个
    截断分支；不去重会刷出几十行同样含义的告警，把真正的告警淹掉。
    """
    if message not in warnings:
        warnings.append(message)


def _extract_response_text(content: Dict[str, Any]) -> Tuple[Text, Optional[Text]]:
    """取出响应体文本，返回 ``(text, 告警)``。

    NOTICE: **HAR 里的响应体经常是 base64**（Chrome 导出、二进制内容时）——
    实测仓库里的 `examples/data/har/demo.har` 三条 entry 全是 `encoding: base64`，
    不先解码就会「声明是 JSON 却解析失败」，白白丢掉形状断言。

    NOTICE（批次 8 / **L21**）：`content` 不是对象、或 `text` 不是字符串时，
    修复前 `content.get(...)` / `text.lstrip()` 会抛 `AttributeError`、**整批导入崩掉**。
    现在按「响应体取不到」处理：返回空文本 + 一条告警，**条目本身照常导入**
    （状态码断言不受影响）。
    """
    if not isinstance(content, dict):
        return "", (
            f"响应体 `content` 不是对象（{type(content).__name__}）→ "
            f"已跳过形状断言生成（状态码断言不受影响）"
        )
    raw = content.get("text") or ""
    if not raw:
        return "", None
    if not isinstance(raw, str):
        return "", (
            f"响应体 `content.text` 不是字符串（{type(raw).__name__}）→ "
            f"已跳过形状断言生成（状态码断言不受影响）"
        )
    if str(content.get("encoding") or "").lower() == "base64":
        import base64  # noqa: PLC0415

        try:
            decoded = base64.b64decode(raw)
        except Exception as ex:  # noqa: BLE001
            return "", f"响应体是 base64 但解码失败（{type(ex).__name__}: {ex}），已跳过形状断言"
        # NOTICE（0920 批次 7 / **N42**）：base64 解出来的可能是**压缩体**。
        #
        # HAR 规范的 `content.compression` 表示"这具响应体被压缩了多少字节"
        # （社区约定：**非 0 即表示 gzip**），而 Chrome/Firefox 导出时
        # 常见的组合就是 `encoding: base64` + 压缩 + `compression: <size>`。
        # 修复前完全不看这个字段 —— 于是解出来的 gzip 字节按 UTF-8 解失败，
        # 被当成"二进制内容"**静默丢掉形状断言**（实测 `.tmp_audit/n42_check.py`：
        # 只断状态码、`jsonschema_match` 整条消失），而告警还写着
        # "base64 二进制/非 UTF-8" —— 把用户引向"这接口返回二进制"的错误结论。
        decoded, decompress_warning = _maybe_decompress(decoded, content)
        if decompress_warning:
            return "", decompress_warning
        try:
            return decoded.decode("utf-8"), None
        except UnicodeDecodeError:
            # 二进制内容（图片/字体等）：不是 JSON，也不该生成形状断言
            return "", None
    return raw, None


def _maybe_decompress(
    decoded: bytes, content: Dict[Text, Any]
) -> Tuple[bytes, Optional[Text]]:
    """按 HAR 的 `content.compression` 解压（0920 批次 7 / **N42**）。

    HAR 规范里 `content.compression` 是**数字**（"响应体压缩后省下多少字节"），
    社区约定**非 0 即 gzip**；部分工具（含 Postman/Insomnia 的导出）
    把它写成 `"gzip"` / `"deflate"` / `"br"` 字符串。两种都认。

    返回 ``(解压后的字节, 告警或 None)``。**无法确定时一律不猜**：
    - 没有 `compression`（0/缺失）→ 原样返回（既有行为）；
    - 有 `compression` 但解压失败 → 原样返回 + 告警（让用户知道"这里本该是压缩体"，
      而不是像修复前那样把它当二进制静默丢掉）。

    NOTICE: **不用"猜 magic bytes"兜底**。gzip 有 `\\x1f\\x8b` 魔术字，但无压缩的
    二进制响应也可能恰好以它开头；本仓的口径是"按声明的字段判断，不猜"。
    """
    raw_compression = content.get("compression")
    if raw_compression in (None, "", 0, "0"):
        return decoded, None

    # 认两种声明形态：数字（非 0 = gzip）与字符串（工具写出的算法名）
    if isinstance(raw_compression, str):
        algorithm = raw_compression.strip().lower()
        if algorithm.isdigit():
            algorithm = "gzip" if int(algorithm) != 0 else ""
    else:
        algorithm = "gzip"

    if algorithm in ("", "none", "identity"):
        return decoded, None

    import gzip  # noqa: PLC0415
    import zlib  # noqa: PLC0415

    # NOTICE（顺序与回退）：**数字形态没有算法名**（HAR 规范只说"压缩了多少字节"）。
    # 社区约定默认按 gzip，但实测导出工具也会给出 zlib/deflate
    # （`zlib.compress` 的 `78 9c` 开头，用 gzip 解会 `BadGzipFile`）。
    # 所以按"gzip → zlib 包装 → 裸 deflate"依次**试**，全部失败才报错。
    # 这不是"猜 magic bytes"：`compression` 已经**声明了这里就是压缩体**，
    # 试哪个算法只是在同一事实下选出可用的解压器。
    attempts = ["gzip", "deflate"] if algorithm == "gzip" else [algorithm]

    last_error: Optional[Exception] = None
    for candidate in attempts:
        try:
            if candidate in ("gzip", "x-gzip"):
                return gzip.decompress(decoded), None
            if candidate in ("deflate", "x-deflate"):
                try:
                    return zlib.decompress(decoded), None
                except zlib.error:
                    # 裸 deflate（无 zlib 头）
                    return zlib.decompress(decoded, -zlib.MAX_WBITS), None
            if candidate in ("br", "brotli"):
                import brotli  # noqa: PLC0415

                return brotli.decompress(decoded), None
        except ImportError:
            return decoded, (
                f"响应体声明了 `content.compression: {raw_compression!r}`，"
                f"但当前环境没有解压算法 {candidate!r} 的依赖 → 已跳过形状断言生成"
                f"（状态码断言不受影响）。"
                f"hint: `pip install brotli`（br 格式）"
            )
        except Exception as ex:  # noqa: BLE001
            last_error = ex

    return decoded, (
        f"响应体声明了 `content.compression: {raw_compression!r}`，但解压失败"
        f"（{type(last_error).__name__}: {last_error}）→ 已跳过形状断言生成"
        f"（状态码断言不受影响）。\n"
        f"  hint: 已按 {attempts} 依次尝试仍不成功，说明该字段与内容不符"
        f"（例如声明了压缩实际未压缩）；请核对导出源，或手工在 validate 里补断言。"
    )


def _response_is_json(text: Text) -> bool:
    """是否该按 JSON 生成形状断言：以「解码后的文本看得出是 JSON」为准。

    NOTICE: 只看 mimeType 会把「声明 JSON 但内容其实是 base64/截断」也算进来，
    于是走到解析失败分支（实测 demo.har 踩过：三条 entry 都是 base64），
    所以这里以文本形态为准，mimeType 只在告警里出现。
    """
    stripped = (text or "").lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _parse_json_text(text: Text) -> Optional[Any]:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _har_headers(
    har_headers: List[Dict[Text, Any]],
) -> Tuple[Dict[Text, Any], List[Text], List[Text]]:
    """HAR 头列表 → dict（跳过伪头与**派生头**），返回 ``(headers, 被丢弃的头名, 形态问题)``。

    NOTICE: 派生头（`Host`/`Content-Length`/`Connection`/`Cookie`/`Accept-Encoding`/`Sec-Fetch-*`）
    录下来的是**那一次连接**的状态，回放时由 requests 自己决定；
    尤其 `Content-Length` 写死会让请求体长度不符（服务端可能一直等或直接 400）。

    NOTICE（批次 8 / **L21**）：`headers` 整体不是列表、或列表里混进非对象条目时，
    修复前 `item.get(...)` 抛 `AttributeError` → **整批导入崩掉**。
    现在跳过那些坏条目并回报（第三个返回值），请求本身照常导入。
    """
    headers: Dict[Text, Any] = {}
    dropped: List[Text] = []
    shape: List[Text] = []
    if not isinstance(har_headers, list):
        return headers, dropped, [f"`headers` 不是列表（{type(har_headers).__name__}）"]
    for index, item in enumerate(har_headers):
        if not isinstance(item, dict):
            shape.append(f"headers[{index}] 不是对象（{type(item).__name__}）")
            continue
        name = str(item.get("name", "")).strip()
        if not name or name.startswith(PSEUDO_HEADER_PREFIX):
            continue
        lowered = name.lower()
        if lowered in DERIVED_HEADERS or lowered.startswith(DERIVED_HEADER_PREFIXES):
            if name not in dropped:
                dropped.append(name)
            continue
        headers.setdefault(name, item.get("value", ""))
    return headers, dropped, shape


def _har_cookies(
    har_cookies: List[Dict[Text, Any]],
) -> Tuple[Dict[Text, Any], List[Text]]:
    """HAR 的 `cookies` → dict；返回 ``(cookies, 形态问题)``（批次 8 / L21 同族）。"""
    cookies: Dict[Text, Any] = {}
    shape: List[Text] = []
    if not isinstance(har_cookies, list):
        return cookies, [f"`cookies` 不是列表（{type(har_cookies).__name__}）"]
    for index, item in enumerate(har_cookies):
        if not isinstance(item, dict):
            shape.append(f"cookies[{index}] 不是对象（{type(item).__name__}）")
            continue
        name = item.get("name")
        if name:
            cookies[str(name)] = item.get("value", "")
    return cookies, shape


def _har_query_params(
    query_string: List[Dict[Text, Any]],
) -> Tuple[Dict[Text, Any], bool, List[Text]]:
    """HAR 的 `queryString` → `params`；返回 ``(params, 是否有重名键, 形态问题)``。

    NOTICE（0919-2 / 缺陷 8）：判重改走共用的 `duplicated_names`（原本是这里的内联判重）。
    语义逐字保持：**跳过 `name` 为 None 的项、重名时保留首个值**。

    NOTICE（批次 8 / **L21**）：`queryString` 不是列表、或列表里混进非对象条目时，
    修复前 `item.get(...)` 抛 `AttributeError` → **整批导入崩掉**。现在跳过并回报。
    """
    shape: List[Text] = []
    if not isinstance(query_string, list):
        return {}, False, [f"`queryString` 不是列表（{type(query_string).__name__}）"]
    pairs = []
    for index, item in enumerate(query_string):
        if not isinstance(item, dict):
            shape.append(f"queryString[{index}] 不是对象（{type(item).__name__}）")
            continue
        if item.get("name") is not None:
            pairs.append((str(item.get("name")), item.get("value", "")))

    params: Dict[Text, Any] = {}
    for name, value in pairs:
        params.setdefault(name, value)  # 重名时保留**首个**（与修复前一致）

    return params, bool(duplicated_names(pairs)), shape


def _form_pairs(
    params: List[Dict[Text, Any]],
) -> Tuple[List[Tuple[Text, Any]], List[Text]]:
    """HAR `postData.params` → `(字段名, 值)` 序列（保留全部重复项）。

    NOTICE: `params` 里的顺序与重复都必须保住 —— 它是「录到的那次请求」的唯一证据，
    转成 dict 之后重复项就再也回不来了。

    NOTICE（批次 8 / **L21**）：非对象条目跳过并回报，不再让整批崩。
    """
    if not isinstance(params, list):
        return [], [f"`postData.params` 不是列表（{type(params).__name__}）"]
    shape: List[Text] = []
    pairs: List[Tuple[Text, Any]] = []
    for index, item in enumerate(params):
        if not isinstance(item, dict):
            shape.append(f"postData.params[{index}] 不是对象（{type(item).__name__}）")
            continue
        if item.get("name"):
            pairs.append((str(item.get("name", "")), item.get("value", "")))
    return pairs, shape


def _keep_raw_form(
    step: IRStep, pairs: List[Tuple[Text, Any]], raw_text: Any, field: Text
) -> None:
    """重名字段：告警 + 把**原始表单串**留在 `data` 里（信息不丢）。

    与 `queryString` 那条口径完全一致（M33）：字典装不下重名 → 就不拆成字典。
    `raw_text` 优先（录到的就是它）；没有原始串时按 `(名, 值)` 序列重建。
    """
    step.data = raw_text or urlencode(pairs)
    step.add_warning(
        f"{field} 里有重名键 {duplicated_names(pairs)}，字典形式的 `data` 装不下"
        f"（只保留最后一个值会让回放**少发几个字段**，批量接口上等于少删/少查）→ "
        f"已把表单串**原样**放进 `data`（与 HAR `queryString` 的重名处理同口径）"
    )


def _apply_post_data(step: IRStep, post_data: Dict[Text, Any]) -> None:
    """HAR 的 `postData` → `json` / `data` / `upload`。

    NOTICE（批次 8 / **L21**）：`postData` 不是对象、或 `params` 里混进非对象条目时，
    修复前 `post_data.get(...)` / `item.get(...)` 抛 `AttributeError` → **整批导入崩掉**。
    现在：`postData` 不是对象 → 告警后按「没有请求体」处理；坏条目 → 跳过并告警，
    请求本身照常导入（字段级坏数据不牵连整条）。
    """
    if not post_data:
        return
    if not isinstance(post_data, dict):
        step.add_warning(
            f"`request.postData` 不是对象（{type(post_data).__name__}）→ "
            f"已按「没有请求体」处理（请求本身照常导入）"
        )
        return

    mime_type = str(post_data.get("mimeType") or "").lower()
    text = post_data.get("text")
    params = post_data.get("params") or []
    if not isinstance(params, list):
        step.add_warning(
            f"`postData.params` 不是列表（{type(params).__name__}）→ 已忽略这些表单字段"
        )
        params = []
    bad_items = [
        index for index, item in enumerate(params) if not isinstance(item, dict)
    ]
    if bad_items:
        step.add_warning(
            f"`postData.params` 里有 {len(bad_items)} 个非对象条目（下标 {bad_items}）→ 已跳过"
        )
        params = [item for item in params if isinstance(item, dict)]

    if "multipart/form-data" in mime_type:
        fields: Dict[Text, Any] = {}
        uploads: Dict[Text, Text] = {}
        multipart_pairs: List[Tuple[Text, Any]] = []
        for item in params:
            name = str(item.get("name", ""))
            if not name:
                continue
            multipart_pairs.append((name, item.get("fileName") or item.get("value", "")))
            if item.get("fileName"):
                uploads[name] = item.get("fileName") or ""
            else:
                fields[name] = item.get("value", "")
        if fields:
            step.data = fields
        if uploads:
            step.upload = uploads
            step.add_warning(
                "multipart 里的文件字段已映射到 `upload`（值来自录制的 fileName，"
                "请确认本地文件路径真实存在）"
            )
        # NOTICE（0919-2 / 缺陷 8，**只做可见化**）：multipart 的重复字段同样会被字典吃掉，
        # 但这里**无法**像 urlencoded 那样退回原始串——`upload` 是 `Dict`（一个字段名只能
        # 对一个文件），重建 multipart 报文体也不是本导入器的职责。所以只告警，如实登记。
        multipart_duplicated = duplicated_names(multipart_pairs)
        if multipart_duplicated:
            step.add_warning(
                f"multipart 里有重名字段 {multipart_duplicated} → 字典装不下，"
                f"回放时会**少发**若干部分（`upload`/`data` 每个字段名只能有一个值）；"
                f"请按实际需要手工补"
            )
        if not params and text:
            step.add_warning(
                "multipart 请求体没有结构化字段（只有原始 text），已无法自动拆分成 upload，"
                "请人工补 `request.upload`"
            )
        return

    if "x-www-form-urlencoded" in mime_type:
        if params:
            pairs, _param_shape = _form_pairs(params)
            if duplicated_names(pairs):
                _keep_raw_form(step, pairs, text, "postData.params")
            else:
                step.data = dict(pairs)
        elif text:
            pairs = parse_qsl(text, keep_blank_values=True)
            if duplicated_names(pairs):
                _keep_raw_form(step, pairs, text, "postData.text")
            else:
                step.data = dict(pairs)
        return

    if "json" in mime_type and text:
        parsed = _parse_json_text(text)
        if parsed is not None:
            step.json_body = parsed
        else:
            step.data = text
            step.add_warning("postData 声称是 JSON 但解析失败，已按原始文本放进 `data`")
        return

    if text:
        step.data = text


def _entry_shape_problem(entry: Any) -> Optional[Text]:
    """条目 / `request` / `response` **对象本身**形态不对 → 返回原因（None = 形态可用）。

    NOTICE（批次 8 / **L21**，与 L5 同族根因）：修复前这里**没有**任何形态检查，
    `entry.get(...)` 在非对象上直接抛 `AttributeError`，冒到 CLI 就是
    `ERROR | 解析 <文件> 失败：AttributeError: 'str' object has no attribute 'get'`
    → 整批 exit 2、`--out` 目录不创建：**一条坏条目带走整份 HAR**（实测
    `.tmp_report/probe_l5b.py`：entry / request / response 三类各有非对象形态，
    另有 `request.url` 缺失的条目会静默生成一个 `url: /` 的空步骤）。
    现在的口径：**跳过这一条条目 + 逐条告警**，其余条目照常导入。
    """
    if not isinstance(entry, dict):
        return f"条目不是对象（{type(entry).__name__}）"
    request = entry.get("request")
    if not isinstance(request, dict):
        return f"`request` 不是对象（{type(request).__name__}）"
    if not str(request.get("url") or "").strip():
        return "`request.url` 缺失/为空（无法回放这个请求）"
    response = entry.get("response")
    if response is not None and not isinstance(response, dict):
        return f"`response` 不是对象（{type(response).__name__}）"
    return None


def _post_data_dedupe_hint(post_data: Any) -> Any:
    """把 `request.postData` 归一成**可比较**的去重键成分（0920 批次 4 / N20）。

    覆盖三种录制形态（缺一不可，否则那一种就会被误判成"重复"）：

    | 形态 | 取什么 |
    | --- | --- |
    | `text`（JSON / 纯文本 / urlencoded 原文） | 原文 |
    | `params`（表单 / multipart 的字段数组） | `[(name, value, fileName), ...]` 按**出现顺序** |
    | 两者都有 | 两者都要（浏览器导出的 HAR 常常同时带） |

    NOTICE: 顺序敏感是有意的 —— `a=1&b=2` 与 `b=2&a=1` 对多数服务端等价，
    但"顺序不同 ⇒ 判成两条不同录制"只是**少去重**（无害，用户可手工删）；
    反过来漏掉一个维度就会**丢录制**（有害）。宁可少去重，不可误去重。
    """
    if not isinstance(post_data, dict):
        return ""

    hint: Dict[Text, Any] = {}
    text = post_data.get("text")
    if text:
        hint["text"] = text

    params = post_data.get("params")
    if isinstance(params, list):
        pairs = []
        for item in params:
            if not isinstance(item, dict):
                # 形态不对的条目：`_apply_post_data` 会告警；这里用 repr 兜住，
                # 保证"两条录制的 params 不一样"这件事仍然能被区分开。
                pairs.append(repr(item))
                continue
            pairs.append(
                (
                    str(item.get("name") or ""),
                    str(item.get("value") or ""),
                    str(item.get("fileName") or ""),
                )
            )
        if pairs:
            hint["params"] = pairs

    if not hint:
        return ""
    return hint


def _render_query_pairs(query_string: Any) -> Text:
    """把 HAR 的 `queryString` 数组渲染成查询串（0920 批次 4 / N22）。

    用途：URL 自己不带查询串、而 `queryString` 又有重名键时，用它**重建**查询串
    （重名键只能靠原始查询串保留，字典装不下）。

    NOTICE: 用 `urlencode` 而不是手工拼 `&` —— 值里的 `&`/`=`/中文必须转义，
    否则重建出来的查询串会被服务端解析成**别的参数**（比丢参数更糟）。
    没有 `name` 的坏条目跳过（`_har_query_params` 已对它们发过告警）。
    """
    if not isinstance(query_string, list):
        return ""

    pairs = []
    for item in query_string:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name is None or str(name) == "":
            continue
        pairs.append((str(name), str(item.get("value", ""))))

    return urlencode(pairs) if pairs else ""


def _should_skip_entry(
    entry: Dict[str, Any], include_hosts: Optional[List[Text]], exclude_hosts: Optional[List[Text]]
) -> Optional[Text]:
    """返回跳过原因（None 表示保留）。

    NOTICE（批次 8 / **L5**）：这里**不能**直接 `int(response.get("status") or 0)`。
    修复前是 `if int(response.get("status") or 0) == 0:` —— 手改/第三方工具的 HAR
    可能把状态码写成 `"200 OK"`，于是 `ValueError` 冒到 CLI：

        ERROR | 解析 <文件> 失败：ValueError: invalid literal for int() with base 10: '200 OK'

    整批 exit 2、`--out` 目录根本不创建（实测 `.tmp_report/probe_l5.py`：一条坏条目
    带走同一份 HAR 里完好的条目）。现在：
    - **数字/数字字符串** 才在这里判「被中止」（`status == 0`，既有口径不变）；
    - **非数字** 不是「非业务请求」→ 不在这里跳过，交给 `convert_har` 的坏数据分支
      （保留条目、不生成状态码断言、逐条告警）。
    """
    request = entry.get("request") or {}
    response = entry.get("response") or {}
    url = str(request.get("url") or "")
    method = str(request.get("method") or "GET").upper()
    host = urlparse(url).netloc.lower()

    if method in SKIPPED_METHODS:
        return f"{method} 是预检请求，不是业务接口"

    if include_hosts and not any(pattern in host for pattern in include_hosts):
        return f"host {host} 不在 --include-hosts 里"
    if exclude_hosts and any(pattern in host for pattern in exclude_hosts):
        return f"host {host} 命中 --exclude-hosts"

    resource_type = str(entry.get("_resourceType") or "").lower()
    if resource_type in NON_API_RESOURCE_TYPES:
        return f"资源类型 {resource_type} 不是接口请求"

    path = urlparse(url).path.lower()
    for extension in STATIC_EXTENSIONS:
        if path.endswith(extension):
            return f"静态资源（{extension}）"

    raw_status = response.get("status")
    if raw_status in (None, ""):
        # 0919-26 文案订正：原文案一口咬定「请求被中止」，但**录制里没有 status**
        # 与「status=0（确实被中止）」是两回事（同 L7 的归因口径：不能把缺失说成别的）。
        return "请求被中止、未收到响应，或录制里没有 `response.status`"
    try:
        numeric_status = int(raw_status)
    except (TypeError, ValueError):
        return None
    if numeric_status == 0:
        return "请求被中止或未收到响应（status=0）"

    # NOTICE（0920 / 缺陷 3）：重定向中间态跳过见 REDIRECT_STATUSES 的注释。
    if numeric_status in REDIRECT_STATUSES:
        location = ""
        for header in response.get("headers") or []:
            if isinstance(header, dict) and str(header.get("name", "")).lower() == "location":
                location = str(header.get("value") or "")
                break
        target = f"（Location: {location}）" if location else ""
        return (
            f"重定向中间态（{numeric_status}）{target} → 已跳过该条目："
            f"框架默认 allow_redirects 会跟随到最终响应，"
            f"照录制断 `status_code == {numeric_status}` 必然失败"
        )

    return None


def _parse_recorded_status(raw_status: Any) -> Tuple[Optional[int], Optional[Text]]:
    """录制的 `response.status` → ``(状态码, 告警)``；取不到状态码时为 ``None``。

    NOTICE（批次 8 / **L5**）：修复前这里是裸 `int(response.get("status") or 0)`，
    非数字直接 `ValueError` → **整批导入崩掉**。现在的判据（与 L7「非正数不是状态码」
    同口径）：

    | 形态 | 处置 |
    | --- | --- |
    | `200` / `"200"` / `200.0` | 可用（数字字符串是常见录制形态；`200.0` 是整数值） |
    | `"200 OK"` / `"abc"` / `None` / `""` | 取不到 → 告警 + **不生成状态码断言** |
    | `200.7` / `True` / `-1` / `0` | **不是状态码** → 告警 + 不生成断言（修复前会被 `int()` 静默截成 `200`/`1`/`-1`） |

    `status == 0`（被中止）在 `_should_skip_entry` 里已被跳过，走不到这里。
    """
    if isinstance(raw_status, bool):
        return None, (
            f"录制的 `response.status` 是布尔值（{raw_status!r}）→ **没有生成状态码断言**"
            f"（其余部分照常导入）。"
        )
    if raw_status in (None, ""):
        return None, None  # 条目已被 `_should_skip_entry` 跳过，正常走不到
    if isinstance(raw_status, int):
        numeric = raw_status
    elif isinstance(raw_status, float):
        if not raw_status.is_integer():
            return None, (
                f"录制的 `response.status` 不是整数状态码（{raw_status!r}）→ "
                f"**没有生成状态码断言**（其余部分照常导入）。"
            )
        numeric = int(raw_status)
    elif isinstance(raw_status, str):
        stripped = raw_status.strip()
        if not stripped.isdigit():
            return None, (
                f"录制的 `response.status` 不是数字（{raw_status!r}）→ "
                f"**没有生成状态码断言**（其余部分照常导入）。\n"
                f"  `status` 应当是 HTTP 状态码（如 200）；HAR 规范里它是数字，"
                f"请核对导出工具或手工改过的 HAR。"
            )
        numeric = int(stripped)
    else:
        return None, (
            f"录制的 `response.status` 形态不对（{type(raw_status).__name__}）→ "
            f"**没有生成状态码断言**（其余部分照常导入）。"
        )
    if numeric <= 0:
        return None, (
            f"录制的 `response.status` 不是正数（{numeric}）→ **没有生成状态码断言**"
            f"（其余部分照常导入）。占位值不能当期望状态码。"
        )
    return numeric, None


def _schema_file_name(case_stem: Text, index: int, step_name: Text) -> Text:
    """`schemas/<用例词干>_<序号>_<步骤词干>.json`。

    NOTICE（批次 9-1 / H5 收口）：步骤词干必须走 `ir.safe_file_stem`。
    修复前这里写的是 `re.sub(r"[^0-9A-Za-z]+", "_", step_name)` —— 正是 H5 修掉的
    「把非 ASCII 整段抹掉」那一条：`"查询用户"` 与 `"查询订单"` 都会退化成 `step`
    （文件名只剩序号能区分）。同包内 Postman 的步骤词干（`_schema_stem`）与 OpenAPI 的
    用例词干早已改用 `safe_file_stem`，只有 HAR/OpenAPI 的**步骤**词干漏了，
    同一个子系统里三处两种规则 —— 现在统一为一种。
    """
    return f"schemas/{case_stem}_{index:02d}_{safe_file_stem(step_name, default='step')}.json"


def convert_har(
    har: Dict[str, Any],
    case_name: Text = "HAR imported case",
    source: Text = "",
    assertion_mode: Text = "status+schema",
    include_hosts: Optional[List[Text]] = None,
    exclude_hosts: Optional[List[Text]] = None,
    dedupe: bool = True,
    case_stem: Text = "case",
) -> IRCase:
    """HAR（dict）→ `IRCase`。

    Args:
        assertion_mode: `status+schema`（默认）/ `status`（只断状态码）/ `none`
        include_hosts / exclude_hosts: 按 host 子串过滤（先 include 后 exclude）
        dedupe: 去掉「方法+URL+请求体」完全相同的重复录制
    """
    case = IRCase(name=case_name, source=source)

    # NOTICE（批次 E / **L1**）：HAR 的**文档根**与 `log` 也要做形态护栏。
    # 修复前 `(har or {}).get("log")` 假定 `har` 是对象、`log` 也是对象：
    # `har` 是列表/字符串时抛裸 `AttributeError: 'list' object has no attribute 'get'`，
    # 而 `log` 写成字符串时下面那句 `.get("version")`/`.get("entries")` 同样裸崩 ——
    # 用户看到的是 traceback，看不出是「HAR 的哪个字段形态不对」。
    if not isinstance(har, dict):
        case.add_warning(
            f"HAR 文档根应该是 JSON 对象，实际是 {type(har).__name__} → 无法导入"
        )
        return case

    log = har.get("log")
    if not isinstance(log, dict):
        case.add_warning(
            "HAR 的 `log` 应该是 JSON 对象"
            f"（实际是 {type(log).__name__ if log is not None else '缺失'}）→ 无法导入。\n"
            '  提示：HAR 的结构是 `{"log": {"version": "1.2", "entries": [...]}}`，'
            "`entries` 必须挂在 `log` 下面。"
        )
        return case

    entries = log.get("entries") or []
    if not isinstance(entries, list):
        case.add_warning(
            f"HAR 的 `log.entries` 应该是数组，实际是 {type(entries).__name__} → 无法导入"
        )
        return case
    if not entries:
        case.add_warning("HAR 里没有 log.entries，无法导入")
        return case

    version = log.get("version")
    if version and str(version) != "1.2":
        case.add_warning(f"HAR 版本是 {version}（本导入器按 1.2 语义解析，未知字段会被忽略）")

    skipped: List[Text] = []
    broken: List[Text] = []
    duplicates = 0
    seen_keys: set = set()
    step_index = 0

    for entry_index, entry in enumerate(entries, start=1):
        # 批次 8 / L21：形态不对的条目单独一桶（**不能**混进「非业务请求」——
        # 那是「这条录制不是接口」，与「这条录制坏了」是两回事，混桶会把用户引向
        # 错误的排查方向，与 L7 的归因订正同一口径）。
        shape_problem = _entry_shape_problem(entry)
        if shape_problem:
            broken.append(f"entry #{entry_index}：{shape_problem}")
            continue

        skip_reason = _should_skip_entry(entry, include_hosts, exclude_hosts)
        if skip_reason:
            skipped.append(f"entry #{entry_index}：{skip_reason}")
            continue

        request = entry.get("request") or {}
        response = entry.get("response") or {}
        url = str(request.get("url") or "")
        method = str(request.get("method") or "GET").upper()

        if dedupe:
            # 批次 8 / L21：`postData` 不是对象时这里也曾经裸崩（`.get` on str）——
            # 去重键取不到请求体时按空串处理（`_apply_post_data` 会另发一条告警）。
            #
            # NOTICE（0920 批次 4 / **N20**）：去重键必须覆盖**结构化的** `postData`
            # （`params` 数组），不能只看 `text`。表单/multipart 的录制常常**只有
            # `params`、没有 `text`** —— 修复前的键是 `(method, url, "")`，
            # 于是**两条内容完全不同**的请求被判成"重复"，第二条**整条被丢掉**，
            # 而汇总告警还写着"跳过了 N 条『方法+URL+请求体』**完全相同**的重复录制"
            # （告警把用户引向"确实是重复录制"这个错误结论）。
            #
            # 实测（`.tmp_audit/aud4/probe_har_dedupe.py`）：
            #   `/up` 两条 multipart 录制（doc=a.pdf note=1 / doc=b.pdf note=2）
            #   → dedupe=True 时只剩 1 步、uploads 只剩 {'doc': 'a.pdf'}
            #   → dedupe=False 时正常 2 步
            #
            # 现在把 `params`（字段名+值+fileName，按出现顺序）也纳入键，
            # 只有**真正逐项相同**的录制才判重复。
            body_hint = json.dumps(
                _post_data_dedupe_hint(request.get("postData")), sort_keys=True
            )
            key = (method, url, body_hint)
            if key in seen_keys:
                duplicates += 1
                continue
            seen_keys.add(key)

        step_index += 1
        step = IRStep(
            name=f"{method} {urlparse(url).path or '/'}",
            method=method,
            source=f"entry #{entry_index}",
        )

        headers, dropped_headers, header_shape = _har_headers(
            request.get("headers") or []
        )
        cookies, cookie_shape = _har_cookies(request.get("cookies") or [])
        params, duplicated_params, query_shape = _har_query_params(
            request.get("queryString") or []
        )
        # 批次 8 / L21：字段级形态问题只影响它自己那一层 —— 告警，但**请求照常导入**
        # （与「条目/请求/响应整个对象坏掉就跳过该条目」的粒度区分开）。
        for problem in header_shape + cookie_shape + query_shape:
            step.add_warning(f"录制里的 {problem} → 已跳过该项，其余字段照常导入")

        # NOTICE（批次 6 / **M9**）：`queryString` 为空/缺失，**但 URL 自己带着查询串**时
        # 必须从 URL 兜底解析。修复前 `params` 只来自 `queryString`，而下面
        # `step.url = parsed.path`（**丢掉 query**）→ **整个查询串静默消失**，
        # 回放的是另一个请求（实测少掉含批量语义的 `ids` 等参数），零信号。
        # 三个源里只有 HAR 没有这个兜底（curl 用 `-G --data-urlencode`、Postman 用 `url.query`）。
        # 口径：**URL 是"实际发出去的那一条"**，录制里的 `queryString` 缺失只是录制不完整。
        if not params:
            url_query = urlparse(url).query
            if url_query:
                pairs = parse_qsl(url_query, keep_blank_values=True)
                for name, value in pairs:
                    params.setdefault(name, value)  # 重名保留首个，与 `_har_query_params` 同口径
                duplicated_params = bool(duplicated_names(pairs))
                step.add_warning(
                    f"HAR 的 `request.queryString` 为空/缺失，但 URL 带查询串 → "
                    f"已从 URL 解析出 {len(params)} 个查询参数（录制可能不完整）"
                )
        if dropped_headers:
            step.add_warning(
                f"已丢弃由客户端/传输层派生的请求头（回放时由框架决定）：{dropped_headers}"
            )

        base_url, _ = split_url(url)
        origin = origin_of(url)
        if not case.base_url:
            case.base_url = base_url
        if origin == case.base_url:
            parsed = urlparse(url)
            step.url = parsed.path or "/"
            if duplicated_params:
                # 重名查询键字典装不下 → 把原始查询串留在 URL 里（信息不丢）
                #
                # NOTICE（0920 批次 4 / **N22**）：URL 自己**没有**查询串时，必须用
                # `queryString` **重建**一条。修复前这里只看 `parsed.query`，
                # 于是「`queryString` 有重名键、但 URL 不带查询串」这种录制
                # （HAR 规范里两者本就允许不一致）会走到 `else step.url` 分支 ——
                # `params` 已经在上一行被清空、URL 又没补上查询串
                # → **参数全部消失**，而告警还写着"已把查询串原样留在 URL 里"。
                # 实测（`.tmp_audit/n22_check.py`）：`?ids=1&ids=2` 整个不见。
                step.params = {}
                raw_query = parsed.query or _render_query_pairs(
                    request.get("queryString") or []
                )
                if raw_query:
                    step.url = f"{step.url}?{raw_query}"
                    step.add_warning(
                        "查询串里有重名键，字典形式的 `params` 装不下 → "
                        "已把查询串原样留在 URL 里"
                    )
                else:
                    # 连重建都做不到（queryString 里全是没有 name 的坏条目）→
                    # 如实说明"参数丢了"，不要把用户引向"已保留"的错误结论。
                    step.add_warning(
                        "查询串里有重名键，但 URL 不带查询串、`queryString` 也无法重建 → "
                        "**这些查询参数已丢失**，请从原始录制里手工补回 "
                        "`url: \"/path?ids=1&ids=2\"`"
                    )
            else:
                step.params = params
        else:
            step.url = url
            step.add_warning(
                f"该请求的 host（{without_userinfo(origin)}）与用例 "
                f"`base_url`（{without_userinfo(case.base_url)}）不同，已保留绝对 URL"
                # NOTICE（批次 6 / M13）：抹掉 userinfo —— 这条告警会进 stdout 与 --report
            )

        _apply_post_data(step, request.get("postData") or {})

        # ---- 断言：状态码 + （可选）响应形状 schema
        # 批次 8 / L5：`status` 坏了不再让整批崩（`_should_skip_entry` 已放过），
        # 这里按「坏数据」处理 —— 告警 + **不生成状态码断言**，条目其余部分照常导入。
        status, status_problem = _parse_recorded_status(response.get("status"))
        if status_problem:
            step.add_warning(status_problem)
        if status is not None and assertion_mode != "none":
            step.assertions.append({"eq": ["status_code", status]})

        content = response.get("content") or {}
        if not isinstance(content, dict):
            # 批次 8 / L21：`content` 不是对象时修复前在下一行 `content.get(...)` 崩掉整批
            step.add_warning(
                f"响应体 `content` 不是对象（{type(content).__name__}）→ "
                f"已按「没有录制响应体」处理（状态码断言不受影响）"
            )
            content = {}
        mime_type = str(content.get("mimeType") or "")
        text, decode_warning = _extract_response_text(content)
        if decode_warning:
            step.add_warning(decode_warning)

        if assertion_mode == "status+schema" and text and _response_is_json(text):
            if len(text) > MAX_BODY_CHARS:
                step.add_warning(
                    f"响应体过大（{len(text)} 字符 > {MAX_BODY_CHARS}），已跳过形状断言生成"
                )
            else:
                parsed_body = _parse_json_text(text)
                if parsed_body is None:
                    step.add_warning(
                        f"响应体 mimeType 是 {mime_type or '未知'}、看着像 JSON 但解析失败，"
                        "已跳过形状断言生成"
                    )
                else:
                    schema = infer_schema(parsed_body, warnings=step.warnings)
                    if schema:
                        schema_file = _schema_file_name(case_stem, step_index, step.name)
                        step.schema_file = schema_file
                        step.schema_content = schema
                        case.extra_files[schema_file] = schema
                        step.assertions.append(
                            {"jsonschema_match": ["body", schema_file]}
                        )
                        step.add_warning(
                            f"已按录制响应生成形状断言 {schema_file}"
                            "（只断 type/required，不断具体值 —— 逐字段断会把随机数据固化成期望）"
                        )
                    else:
                        step.add_warning("响应体无法推断形状（可能是 null/空对象），已只断状态码")
        elif (
            assertion_mode == "status+schema"
            and not text
            and not decode_warning
            and content.get("text")
        ):
            # 批次 8 / L21：补 `not decode_warning` —— 取不出文本时 `_extract_response_text`
            # 已经给过一条更具体的告警（base64 解码失败 / content 非对象 / text 非字符串），
            # 这里再报一次「没能解出可读文本」就是同一条问题的两条告警（实测重复）。
            step.add_warning(
                "响应体没能解出可读文本（base64 二进制/非 UTF-8），已跳过形状断言生成"
            )
        elif assertion_mode == "status+schema" and text:
            # 0917-1：文本响应但**不是 JSON**（XML/HTML/纯文本）——此前这里是**静默跳过**：
            # 生成的用例只有状态码断言，用户以为"迁移完成"，实际等于没校验。
            # 现在明确告警并给出补救方向（XML 场景尤其常见：SOAP/老接口）。
            looks_xml = text.lstrip().startswith("<")
            step.add_warning(
                f"响应体不是 JSON（mimeType={mime_type or '未知'}，"
                f"{'看着是 XML/HTML' if looks_xml else '普通文本'}），"
                "已**只断状态码**、没有生成形状断言。\n"
                "  hint: XML/SOAP 响应请用 xpath_match / xpath_count / soap_fault 补断言"
                "（见 docs/soap/README.md）；纯文本可以用 `contains: [\"text\", \"…\"]`。"
            )

        # 批次 8 / L5：`assertion_mode != "none"` 却**一条断言都没有** → 不能静默。
        # 与 0919-17 给 Postman 补的「未生成任何断言」是同一口径：
        # 走到这里的唯一路径是「录制的 status 取不到状态码」（形状断言各分支都有自己的告警），
        # 而这种 step 在回放时会被判 success（没有任何校验）——正是最贵的「假绿」。
        if assertion_mode != "none" and not step.assertions:
            step.add_warning(
                "这一步**没有任何断言**（录制的 `response.status` 不是一个可用状态码 → "
                "已跳过状态码断言）→ 回放时这一步不会校验任何东西；"
                "请核对导出工具写出的 status 字段，或手工在 validate 里补断言。"
            )

        # ---- 动态值提示（只提示不改写）
        step.warnings.extend(warn_dynamic_values(step.params, "查询串"))
        if isinstance(step.json_body, dict):
            step.warnings.extend(warn_dynamic_values(step.json_body, "请求体"))

        # ---- 敏感信息占位化（统一在最后做）
        step.headers, header_vars, header_warnings = sanitize_headers(headers)
        step.warnings.extend(header_warnings)
        step.cookies, cookie_vars, cookie_warnings = sanitize_cookies(cookies)
        step.warnings.extend(cookie_warnings)

        body_vars: Dict[Text, Any] = {}
        if isinstance(step.json_body, (dict, list)):
            step.json_body, body_vars, body_warnings = sanitize_body(step.json_body)
            step.warnings.extend(body_warnings)
        elif isinstance(step.data, dict):
            step.data, body_vars, body_warnings = sanitize_body(step.data)
            step.warnings.extend(body_warnings)

        step.variables = merge_variables(header_vars, cookie_vars, body_vars)
        case.steps.append(step)

    # 变量统一提升到 config（步骤里不再重复）
    case.variables = merge_variables(case.variables, *(step.variables for step in case.steps))
    for step in case.steps:
        step.variables = {}

    if skipped:
        preview = skipped[:5]
        more = (
            f"（另有 {len(skipped) - len(preview)} 条）"
            if len(skipped) > len(preview)
            else ""
        )
        case.add_warning(
            f"已过滤 {len(skipped)} 条非业务请求："
            + "；".join(preview)
            + more
            + "\n（过滤规则：静态资源扩展名 / 非接口资源类型 / OPTIONS / status=0 / "
            "重定向中间态 3xx；可用 --include-hosts 收窄）"
        )
    if broken:
        preview = broken[:5]
        more = (
            f"（另有 {len(broken) - len(preview)} 条）"
            if len(broken) > len(preview)
            else ""
        )
        case.add_warning(
            f"有 {len(broken)} 条录制的**条目形态不对** → 已跳过（不影响其它条目）："
            + "；".join(preview)
            + more
            + "\n（`entry` / `request` / `response` 应当是对象，且 `request.url` 不能为空；"
            "这类 HAR 通常被手工或第三方工具改过，请核对原始导出）"
        )
    if duplicates:
        case.add_warning(f"去重：跳过了 {duplicates} 条「方法+URL+请求体」完全相同的重复录制")

    if not case.steps:
        case.add_warning("过滤后没有任何可导入的请求：请检查 --include-hosts / --exclude-hosts 或录制内容")

    # M5（0918-2）：统一收口脱敏（查询串 / 非白名单敏感头 / 字符串表单体）
    result = sanitize_case(case)
    # 批次 9-0（M9）：素材里若出现 `${...}` 字面量 → 告警（只告警，不擅自改写；
    # 必须扫**原始素材**而不是结果，理由见 ir.warn_source_expressions）
    warn_source_expressions([result], har, "HAR 素材")
    return result


def convert_har_file(path: Text, **kwargs) -> IRCase:
    """读取 HAR 文件并转换（`case_stem` 默认取文件名）。"""
    with open(path, mode="r", encoding="utf-8-sig") as fp:
        try:
            har = json.load(fp)
        except ValueError as ex:
            raise ValueError(f"HAR 文件不是合法 JSON：{path}\n{ex}") from ex

    kwargs.setdefault("source", path)
    kwargs.setdefault("case_stem", os.path.splitext(os.path.basename(path))[0])
    return convert_har(har, **kwargs)
