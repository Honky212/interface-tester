# -*- coding: utf-8 -*-
"""归因六分类与文档缺陷回流的护栏用例（v8 §5.3 / §十 T29）。

## 为什么需要这个文件

§5.3 的五分类里**没有"文档写错"这一格**（§9.2 的 R15）：文档缺陷导致的失败只能被
塞进「用例错误 / 真实缺陷」→ **文档永远不会被修**、同类错误下次继续污染。
T29 把分类扩到六类并让文档缺陷**回流**成 `reports/doc_defects.md`；
本文件钉住两件事：**分类不缩水**、**证据纪律不松动**。

## 判据（成对，同 `silent_traps_test.py` 的纪律）

- **违规必红**：分类枚举缺格（旧五分类）→ 对账必须报缺；`文档与实现不符` 缺
  文档侧或响应侧证据 → 降级；**编出来的引用**（非原文逐字子串）→ 降级；
  无证据 → 降级"未知待人工确认"；枚举外的类别 → 降级。
- **合规必绿**：双侧证据齐全的文档缺陷 → 保持；普通类别只要有证据（log 亦可）→ 保持；
  引用带 CRLF/ANSI 等噪声 → 归一化后仍命中（**不许因噪声误伤**）。
- **回流形态**：有文档缺陷 → 落 `reports/doc_defects.md`（根形态）；无 → **不落盘**。

## 元护栏（防"打偏"）

`test_evidence_check_actually_rejects_an_injected_fault` 把合规样本的响应引文**改一个字**，
必须从"保持"变"降级"——否则"全绿"可能只是校验函数没在比对原文。
"""

import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.diagnosis import (  # noqa: E402
    CAUSE_CASE_ERROR,
    CAUSE_DOC_DEFECT,
    CAUSE_ENV_ISSUE,
    CAUSE_REAL_DEFECT,
    CAUSE_TEST_DATA_ISSUE,
    CAUSE_TYPES,
    CAUSE_UNKNOWN,
    DOC_DEFECTS_REL_PATH,
    Diagnosis,
    normalize_diagnosis,
    normalize_text,
    render_analysis_section,
    render_doc_defects,
    validate_taxonomy,
    write_doc_defects,
)

DOC = (
    "## 创建订单\n"
    "| 字段 | 类型 | 说明 |\n"
    "| status | int | 订单状态，1=已支付 |\n"
)
RESP = 'HTTP/1.1 200 OK\r\n{"status": "paid", "order_id": 1001}\r\n'


def _diag(cause_type, quotes):
    return Diagnosis(
        case="order_create",
        step="create order",
        cause_type=cause_type,
        reason="文档写 status 是 int=1，实际返回字符串 paid",
        action="把文档的 status 类型与取值改为 string 枚举",
        confidence=0.9,
        evidence_quotes=quotes,
    )


DOC_QUOTE = {"source": "doc", "text": "| status | int | 订单状态，1=已支付 |"}
RESP_QUOTE = {"source": "response", "text": '"status": "paid"'}


class TestTaxonomy(unittest.TestCase):
    """六分类对账：缺格必红（R15 的机器形态）。"""

    def test_taxonomy_is_six_categories(self):
        self.assertEqual(len(CAUSE_TYPES), 6)
        self.assertEqual(validate_taxonomy(CAUSE_TYPES), [])

    def test_doc_defect_category_exists(self):
        self.assertIn(CAUSE_DOC_DEFECT, CAUSE_TYPES)
        self.assertEqual(CAUSE_TYPES.index(CAUSE_DOC_DEFECT), 4)  # 六分类第 5 格


class TestEvidenceDiscipline(unittest.TestCase):
    """§5.3 证据纪律：无证据必须"未知"，文档缺陷必须双侧证据。"""

    def test_doc_defect_with_both_sides_is_kept(self):
        d = normalize_diagnosis(_diag(CAUSE_DOC_DEFECT, [DOC_QUOTE, RESP_QUOTE]), DOC, RESP)
        self.assertEqual(d.cause_type, CAUSE_DOC_DEFECT)

    def test_doc_defect_without_response_side_degrades(self):
        d = normalize_diagnosis(_diag(CAUSE_DOC_DEFECT, [DOC_QUOTE]), DOC, RESP)
        self.assertEqual(d.cause_type, CAUSE_UNKNOWN)
        self.assertIn("双侧证据", d.reason)

    def test_doc_defect_without_doc_side_degrades(self):
        d = normalize_diagnosis(_diag(CAUSE_DOC_DEFECT, [RESP_QUOTE]), DOC, RESP)
        self.assertEqual(d.cause_type, CAUSE_UNKNOWN)

    def test_fabricated_quote_is_not_evidence(self):
        # "文档第 9 节说…" 这句在原文档里不存在 → 不算证据
        fabricated = {"source": "doc", "text": "文档第 9 节说明 status 是 int"}
        d = normalize_diagnosis(_diag(CAUSE_DOC_DEFECT, [fabricated, RESP_QUOTE]), DOC, RESP)
        self.assertEqual(d.cause_type, CAUSE_UNKNOWN)

    def test_no_evidence_degrades_to_unknown(self):
        for cause in (CAUSE_REAL_DEFECT, CAUSE_CASE_ERROR, CAUSE_ENV_ISSUE):
            with self.subTest(cause=cause):
                d = normalize_diagnosis(_diag(cause, []))
                self.assertEqual(d.cause_type, CAUSE_UNKNOWN)
                self.assertEqual(d.confidence, 0.0)

    def test_unknown_cause_type_degrades(self):
        d = normalize_diagnosis(_diag("文档看起来不太行", [DOC_QUOTE, RESP_QUOTE]), DOC, RESP)
        self.assertEqual(d.cause_type, CAUSE_UNKNOWN)

    def test_non_doc_category_keeps_with_log_evidence(self):
        d = normalize_diagnosis(_diag(CAUSE_CASE_ERROR, [{"source": "log", "text": "WARNING x"}]))
        self.assertEqual(d.cause_type, CAUSE_CASE_ERROR)

    def test_normalization_makes_crlf_and_ansi_irrelevant(self):
        # 引用里带 ANSI 色码 + 原文是 CRLF：归一化后仍应命中（不许因噪声误伤）
        noisy = {"source": "response", "text": '\x1b[31m"status": "paid"\x1b[0m'}
        d = normalize_diagnosis(_diag(CAUSE_DOC_DEFECT, [DOC_QUOTE, noisy]), DOC, RESP)
        self.assertEqual(d.cause_type, CAUSE_DOC_DEFECT)
        self.assertNotIn("\r", normalize_text(RESP))


