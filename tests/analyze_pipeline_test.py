# -*- coding: utf-8 -*-
"""失败诊断的护栏用例 —— v8 §5.3（**P1a**）。

## §5.3 的验收是**五条**，逐条都有对应用例

| 验收 | 本文件的用例 |
| --- | --- |
| 引用校验**未降级占比 ≥70%** | `TestDiagnosisPipeline::test_kept_ratio_is_computed_and_reported` |
| 降级 **100% 显式标"未知"** | `test_fabricated_quote_is_degraded_to_unknown` |
| 同输入两次运行**归一化投影后逐字节一致** | `TestOfflineAndAtomicity::test_report_is_byte_identical_across_runs` |
| LLM **断网退出码 3 且无部分写** | `test_transport_failure_writes_nothing` + `test_cli_exit_code_three_when_offline` |
| **归一化对抗样本命中率 100%** | `TestAdversarialQuotes::*`（ANSI / CRLF） |

## 本文件里最要紧的两条"口径"判据

1. **模型自报"未知"算未降级**——它**遵守了规则**。混进降级会让"模型越守规矩、
   指标越难看"，信号正好读反。
2. **引用必须对得上"那一条证据包"**——§5.3 写的是"对应切片的逐字子串"；
   拿 A 用例的响应当 B 用例的证据，是"引用存在"但不是"引用有据"。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.analyze import (  # noqa: E402
    DEGRADED_MARK,
    DiagnosisContractError,
    DiagnosisFailed,
    analyze_summary,
    parse_diagnoses,
    render_analysis_report,
    run_selftest,
)
from interfacetester_ai.diagnosis import (  # noqa: E402
    CAUSE_UNKNOWN,
    audit_diagnosis_contract,
    validate_diagnosis,
)
from interfacetester_ai.evidence import (  # noqa: E402
    REDACTED,
    RESPONSE_BODY_LIMIT,
    build_evidence_packs,
)
from interfacetester_ai.llm import FakeTransport, LLMConfig, LLMTransportError  # noqa: E402

DOC = """# 下单

## POST /api/order

