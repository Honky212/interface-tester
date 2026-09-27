# -*- coding: utf-8 -*-
"""`pyproject.toml` ↔ `requirements.txt` 一致性闸门（0919-2 §三「建议优先」项）。

## 为什么需要这条闸门

`docs/存在的问题0919-2.txt` §三 把它列为「建议优先」，理由不是它复杂，而是**时机**：

> 本批刚同时改过这两个文件，正是漂移高危期，且这是本仓一贯做成闸门的东西。

两个文件的分工是**刻意**的，不是冗余：

| 文件 | 角色 | 形态 |
|---|---|---|
| `pyproject.toml` | **唯一事实来源**：pip 装主依赖时读它 | 声明**区间**（`pydantic>=2.0,<3`） |
| `requirements.txt` | 离线/复现部署的**锁版本**清单（`pip download -r`） | 钉死 `==`（`pydantic==2.13.5`） |

分工决定了三条不变量（每一条都对应一种**真实且静默**的失效）：

- **S1 名字双向对齐**：一边加了依赖另一边不知道 —— 往 `pyproject` 加 → 离线部署装不到
  （`ModuleNotFoundError`，还算响亮）；往 `requirements` 加 → 锁清单里躺着一个框架根本不用的包，
  离线机上白装，**永远没有任何信号**。
- **S2 锁定值必须落在声明区间内**：`pyproject` 把区间收紧（例如 `urllib3` 加 `<3`）而
  `requirements.txt` 没跟着改 → `pip install -e .` 用区间、离线部署用锁定值，**两条链路装出不同版本**。
  这正是 0919-2 那批动过的地方（`python-dotenv` 就是本轮新加的，还带一条跨解释器 NOTICE）。
- **S3 可选 extras 必须在锁清单注释区有对应**：离线用户只看得到 `requirements.txt`；
  注释区是 extras 的**唯一**离线可见面。新增 extra 却忘了登记 → 内网用户不知道有这个能力。

## 刻意的边界（都是为了不产生假报）

- **`dev` extra 不进 `requirements.txt`**：`setuptools`/`wheel`/`build` 是**打包工具**，
  不是运行期依赖，离线部署机上不需要。这条写在 `DEV_ONLY_EXTRAS` 里**显式**豁免——
  不写的话第一次跑就假报，而闸门一旦有假报就会被绕过，等于没有。
- **只比名字与版本口径**：不校验哈希（本仓没有 `--require-hashes`）、不访问网络、
  不比对传递依赖（传递依赖由 `pip download` 自行解析，本仓无从声明，写死只会随上游漂移）。
- **不要求 `pyproject` 也钉死版本**：区间是它的**职责**。反过来还要求
  `requirements.txt` 的每一行必须是 `==` 锁定形态（S4）——它是锁清单，
  写成 `>=` 会让离线部署变得不可复现，属于「文件角色被破坏」。

## 自检（非空跑）

`TestDriftDetectorsCatchSyntheticDrift` 用**合成的**两文件内容驱动同一套判据函数，
确保 S1/S2/S3/S4 四条判据都能真的报出漂移。没有这条，「一直绿」可能只是判据写错、
永远返回空列表（本仓 `static_name_resolution_test.py` / `docs_consistency_test.py`
都用了同一个手法）。

## 依赖

判据用 `packaging`（解析 requirement 与版本区间）。它是 `pytest` 的**直接**依赖
（`pytest 9.x` 的 `Requires-Dist` 里有 `packaging`），所以跑本文件时必然可导入；
不额外为本测试新增依赖，也不把它算进 `pyproject` 的运行期依赖。
"""

import os
import re
import subprocess
import unittest
from typing import Dict, List, NamedTuple, Optional, Sequence

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.8 ~ 3.10
    tomllib = None

try:  # `toml` 是本仓的运行期依赖（见 pyproject），3.8~3.10 上用它兜底
    import toml as _toml
except ModuleNotFoundError:  # pragma: no cover
    _toml = None


BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PYPROJECT_PATH = os.path.join(BASE, "pyproject.toml")
REQUIREMENTS_PATH = os.path.join(BASE, "requirements.txt")

# `dev` extra 是打包工具，**刻意**不登记进离线运行期锁清单（见模块 docstring）。
# 若哪天从 pyproject 里删掉 `dev`，本表会变成"过期豁免"并被 test_stale_allowlist_is_reported 抓住。
DEV_ONLY_EXTRAS = frozenset({"dev"})

