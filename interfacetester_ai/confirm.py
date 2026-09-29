# -*- coding: utf-8 -*-
r"""confirm —— **确认门**（v8 §2.5 行 339 的 P3b-1；2026-09-25）。

## 它把什么变成"一屏批量处置"（§2.5 价值 2）

`pending.json` 记的是"这个期望值文档没写"——能回答的是产品 / 后端 / 数据，
而他们**不会装 venv、不会跑 CLI**。所以确认门要做三件事：

1. 把**待确认项**列成表单；
2. 把 `fix` 的每条建议摆出来，**逐条 accept / reject**；
3. 把"**谁**、什么时候、对**哪一版内容**、同意了/拒绝了**什么**、**为什么**"
   写进 `.ai/reviews/`。

## ★本模块**不写盘、不执行**

- **不写盘**：落盘**委托** `reviews.write_review_record`（T18 登记在册的写入者）。
  本模块源码里**没有**任何 `open(…, "w")` / `makedirs`——所以它**不需要**登记进 T18，
  而 `web.py` 也**仍然**保持"零写盘"的形态（写动作不在它的源码里）。
  这与 P2a/P2b 一脉：**产物给人看，落地动作留给人**。
- **不执行**：只做解析、校验与组织，**不跑用例、不调模型、不起子进程**。
  「自动重跑 L2」属 **P3b-2 触发运行**——那才是真正新增攻击面的那块。

## ★三条硬纪律（每条都有机器判据）

1. **reject 必填理由**（§2.5 行 339 原文）——空 / 纯空白理由**一律拒收**，
   **不是**"默认同意"。理由空着还能 reject，等于把"为什么"这条信息**永久丢掉**：
   半年后没人知道当时为什么不要这条建议。
2. **approver 必填**——复用 `reviews` 的立场："**允许空值等于允许一张没签名的单子**"。
3. **accept 也要留痕**——留痕回答的是"谁在什么时候同意了**什么**"。
   只记 reject 会让"同意"变成一个**没有证据的默认值**。

## 留痕绑的是**内容哈希**（复用 T24）

`confirm` 会把目标内容交给 `reviews.draft_hash`。这继承 T24 的处置：
"确认过"必须绑定到**具体一版内容**上——否则"确认后有人又改了一行"会被继承为"已确认"，
留痕就变成橡皮图章。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

# 裁决动作（只有两种；其余一律拒收——"接受一半"不是一种裁决）
ACTION_ACCEPT = "accept"
ACTION_REJECT = "reject"
ACTIONS: Tuple[Text, ...] = (ACTION_ACCEPT, ACTION_REJECT)

# 待确认清单的落点（与 `pending.PENDING_DIR` 同口径）
PENDING_DIR = ".ai/pending"
PENDING_SUFFIX = ".pending.json"

# 自愈建议的落点（`fix.propose_fixes` 的读根）
CASES_DIR = "cases"

# ★拒收原因码（每条都要能直接打给用户）
REFUSE_NO_APPROVER = "no_approver"
REFUSE_NO_REASON = "reject_without_reason"
REFUSE_BAD_ACTION = "unknown_action"
REFUSE_NO_TARGET = "no_target"
REFUSE_NO_CASE = "no_case"
REFUSE_BAD_JSON = "bad_json"


@dataclass(frozen=True)
class ConfirmDecision:
    """一条人工裁决（对一个目标：某条 PENDING 项 / 某条 fix 建议）。"""

    target: Text
    action: Text
    reason: Text = ""
    fill: Dict[Text, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "target": self.target,
            "action": self.action,
            "reason": self.reason,
            "fill": dict(self.fill),
        }

    @classmethod
    def from_dict(cls, payload: Dict[Text, Any]) -> "ConfirmDecision":
        fill = payload.get("fill")
        return cls(
            target=str(payload.get("target", "") or ""),
            action=str(payload.get("action", "") or "").strip().lower(),
            reason=str(payload.get("reason", "") or ""),
            fill=dict(fill) if isinstance(fill, dict) else {},
        )


@dataclass
class ConfirmRequest:
    """一次确认提交（来自 HTTP body / CLI）。"""

    case: Text = ""
    approver: Text = ""
    decisions: Tuple[ConfirmDecision, ...] = ()
    notes: Text = ""
    target_path: Text = ""  # 留痕绑定的目标产物路径（给人看的）
    target_text: Text = ""  # ★参与内容哈希的**那一版内容**
    approved_at: Text = ""

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "case": self.case,
            "approver": self.approver,
            "decisions": [item.to_dict() for item in self.decisions],
            "notes": self.notes,
            "target_path": self.target_path,
            "approved_at": self.approved_at,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ConfirmRequest":
        if not isinstance(payload, dict):
            return cls()
        raw = payload.get("decisions")
        decisions: List[ConfirmDecision] = []
        if isinstance(raw, list):
            decisions = [
                ConfirmDecision.from_dict(item)
                for item in raw
                if isinstance(item, dict)
            ]
        return cls(
            case=str(payload.get("case", "") or ""),
            approver=str(payload.get("approver", "") or ""),
            decisions=tuple(decisions),
            notes=str(payload.get("notes", "") or ""),
            target_path=str(payload.get("target_path", "") or ""),
            target_text=str(payload.get("target_text", "") or ""),
            approved_at=str(payload.get("approved_at", "") or ""),
        )


@dataclass
class ConfirmResult:
    """一次确认的结果（**拒收与落痕都要能说清**）。"""

    ok: bool = False
    errors: List[Text] = field(default_factory=list)
    codes: List[Text] = field(default_factory=list)
    record_path: Text = ""
    accepted: List[Text] = field(default_factory=list)
    rejected: List[Text] = field(default_factory=list)
    applied: List[Text] = field(default_factory=list)  # ★回填后的内容（**不落盘**，给人贴）

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "codes": list(self.codes),
            "record_path": self.record_path,
            "accepted": list(self.accepted),
            "rejected": list(self.rejected),
            "applied": list(self.applied),
        }


# ---------------------------------------------------------------------------
# 解析（★**不做容错猜测**）
# ---------------------------------------------------------------------------

def parse_payload(raw: Any) -> Tuple[Optional[ConfirmRequest], List[Text]]:
    """把 body（`dict` / `bytes` / `str`）解析成 `ConfirmRequest`。

    ★**不"尽力而为地猜一个请求"**：格式不对就说格式不对。
    猜出来的请求会带着**人的签名**落进留痕——那比报错危险得多
    （留痕的价值全在"这条是人确认的"，一个猜出来的请求会把这个前提抽掉）。
    """
    if isinstance(raw, dict):
        return ConfirmRequest.from_dict(raw), []

    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            return None, [f"body 不是 UTF-8：{error}"]

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, ["body 是空的"]
        try:
            parsed = json.loads(text)
        except ValueError as error:
            return None, [f"body 不是合法 JSON：{error}"]
        return ConfirmRequest.from_dict(parsed), []

    return None, [f"不支持的 body 类型：{type(raw).__name__}"]


# ---------------------------------------------------------------------------
# ★校验（所有拒收理由；空 = 可以落痕）
# ---------------------------------------------------------------------------

def validate(request: ConfirmRequest) -> Tuple[List[Text], List[Text]]:
    """返回 `(错误说明, 原因码)`。**空 = 可以落痕**。"""
    errors: List[Text] = []
    codes: List[Text] = []

    if not request.case.strip():
        errors.append(
            "缺少 `case`：留痕是**按用例**组织的（`.ai/reviews/<case>.json`）"
        )
        codes.append(REFUSE_NO_CASE)

    if not request.approver.strip():
        errors.append(
            "缺少 `approver`：留痕的全部意义就是回答「**谁**确认的」——"
            "允许空值等于允许一张没签名的单子"
        )
        codes.append(REFUSE_NO_APPROVER)

    if not request.decisions:
        errors.append(
            "没有任何裁决（`decisions` 为空）——"
            "空表返回成功会造出「这条已经确认过了」的**假证据**"
        )
        codes.append(REFUSE_NO_TARGET)

    seen: set = set()
    for index, decision in enumerate(request.decisions):
        where = f"decisions[{index}]"

        if not decision.target.strip():
            errors.append(f"{where}：缺 `target`（要对哪一个目标下裁决）")
            codes.append(REFUSE_NO_TARGET)
            continue

        if decision.target in seen:
            errors.append(
                f"{where}：`target` `{decision.target}` 重复出现——"
                "同一条目标给两次裁决时，**以哪一次为准并不明确**"
            )
            codes.append(REFUSE_NO_TARGET)
        seen.add(decision.target)

        if decision.action not in ACTIONS:
            errors.append(
                f"{where}：`action` `{decision.action}` 不认识"
                f"（只接受 {' / '.join(ACTIONS)}）"
            )
            codes.append(REFUSE_BAD_ACTION)
            continue

        # ★★ 本模块最要紧的一条判据：reject **必须**带理由，且不能是纯空白
        if decision.action == ACTION_REJECT and not decision.reason.strip():
            errors.append(
                f"{where}：**reject 必须写理由**（§2.5 行 339）——目标 `{decision.target}`。"
                "理由空着还能 reject，等于把「为什么不要它」**永久丢掉**："
                "半年后没人知道当时为什么否掉这条建议"
            )
            codes.append(REFUSE_NO_REASON)

    return errors, codes


# ---------------------------------------------------------------------------
# 读清单 / 读建议（**只读**）
# ---------------------------------------------------------------------------

def pending_paths() -> List[Text]:
    """扫 `.ai/pending/*.pending.json`（相对工作区根，**只读**）。"""
    if not os.path.isdir(PENDING_DIR):
        return []
    return sorted(
        PENDING_DIR + "/" + name
        for name in os.listdir(PENDING_DIR)
        if name.endswith(PENDING_SUFFIX)
        and os.path.isfile(os.path.join(PENDING_DIR, name))
    )


def load_pending_lists() -> List[Dict[Text, Any]]:
    """读全部待确认清单。

    ★坏文件**不跳过**：跳过等于"这份清单不存在"，而事实是"它存在但读不了"——
    后者必须出现在页面上。否则"没人要确认"与"我看不到要确认的东西"
    **在页面上长得一模一样**。
    """
    lists: List[Dict[Text, Any]] = []
    for rel in pending_paths():
        entry: Dict[Text, Any] = {
            "path": rel,
            "error": "",
            "case": "",
            "count": 0,
            "items": [],
        }
        try:
            with open(rel, encoding="utf-8", errors="replace") as fp:
                payload = json.load(fp)
        except (OSError, ValueError) as error:
            entry["error"] = f"{type(error).__name__}: {error}"
            lists.append(entry)
            continue

        if isinstance(payload, dict):
            entry["case"] = str(payload.get("case", "") or "")
            items = payload.get("items")
            entry["items"] = items if isinstance(items, list) else []
            entry["count"] = len(entry["items"])
        else:
            entry["error"] = "清单不是 JSON 对象"
        lists.append(entry)
    return lists


def fix_suggestions(summary: Any, *, case_dir: Text = CASES_DIR) -> List[Dict[Text, Any]]:
    """把 `fix.propose_fixes` 的条目整理成**待裁决**的形状（**只读**）。

    ★**不在这里重复实现分类逻辑**：`fix` 那边的触发条件（业务码不给改 /
    字段改名要双向包含匹配 / 双前置闸门）是它自己的纪律，多一份实现就是双源漂移。
    """
    from interfacetester_ai.fix import propose_fixes  # noqa: PLC0415

    items: List[Dict[Text, Any]] = []
    for index, suggestion in enumerate(propose_fixes(summary, case_dir=case_dir).suggestions):
        items.append(
            {
                "target": f"fix:{index}:{suggestion.case}/{suggestion.step}",
                "case": suggestion.case,
                "step": suggestion.step,
                "check": suggestion.check,
                "status": suggestion.status,
                "kind": suggestion.kind,
                "reason": suggestion.reason,
                "risk": suggestion.risk,
                "pointer": suggestion.pointer,
                "diff": suggestion.diff,
                "suggested": suggestion.is_suggested(),
            }
        )
    return items


# ---------------------------------------------------------------------------
# 回填与主入口
# ---------------------------------------------------------------------------

def fill_pending(request: ConfirmRequest) -> List[Text]:
    """把裁决里的 `fill` 组织成**可贴的回填文本**。★**不落盘**——给人自己贴。

    产物形态刻意做成"一行一条 `目标.字段 = 值`"：它要能直接复制进文档或 YAML，
    而不是变成另一个需要解析的中间格式。
    """
    lines: List[Text] = []
    for decision in request.decisions:
        if decision.action != ACTION_ACCEPT or not decision.fill:
            continue
        for key, value in decision.fill.items():
            lines.append(f"{decision.target}.{key} = {value!r}")
    return lines


def notes_text(request: ConfirmRequest) -> Text:
    """留痕的 `notes`：把裁决**逐条**写下来（含 reject 的理由）。

    ★为什么理由要进 `notes` 而不是只留在请求里：请求是一次性的，
    而留痕的用途是**半年后回看**"当时为什么否掉它"。
    """
    lines: List[Text] = []
    if request.notes.strip():
        lines.append(request.notes.strip())
    for decision in request.decisions:
        mark = "接受" if decision.action == ACTION_ACCEPT else "拒绝"
        line = f"[{mark}] {decision.target}"
        if decision.reason.strip():
            line += f" —— 理由：{decision.reason.strip()}"
        lines.append(line)
    if request.target_path:
        lines.append(f"目标产物：{request.target_path}")
    return "\n".join(lines)


def confirm(payload: Any, *, record: bool = True) -> ConfirmResult:
    """确认门主入口：解析 → 校验 →（通过则）落痕。**不执行、不落产物**。

    ★落痕**委托** `reviews.write_review_record`——本模块**不自己写盘**：
    写入者是 `reviews`（T18 登记在册，写入根 `.ai/reviews/`），
    本模块只负责"把该记的东西组织对"。所以它不需要登记进 T18，
    `web.py` 也仍然保持"零写盘"的形态。

    ★**校验不过就一行都不写**：留痕是**证据**，半份证据比没有更坏
    （半个文件看起来像"确认过一半"，而实际上什么都没确认）。
    """
    result = ConfirmResult()

    request, parse_errors = parse_payload(payload)
    if request is None:
        result.errors = list(parse_errors)
        result.codes = [REFUSE_BAD_JSON]
        return result

    errors, codes = validate(request)
    if errors:
        result.errors = errors
        result.codes = codes
        return result

    result.accepted = [
        item.target for item in request.decisions if item.action == ACTION_ACCEPT
    ]
    result.rejected = [
        item.target for item in request.decisions if item.action == ACTION_REJECT
    ]
    result.applied = fill_pending(request)

    if record:
        from interfacetester_ai.reviews import write_review_record  # noqa: PLC0415

        try:
            result.record_path = write_review_record(
                request.case,
                draft_path=request.target_path or "(看板确认门)",
                draft_text=request.target_text,
                approver=request.approver,
                resolved_todos=tuple(result.accepted),
                notes=notes_text(request),
                approved_at=request.approved_at,
            )
        except (OSError, ValueError) as error:
            result.errors = [f"留痕写入失败：{type(error).__name__}: {error}"]
            result.codes = ["write_failed"]
            return result

    result.ok = True
    return result


# ---------------------------------------------------------------------------
# 自检（★**不写盘**：全部走 `record=False`——夹具写入归 `tests/`）
# ---------------------------------------------------------------------------

def _good_payload() -> Dict[Text, Any]:
    """一份合规提交（自检 / 测试共用形状）。"""
    return {
        "case": "login_flow",
        "approver": "zhangsan",
        "decisions": [
            {"target": "pending:S3:1", "action": "accept", "fill": {"expect": 200}},
            {
                "target": "fix:0:login_flow/步骤1",
                "action": "reject",
                "reason": "这是真实缺陷，不该改用例",
            },
        ],
        "target_path": ".ai/pending/login_flow.pending.json",
        "target_text": "some content\n",
    }


def run_selftest(verbose: bool = False) -> int:
    """confirm 自检。★**不落痕**（全部 `record=False`）——夹具写入归 `tests/`。"""
    failures: List[Text] = []
    notes: List[Text] = []

    good = _good_payload()

    # ① 通过路径（**record=False**：不落痕、不写盘）
    ok_result = confirm(good, record=False)
    if not ok_result.ok:
        failures.append(f"[通过] 合规提交竟然被拒：{ok_result.errors}")
    if ok_result.record_path:
        failures.append("[不写盘] `record=False` 时不该产生留痕路径")
    if ok_result.accepted != ["pending:S3:1"]:
        failures.append(f"[裁决] 接受清单不对：{ok_result.accepted}")
    if ok_result.rejected != ["fix:0:login_flow/步骤1"]:
        failures.append(f"[裁决] 拒绝清单不对：{ok_result.rejected}")
    if not ok_result.applied:
        failures.append("[回填] 接受项的 fill 没产出可贴文本")

    # ② ★★ 核心判据：reject **必须**带理由（含**纯空白**）
    for blank in ("", "   ", "\n\t "):
        bad = _good_payload()
        bad["decisions"][1]["reason"] = blank
        result = confirm(bad, record=False)
        if result.ok:
            failures.append(f"[reject 理由] 理由为 {blank!r} 时竟然通过了")
        elif REFUSE_NO_REASON not in result.codes:
            failures.append(f"[reject 理由] 原因码不对：{result.codes}")

    # ③ 必填项：approver / case / 非空 decisions
    for key, code in (("approver", REFUSE_NO_APPROVER), ("case", REFUSE_NO_CASE)):
        bad = _good_payload()
        bad[key] = "   "
        result = confirm(bad, record=False)
        if result.ok or code not in result.codes:
            failures.append(f"[必填] 缺 `{key}` 应当拒收（{code}）：{result.codes}")

    empty = _good_payload()
    empty["decisions"] = []
    result = confirm(empty, record=False)
    if result.ok or REFUSE_NO_TARGET not in result.codes:
        failures.append(f"[空表] 空 decisions 应当拒收（否则造出假证据）：{result.codes}")

    # ④ 未知 action / 重复 target / 缺 target
    bad = _good_payload()
    bad["decisions"][0]["action"] = "maybe"
    result = confirm(bad, record=False)
    if result.ok or REFUSE_BAD_ACTION not in result.codes:
        failures.append(f"[动作] 未知 action 应当拒收：{result.codes}")

    dup = _good_payload()
    dup["decisions"] = [dup["decisions"][0], dict(dup["decisions"][0])]
    result = confirm(dup, record=False)
    if result.ok:
        failures.append("[重复] 同一 target 出现两次应当拒收（以哪一次为准并不明确）")

    no_target = _good_payload()
    no_target["decisions"] = [{"target": "", "action": "accept"}]
    result = confirm(no_target, record=False)
    if result.ok or REFUSE_NO_TARGET not in result.codes:
        failures.append(f"[缺目标] 缺 target 应当拒收：{result.codes}")

    # ⑤ 坏 body：★**不猜**
    for raw in ("", "{ not json", b"\xff\xfe", 42, None):
        result = confirm(raw, record=False)
        if result.ok:
            failures.append(f"[坏 body] {raw!r} 竟然被解析成了有效提交")
        elif REFUSE_BAD_JSON not in result.codes:
            failures.append(f"[坏 body] {raw!r} 的原因码不对：{result.codes}")

    # ⑥ ★**校验不过就一行都不写**：`record=True` 但校验不过 → 无留痕路径
    bad = _good_payload()
    bad["approver"] = ""
    result = confirm(bad, record=True)
    if result.record_path:
        failures.append("[半份证据] 校验不通过时竟然产生了留痕路径")

    # ⑦ 理由进 `notes`（留痕的用途是**半年后回看**"当时为什么否掉它"）
    notes_text_value = notes_text(ConfirmRequest.from_dict(good))
    for piece in ("[接受]", "[拒绝]", "真实缺陷"):
        if piece not in notes_text_value:
            failures.append(f"[留痕] notes 里缺 `{piece}`：{notes_text_value}")

    # ⑧ `fill` 只对 accept 产出（reject 项的 fill 不该混进回填文本）
    mixed = _good_payload()
    mixed["decisions"][1]["fill"] = {"expect": "不该出现"}
    for line in fill_pending(ConfirmRequest.from_dict(mixed)):
        if "不该出现" in line:
            failures.append("[回填] reject 项的 fill 混进了回填文本")

    # ⑨ 待确认清单：**不存在**是正常状态（空表，不是异常）
    if not os.path.isdir(PENDING_DIR):
        notes.append("`.ai/pending/` 不存在 → 待确认清单为空（正常状态）")
    else:
        lists = load_pending_lists()
        notes.append(
            f"`.ai/pending/` 存在：{len(pending_paths())} 份清单"
            f"（其中读不到 {sum(1 for item in lists if item['error'])} 份）"
        )
        if len(lists) != len(pending_paths()):
            failures.append("[清单] 读到的清单数与文件数不等（有人被静默跳过了）")

    # ⑩ 元护栏（自检内能做的部分）：**证明 ② 的两份输入只差在理由上**——
    #    否则"被拒"可能来自别的字段，② 就成了打偏的判据。
    probe = _good_payload()
    probe["decisions"][1]["reason"] = "有理由"
    if not confirm(probe, record=False).ok:
        failures.append("[元护栏] 只差在「理由」上的两份输入结论应当相反，实测却没有")

    for note in notes:
        print(f"[confirm 自检] 说明：{note}")

    print("=" * 66)
    if failures:
        print(f"确认门自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("确认门自检全部通过（§2.5 行 339，**P3b-1**）：")
    print("  不写盘：落痕**委托** `reviews`（T18 登记在册）——本模块源码里没有写调用")
    print("  不执行：不跑用例、不调模型、不起子进程（「自动重跑 L2」属 P3b-2）")
    print("  ★reject 必须写理由：空 / 纯空白**一律拒收**（§2.5 行 339 原文）——")
    print("          理由空着还能 reject，等于把「为什么不要它」**永久丢掉**")
    print("  approver 必填：留痕的全部意义就是回答「谁确认的」；")
    print("          `decisions` 空表同样拒收——空表返回成功会造出**假证据**")
    print("  坏 body 不猜：格式不对就说格式不对（猜出来的请求会带着人的签名落进留痕）")
    print("  半份证据不落地：校验不过 → 一行都不写")
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

    parser = argparse.ArgumentParser(description="确认门自检（P3b-1）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "ACTION_ACCEPT",
    "ACTION_REJECT",
    "ACTIONS",
    "CASES_DIR",
    "PENDING_DIR",
    "PENDING_SUFFIX",
    "REFUSE_BAD_ACTION",
    "REFUSE_BAD_JSON",
    "REFUSE_NO_APPROVER",
    "REFUSE_NO_CASE",
    "REFUSE_NO_REASON",
    "REFUSE_NO_TARGET",
    "ConfirmDecision",
    "ConfirmRequest",
    "ConfirmResult",
    "confirm",
    "fill_pending",
    "fix_suggestions",
    "load_pending_lists",
    "notes_text",
    "parse_payload",
    "pending_paths",
    "run_selftest",
    "validate",
]


if __name__ == "__main__":
    raise SystemExit(main())




