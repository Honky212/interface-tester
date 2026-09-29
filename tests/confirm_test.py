# -*- coding: utf-8 -*-
r"""确认门的护栏用例 —— v8 §2.5 行 339 / §5.8 行 1002（**P3b-1**）。

## 本文件钉什么

| 要求 | 用例 |
| --- | --- |
| 每次确认落 `.ai/reviews/*.json`（**含内容哈希**） | `TestRecord::*` |
| **reject 必填理由**（空 / 纯空白都拦） | `TestValidate::test_reject_without_reason_is_refused` |
| 校验不过 → **422**（不是 200） | `TestHttpConfirm::test_refused_body_is_422_not_200` |
| 留痕**绑定内容**，改一行即失效（T24 联动） | `TestRecord::test_record_binds_to_content` |
| `/confirm` 的写例外**没有扩散** | `TestHttpConfirm::test_write_exception_does_not_spread` |

## ★P3b-1 刻意**不做**的两件事（写给未来的自己）

- **不执行**：不跑用例、不调模型、不起子进程——「自动重跑 L2」属 **P3b-2 触发运行**；
- **不落产物**：`accept` 只**记录**；回填内容原样列出，由**人**复制走
  （与 P2a/P2b 同款立场：**产物给人看，落地动作留给人**）。

## 与 `web_readonly_test.py` 的分工

那个文件钉 P3a 的四条硬约束（零写盘 / 零执行 / 穿越 / 回环），并且**只做 GET**；
本文件钉确认门，**会真的落痕**（所以夹具与写动作都在 tests/ 里 —— T18 纪律）。
"""

import json
import os
import sys
import tempfile
import threading
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.confirm import (  # noqa: E402
    ACTION_ACCEPT,
    REFUSE_BAD_ACTION,
    REFUSE_NO_APPROVER,
    REFUSE_NO_CASE,
    REFUSE_NO_REASON,
    REFUSE_NO_TARGET,
    ConfirmRequest,
    confirm,
    fill_pending,
    load_pending_lists,
    notes_text,
    parse_payload,
    run_selftest,
    validate,
)
from interfacetester_ai.reviews import require_review  # noqa: E402
from interfacetester_ai.web import (  # noqa: E402
    ALLOWED_ROOTS,
    CONFIRM_REFUSED_STATUS,
    DEFAULT_HOST,
    fetch,
    make_server,
)


def good_payload():
    """一份合规提交（**每次都新建**——测试会就地改它）。"""
    return {
        "case": "login_flow",
        "approver": "zhangsan",
        "decisions": [
            {"target": "pending:S3:1", "action": "accept", "fill": {"expect": 200}},
            {
                "target": "fix:0:login_flow/步骤1",
                "action": "reject",
                "reason": "这是真实缺陷，不该改用例",
            },
        ],
        "notes": "本轮确认依据接口文档 v3 §2.1",
        "target_path": ".ai/pending/login_flow.pending.json",
        "target_text": "some content\n",
        "approved_at": "2026-09-25T10:00:00",
    }


