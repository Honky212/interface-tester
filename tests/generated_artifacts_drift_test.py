# -*- coding: utf-8 -*-
"""L12：`examples/` 下已入库的 `*_test.py` 与「从 YAML 重新生成」的结果必须**语义一致**。

## 为什么这条不变量是必要的

`examples/` 里有 28 个 `*_test.py` 是 `hmake` 的**生成物**（另外几个是手写的，没有对应 YAML，
不参与本用例）。生成物入库就有**漂移风险**：改了 YAML 却忘了重新生成，
仓库里的示例就会教错写法。这不是假想——**H2 的真实实例就是这种漂移**：
`examples/httpbin/validate.yml` 里写了 `validate_script`（生成器不支持、静默丢弃），
而入库的 `validate_test.py` 里那两条断言**根本不存在**，示例于是「假通过」。

## 为什么断言 AST 等价，而不是逐字节相等（本机实测）

在 Windows 上重新生成 35 个文件后做三种口径对比：

| 口径 | 不同的文件数 |
|---|---|
| 逐字节 | **8** |
| 去注释/空行/行尾空白 | **7** |
| **AST（归一化：忽略注释、空白、行号、引号风格）** | **0** |

两类噪声会把「逐字节相等」变成**不稳定**的断言：
  ① **路径分隔符**：本机生成的是 `# FROM: cookie_manipulation\\hardcode.yml`，
     已入库的是 `cookie_manipulation/hardcode.yml`（原件是在 Linux/CI 上生成的）；
  ② **black 版本**：`pyproject.toml` 声明 `black>=22.3` **没有上界**，不同机器/不同
     black 版本的重排结果不同（实测 `.with_headers(**{...})` 一行 vs 折成两行）。

「逐字节相等」在 Linux CI 上或许能过，在 Windows 上必挂；而**它想防的其实是语义漂移**，
所以正确的判据是 AST。这样既能在 CI 里稳定运行，又能抓住 H2 那一类真问题
（断言被静默丢弃 → AST 必然不同）。

NOTICE: 本用例会真的跑一次 `hmake`（复制 `examples/` 到 `logs/` 下重新生成，
**不碰仓库里的原文件**）。实测批量生成 11 个目录一次约 **1.6s**（单次 black 调用）。
"""

import ast
import os
import shutil
import subprocess
import sys
import unittest
import uuid

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXAMPLES = os.path.join(BASE, "examples")

# 参与对比的下限：低于这个数说明目录扫描出了问题（例如在错误的 cwd 下运行），
# 那时用例会「空跑通过」——宁可失败也不要假通过。
MIN_EXPECTED_COMPARED = 20


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _ast_dump(source: str) -> str:
    """归一化 AST：忽略注释、空白、行号与引号风格，只保留语义。"""
    return ast.dump(ast.parse(source), annotate_fields=True, include_attributes=False)


def _sibling_yaml(generated_path: str) -> str:
    """找到与 `X_test.py` 配对的 `X.yml` / `X.yaml` / `X.json`（没有则返回 None）。"""
    stem = os.path.basename(generated_path)[: -len("_test.py")]
    folder = os.path.dirname(generated_path)
    for ext in (".yml", ".yaml", ".json"):
        candidate = os.path.join(folder, stem + ext)
        if os.path.isfile(candidate):
            return candidate
    return None


