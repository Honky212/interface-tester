# -*- coding: utf-8 -*-
r"""触发运行的护栏用例 —— v8 §5.8 行 1002（**P3b-2**）。

## 本文件钉什么

| 要求 | 用例 |
| --- | --- |
| **触发运行必过 L3 闸门** | `TestGate::test_l3_refused_when_sandbox_off` + `test_l3_is_the_same_gate`（**元护栏**） |
| **被拒时零副作用** | `TestNoSideEffects::*` |
| **闸门在子进程之前** | `TestRealSubprocess::test_gate_refusal_starts_no_process` |
| **作业目录隔离** | `TestRealSubprocess::test_job_writes_into_its_own_dir` |
| **退出码落盘**（R8） | `TestRealSubprocess::test_exit_code_is_recorded` |
| **`jobs` 登记进 T18**（与 `web`/`confirm` **相反**） | `TestModuleForm::*` |

## ★与 `web_readonly_test.py` 的关系

那个文件钉 P3a 的「**零执行**」；本文件钉 P3b-2 的「**受控执行**」。
它们是**对立的两件事**，所以判据必须分开写清：
看板的任何页面都不许跑东西；而 `POST /jobs` 跑东西，**前提是过闸门**。

## ★`env` 参数只影响**闸门判断**

`run_job(request, env=…)` 里的 `env` 是给 `check_l3_allowed` 判的；
**子进程继承的是真实的 `os.environ`**。这不是漏洞，而是**两层各自成立**：
Web 层管"这个请求要不要放行"，内核 CLI 自己有它那一层的开关。
（本文件里子进程跑的是**不存在的用例**，不会真发请求，与两层都无关。）
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

from interfacetester_ai.jobs import (  # noqa: E402
    JOBS_DIR,
    MAX_TIMEOUT,
    REFUSE_ARG_INJECTION,
    REFUSE_BAD_TIMEOUT,
    REFUSE_L3,
    REFUSE_NO_PATH,
    JobRequest,
    JobResult,
    build_cmd,
    case_path_ok,
    check_run_allowed,
    list_jobs,
    read_job,
    render_job_report,
    run_job,
    run_selftest,
)
from interfacetester_ai.web import (  # noqa: E402
    ALLOWED_ROOTS,
    CONFIRM_REFUSED_STATUS,
    DEFAULT_HOST,
    fetch,
    make_server,
)

# 闸门放行所需的 env（**只影响闸门判断**，不影响子进程）
SANDBOX_ON = {"INTERFACETESTER_AI_SANDBOX": "on"}
LOOPBACK_URL = "http://127.0.0.1:80"


class _SandboxCase(unittest.TestCase):
    """临时工作区（`cases/` 有夹具；**不碰本仓的 `.ai/jobs/`**）。"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_jobs_test_")
        os.chdir(self._tmp.name)
        os.makedirs("cases", exist_ok=True)
        with open("cases/login_flow.yml", "w", encoding="utf-8") as fp:
            fp.write("# 自检用的占位用例\n")

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class _ServerCase(_SandboxCase):
    """临时工作区 + 回环临时端口上的看板。"""

    def setUp(self):
        super().setUp()
        self.server = make_server(DEFAULT_HOST, 0, ALLOWED_ROOTS)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        super().tearDown()

    def get(self, path, method=None, body=None):
        return fetch(self.port, path, method=method, body=body)


