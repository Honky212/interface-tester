# -*- coding: utf-8 -*-
"""`interfacetester_ai/quality.py` 的判据（决策清单 §九 第一档①②；评审 §五）。

## 为什么需要这个文件（而不是只靠模块自检）

模块自检（`python -m interfacetester_ai.quality`）回答的是"**我自己的规则我守住了吗**"；
本文件回答的是另一类问题：**这些规则会不会被"改着改着"悄悄放松**。

具体防三件事：

1. **状态机被越权使用**——比如有人为了"让 CI 变绿"，把 `rejected` 直接改成 `static_valid`
   或让 `generated` 跳到 `runtime_verified`。判据是**白名单**，不是"看起来合理就行"。
2. **口径回归**——最危险的一条是"**没有运行证据却出现 `runtime_verified`**"：
   它会把"L2 通过"重新说成"跑过了"（评审 §二 指出的口径混淆）。
   这里用**元护栏**钉住：遍历所有 `runtime=None`/`executed=False` 的组合，逐个断言**绝不出现**该状态。
3. **模块被写脏**——本模块是**纯函数**：一旦有人往里加 `open(..., "w")` 或 `os.makedirs`，
   就必须同步 `bench/write_boundary_scanner.py` 的注册表。这里用 AST 自检挡住"悄悄写盘"，
   与 `tests/web_readonly_test.py` 的"零执行"判据同源。
"""

from __future__ import annotations

import ast
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.quality import (  # noqa: E402
    ALL_STATUSES,
    BLOCKER_MUTATION_NOT_PASSED,
    BLOCKER_NO_RUNTIME_EVIDENCE,
    BLOCKER_PROTOCOL_ONLY,
    BLOCKER_STRUCTURE,
    BLOCKER_TODO,
    BLOCKER_UNKNOWN_CRITICAL,
    WARNING_MERGE_NOTE,
    QualityAssessment,
    QualityStatus,
    RuntimeEvidence,
    assess,
    is_allowed_transition,
    promotion_gaps,
)

MODULE_PATH = os.path.join(BASE, "interfacetester_ai", "quality.py")


class _Unknown:
    """最小 `Unknown` 替身（本模块只读 `.kind`）。"""

    def __init__(self, kind):
        self.kind = kind


class TestStatusMachine(unittest.TestCase):
    """状态转换是**白名单**：非法转换必须被拒，未知状态名一律拒。"""

    def test_illegal_transitions_are_refused(self):
        for current, target in (
            (QualityStatus.REJECTED, QualityStatus.STATIC_VALID),
            (QualityStatus.REJECTED, QualityStatus.NEEDS_REVIEW),
            (QualityStatus.GENERATED, QualityStatus.RUNTIME_VERIFIED),
            (QualityStatus.GENERATED, QualityStatus.AUTO_PROMOTABLE),
            (QualityStatus.STATIC_VALID, QualityStatus.AUTO_PROMOTABLE),
        ):
            with self.subTest(current=current, target=target):
                self.assertFalse(
                    is_allowed_transition(current, target),
                    f"{current} → {target} 不该被允许（跳过'待人工'直接晋级是本轮要防的形态）",
                )

    def test_legal_transitions_are_allowed(self):
        for current, target in (
            (QualityStatus.GENERATED, QualityStatus.STATIC_VALID),
            (QualityStatus.STATIC_VALID, QualityStatus.RUNTIME_VERIFIED),
            (QualityStatus.NEEDS_REVIEW, QualityStatus.STATIC_VALID),
            (QualityStatus.RUNTIME_VERIFIED, QualityStatus.AUTO_PROMOTABLE),
            (QualityStatus.AUTO_PROMOTABLE, QualityStatus.REJECTED),
        ):
            with self.subTest(current=current, target=target):
                self.assertTrue(is_allowed_transition(current, target))

    def test_unknown_status_names_are_refused(self):
        """★宁严不松：编出来的状态名不许被当成合法目标。"""
        self.assertFalse(is_allowed_transition(QualityStatus.STATIC_VALID, "usable"))
        self.assertFalse(is_allowed_transition("whatever", QualityStatus.STATIC_VALID))

    def test_rejected_is_terminal(self):
        self.assertEqual(
            [t for t in ALL_STATUSES if is_allowed_transition(QualityStatus.REJECTED, t)],
            [],
            "`rejected` 必须是终态——拒了的东西不许靠改标签复活，要重跑生成",
        )


