# -*- coding: utf-8 -*-
"""CaseDraft 契约的护栏用例 —— §5.1 的 L1（**P0a**）。

## 为什么需要这个文件

`interfacetester_ai/schema.py` 是本项目里**唯一**允许模型输出经过的结构。
它的判据有两个方向，都会出事：

1. **太松**（漏放）：模型多写一个 `confidence`、少写一个 `source_quote`、`steps` 写成对象
   —— 这些会一路流到装配器甚至写盘之后才炸，而**报错点离原因很远**；
2. **太紧**（误伤）：把合法写法判死。最典型的是 `validate: []`——它是文档里
   "**显式声明不做断言**"的合法形态（§3.3 的 S3 三态之一），契约必须放行。

所以本文件的判据是成对的：**拦得住 7 类结构错 + 不误伤若干合法写法**；
`run_selftest()` 的注入式元护栏再保证"这套断言真的在检查东西"。

## 与 golden 的关系（本文件最有价值的一类判据）

`TestContractExpressesRealGoldenShapes` 把 **15 份 golden 覆盖到的形态特征**
逐个写成 CaseDraft——目的是回答一个具体问题：**契约能不能表达我们从真实文档里见到的一切？**
答不了的话，装配器写得再对也没用（模型根本吐不出来）。
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import interfacetester_ai.schema as schema  # noqa: E402
from interfacetester_ai.schema import (  # noqa: E402
    QUOTE_FIELD,
    DraftValidationError,
    parse_draft,
    run_selftest,
)


def _draft(steps, case_name="契约测试"):
    return {"case_name": case_name, "steps": steps}


def _step(**overrides):
    base = {
        "name": "step",
        "method": "get",
        "url": "/a",
        "validate": [
            {
                "comparator": "eq",
                "check": "body.code",
                "expect": 0,
                QUOTE_FIELD: "code：0 表示成功",
            }
        ],
    }
    base.update(overrides)
    return base


class TestContractRejectsStructuralErrors(unittest.TestCase):
    """**拦得住**：七类结构错逐条点名（每类都断言消息里有定位信息）。"""

    def test_missing_top_level_field(self):
        with self.assertRaises(DraftValidationError) as ctx:
            parse_draft({"steps": [_step()]})
        self.assertIn("case_name", str(ctx.exception))

    def test_unknown_top_level_field(self):
        with self.assertRaises(DraftValidationError) as ctx:
            parse_draft({**_draft([_step()]), "notes": "模型自己加的字段"})
        self.assertIn("未定义字段", str(ctx.exception))

    def test_empty_steps_is_accepted_as_no_interface(self):
        """★`steps: []` 是**合法表达**（2026-09-25 判据修正，原来这里断言它被拒）。

        为什么反过来了：它是模型**诚实地说"本节没有完整接口"**的唯一出口（契约末段
        明写"输出空 `steps`，别为「服务地址」「错误码表」硬造接口"）。原先解析层把它
        判成"格式错误"，回喂给模型的就是"你的 JSON 结构不合法" —— 于是它**只能编一个
        step 出来**（实测：`/api/status`、`/api/placeholder`、`/dummy/success`、
        `/api/file_service` 全是编的）。**把诚实判成错误，等于奖励编造。**

        ★职责分层：`steps: []` 的**形状**合法；内核拒的是"零 step 的**用例**"那个
        **运行语义**（`teststeps` 空 → 零步骤但判成功），由**落盘层**管 ——
        `gen.generate_case` 见到空 steps 记 `skip-model`，即便硬送装配也会被内核 L2
        出口校验拒。两道防线都不靠这条解析判据。
        """
        draft = parse_draft(_draft([]))

        self.assertEqual(draft.steps, (), "空 steps 应当被接受（表示「本节没有接口」）")

    def test_step_missing_required_field_names_the_index(self):
        step = _step()
        step.pop("url")
        with self.assertRaises(DraftValidationError) as ctx:
            parse_draft(_draft([step]))
        self.assertIn("steps[0]", str(ctx.exception))

    def test_assertion_without_quote_names_the_position(self):
        step = _step(validate=[{"comparator": "eq", "check": "body.code", "expect": 0}])
        with self.assertRaises(DraftValidationError) as ctx:
            parse_draft(_draft([step]))
        message = str(ctx.exception)
        self.assertIn("steps[0].validate[0]", message)
        self.assertIn(QUOTE_FIELD, message)

    def test_blank_quote_is_rejected(self):
        step = _step(
            validate=[
                {"comparator": "eq", "check": "body.code", "expect": 0, QUOTE_FIELD: "  "}
            ]
        )
        with self.assertRaises(DraftValidationError) as ctx:
            parse_draft(_draft([step]))
        self.assertIn("非空字符串", str(ctx.exception))

    def test_extra_assertion_field_is_rejected(self):
        step = _step(
            validate=[
                {
                    "comparator": "eq",
                    "check": "body.code",
                    "expect": 0,
                    QUOTE_FIELD: "x",
                    "confidence": 0.9,
                }
            ]
        )
        with self.assertRaises(DraftValidationError) as ctx:
            parse_draft(_draft([step]))
        self.assertIn("confidence", str(ctx.exception))

    def test_all_problems_are_reported_at_once(self):
        """**一次报全**：模型靠这串问题去改；只报第一条会让它来回好几轮。"""
        with self.assertRaises(DraftValidationError) as ctx:
            parse_draft({"unknown": 1, "steps": [{"name": "", "method": "get"}]})

        self.assertGreaterEqual(len(ctx.exception.problems), 3, ctx.exception.problems)


class TestContractAcceptsLegalShapes(unittest.TestCase):
    """**不误伤**：这些是合法写法，契约必须放行（成对判据的另一半）。"""

    def test_blank_validate_is_legal(self):
        """`validate: []` = 显式声明不做断言（§3.3 的 S3 三态之一）—— 绝不能被判非法。"""
        case = parse_draft(_draft([_step(validate=[])]))
        self.assertEqual(case.steps[0].validate, ())

    def test_method_is_normalised_to_upper(self):
        self.assertEqual(parse_draft(_draft([_step(method="post")])).steps[0].method, "POST")

    def test_optional_request_fields_may_be_absent(self):
        step = parse_draft(_draft([_step()])).steps[0]
        self.assertIsNone(step.headers)
        self.assertIsNone(step.json)
        self.assertIsNone(step.extract)
        self.assertEqual(step.request_keys(), ["method", "url"])

    def test_multi_step_with_extract_and_downstream_reference(self):
        """多步依赖形态（golden 的 `auth_token_flow` 就是这种）。"""
        case = parse_draft(
            _draft(
                [
                    _step(
                        name="登录",
                        method="post",
                        url="/api/login",
                        extract={"token": "body.data.token"},
                    ),
                    _step(
                        name="用 token",
                        url="/api/user/me",
                        headers={"Authorization": "Bearer $token"},
                    ),
                ]
            )
        )
        self.assertEqual(len(case.steps), 2)
        self.assertEqual(case.steps[1].headers["Authorization"], "Bearer $token")

    def test_expect_may_be_any_json_value(self):
        values = [0, "ok", True, None, 3.14, ["pending", "paid"], {"type": "object"}]
        step = _step(
            validate=[
                {
                    "comparator": "eq",
                    "check": f"body.f{index}",
                    "expect": value,
                    QUOTE_FIELD: "文档原句",
                }
                for index, value in enumerate(values)
            ]
        )
        self.assertEqual(len(parse_draft(_draft([step])).assertions()), len(values))


class TestContractExpressesRealGoldenShapes(unittest.TestCase):
    """**契约表达力**：15 份 golden 覆盖到的形态，逐个写成 CaseDraft 并解析。

    这一类判据回答的是"**模型有没有可能吐出我们见到的东西**"——
    契约表达不了，装配器写得再对也没用。
    """

    def _parse_step(self, step):
        return parse_draft(_draft([step])).steps[0]

    def test_response_header_assertion(self):
        """golden `get_user_profile` 的响应头断言形态。"""
        step = self._parse_step(
            _step(
                validate=[
                    {
                        "comparator": "eq",
                        "check": 'headers."Content-Type"',
                        "expect": "application/json",
                        QUOTE_FIELD: "响应头：Content-Type",
                    }
                ]
            )
        )
        self.assertEqual(step.validate[0].check, 'headers."Content-Type"')

    def test_inline_json_schema_assertion(self):
        """golden `create_user_schema` / `webhook_notify` 的内联 schema（expect 是 dict）。"""
        schema_expect = {
            "type": "object",
            "required": ["userId", "nickname"],
            "properties": {"userId": {"type": "string"}},
        }
        step = self._parse_step(
            _step(
                validate=[
                    {
                        "comparator": "jsonschema_match",
                        "check": "body.data",
                        "expect": schema_expect,
                        QUOTE_FIELD: "data 是对象，必含 userId 与 nickname",
                    }
                ]
            )
        )
        self.assertEqual(step.validate[0].expect, schema_expect)

    def test_xml_payload_with_xpath_assertion(self):
        """golden `soap_user_service` 的 XML 报文 + `xpath_match`（用 `data` 字段）。"""
        xml = (
            '<?xml version="1.0"?>\n'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">\n'
            "  <soap:Body><GetUser><userId>1001</userId></GetUser></soap:Body>\n"
            "</soap:Envelope>"
        )
        step = self._parse_step(
            _step(
                method="post",
                url="/soap/user",
                headers={"Content-Type": "text/xml; charset=utf-8"},
                data=xml,
                validate=[
                    {
                        "comparator": "xpath_match",
                        "check": "body",
                        "expect": "//nickname",
                        QUOTE_FIELD: "//nickname：字符串，昵称",
                    }
                ],
            )
        )
        self.assertIn("soap:Envelope", step.data)
        self.assertIn("data", step.request_keys())

    def test_enum_and_business_error_code_paths(self):
        """golden `biz_error_order` 的枚举与业务码（`contained_by` / `eq`）。"""
        step = self._parse_step(
            _step(
                validate=[
                    {
                        "comparator": "contained_by",
                        "check": "body.data.status",
                        "expect": ["pending", "paid", "cancelled"],
                        QUOTE_FIELD: "枚举：pending / paid / cancelled",
                    },
                    {
                        "comparator": "eq",
                        "check": "body.code",
                        "expect": 1002,
                        QUOTE_FIELD: "数量超出 1 ~ 99：code: 1002",
                    },
                ]
            )
        )
        self.assertEqual(len(step.validate), 2)


class TestRoundTripIsStable(unittest.TestCase):
    """`parse → to_dict → parse` 必须稳定（修正环会反复走这条路，抖动会被放大）。"""

    def test_dump_and_reparse_is_identical(self):
        original = parse_draft(
            _draft(
                [
                    _step(name="一", extract={"token": "body.data.token"}),
                    _step(name="二", headers={"Authorization": "Bearer $token"}, validate=[]),
                ]
            )
        )
        again = parse_draft(original.to_dict())

        self.assertEqual(again.to_dict(), original.to_dict())
        self.assertEqual(len(again.assertions()), len(original.assertions()))


class TestMetaGuardrails(unittest.TestCase):
    """判据自检：这套断言真的在检查东西。"""

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_required_fields_are_dropped(self):
        """注入：把"必需字段"清空 → "缺必需字段"那几条判据必须变红。"""
        with mock.patch.object(schema, "STEP_REQUIRED_FIELDS", ()):
            self.assertNotEqual(run_selftest(), 0, "必需字段判据失效后自检竟然还是绿的")

    def test_selftest_does_not_write_anything(self):
        """纯逻辑：契约自检不该碰文件系统（生产模块只留自己的写盘点，T27 先例）。"""
        before = sorted(os.listdir(BASE))
        run_selftest()
        self.assertEqual(sorted(os.listdir(BASE)), before, "契约自检写盘了（应当纯逻辑）")


class TestOutputContractIsActuallyGiven(unittest.TestCase):
    """★实测驱动的判据：**输出契约必须真的出现在 prompt 里**。

    起因（2026-09-25 真模型实测）：system prompt 一直说"只输出符合**给定的**
    JSON Schema 的一个 JSON 对象"，但**那份 Schema 从来没给过**——`prompts` 里没有
    注入它的代码、`schema` 里没有导出它的函数、`response_format` 也是 `None`
    （manifest 里记着 `"response_format": null`）。三处都没有 → 模型只能猜。

    实测后果（Ollama `gemma4:latest` + `project-three` 的 8KB 接口文档）：
    15 片 **全部**猜出别的形状（`{"case_name":…,"steps":[…]}` / `{"testcase":…}` /
    `{"type":"r…"}`），修正环又只告诉它"字段不认识"、**不告诉它正确形状** ——
    45 次调用、**0/15 产出**。补上契约后同一条链路立刻产出用例。

    这里钉两件事：① 契约的键名集合与字段白名单**完全一致**；
    ② prompt 里**真的**有契约、且不再出现"符合给定 JSON Schema"这种**自相矛盾**的措辞。
    """

    def test_contract_keys_match_the_whitelist(self):
        """★契约里的键名必须与 `schema` 的字段白名单**一一对应**。

        钉的是"以后有人改了白名单却忘了同步契约"——那正是本仓吃过两次的漂移形态
        （改了白名单，prompt 还按老的写，模型产出永远过不了闸门）。
        """
        text = schema.draft_contract_text()

        for name in sorted(
            schema.CASE_FIELDS | schema.STEP_FIELDS | schema.ASSERTION_FIELDS
        ):
            with self.subTest(field=name):
                self.assertIn(f'"{name}"', text, f"契约里没给出字段 {name}")

    def test_prompt_actually_carries_the_contract(self):
        from interfacetester_ai.prompts import render_system_prompt  # noqa: PLC0415

        prompt = render_system_prompt("gen")

        self.assertIn(schema.draft_contract_text().strip(), prompt)
        # ★"允许交白卷"的出口：没有完整接口的片段要输出空数组，而不是硬造接口
        #   （实测里模型曾为「服务地址表」造出 `"url": "placeholder"`）
        self.assertIn("[]", prompt, "要明说「本节没有完整接口时输出 []」")
        self.assertNotIn(
            "符合给定 JSON Schema",
            prompt,
            "不许再声称「符合**给定的** JSON Schema」却不给那份 Schema",
        )


class TestCliExitCode(unittest.TestCase):
    """`python -m interfacetester_ai.schema` 在重定向 + 无 `PYTHONIOENCODING` 下退出码 0。"""

    def test_module_selftest_exits_zero(self):
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)

        proc = subprocess.run(
            [sys.executable, "-m", "interfacetester_ai.schema"],
            cwd=BASE,
            capture_output=True,
            timeout=120,
            env=env,
        )

        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        self.assertIn("CaseDraft 契约自检全部通过".encode("utf-8"), proc.stdout)
