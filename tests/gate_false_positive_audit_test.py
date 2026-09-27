# -*- coding: utf-8 -*-
"""闸门误伤率审计的护栏用例 —— §3.5 / **T10**。

## 为什么需要这个文件

`bench/gate_false_positive_audit.py` 把闸门挂到 **15 份真实 golden 样本**上，
判据是「**误伤率必须为 0**」。这条判据有两个方向会失效，都必须防：

1. **假绿**：审计脚本没真在跑闸门（例如 golden 目录读不到 → `total=0` → 误伤率 0%）；
2. **漏报**：真有陷阱的样本混进来，审计却没把它算成误伤。

第 1 种由「样本数必须等于 golden 基线（15）」+ 「注入式元护栏」共同防；
第 2 种由「注入一份**带已知陷阱**的样本 → 必须被报出来」防。

## 本文件顺带钉住一个**实测数字**

T21（S4 判据扩展到 headers/params/body 的 `$var` 引用、词边界匹配）落地时，
文档把"能消掉多少 PENDING 噪声"标成**未实测**，并写明"需要 golden 扩到 15 份后统计（T10）"。
现在这个数字有了：**15 份 golden 的 PENDING = 0**。
它是一条**会漂移的判据**——若哪天判据收紧过头或 golden 里出现"提取了却没人用"的写法，
这条会变红，提醒我们回头看。
"""

import io
import json
import os
import subprocess
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from bench.gate_false_positive_audit import audit  # noqa: E402

GOLDEN_DIR = os.path.join(BASE, "bench", "golden")
AUDIT_SCRIPT = os.path.join(BASE, "bench", "gate_false_positive_audit.py")

# 与 `tests/golden_baseline_test.py::GOLDEN_TOTAL` 同源（T9 扩容后的基线）
GOLDEN_TOTAL = 15


class TestAuditFlagsNothingOnTheRealGoldenSet(unittest.TestCase):
    """真实 golden 集：**误伤必须为 0**（§3.5 的"假报会让真报被无视"）。"""

    @classmethod
    def setUpClass(cls):
        cls.report = audit()

    def test_sample_count_matches_the_golden_baseline(self):
        """判据自检：样本数对不上就说明审计没读到 golden（此时"零误伤"是假绿）。"""
        self.assertEqual(
            self.report["total"],
            GOLDEN_TOTAL,
            "审计样本数与 golden 基线不一致——先确认是扩容（更新基线）还是目录读不到",
        )

    def test_no_false_positives(self):
        injured = self.report["injured"]
        detail = "\n".join(
            f"  - {entry['name']}: {[r['code'] for r in entry.get('rejects', [])]}"
            for entry in injured
        )
        self.assertEqual(
            self.report["false_positives"],
            0,
            "真实 golden 用例被闸门误伤（要么修判据，要么把该合法形态写回 MUST_ALLOW）：\n"
            + detail,
        )

    def test_pending_noise_is_zero_after_t21(self):
        """T21 判据加严后的**实测收益**：15 份 golden 的 PENDING = 0。

        NOTICE：这不是"越少越好"的指标——PENDING 是 S4 请人工确认的**设计意图**。
        数字变大时先看是不是 golden 里新加了"提取了但下游没人用"的写法，
        而不是急着把 S4 判据放宽。
        """
        self.assertEqual(
            self.report["pending_cases"],
            0,
            "出现了新的 PENDING 噪声——检查是否为 extract 键未被任何下游引用",
        )


class TestAuditDetectsAnInjectedTrap(unittest.TestCase):
    """元护栏：注入一份**带已知陷阱**的样本，审计必须报出来（否则"零误伤"无意义）。"""

    PROBE_STEM = "_audit_inject"

    def _probe_paths(self):
        return (
            os.path.join(GOLDEN_DIR, self.PROBE_STEM + ".md"),
            os.path.join(GOLDEN_DIR, self.PROBE_STEM + ".yml"),
        )

    def test_injected_silent_trap_is_reported_as_a_false_positive(self):
        probe_md, probe_yml = self._probe_paths()
        src_yml = os.path.join(GOLDEN_DIR, "login.yml")
        with io.open(src_yml, encoding="utf-8") as handle:
            content = handle.read()
        # S1（伪存在性）：`not_equal: [x, ""]` 在字段根本不存在时**会通过**
        injected = content.replace(
            "- eq: [body.code, 0]", '- not_equal: [body.data.token, ""]', 1
        )
        self.assertNotEqual(injected, content, "注入失败：没找到可替换的断言")

        try:
            with io.open(probe_yml, "w", encoding="utf-8") as handle:
                handle.write(injected)
            with io.open(probe_md, "w", encoding="utf-8") as handle:
                handle.write("# audit selftest probe\n")

            report = audit()
        finally:
            for path in self._probe_paths():
                if os.path.exists(path):
                    os.remove(path)

        names = [entry["name"] for entry in report["injured"]]
        self.assertIn(
            self.PROBE_STEM,
            names,
            f"注入了已知静默陷阱，审计却没报成误伤：injured={names}",
        )
        codes = {
            finding["code"]
            for entry in report["injured"]
            if entry["name"] == self.PROBE_STEM
            for finding in entry.get("rejects", [])
        }
        self.assertIn("S1", codes, f"误伤报出来了，但编号不对：{codes}")


class TestCliExitCodes(unittest.TestCase):
    """`python bench/gate_false_positive_audit.py`：干净 → 0；有误伤 → 非 0 且**点名**。"""

    def _run(self):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        return subprocess.run(
            [sys.executable, AUDIT_SCRIPT],
            cwd=BASE,
            capture_output=True,
            timeout=300,
            env=env,
        )

    def test_exit_zero_on_the_clean_golden_set(self):
        proc = self._run()

        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        self.assertIn("误伤率          : 0.0%".encode("utf-8"), proc.stdout)
