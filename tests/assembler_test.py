# -*- coding: utf-8 -*-
"""装配器的护栏用例 —— §5.1 装配链 + §8.1 S-4 + §十 T28（**P0a** D 阶段）。

## 为什么这些判据必须在**真工作区**里跑

`assembler.run_selftest()` 只跑纯逻辑（对账规则）。装配器真正的风险在**落盘那一段**：

- **S-4 的实质判据**不是"写了 `NEEDS_HUMAN.md`"，而是「**带陷阱的产物不在 `cases/`**」
  ——这只能用"文件存在/不存在"来证；
- **查重**必须是硬失败（覆盖人工用例不可逆）；
- **零污染**要能指出"这次运行**多出了哪些文件**"。

所以本文件在 `setUp` 里 `chdir` 到**临时工作区**（`cases/`、`.ai/` 都是相对根），
结束再切回来——既跑到真实写盘路径，又**不往本仓落任何东西**。
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

import interfacetester_ai.assembler as assembler  # noqa: E402
from interfacetester_ai.assembler import (  # noqa: E402
    CASES_DIR,
    Coverage,
    assemble,
    documented_fields,
    draft_to_ircase,
    normalize_type_match,
    run_selftest,
)
from interfacetester_ai.schema import DraftAssertion, parse_draft  # noqa: E402

DOC = """# 下单接口

## POST /api/order

返回 200 表示成功。

| 字段 | 类型 | 必填 |
| --- | --- | --- |
| amount | number | 是 |
| remark | string | 可选 |
| token | string | 否 |

说明：

- amount：整数（int），单位分
- remark：可选字符串
- token：字符串，登录令牌（必填）
- data：对象，必含 userId 与 nickname

示例（请求体）：

```json
{"code": 0, "amount": 99.9}
```
"""

CLEAN_ASSERTIONS = [
    {
        "comparator": "equal",
        "check": "status_code",
        "expect": 200,
        "source_quote": "返回 200 表示成功",
    },
    {
        "comparator": "type_match",
        "check": "body.amount",
        "expect": "int",
        "source_quote": "amount：整数（int），单位分",
    },
    {
        "comparator": "type_match",
        "check": "body.token",
        "expect": "str",
        "source_quote": "token：字符串，登录令牌（必填）",
    },
]

# 陷阱：**空 schema**（S1 硬拦）。
#
# ★为什么不用 `not_equal: [body.amount, ""]` 这类更显眼的陷阱（实测踩到）：
# assembler 的对账**先于**闸门，而"凭空的期望值"会被 T28 的 ② 先摘掉——那条断言
# 连用例都没进，于是闸门**根本看不到它**，`rejected` 自然是 False。
# 要真的测到 S-4 这道前置，得用**能过对账、过不了闸门**的形态：空 schema 的
# expect 是 dict（② 刻意跳过 dict），但 S1 会拦。
# ——这本身是个有用的结论：**对账与闸门是两道不同层的防线，不能互相替代**。
TRAP_ASSERTIONS = CLEAN_ASSERTIONS + [
    {
        "comparator": "jsonschema_match",
        "check": "body.data",
        "expect": {},
        "source_quote": "data：对象，必含 userId 与 nickname",
    }
]


def _draft(assertions, case_name="下单接口"):
    return parse_draft(
        {
            "case_name": case_name,
            "steps": [
                {
                    "name": "下单",
                    "method": "post",
                    "url": "/api/order",
                    "json": {"amount": 100},
                    "validate": assertions,
                }
            ],
        }
    )


class _TempWorkspace(unittest.TestCase):
    """把 cwd 切到临时目录：`cases/`、`.ai/` 是相对根，这样才跑得到真实写盘路径。"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_assemble_")
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _tree(self):
        """工作区里的全部文件（相对路径，排序）——用于"多出了哪些文件"的断言。"""
        found = []
        for root, _dirs, files in os.walk("."):
            for name in files:
                found.append(os.path.relpath(os.path.join(root, name), ".").replace("\\", "/"))
        return sorted(found)


