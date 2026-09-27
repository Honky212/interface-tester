# -*- coding: utf-8 -*-
"""`haify gen` 端到端护栏 —— §5.1 生成闭环 + §六 接入层（**P0a** E 阶段）。

## 这个文件补的是"链路级"判据

前面各模块的测试都是**分段**的（切片器 / llm / schema / assembler 各自的判据）。
但 P0a 的验收是**链路级**的：① 生成物真的落进 `cases/`、② 明文凭据 0、
③ 引用不命中被拦、④ **FakeLLM 下离线全绿**、⑤ 预算约束、⑥ 缓存键含采样参数、⑦ 零污染。

所以这里**不出网**（`FakeTransport` 回放）+ **临时工作区**（`cases/`、`.ai/`、`reports/`
都是相对根），把整条链跑一遍。`--fake` 的语义也在这里被钉住：**只许命中缓存**。
"""

import glob
import json
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.cache import CacheStore  # noqa: E402
from interfacetester_ai.gen import MAX_ROUNDS, generate_case, generate_cases  # noqa: E402
from interfacetester_ai.llm import FakeTransport, LLMConfig  # noqa: E402
from interfacetester_ai.manifest import ManifestStore  # noqa: E402
from interfacetester_ai.prompts import (  # noqa: E402
    INPUT_BUDGET_BYTES,
    PROMPT_VERSION,
    InputBudgetExceeded,
)
from interfacetester_ai.slicer import Slice  # noqa: E402

CONFIG = LLMConfig(base_url="http://127.0.0.1:11434/v1", model="qwen2.5:14b")

DOC = """# 下单接口

## POST /api/order

返回 200 表示成功。

| 字段 | 类型 | 必填 |
| --- | --- | --- |
| amount | number | 是 |
| remark | string | 可选 |

说明：

- amount：整数（int），单位分
- remark：可选字符串
"""

GOOD_DRAFT = {
    "case_name": "模型自报的名字（应被片标题覆盖）",
    "steps": [
        {
            "name": "下单",
            "method": "post",
            "url": "/api/order",
            "json": {"amount": 100},
            "validate": [
                {
                    "comparator": "equal",
                    "check": "status_code",
                    "expect": 200,
                    "source_quote": "返回 200 表示成功",
                },
                {
                    "comparator": "type_match",
                    "check": "body.amount",
                    "expect": "int",
                    "source_quote": "amount：整数（int），单位分",
                },
            ],
        }
    ],
}

# 结构错（断言缺 source_quote）：用来驱动修正环的 l1-schema 分支
BAD_SCHEMA_DRAFT = {
    "case_name": "坏的",
    "steps": [
        {
            "name": "下单",
            "method": "post",
            "url": "/api/order",
            "json": {"amount": 100},
            "validate": [{"comparator": "equal", "check": "status_code", "expect": 200}],
        }
    ],
}

# 陷阱：空 schema（S1 硬拦；expect 是 dict，所以能过 T28 的 ② 检查）
TRAP_DRAFT = {
    "case_name": "陷阱",
    "steps": [
        {
            "name": "下单",
            "method": "post",
            "url": "/api/order",
            "json": {"amount": 100},
            "validate": [
                {
                    "comparator": "jsonschema_match",
                    "check": "body.data",
                    "expect": {},
                    "source_quote": "amount：整数（int），单位分",
                }
            ],
        }
    ],
}


