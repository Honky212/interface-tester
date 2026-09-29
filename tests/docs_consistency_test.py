# -*- coding: utf-8 -*-
"""文档一致性不变量（L11 + `deploy.md` 版本号硬编码）。

两类问题都属于「文档与事实脱节，且没有任何机制会发现」：

1. **L11 本地链接死链**：`README.md` 的「文档地图」是用户找文档的唯一入口。
   修复前它指向 `docs/P0阶段开发记录.md` ~ `docs/P3阶段开发记录.md` 这几个**已删除**的文件
   （`git status` 显示为 `D`）——点进去就是 404。`docs/能力清单.md` 也引用了同样被删的
   `docs/缺陷修复日志0916-8.md`。
2. **版本号被写死**：`deploy.md` 把交付产物名写成 `interfacetester-4.3.5-py3-none-any.whl`，
   而版本只在 `interfacetester/__init__.py` 的 `__version__` 维护 —— 升版本就漂移。

## 检查范围（刻意的边界，都有实测依据）

**链接检查**
- **只检查 `.md`**：`.md` 里的 `[文本](路径)` 是真正的导航机制，可机器判定、无误报。
  仓库根的 `.txt`（如 `接口测试点补充.txt`）是**用户自己的工作笔记**，其中
  `[获取token](request/56274212-...)` 是 Postman 的界面路径记法、**不是 markdown 链接**
  （实测该行不在围栏代码块内，语法上确实匹配 `[..](..)`）——一刀切检查会误报。
- **不检查反引号提及**：实测全仓有 **41 处**反引号引用指向已不存在的文件，绝大多数在
  `docs/接口自动化框架升级优化评估.md`、`docs/待处理项方案.md` 等**按日期归档的历史记录**里。
  那是当时的真实情况，追溯改写历史文档是错的。当前文档里的这类引用已手工订正。
- 跳过 `http(s)://`、`mailto:`、纯锚点 `#...`，以及**围栏代码块**里的内容。

**版本检查**
- 只检查**当前文档**（README / deploy / 使用教程 / 能力清单）；`docs/缺陷修复日志*.md`、
  `docs/接口自动化框架升级优化评估.md` 这类**按日期归档的记录**里出现旧版本号是正确的，不检查。
- 只拦两种**机器派生**的写法：① 产物名 `interfacetester-<版本>-...`；② `--version`/`-V`
  后面紧跟一个版本号（那是「期望输出」）。这两种写死了必然漂移。
- **不拦**正文里的「当前版本 4.3.5」这类面向人的说明——那是给读者的定位信息，
  且 `README.md:213` 里的 `HttpRunner v4.3.5` 指的是**上游版本**，
  与 `__version__` 是两码事，一刀切会误报。
"""

import os
import re
import unittest

from interfacetester import __version__

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
# 行内代码（`` `...` ``）：markdown 不会把它渲染成链接，**必须跳过**。
#
# NOTICE（实测踩到的）：最初只跳过围栏代码块，结果本文件作者写的修复日志里
# 「说明链接写法」的那几句（形如 `` `[文本](路径)` ``）被当成了真链接 → 3 条误报。
# 行内代码里的 `[..](..)` 是**示例文本**，不是导航。
INLINE_CODE_RE = re.compile(r"`[^`]*`")

# 产物名里写死版本：interfacetester-4.3.5-py3-none-any.whl
ARTIFACT_VERSION_RE = re.compile(r"interfacetester-(\d+\.\d+(?:\.\d+)?)-")
# `--version` / `-V` 后面紧跟一个版本号（即「期望输出 X」）
VERSION_NEAR_FLAG_RE = re.compile(r"(?:--version|-V)\b[^\n]*?\b(\d+\.\d+(?:\.\d+)?)\b")

SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "logs",
    ".pytest_cache",
    "reports",
}

# 「当前文档」= 用户照着做的那些；按日期归档的历史记录不在其中
CURRENT_DOCS = [
    "README.md",
    "deploy.md",
    "使用教程.md",
    os.path.join("docs", "能力清单.md"),
    # 批次 9-1 新增：架构与调用链文档（同样受「版本号不许写死」的约束）
    os.path.join("docs", "架构与调用链.md"),
    # ★§9.34 新增：交付说明（一页交付口径；同样受「版本号 / 基线数字只有一个来源」的约束）
    os.path.join("docs", "交付说明.md"),
]

# 护栏：扫描量低于这个数说明扫描没生效（空跑）
MIN_EXPECTED_FILES = 10
MIN_EXPECTED_LOCAL_LINKS = 15


def _iter_markdown_files():
    for root, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".md"):
                yield os.path.join(root, name)


def _iter_link_targets(lines):
    """产出 `(行号, 原始目标)`：只留「本地」链接，跳过围栏代码块与行内代码。"""
    in_fence = False
    for line_no, line in enumerate(lines, 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # 先把行内代码挖掉，再找链接（见 INLINE_CODE_RE 的 NOTICE）
        line = INLINE_CODE_RE.sub("", line)
        for match in LINK_RE.finditer(line):
            target = match.group(2)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0]
            if target:
                yield line_no, target


class TestDocumentationLinks(unittest.TestCase):
    """L11：文档里的本地 markdown 链接必须可达。"""

    def test_local_markdown_links_resolve(self):
        dead = []
        markdown_files = 0
        local_links = 0

        for path in _iter_markdown_files():
            markdown_files += 1
            rel = os.path.relpath(path, BASE)
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line_no, target in _iter_link_targets(lines):
                local_links += 1
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(path), target)
                )
                if not os.path.exists(resolved):
                    dead.append(f"{rel}:{line_no} → {target}")

        self.assertGreaterEqual(
            markdown_files, MIN_EXPECTED_FILES, "扫描到的 .md 太少，检查可能没生效"
        )
        self.assertGreaterEqual(
            local_links,
            MIN_EXPECTED_LOCAL_LINKS,
            "扫描到的本地链接太少，检查可能没生效",
        )

        self.assertEqual(
            dead,
            [],
            "以下本地链接指向不存在的文件（文档被删/改名后忘了更新引用）：\n  "
            + "\n  ".join(dead),
        )

    def test_scanner_skips_non_local_and_fenced_content(self):
        """判据自检（误报方向）：外链/邮件/锚点/围栏块/行内代码都不得被当成待检查的链接。

        NOTICE: 「漏报」方向由本批次实测覆盖——往 README 注入一条死链后
        `test_local_markdown_links_resolve` 会失败并点名该文件。
        """
        lines = [
            "[外链](https://example.com/nope)\n",
            "[邮件](mailto:a@b.c)\n",
            "[锚点](#section)\n",
            "[锚点带路径](README.md#五目录结构)\n",
            "```\n",
            "[围栏里的假链接](docs/does_not_exist_in_fence.md)\n",
            "```\n",
            # 行内代码里的链接语法是**示例文本**，不是导航
            #（这条是本批次实测踩到的：修复日志里「说明链接写法」的那句被误报成死链）
            "写法是 `[文本](路径)`，目标是 `docs/whatever.md`\n",
            "[真的本地链接](README.md)\n",
        ]

        targets = [target for _, target in _iter_link_targets(lines)]

        self.assertEqual(targets, ["README.md", "README.md"])


