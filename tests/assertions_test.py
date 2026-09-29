# -*- coding: utf-8 -*-
"""断言补全的护栏用例 —— §5.2（**P0b**）。

## 核心判据：**在 15 份真 golden 文档上**跑

`assertions.run_selftest()` 用的是**内置小文档**（包的自检不该依赖目录结构）。
但 P0b 的验收说得很具体：**"10 份文档断言算子/字段合法性 100%（L2 全绿）+
伪存在性断言 0 条"**。所以这里逐份读 `bench/golden/*.md`（含 GBK 与 BOM 两份编码样本），
每份都：

1. **能推导出东西**（或明确说出"为什么没推导"——"什么都没说"是不允许的）；
2. 断言**全部**是白名单算子、引用句**逐字可溯源**；
3. 过一遍**真的装配链**（`assemble` → emit → `validate_emitted_case` = **L2 绿**）；
4. **降级条数为 0**（推导出来的断言该全部存活——被 T28 摘掉就说明我们推错了）；
5. **伪存在性断言 0 条**（S1 硬拦的形态）。

每一步都在**临时工作区**里跑（`cases/`、`.ai/`、`reports/` 都是相对根），不污染本仓。
"""

import glob
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai import assertions as assertions_module  # noqa: E402
from interfacetester_ai.assertions import (  # noqa: E402
    derive_assertions,
    find_doc_defects,
    has_todo,
    read_field_rows,
    request_payload_from_rows,
    run_selftest,
    to_draft_payload,
    todo_expect,
)
from interfacetester_ai.gen import (  # noqa: E402
    DOC_DEGRADATION,
    documented_base_urls,
    extract_endpoint,
    generate_assertions_only,
    generate_assertions_only_all,
    interface_sections,
)
from interfacetester_ai.normalize import _read_any_encoding  # noqa: E402
from interfacetester_ai.schema import parse_draft  # noqa: E402

GOLDEN_DIR = os.path.join(BASE, "bench", "golden")

# 伪存在性断言：S1 硬拦的形态（`not_equal: [x, ""]` / `contains: [x, ""]`）
PSEUDO_PRESENCE = (("not_equal", ""), ("not_equal", None), ("contains", ""), ("contains", None))


def _golden_docs():
    for path in sorted(glob.glob(os.path.join(GOLDEN_DIR, "*.md"))):
        yield os.path.basename(path), _read_any_encoding(path)


def _read(path):
    from interfacetester.loader import load_testcase_file  # noqa: PLC0415

    return load_testcase_file(path)


class _TempWorkspace(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_assert_")
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class TestEveryGoldenDocumentDerivesLegalAssertions(_TempWorkspace):
    """★P0b 的核心验收：15 份真文档，一份一份过。"""

    def test_no_document_is_silent(self):
        """每份文档要么有断言、要么有"为什么没有"——**不允许什么都没说**。"""
        for name, text in _golden_docs():
            with self.subTest(doc=name):
                derived, skipped = derive_assertions(text)
                self.assertTrue(
                    derived or skipped,
                    f"{name}：既没推导出断言，也没说明为什么（静默 = 最坏的一种）",
                )

    def test_no_pseudo_presence_assertion_anywhere(self):
        """★P0b 的硬指标：**伪存在性断言 0 条**（它会让用例静默通过）。"""
        offenders = []
        for name, text in _golden_docs():
            for item in derive_assertions(text)[0]:
                if (item.comparator, item.expect) in PSEUDO_PRESENCE:
                    offenders.append((name, item.to_dict()))
        self.assertEqual(offenders, [], f"生成了伪存在性断言：{offenders}")

    def test_all_comparators_are_whitelisted(self):
        from interfacetester.make import BUILTIN_COMPARATOR_NAMES  # noqa: PLC0415

        whitelist = set(BUILTIN_COMPARATOR_NAMES)
        offenders = []
        for name, text in _golden_docs():
            for item in derive_assertions(text)[0]:
                if item.comparator not in whitelist:
                    offenders.append((name, item.comparator))
        self.assertEqual(offenders, [], f"用了不在白名单里的算子（L2 会炸）：{offenders}")

    def test_all_quotes_are_traceable(self):
        """引用句必须逐字来自文档——不然装配器的 T28 会把断言全降级。"""
        offenders = []
        for name, text in _golden_docs():
            for item in derive_assertions(text)[0]:
                if item.source_quote and item.source_quote not in text:
                    offenders.append((name, item.source_quote))
        self.assertEqual(offenders, [], f"引用句不在文档里：{offenders}")

    def test_every_document_passes_the_real_assembly_chain(self):
        """★L2 绿：推导结果走**真的装配链**（emit + `validate_emitted_case`）不抛错。

        而且断言**降级条数必须为 0**——推导出来的断言该全部存活；
        被 T28 摘掉就说明**我们的映射推错了**（不是模型的错）。
        """
        failures = []
        for name, text in _golden_docs():
            method, url = extract_endpoint(text)
            if not url:
                continue  # 没有端点的文档（如纯变更记录）走不到装配
            with self.subTest(doc=name):
                try:
                    result = generate_assertions_only(text, case_name=name[:-3])
                except Exception as error:  # noqa: BLE001
                    failures.append((name, f"{type(error).__name__}: {error}"))
                    continue
                if result.coverage.dropped_assertions:
                    failures.append((name, f"有断言被降级：{result.coverage.dropped_assertions}"))
                # 产物若进了 cases/，必须过内核加载器
                if result.yaml_path:
                    _read(result.yaml_path)
        self.assertEqual(failures, [], f"装配链失败：{failures}")


class TestOptionalFieldsAreNotAsserted(_TempWorkspace):
    """**可选字段不做存在性/类型断言**（做了会在值不存在时误报失败）。"""

    DOC = """# 查询接口

## GET /api/thing

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| id | string | 是 | 主键 |
| verbose | string | 否 | 是否详细 |

**成功响应**：

```json
{"code": 0, "data": {"id": "x", "verbose": "y"}}
```
"""

    def test_optional_field_gets_no_assertion(self):
        derived, _skipped = derive_assertions(self.DOC)

        checks = {item.check for item in derived}
        self.assertIn("body.id", checks)
        self.assertNotIn("body.verbose", checks, "可选字段被断言了（会误报失败）")

    def test_optional_field_is_reported_in_skipped(self):
        """跳过也要**说出来**（"没断言什么"和"断言了什么"一样重要）。"""
        _derived, skipped = derive_assertions(self.DOC)

        self.assertTrue(any("body.verbose" in note for note in skipped), skipped)


class TestTodoPlaceholdersOnlyLandInDraft(_TempWorkspace):
    """`${ENV(TODO_...)}` 的两条硬规则（§5.2）。

    ★这里**直接构造**带 `TODO_` 的 draft，而不是走确定性映射——因为后者**不该**产生
    `TODO_`（实测：`type_match` 的 expect 必须是类型名，S7 会拦 `${ENV(...)}`；
    而"文档没说类型"的正确处置是**不断言**，不是塞占位符）。
    `TODO_` 的正当用途是"**值确实存在、只待人工填**"，那是**模型通路**的场合；
    本文件测的是"一旦出现 TODO_，落点规则是否被遵守"。
    """

    DOC = """# 下单接口

## POST /api/order

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| amount | integer | 是 | 金额，单位分 |
"""

    DRAFT = {
        "case_name": "带未定值",
        "steps": [
            {
                "name": "下单",
                "method": "post",
                "url": "/api/order",
                "validate": [
                    {
                        "comparator": "equal",
                        "check": "body.amount",
                        "expect": todo_expect("AMOUNT"),
                        "source_quote": "| amount | integer | 是 | 金额，单位分 |",
                    }
                ],
            }
        ],
    }

    def test_todo_mechanism_is_marked_needs_human(self):
        derived, _skipped = derive_assertions(self.DOC)
        self.assertTrue(derived)  # 这份文档能推出断言（对照组）

    def test_todo_case_never_lands_in_cases(self):
        """★§5.2 的硬规则：带 TODO_ 的用例**只许落 `.ai/draft/`**。"""
        from interfacetester_ai.assembler import assemble  # noqa: PLC0415

        draft = parse_draft(self.DRAFT)
        result = assemble(draft, self.DOC)

        self.assertTrue(result.draft_only)
        self.assertEqual(result.yaml_path, "", "带 TODO_ 的用例竟然进了 cases/")
        self.assertEqual(glob.glob("cases/*.yml"), [])
        self.assertTrue(glob.glob(".ai/draft/*.yml"), "草稿应当落在 .ai/draft/")

    def test_todo_case_registers_a_pending_list(self):
        """§5.2：`TODO_` 要登记 `<case>.pending.json`（交人工确认）。"""
        from interfacetester_ai.assembler import assemble  # noqa: PLC0415

        result = assemble(parse_draft(self.DRAFT), self.DOC)
        self.assertTrue(result.pending_path, result.to_dict())

        with open(result.pending_path, encoding="utf-8") as fp:
            payload = json.load(fp)
        self.assertGreaterEqual(payload["count"], 1)
        self.assertTrue(any(item["code"] == "TODO" for item in payload["items"]))


class TestReproducibility(_TempWorkspace):
    """确定性映射的卖点：**同一份文档两次推导，断言逐字节相同**（§5.2）。"""

    DOC = """# 下单接口

## POST /api/order

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| sku | string | 是 | 商品编码 |
| quantity | integer | 是 | 数量，1 ~ 99 |
"""

    def test_two_derivations_are_identical(self):
        first, _ = derive_assertions(self.DOC)
        second, _ = derive_assertions(self.DOC)

        self.assertEqual([item.to_dict() for item in first], [item.to_dict() for item in second])

    def test_two_assemblies_differ_only_in_the_case_name(self):
        one = generate_assertions_only(self.DOC, case_name="第一次")
        two = generate_assertions_only(self.DOC, case_name="第二次")

        def body(path):
            with open(path, encoding="utf-8") as fp:
                # ★注释行里也会写用例名（`# 用法：hrun <case>.yml`），所以按**名字**过滤，
                # 而不是只过滤 `name:`——后者会让注释里的差异被算进来（实测踩到）。
                return [
                    line
                    for line in fp.read().splitlines()
                    if "第一次" not in line and "第二次" not in line
                ]

        self.assertEqual(body(one.draft_yaml_path), body(two.draft_yaml_path))


class TestMetaGuardrails(unittest.TestCase):
    """判据自检：这套断言真的在检查东西。"""

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_the_optional_rule_is_neutered(self):
        """注入：让"可选字段也断言" → 自检必须变红。"""
        with mock.patch.object(assertions_module, "_truthy_required", lambda text: (True, False)):
            self.assertNotEqual(run_selftest(), 0, "可选字段判据失效后自检竟然还是绿的")

    def test_selftest_turns_red_when_response_note_reader_is_broken(self):
        """注入：让响应说明句推导不出东西 → 自检必须变红。"""
        with mock.patch.object(
            assertions_module, "derive_from_response_notes", lambda text: ([], [])
        ):
            self.assertNotEqual(run_selftest(), 0, "响应说明句判据失效后自检竟然还是绿的")


class TestCliAssertionsOnly(_TempWorkspace):
    """`haify gen --assertions-only` 的 CLI 契约。"""

    def test_cli_works_without_any_model_config(self):
        """★它**不需要** `BASE_URL/API_KEY`（确定性映射不调模型）——这是它最实用的地方。"""
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        with open("thing.md", "w", encoding="utf-8") as fp:
            fp.write(TestOptionalFieldsAreNotAsserted.DOC)

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["gen", "thing.md", "--assertions-only"]), EXIT_OK)

        self.assertTrue(glob.glob(".ai/draft/*.yml"), "草稿应当产出")
        self.assertEqual(
            glob.glob("cases/*.yml"), [], "★按 §九 第一档②，默认只落 `.ai/draft/`"
        )

    def test_cli_produces_one_draft_per_interface_section(self):
        """★§9.6：**逐片产出**——一份文档里两个接口 → **两条草稿**（各自端点、各自字段）。"""
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        with open("two.md", "w", encoding="utf-8") as fp:
            fp.write(TestSectionWiseAssertions.TWO_ENDPOINTS)

        with mock.patch.dict(os.environ, {}, clear=True):
            code = main(["gen", "two.md", "--assertions-only"])

        self.assertEqual(code, EXIT_OK)
        drafts = sorted(glob.glob(".ai/draft/*.yml"))
        self.assertEqual(len(drafts), 2, f"逐片应产出 2 条草稿，实为 {drafts}")
        self.assertTrue(any("alpha" in path for path in drafts))
        self.assertTrue(any("beta" in path for path in drafts))
        self.assertEqual(glob.glob("cases/*.yml"), [], "★逐片仍只落草稿区")

    def test_cli_writes_the_request_payload_into_the_draft(self):
        """★§9.16：产物里**真的带上请求参数**（`headers` / `json`），并且**请求头不混进请求体**。

        这一条是"生成 → 真跑"的关键：改造前 `request` 里只有 method/url，
        对真实服务来说是**空请求**（要么 400，要么测的不是那个场景）。
        """
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 头名称 | 必填 | 示例值 |\n| --- | --- | --- |\n"
            "| X-Path | 是 | /app1/ |\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| path | string | 是 | 路径 | /app1/documents |\n"
            "| depth | integer | 是 | 层数 | 2 |\n\n"
            "### 响应\n\n"
            "- `data.dirPath`：字符串，新目录路径\n"
        )
        with open("req.md", "w", encoding="utf-8") as fp:
            fp.write(doc)

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["gen", "req.md", "--assertions-only"]), EXIT_OK)

        drafts = glob.glob(".ai/draft/*.yml")
        self.assertEqual(len(drafts), 1, drafts)
        import yaml  # noqa: PLC0415

        with open(drafts[0], encoding="utf-8") as fp:
            case = yaml.safe_load(fp)
        request = case["teststeps"][0]["request"]

        self.assertEqual(request["headers"]["X-Path"], "/app1/")
        self.assertEqual(request["headers"]["Content-Type"], "application/json")
        self.assertEqual(request["json"], {"path": "/app1/documents", "depth": 2})
        self.assertNotIn("X-Path", request["json"], "请求头混进请求体了")

    def test_cli_writes_the_doc_defects_report_and_clears_it_when_fixed(self):
        """★§9.17 **文档缺陷回流**：冲突 → 落 `reports/doc_defects.md`；修好后再跑 → **清掉陈旧清单**。

        为什么必须验"清掉"：旧清单会与现状**自相矛盾**（文档已修好，清单还在说它有问题）——
        本仓为"陈旧留痕"专门吃过一次亏（`assembler.clear_stale_needs_human`）。
        """
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        with open("conflict.md", "w", encoding="utf-8") as fp:
            fp.write(TestResponseTableShapeConflict.CONFLICT_DOC)
        with mock.patch.dict(os.environ, {}, clear=True):
            main(["gen", "conflict.md", "--assertions-only"])

        self.assertTrue(os.path.isfile("reports/doc_defects.md"), "冲突文档没回流清单")
        text = open("reports/doc_defects.md", encoding="utf-8").read()
        self.assertIn("文档原句", text, "回流清单必须带文档原句（证据优先）")
        self.assertIn("sku", text)

        # 修好文档（示例改成对象形态）→ 再跑一次 → 陈旧清单必须消失
        fixed = TestResponseTableShapeConflict.CONFLICT_DOC.replace(
            '{"code": 0, "data": [{"sku": "SKU-0001", "price": 9900}]}',
            '{"code": 0, "data": {"list": [{"sku": "SKU-0001"}]}}',
        )
        with open("conflict.md", "w", encoding="utf-8") as fp:
            fp.write(fixed)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["gen", "conflict.md", "--assertions-only"]), EXIT_OK)

        self.assertFalse(
            os.path.isfile("reports/doc_defects.md"),
            "★文档修好后陈旧清单还在——它会与现状自相矛盾",
        )

    def test_cli_reports_a_refusal_when_nothing_is_derivable(self):
        """文档里没有可确定的东西 → **闸门 S3 拒**（退出码 1），而不是产出一条空白用例。

        ★这是**正确行为**：一条"零断言"的用例看起来像"通过"，实际什么都没验证
        （§3.3 的 S3 判据就是拦它）。CLI 要把这个结论说清楚，而不是悄悄成功。
        """
        from interfacetester_ai.cli import EXIT_CASE_FAILED, main  # noqa: PLC0415

        with open("empty.md", "w", encoding="utf-8") as fp:
            fp.write("# 变更记录\n\n- 2026-09-25：调整了错误码文案。\n")

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                main(["gen", "empty.md", "--assertions-only"]), EXIT_CASE_FAILED
            )

        self.assertEqual(glob.glob("cases/*.yml"), [], "零断言的用例不该进 cases/")


    def test_cli_writes_the_base_url_into_the_draft(self):
        """★`--base-url` → 写进产物的 `config.base_url`（**跑得起来**的必要条件）。

        实测（2026-09-26 打通"生成 → 真跑"）：产物里的 `url` 是**相对路径**，而内核
        `parser.build_url()` 要求 `base_url` 是**字面量 URL** —— 缺了它 `hrun` 第一句就报
        `ParamsError: base url missed!`；而 `base_url: ${ENV(BASE_URL)}` **不管用**（实测同报此错）。
        """
        import contextlib  # noqa: PLC0415
        import io as _io  # noqa: PLC0415

        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        with open("thing.md", "w", encoding="utf-8") as fp:
            fp.write(TestOptionalFieldsAreNotAsserted.DOC)

        out = _io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stdout(out):
                code = main(
                    ["gen", "thing.md", "--assertions-only", "--base-url", "http://127.0.0.1:8899"]
                )

        self.assertEqual(code, EXIT_OK)
        drafts = glob.glob(".ai/draft/*.yml")
        self.assertTrue(drafts, "草稿应当产出")
        text = open(drafts[0], encoding="utf-8").read()
        self.assertIn("base_url: http://127.0.0.1:8899", text)
        self.assertIn("已写进产物的", out.getvalue())
        self.assertNotIn("base url missed", out.getvalue(), "给了地址就不该再警告")

    def test_cli_says_loudly_when_the_base_url_is_missing(self):
        """★**成对判据**：不给地址时**必须响亮**（以前是静默的，于是"生成成功"与"跑得起来"之间
        隔着一个坑）。提示里还要给出**文档自己写着的服务地址**，好让人直接挑一个。"""
        import contextlib  # noqa: PLC0415
        import io as _io  # noqa: PLC0415

        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        doc = "# 接口文档\n\n## 1. 服务地址\n\n| 环境 | 基础URL |\n| --- | --- |\n| 开发环境 | `https://dev.example.com` |\n| 测试环境 | `https://tst.example.com` |\n\n" + (
            TestOptionalFieldsAreNotAsserted.DOC
        )
        with open("thing.md", "w", encoding="utf-8") as fp:
            fp.write(doc)

        out = _io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stdout(out):
                code = main(["gen", "thing.md", "--assertions-only"])

        self.assertEqual(code, EXIT_OK)
        text = out.getvalue()
        self.assertIn("base url missed", text, "必须点名跑起来会报什么错")
        self.assertIn("开发环境", text)
        self.assertIn("https://tst.example.com", text, "要把文档里的候选地址摊出来")

    def test_cli_prints_the_t28_drop_summary(self):
        """★(c)④-②：**降级率**要在生成期就可见（两条通路都要打）。

        实测动机：真模型在真实文档上 **40% 的断言被 T28 拦下**，而 golden 上是 0 降级 ——
        没有这个数字，"模型引用质量退化"只能靠人去 grep 原始草案才发现。
        """
        import contextlib  # noqa: PLC0415
        import io  # noqa: PLC0415

        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        with open("thing.md", "w", encoding="utf-8") as fp:
            fp.write(TestOptionalFieldsAreNotAsserted.DOC)

        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stdout(out):
                code = main(["gen", "thing.md", "--assertions-only"])

        self.assertEqual(code, EXIT_OK)
        text = out.getvalue()
        self.assertIn("T28 降级统计", text, f"降级率没印出来：{text[-400:]!r}")
        self.assertIn("=", text.split("T28 降级统计")[1][:40], "要给出比例（分子/分母 = 百分比）")


    def test_documented_base_urls_are_read_from_the_real_document(self):
        """真实客户文档 §1.1 写着三个环境 —— 它们要能被捞出来（CLI 提示就靠它）。"""
        path = os.path.join(BASE, "project-three", "统一文件服务平台-接入应用接口文档.md")
        if not os.path.isfile(path):
            self.skipTest("真实文档不在工作区里")
        with open(path, encoding="utf-8") as fp:
            found = documented_base_urls(fp.read())

        labels = [label for label, _url in found]
        urls = [url for _label, url in found]
        self.assertIn("开发环境", labels)
        self.assertIn("测试环境", labels)
        self.assertIn("https://unifsp-test.example.com", urls)

    def test_documented_base_urls_ignores_urls_outside_the_address_section(self):
        """★成对判据：**别处的** URL（curl 示例、错误码说明）不许混进候选 —— 那会让人挑错地址。"""
        doc = (
            "# 文档\n\n## 1. 服务地址\n\n| 环境 | 地址 |\n| --- | --- |\n"
            "| 测试 | `https://tst.example.com` |\n\n"
            "## 2. 接口\n\n```bash\ncurl -X POST https://host.example.com/api/x\n```\n"
        )

        found = documented_base_urls(doc)

        self.assertEqual([url for _label, url in found], ["https://tst.example.com"])


