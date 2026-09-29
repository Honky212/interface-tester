# -*- coding: utf-8 -*-
r"""归一化管道（§5.3 的「★ 归一化管道」/ §9.x 的 **P-4**）—— **T7** 交付。

## 为什么需要它

「引用校验」（§5.1 / §5.3）的判据是：`evidence_quotes` 必须是对应切片的
**归一化后逐字子串**。但真实日志 / 响应里，同一段内容会长成很多个样子：

| 噪声源 | 形态 |
| --- | --- |
| loguru 的颜色码 | `\x1b[32m{"code": 0}\x1b[0m` |
| Windows 换行 | `line1\r\nline2` |
| JSON 字符串里的转义 | `{"msg": "he said \"hi\""}` |
| 全角 / 半角标点 | `｛"code"：０｝` |
| 中文 / 英文引号 | `“ok”` vs `"ok"` |

不做归一化，"逐字子串"这条判据会把**真实存在**的证据判成"没命中"→ 归因被迫降级为
"未知"，于是 **≥70% 未降级占比**（§5.3 验收）永远达不到。
反过来，归一化**做过头**（把内容本身改掉）会让**编造的引用**也命中——那是更坏的方向。

所以本模块的纪律是：**只合并"同一段内容的不同书写形式"**，
每一条变换都要说清"它合并的是哪一种书写差异"，并配**对抗样本**自检（§3.5）。

## 管道顺序（§5.3 钉死，不许重排）

**去 ANSI → CRLF/CR → LF、空白折叠、NBSP→空格 → NFKC → JSON 反向解码 → 引号族统一**

顺序不是审美问题：NFKC 必须在 JSON 解码**之前**（全角的 `＼ｕ` 要先变成 `\u` 才可解码）；
引号族统一必须在**之后**（JSON 解码会产出 `"`，统一只负责收尾）。

## 偏移映射（报告定位用）

`normalize_with_map()` 额外返回**偏移映射**：归一化文本的每个字符都记得自己来自原串的哪个区间。
于是报告里可以写"该引用位于原文的 `[a, b)`"，而不是只说"它归一化后是子串"。

## ★边界（诚实登记，R14 纪律）

1. **NFKC 采用逐字符归一化以保住偏移**；整串 NFKC 在少数**跨字符组合**
   （如 `e` + U+0301 → `é`）上与逐字符结果不同。本模块**用对抗样本实测**证明
   "在这四类真实噪声上逐字符 == 整串"（见 `run_selftest`），**不声称**永远相等。
2. **JSON 反向解码对"本意就是字面反斜杠"的文本会过度解码**
   （如 Windows 路径 `C:\temp` 的 `\t`）。这是刻意取舍：真实 I/O 里
   "JSON 转义"远比"裸反斜杠"常见，且过度解码只影响**匹配宽松度**
   （让引用更容易命中），不改变内容来源；该边界已登记在 §9.x 的 P-4。
"""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Text, Tuple

# ANSI 转义序列（SGR / 光标控制 / 单字符转义）——§9.x P-4 点名的 loguru 颜色码
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

# JSON 单字符转义 → 实义字符（`\uXXXX` 单独处理）
_JSON_ESCAPES: Dict[Text, Text] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

# 引号族 → ASCII。NFKC 只处理**全角** `＂`；中文引号（U+201C 等）**不是** NFKC 映射，
# 必须显式列出——这是"看起来 NFKC 已经管了、其实没管"的典型陷阱。
_QUOTE_MAP: Dict[Text, Text] = {
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u300c": '"',
    "\u300d": '"',
    "\u300e": '"',
    "\u300f": '"',
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
}

_HEX4_RE = re.compile(r"[0-9a-fA-F]{4}")

# 水平空白族：空格 / TAB / NBSP（其余按 Unicode 类别 Zs 判定）
_H_SPACES = (" ", "\t", "\u00a0")


