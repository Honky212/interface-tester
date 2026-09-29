"""导入器（curl / HAR / Postman / OpenAPI → InterfaceTester YAML）的统一中间表示。

设计约束（来自评估文档 3.6，逐条都影响这里的写法）
--------------------------------------------------
1. **输出标准 YAML，而不是直接生成 Python** → 复用现有 `hmake` 渲染链路，导入器对运行时**零侵入**；
2. **IR 字段必须与 `models.py` 的 `TRequest` / `KNOWN_REQUEST_FIELDS` 对齐**，
   否则会生成「框架不认」的 YAML（`pydantic` 默认 `extra="ignore"`，写错字段会被静默丢弃）；
3. **合规红线**：Cookie / Authorization / token 等敏感值**一律替换成 `${...}` 占位**，
   明文凭据绝不写进 YAML（占位符在 `config.variables` 里定义成 `${ENV(名字)}`，
   所以生成的用例只要设了环境变量就能直接跑）；
4. 每个源都要**冻结「支持子集」**，不支持的写法必须**明确告警**（写进 `warnings`），不许静默丢弃。

术语
----
``IRStep`` 对应 YAML 里的一个 `teststep`；``IRCase`` 对应一个用例文件（`config` + `teststeps`）。

NOTICE（0918-2 / M5）：第 3 条红线**曾经不成立** —— 四个适配器各自为政，
OpenAPI 那条链路一次 `sanitize_*` 都没调用，查询串 / 字符串表单体 / 非白名单敏感头
在三四个源里都漏。现在改为**单一收口点** `sanitize_case()`（每个适配器返回前调用一次），
判定「什么是凭据」统一走 `utils.is_sensitive_key`，新增一个源天然继承脱敏。
对抗性测试见 `tests/sensitive_data_leak_test.py`。
"""

import re
from base64 import b64encode
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Text, Tuple
from urllib.parse import unquote, urlparse, urlunparse

from interfacetester.utils import (
    dump_json_container,
    is_sensitive_key,
    parse_json_container,
)

# --------------------------------------------------------------------------- 敏感信息
# NOTICE（0918-2）：判定「什么是凭据」**不再各自维护清单**，统一走
# `utils.is_sensitive_key`（大小写不敏感、分隔符归一后子串匹配）。
# 修复前这里有两份窄清单，漏判了 Cookie / 查询串 / `X-Signature` / 裸 `token` /
# `X-Amz-*` 等一大批真实存在的凭据载体。
#
# 下面这个正则只用于**保留 scheme 的特殊处理**（`Bearer x` / `Basic y` 要保住前缀），
# 不再充当白名单。
_BEARER_LIKE_HEADER_PATTERN = re.compile(r"(?i)^(authorization|proxy-authorization)$")


def is_secret_name(name: Any) -> bool:
    """判断头名 / 字段名 / 参数名是否是凭据（统一口径，见 `utils.is_sensitive_key`）。"""
    return is_sensitive_key(name)


def _is_placeholder(value: Any) -> bool:
    """值是否已经是 `${...}` 占位符（幂等：已占位的不再处理）。"""
    return isinstance(value, str) and "${" in value


def to_placeholder_name(prefix: Text, raw: Text) -> Text:
    """把「头名/字段名/cookie 名」转成合法的变量名：`X-Api-Key` → `HEADER_X_API_KEY`。"""
    normalized = re.sub(r"[^0-9A-Za-z]+", "_", raw).strip("_").upper()
    return f"{prefix}_{normalized}" if normalized else prefix


def safe_file_stem(raw: Any, default: Text = "case") -> Text:
    """把任意名字转成**文件名安全**的词干，且**保留非 ASCII 字母/数字**。

    NOTICE（批次 5 / **H5**）：这个函数存在的唯一原因是修复前的两处写法——
    `re.sub(r"[^0-9A-Za-z]+", "_", name)`——会把**非 ASCII 整段抹掉**：

    ```text
    "audit_pm / 用户管理"  ->  "audit_pm_"  ->  .strip("_")  ->  "audit_pm"
    "audit_pm / 订单管理"  ->  "audit_pm_"  ->  .strip("_")  ->  "audit_pm"     ← 两个不同用例同一个词干
    ```

    生成物里的两个用例于是**引用同一个 schema 文件**，`schemas/` 下只剩后写的那一份
    （断言断到了**别人的**响应形状）。而用例自身的 `.yml` 文件名是**保留中文**的
    （`audit_pm_用户管理.yml`），所以"保持中文"在本仓本来就被证明可用 ——
    词干没有理由比文件名更激进。

    判据：**Unicode 字母/数字**（`str.isalnum()`，中文、日文、带音标的拉丁字母都是 `True`）
    与 `-`/`_` 保留，其余一律折成 `_`，连续 `_` 压成一个，首尾去掉，小写化。
    纯 ASCII 名字的结果与修复前**逐字节一致**（`"a b"`→`a_b`、`"a.b"`→`a_b`），
    所以本改动只影响"原先会被抹掉字符"的那一类名字。

    NOTICE: 这里**不**做"唯一性"保证 —— 两个不同名字仍可能归一到同一个词干
    （`用户 管理` 与 `用户-管理`）。跨用例的重名由 CLI 的写盘冲突护栏拦（**响亮报错**），
    见 `cli.main_convert`。
    """
    text = str(raw if raw is not None else "")
    cleaned = "".join(
        char if (char.isalnum() or char in "-_") else "_" for char in text
    )
    return re.sub(r"_{2,}", "_", cleaned).strip("_").lower() or default


def placeholder_var(prefix: Text, raw: Text) -> Text:
    """占位变量名（不含 `${}`）。"""
    return to_placeholder_name(prefix, raw)


def placeholder_ref(name: Text) -> Text:
    """占位引用写法：`${NAME}`。"""
    return "${" + name + "}"


def env_default(name: Text) -> Text:
    """占位变量在 `config.variables` 里的默认值：从环境变量读，避免明文入库。"""
    return "${ENV(" + name + ")}"


