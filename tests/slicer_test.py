# -*- coding: utf-8 -*-
"""切片器的护栏用例 —— §5.1 的「切片」（**P0a**）。

## 本文件的核心判据：**在真实文档上**跑

`interfacetester_ai/slicer.py` 的自检用的是**手写的小文档**；但切片器真正的战场是
`bench/golden/` 那 15 份**真实接口文档**——它们里有表格、代码块、XML、多级标题，
还有两份**非 UTF-8**（GBK / 带 BOM）。

所以这里最重要的两条判据是：

1. **每份 golden 文档都能切完**（不抛 `SliceTooLarge`、不产生空片）；
2. **切片回填后与原文逐行相等**——这一条是"**没有内容被丢掉**"的机器形态。
   少了它，切片器完全可能"悄悄吞掉半节"，而下游只会看到模型"少写了几条断言"。

★编码：GBK / BOM 样本用 `normalize._read_any_encoding()` 读（与内核导入器同序，§9.x P-6）——
切片器**不负责**编码探测（那是文档入口的事），但本文件要证明"探测之后它就能切"。
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import interfacetester_ai.slicer as slicer_module  # noqa: E402
from interfacetester_ai.normalize import _read_any_encoding  # noqa: E402
from interfacetester_ai.slicer import (  # noqa: E402
    SliceTooLarge,
    effective_budget,
    run_selftest,
    split_document,
)

GOLDEN_DIR = os.path.join(BASE, "bench", "golden")


def _golden_documents():
    for name in sorted(os.listdir(GOLDEN_DIR)):
        if name.endswith(".md"):
            yield name, _read_any_encoding(os.path.join(GOLDEN_DIR, name))


class TestEveryGoldenDocumentSlicesCleanly(unittest.TestCase):
    """15 份真实文档全部切一遍（含 GBK / BOM 两份编码样本）。"""

    def test_no_document_raises_and_none_is_empty(self):
        for name, text in _golden_documents():
            with self.subTest(doc=name):
                slices = split_document(text)
                self.assertTrue(slices, f"{name} 切出了空列表（文档有内容却切不出片）")

    def test_every_slice_respects_the_byte_budget(self):
        limit = effective_budget()
        for name, text in _golden_documents():
            for item in split_document(text):
                with self.subTest(doc=name, title=item.title_path):
                    self.assertLessEqual(item.byte_size, limit, f"{name}/{item.title_path}")

    def test_no_content_is_lost_on_any_real_document(self):
        """**关键判据**：按行号回填后与原文逐行相等（只跳过原文里的空行）。"""
        for name, text in _golden_documents():
            with self.subTest(doc=name):
                slices = split_document(text)
                original = text.rstrip("\n").split("\n")
                rebuilt = {}
                for item in slices:
                    for offset, line in enumerate(item.text.split("\n")):
                        rebuilt[item.start_line + offset] = line

                lost = [
                    number
                    for number in range(1, len(original) + 1)
                    if number not in rebuilt and original[number - 1].strip() != ""
                ]
                self.assertEqual(lost, [], f"{name} 有行没落进任何切片：{lost[:5]}")

                wrong = [
                    (number, line, original[number - 1])
                    for number, line in rebuilt.items()
                    if number <= len(original) and line != original[number - 1]
                ]
                self.assertEqual(wrong[:3], [], f"{name} 切片内容与原文不一致")

    def test_title_paths_appear_on_the_interface_documents(self):
        """接口文档至少要切出带 `## ` 的标题路径（否则下游无法定位到"哪个接口"）。"""
        checked = 0
        for name, text in _golden_documents():
            paths = [item.title_path for item in split_document(text)]
            if any("##" in path for path in paths):
                checked += 1
        self.assertGreaterEqual(checked, 10, "过半的 golden 文档应当切出二级标题路径")


class TestOversizedContentIsRefusedNotTruncated(unittest.TestCase):
    """**不静默截断**（§5.1）：超预算单段报错；超预算多段切细——判据成对。"""

    def test_single_oversized_paragraph_is_refused(self):
        doc = "## 巨型节\n\n" + ("x" * 400) + "\n"

        with self.assertRaises(SliceTooLarge) as ctx:
            split_document(doc, budget=100, headroom=0.0)

        self.assertGreater(ctx.exception.overflow, 0)
        self.assertIn("巨型节", str(ctx.exception))

    def test_multi_paragraph_oversized_section_is_split_instead(self):
        doc = "## 多段节\n\n" + "\n\n".join("y" * 200 for _ in range(4)) + "\n"

        pieces = split_document(doc, budget=300, headroom=0.0)

        self.assertGreaterEqual(len(pieces), 2)
        for item in pieces:
            self.assertLessEqual(item.byte_size, 300)


class TestTitlePathAndLineNumbers(unittest.TestCase):
    """标题路径与行号：报告定位与 `source_quote` 校验都依赖它们。"""

    def test_nested_title_path_is_complete(self):
        doc = "# 一级\n\ntext\n\n## 二级\n\nmore\n\n### 三级\n\ndeep\n"
        paths = [item.title_path for item in split_document(doc)]

        self.assertTrue(any(path == "# 一级 > ## 二级" for path in paths), paths)
        self.assertTrue(any(path.endswith("### 三级") for path in paths), paths)

    def test_untitled_leading_content_gets_a_placeholder(self):
        doc = "没有标题的开头段落\n\n# 后来才有标题\n\ntext\n"
        paths = [item.title_path for item in split_document(doc)]

        self.assertIn("(文档开头)", paths)

    def test_indexes_are_sequential_and_line_ranges_are_ordered(self):
        doc = "# A\n\na\n\n## B\n\nb\n\n## C\n\nc\n"
        slices = split_document(doc)

        self.assertEqual([item.index for item in slices], list(range(1, len(slices) + 1)))
        for item in slices:
            self.assertLessEqual(item.start_line, item.end_line)


class TestMetaGuardrails(unittest.TestCase):
    """判据自检：这套断言真的在检查东西。"""

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_the_budget_check_is_neutered(self):
        """注入：把预算上限放大到不可能超出 → "不静默截断"那两条判据必须变红。"""
        with mock.patch.object(slicer_module, "effective_budget", lambda *a, **k: 10**9):
            self.assertNotEqual(run_selftest(), 0, "预算判据失效后自检竟然还是绿的")


class TestCliExitCode(unittest.TestCase):
    """`python -m interfacetester_ai.slicer` 在重定向 + 无 `PYTHONIOENCODING` 下退出码 0。"""

    def test_module_selftest_exits_zero(self):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)

        proc = subprocess.run(
            [sys.executable, "-m", "interfacetester_ai.slicer"],
            cwd=BASE,
            capture_output=True,
            timeout=120,
            env=env,
        )

        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        self.assertIn("切片器自检全部通过".encode("utf-8"), proc.stdout)
