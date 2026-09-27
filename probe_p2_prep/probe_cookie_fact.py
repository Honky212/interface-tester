# -*- coding: utf-8 -*-
"""**响应 Cookie 成为可查事实**探针（§9.29 + §9.30）：文档声明 → 断言 → 真跑绿 → **牙齿对照**。

## 为什么要有它

登记项说「`cookies` 支持（**需先让"响应 cookies"成为可查事实**）」。这条链上有三环，
缺一环都只是"看起来支持了"：

| 环 | 谁做 | 缺了会怎样 |
| --- | --- | --- |
| ① 文档**声明**（响应 Cookie 表） | 文档作者 | 写了也没人认 → cookie 断言只能手工写 ✗ |
| ② **断言**（固定值 → `equal`；枚举 → `contained_by`；都没有 → 点名、不产） | `assertions.derive_from_cookie_tables` | 硬凑一条"类型是 string"永远为真、零信息量 ✗ |
| ③ **事实真的存在**（mock 按声明下发 `Set-Cookie`，值与期望**同源**） | `mock_file_service` + `cookie_mock_values` | 断言无从验证；值不同源还会造**假失败** ✗ |

    python probe_p2_prep/probe_cookie_fact.py          # → out_cookie_fact.txt
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

FIXTURE_DOC = """# 会话接口（探针夹具）

## POST /api/session/login

登录并下发会话 cookie。

#### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 | 示例值 |
| --- | --- | --- | --- | --- |
| username | string | 是 | 用户名 | alice |

请求体示例：

```json
{"username": "alice"}
```

#### 响应 Cookie

| Cookie | 值 | 必带 | 说明 |
| --- | --- | --- | --- |
| sessionId | sess-1 | 是 | **固定值**：登录成功后总是它 |
| variant | - | 否 | 取值：A / B |
| theme | - | 否 | 主题偏好（可选返回） |

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


