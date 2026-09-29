"""golden 基线护栏：把「文档 → 用例」装配链路的通过率**钉住**。

## 为什么需要这条护栏

方案 §7.1 的 P0a 验收标准是「30 份文档：≤2 轮修正环后 `validate_emitted_case` 通过率 ≥90%」。
那个数字**必须先测出来再承诺**——本护栏就是那个「基线」的锚点：

- golden 通过率**下降** → 立即变红（说明装配链路或内核护栏被改坏了，是回归）；
- golden 通过率**上升** → 也要变红，直到有人**显式**更新基线常量
  （防止「通过率悄悄变了但没人知道」，与本仓「基线数字只有一处事实来源」同取向）。

## 与方案的关系

本护栏覆盖的是方案里的 **L1/L2 两级闸门 + 装配器**，**不涉及任何模型**。
它是「模型能力」之外的**全部脚手架**——脚手架不稳，换什么模型都白搭。

口径与 `bench/golden_harness.py` 完全一致（直接调用它，不复制逻辑）。
"""

import io
import json
import os
import subprocess
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:  # T19 需要直接 import bench.golden_harness（此前只走子进程）
    sys.path.insert(0, BASE)
HARNESS = os.path.join(BASE, "bench", "golden_harness.py")
GOLDEN_DIR = os.path.join(BASE, "bench", "golden")

# ---------------------------------------------------------------------------
# 基线：本文件是全仓**唯一**的 golden 通过率事实来源。
# 改这个数字前，请先确认「为什么变了」——它是护栏，不是配置项。
# 实测环境：Python 3.12.10 / interfacetester 5.0.1 / 无任何可选 extra。
#
# ★v8 第九/十批（2026-09-24，T9）：3 → 15 份。新增 12 份覆盖
# 「单步 / 多步依赖 / 含 schema / 含 XML / 含业务错误码 / 编码对抗」六类
# （P0a 验收① 的类别要求），其中 legacy_gbk_doc.md 以 **GBK** 保存、
# utf8_bom_doc.md 带 **UTF-8 BOM**——两份都是"探测顺序必须正确"的端到端样本。
GOLDEN_TOTAL = 15
GOLDEN_PASSED = 15
# ---------------------------------------------------------------------------


