# -*- coding: utf-8 -*-
"""引用解析与**证据绑定**（**WP2.2**，2026-09-26）—— 纯函数、零写盘、零模型调用。

## 它解决什么（实测证据：`probe_p2_prep/out_quote_loss.txt`）

真模型（`gemma4:latest`）在客户真实文档上给出的 `source_quote` 有**三种坏法**：

| 坏法 | 实测例子 | 文档里其实写着什么 |
| --- | --- | --- |
| ① 引**标题**而不是承载事实的那一节 | `成功响应` | `#### 成功响应` 那一节的 json 里有 `"message": "操作成功"` |
| ② 引**自造锚点** | `response_example`（文档里出现 **0** 次） | 字段 `access_token` 在 `### 1.2 认证方式` 那一节里 |
| ③ 引**字段说明单元格** | `操作是否成功` | 字段名 `success` 在**同一张表的另一列**（`| success | Boolean | 操作是否成功 |`） |

三种坏法的共同点：**模型的引用选歪了，而事实本身在文档的某一节里写着**。
本模块把引用**解析成文档里的某一节**（下称"证据窗口"），交给 `assembler` 的 T28 对账；
窗口换成了哪一节**逐条记账**（`QuoteBinding`）——报告里因此能看见
"这条断言不是模型逐字引的，是工具按引用绑到第 60 行那一节的"。

## 边界（本模块**不做**的事）

- **不调模型、不写盘、不改断言**：`check` / `expect` 一个字都不改，只换证据窗口；
- **不把"别的接口那一段"当证据**：窗口若落在**另一个接口段**（那段里有 `METHOD /path`）→ **拒绝绑定**
  （这是 §9.6 ① 归属闸门在"引用解析"上的对应物）；
- **不替人拍板**：绑不上就如实说"找不到出处"，并给出**最接近的标题候选**（`difflib`），
  让人一眼看出"模型大概想引哪一节"。

## 为什么"绑定"不等于"放松"

绑定之后**四类对账一条都不少**（字段名 / expect 字面量 / 限定词 / 示例值硬编码），
只是把"引用句"从**模型那句话**换成**它指向的那一节**。实测（同一份留痕）：
绑定能救回的都是"文档真写了这个字段/值"的断言，而像"把示例值 `操作成功` 当契约"这类
**该拦的仍然拦住**（④ 示例值硬编码照常报）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

from interfacetester_ai.slicer import ENDPOINT_RE, HeadingSpan, heading_spans

KIND_HEADING = "heading"  # 引用 = 某标题 → 绑到该标题那一节
KIND_SECTION = "section"  # 引用是文档里的一句话 → 绑到包含它的那一节
KIND_FACT = "field-section"  # 引用在文档里找不到 → 绑到"含该事实"的最小节
KIND_NONE = "none"  # 绑不上（**交人工**）

# 容器名不是文档字段（与 `assembler._CONTAINER_CHECKS` 同口径：`body` 指"在哪一层找"）
_CONTAINERS = frozenset({"body", "data", "headers", "params", "json", "cookies"})
# 类型名/常见噪声不用于**定位**（"字符串"这种词在文档里到处都是，用它定位等于乱指）
_NOISE = frozenset(
    {
        "str", "string", "int", "integer", "float", "number", "bool", "boolean",
        "list", "array", "dict", "object", "null", "none", "true", "false",
        "字符串", "整数", "数字", "布尔", "数组", "对象",
    }
)


@dataclass(frozen=True)
class QuoteBinding:
    """一次**证据绑定**的结果：`kind` + 绑到的那一节 + 该节文本（`text`）。"""

    kind: Text
    quote: Text = ""  # 模型原引用（原样保留，供报告显示）
    where: Text = ""  # 断言位置（`steps[0].validate[1]`）
    title: Text = ""  # 绑到的那一节标题
    line_no: int = 0  # 那一节的起始行（1-based，供人回溯）
    text: Text = ""  # 证据窗口文本（喂给 T28 的"引用句"）
    shared: bool = False  # 窗口在**片外**、但那节是全篇共享（不含端点）
    narrowed: bool = False  # 窗口已被**窄化到承载该事实的那一行**（比整节更精确）
    suggestion: Text = ""  # 绑不上时：最接近的标题候选
    similarity: float = 0.0  # 候选的相似度

    def ok(self) -> bool:
        return self.kind != KIND_NONE

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "kind": self.kind,
            "quote": self.quote,
            "where": self.where,
            "title": self.title,
            "line_no": self.line_no,
            "shared": self.shared,
            "narrowed": self.narrowed,
            "suggestion": self.suggestion,
            "similarity": round(self.similarity, 2),
        }

    def render(self) -> Text:
        """给 CLI / `unknowns` 的一行话（★要能被人一眼读懂"换到了哪一节"）。"""
        if not self.ok():
            if self.suggestion:
                return (
                    f"引用 {self.quote[:40]!r} 在文档里找不到出处"
                    f"（最接近的标题：{self.suggestion}｜相似度 {self.similarity:.2f}）"
                )
            return f"引用 {self.quote[:40]!r} 在文档里找不到出处"
        if self.narrowed:
            place = f"第 {self.line_no} 行（**承载该事实的那一行**，属于「{self.title}」那一节）"
        else:
            place = f"第 {self.line_no} 行那一节「{self.title}」"
        if self.shared:
            place += "（**全篇共享**：那一节里不含端点）"
        return f"引用 {self.quote[:40]!r} → 绑到{place}（{self.kind}）"


def title_of(quote: Text) -> Text:
    """引用句里"像标题"的那部分：去掉 `#`、首尾空白、以及**尾随冒号**。

    ★为什么要去尾随冒号：实测模型写 `成功响应:`（带冒号），而文档里的标题是 `#### 成功响应`
    —— 一个冒号就足以让"逐字比对"失败，而人看这两者是同一个东西。
    """
    text = (quote or "").strip()
    text = text.lstrip("#").strip()
    for suffix in (":", "："):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip()


def _locator_lines(quote: Text) -> List[Text]:
    """从（可能多行的）引用里挑出**用于定位**的行：最长的一行优先。

    ★为什么不用第一行：多行引用常常以 `` ```json `` 开头，拿它去定位会落在**任何** json 块上。
    最长的那一行几乎必然是该节里最有信息量的一句。
    """
    pieces = [line.strip() for line in (quote or "").split("\n") if line.strip()]
    pieces.sort(key=len, reverse=True)
    return pieces


def locate_quote(
    quote: Text, doc_text: Text, *, spans: Optional[Sequence[HeadingSpan]] = None
) -> Tuple[Optional[HeadingSpan], Text]:
    """引用指向**哪一节**。返回 `(节, kind)`；`kind ∈ {heading, section, none}`。"""
    window = list(spans) if spans is not None else heading_spans(doc_text)
    title = title_of(quote)
    if title:
        for span in window:
            if span.title.strip() == title:
                return span, KIND_HEADING

    lines = (doc_text or "").split("\n")
    for token in _locator_lines(quote):
        line_no = 0
        for index, line in enumerate(lines, start=1):
            if token in line:
                line_no = index
                break
        if not line_no:
            head = token[:24]
            if head:
                for index, line in enumerate(lines, start=1):
                    if head in line:
                        line_no = index
                        break
        if line_no:
            hits = [span for span in window if span.contains_line(line_no)]
            if hits:
                # 最小的一节 = 最具体的那一层（`#### 成功响应` 而不是 `# 整篇`）
                return min(hits, key=lambda span: span.end_line - span.start_line), KIND_SECTION
    return None, KIND_NONE


