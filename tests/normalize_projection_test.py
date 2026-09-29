# -*- coding: utf-8 -*-
"""归一化投影的单测（方案 v8 §5.6 / T11 + T17 落地的判据级用例）。

判据出处（用例名即判据，口径不许含糊——T17）：
  - 同输入两次投影逐字节相等（T11）；
  - **跨秒**两次（Date ±1s、case_id/Request-ID/elapsed/stat/time 全不同）
    投影后仍逐字节相等（T17 判据①）；
  - 响应头白名单：Date/Server/Set-Cookie/Age/ETag 剔，Content-Type/
    Content-Length/与 status 相关的头留（T17 判据②，X5）；
  - 断言结果主体必须保留（§5.6【必须保留】）；
  - 顶层 time 是 T11 落地时新发现的清单漏项，本文件把它钉死（漏剔必红）。

元护栏（与闸门自检同构的成对纪律）：只测"剔得干净"会得到一个
"什么都剔"的投影——那会把结论也剔掉。所以"必须保留"与"必须剔除"
必须同时成立。
"""

import json
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from bench.normalize_projection import (  # noqa: E402
    _sample_summary,
    project_summary,
)

REAL_SUMMARY = os.path.join(BASE, "project-two", "logs", "login_flow.summary.json")


class TestProjectionDeterministic(unittest.TestCase):
    """确定性判据（T11 / T17）。"""

    def test_same_input_projected_twice_is_byte_identical(self):
        """T11：同输入两次投影逐字节相等。"""
        doc = _sample_summary("Tue, 22 Sep 2026 15:02:22 GMT", "cid", "rid", 7.18)
        self.assertEqual(project_summary(doc), project_summary(doc))

    def test_projection_stable_cross_machine(self):
        """T17 判据①：跨秒两次（Date +1s、UUID/耗时/stat 全变）投影后逐字节相等。

        跨机口径（默认剔 platform）。
        """
        x1 = _sample_summary("Tue, 22 Sep 2026 15:02:22 GMT",
                             "7fcfc1d6-05a8-412c-8826-fd15469e68a2",
                             "interfacetester-x1-150222", 7.18)
        x2 = _sample_summary("Tue, 22 Sep 2026 15:02:23 GMT",
                             "00000000-0000-0000-0000-000000000000",
                             "interfacetester-x2-260223", 8.04)
        self.assertEqual(project_summary(x1), project_summary(x2))

    def test_projection_stable_same_machine(self):
        """T17 口径纪律：同机口径保留 platform，且平台信息仍在投影里。"""
        x1 = _sample_summary("Tue, 22 Sep 2026 15:02:22 GMT", "cid", "rid", 7.18)
        out = project_summary(x1, keep_platform=True)
        self.assertIn("platform", out)
        self.assertNotIn("platform", project_summary(x1), "跨机口径（默认）必须剔 platform")

    @unittest.skipUnless(os.path.exists(REAL_SUMMARY), "真实产物不存在")
    def test_real_artifact_projection_is_idempotent(self):
        """真实产物幂等：login_flow.summary.json 两次投影逐字节相等。"""
        with open(REAL_SUMMARY, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(project_summary(doc), project_summary(doc))
        # 同机口径同样幂等
        self.assertEqual(
            project_summary(doc, keep_platform=True),
            project_summary(doc, keep_platform=True),
        )


class TestProjectionKeepsVerdictStripsVolatile(unittest.TestCase):
    """保留主体 / 剔除易变（成对纪律，缺一即假绿）。"""

    def test_verdict_body_is_preserved(self):
        """§5.6【必须保留】：断言结果主体完整保留。"""
        doc = _sample_summary("Tue, 22 Sep 2026 15:02:22 GMT", "cid", "rid", 7.18)
        out = json.loads(project_summary(doc))
        v = out["details"][0]["records"][0]["data"]["validators"][0]
        self.assertEqual("eq", v["comparator"])
        self.assertEqual("status_code", v["check"])
        self.assertEqual(200, v["check_value"])
        self.assertEqual("pass", v["check_result"])

    def test_volatile_fields_are_stripped(self):
        """易变字段必须剔干净：Date/Server/Request-ID/case_id/log/time/stat。"""
        doc = _sample_summary("Tue, 22 Sep 2026 15:02:22 GMT", "cid", "rid", 7.18)
        out = project_summary(doc)
        for banned in ('"Date"', '"Server"', "interfacetester-Request-ID",
                       '"case_id"', '"log"', '"start_at"', '"response_time_ms"',
                       '"elapsed"', '"duration"'):
            self.assertNotIn(banned, out, "投影输出仍含易变字段：" + banned)

    def test_top_level_time_is_stripped(self):
        """★T11 新发现漏项：顶层 time（summary 级 start_at/duration）必须剔除。

        v8 §5.6 的清单（连 v7 补全后）仍漏了它——不剔则"跨秒两次逐字节
        一致"恒红。本用例把这条钉死，防止回归。
        """
        doc = _sample_summary("Tue, 22 Sep 2026 15:02:22 GMT", "cid", "rid", 7.18)
        out = json.loads(project_summary(doc))
        self.assertEqual({}, out["time"])
        self.assertEqual({}, out["details"][0]["time"])
        self.assertEqual({}, out["details"][0]["records"][0]["data"]["stat"])

    def test_response_header_whitelist(self):
        """T17 判据②：易变头剔、稳定头与 status 相关头留（X5）。"""
        doc = _sample_summary("Tue, 22 Sep 2026 15:02:22 GMT", "cid", "rid", 7.18)
        resp = doc["details"][0]["records"][0]["data"]["req_resps"][0]["response"]
        resp["headers"].update({
            "Set-Cookie": "session=abc; Path=/",
            "Age": "12",
            "ETag": '"abc123"',
            "X-Status-Code": "200",
        })
        headers = json.loads(project_summary(doc))["details"][0]["records"][0][
            "data"]["req_resps"][0]["response"]["headers"]
        self.assertNotIn("Date", headers)
        self.assertNotIn("Server", headers)
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn("Age", headers)
        self.assertNotIn("ETag", headers)
        self.assertEqual("application/json", headers["Content-Type"])
        self.assertEqual("87", headers["Content-Length"])
        self.assertEqual("200", headers["X-Status-Code"])

    def test_request_id_is_stripped_but_other_request_headers_kept(self):
        """请求侧只剔 interfacetester-Request-ID，其余请求头保留。"""
        doc = _sample_summary("Tue, 22 Sep 2026 15:02:22 GMT", "cid", "rid", 7.18)
        req = json.loads(project_summary(doc))["details"][0]["records"][0][
            "data"]["req_resps"][0]["request"]["headers"]
        self.assertNotIn("interfacetester-Request-ID", req)
        self.assertEqual("*/*", req["Accept"])
        self.assertEqual("application/json", req["Content-Type"])


if __name__ == "__main__":
    unittest.main()
