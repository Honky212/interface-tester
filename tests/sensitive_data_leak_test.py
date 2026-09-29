"""0918-2（批次 2）：凭据明文的**对抗性**测试集 —— M4（运行期）+ M5（导入器）。

设计原则
--------
1. **两个方向都测**（对抗性测试的一半是"别把东西改坏"）：
   - 机密值**绝不能**出现在任何产物里（日志 / summary / 报告 / 生成的 YAML / 告警）；
   - 非机密值**必须原样保留**（不能为了"安全"把 User-Agent、普通参数也糊掉）。
2. **canary 手法**：用一个绝不会偶然撞上的哨兵值，然后在**所有产物**里搜它。
   比"断言某个字段等于 ******"更强：后者只验证了被想到的那条路径，
   前者能抓住"还有别的地方在漏"。
3. **端到端而非单元**：走真实的 `test_start()` / 真实的适配器入口，
   因为这类缺陷的特点就是"单点看着都对，串起来就漏"。

NOTICE（本测试集的边界，不要当成已覆盖）
--------------------------------------
- **响应侧：头/cookie 与"按键名判定的敏感字段"现在会脱敏（0920 批次 2 / N15 已修）**；
  但**响应体的业务字段一律保留**——被测服务完全可能在响应里回显请求头（httpbin 的
  `/headers`、`/get` 就是如此），那是**断言素材**，一律糊掉会掩盖真实问题。
  修复前是"响应侧一个字都不脱敏"，于是 `Set-Cookie: sessionid=…` 与
  `{"access_token": …}` 明文进了 `.run.log` / `summary.json` / HTML / Allure。
  现在的口径：按 `utils.is_sensitive_key` 的**同一套判据**脱敏（`cookie`/`session`/
  `token`/`jwt`/`authorization`… 本来就在 `SENSITIVE_KEYWORDS` 里），
  非敏感字段（`Server`/`Date`/`order_no`…）逐字保留。
  见 `TestRuntimeResponseCredentialMasking`。
- **不覆盖**：`reports/*.html`（HTML 报告内容由 pytest-html 从 summary 渲染，
  summary 干净即等价）。
- **Allure 附件：已实跑核对（0920 收尾）**。此前这里写的是"同一份文本，不单独验"——
  收尾时补了真 `--alluredir` 端到端核对（见
  `TestAllureAttachmentsDoNotLeakCredentials`）：6 个附件 + `result.json` 里
  **零明文 canary**，且 `request details` / `response details` 附件里
  `Set-Cookie` / `X-Auth-Token` / `access_token` / `Authorization` / `password`
  都是 `******`，而非敏感的 `order_no` / `Server` 逐字保留。
  NOTICE: 该用例在**装了 `allure-pytest` 时**才真跑，否则 skip
  （与本仓其它可选依赖用例同一口径）。
- **注意「服务端主动回显」是天然残留**：如果被测服务把凭据**回显在响应体**里
  （如 `/echo` 回显 `X-Token`），而它又是业务断言的目标，那这个值就会出现在日志里
  ——这是"断言素材"与"凭据"的重叠区，框架**选择保留**（糊掉会让断言失去意义）。
  本测试集因此用**不回显**的 `/status/200` 隔离出真正的泄漏路径。
"""
import json
import os
import shutil
import unittest
from unittest import mock

from loguru import logger

from interfacetester import Config, InterfaceTester, RunRequest, RunTestCase, Step
from interfacetester import utils
from interfacetester.converters import build_case_dict
from interfacetester.converters import postman_adapter
from interfacetester.converters.curl_adapter import convert_curl_file
from interfacetester.converters.emit_yaml import dump_case_yaml
from interfacetester.converters.har_adapter import convert_har
from interfacetester.converters.openapi_adapter import convert_openapi
from interfacetester.converters.postman_adapter import convert_postman
from interfacetester.utils import HTTP_BIN_URL

# 哨兵值：一旦出现在产物里，就是泄漏
CANARY = "CANARY_SECRET_9f3ab21c"
# 纯 16 进制哨兵：用来触发"动态值识别"告警（32 位 hex 会被当成随机串），
# 用于验证「告警文本不得回显明文」。
HEX_CANARY = "b7d03a6947b217efb6f3ec3bd3504582"


def _as_list(result):
    """适配器返回值不统一：curl/HAR 返回单个 `IRCase`，Postman/OpenAPI 返回列表。"""
    return result if isinstance(result, list) else [result]


def _first(result):
    return _as_list(result)[0]


def _dump(result) -> str:
    case = _first(result)
    return dump_case_yaml(case, f"{case.name}.yml")


def _warnings(result) -> str:
    return "\n".join(_first(result).all_warnings())


