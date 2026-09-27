# -*- coding: utf-8 -*-
"""M-6 前置：看真实失败用例的 summary.json 结构（只读）。"""

import json
import pathlib

base = pathlib.Path(r"D:\tester")
for rel in ("project-two/logs/probe_silent.summary.json", "project-two/logs/login_flow.summary.json"):
    path = base / rel
    if not path.exists():
        print(f"缺 {rel}")
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
    stat = data.get("stat") or {}
    print(f"\n== {rel}")
    print("  顶层键:", list(data)[:10])
    print("  success =", data.get("success"))
    print("  stat    =", {k: stat.get(k) for k in list(stat)[:8]})
    details = data.get("details") or []
    print(f"  details 条数 = {len(details)}")
    for item in details[:4]:
        record = (item.get("records") or [{}])[0]
        name = (record.get("name") or item.get("name") or "")
        print(f"    - {name[:40]:40} success={item.get('success')} 步骤={len(item.get('records') or [])}")

print("\n== project-two 目录 ==")
for child in sorted((base / "project-two").iterdir()):
    print("   ", child.name, "（目录）" if child.is_dir() else f"（{child.stat().st_size} 字节）")
