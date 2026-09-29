import os
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import quote

import mock_server
import requests
from loguru import logger

from interfacetester import client
from interfacetester.client import ApiResponse, HttpSession
from interfacetester.converters.har_adapter import warn_dynamic_values
from interfacetester.exceptions import ParamsError
from interfacetester.models import TRequest
from interfacetester.utils import (
    DEFAULT_TIMEOUT_SECONDS,
    HTTP_BIN_URL,
    TIMEOUT_ENV,
    ExtendJSONEncoder,
    ensure_timeout_value,
    resolve_default_timeout,
)

# 本地 mock 的 https 地址（由 tests/mock_server.py 在 tests/conftest.py 导入期启动）。
# NOTICE: 这些用例原先访问公网 postman-echo.com，离线或网络波动时会失败；
# 现在改打本机自签名 mock，断言「http → mock 的 http 端口、https → mock 的 https 端口」的地址解析结果。
# NOTICE: 端口断言必须取 mock_server 的常量而不是写死 80/443——mock 端口可由环境变量改
# （非 root CI 绑不上特权端口，见 docs/ci/README.md）。
HTTPS_BIN_URL = mock_server.HTTPS_BIN_URL


def _redirect_to(base_url: str, target: str) -> str:
    """构造 mock 的跳转地址：/redirect-to?url=<target>。"""
    return f"{base_url}/redirect-to?url={quote(target, safe='')}"


class TestHttpSession(unittest.TestCase):
    """地址解析用例：请求全部打向本地 mock 服务（见 tests/mock_server.py）。"""

    def setUp(self):
        self.session = HttpSession()
        # 本地 https mock 使用自签名证书（保证离线可跑），因此关闭证书校验
        self.session.verify = False

    def test_request_http(self):
        self.session.request("get", f"{HTTP_BIN_URL}/get")
        address = self.session.data.address
        self.assertGreater(len(address.server_ip), 0)
        self.assertEqual(address.server_port, mock_server.HTTP_BIN_PORT)
        self.assertGreater(len(address.client_ip), 0)
        self.assertGreater(address.client_port, 10000)

    @unittest.skipUnless(mock_server.https_available(), mock_server.https_skip_reason())
    def test_request_https(self):
        self.session.request("get", f"{HTTPS_BIN_URL}/get")
        address = self.session.data.address
        self.assertGreater(len(address.server_ip), 0)
        self.assertEqual(address.server_port, mock_server.HTTPS_BIN_PORT)
        self.assertGreater(len(address.client_ip), 0)
        self.assertGreater(address.client_port, 10000)

    @unittest.skipUnless(mock_server.https_available(), mock_server.https_skip_reason())
    def test_request_http_allow_redirects(self):
        # http mock 302 到 https mock：校验跟随重定向后，address 反映的是最终那次请求
        self.session.request(
            "get",
            _redirect_to(HTTP_BIN_URL, f"{HTTPS_BIN_URL}/get"),
            allow_redirects=True,
        )
        address = self.session.data.address
        self.assertNotEqual(address.server_ip, "N/A")
        self.assertEqual(address.server_port, mock_server.HTTPS_BIN_PORT)
        self.assertNotEqual(address.server_ip, "N/A")
        self.assertGreater(address.client_port, 10000)

    @unittest.skipUnless(mock_server.https_available(), mock_server.https_skip_reason())
    def test_request_https_allow_redirects(self):
        self.session.request(
            "get",
            _redirect_to(HTTPS_BIN_URL, f"{HTTPS_BIN_URL}/get"),
            allow_redirects=True,
        )
        address = self.session.data.address
        self.assertNotEqual(address.server_ip, "N/A")
        self.assertEqual(address.server_port, mock_server.HTTPS_BIN_PORT)
        self.assertNotEqual(address.server_ip, "N/A")
        self.assertGreater(address.client_port, 10000)

    def test_request_http_not_allow_redirects(self):
        self.session.request(
            "get",
            _redirect_to(HTTP_BIN_URL, f"{HTTPS_BIN_URL}/get"),
            allow_redirects=False,
        )
        address = self.session.data.address
        self.assertEqual(address.server_ip, "N/A")
        self.assertEqual(address.server_port, 0)
        self.assertEqual(address.client_ip, "N/A")
        self.assertEqual(address.client_port, 0)

    @unittest.skipUnless(mock_server.https_available(), mock_server.https_skip_reason())
    def test_request_https_not_allow_redirects(self):
        self.session.request(
            "get",
            _redirect_to(HTTPS_BIN_URL, f"{HTTPS_BIN_URL}/get"),
            allow_redirects=False,
        )
        address = self.session.data.address
        self.assertEqual(address.server_ip, "N/A")
        self.assertEqual(address.server_port, 0)
        self.assertEqual(address.client_ip, "N/A")
        self.assertEqual(address.client_port, 0)