def _json_text(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _processed(results):
    """**真正被处理过**的片（剔除"本就没有接口"而跳过的标题 / 概述片）。

    ★为什么测试要过滤，而不是改夹具文档：`DOC` 的头一行 `# 下单接口` 是**真实文档的
    常见形态**（一级标题写文档名、二级标题才是接口）。它没有 `METHOD /path`，2026-09-25
    起被判为"本片没有接口"**跳过**。改动前它会被送去调模型、然后失败 —— 而结局和真失败
    混在一起，这正是本轮要修的"分母错了"。这些测试关心的是**接口片**的行为，所以过滤；
    而"标题片确实被跳过、且**没烧模型**"由 `TestNonInterfaceSlices` 专门盯住。
    """
    return [item for item in results if item.outcome() != "skip"]


class _PipelineTest(unittest.TestCase):
    """临时工作区 + 离线 transport 的公共骨架。"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_gen_")
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def _tree(self):
        found = []
        for root, _dirs, files in os.walk("."):
            for name in files:
                found.append(os.path.relpath(os.path.join(root, name), ".").replace("\\", "/"))
        return sorted(found)

    def _run(self, payload, *, doc=DOC, merge=True):
        transport = FakeTransport(
            {"*": payload if isinstance(payload, str) else _json_text(payload)}
        )
        results = generate_cases(
            doc,
            transport=transport,
            config=CONFIG,
            cache=CacheStore(),
            audit=ManifestStore(),
            merge=merge,
        )
        return transport, results


class TestEndToEndProducesRealCases(_PipelineTest):
    """① 健康文档 → **草稿**真的落在 `.ai/draft/`（★默认**不**进 `cases/`，§九 第一档②）；⑦ 只写登记根。"""

    def test_draft_lands_in_draft_dir_and_passes_kernel_loader(self):
        """★2026-09-26（§九 第一档②）：链路级验收从"落进 `cases/`"改为"**落进 `.ai/draft/`**"
        —— 装配与发布分成两步，"装配成功"不再等于"进正式用例目录"。
        """
        from interfacetester.loader import load_testcase_file  # noqa: PLC0415

        _transport, results = self._run(GOOD_DRAFT)

        self.assertTrue(results)
        for item in _processed(results):
            self.assertTrue(item.ok, item.to_dict())
            self.assertTrue(
                # ★Windows 上路径分隔符是 `\`：判据要跟分隔符无关（实测踩到）
                item.assembly.draft_yaml_path.replace("\\", "/").startswith(".ai/draft/"),
                item.assembly.draft_yaml_path,
            )
            self.assertTrue(load_testcase_file(item.assembly.draft_yaml_path))
            self.assertEqual(item.assembly.yaml_path, "", "★装配不该写 `cases/`")
        self.assertEqual(glob.glob("cases/*.yml"), [], "★默认落点必须是草稿区")

    def test_case_name_follows_the_slice_title_not_the_model(self):
        """用例名以**片标题**为准：模型自报的名字不可复现（两次生成会撞车）。"""
        _transport, results = self._run(GOOD_DRAFT)

        for item in results:
            self.assertNotEqual(item.case_name, "模型自报的名字（应被片标题覆盖）")

    def test_new_files_only_under_registered_roots(self):
        before = self._tree()
        self._run(GOOD_DRAFT)
        new = [path for path in self._tree() if path not in before]

        stray = [
            path
            for path in new
            if not (
                path.startswith("cases/")
                or path.startswith(".ai/")
                or path.startswith("reports/")
            )
        ]
        self.assertEqual(stray, [], f"落到了登记根之外：{stray}")


class TestAuditAndCache(_PipelineTest):
    """§六 审计行 + 缓存（P0a 验收⑥）。"""

    def test_audit_records_every_call_and_the_raw_response(self):
        audit = ManifestStore()
        transport = FakeTransport({"*": _json_text(GOOD_DRAFT)})

        generate_cases(DOC, transport=transport, config=CONFIG, cache=CacheStore(), audit=audit)

        self.assertTrue(audit.entries)
        manifests = glob.glob(".ai/manifest/*.json")
        self.assertTrue(manifests)
        with open(manifests[0], encoding="utf-8") as fp:
            entry = json.load(fp)
        # 原始响应照录（复盘的一手材料）——只留 hash 等于把证据换成指纹
        self.assertIn("response_text", entry)
        # ★跟着常量走，不写死（2026-09-25 实测纠正）：prompt 版本会升，
        #   写死版本号的断言会在每次升版时变红——那**不是**判据要挡的东西，
        #   挡住的是"manifest 里没记版本"，而不是"版本号是几"。
        self.assertEqual(entry["prompt_version"], PROMPT_VERSION)
        self.assertEqual(entry["model"], CONFIG.model)

    def test_second_run_hits_cache_without_calling_the_model(self):
        """★⑥ 的链路形态：第二次跑**不再调模型**（命中缓存即回放）。

        ★为什么第二次跑前要清掉 `cases/`：`cases/<用例>.yml` 已存在时，装配器的**查重**
        会硬失败（不覆盖人工工作）——那是**正确行为**，但它会让"第二次跑"走不到
        "产出用例"这一步。清掉产物后再跑，测的才是**缓存回放**这件事本身。
        """
        transport = FakeTransport({"*": _json_text(GOOD_DRAFT)})
        cache = CacheStore()

        generate_cases(DOC, transport=transport, config=CONFIG, cache=cache)
        first_calls = transport.call_count
        self.assertGreater(first_calls, 0)
        for path in glob.glob("cases/*.yml"):
            os.remove(path)

        results = generate_cases(
            DOC, transport=transport, config=CONFIG, cache=cache, doc_check=False
        )

        self.assertTrue(
            all(item.ok for item in _processed(results)), [r.to_dict() for r in results]
        )
        # ★跳过"本就没有接口"的片是**正确行为**（且它连模型都没调），这里顺带钉住：
        #   否则上面那句 `all(ok)` 会变成空断言（过滤后什么也没断言）。
        self.assertTrue(_processed(results), "过滤后没有可断言的片 —— 夹具文档变了？")
        self.assertEqual(
            transport.call_count, first_calls, "第二次跑应当全部命中缓存，不再调模型"
        )

    def test_response_can_be_replayed_from_cache_offline(self):
        """`--fake` 的底层语义：**缓存里有回放**时，空 transport 也能跑通。"""
        cache = CacheStore()
        generate_cases(
            DOC, transport=FakeTransport({"*": _json_text(GOOD_DRAFT)}), config=CONFIG, cache=cache
        )
        # 清掉 cases/ 与草稿产物，只保留缓存
        # NOTICE：只删**文件**——`.ai/draft/` 下还会有 `__pycache__`（`*_test.py` 被 import
        # 时生成），`os.remove` 撞上目录会 `PermissionError`（实测踩到）。
        for pattern in ("cases/*.yml", ".ai/draft/*.yml", ".ai/draft/*.json"):
            for path in glob.glob(pattern):
                if os.path.isfile(path):
                    os.remove(path)

        replay = FakeTransport({})  # 空回放表：**只可能**靠缓存命中
        results = generate_cases(
            DOC, transport=replay, config=CONFIG, cache=cache, doc_check=False
        )
        self.assertTrue(all(item.ok for item in _processed(results)), [r.to_dict() for r in results])
        self.assertEqual(replay.call_count, 0, "纯回放不该触碰 transport")


class TestCorrectionLoop(_PipelineTest):
    """修正环：≤2 轮，按类别回喂（§5.1）。"""

    def _recovering_transport(self):
        """每片的**第一次**调用给坏 JSON；带"上一轮被拦下"的是修正轮 → 给好的。

        ★为什么按 **prompt 内容**判而不是按调用序号：序号是**全局**的，第一片修好之后
        序号已经走过，第二片的首发会直接拿到好 JSON——那样测的就不是修正环了
        （实测踩到：断言 `['l1-schema','ok']` 在第二片上失败）。
        """
        transport = FakeTransport(
            responder=lambda request: _json_text(
                GOOD_DRAFT if "[上一轮被拦下" in request.user else BAD_SCHEMA_DRAFT
            )
        )
        return transport

    def test_schema_error_is_fed_back_and_recovered(self):
        """★第一轮结构错 → 第二轮修好：`rounds` 里要留下"被拦下过"的痕迹。"""
        transport = self._recovering_transport()

        results = generate_cases(DOC, transport=transport, config=CONFIG, cache=CacheStore())

        self.assertTrue(results)
        processed = _processed(results)
        for item in processed:
            self.assertTrue(item.ok, item.to_dict())
            kinds = [rnd.kind for rnd in item.rounds]
            self.assertEqual(kinds, ["l1-schema", "ok"], kinds)
        self.assertEqual(transport.call_count, 2 * len(processed), "每片：首发 + 一轮修正")

    def test_gives_up_after_max_rounds(self):
        """一直错 → 用尽 `max_rounds` → 不产用例（**不产半成品**）。"""
        transport = FakeTransport(responder=lambda request: _json_text(BAD_SCHEMA_DRAFT))

        results = generate_cases(DOC, transport=transport, config=CONFIG, cache=CacheStore())

        self.assertTrue(results)
        processed = _processed(results)
        for item in processed:
            self.assertFalse(item.ok)
            self.assertEqual(len(item.rounds), 1 + MAX_ROUNDS, "首发 1 轮 + 修正 2 轮")
        self.assertEqual(glob.glob("cases/*.yml"), [], "一直失败却产出了用例")

        # ★调用次数是 `每片 2 次` 而不是 `每片 3 次`：第 2、3 轮的 prompt **完全相同**
        # （回喂的就是同一份报错）→ **命中缓存**，不重复调模型。
        # 这正是"缓存键含全部请求字段"的正面效果：重试的重复部分零成本。
        self.assertEqual(transport.call_count, 2 * len(processed))


class TestGateAndBudget(_PipelineTest):
    """S-4 写盘前置 + 输入预算（P0a 验收③⑤）。"""

    def test_merge_adds_documented_field_assertions_end_to_end(self):
        """★C-2（接线后的端到端判据）：模型只给协议级断言 → 合并补上**文档里**的字段级断言。"""
        protocol_only = {
            "case_name": "只给状态码",
            "steps": [
                {
                    "name": "下单",
                    "method": "post",
                    "url": "/api/order",
                    "json": {"amount": 100},
                    "validate": [
                        {
                            "comparator": "equal",
                            "check": "status_code",
                            "expect": 200,
                            "source_quote": "返回 200 表示成功",
                        }
                    ],
                }
            ],
        }
        _transport, results = self._run(protocol_only)

        item = _processed(results)[0]
        self.assertTrue(item.ok, item.to_dict())
        checks = {a.check for a in item.draft.assertions()}
        self.assertIn("status_code", checks, "模型那条必须留着（只补不覆盖）")
        self.assertIn(
            "body.amount",
            checks,
            f"文档里写明「amount：整数（int）」→ 合并应补上字段级断言，实际 {sorted(checks)}",
        )

    def test_no_merge_assertions_leaves_the_draft_alone(self):
        """★C-2 的回滚判据（端到端）：`merge=False` 时草稿**一条都不多**。"""
        protocol_only = {
            "case_name": "只给状态码",
            "steps": [
                {
                    "name": "下单",
                    "method": "post",
                    "url": "/api/order",
                    "json": {"amount": 100},
                    "validate": [
                        {
                            "comparator": "equal",
                            "check": "status_code",
                            "expect": 200,
                            "source_quote": "返回 200 表示成功",
                        }
                    ],
                }
            ],
        }
        _transport, results = self._run(protocol_only, merge=False)

        checks = {a.check for a in _processed(results)[0].draft.assertions()}
        self.assertEqual(checks, {"status_code"}, f"关掉合并后草稿被改动了：{sorted(checks)}")

    def test_trap_never_lands_in_cases(self):
        """③+S-4：闸门拒的产物**不进 cases/**，且留下"人接着要干什么"。"""
        _transport, results = self._run(TRAP_DRAFT)

        for item in _processed(results):
            self.assertFalse(item.ok)
            self.assertIn("gate", [rnd.kind for rnd in item.rounds])
            self.assertTrue(item.assembly.needs_human_path)
        self.assertEqual(glob.glob("cases/*.yml"), [], "带陷阱的产物落进了 cases/")
        self.assertTrue(glob.glob(".ai/failed/*/NEEDS_HUMAN.md"))

    def test_budget_overflow_is_blocked_before_sending(self):
        """⑤：超预算必须挡在**发请求之前**，而且**不重试**（重试不会变好，只会重复花钱）。

        ★夹具按 `INPUT_BUDGET_BYTES` **现算**，不写死 7000（2026-09-25 实测纠正）：
        预算常量从 8000 上修到 12000 时，写死 7000 构成的那片**第一轮不再超限**，
        这条判据于是变成了空判据 —— 它实测到的是"第 2 轮加了回喂文本之后才超"
        （`call_count == 1`），而"挡在发请求之前"要的是**第一轮就挡住**。
        判据的语义必须由常量推出来，否则常量一改就静默测偏。
        """
        huge_text = "x" * (INPUT_BUDGET_BYTES + 1000)
        huge = Slice(
            index=1,
            title_path="# 大节",
            text=huge_text,
            start_line=1,
            end_line=2,
            byte_size=len(huge_text),
        )
        transport = FakeTransport({"*": _json_text(GOOD_DRAFT)})

        with self.assertRaises(InputBudgetExceeded):
            generate_case(
                huge, "# 大节\n" + huge_text, transport=transport, config=CONFIG
            )

        self.assertEqual(transport.call_count, 0, "预算超限时不该有任何请求发出")


class TestContentGateAndDocCheck(_PipelineTest):
    """② 明文凭据 0 + S-10 文档体检前置。"""

    def test_secret_is_redacted_before_being_sent(self):
        """★②：内容闸在**送出去之前**把凭据换成 `${ENV(...)}` 占位。"""
        doc = DOC + "\n- 内部令牌：sk-abcdefghijklmnopqrstuvwx0\n"
        transport = FakeTransport({"*": _json_text(GOOD_DRAFT)})

        results = generate_cases(doc, transport=transport, config=CONFIG, cache=CacheStore())

        sent = "\n".join(call.user for call in transport.calls)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx0", sent, "明文凭据被送给了模型")
        self.assertIn("${ENV(OPENAI_KEY)}", sent, "应当换成占位符")
        # 替换清单要给出（否则用户不知道"送出去的是改过的"），且清单自身不泄露明文
        self.assertTrue(results[0].redactions)
        self.assertNotIn("abcdefghijkl", str(results[0].redactions))

    def test_document_that_looks_like_an_interface_but_is_incomplete_is_refused(self):
        """S-10：**看起来是接口**但缺要素（URL/method/字段表）→ 拒生成 + 交回缺项清单。

        ★为什么不用"一段完全不像接口的话"当样本：`doc_quality` **刻意不误伤**
        非接口文本（§十 T27 的"合规必绿：非接口切片不误伤"）——那种文本连 D1 都不判，
        拿它测前置闸门会得到"没被拒"的假象（实测踩到）。
        """
        from interfacetester_ai.gen import GenerationFailed  # noqa: PLC0415

        # 有 method（GET）与字段表，但**缺 URL** —— 这样它才"像接口"，
        # `doc_quality` 的 D1 才会去逐项判必需要素（实测：什么都没的文本它不判）。
        unfit = (
            "# 用户服务\n\n"
            "## 查询用户\n\n"
            "GET 查询用户详情。\n\n"
            "| 字段 | 类型 |\n| --- | --- |\n| id | string |\n"
        )

        with self.assertRaises(GenerationFailed):
            generate_cases(unfit, transport=FakeTransport({"*": "{}"}), config=CONFIG)

        self.assertTrue(glob.glob("reports/NEEDS_DOC_FIX.md"), "拒生成时必须落下缺项清单")
        self.assertEqual(glob.glob("cases/*.yml"), [])


class TestCacheStoreCorruption(_PipelineTest):
    """缓存损坏按 **miss** 处理（判据从 `cache.run_selftest` 移到这里——生产模块不自写夹具）。"""

    def test_corrupted_cache_is_treated_as_miss(self):
        store = CacheStore()
        written = store.put("deadbeef", '{"ok": true}')

        with open(written, "w", encoding="utf-8") as fp:
            fp.write("{ 这不是 JSON")

        self.assertIsNone(store.get("deadbeef"), "损坏的缓存不许冒充命中")
        # 且它**不妨碍**重新落盘（下次真调用会覆盖它）
        store.put("deadbeef", '{"ok": false}')
        self.assertEqual(store.get("deadbeef"), '{"ok": false}')


class TestCliExitCodes(_PipelineTest):
    """CLI 退出码口径（§六 配置行 + §五 文档体检）。"""

    def test_missing_config_exits_2(self):
        from unittest import mock  # noqa: PLC0415

        from interfacetester_ai.cli import EXIT_CONFIG, main  # noqa: PLC0415

        with open("doc.md", "w", encoding="utf-8") as fp:
            fp.write(DOC)

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["gen", "doc.md"]), EXIT_CONFIG)

    def test_no_command_prints_help_and_exits_2(self):
        from interfacetester_ai.cli import EXIT_CONFIG, main  # noqa: PLC0415

        self.assertEqual(main([]), EXIT_CONFIG)

    def test_missing_document_exits_4(self):
        from interfacetester_ai.cli import EXIT_DOC_UNFIT, main  # noqa: PLC0415

        self.assertEqual(main(["gen", "不存在.md"]), EXIT_DOC_UNFIT)

    def test_unsupported_suffix_exits_4(self):
        from interfacetester_ai.cli import EXIT_DOC_UNFIT, main  # noqa: PLC0415

        with open("doc.docx", "w", encoding="utf-8") as fp:
            fp.write(DOC)

        self.assertEqual(main(["gen", "doc.docx"]), EXIT_DOC_UNFIT)