def _unify_quotes(text: Text) -> Text:
    if not text:
        return text
    return "".join(_QUOTE_MAP.get(ch, ch) for ch in text)


@dataclass(frozen=True)
class Normalized:
    """归一化结果 + **偏移映射**（报告定位用）。"""

    text: Text
    # 归一化后第 i 个字符对应原串区间的**起点**（= spans[i][0]）
    offsets: Tuple[int, ...]
    # 归一化后第 i 个字符来自原串的 `[start, end)`（精确到字符，用于 `span()`）
    spans: Tuple[Tuple[int, int], ...]
    source: Text

    def span(self, start: int, end: int) -> Tuple[int, int]:
        """把归一化文本里的 `[start, end)` 映射回**原串**区间（保守取覆盖范围）。"""
        if not self.spans:
            return (0, 0)
        start = max(0, min(start, len(self.spans)))
        end = max(start, min(end, len(self.spans)))
        if start == end:
            point = self.spans[start][0] if start < len(self.spans) else len(self.source)
            return (point, point)
        return (self.spans[start][0], self.spans[end - 1][1])

    def locate(self, needle: Text) -> Optional[Tuple[int, int]]:
        """在归一化文本里找 `needle`，返回它在**原串**里的区间（未命中 → `None`）。"""
        if not needle:
            return None
        index = self.text.find(needle)
        if index < 0:
            return None
        return self.span(index, index + len(needle))


def normalize_with_map(text: Text) -> Normalized:
    """跑完整管道，同时产出**偏移映射**（逐字符推进 → 映射精确）。

    单次从左到右扫描：每个分支都显式给出"输出字符来自原串的哪个区间"，
    所以不需要事后反推（反推是这类映射最常见的出错来源）。
    """
    if not text:
        return Normalized(text="", offsets=(), spans=(), source="")

    out: List[Text] = []
    spans: List[Tuple[int, int]] = []

    def emit(chunk: Text, start: int, stop: int) -> None:
        for ch in chunk:
            out.append(ch)
            spans.append((start, stop))

    i, n = 0, len(text)
    space_pending = False
    line_start = True

    while i < n:
        # ① 去 ANSI（整段丢弃，不产出字符）
        ansi = _ANSI_RE.match(text, i)
        if ansi:
            i = ansi.end()
            continue

        ch = text[i]

        # ② 换行族 → 单个 `\n`（CRLF / CR / LF 三种书写合并）
        if ch in ("\r", "\n"):
            stop = i + 1
            if ch == "\r" and text[i + 1 : i + 2] == "\n":
                stop = i + 2
            emit("\n", i, stop)
            i = stop
            space_pending = False
            line_start = True
            continue

        # ③ 水平空白族 → 折叠为单个空格（行首不产空格）
        if ch in _H_SPACES or unicodedata.category(ch) == "Zs":
            if not space_pending and not line_start:
                emit(" ", i, i + 1)
                space_pending = True
            i += 1
            continue

        space_pending = False
        line_start = False

        # ④ NFKC（逐字符：全角→半角、兼容字符展开）
        unfolded = unicodedata.normalize("NFKC", ch)

        # ⑤ JSON 反向解码：`\uXXXX` 与单字符转义（**在 NFKC 之后**，见模块 docstring）
        if unfolded == "\\":
            nxt = text[i + 1 : i + 2]
            if (
                nxt == "u"
                and i + 6 <= n
                and _HEX4_RE.fullmatch(text[i + 2 : i + 6])
            ):
                emit(_unify_quotes(chr(int(text[i + 2 : i + 6], 16))), i, i + 6)
                i += 6
                continue
            if nxt in _JSON_ESCAPES:
                emit(_unify_quotes(_JSON_ESCAPES[nxt]), i, i + 2)
                i += 2
                continue

        # ⑥ 引号族统一（1:1，收尾）
        emit(_unify_quotes(unfolded), i, i + 1)
        i += 1

    return Normalized(
        text="".join(out),
        offsets=tuple(span[0] for span in spans),
        spans=tuple(spans),
        source=text,
    )


