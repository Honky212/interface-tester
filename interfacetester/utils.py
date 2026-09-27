import base64
import collections
import copy
import itertools
import json
import os
import os.path
import platform
import random
import re
import sys
import time
import uuid
from collections.abc import Mapping
from multiprocessing import Queue
from typing import Any, Dict, List, Optional, Text

import requests
from loguru import logger

from interfacetester import __version__, exceptions
from interfacetester.models import VariablesMapping


""" run httpbin as test service
https://github.com/postmanlabs/httpbin

$ docker pull kennethreitz/httpbin
$ docker run -p 80:80 kennethreitz/httpbin

本仓库的测试不再依赖 docker：`tests/mock_server.py` 会在同地址起一个 httpbin 兼容子集，
它读取下面这两个端口常量（默认 80/443，与历史行为一致）。

NOTICE: 80/443 是**特权端口**——非 root 的 CI/容器里绑不上。此时把端口调大即可，
mock 与 `HTTP_BIN_URL` 会一起跟着变（同一个环境变量）：

    export INTERFACETESTER_HTTP_BIN_PORT=8000
    export INTERFACETESTER_HTTPS_BIN_PORT=8443

详见 `docs/ci/README.md` 的「坑 2」。
"""
HTTP_BIN_HOST = "127.0.0.1"
HTTP_BIN_PORT_ENV = "INTERFACETESTER_HTTP_BIN_PORT"
HTTPS_BIN_PORT_ENV = "INTERFACETESTER_HTTPS_BIN_PORT"


def resolve_mock_port(env_name: Text, default: int) -> int:
    """读取 mock 服务端口：未设置用 default（80/443），设置但非法则明确报错。

    NOTICE: 非法值不做静默回退——端口写错时静默用 80 会表现为「CI 上莫名 skip 一片用例」，
    比直接报错难查得多。
    """
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return default
    try:
        port = int(raw)
    except ValueError:
        raise exceptions.ParamsError(
            f"invalid {env_name}: {raw!r} is not an integer"
        ) from None
    if not 0 < port < 65536:
        raise exceptions.ParamsError(
            f"invalid {env_name}: {port} is out of range (1-65535)"
        )
    return port


HTTP_BIN_PORT = resolve_mock_port(HTTP_BIN_PORT_ENV, 80)
HTTPS_BIN_PORT = resolve_mock_port(HTTPS_BIN_PORT_ENV, 443)
HTTP_BIN_URL = f"http://{HTTP_BIN_HOST}:{HTTP_BIN_PORT}"
HTTPS_BIN_URL = f"https://{HTTP_BIN_HOST}:{HTTPS_BIN_PORT}"

# 请求超时默认值（秒）与环境变量入口；详见 resolve_default_timeout()
DEFAULT_TIMEOUT_SECONDS = 120
TIMEOUT_ENV = "INTERFACETESTER_TIMEOUT"


def get_platform():
    return {
        "interfacetester_version": __version__,
        "python_version": "{} {}".format(
            platform.python_implementation(), platform.python_version()
        ),
        "platform": platform.platform(),
    }


class GA4Client(object):
    """send events to Google Analytics 4 via Measurement Protocol.
    see https://developers.google.com/analytics/devguides/collection/protocol/ga4
    """

    def __init__(
        self, measurement_id: str, api_secret: str, debug: bool = False
    ) -> None:
        # GA 遥测已禁用：作为独立产品，不再上报事件到官方 Google Analytics。
        # NOTICE（0919-1 / L31）：这一行必须在**构造 Session 之前**——
        # 模块底部 `ga4_client = GA4Client("", "", False)` 在 import 期就会执行，
        # 修复前每次 `import interfacetester.utils` 都会白建一个 `requests.Session()`
        # （连带附带的 HTTPAdapter 与 urllib3 连接池），而 `send_event` 第一行就返回，
        # 那个会话**永远不会被用到**。现在改成惰性创建：只有真正要发事件时才建。
        self.__disabled = True
        self.http_client = None

        self.debug = debug
        if debug:
            uri = "https://www.google-analytics.com/debug/mp/collect"
        else:
            uri = "https://www.google-analytics.com/mp/collect"

        self.uri = f"{uri}?measurement_id={measurement_id}&api_secret={api_secret}"
        self.user_id = str(uuid.uuid4())
        self.common_event_params = get_platform()

    def send_event(self, name: str, event_params: dict = None) -> None:
        if self.__disabled:
            return

        if self.http_client is None:
            # 惰性创建（见 __init__ 的 NOTICE）：禁用态下永不执行到这里
            self.http_client = requests.Session()

        event_params = event_params or {}
        event_params.update(self.common_event_params)
        event = {
            "name": name,
            "params": event_params,
        }

        payload = {
            "client_id": f"{int(random.random() * 10**8)}.{int(time.time())}",
            "user_id": self.user_id,
            "timestamp_micros": int(time.time() * 10**6),
            "events": [event],
        }

        if self.debug:
            logger.debug(f"send GA4 event, uri: {self.uri}, payload: {payload}")

        try:
            resp = self.http_client.post(self.uri, json=payload, timeout=5)
        except Exception as err:  # ProxyError, SSLError, ConnectionError
            logger.error(f"request GA4 failed, error: {err}")
            return

        if resp.status_code >= 300:
            logger.error(
                f"validation response got unexpected status: {resp.status_code}"
            )
            return

        if not self.debug:
            return

        try:
            resp_body = resp.json()
            logger.debug(
                "get GA4 validation response, "
                f"status code: {resp.status_code}, body: {resp_body}"
            )
        except Exception:
            pass


