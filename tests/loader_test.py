import importlib
import os
import shutil
import sys
import unittest
import uuid

from loguru import logger

from interfacetester import compat, exceptions, loader


def _tmp_dir(prefix: str) -> str:
    """临时目录放 logs/ 下（已被 .gitignore 覆盖）。

    NOTICE: 不用 `tempfile.mkdtemp()`——它按 0o700 建目录，在受限环境（沙箱/受限令牌）下
    后续 `open()`/`makedirs()` 会 PermissionError 且 `addCleanup` 反而变成噪声。
    这是 0916-8 已记录过的坑，全仓统一走这个写法。
    """
    path = os.path.join(os.getcwd(), "logs", f"tmp_{prefix}_{uuid.uuid4().hex[:8]}")
    os.makedirs(path)
    return path


def _same_path(left, right):
    """Windows 大小写/分隔符不敏感地比较两个路径。"""
    return os.path.normcase(os.path.normpath(os.path.abspath(left))) == os.path.normcase(
        os.path.normpath(os.path.abspath(right))
    )


def _capture_warnings(func, *args, **kwargs):
    """执行 func 并返回 loguru 捕获到的日志文本列表。"""
    messages = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        func(*args, **kwargs)
    finally:
        logger.remove(sink_id)
    return messages


class TestLoader(unittest.TestCase):
    def test_load_testcase_file(self):
        path = "examples/postman_echo/request_methods/request_with_variables.yml"
        testcase_obj = loader.load_testcase_file(path)
        self.assertEqual(
            testcase_obj.config.name, "request methods testcase with variables"
        )
        self.assertEqual(len(testcase_obj.teststeps), 4)

    def test_load_json_file_file_format_error(self):
        json_tmp_file = "tmp.json"
        # create empty file
        with open(json_tmp_file, "w") as f:
            f.write("")

        with self.assertRaises(exceptions.FileFormatError):
            loader._load_json_file(json_tmp_file)

        os.remove(json_tmp_file)

        # create empty json file
        with open(json_tmp_file, "w") as f:
            f.write("{}")

        loader._load_json_file(json_tmp_file)
        os.remove(json_tmp_file)

        # create invalid format json file
        with open(json_tmp_file, "w") as f:
            f.write("abc")

        with self.assertRaises(exceptions.FileFormatError):
            loader._load_json_file(json_tmp_file)

        os.remove(json_tmp_file)

    def test_load_yaml_file_error_message(self):
        """0918-7 / L1：YAML 语法错误的异常信息必须带文件名与错误细节。

        修复前这里抛的是 `raise exceptions.FileFormatError`（**类**而非实例），
        err_msg 只进了日志——上层捕获到的 `str(exception)` 为空，
        YAML 的行号/语法错误细节全丢（旁边 JSON 分支一直是带信息的实例）。
        """
        yaml_tmp_file = "tmp_bad.yaml"
        with open(yaml_tmp_file, "w", encoding="utf-8") as f:
            f.write("config:\n  name: [unclosed\n")

        with self.assertRaises(exceptions.FileFormatError) as ctx:
            loader._load_yaml_file(yaml_tmp_file)
        os.remove(yaml_tmp_file)

        message = str(ctx.exception)
        self.assertIn("YAMLError", message)
        self.assertIn(yaml_tmp_file, message)

    def test_load_yaml_file_uses_safe_loader(self):
        """0919-1 / L32：用例 YAML 只按**数据**解析，不得构造 Python 对象。

        修复前用的是 `yaml.FullLoader`。PyYAML 5.1+ 已经堵掉了 FullLoader 的 RCE 面
        （实测 6.0.3：`!!python/object/apply` / `!!python/object/new` / `!!python/module`
        / `!!python/object` 在 FullLoader 下同样抛 ConstructorError），所以这**不是**
        一个可利用的漏洞；但 FullLoader 仍多认一批标签，实测下面这两类
        **SafeLoader 拒绝、FullLoader 放行**——
        `!!python/name:` 甚至会构造出一个真实的可调用对象引用。

        用例文件可能来自 `hconvert` 导入的外部素材（HAR 是别人录的、Postman 集合是别人给的），
        按最小权限原则，只放行「纯数据」标签。
        """
        for label, content in (
            ("python/name", "a: !!python/name:os.system\n"),
            ("python/tuple", "a: !!python/tuple [1, 2]\n"),
            ("python/object/apply", 'a: !!python/object/apply:os.system ["echo pwned"]\n'),
            ("python/module", "a: !!python/module:os\n"),
        ):
            with self.subTest(tag=label):
                tmp_file = os.path.join(_tmp_dir("yaml_tag"), "case.yml")
                with open(tmp_file, "w", encoding="utf-8") as f:
                    f.write(content)

                with self.assertRaises(exceptions.FileFormatError) as ctx:
                    loader._load_yaml_file(tmp_file)

                self.assertIn("YAMLError", str(ctx.exception))

    def test_load_yaml_file_still_accepts_normal_case_yaml(self):
        """反向护栏：收紧到 SafeLoader 不能把正常用例语法一起收掉。

        锚点/别名/多行/非 ASCII/`${func()}` 字面量都是 SafeLoader 支持的正常数据语法，
        必须照旧加载（仓库内 127 个 YAML 实测全部通过，这里是钉成用例的那一份）。
        """
        content = (
            "config:\n"
            "    name: 中文用例名\n"
            "    base_url: ${get_base_url()}\n"
            "    variables:\n"
            "        anchor: &shared\n"
            "            a: 1\n"
            "            b: '${func(1, 2)}'\n"
            "        alias_use: *shared\n"
            "teststeps:\n"
            "-\n"
            "    name: step\n"
            "    request:\n"
            "        url: /get\n"
            "        method: GET\n"
            "    validate:\n"
            "        - eq: ['status_code', 200]\n"
        )
        tmp_file = os.path.join(_tmp_dir("yaml_ok"), "case.yml")
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(content)

        result = loader._load_yaml_file(tmp_file)
        self.assertEqual(result["config"]["name"], "中文用例名")
        # 锚点与别名必须按 YAML 语义展开（SafeLoader 一样支持）
        self.assertEqual(result["config"]["variables"]["alias_use"], {"a": 1, "b": "${func(1, 2)}"})
        self.assertEqual(result["teststeps"][0]["request"]["url"], "/get")

    def test_load_testcases_bad_filepath(self):
        testcase_file_path = os.path.join(os.getcwd(), "examples/data/demo")
        with self.assertRaises(exceptions.FileNotFound):
            loader.load_testcase_file(testcase_file_path)

    def test_load_csv_file_one_parameter(self):
        csv_file_path = os.path.join(os.getcwd(), "examples/httpbin/user_agent.csv")
        csv_content = loader.load_csv_file(csv_file_path)
        self.assertEqual(
            csv_content,
            [
                {"user_agent": "iOS/10.1"},
                {"user_agent": "iOS/10.2"},
                {"user_agent": "iOS/10.3"},
            ],
        )

    def test_load_csv_file_multiple_parameters(self):
        csv_file_path = os.path.join(os.getcwd(), "examples/httpbin/account.csv")
        csv_content = loader.load_csv_file(csv_file_path)
        self.assertEqual(
            csv_content,
            [
                {"username": "test1", "password": "111111"},
                {"username": "test2", "password": "222222"},
                {"username": "test3", "password": "333333"},
            ],
        )

    def test_load_folder_files(self):
        folder = os.path.join(os.getcwd(), "examples")
        file1 = os.path.join(os.getcwd(), "examples", "test_utils.py")
        file2 = os.path.join(os.getcwd(), "examples", "httpbin", "hooks.yml")

        files = loader.load_folder_files(folder, recursive=False)
        # NOTICE: examples/ 顶层刻意放了入门 demo（httpbin_demo.yml 及其生成的
        # httpbin_demo_test.py，见《小白入门指南》与 docs/deploy.md），
        # 因此非递归加载不是空列表；断言聚焦「非递归只取顶层文件」这条语义。
        for file_path in files:
            self.assertEqual(os.path.dirname(file_path), folder)
        self.assertIn(os.path.join(folder, "httpbin_demo.yml"), files)
        self.assertIn(os.path.join(folder, "httpbin_demo_test.py"), files)

        files = loader.load_folder_files(folder)
        self.assertIn(file2, files)
        self.assertNotIn(file1, files)

        files = loader.load_folder_files("not_existed_foulder", recursive=False)
        self.assertEqual([], files)

        files = loader.load_folder_files(file2, recursive=False)
        self.assertEqual([], files)

    def test_load_custom_dot_env_file(self):
        dot_env_path = os.path.join(os.getcwd(), "examples", "httpbin", "test.env")
        env_variables_mapping = loader.load_dot_env_file(dot_env_path)
        self.assertIn("PROJECT_KEY", env_variables_mapping)
        self.assertEqual(env_variables_mapping["UserName"], "test")
        self.assertEqual(
            env_variables_mapping["content_type"], "application/json; charset=UTF-8"
        )

    def test_load_env_path_not_exist(self):
        dot_env_path = os.path.join(
            os.getcwd(),
            "tests",
            "data",
        )
        env_variables_mapping = loader.load_dot_env_file(dot_env_path)
        self.assertEqual(env_variables_mapping, {})

    def test_locate_file(self):
        with self.assertRaises(exceptions.FileNotFound):
            loader.locate_file(os.getcwd(), "debugtalk.py")

        with self.assertRaises(exceptions.FileNotFound):
            loader.locate_file("", "debugtalk.py")

        start_path = os.path.join(os.getcwd(), "examples", "httpbin")
        self.assertEqual(
            loader.locate_file(start_path, "debugtalk.py"),
            os.path.join(os.getcwd(), "examples", "httpbin", "debugtalk.py"),
        )
        self.assertEqual(
            loader.locate_file("examples/httpbin/", "debugtalk.py"),
            os.path.join(os.getcwd(), "examples", "httpbin", "debugtalk.py"),
        )
        self.assertEqual(
            loader.locate_file("examples/httpbin/", "debugtalk.py"),
            os.path.join(os.getcwd(), "examples", "httpbin", "debugtalk.py"),
        )


