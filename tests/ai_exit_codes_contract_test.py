# -*- coding: utf-8 -*-
"""AI 旁路包 CLI 的**退出码契约**（表 ↔ 常量 ↔ 端到端断言，三处对账）。

## 为什么需要（R14 的退出码版本）

`EXIT_BUDGET = 5` 的教训（决策清单 §四 **M-4** 实测，2026-09-26）：常量存在、`cli.py` 里
`return EXIT_BUDGET` 也确实写着，但在 `gen` 路径上**没有任何输入能让它返回**——片级预算超限
被 `gen.py` 捕获并记成"片失败"（`Round(1, "budget", …)`），最终统一 `EXIT_CASE_FAILED(1)`。
而手册里那句「`5` = 输入超预算」照旧挂着：**一条永不发生的承诺**（`tests/` 里对它零断言）。

## 判据（按"**别的文件里有没有用例断言过这个码**"判）

1. **表 ↔ 常量对账**：手册退出码表里的码集合 == `cli.py` 的 `EXIT_*` 常量值集合；
2. **每个码要么有端到端断言、要么两边标"保留码"**：断言来源是**本文件以外**的 AI 测试切片
   （见下）；找不到就必须①`cli.py` 该常量行写 `# 保留码：<理由>`，且②手册该行也标「保留码」。

## 两处防假绿（都是**注入式实测**抓出来的，不是想出来的）

- **切片口径**：只扫**提到 `interfacetester_ai` 的测试文件**——内核 CLI 也断言
  `returncode 0/1/2`，不切片会假绿（与 C-8「ai 包模块地图」同款教训）；
- **★判据不能拿自己作证**：本文件里的**合成字符串**（元护栏的输入 `assertEqual(proc.returncode, 5, msg)`）
  会被自己的正则当成"真证据"→ 于是删掉保留码后判据**照样绿**。所以 `_ai_test_evidence_text()`
  **排除本文件**，端到端用例单独放在 `tests/ai_cli_exit_codes_test.py`。

## 元护栏

`test_guard_would_catch_a_reserved_code_without_marker`（注入"表里有码、无断言也无保留标记"）
与 `test_evidence_slice_rejects_negated_and_cross_line_assertions`（否定断言/跨行断言不算证据）
——两条都对应真发生过的空跑。
"""

import os
import pathlib
import re
import unittest

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CLI_PY = os.path.join(BASE, "interfacetester_ai", "cli.py")
MANUAL = os.path.join(BASE, "docs", "使用手册-AI协作层.md")
TESTS_DIR = os.path.join(BASE, "tests")
# 本文件名：**判据不能拿自己作证**（见模块 docstring）
SELF_NAME = "ai_exit_codes_contract_test.py"

# 手册退出码表的行：| `5` | 说明 |
_TABLE_ROW_RE = re.compile(r"^\|\s*`(\d+)`\s*\|\s*(.+?)\s*\|\s*$", re.M)
_RESERVED_MARK = "保留码"


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _constants(cli_source):
    """`[(常量名, 码值, 该行原文)]`——保留码标记写在常量行的注释里。"""
    rows = []
    for line in cli_source.splitlines():
        match = re.match(r"^(EXIT_[A-Z_]+)\s*=\s*(\d+)", line)
        if match:
            rows.append((match.group(1), int(match.group(2)), line))
    return rows


def _documented_rows(manual_text):
    return {int(code): text for code, text in _TABLE_ROW_RE.findall(manual_text)}


def _ai_test_evidence_text(exclude_self=True):
    """AI 切片：提到 `interfacetester_ai` 的测试文件；**默认排除本文件**（防自证）。"""
    chunks = []
    for path in sorted(pathlib.Path(TESTS_DIR).glob("*.py")):
        if exclude_self and path.name == SELF_NAME:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "interfacetester_ai" in text:
            chunks.append(f"\n# ==== {path.name} ====\n{text}")
    return "".join(chunks)


def _has_end_to_end_assertion(code, evidence_text):
    """该码有没有被**正向**断言过。

    ★两个必须防的假绿（注入式元护栏实测发现，2026-09-26）：
    1. **跨行匹配**：`assertNotEqual(proc.returncode,` 与 `5,` 分居两行时，用 `\\s` 会把它们
       当成 `assertEqual(proc.returncode, 5)` —— 否定断言冒充成了证据；
    2. **否定断言本身**：`assertNotEqual(…, 5, …)` 恰恰说明**这个码不该出现**，不算可达证据。
    所以本函数**逐行**判、且**跳过含 `assertNotEqual` 的行**。
    """
    positive_patterns = (
        rf"assertEqual\([^\n]*returncode[^\n]*[,\s]{code}\b",
        rf"assertIn\([^\n]*returncode[^\n]*\b{code}\b",
        rf"returncode\s*==\s*{code}\b",
    )
    for line in evidence_text.splitlines():
        if "assertNotEqual" in line:
            continue
        if any(re.search(pattern, line) for pattern in positive_patterns):
            return True
    return False