# 护栏：解析出来的条目低于这个数，说明判据被"解析器静默跳过"削弱了（空跑）。
MIN_EXPECTED_MAIN_DEPS = 10
MIN_EXPECTED_EXTRAS = 5
MIN_EXPECTED_COMMENTED_EXTRAS = 8


def normalize_name(name: str) -> str:
    """PEP 503 名字归一化：`PyYAML` / `pyyaml` / `py_yaml` 视为同一个包。"""
    return name.strip().lower().replace("_", "-").replace(".", "-")


class ParsedRequirement(NamedTuple):
    """一条 requirement + 它在文件里的出处（报错信息要能点回行号）。"""

    requirement: Requirement
    line_no: int
    raw: str

    @property
    def name(self) -> str:
        return self.requirement.name

    @property
    def normalized_name(self) -> str:
        return normalize_name(self.requirement.name)

    @property
    def specifier(self) -> SpecifierSet:
        return self.requirement.specifier

    @property
    def extras(self):
        return self.requirement.extras


def pinned_version(entry: ParsedRequirement) -> Optional[str]:
    """`==` 锁定形态下的版本号；不是单一 `==` 则返回 None（那是形态问题，由 S4 负责）。"""
    specifiers = list(entry.specifier)
    if len(specifiers) == 1 and specifiers[0].operator == "==":
        return specifiers[0].version
    return None


def load_pyproject(path: str = PYPROJECT_PATH) -> Dict:
    """读 `pyproject.toml`（`tomllib` 优先，3.8~3.10 用本仓依赖的 `toml` 兜底）。"""
    with open(path, "rb") as f:
        raw = f.read().decode("utf-8")

    if tomllib is not None:
        return tomllib.loads(raw)

    if _toml is not None:  # pragma: no cover - CI 只跑 3.12
        return _toml.loads(raw)

    raise AssertionError(  # pragma: no cover
        "既没有 tomllib（Python>=3.11）也没有 toml（pyproject 的运行期依赖），无法校验"
    )


def parse_requirement_lines(text: str, commented: bool) -> List[ParsedRequirement]:
    """从 `requirements.txt` 文本里取主清单（`commented=False`）或注释区（`commented=True`）。

    NOTICE：判据是「**能被 `packaging` 解析成 requirement**」，而不是自写正则。
    理由：这个文件里有大量中文说明与命令行片段（`#   1) 有网络的环境下载 wheel：`、
    `#       pip install -e .  会自动安装。`），自写正则要把它们一条条排除，
    规则只会越来越长且仍然会漏；而它们**全都**不是合法 requirement
    （实测 `Requirement("pip install -e .")` 抛 `InvalidRequirement`），天然被滤掉。

    代价要记住：某一行若被写坏成非法 requirement，这里会**静默跳过**它。
    所以这条不能单独用作判据——它上面还有 MIN_EXPECTED_* 护栏与 S1 的双向差集兜着
    （漏掉一条主依赖，`pyproject` 那一侧的差集立刻报出来）。
    """
    entries: List[ParsedRequirement] = []

    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue

        is_comment = line.startswith("#")
        if is_comment != commented:
            continue

        if is_comment:
            # 去掉一层 `#`，再切掉行尾的行内注释（例如 `# allure-pytest>=2.8.16  # allure 报告`）
            line = line[1:].strip()
        line = line.split("#", 1)[0].strip()
        if not line:
            continue

        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            # 说明性文字 / 命令片段 / 分隔线——不是 requirement，跳过（见上面的 NOTICE）
            continue

        entries.append(ParsedRequirement(requirement, line_no, raw_line.strip()))

    return entries


def parse_declared(dependencies: Sequence[str]) -> Dict[str, Requirement]:
    """把 pyproject 的依赖字符串列表解析成 `归一化名字 -> Requirement`。"""
    declared: Dict[str, Requirement] = {}
    for text in dependencies:
        requirement = Requirement(text)
        declared[normalize_name(requirement.name)] = requirement
    return declared


# --------------------------------------------------------------------------- 四条判据
# 判据函数一律「输入解析结果、输出问题清单」，**不碰文件**——
# 这样自检才能用合成数据驱动它们（见 TestDriftDetectorsCatchSyntheticDrift）。