class _MockChunkedResponseHandler(BaseHTTPRequestHandler):
    """本地 mock 服务：/chunked 返回 chunked 响应（无 Content-Length），/plain 返回带长度头的响应，
    /slow 延迟 1.1s 再响应（用于验证 `stat.elapsed_ms` 的整秒部分）。

    NOTICE: 不依赖外部网络（httpbin/postman-echo 在离线环境下不可用），
    用于稳定验证 content_size / elapsed_ms 统计。
    """

    protocol_version = "HTTP/1.1"
    response_body = b"chunked response body"
    slow_delay_seconds = 1.1
    # 慢速 chunked 流：5 块 × 0.2s ≈ 1.0s（M29 用它证明「统计体积不会把流读完」）
    slow_stream_chunks = 5
    slow_stream_chunk_delay = 0.2

    def _send_body(self):
        self.wfile.write(self.response_body)

    def _send_slow_chunked(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for _index in range(self.slow_stream_chunks):
            time.sleep(self.slow_stream_chunk_delay)
            chunk = self.response_body[:3]
            self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/slow":
            time.sleep(self.slow_delay_seconds)
        if self.path == "/slow-stream":
            self._send_slow_chunked()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        if self.path == "/chunked":
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            # 分块写入：3 字节一块
            for index in range(0, len(self.response_body), 3):
                chunk = self.response_body[index : index + 3]
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(self.response_body)))
            self.end_headers()
            self._send_body()

    def log_message(self, *args):
        # 关闭 http.server 默认写 stderr 的访问日志
        pass


class TestHttpSessionContentSize(unittest.TestCase):
    """响应长度统计：chunked 响应没有 Content-Length，必须按实际响应体字节数统计。"""

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockChunkedResponseHandler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.session = HttpSession()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)

    def test_request_with_chunked_response(self):
        response = self.session.request("get", f"{self.base_url}/chunked")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.session.data.stat.content_size,
            len(_MockChunkedResponseHandler.response_body),
        )

    def test_request_with_content_length_header(self):
        self.session.request("get", f"{self.base_url}/plain")
        self.assertEqual(
            self.session.data.stat.content_size,
            len(_MockChunkedResponseHandler.response_body),
        )

    def test_request_with_content_length_header_is_not_estimated(self):
        """有 Content-Length 时不能走「估算」分支。

        NOTICE: 回归用例 —— response.headers 是大小写不敏感的 CaseInsensitiveDict，
        服务端返回的 "Content-Length"（大写）必须能命中；否则日志会把所有响应都标成
        "estimated, no Content-Length"。
        """
        messages = []
        sink_id = logger.add(messages.append, level="INFO", format="{message}")
        try:
            self.session.request("get", f"{self.base_url}/plain")
        finally:
            logger.remove(sink_id)

        logs = "\n".join(str(message) for message in messages)
        self.assertIn("response_length:", logs)
        self.assertNotIn("estimated, no Content-Length", logs)

    def test_elapsed_ms_keeps_whole_seconds(self):
        """`stat.elapsed_ms` 必须包含整秒部分（P1-a 顺带修正）。

        NOTICE: 回归用例 —— 原先写的是 `response.elapsed.microseconds / 1000`，
        而 `timedelta.microseconds` 只是「秒以下的部分」：1.1s 的响应会被记成 ~100ms，
        于是报告里所有超过 1 秒的接口耗时都是错的。
        """
        response = self.session.request("get", f"{self.base_url}/slow")

        self.assertGreaterEqual(response.elapsed.total_seconds(), 1.0)
        self.assertEqual(
            self.session.data.stat.elapsed_ms,
            round(response.elapsed.total_seconds() * 1000, 2),
        )
        self.assertGreaterEqual(self.session.data.stat.elapsed_ms, 1000)

    def test_chunked_response_is_marked_estimated(self):
        """chunked 的体积是「估算值」，日志必须标出来（口径不能看着像精确值）。"""
        messages = []
        sink_id = logger.add(messages.append, level="INFO", format="{message}")
        try:
            self.session.request("get", f"{self.base_url}/chunked")
        finally:
            logger.remove(sink_id)

        logs = "\n".join(str(message) for message in messages)
        self.assertIn("estimated, no Content-Length", logs)
        self.assertNotIn("unknown, body not read", logs)


