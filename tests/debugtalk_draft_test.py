# -*- coding: utf-8 -*-
r"""debugtalk 草稿的护栏用例 —— v8 §5.9（**P2b**）。

## §5.9 的三条验收，本文件逐条钉住

| 验收 | 用例 |
| --- | --- |
| **每个函数强制带已知答案测试** | `TestKnownAnswerDryRun::test_vectors_are_executed_not_just_checked` |
| **无测试向量则拒产** | `TestNoVectorRefuses::*` |
| **往返解析 100%**（`${fn(...)}` 被 `parser` 解析为函数调用） | `TestRoundtrip::*` |

## 本文件里最要紧的一条：往返判据要用**内核的正则**

§5.9 点名的"源码级地雷"是：内核的参数字符集 `[\$\w\.\-\/\s=,]` **不含引号**，
于是 `${f('abc')}` 不被认作函数调用、**只告警不报错**，最后**静默产出垃圾值**。

所以这里的判据不能是"照规则再写一遍一个校验"——那等于把"能不能被解析"从
**内核说了算**变成**我说了算**。`test_judgement_uses_the_kernel_regex` 就是钉这一点。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai import debugtalk_draft as dd  # noqa: E402
from interfacetester_ai import l3 as l3_mod  # noqa: E402
from interfacetester_ai.debugtalk_draft import (  # noqa: E402
    INPROCESS_MODE,
    REFUSE_BAD_VECTOR,
    REFUSE_DUPLICATE,
    REFUSE_NO_VECTOR,
    REFUSE_QUOTE_IN_ARGS,
    SANDBOX_MODE,
    FunctionRequest,
    TestVector,
    _bare_arg,
    append_to_existing,
    check_request_roundtrip,
    draft_debugtalk,
    dry_run,
    existing_function_names,
    render_draft_report,
    roundtrip_check,
    run_kat_in_sandbox,
    run_selftest,
)

MD5_ABC = "900150983cd24fb0d6963f7d28e17f72"  # 公开已知值：md5("abc")

SPEC = {
    "functions": [
        {
            "name": "sign_md5",
            "args": ["payload"],
            "doc": "接口要求 X-Signature：md5(payload)",
            "body": "import hashlib\nreturn hashlib.md5(str(payload).encode()).hexdigest()",
            "vectors": [{"args": ["abc"], "expect": MD5_ABC}],
        }
    ]
}

EXISTING = "def old_fn():\n    return 1\n"


class _TempWorkspace(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_dt_")
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class TestRoundtrip(unittest.TestCase):
    """★验收③：**往返解析 100%**（判据 = 内核正则本身）。"""

    def test_bare_args_are_accepted(self):
        cases = [
            "${fn()}",
            "${sign_md5($payload)}",
            "${fn(1, 2)}",
            "${fn(a=1, b=$v)}",
            "${fn($a.b-c/d)}",
        ]
        for expr in cases:
            with self.subTest(expr=expr):
                ok, why = roundtrip_check(expr)
                self.assertTrue(ok, why)

    def test_quoted_args_are_rejected(self):
        """★§5.9 的**源码级地雷**：参数带引号 → 内核不认，而且**只告警不报错**。"""
        ok, why = roundtrip_check("${sign_md5('abc')}")

        self.assertFalse(ok)
        self.assertIn("引号", why)
        self.assertIn("不报错", why)

    def test_unwrapped_form_is_rejected(self):
        ok, _why = roundtrip_check("sign_md5($payload)")

        self.assertFalse(ok, "没有 `${…}` 包裹的形态不是函数调用")

    def test_judgement_uses_the_kernel_regex(self):
        """★元护栏：判据必须是**内核的正则**，不是我们自己写的一份。

        手法：把内核的参数字符集**临时改成含引号**，那么 `${f('abc')}` 就该被判**合格**。
        若它仍然不合格，说明我们判的是**自己那份规则**——于是"内核改了、我们没跟上"
        这类漂移永远不会被发现（这正是 §5.9 那行地雷的传播方式）。
        """
        from interfacetester import parser as parser_module  # noqa: PLC0415

        patched = re.compile(r"\$\{([a-zA-Z_]\w*)\(([^)]*)\)\}")
        with mock.patch.object(parser_module, "function_regex_compile", patched):
            ok, _why = roundtrip_check("${f('abc')}")

        self.assertTrue(ok, "内核正则放宽后判据应当跟着放宽（否则判的不是内核的规则）")

    def test_request_roundtrip_covers_every_vector(self):
        request = FunctionRequest(
            name="sign",
            args=("payload",),
            vectors=(TestVector(args=("abc",)), TestVector(args=(18,))),
        )

        self.assertEqual(check_request_roundtrip(request), [])
        self.assertNotEqual(request.call_expr(0), request.call_expr(1))


class TestNoVectorRefuses(unittest.TestCase):
    """★验收②：**无测试向量则拒产**（并说明原因）。"""

    def test_missing_vectors_is_refused(self):
        result = draft_debugtalk({"functions": [{"name": "f", "args": ["x"], "body": "return x"}]})

        self.assertEqual(result.accepted(), [])
        self.assertEqual(result.decisions[0].code, REFUSE_NO_VECTOR)

    def test_refusal_reason_is_explained(self):
        result = draft_debugtalk({"functions": [{"name": "f", "args": ["x"]}]})

        reason = result.decisions[0].reason
        self.assertIn("已知答案测试", reason)
        self.assertIn("§5.9", reason)

    def test_no_draft_block_when_everything_is_refused(self):
        result = draft_debugtalk({"functions": [{"name": "f", "args": ["x"]}]})

        self.assertEqual(result.draft_block, "")
        self.assertEqual(result.diff, "")


class TestKnownAnswerDryRun(unittest.TestCase):
    """★验收①：每个函数**强制带已知答案测试**，而且是**真的执行**。"""

    def test_good_vector_passes(self):
        result = draft_debugtalk(SPEC, source_text=EXISTING)

        self.assertEqual([item.name for item in result.accepted()], ["sign_md5"])
        self.assertEqual(result.decisions[0].code, "")
        self.assertIn(MD5_ABC, result.draft_block)

    def test_wrong_expect_is_refused(self):
        """★元护栏：把期望值改一个字符 → 必须从"采纳"变"拒产"（说明真的执行了）。"""
        spec = json.loads(json.dumps(SPEC, ensure_ascii=False))
        spec["functions"][0]["vectors"][0]["expect"] = "0" * 32

        result = draft_debugtalk(spec, source_text=EXISTING)

        self.assertEqual(result.accepted(), [])
        self.assertEqual(result.decisions[0].code, REFUSE_BAD_VECTOR)

    def test_broken_source_is_refused(self):
        spec = json.loads(json.dumps(SPEC, ensure_ascii=False))
        spec["functions"][0]["body"] = "return undefined_name_here"

        result = draft_debugtalk(spec, source_text=EXISTING)

        self.assertEqual(result.accepted(), [])
        self.assertEqual(result.decisions[0].code, REFUSE_BAD_VECTOR)

    def test_vectors_are_executed_not_just_checked(self):
        """判据是"**函数与期望输出对得上**"，不是"有没有那张表"。"""
        requests = [FunctionRequest(name="f", vectors=(TestVector(args=(2,), expect=5),))]

        failures = dry_run(requests, source="def f(x):\n    return x + 1\n")

        self.assertEqual(len(failures), 1)
        self.assertIn("期望 5", failures[0])

    def test_missing_function_in_source_is_refused(self):
        requests = [FunctionRequest(name="nope", vectors=(TestVector(expect=1),))]

        failures = dry_run(requests, source="def other():\n    return 1\n")

        self.assertIn("没有这个可调用函数", failures[0])


class TestAppendNeverOverwrites(unittest.TestCase):
    """§5.9：**追加块 + diff，绝不整文件覆盖**。"""

    def test_existing_source_is_preserved_verbatim(self):
        result = draft_debugtalk(SPEC, source_text=EXISTING)

        self.assertTrue(result.appended.startswith(EXISTING), "已有源码必须逐字保留")

    def test_diff_only_adds_lines(self):
        result = draft_debugtalk(SPEC, source_text=EXISTING)

        removed = [
            line
            for line in result.diff.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        self.assertEqual(removed, [], "追加 diff 里不该有删除行")
        self.assertIn("+", result.diff)

    def test_no_diff_when_there_is_no_existing_file(self):
        result = draft_debugtalk(SPEC)

        self.assertEqual(result.diff, "")
        self.assertIn("def sign_md5", result.appended)

    def test_duplicate_name_is_flagged_not_rewritten(self):
        """★§5.9："同名只提示不改写"——**仍是采纳**，但要人先看已有的那份。"""
        spec = {"functions": [dict(SPEC["functions"][0], name="old_fn")]}

        result = draft_debugtalk(spec, source_text=EXISTING)

        self.assertEqual(len(result.accepted()), 1)
        self.assertEqual(result.decisions[0].code, REFUSE_DUPLICATE)
        self.assertIn("已存在", result.decisions[0].reason)
        self.assertIn("old_fn", existing_function_names("", source_text=EXISTING))

    def test_append_helper_keeps_missing_newline_safe(self):
        appended = append_to_existing("def a():\n    return 1", "# block")

        self.assertTrue(
            appended.startswith("def a():\n    return 1\n"), "缺换行时要补上而不是粘住"
        )
        self.assertIn("# block", appended)


class TestBareArgRendering(unittest.TestCase):
    """§5.9："标量参数裸写，字符串参数经 `config.variables` 以 `$var` 传入"。"""

    def test_strings_become_dollar_vars(self):
        self.assertEqual(_bare_arg("abc"), "$abc")
        self.assertEqual(_bare_arg("hello world"), "$hello_world")

    def test_numbers_stay_literals(self):
        self.assertEqual(_bare_arg(18), "18")
        self.assertEqual(_bare_arg(1.5), "1.5")
        self.assertEqual(_bare_arg(True), "true")

    def test_generated_call_passes_roundtrip(self):
        """★端到端：草稿渲染出来的调用形态**必须**被内核认作函数调用。"""
        request = FunctionRequest(
            name="sign",
            args=("payload",),
            vectors=(TestVector(args=("abc",)), TestVector(args=(18,))),
        )

        for index in range(2):
            ok, why = roundtrip_check(request.call_expr(index))
            self.assertTrue(ok, f"向量[{index}]：{why}")


class TestCliDebugtalk(_TempWorkspace):
    """`haify debugtalk` 的 CLI 契约。"""

    def _write_spec(self, payload=SPEC):
        with open("spec.json", "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False)
        return "spec.json"

    def test_cli_has_no_apply_flag(self):
        """★与本仓其它命令一致：产物给人看，**落地动作留给人**。"""
        from interfacetester_ai.cli import main  # noqa: PLC0415

        with self.assertRaises(SystemExit):
            main(["debugtalk", "spec.json", "--apply"])

    def test_cli_runs_and_exits_zero(self):
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        self.assertEqual(main(["debugtalk", self._write_spec()]), EXIT_OK)

    def test_cli_exit_code_is_zero_even_when_refused(self):
        """★"无向量 → 拒产"是**正确的输出**，不是故障。"""
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        path = self._write_spec({"functions": [{"name": "f", "args": ["x"]}]})

        self.assertEqual(main(["debugtalk", path]), EXIT_OK)

    def test_cli_missing_spec_is_a_config_error(self):
        from interfacetester_ai.cli import EXIT_CONFIG, main  # noqa: PLC0415

        self.assertEqual(main(["debugtalk", "nope.json"]), EXIT_CONFIG)

    def test_cli_does_not_touch_the_existing_file(self):
        """★只读：`--existing` 指向的文件**不被改写**（这正是"只呈现"的形态）。"""
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        with open("debugtalk.py", "w", encoding="utf-8") as fp:
            fp.write(EXISTING)
        before = os.stat("debugtalk.py").st_mtime_ns

        self.assertEqual(
            main(["debugtalk", self._write_spec(), "--existing", "debugtalk.py"]), EXIT_OK
        )

        self.assertEqual(os.stat("debugtalk.py").st_mtime_ns, before)
        with open("debugtalk.py", encoding="utf-8") as fp:
            self.assertEqual(fp.read(), EXISTING)


class TestPackageFormAndSelftest(unittest.TestCase):
    """模块形态与自检。"""

    def test_debugtalk_draft_is_not_a_registered_writer(self):
        """★"只呈现"的**形态**：本模块**不进** T18 注册表（它不写盘）。"""
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        self.assertNotIn("debugtalk_draft", MODULE_WRITE_BOUNDARIES)

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_quote_check_is_neutered(self):
        """元护栏：把"带引号 → 不认"放宽 → 自检必红（否则那道判据是装饰）。"""
        import interfacetester_ai.debugtalk_draft as module  # noqa: PLC0415

        with mock.patch.object(module, "roundtrip_check", lambda expr: (True, "")):
            self.assertNotEqual(module.run_selftest(), 0)

    def test_selftest_turns_red_when_dry_run_is_skipped(self):
        """元护栏：把"已知答案测试"放宽成恒过 → 自检必红。"""
        import interfacetester_ai.debugtalk_draft as module  # noqa: PLC0415

        with mock.patch.object(module, "dry_run", lambda *a, **k: []):
            self.assertNotEqual(module.run_selftest(), 0)


# ---------------------------------------------------------------------------
# ★L3 沙盒（§5.9 的"（沙盒开关下）L3"；口径：允许执行 · 只跑已知答案测试 · 允许真实加密运算）
# ---------------------------------------------------------------------------

MD5_ABC = "900150983cd24fb0d6963f7d28e17f72"  # md5("abc")，公开可验证
HMAC_ABC = "mUba1OAOkT/Ivo5dP34RCkqegy+D+wnDRShdeGONig4="  # HMAC-SHA256(secret, abc) 的 base64

CRYPTO_SOURCE = (
    "def sign_md5(payload):\n"
    "    import hashlib\n"
    "    return hashlib.md5(payload.encode('utf-8')).hexdigest()\n"
    "\n"
    "def sign_hmac(payload, secret):\n"
    "    import base64\n"
    "    import hashlib\n"
    "    import hmac\n"
    "    digest = hmac.new(\n"
    "        secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256\n"
    "    ).digest()\n"
    "    return base64.b64encode(digest).decode('ascii')\n"
)

CRYPTO_REQUESTS = [
    FunctionRequest(
        name="sign_md5",
        args=("payload",),
        vectors=(TestVector(args=("abc",), expect=MD5_ABC),),
    ),
    FunctionRequest(
        name="sign_hmac",
        args=("payload", "secret"),
        vectors=(TestVector(args=("abc", "secret"), expect=HMAC_ABC),),
    ),
]


def _probe(source):
    """造一个"跑一次就露馅"的请求（函数名固定 `probe`）。"""
    return [FunctionRequest(name="probe", vectors=(TestVector(),))]


class TestL3Sandbox(unittest.TestCase):
    """L3 沙盒：**闸门在前 · 只跑已知答案测试 · 允许真实加密运算 · 拦住出网与文件**。"""

    ON = {l3_mod.SANDBOX_ENV: "on"}

    def test_gate_refuses_when_sandbox_is_off(self):
        with self.assertRaises(l3_mod.L3Refused) as caught:
            run_kat_in_sandbox(CRYPTO_REQUESTS, source=CRYPTO_SOURCE, env={})

        self.assertIn(l3_mod.SANDBOX_ENV, str(caught.exception))

    def test_refusal_starts_no_child_process(self):
        """★元护栏：拒绝时**一个子进程都不起**（"拒绝了、可已经跑了一半"是最坏的形态）。"""
        with mock.patch.object(dd.subprocess, "run") as runner:
            with self.assertRaises(l3_mod.L3Refused):
                run_kat_in_sandbox(CRYPTO_REQUESTS, source=CRYPTO_SOURCE, env={})

        runner.assert_not_called()

    def test_real_crypto_known_answer_tests_pass(self):
        """用户口径的**正面样本**：`hashlib` / `hmac` / `base64` 这些真加密运算照常可用。"""
        run = run_kat_in_sandbox(CRYPTO_REQUESTS, source=CRYPTO_SOURCE, env=self.ON)

        self.assertTrue(run.ok, run.failures)
        self.assertEqual(run.mode, SANDBOX_MODE)
        self.assertEqual(len(run.report.get("results") or []), 2)
        for needed in ("meta_path", "__import__", "open", "exec"):
            with self.subTest(guard=needed):
                self.assertIn(needed, run.report.get("guards") or [])



    def test_wrong_expectation_fails_even_in_the_sandbox(self):
        """元护栏：沙盒只证明"跑了"，**不证明"对了"**。"""
        wrong = [
            FunctionRequest(
                name="sign_md5",
                args=("payload",),
                vectors=(TestVector(args=("abc",), expect="0" * 32),),
            )
        ]

        run = run_kat_in_sandbox(wrong, source=CRYPTO_SOURCE, env=self.ON)

        self.assertFalse(run.ok)
        self.assertIn("期望", " ".join(run.failures))

    def test_network_and_file_access_are_blocked(self):
        """★拦不住就是"假沙箱"——比没有沙箱更坏（它让人以为验过了）。"""
        sources = {
            "出网（import socket）": "def probe():\n    import socket\n    return 1\n",
            "文件（open）": "def probe():\n    return open('x.txt').read()\n",
            "动态加载（importlib）": "def probe():\n    import importlib\n    return 1\n",
        }
        for label, source in sources.items():
            with self.subTest(case=label):
                run = run_kat_in_sandbox(_probe(source), source=source, env=self.ON)

                self.assertFalse(run.ok, f"{label} 竟然没被拦")
                self.assertIn("沙盒", " ".join(run.failures))

    def test_timeout_is_reported_not_silent(self):
        source = "def probe():\n    import time\n    time.sleep(30)\n"

        run = run_kat_in_sandbox(_probe(source), source=source, env=self.ON, timeout=1.0)

        self.assertFalse(run.ok)
        self.assertIn("超时", " ".join(run.failures))

    def test_spawn_failure_is_reported(self):
        """起不来也必须**响亮**（不是"没结论"被当成"通过"）。"""
        run = run_kat_in_sandbox(
            CRYPTO_REQUESTS,
            source=CRYPTO_SOURCE,
            env=self.ON,
            python=os.path.join(BASE, "no_such_python"),
        )

        self.assertFalse(run.ok)
        self.assertIn("起不来", " ".join(run.failures))

    def test_child_env_carries_no_ai_credentials(self):
        """★凭据不进子进程：父进程环境里有 key，子进程的 env 里不该有它。"""
        os.environ["INTERFACETESTER_AI_API_KEY"] = "should-not-leak"
        try:
            run = run_kat_in_sandbox(CRYPTO_REQUESTS, source=CRYPTO_SOURCE, env=self.ON)
        finally:
            os.environ.pop("INTERFACETESTER_AI_API_KEY", None)

        self.assertNotIn("INTERFACETESTER_AI_API_KEY", run.report.get("env_keys") or [])

    def test_draft_records_which_mode_ran_the_kat(self):
        """报告必须写清"**在哪验的**"——两种模式的证据强度完全不同。"""
        spec = {
            "functions": [
                {
                    "name": "sign_md5",
                    "args": ["payload"],
                    "body": (
                        "import hashlib\n"
                        "return hashlib.md5(payload.encode('utf-8')).hexdigest()"
                    ),
                    "doc": "用例 sign_md5($payload) 需要",
                    "vectors": [{"args": ["abc"], "expect": MD5_ABC}],
                }
            ]
        }

        sandboxed = draft_debugtalk(spec, sandbox=True, env=self.ON)
        inprocess = draft_debugtalk(spec)

        self.assertIn("沙盒", sandboxed.dry_run_mode)
        self.assertTrue(sandboxed.sandbox_report)
        self.assertIn("沙盒", render_draft_report(sandboxed))
        self.assertEqual(inprocess.dry_run_mode, INPROCESS_MODE)
        self.assertFalse(inprocess.sandbox_report)



class TestCliSandboxFlag(unittest.TestCase):
    """`haify debugtalk --sandbox`：沙盒没开 → **退出码 2**（配置问题）；开了 → **0**。"""

    SPEC = {
        "functions": [
            {
                "name": "sign_md5",
                "args": ["payload"],
                "body": (
                    "import hashlib\n"
                    "return hashlib.md5(payload.encode('utf-8')).hexdigest()"
                ),
                "doc": "用例 sign_md5($payload) 需要",
                "vectors": [{"args": ["abc"], "expect": MD5_ABC}],
            }
        ]
    }

    def _run(self, *, sandbox_on):
        with tempfile.TemporaryDirectory(prefix="ai_dbg_cli_") as tmp:
            spec = os.path.join(tmp, "spec.json")
            with open(spec, "w", encoding="utf-8") as fp:
                json.dump(self.SPEC, fp, ensure_ascii=False)
            env = {**os.environ, "PYTHONPATH": BASE, "PYTHONIOENCODING": "utf-8"}
            env.pop(l3_mod.SANDBOX_ENV, None)
            if sandbox_on:
                env[l3_mod.SANDBOX_ENV] = "on"
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "interfacetester_ai",
                    "debugtalk",
                    spec,
                    "--sandbox",
                ],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )

    def test_sandbox_off_returns_config_exit_code(self):
        proc = self._run(sandbox_on=False)

        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("L3 拒绝", proc.stderr)
        self.assertIn(l3_mod.SANDBOX_ENV, proc.stderr)

    def test_sandbox_on_runs_and_reports_the_mode(self):
        proc = self._run(sandbox_on=True)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("沙盒", proc.stdout)



if __name__ == "__main__":
    unittest.main()