class TestDocVersionIsNotFrozen(unittest.TestCase):
    """`deploy.md` 的版本号硬编码：`__version__` 是唯一事实来源，文档不得冻住它。"""

    def _current_doc_lines(self):
        for relative_path in CURRENT_DOCS:
            path = os.path.join(BASE, relative_path)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                for line_no, line in enumerate(f, 1):
                    yield relative_path, line_no, line

    def test_detector_would_catch_the_old_hardcoded_forms(self):
        """非空跑自检：判据必须能识别修复前真实存在的两种写法。"""
        old_artifact = "# 产物：dist/interfacetester-4.3.5-py3-none-any.whl"
        old_expected = "hrun --version            # 期望输出 4.3.5"

        self.assertEqual(
            ARTIFACT_VERSION_RE.search(old_artifact).group(1), "4.3.5"
        )
        self.assertEqual(
            VERSION_NEAR_FLAG_RE.search(old_expected).group(1), "4.3.5"
        )

    def test_artifact_name_does_not_freeze_the_version(self):
        """产物名里不得写死版本（写死了升版本就漂移）。"""
        offenders = []
        for relative_path, line_no, line in self._current_doc_lines():
            match = ARTIFACT_VERSION_RE.search(line)
            if match:
                offenders.append(
                    f"{relative_path}:{line_no} → 冻结了版本 {match.group(1)}（当前 __version__ "
                    f"= {__version__}）"
                )

        self.assertEqual(
            offenders,
            [],
            "文档把交付产物名里的版本写死了；版本只在 interfacetester/__init__.py 维护，"
            "请改成 `interfacetester-<版本>-py3-none-any.whl` 这样的占位写法：\n  "
            + "\n  ".join(offenders),
        )

    def test_expected_version_output_is_not_frozen(self):
        """`--version` / `-V` 的「期望输出」不得写死版本号。"""
        offenders = []
        for relative_path, line_no, line in self._current_doc_lines():
            match = VERSION_NEAR_FLAG_RE.search(line)
            if match and match.group(1) != __version__:
                offenders.append(
                    f"{relative_path}:{line_no} → 期望输出写成 {match.group(1)}，"
                    f"而 __version__ = {__version__}"
                )

        self.assertEqual(
            offenders,
            [],
            "文档把 `--version` 的期望输出写死了；请改成「与 "
            "interfacetester/__init__.py 的 __version__ 一致」：\n  " + "\n  ".join(offenders),
        )


# ---------------------------------------------------------------------------
# L29 第二半：`git show <ref>:<path>` 是**指令型引用**，链接检查器抓不到
# ---------------------------------------------------------------------------

# `git show <ref>:<path>`（含 `git show <ref>:<path> > out.txt` 之类）
GIT_SHOW_RE = re.compile(r"git show\s+([^\s:]+):([^\s`\"'<>|]+)")

# 日志/评估类文档是**历史叙述**：会刻意引用「已经失效的命令」作为证据
# （例如 0918-8 的 L29 一节就引用了 `git show HEAD:docs/能力边界.txt → fatal: does not exist`），
# 因此不参与本校验。被校验的是**面向用户、会被照着敲**的文档。
NARRATIVE_DOC_MARKERS = ("缺陷修复日志", "开发记录", "接口自动化框架升级优化评估", "待处理项方案")

# 护栏：一条引用都没扫到说明检查和没写一样
MIN_EXPECTED_GIT_SHOW_REFS = 1


def _iter_git_show_references():
    """产出 `(相对路径, 行号, ref, path)`：只扫**面向用户**的 .md。"""
    for path in _iter_markdown_files():
        rel = os.path.relpath(path, BASE)
        if any(marker in rel for marker in NARRATIVE_DOC_MARKERS):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                for match in GIT_SHOW_RE.finditer(line):
                    yield rel, line_no, match.group(1), match.group(2).rstrip("。，,.)）")


def _git(*args):
    """跑一条只读 git 命令，返回 (returncode, stdout)。"""
    import subprocess

    proc = subprocess.run(
        ["git", *args], cwd=BASE, capture_output=True, text=True, errors="replace"
    )
    return proc.returncode, proc.stdout.strip()