class TestResponseContentSizeWithoutReadingBody(unittest.TestCase):
    """M29：统计体积**不能**触发 body 下载（那会把 requests 的 `stream=True` 推翻）。

    NOTICE: 修复前 `_get_response_content_size` 直接读 `resp_obj.content`，
    而框架默认 `stream=True`（为了取 socket 地址）—— 于是「懒加载」变「立刻读完」，
    chunked / SSE / 长轮询响应会在这里一直阻塞到流结束。
    """

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockChunkedResponseHandler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)

    def test_unread_chunked_body_is_not_downloaded(self):
        response = requests.get(f"{self.base_url}/chunked", stream=True)
        # 前置条件：stream=True 时 body 还没被下载
        self.assertIs(response._content, False)

        size = client._get_response_content_size(response)

        self.assertEqual(size, 0)
        self.assertIs(
            response._content, False, "统计体积把响应体读下来了（stream=True 被推翻）"
        )

    def test_already_read_body_reports_exact_size(self):
        """body 已经被物化时（框架的 req_resps 会物化）必须给出精确值，口径不变。"""
        response = requests.get(f"{self.base_url}/chunked", stream=True)
        body = response.content  # 主动读一次（模拟 get_req_resp_record 的物化）

        size = client._get_response_content_size(response)

        self.assertEqual(size, len(body))
        self.assertEqual(size, len(_MockChunkedResponseHandler.response_body))

    def test_slow_chunked_stream_is_not_consumed_by_size_estimation(self):
        """慢速 chunked 流：统计体积必须**立刻返回**，而不是等到流结束。

        NOTICE: 这里用「同一个流读全量确实很慢」做自证条件——如果响应其实不慢，
        本用例的耗时断言就是空洞的。
        """
        response = requests.get(f"{self.base_url}/slow-stream", stream=True)

        started = time.time()
        size = client._get_response_content_size(response)
        estimate_elapsed = time.time() - started

        self.assertEqual(size, 0)
        self.assertLess(
            estimate_elapsed,
            0.5,
            f"统计体积耗时 {estimate_elapsed:.2f}s，说明它把整个流读完了",
        )

        read_started = time.time()
        body = response.content
        read_elapsed = time.time() - read_started

        # 自证条件：这个流本身确实很慢（否则上面的「很快」毫无意义）
        self.assertGreater(read_elapsed, 0.5)
        self.assertEqual(len(body), 3 * _MockChunkedResponseHandler.slow_stream_chunks)

    def test_error_response_reports_zero(self):
        """请求失败（status_code=0、content 为 None）时记 0，与修复前一致。"""
        response = ApiResponse()
        response.status_code = 0

        self.assertEqual(client._get_response_content_size(response), 0)

    def test_unknown_size_is_labelled_in_log(self):
        """拿不到体积时必须标成「unknown, body not read」，而不是伪装成 0 字节。

        NOTICE: 正常跑用例时 `get_req_resp_record` 会物化 body，因此这一分支只在
        「record 没有物化 body」时出现——这里 patch 掉 record 构造来覆盖该分支，
        并借此钉住「不许把未知值说成 0」的口径。
        """
        messages = []
        sink_id = logger.add(messages.append, level="INFO", format="{message}")
        try:
            with mock.patch.object(
                client, "get_req_resp_record", lambda resp_obj: None
            ):
                response = HttpSession().request("get", f"{self.base_url}/chunked")
        finally:
            logger.remove(sink_id)

        self.assertEqual(response.status_code, 200)
        logs = "\n".join(str(message) for message in messages)
        self.assertIn("response_length: 0 bytes", logs)
        self.assertIn("unknown, body not read", logs)


