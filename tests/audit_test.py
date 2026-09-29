# -*- coding: utf-8 -*-
r"""审计时间线的护栏用例 —— v8 §2.5 安全面 3 / §5.8 行 1003（**P3c**）。

## 本文件钉什么

| 要求 | 用例 |
| --- | --- |
| **投影而非双源**（每条事件都能指回文件） | `TestProjection::test_events_point_back_at_files` |
| **时间是证据说了算**（不是"现在"） | `TestProjection::test_time_comes_from_evidence` |
| **时间未知的不排进时间轴** | `TestSplitEvents::*` + `test_naive_sort_would_put_unknown_first`（元护栏） |
| **读不到单列**（它是异常，不是"没有"） | `TestProjection::test_unreadable_is_listed_separately` |
| **同一口径散在三处 → 用判据绑住** | `TestSuffixBinding::*` |
| **只读 → 不进 T18** | `TestModuleForm::*` |

## ★夹具怎么造

**复用真的 writer**（`reviews.write_review_record` / `jobs.write_job_record`）——
不手写 JSON。理由是：手写的夹具只能证明"我能读我写的格式"，
而真 writer 写出来的才是**产线真的会留下的东西**。
"""

import json
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.audit import (  # noqa: E402
    ACTION_ACCEPT,
    ACTION_MIXED,
    ACTION_OPEN,
    ACTION_RUN,
    ACTION_UNREADABLE,
    KIND_JOB,
    KIND_PENDING,
    KIND_REVIEW,
    UNKNOWN_TIME,
    AuditEvent,
    AuditTimeline,
    audit_summary,
    audit_timeline,
    events_from_jobs,
    events_from_pending,
    events_from_reviews,
    render_audit,
    run_selftest,
    split_events,
)
from interfacetester_ai.jobs import JobResult, write_job_record  # noqa: E402
from interfacetester_ai.reviews import write_review_record  # noqa: E402