def main_name_drift(
    declared: Dict[str, Requirement], entries: Sequence[ParsedRequirement]
) -> List[str]:
    """S1：`[project.dependencies]` 与 `requirements.txt` 主清单的名字必须双向对齐。"""
    declared_names = set(declared)
    locked_names = {entry.normalized_name for entry in entries}

    problems = []
    for name in sorted(declared_names - locked_names):
        problems.append(
            f"{name}: 在 pyproject `[project.dependencies]` 里声明"
            f"（{declared[name].specifier}），但 requirements.txt 主清单里没有 → "
            f"离线部署会缺这个包"
        )
    for name in sorted(locked_names - declared_names):
        problems.append(
            f"{name}: 在 requirements.txt 主清单里锁定，但 pyproject 里没有声明 → "
            f"锁清单里躺着一个框架不用的包（白装，且无任何信号）"
        )
    return problems


def lock_form_drift(entries: Sequence[ParsedRequirement]) -> List[str]:
    """S4：`requirements.txt` 主清单的每一行都必须是 `==` 锁定形态。"""
    problems = []
    for entry in entries:
        specifiers = list(entry.specifier)
        if len(specifiers) == 1 and specifiers[0].operator == "==":
            continue
        problems.append(
            f"第 {entry.line_no} 行 {entry.raw!r}：不是单一 `==` 锁定形态 → "
            f"本文件是离线/复现部署的锁清单，写成区间会让部署结果随上游漂移"
        )
    return problems


def pin_range_drift(
    declared: Dict[str, Requirement], entries: Sequence[ParsedRequirement]
) -> List[str]:
    """S2：`requirements.txt` 的 `==` 锁定值必须落在 `pyproject` 声明的区间内。"""
    problems = []
    for entry in entries:
        requirement = declared.get(entry.normalized_name)
        if requirement is None:
            continue  # 不在 pyproject 里 → 由 S1 负责点名，这里不重复报
        if not requirement.specifier:
            continue  # pyproject 未声明区间 → 任何版本都合法，无可校验

        version = pinned_version(entry)
        if version is None:
            continue  # 形态问题 → 由 S4 负责点名

        try:
            parsed = Version(version)
        except InvalidVersion:  # pragma: no cover - 非法版本号
            problems.append(
                f"第 {entry.line_no} 行 {entry.raw!r}：版本号 {version!r} 无法解析"
            )
            continue

        # NOTICE: `prereleases=True` 是刻意的。这里校验的是「区间算术语义」
        # （锁定值是否被声明的上下限允许），不是 pip 的预发布版选取策略——
        # 用默认的 `prereleases=None` 会让 `foo==2.0.0rc1` 这类锁定值**假报**，
        # 而闸门的头号死因就是假报。
        if not requirement.specifier.contains(parsed, prereleases=True):
            problems.append(
                f"{entry.normalized_name}: requirements.txt 锁定 {version}"
                f"（第 {entry.line_no} 行），但 pyproject 声明的是 "
                f"{requirement.specifier} → 两条安装链路会装出不同版本"
            )
    return problems


def extras_listing_drift(
    extras: Dict[str, Sequence[str]],
    commented_entries: Sequence[ParsedRequirement],
    allowlist=frozenset(),
) -> List[str]:
    """S3：`[project.optional-dependencies]` 的每项都必须在锁清单注释区有一份对应。"""
    listed = {entry.normalized_name for entry in commented_entries}
    declared_names = set()
    problems = []

    for extra_name, dependencies in extras.items():
        if extra_name in allowlist:
            continue
        for text in dependencies:
            name = normalize_name(Requirement(text).name)
            declared_names.add(name)
            if name not in listed:
                problems.append(
                    f"{name}: 属于 extra `{extra_name}`，但 requirements.txt 的"
                    f"「可选扩展」注释区里没有 → 离线用户看不到有这个能力"
                )

    for name in sorted(listed - declared_names):
        problems.append(
            f"{name}: 出现在 requirements.txt 的「可选扩展」注释区，"
            f"但 pyproject 的任何 extra 里都没有 → 注释区留着一个已删除的 extra"
        )

    return problems


def stale_allowlist(
    extras: Dict[str, Sequence[str]], allowlist=frozenset()
) -> List[str]:
    """豁免表本身也会过期：豁免了一个已经不存在的 extra 就该报出来。"""
    return [
        f"{name}: 在豁免表里，但 pyproject 里已经没有这个 extra → 请从 DEV_ONLY_EXTRAS 移除"
        for name in sorted(allowlist - set(extras))
    ]