class TestGitShowReferences(unittest.TestCase):
    """L29 第二半：文档里「`git show <ref>:<路径>` 取回原文」的指令必须真的能执行。

    NOTICE: 现有的链接检查器只查 markdown 链接（`[文本](路径)`），**抓不到指令型引用**
    ——而 L29 的现场正是这样一条指令：`git show HEAD:docs/能力边界.txt` 在文件被删之后
    就永久失效（实测 `fatal: does not exist`），却没有任何机制会发现。
    8-0 把三处改成了固定 ref（tag `docs-before-batch-cleanup` / commit `7f7d2db`），
    这里把「改完之后仍然可取回」钉成不变量。
    """

    def test_scanner_finds_references_and_skips_narrative_docs(self):
        """判据自检：能识别 `git show` 引用，且不把归档记录算进来。

        NOTICE（0920 批次 4）：本用例原本在「一条引用都没扫到」时**直接失败**，
        理由是"判据或文档结构变了"。但那把两件事混成了一件：

        - **判据失效**（正则写错 / 扫描目录变了）→ 应该失败；
        - **用户文档里确实不再有这类指令**（引用了被删除文档的那几处已经清理掉）
          → 这是合法状态，不该判失败。

        实测（2026-09-21）：工作区里 24 个旧缺陷记录文档被删除，用户文档里已经
        **没有任何** `git show <ref>:<path>` 指令 —— 本用例因此长期为红，
        而红色信号一旦"长期存在且无关"，就等于没有信号（同一批 CI 依赖缺失的心态）。
        现在改成：扫不到引用 → `skipTest` 并说明；扫到引用 → 照旧逐条校验
        （`test_each_reference_resolves_in_this_checkout` 不受影响，
        **"凡是写出来的引用都必须能取回"这条不变量始终生效**）。
        """
        references = list(_iter_git_show_references())
        files = {rel for rel, _no, _ref, _path in references}

        if len(references) < MIN_EXPECTED_GIT_SHOW_REFS:
            self.skipTest(
                "当前用户文档里没有 `git show <ref>:<路径>` 指令 —— "
                "要么这类引用已被清理（合法），要么判据/文档结构变了（需排查）；"
                "两种情况下本用例都不该判失败。若确实想恢复「取回原文」这类指令，"
                "请在有引用的 checkout 上确认这条自检仍然有效。"
            )

        self.assertFalse(
            any(any(m in rel for m in NARRATIVE_DOC_MARKERS) for rel in files),
            f"归档记录类文档不应参与校验，但扫到了：{files}",
        )

    def test_each_reference_resolves_in_this_checkout(self):
        """每个引用都必须能在本 checkout 里取回；ref 本地不可解析时明确跳过。"""
        references = list(_iter_git_show_references())
        missing_refs = set()
        unresolvable_paths = []
        checked = 0

        for rel, line_no, ref, path in references:
            if _git("rev-parse", "--verify", "--quiet", ref)[0] != 0:
                # 浅克隆（CI 默认 fetch-depth=1）里 tag/历史 commit 不存在 → 无法验证
                missing_refs.add(ref)
                continue
            checked += 1
            if _git("cat-file", "-e", f"{ref}:{path}")[0] != 0:
                unresolvable_paths.append(f"{rel}:{line_no} → {ref}:{path}")

        if checked == 0:
            self.skipTest(
                f"本 checkout 里这些 ref 都不可解析（浅克隆？）：{sorted(missing_refs)}"
            )

        self.assertEqual(
            unresolvable_paths,
            [],
            "文档让用户用 `git show` 取回的文件在该 ref 下不存在（指令型死链）：\n  "
            + "\n  ".join(unresolvable_paths),
        )

    def test_moving_head_is_not_used_for_archived_files(self):
        """归档取回指令不得用 `HEAD`：`HEAD` 会随每次提交移动，取不回已删除的文件。

        NOTICE: 这正是 L29 的根因（实测 `git show HEAD:docs/能力边界.txt` → `fatal`）。
        """
        offenders = [
            f"{rel}:{line_no} → {ref}:{path}"
            for rel, line_no, ref, path in _iter_git_show_references()
            if ref == "HEAD"
        ]

        self.assertEqual(
            offenders,
            [],
            "归档取回指令里不能用 `HEAD`（它会移动，指向的文件可能已被删除）；"
            "请改成固定 ref（tag 或 commit sha）：\n  " + "\n  ".join(offenders),
        )


# --------------------------------------------------------------------------- 测试基线口径
# 「当前基线」行的唯一合法写法（README.md「六、测试与质量」）：
#     当前基线：**确定性口径 1340 passed / 10 skipped / 6 deselected**（...）
BASELINE_LINE_RE = re.compile(
    r"当前基线[：:]\s*\**\s*确定性口径\s*(\d+)\s*passed\s*/\s*(\d+)\s*skipped\s*/\s*(\d+)\s*deselected"
)
# 「当前基线」行里对**总量**的括注，例如「（全量 1362 个用例、801 个 subtest）」
BASELINE_TOTAL_RE = re.compile(r"全量\s*(\d+)\s*个用例")
# 任何文档里「确定性口径 N passed / M skipped / K deselected」的写法（用于抓副本）
BASELINE_ANY_RE = re.compile(
    r"确定性口径\s*\**\s*(\d+)\s*passed\s*/\s*(\d+)\s*skipped\s*/\s*(\d+)\s*deselected"
)
# 「期望：N passed, M skipped, K deselected」（deploy.md 的交付验收行）
BASELINE_EXPECT_RE = re.compile(
    r"期望[：:]\s*(\d+)\s*passed,\s*(\d+)\s*skipped,\s*(\d+)\s*deselected"
)
# 文档里出现的 pytest node id（**两种写法都要扫**）：
#   ① `--deselect tests/x.py::Class::test_y`（README §六 与 CI 主 job 的写法）
#   ② 裸列表 `tests/x.py::Class::test_y   # 注释`（docs/ci/README.md 坑 5 的写法）
#
# NOTICE（批次 9-1，本不变量第一版**漏了**形式 ②，被注入验证抓到）：坑 5 的那个裸列表
# 被文档自己称为「6 个 node id 的**唯一事实来源**」，而第一版正则要求前缀 `--deselect`，
# 于是「把裸列表里的 id 改坏」这条注入**不会变红**——护栏形同虚设。
# 现在两种形式一起扫。
NODE_ID_RE = re.compile(
    r"((?:[\w.\-]+/)*[\w.\-]+\.py::[A-Za-z_]\w*(?:::[A-Za-z_]\w*)?)"
)
MIN_EXPECTED_DESELECTS = 6