class TestTlsVerificationWarning(unittest.TestCase):
    """批次 C / **M5**：默认关闭证书校验这件事**必须可见**（每个进程一次）。

    ## 修复前的现场（`probe_tls.py`，拦截 `requests.Session.request` 的 kwargs）

    ```text
    import 框架前 InsecureRequestWarning 过滤器: []
    import 框架后 InsecureRequestWarning 过滤器: [('ignore', None, InsecureRequestWarning, None, 0), ...]
    框架传给 requests 的 verify = False        ← 用户一个字段都没配
    requests 自己的默认 verify = True
    ```

    两条叠加起来就是「**证书没校验，而且在任何地方都收不到一个信号**」：
    `TConfig.verify` 默认 `False`（沿用上游取向）+ `client.py` 在 import 期把
    `InsecureRequestWarning` 装进全局 `warnings.filters`。
    文档侧也没说默认值（`使用教程` 写「`verify: false  # 可选`」、
    `能力清单` 只写「证书校验开关 ✅」），读者按 requests 的常识会以为默认是校验的。

    NOTICE（本批的做法）：**不改默认行为**（改了会动既有用例），只把这件事说一次 ——
    判据是「https + verify 明确为假」，且每个进程只提示一次（逐请求告警会把日志淹掉）。
    """

    def setUp(self):
        self._original = client._TLS_WARNING_EMITTED
        client._TLS_WARNING_EMITTED = False

    def tearDown(self):
        client._TLS_WARNING_EMITTED = self._original

    @staticmethod
    def _warnings_for(url, verify):
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            client._warn_if_tls_verification_disabled(url, verify)
        finally:
            logger.remove(sink_id)
        return messages

    def test_https_with_verify_false_warns_once(self):
        messages = self._warnings_for("https://api.example.test/login", False)

        self.assertEqual(len(messages), 1, messages)
        self.assertIn("不会校验 HTTPS 证书", messages[0])
        self.assertIn("verify: true", messages[0])  # 给出改法
        # 每个进程只提示一次
        self.assertEqual(self._warnings_for("https://other.test/x", False), [])

    def test_https_with_verify_true_does_not_warn(self):
        self.assertEqual(self._warnings_for("https://api.example.test/", True), [])

    def test_verify_none_does_not_warn(self):
        """`verify=None` 表示「没传」→ requests/session 自己决定（默认**校验**）→ 不告警。"""
        self.assertEqual(self._warnings_for("https://api.example.test/", None), [])

    def test_http_with_verify_false_does_not_warn(self):
        """http 上 `verify` 本来就没有意义 → 不告警（避免刷噪声）。"""
        self.assertEqual(self._warnings_for("http://api.example.test/", False), [])

    def test_http_session_calls_the_hook_with_the_effective_verify(self):
        """集成：`HttpSession.request` 必须把**实际生效**的 verify 交给这个提示函数。"""
        with mock.patch.object(
            client, "_warn_if_tls_verification_disabled"
        ) as hook, mock.patch.object(
            HttpSession,
            "_send_request_safe_mode",
            side_effect=RuntimeError("stop here"),
        ):
            with self.assertRaises(RuntimeError):
                HttpSession().request("get", "https://api.example.test/x", verify=False)

        hook.assert_called_once_with("https://api.example.test/x", False)


