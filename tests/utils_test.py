import decimal
import json
import os
import unittest
from unittest import mock
from pathlib import Path

import toml
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from interfacetester import __version__, exceptions, loader, utils
from interfacetester.utils import ExtendJSONEncoder, merge_variables, ga4_client


class TestUtils(unittest.TestCase):
    def test_set_os_environ(self):
        self.assertNotIn("abc", os.environ)
        variables_mapping = {"abc": "123"}
        utils.set_os_environ(variables_mapping)
        self.assertIn("abc", os.environ)
        self.assertEqual(os.environ["abc"], "123")

    def test_validators(self):
        from interfacetester.builtin import comparators

        functions_mapping = loader.load_module_functions(comparators)

        functions_mapping["equal"](None, None)
        functions_mapping["equal"](1, 1)
        functions_mapping["equal"]("abc", "abc")
        with self.assertRaises(AssertionError):
            functions_mapping["equal"]("123", 123)

        functions_mapping["less_than"](1, 2)
        functions_mapping["less_or_equals"](2, 2)

        functions_mapping["greater_than"](2, 1)
        functions_mapping["greater_or_equals"](2, 2)

        functions_mapping["not_equal"](123, "123")

        functions_mapping["length_equal"]("123", 3)
        # NOTICE（0921-3 / 轻微项 1）：`length_equal("123", "3")` 从"必须抛 AssertionError"
        # 改成**接受**——`.env` / CSV / `${ENV(...)}` 出来的值永远是字符串，
        # 这正是 L2「变量来的数字要转」那一族的遗漏。非数字串仍然报错（见
        # `TestBatch0921_3ComparatorConsistency` 的失败面用例）。
        functions_mapping["length_equal"]("123", "3")
        with self.assertRaises(exceptions.ParamsError):
            functions_mapping["length_equal"]("123", "abc")
        functions_mapping["length_greater_than"]("123", 2)
        functions_mapping["length_greater_or_equals"]("123", 3)

        functions_mapping["contains"]("123abc456", "3ab")
        functions_mapping["contains"](["1", "2"], "1")
        functions_mapping["contains"]({"a": 1, "b": 2}, "a")
        functions_mapping["contained_by"]("3ab", "123abc456")
        functions_mapping["contained_by"](0, [0, 200])

        # NOTICE（0918-8 顺带订正）：写成 raw string —— `"^123\w+456$"` 里的 `\w`
        # 是**非法转义序列**，Python 会报 `SyntaxWarning: invalid escape sequence '\w'`
        # （现在只是噪声，将来版本会变硬错误）。正则里的转义一律用 r"" 最省事。
        functions_mapping["regex_match"]("123abc456", r"^123\w+456$")
        with self.assertRaises(AssertionError):
            functions_mapping["regex_match"]("123abc456", "^12b.*456$")

        functions_mapping["startswith"]("abc123", "ab")
        functions_mapping["startswith"]("123abc", 12)
        functions_mapping["startswith"](12345, 123)

        functions_mapping["endswith"]("abc123", 23)
        functions_mapping["endswith"]("123abc", "abc")
        functions_mapping["endswith"](12345, 45)

        functions_mapping["type_match"](580509390, int)
        functions_mapping["type_match"](580509390, "int")
        functions_mapping["type_match"]([], list)
        functions_mapping["type_match"]([], "list")
        functions_mapping["type_match"]([1], "list")
        functions_mapping["type_match"]({}, "dict")
        functions_mapping["type_match"]({"a": 1}, "dict")
        functions_mapping["type_match"](None, "None")
        functions_mapping["type_match"](None, "NoneType")
        functions_mapping["type_match"](None, None)

    def test_lower_dict_keys(self):
        request_dict = {
            "url": "http://127.0.0.1:5000",
            "METHOD": "POST",
            "Headers": {"Accept": "application/json", "User-Agent": "ios/9.3"},
        }
        new_request_dict = utils.lower_dict_keys(request_dict)
        self.assertIn("method", new_request_dict)
        self.assertIn("headers", new_request_dict)
        self.assertIn("Accept", new_request_dict["headers"])
        self.assertIn("User-Agent", new_request_dict["headers"])

        request_dict = "$default_request"
        new_request_dict = utils.lower_dict_keys(request_dict)
        self.assertEqual("$default_request", request_dict)

        request_dict = None
        new_request_dict = utils.lower_dict_keys(request_dict)
        self.assertEqual(None, request_dict)

    def test_print_info(self):
        info_mapping = {"a": 1, "t": (1, 2), "b": {"b1": 123}, "c": None, "d": [4, 5]}
        utils.print_info(info_mapping)

    def test_sort_dict_by_custom_order(self):
        self.assertEqual(
            list(
                utils.sort_dict_by_custom_order(
                    {"C": 3, "D": 2, "A": 1, "B": 8}, ["A", "D"]
                ).keys()
            ),
            ["A", "D", "C", "B"],
        )

    def test_safe_dump_json(self):
        class A(object):
            pass

        data = {"a": A(), "b": decimal.Decimal("1.45")}

        with self.assertRaises(TypeError):
            json.dumps(data)

        json.dumps(data, cls=ExtendJSONEncoder)

    def test_override_config_variables(self):
        step_variables = {"base_url": "$base_url", "foo1": "bar1"}
        config_variables = {"base_url": "https://postman-echo.com", "foo1": "bar111"}
        self.assertEqual(
            merge_variables(step_variables, config_variables),
            {"base_url": "https://postman-echo.com", "foo1": "bar1"},
        )

    def test_cartesian_product_one(self):
        parameters_content_list = [[{"a": 1}, {"a": 2}]]
        product_list = utils.gen_cartesian_product(*parameters_content_list)
        self.assertEqual(product_list, [{"a": 1}, {"a": 2}])

    def test_cartesian_product_multiple(self):
        parameters_content_list = [
            [{"a": 1}, {"a": 2}],
            [{"x": 111, "y": 112}, {"x": 121, "y": 122}],
        ]
        product_list = utils.gen_cartesian_product(*parameters_content_list)
        self.assertEqual(
            product_list,
            [
                {"a": 1, "x": 111, "y": 112},
                {"a": 1, "x": 121, "y": 122},
                {"a": 2, "x": 111, "y": 112},
                {"a": 2, "x": 121, "y": 122},
            ],
        )

    def test_cartesian_product_empty(self):
        parameters_content_list = []
        product_list = utils.gen_cartesian_product(*parameters_content_list)
        self.assertEqual(product_list, [])

    def test_versions_are_in_sync(self):
        """版本号唯一事实来源是 interfacetester.__version__，pyproject.toml 必须动态引用它。

        修复前 version 在 pyproject.toml 与 __init__.py 双份维护，存在漂移风险；
        现改为 pyproject 用 dynamic + tool.setuptools.dynamic.version.attr 读取，
        因此这里断言的是「引用关系正确」，而不是「两处字面量相等」。
        """

        path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        # pyproject.toml 按 TOML 规范必须是 UTF-8，这里显式指定编码，
        # 否则在 Windows 上会按默认 GBK 解码而报 UnicodeDecodeError。
        pyproject = toml.loads(path.read_text(encoding="utf-8"))
        # [project] 里不应再出现写死的 version，否则又变成双份维护
        self.assertNotIn("version", pyproject["project"])
        self.assertIn("version", pyproject["project"]["dynamic"])
        self.assertEqual(
            pyproject["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "interfacetester.__version__"},
        )
        # 构建期静态读取的 attr 必须真的存在、且是**可解析的版本号**。
        # NOTICE: 这里刻意**不**钉死某个字面量（原为 `__version__ == "4.3.5"`）——
        # 版本号升级本是常规动作，钉死字面量会让每次发版都必须改测试，
        # 而那正是本仓最忌讳的「测试守护的不是行为，而是某个会过期的值」。
        # 改成钉「形态」：能被 packaging 解析成合法版本、且是三段式。
        from packaging.version import Version

        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$", "版本号应为 X.Y.Z 形态")
        self.assertEqual(
            str(Version(__version__)), __version__, "版本号必须能被 packaging 规范化解析"
        )

    def test_mask_sensitive_variables(self):
        variables = {
            "password": "123456",
            "username": "user1",
            "headers": {"Authorization": "Bearer abc", "X-Trace": "t1"},
            "accounts": [
                {"username": "user1", "password": "p1"},
                {"username": "user2", "access_token": "t2"},
            ],
            "pair": ({"secret_key": "s1"}, "plain"),
            "nested_lists": [[{"api_key": "k1"}]],
        }

        masked = utils.mask_sensitive_variables(variables)

        self.assertEqual(masked["password"], utils.MASKED_VALUE)
        self.assertEqual(masked["username"], "user1")
        self.assertEqual(masked["headers"]["Authorization"], utils.MASKED_VALUE)
        self.assertEqual(masked["headers"]["X-Trace"], "t1")
        # NOTICE: 修复前只递归 dict，列表/元组里的字典整体漏过（明文进报告）
        self.assertEqual(masked["accounts"][0]["password"], utils.MASKED_VALUE)
        self.assertEqual(masked["accounts"][0]["username"], "user1")
        self.assertEqual(masked["accounts"][1]["access_token"], utils.MASKED_VALUE)
        self.assertEqual(masked["pair"][0]["secret_key"], utils.MASKED_VALUE)
        self.assertEqual(masked["pair"][1], "plain")
        self.assertEqual(masked["nested_lists"][0][0]["api_key"], utils.MASKED_VALUE)
        # 容器类型与结构保持原样
        self.assertIsInstance(masked["accounts"], list)
        self.assertIsInstance(masked["pair"], tuple)
        self.assertIsInstance(masked["nested_lists"][0], list)
        # 不修改入参
        self.assertEqual(variables["accounts"][0]["password"], "p1")

    def test_ga4_send_event(self):
        ga4_client.send_event(
            "interfacetester_debug_event",
            {
                "a": 123,
                "b": 456,
            },
        )

    def test_ga4_disabled_client_does_not_build_a_session(self):
        """0919-1 / L31：GA 已禁用时不得在**构造期**白建 `requests.Session()`。

        `interfacetester/utils.py` 底部的 `ga4_client = GA4Client("", "", False)` 是
        **import 期**执行的；而修复前 `__init__` 的第一行就是 `requests.Session()`
        （连带附带的 HTTPAdapter 与 urllib3 连接池）。但 `send_event()` 第一行就 return，
        那个会话**永远不会被用到**——纯属每次 import 都白建一个连接池。

        修法是把 `self.__disabled = True` 提到最前、会话改成惰性创建（只有真要发事件时才建），
        所以这条用例同时钉住两件事：禁用态下不建会话，且 `send_event` 仍是安全的 no-op。
        """
        # import 期就已构造的模块级实例：此刻不该有任何会话
        self.assertIsNone(ga4_client.http_client)

        # send_event 必须是 no-op，且不得顺手把会话来出来
        ga4_client.send_event("interfacetester_disabled_event", {"a": 1})
        self.assertIsNone(ga4_client.http_client)

    def test_ga4_new_instances_do_not_build_sessions_either(self):
        """新构造的实例（含 debug 分支）同样不得在构造期建会话。"""
        for debug in (False, True):
            with self.subTest(debug=debug):
                client = utils.GA4Client("measurement-id", "api-secret", debug)
                self.assertIsNone(client.http_client)


class TestSensitiveKeyPredicate(unittest.TestCase):
    """0918-2（M4/M5）：`is_sensitive_key` 成了全框架**唯一**的"什么是凭据"判定。

    它同时服务两条链路：
      - 运行期脱敏（`client.get_req_resp_record` / `step_request` 的请求日志）；
      - 导入器占位化（`converters/ir.py` 的 `sanitize_*`）。

    所以它的**漏判** = 明文外泄，**误判** = 只是显示上糊掉一点。方向必须偏保守。
    """

    def test_matches_common_credential_names(self):
        for name in (
            "password",
            "Password",
            "passwd",
            "pwd",
            "secret",
            "token",
            "access_token",
            "api_key",
            "apikey",
            "secret_key",
            "private_key",
            "authorization",
            "credential",
            # 0918-2 新增的几类（修复前全是漏判）
            "Cookie",
            "session",
            "sessionId",
            "PHPSESSID",
            "jwt",
            "sid",
            "X-Signature",
            "sign",
            "csrf",
            "xsrf",
        ):
            with self.subTest(key=name):
                self.assertTrue(utils.is_sensitive_key(name))

    def test_separator_normalization(self):
        """分隔符归一：`X-Api-Key` / `x.api.key` 都要命中 `api_key`。

        NOTICE: 修复前是直接对原始小写名做子串匹配，`X-Api-Key` 因为短横线而**不命中**——
        而它恰恰是最常见的 API Key 头名之一。
        """
        for name in ("X-Api-Key", "x-api-key", "X_API_KEY", "x.api.key"):
            with self.subTest(key=name):
                self.assertTrue(utils.is_sensitive_key(name))

        # `X-Amz-Security-Token` 命中 `token`
        self.assertTrue(utils.is_sensitive_key("X-Amz-Security-Token"))

    def test_does_not_match_ordinary_names(self):
        for name in (
            "User-Agent",
            "Content-Type",
            "X-Trace-Id",
            "page",
            "username",
            "amount",
            "note",
            None,
            123,
        ):
            with self.subTest(key=name):
                self.assertFalse(utils.is_sensitive_key(name))

    def test_mask_sensitive_text(self):
        """表单串按参数名脱敏，非敏感参数与结构保持不变。"""
        self.assertEqual(
            utils.mask_sensitive_text("user=bob&password=p1&page=2"),
            f"user=bob&password={utils.MASKED_VALUE}&page=2",
        )
        # 没有 `=` 的字符串原样返回（不能瞎动）
        self.assertEqual(utils.mask_sensitive_text("plain text"), "plain text")
        self.assertEqual(utils.mask_sensitive_text(None), None)

    def test_mask_sensitive_url(self):
        """URL 只脱敏查询串里敏感的取值，scheme/host/path 逐字保留。"""
        self.assertEqual(
            utils.mask_sensitive_url("https://h/a?access_token=abc&page=1"),
            f"https://h/a?access_token={utils.MASKED_VALUE}&page=1",
        )
        # 无查询串 → 原样
        self.assertEqual(
            utils.mask_sensitive_url("https://h/a"), "https://h/a"
        )
        # NOTICE: 必须先切掉 base 再解析键值对，否则 `https://h/a?access_token`
        # 会被当成参数名（这条断言就是防这个回归）
        self.assertIn(
            "https://h/a?", utils.mask_sensitive_url("https://h/a?access_token=abc")
        )

    def test_mask_sensitive_url_masks_userinfo(self):
        """0920 批次 2 / **N4**：`scheme://user:pass@host` 的凭据必须糊掉。

        修复前 `mask_sensitive_url` **只**处理查询串，userinfo 原样保留 ——
        而 `.run.log` 是无条件 DEBUG 落盘、summary 还会进 HTML/Allure，
        凭据因此直接外泄（同一份日志里 `Authorization` 早已是 `******`）。
        """
        secret = "CANARY_USERINFO_9f3ab21c"

        masked = utils.mask_sensitive_url(f"http://alice:{secret}@host/ok")
        self.assertNotIn(secret, masked, "userinfo 里的口令明文仍在")
        self.assertNotIn("alice", masked, "userinfo 整段必须一起糊（用户名也可能是凭据）")
        self.assertIn("@host", masked, "要能看出'这个 URL 带凭据'，不能把 host 也吃掉")

        # 同时带 userinfo 与敏感查询串：两边都要糊
        both = utils.mask_sensitive_url(f"http://alice:{secret}@host/ok?token={secret}")
        self.assertNotIn(secret, both)
        self.assertIn(f"token={utils.MASKED_VALUE}", both)

        # 带端口同样成立
        port_masked = utils.mask_sensitive_url(f"https://u:p@{secret}.test:8443/x")
        self.assertNotIn("u:p@", port_masked)
        self.assertIn(".test:8443", port_masked)

    def test_mask_sensitive_url_does_not_mistake_path_at_for_userinfo(self):
        """反向护栏：路径里合法的 `@` 不能被当成 userinfo（否则会改坏展示）。

        `/users/@me`、`/a@b/c` 这类 URL 很常见（Mastodon 风格的 API 尤其多），
        它们**没有** authority 段里的 `@`，必须逐字保留。
        """
        for url in (
            "https://api.test/users/@me",
            "https://api.test/a@b/c",
            "https://api.test/x?q=a@b",
            "https://api.test/plain/path",
        ):
            with self.subTest(url=url):
                self.assertEqual(utils.mask_sensitive_url(url), url)

    def test_mask_sensitive_request_data(self):
        """统一入口：dict 按键名递归，字符串按表单串。"""
        self.assertEqual(
            utils.mask_sensitive_request_data(
                {"Authorization": "Bearer x", "X-Trace-Id": "t1"}
            ),
            {"Authorization": utils.MASKED_VALUE, "X-Trace-Id": "t1"},
        )
        self.assertEqual(
            utils.mask_sensitive_request_data("a=1&token=t"),
            f"a=1&token={utils.MASKED_VALUE}",
        )
        # 不修改入参
        payload = {"Cookie": "sid=x"}
        utils.mask_sensitive_request_data(payload)
        self.assertEqual(payload, {"Cookie": "sid=x"})


class TestMaskingHandlesNonDictMappings(unittest.TestCase):
    """0919-3 / L35：脱敏判据必须是 `Mapping`，不能是 `dict`。

    修复前用的是 `isinstance(value, dict)`，于是**任何非 dict 的映射类型都被静默放过**。
    最典型的就是 `requests` 自己那两个：

      - `RequestsCookieJar`：响应 `resp.cookies` 的真实类型（继承 `MutableMapping`，
        **不是** dict 子类）；
      - `CaseInsensitiveDict`：`resp.headers` 的真实类型。

    实测（同一份数据、两种容器）::

        mask_sensitive_request_data({"sessionid": "SECRET"})  -> {"sessionid": "******"}   ✅
        mask_sensitive_request_data(RequestsCookieJar(...))   -> 原样返回，明文仍在        ❌

    这是最坏的一类缺陷：**「看起来脱敏了，其实没有」**——调用方以为已经收口了，
    比完全不做脱敏更难发现。所以本类把每种容器形态都钉一条。
    """

    def test_requests_cookie_jar_is_masked(self):
        jar = RequestsCookieJar()
        jar.set("sessionid", "COOKIE-SECRET")
        jar.set("lang", "zh")

        masked = utils.mask_sensitive_request_data(jar)

        self.assertEqual(masked["sessionid"], utils.MASKED_VALUE)
        self.assertEqual(masked["lang"], "zh")
        self.assertNotIn("COOKIE-SECRET", str(masked))

    def test_case_insensitive_dict_is_masked(self):
        """`resp.headers` 的真实类型；顺带验证 `X-Api-Key` 这类带短横线的头名能命中。"""
        headers = CaseInsensitiveDict(
            {"Set-Cookie": "sid=COOKIE-SECRET", "X-Api-Key": "KEY-SECRET", "Accept": "json"}
        )

        masked = utils.mask_sensitive_request_data(headers)

        self.assertEqual(masked["Set-Cookie"], utils.MASKED_VALUE)
        self.assertEqual(masked["X-Api-Key"], utils.MASKED_VALUE)
        self.assertEqual(masked["Accept"], "json")

    def test_nested_mapping_is_masked(self):
        """嵌套位置放一个非 dict 映射时，**不能整块漏过**。

        修复前 `_mask_sensitive_value` 也用 `dict` 判断，于是外层脱敏了、里层明文还在。
        """
        headers = CaseInsensitiveDict({"X-Auth-Token": "NESTED-SECRET"})

        masked = utils.mask_sensitive_variables({"outer": headers})

        self.assertNotIn("NESTED-SECRET", str(masked))
        self.assertEqual(masked["outer"]["X-Auth-Token"], utils.MASKED_VALUE)

    def test_mapping_inside_list_is_masked(self):
        """列表里的非 dict 映射也要被脱敏。

        NOTICE: 外层键名刻意用**不敏感**的 `items`。若写成 `cookies`，
        `is_sensitive_key("cookies")` 会命中（`cookies` 含关键词 `cookie`），
        于是整块值被直接换成 `******`——那样即使内层判断是坏的，用例也会绿，
        起不到判别作用。这个坑在写这条用例时实测踩过一次。
        """
        jar = RequestsCookieJar()
        jar.set("token", "IN-LIST-SECRET")

        masked = utils.mask_sensitive_variables({"items": [jar]})

        self.assertNotIn(
            "IN-LIST-SECRET",
            str(masked),
            "列表里的非 dict 映射整块漏过了脱敏",
        )
        self.assertEqual(masked["items"][0]["token"], utils.MASKED_VALUE)

    def test_masked_container_is_a_plain_dict(self):
        """返回值统一成普通 dict：方便下游序列化与展示（CookieJar 不能直接被 json.dumps）。"""
        jar = RequestsCookieJar()
        jar.set("sessionid", "x")

        masked = utils.mask_sensitive_request_data(jar)

        self.assertIs(type(masked), dict)

    def test_non_mapping_inputs_are_unchanged(self):
        """反向护栏：改成 `Mapping` 判据不能影响非映射输入。"""
        # 字符串仍走「表单串」脱敏（这是既有语义，不是"原样返回"）
        self.assertEqual(
            utils.mask_sensitive_request_data("a=1&token=t"),
            f"a=1&token={utils.MASKED_VALUE}",
        )
        self.assertEqual(utils.mask_sensitive_request_data(42), 42)
        self.assertEqual(utils.mask_sensitive_request_data(None), None)
        # NOTICE（批次 4 / M3）：这条断言**被有意改掉了**。
        # 它原先写的是 `["token=x"] -> ["token=x"]`（原样返回），当时是在记录"非映射输入
        # 不受影响"，顺带把「顶层 list 之内的内容完全不脱敏」这个**缺陷**当成了正常行为。
        # M3 修好后，list 里的字符串同样按表单串脱敏：
        self.assertEqual(
            utils.mask_sensitive_request_data(["token=x"]),
            [f"token={utils.MASKED_VALUE}"],
            "顶层 list 里的字符串值漏过脱敏了（M3 回归）",
        )


class TestBatch0919_22JsonStringIsNotSlicedByEquals(unittest.TestCase):
    r"""批次 4 / N2（+ M3）：JSON 形态的字符串**不能**按 `k=v` 切分。

    ## 现场（修复前实测）

    ```text
    mask_sensitive_request_data('{"password": "CANARY="}')
      -> '{"password": "CANARY=******'
    ```

    两个后果同时发生：**输出成了非法 JSON**（值被切成两半），
    而值的**前半段仍然明文可见**（`CANARY` 还在）—— 复核者看到 `******` 却以为已经收口。

    根因：`_mask_query_pairs` 对任何含 `=` 的字符串都按 `partition("=")` 切，
    于是 JSON 的**值内部**那个 `=`（base64 结尾 `==`、带签名的串）被当成了分隔符，
    `name` 变成 `{"password": "CANARY` 这种碎片。

    修法：先 `parse_json_container` 判一次，是 `dict`/`list` 就走**结构化**脱敏
    （按键名递归），再按紧凑写法写回字符串。本类逐条钉住判定器、脱敏结果与两条反向护栏。
    """

    CANARY = "CANARY-9f3a"

    # ---------------------------------------------------------------- 判定器
    def test_parse_json_container_accepts_objects_and_arrays(self):
        self.assertEqual(utils.parse_json_container('{"a": 1}'), {"a": 1})
        self.assertEqual(utils.parse_json_container("  [1, 2]  "), [1, 2])

    def test_parse_json_container_rejects_non_containers(self):
        """标量 JSON / 非法 JSON / 表单串 / 非字符串 一律返回 `None`（走原路径）。"""
        for value in (
            "123",            # 标量 JSON：没有字段名可查
            "true",
            '"abc"',
            "null",
            "a=1&b=2",        # 表单串
            "{a=1&b=2}",      # 像 JSON 但不是
            '{"a": 1',        # 截断的 JSON
            "just text",
            "",
            "   ",
            42,
            None,
            {"a": 1},         # 已经是容器，不是"字符串形态"
        ):
            with self.subTest(value=value):
                self.assertIsNone(utils.parse_json_container(value))

    # ------------------------------------------------------------ N2 的修复
    def test_json_string_body_is_masked_and_stays_valid_json(self):
        masked = utils.mask_sensitive_request_data(
            '{"password": "%s="}' % self.CANARY
        )

        self.assertEqual(masked, '{"password":"******"}')
        self.assertNotIn(self.CANARY, masked, "值的前半段仍然明文可见")
        json.loads(masked)  # 非法 JSON 会在这里抛

    def test_base64_style_double_equals_value_is_fully_masked(self):
        """base64 结尾的 `==` 是最常见的触发形态，值要**整体**变成 `******`。"""
        masked = utils.mask_sensitive_request_data('{"token": "YWJj=="}')

        self.assertEqual(masked, '{"token":"******"}')
        self.assertNotIn("YWJj", masked)

    def test_json_array_string_body_is_masked(self):
        masked = utils.mask_sensitive_request_data(
            '[{"user": "bob", "password": "%s="}]' % self.CANARY
        )

        self.assertEqual(masked, '[{"user":"bob","password":"******"}]')
        self.assertNotIn(self.CANARY, masked)

    def test_nested_json_string_body_is_masked(self):
        """嵌在 dict / list 里的 JSON 字符串体同样要脱敏（M3 的收口递归）。"""
        for payload, expected in (
            ({"body": '{"password": "%s="}' % self.CANARY},
             {"body": '{"password":"******"}'}),
            (['{"password": "%s="}' % self.CANARY],
             ['{"password":"******"}']),
        ):
            with self.subTest(payload=payload):
                masked = utils.mask_sensitive_request_data(payload)
                self.assertEqual(masked, expected)
                self.assertNotIn(self.CANARY, str(masked))

    # ------------------------------------------------------------ 反向护栏
    def test_json_body_without_secrets_is_not_reformatted(self):
        """**没命中任何凭据时一个字节都不能动**（转换器/展示层都不该无谓重排）。

        `{"a": 1}` 与 `{"a":1}` 语义相同，但若每次都重建，就会把用户的原始报文
        悄悄换个写法 —— 检查生成物的复核者会以为请求也被改了。
        """
        for text in ('{"a": 1}', '{"a":1}', '{"中文": "值"}', '[1, 2, 3]'):
            with self.subTest(text=text):
                self.assertEqual(utils.mask_sensitive_request_data(text), text)

    def test_form_string_masking_is_unchanged(self):
        """反向护栏：真表单串的既有口径不变（含 `&` 分段、非敏感段原样保留）。"""
        self.assertEqual(
            utils.mask_sensitive_request_data(f"user=bob&password={self.CANARY}&page=2"),
            "user=bob&password=******&page=2",
        )

    def test_plain_text_without_equals_is_untouched(self):
        """反向护栏：无 `=`、非 JSON 的普通文本原样返回。"""
        for text in ("just plain text", "<xml>a</xml>", ""):
            with self.subTest(text=text):
                self.assertEqual(utils.mask_sensitive_request_data(text), text)


class TestSensitiveKeyMatching(unittest.TestCase):
    """0918-8 / L22：凭据判定（`is_sensitive_key`）的关键词表与匹配方式。

    NOTICE（修复前实测）：
    - **漏判**：`sig`（Azure SAS 的签名，等价于凭据）、`otp`、`pin` 都不在关键词表里 →
      查询串 / 表单字段原样入库（`?sv=…&sig=AvC123…`）；
    - **误判**：表里的 `sign` 走**子串**匹配，于是 `design` / `assignee` / `signal_id`
      都被当成凭据。误判的代价不只是"显示上糊掉"——**导入器用同一份判定做占位化**，
      这些字段的值会被换成 `${...}`，生成的用例运行期直接 `EnvNotFound`。

    修法：短关键词（`sig`/`otp`/`pin`/`sign`）改为**整段相等**匹配（按 `_` 与驼峰边界切段），
    并把 `sign` 从子串表挪过去；同时补 `presign`（预签名 URL 是凭据）。
    **刻意不收录 `code`**：`errorCode`/`status_code`/`body.code` 都是正常字段。
    """

    MUST_MATCH = (
        "password",
        "passwd",
        "pwd",
        "client_secret",
        "token",
        "accessToken",
        "X-Api-Key",
        "Authorization",
        "Cookie",
        "sessionId",
        "PHPSESSID",
        "jwt",
        "csrf_token",
        # 0918-8 / L22 新增与迁移
        "sig",
        "X-Sig",
        "x_sig",
        "otp",
        "smsOtp",
        "sms_otp",
        "otpCode",
        "pin",
        "userPin",
        "payment_pin",
        "sign",
        "app_sign",
        "X-Sign",
        "signature",
        "presigned_url",
        "presign_expiry",
    )

    MUST_NOT_MATCH = (
        "code",
        "errorCode",
        "status_code",
        "body.code",
        "username",
        "orderId",
        "lang",
        "created_at",
        # `pin` 的子串误报面
        "shipping_address",
        "pinterest_url",
        "spinning_top",
        "ping",
        # `sign` 的子串误报面（0918-2 §六.3 登记的假报，本次修掉）
        "design",
        "designer",
        "design_id",
        "assignee",
        "assignment_id",
        "signal_id",
        "signalR",
    )

    def test_credentials_are_detected(self):
        for key in self.MUST_MATCH:
            with self.subTest(key=key):
                self.assertTrue(utils.is_sensitive_key(key), f"{key} 应当被判为凭据")

    def test_normal_fields_are_not_flagged(self):
        """误判的代价是"生成的用例跑不起来"（占位化 → EnvNotFound），所以这条同样重要。"""
        for key in self.MUST_NOT_MATCH:
            with self.subTest(key=key):
                self.assertFalse(
                    utils.is_sensitive_key(key), f"{key} 不该被判为凭据（假报）"
                )

    def test_short_keywords_match_whole_segments_only(self):
        """短关键词必须整段相等：`pin`/`sig` 分别不能命中 `shipping`/`design`。"""
        self.assertTrue(utils.is_sensitive_key("user_pin"))
        self.assertFalse(utils.is_sensitive_key("shipping_address"))
        self.assertTrue(utils.is_sensitive_key("X-Sig"))
        self.assertFalse(utils.is_sensitive_key("design"))

    def test_non_string_keys_are_safe(self):
        for key in (None, 123, ["a"], {"k": 1}):
            with self.subTest(key=key):
                self.assertFalse(utils.is_sensitive_key(key))


class TestBatch0919_27EnsureIntValue(unittest.TestCase):
    """批次 8 / **L2**：`ensure_int_value` 是"变量来的数字要转"的一个**共用收口**。

    它给 `gen_random_string` / `get_timestamp` 用（那两个函数以前直接吃 `range("8")`）。
    口径要点逐条钉住：

    - **收**：`int`、数字字符串（含首尾空白，`int()` 本来就容忍）、整数值 float；
    - **拒**：`bool`（`true` 是笔误，不是 1）、**非整数 float / 字符串小数**
      （`int()` 会静默截断 —— 那正是本项目反复登记的静默坏值来源，见 L5 的 `200.7`）；
    - **区间**：越界报错要带**实际值**与**真实边界**；
    - 报错消息里必须带 `where`（调用方给的中文/英文名）——否则用户看不出是哪个字段。
    """

    def test_accepts_int_numeric_string_and_integral_float(self):
        for value, expected in ((8, 8), ("8", 8), (" 8 ", 8), ("+8", 8), (8.0, 8)):
            with self.subTest(value=value):
                self.assertEqual(utils.ensure_int_value(value, "len"), expected)

    def test_rejects_bool_explicitly(self):
        """`True` 在 Python 里 `isinstance(True, int)` 为真 —— 必须先拦掉。"""
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    utils.ensure_int_value(value, "len")

                self.assertIn("bool", str(ctx.exception))

    def test_rejects_values_that_would_be_silently_truncated(self):
        for value in (8.5, "8.5", "8.0"):
            with self.subTest(value=value):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    utils.ensure_int_value(value, "len")

                self.assertIn("len", str(ctx.exception))

    def test_rejects_other_shapes_with_the_type_in_the_message(self):
        for value in (None, [8], {"k": 8}):
            with self.subTest(value=value):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    utils.ensure_int_value(value, "len")

                message = str(ctx.exception)
                self.assertIn("len", message)
                self.assertIn(type(value).__name__, message)

    def test_bounds_report_the_actual_value_and_the_real_limits(self):
        with self.assertRaises(exceptions.ParamsError) as ctx:
            utils.ensure_int_value(-3, "random string length", minimum=0)
        self.assertIn("-3", str(ctx.exception))
        self.assertIn("不小于 0", str(ctx.exception))

        with self.assertRaises(exceptions.ParamsError) as ctx:
            utils.ensure_int_value(20, "timestamp length", minimum=1, maximum=16)
        self.assertIn("20", str(ctx.exception))
        self.assertIn("不大于 16", str(ctx.exception))

        self.assertEqual(
            utils.ensure_int_value("16", "timestamp length", minimum=1, maximum=16), 16
        )

    def test_passes_through_when_no_bounds_are_given(self):
        """反向护栏：不传区间时不得擅自加限制（负数也照原样返回）。"""
        self.assertEqual(utils.ensure_int_value(-5, "n"), -5)


class TestBatch0921EnsureBoolValue(unittest.TestCase):
    """0921：`verify` 的链式 setter 绕过 pydantic 校验，必须自己转。

    ## 现场（实测，修复前，`.tmp_audit/verify0921/`）

    `TRequest` / `TConfig` **没有** `validate_assignment=True`（只有 `TStep` 有），
    于是：

    ```text
    step.set_verify("false")     -> 存成字符串 'false'（不是 False）
    Config("x").verify("false")  -> 存成字符串 'false'
    ```

    `.env` / CSV / `${ENV(...)}` 出来的值**永远是字符串**，所以手写 Python API 时
    "把环境变量直接喂给 verify"是很自然的写法。而字符串 `"false"` 在 Python 里是
    **真值**，后果有两层：

    1. `requests` 把它当成 **CA 证书路径** →
       `OSError: Could not find a suitable TLS CA certificate bundle, invalid path: false`
       （报错里完全看不出"这个值本该是布尔"）；
    2. `client._warn_if_tls_verification_disabled` 判 `if verify is None or verify:`
       → 非空字符串被判成"已开启校验"→ **TLS 关闭告警被吞掉**。

    ## 口径

    刻意**抄 pydantic 的取值集合**（`true/false/yes/no/on/off/1/0` + 0/1 + bool），
    否则会出现"YAML 能写、Python API 不能写"的两套口径。
    `None` **原样返回 None**：`TRequest.verify=None` 的语义是"未显式设置"
    （运行时回落到 `config.verify`），压成 `False` 会把"没设置"变成"显式关闭"。
    """

    def test_pydantic_compatible_strings_are_coerced(self):
        """**核心**：pydantic 认的字符串，这里必须得到**同样**的结果。"""
        from interfacetester.models import TRequest

        cases = ["true", "false", "yes", "no", "on", "off", "1", "0",
                 "TRUE", "False", 0, 1, 0.0, 1.0]
        for value in cases:
            with self.subTest(value=value):
                expected = TRequest(method="GET", url="/x", verify=value).verify
                self.assertEqual(
                    utils.ensure_bool_value(value, "verify"),
                    expected,
                    f"{value!r} 的转换结果与 YAML 路径（pydantic）不一致",
                )

    def test_whitespace_is_rejected_exactly_like_pydantic(self):
        """口径一致性：pydantic 对 bool **不去空白**，这里也必须拒绝。

        NOTICE: 这条是写用例时**实测发现的**：`ensure_bool_value` 第一版写了
        `.strip()`，于是 `" true "` 能过，而 `TRequest(verify=" true ")` 是
        `ValidationError` —— 正是"YAML 报错、Python API 放行"的两套口径。
        不 strip 才是对的（宽容一点就多一个不一致）。
        """
        from interfacetester.models import TRequest

        for value in (" true ", "  false  "):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    TRequest(method="GET", url="/x", verify=value)

                with self.assertRaises(exceptions.ParamsError):
                    utils.ensure_bool_value(value, "verify")

    def test_real_bools_pass_through(self):
        self.assertIs(utils.ensure_bool_value(True, "verify"), True)
        self.assertIs(utils.ensure_bool_value(False, "verify"), False)

    def test_none_keeps_its_not_set_semantics(self):
        """**关键**：`None` 不能被压成 `False`（那会把"没设置"变成"显式关闭"）。"""
        self.assertIsNone(utils.ensure_bool_value(None, "verify"))

    def test_the_dangerous_truthy_string_is_now_false(self):
        """本缺陷的要害：字符串 `'false'` 是真值，必须转成 `False`。"""
        self.assertTrue(bool("false"), "前提：字符串 'false' 在 Python 里是真值")
        self.assertIs(utils.ensure_bool_value("false", "verify"), False)

    def test_illegal_values_raise_with_the_actual_value(self):
        """失败面：非法值必须报 `ParamsError`（而不是静默存进模型）。"""
        for value in ("maybe", 2, "2", [], {}, 3.5, object()):
            with self.subTest(value=value):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    utils.ensure_bool_value(value, "request.verify")

                message = str(ctx.exception)
                self.assertIn("request.verify", message)

    def test_setters_actually_use_the_coercion(self):
        """端到端：两个链式 setter 都必须真的转（钉住"只改了 utils 没接上"）。"""
        from interfacetester.config import Config
        from interfacetester.models import TRequest, TStep
        from interfacetester.step_request import RequestWithOptionalArgs

        step = RequestWithOptionalArgs(
            TStep(name="t", request=TRequest(method="GET", url="https://x"))
        )
        step.set_verify("false")
        self.assertIs(step.struct().request.verify, False)

        step.set_verify("true")
        self.assertIs(step.struct().request.verify, True)

        config = Config("c")
        config.verify("false")
        self.assertIs(config.struct().verify, False)


class TestBatch0921OmitLongDataSpelling(unittest.TestCase):
    """0921：`omit_long_data` 的省略提示拼写错误（`CHARACTORS` → `CHARACTERS`）。

    会原样出现在日志/报告里（`utils.py`）。这里同时钉住 suffix 的**结构**
    （省略字数算得对、bytes 走 bytes），避免只改拼写却把行为改坏。
    """

    def test_spelling_is_fixed(self):
        result = utils.omit_long_data("x" * 600, omit_len=512)
        self.assertIn("CHARACTERS", result)
        self.assertNotIn("CHARACTORS", result)

    def test_omitted_count_is_still_correct(self):
        """省略掉的字数必须还是 `总长 - omit_len`。"""
        result = utils.omit_long_data("x" * 600, omit_len=512)
        self.assertIn("OMITTED 88 CHARACTERS", result)
        self.assertEqual(result[:512], "x" * 512)

    def test_bytes_still_yields_bytes(self):
        """bytes 输入的 suffix 必须是 bytes（否则拼接会 `TypeError`）。"""
        result = utils.omit_long_data(b"y" * 600, omit_len=512)
        self.assertIsInstance(result, bytes)
        self.assertIn(b"OMITTED 88 CHARACTERS", result)

    def test_short_and_non_text_pass_through(self):
        """反向护栏：不超长、以及非 str/bytes 的输入原样返回。"""
        self.assertEqual(utils.omit_long_data("abc"), "abc")
        self.assertEqual(utils.omit_long_data({"a": 1}), {"a": 1})

    def test_no_misspelling_anywhere_in_the_package(self):
        r"""**全包**扫描：`charactors` 这个拼写不许再出现在 `interfacetester/` 里。

        NOTICE（0921-2 / 发现 1）：上一轮只改了 `utils.py` 的输出串并加了护栏，
        但**同一处拼写**还留在 `client.py:152` 的注释里 —— 当时那条护栏只断言
        `omit_long_data()` 的**返回值**，扫不到注释，于是"修了一半"没人发现。
        这里改成扫**源码文本**，把"全仓无该拼写"真正变成可强制的判据。

        刻意扫**整个包**而不是只扫 `utils.py`：这个错拼是"手滑型"的，
        下次完全可能出现在别的文件里。
        """
        import interfacetester

        package_dir = os.path.dirname(os.path.abspath(interfacetester.__file__))
        offenders = []
        for dirpath, _dirnames, filenames in os.walk(package_dir):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line_no, line in enumerate(f, 1):
                        if "charactor" in line.lower():
                            rel = os.path.relpath(path, package_dir)
                            offenders.append(f"{rel}:{line_no}: {line.strip()}")

        self.assertEqual(
            offenders,
            [],
            "`charactors` 拼写又出现了（应为 `characters`）：\n  " + "\n  ".join(offenders),
        )


class TestBatch0921_2ConfigCallerPathFallback(unittest.TestCase):
    """0921-2 / **发现 2**：`Config.__init__` 的 `inspect.stack()` 必须有兜底。

    ## 修复前

    `config.py` 是**裸调用** `inspect.stack()[1]`，而 `parser.py:783` 对同一件事
    （定位 caller path）写了 `try/except (IndexError, ValueError)` + 回退 cwd。
    值得记一笔的是 `parser.py` 那句注释自称"与 Config 的实现保持一致"——
    但 Config 从来没有那层兜底，"一致"只存在于注释里。

    ## 为什么要兜底

    `inspect.stack()` 在某些执行环境下拿不到调用帧（解释器内嵌、`exec` 编译出的
    伪文件名、部分 C 扩展回调），`stack()[1]` 会抛 `IndexError`。修复前会**直接崩**
    在 `Config(...)` 上，报错是 `IndexError: list index out of range` ——
    完全看不出"这是项目根定位失败"，用户会去查自己的用例。

    回退 cwd 与 `loader.locate_project_root_directory` 找不到 `debugtalk.py` 时的
    既有兜底口径一致（那里也是 `os.getcwd()`）。
    """

    def test_normal_path_still_resolves_to_the_caller_file(self):
        """反向护栏：正常情况下必须仍取**调用方文件**，不能被兜底"吃掉"。"""
        from interfacetester.config import Config

        config = Config("normal")
        self.assertEqual(
            os.path.normcase(os.path.abspath(config.path)),
            os.path.normcase(os.path.abspath(__file__)),
        )

    def test_index_error_falls_back_to_cwd_instead_of_crashing(self):
        """**核心**：拿不到调用帧时回退 cwd，而不是抛 `IndexError`。"""
        from interfacetester.config import Config

        with mock.patch("inspect.stack", side_effect=IndexError("list index out of range")):
            config = Config("fallback")

        self.assertEqual(
            os.path.normcase(os.path.abspath(config.path)),
            os.path.normcase(os.path.abspath(os.getcwd())),
        )

    def test_value_error_also_falls_back(self):
        """`parser.py` 捕获的是 `(IndexError, ValueError)`，这里必须同口径。"""
        from interfacetester.config import Config

        with mock.patch("inspect.stack", side_effect=ValueError("boom")):
            config = Config("fallback")

        self.assertEqual(
            os.path.normcase(os.path.abspath(config.path)),
            os.path.normcase(os.path.abspath(os.getcwd())),
        )

    def test_chain_still_usable_after_a_fallback(self):
        """兜底之后链式调用仍要可用（`SessionRunner` 依赖 `.path` / `.verify`）。"""
        from interfacetester.config import Config

        with mock.patch("inspect.stack", side_effect=IndexError):
            config = Config("chain").base_url("http://example.com").verify(False)

        self.assertTrue(config.path)
        self.assertIs(config.struct().verify, False)


class TestBatch0921_3ComparatorConsistency(unittest.TestCase):
    """0921-3：断言算子的三处「同族遗漏 / 口径不齐」。

    ## 现场（实测，修复前，`.tmp_audit/verify0921_3/`）

    **轻微项 1**：`length_*` 用裸 `assert isinstance(expect_value, int)`，
    `.env` / `${ENV(...)}` 的值永远是字符串 →

        length_equal: [body, ${ENV(LEN)}]   # LEN="4"
            -> AssertionError: expect_value should be int type

    英文硬 assert，既不说是 length 的期望值、也不点出"值是字符串"这个根因。
    这是 `sleep` / `gen_random_string` / `get_timestamp` 那一族（L2）的**漏网之鱼**。

    **轻微项 2**：`type_match` 对写错的类型名抛 `ValueError`，而
    `response.validate` 只捕 `AssertionError` / `TypeError` →

        type_match: [body, "eval"]  ->  pytest **ERROR**（而不是 FAILED）

    "用户写错用法"按本仓既有划分应记 failed，这里分类错位。

    **轻微项 3**：`regex_match` 用裸 `assert isinstance(check_value, str)`，
    而 `string_equals` / `startswith` / `endswith` 走 `_ensure_string_shaped()`
    （中文提示 + 「断在 text 上」的可操作出路）→ 同族算子两套报错质量。
    """

    def test_length_operators_accept_numeric_strings(self):
        """**核心**（轻微项 1）：变量来的数字字符串必须被接受。"""
        from interfacetester.builtin import comparators as comp

        cases = [
            ("length_equal", 4),
            ("length_greater_or_equals", 4),
            ("length_less_or_equals", 4),
        ]
        for name, expected in cases:
            with self.subTest(operator=name):
                # 不抛异常即为通过（"4" == len("abcd")）
                self.assertIsNone(getattr(comp, name)("abcd", "4"))
                self.assertIsNone(getattr(comp, name)("abcd", 4))

        # 严格大小关系的两条：字符串形态也要能参与比较
        self.assertIsNone(comp.length_greater_than("abcde", "4"))
        self.assertIsNone(comp.length_less_than("abc", "4"))

    def test_length_equal_keeps_its_int_only_contract(self):
        """反向护栏：`length_equal` 仍然**只收整数**（长度相等是精确判断）。

        这是它与其他四个 `length_*` 的既有差异（后者的 expect_value 本就接受 float），
        修 轻微项 1 时**不能**顺手把它放宽成 float。
        """
        from interfacetester.builtin import comparators as comp

        with self.assertRaises(exceptions.ParamsError):
            comp.length_equal("abcd", 4.5)
        with self.assertRaises(exceptions.ParamsError):
            comp.length_equal("abcd", "4.5")

        # 另外四个本就接受 float，不能被误伤
        self.assertIsNone(comp.length_greater_than("abcde", 4.5))
        self.assertIsNone(comp.length_greater_than("abcde", "4.5"))

    def test_length_operators_reject_bool_and_junk_readably(self):
        """失败面：非法值必须是**可读的** `ParamsError`，且带算子名与根因。"""
        from interfacetester.builtin import comparators as comp

        for name in (
            "length_equal",
            "length_greater_than",
            "length_less_than",
            "length_greater_or_equals",
            "length_less_or_equals",
        ):
            for bad in ("abc", True, None, [1]):
                with self.subTest(operator=name, value=bad):
                    with self.assertRaises(exceptions.ParamsError) as ctx:
                        getattr(comp, name)("abcd", bad)
                    self.assertIn(name, str(ctx.exception))

    def test_regex_match_shares_the_string_shape_guard(self):
        """**核心**（轻微项 3）：bytes / None 取值要给同族的中文提示，不是英文 assert。"""
        from interfacetester.builtin import comparators as comp

        for bad in (b"<xml/>", bytearray(b"x"), None):
            with self.subTest(value=type(bad).__name__):
                with self.assertRaises(AssertionError) as ctx:
                    comp.regex_match(bad, ".*")

                message = str(ctx.exception)
                self.assertIn("regex_match", message)
                # 与 string_equals / startswith 同一套提示
                self.assertIn("hint", message)
                self.assertNotIn("check_value should be Text type", message)

    def test_regex_match_reports_invalid_pattern_readably(self):
        """无效正则是**用法问题** → 可读报错（原先 `re.error` 冒泡成 pytest error）。"""
        from interfacetester.builtin import comparators as comp

        for bad in ("[", "(unclosed", "*invalid"):
            with self.subTest(pattern=bad):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    comp.regex_match("abc", bad)
                self.assertIn("不是合法正则", str(ctx.exception))

    def test_regex_match_still_works(self):
        """反向护栏：合法用法一字不变。"""
        from interfacetester.builtin import comparators as comp

        self.assertIsNone(comp.regex_match("123abc456", r"^123\w+456$"))
        with self.assertRaises(AssertionError):
            comp.regex_match("123abc456", "^12b.*456$")

    def test_type_match_raises_params_error_not_bare_value_error(self):
        """**核心**（轻微项 2）：必须是 `ParamsError`（`validate` 转成 failed），
        而不是裸 `ValueError`（冒泡成 pytest error）。"""
        from interfacetester.builtin import comparators as comp

        for bad in ("eval", "nonexistent", "timedelta"):
            with self.subTest(name=bad):
                with self.assertRaises(exceptions.ParamsError) as ctx:
                    comp.type_match("anything", bad)
                self.assertIn(bad, str(ctx.exception))

        # 非字符串非类型：也要给出可读说明（原先只有裸 `ValueError(123)`）
        with self.assertRaises(exceptions.ParamsError) as ctx:
            comp.type_match(1, 123)
        self.assertIn("type_match", str(ctx.exception))

    def test_validate_turns_operator_params_error_into_failure(self):
        """端到端：算子抛的 `ParamsError` 必须被 `validate` 记为**可读失败**。"""
        import requests

        from interfacetester.parser import Parser
        from interfacetester.response import ResponseObject

        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"name": "abcd"}'
        resp.headers["Content-Type"] = "application/json"
        resp.encoding = "utf-8"
        resp.url = "http://127.0.0.1/x"

        resp_obj = ResponseObject(resp, Parser({}))
        with self.assertRaises(exceptions.ValidationFailure) as ctx:
            resp_obj.validate([{"type_match": ["body.name", "eval"]}], {})

        message = str(ctx.exception)
        self.assertIn("不是类型名", message)
        self.assertIn("params_error", message)

    def test_validate_still_lets_real_internal_value_errors_escape(self):
        """**关键反向护栏**：算子内部的**真** `ValueError` 不能被吞成断言失败。

        这正是 `validate` 当初只捕获 `AssertionError` / `TypeError` 的原因
        （见 `response.py` 的 P1-a NOTICE）：把算子 bug 伪装成"你的断言写错了"
        比崩溃更糟。现在只转换**主动声明**为用户误用的 `ParamsError`。
        """
        import requests

        from interfacetester.parser import Parser
        from interfacetester.response import ResponseObject

        def buggy(check_value, expect_value, message=""):
            # 模拟算子内部 bug（不是用户误用）：裸 ValueError
            return int("not-a-number")

        resp = requests.Response()
        resp.status_code = 200
        resp._content = b'{"name": "abcd"}'
        resp.headers["Content-Type"] = "application/json"
        resp.encoding = "utf-8"
        resp.url = "http://127.0.0.1/x"

        resp_obj = ResponseObject(resp, Parser({"buggy": buggy}))
        # 必须原样抛出 ValueError（= pytest error），不能被降级成 ValidationFailure
        with self.assertRaises(ValueError) as ctx:
            resp_obj.validate([{"buggy": ["body.name", 1]}], {})
        self.assertNotIsInstance(ctx.exception, exceptions.ValidationFailure)