def _run_harness() -> dict:
    """以子进程跑 harness 并解析 JSON（隔离 goldens 的生成副作用）。"""
    proc = subprocess.run(
        [sys.executable, HARNESS, "--json"],
        cwd=BASE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    output = proc.stdout or ""
    start = output.find("{")
    if start < 0:
        raise AssertionError(
            "harness 没有输出 JSON（判据失效或 harness 崩了）：\n"
            + (output[-1200:] + "\n--- stderr ---\n" + (proc.stderr or "")[-1200:])
        )
    return json.loads(output[start:])


class TestClassifyBucketsArePinned(unittest.TestCase):
    """★T19（方案 v8 §3.5 第四道元护栏）：钉住 `_classify` 的可达桶名集合。

    判据（§3.2 / §十 T19）：失败分桶是**可达三桶** `{load, make, compile}`——
    `FAIL_PROJECT` / `FAIL_OTHER` 虽作为分类学常量存在，但 `_classify` 从不返回
    它们。v6 曾称「分桶已被 golden_baseline_test.py 钉住」，v7 复核实测为假
    （§12.7 乙③）——本用例补上这道钉子。若确要 `project` 桶：先改 `_classify`，
    再同步改本用例与 §3.2 口径（顺序不能反，否则记分卡口径静默漂移）。
    """

    REACHABLE_BUCKETS = {"load", "make", "compile"}

    def test_classify_buckets_are_pinned(self):
        from bench import golden_harness  # noqa: PLC0415

        import inspect

        # 1) 代表样本：每条实际归类必须正确（分类行为钉住）
        ValidationError = type("ValidationError", (Exception,), {})
        TestCaseFormatError = type("TestCaseFormatError", (Exception,), {})
        ParamsError = type("ParamsError", (Exception,), {})
        reps = [
            (ValidationError("bad"), "load"),
            (TestCaseFormatError("bad"), "load"),
            (ParamsError("bad"), "make"),
            (SyntaxError("bad"), "compile"),
            (RuntimeError("compile() returned None"), "compile"),
            (RuntimeError("boom"), "make"),
        ]
        for exc, expected in reps:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(
                    golden_harness._classify(exc),
                    expected,
                    f"{type(exc).__name__} 应归入 {expected!r} 桶——分类行为漂移",
                )

        # 2) 三桶全部可达（缺一即「死桶」，failure_buckets 口径出现空洞）
        self.assertEqual(
            {expected for _, expected in reps},
            self.REACHABLE_BUCKETS,
            "可达桶集合漂移：记分卡 failure_buckets 的口径随之失真",
        )

        # 3) 无第四桶：_classify 源码不得引用不可达常量（防 project/other 悄悄可达）
        src = inspect.getsource(golden_harness._classify)
        self.assertNotIn(
            "FAIL_PROJECT", src, "project 桶变得可达——先同步 §3.2 与本用例口径"
        )
        self.assertNotIn("FAIL_OTHER", src, "other 兜底桶变得可达——同上")

        # 4) 常量值对账：三个桶常量的字面值与判据一致（防常量改字导致报告失真）
        self.assertEqual(
            self.REACHABLE_BUCKETS,
            {golden_harness.FAIL_LOAD, golden_harness.FAIL_MAKE, golden_harness.FAIL_COMPILE},
            "FAIL_* 常量值与三桶口径不一致（记分卡键名随之漂移）",
        )


class TestGoldenBaseline(unittest.TestCase):
    """golden 集与装配链路的确定性护栏。"""

    @classmethod
    def setUpClass(cls):
        cls.report = _run_harness()

    def test_golden_set_is_not_empty(self):
        """判据自检：golden 集不能为空，否则下面的断言会「假通过」。"""
        self.assertGreaterEqual(
            self.report["total"],
            3,
            "golden 样本太少（或目录读不到）——本护栏无法反映真实装配能力",
        )

    def test_documented_baseline_matches_actual(self):
        """记分卡必须与基线常量**逐字一致**（通过率漂移必须被看见）。"""
        self.assertEqual(
            (self.report["total"], self.report["passed"]),
            (GOLDEN_TOTAL, GOLDEN_PASSED),
            f"golden 基线漂移：实测 {self.report['passed']}/{self.report['total']}，"
            f"基线记录 {GOLDEN_PASSED}/{GOLDEN_TOTAL}。\n"
            f"  失败原因分布：{self.report['failure_buckets']}\n"
            f"  逐条结果："
            + json.dumps(
                [
                    {"name": r["name"], "ok": r["ok"], "reason": (r.get("reason") or "")[:200]}
                    for r in self.report["results"]
                ],
                ensure_ascii=False,
                indent=2,
            )
            + "\n  修法：确认是回归还是改进；改进则显式更新本文件顶部的基线常量。",
        )

    def test_every_golden_case_passes(self):
        """逐条断言：任何一条 golden 掉出，都要能**点名**是哪一条、为什么。"""
        failures = [r for r in self.report["results"] if not r["ok"]]
        self.assertEqual(
            failures,
            [],
            "以下 golden 用例未通过 L2 校验：\n"
            + "\n".join(f"  - {r['name']}（{r['bucket']}）：{r.get('reason', '')[:300]}" for r in failures),
        )

    def test_golden_docs_have_paired_yaml(self):
        """每份 golden 文档都要有配对的期望 YAML，否则样本静默减少。"""
        docs = sorted(n[:-3] for n in os.listdir(GOLDEN_DIR) if n.endswith(".md"))
        self.assertTrue(docs, "golden 目录里没有 .md 文档")
        for stem in docs:
            self.assertTrue(
                os.path.isfile(os.path.join(GOLDEN_DIR, stem + ".yml")),
                f"golden 文档 {stem}.md 没有配对的期望产物 {stem}.yml",
            )

    def test_harness_detects_injected_fault(self):
        """**判据自检**：故意注入一个非法算子名，harness 必须报 FAIL。

        没有这一条，上面的「全绿」可能只是因为 harness 根本没在检查任何东西
        （本仓把这类叫「护栏打偏」，见《架构与调用链.md》§7 的自纠记录）。
        """
        src_yml = os.path.join(GOLDEN_DIR, "login.yml")
        self.assertTrue(os.path.isfile(src_yml), "缺少 login.yml，无法做判据自检")

        probe_stem = "_selftest_inject"
        probe_md = os.path.join(GOLDEN_DIR, probe_stem + ".md")
        probe_yml = os.path.join(GOLDEN_DIR, probe_stem + ".yml")
        with io.open(src_yml, encoding="utf-8") as fp:
            content = fp.read()
        # 注入一个**不存在**的算子：生成期白名单必须拦住
        injected = content.replace("- eq: [status_code, 200]", "- equals2: [status_code, 200]", 1)
        self.assertNotEqual(injected, content, "注入失败：没找到可替换的断言，判据失效")

        try:
            with io.open(probe_yml, "w", encoding="utf-8") as fp:
                fp.write(injected)
            with io.open(probe_md, "w", encoding="utf-8") as fp:
                fp.write("# selftest probe\n")

            report = _run_harness()
            bad = [r for r in report["results"] if r["name"] == probe_stem]
            self.assertEqual(len(bad), 1, "探针样本没有被 harness 收集到（判据失效）")
            self.assertFalse(
                bad[0]["ok"],
                "harness 没有检出注入的非法算子名 —— 它是假绿的，本文件其余断言都不可信",
            )
        finally:
            for path in (probe_md, probe_yml):
                if os.path.exists(path):
                    os.remove(path)


if __name__ == "__main__":
    unittest.main()
