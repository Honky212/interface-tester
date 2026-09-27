# -*- coding: utf-8 -*-
"""归一化管道的护栏用例 —— v8 §5.3 的「★ 归一化管道」/ §9.x **P-4**（**T7**）。

## 为什么需要这个文件

引用校验（§5.1 / §5.3）的判据是「`evidence_quotes` 必须是对应切片的**归一化后逐字子串**」。
这条判据有两个相反的失效方向，**必须成对防**：

1. **漏并**（归一化太弱）：真实日志里同一段内容带 ANSI 颜色码 / CRLF / JSON 转义 / 全角标点，
   归一化没合并 → 真证据被判成"没证据" → 归因降级 → **≥70% 未降级占比**永远达不到；
2. **过度合并**（归一化太强）：把**不同内容**并成一个 → **编造的引用**也能命中 →
   闸门从"防幻觉"变成"帮幻觉过闸"，比第 1 种更坏。

所以本文件的判据是成对的：4 组对抗样本必须合并，4 条反例必须**不**合并；
`run_selftest()` 的注入式元护栏再保证"这套断言真的在检查东西"。

## 与 `diagnosis` 的关系（单一来源）

`diagnosis.normalize_text` 是 `normalize.normalize_text` 的**薄封装**。
本文件钉住这一点：两处对同一输入必须给出**同一个结果**——
"两个模块各有一套归一化"是这类代码最典型的口径漂移，且它的症状
（真证据被判成没证据）与"模型确实没找到证据"在报告里长得一模一样。
"""

import os
import re
import subprocess
import sys
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai import diagnosis, normalize  # noqa: E402
from interfacetester_ai.normalize import (  # noqa: E402
    ADVERSARIAL_SAMPLES,
    MUST_NOT_MERGE,
    normalize_text,
    normalize_with_map,
    run_selftest,
)


class TestPipelineMergesWritingVariants(unittest.TestCase):
    """四组对抗样本（§9.x P-4）：**同一段内容的不同书写**必须合并。"""

    def test_every_sample_normalizes_to_the_clean_form(self):
        for sample in ADVERSARIAL_SAMPLES:
            with self.subTest(sample=sample.name):
                self.assertEqual(
                    normalize_text(sample.noisy),
                    normalize_text(sample.clean),
                    f"{sample.label} 没有被合并（引用校验会因此把真证据判成没证据）",
                )

    def test_quotes_hit_after_normalization(self):
        """§5.3 验收的「归一化对抗样本命中率 100%」——逐组逐条断言。"""
        for sample in ADVERSARIAL_SAMPLES:
            with self.subTest(sample=sample.name):
                self.assertIn(
                    normalize_text(sample.quote),
                    normalize_text(sample.clean),
                    f"{sample.name} 的引用归一化后没命中",
                )

    def test_raw_noise_really_is_present_in_the_samples(self):
        """判据自检：样本里**真的**带着那种噪声（否则这组样本什么也没测）。"""
        noise_markers = {
            "ansi": "\x1b[",
            "crlf": "\r\n",
            "escape": '\\"',
            "fullwidth": "＂",
        }
        for sample in ADVERSARIAL_SAMPLES:
            with self.subTest(sample=sample.name):
                self.assertIn(
                    noise_markers[sample.name],
                    sample.noisy,
                    f"{sample.name} 样本里没有对应噪声，这组对抗样本是空跑的",
                )
                self.assertNotIn(
                    noise_markers[sample.name],
                    normalize_text(sample.noisy),
                    f"{sample.name} 归一化后仍残留噪声",
                )


class TestPipelineDoesNotOverMerge(unittest.TestCase):
    """反例：**不同内容不许被合并**（否则编造的引用也能命中）。"""

    def test_different_content_stays_different(self):
        for label, left, right in MUST_NOT_MERGE:
            with self.subTest(label=label):
                self.assertNotEqual(
                    normalize_text(left),
                    normalize_text(right),
                    f"{label}：{left!r} 与 {right!r} 被合并了（过度归一化）",
                )

    def test_identity_for_plain_text(self):
        """已经干净的文本不该被改动（零噪声 → 恒等）。

        NOTICE：全角冒号（`：`）**不**在此列——它属 NFKC 要合并的那类书写差异
        （见 `fullwidth` 对抗样本），放进"恒等"样本会与本模块的口径自相矛盾。
        """
        for text in ("hello world", "code: 0\nmessage: ok", "中文内容 正常"):
            with self.subTest(text=text):
                self.assertEqual(normalize_text(text), text)

    def test_idempotent_on_the_four_real_noises(self):
        for sample in ADVERSARIAL_SAMPLES:
            with self.subTest(sample=sample.name):
                once = normalize_text(sample.noisy)
                self.assertEqual(normalize_text(once), once, "再归一化一次结果又变了")