# ===========================================================================
# M4：运行期 —— 请求侧的凭据不得进日志 / summary / 报告
# ===========================================================================
class TestRuntimeRequestCredentialMasking(unittest.TestCase):
    """M4：含 `Authorization`/`Cookie` 的请求头（以及体里的密码）明文写进
    `.run.log`、summary、HTML/Allure 报告。

    修复前实测的泄漏路径（4 处）::

        .step_results[0].data.req_resps[0].request.headers.Authorization
        .step_results[0].data.req_resps[0].request.headers.Cookie
        .step_results[0].data.req_resps[0].request.headers.X-Custom-Token
        .step_results[0].data.req_resps[0].request.body.password

    根本原因不是"没有脱敏机制"——`utils.mask_sensitive_variables` 早就存在、
    `config_vars` 也确实被脱敏了（实测 `{'token': '******'}`）——而是**只应用在了
    config_vars 这一处**，请求头/体这条最常带凭据的链路完全没接上。
    """

    def _run_case(self, step_chain):
        """真实跑一次用例，返回 (summary, run.log 文本, 捕获到的 stdout 文本)。

        用 `/status/200`（不回声请求）隔离**请求侧**泄漏，见模块 docstring。
        """
        captured = []
        sink_id = logger.add(captured.append, level="DEBUG", format="{message}")

        class _LeakProbeCase(InterfaceTester):
            config = Config("leak probe").base_url(HTTP_BIN_URL).variables(
                token=CANARY
            )
            teststeps = [step_chain]

        try:
            runner = _LeakProbeCase().test_start()
        finally:
            logger.remove(sink_id)

        summary = runner.get_summary()
        with open(summary.log, encoding="utf-8") as f:
            log_text = f.read()

        return summary, log_text, "\n".join(str(m) for m in captured)

    @staticmethod
    def _case_with_secret_headers():
        # NOTICE: 整条链必须用 `Step(...)` 包住 —— 链尾的 `.assert_equal()` 返回的是
        # `StepRequestValidation`（只有 name/type/run），而 `teststeps` 里需要的是有
        # `retry_times`/`retry_interval` 的 `Step` 包装（`runner.__run_step` 会读它）。
        # `hmake` 生成的代码正是 `Step(RunRequest(...)...assert_equal(...))`。
        return Step(
            RunRequest("secret headers step")
            .get("/status/200")
            .with_headers(
                **{
                    "Authorization": f"Bearer {CANARY}",
                    "Cookie": f"session={CANARY}",
                    "X-Custom-Token": CANARY,
                    "X-Signature": CANARY,
                }
            )
            .with_json({"username": "bob", "password": CANARY, "note": "plain"})
            .validate()
            .assert_equal("status_code", 200)
        )

    def test_summary_does_not_leak_request_credentials(self):
        """summary（进而 HTML 报告 / `--save-tests` 的 summary.json）不得出现明文。"""
        summary, _log_text, _stdout = self._run_case(self._case_with_secret_headers())

        payload = json.dumps(summary.model_dump(), ensure_ascii=False, default=str)

        self.assertNotIn(
            CANARY,
            payload,
            "summary 里出现了明文凭据（会随 summary.json / HTML 报告 / Allure 一起外泄）",
        )

    def test_run_log_does_not_leak_request_credentials(self):
        """`logs/{case_id}.run.log` 不得出现明文。"""
        _summary, log_text, _stdout = self._run_case(self._case_with_secret_headers())

        self.assertNotIn(CANARY, log_text, ".run.log 里出现了明文凭据")

    def test_stdout_logs_do_not_leak_request_credentials(self):
        """控制台（loguru → stdout）不得出现明文——CI 日志是公开可见的。"""
        _summary, _log_text, stdout_text = self._run_case(
            self._case_with_secret_headers()
        )

        self.assertNotIn(CANARY, stdout_text, "stdout 日志里出现了明文凭据")

    def test_urlencoded_string_body_is_masked(self):
        """字符串表单体（`data: "user=b&password=x"`）同样要脱敏。

        NOTICE: 这条与 JSON 体是两条不同路径——`mask_sensitive_variables` 只按 dict 的
        **键名**匹配，字符串体没有键，必须单独解析。
        """
        step = Step(
            RunRequest("form body step")
            .post("/status/200")
            .with_data(f"user=bob&password={CANARY}&note=plain")
            .validate()
            .assert_equal("status_code", 200)
        )

        summary, log_text, stdout_text = self._run_case(step)

        payload = json.dumps(summary.model_dump(), ensure_ascii=False, default=str)
        self.assertNotIn(CANARY, payload, "字符串表单体在 summary 里明文出现")
        self.assertNotIn(CANARY, log_text, "字符串表单体在 .run.log 里明文出现")
        self.assertNotIn(CANARY, stdout_text, "字符串表单体在 stdout 里明文出现")

    def test_top_level_list_json_body_is_masked(self):
        """批次 4 / M3：**顶层是数组**的 JSON 体（`json: [{...}]`）同样不得泄漏。

        NOTICE（修复前实测）：`mask_sensitive_request_data` 的非 `Mapping` 分支只交给
        `_mask_query_pairs`（只认 `str`），于是顶层 list **整块原样返回**：

        ```text
        json: [{user: bob, password: CANARY}]  ->  .run.log 里 2 处明文
        json: {user: bob, password: CANARY}    ->  0 处（dict 体是对的）
        ```

        数组体（批量接口）是**正常 YAML 写法**，不是边角输入；
        而 `docs/能力清单.md` 宣称"请求的 url/headers/cookies/body 都过
        `mask_sensitive_request_data`"——**「看起来脱敏了，其实没有」**。
        """
        step = Step(
            RunRequest("list body step")
            .post("/status/200")
            .with_json([{"user": "bob", "password": CANARY, "note": "plain"}])
            .validate()
            .assert_equal("status_code", 200)
        )

        summary, log_text, stdout_text = self._run_case(step)

        payload = json.dumps(summary.model_dump(), ensure_ascii=False, default=str)
        self.assertNotIn(CANARY, payload, "顶层 list 体在 summary 里明文出现（M3）")
        self.assertNotIn(CANARY, log_text, "顶层 list 体在 .run.log 里明文出现（M3）")
        self.assertNotIn(CANARY, stdout_text, "顶层 list 体在 stdout 里明文出现（M3）")

    def test_json_string_body_with_equals_is_masked_and_stays_valid_json(self):
        r"""批次 4 / N2：JSON **字符串**体里值自带 `=` 时，脱敏不能把体改坏。

        NOTICE（修复前实测）：

        ```text
        data: '{"password": "CANARY="}'
          -> 日志副本 '{"password": "CANARY=******'     非法 JSON + CANARY 前缀仍明文
        ```

        它只作用于**展示副本**（真实请求不受影响），但报告/日志里显示的请求体是坏的，
        而 `******` 会让人以为已经收口。这里同时钉住「值整体变成 `******`」与「仍是合法 JSON」。
        """
        step = Step(
            RunRequest("json string body step")
            .post("/status/200")
            .with_data(f'{{"password": "{CANARY}="}}')
            .validate()
            .assert_equal("status_code", 200)
        )

        summary, log_text, stdout_text = self._run_case(step)

        payload = json.dumps(summary.model_dump(), ensure_ascii=False, default=str)
        self.assertNotIn(CANARY, payload, "JSON 字符串体的值前半段仍明文（N2）")
        self.assertNotIn(CANARY, log_text, "JSON 字符串体在 .run.log 里仍明文（N2）")
        self.assertNotIn(CANARY, stdout_text, "JSON 字符串体在 stdout 里仍明文（N2）")

        # 记录副本里的请求体必须仍是**合法 JSON**（坏体比明文更难查）
        # NOTICE: `runner.get_summary()` 返回的是**单个用例**的 `TestCaseSummary`
        # （带 `details` 的那个聚合体是 `compat._generate_conftest_for_summary` 拼的），
        # 所以这里走 `step_results` 而不是 `details[0].records`。
        recorded = summary.step_results[0].data.req_resps[0].request.body
        self.assertEqual(recorded, {"password": utils.MASKED_VALUE})

    def test_nested_string_body_is_masked(self):
        """同一入口的另一半：**嵌套位置**放字符串体（`{"body": "password=x"}`）也要脱敏。

        NOTICE: 修复前 `_mask_sensitive_value` 对 `str` 直接原样返回，
        所以"顶层是 str"能脱敏、"嵌在 dict/list 里的 str"反而不脱敏 ——
        同一入口两套覆盖。这条钉住收口后的规则：**递归碰到字符串就按表单串/JSON 串处理**。
        """
        step = Step(
            RunRequest("nested string body step")
            .post("/status/200")
            .with_json({"payload": f"user=bob&password={CANARY}"})
            .validate()
            .assert_equal("status_code", 200)
        )

        summary, log_text, _stdout = self._run_case(step)

        payload = json.dumps(summary.model_dump(), ensure_ascii=False, default=str)
        self.assertNotIn(CANARY, payload, "嵌套字符串体在 summary 里明文出现")
        self.assertNotIn(CANARY, log_text, "嵌套字符串体在 .run.log 里明文出现")

    def test_basic_auth_style_authorization_is_masked(self):
        step = Step(
            RunRequest("basic auth step")
            .get("/status/200")
            .with_headers(**{"Authorization": "Basic Ym9iOnMzY3JldA=="})
            .validate()
            .assert_equal("status_code", 200)
        )

        summary, log_text, _stdout = self._run_case(step)

        payload = json.dumps(summary.model_dump(), ensure_ascii=False, default=str)
        self.assertNotIn("Ym9iOnMzY3JldA==", payload)
        self.assertNotIn("Ym9iOnMzY3JldA==", log_text)

    def test_non_sensitive_request_data_is_preserved(self):
        """**反向断言**：不能为了安全把普通数据也糊掉。

        如果实现偷懒（例如"把整个 request 都替换成 ******"），这条会失败。
        """
        step = Step(
            RunRequest("mixed step")
            .post("/status/200")
            .with_headers(
                **{"User-Agent": "InterfaceTester/3.0", "X-Trace-Id": "trace-123"}
            )
            .with_json({"username": "bob", "note": "plain", "amount": 100})
            .validate()
            .assert_equal("status_code", 200)
        )

        summary, _log_text, _stdout = self._run_case(step)
        payload = json.dumps(summary.model_dump(), ensure_ascii=False, default=str)

        self.assertIn("InterfaceTester/3.0", payload, "User-Agent 不该被脱敏")
        self.assertIn("trace-123", payload, "非敏感自定义头不该被脱敏")
        self.assertIn("bob", payload, "普通字段不该被脱敏")
        self.assertIn("plain", payload, "普通字段不该被脱敏")

    def test_config_vars_masking_is_not_regressed(self):
        """既有能力（`config_vars` 脱敏）不能被改坏。"""
        summary, _log_text, _stdout = self._run_case(self._case_with_secret_headers())

        self.assertEqual(summary.in_out.config_vars.get("token"), "******")

    def test_real_request_still_carries_the_true_credential(self):
        """**最关键的反向断言**：脱敏只能作用于"记录/展示"，绝不能改到真正发出去的请求。

        如果实现偷懒（在 `step_request` 里直接原地改 `parsed_request_dict`，或者把
        脱敏做在 `HttpSession` 请求参数上），用例会带着 `******` 去请求被测服务 ——
        那不是"更安全"，而是**把鉴权彻底改坏**，而且失败现场极难定位
        （服务端 401，日志里看到的却是被糊过的头）。
        """
        import requests
        from unittest import mock

        captured = {}

        def spy(session_self, method, url, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop before sending")

        step = Step(
            RunRequest("outgoing credential step")
            .post("/status/200")
            .with_headers(
                **{
                    "Authorization": f"Bearer {CANARY}",
                    "X-Signature": CANARY,
                }
            )
            .with_data(f"user=bob&password={CANARY}")
            .with_json({"password": CANARY})
            .validate()
            .assert_equal("status_code", 200)
        )

        with mock.patch.object(requests.Session, "request", spy):
            try:
                self._run_case(step)
            except RuntimeError:
                pass

        # 真正发出去的请求：原文必须是明文凭据
        self.assertEqual(captured.get("headers", {}).get("Authorization"), f"Bearer {CANARY}")
        self.assertEqual(captured.get("headers", {}).get("X-Signature"), CANARY)
        self.assertEqual(captured.get("data"), f"user=bob&password={CANARY}")
        self.assertEqual(captured.get("json"), {"password": CANARY})


# ===========================================================================
# L36（0919-3）：运行期 —— `extract mapping` 的日志必须以脱敏副本打印
# ===========================================================================
class TestRuntimeExtractMappingMasking(unittest.TestCase):
    """`extract mapping` 这条日志会把**提取到的值**原样打出来。

    它属于响应侧，但和响应头/响应体不同：**脱敏它几乎不损失调试能力**。
    理由正是它的泄漏面与价值严重不对称：

    - **泄漏面**：两条链路——`.run.log`（sink 是硬编码 `level="DEBUG"`，
      与 `--log-level` 无关，所以**每个用例都会无条件写进文件**）与 stdout；
    - **价值低**：提取到的值在断言里、在后续步骤的请求里都能看到，
      这条日志要回答的其实是「extract 有没有命中、命中了哪几个键」；
    - **命中率高**：`extract` 最标准的用法就是「登录后提取 token」——
      也就是说这是**默认会踩**的一条，不是边角。

    NOTICE: 本用例只断言 **`extract mapping:` 那一行**，**不**断言整份日志干净。
    响应体（含被回显的请求参数）**仍按现状保持明文**——那是 B.6 里明确本批不做的部分。
    别把这条用例误读成「日志已经安全了」。
    """

    def _run_case(self, steps):
        """真实跑一次用例，返回 (summary, run.log 文本, loguru 捕获的消息列表)。

        NOTICE: `steps` 里每个元素都必须是 `Step(...)` 包装过的。
        链尾的 `.assert_equal()` 返回的是 `StepRequestValidation`，它**没有**
        `retry_times` / `retry_interval`，而 `runner.__run_step` 要读这两个属性——
        直接塞未包装的链会在运行期炸成 `AttributeError`（同文件 M4 那段也踩过）。
        """
        captured = []
        sink_id = logger.add(captured.append, level="DEBUG", format="{message}")

        class _ExtractProbeCase(InterfaceTester):
            config = Config("extract probe").base_url(HTTP_BIN_URL)
            teststeps = list(steps)

        try:
            runner = _ExtractProbeCase().test_start()
        finally:
            logger.remove(sink_id)

        summary = runner.get_summary()
        with open(summary.log, encoding="utf-8") as f:
            log_text = f.read()

        return summary, log_text, captured

    @staticmethod
    def _extract_lines(messages, log_text):
        """取出所有 `extract mapping:` 行（stdout 捕获 + .run.log 两侧都要看）。"""
        from_messages = [str(m) for m in messages if "extract mapping:" in str(m)]
        from_log = [line for line in log_text.splitlines() if "extract mapping:" in line]
        return from_messages, from_log

    def test_sensitive_extracted_value_is_masked_in_the_log(self):
        """键名敏感时（`token`），日志里的值必须是 `******`。"""
        step = Step(
            RunRequest("login and extract token")
            .get("/get")
            .with_params(token=CANARY)
            .extract()
            .with_jmespath("body.args.token", "token")
            .validate()
            .assert_equal("status_code", 200)
        )

        _summary, log_text, messages = self._run_case([step])
        from_messages, from_log = self._extract_lines(messages, log_text)

        self.assertTrue(
            from_messages, "没有捕获到 extract mapping 日志——用例本身可能没跑起来"
        )
        self.assertTrue(
            from_log, "`.run.log` 里没有 extract mapping 行——sink 覆盖率变了？"
        )

        for label, lines in (("stdout", from_messages), ("run.log", from_log)):
            with self.subTest(channel=label):
                for line in lines:
                    self.assertNotIn(
                        CANARY, line, f"{label} 的 extract mapping 行里出现了明文凭据"
                    )
                    self.assertIn("******", line)

    def test_extracted_runtime_value_is_still_the_real_one(self):
        """**关键不变量**：脱敏的只是日志副本，运行期取值必须仍是真值。

        修复时最容易犯的错是「顺手把 `extract_mapping` 原地改掉」——
        它会被写进 `StepResult.export_vars`，再由
        `self.__session_variables.update(step_result.export_vars)`（runner.py:440）
        参与后续步骤与被引用用例的变量传递。取成脱敏值会让下游拿到 `******`。

        这里用「第 2 步把 `$token` 回显出去并断言等于哨兵」来证明传递链没被破坏。
        """
        steps = [
            Step(
                RunRequest("login and extract token")
                .get("/get")
                .with_params(token=CANARY)
                .extract()
                .with_jmespath("body.args.token", "token")
                .validate()
                .assert_equal("status_code", 200)
            ),
            Step(
                RunRequest("use the extracted token downstream")
                .get("/get?seen=$token")
                .validate()
                .assert_equal("status_code", 200)
                # 回显必须等于**真值**——若运行期拿到的是 `******`，这条会失败
                .assert_equal("body.args.seen", CANARY)
            ),
        ]

        summary, _log_text, _messages = self._run_case(steps)

        self.assertTrue(
            summary.success,
            "下游步骤没有拿到真实的提取值——脱敏泄漏到了运行期取值（变量传递被破坏）",
        )

    def test_non_sensitive_extracted_value_is_left_visible(self):
        """反向护栏：键名不敏感的提取值**不该**被糊掉。

        这条日志的价值就在于「extract 命中了什么」。如果实现偷懒（把整条 mapping
        全部替换成 `******`），这条会失败——过度脱敏会让排查失去依据。
        """
        step = Step(
            RunRequest("extract a plain field")
            .get("/get")
            .with_params(uid="user-12345")
            .extract()
            .with_jmespath("body.args.uid", "user_identifier")
            .validate()
            .assert_equal("status_code", 200)
        )

        _summary, log_text, messages = self._run_case([step])
        from_messages, _from_log = self._extract_lines(messages, log_text)

        self.assertTrue(from_messages)
        self.assertIn(
            "user-12345",
            "\n".join(from_messages),
            "非敏感的提取值也被糊掉了——这条日志的排查价值被抹掉了",
        )


# ===========================================================================
# M5：导入器 —— 生成 YAML / 告警里不得有明文凭据
# ===========================================================================
class TestConverterCredentialLeakCurl(unittest.TestCase):
    """curl 源：查询串、非白名单敏感头、字符串表单体都会明文入库。"""

    def test_query_string_credential_is_placeholder(self):
        content = (
            f"curl 'https://api.example.com/login?access_token={CANARY}&page=1' "
            f"-H 'Authorization: Bearer {CANARY}'"
        )

        yaml_text = _dump(convert_curl_file(content, "curl_query", "test"))

        self.assertNotIn(CANARY, yaml_text, "curl 查询串里的凭据明文写进了生成 YAML")
        self.assertIn("page=1", yaml_text, "非敏感查询参数不该被处理掉")

    def test_non_whitelisted_secret_header_is_placeholder(self):
        """`X-Signature` / 裸 `token` 不在修复前的白名单里，但显然是凭据。"""
        content = (
            f"curl 'https://api.example.com/x' "
            f"-H 'X-Signature: {CANARY}' "
            f"-H 'token: {CANARY}' "
            f"-H 'X-Amz-Security-Token: {CANARY}'"
        )

        yaml_text = _dump(convert_curl_file(content, "curl_hdr", "test"))

        self.assertNotIn(CANARY, yaml_text, "非白名单敏感头明文写进了生成 YAML")

    def test_string_form_body_is_placeholder(self):
        """`-d 'user=b&password=x'`（非 JSON Content-Type → 字符串体）必须脱敏。"""
        content = (
            f"curl -X POST 'https://api.example.com/login' "
            f"-H 'Content-Type: application/x-www-form-urlencoded' "
            f"-d 'user=bob&password={CANARY}'"
        )

        yaml_text = _dump(convert_curl_file(content, "curl_form", "test"))

        self.assertNotIn(CANARY, yaml_text, "字符串表单体明文写进了生成 YAML")
        self.assertIn("bob", yaml_text, "非敏感表单字段不该被处理掉")

    def test_json_body_password_is_placeholder(self):
        """回归：JSON 体（dict 形态）原本就脱敏，不能被改坏。"""
        content = (
            f"curl -X POST 'https://api.example.com/login' "
            f"-H 'Content-Type: application/json' "
            f"""-d '{{"user":"bob","password":"{CANARY}"}}'"""
        )

        yaml_text = _dump(convert_curl_file(content, "curl_json", "test"))

        self.assertNotIn(CANARY, yaml_text)
        self.assertIn("bob", yaml_text)

    def test_json_string_body_without_content_type_is_placeholder(self):
        r"""批次 4 / H8：**没有** `Content-Type: application/json` 的 JSON 体也必须脱敏。

        NOTICE（修复前实测）：`-d '<json>'` 不带那个头时，体落到字符串形态 →
        `sanitize_urlencoded_text` 因为「整串里没有 `=`」在**第一行**就原样返回：

        ```text
        curl -X POST 'https://api.example.com/login' \
             -d '{"username":"bob","password":"S3cr3t!","token":"abc123"}'
          -> data: '{"username":"bob","password":"S3cr3t!","token":"abc123"}'   明文 + **零告警**
        ```

        而生成文件头写着"敏感信息（Cookie/Authorization/token 等）已替换成 `${...}` 占位"
        —— **主动误导复核者**以为已经收口。上面那条 `test_string_form_body_is_placeholder`
        只覆盖了"真是表单串"的一支，这条补上 JSON 字符串体这一支。
        """
        content = (
            f"curl -X POST 'https://api.example.com/login' "
            f"""-d '{{"username":"bob","password":"{CANARY}","token":"tok-{CANARY}"}}'"""
        )

        case = convert_curl_file(content, "curl_json_raw", "test")
        yaml_text = _dump(case)

        self.assertNotIn(CANARY, yaml_text, "无 Content-Type 的 JSON 字符串体明文入库（H8）")
        self.assertIn("bob", yaml_text, "非敏感字段不该被处理掉")
        # 占位符必须真的接上（否则"没有明文"可能只是因为整段被丢掉了）
        self.assertIn("BODY_PASSWORD", yaml_text)
        self.assertIn("BODY_TOKEN", yaml_text)
        # NOTICE: `_warnings()` 返回的是**拼好的字符串**，不是列表 —— 直接 assertIn 即可
        # （第一版写成 `[w for w in _warnings(case) ...]`，等于逐字符遍历，恒为空）
        warnings_text = _warnings(case)
        self.assertIn(
            "BODY_PASSWORD",
            warnings_text,
            "脱敏发生了却没有告警，用户无从知道哪个字段被占位化了",
        )

    def test_json_string_body_with_equals_in_value_is_not_corrupted(self):
        r"""批次 4 / N1：JSON 字符串体里**值自带 `=`**（base64 结尾）时不能把体改坏。

        NOTICE（修复前实测）：按 `partition("=")` 切分会在**值内部**下刀：

        ```text
        -d '{"password": "S3cr3t="}'
          -> data: '{"password": "S3cr3t=${BODY_PASSWORD_S3CR3T}'     非法 JSON
             且 `S3cr3t` 前缀仍然明文可见；占位符名是从**损坏键名**派生的垃圾
        ```

        比 H8 更糟：**请求体被改坏**（发出去是畸形 payload），
        而复核者看到 `${...}` 占位符会以为已经收口。
        这条同时钉住三件事：明文没了、体仍是**合法 JSON**、占位符名是干净的。
        """
        content = (
            f"curl -X POST 'https://api.example.com/login' "
            f"""-d '{{"user":"bob","password":"{CANARY}="}}'"""
        )

        case = convert_curl_file(content, "curl_json_eq", "test")
        yaml_text = _dump(case)

        self.assertNotIn(CANARY, yaml_text, "值的前半段仍然明文可见（N1）")
        self.assertIn("${BODY_PASSWORD}", yaml_text)
        self.assertNotIn(
            "BODY_PASSWORD_",
            yaml_text,
            "占位符名是从损坏的键名派生的垃圾（N1 的现场特征）",
        )

        body = case.steps[0].data
        self.assertIsInstance(body, str, "体必须仍是字符串（data:），改成 json: 会改变真实请求")
        json.loads(body)  # 非法 JSON 会在这里抛，用例即失败

    def test_warning_text_does_not_echo_secret(self):
        """告警文本不得回显明文。

        修复前 `warn_dynamic_values(headers, ...)` 在 `sanitize_headers` **之前**执行，
        于是 32 位 hex 的凭据会命中"长随机 hex"规则，被**原样**写进告警；
        而告警既打 stdout 又会进 `hconvert --report` 的 markdown 报告。
        """
        content = f"curl 'https://api.example.com/x' -H 'X-Signature: {HEX_CANARY}'"

        case = convert_curl_file(content, "curl_warn", "test")

        self.assertNotIn(HEX_CANARY, _warnings(case), "告警文本回显了明文凭据")
        self.assertNotIn(HEX_CANARY, _dump(case), "生成 YAML 里出现明文凭据")


class TestWarningTextDoesNotEchoCredentials(unittest.TestCase):
    """0920 批次 4 / **N21**：**告警文本**这条泄漏路径。

    M13 修的是"URL 里的 userinfo 不许进告警"，但"告警回显其它凭据原文"当时漏了两处。
    而告警不只是打日志 —— `cli._build_report` 会把**全部**告警写进
    `hconvert --report` 的 markdown，等于进了 CI 产物。

    修复前的现场（`.tmp_audit/n21_check.py`，canary 手法）：

    ```text
    curl：`-d` 的内容已被**丢弃**： 'user=bob&password=CANARY…&token=tok-CANARY…'   ← 明文
    Postman：`Cookie` 头的值无法解析成 `k=v`（已忽略）：'eyJ…CANARY…sig'          ← 明文
    ```

    两侧的**生成 YAML 一直是干净的** —— 泄漏的只有告警这一路。
    修法是"可辨识但不回显"：表单串只列**字段名**（敏感名标 `=***`）、
    JWT 只报形态与长度。
    """

    def test_curl_dropped_body_warning_does_not_echo_values(self):
        content = (
            f"curl -X POST https://api.example.com/x "
            f"-d 'user=bob&password={CANARY}&token=tok-{CANARY}' "
            f"-F 'avatar=@./a.png'"
        )
        case = convert_curl_file(content, "n21_curl", "test")

        warnings = "\n".join(case.all_warnings())

        self.assertNotIn(CANARY, warnings, "被丢弃请求体的原文进了告警（会随 --report 外泄）")
        # 仍然要能看出"丢掉的是什么形状"
        self.assertIn("password=***", warnings)
        self.assertIn("user", warnings)
        self.assertIn("token=***", warnings)

    def test_postman_unparsable_cookie_warning_does_not_echo_the_value(self):
        collection = {
            "info": {
                "name": "n21",
                "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            },
            "item": [
                {
                    "name": "n21",
                    "request": {
                        "method": "GET",
                        "header": [
                            {"key": "Cookie", "value": f"eyJhbGciOiJIUzI1NiJ9.{CANARY}.sig"}
                        ],
                        "url": {
                            "raw": "https://api.test/x",
                            "protocol": "https",
                            "host": ["api", "test"],
                            "path": ["x"],
                        },
                    },
                }
            ],
        }
        case = _first(postman_adapter.convert_postman(collection, case_name="n21_pm"))

        warnings = "\n".join(case.all_warnings())

        self.assertNotIn(CANARY, warnings, "无法解析的 Cookie 原文进了告警")
        self.assertIn("JWT", warnings, "要给出可辨识的形态，否则这条告警没有排查价值")

    def test_opaque_value_description_stays_useful(self):
        """反向护栏：描述不能"什么都糊掉"——字段名是排查线索，必须留着。"""
        from interfacetester.converters.ir import describe_opaque_value

        described = describe_opaque_value(
            f"user=bob&password={CANARY}&page=2"
        )
        self.assertIn("user", described)
        self.assertIn("page", described)
        self.assertIn("password=***", described)
        self.assertNotIn(CANARY, described)
        self.assertNotIn("bob", described, "值不该出现在描述里")

    def test_opaque_value_description_handles_shapes(self):
        from interfacetester.converters.ir import describe_opaque_value

        self.assertEqual(describe_opaque_value(""), "空字符串")
        self.assertEqual(describe_opaque_value(None), "空值")
        self.assertIn("不透明值", describe_opaque_value("no-structure-here"))
        self.assertIn(
            "JWT", describe_opaque_value("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig")
        )


class TestConverterCredentialLeakHar(unittest.TestCase):
    """HAR 源：查询串与非白名单敏感头会明文入库。"""

    @staticmethod
    def _har(headers=None, query=None, post_text=None, cookies=None):
        return {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {
                            "method": "POST",
                            "url": "https://api.example.com/login",
                            "headers": headers or [],
                            "queryString": query or [],
                            "cookies": cookies or [],
                            "postData": {
                                "mimeType": "application/x-www-form-urlencoded",
                                "text": post_text or "user=bob",
                            },
                        },
                        "response": {
                            "status": 200,
                            "content": {
                                "mimeType": "application/json",
                                "text": '{"ok":true}',
                            },
                        },
                    }
                ],
            }
        }

    def test_query_string_credential_is_placeholder(self):
        har = self._har(
            query=[
                {"name": "access_token", "value": CANARY},
                {"name": "page", "value": "1"},
            ]
        )

        yaml_text = _dump(convert_har(har, "har_query", "test"))

        self.assertNotIn(CANARY, yaml_text, "HAR 查询串凭据明文入库")
        self.assertIn("page", yaml_text)

    def test_non_whitelisted_secret_header_is_placeholder(self):
        har = self._har(
            headers=[
                {"name": "token", "value": CANARY},
                {"name": "X-Signature", "value": CANARY},
            ]
        )

        yaml_text = _dump(convert_har(har, "har_hdr", "test"))

        self.assertNotIn(CANARY, yaml_text, "HAR 非白名单敏感头明文入库")

    def test_string_form_body_is_placeholder(self):
        har = self._har(post_text=f"user=bob&password={CANARY}")

        yaml_text = _dump(convert_har(har, "har_form", "test"))

        self.assertNotIn(CANARY, yaml_text, "HAR 字符串表单体明文入库")

    def test_authorization_and_cookie_still_masked(self):
        """回归：原本就脱敏的两类不能被改坏。"""
        har = self._har(
            headers=[
                {"name": "Authorization", "value": f"Bearer {CANARY}"},
                {"name": "Cookie", "value": f"session={CANARY}"},
            ]
        )

        yaml_text = _dump(convert_har(har, "har_auth", "test"))

        self.assertNotIn(CANARY, yaml_text)


