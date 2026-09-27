# -*- coding: utf-8 -*-
"""合并（**模型草稿 × 确定性映射**）的判据 —— 决策清单 §三 **C-2** 的 10 条。

口径唯一来源：`interfacetester_ai/merge.py` 的 docstring（只补不覆盖 / 多步不合并 / 按行号绑定；
默认开 + 两条回滚）。这里逐条把它变成**能红**的判据，并留一条**注入式元护栏**。

- 判据若只写"合并后断言数变多"，把合并换成"原样返回"时**不一定**红（可能本来就有断言）——
  所以第 1 条同时钉住三件事：**新增的必须是字段级**、**模型原有的必须逐字节不变**、**总数必须变多**；
- 元护栏 `test_guard_would_catch_a_no_op_merge` 用"注入空合并"证明这套口径不是空跑。
"""

import os
import sys
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai import merge as merge_module  # noqa: E402
from interfacetester_ai.merge import (  # noqa: E402
    MERGE_ENV,
    MergeOutcome,
    merge_derived_assertions,
    merge_enabled,
)
from interfacetester_ai.schema import CaseDraft, DraftAssertion, DraftStep  # noqa: E402

# 行号账（下面的 slice 范围都按它写）：
#   1 # 下单接口 / 3 ## POST /api/order / 5~9 字段表（7 sku、8 quantity、9 sort）
#   13 - data.orderNo：字符串，订单号
DOC = """# 下单接口

## POST /api/order

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| sku | string | 是 | 商品编码 |
| quantity | integer | 是 | 数量，1 ~ 99 |
| sort | string | 否 | 枚举：price_asc / price_desc |

响应：

- data.orderNo：字符串，订单号
- data.sku：字符串，商品编码
- data.quantity：整数，数量
"""

# ★2026-09-26（§9.14 响应侧归属）：上面这份响应说明句里**必须**写上 `sku`/`quantity`
#   —— 它们原本只写在**请求**字段表里，归属闸门会让"断言 `body.sku`"变成"断言响应里不存在的字段"。
#   要判的仍然是**合并机制**（确定性映射 × 模型草案），所以要给它们一条**响应侧**的据。
WHOLE_DOC = (1, 16)
GOLDEN_DIR = os.path.join(BASE, "bench", "golden")


def _model_draft(validate=None):
    """模型给的草稿：**只有协议级断言**（这正是 P0a 实测到的形态）。"""
    if validate is None:
        validate = (DraftAssertion("equal", "status_code", 200, "POST /api/order"),)
    return CaseDraft(
        case_name="下单",
        steps=(DraftStep(name="下单", method="POST", url="/api/order", validate=validate),),
    )


class TestMergeAddsFieldLevelAssertions(unittest.TestCase):
    """① 只补不覆盖；② 模型断言逐字节不变；③ 补的是字段级。"""

    def test_adds_field_level_but_never_touches_the_model_assertion(self):
        original = _model_draft()
        outcome = merge_derived_assertions(original, DOC, slice_start=WHOLE_DOC[0], slice_end=WHOLE_DOC[1])

        self.assertTrue(outcome.added, "合并后一条都没补 —— 判据（或合并）失效")
        merged = outcome.draft.assertions()
        checks = {item.check for item in merged}
        self.assertIn("body.sku", checks)  # 补进来的必须是**字段级**
        self.assertIn("body.quantity", checks)
        self.assertIn("body.data.orderNo", checks)  # 说明句 `data.orderNo` → 挂在 body. 下
        self.assertEqual(merged[0], original.assertions()[0])  # 模型那条原样还在
        self.assertEqual(merged[0].source_quote, "POST /api/order")
        self.assertGreater(len(merged), len(original.assertions()))

    def test_range_from_the_note_column(self):
        outcome = merge_derived_assertions(_model_draft(), DOC, slice_start=1, slice_end=14)
        pairs = {(item.check, item.comparator) for item in outcome.draft.assertions()}
        self.assertIn(("body.quantity", "greater_or_equals"), pairs)  # 范围下界
        self.assertIn(("body.quantity", "less_or_equals"), pairs)  # 范围上界

    def test_optional_field_is_never_added(self):
        """★继承映射器的自律：可选字段不断言（断言它存在会误报失败）。"""
        outcome = merge_derived_assertions(_model_draft(), DOC, slice_start=1, slice_end=14)
        self.assertNotIn("body.sort", {item.check for item in outcome.draft.assertions()})

    def test_idempotent(self):
        once = merge_derived_assertions(_model_draft(), DOC, slice_start=1, slice_end=14)
        twice = merge_derived_assertions(once.draft, DOC, slice_start=1, slice_end=14)
        self.assertEqual(twice.added, (), "重复合并不该再补一遍（会产生重复断言）")
        self.assertEqual(
            len(twice.draft.assertions()), len(once.draft.assertions()), "断言的条数被合并改动了"
        )