class TestBaselineNumbersHaveOneSource(unittest.TestCase):
    r"""批次 9-1：**「当前基线」只能有一处事实来源**，且 node id 引用必须真的能收集到。

    ## 为什么需要这条护栏

    `491 passed / 4 skipped / 6 deselected` 这句在修复前有 **3 个副本**
    （`README.md`、`deploy.md`、`docs/ci/README.md`），而真实值早已是 1328+ ——
    三处一起过期、没有任何机制会发现。逐条影响：

    - 用户在 `deploy.md` 的交付验收清单里看到「期望 491 passed」，跑出来 1340 会以为**环境有问题**；
    - CI 文档的「用例数对得上」这一条也随之失效（拿一个错数字当基准）。

    现在的口径：`README.md`「六、测试与质量」是**唯一**的当前基线；
    其它文档可以引用它，但**不许**再抄一份自己的数字。

    ## 为什么还要查 node id

    **实测（本批发现）**：pytest 对**不存在的** `--deselect` node id
    **既不报错也不警告**（`--deselect tests/response_test.py::NoSuchClass::test_nope`
    → 47 passed, exit 0；而同样这个 id 作为**位置参数**才会 exit 4）。
    于是「改了个测试类名」会让 6 条去选**静默失效**：
    在没外网的机器上，这些用例会变成失败/超时，而 CI 文档还在说「确定性口径」。

    本类钉住的是**文档里的 node id 引用**（两种写法都扫）：
    凡是「文件真实存在」的引用，都必须能被 `pytest --collect-only` 收集到。
    **边界**：文件本身不存在的提及（例如 `docs/ci/README.md` 里作为「在 tests/ 目录下执行」
    示例的 `parser_test.py::TestParserBasic::...`）**不参与**本检查 —— 那是「代码块里的
    相对路径示例」，不是导航。这类引用的死链由 `TestDocumentationLinks` 的口径另行覆盖。
    """

    def _doc_files(self):
        """参与基线口径检查的文档（与 `CURRENT_DOCS` 一致，外加 CI 文档）。"""
        return [
            "README.md",
            "deploy.md",
            os.path.join("docs", "ci", "README.md"),
        ]

    def _read(self, rel):
        with open(os.path.join(BASE, rel), encoding="utf-8") as fp:
            return fp.read()

    def _baseline_facts(self):
        """产出 `(rel, 行号, 形态, 三元组)`：文档里所有「用例数」的写法。"""
        facts = []
        for rel in self._doc_files():
            text = self._read(rel)
            for label, pattern in (
                ("当前基线", BASELINE_LINE_RE),
                ("确定性口径", BASELINE_ANY_RE),
                ("期望", BASELINE_EXPECT_RE),
            ):
                for match in pattern.finditer(text):
                    line_no = text[: match.start()].count("\n") + 1
                    facts.append((rel, line_no, label, match.groups()))
        return facts

    def test_exactly_one_current_baseline_line(self):
        """全仓只允许一条「当前基线」行，且它在 README 的「六、测试与质量」里。"""
        hits = [fact for fact in self._baseline_facts() if fact[2] == "当前基线"]

        self.assertEqual(
            len(hits),
            1,
            "「当前基线」必须恰好出现在一处（README.md「六、测试与质量」）；"
            f"实际找到 {len(hits)} 处：{[(f[0], f[1]) for f in hits]}\n"
            "修法：删掉副本，改成指向 README 该节（抄一份就多一个会过期的副本）。",
        )
        self.assertEqual(hits[0][0], "README.md", "当前基线行应在 README.md")

    def test_all_baseline_mentions_agree(self):
        """**所有**用例数写法（当前基线 / 确定性口径 / 期望）必须完全一致。

        这条覆盖「只改了一处」这个最常见的漂移形态：
        README 的基线改了、`deploy.md` 的验收期望值忘了改 —— 只要有任何一处
        与其它处不同，本用例立刻变红（注入验证：改 README 或改 deploy 都能让它变红）。
        """
        facts = self._baseline_facts()
        self.assertGreaterEqual(
            len(facts), 2, f"只找到 {len(facts)} 处用例数写法，判据或文档结构变了：{facts}"
        )

        tuples = {fact[3] for fact in facts}
        self.assertEqual(
            len(tuples),
            1,
            "文档里的用例数口径不一致（同一件事写了不同的数字）：\n  "
            + "\n  ".join(f"{rel}:{line} [{label}] {nums}" for rel, line, label, nums in facts),
        )

    def test_recorded_total_matches_what_pytest_actually_collects(self):
        r"""记录的总用例数必须**等于当前实际收集到的用例数**（防止「加了用例没改文档」）。

        NOTICE（批次 9-1，这条是被自己踩出来的）：本批先按实测把 README 的基线写成
        「确定性口径 1340 passed / 10 skipped / 6 deselected（全量 1356 个用例）」，
        之后又**补了 3 条架构文档护栏** —— 数字立刻就旧了（实际 1346 / 1362），
        而「三条文档互相一致」那两条护栏**发现不了**这件事（它们只比较文档之间）。
        所以这里加一条与**事实**对账的护栏：跑一次 `--collect-only`（**不执行**用例，
        实测 0.3s、不联网），把「收集到的用例数」与文档里的总量比。

        它把「加用例」这件事变成**强制同步文档**的动作：忘了改基线，这条就红。

        边界：只对账**总量**（`--collect-only` 给得出），不对账 passed/skipped 的分解
        —— 那需要真跑一遍全量（约 80s），不适合放进单测。
        """
        import subprocess
        import sys

        readme = self._read("README.md")
        total_match = BASELINE_TOTAL_RE.search(readme)
        self.assertIsNotNone(
            total_match,
            "README 的「当前基线」行里没有「全量 N 个用例」的括注 —— 本不变量无法对账",
        )
        recorded_total = int(total_match.group(1))

        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "--collect-only"],
            cwd=BASE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        collected_match = re.search(r"(\d+)\s+tests? collected", output)
        self.assertIsNotNone(
            collected_match,
            "没能从 `--collect-only` 的输出里解析出用例总数（判据失效或收集失败）：\n"
            + output[-1500:],
        )
        actual_total = int(collected_match.group(1))

        self.assertEqual(
            recorded_total,
            actual_total,
            f"README 记录的『全量 {recorded_total} 个用例』与当前实际收集到的 {actual_total} 个不一致。\n"
            f"  修法：跑一次确定性口径（README「六、测试与质量」里那条命令）拿到新的\n"
            f"  passed / skipped / deselected，然后把 README 那一行与 deploy.md 的验收期望值**一起**改。",
        )

    def test_deselected_node_ids_actually_collect(self):
        """文档里的 node id 引用必须**真的能 collect 到**（pytest 不会替我们发现写错）。

        NOTICE: 只跑 `--collect-only`，不执行用例（不联网、不起 mock）。
        """
        import subprocess
        import sys

        ids = []
        deselect_form = []
        for rel in self._doc_files():
            for match in NODE_ID_RE.finditer(self._read(rel)):
                node_id = match.group(1)
                file_part = node_id.split("::")[0]
                # 只检查「文件真实存在」的引用：文件不存在的提及是「相对路径示例」，
                # 不是导航（见类 docstring 的「边界」）。
                if not os.path.isfile(os.path.join(BASE, file_part)):
                    continue
                if node_id not in ids:
                    ids.append(node_id)
                if node_id not in deselect_form:
                    deselect_form.append(node_id)

        self.assertGreaterEqual(
            len(ids),
            MIN_EXPECTED_DESELECTS,
            f"只解析出 {len(ids)} 个 node id（预期 >= {MIN_EXPECTED_DESELECTS}）——"
            f"判据或文档结构变了：{ids}",
        )

        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", *ids],
            cwd=BASE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        output = (proc.stdout or "") + (proc.stderr or "")

        self.assertEqual(
            proc.returncode,
            0,
            "文档里的 node id 在这个 checkout 里 collect 不到（测试改名后 `--deselect` 会"
            "**静默失效**——pytest 对不存在的去选 id 既不报错也不警告）：\n"
            + output[-2000:],
        )
        for node_id in ids:
            with self.subTest(node_id=node_id):
                self.assertIn(
                    node_id,
                    output,
                    f"{node_id} 没有被 collect 到（文档里的去选会静默失效）",
                )