class TestDerivedAssertionsCarryRealLineNumbers(unittest.TestCase):
    """★C-2 的前置判据：`line_no` 必须**真的**指回文档里那一行（片级绑定靠它）。

    为什么不能只断言"`line_no > 0`"：**整体偏一行时它照样绿**——那是形式检查，
    挡不住 C-2 里"绑错片"的那种错。所以判据是"按行号取原文 → 必须与引用句互相包含"，
    外加一条**注入式**反向检查：故意偏一行必须对不上。
    """

    def test_every_golden_document_gets_real_line_numbers(self):
        offenders = []
        for name, text in _golden_docs():
            lines = text.split("\n")
            for item in derive_assertions(text)[0]:
                if item.line_no <= 0 or item.line_no > len(lines):
                    offenders.append((name, item.check, item.line_no, "行号缺失/越界"))
                    continue
                actual = lines[item.line_no - 1].strip()
                if item.source_quote not in actual and actual not in item.source_quote:
                    offenders.append(
                        (
                            name,
                            item.check,
                            item.line_no,
                            f"指向 {actual[:40]!r}，而引用句是 {item.source_quote[:40]!r}",
                        )
                    )
        self.assertEqual(offenders, [], f"行号指不回文档那一行（C-2 会绑错片）：{offenders}")

    def test_guard_actually_scanned_something(self):
        """判据自检：上一条必须**真的**扫到了断言，否则它是空跑。"""
        pairs = [(name, text) for name, text in _golden_docs()]
        derived = [item for _name, text in pairs for item in derive_assertions(text)[0]]
        self.assertGreater(len(derived), 0, "15 份 golden 推导出 0 条断言 —— 上一条判据等于没跑")
        self.assertTrue(
            all(item.line_no > 0 for item in derived),
            "有断言的 line_no 还是 0：说明行号没被盖上去（`derive_assertions` 的记账漏了）",
        )

    def test_off_by_one_would_be_caught(self):
        """元护栏：把行号故意偏一行 → "互相包含"必须对不上（否则判据没有牙）。"""
        caught = 0
        for _name, text in _golden_docs():
            lines = text.split("\n")
            for item in derive_assertions(text)[0]:
                if item.line_no <= 0 or item.line_no >= len(lines):
                    continue
                wrong = lines[item.line_no].strip()  # ← 故意看下一行
                if item.source_quote not in wrong and wrong not in item.source_quote:
                    caught += 1
        self.assertGreater(caught, 0, "偏一行竟然也能对上 —— 那条行号判据没有牙")