class TestGate(_SandboxCase):
    """★闸门：运行域 + 参数注入 + L3 + 超时。"""

    def test_good_paths_are_allowed(self):
        for good in ("cases/login_flow.yml", "cases/sub/x.yaml"):
            with self.subTest(path=good):
                self.assertTrue(case_path_ok(good)[0])

    def test_outside_domain_is_refused(self):
        for bad in ("../etc/x.yml", "cases_evil/x.yml", "/etc/passwd.yml", "cases/x.py"):
            with self.subTest(path=bad):
                ok, why = case_path_ok(bad)

                self.assertFalse(ok)
                self.assertTrue(why)

    def test_argument_injection_is_refused(self):
        """★`-` 开头的是 **pytest 选项**：`--pyargs` / `-p` 都能扩大执行面。"""
        for injected in ("--pyargs", "-p", "--rootdir=/", "-k"):
            with self.subTest(arg=injected):
                ok, why = case_path_ok(injected)

                self.assertFalse(ok)
                self.assertIn("选项", why)

    def test_l3_refused_when_sandbox_off(self):
        """★§2.5 安全面 1 / R7：触发运行**必过** L3 闸门，"是网页点的"不构成放宽理由。

        ★实测修正（2026-09-25）：本用例原先**依赖真实环境** —— `check_run_allowed`
        不传 `env` 就读 `os.environ`，于是**用户/CI 只要设了 `INTERFACETESTER_AI_SANDBOX=on`
        这条就必红**（而那是**完全正常**的使用方式——真模型端到端本来就要求设它）。
        判据是"沙盒**关**时必须拒"，所以必须**显式构造"关"的环境**，
        而不是指望它恰好是关的。
        """
        from unittest import mock  # noqa: PLC0415

        with mock.patch.dict(os.environ, {}, clear=True):
            allowed, code, reason = check_run_allowed(JobRequest(paths=("cases/x.yml",)))

        self.assertFalse(allowed)
        self.assertEqual(code, REFUSE_L3)
        self.assertIn("INTERFACETESTER_AI_SANDBOX", reason)

    def test_l3_refused_for_non_loopback_even_with_sandbox_on(self):
        allowed, code, _reason = check_run_allowed(
            JobRequest(paths=("cases/x.yml",), base_url="http://example.com"), SANDBOX_ON
        )

        self.assertFalse(allowed)
        self.assertEqual(code, REFUSE_L3)

    def test_loopback_with_sandbox_on_is_allowed(self):
        allowed, code, reason = check_run_allowed(
            JobRequest(paths=("cases/x.yml",), base_url=LOOPBACK_URL), SANDBOX_ON
        )

        self.assertTrue(allowed, f"{code} / {reason}")
        self.assertIn("回环", reason)

    def test_l3_is_the_same_gate(self):
        """★**元护栏**：L3 必须是**同一个**闸门，不是另写一份。

        手法：patch `l3.check_l3_allowed` → 本模块的结论**必须**跟着变。
        若不变，说明这里另写了一套判断——那正是 §2.5 明令禁止的
        「Web 里出现另一套校验」（"Web 与 CLI 是同一个 Core 的两个前端"）。
        """
        from unittest import mock  # noqa: PLC0415

        import interfacetester_ai.l3 as l3_module  # noqa: PLC0415

        with mock.patch.object(
            l3_module, "check_l3_allowed", lambda url, env=None: (True, "补丁放行")
        ):
            allowed, _code, reason = check_run_allowed(
                JobRequest(paths=("cases/x.yml",), base_url="http://example.com")
            )

        self.assertTrue(allowed, "patch 之后应当放行——否则用的不是同一个闸门")
        self.assertIn("补丁放行", reason)

    def test_empty_paths_is_refused(self):
        allowed, code, _reason = check_run_allowed(JobRequest())

        self.assertFalse(allowed)
        self.assertEqual(code, REFUSE_NO_PATH)

    def test_bad_timeout_is_refused(self):
        for bad_timeout in (0, -1, MAX_TIMEOUT + 1):
            with self.subTest(timeout=bad_timeout):
                allowed, code, _reason = check_run_allowed(
                    JobRequest(paths=("cases/x.yml",), timeout=bad_timeout)
                )

                self.assertFalse(allowed)
                self.assertEqual(code, REFUSE_BAD_TIMEOUT)

    def test_argument_injection_code_is_distinct(self):
        """参数注入要有**自己的**拒因码（可度量：闸门拒了多少次、都因为什么）。"""
        allowed, code, _reason = check_run_allowed(JobRequest(paths=("--pyargs",)))

        self.assertFalse(allowed)
        self.assertEqual(code, REFUSE_ARG_INJECTION)


