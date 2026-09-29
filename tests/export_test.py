# -*- coding: utf-8 -*-
r"""离线静态导出（P3d 收口）：把「`file://` 真的点得到」变成机器判据。

## 为什么需要这个文件

**「页面自包含」只是必要条件**。真机（Chrome）实测的两面：

    # 只看单页（不点任何链接）——通过
    file:///…/index.html → 200，charset/内联 CSS/表格/数字都在
    # 但导航是**绝对路径**，file:// 下它指到文件系统根
    点「度量」→ chrome-error://chromewebdata/  标题 file:///D:/metrics
                body：无法访问您的文件 / ERR_FILE_NOT_FOUND

所以本节判据问的不是"文件在不在"，而是**"从入口页一路点下去，能不能到"**：
① 零绝对路径；② 每条链接的目标在产物里；③ 入口可达全部页面；④ 幂等；
⑤ 只写 `.ai/export/`（裁决权在 T18：本模块**已登记**，而看板**依然不在册**）。
真机四元组（真浏览器 + `file://`）见方案 v8 §12.8-⑧。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai import export as ex  # noqa: E402
from interfacetester_ai.export import (  # noqa: E402
    INDEX_NAME,
    OFFLINE_NAME,
    OfflineBundleNotSafe,
    assert_offline_safe,
    build_bundle,
    export_site,
    link_values,
)

TICK = "2026-09-25T00:00:00"

SAMPLE = {
    "success": False,
    "stat": {
        "testcases": {"total": 1, "success": 0, "fail": 1},
        "teststeps": {"total": 1, "success": 0, "fail": 1},
    },
    "details": [
        {
            "name": "演示用例",
            "records": [
                {
                    "name": "步骤一",
                    "step_type": "request",
                    "success": False,
                    "data": {
                        "address": "http://127.0.0.1/demo",
                        "validators": {
                            "validate_extractor": [
                                {
                                    "check": "status_code",
                                    "check_result": "fail",
                                    "check_value": 500,
                                    "comparator": "equal",
                                    "expect": 200,
                                    "message": "状态码不符",
                                }
                            ]
                        },
                    },
                }
            ],
        }
    ],
}


def _snapshot():
    """工作区快照（**不排除任何目录**——本节的判据正是"谁被写出来了"）。"""
    found = {}
    for folder, dirs, names in os.walk("."):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in names:
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, ".").replace(os.sep, "/")
            found[rel] = os.path.getsize(path)
    return found


class _SandboxCase(unittest.TestCase):
    """在临时工作区里跑（夹具写在自己的登记根内：`.ai/export/_selftest/logs/`）。

    ★为什么夹具不写 `logs/`：产线模块里"根外的写盘"会被 T18 判违规；测试里虽然可以写，
    但**用同一形态**更接近真实使用方式——`--root` 追加读根本来就是支持的路径。
    """

    ROOTS = (os.path.join(ex.EXPORT_ROOT, "_selftest"),)

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_export_test_")
        os.chdir(self._tmp.name)
        os.makedirs(".ai/export/_selftest/logs", exist_ok=True)
        with open(
            ".ai/export/_selftest/logs/demo.summary.json", "w", encoding="utf-8"
        ) as fp:
            json.dump(SAMPLE, fp, ensure_ascii=False, indent=2)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _read(self, name):
        with open(os.path.join(ex.EXPORT_ROOT, name), encoding="utf-8") as fp:
            return fp.read()


GOOD_DRAFT = (
    "config:\n  name: good\nteststeps:\n- name: good\n  request:\n"
    "    method: POST\n    url: /x\n  validate:\n  - type_match:\n    - body.x\n    - str\n"
)


class TestPendingSnapshotShowsQuality(_SandboxCase):
    """★C（2026-09-26）：**离线快照**的待确认页也要带质量状态。

    离线交付物是"拿在手上看"的那一份——它上面看不到质量状态，等于交付物里缺了评审 §五
    要求"并列展示"的那一栏。
    """

    def test_pending_html_carries_the_quality_section(self):
        os.makedirs(".ai/draft", exist_ok=True)
        with open(".ai/draft/good.yml", "w", encoding="utf-8") as fp:
            fp.write(GOOD_DRAFT)
        with open(".ai/draft/good.unknowns.json", "w", encoding="utf-8") as fp:
            json.dump(
                {"case": "good", "count": 1, "items": [{"kind": "protocol-only"}]},
                fp,
                ensure_ascii=False,
            )

        files, problems = build_bundle(self.ROOTS, stamp=TICK)

        # ★`problems` 里**本来**就会列"被改写成 offline.html 的路由"（那是离线化的正常产物）；
        #   这里要确认的是**新增的质量栏没有引入新的离线问题**。
        self.assertEqual(
            [item for item in problems if "pending" in item or "draft" in item],
            [],
            f"新增的质量栏引入了离线问题：{problems}",
        )
        page = files[ex.PENDING_NAME]
        self.assertIn("草稿与质量状态", page)
        self.assertIn(".ai/draft/good.yml", page)
        self.assertIn("needs_review", page)
        self.assertIn("protocol-only", page)


class TestBundleIsOfflineSafe(_SandboxCase):
    """① 零绝对路径 ② 目标存在 ③ 入口可达 ④ 幂等 ⑤ 判据不过不写盘。"""

    def test_no_absolute_links_in_bundle(self):
        files, _ = build_bundle(self.ROOTS, stamp=TICK)

        for name, text in sorted(files.items()):
            with self.subTest(page=name):
                self.assertNotIn('href="/', text, "站内绝对路径在 file:// 下必失效")
                self.assertNotIn('action="/', text)

    def test_every_link_target_exists_in_the_bundle(self):
        """产物内没有服务端兜底：链接指向不存在的文件 = 点下去 `ERR_FILE_NOT_FOUND`。"""
        files, _ = build_bundle(self.ROOTS, stamp=TICK)
        missing = []

        for name, text in sorted(files.items()):
            for value in link_values(text):
                target = value.split("#")[0].split("?")[0]
                if target and target not in files:
                    missing.append(f"{name} → {value}")

        self.assertEqual(missing, [], f"悬空链接：{missing}")

    def test_all_pages_reachable_from_index(self):
        """★"文件都在"≠"点得到"：从入口页 BFS 必须覆盖全部页面。"""
        files, _ = build_bundle(self.ROOTS, stamp=TICK)
        seen = {INDEX_NAME}
        queue = [INDEX_NAME]

        while queue:
            current = queue.pop()
            for value in link_values(files[current]):
                target = value.split("#")[0].split("?")[0]
                if target in files and target not in seen:
                    seen.add(target)
                    queue.append(target)

        self.assertEqual(sorted(set(files) - seen), [], "有页面从入口页走不到")

    def test_export_is_idempotent(self):
        """同一 stamp 两次导出必须逐字节相同（否则产物不可 diff，也就不可入库）。"""
        first = export_site(self.ROOTS, stamp=TICK)
        before = {name: self._read(name) for name in first["files"]}

        export_site(self.ROOTS, stamp=TICK)

        for name, text in before.items():
            with self.subTest(page=name):
                self.assertEqual(self._read(name), text)

    def test_judgment_failure_refuses_to_write(self):
        """★前置闸门：判据不过 → **一个字节都不落**（半份产物看起来是完整的）。"""
        broken = {INDEX_NAME: '<a href="/metrics">x</a>'}
        with mock.patch.object(ex, "build_bundle", return_value=(broken, [])):
            with self.assertRaises(OfflineBundleNotSafe):
                export_site(self.ROOTS, stamp=TICK)

        self.assertFalse(os.path.exists(ex.EXPORT_ROOT + "/" + INDEX_NAME))


class TestWhatGetsExported(_SandboxCase):
    """导出内容：五个固定页 + 运行明细 + fix 页 + 离线说明页。"""

    def test_expected_pages_are_exported(self):
        result = export_site(self.ROOTS, stamp=TICK)
        names = set(result["files"])

        for required in (
            INDEX_NAME,
            "metrics.html",
            "audit.html",
            "pending.html",
            "jobs.html",
            OFFLINE_NAME,
        ):
            with self.subTest(page=required):
                self.assertIn(required, names)
        self.assertTrue([n for n in names if n.startswith("run-")], "没有导出运行明细页")
        self.assertTrue([n for n in names if n.startswith("fix-")], "没有导出 fix 页")

    def test_offline_page_explains_what_needs_a_server(self):
        """`offline.html` 必须**点名**哪些能力要服务端（而不是让人自己猜）。"""
        export_site(self.ROOTS, stamp=TICK)
        page = self._read(OFFLINE_NAME)

        for route in ("/confirm", "/file"):
            with self.subTest(route=route):
                self.assertIn(route, page)
        self.assertIn("haify serve", page)

    def test_snapshot_banner_says_it_is_a_snapshot(self):
        """快照与活看板长得几乎一样：**必须**在页面里说清"这是快照、不会更新"。"""
        export_site(self.ROOTS, stamp=TICK)
        page = self._read(INDEX_NAME)

        self.assertIn("离线静态导出", page)
        self.assertIn(TICK, page)


class TestWriteBoundaryMatchesTheDashboard(_SandboxCase):
    """只写 `.ai/export/`；并且**导出与看板的边界是刻意不同的**。"""

    def test_only_the_export_root_is_touched(self):
        before = _snapshot()
        export_site(self.ROOTS, stamp=TICK)
        after = _snapshot()

        new_paths = sorted(set(after) - set(before))
        self.assertTrue(new_paths, "什么都没写出来——那条判据会变成恒真的空护栏")
        outside = [p for p in new_paths if not p.startswith(ex.EXPORT_ROOT + "/")]
        self.assertEqual(outside, [], f"导出动了登记根之外的东西：{outside}")

    def test_export_is_a_registered_writer_but_web_is_not(self):
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        self.assertEqual(MODULE_WRITE_BOUNDARIES["export"], ("roots", (".ai/",)))
        self.assertNotIn("web", MODULE_WRITE_BOUNDARIES, "看板必须**始终**零写盘")


class TestInjectedFaultIsCaught(_SandboxCase):
    """元护栏：注入绝对链接与悬空链接，判据**必须**同时报出来（防假绿）。"""

    def test_injected_problems_are_reported(self):
        files, _ = build_bundle(self.ROOTS, stamp=TICK)
        self.assertEqual(assert_offline_safe(files), [], "干净产物不该有问题")

        broken = dict(files)
        broken["broken.html"] = '<a href="/metrics">x</a><a href="nope.html">y</a>'
        problems = assert_offline_safe(broken)

        self.assertGreaterEqual(len(problems), 2, f"注入的问题没被报出来：{problems}")
        self.assertTrue(any("绝对路径" in item for item in problems))
        self.assertTrue(any("不存在" in item for item in problems))


class TestCliExport(_SandboxCase):
    """`python -m interfacetester_ai export` 一键导出（退出码 0 + 产物落位）。"""

    def test_cli_exports_and_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, "-m", "interfacetester_ai", "export"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=os.getcwd(),
            env={**os.environ, "PYTHONPATH": BASE, "PYTHONIOENCODING": "utf-8"},
            timeout=180,
        )

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(".ai/export/", proc.stdout)
        self.assertTrue(
            os.path.isfile(os.path.join(ex.EXPORT_ROOT, INDEX_NAME)),
            "CLI 说导出了，但入口页不在",
        )


if __name__ == "__main__":
    unittest.main()