def _sanitize_header_value(name: Text, value: Text) -> Tuple[Text, Optional[Tuple[Text, Text]], List[Text]]:
    """把单个敏感头的值换成占位符，返回 ``(新值, (变量名, 变量默认值), 告警)``。"""
    warnings: List[Text] = []
    lowered = value.strip().lower()
    if _BEARER_LIKE_HEADER_PATTERN.match(str(name)) and lowered.startswith("bearer "):
        var = "AUTH_TOKEN"
        return f"Bearer {placeholder_ref(var)}", (var, env_default(var)), [
            f"Authorization: Bearer 已替换为 `Bearer ${{{var}}}`（值从环境变量读取，明文不入库）"
        ]
    if _BEARER_LIKE_HEADER_PATTERN.match(str(name)) and lowered.startswith("basic "):
        var = "AUTH_BASIC"
        return f"Basic {placeholder_ref(var)}", (var, env_default(var)), [
            f"Authorization: Basic 已替换为 `Basic ${{{var}}}`（值应为 base64(user:pass)，从环境变量读取）"
        ]
    var = placeholder_var("HEADER", name)
    return placeholder_ref(var), (var, env_default(var)), [
        f"敏感请求头 {name} 已替换为 {placeholder_ref(var)}（值从环境变量读取，明文不入库）"
    ]


def sanitize_headers(
    headers: Optional[Dict[Text, Any]]
) -> Tuple[Dict[Text, Any], Dict[Text, Any], List[Text]]:
    """把敏感请求头替换成占位符。

    Returns:
        ``(headers, variables, warnings)``；`variables` 里是占位符的默认值（`${ENV(...)}`）。

    NOTICE（0918-2）：判定改用统一的 `is_secret_name`（**远宽于**修复前的锚定白名单），
    因此 `X-Signature` / 裸 `token` / `X-Amz-Security-Token` / `X-Session-Id` 这类
    真实存在的凭据头不再漏判；已占位的值跳过（可安全重复调用）。
    """
    cleaned: Dict[Text, Any] = {}
    variables: Dict[Text, Any] = {}
    warnings: List[Text] = []

    for name, value in (headers or {}).items():
        if (
            is_secret_name(name)
            and isinstance(value, str)
            and value
            and not _is_placeholder(value)
        ):
            new_value, variable, notes = _sanitize_header_value(str(name), value)
            cleaned[name] = new_value
            if variable:
                variables[variable[0]] = variable[1]
            warnings.extend(notes)
        else:
            cleaned[name] = value

    return cleaned, variables, warnings


def sanitize_cookies(
    cookies: Optional[Dict[Text, Any]]
) -> Tuple[Dict[Text, Any], Dict[Text, Any], List[Text]]:
    """把 Cookie 值替换成占位符（按 cookie 名逐个占位，便于只替换需要保密的那几个）。"""
    cleaned: Dict[Text, Any] = {}
    variables: Dict[Text, Any] = {}
    warnings: List[Text] = []

    for name, value in (cookies or {}).items():
        if _is_placeholder(value):
            cleaned[name] = value
            continue
        var = placeholder_var("COOKIE", str(name))
        cleaned[name] = placeholder_ref(var)
        variables[var] = env_default(var)
        warnings.append(
            f"Cookie {name} 已替换为 {placeholder_ref(var)}（值从环境变量读取，明文不入库）"
        )

    if warnings:
        warnings.insert(
            0, "Cookie 会随请求发送，属于凭据，因此**默认全部占位化**（需要固定值时用环境变量注入）"
        )

    return cleaned, variables, warnings


