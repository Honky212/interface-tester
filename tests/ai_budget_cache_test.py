# -*- coding: utf-8 -*-
"""输入预算（字节口径）与缓存键（全字段）的护栏用例 —— **T4** / **T5**。

## 为什么需要这个文件

这两条待办的共同点是「**把文档里的口径变成代码常量**」：

| 待办 | 文档里的说法 | 代码化后要防的漂移 |
| --- | --- | --- |
| **T4** | §5.1 与 §9.x P-5 各写了一遍 `INPUT_BUDGET_BYTES = 8000` | 文档改了、代码没改（或反过来）——**没有任何机制会发现** |
| **T5** | §5.6：缓存键纳入"全部请求字段"，取样参数全进 | **漏一个字段 = 那一项改了还能命中旧缓存**，症状是"换参数没效果"，看起来像模型变笨 |

所以本文件的判据分两层：
1. **能力层**：计量口径（UTF-8 字节，不是字符数）、超限**报错并记录超出量**、缓存键对每个字段都敏感；
2. **对账层**：**从文档里把数字/字段名抓出来**，与代码常量逐一比对——
   这才是"数字只活在文档里"这个病的解药（与 `docs_consistency_test` 同一取向）。
"""

import os
import re
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import interfacetester_ai.cache as cache_module  # noqa: E402
import interfacetester_ai.prompts as prompts_module  # noqa: E402
from interfacetester_ai.cache import (  # noqa: E402
    CACHE_KEY_FIELDS,
    CacheKeyFieldsMismatch,
    build_cache_key,
)
from interfacetester_ai.prompts import (  # noqa: E402
    INPUT_BUDGET_BYTES,
    InputBudgetExceeded,
    build_prompt_payload,
    check_input_budget,
    measure_bytes,
)

PLAN_DOC = os.path.join(BASE, "docs", "interfacetester智能化改造方案8.md")

# NOTICE（外发仓口径）：`docs/interfacetester智能化改造方案8.md` 是内网方案文档，
# 不随本仓库分发 → 文档不在时，下面 3 条「文档 ↔ 代码对账」判据自动 skip（不静默通过）。
PLAN_DOC_PRESENT = os.path.isfile(PLAN_DOC)
PLAN_DOC_SKIP_REASON = "内网方案文档不在工作区（docs/interfacetester智能化改造方案8.md）"


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@unittest.skipUnless(PLAN_DOC_PRESENT, PLAN_DOC_SKIP_REASON)
class TestDocumentedBudgetMatchesTheCode(unittest.TestCase):
    """**对账层**：文档里的预算数字必须与代码常量一致（T4 的处置就是消灭"两处写法"）。"""

    def test_plan_document_budget_equals_the_code_constant(self):
        found = set(re.findall(r"INPUT_BUDGET_BYTES\s*=\s*(\d+)", _read(PLAN_DOC)))
        self.assertTrue(found, "方案文档里找不到 INPUT_BUDGET_BYTES 的取值，判据失效")
        self.assertEqual(
            found,
            {str(INPUT_BUDGET_BYTES)},
            f"文档写着 {sorted(found)}，代码常量是 {INPUT_BUDGET_BYTES}——数字必须先改代码再改文档",
        )


class TestBudgetMeasurementIsBytes(unittest.TestCase):
    """**能力层**：计量口径是 **UTF-8 字节**，不是字符数（§5.1 明写"字节口径"）。"""

    def test_chinese_text_measures_as_utf8_bytes(self):
        self.assertEqual(measure_bytes("中文"), 6)
        self.assertNotEqual(measure_bytes("中文"), len("中文"))

    def test_within_budget_returns_actual_byte_count(self):
        self.assertEqual(check_input_budget("abc"), 3)
        self.assertEqual(check_input_budget("中"), 3)

    def test_over_budget_raises_and_records_the_overflow(self):
        """§9.x P-5 的判据：超限**报错 + 记录截断量**（这里就是"超出多少字节"）。"""
        with self.assertRaises(InputBudgetExceeded) as ctx:
            check_input_budget("中" * 10, budget=12, label="slice#3")

        self.assertEqual(ctx.exception.size, 30)
        self.assertEqual(ctx.exception.budget, 12)
        self.assertEqual(ctx.exception.overflow, 18)
        self.assertIn("slice#3", str(ctx.exception))

    def test_exactly_at_budget_is_allowed(self):
        """边界：**等于**上限是合法的（`>` 而不是 `>=`）——差一字节不该判失败。"""
        self.assertEqual(check_input_budget("x" * 10, budget=10), 10)

    def test_budget_is_checked_on_the_joined_payload(self):
        """各段都不超、合起来超了 → 必须报错（分开看是这类预算最典型的漏检形态）。"""
        with self.assertRaises(InputBudgetExceeded):
            build_prompt_payload("s" * 30, ["x" * 30, "y" * 30], "u" * 30, budget=100)

    def test_joined_payload_returns_when_within_budget(self):
        payload = build_prompt_payload("sys", ["ex1", "ex2"], "user")
        self.assertEqual(payload, "sys\nex1\nex2\nuser")
        self.assertEqual(measure_bytes(payload), len("sys\nex1\nex2\nuser".encode("utf-8")))