class TestConverterCredentialLeakPostman(unittest.TestCase):
    """Postman 源：未被引用的集合级变量、查询串、非白名单敏感头会明文入库。"""

    @staticmethod
    def _collection(variables=None, header=None, query=None, raw_body=None):
        return {
            "info": {
                "name": "probe",
                "schema": (
                    "https://schema.getpostman.com/json/collection/"
                    "v2.1.0/collection.json"
                ),
            },
            "variable": variables or [],
            "item": [
                {
                    "name": "login",
                    "request": {
                        "method": "POST",
                        "header": header or [],
                        "url": {
                            "raw": "https://api.example.com/login",
                            "protocol": "https",
                            "host": ["api", "example", "com"],
                            "path": ["login"],
                            "query": query or [],
                        },
                        "body": {
                            "mode": "raw",
                            "raw": raw_body or '{"user":"bob"}',
                            "options": {"raw": {"language": "json"}},
                        },
                    },
                }
            ],
        }

    def test_unreferenced_collection_variable_is_not_plaintext(self):
        """集合级变量即使**一次都没被引用**也不能明文入库。

        修复前 `postman_adapter` 用 `name in used or name in base_variables` 过滤，
        于是所有集合变量都进 `config.variables`；而 `jwt`/`session` 这类名字
        不在它的敏感名清单里 → 明文 + **零告警**。
        """
        collection = self._collection(variables=[{"key": "jwt", "value": CANARY}])

        yaml_text = _dump(convert_postman(collection, "pm_var", "test"))

        self.assertNotIn(CANARY, yaml_text, "Postman 集合变量里的凭据明文入库")

    def test_query_string_credential_is_placeholder(self):
        collection = self._collection(
            query=[
                {"key": "access_token", "value": CANARY},
                {"key": "page", "value": "1"},
            ]
        )

        yaml_text = _dump(convert_postman(collection, "pm_query", "test"))

        self.assertNotIn(CANARY, yaml_text, "Postman 查询串凭据明文入库")

    def test_non_whitelisted_secret_header_is_placeholder(self):
        collection = self._collection(header=[{"key": "token", "value": CANARY}])

        yaml_text = _dump(convert_postman(collection, "pm_hdr", "test"))

        self.assertNotIn(CANARY, yaml_text, "Postman 非白名单敏感头明文入库")

    def test_json_body_password_is_placeholder(self):
        collection = self._collection(
            raw_body=json.dumps({"user": "bob", "password": CANARY})
        )

        yaml_text = _dump(convert_postman(collection, "pm_body", "test"))

        self.assertNotIn(CANARY, yaml_text)
        self.assertIn("bob", yaml_text)

    def test_non_secret_values_are_preserved(self):
        """反向断言：普通集合变量与参数必须保留。"""
        collection = self._collection(
            variables=[{"key": "baseUrl", "value": "https://api.example.com"}],
            query=[{"key": "page", "value": "1"}],
        )

        yaml_text = _dump(convert_postman(collection, "pm_clean", "test"))

        self.assertIn("https://api.example.com", yaml_text)
        self.assertIn("page", yaml_text)


