# -*- coding: utf-8 -*-
"""**生成 → 真跑一遍**的端到端驱动（只读产品代码，不改它；用的全是**用户会走的命令**）。

    用法：
      python probe_p2_prep/e2e_run_generated.py <工作区> \
          [--drafts <草稿目录>] [--base-url http://127.0.0.1:8899] [--approver e2e]

它做四件事：

1. **搬草稿**：把 `<草稿目录>/.ai/draft/*.yml`（默认 = 那次 `gemma4:latest` 真实复跑的产物）
   复制进新工作区的 `.ai/draft/`；
2. **补目标地址**：往每份的 `config` 里写 `base_url` —— ★那次复跑早于 `--base-url` 参数，
   所以这一步就是**人工转正时会做的编辑**（生成侧现在有 `--base-url`，见 §9.13）；
3. **逐份转正**：`haify review --approve <用例> --approver <人> [--resolve-blocker <码>...]`
   —— 要处置哪些码**由产品自己的口径算**（`quality` 的 blocker 映射），不在这里另立一份判断；
4. **逐份真跑**：`haify run cases/<用例>.yml`（走 `jobs.run_job`：L3 闸门 → 子进程 → 结果留痕），
   失败的那几条再单独跑一次内核 `interfacetester.cli run`，把**断言级的差异**抓出来。

★它服务端只用 `mock_file_service.py`（**从同一份文档推导**出来的 mock，不看用例）——
"跑绿"证明的是"**文档 → 用例 → 跑**"这条链在"服务端照文档实现"时是通的，
**不证明**用例的断言语义有多深（深度另由覆盖矩阵与 T28 降级率量）。
"""

import argparse
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.quality import _blockers_from_unknowns  # noqa: E402

DRAFTS_DEFAULT = os.path.join(os.environ.get("TEMP", "/tmp"), "ai_real_run", ".ai", "draft")

# 用例里用到的 `${ENV(...)}`：真跑时必须有值（mock 不看这些值，但**变量必须存在**）
# ★`TEST_PATH` 必须与 `X_PATH` 一致：文档写明了「`path` 必须与请求头 `X-Path` 完全一致，否则返回
#   `FILE_1018`」——mock 现在**照这条判**，env 自相矛盾就会把**正例**判成错误 ✗
#   （实测踩到：T4_2 的正例步骤因为 /app1/documents/ vs /app1/ 被判 FILE_1018）。
#   取值用的是文档自己的查询示例 `path=/app1/` + `X-Path: /app1/` ✓。
RUN_ENV = {
    "AUTH_HEADER": "Bearer e2e-token",
    "AUTH_TOKEN": "e2e-token",
    "X_PATH": "/app1/",
    "CLIENT_ID": "e2e-client",
    "CLIENT_SECRET": "e2e-secret",
    "TEST_FILENAME": "report.pdf",
    "TEST_PATH": "/app1/",
    "TEST_VERSION": "v1",
    "NON_EXISTENT_VERSION": "v999",
}


def _run(cmd, cwd, env):
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def inject_base_url(path, base_url):
    """把 `base_url` 写进 `config`（已有就不动）。返回是否注入。"""
    text = io.open(path, encoding="utf-8").read()
    if re.search(r"(?m)^\s*base_url:", text):
        return False
    lines = text.split("\n")
    out = []
    injected = False
    for index, line in enumerate(lines):
        out.append(line)
        previous = lines[index - 1].strip() if index else ""
        if not injected and line.strip().startswith("name:") and previous == "config:":
            out.append(f"  base_url: {base_url}")
            injected = True
    io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out))
    return injected