class TestEvidenceCheckActuallyRejectsInjectedFault(unittest.TestCase):
    """元护栏：把合规样本的响应引文改一个字，必须从"保持"变"降级"。"""

    def test_injected_fault_is_detected(self):
        good = _diag(CAUSE_DOC_DEFECT, [DOC_QUOTE, RESP_QUOTE])
        self.assertEqual(
            normalize_diagnosis(good, DOC, RESP).cause_type, CAUSE_DOC_DEFECT
        )
        tampered = _diag(
            CAUSE_DOC_DEFECT,
            [DOC_QUOTE, {"source": "response", "text": '"status": "unpaid"'}],
        )
        self.assertEqual(
            normalize_diagnosis(tampered, DOC, RESP).cause_type, CAUSE_UNKNOWN
        )


class TestRenderAndReflux(unittest.TestCase):
    """渲染与回流清单（analysis 片段 + doc_defects.md）。"""

    def _good(self):
        return normalize_diagnosis(
            _diag(CAUSE_DOC_DEFECT, [DOC_QUOTE, RESP_QUOTE]), DOC, RESP
        )

    def test_analysis_section_lists_six_categories(self):
        section = render_analysis_section([self._good()])
        for cause in CAUSE_TYPES:
            with self.subTest(cause=cause):
                self.assertIn(cause, section)
        self.assertIn("归因分布（六分类）", section)

    def test_analysis_section_includes_doc_defect_quotes(self):
        section = render_analysis_section([self._good()])
        self.assertIn("文档原句", section)
        self.assertIn("实际响应", section)

    def test_doc_defects_markdown_contains_quote_and_action(self):
        md = render_doc_defects([self._good()])
        self.assertIn("订单状态，1=已支付", md)
        self.assertIn("建议改法", md)
        self.assertIn("回归输入", md)

    def test_write_doc_defects_lands_under_reports_root(self):
        old = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="diag_test_") as tmp:
            try:
                os.chdir(tmp)
                rel = write_doc_defects([self._good()])
                self.assertEqual(rel, DOC_DEFECTS_REL_PATH)
                self.assertTrue(rel.startswith("reports/"))
                self.assertTrue(os.path.isfile("reports/doc_defects.md"))
            finally:
                os.chdir(old)

    def test_no_doc_defect_writes_nothing(self):
        plain = normalize_diagnosis(_diag(CAUSE_CASE_ERROR, [{"source": "log", "text": "x"}]))
        old = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="diag_test_") as tmp:
            try:
                os.chdir(tmp)
                self.assertIsNone(write_doc_defects([plain]))
                self.assertFalse(os.path.exists("reports"), "无文档缺陷却创建了 reports/")
            finally:
                os.chdir(old)


class TestSelftestEntry(unittest.TestCase):
    """模块入口自检：重定向 + GBK locale 下退出码必须可靠（T26 纪律）。"""

    def test_selftest_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, "-m", "interfacetester_ai.diagnosis"],
            cwd=BASE,
            capture_output=True,
            timeout=120,
        )
        out = proc.stdout.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, out + proc.stderr.decode("utf-8", "replace"))
        self.assertIn("6 类齐全".encode("utf-8"), proc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


    def test_legacy_five_categories_are_detected_as_missing_one(self):
        legacy_five = [
            CAUSE_REAL_DEFECT,
            CAUSE_CASE_ERROR,
            CAUSE_ENV_ISSUE,
            CAUSE_TEST_DATA_ISSUE,
            CAUSE_UNKNOWN,
        ]
        self.assertEqual(validate_taxonomy(legacy_five), [CAUSE_DOC_DEFECT])

    def test_taxonomy_matches_v8_documentation(self):
        doc_path = os.path.join(BASE, "docs", "interfacetester智能化改造方案8.md")
        with open(doc_path, encoding="utf-8") as fp:
            lines = fp.readlines()
        rows = [l for l in lines if "归因维度" in l]
        self.assertTrue(rows, "文档里找不到「归因维度」行（§5.3）")
        joined = "".join(rows)
        for cause in CAUSE_TYPES:
            with self.subTest(cause=cause):
                self.assertIn(
                    cause, joined,
                    f"§5.3 的归因维度行未列类别「{cause}」——文档与代码漂移（判据 ↔ 实现对账）",
                )
