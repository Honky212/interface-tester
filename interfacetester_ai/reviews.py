# -*- coding: utf-8 -*-
"""reviews —— 人工确认留痕 writer（v8 §十 **T24** + §5.2 的落点纪律，2026-09-25）。

## 为什么需要它（§0.4-⑧ 那条"两条落点规则打架"）

§5.2 要求"期望值缺失 → 写 `${ENV(TODO_<语义名>)}`"，而这类用例**能过** L1/S/L2，
于是会落进 `cases/`，然后 CI 因 `EnvNotFound` 变红——**变红的原因却是"还没人确认期望值"**。
那等于**把人工待办伪装成质量信号**。

处置是**三条**（前两条成对，第三条是 2026-09-26 补的）：

1. **带 `TODO_` 的产物只落 `.ai/draft/`**（装配器内置的硬规则，见 `todo_findings`）；
2. **转 `cases/` 必须经 `.ai/reviews/*.json` 留痕**（本模块）——"谁在什么时候确认的"要写下来，
   不能只存在于某人的记忆里；
3. ★**有未处置的 blocker 就不许转正**（`--resolve-blocker <码>` **逐条点名**）——
   这条补的是"人工审核这一步也会放过薄用例"：`TODO_` 只是 blocker 的**一种**，
   `protocol-only`（断言全是协议级）、`unknown-critical`（引用/端点对不上）同样是 blocker，
   而改造前它们**不进转正门**（详见 `docs/未做完的待决策项.md` §九 第一档 A / 评审 §五）。
   ★**自由文本 `--notes` 不能当处置**：处置必须**逐条点名**到具体码，否则"我看了"
   是一句没有对象的话，闸门会退化成橡皮图章。

## ★留痕记的是**内容哈希**，不是路径

`draft_hash` 是那份草稿转正时的 **sha256**。"确认过"这件事必须绑定到**具体一版内容**上——
否则"确认后有人又改了一行"就会被继承为"已确认"，留痕变成橡皮图章。
所以 `require_review()` 比对哈希：不符 = **留痕失效**，请**重新确认**。

## 架构纪律（同 `pending.py`）

- 本模块是**登记在册的写入者**（T18 注册表 `reviews` → `("roots", (".ai/",))`），
  写入根固定 `.ai/reviews/`，写盘行**内联字面量前缀**（T18 静态可判纪律）；
- **原子写**：先写 `.tmp` 再 `os.replace`——"写到一半崩了"不该留下一个**看起来有效**的留痕
  （P1a 的"断网退出码 3 且无部分写"是同一套纪律）；
- **不依赖系统时钟**：`approved_at` 由调用方传入（默认空 → 由调用方补），
  这样测试可复现，也避免了"时间戳从哪来"的隐式依赖。

## 用法（cwd 必须是工作区根——`.ai/` 是相对根，§6）

    write_review_record("login_flow", draft_path=".ai/draft/login_flow.yml",
                        draft_text=text, approver="zhangsan",
                        resolved_todos=("${ENV(TODO_STATUS)}",))
    require_review("login_flow", draft_path=..., draft_text=...)   # None = 放行
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

from interfacetester_ai.quality import (
    QualityAssessment,
    assess,
    assess_from_artifacts,
    missing_blockers,
)

# 登记在册的写入根（相对工作区根；T18 注册表 reviews → ("roots", (".ai/",))）
REVIEWS_DIR = ".ai/reviews"


def review_path(case_name: Text) -> Text:
    """留痕相对路径（与 `pending.py` 同款命名口径）。"""
    return REVIEWS_DIR + "/" + case_name + ".json"


def draft_hash(draft_text: Text) -> Text:
    """草稿内容哈希（**归一化换行**后取 sha256）。

    归一只做 CRLF→LF：Windows 上检出可能带 CR，而"内容有没有变"不该被换行符风格影响。
    别的归一化（空白/引号）**刻意不做**——留痕要绑的是**逐字节内容语义**，
    多归一一步就多一个"改了什么却hash不变"的缝。
    """
    normalized = (draft_text or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReviewRecord:
    """一份人工确认记录（`.ai/reviews/<case>.json`）。"""

    case: Text
    approver: Text
    approved_at: Text
    draft: Text
    draft_hash: Text
    resolved_todos: Tuple[Text, ...] = ()
    notes: Text = ""
    # ★WP1.2（§九 第一档 A）：审核当时的**质量状态**与**已处置的 blocker 码**。
    #   为什么要记 `blockers`（实际有哪些）而不是只记 `resolved_blockers`（处置了哪些）：
    #   只有两者都在，事后才能回答"当时到底是凭什么放行的"——
    #   只记处置会让"漏了哪一条"变成无法复核的问题。
    quality_status: Text = ""
    blockers: Tuple[Text, ...] = ()
    resolved_blockers: Tuple[Text, ...] = ()

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "case": self.case,
            "approver": self.approver,
            "approved_at": self.approved_at,
            "draft": self.draft,
            "draft_hash": self.draft_hash,
            "resolved_todos": list(self.resolved_todos),
            "notes": self.notes,
            "quality_status": self.quality_status,
            "blockers": list(self.blockers),
            "resolved_blockers": list(self.resolved_blockers),
            "note": "本文件是**证据**（人工确认记录），刻意不被 .gitignore 忽略（T12/P-7）",
        }

    @classmethod
    def from_dict(cls, payload: Dict[Text, Any]) -> "ReviewRecord":
        return cls(
            case=payload.get("case", ""),
            approver=payload.get("approver", ""),
            approved_at=payload.get("approved_at", ""),
            draft=payload.get("draft", ""),
            draft_hash=payload.get("draft_hash", ""),
            resolved_todos=tuple(payload.get("resolved_todos", ())),
            notes=payload.get("notes", ""),
            quality_status=payload.get("quality_status", ""),
            blockers=tuple(payload.get("blockers", ())),
            resolved_blockers=tuple(payload.get("resolved_blockers", ())),
        )


def write_review_record(
    case_name: Text,
    *,
    draft_path: Text,
    draft_text: Text,
    approver: Text,
    resolved_todos: Sequence[Text] = (),
    notes: Text = "",
    approved_at: Text = "",
    resolved_blockers: Sequence[Text] = (),
    quality: Optional[QualityAssessment] = None,
) -> Text:
    """写 `.ai/reviews/<case>.json`（**原子写**）。返回相对路径。

    - `approver` **必填非空** → 空值直接抛：留痕的全部意义就是回答"**谁**确认的"，
      允许空值等于允许一张没签名的单子；
    - **原子写**（先 `.tmp` 再 `os.replace`）：避免"看起来有效的半个文件"；
    - `approved_at` 由调用方给（默认空串）——本模块**不读系统时钟**，
      这样留痕内容可复现，也少了"时间戳从哪来"的隐式依赖；
    - ★`quality`（可选）：把**审核当时**的质量状态与实际 blocker 清单一起留痕
      （WP1.2）——事后要能回答"当时凭什么放行的"，而不只是"谁签的"。
    """
    if not (approver or "").strip():
        raise ValueError("留痕必须写清 approver（'谁确认的'是这条记录的全部意义）")

    payload = ReviewRecord(
        case=case_name,
        approver=approver.strip(),
        approved_at=approved_at,
        draft=draft_path,
        draft_hash=draft_hash(draft_text),
        resolved_todos=tuple(resolved_todos),
        notes=notes,
        quality_status=(quality.status if quality is not None else ""),
        blockers=(tuple(quality.blockers) if quality is not None else ()),
        resolved_blockers=tuple(resolved_blockers),
    ).to_dict()

    os.makedirs(".ai/reviews", exist_ok=True)  # 写入根字面量（T18 静态可判）
    with open(".ai/reviews/" + case_name + ".json.tmp", "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    os.replace(".ai/reviews/" + case_name + ".json.tmp", ".ai/reviews/" + case_name + ".json")
    return review_path(case_name)


def read_review_record(case_name: Text) -> Optional[ReviewRecord]:
    """读留痕。**不存在 / 坏 JSON → None**（不抛：调用方问的是"有没有"）。"""
    path = ".ai/reviews/" + case_name + ".json"
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fp:
            payload = json.load(fp)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return ReviewRecord.from_dict(payload)


def require_review(case_name: Text, *, draft_path: Text, draft_text: Text) -> Optional[Text]:
    """转正的**前置检查**：`None` = 放行；否则返回拒绝原因（人话，可直接打给用户）。

    三条判据，缺一不可：

    1. 留痕**存在**；
    2. `approver` **非空**（"谁来确认的"）；
    3. ★`draft_hash` **与当前草稿一致**——"确认过"绑的是**内容**：确认之后又改一行，
       就该**重新确认**。少了这条，留痕会退化成**橡皮图章**（签一次，以后随便改都算签过）。
    """
    record = read_review_record(case_name)
    if record is None:
        return (
            f"未找到人工确认留痕 `.ai/reviews/{case_name}.json`——"
            "带 `TODO_` 的草稿必须经 `haify review --approve` 才能转进 `cases/`（§5.2 / T24）"
        )
    if not record.approver.strip():
        return f"留痕 `.ai/reviews/{case_name}.json` 没写 `approver`——'谁确认的'不能空着"
    current = draft_hash(draft_text)
    if record.draft_hash != current:
        return (
            f"草稿在确认**之后**又变了（留痕 {record.draft_hash[:12]}… ≠ 当前 {current[:12]}…）"
            "——★'确认'绑的是**当时那一版内容**，请**重新确认**"
        )
    return None


def unknowns_path(case_name: Text) -> Text:
    """`.ai/draft/<用例>.unknowns.json`（装配器的落点口径：按**用例名**，不按传入路径）。"""
    return ".ai/draft/" + case_name + ".unknowns.json"


def read_unknowns(case_name: Text) -> Optional[Any]:
    """读 `.ai/draft/<用例>.unknowns.json`；不存在 / 坏 JSON → `None`（**不抛**）。

    ★与 `read_review_record` 同款口径：调用方问的是"有没有"，不是"为什么坏"。
    抛异常会让 `haify review --list` 崩在某一份坏产物上。
    """
    path = unknowns_path(case_name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def gate_check(draft_path: Text) -> Tuple[bool, Tuple[Text, ...]]:
    """对**当前这一版**草稿重跑静态闸门（内核加载 + S 系列）。返回 `(是否通过, 闸门编号)`。

    ★**为什么重跑，而不是读 `.ai/failed/<用例>/NEEDS_HUMAN.md`**：那份留痕是**上一次装配时**
    的结论；草稿被人改过（转正前填值、补断言）之后它就**过期**了——按过期结论判新内容，
    正是本仓最反对的"结论与内容脱钩"。重跑判的是**当前这一版**，与 `draft_hash` 同一口径。

    ★判据从**闸门自己的 `to_dict()`** 取（不重写一套"算不算通过"）：
    闸门有 S1~S11 + PENDING/REJECT 三态之分，另立一套口径迟早与它漂移。

    加载/闸门**抛异常**一律视为"没通过"（`gate-error`）——宁严不松：
    转正门上的"不确定"，对用户而言就是"先别转"。
    """
    from interfacetester.loader import load_test_file  # noqa: PLC0415
    from interfacetester_ai.gates import check_testcase  # noqa: PLC0415

    try:
        report = check_testcase(load_test_file(draft_path))
    except Exception:  # noqa: BLE001 - 加载/闸门抛异常一律算没通过
        return False, ("gate-error",)
    snapshot = report.to_dict()
    return bool(snapshot.get("ok")), tuple(snapshot.get("codes") or ())


def quality_of_draft(case_name: Text, draft_text: Text, *, draft_path: Text = "") -> QualityAssessment:
    """**复算**这份草稿**当前**的质量状态（WP1.2 的入口）。

    ★为什么是"复算"而不是"读装配时写下的状态"（两条理由）：
    1. 转正前人会改草稿（填 `TODO_`）→ 装配时那个状态**已经过期**；
       "审核的是当前那一版内容"，与 `draft_hash` 同一口径；
    2. **不新增写盘点**：判据全部来自**既有产物**（草稿文本 + 装配器写的 `unknowns.json`
       + 闸门对**当前这一版**的结论），所以 `bench/write_boundary_scanner.py` 的注册表不用动。

    ★传入 `draft_path` 时会**重跑静态闸门**，且闸门结论是**硬结论**：
    它优先于内容级判定（一份过不了 S 闸门的草稿，状态必须是 `rejected`——
    否则 `review --list` 会把"闸门拒掉的草稿"显示成 `static_valid`，审核者会对着错状态签字）。

    本函数**只读**，不做任何写入。
    """
    assessment = assess_from_artifacts(draft_text, read_unknowns(case_name))
    if draft_path and os.path.exists(draft_path):
        passed, codes = gate_check(draft_path)
        if not passed:
            return assess(rejected=True, gate_codes=codes)
    return assessment


def read_draft_text(path: Text) -> Tuple[Text, Text]:
    """读草稿文本；返回 `(文本, 失败原因)`。

    ★读不到时**不抛**、也不静默：`(空文本, 原因)` —— 调用方必须把原因显示出来
    （"这份不存在"与"这份读不了"在页面上必须可分，`confirm.load_pending_lists` 同款口径）。
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fp:
            return fp.read(), ""
    except OSError as error:
        return "", f"{type(error).__name__}: {error}"