class TestFailureGuidanceTellsTheTruth(_PipelineTest):
    """★实测驱动的判据：**装配前**失败的指引不许指向不存在的 `NEEDS_HUMAN.md`。

    起因（2026-09-25 **真模型**实测）：本地模型对 5 片**全部返回 `[]`**，L1 schema 在修正环里
    反复不过 → 0/5 产出、退出码 1。而 CLI 原先**一律**打印
    "未产出的切片在 `.ai/failed/<用例>/NEEDS_HUMAN.md` 里写了人接着要做什么"
    —— **那个文件根本不存在**：`NEEDS_HUMAN.md` 只在装配器的**写盘前置闸门 REJECT** 时才产出，
    而这类失败发生在**装配之前**（`assembly is None`）。用户照着找会一无所获；
    真正有用的证据在 `.ai/manifest/` 的原始响应里（就是那条 `"response_text": "[]"`）。

    两条**成对**判据：装配前 → 指 `.ai/manifest/`；装配后 → **仍然**指 NEEDS_HUMAN。
    只钉一个方向会让"把两类混成一类改坏"重新变得可能。
    """

    def setUp(self):
        super().setUp()
        with open("doc.md", "w", encoding="utf-8") as fp:
            fp.write(DOC)

    @staticmethod
    def _assembly_stub():
        from types import SimpleNamespace  # noqa: PLC0415

        return SimpleNamespace(
            ok=lambda: False,
            yaml_path="",
            draft_yaml_path=".ai/draft/c.yml",
            draft_path=".ai/draft/c.draft.json",
            unknowns_path="",
            pending_path="",
            needs_human_path=".ai/failed/c/NEEDS_HUMAN.md",
            draft_only=True,
            todo_items=(),
            quality=SimpleNamespace(render=lambda: "rejected · structure-rejected"),
            coverage=SimpleNamespace(render=lambda: "覆盖口径：—"),
        )

    def _run(self, results):
        import contextlib  # noqa: PLC0415
        import io  # noqa: PLC0415
        from unittest import mock  # noqa: PLC0415

        from interfacetester_ai.cli import main  # noqa: PLC0415

        env = {
            "INTERFACETESTER_AI_BASE_URL": "http://127.0.0.1:11434/v1",
            "INTERFACETESTER_AI_MODEL": "some-model",
        }
        with mock.patch("interfacetester_ai.cli.generate_cases", return_value=results), \
                mock.patch.dict(os.environ, env, clear=False), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["gen", "doc.md"])
        return code, out.getvalue()

    def test_early_failure_points_at_manifest_not_a_missing_file(self):
        from interfacetester_ai.cli import EXIT_CASE_FAILED  # noqa: PLC0415
        from interfacetester_ai.gen import GenResult, Round  # noqa: PLC0415

        early = GenResult(case_name="c", rounds=[Round(1, "l1-schema", "模型返回空数组")])

        code, text = self._run([early])

        self.assertEqual(code, EXIT_CASE_FAILED)
        self.assertIn(".ai/manifest/", text, "要指到**真正**有证据的地方")
        self.assertNotIn(
            "<用例>/NEEDS_HUMAN.md", text, "装配前失败的指引指向了一个不存在的文件"
        )

    def test_gated_failure_still_points_at_needs_human(self):
        from interfacetester_ai.cli import EXIT_CASE_FAILED  # noqa: PLC0415
        from interfacetester_ai.gen import GenResult, Round  # noqa: PLC0415

        gated = GenResult(
            case_name="c", rounds=[Round(1, "gate", "S1 形态陷阱")], assembly=self._assembly_stub()
        )

        code, text = self._run([gated])

        self.assertEqual(code, EXIT_CASE_FAILED)
        self.assertIn("<用例>/NEEDS_HUMAN.md", text, "闸门拒那一类**本来就该**指 NEEDS_HUMAN")


