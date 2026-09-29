# -*- coding: utf-8 -*-
r"""打包与离线部署的护栏用例 —— v8 §5.8 行 1003（**P3c** 的第三件事）。

## ★这一批的诚实范围（写在最前面）

P3c 要的是「**离线环境一键起服务；前端不暴露任何密钥**」。本机 `.venv` **没有**
`build` / `wheel` / `setuptools`（`pip wheel .` 需要它们的后端）——但**基础解释器**
（`sys.base_prefix`）**有** setuptools，于是**离线打轮被实测下来了**（第十二批，2026-09-25）：

    # 用来打轮的解释器（`.venv` 里没有 setuptools，基础解释器有）
    C:\python312\python.exe -m pip wheel --no-build-isolation --no-deps --no-index ^
        -w <临时目录> <interfacetester_ai 的临时副本>
    # 装进**全新的空 venv**（没有任何 build 工具），并起服务
    <新venv>\Scripts\python.exe -m pip install --no-index --no-deps <两个 wheel>
    <新venv>\Scripts\python.exe -m interfacetester_ai serve --port <port>

★两条纪律写进代码（本文件下半部分）：
① 打轮**必须在临时副本上做**——直接在包目录里打，setuptools 会留下 `build/` 与
   `*.egg-info/`（实测），那是"改了源码树"；
② 判据分两层：`TestBuiltWheelMatchesSourceTree` 管**产物内容**（产物 ↔ 源码树对账，
   与内核 `tests/wheel_content_test.py` 同口径），`TestOfflineWheelBuildsAndInstalls`
   管**离线打得出来 + 装得上 + 入口在**。

下面这些仍然是把「离线一键」的**根据**变成机器判据：

| 判据 | 为什么它才是根据 |
| --- | --- |
| **零三方依赖**（`pyproject.dependencies = []` **且** 包内 import 扫描干净） | "离线装得上"的全部依据就是这个。声明是空的、但代码 import 了 `requests`，离线就装不起来 |
| **`[build-system]` 声明完整** | 有网时能打轮；无网时也能用 `--no-build-isolation` 打 |
| **入口脚本在 `[project.scripts]`** | `haify` 是**声明出来**的入口，不是靠 `python -m` 兜着 |

「**前端不暴露任何密钥**」那一条在 `tests/web_readonly_test.py` 里（token 不出现在页面）。
"""

import ast
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from typing import Tuple

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

PKG_DIR = os.path.join(BASE, "interfacetester_ai")
PYPROJECT = os.path.join(PKG_DIR, "pyproject.toml")

# 兜底 stdlib 名单（`sys.stdlib_module_names` 是 3.10+ 才有，而本包声明 >=3.8）
# ——只列**本包实际用到的**，不追求完备：判据要的是"没混进第三方"，不是"认全 stdlib"。
_STDLIB_FALLBACK = frozenset(
    {
        "argparse",
        "ast",
        "collections",
        "contextlib",
        "dataclasses",
        "difflib",
        "functools",
        "hashlib",
        "hmac",
        "html",
        "http",
        "io",
        "itertools",
        "json",
        "os",
        "pathlib",
        "posixpath",
        "re",
        "shutil",
        "string",
        "subprocess",
        "sys",
        "tempfile",
        "textwrap",
        "threading",
        "time",
        "typing",
        "unittest",
        "urllib",
    }
)

# 本仓自己的两个包（`__future__` 是语言特性，不是第三方）
_OWN_PACKAGES = frozenset({"interfacetester", "interfacetester_ai", "__future__"})


def _upstream_requirements() -> set:
    """内核声明的依赖名（`requirements.txt`，规范化成小写）。

    ★这是"允许清单"的**唯一**依据：允许某个 import 名 ⟺ 它对应的包**已被内核声明**。
    否则"允许清单"就成了一张**谁都能往上加名字**的白纸。
    """
    path = os.path.join(BASE, "requirements.txt")
    names: set = set()
    if not os.path.exists(path):
        return names
    with open(path, encoding="utf-8", errors="replace") as fp:
        for line in fp:
            line = line.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            name = re.split(r"[<>=!\[;]", line)[0].strip().lower()
            if name:
                names.add(name)
    return names


