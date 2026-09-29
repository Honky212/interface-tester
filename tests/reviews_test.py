# -*- coding: utf-8 -*-
"""人工确认留痕的护栏用例 —— v8 §十 **T24**（`TODO_` 落盘纪律）。

## T24 的验收是**两条成对**的判据

1. 带 `TODO_` 的产物**不出现在 `cases/`**（由装配器的 `draft_only` 硬规则保证，
   已在 `tests/assertions_test.py` 钉住）；
2. **`.ai/reviews/` 有确认记录**——转正必须留痕（本文件）。

只有 ① 没有 ② 时，人会绕过门："直接把值填了塞进 `cases/`"，于是**没人知道谁确认的**；
只有 ② 没有 ① 时，草稿根本没产生，门也就没意义。所以两条要一起成立。

## 本条最要紧的判据是"哈希绑定"

`require_review` 比对的是**内容哈希**，不是路径。所以第 4 个用例
（`test_hash_mismatch_invalidates_the_record`）是这条纪律的**元护栏**：
确认之后再改一行，留痕必须**失效**——否则"签一次、以后随便改都算签过"，
留痕退化成橡皮图章，还不如没有（至少没有时人会去读草稿）。
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

from interfacetester_ai import reviews as reviews_module  # noqa: E402
from interfacetester_ai.assembler import promote_draft  # noqa: E402
from interfacetester_ai.reviews import (  # noqa: E402
    draft_hash,
    drafts_overview,
    list_drafts,
    quality_of_draft,
    read_review_record,
    require_review,
    review_path,
    run_selftest,
    write_review_record,
)

TODO_DRAFT = (
    "config:\n  name: with_todo\nteststeps:\n- name: with_todo\n  request:\n"
    "    method: POST\n    url: /x\n  validate:\n  - equal:\n    - body.x\n"
    '    - "${ENV(TODO_X)}"\n'
)
FILLED_DRAFT = (
    "config:\n  name: filled\nteststeps:\n- name: filled\n  request:\n"
    "    method: POST\n    url: /x\n  validate:\n  - type_match:\n    - body.x\n    - str\n"
)
# ★S1 的经典 MUST_REJECT 形态（伪存在性断言）：闸门**必拒**。
# 用它钉住"闸门结论是硬结论、`--resolve-blocker` 覆盖不了"。
GATE_TRAP_DRAFT = (
    "config:\n  name: trap\nteststeps:\n- name: trap\n  request:\n"
    "    method: POST\n    url: /x\n  validate:\n"
    '  - not_equal:\n    - body.x\n    - ""\n'
)


class _TempWorkspace(unittest.TestCase):
    """每个用例一个临时工作区（`.ai/`、`cases/` 都是相对根，不污染本仓）。"""

    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_review_")
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def write_draft(self, name, text):
        os.makedirs(".ai/draft", exist_ok=True)
        path = ".ai/draft/" + name + ".yml"
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(text)
        return path

    def write_unknowns(self, name, kinds):
        """写一份 `.ai/draft/<name>.unknowns.json`（装配器在真实链路里产的那个文件）。"""
        payload = {
            "case": name,
            "count": len(kinds),
            "note": "测试夹具：只放 `kind`（复算只看它）",
            "items": [
                {"kind": kind, "level": "degrade", "where": "case", "message": f"{kind} 的原文"}
                for kind in kinds
            ],
        }
        path = ".ai/draft/" + name + ".unknowns.json"
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False)
        return path

    def snapshot(self):
        """工作区快照（路径 + 大小 + mtime）：判"只读"用。"""
        found = []
        for root, _dirs, files in os.walk("."):
            for name in files:
                full = os.path.join(root, name)
                stat = os.stat(full)
                found.append((os.path.relpath(full), stat.st_size, stat.st_mtime_ns))
        return sorted(found)


class TestPromotionGate(_TempWorkspace):
    """转正门（`promote_draft`）：带 `TODO_` 拒、没签名拒、填好了才放行。"""

    def test_todo_draft_is_refused(self):
        """★T24 的核心判据：**还没填值就转正**必须被拒（否则 CI 会因"没填"变红）。"""
        path = self.write_draft("with_todo", TODO_DRAFT)

        result = promote_draft(path, approver="zhangsan")

        self.assertFalse(result.ok())
        self.assertIn("TODO_", result.reason)
        self.assertFalse(os.path.isdir("cases"), "被拒了不该建 cases/")
        self.assertFalse(os.path.isdir(".ai/reviews"), "被拒了不该留痕")

    def test_missing_approver_is_refused(self):
        """留痕必须写清"谁确认的"——没签名的单子不算留痕。"""
        path = self.write_draft("filled", FILLED_DRAFT)

        result = promote_draft(path, approver="   ")

        self.assertFalse(result.ok())
        self.assertIn("approver", result.reason)
        self.assertEqual(sorted(os.listdir("cases")) if os.path.isdir("cases") else [], [])

    def test_filled_draft_is_promoted_with_a_record(self):
        """填好了 → 转进 `cases/` **且**留下确认记录（两条判据同时成立）。"""
        path = self.write_draft("filled", FILLED_DRAFT)

        result = promote_draft(path, approver="zhangsan", approved_at="2026-09-25T10:00:00")

        self.assertTrue(result.ok(), result.to_dict())
        self.assertEqual(result.yaml_path, "cases/filled.yml")
        self.assertEqual(result.review_path, ".ai/reviews/filled.json")
        self.assertTrue(os.path.exists("cases/filled.yml"))

        record = read_review_record("filled")
        self.assertIsNotNone(record)
        self.assertEqual(record.approver, "zhangsan")  # type: ignore[union-attr]
        self.assertEqual(record.approved_at, "2026-09-25T10:00:00")  # type: ignore[union-attr]
        self.assertEqual(record.draft_hash, draft_hash(FILLED_DRAFT))  # type: ignore[union-attr]

    def test_hash_mismatch_invalidates_the_record(self):
        """★元护栏：**确认之后又改一行** → 留痕必须失效（否则留痕是橡皮图章）。"""
        path = self.write_draft("filled", FILLED_DRAFT)
        promote_draft(path, approver="zhangsan")
        self.assertIsNone(
            require_review("filled", draft_path=path, draft_text=FILLED_DRAFT),
            "刚确认过就该放行",
        )

        changed = FILLED_DRAFT + "  # 确认之后又改了一行\n"
        blocking = require_review("filled", draft_path=path, draft_text=changed)

        self.assertIsNotNone(blocking, "改了内容却仍放行 = 留痕失效判据没生效")
        self.assertIn("又变了", blocking or "")

    def test_promotion_does_not_overwrite_an_existing_case(self):
        """`cases/` 里可能是人手写的用例——查重要硬失败，不静默覆盖。"""
        path = self.write_draft("filled", FILLED_DRAFT)
        os.makedirs("cases", exist_ok=True)
        with open("cases/filled.yml", "w", encoding="utf-8") as fp:
            fp.write("# 人手写的用例，别动我\n")

        result = promote_draft(path, approver="zhangsan")

        self.assertFalse(result.ok())
        with open("cases/filled.yml", encoding="utf-8") as fp:
            self.assertEqual(fp.read(), "# 人手写的用例，别动我\n", "人手写的用例被覆盖了")
        self.assertIsNone(read_review_record("filled"), "被拒了不该留痕")

    def test_promotion_is_atomic_and_leaves_no_tmp(self):
        """原子写：留痕目录里不该有 `.tmp` 残留（半个文件比没有更坏）。"""
        path = self.write_draft("filled", FILLED_DRAFT)
        promote_draft(path, approver="zhangsan")

        self.assertEqual(os.listdir(".ai/reviews"), ["filled.json"])


class TestReviewRecordWriter(_TempWorkspace):
    """留痕 writer 自身的纪律。"""

    def test_empty_approver_raises(self):
        """空 approver **直接抛**：不给调用方"先写后校验"的机会（留痕必须有签名）。"""
        with self.assertRaises(ValueError):
            write_review_record("x", draft_path=".ai/draft/x.yml", draft_text="t", approver="  ")

    def test_record_says_it_is_evidence(self):
        """留痕里写明"本文件是**证据**、刻意不被忽略"——把 T12/P-7 的分界写进文件本身。"""
        path = write_review_record(
            "x", draft_path=".ai/draft/x.yml", draft_text="t", approver="a", approved_at="t"
        )
        with open(path, encoding="utf-8") as fp:
            body = fp.read()

        self.assertIn("证据", body)
        self.assertIn("gitignore", body)
        self.assertEqual(path, review_path("x"))

    def test_crlf_and_lf_hash_the_same(self):
        """CRLF/LF 归一：Windows 检出（带 CR）不该让**所有**留痕整体失效。"""
        self.assertEqual(draft_hash("a\r\nb\n"), draft_hash("a\nb\n"))

    def test_list_drafts_is_empty_without_a_draft_dir(self):
        """没有 `.ai/draft/` → 空列表（不该抛，也不该造目录）。"""
        self.assertEqual(list_drafts(), [])
        self.assertFalse(os.path.isdir(".ai/draft"))


class TestReviewCli(_TempWorkspace):
    """`haify review` 的 CLI 契约。"""

    def test_list_shows_drafts_and_their_state(self):
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        self.write_draft("filled", FILLED_DRAFT)
        self.assertEqual(main(["review", "--list"]), EXIT_OK)

    def test_approve_accepts_the_case_name_shorthand(self):
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        self.write_draft("filled", FILLED_DRAFT)

        self.assertEqual(main(["review", "--approve", "filled", "--approver", "lisi"]), EXIT_OK)
        self.assertTrue(os.path.exists("cases/filled.yml"))
        record = read_review_record("filled")
        self.assertIsNotNone(record)
        self.assertEqual(record.approver, "lisi")  # type: ignore[union-attr]

    def test_approve_refuses_without_approver(self):
        from interfacetester_ai.cli import EXIT_CASE_FAILED, main  # noqa: PLC0415

        self.write_draft("filled", FILLED_DRAFT)

        self.assertEqual(main(["review", "--approve", "filled"]), EXIT_CASE_FAILED)
        self.assertFalse(os.path.isdir("cases"))

    def test_no_action_prints_usage(self):
        from interfacetester_ai.cli import EXIT_CONFIG, main  # noqa: PLC0415

        self.assertEqual(main(["review"]), EXIT_CONFIG)


class TestBlockersMustBeResolved(_TempWorkspace):
    """★WP1.2（§九 第一档 A）：**未处置的 blocker 不许转正**。

    这条补的是"人工审核这一步也会放过薄用例"：`TODO_` 只是 blocker 的**一种**，
    `protocol-only`（断言全是协议级）与 `unknown-critical`（引用/端点对不上）同样是——
    改造前它们**不进转正门**（实证：`cases/T1_2_认证方式.yml` 零留痕躺在正式用例目录里）。

    ★判据**成对**：既要挡住"没处置就转正"，也要证明"处置了就放行""干净草稿不用处置"。
    """

    def test_unresolved_blocker_refuses_promotion(self):
        path = self.write_draft("thin", FILLED_DRAFT)
        self.write_unknowns("thin", ("protocol-only",))

        result = promote_draft(path, approver="zhangsan")

        self.assertFalse(result.ok())
        self.assertIn("protocol-only", result.reason)
        self.assertIn("--resolve-blocker", result.reason, "拒绝理由要说清**怎么**处置")
        self.assertEqual(result.quality.status, "needs_review", "拒绝时要带上质量状态")
        self.assertFalse(os.path.isdir("cases"), "被拒了不该建 cases/")
        self.assertFalse(os.path.isdir(".ai/reviews"), "被拒了不该留痕")
        self.assertIsNone(read_review_record("thin"))

    def test_named_resolution_lets_it_through_and_is_recorded(self):
        path = self.write_draft("thin", FILLED_DRAFT)
        self.write_unknowns("thin", ("protocol-only",))

        result = promote_draft(
            path,
            approver="zhangsan",
            resolved_blockers=("protocol-only",),
            notes="看过 unknowns 了",
        )

        self.assertTrue(result.ok(), result.to_dict())
        self.assertTrue(os.path.exists("cases/thin.yml"))
        record = read_review_record("thin")
        self.assertIsNotNone(record)
        self.assertEqual(record.quality_status, "needs_review")  # type: ignore[union-attr]
        self.assertEqual(record.blockers, ("protocol-only",))  # type: ignore[union-attr]
        self.assertEqual(record.resolved_blockers, ("protocol-only",))  # type: ignore[union-attr]
        self.assertIn("看过 unknowns 了", record.notes)  # type: ignore[union-attr]

    def test_partial_resolution_still_refuses(self):
        """★"处置了**一部分**"不算处置：漏掉的那条必须仍然拦住（否则等于没门）。"""
        path = self.write_draft("thin", FILLED_DRAFT)
        self.write_unknowns("thin", ("protocol-only", "quote-miss"))

        result = promote_draft(path, approver="zhangsan", resolved_blockers=("protocol-only",))

        self.assertFalse(result.ok())
        self.assertIn(
            "unknown-critical", result.reason, "剩那条（`quote-miss` → `unknown-critical`）要点名"
        )
        self.assertFalse(os.path.isdir("cases"))

    def test_irrelevant_resolution_does_not_open_the_door(self):
        """★处置必须**指名到 blocker 码**：写一个不相干的词不算处置。"""
        path = self.write_draft("thin", FILLED_DRAFT)
        self.write_unknowns("thin", ("protocol-only",))

        result = promote_draft(path, approver="zhangsan", resolved_blockers=("我觉得没事",))

        self.assertFalse(result.ok(), "随便写个词就放行 = 闸门退化成橡皮图章")
        self.assertFalse(os.path.isdir("cases"))

    def test_notes_alone_never_substitute_for_resolution(self):
        """★WP1.2 原文口径：**自由文本备注不能单独视作 blocker 已解决**。"""
        path = self.write_draft("thin", FILLED_DRAFT)
        self.write_unknowns("thin", ("protocol-only",))

        result = promote_draft(
            path, approver="zhangsan", notes="我逐条看过了，没问题（但没有点名 blocker 码）"
        )

        self.assertFalse(result.ok(), "只写备注就放行，等于把闸门交给一句话")

    def test_clean_draft_needs_no_resolution(self):
        """★成对判据：干净草稿（没有 unknowns 文件）**不需要**任何处置，照常转正。"""
        path = self.write_draft("filled", FILLED_DRAFT)

        result = promote_draft(path, approver="zhangsan")

        self.assertTrue(result.ok(), result.to_dict())
        self.assertEqual(result.quality.status, "static_valid")

    def test_quality_of_draft_is_read_only(self):
        """★复算入口**只读**（不新增写盘点）：调用前后工作区逐字节不变。"""
        self.write_draft("thin", FILLED_DRAFT)
        self.write_unknowns("thin", ("protocol-only",))
        before = self.snapshot()

        quality = quality_of_draft("thin", FILLED_DRAFT)

        self.assertEqual(quality.status, "needs_review")
        self.assertEqual(self.snapshot(), before, "复算竟然动了文件")

    def test_gate_rejection_is_a_hard_conclusion(self):
        """★闸门结论**不能被 `--resolve-blocker` 覆盖**（它管的是"看起来通过、其实什么都没测"）。

        ★这条是**端到端实测暴露出来的**：改造后 `review --list` 一开始把一份被 S 闸门拒掉的
        草稿显示成 `static_valid`（因为"复算"当时只看 `unknowns`）——审核者会对着一份错状态签字。
        修法是"传 `draft_path` 就**重跑闸门**，且闸门优先于内容级判定"（见 `quality_of_draft`）。
        """
        path = self.write_draft("trap", GATE_TRAP_DRAFT)

        refused = promote_draft(path, approver="zhangsan")
        self.assertFalse(refused.ok())
        self.assertEqual(refused.quality.status, "rejected")
        self.assertIn("闸门", refused.reason)
        self.assertFalse(os.path.isdir("cases"))

        overridden = promote_draft(
            path, approver="zhangsan", resolved_blockers=("structure-rejected",)
        )
        self.assertFalse(overridden.ok(), "点名 `structure-rejected` 就把闸门点掉了？")
        self.assertIn("不能用 `--resolve-blocker` 覆盖", overridden.reason)
        self.assertFalse(os.path.isdir("cases"))
        self.assertIsNone(read_review_record("trap"))

    def test_gate_check_reports_the_codes(self):
        """`gate_check` 对当前这一版重跑闸门：好的通过、坏的给编号（口径从闸门自己的 `to_dict()` 取）。"""
        from interfacetester_ai.reviews import gate_check  # noqa: PLC0415

        good = self.write_draft("filled", FILLED_DRAFT)
        bad = self.write_draft("trap", GATE_TRAP_DRAFT)

        passed, codes = gate_check(good)
        self.assertTrue(passed, f"干净草稿没通过闸门：{codes}")
        self.assertEqual(codes, ())

        passed, codes = gate_check(bad)
        self.assertFalse(passed, "S1 的 MUST_REJECT 形态竟然过了闸门")
        self.assertTrue(codes, "拒了却没说编号 —— 用户不知道去看哪一条")


class TestReviewSurfacesQualityToHumans(_TempWorkspace):
    """★WP1.2 的另一半：**人要看得到**（清单 + 转正前摘要）。

    只把闸门加严、却不把状态显示出来，人会遇到"明明点了同意却失败、不知道为什么"——
    那就从"放过了"变成"卡住了"，仍然没解决"审核看不到质量"。
    """

    def _review(self, argv):
        import contextlib  # noqa: PLC0415
        import io  # noqa: PLC0415

        from interfacetester_ai.cli import main  # noqa: PLC0415

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["review"] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_list_shows_quality_status_and_blockers(self):
        self.write_draft("thin", FILLED_DRAFT)
        self.write_unknowns("thin", ("protocol-only",))

        code, out, _err = self._review(["--list"])

        self.assertEqual(code, 0)
        self.assertIn("needs_review", out, "清单里看不到质量状态，那它只是「待确认」三个字")
        self.assertIn("protocol-only", out)
        self.assertIn("--resolve-blocker", out)

    def test_approve_prints_the_brief_and_names_what_is_missing(self):
        self.write_draft("thin", FILLED_DRAFT)
        self.write_unknowns("thin", ("protocol-only",))

        code, out, err = self._review(["--approve", "thin", "--approver", "zhangsan"])

        self.assertEqual(code, 1)
        self.assertIn("质量状态", out, "转正前必须先摊开状态")
        self.assertIn("未处置", out)
        self.assertIn("protocol-only", out + err)

    def test_list_shows_rejected_for_a_gate_rejected_draft(self):
        """★清单里必须显示 `rejected`（这正是本轮实测暴露的缺陷：闸门拒的草稿显示成 static_valid）。"""
        self.write_draft("trap", GATE_TRAP_DRAFT)

        code, out, _err = self._review(["--list"])

        self.assertEqual(code, 0)
        self.assertIn("rejected", out, "闸门拒掉的草稿在清单里被显示成了「通过」")
        self.assertIn("structure-rejected", out)

    def test_approve_refuses_a_gate_rejected_draft(self):
        self.write_draft("trap", GATE_TRAP_DRAFT)

        code, out, err = self._review(["--approve", "trap", "--approver", "zhangsan"])

        self.assertEqual(code, 1)
        self.assertIn("闸门", out + err)
        self.assertFalse(os.path.isdir("cases"))

    def test_approve_with_resolution_succeeds_end_to_end(self):
        self.write_draft("thin", FILLED_DRAFT)
        self.write_unknowns("thin", ("protocol-only",))

        code, out, _err = self._review(
            [
                "--approve",
                "thin",
                "--approver",
                "zhangsan",
                "--resolve-blocker",
                "protocol-only",
            ]
        )

        self.assertEqual(code, 0, out)
        self.assertIn("已转正", out)
        self.assertIn("审核时质量状态", out)
        self.assertTrue(os.path.exists("cases/thin.yml"))
        record = read_review_record("thin")
        self.assertIsNotNone(record)
        self.assertEqual(record.resolved_blockers, ("protocol-only",))  # type: ignore[union-attr]


class TestDraftsOverview(_TempWorkspace):
    """★C（2026-09-26）：**三处共用**的只读投影（CLI / 只读看板 / 离线导出）。

    为什么要单独立判据：三处各自复算质量状态迟早不一致，**而不一致的看板等于没有看板**
    （与 `audit.py`"刻意不是第二份日志"同源）。所以口径必须只有一份，并且它是**只读**的。
    """

    def test_rows_carry_quality_review_state_and_blockers(self):
        self.write_draft("thin", FILLED_DRAFT)
        self.write_unknowns("thin", ("protocol-only",))

        rows = drafts_overview()

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["case"], "thin")
        self.assertEqual(row["quality_status"], "needs_review")
        self.assertIn("protocol-only", row["blockers"])
        self.assertFalse(row["reviewed"], "还没确认过，`reviewed` 不该为真")
        self.assertTrue(row["review_blocking"], "未确认时必须带上原因")
        self.assertEqual(row["gate"], "probed")
        self.assertEqual(row["error"], "")

    def test_with_gate_false_is_explicitly_not_probed(self):
        """★**不猜**：没跑闸门就标 `not_probed`，不许让它看起来像跑过。"""
        self.write_draft("trap", GATE_TRAP_DRAFT)

        probed = drafts_overview()[0]
        skipped = drafts_overview(with_gate=False)[0]

        self.assertEqual(probed["quality_status"], "rejected")
        self.assertEqual(probed["gate"], "probed")
        self.assertEqual(skipped["gate"], "not_probed")
        self.assertNotEqual(
            skipped["quality_status"], "rejected", "没跑闸门却给 `rejected` = 替它编结论"
        )

    def test_gate_rejection_never_shows_as_passing(self):
        """★端到端实测暴露过的状态错：闸门拒掉的草稿**必须**是 `rejected`。"""
        self.write_draft("trap", GATE_TRAP_DRAFT)

        row = drafts_overview()[0]

        self.assertEqual(row["quality_status"], "rejected")
        self.assertIn("structure-rejected", row["blockers"])
        self.assertIn("rejected", row["quality_render"])

    def test_overview_is_read_only(self):
        self.write_draft("thin", FILLED_DRAFT)
        self.write_unknowns("thin", ("protocol-only",))
        before = self.snapshot()

        drafts_overview()

        self.assertEqual(self.snapshot(), before, "只读投影竟然动了文件")

    def test_unreadable_draft_is_listed_with_the_reason(self):
        """读不到**不跳过**：跳过等于"这份不存在"，而事实是"它存在但读不了"。"""
        os.makedirs(".ai/draft/broken.yml", exist_ok=True)

        rows = drafts_overview()

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["error"], "读不到必须带原因（不跳过、不静默）")


class TestWriteBoundaryAndSelftest(unittest.TestCase):
    """写盘边界（T18）与自检本身。"""

    def test_reviews_is_a_registered_writer(self):
        """T18 注册表：`reviews` 必须在册——**不在册的写盘一律违规**（白名单默认禁止）。"""
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        self.assertIn("reviews", MODULE_WRITE_BOUNDARIES)
        rule, roots = MODULE_WRITE_BOUNDARIES["reviews"]
        self.assertEqual(rule, "roots")
        self.assertEqual(roots, (".ai/",), "留痕根固定 `.ai/`（`.ai/reviews/` 是其子目录）")

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_hash_is_constant(self):
        """元护栏：哈希退化成常量（或只取长度）→ 自检必红。"""
        with mock.patch.object(reviews_module, "draft_hash", lambda text: "same"):
            self.assertNotEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_the_gate_always_passes(self):
        """元护栏：门被改成**恒放行** → 自检必红（门形同虚设是最坏的一种"通过"）。"""
        with mock.patch.object(reviews_module, "require_review", lambda *a, **k: None):
            self.assertNotEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_blocker_gate_is_neutered(self):
        """元护栏：把"处置判据"改成**恒放行** → 自检必红（WP1.2 的门不许形同虚设）。"""
        with mock.patch.object(
            reviews_module, "missing_blockers", lambda actual, resolved: ()
        ):
            self.assertNotEqual(run_selftest(), 0)


if __name__ == "__main__":
    unittest.main()