class TestConflictKeepsTheModelVersion(unittest.TestCase):
    """④ 冲突：**保留模型的**，但必须记下来（不自动改、也不静默丢）。"""

    def test_conflicting_value_is_reported_and_model_version_survives(self):
        model = _model_draft(
            validate=(
                DraftAssertion("equal", "status_code", 200, "POST /api/order"),
                DraftAssertion("type_match", "body.sku", "int", "| sku | string | 是 | 商品编码 |"),
            )
        )
        outcome = merge_derived_assertions(model, DOC, slice_start=1, slice_end=14)

        self.assertTrue(outcome.conflicts, "模型与确定性映射对同一字段给出不同值时，必须记冲突")
        sku = [item for item in outcome.draft.assertions() if item.check == "body.sku"]
        self.assertEqual(len(sku), 1, "同字段出现了两条断言（覆盖或重复）")
        self.assertEqual(sku[0].expect, "int", "冲突时应当**保留模型的**版本")
        self.assertIn("冲突", " ".join(outcome.notes()))

    def test_same_assertion_is_not_reported_as_conflict(self):
        model = _model_draft(
            validate=(DraftAssertion("type_match", "body.sku", "str", "| sku | string | 是 | 商品编码 |"),)
        )
        outcome = merge_derived_assertions(model, DOC, slice_start=1, slice_end=14)
        self.assertEqual(outcome.conflicts, (), f"同一条断言被当成了冲突：{outcome.conflicts}")


class TestBindingRules(unittest.TestCase):
    """⑤ 按行号绑定；⑥ 绑定不上的可见；⑦ 多步不合并。"""

    def test_assertions_outside_the_slice_are_not_merged(self):
        # 片只覆盖标题区（1~4 行），字段表在第 7~9 行 → 一条都不该并进来
        outcome = merge_derived_assertions(_model_draft(), DOC, slice_start=1, slice_end=4)
        self.assertEqual(outcome.added, (), "片外的断言被并进来了（会绑到别的接口上）")
        self.assertTrue(outcome.skipped, "片外断言必须**可见**地记下来，不能静默丢")
        self.assertIn("不在本接口段", " ".join(outcome.skipped))

    def test_owner_ranges_lets_the_interface_section_own_its_subsections(self):
        """★2026-09-26 修正的主口径：**接口段 + 它名下的子节**（真模型实测踩出来的）。

        golden 的写法是 `## POST /api/order` → `### 请求体`（字段表在子节里，子节被判"非接口片"跳过），
        所以"行号 ∈ 接口片"**必然绑不上**；按 `title_path` 前缀把子节也算进来才对。
        """
        outcome = merge_derived_assertions(
            _model_draft(), DOC, owner_ranges=[(3, 4), (5, 12)]
        )
        self.assertTrue(outcome.added, f"按 owner_ranges 该补出字段级断言，实际：{outcome.skipped}")
        checks = {item.check for item in outcome.draft.assertions()}
        self.assertIn("body.sku", checks)
        self.assertIn("body.quantity", checks)

    def test_owner_ranges_still_excludes_other_interfaces_sections(self):
        """反向：`owner_ranges` 只认本接口段的子节——别的接口段的字段表**不许**并进来。"""
        outcome = merge_derived_assertions(
            _model_draft(), DOC, owner_ranges=[(3, 4)]  # 只有接口段本身，不含 5~12 的子节
        )
        self.assertEqual(outcome.added, (), "把别的/未归属章节的断言并进来了")
        self.assertIn("不在本接口段", " ".join(outcome.skipped))

    def test_guard_would_catch_owner_ranges_being_ignored(self):
        """元护栏：把 `owner_ranges` 当空气（只按本片行号判）→ 上面那条正向判据必须红。"""
        ignored = merge_derived_assertions(_model_draft(), DOC, slice_start=3, slice_end=4)
        honored = merge_derived_assertions(_model_draft(), DOC, owner_ranges=[(3, 4), (5, 12)])
        self.assertEqual(ignored.added, (), "只按本片行号判时不该补出东西")
        self.assertTrue(
            honored.added,
            "不认 owner_ranges 时一条都补不出 —— 「认了就能补」这句话才成立",
        )

    def test_unknown_line_number_is_not_merged(self):
        ghost = mock.Mock(
            comparator="type_match",
            check="body.x",
            expect="str",
            source_quote="x",
            reason="合成",
            needs_human=False,
            line_no=0,
        )
        with mock.patch.object(merge_module, "derive_assertions", return_value=([ghost], [])):
            outcome = merge_derived_assertions(_model_draft(), DOC, slice_start=1, slice_end=14)
        self.assertEqual(outcome.added, (), "行号未知的断言不该被合并（可能绑错片）")
        self.assertIn("行号未知", " ".join(outcome.skipped))

    def test_multi_step_draft_is_left_alone(self):
        draft = CaseDraft(
            case_name="两步",
            steps=(
                DraftStep(
                    "第一步", "GET", "/a",
                    (DraftAssertion("equal", "status_code", 200, "GET /a"),),
                ),
                DraftStep(
                    "第二步", "POST", "/b",
                    (DraftAssertion("equal", "status_code", 201, "POST /b"),),
                ),
            ),
        )
        outcome = merge_derived_assertions(draft, DOC, slice_start=1, slice_end=14)
        self.assertEqual(outcome.added, (), "多步用例不该被自动合并")
        self.assertEqual(outcome.draft, draft, "多步用例的草稿必须原样返回")
        self.assertIn("多步", " ".join(outcome.skipped))