# GA 遥测已禁用：作为独立产品，不再上报事件到官方 Google Analytics
ga4_client = GA4Client("", "", False)


def set_os_environ(variables_mapping):
    """set variables mapping to os.environ"""
    for variable in variables_mapping:
        os.environ[variable] = variables_mapping[variable]
        logger.debug(f"Set OS environment variable: {variable}")


def unset_os_environ(variables_mapping):
    """unset variables mapping to os.environ"""
    for variable in variables_mapping:
        os.environ.pop(variable, None)
        logger.debug(f"Unset OS environment variable: {variable}")


def get_os_environ(variable_name):
    """get value of environment variable.

    Args:
        variable_name(str): variable name

    Returns:
        value of environment variable.

    Raises:
        exceptions.EnvNotFound: If environment variable not found.

    """
    try:
        return os.environ[variable_name]
    except KeyError:
        raise exceptions.EnvNotFound(variable_name)


def lower_dict_keys(origin_dict):
    """convert keys in dict to lower case

    Args:
        origin_dict (dict): mapping data structure

    Returns:
        dict: mapping with all keys lowered.

    Examples:
        >>> origin_dict = {
            "Name": "",
            "Request": "",
            "URL": "",
            "METHOD": "",
            "Headers": "",
            "Data": ""
        }
        >>> lower_dict_keys(origin_dict)
            {
                "name": "",
                "request": "",
                "url": "",
                "method": "",
                "headers": "",
                "data": ""
            }

    """
    if not origin_dict or not isinstance(origin_dict, dict):
        return origin_dict

    return {key.lower(): value for key, value in origin_dict.items()}


def print_info(info_mapping):
    """print info in mapping.

    Args:
        info_mapping (dict): input(variables) or output mapping.

    Examples:
        >>> info_mapping = {
                "var_a": "hello",
                "var_b": "world"
            }
        >>> info_mapping = {
                "status_code": 500
            }
        >>> print_info(info_mapping)
        ==================== Output ====================
        Key              :  Value
        ---------------- :  ----------------------------
        var_a            :  hello
        var_b            :  world
        ------------------------------------------------

    """
    if not info_mapping:
        return

    content_format = "{:<16} : {:<}\n"
    content = "\n==================== Output ====================\n"
    content += content_format.format("Variable", "Value")
    content += content_format.format("-" * 16, "-" * 29)

    for key, value in info_mapping.items():
        if isinstance(value, (tuple, collections.deque)):
            continue
        elif isinstance(value, (dict, list)):
            value = json.dumps(value)
        elif value is None:
            value = "None"

        content += content_format.format(key, value)

    content += "-" * 48 + "\n"
    logger.info(content)


def omit_long_data(body, omit_len=512):
    """omit too long str/bytes"""
    if not isinstance(body, (str, bytes)):
        return body

    body_len = len(body)
    if body_len <= omit_len:
        return body

    omitted_body = body[0:omit_len]

    appendix_str = f" ... OMITTED {body_len - omit_len} CHARACTERS ..."
    if isinstance(body, bytes):
        appendix_str = appendix_str.encode("utf-8")

    return omitted_body + appendix_str


def sort_dict_by_custom_order(raw_dict: Dict, custom_order: List):
    def get_index_from_list(lst: List, item: Any):
        try:
            return lst.index(item)
        except ValueError:
            # item is not in lst
            return len(lst) + 1

    return dict(
        sorted(raw_dict.items(), key=lambda i: get_index_from_list(custom_order, i[0]))
    )


class ExtendJSONEncoder(json.JSONEncoder):
    """especially used to safely dump json data with python object,
    such as MultipartEncoder"""

    def default(self, obj):
        try:
            return super(ExtendJSONEncoder, self).default(obj)
        except (UnicodeDecodeError, TypeError):
            return repr(obj)


def json_safe_bytes(value: Any) -> Text:
    """bytes/bytearray → 可 JSON 序列化的文本：能按 UTF-8 解码就用文本，否则 base64。

    NOTICE（0919-15 / 登记项③）：这个函数**原先只存在于** `thrift/data_convertor.py`
    （0919-8 / §三.6b 为 thrift 的 `binary` 字段定的口径）。登记项③ 要把
    **同一口径**用到数据库的 `BLOB` 列上，而 `data_convertor` 顶部
    `from thrift.Thrift import TType` —— 让 DB 路径去 import 它会平白拖进 Apache thrift
    依赖。所以口径收口到 `utils`（本仓「共用能力放 utils、两边都调同一个」的既有做法），
    `data_convertor.json_safe_bytes` 保留为**同一个函数的引用**（有身份断言钉住「只有一份」）。

    口径本身（两侧共用，别再各写一份）：
    - 能按 UTF-8 解码 → 解码成文本：报告可读，且保持 0918-8 / M28 已经钉住的
      `thrift2dict(b"pong") == "pong"` 行为不变；
    - 不能解码 → base64：真正的二进制载荷，base64 是唯一无损选择，且**看得见**。
    已知代价：base64 文本与「恰好长得像 base64 的普通字符串」无法区分（要区分得改模型，
    属独立立项）。
    """
    raw = bytes(value)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(raw).decode("ascii")


def merge_variables(
    variables: VariablesMapping, variables_to_be_overridden: VariablesMapping
) -> VariablesMapping:
    """merge two variables mapping, the first variables have higher priority"""
    step_new_variables = {}
    for key, value in variables.items():
        if f"${key}" == value or "${" + key + "}" == value:
            # e.g. {"base_url": "$base_url"}
            # or {"base_url": "${base_url}"}
            continue

        step_new_variables[key] = value

    merged_variables = copy.copy(variables_to_be_overridden)
    merged_variables.update(step_new_variables)
    return merged_variables