class TestCrossReferenceSlices(_PipelineTest):
    """★(c)④-①：**交叉引用片**的处置（2026-09-26 真模型实测带出）。

    实测来源：真实文档的 `## 5. 元数据服务接口` 那一片，正文**只有一句交叉引用**
    （`…文件重命名接口（POST /api/metadata/rename）见 [3.5 文件重命名](#35-文件重命名)。`）
    → 判据认为"像接口片" → 送模型 → 模型照那句给出**别的段**的 url、**0 断言** →
    白烧一次调用 + 产出一条 `NEEDS_HUMAN` 噪音（`T5__元数据服务接口`，实测 16 分钟那轮的 6 片失败之一）。
    """

    CROSS_ONLY = (
        "## 5. 元数据服务接口\n\n"
        "- > 本节包含元数据查询接口；文件重命名接口（`POST /api/metadata/rename`）"
        "见 [3.5 文件重命名](#35-文件重命名)。\n"
    )

    def test_a_slice_with_only_cross_references_is_skipped(self):
        from interfacetester_ai.gen import slice_looks_like_interface  # noqa: PLC0415

        self.assertFalse(
            slice_looks_like_interface(self.CROSS_ONLY),
            "只有交叉引用的片不该被当成接口片（它没有自己的端点声明）",
        )

    def test_real_endpoint_slices_are_never_skipped(self):
        """★**成对判据**：真接口片一条都不许被跳（标题式 / 表格式 / curl / **混合片**）。"""
        from interfacetester_ai.gen import slice_looks_like_interface  # noqa: PLC0415

        samples = {
            "标题式": "## POST /api/order\n\n创建订单。\n",
            "表格式": "### 2.1 创建目录\n\n| 属性 | 值 |\n| --- | --- |\n"
            "| **URL** | `POST /api/directory/create` |\n",
            "curl": "curl -X POST https://host.example/api/x\n",
            # ★最关键的一条：片里**既有**交叉引用行、**又有**真端点行 → 必须照旧送模型
            "混合片": self.CROSS_ONLY + "- `POST /api/metadata/query`\n",
        }
        for label, text in samples.items():
            with self.subTest(sample=label):
                self.assertTrue(
                    slice_looks_like_interface(text), f"{label} 的真接口片被误跳了"
                )

    def test_cross_reference_rule_needs_both_signals(self):
        """★判据**要两个信号同时成立**：光有内部锚点链接（无端点）不该被当接口；光有端点也不该被剔。"""
        from interfacetester_ai.gen import slice_looks_like_interface  # noqa: PLC0415

        self.assertFalse(slice_looks_like_interface("- 详见 [3.5](#35) 的字段说明\n"))
        self.assertTrue(slice_looks_like_interface("- `POST /api/real/endpoint`\n"))