class TestSectionWiseAssertions(_TempWorkspace):
    """★§9.6：确定性通路改**逐片**（每接口段一条）+ **归属闸门**（段外断言丢弃且**可见**）。

    起因（真实客户文档实测）：整篇通路产出一条**打认证接口、却带着创建目录/文件上传字段**的
    用例，而它**通过了全部闸门**（`static_valid`）——"静默错位"。
    这里的判据都是**成对**的：既钉住"段外的不许进来"，也钉住"本段自己的不许被误丢"。
    """

    TWO_ENDPOINTS = """# 示例服务

## 1. 概述

本服务提供两个接口。

## POST /api/alpha

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| alphaName | string | 是 | 名称 |

### 响应

- `data.alphaName`：字符串，名称
- `data.alphaId`：整数，编号

## POST /api/beta

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| betaName | string | 是 | 名称 |

### 响应

- `data.betaName`：字符串，名称
- `data.betaId`：整数，编号
"""

    def test_two_endpoints_become_two_sections_with_own_spans(self):
        sections = interface_sections(self.TWO_ENDPOINTS)

        self.assertEqual([item.url for item in sections], ["/api/alpha", "/api/beta"])
        # ★跨度必须**含子标题**（字段表常挂在 `### 响应` 下）
        self.assertLess(sections[0].start_line, sections[0].end_line)
        self.assertLess(sections[0].end_line, sections[1].start_line)

    def test_parent_heading_is_not_counted_as_a_duplicate_section(self):
        """★父标题（`# 示例服务`）的跨度包含子段 → 只能保留**最具体**那段，否则会出重复用例。"""
        sections = interface_sections(self.TWO_ENDPOINTS)

        self.assertEqual(
            len(sections), 2, f"父标题被当成了额外的段：{[item.url for item in sections]}"
        )

    def test_table_form_endpoint_is_recognised(self):
        """真实文档把端点写在表格里（`| **URL** | \\`POST /api/x\\` |`）——原来一个都认不出。"""
        doc = (
            "# 服务\n\n### 1.1 创建\n\n"
            "| 属性 | 值 |\n| --- | --- |\n| **URL** | `POST /api/directory/create` |\n\n"
            "| 字段 | 类型 | 必填 |\n| --- | --- | --- |\n| path | String | 是 |\n"
        )

        self.assertEqual(extract_endpoint(doc), ("POST", "/api/directory/create"))
        self.assertEqual([item.url for item in interface_sections(doc)], ["/api/directory/create"])

    def test_out_of_section_assertions_are_dropped_and_visible(self):
        """★归属闸门：段外断言**不进本条用例**（成对：本段自己的必须留下），且**可见**。"""
        alpha, beta = generate_assertions_only_all(self.TWO_ENDPOINTS)

        self.assertEqual(set(alpha.coverage.covered_fields), {"alphaName", "alphaId"})
        self.assertEqual(set(beta.coverage.covered_fields), {"betaName", "betaId"})

        alpha_notes = [item for item in alpha.unknowns if item.kind == "out-of-section"]
        beta_notes = [item for item in beta.unknowns if item.kind == "out-of-section"]
        self.assertTrue(alpha_notes, "段外断言被**静默**丢掉了——必须写进 unknowns")
        self.assertTrue(beta_notes)
        self.assertIn("betaName", alpha_notes[0].message, "要**点名举例**，不能只给一个数字")
        self.assertIn("alphaName", beta_notes[0].message)

    def test_compat_wrapper_returns_the_first_section(self):
        """★兼容口径：`generate_assertions_only()` 现在返回**第一个接口段**（不再是整篇混装）。"""
        result = generate_assertions_only(self.TWO_ENDPOINTS, case_name="第一个")

        self.assertEqual(set(result.coverage.covered_fields), {"alphaName", "alphaId"})
        self.assertNotIn("betaName", set(result.coverage.covered_fields))


class TestResponseTableSchema(unittest.TestCase):
    """★口径 **B2**（§9.8）：**无「必填」列的字段表 = 响应数据表** → 一条 `jsonschema_match`。

    语义核心只有一句：**不写 `required`** —— 字段缺失**不算失败**、存在但类型错**才算**。
    所以本类里最要紧的判据就是"expect 里没有 required"，以及"类型不明确的不许猜进 schema"。
    """

    DOC = """# 示例服务

## POST /api/order

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| amount | integer | 是 | 金额 |

响应：

| 字段名 | 类型 | 说明 |
| --- | --- | --- |
| orderNo | String | 订单号 |
| status | String | 状态 |
| extra | - | 待定 |
"""

    def _schema_assertions(self, text):
        derived, skipped = derive_assertions(text)
        return [item for item in derived if item.comparator == "jsonschema_match"], skipped, derived

    def test_response_table_becomes_one_schema_assertion(self):
        schemas, _skipped, derived = self._schema_assertions(self.DOC)

        self.assertEqual(len(schemas), 1, "无必填列的响应表应当生成且只生成一条 schema 断言")
        item = schemas[0]
        self.assertEqual(item.check, "body")
        properties = item.expect["properties"]["data"]["properties"]
        self.assertEqual(sorted(properties), ["orderNo", "status"], "类型不明确的字段不该被猜进 schema")
        # ★§9.14 归属：请求表（有「必填」列）里的 `amount` **不许**被断言在响应 `body` 上——
        #   文档的响应侧只有 orderNo/status/extra，服务端不会回 `amount`（跑起来必红）。
        self.assertEqual(
            [one.check for one in derived if one.check == "body.amount"],
            [],
            "请求侧字段被当成响应字段断言了",
        )
        self.assertTrue(any("body.amount" in note for note in _skipped), _skipped)
        self.assertEqual(len(item.covered_fields), 2)

    def test_schema_never_requires_a_field(self):
        """★核心判据：**不写 `required`**——写了就变成"必须存在"，那是另一种误报。"""
        schemas, _skipped, _derived = self._schema_assertions(self.DOC)

        self.assertNotIn("required", json.dumps(schemas[0].expect), "schema 里不许出现 required")

    def test_schema_has_no_dollar_keys(self):
        """★`$` 前缀键会被内核在 hmake 阶段拦住（`$` 当变量解析）——我们自己产的必须干净。"""
        schemas, _skipped, _derived = self._schema_assertions(self.DOC)

        for item in schemas:
            self.assertNotIn("$", json.dumps(item.expect), "内联 schema 不许含 $ 键")

    def test_quote_is_the_whole_table(self):
        """引用句 = **整张表**（逐字）→ 字段名天然都在引用句里（T28 的①才判得过去）。"""
        schemas, _skipped, _derived = self._schema_assertions(self.DOC)
        quote = schemas[0].source_quote

        self.assertIn("orderNo", quote)
        self.assertIn("字段名", quote, "引用句应含表头行")
        self.assertIn(quote, self.DOC, "引用句必须逐字出现在文档里")

    def test_type_unknown_fields_are_reported_not_guessed(self):
        schemas, _skipped, _derived = self._schema_assertions(self.DOC)

        self.assertIn("类型不明确", schemas[0].reason)
        self.assertIn("extra", schemas[0].reason)

    def test_rows_no_longer_emit_the_conflicting_skip_note(self):
        r""""必填列没写清 → 不断言" 与 schema 覆盖**自相矛盾**：现在不该再出现那条说明。"""
        _schemas, skipped, _derived = self._schema_assertions(self.DOC)

        self.assertFalse(
            [text for text in skipped if "必填列没写清" in text],
            "响应表的字段行不该再说「没断言」——它们已被 schema 覆盖",
        )

    def test_all_unknown_types_means_no_schema_at_all(self):
        """整表类型都不明确 → **不生成**（空 schema 是 S1 的「伪存在性形态」，会被闸门拦）。"""
        doc = (
            "# 服务\n\n## GET /api/x\n\n| 字段名 | 类型 | 说明 |\n| --- | --- | --- |\n"
            "| a | - | 待定 |\n| b | number | 数值 |\n"
        )
        schemas, skipped, _derived = self._schema_assertions(doc)

        self.assertEqual(schemas, [])
        self.assertTrue([text for text in skipped if "整个整表" in text or "整表字段类型都不明确" in text])

    def test_error_code_table_is_not_a_field_table(self):
        """`错误码｜错误信息｜说明` 不是字段表（第一列不匹配字段列）→ 不许产出 schema。"""
        doc = (
            "# 服务\n\n## GET /api/x\n\n| 错误码 | 错误信息 | 说明 |\n| --- | --- | --- |\n"
            "| FILE_1001 | 文件列表不能为空 | 上传为空 |\n"
        )
        schemas, _skipped, _derived = self._schema_assertions(doc)

        self.assertEqual(schemas, [], "错误码表被误当响应字段表了")


class TestResponseTableAlwaysSentFields(unittest.TestCase):
    """★§9.31：**响应数据表**也能标「必带」→ 安全的 `required`（把 §9.30 的判据从 Cookie 表推广过来）。

    ★与上面那条"**不写 `required`**"并不矛盾 —— 差别就在**文档有没有建模**：

    | | 文档有没有建模"一定会给" | 该怎么断 |
    | --- | --- | --- |
    | 响应数据表（默认，见 `TestResponseTableSchema`） | **没有** | **不写** `required`（写了就是替文档假定 → 误报 ✗） |
    | 同一张表里标了「必带」 | **有**（明写） | **写** `required`（服务端不给 = 文档被违反，真缺陷 ✓） |

    ★两张**成对**的判据一起看才完整：`test_schema_never_requires_a_field`（没标 → 不许有 required）
    与下面的 `test_always_sent_field_goes_into_required_at_the_right_level`（标了 → 必须有）。
    """

    def _schema(self, doc):
        derived, skipped = derive_assertions(doc)
        schemas = [item for item in derived if item.comparator == "jsonschema_match"]
        return (schemas[0] if schemas else None), skipped

    def test_always_sent_field_goes_into_required_at_the_right_level(self):
        """★**层级**最要紧：裸字段名挂在 `data` 下 → `required` 必须在 **`data` 那一层**，不是根上。"""
        doc = (
            "# 示例服务\n\n## POST /api/file\n\n"
            "| 字段 | 类型 | 必带 | 说明 |\n| --- | --- | --- | --- |\n"
            "| fileName | string | 是 | 文件名 |\n"
            "| fileVersion | integer | 否 | 版本号 |\n"
        )
        schema = self._schema(doc)[0]

        self.assertEqual(schema.expect["properties"]["data"]["required"], ["fileName"])
        self.assertNotIn("required", schema.expect, "全挂到根上了（语义变成\"顶层必须有 fileName\" ✗）")
        self.assertIn("必带", schema.reason)

    def test_nested_path_gets_required_in_its_own_container(self):
        """全路径 `data.a.b` 标必带 → `required` 挂在 **`a` 那一层**（`b` 是它里面的字段）。"""
        doc = (
            "# 示例服务\n\n## POST /api/file\n\n"
            "| 字段 | 类型 | 必带 | 说明 |\n| --- | --- | --- | --- |\n"
            "| data.a.b | string | 是 | 深层字段 |\n"
        )
        schema = self._schema(doc)[0]

        self.assertEqual(
            schema.expect["properties"]["data"]["properties"]["a"]["required"], ["b"]
        )

    def test_marker_in_the_note_also_works(self):
        """不新立列：说明里写「**必带**」同样认。"""
        doc = (
            "# 示例服务\n\n## POST /api/file\n\n"
            "| 字段 | 类型 | 说明 |\n| --- | --- | --- |\n"
            "| fileName | string | 文件名，**必带** |\n"
        )
        schema = self._schema(doc)[0]

        self.assertEqual(schema.expect["properties"]["data"]["required"], ["fileName"])

    def test_always_sent_beats_an_unknown_type(self):
        """★必带与类型无关：**类型判不了也要断"有没有"**（所以必带那段在类型检查之前跑）。"""
        doc = (
            "# 示例服务\n\n## POST /api/file\n\n"
            "| 字段 | 类型 | 必带 | 说明 |\n| --- | --- | --- | --- |\n"
            "| bizNo | number | 是 | 类型不明确（number） |\n"
            "| fileName | string | 否 | 文件名 |\n"
        )
        schema = self._schema(doc)[0]

        self.assertEqual(schema.expect["properties"]["data"]["required"], ["bizNo"])
        self.assertNotIn("bizNo", schema.expect["properties"]["data"]["properties"])

    def test_always_sent_conflicting_with_conditional_return_is_not_forced(self):
        """★**矛盾输入不硬判**：同一行既说「必带」又说「仅当…时返回」→ 不写 `required` + 点名。"""
        doc = (
            "# 示例服务\n\n## POST /api/file\n\n"
            "| 字段 | 类型 | 必带 | 说明 |\n| --- | --- | --- | --- |\n"
            "| tag | string | 是 | 仅当 `withTag=true` 时才返回 |\n"
        )
        schema, skipped = self._schema(doc)

        self.assertNotIn("required", json.dumps(schema.expect))
        self.assertTrue(any("必带" in note and "条件返回" in note for note in skipped), skipped)

    def test_always_sent_column_left_blank_says_so(self):
        """★成对：有「必带」列却一行都没写清 → 不产 `required`，且**点名**。"""
        doc = (
            "# 示例服务\n\n## POST /api/file\n\n"
            "| 字段 | 类型 | 必带 | 说明 |\n| --- | --- | --- | --- |\n"
            "| fileName | string |  | 文件名 |\n"
        )
        schema, skipped = self._schema(doc)

        self.assertNotIn("required", json.dumps(schema.expect))
        self.assertTrue(any("必带" in note for note in skipped), skipped)

    def test_required_list_follows_the_document(self):
        """★元护栏：`required` 来自**文档**（加一行必带 → 列表跟着变，不是写死的）。"""

        def required_of(rows):
            doc = (
                "# 示例服务\n\n## POST /api/file\n\n"
                "| 字段 | 类型 | 必带 | 说明 |\n| --- | --- | --- | --- |\n" + rows
            )
            schema = self._schema(doc)[0]
            return schema.expect["properties"]["data"].get("required", [])

        self.assertEqual(required_of("| a | string | 是 | 甲 |\n"), ["a"])
        self.assertEqual(
            required_of("| a | string | 是 | 甲 |\n| b | integer | 是 | 乙 |\n"), ["a", "b"]
        )