class TestNoSideEffects(_SandboxCase):
    """★「拒绝」必须发生在**任何副作用之前**（查事实，不是查声明）。"""

    def test_refusal_allocates_nothing(self):
        result = run_job(JobRequest(paths=("cases/login_flow.yml",)))  # 沙盒未开

        self.assertTrue(result.refused)
        self.assertEqual(result.code, REFUSE_L3)
        self.assertEqual(result.job_id, "", "被拒时不该分配作业号")
        self.assertEqual(result.job_dir, "", "被拒时不该有作业目录")
        self.assertEqual(result.cmd, (), "被拒时不该构造命令（更不该起进程）")
        self.assertIsNone(result.exit_code)

    def test_jobs_dir_is_untouched_on_refusal(self):
        """★查**文件系统事实**：被拒前后 `.ai/jobs/` 的目录集合必须不变。"""
        before = set(os.listdir(JOBS_DIR)) if os.path.isdir(JOBS_DIR) else set()

        run_job(JobRequest(paths=("cases/login_flow.yml",)))
        run_job(JobRequest(paths=("--pyargs",)))
        run_job(JobRequest(paths=("cases/login_flow.yml",), timeout=0))

        after = set(os.listdir(JOBS_DIR)) if os.path.isdir(JOBS_DIR) else set()
        self.assertEqual(after, before, f"被拒后多出了：{sorted(after - before)}")

    def test_dry_run_does_not_execute(self):
        """★`dry_run`：只到闸门为止——能回答"会跑什么"，但一行都不跑。"""
        result = run_job(
            JobRequest(paths=("cases/login_flow.yml",), base_url=LOOPBACK_URL),
            env=SANDBOX_ON,
            dry_run=True,
        )

        self.assertFalse(result.refused)
        self.assertTrue(result.ok)
        self.assertIn("未执行", result.reason)
        self.assertEqual(result.job_dir, "")
        self.assertIsNone(result.exit_code)


class TestRealSubprocess(_SandboxCase):
    """★**真的起子进程**——这是"受控执行"能被叫作"执行"的唯一证据。

    跑的是**不存在的用例**：pytest 会以非 0 退出，所以这几条**不需要网络**、
    也不需要真服务——要验的是「闸门放行 → 真的起了进程 → 退出码落了盘」。
    """

    def setUp(self):
        super().setUp()
        # ★子进程用的是**真实的 `os.environ`**（`run_job` 的 `env` 只影响**闸门判断**），
        #   所以这里要让它找得到 `interfacetester` 包——否则测出来的是
        #   "模块找不到"而不是"pytest 的行为"，那样的绿灯是假的。
        self._old_pp = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = BASE + os.pathsep + (self._old_pp or "")

    def tearDown(self):
        if self._old_pp is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = self._old_pp
        super().tearDown()

    def _run_missing(self, **kwargs):
        return run_job(
            JobRequest(
                paths=("cases/definitely_not_here.yml",),
                base_url=LOOPBACK_URL,
                **kwargs,
            ),
            env=dict(SANDBOX_ON),
        )

    def test_exit_code_is_recorded(self):
        result = self._run_missing(triggered_by="tester", timeout=120)

        self.assertFalse(result.refused, result.reason)
        self.assertIsNotNone(result.exit_code, "真跑过就必须有退出码")
        # pytest 对"文件不存在"给非 0（4）；对"没收集到测试"给 5。两者都非 0。
        self.assertNotEqual(result.exit_code, 0)

    def test_job_writes_into_its_own_dir(self):
        result = self._run_missing(triggered_by="tester", timeout=120)

        self.assertTrue(result.job_id)
        self.assertEqual(result.job_dir, JOBS_DIR + "/" + result.job_id)
        record = os.path.join(JOBS_DIR, result.job_id, "result.json")
        self.assertTrue(os.path.isfile(record), "结果必须落盘（含退出码与触发者）")

        with open(record, encoding="utf-8") as fp:
            payload = json.load(fp)

        self.assertEqual(payload["job_id"], result.job_id)
        self.assertEqual(payload["exit_code"], result.exit_code)
        self.assertEqual(payload["triggered_by"], "tester")
        self.assertIn("stdout_truncated", payload)
        self.assertIn("stderr_truncated", payload)
        self.assertTrue(payload["note"])

    def test_gate_refusal_starts_no_process(self):
        """★闸门拒 → **子进程一个都没起**：`.ai/jobs/` 完全没动。"""
        before = set(os.listdir(JOBS_DIR)) if os.path.isdir(JOBS_DIR) else set()

        result = run_job(JobRequest(paths=("cases/login_flow.yml",)))  # 沙盒未开

        after = set(os.listdir(JOBS_DIR)) if os.path.isdir(JOBS_DIR) else set()
        self.assertTrue(result.refused)
        self.assertEqual(after, before)

    def test_list_jobs_is_newest_first(self):
        first = self._run_missing(timeout=120)
        second = self._run_missing(timeout=120)

        ids = [entry.get("job_id") for entry in list_jobs()]

        self.assertIn(first.job_id, ids)
        self.assertIn(second.job_id, ids)
        self.assertLess(
            ids.index(second.job_id), ids.index(first.job_id), "最近的应当排前面"
        )