class TestDropSummaryAcceptsBothShapes(unittest.TestCase):
    """★(c)④-② 的**形状判据**：两条通路的对象形状不同，统计必须都吃得下。

    实测踩到：`_print_drop_summary` 最初只认模型通路的 `GenResult`（`coverage` 挂在 `.assembly` 上），
    于是 `--assertions-only`（传的是 `AssemblyResult`，`coverage` 就在它自己身上）分母恒为 0 ——
    而表现是"**没有可比对的断言**"，**看起来还挺合理**（本仓最防的那类静默失真）。
    """

    def test_summary_reads_both_object_shapes(self):
        from types import SimpleNamespace  # noqa: PLC0415

        from interfacetester_ai.cli import _print_drop_summary  # noqa: PLC0415
        import contextlib  # noqa: PLC0415
        import io  # noqa: PLC0415

        coverage = SimpleNamespace(
            assertion_count=4,
            dropped_assertions=["steps[0].validate[0]"],
            dropped_kinds=[("quote-miss", 1)],
            render=lambda: "x",
        )
        as_model = SimpleNamespace(assembly=SimpleNamespace(coverage=coverage))
        as_plain = SimpleNamespace(coverage=coverage)

        for label, item in (("模型通路(GenResult)", as_model), ("确定性通路(AssemblyResult)", as_plain)):
            with self.subTest(shape=label):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    _print_drop_summary([item])
                text = out.getvalue()
                self.assertIn("1/4 = 25%", text, f"{label} 的分母没算对：{text!r}")
                self.assertIn("quote-miss×1", text)

    def test_summary_says_zero_denominator_is_not_zero_percent(self):
        from types import SimpleNamespace  # noqa: PLC0415

        from interfacetester_ai.cli import _print_drop_summary  # noqa: PLC0415
        import contextlib  # noqa: PLC0415
        import io  # noqa: PLC0415

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _print_drop_summary([])

        self.assertIn("分母 0", out.getvalue())
        self.assertNotIn("= 0%", out.getvalue())


