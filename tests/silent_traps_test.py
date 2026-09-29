# -*- coding: utf-8 -*-
"""静默陷阱闸门的护栏用例。

## 为什么需要这个文件

`bench/silent_traps.py` 把「已知会静默假通过的形态」做成了生成期闸门。
但**闸门本身也会坏**，而且坏法有两种，方向相反，必须同时防：

1. **漏拦**（false negative）：闸门失效 → 带已知缺陷的用例照常落盘 → 假通过回归。
2. **误伤**（false positive）：闸门过严 → 合法写法被拦 → 用户整体关掉闸门 → 等于没有。
   本仓对这一点有明确口径（`comparators.py`）：
   「只拦能精确判定的形态」，**假报会让真报被无视**。

所以本文件的判据是**成对**的：MUST_REJECT 与 MUST_ALLOW 必须同时成立。
只测前者会得到一道"宁可错杀"的闸门，那种闸门在真实项目里活不过一周。

## 判据自检（防"假绿"）

`test_gate_actually_detects_injected_fault` 与 `test_allow_set_would_catch_overblocking`
是本文件的**元护栏**：它们验证"这套断言真的在检查东西"。
没有它们，上面那些"全绿"可能只是因为闸门压根没被调用（本仓把这类叫"护栏打偏"，
见 `docs/架构与调用链.md` §7 的自纠记录）。

## 与内核的关系

闸门的 S6/S8/S9 是**复用**内核护栏，因此本文件也顺带钉住了
「内核护栏仍可被调用、签名未漂移」。内核升级（5.x）后若这些用例变红，
说明需要重新对账 —— 与 R10 风险项同一处置。
"""

import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from bench.silent_traps import (  # noqa: E402
    MUST_ALLOW,
    MUST_REJECT,
    PENDING,
    REJECT,
    GateReport,
    SilentTrapRejected,
    assert_testcase_passes,
    check_testcase,
    gate_s1_pseudo_presence,
    gate_s2_lexicographic,
    gate_s3_assertion_vacuum,
    gate_s4_extract_without_assertion,
    gate_s5_quoted_function,
    gate_s6_unknown_comparators,
    gate_s7_type_match_expect,
    gate_s8_inline_schema,
    gate_s9_generatable_and_body,
    gate_s10_module_collision,
    gate_s11_document_encoding,
    kernel_comparator_names,
)


class TestGatesRejectKnownSilentTraps(unittest.TestCase):
    """闸门必须拦下每一条已知的静默陷阱（漏拦 = 假通过回归）。"""

    def test_every_must_reject_sample_is_blocked(self):
        for code, label, case in MUST_REJECT:
            with self.subTest(code=code, label=label):
                report = check_testcase(case, path=f"<test:{label}>")
                self.assertFalse(
                    report.ok,
                    f"闸门**漏拦**了已知陷阱：{code} {label}\n"
                    f"  这是最危险的方向——带缺陷的用例会照常落盘并假通过。",
                )
                self.assertIn(
                    code,
                    report.codes(),
                    f"{label} 虽然被拦下，但编号是 {report.codes()}，期望含 {code}："
                    f"编号错了会让「按编号统计闸门命中」失真。",
                )

    def test_assert_version_raises_with_report(self):
        """抛异常版本必须带完整报告（供 gen/fix 流程给出可操作报错）。"""
        case = {
            "config": {"name": "t", "base_url": "http://127.0.0.1:1"},
            "teststeps": [
                {
                    "name": "s",
                    "request": {"method": "GET", "url": "/a"},
                    "validate": [{"not_equal": ["body.token", ""]}],
                }
            ],
        }
        with self.assertRaises(SilentTrapRejected) as ctx:
            assert_testcase_passes(case, path="<test>")
        self.assertIn("S1", ctx.exception.report.codes())
        # 报错必须**可操作**：要含改法，否则用户无法自修
        self.assertIn("type_match", str(ctx.exception))

    def test_reject_count_is_meaningful(self):
        """健康检查：对抗样本集不能为空（否则下面的"全绿"毫无意义）。"""
        self.assertGreaterEqual(len(MUST_REJECT), 10)
        self.assertGreaterEqual(len(MUST_ALLOW), 10)


