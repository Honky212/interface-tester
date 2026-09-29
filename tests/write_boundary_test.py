# -*- coding: utf-8 -*-
"""红线③ 写盘白名单静态扫描器的护栏用例（v8 §十 T18）。

## 为什么需要这个文件

红线③ 的 v6 原文（「LLM 模块里禁止出现 open( / Path.write」）与 §6 的缓存/审计
落盘直接冲突，v7 §0.4-② 把它重写为「**写入目标路径 + 模块白名单**」——本用例钉的
就是这个可执行形态（`bench/write_boundary_scanner.py`）。

## 判据（成对，同 `silent_traps_test.py` 的纪律）

- **违规必红**：llm.py 写盘（open 'w' / write_text）；cache/manifest/pending 越出
  `.ai/`（写 `cases/`、写别的根、目标不可静态判定）；未登记模块任何写盘调用
  （含写 `cases/`——「cases/ 唯一入口是装配器」）。
- **合规必绿**：llm.py 只读（open 'rb'）；cache/manifest/pending 写 `.ai/` 下
  （字面量 / f-string / 拼接 / `Path(...)` / `os.path.join(...)` 首参形态）；
  装配器写 `cases/` 与 `.ai/draft/`。
- **同名异义不误伤**：`str.replace` / `dict.copy` 不是写盘调用；
  `os.replace` / `shutil.copy` 是。
- **真代码基线**：bench/ 写盘点与 `BENCH_KNOWN_WRITES` 登记表**计数相等**
  （工作台新增写盘必须显式登记，否则红）；interfacetester_ai/ 生产包零违规
  （T6 建包后生效）。

## 元护栏（防"假绿"）

`test_scanner_actually_detects_injected_fault` 验证"扫描器真的在扫"——
把一个写盘调用注入干净源码，违规数必须从 0 变 1。没有它，"全绿"可能只是
扫描器压根没匹配到任何东西（护栏打偏，同 `silent_traps_test.py` 的先例）。
"""

import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from bench.write_boundary_scanner import (  # noqa: E402
    BENCH_KNOWN_WRITES,
    MODULE_WRITE_BOUNDARIES,
    SELFTEST_CLEAN,
    SELFTEST_VIOLATIONS,
    scan_source,
    scan_tree,
)


class TestRedline3PairwiseFixtures(unittest.TestCase):
    """违规必红 / 合规必绿——同一注册表规则的两个方向都被钉住。"""

    def test_clean_fixtures_yield_no_violation(self):
        for fname, source in SELFTEST_CLEAN.items():
            with self.subTest(fname=fname):
                self.assertEqual(
                    scan_source(source, fname), [],
                    f"合规样本被误报：{fname}",
                )

    def test_violation_fixtures_hit_expected_counts_and_reasons(self):
        for fname, (source, want, keyword) in SELFTEST_VIOLATIONS.items():
            with self.subTest(fname=fname):
                bad = scan_source(source, fname)
                self.assertEqual(len(bad), want)
                self.assertTrue(
                    any(keyword in v.reason for v in bad),
                    f"违规原因未命中类别「{keyword}」：{bad}",
                )

    def test_llm_readonly_open_is_not_a_write(self):
        src = 'def f(p):\n    with open(p, "rb") as fp:\n        return fp.read()\n'
        self.assertEqual(scan_source(src, "llm.py"), [])

    def test_llm_write_text_is_a_violation(self):
        src = 'from pathlib import Path\nPath("x.json").write_text("{}")\n'
        bad = scan_source(src, "llm.py")
        self.assertEqual(len(bad), 1)
        self.assertIn("一律不写盘", bad[0].reason)

    def test_cache_writing_cases_is_violation(self):
        src = 'def e(c, t):\n    with open("cases/" + c + ".yml", "w") as fp:\n        fp.write(t)\n'
        bad = scan_source(src, "cache.py")
        self.assertEqual(len(bad), 1)
        self.assertIn("不在允许根", bad[0].reason)

    def test_undecidable_target_is_violation_for_registered_module(self):
        src = 'def w(root):\n    with open(root, "w") as fp:\n        fp.write("")\n'
        bad = scan_source(src, "cache.py")
        self.assertEqual(len(bad), 1)
        self.assertIn("不可静态判定", bad[0].reason)

    def test_unregistered_module_writing_cases_is_violation(self):
        src = (
            "from pathlib import Path\n"
            "def export(name, text):\n"
            '    Path("cases/" + name + ".yml").write_text(text)\n'
        )
        bad = scan_source(src, "helper.py")
        self.assertEqual(len(bad), 1)
        self.assertIn("未登记模块", bad[0].reason)

    def test_assembler_may_write_cases_and_draft(self):
        src = (
            "from pathlib import Path\n"
            'Path("cases").mkdir(exist_ok=True)\n'
            'Path(".ai/draft").mkdir(parents=True, exist_ok=True)\n'
            'Path("cases/a.yml").write_text("")\n'
            'Path(f".ai/draft/a.yml").write_text("")\n'
        )
        self.assertEqual(scan_source(src, "assembler.py"), [])

    def test_root_matching_is_segment_aligned(self):
        # .ai 只放行 .ai/… 或恰好 .ai，不放行 .aix/…
        ok = 'import os\nos.makedirs(".ai/cache", exist_ok=True)\n'
        self.assertEqual(scan_source(ok, "cache.py"), [])
        bad = 'import os\nos.makedirs(".aix/cache", exist_ok=True)\n'
        self.assertEqual(len(scan_source(bad, "cache.py")), 1)


