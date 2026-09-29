"""SOAP 风格 XML mock（离线，只为跑通示例与 CI 冒烟，不依赖外网）。

端点（全部接受 POST）：

| 路径 | 返回 | 用来演示什么 |
| --- | --- | --- |
| `/svc/query` | 200 + `<ns1:code>0000</ns1:code>` 等 | 正常业务响应 |
| `/svc/query-drift` | 200 + **同一份契约、前缀从 `ns1:` 漂移成 `abc:`** | 「字符串断言会假失败、结构化断言照过」 |
| `/svc/query-broken` | 200 + **不是合法 XML**（元素用 `abc:` 但声明还写 `ns1:`） | 「字符串断言照样绿、结构化断言当场报错」 |
| `/svc/query-badcode` | 200 + 业务码 `1002`（**违反 XSD 的 `pattern 0[0-9]{3}`**） | 「XSD 契约校验能拦住不合契约的业务码」（A2-2 对照演示） |
| `/svc/items` | 200 + 3 条 `<ns1:item id="N">` | `xpath_count` 个数断言、属性选择 |
| `/svc/fault` | **500** + `soap:Fault` | SOAP 的业务错误也是 HTTP 500（断言模板的坑） |
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SVCS_NS = "http://demo.example.com/svc"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

DEFAULT_PORT = 8907

OK = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{soap}">
  <soap:Body>
    <ns1:QueryResponse xmlns:ns1="{svc}">
      <ns1:code>0000</ns1:code>
      <ns1:message>成功</ns1:message>
      <ns1:amount>1234.50</ns1:amount>
    </ns1:QueryResponse>
  </soap:Body>
</soap:Envelope>
""".format(soap=SOAP_NS, svc=SVCS_NS)

# 同一个命名空间 URI，只换前缀：契约完全没变（真实环境里不同网元/网关的前缀常不一致）
# NOTICE: 这里刻意**手写**整份报文，而不是 `OK.replace("ns1:", "abc:")`——那样写只会改元素前缀、
# 不改 `xmlns` 声明，产出的是**非法 XML**（`unbound prefix`）。那个坑在 `/svc/query-broken` 里保留演示。
DRIFT = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{soap}">
  <soap:Body>
    <abc:QueryResponse xmlns:abc="{svc}">
      <abc:code>0000</abc:code>
      <abc:message>成功</abc:message>
      <abc:amount>1234.50</abc:amount>
    </abc:QueryResponse>
  </soap:Body>
</soap:Envelope>
""".format(soap=SOAP_NS, svc=SVCS_NS)

# 故意造一份**不是合法 XML** 的响应：元素用 abc: 前缀，声明却写着 ns1:
# （用字符串替换去改报文最容易踩的坑：文档已破，`contains` 这类断言照样绿）
BROKEN = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{soap}">
  <soap:Body>
    <abc:QueryResponse xmlns:ns1="{svc}">
      <abc:code>0000</abc:code>
      <abc:message>成功</abc:message>
    </abc:QueryResponse>
  </soap:Body>
</soap:Envelope>
""".format(soap=SOAP_NS, svc=SVCS_NS)

# 业务码不合契约（`0[0-9]{3}` 之外的值）：给 A2-2 的 XSD 校验做「失败可读」对照演示
BADCODE = OK.replace("<ns1:code>0000</ns1:code>", "<ns1:code>1002</ns1:code>")

ITEMS = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{soap}">
  <soap:Body>
    <ns1:ItemsResponse xmlns:ns1="{svc}">
      <ns1:item id="1"><ns1:name>甲</ns1:name></ns1:item>
      <ns1:item id="2"><ns1:name>乙</ns1:name></ns1:item>
      <ns1:item id="3"><ns1:name>丙</ns1:name></ns1:item>
    </ns1:ItemsResponse>
  </soap:Body>
</soap:Envelope>
""".format(soap=SOAP_NS, svc=SVCS_NS)

FAULT = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{soap}">
  <soap:Body>
    <soap:Fault>
      <faultcode>soap:Client</faultcode>
      <faultstring>Invalid parameter: userId</faultstring>
      <detail>
        <ns1:code xmlns:ns1="{svc}">SVC-1002</ns1:code>
      </detail>
    </soap:Fault>
  </soap:Body>
</soap:Envelope>
""".format(soap=SOAP_NS, svc=SVCS_NS)

ROUTES = {
    "/svc/query": (200, OK),
    "/svc/query-drift": (200, DRIFT),
    "/svc/query-broken": (200, BROKEN),
    "/svc/query-badcode": (200, BADCODE),
    "/svc/items": (200, ITEMS),
    "/svc/fault": (500, FAULT),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status, body):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _route(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)  # 请求体不重要，读掉避免连接卡住
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        status, body = ROUTES.get(path, (404, "<error>unknown path: {}</error>".format(path)))
        self._reply(status, body)

    do_POST = _route
    do_GET = _route

    def log_message(self, *args):
        pass


def serve(port: int = DEFAULT_PORT):
    """起一个后台线程的 mock，返回 ``(httpd, base_url)``。port=0 → 系统分配空闲端口。

    NOTICE: 必须用 `ThreadingHTTPServer` + `daemon_threads`（与 `examples/data_management`
    的 mock 同源）。用单线程的 `HTTPServer` 时，HTTP/1.1 keep-alive 会让服务线程**阻塞在
    「读下一个请求」**上 → 会话结束时的 `httpd.shutdown()` 会**永久挂住**（实测踩过）。
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    base_url = f"http://127.0.0.1:{httpd.server_port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, base_url


if __name__ == "__main__":
    import os

    port = int(os.environ.get("SOAP_MOCK_PORT") or DEFAULT_PORT)
    server, url = serve(port)
    print(f"soap mock on {url}", flush=True)
    print(json.dumps(sorted(ROUTES), ensure_ascii=False), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