class TestResponseCookieTables(unittest.TestCase):
    """★§9.29 **响应 Cookie 表 → 可查事实 + 有牙的断言**（登记项 §9.25 第 7 节 / §9.28 第 5 节）。

    背景：内核**一直**支持 `cookies.*`（`response.py`：`resp.cookies.get_dict()` → **字符串字典**），
    缺的是"文档里怎么声明响应 cookie"这一环。

    ★**一轮自查纠正（值得记）**：最初写的是"一条 `jsonschema_match` 断言 cookie 类型是 string"——
    但 cookie 值在内核里**恒为字符串**（HTTP 协议如此）→ 那条断言**永远为真、零信息量** ✗，
    正是本仓最反对的"看起来在断言、其实什么都没判"。
    现在的口径：**只在文档给出可判据事实时产断言**（固定值 → `equal`；枚举 → `contained_by`），
    都没有 → **认下事实 + 点名**，**不硬凑** ✓。
    """

    def test_fixed_value_cookie_becomes_an_equal_assertion(self):
        """说明列标**固定值** + 值列给了值 → `equal: [cookies.<名>, <值>]`（**有牙**）。"""
        doc = (
            "# 示例服务\n\n## POST /api/session\n\n"
            "| Cookie | 值 | 说明 |\n| --- | --- | --- |\n"
            "| sessionId | sess-1 | **固定值**：登录成功后总是它 |\n"
        )
        result, _skipped = derive_assertions(doc)
        found = [item for item in result if item.check == "cookies.sessionId"]

        self.assertEqual(len(found), 1, [item.check for item in result])
        self.assertEqual(found[0].comparator, "equal")
        self.assertEqual(found[0].expect, "sess-1")

    def test_enum_cookie_becomes_contained_by_with_strings(self):
        """说明列给**枚举** → `contained_by`，取值按**字符串**（内核里 cookie 值恒为字符串）。"""
        doc = (
            "# 示例服务\n\n## POST /api/session\n\n"
            "| Cookie | 值 | 说明 |\n| --- | --- | --- |\n"
            "| variant | - | 取值：A / B |\n"
        )
        result, _skipped = derive_assertions(doc)
        found = [item for item in result if item.check == "cookies.variant"]

        self.assertEqual(found[0].comparator, "contained_by")
        self.assertEqual(found[0].expect, ["A", "B"])

    def test_cookie_without_a_decidable_fact_is_named_not_asserted(self):
        """★成对（本仓核心纪律）：声明了 cookie 但**没给可判据事实** → **不产断言**，只点名。"""
        doc = (
            "# 示例服务\n\n## POST /api/session\n\n"
            "| Cookie | 说明 |\n| --- | --- |\n"
            "| theme | 主题偏好（可选返回） |\n"
        )
        result, skipped = derive_assertions(doc)

        self.assertFalse([item for item in result if item.check.startswith("cookies.")])
        self.assertTrue(any("theme" in note for note in skipped), skipped)

    def test_fixed_value_marked_but_no_value_is_not_invented(self):
        """★成对：标了固定值**却没给值** → 不编一个值出来（点名，交人工）。"""
        doc = (
            "# 示例服务\n\n## POST /api/session\n\n"
            "| Cookie | 说明 |\n| --- | --- |\n"
            "| sessionId | **固定值**：见环境文档 |\n"
        )
        result, skipped = derive_assertions(doc)

        self.assertFalse([item for item in result if item.check.startswith("cookies.")])
        self.assertTrue(any("sessionId" in note for note in skipped), skipped)

    def test_cookie_table_with_a_required_column_is_request_side(self):
        """★成对：带「必填」列的 Cookie 表是**请求**侧（要发出去的）→ 不产响应 cookie 断言，并点名。"""
        doc = (
            "# 示例服务\n\n## POST /api/session\n\n"
            "| Cookie | 值 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| sessionId | abc | 是 | 会话标识 |\n"
        )
        result, skipped = derive_assertions(doc)

        self.assertFalse([item for item in result if item.check.startswith("cookies.")])
        self.assertTrue(any("请求" in note for note in skipped), skipped)

    def test_request_cookie_header_is_still_a_credential(self):
        """★成对：请求头表里的 `Cookie` 头 = **凭据**（`${ENV(COOKIE)}`），不是响应 cookie 声明。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 头 | 必填 | 说明 |\n| --- | --- | --- |\n"
            "| Cookie | 是 | 会话 cookie |\n"
        )
        rows = read_field_rows(doc)
        payload, _notes = request_payload_from_rows(
            rows, doc, start_line=1, end_line=len(doc.split("\n")), method="POST"
        )
        result, _skipped = derive_assertions(doc)

        self.assertEqual(payload["headers"]["Cookie"], "${ENV(COOKIE)}")
        self.assertFalse([item for item in result if item.check.startswith("cookies.")])

    def test_always_sent_column_becomes_required(self):
        """★§9.30：说明「**必带**」→ `jsonschema_match` 的 `required`（服务端不给就是文档被违反）。"""
        doc = (
            "# 示例服务\n\n## POST /api/session\n\n"
            "| Cookie | 必带 | 说明 |\n| --- | --- | --- |\n"
            "| sessionId | 是 | 会话标识 |\n"
            "| theme | 否 | 主题偏好 |\n"
        )
        result, _skipped = derive_assertions(doc)
        schemas = [item for item in result if item.comparator == "jsonschema_match"]

        self.assertEqual(len(schemas), 1, [item.comparator for item in result])
        self.assertEqual(schemas[0].check, "cookies")
        self.assertEqual(schemas[0].expect["required"], ["sessionId"], "必带列表里混进了非必带的")

    def test_always_sent_marker_in_the_note_also_works(self):
        """说明列里写「**必带**」（不新立列）同样认。"""
        doc = (
            "# 示例服务\n\n## POST /api/session\n\n"
            "| Cookie | 说明 |\n| --- | --- |\n"
            "| sessionId | 会话标识，**必带** |\n"
        )
        result, _skipped = derive_assertions(doc)
        schemas = [item for item in result if item.comparator == "jsonschema_match"]

        self.assertEqual(schemas[0].expect["required"], ["sessionId"])

    def test_coexisting_always_sent_and_fixed_value_give_two_assertions(self):
        """必带 + 固定值 → **两条**断言（一条 presence、一条取值），各查各的。"""
        doc = (
            "# 示例服务\n\n## POST /api/session\n\n"
            "| Cookie | 值 | 必带 | 说明 |\n| --- | --- | --- | --- |\n"
            "| sessionId | sess-1 | 是 | **固定值**：登录后总是它 |\n"
        )
        result, _skipped = derive_assertions(doc)

        self.assertTrue([item for item in result if item.comparator == "equal"])
        self.assertTrue([item for item in result if item.comparator == "jsonschema_match"])

    def test_without_a_marker_there_is_no_required(self):
        """★成对（**不误报**）：没标必带 → **不产** `required`（响应侧默认"有就查、没有不算失败"）。"""
        doc = (
            "# 示例服务\n\n## POST /api/session\n\n"
            "| Cookie | 值 | 说明 |\n| --- | --- | --- |\n"
            "| theme | - | 主题偏好（可选返回） |\n"
        )
        result, _skipped = derive_assertions(doc)

        self.assertFalse([item for item in result if item.comparator == "jsonschema_match"])

    def test_always_sent_column_left_blank_says_so(self):
        """★成对：有「必带」列却**一行都没写清** → 不产 `required`，且**点名**（不替文档假定）。"""
        doc = (
            "# 示例服务\n\n## POST /api/session\n\n"
            "| Cookie | 必带 | 说明 |\n| --- | --- | --- |\n"
            "| sessionId |  | 会话标识 |\n"
        )
        result, skipped = derive_assertions(doc)

        self.assertFalse([item for item in result if item.comparator == "jsonschema_match"])
        self.assertTrue(any("必带" in note for note in skipped), skipped)

    def test_required_list_follows_the_document(self):
        """★元护栏：`required` 列表来自**文档**（加一行必带 → 列表跟着变，不是写死的）。"""

        def required_of(rows):
            doc = (
                "# 示例服务\n\n## POST /api/session\n\n"
                "| Cookie | 必带 | 说明 |\n| --- | --- | --- |\n" + rows
            )
            result, _skipped = derive_assertions(doc)
            schemas = [item for item in result if item.comparator == "jsonschema_match"]
            return schemas[0].expect["required"] if schemas else []

        self.assertEqual(required_of("| sessionId | 是 | 会话 |\n"), ["sessionId"])
        self.assertEqual(
            required_of("| sessionId | 是 | 会话 |\n| csrf | 是 | 防跨站 |\n"),
            ["sessionId", "csrf"],
        )

    def test_only_a_cookie_first_column_counts(self):
        """★边界（元护栏）：判据就是**首列** —— 换成「字段」就不再是 Cookie 表（也不产 cookies 断言）。"""
        doc = (
            "# 示例服务\n\n## POST /api/session\n\n"
            "| 字段 | 类型 | 说明 |\n| --- | --- | --- |\n"
            "| sessionId | string | 固定值 |\n"
        )
        result, _skipped = derive_assertions(doc)

        self.assertFalse([item for item in result if item.check.startswith("cookies.")])



class TestResponseTableSchemaEndToEnd(_TempWorkspace):
    """B2 的端到端：覆盖矩阵按**叶子字段**展开、分母是**本段**的字段数。"""

    def test_coverage_expands_leaf_fields_and_excludes_the_container(self):
        results = generate_assertions_only_all(TestResponseTableSchema.DOC)

        self.assertEqual(len(results), 1)
        covered = set(results[0].coverage.covered_fields)
        self.assertIn("orderNo", covered)
        # ★§9.14 归属的**成对判据**：响应侧字段（orderNo/status）算覆盖，
        #   而**只写在请求侧**的 `amount` 不算——它一个字都不该进覆盖矩阵
        #   （算进去就是"虚高的覆盖率"，正是本仓最忌讳的那种好看数字）。
        self.assertIn("status", covered)
        self.assertNotIn("amount", covered, "请求侧字段不该被算成已覆盖")
        self.assertNotIn("data", covered, "容器层 `data` 不是字段（算进去会虚高）")
        self.assertNotIn("body", covered, "schema 断言的 check=`body` 不是字段")
        self.assertEqual(
            len(results[0].coverage.documented_fields), 4, "分母应当是**本段**声明的字段数"
        )


class TestConditionalReturnAndColumnSynonyms(unittest.TestCase):
    """★§九 第一档④ 的两处**实证**缺陷：条件返回字段被无条件断言、枚举覆盖不一致。

    两条都来自评审 §四 P1 的抽检记录（`avatarUrl` / `status`）。它们的共同形态是
    **不会报错的错**：一条是**断言多了**（字段不出现时误报失败），
    一条是**断言少了**（少一条 `contained_by`，静默降低覆盖）。
    所以这里的判据都是**成对**的：既要挡住错的那一侧，也要证明对的那一侧没被误伤。
    """

    CONDITIONAL_DOC = """# 用户档案