# 变量名命中这些关键字时，认为其值是敏感信息，需要在 summary/报告中脱敏。
#
# NOTICE（0918-2 / M4-M5）：这份清单现在是**全框架唯一的"什么是凭据"判定**——
# 运行期脱敏（client/step_request）与导入器占位化（converters/ir）都走它，
# 避免"两处清单各自漂移，一处修了另一处漏"（导入器的 SQL 化占位原先就漏了
# Cookie/查询串/非白名单头四类，根因正是它有自己的一份清单）。
SENSITIVE_KEYWORDS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "secret_key",
    "private_key",
    "authorization",
    "credential",
    # 0918-2 补充：实测（tests/sensitive_data_leak_test.py）里这几类都是明文外泄的载体
    "cookie",  # Cookie / Set-Cookie 头
    "session",  # session / sessionId / PHPSESSID
    "jwt",
    "sid",
    "signature",
    "csrf",
    "xsrf",
    # 0918-8 / L22：`sign` 挪到了下面的**整段**匹配里（子串匹配会命中 `design`/`assignee`/
    # `signal_id`，而误判会让导入器把正常字段的值占位化 → 生成的用例运行期 EnvNotFound）。
    # 换成了它来兜「预签名 URL」这类载体（`presigned_url` 是凭据）。
    "presign",
)

MASKED_VALUE = "******"

# 名字里的分隔符统一成 `_` 再匹配：`X-Api-Key` → `x_api_key`（命中 `api_key`）。
# NOTICE: 不归一化的话 `is_sensitive_key("X-Api-Key")` 会因为短横线而**不命中**——
# 这是个很容易漏的方向：漏判 = 明文外泄，误判 = 只是显示上糊掉一点。
_KEY_SEPARATOR_PATTERN = re.compile(r"[-.\s]+")

# 驼峰边界也当分隔符：`smsOtp` → `sms_otp`、`userPin` → `user_pin`（配合下面的整段匹配）。
_KEY_CAMEL_BOUNDARY_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# **短关键词**：只能做**整段相等**匹配，不能像长关键词那样做子串匹配（0918-8 / L22）。
#
# 为什么：子串匹配下 `pin` 会命中 `shipping_address`（"ship**pin**g"）、`sig` 会命中 `design`
# （"de**sig**n"）——而误判的代价不只是"显示上糊掉"：**导入器**用同一份判定做占位化，
# 于是这些字段的值会被换成 `${...}` 占位，生成的用例运行期直接 `EnvNotFound`（照不出来）。
# 所以这里改成「按 `_`/驼峰切段后，任一段**完全等于**关键词」。
#
# 收录判断（逐条实测过误报面）：
#   - `sig`：Azure SAS 的签名参数（等价于凭据）——`?sv=…&sig=…`，实测修复前原样入库；
#   - `otp` / `pin`：短信验证码 / 支付密码，现场常见；
#   - `sign`：中文接口常见的 `sign` / `app_sign` / `X-Sign`（整段匹配下 `design`、`assignee`、
#     `signal_id` 都不再误判——那三个是 0918-2 §六.3 登记的假报，本次一并修掉）；
#   - **刻意不收录 `code`**：`errorCode` / `status_code` / `body.code`（SOAP 成功码！）都是正常字段，
#     一旦命中就会被占位化 → 生成用例直接跑不起来。宁可漏报，也不制造这种假报。
_SENSITIVE_EXACT_SEGMENTS = ("sig", "otp", "pin", "sign")


def is_sensitive_key(key: Any) -> bool:
    """判断名字（变量名/头名/字段名/参数名）是否疑似凭据。

    大小写不敏感、分隔符归一后做**子串**匹配（长关键词）；**短关键词**（见
    `_SENSITIVE_EXACT_SEGMENTS`）额外要求**整段相等**，避免 `shipping_address` 这类误报。
    总体取向：宁可多判（显示上糊掉），也不漏判（明文外泄）。
    """
    if not isinstance(key, str):
        return False

    # 驼峰先切开再归一化分隔符：`smsOtp` → `sms_otp`（子串关键词的行为不变，
    # 因为切出来的段落拼回去仍然包含原关键词）。
    camel_split = _KEY_CAMEL_BOUNDARY_PATTERN.sub("_", key)
    normalized_key = _KEY_SEPARATOR_PATTERN.sub("_", camel_split.lower())

    if any(keyword in normalized_key for keyword in SENSITIVE_KEYWORDS):
        return True

    segments = [segment for segment in normalized_key.split("_") if segment]
    return any(segment in _SENSITIVE_EXACT_SEGMENTS for segment in segments)