# --------------------------------------------------------------------------- 文件级装配
def _real_data():
    """读真实的两文件并解析（每条判据都用它，避免同一份解析逻辑抄四遍）。"""
    pyproject = load_pyproject()
    project = pyproject["project"]

    with open(REQUIREMENTS_PATH, encoding="utf-8") as f:
        requirements_text = f.read()

    return {
        "declared": parse_declared(project["dependencies"]),
        "extras": project.get("optional-dependencies", {}),
        "main_entries": parse_requirement_lines(requirements_text, commented=False),
        "commented_entries": parse_requirement_lines(requirements_text, commented=True),
    }


class TestRequirementsCoversDeclaredDependencies(unittest.TestCase):
    """S1：名字必须双向对齐（`tests/dependency_consistency_test.py` 模块 docstring）。"""

    def test_declared_and_locked_names_match_both_ways(self):
        data = _real_data()

        # 护栏：解析结果太少说明判据在空跑（解析器静默跳过的问题由这里兜底）
        self.assertGreaterEqual(
            len(data["declared"]),
            MIN_EXPECTED_MAIN_DEPS,
            "从 pyproject 解析到的运行期依赖太少，判据可能没生效",
        )
        self.assertGreaterEqual(
            len(data["main_entries"]),
            MIN_EXPECTED_MAIN_DEPS,
            "从 requirements.txt 解析到的主依赖太少，判据可能没生效",
        )

        problems = main_name_drift(data["declared"], data["main_entries"])
        self.assertEqual(
            problems,
            [],
            "`pyproject.toml` 与 `requirements.txt` 的运行期依赖名字不一致"
            "（两个文件的分工见本文件模块 docstring）：\n  " + "\n  ".join(problems),
        )

    def test_no_duplicate_names(self):
        """同名重复出现在两个文件里都是漂移源（后一条会静默覆盖前一条的语义）。"""
        data = _real_data()
        problems = []

        for label, entries in (
            ("requirements.txt 主清单", data["main_entries"]),
            ("requirements.txt 可选扩展注释区", data["commented_entries"]),
        ):
            seen: Dict[str, int] = {}
            for entry in entries:
                if entry.normalized_name in seen:
                    problems.append(
                        f"{label} 第 {seen[entry.normalized_name]} 行与第 "
                        f"{entry.line_no} 行重复声明 {entry.normalized_name}"
                    )
                seen.setdefault(entry.normalized_name, entry.line_no)

        self.assertEqual(problems, [], "\n  ".join(problems))


class TestLockedVersionsSatisfyDeclaredRanges(unittest.TestCase):
    """S2：锁定的版本必须落在声明的区间内。"""

    def test_each_pin_satisfies_the_pyproject_specifier(self):
        data = _real_data()
        problems = pin_range_drift(data["declared"], data["main_entries"])
        self.assertEqual(
            problems,
            [],
            "`requirements.txt` 的锁定值与 `pyproject.toml` 声明的区间冲突"
            "（两条安装链路会装出不同版本）：\n  " + "\n  ".join(problems),
        )


class TestRequirementsIsALockFile(unittest.TestCase):
    """S4：`requirements.txt` 必须是 `==` 锁定形态。"""

    def test_every_main_requirement_is_exactly_pinned(self):
        data = _real_data()
        problems = lock_form_drift(data["main_entries"])
        self.assertEqual(
            problems,
            [],
            "`requirements.txt` 是离线/复现部署的锁清单，主清单每一行都要 `==` 钉死"
            "（区间请写在 pyproject.toml 里）：\n  " + "\n  ".join(problems),
        )


class TestOptionalExtrasAreDiscoverableOffline(unittest.TestCase):
    """S3：extras 必须在锁清单注释区可见（`dev` 豁免，理由见模块 docstring）。"""

    def test_every_extra_is_listed_in_requirements_comments(self):
        data = _real_data()

        self.assertGreaterEqual(
            len(data["extras"]),
            MIN_EXPECTED_EXTRAS,
            "从 pyproject 解析到的 extra 太少，判据可能没生效",
        )
        self.assertGreaterEqual(
            len(data["commented_entries"]),
            MIN_EXPECTED_COMMENTED_EXTRAS,
            "从 requirements.txt 注释区解析到的可选项太少，判据可能没生效"
            "（先确认「可选扩展」注释区还在、且仍然是 requirement 形态）",
        )

        problems = extras_listing_drift(
            data["extras"], data["commented_entries"], DEV_ONLY_EXTRAS
        )
        self.assertEqual(
            problems,
            [],
            "`pyproject.toml` 的 extras 与 `requirements.txt` 的「可选扩展」注释区不一致：\n  "
            + "\n  ".join(problems),
        )

    def test_dev_only_allowlist_is_not_stale(self):
        """豁免表不得留着一个已经不存在的 extra（豁免过期 = 掩盖真漂移）。"""
        data = _real_data()
        problems = stale_allowlist(data["extras"], DEV_ONLY_EXTRAS)
        self.assertEqual(problems, [], "\n  ".join(problems))