class TestDuplicateKeyRejection(unittest.TestCase):
    """批次 A / H1：YAML 里的**重复键**必须响亮报错，不能静默「后者胜」。

    NOTICE（为什么这条护栏必须存在）：PyYAML 对同一个映射里重复出现的键是
    「后者胜、前者静默丢弃」，而键本身都是**认识的字段**——所以
    `warn_unknown_testcase_fields`（只按键名）、`make.ensure_generatable_teststeps`
    与 pydantic 校验（拿到的都是合并后的 dict）**全都不会响**。
    实测后果：一个 step 里写两段 `validate:`，第一段里那条**故意失败**的断言整体蒸发
    → `hmake exit 0`、零告警、`hrun` 报 **1 passed**（假通过）。

    它与 `docs/架构与调用链.md` §7 第 8 条是同一个失效形态，只是发生在更靠前的加载层。
    """

    @staticmethod
    def _write(content: str, prefix: str = "dupkey") -> str:
        tmp_file = os.path.join(_tmp_dir(prefix), "case.yml")
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(content)
        return tmp_file

    def test_duplicate_step_validate_is_rejected(self):
        """真实事故形态：两段 `validate:` → 前一段（含故意失败的断言）会整体消失。"""
        tmp_file = self._write(
            "config:\n"
            "    name: dup\n"
            "teststeps:\n"
            "-\n"
            "    name: step\n"
            "    request:\n"
            "        url: /get\n"
            "        method: GET\n"
            "    validate:\n"
            "        - eq: ['status_code', 999]\n"
            "    validate:\n"
            "        - eq: ['status_code', 200]\n"
        )

        with self.assertRaises(exceptions.FileFormatError) as ctx:
            loader._load_yaml_file(tmp_file)

        message = str(ctx.exception)
        self.assertIn("重复的键", message)
        self.assertIn("validate", message)
        # 两个位置都要点出来（第 9 行与第 11 行）
        self.assertIn("第 9 行", message)
        self.assertIn("第 11 行", message)

    def test_duplicate_nested_request_key_is_rejected(self):
        """嵌套映射（request 里的 headers）同样要判重——不只盯顶层。"""
        tmp_file = self._write(
            "config:\n"
            "    name: dup-nested\n"
            "teststeps:\n"
            "-\n"
            "    name: step\n"
            "    request:\n"
            "        url: /get\n"
            "        headers:\n"
            "            A: '1'\n"
            "        headers:\n"
            "            B: '2'\n",
            prefix="dupkey_nested",
        )

        with self.assertRaises(exceptions.FileFormatError) as ctx:
            loader._load_yaml_file(tmp_file)

        message = str(ctx.exception)
        self.assertIn("headers", message)
        self.assertIn("第 8 行", message)
        self.assertIn("第 10 行", message)

    def test_merge_key_does_not_false_positive(self):
        """反向护栏：`<<` 合并键 + 显式覆盖同名键是**合法** YAML，不得误报。

        `flatten_mapping` 会把锚点里的键插到显式键**前面**，于是 `node.value` 里
        同一个键会出现两次——但按 YAML 语义「显式键覆盖合并键」，这是正常写法。
        判重放在 `flatten_mapping` **之前**就是为了不误伤它（假报会让真报被无视）。
        """
        tmp_file = self._write(
            "config:\n"
            "    name: merge\n"
            "teststeps:\n"
            "-\n"
            "    name: step\n"
            "    request:\n"
            "        <<: &base_req\n"
            "            url: /get\n"
            "            method: GET\n"
            "        method: GET\n",
            prefix="dupkey_merge",
        )

        result = loader._load_yaml_file(tmp_file)
        request = result["teststeps"][0]["request"]
        # 显式键（后面的 method）按 YAML 语义胜出，合并键带来的字段仍在
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["url"], "/get")

    def test_repeated_validators_in_a_list_are_not_duplicate_keys(self):
        """反向护栏：**列表元素**可以重复（两条一样的断言是合法的），只有映射的键不能重复。"""
        tmp_file = self._write(
            "config:\n"
            "    name: same-validators\n"
            "teststeps:\n"
            "-\n"
            "    name: step\n"
            "    request:\n"
            "        url: /get\n"
            "        method: GET\n"
            "    validate:\n"
            "        - eq: ['status_code', 200]\n"
            "        - eq: ['status_code', 200]\n",
            prefix="dupkey_list",
        )

        result = loader._load_yaml_file(tmp_file)
        self.assertEqual(len(result["teststeps"][0]["validate"]), 2)

    def test_repository_yaml_files_are_all_accepted(self):
        """反向护栏：仓库里所有 YAML 用例/夹具在收紧后必须照旧加载。

        （判重只可能拦住「真的写重了」的文件；这条用例同时守住「没有把正常 YAML 收坏」。）

        NOTICE: 只扫**仓库自身的** YAML —— 跳过隐藏目录（`.git`/`.venv`/`.tmp_report`…）
        与仓库约定的临时目录前缀（`tmp_*` / `probe_*`，见 `.gitignore`）。
        那些目录里放着**故意写坏**的探针夹具（例如专门写两段 `validate:` 的重复键样本），
        它们被这条新防线拦住是**期望行为**，不是误伤。
        """
        root = os.getcwd()
        skip_dir_names = {"__pycache__", "logs", "build", "dist", "node_modules"}
        checked = []

        for current_dir, dirs, files in os.walk(root):
            dirs[:] = [
                name
                for name in dirs
                if not name.startswith(".")
                and name not in skip_dir_names
                and not name.startswith(("tmp_", "probe_"))
            ]
            for file_name in files:
                if os.path.splitext(file_name)[1].lower() not in (".yml", ".yaml"):
                    continue
                file_path = os.path.join(current_dir, file_name)
                checked.append(file_path)
                try:
                    loader._load_yaml_file(file_path)
                except exceptions.FileFormatError as ex:
                    # 允许「本来就坏的」夹具存在（例如专门测错误信息的 YAML），
                    # 但绝不能是**重复键**这一类——那说明收紧误伤了正常文件。
                    self.assertNotIn(
                        "重复的键",
                        str(ex),
                        f"{file_path} 被误判为重复键：{ex}",
                    )

        # 扫描面必须真的覆盖到仓库里的用例文件（否则这条护栏会因为「什么都没扫到」而空转）
        checked_paths = [os.path.normcase(os.path.abspath(p)) for p in checked]
        for real_case in (
            "examples/httpbin/basic.yml",
            "examples/postman_echo/request_methods/request_with_functions.yml",
            "examples/soap_xpath/xpath.yml",
            "examples/soap/chain.yml",
            "tests/golden/converters/postman_collection.yml",
        ):
            self.assertIn(
                os.path.normcase(os.path.abspath(real_case)),
                checked_paths,
                f"扫描面没有覆盖到 {real_case}，这条护栏失效了",
            )


