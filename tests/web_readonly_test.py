# -*- coding: utf-8 -*-
r"""只读看板的护栏用例 —— v8 §2.5 / §5.8 行 1001（**P3a**）。

## §5.8 行 1001 的四条验收，本文件逐条钉住

| 验收 | 用例 |
| --- | --- |
| **零写盘** | `TestZeroWrite::test_nothing_on_disk_changes_after_serving_every_page` |
| **零执行** | `TestZeroExecute::*` |
| **目录穿越用例全被拒** | `TestPathSafety::*` + `TestPrefixConfusion::*` |
| **只监听回环** | `TestHostBinding::*` |

## 另外两条来自别处、同样硬的

- **R8**（行 1114）：Web 天然把失败柔化成 UI 提示。**失败不得用「警告」色渲染**——
  机器判据是渲染产物里 `warning`/`warn`/`orange`/`yellow` **零出现**
  （`TestR8FailureIsNotSoftened`）。
- **§5.5 那个指针**（v4 在这里错过一次）：
  `details[i].records[j].data.validators.validate_extractor[k]`。
  `TestPointer` 里有一条**元护栏**：故意把指针漏一层 `.data.`，判据必须报「取不到」，
  **不能**静默成「没有断言」——因为两者在页面上长得一样，而含义相反。
"""

import json
import os
import sys
import tempfile
import threading
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.web import (  # noqa: E402
    ALLOWED_ROOTS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    FORBIDDEN_STYLE_WORDS,
    LOOPBACK_HOSTS,
    NO_SOURCE,
    PRICE_COMPLETION_ENV,
    PRICE_PROMPT_ENV,
    STATE_ERROR,
    STATE_PASS,
    TRAVERSAL_SAMPLES,
    PathRefused,
    check_host,
    diff_all,
    extract_assertions,
    failure_trend,
    fetch,
    fetch_status_headers,
    iter_runs,
    load_summary,
    make_server,
    metrics_payload,
    read_cache_hit_rate,
    read_cost_estimate,
    read_kept_ratio,
    read_manifest_stats,
    read_token_usage,
    render_error_page,
    render_index,
    render_metrics,
    render_run,
    resolve_read_path,
    run_selftest,
    safe_rel_path,
    snapshot_all,
    state_span,
)

# 夹具：★字段路径**逐字照实测的 `summary.json`**（含 `.data.validators.validate_extractor`）
FIXTURE = {
    "success": False,
    "stat": {
        "testcases": {"fail": 1, "success": 1, "total": 2},
        "teststeps": {"failures": 1, "successes": 1, "total": 2},
    },
    "time": {"duration": 0.5, "start_at": 1.0},
    "platform": {"interfacetester_version": "5.0.1", "platform": "t", "python_version": "3"},
    "details": [
        {
            "name": "用例A",
            "success": True,
            "case_id": "c1",
            "records": [
                {
                    "name": "步骤1",
                    "step_type": "request",
                    "success": True,
                    "data": {
                        "address": "http://127.0.0.1:80/get",
                        "validators": {
                            "validate_extractor": [
                                {
                                    "check": "status_code",
                                    "check_result": "pass",
                                    "check_value": 200,
                                    "comparator": "equal",
                                    "expect": 200,
                                    "message": "",
                                }
                            ]
                        },
                    },
                }
            ],
        },
        {
            "name": "用例B",
            "success": False,
            "case_id": "c2",
            "records": [
                {
                    "name": "步骤2",
                    "step_type": "request",
                    "success": False,
                    "data": {
                        "address": "http://127.0.0.1:80/get",
                        "validators": {
                            "validate_extractor": [
                                {
                                    "check": "body.user",
                                    "check_result": "fail",
                                    "check_value": "alice",
                                    "comparator": "equal",
                                    "expect": "bob",
                                    "message": "值不相等",
                                }
                            ]
                        },
                    },
                }
            ],
        },
    ],
}


