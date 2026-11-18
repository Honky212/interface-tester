"""40 分钟 demo 专用的本地 mock 服务（离线自包含，只用标准库）。

为什么需要它：现场演示**不能依赖外网**（httpbin.org 之类）；这个 mock 同时承担三件事：
  1. `POST /api/login`      → 签发 token（给"登录 → 带 token 查询"链路演示用）
  2. `GET  /api/user/info`  → 校验 `Authorization: Bearer <token>`（给 extract 演示用）
  3. `GET  /get`            → 仿 httpbin 的形状（让仓库自带的 quickstart「假通过」对照用例能离线跑）

用法（保持运行，另开一个终端跑用例）：
    python selling-this-product/demo/mock_server.py
    # 换了端口的话，记得同步改 login.yml 里的 base_url
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 18080

TOKEN = "demo-token-1234567890"
EXPECTED_USER = "admin"
EXPECTED_PASSWORD = "123456"

USER_INFO = {
    "code": 0,
    "message": "ok",
    "data": {
        "username": "admin",
        "role": "admin",
        "email": "admin@example.com",
    },
}


class Handler(BaseHTTPRequestHandler):
    server_version = "DemoMock/1.0"

    # ---------- 工具 ----------
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 让控制台干净一点
        pass

    # ---------- 路由 ----------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/login":
            self._send_json(404, {"code": 404, "message": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            data = {}

        if data.get("username") == EXPECTED_USER and data.get("password") == EXPECTED_PASSWORD:
            self._send_json(200, {"code": 0, "message": "ok", "data": {"token": TOKEN}})
        else:
            self._send_json(200, {"code": 1001, "message": "用户名或密码错误"})

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/user/info":
            if self.headers.get("Authorization", "") == f"Bearer {TOKEN}":
                self._send_json(200, USER_INFO)
            else:
                # ★故意返回业务错误码（HTTP 仍是 200）——用来演示
                #   「HTTP 200 也可能是业务失败」，以及为什么断言要断 body.code
                self._send_json(401, {"code": 401, "message": "未授权：token 无效"})
            return

        if path == "/get":
            # 仿 httpbin，供 examples/quickstart 的对照用例离线复用
            self._send_json(
                200,
                {
                    "args": {},
                    "headers": {k: v for k, v in self.headers.items()},
                    "url": f"http://{HOST}:{PORT}{self.path}",
                },
            )
            return

        self._send_json(404, {"code": 404, "message": "not found"})


if __name__ == "__main__":
    print(f"demo mock server running at http://{HOST}:{PORT}")
    print("  POST /api/login       (admin / 123456)")
    print("  GET  /api/user/info   (需要 Authorization: Bearer demo-token-1234567890)")
    print("  GET  /get             (仿 httpbin，给 quickstart 对照用例用)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