def drafts_overview(*, with_gate: bool = True) -> List[Dict[Text, Any]]:
    """列出 `.ai/draft/` 的草稿 + **复算**质量状态 + 留痕状态（**只读**，WP1.2/C）。

    ★**三处共用同一份口径**：`haify review --list`、只读看板（`web`）、离线导出（`export`）。
    各算一份迟早不一致，而不一致的看板等于没有看板（与 `audit.py`"刻意不是第二份日志"同源）。

    `with_gate=True`（默认）对每份草稿**重跑静态闸门**——它是**纯逻辑**（不写盘、不起子进程；
    black 探针是另一条显式路径，不在闸门里），所以拿它做只读展示是安全的，
    而且能避免"把闸门拒掉的草稿显示成通过"这类**状态错**。

    `with_gate=False` 只做内容级复算（`TODO_` + `unknowns`），并把 `gate` 标成 `not_probed`
    ——★**不猜**：没跑就说没跑，让读者知道"这一档结论不完整"。
    """
    rows: List[Dict[Text, Any]] = []
    for path in list_drafts():
        case = os.path.splitext(os.path.basename(path))[0]
        text, error = read_draft_text(path)
        if error:
            rows.append(
                {
                    "path": path,
                    "case": case,
                    "error": error,
                    "quality_status": "",
                    "quality_render": "",
                    "blockers": [],
                    "reviewed": False,
                    "review_blocking": "",
                    "resolved_blockers": [],
                    "gate": "",
                }
            )
            continue

        quality = quality_of_draft(case, text, draft_path=path if with_gate else "")
        blocking = require_review(case, draft_path=path, draft_text=text)
        record = read_review_record(case)
        rows.append(
            {
                "path": path,
                "case": case,
                "error": "",
                "quality_status": quality.status,
                "quality_render": quality.render(),
                "blockers": [str(item) for item in quality.blockers],
                "reviewed": blocking is None,
                "review_blocking": blocking or "",
                "resolved_blockers": [str(item) for item in record.resolved_blockers]
                if record
                else [],
                "gate": "probed" if with_gate else "not_probed",
            }
        )
    return rows