BIND_DOC = """# 下单接口文档

## 1. 统一响应格式

#### 成功响应

**响应示例**:

```json
{"success": true, "message": "下单成功"}
```

## POST /api/order

返回 200 表示成功。

### 请求体

| 字段名 | 类型 | 说明 |
| --- | --- | --- |
| `amount` | int | 金额 |
| `coupon` | string | 优惠券 |
"""

# ★实测形态：模型把 `source_quote` 写成**标题**（`请求体`），而不是"承载该事实的那句话"。
BIND_DRAFT = {
    "case_name": "弱引用",
    "steps": [
        {
            "name": "下单",
            "method": "post",
            "url": "/api/order",
            "json": {"amount": 100},
            "validate": [
                {
                    "comparator": "type_match",
                    # ★§9.32：归属**按路径**判 —— 本夹具的「请求体」表没有「必填」列（= 响应数据表），
                    #   字段挂在 `data` 下，所以写 `body.data.amount`。
                    "check": "body.data.amount",
                    "expect": "int",
                    "source_quote": "请求体",
                },
            ],
        }
    ],
}


class TestEvidenceBindingEndToEnd(_PipelineTest):
    """★WP2.2 端到端：模型引了**标题**（弱引用）→ 用例**照样产出**，且 `unknowns` 里留下 `quote-bound`。

    这是"**证据绑定**"这件事**跟着产物走**的判据：光在内存里救回断言不够 ——
    交付物（`.yml` + `.unknowns.json`）必须同时说明"断言在"与"证据是工具绑的"。
    """

    def test_weak_citation_is_bound_and_recorded_in_artifacts(self):
        _transport, results = self._run(BIND_DRAFT, doc=BIND_DOC)

        # ★这份文档被切成 5 片（标题块），只有 `## POST /api/order` 那一片是**接口片**
        #   —— 其余按设计跳过（`slice_looks_like_interface` 找不到端点形态）。
        produced = [item for item in results if item.ok]
        self.assertEqual(
            len(produced),
            1,
            [(item.case_name, item.outcome()) for item in results],
        )
        drafts = glob.glob(".ai/draft/*.yml")
        self.assertEqual(len(drafts), 1, drafts)
        with open(drafts[0], encoding="utf-8") as handle:
            yaml_text = handle.read()
        self.assertIn("body.data.amount", yaml_text, "绑定后断言必须真的落进产物")

        unknown_files = glob.glob(".ai/draft/*.unknowns.json")
        self.assertEqual(len(unknown_files), 1, unknown_files)
        with open(unknown_files[0], encoding="utf-8") as handle:
            payload = json.load(handle)
        items = {item["kind"]: item for item in payload["items"]}

        self.assertIn("quote-bound", items, f"交付物里必须能看到绑定：{payload['items']}")
        self.assertIn("请求体", items["quote-bound"]["message"])
        self.assertIn("unknown-critical", items["quote-bound"]["hint"])

    def test_a_verbatim_citation_is_not_recorded_as_bound(self):
        """★**成对判据**：模型引的是**原句**时，不许出现 `quote-bound`（否则那个信号就废了）。"""
        clean = json.loads(json.dumps(BIND_DRAFT))
        clean["steps"][0]["validate"][0]["source_quote"] = "| `amount` | int | 金额 |"
        _transport, results = self._run(clean, doc=BIND_DOC)

        self.assertEqual(len([item for item in results if item.ok]), 1)
        kinds = []
        for path in glob.glob(".ai/draft/*.unknowns.json"):
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            kinds.extend(item["kind"] for item in payload["items"])

        self.assertNotIn("quote-bound", kinds, kinds)


