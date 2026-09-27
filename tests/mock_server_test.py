"""本地 mock 服务（``tests/mock_server.py``）自身的冒烟用例。

目的：保证 mock 的返回结构与 httpbin 对齐，避免「用例断言」和「mock 行为」一起漂移，
否则用例会假绿。
"""

import unittest

import requests
import urllib3

from mock_server import HTTP_BIN_URL, HTTPS_BIN_URL, https_available

# 本地 https mock 用自签名证书，关闭 urllib3 的 InsecureRequestWarning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class TestMockHttpBin(unittest.TestCase):
    """HTTP mock（默认 127.0.0.1:80，可用 INTERFACETESTER_HTTP_BIN_PORT 改；地址与 interfacetester.utils.HTTP_BIN_URL 一致）。"""

    def test_get_echo(self):
        resp = requests.get(f"{HTTP_BIN_URL}/get", params={"a": "1"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["args"], {"a": "1"})
        self.assertEqual(payload["method"], "GET")
        self.assertEqual(payload["origin"], "127.0.0.1")

    def test_post_anything_echo_json_body(self):
        body = {"locations": [{"name": "Seattle", "state": "WA"}]}
        resp = requests.post(f"{HTTP_BIN_URL}/anything", json=body)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["json"], body)

    def test_post_form_echo(self):
        resp = requests.post(f"{HTTP_BIN_URL}/post", data={"a": "1", "b": "2"})
        self.assertEqual(resp.json()["form"], {"a": "1", "b": "2"})

    def test_headers_cookies_and_user_agent(self):
        resp = requests.get(f"{HTTP_BIN_URL}/headers", headers={"X-Test": "v"})
        self.assertEqual(resp.json()["headers"]["X-Test"], "v")

        resp = requests.get(f"{HTTP_BIN_URL}/cookies", cookies={"foo": "bar"})
        self.assertEqual(resp.json()["cookies"], {"foo": "bar"})

        resp = requests.get(f"{HTTP_BIN_URL}/user-agent")
        self.assertIn("python-requests", resp.json()["user-agent"])

    def test_status_code(self):
        for status_code in (200, 201, 404, 500):
            resp = requests.get(f"{HTTP_BIN_URL}/status/{status_code}")
            self.assertEqual(resp.status_code, status_code)

    def test_redirect_to(self):
        resp = requests.get(
            f"{HTTP_BIN_URL}/redirect-to",
            params={"url": f"{HTTP_BIN_URL}/get"},
            allow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], f"{HTTP_BIN_URL}/get")

        resp = requests.get(
            f"{HTTP_BIN_URL}/redirect-to", params={"url": f"{HTTP_BIN_URL}/get"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.history), 1)

    def test_stream_is_chunked_without_content_length(self):
        resp = requests.get(f"{HTTP_BIN_URL}/stream/3")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("content-length", resp.headers)
        self.assertEqual(resp.headers.get("transfer-encoding"), "chunked")
        lines = [line for line in resp.text.splitlines() if line]
        self.assertEqual(len(lines), 3)

    def test_bytes_and_image(self):
        resp = requests.get(f"{HTTP_BIN_URL}/bytes/16")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.content), 16)

        resp = requests.get(f"{HTTP_BIN_URL}/image/png")
        self.assertEqual(resp.headers["Content-Type"], "image/png")
        self.assertTrue(resp.content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_gzip_is_transparently_decoded_by_requests(self):
        resp = requests.get(f"{HTTP_BIN_URL}/gzip")
        self.assertEqual(resp.headers["Content-Encoding"], "gzip")
        self.assertEqual(resp.json()["method"], "GET")

    def test_basic_auth(self):
        resp = requests.get(f"{HTTP_BIN_URL}/basic-auth/user/passwd")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.headers["WWW-Authenticate"], 'Basic realm="Fake Realm"')

        resp = requests.get(
            f"{HTTP_BIN_URL}/basic-auth/user/passwd", auth=("user", "passwd")
        )
        self.assertTrue(resp.json()["authenticated"])

    def test_delay_and_timeout_mock(self):
        resp = requests.get(f"{HTTP_BIN_URL}/delay/0.1")
        self.assertEqual(resp.status_code, 200)

    def test_unimplemented_path_returns_404(self):
        resp = requests.get(f"{HTTP_BIN_URL}/not-implemented")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("error", resp.json())


class TestMockHttpsBin(unittest.TestCase):
    """HTTPS mock（默认 127.0.0.1:443，可用 INTERFACETESTER_HTTPS_BIN_PORT 改；自签名证书，用例以 verify=False 访问）。"""

    @unittest.skipUnless(https_available(), "https mock 未启动")
    def test_get_over_tls(self):
        resp = requests.get(f"{HTTPS_BIN_URL}/get", verify=False)
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertTrue(payload["url"].startswith("https://"))
        self.assertEqual(payload["origin"], "127.0.0.1")
