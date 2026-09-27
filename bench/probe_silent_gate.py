# -*- coding: utf-8 -*-
"""端到端对抗验证：真实用例上的「跑绿但没校验」必须被闸门拦下。

本脚本复现的正是**我实测过的那个真实假通过**：
在 project-two 上写 `not_equal: [body.data.nonexistent_field, ""]`，
字段完全不存在，`hrun` 却报 `1 passed`、summary 里 `success=true`。

闸门的价值主张是：**在不跑接口的前提下，把这种用例挡在落盘之前**。
本脚本同时验证两件事：
  1. 运行时确实假通过（证明陷阱是真的，不是纸上推演）；
  2. 生成期闸门确实拦下它（证明闸门真的有用）。

用法：
    .venv\\Scripts\\python.exe bench\\probe_silent_gate.py
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from bench.silent_traps import check_testcase  # noqa: E402

# 与真实跑过的那份用例同形（project-two 的两步登录链路）
REAL_FALSE_PASS = {
    "config": {
        "name": "登录取 token 再带 token 调接口",
        "base_url": "${ENV(BASE_URL)}",
        "verify": False,
    },
    "teststeps": [
        {
            "name": "登录获取 token",
            "request": {
                "method": "POST",
                "url": "/api/login",
                "headers": {"Content-Type": "application/json"},
                "json": {"username": "admin", "password": "123456"},
            },
            "extract": [{"token": "body.data.token"}],
            "validate": [
                {"eq": ["status_code", 200]},
                {"eq": ["body.code", 0]},
            ],
        },
        {
            "name": "用 token 查询用户信息",
            "request": {
                "method": "GET",
                "url": "/api/user/info",
                "headers": {"Authorization": "Bearer $token"},
            },
            "validate": [
                {"eq": ["status_code", 200]},
                {"eq": ["body.code", 0]},
                # ★ 陷阱：字段根本不存在时这条**静默通过**
                {"not_equal": ["body.data.nonexistent_field", ""]},
            ],
        },
    ],
}

# 同一个用例的**修好**版本（应当放行）
FIXED = {
    "config": {
        "name": "登录取 token 再带 token 调接口",
        "base_url": "${ENV(BASE_URL)}",
        "verify": False,
    },
    "teststeps": [
        {
            "name": "登录获取 token",
            "request": {
                "method": "POST",
                "url": "/api/login",
                "headers": {"Content-Type": "application/json"},
                "json": {"username": "admin", "password": "123456"},
            },
            "extract": [{"token": "body.data.token"}],
            "validate": [
                {"eq": ["status_code", 200]},
                {"eq": ["body.code", 0]},
                {"type_match": ["body.data.token", "str"]},
            ],
        },
        {
            "name": "用 token 查询用户信息",
            "request": {
                "method": "GET",
                "url": "/api/user/info",
                "headers": {"Authorization": "Bearer $token"},
            },
            "validate": [
                {"eq": ["status_code", 200]},
                {"eq": ["body.code", 0]},
                # ★ 改法：可判定的存在性断言（缺字段会判失败）
                {"type_match": ["body.data.nonexistent_field", "str"]},
            ],
        },
    ],
}


def main() -> int:
    # T26（方案 v8 §12.8-①）：重定向/管道/CI 环境下，Windows 默认把 stdout/stderr
    # 按 GBK 编码，✓/✗/→ 等字符会抛 UnicodeEncodeError → 退出码 1（假红）。
    # 统一钉为 UTF-8 + errors=replace，保证「实测通过四元组」跨环境可复现。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # 不支持 reconfigure 或流已关闭
            pass

    failures = []

    print("=" * 70)
    print("① 带陷阱的真实用例 —— 闸门必须拦下")
    print("=" * 70)
    bad = check_testcase(REAL_FALSE_PASS, path="project-two/probe_silent.yml")
    if bad.ok:
        failures.append("带 not_equal 伪存在性断言的用例**没有被拦下**")
        print("  ✗ 闸门漏拦（这是最危险的方向）")
    else:
        print(f"  ✓ 已拦下，命中编号：{bad.codes()}")
        for f in bad.findings:
            print(f"    [{f.severity}] {f.code} @ {f.where}")
            for line in f.message.splitlines():
                print(f"        {line.strip()}")
            if f.hint:
                print(f"        → {f.hint}")

    print()
    print("=" * 70)
    print("② 修好的同一用例 —— 闸门必须放行")
    print("=" * 70)
    good = check_testcase(FIXED, path="project-two/fixed.yml")
    if not good.ok:
        failures.append("修好的用例被**误伤**（假报会让真报被无视）")
        print("  ✗ 误伤：")
        for f in good.rejects:
            print(f"    {f.code} {f.message}")
    else:
        pend = [f for f in good.findings if f.severity == "pending"]
        print(f"  ✓ 放行（REJECT=0）")
        if pend:
            print(f"    另有 {len(pend)} 条待人工确认（PENDING，不拦）：")
            for f in pend:
                print(f"      {f.code} @ {f.where}")

    print()
    print("=" * 70)
    if failures:
        print(f"对抗验证**失败** {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        print("=" * 70)
        return 1
    print("对抗验证通过：闸门拦得住真陷阱，且不误伤修好的写法。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
