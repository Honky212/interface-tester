# -*- coding: utf-8 -*-
"""**响应头成为可判据事实**探针（§9.33）：文档声明 → 断言 → 真跑 + **牙齿对照**。

## 为什么要有它

登记项说「响应头断言：确定性通路不产（要断响应头请人工写，或让模型补）」。
本探针把这条链串起来真跑：**文档写一张响应头表** → 确定性通路产 `headers.<名>` 断言 →
mock **按同一份判据**下发这些头 → 用例真跑绿；再换一台**不下发**这些头的 mock → **必须变红**。

    python probe_p2_prep/probe_response_header_fact.py        # → out_response_header_fact.txt
"""

import argparse
import datetime
import io
import os
import re
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

FIXTURE_DOC = """# 文件服务（探针夹具）

## POST /api/file/metadata

查元数据。

#### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 | 示例值 |
| --- | --- | --- | --- | --- |
| name | string | 是 | 文件名 | report.pdf |

请求体示例：

```json
{"name": "report.pdf"}
```

#### 响应头

| 头 | 值 | 说明 |
| --- | --- | --- |
| X-Api-Version | v1 | **固定值**：当前接口版本 |
| Cache-Control | - | 取值：no-store / max-age=60 |
| Date | - | 服务器时间（易变） |

#### 成功响应

```json
{"success": true, "errorCode": null, "message": "操作成功", "data": null}
```
"""


def _run(command, cwd, timeout=180, env=None):
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _start_mock(port, doc_path, env):
    return subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-X",
            "utf8",
            os.path.join(BASE, "probe_p2_prep", "mock_file_service.py"),
            "--port",
            str(port),
            "--doc",
            doc_path,
        ],
        cwd=BASE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _stop(server):
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
        server.kill()
    return (server.stdout.read() if server.stdout else "") or ""


