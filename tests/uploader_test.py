# -*- coding: utf-8 -*-
"""uploader 扩展的行为用例（0918-4 批次：M7）。

M7：`ensure_upload_ready()` 在依赖缺失时直接 `sys.exit(1)`。在 pytest 进程内
`sys.exit` 会把**整个测试会话**杀掉——其余用例的结果全部丢失，用户只看到一个
「进程退出」。项目的 SQL / thrift / loader 三处同类问题都已改成抛异常
（见 `step_sql_request.py:41`、`step_thrift_request.py:66`、`loader.py:445` 的注释），
唯独 uploader 漏改。
"""
import io
import os
import re
import shutil
import sys
import tracemalloc
import unittest
import uuid
from unittest import mock

import requests
from loguru import logger

from interfacetester import Config, InterfaceTester, RunRequest, Step, exceptions, loader
from interfacetester.ext import uploader
from interfacetester.models import MethodEnum, TRequest, TStep


class _UploadCase(InterfaceTester):
    """一个用到 upload 的用例（`request.upload` 会触发 `prepare_upload_step`）。"""

    config = Config("upload fail fast")
    teststeps = [
        Step(RunRequest("upload").post("/post").upload(file="data/file_to_upload"))
    ]


class TestUploaderFailFast(unittest.TestCase):
    """依赖缺失时的失败方式必须是「抛异常」，不是「杀进程」。"""

    def test_ensure_upload_ready_raises_instead_of_exit(self):
        with mock.patch.object(uploader, "UPLOAD_READY", False):
            with self.assertRaises(RuntimeError) as cm:
                uploader.ensure_upload_ready()

        # 提示里要给出可执行的安装方式，而不是只说「依赖缺失」
        self.assertIn("interfacetester[upload]", str(cm.exception))

    def test_ensure_upload_ready_is_noop_when_deps_installed(self):
        with mock.patch.object(uploader, "UPLOAD_READY", True):
            self.assertIsNone(uploader.ensure_upload_ready())

    def test_upload_step_error_does_not_exit_process(self):
        """端到端：依赖缺失时跑 upload 用例只让该用例报错，进程不会被杀掉。

        NOTICE: `assertRaises(RuntimeError)` 本身就排除了 `SystemExit`——
        后者不是 `RuntimeError` 的子类，若仍走 `sys.exit(1)` 本用例会以
        `SystemExit: 1` 失败（在 pytest 里表现为整个会话被中断）。
        """
        with mock.patch.object(uploader, "UPLOAD_READY", False):
            with self.assertRaises(RuntimeError):
                _UploadCase().test_start()


class TestBatch0919_24UploadAliasAndBytesPaths(unittest.TestCase):
    r"""批次 6 / **M4**（别名撞车）+ **M8**（bytes 路径）。

    ## M4（静默错值）

    ```text
    upload: {'m_upload_0': 'AAA', '文件名': 'BBB'}
      -> 'm_upload_0' 是合法变量名 → 引用 $m_upload_0
      -> '文件名' 不能当变量名 → 生成别名 m_upload_0（当时"没人用"）→ **也引用 $m_upload_0**
      -> 实测 multipart 里 name="m_upload_0" 发出去的是 **BBB**（期望 AAA），**零告警**
    ```

    根因：别名只避让 `step_variables` 与已生成的别名，而 upload 的**值**是之后才注入
    `step_variables` 的 —— 字段名自己就是最该避让的那一类。

    ## M8（bytes 路径）

    ```text
    相对路径 b"a.bin"  -> TypeError: Can't mix strings and bytes in path components（与上传无关的报错）
    绝对路径 b"/tmp/x" -> filename="b'x'"（repr 当文件名，请求带损坏的 multipart 头）
    ```
    """

    def setUp(self):
        self._ready_patch = mock.patch.object(uploader, "UPLOAD_READY", True)
        self._ready_patch.start()

    def tearDown(self):
        self._ready_patch.stop()

    @staticmethod
    def _plan(upload):
        """走真实入口 `prepare_upload_step`，返回 (表达式, {字段名: 实际发出的值})。"""
        step = TStep(name="upload", request=TRequest(method=MethodEnum.POST, url="/post"))
        step.request.upload = dict(upload)
        step_variables = {}
        uploader.prepare_upload_step(step, step_variables, {})
        expression = step_variables["m_encoder"]
        pairs = re.findall(r"([^,()\s]+)=\$([A-Za-z_0-9]+)", expression)
        body = uploader.multipart_encoder(
            **{name: step_variables.get(var) for name, var in pairs}
        ).read().decode("utf-8")
        sent = {}
        for part in body.split("--"):
            if "name=" not in part:
                continue
            head, _, value = part.partition("\r\n\r\n")
            name = re.search(r'name="([^"]*)"', head).group(1)
            sent[name] = value.rstrip("\r\n")
        return expression, sent

    # ------------------------------------------------------------------ M4
    def test_field_name_m_upload_0_does_not_collide_with_a_generated_alias(self):
        """字段名恰好叫 `m_upload_0` 时，两个字段必须**各发各的值**。"""
        expression, sent = self._plan({"m_upload_0": "AAA", "文件名": "BBB"})

        self.assertEqual(sent["m_upload_0"], "AAA", f"字段值发错了：{sent} / {expression}")
        self.assertEqual(sent["文件名"], "BBB", f"字段值发错了：{sent} / {expression}")

    def test_alias_avoids_every_upload_field_name(self):
        """别名要避开**所有** upload 字段名（换顺序也一样）。"""
        for upload in (
            {"m_upload_0": "AAA", "文件名": "BBB"},
            {"文件名": "BBB", "m_upload_0": "AAA"},
            {"m_upload_1": "AAA", "中文一": "B1", "中文二": "B2"},
        ):
            with self.subTest(upload=upload):
                _expression, sent = self._plan(upload)
                self.assertEqual(sent, upload, "有字段发出了别的字段的值（静默错值）")

    # ------------------------------------------------------------------ M8
    def test_relative_bytes_path_matches_the_string_behaviour(self):
        """反向护栏 + 等价性：bytes 路径与 str 路径的行为必须**完全一致**。"""
        as_str = self._value_sent("real.bin")
        as_bytes = self._value_sent(b"real.bin")

        self.assertEqual(
            as_bytes,
            as_str,
            "bytes 路径与 str 路径的行为不一致（修复前 bytes 会抛与上传无关的 TypeError）",
        )

    def test_absolute_bytes_path_is_uploaded_with_a_clean_filename(self):
        """绝对 bytes 路径要真的上传，且文件名不能是 `b'…'` 这种 repr。"""
        import os as _os

        tmp_dir = _os.path.join(_os.getcwd(), "logs", "tmp_upload_bytes")
        _os.makedirs(tmp_dir, exist_ok=True)
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        target = _os.path.join(tmp_dir, "real.bin")
        with open(target, "wb") as f:
            f.write(b"CONTENT-BYTES")

        body = uploader.multipart_encoder(file=target.encode("utf-8")).read().decode("utf-8")

        self.assertIn('filename="real.bin"', body)
        self.assertNotIn("b'real.bin'", body, "bytes 的 repr 被当成了文件名")
        self.assertIn("CONTENT-BYTES", body)

    def _value_sent(self, value):
        """把值当 upload 字段发出去，返回报文里的形态（用于比对 str / bytes 两条路径）。"""
        body = uploader.multipart_encoder(field=value).read().decode("utf-8")
        return 'filename="' in body, "TypeError" not in body