def normalize_text(text: Text) -> Text:
    """只要归一化后的文本（引用校验的判据用这个）。

    NOTICE：`diagnosis.normalize_text` 是本函数的**薄封装**——单一来源，
    避免"两个模块各有一套归一化"这种最典型的口径漂移。
    """
    return normalize_with_map(text).text


# ---------------------------------------------------------------------------
# 对抗样本（§9.x P-4 点名的四组）+ 反例
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdversarialSample:
    """一组对抗样本：`clean` 是干净形态，`noisy` 是同内容的**含噪形态**。

    `quote` 是从 `noisy` 里截出来的引用片段（含噪）——判据是：
    **它归一化后必须命中 `clean` 的归一化文本**（这正是 §5.3 验收的
    "归一化对抗样本命中率 100%"）。
    """

    name: Text
    label: Text
    clean: Text
    noisy: Text
    quote: Text


ADVERSARIAL_SAMPLES: Tuple[AdversarialSample, ...] = (
    AdversarialSample(
        name="ansi",
        label="loguru 的 ANSI 颜色码",
        clean='{"code": 0, "message": "ok"}',
        noisy='\x1b[32m{"code": 0, "message": "ok"}\x1b[0m',
        quote='\x1b[32m{"code": 0,\x1b[0m',
    ),
    AdversarialSample(
        name="crlf",
        label="Windows CRLF 换行",
        clean="line1\nline2\nline3",
        noisy="line1\r\nline2\r\nline3",
        quote="line1\r\nline2",
    ),
    AdversarialSample(
        name="escape",
        label='JSON 字符串里的 \\" 转义',
        clean='{"msg": "he said "hi""}',
        noisy='{"msg": "he said \\"hi\\""}',
        quote='"he said \\"hi\\""',
    ),
    AdversarialSample(
        name="fullwidth",
        label="全角标点、全角引号与全角空格",
        clean='{"code": 0}',
        noisy="｛＂code＂：　０｝",
        quote="＂code＂",
    ),
)


# 反例（§3.5「判据必须成对」的归一化版）：这些**不许**被合并——
# "归一化越强越好"是错的：把不同内容并成一个，会让**编造的引用**也命中。
MUST_NOT_MERGE: Tuple[Tuple[Text, Text, Text], ...] = (
    ("不同数字是不同内容", "code: 0", "code: 1"),
    ("不同汉字是不同内容", "创建成功", "创建失败"),
    ("大小写有信息，刻意不做 casefold", "TOKEN", "token"),
    ("连续空行是结构，刻意不折叠", "a\n\nb", "a\nb"),
)