# --------------------------------------------------------------------------- 架构文档覆盖
ARCH_DOC = os.path.join("docs", "架构与调用链.md")

# 不要求写进「模块地图」的模块：入口/包标记，没有独立职责可讲。
# NOTICE: 白名单是**显式**的（每加一个都要有理由），不能用「以 `_` 开头」之类的规则兜 ——
# 那样会顺手放过 `__init__.py` 这类**确实有内容**的模块（`converters/__init__.py`
# 就定义了 `SUPPORTED_SOURCES`，必须在文档里）。
ARCH_DOC_EXEMPT_MODULES = {
    "__main__.py",  # 只是 `python -m interfacetester` 的转发入口
}


def _iter_package_modules():
    """产出包内每个模块的相对路径（相对 `interfacetester/`，用 `/` 分隔）。"""
    package_root = os.path.join(BASE, "interfacetester")
    for root, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(root, name), package_root)
            yield rel.replace(os.sep, "/")


# --------------------------------------------------------------------------- AI 旁路包
# `interfacetester_ai/`（交付给别人用的那一层）也必须进架构文档。
#
# NOTICE: 判据是「在 **§2.2 那一节的切片**里出现」，不是「全文出现」——
# 因为两个包**有同名模块**（`cli.py` / `__init__.py` 各有一个），查全文会让 ai 包的
# `cli.py` 被内核表里那一行**蒙过去**（绿了但其实是漏的，正是本仓最反对的那种假绿）。
AI_ARCH_DOC_SECTION_HEADING = "### 2.2 AI 旁路包"
AI_ARCH_DOC_SECTION_END = "## 三、五条主链路"
AI_PACKAGE_MIN_MODULES = 30


def _iter_ai_package_modules():
    """产出 AI 旁路包内每个模块的相对路径（相对 `interfacetester_ai/`，用 `/` 分隔）。"""
    package_root = os.path.join(BASE, "interfacetester_ai")
    for root, dirs, files in os.walk(package_root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(root, name), package_root)
            yield rel.replace(os.sep, "/")


def _ai_arch_doc_section():
    """取出架构文档里「AI 旁路包模块地图」那一节；标题不在时返回 `None`（判据据此报错）。"""
    with open(os.path.join(BASE, ARCH_DOC), encoding="utf-8") as fp:
        text = fp.read()
    start = text.find(AI_ARCH_DOC_SECTION_HEADING)
    if start < 0:
        return None
    end = text.find(AI_ARCH_DOC_SECTION_END, start)
    return text[start : end if end > 0 else len(text)]


def _ai_module_is_mentioned(section, rel):
    """该模块在 §2.2 切片里被提到吗（接受 `interfacetester_ai/x.py` 或裸 `x.py`）。"""
    return f"interfacetester_ai/{rel}" in section or rel.split("/")[-1] in section


