# -*- coding: utf-8 -*-
r"""audit —— **审计时间线**（v8 §2.5 安全面 3 / §5.8 行 1003 的 P3c；2026-09-25）。

## ★它是**投影**，不是第二份日志

§2.5 行 346 要求"每次人工确认写 `.ai/reviews/<ts>-<manifest_hash>.json`"。
这里很容易顺手做错一件事：**再写一份 `audit.log`**。那会立刻变成**双源**——
证据在 `reviews/` 与 `jobs/`，日志在这里，两边迟早不一致；
而**不一致的审计等于没有审计**（你无法判断该信哪一份）。

所以本模块**只做投影**：把既有证据读出来，按时间排成一条线。

| 源 | 回答什么 |
| --- | --- |
| `.ai/reviews/<case>.json` | **谁**确认了**哪一版内容**、接受了/拒绝了什么、**为什么** |
| `.ai/jobs/<id>/result.json` | 谁触发了运行、**退出码**、闸门结论、耗时 |
| `.ai/pending/*.pending.json` | 还有**哪些**待确认没处理（它们本身就是信号） |

每条事件都带 `source`（**哪个文件**）——所以时间线上任何一个结论，
都能一路指回它来自哪个文件。

## 纪律

- **只读**（不写盘）→ 本模块**不进** T18 注册表（与 `web` / `confirm` 同款）；
- **不读系统时钟**：时间一律**来自证据**（`approved_at` / `finished_at`）。
  读时钟会让"重放同一份证据"得到不同结果，而审计的全部价值在于**可复现**；
  证据里没有时间就显式写 `(无时间戳)`，**不拿"现在"去填空**（那是伪造时间）；
- **坏文件不跳过**：读不出来**本身就是一条事件**（它是异常信号），不是"没有这条"。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

# 三个证据源（**复用各模块自己的常量**，不另立口径）
from interfacetester_ai.jobs import JOBS_DIR
from interfacetester_ai.pending import PENDING_DIR
from interfacetester_ai.reviews import REVIEWS_DIR

# ★待确认清单的命名后缀。这里是**第二处**定义（第一处在 `pending.pending_path()`
# 的内联拼接、第二处在 `confirm.PENDING_SUFFIX`）。为什么允许它们并存：
# `pending.py` 的写盘行**必须内联字面量**（T18 的"目标必须可静态判定"纪律），
# 所以那个口径**不可能**被"引用"；而"同一口径散在多处"必然会漂移——
# 处置不是"靠人记得"，而是**用判据绑住**：`tests/audit_test.py` 里有一条断言，
# 同时校验 `audit` / `confirm` 两处的常量，以及 `pending.pending_path("x")`
# 实际产出的形态——三者不一致就红。
PENDING_SUFFIX = ".pending.json"

KIND_REVIEW = "review"
KIND_JOB = "job"
KIND_PENDING = "pending"

ACTION_ACCEPT = "accept"
ACTION_REJECT = "reject"
ACTION_MIXED = "mixed"
ACTION_RUN = "run"
ACTION_REFUSED = "refused"
ACTION_OPEN = "open"  # 待确认（**还没人处理**）
ACTION_UNREADABLE = "unreadable"

# 时间未知时的占位：★**显式**标注，不用"现在"去填空（那会伪造时间）
UNKNOWN_TIME = "(无时间戳)"


@dataclass(frozen=True)
class AuditEvent:
    """一条审计事件（**投影自某个证据文件**）。"""

    when: Text
    kind: Text
    action: Text
    who: Text = ""
    target: Text = ""
    reason: Text = ""
    source: Text = ""  # ★哪个文件——可溯源
    detail: Text = ""
    exit_code: Optional[int] = None
    refused: bool = False
    # ★WP1.2/C（2026-09-26）：审批时的**质量状态**（留痕里新写的三个字段的投影）。
    #   空串 = **留痕里没有这个字段**（本次改动之前签的那批）——`render_audit` 会如实标"未记录"，
    #   **不拿"没问题"去填空**（那是伪造结论：老留痕没记过质量，就不能说它当时质量如何）。
    quality: Text = ""

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "when": self.when,
            "kind": self.kind,
            "action": self.action,
            "who": self.who,
            "target": self.target,
            "reason": self.reason,
            "source": self.source,
            "detail": self.detail,
            "exit_code": self.exit_code,
            "refused": self.refused,
            "quality": self.quality,
        }


def _read_json(rel_path: Text) -> Tuple[Any, Text]:
    """读 JSON，返回 `(对象, 失败原因)`。"""
    try:
        with open(rel_path, encoding="utf-8", errors="replace") as fp:
            return json.load(fp), ""
    except FileNotFoundError:
        return None, "文件不存在"
    except OSError as error:
        return None, f"{type(error).__name__}: {error}"
    except ValueError as error:
        return None, f"JSON 解析失败：{error}"


def _listdir_sorted(rel_dir: Text) -> List[Text]:
    if not os.path.isdir(rel_dir):
        return []
    return sorted(os.listdir(rel_dir))


def _unreadable(kind: Text, rel: Text, why: Text) -> AuditEvent:
    """读不到 → **一条事件**（不跳过）。"""
    return AuditEvent(
        when=UNKNOWN_TIME,
        kind=kind,
        action=ACTION_UNREADABLE,
        source=rel,
        detail=f"读不到：{why}",
    )


def events_from_reviews() -> List[AuditEvent]:
    """`.ai/reviews/*.json` → 人工确认事件（谁 / 什么时候 / 对哪一版 / 为什么）。"""
    events: List[AuditEvent] = []
    for name in _listdir_sorted(REVIEWS_DIR):
        if not name.endswith(".json"):
            continue
        rel = REVIEWS_DIR + "/" + name
        payload, why = _read_json(rel)
        if why or not isinstance(payload, dict):
            events.append(_unreadable(KIND_REVIEW, rel, why or "留痕不是 JSON 对象"))
            continue

        notes = str(payload.get("notes", "") or "")
        # ★留痕里本来就写着「[接受] …」/「[拒绝] … 理由：…」——这里把它们**读回来**，
        #   而不是回过头去解析原始请求（原始请求早没了；**留痕才是证据**）。
        has_accept = "[接受]" in notes
        has_reject = "[拒绝]" in notes
        if has_accept and has_reject:
            action = ACTION_MIXED
        elif has_reject:
            action = ACTION_REJECT
        elif has_accept:
            action = ACTION_ACCEPT
        else:
            # ★不猜：读不出意图就**如实说读不出**（默认成 accept 等于伪造同意）
            action = "unknown"

        events.append(
            AuditEvent(
                when=str(payload.get("approved_at", "") or "") or UNKNOWN_TIME,
                kind=KIND_REVIEW,
                action=action,
                who=str(payload.get("approver", "") or ""),
                target=str(payload.get("draft", "") or ""),
                reason=notes,
                source=rel,
                detail=f"内容哈希 {str(payload.get('draft_hash', '') or '')[:12]}",
                quality=_quality_text(payload),
            )
        )
    return events


def _quality_text(payload: Dict[Text, Any]) -> Text:
    """留痕 → 一行质量说明（**没记过就返回空串，不替它编**）。

    投影的三样：审批时的质量状态、**当时实际存在的** blocker、其中**已点名处置**的。
    ★为什么要三样都在：只记"处置了哪些"会让"漏了哪一条"变成无法复核的问题；
    只记状态又会看不出"当时到底是凭什么放行的"。
    """
    status = str(payload.get("quality_status", "") or "")
    blockers = [str(item) for item in (payload.get("blockers") or ())]
    resolved = [str(item) for item in (payload.get("resolved_blockers") or ())]
    if not (status or blockers or resolved):
        return ""
    parts: List[Text] = []
    if status:
        parts.append(status)
    if blockers:
        parts.append("blocker：" + "、".join(blockers))
    if resolved:
        parts.append("已点名处置：" + "、".join(resolved))
    return "；".join(parts)


def events_from_jobs() -> List[AuditEvent]:
    """`.ai/jobs/*/result.json` → 触发运行事件（谁跑的、退出码、闸门结论）。"""
    events: List[AuditEvent] = []
    for name in _listdir_sorted(JOBS_DIR):
        if not os.path.isdir(os.path.join(JOBS_DIR, name)):
            continue
        rel = JOBS_DIR + "/" + name + "/result.json"
        payload, why = _read_json(rel)
        if why or not isinstance(payload, dict):
            entry = _unreadable(KIND_JOB, rel, why or "结果不是 JSON 对象")
            events.append(
                AuditEvent(
                    when=entry.when,
                    kind=entry.kind,
                    action=entry.action,
                    target=name,
                    source=entry.source,
                    detail=entry.detail,
                )
            )
            continue

        refused = bool(payload.get("refused"))
        exit_code = payload.get("exit_code")
        events.append(
            AuditEvent(
                when=str(payload.get("finished_at", "") or "")
                or str(payload.get("started_at", "") or "")
                or UNKNOWN_TIME,
                kind=KIND_JOB,
                action=ACTION_REFUSED if refused else ACTION_RUN,
                who=str(payload.get("triggered_by", "") or ""),
                target=name,
                reason=str(payload.get("reason", "") or ""),
                source=rel,
                detail=str(payload.get("l3_reason", "") or ""),
                exit_code=exit_code if isinstance(exit_code, int) else None,
                refused=refused,
            )
        )
    return events


def events_from_pending() -> List[AuditEvent]:
    """`.ai/pending/*.pending.json` → **还没人处理**的待确认项。

    ★它们的 `when` 一律是 `(无时间戳)`：清单里**本来就没有**时间字段。
    编一个"现在"会让"这份清单躺了三周"这件事**永远看不见**——
    而不见的那部分恰恰是最该被看见的。
    """
    events: List[AuditEvent] = []
    for name in _listdir_sorted(PENDING_DIR):
        if not name.endswith(PENDING_SUFFIX):
            continue
        rel = PENDING_DIR + "/" + name
        payload, why = _read_json(rel)
        if why or not isinstance(payload, dict):
            events.append(_unreadable(KIND_PENDING, rel, why or "清单不是 JSON 对象"))
            continue

        count = payload.get("count")
        events.append(
            AuditEvent(
                when=UNKNOWN_TIME,
                kind=KIND_PENDING,
                action=ACTION_OPEN,
                target=str(payload.get("case", "") or name),
                source=rel,
                detail=f"{count if isinstance(count, int) else '?'} 条待确认",
            )
        )
    return events


@dataclass
class AuditTimeline:
    """一条审计时间线——★**刻意分成两段**（见 `audit_timeline` 的说明）。"""

    timed: List[AuditEvent] = field(default_factory=list)
    untimed: List[AuditEvent] = field(default_factory=list)

    def all_events(self) -> List[AuditEvent]:
        return list(self.timed) + list(self.untimed)

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "timed": [item.to_dict() for item in self.timed],
            "untimed": [item.to_dict() for item in self.untimed],
            "total": len(self.timed) + len(self.untimed),
        }


def audit_summary(timeline: AuditTimeline) -> Dict[Text, Any]:
    """计数（按 `kind` / `action`）——**纯代码**，供看板与 CLI 用。"""
    by_kind: Dict[Text, int] = {}
    by_action: Dict[Text, int] = {}
    for event in timeline.all_events():
        by_kind[event.kind] = by_kind.get(event.kind, 0) + 1
        by_action[event.action] = by_action.get(event.action, 0) + 1
    return {
        "total": len(timeline.timed) + len(timeline.untimed),
        "timed": len(timeline.timed),
        "untimed": len(timeline.untimed),
        "by_kind": by_kind,
        "by_action": by_action,
        "unreadable": by_action.get(ACTION_UNREADABLE, 0),
        "open_pending": by_action.get(ACTION_OPEN, 0),
    }


def split_events(
    events: Sequence[AuditEvent],
) -> Tuple[List[AuditEvent], List[AuditEvent]]:
    """把事件分成 `(有时间戳的, 时间未知的)`，前者按时间**升序**。

    ★抽成独立函数是为了**能被直接测**：分段这件事的判据不该依赖文件系统
    （而本模块的自检**不造夹具**——产线模块里不出现写调用，T18 纪律）。
    """
    timed = sorted(
        (item for item in events if item.when != UNKNOWN_TIME),
        key=lambda item: (item.when, item.kind, item.source),
    )
    untimed = [item for item in events if item.when == UNKNOWN_TIME]
    return timed, untimed


def audit_timeline() -> AuditTimeline:
    """把三个源合成一条时间线。

    ★返回**两段**而不是一个大列表：

    - `timed`：**有时间戳**的事件，按时间**升序**（`when` 是
      `YYYY-MM-DDTHH:MM:SS` 形态，所以字典序 = 时间序）；
    - `untimed`：**时间未知**的（待确认清单、读不到的文件）。

    ★为什么必须分段：`(无时间戳)` 的字典序比任何数字都**小**，混排会让它
    **排在最前面**——看起来像"最早发生的事"，而它其实只是"没有时间字段"。
    把"未知"混进时间轴，等于**给未知伪造了一个位置**。
    （分开之后"待确认清单"反而更显眼——这恰好是我们要的：它是唯一需要人去做的事。）
    """
    events = events_from_reviews() + events_from_jobs() + events_from_pending()
    timed, untimed = split_events(events)
    return AuditTimeline(timed=timed, untimed=untimed)


def _event_line(event: AuditEvent) -> Text:
    """一条事件的文本形式（**带来源**——可溯源）。"""
    parts = [f"- `{event.when}` **{event.kind}/{event.action}**"]
    if event.who:
        parts.append(f"by {event.who}")
    if event.target:
        parts.append(f"→ {event.target}")
    if event.exit_code is not None:
        parts.append(f"exit={event.exit_code}")
    line = " ".join(parts)
    if event.detail:
        line += f"（{event.detail}）"
    line += f"\n  - 来源：`{event.source}`"
    if event.quality:
        line += f"\n  - 质量：{event.quality}"
    elif event.kind == KIND_REVIEW:
        # ★不拿"没问题"填空：老留痕**没记过**质量，就不能说它当时质量如何
        line += "\n  - 质量：**未记录**（2026-09-26 之前的留痕还没有质量字段）"
    if event.reason:
        line += "\n  - 理由：" + event.reason.replace("\n", " / ")[:200]
    return line


def render_audit(
    timeline: Optional[AuditTimeline] = None, *, limit: int = 0
) -> Text:
    """渲染成人看的审计时间线（**纯文本，不写盘**）。"""
    timeline = timeline if timeline is not None else audit_timeline()
    summary = audit_summary(timeline)

    lines = [
        "# 审计时间线（§2.5 安全面 3 / P3c）",
        "",
        "> ★这是**投影**，不是第二份日志：每条都指向一个**既有证据文件**",
        "> （`.ai/reviews/` · `.ai/jobs/` · `.ai/pending/`）。不另写日志是因为",
        "> 双源迟早不一致，而**不一致的审计等于没有审计**。",
        "",
        f"- 事件总数：**{summary['total']}**"
        f"（有时间戳 {summary['timed']} / 时间未知 {summary['untimed']}）",
        f"- 按来源：{summary['by_kind'] or '（无）'}",
        f"- 按动作：{summary['by_action'] or '（无）'}",
    ]

    if summary["unreadable"]:
        lines.append(
            f"- ★**读不到的证据：{summary['unreadable']} 条**"
            "（它不是「没有」，是「异常」——所以单列）"
        )
    if summary["open_pending"]:
        lines.append(
            f"- ★**待确认未处理：{summary['open_pending']} 份清单**"
            "（这是唯一在等人做事的信号）"
        )

    if timeline.untimed:
        lines += ["", "## 时间未知（★不排进时间轴——那会给未知伪造一个位置）", ""]
        lines += [_event_line(item) for item in timeline.untimed]

    if timeline.timed:
        lines += ["", "## 时间线（升序）", ""]
        shown = timeline.timed[-limit:] if limit > 0 else timeline.timed
        lines += [_event_line(item) for item in shown]

    if not timeline.all_events():
        lines += ["", "（没有读到任何证据：`.ai/` 下既没有确认留痕、也没有作业记录）"]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 自检（★**不造夹具**：只用**内存构造**的 `AuditEvent` + 已存在的证据）
# ---------------------------------------------------------------------------

def run_selftest(verbose: bool = False) -> int:
    """audit 自检。**不写盘**——夹具写入归 `tests/audit_test.py`（T18 纪律）。"""
    failures: List[Text] = []
    notes: List[Text] = []

    # ① ★分段：时间未知的**不许**混进时间轴
    events = [
        AuditEvent(
            when="2026-09-25T10:00:00", kind=KIND_REVIEW, action=ACTION_ACCEPT, source="r1"
        ),
        AuditEvent(when=UNKNOWN_TIME, kind=KIND_PENDING, action=ACTION_OPEN, source="p1"),
        AuditEvent(when="2026-09-25T09:00:00", kind=KIND_JOB, action=ACTION_RUN, source="j1"),
    ]
    timed, untimed = split_events(events)

    if [item.when for item in timed] != ["2026-09-25T09:00:00", "2026-09-25T10:00:00"]:
        failures.append(f"[分段/排序] 时间轴不对：{[item.when for item in timed]}")
    if len(untimed) != 1 or untimed[0].kind != KIND_PENDING:
        failures.append(f"[分段] 时间未知的没被分出去：{untimed}")

    # ★元护栏：证明"分段"不是装饰——**朴素按字典序混排**时，「无时间戳」会跑到最前面
    naive = sorted(events, key=lambda item: item.when)
    if naive[0].when != UNKNOWN_TIME:
        failures.append(
            "[元护栏] 混排时 `(无时间戳)` 没排在最前——分段的意义需要重新评估"
        )

    # ② 计数
    timeline = AuditTimeline(timed=timed, untimed=untimed)
    summary = audit_summary(timeline)
    if (summary["total"], summary["timed"], summary["untimed"]) != (3, 2, 1):
        failures.append(f"[计数] 不对：{summary}")
    if summary["open_pending"] != 1:
        failures.append(f"[计数] 待确认数不对：{summary}")
    if summary["by_kind"].get(KIND_JOB) != 1:
        failures.append(f"[计数] 按来源分组不对：{summary}")

    # ③ 读不到要**单列**（它不是「没有」，是「异常」）
    broken = AuditTimeline(
        timed=[],
        untimed=[
            AuditEvent(
                when=UNKNOWN_TIME,
                kind=KIND_REVIEW,
                action=ACTION_UNREADABLE,
                source="x.json",
                detail="读不到：坏 JSON",
            )
        ],
    )
    if audit_summary(broken)["unreadable"] != 1:
        failures.append(f"[读不到] 没被单列：{audit_summary(broken)}")

    # ④ 渲染：来源必须在、时间未知段必须在时间轴段**之前**
    text = render_audit(timeline)
    for piece in ("r1", "j1", "p1", "来源", "时间未知", "时间线"):
        if piece not in text:
            failures.append(f"[渲染] 缺 `{piece}`：{text[:200]}")
    if text.index("时间未知") > text.index("## 时间线"):
        failures.append("[渲染] 时间未知段应当排在时间轴之前（否则未知会被当成最早）")

    if "读不到的证据" not in render_audit(broken):
        failures.append("[渲染] 有读不到的证据时没有单列提示")

    # ⑤ 空时间线：不抛、且有说明
    empty_text = render_audit(AuditTimeline())
    if "没有读到任何证据" not in empty_text:
        failures.append("[渲染] 空时间线没有说明")

    # ⑥ 真读一次（本仓 `.ai/` 可能什么都没有——那也必须**不抛**）
    live = audit_timeline()
    notes.append(
        f"实测读到 {len(live.timed)} 条有时间戳 + {len(live.untimed)} 条时间未知"
        "（`.ai/` 为空是正常状态）"
    )

    # ⑦ 形态：本模块**源码里没有写调用**（所以它**不进** T18 注册表）
    import ast as ast_module  # noqa: PLC0415

    with open(__file__, encoding="utf-8") as fp:
        tree = ast_module.parse(fp.read(), filename=__file__)

    called = set()
    for node in ast_module.walk(tree):
        if not isinstance(node, ast_module.Call):
            continue
        parts = []
        func = node.func
        while isinstance(func, ast_module.Attribute):
            parts.append(func.attr)
            func = func.value
        if isinstance(func, ast_module.Name):
            parts.append(func.id)
        called.add(".".join(reversed(parts)))

    for banned in ("os.makedirs", "os.remove", "os.replace", "os.rename", "shutil.rmtree"):
        if banned in called:
            failures.append(f"[形态] `audit.py` 里出现了写盘调用 `{banned}`")

    for note in notes:
        print(f"[audit 自检] 说明：{note}")

    print("=" * 66)
    if failures:
        print(f"审计时间线自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("审计时间线自检全部通过（§2.5 安全面 3 / **P3c**）：")
    print("  ★**投影**而非双源：每条事件都指向一个既有证据文件（带 `source`）")
    print("          ——不另写日志：双源迟早不一致，而不一致的审计等于没有审计")
    print("  ★时间**只来自证据**：证据里没有时间就写「(无时间戳)」")
    print("          ——不拿「现在」填空（那是伪造时间）")
    print("  ★**分段**：时间未知的**不排进时间轴**")
    print("          ——`(无时间戳)` 的字典序比数字小，混排会让未知冒充「最早」")
    print("  读不到**单列**：它不是「没有」，是「异常」")
    print("  只读   ：源码里没有写调用 → **不进** T18 注册表（与 web/confirm 同款）")
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

    parser = argparse.ArgumentParser(description="审计时间线自检（P3c）")
    parser.add_argument("--render", action="store_true", help="打印时间线")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.render:
        print(render_audit())
        return 0
    return run_selftest(verbose=args.verbose)


__all__ = [
    "ACTION_ACCEPT",
    "ACTION_MIXED",
    "ACTION_OPEN",
    "ACTION_REFUSED",
    "ACTION_REJECT",
    "ACTION_RUN",
    "ACTION_UNREADABLE",
    "KIND_JOB",
    "KIND_PENDING",
    "KIND_REVIEW",
    "UNKNOWN_TIME",
    "AuditEvent",
    "AuditTimeline",
    "audit_summary",
    "audit_timeline",
    "events_from_jobs",
    "events_from_pending",
    "events_from_reviews",
    "render_audit",
    "run_selftest",
    "split_events",
]


if __name__ == "__main__":
    raise SystemExit(main())