class TestGatesDoNotOverblock(unittest.TestCase):
    """闸门必须放行内核明示的合法写法（误伤比漏拦更致命）。"""

    def test_every_must_allow_sample_passes(self):
        for code, label, case in MUST_ALLOW:
            with self.subTest(code=code, label=label):
                report = check_testcase(case, path=f"<test:{label}>")
                detail = "\n".join(
                    f"    {f.code} {f.message.splitlines()[0][:100]}" for f in report.rejects
                )
                self.assertTrue(
                    report.ok,
                    f"闸门**误伤**了合法写法：{code} {label}\n{detail}\n"
                    f"  误伤会让用户整体关掉闸门（本仓口径：假报让真报被无视）。",
                )


class TestGateMetaGuardrails(unittest.TestCase):
    """元护栏：证明"这套断言真的在检查东西"（防护栏打偏 / 假绿）。"""

    def test_gate_actually_detects_injected_fault(self):
        """注入一个已知陷阱，闸门必须变红——否则本文件其余断言都不可信。"""
        clean = {
            "config": {"name": "t", "base_url": "http://127.0.0.1:1"},
            "teststeps": [
                {
                    "name": "s",
                    "request": {"method": "GET", "url": "/a"},
                    "validate": [{"eq": ["status_code", 200]}],
                }
            ],
        }
        import copy

        self.assertTrue(
            check_testcase(copy.deepcopy(clean), path="<meta>").ok,
            "干净的对照样本本身就没通过 —— 下面注入的结论不可信",
        )
        injected = copy.deepcopy(clean)
        injected["teststeps"][0]["validate"] = [{"not_equal": ["body.token", ""]}]
        self.assertFalse(
            check_testcase(injected, path="<meta>").ok,
            "注入伪存在性断言后闸门**没有**变红 —— 闸门是假绿的",
        )

    def test_allow_set_would_catch_overblocking(self):
        """反向自检：故意构造一个"过严闸门"应拦的样本，确认 ALLOW 集非空且有效。"""
        self.assertTrue(MUST_ALLOW, "ALLOW 集为空 → 无法检出误伤")
        # `eq: [body.x, null]` 是内核明示放过的最关键反例
        null_case = {
            "config": {"name": "t", "base_url": "http://127.0.0.1:1"},
            "teststeps": [
                {
                    "name": "s",
                    "request": {"method": "GET", "url": "/a"},
                    "validate": [{"eq": ["body.x", None]}],
                }
            ],
        }
        self.assertTrue(
            check_testcase(null_case, path="<meta>").ok,
            "`eq: [body.x, null]`（明确期望 null）被误伤 —— "
            "这是内核 `_assertion_expects_null` 刻意放过的合法写法",
        )

    def test_kernel_guardrails_are_still_callable(self):
        """S6/S8/S9 复用内核护栏：钉住签名未漂移（内核升级后本用例会先红）。"""
        names = kernel_comparator_names()
        self.assertIn("equal", names)
        self.assertIn("type_match", names)
        self.assertGreaterEqual(len(names), 20, f"内置算子数异常：{len(names)}")