class TestTimeoutResolution(unittest.TestCase):
    """默认请求超时的解析：环境变量 > 内置默认值（120s，维持不变）。

    用 spy 包住 ``requests.Session.request``（真实请求照打本地 mock），
    这样既能断言实际传下去的 timeout，又不需要另造 HTTP 服务。
    """

    def _capture_request_timeout(self, session, url):
        """真实发一次请求，返回实际传给 requests 的 timeout。"""
        original_request = requests.Session.request
        captured = {}

        def spy(session_self, method, req_url, **kwargs):
            captured.update(kwargs)
            return original_request(session_self, method, req_url, **kwargs)

        with mock.patch.object(requests.Session, "request", spy):
            session.request("get", url)

        return captured.get("timeout")

    def test_default_timeout_is_120(self):
        """未配置任何超时时，默认值维持 120s（本次不改变既有行为）。"""
        timeout = self._capture_request_timeout(HttpSession(), f"{HTTP_BIN_URL}/get")
        self.assertEqual(timeout, float(DEFAULT_TIMEOUT_SECONDS))
        self.assertEqual(timeout, 120.0)

    def test_default_timeout_can_be_overridden_by_env(self):
        """环境变量可整体覆盖默认超时（供 CI 统一调短，不必改用例）。"""
        with mock.patch.dict(os.environ, {TIMEOUT_ENV: "2"}):
            timeout = self._capture_request_timeout(
                HttpSession(), f"{HTTP_BIN_URL}/get"
            )
        self.assertEqual(timeout, 2.0)

    def test_resolve_default_timeout_fallback(self):
        """未设置 → 默认值；非法/非正值 → 告警后回落默认值（不抛异常）。"""
        with mock.patch.dict(os.environ):
            os.environ.pop(TIMEOUT_ENV, None)
            self.assertEqual(
                resolve_default_timeout(), float(DEFAULT_TIMEOUT_SECONDS)
            )

        for invalid_value in ["abc", "0", "-1", ""]:
            with self.subTest(invalid_value=invalid_value):
                with mock.patch.dict(os.environ, {TIMEOUT_ENV: invalid_value}):
                    self.assertEqual(
                        resolve_default_timeout(), float(DEFAULT_TIMEOUT_SECONDS)
                    )

    def test_ensure_timeout_value(self):
        """超时值规范化：支持 ${ENV()} 解析出的数字字符串，非法值明确报错。"""
        self.assertEqual(ensure_timeout_value("5"), 5.0)
        self.assertEqual(ensure_timeout_value(0), 0.0)
        self.assertEqual(ensure_timeout_value(2.5), 2.5)

        for invalid_value in ["abc", None, True, [1]]:
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises(ParamsError):
                    ensure_timeout_value(invalid_value)


class TestRequestProxies(unittest.TestCase):
    """请求级代理是真的生效（修复前 YAML 里写 proxies 会被静默丢弃）。"""

    def _dead_port(self) -> int:
        """取一个「刚被释放、几乎不可能有人在听」的本机端口。"""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_proxy_is_actually_used(self):
        session = HttpSession()
        # 不跟随环境里的 HTTP_PROXY/NO_PROXY，保证结论只由本用例的 proxies 参数决定
        session.trust_env = False

        # 对照组：直连本地 mock 应当成功
        direct_response = session.request("get", f"{HTTP_BIN_URL}/get")
        self.assertEqual(direct_response.status_code, 200)

        # 实验组：把代理指到一个没人监听的端口 → 请求必然失败（safe-mode 兜底成 0）。
        # 若 proxies 被静默忽略，这里会直连成功（200），用例即失败。
        proxied_response = session.request(
            "get",
            f"{HTTP_BIN_URL}/get",
            proxies={"http": f"http://127.0.0.1:{self._dead_port()}"},
        )
        self.assertEqual(proxied_response.status_code, 0)