class TestAssembleProducesADraft(_TempWorkspace):
    """健康草稿 → 真的写出**草稿** `.ai/draft/<case>.yml`，且**能被内核加载**。

    ★2026-09-26（§九 第一档②）：本类原叫 `…ProducesARealCase` 并断言"写出 `cases/<case>.yml`"。
    默认落点改了之后，"装配成功"的**证据**就是草稿区里那份——`cases/` 必须保持干净。
    """

    def test_writes_draft_yaml_and_records_coverage(self):
        result = assemble(_draft(CLEAN_ASSERTIONS), DOC)

        self.assertTrue(result.ok(), result.to_dict())
        self.assertTrue(os.path.isfile(result.draft_yaml_path), result.draft_yaml_path)
        self.assertTrue(os.path.isfile(result.draft_path))
        self.assertTrue(result.draft_only, "产物只该在草稿区（未转正）")
        self.assertEqual(result.yaml_path, "", "★装配不该写 `cases/`（§九 第一档②）")
        self.assertEqual(glob.glob("cases/*.yml"), [], "★默认落点必须是草稿区")
        self.assertEqual(result.quality.status, "static_valid")
        self.assertEqual(result.coverage.assertion_count, 3)
        self.assertEqual(result.coverage.dropped_assertions, ())

    def test_emitted_case_passes_the_kernel_loader(self):
        """★关键：产物必须能过内核 `load_testcase_file`（否则"装配成功"毫无意义）。"""
        from interfacetester.loader import load_testcase_file  # noqa: PLC0415

        result = assemble(_draft(CLEAN_ASSERTIONS), DOC)

        case = load_testcase_file(result.draft_yaml_path)  # 抛异常即失败
        self.assertTrue(case)

    def test_unknowns_file_is_written_only_when_there_are_unknowns(self):
        clean = assemble(_draft(CLEAN_ASSERTIONS), DOC)
        self.assertEqual(clean.unknowns_path, "", "没有 unknowns 时不该落清单文件")

        dirty_assertions = CLEAN_ASSERTIONS + [
            {
                "comparator": "equal",
                "check": "body.price",
                "expect": 1,
                "source_quote": "amount：整数（int），单位分",
            }
        ]
        dirty = assemble(_draft(dirty_assertions, case_name="含未知"), DOC)
        self.assertTrue(dirty.unknowns_path, "有 unknowns 时必须落清单")
        with open(dirty.unknowns_path, encoding="utf-8") as fp:
            payload = json.load(fp)
        self.assertEqual(payload["count"], len(dirty.unknowns))
        self.assertIn(payload["items"][0]["level"], {"degrade", "reject"})

    def test_dropped_assertion_does_not_reach_the_yaml(self):
        """被降级的断言**不许出现在用例里**（否则它的失败看起来和真缺陷一样）。"""
        assertions = CLEAN_ASSERTIONS + [
            {
                "comparator": "equal",
                "check": "body.price",
                "expect": 1,
                "source_quote": "amount：整数（int），单位分",
            }
        ]
        result = assemble(_draft(assertions), DOC)

        with open(result.draft_yaml_path, encoding="utf-8") as fp:
            yaml_text = fp.read()
        self.assertIn("body.amount", yaml_text)
        self.assertNotIn("body.price", yaml_text, "被降级的断言漏进了用例")

    def test_assemble_never_touches_an_existing_case(self):
        """★落点纪律（§九 第一档②）：装配**不碰** `cases/`。

        所以"覆盖人工用例"在装配阶段已经**不可能发生**；查重（不覆盖人工文件）仍在
        转正入口 `promote_draft()`（T24）——那里才是唯一写 `cases/` 的地方。
        """
        os.makedirs(CASES_DIR, exist_ok=True)
        with open(os.path.join(CASES_DIR, "hand_written.yml"), "w", encoding="utf-8") as fp:
            fp.write("# 人手写的用例，别动我\n")

        first = assemble(_draft(CLEAN_ASSERTIONS), DOC)
        second = assemble(_draft(CLEAN_ASSERTIONS), DOC)  # 同名再装配一次

        with open(os.path.join(CASES_DIR, "hand_written.yml"), encoding="utf-8") as fp:
            self.assertEqual(fp.read(), "# 人手写的用例，别动我\n", "人工用例被改了")
        self.assertTrue(os.path.isfile(first.draft_yaml_path))
        self.assertTrue(os.path.isfile(second.draft_yaml_path))
        self.assertEqual(
            [
                path
                for path in glob.glob(os.path.join(CASES_DIR, "*.yml"))
                if not path.endswith("hand_written.yml")
            ],
            [],
            "装配往 cases/ 里写了东西",
        )


class TestTrapIsRefusedBeforeCases(_TempWorkspace):
    """★S-4 的实质判据：**带陷阱的产物不落 `cases/`**，且落点给出人工指引。"""

    def test_trap_case_never_lands_in_cases(self):
        result = assemble(_draft(TRAP_ASSERTIONS, case_name="带陷阱"), DOC)

        self.assertTrue(result.rejected)
        self.assertEqual(result.yaml_path, "", "被拒的用例不该有 cases/ 路径")
        self.assertEqual(glob.glob(os.path.join(CASES_DIR, "*.yml")), [], "带陷阱的产物落进了 cases/")

    def test_needs_human_lists_what_to_do_next(self):
        result = assemble(_draft(TRAP_ASSERTIONS, case_name="带陷阱"), DOC)

        self.assertTrue(os.path.isfile(result.needs_human_path), result.needs_human_path)
        with open(result.needs_human_path, encoding="utf-8") as fp:
            text = fp.read()
        self.assertIn("没有进入 `cases/`", text)
        self.assertIn("S1", text, "NEEDS_HUMAN 必须写清是哪一个闸门编号")
        self.assertIn("覆盖口径", text)


class TestT28DropAccounting(unittest.TestCase):
    """★(c)④-②：降级**按原因分类** + 降级率——让"引用质量"可观测。

    起因（实测）：真模型在**真实文档**上给的 75 条断言有 **30 条（40%）** 被 T28 拦下，
    而在 15 份 golden 上是 **0 降级**。没有这两个数字，那类退化只能靠人去 grep 原始草案。
    ★口径：`dropped_kinds` 计的是**命中项**（一条断言可能命中多条对账），
    而"降级**条数**"看 `dropped_assertions`（**按断言**记）——两者分开报。
    """

    REF_MISS = [
        {
            "comparator": "equal",
            "check": "body.amount",
            "expect": 1,
            "source_quote": "这句话文档里根本没有",
        }
    ]

    def test_reasons_are_counted_by_kind(self):
        coverage = draft_to_ircase(_draft(self.REF_MISS), doc_text=DOC).coverage

        self.assertEqual([item[0] for item in coverage.dropped_kinds], ["quote-miss"])
        self.assertEqual(len(coverage.dropped_assertions), 1)
        self.assertEqual(
            coverage.drop_rate(), 1.0, "0 条留下 + 1 条拦下 → 降级率 100%"
        )

    def test_drop_rate_is_none_when_there_is_nothing_to_judge(self):
        """★分母 0 → `None`（不许把"没有断言可评"写成 0%）。"""
        coverage = Coverage()

        self.assertIsNone(coverage.drop_rate())
        self.assertEqual(coverage.dropped_summary(), "")

    def test_render_carries_rate_and_reasons(self):
        coverage = Coverage(
            assertion_count=4,
            dropped_assertions=("steps[0].validate[0]",),
            dropped_kinds=(("field-not-in-quote", 2),),
            documented_fields=("amount",),
        )

        line = coverage.render()

        self.assertIn("本次降级/拒掉 1 条", line)
        self.assertIn("25%", line, "降级率 = 1 拦下 / **提议的 4 条**")
        self.assertIn("field-not-in-quote×2", line)
        self.assertIn("原因：", line)

    def test_denominator_is_the_proposed_count_not_double_counted(self):
        """★分母是**提议条数**（含被降级的），**不是**"留下的 + 被丢的"——后者是双重计数。"""
        coverage = Coverage(assertion_count=2, dropped_assertions=("a", "b"))

        self.assertEqual(coverage.drop_rate(), 1.0, "2 条全被拦 → 100%，不是 50%")

    def test_render_stays_quiet_when_nothing_was_dropped(self):
        """成对判据：**没有降级**时不许印比例（避免"0% 看起来像有指标"）。"""
        coverage = Coverage(assertion_count=5, documented_fields=("amount",))

        line = coverage.render()

        self.assertNotIn("%", line)
        self.assertNotIn("原因：", line)


