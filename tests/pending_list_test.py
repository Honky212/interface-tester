# -*- coding: utf-8 -*-
"""PENDING 清单落盘 writer 的护栏用例（v8 §8.1 S-6）。

## 为什么需要这个文件

S-6 的判据（§3.4 / §8.1）：S3(部分) / S4 的 PENDING → `<case>.pending.json`，
**与 §5.2 的清单格式一致**。T1 落地后 S3 的 PENDING 态已存在（显式
`validate: []`），本文件钉住「report → 清单文件」这一步不回退。

## 判据（成对）

- **有 PENDING 必落盘**：S3 显式空 / S4 未引用的 extract，各落一份清单，
  文件名按 §5.2 的 `<case>.pending.json`，items 与 Finding.to_dict 同构；
- **无 PENDING 不落盘**：全绿 case 返回 None 且不创建文件——空清单会让
  人误以为有活要干（同「半成品冒成功」的取向）；
- **闸门零副作用**：check_testcase 本身不写任何文件（§0.6 架构图约束，
  落盘由调用方显式触发）；
- **写盘根形态**：落盘路径必须以 `.ai/` 开头（红线③ 注册表——
  pending 登记在册只许写 `.ai/`，与 `tests/write_boundary_test.py` 互补：
  那边钉**源码形态**，这边钉**运行期行为**）。
"""

import json
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.gates import PENDING, check_testcase  # noqa: E402
from interfacetester_ai.pending import (  # noqa: E402
    pending_path,
    write_pending_list,
)


def _case(steps):
    return {"name": "t", "teststeps": steps}


def _req(name, validate=None, extract=None):
    step = {"name": name, "request": {"method": "GET", "url": "/api/x"}}
    if validate is not None:
        step["validate"] = validate
    if extract is not None:
        step["extract"] = extract
    return step


class _TmpWorkdir(unittest.TestCase):
    """公共夹具：在临时工作区根下跑（.ai/ 是相对根，cwd 必须是工作区根）。"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="pending_test_")
        os.chdir(self._tmp)

    def tearDown(self):
        os.chdir(self._old_cwd)
        # 临时目录整体交给系统清理（mkdtemp 生命周期），不依赖 rmtree


class TestPendingWrittenForS3(_TmpWorkdir):
    """S3(部分) 的 PENDING（显式 validate: []）→ 落盘（T1 提供落盘对象）。"""

    def test_explicit_empty_validate_lands_pending_file(self):
        report = check_testcase(_case([_req("s", validate=[])]))
        self.assertEqual(len(report.pendings), 1)
        self.assertEqual(report.pendings[0].code, "S3")

        rel = write_pending_list(report, "login_flow")
        self.assertIsNotNone(rel)
        self.assertTrue(os.path.isfile(rel), f"清单未落盘：{rel}")

        payload = json.loads(open(rel, encoding="utf-8").read())
        self.assertEqual(payload["case"], "login_flow")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["code"], "S3")
        self.assertEqual(payload["items"][0]["severity"], PENDING)


class TestPendingWrittenForS4(_TmpWorkdir):
    """S4 的 PENDING（extract 键从未被校验/引用）→ 落盘。"""

    def test_unreferenced_extract_lands_pending_file(self):
        report = check_testcase(
            _case([_req("login", extract={"token": "body.data.token"}),
                   _req("next", validate=[{"eq": ["status_code", 200]}])])
        )
        pend_codes = [f.code for f in report.pendings]
        self.assertIn("S4", pend_codes)

        rel = write_pending_list(report, "multi_step")
        self.assertIsNotNone(rel)
        payload = json.loads(open(rel, encoding="utf-8").read())
        self.assertEqual(payload["case"], "multi_step")
        self.assertIn("S4", [item["code"] for item in payload["items"]])


class TestNoPendingNoFile(_TmpWorkdir):
    """无 PENDING → 返回 None 且不创建任何文件（空清单 = 假信号）。"""

    def test_clean_case_writes_nothing(self):
        report = check_testcase(
            _case([_req("s", validate=[{"eq": ["status_code", 200]}])])
        )
        self.assertEqual(report.pendings, [])

        self.assertIsNone(write_pending_list(report, "clean"))
        self.assertFalse(os.path.exists(".ai"), "无 PENDING 却创建了 .ai/ 目录")


class TestFilenameConvention(_TmpWorkdir):
    """§5.2 命名口径：`<case>.pending.json`，落盘根必须在 .ai/ 下。"""

    def test_filename_is_case_dot_pending_json(self):
        report = check_testcase(_case([_req("s", validate=[])]))
        rel = write_pending_list(report, "order_create")
        self.assertTrue(
            rel.endswith("order_create.pending.json"),
            f"文件名不符合 §5.2 口径：{rel}",
        )

    def test_pending_path_is_under_ai_root(self):
        # 红线③：pending 登记在册的写入根固定 .ai/（运行期行为与 T18 源码形态互补）
        self.assertTrue(pending_path("x").startswith(".ai/"))
        report = check_testcase(_case([_req("s", validate=[])]))
        rel = write_pending_list(report, "x")
        self.assertTrue(os.path.relpath(rel, ".").startswith(".ai"))


class TestGatesStaySideEffectFree(_TmpWorkdir):
    """§0.6：闸门零副作用——check_testcase 本身绝不写盘。"""

    def test_check_testcase_creates_no_files(self):
        before = sorted(os.listdir("."))
        report = check_testcase(_case([_req("s", validate=[])]))
        after = sorted(os.listdir("."))
        self.assertEqual(before, after)  # 有 1 条 PENDING，但没有落盘
        self.assertEqual(len(report.pendings), 1)

    def test_payload_matches_finding_shape(self):
        # items 与 Finding.to_dict 同构：code/severity/where/message（+hint）
        report = check_testcase(_case([_req("s", validate=[])]))
        rel = write_pending_list(report, "shape")
        item = json.loads(open(rel, encoding="utf-8").read())["items"][0]
        for key in ("code", "severity", "where", "message"):
            self.assertIn(key, item)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