class TestHttpJobs(_ServerCase):
    """`/jobs` 的 HTTP 契约（**只读面** + 触发面的拒绝路径）。"""

    def test_jobs_page_is_served(self):
        status, body = self.get("/jobs")

        self.assertEqual(status, 200)
        self.assertIn("触发运行".encode("utf-8"), body)

    def test_unknown_job_is_404(self):
        self.assertEqual(self.get("/jobs?job=run-19700101-000000-1-0001")[0], 404)

    def test_missing_triggered_by_is_422(self):
        """★与确认门要求 `approver` 是**同一条**纪律：没有触发者就不跑。"""
        status, body = self.get(
            "/jobs", body=json.dumps({"paths": ["cases/x.yml"]}).encode("utf-8")
        )

        self.assertEqual(status, CONFIRM_REFUSED_STATUS)
        self.assertIn("triggered_by".encode("utf-8"), body)

    def test_bad_json_is_422(self):
        self.assertEqual(
            self.get("/jobs", body=b"{ not json")[0], CONFIRM_REFUSED_STATUS
        )

    def test_gate_refusal_is_422_and_side_effect_free(self):
        before = set(os.listdir(JOBS_DIR)) if os.path.isdir(JOBS_DIR) else set()

        status, body = self.get(
            "/jobs",
            body=json.dumps(
                {"paths": ["cases/login_flow.yml"], "triggered_by": "tester"}
            ).encode("utf-8"),
        )

        after = set(os.listdir(JOBS_DIR)) if os.path.isdir(JOBS_DIR) else set()

        self.assertEqual(status, CONFIRM_REFUSED_STATUS)
        self.assertIn("没有执行".encode("utf-8"), body)
        self.assertEqual(after, before, "被拒的触发**不许**留下任何作业目录")


class TestModuleForm(unittest.TestCase):
    """★`jobs` 与 `web` / `confirm` 的**有意对比**。

    那两个**刻意不在册**（源码里没有写调用）；`jobs` 会**真的执行 + 真的写盘**，
    所以**必须**登记。这条对比本身就是设计的一部分——不是"谁能写就登记谁"。
    """

    def test_jobs_is_a_registered_writer(self):
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        self.assertIn("jobs", MODULE_WRITE_BOUNDARIES)
        kind, roots = MODULE_WRITE_BOUNDARIES["jobs"]

        self.assertEqual(kind, "roots")
        self.assertIn(".ai/", roots)

    def test_web_and_confirm_are_not_registered(self):
        """反面：看板与确认门**刻意不在册**（零写盘 / 落盘委托给别处）。"""
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        for name in ("web", "confirm"):
            with self.subTest(module=name):
                self.assertNotIn(name, MODULE_WRITE_BOUNDARIES)

    def test_command_uses_sys_executable(self):
        """★不用 PATH 里的 `hrun`：它不保证存在（客户环境常见）。"""
        cmd = build_cmd(JobRequest(paths=("cases/a.yml",)))

        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1:4], ("-m", "interfacetester.cli", "run"))

    def test_render_refused_report_says_not_executed(self):
        text = render_job_report(JobResult(refused=True, code=REFUSE_L3, reason="沙盒未开"))

        self.assertIn("没有执行", text)
        self.assertIn(REFUSE_L3, text)

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)


if __name__ == "__main__":
    unittest.main()