BIND_DOC = """# 下单接口文档

## 1. 统一响应格式

#### 成功响应

**响应示例**:

```json
{"code": 0, "message": "下单成功"}
```

## POST /api/order

返回 200 表示成功。

### 请求体

| 字段名 | 类型 | 说明 |
| --- | --- | --- |
| `amount` | int | 金额 |
| `coupon` | string | 优惠券 |

## POST /api/order/cancel

取消订单。

### 请求体

| 字段名 | 类型 | 说明 |
| --- | --- | --- |
| `cancelNote` | string | 取消备注 |
"""


def _bind_scope():
    """本用例自己的那一节（`## POST /api/order`）——与 `gen` 传给装配器的 `scope` 同口径。"""
    from interfacetester_ai.slicer import heading_spans  # noqa: PLC0415

    span = next(item for item in heading_spans(BIND_DOC) if item.title == "POST /api/order")
    return (span.start_line, span.end_line), span


class TestEvidenceBinding(unittest.TestCase):
    """★WP2.2：**证据绑定**（引用引歪了 → 绑到文档里承载该事实的那一节）与它的**记账**。

    全部走**真装配链**（`draft_to_ircase`，不打桩），因为要判的正是"记账有没有跟着产物走"。
    """

    def _assemble(self, assertion):
        scope, _span = _bind_scope()
        return draft_to_ircase(_draft([assertion]), doc_text=BIND_DOC, scope=scope)

    @staticmethod
    def _kinds(outcome):
        return [item.kind for item in outcome.unknowns]

    def test_heading_quote_is_bound_kept_and_recorded(self):
        """引用写成**标题**（`请求体`）→ 绑到那一节 → 断言活着，且**留痕**。"""
        outcome = self._assemble(
            {
                "comparator": "type_match",
                # ★§9.32：归属**按路径**判，这里必须写文档里那一层 —— 本夹具的「请求体」表
                #   **没有「必填」列**（按口径 B2 它就是一张**响应数据表**），字段挂在 `data` 下，
                #   所以断言对象是 `body.data.amount`；写成 `body.amount` 会被闸门判"层级取错"并摘掉
                #   （以前按**名字**对，`amount` 对得上就放行 —— 那正是 §9.32 修掉的必红形态）。
                "check": "body.data.amount",
                "expect": "int",
                "source_quote": "请求体",
            }
        )

        self.assertEqual(
            [step.assertions for step in outcome.ir_case.steps][0],
            [{"type_match": ["body.data.amount", "int"]}],
            "绑定后断言必须真的进用例",
        )
        self.assertEqual(len(outcome.coverage.bound_assertions), 1)
        self.assertIn("quote-bound", self._kinds(outcome), "绑定必须进 unknowns（交付物可见）")
        self.assertIn("证据是工具绑定的", outcome.coverage.render())
        self.assertIn("第", outcome.coverage.bound_assertions[0])

    def test_without_binding_the_same_assertion_is_dropped(self):
        """★**成对判据**：不启用绑定时（`bound=None`）老行为一字不变 —— 仍按句级降级。"""
        from interfacetester_ai.assembler import audit_assertion, find_sample_values  # noqa: PLC0415
        from interfacetester_ai.schema import DraftAssertion  # noqa: PLC0415

        assertion = DraftAssertion(
            comparator="type_match", check="body.amount", expect="int", source_quote="请求体"
        )
        found = audit_assertion(
            assertion,
            quote=assertion.source_quote,
            doc_text=BIND_DOC,
            sample_values=find_sample_values(BIND_DOC),
            where="t",
        )

        self.assertTrue(found, "句级判据下这条引用站不住（`amount` 不在「请求体」这三个字里）")

    def test_shared_section_outside_the_slice_is_allowed_and_marked(self):
        """片外的**共享节**（统一响应格式）允许绑定，但必须标出来不是本片的事实。"""
        outcome = self._assemble(
            {
                "comparator": "type_match",
                "check": "body.message",
                "expect": "str",
                "source_quote": "成功响应",
            }
        )

        self.assertEqual(len(outcome.coverage.bound_assertions), 1)
        self.assertIn("全篇共享", outcome.coverage.bound_assertions[0])

    def test_another_interface_section_is_refused(self):
        """★归属闸门：事实只出现在**别的接口**那一段里（含它的字段表子节）→ 拒绝绑定、仍降级。"""
        outcome = self._assemble(
            {
                "comparator": "type_match",
                "check": "body.cancelNote",
                "expect": "str",
                "source_quote": "取消备注",
            }
        )

        self.assertEqual(outcome.coverage.bound_assertions, ())
        self.assertEqual(len(outcome.ir_case.steps[0].assertions), 0, "断言不该进用例")
        self.assertIn("field-not-in-quote", self._kinds(outcome))

    def test_sample_value_drop_leaves_no_dangling_binding_record(self):
        """★**不许悬空**：绑了但随后被 ④（示例值硬编码）拦下的断言，**不许**留下绑定记录。"""
        outcome = self._assemble(
            {
                "comparator": "equal",
                "check": "body.message",
                "expect": "下单成功",
                "source_quote": "成功响应",
            }
        )

        self.assertEqual(len(outcome.ir_case.steps[0].assertions), 0)
        self.assertEqual(
            outcome.coverage.bound_assertions, (), "产物里没有这条断言，报告就不该说它绑过"
        )
        self.assertIn("sample-value-hardcoded", self._kinds(outcome))

    def test_quote_bound_upgrades_to_the_unknown_critical_blocker(self):
        """`quote-bound` 不是 blocker 码本身 —— 它升级成 **`unknown-critical`**，转正要点名处置。

        ★走**审核者那条路**：`unknowns` → `.unknowns.json` 的 payload 形状 → `assess_from_artifacts`
        （`quality.unknown_views` 只认 dict，这正是交付物的形态）。
        """
        from interfacetester_ai.quality import BLOCKER_UNKNOWN_CRITICAL, assess_from_artifacts  # noqa: PLC0415

        outcome = self._assemble(
            {
                "comparator": "type_match",
                "check": "body.amount",
                "expect": "int",
                "source_quote": "请求体",
            }
        )
        payload = {
            "case": "下单接口",
            "count": len(outcome.unknowns),
            "items": [item.to_dict() for item in outcome.unknowns],
        }
        assessment = assess_from_artifacts("config: {}", payload)

        self.assertIn(BLOCKER_UNKNOWN_CRITICAL, assessment.blockers)
        self.assertEqual(assessment.status, "needs_review")


