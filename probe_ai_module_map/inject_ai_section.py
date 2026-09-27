# -*- coding: utf-8 -*-
"""注入式元护栏：把 §2.2 的标题改坏，判据**必须变红**；然后原样还原。

一次性取证脚本（用完即弃，不进 tests/）——本仓的纪律是"护栏必须被证明能红"，
否则它可能只是永远绿的装饰品（§12.8 里那类"空跑变绿"的教训）。
"""

import hashlib
import pathlib
import subprocess
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
DOC = BASE / "docs" / "架构与调用链.md"
MARKER = "### 2.2 AI 旁路包"
TARGET = "tests/docs_consistency_test.py::TestArchitectureDocCoversEveryModule"


def digest(path):
    return hashlib.md5(path.read_bytes()).hexdigest()[:12]


def main() -> int:
    original = DOC.read_bytes()
    before = digest(DOC)
    text = original.decode("utf-8")
    assert MARKER in text, "标记不在文档里，注入前提不成立"

    # ① 注入：**真的把标记拿掉**（第一次注入写成"### 2.2 AI 旁路包（改名…）"，
    #    结果标记还是它的**前缀**、判据照旧通过 —— 注入本身太弱，等于没验）。
    mutated = text.replace(MARKER, "### 2.2 旁路包模块地图")
    assert MARKER not in mutated, "注入后标记仍在，注入无效"
    DOC.write_bytes(mutated.encode("utf-8"))
    red = subprocess.run(
        [sys.executable, "-m", "pytest", TARGET, "-q", "-p", "no:cacheprovider"],
        cwd=BASE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print("① 注入后 pytest 退出码：", red.returncode)
    tail = [ln for ln in (red.stdout or "").splitlines() if "passed" in ln or "failed" in ln]
    print("   结果行：", tail[-1] if tail else "(无)")

    # ② 还原：**按字节写回**（`read_text`/`write_text` 会做换行归一，md5 会变 —— 踩过）
    DOC.write_bytes(original)
    after = digest(DOC)
    print("② 还原后 md5：", after, "（注入前：", before, "）一致性：", after == before)

    green = subprocess.run(
        [sys.executable, "-m", "pytest", TARGET, "-q", "-p", "no:cacheprovider"],
        cwd=BASE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print("③ 还原后 pytest 退出码：", green.returncode)
    tail = [ln for ln in (green.stdout or "").splitlines() if "passed" in ln or "failed" in ln]
    print("   结果行：", tail[-1] if tail else "(无)")
    return 0 if (red.returncode != 0 and green.returncode == 0 and after == before) else 1


if __name__ == "__main__":
    raise SystemExit(main())