def parse_json_container(text: Any) -> Optional[Any]:
    """字符串形态的 **JSON 对象 / 数组**：能解析成 `dict`/`list` 就返回它，否则 `None`。

    NOTICE（批次 4 / **H8 + N1** 与 **M3 + N2**）：这个判别器是**两处共用的一条口径**——
    转换层（`converters.ir.sanitize_string_body`）与 runtime 展示层（本模块的
    `_mask_query_pairs`）都靠它把「其实不是表单串」的字符串从 `k=v` 切分路径里摘出来。

    为什么必须摘出来：JSON 的**值内部**可以合法地出现 `=`（base64 结尾的 `==`、
    带签名的串、`"a=b"`），按 `=` 切分会在值中间下刀，后果有两层（四组现场全部实测）：

    ```text
    转换层（改生成物，会入库）：       {"password": "S3cr3t="}
      -> {"password": "S3cr3t=${BODY_PASSWORD_S3CR3T}     非法 JSON + S3cr3t 前缀仍明文
    runtime 展示层（改日志副本）：     {"password": "CANARY="}
      -> {"password": "CANARY=******                      非法 JSON + CANARY 前缀仍明文
    ```

    而"值里恰好没有 `=`"的 JSON 更糟——**整段原样入库、零告警**（H8）。

    只认 `dict`/`list`：标量 JSON（`123`、`"abc"`、`true`）没有字段名可查，脱敏无从下手，
    按原文处理即可（它们也不含键名，没有"按名判定"的依据）。
    """
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    # 快速排除：表单串/普通文本几乎都以别的字符开头（也避免对每个字符串都跑一次解析）
    if not stripped or stripped[0] not in "[{":
        return None

    try:
        parsed = json.loads(stripped)
    except ValueError:
        # 不是合法 JSON（例如 `{a=1&b=2}` 这种"看起来像但其实是表单串"的输入）→ 走原路径
        return None

    return parsed if isinstance(parsed, (dict, list)) else None


def dump_json_container(parsed: Any) -> Text:
    """把结构化处理后的结果写回字符串，**紧凑写法** + `ensure_ascii=False`。

    NOTICE（口径，不是随手选的）：

    - **必须是字符串**：调用方拿到的是 `data:`（原始报文）而不是 `json:`（结构化字段）。
      换字段会**改变真实请求** —— `requests` 收到 `json=` 会自己加
      `Content-Type: application/json` 并按自己的风格重编码，而 curl 原报文可能压根没有这个头。
      所以这里只把**串里的内容**结构化脱敏，再写回同一种形态。
    - **紧凑**：重建是必然的（要替换值），空白不参与 JSON 语义，所以选与手写报文最常见的
      紧凑写法一致（`{"a":1}` 而不是 `{"a": 1}`），让生成物与用户原始报文尽量贴近。
      对照说明：框架的 `json:` 路径由 `requests` 编码，用的是**带空格**风格 ——
      两条路径的空白风格本就不同，这里不对齐它，只保证**语义一致**。
    - `ensure_ascii=False`：中文等非 ASCII 不被转成 `\\uXXXX`（否则报文内容虽然等价，
      但生成物会变得完全不可读）。
    """
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _mask_query_pairs(raw: Any) -> Any:
    """把 `a=1&password=x` 形态的键值对里，名字敏感的取值换成 ``******``。

    只按**名字**判定、只处理 `k=v` 段，不碰其它内容（所以对非表单串是安全的）。

    NOTICE（批次 4 / **N2**）：**先判是不是 JSON**。修复前对任何含 `=` 的字符串都按
    `partition("=")` 切分，于是 JSON 字符串体在**值内部**被下刀：

    ```text
    '{"password": "CANARY="}'  ->  '{"password": "CANARY=******'
    ```

    输出成了**非法 JSON**，而且值的**前半段仍然明文可见**（`CANARY` 还在），
    复核者看到 `******` 却以为已经收口。现在 JSON 对象/数组走**结构化**脱敏
    （`_mask_sensitive_value` → 按键名递归），值整体变成 `******`：

    ```text
    '{"password": "CANARY="}'  ->  '{"password":"******"}'
    ```
    """
    if not isinstance(raw, str) or "=" not in raw:
        return raw

    parsed = parse_json_container(raw)
    if parsed is not None:
        return dump_json_container(_mask_sensitive_value(parsed))

    masked_pairs = []
    for pair in raw.split("&"):
        name, sep, value = pair.partition("=")
        if sep and value and is_sensitive_key(name):
            masked_pairs.append(f"{name}={MASKED_VALUE}")
        else:
            masked_pairs.append(pair)

    return "&".join(masked_pairs)


def mask_sensitive_text(value: Any) -> Any:
    """对表单串（`user=bob&password=x`）按参数名脱敏，用于日志/报告展示。"""
    return _mask_query_pairs(value)


def mask_sensitive_url(url: Any) -> Any:
    """对 URL 里的 **userinfo** 与查询串里名字敏感的取值脱敏，用于日志/报告展示。

    NOTICE: 必须先切掉 `scheme://host/path` 再处理查询串——直接对整条 URL 跑
    键值对切分会把 `https://h/a?access_token` 当成参数名。

    NOTICE（0920 批次 2 / **N4**）：修复前**只**处理查询串，`scheme://user:pass@host/...`
    里的账号口令**原样保留**。实测（`.tmp_audit/verify_kernel_findings.py`）：

    ```text
    mask_sensitive_url("http://alice:CANARY@host/ok?token=CANARY")
      -> "http://alice:CANARY@host/ok?token=******"
                    ^^^^^^ 口令明文仍在（查询串那半边却糊了）
    ```

    `.run.log` 的 sink 是硬编码 `level="DEBUG"`（每个用例无条件落盘），summary 还会进
    `summary.json` / HTML / Allure —— 凭据因此直接外泄。导入器侧早就修过同一类问题
    （H10/M13：`converters/ir.py` 的 `split_userinfo` / `sanitize_case` 会把 userinfo 剥掉
    并转成占位），运行期这条链路当时漏了。

    口径：userinfo **整段**换成 `******`（保留 `@` 与 host，便于看出"这个 URL 带凭据"，
    与导入器 H10 的 `***@` 口径一致）；用户名不单独保留——`user:pass` 里哪一段算凭据
    无法可靠判断，整体糊掉最安全。
    """
    if not isinstance(url, str):
        return url

    masked = url
    # ── userinfo：`scheme://user:pass@host` ──
    # 只在 `//` 之后、第一个 `/`（或 `?`/`#`）之前的那一段里找 `@`，
    # 否则会把路径里合法的 `@`（如 `/users/@me`）误判成 userinfo。
    scheme_sep = masked.find("://")
    if scheme_sep != -1:
        authority_start = scheme_sep + 3
        authority_end = len(masked)
        for delimiter in ("/", "?", "#"):
            position = masked.find(delimiter, authority_start)
            if position != -1:
                authority_end = min(authority_end, position)
        authority = masked[authority_start:authority_end]
        if "@" in authority:
            host = authority.rsplit("@", 1)[1]
            masked = (
                masked[:authority_start]
                + f"{MASKED_VALUE}@{host}"
                + masked[authority_end:]
            )

    # ── 查询串 ──
    if "?" not in masked:
        return masked

    base, _, query = masked.partition("?")
    return f"{base}?{_mask_query_pairs(query)}"