def blockers_of(unknowns_path):
    """用**产品自己的口径**算这份草稿要处置哪些 blocker 码。"""
    if not os.path.isfile(unknowns_path):
        return []
    try:
        payload = json.load(io.open(unknowns_path, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    blockers, _warnings = _blockers_from_unknowns(payload.get("items") or [])
    return list(blockers)


def _classify_steps(steps):
    """把步骤名分成 `(正例, 负例)`：负例的关键词表（**判不了就算正例**，宁可少算）。"""
    negative_words = ("失败", "错误", "不存在", "不匹配", "不一致", "非法", "无效", "超出", "超过",
                      "大于", "小于", "为空", "不能", "重复", "已存在", "过期", "锁定")
    good, bad = [], []
    for name in steps:
        (bad if any(word in name for word in negative_words) else good).append(name)
    return good, bad


def _rule_summary(doc_path):
    """★§9.21 错误条件引擎的**可模拟 / 不模拟**清单（直接从探针模块里取，口径只有一处）。"""
    try:
        sys.path.insert(0, os.path.join(BASE, "probe_p2_prep"))
        import mock_file_service  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 取不到就不报（脚本不因此失败）
        return None
    try:
        text = io.open(doc_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    rules, unbound = mock_file_service.build_error_rules(text)
    statements = [item for item in unbound if mock_file_service.is_condition_statement(item[1])]
    return {
        "bound": sum(len(items) for items in rules.values()),
        "routes": len([key for key, items in rules.items() if items]),
        "unbound": unbound,
        "statements": statements,
    }


def _step_names(case_path):
    """用例 YAML 里的**步骤名**列表（失败时一起打印——"负例步骤"跑不绿是服务端的锅，要能一眼分开）。"""
    if not os.path.isfile(case_path):
        return []
    names = []
    for line in io.open(case_path, encoding="utf-8", errors="replace"):
        match = re.match(r"^\s*-\s*name:\s*(.+?)\s*$", line)
        if match:
            names.append(match.group(1))
    return names


def main():  # noqa: C901
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="生成 → 真跑一遍（端到端）")
    parser.add_argument("workspace")
    parser.add_argument("--drafts", default=DRAFTS_DEFAULT)
    parser.add_argument("--base-url", default="http://127.0.0.1:8899")
    parser.add_argument("--approver", default="e2e")
    parser.add_argument(
        "--record",
        default=os.environ.get("MOCK_RECORD", ""),
        help="mock 的请求回读文件（JSONL）——**参数真的发出去了**的唯一证明（§9.16）",
    )
    parser.add_argument(
        "--doc",
        default="",
        help="接口文档路径（§9.21：错误条件可模拟/不模拟清单从这里推导；默认取探针模块的 DOC_DEFAULT）",
    )
    parser.add_argument(
        "--out",
        default=os.path.join(BASE, "probe_p2_prep", "out_e2e_run.txt"),
        help="报告落点（默认覆盖 out_e2e_run.txt；换通路跑时请换一个名字，别把上一次的结论冲掉）",
    )
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    os.makedirs(os.path.join(ws, ".ai", "draft"), exist_ok=True)
    report = [
        "=" * 74,
        f"工作区：{ws}",
        f"草稿源：{args.drafts}",
        f"目标  ：{args.base_url}",
        "=" * 74,
    ]

    env = dict(os.environ)
    env.update(RUN_ENV)
    env["BASE_URL"] = args.base_url
    env["PYTHONPATH"] = BASE
    # ★L3 闸门（§五 L3）：真发请求必须**显式**开启沙盒，否则 `haify run` 会被拒（`l3_refused`）
    #   —— 这是本仓**有意**的保险，不是缺陷；脚本替人把这一步做掉并写在这里。
    env["INTERFACETESTER_AI_SANDBOX"] = "on"

    copied = 0
    for pattern in ("*.yml", "*.yaml", "*.unknowns.json"):
        for src in glob.glob(os.path.join(args.drafts, pattern)):
            shutil.copy2(src, os.path.join(ws, ".ai", "draft", os.path.basename(src)))
            copied += 1
    report.append(f"\n① 搬入 {copied} 个文件（草稿 + unknowns）")

    injected = 0
    cases = []
    for path in sorted(glob.glob(os.path.join(ws, ".ai", "draft", "*.yml"))):
        if inject_base_url(path, args.base_url):
            injected += 1
        cases.append(os.path.splitext(os.path.basename(path))[0])
    report.append(f"② 补 `base_url` 到 {injected}/{len(cases)} 份草稿（= 人工转正时的编辑）")

    report.append("\n③ 转正（`haify review --approve`）")
    promoted = []
    for case in cases:
        codes = blockers_of(os.path.join(ws, ".ai", "draft", case + ".unknowns.json"))
        cmd = [
            sys.executable, "-m", "interfacetester_ai", "review",
            "--approve", case, "--approver", args.approver,
        ]
        for code in codes:
            cmd += ["--resolve-blocker", code]
        done = _run(cmd, ws, env)
        ok = done.returncode == 0
        if ok:
            promoted.append(case)
        tail = "" if ok else "  " + ((done.stdout or "").strip().splitlines() or [""])[-1][:90]
        report.append(
            f"  {'OK ' if ok else 'NO '} {case:30} exit={done.returncode}"
            f"{('  处置=' + ','.join(codes)) if codes else ''}{tail}"
        )

    report.append(f"\n④ 真跑（`haify run`）—— 共 {len(promoted)} 条")
    passed = []
    for case in promoted:
        done = _run(
            [
                sys.executable, "-m", "interfacetester_ai", "run", f"cases/{case}.yml",
                "--approver", args.approver, "--base-url", args.base_url,
            ],
            ws,
            env,
        )
        ok = done.returncode == 0
        if ok:
            passed.append(case)
        reason = ""
        if not ok:
            # ★被 L3 闸门拒的（`l3_refused`）要**把拒因打出来**——否则只看到一个退出码，
            #   人会以为是"用例跑挂了"，而其实是"没开沙盒"（实测踩到）。
            for line in (done.stdout or "").splitlines():
                if "拒" in line or "refused" in line.lower():
                    reason = "  " + line.strip()[:100]
                    break
        report.append(f"  {'PASS' if ok else 'FAIL'} {case:30} exit={done.returncode}{reason}")

    failed = [case for case in promoted if case not in passed]
    if failed:
        report.append(f"\n⑤ 失败明细（{len(failed)} 条，内核 pytest 输出尾部 + **本用例的步骤名**）")
        report.append(
            "    ★步骤名要一起看：像「路径不匹配时返回错误码」这种**负例步骤**，"
            "mock 只照文档的**成功响应**应答 → 它**跑不绿是服务端没模拟错误条件**，"
            "不是断言写错了（当前这条链还没做「按错误条件触发失败」）。"
        )
        for case in failed:
            steps = _step_names(os.path.join(ws, "cases", case + ".yml"))
            report.append(f"\n  --- {case} ---")
            if steps:
                report.append("    步骤：" + " / ".join(steps))
            done = _run(
                [sys.executable, "-m", "interfacetester.cli", "run", f"cases/{case}.yml"], ws, env
            )
            text = (done.stdout or "") + (done.stderr or "")
            keep = [
                line
                for line in text.splitlines()
                if re.search(r"assert|AssertionError|E   |failed|passed", line)
            ]
            for line in keep[-8:]:
                report.append("    " + line.strip()[:150])

    if args.record and os.path.isfile(args.record):
        entries = [
            json.loads(line)
            for line in io.open(args.record, encoding="utf-8")
            if line.strip()
        ]
        head_keys = ("x-path", "authorization", "content-type")
        with_headers = [
            item
            for item in entries
            if any(key.lower() in head_keys for key in item["headers"])
        ]
        with_body = [item for item in entries if item["body"] not in (None, {}, "")]
        report += [
            f"\n⑥ **请求侧回读**（mock 照录到的请求：{len(entries)} 条）",
            f"  带文档声明的请求头（X-Path / Authorization / Content-Type）：{len(with_headers)} 条",
            f"  带请求体：{len(with_body)} 条",
            "  ★本节是「**参数真的发出去了**」的唯一证明：mock **不校验**参数，"
            "所以「没发参数」在跑分上照样是**绿**的（静默假通过）——只能靠回读。",
            "  ★条数是**累计**的（⑤ 的失败明细会把用例再跑一遍）。",
        ]
        for item in entries[:8]:
            body = (
                json.dumps(item["body"], ensure_ascii=False)[:70] if item["body"] else "（无）"
            )
            report.append(
                f"  {item['method']:5} {item['path'][:44]:46} "
                f"headers={sorted(item['headers'])} body={body}"
            )

    # ★§9.21 **正例步 / 负例步分开统计**（负例步能不能跑，取决于服务端有没有模拟错误条件）
    with_negative, without_negative = [], []
    for case in promoted:
        _good, bad = _classify_steps(_step_names(os.path.join(ws, "cases", case + ".yml")))
        (with_negative if bad else without_negative).append(case)
    doc_path = args.doc or os.environ.get("MOCK_DOC", "")
    rules = _rule_summary(doc_path) if doc_path else None
    report += [
        f"\n⑦ **错误条件（负例可验证性）**",
        f"  含负例步的用例：{len(with_negative)} 条"
        f"（真跑通过 {len([c for c in with_negative if c in passed])}）"
        f"；纯正例用例：{len(without_negative)} 条"
        f"（真跑通过 {len([c for c in without_negative if c in passed])}）",
        "  ★负例步跑不绿**不等于用例错**：要看服务端有没有按文档的错误条件返回失败响应。",
    ]
    if rules:
        report.append(
            f"  ★§9.21/§9.22 错误条件引擎：**可模拟 {rules['bound']} 条**（{rules['routes']} 个接口；"
            "五族：关系/非空/上限/存在性/格式）；**不模拟 "
            f"{len(rules['unbound'])} 条** = "
            f"**条件陈述 {len(rules['statements'])} 条**（能力边界）+ 示例碎片 "
            f"{len(rules['unbound']) - len(rules['statements'])} 条"
        )
        for _where, sentence in rules["statements"][:6]:
            report.append(f"      · 不模拟（条件陈述）：{sentence[:76]}")
    if args.record and os.path.isfile(args.record):
        hits = [
            item.get("triggered_rule", "")
            for item in (
                json.loads(line)
                for line in io.open(args.record, encoding="utf-8")
                if line.strip()
            )
            if item.get("triggered_rule")
        ]
        if hits:
            report.append(f"  ★mock 本次**实际命中**的错误条件 {len(hits)} 次（照录在回读文件里）：")
            for hit in hits[:4]:
                report.append(f"      · {hit}")

    rate = f"{len(passed) / len(promoted):.0%}" if promoted else "n/a"
    report += [
        "",
        "## 汇总",
        f"  转正成功      ：{len(promoted)}/{len(cases)}",
        f"  **真跑通过**  ：{len(passed)}/{len(promoted)}（{rate}）",
        f"  通过名单      ：{', '.join(passed) if passed else '（无）'}",
    ]
    out_path = args.out
    io.open(out_path, "w", encoding="utf-8", newline="\n").write("\n".join(report) + "\n")
    print("\n".join(report))
    print(f"\nwritten {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