class TestRollbackSwitches(unittest.TestCase):
    """⑧ 两条回滚（CLI 开关 / 环境变量）成对生效。"""

    def test_default_is_on(self):
        self.assertTrue(merge_enabled(env={}))

    def test_cli_flag_turns_it_off(self):
        self.assertFalse(merge_enabled(cli_flag=False, env={}))

    def test_env_turns_it_off_but_explicit_on_wins(self):
        for token in ("off", "0", "false", "NO", " disabled "):
            with self.subTest(token=token):
                self.assertFalse(merge_enabled(env={MERGE_ENV: token}))
        self.assertTrue(merge_enabled(env={MERGE_ENV: "on"}))
        self.assertTrue(merge_enabled(env={MERGE_ENV: "1"}))


class TestRealGoldenDocument(unittest.TestCase):
    """⑨ 真样本：golden 文档能补出字段级断言（不是只在合成文档上成立）。

    ★样本选择（2026-09-26，§9.14）：用 `paged_product_list.md` —— 它的**响应侧**有据
    （响应说明句写着 `data.total`/`data.list`）。原先用的 `create_user_schema.md`
    只声明了**请求**字段（`username`/`email`），归属闸门按新口径不再把它们断言到响应上，
    于是"补出字段级断言"这句话在它身上**不成立**（不是合并坏了，是那份文档没给响应侧的据）。
    """

    def test_golden_document_gets_field_level_assertions(self):
        path = os.path.join(GOLDEN_DIR, "paged_product_list.md")
        self.assertTrue(os.path.exists(path), "缺 golden 样本，判据失效")
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        outcome = merge_derived_assertions(_model_draft(), text)
        self.assertTrue(outcome.added, f"golden 文档一条都没补出来：{outcome.skipped}")
        self.assertTrue(
            all(item.check != "status_code" for item in outcome.draft.assertions()[1:]),
            "补进来的居然是协议级断言？那和模型给的重了",
        )
        # ★成对：补进来的必须是**响应侧**字段，不许把请求参数（`params.*`）混进来
        #   （内核不把请求参数当断言对象，`params.X` 取不到值）。
        self.assertTrue(
            all(not item.check.startswith("params.") for item in outcome.draft.assertions()[1:]),
            "请求参数被当成断言补进来了",
        )


class TestNoOpMergeWouldBeCaught(unittest.TestCase):
    """⑩ 注入式元护栏：把合并换成「原样返回」→ 上面那条核心口径必须**真红**。"""

    def test_guard_would_catch_a_no_op_merge(self):
        def noop(draft, _doc, **_kwargs):
            return MergeOutcome(draft=draft)

        with mock.patch.object(merge_module, "merge_derived_assertions", noop):
            injected = merge_module.merge_derived_assertions(
                _model_draft(), DOC, slice_start=1, slice_end=14
            )
        self.assertEqual(injected.added, (), "注入的空合并竟然报出了 added")

        real = merge_derived_assertions(_model_draft(), DOC, slice_start=1, slice_end=14)
        self.assertTrue(real.added, "真实合并也该给得出 added —— 否则「空合并会被抓到」这句话本身就没意义")


if __name__ == "__main__":
    unittest.main()
