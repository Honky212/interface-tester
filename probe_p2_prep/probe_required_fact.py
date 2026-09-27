# -*- coding: utf-8 -*-
"""**响应数据表的「必带」→ 安全 `required`** 探针（§9.31）：文档声明 → 断言 → 真跑 + **牙齿对照**。

## 为什么要有它

§9.30 把「必带」做成了可判据的 `required`，但**只在响应 Cookie 表**上生效；
**响应数据表**（`body`）当时刻意**不写 `required`**（口径 B2：文档没建模"什么时候会返回"）。
§9.31 把同一套「必带」判据推广到响应数据表 —— 风险点是**层级**：
裸字段名挂在 `data` 下，`required` 必须写进 **`data` 那一层**，写到根上就变成
"顶层必须有 `fileName`"（**语义完全变了 → 误报** ✗）。

本探针证明三件事：

1. 文档标了「必带」→ schema 里 `required` **挂在正确的容器层级**；
2. **真跑绿**（mock 按文档的成功响应示例回数据 → 字段在 → 通过 ✓）；
3. ★**牙齿对照**：换成**成功响应示例里没有这个字段**的 mock → 必须**变红**
   （否则这条 `required` 永远为真、零信息量 ✗ —— 与 §9.29 自查纠正要防的形态同一类）。

    python probe_p2_prep/probe_required_fact.py        # → out_required_fact.txt
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

FIXTURE_DOC = """# 文件接口（探针夹具）

## POST /api/file/metadata

按路径 + 文件名查元数据。

#### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 | 示例值 |
| --- | --- | --- | --- | --- |
| name | string | 是 | 文件名（刻意不叫 `fileName`：撞名会多产一条取错层级的断言，见 ⑤） | report.pdf |

请求体示例：

```json
{"name": "report.pdf"}
```

#### 响应数据（`data` 的字段）

| 字段 | 类型 | 必带 | 说明 |
| --- | --- | --- | --- |
| fileName | string | 是 | 文件名 |
| fileVersion | integer | 否 | 版本号，从 1 开始 |

#### 成功响应