class TestGateBranchAccounting(unittest.TestCase):
    """★T20（方案 v8 §3.5 第三道元护栏）：S 闸门三方对账。

    判据（§3.5 / §十 T20）：**判据表（§3.3）↔ 代码分支 ↔ 红线样本**三方一一
    对应，任何一方漂移本类都会红。落地口径：

    - **Finding 分支数** = 闸门函数里 `report.findings.append` 的调用点数
      （静态可数；S0 是 `check_testcase` 的前置防御分支）；
    - **REJECT 分支**由 `MUST_REJECT` 钉住：运行全部样本后，该闸门累计的
      REJECT finding 数必须 ≥ 其 REJECT 分支数（样本被删/失效即红）；
    - **PENDING 分支**（S3 之②、S4）与**结构性进不了 MUST_REJECT 的维度**
      （S0 前置防御 / S10 批量输入 / S11 文件路径输入）由
      `test_structural_exception_branches_are_pinned` 的行为级探针逐分支钉住。

    v7 曾把这道元护栏写成「已完成」，实测本文件并无此用例（§0.5-②、§12.8-②）；
    v8 于 2026-09-23 补齐（T20 落地）。
    """

    # 文档侧登记：闸门编号 → Finding 分支数，与 §3.3 判据表一一对应。
    # 判据增删时必须同步改这张表（对账红即是提醒，不要删断言消音）。
    GATE_FINDING_BRANCHES = {
        "S0": 1,  # check_testcase 前置防御：用例不是字典
        "S1": 3,  # not_equal 空值 / contains 空串 / jsonschema 空 schema
        "S2": 1,  # 数值算子期望字符串数字
        "S3": 3,  # ①a 步骤级 REJECT / ② 显式空 PENDING / ③ 整例级 REJECT
        "S4": 1,  # PENDING 单分支（「被使用」a/b/c 是放行分支，不产 Finding）
        "S5": 1,
        "S6": 1,
        "S7": 1,
        "S8": 1,
        "S9": 1,
        "S10": 1,
        "S11": 3,  # 文档不存在 / 0 字节 / 编码不可识别
    }
    # 其中产生 PENDING（而非 REJECT）的分支数：走行为级探针，不进 REJECT 对账。
    GATE_PENDING_BRANCHES = {"S3": 1, "S4": 1}
    # 结构性进不了 MUST_REJECT 的编号（输入形态不是单个用例字典）。
    STRUCTURAL_EXCEPTION_CODES = ("S0", "S10", "S11")

    GATE_FUNCTIONS = {
        "S0": check_testcase,
        "S1": gate_s1_pseudo_presence,
        "S2": gate_s2_lexicographic,
        "S3": gate_s3_assertion_vacuum,
        "S4": gate_s4_extract_without_assertion,
        "S5": gate_s5_quoted_function,
        "S6": gate_s6_unknown_comparators,
        "S7": gate_s7_type_match_expect,
        "S8": gate_s8_inline_schema,
        "S9": gate_s9_generatable_and_body,
        "S10": gate_s10_module_collision,
        "S11": gate_s11_document_encoding,
    }

    def test_documented_branch_counts_match_code(self):
        """对账第一边：§3.3 登记的分支数 == 代码里的 Finding 调用点数。"""
        import inspect

        for code, func in self.GATE_FUNCTIONS.items():
            with self.subTest(code=code):
                actual = inspect.getsource(func).count("report.findings.append")
                self.assertEqual(
                    self.GATE_FINDING_BRANCHES[code],
                    actual,
                    f"{code} 的 Finding 分支数漂移：登记 "
                    f"{self.GATE_FINDING_BRANCHES[code]}，代码实际 {actual}。\n"
                    f"  若判据确有增删，请同步改 GATE_FINDING_BRANCHES 与 §3.3 判据表。",
                )

    def test_must_reject_codes_are_known_gates(self):
        """对账第二边：MUST_REJECT 的编号必须都能指认到登记表里的闸门。"""
        unknown = sorted(
            {code for code, _, _ in MUST_REJECT} - set(self.GATE_FINDING_BRANCHES)
        )
        self.assertEqual(
            unknown,
            [],
            f"MUST_REJECT 出现登记表之外的编号 {unknown}——样本编号漂移，"
            f"「按编号统计闸门命中」会失真。",
        )

    def test_every_gate_branch_has_a_must_reject_sample(self):
        """对账第三边（REJECT 方向）：每个 REJECT 分支都有红线样本覆盖。

        口径：运行全部 MUST_REJECT 样本，该闸门累计的 REJECT finding 数
        ≥ 其 REJECT 分支数（= 登记分支数 − PENDING 分支数）。
        结构性例外维度（S0/S10/S11）与 PENDING 分支由下一个用例钉住。
        """
        hit = {}
        for code, label, case in MUST_REJECT:
            report = check_testcase(case, path=f"<t20:{label}>")
            for f in report.rejects:
                hit[f.code] = hit.get(f.code, 0) + 1
        for code, branches in self.GATE_FINDING_BRANCHES.items():
            if code in self.STRUCTURAL_EXCEPTION_CODES or code in self.GATE_PENDING_BRANCHES:
                continue
            expected = branches - self.GATE_PENDING_BRANCHES.get(code, 0)
            with self.subTest(code=code):
                self.assertGreaterEqual(
                    hit.get(code, 0),
                    expected,
                    f"{code} 有 {expected} 个 REJECT 分支，红线样本只命中 "
                    f"{hit.get(code, 0)} 条——样本被删或失效，或有分支无样本钉住。",
                )

    def test_structural_exception_branches_are_pinned(self):
        """S0 / S10 / S11 与 PENDING 分支（S3 之②、S4）的逐分支行为级探针。

        这五类 Finding 分支在结构上进不了 MUST_REJECT（输入不是单个用例
        字典，或只产 PENDING 不产 REJECT），由本用例逐分支注入对抗样本。
        """
        import tempfile

        # S0：前置防御——用例不是字典
        r0 = check_testcase("not-a-dict", path="<t20-s0>")
        self.assertFalse(r0.ok, "S0：非字典输入未被拦下")
        self.assertIn("S0", r0.codes())

        # S10：批量维度——两份用例归一化后撞同一模块段
        r10 = GateReport()
        gate_s10_module_collision([("order+create", {}), ("order create", {})], r10)
        self.assertFalse(r10.ok, "S10：归一化撞名未被拦下")
        self.assertIn("S10", r10.codes())

        # S11 分支 1：文档不存在
        r11a = GateReport()
        gate_s11_document_encoding(os.path.join(BASE, "no_such_doc_t20.md"), r11a)
        self.assertFalse(r11a.ok, "S11 分支1：不存在的文档未被拦下")

        # S11 分支 2：0 字节
        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        try:
            r11b = GateReport()
            gate_s11_document_encoding(path, r11b)
            self.assertFalse(r11b.ok, "S11 分支2：0 字节文档未被拦下")
        finally:
            os.unlink(path)

        # S11 分支 3：编码不可识别（utf-8-sig / utf-8 / gbk / gb18030 全部失败）
        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        try:
            with open(path, "wb") as fp:
                fp.write(b"\xff\xff\xff\xff")
            r11c = GateReport()
            enc = gate_s11_document_encoding(path, r11c)
            self.assertIsNone(enc, "S11 分支3：坏编码文档被误判可读")
            self.assertFalse(r11c.ok, "S11 分支3：四编码全失败的文档未被拦下")
        finally:
            os.unlink(path)

        # S3 之②：显式 `validate: []` → 恰一条 PENDING（T1 三态）
        explicit_empty = {
            "config": {"name": "t", "base_url": "http://127.0.0.1:1"},
            "teststeps": [
                {"name": "s", "request": {"method": "GET", "url": "/a"}, "validate": []}
            ],
        }
        r3 = check_testcase(explicit_empty, path="<t20-s3b>")
        s3 = [f for f in r3.findings if f.code == "S3"]
        self.assertEqual(1, len(s3), "S3 之②：显式空应恰好一条 S3 发现")
        self.assertEqual(PENDING, s3[0].severity, "S3 之②：应为 PENDING")

        # S4：extract 未被校验/使用 → 恰一条 PENDING（词边界判据 a/b/c 均不命中）
        unused_extract = {
            "config": {"name": "t", "base_url": "http://127.0.0.1:1"},
            "teststeps": [
                {
                    "name": "s",
                    "request": {"method": "GET", "url": "/a"},
                    "extract": {"token": "body.data.token"},
                    "validate": [{"eq": ["status_code", 200]}],
                }
            ],
        }
        r4 = check_testcase(unused_extract, path="<t20-s4>")
        s4 = [f for f in r4.findings if f.code == "S4"]
        self.assertEqual(1, len(s4), "S4：未被使用的 extract 应恰好一条发现")
        self.assertEqual(PENDING, s4[0].severity, "S4：应为 PENDING")


