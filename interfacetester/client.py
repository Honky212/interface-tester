import json
import time

import requests
import urllib3
from loguru import logger
from requests import Request, Response
from requests.exceptions import (
    InvalidHeader,
    InvalidJSONError,
    InvalidSchema,
    InvalidURL,
    MissingSchema,
    RequestException,
)

from interfacetester import exceptions
from interfacetester.models import RequestData, ResponseData
from interfacetester.models import SessionData, ReqRespData
from interfacetester.utils import (
    lower_dict_keys,
    mask_sensitive_request_data,
    mask_sensitive_url,
    omit_long_data,
    resolve_default_timeout,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# NOTICE（批次 C / M5）：`urllib3.disable_warnings(...)` 是**刻意**保留的，但要说明代价 ——
# 它把 `InsecureRequestWarning` 装进 `warnings.filters` 的 `ignore`，对**整个进程**生效
# （把框架当库用的宿主程序里，用户自己的 HTTPS 调用也不会再警告）。
# 之所以不能就这么算了：框架自己**默认**就是 `verify=False`（`TConfig.verify` 的默认值，
# 沿用上游取向），于是「证书没校验」这件事在**任何地方都收不到一个信号** —— 而本仓其它
# 地方的取向恰恰是「宁可响亮」。折中做法：不改默认行为（改了会动既有用例），
# 但把这件事**说一次**（`_warn_if_tls_verification_disabled`，每个进程一次、只对 https）。
_TLS_WARNING_EMITTED = False


def _warn_if_tls_verification_disabled(url, verify) -> None:
    """https 请求且 `verify` 明确为假时，**每个进程告警一次**（批次 C / M5）。

    NOTICE（为什么只告警一次、只看 https）：`verify=False` 是框架默认值，逐请求告警会把
    日志淹掉（用户反而看不见）；而 http 请求上这个参数本来就没有意义。一次提示足以让
    「证书没校验」不再是零信号，同时不改变任何行为。
    """
    global _TLS_WARNING_EMITTED
    if _TLS_WARNING_EMITTED:
        return
    # `None` 表示「没传」→ requests/session 自己决定（默认是**校验**）→ 不告警
    if verify is None or verify:
        return
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        return

    _TLS_WARNING_EMITTED = True
    logger.warning(
        "本次运行**不会校验 HTTPS 证书**（`verify=False`）——这是框架的**默认值**"
        "（沿用上游 HttpRunner 的取向），且 `InsecureRequestWarning` 已被全局关闭，"
        "所以证书错误（自签名 / 过期 / 中间人）**不会被发现**。\n"
        f"  触发请求: {mask_sensitive_url(url)}\n"
        "  要校验证书：在用例 `config` 里写 `verify: true`，或给该 step 写 "
        "`request: {verify: true}`（step 级优先）；Python API 用 `Config(...).verify(True)`。\n"
        "  只有**测试环境**（自签名证书）才应该保持关闭；测生产地址时请务必打开。\n"
        "  NOTICE: 本提示每个进程只出现一次，且不影响任何行为（仅提示）。"
    )


class ApiResponse(Response):
    def raise_for_status(self):
        if hasattr(self, "error") and self.error:
            raise self.error
        Response.raise_for_status(self)


def get_req_resp_record(resp_obj: Response) -> ReqRespData:
    """get request and response info from Response() object."""

    def log_print(req_or_resp, r_type):
        msg = f"\n================== {r_type} details ==================\n"
        for key, value in req_or_resp.model_dump().items():
            if isinstance(value, dict) or isinstance(value, list):
                value = json.dumps(value, indent=4, ensure_ascii=False)

            msg += "{:<8} : {}\n".format(key, value)
        logger.debug(msg)

    # record actual request info
    request_headers = dict(resp_obj.request.headers)
    # requests 未公开 cookies 的读取方式，只能读私有属性 _cookies；这里逐层降级，
    # 取不到时用 {} 兜底（例如错误兜底路径构造的 PreparedRequest 可能没有该属性）。
    request_cookies = {}
    request_cookies_jar = getattr(resp_obj.request, "_cookies", None)
    if request_cookies_jar is not None:
        request_cookies = request_cookies_jar.get_dict()

    request_body = resp_obj.request.body
    if request_body is not None:
        try:
            request_body = json.loads(request_body)
        except json.JSONDecodeError:
            # str: a=1&b=2
            pass
        except UnicodeDecodeError:
            # bytes/bytearray: request body in protobuf
            pass
        except TypeError:
            # neither str nor bytes/bytearray, e.g. <MultipartEncoder>
            pass

        request_content_type = lower_dict_keys(request_headers).get("content-type")
        if request_content_type and "multipart/form-data" in request_content_type:
            # upload file type
            request_body = "upload file stream (OMITTED)"

    # M4（0918-2）：请求侧凭据脱敏 —— 这里是 `ReqRespData` 的**唯一构造点**，
    # 因此同时覆盖两条泄漏链路：
    #   ① 下面 `log_print(request_data, "request")` 的 DEBUG 日志（→ .run.log / stdout）；
    #   ② 记录进 `SessionData.req_resps` 的请求详情（→ summary.json / HTML 报告 / Allure）。
    # 修复前这两条都是明文（实测：`Authorization` / `Cookie` / 体里的 `password` 全部原样入库）。
    # NOTICE: 脱敏的是**构造记录用的副本**——真正发出去的请求（`response.request`）不受影响，
    # 断言也仍然打向未脱敏的真实响应。
    request_data = RequestData(
        method=resp_obj.request.method,
        # NOTICE（0920 批次 2 / **N4**）：URL 必须走 `mask_sensitive_url`（它同时处理
        # **userinfo** 与查询串）。修复前这里用 `mask_sensitive_request_data`，
        # 而那个入口把字符串当**表单串**处理（`a=1&b=2`），于是
        # `https://alice:pass@host/x` 里的口令原样进了 summary / 报告 / Allure。
        url=mask_sensitive_url(resp_obj.request.url),
        headers=mask_sensitive_request_data(request_headers),
        cookies=mask_sensitive_request_data(request_cookies),
        body=mask_sensitive_request_data(request_body),
    )

    # log request details in debug mode
    log_print(request_data, "request")

    # record response info
    resp_headers = dict(resp_obj.headers)
    lower_resp_headers = lower_dict_keys(resp_headers)
    content_type = lower_resp_headers.get("content-type", "")

    if "image" in content_type:
        # response is image type, record bytes content only
        response_body = resp_obj.content
    else:
        try:
            # try to record json data
            response_body = resp_obj.json()
        except ValueError:
            # only record at most 512 text characters
            resp_text = resp_obj.text
            response_body = omit_long_data(resp_text)

    # NOTICE（0920 批次 2 / **N15**）：响应侧同样是**凭据载体**，此前"一个字都没脱敏"。
    # 修复前实测（`.tmp_audit/verify_aud3.py`）：同一个请求里请求侧的
    # `Authorization` 打印成 `******`，而响应侧的 `Set-Cookie: sessionid=CANARY…` /
    # `X-Auth-Token` / 体里的 `{"access_token": CANARY…}` **原样**进了 `.run.log`、
    # `summary.json`、HTML 报告与 Allure「response details」附件。
    #
    # 口径（与 `tests/sensitive_data_leak_test.py` 的边界声明对齐，**不是**"响应全糊"）：
    #   · 响应**头/cookie** 按 `is_sensitive_key` 判据脱敏 —— `Set-Cookie` / `sessionid`
    #     / `token` / `jwt` 这些名字本来就在 `SENSITIVE_KEYWORDS` 里，框架已经认定它们敏感；
    #   · 响应**体**按键名递归脱敏（结构不变、只把敏感键的值换成 `******`）——
    #     断言素材**不受影响**：下面 `ResponseObject(resp, …)` 用的是**原始 `resp`**，
    #     真正参与 extract/validate 的仍是未脱敏的真值；
    #   · 非敏感字段（`Server`/`Date`/普通业务字段）**逐字保留**，响应里"服务端到底返回了
    #     什么"这个排查信息不被抹掉。
    #
    # 记录/展示用的脱敏副本（真正参与断言的仍是 `resp_obj` 本身）
    display_headers = mask_sensitive_request_data(resp_headers)
    display_cookies = mask_sensitive_request_data(dict(resp_obj.cookies or {}))
    display_body = mask_sensitive_request_data(response_body)

    response_data = ResponseData(
        status_code=resp_obj.status_code,
        cookies=display_cookies,
        encoding=resp_obj.encoding,
        headers=display_headers,
        content_type=content_type,
        body=display_body,
    )

    # log response details in debug mode
    log_print(response_data, "response")

    req_resp_data = ReqRespData(request=request_data, response=response_data)
    return req_resp_data


def _get_connection_socket(resp_obj: Response):
    """尽力取出响应底层的 TCP socket。

    requests/urllib3 没有公开获取 client/server IP:Port 的 API，只能依赖私有属性
    ``raw._connection``；而该属性在连接释放、重定向、连接池回收等阶段取值并不稳定，
    因此这里逐层降级，取不到就返回 None，由调用方保留 AddressData 的 "N/A"/0 默认值。
    """
    raw = getattr(resp_obj, "raw", None)
    connection = getattr(raw, "_connection", None)
    return getattr(connection, "sock", None)


def _get_response_content_size(resp_obj: Response) -> int:
    """响应没有 Content-Length 时，按**已经物化**的响应体字节数统计（拿不到返回 0）。

    NOTICE（0918-8 / M29）：修复前这里直接访问 `resp_obj.content`，而 requests 的
    `stream=True` 语义正是「先不下载 body，访问 `.content` 才下载」——框架默认就开着
    `stream=True`（为了取 socket 地址），于是 chunked 响应会在这一行一直阻塞到流结束
    （实测：慢速 chunked 流的 `request()` 耗时 ≈ 整个流的时长）。
    现在只统计 requests **已经**缓存下来的内容（`_content`），绝不因为统计体积而触发下载。

    NOTICE: 为什么不按计划改用 `raw.length_remaining`：实测它在真实路径上**取不到有用值**——
    chunked 响应的 `length` 是 None（urllib3 返回 None 或 0），而只要传输层知道长度，
    响应头里就一定有 Content-Length（那条分支在前面就返回了）。与其留一段「看起来更聪明、
    实际从不生效」的分支，不如把口径写死成「已经读到的才算」。

    NOTICE（M29 的边界，如实说明）：`get_req_resp_record` 是**设计上**必须物化 body 的
    （报告与断言都要用），所以框架仍会在同一个 `request()` 调用里读完有界响应；
    本函数只保证「统计体积」这件事不再额外触发一次读取。
    真正的 SSE/无限长轮询需要「响应体不进报告」这个语义变更，见
    docs/缺陷修复日志0918-8.md 的 8-4 遗留项。
    """
    # requests 把已读内容缓存在 `_content`（未读时是 False，status_code==0 时是 None）
    cached_content = getattr(resp_obj, "_content", False)
    if cached_content is False or cached_content is None:
        return 0

    try:
        return len(cached_content)
    except TypeError:  # pragma: no cover - 非 bytes 的异常缓存值
        return 0


class HttpSession(requests.Session):
    """
    Class for performing HTTP requests and holding (session-) cookies between requests (in order
    to be able to log in and out of websites). Each request is logged so that InterfaceTester can
    display statistics.

    This is a slightly extended version of `python-request <http://python-requests.org>`_'s
    :py:class:`requests.Session` class and mostly this class works exactly the same.
    """

    def __init__(self):
        super(HttpSession, self).__init__()
        self.data = SessionData()

    def update_last_req_resp_record(self, resp_obj):
        """
        update request and response info from Response() object.

        NOTICE:
            框架内目前没有调用方，保留此方法是为了兼容上游 HttpRunner 的 API。
            这里先判空再 pop，避免 req_resps 为空时抛 IndexError。
        """
        if self.data.req_resps:
            self.data.req_resps.pop()
        self.data.req_resps.append(get_req_resp_record(resp_obj))

    def request(self, method, url, name=None, **kwargs):
        """
        Constructs and sends a :py:class:`requests.Request`.
        Returns :py:class:`requests.Response` object.

        :param method:
            method for the new :class:`Request` object.
        :param url:
            URL for the new :class:`Request` object.
        :param name: (optional)
            Placeholder, make compatible with Locust's HttpSession
        :param params: (optional)
            Dictionary or bytes to be sent in the query string for the :class:`Request`.
        :param data: (optional)
            Dictionary or bytes to send in the body of the :class:`Request`.
        :param headers: (optional)
            Dictionary of HTTP Headers to send with the :class:`Request`.
        :param cookies: (optional)
            Dict or CookieJar object to send with the :class:`Request`.
        :param files: (optional)
            Dictionary of ``'filename': file-like-objects`` for multipart encoding upload.
        :param auth: (optional)
            Auth tuple or callable to enable Basic/Digest/Custom HTTP Auth.
        :param timeout: (optional)
            How long to wait for the server to send data before giving up, as a float, or \
            a (`connect timeout, read timeout <user/advanced.html#timeouts>`_) tuple.
            :type timeout: float or tuple
        :param allow_redirects: (optional)
            Set to True by default.
        :type allow_redirects: bool
        :param proxies: (optional)
            Dictionary mapping protocol to the URL of the proxy.
        :param stream: (optional)
            whether to immediately download the response content. Defaults to ``False``.
        :param verify: (optional)
            if ``True``, the SSL cert will be verified. A CA_BUNDLE path can also be provided.
        :param cert: (optional)
            if String, path to ssl client cert file (.pem). If Tuple, ('cert', 'key') pair.
        """
        self.data = SessionData()

        # timeout default to 120 seconds（或环境变量 INTERFACETESTER_TIMEOUT 指定的值）
        kwargs.setdefault("timeout", resolve_default_timeout())

        # 批次 C / M5：https + verify 为假时提示一次「证书不会被校验」
        _warn_if_tls_verification_disabled(url, kwargs.get("verify", True))

        # default stream to True, in order to get client/server IP/Port;
        # 用 setdefault 而不是直接赋值，避免覆盖用户显式传入的 stream 值。
        kwargs.setdefault("stream", True)

        start_timestamp = time.time()
        response = self._send_request_safe_mode(method, url, **kwargs)
        response_time_ms = round((time.time() - start_timestamp) * 1000, 2)

        # NOTE:
        # 客户端/服务端 IP、端口只能从底层 socket 读取，而 requests/urllib3 未公开该能力，
        # 这里统一走 _get_connection_socket 逐层降级；取不到时保持 "N/A"/0 默认值，
        # 避免版本升级后静默丢字段而无从排查。
        sock = _get_connection_socket(response)
        if sock is None:
            logger.debug(
                "client/server address unavailable: raw socket is not accessible"
            )
        else:
            try:
                client_ip, client_port = sock.getsockname()
                self.data.address.client_ip = client_ip
                self.data.address.client_port = client_port
                logger.debug(f"client IP: {client_ip}, Port: {client_port}")
            except Exception as ex:
                logger.debug(f"failed to get client address: {ex}")

            try:
                server_ip, server_port = sock.getpeername()
                self.data.address.server_ip = server_ip
                self.data.address.server_port = server_port
                logger.debug(f"server IP: {server_ip}, Port: {server_port}")
            except Exception as ex:
                logger.debug(f"failed to get server address: {ex}")

        # record request and response histories, include 30X redirection
        response_list = response.history + [response]
        self.data.req_resps = [
            get_req_resp_record(resp_obj) for resp_obj in response_list
        ]

        # get length of the response content
        # NOTICE: chunked 响应（Transfer-Encoding: chunked）没有 Content-Length 头，
        # 修复前统一记 0，日志/报告里的 response_length 恒为 0，与真实响应体大小不符。
        # 这里退化为按实际响应体字节数统计，并在日志里标注为估算值，避免用户误以为
        # 该数值一定是压缩前的传输长度。
        # NOTICE（0918-8 / M29）：**必须在 `req_resps` 之后统计**。这一统计不再主动读取
        # body（见 `_get_response_content_size`），而框架生成报告/断言本来就要物化 body
        # （上面那两行），因此放在其后取值，既不会因为统计体积而多读一次，数值也与
        # 修复前完全一致；若放在前面，chunked 响应就会退化成 0。
        # NOTICE: response.headers 是 CaseInsensitiveDict，必须直接用它取值；
        # 先 dict() 再取会退化成大小写敏感查找，而多数服务端返回的是
        # "Content-Length"（大写），会误判成「无 Content-Length」走估算分支。
        raw_content_length = response.headers.get("content-length")
        try:
            content_size = int(raw_content_length)
            content_size_is_estimated = False
            content_size_is_unknown = False
        except (TypeError, ValueError):
            # 无 Content-Length（chunked）或该头部非法 → 退化为按已物化的响应体字节数统计
            content_size = _get_response_content_size(response)
            content_size_is_estimated = content_size > 0
            # 没拿到任何可用的体积信息（body 还没被物化）→ 明确标成「未知」，
            # 不能再让日志显示「response_length: 0 bytes」这种看起来像答案的假数据
            content_size_is_unknown = content_size == 0

        # record the consumed time
        # NOTICE: 原先写的是 `response.elapsed.microseconds / 1000.0`——`timedelta.microseconds`
        # 只是「秒以下的部分」，响应超过 1 秒时会把 2.5s 记成 500ms（P1-a 顺带修正）。
        self.data.stat.response_time_ms = response_time_ms
        self.data.stat.elapsed_ms = round(response.elapsed.total_seconds() * 1000, 2)
        self.data.stat.content_size = content_size

        try:
            response.raise_for_status()
        except RequestException as ex:
            logger.error(f"{str(ex)}")
        else:
            logger.info(
                f"status_code: {response.status_code}, "
                f"response_time(ms): {response_time_ms} ms, "
                f"response_length: {content_size} bytes"
                f"{' (estimated, no Content-Length)' if content_size_is_estimated else ''}"
                f"{' (unknown, body not read)' if content_size_is_unknown else ''}"
            )

        return response

    def _send_request_safe_mode(self, method, url, **kwargs):
        """
        Send a HTTP request, and catch any exception that might occur due to connection problems.
        Safe mode has been removed from requests 1.x.
        """
        try:
            return requests.Session.request(self, method, url, **kwargs)
        except (MissingSchema, InvalidSchema, InvalidURL):
            raise
        except (InvalidHeader, InvalidJSONError) as ex:
            # NOTICE（批次 7 / **M6**）：**构造阶段**的错误与网络错误必须分开。
            #
            # 修复前它们一起落进下面的 `except RequestException` 兜底，被降级成
            # `status_code = 0` 的**假响应** —— 而 `status_code: 0` 正是"连不上服务器"的形态，
            # 于是「你的请求**根本没构造出来**」被显示成「连不上」：
            #
            # ```text
            # headers: {X-Count: $count}   # $count 是数字 123
            #   -> ERROR | Header part (123) from ('X-Count', 123) must be of type str or bytes
            #   -> 用户看到的是 status_code == 0 的断言失败（与网络故障同形）
            #   -> **没有断言的 step 还会被判成功**（最坏的一种"假通过"）
            # ```
            #
            # 现在直接抛 `ParamsError`：把"哪个字段、什么类型、怎么改"说清楚，
            # 并保留原始异常在异常链里（`from ex`）。
            raise exceptions.ParamsError(
                "请求在**构造阶段**就失败了（请求一次都没发出去，"
                "所以这不是网络问题，也不会有 status_code）：\n"
                f"  {type(ex).__name__}: {ex}\n"
                "  请检查请求头 / 参数 / 请求体的**取值类型**：YAML 里 `${...}` 引用与变量"
                "取出来的值**一律是字符串**，数字/布尔要显式转换（例如 `${int($count)}`），"
                "否则 requests 会拒绝构造请求。\n"
                "  NOTICE: 修复前这类错误被吞成 `status_code: 0` 的假响应，"
                "与「连不上服务器」完全同形；没有断言的步骤还会被判成功。"
            ) from ex
        except RequestException as ex:
            resp = ApiResponse()
            resp.error = ex
            resp.status_code = 0  # with this status_code, content returns None
            # 兜底记录请求时尽量带上原始请求参数（headers/params/data/json/cookies），
            # 否则 req_resps 里记录的 request 是空壳，排查连接失败/超时时看不到请求内容。
            try:
                resp.request = Request(
                    method=method,
                    url=url,
                    headers=kwargs.get("headers"),
                    params=kwargs.get("params"),
                    data=kwargs.get("data"),
                    json=kwargs.get("json"),
                    cookies=kwargs.get("cookies"),
                ).prepare()
            except Exception as prepare_ex:
                logger.debug(f"failed to prepare fallback request: {prepare_ex}")
                resp.request = Request(method, url).prepare()
            return resp