class TestOffsetMap(unittest.TestCase):
    """偏移映射：归一化区间必须能映射回**原串**（报告定位用）。"""

    def test_locate_returns_the_original_span(self):
        sample = ADVERSARIAL_SAMPLES[3]  # fullwidth
        located = normalize_with_map(sample.noisy).locate(normalize_text(sample.quote))

        self.assertIsNotNone(located)
        start, stop = located
        self.assertEqual(sample.noisy[start:stop], sample.quote)

    def test_locate_lands_after_deleted_noise(self):
        """前面被删掉一段（ANSI）后，定位必须落在**原文**的正确位置。"""
        sample = ADVERSARIAL_SAMPLES[0]  # ansi
        located = normalize_with_map(sample.noisy).locate('{"code"')

        self.assertIsNotNone(located)
        start, stop = located
        self.assertEqual(sample.noisy[start:stop], '{"code"')

    def test_empty_input_is_safe(self):
        result = normalize_with_map("")

        self.assertEqual((result.text, result.offsets, result.spans), ("", (), ()))
        self.assertEqual(result.span(0, 0), (0, 0))
        self.assertIsNone(result.locate("anything"))

    def test_spans_stay_inside_the_source_and_never_go_backwards(self):
        for sample in ADVERSARIAL_SAMPLES:
            with self.subTest(sample=sample.name):
                result = normalize_with_map(sample.noisy)
                previous = 0
                for start, stop in result.spans:
                    self.assertGreaterEqual(start, previous, "区间回退了（映射不可用）")
                    self.assertLessEqual(stop, len(result.source))
                    previous = start
                self.assertEqual(len(result.offsets), len(result.text))

    def test_crlf_span_covers_both_characters(self):
        """`\\r\\n` 折叠出的那个换行，其原文区间要覆盖两个字符（否则报告定位会截断）。"""
        result = normalize_with_map("a\r\nb")
        index = result.text.index("\n")

        self.assertEqual(result.spans[index], (1, 3))


class TestSingleSourceWithDiagnosis(unittest.TestCase):
    """`diagnosis.normalize_text` 必须是本模块的薄封装（**单一来源**）。"""

    def test_both_agree_on_every_adversarial_sample(self):
        for sample in ADVERSARIAL_SAMPLES:
            with self.subTest(sample=sample.name):
                self.assertEqual(
                    diagnosis.normalize_text(sample.noisy),
                    normalize_text(sample.noisy),
                )

    def test_noisy_evidence_quote_still_passes_the_evidence_check(self):
        """端到端形态：**含噪**证据喂给诊断，引用校验必须仍然认可。

        NOTICE：直接调 `diagnosis._quote_ok`——"真证据被归一化漏掉"最终就是在这里
        变成"降级为未知"的，所以这条用例盯的是后果而不是中间量。
        """
        doc_text = '登录成功后返回 {"code": 0, "message": "ok"}'
        noisy_quote = {"source": "doc", "text": '{"code": 0, "message": "ok"}\x1b[0m'}

        self.assertTrue(diagnosis._quote_ok(noisy_quote, doc_text, ""))


class TestMetaGuardrails(unittest.TestCase):
    """判据自检：这套断言真的在检查东西（防「护栏打偏」）。"""

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_ansi_stripping_is_broken(self):
        with mock.patch.object(normalize, "_ANSI_RE", re.compile("绝不可能匹配的图案")):
            self.assertNotEqual(run_selftest(), 0, "ANSI 不再被去掉，自检竟然还是绿的")

    def test_selftest_turns_red_when_json_unescaping_is_broken(self):
        with mock.patch.object(normalize, "_JSON_ESCAPES", {}):
            self.assertNotEqual(run_selftest(), 0, "JSON 反向解码失效，自检竟然还是绿的")


class TestCliExitCode(unittest.TestCase):
    """`python -m interfacetester_ai.normalize` 在重定向 + 无 `PYTHONIOENCODING` 下退出码 0。"""

    def test_module_selftest_exits_zero(self):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)

        proc = subprocess.run(
            [sys.executable, "-m", "interfacetester_ai.normalize"],
            cwd=BASE,
            capture_output=True,
            timeout=120,
            env=env,
        )

        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
