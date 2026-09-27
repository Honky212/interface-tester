# -*- coding: utf-8 -*-
r"""slicer —— **文档切片器**（§5.1 的「切片」；**P0a** 交付）。

## 为什么需要它

`haify gen` 的输入是**整篇文档**，但送给模型的必须是**受预算约束的切片**：

> 切片：按文档标题层级切块；**统一按"输入预算字节数"**：`INPUT_BUDGET_BYTES = 8000`
> （含 system + few-shot），切片留 15% 余量。

三件事必须由切片器负责（都不能交给模型）：

1. **预算**：每片 ≤ `INPUT_BUDGET_BYTES × (1 − INPUT_BUDGET_HEADROOM)`（**字节口径**，见 `prompts.py`）；
2. **定位**：每片记住自己的**行号范围**——`source_quote` 校验与报告定位都要用它
   （"这句话在原文档第几行"是文档作者能去改的前提）；
3. **不静默截断**：单片超预算且**无法再切** → 报错，把选择权交回调用方
   （换更小粒度 / 让文档作者拆文档 / 显式降级）——静默截断会让模型看到半句话而报告显示"成功"。

## 切分规则（两级，先结构后备选）

    ① 按标题（`#`~`######`）切 → 每片带完整**标题路径**（如 `# 登录接口 > ## POST /api/login`）
    ② 仍超预算的片 → 按**空行分段**切（保持段落完整）
    ③ 还超 → `SliceTooLarge`（消息附：片标题 / 字节数 / 上限 / 建议）

★边界（诚实）：切片只做**结构切分**，不做语义判断——"这两节讲的是同一个接口"**不由切片器决定**
（理解内容正是模型该做的事）。所以**宁可多切几片**：多切只花预算，错并会让两边都错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

from interfacetester_ai.prompts import INPUT_BUDGET_BYTES, INPUT_BUDGET_HEADROOM

# Markdown 标题：`#`~`######` + 空格（ATX 风格；这是接口文档的主流写法）
TITLE_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$")

# ★**端点形态**的**单一来源**（2026-09-26 归并）：`gen.SLICE_ENDPOINT_RE` 与
#   `citation` 的"这一节里有没有端点"都必须用同一个正则 —— 三份副本迟早漂移，
#   而漂移的后果是"切片说这节是接口 / 归属说不是"这种**自相矛盾**的产物。
#   形态覆盖（真文档里三种写法都要认，且**不限行首**——表格里的端点行首是 `|`）：
#     - 标题：`## POST /api/order`
#     - 表格：`| **URL** | `POST /api/directory/create` |`
#     - curl：`curl -X POST https://host/api/directory/create`
ENDPOINT_RE = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b"
    r"[\s:`|]*?"
    r"(/[^\s`|)\"']+|https?://[^\s`|)\"']+)",
    re.IGNORECASE,
)


class SliceTooLarge(Exception):
    """单片超过预算且无法再切——**报错，不截断**（§5.1 的"不静默截断"）。"""

    def __init__(self, title: Text, size: int, budget: int, start_line: int = 0) -> None:
        self.title = title
        self.size = size
        self.budget = budget
        self.start_line = start_line
        self.overflow = size - budget
        super().__init__(
            f"切片超预算且无法再切：{title!r}（原文档第 {start_line} 行起）"
            f"\n  实测 {size} 字节 > 上限 {budget} 字节（超出 {self.overflow}）。"
            "\n  处置：① 让文档作者把这一节拆小；② 或显式把该片标记为需要人工，"
            "\n  不要静默截断——模型看到半句话时，报告里显示的却是「成功」。"
        )


@dataclass(frozen=True)
class Slice:
    """一个文档切片：**内容 + 出处**（标题路径与行号范围）。"""

    index: int
    title_path: Text
    text: Text
    start_line: int  # 1-based，含
    end_line: int  # 1-based，含
    byte_size: int

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "index": self.index,
            "title_path": self.title_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "byte_size": self.byte_size,
        }


def effective_budget(
    budget: int = INPUT_BUDGET_BYTES, headroom: float = INPUT_BUDGET_HEADROOM
) -> int:
    """切片可用的字节上限 = 预算 × (1 − 余量)。余量留给 system + few-shot（§5.1）。"""
    return int(budget * (1.0 - headroom))


def _title_level(line: Text) -> Optional[Tuple[int, Text]]:
    match = TITLE_RE.match(line)
    if not match:
        return None
    return len(match.group(1)), match.group(2)


def _byte_size(text: Text) -> int:
    return len(text.encode("utf-8"))


def _group_by_title(lines: Sequence[Text]) -> List[Dict[str, Any]]:
    """按标题把行分组，并给每组算**完整标题路径**（供报告与 prompt 定位用）。"""
    groups: List[Dict[str, Any]] = []
    stack: List[Tuple[int, Text]] = []
    for number, line in enumerate(lines, start=1):
        found = _title_level(line)
        if found:
            level, title = found
            stack = [(lv, tt) for lv, tt in stack if lv < level]
            stack.append((level, title))
            path = " > ".join(f"{'#' * lv} {tt}" for lv, tt in stack)
            groups.append({"path": path, "start": number, "lines": [line]})
        else:
            if not groups:
                # 文档开头没有标题的内容也要有归属（否则它会静默消失）
                groups.append({"path": "(文档开头)", "start": number, "lines": []})
            groups[-1]["lines"].append(line)
    return groups


def _paragraphs(lines: Sequence[Text], start_line: int) -> List[Tuple[int, Text]]:
    """把一段行按**空行**再切（保持段落完整），返回 `(起行号, 文本)`。"""
    pieces: List[Tuple[int, Text]] = []
    buffer: List[Text] = []
    piece_start = start_line
    for offset, line in enumerate(lines):
        if line.strip() == "" and buffer:
            pieces.append((piece_start, "\n".join(buffer)))
            buffer = []
        elif not buffer:
            piece_start = start_line + offset
        buffer.append(line)
    if buffer:
        pieces.append((piece_start, "\n".join(buffer)))
    return pieces


@dataclass(frozen=True)
class HeadingSpan:
    """一个标题及其**整块**（到"下一个同级或更高级标题"之前）——**按层级算跨度**。

    ★为什么需要它（2026-09-26，WP2.2 证据绑定）：`split_document()` 的片是**扁平**的
    （每个标题一片、超预算再按空行切，**不含子标题**），而"**这一节到底包含哪些内容**"
    是 T28 要问的问题——字段表恰恰常挂在 `### 请求体` 这类子标题下。
    `gen.interface_sections()` 早就有同一套跨度算法（§9.6），本类型是它的**通用化**
    （不要求段里有端点），供引用解析复用。
    """

    title: Text
    level: int
    start_line: int  # 1-based，含（= 标题行）
    end_line: int  # 1-based，含
    text: Text
    title_path: Text = ""

    def contains_line(self, line_no: int) -> bool:
        return bool(line_no) and self.start_line <= line_no <= self.end_line

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "title": self.title,
            "level": self.level,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "title_path": self.title_path,
        }


def heading_spans(text: Text) -> List[HeadingSpan]:
    """把文档切成**标题块**（层级跨度），顺序与文档一致；无标题的文档 → 空列表。

    ★这段内容**只有一份实现**：`gen.interface_sections()` 用同一套跨度规则挑接口段。
    """
    lines = (text or "").split("\n")
    heads: List[Tuple[int, int, Text, Text]] = []  # (行号, 层级, 标题, 标题路径)
    stack: List[Tuple[int, Text]] = []
    for number, line in enumerate(lines, start=1):
        found = _title_level(line)
        if not found:
            continue
        level, title = found
        stack = [(lv, tt) for lv, tt in stack if lv < level]
        stack.append((level, title))
        path = " > ".join(f"{'#' * lv} {tt}" for lv, tt in stack)
        heads.append((number, level, title, path))

    out: List[HeadingSpan] = []
    for index, (number, level, title, path) in enumerate(heads):
        end = len(lines)
        for next_number, next_level, _title, _path in heads[index + 1 :]:
            if next_level <= level:
                end = next_number - 1
                break
        out.append(
            HeadingSpan(
                title=title,
                level=level,
                start_line=number,
                end_line=end,
                text="\n".join(lines[number - 1 : end]),
                title_path=path,
            )
        )
    return out


def split_document(
    text: Text,
    *,
    budget: Optional[int] = None,
    headroom: Optional[float] = None,
) -> List[Slice]:
    """把文档切成受预算约束的片；每片带**标题路径 + 行号范围**。

    超预算且无法再切 → `SliceTooLarge`（**不静默截断**，§5.1）。
    """
    limit = effective_budget(
        INPUT_BUDGET_BYTES if budget is None else budget,
        INPUT_BUDGET_HEADROOM if headroom is None else headroom,
    )
    lines = text.split("\n")
    if not text.strip():
        # 空文档 → 空列表：由调用方判"没东西可生成"（`doc_quality` 的 D4 已经在生成前拦了空文件）
        return []
    slices: List[Slice] = []

    def add(path: Text, body: Text, start_line: int) -> None:
        body = body.rstrip("\n")
        slices.append(
            Slice(
                index=len(slices) + 1,
                title_path=path,
                text=body,
                start_line=start_line,
                end_line=start_line + body.count("\n"),
                byte_size=_byte_size(body),
            )
        )

    for group in _group_by_title(lines):
        body = "\n".join(group["lines"]).rstrip("\n")
        if _byte_size(body) <= limit:
            add(group["path"], body, group["start"])
            continue

        # ② 超预算 → 按空行分段再切
        pieces = _paragraphs(group["lines"], group["start"])
        if len(pieces) <= 1:
            # 一整段就超了：**报错**，不截断（且不假装"切过了"）
            raise SliceTooLarge(group["path"], _byte_size(body), limit, group["start"])
        for piece_start, piece_text in pieces:
            if _byte_size(piece_text.rstrip("\n")) > limit:
                raise SliceTooLarge(
                    group["path"], _byte_size(piece_text), limit, piece_start
                )
            add(group["path"], piece_text, piece_start)

    return slices


def run_selftest(verbose: bool = False) -> int:
    """跑切片器自检（**纯逻辑、不写盘**）。返回 0=全绿；非 0=失败项数。"""
    failures: List[Text] = []

    doc = (
        "# 用户登录接口\n"
        "\n"
        "登录并返回访问令牌。\n"
        "\n"
        "## POST /api/login\n"
        "\n"
        "### 请求体（JSON）\n"
        "\n"
        "| 字段 | 类型 | 必填 |\n"
        "| --- | --- | --- |\n"
        "| username | string | 是 |\n"
        "\n"
        "### 响应\n"
        "\n"
        '```json\n{"code": 0, "data": {"token": "..."}}\n```\n'
        "\n"
        "- `code`：整数，0 表示成功\n"
    )
    slices = split_document(doc)

    # ① 覆盖性：按行号把切片回填，必须与原文**逐行相等**（证明没有内容被丢掉）
    rebuilt: Dict[int, Text] = {}
    for item in slices:
        for offset, line in enumerate(item.text.split("\n")):
            rebuilt[item.start_line + offset] = line
    original_lines = doc.rstrip("\n").split("\n")
    missing = [
        number
        for number in range(1, len(original_lines) + 1)
        if number not in rebuilt and original_lines[number - 1].strip() != ""
    ]
    if missing:
        failures.append(f"[内容丢失] 这些行没有落进任何切片：{missing[:5]}")
    mismatched = [
        number
        for number, line in rebuilt.items()
        if number <= len(original_lines) and line != original_lines[number - 1]
    ]
    if mismatched:
        failures.append(f"[内容错位] 这些行的内容与原文不一致：{mismatched[:5]}")

    # ② 标题路径：层级要正确（`# A > ## B`）
    paths = [item.title_path for item in slices]
    if not any("## POST /api/login" in path and path.startswith("# 用户登录接口") for path in paths):
        failures.append(f"[标题路径错] 期望看到 `# 用户登录接口 > ## POST /api/login`，实为 {paths}")
    if not any("### 响应" in path for path in paths):
        failures.append(f"[标题路径错] 三级标题没出现在路径里：{paths}")

    # ③ 预算：每片都必须在上限内
    limit = effective_budget()
    over = [item for item in slices if item.byte_size > limit]
    if over:
        failures.append(f"[超预算] 有切片超过 {limit} 字节：{[(i.title_path, i.byte_size) for i in over]}")

    # ④ 行号范围：起 <= 终，且落在原文范围内
    for item in slices:
        if not (item.start_line <= item.end_line <= len(original_lines)):
            failures.append(f"[行号越界] {item.title_path}: {item.start_line}-{item.end_line}")

    # ⑤ 不静默截断：超预算且**只有一整段** → 必须报错
    huge_single_paragraph = "## 巨型节\n\n" + ("x" * 400) + "\n"
    try:
        split_document(huge_single_paragraph, budget=100, headroom=0.0)
    except SliceTooLarge as error:
        if error.size <= error.budget or error.overflow <= 0:
            failures.append(f"[报错信息不对] 超出量应 > 0：{error}")
        if verbose:
            print("  [拦下 ok] 超预算单片被拒（不静默截断）")
    else:
        failures.append("[漏放] 超预算的单段竟然被切片了（静默截断）")

    # ⑥ 多段超预算 → 应被**切细**而不是报错（判据成对：不能一律报错）
    multi = "## 多段节\n\n" + "\n\n".join("y" * 200 for _ in range(4)) + "\n"
    try:
        pieces = split_document(multi, budget=300, headroom=0.0)
    except SliceTooLarge as error:
        failures.append(f"[误伤] 多段超预算应当能切细，却报了错：{error}")
    else:
        if len(pieces) < 2:
            failures.append(f"[未切细] 多段超预算只切出 {len(pieces)} 片")
        if any(item.byte_size > 300 for item in pieces):
            failures.append("[超预算] 切细后仍有片超预算")

    # ⑦ 空文档 → 空列表（由调用方判"没东西可生成"，不是切片器的职责）
    if split_document("") != []:
        failures.append("[空文档] 空文档应返回空列表")

    print("=" * 66)
    if failures:
        print(f"切片器自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("切片器自检全部通过：")
    print(f"  覆盖性  ：{len(slices)} 片按行号回填后与原文逐行相等（没有内容被丢掉）")
    print(f"  预算    ：每片 ≤ {limit} 字节（= {INPUT_BUDGET_BYTES} × (1 − {INPUT_BUDGET_HEADROOM})）")
    print("  可定位  ：每片带标题路径 + 行号范围（供 source_quote 校验与报告定位）")
    print("  不截断  ：超预算单段 → 报错；超预算多段 → 切细（判据成对）")
    print("=" * 66)
    return 0


def main() -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="文档切片器（P0a）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "ENDPOINT_RE",
    "TITLE_RE",
    "HeadingSpan",
    "Slice",
    "SliceTooLarge",
    "effective_budget",
    "heading_spans",
    "run_selftest",
    "split_document",
]


if __name__ == "__main__":
    raise SystemExit(main())