class _SandboxCase(unittest.TestCase):
    """临时工作区（`.ai/pending/` 有夹具；**不碰本仓的 `.ai/`**）。"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_confirm_test_")
        os.chdir(self._tmp.name)
        os.makedirs(".ai/pending", exist_ok=True)
        items = [
            {
                "code": "S3",
                "severity": "pending",
                "where": "validate[0].expect",
                "message": "文档没写这个期望值",
                "hint": "请问产品",
            }
        ]
        with open(".ai/pending/login_flow.pending.json", "w", encoding="utf-8") as fp:
            json.dump(
                {"case": "login_flow", "count": 1, "items": items},
                fp,
                ensure_ascii=False,
                indent=2,
            )

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class _ServerCase(_SandboxCase):
    """临时工作区 + 回环临时端口上的看板（测完即关）。"""

    def setUp(self):
        super().setUp()
        self.server = make_server(DEFAULT_HOST, 0, ALLOWED_ROOTS)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        super().tearDown()

    def get(self, path, method=None, body=None):
        return fetch(self.port, path, method=method, body=body)


class TestValidate(unittest.TestCase):
    """★校验层：所有拒收理由（这是本模块最厚的一层）。"""

    def test_good_payload_passes(self):
        request, parse_errors = parse_payload(good_payload())

        self.assertEqual(parse_errors, [])
        self.assertEqual(validate(request), ([], []))

    def test_reject_without_reason_is_refused(self):
        """★§2.5 行 339 原文：reject **必填理由**——空 / 纯空白**一律拒收**。

        理由空着还能 reject，等于把「为什么不要它」**永久丢掉**：
        半年后没人知道当时为什么否掉这条建议。
        """
        for blank in ("", "   ", "\n\t ", "\u3000"):
            with self.subTest(blank=repr(blank)):
                payload = good_payload()
                payload["decisions"][1]["reason"] = blank

                errors, codes = validate(ConfirmRequest.from_dict(payload))

                self.assertTrue(errors)
                self.assertIn(REFUSE_NO_REASON, codes)

    def test_accept_does_not_need_a_reason(self):
        """反过来说：**接受**不该被逼着写理由（否则人会随手填个"ok"应付过去）。"""
        payload = good_payload()
        payload["decisions"] = [{"target": "t", "action": ACTION_ACCEPT, "reason": ""}]

        errors, _codes = validate(ConfirmRequest.from_dict(payload))

        self.assertEqual(errors, [])

    def test_missing_approver_is_refused(self):
        """★"允许空值等于允许一张没签名的单子"。"""
        payload = good_payload()
        payload["approver"] = "   "

        errors, codes = validate(ConfirmRequest.from_dict(payload))

        self.assertTrue(errors)
        self.assertIn(REFUSE_NO_APPROVER, codes)

    def test_missing_case_is_refused(self):
        payload = good_payload()
        payload["case"] = ""

        errors, codes = validate(ConfirmRequest.from_dict(payload))

        self.assertTrue(errors)
        self.assertIn(REFUSE_NO_CASE, codes)

    def test_empty_decisions_is_refused(self):
        """★空表返回成功 = 造出「这条已经确认过了」的**假证据**。"""
        payload = good_payload()
        payload["decisions"] = []

        errors, codes = validate(ConfirmRequest.from_dict(payload))

        self.assertTrue(errors)
        self.assertIn(REFUSE_NO_TARGET, codes)

    def test_unknown_action_is_refused(self):
        payload = good_payload()
        payload["decisions"][0]["action"] = "maybe"

        errors, codes = validate(ConfirmRequest.from_dict(payload))

        self.assertTrue(errors)
        self.assertIn(REFUSE_BAD_ACTION, codes)

    def test_duplicate_target_is_refused(self):
        """同一条目标给两次裁决时，**以哪一次为准并不明确**。"""
        payload = good_payload()
        payload["decisions"] = [payload["decisions"][0], dict(payload["decisions"][0])]

        errors, _codes = validate(ConfirmRequest.from_dict(payload))

        self.assertTrue(errors)

    def test_missing_target_is_refused(self):
        payload = good_payload()
        payload["decisions"] = [{"target": "   ", "action": ACTION_ACCEPT}]

        errors, codes = validate(ConfirmRequest.from_dict(payload))

        self.assertTrue(errors)
        self.assertIn(REFUSE_NO_TARGET, codes)

    def test_bad_body_is_not_guessed(self):
        """★**不猜**：格式不对就说格式不对（猜出来的请求会带着人的签名落进留痕）。"""
        for raw in ("", "{ not json", b"\xff\xfe", 42, None, "[1,2]"):
            with self.subTest(raw=repr(raw)):
                result = confirm(raw, record=False)

                self.assertFalse(result.ok)
                self.assertTrue(result.errors, f"{raw!r} 应当被拒且给出理由")


class TestRecord(_SandboxCase):
    """★留痕：`.ai/reviews/<case>.json`——**含内容哈希**，绑定到那一版内容上。"""

    def test_confirm_writes_a_record(self):
        result = confirm(good_payload())

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.record_path, ".ai/reviews/login_flow.json")
        self.assertTrue(os.path.isfile(".ai/reviews/login_flow.json"))

    def test_record_contains_who_when_what_why(self):
        """留痕要回答五件事：**谁、什么时候、对什么、对哪一版、为什么**。"""
        confirm(good_payload())

        with open(".ai/reviews/login_flow.json", encoding="utf-8") as fp:
            record = json.load(fp)

        self.assertEqual(record["approver"], "zhangsan")  # 谁
        self.assertEqual(record["approved_at"], "2026-09-25T10:00:00")  # 什么时候
        self.assertEqual(record["draft"], ".ai/pending/login_flow.pending.json")  # 对什么
        self.assertTrue(record["draft_hash"])  # 对哪一版（内容哈希）
        self.assertIn("真实缺陷", record["notes"])  # 为什么（★reject 的理由）
        self.assertIn("[接受]", record["notes"])
        self.assertIn("[拒绝]", record["notes"])

    def test_record_binds_to_content(self):
        """★T24 联动：留痕绑的是**内容哈希**——改一行即失效，必须重新确认。

        这正是"确认后有人又改了一行"**不能**被继承为"已确认"的原因；
        否则留痕就变成橡皮图章。
        """
        confirm(good_payload())
        draft_path = ".ai/draft/login_flow.yml"

        self.assertIsNone(
            require_review("login_flow", draft_path=draft_path, draft_text="some content\n"),
            "同一版内容应当放行",
        )
        self.assertIsNotNone(
            require_review("login_flow", draft_path=draft_path, draft_text="some content!\n"),
            "改了一行之后必须判为**留痕失效**",
        )

    def test_refused_submission_writes_nothing(self):
        """★**校验不过就一行都不写**：留痕是**证据**，半份证据比没有更坏。"""
        payload = good_payload()
        payload["approver"] = ""

        result = confirm(payload)

        self.assertFalse(result.ok)
        self.assertFalse(os.path.exists(".ai/reviews/login_flow.json"))

    def test_applied_text_is_presented_not_written(self):
        """★`accept` 只**记录**：回填内容**列出来给人**，不落进 `cases/`。"""
        result = confirm(good_payload())

        self.assertTrue(result.applied)
        self.assertTrue(any("expect" in line for line in result.applied))
        self.assertFalse(os.path.isdir("cases"))


class TestModuleForm(unittest.TestCase):
    """★"不写盘"的**形态**：写动作在 `reviews` 里，所以确认门**不进** T18 注册表。"""

    def test_confirm_is_not_a_registered_writer(self):
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        self.assertNotIn("confirm", MODULE_WRITE_BOUNDARIES)

    def test_confirm_source_has_no_write_calls(self):
        """AST 查 `confirm.py`：源码里**没有**写盘调用（落盘全委托给 `reviews`）。

        ★这就是"`web.py` 仍能保持零写盘形态"的机制：写发生在**别处**，
        而扫描器与审计看到的都是**登记在册**的那个 writer。
        """
        import ast  # noqa: PLC0415

        path = os.path.join(BASE, "interfacetester_ai", "confirm.py")
        with open(path, encoding="utf-8") as fp:
            tree = ast.parse(fp.read(), filename=path)

        called = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            parts = []
            func = node.func
            while isinstance(func, ast.Attribute):
                parts.append(func.attr)
                func = func.value
            if isinstance(func, ast.Name):
                parts.append(func.id)
            called.add(".".join(reversed(parts)))

        for banned in (
            "os.makedirs",
            "os.remove",
            "os.unlink",
            "os.replace",
            "os.rename",
            "shutil.rmtree",
        ):
            self.assertNotIn(banned, called, f"`confirm.py` 里出现了写盘调用 `{banned}`")

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_notes_text_keeps_the_reason(self):
        request = ConfirmRequest.from_dict(good_payload())

        text = notes_text(request)

        self.assertIn("[接受]", text)
        self.assertIn("[拒绝]", text)
        self.assertIn("真实缺陷", text)
        self.assertIn("目标产物", text)

    def test_fill_only_covers_accepted_items(self):
        payload = good_payload()
        payload["decisions"][1]["fill"] = {"expect": "不该出现"}

        lines = fill_pending(ConfirmRequest.from_dict(payload))

        self.assertTrue(lines)
        self.assertFalse(any("不该出现" in line for line in lines))

    def test_pending_lists_are_readable(self):
        loader = load_pending_lists

        self.assertTrue(callable(loader))


class TestPendingRead(_SandboxCase):
    """待确认清单的读取：★**坏文件不跳过**。"""

    def test_lists_are_found(self):
        lists = load_pending_lists()

        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]["case"], "login_flow")
        self.assertEqual(lists[0]["count"], 1)
        self.assertEqual(lists[0]["error"], "")

    def test_broken_list_is_not_silently_skipped(self):
        """★坏文件**不跳过**：否则"没人要确认"与"我看不到要确认的东西"长得一样。"""
        with open(".ai/pending/broken.pending.json", "w", encoding="utf-8") as fp:
            fp.write("{ not json")

        lists = load_pending_lists()

        self.assertEqual(len(lists), 2, "坏文件也必须出现在清单里")
        broken = [item for item in lists if "broken" in str(item["path"])]
        self.assertTrue(broken)
        self.assertTrue(broken[0]["error"], "坏文件要带上'读不到'的原因")

    def test_missing_dir_is_empty_not_an_error(self):
        import shutil  # noqa: PLC0415

        shutil.rmtree(".ai/pending")

        self.assertEqual(load_pending_lists(), [])


class TestHttpConfirm(_ServerCase):
    """`POST /confirm` 的 HTTP 契约（★端到端：**真的落痕**）。"""

    def test_accepted_submission_returns_200_and_records(self):
        status, body = self.get(
            "/confirm", body=json.dumps(good_payload()).encode("utf-8")
        )

        self.assertEqual(status, 200)
        self.assertIn("留痕已写入".encode("utf-8"), body)
        self.assertTrue(os.path.isfile(".ai/reviews/login_flow.json"))

    def test_refused_body_is_422_not_200(self):
        """★失败**不得**被柔化成一个"成功的页面"（R8）。"""
        for raw in (b"", b"{ not json"):
            with self.subTest(raw=raw):
                status, body = self.get("/confirm", body=raw)

                self.assertEqual(status, CONFIRM_REFUSED_STATUS)
                self.assertIn("拒绝落痕".encode("utf-8"), body)

    def test_reject_without_reason_is_422_and_writes_nothing(self):
        payload = good_payload()
        payload["decisions"][1]["reason"] = "   "

        status, _body = self.get("/confirm", body=json.dumps(payload).encode("utf-8"))

        self.assertEqual(status, CONFIRM_REFUSED_STATUS)
        self.assertFalse(
            os.path.exists(".ai/reviews/login_flow.json"),
            "被拒的提交**不许**留下留痕（半份证据比没有更坏）",
        )

    def test_write_exception_does_not_spread(self):
        """★`/confirm` 是**唯一**接受写的路径——其余路径的写方法仍必须 405。"""
        for path in ("/", "/metrics", "/pending", "/fix", "/run", "/file"):
            for method in ("PUT", "PATCH", "DELETE"):
                with self.subTest(path=path, method=method):
                    status, _body = self.get(path, method=method)

                    self.assertEqual(status, 405)

    def test_pending_page_renders_a_form(self):
        status, body = self.get("/pending")

        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("login_flow", text)
        self.assertIn("cf-body", text)  # 表单本体
        self.assertIn("/confirm", text)  # 提交目标

    def test_fix_page_needs_a_path(self):
        self.assertEqual(self.get("/fix")[0], 400)

    def test_oversized_body_is_413(self):
        from interfacetester_ai.web import MAX_CONFIRM_BYTES  # noqa: PLC0415

        status, _body = self.get("/confirm", body=b"x" * (MAX_CONFIRM_BYTES + 16))

        self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main()