- 库存不足：`code: 1001`，`message: 库存不足`
"""

RESPONSE_BODY = '{"code": 1001, "message": "库存不足"}'
CONFIG = LLMConfig(base_url="http://127.0.0.1:11434", model="fake")


def make_summary(*, failures=2, body=RESPONSE_BODY, log=""):
    """造一份 summary（`failures` 个失败用例，各一条失败断言）。

    ★形状照**真实产物**：`data.validators` 是 `{step_type: [断言…]}` 的 dict、
    响应体在 `response.body`（见 `evidence.py` 注释与 `projection.py` 的遍历）。
    """

    def detail(index):
        return {
            "name": f"下单_{index}",
            "success": False,
            "log": log,
            "records": [
                {
                    "name": "下单",
                    "data": {
                        "validators": {
                            "validate": [
                                {
                                    "comparator": "equal",
                                    "check": "status_code",
                                    "expect": 200,
                                    "expect_value": 200,
                                    "check_result": "pass",
                                },
                                {
                                    "comparator": "equal",
                                    "check": "body.code",
                                    "expect": 0,
                                    "expect_value": 1001,
                                    "check_result": "fail",
                                },
                            ]
                        },
                        "req_resps": [
                            {
                                "request": {
                                    "method": "POST",
                                    "url": "/api/order",
                                    "headers": {
                                        "Authorization": "Bearer SECRET",
                                        "Accept": "application/json",
                                    },
                                },
                                "response": {
                                    "status_code": 200,
                                    "headers": {
                                        "Content-Type": "application/json",
                                        "Date": "now",
                                    },
                                    "body": body,
                                },
                            }
                        ],
                    },
                }
            ],
        }

    return {"success": False, "details": [detail(i) for i in range(failures)]}


def diagnosis(case, cause="真实缺陷", quotes=None, reason="因为响应这么写", **kw):
    payload = {
        "case": case,
        "step": "下单",
        "cause_type": cause,
        "confidence": 0.8,
        "reason": reason,
        "action": "人工复核",
        "evidence_quotes": (
            quotes if quotes is not None else [{"source": "response", "text": "库存不足"}]
        ),
    }
    payload.update(kw)
    return payload


def replay(*items):
    """把诊断项打成模型回放的 JSON 文本。"""
    return json.dumps({"diagnoses": list(items)}, ensure_ascii=False)


def parse_one(payload):
    parsed = parse_diagnoses(payload)
    assert parsed, "回放解析失败"
    return parsed[0]


class _TempWorkspace(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_analyze_")
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()


class TestEvidenceAssembly(unittest.TestCase):
    """证据包装配（§5.3 的"输入装配（纯代码）"）。"""

    def test_only_failures_are_packed(self):
        summary = make_summary(failures=3)
        summary["details"].append({"name": "全过", "success": True, "records": []})

        packs = build_evidence_packs(summary)

        self.assertEqual(len(packs), 3)
        self.assertTrue(all(pack.validator["check_result"] == "fail" for pack in packs))

    def test_siblings_are_the_other_assertions(self):
        """`siblings` 给"只有这条没过"这个信号——且**不含自己**。"""
        pack = build_evidence_packs(make_summary(failures=1))[0]

        self.assertEqual([v["check"] for v in pack.siblings], ["status_code"])

    def test_credentials_are_redacted_but_keys_kept(self):
        """★合规：凭据值换占位，**键名保留**（删掉键会让模型读不出"这里有认证"）。"""
        pack = build_evidence_packs(make_summary(failures=1))[0]

        headers = pack.request["headers"]
        self.assertEqual(headers["Authorization"], REDACTED)
        self.assertEqual(headers["Accept"], "application/json")

    def test_response_body_is_clipped_with_a_marker(self):
        """超长响应体截断**并标明**（静默截断会让模型以为原文就这样）。"""
        long_body = "x" * (RESPONSE_BODY_LIMIT + 99)
        pack = build_evidence_packs(make_summary(failures=1, body=long_body))[0]

        self.assertIn("已截断", pack.response["body"])

    def test_missing_log_is_not_an_error(self):
        """日志取不到**不算错**——很多 CI 不落盘；它只意味着证据少一份。"""
        pack = build_evidence_packs(make_summary(failures=1, log="no/such/file.log"))[0]

        self.assertEqual(pack.log_lines, ())


class TestAdversarialQuotes(unittest.TestCase):
    """**归一化对抗样本命中率 100%**（§5.3 的第五条验收）。

    真实噪声清单：ANSI 颜色码 / CRLF / 全角标点 / JSON 转义。
    模型抄引用时很容易把这些改写一半，于是"逐字子串"判定会**误伤**——
    归一化就是为此存在的。
    """

    def test_quote_with_noise_still_matches(self):
        noisy = "  \x1b[31m库存不足\x1b[0m\r\n"  # 模型抄回来时带了 ANSI 与空白
        pack = build_evidence_packs(make_summary(failures=1))[0]
        guess = parse_one(
            replay(diagnosis("下单_0", quotes=[{"source": "response", "text": noisy}]))
        )

        ok, why = validate_diagnosis(guess, DOC, pack.haystack())

        self.assertTrue(ok, f"带 ANSI/CRLF 噪声的引用应当归一化后仍命中（不该误伤）：{why}")

    def test_fabricated_quote_is_still_rejected(self):
        """元护栏：噪声能过，**编的**必须过不了（否则归一化就成了万能钥匙）。"""
        pack = build_evidence_packs(make_summary(failures=1))[0]
        guess = parse_one(
            replay(
                diagnosis(
                    "下单_0", quotes=[{"source": "response", "text": "这段原文根本不存在"}]
                )
            )
        )

        ok, _why = validate_diagnosis(guess, DOC, pack.haystack())

        self.assertFalse(ok)


class TestDiagnosisPipeline(_TempWorkspace):
    """端到端（`FakeTransport` 回放，**不出网**）。"""

    def test_one_call_for_all_packs(self):
        """★§5.3"批量与成本"：**所有**证据包放进同一个请求（不是每包一次）。"""
        transport = FakeTransport({"*": replay(diagnosis("下单_0"), diagnosis("下单_1"))})

        analyze_summary(
            make_summary(failures=2),
            transport=transport,
            config=CONFIG,
            doc_text=DOC,
            write=False,
        )

        self.assertEqual(transport.call_count, 1)

    def test_evidence_backed_conclusion_is_kept(self):
        transport = FakeTransport({"*": replay(diagnosis("下单_0"))})

        result = analyze_summary(
            make_summary(failures=1),
            transport=transport,
            config=CONFIG,
            doc_text=DOC,
            write=False,
        )

        self.assertEqual((result.kept, result.degraded), (1, 0))
        self.assertNotIn(DEGRADED_MARK, result.diagnoses[0].reason)

    def test_fabricated_quote_is_degraded_to_unknown(self):
        """★验收②：降级**100% 显式标"未知"**（类别落未知 + 置信度归 0 + 标出原因）。"""
        transport = FakeTransport(
            {"*": replay(diagnosis("下单_0", quotes=[{"source": "response", "text": "编的"}]))}
        )

        result = analyze_summary(
            make_summary(failures=1),
            transport=transport,
            config=CONFIG,
            doc_text=DOC,
            write=False,
        )

        diag = result.diagnoses[0]
        self.assertEqual(diag.cause_type, CAUSE_UNKNOWN)
        self.assertEqual(diag.confidence, 0.0)
        self.assertIn(DEGRADED_MARK, diag.reason)
        self.assertEqual(result.degraded, 1)

    def test_self_reported_unknown_counts_as_not_degraded(self):
        """★口径：模型**自报**「未知待人工确认」是**遵守规则**，算**未降级**。

        否则模型越守规矩、"未降级占比"越低——指标会把好消息读成坏消息。
        """
        transport = FakeTransport(
            {"*": replay(diagnosis("下单_0", cause=CAUSE_UNKNOWN, quotes=[]))}
        )

        result = analyze_summary(
            make_summary(failures=1),
            transport=transport,
            config=CONFIG,
            doc_text=DOC,
            write=False,
        )

        self.assertEqual(result.kept, 1)
        self.assertEqual(result.degraded, 0)
        self.assertEqual(result.diagnoses[0].confidence, 0.0, "未知不该带置信度")

    def test_quote_from_another_pack_is_rejected(self):
        """★引用必须对得上**那一条**证据包——"引用存在"不等于"引用有据"（§5.3）。"""
        summary = make_summary(failures=2)
        summary["details"][1]["records"][0]["data"]["req_resps"][0]["response"]["body"] = (
            '{"code": 9999, "message": "另一条"}'
        )
        transport = FakeTransport(
            {
                "*": replay(
                    diagnosis("下单_0", quotes=[{"source": "response", "text": "另一条"}]),
                    # ★§9.24 契约要求"每个证据包一条"→ 2 包就得 2 条（少一条会被回喂重试）
                    diagnosis("下单_1", cause=CAUSE_UNKNOWN, quotes=[]),
                )
            }
        )

        result = analyze_summary(
            summary, transport=transport, config=CONFIG, doc_text=DOC, write=False
        )

        self.assertEqual(
            result.diagnoses[0].cause_type,
            CAUSE_UNKNOWN,
            "拿别的用例的响应当证据，不该通过校验",
        )

    def test_kept_ratio_is_computed_and_reported(self):
        """★验收①：**未降级占比**（分子=没被代码降级的，分母=全部）——而且**必须可见**。"""
        transport = FakeTransport(
            {
                "*": replay(
                    diagnosis("下单_0"),
                    diagnosis("下单_1", cause=CAUSE_UNKNOWN, quotes=[]),
                    diagnosis("下单_0", quotes=[{"source": "response", "text": "编的"}]),
                )
            }
        )

        result = analyze_summary(
            make_summary(failures=3),
            transport=transport,
            config=CONFIG,
            doc_text=DOC,
            write=False,
        )

        self.assertEqual((result.kept, result.degraded), (2, 1))
        self.assertAlmostEqual(result.kept_ratio(), 2 / 3, places=4)
        self.assertIn("未降级占比", render_analysis_report(result))

    def test_no_failures_means_no_call_and_no_report(self):
        """没有失败断言 → **不调模型、不产报告**（"空报告"会被误读成"分析过了"）。"""
        transport = FakeTransport({"*": replay(diagnosis("下单_0"))})

        result = analyze_summary(
            {"details": [{"name": "全过", "success": True, "records": []}]},
            transport=transport,
            config=CONFIG,
            write=True,
        )

        self.assertEqual(transport.call_count, 0)
        self.assertEqual(result.packs, 0)
        self.assertFalse(os.path.exists("reports/analysis.md"))

    def test_log_quote_is_checked_against_the_pack_haystack(self):
        """★P1a 兑现 T29 留的那行 TODO：`log` 引用**真的**被逐字校验。

        `analyze` 传进去的 haystack 是 `响应体 + 日志关键行`，所以
        ① 真实日志行 → 通过；② 编的日志行 → 降级（见下一条元护栏）。
        （对照：`validate_diagnosis` 在**调用方没给原文**时按放行处理——
        "没有可查的原文"与"原文里没有"是两件事。）
        """
        log_path = os.path.join(os.getcwd(), "run.log")
        with open(log_path, "w", encoding="utf-8") as fp:
            fp.write("INFO all good\nWARNING body.code mismatch\n")
        transport = FakeTransport(
            {
                "*": replay(
                    diagnosis(
                        "下单_0",
                        quotes=[{"source": "log", "text": "WARNING body.code mismatch"}],
                    )
                )
            }
        )

        result = analyze_summary(
            make_summary(failures=1, log=log_path),
            transport=transport,
            config=CONFIG,
            doc_text=DOC,
            write=False,
        )

        self.assertEqual(result.diagnoses[0].cause_type, "真实缺陷", "真实日志行应当通过")

    def test_fabricated_log_quote_is_degraded(self):
        """元护栏：编的日志引用必须降级（否则"校验 log"就是句空话）。"""
        log_path = os.path.join(os.getcwd(), "run.log")
        with open(log_path, "w", encoding="utf-8") as fp:
            fp.write("INFO all good\n")
        transport = FakeTransport(
            {
                "*": replay(
                    diagnosis(
                        "下单_0", quotes=[{"source": "log", "text": "WARNING 这句不在日志里"}]
                    )
                )
            }
        )

        result = analyze_summary(
            make_summary(failures=1, log=log_path),
            transport=transport,
            config=CONFIG,
            doc_text=DOC,
            write=False,
        )

        self.assertEqual(result.diagnoses[0].cause_type, CAUSE_UNKNOWN)


class TestOfflineAndAtomicity(_TempWorkspace):
    """验收③④：投影稳定 / 断网无部分写。"""

    def _run_once(self):
        analyze_summary(
            make_summary(failures=1),
            transport=FakeTransport({"*": replay(diagnosis("下单_0"))}),
            config=CONFIG,
            doc_text=DOC,
            write=True,
        )

    def test_transport_failure_writes_nothing(self):
        """★验收④：**断网 → 一个文件都没写**（半份报告比没有报告更坏）。"""
        transport = FakeTransport({"*": ""}, failures=99)

        with self.assertRaises(LLMTransportError):
            analyze_summary(
                make_summary(failures=1),
                transport=transport,
                config=CONFIG,
                doc_text=DOC,
                write=True,
            )

        self.assertFalse(os.path.isdir("reports"), "断网时不该创建 reports/")

    def test_report_is_written_atomically(self):
        """原子写：落盘后不该有 `.tmp` 残留。"""
        self._run_once()

        self.assertEqual(os.listdir("reports"), ["analysis.md"])

    def test_report_is_byte_identical_across_runs(self):
        """★验收③：同输入两次运行 → 报告**逐字节相同**（报告里不含时间戳等易变字段）。"""
        self._run_once()
        with open("reports/analysis.md", encoding="utf-8") as fp:
            first = fp.read()
        os.remove("reports/analysis.md")
        self._run_once()
        with open("reports/analysis.md", encoding="utf-8") as fp:
            second = fp.read()

        self.assertEqual(first, second)

    def test_doc_defects_are_written_only_when_present(self):
        """文档缺陷回流：**无缺陷不落盘**（空清单会被当成"有活要干"）。"""
        self._run_once()

        self.assertFalse(os.path.exists("reports/doc_defects.md"))


class TestCliAnalyze(_TempWorkspace):
    """`haify analyze` 的 CLI 契约。"""

    def _write_summary(self):
        with open("summary.json", "w", encoding="utf-8") as fp:
            json.dump(make_summary(failures=1), fp, ensure_ascii=False)
        return "summary.json"

    def test_missing_summary_is_a_config_error(self):
        from interfacetester_ai.cli import EXIT_CONFIG, main  # noqa: PLC0415

        self.assertEqual(main(["analyze", "nope.json"]), EXIT_CONFIG)

    def test_offline_exit_code_is_three(self):
        """★验收④：**断网 → 退出码 3**（由 `main` 统一转），且此时没有半个报告。"""
        from interfacetester_ai.cli import EXIT_TRANSPORT, main  # noqa: PLC0415

        path = self._write_summary()
        with mock.patch(
            "interfacetester_ai.cli.LLMConfig.from_env", return_value=CONFIG
        ), mock.patch(
            "interfacetester_ai.cli.build_transport",
            return_value=(FakeTransport({"*": ""}, failures=99), "fake"),
        ), mock.patch(
            "interfacetester_ai.cli.CacheStore", return_value=None
        ), mock.patch(
            "interfacetester_ai.cli.ManifestStore", return_value=None
        ):
            self.assertEqual(main(["analyze", path]), EXIT_TRANSPORT)

        self.assertFalse(os.path.isdir("reports"))


class TestWriteBoundaryAndSelftest(unittest.TestCase):
    """写盘边界（T18）与自检本身。"""

    def test_analyze_is_registered_as_a_reports_writer(self):
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        self.assertIn("analyze", MODULE_WRITE_BOUNDARIES)
        self.assertEqual(MODULE_WRITE_BOUNDARIES["analyze"], ("roots", ("reports/",)))

    def test_evidence_is_not_a_writer(self):
        """`evidence` 只读不写——**不写盘的模块不该进注册表**（登记本身就是一次签字）。"""
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        self.assertNotIn("evidence", MODULE_WRITE_BOUNDARIES)

    def test_selftest_is_green(self):
        from interfacetester_ai.analyze import run_selftest  # noqa: PLC0415

        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_the_quote_check_is_neutered(self):
        """元护栏：把"引用必须是原文"放宽成"永远通过" → 自检必红。

        这条证明"证据纪律"真的在起作用，而不是自检恰好在别的分支上通过。
        """
        import interfacetester_ai.diagnosis as diagnosis_module  # noqa: PLC0415

        with mock.patch.object(
            diagnosis_module, "validate_diagnosis", lambda *a, **k: (True, "")
        ):
            self.assertNotEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_degradation_becomes_a_no_op(self):
        """元护栏：把归一化改成**恒等**（降级不再发生）→ 自检必红。"""
        import interfacetester_ai.analyze as analyze_module  # noqa: PLC0415

        with mock.patch.object(
            analyze_module, "normalize_diagnosis", lambda diag, *a, **k: diag
        ):
            self.assertNotEqual(analyze_module.run_selftest(), 0)


def _sequence(*texts):
    """按**调用次序**回放的 `FakeTransport`（第 N 次给第 N 个文本；用完就重复最后一个）。"""
    transport = FakeTransport()

    def responder(request):  # noqa: ARG001 - 只用调用次序
        position = min(len(transport.calls), len(texts)) - 1
        return texts[position]

    transport.responder = responder
    return transport


class TestDiagnosisContractAndRefineLoop(_TempWorkspace):
    """★§9.24 **输出契约 + 修正环** —— 直接对策于"能不能跑只看模型"那次事故。

    事故（2026-09-27 实测，同一份真 summary、同一台 ollama）：
    `qwen3.8:27b` 成功（6/6 未降级、引用可溯源）；`gemma4:latest`/`gemma4:12b`
    返回 `<unused50>`×N（**不是 JSON**）→ 整次失败、无报告 ✗。
    两个原因缺一不可：**prompt 没给输出形状** + **没有修正环**（`llm.complete` 的重试只覆盖传输层）。
    """

    # ---- ① 契约在场，且与校验同源 ----

    def test_contract_is_rendered_into_system_prompt(self):
        from interfacetester_ai.prompts import render_diagnosis_system_prompt  # noqa: PLC0415

        prompt = render_diagnosis_system_prompt(2)
        self.assertEqual(audit_diagnosis_contract(prompt), [], "诊断 prompt 里缺输出契约")
        self.assertIn("**2** 条", prompt, "契约里应当写明本轮证据包条数（必须 N 条）")

    def test_contract_derives_from_cause_types(self):
        """元护栏：加一类 → 契约跟着出现（手抄一份必然漂移）。"""
        import interfacetester_ai.diagnosis as diag_mod  # noqa: PLC0415

        with mock.patch.object(
            diag_mod, "CAUSE_TYPES", diag_mod.CAUSE_TYPES + ("第七类",)
        ):
            text = diag_mod.diagnosis_contract_text(1)
        self.assertIn("第七类", text)

    def test_audit_catches_removed_contract(self):
        from interfacetester_ai.prompts import render_diagnosis_system_prompt  # noqa: PLC0415

        prompt = render_diagnosis_system_prompt(1)
        damaged = prompt.replace("evidence_quotes", "")
        self.assertNotEqual(prompt, damaged)
        self.assertIn("evidence_quotes", audit_diagnosis_contract(damaged))

    # ---- ② 修正环：坏 → 好 ----

    def test_bad_json_is_retried_then_recovers(self):
        transport = _sequence(
            "<unused50><unused50><unused50>",
            replay(diagnosis("下单_0"), diagnosis("下单_1")),
        )

        result = analyze_summary(
            make_summary(failures=2), transport=transport, config=CONFIG, doc_text=DOC, write=False
        )

        self.assertEqual(transport.call_count, 2, "应当首发 1 次 + 修正 1 次")
        self.assertEqual(len(result.diagnoses), 2)
        self.assertEqual([item.kind for item in result.rounds], ["l1-format"])

    def test_retry_changes_the_input(self):
        """★本仓口径：**重试必须改变输入**（重发同样的 prompt 只会得到同样的错误）。"""
        transport = _sequence("<unused50>", replay(diagnosis("下单_0")))
        analyze_summary(
            make_summary(failures=1), transport=transport, config=CONFIG, doc_text=DOC, write=False
        )

        self.assertEqual(transport.call_count, 2)
        self.assertNotEqual(transport.calls[0].user, transport.calls[1].user)
        self.assertIn("上一轮被拦下", transport.calls[1].user)

    def test_contract_violation_is_retried_with_the_reason(self):
        """结构错（少一条）也回喂，且**回喂文本里说清是哪一条**。"""
        transport = _sequence(
            replay(diagnosis("下单_0")),
            replay(diagnosis("下单_0"), diagnosis("下单_1")),
        )

        result = analyze_summary(
            make_summary(failures=2), transport=transport, config=CONFIG, doc_text=DOC, write=False
        )

        self.assertEqual(transport.call_count, 2)
        self.assertEqual(result.rounds[0].kind, "l1-schema")
        self.assertIn("给了 1 条", result.rounds[0].message)
        self.assertIn("给了 1 条", transport.calls[1].user, "回喂文本要进第二轮 prompt")

    def test_taxonomy_and_evidence_are_not_structural(self):
        """★分工判据：类别不合法 / 引用编造 → **降级**（不重试）；只有**形状**错才重试。

        混起来会把"模型守规矩地说不知道"当结构失败去重试，白烧钱 ✗。
        """
        transport = FakeTransport(
            {
                "*": replay(
                    diagnosis("下单_0", cause="第七类"),
                    diagnosis("下单_1", quotes=[{"source": "response", "text": "编的"}]),
                )
            }
        )

        result = analyze_summary(
            make_summary(failures=2), transport=transport, config=CONFIG, doc_text=DOC, write=False
        )

        self.assertEqual(transport.call_count, 1, "类别/证据问题不该触发重试")
        self.assertEqual((result.kept, result.degraded), (0, 2))

    def test_exhausted_rounds_fail_loudly_and_write_nothing(self):
        """用尽 → 抛 `DiagnosisFailed`（CLI 退出码 4），且**一个文件都没写**。"""
        transport = FakeTransport({"*": "<unused50>"})

        with self.assertRaises(DiagnosisFailed) as caught:
            analyze_summary(
                make_summary(failures=1),
                transport=transport,
                config=CONFIG,
                doc_text=DOC,
                write=True,
                max_rounds=1,
            )

        self.assertEqual(len(caught.exception.rounds), 2)
        self.assertEqual(transport.call_count, 2)
        self.assertFalse(os.path.isdir("reports"), "用尽时不该创建 reports/（不产半成品）")

    def test_strict_and_lenient_parsing_both_available(self):
        """宽容解析仍在（下游逐条判合格）；`strict` 只多一条"形状"判据。"""
        with self.assertRaises(DiagnosisContractError):
            parse_diagnoses(replay(diagnosis("下单_0")), strict=True, expected=2)
        self.assertEqual(len(parse_diagnoses(replay(diagnosis("下单_0")))), 1)

    def test_default_rounds_match_gen(self):
        """轮数**从 gen 取**（不手抄一个 2——抄了必然漂移）。"""
        from interfacetester_ai import analyze as analyze_mod  # noqa: PLC0415
        from interfacetester_ai.gen import MAX_ROUNDS as GEN_ROUNDS  # noqa: PLC0415

        self.assertEqual(analyze_mod.MAX_ROUNDS, GEN_ROUNDS)

    def test_cli_wires_max_rounds_and_exit_code(self):
        """CLI：`--max-rounds` 能传进去；用尽时退出码 **4**（与 gen 的"产出失败"同码）。"""
        import interfacetester_ai.cli as cli_mod  # noqa: PLC0415

        args = cli_mod.build_parser().parse_args(["analyze", "s.json", "--max-rounds", "3"])
        self.assertEqual(args.max_rounds, 3)

        with open("summary.json", "w", encoding="utf-8") as fp:
            fp.write("{}")
        env = {
            "INTERFACETESTER_AI_BASE_URL": "http://127.0.0.1:11434/v1",
            "INTERFACETESTER_AI_MODEL": "fake",
        }
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(cli_mod, "analyze_summary", side_effect=DiagnosisFailed([])):
                code = cli_mod.main(["analyze", "summary.json"])
        self.assertEqual(code, cli_mod.EXIT_DOC_UNFIT)


if __name__ == "__main__":
    unittest.main()