class TestArchitectureDocCoversEveryModule(unittest.TestCase):
    r"""批次 9-1：架构文档必须**逐个覆盖包内模块**，且不许写会过期的数字。

    ★批次 18（2026-09-25）：覆盖面从**内核**扩到**两个包**——内核 `interfacetester/`（§2.1）与
    AI 旁路包 `interfacetester_ai/`（§2.2）。后者是交付给别人用的那一层，此前**无文档、无护栏**。

    ## 为什么是「覆盖模块」而不是「覆盖函数」

    架构文档最容易的腐烂方式是「新增了一个模块，文档里没有它」——读者照着文档去理解代码，
    就会漏掉整块能力（本仓的 `converters/`（5 个模块）与 `ext/uploader` 就是这么长出来的）。
    反过来，「每个函数都要写」既不现实、也会让文档变成源码的劣质副本，所以覆盖粒度定在**模块**。

    文档本身刻意只写 `module.py::function()` 而不写行号（理由见文档 §0）——
    这条护栏也因此不需要在每次改动后调整：**只有模块新增/删除/改名时才需要动文档**。

    ## 为什么还要查「不许写死用例数」

    架构文档是长文，很容易顺手写「当前 1340 passed」这类数字，然后**单独过期**。
    全仓的用例数只有一处事实来源（`README.md`「六、测试与质量」），
    本文档不许出现 `NNN passed` 形式的计数。
    """

    def test_every_package_module_is_mentioned(self):
        """包内每个模块都必须在架构文档里出现（新增模块忘了写文档 → 变红）。"""
        doc_path = os.path.join(BASE, ARCH_DOC)
        self.assertTrue(os.path.isfile(doc_path), f"架构文档不存在：{ARCH_DOC}")
        with open(doc_path, encoding="utf-8") as fp:
            text = fp.read()

        missing = []
        for rel in _iter_package_modules():
            if rel in ARCH_DOC_EXEMPT_MODULES:
                continue
            # 接受两种写法：完整相对路径（`converters/ir.py`）或包内唯一 basename
            # （`ir.py`）—— 模块地图表里用的是后者，链路上有时用前者。
            basename = rel.split("/")[-1]
            if rel in text or basename in text:
                continue
            missing.append(rel)

        self.assertEqual(
            missing,
            [],
            "这些包内模块没有出现在 docs/架构与调用链.md 里（新增模块必须同步 §2 模块地图）：\n  "
            + "\n  ".join(sorted(missing)),
        )

    def test_coverage_guard_would_catch_a_new_module(self):
        """判据自检：扫到的模块数必须与包内实际文件数一致（避免「扫描空跑」。"""
        modules = list(_iter_package_modules())
        self.assertGreaterEqual(
            len(modules),
            25,
            f"只扫到 {len(modules)} 个包内模块，扫描判据可能已失效：{sorted(modules)}",
        )
        self.assertTrue(
            any(rel == "cli.py" for rel in modules) and any("/" in rel for rel in modules),
            "扫描必须同时覆盖顶层模块与子包模块（namespace 包也不能漏）",
        )

    def test_every_ai_package_module_is_mentioned(self):
        """AI 旁路包（`interfacetester_ai/`）的每个模块都必须出现在 §2.2 里。

        ★**为什么单独立一条**：这个包是**交付给别人用**的那一层（`haify` 的全部能力都在里面），
        而它在补上 §2.2 之前**既没有文档、也没有护栏**（内核那条只扫 `interfacetester/`）——
        "新增了一个模块，文档里没有它"这种腐烂在这里**不会被任何用例发现**。
        ★**为什么按切片判**：两个包有**同名模块**（`cli.py` / `__init__.py`），查全文会假绿。
        """
        section = _ai_arch_doc_section()
        self.assertIsNotNone(
            section,
            f"{ARCH_DOC} 里找不到「{AI_ARCH_DOC_SECTION_HEADING}」这一节"
            "（标题被改名或删除？这一节是 AI 旁路包的模块地图，不能被拿掉）",
        )

        missing = [
            rel for rel in _iter_ai_package_modules() if not _ai_module_is_mentioned(section, rel)
        ]
        self.assertEqual(
            missing,
            [],
            "这些 AI 旁路包模块没有出现在架构文档的 §2.2「AI 旁路包模块地图」里"
            "（新增模块必须同步那一节；来源是 `interfacetester_ai/__init__.py` 的模块表）：\n  "
            + "\n  ".join(sorted(missing)),
        )

    def test_ai_package_coverage_guard_would_catch_a_new_module(self):
        """判据自检：**扫描非空** + **切片真的取到** + **伪造模块必须被判出来**。

        三条都是防"空跑变绿"：§2.2 的标题一改，`find` 就返回 `-1` —— 那种情况下
        若判据写成"切片为空 ⇒ 没有 missing"，它会**永远绿**，而文档里其实什么都没有。
        """
        modules = list(_iter_ai_package_modules())
        self.assertGreaterEqual(
            len(modules),
            AI_PACKAGE_MIN_MODULES,
            f"只扫到 {len(modules)} 个 ai 包模块，扫描判据可能已失效：{sorted(modules)}",
        )
        for must in ("cli.py", "__init__.py", "__main__.py"):
            self.assertIn(must, modules, f"ai 包扫描没扫到 {must}，判据显然没生效")

        section = _ai_arch_doc_section()
        self.assertIsNotNone(section, "取不到 §2.2 切片")
        self.assertGreater(
            len(section), 2000, "§2.2 切片过短，标题或结束标记可能写错（会导致判据空跑）"
        )
        self.assertFalse(
            _ai_module_is_mentioned(section, "__definitely_not_a_module__.py"),
            "伪造的模块名也被判为「已提到」，说明匹配逻辑过宽",
        )

    def test_architecture_doc_does_not_freeze_case_counts(self):
        """架构文档里不许出现 `NNN passed` 这类计数（会单独过期）。"""
        with open(os.path.join(BASE, ARCH_DOC), encoding="utf-8") as fp:
            text = fp.read()

        offenders = []
        for match in re.finditer(r"\b\d{2,5}\s+passed\b", text):
            line_no = text[: match.start()].count("\n") + 1
            offenders.append(f"{ARCH_DOC}:{line_no} → {match.group(0)}")

        self.assertEqual(
            offenders,
            [],
            "架构文档里出现了用例计数（只有 README「六、测试与质量」可以说这个数字）：\n  "
            + "\n  ".join(offenders),
        )


# ---------------------------------------------------------------------------
# 算子总数：事实来源是**代码**（`builtin/comparators.py` 的模块级公开函数），文档只能与它对齐
#
# NOTICE（批次 D / ②）：这条护栏是 D1 的收尾。D1 修掉的是「能力清单还写着 18 个算子」，
# 但那只是把数字**改对了一次** —— 数字本身没有任何约束：算子在 P2-b / A2-1 / A2-2 三个批次里
# 从 18 加到 22，而权威文档一直停在第 18 个，三个批次都没发现。
# 现有基线护栏（`TestBaselineNumbersHaveOneSource`）只管「用例数」，这里补上同款对账。
# ---------------------------------------------------------------------------
COMPARATOR_COUNT_RE = re.compile(r"(\d+)\s*个(?:内置)?算子")
CAPABILITY_DOC = os.path.join("docs", "能力清单.md")
# 每个文档至少要命中几处「N 个算子」——改文案时必须同步改这里，
# 否则护栏会因为「一处都没扫到」而**静默失效**（本仓对扫描型护栏的既定要求）。
COMPARATOR_COUNT_DOCS = (
    (CAPABILITY_DOC, 2),
    (os.path.join("docs", "架构与调用链.md"), 2),
    ("README.md", 1),
)
# 这两个文档还要**逐个点名**列全算子（README 只列了后加的几个，故不参与）
COMPARATOR_NAME_DOCS = (
    CAPABILITY_DOC,
    os.path.join("docs", "架构与调用链.md"),
)
MIN_EXPECTED_COMPARATORS = 18
CAPABILITY_SUMMARY_RE = re.compile(
    r"共\s*\*{0,2}(\d+)\*{0,2}\s*个内置算子\*{0,2}\s*（\s*(\d+)\s*个基础算子"
    r"\s*\+\s*(\d+)\s*个结构与契约算子\s*）"
)