class TestConverterCredentialLeakOpenapi(unittest.TestCase):
    """OpenAPI 源：**整条链路没有调用任何 sanitize**，是覆盖面最大的一处。"""

    @staticmethod
    def _doc(parameters=None, example=None):
        return {
            "openapi": "3.0.0",
            "info": {"title": "probe", "version": "1.0"},
            "servers": [{"url": "https://api.example.com"}],
            "paths": {
                "/login": {
                    "post": {
                        "tags": ["auth"],
                        "parameters": parameters or [],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"},
                                    "example": example or {"user": "bob"},
                                }
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }

    def test_query_parameter_credential_is_placeholder(self):
        doc = self._doc(
            parameters=[
                {
                    "name": "access_token",
                    "in": "query",
                    "required": True,
                    "example": CANARY,
                    "schema": {"type": "string"},
                }
            ]
        )

        yaml_text = _dump(convert_openapi(doc, "oa_query", "test"))

        self.assertNotIn(CANARY, yaml_text, "OpenAPI 查询参数凭据明文入库")

    def test_header_and_cookie_parameter_credentials_are_placeholders(self):
        doc = self._doc(
            parameters=[
                {
                    "name": "X-Signature",
                    "in": "header",
                    "required": True,
                    "example": CANARY,
                    "schema": {"type": "string"},
                },
                {
                    "name": "sid",
                    "in": "cookie",
                    "required": True,
                    "example": CANARY,
                    "schema": {"type": "string"},
                },
            ]
        )

        yaml_text = _dump(convert_openapi(doc, "oa_hdr", "test"))

        self.assertNotIn(CANARY, yaml_text, "OpenAPI header/cookie 参数凭据明文入库")

    def test_request_body_example_credential_is_placeholder(self):
        doc = self._doc(example={"user": "bob", "password": CANARY, "api_key": CANARY})

        yaml_text = _dump(convert_openapi(doc, "oa_body", "test"))

        self.assertNotIn(CANARY, yaml_text, "OpenAPI 请求体示例凭据明文入库")
        self.assertIn("bob", yaml_text, "非敏感示例值不该被处理掉")

    def test_security_scheme_placeholder_still_works(self):
        """回归：认证方案（bearer/apiKey）原本就占位，不能被改坏。"""
        doc = self._doc()
        doc["components"] = {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}
        }
        doc["security"] = [{"bearer": []}]

        yaml_text = _dump(convert_openapi(doc, "oa_sec", "test"))

        self.assertIn("${", yaml_text, "认证头应当仍是占位符")
        self.assertNotIn(CANARY, yaml_text)

    def test_non_secret_parameters_are_preserved(self):
        """反向断言：普通参数、示例值必须原样保留。"""
        doc = self._doc(
            parameters=[
                {
                    "name": "page",
                    "in": "query",
                    "required": True,
                    "example": "1",
                    "schema": {"type": "string"},
                }
            ],
            example={"user": "bob", "amount": 100},
        )

        yaml_text = _dump(convert_openapi(doc, "oa_clean", "test"))

        self.assertIn("page", yaml_text)
        self.assertIn("bob", yaml_text)
        self.assertIn("100", yaml_text)


# ===========================================================================
# 防复发不变量：脱敏必须走统一收口，不能退回"各适配器各自为政"
# ===========================================================================
class TestSanitizeInvariant(unittest.TestCase):
    """M5 的真正根因是**脱敏散落在四个适配器里**（于是 OpenAPI 整条链路漏掉了）。

    所以只修好当前的漏点是不够的——必须钉住"收口"这件事本身：
    新增一个适配器时如果忘了调统一收口，下面第一条测试会直接失败。
    """

    ADAPTER_ENTRY_POINTS = {
        "curl_adapter": "convert_curl_file",
        "har_adapter": "convert_har",
        "postman_adapter": "convert_postman",
        "openapi_adapter": "convert_openapi",
    }

    def test_every_adapter_calls_the_central_sanitizer(self):
        """每个适配器的入口函数都必须调用 `sanitize_case` / `sanitize_cases`。"""
        import importlib
        import inspect

        for module_name, func_name in self.ADAPTER_ENTRY_POINTS.items():
            with self.subTest(adapter=module_name):
                module = importlib.import_module(
                    f"interfacetester.converters.{module_name}"
                )
                source = inspect.getsource(getattr(module, func_name))

                self.assertTrue(
                    "sanitize_case" in source or "sanitize_cases" in source,
                    f"{module_name}.{func_name} 没有调用统一收口的脱敏函数"
                    f"（新增适配器时最容易漏的就是这一步）",
                )

    def test_no_second_sensitive_name_list(self):
        """`converters/ir.py` 不得再维护第二份"什么是凭据"清单。

        修复前它有两份（`SECRET_HEADER_PATTERN` 锚定白名单 + `SECRET_FIELD_PATTERN`
        子串），与 `utils.SENSITIVE_KEYWORDS` 各自漂移 —— 这正是漏判的来源。
        现在判定统一走 `utils.is_sensitive_key`。
        """
        import inspect

        from interfacetester.converters import ir

        source = inspect.getsource(ir)

        self.assertNotIn("SECRET_HEADER_PATTERN", source)
        self.assertNotIn("SECRET_FIELD_PATTERN", source)
        self.assertIn("is_sensitive_key", source)

    def test_runtime_masking_has_a_single_predicate(self):
        """运行期脱敏同理：只允许走 `utils.is_sensitive_key`。"""
        import inspect

        from interfacetester import client, step_request

        for module in (client, step_request):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertTrue(
                    "mask_sensitive" in source or "interfacetester.utils" in source,
                    f"{module.__name__} 里找不到请求侧脱敏的调用",
                )


# ===========================================================================
# H10（0918-8）：URL 里的 userinfo 是凭据，不得明文入库
# ===========================================================================
class TestConverterCredentialLeakUrlUserinfo(unittest.TestCase):
    """`https://user:pass@host/…` 的 userinfo 必须剥掉并转成 `Authorization` 占位。

    NOTICE（修复前实测）：四个源**都没有**处理 userinfo，而 `split_url()` 把整段 `netloc`
    当 `base_url`，于是明文口令直接落进生成物：

        config:
          base_url: https://alice:S3cr3tP%40ssw0rd@api.example.com

    而生成文件头还写着「敏感信息（Cookie/Authorization/token 等）已替换成 ${...} 占位」
    —— 属于**主动误导**复核者。curl / HAR / Postman 三源都能复现（OpenAPI 的 `servers`
    通常不含 userinfo，所以这里三个源各一条）。
    """

    USERNAME = "alice"
    URL = f"https://alice:{CANARY}@api.example.com/v1/me"

    def _assert_clean(self, yaml_text: str) -> None:
        self.assertNotIn(CANARY, yaml_text, "URL userinfo 里的口令写进了生成 YAML")
        self.assertNotIn(self.USERNAME, yaml_text, "URL userinfo 里的用户名写进了生成 YAML")
        self.assertIn(
            "base_url: https://api.example.com",
            yaml_text,
            "userinfo 没有被从 base_url 里剥掉",
        )
        self.assertIn("AUTH_BASIC: ${ENV(AUTH_BASIC)}", yaml_text)
        self.assertIn("Authorization: Basic ${AUTH_BASIC}", yaml_text)

    def test_curl_userinfo_is_stripped_and_placeholder(self):
        yaml_text = _dump(
            convert_curl_file(f"curl -X GET '{self.URL}'", "curl_ui", "test")
        )
        self._assert_clean(yaml_text)

    def test_har_userinfo_is_stripped_and_placeholder(self):
        har = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "request": {
                            "method": "GET",
                            "url": self.URL,
                            "headers": [],
                            "queryString": [],
                            "cookies": [],
                        },
                        "response": {
                            "status": 200,
                            "content": {"mimeType": "application/json", "text": "{}"},
                        },
                    }
                ],
            }
        }
        yaml_text = _dump(convert_har(har, "har_ui", "test"))
        self._assert_clean(yaml_text)

    def test_postman_userinfo_is_stripped_and_placeholder(self):
        collection = {
            "info": {
                "name": "probe",
                "schema": (
                    "https://schema.getpostman.com/json/collection/"
                    "v2.1.0/collection.json"
                ),
            },
            "item": [
                {
                    "name": "me",
                    "request": {"method": "GET", "url": self.URL},
                }
            ],
        }
        yaml_text = _dump(convert_postman(collection, "pm_ui", "test"))
        self._assert_clean(yaml_text)

    def test_warning_tells_what_happened(self):
        """告警要说清「凭据被搬到哪里去了」，否则用户会以为凭据丢了。"""
        result = convert_curl_file(f"curl -X GET '{self.URL}'", "curl_ui_warn", "test")

        joined = _warnings(result)

        self.assertIn("userinfo", joined)
        self.assertIn("AUTH_BASIC", joined)
        self.assertNotIn(CANARY, joined, "告警文本本身不能回显明文")

    def test_existing_authorization_header_is_not_overwritten(self):
        """反向：步骤里**已经有** Authorization 时，只丢弃 URL 里的 userinfo 并告警。

        覆盖会让用户显式写的头被 URL 里的凭据顶掉——那是更隐蔽的错误。
        """
        content = (
            f"curl -X GET '{self.URL}' "
            f"-H 'Authorization: Bearer {CANARY}'"
        )

        result = convert_curl_file(content, "curl_ui_conflict", "test")
        step = _first(result).steps[0]

        self.assertEqual(step.headers["Authorization"], "Bearer ${AUTH_TOKEN}")
        self.assertTrue(
            any("已有 Authorization" in warning for warning in step.warnings),
            step.warnings,
        )

    def test_split_userinfo_only_touches_netloc_and_decodes_percent(self):
        """`split_userinfo` 的边界：只动 netloc、解码百分号、无 userinfo 原样返回。"""
        from interfacetester.converters.ir import split_userinfo

        cleaned, userinfo = split_userinfo(
            "https://alice:S3cr3tP%40ssw0rd@api.example.com/v1/me?a=b@c"
        )
        self.assertEqual(cleaned, "https://api.example.com/v1/me?a=b@c")
        self.assertEqual(userinfo, "alice:S3cr3tP@ssw0rd", "userinfo 必须先解码再算 base64")

        # 路径/查询里的 `@` 不是 userinfo
        for url in (
            "https://api.example.com/a@b",
            "https://api.example.com/x?mail=a@b",
            "https://api.example.com/x",
            "/relative/path@x",
            "",
        ):
            with self.subTest(url=url):
                self.assertEqual(split_userinfo(url), (url, None))

    def test_base64_matches_the_decoded_userinfo(self):
        """凭据必须**真的还能用**：占位符背后应当是 `base64(解码后的 user:pass)`。

        难点：base64 值会被 `sanitize_headers` 立刻换成 `${AUTH_BASIC}`，从产物上看不到。
        所以这里在**衔接处**打桩（`_sanitize_header_value` 的入参），断言喂进去的正是
        `Basic base64(user:pass)`——这是「凭据没被搬错」唯一可观测的证据点。

        NOTICE（批次 9-1）：本用例现在**两种写法都跑**。修复前只有 URL userinfo 那条路径
        真的做了 base64；`curl -u` 那条放的是 `Basic alice:原样密码` ——
        因为下游会把整个值换成 `Basic ${AUTH_BASIC}`，**产物上完全看不出差别**，
        所以下面那条 YAML 等价性用例一直是绿的（它比的是**占位后**的产物）。
        于是「环境变量 AUTH_BASIC 应当填什么」在两条路径上语义不同，谁照警告文案填谁填错。
        打桩点是唯一能分辨的地方 —— 这正是本用例存在的理由。
        """
        import base64

        from interfacetester.converters import ir as ir_module

        expect_encoded = base64.b64encode(f"alice:{CANARY}".encode("utf-8")).decode(
            "ascii"
        )
        # 两种写法：URL userinfo / `-u`（curl 里它们语义相同）
        commands = {
            "userinfo": f"curl -X GET '{self.URL}'",
            "dash_u": f"curl -X GET 'https://api.example.com/v1/me' -u 'alice:{CANARY}'",
        }

        for label, command in commands.items():
            with self.subTest(form=label):
                captured: dict = {}
                original = ir_module._sanitize_header_value

                def spy(name, value):
                    if str(name).lower() == "authorization":
                        captured["value"] = value
                    return original(name, value)

                with mock.patch.object(
                    ir_module, "_sanitize_header_value", side_effect=spy
                ):
                    convert_curl_file(command, "curl_b64", "test")

                self.assertEqual(
                    captured.get("value"),
                    f"Basic {expect_encoded}",
                    f"{label} 形态没有按 base64 组装 Authorization（凭据会被搬错）",
                )

    def test_userinfo_form_equals_dash_u_form(self):
        """等价性：`https://user:pass@host/x` 与 `--user user:pass https://host/x` 是同一件事。

        NOTICE（批次 9-1）：本用例只比**占位后**的产物，因此**不能**用来证明两条路径的内部
        表示一致（修复前它照样是绿的）——「内部表示一致」由上面
        `test_base64_matches_the_decoded_userinfo` 的打桩断言负责。
        """
        from_url = convert_curl_file(f"curl -X GET '{self.URL}'", "same", "test")
        from_u = convert_curl_file(
            f"curl -X GET 'https://api.example.com/v1/me' -u 'alice:{CANARY}'",
            "same",
            "test",
        )

        self.assertEqual(
            _dump(from_url),
            _dump(from_u),
            "userinfo 与 -u/--user 应当产出完全相同的 YAML（同一个占位链路）",
        )


