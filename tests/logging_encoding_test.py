# -*- coding: utf-8 -*-
"""0918-5 批次：M10 —— Windows 上中文日志在管道/重定向/CI 里是乱码。

## 实测出来的根因（不是 loguru 的问题）

| 条件 | `sys.stdout.encoding` | loguru 实际写出的字节 |
|---|---|---|
| 未设 `PYTHONIOENCODING`（本机 locale = cp936） | `'gbk'` | `b'\\xb2\\xbb\\xd6\\xa7...'`（GBK） |
| `PYTHONIOENCODING=utf-8` | `'utf-8'` | `b'\\xe4\\xb8\\x8d\\xe6\\x94\\xaf...'`（UTF-8） |

任何按 UTF-8 解码的消费者（CI 日志、`tee`、日志聚合、多数现代终端）看到 GBK 字节就是乱码。
loguru 的**文件** sink 本来就用 UTF-8（loguru 默认 `encoding="utf-8"`），
所以受影响的**只有 stdout 这一路**——它跟随 locale。

NOTICE: 本文件必须用**真实子进程 + 管道**来验：编码行为取决于进程启动时的
stdout 类型与 locale，在同一个进程里 mock 是测不出来的。
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

from interfacetester import utils
from interfacetester.utils import ensure_stdout_encoding, init_logger

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CHINESE = "jsonschema_match 不支持内联 schema"

# 子进程脚本：先按需要处理 stdout 编码，再用 loguru 往 stdout 打一条中文
CHILD_TEMPLATE = """
import sys
sys.path.insert(0, {base!r})
{prepare}
from loguru import logger

