"""示例工程用的「数据服务」：一个**有状态**的本地 mock（只用标准库）。

为什么要有它
------------
P2-a 要交付的是「测试数据管理约定」（造数 / 隔离 / 回收 / 环境矩阵）。这套约定必须能被**离线复现**，
否则它就只是一纸文档。真实项目里这个「有状态的数据源」是数据库/配置中心；这里用 60 行的
内存服务扮演同一个角色，让示例工程在无 MySQL、无外网的环境下也能完整演示：

- 会话级准备：起服务 + 登录拿 token（T1）
- 用例级隔离：每条数据带「运行前缀 + 用例前缀」（T3）
- 用例级回收：按前缀删除自己造的数据（T2）
- 危险操作闸门：全库清空需要显式开关（T2，生产环境强制关闭）

接口（与真实项目保持一致的最小集）
----------------------------------
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | ``/login`` | ``{"username","password"}`` → ``{"token"}``；不需要鉴权 |
| POST | ``/records`` | 造数；需要 ``Authorization: Bearer <token>``；返回 ``{"id","name",...}`` |
| GET | ``/records`` | 查列表；支持 ``?prefix=`` 按名字前缀过滤；返回 ``{"records": [...], "count": N}`` |
| GET | ``/records/<id>`` | 查单条；不存在返回 404 |
| DELETE | ``/records?prefix=<p>`` | **按前缀**删除（安全，常用）；缺 ``prefix`` 视为全库清空 |
| DELETE | ``/records`` | **全库清空**：必须带 ``X-Allow-Dangerous: on``，否则 403 |
| GET | ``/health`` | 就绪探针 |

NOTICE: 这个服务**只用于示例与本地演示**，没有并发写保护之外的任何安全措施，不要用于真实环境。
"""

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

DEFAULT_USERNAME = "demo"
DEFAULT_PASSWORD = "demo"
DANGEROUS_HEADER = "X-Allow-Dangerous"
DANGEROUS_VALUE = "on"


class DataStore:
    """内存「数据表」：id → 记录。"""

    def __init__(self) -> None:
        self._records: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, name: str, payload: Any = None) -> Dict[str, Any]:
        record = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "payload": payload,
            "created_at": round(time.time(), 3),
        }
        with self._lock:
            self._records[record["id"]] = record
        return record

    def get(self, record_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._records.get(record_id)

    def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._records.values())
        if prefix:
            items = [item for item in items if item["name"].startswith(prefix)]
        # 稳定顺序：便于断言 records[0]
        return sorted(items, key=lambda item: (item["created_at"], item["id"]))

    def delete(self, prefix: str = "") -> int:
        with self._lock:
            if not prefix:
                count = len(self._records)
                self._records.clear()
                return count
            doomed = [key for key, item in self._records.items() if item["name"].startswith(prefix)]
            for key in doomed:
                del self._records[key]
            return len(doomed)


class _Handler(BaseHTTPRequestHandler):
    server_version = "dm-demo-store/1.0"
    protocol_version = "HTTP/1.1"
    store: DataStore  # 由 serve() 注入

    # ---------------------------------------------------------------- 工具
    def log_message(self, format, *args):  # noqa: A002 - 与基类签名一致
        # 默认实现把每条访问日志写到 stderr；需要排查时设 DM_STORE_VERBOSE=1
        if os.environ.get("DM_STORE_VERBOSE") == "1":
            print(f"[data-store] {format % args}", flush=True)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _token_ok(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        return auth == f"Bearer {self.server.expected_token}"  # type: ignore[attr-defined]

    def _dangerous_allowed(self) -> bool:
        return (self.headers.get(DANGEROUS_HEADER) or "").lower() == DANGEROUS_VALUE

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        parsed = urlparse(self.path)
        segments = [segment for segment in parsed.path.split("/") if segment]
        query = parse_qs(parsed.query)

        if not segments or segments[0] == "health":
            return self._send_json({"status": "ok"})

        if segments[0] != "records":
            return self._send_json({"error": f"unknown path: {self.path}"}, status=404)

        if not self._token_ok():
            return self._send_json({"error": "missing or invalid token"}, status=401)

        if len(segments) >= 2:
            record = self.store.get(segments[1])
            if record is None:
                return self._send_json({"error": f"record not found: {segments[1]}"}, status=404)
            return self._send_json(record)

        prefix = (query.get("prefix") or [""])[0]
        records = self.store.list(prefix)
        return self._send_json({"records": records, "count": len(records), "prefix": prefix})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        segments = [segment for segment in parsed.path.split("/") if segment]
        payload = self._read_json()

        if segments and segments[0] == "login":
            payload = payload or {}
            username = payload.get("username", "")
            password = payload.get("password", "")
            if username != self.server.expected_username or password != self.server.expected_password:  # type: ignore[attr-defined]
                return self._send_json({"error": "bad credentials"}, status=401)
            return self._send_json({"token": self.server.expected_token, "user": username})  # type: ignore[attr-defined]

        if not segments or segments[0] != "records":
            return self._send_json({"error": f"unknown path: {self.path}"}, status=404)

        if not self._token_ok():
            return self._send_json({"error": "missing or invalid token"}, status=401)

        payload = payload or {}
        name = payload.get("name")
        if not name:
            return self._send_json({"error": "field 'name' is required"}, status=400)

        record = self.store.create(name, payload.get("payload"))
        return self._send_json(record, status=201)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        segments = [segment for segment in parsed.path.split("/") if segment]
        query = parse_qs(parsed.query)

        if not segments or segments[0] != "records":
            return self._send_json({"error": f"unknown path: {self.path}"}, status=404)

        if not self._token_ok():
            return self._send_json({"error": "missing or invalid token"}, status=401)

        prefix = (query.get("prefix") or [""])[0]
        if not prefix and not self._dangerous_allowed():
            return self._send_json(
                {
                    "error": "refusing to wipe the whole store",
                    "hint": f"带 ?prefix= 只删自己造的数据；确实要清空请加 {DANGEROUS_HEADER}: {DANGEROUS_VALUE}",
                },
                status=403,
            )

        deleted = self.store.delete(prefix)
        return self._send_json({"deleted": deleted, "prefix": prefix})


def serve(
    port: int = 0,
    host: str = "127.0.0.1",
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    token: Optional[str] = None,
) -> Tuple[ThreadingHTTPServer, str]:
    """启动服务，返回 ``(server, base_url)``。``port=0`` 表示由系统分配空闲端口。"""
    handler = type("_BoundHandler", (_Handler,), {"store": DataStore()})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.expected_username = username  # type: ignore[attr-defined]
    httpd.expected_password = password  # type: ignore[attr-defined]
    httpd.expected_token = token or f"dm-{uuid.uuid4().hex[:16]}"  # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://{host}:{httpd.server_address[1]}"