# ★§9.15 的夹具：响应侧有据（`success`/`message`/`data` 都在成功响应示例里），
#   这样唯一能把断言摘掉的理由就是"**期望值语义**"本身（与归属闸门解耦，判据才干净）。
SEMANTICS_DOC = """# 示例服务

## POST /api/order

**成功响应**：

```json
{"success": true, "message": "操作成功", "data": {"id": "1"}}
```

- `message`：字符串，**固定值**「操作成功」
"""


class TestExpectSemantics(unittest.TestCase):
    """★§9.15 **期望值语义闸**：形态合法（过 T28 四类）但**讲不通**的断言，摘掉并留痕。

    每一条都配**成对判据**——既挡住错的那一侧，也证明对的那一侧没被误伤。
    """

    def _assemble(self, comparator, check, expect, quote="成功响应", doc=SEMANTICS_DOC):
        return draft_to_ircase(
            _draft([{"comparator": comparator, "check": check, "expect": expect, "source_quote": quote}]),
            doc_text=doc,
        )

    @staticmethod
    def _kept(outcome):
        return [item for step in outcome.ir_case.steps for item in step.assertions]

    @staticmethod
    def _kinds(outcome):
        return [item.kind for item in outcome.unknowns]

    def test_string_only_comparator_with_a_bool_expect_is_dropped(self):
        """`string_equals` 配布尔 → 内核字符串守卫当场报错（跑起来必红）。"""
        outcome = self._assemble("string_equals", "body.success", True)

        self.assertEqual(self._kept(outcome), [])
        self.assertIn("expect-semantics", self._kinds(outcome))
        self.assertTrue(
            any("只比较**字符串**" in item.message for item in outcome.unknowns),
            [item.message for item in outcome.unknowns],
        )

    def test_a_leaf_string_assertion_is_left_alone(self):
        """★成对：`string_equals` 配**字符串**、对象是**字段**（不是整包）→ 一字不动。"""
        outcome = self._assemble(
            "string_equals",
            "body.message",
            "操作成功",
            quote="- `message`：字符串，**固定值**「操作成功」",
        )

        self.assertEqual(
            self._kept(outcome), [{"string_equals": ["body.message", "操作成功"]}]
        )
        self.assertNotIn("expect-semantics", self._kinds(outcome))

    def test_whole_body_compared_with_a_string_is_dropped(self):
        """`string_equals: [body, 'true']`：整包是 JSON 对象，比对永远不成立。"""
        outcome = self._assemble("string_equals", "body", "true")

        self.assertEqual(self._kept(outcome), [])
        self.assertIn("expect-semantics", self._kinds(outcome))

    def test_plain_text_response_is_exempt(self):
        """★成对：文档明说响应是**二进制/纯文本** → "整包当字符串比"说得通 → 放行。"""
        doc = SEMANTICS_DOC + "\n- `Content-Type`: application/octet-stream\n- Body: 文件二进制流\n"
        outcome = self._assemble(
            "string_equals", "body", "文件二进制流", quote="Body: 文件二进制流", doc=doc
        )

        self.assertEqual(self._kept(outcome), [{"string_equals": ["body", "文件二进制流"]}])
        self.assertNotIn("expect-semantics", self._kinds(outcome))

    def test_contains_on_the_whole_body_with_a_value_is_dropped(self):
        """★③-b（2026-09-27 更正）：`contains` 对**字典**找的是**键**，拿"取值"去查 → 永不命中。

        实测证据：mock 按文档规则回了 `{"success": false, "errorCode": "FILE_1018", ...}`，
        而 `contains: [body, 'FILE_1018']` 仍然判红 ✗ —— 早先写的"可能真的成立"**前提错了**。
        """
        outcome = self._assemble(
            "contains",
            "body",
            "操作成功",
            quote="- `message`：字符串，**固定值**「操作成功」",
        )

        self.assertEqual(self._kept(outcome), [])
        self.assertIn("expect-semantics", self._kinds(outcome))
        self.assertTrue(
            any("找的是**键**" in item.message for item in outcome.unknowns),
            [item.message for item in outcome.unknowns],
        )

    def test_contains_with_a_documented_key_is_left_alone(self):
        """★成对：`expect` 是**文档化的键名** → `contains` 语义正确 → 一字不动。"""
        outcome = self._assemble("contains", "body", "message", quote="成功响应")

        self.assertEqual(self._kept(outcome), [{"contains": ["body", "message"]}])
        self.assertNotIn("expect-semantics", self._kinds(outcome))

    def test_json_fragment_as_a_contains_expect_is_dropped(self):
        """`contains: [body, '"success": true']`：对字典找的是**键/元素**，JSON 片段永远命中不了。"""
        outcome = self._assemble(
            "contains", "body", '"success": true', quote='成功响应: {"success": true}'
        )

        self.assertEqual(self._kept(outcome), [])
        self.assertIn("expect-semantics", self._kinds(outcome))

    def test_contains_is_not_judged_when_there_is_no_json_object(self):
        """★成对（判不了就不判）：本节**没有** JSON 对象响应示例 → 不知道是不是字典 → 放行。"""
        outcome = self._assemble(
            "contains",
            "body",
            "操作成功",
            quote="- `message`：字符串，**固定值**「操作成功」",
            doc="# 示例服务\n\n## POST /api/order\n\n- `message`：字符串，**固定值**「操作成功」\n",
        )

        self.assertEqual(self._kept(outcome), [{"contains": ["body", "操作成功"]}])
        self.assertNotIn("expect-semantics", self._kinds(outcome))

    def test_negative_step_bool_assertion_is_not_judged(self):
        """★刻意不判：`equal: [body.success, false]` 在**负例步骤**里完全正当 → 放行。"""
        outcome = self._assemble("equal", "body.success", False)

        self.assertEqual(self._kept(outcome), [{"equal": ["body.success", False]}])
        self.assertNotIn("expect-semantics", self._kinds(outcome))


