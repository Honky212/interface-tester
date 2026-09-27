"""对抗探针：检验 S3 三态与 S4 引用判定是否按 §3.3 判据工作（T1 判据的实测载体）。

三个用例对应 §12.5/§12.7 的三例：
  ① 登录步骤只有 extract、无 validate（多步链路最常见写法）
     → 期望：通过且零 PENDING（extract 的键被下游 $token / check 引用，T21 判据①）
  ② 按闸门建议写 validate: []（显式声明只是取 token）
     → 期望：S3 只报 PENDING（三态之②），不再 REJECT
  ③ 对照：登录步骤补一条 eq: [status_code, 200]
     → 期望：通过

只读探针，不写任何被测文件；输出留档于本目录 out_probe_s3_edge.txt（T25）。
"""

import sys
import os

# T26（方案 v8 §12.8-①）同款：重定向/管道/CI 下 Windows 默认 GBK 编码，
# ✓/→ 等字符会抛 UnicodeEncodeError → 退出码 1（假红）。钉为 UTF-8 保取证可复现。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench.silent_traps import check_testcase  # noqa: E402


def case(validate_line):
    steps = [
        {
            "name": "登录并提取 token（该步骤只负责取 token）",
            "request": {
                "method": "POST",
                "url": "/api/login",
                "json": {"username": "u", "password": "p"},
            },
            "extract": {"token": "body.data.token"},
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
                {"type_match": ["body.data.token", "str"]},
            ],
        },
    ]
    if validate_line is not None:
        # 按闸门自己的建议显式声明意图
        steps[0]["validate"] = validate_line
    return {"config": {"name": "两步链路", "base_url": "http://127.0.0.1:8000"}, "teststeps": steps}


def show(title, tc):
    rep = check_testcase(tc, path="<probe>")
    print(f"--- {title} ---")
    print("  findings:", [f.code for f in rep.findings])
    print("  REJECT count:", len(rep.rejects), " PENDING count:", len(rep.pendings))
    for f in rep.findings:
        print(f"    [{f.severity}] {f.code} @ {f.where}: {f.message.splitlines()[0]}")
        if f.hint:
            print(f"        提示: {f.hint.splitlines()[0]}")
    print("  闸门结果:", "通过" if rep.ok else "被拒")
    print()


show("① 登录步骤只有 extract、无 validate（多步链路常见写法）", case(None))
show("② 按闸门建议写 validate: []（显式声明只是取 token）", case([]))
show("③ 对照：登录步骤补一条 eq: [status_code, 200]", case([{"eq": ["status_code", 200]}]))
