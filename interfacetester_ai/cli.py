# -*- coding: utf-8 -*-
r"""cli —— `haify` 命令行（§5.1；**P0a** E 阶段）。

    haify gen <doc.md> [--case-name-prefix P] [--fake] [--no-cache] [--no-doc-check]
                       [--max-rounds N] [--allow-host H] [--timeout S]

## 退出码（§六 的配置行 + §5.3 的断网口径，逐条钉死）

| 码 | 含义 | 用户下一步 |
| --- | --- | --- |
| **0** | 全部切片都**产出了草稿**（`.ai/draft/`；★**不等于**可以进 CI） | 看 `.ai/draft/` + `.ai/draft/*.unknowns.json`；要进 `cases/` 走 `haify review --approve` |
| **1** | 有切片失败（闸门拒 / 修正环用尽） | 看 `.ai/failed/<case>/NEEDS_HUMAN.md` |
| **2** | 配置缺失或非法（`LLMConfigMissing`） | 按打印出的配置指引填 `.env`；**本工具绝不隐式出网** |
| **3** | 网络/传输失败（`LLMTransportError`） | 查 base_url / 网络；或先用 `--fake` 走回放 |
| **4** | 文档体检未通过（`GenerationFailed`） | 改文档（清单在 `reports/NEEDS_DOC_FIX.md`） |
| **5** | 输入预算超限（`InputBudgetExceeded`） | 把文档拆小；**不重试**（重试不会变好） |

## 为什么 `--fake` 是"只许回放"而不是"不发请求的假执行"

`--fake` 用空的 `FakeTransport`：**只有缓存命中的那一轮**能成——这正是"CI 不出网"该有的
形态（回放来自 `.ai/cache/`，是**真跑过一次**留下的证据）。若缓存没命中，它会**响亮报错**
而不是编一个响应出来：**假执行会让"跑通了"变成一句没有依据的话**。

## 边界纪律（红线③）

本模块**一律不写盘**：读文档走只读 `open`，落盘全部委托给登记写入者。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

import interfacetester_ai.analyze as analyze_mod
from interfacetester_ai.analyze import DiagnosisFailed, analyze_summary
from interfacetester_ai.assembler import promote_draft
from interfacetester_ai.cache import CacheStore
from interfacetester_ai.debugtalk_draft import draft_debugtalk, render_draft_report
from interfacetester_ai.fix import propose_fixes, render_fix_report
from interfacetester_ai.gen import (
    MAX_ROUNDS,
    GenResult,
    GenerationFailed,
    documented_base_urls,
    generate_cases,
)
from interfacetester_ai.l3 import ALLOW_HOST_ENV, L3Refused, SANDBOX_ENV
from interfacetester_ai.llm import (
    FakeTransport,
    HttpTransport,
    LLMConfig,
    LLMConfigMissing,
    LLMTransportError,
)
from interfacetester_ai.manifest import ManifestStore
from interfacetester_ai.normalize import _read_any_encoding
from interfacetester_ai.prompts import INPUT_BUDGET_BYTES, InputBudgetExceeded
from interfacetester_ai.quality import BLOCKER_STRUCTURE, missing_blockers
from interfacetester_ai.reviews import drafts_overview, quality_of_draft
from interfacetester_ai.summary import summarize_summary
from interfacetester_ai import jobs as jobs_mod
from interfacetester_ai import export as export_mod
from interfacetester_ai.web import ALLOWED_ROOTS, DEFAULT_HOST, DEFAULT_PORT, PathRefused, serve

EXIT_OK = 0
EXIT_CASE_FAILED = 1
EXIT_CONFIG = 2
EXIT_TRANSPORT = 3
EXIT_DOC_UNFIT = 4
EXIT_BUDGET = 5  # 保留码：gen 路径上预算超限被 gen.py 记为"片级失败"，最终统一 EXIT_CASE_FAILED(1)

SUPPORTED_SUFFIXES = (".md", ".txt")


def read_document(path: Text) -> Text:
    """读文档：**只收 md/txt**（§5.1），编码交给归一化管道的探测（GBK/BOM 都能读）。"""
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise GenerationFailed(
            f"只收 {SUPPORTED_SUFFIXES} 的文档，实为 {suffix or '(无扩展名)'}：{path}\n"
            "  Word/PDF 请先转成 markdown（§5.1：转换不在本工具职责内，避免把'解析失败'伪装成'生成失败'）"
        )
    if not os.path.isfile(path):
        raise GenerationFailed(f"文档不存在：{path}")
    text = _read_any_encoding(path)
    return text or ""


def build_transport(args: argparse.Namespace) -> Tuple[Any, Text]:
    """按参数造 transport：`--fake` → 只许回放；否则 → 真出网（L3 闸门在它内部）。"""
    if args.fake:
        return FakeTransport({}), "回放（--fake：只允许命中 .ai/cache/，不出网）"
    return HttpTransport(), "实调（L3 闸门：需 SANDBOX=on，回环免白名单）"


def apply_cli_env(args: argparse.Namespace) -> None:
    """把 CLI 参数落到环境变量上（**L3 与配置层只认环境变量**——单一来源）。

    `--allow-host` 走 `INTERFACETESTER_AI_ALLOW_HOST`（不是自己另外判一遍 L3：
    闸门只有一份实现，CLI 只负责把用户的话传进去）。
    """
    if getattr(args, "allow_host", None):
        os.environ[ALLOW_HOST_ENV] = args.allow_host


# ---------------------------------------------------------------------------
# `haify gen`
# ---------------------------------------------------------------------------

def cmd_gen(args: argparse.Namespace) -> int:
    """文档 → 用例：逐片打印结论，最后给汇总与退出码。"""
    apply_cli_env(args)
    doc_text = read_document(args.doc)

    # ★`--assertions-only` 走**确定性映射**：不调模型，所以**也不需要 `BASE_URL/API_KEY`**。
    # 文档刚写完就能跑一遍，看看"光靠文档能确定多少断言"，不必先配好模型。
    if getattr(args, "assertions_only", False):
        return _run_assertions_only(args, doc_text)

    config = LLMConfig.from_env()  # 缺配置 → LLMConfigMissing（由 main 转成退出码 2）
    transport, mode = build_transport(args)
    cache = CacheStore()
    audit = ManifestStore()

    print(f"[haify gen] 文档  ：{args.doc}（{len(doc_text)} 字符）")
    print(f"[haify gen] 模型  ：{config.model} @ {config.base_url}")
    print(f"[haify gen] 通道  ：{mode}")
    print(f"[haify gen] 预算  ：{INPUT_BUDGET_BYTES} 字节/次；修正环 ≤ {args.max_rounds} 轮")
    # ★"产物要打到哪个服务"必须说清楚（2026-09-26 打通"生成 → 真跑"）：不给地址跑不起来
    _print_target_hint(doc_text, getattr(args, "base_url", "") or "")
    print("-" * 66)

    results: List[GenResult] = generate_cases(
        doc_text,
        transport=transport,
        config=config,
        cache=cache,
        audit=audit,
        doc_check=not args.no_doc_check,
        case_name_prefix=args.case_name_prefix or "",
        max_rounds=args.max_rounds,
        # ★`getattr` 而不是 `args.xxx`：本字段是后加的，而测试里有用 `Namespace`/mock
        #   直接构造参数的地方（缺字段会 `AttributeError`）。
        skip_non_interface=not getattr(args, "no_skip_non_interface", False),
        merge=not getattr(args, "no_merge_assertions", False),
        base_url=getattr(args, "base_url", "") or "",
    )

    produced = 0
    skipped = 0
    for item in results:
        # ★三态标记（2026-09-25）：`ok` / `skip`（本片没有接口）/ `fail`
        mark = {"ok": "✓", "skip": "-", "fail": "✗"}[item.outcome()]
        print(f"{mark} {item.case_name}　（{item.slice_title}）")
        for rnd in item.rounds:
            print(f"    · 第 {rnd.index} 轮：{rnd.kind}")
        if item.skipped:
            # ★跳过**必须说明为什么**。静默跳过比"失败"更糟：用户会以为文档没问题、
            #   而实际上那些章节**根本没被处理**（"没做"伪装成"做完了"是本仓最忌讳的形态）。
            print(f"    · 已跳过（不是失败）：{item.skip_reason()}")
        assembly = item.assembly
        if assembly is not None:
            print(f"    · {assembly.coverage.render()}")
            quality = getattr(assembly, "quality", None)
            if quality is not None:
                # ★2026-09-26（§九 第一档②）：退出码 0 **不是**"可用"，质量状态才是。
                print(f"    · 质量状态：{quality.render()}")
            for label, path in (
                ("草稿    ", getattr(assembly, "draft_yaml_path", "")),
                ("草案快照", getattr(assembly, "draft_path", "")),
                ("unknowns", getattr(assembly, "unknowns_path", "")),
                ("PENDING ", getattr(assembly, "pending_path", "")),
                ("需人工  ", getattr(assembly, "needs_human_path", "")),
            ):
                if path:
                    print(f"    · {label}：{path}")
        produced += 1 if item.ok else 0
        skipped += 1 if item.skipped else 0

    print("-" * 66)
    failed = len(results) - produced - skipped
    print(
        f"[haify gen] {produced}/{len(results)} 片产出草稿（★**待人工确认后转正**，"
        f"退出码 0 ≠ 可直接进 CI）；"
        f"另有 {skipped} 片**本就没有接口**（已跳过，不算失败）；{failed} 片失败。"
        f"模型调用审计 {len(audit.entries)} 条（.ai/manifest/）"
    )
    _print_drop_summary(results)
    if failed:
        # ★按"失败发生在哪一层"分别给下一步——两类的**留痕位置完全不同**：
        #   ① 装配后失败（写盘前置闸门拒 / L2 没过）：`.ai/failed/<用例>/NEEDS_HUMAN.md`
        #      里有人话版"人接着要做什么"；
        #   ② 装配**前**失败（L1 格式/结构在修正环里反复不过）：
        #      **没有**那份清单——证据只有 `.ai/manifest/` 里的**原始响应**。
        #
        # 实测（2026-09-25，真模型）：本地模型对 5 片全返回 `[]` → 0/5 产出，而原先
        # 一律打印"去看 NEEDS_HUMAN.md"——**那个文件不存在**，用户照着找会一无所获。
        blocked = [item for item in results if item.outcome() == "fail"]
        gated = [item for item in blocked if item.assembly is not None]
        early = [item for item in blocked if item.assembly is None]

        if gated:
            print(
                "[haify gen] 被**写盘前置闸门**拒掉的切片："
                ".ai/failed/<用例>/NEEDS_HUMAN.md 里写了**人接着要做什么**。"
            )
        if early:
            kinds = "、".join(sorted({rnd.kind for item in early for rnd in item.rounds}))
            print(
                f"[haify gen] 另有 {len(early)} 片**还没走到装配**（{kinds}）——"
                "这类**没有** NEEDS_HUMAN.md；模型的原始响应留在 `.ai/manifest/` 里"
                "（按时间倒序，最新一条是最后一轮），照它判断是模型能力问题还是切片/prompt 问题。"
            )
    # ★退出码口径（2026-09-25 修正）：**跳过不算失败** —— 否则"文档里有一半章节不是接口"
    #   这种**正常文档**永远拿不到 0，用户会把"正常"当成"工具坏了"（实测：15 片里 13 片
    #   是非接口章节，旧口径下退出码恒为 1）。真失败仍然如实报 1。
    return EXIT_OK if produced + skipped == len(results) else EXIT_CASE_FAILED


def _report_result(result: Any) -> None:
    """打印一条装配结果（`gen` 与 `--assertions-only` 共用同一套口径）。"""
    print(f"{'✓' if result.ok() else '✗'} {result.case_name}")
    print(f"    · {result.coverage.render()}")
    quality = getattr(result, "quality", None)
    if quality is not None:
        # ★2026-09-26（§九 第一档②）：`✓` 只表示"装配流程完成"，质量状态才是"能不能用"。
        print(f"    · 质量状态：{quality.render()}")
    for label, path in (
        ("草稿    ", getattr(result, "draft_yaml_path", "")),
        ("草案快照", result.draft_path),
        ("unknowns", result.unknowns_path),
        ("PENDING ", result.pending_path),
        ("需人工  ", result.needs_human_path),
    ):
        if path:
            print(f"    · {label}：{path}")
    if result.draft_only:
        print(
            "    · ⚠ 产物**只在 `.ai/draft/`**（§九 第一档②：默认交付草稿）——"
            "进 `cases/` 必须先人工确认：`haify review --approve <草稿> --approver <姓名>`"
        )
        if getattr(result, "todo_items", ()):
            print("      （其中含未确认的 `TODO_` 占位：**先填值**再转正，否则 T24 会拒）")


def _run_assertions_only(args: argparse.Namespace, doc_text: Text) -> int:
    """`--assertions-only`：**确定性断言补全**（不调模型、可复现）。

    ★2026-09-26（§9.6）：改为**逐片**——**每个接口段一条草稿**。原来"整篇一条"会把
    别的接口的字段断言塞进同一条用例（实测：一条打认证接口的用例带着创建目录/文件上传的字段），
    而且**能过全部闸门**（`static_valid`）——即"静默错位"。段外断言现在由归属闸门丢弃，
    并写进该条的 `unknowns`（可见，不静默）。
    """
    from interfacetester_ai.gen import generate_assertions_only_all  # noqa: PLC0415

    print(f"[haify gen --assertions-only] 文档：{args.doc}（{len(doc_text)} 字符）")
    print("[haify gen --assertions-only] 通道：确定性映射（**不调模型**，同一文档结果可复现）")
    print("[haify gen --assertions-only] 落点：**按接口段逐片**（每段一条草稿；段外断言不进本条）")
    _print_target_hint(doc_text, getattr(args, "base_url", "") or "")
    print("-" * 66)

    results = generate_assertions_only_all(
        doc_text, case_name=args.case_name_prefix or "", base_url=getattr(args, "base_url", "") or ""
    )
    if not results:
        # ★一个接口段都没有 → **响亮拒绝**（不许静默成功：产不出东西就是失败）
        print(
            "[haify gen --assertions-only] 这份文档里**没有识别到任何接口段**——"
            "需要 `METHOD /path` 形态（标题式 `## POST /api/x`，或表格式 "
            "`| **URL** | `POST /api/x` |`）",
            file=sys.stderr,
        )
        return EXIT_CASE_FAILED

    produced = 0
    for result in results:
        _report_result(result)
        produced += 1 if result.ok() else 0

    print("-" * 66)
    print(
        f"[haify gen --assertions-only] {produced}/{len(results)} 条**接口段**产出草稿"
        "（★每段一条；转正仍需 `haify review --approve` —— 退出码 0 ≠ 可直接进 CI）"
    )
    _print_drop_summary(results)
    _print_doc_degradation(doc_text)
    return EXIT_OK if produced == len(results) else EXIT_CASE_FAILED


def _print_doc_degradation(doc_text: Text) -> None:
    """把**文档级降级说明**在终端上也说一遍（§9.33 登记项 3）。

    ★为什么终端也要说：它虽然已随每段草稿落进 `unknowns.json`，但那是**文件** ——
      用户跑完命令看到的是"3/3 条接口段产出草稿 ✓"，很容易以为"文档里能断的都断了"。
      本仓的口径是：**没断言什么，必须在人眼前出现过一次**（不是"我们记得"）。
    """
    from interfacetester_ai.gen import doc_degradation_notes  # noqa: PLC0415

    notes = doc_degradation_notes(doc_text)
    if not notes:
        return
    print(
        f"[haify gen --assertions-only] 文档级降级说明：**{len(notes)} 条**"
        "（文档声明了、但判不出可断言的内容 → 工具**没有替文档拍板**；"
        "每段草稿的 `unknowns` 里都带了一份）"
    )
    for index, note in enumerate(notes, 1):
        print(f"    {index}) {note[:118]}{'…' if len(note) > 118 else ''}")


def _print_promotion_brief(target: Text, *, resolved_blockers: Sequence[Text] = ()) -> None:
    """转正**前**把"凭什么放行"摊开（WP1.2）。**只读**，不写盘。

    ★为什么必须先摊开：`--approve` 只打印"✓ 已转正"时，人工确认与"点一下同意"没有区别——
    审核者看不到质量状态，也看不到自己漏了哪条 blocker。评审 §五 要的正是"报告并列展示"。
    """
    import os as _os  # noqa: PLC0415

    if not _os.path.exists(target):
        return
    try:
        with open(target, encoding="utf-8") as fp:
            text = fp.read()
    except OSError as error:
        print(f"    · ⚠ 读不了草稿：{error}")
        return

    case = _os.path.splitext(_os.path.basename(target))[0]
    quality = quality_of_draft(case, text, draft_path=target)
    pending = missing_blockers(quality.blockers, resolved_blockers)

    print(f"[haify review] 待转正：{target}")
    print(f"    · 质量状态：{quality.render()}")
    if quality.blockers:
        done = "、".join(resolved_blockers) or "（无）"
        print(f"    · blocker：{'、'.join(quality.blockers)}；已点名处置：{done}")
        for code in pending:
            if code == BLOCKER_STRUCTURE:
                print(
                    f"      ⚠ **闸门拒**（{code}）→ 这是**硬结论**：`--resolve-blocker` 覆盖不了；"
                    "按 `.ai/failed/<用例>/NEEDS_HUMAN.md` 改好文档后**重新生成**草稿"
                )
            else:
                print(
                    f"      ⚠ **未处置**：{code} → 转正会被拒；"
                    f"处置请加 `--resolve-blocker {code}`"
                )
    else:
        print("    · blocker：无（可以直接转正）")


def _print_target_hint(doc_text: Text, base_url: Text) -> None:
    """把"**这份产物要打到哪个服务**"说清楚（不给地址 = 跑不起来，不是"可选"）。

    ★为什么必须响亮（2026-09-26 打通"生成 → 真跑"时实测）：产物里的 `url` 是相对路径，
    而内核 `parser.build_url()` 要求 `config.base_url` 是**字面量 URL** ——
    没有它，`hrun` 第一句就报 `ParamsError: base url missed!`。
    以前生成侧**完全不说这件事**，于是"生成成功"与"跑得起来"之间隔着一个静默的坑。
    """
    if base_url:
        print(f"[haify gen] 目标  ：{base_url}（已写进产物的 `config.base_url`）")
        return
    print("[haify gen] ⚠ 目标  ：**产物里没有 `base_url`** → 直接跑会报 `base url missed!`；")
    candidates = documented_base_urls(doc_text)
    if candidates:
        print("            文档里写着的服务地址（挑一个传给 `--base-url`）：")
        for label, url in candidates:
            print(f"              - {label}：{url}")
    else:
        print("            文档里没有服务地址表 → 生成时显式加 `--base-url http://host:port`")


def _print_drop_summary(results: Sequence[Any]) -> None:
    """把各条的 **T28 降级**合计成一行（(c)④-②）。只统计走到装配的片。

    ★为什么要按文档合计：单条用例的"降级 1 条"看不出问题，而**一篇文档 40% 的断言被拦**
    就说明"模型在这份文档上引用质量差"（实测：真实文档 75 条原始断言 → 30 条被拦）。
    分母 0 时**不报 0%**——"没有断言可比对"与"全都没问题"是两件事。
    """
    seen = dropped = 0
    kinds: Dict[Text, int] = {}
    for item in results:
        # ★两条通路的对象形状**不同**（2026-09-26 实测踩到）：
        #   模型通路传 `GenResult`（装配结果挂在 `.assembly` 上）；
        #   确定性通路传 `AssemblyResult`（`coverage` 就在它自己身上）。
        #   只认一种 → 另一条路的分母恒为 0（表现是"没有可比对的断言"，**看起来很合理**）。
        coverage = getattr(item, "coverage", None)
        if coverage is None:
            assembly = getattr(item, "assembly", None)
            coverage = getattr(assembly, "coverage", None) if assembly is not None else None
        if coverage is None:
            continue
        # ★`assertion_count` 是"**提议的**条数（含被降级的）"——直接当分母，
        #   不要再加 `dropped`（那是双重计数，实测把 100% 算成 50%）。
        seen += int(getattr(coverage, "assertion_count", 0) or 0)
        dropped += len(getattr(coverage, "dropped_assertions", ()) or ())
        for kind, count in getattr(coverage, "dropped_kinds", ()) or ():
            kinds[kind] = kinds.get(kind, 0) + count

    if seen <= 0:
        print("[haify gen] T28 降级统计：**没有可比对的断言**（分母 0，不当作 0%）")
        return
    line = f"[haify gen] T28 降级统计：{dropped}/{seen} = {dropped / seen:.0%}"
    if kinds:
        top = "、".join(
            f"{kind}×{count}"
            for kind, count in sorted(kinds.items(), key=lambda pair: (-pair[1], pair[0]))[:4]
        )
        line += f"　原因：{top}"
    print(line)


def cmd_review(args: argparse.Namespace) -> int:
    """`haify review`：列待确认草稿 / 人工确认后**转正**进 `cases/`（T24）。"""
    import os as _os  # noqa: PLC0415

    apply_cli_env(args)

    if getattr(args, "list", False):
        # ★WP1.2/C：清单口径**只此一份**（`drafts_overview`）——CLI、只读看板、离线导出共用，
        #   避免"三处各算一份质量状态"（不一致的看板等于没有看板）。
        rows = drafts_overview()
        print(f"[haify review --list] `.ai/draft/` 里 {len(rows)} 份草稿")
        print("-" * 66)
        for row in rows:
            if row["error"]:
                # ★读不到**不跳过**：跳过等于"这份不存在"，而事实是"它存在但读不了"
                print(f"  · {row['path']}  → 读不了：{row['error']}")
                continue
            state = "已留痕且哈希一致" if row["reviewed"] else "**待确认**"
            detail = f"质量状态 {row['quality_render']}"
            if BLOCKER_STRUCTURE in row["blockers"]:
                # ★闸门拒是**硬结论**：这里若照旧提示"点名处置"，会把用户引到一条走不通的路上
                detail += "；★闸门**硬结论**（点名处置覆盖不了）→ 按 NEEDS_HUMAN.md 改好文档后重新生成"
            elif row["blockers"]:
                detail += f"；转正需逐条点名处置（`--resolve-blocker {row['blockers'][0]}`）"
            print(f"  · {row['path']}  → {state}；{detail}")
        print("-" * 66)
        print("转正：haify review --approve <草稿> --approver <姓名> [--resolve-blocker <码> …]")
        return EXIT_OK

    if not getattr(args, "approve", ""):
        print("用法：haify review --list | haify review --approve <草稿> --approver <姓名>")
        return EXIT_CONFIG

    target = args.approve
    if _os.path.sep not in target and "/" not in target:
        target = ".ai/draft/" + target + ".yml"  # 内联字面量（T18 静态可判）

    from datetime import datetime  # noqa: PLC0415

    resolved = tuple(getattr(args, "resolve_blocker", None) or ())
    _print_promotion_brief(target, resolved_blockers=resolved)

    result = promote_draft(
        target,
        approver=args.approver or "",
        notes=getattr(args, "notes", "") or "",
        approved_at=datetime.now().isoformat(timespec="seconds"),
        resolved_blockers=resolved,
    )
    if result.ok():
        print(f"✓ 已转正：{result.yaml_path}")
        print(f"    · 留痕：{result.review_path}（approver={args.approver}）")
        print(f"    · 审核时质量状态：{result.quality.render()}")
        return EXIT_OK

    print(f"✗ 未转正：{result.reason}", file=sys.stderr)
    return EXIT_CASE_FAILED


def cmd_analyze(args: argparse.Namespace) -> int:
    """`haify analyze`：失败诊断（§5.3，P1a）。断网 → 退出码 3（由 `main` 统一转）。"""
    import json as _json  # noqa: PLC0415

    apply_cli_env(args)

    if not os.path.exists(args.summary):
        print(f"summary 不存在：{args.summary}", file=sys.stderr)
        return EXIT_CONFIG
    with open(args.summary, encoding="utf-8") as fp:
        summary = _json.load(fp)

    doc_text = read_document(args.doc) if getattr(args, "doc", "") else ""
    config = LLMConfig.from_env()
    transport, mode = build_transport(args)

    print(f"[haify analyze] summary：{args.summary}")
    print(f"[haify analyze] 通道：{mode}（transport 注入；`--fake` 只回放不出网）")
    print("-" * 66)

    result = analyze_summary(
        summary,
        transport=transport,
        config=config,
        doc_text=doc_text,
        cache=CacheStore(),
        audit=ManifestStore(),
        max_rounds=getattr(args, "max_rounds", analyze_mod.MAX_ROUNDS),
    )

    if not result.packs:
        # 没有失败断言 → **不产报告**：一份"空报告"会被误读成"分析过了"（§5.3）
        print("✓ 没有失败断言，无需分析（也不生成报告）")
        return EXIT_OK

    print(
        f"✓ 证据包 {result.packs} 条 → 结论 {len(result.diagnoses)} 条"
        f"（未降级 {result.kept} / 降级 {result.degraded}，**占比 {result.kept_ratio():.0%}**）"
    )
    if result.analysis_path:
        print(f"    · 分析报告：{result.analysis_path}")
    if result.rounds:
        print(
            "    · ★修正环："
            + "；".join(f"第 {item.index} 轮 [{item.kind}] 被拦" for item in result.rounds)
            + "（已回喂重试，重试**改变了输入**）"
        )
    if result.doc_defects_path:
        print(f"    · 文档缺陷回流：{result.doc_defects_path}（交文档维护者）")
    if result.cached:
        print("    · 本次是**缓存回放**（未真调模型）")
    print("-" * 66)
    return EXIT_OK


def cmd_summary(args: argparse.Namespace) -> int:
    """`haify summary`：结果摘要（§5.4，P1b）。

    ★**缺模型配置不是错误**（与 `gen`/`analyze` 故意不同）：摘要的数字是代码算的，
    模型只加 2~3 句导语。§5.4 明写"报告永不缺席"——把"锦上添花"失败升级成"整件事失败"，
    等于拿主产物给附加物陪葬。
    """
    import json as _json  # noqa: PLC0415

    apply_cli_env(args)

    if not os.path.exists(args.summary):
        print(f"summary 不存在：{args.summary}", file=sys.stderr)
        return EXIT_CONFIG
    with open(args.summary, encoding="utf-8") as fp:
        summary = _json.load(fp)

    transport = None
    config = None
    mode = "模板版（不调模型）"
    if not getattr(args, "no_llm", False):
        try:
            config = LLMConfig.from_env()
        except LLMConfigMissing:
            mode = "模板版（缺 BASE_URL/API_KEY —— 按 §5.4 **不算错误**）"
        else:
            transport, mode = build_transport(args)

    result = summarize_summary(
        summary,
        transport=transport,
        config=config,
        cache=CacheStore() if transport is not None else None,
        audit=ManifestStore() if transport is not None else None,
        source=args.summary,
    )

    stats = result.stats
    print(f"[haify summary] {args.summary}（{mode}）")
    print("-" * 66)
    print(
        f"✓ 用例 {stats.total}（成功 {stats.success} / 失败 {stats.fail}）"
        f"｜步骤 {stats.steps_total}（失败 {stats.steps_fail}）"
        f"｜耗时 {stats.duration_text()}"
    )
    print(f"    · {result.lead_status}")
    if result.report_path:
        print(f"    · 报告：{result.report_path}")
    print("-" * 66)
    return EXIT_OK


def cmd_fix(args: argparse.Namespace) -> int:
    """`haify fix`：用例自愈建议（§5.8，P2a）。

    ★**只建议、不落地**：**没有 `--apply`，也永远不会有**（§5.8 明写"`--apply` 永不存在"），
    更不会写任何用例文件——产物只是打到屏上的一段 diff + 理由 + 风险提示。

    ★**退出码恒 0**：本命令的产物是**建议**，"给不出建议"不是错误——
    尤其"疑似真实缺陷、不给改"是**正确的输出**。把正常拒绝报成非零退出码，
    会让人把它当故障处理（重试、绕过），而它本该是被认真读的一条结论。
    """
    import json as _json  # noqa: PLC0415

    apply_cli_env(args)

    if not os.path.exists(args.summary):
        print(f"summary 不存在：{args.summary}", file=sys.stderr)
        return EXIT_CONFIG
    with open(args.summary, encoding="utf-8") as fp:
        summary = _json.load(fp)

    result = propose_fixes(summary, case_dir=getattr(args, "cases", "cases"))
    print(render_fix_report(result))
    print("-" * 66)
    print(
        f"[haify fix] 共 {len(result.suggestions)} 条："
        f"给出补丁 {len(result.suggested())} 条 / 不给改 {len(result.refused())} 条"
        "　（**只建议、不落地**；请自行判断后手工粘贴）"
    )
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    """`haify run`：触发运行（§5.8 P3b-2）。

    ★它与看板的 `POST /jobs` **走同一个 Core**（`jobs.run_job`）——§2.5 要求
    "Web 与 CLI 是**同一个 Core** 的两个前端，绝不允许 Web 里出现另一套校验"。
    所以这里的闸门、运行域、串行**一行都不另写**。

    ★退出码口径：
    - **闸门拒绝** → `EXIT_CONFIG`（2）：那是**配置 / 环境**问题
      （路径不在运行域、沙盒没开、超时越界），不是"用例失败"；
    - **执行了** → **透传子进程的退出码**（这样 `haify run` 能被 CI 直接用）。
    """
    apply_cli_env(args)

    request = jobs_mod.JobRequest(
        paths=tuple(args.paths),
        base_url=args.base_url,
        label=args.label,
        timeout=args.timeout,
        triggered_by=args.approver or "(CLI)",
    )
    result = jobs_mod.run_job(request, dry_run=bool(args.dry_run))
    print(jobs_mod.render_job_report(result))

    if result.refused:
        print(f"[haify run] 闸门拒绝（{result.code}）：{result.reason}", file=sys.stderr)
        return EXIT_CONFIG
    return int(result.exit_code or 0)


def cmd_export(args: argparse.Namespace) -> int:
    """`haify export`：把只读看板导出成**离线静态快照**（§5.8 行 1004「离线可直接打开」）。

    ★为什么必须做这一步（真机实测）：看板的导航是**绝对路径**（`href="/metrics"`），
    而 `file://` 下 `/metrics` 会被解析成 `file:///D:/metrics`——点下去是
    `ERR_FILE_NOT_FOUND`。所以"页面自包含"只是**必要**条件；导出把站内链接改写成
    **相对文件名**，才是"离线可直接打开"的充分条件。

    ★退出码：**0** = 导出成功；**1** = 产物判据没过（**拒绝写盘**，一个字节都不落）。
    """
    roots = tuple(ALLOWED_ROOTS) + tuple(getattr(args, "root", ()) or ())
    apply_cli_env(args)
    try:
        result = export_mod.export_site(roots)
    except export_mod.OfflineBundleNotSafe as unsafe:
        print(
            f"[haify export] 产物判据没过，**拒绝写盘**（不产半份快照）：\n{unsafe}",
            file=sys.stderr,
        )
        return EXIT_CASE_FAILED

    print(f"[haify export] 已导出 {len(result['files'])} 个页面 → {result['dir']}/")
    for name in result["files"]:
        print(f"    · {result['dir']}/{name}")
    if result["rewritten"]:
        print("  被改写成「需要服务端」的路由（确认台 / 触发运行 / 下载）：")
        for route in result["rewritten"]:
            print(f"    - {route}")
    print(
        f"  离线打开：{os.path.abspath(os.path.join(result['dir'], export_mod.INDEX_NAME))}"
    )
    print("  提示：快照是**只读**的；要确认台 / 触发运行请用 `haify serve`。")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    """`haify serve`：只读看板（§5.8 行 1001，P3a）。

    ★这是本包**唯一**会长时间阻塞的命令（前台常驻）。三条纪律：

    ① 默认只监听 `127.0.0.1`（验收第 4 条：只监听回环），对外必须**显式** `--host`；
    ② 它**不写盘、不执行**——产物仍以 CLI 与磁盘上的文件为准（§2.5：CLI 是唯一执行面）；
    ③ 违规的读根在**启动时**就拒绝（不是等某个请求恰好命中才暴露）。
    """
    apply_cli_env(args)

    roots = tuple(ALLOWED_ROOTS) + tuple(getattr(args, "root", None) or ())
    try:
        return serve(
            args.host,
            int(args.port),
            roots,
            allowed_ips=tuple(getattr(args, "allow_ip", None) or ()),
            token_env=getattr(args, "token_env", "") or "",
            trust_proxy=bool(getattr(args, "trust_proxy", False)),
        )
    except PathRefused as refused:
        print(f"读根不合法：{refused}", file=sys.stderr)
        return EXIT_CONFIG


def cmd_debugtalk(args: argparse.Namespace) -> int:
    """`haify debugtalk`：签名/加密函数草稿（§5.9，P2b）。

    ★**只呈现、不落地**：不写 `debugtalk.py`（§5.9：「追加块 + diff 呈现」，人工自己贴），
    所以本命令**没有 `--apply`**、也不是"零写盘"里的写入者。

    ★**退出码恒 0**（与 P2a 同立场）："无测试向量 → 拒产"是**正确的输出**，不是故障。
    把正常拒产报成非零退出码，会让人把它当失败重试或绕过——而它本该被认真读。

    ★**例外**：`--sandbox` 而沙盒没开 → **退出码 2**（配置问题，与 `haify run` 的闸门拒绝同款）。
    这不是"拒产"，而是"你要求了一种**没被允许的执行方式**"——两者的下一步动作完全不同：
    前者该去补测试向量，后者该去看开关怎么开。
    """
    apply_cli_env(args)

    spec_path = args.spec
    if not os.path.exists(spec_path):
        print(f"spec 不存在：{spec_path}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        result = draft_debugtalk(
            spec_path,
            existing_path=getattr(args, "existing", "") or "",
            skip_dry_run=bool(getattr(args, "skip_dry_run", False)),
            sandbox=bool(getattr(args, "sandbox", False)),
        )
    except L3Refused as refused:
        print(f"[haify debugtalk] L3 拒绝：{refused}", file=sys.stderr)
        print(
            f"  若确实要在沙盒里跑：设 {SANDBOX_ENV}=on"
            f'（PowerShell：$env:{SANDBOX_ENV}="on"）。',
            file=sys.stderr,
        )
        return EXIT_CONFIG
    print(render_draft_report(result))
    print("-" * 66)
    print(
        f"[haify debugtalk] 采纳 {len(result.accepted())} 个 / 拒产 {len(result.rejected())} 个"
        "　（**只呈现、不落地**：请人工审阅后自己贴进 `debugtalk.py`）"
    )
    return EXIT_OK


def _version_text() -> str:
    """`haify --version` 的文案：**AI 包 + 内核**两个版本都报。

    ★为什么带上内核版本：AI 层读内核的**公开面**（`loader` / `make` / `parser` / `converter`），
      两者必须**配套**使用（见 `deploy.md` 的版本口径）。只报自己那一半，出问题时要靠人猜
      "是不是版本不配套" —— 那是现场最难查的一类问题。

    成对情形：**没装内核**也要能问版本（此时如实写"（未装）"），因为
    「我到底装没装、装的是哪个」正是用户来问版本的原因。
    """
    from interfacetester_ai import __version__ as ai_version  # noqa: PLC0415

    try:
        from interfacetester import __version__ as kernel_version  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 没装内核也要能问出版本
        kernel_version = "（未装）"
    return f"interfacetester-ai {ai_version}（内核 interfacetester {kernel_version}）"


def build_parser() -> argparse.ArgumentParser:
    """命令行解析器（`haify` 的公开面）。"""
    parser = argparse.ArgumentParser(
        prog="haify",
        description="interfacetester 的 AI 协作层（P0a：gen——文档 → 用例）",
    )
    # ★与 `hrun` / `hmake` **同一口径**：`-V` / `--version` 都能问版本。
    #   现场（2026-09-27）：装好 `haify` 后第一件事往往是问版本（`hrun --version` 的习惯），
    #   而它此前只认 `-h` → 报 `unrecognized arguments: --version`（退出码 2），
    #   看起来像"这个命令坏了"——**同一条链路上的工具，版本问法就该一样**。
    parser.add_argument("-V", "--version", action="version", version=_version_text())
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("gen", help="文档 → 用例草稿（§5.1）")
    gen.add_argument("doc", help="接口文档路径（只收 .md/.txt）")
    gen.add_argument("--case-name-prefix", default="", help="用例名前缀（多份文档时避免撞名）")
    gen.add_argument(
        "--fake",
        action="store_true",
        help="只许回放：命中 .ai/cache/ 才成功（CI 不出网）；未命中会响亮报错",
    )
    gen.add_argument("--no-doc-check", action="store_true", help="跳过生成前的文档体检闸（S-10）")
    gen.add_argument(
        "--no-merge-assertions",
        action="store_true",
        help=(
            "关掉「模型草稿 × 确定性映射」的合并（**C-2**，默认开）——"
            "等价于 INTERFACETESTER_AI_MERGE_ASSERTIONS=off"
        ),
    )
    gen.add_argument(
        "--no-skip-non-interface",
        action="store_true",
        help=(
            "不跳过'本就没有接口'的章节（默认跳过：标题/概述/字段表/附录这类片里没有 "
            "`METHOD /path`，送模型只会得到编造的端点或非法结构）。判据再准也不替用户拍板"
        ),
    )
    gen.add_argument(
        "--assertions-only",
        action="store_true",
        help="只做**确定性断言映射**（不调模型、**不需要** BASE_URL/API_KEY、可复现）",
    )
    gen.add_argument(
        "--max-rounds", type=int, default=MAX_ROUNDS, help=f"修正环轮数（默认 {MAX_ROUNDS}）"
    )
    gen.add_argument(
        "--allow-host",
        default="",
        help=f"L3 闸门：显式放行的主机（非回环必须列名，等价于 {ALLOW_HOST_ENV}）",
    )
    gen.add_argument(
        "--base-url",
        default="",
        help="目标服务地址（写进产物的 `config.base_url`）——"
        "★**不写就跑不起来**：内核要求它是字面量 URL，缺了会报 `base url missed!`",
    )
    gen.set_defaults(func=cmd_gen)

    review = sub.add_parser("review", help="待确认草稿 → 人工确认后转正进 cases/（T24）")
    review.add_argument("--list", action="store_true", help="列出 `.ai/draft/` 里的草稿与确认状态")
    review.add_argument("--approve", default="", help="要转正的草稿（用例名或相对路径）")
    review.add_argument("--approver", default="", help="**谁**确认的（留痕必填，空则拒）")
    review.add_argument(
        "--resolve-blocker",
        action="append",
        default=None,
        help="★逐条点名**处置**某个 blocker（可重复；如 `--resolve-blocker protocol-only`）。"
        "未点名的 blocker 会**拦住转正**；自由文本 `--notes` 不算处置",
    )
    review.add_argument("--notes", default="", help="确认备注（可选；★不能替代处置）")
    review.set_defaults(func=cmd_review)

    analyze = sub.add_parser("analyze", help="失败诊断：summary → reports/analysis.md（§5.3）")
    analyze.add_argument("summary", help="runner 产出的 summary.json")
    analyze.add_argument("--doc", default="", help="接口文档路径（doc 侧证据的出处）")
    analyze.add_argument(
        "--fake",
        action="store_true",
        help="只许回放：命中 .ai/cache/ 才成功（CI 不出网）；未命中会响亮报错",
    )
    analyze.add_argument(
        "--allow-host",
        default="",
        help=f"L3 闸门：显式放行的主机（非回环必须列名，等价于 {ALLOW_HOST_ENV}）",
    )
    analyze.add_argument(
        "--max-rounds",
        type=int,
        default=analyze_mod.MAX_ROUNDS,
        help="★§9.24 修正环：结构不合格时最多重试几轮（首发 1 次 + 修正 N 次；默认与 gen 同口径）",
    )
    analyze.set_defaults(func=cmd_analyze)

    summary_cmd = sub.add_parser("summary", help="结果摘要：summary → reports/summary.md（§5.4）")
    summary_cmd.add_argument("summary", help="runner 产出的 summary.json")
    summary_cmd.add_argument(
        "--no-llm", action="store_true", help="连导语也不调模型（纯模板版；报告永不缺席）"
    )
    summary_cmd.add_argument(
        "--fake",
        action="store_true",
        help="只许回放：命中 .ai/cache/ 才成功（CI 不出网）；未命中会响亮报错",
    )
    summary_cmd.add_argument(
        "--allow-host",
        default="",
        help=f"L3 闸门：显式放行的主机（非回环必须列名，等价于 {ALLOW_HOST_ENV}）",
    )
    summary_cmd.set_defaults(func=cmd_summary)

    fix = sub.add_parser("fix", help="用例自愈建议：只读 diff，**不写盘、不落地**（§5.8）")
    fix.add_argument("summary", help="runner 产出的 summary.json")
    fix.add_argument("--cases", default="cases", help="用例目录（默认 cases；只读）")
    fix.set_defaults(func=cmd_fix)

    debugtalk = sub.add_parser(
        "debugtalk", help="签名/加密函数草稿：只呈现 + diff，**不写盘**（§5.9）"
    )
    debugtalk.add_argument("spec", help="函数请求 spec.json（含 name/args/body/**vectors**）")
    debugtalk.add_argument(
        "--existing", default="", help="已有 debugtalk.py（**只读**：用于查重与追加 diff）"
    )
    debugtalk.add_argument(
        "--skip-dry-run",
        action="store_true",
        help="跳过已知答案测试（**不建议**：那就等于没有验过）",
    )
    debugtalk.add_argument(
        "--sandbox",
        action="store_true",
        help=(
            "在 **L3 沙盒**里跑已知答案测试（子进程 + 只跑 KAT + 允许真实加密运算；"
            f"需 {SANDBOX_ENV}=on）"
        ),
    )
    debugtalk.set_defaults(func=cmd_debugtalk)

    run_parser = sub.add_parser(
        "run",
        help="触发运行：过闸门后**真跑**用例（§5.8 P3b-2；与 `POST /jobs` **同一个 Core**）",
    )
    run_parser.add_argument(
        "paths", nargs="*", help="要跑的用例（只接受 `cases/` 下的 *.yml / *.yaml）"
    )
    run_parser.add_argument("--base-url", default="", help="交给 L3 闸门判的地址")
    run_parser.add_argument(
        "--timeout", type=int, default=jobs_mod.DEFAULT_TIMEOUT, help="子进程超时（秒）"
    )
    run_parser.add_argument("--label", default="", help="这次运行的说明")
    run_parser.add_argument("--approver", default="", help="谁触发的（留痕用）")
    run_parser.add_argument(
        "--dry-run", action="store_true", help="**只到闸门为止**：说清会跑什么，但不执行"
    )
    run_parser.set_defaults(func=cmd_run)

    serve_parser = sub.add_parser(
        "serve", help="只读看板：零写盘、零执行、默认只监听回环（§5.8 P3a）"
    )
    serve_parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"监听地址，默认 {DEFAULT_HOST}（对外必须**显式**改，且需自备反代 + IP 白名单）",
    )
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口，默认 {DEFAULT_PORT}")
    serve_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="**追加**只读根（默认 " + "/ ".join(ALLOWED_ROOTS) + "）；可重复，例：--root project-two/logs",
    )
    serve_parser.add_argument(
        "--allow-ip",
        action="append",
        default=[],
        help="IP 白名单（可重复）。★判据用的是**真实对端地址**，不是 `X-Forwarded-For`",
    )
    serve_parser.add_argument(
        "--token-env",
        default="",
        help="从**这个环境变量**读 Bearer token（传**变量名**而不是值：命令行会进 `ps` / shell 历史）",
    )
    serve_parser.add_argument(
        "--trust-proxy",
        action="store_true",
        help="信任 `X-Forwarded-For` 的**最后一跳**（只有确认前面是我们自己的反代时才开）",
    )
    serve_parser.set_defaults(func=cmd_serve)

    export_parser = sub.add_parser(
        "export",
        help="离线静态导出：把看板落成一组**相对链接**的 .html（`.ai/export/`，P3d 收口）",
    )
    export_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="**追加**只读根（默认 " + " / ".join(ALLOWED_ROOTS) + "）；可重复，同 `serve --root`",
    )
    export_parser.set_defaults(func=cmd_export)
    return parser


def main(argv: Optional[Sequence[Text]] = None) -> int:
    """CLI 入口。**退出码见模块 docstring 的表**（0/1/2/3/4/5）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        parser.print_help()
        return EXIT_CONFIG

    try:
        return int(args.func(args))
    except LLMConfigMissing as error:
        print(f"\n[配置缺失 · 退出码 {EXIT_CONFIG}]\n{error}", file=sys.stderr)
        return EXIT_CONFIG
    except GenerationFailed as error:
        print(f"\n[无法生成 · 退出码 {EXIT_DOC_UNFIT}]\n{error}", file=sys.stderr)
        return EXIT_DOC_UNFIT
    except DiagnosisFailed as error:
        print(f"\n[无法产出分析 · 退出码 {EXIT_DOC_UNFIT}]\n{error}", file=sys.stderr)
        return EXIT_DOC_UNFIT
    except InputBudgetExceeded as error:
        print(f"\n[输入预算超限 · 退出码 {EXIT_BUDGET}]\n{error}", file=sys.stderr)
        return EXIT_BUDGET
    except LLMTransportError as error:
        hint = (
            "\n  提示：`--fake` 只用 `.ai/cache/` 回放——先去掉 `--fake` 真跑一次以填充缓存。"
            if getattr(args, "fake", False)
            else ""
        )
        print(f"\n[传输失败 · 退出码 {EXIT_TRANSPORT}]\n{error}{hint}", file=sys.stderr)
        return EXIT_TRANSPORT


__all__ = [
    "EXIT_BUDGET",
    "EXIT_CASE_FAILED",
    "EXIT_CONFIG",
    "EXIT_DOC_UNFIT",
    "EXIT_OK",
    "EXIT_TRANSPORT",
    "SUPPORTED_SUFFIXES",
    "apply_cli_env",
    "build_parser",
    "build_transport",
    "cmd_analyze",
    "cmd_debugtalk",
    "cmd_run",
    "cmd_serve",
    "cmd_fix",
    "cmd_gen",
    "cmd_review",
    "cmd_summary",
    "main",
    "read_document",
]


if __name__ == "__main__":
    raise SystemExit(main())
