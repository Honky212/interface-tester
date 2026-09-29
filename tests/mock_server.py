"""测试用本地 mock 服务（httpbin 兼容子集），让测试不依赖外网。

背景
----
``tests/`` 里的 client_test / response_test 依赖两个外部服务：

- ``interfacetester.utils.HTTP_BIN_URL``（默认 ``http://127.0.0.1:80``，需要本机先
  ``docker run -p 80:80 kennethreitz/httpbin``）；
- ``https://postman-echo.com``：公网服务，离线或网络波动时用例直接失败
  （相同断言本机网络不通时表现为 「ConnectionError → address 保持 N/A」）。

这里用标准库 ``http.server`` 起两个本地服务，由 ``tests/conftest.py`` 在导入阶段启动、
进程退出时关闭：

- HTTP  mock：``127.0.0.1:80``（地址与 ``HTTP_BIN_URL`` 一致，用例无需改 URL）；
- HTTPS mock：``127.0.0.1:443``（自签名证书，用例以 ``verify=False`` 访问）。

**端口可配置**（P1-c）：两个端口统一取自 ``interfacetester.utils``，可用环境变量改
（``INTERFACETESTER_HTTP_BIN_PORT`` / ``INTERFACETESTER_HTTPS_BIN_PORT``，默认仍是 80/443）——
80/443 是特权端口，非 root 的 CI/容器绑不上，把端口调大即可，用例里的 ``HTTP_BIN_URL`` 会一起变。

实现范围是 httpbin 的常用接口子集，未覆盖的路径返回 404 + JSON 提示（不静默返回错误结果）。
端口被其它进程占用、或本机取不到可用的自签名证书时，不会中断测试：前者假定已有
httpbin 在运行，后者让依赖 https 的用例 skip 并给出原因。

**单独启动（给 CI 的 examples 冒烟用，见 docs/ci/README.md）**：

    python tests/mock_server.py          # 前台常驻；Ctrl-C 退出

等价于 ``python -c "from mock_server import start_servers; start_servers()" 后挂住进程``，
但不必关心 sys.path。pytest 跑 ``tests/`` 时不需要它（``tests/conftest.py`` 会自己启动）。
"""

import atexit
import gzip
import json
import os
import shutil
import socket
import ssl
import struct
import sys
import sysconfig
import tempfile
import threading
import time
import uuid
import zlib
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple, Type
from urllib.parse import parse_qs, urlparse

from interfacetester import utils

HTTP_BIN_HOST = utils.HTTP_BIN_HOST
# NOTICE: 端口不是写死的 80/443——统一由 interfacetester.utils 从环境变量解析
# （INTERFACETESTER_HTTP_BIN_PORT / INTERFACETESTER_HTTPS_BIN_PORT，默认 80/443），
# 这样非 root 的 CI 能把端口调大，而用例里的 HTTP_BIN_URL 会自动跟着变。
HTTP_BIN_PORT = utils.HTTP_BIN_PORT
HTTPS_BIN_PORT = utils.HTTPS_BIN_PORT

HTTP_BIN_URL = utils.HTTP_BIN_URL
HTTPS_BIN_URL = utils.HTTPS_BIN_URL

# 保护的阈值：避免用例里写错数字（如 /delay/1000）把测试线程挂死
MAX_DELAY_SECONDS = 10
MAX_BYTES_SIZE = 100 * 1024
MAX_STREAM_LINES = 50

# 自签名证书来自标准库自带的测试数据（见 load_server_certificate），
# 只用于本机 mock，用例以 verify=False 访问，不参与任何真实安全校验。
_TEMP_CERT_DIR: Optional[str] = None


def _collapse_values(pairs: Dict[str, List[str]]) -> Dict[str, Any]:
    """把 parse_qs 的 {key: [values]} 折叠成 httpbin 风格的 {key: value}。"""
    return {
        key: values[0] if len(values) == 1 else values for key, values in pairs.items()
    }