def mask_sensitive_request_data(value: Any) -> Any:
    """请求侧展示用脱敏的统一入口（映射按键名递归 / 字符串按表单串）。

    NOTICE（0919-3 / L35）：判据必须是 `Mapping`，**不能**是 `dict`。
    修复前用的是 `isinstance(value, dict)`，于是**任何非 dict 的映射类型都被静默放过**：

      - `requests.cookies.RequestsCookieJar`：响应 `resp.cookies` 的真实类型，
        它继承 `MutableMapping` 但**不是** dict 子类；
      - `requests.structures.CaseInsensitiveDict`：`resp.headers` 的真实类型。

    实测（同一份数据、两种容器）：

    ```text
    mask_sensitive_request_data({"sessionid": "SECRET"})  -> {"sessionid": "******"}   ✅
    mask_sensitive_request_data(RequestsCookieJar(...))   -> 原样返回，明文仍在        ❌
    ```

    这是最坏的一类缺陷：**「看起来脱敏了，其实没有」**——调用方以为自己已经收口了，
    比完全不做脱敏更难发现。返回值统一成普通 `dict`，方便下游序列化与展示。

    NOTICE（批次 4 / **M3**）：非 `Mapping` 分支修复前**只交给 `_mask_query_pairs`**
    （它只认 `str`），于是**顶层是 list 的请求体整块漏过**：

    ```text
    mask_sensitive_request_data([{"user": "bob", "password": "CANARY"}])
      -> [{'user': 'bob', 'password': 'CANARY'}]        ← 明文进 .run.log / summary / 报告
    mask_sensitive_request_data({"user": "bob", "password": "CANARY"})
      -> {'user': 'bob', 'password': '******'}          ← 同一入口，dict 体是对的
    ```

    `json: [{...}]`（数组体的批量接口）是**正常 YAML 写法**，不是边角输入。
    现在非映射一律走 `_mask_sensitive_value`（它会递归 list/tuple，并对字符串套用
    `_mask_query_pairs`）——**同一入口只剩一套覆盖**，不再出现"dict 体脱敏、list 体不脱敏"。
    """
    if isinstance(value, Mapping):
        return mask_sensitive_variables(value)

    return _mask_sensitive_value(value)


def mask_sensitive_variables(variables_mapping: VariablesMapping) -> VariablesMapping:
    """对疑似密钥的变量做脱敏，用于写日志/报告/summary 等展示场景。

    NOTICE:
        - 递归脱敏 dict（按变量名匹配）以及 list/tuple 里的元素
          （修复前只递归 dict，``{"headers": ["token: abc"]}`` 或
          ``{"tokens": [{"password": "123456"}]}`` 这类结构会整体漏过）；
        - 仅用于展示，**不可**用于运行时变量传递（如 testcase 引用的 export_vars），
          否则会把 ``******`` 真的传给被引用用例。
        - 0919-3 / L35：判据放宽到 `Mapping`（见 `mask_sensitive_request_data` 的 NOTICE）。
    """
    if isinstance(variables_mapping, Mapping):
        masked = {}
        for key, value in variables_mapping.items():
            if is_sensitive_key(key):
                masked[key] = MASKED_VALUE
            else:
                masked[key] = _mask_sensitive_value(value)
        return masked

    return _mask_sensitive_value(variables_mapping)


def _mask_sensitive_value(value: Any) -> Any:
    """按容器类型递归脱敏：映射走 mask_sensitive_variables，list/tuple/str 逐层处理。

    NOTICE（0919-3 / L35）：映射判据同样是 `Mapping` 而不是 `dict`——嵌套结构里
    放一个 `CaseInsensitiveDict` / `RequestsCookieJar` 时，用 `dict` 判断会**整块漏过**
    （外层脱敏了、里层明文还在，最容易被误判成「已经收口」）。

    NOTICE（批次 4 / **M3 收口**）：新增 `str` 分支 → `_mask_query_pairs`
    （它内部还会先判是不是 JSON，见那里）。这是把"字符串值"也纳入同一条递归规则，
    带来的覆盖（修复前全部漏过，均已实测）：

    ```text
    [{"password": "CANARY"}]              顶层 list 体              -> 现在脱敏（M3）
    ["password=CANARY"]                   list 里的表单串           -> 现在脱敏
    {"body": "password=CANARY"}           嵌套位置放字符串体         -> 现在脱敏
    {"body": '{"password": "CANARY="}'}   嵌套位置放 JSON 字符串体   -> 现在脱敏且仍是合法 JSON（N2）
    ```

    **不能**反过来"只给顶层 list 打补丁"：那正是本模块先前的问题形态——
    同一个入口两套覆盖，漏的那一套总要等下一个人再发现一次。
    """
    if isinstance(value, Mapping):
        return mask_sensitive_variables(value)

    if isinstance(value, list):
        return [_mask_sensitive_value(element) for element in value]

    if isinstance(value, tuple):
        return tuple(_mask_sensitive_value(element) for element in value)

    if isinstance(value, str):
        return _mask_query_pairs(value)

    return value


