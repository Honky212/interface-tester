"""Postman Collection v2.1 → IR 适配器（P3-b）。

**支持子集**（超出即告警；完整说明见 `docs/convert/README.md`）
----------------------------------------------------------------
| Postman | 处理 |
| --- | --- |
| `info.name` / `info.schema` | 用例名；schema 不是 v2.1 时告警仍按 v2.1 解析 |
| `item[]`（嵌套 folder） | 默认**每个顶层 folder 一个用例文件**（folder → `config.name`），根级请求单独一个；`--single-file` 合并成一个用例 |
| `request.method` / `request.url`（字符串或对象） | 对象形式按 `protocol`+`host`+`path`+`query`+`variable` 重建；**路径变量 `:path` 用 `url.variable` 求值** |
| `request.header[]`（`disabled: true` 跳过） | → `headers`；派生头（Host/Content-Length/Connection/Cookie/Accept-Encoding）丢弃 |
| `request.body.mode` | `raw`(json→`json`，其它→`data`)、`urlencoded`→`data`、`formdata`（text→`data`，file→`upload`）；`file`/`graphql` 告警 |
| `request.auth` / `collection.auth` | `bearer`→`Bearer ${AUTH_TOKEN}`、`basic`→`Basic ${AUTH_BASIC}`、`apikey`→头/查询占位；`oauth2` 告警并建议用 `config.oauth2` |
| `item.response[]`（保存的示例响应） | 取第一条 **2xx** 响应 → `eq [status_code, code]` + 响应**形状** `jsonschema_match`（文件引用）；没有 2xx 时回退第一条并告警（3xx 是重定向中间态，不能当基准） |
| `item.event[]`（pre-request / test 脚本，JS） | **无法无损迁移** → 告警（附脚本首行，便于人工改写） |
| `variable[]`（collection / item 级） | → `config.variables`；名字像凭据的默认写成 `${ENV(...)}` |
| `{{var}}` | → `${var}`（并登记到 `config.variables`；未定义的给 `${ENV(...)}` 默认值 + 告警） |
| `{{$guid}}` / `{{$randomUUID}}` / `{{$timestamp}}` / `{{$randomInt}}` | → `${uuid4_str()}` / `${uuid4_str()}` / `${get_timestamp(10)}` / `${random_int()}`（后两个 helper 由生成的 `debugtalk.py` 提供） |
| 其它 `{{$randomXxx}}` | 告警 + 占位为 `${PM_XXX}`（默认从环境变量读），避免静默产出跑不通的用例 |

设计取向：**宁可显式告警，也不猜**。Postman 的脚本、动态变量、环境变量注入都无法从集合本身还原，
导入器只负责把「结构化的请求 + 保存的示例响应」翻译成框架能跑的 YAML，剩下的交给人工。
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Text, Tuple
from urllib.parse import urlencode, urlparse

from interfacetester.converters.har_adapter import (
    DERIVED_HEADER_PREFIXES,
    DERIVED_HEADERS,
    infer_schema,
)
from interfacetester.converters.ir import (
    IRCase,
    IRStep,
    describe_opaque_value,
    duplicated_names,
    merge_variables,
    origin_of,
    parse_cookie_header,
    safe_dict_entries,
    safe_file_stem,
    sanitize_body,
    sanitize_cases,
    sanitize_cookies,
    sanitize_headers,
    split_url,
    warn_dynamic_values,
    warn_source_expressions,
    without_userinfo,
)

# {{name}} / {{$dynamic}}
VARIABLE_PATTERN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
DYNAMIC_PREFIX = "$"

# Postman 动态变量 → 框架侧表达式（配套 helper 见 emit_yaml 的 debugtalk 模板）
DYNAMIC_VARIABLE_MAP: Dict[Text, Text] = {
    "$guid": "${uuid4_str()}",
    "$randomuuid": "${uuid4_str()}",
    # 0918-7 / L7：Postman 的 `{{$timestamp}}` 语义是 **10 位秒级**时间戳，而
    # `get_timestamp()` 默认返回 13 位毫秒——量纲不一致会让按秒校验时间窗的服务端
    # 判定失败。`get_timestamp(10)` 截取 `str(time.time())` 的前 10 位 = 秒级，与 Postman 对齐。
    "$timestamp": "${get_timestamp(10)}",
    "$randomint": "${random_int()}",
}

# 需要 helper 才能跑通的映射（输出目录的 debugtalk.py 必须提供这些函数）
DYNAMIC_HELPER_FUNCTIONS = ("uuid4_str", "random_int")

# URL 里的路径变量占位（Postman 写法 `:name`）
PATH_VARIABLE_PATTERN = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")

# 名字像凭据的变量：默认值改成从环境变量读（不把明文写进入库的 YAML）
SECRET_NAME_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|key|csrf|xsrf|signature|sign|credential|auth)"
)


def _to_python_placeholder(name: Text) -> Text:
    """Postman 变量名 → 框架变量名（尽量保留原名；非法字符换成下划线）。

    NOTICE（0920 批次 4 / **N24**）：**不能保留非 ASCII**。
    修复前字符集里带了 `\\u4e00-\\u9fff`，于是 `{{域名}}` → `${域名}` ——
    而运行期 `parser` 的变量正则只认 `[A-Za-z_][0-9A-Za-z_]*`，
    `${域名}` **根本不会被解析**（原样发给服务端），生成物必然跑不通；
    更糟的是 `_used_variable_names` 的扫描正则同样只认 ASCII，
    于是这个变量被判"没被引用"，连**定义都被剪枝删掉**：
    `base_url: https://${域名}` + 空的 `config.variables`。
    而 ASCII 名字的对照组完全正常 —— 也就是说"中文变量名"是一条**静默失败**的路径。

    现在把非 ASCII 字符转义成 `_uXXXX`（可读性与唯一性兼顾），
    例如 `域名` → `_u57df_u540d`。这样扫描正则、运行期解析、以及
    `config.variables` 三处口径**天然一致**。
    """
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_")
    if cleaned != name or not name:
        # 含非 ASCII / 特殊字符：用码点转义，保证唯一且是合法标识符。
        # NOTICE: **先转义、再补前缀** —— 反过来 `strip("_")` 会把 `_uXXXX` 的
        # 前导下划线一起吃掉（第一版就这么写的，实测 `域名` → `u57df_u540d`，
        # 虽然仍能用，但与文档里写的 `_u57df_u540d` 不一致，容易误导）。
        escaped = "".join(
            ch if (ch.isascii() and (ch.isalnum() or ch == "_")) else f"_u{ord(ch):04x}"
            for ch in name
        )
        # 只把**首尾**的多余下划线收掉，但必须保留 `_uXXXX` 的前导下划线：
        # 用 lstrip/rstrip 会连带吃掉它，所以这里只处理"整串都是下划线"与数字开头。
        if not escaped.strip("_"):
            escaped = cleaned
        if not escaped or escaped[0].isdigit():
            escaped = f"v_{escaped}"
        return escaped
    return cleaned or "pm_var"


def substitute_variables(
    text: Text,
    variables: Dict[Text, Any],
    warnings: List[Text],
    used: Set[Text],
    dynamic_used: Set[Text],
) -> Text:
    """把 `{{var}}` 换成 `${var}`，把 `{{$dyn}}` 换成映射后的表达式。

    NOTICE: 只做「写法翻译」，**不内联变量的值** —— 值统一放 `config.variables`，
    这样凭据不会被写进请求体、也便于按环境覆盖。
    """
    if not isinstance(text, Text) or "{{" not in text:
        return text

    def replace(match: "re.Match[Text]") -> Text:
        raw_name = match.group(1).strip()
        if raw_name.startswith(DYNAMIC_PREFIX):
            expression = DYNAMIC_VARIABLE_MAP.get(raw_name.lower())
            if expression:
                dynamic_used.add(raw_name.lower())
                warnings.append(f"动态变量 {{{{{raw_name}}}}} 已映射为 `{expression}`")
                return expression
            placeholder = "PM_" + _to_python_placeholder(raw_name.lstrip("$")).upper()
            variables.setdefault(placeholder, "${ENV(" + placeholder + ")}")
            used.add(placeholder)
            warnings.append(
                f"动态变量 {{{{{raw_name}}}}} 无对应实现 → 已占位为 `${{{placeholder}}}`"
                f"（默认从环境变量读，请改成 `${{func()}}` 或固定值）"
            )
            return "${" + placeholder + "}"

        name = _to_python_placeholder(raw_name)
        used.add(name)
        return "${" + name + "}"

    return VARIABLE_PATTERN.sub(replace, text)


def _translate_variable_value(
    value: Any,
    variables: Dict[Text, Any],
    warnings: List[Text],
    used: Set[Text],
    dynamic_used: Set[Text],
) -> Any:
    """变量**值**里的 `{{...}}` → `${...}`（批次 8 / **L6**）。

    NOTICE: 与请求文本**同一条口径**（`substitute_variables`：只翻译写法、**不内联值**），
    嵌套的多层引用交给框架自己的"变量引用变量"收敛逻辑（能力清单 §3.2：多轮收敛、有环报错），
    转换层不需要、也不应该递归展开。

    非字符串值（数字/布尔/数组/对象）原样保留 —— Postman 的变量值在 JSON 里可以是任何类型。
    """
    if not isinstance(value, Text):
        return value
    return substitute_variables(value, variables, warnings, used, dynamic_used)


def _raw_variable_values(node: Dict[str, Any]) -> Tuple[Dict[Text, Any], List[Text]]:
    """`variable[]` → ``({框架变量名: 原始值}, 形态问题)``；**不翻译**（翻译在用例上下文里做，见 L6）。

    NOTICE（批次 8 / **L24**，与 HAR 的 L21 同族）：修复前这里是
    `for entry in node.get("variable") or []: key = entry.get("key")` ——
    `variable` 不是列表、或列表里混进非对象条目时抛裸 `AttributeError`，
    **整份集合一个用例都不产出**（实测：`'oops'` / `123` / `None` 三种条目、
    以及 `"variable": "oops"` 全都崩）。现在：坏条目**跳过并回报**，其余变量照常。
    """
    values: Dict[Text, Any] = {}
    problems: List[Text] = []
    entries = node.get("variable") or [] if isinstance(node, dict) else []
    if not isinstance(entries, list):
        return values, [f"`variable` 不是列表（{type(entries).__name__}）"]
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"variable[{index}] 不是对象（{type(entry).__name__}）")
            continue
        key = entry.get("key")
        if key:
            values[_to_python_placeholder(str(key))] = entry.get("value", "")
    return values, problems


def _translate_variable_values(
    raw_values: Dict[Text, Any],
    warnings: List[Text],
    used: Set[Text],
    dynamic_used: Set[Text],
) -> Dict[Text, Any]:
    """把 `{名字: 原始值}` 里每个**值**的 `{{...}}` 翻译成 `${...}`（L6 口径）。"""
    translated: Dict[Text, Any] = {}
    for name, value in raw_values.items():
        translated[name] = _translate_variable_value(
            value, translated, warnings, used, dynamic_used
        )
    return translated


def _report_variable_shape(
    location: Text, problems: List[Text], warnings: List[Text]
) -> None:
    for problem in problems:
        warnings.append(
            f"{location} 的 `variable[]` 里有形态不对的条目 → 已跳过：{problem}"
        )


def _collection_variables(
    collection: Dict[str, Any],
    warnings: List[Text],
    used: Set[Text],
    dynamic_used: Set[Text],
) -> Dict[Text, Any]:
    """collection 级 `variable[]` 汇总。

    NOTICE（批次 8 / **L6**）：**值里嵌套的 `{{...}}` 也要翻译**。修复前这里只是
    `variables[name] = entry.get("value", "")` 原样搬运，于是

        {"key": "hostBase", "value": "127.0.0.1"},
        {"key": "host",     "value": "{{hostBase}}"}          ← 值里嵌套引用
        {"key": "apiRoot",  "value": "{{protocol}}://{{host}}/v1"}

    生成的是 `host: '{{hostBase}}'` / `apiRoot: '{{protocol}}://{{host}}/v1'` —— 而
    `{{...}}` **不是框架语法**，运行期它就是一段普通字符串。实测三种后果
    （`.tmp_report/probe_l6.py`，真 CLI `hconvert` + `hrun`）：

    | 素材形态 | 修复前运行期 |
    | --- | --- |
    | `{{apiRoot}}/things/1` | `ParamsError: base url missed!`（URL 解析不出 netloc） |
    | `{{protocol}}://{{host}}/v1/things/1`（host 值里带端口） | `requests.InvalidURL: '{{hostBase}}:{{port}}' is not a valid host or port` |
    | `{{protocol}}://{{host}}:18102/…`（host 值只嵌套主机名） | **`NameResolutionError` 被吞成 status_code=0 → 零断言 → `1 passed`（假绿）** |

    最后一行就是记录里 L6 的原话（"运行期 NameResolutionError，且因零断言而 1 passed"），
    已在真 CLI 上复现。

    NOTICE（批次 8 / **L22**）：旧的 docstring 声称这里汇总"collection / **folder** / item 级"
    —— **folder 那半是假的**（folder 级 `variable[]` 从不读取）。现在 folder 级由
    `_iter_items` 沿路径继承后传进来，这句承诺才成立。
    """
    raw, problems = _raw_variable_values(collection)
    _report_variable_shape("集合", problems, warnings)
    return _translate_variable_values(raw, warnings, used, dynamic_used)


def _item_variables(
    item: Dict[str, Any],
    warnings: List[Text],
    used: Set[Text],
    dynamic_used: Set[Text],
) -> Dict[Text, Any]:
    """item 级 `variable[]`（覆盖 collection / folder 级）；值同样要翻译嵌套 `{{...}}`（L6）。"""
    raw, problems = _raw_variable_values(item)
    _report_variable_shape(f"item {item.get('name')!r}", problems, warnings)
    return _translate_variable_values(raw, warnings, used, dynamic_used)


def _url_from_request(
    request: Dict[str, Any], warnings: List[Text]
) -> Tuple[Text, Dict[Text, Any], Dict[Text, Any], List[Text]]:
    """解析 `request.url`，返回 ``(url, params, path_variables, warnings)``。

    Postman 的 url 有两种形态：字符串、或结构化对象（protocol/host/path/query/variable）。
    路径变量（`:path`）用 `url.variable` 求值 —— 求不到就**保留原样并告警**（不猜）。
    """
    url = request.get("url")
    params: Dict[Text, Any] = {}
    local_warnings: List[Text] = []

    if isinstance(url, Text):
        return url, params, {}, local_warnings

    if not isinstance(url, dict):
        return "", params, {}, ["request.url 形态无法识别，已跳过该请求"]

    path_variables: Dict[Text, Any] = {}
    # NOTICE（批次 E / L1）：形态护栏统一走 `safe_dict_entries`（见该函数 NOTICE）。
    for entry in safe_dict_entries(url.get("variable"), "url.variable", local_warnings):
        key = entry.get("key")
        if key:
            path_variables[str(key)] = entry.get("value", "")

    # NOTICE（0920 批次 4 / **N23**）：`protocol` 在 v2.1 里是**可选**的（只有 `raw` 必填），
    # 而修复前这里直接 `or "https"` —— host/path 齐全、只缺 protocol 时，
    # `raw` 里明明写着 `http://`，生成的却是 `https://`，**零告警**：
    #
    # ```text
    # url={'raw':'http://127.0.0.1:18080/ok','host':[…],'port':'18080','path':['ok']}
    #   -> base_url='https://127.0.0.1:18080'   ← raw 的 http 被丢弃
    # ```
    #
    # 后果是生成物打到**错误的协议**（本地 http 服务被 https 握手打死），而导入期全绿。
    # 现在：先尝试从 `raw` 取 scheme，取不到才回落到 https（并告警说明用了默认值）。
    protocol = url.get("protocol")
    if not protocol and url.get("raw"):
        parsed_scheme = urlparse(str(url["raw"]).strip()).scheme
        if parsed_scheme:
            protocol = parsed_scheme
            local_warnings.append(
                f"`url.protocol` 缺失（Postman 里可选）→ 已按 `url.raw` 的 "
                f"`{parsed_scheme}://` 判定，而不是默认 https"
            )
    if not protocol:
        protocol = "https"
    host = url.get("host")
    if isinstance(host, list):
        host = ".".join(str(part) for part in host)
    host = host or ""
    port = url.get("port")
    if port:
        host = f"{host}:{port}"

    path = url.get("path")
    if isinstance(path, list):
        path = "/" + "/".join(str(segment) for segment in path)
    path = path or "/"

    # NOTICE（0918-8 / M24）：`url.raw` 是 Postman v2.1 schema 里**唯一必填**的字段，
    # `host`/`path` 都是可选的——第三方导出的集合经常只给 `raw`。
    # 修复前「host 缺省成空串、path 缺省成 `/`」直接拼成 `https:///?page=2`：
    # host 与 path **双双丢失**，告警只说「host（）与 base_url（空）不同」（信息量约等于零），
    # hmake 还能过、exit 0，只有真跑时才以一个 InvalidURL 报错。
    # 处置：结构化字段缺任一项时，用 `raw` 的解析结果**回填**（而不是另起一条分支）——
    # 这样路径变量替换（`:id`）、查询串合并等既有逻辑全部照常生效，且 URL 不丢信息。
    if url.get("raw") and (
        not url.get("host") or not url.get("path")
    ):
        parsed_raw = urlparse(str(url["raw"]).strip())
        if parsed_raw.netloc:
            protocol = parsed_raw.scheme or protocol
            host = parsed_raw.netloc
            path = parsed_raw.path or "/"
            local_warnings.append(
                f"`url.host`/`url.path` 缺失（Postman 导出常见）→ 已按 `url.raw` 回填："
                f"{protocol}://{host}{path}"
            )

    # NOTICE（0919-2 / 缺陷 3）：路径变量必须**单趟**替换。
    # 修复前是「`finditer` 预收集 + 逐轮 `path.replace(f":{name}", ...)`」，两个后果：
    #   ① `:id` 的替换会把 `:ids` 的**前半段**一起换掉——`/users/:id/:ids` 在 id=5 时
    #      变成 `/users/5/5s`（`ids` 的 `id` 被打掉、尾巴 `s` 留在原地），零告警地把请求
    #      打到**另一个路径**上。`:id`/`:ids`、`:type`/`:types` 这类前缀变量并存很常见
    #      （批量删除接口就是 `/x/:id` 与 `/x/:ids`）。
    #   ② 替换进来的**值本身**还会被后续轮次再替换（值里含 `:xxx` 时二次污染）。
    # 单趟 `re.sub` + 替换函数一次收掉两者：每个 match 只消费原串一次，
    # 替换结果不进入后续扫描面（`str.replace` 的语义是「全串无边界替换」，本就不该用在这里）。
    #
    # 判据本身不必改：`PATH_VARIABLE_PATTERN` 匹配的是**完整标识符**（`[A-Za-z_][A-Za-z0-9_]*`），
    # 所以 `:ids` 一直是被当成一个 token 匹配的 —— 出错的是替换，不是识别。
    unresolved = []

    def _replace_path_variable(match: "re.Match") -> Text:
        name = match.group(1)
        value = path_variables.get(name)
        if value in (None, ""):
            unresolved.append(name)
            return match.group(0)
        return str(value).lstrip("/")

    path = PATH_VARIABLE_PATTERN.sub(_replace_path_variable, path)

    if unresolved:
        local_warnings.append(
            f"路径变量 {[':' + name for name in unresolved]} 在 url.variable 里没有值 → "
            "已原样保留，请手工改成实际路径（例如 /get）"
        )

    query_pairs: List[Tuple[Text, Any]] = []
    for entry in safe_dict_entries(url.get("query"), "url.query", local_warnings):
        if entry.get("disabled"):
            continue
        key = entry.get("key")
        if key is None:
            continue
        query_pairs.append((str(key), entry.get("value", "")))

    built = f"{protocol}://{host}{path}"

    # NOTICE（0919-2 / 缺陷 8）：`url.query` 的重名键与 HAR 的 `queryString` 是同一类输入
    # （`?ids=1&ids=2` 是批量接口的常见形态），但修复前这里是**静默覆盖**：
    # `params[str(key)] = ...` 只留下最后一个值，`warnings` 是空的 ——
    # 回放时批量删除会**少删几条**，而且没有任何信号。现在与 M33（HAR queryString）
    # 完全同口径：告警 + 把查询串**原样留在 URL 里**（字典装不下重名，就不拆成字典）。
    duplicated_query = duplicated_names(query_pairs)
    if duplicated_query:
        raw_query = ""
        if url.get("raw") and "?" in str(url["raw"]):
            raw_query = str(url["raw"]).split("?", 1)[1]
        built = f"{built}?{raw_query or urlencode(query_pairs)}"
        local_warnings.append(
            f"url.query 里有重名键 {duplicated_query}，字典形式的 `params` 装不下"
            f"（只保留最后一个值会让回放少发几个参数）→ 已把查询串原样留在 URL 里"
        )
    else:
        params = dict(query_pairs)

    if not url.get("query") and url.get("raw") and "?" in str(url["raw"]):
        # 结构化 query 缺失但 raw 里有查询串（罕见）：保留 raw 的查询串，信息不丢
        raw_query = str(url["raw"]).split("?", 1)[1]
        if raw_query:
            built = f"{built}?{raw_query}"
            local_warnings.append(
                "url.query 缺失但 raw 里有查询串 → 已把查询串原样拼回 URL（没拆成 params）"
            )

    return built, params, path_variables, local_warnings


def _headers_from_request(
    request: Dict[str, Any], variables: Dict[Text, Any], warnings: List[Text], used: Set[Text], dynamic_used: Set[Text]
) -> Dict[Text, Any]:
    headers: Dict[Text, Any] = {}
    dropped: List[Text] = []

    for entry in safe_dict_entries(request.get("header"), "request.header", warnings):
        if entry.get("disabled"):
            continue
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        lowered = key.lower()
        if lowered in DERIVED_HEADERS or lowered.startswith(DERIVED_HEADER_PREFIXES):
            dropped.append(key)
            continue
        value = substitute_variables(
            str(entry.get("value", "")), variables, warnings, used, dynamic_used
        )
        headers[key] = value

    if dropped:
        warnings.append(
            f"已丢弃由客户端/传输层派生的请求头（回放时由框架决定）：{dropped}"
        )
    return headers


def _cookies_from_request(
    request: Dict[str, Any],
    variables: Dict[Text, Any],
    warnings: List[Text],
    used: Set[Text],
    dynamic_used: Set[Text],
) -> Dict[Text, Any]:
    """收集 cookie：`Cookie` 请求头 + `request.cookie` 数组（**合并**，不丢任何一侧）。

    NOTICE（0918-8 / M33）：Postman 集合里 cookie 的**唯一载体就是 `Cookie` 头**
    （v2.1 的 item 没有别的 cookie 字段；`request.cookie` 是 Postman 自己扩展出来的，
    导出的集合里通常为空）。而 `Cookie` 在 `DERIVED_HEADERS` 里 —— 那条规则是为
    HAR/curl 写的（它们另有 `request.cookies` / `-b` 兜住），Postman 复用之后
    cookie 就**凭空消失了**：生成的用例发的是另一个请求，且零报错。
    这里把它解析回 `cookies`（后续由 `sanitize_cookies` 统一占位化），与另两个源一致。
    """
    cookies: Dict[Text, Any] = {}

    for entry in safe_dict_entries(request.get("header"), "request.header", warnings):
        if entry.get("disabled"):
            continue
        if str(entry.get("key") or "").strip().lower() != "cookie":
            continue
        raw_value = substitute_variables(
            str(entry.get("value", "")), variables, warnings, used, dynamic_used
        )
        parsed = parse_cookie_header(raw_value)
        if not parsed:
            # NOTICE（0920 批次 4 / **N21**）：**不能把 Cookie 原文打进告警**。
            # 告警会进 stdout 与 `hconvert --report` 生成的 markdown（`cli._build_report`
            # 把**全部**告警写进报告）—— 而这里回显的正是**凭据本体**
            # （典型形态是裸 JWT：`eyJhbGciOi….CANARY.sig`，连 `k=v` 都不是）。
            # 修复前实测（`.tmp_audit/verify_sub_f1_f3.py`）：canary 出现在告警里，
            # 而生成的 YAML 是干净的 —— 属于"M13 脱敏收口漏了告警文本这一路"。
            # 现在只给**长度 + 形态**，够定位问题、不泄露值。
            warnings.append(
                f"`Cookie` 头的值无法解析成 `k=v`（已忽略）："
                f"长度 {len(raw_value)} 字符、形如 "
                f"{describe_opaque_value(raw_value)}。"
                f"请确认它是不是被 Postman 的动态变量/脚本改写过。"
            )
        cookies.update(parsed)

    explicit = request.get("cookie")
    if isinstance(explicit, list):
        for entry in explicit:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            name = str(entry["name"])
            value = entry.get("value", "")
            if name in cookies and str(cookies[name]) != str(value):
                warnings.append(
                    f"cookie {name!r} 在 `Cookie` 头与 `request.cookie` 里不一致，"
                    f"已采用请求头里的值（那是实际发出去的）"
                )
                continue
            cookies[name] = value

    return cookies


def _body_from_request(
    request: Dict[str, Any],
    variables: Dict[Text, Any],
    warnings: List[Text],
    used: Set[Text],
    dynamic_used: Set[Text],
    content_type: Text = "",
) -> Tuple[Any, Any, Dict[Text, Text]]:
    """解析 `request.body`，返回 ``(json_body, data, upload)``。

    NOTICE（0918-8 / H12）：`mode=raw` 时**不能只看 `body.options.raw.language`**。
    那个字段是 Postman 的可选提示，导出的集合里经常缺失或写成 `text`/`javascript`，
    而 `Content-Type: application/json` 才是可靠信号（curl/HAR 两个源就是这么判的）。
    修复前只看 language，于是「声明 JSON 但 language 缺失/不匹配」的集合里，
    JSON 体被当成**原始字符串**放进 `data` —— 而 `sanitize_body` 只认 dict/list、
    字符串体走的是 `a=b&c=d` 那条路，于是体里的 `password`/`token` **明文写进 YAML**。
    """
    body = request.get("body")
    if not isinstance(body, dict):
        return None, None, {}

    mode = str(body.get("mode") or "").lower()

    if mode == "raw":
        text = substitute_variables(str(body.get("raw") or ""), variables, warnings, used, dynamic_used)
        # NOTICE（批次 E / L1）：`options`/`raw` 都可能不是对象（导出工具写法不一），
        # 修复前直接 `.get` 链会抛裸 `AttributeError: 'str' object has no attribute 'get'`。
        options = body.get("options")
        options = options if isinstance(options, dict) else {}
        raw_options = options.get("raw")
        raw_options = raw_options if isinstance(raw_options, dict) else {}
        language = str(raw_options.get("language") or "").lower()
        # 判据：显式声明 json（原有行为不变）**或** Content-Type 表明是 JSON（新增）
        looks_json = language == "json" or "json" in str(content_type or "").lower()
        if looks_json:
            try:
                structured = json.loads(text)
            except ValueError:
                warnings.append(
                    "body.mode=raw 且看起来是 JSON（language/Content-Type），但内容不是合法 JSON "
                    "→ 已按原始字符串放进 `data`"
                )
                return None, text, {}
            if not isinstance(structured, (dict, list)):
                # `"abc"` / `123` / `null` 这类标量：放进 json 没有意义，保留原文
                return None, text, {}
            if language != "json":
                warnings.append(
                    "body.mode=raw 的 `options.raw.language` 不是 json，但 Content-Type 是 "
                    "application/json → 已按结构化 JSON 放进 `request.json`"
                    "（与 curl / HAR 两个源的口径一致，敏感字段才能按字段名占位）"
                )
            return structured, None, {}
        if language in ("xml", "html"):
            warnings.append(
                f"body 是 {language.upper()} 文本 → 已放进 `data`；"
                "如需 SOAP 请自行补 `Content-Type: text/xml` 与 SOAPAction 头"
            )
        return None, text, {}

    if mode == "urlencoded":
        fields: Dict[Text, Any] = {}
        dropped = []
        pairs: List[Tuple[Text, Any]] = []
        for entry in safe_dict_entries(
            body.get("urlencoded"), "body.urlencoded", warnings
        ):
            if entry.get("disabled"):
                dropped.append(str(entry.get("key")))
                continue
            key = entry.get("key")
            if key is None:
                continue
            pairs.append((str(key), entry.get("value", "")))
            fields[str(key)] = substitute_variables(
                str(entry.get("value", "")), variables, warnings, used, dynamic_used
            )
        if dropped:
            warnings.append(f"已跳过 disabled 的表单字段：{dropped}")
        # NOTICE（0920 批次 2 / **N2**）：重名字段必须**可见**。
        # IR 的 `data` 是 dict，装不下 `ids=1&ids=2&ids=3`（批量接口的常见形态），
        # 修复前这里是**静默**只留最后一个值 → 回放少发字段、用例照绿。
        # 同包的 `duplicated_names` 早就存在，HAR `postData.params` 与 Postman `url.query`
        # 都接了，唯独**请求体**这两处（urlencoded / formdata）漏了。
        duplicated = duplicated_names(pairs)
        if duplicated:
            warnings.append(
                f"表单体里有重名字段 {duplicated} → 字典装不下，**只保留最后一个值**"
                f"（回放时会少发若干字段）。\n"
                f"  原始表单：{[f'{name}={value}' for name, value in pairs]}\n"
                f"  hint: 需要重名字段（批量删除/批量查询）时，请手工把这步改成原始字符串体："
                f"`data: \"{urlencode(pairs)}\"`（并保留对应的 Content-Type）"
            )
        return None, fields, {}

    if mode == "formdata":
        fields = {}
        uploads: Dict[Text, Text] = {}
        dropped = []
        pairs = []
        for entry in safe_dict_entries(body.get("formdata"), "body.formdata", warnings):
            if entry.get("disabled"):
                dropped.append(str(entry.get("key")))
                continue
            key = str(entry.get("key") or "")
            if not key:
                continue
            if entry.get("type") == "file":
                src = entry.get("src")
                if isinstance(src, list):
                    src = src[0] if src else None
                if not src:
                    warnings.append(f"formdata 的 file 字段 {key} 没有 src（Postman 里未选文件）→ 已跳过")
                    continue
                pairs.append((key, src))
                uploads[key] = str(src)
            else:
                pairs.append((key, entry.get("value", "")))
                fields[key] = substitute_variables(
                    str(entry.get("value", "")), variables, warnings, used, dynamic_used
                )
        if dropped:
            warnings.append(f"已跳过 disabled 的表单字段：{dropped}")
        # 同 urlencoded：重名字段要可见（**包括 file 字段**——`upload` 也是 dict）
        duplicated = duplicated_names(pairs)
        if duplicated:
            warnings.append(
                f"formdata 里有重名字段 {duplicated} → 字典装不下，**只保留最后一个值**"
                f"（`data`/`upload` 每个字段名只能有一个值，回放时会少发若干部分）。"
            )
        if uploads:
            warnings.append(
                f"multipart 的文件字段已映射到 `upload`：{uploads}（src 来自 Postman 的工作目录，请确认本地路径）"
            )
        return None, (fields or None), uploads

    if mode == "file":
        warnings.append(
            "body.mode=file（二进制请求体）无对应能力 → 已跳过请求体；"
            "如需上传文件请改成 `request.upload`"
        )
        return None, None, {}

    if mode == "graphql":
        warnings.append("body.mode=graphql 不支持 → 已跳过请求体（请手工改写成 JSON）")
        return None, None, {}

    warnings.append(f"未知的 body.mode={mode!r} → 已跳过请求体")
    return None, None, {}


def _auth_headers(
    auth: Optional[Dict[str, Any]],
    variables: Dict[Text, Any],
    warnings: List[Text],
    where: Text,
) -> Dict[Text, Any]:
    """Postman 的 `auth` → 请求头占位（**不落明文凭据**）。"""
    if not isinstance(auth, dict):
        return {}
    auth_type = str(auth.get("type") or "").lower()
    if auth_type in ("", "noauth"):
        return {}

    def field_value(name: Text) -> Optional[Text]:
        for entry in auth.get(auth_type) or []:
            if str(entry.get("key")) == name:
                return entry.get("value")
        return None

    if auth_type == "bearer":
        variables.setdefault("AUTH_TOKEN", "${ENV(AUTH_TOKEN)}")
        warnings.append(f"{where} 使用 bearer 认证 → `Authorization: Bearer ${{AUTH_TOKEN}}`（值从环境变量读）")
        return {"Authorization": "Bearer ${AUTH_TOKEN}"}

    if auth_type == "basic":
        variables.setdefault("AUTH_BASIC", "${ENV(AUTH_BASIC)}")
        warnings.append(
            f"{where} 使用 basic 认证 → `Authorization: Basic ${{AUTH_BASIC}}`"
            "（值应为 base64(user:password)，从环境变量读）"
        )
        return {"Authorization": "Basic ${AUTH_BASIC}"}

    if auth_type == "apikey":
        key_name = str(field_value("key") or "X-Api-Key")
        location = str(field_value("in") or "header").lower()
        variable_name = "API_KEY_" + _to_python_placeholder(key_name).upper()
        variables.setdefault(variable_name, "${ENV(" + variable_name + ")}")
        if location == "query":
            warnings.append(
                f"{where} 的 apikey 认证在 **query** 里 → 请在 YAML 的 request.params 手工加 "
                f"`{key_name}: ${{{variable_name}}}`（导入器不擅自改 URL）"
            )
            return {}
        warnings.append(f"{where} 使用 apikey 认证 → 请求头 `{key_name}: ${{{variable_name}}}`")
        return {key_name: "${" + variable_name + "}"}

    if auth_type == "oauth2":
        warnings.append(
            f"{where} 使用 oauth2 认证 → **未自动迁移**；"
            "框架内置 Client Credentials 支持，请在 YAML 里补 `config.oauth2`"
            "（token_url/client_id/client_secret），或用 debugtalk 取 token"
        )
        return {}

    warnings.append(f"{where} 的 auth 类型 {auth_type!r} 未支持 → 已忽略，请手工补认证头")
    return {}


def _assertions_from_item(
    item: Dict[str, Any], step_index: int, case_stem: Text, assertion_mode: Text
) -> Tuple[List[Dict[str, Any]], Optional[Text], Optional[Dict[str, Any]], List[Text]]:
    """用 Postman 保存的示例响应生成断言（状态码 + 形状 schema）。"""
    responses = item.get("response") or []
    warnings: List[Text] = []
    if assertion_mode == "none" or not responses:
        return [], None, None, warnings

    # NOTICE（0919-2 / 缺陷 9）：`code` 不能直接 `int()`。
    # 修复前是 `[r for r in responses if int(r.get("code") or 0) > 0]` 与
    # `int(primary.get("code"))` —— 官方导出给的是数字，但**手改/第三方工具**的产物
    # 可能是 `"200 OK"`，于是 `ValueError` 冒到 CLI：
    #     ERROR | 解析 <文件> 失败：ValueError: invalid literal for int() with base 10: '200 OK'
    # 整个文件**一个用例都不产出**（实测 `--out` 目录根本不会创建）——
    # 一个坏示例响应杀掉整批导入，而报错信息完全没提「示例响应的 code」。
    # 现在的口径：**code 缺失/为空**（录制里本来就没有状态码）静默跳过；
    # **code 有值但不是数字**是坏数据 → 告警并跳过**那一条**响应，其余照常。
    usable: List[Tuple[int, Dict[str, Any]]] = []
    malformed: List[Tuple[Text, Any]] = []
    # NOTICE（批次 8 / L7）：`code` 是数字但**不是正数**（`0` / `-1`）单独一桶。
    # 修复前它既不算可用、也不算坏数据 → **完全静默地被丢掉**；
    # 而没有可用响应那条告警还会把它说成「code 缺失/为空」（归因错误）。
    non_positive: List[Tuple[Text, int]] = []
    # NOTICE（批次 8 / L7 末项）：非 dict 条目也**不能静默丢**。
    # 修复前这里是裸 `continue`：一份被手改过的集合里放了个字符串/数字，
    # 该条目消失得无影无踪（实测 `.tmp_report/probe_l7.py` ⑦：`["oops", {code:200}]`
    # 只产出「已按保存的示例响应生成形状断言」——用户以为两条示例都被用上了）。
    # 与 malformed（code 坏了）分开报：坏的是**条目本身**，不是 code。
    wrong_shape: List[Text] = []
    for index, response in enumerate(responses):
        if not isinstance(response, dict):
            wrong_shape.append(f"[{index}] 是 {type(response).__name__}")
            continue
        raw_code = response.get("code")
        if raw_code in (None, ""):
            continue
        try:
            numeric_code = int(raw_code)
        except (TypeError, ValueError):
            malformed.append((str(response.get("name") or "未命名示例响应"), raw_code))
            continue
        if numeric_code > 0:
            usable.append((numeric_code, response))
        else:
            non_positive.append(
                (str(response.get("name") or "未命名示例响应"), numeric_code)
            )

    if wrong_shape:
        detail = "\n".join(f"    {item}" for item in wrong_shape)
        warnings.append(
            f"有 {len(wrong_shape)} 个示例响应条目**形态不对** → 已跳过"
            f"（不影响其它响应）：\n"
            f"{detail}\n"
            f"  示例响应条目应当是对象（含 code / body 等字段）；"
            f"这份集合可能被手工或第三方工具改过，请核对导出的原始 JSON。"
        )

    if malformed:
        detail = "\n".join(
            f"    {name!r} 的 code = {code!r}" for name, code in malformed
        )
        warnings.append(
            f"有 {len(malformed)} 个示例响应的 `code` 不是数字 → 已跳过（不影响其它响应）：\n"
            f"{detail}\n"
            f"  `code` 应当是 HTTP 状态码（如 200）。这个请求的断言只按剩余可用响应生成；"
            f"若本来想断状态码，请把示例响应的 code 改成数字。"
        )

    if non_positive:
        detail = "\n".join(
            f"    {name!r} 的 code = {code!r}" for name, code in non_positive
        )
        warnings.append(
            f"有 {len(non_positive)} 个示例响应的 `code` **不是正数** → 已跳过"
            f"（不影响其它响应）：\n"
            f"{detail}\n"
            f"  `code` 应当是 HTTP 状态码（如 200），`0` / `-1` 这类占位值不能当期望；"
            f"若这些示例本来就没有状态码，请把 code 补成实际状态码。"
        )

    if not usable:
        # 0919-17 / ②：「存了示例响应，但没有一条可用」不再是无信号的。
        # 刻意区分两层（0919-7 拍板的「缺失不告警」保持不动）：
        #   - **单条** code 缺失/为空 → 按非坏数据处理、不告警（部分缺失是常态，
        #     逐条告警会刷屏——见 test_missing_or_empty_code_is_still_skipped_silently）；
        #   - **全部**不可用 → 结果是「这个请求一条断言都没生成」，属于
        #     「静默产出与预期不符」，必须可见（malformed 已告警的情形不重复）。
        # 刻意**不**顺手把示例响应的 body 自动变成形状断言：那会给既有导入产物
        # 新增断言（录制的示例体与真实服务不一定一致，可能把通过的用例改红）。
        # 要形状断言请把 code 补成数字，或手工在 validate 里加 jsonschema_match
        # （已知边界，见 docs/能力清单.md 的存量资产导入边界表）。
        # 批次 8 / L7-③：抑制条件必须**精确**，否则会留下一句**假承诺**。
        # malformed 那条告警里写着「这个请求的断言只按剩余可用响应生成」——
        # 若同时还存在 non_positive / wrong_shape，则「剩余可用」其实是 **0 条**
        # （断言一条都没有），此时抑制这条结果级告警，用户拿到的就只有一句
        # 指向错误方向的说明。
        # 实测（.tmp_report/probe_l7.py ⑤）：{weird:"200 OK", zero:0} 组合在修复前
        # assertions=0 且三条告警里没有任何一条说「未生成任何断言」。
        # 只有 malformed **单独**存在时，它自己已经说清「断言按剩余可用响应生成」＝
        # 没有剩余（该请求只有这一条响应）也谈不上矛盾，故仍不重复打第二条
        # （既有口径，见 test_no_duplicate_warning_when_malformed_already_warned）。
        if not malformed or non_positive or wrong_shape:
            warnings.append(
                f"该请求保存了 {len(responses)} 个示例响应，但没有一条可用"
                f"（code 缺失/为空、**不是正数**，或条目形态不对）→ 未生成任何断言。\n"
                f"  状态码与形状断言都来自示例响应；若需要断言，"
                f"请把示例响应的 code 补成数字（如 200），"
                f"或手工在 validate 里加断言（如 jsonschema_match）。"
            )
        return [], None, None, warnings

    primary_code, primary = usable[0]
    # NOTICE（批次 6 / **M12**）：优先取**成功响应**作为断言基准。
    #
    # 本模块 docstring 一直写着"取第一条**非错误**响应"，但修复前这里直接取 `usable[0]`，
    # 而 `usable` 只过滤了"code 是数字且 > 0" —— 于是**保存的第一条示例是 404/500 时，
    # 用例断的就是那个错误码**：接口正常返回 200 时用例反而**红灯**，
    # 而接口真的返回 404 时用例**绿灯通过** —— 把错误分支固化成了契约。
    # Postman 的示例列表里 4xx 常排在前面（团队习惯把"未授权""不存在"排在成功之前）。
    #
    # NOTICE（0920 / **缺陷 3**）：判据从 `code < 400` 收紧成 `200 <= code < 300`。
    # 3xx **不是**成功响应，而是重定向中间态：框架默认 allow_redirects=True 会跟到最终
    # 响应，于是"把 302 当成功基准"生成的 `eq: [status_code, 302]` **必然失败**
    # （实测：示例 `[302, 200]` → 生成 `eq 302` → 回放 `assert status_code equal 302
    # ==> fail`，实际 200）。修复前 `code < 400` 会把 302 判成"成功"并优先选中它。
    success_examples = [(code, response) for code, response in usable if 200 <= code < 300]
    if success_examples:
        primary_code, primary = success_examples[0]
    else:
        primary_code, primary = usable[0]
        warnings.append(
            f"该请求保存的 {len(usable)} 个示例响应**没有 2xx**"
            f"（状态码 {[code for code, _response in usable]}）→ 断言只能按第一条 "
            f"{primary_code} 生成。\n"
            f"  这意味着用例把「错误/重定向分支」当成了期望：接口正常返回 2xx 时用例会**失败**；"
            f"若第一条是 3xx（重定向），框架默认会跟到最终响应，这条断言同样会失败。\n"
            f"  hint: 在 Postman 里给该请求补一条 200 的示例响应，或手工把 "
            f"validate 里的状态码改成实际期望值。"
        )
    assertions: List[Dict[str, Any]] = []
    if assertion_mode != "none":
        assertions.append({"eq": ["status_code", primary_code]})

    if len(usable) > 1:
        names = [
            str(response.get("name") or f"response #{index}")
            for index, (_code, response) in enumerate(usable)
        ]
        warnings.append(
            f"该请求在 Postman 里保存了 {len(usable)} 个示例响应（{names}）→ "
            f"断言只按第一条 {primary.get('name')!r} 生成，其余请按需调整"
        )

    schema_file = None
    schema_content = None
    if assertion_mode == "status+schema":
        body_text = primary.get("body")
        if isinstance(body_text, str) and body_text.strip().startswith(("{", "[")):
            try:
                parsed = json.loads(body_text)
            except ValueError:
                warnings.append("示例响应体像 JSON 但解析失败 → 已只断状态码")
            else:
                schema = infer_schema(parsed)
                if schema:
                    # NOTICE（批次 5 / H5）：词干用 `ir.safe_file_stem`（**保留非 ASCII**）。
                    # 修复前这里写的是 `re.sub(r"[^0-9A-Za-z]+", "_", ...)`，中文 item 名
                    # 会被整段抹掉、退化成 `step`，中文 folder 名同理 → 不同用例落到同一个
                    # schema 文件上互相覆盖（断言断错对象）。
                    safe_item = safe_file_stem(item.get("name"), default="step")
                    schema_file = f"schemas/{case_stem}_{step_index:02d}_{safe_item}.json"
                    schema_content = schema
                    assertions.append({"jsonschema_match": ["body", schema_file]})
                    warnings.append(
                        f"已按保存的示例响应生成形状断言 {schema_file}"
                        "（只断 type/required，不断具体值）"
                    )
        elif isinstance(body_text, str) and body_text.strip():
            # 0917-1：示例响应是**非 JSON** 文本（XML/HTML/纯文本）时此前**静默跳过**——
            # 生成的用例只有状态码断言，容易被误认为「迁移完成」。
            looks_xml = body_text.lstrip().startswith("<")
            warnings.append(
                f"示例响应不是 JSON（{'看着是 XML/HTML' if looks_xml else '普通文本'}），"
                "已**只断状态码**、没有生成形状断言。\n"
                "  hint: XML/SOAP 响应请用 xpath_match / xpath_count / soap_fault 补断言"
                "（见 docs/soap/README.md）；纯文本可以用 `contains: [\"text\", \"…\"]`。"
            )
    return assertions, schema_file, schema_content, warnings


def _iter_items(
    items: List[Dict[str, Any]],
    folder_path: Tuple[Text, ...] = (),
    folder_variables: Optional[Dict[Text, Any]] = None,
    folder_problems: Optional[List[Text]] = None,
) -> List[Tuple[Tuple[Text, ...], Dict[str, Any], Dict[Text, Any]]]:
    """展开嵌套 folder，返回 ``[(folder 路径, item, 该 folder 链上的变量), ...]``。

    NOTICE（批次 8 / **L22**）：folder 自身的 `variable[]` 要**沿路径继承**
    （Postman 的支持范围是 collection → folder → item，**越具体越优先**）。
    修复前这里只展平出叶子 item，folder 的 `variable[]` 从不读取 ——
    引到它的请求于是退化成 `${ENV(name)}` + 一条"变量未定义"的告警，
    **录到的值被丢掉**（实测 `.tmp_report/probe_l22_folder_vars.py`）。
    """
    inherited: Dict[Text, Any] = dict(folder_variables or {})
    flattened: List[Tuple[Tuple[Text, ...], Dict[str, Any], Dict[Text, Any]]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "item")
        if item.get("item") is not None:
            # folder：本层的 `variable[]` 并进继承链（同名时**内层覆盖外层**）
            layer = dict(inherited)
            raw, problems = _raw_variable_values(item)
            if folder_problems is not None:
                folder_problems.extend(
                    f"folder {name!r}：{problem}" for problem in problems
                )
            layer.update(raw)
            flattened.extend(
                _iter_items(
                    item.get("item") or [],
                    folder_path + (name,),
                    layer,
                    folder_problems,
                )
            )
        else:
            flattened.append((folder_path, item, inherited))
    return flattened


def convert_postman(
    collection: Dict[str, Any],
    case_name: Text = "",
    split_folders: bool = True,
    assertion_mode: Text = "status+schema",
) -> List[IRCase]:
    """Postman Collection v2.1 → `IRCaste` 列表。

    Args:
        split_folders: True（默认）→ **每个顶层 folder 一个用例**，根级请求另成一个用例；
                       False → 整个集合合成一个用例（步骤名带 folder 前缀）。
        assertion_mode: `status+schema`（默认）/ `status` / `none`
    """
    info = collection.get("info") or {}
    collection_name = case_name or str(info.get("name") or "postman collection")
    global_warnings: List[Text] = []

    schema_url = str(info.get("schema") or "")
    if schema_url and "v2.1" not in schema_url:
        global_warnings.append(
            f"集合 schema 是 {schema_url}（本导入器按 Collection **v2.1** 解析，未知字段会被忽略）"
        )
    if not schema_url:
        global_warnings.append("集合缺少 info.schema，已按 Collection v2.1 解析")

    # 批次 8 / **L6**：集合变量的**值**也要翻译 `{{...}}`，且它引到的名字同样要登记
    # （未定义的按 `${ENV(name)}` 占位并告警 —— 与请求文本里引到未定义变量同口径）。
    base_used: Set[Text] = set()
    base_dynamic_used: Set[Text] = set()
    base_variables = _collection_variables(
        collection, global_warnings, base_used, base_dynamic_used
    )
    for name in base_used:
        if name not in base_variables:
            base_variables[name] = "${ENV(" + name + ")}"
            global_warnings.append(
                f"集合变量引用了未定义的 {name} → 已按 `${{ENV({name})}}` 占位，请确认取值来源"
            )
    base_auth = collection.get("auth")

    # L22：folder 链上的 `variable[]` 由 `_iter_items` 继承下来；L24：坏条目收进 folder_problems
    folder_problems: List[Text] = []
    flat_items = _iter_items(collection.get("item") or [], folder_problems=folder_problems)
    if folder_problems:
        global_warnings.append(
            "有 folder 的 `variable[]` 条目形态不对 → 已跳过这些条目（其余变量照常）："
            + "；".join(folder_problems[:5])
        )
    if not flat_items:
        return [
            IRCase(name=collection_name, warnings=["集合里没有任何请求（item 为空）"])
        ]

    # 顶层 folder 分组；split_folders=False 时全部合一组
    GroupEntry = Tuple[Tuple[Text, ...], Dict[str, Any], Dict[Text, Any]]
    groups: Dict[Tuple[Text, ...], List[GroupEntry]] = {}
    for folder_path, item, folder_variables in flat_items:
        key: Tuple[Text, ...] = (folder_path[0],) if (split_folders and folder_path) else ()
        groups.setdefault(key, []).append((folder_path, item, folder_variables))

    cases: List[IRCase] = []
    for group_key, entries in groups.items():
        group_name = f"{collection_name} / {group_key[0]}" if group_key else collection_name
        case = IRCase(name=group_name, source=str(info.get("name") or "postman"))
        case.warnings.extend(global_warnings)
        case_variables: Dict[Text, Any] = dict(base_variables)
        # L6：集合变量值里用到的动态变量（如 `{{$guid}}`）也要计入本用例的提示
        dynamic_used: Set[Text] = set(base_dynamic_used)
        step_origins: List[Text] = []
        script_warnings = 0
        # L23：URL 以变量开头时 origin 静态不可知，只提示**一次**（每步重复是无信息量的噪音）
        origin_unknown_warned = False
        # L25：跨作用域重名变量的冲突记录（每个用例文件一份）+ 每个步骤的作用域变量
        scoped_seen: Dict[Text, Any] = {}
        scoped_conflicts: Dict[Text, List[Any]] = {}
        scoped_by_step: List[Dict[Text, Any]] = []

        for step_index, (folder_path, item, folder_variables) in enumerate(
            entries, start=1
        ):
            request = item.get("request")
            if isinstance(request, Text):  # `request` 可以是 URL 字符串
                request = {"method": "GET", "url": request}
            if not isinstance(request, dict):
                case.add_warning(f"item {item.get('name')!r} 没有 request → 已跳过")
                continue

            used: Set[Text] = set()
            warnings: List[Text] = []
            step_name = str(item.get("name") or f"step {step_index}")
            if not split_folders and folder_path:
                # 合并成一个用例时，步骤名带上**完整** folder 路径（否则嵌套层级会丢）
                step_name = " / ".join(list(folder_path) + [step_name])

            # 0918-7 / L6：修复前 f-string 写错了位置——`.join` 作用在整个 f-string 上
            # （`f"item: {name} / ".join(folder_path)`），产出形如 `"FolderAitem: 登录 / "`
            # 的拼接垃圾，而非预期的 `item: 登录 / FolderA`。
            folder_prefix = "/".join(folder_path)
            source = (
                f"item: {item.get('name')} / {folder_prefix}"
                if folder_prefix
                else f"item: {item.get('name')}"
            )
            step = IRStep(name=step_name, source=source)

            # ---- URL
            raw_url, params, _path_vars, url_warnings = _url_from_request(request, warnings)
            warnings.extend(url_warnings)
            raw_url = substitute_variables(raw_url, case_variables, warnings, used, dynamic_used)
            if not raw_url:
                step.add_warning("没有解析出 URL，已跳过")
                continue

            step.method = str(request.get("method") or "GET").upper()
            if params:
                step.params = {
                    key: substitute_variables(str(value), case_variables, warnings, used, dynamic_used)
                    for key, value in params.items()
                }

            # ---- headers / auth / body
            headers = _headers_from_request(request, case_variables, warnings, used, dynamic_used)
            auth = request.get("auth") if request.get("auth") is not None else base_auth
            headers.update(_auth_headers(auth, case_variables, warnings, f"步骤 {step_name!r}"))

            # NOTICE（0918-8 / H12）：body 的 JSON 判定要看 Content-Type（此时 headers 已就绪）
            content_type = ""
            for name, value in headers.items():
                if str(name).lower() == "content-type":
                    content_type = str(value)
                    break

            json_body, data, upload = _body_from_request(
                request, case_variables, warnings, used, dynamic_used, content_type=content_type
            )
            step.headers = headers
            step.json_body = json_body
            step.data = data
            step.upload = upload

            # NOTICE（0918-8 / M33）：cookie 从 `Cookie` 头（+ `request.cookie`）解析，
            # 不能像 HAR/curl 那样直接丢——Postman 里没有第二个 cookie 载体。
            cookie_dict = _cookies_from_request(
                request, case_variables, warnings, used, dynamic_used
            )

            # ---- 事件脚本：无法迁移，但要让人知道丢了什么
            for event in safe_dict_entries(item.get("event"), "item.event", warnings):
                listen = str(event.get("listen") or "")
                script = (
                    event.get("script") if isinstance(event.get("script"), dict) else {}
                ).get("exec") or []
                if isinstance(script, list) and script:
                    first_line = str(script[0]).strip()
                    script_warnings += 1
                    step.add_warning(
                        f"Postman 的 {listen} 脚本（JS）**无法无损迁移** → 已丢弃；"
                        f"首行：{first_line[:80]}"
                        "（请人工改写成 debugtalk.py 函数 + YAML 里的 ${func()} 或 hooks）"
                    )

            # ---- 断言（保存的示例响应）
            assertions, schema_file, schema_content, assertion_warnings = _assertions_from_item(
                item, step_index, _case_stem(group_name), assertion_mode
            )
            step.assertions = assertions
            step.schema_file = schema_file
            step.schema_content = schema_content
            warnings.extend(assertion_warnings)

            # ---- 敏感信息与变量
            step.headers, header_vars, header_warnings = sanitize_headers(step.headers)
            warnings.extend(header_warnings)
            step.cookies, cookie_vars, cookie_warnings = sanitize_cookies(cookie_dict)
            warnings.extend(cookie_warnings)

            body_vars: Dict[Text, Any] = {}
            if isinstance(step.json_body, (dict, list)):
                step.json_body, body_vars, body_warnings = sanitize_body(step.json_body)
                warnings.extend(body_warnings)
            elif isinstance(step.data, dict):
                step.data, body_vars, body_warnings = sanitize_body(step.data)
                warnings.extend(body_warnings)

            case_variables.update(header_vars or {})
            case_variables.update(cookie_vars or {})
            case_variables.update(body_vars or {})
            # L22：folder 级变量（越靠内层越优先）—— 放在 item 级**之前**，item 仍可覆盖它
            scoped_variables = _translate_variable_values(
                folder_variables, warnings, used, dynamic_used
            )
            scoped_variables.update(_item_variables(item, warnings, used, dynamic_used))
            # NOTICE（批次 8 / **L25**）：`config.variables` 是**扁平**的，跨 folder/item 重名的变量
            # 只能保留一个值（最后出现的那个）。这在 Postman 里是"作用域"，在生成的用例里做不到
            # ——今天的行为是**静默按最后一个值**解析所有步骤。这里至少在发生冲突时告警一次
            # （口径：宁可吵，也不静默给错值）。
            for name, value in scoped_variables.items():
                if name in scoped_seen and scoped_seen[name] != value:
                    scoped_conflicts.setdefault(name, [scoped_seen[name]]).append(value)
                else:
                    scoped_seen.setdefault(name, value)
            case_variables.update(scoped_variables)

            # 变量登记（`{{var}}` 引到但集合里没定义的）
            for name in used:
                if name not in case_variables:
                    case_variables[name] = "${ENV(" + name + ")}"
                    warnings.append(
                        f"变量 {name} 在集合里没有定义 → 已按 `${{ENV({name})}}` 占位，请确认取值来源"
                    )

            # ---- URL 拆分（base_url 进 config）
            origin = origin_of(raw_url)
            if not case.base_url:
                case.base_url = origin
                step_origins.append(origin)
            if origin and origin == case.base_url:
                step.url = split_url(raw_url)[1]
            elif not origin:
                # NOTICE（批次 8 / **L23**）：URL **以变量开头**（`${apiRoot}/v1/things`）时
                # `origin_of` 拿不到 netloc —— 这是**正常形态**，不是配置错误：
                # 变量解析后是绝对 URL，`build_url` 会直接放行（实测修完 L6 后 5/5 请求都打到了服务端）。
                # 修复前这里落到下面那条"host 与 base_url 不同"的分支，输出
                # 「该请求的 host（）与用例 base_url（空）不同」——信息量≈0 且会被误读成配置错了，
                # 而且是**每条请求一条**。现在给一条**准确**的说明，并且**每个用例只提示一次**。
                step.url = raw_url
                if not origin_unknown_warned:
                    origin_unknown_warned = True
                    step.add_warning(
                        f"该请求的 URL 以变量开头（{without_userinfo(raw_url)}）→ "
                        f"导入器**无法静态确定** origin，已原样保留完整 URL（运行期由变量决定）。\n"
                        f"  这**不是**配置错误：只要该变量解析出来是绝对 URL"
                        f"（如 `http://host/v1`），请求就能正常发出。\n"
                        f"  hint: 若运行期报 `ParamsError: base url missed!`，说明变量解析结果"
                        f"不是绝对 URL —— 请给它一个带 scheme 的值，或在 `config` 里手工补 `base_url`。"
                    )
            else:
                step.url = raw_url
                if origin and origin not in step_origins:
                    step_origins.append(origin)
                step.add_warning(
                    # NOTICE（批次 6 / M13）：抹掉 userinfo —— 这条告警会进 stdout 与 --report
                    f"该请求的 host（{without_userinfo(origin)}）与用例 "
                    f"`base_url`（{without_userinfo(case.base_url) or '空'}）不同，"
                    f"已保留绝对 URL：{without_userinfo(raw_url)}"
                )

            warnings.extend(warn_dynamic_values(step.params, "查询串"))
            step.warnings.extend(warnings)
            step.variables = {}
            # L25：记下本步骤的**作用域变量**（folder + item），循环结束后再决定
            # 「进 config」还是「落到这一步的 `variables:`」
            scoped_by_step.append(dict(scoped_variables))

            if schema_file and schema_content:
                case.extra_files[schema_file] = schema_content
            case.steps.append(step)

        # NOTICE（批次 8 收尾 / **L25**）：跨 folder / item **重名**的变量要落到**步骤级**。
        #
        # 修复前（`0919-29`）这里只告警，值仍按"最后出现的那个"解析**所有**步骤 ——
        # 因为导入器把所有作用域变量平铺进 `config.variables`，而那是**扁平**的。
        # 但生成物格式、IR 模型、运行期优先级其实**早就支持步骤级变量**
        # （`emit_yaml` 会写 `teststeps[].variables`；`IRStep.variables` 存在；
        # `runner.merge_step_variables` 的优先级是 step > extract > config），
        # 实测：两个步骤各自写 `variables: {who: …}` 时服务端确实收到两个不同的值
        # （`.tmp_report/probe_step_variables.py`）。缺的只是导入器没用它。
        #
        # 口径（刻意做到"不冲突时产物一个字都不变"）：
        #   - 某个名字在所有作用域里**只有一个值** → 照旧进 `config.variables`（既有行为）；
        #   - **有多个值** → 每个"作用域里带着它"的步骤写自己的值进 `step.variables`
        #     （运行期 step 覆盖 config）；`config.variables` 里保留**最后出现的那个值**，
        #     作为"没在作用域里的步骤"的兜底（这一点与修复前一致，没有变得更糟）。
        if scoped_conflicts:
            conflicting = set(scoped_conflicts)
            for step, scoped in zip(case.steps, scoped_by_step):
                overrides = {
                    name: value for name, value in scoped.items() if name in conflicting
                }
                if overrides:
                    step.variables = overrides
            detail = "\n".join(
                f"    {name}：{len(set(values))} 种值（{values}）"
                for name, values in sorted(scoped_conflicts.items())
            )
            fallback = "、".join(
                f"{name}={case_variables.get(name)!r}" for name in sorted(conflicting)
            )
            case.add_warning(
                "同一用例文件里有**跨 folder / item 重名**的变量（Postman 里那是**作用域**）→ "
                "已按作用域生成**步骤级 `variables:`**，每个步骤取自己作用域里的值：\n"
                f"{detail}\n"
                f"  `config.variables` 里保留的是**兜底值**（{fallback}）——"
                "没在任何作用域里的步骤才会用到它。\n"
                "  已验证：运行期优先级是 `step.variables` > 上一步 extract > `config.variables`，"
                "所以同一文件里同名变量可以各是各的值。\n"
                "  仍建议顺手看一眼：① 变量名在同一文件内最好唯一；"
                "② 需要跨用例共享时，按顶层 folder 拆成多个用例文件（**默认行为**）。"
            )

        # 变量里名字像凭据的：默认值改成 `${ENV(...)}`（避免把明文写进 YAML）
        secret_warned: Set[Text] = set()
        _harden_secret_variables(case_variables, case, secret_warned)
        # 步骤级变量同样要过（L25 之后它们也会写进生成物，不能成为明文通道）
        for step in case.steps:
            _harden_secret_variables(
                step.variables, case, secret_warned, location=f"步骤 {step.name!r} 的"
            )

        case.variables = {
            name: value
            for name, value in case_variables.items()
            if name in _used_variable_names(case, case_variables)
            or name in base_variables
        }
        if dynamic_used:
            case.add_warning(
                "集合用到了 Postman 动态变量，已映射为框架函数："
                + "、".join(sorted(dynamic_used))
                + f"（需要 debugtalk.py 提供 {list(DYNAMIC_HELPER_FUNCTIONS)}，导入时会自动写入模板）"
            )
        if script_warnings:
            case.add_warning(
                f"共有 {script_warnings} 段 Postman 脚本（pre-request/test）无法迁移 → 已在对应步骤给出首行提示"
            )
        if not case.steps:
            case.add_warning("该分组里没有可导入的请求")
        cases.append(case)

    # M5（0918-2）：统一收口脱敏。
    # 这里尤其重要——Postman 的**集合级变量**（`variable[]`）原先只按"名字像凭据"过滤，
    # 而 `jwt`/`session`/`cookie` 不在旧清单里 → 未被引用的变量也会连明文一起进
    # `config.variables`，且**零告警**。
    result = sanitize_cases([case for case in cases if case.steps] or cases)
    # 批次 9-0（M9）：素材里若出现 `${...}` 字面量 → 告警（只告警，不擅自改写）
    warn_source_expressions(result, collection, "Postman 集合")
    return result


def _harden_secret_variables(
    mapping: Dict[Text, Any],
    case: IRCase,
    already_warned: Set[Text],
    location: Text = "",
) -> None:
    """把"名字像凭据"的变量值改写成 `${ENV(name)}`（明文不入库）。

    NOTICE（批次 8 收尾 / **L25**）：这段逻辑原先只作用于 `case_variables`；L25 之后
    同名变量可能被写进**步骤级** `variables:`（生成物里同样是明文可见的位置），
    所以抽成函数、两处都用同一份判据 —— 否则"按作用域生成步骤级变量"会顺手开一个
    **明文凭据通道**（`folder.variable.password = 'S3cr3t'` 会原样进 YAML）。
    `already_warned` 只用来**去重告警**（同一个名字出现在 config 与多个步骤时只提示一次）；
    **改写本身必须每个映射各做一次** —— 第一版把去重写成了 `if name in already_warned: continue`，
    于是 config 那遍处理过之后，步骤级那遍**跳过改写**、明文原样进了 YAML
    （`test_scoped_secret_name_is_not_written_as_plaintext` 立刻抓到，见 `0919-31` §三）。
    """
    for name, value in list(mapping.items()):
        if (
            SECRET_NAME_PATTERN.search(name)
            and isinstance(value, str)
            and value
            and not value.startswith("${")
        ):
            mapping[name] = "${ENV(" + name + ")}"
            if name in already_warned:
                continue
            already_warned.add(name)
            case.add_warning(
                f"{location}变量 {name} 的值看起来是凭据 → "
                f"已改为 `${{ENV({name})}}`（明文不入库）"
            )


def _used_variable_names(
    case: IRCase, variables_to_scan: Dict[Text, Any] = None
) -> Set[Text]:
    """扫一遍已生成的 YAML 结构，找出实际引用到的变量名（`${name}` 形式）。

    NOTICE（批次 5 / **H6**）：**必须连 `case.base_url` 一起扫**。
    `{{baseUrl}}` / `{{host}}` / `{{gateway}}` 只出现在 URL 的 host 段上，是 Postman
    里最常见的写法；修复前这里只扫 `case.steps`，于是这种变量被下面的过滤器
    （`case.variables = {名: 值 for ... if 名 in _used_variable_names(case)}`）判成
    "没被引用"而整个删掉：

    ```text
    生成物  base_url: https://${baseUrl}      ← 引用了
    config.variables                          ← 却什么都没有（连定义都被删了）
    告警    变量 baseUrl 在集合里没有定义 → 已按 `${ENV(baseUrl)}` 占位   ← 说了要占位，实际没登记
    ```

    运行期 `VariableNotFound: baseUrl not found in {}`，而按提示设了环境变量也无效
    （占位符根本没进 `variables`）。同一文件头还写着"占位符的值定义在 config.variables 里"，
    属**主动误导**。本模块 docstring 承诺的正是"并登记到 `config.variables`"。

    NOTICE（批次 A / **H5**）：**必须连「变量的值」与「步骤级变量」一起扫**。
    修复前只扫 `case.steps` + `case.base_url`，于是「一个变量的值引用另一个变量」这种
    **Postman 最常见的 folder 变量组织方式**被整条剪掉（实测 `.tmp_audit/verify_prune.py`）：

    ```text
    folder 变量:  baseHost = "http://127.0.0.1:18080"
                  apiRoot   = "{{baseHost}}/v1"        ← baseHost 只出现在这里
    步骤 url:     "{{apiRoot}}/ok"

    生成 YAML:    variables: {apiRoot: ${baseHost}/v1}   ← baseHost 的定义被删了
    hconvert:     导出即验证通过（load_testcase + hmake 干跑 + 语法检查）→ exit 0
    hrun:         VariableNotFound: ['baseHost']          ← 生成物**必然**跑不起来
    ```

    `validate_emitted_case` 按设计**不解析变量**，所以这道防线结构上抓不到它 ——
    必须在剪枝这一步就不能把依赖删掉。`base_variables`（集合级）不参与剪枝，
    所以中招的一直是 **folder/item 级**变量。

    NOTICE（参数 `variables_to_scan`）：剪枝发生在 `case.variables = {...}` **赋值之前**，
    那一刻 `case.variables` 还是空的 —— 所以要扫的候选集合必须由调用方显式传进来
    （本函数是模块私有，只有一个调用点）。`case.variables` 也一并扫，是为了让
    「已经装配好的 IRCase」传给本函数时也成立（防御性，不改变当前行为）。
    """
    used: Set[Text] = set()

    def scan(value: Any) -> None:
        if isinstance(value, str):
            # NOTICE（0920 批次 4 / **N24**）：扫描面要覆盖**框架真正会解析的**形态。
            # 修复前是 `[A-Za-z_][0-9A-Za-z_]*`，与 `_to_python_placeholder` 当时
            # "保留 CJK" 的口径**不一致** → 中文变量被判"没被引用"而删掉定义。
            # 现在两处都用「ASCII 标识符」，并且额外把 `_uXXXX` 转义形态一并纳入
            # （那是 `_to_python_placeholder` 的产物），保证"改写后的名字也扫得到"。
            for match in re.finditer(r"\$\{([A-Za-z_][0-9A-Za-z_]*)\}", value):
                used.add(match.group(1))
            # 兜底：万一还有没被改写的非 ASCII 引用（例如用户手工写的 YAML），
            # 也要扫到 —— 宁可多留一个变量定义，也不能把依赖删掉。
            for match in re.finditer(r"\$\{([^}\s]+)\}", value):
                used.add(match.group(1))
        elif isinstance(value, dict):
            for item in value.values():
                scan(item)
        elif isinstance(value, list):
            for item in value:
                scan(item)

    # 用例级字段：`base_url` 是唯一会承载 `${...}` 的（文件名/来源名不进 YAML 的取值域）
    scan(case.base_url)

    # NOTICE（批次 A / H5）：变量**值之间**的引用（`{apiRoot: ${baseHost}/v1}`）。
    # 扫的是「全部候选变量」而不是「最终保留的那些」—— 一遍就能把整条依赖链留下
    # （A 引用 B、B 引用 C 时，扫全部值即同时看到 B 与 C）。
    # 多扫出来的名字只会让某个变量**被保留**（无害：多一个没被用到的定义不影响用例），
    # 绝不会让谁被删掉 —— 方向是「宁可多留，不可错删」。
    scan(variables_to_scan if variables_to_scan is not None else {})
    scan(case.variables)

    for step in case.steps:
        scan(step.headers)
        scan(step.cookies)
        scan(step.json_body)
        scan(step.data)
        scan(step.params)
        scan(step.upload)
        scan(step.url)
        scan(step.assertions)
        # NOTICE（批次 A / H5）：步骤级变量也是 `${...}` 的载体（L25 之后它们会写进生成物）
        scan(step.variables)
    return used


def _case_stem(name: Text) -> Text:
    """用例名 → schema 文件名词干（**保留非 ASCII**，见 `ir.safe_file_stem` 的 NOTICE）。"""
    return safe_file_stem(name)


def convert_postman_file(path: Text, **kwargs) -> List[IRCase]:
    """读取 Postman 集合文件并转换。"""
    with open(path, mode="r", encoding="utf-8-sig") as fp:
        try:
            collection = json.load(fp)
        except ValueError as ex:
            raise ValueError(f"Postman 集合不是合法 JSON：{path}\n{ex}") from ex

    if not isinstance(collection, dict) or "item" not in collection:
        raise ValueError(
            f"看起来不是 Postman Collection（缺少 item 字段）：{path}\n"
            "提示：Postman 里导出时请选「Collection v2.1」格式"
        )

    kwargs.setdefault("case_name", os.path.splitext(os.path.basename(path))[0])
    return convert_postman(collection, **kwargs)


def required_helper_functions(cases: List[IRCase]) -> List[Text]:
    """这些用例需要输出目录的 debugtalk.py 提供哪些 helper（Postman 动态变量映射用）。

    NOTICE: 必须扫**全部**会承载表达式的字段 —— 第一版漏了 `params`，
    于是「动态变量只出现在查询串」的集合不会触发 helper 检查（测试抓到了）。
    """
    needed: List[Text] = []
    text = "\n".join(
        json.dumps(
            {
                "url": [step.url for step in case.steps],
                "params": [step.params for step in case.steps],
                "headers": [step.headers for step in case.steps],
                "cookies": [step.cookies for step in case.steps],
                "json": [step.json_body for step in case.steps],
                "data": [step.data for step in case.steps],
                "upload": [step.upload for step in case.steps],
                "validate": [step.assertions for step in case.steps],
            },
            ensure_ascii=False,
            default=str,
        )
        for case in cases
    )
    for name in DYNAMIC_HELPER_FUNCTIONS:
        if f"${{{name}(" in text:
            needed.append(name)
    return needed