## GET /api/user/profile

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| userId | string | 是 | 用户 ID |

- `data.nickname`：字符串，昵称
- `data.avatarUrl`：字符串，仅当 `withAvatar=true` 时返回
"""

    def test_conditional_field_is_not_asserted(self):
        """★缺陷一：`仅当 … 时返回` 的字段**不许**被无条件断言。"""
        derived, skipped = derive_assertions(self.CONDITIONAL_DOC)
        checks = {item.check for item in derived}

        self.assertIn("body.data.nickname", checks, "普通说明句该照常推出")
        self.assertNotIn(
            "body.data.avatarUrl",
            checks,
            "★条件返回字段被无条件断言了 —— 条件不满足时字段不出现，它会误报失败",
        )
        self.assertTrue(
            any("avatarUrl" in item and "条件返回" in item for item in skipped),
            "跳过必须**说明原因**（静默不生成是本仓最忌讳的形态）",
        )

    def test_plain_response_note_still_derives(self):
        """★成对判据：别把"响应说明句"整类挡掉——只有**写了条件**的才跳过。"""
        derived, _skipped = derive_assertions("- `data.total`：整数，总条数")

        self.assertEqual([item.check for item in derived], ["body.data.total"])

    def test_conditional_field_in_table_is_also_skipped(self):
        """字段表里的条件列同样不放过（不能只堵住说明句那一路）。"""
        doc = (
            "# 用户档案\n\n## GET /api/user/profile\n\n"
            "| 字段 | 类型 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| avatarUrl | string | 是 | 仅当 withAvatar=true 时返回 |\n"
        )
        derived, skipped = derive_assertions(doc)

        self.assertEqual(
            [item.check for item in derived],
            [],
            "★字段表里写了条件，却照样断言了存在性/类型",
        )
        self.assertTrue(any("条件返回" in item for item in skipped))

    def test_field_table_synonym_columns_are_equivalent(self):
        """★缺陷二：同一句枚举，列头 `说明` 与 `取值范围`/`允许值`/`约束` 必须**等价**。"""
        template = (
            "# 下单\n\n## POST /api/order\n\n"
            "| 字段 | 类型 | 必填 | {header} |\n| --- | --- | --- | --- |\n"
            "| status | string | 是 | 枚举：pending / paid / cancelled |\n"
        )
        got = {}
        for header in ("说明", "取值范围", "允许值", "约束"):
            with self.subTest(header=header):
                enums = [
                    item
                    for item in derive_assertions(template.format(header=header))[0]
                    if item.comparator == "contained_by"
                ]
                self.assertEqual(len(enums), 1, f"列头 {header} 没推出枚举断言")
                got[header] = tuple(enums[0].expect)

        self.assertEqual(
            len(set(got.values())),
            1,
            f"同一表达形式得到了不同的取值集合（这正是改造前的不一致）：{got}",
        )

    def test_pointer_enum_is_still_refused(self):
        """★反向：`枚举：见错误码表`（值没给全）仍**不许**生成 `contained_by`。"""
        doc = (
            "# 下单\n\n## POST /api/order\n\n"
            "| 字段 | 类型 | 必填 | 取值范围 |\n| --- | --- | --- | --- |\n"
            "| code | string | 是 | 枚举：见错误码表 |\n"
        )
        derived, skipped = derive_assertions(doc)

        self.assertEqual(
            [item.comparator for item in derived if item.comparator == "contained_by"], []
        )
        self.assertTrue(any("没给全" in item for item in skipped))

    def test_golden_regression_pairs(self):
        """★回归（评审 §四 点名的两条真样本）+ ★§9.14/§9.32 归属的**成对判据**：

        - `order_create.md` 的 `body.status`（列头 **`取值范围`**）→ 必须**有**枚举断言
          （它在**响应侧**有据：成功响应示例的**顶层**就有 `status`）。
          ★§9.32 起归属按**路径**判：请求表里的 `orderId` 与响应 `data.orderId` **末段撞名**时，
          `body.orderId` 会被摘掉（取错层级必红）—— 所以那份 golden 把请求侧改名为 `clientOrderId`。
        - `webhook_notify.md` 的 `body.event`（列头 `说明`）→ **不再**产枚举断言：
          `event` 只写在**请求体**里（那是"我们要发出去的事件名"），而该接口的成功响应
          只有 `delivered`/`attempt` —— 断言 `body.event` 对任何照文档实现的服务都必红。
          取值集合本身仍要在**跳过说明**里可见（"没断言什么"也要能看见）。
        """
        with open(os.path.join(GOLDEN_DIR, "order_create.md"), encoding="utf-8") as fp:
            order_text = fp.read()
        hit = [
            item
            for item in derive_assertions(order_text)[0]
            if item.comparator == "contained_by" and item.check == "body.status"
        ]
        self.assertEqual(
            len(hit), 1, "order_create 的 body.status 仍没生成唯一的 contained_by"
        )
        self.assertEqual(tuple(hit[0].expect), ("pending", "paid", "cancelled"))

        with open(os.path.join(GOLDEN_DIR, "webhook_notify.md"), encoding="utf-8") as fp:
            webhook_text = fp.read()
        derived, skipped = derive_assertions(webhook_text)
        self.assertEqual(
            [one.check for one in derived if one.check == "body.event"],
            [],
            "请求侧字段 `event` 又被断言在响应上了",
        )
        self.assertTrue(
            any("body.event" in note for note in skipped),
            "归属闸门摘掉 `body.event` 却没写原因（'没断言什么'也要能看见）",
        )

    def test_golden_conditional_field_regression(self):
        """★回归：`get_user_profile.md` 的 `data.avatarUrl` 必须**不再**被断言。"""
        with open(
            os.path.join(GOLDEN_DIR, "get_user_profile.md"), encoding="utf-8"
        ) as fp:
            text = fp.read()
        derived, skipped = derive_assertions(text)

        self.assertNotIn(
            "body.data.avatarUrl", {item.check for item in derived}, "条件字段又被无条件断言了"
        )
        self.assertTrue(any("avatarUrl" in item for item in skipped))


class TestOwnershipIsByPath(_TempWorkspace):
    """★§9.32：归属闸门从"**按名字**"升级为"**按路径**"。

    实测根因（§9.31 探针抓到）：请求表字段名与响应字段**末段撞名**时（都叫 `fileName`），
    文档里其实只有 `data.fileName`，但 `body.fileName` 因为"名字对得上"被放行 →
    **照文档实现的服务必让这条断言取不到值**（`check_value: None`）→ 必红，
    而它的失败**看起来像接口坏了**（分叉型静默）。

    ★本类的判据全是**成对**的：同名不同层级 → 结果必须不同；判不了 → 仍要放行。
    """

    # 响应侧字段挂在 `data` 下（表格裸名 → `data.<名>`），请求侧恰好同名 → 撞名现场
    COLLIDING_DOC = """# 文件服务

## POST /api/file/metadata

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| fileName | string | 是 | 文件名（请求侧） |

### 响应数据

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| fileName | string | 文件名 |
| fileSize | integer | 字节数 |
"""

    def _ownership(self, doc, check, quote="响应数据", expect="str"):
        from interfacetester_ai.assembler import check_response_ownership  # noqa: PLC0415
        from interfacetester_ai.schema import DraftAssertion  # noqa: PLC0415

        return check_response_ownership(
            DraftAssertion(
                comparator="type_match", check=check, expect=expect, source_quote=quote
            ),
            doc,
            where="t",
        )

    def test_top_level_assertion_on_a_data_field_is_dropped(self):
        """★核心：`body.fileName`（顶层）而文档只在 `data.fileName` 有据 → **摘掉**。"""
        found = self._ownership(self.COLLIDING_DOC, "body.fileName")

        self.assertIsNotNone(found, "层级取错的断言被放行了")
        self.assertIn("层级取错", found.message)
        self.assertIn("data.fileName", found.message, "要点名它其实在哪一层")

    def test_the_correct_level_is_kept(self):
        """★成对：同一条现场，写**对层级**（`body.data.fileName`）→ **放行**。"""
        self.assertIsNone(self._ownership(self.COLLIDING_DOC, "body.data.fileName"))

    def test_same_name_at_top_level_is_kept(self):
        """★成对（与第 1 条**只差层级**）：文档把该字段声明在**顶层** → `body.fileName` **放行**。

        ★这一对就是"判据真的是**路径**、不是名字"的机器证明：同一个名字，层级不同 → 结论不同。
        """
        doc = (
            "# 文件服务\n\n## POST /api/file/metadata\n\n"
            "响应示例：\n\n```json\n{\"fileName\": \"report.pdf\", \"size\": 1}\n```\n"
        )

        self.assertIsNone(self._ownership(doc, "body.fileName", quote="响应示例"))

    def test_absent_field_keeps_the_old_message(self):
        """★成对：文档里**根本没有**这个字段 → 仍用老消息（"响应侧没有这个字段"），别混成"层级取错"。"""
        found = self._ownership(self.COLLIDING_DOC, "body.orderNo")

        self.assertIsNotNone(found)
        self.assertNotIn("层级取错", found.message)
        self.assertIn("没有这个字段", found.message)

    def test_note_paths_are_compared_by_level_too(self):
        """说明句的路径同样按层级比：`data.total` 有据 → `body.data.total` 放行 / `body.total` 摘掉。"""
        doc = (
            "# 订单服务\n\n## GET /api/orders\n\n"
            "### 响应\n\n"
            "- `data.total`：整数，总条数\n"
            "- `data.page`：整数，页码\n"
        )

        self.assertIsNone(self._ownership(doc, "body.data.total", quote="响应", expect="int"))
        self.assertIsNotNone(self._ownership(doc, "body.total", quote="响应", expect="int"))

    def test_unknown_location_is_still_tolerant(self):
        """★宽容线不能丢：引用句在文档里**定位不到** → **不判**（宁可不摘，也不误摘）。"""
        self.assertIsNone(self._ownership(self.COLLIDING_DOC, "body.fileName", quote="不存在的引用句"))

    def test_meta_guardrail_the_path_rule_is_what_drops_it(self):
        """★★元护栏：把"裸名挂 `data` 下"这条换算**掐掉**（退化成旧的按名字）→ 撞名那条**必被放行**。

        证明"摘掉它"的正是**路径判据**，而不是别的什么顺手规则。
        """
        from unittest import mock as _mock  # noqa: PLC0415

        from interfacetester_ai import assertions as assertions_module  # noqa: PLC0415

        self.assertIsNotNone(self._ownership(self.COLLIDING_DOC, "body.fileName"))
        with _mock.patch.object(
            assertions_module, "_response_table_path", lambda name: (name,)
        ):  # 名字即路径（旧口径）
            self.assertIsNone(
                self._ownership(self.COLLIDING_DOC, "body.fileName"),
                "掐掉路径换算后仍被摘 —— 说明拦住它的不是这条规则，本护栏无效",
            )


class TestResponseHeaderTables(unittest.TestCase):
    """★§9.33 **响应头表 → 可判据的 `headers.<名>` 断言**（把 §9.29/§9.30 的纪律延续到响应头）。

    与 Cookie 那套**同源**：只断文档**明写**的可判据事实（固定值 / 枚举），其余**点名**、不硬凑 ——
    因为响应头值多半**易变**（`Date` / `X-Request-Id`），而"断言它是字符串"永远为真、零信息量 ✗。

    表形态判据是**两条一起**：`头` 打头 **且没有「必填」列**（有必填 = 请求头表）**且**表附近出现过
    "响应/返回"字样（否则"请求头表恰好忘写必填列"会被误当响应头表 ✗）。
    """

    DOC = """# 文件服务

## POST /api/file/upload

上传文件。

### 响应

响应头：