# ★本包复用内核三方依赖的**全部**情形：`import 名 → 内核声明的包名`。
# 判据要求**同时**满足两条：
#   ① 那个包名**已在内核 `requirements.txt` 里**（不是我自造的依赖）；
#   ② 这个 import **只出现在函数体内**（"没装它"时本包仍能被导入）。
# 两条缺一不可——少了任何一条，"允许清单"都会变成一张谁都能加名字的白纸。
_UPSTREAM_REUSED = {
    "yaml": "PyYAML",  # fix.py：内核的硬依赖，注释里已写明"本包不新增依赖"
    "requests": "requests",  # llm.py：延迟 import，缺它时包仍可导入
}


def _module_imports(path: str) -> Tuple[set, set]:
    """`(顶层 import 名, 只在函数体内的 import 名)`。

    ★为什么必须**分层**：一个 import 写在模块顶层，意味着"**没有它这个模块就 import 不了**"；
    写在函数体内则意味着"**用到那一步**才需要它"。对"离线能不能装"这件事，
    这两者的含义完全不同——所以判据也必须分开。
    """
    with open(path, encoding="utf-8") as fp:
        tree = ast.parse(fp.read(), filename=path)

    def names_of(node) -> set:
        found: set = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                found.update(alias.name.split(".")[0] for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                if child.level:  # 相对 import —— 属本包
                    found.add("interfacetester_ai")
                elif child.module:
                    found.add(child.module.split(".")[0])
        return found

    top: set = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            top |= names_of(node)

    inner: set = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner |= names_of(node)

    return top, inner - top


def _pyproject_text() -> str:
    with open(PYPROJECT, encoding="utf-8") as fp:
        return fp.read()


def _stdlib_names() -> frozenset:
    names = getattr(sys, "stdlib_module_names", None)
    return frozenset(names) if names else _STDLIB_FALLBACK


def _top_level_imports(path: str) -> set:
    """一个模块的**顶层 import 名**（`a.b` 只取 `a`）。"""
    with open(path, encoding="utf-8") as fp:
        tree = ast.parse(fp.read(), filename=path)

    found: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对 import（`from . import x`）——属本包
                found.add("interfacetester_ai")
            elif node.module:
                found.add(node.module.split(".")[0])
    return found


class TestOfflineInstallability(unittest.TestCase):
    """★「离线一键起服务」的**根据**（而不是一句声明）。"""

    def test_dependencies_are_empty(self):
        """★零三方依赖 —— 这是"离线装得上"的全部依据。

        （行内注释要允许：`pyproject` 里那一行后面就跟着一句为什么是空的。）
        """
        match = re.search(
            r"^dependencies\s*=\s*(\[.*?\])(?:\s*#.*)?$", _pyproject_text(), re.M
        )
        if match is None:
            self.fail("`pyproject.toml` 里没有 `dependencies = [...]` 这一行")
        self.assertEqual(
            re.sub(r"\s+", "", match.group(1)),
            "[]",
            "一旦引入三方依赖，就该在这里**显式改口**——而不是让离线环境装不上",
        )

    def test_top_level_imports_are_stdlib_or_ours(self):
        """★**顶层** import 只许 stdlib + 本仓自己的包。

        ★注意这条判据的**定位**：它只管顶层。一个 import 写在模块顶层意味着
        "没有它这个模块就 import 不了"——那才是"离线装不上"的那种依赖。
        允许复用的内核三方依赖**只能**出现在函数体内（由下一条判据管）。
        """
        allowed = _stdlib_names() | _OWN_PACKAGES
        offenders: dict = {}

        for name in sorted(os.listdir(PKG_DIR)):
            if not name.endswith(".py"):
                continue
            top, _inner = _module_imports(os.path.join(PKG_DIR, name))
            for module in sorted(top):
                if module not in allowed:
                    offenders.setdefault(module, []).append(name)

        self.assertEqual(
            offenders, {}, f"顶层出现了 stdlib / 本仓之外的 import：{offenders}"
        )

    def test_reused_upstream_imports_are_lazy_and_declared(self):
        """★复用内核依赖的**两条**判据（缺一不可）。

        1. 那个包**已在**内核 `requirements.txt` 里（不是我自造的依赖）；
        2. 它**只**出现在函数体内（"没装它"时本包仍能被导入）。

        少了第 1 条，"允许清单"会变成一张谁都能加名字的白纸；
        少了第 2 条，本包就悄悄变成了"必须装 PyYAML 才能 import"——
        而离线环境下这**正是装不上的原因**。
        """
        declared = _upstream_requirements()
        offenders: dict = {}

        for name, package in _UPSTREAM_REUSED.items():
            if package.lower() not in declared:
                offenders[name] = f"内核 `requirements.txt` 里没有声明 `{package}`"
                continue
            for filename in sorted(os.listdir(PKG_DIR)):
                if not filename.endswith(".py"):
                    continue
                top, _inner = _module_imports(os.path.join(PKG_DIR, filename))
                if name in top:
                    offenders[name] = (
                        f"`{filename}` 在**顶层** import 了它"
                        "——应当延迟到函数体内（否则缺它时整个模块 import 不了）"
                    )
                    break

        self.assertEqual(offenders, {}, f"复用内核依赖的判据不满足：{offenders}")

    def test_requirements_txt_documents_offline_install(self):
        """★"离线一键"的**说明书**就在 `requirements.txt` 里（不是另写一份）。

        判据：那两步命令必须在文件里——它们是"离线环境一键起服务"的**可执行形式**。
        """
        path = os.path.join(BASE, "requirements.txt")
        self.assertTrue(os.path.exists(path), "内核的 `requirements.txt` 不见了")

        with open(path, encoding="utf-8", errors="replace") as fp:
            text = fp.read()

        for piece in ("pip download", "--no-index", "--find-links"):
            self.assertIn(piece, text, f"`requirements.txt` 里缺离线部署的 `{piece}`")


    def test_every_module_in_the_package_is_scanned(self):
        """元护栏：别让上一条因为"一个文件都没扫到"而空转。"""
        names = [n for n in os.listdir(PKG_DIR) if n.endswith(".py")]

        self.assertGreaterEqual(len(names), 20, "包内模块数异常")
        self.assertIn("__init__.py", names)

    def test_build_system_is_declared(self):
        """有网时能打轮；无网时也能用 `--no-build-isolation` 打（后端已声明）。"""
        text = _pyproject_text()

        for piece in ("[build-system]", "requires", "build-backend", "setuptools"):
            self.assertIn(piece, text)


class TestEntryPoints(unittest.TestCase):
    """`haify` 必须是**声明出来**的入口（不是靠 `python -m` 兜着）。"""

    def test_haify_script_is_declared(self):
        text = _pyproject_text()

        self.assertIn("[project.scripts]", text)
        self.assertRegex(text, r"(?m)^haify\s*=")

    def test_module_entry_matches_the_script(self):
        """`python -m interfacetester_ai` 与 `haify` 必须指向**同一个** `main`。

        ★"两套行为"是这个仓最忌讳的形态之一：用户换了启动方式，
        不该得到不同的东西。所以这里用 `is` 比对象身份，而不是比名字。
        """
        from interfacetester_ai import __main__ as entry  # noqa: PLC0415
        from interfacetester_ai import cli  # noqa: PLC0415

        self.assertIs(
            getattr(entry, "main", None),
            cli.main,
            "`__main__` 与 `cli` 的 `main` 不是同一个对象——那就是两套行为",
        )

    def test_script_target_is_the_cli_main(self):
        text = _pyproject_text()
        target = re.search(r'(?m)^haify\s*=\s*"([^"]+)"', text)

        self.assertIsNotNone(target)
        self.assertEqual(target.group(1), "interfacetester_ai.cli:main")

    def test_requires_python_is_declared(self):
        self.assertRegex(_pyproject_text(), r"(?m)^requires-python\s*=")


class TestOfflineWheelBuildsAndInstalls(unittest.TestCase):
    """★「离线一键起服务」的**前半段**：离线打得出来 + 在源码树之外装得上 + 入口随包落地。

    为什么这条判据必须"自己找解释器"：本机 `.venv` **没有** `build`/`wheel`/`setuptools`
    （实测），所以标准路径 `python -m build --wheel` 在本机跑不起来；
    但**基础解释器**（`sys.base_prefix`）有 setuptools —— 于是走

        <builder> -m pip wheel --no-build-isolation --no-deps --no-index -w <tmp> <src 副本>

    这**同样**是离线可复现的（`--no-index` 保证不碰网络）。

    ★两条形态纪律：
    ① **在临时副本上打轮**：直接在包目录里打，setuptools 会留下 `build/` 与
       `*.egg-info/`（实测过一次），那是"改了源码树"；
    ② `--target` 装到源码树之外，判"装得上"不靠 cwd 蒙对。

    ★本类取代了历史的 `TestBuildToolingIsNotAvailableHere`（那个类断言
    「`dist/` 里不许有 wheel」，并留话"真打了轮就回来改它"——本批就是回来签字的）。
    """

    TIMEOUT = 300

    @staticmethod
    def _interpreters_with_setuptools():
        """能当"打轮器"的解释器：本进程的，以及**基础解释器**（venv 的 base）。"""
        candidates = [sys.executable]
        if os.name == "nt":
            candidates.append(os.path.join(sys.base_prefix, "python.exe"))
        else:
            candidates.append(os.path.join(sys.base_prefix, "bin", "python3"))
            candidates.append(os.path.join(sys.base_prefix, "bin", "python"))

        found = []
        for exe in candidates:
            if not exe or not os.path.exists(exe) or exe in found:
                continue
            try:
                probe = subprocess.run(  # noqa: S603
                    [exe, "-c", "import setuptools"],
                    capture_output=True,
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if probe.returncode == 0:
                found.append(exe)
        return found

    @staticmethod
    def _py_files_under(root):
        names = []
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    rel = os.path.relpath(os.path.join(folder, name), root)
                    names.append(rel.replace(os.sep, "/"))
        return sorted(names)

    def _run(self, argv):
        try:
            return subprocess.run(  # noqa: S603
                argv, capture_output=True, text=True, timeout=self.TIMEOUT
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - 只防挂死
            self.fail(f"命令超时（{self.TIMEOUT}s）：{argv}\n{exc}")

    def test_offline_build_and_install_round_trip(self):
        builders = self._interpreters_with_setuptools()
        if not builders:
            raise unittest.SkipTest(
                "找不到带 `setuptools` 的解释器——本条判据需要一个能离线打轮的解释器"
                "（本机：基础解释器通常有；CI 的 `package` job 会显式装 `[dev]`）"
            )

        with tempfile.TemporaryDirectory(prefix="ai_wheel_roundtrip_") as tmp:
            src = os.path.join(tmp, "src")
            shutil.copytree(
                PKG_DIR,
                src,
                ignore=shutil.ignore_patterns("__pycache__", "build", "*.egg-info"),
            )
            out = os.path.join(tmp, "out")
            os.makedirs(out)

            build = self._run(
                [
                    builders[0],
                    "-m",
                    "pip",
                    "wheel",
                    "--no-build-isolation",
                    "--no-deps",
                    "--no-index",
                    "--disable-pip-version-check",
                    "-w",
                    out,
                    src,
                ]
            )
            self.assertEqual(
                build.returncode, 0, f"离线打轮失败：\n{build.stdout}\n{build.stderr}"
            )

            wheels = [n for n in os.listdir(out) if n.endswith(".whl")]
            self.assertEqual(len(wheels), 1, f"期望恰好一个 wheel，实得 {wheels}")
            wheel = os.path.join(out, wheels[0])

            target = os.path.join(tmp, "installed")
            install = self._run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--disable-pip-version-check",
                    "--target",
                    target,
                    wheel,
                ]
            )
            self.assertEqual(
                install.returncode,
                0,
                "`--target` 安装失败（离线、无依赖）：\n"
                f"{install.stdout}\n{install.stderr}",
            )

            installed_pkg = os.path.join(target, "interfacetester_ai")
            self.assertTrue(
                os.path.isfile(os.path.join(installed_pkg, "__init__.py")),
                "装完在目标目录里找不到 `interfacetester_ai/__init__.py`",
            )
            self.assertEqual(
                self._py_files_under(installed_pkg),
                _source_module_names(),
                "装出来的模块集合与源码树不一致——少一个文件就是『缺功能交付』",
            )

            dist_infos = [d for d in os.listdir(target) if d.endswith(".dist-info")]
            self.assertEqual(len(dist_infos), 1, f"期望一个 dist-info，实得 {dist_infos}")
            ep_path = os.path.join(target, dist_infos[0], "entry_points.txt")
            self.assertTrue(
                os.path.isfile(ep_path),
                "装完没有 `entry_points.txt`——`haify` 的入口声明没随包落地",
            )
            with open(ep_path, encoding="utf-8") as fp:
                self.assertIn("interfacetester_ai.cli:main", fp.read())

            # ★形态纪律①：打轮**不许**在包目录里留下 build/ 与 egg-info
            leftovers = [
                n
                for n in os.listdir(PKG_DIR)
                if n in ("build", "dist") or n.endswith(".egg-info")
            ]
            self.assertEqual(
                leftovers, [], f"打轮污染了包目录：{leftovers}（必须在临时副本上打）"
            )


# ---------------------------------------------------------------------------
# ★第十二批（2026-09-25）：**产物 ↔ 源码树对账**（同内核 wheel_content_test.py 的口径）
# ---------------------------------------------------------------------------

AI_WHEEL_ENV = "INTERFACETESTER_AI_WHEEL_PATH"
AI_WHEEL_SKIP = (
    f"未指定 {AI_WHEEL_ENV}（普通单测跑的是源码树，没有 wheel 可查）。"
    "先 `python -m build --wheel --outdir dist_ai interfacetester_ai`，"
    f"再让 {AI_WHEEL_ENV} 指向产物即可运行本用例。"
)


def _ai_wheel_path():
    raw = os.environ.get(AI_WHEEL_ENV, "").strip()
    if not raw:
        return None
    # 支持 shell 通配展开后的多值（取第一个）与相对路径
    candidate = raw.split(os.pathsep)[0].strip()
    if not os.path.isabs(candidate):
        candidate = os.path.join(BASE, candidate)
    return candidate


def _source_module_names():
    """源码树里 `interfacetester_ai/` 下的模块（相对包目录，/ 分隔）。

    ★刻意用"与源码对比"而不是硬编码清单：硬编码清单在下次新增模块时不会自己更新，
    等于把"交付少了文件"这个坑再挖一遍（内核 `wheel_content_test.py` 的同一取向）。
    """
    names = []
    for folder, dirs, files in os.walk(PKG_DIR):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "build")]
        for name in files:
            if name.endswith(".py"):
                rel = os.path.relpath(os.path.join(folder, name), PKG_DIR)
                names.append(rel.replace(os.sep, "/"))
    return sorted(names)


