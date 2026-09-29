# -*- coding: utf-8 -*-
"""结果摘要的护栏用例 —— v8 §5.4（**P1b**）。

## §5.4 的验收是**两条**，本文件都钉住

1. **数字与 `summary.json` 全等** —— `TestStatsMatchSummaryJson`（含**真产物**对账：
   直接读 `project-two/logs/*.summary.json`，逐字段对齐 `stat` 块）；
2. **无 LLM 时模板版仍产出** —— `TestNoLlmStillProducesReport`
   （没有 transport / 缺配置 / **断网**，三种情形都必须有报告，且**都不算错误**）。

## 本文件里最要紧的一条判据：导语数字白名单

§5.4 说"导语里的数字受数字白名单正则复核；违规即弃用纯模板版"。
`test_discarded_lead_is_absent_from_report` 是它的**元护栏**：
弃用必须**落到正文**——只把状态改成"已弃用"而导语还留在报告里，等于没弃用。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.llm import FakeTransport, LLMConfig, LLMTransportError  # noqa: E402
from interfacetester_ai.summary import (  # noqa: E402
    TOP_N,
    UNGROUPED,
    allowed_numbers,
    case_prefix,
    collect_stats,
    render_summary_report,
    run_selftest,
    summarize_summary,
    verify_lead,
)

CONFIG = LLMConfig(base_url="http://127.0.0.1:11434", model="fake")

# 真产物的路径（"数字与 summary.json 全等"必须拿**真文件**验一次）
REAL_SUMMARY = os.path.join(BASE, "project-two", "logs", "probe_silent.summary.json")

FIXTURE = {
    "success": False,
    "stat": {
        "testcases": {"total": 4, "success": 2, "fail": 2},
        "teststeps": {"total": 6, "failures": 2, "successes": 4},
    },
    "time": {"duration": 383.3},
    "details": [
        {"name": "login_flow", "success": True, "time": {"duration": 1.766}},
        {"name": "order_create", "success": False, "time": {"duration": 2.040}},
        {"name": "order_pay", "success": False, "time": {"duration": 120.557}},
        {"name": "查询商品", "success": True, "time": {"duration": 3.992}},
    ],
}


class _TempWorkspace(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_summary_")
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class TestStatsMatchSummaryJson(unittest.TestCase):
    """★验收①：**数字与 `summary.json` 全等**（不是"差不多"，是逐字段相等）。"""

    def test_numbers_equal_the_stat_block(self):
        stats = collect_stats(FIXTURE)

        self.assertEqual((stats.total, stats.success, stats.fail), (4, 2, 2))
        self.assertEqual(
            (stats.steps_total, stats.steps_success, stats.steps_fail), (6, 4, 2)
        )
        self.assertAlmostEqual(stats.duration, 383.3, places=3)

    def test_numbers_equal_a_real_summary_json(self):
        """拿**真产物**对账：从文件读出来的数字，必须与 `stat` 块逐字相等。"""
        if not os.path.exists(REAL_SUMMARY):
            self.skipTest("真产物不在（project-two/logs/probe_silent.summary.json）")
        with open(REAL_SUMMARY, encoding="utf-8") as fp:
            raw = json.load(fp)

        stats = collect_stats(raw)
        cases = raw["stat"]["testcases"]
        steps = raw["stat"]["teststeps"]

        self.assertEqual(stats.total, cases["total"])
        self.assertEqual(stats.success, cases["success"])
        self.assertEqual(stats.fail, cases["fail"])
        self.assertEqual(stats.steps_total, steps["total"])
        self.assertEqual(stats.steps_success, steps["successes"])
        self.assertEqual(stats.steps_fail, steps["failures"])

    def test_missing_stat_block_falls_back_to_details(self):
        """`stat` 缺失时用 `details` 兜底——异常输入不该让摘要整个空掉。"""
        stats = collect_stats({"details": [{"name": "a"}, {"name": "b"}]})

        self.assertEqual(stats.total, 2)


class TestDistributionAndTopN(unittest.TestCase):
    """失败分布（按前缀）与耗时 TopN 的口径。"""

    def test_failures_are_grouped_by_prefix(self):
        stats = collect_stats(FIXTURE)

        self.assertEqual(stats.failures_by_prefix, (("order", 2),))

    def test_prefix_definition(self):
        """前缀口径：第一个 `_` 之前；没有 `_` 则**各自成组**（不硬凑）。"""
        self.assertEqual(case_prefix("order_pay"), "order")
        self.assertEqual(case_prefix("查询商品"), "查询商品")
        self.assertEqual(case_prefix("_leading"), UNGROUPED)

    def test_names_without_underscore_are_not_lumped_together(self):
        summary = {
            "details": [
                {"name": "甲", "success": False},
                {"name": "乙", "success": False},
            ]
        }

        stats = collect_stats(summary)

        self.assertEqual(len(stats.failures_by_prefix), 2, "没有 `_` 的名字不该被并成一组")

    def test_slowest_is_descending_and_ties_break_by_name(self):
        """★同分**按名字**定序：报告要能进 diff，排序就不能依赖输入顺序的偶然。"""
        summary = {
            "details": [
                {"name": "b_case", "time": {"duration": 5.0}},
                {"name": "a_case", "time": {"duration": 5.0}},
                {"name": "c_case", "time": {"duration": 9.0}},
            ]
        }

        names = [name for name, _ in collect_stats(summary).slowest]

        self.assertEqual(names, ["c_case", "a_case", "b_case"])

    def test_top_n_is_capped(self):
        summary = {"details": [{"name": f"c{i}", "time": {"duration": i}} for i in range(20)]}

        self.assertEqual(len(collect_stats(summary).slowest), TOP_N)


class TestLeadWhitelist(unittest.TestCase):
    """**导语数字白名单**（§5.4 的核心判据）。"""

    def setUp(self):
        self.stats = collect_stats(FIXTURE)

    def test_compliant_lead_is_adopted(self):
        good = "本次共 4 个用例，其中 2 个失败，集中在 order 前缀。"

        ok, why = verify_lead(good, self.stats)

        self.assertTrue(ok, why)

    def test_number_outside_template_is_discarded(self):
        ok, why = verify_lead("本次共 7 个用例失败。", self.stats)

        self.assertFalse(ok)
        self.assertIn("7", why)

    def test_topn_ordinal_counts_as_a_template_number(self):
        """★实测踩到的口径：`3` **是**模板里的数字——它是耗时 TopN 的**序号**。

        这条说明白名单只能**扫模板**，不能凭直觉列：凭直觉时，正是这种数会被误判成违规，
        结果合规导语被白白丢弃。
        """
        self.assertIn("3", allowed_numbers(self.stats))
        self.assertTrue(verify_lead("第 3 慢的用例超过了 2 秒。", self.stats)[0])

    def test_discarded_lead_is_absent_from_report(self):
        """★元护栏：**弃用必须落到正文**（只改状态、导语还在 = 没弃用）。"""
        bad = "本次共 7 个用例失败。"

        text, status = render_summary_report(self.stats, bad)

        self.assertNotIn(bad, text)
        self.assertIn("已弃用", status)

    def test_adopted_lead_appears_before_the_overview(self):
        good = "本次共 4 个用例，其中 2 个失败。"

        text, _status = render_summary_report(self.stats, good)

        self.assertIn(good, text)
        self.assertLess(text.index("## 导语"), text.index("## 总览"))


class TestNoLlmStillProducesReport(_TempWorkspace):
    """★验收②：**无 LLM 时模板版仍产出**（§5.4："报告永不缺席"）。"""

    def test_no_transport_still_writes_report(self):
        result = summarize_summary(FIXTURE, transport=None)

        self.assertEqual(result.report_path, "reports/summary.md")
        with open("reports/summary.md", encoding="utf-8") as fp:
            body = fp.read()
        self.assertIn("| 用例总数 | 4 |", body)
        self.assertIn("未提供", result.lead_status)

    def test_transport_failure_falls_back_to_template(self):
        """★断网 → **模板版**，不是异常（与 `gen`/`analyze` 故意不同）。"""
        result = summarize_summary(
            FIXTURE, transport=FakeTransport({"*": ""}, failures=99), config=CONFIG
        )

        self.assertTrue(os.path.exists("reports/summary.md"))
        self.assertIn("未生成", result.lead_status)

    def test_compliant_lead_from_the_model_is_adopted(self):
        lead = "本次共 4 个用例，2 个失败都集中在 order 前缀。"
        result = summarize_summary(
            FIXTURE, transport=FakeTransport({"*": "```\n" + lead + "\n```"}), config=CONFIG
        )

        self.assertEqual(result.lead, lead, "围栏要被剥掉")
        with open("reports/summary.md", encoding="utf-8") as fp:
            self.assertIn(lead, fp.read())

    def test_fabricated_lead_from_the_model_is_discarded(self):
        """★元护栏：模型编了模板外的数字 → 导语**不进报告**（而报告照出）。"""
        bad = "本次共 9 个用例失败。"
        result = summarize_summary(FIXTURE, transport=FakeTransport({"*": bad}), config=CONFIG)

        self.assertFalse(result.lead)
        with open("reports/summary.md", encoding="utf-8") as fp:
            self.assertNotIn(bad, fp.read())
        self.assertIn("已弃用", result.lead_status)

    def test_report_is_written_atomically(self):
        summarize_summary(FIXTURE, transport=None)

        self.assertEqual(os.listdir("reports"), ["summary.md"])

    def test_report_is_byte_identical_across_runs(self):
        summarize_summary(FIXTURE, transport=None)
        with open("reports/summary.md", encoding="utf-8") as fp:
            first = fp.read()
        os.remove("reports/summary.md")
        summarize_summary(FIXTURE, transport=None)
        with open("reports/summary.md", encoding="utf-8") as fp:
            self.assertEqual(first, fp.read())


class TestCliSummary(_TempWorkspace):
    """`haify summary` 的 CLI 契约。★缺模型配置**不是错误**。"""

    def _write_summary(self):
        with open("summary.json", "w", encoding="utf-8") as fp:
            json.dump(FIXTURE, fp, ensure_ascii=False)
        return "summary.json"

    def test_missing_summary_is_a_config_error(self):
        from interfacetester_ai.cli import EXIT_CONFIG, main  # noqa: PLC0415

        self.assertEqual(main(["summary", "nope.json", "--no-llm"]), EXIT_CONFIG)

    def test_no_llm_writes_a_report_and_exits_zero(self):
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        self.assertEqual(main(["summary", self._write_summary(), "--no-llm"]), EXIT_OK)
        self.assertTrue(os.path.exists("reports/summary.md"))

    def test_missing_model_config_is_not_an_error(self):
        """★与 `gen`/`analyze` 的分野：它们缺配置 → 2；摘要缺配置 → **照出报告、退出码 0**。"""
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415
        from interfacetester_ai.llm import LLMConfigMissing  # noqa: PLC0415

        path = self._write_summary()
        with mock.patch(
            "interfacetester_ai.cli.LLMConfig.from_env",
            side_effect=LLMConfigMissing("no key"),
        ):
            self.assertEqual(main(["summary", path]), EXIT_OK)

        with open("reports/summary.md", encoding="utf-8") as fp:
            self.assertIn("| 用例总数 | 4 |", fp.read())


class TestWriteBoundaryAndSelftest(unittest.TestCase):
    def test_summary_is_registered_as_a_reports_writer(self):
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        self.assertEqual(MODULE_WRITE_BOUNDARIES["summary"], ("roots", ("reports/",)))

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_the_whitelist_accepts_everything(self):
        """元护栏：白名单退化成"什么都放行" → 自检必红（否则白名单是装饰）。"""
        import interfacetester_ai.summary as summary_module  # noqa: PLC0415

        with mock.patch.object(summary_module, "verify_lead", lambda *a, **k: (True, "")):
            self.assertNotEqual(summary_module.run_selftest(), 0)


if __name__ == "__main__":
    unittest.main()