| 头 | 值 | 说明 |
| --- | --- | --- |
| X-Api-Version | v1 | **固定值**：当前接口版本 |
| Cache-Control | - | 取值：no-store / max-age=60 |
| Date | - | 服务器时间（易变） |
"""

    def _derived(self, doc):
        return derive_assertions(doc)

    def test_fixed_value_header_becomes_an_equal_assertion(self):
        """说明列标**固定值** + 值列给了值 → `equal: [headers.<名>, <值>]`（**有牙**）。"""
        result, _skipped = self._derived(self.DOC)
        found = [item for item in result if item.check == "headers.X-Api-Version"]

        self.assertEqual(len(found), 1, [item.check for item in result])
        self.assertEqual(found[0].comparator, "equal")
        self.assertEqual(found[0].expect, "v1")

    def test_enum_header_becomes_contained_by(self):
        """说明列给**枚举** → `contained_by`（响应头值恒为字符串，按字符串比）。"""
        result, _skipped = self._derived(self.DOC)
        found = [item for item in result if item.check == "headers.Cache-Control"]

        self.assertEqual(found[0].comparator, "contained_by")
        self.assertEqual(found[0].expect, ["no-store", "max-age=60"])

    def test_header_without_a_decidable_fact_is_named_not_asserted(self):
        """★成对（本仓核心纪律）：声明了但没有可判据事实 → **不产断言**，只**点名**。"""
        result, skipped = self._derived(self.DOC)

        self.assertFalse([item for item in result if item.check == "headers.Date"])
        self.assertTrue(any("Date" in note for note in skipped), skipped)

    def test_request_header_table_is_not_a_response_header_table(self):
        """★成对：带「必填」列的**请求头表**（即便附近写着"响应"）→ 不产 `headers.*` 断言。"""
        doc = (
            "# 文件服务\n\n## POST /api/file/upload\n\n"
            "响应如下。\n\n"
            "| 头 | 值 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| X-Path | /app1/ | 是 | 父目录 |\n"
        )
        result, _skipped = self._derived(doc)

        self.assertFalse([item for item in result if item.check.startswith("headers.")])

    def test_table_far_from_any_response_wording_is_ignored(self):
        """★成对：没有「必填」列、但表附近**完全没有**"响应/返回"字样 → 也不当响应头表。

        这条防的是"请求头表恰好忘写必填列"被误当响应头表（凭空多出必红断言 ✗）。
        """
        doc = (
            "# 文件服务\n\n## POST /api/file/upload\n\n"
            "### 请求头\n\n"
            "| 头 | 值 | 说明 |\n| --- | --- | --- |\n"
            "| X-Path | /app1/ | 父目录 |\n"
        )
        result, _skipped = self._derived(doc)

        self.assertFalse([item for item in result if item.check.startswith("headers.")])

    def test_header_assertions_carry_a_real_line_number(self):
        """★**必须带行号**：段外过滤（§9.6/§9.14）的唯一口径是"行号落在本接口段内"——
        行号 `0` 会被当**段外**整条丢弃（§9.29 的 cookie 断言就是这么丢过一次 ✗）。"""
        result, _skipped = self._derived(self.DOC)

        for item in result:
            if item.check.startswith("headers."):
                self.assertGreater(item.line_no, 0, f"{item.check} 没带行号 → 会被当段外丢掉")

    def test_enum_list_follows_the_document(self):
        """★元护栏：枚举取值来自**文档**（改取值 → 断言跟着变，不是写死的）。"""

        def enum_of(note_text):
            doc = (
                "# 文件服务\n\n## POST /api/file/upload\n\n"
                "### 响应\n\n"
                "| 头 | 值 | 说明 |\n| --- | --- | --- |\n"
                f"| Cache-Control | - | {note_text} |\n"
            )
            result, _skipped = self._derived(doc)
            found = [item for item in result if item.check == "headers.Cache-Control"]
            return found[0].expect if found else []

        self.assertEqual(enum_of("取值：no-store / max-age=60"), ["no-store", "max-age=60"])
        self.assertEqual(enum_of("取值：no-cache"), ["no-cache"])


class TestDocDegradationIsVisible(unittest.TestCase):
    """★§9.33 登记项 3：`derive_assertions` 的**降级说明**必须落进 `unknowns`（可见，不静默）。

    **改动前的现场**：两条通路都写成 `all_assertions, _skipped = derive_assertions(...)` ——
    `_skipped`（"**文档声明了、但判不出可断言内容**"的逐条说明）被丢掉，产物上**完全看不出**
    "我们没断言什么"。真实文档上量过：**10~15 条**（见 `docs/未做完的待决策项.md` §9.33 第 7 节第 3 条）。

    ★为什么是"文档级"而不是"逐段"：`skipped` 的契约是 `List[str]`（**没有行号**）——
    猜它属于哪一段比不标更坏（§9.32 的同一课），所以随**每段**草稿各带一份并写明"对所有段适用"。
    """

    # 两条必然的降级说明：① 成功响应示例里 `data` 是**结构**（不按值断言）；
    # ② 失败响应示例要非法请求才触发（负例该单列用例）
    DOC_WITH_DEGRADATION = (
        "# 服务\n\n## POST /api/order\n\n建单。\n\n"
        "### 请求体（JSON）\n\n"
        "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
        "| orderId | string | 是 | 订单号 | A-1 |\n\n"
        '请求体示例：\n\n```json\n{"orderId": "A-1"}\n```\n\n'
        '### 成功响应\n\n```json\n{"success": true, "data": {"orderId": "A-1"}}\n```\n\n'
        '### 失败响应\n\n```json\n{"success": false, "errorCode": "ORDER_4001"}\n```\n'
    )
    # 同一形态但**没有**任何"判不了"的东西 → 不许出现这条（成对）
    CLEAN_DOC = (
        "# 服务\n\n## GET /api/ping\n\n存活探测。\n\n"
        "### 响应\n\n"
        "| 字段 | 类型 | 说明 |\n| --- | --- | --- |\n| ok | bool | 是否成功 |\n"
    )

    def test_every_draft_carries_the_doc_level_degradation(self):
        results = generate_assertions_only_all(self.DOC_WITH_DEGRADATION)

        self.assertTrue(results, "夹具没有识别出接口段")
        for result in results:  # ★每段都带（文档级说明对所有接口段都适用）
            kinds = [item.kind for item in result.unknowns]
            self.assertIn(DOC_DEGRADATION, kinds, kinds)
        note = next(item for item in results[0].unknowns if item.kind == DOC_DEGRADATION)
        self.assertIn("失败响应", note.message)  # 逐条原文在里面（不是空壳）
        self.assertIn("文档级", note.message)
        # ★它**不阻止转正**：这不是"错"，是"判不了"（level=degrade）
        self.assertEqual(note.level, "degrade")

    def test_clean_document_has_no_degradation_note(self):
        """★成对：文档里没有"判不了"的东西 → **不产**这条（别把正常情况也标成降级）。"""
        results = generate_assertions_only_all(self.CLEAN_DOC)

        self.assertTrue(results)
        for result in results:
            kinds = [item.kind for item in result.unknowns]
            self.assertNotIn(DOC_DEGRADATION, kinds, kinds)

    def test_wiring_is_what_makes_it_appear(self):
        """★元护栏：把接线掐掉（`_doc_degradation_unknowns` 恒返回 []）→ 那条**必消失**。

        证明"它在产物里"靠的是**这条接线**，而不是别的东西顺手带出来的。
        """
        import interfacetester_ai.gen as gen_module

        with mock.patch.object(gen_module, "_doc_degradation_unknowns", lambda notes: []):
            result = generate_assertions_only_all(self.DOC_WITH_DEGRADATION)[0]

        self.assertNotIn(DOC_DEGRADATION, [item.kind for item in result.unknowns])


class TestRequestPayloadFromFieldTables(unittest.TestCase):
    """★§9.16（2026-09-27）：**文档写明的请求参数要真的发出去**（`headers`/`params`/`json`/`data`）。

    改造前的缺口（登记在 §9.14 第六节第 2 项 / §9.15 第四节第 3 项）：确定性产物的 `request`
    只有 `method`/`url` —— 用例"能加载、能断言"，但对真实服务是**空请求**。

    每条判据都写成**成对**的：既钉住"该发的必须发"，也钉住"不该发的（响应数据表 / 可选字段 /
    凭据明文 / multipart 文件字段）一个都不许乱发"。
    """

    BODY_DOC = """# 示例服务

## POST /api/dir

| 字段 | 类型 | 必填 | 说明 | 示例值 |
| --- | --- | --- | --- | --- |
| path | string | 是 | 目录路径 | /app1/documents |
| depth | integer | 是 | 层数 | 2 |
| note | string | 否 | 备注 | x |

**请求体示例：**