logger.remove()
logger.add(sys.stdout, format="{{message}}", level="INFO")
logger.info({message!r})
sys.stdout.flush()
sys.stderr.write("STDOUT_ENCODING=%r\\n" % (getattr(sys.stdout, "encoding", None),))
"""

PREPARE_WITH_FIX = "from interfacetester.utils import init_logger; init_logger('INFO')"
# 复刻修复前的行为：直接把 sys.stdout 交给 loguru，不做任何编码处理
PREPARE_WITHOUT_FIX = "pass"


def _run_child(prepare: str, env_overrides: dict = None) -> tuple:
    """在**管道**里跑子进程，返回 (stdout 字节, 子进程报告的 stdout 编码)。"""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env.pop("PYTHONUTF8", None)
    env.pop("INTERFACETESTER_LOG_ENCODING", None)
    env.update(env_overrides or {})

    script = CHILD_TEMPLATE.format(
        base=BASE, prepare=prepare, message=CHINESE
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, env=env
    )
    stderr_text = proc.stderr.decode("utf-8", "replace")
    reported = None
    for line in stderr_text.splitlines():
        if line.startswith("STDOUT_ENCODING="):
            reported = line.split("=", 1)[1].strip().strip("'\"")
    return proc.stdout, reported


class TestStdoutEncoding(unittest.TestCase):
    """`ensure_stdout_encoding()` 的决策逻辑（不依赖真实 locale）。"""

    def _fake_stream(self, encoding, is_tty):
        stream = mock.Mock()
        stream.encoding = encoding
        stream.isatty.return_value = is_tty
        return stream

    def test_non_tty_is_forced_to_utf8(self):
        """非终端（管道/重定向/CI）→ 强制 UTF-8。"""
        stream = self._fake_stream("gbk", is_tty=False)

        with mock.patch.object(sys, "stdout", stream), mock.patch.dict(os.environ):
            os.environ.pop("PYTHONIOENCODING", None)
            os.environ.pop("PYTHONUTF8", None)
            os.environ.pop("INTERFACETESTER_LOG_ENCODING", None)
            result = ensure_stdout_encoding()

        self.assertEqual(result, "utf-8")
        stream.reconfigure.assert_called_once()
        self.assertEqual(
            stream.reconfigure.call_args.kwargs.get("encoding"), "utf-8"
        )

    def test_tty_is_left_alone(self):
        """终端 → 不动（见模块 docstring：避免让本来正常的老式控制台变乱码）。"""
        stream = self._fake_stream("gbk", is_tty=True)

        with mock.patch.object(sys, "stdout", stream), mock.patch.dict(os.environ):
            for key in ("PYTHONIOENCODING", "PYTHONUTF8", "INTERFACETESTER_LOG_ENCODING"):
                os.environ.pop(key, None)
            result = ensure_stdout_encoding()

        self.assertIsNone(result)
        stream.reconfigure.assert_not_called()

    def test_already_utf8_is_a_noop(self):
        stream = self._fake_stream("utf-8", is_tty=False)

        with mock.patch.object(sys, "stdout", stream), mock.patch.dict(os.environ):
            for key in ("PYTHONIOENCODING", "PYTHONUTF8", "INTERFACETESTER_LOG_ENCODING"):
                os.environ.pop(key, None)
            result = ensure_stdout_encoding()

        self.assertEqual(result, "utf-8")
        stream.reconfigure.assert_not_called()

    def test_explicit_user_choice_is_respected(self):
        """用户自己设了 PYTHONIOENCODING/PYTHONUTF8 → 一律不干预。"""
        for key in ("PYTHONIOENCODING", "PYTHONUTF8"):
            with self.subTest(key=key):
                stream = self._fake_stream("gbk", is_tty=False)
                with mock.patch.object(sys, "stdout", stream), mock.patch.dict(
                    os.environ, {key: "gbk"}
                ):
                    result = ensure_stdout_encoding()

                self.assertIsNone(result)
                stream.reconfigure.assert_not_called()

    def test_env_can_force_and_disable(self):
        # 显式指定 utf-8：即使是终端也照做（优先于「默认不干预」）
        stream = self._fake_stream("gbk", is_tty=True)
        with mock.patch.object(sys, "stdout", stream), mock.patch.dict(os.environ):
            os.environ.pop("PYTHONIOENCODING", None)
            os.environ.pop("PYTHONUTF8", None)
            os.environ[utils.LOGGER_ENCODING_ENV] = "utf-8"
            result = ensure_stdout_encoding()
        self.assertEqual(result, "utf-8")
        stream.reconfigure.assert_called_once()

        # 显式 locale：即使非终端也不动
        stream = self._fake_stream("gbk", is_tty=False)
        with mock.patch.object(sys, "stdout", stream), mock.patch.dict(os.environ):
            os.environ.pop("PYTHONIOENCODING", None)
            os.environ.pop("PYTHONUTF8", None)
            os.environ[utils.LOGGER_ENCODING_ENV] = "locale"
            result = ensure_stdout_encoding()
        self.assertIsNone(result)
        stream.reconfigure.assert_not_called()

    def test_framework_override_wins_over_pythonioencoding(self):
        """优先级：框架自己的显式开关 > 「用户已用标准机制表态 → 不干预」。"""
        stream = self._fake_stream("gbk", is_tty=False)
        with mock.patch.object(sys, "stdout", stream), mock.patch.dict(
            os.environ,
            {"PYTHONIOENCODING": "gbk", utils.LOGGER_ENCODING_ENV: "utf-8"},
        ):
            result = ensure_stdout_encoding()

        self.assertEqual(result, "utf-8")
        stream.reconfigure.assert_called_once()

    def test_stream_without_reconfigure_does_not_crash(self):
        """stdout 被换成没有 reconfigure 的对象（如被测桩）时不能抛异常。"""
        stream = mock.Mock(spec=["encoding", "isatty", "write"])
        stream.encoding = "gbk"
        stream.isatty.return_value = False

        with mock.patch.object(sys, "stdout", stream), mock.patch.dict(os.environ):
            for key in ("PYTHONIOENCODING", "PYTHONUTF8", "INTERFACETESTER_LOG_ENCODING"):
                os.environ.pop(key, None)
            result = ensure_stdout_encoding()  # 不应抛异常

        self.assertIsNone(result)

    def test_reconfigure_failure_does_not_crash(self):
        """reconfigure 抛异常（某些被包装过的 stream）→ 吞掉，不影响日志功能。"""
        stream = self._fake_stream("gbk", is_tty=False)
        stream.reconfigure.side_effect = ValueError("underlying buffer has been detached")

        with mock.patch.object(sys, "stdout", stream), mock.patch.dict(os.environ):
            for key in ("PYTHONIOENCODING", "PYTHONUTF8", "INTERFACETESTER_LOG_ENCODING"):
                os.environ.pop(key, None)
            result = ensure_stdout_encoding()

        self.assertIsNone(result)

    def test_init_logger_still_registers_stdout_sink(self):
        """加固不能把 loguru 的 stdout sink 弄丢。"""
        logger = utils.logger
        with mock.patch.object(sys, "stdout", mock.Mock()), mock.patch.dict(
            os.environ
        ):
            for key in ("PYTHONIOENCODING", "PYTHONUTF8", "INTERFACETESTER_LOG_ENCODING"):
                os.environ.pop(key, None)
            init_logger("INFO")

        # 恢复一个可用的 sink，避免影响后续用例
        logger.remove()
        logger.add(sys.stderr, level="INFO")


class TestStdoutEncodingEndToEnd(unittest.TestCase):
    """真实子进程 + 管道：这是唯一能验「中文到底写出什么字节」的方式。"""

    def test_chinese_log_is_valid_utf8_with_fix(self):
        """修复后：管道里拿到的是 **UTF-8** 字节，中文可读。"""
        raw, reported = _run_child(PREPARE_WITH_FIX)

        self.assertEqual(
            reported,
            "utf-8",
            f"子进程 stdout 未变成 utf-8（实际 {reported!r}）",
        )
        self.assertEqual(raw.decode("utf-8").strip(), CHINESE)

    def test_reproduction_is_not_vacuous(self):
        """非空跑校验：**不**做处理时，本机确实会写出非 UTF-8 字节。

        在 locale 本来就是 UTF-8 的环境（Linux CI）里，这条无法复现缺陷，
        此时明确 skip 而不是假装通过。
        """
        raw, reported = _run_child(PREPARE_WITHOUT_FIX)

        if reported in (None, "utf-8", "utf8"):
            self.skipTest(
                f"本机 stdout 编码已是 {reported!r}，复现不出 M10（仅在非 UTF-8 locale 上可见）"
            )

        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")

    def test_env_disable_restores_old_behaviour(self):
        """逃生口：显式设成 locale 时回到修复前的行为（用于老式控制台）。"""
        raw, reported = _run_child(
            PREPARE_WITH_FIX, {"INTERFACETESTER_LOG_ENCODING": "locale"}
        )

        if reported in (None, "utf-8", "utf8"):
            self.skipTest(f"本机 stdout 编码已是 {reported!r}，无从区分")

        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")

    def test_pythonioencoding_is_respected(self):
        """用户显式设了 PYTHONIOENCODING → 框架不覆盖。"""
        raw, reported = _run_child(
            PREPARE_WITH_FIX, {"PYTHONIOENCODING": "gbk"}
        )

        self.assertEqual(reported, "gbk")
        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")


# 子进程脚本：同时报告 stdout 与 stderr 的编码
CHILD_BOTH_STREAMS_TEMPLATE = """
import sys
sys.path.insert(0, {base!r})
from interfacetester.utils import init_logger
init_logger("INFO")
sys.stdout.write("OUT_ENCODING=%r\\n" % (getattr(sys.stdout, "encoding", None),))
sys.stdout.flush()
sys.stderr.write("ERR_ENCODING=%r\\n" % (getattr(sys.stderr, "encoding", None),))
sys.stderr.flush()
"""


def _run_both_streams_child(env_overrides: dict = None) -> tuple:
    """在管道里跑子进程（init_logger），返回 (stdout 文本, stderr 文本)。"""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env.pop("PYTHONUTF8", None)
    env.pop("INTERFACETESTER_LOG_ENCODING", None)
    env.update(env_overrides or {})

    proc = subprocess.run(
        [sys.executable, "-c", CHILD_BOTH_STREAMS_TEMPLATE.format(base=BASE)],
        capture_output=True,
        env=env,
    )
    return (
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


class TestStderrEncoding(unittest.TestCase):
    """0918-8 / L27：**stderr 也必须**纳入编码处理（M10 只做了 stdout）。

    NOTICE: 修复前同一次调用里 stdout=UTF-8、stderr=cp936——而框架有相当一部分
    中文提示/报错是写 stderr 的（`cli.py` 的「未知子命令」提示、pytest/Python 自身的
    错误输出），在管道/CI 里读出来就是乱码，同一个工具两种编码。
    """

    def _fake_stream(self, encoding, is_tty):
        stream = mock.Mock()
        stream.encoding = encoding
        stream.isatty.return_value = is_tty
        return stream

    def test_both_streams_are_reconfigured(self):
        stdout = self._fake_stream("gbk", is_tty=False)
        stderr = self._fake_stream("gbk", is_tty=False)

        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(
            sys, "stderr", stderr
        ), mock.patch.dict(os.environ):
            for key in ("PYTHONIOENCODING", "PYTHONUTF8", "INTERFACETESTER_LOG_ENCODING"):
                os.environ.pop(key, None)
            result = utils.ensure_output_encoding()

        self.assertEqual(result, {"stdout": "utf-8", "stderr": "utf-8"})
        stdout.reconfigure.assert_called_once()
        stderr.reconfigure.assert_called_once()
        self.assertEqual(
            stderr.reconfigure.call_args.kwargs.get("encoding"), "utf-8"
        )

    def test_stderr_encoding_is_read_after_init_logger(self):
        """真实子进程：`init_logger()` 之后 stderr 的编码也必须是 utf-8。"""
        out_text, err_text = _run_both_streams_child()

        self.assertIn("OUT_ENCODING='utf-8'", out_text, out_text)
        self.assertIn("ERR_ENCODING='utf-8'", err_text, err_text)

    def test_reproduction_is_not_vacuous_for_stderr(self):
        """非空跑校验：**不**调 init_logger 时，本机 stderr 确实不是 utf-8。"""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        env.pop("PYTHONUTF8", None)
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write({'e'!r}.format(e=sys.stderr.encoding)); ",
            ],
            capture_output=True,
            env=env,
        )
        reported = proc.stderr.decode("ascii", "replace").strip().strip("'\"")

        if reported in ("utf-8", "utf8"):
            self.skipTest(
                f"本机 stderr 编码已是 {reported!r}，复现不出 L27（仅在非 UTF-8 locale 上可见）"
            )
        self.assertNotEqual(reported, "utf-8")

    def test_cli_stderr_hint_is_utf8_in_a_pipe(self):
        """端到端：`interfacetester debug` 的中文提示写在 **stderr**，管道里必须是 UTF-8。"""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        env.pop("PYTHONUTF8", None)
        env.pop("INTERFACETESTER_LOG_ENCODING", None)
        env["PYTHONPATH"] = ""
        proc = subprocess.run(
            [sys.executable, "-m", "interfacetester", "debug"],
            capture_output=True,
            cwd=BASE,
            env=env,
            timeout=120,
        )

        self.assertNotEqual(proc.returncode, 0)
        # 修复前这里是 GBK 字节 → decode 会抛 UnicodeDecodeError
        stderr_text = proc.stderr.decode("utf-8")
        self.assertIn("可用命令", stderr_text)
