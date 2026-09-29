# -*- coding: utf-8 -*-
"""interfacetester_ai 包骨架的形态自检（v8 §十 T6）。

## 为什么需要这个文件

T6 把 `bench/silent_traps.py` / `bench/normalize_projection.py` 整体迁入
`interfacetester_ai` 包（gates / projection），bench 路径保留**兼容壳**。
迁移最危险的失败形态是**双源漂移**：壳被改成"复制粘贴"而非"转发"，
于是 bench 与包各自演化，§3.5 刚建立的三方对账就在两份代码间失效。

所以本文件的判据是**同一性**（`is`），不是"两边都跑得通"：
  bench 壳导出的对象 == 包模块的对象（同一内存对象，零拷贝）。

## 判据清单（T6 验收：新包自检全绿 + 内核零改动 + 违规必红/合规必绿）

- 同一性：壳 ↔ 包（TestBenchShims）；
- 包入口：`python -m interfacetester_ai.gates/.projection` 退出码 0；
  壳 CLI `python bench/silent_traps.py` 同样 0（TestPackageEntryPoints）；
- 骨架齐：九模块 + pyproject，版本号两处对账（TestPackageLayout）；
- **内核零改动/零依赖**：内核源码不 import 本包（红线①，TestKernelIsolation）
  ——T6 验收的 `git diff` 空在本仓（非 git）以"内核源码无 AI 包引用 +
  迁移不动 interfacetester/ 目录"承担；
- 违规必红/合规必绿：闸门本体由 `tests/silent_traps_test.py`（MUST_REJECT/
  MUST_ALLOW 成对）承担；写盘边界由 `tests/write_boundary_test.py`
  （interfacetester_ai/ 零违规）承担——本文件不重复，只钉骨架形态。
"""

import os
import subprocess
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import bench.ai_guardrails  # noqa: E402
import bench.normalize_projection  # noqa: E402
import bench.silent_traps  # noqa: E402
import interfacetester_ai  # noqa: E402
import interfacetester_ai.gates  # noqa: E402
import interfacetester_ai.l3  # noqa: E402
import interfacetester_ai.prompts  # noqa: E402
import interfacetester_ai.projection  # noqa: E402


class TestBenchShims(unittest.TestCase):
    """同一性：bench 壳是转发不是复制（防双源漂移）。"""

    def test_shim_reexports_are_identical_objects(self):
        pairs = [
            (bench.silent_traps.check_testcase, interfacetester_ai.gates.check_testcase),
            (bench.silent_traps.assert_testcase_passes, interfacetester_ai.gates.assert_testcase_passes),
            (bench.silent_traps.gate_s1_pseudo_presence, interfacetester_ai.gates.gate_s1_pseudo_presence),
            (bench.silent_traps.gate_s11_document_encoding, interfacetester_ai.gates.gate_s11_document_encoding),
            (bench.silent_traps.GateReport, interfacetester_ai.gates.GateReport),
            (bench.silent_traps.SilentTrapRejected, interfacetester_ai.gates.SilentTrapRejected),
            (bench.silent_traps.MUST_REJECT, interfacetester_ai.gates.MUST_REJECT),
            (bench.silent_traps.MUST_ALLOW, interfacetester_ai.gates.MUST_ALLOW),
            (bench.silent_traps.run_selftest, interfacetester_ai.gates.run_selftest),
            (bench.normalize_projection.project_summary, interfacetester_ai.projection.project_summary),
            (bench.normalize_projection._sample_summary, interfacetester_ai.projection._sample_summary),
            # ★P0a（2026-09-25）：L3 闸门与 prompt 渲染按**同款处置**收口到包内
            #   —— 此前 `bench/ai_guardrails.py` 自己实现了一遍 L3，prompt 的
            #   `PROMPT_VERSION` 还两处不一致（bench "v2.0" / 包 "0"，而缓存键取包内那个）。
            (bench.ai_guardrails.check_l3_allowed, interfacetester_ai.l3.check_l3_allowed),
            (bench.ai_guardrails.ensure_l3_allowed, interfacetester_ai.l3.ensure_l3_allowed),
            (bench.ai_guardrails.L3Refused, interfacetester_ai.l3.L3Refused),
            (bench.ai_guardrails.render_system_prompt, interfacetester_ai.prompts.render_system_prompt),
            (
                bench.ai_guardrails.audit_prompt_against_kernel,
                interfacetester_ai.prompts.audit_prompt_against_kernel,
            ),
            (bench.ai_guardrails.get_comparator_aliases, interfacetester_ai.prompts.get_comparator_aliases),
            # ★§9.23：算子用法正例（渲染与判据同源）也按同款收口
            (
                bench.ai_guardrails.audit_operator_guidance,
                interfacetester_ai.prompts.audit_operator_guidance,
            ),
            (
                bench.ai_guardrails.operator_guidance_text,
                interfacetester_ai.prompts.operator_guidance_text,
            ),
        ]
        for shim_obj, pkg_obj in pairs:
            with self.subTest(shim=str(getattr(shim_obj, "__name__", shim_obj))):
                self.assertIs(shim_obj, pkg_obj)

    def test_prompt_version_has_exactly_one_source(self):
        """`PROMPT_VERSION` 只能有一个来源——它进**缓存键**，两处不一致等于缓存语义是错的。"""
        self.assertEqual(
            bench.ai_guardrails.PROMPT_VERSION,
            interfacetester_ai.prompts.PROMPT_VERSION,
            "bench 与包内的 prompt 版本号不一致（缓存键取的是包内那个）",
        )

    def test_gate_module_identity(self):
        # 闸门函数的 __module__ 已指向包（T20 的 inspect.getsource 依赖此形态）
        self.assertEqual(
            bench.silent_traps.gate_s3_assertion_vacuum.__module__,
            "interfacetester_ai.gates",
        )