class TestExportVarsDisplayCopyIsMasked(unittest.TestCase):
    """0919-13 批次 E（§二.2）：`export_vars` 的**展示副本**脱敏，运行期取值保持真值。

    修复前的耦合：`step_testcase` 把 `summary.in_out.export_vars` 当**运行期取值源**
    （再经 `runner.py` 的 `self.__session_variables.update(step_result.export_vars)`
    传给后续步骤与被引用用例）。于是「报告里的展示副本」和「运行期传递的值」是**同一个
    对象**——`get_summary()` 一旦脱敏，被引用用例的 export 就变成 `******`，
    静默破坏变量传递。当时只能选择**不脱敏**（`config_vars` 早就脱敏了，两边口径分裂）。

    现在拆成两条通道，本组用例**两个方向都测**：
      · 展示副本（summary）：敏感键名 → `******`；非敏感键名 → 原样（不过度脱敏）；
      · 运行期（变量传递）：下游步骤必须拿到**真值** —— 用 CANARY 回显来证明。
    """

    @staticmethod
    def _run_referring_case():
        """A 用例导出两个变量 → B 用例引用它，并在后续步骤里回显 `$token`。"""

        class _ExportingCase(InterfaceTester):
            config = (
                Config("exporting case")
                .base_url(HTTP_BIN_URL)
                .export("token", "order_no")
            )
            teststeps = [
                Step(
                    RunRequest("extract two vars")
                    .get("/get")
                    .with_params(token=CANARY, order_no="ORDER-12345")
                    .extract()
                    .with_jmespath("body.args.token", "token")
                    .with_jmespath("body.args.order_no", "order_no")
                    .validate()
                    .assert_equal("status_code", 200)
                )
            ]

        class _ReferringCase(InterfaceTester):
            config = Config("referring case").base_url(HTTP_BIN_URL)
            teststeps = [
                Step(RunTestCase("call exporting case").call(_ExportingCase)),
                Step(
                    RunRequest("use the exported token downstream")
                    .get("/get?seen=$token")
                    .validate()
                    .assert_equal("status_code", 200)
                    # 回显必须等于**真值**——若运行期拿到的是 `******`，这条会失败
                    .assert_equal("body.args.seen", CANARY)
                ),
            ]

        runner = _ReferringCase().test_start()
        return runner, runner.get_summary()

    def test_display_copy_of_step_export_vars_is_masked(self):
        """展示副本：敏感键名糊掉、非敏感键名原样（**两个方向同时测**）。

        必须覆盖 **step 级**的 `export_vars`（不只是 `in_out`）：
        `step_results` 会整体进 summary.json 的 `records`，只脱敏 `in_out`
        就是「看起来脱敏了，其实没有」。
        """
        _runner, summary = self._run_referring_case()

        exported = summary.step_results[0].export_vars
        self.assertEqual(exported.get("token"), "******")
        self.assertEqual(
            exported.get("order_no"),
            "ORDER-12345",
            "非敏感变量被一起糊掉了——过度脱敏会让报告失去排查依据",
        )

    def test_variable_channels_contain_no_canary(self):
        """canary 手法（**限定在变量通道内**）：变量副本里不得出现明文。

        NOTICE: 不能在**整份** summary 里搜 canary。本组用例故意让响应回显 `$token`
        （`?seen=$token`，用来证明运行期拿到的是真值），而**响应侧不脱敏**是本仓
        已登记的边界（见本文件顶部 NOTICE）；另外 `seen` 不是敏感字段名，
        「按名脱敏」本来也不会命中它。所以这里只扫**变量通道** —— 那才是本批要保证的地方。

        非空跑自检：先证明「运行期确实有明文」（否则下面的断言等于白跑），
        再证明展示副本里没有。
        """
        runner, summary = self._run_referring_case()

        runtime_dump = json.dumps(
            runner.merge_step_variables({}), ensure_ascii=False, default=str
        )
        self.assertIn(CANARY, runtime_dump, "运行期没有明文——本用例失去意义")

        displayed = {
            "in_out": summary.in_out.model_dump(),
            "step_export_vars": [sr.export_vars for sr in summary.step_results],
        }
        displayed_dump = json.dumps(displayed, ensure_ascii=False)
        self.assertNotIn(CANARY, displayed_dump, "变量通道里出现了明文凭据")
        self.assertIn(
            "******", displayed_dump, "既没明文也没掩码——断言没覆盖到变量通道"
        )

    def test_runtime_value_stays_real(self):
        """运行期：会话变量里必须仍是**真值**（变量传递的命根子）。

        两处独立证据：
          1. `merge_step_variables({})` 能读到真值（它就是步骤解析变量时用的入口）；
          2. 被引用用例的第二步把 `$token` 回显出来并断言等于 CANARY（端到端）。
             用例整体成功即证明第 2 条 —— 若运行期拿到 `******`，那条断言会失败。
        """
        runner, summary = self._run_referring_case()

        self.assertTrue(summary.success)
        self.assertEqual(runner.merge_step_variables({}).get("token"), CANARY)

    def test_get_summary_does_not_mutate_runtime_objects(self):
        """取 summary 不能**原地**改写运行期对象（这是最容易犯的错）。

        症状：第一次取 summary 时把 `export_vars` 就地换成 `******`，
        于是后续步骤/被引用用例拿到掩码值。这里连取两次 summary 并再次确认运行期值。
        """
        runner, first = self._run_referring_case()
        second = runner.get_summary()

        self.assertEqual(
            first.step_results[0].export_vars, second.step_results[0].export_vars
        )
        self.assertEqual(runner.merge_step_variables({}).get("token"), CANARY)

    def test_in_out_display_copy_is_masked_too(self):
        """用例**自己**导出敏感变量时：运行期真值 与 `in_out` 展示副本 各是各的。"""

        class _SelfExportingCase(InterfaceTester):
            config = Config("self exporting").base_url(HTTP_BIN_URL).export("token")
            teststeps = [
                Step(
                    RunRequest("extract then export")
                    .get("/status/200")
                    .extract()
                    .with_jmespath("status_code", "token")
                    .validate()
                    .assert_equal("status_code", 200)
                )
            ]

        runner = _SelfExportingCase().test_start()

        runtime = runner.get_export_variables(strict=True)
        display = runner.get_summary().in_out.export_vars

        self.assertEqual(runtime, {"token": 200}, "运行期必须拿到真值")
        self.assertEqual(display, {"token": "******"}, "展示副本必须脱敏")
        self.assertIsNot(runtime, display, "展示副本应当是拷贝，不是运行期那个对象")