class TestMultipartEncoderFieldValues(unittest.TestCase):
    """0918-8 / L24：`upload` 里的非字符串值不能变成看不懂的 TypeError。

    修复前 `multipart_encoder` 无条件调 `os.path.isabs(value)`，于是
    `upload: {field: 123}` 抛的是 `TypeError: ... not int` 之类，
    错误信息完全不涉及 upload，用户只能猜是哪个字段写错了。
    """

    def setUp(self):
        self._ready_patch = mock.patch.object(uploader, "UPLOAD_READY", True)
        self._ready_patch.start()

    def tearDown(self):
        self._ready_patch.stop()

    def _body(self, **fields):
        encoder = uploader.multipart_encoder(**fields)
        return encoder.read().decode("utf-8")

    def test_int_field_is_sent_as_plain_field(self):
        body = self._body(field1=123)

        self.assertIn('name="field1"', body)
        self.assertIn("123", body)
        self.assertNotIn("filename=", body)

    def test_bool_and_float_fields_are_sent_as_plain_fields(self):
        body = self._body(flag=True, ratio=1.5)

        self.assertIn('name="flag"', body)
        self.assertIn("True", body)
        self.assertIn("1.5", body)

    def test_empty_value_becomes_empty_field(self):
        """YAML 里的 `field:` 是 None → 空表单字段（与写空字符串一致）。"""
        body = self._body(note=None)

        self.assertIn('name="note"', body)

    def test_container_value_warns_but_is_not_dropped(self):
        """容器类型几乎肯定是写错了：告警，但也不能静默丢掉。"""
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            body = self._body(tags=["a", "b"])
        finally:
            logger.remove(sink_id)

        self.assertIn('name="tags"', body)
        logs = "\n".join(str(message) for message in messages)
        self.assertIn("tags", logs)
        self.assertIn("container", logs)

    def test_real_file_path_is_still_uploaded_as_file(self):
        """回归：真正的文件路径必须照旧按「文件字段」发送（带 filename 与文件内容）。"""
        # NOTICE: 不用 tempfile.TemporaryDirectory/mkdtemp——受限环境（沙箱）下它建出的
        # 目录 ACL 会让随后的 open()/清理直接 PermissionError；
        # logs/ 已被 .gitignore 覆盖，用完自行清理（与其它测试文件的 _tmp_dir 同款）。
        tmp_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "logs",
            f"tmp_upload_{uuid.uuid4().hex[:8]}",
        )
        os.makedirs(tmp_dir)
        file_path = os.path.join(tmp_dir, "hello.txt")
        try:
            with open(file_path, "wb") as f:
                f.write(b"file-content")

            with mock.patch.object(
                uploader,
                "filetype",
                mock.Mock(guess=lambda path: None),
                create=True,  # 本机没装 filetype，模块里没有这个属性
            ):
                body = self._body(doc=file_path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertIn('name="doc"; filename="hello.txt"', body)
        self.assertIn("file-content", body)

    def test_relative_path_field_that_does_not_exist_is_plain_field(self):
        """不存在的相对路径（普通字符串）仍然按普通字段发送——既有行为不变。"""
        body = self._body(field1="value1")

        self.assertIn('name="field1"', body)
        self.assertIn("value1", body)
        self.assertNotIn("filename=", body)


class TestUploadMissingPathWarns(unittest.TestCase):
    """0919-4 / L37：形似路径但文件不存在时，不能静默当普通字段发出去。

    修复前 `multipart_encoder` 对「路径形态但 isfile 为假」的值原样当普通字段发出，
    零告警——路径打错一个字母 = 请求照发、可能 200、用例通过（静默假通过）。
    """

    def setUp(self):
        self._ready_patch = mock.patch.object(uploader, "UPLOAD_READY", True)
        self._ready_patch.start()

    def tearDown(self):
        self._ready_patch.stop()

    def _body_and_warnings(self, **fields):
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            body = uploader.multipart_encoder(**fields).read().decode("utf-8")
        finally:
            logger.remove(sink_id)
        return body, "\n".join(str(message) for message in messages)

    def test_missing_absolute_path_warns(self):
        """绝对路径不存在 → 告警，且告警里能看到被检查的路径与字段名。"""
        missing = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "logs",
            f"definitely_missing_{uuid.uuid4().hex[:8]}.png",
        )
        body, logs = self._body_and_warnings(doc=missing)

        self.assertIn('name="doc"', body)
        self.assertNotIn("filename=", body)
        self.assertIn("doc", logs)
        self.assertIn("definitely_missing_", logs)

    def test_missing_relative_path_with_separator_warns(self):
        """相对路径（含分隔符）不存在 → 告警（多项目/typo 场景）。"""
        body, logs = self._body_and_warnings(doc="data/typo_name.png")

        self.assertIn('name="doc"', body)
        self.assertNotIn("filename=", body)
        self.assertIn("doc", logs)
        self.assertIn("typo_name", logs)

    def test_missing_pathlib_path_warns(self):
        """`pathlib.Path` 一定是「路径意图」，不存在也要告警。"""
        from pathlib import Path

        body, logs = self._body_and_warnings(doc=Path("data") / "no_such_file.txt")

        self.assertNotIn("filename=", body)
        self.assertIn("no_such_file", logs)

    def test_plain_string_value_does_not_warn(self):
        """反向护栏：裸字符串（合法混写的普通字段）不告警——判据必须收窄。"""
        body, logs = self._body_and_warnings(field1="value1", md5="123")

        self.assertIn('name="field1"', body)
        self.assertEqual("", logs)

    def test_existing_file_does_not_warn(self):
        """真实存在的文件照旧按文件字段发送，且不告警（不误伤合法路径）。"""
        tmp_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "logs",
            f"tmp_upload_{uuid.uuid4().hex[:8]}",
        )
        os.makedirs(tmp_dir)
        file_path = os.path.join(tmp_dir, "hello.txt")
        try:
            with open(file_path, "wb") as f:
                f.write(b"file-content")

            with mock.patch.object(
                uploader,
                "filetype",
                mock.Mock(guess=lambda path: None),
                create=True,
            ):
                body, logs = self._body_and_warnings(doc=file_path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertIn('name="doc"; filename="hello.txt"', body)
        self.assertIn("file-content", body)
        self.assertEqual("", logs)


def _upload_step(upload_dict):
    """构造一个带 upload 字段的请求 step（用于 prepare_upload_step 的直调测试）。"""
    step = TStep(name="upload", request=TRequest(method=MethodEnum.POST, url="/post"))
    step.request.upload = dict(upload_dict)
    return step


class TestPrepareUploadStepVariableCollisions(unittest.TestCase):
    """0919-4 / L38 & L39：upload 键名对步骤变量的静默污染。

    - L38：upload 键与既有变量同名且值不同 → 变量被静默覆盖，其它引用拿到新值；
    - L39：键名 `m_encoder` 撞上机制保留名 → 字段值被整段覆盖（必须报错）。
    """

    def setUp(self):
        self._ready_patch = mock.patch.object(uploader, "UPLOAD_READY", True)
        self._ready_patch.start()

    def tearDown(self):
        self._ready_patch.stop()

    def _warnings_of(self, step, step_variables):
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            uploader.prepare_upload_step(step, step_variables, {})
        finally:
            logger.remove(sink_id)
        return "\n".join(str(message) for message in messages)

    def test_upload_key_overwriting_variable_warns(self):
        step = _upload_step({"token": "data/file_to_upload"})
        step_variables = {"token": "secret-token-value"}
        logs = self._warnings_of(step, step_variables)

        self.assertIn("token", logs)
        self.assertIn("secret-token-value", logs)
        # 行为保持不变：仍然覆盖，保证 upload 字段拿到正确的值
        self.assertEqual(step_variables["token"], "data/file_to_upload")
        self.assertEqual(
            step_variables["m_encoder"], "${multipart_encoder(token=$token)}"
        )

    def test_upload_key_referencing_same_variable_stays_silent(self):
        """`upload: {file: $file}` 是合法写法：同名同值，不得告警。"""
        step = _upload_step({"file": "$file"})
        step_variables = {"file": "data/file_to_upload"}
        logs = self._warnings_of(step, step_variables)

        self.assertEqual("", logs)
        self.assertEqual(step_variables["file"], "data/file_to_upload")

    def test_m_encoder_upload_key_raises(self):
        step = _upload_step({"m_encoder": "some-value"})
        with self.assertRaises(exceptions.ParamsError) as cm:
            uploader.prepare_upload_step(step, {}, {})

        self.assertIn("m_encoder", str(cm.exception))

    def test_preexisting_m_encoder_variable_warns(self):
        step = _upload_step({"file": "data/f.txt"})
        step_variables = {"m_encoder": "user-defined"}
        logs = self._warnings_of(step, step_variables)

        self.assertIn("m_encoder", logs)
        self.assertIn("user-defined", logs)
        self.assertTrue(step_variables["m_encoder"].startswith("${multipart_encoder("))

    def test_no_warning_when_no_collision(self):
        step = _upload_step({"file": "data/f.txt"})
        step_variables = {"other": 1}
        logs = self._warnings_of(step, step_variables)

        self.assertEqual("", logs)


def _real_file(name="probe_upload.bin") -> str:
    """在 `logs/` 下造一个真实文件，返回**绝对路径**（避开项目根解析）。"""
    directory = os.path.join(os.getcwd(), "logs")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{uuid.uuid4().hex[:8]}_{name}")
    with open(path, "wb") as f:
        f.write(b"probe-file-bytes")
    return path


def _resolve_encoder(step_variables):
    """把 `$m_encoder` 真正求值出来（= 运行时那一步），返回 multipart 编码器。"""
    from interfacetester import parser

    return parser.parse_data(
        step_variables["m_encoder"],
        step_variables,
        {"multipart_encoder": uploader.multipart_encoder},
    )


class TestPrepareUploadStepBodyConflicts(unittest.TestCase):
    """0920 / **缺陷 1 的运行期兜底**：upload 接管请求体前，`data`/`json` 不能"静默没了"。

    生成期闸门（`make.ensure_upload_body_is_unambiguous`）管的是**手写 YAML**；
    运行期还有两条不经过生成期的入口（Python API 直接构造 `TStep`、被引用用例改写
    request），那里原本是**零信号**地丢掉请求体：

    - `data` 已有真值 → 被 `step.request.data = "$m_encoder"` 整段覆盖；
    - `json` 已有真值 → `data` 变成编码器后 requests 的
      `if not data and json is not None` 走**假分支**，json 从不出现。
    """

    def setUp(self):
        self._ready_patch = mock.patch.object(uploader, "UPLOAD_READY", True)
        self._ready_patch.start()

    def tearDown(self):
        self._ready_patch.stop()

    def _warnings_of(self, step, step_variables=None):
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            uploader.prepare_upload_step(step, step_variables or {}, {})
        finally:
            logger.remove(sink_id)
        return "\n".join(str(message) for message in messages)

    def test_json_and_upload_raises_instead_of_silently_dropping_json(self):
        step = _upload_step({"file": "a.txt"})
        step.request.req_json = {"from_json": True}

        with self.assertRaises(exceptions.ParamsError) as ctx:
            uploader.prepare_upload_step(step, {}, {})

        message = str(ctx.exception)
        self.assertIn("json", message)
        self.assertIn("upload", message)
        self.assertIn("静默丢弃", message)

    def test_existing_data_warns_before_being_overwritten(self):
        step = _upload_step({"file": "a.txt"})
        step.request.data = {"note": "hello"}

        logs = self._warnings_of(step)

        self.assertIn("note", logs)
        self.assertIn("upload", logs)
        # 行为不变：仍然由 upload 机制接管请求体
        self.assertEqual(step.request.data, "$m_encoder")

    def test_clean_upload_step_stays_silent(self):
        """反向护栏：正常的 upload 步骤不许产生噪音告警。"""
        step = _upload_step({"file": "a.txt"})

        self.assertEqual("", self._warnings_of(step))
        self.assertEqual(step.request.data, "$m_encoder")

    def test_empty_data_does_not_warn(self):
        """`data` 为空值（`""`/`{}`）时 requests 也当"没写"，不告警。"""
        for empty in ("", {}):
            with self.subTest(data=empty):
                step = _upload_step({"file": "a.txt"})
                step.request.data = empty
                self.assertEqual("", self._warnings_of(step))


class TestBatch0920FileObjectUploadValue(unittest.TestCase):
    """0920 批次 4 / **N25**：`upload` 的值是**文件对象**时要按文件发，不能发 `repr`。

    ## 修复前的现场（`.tmp_audit/n25_check.py`）

    文件对象掉进了"非路径标量"兜底分支 → `fields_dict[key] = value` →
    编码器 `f"{value}\\r\\n"` → 请求体里是

    ```text
    Content-Disposition: form-data; name="file"
    <_io.BufferedReader name='D:\\…\\payload.txt'>        ← repr 字符串，92 字节
    ```

    **文件内容从未离开进程**，而且零告警（那条兜底只打 DEBUG 日志）。
    编码器其实**本来就支持文件对象**（`_current_handle` / `_part_length` 都专门处理了），
    只是这条入口没接上；API 用户按 `requests` 的习惯传 `open(...)` 就会静默发错东西。
    """

    def setUp(self):
        self._ready_patch = mock.patch.object(uploader, "UPLOAD_READY", True)
        self._ready_patch.start()
        self._path = _real_file("file_object_probe.txt")
        with open(self._path, "wb") as f:
            f.write(b"HELLO-PAYLOAD")

    def tearDown(self):
        self._ready_patch.stop()
        try:
            os.remove(self._path)
        except OSError:
            pass

    def test_opened_file_object_sends_its_content(self):
        with open(self._path, "rb") as handle:
            body = uploader._build_multipart_encoder({"file": handle}).body.decode(
                "utf-8", "replace"
            )

        self.assertIn("HELLO-PAYLOAD", body, "文件内容没有出现在请求体里")
        self.assertNotIn("_io.", body, "请求体里出现的是文件对象的 repr（N25）")
        self.assertIn(
            f'filename="{os.path.basename(self._path)}"',
            body,
            "应当用文件对象的名字作为 multipart 的 filename",
        )

    def test_named_buffer_without_real_path_still_works(self):
        """没有真实路径的 `BytesIO` 也要按文件发（用字段名兜底做 filename）。"""
        body = uploader._build_multipart_encoder(
            {"doc": io.BytesIO(b"STREAM-CONTENT")}
        ).body.decode("utf-8", "replace")

        self.assertIn("STREAM-CONTENT", body)
        self.assertNotIn("BytesIO", body)

    def test_non_file_scalar_is_still_a_plain_field(self):
        """反向护栏：普通标量（int/bool/str）照旧是普通字段，不会被误判成文件。"""
        body = uploader._build_multipart_encoder(
            {"count": 3, "flag": True, "note": "hi"}
        ).body.decode("utf-8", "replace")

        self.assertIn('name="count"', body)
        self.assertIn("3", body)
        self.assertIn('name="note"', body)
        self.assertNotIn("filename=", body, "普通字段不该带 filename")


class TestUploadFieldNamesWithOddCharacters(unittest.TestCase):
    """0919-7：upload **字段名**含 `-` / `.` / 空格 / 中文开头时的取值。

    ## 现场（实测，修复前）——报告说「响亮报错，属次要」，实测**结论是反的**

    框架把每个 upload 字段拼成 `<字段名>=$<字段名>` 再放进
    `${multipart_encoder(...)}`，于是字段名被**当成了变量名**：

    | 字段名 | parser 是否认作变量引用 | 修复前实测后果 |
    |---|---|---|
    | `file` / `file_name` / `name文件` | ✅ | 正常 |
    | `file-name` / `file.name` / `file name` | ❌ 被切成 `$file` | 响亮报错，但点名的是**另一个**变量（`file not found`） |
    | `文件名` / `2file` / `文件-名` | ❌ **完全不匹配** | **静默**：值原样留字面量 `$文件名`，**文件根本没上传**（发出的是一个内容为 `$文件名` 的普通文本字段），请求照发、用例可能通过 |

    第三行是**零告警的假通过**（实测 body 里留着字面量 `$文件名`），
    比「报错」严重得多——所以它不是「次要」，而且修法不该只是补个告警。

    ## 修法

    字段名不能当变量名时，改用生成的 `$m_upload_N` 别名取值：
    **字段名本身照旧发出去**（这是接口要求的），只有取值的引用换成合法变量名。
    字段名能当变量名时一切照旧（逐字不变）。
    """

    def setUp(self):
        self._ready_patch = mock.patch.object(uploader, "UPLOAD_READY", True)
        self._ready_patch.start()
        self._files = []

    def tearDown(self):
        self._ready_patch.stop()
        for path in self._files:
            try:
                os.remove(path)
            except OSError:
                pass

    def _prepare(self, upload):
        step = _upload_step(upload)
        step_variables = {}
        uploader.prepare_upload_step(step, step_variables, {})
        return step, step_variables

    def _body_of(self, step_variables) -> str:
        encoder = _resolve_encoder(step_variables)
        return encoder.body.decode("utf-8", errors="replace")

    def _real(self) -> str:
        path = _real_file()
        self._files.append(path)
        return path

    def test_plain_field_name_expression_is_unchanged(self):
        """反向护栏：能当变量名的字段名**逐字保持**既有表达式（零变化）。"""
        _step, step_variables = self._prepare({"file": "data/file_to_upload"})

        self.assertEqual(
            step_variables["m_encoder"], "${multipart_encoder(file=$file)}"
        )

    def test_cjk_prefixed_name_is_still_referenceable(self):
        """`\\w` 含中文，所以「以 ASCII 字母开头 + 中文」是合法的变量名，照旧走 `$名字`。"""
        _step, step_variables = self._prepare({"name文件": "data/f.bin"})

        self.assertEqual(
            step_variables["m_encoder"], "${multipart_encoder(name文件=$name文件)}"
        )

    def test_dashed_field_name_now_resolves_the_value(self):
        """`file-name`：修复前只有一句点错名的 `VariableNotFound`，现在值真的被替换。"""
        path = self._real()
        _step, step_variables = self._prepare({"file-name": path})

        self.assertIn("$m_upload_0", step_variables["m_encoder"])
        self.assertIn("file-name=", step_variables["m_encoder"])
        body = self._body_of(step_variables)
        self.assertIn("file-name", body)
        self.assertIn(os.path.basename(path), body)

    def test_dotted_and_spaced_field_names_resolve_too(self):
        for key in ("file.name", "file name"):
            with self.subTest(key=key):
                path = self._real()
                _step, step_variables = self._prepare({key: path})

                self.assertIn(os.path.basename(path), self._body_of(step_variables))

    def test_cjk_field_name_now_uploads_instead_of_silently_sending_a_literal(self):
        """**本批最重的一条**：中文开头的字段名修复前是静默假通过。

        修复前实测：body 里留着字面量 `$文件名`，文件字节**根本没进请求体**，
        而且零告警（`multipart_encoder` 把它当普通文本字段发了出去）。
        """
        path = self._real()
        _step, step_variables = self._prepare({"文件名": path})
        body = self._body_of(step_variables)

        self.assertIn("文件名", body, "字段名必须原样保留（接口要的就是它）")
        self.assertIn(
            os.path.basename(path), body, "文件没有被上传——值没被替换（修复前的静默假通过）"
        )
        self.assertNotIn(
            "$文件名", body, "body 里还留着字面量 `$文件名`，说明取值引用没生效"
        )

    def test_digit_leading_field_name_resolves(self):
        path = self._real()
        _step, step_variables = self._prepare({"2file": path})

        self.assertIn(os.path.basename(path), self._body_of(step_variables))

    def test_alias_avoids_colliding_with_existing_variables(self):
        """反向护栏：别名不得覆盖用户已有的同名变量。"""
        step = _upload_step({"文件名": "data/f.bin"})
        step_variables = {"m_upload_0": "user-value"}

        uploader.prepare_upload_step(step, step_variables, {})

        self.assertEqual(step_variables["m_upload_0"], "user-value")
        self.assertIn("$m_upload_1", step_variables["m_encoder"])

    def test_each_odd_field_gets_its_own_alias(self):
        path = self._real()
        _step, step_variables = self._prepare({"文件名": path, "file-name": path})

        self.assertIn("$m_upload_0", step_variables["m_encoder"])
        self.assertIn("$m_upload_1", step_variables["m_encoder"])
        body = self._body_of(step_variables)
        self.assertIn("文件名", body)
        self.assertIn("file-name", body)

    def test_unrepresentable_field_name_raises_with_the_name(self):
        """字段名含会改变参数切分/表达式匹配的字符 → 响亮报错而不是静默发错。"""
        for key in ("a,b", "a=b", "a)b", "a{b", "a]b"):
            with self.subTest(key=key):
                step = _upload_step({key: "data/f.bin"})
                with self.assertRaises(exceptions.ParamsError) as cm:
                    uploader.prepare_upload_step(step, {}, {})

                self.assertIn(repr(key), str(cm.exception))

    def test_surrounding_whitespace_in_the_name_is_normalized_consistently(self):
        """字段名首尾空白：由 `parse_data` 统一 strip，表达式与发出的字段名**一致**。

        NOTICE（实测后订正）：我最初以为首尾空白会让「表达式里的名字」与
        「实际发出的字段名」不一致（那会是又一次静默改错）。实测**不是**：
        `prepare_upload_step` 在拼表达式**之前**会先 `parse_data(step.request.upload)`
        走一遍，而 `parse_string` 会 `strip(" \\t")` 掉键名 ——
        所以 `"a b "` / `" a b"` / `"a b"` 三种写法产出的表达式与字段名都是 `a b`，
        行为自洽。这条把它钉住，免得以后有人在表达式侧再 strip 一次、造成两处不一致。
        """
        path = self._real()
        for key in ("a b ", " a b", "a b"):
            with self.subTest(key=key):
                _step, step_variables = self._prepare({key: path})

                self.assertIn("a b=$m_upload_0", step_variables["m_encoder"])
                self.assertIn('name="a b"', self._body_of(step_variables))

    def test_repo_style_upload_still_produces_the_documented_expression(self):
        """回归：模块 docstring 里的标准用法表达式不变。"""
        step = _upload_step({"file": "data/file_to_upload", "md5": "123"})
        step_variables = {}
        uploader.prepare_upload_step(step, step_variables, {})

        self.assertEqual(
            step_variables["m_encoder"],
            "${multipart_encoder(file=$file, md5=$md5)}",
        )


class TestUploadContentTypeIsNotSilentlyOverwritten(unittest.TestCase):
    """0919-7 / 顺带发现 ①：upload 步骤的 Content-Type 被静默覆盖。

    upload 机制**必须**拥有 Content-Type（multipart 的 boundary 由编码器生成，
    写死的 boundary 会让请求体与头不匹配），所以覆盖本身没有替代方案。
    但「静默覆盖」不行 —— 与 L38 同族：用户在 headers 里写了 Content-Type，
    生成物里却变成另一套，且零信号。
    """

    def setUp(self):
        self._ready_patch = mock.patch.object(uploader, "UPLOAD_READY", True)
        self._ready_patch.start()

    def tearDown(self):
        self._ready_patch.stop()

    def _warnings_of(self, step, step_variables=None):
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            uploader.prepare_upload_step(step, step_variables or {}, {})
        finally:
            logger.remove(sink_id)
        return "\n".join(str(message) for message in messages)

    def test_user_content_type_is_warned_about(self):
        step = _upload_step({"file": "data/f.bin"})
        step.request.headers["Content-Type"] = "application/json"

        logs = self._warnings_of(step)

        self.assertIn("Content-Type", logs)
        self.assertIn("application/json", logs)
        # 行为不变：机制仍然接管（没有替代方案）
        self.assertEqual(
            step.request.headers["Content-Type"],
            "${multipart_content_type($m_encoder)}",
        )

    def test_user_boundary_is_warned_about(self):
        """写死 boundary 的 Content-Type 是最该被点出来的一种。"""
        step = _upload_step({"file": "data/f.bin"})
        step.request.headers["Content-Type"] = "multipart/form-data; boundary=mine"

        logs = self._warnings_of(step)

        self.assertIn("boundary=mine", logs)

    def test_lowercase_header_spelling_is_also_caught(self):
        """HTTP 头名大小写不敏感 —— 写 `content-type` 同样要被告知。"""
        step = _upload_step({"file": "data/f.bin"})
        step.request.headers["content-type"] = "application/json"

        logs = self._warnings_of(step)

        self.assertIn("content-type", logs)

    def test_no_warning_when_user_did_not_set_it(self):
        """反向护栏：用户没写 Content-Type 时不得凭空告警（假报会让告警被无视）。"""
        step = _upload_step({"file": "data/f.bin"})

        self.assertEqual("", self._warnings_of(step))

    def test_no_warning_when_user_already_wrote_the_framework_expression(self):
        """反向护栏：用户已经写了框架表达式（例如照抄 `--save-tests` 产物）时不该告警。"""
        step = _upload_step({"file": "data/f.bin"})
        step.request.headers["Content-Type"] = "${multipart_content_type($m_encoder)}"

        self.assertEqual("", self._warnings_of(step))


# ---------------------------------------------------------------------------
# 0919-12 批次 F（§二.5）：upload 的相对路径基准 = **本用例所属项目**的根
#
# 现场：两个项目 A / B 下各有一个**同名文件**、内容不同；全局「当前已加载」的项目
# 是 B，而正在跑的用例属于 A。
#   · 修复前 `multipart_encoder` 用 `load_project_meta("")` → 解析到 B →
#     **静默上传 B 的文件**（请求照发、可能 200、用例通过）；
#   · 修复后 runner 在 `_setup_runner` 里把自己项目的根绑进编码器 → 解析到 A。
# ---------------------------------------------------------------------------
class TestUploadRelativePathRoot(unittest.TestCase):
    """`runner.root_dir` 必须决定相对路径的基准，而不是「当前已加载的项目根」。"""

    def setUp(self):
        self._ready_patch = mock.patch.object(uploader, "UPLOAD_READY", True)
        self._ready_patch.start()
        self._sys_path_backup = list(sys.path)
        self._projects = []
        self._a = self._make_project("CONTENT-FROM-PROJECT-A")
        self._b = self._make_project("CONTENT-FROM-PROJECT-B")
        # 把全局「当前已加载」的项目切到 B —— 这就是多项目混跑时的现场
        loader.load_project_meta(os.path.join(self._b, "case.yml"))

    def tearDown(self):
        self._ready_patch.stop()
        sys.path[:] = self._sys_path_backup
        # 先把项目定位恢复成「仓库根」，再删临时项目目录——否则后面的用例会拿到
        # 一个已经被删掉的 RootDir（loader 对「无项目标记的路径」会沿用当前 meta）。
        loader.reset_project_meta()
        loader.load_project_meta(os.path.join(os.getcwd(), "tests", "uploader_test.py"))
        for path in self._projects:
            shutil.rmtree(path, ignore_errors=True)

    def _make_project(self, content: str) -> str:
        """在 `logs/`（已 gitignore）下造一个**带 debugtalk.py 的真项目**，含 data/x.txt。"""
        root = os.path.join(os.getcwd(), "logs", f"upload_root_{uuid.uuid4().hex[:8]}")
        os.makedirs(os.path.join(root, "data"), exist_ok=True)
        with open(os.path.join(root, "debugtalk.py"), "w", encoding="utf-8") as f:
            f.write("# 临时项目标记（upload 相对路径基准用例）\n")
        with open(os.path.join(root, "data", "x.txt"), "w", encoding="utf-8") as f:
            f.write(content)
        with open(os.path.join(root, "case.yml"), "w", encoding="utf-8") as f:
            f.write("config:\n  name: probe\n")
        self._projects.append(root)
        return root

    def _runner_in(self, root_dir):
        class _Case(InterfaceTester):
            pass

        _Case.root_dir = root_dir
        _Case.config = Config("upload root case")
        _Case.teststeps = []
        runner = _Case()
        runner._setup_runner()
        return runner

    @staticmethod
    def _body_of(runner) -> str:
        """走 runner 的 parser 求值 `$m_encoder`（= 运行时那一步）。"""
        encoder = runner.parser.parse_data(
            "${multipart_encoder(file=$f)}", {"f": "data/x.txt"}
        )
        return encoder.read().decode("utf-8")

    # ------------------------------------------------------------ 主用例
    def test_relative_path_uses_this_case_project_root(self):
        """A 项目里的用例，必须读到 A 的文件（修复前读到的是 B 的）。"""
        runner = self._runner_in(self._a)

        body = self._body_of(runner)

        self.assertIn("CONTENT-FROM-PROJECT-A", body)
        self.assertNotIn("CONTENT-FROM-PROJECT-B", body)

    def test_non_vacuous_probe_the_two_projects_really_differ(self):
        """非空跑自检：两个项目的同名文件内容不同，否则上面的断言没有信息量。"""
        with open(os.path.join(self._a, "data", "x.txt"), encoding="utf-8") as f:
            content_a = f.read()
        with open(os.path.join(self._b, "data", "x.txt"), encoding="utf-8") as f:
            content_b = f.read()

        self.assertNotEqual(content_a, content_b)
        # 全局确实指向 B —— 前置条件成立
        self.assertEqual(
            os.path.normcase(loader.load_project_meta("").RootDir),
            os.path.normcase(self._b),
        )

    def test_each_runner_binds_its_own_root(self):
        """两个 runner（不同项目）各读各的——绑定是**每个 runner 一份**的。"""
        body_a = self._body_of(self._runner_in(self._a))
        body_b = self._body_of(self._runner_in(self._b))

        self.assertIn("CONTENT-FROM-PROJECT-A", body_a)
        self.assertIn("CONTENT-FROM-PROJECT-B", body_b)

    # ------------------------------------------------------------ 护栏
    def test_public_entry_keeps_the_historical_fallback(self):
        """公开入口 `multipart_encoder(...)` 保留历史回退（仍按已加载的项目根）。

        这条**刻意钉住兼容面**：手工调用该函数时没有「本用例」的概念，
        回退到 `load_project_meta("")` 是文档化的行为（见其 docstring）。
        跑用例时不会走到这里 —— parser 函数表里拿到的是绑定版。
        """
        body = uploader.multipart_encoder(file="data/x.txt").read().decode("utf-8")

        self.assertIn("CONTENT-FROM-PROJECT-B", body)

    def test_binding_does_not_pollute_the_shared_project_meta(self):
        """护栏：绑定只写进 runner 自己的函数表副本，不污染按项目缓存的 meta。

        `project_meta.functions` 是**按项目缓存、被多个 runner 共享**的对象；
        把绑定写进去会让「某个 runner 的根」残留给别的 runner / 别的用例。
        """
        runner = self._runner_in(self._a)

        self.assertNotIn("multipart_encoder", loader.project_meta.functions)
        self.assertIn("multipart_encoder", runner.parser.functions_mapping)

    def test_user_defined_multipart_encoder_is_not_clobbered(self):
        """护栏：`debugtalk.py` 里自定义了同名函数时，框架不顶掉用户的实现。"""
        user_defined = lambda **kwargs: "user-defined"  # noqa: E731
        functions = {"multipart_encoder": user_defined}

        installed = uploader.install_root_bound_multipart_encoder(functions, self._a)

        self.assertFalse(installed)
        self.assertIs(functions["multipart_encoder"], user_defined)

    def test_missing_file_warning_names_the_project_root_used(self):
        """文件不存在时的告警必须把**实际用的基准**打出来（修复前写的是「当前已加载」）。"""
        messages = []
        sink_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            uploader._build_multipart_encoder(
                {"doc": "data/typo.png"}, root_dir=self._a
            ).read()
        finally:
            logger.remove(sink_id)
        logs = "\n".join(str(message) for message in messages)

        self.assertIn(self._a, logs)
        self.assertNotIn(self._b, logs)
        # L37 的行为不能退化：字段名与拼错的路径照旧要点名
        self.assertIn("doc", logs)
        self.assertIn("typo.png", logs)
        self.assertIn("本用例所属项目的根目录", logs)

    def test_absolute_paths_still_work(self):
        """护栏：绝对路径不受绑定影响（`isabs` 分支本来就不看基准）。"""
        absolute = os.path.join(self._b, "data", "x.txt")

        body = (
            uploader._build_multipart_encoder({"file": absolute}, root_dir=self._a)
            .read()
            .decode("utf-8")
        )

        self.assertIn("CONTENT-FROM-PROJECT-B", body)


class TestMultipartHeaderEscaping(unittest.TestCase):
    """批次 C / **L14-①**：multipart 头参数（字段名/文件名）里的 `"` / CR / LF 必须转义。

    ## 修复前的现场（`probe_toolbelt_compare.py`）

    ```text
    字段名 "a\\nb"               → Content-Disposition: form-data; name="a<真实换行>b"   ← 头被拆行
    文件名 na\\r\\nX-Evil: 1"q   → 在 multipart 体内**伪造出一行头**（X-Evil: 1）
    ```

    而被本模块替换掉的 `requests_toolbelt` 两个方向都会转义成 `%22/%0D/%0A` ——
    **自研版本丢掉了这层转义**。字段名形态可端到端复现（YAML 里写 `"a\\nb": v`）；
    文件名形态需要 Linux/macOS 上的非常规文件名（Windows 不允许）。
    """

    def setUp(self):
        self._ready_patch = mock.patch.object(uploader, "UPLOAD_READY", True)
        self._ready_patch.start()

    def tearDown(self):
        self._ready_patch.stop()

    def test_field_name_with_newline_is_escaped(self):
        encoder = uploader.Utf8MultipartEncoder({"a\nb": "V1"})
        body = encoder.body
        head = body.split(b"\r\n\r\n")[0]

        self.assertIn(b'name="a%0Ab"', body, "字段名里的换行没有被转义（L14）")
        # 头里不许出现裸 LF（除行尾的 CRLF 之外）
        self.assertIsNone(
            re.search(rb"[^\r]\n", head),
            f"头被拆行了（出现了裸换行）：{head!r}",
        )

    def test_filename_with_crlf_and_quote_is_escaped(self):
        """文件名里的 CRLF + 引号会被用来**伪造一行头**，必须转义。"""
        encoder = uploader.Utf8MultipartEncoder(
            {
                "file": (
                    'na\r\nX-Evil: 1"q.bin',
                    b"payload",
                    "application/octet-stream",
                )
            }
        )
        body = encoder.body
        head = body.split(b"\r\n\r\n")[0]

        self.assertIn(b'filename="na%0D%0AX-Evil: 1%22q.bin"', body)
        self.assertNotIn(b"X-Evil: 1\"", head, "伪造出来的头没有被消解")
        self.assertIsNone(re.search(rb"[^\r]\n", head), f"头被拆行了：{head!r}")

    def test_normal_names_are_untouched(self):
        """反向护栏：普通字段名/文件名**逐字不变**（不许在正常路径上加转义）。"""
        encoder = uploader.Utf8MultipartEncoder(
            {"file": ("普通 文件名-1.bin", b"x", "application/octet-stream")}
        )
        body = encoder.body

        self.assertIn('name="file"'.encode("utf-8"), body)
        self.assertIn('filename="普通 文件名-1.bin"'.encode("utf-8"), body)


class TestMultipartStreaming(unittest.TestCase):
    """批次 C / **L14-②**：编码器必须**流式**（文件不进内存），且长度/内容与整包一致。

    ## 修复前的现场（`probe_upload_memory.py`，32 MB 文件、tracemalloc）

    ```text
    自研 Utf8MultipartEncoder : 当前 32.0 MB / 峰值 68.0 MB   ← 文件一份 + 整包一份（≈2N）
    requests_toolbelt 构造    : 当前 0.0 MB / 峰值 0.0 MB
    requests_toolbelt 流式读   : 共 32.0 MB / 峰值 3.0 MB
    ```

    而 `upload` extra 的卖点正是「**大文件** multipart」。现在：片段列表 + 按需 `read()`，
    文件字段只留**路径**（长度用 `os.path.getsize`，requests 的 `super_len` 走 `__len__`）。
    """

    def setUp(self):
        self._ready_patch = mock.patch.object(uploader, "UPLOAD_READY", True)
        self._ready_patch.start()
        self._path = _real_file("streaming_probe.bin")

    def tearDown(self):
        self._ready_patch.stop()
        try:
            os.remove(self._path)
        except OSError:
            pass

    def test_length_equals_materialized_body(self):
        """长度不变量：`Content-Length` 用的 `__len__` 必须等于真实字节数。"""
        encoder = uploader.Utf8MultipartEncoder(
            {"file": ("x.bin", self._path, "application/octet-stream")}
        )
        body = encoder.body

        self.assertEqual(len(encoder), len(body), "len(encoder) 与整包字节数不一致")

    def test_chunked_read_equals_whole_body(self):
        """分块读出来的内容必须与整包逐字节相同（小块读取更暴露边界问题）。

        NOTICE: 两次构造会拿到**不同的 boundary**（uuid），所以要先把 boundary 归一化再比。
        """
        first = uploader.Utf8MultipartEncoder(
            {"file": ("x.bin", self._path, "application/octet-stream")}
        )
        expected = first.body

        second = uploader.Utf8MultipartEncoder(
            {"file": ("x.bin", self._path, "application/octet-stream")}
        )
        streamed = b"".join(iter(lambda: second.read(7), b""))

        normalize = lambda body: re.sub(rb"--[0-9a-f]{32}", b"<B>", body)  # noqa: E731
        self.assertEqual(normalize(streamed), normalize(expected))

    def test_file_is_kept_as_a_path_not_read_into_memory(self):
        """机制护栏：**走真实入口**（`_build_multipart_encoder`）时，文件在片段列表里
        必须是**路径字符串**，而不是预读进来的 bytes。

        NOTICE（护栏要打在真实入口上）：直接 `Utf8MultipartEncoder({"file": (n, path, m)})`
        只证明「编码器支持路径」，证明不了「生成器**选择**了传路径」——
        注入验证实测过这一点：把 `_build_multipart_encoder` 改回预读 bytes，
        只测编码器的用例**照样全绿**。所以这里必须从 builder 进。
        """
        encoder = uploader._build_multipart_encoder({"file": self._path})

        path_parts = [part for part in encoder._parts if isinstance(part, str)]
        self.assertEqual(path_parts, [self._path], f"片段列表：{encoder._parts!r}")

    def test_peak_memory_stays_far_below_file_size(self):
        """32 MB 文件流式读完的峰值内存必须远小于文件本身（修复前 ≈2N）。

        NOTICE: 同样从 `_build_multipart_encoder` 进 —— 内存问题出在**生成器的选择**上
        （预读 bytes 再拼整包），不是编码器的接口。
        """
        big = os.path.join(os.getcwd(), "logs", f"{uuid.uuid4().hex[:8]}_big.bin")
        size = 32 * 1024 * 1024
        with open(big, "wb") as f:
            f.write(b"y" * size)
        try:
            encoder = uploader._build_multipart_encoder({"file": big})
            self.assertGreater(len(encoder), size)

            tracemalloc.start()
            base = tracemalloc.get_traced_memory()[0]
            total = 0
            while True:
                chunk = encoder.read(65536)
                if not chunk:
                    break
                total += len(chunk)
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            self.assertEqual(total, len(encoder))
            peak_mb = (peak - base) / 1024 / 1024
            self.assertLess(
                peak_mb,
                8,
                f"流式读取 32 MB 文件的峰值内存 {peak_mb:.2f} MB 太高"
                f"（修复前约 64 MB；期望只占几个读块）",
            )
        finally:
            os.remove(big)

    def test_bytes_style_input_still_produces_identical_bytes(self):
        """反向护栏：老写法（`(filename, <bytes>, mime)`）与路径写法**逐字节一致**。

        （既有用户的 upload 产物不许变：边界名以外的内容必须一模一样。）
        """
        with open(self._path, "rb") as f:
            content = f.read()

        bytes_style = uploader.Utf8MultipartEncoder(
            {"f": "V", "file": ("x.bin", content, "application/octet-stream")}
        ).body
        path_style = uploader.Utf8MultipartEncoder(
            {"f": "V", "file": ("x.bin", self._path, "application/octet-stream")}
        ).body

        normalize = lambda body: re.sub(rb"--[0-9a-f]{32}", b"<B>", body)  # noqa: E731
        self.assertEqual(normalize(bytes_style), normalize(path_style))

    def test_inline_string_content_is_still_written_as_content(self):
        """反向护栏：三元组第二项写成**内容字符串**（不是已存在的文件）时照旧当内容发出。"""
        encoder = uploader.Utf8MultipartEncoder(
            {"f": ("n.txt", "CONTENT-NOT-A-PATH", "text/plain")}
        )

        self.assertIn(b"CONTENT-NOT-A-PATH", encoder.body)

    # ------------------------------------------------ 0920 批次 2 / N10：可回绕
    def test_encoder_is_rewindable(self):
        """**N10**：编码器必须支持 `tell` / `seek(0)` / `seekable`，否则重定向会发空体。

        `requests` 只在请求体支持 `tell` 时才记 `_body_position`，
        也只在 `_body_position is not None` 时才在 307/308 上 `rewind_body`。
        修复前编码器只有 `read`/`__len__`/`__iter__` → `rewindable=False` →
        把**已读完**的编码器原样重发：服务端按 `Content-Length` 等字节，收到 **0**，
        而用例照样绿（实测 `.tmp_audit/up_audit/probe_upload_redirect_e2e.py`）。
        """
        encoder = uploader.Utf8MultipartEncoder({"file": self._path})

        first = encoder.read()

        self.assertGreater(len(first), 0)
        self.assertTrue(encoder.seekable(), "必须声明可 seek")
        self.assertEqual(encoder.tell(), len(first), "tell 应等于已读字节数")
        self.assertEqual(len(encoder), len(first), "len 不随读取推进而变")

        encoder.seek(0)
        self.assertEqual(encoder.tell(), 0)
        second = encoder.read()

        self.assertEqual(
            first,
            second,
            "回绕后再读必须拿到**同样的字节**（修复前第二次是 b'' → 空体）",
        )

    def test_encoder_refuses_arbitrary_seek(self):
        """只支持回到开头：其它偏移**响亮报错**，而不是静默给出错位置。"""
        encoder = uploader.Utf8MultipartEncoder({"file": self._path})

        with self.assertRaises(OSError):
            encoder.seek(10)

    def test_redirect_resends_the_whole_body(self):
        """端到端：307 重定向时服务端必须收到**完整**请求体（不是 0 字节）。

        真实走 `requests` 的重定向链路（`resolve_redirects` → `rewind_body`），
        这是 N10 的原始现场：修复前第二条请求 `Content-Length=185` 却收到 0 字节。
        """
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen = []

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                seen.append((self.path, length, len(body)))
                if self.path.startswith("/upload"):
                    self.send_response(307)
                    self.send_header("Location", "/final")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):  # noqa: A002
                pass

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

        resp = requests.post(
            f"http://127.0.0.1:{port}/upload",
            data=uploader.Utf8MultipartEncoder({"file": self._path}),
            timeout=20,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(seen), 2, f"应当有一次重定向后的重发：{seen}")
        for path, declared, received in seen:
            self.assertEqual(
                declared,
                received,
                f"{path} 声明 {declared} 字节却只收到 {received} —— 重定向重发了空体（N10）",
            )