def resolve_default_timeout() -> float:
    """解析框架默认请求超时（秒）。

    优先级：环境变量 ``INTERFACETESTER_TIMEOUT`` > 内置默认值 ``DEFAULT_TIMEOUT_SECONDS``。
    NOTICE: 默认值维持 120s 不变（改数值会改变既有用例的失败时机与最长等待时长），
    这里只提供「不改代码就能按环境调整」的入口，例如 CI 上统一调短：
    ``INTERFACETESTER_TIMEOUT=5 hrun test.yml``。
    """
    raw_value = os.environ.get(TIMEOUT_ENV)
    if raw_value is None or raw_value.strip() == "":
        # 未显式配置 → 静默使用默认值
        return float(DEFAULT_TIMEOUT_SECONDS)

    try:
        timeout = float(raw_value)
    except ValueError:
        logger.warning(
            f"invalid {TIMEOUT_ENV}: {raw_value!r}, "
            f"fallback to {DEFAULT_TIMEOUT_SECONDS}s"
        )
        return float(DEFAULT_TIMEOUT_SECONDS)

    if timeout <= 0:
        # 0 或负数是非法超时（不是「无限等待」的合法表达）→ 回落默认值
        logger.warning(
            f"invalid {TIMEOUT_ENV}: {raw_value!r}, "
            f"fallback to {DEFAULT_TIMEOUT_SECONDS}s"
        )
        return float(DEFAULT_TIMEOUT_SECONDS)

    return timeout


