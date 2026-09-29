# -*- coding: utf-8 -*-
"""文档体检闸（S-10 / T27）的护栏用例。

## 为什么需要这个文件

"文档不准 → 用例不准"这条链上，**事前**那一环就是本闸门（§9.2 R15/R16 的配套）。
它最容易坏的方式**不是漏判，而是误伤**：体检闸一旦把能生成的文档判死，用户会直接
关掉它——那等于没有（本仓口径："假报会让真报被无视"，`comparators.py`）。

所以本文件的判据是**成对**的：

- **违规必红**：缺 URL/method、缺字段表、同字段两种类型、示例与字段表冲突、
  0 字节文档（复用 S11）→ 必须拒；
- **合规必绿**：健康文档 0 finding、**只缺可选要素（示例）→ 放行**、
  非接口切片（概述/变更记录）不误伤、BOM/HTML 残留/表格残缺只提示不拒。

## 夹具独立于实现

本文件**自带**文档夹具（不复用 `doc_quality.GOOD_DOC`）——共用夹具会让
"夹具本身写错"这件事永远发现不了（判据同 §3.5 的元护栏取向）。

## 元护栏

`test_injected_missing_element_turns_green_to_reject`：把健康文档的 URL/method 行删掉，
必须从"放行"变"拒"——否则"全绿"可能只是体检没在比对要素。
"""

import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.doc_quality import (  # noqa: E402
    D1_INCOMPLETE,
    D2_CONTRADICTION,
    D3_ADJUDICABILITY,
    D4_ENCODING,
    NEEDS_DOC_FIX_REL_PATH,
    audit_document,
    audit_text,
    parse_field_tables,
    render_needs_doc_fix,
    split_slices,
    write_needs_doc_fix,
)

DOC = (
    "# 创建订单\n"
    "\n"
    "请求：`POST /api/order`\n"
    "\n"
    "状态码：成功返回 200，参数错误返回 400。\n"
    "\n"
    "## 请求字段\n"
    "\n"
    "| 字段 | 类型 | 必填 | 说明 |\n"
    "| --- | --- | --- | --- |\n"
    "| userId | int | 是 | 用户 ID |\n"
    "| amount | number | 是 | 金额，枚举：1=全额 2=部分 |\n"
    "\n"
    "## 响应示例\n"
    "\n"
    "```json\n"
    '{"userId": 7, "amount": 1}\n'
    "```\n"
)


class TestElementCompleteness(unittest.TestCase):
    """D1 要素完整性：必需项缺 → 拒；可选要素缺 → 只提示。"""

    def test_healthy_document_has_zero_findings(self):
        rep = audit_text(DOC, "good.md")
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(rep.findings, [], rep.to_dict())

    def test_missing_url_and_method_is_rejected(self):
        text = DOC.replace("`POST /api/order`", "创建订单接口")
        rep = audit_text(text, "bad.md")
        self.assertFalse(rep.ok)
        self.assertIn(D1_INCOMPLETE, rep.codes())
        msgs = " ".join(f.message for f in rep.rejects)
        self.assertIn("URL", msgs)
        self.assertIn("method", msgs)

    def test_missing_field_table_is_rejected(self):
        text = "# 创建订单\n\n请求：`POST /api/order`\n\n状态码：成功返回 200。\n"
        rep = audit_text(text, "bad.md")
        self.assertFalse(rep.ok)
        self.assertIn("字段表", " ".join(f.message for f in rep.rejects))

    def test_missing_optional_example_only_warns(self):
        # 去掉示例**小节**（连标题一起）——只留必需要素：必须放行，且提示"无示例"
        text = DOC.replace("## 响应示例\n\n", "").split("```json")[0]
        rep = audit_text(text, "warn.md")
        self.assertTrue(rep.ok, f"只缺可选要素却拒了：{rep.to_dict()}")
        self.assertTrue(any("示例" in f.message for f in rep.warns))

    def test_non_interface_slice_is_not_rejected(self):
        # 只有概述、没有任何接口要素的文档 → 不该被 D1 必需项拒（它根本不是接口文档）
        rep = audit_text("# 变更记录\n\n- 2026-09-23 初版\n", "changelog.md")
        self.assertTrue(rep.ok)