class _SandboxCase(unittest.TestCase):
    """在**临时工作区**里跑（不碰本仓产物；`.ai/`、`logs/`、`reports/` 全是自造的）。"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_web_test_")
        os.chdir(self._tmp.name)
        for rel in ("logs", "reports", ".ai/pending", ".ai/reviews", ".ai/draft", "logs_evil"):
            os.makedirs(rel, exist_ok=True)
        with open("logs/sample.summary.json", "w", encoding="utf-8") as fp:
            json.dump(FIXTURE, fp, ensure_ascii=False, indent=2)
        with open("reports/analysis.md", "w", encoding="utf-8") as fp:
            fp.write("# 分析\n\n- **未降级占比：86%**（§5.3 的验收线是 ≥70%）\n")
        with open(".ai/pending/demo.pending.json", "w", encoding="utf-8") as fp:
            json.dump({"case": "demo", "count": 1, "items": []}, fp, ensure_ascii=False)
        with open("logs_evil/secret.txt", "w", encoding="utf-8") as fp:
            fp.write("不该被读到\n")

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class _ServerCase(_SandboxCase):
    """在临时工作区里起一个**回环临时端口**上的看板（测完即关）。"""

    def setUp(self):
        super().setUp()
        self.server = make_server(DEFAULT_HOST, 0, ALLOWED_ROOTS)  # 端口 0 = 系统分配
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        super().tearDown()

    def get(self, path, method="GET"):
        return fetch(self.port, path, method=method)


GOOD_DRAFT = (
    "config:\n  name: good\nteststeps:\n- name: good\n  request:\n"
    "    method: POST\n    url: /x\n  validate:\n  - type_match:\n    - body.x\n    - str\n"
)
TRAP_DRAFT = (
    "config:\n  name: trap\nteststeps:\n- name: trap\n  request:\n"
    "    method: POST\n    url: /x\n  validate:\n"
    '  - not_equal:\n    - body.x\n    - ""\n'
)


class TestPendingPageShowsQuality(_ServerCase):
    """★C（2026-09-26）：待确认页必须显示草稿的**质量状态与 blocker**。

    评审 §五 要求"报告**并列展示**"；而看板与离线导出正是**交付物**——
    只在 CLI 打印等于交付物上看不到。判据同时钉住"闸门拒的草稿不许显示成通过"。
    """

    def _write_draft(self, name, text):
        os.makedirs(".ai/draft", exist_ok=True)
        path = ".ai/draft/" + name + ".yml"
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(text)
        return path

    def _write_unknowns(self, name, kind):
        os.makedirs(".ai/draft", exist_ok=True)
        with open(".ai/draft/" + name + ".unknowns.json", "w", encoding="utf-8") as fp:
            json.dump({"case": name, "count": 1, "items": [{"kind": kind}]}, fp, ensure_ascii=False)

    def test_page_lists_drafts_with_quality_and_blockers(self):
        self._write_draft("good", GOOD_DRAFT)
        self._write_unknowns("good", "protocol-only")

        status, raw = self.get("/pending")
        body = raw.decode("utf-8", "replace")

        self.assertEqual(status, 200)
        self.assertIn("草稿与质量状态", body, "页面上没有质量状态这一栏")
        self.assertIn(".ai/draft/good.yml", body)
        self.assertIn("needs_review", body)
        self.assertIn("protocol-only", body)

    def test_rejected_draft_is_not_shown_as_passing(self):
        """★"闸门拒掉的草稿被显示成通过"是本轮端到端实测暴露过的缺陷，这里在看板侧再钉一次。"""
        self._write_draft("trap", TRAP_DRAFT)

        status, raw = self.get("/pending")

        self.assertEqual(status, 200)
        self.assertIn("rejected", raw.decode("utf-8", "replace"))

    def test_page_renders_the_shared_overview_verbatim(self):
        """★口径只有一个来源：页面显示的就是 `reviews.drafts_overview()` 给的那份（不各自复算）。"""
        from interfacetester_ai import reviews as reviews_mod  # noqa: PLC0415
        from interfacetester_ai.web import render_draft_quality  # noqa: PLC0415

        self._write_draft("good", GOOD_DRAFT)
        self._write_unknowns("good", "protocol-only")

        rows = reviews_mod.drafts_overview()
        body = render_draft_quality(rows)

        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["quality_render"], body)
        self.assertIn(rows[0]["path"], body)

    def test_empty_draft_dir_says_so_and_does_not_raise(self):
        from interfacetester_ai.web import render_draft_quality  # noqa: PLC0415

        body = render_draft_quality([])

        self.assertIn("没有草稿", body)


class TestPathSafety(unittest.TestCase):
    """③ 目录穿越**全被拒**（函数级；HTTP 级见 `TestHttpSurface`）。"""

    def test_traversal_samples_are_refused(self):
        for sample in TRAVERSAL_SAMPLES:
            with self.subTest(sample=sample):
                with self.assertRaises(PathRefused):
                    resolve_read_path(sample, ALLOWED_ROOTS)

    def test_absolute_paths_are_refused(self):
        for sample in ("/etc/passwd", "C:/Windows/win.ini", "D:\\secret.txt"):
            with self.subTest(sample=sample):
                with self.assertRaises(PathRefused):
                    safe_rel_path(".", sample)

    def test_empty_path_is_refused(self):
        with self.assertRaises(PathRefused):
            safe_rel_path(".", "")

    def test_refusal_is_loud(self):
        """★拒绝必须**响亮**：异常里要带上被拒的路径，不能只说一句"不合法"。"""
        with self.assertRaises(PathRefused) as ctx:
            safe_rel_path(".", "../interfacetester/parser.py")

        self.assertIn("..", str(ctx.exception))

    def test_encoded_traversal_is_refused(self):
        """`%2e%2e%2f` 这类编码穿越：解码一次后仍必须被拒。"""
        with self.assertRaises(PathRefused):
            resolve_read_path("..%2finterfacetester%2fparser.py", ALLOWED_ROOTS)


class TestPrefixConfusion(_SandboxCase):
    """★`logs_evil/` 不是 `logs/`：前缀比较必须用 `commonpath`，**不是** `startswith`。

    这条不是假想：本仓根目录下同时有 `logs/`，而"按名字拼路径"时
    `startswith("logs")` 会**放行** `logs_evil/`——白名单就变成"同前缀随便读"。
    """

    def test_sibling_with_same_prefix_is_refused(self):
        with self.assertRaises(PathRefused) as ctx:
            resolve_read_path("logs_evil/secret.txt", ALLOWED_ROOTS)

        self.assertIn("读根", str(ctx.exception))

    def test_file_really_exists_so_the_refusal_is_about_the_rule(self):
        """★关键：诱饵文件**真的存在**——否则"被拒"可能只是因为文件没生成。"""
        self.assertTrue(os.path.isfile("logs_evil/secret.txt"))

    def test_startswith_would_have_said_yes(self):
        """元护栏：证明两种判据在本机上**结论不同**（否则这条用例没意义）。"""
        root = os.path.realpath(os.path.join(os.getcwd(), "logs"))
        target = os.path.realpath(os.path.join(os.getcwd(), "logs_evil", "secret.txt"))

        self.assertTrue(target.startswith(root), "本机路径形态变了：startswith 不再误判")
        self.assertNotEqual(os.path.commonpath([root, target]), root)


class TestResolveReadPath(_SandboxCase):
    """读根解析：**路径不许被拼两遍**（本轮自检踩到的真 bug，必须有回归）。"""

    def test_summary_is_found(self):
        payload, root, why = load_summary("logs/sample.summary.json", ALLOWED_ROOTS)

        self.assertEqual(why, "", "读不到摘要——很可能把读根拼了两遍（logs/logs/…）")
        self.assertTrue(payload)
        self.assertEqual(root, "logs")

    def test_kept_ratio_is_found(self):
        """★度量页的那个数字要真读得到（否则首页显示"未读到"，功能等于没有）。"""
        line = read_kept_ratio()

        self.assertEqual(line.value, "86%")
        self.assertIn("analysis.md", line.source)

    def test_iter_runs_finds_the_fixture(self):
        runs = iter_runs(ALLOWED_ROOTS)

        self.assertEqual([item.rel_path for item in runs], ["logs/sample.summary.json"])
        self.assertFalse(runs[0].success)
        self.assertEqual((runs[0].case_fail, runs[0].case_total), (1, 2))
        self.assertEqual((runs[0].step_fail, runs[0].step_total), (1, 2))

    def test_each_allowed_root_is_accepted(self):
        for root in ALLOWED_ROOTS:
            with self.subTest(root=root):
                resolve_read_path(root, ALLOWED_ROOTS)

    def test_path_outside_all_roots_is_refused(self):
        with self.assertRaises(PathRefused):
            resolve_read_path("interfacetester/parser.py", ALLOWED_ROOTS)

    def test_extra_read_root_extends_the_whitelist(self):
        """`--root` 的形态：追加的读根也**逐条**过校验（不是放宽成通配）。"""
        os.makedirs("project-two/logs", exist_ok=True)
        with open("project-two/logs/x.summary.json", "w", encoding="utf-8") as fp:
            json.dump(FIXTURE, fp, ensure_ascii=False)

        roots = tuple(ALLOWED_ROOTS) + ("project-two/logs",)
        payload, _root, why = load_summary("project-two/logs/x.summary.json", roots)

        self.assertEqual(why, "")
        self.assertTrue(payload)
        # 不追加就读不到（白名单没放宽）
        with self.assertRaises(PathRefused):
            resolve_read_path("project-two/logs/x.summary.json", ALLOWED_ROOTS)


class TestPointer(_SandboxCase):
    """★§5.5 的指针：`details[i].records[j].data.validators.validate_extractor[k]`。"""

    def test_assertions_are_extracted(self):
        summary, _root, why = load_summary("logs/sample.summary.json", ALLOWED_ROOTS)
        self.assertEqual(why, "")

        rows, problems = extract_assertions(summary)

        self.assertEqual(problems, [], "夹具是合规结构，不该有取不到的原因")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.check for row in rows}, {"status_code", "body.user"})

    def test_failing_assertion_is_visible(self):
        summary, _root, _why = load_summary("logs/sample.summary.json", ALLOWED_ROOTS)
        rows, _problems = extract_assertions(summary)

        failed = [row for row in rows if not row.passed]

        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].check_value, "alice")
        self.assertEqual(failed[0].expect, "bob")
        self.assertEqual(failed[0].message, "值不相等")

    def test_passed_is_strict_not_loose(self):
        """★`passed` 只在 `check_result == "pass"` 时为真。

        写成 `!= "fail"` 的话，`None`／拼错的／新版本加的状态**全都会算通过**——
        那正是"假通过"的标准长相。
        """
        summary = {
            "details": [
                {
                    "name": "x",
                    "success": False,
                    "records": [
                        {
                            "name": "s",
                            "step_type": "request",
                            "success": False,
                            "data": {
                                "validators": {
                                    "validate_extractor": [
                                        {"check": "a", "check_result": None},
                                        {"check": "b", "check_result": "unknown"},
                                    ]
                                }
                            },
                        }
                    ],
                }
            ]
        }
        rows, _problems = extract_assertions(summary)

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not row.passed for row in rows))

    def test_missing_data_layer_says_which_layer(self):
        """★元护栏：**故意**漏一层 `.data.` → 必须报「取不到」并说清缺在哪一层。

        v4 的错就是这个，而且它**看起来像"这份用例没有断言"**——两者含义相反。
        """
        without_data = {
            "details": [
                {
                    "name": "x",
                    "success": False,
                    "records": [
                        {
                            "name": "s",
                            "step_type": "request",
                            "success": False,
                            "validators": {"validate_extractor": []},
                        }
                    ],
                }
            ]
        }

        rows, problems = extract_assertions(without_data)

        self.assertEqual(rows, [], "漏一层后不该还能取到断言")
        self.assertTrue(problems)
        self.assertIn("data.validators", problems[0])

    def test_plain_no_assertions_is_not_a_problem(self):
        """**真正的**"没有断言"必须与"取不到"分开：它不该出现在 problems 里。"""
        empty = {
            "details": [
                {
                    "name": "x",
                    "success": True,
                    "records": [
                        {
                            "name": "s",
                            "step_type": "request",
                            "success": True,
                            "data": {"validators": {"validate_extractor": []}},
                        }
                    ],
                }
            ]
        }

        rows, problems = extract_assertions(empty)

        self.assertEqual(rows, [])
        self.assertEqual(problems, [])


class TestHostBinding(unittest.TestCase):
    """④ 只监听回环（§5.8 行 1001 验收第 4 条）。"""

    def test_default_is_loopback(self):
        self.assertEqual(DEFAULT_HOST, "127.0.0.1")
        self.assertIn(DEFAULT_HOST, LOOPBACK_HOSTS)

    def test_loopback_forms_are_loopback(self):
        for host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            with self.subTest(host=host):
                self.assertTrue(check_host(host)[0])

    def test_external_hosts_are_flagged_with_a_reason(self):
        """对外监听**不是禁止**，但必须显式，而且**必须把理由说出来**。"""
        for host in ("0.0.0.0", "192.168.1.10", "example.com"):
            with self.subTest(host=host):
                is_loop, why = check_host(host)

                self.assertFalse(is_loop)
                self.assertIn("反向代理", why)

    def test_bad_read_root_is_refused_at_startup(self):
        """★违规读根**启动即拒**（不是等某个请求恰好命中它才暴露）。"""
        for bad in ("../outside", "/etc", "a/../../b"):
            with self.subTest(bad=bad):
                with self.assertRaises(PathRefused):
                    make_server(DEFAULT_HOST, 0, (bad,))


class TestR8FailureIsNotSoftened(_SandboxCase):
    """★R8（行 1114）：失败**不得**用"警告/提示"色渲染。

    机器判据不是"看起来够不够红"，而是：渲染产物里
    `warning`/`warn`/`orange`/`yellow` **一个都不许出现**，且失败态只能走 `state-error`。
    """

    def _pages(self):
        summary, _root, _why = load_summary("logs/sample.summary.json", ALLOWED_ROOTS)
        rows, problems = extract_assertions(summary)
        return {
            "首页": render_index(iter_runs(ALLOWED_ROOTS), metrics_payload(ALLOWED_ROOTS)),
            "明细页": render_run("logs/sample.summary.json", summary, rows, problems),
            "度量页": render_metrics(
                metrics_payload(ALLOWED_ROOTS), failure_trend(ALLOWED_ROOTS)
            ),
            "错误页": render_error_page(405, "本服务不接受写", "x"),
        }

    def test_no_forbidden_style_words(self):
        for label, text in self._pages().items():
            with self.subTest(page=label):
                hit = [word for word in FORBIDDEN_STYLE_WORDS if word in text.lower()]

                self.assertEqual(hit, [], f"{label}里出现了禁用词 {hit}")

    def test_failure_and_pass_both_have_their_state(self):
        run_html = self._pages()["明细页"]

        self.assertIn('class="state-error"', run_html)
        self.assertIn('class="state-pass"', run_html)

    def test_state_span_has_one_entry(self):
        """★"失败用什么颜色"只有**一个**入口，所以不可能两处不一致。"""
        self.assertIn('class="state-pass"', state_span(True))
        self.assertIn('class="state-error"', state_span(False))
        self.assertIn(STATE_PASS, state_span(True))
        self.assertIn(STATE_ERROR, state_span(False))

    def test_failure_says_failure_not_something_softer(self):
        run_html = self._pages()["明细页"]

        self.assertIn("success = false", run_html)
        for softer in ("未能通过", "轻度", "请留意", "轻微"):
            self.assertNotIn(softer, run_html)

    def test_success_page_shows_success_verbatim(self):
        good = dict(FIXTURE)
        good["success"] = True

        html = render_run("x", good, [], [])

        self.assertIn("success = true", html)

    def test_pages_load_no_external_resources(self):
        """离线可用（§5.8 行 1004）：页面**不许**引**外部**资源。

        ★**内联** `<script>` 是允许的——§5.8 行 1004 明确要求"静态 HTML + **原生 JS**"，
        要禁的是 CDN / 字体 / 框架这些**外部**依赖（`<script src=…>` / `<link>`）。
        """
        for label, text in self._pages().items():
            with self.subTest(page=label):
                for bad in ("cdn", "<script src", "<link", "fonts.googleapis", "http://127.0.0.1/"):
                    self.assertNotIn(bad, text.lower())


class TestMetricsSourcing(_SandboxCase):
    """★"每个数字都要能说清来源"：没有来源的项显式写「未读到」，**不编 0**。"""

    def test_every_line_has_a_source(self):
        payload = metrics_payload(ALLOWED_ROOTS)

        self.assertTrue(payload["lines"])
        for line in payload["lines"]:
            with self.subTest(label=line["label"]):
                self.assertTrue(
                    line["source"], "度量行少了来源——来源不明的数字会被当成事实引用"
                )

    def test_kept_ratio_points_at_a_line(self):
        line = read_kept_ratio()

        self.assertEqual(line.value, "86%")
        self.assertRegex(line.source, r"analysis\.md:\d+")

    def test_missing_report_is_not_zero(self):
        """★报告不存在 → 「未读到」，**不是 0%**。

        `0%` 会被读成"所有结论都被降级了"——一个刺眼的**假**信号；
        而真相很可能只是"还没跑过 `haify analyze`"。两者处置完全不同。
        """
        os.remove("reports/analysis.md")

        line = read_kept_ratio()

        self.assertEqual(line.value, "未读到")
        self.assertNotIn("%", line.value)

    def test_unbacked_metrics_say_no_source(self):
        """**没有留痕时**显式标注，不编数字（沙箱里 `.ai/manifest/` 根本不存在）。"""
        by_label = {line["label"]: line for line in metrics_payload(ALLOWED_ROOTS)["lines"]}

        for label in ("缓存命中率", "token 用量（留痕合计）", "花费估算"):
            with self.subTest(label=label):
                self.assertEqual(by_label[label]["value"], "未读到")
                self.assertEqual(by_label[label]["source"], NO_SOURCE)
                self.assertIn(
                    ".ai/manifest",
                    by_label[label]["note"],
                    "「未读到」必须说清**去看哪儿**（否则用户不知道该跑什么）",
                )

    def _write_manifest(self, name, payload):
        """往沙箱的 `.ai/manifest/` 写一条留痕（**测试夹具**，不是产品写盘）。"""
        folder = os.path.join(".ai", "manifest")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fp:
            if isinstance(payload, str):
                fp.write(payload)
            else:
                json.dump(payload, fp, ensure_ascii=False)

    def test_hit_rate_comes_from_the_manifest_ledger(self):
        """★注入 3 条留痕（2 命中）→ 命中率必须是 **66.7%**，且**数字真的上了看板**。

        这条是"接了来源"的**核心判据**：它同时钉住三件事——
        ① 数字来自留痕而不是拍脑袋；② 分母是**可读条数**；③ 结果显示在度量页上。
        """
        self._write_manifest("a.json", {"cached": True, "model": "m"})
        self._write_manifest("b.json", {"cached": True, "model": "m"})
        self._write_manifest("c.json", {"cached": False, "model": "m"})

        line = read_cache_hit_rate()

        self.assertEqual(line.value, "66.7%")
        self.assertIn(".ai/manifest", line.source)
        self.assertIn("2 条 `cached: true`", line.note)

        html = render_metrics(metrics_payload(ALLOWED_ROOTS), failure_trend(ALLOWED_ROOTS))
        self.assertIn("66.7%", html, "算出来了却没上板——那等于没接")

    def test_corrupt_manifest_is_counted_not_silently_zero(self):
        """坏 JSON **不进分母**，但必须被说出来。

        反例（静默当 0）会让 1/2 变成 1/3——一个**看起来正常**的错数字，
        正是这条判据要拦的形态。
        """
        self._write_manifest("ok1.json", {"cached": True})
        self._write_manifest("ok2.json", {"cached": False})
        self._write_manifest("broken.json", "{ 不是 JSON")

        stats = read_manifest_stats()
        line = read_cache_hit_rate()

        self.assertEqual((stats["files"], stats["readable"], stats["unreadable"]), (3, 2, 1))
        self.assertEqual(line.value, "50.0%")
        self.assertIn("读不到", line.note)

    def test_cached_flag_only_counts_real_booleans(self):
        """`cached` 只认 `is True`：字符串 `"true"` / 缺字段**都不算命中**（宁可少算）。"""
        self._write_manifest("a.json", {"cached": "true"})
        self._write_manifest("b.json", {"cached": 1})
        self._write_manifest("c.json", {})

        self.assertEqual(read_cache_hit_rate().value, "0.0%")
        self.assertEqual(read_manifest_stats()["cached"], 0)

    def test_ai_counts_come_from_real_files(self):
        by_label = {line["label"]: line for line in metrics_payload(ALLOWED_ROOTS)["lines"]}

        self.assertEqual(by_label["待人工确认（PENDING）"]["value"], "1")
        self.assertEqual(by_label["已留痕确认"]["value"], "0")
        self.assertEqual(by_label["待确认草稿"]["value"], "0")
        self.assertEqual(by_label["运行数"]["value"], "1")
        self.assertEqual(by_label["失败运行数"]["value"], "1")


    def test_token_usage_totals_the_ledger(self):
        """token 合计来自留痕里的 `usage`；**没有 `usage` 的条数要单独说**。"""
        self._write_manifest(
            "a.json", {"usage": {"prompt_tokens": 1000, "completion_tokens": 200}}
        )
        self._write_manifest(
            "b.json", {"usage": {"prompt_tokens": 500, "completion_tokens": 100}}
        )
        self._write_manifest("c.json", {"cached": True})  # 命中那次本来就没有 usage

        line = read_token_usage()

        self.assertEqual(line.value, "prompt 1500 / completion 300")
        self.assertIn(".ai/manifest", line.source)
        self.assertIn("1 条没有 `usage`", line.note)

    def test_cost_needs_prices_and_names_the_missing_env_var(self):
        """有 usage 但没配单价 → 「未读到」+ **点名缺哪个变量**（不编 0）。"""
        self._write_manifest("a.json", {"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

        line = read_cost_estimate(env={})

        self.assertEqual(line.value, "未读到")
        self.assertIn(PRICE_PROMPT_ENV, line.note)
        self.assertIn(PRICE_COMPLETION_ENV, line.note)
        self.assertIn(".ai/manifest", line.source, "来源要指到 `usage` 与那两个变量")

    def test_cost_follows_the_configured_prices(self):
        """★元护栏：改单价 → 数字必须跟着变（否则"接了来源"只是装饰）。"""
        self._write_manifest(
            "a.json", {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 2_000_000}}
        )

        line = read_cost_estimate(env={PRICE_PROMPT_ENV: "3", PRICE_COMPLETION_ENV: "5"})
        other = read_cost_estimate(env={PRICE_PROMPT_ENV: "0", PRICE_COMPLETION_ENV: "5"})

        self.assertEqual(line.value, "13.0000")  # 1M×3/1M + 2M×5/1M
        self.assertEqual(other.value, "10.0000")
        self.assertNotEqual(line.value, other.value)
        self.assertIn(PRICE_PROMPT_ENV, line.source)
        self.assertIn("3.0", line.source, "来源里要带**当时用的单价**（复盘时才知道按什么算的）")

    def test_cost_does_not_invent_a_number_when_usage_is_missing(self):
        """缺 `usage` 时**即使配了单价**也不出数——按 0 算会给出一个偏小的假花费。"""
        self._write_manifest("a.json", {"cached": True})

        line = read_cost_estimate(env={PRICE_PROMPT_ENV: "3", PRICE_COMPLETION_ENV: "5"})

        self.assertEqual(line.value, "未读到")
        self.assertNotIn("0.0000", line.value)
        self.assertIn("没有 `usage`", line.note)


class TestHttpSurface(_ServerCase):
    """真实 HTTP 契约（**经回环真起服务**，不是只调渲染函数）。"""

    def test_pages_are_served(self):
        for page in (
            "/",
            "/metrics",
            "/run?path=logs/sample.summary.json",
            "/file?path=reports/analysis.md",
        ):
            with self.subTest(page=page):
                status, body = self.get(page)

                self.assertEqual(status, 200)
                self.assertTrue(body)

    def test_index_lists_the_run_and_the_ratio(self):
        _status, body = self.get("/")

        self.assertIn(b"logs/sample.summary.json", body)
        self.assertIn("未降级占比".encode("utf-8"), body)

    def test_file_download_is_byte_exact(self):
        _status, body = self.get("/file?path=logs/sample.summary.json")

        with open("logs/sample.summary.json", "rb") as fp:
            self.assertEqual(body, fp.read())

    def test_missing_path_param_is_400(self):
        for page in ("/run", "/file"):
            with self.subTest(page=page):
                self.assertEqual(self.get(page)[0], 400)

    def test_unknown_route_is_404(self):
        self.assertEqual(self.get("/nope")[0], 404)

    def test_write_methods_are_405_not_501(self):
        """★`405 不允许` 与 `501 不认得` 是两句不同的话——这里必须是 405。"""
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, body = self.get("/", method=method)

                self.assertEqual(status, 405, "写方法必须被**明确拒绝**")
                self.assertIn(b"405", body)

    def test_head_has_no_body(self):
        status, body = self.get("/", method="HEAD")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_traversal_over_http_is_refused(self):
        from urllib.parse import quote  # noqa: PLC0415

        for sample in TRAVERSAL_SAMPLES:
            with self.subTest(sample=sample):
                status, _body = self.get("/file?path=" + quote(sample, safe=""))

                self.assertEqual(status, 404)

    def test_prefix_confusion_over_http_is_refused(self):
        """★诱饵文件**真的存在**（`logs_evil/secret.txt`），仍必须被拒——
        否则"被拒"可能只是因为文件没生成，那样就没测到规则。"""
        self.assertTrue(os.path.isfile("logs_evil/secret.txt"))

        status, body = self.get("/file?path=logs_evil/secret.txt")

        self.assertEqual(status, 404)
        self.assertNotIn("不该被读到".encode("utf-8"), body)

    def test_bad_json_summary_is_not_silently_empty(self):
        """坏 JSON → 404 且页面**写明读不到**，不渲染成一个"空的正常页"。"""
        with open("logs/broken.summary.json", "w", encoding="utf-8") as fp:
            fp.write("{ not json")

        status, body = self.get("/run?path=logs/broken.summary.json")

        self.assertEqual(status, 404)
        self.assertIn("读不到".encode("utf-8"), body)


class TestZeroWrite(_ServerCase):
    """★验收第 1 条：**零写盘**。

    判据不是"我说不写"，而是**取快照 → 把页面全跑一遍 → 再对账**。
    快照必须是**全量**的（含 `.ai/`、`reports/`、`logs/`）——那恰恰是看板唯一能触碰的地方。
    """

    def test_nothing_on_disk_changes_after_serving_every_page(self):
        before = snapshot_all(os.getcwd())
        self.assertTrue(
            any(path.startswith(".ai/") for path in before), "快照没覆盖 .ai/，判据会是空的"
        )

        for page in (
            "/",
            "/metrics",
            "/run?path=logs/sample.summary.json",
            "/file?path=reports/analysis.md",
            "/file?path=logs/sample.summary.json",
            "/nope",
            "/run",
            "/file",
            "/file?path=logs_evil/secret.txt",
            "/file?path=../interfacetester/parser.py",
        ):
            self.get(page)
        self.get("/", method="POST")
        self.get("/", method="HEAD")

        after = snapshot_all(os.getcwd())

        self.assertEqual(diff_all(before, after), [])

    def test_download_does_not_touch_the_source_file(self):
        target = os.path.join(os.getcwd(), "logs", "sample.summary.json")
        before = os.stat(target).st_mtime_ns

        self.get("/file?path=logs/sample.summary.json")

        self.assertEqual(os.stat(target).st_mtime_ns, before)

    def test_snapshot_all_covers_the_dirs_snapshot_tree_ignores(self):
        """★元护栏（本文件里最重要的一条）：证明**不能**复用 `workdir.snapshot_tree`。

        那个函数为「L2 别污染源码树」而**刻意忽略** `.ai/`、`logs/` 这些运行期产物目录；
        而看板要监测的恰恰就是它们。若用它的口径，「零写盘」会变成一条**永远为真**的护栏——
        比没有护栏更坏，因为它会让人以为验过了。
        """
        from interfacetester_ai.workdir import snapshot_tree  # noqa: PLC0415

        mine = snapshot_all(os.getcwd())
        theirs = snapshot_tree(os.getcwd())

        self.assertIn("logs/sample.summary.json", mine)
        self.assertNotIn(
            "logs/sample.summary.json",
            theirs,
            "快照口径变了：请重新评估「零写盘」是否可以直接复用 snapshot_tree",
        )


class TestZeroExecute(unittest.TestCase):
    """★验收第 2 条：**零执行**——看板不跑任何用户代码。"""

    def _module_tree(self):
        import ast  # noqa: PLC0415

        path = os.path.join(BASE, "interfacetester_ai", "web.py")
        with open(path, encoding="utf-8") as fp:
            return ast.parse(fp.read(), filename=path)

    def test_module_imports_nothing_executable(self):
        import ast  # noqa: PLC0415

        imported = set()
        for node in ast.walk(self._module_tree()):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        banned = sorted(
            name
            for name in imported
            if name
            in {
                "pytest",
                "subprocess",
                "importlib",
                "runpy",
                "ctypes",
                "interfacetester.make",
                "interfacetester_ai.llm",
                "interfacetester_ai.gen",
            }
        )

        self.assertEqual(banned, [], f"看板 import 了会执行东西的模块：{banned}")

    def test_handler_makes_no_write_calls(self):
        """★只查 `WebHandler`（**请求路径**）：里面不许有任何写盘调用。

        （模块自检为了造夹具会在**临时目录**里写文件——那是自检，不是看板运行时。
        用 AST 把两者分开查，比在整份源码上做文本匹配准得多。）
        """
        import ast  # noqa: PLC0415

        handler = next(
            node
            for node in self._module_tree().body
            if isinstance(node, ast.ClassDef) and node.name == "WebHandler"
        )

        called = set()
        for node in ast.walk(handler):
            if not isinstance(node, ast.Call):
                continue
            parts = []
            func = node.func
            while isinstance(func, ast.Attribute):
                parts.append(func.attr)
                func = func.value
            if isinstance(func, ast.Name):
                parts.append(func.id)
            called.add(".".join(reversed(parts)))

        for banned in (
            "os.makedirs",
            "os.remove",
            "os.unlink",
            "os.rmdir",
            "os.replace",
            "os.rename",
            "shutil.rmtree",
            "os.chmod",
        ):
            self.assertNotIn(banned, called, f"WebHandler 里出现了写盘调用 `{banned}`")


class TestCliServe(_SandboxCase):
    """CLI 契约（`serve` 是**常驻**命令，所以这里只查它不阻塞的那些面）。"""

    def test_serve_is_registered(self):
        from interfacetester_ai.cli import build_parser  # noqa: PLC0415

        self.assertIn("serve", build_parser().format_help())

    def test_defaults_are_loopback(self):
        from interfacetester_ai.cli import build_parser  # noqa: PLC0415

        args = build_parser().parse_args(["serve"])

        self.assertEqual(args.host, DEFAULT_HOST)
        self.assertEqual(args.port, DEFAULT_PORT)
        self.assertEqual(args.root, [])

    def test_bad_read_root_is_a_config_error(self):
        """★违规读根 → 退出码 2（**不是** traceback 糊一脸）。"""
        from interfacetester_ai.cli import EXIT_CONFIG, main  # noqa: PLC0415

        self.assertEqual(main(["serve", "--root", "../outside"]), EXIT_CONFIG)


class TestPackageFormAndSelftest(unittest.TestCase):
    """模块形态与自检。"""

    def test_web_is_not_a_registered_writer(self):
        """★"零写盘"的**形态**：看板**不进** T18 写盘注册表（它没有写盘点）。"""
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        self.assertNotIn("web", MODULE_WRITE_BOUNDARIES)

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_host_check_is_neutered(self):
        """元护栏：让"回环判定"恒真 → 自检必红（否则那道判据是装饰）。"""
        import interfacetester_ai.web as module  # noqa: PLC0415
        from unittest import mock  # noqa: PLC0415

        with mock.patch.object(module, "check_host", lambda host: (True, "")):
            self.assertNotEqual(module.run_selftest(), 0)

    def test_selftest_turns_red_when_snapshot_is_blinded(self):
        """元护栏：让全量快照恒空 → 「零写盘」必红（证明它真在看文件）。"""
        import interfacetester_ai.web as module  # noqa: PLC0415
        from unittest import mock  # noqa: PLC0415

        with mock.patch.object(module, "snapshot_all", lambda root: {}):
            self.assertNotEqual(module.run_selftest(), 0)


class TestHardening(_SandboxCase):
    """★P3c 加固：IP 白名单 + Bearer token + **反代头默认不信任**。

    （模块自检里有同款判据；这里是**从测试角度**再钉一遍——两者独立：
    自检管"模块自己跑得起来"，测试管"集成行为对"。）
    """

    def _serve(self, **kwargs):
        server = make_server(DEFAULT_HOST, 0, ALLOWED_ROOTS, **kwargs)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # `addCleanup` 是**逆序**执行的：先注册 close、再注册 shutdown，
        # 于是实际执行 shutdown → close（顺序不能反）
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return port

    def _get(self, port, path, headers=None):
        return fetch(port, path, headers=headers)

    def test_default_is_open_on_loopback(self):
        """默认（回环 + 未配鉴权）= 本地开发形态：**不拦**。

        ★这不是"忘配了"：默认只监听回环、且默认没有任何鉴权项，
        此时拦住全部请求只会让"在本地看一眼产物"变成麻烦事。
        """
        port = self._serve()

        self.assertEqual(self._get(port, "/")[0], 200)

    def test_ip_allowlist_blocks_loopback(self):
        port = self._serve(allowed_ips=("10.0.0.1",))

        self.assertEqual(self._get(port, "/")[0], 403)

    def test_forwarded_header_is_not_trusted_by_default(self):
        """★**伪造 `X-Forwarded-For` 骗不过白名单**（本批最要紧的一条）。

        `X-Forwarded-For: 127.0.0.1` 是**一句话的事**——拿它当白名单依据，
        等于没有白名单。所以判定必须用 `client_address`（真实对端）。
        """
        port = self._serve(allowed_ips=("10.0.0.1",))

        status, _body = self._get(port, "/", headers={"X-Forwarded-For": "10.0.0.1"})

        self.assertEqual(status, 403)

    def test_trust_proxy_makes_the_header_effective(self):
        """★元护栏：证明上一条不是"这个头根本没被读"。

        显式 `trust_proxy=True` 之后，同一个头**必须**生效——
        否则上一条的 403 可能只是因为实现压根没看这个头，
        那样它就成了一个"永远为真"的判据。
        """
        port = self._serve(allowed_ips=("10.0.0.1",), trust_proxy=True)

        status, _body = self._get(port, "/", headers={"X-Forwarded-For": "10.0.0.1"})

        self.assertEqual(status, 200)

    def test_missing_token_is_401_with_challenge(self):
        port = self._serve(expected_token="s3cr3t")

        status, headers = fetch_status_headers(port, "/")

        self.assertEqual(status, 401)
        self.assertIn("bearer", str(headers.get("WWW-Authenticate", "")).lower())

    def test_wrong_token_is_403(self):
        port = self._serve(expected_token="s3cr3t")

        status, _body = self._get(
            port, "/", headers={"Authorization": "Bearer nope"}
        )

        self.assertEqual(status, 403)

    def test_right_token_passes_and_token_never_lands_in_the_page(self):
        port = self._serve(expected_token="s3cr3t")

        status, body = self._get(
            port, "/", headers={"Authorization": "Bearer s3cr3t"}
        )

        self.assertEqual(status, 200)
        self.assertNotIn(b"s3cr3t", body, "★密钥绝不透传给前端")


if __name__ == "__main__":
    unittest.main()






