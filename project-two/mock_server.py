"""project-two 的本地 mock 服务：模拟「登录发 token → 校验 token」两个接口。

作用：让 login_flow.yml 这个两步链路用例可以**离线、确定性**地跑通，不依赖外网。

用法：
  1) 先在一个终端里启动它（保持运行）：
         python mock_server.py
  2) 另开一个终端跑用例：
         hrun login_flow.yml
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = "demo-token-1234567890"
EXPECTED_USER = "admin"
EXPECTED_PASSWORD = "123456"


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/login":
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                data = {}
            if data.get("username") == EXPECTED_USER and data.get("password") == EXPECTED_PASSWORD:
                # 登录成功：签发 token
                self._send_json(200, {"code": 0, "message": "ok", "data": {"token": TOKEN}})
            else:
                self._send_json(200, {"code": 1001, "message": "用户名或密码错误"})
        else:
            self._send_json(404, {"code": 404, "message": "not found"})

    def do_GET(self):
        if self.path == "/api/user/info":
            auth = self.headers.get("Authorization", "")
            if auth == f"Bearer {TOKEN}":
                self._send_json(
                    200,
                    {
                        "code": 0,
                        "data": {
                            "username": "admin",
                            "role": "admin",
                            "email": "admin@example.com",
                        },
                    },
                )
            else:
                # token 不对 / 没带 → 401
                self._send_json(401, {"code": 401, "message": "未授权：token 无效"})
        else:
            self._send_json(404, {"code": 404, "message": "not found"})

    def log_message(self, *args):  # 精简控制台日志
        pass


if __name__ == "__main__":
    print("mock server running at http://127.0.0.1:8000")
    HTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
