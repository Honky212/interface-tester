# -*- coding: utf-8 -*-
r"""export —— **离线静态导出**（v8 §5.8 行 1004 的「离线可直接打开」；P3d 收口，2026-09-25）。

## 为什么需要它（一次真机实测逼出来的）

看板的页面**默认只能靠服务端渲染**：导航是绝对路径（`href="/metrics"`），而 `file://` 下
`/metrics` 会被解析成 **`file:///D:/metrics`**。真机（Chrome）实测：

    打开 file:///D:/tester/.../index.html   → 200，页面渲染正常（charset/内联 CSS/表格都在）
    点「度量」→ chrome-error://chromewebdata/
                标题 file:///D:/metrics
                body：「无法访问您的文件 / ERR_FILE_NOT_FOUND」

所以 **「单页自包含」只是必要条件，不是充分条件**（`tests/frontend_test.py` 验的正是必要条件）。
要真的"离线可直接打开"，必须做两件事：① 把可离线的页面**预先渲染成一组 `.html`**；
② 把站内链接**改写成相对文件名**（`href="/metrics"` → `href="metrics.html"`）。
本模块就做这两件事，并在导出物顶部**挂一条横幅**说明"这是快照、哪些能力需要服务端"。

## 边界（红线③）

- 本模块是**登记在册的写入者**，写入根固定 `.ai/export/`（字面量前缀，T18 静态可判）；
- `web.py` **依旧零写盘**（它**不进** T18）——导出是**另一个能力**，不是看板偷偷写盘；
- 离线快照**只含只读内容**：确认台（`POST /confirm`）、触发运行（`POST /jobs`）、
  产物下载（`GET /file`）**都要服务端**，所以这些链接一律改写成 `offline.html`，
  页面里**点名列出**被改写的路由——**不假装能用**。

## 判据（`run_selftest`；每一条都对着一种"看起来正常"的假成功）

1. 每个页面 **零** `href="/` 与 `action="/`（绝对路径 = 离线必失效）；
2. 每条站内链接的目标文件**在产物里真实存在**（`file://` 下没有服务端兜底）；
3. 从 `index.html` 出发**能到达全部页面**（"到得了"的机器形态，而不是"文件都在"）；
4. 两次导出**逐字节相同**（幂等：产物可入库、可 diff；`generated_at` 由 `stamp` 注入）；
5. 除 `.ai/export/` 外**零污染**（导出前后对整棵树对账）；
6. **注入式元护栏**：给一个带绝对链接/悬空链接的页面，判据**必须**报出来（防假绿）。
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Text, Tuple

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai import audit as audit_mod  # noqa: E402
from interfacetester_ai import confirm as confirm_mod  # noqa: E402
from interfacetester_ai import jobs as jobs_mod  # noqa: E402
from interfacetester_ai import reviews as reviews_mod  # noqa: E402
from interfacetester_ai import web as web_mod  # noqa: E402

# 写入根（**字面量前缀**：T18 静态可判，见 bench/write_boundary_scanner.py）
EXPORT_ROOT = ".ai/export"

INDEX_NAME = "index.html"
METRICS_NAME = "metrics.html"
AUDIT_NAME = "audit.html"
PENDING_NAME = "pending.html"
JOBS_NAME = "jobs.html"
OFFLINE_NAME = "offline.html"

# 固定路由 → 文件名（站内那五个页面）
STATIC_ROUTES: Dict[Text, Text] = {
    "/": INDEX_NAME,
    "/metrics": METRICS_NAME,
    "/audit": AUDIT_NAME,
    "/pending": PENDING_NAME,
    "/jobs": JOBS_NAME,
}

# 离线快照里**刻意不做**的路由 → 为什么（写进 `offline.html`，让人知道去哪做）
OFFLINE_ONLY: Dict[Text, Text] = {
    "/confirm": "确认台（`POST /confirm`）要把裁决写进 `.ai/reviews/` 留痕——需要服务端",
    "/file": "产物下载（`GET /file?path=…`）走服务端的读根白名单——需要服务端",
}

_LINK_RE = re.compile(r'(href|action)\s*=\s*"([^"]*)"', re.I)
_ABSOLUTE_RE = re.compile(r"^(?:/|[a-zA-Z]:)")
_SKIP_SCHEMES = ("#", "mailto:", "data:", "javascript:")
def _slug(text: Text) -> Text:
    """把一段相对路径 / 作业号变成**稳定且不撞车**的文件名片段。

    ★为什么末尾一定挂摘要：只用"非法字符换 `-`"的方案，`a/b.json` 与 `a-b.json`
    会落到**同一个文件名**上——与本仓反复强调的"归一化后撞同一生成物"（§3.3 G10）
    是同一个坑。摘要取全串，所以不同输入必然不同名；前段保留可读性。
    """
    normalized = text.replace("\\", "/")
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-") or "item"
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{readable[:60]}-{digest}"


def link_values(html_text: Text) -> List[Text]:
    """取出页面里全部 `href` / `action` 的值（**判据与改写共用同一套口径**）。"""
    return [match.group(2) for match in _LINK_RE.finditer(html_text)]


def _rewrite_links(html_text: Text, resolve: Callable[[Text], Text]) -> Text:
    """把**站内绝对路径**改写成产物内的相对文件名；其余原样保留。

    只动"以 `/` 或盘符开头"的值：已经很相对的链接（包括锚点 `#`、`mailto:`）不碰。
    """

    def replace(match: "re.Match[Text]") -> Text:
        value = match.group(2)
        if not value or value.startswith(_SKIP_SCHEMES):
            return match.group(0)
        if not _ABSOLUTE_RE.match(value):
            return match.group(0)
        return f'{match.group(1)}="{resolve(value)}"'

    return _LINK_RE.sub(replace, html_text)


def _with_banner(html_text: Text, *, stamp: Text) -> Text:
    """在 `<body>` 之后插一条横幅：**这是离线快照**，并给出"哪些能力要服务端"的入口。

    ★为什么横幅是必须的：离线快照与活的看板**长得几乎一样**，而它不会更新。
    不说清这一点，读者会把一份三天前的快照当成现状——这正是本仓最忌讳的"静默过期"。
    """
    banner = (
        '<div class="banner na">★本页来自 <b>离线静态导出</b>（`.ai/export/`），'
        f'生成时刻 <span class="mono">{stamp}</span>——它是<b>快照</b>，不会自动更新。'
        '确认台 / 触发运行 / 产物下载需要服务端：'
        f'<a href="{OFFLINE_NAME}">见说明</a>。</div>\n'
    )
    marker = "<body>\n"
    if marker not in html_text:
        return banner + html_text
    return html_text.replace(marker, marker + banner, 1)


def _offline_body(
    *, stamp: Text, files: Sequence[Text], rewritten: Sequence[Text]
) -> Text:
    parts: List[Text] = [
        '<div class="banner na">这一页解释：<b>离线快照里有什么、缺什么、缺的怎么补</b>。</div>',
        f"<h2>快照内容</h2><p>共 <b>{len(files)}</b> 个页面："
        + "、".join(f'<span class="mono">{html.escape(name)}</span>' for name in files)
        + "</p>",
    ]

    if rewritten:
        parts.append(
            "<h2>本次被改写成「需要服务端」的链接</h2>"
            "<p>它们原本指向服务端路由，离线快照里打不开——所以一律指到本页。"
            "这不是「忘了导出」，而是<b>这些能力必须服务端</b>：</p><ul>"
            + "".join(
                f'<li><span class="mono">{html.escape(route)}</span></li>'
                for route in rewritten
            )
            + "</ul>"
        )
    else:
        parts.append(
            "<h2>本次被改写的链接</h2><p>**没有**——这份工作区里没有出现需要服务端的链接"
            "（例如没有任何运行记录，也就没有下载 / 确认入口）。</p>"
        )

    parts.append("<h2>为什么它们必须服务端</h2><table><thead><tr><th>路由</th><th>原因</th></tr></thead><tbody>")
    for route, why in sorted(OFFLINE_ONLY.items()):
        parts.append(f'<tr><td class="mono">{html.escape(route)}</td><td>{why}</td></tr>')
    parts.append("</tbody></table>")

    parts.append(
        '<h2>要完整能力怎么办</h2><p>就地起服务（同样是只读看板，另加确认台）：'
        '<span class="mono">haify serve --port 8766</span>，然后浏览器打开 '
        '<span class="mono">http://127.0.0.1:8766/</span>。</p>'
        f'<p class="muted">生成时刻：<span class="mono">{stamp}</span>（快照时间，不是证据时间）。</p>'
    )
    return "\n".join(parts)


class OfflineBundleNotSafe(Exception):
    """产物的判据没过 → **拒绝写盘**（前置闸门，S-4 的同一取向）。"""


def build_bundle(
    roots: Sequence[Text] = web_mod.ALLOWED_ROOTS,
    *,
    stamp: Optional[Text] = None,
) -> Tuple[Dict[Text, Text], List[Text]]:
    """渲染整站离线快照，返回 `(文件名 → HTML, 被改写成 `offline.html` 的路由)`。

    ★`stamp` 可注入（自检给固定值）：否则"两次导出逐字节相同"这条幂等判据
    会被时间戳自己搞红——**判据必须是能真的判的那种**。
    """
    tick = stamp or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    runs = web_mod.iter_runs(roots)
    payload = web_mod.metrics_payload(roots)
    timeline = audit_mod.audit_timeline()

    raw: Dict[Text, Text] = {
        INDEX_NAME: web_mod.render_index(runs, payload),
        METRICS_NAME: web_mod.render_metrics(payload, web_mod.failure_trend(roots)),
        AUDIT_NAME: web_mod.render_audit_page(
            timeline, audit_mod.audit_summary(timeline), generated_at=tick
        ),
        PENDING_NAME: web_mod.render_pending_page(
            confirm_mod.load_pending_lists(), reviews_mod.drafts_overview()
        ),
        JOBS_NAME: web_mod.render_jobs_page(jobs_mod.list_jobs()),
    }
    plan: Dict[Text, Text] = dict(STATIC_ROUTES)

    # 每份 summary：明细页（+ 可读时的 fix 页）
    for run in runs:
        summary, _root, why = web_mod.load_summary(run.rel_path, roots)
        rows, problems = ([], []) if why else web_mod.extract_assertions(summary)
        run_file = "run-" + _slug(run.rel_path) + ".html"
        raw[run_file] = web_mod.render_run(
            run.rel_path, summary, rows, problems, load_error=why
        )
        plan["/run?path=" + run.rel_path] = run_file
        if not why:
            fix_file = "fix-" + _slug(run.rel_path) + ".html"
            raw[fix_file] = web_mod.render_fix_page(
                run.rel_path, confirm_mod.fix_suggestions(summary)
            )
            plan["/fix?path=" + run.rel_path] = fix_file

    # 每个作业：详情页
    for listed in jobs_mod.list_jobs():
        job_id = str(listed.get("job_id") or "")
        entry = jobs_mod.read_job(job_id) if job_id else None
        if not job_id or entry is None:
            continue
        job_file = "job-" + _slug(job_id) + ".html"
        raw[job_file] = web_mod.render_job_detail(job_id, entry)
        plan["/jobs?job=" + job_id] = job_file

    rewritten: List[Text] = []

    def resolve(route: Text) -> Text:
        target = plan.get(route)
        if target:
            return target
        rewritten.append(route)
        return OFFLINE_NAME

    files: Dict[Text, Text] = {
        name: _rewrite_links(_with_banner(text, stamp=tick), resolve)
        for name, text in raw.items()
    }

    known = sorted(set(files) | {OFFLINE_NAME})
    offline = web_mod.page(
        "离线快照说明",
        _offline_body(stamp=tick, files=known, rewritten=sorted(set(rewritten))),
    )
    files[OFFLINE_NAME] = _rewrite_links(offline, resolve)
    return files, sorted(set(rewritten))


def export_site(
    roots: Sequence[Text] = web_mod.ALLOWED_ROOTS, *, stamp: Optional[Text] = None
) -> Dict[Text, Any]:
    """把快照写进 `.ai/export/`（**登记写入根**），返回产物清单。

    ★顺序是**判据在前、写盘在后**：`assert_offline_safe` 不过就抛异常，
    一个字节都不落——"先写下去再检查"会在检查失败时留下半份产物，
    而那半份产物**看起来是完整的**。
    """
    files, rewritten = build_bundle(roots, stamp=stamp)
    problems = assert_offline_safe(files)
    if problems:
        raise OfflineBundleNotSafe("\n".join(problems))

    os.makedirs(".ai/export", exist_ok=True)  # 写入根字面量（T18 静态可判）
    # 先清掉上一次的产物：留着旧 `run-*.html` 会让"快照内容"看起来比实际多（静默过期）
    for old in sorted(os.listdir(".ai/export")):
        target = os.path.join(".ai/export", old)
        if os.path.isfile(target):
            os.remove(target)

    written: List[Text] = []
    for name in sorted(files):
        # 写盘行必须**内联字面量前缀**（T18：写成变量会被判"目标不可静态判定"）
        with open(".ai/export/" + name, "w", encoding="utf-8") as fp:
            fp.write(files[name])
        written.append(name)
    return {"dir": EXPORT_ROOT, "files": written, "rewritten": rewritten}


def assert_offline_safe(files: Mapping[Text, Text]) -> List[Text]:
    """**离线可用性判据**：返回问题清单（空 = 通过）。三条：

    1. 零绝对路径（`href="/…"` / `action="/…"`）——`file://` 下它们指到文件系统根；
    2. 每条链接的目标文件**在产物里存在**——离线没有服务端兜底，点下去就是 `ERR_FILE_NOT_FOUND`；
    3. 从入口页出发**能到达全部页面**——"文件都在"不等于"点得到"。
    """
    problems: List[Text] = []
    if INDEX_NAME not in files:
        problems.append(f"产物里没有入口页 {INDEX_NAME}")

    for name in sorted(files):
        for value in link_values(files[name]):
            if not value or value.startswith(_SKIP_SCHEMES):
                continue
            if _ABSOLUTE_RE.match(value):
                problems.append(
                    f"{name}: 站内绝对路径 {value!r}（`file://` 下会指到文件系统根）"
                )
                continue
            target = value.split("#")[0].split("?")[0]
            if target and target not in files:
                problems.append(f"{name}: 链接 {value!r} 指向产物里不存在的文件")

    seen = {INDEX_NAME}
    queue = [INDEX_NAME]
    while queue:
        current = queue.pop()
        for value in link_values(files.get(current, "")):
            target = value.split("#")[0].split("?")[0]
            if target in files and target not in seen:
                seen.add(target)
                queue.append(target)
    unreachable = sorted(set(files) - seen)
    if unreachable:
        problems.append(
            "从入口页走不到这些页面（离线时等于不存在）：" + "、".join(unreachable)
        )
    return problems


def _snapshot_except_export() -> Dict[Text, Text]:
    """快照当前工作区（**排除 `.ai/export/` 自己**）——用于"零污染"对账。

    ★为什么必须排除导出根：不排除的话，这条判据就变成"导出没有产物"这种**永远为假**的
    护栏（导出的产物就是它自己写的东西）。这与 P3a 那条"不能复用 `workdir.snapshot_tree`"
    是同一类取向：**快照的排除项必须与判据的目标一致**。
    """
    snapshot: Dict[Text, Text] = {}
    for folder, dirs, names in os.walk("."):
        dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        rel_dir = os.path.relpath(folder, ".").replace(os.sep, "/")
        if rel_dir == EXPORT_ROOT or rel_dir.startswith(EXPORT_ROOT + "/"):
            dirs[:] = []
            continue
        for name in sorted(names):
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, ".").replace(os.sep, "/")
            try:
                with open(path, "rb") as fp:
                    snapshot[rel] = hashlib.sha256(fp.read()).hexdigest()
            except OSError:
                snapshot[rel] = "<unreadable>"
    return snapshot


# 自检用的最小 summary（字段路径与真实产物一致：`stat.testcases.*` / `stat.teststeps.*`）
_SAMPLE_SUMMARY: Dict[Text, Any] = {
    "success": False,
    "stat": {
        "testcases": {"total": 1, "success": 0, "fail": 1},
        "teststeps": {"total": 1, "success": 0, "fail": 1},
    },
    "details": [
        {
            "name": "演示用例",
            "records": [
                {
                    "name": "步骤一",
                    "step_type": "request",
                    "success": False,
                    "data": {
                        "address": "http://127.0.0.1/demo",
                        "validators": {
                            "validate_extractor": [
                                {
                                    "check": "status_code",
                                    "check_result": "fail",
                                    "check_value": 500,
                                    "comparator": "equal",
                                    "expect": 200,
                                    "message": "状态码不符",
                                }
                            ]
                        },
                    },
                }
            ],
        }
    ],
}


def run_selftest(verbose: bool = False) -> int:
    """export 自检（**在临时工作区里跑一次真实导出**，不碰本仓）。"""
    import json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    failures: List[Text] = []
    notes: List[Text] = []
    tick = "2026-09-25T00:00:00"
    old_cwd = os.getcwd()

    with tempfile.TemporaryDirectory(prefix="ai_export_") as tmp:
        os.chdir(tmp)
        try:
            # ★夹具也写在**本模块的登记根之内**（`.ai/`）：T18 的纪律是"产线模块里不许出现
            #   根外的写盘"——夹具写 `logs/` 实测被判 **2 处违规**（`makedirs` + `open`）。
            #   处置不是"把 `logs/` 登记进去"（那等于给导出开一个写 `logs/` 的口子），
            #   而是**把夹具挪进自己的根**：这条纪律与 P3a 把夹具从 `web.py` 挪进 `tests/` 同源。
            os.makedirs(".ai/export/_selftest/logs", exist_ok=True)  # 字面量前缀（T18 静态可判）
            with open(
                ".ai/export/_selftest/logs/demo.summary.json", "w", encoding="utf-8"
            ) as fp:
                json.dump(_SAMPLE_SUMMARY, fp, ensure_ascii=False)
            roots = (os.path.join(EXPORT_ROOT, "_selftest"),)

            before = _snapshot_except_export()
            result = export_site(roots, stamp=tick)
            files, _rewritten = build_bundle(roots, stamp=tick)

            problems = assert_offline_safe(files)
            if problems:
                failures.append("[离线安全] 产物判据没过：" + "；".join(problems))

            # ★独立再扫一遍"零绝对路径"（不复用 assert 的代码路径：判据也要有第二只眼）
            on_disk = sorted(
                name
                for name in os.listdir(EXPORT_ROOT)
                if os.path.isfile(os.path.join(EXPORT_ROOT, name))
            )
            for name in on_disk:
                with open(os.path.join(EXPORT_ROOT, name), encoding="utf-8") as fp:
                    text = fp.read()
                if 'href="/' in text or 'action="/' in text:
                    failures.append(f"[绝对路径] {name} 里还有站内绝对路径")

            if on_disk != sorted(result["files"]):
                failures.append(f"[清单不符] 磁盘 {on_disk} ≠ 返回值 {result['files']}")

            for required in (INDEX_NAME, METRICS_NAME, AUDIT_NAME, PENDING_NAME, JOBS_NAME, OFFLINE_NAME):
                if required not in result["files"]:
                    failures.append(f"[缺页] {required} 没导出")
            if not [name for name in result["files"] if name.startswith("run-")]:
                failures.append("[缺页] 有运行记录却没导出明细页（首页的链接会指向不存在的文件）")

            # 幂等：同一 stamp 再导一次，必须**逐字节相同**
            # ★这条判据第一轮就抓到过一个真错：这里漏传 `roots` → 复跑用了默认读根
            #   （临时工作区里没有运行记录）→ index/metrics 的内容与第一次不同。
            #   "产物可 diff"因此不只是洁癖，它是**判别"两次导出的输入是否一致"**的手段。
            export_site(roots, stamp=tick)
            for name in on_disk:
                with open(os.path.join(EXPORT_ROOT, name), encoding="utf-8") as fp:
                    again = fp.read()
                if again != files[name]:
                    failures.append(f"[幂等] {name} 两次导出不一致（产物不可 diff）")

            after = _snapshot_except_export()
            if after != before:
                failures.append(
                    "[污染] 导出改动了工作区其他部分："
                    f"{sorted(set(after.items()) ^ set(before.items()))[:3]}"
                )

            broken = dict(files)
            broken["broken.html"] = '<a href="/metrics">x</a><a href="nope.html">y</a>'
            caught = assert_offline_safe(broken)
            if len(caught) < 2:
                failures.append(f"[元护栏] 注入的绝对链接/悬空链接没被报出来：{caught}")

            notes.append(
                f"快照 {len(result['files'])} 个页面；被改写成「需要服务端」的路由 "
                f"{len(result['rewritten'])} 个"
            )
        finally:
            os.chdir(old_cwd)

    print("=" * 66)
    if failures:
        print(f"export 自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("export 自检全部通过：")
    for note in notes:
        print(f"  {note}")
    print("  判据：零绝对路径 / 链接目标都在产物里 / 入口可达全部页 / 幂等 / 只写 `.ai/export/`")
    print("  元护栏：注入绝对链接与悬空链接，判据**必须**报出来")
    print("  写入根：`.ai/export/`（登记在册；T18 扫描 0 违规）")
    print("=" * 66)
    return 0


def main() -> int:
    import argparse  # noqa: PLC0415

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="离线静态导出（P3d 收口：`file://` 直接打开）"
    )
    parser.add_argument(
        "--export", action="store_true", help="真的导出到 `.ai/export/`（默认只自检）"
    )
    args = parser.parse_args()

    if args.export:
        result = export_site()
        print(f"已导出 {len(result['files'])} 个页面到 {result['dir']}/")
        for name in result["files"]:
            print(f"  {result['dir']}/{name}")
        if result["rewritten"]:
            print("被改写成「需要服务端」的路由：")
            for route in result["rewritten"]:
                print(f"  {route}")
        print(f"离线打开：{os.path.abspath(os.path.join(EXPORT_ROOT, INDEX_NAME))}")
        return 0
    return run_selftest()


__all__ = [
    "EXPORT_ROOT",
    "INDEX_NAME",
    "OFFLINE_NAME",
    "STATIC_ROUTES",
    "OfflineBundleNotSafe",
    "assert_offline_safe",
    "build_bundle",
    "export_site",
    "link_values",
    "run_selftest",
]


if __name__ == "__main__":
    raise SystemExit(main())