def fact_span(
    tokens: Sequence[Text],
    doc_text: Text,
    *,
    spans: Optional[Sequence[HeadingSpan]] = None,
    scope: Optional[Tuple[int, int]] = None,
) -> Optional[HeadingSpan]:
    """含这些**事实 token** 的**最小节**（按 token 可信度递减试）；`scope` 内优先。"""
    window = list(spans) if spans is not None else heading_spans(doc_text)
    usable = [token for token in tokens if token and token not in _NOISE]
    if not usable:
        return None
    # ★片内优先：片外可能也有一个更小的节含这个字段名，但那多半是**别的接口**的段
    #   （含端点 → 会被拒绝），先找片内就不会因为"外面那个更小"而把整条弃掉。
    pools = [window]
    if scope is not None:
        start, end = scope
        # ★片内判据是"**这一节的头行落在片内**"，不是"整节被片包住"（踩过）：
        #   模型通路的 `scope` 来自 `slicer` 的**扁平片**（21-45），而同一节的标题跨度是
        #   21-46（`split_document` 不算尾随空行）——用"整节被包住"会差一行，
        #   于是**本节自己的窗口**被判成"别的接口那一节"而拒绝绑定（实测：`access_token`
        #   明明在片内却被判 `quote-miss`）。头行为 21 ∈ [21,45] → 正确归入片内。
        inside = [span for span in window if start <= span.start_line <= end]
        pools = [inside, window]
    for pool in pools:
        for count in range(len(usable), 0, -1):
            hits = [
                span for span in pool if all(token in span.text for token in usable[:count])
            ]
            if hits:
                return min(hits, key=lambda span: span.end_line - span.start_line)
    return None