class TestBatch0920YamlScalarCoercionWarning(unittest.TestCase):
    """0920 批次 6 / **N35**：YAML 1.1 把用户写的文本**静默改义**时必须可见。

    ## 修复前的现场（`.tmp_audit/n35_check.py`，真 `hmake`）

    ```yaml
    variables: {zipcode: 01234, state: no, count: 0755, enabled: off}
    ```

    读出来是（生成物里也一样，零告警）：

    ```text
    {'zipcode': 668, 'state': False, 'count': 493, 'enabled': False}
    ```

    `01234` 被当**八进制**（→668）、`no`/`off` 被当**布尔**。
    而 `params` / `data` / `json` / `variables` 都是 `Any` 类型 ——
    这些值**原样进生成物**，光看 YAML 根本看不出被改过。

    ## 口径：**告警**，不改解析器

    换成 YAML 1.2 语义会改变**所有既有用例**的解释（`no` 从 False 变回字符串），
    风险远大于收益。本仓一贯做法是"不静默改写用户数据，但要让改写**可见**"，
    所以这里只告警 + 给改法（加引号）。

    NOTICE（**刻意排除 `true`/`false`**）：它们是 YAML 1.2 与 JSON 的标准布尔写法，
    用户写 `flag: true` 就是想要布尔值 —— 对它告警是噪音，而噪音会让真告警被忽略。
    """

    def setUp(self):
        self._sink_messages = []
        sink_id = logger.add(
            self._sink_messages.append, level="WARNING", format="{message}"
        )
        self.addCleanup(logger.remove, sink_id)

    def _load(self, content: str) -> dict:
        tmp_dir = _tmp_dir("yaml_coerce")
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        path = os.path.join(tmp_dir, "case.yml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        result = loader._load_yaml_file(path)
        self._warnings = "\n".join(str(m) for m in self._sink_messages)
        return result

    def test_octal_looking_scalar_warns(self):
        """`01234` / `0755` 会被当八进制 → 必须告警并说明解析成了什么。"""
        self._load("config:\n  name: x\n  variables:\n    zipcode: 01234\n")

        self.assertIn("八进制", self._warnings)
        self.assertIn("zipcode", self._warnings)
        self.assertIn("01234", self._warnings)
        self.assertIn("668", self._warnings, "要写出被改成了什么值")

    def test_yaml11_boolean_words_warn(self):
        """`no` / `off` / `yes` / `on` 会被当布尔 → 必须告警。"""
        self._load(
            "config:\n  name: x\n  variables:\n    state: no\n    enabled: off\n"
        )

        self.assertIn("布尔", self._warnings)
        self.assertIn("state", self._warnings)
        self.assertIn("enabled", self._warnings)

    def test_warning_names_line_numbers(self):
        """告警要能定位到行（否则用户不知道该改哪一行）。"""
        self._load("config:\n  name: x\n  variables:\n    zipcode: 01234\n")

        self.assertIn("第 4 行", self._warnings)

    def test_quoted_values_do_not_warn(self):
        """反向护栏：加了引号 = 用户显式声明成文本，不许告警。"""
        self._load(
            'config:\n  name: x\n  variables:\n    zipcode: "01234"\n    state: "no"\n'
        )

        self.assertNotIn("静默改义", self._warnings)

    def test_normal_scalars_do_not_warn(self):
        """反向护栏：正常数字/字符串不告警（噪音会让真告警被忽略）。"""
        self._load(
            "config:\n  name: x\n  variables:\n"
            "    count: 1234\n    ratio: 1.10\n    city: seoul\n    note: hello\n"
        )

        self.assertNotIn("静默改义", self._warnings)

    def test_json_style_booleans_do_not_warn(self):
        """`true`/`false` 是 YAML 1.2 / JSON 的标准布尔，刻意不告警。"""
        self._load("config:\n  name: x\n  variables:\n    flag: true\n")

        self.assertNotIn("静默改义", self._warnings)

    def test_loading_still_returns_the_coerced_values(self):
        """**行为不变**：本批只加告警，解析结果仍是 YAML 1.1 语义（不偷偷改解析器）。"""
        result = self._load(
            "config:\n  name: x\n  variables:\n    zipcode: 01234\n    state: no\n"
        )

        self.assertEqual(result["config"]["variables"]["zipcode"], 668)
        self.assertIs(result["config"]["variables"]["state"], False)


class TestBatch0920JsonDuplicateKeyRejection(unittest.TestCase):
    """0920 批次 6 / **N41**：JSON 用例的重复键必须与 YAML 侧**同口径**报错。

    YAML 分支有 `_DuplicateKeyRejectingSafeLoader`（批次 A / H1），而 JSON 分支
    一直是裸 `json.load` —— 后者的语义同样是「后者胜、前者静默丢弃」。
    两种受支持的用例格式，一侧有护栏、一侧没有：

    ```json
    {"teststeps": [{"name": "s",
                    "validate": [{"eq": ["status_code", 999]}],
                    "validate": [{"eq": ["status_code", 200]}]}]}
    ```

    第一段（含**故意失败**的断言）整体消失 → `hmake` exit 0、零告警、`hrun` 1 passed
    —— 与 YAML 侧被修掉的那个假通过形态**一字不差**，只是换个输入格式。
    """

    def _write(self, content: str, prefix: str) -> str:
        tmp_file = os.path.join(_tmp_dir(prefix), "case.json")
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(content)
        return tmp_file

    def test_duplicate_validate_key_is_rejected(self):
        path = self._write(
            '{"config": {"name": "x"}, "teststeps": [{"name": "s",'
            ' "validate": [{"eq": ["status_code", 999]}],'
            ' "validate": [{"eq": ["status_code", 200]}]}]}',
            "json_dup",
        )

        with self.assertRaises(exceptions.FileFormatError) as ctx:
            loader._load_json_file(path)

        message = str(ctx.exception)
        self.assertIn("重复的键", message)
        self.assertIn("validate", message)
        self.assertIn("假通过", message, "要说明为什么必须报错")

    def test_duplicate_nested_key_is_rejected(self):
        """嵌套层（request.headers）同样判重——不只盯顶层。"""
        path = self._write(
            '{"config": {"name": "x"}, "teststeps": [{"name": "s",'
            ' "request": {"method": "GET", "url": "/x",'
            ' "headers": {"A": "1"}, "headers": {"B": "2"}}}]}',
            "json_dup_nested",
        )

        with self.assertRaises(exceptions.FileFormatError):
            loader._load_json_file(path)

    def test_normal_json_is_unaffected(self):
        """反向护栏：正常 JSON 照旧加载（不许把合法用例判成重复键）。"""
        path = self._write(
            '{"config": {"name": "x"}, "teststeps": [{"name": "s",'
            ' "validate": [{"eq": ["status_code", 200]}, {"eq": ["status_code", 200]}]}]}',
            "json_ok",
        )

        result = loader._load_json_file(path)

        self.assertEqual(result["config"]["name"], "x")
        # 列表里的重复元素是合法的（只有**映射的键**不能重复）
        self.assertEqual(len(result["teststeps"][0]["validate"]), 2)


class TestCsvEncoding(unittest.TestCase):
    """批次 B / M9：CSV 按 `utf-8-sig` 读，BOM 不许污染第一列的列名。"""

    def test_load_csv_file_with_utf8_bom_does_not_pollute_first_column(self):
        """修复前用的是 `encoding="utf-8"`（仓库里其它三处读取点——`.env`、`cli.main_convert`、
        `converters/*`——早就统一成 `utf-8-sig` 并注明「PowerShell 5.1 / 部分编辑器的 UTF-8
        会写 BOM」，**唯独 CSV 漏了**）。后果不是"某条断言失败"，而是
        `${parameterize(x.csv)}` 这条路径在 `parser.parse_parameters` 里按参数名取键时
        裸 `KeyError: 'username'` → pytest **收集期** exit 2、**整个文件**跑不起来，
        而报错里一个字都没提编码/BOM —— CSV 里明明写着 `username`。

        而 BOM 恰好是 Excel「CSV UTF-8」与 PowerShell `Set-Content -Encoding UTF8` 的默认产物。
        """
        tmp_dir = _tmp_dir("csv_bom")
        csv_path = os.path.join(tmp_dir, "bom.csv")
        with open(csv_path, "wb") as f:
            f.write("\ufeffusername,password\nu1,p1\n".encode("utf-8"))

        rows = loader.load_csv_file(csv_path)

        self.assertEqual(
            rows,
            [{"username": "u1", "password": "p1"}],
            f"BOM 污染了列名：{rows}",
        )

    def test_load_csv_file_without_bom_is_unchanged(self):
        """反向护栏：不带 BOM 的 CSV 逐字不变（`utf-8-sig` 对无 BOM 输入是透明的）。"""
        tmp_dir = _tmp_dir("csv_plain")
        csv_path = os.path.join(tmp_dir, "plain.csv")
        with open(csv_path, "wb") as f:
            f.write("username,password\nu1,p1\n".encode("utf-8"))

        self.assertEqual(
            loader.load_csv_file(csv_path), [{"username": "u1", "password": "p1"}]
        )


class TestProjectMetaCache(unittest.TestCase):
    """`load_project_meta` 的缓存必须以 RootDir 为粒度。

    NOTICE: 修复前只有一个全局 project_meta，加载过一次后传入任何 test_path 都会返回
    第一个项目的 meta（RootDir/.env/debugtalk 函数全是错的），跨项目时尤其明显。
    """

    def setUp(self):
        self.sys_path_backup = list(sys.path)
        loader.reset_project_meta()
        self.httpbin_dir = os.path.join(os.getcwd(), "examples", "httpbin")
        self.postman_echo_dir = os.path.join(os.getcwd(), "examples", "postman_echo")

    def tearDown(self):
        sys.path[:] = self.sys_path_backup
        loader.reset_project_meta()

    def test_load_project_meta_by_root_dir(self):
        httpbin_meta = loader.load_project_meta(self.httpbin_dir)
        self.assertEqual(
            os.path.normcase(httpbin_meta.RootDir), os.path.normcase(self.httpbin_dir)
        )
        self.assertIn("get_httpbin_server", httpbin_meta.functions)

        # 换成另一个项目的路径 → 必须按新路径重新定位，而不是沿用上一个项目的 meta
        postman_echo_meta = loader.load_project_meta(self.postman_echo_dir)
        self.assertEqual(
            os.path.normcase(postman_echo_meta.RootDir),
            os.path.normcase(self.postman_echo_dir),
        )
        self.assertIn("calculate_two_nums", postman_echo_meta.functions)
        self.assertNotIn("get_httpbin_server", postman_echo_meta.functions)

        # 切回原项目：命中 RootDir 缓存，返回同一个对象
        self.assertIs(
            loader.load_project_meta(os.path.join(self.httpbin_dir, "hooks.yml")),
            httpbin_meta,
        )
        # 两个 RootDir 各自缓存一份
        self.assertEqual(len(loader.project_meta_cache), 2)
        self.assertIn(httpbin_meta, list(loader.project_meta_cache.values()))
        self.assertIn(postman_echo_meta, list(loader.project_meta_cache.values()))

    def test_load_project_meta_switches_debugtalk_module(self):
        loader.load_project_meta(self.postman_echo_dir)
        debugtalk_module = importlib.import_module("debugtalk")
        self.assertTrue(hasattr(debugtalk_module, "calculate_two_nums"))
        self.assertEqual(
            os.path.normcase(sys.path[0]), os.path.normcase(self.postman_echo_dir)
        )

        # 切换项目后 import 必须解析到新项目（修复前 sys.modules 里缓存的旧模块会遮蔽新项目）
        loader.load_project_meta(self.httpbin_dir)
        debugtalk_module = importlib.import_module("debugtalk")
        self.assertTrue(hasattr(debugtalk_module, "get_httpbin_server"))
        self.assertFalse(hasattr(debugtalk_module, "calculate_two_nums"))
        self.assertEqual(
            os.path.normcase(sys.path[0]), os.path.normcase(self.httpbin_dir)
        )

    def test_load_project_meta_keeps_loaded_meta_for_projectless_path(self):
        """向上找不到 debugtalk.py 的路径（如 tests/ 下的脚本、仓库根目录）不对应任何项目，
        此时沿用当前已加载的 meta，避免把已加载项目的 RootDir/functions 丢掉。"""
        httpbin_meta = loader.load_project_meta(self.httpbin_dir)
        self.assertIs(
            loader.load_project_meta(
                os.path.join(os.getcwd(), "tests", "loader_test.py")
            ),
            httpbin_meta,
        )

    def test_load_project_meta_reload_forces_rebuild(self):
        httpbin_meta = loader.load_project_meta(self.httpbin_dir)
        reloaded_meta = loader.load_project_meta(self.httpbin_dir, reload=True)
        self.assertIsNot(httpbin_meta, reloaded_meta)
        self.assertEqual(reloaded_meta.RootDir, httpbin_meta.RootDir)
        # reload 后缓存也更新为新对象
        self.assertIs(loader.load_project_meta(self.httpbin_dir), reloaded_meta)

    def test_load_project_meta_with_empty_path(self):
        """`load_project_meta("")` 返回**当前已加载**的 meta（空路径分支的既有语义）。

        NOTICE（0919-12 / 批次 F）：uploader **不再依赖**这条来定位相对路径了——
        `SessionRunner._setup_runner` 会把本用例所属项目的根绑进 `multipart_encoder`。
        这个分支仍然保留给「没有路径信息」的调用方（并作为直接调用
        `multipart_encoder(...)` 时的兼容回退）。
        """
        httpbin_meta = loader.load_project_meta(self.httpbin_dir)
        self.assertIs(loader.load_project_meta(""), httpbin_meta)

        loader.reset_project_meta()
        self.assertEqual(
            os.path.normcase(loader.load_project_meta("").RootDir),
            os.path.normcase(os.getcwd()),
        )


class TestProjectMetaRootDirDefault(unittest.TestCase):
    """0919-16 / 批次 G（§三.6c）：`ProjectMeta.RootDir` 的默认值要**跟随构造时的 cwd**。

    修复前写的是 `RootDir: Text = os.getcwd()` —— 在**类创建时**（import 期）求值一次，
    之后整个进程都固化成那个值。框架内所有正常路径都会覆盖它（`loader` 加载项目时赋值），
    所以日常跑用例看不出问题；**只有「import 之后 `os.chdir()` 过」的进程**
    （第三方 conftest / debugtalk / 把框架当库用的脚本）才会拿到过期的 cwd。

    这是一个**防御性**修复：框架自己全仓 0 处 `os.chdir`，因此**没有**已知的可复现路径；
    测试只能直接验证「默认值语义」这一层。
    """

    def setUp(self):
        self._cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._cwd)

    def test_default_follows_the_current_working_directory(self):
        from interfacetester.models import ProjectMeta

        baseline = ProjectMeta().RootDir
        target = os.path.join(self._cwd, "logs")
        os.chdir(target)

        fresh = ProjectMeta().RootDir

        # 非空跑自检：两次取到的确实不同，否则「冻结在 import 期」这个前提就没被覆盖
        self.assertNotEqual(os.path.normcase(fresh), os.path.normcase(baseline))
        self.assertEqual(os.path.normcase(fresh), os.path.normcase(target))

    def test_explicit_root_dir_still_wins(self):
        """反向护栏：显式传入的 RootDir 不受默认值影响。"""
        from interfacetester.models import ProjectMeta

        explicit = os.path.join(self._cwd, "examples")

        self.assertEqual(ProjectMeta(RootDir=explicit).RootDir, explicit)