class TestSchemaVsExampleConflict(unittest.TestCase):
    """★§9.17（装配器侧）：**模型产的** `jsonschema_match` 与**文档自己的**响应示例矛盾 → 摘掉。

    实测起因：模型跟着"响应数据表"写出 `data: object`，而文档**自己的示例**写着 `data: [...]`
    → 断言必然失败，看起来却像"接口坏了"（分叉型静默）。成对判据：不矛盾的照旧放行、判不了的不管。
    """

    CONFLICT_DOC = """# 示例服务

## POST /api/order

**成功响应**：

```json
{"code": 0, "data": [{"sku": "SKU-0001"}]}
```
"""

    @staticmethod
    def _assemble(schema, doc, quote="**成功响应**："):
        return draft_to_ircase(
            _draft(
                [
                    {
                        "comparator": "jsonschema_match",
                        "check": "body",
                        "expect": schema,
                        "source_quote": quote,
                    }
                ]
            ),
            doc_text=doc,
        )

    def test_schema_conflicting_with_the_example_is_dropped(self):
        outcome = self._assemble({"type": "object", "properties": {"data": {"type": "object"}}}, self.CONFLICT_DOC)

        self.assertEqual(
            [item for step in outcome.ir_case.steps for item in step.assertions],
            [],
            "★模型产的 schema 与文档示例冲突，却照样进了用例（跑起来必红）",
        )
        self.assertIn("doc-schema-conflict", [item.kind for item in outcome.unknowns])

    def test_matching_schema_is_left_alone(self):
        """★成对：schema 与示例**一致**（`data` 是对象）→ 一字不动。"""
        doc = self.CONFLICT_DOC.replace('"data": [{"sku": "SKU-0001"}]', '"data": {"sku": "SKU-0001"}')
        outcome = self._assemble({"type": "object", "properties": {"data": {"type": "object"}}}, doc)

        self.assertEqual(
            [list(item) for step in outcome.ir_case.steps for item in step.assertions],
            [["jsonschema_match"]],
        )
        self.assertNotIn("doc-schema-conflict", [item.kind for item in outcome.unknowns])

    def test_no_example_means_we_do_not_judge(self):
        """★成对（判不了就不判）：本节**没有** JSON 响应示例 → 放行。"""
        quote = "**调用示例**：curl -X POST https://h/api/order，响应里 `data` 是对象"
        outcome = self._assemble(
            {"type": "object", "properties": {"data": {"type": "object"}}},
            f"# 示例服务\n\n## POST /api/order\n\n{quote}\n",
            quote=quote,
        )

        self.assertEqual(
            [list(item) for step in outcome.ir_case.steps for item in step.assertions],
            [["jsonschema_match"]],
        )

    def test_boolean_and_integer_are_mutually_tolerated(self):
        """★成对（避免误伤）：示例里是 `1`、schema 写 `boolean` → 互认 → 放行。"""
        doc = self.CONFLICT_DOC.replace('"data": [{"sku": "SKU-0001"}]', '"flag": 1')
        outcome = self._assemble({"type": "object", "properties": {"flag": {"type": "boolean"}}}, doc)

        self.assertEqual(
            [list(item) for step in outcome.ir_case.steps for item in step.assertions],
            [["jsonschema_match"]],
        )


class TestTypeMatchExpectMustBeGateLegal(_TempWorkspace):
    """★S7 的类型名：`type_match` 的期望值必须**闸门认**，否则**整条用例**被 REJECT。

    实测来源：真实客户文档的字段表写 `number`，而 S7 只认内置类型名（`int`/`str`/`float`…）
    —— 模型照抄文档就会被拒（§9.7 那轮 18 片里**有 1 片的失败正是它**）。
    处置口径：**能确定的确定下来**（无歧义别名归一），**不能确定的响亮点名**
    （有歧义的 `number` 摘下来交人工），而**用例照常产出** —— 一条断言的**名字**不该把
    整条用例打死（覆盖率就是这么丢的）。
    """

    ALIASES = {"string": "str", "字符串": "str", "integer": "int", "整数": "int",
               "对象": "dict", "数组": "list", "boolean": "bool", "double": "float"}

    def test_unambiguous_alias_is_normalised(self):
        for raw, expected in self.ALIASES.items():
            with self.subTest(expect=raw):
                normalized, problem = normalize_type_match(
                    DraftAssertion(
                        comparator="type_match", check="body.x", expect=raw, source_quote="q"
                    )
                )

                self.assertIsNone(problem, f"{raw} 是无歧义别名，不该交人工")
                self.assertEqual(normalized.expect, expected)

    def test_legal_names_are_left_alone(self):
        """★成对判据：闸门认的类型名**一个字都不改**（改了反而可能撞 ③ 限定词检查）。"""
        for name in ("int", "str", "dict", "float", "bool", "list", "None", "NoneType"):
            with self.subTest(expect=name):
                normalized, problem = normalize_type_match(
                    DraftAssertion(
                        comparator="type_match", check="body.x", expect=name, source_quote="q"
                    )
                )

                self.assertIsNone(problem)
                self.assertIs(normalized.expect, name)

    def test_ambiguous_word_is_handed_to_a_human_without_killing_the_case(self):
        """`number` → **不猜**：摘掉该条断言并记账，**其他断言照常进用例**。"""
        outcome = draft_to_ircase(
            _draft(
                [
                    {
                        "comparator": "equal",
                        "check": "status_code",
                        "expect": 200,
                        "source_quote": "返回 200 表示成功",
                    },
                    {
                        "comparator": "type_match",
                        "check": "body.amount",
                        "expect": "number",
                        "source_quote": "amount：整数（int），单位分",
                    },
                ]
            ),
            doc_text=DOC,
        )

        self.assertEqual(
            outcome.ir_case.steps[0].assertions,
            [{"equal": ["status_code", 200]}],
            "被摘的只能是那一条，另一条必须留下",
        )
        kinds = [item.kind for item in outcome.unknowns]
        self.assertIn("type-name-unusable", kinds)
        self.assertEqual(
            [kind for kind, _count in outcome.coverage.dropped_kinds], ["type-name-unusable"]
        )

    def test_the_case_survives_the_gate(self):
        """★端到端：带 `type_match: number` 的草稿**不再被 S7 拒**（闸门结论里没有 S7）。"""
        from interfacetester_ai.reviews import gate_check  # noqa: PLC0415

        result = assemble(
            _draft(
                [
                    {
                        "comparator": "equal",
                        "check": "status_code",
                        "expect": 200,
                        "source_quote": "返回 200 表示成功",
                    },
                    {
                        "comparator": "type_match",
                        "check": "body.amount",
                        "expect": "number",
                        "source_quote": "amount：整数（int），单位分",
                    },
                ]
            ),
            DOC,
        )

        self.assertFalse(result.rejected, result.to_dict())
        _passed, codes = gate_check(result.draft_yaml_path)
        self.assertNotIn("S7", codes, f"闸门仍报了 S7：{codes}")