def nearest_title(
    quote: Text, doc_text: Text, *, spans: Optional[Sequence[HeadingSpan]] = None
) -> Tuple[float, Text]:
    """最接近的标题 + 相似度（绑不上时给人**修正方向**）。"""
    window = list(spans) if spans is not None else heading_spans(doc_text)
    target = title_of(quote)
    best = (0.0, "")
    for span in window:
        ratio = SequenceMatcher(None, target, span.title).ratio()
        if ratio > best[0]:
            best = (ratio, span.title)
    return best


def _interface_spans(window: Sequence[HeadingSpan]) -> List[HeadingSpan]:
    """**最具体的接口段**：含端点的节里，不再严格包含另一个含端点节的那些。

    ★为什么"这一节自己含不含端点"不够（踩过）：字段表子节（`#### 请求体`）**自己不含端点**，
    但它是**另一个接口段**的一部分——只看自己就会把它误判成"全篇共享"，于是
    别人接口的字段表被当成证据（归属闸门漏了）。
    ★去重规则与 `gen.interface_sections()` 一致：父段被丢弃，只留最具体的那一层
    （否则根标题「整篇」会包含一切，全篇都成"别的接口"了）。
    """
    hits = [span for span in window if ENDPOINT_RE.search(span.text)]
    out: List[HeadingSpan] = []
    for span in hits:
        nested = any(
            other is not span
            and span.start_line <= other.start_line
            and other.end_line <= span.end_line
            and (other.start_line, other.end_line) != (span.start_line, span.end_line)
            for other in hits
        )
        if not nested:
            out.append(span)
    return out


def _in_another_interface(span: HeadingSpan, window: Sequence[HeadingSpan]) -> bool:
    return any(
        other.start_line <= span.start_line and span.end_line <= other.end_line
        for other in _interface_spans(window)
    )


def _line_carrying_fact(span: "HeadingSpan", tokens: Sequence[Text], doc_text: Text) -> int:
    """在这一节里找**承载该事实的那一行**（含全部可用 token），返回行号；找不到 → 0。

    ★为什么必须窄化（实测踩到）：绑到**整节**时，③ 限定词检查会把**同表里别的字段**的类型
    当成"这句话说该字段是字符串"——`| coupon | string | ... |` 那一行会让
    `type_match: [body.amount, 'int']` 被误判成 `qualifier-conflict`（整表被当成一句话）。
    窄化到**该字段那一行**之后，③ 判的就是"这一行讲的是哪个字段、类型是什么"，
    正是它本来的口径。
    ★窄化还顺带保住 ②：窄化的前提是"该行同时含**全部**可用 token"（含期望值，类型名与噪声词除外）。
    """
    usable = [token for token in tokens if token and token not in _NOISE]
    if not usable:
        return 0
    lines = (doc_text or "").split("\n")
    stop = min(span.end_line, len(lines))
    for number in range(span.start_line, stop + 1):
        line = lines[number - 1]
        if len(line.strip()) < 4:
            continue
        if all(token in line for token in usable):
            return number
    return 0


def bind(
    quote: Text,
    tokens: Sequence[Text],
    doc_text: Text,
    *,
    where: Text = "",
    scope: Optional[Tuple[int, int]] = None,
) -> QuoteBinding:
    """给出**证据绑定**：把引用解析成文档里的某一节（绑不上 → `KIND_NONE` + 最近标题候选）。

    优先级：**引用 = 标题** → **引用 = 文档里的一句话** → **含事实 token 的最小节**。

    `scope`（片行号范围，1-based 含）给出时的规矩：

    - 窗口落在**片内** → 正常绑定；
    - 窗口在片外、但**不落在任何接口段里**（全篇共享：统一响应格式 / 公共请求头 / 错误码表）
      → **允许**，并标 `shared=True`（报告里单独可见）；
    - 窗口在片外、且落在**某个接口段**里（= **别的接口**那一段，含它的字段表子节）
      → **拒绝**（归属闸门）。
    """
    spans = heading_spans(doc_text)
    span, kind = locate_quote(quote, doc_text, spans=spans)
    if span is None:
        span = fact_span(tokens, doc_text, spans=spans, scope=scope)
        kind = KIND_FACT if span is not None else KIND_NONE
    if span is None:
        similarity, suggestion = nearest_title(quote, doc_text, spans=spans)
        return QuoteBinding(
            kind=KIND_NONE,
            quote=quote,
            where=where,
            suggestion=suggestion,
            similarity=similarity,
        )
    shared = False
    if scope is not None:
        start, end = scope
        # ★同 `fact_span`：片内判据 = **这一节的头行落在片内**（扁平片比标题跨度少一行，
        #   用"整节被包住"会把本节自己的窗口判成别的接口 → 拒绝绑定；实测踩过）。
        if not (start <= span.start_line <= end):
            if _in_another_interface(span, spans):
                similarity, suggestion = nearest_title(quote, doc_text, spans=spans)
                return QuoteBinding(
                    kind=KIND_NONE,
                    quote=quote,
                    where=where,
                    suggestion=suggestion,
                    similarity=similarity,
                )
            shared = True
    narrowed = 0
    # ★窄化：能定位到"承载该事实的那一行"就用那一行（更精确，避免整表被当成一句话）
    line_no = _line_carrying_fact(span, tokens, doc_text)
    if line_no:
        window_text = (doc_text or "").split("\n")[line_no - 1]
        narrowed = line_no
    else:
        window_text = span.text
    return QuoteBinding(
        kind=kind,
        quote=quote,
        where=where,
        title=span.title,
        line_no=narrowed or span.start_line,
        text=window_text,
        shared=shared,
        narrowed=bool(narrowed),
    )