class TestUnknownFieldWarnings(unittest.TestCase):
    """写了但会被静默忽略的字段必须给出告警。

    NOTICE: pydantic 默认 `extra="ignore"`，模型里没有的字段会被直接丢掉——
    YAML 里写 `proxies:`（修复前没有该字段）时用户以为配了代理、实际直连；
    把 `validate` 写成 `validates` 时只会看到「断言没生效」，极难排查。
    这里锁定：未知字段有告警、已知字段不误报。
    """

    @staticmethod
    def _testcase(request: dict, config: dict = None) -> dict:
        return {
            "config": config or {"name": "unknown field case"},
            "teststeps": [{"name": "step", "request": request}],
        }

    def test_unknown_config_field_warns(self):
        testcase = self._testcase(
            {"method": "GET", "url": "/get"}, config={"name": "c", "base_uri": "x"}
        )
        messages = _capture_warnings(loader.load_testcase, testcase)

        self.assertTrue(
            any(
                "config 中存在无法识别的字段" in message and "base_uri" in message
                for message in messages
            ),
            messages,
        )

    def test_unknown_top_level_field_warns(self):
        """0920 批次 6 / **N36**：**顶层**未知键此前完全不告警。

        修复前这个函数把 config、每个 step、每个 request 都查了，唯独没查顶层 ——
        而它存在的意义正是"让写了却被忽略的字段可见"，顶层恰恰最容易整块写错：
        `variables:` 写在顶层（应该写在 `config` 下）会被 pydantic 直接丢掉，
        零告警，运行期只表现为"变量取不到"。
        """
        testcase = self._testcase({"method": "GET", "url": "/get"})
        testcase["variables"] = {"token": "abc"}
        messages = _capture_warnings(loader.load_testcase, testcase)

        self.assertTrue(
            any("顶层" in message and "variables" in message for message in messages),
            messages,
        )

    def test_top_level_hint_points_at_config(self):
        """"本该写在 config 下"的字段要给出**具体改法**，而不是只说"不认识"。"""
        testcase = self._testcase({"method": "GET", "url": "/get"})
        testcase["variables"] = {"token": "abc"}
        messages = _capture_warnings(loader.load_testcase, testcase)

        joined = "\n".join(messages)
        self.assertIn("config", joined)
        self.assertIn("静默丢弃", joined)

    def test_clean_top_level_does_not_warn(self):
        """反向护栏：正常的顶层（只有 config / teststeps）不许告警。"""
        testcase = self._testcase({"method": "GET", "url": "/get"})
        messages = _capture_warnings(loader.load_testcase, testcase)

        self.assertFalse(
            any("顶层" in message for message in messages), messages
        )

    def test_unknown_step_field_warns(self):
        """0917-1：step 级未知键此前**完全不告警**（`teardown_hook` 就是这么被静默丢掉的）。"""
        testcase = self._testcase({"method": "GET", "url": "/get"})
        testcase["teststeps"][0]["retry_times_x"] = 3
        messages = _capture_warnings(loader.load_testcase, testcase)

        self.assertTrue(
            any(
                "步骤 'step' 中存在无法识别的字段" in message and "retry_times_x" in message
                for message in messages
            ),
            messages,
        )

    def test_singular_hook_key_warns_with_plural_hint(self):
        """单数 `teardown_hook` 会让钩子**静默不执行**，告警必须点出复数写法。"""
        testcase = self._testcase({"method": "GET", "url": "/get"})
        testcase["teststeps"][0]["teardown_hook"] = "${noop($response)}"
        messages = _capture_warnings(loader.load_testcase, testcase)

        self.assertTrue(any("teardown_hook" in message for message in messages), messages)
        self.assertTrue(
            any("teardown_hooks" in message and "不会执行" in message for message in messages),
            messages,
        )

    def test_plural_hooks_do_not_warn(self):
        """复数键是正确写法，不得误报。"""
        testcase = self._testcase({"method": "GET", "url": "/get"})
        testcase["teststeps"][0]["setup_hooks"] = ["${noop($request)}"]
        testcase["teststeps"][0]["teardown_hooks"] = ["${noop($response)}"]
        testcase["teststeps"][0]["validate"] = [{"eq": ["status_code", 200]}]
        messages = _capture_warnings(loader.load_testcase, testcase)

        self.assertEqual([message for message in messages if "无法识别" in message], [])

    def test_unknown_request_field_warns_with_hint(self):
        testcase = self._testcase(
            {"method": "GET", "url": "/get", "validates": [{"eq": ["status_code", 200]}]}
        )
        messages = _capture_warnings(loader.load_testcase, testcase)

        self.assertTrue(
            any("validates" in message for message in messages), messages
        )
        # 提示里要给出替代写法，而不只是「不认这个字段」
        self.assertTrue(
            any("proxies / cert / stream" in message for message in messages),
            messages,
        )

    def test_known_request_fields_do_not_warn(self):
        """已支持字段（含别名 json 与新增的 proxies/cert/stream）不得误报。"""
        testcase = self._testcase(
            {
                "method": "GET",
                "url": "/get",
                "params": {"a": 1},
                "headers": {"X-A": "1"},
                "json": {"b": 2},
                "data": "raw",
                "cookies": {"c": "1"},
                "timeout": 5,
                "verify": False,
                "allow_redirects": False,
                "upload": {},
                "x_path": "/a",
                "proxies": {"http": "http://127.0.0.1:8888"},
                "cert": "/tmp/c.pem",
                "stream": True,
            }
        )
        messages = _capture_warnings(loader.load_testcase, testcase)

        self.assertEqual(messages, [])

    def test_unknown_step_field_warns(self):
        """step 级字段是在格式转换的白名单里被丢掉的，因此告警来自 compat。"""
        test_content = {
            "config": {"name": "c"},
            "teststeps": [
                {"name": "step", "request": {"method": "GET", "url": "/get"}, "validates": []}
            ],
        }
        messages = _capture_warnings(compat.ensure_testcase_v4, test_content)

        self.assertTrue(
            any(
                "teststep" in message and "validates" in message
                for message in messages
            ),
            messages,
        )

    def test_empty_testcase_file_raises_clear_error(self):
        """0917-1：空文件此前抛 `AttributeError: 'NoneType' object has no attribute 'get'`。"""
        with self.assertRaises(exceptions.FileFormatError) as ctx:
            loader.load_testcase(None)
        self.assertIn("空文件", str(ctx.exception))

    def test_real_example_yaml_does_not_warn(self):
        """回归：仓库自带的**全部**示例用例不得产生任何「无法识别字段」告警。

        0917-1：此前只扫了 1 个文件，于是 `examples/postman_echo/.../request_with_functions.yml`
        里的上游遗留字段 `weight`（框架未实现、写了会被静默忽略）一直没被发现——
        现在扩成递归扫描 `examples/**`，把「示例不得教错写法」变成不变量。

        NOTICE（0921）：**唯一**的例外是 `contrast_*` 前缀的对照演示用例 —— 它们存在的意义
        就是**故意写错**（`examples/quickstart/contrast_false_pass.yml` 故意把 `validate`
        拼成 `validates`，用来演示「假通过：绿了但断言根本没执行」）。

        例外刻意做成**收得很紧**，并且**带自检**：
          - 判据与各示例 conftest 的跳过判据**同源**（都是 `contrast_` 前缀），
            新增对照文件不需要再改本用例；
          - 例外集合必须**真的**产生告警（末尾那段 `assertTrue`）——
            否则说明某个对照文件被「改好了」而例外没收回（例外腐烂，等于判据失效）。
        """
        # 这些示例**不是独立用例**（profile 片段 / 契约 / 被引用的子用例），
        # 单独 `load_testcase_file` 必然失败；除它们之外，任何示例若加载失败都要暴露出来。
        non_standalone = {
            os.path.normpath("examples/data/profile.yml"),
            os.path.normpath("examples/data/profile_override.yml"),
            os.path.normpath("examples/data/openapi/petstore_demo.yaml"),
            os.path.normpath("examples/unifsp/auth_login.yml"),
        }

        example_files = [
            os.path.join(root, name)
            for root, _, names in os.walk("examples")
            for name in names
            if name.endswith((".yml", ".yaml")) and not name.endswith(("_test.yml", "_test.yaml"))
        ]
        self.assertGreater(len(example_files), 20, example_files)  # 防止扫错目录导致空跑

        warnings, load_failures = [], []
        contrast_warnings = []  # `contrast_*` 故意写错而产生的那部分（见 NOTICE）
        for path in example_files:
            if os.path.normpath(path) in non_standalone:
                continue
            try:
                messages = _capture_warnings(loader.load_testcase_file, path)
            except Exception as ex:  # noqa: BLE001
                load_failures.append(f"{path}: {type(ex).__name__}")
                continue
            found = [message for message in messages if "无法识别" in message]
            if os.path.basename(path).startswith("contrast_"):
                contrast_warnings.extend(f"{path}: {message}" for message in found)
                continue
            warnings.extend(f"{path}: {message}" for message in found)

        self.assertEqual(load_failures, [])
        self.assertEqual(warnings, [])
        self.assertTrue(
            contrast_warnings,
            "没有任何 `contrast_*` 示例产生「无法识别字段」告警 —— 说明对照演示"
            "（例如把 validate 拼成 validates 的那条）已经被改好了，"
            "本用例的例外应当**一并收回**（否则例外就成了空跑）。",
        )