class _SandboxCase(unittest.TestCase):
    """临时工作区（三个证据源都由**真 writer** 造出来）。"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_audit_test_")
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _write_review(self, case="login_flow", *, who="zhangsan", when="2026-09-25T10:00:00"):
        write_review_record(
            case,
            draft_path=f".ai/pending/{case}.pending.json",
            draft_text="some content\n",
            approver=who,
            resolved_todos=("pending:S3:1",),
            notes="[接受] pending:S3:1\n[拒绝] fix:0 —— 理由：这是真实缺陷",
            approved_at=when,
        )

    def _write_job(self, job_id="run-20260925-100500-1-0001", *, who="tester", when="2026-09-25T10:05:00"):
        write_job_record(
            JobResult(
                job_id=job_id,
                ok=False,
                exit_code=4,
                triggered_by=who,
                started_at=when,
                finished_at=when,
                job_dir=".ai/jobs/" + job_id,
                stderr="pytest: file not found",
            )
        )

    def _write_pending(self, case="login_flow"):
        os.makedirs(".ai/pending", exist_ok=True)
        with open(f".ai/pending/{case}.pending.json", "w", encoding="utf-8") as fp:
            json.dump(
                {"case": case, "count": 1, "items": [{"code": "S3"}]}, fp, ensure_ascii=False
            )


class TestSplitEvents(unittest.TestCase):
    """★分段与排序（纯函数，不碰文件系统）。"""

    def _events(self):
        return [
            AuditEvent(when="2026-09-25T10:00:00", kind=KIND_REVIEW, action=ACTION_ACCEPT, source="r"),
            AuditEvent(when=UNKNOWN_TIME, kind=KIND_PENDING, action=ACTION_OPEN, source="p"),
            AuditEvent(when="2026-09-25T09:00:00", kind=KIND_JOB, action=ACTION_RUN, source="j"),
        ]

    def test_timed_is_sorted_and_untimed_is_separate(self):
        timed, untimed = split_events(self._events())

        self.assertEqual(
            [item.when for item in timed],
            ["2026-09-25T09:00:00", "2026-09-25T10:00:00"],
        )
        self.assertEqual(len(untimed), 1)
        self.assertEqual(untimed[0].kind, KIND_PENDING)

    def test_naive_sort_would_put_unknown_first(self):
        """★**元护栏**：证明"分段"不是装饰。

        朴素的按字典序混排会让 `(无时间戳)` 跑到**最前面**——
        看起来像"最早发生的事"，而它其实只是"没有时间字段"。
        这一条一旦不再成立（比如把占位符换了），分段的必要性就要重新评估。
        """
        naive = sorted(self._events(), key=lambda item: item.when)

        self.assertEqual(naive[0].when, UNKNOWN_TIME)

    def test_unknown_is_a_placeholder_not_now(self):
        """占位符必须是**显眼的**，不能长得像时间戳（否则又会被当成时间）。"""
        self.assertNotIn("20", UNKNOWN_TIME)


class TestSuffixBinding(_SandboxCase):
    """★同一口径散在三处 → **用判据绑住**（而不是靠人记得）。"""

    def test_pending_suffix_is_bound_across_modules(self):
        """`pending.py` 的写盘行**必须内联字面量**（T18 静态可判纪律），
        所以那个口径**引用不了**；`confirm` 与 `audit` 各有一处独立定义。

        三者不一致的后果很隐蔽：**审计会把清单读漏**，
        而"读漏"在页面上看起来**和"没有待确认项"一模一样**。
        """
        from interfacetester_ai import audit as audit_mod  # noqa: PLC0415
        from interfacetester_ai import confirm as confirm_mod  # noqa: PLC0415
        from interfacetester_ai.pending import pending_path  # noqa: PLC0415

        self.assertEqual(audit_mod.PENDING_SUFFIX, confirm_mod.PENDING_SUFFIX)
        self.assertTrue(
            pending_path("x").endswith(audit_mod.PENDING_SUFFIX),
            f"`pending.pending_path('x')` = {pending_path('x')} 与审计的后缀对不上",
        )

    def test_audit_reads_a_real_pending_list(self):
        """绑住的判据要能**真的读到**真 writer 写出来的清单。"""
        from interfacetester_ai.pending import pending_path  # noqa: PLC0415

        self._write_pending()

        events = events_from_pending()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, ACTION_OPEN)
        self.assertEqual(events[0].source, pending_path("login_flow"))


class TestProjection(_SandboxCase):
    """★投影：三个源都要读到，且**每条都能指回文件**。"""

    def test_events_point_back_at_files(self):
        self._write_review()
        self._write_job()
        self._write_pending()

        sources = {event.source for event in audit_timeline().all_events()}

        self.assertIn(".ai/reviews/login_flow.json", sources)
        self.assertIn(".ai/jobs/run-20260925-100500-1-0001/result.json", sources)
        self.assertIn(".ai/pending/login_flow.pending.json", sources)

    def test_time_comes_from_evidence(self):
        """★时间不是"现在"——它是**证据里写着的**那一个。

        判据：把 `approved_at` 设成 2020 年，时间线里就必须是 2020 年。
        （实现若偷懒读系统时钟，这条立刻红。）
        """
        self._write_review(when="2020-01-01T00:00:00")

        reviews = [item for item in audit_timeline().timed if item.kind == KIND_REVIEW]

        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].when, "2020-01-01T00:00:00")

    def test_who_and_why_are_carried(self):
        self._write_review(who="lisi")

        event = events_from_reviews()[0]

        self.assertEqual(event.who, "lisi")
        self.assertIn("真实缺陷", event.reason)  # ★reject 的理由
        self.assertIn("内容哈希", event.detail)

    def test_accept_and_reject_together_is_labelled_mixed(self):
        """留痕里同时有接受与拒绝 → **不猜成其中一种**。"""
        self._write_review()

        self.assertEqual(events_from_reviews()[0].action, ACTION_MIXED)

    def test_job_event_carries_exit_code(self):
        self._write_job()

        event = events_from_jobs()[0]

        self.assertEqual(event.exit_code, 4)
        self.assertEqual(event.who, "tester")
        self.assertEqual(event.action, ACTION_RUN)

    def test_unreadable_is_listed_separately(self):
        """★读不到**单列**：它不是「没有」，是「异常」。"""
        os.makedirs(".ai/reviews", exist_ok=True)
        with open(".ai/reviews/broken.json", "w", encoding="utf-8") as fp:
            fp.write("{ not json")

        events = events_from_reviews()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, ACTION_UNREADABLE)
        self.assertEqual(audit_summary(audit_timeline())["unreadable"], 1)

    def test_pending_has_no_invented_time(self):
        """★待确认清单**没有**时间字段 → 一律 `(无时间戳)`，**不编一个"现在"**。

        编了会让"这份清单躺了三周"这件事永远看不见——而它最该被看见。
        """
        self._write_pending()

        self.assertEqual(events_from_pending()[0].when, UNKNOWN_TIME)

    def test_ordering_is_chronological(self):
        self._write_review(when="2026-09-25T10:00:00")
        self._write_job(when="2026-09-25T09:00:00")

        timed = audit_timeline().timed

        self.assertEqual(timed[0].when, "2026-09-25T09:00:00")
        self.assertEqual(timed[-1].when, "2026-09-25T10:00:00")

    def test_render_shows_sources_and_sections(self):
        self._write_review()
        self._write_pending()

        text = render_audit()

        self.assertIn("来源", text)
        self.assertIn(".ai/reviews/login_flow.json", text)
        self.assertLess(text.index("时间未知"), text.index("## 时间线"))

    def test_empty_workspace_does_not_raise(self):
        timeline = audit_timeline()

        self.assertEqual(timeline.all_events(), [])
        self.assertIn("没有读到任何证据", render_audit(timeline))


class TestQualityIsProjected(_SandboxCase):
    """★C（2026-09-26）：审批事件必须带上**审批时的质量状态**，且老留痕**不编**。

    评审 §五 要求"报告并列展示已覆盖/未覆盖/协议级/降级/状态"——审计时间线是最该说这件事的地方：
    它回答的是"**当时凭什么放行**"。而改造前的留痕只记了"谁签的"。
    """

    def test_written_quality_shows_up_in_the_event_and_the_render(self):
        from interfacetester_ai.quality import QualityAssessment  # noqa: PLC0415

        write_review_record(
            "thin",
            draft_path=".ai/draft/thin.yml",
            draft_text="content\n",
            approver="zhangsan",
            approved_at="2026-09-26T10:00:00",
            resolved_blockers=("protocol-only",),
            quality=QualityAssessment(status="needs_review", blockers=("protocol-only",)),
        )

        review_events = [item for item in audit_timeline().timed if item.kind == "review"]

        self.assertEqual(len(review_events), 1)
        event = review_events[0]
        self.assertIn("needs_review", event.quality)
        self.assertIn("protocol-only", event.quality)
        self.assertIn("已点名处置", event.quality)
        # 三样都要能被复核：状态 / 当时实际有哪些 blocker / 其中处置了哪些
        self.assertEqual(event.quality, event.to_dict()["quality"])

        text = render_audit()
        self.assertIn("needs_review", text)
        self.assertIn("已点名处置：protocol-only", text)

    def test_old_records_are_not_filled_in(self):
        """★**不拿"没问题"填空**：老留痕没记过质量，就必须说"未记录"。"""
        self._write_review()  # 这份留痕**不带** quality 字段（本次改动之前签的那批）

        event = [item for item in audit_timeline().timed if item.kind == "review"][0]

        self.assertEqual(event.quality, "", "没记录的字段不该被编出来")
        self.assertIn("未记录", render_audit(), "读不到就要如实说，而不是留空让人以为没问题")

    def test_job_events_do_not_get_a_quality_column_value(self):
        """作业事件没有质量概念：渲染里是 `—`，**不许**写"未记录"（那是审批才有的说法）。"""
        self._write_job()

        text = render_audit()

        self.assertNotIn("质量：**未记录**", text)


class TestModuleForm(unittest.TestCase):
    """形态：只读、不进 T18、自检绿。"""

    def test_audit_is_not_a_registered_writer(self):
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        self.assertNotIn("audit", MODULE_WRITE_BOUNDARIES)

    def test_audit_source_has_no_write_calls(self):
        import ast  # noqa: PLC0415

        path = os.path.join(BASE, "interfacetester_ai", "audit.py")
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
            "os.replace",
            "os.rename",
            "shutil.rmtree",
        ):
            self.assertNotIn(banned, called, f"`audit.py` 里出现了写盘调用 `{banned}`")

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)


if __name__ == "__main__":
    unittest.main()