```json
{"path": "/app1/documents", "depth": 2}
```
"""

    def _payload(self, doc, *, method="POST", public_scopes=(), start_line=None, end_line=None):
        rows = read_field_rows(doc)
        lines = doc.split("\n")
        return request_payload_from_rows(
            rows,
            doc,
            start_line=1 if start_line is None else start_line,
            end_line=len(lines) if end_line is None else end_line,
            public_scopes=public_scopes,
            method=method,
        )

    # ---- 该发的必须发 -----------------------------------------------------

    def test_body_table_becomes_json(self):
        """请求体表（首列「字段」）→ `json`，且**示例值按声明类型转成字面量**（`2` 是 int）。"""
        payload, _notes = self._payload(self.BODY_DOC)

        self.assertEqual(payload["json"]["path"], "/app1/documents")
        self.assertEqual(payload["json"]["depth"], 2)
        self.assertNotIsInstance(payload["json"]["depth"], str)

    def test_json_body_gets_a_content_type_and_says_so(self):
        """发了 JSON 体 → 补 `Content-Type`（协议事实），且**必须记一条 note**（可见，不静默）。"""
        payload, notes = self._payload(self.BODY_DOC)

        self.assertEqual(payload["headers"]["Content-Type"], "application/json")
        self.assertTrue(any("Content-Type" in note for note in notes), notes)

    def test_query_param_table_becomes_params(self):
        """首列「参数」→ 查询参数（`params`），**不**落 `json`。"""
        doc = (
            "# 示例服务\n\n## GET /api/list\n\n"
            "| 参数 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| page | integer | 是 | 页码 | 1 |\n"
        )
        payload, _notes = self._payload(doc, method="GET")

        self.assertEqual(payload["params"], {"page": 1})
        self.assertNotIn("json", payload)

    def test_header_table_becomes_headers_and_never_json(self):
        """★核心修复：首列「**头名称**」→ `headers`。

        改造前它同时命中 `_FIELD_COL_RE`（"名称"）→ 被当成**请求体字段**：一旦按字段表补请求体，
        `Authorization`/`X-Path`/`Content-Type` 就会被写进 JSON ✗（实测那份客户文档全是这种表）。
        """
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 头名称 | 必填 | 示例值 |\n| --- | --- | --- |\n"
            "| X-Path | 是 | /app1/ |\n"
            "| Content-Type | 是 | application/json |\n"
        )
        payload, _notes = self._payload(doc)

        self.assertEqual(
            payload["headers"], {"X-Path": "/app1/", "Content-Type": "application/json"}
        )
        self.assertNotIn("json", payload, "请求头被写进了请求体")

    def test_header_table_with_a_bare_head_column_is_read(self):
        """★§9.27（**真缺陷修复**）：首列写「头」时，整张头表原本**一行都读不到**。

        实测代价（2026-09-27，转换后的客户文档）：必填的 `X-Path` 同时写在头表与 curl 里，
        产物却**一个头都不带** → 打真实服务必被拒（`FILE_1018`），而且**不报错、不点名** ✗✗。
        这条钉「首列就是「头」」的形态；「头名称」由上面那条判据钉住（两种都得认）。
        """
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 头 | 必填 | 说明 |\n| --- | --- | --- |\n"
            "| X-Path | 是 | 父目录路径 |\n"
            "| Accept | 否 | 可选头 |\n"
        )
        payload, _notes = self._payload(doc)

        self.assertIn("X-Path", payload.get("headers", {}))
        self.assertNotIn("Accept", payload.get("headers", {}), "可选头被发出去了")

    def test_header_value_column_is_recognized(self):
        """头表的取值列习惯写「值」（`| 头 | 值 | 必填 | 说明 |`，golden 就是这么写的）→ 必须取到。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 头 | 值 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| X-Path | /app1/ | 是 | 父目录路径 |\n"
        )
        payload, _notes = self._payload(doc)

        self.assertEqual(payload["headers"]["X-Path"], "/app1/")

    def test_header_without_any_source_becomes_a_loud_placeholder(self):
        """★成对：必填头**文档确实没给值** → `${ENV(TODO_…)}` 占位 + 点名（不许静默丢掉一个必填头）。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 头 | 必填 | 说明 |\n| --- | --- | --- |\n"
            "| X-Path | 是 | 父目录路径 |\n"
        )
        payload, notes = self._payload(doc)

        self.assertTrue(has_todo(payload["headers"]["X-Path"]), payload["headers"])
        self.assertTrue(any("X-Path" in note for note in notes), notes)

    def test_public_header_section_is_inherited_and_said_so(self):
        """★公共请求头是**跨接口契约** → 继承进本接口，且必须**看得见**（一条 note）。"""
        doc = (
            "# 示例服务\n\n## 公共请求头\n\n"
            "| 头 | 值 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| X-Path | /app1/ | 是 | 路径校验头 |\n\n"
            "## POST /api/dir\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| path | string | 是 | 路径 | /app1/x |\n"
        )
        lines = doc.split("\n")
        post_line = next(i for i, line in enumerate(lines, 1) if line.startswith("## POST"))
        payload, notes = self._payload(
            doc,
            public_scopes=[(1, post_line - 1)],
            start_line=post_line,
            end_line=len(lines),
        )

        self.assertEqual(payload["headers"]["X-Path"], "/app1/")
        self.assertTrue(any("公共请求头" in note for note in notes), notes)

    def test_bare_value_column_is_only_a_source_inside_a_header_table(self):
        """★边界（与上一条成对）：取值列「值」**只在头表里认** —— 请求体表里的「值」不是示例值。

        只在头表认的理由：body/params 表没有这种约定，认了会把「取值范围」这类列也当值吃进来。
        """
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 值 |\n| --- | --- | --- | --- | --- |\n"
            "| path | string | 是 | 路径 | /app1/x |\n"
        )
        payload, _notes = self._payload(doc)

        self.assertTrue(has_todo(payload["json"]["path"]), payload["json"])

    def test_get_method_sends_body_fields_as_query(self):
        """★协议事实：`GET` 没有请求体 → 这一节的请求字段按**查询参数**发（并记一条 note）。"""
        doc = (
            "# 示例服务\n\n## GET /api/query\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| path | string | 是 | 路径 | /app1/ |\n"
        )
        payload, notes = self._payload(doc, method="GET")

        self.assertEqual(payload["params"], {"path": "/app1/"})
        self.assertNotIn("json", payload)
        self.assertTrue(any("没有请求体" in note for note in notes), notes)

    def test_value_comes_from_the_request_sample_block(self):
        """没写「示例值」列时，值可来自**本段**的请求体示例（同名字段）。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 字段 | 类型 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| name | string | 是 | 名字 |\n\n"
            '**请求体示例：**\n\n```json\n{"name": "reports"}\n```\n'
        )
        payload, _notes = self._payload(doc)

        self.assertEqual(payload["json"], {"name": "reports"})

    def test_form_urlencoded_goes_to_data(self):
        """`x-www-form-urlencoded` → 落 `data`（而不是 `json`）。"""
        doc = (
            "# 示例服务\n\n## POST /api/form\n\n"
            "Content-Type: `application/x-www-form-urlencoded`\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| keyword | string | 是 | 关键字 | abc |\n"
        )
        payload, _notes = self._payload(doc)

        self.assertEqual(payload["data"], {"keyword": "abc"})
        self.assertNotIn("json", payload)


    # ---- 不该发的必须不许发 -----------------------------------------------

    def test_optional_field_is_not_sent(self):
        """★成对：**可选**字段默认不发（与"可选字段不断言"同一精神），但要在 note 里点名。"""
        payload, notes = self._payload(self.BODY_DOC)

        self.assertNotIn("note", payload["json"], "可选字段被发出去了")
        self.assertTrue(any("可选" in note for note in notes), notes)

    def test_missing_value_becomes_a_todo_placeholder(self):
        """★文档确实没给值 → `${ENV(TODO_…)}` 占位（运行期响亮报错），绝不编一个像真的值。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 字段 | 类型 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| bizNo | string | 是 | 业务单号 |\n"
        )
        payload, notes = self._payload(doc)

        self.assertTrue(has_todo(payload["json"]["bizNo"]), payload["json"])
        self.assertTrue(any("bizNo" in note for note in notes), notes)

    def test_credential_value_is_never_copied_from_the_document(self):
        """★脱敏硬线：文档示例值里写着真 token，产物里也**只能是** `${ENV(AUTHORIZATION)}`。"""
        doc = (
            "# 示例服务\n\n## GET /api/me\n\n"
            "| 头名称 | 必填 | 示例值 |\n| --- | --- | --- |\n"
            "| Authorization | 是 | Bearer eyJhbGciOiJIUzI1NiJ9.SECRET |\n"
        )
        payload, notes = self._payload(doc, method="GET")

        self.assertEqual(payload["headers"]["Authorization"], "${ENV(AUTHORIZATION)}")
        self.assertNotIn("SECRET", json.dumps(payload, ensure_ascii=False))
        self.assertTrue(any("凭据" in note for note in notes), notes)

    def test_credential_hint_quotes_the_note_column_and_names_the_env_var(self):
        """★§9.28：光写 `${ENV(AUTHORIZATION)}` 不够 —— 必须告诉人**填哪个环境变量、取值方式出自文档哪里**。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 头 | 必填 | 说明 |\n| --- | --- | --- |\n"
            "| Authorization | 是 | OAuth2.0 的 Bearer Token：`Bearer {access_token}` |\n"
        )
        payload, notes = self._payload(doc)

        self.assertEqual(payload["headers"]["Authorization"], "${ENV(AUTHORIZATION)}")
        text = "\n".join(notes)
        self.assertIn("AUTHORIZATION", text, "没告诉人该填哪个环境变量")
        self.assertIn("OAuth2.0 的 Bearer Token", text, "提示必须引用文档原文")

    def test_credential_hint_points_at_the_authentication_section(self):
        """文档里有讲认证的那一节 → 提示给出**节名 + 行号**（人可以直接跳过去看怎么换 token）。"""
        doc = (
            "# 示例服务\n\n## 认证方式\n\n先用 client_id / client_secret 换 Token。\n\n"
            "## POST /api/dir\n\n"
            "| 头 | 必填 | 说明 |\n| --- | --- | --- |\n"
            "| Authorization | 是 | 令牌 |\n"
        )
        lines = doc.split("\n")
        post_line = next(i for i, line in enumerate(lines, 1) if line.startswith("## POST"))
        _payload, notes = self._payload(doc, start_line=post_line, end_line=len(lines))

        text = "\n".join(notes)
        self.assertIn("认证方式", text)
        self.assertIn("行", text)

    def test_credential_hint_admits_when_the_document_says_nothing(self):
        """★成对：文档**确实没写**怎么取凭据 → 如实说"文档没写、交人工"，**不许编一句**。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 头 | 必填 |\n| --- | --- |\n"
            "| Authorization | 是 |\n"
        )
        _payload, notes = self._payload(doc)

        text = "\n".join(notes)
        self.assertIn("没有", text)
        self.assertIn("人工", text)

    def test_ordinary_fields_get_no_credential_hint(self):
        """★成对：非凭据字段**不**该带这类提示（提示只对凭据有意义，乱加就是噪音）。"""
        _payload, notes = self._payload(self.BODY_DOC)

        self.assertFalse(any("凭据" in note for note in notes), notes)

    def test_credential_hint_follows_the_document_not_a_hardcoded_string(self):
        """★元护栏：提示来自**文档** —— 改说明列原文，提示必须跟着变（防"写死一句话"）。"""

        def hint_of(note_text):
            doc = (
                "# 示例服务\n\n## POST /api/dir\n\n"
                "| 头 | 必填 | 说明 |\n| --- | --- | --- |\n"
                f"| Authorization | 是 | {note_text} |\n"
            )
            _payload, notes = self._payload(doc)
            return "\n".join(notes)

        first = hint_of("OAuth2.0 的 Bearer Token")
        second = hint_of("换来的 access_token，有效期一小时")

        self.assertIn("OAuth2.0 的 Bearer Token", first)
        self.assertNotIn("OAuth2.0 的 Bearer Token", second)
        self.assertIn("换来的 access_token", second)

    def test_response_data_table_does_not_leak_into_the_request(self):
        """★成对：**没有「必填」列**的响应数据表不许漏进请求（判不出必填 → 不发是免费的）。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| path | string | 是 | 路径 | /app1/ |\n\n"
            "### 响应数据\n\n"
            "| 字段 | 类型 | 说明 |\n| --- | --- | --- |\n"
            "| dirId | string | 目录 id |\n"
        )
        payload, _notes = self._payload(doc)

        self.assertEqual(payload["json"], {"path": "/app1/"})

    def test_multipart_fields_go_to_upload_with_a_placeholder_path(self):
        """★§9.20：multipart 的字段进 `upload`，值是 `${ENV(TODO_…)}`（**文件路径编不出来**）。

        内核侧一直支持 `upload`（`IRStep.upload`），缺的是**草案契约**这一环 ——
        改造前只能整块丢掉（产物里连"这里要传文件"都看不到）。
        """
        doc = (
            "# 示例服务\n\n## POST /api/upload\n\n"
            "Content-Type: `multipart/form-data`\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| file | string | 是 | 上传的文件 | report.pdf |\n"
        )
        payload, notes = self._payload(doc)

        self.assertTrue(has_todo(payload["upload"]["file"]), payload)
        self.assertNotIn("json", payload)
        self.assertTrue(any("文件上传" in note for note in notes), notes)

    def test_payload_is_empty_when_nothing_is_required(self):
        """一条必填都没有 → **空载荷**（不写 `{}`，免得产物里多一个"看起来有内容"的空壳）。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 字段 | 类型 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| note | string | 否 | 备注 |\n"
        )
        payload, _notes = self._payload(doc)

        self.assertEqual(payload, {})

    def test_required_column_missing_means_not_sent(self):
        """★必填列**没写清**（空白）→ 与"可选"同处置：不发（宁缺勿错）。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 字段 | 类型 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| path | string |  | 路径 |\n"
        )
        payload, _notes = self._payload(doc)

        self.assertEqual(payload, {})


    # ---- §9.18 curl 示例当取值来源 ----------------------------------------

    def test_curl_example_values_win_over_the_example_column(self):
        """★§9.18：curl 示例（**一次完整可跑的调用**）的取值优先于「示例值」列。

        真实文档里 curl 往往比表格"新"（表格写 `/old/`、curl 写 `/app1/documents/`），
        而且它把头、体、查询串都写全了 —— 用它当请求值，产物更接近"作者真的调过一次"。
        """
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 头名称 | 必填 | 示例值 |\n| --- | --- | --- |\n"
            "| X-Path | 是 | /old/ |\n"
            "| Authorization | 是 | Bearer OLD-TOKEN |\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| path | string | 是 | 路径 | /old/dir |\n\n"
            "**调用示例**：\n\n```bash\n"
            "curl -X POST https://host/api/dir \\\n"
            "  -H \"X-Path: /app1/documents/\" \\\n"
            "  -H \"Authorization: Bearer REAL-TOKEN\" \\\n"
            "  -d '{\"path\": \"/app1/documents/reports\"}'\n"
            "```\n"
        )
        payload, notes = self._payload(doc)

        self.assertEqual(payload["headers"]["X-Path"], "/app1/documents/")
        self.assertEqual(payload["json"]["path"], "/app1/documents/reports")
        # ★脱敏不受"来源变多"影响：curl 里的真 token 照样不落产物
        self.assertEqual(payload["headers"]["Authorization"], "${ENV(AUTHORIZATION)}")
        self.assertNotIn("REAL-TOKEN", json.dumps(payload, ensure_ascii=False))
        self.assertTrue(any("curl" in note for note in notes), notes)

    def test_curl_url_query_becomes_params(self):
        """curl 的 URL 查询串 → `params`（值做 URL 解码）；★**表里没有的键不凭空加**。"""
        doc = (
            "# 示例服务\n\n## GET /api/list\n\n"
            "| 参数 | 类型 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| path | string | 是 | 路径 |\n\n"
            "**调用示例**：\n\n```bash\n"
            'curl -X GET "https://host/api/list?path=%2Fapp1%2F&page=2"\n'
            "```\n"
        )
        payload, _notes = self._payload(doc, method="GET")

        self.assertEqual(payload["params"]["path"], "/app1/")
        self.assertNotIn("page", payload["params"], "curl 里的 `page` 表里没有 → 不许凭空加")

    def test_curl_only_fields_are_not_invented(self):
        """★成对：curl 里有、**表里没有**的头/字段 → **不许**凭空加进请求（只作取值来源）。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| path | string | 是 | 路径 | /dir |\n\n"
            "**调用示例**：\n\n```bash\n"
            "curl -X POST https://host/api/dir -H \"X-Trace-Id: abc\" "
            "-d '{\"path\": \"/x\", \"extra\": 1}'\n"
            "```\n"
        )
        payload, _notes = self._payload(doc)

        # ★curl 的值**优先**（设计如此）：表里示例值是 `/dir`，curl 里是 `/x` → 用 `/x`
        self.assertEqual(payload["json"], {"path": "/x"})
        self.assertNotIn("X-Trace-Id", payload.get("headers", {}))
        self.assertNotIn("extra", payload["json"], "curl 里有、表里没有的字段**不许**凭空加")

    def test_without_curl_the_example_column_still_wins(self):
        """★成对：没有 curl 示例 → 仍按「示例值」列（老行为不变）。"""
        payload, _notes = self._payload(self.BODY_DOC)

        self.assertEqual(payload["json"]["path"], "/app1/documents")

    # ---- 接线（`to_draft_payload`）与既有判据互不干扰 ----------------------

    def test_to_draft_payload_without_request_keeps_the_old_shape(self):
        """★成对：不传 `request` 时产物**一字不变**（只有 method/url）——外部调用方不受影响。"""
        payload = to_draft_payload("", case_name="x", url="/api/x", assertions=[])
        step = payload["steps"][0]

        self.assertEqual(sorted(step), ["method", "name", "url", "validate"])

    def test_to_draft_payload_merges_the_request_payload(self):
        """传了就合并进去；空载荷不写空壳。"""
        payload = to_draft_payload(
            "", case_name="x", url="/api/x", assertions=[], request={"json": {"a": 1}, "params": {}}
        )
        step = payload["steps"][0]

        self.assertEqual(step["json"], {"a": 1})
        self.assertNotIn("params", step)

    def test_request_side_tables_produce_no_assertions(self):
        """★请求侧（头 / 查询参数）**不产断言**；请求体表照旧产（成对）。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 头名称 | 必填 | 示例值 |\n| --- | --- | --- |\n"
            "| X-Path | 是 | /app1/ |\n\n"
            "| 字段 | 类型 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| path | string | 是 | 路径 |\n"
        )
        derived, skipped = derive_assertions(doc)
        checks = {item.check for item in derived}

        self.assertEqual(
            [check for check in checks if check.startswith(("headers.", "params."))],
            [],
            f"请求侧字段被断言在响应上了：{sorted(checks)}",
        )
        self.assertIn("body.path", checks, "请求体表照旧要推断言（成对）")
        self.assertTrue(any("请求侧" in note for note in skipped), skipped)

    def test_consistency_requirement_is_flagged(self):
        """说明列写着"必须与…相同"→ 值照文档示例填，但**必须记一条 note** 请人工确认。"""
        doc = (
            "# 示例服务\n\n## POST /api/dir\n\n"
            "| 头名称 | 必填 | 示例值 | 说明 |\n| --- | --- | --- | --- |\n"
            "| X-Path | 是 | /app1/ | 必须与请求参数中的 `path` 字段完全相同 |\n"
        )
        _payload, notes = self._payload(doc)

        self.assertTrue(any("保持一致" in note for note in notes), notes)

    def test_public_request_headers_are_inherited(self):
        """★「公共请求头」节的头，每个接口都要带（这是文档自己声明的跨接口契约）。

        ★**接口段的范围只到 `## POST /api/dir` 那一段**（公共节在范围外）——不然这条判据会
        "顺手"通过：公共表本来就在范围里，根本证明不了"继承"这件事（假绿）。
        """
        doc = (
            "# 示例服务\n\n## 公共请求头\n\n"
            "| 头名称 | 必填 | 示例值 |\n| --- | --- | --- |\n"
            "| Tenant | 是 | t-1 |\n\n"
            "## POST /api/dir\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| path | string | 是 | 路径 | /app1/ |\n"
        )
        dir_line = [n for n, line in enumerate(doc.split("\n"), start=1) if "/api/dir" in line][0]
        payload, _notes = self._payload(
            doc, public_scopes=[(3, 8)], start_line=dir_line, end_line=len(doc.split("\n"))
        )

        self.assertEqual(payload["headers"]["Tenant"], "t-1")
        self.assertEqual(payload["json"], {"path": "/app1/"})

    def test_public_body_table_is_not_inherited(self):
        """★成对：公共节的**请求体表**不许套给每个接口（那正是"错位"要防的事）。"""
        doc = (
            "# 示例服务\n\n## 公共请求体\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| sharedField | string | 是 | 公共字段 | v |\n\n"
            "## POST /api/dir\n\n"
            "| 字段 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| path | string | 是 | 路径 | /app1/ |\n"
        )
        dir_line = [n for n, line in enumerate(doc.split("\n"), start=1) if "/api/dir" in line][0]
        payload, _notes = self._payload(
            doc, public_scopes=[(3, 8)], start_line=dir_line, end_line=len(doc.split("\n"))
        )

        self.assertNotIn("sharedField", json.dumps(payload, ensure_ascii=False))


    def test_get_section_rows_are_treated_as_query_params(self):
        """★§9.16（实测那份客户文档）：**GET 段的「字段名」表**其实是**查询参数**。

        改造前它被当成请求体表 → 产出 `body.path` 断言；而该节的**响应数据表**里恰好也有
        `path`（列名宽松判据按**末段名**比）→ 归属闸门放行 → 真跑**必红**。
        判据成对：默认（不传 `query_scopes`）保持老行为；传了 GET 段范围才改口径。
        """
        doc = (
            "# 示例服务\n\n## GET /api/dir\n\n"
            "**请求参数**（Query String）：\n\n"
            "| 字段名 | 类型 | 必填 | 说明 | 示例值 |\n| --- | --- | --- | --- | --- |\n"
            "| path | String | 是 | 目录路径 | /app1/ |\n\n"
            "### 响应数据\n\n"
            "| 字段 | 类型 | 说明 |\n| --- | --- | --- |\n"
            "| path | string | 目录路径 |\n"
        )
        last = len(doc.split("\n"))

        legacy, _skipped = derive_assertions(doc)
        self.assertIn(
            "body.path",
            {item.check for item in legacy},
            "不传 query_scopes 时必须保持老行为（成对判据）",
        )

        fixed, skipped = derive_assertions(doc, query_scopes=[(3, last)])
        self.assertNotIn(
            "body.path",
            {item.check for item in fixed},
            "★GET 段的请求字段又被断言在响应上了（真跑必红）",
        )
        self.assertTrue(any("请求侧" in note for note in skipped), skipped)