_SELFTEST_DOC = """# 订单接口文档

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


def run_selftest(verbose: bool = False) -> int:
    """`citation` 自检（**纯逻辑、不写盘、不调模型**）。返回 0=全绿；非 0=失败项数。"""
    doc = _SELFTEST_DOC
    failures: List[Text] = []

    def say(text: Text) -> None:
        if verbose:
            print(text)

    # ① 引用 = 标题（含尾随冒号）→ 绑到那一节
    hit = bind("成功响应：", ["amount"], doc, where="t")
    if not (hit.ok() and hit.kind in (KIND_HEADING, KIND_SECTION)):
        failures.append("[①] 标题式引用没绑上")
    elif "amount" not in hit.text:
        failures.append("[①] 绑到的那一节里没有该字段（窗口取错了）")

    # ② 引用是文档里的一句话 → 绑到含它的那一节
    hit = bind("| ORDER_1001 | 金额不能为空 |", ["ORDER_1001"], doc)
    if not (hit.ok() and hit.kind == KIND_SECTION and "ORDER_1001" in hit.text):
        failures.append("[②] 引用是正文行时没绑到那一节")

    # ③ 自造锚点 → 绑到"含事实"的最小节，且给出最近标题候选
    hit = bind("response_example", ["amount"], doc)
    if not (hit.ok() and hit.kind == KIND_FACT and "amount" in hit.text):
        failures.append("[③] 自造锚点没落到'含事实的那一节'")
    miss = bind("响应规范", ["amount"], _SELFTEST_DOC.replace("amount", "total"))
    if miss.ok():
        failures.append("[③] 文档里没有这个事实时不该绑上")

    # ④ **别的接口那一段不许当证据**（片外 + 落在别的接口段里 → 拒绝）
    spans = heading_spans(doc)
    own = next(item for item in spans if item.title == "2.1 创建订单")
    scope = (own.start_line, own.end_line)
    hit = bind("orderId 的说明", ["orderId"], doc, scope=scope)
    if hit.ok():
        failures.append("[④] 绑到了**别的接口**那一段（归属闸门失效）")

    # ⑤ 片内优先：事实在片内 → 不去碰片外的更小节
    hit = bind("request_example", ["coupon"], doc, scope=scope)
    if not (hit.ok() and "coupon" in hit.text):
        failures.append("[⑤] 片内有该事实时没在片内绑定")

    # ⑥ 片外但**全篇共享**（不含端点）→ 允许，并标 shared
    hit = bind("成功响应", ["message"], doc, scope=scope)
    if not hit.ok():
        failures.append("[⑥] 全篇共享节（统一响应格式）被误拒")
    elif not hit.shared:
        failures.append("[⑥] 片外共享节没标 shared（报告里就看不见它不是本片的）")

    # ⑦ 绑不上 → KIND_NONE + 有候选（不许静默给个空窗口）
    none = bind("完全无关的一句话", ["zzz"], doc)
    if none.ok() or not none.suggestion:
        failures.append("[⑦] 绑不上时没给出最近标题候选")

    for item in failures:
        print(f"[citation 自检] {item}")
    if verbose and not failures:
        print("[citation 自检] 全绿")
    return len(failures)


def main() -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="引用解析与证据绑定（WP2.2）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "KIND_FACT",
    "KIND_HEADING",
    "KIND_NONE",
    "KIND_SECTION",
    "QuoteBinding",
    "bind",
    "fact_span",
    "locate_quote",
    "nearest_title",
    "run_selftest",
    "title_of",
]


if __name__ == "__main__":
    raise SystemExit(main())