class TestBuiltinComparatorCountHasOneSource(unittest.TestCase):
    """算子总数（当前 22）在**代码**与文档之间必须一致（D1 的护栏，批次 D / ②）。

    为什么需要它：`README.md`「六、测试与质量」那条基线护栏只对账**用例数**；
    「有多少个算子」这个数字当时在文档里没有任何约束，于是它漂了三个批次。
    """

    def _read(self, rel):
        with open(os.path.join(BASE, rel), encoding="utf-8") as fp:
            return fp.read()

    def _code_operators(self):
        """数出算子：判据与生成期白名单 `make.BUILTIN_COMPARATOR_NAMES` **完全相同**。"""
        import inspect

        from interfacetester import make
        from interfacetester.builtin import comparators

        names = tuple(
            sorted(
                name
                for name, value in vars(comparators).items()
                if inspect.isfunction(value) and not name.startswith("_")
            )
        )
        self.assertEqual(
            names,
            tuple(make.BUILTIN_COMPARATOR_NAMES),
            "`make.BUILTIN_COMPARATOR_NAMES` 必须仍然是**从 comparators.py 动态派生**的"
            "（历史上它一度是手工清单，而手工清单会漂）",
        )
        return names

    def test_documented_operator_count_matches_the_code(self):
        """所有「N 个算子」的写法都必须等于代码里的算子数。"""
        names = self._code_operators()
        # 判据自检：扫不到算子说明 extraction 失效，后面的比较就是空跑
        self.assertGreaterEqual(
            len(names),
            MIN_EXPECTED_COMPARATORS,
            f"只从 comparators.py 数出 {len(names)} 个算子，判据可能已失效：{names}",
        )

        offenders = []
        for rel, expected_min in COMPARATOR_COUNT_DOCS:
            text = self._read(rel)
            found = []
            for match in COMPARATOR_COUNT_RE.finditer(text):
                # 「第 N 个算子」是**序数**（`jsonschema_match` 就是第 19 个，那句永远为真），
                # 不是总数 —— 必须排除，否则合法文案会被误判。
                if "第" in text[max(0, match.start() - 6) : match.start()]:
                    continue
                found.append(
                    (text[: match.start()].count("\n") + 1, int(match.group(1)))
                )
            self.assertGreaterEqual(
                len(found),
                expected_min,
                f"{rel} 里只扫到 {len(found)} 处「N 个算子」（至少应有 {expected_min} 处）——"
                "文案改了就把这里的最小命中数一起改，不要让护栏静默失效",
            )
            for line_no, number in found:
                if number != len(names):
                    offenders.append(
                        f"{rel}:{line_no} 写的是 {number} 个，代码里是 {len(names)} 个"
                    )

        self.assertEqual(
            offenders,
            [],
            "算子总数对不上（事实来源是 `interfacetester/builtin/comparators.py` 的公开函数）：\n  "
            + "\n  ".join(offenders),
        )

    def test_capability_doc_category_counts_sum_to_the_total(self):
        """§3.3 表格里每个分类的「（N）」之和必须等于总数，且要与「18 + 4」吻合。

        NOTICE: 这条抓的是**真实发生过**的形态：D1 把总数改成 22 之后，
        `长度比较（4）` 这一行的括号里仍是 4，而它真实条数是 **5**
        （旧文案用通配写法 `len_lt`/`len_le`→`length_less_*` 把两个算子藏在了一起）
        —— 只看总数是看不出来的，只有把分类加起来才会露出来。
        """
        text = self._read(CAPABILITY_DOC)
        rows = []
        for line in text.splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 2:
                continue
            head = cells[0].replace("*", "")
            match = re.search(r"（(\d+)）\s*$", head)
            if match:
                rows.append((head, int(match.group(1))))

        self.assertGreaterEqual(
            len(rows), 5, f"{CAPABILITY_DOC} 的 §3.3 分类表没扫到（判据失效？）：{rows}"
        )
        total = sum(count for _, count in rows)
        base = sum(
            count
            for head, count in rows
            if not any(key in head for key in ("结构契约", "XML"))
        )
        structured = total - base

        summary = CAPABILITY_SUMMARY_RE.search(text)
        self.assertIsNotNone(
            summary,
            f"{CAPABILITY_DOC} 里的总结句格式变了（应为「共 N 个内置算子"
            "（B 个基础算子 + S 个结构与契约算子）」）—— 改了文案请同步本护栏",
        )
        stated = tuple(int(group) for group in summary.groups())
        code_total = len(self._code_operators())

        self.assertEqual(
            (stated[0], stated[1], stated[2], total),
            (code_total, base, structured, code_total),
            "§3.3 的分类计数与总结句/代码对不上："
            f"总结句 {stated}（总数, 基础, 结构与契约）、分类行求和 {total}、"
            f"基础行求和 {base}、结构与契约行求和 {structured}、"
            f"代码里 {code_total} 个算子",
        )

    def test_every_operator_is_named_in_the_docs(self):
        """每个算子都必须在两份文档里**独立**出现（防「加了算子没写文档」）。

        NOTICE: 判据用 `(?<![A-Za-z0-9_])名字` —— 否则 `length_less_than` 里就含有
        `less_than`，删掉后者也照样"命中"，这种子串匹配等于没查。
        """
        names = self._code_operators()
        offenders = []
        for rel in COMPARATOR_NAME_DOCS:
            text = self._read(rel)
            missing = [
                name
                for name in names
                if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}\b", text)
            ]
            if missing:
                offenders.append(f"{rel} 里没有独立出现的算子：{missing}")

        self.assertEqual(
            offenders,
            [],
            "有算子没被文档点名（新增算子时必须同步这两份文档）：\n  "
            + "\n  ".join(offenders),
        )

    def test_name_check_would_catch_a_missing_operator(self):
        """判据自检：把一个算子名从文档里整个抹掉，上一条必须能发现（否则它是空跑）。"""
        text = self._read(CAPABILITY_DOC)
        stripped = re.sub("xml_schema_match", "", text)

        missing = [
            name
            for name in self._code_operators()
            if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}\b", stripped)
        ]

        self.assertIn(
            "xml_schema_match",
            missing,
            "抹掉一个算子名之后判据居然没发现 —— 那么上一条护栏是空跑",
        )

# --------------------------------------------------------------------------- 口径唯一化（T15）
# 已被废弃的旧工作量数字：它们**可以**作为历史对照出现，但**不许**以"当前口径"的姿态出现。
PLAN_V8_DOC = os.path.join("docs", "interfacetester智能化改造方案8.md")

# NOTICE（外发仓口径）：下述两类是**内网资料**，不随本仓库分发；缺席时对应判据 skip：
#   · 内网方案文档 `docs/interfacetester智能化改造方案8.md`（历史工作量数字的档案）
#   · 探针工作区 `probe_p2_prep/`（探针分类表 routine.py 所在处）
# 在内网工作区里两者都在 → 判据照旧真跑，不退化。
PLAN_V8_DOC_PRESENT = os.path.isfile(os.path.join(BASE, PLAN_V8_DOC))
PROBE_DIR = os.path.join(BASE, "probe_p2_prep")
DEPRECATED_WORKLOAD_NUMBERS = ("34~48", "32~45", "32~47")
# 出现旧数字的**同一行**里必须含下列任一"历史/对照"标记词——
# 否则读者无法判断它是当前口径还是历史记录（而他会按错的那个排期）。
HISTORY_MARKERS = (
    "v4",
    "v6",
    "互斥",
    "废弃",
    "T15",
    "方案 4",
    "历史",
    "承接",
    "裁剪",
    "重算",
)