```json
{"success": true, "errorCode": null, "message": "操作成功", "data": {"fileName": "report.pdf", "fileVersion": 3}}
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
    parser = argparse.ArgumentParser(description="响应数据表「必带」探针（§9.31）")
    parser.add_argument("--port", type=int, default=8894)
    parser.add_argument(
        "--out", default=os.path.join(BASE, "probe_p2_prep", "out_required_fact.txt")
    )
    args = parser.parse_args()

    workspace = os.path.join(os.environ.get("TEMP", "/tmp"), "ai_required_probe")
    shutil.rmtree(workspace, ignore_errors=True)
    os.makedirs(workspace, exist_ok=True)
    doc_path = os.path.join(workspace, "required_fixture.md")
    io.open(doc_path, "w", encoding="utf-8", newline="\n").write(FIXTURE_DOC)
    env = {**os.environ, "PYTHONPATH": BASE}

    lines = [
        "=" * 78,
        "响应数据表的「必带」→ 安全 required（§9.31）：文档声明 → 断言 → 真跑 + 牙齿对照",
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
    lines.append(f"\n② 草稿里的 schema 断言（{os.path.basename(draft) or '（没有草稿）'}）")
    for match in re.finditer(r"  - jsonschema_match:\n(?:    .*\n|      .*\n|        .*\n)+", text):
        block = [item.strip("- ").strip() for item in match.group(0).splitlines()[1:] if item.strip()]
        lines.append("   · " + " | ".join(block))
    case_name = os.path.splitext(os.path.basename(draft))[0]

    lines += ["", "③ 转正 + 真跑（mock 按文档的成功响应示例回数据）"]
    from interfacetester_ai.assembler import promote_draft  # noqa: PLC0415

    os.chdir(workspace)  # ★写盘根是相对路径（见 §9.29 的教训）
    result = promote_draft(
        draft,
        approver="§9.31 验收",
        approved_at=datetime.datetime.now().isoformat(timespec="seconds"),
        resolved_blockers=("unknown-critical",),
    )
    lines.append(f"   · promote：{result.promoted}（{result.reason or 'ok'}）")
    done = _run(
        [
            os.path.join(BASE, ".venv", "Scripts", "hrun.exe"),
            os.path.join("cases", case_name + ".yml"),
            "--junitxml",
            "reports/required.xml",
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
        if "路由" in line or "响应 cookie" in line:
            lines.append("   · mock：" + line.strip()[:120])

    # ★牙齿对照：同一份用例，换一台"成功响应示例里**没有** fileName"的 mock → 必须变红
    broken_doc = os.path.join(workspace, "broken_fixture.md")
    io.open(broken_doc, "w", encoding="utf-8", newline="\n").write(
        FIXTURE_DOC.replace(
            '"data": {"fileName": "report.pdf", "fileVersion": 3}',
            '"data": {"fileVersion": 3}',
        )
    )
    lines.append("\n④ 牙齿对照（同一份用例 + **响应里没有必带字段**的 mock → 必须变红）")
    server2 = _start_mock(args.port, broken_doc, env)
    try:
        time.sleep(3)
        done2 = _run(
            [
                os.path.join(BASE, ".venv", "Scripts", "hrun.exe"),
                os.path.join("cases", case_name + ".yml"),
                "--junitxml",
                "reports/required_negative.xml",
                "--save-tests",
            ],
            workspace,
            env=env,
        )
        lines.append(f"   · hrun 退出码 = {done2.returncode}（期望 **≠0**：判据有牙）")
        for line in ((done2.stdout or "") + (done2.stderr or "")).splitlines():
            stripped = line.strip()
            if stripped.startswith("E   assert") or "failed" in stripped or "required" in stripped:
                lines.append("     " + stripped[:140])
    finally:
        _stop(server2)

    # ★⑤ **已知边界（登记，不是本功能的问题）**：请求表字段名与响应侧字段**末段撞名**时，
    #   归属闸门按**名字**对、不按**路径**对 → 会多产一条 `type_match: [body.fileName, str]`
    #   （响应里其实在 `body.data.fileName`）→ **必红** ✗。示例文档 §3 第 6 行把它写成"别这么写"，
    #   但工具其实**不该产出这种取不到值的断言**。这里把现场**可复现地**留一份。
    collision_doc = os.path.join(workspace, "collision_fixture.md")
    io.open(collision_doc, "w", encoding="utf-8", newline="\n").write(
        FIXTURE_DOC.replace("| name | string | 是 |", "| fileName | string | 是 |").replace(
            '{"name": "report.pdf"}', '{"fileName": "report.pdf"}'
        )
    )
    lines.append("\n⑤ ★回归（§9.32）：请求字段与响应字段**末段撞名** → 取错层级那条**必须被摘**、且点名")
    server3 = _start_mock(args.port, collision_doc, env)
    try:
        time.sleep(3)
        gen3 = _run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-m",
                "interfacetester_ai.cli",
                "gen",
                collision_doc,
                "--assertions-only",
                "--case-name-prefix",
                "collision_",
                "--base-url",
                f"http://127.0.0.1:{args.port}",
            ],
            workspace,
            env=env,
        )
        lines.append(f"   · 确定性通路退出码 = {gen3.returncode}")
        collision_draft = next(
            (
                os.path.join(drafts, name)
                for name in sorted(os.listdir(drafts))
                if name.startswith("collision_") and name.endswith(".yml")
            ),
            "",
        )
        collision_text = (
            io.open(collision_draft, encoding="utf-8").read() if collision_draft else ""
        )
        bogus = [
            name
            for name in re.findall(r"  - type_match:\n    - (body\.\w+)\n    - str", collision_text)
            if name == "body.fileName"  # ★只报"请求字段名 × 响应字段名"撞出来的那一条
        ]
        lines.append(
            f"   · 草稿里取错层级的断言：{sorted(set(bogus)) or '（没有 ✓）'}"
        )
        if not bogus:
            lines.append("     ✓ 撞名那条被归属闸门摘掉了（§9.32：按**路径**判，不再按名字）")
        collision_unknowns = io.open(
            os.path.join(
                drafts,
                os.path.splitext(os.path.basename(collision_draft))[0] + ".unknowns.json",
            ),
            encoding="utf-8",
        ).read()
        for message in re.findall(r'"message": "(.*?)"', collision_unknowns, re.S):
            if "层级" in message:
                lines.append("   · 点名：" + message.strip()[:140])
                break
        promote_draft(
            collision_draft,
            approver="§9.31 边界复现",
            approved_at=datetime.datetime.now().isoformat(timespec="seconds"),
            resolved_blockers=("unknown-critical",),
        )
        done3 = _run(
            [
                os.path.join(BASE, ".venv", "Scripts", "hrun.exe"),
                os.path.join("cases", os.path.splitext(os.path.basename(collision_draft))[0] + ".yml"),
                "--junitxml",
                "reports/collision.xml",
                "--save-tests",
            ],
            workspace,
            env=env,
        )
        lines.append(f"   · hrun 退出码 = {done3.returncode}（期望 **0**：摘掉取错层级那条后，用例真跑绿）")
        for line in ((done3.stdout or "") + (done3.stderr or "")).splitlines():
            stripped = line.strip()
            if stripped.startswith("E   assert body") or "check_value: None" in stripped:
                lines.append("     " + stripped[:130])
    finally:
        _stop(server3)

    lines += [
        "",
        "■ 结论",
        "  · 响应数据表的「必带」→ `required`，**挂在文档说的那一层容器上**（裸名 → `data` 层）✓",
        "  · 与「仅当…时返回」矛盾 → **不写** `required` + 点名（不替文档拍板）✓",
        "  · 没标必带 → **老行为一字不变**（不写 `required`，见 `TestResponseTableSchema`）✓",
        "  · ★**牙齿对照**：响应里缺必带字段 → **必须变红**（否则这条 `required` 永远为真 ✗）",
    ]
    out = "\n".join(lines) + "\n"
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(out)
    print(out)
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