class _MockScalarResponseHandler(BaseHTTPRequestHandler):
    """只回**裸 JSON 标量**的本地服务（端口由测试取 0 号临时端口，不碰共享 mock）。"""

    # path -> (body bytes, expected python value)
    SCALARS = {
        "/num": b"123",
        "/bool": b"true",
        "/false": b"false",
        "/float": b"1.5",
        "/null": b"null",
        "/str": b'"hello"',
        "/obj": b'{"n": 123}',
        "/arr": b"[1, 2, 3]",
    }

    def do_GET(self):  # noqa: N802
        payload = self.SCALARS.get(self.path, b"{}")
        self._send(payload)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        # 把收到的原始请求体原样回显（便于断言"服务端确实收到了什么"）
        self._send(b'{"echo": "' + body + b'"}')

    def _send(self, payload: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class TestScalarRecordBody(unittest.TestCase):
    r"""批次 3 / H3：裸 JSON 标量（数字 / 布尔）的**记录体**不得让整条用例炸掉。

    ## 修复前的现场（实测，真 CLI 打自建 mini server）

    ```text
    服务端返回 123  + Content-Type: application/json  ->  exit 1
      pydantic_core.ValidationError: 4 validation errors for ResponseData
    服务端返回 true                                   ->  exit 1（同上）
    服务端返回 "hello"（裸字符串）                     ->  exit 0
    服务端返回 {"n": 123}（对象，对照组）              ->  exit 0
    请求侧同源：YAML 里 `data: "123"`                  ->  ValidationError ... for RequestData
    ```

    ## 根因

    `RequestData` / `ResponseData` 是**记录用**模型（只进 summary.json / HTML 报告 / Allure，
    不参与断言、也不参与真实收发），而两个 `body` 字段写的是
    `Union[Text, bytes, List, Dict, None]` —— **不含 int/float/bool**。
    于是 `client.py` 里 `resp_obj.json()` 拿到 `123` 时，pydantic 在**记录这一步**抛错。

    危害不止"记不下来"：**请求早就发出去了**（服务端可能已经改数据），
    框架却把整条用例的结果丢弃，抛出的错还与用户 YAML 毫无关系
    （`hrun` exit 1、报告里没有响应记录、后续 extract / 断言全部不执行）。

    ## 修法

    `models.py` 里收口成**一个**记录体口径 `RecordBody`，两个字段共用：

    ```python
    RecordBody = Union[bool, int, float, Text, bytes, List, Dict, None]
    ```

    `bool` **必须在这个联合类型里**（`bool` 是 `int` 的子类，而 pydantic v2 的 int 校验器
    会把 `True` 变成 `1` —— 实测漏掉 `bool` 时 `body=True` 记为 `1`，**不报错**，只是静默失真）。
    至于它在联合类型里的**位置**：只要在，前后都一样（smart union 按精确类型挑），
    所以下面 `test_boolean_is_not_recorded_as_a_number` 钉的是"`bool` 得在"，而不是"`bool` 得排第一"。
    """

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockScalarResponseHandler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.session = HttpSession()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)

    def _recorded_response_body(self, path: str):
        """请求一次，返回**记录进 `req_resps` 的**响应体（不是真实响应的 body）。"""
        self.session.request("get", f"{self.base_url}{path}")
        return self.session.data.req_resps[0].response.body

    def test_scalar_response_bodies_are_recorded_without_error(self):
        """123 / true / false / 1.5 / null / "hello" 六种标量都必须能记下来。"""
        expected = {
            "/num": 123,
            "/bool": True,
            "/false": False,
            "/float": 1.5,
            "/null": None,
            "/str": "hello",
        }
        for path, want in expected.items():
            with self.subTest(path=path):
                got = self._recorded_response_body(path)
                self.assertEqual(
                    got,
                    want,
                    f"{path} 的记录体是 {got!r}，期望 {want!r}"
                    "（修复前这里抛 ValidationError，整条用例的结果会被丢弃）",
                )
                self.assertIs(type(got), type(want), "记录体丢失了原始类型")

    def test_boolean_is_not_recorded_as_a_number(self):
        """`true` 必须记成**布尔**，不能因为 `bool` 是 `int` 子类就被记成 `1`。

        这条钉的是「`bool` 得在 `RecordBody` 里」：漏掉它不会报错，只会**静默**把布尔
        记录成数字，报告与 summary.json 一起失真（注入验证：把 `bool` 从联合类型里删掉，
        本用例与上面那条一起变红；只把 `bool` 挪到 `int` 后面则**不变红**——位置不影响）。
        """
        got = self._recorded_response_body("/bool")

        self.assertIs(got, True)
        self.assertNotEqual(
            str(got), "1", "布尔响应被记成了数字 1（RecordBody 里 bool/int 的顺序反了）"
        )

    def test_object_and_array_bodies_are_unchanged(self):
        """反向护栏：dict / list 体的记录口径与修复前完全一致。"""
        self.assertEqual(self._recorded_response_body("/obj"), {"n": 123})
        self.assertEqual(self._recorded_response_body("/arr"), [1, 2, 3])

    def test_scalar_request_body_is_recorded(self):
        """请求侧同源：数字样**文本**体（`data: "123"`）经 `json.loads` 变 int 后同样要能记。"""
        self.session.request(
            "post", f"{self.base_url}/echo", data="123", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(self.session.data.req_resps[0].request.body, 123)

        # 对照组：不是 JSON 的文本体保持原样（`json.loads` 失败 → str），口径不变
        self.session.request(
            "post", f"{self.base_url}/echo", data="a=1&b=2"
        )
        self.assertEqual(self.session.data.req_resps[-1].request.body, "a=1&b=2")

    def test_record_is_json_serializable_like_the_summary(self):
        """记录体要能走 summary.json 那条序列化路径（`ExtendJSONEncoder`），否则报告里仍是空的。

        NOTICE: `HttpSession.request` 每次都会**替换** `data.req_resps`（它记的是含 30X
        跳转链的本次请求），所以这里必须**逐个标量各发一次请求再序列化**——
        把 6 个请求连发再统一序列化，只剩最后一条记录（第一版就是这么写错的）。
        """
        expected = {
            "/num": 123,
            "/bool": True,
            "/float": 1.5,
            "/null": None,
            "/obj": {"n": 123},
            "/arr": [1, 2, 3],
        }
        for path, want in expected.items():
            with self.subTest(path=path):
                self._recorded_response_body(path)
                payload = json.dumps(
                    self.session.data.model_dump(), cls=ExtendJSONEncoder
                )
                restored = json.loads(payload)["req_resps"][0]["response"]["body"]
                self.assertEqual(restored, want)
                self.assertIs(type(restored), type(want))


class TestBatch0919_30ScalarJsonRequestBody(unittest.TestCase):
    r"""批次 8 收尾 / **M14**：YAML 的 `json:` 必须能写**裸标量**（RFC 8259 的顶层标量）。

    ## 现场（实测，修复前，`.tmp_report/probe_m14.py`）

    ```text
    TRequest(method="POST", url="…", json=123)   → pydantic_core.ValidationError: 3 validation errors
    （true / 1.5 / 显式 null 同样被拒；"text" / {} / [] 正常）
    ```

    三个候选类型（`dict` / `list` / `str`）也推不出"为什么不支持标量"。
    与 H3 的关系：H3 已经把**记录层**（`RequestData`/`ResponseData.body`）的裸标量收口了，
    请求**构造层**却仍然表达不了 —— 同一个关注点上上下不一致。

    ## 修法（按用户拍板：**放宽**，与 H3 的 `RecordBody` 同口径）

    ```python
    req_json: Union[Dict, List, Text, bool, int, float, None] = Field(None, alias="json")
    ```

    `bool` 必须在（漏掉它时 pydantic 的 int 校验器会把 `True` 静默变成 `1`）；
    **不含 `bytes`**：`json=` 最终走 `json.dumps`，真要发二进制请用 `data:`。
    （注意：pydantic 的 lax 模式本来就会把 `bytes` 解成 `str`，这是既有行为，本批没动。）
    顺带把 `ir.warn_dynamic_values` 对**标量**的 `AttributeError` 收掉 —— M14 放宽了接受面，
    这个边界不该再留（实测 `probe_m14.py` ②）。
    """

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MockScalarResponseHandler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.tmp_dir = os.path.join(
            os.getcwd(), "logs", f"tmp_m14_{uuid.uuid4().hex[:8]}"
        )
        os.makedirs(self.tmp_dir)
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)

    def _write_case(self, json_literal: str, expected_echo: str) -> str:
        path = os.path.join(self.tmp_dir, "case.yml")
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(
                "config:\n"
                "    name: m14\n"
                f"    base_url: {self.base_url}\n"
                "teststeps:\n"
                "    -\n"
                "        name: post body\n"
                "        request:\n"
                "            method: POST\n"
                "            url: /echo\n"
                f"            json: {json_literal}\n"
                "        validate:\n"
                f"            - eq: [\"body.echo\", '{expected_echo}']\n"
            )
        return path

    def _run_cli(self, yml_path: str):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "interfacetester",
                "run",
                os.path.basename(yml_path),
            ],
            cwd=self.tmp_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=180,
        )

    def test_scalar_json_bodies_are_accepted_by_the_request_model(self):
        """**核心**：`json` 的四种裸标量都必须能构造（修复前全是 ValidationError）。"""
        for value in (123, True, 1.5, None):
            with self.subTest(value=value):
                request = TRequest(method="POST", url="http://x/y", json=value)

                self.assertEqual(request.req_json, value)
                self.assertIs(type(request.req_json), type(value), "标量类型被改了")

    def test_boolean_body_is_not_coerced_into_a_number(self):
        """`true` 必须保持布尔（`bool` 在联合类型里；漏掉它会被静默记成 1）。"""
        request = TRequest(method="POST", url="http://x/y", json=True)

        self.assertIs(request.req_json, True)

    def test_int_scalar_body_is_really_sent(self):
        """**端到端**：真 CLI 跑 `json: 123`，服务端收到的原始报文必须是 `123`。"""
        yml_path = self._write_case("123", "123")

        proc = self._run_cli(yml_path)

        self.assertEqual(
            proc.returncode,
            0,
            f"exit={proc.returncode}\nstdout=\n{proc.stdout}\nstderr=\n{proc.stderr}",
        )
        self.assertIn("1 passed", proc.stdout)

    def test_bool_scalar_body_is_sent_as_json_true(self):
        """`json: true` 出去的报文是 JSON 的 `true`（不是 Python 的 `True`）。"""
        yml_path = self._write_case("true", "true")

        proc = self._run_cli(yml_path)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("1 passed", proc.stdout)

    def test_object_and_list_bodies_are_unchanged(self):
        """反向护栏：对象/数组体的既有行为不变（生成物与运行时都不受影响）。"""
        for value in ({"a": 1}, [1, 2]):
            with self.subTest(value=value):
                request = TRequest(method="POST", url="http://x/y", json=value)

                self.assertEqual(request.req_json, value)

    def test_warn_dynamic_values_tolerates_scalar_bodies(self):
        """M14 顺带：`warn_dynamic_values` 收到标量不得抛 `AttributeError`。"""
        for value in (123, True, 1.5, None, "text"):
            with self.subTest(value=value):
                self.assertEqual(warn_dynamic_values(value, "请求体"), [])

        # 非空跑自检：dict 体仍然照常提示（证明这条不是"恒空")
        self.assertTrue(warn_dynamic_values({"updated_at": "1750000000"}, "请求体"))