def _record_entries(zf):
    """把 `RECORD` 解析成 `{成员路径: (sha256 字段, size 字段)}`。"""
    names = [n for n in zf.namelist() if n.endswith(".dist-info/RECORD")]
    text = zf.read(names[0]).decode("utf-8")
    entries = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(",")
        entries[parts[0]] = (
            parts[1] if len(parts) > 1 else "",
            parts[2] if len(parts) > 2 else "",
        )
    return entries


class TestBuiltWheelMatchesSourceTree(unittest.TestCase):
    """★交付的轮子必须**就是当前源码**（默认 skip，把 wheel 路径给它才跑）。

    为什么必须有这条：单测跑的是**源码树**——轮子里少一个模块、版本号没跟上、
    入口没随包落地、或者声明了三方依赖（离线就装不上），源码树单测**一条都看不出来**。
    内核早就为此立了 `tests/wheel_content_test.py`（坑 10），本包同款口径。
    """

    def setUp(self):
        path = _ai_wheel_path()
        if not path:
            raise unittest.SkipTest(AI_WHEEL_SKIP)
        if not os.path.isfile(path):
            self.fail(f"{AI_WHEEL_ENV} 指向的 wheel 不存在：{path}")
        self.wheel = path
        self.zf = zipfile.ZipFile(path)  # noqa: SIM115 - 由 addCleanup 关闭
        self.addCleanup(self.zf.close)

    def _members_with_suffix(self, suffix):
        return sorted(n for n in self.zf.namelist() if n.endswith(suffix))

    def test_module_set_equals_source_tree(self):
        in_wheel = sorted(
            n[len("interfacetester_ai/") :]
            for n in self.zf.namelist()
            if n.startswith("interfacetester_ai/") and n.endswith(".py")
        )

        self.assertEqual(
            in_wheel,
            _source_module_names(),
            "wheel 里的模块与源码树不一致——少一个文件就是『缺功能交付』",
        )

    def test_metadata_declares_zero_dependencies(self):
        metas = self._members_with_suffix(".dist-info/METADATA")
        self.assertEqual(len(metas), 1, f"期望一个 METADATA，实得 {metas}")
        meta = self.zf.read(metas[0]).decode("utf-8")
        requires = [
            line
            for line in meta.splitlines()
            if line.lower().startswith("requires-dist:")
        ]

        self.assertEqual(
            requires, [], f"产物声明了三方依赖，离线环境就装不上：{requires}"
        )
        self.assertIn("Name: interfacetester-ai", meta)

        from interfacetester_ai import __version__ as pkg_version  # noqa: PLC0415

        self.assertIn(
            f"Version: {pkg_version}", meta, "产物版本与源码里的 `__version__` 不一致"
        )

    def test_entry_point_is_haify_to_the_same_main(self):
        eps = self._members_with_suffix(".dist-info/entry_points.txt")
        self.assertEqual(len(eps), 1, f"期望一个 entry_points.txt，实得 {eps}")
        text = self.zf.read(eps[0]).decode("utf-8")

        self.assertIn("[console_scripts]", text)
        self.assertRegex(text, r"(?m)^haify\s*=\s*interfacetester_ai\.cli:main\s*$")

    def test_every_member_is_hashed_in_record(self):
        """RECORD 是 pip 安装时的**完整性判据**——它缺项/写错，装出来的东西就不可信。"""
        entries = _record_entries(self.zf)
        problems = []

        for name in self.zf.namelist():
            if name.endswith("/RECORD"):
                continue
            digest, size = entries.get(name, ("", ""))
            if not digest:
                problems.append(f"{name}: RECORD 里没有这一项或没有哈希")
                continue
            data = self.zf.read(name)
            got = (
                base64.urlsafe_b64encode(hashlib.sha256(data).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            if digest != f"sha256={got}":
                problems.append(f"{name}: RECORD 里的哈希与内容不符")
            elif size != str(len(data)):
                problems.append(f"{name}: RECORD 里的字节数与内容不符")

        self.assertEqual(problems, [], f"RECORD 不完整或与内容不符：{problems}")

    def test_wheel_carries_nothing_outside_the_package(self):
        dist_roots = {n.split("/")[0] for n in self.zf.namelist() if ".dist-info/" in n}
        self.assertEqual(len(dist_roots), 1, f"期望一个 dist-info 目录，实得 {dist_roots}")
        dist_root = dist_roots.pop()
        stray = [
            n
            for n in self.zf.namelist()
            if not n.startswith("interfacetester_ai/")
            and not n.startswith(dist_root + "/")
        ]

        self.assertEqual(
            stray,
            [],
            f"wheel 里混进了包外的东西（`tests/`/`bench/`/`.ai/` 都不该进产物）：{stray}",
        )


if __name__ == "__main__":
    unittest.main()