class TestResponseTableShapeConflict(unittest.TestCase):
    """★§9.17：响应数据表与**它自己的成功响应示例**冲突 → 不产必败断言 + **回流**成文档缺陷。

    实测（真实客户文档，2026-09-27）：4 个"列表型"接口的响应表列的是**元素字段**（裸名），
    而它们的「成功响应」示例写着 `"data": [ {...} ]` —— 按表推出来的 schema 说 `data` 是对象，
    跑起来**必然失败**（`- $.data: 期望 type=object，实际 [...]`），且看起来像"接口坏了"。
    判据全部成对：冲突的挡下、不冲突的照产、**判不了的不判**（老行为不变）。
    """

    CONFLICT_DOC = """# 示例服务

## GET /api/product/list

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| sku | string | 商品编码 |
| price | integer | 单价（分） |

**成功响应**：

```json
{"code": 0, "data": [{"sku": "SKU-0001", "price": 9900}]}
```
"""

    def test_conflicting_table_produces_no_schema_assertion(self):
        derived, skipped = derive_assertions(self.CONFLICT_DOC)

        self.assertEqual(
            [item.comparator for item in derived if item.comparator == "jsonschema_match"],
            [],
            "★与本节示例冲突的 schema 又被产出来了（跑起来必红）",
        )
        self.assertTrue(
            any("[文档缺陷]" in note for note in skipped),
            "冲突必须**说清**并带回流标记，否则等于静默少一条断言",
        )

    def test_object_shaped_example_still_produces_the_schema(self):
        """★成对：示例里 `data` 是**对象** → 照旧产 schema（不许把对的也挡掉）。"""
        doc = (
            "# 示例服务\n\n## GET /api/product/info\n\n"
            "| 字段 | 类型 | 说明 |\n| --- | --- | --- |\n"
            "| sku | string | 商品编码 |\n\n"
            "**成功响应**：\n\n```json\n{\"code\": 0, \"data\": {\"sku\": \"SKU-0001\"}}\n```\n"
        )
        derived, skipped = derive_assertions(doc)

        self.assertEqual(
            [item.comparator for item in derived if item.comparator == "jsonschema_match"],
            ["jsonschema_match"],
        )
        self.assertFalse(any("[文档缺陷]" in note for note in skipped), skipped)

    def test_no_success_example_means_we_do_not_judge(self):
        """★成对：本段**没有**成功响应示例 → 判不了就不判（老行为：照产 schema）。"""
        doc = (
            "# 示例服务\n\n## GET /api/product/list\n\n"
            "| 字段 | 类型 | 说明 |\n| --- | --- | --- |\n"
            "| sku | string | 商品编码 |\n"
        )
        derived, skipped = derive_assertions(doc)

        self.assertEqual(
            [item.comparator for item in derived if item.comparator == "jsonschema_match"],
            ["jsonschema_match"],
        )
        self.assertFalse(any("[文档缺陷]" in note for note in skipped), skipped)

    def test_null_shaped_example_is_also_a_conflict(self):
        """`"data": null` 与"表里列了 3 个字段"同样矛盾（照表实现的服务不会回 null）。"""
        doc = self.CONFLICT_DOC.replace(
            '{"code": 0, "data": [{"sku": "SKU-0001", "price": 9900}]}',
            '{"code": 0, "data": null}',
        )
        derived, skipped = derive_assertions(doc)

        self.assertEqual(
            [item.comparator for item in derived if item.comparator == "jsonschema_match"], []
        )
        self.assertTrue(any("[文档缺陷]" in note for note in skipped), skipped)

    def test_a_request_table_is_not_judged_by_this_rule(self):
        """★成对：**有「必填」列**的是请求表 → 不归这条判据（不许把请求表当响应表判）。"""
        doc = (
            "# 示例服务\n\n## POST /api/product/create\n\n"
            "| 字段 | 类型 | 必填 | 说明 |\n| --- | --- | --- | --- |\n"
            "| sku | string | 是 | 商品编码 |\n\n"
            "**成功响应**：\n\n```json\n{\"code\": 0, \"data\": [{\"sku\": \"SKU-0001\"}]}\n```\n"
        )
        _derived, skipped = derive_assertions(doc)

        self.assertFalse(any("[文档缺陷]" in note for note in skipped), skipped)

    def test_defect_carries_verbatim_evidence_from_both_sides(self):
        """回流条目必须带**双方原文**（文档原句 + 示例原文）——报告不做无证据的结论。"""
        defects = find_doc_defects(self.CONFLICT_DOC)

        self.assertEqual(len(defects), 1, defects)
        self.assertIn("sku", defects[0].table_quote)
        self.assertIn('"data": [', defects[0].example_quote)
        self.assertIn("data", defects[0].message)
        self.assertTrue(defects[0].action, "必须给改法（只指出问题不算回流）")


if __name__ == "__main__":
    unittest.main()