class TestNonInterfaceSlices(_PipelineTest):
    """★"本片没有接口"的处置（2026-09-25 真模型实测新增）。

    起因（实测）：`project-three/统一文件服务平台…` 15 片里**只有 2 片是真接口**，
    其余 13 片是标题 / 接口概述 / 服务地址 / 公共请求头 / 响应格式 / 错误码表 /
    目录服务接口（标题）/ 附录 / 路径格式规范 / 代码参考 / 常见问题。旧行为下那 13 片
    **照样被调 3 轮模型**，而模型只能编一个不存在的端点（`/api/status`、
    `/api/placeholder`、`/dummy/success`、`/api/file_service`）或给不出合法结构；
    结局在报告里**和真失败混在一起** —— "15 片里 13 片失败"看起来像工具坏了，
    实际是**分母错了**：它们本就不该产出用例。
    """

    def test_title_slice_is_skipped_without_calling_the_model(self):
        """★最硬的判据：跳过要**省下调用**。

        只断言"它被跳过了"是不够的 —— 若照旧调 3 轮再判跳过，那笔钱还是花掉了。
        `DOC` 有 2 片（`# 下单接口` 标题片 + `## POST /api/order` 接口片），
        所以模型应当**只被调 1 次**。
        """
        transport, results = self._run(GOOD_DRAFT)

        skipped = [item for item in results if item.outcome() == "skip"]
        self.assertEqual([item.slice_title for item in skipped], ["# 下单接口"])
        self.assertEqual(transport.call_count, 1, "标题片不该被送模型")

    def test_skip_reason_is_readable(self):
        """跳过**必须说明为什么**（静默跳过 = "没做"伪装成"做完了"）。"""
        _transport, results = self._run(GOOD_DRAFT)

        for item in results:
            if item.outcome() == "skip":
                self.assertEqual(item.last_kind(), "skip-static")
                self.assertIn("METHOD /path", item.skip_reason())
                self.assertIn("--no-skip-non-interface", item.skip_reason())

    def test_skipped_slice_is_not_a_failure(self):
        """★跳过**不是失败**：不进 `cases/`，但也不需要人去看 NEEDS_HUMAN。"""
        _transport, results = self._run(GOOD_DRAFT)

        skipped = [item for item in results if item.outcome() == "skip"]
        self.assertTrue(skipped, "夹具文档的标题片应当被跳过")
        for item in skipped:
            self.assertFalse(item.ok)
            self.assertEqual(item.outcome(), "skip")  # ← 关键：不是 "fail"
            self.assertIsNone(item.assembly, "跳过不该走到装配")
            self.assertIn("skip_reason", item.to_dict())

    def test_model_saying_there_is_nothing_is_also_a_skip(self):
        """★第二个来源：模型返回 `steps: []`（它自己判断本节没有可测的接口）。

        改动前：送进装配 → 零 step 在 L2 出口校验抛 `ParamsError`（内核原话"零步骤但判
        成功… 绿了但什么都没测"）→ 归成 `l2-emit` → **回喂重试 2 轮**（重试不会变好：
        它已经答对了）→ 报告里和真失败混在一起。
        现在：记 `skip-model`，不回喂、不重试、不落盘。
        """
        transport = FakeTransport({"*": _json_text({"case_name": "空", "steps": []})})

        results = generate_cases(DOC, transport=transport, config=CONFIG, cache=CacheStore())

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].last_kind(), "skip-static", "标题片仍是静态跳过")
        self.assertEqual(results[1].last_kind(), "skip-model", "模型说没有 → 跳过")
        self.assertEqual(results[1].outcome(), "skip")
        self.assertEqual(transport.call_count, 1, "它已经答对了，不该再回喂重试")
        self.assertEqual(glob.glob("cases/*.yml"), [])

    def test_flag_forces_everything_to_the_model(self):
        """`--no-skip-non-interface`：判据再准也不替用户拍板（把选择权交回调用方）。

        ★它顺带说明了两件事：

        ① **端点溯源的口径是整篇文档**（与 `source_quote` 一致）：标题片原文里没有
           `/api/order`，但整篇文档里有 → 那份草稿**不算编造**，两片都能成功。
        ② 所以"跳过非接口片"的价值**不是"避免失败"，而是"避免白花调用 + 产出重复用例"**：
           标题片产出的用例与接口片的那条**指向同一个端点**，是同一条用例的复制品，
           而它花掉了一整轮调用去生成。
        """
        transport = FakeTransport({"*": _json_text(GOOD_DRAFT)})

        results = generate_cases(
            DOC,
            transport=transport,
            config=CONFIG,
            cache=CacheStore(),
            skip_non_interface=False,
        )

        self.assertEqual([item.outcome() for item in results], ["ok", "ok"])
        self.assertEqual(transport.call_count, 2, "两片各 1 次（首发即成功）")
        self.assertEqual(
            len(glob.glob(".ai/draft/*.yml")),
            2,
            "两片都产出了草稿 —— 而它们的内容指向同一端点（★现在落草稿区，不再进 cases/）",
        )
        self.assertEqual(glob.glob("cases/*.yml"), [], "★装配不该写 cases/")


if __name__ == "__main__":
    unittest.main()
