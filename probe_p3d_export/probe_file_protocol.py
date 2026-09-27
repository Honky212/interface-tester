# -*- coding: utf-8 -*-
"""P3d 第 6a 条取证：把只读看板的各页面**渲染成静态 .html**，再用真浏览器以 `file://` 打开。

★留档说明（照 `probe_s3_review/` 的先例）：本探针的输出 **不删**——
    `before_fix_site/` 是**修复前**的 5 页原文（它们带着绝对路径导航，是"单页能读、点不到"
    这个结论的一手材料）；三张真机截图同目录。真机四元组见方案 v8 §12.8-⑧。

★为什么写在这里而不是 `web.py` 里：`web.py` 是**零写盘**的（红线：它不在 T18 注册表里）。
   本脚本只是**取证探针**，不构成产品能力（产品能力是 `interfacetester_ai/export.py`）。
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
os.chdir(BASE)

from interfacetester_ai import audit as audit_mod  # noqa: E402
from interfacetester_ai import confirm as confirm_mod  # noqa: E402
from interfacetester_ai import jobs as jobs_mod  # noqa: E402
from interfacetester_ai import web as w  # noqa: E402

OUT = os.path.join(BASE, ".tmp_static_probe", "site")
os.makedirs(OUT, exist_ok=True)

roots = w.ALLOWED_ROOTS
runs = w.iter_runs(roots)
payload = w.metrics_payload(roots)
timeline = audit_mod.audit_timeline()

pages = {
    "index.html": w.render_index(runs, payload),
    "metrics.html": w.render_metrics(payload, w.failure_trend(roots)),
    "audit.html": w.render_audit_page(
        timeline,
        audit_mod.audit_summary(timeline),
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    ),
    "pending.html": w.render_pending_page(confirm_mod.load_pending_lists()),
    "jobs.html": w.render_jobs_page(jobs_mod.list_jobs()),
}

# 运行明细页（每份 summary 一页）——首页的链接指向的是 `/run?path=…`
for index, run in enumerate(runs):
    summary, _root, why = w.load_summary(run.rel_path, roots)
    rows, problems = ([], []) if why else w.extract_assertions(summary)
    pages[f"run-{index}.html"] = w.render_run(
        run.rel_path, summary, rows, problems, load_error=why
    )

for name, text in pages.items():
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as fp:
        fp.write(text)

print("runs =", len(runs))
print("pages =", len(pages), sorted(pages))
# ★当场数一遍"绝对路径链接"：`file://` 下它们会指向文件系统根，必然到不了
for name, text in sorted(pages.items()):
    absolute = text.count('href="/') + text.count("action=\"/")
    print(f"  {name}: 绝对路径链接 {absolute} 个；外部资源引用 "
          f"{text.count('http://') + text.count('https://')} 个")