def _contract_gaps(constants, documented, evidence_text):
    """返回问题清单（空 = 通过）。纯函数，方便元护栏注入合成输入。"""
    problems = []
    code_values = {code for _, code, _ in constants}

    for code, text in sorted(documented.items()):
        if code not in code_values:
            problems.append(f"手册写了退出码 {code}，但 cli.py 里没有对应常量：{text[:60]}")

    for name, code, line in constants:
        documented_text = documented.get(code, "")
        marked_in_code = _RESERVED_MARK in line
        marked_in_doc = _RESERVED_MARK in documented_text
        asserted = _has_end_to_end_assertion(code, evidence_text)

        if code not in documented:
            problems.append(f"{name} = {code} 没写进手册退出码表")
            continue
        if asserted:
            continue
        if marked_in_code and marked_in_doc:
            continue
        if marked_in_code and not marked_in_doc:
            problems.append(f"{name} = {code} 在 cli.py 标了保留码，但手册那行没标")
        elif marked_in_doc and not marked_in_code:
            problems.append(f"{name} = {code} 在手册标了保留码，但 cli.py 那行没标")
        else:
            problems.append(
                f"{name} = {code} 既没有端到端断言、也没标保留码"
                f"（文档却把它当既有能力写：{documented_text[:50]}）"
            )
    return problems


class TestExitCodeContract(unittest.TestCase):
    """表 ↔ 常量 ↔ 端到端断言（**别人的用例**），三处对账。"""

    def test_documented_codes_match_cli_constants(self):
        documented = _documented_rows(_read(MANUAL))
        self.assertTrue(documented, "手册里没抓到退出码表？判据失效")

        code_values = {code for _, code, _ in _constants(_read(CLI_PY))}
        self.assertEqual(
            sorted(documented),
            sorted(code_values),
            "手册退出码表与 cli.py 的 EXIT_* 常量不一致（新增/删码必须两边同步）",
        )

    def test_evidence_slice_is_not_empty_and_covers_end_to_end_file(self):
        """判据自检：切片里必须**真的**含端到端用例文件，否则下面那条是空跑。"""
        evidence = _ai_test_evidence_text()
        self.assertIn(
            "ai_cli_exit_codes_test.py",
            evidence,
            "证据切片里没有端到端用例文件 —— 判据会退化成'永远只认保留码'",
        )

    def test_every_code_is_asserted_or_marked_reserved(self):
        gaps = _contract_gaps(
            _constants(_read(CLI_PY)), _documented_rows(_read(MANUAL)), _ai_test_evidence_text()
        )
        self.assertEqual(
            gaps,
            [],
            "退出码契约有缺口（要么补端到端断言，要么两边都标保留码）：\n  " + "\n  ".join(gaps),
        )

    def test_guard_would_catch_a_reserved_code_without_marker(self):
        """元护栏：合成「表里写了 5、既无断言也没标保留」的输入，判据必须点出来。"""
        constants = [("EXIT_OK", 0, "EXIT_OK = 0"), ("EXIT_BUDGET", 5, "EXIT_BUDGET = 5")]
        documented = {0: "正常", 5: "输入超预算（切片太大）"}
        gaps = _contract_gaps(constants, documented, "self.assertEqual(proc.returncode, 0, out)")
        self.assertTrue(
            any("既没有端到端断言、也没标保留码" in gap for gap in gaps),
            "注入的『永不返回的码』竟然没被发现 —— 那么上面那条护栏是空跑：" + str(gaps),
        )

    def test_evidence_slice_rejects_negated_and_cross_line_assertions(self):
        """★元护栏（注入式实测抓到的假绿）：否定断言与跨行断言都不算"有端到端断言"。"""
        cross_line = "self.assertNotEqual(\n    proc.returncode,\n    5,\n    msg,\n)"
        self.assertFalse(_has_end_to_end_assertion(5, cross_line))
        self.assertFalse(_has_end_to_end_assertion(5, "self.assertNotEqual(proc.returncode, 5, msg)"))
        self.assertTrue(_has_end_to_end_assertion(5, "self.assertEqual(proc.returncode, 5, msg)"))

    def test_reserved_marker_on_both_sides_would_satisfy_the_guard(self):
        """反向：两边都标保留码时判据必须放行（否则它只是"永远红"）。"""
        constants = [
            ("EXIT_OK", 0, "EXIT_OK = 0"),
            ("EXIT_BUDGET", 5, "EXIT_BUDGET = 5  # 保留码：说明"),
        ]
        documented = {0: "正常", 5: "**保留码**：当前无端到端入口"}
        self.assertEqual(
            _contract_gaps(constants, documented, "self.assertEqual(proc.returncode, 0, out)"), []
        )

    def test_guard_ignores_its_own_synthetic_strings(self):
        """★元护栏：本文件里的合成字符串不得被当成证据（判据不能拿自己作证）。

        判法：拿**本文件独有的合成证据串**去撞切片——它若出现，说明自证漏洞又回来了。
        （不能拿"文件名"判：别的文件会在 docstring 里提到本文件名。）
        """
        evidence = _ai_test_evidence_text()
        self.assertNotIn("assertEqual(proc.returncode, 5, msg)", evidence)
        self.assertNotIn("EXIT_BUDGET = 5  # 保留码：说明", evidence)
        self.assertFalse(
            _has_end_to_end_assertion(5, evidence),
            "证据切片里居然有对码 5 的正向断言 —— 要么真补了入口（那该同步三处文档），"
            "要么判据又在自己给自己作证",
        )