# ---------------------------------------------------------------------------
# 0918-4 批次：项目定位的两处一致性修复
#   M8   `relative_to_root_dir` 用不带分隔符的 startswith 判断项目归属
#   M15  `load_project_meta` 的快路径吞掉嵌套项目
# ---------------------------------------------------------------------------


class TestRelativeToRootDirOwnership(unittest.TestCase):
    """M8：`relative_to_root_dir` 的归属判断必须与 `_is_path_in_root_dir` 同口径。

    NOTICE: 同文件里的 `_is_path_in_root_dir` 已经用
    「normalize + root_dir + os.sep」判断，`relative_to_root_dir` 却还是裸的
    `abs_path.startswith(project_meta.RootDir)`——同一件事两个口径，
    典型的「修了一处漏了另一处」。
    """

    def setUp(self):
        self.tmp_dir = _tmp_dir("rel_root")
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.root = os.path.join(self.tmp_dir, "interface-tester")
        self.sibling = os.path.join(self.tmp_dir, "interface-tester2")
        os.makedirs(self.root)
        os.makedirs(self.sibling)

    def _meta(self):
        meta = loader.ProjectMeta()
        meta.RootDir = self.root
        return meta

    def test_converts_path_inside_root_dir(self):
        abs_path = os.path.join(self.root, "a-b.c", "case.yml")

        relative = loader.relative_to_root_dir(abs_path, self._meta())

        self.assertEqual(relative, os.path.join("a-b.c", "case.yml"))

    def test_rejects_sibling_dir_sharing_the_root_prefix(self):
        """`<root>2/x.yml` 不是本项目内文件，必须报错而不是产出一个假相对路径。

        修复前：`startswith("<root>")` 为真 → 返回 `'\\x.yml'`（或 `'/x.yml'`），
        调用方拿到一个「看起来像相对路径」的垃圾值，错误被推到很远的地方才暴露。
        """
        abs_path = os.path.join(self.sibling, "x.yml")

        with self.assertRaises(exceptions.ParamsError):
            loader.relative_to_root_dir(abs_path, self._meta())

    def test_rejects_path_completely_outside(self):
        abs_path = os.path.join(self.tmp_dir, "elsewhere", "x.yml")

        with self.assertRaises(exceptions.ParamsError):
            loader.relative_to_root_dir(abs_path, self._meta())

    @unittest.skipUnless(
        os.path.normcase("A") == os.path.normcase("a"),
        "仅大小写不敏感的平台（Windows）需要本条",
    )
    def test_accepts_differently_cased_path(self):
        """Windows 上大小写不同不代表项目不同（修复前会被误判为「项目外」）。"""
        abs_path = os.path.join(self.root, "case.yml").upper()

        relative = loader.relative_to_root_dir(abs_path, self._meta())

        self.assertEqual(relative.lower(), "case.yml")

    # ---------------------------------------------------------------- 0918-8 / M17
    def test_dirty_path_components_are_normalized(self):
        r"""M17：返回值不能拿**原始字符串**切片算。

        修复前是 `abs_path[len(RootDir) + 1:]`：归属判断用规范化路径、取值却切原始串，
        于是 `.\\`、`..`、重复分隔符会让偏移量错位，返回的相对路径从中间截断
        （实测 `hmake .\nested\case.yml` → 生成物落到 `nested\d\case_test.py`）。
        """
        cases = {
            os.path.join(self.root, ".", "a-b.c", "case.yml"): os.path.join(
                "a-b.c", "case.yml"
            ),
            self.root + os.sep + os.sep + "a-b.c" + os.sep + "case.yml": os.path.join(
                "a-b.c", "case.yml"
            ),
            os.path.join(self.root, "sub", "..", "a-b.c", "case.yml"): os.path.join(
                "a-b.c", "case.yml"
            ),
        }

        for abs_path, expected in cases.items():
            with self.subTest(abs_path=abs_path):
                self.assertEqual(
                    loader.relative_to_root_dir(abs_path, self._meta()), expected
                )

    def test_clean_path_is_unchanged(self):
        """反向：本来就干净的路径，结果必须与修复前**逐字一致**（零生成物漂移）。"""
        abs_path = os.path.join(self.root, "a-b.c", "case.yml")

        self.assertEqual(
            loader.relative_to_root_dir(abs_path, self._meta()),
            os.path.join("a-b.c", "case.yml"),
        )

    def test_root_dir_itself_returns_empty_string(self):
        """传入 RootDir 本身时返回空串（`ensure_file_abs_path_valid` 依赖这个语义：
        空串 = 没有可派生的名字，直接原样返回）。改成 `os.path.relpath` 后不能变成 `.`。"""
        self.assertEqual(loader.relative_to_root_dir(self.root, self._meta()), "")