class TestStaleNeedsHumanIsClearedOnSuccess(_TempWorkspace):
    """★**真实复跑**发现的问题：修正环里**每一轮**被闸门拒都会写 `NEEDS_HUMAN.md`，
    某一轮修好之后那份文件**留在原地** → 同一份工作区里出现两处**互相矛盾**的结论：
    报告说"**0 片失败**"，而 `.ai/failed/` 里挂着「# 需要人工处理：<用例>」。
    留痕的价值在于"它说的是**现在**的结论"，所以：**通过即清，拒了照旧写**。
    """

    def test_success_clears_a_stale_needs_human(self):
        os.makedirs(os.path.join(".ai", "failed", "下单接口"), exist_ok=True)
        with open(
            os.path.join(".ai", "failed", "下单接口", "NEEDS_HUMAN.md"), "w", encoding="utf-8"
        ) as handle:
            handle.write("# 需要人工处理：下单接口\n")

        result = assemble(_draft(CLEAN_ASSERTIONS), DOC)

        self.assertFalse(result.rejected, result.to_dict())
        self.assertFalse(
            os.path.exists(os.path.join(".ai", "failed", "下单接口")),
            "早先轮次留下的 `NEEDS_HUMAN` 必须被清掉（陈旧留痕会与报告自相矛盾）",
        )
        self.assertEqual(result.cleared_needs_human, ".ai/failed/下单接口")
        self.assertEqual(result.to_dict()["cleared_needs_human"], ".ai/failed/下单接口")

    def test_rejection_still_writes_it(self):
        """★**成对判据**：被拒时照旧写下 `NEEDS_HUMAN`（清理不能把真失败也抹掉）。"""
        result = assemble(_draft(TRAP_ASSERTIONS), DOC)

        self.assertTrue(result.rejected)
        self.assertTrue(os.path.isfile(result.needs_human_path), result.needs_human_path)
        self.assertEqual(result.cleared_needs_human, "")

    def test_nothing_to_clear_reports_empty(self):
        result = assemble(_draft(CLEAN_ASSERTIONS), DOC)

        self.assertEqual(result.cleared_needs_human, "")