class TestContradictions(unittest.TestCase):
    """D2 内部矛盾：矛盾输入必然产出矛盾用例 → 拒。"""

    def test_same_field_two_types_is_rejected(self):
        text = (
            "# 订单\n\n请求：`POST /api/order`\n\n"
            "| 字段 | 类型 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| status | int | 是 | 状态 |\n"
            "| status | string | 否 | 状态（第二处） |\n"
        )
        rep = audit_text(text, "conflict.md")
        self.assertFalse(rep.ok)
        self.assertIn(D2_CONTRADICTION, rep.codes())

    def test_example_conflicting_with_field_table_is_rejected(self):
        text = DOC.replace('"amount": 1', '"amount": "many"')
        rep = audit_text(text, "conflict.md")
        self.assertFalse(rep.ok)
        self.assertIn(D2_CONTRADICTION, rep.codes())
        self.assertIn("示例", " ".join(f.where for f in rep.rejects))

    def test_consistent_example_passes(self):
        # 反向：把示例改成与字段表一致 → 无 D2
        text = DOC.replace('"amount": 1', '"amount": 12.5')
        rep = audit_text(text, "ok.md")
        self.assertTrue(rep.ok)
        self.assertNotIn(D2_CONTRADICTION, rep.codes())


class TestAdjudicability(unittest.TestCase):
    """D3 可判定率：只降级（建议"骨架 + unknowns"），绝不拒。"""

    def test_low_adjudicability_warns_but_passes(self):
        text = (
            "# 订单\n\n请求：`POST /api/order`\n\n"
            "| 字段 | 必填 | 说明 |\n| --- | --- | --- |\n"
            "| a | 是 | 无类型 |\n| b | 是 | 无类型 |\n"
        )
        rep = audit_text(text, "low.md")
        self.assertTrue(rep.ok, f"可判定率低不该被拒：{rep.to_dict()}")
        self.assertIn(D3_ADJUDICABILITY, rep.codes())


class TestEncodingAndParsing(unittest.TestCase):
    """D4 编码 / 可解析：坏编码与空文件复用 S11（拒）；BOM/HTML/表格残缺只提示。"""

    def _tmp(self, name, data):
        tmp = tempfile.mkdtemp(prefix="docq_test_")
        path = os.path.join(tmp, name)
        with open(path, "wb") as fp:
            fp.write(data)
        return path

    def test_empty_document_is_rejected_via_s11(self):
        rep = audit_document(self._tmp("empty.md", b""))
        self.assertFalse(rep.ok)
        self.assertIn(D4_ENCODING, rep.codes())
        self.assertIn("S11", " ".join(f.message for f in rep.rejects))

    def test_bom_document_warns_but_passes(self):
        body = DOC.encode("utf-8")
        rep = audit_document(self._tmp("bom.md", b"\xef\xbb\xbf" + body))
        self.assertTrue(rep.ok, f"带 BOM 的健康文档被判拒：{rep.to_dict()}")
        self.assertTrue(any("BOM" in f.message for f in rep.warns))

    def test_html_residue_warns(self):
        rep = audit_document(self._tmp("html.md", (DOC + "<br>&nbsp;\n").encode("utf-8")))
        self.assertTrue(rep.ok)
        self.assertTrue(any("HTML" in f.message for f in rep.warns))

    def test_broken_table_warns(self):
        text = DOC.replace(
            "## 请求字段\n", "## 请求字段\n",
        ).replace(
            "| amount | number | 是 | 金额，枚举：1=全额 2=部分 |",
            "| amount | number |",  # 该行列数不齐 → 表格残缺提示
        )
        rep = audit_text(text, "broken.md")
        self.assertTrue(any("表格列数不齐" in f.message for f in rep.warns))