class TestSameNameDisambiguation(unittest.TestCase):
    """同名异义：str.replace / dict.copy 不是写盘；os.replace / shutil.copy 是。"""

    def test_str_replace_and_dict_copy_are_not_writes(self):
        src = (
            "def f(s, d):\n"
            '    t = s.replace("\\\\", "/")\n'
            "    return t, d.copy()\n"
        )
        self.assertEqual(scan_source(src, "helper.py"), [])

    def test_os_replace_and_shutil_copy_are_writes(self):
        src = (
            "import os, shutil\n"
            'os.replace("a.tmp", "b.tmp")\n'
            'shutil.copy("a", "b")\n'
        )
        bad = scan_source(src, "helper.py")
        self.assertEqual(len(bad), 2)


class TestRegistryMatchesDocumentation(unittest.TestCase):
    """注册表 ↔ §6「写盘边界」行对账（文档 ↔ 代码三方对账的一边）。"""

    def test_registry_covers_all_documented_writers(self):
        self.assertEqual(
            set(MODULE_WRITE_BOUNDARIES),
            {
                "llm",
                "cache",
                "manifest",
                "pending",
                "diagnosis",
                "doc_quality",
                "workdir",
                "assembler",
                "reviews",
                "analyze",
                "summary",
                "jobs",
                "export",
            },
            "§6 写盘边界：登记在册的写入者（cache/manifest/pending/workdir/reviews/jobs/export 只写 .ai/；"
            "diagnosis/doc_quality/analyze/summary 只写 reports/）+ llm 禁写 + 装配器（cases/ 与 .ai/draft/）"
            "；★web/confirm **刻意不在册**（源码里没有写调用；导出的写盘在 export.py，看板依旧零写盘）",
        )

    def test_llm_is_forbidden(self):
        self.assertEqual(MODULE_WRITE_BOUNDARIES["llm"][0], "forbid")

    def test_ai_root_writers_are_pinned(self):
        for mod in ("cache", "manifest", "pending", "workdir"):
            with self.subTest(module=mod):
                kind, roots = MODULE_WRITE_BOUNDARIES[mod]
                self.assertEqual((kind, roots), ("roots", (".ai/",)))

    def test_report_writers_are_pinned_to_reports(self):
        # T29 / T27：AI 层报告是一类登记写入者，根固定 reports/（扩白名单必须显式登记）
        for mod in ("diagnosis", "doc_quality"):
            with self.subTest(module=mod):
                self.assertEqual(
                    MODULE_WRITE_BOUNDARIES[mod],
                    ("roots", ("reports/",)),
                )

    def test_assembler_roots_are_cases_and_ai(self):
        self.assertEqual(
            MODULE_WRITE_BOUNDARIES["assembler"],
            ("roots", ("cases/", ".ai/")),
        )


class TestScannerActuallyDetectsInjectedFault(unittest.TestCase):
    """元护栏：把一个写盘调用注入干净源码，违规数必须从 0 变 1（防假绿）。"""

    def test_injected_fault_is_detected(self):
        clean = SELFTEST_CLEAN["transport.py"]
        self.assertEqual(scan_source(clean, "transport.py"), [])
        injected = clean + (
            "\ndef save(x):\n"
            '    with open("resp.json", "w") as fp:\n'
            "        fp.write(x)\n"
        )
        self.assertEqual(len(scan_source(injected, "transport.py")), 1)


class TestScanTreeOnDisk(unittest.TestCase):
    """scan_tree 对磁盘目录的端到端行为（含 __pycache__ 跳过）。"""

    def test_scan_tree_reports_helper_write_and_skips_pycache(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "helper.py"), "w", encoding="utf-8") as fp:
                fp.write(
                    "from pathlib import Path\n"
                    'Path("cases/x.yml").write_text("")\n'
                )
            os.makedirs(os.path.join(tmp, "__pycache__"))
            with open(
                os.path.join(tmp, "__pycache__", "junk.py"), "w", encoding="utf-8"
            ) as fp:
                fp.write('open("z", "w")\n')
            bad = scan_tree(tmp)
            self.assertEqual(len(bad), 1)
            self.assertEqual(bad[0].path, "helper.py")


class TestBenchBaseline(unittest.TestCase):
    """bench/ 真扫描 ↔ 登记表计数对账（工作台新增写盘必须显式登记）。"""

    def test_bench_write_points_match_registry_counts(self):
        bad = scan_tree(os.path.join(BASE, "bench"))
        got = {}
        for v in bad:
            key = (v.path, v.func)
            got[key] = got.get(key, 0) + 1
        self.assertEqual(got, dict(BENCH_KNOWN_WRITES))


class TestAiPackageBaseline(unittest.TestCase):
    """interfacetester_ai/ 生产包零违规（T6 建包后自动生效）。"""

    def test_ai_package_scans_clean(self):
        pkg = os.path.join(BASE, "interfacetester_ai")
        if not os.path.isdir(pkg):
            self.skipTest("interfacetester_ai/ 尚未建包（T6 待做）")
        self.assertEqual(scan_tree(pkg), [])


class TestSelftestEntry(unittest.TestCase):
    """自检脚本入口：重定向 + GBK locale 下退出码必须可靠（T26 纪律）。"""

    def test_selftest_script_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, os.path.join("bench", "write_boundary_scanner.py")],
            cwd=BASE,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(
            proc.returncode, 0,
            proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace"),
        )
        self.assertIn("红线③ 写盘边界扫描自检全部通过".encode("utf-8"), proc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