class TestEndpointMustHaveADocSource(_TempWorkspace):
    """★⑤ 端点溯源（2026-09-25 真模型实测新增）。

    起因：模型为「1.1 服务地址」那段（只有环境 URL 表、**没有任何接口**）造出了
    `GET ${ENV(BASE_URL)}/api/status`，而它的 `source_quote` **照样命中**
    （引的是「服务地址」那句**真话**）—— **"引用句真"不等于"端点真"**。
    那条用例一路进了 `cases/`，跑起来会打到不存在的路径上，
    **而它的失败看起来和真缺陷一模一样**。

    这一条补的正是 T28 前四类的缝：它们**全是断言级**（字段名 / expect 字面量 /
    限定词冲突 / 示例值），**没有一条管 step 的端点**。
    """

    def _endpoint_draft(
        self, url, method="POST", case_name="端点溯源", quote="返回 200 表示成功"
    ):
        return parse_draft(
            {
                "case_name": case_name,
                "steps": [
                    {
                        "name": "一步",
                        "method": method,
                        "url": url,
                        "validate": [
                            {
                                "comparator": "equal",
                                "check": "status_code",
                                "expect": 200,
                                "source_quote": quote,
                            }
                        ],
                    }
                ],
            }
        )

    def test_invented_url_never_lands_in_cases(self):
        """编造的 url → 该 step 被丢弃 → 这个用例**产不出来**（`cases/` 里没有它）。

        ★丢弃之后会发生什么（**不是**"悄悄成功"）：用例变成"零 step"，走到 **L2 出口校验**
        时被内核拦下（`make.py`：`teststeps` 是空列表 ——「零步骤但判成功」，也就是"绿了但
        什么都没测"）。所以这里断言的是**结果**：`cases/` 干净 + `unknowns` 里写清了为什么。
        """
        from interfacetester.exceptions import ParamsError  # noqa: PLC0415

        with self.assertRaises(ParamsError):
            assembler.assemble(self._endpoint_draft("/api/status"), DOC)

        self.assertEqual(glob.glob("cases/*.yml"), [], "编造端点的用例落进了 cases/")

        unknowns = glob.glob(".ai/draft/*.unknowns.json")
        self.assertTrue(unknowns, "丢弃了 step 却没留下 unknowns —— 人看不到为什么")
        with open(unknowns[0], encoding="utf-8") as fp:
            self.assertIn("endpoint-miss", fp.read())

    def test_placeholder_urls_are_left_to_the_gate(self):
        """★成对判据：**没有具体路径段**的 url 交给闸门，不在溯源这一层丢掉。

        `"/"` 是 `assertions.generate_assertions_only` 在文档取不到端点时的**兜底值**；
        丢掉它会让产物变成"零 step"，异常从更靠后的 L2 出口校验抛出、**抛穿 CLI** ——
        退出码从"闸门拒 → 1"变成未捕获异常（实测踩到，`assertions_test` 那条当场变红）。

        ★注意它**不等于**"该用例产不出来"：端点虽然泛（`/`），但它**不假** ——
        只要断言成立，用例照常产出（跑起来打到 `/` 会失败，但那是**看得见**的失败）。
        界线是**危险度**：`/` 不指向任何具体终点，而 `/api/status` 会打到不存在的路径上。
        """
        for index, url in enumerate(("/", "${ENV(BASE_URL)}", "${ENV(BASE_URL)}/")):
            with self.subTest(url=url):
                result = assembler.assemble(
                    self._endpoint_draft(url, case_name=f"占位端点{index}"), DOC
                )

                self.assertTrue(result.ok(), f"{url} 被误伤了：{result.unknowns}")
                # ★§九 第一档②：证据是"草稿区里多了那份"，不是"`cases/` 里多了那份"
                self.assertTrue(os.path.isfile(result.draft_yaml_path), result.draft_yaml_path)
                self.assertNotIn(
                    "endpoint-miss",
                    [item.kind for item in result.unknowns],
                    f"{url} 不是「编造」，不该记成端点无出处",
                )

    def test_has_actual_path_marks_the_boundary(self):
        """`has_actual_path` 是这条判据的**分界线**，单独钉住它。"""
        for url in ("/", "", "${ENV(BASE_URL)}", "${ENV(BASE_URL)}/", "https://x.y/"):
            with self.subTest(url=url):
                self.assertFalse(assembler.has_actual_path(url), f"{url} 不该算有路径")

        for url in ("/api/a", "${ENV(BASE_URL)}/api/a", "https://x.y/api/a", "/a"):
            with self.subTest(url=url):
                self.assertTrue(assembler.has_actual_path(url), f"{url} 应当算有路径")

    def test_documented_endpoint_is_not_harmed(self):
        """★成对判据：文档里**写了**的端点照常产出（别把正常路径一起挡掉）。"""
        result = assembler.assemble(self._endpoint_draft("/api/order"), DOC)

        self.assertTrue(result.ok(), f"合规端点被误伤：{result.unknowns}")
        self.assertTrue(os.path.isfile(result.draft_yaml_path), result.draft_yaml_path)
        self.assertEqual(glob.glob("cases/*.yml"), [], "装配不该写 cases/")

    def test_env_placeholder_is_matched_by_its_path(self):
        """`${ENV(BASE_URL)}/api/order` 靠**路径部分**命中（占位符不是文档里的字面量）。"""
        result = assembler.assemble(
            self._endpoint_draft("${ENV(BASE_URL)}/api/order"), DOC
        )

        self.assertTrue(result.ok(), f"带占位符的合规端点被误伤：{result.unknowns}")

    def test_method_never_mentioned_is_dropped(self):
        """url 有出处、但**动词从没出现过** → 也丢（防"编一个 DELETE"）。"""
        from interfacetester.exceptions import ParamsError  # noqa: PLC0415

        with self.assertRaises(ParamsError):
            assembler.assemble(
                self._endpoint_draft("/api/order", method="DELETE"), DOC
            )

        self.assertEqual(glob.glob("cases/*.yml"), [])

    def test_candidates_are_ordered_and_clean(self):
        """候选串：从最严到最松，且不出现 `//host/path` 这种畸形串。"""
        candidates = assembler.endpoint_candidates("https://unifsp.example.com/api/a")

        self.assertEqual(candidates[0], "https://unifsp.example.com/api/a")
        self.assertIn("/api/a", candidates)
        self.assertFalse([item for item in candidates if item.startswith("//")])


class TestProtocolOnlyAssertions(_TempWorkspace):
    """★"断言全是协议级"这条事实必须**跟着产物走**（2026-09-25 真模型实测新增）。

    起因（实测）：真模型给「2.1 创建目录」产的两条用例，**唯一**的断言是 `status_code 200`，
    而**文档里根本没有 `200`** —— 它是靠 `_PROTOCOL_CHECKS` 豁免进来的（豁免本身是对的：
    "返回 200 表示成功"这类话里本来就不会出现 `status_code` 这个词）。
    问题出在**报告口径**，两处都失真：
      ① 覆盖率把它算成"覆盖字段 1 个"，于是"覆盖 1 / 文档声明 46"读起来像"覆盖了一点"，
         **实际是覆盖了 0 个文档事实**（"绿了但什么都没测"的另一种形态）；
      ② 那个 46 是**算出来的、不是量出来的**（`covered + uncovered` 反推）—— 文档实际
         声明 45 个，虚高的 1 正好就是它。

    ★处置是 DEGRADE 而不是 REJECT：这类用例能挡住 4xx/5xx，**有用，只是薄** ——
    所以是"写进 `unknowns` 交人工"，不是"拒产"。
    """

    PROTOCOL_ONLY = [
        {
            "comparator": "equal",
            "check": "status_code",
            "expect": 200,
            "source_quote": "返回 200 表示成功",
        }
    ]

    def test_status_code_is_not_documented_field_coverage(self):
        cov = draft_to_ircase(_draft(self.PROTOCOL_ONLY), doc_text=DOC).coverage

        self.assertEqual(cov.covered_fields, (), "status_code 不是文档字段，不该算进覆盖")
        self.assertEqual(cov.protocol_assertions, ("status_code",))

    def test_report_names_the_denominator_it_measured(self):
        """分母与"协议级"都必须在**同一行**里说清（否则读起来仍是虚高）。"""
        line = draft_to_ircase(_draft(self.PROTOCOL_ONLY), doc_text=DOC).coverage.render()

        self.assertIn(
            f"覆盖字段 0 个（文档声明字段 {len(documented_fields(DOC))} 个）", line
        )
        self.assertIn("另有**协议级断言** 1 条", line)
        self.assertIn("status_code", line)

    def test_the_fact_travels_with_the_artifact(self):
        """★本轮改动（§九 第一档②③）：仅协议断言的产物**只落草稿**，且质量状态是「待人工」。

        改造前它满足 `ok()` 并**落进 `cases/`**（实证 `cases/T1_2_认证方式.yml`，零审核留痕）
        —— 那正是"会冒充已审核"的形态。现在它：
        ① 带 `protocol-only` unknown（人看得到"全是协议级"）；② `draft_only`；
        ③ `cases/` 干净；④ 质量状态 `needs_review` + blocker 码可见。
        """
        from interfacetester_ai.quality import QualityStatus  # noqa: PLC0415

        result = assemble(_draft(self.PROTOCOL_ONLY, case_name="只有协议级"), DOC)

        self.assertIn(
            "protocol-only",
            [item.kind for item in result.unknowns],
            "产物旁边那份 unknowns 里没写「断言全是协议级」",
        )
        path = ".ai/draft/只有协议级.unknowns.json"
        self.assertTrue(os.path.exists(path), f"unknowns 没落盘：{path}")
        self.assertTrue(result.draft_only, "产物只该在草稿区")
        self.assertEqual(glob.glob("cases/*.yml"), [], "★仅协议断言的产物不许进 cases/")
        self.assertEqual(result.quality.status, QualityStatus.NEEDS_REVIEW)
        self.assertIn("protocol-only", result.quality.blockers)
        self.assertFalse(result.quality.is_usable(), "★它不许被当成「可直接使用」")
        with open(path, encoding="utf-8") as fp:
            self.assertIn("protocol-only", fp.read())

    def test_one_documented_assertion_silences_it(self):
        """★成对判据：只要有一条**文档级**断言，就不该再报"全是协议级"。"""
        result = assemble(_draft(CLEAN_ASSERTIONS, case_name="有文档级"), DOC)

        self.assertNotIn("protocol-only", [item.kind for item in result.unknowns])


