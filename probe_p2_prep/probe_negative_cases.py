# -*- coding: utf-8 -*-
"""**成对**验证错误条件引擎：正例不误报、负例必命中（§9.22 的能力证明，不依赖 LLM）。

    python probe_p2_prep/mock_file_service.py --port 8899 &      # 先起 mock
    python probe_p2_prep/probe_negative_cases.py                  # 再跑本脚本

为什么要成对：只验负例（"命中了吗"）会漏掉**误报**（正例也被判成错误）——那是更坏的缺陷，
因为它把"正确的用例"判红。每一族都给 (正例, 负例) 两条：正例期望**不命中**，负例期望命中的**错误码**。

★它只发 HTTP、只读响应，不写产品目录；服务端是**从文档推导**的 mock（不看用例）。
"""

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (族, 说明, 方法, 路径, 头, 请求体/查询, 期望错误码（None = 期望成功）)
CASES = [
    ("relation", "正例：path 与 X-Path 一致", "GET",
     "/api/version/getInfo?path=%2Fapp1%2F&filename=report.pdf&version=v1",
     {"X-Path": "/app1/"}, None, None),
    ("relation", "负例：path 与 X-Path 不一致 → FILE_1018", "GET",
     "/api/version/getInfo?path=%2Fapp1%2F&filename=report.pdf&version=v1",
     {"X-Path": "/other/"}, None, "FILE_1018"),
    ("exist", "正例：版本 v1 在文档里出现过", "GET",
     "/api/version/getInfo?path=%2Fapp1%2F&filename=report.pdf&version=v1",
     {"X-Path": "/app1/"}, None, None),
    ("exist", "负例：版本 v999 文档里没有 → FILE_2005", "GET",
     "/api/version/getInfo?path=%2Fapp1%2F&filename=report.pdf&version=v999",
     {"X-Path": "/app1/"}, None, "FILE_2005"),
    ("exist", "正例：目录 /app1/ 是文档示例值", "GET",
     "/api/metadata/get?path=%2Fapp1%2F", {"X-Path": "/app1/"}, None, None),
    ("exist", "负例：目录 /nope-xyz/ 不在种子数据里 → FILE_2001", "GET",
     "/api/metadata/get?path=%2Fnope-xyz%2F", {"X-Path": "/nope-xyz/"}, None, "FILE_2001"),
    ("required", "正例：必填 path 在场", "GET",
     "/api/metadata/get?path=%2Fapp1%2F", {"X-Path": "/app1/"}, None, None),
    ("required", "负例：必填 path 缺失 → FILE_1010", "GET",
     "/api/metadata/get", {"X-Path": "/app1/"}, None, "FILE_1010"),
    ("format", "正例：目录路径以 / 开头并以 / 结尾", "POST",
     "/api/directory/update",
     {"X-Path": "/app1/documents/", "Content-Type": "application/json"},
     {"sourceDirName": "/app1/documents/", "targetDirName": "/app1/"}, None),
    ("format", "负例：不以 / 结尾 → FILE_4001", "POST",
     "/api/directory/update",
     {"X-Path": "/app1/documents/", "Content-Type": "application/json"},
     {"sourceDirName": "/app1/documents", "targetDirName": "/app1/"}, "FILE_4001"),
    ("format", "负例：含连续的 / → FILE_4003", "POST",
     "/api/directory/update",
     {"X-Path": "/app1/documents/", "Content-Type": "application/json"},
     {"sourceDirName": "/app1//documents/", "targetDirName": "/app1/"}, "FILE_4003"),
    ("format", "负例：首尾有空格 → FILE_4018", "POST",
     "/api/directory/update",
     {"X-Path": "/app1/documents/", "Content-Type": "application/json"},
     {"sourceDirName": " /app1/documents/ ", "targetDirName": "/app1/"}, "FILE_4018"),
    ("relation", "正例：源目录 != 目标目录", "POST",
     "/api/directory/update",
     {"X-Path": "/app1/documents/", "Content-Type": "application/json"},
     {"sourceDirName": "/app1/documents/", "targetDirName": "/app1/"}, None),
    ("relation", "负例：源目录 == 目标目录 → FILE_4009", "POST",
     "/api/directory/update",
     {"X-Path": "/app1/documents/", "Content-Type": "application/json"},
     {"sourceDirName": "/app1/", "targetDirName": "/app1/"}, "FILE_4009"),
    ("limit", "正例：上传 2 个文件（上限 10）", "POST",
     "/api/file/upload?path=%2Fapp1%2F&files=@a.pdf&files=@b.pdf",
     {"X-Path": "/app1/"}, None, None),
    ("limit", "负例：上传 11 个文件 → FILE_1016", "POST",
     "/api/file/upload?path=%2Fapp1%2F&" + "&".join(f"files=@f{index}.pdf" for index in range(11)),
     {"X-Path": "/app1/"}, None, "FILE_1016"),
    ("limit", "★判不了：请求里没给『大小』这个事实 → 不命中（也不乱报）", "POST",
     "/api/file/upload?path=%2Fapp1%2F&files=@huge.zip", {"X-Path": "/app1/"}, None, None),
]


def request(base_url, method, path, headers, payload):
    """发一次请求，返回 `(status, 解析后的响应体)`。"""
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_ = urllib.request.Request(base_url + path, data=data, method=method)
    for key, value in (headers or {}).items():
        request_.add_header(key, value)
    try:
        with urllib.request.urlopen(request_, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8") or "{}")


def main():
    parser = argparse.ArgumentParser(description="错误条件引擎的成对能力证明")
    parser.add_argument("--base-url", default="http://127.0.0.1:8899")
    parser.add_argument(
        "--out", default=os.path.join(BASE, "probe_p2_prep", "out_negative_cases.txt")
    )
    args = parser.parse_args()

    lines = ["=" * 74, f"目标：{args.base_url}", "=" * 74]
    passed = 0
    for family, note, method, path, headers, payload, expect in CASES:
        status, body = request(args.base_url, method, path, headers, payload)
        code = body.get("errorCode") if isinstance(body, dict) else None
        ok = (code == expect) if expect else (body.get("success") is True and not code)
        passed += 1 if ok else 0
        lines.append(
            f"  {'PASS' if ok else 'FAIL'}  [{family}] {note}｜实际 errorCode={code!r}"
            f"（期望 {expect!r}）status={status}"
        )
    lines.append(f"\n成对结果：**{passed}/{len(CASES)}**")
    text = "\n".join(lines) + "\n"
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(text)
    print(text)
    print(f"written {args.out}")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