def parse_cookie_header(cookie_text: Text) -> Dict[Text, Text]:
    """把 `Cookie` 请求头的值（`k=v; k2=v2`）解析成 cookie 字典。

    NOTICE（0918-8 / M33）：**三个源共用这一个解析器**。
    `Cookie` 头在 HAR 里被当"派生头"丢弃、由 `request.cookies` 兜住；curl 从 `-b` 拿；
    而 Postman **只有这个头**是 cookie 的载体 —— 修复前它被同一条"派生头"规则丢掉，
    且没有别处兜住，于是 cookie **静默消失**（生成的用例发的是另一个请求）。
    """
    cookies: Dict[Text, Text] = {}
    for item in str(cookie_text or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        cookies[name.strip()] = value.strip()
    return cookies


def sanitize_params(
    params: Optional[Dict[Text, Any]]
) -> Tuple[Dict[Text, Any], Dict[Text, Any], List[Text]]:
    """把**查询参数**里名字敏感的取值替换成占位符（0918-2 新增）。

    NOTICE: 修复前查询串完全没人管——四个源都会把 `?access_token=...` 明文写进 YAML。
    查询串是凭据最常见的落点之一（OAuth2 的 `access_token`、签名接口的 `sign`）。
    """
    cleaned: Dict[Text, Any] = {}
    variables: Dict[Text, Any] = {}
    warnings: List[Text] = []

    for name, value in (params or {}).items():
        if is_secret_name(name) and not _is_placeholder(value) and value not in (None, ""):
            var = placeholder_var("PARAM", str(name))
            cleaned[name] = placeholder_ref(var)
            variables[var] = env_default(var)
            warnings.append(
                f"查询参数 {name} 已替换为 {placeholder_ref(var)}（值从环境变量读取，明文不入库）"
            )
        else:
            cleaned[name] = value

    return cleaned, variables, warnings


def sanitize_url(url: Text) -> Tuple[Text, Dict[Text, Any], List[Text]]:
    """把 URL 里**内联查询串**中敏感的取值替换成占位符（0918-2 新增）。

    NOTICE: curl 源会把查询串留在 `url` 上（不拆进 `params`），所以这里必须单独处理，
    否则 `curl 'https://h/a?access_token=x'` 一定明文入库。
    只切查询串、只动名字敏感的那几段，URL 其余部分逐字保留。
    """
    if not isinstance(url, Text) or "?" not in url:
        return url, {}, []

    base, _, query = url.partition("?")
    variables: Dict[Text, Any] = {}
    warnings: List[Text] = []
    rebuilt: List[Text] = []

    for pair in query.split("&"):
        name, sep, value = pair.partition("=")
        if sep and value and is_secret_name(name) and not _is_placeholder(value):
            var = placeholder_var("PARAM", name)
            rebuilt.append(f"{name}={placeholder_ref(var)}")
            variables[var] = env_default(var)
            warnings.append(
                f"URL 查询参数 {name} 已替换为 {placeholder_ref(var)}（值从环境变量读取，明文不入库）"
            )
        else:
            rebuilt.append(pair)

    return f"{base}?{'&'.join(rebuilt)}", variables, warnings


def sanitize_string_body(
    text: Any, prefix: Text = "BODY"
) -> Tuple[Any, Dict[Text, Any], List[Text]]:
    """**字符串态请求体**的统一入口：先试 JSON，不是再按表单串处理。

    NOTICE（批次 4 / **H8 + N1**）：修复前字符串体一律交给 `sanitize_urlencoded_text`
    按 `k=v` 切分，而"其实不是表单串"的两类输入各有一种错法（**都实测过**）：

    | 输入（`data:` 里的一整串） | 修复前 |
    | --- | --- |
    | `{"username":"bob","password":"S3cr3t!","token":"abc123"}` | 值里没有 `=` → 命中 `"=" not in text` → **整段原样入库、零告警**（H8）。而生成文件头写着"敏感信息已替换成 `${...}` 占位"，**主动误导复核者** |
    | `{"password": "S3cr3t="}` | 在**值内部**下刀 → `{"password": "S3cr3t=${BODY_PASSWORD_S3CR3T}`：**非法 JSON** + `S3cr3t` 前缀仍明文可见（N1）。占位符名还是从损坏的键名派生的垃圾 |
    | `[{"user":"bob","password":"S3cr3t="}]` | 同上，垃圾名更长：`BODY_USER_BOB_PASSWORD_S3CR3T` |

    两类同一个根因：**缺"这是不是 JSON"的判别**。所以这里先 `parse_json_container`
    试一次，是 `dict`/`list` 就走**结构化**脱敏（`sanitize_body` 按字段名判定，与
    `json:` / HAR / Postman 三条结构化路径**同一口径**），再写回字符串。

    ### 两条刻意的口径

    1. **结果仍是字符串**（写回 `data:`，不改成 `json:`）：换字段会**改变真实请求**
       ——`requests` 收到 `json=` 会自己加 `Content-Type: application/json`，
       而 curl 原报文可能压根没有这个头。本函数只把**串里的内容**结构化脱敏。
       （"改坏请求体"正是 N1 的罪状，不能再引入一个新的。）
    2. **写回用紧凑写法**（见 `utils.dump_json_container` 的说明）：空白不参与 JSON 语义，
       重建是必然的（要替换值），选与手写报文最常见的紧凑写法一致。
       唯一可见影响是"带空格风格"的原文会变成紧凑风格 —— 语义完全一致。

    只认 `dict`/`list`：标量 JSON（`123`/`"abc"`）没有字段名可查，按原文处理。

    ### 第三条口径：**没有要替换的字段就一个字节都不动**

    重建必然改变空白风格（紧凑 vs 带空格），所以这里加了一道"没改动就返回原文"的闸门：
    `{"a": 1}` 这类**不含敏感字段**的 JSON 体原样返回（实测仍是 `'{"a": 1}'`，
    不是 `'{"a":1}'`）。这样转换器只动它必须动的地方，也顺带避免了"字节级签名体"
    在无凭据时被无谓重排。
    """
    parsed = parse_json_container(text)
    if parsed is None:
        return sanitize_urlencoded_text(text, prefix)

    cleaned, variables, warnings = sanitize_body(parsed)
    if cleaned == parsed:
        # 结构没变（没有任何字段命中凭据判定）→ 原文返回，连空白都不动
        return text, variables, warnings

    return dump_json_container(cleaned), variables, warnings


def sanitize_urlencoded_text(
    text: Text, prefix: Text = "BODY"
) -> Tuple[Text, Dict[Text, Any], List[Text]]:
    """把 `user=bob&password=x` 形态的**字符串请求体**里敏感字段的值替换成占位符（0918-2 新增）。

    NOTICE: `sanitize_body` 只处理 dict/list——字符串体没有键名可查，修复前**整段原样入库**。
    curl 在非 JSON Content-Type 下、HAR 在"其它 mimeType"下都会产出字符串体，是最容易漏的一类。

    NOTICE（批次 4）：本函数**只负责真正的表单串**。字符串体的入口是
    `sanitize_string_body`（它先判 JSON 再转到这里）——直接拿 JSON 调本函数会重现
    H8/N1 两种错法，别绕过上面那一层。
    """
    if not isinstance(text, Text) or "=" not in text:
        return text, {}, []

    variables: Dict[Text, Any] = {}
    warnings: List[Text] = []
    rebuilt: List[Text] = []

    for pair in text.split("&"):
        name, sep, value = pair.partition("=")
        if sep and value and is_secret_name(name) and not _is_placeholder(value):
            var = placeholder_var(prefix, name)
            rebuilt.append(f"{name}={placeholder_ref(var)}")
            variables[var] = env_default(var)
            warnings.append(
                f"表单体字段 {name} 已替换为 {placeholder_ref(var)}（值从环境变量读取，明文不入库）"
            )
        else:
            rebuilt.append(pair)

    return "&".join(rebuilt), variables, warnings


def sanitize_body(
    body: Any, path: Text = "", location: Text = "请求体字段"
) -> Tuple[Any, Dict[Text, Any], List[Text]]:
    """递归把 JSON / 表单体里「名字像凭据」的字段值替换成占位符。

    NOTICE: 只按**字段名**判定（`password`/`token`/`csrf`/`sign`…），不做值猜谜 ——
    误伤业务字段比漏掉一个字段更麻烦；漏掉的情况会以告警形式提示人工确认。

    NOTICE（0918-2）：字符串体走 `sanitize_string_body`（见那里：先判 JSON，再退回表单串），
    本函数只管 dict/list。

    NOTICE（批次 A / **H4**）：新增 `location` 参数——同一个递归脱敏现在也被
    **用例级变量**（`sanitize_case` 里 `config.variables` 的容器值）复用，
    而那里的告警不该说「请求体字段」。默认值保持原措辞，既有调用点一个字不改。
    """
    variables: Dict[Text, Any] = {}
    warnings: List[Text] = []

    if isinstance(body, dict):
        cleaned: Dict[Text, Any] = {}
        for key, value in body.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(value, (dict, list)):
                new_value, child_vars, child_warnings = sanitize_body(
                    value, child_path, location
                )
                cleaned[key] = new_value
                variables.update(child_vars)
                warnings.extend(child_warnings)
            elif (
                is_secret_name(key)
                and not _is_placeholder(value)
                and value not in (None, "")
            ):
                var = placeholder_var("BODY", str(key))
                cleaned[key] = placeholder_ref(var)
                variables[var] = env_default(var)
                warnings.append(
                    f"{location} {child_path} 已替换为 {placeholder_ref(var)}（值从环境变量读取）"
                )
            else:
                cleaned[key] = value
        return cleaned, variables, warnings

    if isinstance(body, list):
        cleaned_list = []
        for index, item in enumerate(body):
            new_item, child_vars, child_warnings = sanitize_body(
                item, f"{path}[{index}]", location
            )
            cleaned_list.append(new_item)
            variables.update(child_vars)
            warnings.extend(child_warnings)
        return cleaned_list, variables, warnings

    if isinstance(body, Text):
        return sanitize_string_body(body)

    return body, variables, warnings


def duplicated_names(pairs: List[Tuple[Text, Any]]) -> List[Text]:
    """返回**出现多于一次**的字段名（按首次出现顺序）。

    NOTICE（0919-2 / 缺陷 8）：转换层有三处落在**同一个结构限制**上——
    IR 里的 `params` / `data` / `upload` 都是 **dict**，装不下重名键，
    而 `a=1&a=2`（批量删除/批量查询接口的常见形态）恰恰需要装两个。
    修复前的口径是这样的：

    | 转换点 | 修复前行为 |
    |---|---|
    | HAR `queryString` | **告警** + 把原始查询串留在 URL 里（M33 的正面示范） |
    | HAR `postData.params` | 静默只留**最后**一个值 |
    | Postman `url.query` | 静默只留**最后**一个值 |

    同一个包、同一类输入、三种口径 —— 所以这里把「哪些名字重了」收成一个函数，
    三处共用（顺便把 M33 那段内联判重也换过来），口径只此一份。
    """
    counts: Dict[Text, int] = {}
    for name, _value in pairs:
        counts[name] = counts.get(name, 0) + 1
    return [name for name, count in counts.items() if count > 1]


def merge_variables(*mappings: Optional[Dict[Text, Any]]) -> Dict[Text, Any]:
    """按顺序合并变量（后面的覆盖前面的），忽略空值。"""
    merged: Dict[Text, Any] = {}
    for mapping in mappings:
        if mapping:
            merged.update(mapping)
    return merged


# --------------------------------------------------------------------------- 形态护栏（批次 E / L1）
def safe_dict_entries(
    container: Any, where: Text, warnings: List[Text]
) -> List[Dict[Text, Any]]:
    """安全遍历「**对象列表**」：容器形态不对 → 告警并返回空；列表里的非对象条目 → 跳过并告警。

    NOTICE（批次 E / **L1**）：三个导入器（Postman / HAR / OpenAPI）里有十几处
    `for entry in x or []: entry.get(...)`。只要素材里那个字段的形态与规范不符
    （最常见的现场就是 **漏了 `-`**：`parameters` / `servers` / `security` / `header`
    本该是「对象列表」却写成了一个对象），就会抛裸
    `AttributeError: 'str' object has no attribute 'get'` —— 用户看到的是 traceback，
    **看不出是哪个字段**；而在 OpenAPI 的 `parameters` 上更糟：写成对象时它会
    **静默丢掉全部参数**（包括 `required: true` 的查询参数），生成的用例少发参数却无人知晓。

    口径与 HAR 侧（批次 8 / L21）**完全一致**：字段级坏数据只影响它自己那一层 ——
    跳过坏条目、逐条告警，**其余字段照常导入**；容器整体形态不对时也给一条点名的告警，
    而不是让整批导入崩掉。

    Args:
        container: 待遍历的容器（期望是 list；None 视为空）。
        where: 告警里的位置文案（如 `url.query`、`parameters`）。
        warnings: 告警收集列表（直接追加，与三个适配器的既有风格一致）。
    """
    if container is None:
        return []

    if isinstance(container, dict):
        _append_warning(
            warnings,
            f"`{where}` 应该是**对象列表**，却写成了对象 → 已跳过（这一段整体丢失）。\n"
            f"  最常见的原因是漏了列表符号 `-`：列表写法是\n"
            f"      {where}:\n"
            f"        - key: value\n"
            f"  而不是 `{where}: {{key: value}}`。",
        )
        return []

    if not isinstance(container, list):
        _append_warning(
            warnings,
            f"`{where}` 的形态不对（{type(container).__name__}，期望是对象列表）→ 已跳过",
        )
        return []

    entries: List[Dict[Text, Any]] = []
    bad_indexes = []
    for index, entry in enumerate(container):
        if isinstance(entry, dict):
            entries.append(entry)
        else:
            bad_indexes.append(index)

    if bad_indexes:
        _append_warning(
            warnings,
            f"`{where}` 里有 {len(bad_indexes)} 个**非对象条目**（下标 {bad_indexes}）→ "
            f"已跳过该项，其余条目照常导入",
        )
    return entries


def _append_warning(warnings: List[Text], message: Text) -> None:
    """追加告警并**去重**（同一个字段可能被多个函数各扫一遍，例如 `request.header`
    同时被"取请求头"与"取 cookie"遍历 —— 不去重会在报告里出现两条一模一样的告警）。"""
    if message not in warnings:
        warnings.append(message)


# --------------------------------------------------------------------------- URL / 动态值
def split_url(url: Text) -> Tuple[Text, Text]:
    """把 URL 拆成 ``(base_url, path)``：`https://h:8080/a?b=1` → `('https://h:8080', '/a?b=1')`。"""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "", url
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return base_url, path


def origin_of(url: Text) -> Text:
    """URL 的 origin（`scheme://host:port`），取不到时返回空串。"""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


# --------------------------------------------------------------------------- URL userinfo（凭据）
# NOTICE（0918-8 / H10）：`https://user:pass@host/path` 是 curl 的合法写法，HAR / Postman 的
# 字符串 URL 里也可能出现，而 **userinfo 就是凭据**（语义与 `-u/--user` 完全一致）。
# 修复前四个源都没有处理它：`split_url()` 把整段 `netloc` 当 `base_url`，
# 于是**明文口令直接落进生成的 YAML**（实测三源：`base_url: https://alice:S3cr3tP%40…@api.example.com`），
# 而且生成文件头还写着「敏感信息已替换成 ${...} 占位」——主动误导复核者。
#
# 处置放在**收口点** `sanitize_case()`（与 M5 同一处，新增源天然继承）：
# 剥掉 userinfo → 转成 `Authorization: Basic …` → 交给既有 `sanitize_headers` 占位成
# `Basic ${AUTH_BASIC}`（值从环境变量读）。这样既不泄漏，也**不改变请求语义**
# （凭据仍然会带上，只是运行期从环境变量取）。
def split_userinfo(url: Text) -> Tuple[Text, Optional[Text]]:
    """把 URL 里的 `user[:pass]@` 剥出来：返回 ``(干净 URL, userinfo 原文或 None)``。

    只处理 `netloc` 里的 userinfo（路径/查询里的 `@` 不受影响）；userinfo 里的百分号编码
    会被解码（`S3cr3tP%40ssw0rd` → `S3cr3tP@ssw0rd`），否则算出来的 base64 是错的。
    """
    if not isinstance(url, Text) or "@" not in url:
        return url, None

    parsed = urlparse(url)
    if not parsed.netloc or "@" not in parsed.netloc:
        return url, None

    raw_userinfo, _, host = parsed.netloc.rpartition("@")
    if not raw_userinfo:
        return url, None

    return urlunparse(parsed._replace(netloc=host)), unquote(raw_userinfo)


def describe_opaque_value(value: Any) -> Text:
    """**展示用**：描述一个"不该回显原文"的值（长度 + 形态），不泄露内容。

    NOTICE（0920 批次 4 / **N21**）：M13 修的是"URL 里的 userinfo 不许进告警"，
    但**告警文本回显其它凭据原文**这条路径当时漏了两处（curl 被丢弃的 `-d` 体、
    Postman 无法解析的 `Cookie` 头）。而告警不只是打日志 ——
    `hconvert --report` 会把**全部**告警写进 markdown 报告，等于进了 CI 产物。

    实测（`.tmp_audit/verify_sub_f1_f3.py`）：

    ```text
    curl … -d 'user=bob&password=CANARY…&token=tok-CANARY…' -F 'avatar=@./a.png'
      告警: `-d` 的内容已被**丢弃**： 'user=bob&password=CANARY…&token=tok-CANARY…'   ← 明文
    Postman 头 Cookie: eyJhbGciOiJIUzI1NiJ9.CANARY…sig
      告警: `Cookie` 头的值无法解析成 `k=v`（已忽略）：'eyJ…CANARY…sig'              ← 明文
    ```

    判据：**能定位问题**就够了，原文没有必要。这里按形态给出可辨识的描述：

    | 形态 | 输出 |
    | --- | --- |
    | 像表单串（含 `=`，如 `user=bob&password=x`） | 只列**字段名**（`user`、`password`…），值一律不出现 |
    | 像 JWT（`a.b.c` 三段 base64url） | `JWT 形态（3 段）` |
    | 其它 | `长度 N 的不透明值（首字符 X）` |

    NOTICE: **字段名可以留**（它是排查线索，且 `password` 这个"名字"不是凭据）；
    判断"字段名是否敏感"用 `utils.is_sensitive_key`，敏感名字后加 `=***`。
    """
    if value is None:
        return "空值"
    text = value if isinstance(value, str) else str(value)
    if not text:
        return "空字符串"

    # 表单串：只回显字段名
    if "=" in text:
        names = []
        for pair in text.split("&"):
            name, sep, _val = pair.partition("=")
            if not sep:
                continue
            name = name.strip()
            if not name:
                continue
            names.append(
                f"{name}=***" if is_sensitive_key(name) else name
            )
        if names:
            return "表单串，字段名：" + "、".join(names)

    # JWT 形态
    if text.count(".") == 2 and len(text) > 20 and " " not in text:
        return f"JWT 形态（3 段、共 {len(text)} 字符）"

    return f"长度 {len(text)} 的不透明值（首字符 {text[0]!r}）"


def without_userinfo(url: Any) -> Text:
    """**展示用**：把 URL 里的 `user:pass@` 抹成 `***@`，其余原样。

    NOTICE（批次 6 / **M13**）：告警文本会把 URL / host 抄进 stdout，
    而 `hconvert --report` 还会把告警写进 markdown 报告 —— 也就是**所有能看到 CI 日志的人**。
    修复前三个源的"多主机"告警都在 `sanitize_case` 剥 userinfo **之前**生成，
    于是 `curl 'https://alice:S3cr3t@a.example.com/x'` 的口令**原样**进了告警：

    ```text
    该步骤的 host（https://alice:S3cr3t@a.example.com）与用例 `base_url`（…）不同，已保留绝对 URL：…
    ```

    生成的 YAML 本身是干净的（`sanitize_case` 会剥掉并转成 Authorization 占位）——
    泄漏的**只有告警这一路**。这里用"**抹成 `***@` 而不是整段删掉**"：
    告警要说清楚"是这个带凭据的 host 与 base_url 不同"，把 userinfo 完全删掉会让
    `https://a.example.com` 与 `https://a.example.com`（userinfo 不同）看起来一样，反而误导。
    """
    if not isinstance(url, str) or "@" not in url:
        return url
    clean, userinfo = split_userinfo(url)
    if userinfo is None:
        return url
    # 找回 host 部分（`split_userinfo` 已把它切出来了）
    return clean.replace("://", "://***@", 1)


def _has_authorization(headers: Optional[Dict[Text, Any]]) -> bool:
    """该步骤是否**已经有** Authorization 头（有就不覆盖，只告警）。"""
    return any(
        _BEARER_LIKE_HEADER_PATTERN.match(str(name)) for name in (headers or {})
    )


# 动态值识别（只告警、不自动改写：改了可能让用例失去原本的语义）
DYNAMIC_VALUE_PATTERNS: List[Tuple[Text, "re.Pattern"]] = [
    ("13 位时间戳(毫秒)", re.compile(r"^\d{13}$")),
    ("10 位时间戳(秒)", re.compile(r"^\d{10}$")),
    ("UUID", re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")),
    ("长随机 hex", re.compile(r"^[0-9a-fA-F]{16,}$")),
    ("ISO 时间", re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")),
]


def detect_dynamic_value(value: Any) -> Optional[Text]:
    """识别「看起来是动态值」的字符串（时间戳 / UUID / 长随机串 / ISO 时间），返回人类可读的种类。"""
    if not isinstance(value, str):
        return None
    for label, pattern in DYNAMIC_VALUE_PATTERNS:
        if pattern.match(value):
            return label
    return None


def warn_dynamic_values(values: Dict[Text, Any], where: Text) -> List[Text]:
    """对一组键值里疑似动态的值给出参数化建议（**只提示，不改写**）。

    NOTICE（0918-2 / 泄漏修复）：**名字像凭据的键直接跳过**。两个理由，第二个才是关键：
      ① 语义上多余：这类值马上会被 `sanitize_*` 换成 `${...}` 占位符（已经从环境变量读了），
         再提示"建议改成 `${func()}` 或 extract 变量"是自相矛盾的；
      ② **它本身就在泄漏**：告警文本会把值**原样**拼进去，而告警既打 stdout、
         又会被 `hconvert --report` 写进 markdown 报告 —— 于是"提醒你别写死动态值"
         的那句话，恰恰把凭据抄送给了所有能看日志的人。
         实测：`-H 'X-Signature: <32 位 hex>'` 命中"长随机 hex"规则，
         修复前告警与报告里都是明文。

    NOTICE（批次 8 收尾 / **M14**）：**标量体**（`json: 123` 这种合法 JSON 顶层标量）
    没有"键值"可提示 → 直接返回空列表。修复前 `(values or {}).items()` 对 int/bool/float
    抛 `AttributeError: 'int' object has no attribute 'items'`（实测 `.tmp_report/probe_m14.py`）；
    当前调用方都自带 `isinstance(..., (dict, list))` 守卫，所以它是**潜伏**的，
    但 M14 放宽了 `json:` 的接受面，这个边界不该再留。
    """
    warnings: List[Text] = []
    if not isinstance(values, dict):
        return warnings
    for key, value in (values or {}).items():
        if is_secret_name(key):
            # 凭据一律不给"动态值"提示（详见上面的 NOTICE）
            continue
        kind = detect_dynamic_value(value)
        if kind:
            warnings.append(
                f"{where} 的 {key} 看起来是动态值（{kind}：{value}）——"
                f"建议改成 `${{func()}}` 或上一步 `extract` 出来的变量，否则重放会失败"
            )
    return warnings


# --------------------------------------------------------------------------- 表达式字面量（M9）
# 批次 9-0（M9）：素材里的 `${...}` 会被**原样**写进生成的 YAML，而运行期
# `parser.parse_string` 会把它当**表达式执行**（`${func()}` 真的会调用函数）。
# 修复前 hconvert 对此**一个字都不提示**，于是一份素材就能构成一条 RCE 链：
#   实测（见 docs/缺陷修复日志0918-9.md）：Postman 集合的变量里放
#   `__import__('os').system(...)`、URL 里放 `${eval($p)}` → `hconvert` 零告警 →
#   `hrun` 真的执行了它，用例还报 passed / exit 0。
#
# 这里**只告警、不擅自改写**，两个理由都是实测出来的：
#   ① 素材里的 `${var}` 可能是作者**有意**引用框架变量（已按本框架约定写的集合），
#      报错会误伤合法素材，静默改写会把变量引用变成字面量；
#   ② 也**不能**用 `$$` 转义来中和：`$${eval($p)}` 只挡住外层的 `$`，里面的 `$p`
#      照样被变量替换（实测结果是 `${eval(PAYLOAD)}`，请求语义已经坏了）。
# 真正的收口在运行期那一侧：`parser.ALLOWED_BUILTINS_IN_INTERPOLATION`
# （内置函数正向白名单，`eval`/`exec`/`open`/`__import__`/`getattr`/`input` 一律拒绝）。
_EXPRESSION_LITERAL_REGEX = re.compile(r"\$\{[^{}\n]{1,120}\}")

# 告警里最多列举几个样例（避免素材里几百处 `${}` 时把报告刷爆）
_EXPRESSION_SAMPLE_LIMIT = 3


def _iter_material_strings(node: Any):
    """递归遍历素材（str / dict / list / tuple / set）里的所有字符串。"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_material_strings(key)
            yield from _iter_material_strings(value)
    elif isinstance(node, (list, tuple, set)):
        for item in node:
            yield from _iter_material_strings(item)


def find_expression_literals(material: Any, limit: int = 50) -> List[Text]:
    """找出素材里所有 `${...}` 字面量（去重、保序、最多 `limit` 条）。"""
    found: List[Text] = []
    for text in _iter_material_strings(material):
        for match in _EXPRESSION_LITERAL_REGEX.findall(text):
            if match not in found:
                found.append(match)
                if len(found) >= limit:
                    return found
    return found


def warn_source_expressions(
    cases: List["IRCase"], material: Any, where: Text = "素材"
) -> List[Text]:
    """素材里出现 `${...}` → 给**第一个用例**登记一条告警，并返回找到的表达式。

    只看第一个用例登记：告警说的是**素材**的性质（整个文件），
    给每个用例都挂一条只会在 `--report` 里重复刷屏。
    """
    found = find_expression_literals(material)
    if not found or not cases:
        return found

    samples = "、".join(f"`{item}`" for item in found[:_EXPRESSION_SAMPLE_LIMIT])
    more = (
        f"（另有 {len(found) - _EXPRESSION_SAMPLE_LIMIT} 处类似写法）"
        if len(found) > _EXPRESSION_SAMPLE_LIMIT
        else ""
    )
    cases[0].add_warning(
        f"{where}里出现 {len(found)} 处 `${{...}}` 表达式：{samples}{more}。\n"
        f"    它们会被**原样**写进生成的 YAML，并在运行期被当作表达式执行"
        f"（`${{func()}}` 会真的调用函数）。若这些是原作者有意写的变量/函数引用，"
        f"请确认它引用的变量与函数都可信；若它本该是「原样发给接口的文字」，"
        f"请在素材或生成物里改掉——导入器**不擅自改写**"
        f"（`$$` 转义会连内部的 `$变量` 一起改坏）。\n"
        f"    hint: 运行期只允许内置函数白名单（`eval`/`exec`/`open`/`__import__` 等会被拒绝），"
        f"危险的内置调用会明确报错；详见 docs/能力清单.md「变量与函数」一节。"
    )
    return found


# --------------------------------------------------------------------------- IR
@dataclass
class IRStep:
    """一个步骤（对应 YAML 的 `teststeps` 元素）。字段与 `TRequest` 对齐。"""

    name: Text
    method: Text = "GET"
    url: Text = "/"
    params: Dict[Text, Any] = field(default_factory=dict)
    headers: Dict[Text, Any] = field(default_factory=dict)
    cookies: Dict[Text, Any] = field(default_factory=dict)
    json_body: Any = None  # -> request.json
    data: Any = None  # -> request.data（表单字符串/字典）
    upload: Dict[Text, Text] = field(default_factory=dict)  # -> request.upload
    verify: Optional[bool] = None  # -> request.verify
    proxies: Optional[Dict[Text, Text]] = None  # -> request.proxies
    assertions: List[Dict[Text, Any]] = field(default_factory=list)  # -> validate
    extracts: Dict[Text, Text] = field(default_factory=dict)  # -> extract
    variables: Dict[Text, Any] = field(default_factory=dict)  # -> step.variables
    # 该步骤关联的 schema 文件（HAR 自动断言用；内容是「引用」，不能内联，见 P2-b）
    schema_file: Optional[Text] = None
    schema_content: Optional[Dict[Text, Any]] = None
    # 溯源与告警（不写进 YAML，由 CLI 汇总输出）
    source: Text = ""
    warnings: List[Text] = field(default_factory=list)

    def add_warning(self, message: Text) -> None:
        self.warnings.append(message)


@dataclass
class IRCase:
    """一个用例文件（对应 `config` + `teststeps`）。"""

    name: Text
    base_url: Text = ""
    variables: Dict[Text, Any] = field(default_factory=dict)
    steps: List[IRStep] = field(default_factory=list)
    warnings: List[Text] = field(default_factory=list)
    source: Text = ""
    # 用例级附加产物：{相对路径: 内容}（例如 HAR 生成的 schema 文件）
    extra_files: Dict[Text, Any] = field(default_factory=dict)

    def add_warning(self, message: Text) -> None:
        self.warnings.append(message)

    def all_warnings(self) -> List[Text]:
        """用例级 + 步骤级告警（步骤级带步骤名前缀，便于定位）。"""
        collected = list(self.warnings)
        for step in self.steps:
            collected.extend(f"[{step.name}] {message}" for message in step.warnings)
        return collected


# --------------------------------------------------------------------------- 统一收口
def sanitize_case(ir_case: IRCase) -> IRCase:
    """**导入器的最后一道防线**：把 IRCase 里仍然是明文的凭据全部换成 `${...}` 占位符。

    为什么要有这一层（而不是继续在各适配器里就地处理）
    --------------------------------------------------
    修复前四个适配器各自为政，实测（`tests/sensitive_data_leak_test.py`）漏了四类：
      ① **查询串**（四个源全漏：curl 留在 url 上、HAR/Postman/OpenAPI 走 params）；
      ② **字符串表单体**（curl 非 JSON Content-Type、HAR 其它 mimeType）；
      ③ **非白名单敏感头**（`X-Signature` / 裸 `token` / `X-Amz-Security-Token`）；
      ④ **OpenAPI 整条链路**（一次 `sanitize_*` 都没调用）。
    逐条打补丁只能治标：只要"脱敏"散落在四个适配器里，第五个源一定还会漏。

    因此收口成**一个函数 + 一个判定**（`is_secret_name` → `utils.is_sensitive_key`），
    由每个适配器在 return 之前调用一次。新增源天然继承，且可以用一条不变量测试钉住
    （见 `tests/sensitive_data_leak_test.py` 的 canary 断言）。

    幂等性：已经写成 `${...}` 的值一律跳过，所以重复调用不会叠加占位符
    （适配器里已就地处理过的部分不会被二次改写）。

    覆盖范围：`url` 内联查询串 / `params` / `headers` / `cookies` / `json_body` /
    字符串或字典形态的 `data` / 用例级 `variables` 里的字面量凭据。

    **不覆盖**（明确记录，避免误以为已覆盖）：响应侧数据、`upload` 的文件路径、
    以及"值像凭据但名字不像"的字段（只按名字判定，见 `sanitize_body` 的说明）。

    Args:
        ir_case: 适配器产出的用例（**原地修改**并返回，便于链式调用）

    Returns:
        同一个 ``IRCase`` 实例。
    """
    added_variables: Dict[Text, Any] = {}
    case_warnings: List[Text] = []

    # 0918-8 / H10：先把 base_url 里的 userinfo 剥掉（它是凭据，且会被原样写进 config.base_url）。
    # 这里剥下来的凭据按“用例级凭据”处理：下面逐步骤补到没有 Authorization 的步骤上。
    clean_base_url, case_userinfo = split_userinfo(ir_case.base_url or "")
    if case_userinfo is not None:
        ir_case.base_url = clean_base_url
        case_warnings.append(
            "URL 里的账号密码（userinfo）已从 `base_url` 移除，并转为 "
            "`Authorization: Basic ${AUTH_BASIC}` 占位"
            "（值应为 base64(user:pass)，从环境变量读取；明文不入库）"
        )

    for step in ir_case.steps:
        # 步骤 URL 上的 userinfo：同样是凭据
        step.url, step_userinfo = split_userinfo(step.url)
        userinfo = step_userinfo if step_userinfo is not None else case_userinfo

        if step_userinfo is not None:
            step.add_warning(
                "URL 里的账号密码（userinfo）已移除，并转为 "
                "`Authorization: Basic ${AUTH_BASIC}` 占位（明文不入库）"
            )

        if userinfo:
            if _has_authorization(step.headers):
                step.add_warning(
                    f"URL 里的 userinfo 已丢弃：该步骤已有 Authorization 头，"
                    f"保留原有的、不覆盖（原值 `Basic …` 已占位化）"
                )
            else:
                # 与 `-u/--user` 走**同一条**链路：先放 base64，再由 sanitize_headers 占位
                step.headers = dict(step.headers or {})
                step.headers["Authorization"] = (
                    "Basic " + b64encode(userinfo.encode("utf-8")).decode("ascii")
                )

        step.url, variables, warnings = sanitize_url(step.url)
        added_variables.update(variables)
        step.warnings.extend(warnings)

        step.params, variables, warnings = sanitize_params(step.params)
        added_variables.update(variables)
        step.warnings.extend(warnings)

        step.headers, variables, warnings = sanitize_headers(step.headers)
        added_variables.update(variables)
        step.warnings.extend(warnings)

        step.cookies, variables, warnings = sanitize_cookies(step.cookies)
        added_variables.update(variables)
        step.warnings.extend(warnings)

        # `sanitize_body` 内部对字符串体转交 `sanitize_string_body`（它先判是不是 JSON，
        # 再决定走结构化还是表单串），所以 json_body / data（字典或字符串）都能覆盖。
        step.json_body, variables, warnings = sanitize_body(step.json_body)
        added_variables.update(variables)
        step.warnings.extend(warnings)

        step.data, variables, warnings = sanitize_body(step.data)
        added_variables.update(variables)
        step.warnings.extend(warnings)

        # NOTICE（0918-8 / M22）：`upload` 里放的是**本地文件路径**（`-F k=@file` 的语义），
        # 这里**刻意不做脱敏**——本模块的 docstring 一直这么写着（「不覆盖…`upload` 的文件路径」），
        # 但代码曾经对 `step.upload` 调 `sanitize_body`，于是文件名命中凭据关键词时会被占位化：
        #     实测 `-F 'private_key=@/home/alice/key.pem'` → `upload: {private_key: ${BODY_PRIVATE_KEY}}`
        #   → 路径彻底丢失，生成的用例运行期必然失败（会去打开一个"名字等于环境变量值"的文件），
        #     而**文件名不是凭据**，占位化没有任何安全收益。现在与 docstring 对齐：路径原样保留。

    # 用例级变量：名字像凭据、值还是字面量的，改成从环境变量读
    for name, value in list(ir_case.variables.items()):
        if (
            is_secret_name(name)
            and value not in (None, "")
            and not _is_placeholder(value)
        ):
            ir_case.variables[name] = env_default(str(name))
            case_warnings.append(
                f"用例变量 {name} 的明文值已替换为 {env_default(str(name))}"
                f"（值从环境变量读取，明文不入库）"
            )
            continue

        # NOTICE（批次 A / **H4**）：**容器值的内部字段也要按名判定**。
        #
        # 修复前这里只处理「变量名像凭据」这一种情况，于是**变量名不像凭据、值里却有凭据字段**
        # 的形态整块漏过（实测 `.tmp_audit/verify_claims.py`，真 `hconvert`）：
        #
        #     Postman: variable = [{key: "loginPayload",
        #                           value: {"username": "bob", "password": "S3cr3t"}}]
        #     生成 YAML: variables: {loginPayload: {username: bob, password: S3cr3t}}   ← 明文入库
        #
        # 而**同一份字典当请求体**时会被 `sanitize_body` 正确占位成 `${BODY_PASSWORD}`
        # —— 所以这不是「机制做不到」，而是漏用了既有机制（`sanitize_body` 早就实现了
        # 「按键名递归 dict/list」）。这里把变量值也接到同一条递归规则上：
        # 同一入口只剩一套覆盖，不再出现「dict 体脱敏、变量容器不脱敏」。
        #
        # 判据只对 dict/list 生效：标量值没有「字段名」可判，与 `sanitize_body` 的口径一致
        # （`sanitize_string_body` 那条字符串分支在这里**刻意不接**——变量值里的
        #  `k=v` / JSON 字符串体没有「字段」语义，误伤面大于收益，见 L20 的已知边界）。
        if isinstance(value, (dict, list)):
            sanitized_value, child_vars, child_warnings = sanitize_body(
                value, str(name), location=f"用例变量 {name!r} 的字段"
            )
            if child_vars:
                ir_case.variables[name] = sanitized_value
                for child_name, child_value in child_vars.items():
                    # 与下面同一口径：已有定义优先，不覆盖适配器自己登记的那份
                    added_variables.setdefault(child_name, child_value)
                case_warnings.extend(child_warnings)

    # 新增的占位变量不覆盖已有定义（同名时以适配器已有的为准，值形态一致）
    for name, value in added_variables.items():
        ir_case.variables.setdefault(name, value)

    if added_variables:
        case_warnings.append(
            f"已对 {len(added_variables)} 个凭据占位化（共 "
            f"{len(added_variables)} 个占位变量）；明文值不入库，"
            f"运行前请用环境变量注入对应取值"
        )

    ir_case.warnings.extend(case_warnings)
    return ir_case


def sanitize_cases(ir_cases: List[IRCase]) -> List[IRCase]:
    """对一批用例做统一脱敏（Postman / OpenAPI 会一次产出多个用例）。"""
    return [sanitize_case(ir_case) for ir_case in ir_cases]