def ensure_timeout_value(value: Any, where: Text = "timeout") -> float:
    """把超时值规范成数字（秒）。

    NOTICE: ``$var`` / ``${ENV(TIMEOUT)}`` 解析出来的是**字符串**，直接传给 requests 会在
    更深处抛难懂的错误；这里统一转换，并在非法时给出明确报错。bool 也一并拒绝
    （``timeout: true`` 是笔误，而不是 1 秒）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise exceptions.ParamsError(
            f"invalid {where}: {value!r}（应为秒数，int 或 float）"
        )

    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            raise exceptions.ParamsError(
                f"invalid {where}: {value!r}（应为秒数，int 或 float）"
            )

    return float(value)


def ensure_int_value(
    value: Any,
    where: Text = "value",
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """把「变量 / ENV 来的数字」规范成 int，非法时给出可读报错（批次 8 / **L2**）。

    NOTICE: `.env` / CSV / `${ENV(...)}` 出来的值**永远是字符串**，而
    `gen_random_string($len)` / `get_timestamp($len)` 直接把它当 int 用 —— 修复前：

        ${gen_random_string($len)}  len="8"  → TypeError: 'str' object cannot be interpreted as an integer
        ${get_timestamp($len)}      len="16" → ParamsError: timestamp length can only between 0 and 16.

    第二条尤其误导：**16 明明合法**，用户会去改长度而不是去看"值是字符串"。

    口径与 `ensure_timeout_value` 同一套（**变量来的数字要转**）：
    - 收 `int` 与数字字符串（含首尾空白的 `" 8 "`，`int()` 本来就容忍）；
    - 收**整数值的 float**（`8.0` → 8），但**拒绝** `8.5` —— 截断就是本项目反复登记的
      静默坏值来源（同 L5 的 `200.7`）；
    - `bool` 一律拒绝（`true` 是笔误，不是 1）；
    - 其余形态报 `ParamsError`，消息里带上**实际收到的值**与合法区间。
    """
    if isinstance(value, bool):
        raise exceptions.ParamsError(
            f"invalid {where}: {value!r}（应为整数，bool 不接受 —— `true` 通常是笔误）"
        )
    if isinstance(value, int):
        numeric = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise exceptions.ParamsError(
                f"invalid {where}: {value!r}（应为整数，非整数的小数会被截断，已拒绝）"
            )
        numeric = int(value)
    elif isinstance(value, str):
        try:
            numeric = int(value.strip())
        except ValueError:
            raise exceptions.ParamsError(
                f"invalid {where}: {value!r}（应为整数；"
                f"来自变量 / ENV 的值是字符串，请确认真的是纯数字）"
            )
    else:
        raise exceptions.ParamsError(
            f"invalid {where}: {value!r}（应为整数，收到 {type(value).__name__}）"
        )

    if minimum is not None and numeric < minimum:
        raise exceptions.ParamsError(
            f"invalid {where}: {numeric}（应不小于 {minimum}）"
        )
    if maximum is not None and numeric > maximum:
        raise exceptions.ParamsError(
            f"invalid {where}: {numeric}（应不大于 {maximum}）"
        )
    return numeric


# pydantic v2 认的 bool 字符串（与 ``TRequest.verify`` 的构造期校验完全同口径）：
# 只有下面这些能被转成 bool，其余（`"maybe"` / `2` / `"2"` / 列表…）一律拒绝。
# 刻意**抄 pydantic 的取值集合**而不是自己编一套，否则「YAML 能写、Python API 不能写」
# 或反过来，就会出现两套口径（本仓最忌讳的同一件事两个答案）。
_BOOL_STRINGS = {
    "true": True,
    "false": False,
    "yes": True,
    "no": False,
    "on": True,
    "off": False,
    "1": True,
    "0": False,
}


def ensure_bool_value(value: Any, where: Text = "value") -> bool:
    """把「变量 / ENV / 手写 Python API 来的布尔值」规范成真正的 `bool`。

    NOTICE（0921 / **verify 链式 setter 绕过校验**）：`TRequest` 与 `TConfig`
    **没有** `validate_assignment=True`（只有 `TStep` 有），于是链式 setter
    完全不校验。修复前实测：

        step.set_verify("false")        -> 存成字符串 'false'（不是 False）
        Config("x").verify("false")     -> 存成字符串 'false'

    `.env` / CSV / `${ENV(...)}` 出来的值**永远是字符串**，所以手写 Python API 时
    「把环境变量直接喂给 verify」是很自然的写法。字符串 `"false"` 在 Python 里
    **是真值**（`bool("false") is True`），后果有两层：

    1. `requests` 把它当成 **CA 证书路径** →
       `OSError: Could not find a suitable TLS CA certificate bundle, invalid path: false`
       （报错里完全看不出是"这个值本该是布尔"）；
    2. `client._warn_if_tls_verification_disabled` 的判据是 `if verify is None or verify:`
       → 非空字符串被判成"已开启校验"→ **TLS 关闭告警被吞掉**，
       用户既看不到提示、又拿到一个与"验证"无关的报错。

    口径：
    - 收 `bool` 原样返回；收 pydantic 认的数字/字符串（见 `_BOOL_STRINGS`）；
    - `None` **原样返回 None**（`TRequest.verify` 的 None 有专有语义：
      「未显式设置」→ 运行时回落到 `config.verify`，所以不能把它压成 False）；
    - 其余一律 `ParamsError`，消息里带上**实际收到的值**。
    """
    if value is None:
        # None = 「没写」而不是「假」——`TRequest.verify=None` 会回落到 config.verify，
        # 压成 False 会把"没设置"变成"显式关闭"，语义反转。
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
        raise exceptions.ParamsError(
            f"invalid {where}: {value!r}（应为布尔值 true/false；"
            f"只接受 0 / 1，其它数字是笔误）"
        )
    if isinstance(value, str):
        # 刻意**不** `.strip()`：pydantic 对 bool **不做**去空白
        # （实测 `TRequest(verify=' true ')` 直接 ValidationError），
        # 这里若宽容一点就会出现"YAML 报错、Python API 放行"的两套口径
        # ——本仓最忌讳同一件事两个答案。大小写 pydantic 是认的（'TRUE' → True）。
        normalized = value.lower()
        if normalized in _BOOL_STRINGS:
            return _BOOL_STRINGS[normalized]
        raise exceptions.ParamsError(
            f"invalid {where}: {value!r}（应为布尔值 true/false；"
            f"来自变量 / ENV 的值是字符串，请确认真的是 true 或 false —— "
            f"注意字符串 'false' 在 Python 里是**真值**，直接传给 requests 会被"
            f"当成 CA 证书路径）"
        )
    raise exceptions.ParamsError(
        f"invalid {where}: {value!r}（应为布尔值，收到 {type(value).__name__}）"
    )


def is_support_multiprocessing() -> bool:
    """判断本机是否支持 multiprocessing（依赖信号量）。

    NOTICE:
        - 原实现 ``Queue()`` 创建即丢弃，每次调用都泄漏一个句柄/信号量；这里创建后
          显式 ``close()``/``join_thread()`` 释放，返回值语义不变；
        - 框架自身已不再用它决定 black 的调用方式（black 在进程池不可用时会自行回落到
          线程池，见 ``make.format_pytest_with_black``），保留该函数仅为兼容可能的外部引用。
    """
    queue = None
    try:
        queue = Queue()
        return True
    except (ImportError, OSError):
        # system that does not support semaphores
        # (dependency of multiprocessing), like Android termux
        return False
    finally:
        if queue is not None:
            try:
                queue.close()
                queue.join_thread()
            except Exception as ex:
                logger.debug(f"failed to release multiprocessing queue: {ex}")


def gen_cartesian_product(*args: List[Dict]) -> List[Dict]:
    """generate cartesian product for lists

    Args:
        args (list of list): lists to be generated with cartesian product

    Returns:
        list: cartesian product in list

    Examples:

        >>> arg1 = [{"a": 1}, {"a": 2}]
        >>> arg2 = [{"x": 111, "y": 112}, {"x": 121, "y": 122}]
        >>> args = [arg1, arg2]
        >>> gen_cartesian_product(*args)
        >>> # same as below
        >>> gen_cartesian_product(arg1, arg2)
            [
                {'a': 1, 'x': 111, 'y': 112},
                {'a': 1, 'x': 121, 'y': 122},
                {'a': 2, 'x': 111, 'y': 112},
                {'a': 2, 'x': 121, 'y': 122}
            ]

    """
    if not args:
        return []
    elif len(args) == 1:
        return args[0]

    product_list = []
    for product_item_tuple in itertools.product(*args):
        product_item_dict = {}
        for item in product_item_tuple:
            product_item_dict.update(item)

        product_list.append(product_item_dict)

    return product_list


LOGGER_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green>"
    + " | <level>{level}</level> | <level>{message}</level>"
)

# stdout 输出编码的显式开关（0918-5 / M10）：
#   - 未设置 → 非终端强制 UTF-8，终端保持平台默认（见 ensure_stdout_encoding）；
#   - 设为 "utf-8" 等编码名 → 无条件按它设置（终端也照做）；
#   - 设为 "locale"/"off"/"false"/"0"/空 → 完全不干预（回到修复前的行为）。
LOGGER_ENCODING_ENV = "INTERFACETESTER_LOG_ENCODING"
LOGGER_ENCODING_DISABLED = {"", "locale", "off", "false", "0"}


def _ensure_stream_encoding(stream) -> Optional[Text]:
    """`ensure_stdout_encoding()` / `ensure_output_encoding()` 的公共实现（见前者的 docstring）。"""
    current_encoding = (getattr(stream, "encoding", None) or "").lower()

    raw_override = os.environ.get(LOGGER_ENCODING_ENV)
    if raw_override is not None:
        value = raw_override.strip().lower()
        if value in LOGGER_ENCODING_DISABLED:
            return None  # ① 显式关掉
        target_encoding = value  # ② 显式指定（优先）
    else:
        if os.environ.get("PYTHONIOENCODING") or os.environ.get("PYTHONUTF8"):
            return None  # ③ 用户已用标准机制表态
        if _is_tty(stream):
            return None  # ⑤ 终端保持平台默认
        target_encoding = "utf-8"  # ④ 非终端强制 UTF-8

    if current_encoding.replace("_", "-") == target_encoding.replace("_", "-"):
        return target_encoding

    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        # stdout 被换成了没有 reconfigure 的对象（某些测试桩/包装流）→ 保持原样
        return None

    try:
        # errors="replace"：日志绝不能因为一个无法编码的字符把整个用例跑挂
        reconfigure(encoding=target_encoding, errors="replace")
    except Exception as ex:  # noqa: BLE001
        logger.debug(f"failed to set stream encoding to {target_encoding}: {ex}")
        return None

    return target_encoding


def ensure_stdout_encoding() -> Optional[Text]:
    """确保 **stdout** 以 UTF-8 输出；返回最终使用的编码名（未处理时返回 None）。

    NOTICE（0918-5 / M10）：Windows 上中文告警/报错在**管道、重定向、CI 日志**里是乱码。
    实测根因在 stdout 的编码，而不是 loguru：

    ============================================  ==========================  ==========================
    条件                                            ``sys.stdout.encoding``     实际写出的中文字节
    ============================================  ==========================  ==========================
    未设 ``PYTHONIOENCODING``（本机 locale=cp936）   ``'gbk'``                   ``b'\\xb2\\xbb\\xd6\\xa7...'``
    设了 ``PYTHONIOENCODING=utf-8``                  ``'utf-8'``                 ``b'\\xe4\\xb8\\x8d\\xe6\\x94\\xaf...'``
    ============================================  ==========================  ==========================

    任何按 UTF-8 解码的消费者（CI 日志、``tee``、日志聚合、多数现代终端）读到 GBK 字节就是乱码。
    loguru 的**文件** sink 本来就是 UTF-8（loguru 默认 ``encoding="utf-8"``），
    所以受影响的**只有 stdout 这一路**——它跟随 locale。

    处置原则（优先级从高到低）：
      1. ``INTERFACETESTER_LOG_ENCODING`` 显式关掉 → **不干预**（回到修复前的行为）；
      2. ``INTERFACETESTER_LOG_ENCODING`` 显式给了编码名 → **无条件照做**（终端也照做）——
         它比下面的「默认不干预」更具体，所以优先于 ``PYTHONIOENCODING``；
      3. 用户已设 ``PYTHONIOENCODING``/``PYTHONUTF8``（标准机制）→ 不干预，尊重用户选择；
      4. **非终端**（管道/文件/CI）→ 强制 UTF-8：这里没有「控制台编码」需要尊重；
      5. **终端** → 不动（保持平台默认），避免让本来正常的老式 cp936 控制台反而变乱码。
    """
    stream = sys.stdout
    return _ensure_stream_encoding(stream)


def ensure_output_encoding() -> Dict[Text, Optional[Text]]:
    """确保 **stdout 与 stderr 都**按同一口径处理；返回 `{流名: 最终编码名}`。

    NOTICE（0918-8 / L27）：M10 只处理了 stdout，于是**同一次 CLI 调用里**
    stdout=UTF-8 而 stderr=cp936——而框架的中文提示/报错有相当一部分是**写 stderr 的**
    （`cli.py` 的「未知子命令」提示、Python 自身与 pytest 的错误输出），
    在管道/CI 里读出来就是乱码，且与 stdout 不一致（同一个工具两种编码）。

    两个流用**同一套决策**（见 `ensure_stdout_encoding` 的 docstring）：
    显式开关 → 显式编码名 → 用户设了 `PYTHONIOENCODING` 就不干预 → 非终端强制 UTF-8 → 终端不动。
    """
    return {
        "stdout": _ensure_stream_encoding(sys.stdout),
        "stderr": _ensure_stream_encoding(sys.stderr),
    }


def _is_tty(stream) -> bool:
    """stream 是否是终端（取不到判断时按「不是终端」处理，即倾向于强制 UTF-8）。"""
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False

    try:
        return bool(isatty())
    except Exception:  # noqa: BLE001
        return False


def init_logger(level: str):
    level = level.upper()
    if level not in ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
        level = "INFO"  # default

    # M10 + L27：先把 stdout / stderr 的编码定下来，再挂 sink——
    # 否则中文日志/中文提示在管道/CI 里是乱码，而且两个流还会用不同的编码。
    ensure_output_encoding()

    # set log level to INFO
    logger.remove()
    logger.add(sys.stdout, format=LOGGER_FORMAT, level=level)