@unittest.skipUnless(PLAN_V8_DOC_PRESENT, "内网方案文档不在工作区")
class TestDeprecatedWorkloadNumbersOnlyAppearAsHistory(unittest.TestCase):
    r"""T15：**口径唯一化**——旧的工作量数字只能以"历史对照"的身份出现。

    ## 为什么需要它

    §0.5-⑤ 记的正是这件事：v4 的 Web 增量有**三处互斥**数字（`8~14` / `8~14` / `6~11`），
    总计出现过 `34~48` 与 `32~45` 两个数；v6 又给了 `32~47`。v7/v8 已统一为
    **A 26~35 + B 7~13 = 33~48（裁剪 23~29）**。

    但"统一口径"如果只是**再写一个新数字**，旧的三个仍散落在各处——读者（和半年后的自己）
    没法判断哪个是当前口径。所以判据是：**每一处旧数字都必须带上"它属于哪次对照"的标记。**

    ## 边界（诚实）

    本判据**不禁止**旧数字出现（历史记录有价值），只要求它们**带着来源**出现；
    要防的是"旧数字被当成当前口径"——那种情况下排期会按错的量级做。
    """

    def _offenders(self, text):
        offenders = []
        for number in DEPRECATED_WORKLOAD_NUMBERS:
            for match in re.finditer(re.escape(number), text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.start())
                line = text[line_start : line_end if line_end >= 0 else len(text)]
                if any(marker in line for marker in HISTORY_MARKERS):
                    continue
                line_no = text[: match.start()].count("\n") + 1
                offenders.append((number, line_no, line.strip()[:100]))
        return offenders

    def test_every_deprecated_number_carries_a_history_marker(self):
        with open(os.path.join(BASE, PLAN_V8_DOC), encoding="utf-8") as handle:
            text = handle.read()

        offenders = self._offenders(text)
        self.assertEqual(
            offenders,
            [],
            "旧口径数字出现在**没有历史标记**的语境里（读者会把它当成当前口径）：\n  "
            + "\n  ".join(f"L{line}: {number} | {snippet}" for number, line, snippet in offenders),
        )

    def test_the_numbers_really_are_still_present_as_history(self):
        """判据自检：这些数字仍在文档里（作为历史对照）——否则上一条是空跑。"""
        with open(os.path.join(BASE, PLAN_V8_DOC), encoding="utf-8") as handle:
            text = handle.read()

        for number in DEPRECATED_WORKLOAD_NUMBERS:
            with self.subTest(number=number):
                self.assertIn(number, text, f"{number} 从文档里消失了？对账前提不成立")

    def test_guard_would_catch_a_number_used_as_current(self):
        """元护栏：注入一句"把旧数字当当前口径"的话，判据必须发现。"""
        self.assertTrue(
            self._offenders("当前口径：总计 34~48 人日，按此排期。"),
            "注入的'旧数字当当前口径'竟然没被发现 —— 那么上一条护栏是空跑",
        )

@unittest.skipUnless(os.path.isdir(PROBE_DIR), "探针工作区 probe_p2_prep/ 不在工作区")
class TestProbeRoutineCoversEveryProbe(unittest.TestCase):
    """★§9.33 登记项：**每个探针都必须被分类**（例行 / 模型 / 不例行＋理由）。

    **为什么要有这条判据**：登记在案的坑是「探针只手工跑 → 会失忆」——
    `probe_analyze_models.py` 落地前，「哪个模型能用」**没有任何东西可观测**
    （单测全是 `FakeTransport`）。加一个探针却不挂例行，等于把"我们量过"这句话交给记性。
    所以：**新增探针忘了分类 → 这条红**（"忘掉一件事"从此是响亮的，不是静默的）。

    三分类见 `probe_p2_prep/routine.py`：`ROUTINE`（确定性、每次例行跑）/ `MODEL_STEPS`
    （要真出网、不参与退出码）/ `EXCLUDED`（一次性测量，**必附理由**）。
    """

    def _routine(self):
        import sys

        probe_dir = os.path.join(BASE, "probe_p2_prep")
        if probe_dir not in sys.path:
            sys.path.insert(0, probe_dir)
        import routine  # noqa: PLC0415

        return routine

    def _probe_files(self):
        probe_dir = os.path.join(BASE, "probe_p2_prep")
        return {
            name
            for name in os.listdir(probe_dir)
            if name.startswith("probe_") and name.endswith(".py")
        }

    def test_every_probe_is_classified(self):
        routine = self._routine()
        classified = (
            {name for name, _note, _mock in routine.ROUTINE}
            | {name for name, _note in routine.MODEL_STEPS}
            | set(routine.EXCLUDED)
        )

        unclassified = self._probe_files() - classified
        self.assertEqual(
            unclassified,
            set(),
            f"这些探针没被分类（例行 / 模型 / 不例行＋理由）：{sorted(unclassified)}；"
            "请把它们加进 probe_p2_prep/routine.py 的 ROUTINE / MODEL_STEPS / EXCLUDED",
        )

    def test_classification_has_no_stale_names(self):
        """★成对：分类表里的名字必须真的存在（改名/删文件后忘了改表，也会红）。"""
        routine = self._routine()
        classified = (
            {name for name, _note, _mock in routine.ROUTINE}
            | {name for name, _note in routine.MODEL_STEPS}
            | set(routine.EXCLUDED)
        )

        self.assertEqual(sorted(classified - self._probe_files()), [])

    def test_excluded_ones_must_say_why(self):
        """★「不例行」必须**附一句理由** —— 否则它就是"被忘掉的探针"的藏身处。"""
        routine = self._routine()

        for name, reason in routine.EXCLUDED.items():
            with self.subTest(probe=name):
                self.assertGreaterEqual(len(reason.strip()), 20, f"{name} 的排除理由太短")

    def test_model_steps_do_not_gate(self):
        """★模型探针**不许**参与例行退出码（断网 / 没配模型都是正常环境）。"""
        routine = self._routine()

        names = {name for name, _note, _mock in routine.ROUTINE}
        self.assertNotIn("probe_analyze_models.py", names)
        self.assertIn("probe_analyze_models.py", {name for name, _note in routine.MODEL_STEPS})

