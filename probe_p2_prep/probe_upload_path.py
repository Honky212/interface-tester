# -*- coding: utf-8 -*-
"""**上传路径真跑**探针（§9.26）：真发 multipart 文件 → 负例步骤**可验证**；只给名字 → **判不了**。

## 为什么要有它

`T3_1_文件上传` 的负例步骤（"批次里有超大文件 → 部分失败"）一直跑不绿，归因是
「**请求里没有『大小』这个事实**」——它只发了文件名（`files=@big.zip`）。
那句话不能只写在结论里，得**钉成可复跑的形态**：文档写「单文件超过 1KB → FILE_9001」时，

| 用例形态 | 服务端有没有事实 | 期望行为 |
| --- | --- | --- |
| **真发 2KB 文件**（`upload: {files: <path>}`） | 有（multipart 部件大小） | **命中 FILE_9001** ✓（负例步骤可验证） |
| **只发文件名**（`params: {files: @big.zip}`） | **没有** | **不触发** ✓（跑不绿不是用例错） |

★它把两件事一起证明：① **框架的 upload 路径真的能用**（`filetype` 装上之后；探针顺带验证这个
依赖前提）；② **mock 能从 multipart 里量出文件大小**（`multipart_file_sizes`）——这两条都成立，
"上传类负例步骤"才谈得上可验证。

    python probe_p2_prep/probe_upload_path.py            # → out_upload_path.txt

不起长驻服务（自己拉起 mock 再关掉）、不改产品目录（一切都在临时工作区里）。
"""

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# 探针用的**迷你文档**：把真实文档的 50MB 缩到 1KB，判据才跑得动（真实文档侧另有用例冒烟）
FIXTURE_DOC = """# 上传接口（探针夹具）

## 1. 公共错误码

| 错误码 | 含义 | 说明 |
|-------|------|------|
| `FILE_9001` | 单文件超过 1KB | 上传文件大小超过限制 |

## 2.1 上传文件

| **URL** | `POST /api/file/upload` |
|---------|-------------------------|

**请求参数**：

| 参数名 | 类型 | 必填 | 说明 | 示例值 |
|--------|------|------|------|--------|
| `path` | String | 是 | 目录路径 | `/app1/` |
| `files` | MultipartFile[] | 是 | 文件数组 | - |

**响应示例**：

```json
{"success": true, "errorCode": null, "message": "操作成功", "data": null}
```
"""

CASE_WITH_FILE = """config:
  name: upload_with_file
  base_url: http://127.0.0.1:{port}
teststeps:
- name: 上传一个超过 1KB 的文件（真发文件 → 服务端有『大小』这个事实）
  request:
    method: POST
    url: /api/file/upload
    params:
      path: /app1/
    headers:
      X-Path: /app1/
    upload:
      files: {big}
  validate:
  - equal:
    - status_code
    - 200
  - equal:
    - body.errorCode
    - FILE_9001
"""

CASE_NAMES_ONLY = """config:
  name: upload_names_only
  base_url: http://127.0.0.1:{port}
teststeps:
- name: 只给文件名（T3_1 的形态：请求里没有『大小』这个事实）
  request:
    method: POST
    url: /api/file/upload
    params:
      path: /app1/
      files:
      - '@big_over_1kb.zip'
    headers:
      X-Path: /app1/
  validate:
  - equal:
    - status_code
    - 200



  - equal:
    - body.errorCode
    - FILE_9001
"""


def _run(command, cwd, timeout=180):
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _record_entries(path):
    """读 mock 的回读文件（JSONL）→ 每次请求一条。"""
    if not os.path.isfile(path):
        return []
    return [json.loads(line) for line in io.open(path, encoding="utf-8") if line.strip()]