# ===========================================================================
# 0920 批次 2 / N15：**响应侧**凭据脱敏
# ===========================================================================
class TestRuntimeResponseCredentialMasking(unittest.TestCase):
    """N15：响应头 / cookie / 体里**按键名判定的敏感字段**此前一个字都没脱敏。

    修复前实测（`.tmp_audit/verify_aud3.py`，服务端返回 `Set-Cookie: sessionid=CANARY`、
    `X-Auth-Token: CANARY`、体里 `{"access_token": CANARY}`）：

    ```text
    同一个请求、同一条记录：
      请求侧  Authorization: "******"        ← 早就脱敏了（M4）
      响应侧  Set-Cookie:    "sessionid=CANARY..."   ← 明文
              body:          {"access_token": "CANARY..."}  ← 明文
      → 7 次出现在 .run.log（→ 还有 summary.json / HTML / Allure）
    ```

    口径（**不是**"响应全糊"，与模块 docstring 的边界一致）：
      · 响应**头/cookie** 与响应体里的**敏感键名**按 `is_sensitive_key` 脱敏；
      · **非敏感字段逐字保留**（`Server`/业务字段）—— 服务端到底返回了什么，
        是排查问题的主要信息，不能抹掉；
      · **断言素材不受影响**：`ResponseObject` 拿的是原始 `resp`，
        extract / validate 仍打向未脱敏的真值（本组用例两个方向都测）。
    """

    def _run_case(self, steps):
        captured = []
        sink_id = logger.add(captured.append, level="DEBUG", format="{message}")

        class _RespLeakProbeCase(InterfaceTester):
            config = Config("resp leak probe").base_url(HTTP_BIN_URL)
            teststeps = list(steps)

        try:
            runner = _RespLeakProbeCase().test_start()
        finally:
            logger.remove(sink_id)

        summary = runner.get_summary()
        with open(summary.log, encoding="utf-8") as f:
            log_text = f.read()

        return runner, summary, log_text, "\n".join(str(m) for m in captured)

    @staticmethod
    def _set_cookie_step():
        """`/cookies/set` 会回 `Set-Cookie: sessionid=CANARY`（响应侧凭据的典型形态）。"""
        return Step(
            RunRequest("trigger a Set-Cookie response")
            .get(f"/cookies/set?sessionid={CANARY}")
            .validate()
            .assert_equal("status_code", 200)
        )

    def test_response_cookie_is_not_leaked_to_run_log(self):
        _runner, _summary, log_text, stdout_text = self._run_case(
            [self._set_cookie_step()]
        )

        self.assertNotIn(
            CANARY,
            log_text,
            "响应侧凭据（Set-Cookie）明文进了 .run.log —— 这正是修复前的 N15",
        )
        self.assertNotIn(CANARY, stdout_text, "响应侧凭据明文进了 stdout")

    def test_response_cookie_is_not_leaked_to_summary(self):
        _runner, summary, _log_text, _stdout = self._run_case([self._set_cookie_step()])

        payload = json.dumps(summary.model_dump(), ensure_ascii=False, default=str)
        self.assertNotIn(
            CANARY,
            payload,
            "响应侧凭据明文进了 summary（会随 summary.json / HTML / Allure 外泄）",
        )

    def test_response_sensitive_fields_are_masked_in_record(self):
        """记录里的响应头/cookie 敏感键必须是 `******`（不是"整块被删掉"）。"""
        _runner, summary, _log_text, _stdout = self._run_case([self._set_cookie_step()])

        response = summary.step_results[0].data.req_resps[0].response
        self.assertIn("Set-Cookie", response.headers, "响应头不该被整块丢掉")
        self.assertEqual(response.headers["Set-Cookie"], "******")
        self.assertEqual(response.cookies.get("sessionid"), "******")

    def test_non_sensitive_response_fields_are_preserved(self):
        """反向护栏：'不要为了安全把排查信息也抹掉'。"""
        _runner, summary, log_text, _stdout = self._run_case([self._set_cookie_step()])

        response = summary.step_results[0].data.req_resps[0].response
        # 非敏感头必须还在，且**只**糊掉敏感的那一个
        # NOTICE: `/cookies/set` 回的是 302（requests 会跟到 `/cookies`），
        # 记录里那条是**重定向那一跳**，它本来就没有 Content-Type。
        self.assertEqual(response.status_code, 302)
        self.assertIn("Server", response.headers)
        self.assertIn("Location", response.headers, "非敏感头不该被丢掉")
        self.assertIn("Date", response.headers)
        self.assertEqual(
            response.headers["Set-Cookie"], "******", "敏感头没被脱敏"
        )
        self.assertIn("Server", log_text)
        self.assertIn("******", log_text, "脱敏痕迹本身要可见（否则用户以为没脱敏）")

    def test_server_echoed_body_field_is_masked_by_key_name(self):
        """体里**按键名**判定的敏感字段（`access_token`）也要脱敏。

        用 `/get?access_token=…`：mock 会把它回显在 `body.args` 里，
        键名命中 `SENSITIVE_KEYWORDS`，因此必须变成 `******`。
        """
        step = Step(
            RunRequest("echo a token through the query string")
            .get(f"/get?access_token={CANARY}&order_no=ORDER-1")
            .validate()
            .assert_equal("status_code", 200)
        )

        _runner, summary, log_text, _stdout = self._run_case([step])

        payload = json.dumps(summary.model_dump(), ensure_ascii=False, default=str)
        self.assertNotIn(CANARY, payload, "体里按键名命中的敏感字段仍明文")
        self.assertNotIn(CANARY, log_text, "体里按键名命中的敏感字段仍明文进日志")
        # 非敏感字段照旧可见
        self.assertIn("ORDER-1", log_text)

    def test_assertions_still_see_the_real_response_values(self):
        """**最关键的反向护栏**：脱敏只作用于展示副本，断言必须打向真值。

        若把 `ResponseObject` 的素材也糊了，`assert_equal("body.args.token", CANARY)`
        会失败 → 本用例红。
        """
        step = Step(
            RunRequest("assert on the real echoed value")
            .get(f"/get?token={CANARY}")
            .validate()
            .assert_equal("status_code", 200)
            # 断言素材是**真值**：脱敏若误伤运行期，这条必失败
            .assert_equal("body.args.token", CANARY)
        )

        _runner, summary, _log_text, _stdout = self._run_case([step])

        self.assertTrue(summary.success, "断言没能读到真实响应值（脱敏误伤了断言素材）")

    def test_extract_still_yields_the_real_value(self):
        """extract 也必须拿到真值（脱敏误伤会让下游步骤拿到 `******`）。"""
        steps = [
            Step(
                RunRequest("extract the echoed token")
                .get(f"/get?token={CANARY}")
                .extract()
                .with_jmespath("body.args.token", "got_token")
                .validate()
                .assert_equal("status_code", 200)
            ),
            Step(
                RunRequest("use the extracted token as a query param")
                .get("/get?seen=$got_token")
                .validate()
                .assert_equal("status_code", 200)
                # 回显必须等于真值 —— 若 extract 拿到 `******`，这条失败
                .assert_equal("body.args.seen", CANARY)
            ),
        ]

        _runner, summary, _log_text, _stdout = self._run_case(steps)

        self.assertTrue(summary.success, "extract 拿到了脱敏值而不是真值")