class TestLicenseFilesShipTheNotice(unittest.TestCase):
    r"""S6：`NOTICE` 必须**随包分发**，且必须被 git 跟踪（Apache-2.0 第 4(d) 条）。

    ## 修复前的现场

    本包是 HttpRunner 4.3.5 的衍生作品，`NOTICE` 里写明来源与商标约束
    （README「七、许可与来源」也要求对外分发时注明）。而修复前：

    - `pyproject.toml` 写的是 `license-files = ["LICENSE"]` —— **没有 NOTICE**；
    - `NOTICE` 连 **git 都没跟踪**（`git ls-files NOTICE` 为空）。

    两条叠加的后果是：**克隆仓库与安装 wheel 这两条最需要它的路径，同时拿不到它**。
    单测全绿 —— 因为没有任何护栏看这件事（`wheel_content_test.py` 只查 `.py` 与入口点，
    且默认 skip）。

    ## 判据为什么刻意用「双向」而不是只查 pyproject

    只查 `license-files` 会漏掉「文件本身没入库」这一半：`license-files` 里写了名字，
    但仓库里根本没有这个文件（或它没被 git 跟踪 → 别人 clone 不到），
    `python -m build` 在本地却可能因为文件存在而成功 —— 于是**本地绿、交付缺**。
    """

    def test_license_files_declares_notice(self):
        with open(PYPROJECT_PATH, encoding="utf-8") as f:
            pyproject = f.read()

        match = re.search(r"^license-files\s*=\s*\[(.*?)\]", pyproject, re.M | re.S)
        self.assertIsNotNone(
            match,
            "pyproject.toml 里没有 license-files 声明（PEP 639）—— 本判据无法对账",
        )
        declared = {
            item.strip().strip("\"'") for item in match.group(1).split(",") if item.strip()
        }

        self.assertIn(
            "LICENSE",
            declared,
            f"license-files 丢了 LICENSE：{sorted(declared)}",
        )
        self.assertIn(
            "NOTICE",
            declared,
            "license-files 里没有 NOTICE —— 本包是 HttpRunner 的衍生作品，"
            "`NOTICE` 里写明来源与商标约束（Apache-2.0 第 4(d) 条要求随附），"
            f"分发时必须带上。当前声明：{sorted(declared)}",
        )

    def test_declared_license_files_actually_exist_and_are_tracked(self):
        """声明了还不够：文件必须真的在，**且被 git 跟踪**（否则别人 clone 不到）。

        NOTICE: 这里用 `git ls-files` 而不是 `os.path.exists` —— 后者在开发者本机
        永远为真（文件就在那儿），而交付缺的恰恰是「它没入库」这一半。
        git 不可用（例如从 sdist 解包出来的环境）时本用例 skip，不产生假失败。
        """
        with open(PYPROJECT_PATH, encoding="utf-8") as f:
            pyproject = f.read()
        match = re.search(r"^license-files\s*=\s*\[(.*?)\]", pyproject, re.M | re.S)
        if match is None:
            self.skipTest("pyproject.toml 里没有 license-files 声明（由上一条用例负责报）")
        declared = [
            item.strip().strip("\"'") for item in match.group(1).split(",") if item.strip()
        ]

        try:
            proc = subprocess.run(
                ["git", "ls-files", *declared],
                cwd=BASE,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            self.skipTest("本环境没有 git 可执行文件")

        if proc.returncode != 0:
            self.skipTest(f"git ls-files 不可用（{proc.stderr.strip()[:120]}）")

        tracked = {
            os.path.basename(line.strip())
            for line in (proc.stdout or "").splitlines()
            if line.strip()
        }

        # 文件确实存在（本机视角）
        missing_on_disk = [name for name in declared if not os.path.isfile(os.path.join(BASE, name))]
        self.assertEqual(
            missing_on_disk,
            [],
            f"license-files 声明了但磁盘上没有这些文件：{missing_on_disk}（构建会直接失败）",
        )

        # 且确实被 git 跟踪（交付视角）
        untracked = [name for name in declared if name not in tracked]
        self.assertEqual(
            untracked,
            [],
            f"license-files 声明了但**没有被 git 跟踪**：{untracked}\n"
            f"  后果：别人克隆仓库拿不到这些文件（本机 `python -m build` 却可能照常成功）→ "
            f"本地绿、交付缺。修法：`git add {' '.join(untracked)}`",
        )


class TestCiJobDependencySetsCoverTheTests(unittest.TestCase):
    """S5（0920 批次 3 / **N13**）：CI 的依赖集必须装得上"要有它才能跑"的 extra。

    ## 修复前的现场（`.tmp_audit/n13_block_plugin.py`，模拟 `.[dev,xml]`）

    `unit-test` 是文档里写明"**必须绿**"的 job，而它的安装行是 `pip install -e ".[dev,xml]"`。
    于是跑 `tests/uploader_test.py` 时：

    ```text
    NameError: name 'filetype' is not defined
    interfacetester/ext/uploader/__init__.py:386  (get_filetype)
    ... 16 failed
    ```

    两件事叠在一起才让它**没被发现**：

    1. `ext/uploader/__init__.py` 的 `except ModuleNotFoundError` 只把 `UPLOAD_READY` 置 False，
       **没有绑定** `filetype` 这个名字 → 缺依赖时抛的是与依赖毫无关系的 `NameError`
       （代码侧已修：绑成 None 并走可读降级）；
    2. uploader 的用例把 `UPLOAD_READY` mock 成 `True`（这是它们**该做的事**——
       要测真实编码器），于是 `ensure_upload_ready()` 那句清晰的
       "请装 `interfacetester[upload]`" **永远不会执行**。

    也就是说："必须绿"的 job 一直红着 → 等于**没有红灯信号**。
    本用例把"job 依赖集 ⊇ 测试需要的 extra"钉成不变量。
    """

    # job 名 → 「它负责的测试文件需要哪些 extra」
    REQUIRED_EXTRAS = {
        "unit-test": {"xml", "upload"},
        "examples-smoke": {"xml", "upload"},
    }

    def _workflow_texts(self) -> Dict[str, str]:
        base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        paths = [
            os.path.join(base, ".github", "workflows", "test.yml"),
            os.path.join(base, ".gitlab-ci.yml"),
        ]
        texts = {}
        for path in paths:
            with open(path, encoding="utf-8") as f:
                texts[os.path.basename(path)] = f.read()
        return texts

    @staticmethod
    def _job_section(text: str, job: str) -> Optional[str]:
        """取出某个 job 的段落（到下一个同级 job 定义为止）。

        两种 CI 文件的缩进不同（GitHub Actions 是 2 空格、GitLab 是 0 缩进），
        所以按"行首缩进 + `名字:`"匹配，并切到**下一个同级或更外层**的键为止。
        """
        lines = text.splitlines()
        start = None
        indent = ""
        for index, line in enumerate(lines):
            if line.strip() != f"{job}:":
                continue
            start = index
            indent = line[: len(line) - len(line.lstrip())]
            break
        if start is None:
            return None

        section = [lines[start]]
        for line in lines[start + 1 :]:
            if not line.strip():
                section.append(line)
                continue
            current_indent = line[: len(line) - len(line.lstrip())]
            # 回到同级或更外层 → 这个 job 段落结束
            if len(current_indent) <= len(indent):
                break
            section.append(line)
        return "\n".join(section)

    def test_every_ci_install_line_for_these_jobs_includes_required_extras(self):
        """`unit-test` / `examples-smoke` 的安装行必须带上它们要用的 extra。"""
        for file_name, text in self._workflow_texts().items():
            for job, extras in self.REQUIRED_EXTRAS.items():
                section = self._job_section(text, job)
                self.assertIsNotNone(
                    section, f"{file_name} 里找不到 job {job!r}（判据失效）"
                )

                install_lines = re.findall(
                    r'pip install -e "\.\[([^\]]*)\]"', section
                )
                self.assertTrue(
                    install_lines,
                    f"{file_name} 的 {job} job 里没找到 `pip install -e \".[...]\"`"
                    f"（判据失效，别让它假通过）",
                )
                for line in install_lines:
                    declared = {part.strip() for part in line.split(",") if part.strip()}
                    missing = extras - declared
                    self.assertEqual(
                        missing,
                        set(),
                        f"{file_name} 的 {job} job 装的是 `.[{line}]`，"
                        f"缺 {sorted(missing)} —— 该 job 会因此失败或静默跳过测试（N13）",
                    )

    def test_uploader_tests_survive_without_the_upload_extra(self):
        """反向保证：缺 `filetype` 时**不许**退化成 `NameError`。

        直接构造"依赖缺失"的状态（把模块级 `filetype` 置 None —— 这正是
        `except ModuleNotFoundError` 分支现在的样子），断言类型识别走既有兜底，
        而不是抛 `NameError`。
        """
        from interfacetester.ext import uploader

        original_ready = uploader.UPLOAD_READY
        original_filetype = uploader.filetype
        try:
            uploader.UPLOAD_READY = True  # 用例的常规做法（mock 掉依赖守卫）
            uploader.filetype = None
            mime = uploader._build_multipart_encoder(
                {"f": ("a.txt", "CONTENT", "text/plain")}
            ).content_type
        finally:
            uploader.UPLOAD_READY = original_ready
            uploader.filetype = original_filetype

        self.assertTrue(
            mime.startswith("multipart/form-data"),
            f"缺 filetype 时没能走通编码器（返回 {mime!r}）",
        )

    def test_requests_toolbelt_is_not_a_hard_prerequisite(self):
        """0920 批次 6 / **N37**：`requests_toolbelt` **不是**硬前置。

        它在整个包里**从未被使用**（L14 的流式改写后用的是自研
        `Utf8MultipartEncoder`；全文搜索只剩注释）。

        修复前 `import filetype` 与 `from requests_toolbelt import MultipartEncoder`
        写在**同一个 try** 里，任一失败就 `UPLOAD_READY = False` —— 于是
        "filetype 装了、编码器完全可用"，却只因没装一个用不到的包，
        整个上传功能直接 `RuntimeError`（实测 `.tmp_audit/n37_verify2.py`）。

        这里用**模块级属性**断言这条不变量：`UPLOAD_READY` 只由 `filetype` 决定。
        """
        import inspect

        from interfacetester.ext import uploader

        source = inspect.getsource(uploader)

        # `UPLOAD_READY = True` 必须出现在**只 try import filetype** 的块里，
        # 而 requests_toolbelt 的 import 必须是**另一个** try（可选）。
        self.assertIn("import filetype", source)
        self.assertIn("UPLOAD_READY = True", source)

        # 判据：把 filetype 置 None（模拟它缺失）时 UPLOAD_READY 应当为 False；
        # 而把 MultipartEncoder 置 None 不该影响可用性。
        original_ready = uploader.UPLOAD_READY
        original_encoder = uploader.MultipartEncoder
        try:
            uploader.MultipartEncoder = None
            # 编码器仍应可用（它根本不依赖 MultipartEncoder）
            result = uploader._build_multipart_encoder(
                {"f": ("a.txt", "CONTENT", "text/plain")}
            )
            self.assertTrue(
                result.content_type.startswith("multipart/form-data"),
                "`requests_toolbelt` 缺失竟然让编码器不可用了（N37 回归）",
            )
        finally:
            uploader.MultipartEncoder = original_encoder
            uploader.UPLOAD_READY = original_ready

    def test_upload_extra_still_declares_both_packages(self):
        """反向护栏：`pyproject.toml` 仍声明 `requests_toolbelt`（不擅自删依赖）。

        NOTICE: 它虽然不再被 import 使用，但**移除依赖**属于对外行为变更
        （老环境/离线锁清单/文档都引用它），本批刻意**不动声明**，
        只解除它在代码里的"硬前置"地位。要清理依赖请单独拍板。
        """
        base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(base, "pyproject.toml"), encoding="utf-8") as f:
            content = f.read()

        self.assertIn("requests-toolbelt", content)
        self.assertIn("filetype", content)


