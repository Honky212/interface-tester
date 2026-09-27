# -*- coding: utf-8 -*-
"""AI 旁路包 CLI 的**端到端退出码**：2 / 3 / 4 / 1 各真跑一次（离线、不用模型）。

★本文件是 `tests/ai_exit_codes_contract_test.py` 那条契约判据的**证据来源**：
判据会在这里找 `assertEqual(proc.returncode, N, …)`。两件事刻意分开——
**判据不能拿自己的合成字符串作证**（注入式实测踩到过，见那边的 docstring）。

## 为什么这几个码能用"离线"方式验（不联网、不需要 Ollama）

`--fake` 只回放 `.ai/cache/`；其余分支都在**调模型之前**结束：

| 码 | 触发条件 | 谁拦的 |
| --- | --- | --- |
| `2` | 少模型配置（`BASE_URL` / `SANDBOX`） | 配置闸（在预算闸之前） |
| `4` | 文档没有字段表 | 文档体检闸 S-10（在切片之前） |
| `3` | `--fake` 且缓存无此输入 | 传输层（`FakeTransport` 明确报"没准备响应"，**不静默假成功**） |
| `1` | 单切片 payload 超预算 | `prompts` 的预算闸 → 被 `gen.py` 记为**片级失败**（★M-4 的发现，不是 `5`） |

工作区零污染：全部跑在**仓外临时目录**（`tempfile.TemporaryDirectory`）。
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _run_cli(args, doc_text=None, env_overrides=None):
    """在仓外临时目录跑 CLI；模型三件套 + 沙盒默认给全（缺任一项会先被"配置闸"拦成 2）。"""
    env = {
        **os.environ,
        "PYTHONPATH": BASE,
        "PYTHONIOENCODING": "utf-8",
        "INTERFACETESTER_AI_BASE_URL": "http://127.0.0.1:11434/v1",
        "INTERFACETESTER_AI_MODEL": "gemma4:latest",
        "INTERFACETESTER_AI_SANDBOX": "on",
    }
    env.update(env_overrides or {})
    with tempfile.TemporaryDirectory(prefix="haify_exit_probe_") as workdir:
        doc = pathlib.Path(workdir) / "probe.md"
        if doc_text is not None:
            doc.write_text(doc_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "interfacetester_ai.cli", "gen", str(doc), *args],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )


class TestExitCodesAreActuallyReachable(unittest.TestCase):
    """四个码各真跑一次，并把 M-4 的发现钉进用例。"""

    def test_code_2_when_model_config_is_missing(self):
        proc = _run_cli(
            ["--fake"],
            doc_text="# 小接口\n\nPOST /tiny\n",
            env_overrides={"INTERFACETESTER_AI_SANDBOX": "", "INTERFACETESTER_AI_BASE_URL": ""},
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_code_4_when_the_document_fails_the_health_check(self):
        """文档体检在先：没有字段表的文档会被 S-10 拒（连切片都不做）。"""
        proc = _run_cli(
            ["--fake"], doc_text="# 巨型接口\n\nPOST /big-endpoint\n\n" + "x" * 200 + "\n"
        )
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)

    def test_code_3_when_the_replay_has_no_cache_entry(self):
        """`--fake` 且 `.ai/cache/` 空 → 传输层报「没准备响应」（也是"预算闸放行"的观测点）。"""
        proc = _run_cli(
            ["--no-doc-check", "--fake"], doc_text="# 小接口\n\nPOST /tiny\n\n字段 id 是数字。\n"
        )
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)

    def test_code_1_when_a_slice_exceeds_the_payload_budget(self):
        """★M-4 的发现：`gen` 路径上预算超限是**片级失败** → 退出码 **1**（不是 5）。

        构造：单切片、无子标题；正文取「单片上限」附近——切片器不拦（≤ 上限），
        但拼上 system prompt 后**超过 payload 预算**（由 `prompts` 拦），且**不重试**。
        """
        from interfacetester_ai.prompts import INPUT_BUDGET_BYTES, INPUT_BUDGET_HEADROOM

        slice_bytes = int(INPUT_BUDGET_BYTES * (1 - INPUT_BUDGET_HEADROOM))
        proc = _run_cli(
            ["--no-doc-check", "--fake"],
            doc_text="# 巨型接口\n\nPOST /big-endpoint\n\n" + "x" * (slice_bytes + 50) + "\n",
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)

    def test_budget_overrun_is_not_the_reserved_code(self):
        """反向断言：保留码 `5` 在 `gen` 路径上**不该出现**（出现即须同步三处文档/判据）。"""
        from interfacetester_ai.prompts import INPUT_BUDGET_BYTES, INPUT_BUDGET_HEADROOM

        slice_bytes = int(INPUT_BUDGET_BYTES * (1 - INPUT_BUDGET_HEADROOM))
        proc = _run_cli(
            ["--no-doc-check", "--fake"],
            doc_text="# 巨型接口\n\nPOST /big-endpoint\n\n" + "x" * (slice_bytes + 50) + "\n",
        )
        self.assertNotEqual(
            proc.returncode,
            5,
            "预算超限返回了 5 —— 说明入口补上了，请同步三处：手册退出码表（5 不再是保留码）、"
            "cli.py 的保留码注释、决策清单 §四 M-4 的结论",
        )