class TestBatchAndDocumentDimensions(unittest.TestCase):
    """S10（批量撞名）与 S11（文档编码）两个非断言语义维度。"""

    def test_s10_detects_module_name_collision(self):
        report = GateReport()
        gate_s10_module_collision([("order+create", {}), ("order create", {})], report)
        self.assertFalse(report.ok, "两个用例归一化后撞同一模块段，未被拦下")
        self.assertIn("S10", report.codes())

    def test_s10_allows_distinct_names(self):
        report = GateReport()
        gate_s10_module_collision([("order_create", {}), ("user_info", {})], report)
        self.assertTrue(report.ok, "互不相同的用例名被误伤")

    def test_s11_rejects_empty_document(self):
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        try:
            report = GateReport()
            enc = gate_s11_document_encoding(path, report)
            self.assertIsNone(enc)
            self.assertFalse(report.ok, "0 字节文档未被拦下")
            self.assertIn("S11", report.codes())
        finally:
            os.unlink(path)

    def test_s11_rejects_missing_document(self):
        report = GateReport()
        gate_s11_document_encoding(os.path.join(BASE, "no_such_doc_xyz.md"), report)
        self.assertFalse(report.ok, "不存在的文档未被拦下")

    def test_s11_reads_gbk_and_bom(self):
        """编码探测必须复用导入器口径：gbk / utf-8-sig 都要读得出来（P-6 对抗样本）。"""
        import tempfile

        cases = [
            ("utf-8-sig", "接口文档".encode("utf-8-sig")),
            ("gbk", "接口文档".encode("gbk")),
            ("utf-8", "接口文档".encode("utf-8")),
        ]
        for expected, raw in cases:
            with self.subTest(encoding=expected):
                fd, path = tempfile.mkstemp(suffix=".md")
                os.close(fd)
                try:
                    with open(path, "wb") as fp:
                        fp.write(raw)
                    report = GateReport()
                    enc = gate_s11_document_encoding(path, report)
                    self.assertIsNotNone(enc, f"{expected} 文档应当可读，实际被拒")
                    self.assertTrue(report.ok, f"{expected} 文档被误伤")
                finally:
                    os.unlink(path)


