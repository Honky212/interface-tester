# -*- coding: utf-8 -*-
"""H5：交付用的 wheel 必须与**当前源码**一致。

## 为什么需要这条

`dist/` 里那份 wheel 曾经是 **09/16 20:48** 构建的，早于 P2-b / P3 / A2 / 0918 全部特性提交：
`build/lib/interfacetester/` 里**没有 `converters/`**（缺 P3 的四个导入器），
`comparators.py` 里也**没有** `jsonschema_match` / `xpath_match` / `xml_schema_match`。
README/deploy 文档写的发布流程是 `python -m build --wheel → dist/*.whl`，
**照做就会交付一个缺功能的旧包** —— 而单测全绿，因为单测跑的是源码树而不是 wheel。

所以这里对**构建产物**本身做校验。判据刻意做成**与源码对比**而不是硬编码清单：
硬编码清单在下次新增算子/新增子包时不会自己更新，等于把同一个坑再挖一遍。

## 怎么运行

本用例默认 **skip**（普通单测跑的是源码树，没有 wheel 可查）。要跑它就把 wheel 路径给它：

```bash
python -m build --wheel
INTERFACETESTER_WHEEL_PATH=dist/interfacetester-4.3.5-py3-none-any.whl \\
    python -m pytest tests/wheel_content_test.py -q
```

CI 里由 `unit-test` job 的「构建 wheel 并校验内容」步骤执行（见 `.github/workflows/test.yml`
与 `.gitlab-ci.yml`）。
"""

import ast
import os
import re
import unittest
import zipfile

from interfacetester import __version__

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WHEEL_ENV = "INTERFACETESTER_WHEEL_PATH"
SKIP_REASON = (
    f"未指定 {WHEEL_ENV}（普通单测没有 wheel 可查）。"
    f"先 `python -m build --wheel`，再设 {WHEEL_ENV} 指向产物即可运行本用例。"
)


def _wheel_path():
    raw = os.environ.get(WHEEL_ENV, "").strip()
    if not raw:
        return None
    # 支持 shell 通配展开后的多值（取第一个）与相对路径
    candidate = raw.split(os.pathsep)[0].strip()
    if not os.path.isabs(candidate):
        candidate = os.path.join(BASE, candidate)
    return candidate


def _source_package_files():
    """源码树里 `interfacetester/` 下的全部 .py（相对路径，/ 分隔）。"""
    files = []
    for root, dirs, names in os.walk(os.path.join(BASE, "interfacetester")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in names:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                files.append(os.path.relpath(path, BASE).replace(os.sep, "/"))
    return sorted(files)


def _module_level_functions(source: str):
    """模块级公开函数名（= 内置算子白名单的派生依据）。"""
    return sorted(
        node.name
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    )


class TestWheelContent(unittest.TestCase):
    """构建产物必须包含当前源码的全部内容（H5）。"""

    @classmethod
    def setUpClass(cls):
        cls.wheel = _wheel_path()
        if cls.wheel is None:
            raise unittest.SkipTest(SKIP_REASON)
        if not os.path.isfile(cls.wheel):
            raise AssertionError(f"{WHEEL_ENV} 指向的文件不存在：{cls.wheel}")
        cls.archive = zipfile.ZipFile(cls.wheel)
        cls.names = set(cls.archive.namelist())

    def test_package_files_are_all_present(self):
        """源码树里的每个 `.py` 都必须出现在 wheel 里。

        这条能**通用地**抓住「整个子包没被打进去」——正是 H5 的真实形态
        （`converters/` 整目录缺失）。硬编码「应该有 converters」在下次新增子包时不会更新。
        """
        missing = [p for p in _source_package_files() if p not in self.names]

        self.assertEqual(
            missing,
            [],
            "wheel 缺少源码树里的这些文件（dist 里的是过期产物？）：\n  "
            + "\n  ".join(missing)
            + "\n\n重新构建：python -m build --wheel",
        )

    def test_builtin_operators_are_all_present(self):
        """内置算子白名单里的每个函数都必须在 wheel 的 `comparators.py` 里。

        与源码**对比**而非硬编码清单：H5 的真实形态就是「22 个算子只打进 19 个」。
        """
        relative = "interfacetester/builtin/comparators.py"
        with open(os.path.join(BASE, relative), encoding="utf-8") as f:
            source_functions = _module_level_functions(f.read())

        wheel_functions = _module_level_functions(
            self.archive.read(relative).decode("utf-8")
        )

        missing = sorted(set(source_functions) - set(wheel_functions))
        self.assertEqual(
            missing,
            [],
            f"wheel 里的 {relative} 缺少这些算子（dist 里的是过期产物？）：{missing}",
        )

    def test_entry_points_match_pyproject(self):
        """wheel 声明的控制台脚本必须与 `pyproject.toml` 的 `[project.scripts]` 一致。

        案例：`hconvert` 曾因为「入口点声明在 pyproject 里、但安装/产物是更早的」
        而在 README 里被写成交付命令却**根本不存在**。
        """
        with open(os.path.join(BASE, "pyproject.toml"), encoding="utf-8") as f:
            pyproject = f.read()
        declared = set(
            re.findall(
                r"^([A-Za-z0-9_-]+)\s*=\s*\"interfacetester\.cli:", pyproject, re.M
            )
        )
        self.assertTrue(declared, "没从 pyproject.toml 解析出 [project.scripts]")

        entry_point_names = [
            name for name in self.names if name.endswith(".dist-info/entry_points.txt")
        ]
        self.assertEqual(
            len(entry_point_names), 1, f"wheel 里的 entry_points.txt 数量异常：{entry_point_names}"
        )

        content = self.archive.read(entry_point_names[0]).decode("utf-8")
        packaged = set(
            re.findall(
                r"^([A-Za-z0-9_-]+)\s*=\s*interfacetester\.cli:", content, re.M
            )
        )

        self.assertEqual(
            declared - packaged,
            set(),
            "pyproject 声明了但 wheel 里没有的控制台脚本：" f"{sorted(declared - packaged)}",
        )

    def test_version_matches_source(self):
        """wheel 的版本号必须与 `__version__` 一致（两者都写死会各走各的）。"""
        metadata_names = [
            name for name in self.names if name.endswith(".dist-info/METADATA")
        ]
        self.assertEqual(len(metadata_names), 1, metadata_names)

        metadata = self.archive.read(metadata_names[0]).decode("utf-8")
        match = re.search(r"^Version:\s*(\S+)", metadata, re.M)
        self.assertIsNotNone(match, "METADATA 里没有 Version 字段")

        self.assertEqual(
            match.group(1),
            __version__,
            "wheel 的版本与源码 __version__ 不一致（产物过期，请重新构建）",
        )
