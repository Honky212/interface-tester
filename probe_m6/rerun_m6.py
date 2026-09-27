# -*- coding: utf-8 -*-
"""M-6 补齐：① `analyze` 连跑两次（判是否偶发）② `fix` 指向正确 cases 目录 ③ `debugtalk` 存**完整**输出。

上一轮（`run_m6.py`）结果：analyze 退出码 1（模型吐 `<unused50>…` 畸形输出、无 reports/analysis.md）；
summary 0 ✓；fix 0 ✓（6 条全"不给改"，因用例名对不上文件）；debugtalk 0 ✓ 但"采纳 0 / 拒绝 0"。
本脚本把这三处各再跑一次，并把**完整 stdout/stderr** 落到 `probe_m6/out/`（仓内留档，便于复核）。
"""

import hashlib
import json
import os
import pathlib
import subprocess

REPO = pathlib.Path(r"D:\tester")
OUT = REPO / "probe_m6" / "out"
OUT.mkdir(parents=True, exist_ok=True)
PY = REPO / ".venv" / "Scripts" / "python.exe"
SUMMARY = REPO / "project-two" / "logs" / "probe_silent.summary.json"
CASES = REPO / "examples" / "postman_echo" / "request_methods"
ENV = {
    **os.environ,
    "PYTHONPATH": str(REPO),
    "PYTHONIOENCODING": "utf-8",
    "INTERFACETESTER_AI_BASE_URL": "http://127.0.0.1:11434/v1",
    "INTERFACETESTER_AI_MODEL": "gemma4:latest",
    "INTERFACETESTER_AI_SANDBOX": "on",
    "INTERFACETESTER_AI_TIMEOUT": "300",
    "INTERFACETESTER_AI_RETRIES": "1",
}


def run(tag, args, workdir, spec_text=None):
    if spec_text is not None:
        (pathlib.Path(workdir) / "spec.json").write_text(spec_text, encoding="utf-8")
    proc = subprocess.run(
        [str(PY), "-m", "interfacetester_ai.cli", *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ENV,
    )
    (OUT / f"{tag}.stdout.txt").write_text(proc.stdout or "", encoding="utf-8")
    (OUT / f"{tag}.stderr.txt").write_text(proc.stderr or "", encoding="utf-8")
    products = sorted(
        str(p.relative_to(workdir)).replace("\\", "/")
        for p in pathlib.Path(workdir).rglob("*")
        if p.is_file() and p.name != "spec.json"
    )
    print(f"[{tag}] 退出码={proc.returncode}  产物={products}")
    return proc.returncode


def main() -> int:
    work = OUT / "work"
    work.mkdir(parents=True, exist_ok=True)
    codes = {}

    for index in (1, 2):
        codes[f"analyze#{index}"] = run(f"analyze_run{index}", ["analyze", str(SUMMARY)], work)

    codes["fix"] = run("fix_rerun", ["fix", str(SUMMARY), "--cases", str(CASES)], work)

    spec = {
        "name": "sign_params",
        "args": ["params"],
        "body": "把 params 按 key 排序拼成 `k=v&...` 查询串，再取 md5 十六进制小写",
        "vectors": [
            {
                "args": [{"b": 2, "a": 1}],
                "expect": hashlib.md5(b"a=1&b=2").hexdigest(),
            }
        ],
    }
    codes["debugtalk"] = run(
        "debugtalk_rerun",
        ["debugtalk", str(work / "spec.json"), "--sandbox"],
        work,
        spec_text=json.dumps(spec, ensure_ascii=False, indent=2),
    )

    print("\n汇总退出码:", codes)
    print("完整输出已落:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