class TestS3ThreeStatesAndS4Precision(unittest.TestCase):
    """S3 三态（T1~T3）与 S4 词边界引用（T21）的判据级用例（方案 v8 落地）。

    每条用例都能指认到 §3.3 表的某一行 / §十 T1、T21 的验收判据——
    与 MUST_REJECT / MUST_ALLOW 双集一起构成"三方对账"的红线样本。
    """

    def _two_step_login(self, first_validate):
        """两步登录链路（§12.7 乙⑦ 形态）：登录只取 token，下游用 $token。"""
        steps = [
            {
                "name": "登录并提取 token（该步骤只负责取 token）",
                "request": {"method": "POST", "url": "/api/login", "json": {"u": 1, "p": 2}},
                "extract": {"token": "body.data.token"},
            },
            {
                "name": "用 token 查询用户信息",
                "request": {
                    "method": "GET",
                    "url": "/api/user/info",
                    "headers": {"Authorization": "Bearer $token"},
                },
                "validate": [
                    {"eq": ["status_code", 200]},
                    {"type_match": ["body.data.token", "str"]},
                ],
            },
        ]
        if first_validate is not None:
            steps[0]["validate"] = first_validate
        return {
            "config": {"name": "两步链路", "base_url": "http://127.0.0.1:1"},
            "teststeps": steps,
        }

    def test_login_only_extract_passes_with_zero_pending(self):
        """T21 判据①：登录取 token、下游用 $token → 通过且零 PENDING。"""
        report = check_testcase(self._two_step_login(None), path="<t21-1>")
        self.assertTrue(
            report.ok, f"多步链路合法写法被拦下：{[f.code for f in report.rejects]}"
        )
        self.assertEqual([], report.findings, "该形态不应有任何 finding（含 PENDING）")

    def test_explicit_empty_validate_is_pending_not_reject(self):
        """T1 判据②：validate: [] 是显式声明 → S3 PENDING，不再 REJECT。"""
        report = check_testcase(self._two_step_login([]), path="<t1-2>")
        self.assertTrue(report.ok, "显式空 validate 不得再被硬拒（旧实现即死于此）")
        s3 = [f for f in report.findings if f.code == "S3"]
        self.assertEqual(1, len(s3), "显式空应恰好产生一条 S3 发现")
        self.assertEqual(PENDING, s3[0].severity, "三态之②是 PENDING，不是 REJECT")

    def test_s4_substring_match_must_not_count_as_used(self):
        """T21 判据②：check 的 token_type 不是 token 的引用（旧子串匹配会静默漏报）。"""
        case = {
            "config": {"name": "t", "base_url": "http://127.0.0.1:1"},
            "teststeps": [
                {
                    "name": "s",
                    "request": {"method": "POST", "url": "/login", "json": {"u": 1}},
                    "extract": {"token": "body.data.token"},
                    "validate": [
                        {"eq": ["status_code", 200]},
                        {"eq": ["body.data.token_type", "Bearer"]},
                    ],
                }
            ],
        }
        report = check_testcase(case, path="<t21-2>")
        s4 = [f for f in report.findings if f.code == "S4" and f.severity == PENDING]
        self.assertEqual(1, len(s4), "token_type ≠ token：S4 必须仍报 PENDING（漏报对抗）")

    def test_s4_var_word_boundary_not_prefix_matched(self):
        """T21 词边界负例：$token_id 是另一个变量名，不算 $token 的使用。"""
        case = {
            "config": {"name": "t", "base_url": "http://127.0.0.1:1"},
            "teststeps": [
                {
                    "name": "login",
                    "request": {"method": "POST", "url": "/login", "json": {"u": 1}},
                    "extract": {"token": "body.data.token"},
                },
                {
                    "name": "use",
                    "request": {
                        "method": "GET",
                        "url": "/me",
                        "headers": {"Authorization": "Bearer $token_id"},
                    },
                    "validate": [{"eq": ["status_code", 200]}],
                },
            ],
        }
        report = check_testcase(case, path="<t21-3>")
        s4 = [f for f in report.findings if f.code == "S4" and f.severity == PENDING]
        self.assertEqual(1, len(s4), "$token_id ≠ $token：S4 应仍报 PENDING")


