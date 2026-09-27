# -*- coding: utf-8 -*-
"""L2 格式化（black）显式化的护栏用例 —— v8 §3.4 / **S-8** / T13。

## 为什么需要这个文件

`make.format_pytest_with_black` 的 docstring 明写「超时/失败都**只告警**、不阻断 hmake」。
于是「L2 通过」既可能是**格式化成功**，也可能是**black 根本没跑**——
两者的正确性相同、风险不同，而「L2 通过率」这个指标会把它们混在一起（§0.3-⑦）。

S-8/T13 的处置是**把这件事显式化**：闸门报告里加 `formatting` 字段
（`{"ran": bool, "reason": ...}`）。本文件钉住三件事：

1. **判据成对**：四种「执行结局」（`ok`/`timeout`/`failed`/`missing_black`）逐条可判，
   且两种「没到 black」（`no_files`/`not_probed`）**不许**与 `missing_black` 混为一谈——
   前者的运维动作是"查调用链"，后者是"装依赖"；
2. **零副作用**：闸门**不自己探针**（`run_black` 是写盘操作），报告只携带调用方
   显式探针的结论（S-6 的零副作用口径）；
3. **元护栏**：把 `formatting` 从 `to_dict()` 里删掉，本文件的判据**必须变红**
   ——否则那一堆"全绿"可能只是"恰好绿"（本仓叫「护栏打偏」，见
   `docs/架构与调用链.md` §7 的自纠记录）。
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import interfacetester.make as kernel_make  # noqa: E402

from interfacetester_ai.gates import (  # noqa: E402
    FORMATTING_FAILED,
    FORMATTING_MISSING_BLACK,
    FORMATTING_NOT_EXECUTED_REASONS,
    FORMATTING_NOT_PROBED,
    FORMATTING_NO_FILES,
    FORMATTING_OK,
    FORMATTING_REASONS,
    FORMATTING_TIMEOUT,
    FORMATTING_UNKNOWN,
    GateReport,
    _formatting_failures,
    check_testcase,
    normalize_formatting,
    not_probed_formatting,
    probe_formatting,
)

# 真跑分支只在装了 black 时启用（本机未装 → 该条 skip，不留假绿）
_BLACK_AVAILABLE = (
    shutil.which("black") is not None
    or importlib.util.find_spec("black") is not None
)

# 文档 §3.4 的原规格四值（描述 black 的**执行结局**）
DOC_SPEC_REASONS = (
    FORMATTING_OK,
    FORMATTING_TIMEOUT,
    FORMATTING_FAILED,
    FORMATTING_MISSING_BLACK,
)


class TestFormattingStatusModel(unittest.TestCase):
    """`formatting` 的取值模型：原规格四值 + 实现补充三值，语义不许混。"""

    def test_reason_vocabulary_covers_doc_spec_plus_implementation_extras(self):
        self.assertEqual(len(FORMATTING_REASONS), 7)
        for reason in DOC_SPEC_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(reason, FORMATTING_REASONS)
        self.assertEqual(
            set(FORMATTING_NOT_EXECUTED_REASONS),
            {FORMATTING_NO_FILES, FORMATTING_NOT_PROBED, FORMATTING_UNKNOWN},
        )

    def test_not_probed_is_not_the_same_as_no_files(self):
        """「没人问过」与「没有产物可格式化」必须可分（否则没法判该查谁）。"""
        self.assertNotEqual(not_probed_formatting(), {"ran": False, "reason": FORMATTING_NO_FILES})
        self.assertEqual(not_probed_formatting(), {"ran": False, "reason": FORMATTING_NOT_PROBED})

    def test_valid_status_passes_through_unchanged(self):
        for reason in FORMATTING_REASONS:
            for ran in (True, False):
                with self.subTest(reason=reason, ran=ran):
                    status = {"ran": ran, "reason": reason}
                    self.assertEqual(normalize_formatting(status), status)

    def test_invalid_status_degrades_to_unknown_without_raising(self):
        """非法输入**不许**抛，也**不许**静默当成 ok（降级要留痕）。

        NOTICE：`{"reason": "ok"}`（只给 reason）**不**属非法——那是刻意允许的宽容形态，
        由 `test_missing_ran_is_inferred_from_reason` 覆盖；这里只列真正无法识别的输入。
        """
        for bad in (None, "ok", 42, ["ok"], {}, {"ran": True, "reason": "nope"}):
            with self.subTest(bad=bad):
                self.assertEqual(
                    normalize_formatting(bad), {"ran": False, "reason": FORMATTING_UNKNOWN}
                )

    def test_missing_ran_is_inferred_from_reason(self):
        """`ran` 缺失时按 reason 推：唯一 `ran=False` 的执行结局是 `missing_black`。"""
        for reason in FORMATTING_REASONS:
            with self.subTest(reason=reason):
                status = normalize_formatting({"reason": reason})
                expected_ran = reason not in FORMATTING_NOT_EXECUTED_REASONS and (
                    reason != FORMATTING_MISSING_BLACK
                )
                self.assertEqual(status["ran"], expected_ran)


class TestProbeFormatting(unittest.TestCase):
    """探针的四条分支与内核 `format_pytest_with_black` 的 except 分支逐条对应。"""

    def _probe_with(self, side_effect):
        with mock.patch.object(kernel_make, "run_black", side_effect=side_effect):
            return probe_formatting("a_test.py")

    def test_success_is_ok_and_ran(self):
        self.assertEqual(
            self._probe_with(None), {"ran": True, "reason": FORMATTING_OK}
        )

    def test_timeout_is_distinguishable_from_failed(self):
        """超时 ≠ 失败：处置不同（调超时上限 vs 修产物语法）。"""
        timeout_status = self._probe_with(
            subprocess.TimeoutExpired(cmd="black", timeout=1)
        )
        failed_status = self._probe_with(subprocess.CalledProcessError(1, "black"))

        self.assertEqual(timeout_status, {"ran": True, "reason": FORMATTING_TIMEOUT})
        self.assertEqual(failed_status, {"ran": True, "reason": FORMATTING_FAILED})
        self.assertNotEqual(timeout_status, failed_status)

    def test_missing_black_is_the_only_ran_false_execution_outcome(self):
        status = self._probe_with(OSError("no black"))
        self.assertEqual(status, {"ran": False, "reason": FORMATTING_MISSING_BLACK})

    def test_empty_input_returns_no_files_and_never_calls_black(self):
        with mock.patch.object(kernel_make, "run_black") as mocked_run:
            status = probe_formatting()

        mocked_run.assert_not_called()
        self.assertEqual(status, {"ran": False, "reason": FORMATTING_NO_FILES})

    def test_probe_reuses_kernel_timeout(self):
        """复用内核的 `get_black_timeout()`（不重造，也不写死 60）。"""
        calls = []

        def fake_run(timeout, *paths):
            calls.append((timeout, paths))

        with mock.patch.object(kernel_make, "run_black", side_effect=fake_run):
            with mock.patch.object(kernel_make, "get_black_timeout", return_value=7.5):
                probe_formatting("a_test.py", "b_test.py")

        self.assertEqual(calls, [(7.5, ("a_test.py", "b_test.py"))])


class TestGateReportCarriesFormatting(unittest.TestCase):
    """报告字段：默认 `not_probed`、挂载后透传、非法挂载降级（不抛）。"""

    def test_fresh_report_reports_not_probed(self):
        report = GateReport()

        self.assertIsNone(report.formatting)
        self.assertEqual(
            report.to_dict()["formatting"],
            {"ran": False, "reason": FORMATTING_NOT_PROBED},
        )

    def test_with_formatting_is_chainable_and_visible_in_payload(self):
        report = GateReport().with_formatting({"ran": True, "reason": FORMATTING_OK})

        self.assertIsInstance(report, GateReport)
        self.assertEqual(report.formatting_status(), {"ran": True, "reason": FORMATTING_OK})
        self.assertEqual(report.to_dict()["formatting"], {"ran": True, "reason": FORMATTING_OK})

    def test_garbage_status_degrades_instead_of_raising(self):
        report = GateReport().with_formatting("不是字典")

        self.assertEqual(
            report.to_dict()["formatting"],
            {"ran": False, "reason": FORMATTING_UNKNOWN},
        )

    def test_payload_keys_are_exactly_the_documented_set(self):
        """加字段不许悄悄动既有键（消费者已按老形态写）。"""
        self.assertEqual(
            set(GateReport().to_dict()),
            {"ok", "rejects", "pendings", "codes", "findings", "formatting"},
        )


class TestRealBlackProbe(unittest.TestCase):
    """真跑 black（装了才跑）：`ok` 可判 + 闸门**不自动**探针（零副作用）。"""

    @unittest.skipUnless(_BLACK_AVAILABLE, "本机没有 black，真跑分支跳过（不留假绿）")
    def test_real_run_is_ok_and_actually_formats_the_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "ugly_test.py")
            with open(path, "w", encoding="utf-8") as fp:
                fp.write("x=[1,2,3]\n")

            status = probe_formatting(path)

            self.assertEqual(status, {"ran": True, "reason": FORMATTING_OK})
            with open(path, encoding="utf-8") as fp:
                self.assertEqual(fp.read(), "x = [1, 2, 3]\n")

    def test_running_the_gate_does_not_probe_by_itself(self):
        """零副作用：跑闸门不会让报告带上"已探针"的结论（否则闸门会写盘）。"""
        report = check_testcase(
            {
                "name": "tiny",
                "request": {"method": "GET", "url": "/a"},
                "validate": [{"eq": ["status_code", 200]}],
            }
        )

        self.assertEqual(
            report.to_dict()["formatting"],
            {"ran": False, "reason": FORMATTING_NOT_PROBED},
        )


class TestMetaGuardrails(unittest.TestCase):
    """判据自检：这套断言真的在检查东西（防「护栏打偏」）。"""

    def test_criteria_are_green_on_the_real_module(self):
        self.assertEqual(_formatting_failures(), [])

    def test_criteria_turn_red_when_the_field_is_removed(self):
        original_to_dict = GateReport.to_dict

        def to_dict_without_formatting(self):
            payload = original_to_dict(self)
            payload.pop("formatting", None)
            return payload

        GateReport.to_dict = to_dict_without_formatting
        try:
            failures = _formatting_failures()
        finally:
            GateReport.to_dict = original_to_dict

        self.assertTrue(failures, "删掉 formatting 字段后判据竟然还是绿的（护栏打偏）")


class TestBenchShimExposesFormattingProbe(unittest.TestCase):
    """兼容壳要能拿到新入口（同一性，防双源漂移）。"""

    def test_shim_reexports_are_the_same_objects(self):
        import bench.silent_traps as shim  # noqa: PLC0415

        self.assertIs(shim.probe_formatting, probe_formatting)
        self.assertIs(shim.normalize_formatting, normalize_formatting)