class TestAssessFromAssemblyFacts(unittest.TestCase):
    """由装配事实（拒 / unknowns / TODO）算状态。"""

    def test_rejected_gate_returns_rejected_with_stable_code(self):
        result = assess(rejected=True, gate_codes=("S4", "S1"))
        self.assertEqual(result.status, QualityStatus.REJECTED)
        self.assertIn(BLOCKER_STRUCTURE, result.blockers)
        self.assertFalse(result.ok())
        self.assertFalse(result.is_usable())
        self.assertIn("S4", result.reasons[0], "拒绝原因要能点到闸门编号")

    def test_protocol_only_is_needs_review(self):
        """★本轮的核心改动：仅协议断言**不再是**"可以落 cases/"的产物。"""
        result = assess(unknowns=[_Unknown("protocol-only")])
        self.assertEqual(result.status, QualityStatus.NEEDS_REVIEW)
        self.assertIn(BLOCKER_PROTOCOL_ONLY, result.blockers)
        self.assertTrue(any("测不出业务错" in item for item in result.reasons))

    def test_merge_skip_is_needs_review(self):
        """"模型零断言、合并不救它"是**质量信号**，不该被当成可用（C-2 第四条口径）。"""
        result = assess(unknowns=[_Unknown("merge-skip")])
        self.assertEqual(result.status, QualityStatus.NEEDS_REVIEW)
        self.assertIn(BLOCKER_UNKNOWN_CRITICAL, result.blockers)

    def test_unresolved_degrade_is_needs_review(self):
        """未决降级项（引用/端点对不上）→ 待人工（`quote-miss` 是代表形态）。"""
        for kind in ("quote-miss", "endpoint-not-in-doc", "condition-unmodeled"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    assess(unknowns=[_Unknown(kind)]).status, QualityStatus.NEEDS_REVIEW
                )

    def test_merge_note_is_warning_not_blocker(self):
        """合并过程说明**不拦**，但必须**可见**（不许静默）。"""
        result = assess(unknowns=[_Unknown("merge-note")])
        self.assertEqual(result.status, QualityStatus.STATIC_VALID)
        self.assertIn(WARNING_MERGE_NOTE, result.warnings)

    def test_todo_is_needs_review(self):
        result = assess(todo_items=[{"case": "x"}, {"case": "y"}])
        self.assertEqual(result.status, QualityStatus.NEEDS_REVIEW)
        self.assertIn(BLOCKER_TODO, result.blockers)
        self.assertIn("2 条", "".join(result.reasons))

    def test_clean_draft_is_static_valid_but_not_usable(self):
        result = assess()
        self.assertEqual(result.status, QualityStatus.STATIC_VALID)
        self.assertTrue(result.ok(), "干净草稿的 `ok()` 必须为真（否则状态失去区分度）")
        self.assertFalse(
            result.is_usable(), "★静态通过只是'框架接受'，不等于'可直接使用'"
        )
        self.assertEqual(promotion_gaps(result), (BLOCKER_NO_RUNTIME_EVIDENCE,))
class TestNoRuntimeEvidenceIsAGate(unittest.TestCase):
    """★**元护栏**：没有执行记录时，任何组合都**不许**出现 `runtime_verified`/`auto_promotable`。

    防的不是某个分支写错，而是"**将来有人加一条捷径**"——所以这里**穷举**参数组合，
    逐格断言（"没跑"绝不能被说成"跑过了"）。
    """

    def test_all_runtime_combinations_behave_exactly_as_specified(self):
        for executed in (True, False):
            for passed in (True, False):
                for stable in (True, False):
                    for killed, total in ((0, 0), (1, 10), (9, 10), (20, 20)):
                        with self.subTest(
                            executed=executed,
                            passed=passed,
                            stable=stable,
                            killed=killed,
                            total=total,
                        ):
                            status = assess(
                                runtime=RuntimeEvidence(
                                    executed=executed,
                                    passed=passed,
                                    repeated_stable=stable,
                                    mutation_killed=killed,
                                    mutation_total=total,
                                )
                            ).status
                            if not (executed and passed):
                                self.assertNotIn(
                                    status,
                                    (QualityStatus.RUNTIME_VERIFIED, QualityStatus.AUTO_PROMOTABLE),
                                    "★没有执行记录/未通过，却拿到了运行态（'没跑'被说成'跑过了'）",
                                )
                                continue
                            eligible = stable and total > 0 and (killed / total) >= 0.9
                            self.assertEqual(
                                status,
                                QualityStatus.AUTO_PROMOTABLE
                                if eligible
                                else QualityStatus.RUNTIME_VERIFIED,
                                "门槛判定与'重复稳定 + 变异达标'不一致",
                            )

    def test_mutation_denominator_zero_is_not_a_pass(self):
        """分母为 0 ≠ 100%：没有任何变异样本时**不许**晋级。"""
        result = assess(runtime=RuntimeEvidence(executed=True, passed=True, repeated_stable=True))
        self.assertEqual(result.status, QualityStatus.RUNTIME_VERIFIED)
        self.assertIsNone(RuntimeEvidence().mutation_rate())

    def test_unstable_repeat_blocks_promotion(self):
        result = assess(
            runtime=RuntimeEvidence(
                executed=True, passed=True, repeated_stable=False, mutation_killed=20, mutation_total=20
            )
        )
        self.assertEqual(result.status, QualityStatus.RUNTIME_VERIFIED)
        self.assertFalse(result.is_usable())

    def test_low_mutation_rate_blocks_promotion_with_stable_code(self):
        result = assess(
            runtime=RuntimeEvidence(
                executed=True, passed=True, repeated_stable=True, mutation_killed=1, mutation_total=10
            )
        )
        self.assertEqual(result.status, QualityStatus.RUNTIME_VERIFIED)
        self.assertIn(BLOCKER_MUTATION_NOT_PASSED, result.blockers)

    def test_all_gates_passed_gives_eligibility_only(self):
        result = assess(
            runtime=RuntimeEvidence(
                executed=True, passed=True, repeated_stable=True, mutation_killed=19, mutation_total=20
            )
        )
        self.assertEqual(result.status, QualityStatus.AUTO_PROMOTABLE)
        self.assertTrue(result.is_auto_promotable())
        self.assertIn("资格", "".join(result.reasons), "★要写明它只是**资格**，转正仍是独立动作")

    def test_executed_but_failed_is_not_usable(self):
        result = assess(runtime=RuntimeEvidence(executed=True, passed=False))
        self.assertFalse(result.is_usable())
        self.assertEqual(result.status, QualityStatus.STATIC_VALID)

    def test_blockers_outrank_runtime_evidence(self):
        """跑通了但断言全是协议级 → 仍是待人工（"能跑"≠"测得有意义"）。"""
        result = assess(
            unknowns=[_Unknown("protocol-only")],
            runtime=RuntimeEvidence(
                executed=True, passed=True, repeated_stable=True, mutation_killed=20, mutation_total=20
            ),
        )
        self.assertEqual(result.status, QualityStatus.NEEDS_REVIEW)
        self.assertFalse(result.is_usable())