class TestSeveritySemantics(unittest.TestCase):
    """严重级别语义：REJECT 拦下，PENDING 只提请人工确认（不拦）。"""

    def test_extract_without_assertion_is_pending_not_reject(self):
        """S4 刻意只报 PENDING：多步链路里"容忍 None"是合理权衡，闸门不替用户下结论。"""
        case = {
            "config": {"name": "t", "base_url": "http://127.0.0.1:1"},
            "teststeps": [
                {
                    "name": "s",
                    "request": {"method": "GET", "url": "/a"},
                    "extract": {"token": "body.data.token"},
                    "validate": [{"eq": ["status_code", 200]}],
                }
            ],
        }
        report = check_testcase(case, path="<test>")
        self.assertTrue(report.ok, "S4 不该硬拒（它只是待确认项）")
        self.assertTrue(
            any(f.code == "S4" and f.severity == PENDING for f in report.findings),
            "未校验的 extract 应当产生一条 PENDING 发现",
        )

    def test_report_serializes(self):
        case = {
            "config": {"name": "t", "base_url": "http://127.0.0.1:1"},
            "teststeps": [
                {
                    "name": "s",
                    "request": {"method": "GET", "url": "/a"},
                    "validate": [{"not_equal": ["body.token", ""]}],
                }
            ],
        }
        d = check_testcase(case, path="<test>").to_dict()
        self.assertFalse(d["ok"])
        self.assertGreaterEqual(d["rejects"], 1)
        self.assertIn("S1", d["codes"])
        self.assertTrue(all({"code", "severity", "where", "message"} <= set(f) for f in d["findings"]))


if __name__ == "__main__":
    unittest.main()