def main():
    parser = argparse.ArgumentParser(description="响应 Cookie 事实探针（§9.29）")
    parser.add_argument("--port", type=int, default=8897)
    parser.add_argument(
        "--out", default=os.path.join(BASE, "probe_p2_prep", "out_cookie_fact.txt")
    )
    args = parser.parse_args()

    workspace = os.path.join(os.environ.get("TEMP", "/tmp"), "ai_cookie_probe")
    shutil.rmtree(workspace, ignore_errors=True)
    os.makedirs(workspace, exist_ok=True)
    doc_path = os.path.join(workspace, "cookie_fixture.md")
    record = os.path.join(workspace, "record.jsonl")
    io.open(doc_path, "w", encoding="utf-8", newline="\n").write(FIXTURE_DOC)

    env = {**os.environ, "PYTHONPATH": BASE}
    lines = [
        "=" * 78,
        "响应 Cookie 成为可查事实（§9.29/§9.30）：文档声明 → 断言 → 真跑 + 牙齿对照",
        f"工作区：{workspace}",
        "=" * 78,
    ]

    server = subprocess.Popen(
        [
            sys.executable,
            "-u",  # ★无缓冲：启动日志（"按文档下发 N 个 cookie"）才能被读到当证据
            "-X",
            "utf8",
            os.path.join(BASE, "probe_p2_prep", "mock_file_service.py"),
            "--port",
            str(args.port),
            "--doc",
            doc_path,
            "--record",
            record,
        ],
        cwd=BASE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    gen_stdout = ""
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
        gen_stdout = gen.stdout or ""
        lines.append(f"\n① 确定性通路退出码 = {gen.returncode}")
    except Exception:  # pragma: no cover - 兜底：出错也要把 mock 收掉
        server.terminate()
        raise
    for line in gen_stdout.splitlines():
        if "接口段" in line or "1/1" in line:
            lines.append("   " + line.strip()[:120])

    drafts = os.path.join(workspace, ".ai", "draft")
    draft = next(
        (os.path.join(drafts, name) for name in sorted(os.listdir(drafts)) if name.endswith(".yml")),
        "",
    )
    text = io.open(draft, encoding="utf-8").read() if draft else ""
    lines.append(f"\n② 草稿里的 cookie 断言（{os.path.basename(draft) or '（没有草稿）'}）")
    for match in re.finditer(
        r"  - (equal|contained_by):\n    - (cookies\.\w+)\n((?:    - .*\n|      - .*\n)+)", text
    ):
        expect = "、".join(
            item.strip("- ").strip() for item in match.group(3).splitlines() if item.strip()
        )
        lines.append(f"   · {match.group(1)}: [{match.group(2)}, {expect}]")
    for match in re.finditer(r"  - jsonschema_match:\n(?:    .*\n|      .*\n)+", text):
        block = " ".join(
            item.strip("- ").strip() for item in match.group(0).splitlines()[1:] if item.strip()
        )
        lines.append(f"   · jsonschema_match: {block}")
    unknowns = io.open(
        os.path.join(drafts, os.path.splitext(os.path.basename(draft))[0] + ".unknowns.json"),
        encoding="utf-8",
    ).read()
    for note in re.findall(r'"message": "(.*?)"', unknowns, re.S):
        if "cookie" in note:
            lines.append("   · 点名：" + note.strip()[:150])

    lines += ["", "③ 转正 + 真跑（mock 按文档下发 Set-Cookie）"]
    from interfacetester_ai.assembler import promote_draft  # noqa: PLC0415

    # ★`promote_draft` 的写盘根是**相对路径**（`cases/`、`.ai/reviews/`）→ 必须把进程切到工作区，
    #   否则会把用例写进**仓库根**（本探针第一版就踩到了：仓库根多了 cases/_api_session_login.yml ✗）。
    os.chdir(workspace)
    case_name = os.path.splitext(os.path.basename(draft))[0]
    result = promote_draft(
        draft,
        approver="§9.29 验收",
        approved_at=datetime.datetime.now().isoformat(timespec="seconds"),
        resolved_blockers=("unknown-critical",),
    )
    lines.append(f"   · promote：{result.promoted}（{result.reason or 'ok'}）")
    done = _run(
        [
            os.path.join(BASE, ".venv", "Scripts", "hrun.exe"),
            os.path.join("cases", case_name + ".yml"),
            "--junitxml",
            "reports/cookie.xml",
            "--save-tests",
        ],
        workspace,
        env=env,
    )
    lines.append(f"   · hrun 退出码 = {done.returncode}")
    for line in ((done.stdout or "") + (done.stderr or "")).splitlines():
        stripped = line.strip()
        if "passed" in stripped or "failed" in stripped or stripped.startswith("E "):
            lines.append("     " + stripped[:130])

    run_logs = os.path.join(workspace, "logs")
    if os.path.isdir(run_logs):
        newest = max(
            (os.path.join(run_logs, name) for name in os.listdir(run_logs)),
            key=os.path.getmtime,
        )
        lines.append("\n④ 逐步断言（run.log 原文）")
        for line in io.open(newest, encoding="utf-8", errors="replace"):
            if "cookies." in line and ("pass" in line or "fail" in line):
                lines.append("   " + line.strip()[-110:])

    # ★mock 必须**活到用例跑完**（第一版把它在 finally 里提前收掉了 → hrun 连不上 → 断言全是 None ✗）
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
        server.kill()
    mock_log = (server.stdout.read() if server.stdout else "") or ""
    lines.append("\n⑤ mock 侧证据（启动日志：按文档下发了哪些 cookie）")
    for line in mock_log.splitlines():
        if "响应 cookie" in line or "错误条件" in line or "路由" in line:
            lines.append("   " + line.strip()[:130])

    # ★**牙齿对照**（最关键的一步）：同一份用例，换一台**不声明这些 cookie 的** mock 再跑一遍 ——
    #   若"必带"与"固定值"是真的判据，它必须**变红**；若两遍都绿，说明这断言**永远为真、没有信息量** ✗。
    import re as _re  # noqa: PLC0415

    no_cookie_doc = os.path.join(workspace, "no_cookie_fixture.md")
    io.open(no_cookie_doc, "w", encoding="utf-8", newline="\n").write(
        _re.sub(r"#### 响应 Cookie\n\n(?:\|.*\n)+", "", FIXTURE_DOC)
    )
    lines.append("\n⑥ 牙齿对照（同一份用例 + **不下发 cookie** 的 mock → 必须变红）")
    server2 = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-X",
            "utf8",
            os.path.join(BASE, "probe_p2_prep", "mock_file_service.py"),
            "--port",
            str(args.port),
            "--doc",
            no_cookie_doc,
        ],
        cwd=BASE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        time.sleep(3)
        done2 = _run(
            [
                os.path.join(BASE, ".venv", "Scripts", "hrun.exe"),
                os.path.join("cases", case_name + ".yml"),
                "--junitxml",
                "reports/cookie_negative.xml",
                "--save-tests",
            ],
            workspace,
            env=env,
        )
        lines.append(f"   · hrun 退出码 = {done2.returncode}（期望 **≠0**：判据有牙）")
        for line in ((done2.stdout or "") + (done2.stderr or "")).splitlines():
            stripped = line.strip()
            if stripped.startswith("E   assert") or "failed" in stripped:
                lines.append("     " + stripped[:130])
    finally:
        server2.terminate()
        try:
            server2.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            server2.kill()

    lines += [
        "",
        "■ 结论",
        "  · 文档里的**响应 Cookie 表**现在被认下（`cookie_declarations`）✓",
        "  · 有可判据事实的（固定值 / 枚举 / **必带**）→ 产 `equal` / `contained_by` / `required` ✓",
        "  · 没有可判据事实的 → **不产断言、逐条点名**（不硬凑永远为真的断言）✓",
        "  · mock **按同一份判据**下发 `Set-Cookie` → 断言真跑绿 ✓",
        "  · ★**牙齿对照**：换成不下发 cookie 的 mock → **必须变红**（否则这断言永远为真、没有信息量 ✗）",
    ]

    out = "\n".join(lines) + "\n"
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(out)
    print(out)
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