def _allure_available() -> bool:
    """装了 `allure-pytest` 才跑 Allure 端到端用例（可选依赖，与本仓口径一致）。"""
    import importlib.util  # noqa: PLC0415

    return importlib.util.find_spec("allure") is not None


@unittest.skipUnless(_allure_available(), "未安装 allure-pytest（可选依赖 [allure]）")
class TestAllureAttachmentsDoNotLeakCredentials(unittest.TestCase):
    """0920 收尾：Allure 附件的**真端到端**核对（N15 的最后一块）。

    此前 `sensitive_data_leak_test` 的模块 docstring 把它列为"不覆盖：同一份文本"——
    那是**推断**，不是实测。收尾时把 `.venv` 装上 `allure-pytest` 后真跑了一遍，
    结论是"成立"，本用例把它**钉住**。

    做法：自建一个回凭据的服务，真跑 `hrun --alluredir=…`，然后扫
    **结果目录里的每一个文件**（附件正文 + `result.json`）：

    - 机密 canary **一次都不许出现**；
    - 但 `******` 必须出现（否则可能是"整体没脱敏"或"啥都没挂"）；
    - 非敏感字段（`order_no`）必须**逐字保留**（脱敏不能把排查信息也抹掉）。
    """

    def setUp(self):
        import threading  # noqa: PLC0415
        from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: PLC0415

        canary = CANARY

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                payload = json.dumps(
                    {
                        "access_token": canary,
                        "order_no": "ORDER-VISIBLE-1",
                        "note": "login ok",
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"sessionid={canary}; Path=/")
                self.send_header("X-Auth-Token", canary)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _reply
            do_POST = _reply

            def log_message(self, *args):  # noqa: A002
                pass

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.addCleanup(self._server.shutdown)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def test_allure_output_contains_no_plaintext_credentials(self):
        import subprocess  # noqa: PLC0415
        import uuid  # noqa: PLC0415

        # NOTICE: 用 `logs/` 下的临时目录（本仓惯用落点，已被 .gitignore 覆盖）——
        # 系统 temp 目录在某些受控环境下不可写。
        tmp_dir = os.path.join(
            os.getcwd(), "logs", f"tmp_allure_{uuid.uuid4().hex[:8]}"
        )
        os.makedirs(tmp_dir, exist_ok=True)
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        with open(os.path.join(tmp_dir, "debugtalk.py"), "w", encoding="utf-8") as f:
            f.write("")

        case_path = os.path.join(tmp_dir, "case.yml")
        with open(case_path, "w", encoding="utf-8") as f:
            f.write(
                "config:\n"
                "  name: allure masking probe\n"
                f"  base_url: {self.base_url}\n"
                "teststeps:\n"
                "- name: login\n"
                "  request:\n"
                "    method: POST\n"
                "    url: /login\n"
                "    headers:\n"
                f'      Authorization: "Bearer {CANARY}"\n'
                "    json:\n"
                "      username: bob\n"
                f"      password: {CANARY}\n"
                "  extract:\n"
                "    got_token: body.access_token\n"
                "  validate:\n"
                "  - eq: [status_code, 200]\n"
                "  - eq: [body.order_no, ORDER-VISIBLE-1]\n"
            )

        allure_dir = os.path.join(tmp_dir, "allure-results")
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        hrun = os.path.join(repo_root, ".venv", "Scripts", "hrun.exe")
        if not os.path.isfile(hrun):  # pragma: no cover - 非 Windows 布局
            hrun = os.path.join(repo_root, ".venv", "bin", "hrun")

        proc = subprocess.run(
            [hrun, case_path, f"--alluredir={allure_dir}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout[-800:])
        self.assertTrue(
            os.path.isdir(allure_dir), f"没有产出 allure-results：{proc.stdout[-500:]}"
        )

        files = sorted(os.listdir(allure_dir))
        self.assertGreaterEqual(len(files), 3, f"Allure 结果文件太少：{files}")

        joined = ""
        for name in files:
            with open(os.path.join(allure_dir, name), encoding="utf-8", errors="replace") as f:
                joined += f.read()

        self.assertNotIn(
            CANARY,
            joined,
            "Allure 产物里出现了明文凭据（附件与 result.json 都要查）",
        )
        self.assertIn(
            "******",
            joined,
            "Allure 产物里一个脱敏痕迹都没有 —— 脱敏可能整体失效、或附件根本没挂上",
        )
        # 脱敏不能把排查信息也抹掉
        self.assertIn("ORDER-VISIBLE-1", joined)
        # 响应侧的敏感字段确实被处理过（N15 的核心）
        self.assertIn("Set-Cookie", joined)