class TestAssessmentShape(unittest.TestCase):
    """`to_dict()` 的键集是**公开面**（CLI / 报告 / 留痕都吃它），不许悄悄改。"""

    def test_serialization_keys_are_pinned(self):
        payload = QualityAssessment().to_dict()
        self.assertEqual(
            sorted(payload),
            ["blockers", "reasons", "status", "usable_without_review", "warnings"],
        )
        self.assertFalse(payload["usable_without_review"], "默认态不许自称可用")

    def test_render_is_one_line(self):
        rendered = assess(unknowns=[_Unknown("protocol-only")]).render()
        self.assertEqual(len(rendered.splitlines()), 1)
        self.assertIn(QualityStatus.NEEDS_REVIEW, rendered)
        self.assertIn(BLOCKER_PROTOCOL_ONLY, rendered)


class TestModuleStaysPure(unittest.TestCase):
    """★元护栏：本模块是**纯函数**——不许悄悄长出写盘 / 子进程 / 网络。

    与 `tests/web_readonly_test.py` 的"零执行"判据同源：**判据查的是代码形态**，
    不是"我说它不写盘"。真需要写盘时，正路是**登记**到
    `bench/write_boundary_scanner.py` 的注册表——那一步要签字。
    """

    _FORBIDDEN_CALLS = {
        "open",
        "makedirs",
        "mkdir",
        "remove",
        "unlink",
        "rmtree",
        "rename",
        "replace",
        "write_text",
        "write_bytes",
        "system",
        "run",
        "Popen",
        "check_output",
        "urlopen",
    }
    _FORBIDDEN_IMPORTS = {"subprocess", "socket", "shutil", "pathlib", "requests", "urllib"}

    def _tree(self):
        with open(MODULE_PATH, encoding="utf-8") as fp:
            return ast.parse(fp.read())

    def test_module_has_no_write_or_subprocess_calls(self):
        offenders = []
        for node in ast.walk(self._tree()):
            if not isinstance(node, ast.Call):
                continue
            target = ""
            if isinstance(node.func, ast.Name):
                target = node.func.id
            elif isinstance(node.func, ast.Attribute):
                target = node.func.attr
            if target in self._FORBIDDEN_CALLS:
                offenders.append(f"第 {node.lineno} 行调用了 {target}()")
        self.assertEqual(
            offenders,
            [],
            "纯函数模块里出现了写盘/子进程/网络调用：\n  " + "\n  ".join(offenders),
        )

    def test_module_does_not_import_io_libraries(self):
        offenders = []
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in self._FORBIDDEN_IMPORTS:
                        offenders.append(f"第 {node.lineno} 行 import {alias.name}")
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in self._FORBIDDEN_IMPORTS:
                    offenders.append(f"第 {node.lineno} 行 from {node.module} import …")
        self.assertEqual(offenders, [], "纯函数模块 import 了 IO 库：\n  " + "\n  ".join(offenders))

    def test_module_selftest_exits_zero(self):
        """自检本身要能跑通（CLI 形态，与 `gates` / `doc_quality` 同款）。"""
        import subprocess

        proc = subprocess.run(
            [sys.executable, "-m", "interfacetester_ai.quality"],
            cwd=BASE,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