def run_selftest(verbose: bool = False) -> int:
    """跑管道自检。返回 0=全绿；非 0=失败项数。"""
    failures: List[Text] = []

    for sample in ADVERSARIAL_SAMPLES:
        # ① 含噪形态归一化后必须与干净形态**逐字相等**
        want = normalize_text(sample.clean)
        got = normalize_text(sample.noisy)
        if got != want:
            failures.append(
                f"[漏并] {sample.name}（{sample.label}）：\n"
                f"      期望 {want!r}\n      实得 {got!r}"
            )
        if verbose:
            print(f"  [合并 ok] {sample.name:10} {sample.label}")

        # ② 引用命中 —— 即 §5.3 验收里的"归一化对抗样本命中率"
        if normalize_text(sample.quote) not in want:
            failures.append(
                f"[未命中] {sample.name}：引用 {sample.quote!r} 归一化后未命中 "
                f"{want!r}（引用校验会因此把真证据判成没证据）"
            )

        # ③ 偏移映射：必须能在**原串**上定位回该引用
        located = normalize_with_map(sample.noisy).locate(normalize_text(sample.quote))
        if located is None:
            failures.append(f"[映射缺失] {sample.name}：locate() 没能给出原串区间")
        else:
            start, stop = located
            if normalize_text(sample.quote) not in normalize_text(sample.noisy[start:stop]):
                failures.append(
                    f"[映射错位] {sample.name}：原串 [{start}, {stop}) 归一化后不含该引用"
                )

        # ④ 逐字符 NFKC 与整串 NFKC 必须一致（偏移映射的**前提**，实测而非声称）
        per_char = "".join(unicodedata.normalize("NFKC", ch) for ch in sample.noisy)
        whole = unicodedata.normalize("NFKC", sample.noisy)
        if per_char != whole:
            failures.append(
                f"[NFKC 偏差] {sample.name}：逐字符 {per_char!r} != 整串 {whole!r}"
                "（偏移映射的前提不成立）"
            )

        # ⑤ 幂等：对四组**真实噪声**再跑一次不应再变
        #（`C:\temp` 型"过度解码后又被折叠"属已登记边界，不在样本内）
        once = normalize_text(sample.noisy)
        if normalize_text(once) != once:
            failures.append(f"[非幂等] {sample.name}：再归一化一次结果又变了")

    # ⑥ 反例：不同内容**不许**被合并
    for label, left, right in MUST_NOT_MERGE:
        if normalize_text(left) == normalize_text(right):
            failures.append(f"[过度合并] {label}：{left!r} 与 {right!r} 归一化后竟然相等")

    # ⑦ 偏移映射本身：区间必须落在原串范围内且不回退
    probe = normalize_with_map(ADVERSARIAL_SAMPLES[3].noisy)
    if any(s < 0 or e > len(probe.source) or e < s for s, e in probe.spans):
        failures.append("[映射越界] spans 里出现了超出原串范围的区间")

    print("=" * 66)
    if failures:
        print(f"归一化管道自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("归一化管道自检全部通过：")
    print(f"  合并书写差异：{len(ADVERSARIAL_SAMPLES)} 组对抗样本归一化后与干净形态逐字相等")
    print(f"  引用命中    ：{len(ADVERSARIAL_SAMPLES)} 组样本的引用归一化后命中率 100%")
    print("  偏移映射    ：归一化区间可映射回原串区间（报告定位用）")
    print(f"  不误合并    ：{len(MUST_NOT_MERGE)} 条反例（数字/汉字/大小写/空行结构）保持不同")
    print("=" * 66)
    return 0


def _read_any_encoding(path: Text) -> Text:
    """按 `utf-8-sig → utf-8 → gbk → gb18030` 依次尝试（与内核导入器同序，§9.x P-6）。"""

    last_error: Optional[BaseException] = None
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            with open(path, encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError as error:
            last_error = error
    raise ValueError(f"无法按已知编码读取 {path}（最后错误：{last_error}）")


def main() -> int:
    # T26（方案 v8 §12.8-①）：重定向/CI 下 Windows 默认 GBK，✓/→ 会抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    import argparse

    parser = argparse.ArgumentParser(description="引用校验的归一化管道（T7）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    parser.add_argument(
        "--file",
        metavar="PATH",
        help="读一份文本（按 utf-8-sig/utf-8/gbk/gb18030 依次尝试）并打印归一化结果",
    )
    args = parser.parse_args()

    if args.file:
        raw = _read_any_encoding(args.file)
        result = normalize_with_map(raw)
        print(f"原串 {len(result.source)} 字符 → 归一化后 {len(result.text)} 字符")
        print(result.text)
        return 0

    return run_selftest(verbose=args.verbose)


__all__ = [
    "ADVERSARIAL_SAMPLES",
    "AdversarialSample",
    "MUST_NOT_MERGE",
    "Normalized",
    "normalize_text",
    "normalize_with_map",
    "run_selftest",
]


if __name__ == "__main__":
    raise SystemExit(main())
