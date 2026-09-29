# -*- coding: utf-8 -*-
"""引用解析与**证据绑定**（**WP2.2**）的判据 —— 全部离线、纯函数。

判据分三层：

1. **解析**：引用=标题 / 引用=正文行 / 引用=自造锚点，三条路各自绑到对的那一节；
2. **边界（成对判据）**：片外的**共享节**允许（标 `shared`）、片外的**别的接口段**必须拒
   —— 后者就是 §9.6 ① 归属闸门在"引用解析"上的对应物；
3. **不许静默**：绑不上时必须给出**最近的标题候选**（人要知道往哪儿改）。

★这里有一条**实测回归**（`test_flat_slice_off_by_one_still_binds`）：
模型通路的 `scope` 来自 `slicer` 的**扁平片**（如 21-45），而同一节的标题跨度是 21-46 ——
用"整节被片包住"判片内会差一行，把**本节自己的窗口**判成"别的接口"而拒绝绑定。
"""

import ast
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.citation import (  # noqa: E402
    KIND_FACT,
    KIND_HEADING,
    KIND_NONE,
    KIND_SECTION,
    bind,
    nearest_title,
    run_selftest,
    title_of,
)
from interfacetester_ai.slicer import heading_spans  # noqa: E402

DOC = """# 订单接口文档

## 1. 通用约定

### 1.4 统一响应格式

#### 成功响应

```json
{"success": true, "message": "操作成功", "amount": 99.9}
```

### 1.5 常见错误码

| 错误码 | 说明 |
| --- | --- |
| ORDER_1001 | 金额不能为空 |

## 2. 订单接口

### 2.1 创建订单

| 属性 | 值 |
| --- | --- |
| **URL** | `POST /api/order` |

#### 请求体

| 字段名 | 类型 | 说明 |
| --- | --- | --- |
| `amount` | 数字 | 单价 |
| `coupon` | String | 优惠券 |

### 2.2 删除订单

| 属性 | 值 |
| --- | --- |
| **URL** | `POST /api/order/delete` |

#### 请求体

| 字段名 | 类型 | 说明 |
| --- | --- | --- |
| `orderId` | 数字 | 订单号 |
"""


def section(title):
    return next(item for item in heading_spans(DOC) if item.title == title)


OWN = section("2.1 创建订单")  # 本用例自己的那一节
SCOPE = (OWN.start_line, OWN.end_line)


class TestQuoteResolution(unittest.TestCase):
    """三种坏法的解析。"""

    def test_title_quote_binds_to_that_section(self):
        hit = bind("成功响应", ["message"], DOC)

        self.assertEqual(hit.kind, KIND_HEADING)
        self.assertEqual(hit.title, "成功响应")
        self.assertIn("操作成功", hit.text, "窗口必须是**那一节的正文**，不是标题行本身")

    def test_title_quote_tolerates_a_trailing_colon(self):
        """实测模型写 `成功响应:`（带冒号）而文档标题是 `#### 成功响应` —— 一个冒号不该让绑定失败。"""
        self.assertEqual(title_of("成功响应："), "成功响应")
        self.assertEqual(bind("成功响应：", ["message"], DOC).kind, KIND_HEADING)

    def test_body_line_quote_binds_to_the_section_that_owns_it(self):
        hit = bind("| ORDER_1001 | 金额不能为空 |", ["ORDER_1001"], DOC)

        self.assertEqual(hit.kind, KIND_SECTION)
        self.assertEqual(hit.title, "1.5 常见错误码")
        self.assertIn("ORDER_1001", hit.text)

    def test_invented_anchor_binds_to_the_section_holding_the_fact(self):
        """实测：模型写 `response_example`（文档里 0 次），而字段 `amount` 在 1.4 那一节里。"""
        hit = bind("response_example", ["amount"], DOC)

        self.assertEqual(hit.kind, KIND_FACT)
        self.assertIn("amount", hit.text)

    def test_nothing_to_bind_stays_unresolved_but_suggests_a_title(self):
        hit = bind("完全无关的一句话", ["zzz"], DOC)

        self.assertFalse(hit.ok())
        self.assertEqual(hit.kind, KIND_NONE)
        self.assertTrue(hit.suggestion, "绑不上时必须给出最近标题候选（不许静默给个空窗口）")
        self.assertIn("找不到出处", hit.render())


class TestOwnershipBoundary(unittest.TestCase):
    """片边界：共享节**允许**、别的接口段**必须拒**（成对判据）。"""

    def test_a_span_inside_the_slice_is_bound_normally(self):
        hit = bind("request_example", ["coupon"], DOC, scope=SCOPE)

        self.assertTrue(hit.ok())
        self.assertIn("coupon", hit.text)
        self.assertFalse(hit.shared, "片内绑定不该被标成 shared")

    def test_another_interface_is_refused_even_via_its_field_table(self):
        """★关键：`orderId` 只出现在 **2.2 删除订单** 的字段表里（那子节**自己不含端点**）。"""
        hit = bind("orderId 的说明", ["orderId"], DOC, scope=SCOPE)

        self.assertFalse(hit.ok(), "绑到了别的接口那一段（归属闸门失效）")

    def test_a_shared_section_outside_the_slice_is_allowed_and_marked(self):
        hit = bind("成功响应", ["message"], DOC, scope=SCOPE)

        self.assertTrue(hit.ok(), "全篇共享节（统一响应格式）不该被误拒")
        self.assertTrue(hit.shared, "片外绑定必须标 shared（报告里要能看见它不是本片的）")
        self.assertIn("全篇共享", hit.render())

    def test_flat_slice_off_by_one_still_binds(self):
        """★**实测回归**：`slicer` 的扁平片不含尾随空行 → 片范围 21-45、标题跨度 21-46；
        用"整节被片包住"判片内会差一行，于是本节自己的窗口被判成"别的接口"而拒绝绑定。"""
        flat = (OWN.start_line, OWN.end_line - 1)  # 模拟扁平片（少一行）
        self.assertNotEqual(flat, SCOPE)
        hit = bind("request_example", ["coupon"], DOC, scope=flat)

        self.assertTrue(hit.ok(), "扁平片少一行不该导致本节自己的窗口被拒")
        self.assertFalse(hit.shared)


class TestNearestTitleCandidate(unittest.TestCase):
    def test_candidate_points_at_the_closest_heading(self):
        ratio, title = nearest_title("成功响应，包含 data 字段", DOC)

        self.assertEqual(title, "成功响应")
        # ★不锁死相似度阈值：候选的价值是"指向哪一节"，而**长句 vs 短标题**的相似度天然偏低
        #   （实测这句 0.42）。锁一个数字会在换样本时变成假红。
        self.assertGreater(ratio, 0.0)


class TestModuleDiscipline(unittest.TestCase):
    """模块级元护栏。"""

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_module_never_writes_to_disk(self):
        """纯函数模块：源码里**不许**出现任何写盘调用（不进 T18 注册表就得守这条）。"""
        path = os.path.join(BASE, "interfacetester_ai", "citation.py")
        with open(path, encoding="utf-8") as fp:
            tree = ast.parse(fp.read())
        forbidden = ("write", "write_text", "dump", "mkdir", "makedirs", "remove", "unlink")
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in forbidden:
                    hits.append(f"line {node.lineno}: {node.func.attr}")
        self.assertEqual(hits, [], f"citation.py 出现了写盘调用：{hits}")


if __name__ == "__main__":
    unittest.main()