class TestOnlyRegisteredRootsAreTouched(_TempWorkspace):
    """装配器只许写 `cases/` 与 `.ai/`（T18 注册表 `("roots", ("cases/", ".ai/"))`）。

    这条比"零污染"更精确：装配器**本来就该**产出文件，判据是"**产出的位置**对不对"。
    """

    def test_new_files_all_live_under_cases_or_dot_ai(self):
        before = self._tree()
        assemble(_draft(CLEAN_ASSERTIONS), DOC)
        after = self._tree()

        new = [path for path in after if path not in before]
        self.assertTrue(new, "装配应当产出文件")

        stray = [
            path for path in new if not (path.startswith("cases/") or path.startswith(".ai/"))
        ]
        self.assertEqual(stray, [], f"落到了登记根之外：{stray}")


class TestMetaGuardrails(unittest.TestCase):
    """判据自检：这套断言真的在检查东西。"""

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_the_optional_rule_is_neutered(self):
        """注入：把"可选↔必填"规则掐掉 → 自检必须变红（否则那些判据是摆设）。"""
        with mock.patch.object(assembler, "check_optional_conflict", lambda *a, **k: None):
            self.assertNotEqual(run_selftest(), 0, "限定词规则失效后自检竟然还是绿的")

    def test_selftest_turns_red_when_word_boundary_becomes_substring(self):
        """注入：把词边界判定换成子串判定 → 自检必须变红（T21 的血债）。"""
        with mock.patch.object(
            assembler, "_word_boundary_hit", lambda needle, haystack: needle in (haystack or "")
        ):
            self.assertNotEqual(run_selftest(), 0, "词边界判据失效后自检竟然还是绿的")


class TestCliExitCode(unittest.TestCase):
    """`python -m interfacetester_ai.assembler` 在重定向 + 无 `PYTHONIOENCODING` 下退出码 0。"""

    def test_module_selftest_exits_zero(self):
        import subprocess  # noqa: PLC0415

        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)

        proc = subprocess.run(
            [sys.executable, "-m", "interfacetester_ai.assembler"],
            cwd=BASE,
            capture_output=True,
            timeout=180,
            env=env,
        )

        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        self.assertIn("装配器自检全部通过".encode("utf-8"), proc.stdout)


class TestRequestPlaceholdersAreAlsoRegistered(unittest.TestCase):
    """★§9.16：**请求值**里的 `${ENV(TODO_…)}` 也要登记成 PENDING。

    缺口形态（改造前）：`todo_findings` 只扫 `validate[].expect` —— 请求体里那堆
    `${ENV(TODO_…)}` 在**报告与 `.ai/pending/` 里完全看不见** ✗，只有 `promote_draft` 会拒。
    于是"这条用例其实还跑不起来"这件事**只躲在一份转正失败的提示里**。
    """

    @staticmethod
    def _draft(request_value):
        return parse_draft(
            {
                "case_name": "下单接口",
                "steps": [
                    {
                        "name": "下单",
                        "method": "post",
                        "url": "/api/order",
                        "json": {"bizNo": request_value},
                        "validate": [],
                    }
                ],
            }
        )

    def test_todo_in_the_request_body_becomes_a_pending_item(self):
        from interfacetester_ai.assembler import todo_findings  # noqa: PLC0415

        items = todo_findings(self._draft("${ENV(TODO_BIZNO)}"))

        self.assertEqual(len(items), 1, [item.to_dict() for item in items])
        self.assertIn("json", items[0].where, "待办必须**点名落在请求的哪个字段**里")
        self.assertIn("请求", items[0].message)

    def test_a_filled_request_value_is_not_registered(self):
        """★成对：值是填好的（`${ENV(BIZNO)}` 或字面量）→ 不许登记成待办（假报会让真报被无视）。"""
        from interfacetester_ai.assembler import todo_findings  # noqa: PLC0415

        self.assertEqual(todo_findings(self._draft("${ENV(BIZNO)}")), [])
        self.assertEqual(todo_findings(self._draft("SO-2026-0001")), [])


if __name__ == "__main__":
    unittest.main()