class TestNeedsDocFixReflux(unittest.TestCase):
    """缺项清单：该拒才落盘 reports/NEEDS_DOC_FIX.md，且必须点名缺失项。"""

    def test_rejected_document_lands_needs_doc_fix(self):
        rep = audit_text(DOC.replace("`POST /api/order`", ""), "bad.md")
        old = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="docq_test_") as tmp:
            try:
                os.chdir(tmp)
                rel = write_needs_doc_fix(rep)
                self.assertEqual(rel, NEEDS_DOC_FIX_REL_PATH)
                self.assertTrue(rel.startswith("reports/"))
                body = open(rel, encoding="utf-8").read()
                self.assertIn("必改", body)
                self.assertIn("建议", body)
                self.assertIn("URL", body)  # 点名缺失项
            finally:
                os.chdir(old)

    def test_passing_document_writes_nothing(self):
        rep = audit_text(DOC, "good.md")
        old = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="docq_test_") as tmp:
            try:
                os.chdir(tmp)
                self.assertIsNone(write_needs_doc_fix(rep))
                self.assertFalse(os.path.exists("reports"), "放行却创建了 reports/")
            finally:
                os.chdir(old)

    def test_render_contains_sections(self):
        md = render_needs_doc_fix(audit_text(DOC.replace("`POST /api/order`", ""), "bad.md"))
        self.assertIn("## 一、必改", md)


class TestSlicingAndTableParsing(unittest.TestCase):
    """结构形态：切片按标题、字段表解析出字段/类型/必填。"""

    def test_split_slices_by_heading(self):
        names = [s.name for s in split_slices(DOC)]
        self.assertIn("创建订单", names)
        self.assertIn("请求字段", names)

    def test_parse_field_tables_reads_type_and_required(self):
        fields, broken = parse_field_tables(DOC)
        self.assertEqual(broken, [])
        by_name = {f.name: f for f in fields}
        self.assertEqual(by_name["userId"].type_text, "int")
        self.assertEqual(by_name["amount"].required_text, "是")


class TestCliExitCodes(unittest.TestCase):
    """CLI：放行 0 / 拒 2（`python -m interfacetester_ai.doc_quality <doc.md>`）。"""

    def _run(self, doc_text):
        tmp = tempfile.mkdtemp(prefix="docq_cli_")
        path = os.path.join(tmp, "doc.md")
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(doc_text)
        env = dict(os.environ)
        env["PYTHONPATH"] = BASE + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "interfacetester_ai.doc_quality", path],
            cwd=tmp,  # 拒时的 reports/ 落在临时目录，不污染工作区
            capture_output=True,
            env=env,
            timeout=120,
        )

    def test_exit_zero_on_good_doc(self):
        proc = self._run(DOC)
        self.assertEqual(
            proc.returncode, 0, proc.stdout.decode("utf-8", "replace")
        )
        self.assertIn("体检通过".encode("utf-8"), proc.stdout)

    def test_exit_two_on_bad_doc(self):
        proc = self._run(DOC.replace("`POST /api/order`", ""))
        self.assertEqual(proc.returncode, 2, proc.stdout.decode("utf-8", "replace"))
        self.assertIn("未通过".encode("utf-8"), proc.stdout)


class TestInjectedElementTurnsGreenToReject(unittest.TestCase):
    """元护栏：删掉 URL/method 行，必须从"放行"变"拒"（体检真的在比对要素）。"""

    def test_injected_missing_element_turns_green_to_reject(self):
        self.assertTrue(audit_text(DOC, "good.md").ok)
        injected = audit_text(DOC.replace("`POST /api/order`", ""), "injected.md")
        self.assertFalse(injected.ok)
        self.assertIn(D1_INCOMPLETE, injected.codes())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