class TestGeneratedArtifactsMatchTheirYaml(unittest.TestCase):
    """L12：生成物一旦入库，就必须与它的 YAML 保持语义一致。"""

    @classmethod
    def setUpClass(cls):
        cls.work_dir = os.path.join(
            BASE, "logs", f"tmp_drift_{uuid.uuid4().hex[:8]}"
        )
        os.makedirs(cls.work_dir)
        cls.copy_examples = os.path.join(cls.work_dir, "examples")
        shutil.copytree(EXAMPLES, cls.copy_examples)

        # 需要重新生成的目录（含 yml/yaml 的目录）；一次调用把 black 的开销摊薄
        cls.target_dirs = sorted(
            {
                root
                for root, _, files in os.walk(cls.copy_examples)
                if any(f.endswith((".yml", ".yaml")) for f in files)
            }
        )
        cls.make_result = subprocess.run(
            [sys.executable, "-m", "interfacetester.cli", "make", *cls.target_dirs],
            cwd=BASE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work_dir, ignore_errors=True)

    def _pairs(self):
        """返回 [(相对路径, 已入库文件, 重新生成的文件)]。"""
        pairs = []
        for root, dirs, files in os.walk(self.copy_examples):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if not name.endswith("_test.py"):
                    continue
                regenerated = os.path.join(root, name)
                rel = os.path.relpath(regenerated, self.copy_examples)
                original = os.path.join(EXAMPLES, rel)
                if not os.path.isfile(original):
                    continue
                if _sibling_yaml(original) is None:
                    continue  # 手写用例（没有对应 YAML）不参与
                pairs.append((rel, original, regenerated))
        return sorted(pairs)

    def test_regeneration_succeeded(self):
        """先确认批量生成这一步本身是成功的（否则后面的对比没有意义）。"""
        self.assertEqual(
            self.make_result.returncode,
            0,
            f"批量 hmake 失败：{(self.make_result.stdout + self.make_result.stderr)[-800:]}",
        )

    def test_every_generated_artifact_has_a_yaml(self):
        """护栏：参与对比的文件数不能太少，防止「扫错目录 → 空跑通过」。"""
        pairs = self._pairs()

        self.assertGreaterEqual(
            len(pairs),
            MIN_EXPECTED_COMPARED,
            f"只找到 {len(pairs)} 个「有 YAML 的生成物」，预期至少 "
            f"{MIN_EXPECTED_COMPARED} 个——目录扫描可能有问题（当前 cwd 是否正确？）",
        )

    def test_committed_artifacts_are_semantically_in_sync(self):
        """核心不变量：已入库的生成物与重新生成的结果 **AST 等价**。

        逐字节检查刻意不做（会被路径分隔符与 black 版本差异打中，见模块 docstring）。
        """
        offenders = []
        for rel, original, regenerated in self._pairs():
            with open(original, encoding="utf-8", errors="replace") as f:
                old_source = f.read()
            with open(regenerated, encoding="utf-8", errors="replace") as f:
                new_source = f.read()

            try:
                old_dump = _ast_dump(old_source)
                new_dump = _ast_dump(new_source)
            except SyntaxError as ex:
                offenders.append(f"{rel}: 语法错误 {ex}")
                continue

            if old_dump != new_dump:
                offenders.append(rel)

        self.assertEqual(
            offenders,
            [],
            "以下已入库的生成物与「从 YAML 重新生成」的结果**语义不一致**"
            "（改了 YAML 但忘了重新生成？）：\n  "
            + "\n  ".join(offenders)
            + "\n\n重新生成方式：在项目根跑 "
            "`hmake <对应目录>`（生成物会覆盖同名文件）。",
        )

    def test_ast_judgement_actually_detects_drift(self):
        """非空跑自检：AST 判据必须真的能发现「断言被丢掉」这种漂移。

        否则用例可能因为归一化过度（例如把整个 body 丢掉）而永远通过。
        """
        before = (
            "from interfacetester import InterfaceTester, Config, Step, RunRequest\n"
            "class TestCaseX(InterfaceTester):\n"
            "    config = Config('x')\n"
            "    teststeps = [\n"
            "        Step(RunRequest('s').get('/get')"
            ".validate().assert_equal('status_code', 200)),\n"
            "    ]\n"
        )
        # 模拟 H2：断言被静默丢弃
        after = before.replace(
            ".validate().assert_equal('status_code', 200)", ""
        )

        self.assertNotEqual(_ast_dump(before), _ast_dump(after))

        # 反向：仅注释/空白/引号风格变化不得被判为漂移
        cosmetic = before.replace("'x'", '"x"').replace(
            "class TestCaseX", "# 新注释\nclass TestCaseX"
        )
        self.assertEqual(_ast_dump(before), _ast_dump(cosmetic))