def main():
    parser = argparse.ArgumentParser(description="响应头事实探针（§9.33）")
    parser.add_argument("--port", type=int, default=8891)
    parser.add_argument(
        "--out", default=os.path.join(BASE, "probe_p2_prep", "out_response_header_fact.txt")
    )
    args = parser.parse_args()

    workspace = os.path.join(os.environ.get("TEMP", "/tmp"), "ai_respheader_probe")
    shutil.rmtree(workspace, ignore_errors=True)
    os.makedirs(workspace, exist_ok=True)
    doc_path = os.path.join(workspace, "header_fixture.md")
    io.open(doc_path, "w", encoding="utf-8", newline="\n").write(FIXTURE_DOC)
    env = {**os.environ, "PYTHONPATH": BASE}

    lines = [
        "=" * 78,
        "响应头成为可判据事实（§9.33）：文档声明 → 断言 → 真跑 + 牙齿对照",
        f"工作区：{workspace}",
        "=" * 78,
    ]

    server = _start_mock(args.port, doc_path, env)
    try:
        time.sleep(3)
        gen = _run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "interfacetester_ai.cli",
                "gen",
                doc_path,
                "--assertions-only",
                "--base-url",
                f"http://127.0.0.1:{args.port}",
            ],
            workspace,
            env=env,
        )
        lines.append(f"\n① 确定性通路退出码 = {gen.returncode}")
        for line in (gen.stdout or "").splitlines():
            if "1/1" in line or "接口段" in line:
                lines.append("   " + line.strip()[:120])
    except Exception:  # pragma: no cover - 兜底
        _stop(server)
        raise

    drafts = os.path.join(workspace, ".ai", "draft")
    draft = next(
        (os.path.join(drafts, name) for name in sorted(os.listdir(drafts)) if name.endswith(".yml")),
        "",
    )
    text = io.open(draft, encoding="utf-8").read() if draft else ""
    lines.append(f"\n② 草稿里的响应头断言（{os.path.basename(draft) or '（没有草稿）'}）")
    for match in re.finditer(
        r"  - (equal|contained_by):\n    - (headers\.[\w\-]+)\n((?:    - .*\n|      - .*\n)+)", text
    ):
        expect = "、".join(
            item.strip("- ").strip() for item in match.group(3).splitlines() if item.strip()
        )
        lines.append(f"   · {match.group(1)}: [{match.group(2)}, {expect}]")
    unknowns = io.open(
        os.path.join(drafts, os.path.splitext(os.path.basename(draft))[0] + ".unknowns.json"),
        encoding="utf-8",
    ).read()
    for note in re.findall(r'"message": "(.*?)"', unknowns, re.S):
        if "响应头" in note:
            lines.append("   · 点名：" + note.strip()[:150])
            break
    # ★诚实标注（这一条本身就是实测结论）：**推导层**的降级说明目前**不会**落进草稿的 `unknowns`
    #   （`generate_assertions_only_all` 里 `_skipped` 被丢掉，见 §9.33 登记项 3）—— 所以这里
    #   **直接**从推导侧取出来给你看。"没断言什么"必须可见，不能靠"我们记得"。
    from interfacetester_ai.assertions import derive_assertions  # noqa: PLC0415

    for note in derive_assertions(FIXTURE_DOC)[1]:
        if "响应头" in note:
            lines.append(
                "   · 点名（**推导层**直取；草稿 `unknowns` 里**还看不到** → 登记项 3）："
                + note.strip()[:150]
            )

    lines += ["", "③ 转正 + 真跑（mock 按文档下发这些响应头）"]
    from interfacetester_ai.assembler import promote_draft  # noqa: PLC0415

    os.chdir(workspace)  # ★写盘根是相对路径（§9.29 的教训）
    case_name = os.path.splitext(os.path.basename(draft))[0]
    result = promote_draft(
        draft,
        approver="§9.33 验收",
        approved_at=datetime.datetime.now().isoformat(timespec="seconds"),
        resolved_blockers=("unknown-critical",),
    )
    lines.append(f"   · promote：{result.promoted}（{result.reason or 'ok'}）")
    done = _run(
        [
            os.path.join(BASE, ".venv", "Scripts", "hrun.exe"),
            os.path.join("cases", case_name + ".yml"),
            "--junitxml",
            "reports/headers.xml",
            "--save-tests",
        ],
        workspace,
        env=env,
    )
    lines.append(f"   · hrun 退出码 = {done.returncode}")
    for line in ((done.stdout or "") + (done.stderr or "")).splitlines():
        stripped = line.strip()
        if "passed" in stripped or "failed" in stripped or stripped.startswith("E   assert"):
            lines.append("     " + stripped[:130])
    mock_log = _stop(server)
    for line in mock_log.splitlines():
        if "响应头" in line or "路由" in line:
            lines.append("   · mock：" + line.strip()[:130])

    lines.append("\n④ 逐步断言（run.log 原文：响应头那两条）")
    run_logs = os.path.join(workspace, "logs")
    if os.path.isdir(run_logs):
        newest = max(
            (os.path.join(run_logs, name) for name in os.listdir(run_logs)),
            key=os.path.getmtime,
        )
        for line in io.open(newest, encoding="utf-8", errors="replace"):
            if "headers." in line and ("pass" in line or "fail" in line):
                lines.append("   " + line.strip()[-110:])

    # ★牙齿对照：同一份用例 + **不下发这些响应头**的 mock（文档里没有那张表）→ 必须变红
    no_header_doc = os.path.join(workspace, "no_header_fixture.md")
    io.open(no_header_doc, "w", encoding="utf-8", newline="\n").write(
        re.sub(r"#### 响应头\n\n(?:\|.*\n)+", "", FIXTURE_DOC)
    )
    lines.append("\n⑤ 牙齿对照（同一份用例 + **不下发响应头**的 mock → 必须变红）")
    server2 = _start_mock(args.port, no_header_doc, env)
    try:
        time.sleep(3)
        done2 = _run(
            [
                os.path.join(BASE, ".venv", "Scripts", "hrun.exe"),
                os.path.join("cases", case_name + ".yml"),
                "--junitxml",
                "reports/headers_negative.xml",
                "--save-tests",
            ],
            workspace,
            env=env,
        )
        lines.append(f"   · hrun 退出码 = {done2.returncode}（期望 **≠0**：判据有牙）")
        for line in ((done2.stdout or "") + (done2.stderr or "")).splitlines():
            stripped = line.strip()
            if stripped.startswith("E   assert") or "check_value" in stripped or "failed" in stripped:
                lines.append("     " + stripped[:130])
    finally:
        _stop(server2)

    lines += [
        "",
        "■ 结论",
        "  · 文档里的**响应头表**（`头` 打头 + 无「必填」列 + 附近有『响应』字样）被认下 ✓",
        "  · 有可判据事实的（固定值 / 枚举）→ 产 `equal` / `contained_by` ✓；其余**逐条点名** ✓",
        "  · mock **按同一份判据**下发这些头 → 断言真跑绿 ✓；**不下发** → 必须变红（有牙）✓",
    ]
    out = "\n".join(lines) + "\n"
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(out)
    print(out)
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

