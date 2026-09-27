# -*- coding: utf-8 -*-
"""L2 作业隔离的护栏用例 —— v8 §9.x **P-3** / 风险 **R5**（**T8**）。

## 为什么需要这个文件

`interfacetester_ai/workdir.py` 提供三件事：**作业临时目录**、**清理 + 残留上报**、
**前后工作树快照的零污染断言**。它们每一条都能"看起来在跑、其实没生效"，
所以本文件的判据全部是**行为级**的，而且**要真实文件系统**：

| P-3 的要求 | 本文件怎么测 |
| --- | --- |
| 在 `.ai/work/<job_id>/` 下跑 | 作业里断言 `work_dir` 落在 `.ai/work/` 下 |
| 跑完清理 | 作业结束后断言目录**不存在** |
| 清理失败必须报残留路径 | mock `rmtree` 变 no-op → 必须抛 `WorkspaceResidue` 且**带路径** |
| 前后工作树快照不变 | 作业故意往 `cases/` 写 → 必须抛 `WorkspaceContaminated` 且**点名路径** |
| 作业串行 | 作业期间 `_SERIAL_LOCK` 必须被持有，结束必须释放 |

`workdir.run_selftest()` 只做**纯逻辑**判据（不写盘）——因为生产模块里只该留
**它自己的**写盘点（T27 的教训：自检夹具写盘会被写盘扫描器判违规）。
"需要真实文件系统"的部分因此全部落在本文件。

★边界：所有沙箱都在 `tempfile.TemporaryDirectory()` 里，**不碰真仓库**；
唯一在真仓上跑的是最后一条——它只**读**（跑两个自检脚本，再比对源码树快照）。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import interfacetester_ai.workdir as workdir  # noqa: E402
from interfacetester_ai.workdir import (  # noqa: E402
    JobWorkspace,
    WorkspaceContaminated,
    WorkspaceResidue,
    diff_snapshots,
    run_isolated,
    snapshot_tree,
)


def _write(path, text):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class TestIsolationEndToEnd(unittest.TestCase):
    """真实文件系统上的端到端判据（沙箱里模拟一个迷你仓库）。"""

    def setUp(self):
        self._original_cwd = os.getcwd()
        self._sandbox = tempfile.TemporaryDirectory()
        self.sandbox = self._sandbox.name
        os.chdir(self.sandbox)
        _write(os.path.join(self.sandbox, "cases", "keep.yml"), "x: 1\n")
        _write(os.path.join(self.sandbox, "src", "app.py"), "print(1)\n")

    def tearDown(self):
        os.chdir(self._original_cwd)
        self._sandbox.cleanup()

    def test_job_writes_inside_work_dir_and_leaves_the_tree_untouched(self):
        def job(work_dir):
            _write(os.path.join(work_dir, "generated_test.py"), "x = 1\n")
            _write(os.path.join(work_dir, "schemas", "a.json"), "{}")
            return work_dir

        returned = run_isolated(job, root=self.sandbox, job_id="t-clean")

        expected = os.path.join(self.sandbox, ".ai", "work")
        self.assertTrue(os.path.abspath(returned).startswith(expected), returned)
        self.assertFalse(os.path.exists(returned), "作业结束后目录必须被清理")

    def test_contamination_is_detected_and_the_path_is_named(self):
        def polluting_job(work_dir):
            _write(os.path.join(self.sandbox, "cases", "leak.yml"), "leak\n")

        with self.assertRaises(WorkspaceContaminated) as ctx:
            run_isolated(polluting_job, root=self.sandbox, job_id="t-dirty")

        self.assertTrue(
            any("cases/leak.yml" in item for item in ctx.exception.leaks),
            ctx.exception.leaks,
        )
        self.assertIn("cases/leak.yml", str(ctx.exception))

    def test_residue_is_reported_when_cleanup_fails(self):
        space = JobWorkspace(job_id="t-residue")
        space.__enter__()
        try:
            with mock.patch.object(workdir.shutil, "rmtree", lambda *a, **k: None):
                with self.assertRaises(WorkspaceResidue) as ctx:
                    space.__exit__(None, None, None)
            self.assertTrue(ctx.exception.residues, "残留清单不能为空")
            self.assertTrue(
                any("t-residue" in item for item in ctx.exception.residues),
                ctx.exception.residues,
            )
        finally:
            workdir.shutil.rmtree(space.path, ignore_errors=True)

    def test_serial_lock_is_held_only_during_the_job(self):
        with JobWorkspace(job_id="t-lock") as space:
            self.assertTrue(workdir._SERIAL_LOCK.locked(), "作业期间必须持有串行锁")
            _write(os.path.join(space.path, "f.txt"), "1")

        self.assertFalse(workdir._SERIAL_LOCK.locked(), "作业结束后必须释放串行锁")

    def test_running_artifacts_do_not_trigger_a_false_alarm(self):
        """.ai/ 与 logs/ 本来就该变——把它们算成污染，会让这条断言很快被忽略。"""
        before = snapshot_tree(self.sandbox)

        run_isolated(lambda work_dir: None, root=self.sandbox, job_id="t-artifacts")
        _write(os.path.join(self.sandbox, ".ai", "cache", "k.json"), "{}")
        _write(os.path.join(self.sandbox, "logs", "run.log"), "log\n")

        self.assertEqual(diff_snapshots(before, snapshot_tree(self.sandbox)), [])

    def test_write_helper_creates_parent_directories(self):
        with JobWorkspace(job_id="t-write") as space:
            target = space.write("nested/deep/a_test.py", "y = 2\n")
            with open(target, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "y = 2\n")


class TestRealWorkspaceStaysClean(unittest.TestCase):
    """在**真仓**上跑护栏自检：源码树快照必须不变（P-3 的原话口径）。"""

    def test_guardrail_selftests_do_not_touch_the_source_tree(self):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        before = snapshot_tree(BASE)

        for module in ("interfacetester_ai.workdir", "interfacetester_ai.normalize"):
            with self.subTest(module=module):
                proc = subprocess.run(
                    [sys.executable, "-m", module],
                    cwd=BASE,
                    capture_output=True,
                    timeout=180,
                    env=env,
                )
                self.assertEqual(
                    proc.returncode, 0, proc.stdout.decode("utf-8", "replace")
                )

        self.assertEqual(
            diff_snapshots(before, snapshot_tree(BASE)), [], "护栏自检动了源码树"
        )


class TestCliExitCode(unittest.TestCase):
    """`python -m interfacetester_ai.workdir` 在重定向 + 无 `PYTHONIOENCODING` 下退出码 0。"""

    def test_module_selftest_exits_zero(self):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)

        proc = subprocess.run(
            [sys.executable, "-m", "interfacetester_ai.workdir"],
            cwd=BASE,
            capture_output=True,
            timeout=120,
            env=env,
        )

        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        self.assertIn("L2 作业隔离自检全部通过".encode("utf-8"), proc.stdout)


class TestMetaGuardrails(unittest.TestCase):
    """判据自检：**忽略范围**本身也要被钉住（否则"不假报"会变成"什么都看不见"）。"""

    def test_ignoring_everything_would_hide_contamination_but_reality_does_not(self):
        with tempfile.TemporaryDirectory() as sandbox:
            _write(os.path.join(sandbox, "cases", "leak.yml"), "leak\n")

            with mock.patch.object(workdir, "_should_ignore_dir", lambda name: True):
                self.assertEqual(
                    snapshot_tree(sandbox),
                    {},
                    "忽略规则吞掉一切时确实什么都看不见——这正是要防的方向",
                )

            self.assertIn(
                "cases/leak.yml",
                snapshot_tree(sandbox),
                "真实规则下 cases/ 必须可见（否则污染永远检不出来）",
            )