class TestDriftDetectorsCatchSyntheticDrift(unittest.TestCase):
    """判据自检：四条判据都必须能报出**合成**的漂移，否则「一直绿」没有意义。

    NOTICE: 这里刻意**不**读真实文件——用合成输入驱动判据函数，
    这样它对「两个文件当前恰好一致」这个状态无感，注入式验证才有意义。
    """

    def _entry(self, text: str, line_no: int = 1) -> ParsedRequirement:
        return ParsedRequirement(Requirement(text), line_no, text)

    def test_name_drift_is_reported_in_both_directions(self):
        declared = parse_declared(["requests>=2.31", "only-in-pyproject>=1.0"])
        entries = [self._entry("requests==2.31.0"), self._entry("only-in-req==3.0")]

        problems = main_name_drift(declared, entries)
        joined = "\n".join(problems)

        self.assertIn("only-in-pyproject", joined)
        self.assertIn("only-in-req", joined)
        self.assertEqual(len(problems), 2)
        # 双向都报了之外，共同存在的那条不能误报
        self.assertNotIn("requests", joined)

    def test_name_normalization_does_not_fake_a_drift(self):
        """`PyYAML` ↔ `pyyaml` 是同一个包，不得因为大小写/分隔符差异假报。"""
        declared = parse_declared(["PyYAML>=6.0.1"])
        entries = [self._entry("pyyaml==6.0.3")]
        self.assertEqual(main_name_drift(declared, entries), [])

    def test_out_of_range_pin_is_reported(self):
        declared = parse_declared(["urllib3>=1.26,<3"])
        entries = [self._entry("urllib3==3.0.0")]

        problems = pin_range_drift(declared, entries)
        self.assertEqual(len(problems), 1)
        self.assertIn("urllib3", problems[0])
        self.assertIn("<3", problems[0])

    def test_lower_bound_violation_is_reported_too(self):
        """下界也要查：`>=2.0` 而锁定 `1.9` 同样是漂移（不是只查上界）。"""
        declared = parse_declared(["pydantic>=2.0,<3"])
        entries = [self._entry("pydantic==1.10.13")]
        self.assertEqual(len(pin_range_drift(declared, entries)), 1)

    def test_in_range_pin_is_not_reported(self):
        """反向自检：合法锁定不得报（假报是闸门的头号死因）。"""
        declared = parse_declared(["urllib3>=1.26,<3", "pydantic>=2.0,<3"])
        entries = [self._entry("urllib3==2.8.0"), self._entry("pydantic==2.13.5")]
        self.assertEqual(pin_range_drift(declared, entries), [])
        self.assertEqual(lock_form_drift(entries), [])

    def test_unpinned_or_ranged_lock_line_is_reported(self):
        entries = [self._entry("requests>=2.31", 3), self._entry("urllib3", 4)]

        problems = lock_form_drift(entries)
        self.assertEqual(len(problems), 2)
        self.assertIn("第 3 行", problems[0])
        self.assertIn("第 4 行", problems[1])

    def test_missing_and_stale_extras_are_reported(self):
        extras = {"sql": ["sqlalchemy>=2.0"], "upload": ["filetype>=1.0.7"]}
        commented = [self._entry("sqlalchemy>=2.0"), self._entry("gone-extra>=1.0")]

        problems = extras_listing_drift(extras, commented)
        joined = "\n".join(problems)
        self.assertIn("filetype", joined)  # pyproject 有、注释区没有
        self.assertIn("gone-extra", joined)  # 注释区有、pyproject 没有
        self.assertEqual(len(problems), 2)

    def test_dev_only_allowlist_suppresses_but_stays_visible(self):
        """豁免必须真的生效，且豁免项消失后要被报成过期。"""
        extras = {"dev": ["build>=1.0"], "sql": ["sqlalchemy>=2.0"]}
        commented = [self._entry("sqlalchemy>=2.0")]

        # 豁免生效：`dev` 不在注释区也不报
        self.assertEqual(extras_listing_drift(extras, commented, DEV_ONLY_EXTRAS), [])

        # 但豁免项一旦从 pyproject 消失，豁免表本身要被报成过期
        self.assertEqual(stale_allowlist(extras, DEV_ONLY_EXTRAS), [])
        stale = stale_allowlist({"sql": ["sqlalchemy>=2.0"]}, DEV_ONLY_EXTRAS)
        self.assertEqual(len(stale), 1)
        self.assertIn("dev", stale[0])

    def test_requirement_parser_skips_prose_and_keeps_requirements(self):
        """解析器自检：说明文字/命令片段不得被当成 requirement。"""
        text = (
            "# InterfaceTester 运行时依赖清单（锁版本，供离线/复现部署）\n"
            "#   1) 有网络的环境下载 wheel：\n"
            "#        pip download -r requirements.txt -d ./wheels\n"
            "#        pip install -e .  会自动安装。\n"
            "pydantic==2.13.5\n"
            "# ---- 可选扩展（按需取消注释）----\n"
            "# allure-pytest>=2.8.16        # allure 报告\n"
            "#                              # 二进制包说明\n"
        )

        main = parse_requirement_lines(text, commented=False)
        commented = parse_requirement_lines(text, commented=True)

        self.assertEqual([e.normalized_name for e in main], ["pydantic"])
        self.assertEqual([e.normalized_name for e in commented], ["allure-pytest"])
        # 行号必须指回真实行（报错信息要能直接定位）
        self.assertEqual(main[0].line_no, 5)
        self.assertEqual(commented[0].line_no, 7)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