def _build_png(size: int = 1) -> bytes:
    """用标准库生成一张 size*size 的黑色 PNG（避免把二进制样例文件放进仓库）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    # 8bit 真彩色（RGB），每个像素前有一个 filter 字节 0
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


PNG_1X1 = _build_png()

# `/xml/soap` 的报文（A2-1）：给内核 XML 算子做端到端冒烟用，**不依赖外网**。
# 契约：code/message 各一个、item 三条（带 id 属性）；前缀用 ns1:，与 examples/soap 的契约一致。
SOAP_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <ns1:QueryResponse xmlns:ns1="http://demo.example.com/svc">
      <ns1:code>0000</ns1:code>
      <ns1:message>成功</ns1:message>
      <ns1:item id="1"/><ns1:item id="2"/><ns1:item id="3"/>
    </ns1:QueryResponse>
  </soap:Body>
</soap:Envelope>
"""


class HttpBinHandler(BaseHTTPRequestHandler):
    """httpbin 兼容子集。

    已实现：``/``、``/get``、``/post``、``/put``、``/patch``、``/delete``、
    ``/anything[/...]``、``/headers``、``/cookies``、``/user-agent``、``/ip``、
    ``/uuid``、``/gzip``、``/status/<code>``、``/redirect-to``、``/delay/<n>``、
    ``/bytes/<n>``、``/stream/<n>``、``/basic-auth/<user>/<pass>``、``/bearer``、
    ``/image/{png,svg,jpeg,webp}``、``/spec.json``、``/json``、``/html``、``/xml``、
    ``/xml/soap``（A2-1 的 XPath 冒烟用）、``/robots.txt``、``/deny``。
    """

    protocol_version = "HTTP/1.1"
    server_version = "InterfaceTesterMockHttpBin"
    scheme = "http"

    # ------------------------------------------------------------------ 入口
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def log_message(self, format, *args):  # noqa: A002 - 与基类签名保持一致
        # 默认实现会把每条访问日志写到 stderr，测试输出会很吵；
        # 需要排查 mock 到底收到什么请求时设置 IT_MOCK_VERBOSE=1。
        if os.environ.get("IT_MOCK_VERBOSE") == "1":
            sys.stderr.write(f"[mock-httpbin] {format % args}\n")

    def _dispatch(self, method: str) -> None:
        # body 必须先读完：HTTP/1.1 长连接下残留字节会串到下一个请求
        self._request_body = self._read_request_body()
        parsed = urlparse(self.path)
        segments = [segment for segment in parsed.path.split("/") if segment]
        query = parse_qs(parsed.query)

        if not segments:
            return self._send_echo(method)

        head = segments[0]
        if head in ("get", "post", "put", "patch", "delete", "anything"):
            return self._send_echo(method)
        if head == "headers":
            return self._send_json({"headers": self._headers_dict()})
        if head == "cookies":
            if len(segments) >= 2 and segments[1] in ("set", "delete"):
                return self._send_cookie_change(segments, query)
            return self._send_json({"cookies": self._cookies_dict()})
        if head == "user-agent":
            return self._send_json({"user-agent": self.headers.get("User-Agent", "")})
        if head == "ip":
            return self._send_json({"origin": self.client_address[0]})
        if head == "uuid":
            return self._send_json({"uuid": str(uuid.uuid4())})
        if head == "gzip":
            return self._send_json(self._echo_payload(method), gzipped=True)
        if head == "redirect-to":
            return self._send_redirect(query)
        if head == "status":
            return self._send_status(segments)
        if head == "delay":
            return self._send_delay(segments, method)
        if head == "bytes":
            return self._send_bytes(segments)
        if head == "stream":
            return self._send_stream(segments, method)
        if head == "basic-auth":
            return self._send_basic_auth(segments)
        if head == "bearer":
            return self._send_bearer()
        if head == "image":
            return self._send_image(segments)
        if head == "spec.json":
            # NOTICE: 顶层键与真实 httpbin 的 Swagger 2.0 文档一致（9 个）——
            # `examples/httpbin/basic.yml` 最后一步就是 `len_eq: [body, 9]`，
            # 早期这里只回 3 个键，跑 examples 冒烟时会假失败（见 docs/ci/README.md）。
            return self._send_json(
                {
                    "swagger": "2.0",
                    "info": {"title": "mock httpbin", "version": "1.0.0"},
                    "host": f"{HTTP_BIN_HOST}:{HTTP_BIN_PORT}",
                    "basePath": "/",
                    "schemes": ["http"],
                    "consumes": ["application/json"],
                    "produces": ["application/json"],
                    "paths": {
                        "/get": {"get": {"responses": {"200": {"description": "OK"}}}},
                        "/post": {"post": {"responses": {"200": {"description": "OK"}}}},
                    },
                    "definitions": {},
                }
            )
        if head == "json":
            return self._send_json(
                {
                    "slideshow": {
                        "author": "Yours Truly",
                        "date": "date of publication",
                        "slides": [{"title": "Wake up to WonderWidgets!"}],
                        "title": "Sample Slide Show",
                    }
                }
            )
        if head == "html":
            return self._send_text(
                "<html><body><h1>mock httpbin</h1></body></html>",
                "text/html; charset=utf-8",
            )
        if head == "xml":
            # `/xml/soap`（A2-1）：带命名空间的 SOAP 报文，供 XPath 算子的端到端用例使用
            if len(segments) >= 2 and segments[1] == "soap":
                return self._send_text(SOAP_SAMPLE, "text/xml; charset=utf-8")
            return self._send_text(
                '<?xml version="1.0" encoding="us-ascii"?><slideshow/>',
                "application/xml",
            )
        if head == "robots.txt":
            return self._send_text(
                "User-agent: *\nDisallow: /deny\n", "text/plain; charset=utf-8"
            )
        if head == "deny":
            return self._send_text(
                "YOU SHOULDN'T BE HERE\n", "text/plain; charset=utf-8"
            )

        return self._send_json(
            {
                "error": "mock httpbin 未实现该接口",
                "method": method,
                "path": self.path,
                "hint": "在 tests/mock_server.py 的 HttpBinHandler._dispatch 里补充路由",
            },
            status=404,
        )

    # -------------------------------------------------------------- 请求解析
    def _read_request_body(self) -> bytes:
        try:
            content_length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if content_length <= 0:
            return b""
        return self.rfile.read(content_length)

    def _headers_dict(self) -> Dict[str, Any]:
        return {key: value for key, value in self.headers.items()}

    def _cookies_dict(self) -> Dict[str, str]:
        cookies: Dict[str, str] = {}
        for item in (self.headers.get("Cookie") or "").split(";"):
            if "=" in item:
                key, value = item.split("=", 1)
                cookies[key.strip()] = value.strip()
        return cookies

    def _json_body(self) -> Any:
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "json" not in content_type or not self._request_body:
            return None
        try:
            return json.loads(self._request_body.decode("utf-8"))
        except ValueError:
            return None

    def _form_fields(self) -> Dict[str, Any]:
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "application/x-www-form-urlencoded" not in content_type:
            return {}
        body = self._request_body.decode("utf-8", errors="replace")
        return _collapse_values(parse_qs(body))

    def _echo_payload(self, method: str) -> Dict[str, Any]:
        """httpbin 的 /get、/anything 等接口返回体。"""
        parsed = urlparse(self.path)
        return {
            "args": _collapse_values(parse_qs(parsed.query)),
            "data": self._request_body.decode("utf-8", errors="replace"),
            "files": {},
            "form": self._form_fields(),
            "headers": self._headers_dict(),
            "json": self._json_body(),
            "method": method,
            "origin": self.client_address[0],
            "url": f"{self.scheme}://{self.headers.get('Host', '')}{self.path}",
        }

    # -------------------------------------------------------------- 响应输出
    def _write_chunks(self, body: bytes, chunk_size: int = 64) -> None:
        for index in range(0, len(body), chunk_size):
            chunk = body[index : index + chunk_size]
            self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _send_body(
        self,
        body: bytes,
        status: int = 200,
        headers: Optional[Dict[str, str]] = None,
        chunked: bool = False,
        raw_headers: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        # NOTICE: `Set-Cookie` 需要多个同名头，dict 装不下 → 用 raw_headers 传元组列表。
        for key, value in raw_headers or []:
            self.send_header(key, value)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._write_chunks(body)
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(
        self,
        payload: Any,
        status: int = 200,
        gzipped: bool = False,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        final_headers = {"Content-Type": "application/json"}
        if gzipped:
            body = gzip.compress(body)
            final_headers["Content-Encoding"] = "gzip"
        final_headers.update(headers or {})
        self._send_body(body, status=status, headers=final_headers)

    def _send_text(self, text: str, content_type: str, status: int = 200) -> None:
        self._send_body(
            text.encode("utf-8"), status=status, headers={"Content-Type": content_type}
        )

    # ---------------------------------------------------------------- 各路由
    def _send_echo(self, method: str) -> None:
        self._send_json(self._echo_payload(method))

    def _send_redirect(self, query: Dict[str, List[str]]) -> None:
        target = (query.get("url") or [""])[0]
        if not target:
            return self._send_json(
                {"error": "缺少 url 查询参数，无法重定向"}, status=400
            )
        try:
            status_code = int((query.get("status_code") or ["302"])[0])
        except ValueError:
            status_code = 302
        self._send_body(b"", status=status_code, headers={"Location": target})

    def _send_cookie_change(
        self, segments: List[str], query: Dict[str, List[str]]
    ) -> None:
        """httpbin 的 ``/cookies/set[/name/value]`` 与 ``/cookies/delete``。

        语义（与 httpbin 一致）：按路径段或查询参数改 cookie，然后 **302 跳回 ``/cookies``**；
        客户端（requests 默认跟随重定向）最终看到的是 ``/cookies`` 的回显，
        所以 ``examples/httpbin/basic.yml`` 的「set cookie → extract cookie」两步能连贯通过。
        """
        changing = segments[1] == "set"
        raw_headers: List[Tuple[str, str]] = []
        if len(segments) >= 4:
            pairs = {segments[2]: segments[3]}
        else:
            pairs = {key: values[0] for key, values in query.items()}

        if not pairs:
            return self._send_json(
                {"error": f"缺少 cookie 名/值（用法：/cookies/{segments[1]}?name=value）"},
                status=400,
            )

        for name, value in pairs.items():
            if changing:
                raw_headers.append(("Set-Cookie", f"{name}={value}; Path=/"))
            else:
                raw_headers.append(("Set-Cookie", f"{name}=; Path=/; Max-Age=0"))

        # NOTICE: 用 302 + Location 而不是直接回显 cookies——保持与 httpbin 一致，
        # 否则「先 set 再读」这类用例会因为客户端没机会带上新 cookie 而失败。
        self._send_body(
            b"",
            status=302,
            headers={"Location": "/cookies"},
            raw_headers=raw_headers,
        )

    def _send_status(self, segments: List[str]) -> None:
        try:
            status_code = int(segments[1])
        except (IndexError, ValueError):
            return self._send_json(
                {"error": "用法：/status/<code>，code 为整数"}, status=400
            )
        if not 100 <= status_code <= 599:
            return self._send_json(
                {"error": "status code 必须在 100~599 之间"}, status=400
            )
        # 204/304 等状态码按规范不能带响应体，这里统一返回空体
        self._send_body(b"", status=status_code)

    def _send_delay(self, segments: List[str], method: str) -> None:
        try:
            seconds = float(segments[1])
        except (IndexError, ValueError):
            return self._send_json(
                {"error": "用法：/delay/<seconds>，seconds 为数字"}, status=400
            )
        time.sleep(max(0.0, min(seconds, MAX_DELAY_SECONDS)))
        self._send_echo(method)

    def _send_bytes(self, segments: List[str]) -> None:
        try:
            size = int(segments[1])
        except (IndexError, ValueError):
            return self._send_json({"error": "用法：/bytes/<n>"}, status=400)
        size = max(0, min(size, MAX_BYTES_SIZE))
        self._send_body(
            os.urandom(size), headers={"Content-Type": "application/octet-stream"}
        )

    def _send_stream(self, segments: List[str], method: str) -> None:
        try:
            lines = int(segments[1])
        except (IndexError, ValueError):
            return self._send_json({"error": "用法：/stream/<n>"}, status=400)
        lines = max(1, min(lines, MAX_STREAM_LINES))
        origin = self.client_address[0]
        url = f"{self.scheme}://{self.headers.get('Host', '')}{self.path}"
        body = b"".join(
            (json.dumps({"id": index, "origin": origin, "url": url}) + "\n").encode(
                "utf-8"
            )
            for index in range(lines)
        )
        # 故意不带 Content-Length：用于覆盖 chunked 响应的解析/长度统计
        self._send_body(
            body, headers={"Content-Type": "application/json"}, chunked=True
        )

    def _send_basic_auth(self, segments: List[str]) -> None:
        if len(segments) < 3:
            return self._send_json(
                {"error": "用法：/basic-auth/<user>/<passwd>"}, status=400
            )
        user, password = segments[1], segments[2]
        expected = "Basic " + b64encode(f"{user}:{password}".encode("utf-8")).decode(
            "ascii"
        )
        if self.headers.get("Authorization") != expected:
            return self._send_body(
                b"",
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="Fake Realm"'},
            )
        self._send_json({"authenticated": True, "user": user})

    def _send_bearer(self) -> None:
        authorization = self.headers.get("Authorization") or ""
        if not authorization.startswith("Bearer "):
            return self._send_body(
                b"",
                status=401,
                headers={"WWW-Authenticate": 'Bearer realm="Fake Realm"'},
            )
        self._send_json({"authenticated": True, "token": authorization[7:]})

    def _send_image(self, segments: List[str]) -> None:
        image_type = segments[1] if len(segments) > 1 else ""
        if image_type == "png":
            return self._send_body(PNG_1X1, headers={"Content-Type": "image/png"})
        if image_type == "svg":
            return self._send_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
                "image/svg+xml",
            )
        if image_type in ("jpeg", "webp"):
            # NOTICE: 标准库无法生成 jpeg/webp 二进制，这里只保证「200 + 正确的
            # Content-Type」；examples/httpbin/load_image.yml 也只断言 status_code。
            # 需要真实图片字节请用 /image/png。
            return self._send_body(b"", headers={"Content-Type": f"image/{image_type}"})
        return self._send_json(
            {"error": "用法：/image/{png,svg,jpeg,webp}"}, status=404
        )


class HttpsBinHandler(HttpBinHandler):
    """行为与 HttpBinHandler 一致，只是回显的 url 使用 https。"""

    scheme = "https"


# ---------------------------------------------------------------------- 服务管理
class MockHttpBinServer:
    """单个 mock 服务的生命周期（后台线程 + 可选 TLS）。"""

    def __init__(
        self,
        host: str,
        port: int,
        handler: Type[BaseHTTPRequestHandler],
        url_scheme: str,
    ):
        self.host = host
        self.port = port
        self.handler = handler
        self.url_scheme = url_scheme
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.skip_reason = ""

    @property
    def url(self) -> str:
        return f"{self.url_scheme}://{self.host}:{self.port}"

    def start(self, ssl_context: Optional[ssl.SSLContext] = None) -> bool:
        if not _is_port_available(self.host, self.port):
            self.skip_reason = f"{self.url} 已被其它进程占用，未启动本地 mock"
            return False

        try:
            httpd = ThreadingHTTPServer((self.host, self.port), self.handler)
        except OSError as ex:  # pragma: no cover - 端口竞态等异常环境
            self.skip_reason = f"启动 {self.url} 失败：{ex}"
            return False

        if ssl_context is not None:
            httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True)

        self.httpd = httpd
        self.thread = threading.Thread(
            target=httpd.serve_forever, daemon=True, name=f"mock-httpbin-{self.port}"
        )
        self.thread.start()
        return True

    def stop(self) -> None:
        if self.httpd is None:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.httpd = None
        self.thread = None