class TestNestedProjectMeta(unittest.TestCase):
    """M15：快路径不得把嵌套项目吞进父项目。

    实测（修复前）：
        1) 全新进程直接加载嵌套项目 → RootDir = 嵌套项目      ✅
        2) 先加载父项目，再加载同一嵌套项目 → RootDir = 父项目  ❌ 复用旧 meta
    后果是同进程多项目场景下，子项目拿到父项目的 RootDir / .env / debugtalk 函数，
    `--save-tests` 还会把 conftest.py 写到**父项目根**。
    """

    def setUp(self):
        loader.reset_project_meta()
        self.tmp_dir = _tmp_dir("nested_proj")
        self.addCleanup(self._cleanup_global_state)
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)

        self.parent = os.path.join(self.tmp_dir, "parent")
        self.child = os.path.join(self.parent, "child")
        os.makedirs(self.child)
        for directory in (self.parent, self.child):
            # 两个目录都有 debugtalk.py = 两个各自独立的项目
            with open(os.path.join(directory, "debugtalk.py"), "w") as f:
                f.write("")

        self.parent_case = os.path.join(self.parent, "case.yml")
        self.child_case = os.path.join(self.child, "case.yml")
        for path in (self.parent_case, self.child_case):
            with open(path, "w") as f:
                f.write("")

    def _cleanup_global_state(self):
        """本类会真的把临时目录塞进 sys.path 并 import debugtalk，必须还原。"""
        loader.reset_project_meta()
        sys.modules.pop("debugtalk", None)
        for entry in list(sys.path):
            if isinstance(entry, str) and entry.startswith(self.tmp_dir):
                sys.path.remove(entry)

    def test_child_project_loaded_directly(self):
        """对照组：不先加载父项目时，本来就是对的行为。"""
        meta = loader.load_project_meta(self.child_case)

        self.assertTrue(_same_path(meta.RootDir, self.child), meta.RootDir)

    def test_child_project_is_not_swallowed_by_parent(self):
        """先加载父项目后，嵌套项目仍必须拿到自己的 RootDir。"""
        parent_meta = loader.load_project_meta(self.parent_case)
        self.assertTrue(_same_path(parent_meta.RootDir, self.parent), parent_meta.RootDir)

        child_meta = loader.load_project_meta(self.child_case)

        self.assertTrue(
            _same_path(child_meta.RootDir, self.child),
            f"嵌套项目的 RootDir 被父项目吞掉了: {child_meta.RootDir}",
        )

    def test_switching_back_to_parent_still_works(self):
        """反向：切到子项目后再切回父项目，也必须正确（缓存不能只有单向）。"""
        loader.load_project_meta(self.child_case)

        parent_meta = loader.load_project_meta(self.parent_case)

        self.assertTrue(_same_path(parent_meta.RootDir, self.parent), parent_meta.RootDir)