def list_drafts() -> List[Text]:
    """列出 `.ai/draft/` 里的草稿（**待确认清单**，供 `haify review --list`）。"""
    if not os.path.isdir(".ai/draft"):
        return []
    names = [
        entry
        for entry in os.listdir(".ai/draft")
        if entry.endswith(".yml") or entry.endswith(".yaml")
    ]
    return sorted(".ai/draft/" + name for name in names)


def run_selftest(verbose: bool = False) -> int:
    """reviews 自检。返回 0=全绿；非 0=失败项数。

    ★**本自检不写盘**，只测纯函数——T27 那笔教训：**夹具写盘要放进 `tests/`**
    （生产模块只该留自己的写盘点；否则 T18 扫描器会判"目标不可静态判定"，
    而那是**真违规**，不该靠登记白名单糊过去）。写→读→拒的整链在
    `tests/reviews_test.py` 的临时工作区里测。
    """
    failures: List[Text] = []
    missing = "__definitely_missing_case__"

    # ① 哈希归一：CRLF / LF 同一内容 → 同哈希。
    #    否则 Windows 检出（带 CR）后，所有留痕的哈希会**整体对不上** → 全员被迫重新确认。
    if draft_hash("a\r\nb\n") != draft_hash("a\nb\n"):
        failures.append("[哈希] CRLF/LF 归一失效（Windows 检出后留痕会整体失效）")

    # ② 元护栏：不同内容必须不同哈希（防有人把哈希写成常量/只取长度）
    if draft_hash("a") == draft_hash("b"):
        failures.append("[哈希] 不同内容竟得同一哈希")

    # ③ 路径口径（登记根固定 `.ai/reviews/`）
    if review_path("x") != ".ai/reviews/x.json":
        failures.append(f"[路径] review_path 口径变了：{review_path('x')}")

    # ④ `ReviewRecord` 往返等价
    record = ReviewRecord(
        case="c",
        approver="a",
        approved_at="2026-09-25T10:00:00",
        draft=".ai/draft/c.yml",
        draft_hash="h",
        resolved_todos=("${ENV(TODO_X)}",),
        quality_status="needs_review",
        blockers=("protocol-only",),
        resolved_blockers=("protocol-only",),
    )
    if ReviewRecord.from_dict(record.to_dict()) != record:
        failures.append("[往返] ReviewRecord 序列化/反序列化不等价")

    # ⑤ 读不存在 → None（**不抛**：调用方问的是"有没有"）
    if read_review_record(missing) is not None:
        failures.append("[读] 不存在时应当返回 None")

    # ⑥ **门的核心判据**：无留痕 → 必须拒（放行等于门形同虚设）
    reason = require_review(missing, draft_path=".ai/draft/x.yml", draft_text="x")
    if not reason:
        failures.append("[门] 无留痕时 require_review 竟然放行")

    # ⑦ ★WP1.2：**未处置的 blocker 就是不放行**（自由文本不算处置）——
    #     这里只测**纯函数**（真链路在 `tests/reviews_test.py` 的临时工作区里跑）
    assessment = assess_from_artifacts("url: /x\n", {"items": [{"kind": "protocol-only"}]})
    if assessment.status != "needs_review":
        failures.append("[处置] 有 `protocol-only` 却复算成了非待人工")
    if not missing_blockers(assessment.blockers, ()):
        failures.append("[处置] 什么都没点名，却算成「已处置」")
    if missing_blockers(assessment.blockers, ("protocol-only",)):
        failures.append("[处置] 已逐条点名，却仍被判未处置")
    if missing_blockers(assessment.blockers, ("别的码",)) == ():
        failures.append("[处置] 点名了**不相干**的码竟然放行（处置要指名到 blocker 码）")
    if assess_from_artifacts("url: /x\n").status != "static_valid":
        failures.append("[处置] 干净草稿应复算为 static_valid（别把正常路径也拦了）")
    if unknowns_path("c") != ".ai/draft/c.unknowns.json":
        failures.append(f"[路径] unknowns_path 口径变了：{unknowns_path('c')}")

    print("=" * 66)
    if failures:
        print(f"reviews 自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("人工确认留痕自检全部通过（T24 / §5.2 的落点纪律 + WP1.2 的 blocker 处置）：")
    print("  哈希  ：CRLF/LF 归一（Windows 检出后留痕不会整体失效）+ 不同内容不同哈希（元护栏）")
    print("  门    ：**无留痕 → 拒**（转正必须经 `.ai/reviews/<case>.json`）")
    print("  绑定  ：确认记的是**内容哈希**——确认后又改一行 → 留痕失效，必须重新确认")
    print("  处置  ：**未处置的 blocker → 不放行**（要逐条点名到 blocker 码；自由文本不算处置）")
    print("  原子写：先 `.tmp` 再 `os.replace`（不留\"看起来有效的半个文件\"）")
    print("=" * 66)
    return 0


def main() -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="人工确认留痕（T24）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "REVIEWS_DIR",
    "ReviewRecord",
    "draft_hash",
    "drafts_overview",
    "gate_check",
    "list_drafts",
    "quality_of_draft",
    "read_draft_text",
    "read_review_record",
    "read_unknowns",
    "require_review",
    "review_path",
    "run_selftest",
    "unknowns_path",
    "write_review_record",
]


if __name__ == "__main__":
    raise SystemExit(main())