def _is_port_available(host: str, port: int) -> bool:
    """探测端口是否空闲。

    NOTICE: 这里故意不带 SO_REUSEADDR —— http.server 默认开启该选项，在 Windows 上
    即使端口已被占用也能 bind 成功，会静默顶掉别人（例如开发机上正在跑的 httpbin）。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _iter_stdlib_certdata_dir() -> Any:
    """标准库自带测试证书（test/certdata）的候选目录。"""
    bases = [
        sys.base_prefix,
        sys.prefix,
        sysconfig.get_path("stdlib") or "",
        os.path.dirname(os.__file__),
    ]
    for base in bases:
        if not base:
            continue
        yield os.path.join(base, "Lib", "test", "certdata")  # Windows 布局
        yield os.path.join(base, "test", "certdata")  # POSIX 布局
        yield os.path.join(base, "lib", "test", "certdata")  # 部分发行版布局


def load_server_certificate() -> Optional[Tuple[str, str]]:
    """返回 (certfile, keyfile)，取不到返回 None（此时 https mock 不启动）。

    优先用 trustme 现场签发自签名证书（若已安装），否则退回 CPython 自带的
    ``test/certdata/ssl_cert.pem`` + ``ssl_key.pem``；两者都只用于本机 mock，
    用例以 ``verify=False`` 访问，不参与任何真实安全校验。
    """
    global _TEMP_CERT_DIR
    try:
        import trustme  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        ca = trustme.CA()
        server_cert = ca.issue_cert("127.0.0.1", "localhost")
        _TEMP_CERT_DIR = tempfile.mkdtemp(prefix="it_mock_cert_")
        cert_file = os.path.join(_TEMP_CERT_DIR, "server.crt.pem")
        key_file = os.path.join(_TEMP_CERT_DIR, "server.key.pem")
        server_cert.cert_chain_pems[0].write_to_path(cert_file)
        server_cert.private_key_pem.write_to_path(key_file)
        return cert_file, key_file

    for certdata_dir in _iter_stdlib_certdata_dir():
        cert_file = os.path.join(certdata_dir, "ssl_cert.pem")
        key_file = os.path.join(certdata_dir, "ssl_key.pem")
        if os.path.exists(cert_file) and os.path.exists(key_file):
            return cert_file, key_file
    return None


_http_server = MockHttpBinServer(HTTP_BIN_HOST, HTTP_BIN_PORT, HttpBinHandler, "http")
_https_server = MockHttpBinServer(
    HTTP_BIN_HOST, HTTPS_BIN_PORT, HttpsBinHandler, "https"
)
_started = False
_notes: List[str] = []


def start_servers() -> List[str]:
    """启动 http/https mock（幂等），返回启动说明（含跳过原因）。

    NOTICE: 端口被占用、取不到证书等情况下不会抛异常，只把原因记进 notes，
    由调用方（tests/conftest.py）决定是否让相关用例 skip。
    """
    global _started
    if _started:
        return list(_notes)
    _started = True

    if _http_server.start():
        _notes.append(f"httpbin mock 已启动：{HTTP_BIN_URL}")
    else:
        # 端口被占用时假定本机已有 httpbin 在跑（等价于修复前的用法，不中断测试）
        _notes.append(f"注意：{_http_server.skip_reason}")

    certificate = load_server_certificate()
    if certificate is None:
        _https_server.skip_reason = "本机取不到可用的自签名证书（trustme 未安装，且标准库 test/certdata 不存在）"
    else:
        cert_file, key_file = certificate
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(cert_file, key_file)
        except Exception as ex:  # pragma: no cover - 证书损坏等异常环境
            _https_server.skip_reason = f"加载自签名证书失败：{ex}"
        else:
            if _https_server.start(ssl_context=context):
                _notes.append(
                    f"https mock 已启动：{HTTPS_BIN_URL}（自签名证书：{cert_file}）"
                )
            else:
                _notes.append(f"注意：{_https_server.skip_reason}")

    if not https_available():
        _notes.append(f"依赖 https mock 的用例将跳过：{https_skip_reason()}")

    atexit.register(stop_servers)
    return list(_notes)


def stop_servers() -> None:
    """关闭 mock 服务并清理临时证书（进程退出时自动调用）。"""
    global _started
    _http_server.stop()
    _https_server.stop()
    if _TEMP_CERT_DIR:
        shutil.rmtree(_TEMP_CERT_DIR, ignore_errors=True)
    _started = False


def http_mock_started() -> bool:
    """80 端口是否由本地 mock 提供（False 表示假定已有其它 httpbin 服务）。"""
    return _http_server.httpd is not None


def https_available() -> bool:
    """https mock 是否可用（不可用时依赖它的用例应 skip）。"""
    return _https_server.httpd is not None


def https_skip_reason() -> str:
    return _https_server.skip_reason or "https mock 未启动"


def startup_notes() -> List[str]:
    """启动过程中的说明信息（供 conftest 打印，便于排查环境问题）。"""
    return list(_notes)


def main() -> int:
    """前台常驻启动 mock（供 CI 在跑 ``examples/`` 之前后台拉起）。

    NOTICE: 用「等 KeyboardInterrupt」而不是 ``threading.Event().wait()``——后者在
    Windows 上收不到 Ctrl-C，进程只能被强杀；端口绑定失败时也**不退出**（与 pytest
    场景一致：假定本机已有 httpbin），由使用者看 notes 判断。
    """
    for note in start_servers():
        print(f"[mock-httpbin] {note}", flush=True)
    print("[mock-httpbin] 常驻中，Ctrl-C 退出", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("[mock-httpbin] 收到 Ctrl-C，正在关闭", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