class TestNoSilentTruncation(unittest.TestCase):
    """**元护栏**：§9.x P-5 明写"**不允许静默截断**"——所以模块里不该存在截断入口。"""

    def test_prompts_module_exposes_no_truncation_helper(self):
        offenders = [name for name in dir(prompts_module) if "truncat" in name.lower()]
        self.assertEqual(
            offenders,
            [],
            f"出现了截断类入口 {offenders}——静默截断会让模型看到半句话而报告显示成功",
        )


class TestCacheKeyCoversEveryDocumentedField(unittest.TestCase):
    """**对账层**：§5.6 写明的字段 ↔ `CACHE_KEY_FIELDS` 双向对账。

    单向对账不够：只查"文档要求的都在代码里"会漏掉"代码里多了一个文档没提的字段"
    ——那个字段的来源不明，很可能就是"有人顺手加的、没走评审"。
    """

    DOCUMENTED_FIELDS = (
        "temperature",
        "seed",
        "max_tokens",
        "response_format",
        "base_url",
        "model",
        "prompt_version",
        "system",
        "user",
    )

    @unittest.skipUnless(PLAN_DOC_PRESENT, PLAN_DOC_SKIP_REASON)
    def test_every_documented_field_is_in_the_code_list(self):
        text = _read(PLAN_DOC)
        for name in self.DOCUMENTED_FIELDS:
            with self.subTest(field=name):
                self.assertIn(name, text, f"方案文档里没提到 {name}，对账前提不成立")
                self.assertIn(
                    name, CACHE_KEY_FIELDS, f"文档要求 {name} 进键，代码清单里没有"
                )

    @unittest.skipUnless(PLAN_DOC_PRESENT, PLAN_DOC_SKIP_REASON)
    def test_code_list_has_no_field_the_document_never_mentions(self):
        text = _read(PLAN_DOC)
        for name in CACHE_KEY_FIELDS:
            with self.subTest(field=name):
                self.assertIn(name, text, f"代码清单里的 {name} 文档从未提及（来源不明）")

    def test_field_count_is_pinned(self):
        self.assertEqual(
            len(CACHE_KEY_FIELDS), 9, "字段数变了就必须显式更新本断言（防悄悄扩/缩）"
        )


class TestCacheKeySensitivityAndStrictness(unittest.TestCase):
    """**能力层**：改任一字段必换键；缺字段与多字段都必须报错。"""

    @staticmethod
    def _fields(**overrides):
        base = {name: f"v-{name}" for name in CACHE_KEY_FIELDS}
        base.update(overrides)
        return base

    def test_changing_any_single_field_changes_the_key(self):
        base_key = build_cache_key(self._fields())
        for name in CACHE_KEY_FIELDS:
            with self.subTest(field=name):
                self.assertNotEqual(
                    build_cache_key(self._fields(**{name: "CHANGED"})),
                    base_key,
                    f"改 {name} 竟然没换键（会误命中旧缓存）",
                )

    def test_temperature_change_misses(self):
        """文档点名的场景单独一条（§9.x P-2 的判据原话）。"""
        self.assertNotEqual(
            build_cache_key(self._fields(temperature=0)),
            build_cache_key(self._fields(temperature=0.7)),
        )

    def test_missing_field_raises_instead_of_defaulting(self):
        fields = self._fields()
        fields.pop("seed")

        with self.assertRaises(CacheKeyFieldsMismatch) as ctx:
            build_cache_key(fields)

        self.assertEqual(ctx.exception.missing, ["seed"])

    def test_extra_field_raises_too(self):
        """**镜像方向**：加了采样参数却没同步清单 → 那一项改了不会 miss（不易察觉）。"""
        with self.assertRaises(CacheKeyFieldsMismatch) as ctx:
            build_cache_key(self._fields(top_p=0.9))

        self.assertEqual(ctx.exception.extra, ["top_p"])

    def test_key_is_order_independent_and_stable(self):
        fields = self._fields()
        self.assertEqual(
            build_cache_key(fields), build_cache_key(dict(reversed(list(fields.items()))))
        )
        self.assertEqual(build_cache_key(fields), build_cache_key(self._fields()))

    def test_key_is_a_sha256_hex_digest(self):
        self.assertRegex(build_cache_key(self._fields()), r"^[0-9a-f]{64}$")

    def test_chinese_value_is_stable_and_content_sensitive(self):
        """`ensure_ascii=False` 是刻意的：同一份中文 prompt 不该因转义方式不同而换键。"""
        same = self._fields(user="请为订单接口生成用例")
        self.assertEqual(build_cache_key(same), build_cache_key(dict(same)))
        self.assertNotEqual(
            build_cache_key(same),
            build_cache_key(self._fields(user="请为订单接口生成用例。")),
        )

    def test_build_key_does_not_write_anything(self):
        """纯函数：缓存键构造**不写盘**（写盘只发生在落盘入口，见 cache.py 的边界纪律）。"""
        before = sorted(os.listdir(BASE))
        build_cache_key(self._fields())
        self.assertEqual(sorted(os.listdir(BASE)), before)
        self.assertFalse(hasattr(cache_module, "save_cache"))