def main():
    parser = argparse.ArgumentParser(description="上传路径真跑探针（§9.26）")
    parser.add_argument("--port", type=int, default=8898)
    parser.add_argument(
        "--out", default=os.path.join(BASE, "probe_p2_prep", "out_upload_path.txt")
    )
    args = parser.parse_args()

    workspace = os.path.join(os.environ.get("TEMP", "/tmp"), "ai_upload_probe")
    shutil.rmtree(workspace, ignore_errors=True)
    cases_dir = os.path.join(workspace, "cases")
    os.makedirs(cases_dir, exist_ok=True)
    doc_path = os.path.join(workspace, "upload_fixture.md")
    record = os.path.join(workspace, "record.jsonl")

    io.open(doc_path, "w", encoding="utf-8", newline="\n").write(FIXTURE_DOC)
    big_file = os.path.join(cases_dir, "big.bin")
    with io.open(big_file, "wb") as fp:
        fp.write(b"x" * 2048)  # 2KB > 文档限额 1KB
    io.open(
        os.path.join(cases_dir, "upload_with_file.yml"), "w", encoding="utf-8", newline="\n"
    ).write(CASE_WITH_FILE.format(port=args.port, big=big_file))
    io.open(
        os.path.join(cases_dir, "upload_names_only.yml"), "w", encoding="utf-8", newline="\n"
    ).write(CASE_NAMES_ONLY.format(port=args.port))

    lines = [
        "=" * 78,
        "上传路径真跑（§9.26）：真发 multipart 文件 vs 只给文件名",
        f"工作区：{workspace}",
        f"夹具文档：{doc_path}（限额 1KB，便于判据跑得动）",
        "=" * 78,
    ]

    ready_probe = _run(
        [
            sys.executable,
            "-c",
            "from interfacetester.ext import uploader; print(uploader.UPLOAD_READY)",
        ],
        BASE,
    )
    ready = (ready_probe.stdout or "").strip()
    lines.append(f"\n① 前提：`UPLOAD_READY = {ready}`（缺 `filetype` 时会是 False）")
    if ready != "True":
        lines.append(
            "   ✗ 上传路径**没解锁**：请 `pip install filetype`"
            "（或 `pip install \"interfacetester[upload]\"`）后重跑"
        )
        io.open(args.out, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
        print("\n".join(lines))
        return 1

    server = subprocess.Popen(
        [
            sys.executable,
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
    )
    results = {}
    try:
        time.sleep(3)
        for case in ("upload_with_file", "upload_names_only"):
            done = _run(
                [
                    os.path.join(BASE, ".venv", "Scripts", "hrun.exe"),
                    f"cases/{case}.yml",
                    "--junitxml",
                    f"reports/{case}.xml",
                    "--save-tests",
                ],
                workspace,
            )
            results[case] = done.returncode
            lines.append(f"\n② `{case}` 真跑退出码 = {done.returncode}（0 = 用例通过）")
            tail = [
                line.strip()
                for line in ((done.stdout or "") + (done.stderr or "")).splitlines()
                if re.search(r"passed|failed|FILE_9001|errorCode", line)
            ]
            for line in tail[-3:]:
                lines.append(f"     {line[:118]}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            server.kill()

    entries = _record_entries(record)
    lines.append(f"\n③ 回读（mock 照录到的请求：{len(entries)} 条）")
    for item in entries:
        body = item.get("body")
        multipart = isinstance(body, str) and "form-data" in str(body)
        kind = "multipart ✓" if multipart else type(body).__name__
        lines.append(
            f"     {item['method']:5} {item['path'][:46]:48} 请求体={kind:12}"
            f"｜命中={(item.get('triggered_rule') or '（未命中）')[:40]}"
        )

    multipart_count = sum(
        1
        for item in entries
        if isinstance(item.get("body"), str) and "form-data" in str(item.get("body"))
    )
    hit_count = sum(1 for item in entries if item.get("triggered_rule"))
    lines += [
        "",
        "■ 结论",
        f"  · 带文件那次：请求体是 multipart ✓（{multipart_count} 条）、mock **命中**大小规则"
        f"（共 {hit_count} 次命中）→ 用例退出码 {results.get('upload_with_file')}（0 = **负例步骤真绿**）",
        f"  · 只给名字那次：**没有** multipart 体、**没有**命中 → 用例退出码 "
        f"{results.get('upload_names_only')}（≠0 正如预期：**请求里没有事实，服务端判不了**）",
        "",
        "★这一对就是 §9.26 要固化的形态：「**大小这个事实在不在请求里**」决定了上传类负例步骤",
        "能不能被验证 —— 与「模型提示词写得好不好」无关。",
    ]

    text = "\n".join(lines) + "\n"
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(text)
    print(text)
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
