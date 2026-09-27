# -*- coding: utf-8 -*-
"""取证：退出码契约护栏的**注入式元护栏**（把两边的"保留码"标记抹掉 → 判据必须真红）。

做法：**先备份字节** → 注入 → 跑定向用例（期望 RED）→ **字节级还原** → 再跑（期望 GREEN）。
不修改任何历史内容；还原后校验 sha256 一致（不一致就抛错，避免"取证脚本把仓改坏"）。

用法：`.\.venv\Scripts\python.exe probe_ai_exit_codes\inject_reserved_marker.py`
"""

import hashlib
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CLI_PY = REPO / "interfacetester_ai" / "cli.py"
MANUAL = REPO / "docs" / "使用手册-AI协作层.md"
TARGET = (
    "tests/ai_exit_codes_contract_test.py::"
    "TestExitCodeContract::test_every_code_is_asserted_or_marked_reserved"
)
MARK = "保留码"


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_target():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TARGET, "-q", "--no-header"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    passed = "1 passed" in (proc.stdout or "")
    failed = "1 failed" in (proc.stdout or "")
    return proc.returncode, passed, failed


def main():
    backups = {p: p.read_bytes() for p in (CLI_PY, MANUAL)}
    baseline = {p: _digest(p) for p in backups}
    try:
        print("=== 注入前（期望绿） ===")
        code, passed, failed = _run_target()
        print(f"  退出码={code} passed={passed} failed={failed}")

        # 注入①：抹掉 cli.py 常量行注释里的标记；注入②：抹掉手册那行的标记
        cli_text = CLI_PY.read_text(encoding="utf-8").replace(
            'EXIT_BUDGET = 5  # 保留码：gen 路径上预算超限被 gen.py 记为"片级失败"，最终统一 EXIT_CASE_FAILED(1)',
            "EXIT_BUDGET = 5",
        )
        CLI_PY.write_text(cli_text, encoding="utf-8")
        manual_text = MANUAL.read_text(encoding="utf-8").replace(f"**{MARK}**：输入超预算。", "输入超预算（切片太大）。")
        MANUAL.write_text(manual_text, encoding="utf-8")
        print(f"\n=== 注入后（期望红；{MARK} 标记已从两边抹掉）===")
        code, passed, failed = _run_target()
        print(f"  退出码={code} passed={passed} failed={failed}")
        injected_red = failed and not passed
    finally:
        for path, raw in backups.items():
            path.write_bytes(raw)

    print("\n=== 还原后（期望绿 + 字节一致） ===")
    for path in backups:
        same = _digest(path) == baseline[path]
        print(f"  {path.relative_to(REPO)} sha256 与注入前一致: {same}")
        assert same, f"{path} 未被字节级还原！"
    code, passed, failed = _run_target()
    print(f"  退出码={code} passed={passed} failed={failed}")

    print("\n结论：注入式元护栏" + ("**真红** ✓（判据不是空跑）" if injected_red else "**没红** ✗（判据是空跑！）"))


if __name__ == "__main__":
    main()