class TestPackageEntryPoints(unittest.TestCase):
    """包入口自检：`python -m` 与壳 CLI 双形态退出码全 0。"""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, *args], cwd=BASE, capture_output=True, timeout=180
        )

    def test_gates_module_selftest_exits_zero(self):
        proc = self._run("-m", "interfacetester_ai.gates")
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        self.assertIn("11 条对抗样本全部命中".encode("utf-8"), proc.stdout)
        self.assertIn("17 条合法写法全部放行".encode("utf-8"), proc.stdout)

    def test_projection_module_selftest_exits_zero(self):
        proc = self._run("-m", "interfacetester_ai.projection")
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))

    def test_bench_shim_cli_still_works(self):
        proc = self._run(os.path.join("bench", "silent_traps.py"))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))

    def test_version_flag_answers_like_hrun(self):
        """★§9.35：`haify -V/--version` 与 `hrun --version` **同一口径**（两个别名都认）。

        现场：装好 `haify` 后第一件事往往是问版本（`hrun --version` 的习惯），而它此前只认
        `-h` → `unrecognized arguments: --version`（退出码 2）—— **看起来像"命令坏了"**，
        其实只是"同一条链路上的工具，版本问法不一致"。
        """
        for flag in ("--version", "-V"):
            with self.subTest(flag=flag):
                proc = self._run("-m", "interfacetester_ai", flag)
                text = proc.stdout.decode("utf-8", "replace")
                self.assertEqual(proc.returncode, 0, text)
                self.assertIn("interfacetester-ai", text)

    def test_version_text_also_names_the_kernel(self):
        """★AI 层读内核的**公开面** → 两者必须**配套**；版本文案要把两边都报出来。

        判据用的是**内核现装的那一版**（不是写死的字符串）——内核升版而文案没跟上就会红。
        """
        from interfacetester import __version__ as kernel_version  # noqa: PLC0415

        proc = self._run("-m", "interfacetester_ai", "--version")
        text = proc.stdout.decode("utf-8", "replace")

        self.assertIn(interfacetester_ai.__version__, text)
        self.assertIn(kernel_version, text)


class TestPackageLayout(unittest.TestCase):
    """骨架齐 + 版本两处对账（形态自检，T6 验收「新包内自检全绿」）。"""

    EXPECTED_MODULES = (
        "__init__",
        "gates",
        "projection",
        "normalize",
        "workdir",
        "schema",
        "citation",
        "slicer",
        "assembler",
        "assertions",
        "reviews",
        "evidence",
        "analyze",
        "summary",
        "fix",
        "debugtalk_draft",
        "web",
        "confirm",
        "jobs",
        "audit",
        "export",
        "gen",
        "cli",
        "l3",
        "pending",
        "quality",
        "diagnosis",
        "doc_quality",
        "llm",
        "cache",
        "manifest",
        "schema",
        "prompts",
        "evidence",
    )

    def test_skeleton_modules_are_pinned(self):
        for mod in self.EXPECTED_MODULES:
            with self.subTest(module=mod):
                path = os.path.join(BASE, "interfacetester_ai", mod + ".py")
                self.assertTrue(os.path.isfile(path), f"缺模块 {path}")

    def test_pyproject_declares_package(self):
        text = open(
            os.path.join(BASE, "interfacetester_ai", "pyproject.toml"),
            encoding="utf-8",
        ).read()
        self.assertIn('name = "interfacetester-ai"', text)
        self.assertIn('version = "0.1.0"', text)

    def test_pyproject_declares_the_haify_console_script(self):
        """★入口必须声明在**包自己的** `pyproject.toml` 里 —— 它悄悄消失的后果就是
        「**`haify` 命令用不了**」。

        现场（2026-09-27）：根 `pyproject.toml` 只声明 `hrun` / `hmake` / `hconvert`（内核），
        而 `haify` 属于 **AI 包**；用户 `pip install -e .`（只装内核）后敲 `haify` → 命令不存在，
        只有 `python -m interfacetester_ai` 能用。装法见 `deploy.md`（`pip install -e interfacetester_ai`）。
        """
        text = open(
            os.path.join(BASE, "interfacetester_ai", "pyproject.toml"), encoding="utf-8"
        ).read()

        self.assertIn("[project.scripts]", text)
        self.assertIn('haify = "interfacetester_ai.cli:main"', text)

    def test_version_is_consistent_between_init_and_pyproject(self):
        self.assertEqual(interfacetester_ai.__version__, "0.1.0")


class TestKernelIsolation(unittest.TestCase):
    """红线① 反向依赖：内核永不 import LLM 层（§2.2-①）。"""

    def test_kernel_sources_never_import_ai_package(self):
        kernel_dir = os.path.join(BASE, "interfacetester")
        hits = []
        for dirpath, dirnames, filenames in os.walk(kernel_dir):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fname)
                with open(full, encoding="utf-8") as fp:  # 只读
                    for i, line in enumerate(fp, 1):
                        if "interfacetester_ai" in line:
                            hits.append(f"{os.path.relpath(full, BASE)}:{i}")
        self.assertEqual(
            hits, [], "内核模块出现对 AI 旁路包的引用，违反只读白名单方向（红线①）"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

