# -*- coding: utf-8 -*-
"""quality.py —— **质量状态与晋级资格**（决策 `未做完的待决策项.md` §九 第一档①②；评审 §五 的状态表）。

## 为什么要有这个模块

改造前，一份产物"好不好"是靠 `AssemblyResult.ok()` 回答的，而 `ok()` 的语义实际是
"**进了 `cases/` 且没被闸门拒**"。后果（2026-09-26 实证，见 §九 9.4）：

- 一份**只断言 `status_code`** 的薄用例（`cases/T1_2_认证方式.yml`）也满足 `ok()`，
  而 `.ai/reviews/` 里**零留痕**——它和"人工复核过的用例"在调用方看来**没有区别**；
- 于是"产出成功 / 静态通过 / 待人工 / 跑过真环境 / 可自动晋级"这**五件不同的事**
  被压成了一个布尔值。使用者把"退出码 0"读成"用例可用"，是本仓最危险的一种误读：
  它不报错，只静默地把"没人看过的东西"当成"已确认的东西"。

本模块把五件事拆成**稳定状态 + 稳定 blocker 码**，并**只做纯计算**：
不读文件、不写文件、不调模型、不起子进程（→ **不进** `bench/write_boundary_scanner.py` 的写入者注册表）。

## 状态语义（与评审 §五 的表一致）

| 状态 | 含义 | 能不能当"可用" |
| --- | --- | --- |
| `generated` | 模型返回了可解析草稿 | ❌ 只代表**能解析** |
| `static_valid` | schema + L2 + 已知静态闸门都过 | ❌ 只代表**框架接受**，不代表语义正确 |
| `needs_review` | 有 blocker（仅协议断言 / `TODO_` / 未决降级项） | ❌ **默认落点**：交人工 |
| `runtime_verified` | 在**指定、隔离、可重置**的环境跑过且通过 | ❌ 不等于契约正确（还要看变异检出） |
| `auto_promotable` | 来源受支持 + 关键未知为零 + 静态/运行/变异门槛全过 | ✅ 仍是**资格**，另有策略开关决定是否转正 |
| `rejected` | 结构不合法 / 关键冲突 / 危险静默形态 | ❌ 终态，不落生产用例 |

★**两条硬规则**（都有自检钉住，且属"防假通过"那一类）：

1. **没有运行证据就到不了 `runtime_verified`**——`runtime=None` 时状态最高只到
   `static_valid`/`needs_review`。防的正是"把 L2 通过说成跑过了"（评审 §二 的口径混淆）。
2. **非法状态转换必须被拒**——`is_allowed_transition()` 是**白名单**，不是"什么都能变"。

★**`auto_promotable` 是资格、不是动作**：拿到它**不代表**产物已被转正。转正仍由
`assembler.promote_draft()`（经 `.ai/reviews/` 留痕）单独完成——**权限分离**（评审 §五 末段）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Text, Tuple


class QualityStatus:
    """质量状态（**稳定字符串**：会长期出现在报告与留痕里，不随文案改）。"""

    GENERATED = "generated"
    STATIC_VALID = "static_valid"
    NEEDS_REVIEW = "needs_review"
    RUNTIME_VERIFIED = "runtime_verified"
    AUTO_PROMOTABLE = "auto_promotable"
    REJECTED = "rejected"


ALL_STATUSES: Tuple[Text, ...] = (
    QualityStatus.GENERATED,
    QualityStatus.STATIC_VALID,
    QualityStatus.NEEDS_REVIEW,
    QualityStatus.RUNTIME_VERIFIED,
    QualityStatus.AUTO_PROMOTABLE,
    QualityStatus.REJECTED,
)
# ---------------------------------------------------------------------------
# 状态转换白名单
# ---------------------------------------------------------------------------
# 设计依据（逐条）：① 只能**逐级向上**，不许跳过"待人工"直接到"可晋级"；
# ② `rejected` 是**终态**（拒了的东西不该靠改标签复活——要重跑生成）；
# ③ `needs_review` 可**回到** `static_valid`（人工补齐后重判）或直接去 `runtime_verified`；
# ④ `auto_promotable` 允许**回退**（回滚是允许的），但只能退回"待人工/被拒"，不许退回"没跑过"。
_ALLOWED_TRANSITIONS: Dict[Text, Tuple[Text, ...]] = {
    QualityStatus.GENERATED: (
        QualityStatus.STATIC_VALID,
        QualityStatus.NEEDS_REVIEW,
        QualityStatus.REJECTED,
    ),
    QualityStatus.STATIC_VALID: (
        QualityStatus.NEEDS_REVIEW,
        QualityStatus.RUNTIME_VERIFIED,
        QualityStatus.REJECTED,
    ),
    QualityStatus.NEEDS_REVIEW: (
        QualityStatus.STATIC_VALID,
        QualityStatus.RUNTIME_VERIFIED,
        QualityStatus.REJECTED,
    ),
    QualityStatus.RUNTIME_VERIFIED: (
        QualityStatus.AUTO_PROMOTABLE,
        QualityStatus.NEEDS_REVIEW,
        QualityStatus.REJECTED,
    ),
    QualityStatus.AUTO_PROMOTABLE: (
        QualityStatus.REJECTED,
        QualityStatus.NEEDS_REVIEW,
    ),
    QualityStatus.REJECTED: (),
}


def is_allowed_transition(current: Text, target: Text) -> bool:
    """`current → target` 是不是合法转换（**未知名一律 `False`**：宁严不松）。"""
    return target in _ALLOWED_TRANSITIONS.get(current, ())


# ---------------------------------------------------------------------------
# blocker / warning：**稳定机器码**（评审 §五："不能只拼中文字符串"）
# ---------------------------------------------------------------------------

# blocker = 阻止"可直接使用 / 可自动晋级"的机器码
BLOCKER_STRUCTURE = "structure-rejected"  # 结构不合法 / 闸门拒（含危险静默形态）
BLOCKER_PROTOCOL_ONLY = "protocol-only"  # 断言**全是协议级**：能挡 4xx/5xx，测不出业务错
BLOCKER_TODO = "todo-placeholder"  # 还有 `${ENV(TODO_...)}` 没填值
BLOCKER_UNKNOWN_CRITICAL = "unknown-critical"  # 有未决降级项（端点/引用/条件对不上、模型零断言）
BLOCKER_NO_RUNTIME_EVIDENCE = "no-runtime-evidence"  # 没有"在隔离环境真跑过"的记录
BLOCKER_MUTATION_NOT_PASSED = "mutation-not-passed"  # 变异检出未达标（测不出已知缺陷）

# warning = 不阻止，但必须可见
WARNING_MERGE_NOTE = "merge-note"  # 合并时补了/没补上哪些断言
WARNING_DEGRADE = "degrade-unknown"  # 降级项（可见、不挡落盘）

# 这些 `unknowns.kind` **不是** blocker —— 它们是"合并过程说明"，属可见 warning。
# 其余 kind **默认按 blocker 处理**：让"新加的降级形态"默认拦住晋级，而不是默认放行
# （评审 §五 的口径：未知不许静默）。
_NON_BLOCKING_UNKNOWN_KINDS = ("merge-note",)


def _blockers_from_unknowns(unknowns: Sequence[Any]) -> Tuple[Tuple[Text, ...], Tuple[Text, ...]]:
    """把 `unknowns` 拆成 `(blockers, warnings)`（**稳定码**，不复制中文文案）。"""
    blockers: list = []
    warnings: list = []
    for item in unknowns or ():
        kind = getattr(item, "kind", "") or ""
        if kind in _NON_BLOCKING_UNKNOWN_KINDS:
            if WARNING_MERGE_NOTE not in warnings:
                warnings.append(WARNING_MERGE_NOTE)
            continue
        if kind == "protocol-only":
            if BLOCKER_PROTOCOL_ONLY not in blockers:
                blockers.append(BLOCKER_PROTOCOL_ONLY)
            continue
        # `merge-skip`（模型草稿零断言、合并**不救它**）是"模型没干活"的质量信号 → blocker；
        # 其余（`quote-miss` / `endpoint-not-in-doc` / 未来新形态）同样按 blocker 处理。
        if BLOCKER_UNKNOWN_CRITICAL not in blockers:
            blockers.append(BLOCKER_UNKNOWN_CRITICAL)
        if WARNING_DEGRADE not in warnings:
            warnings.append(WARNING_DEGRADE)
    return tuple(blockers), tuple(warnings)
# ---------------------------------------------------------------------------
# 运行证据（WP5 的接口面：**当前不产生**，但状态机必须先能表达）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeEvidence:
    """一次隔离执行的证据（评审 §五 的 `runtime_verified` 行：要记环境/数据/时间）。

    ★`executed` 与 `passed` **刻意分开**：没执行过（`executed=False`）与
    "执行了但没过"，是两件不同的事——合成一个布尔值，会让"没跑"看起来像"跑了没过"。
    """

    executed: bool = False
    passed: bool = False
    environment: Text = ""
    repeated_stable: bool = False
    mutation_killed: int = 0
    mutation_total: int = 0

    def mutation_rate(self) -> Optional[float]:
        """变异检出率；★**分母为 0 → `None`**（不许把"没测"算成 100%）。"""
        if self.mutation_total <= 0:
            return None
        return self.mutation_killed / self.mutation_total


@dataclass(frozen=True)
class QualityAssessment:
    """一份产物的质量评估（**纯数据**，可直接进报告 / 审核清单 / 留痕）。"""

    status: Text = QualityStatus.GENERATED
    blockers: Tuple[Text, ...] = ()
    warnings: Tuple[Text, ...] = ()
    reasons: Tuple[Text, ...] = ()

    def ok(self) -> bool:
        """⚠ **`ok()` 不是"可用"** —— 它只表示"**没有 blocker 拦住静态可用**"。

        ★命名刻意保留（旧调用方兼容），语义按评审 §五 收紧：
        "可直接使用"用 `is_usable()`；"可自动晋级"用 `is_auto_promotable()`。
        """
        return not self.blockers and self.status != QualityStatus.REJECTED

    def is_usable(self) -> bool:
        """能不能当"无需审核直接进 CI"用 —— **只有 `auto_promotable` 算**。"""
        return self.status == QualityStatus.AUTO_PROMOTABLE

    def is_auto_promotable(self) -> bool:
        return self.status == QualityStatus.AUTO_PROMOTABLE

    def needs_review(self) -> bool:
        return self.status == QualityStatus.NEEDS_REVIEW

    def render(self) -> Text:
        """一行给人看：`needs_review · protocol-only、unknown-critical`。"""
        if self.blockers:
            return f"{self.status} · {'、'.join(self.blockers)}"
        return self.status

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "status": self.status,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "reasons": list(self.reasons),
            "usable_without_review": self.is_usable(),
        }
# ---------------------------------------------------------------------------
# 评估入口（**纯函数**：同样输入必得同样结果）
# ---------------------------------------------------------------------------


def assess(
    *,
    rejected: bool = False,
    gate_codes: Sequence[Text] = (),
    unknowns: Sequence[Any] = (),
    todo_items: Sequence[Any] = (),
    runtime: Optional[RuntimeEvidence] = None,
    mutation_required_rate: float = 0.9,
) -> QualityAssessment:
    """由装配事实计算质量状态。

    判据顺序（**顺序本身就是口径**）：

    1. `rejected`（闸门拒 / 结构不合法）→ `rejected`（**终态**）；
    2. 有 blocker（仅协议断言 / `merge-skip` / 未决降级项）→ `needs_review`；
    3. 无 blocker → `static_valid`；
    4. 有**隔离执行且通过**的证据 → `runtime_verified`（★没证据**永不**到这一档）；
    5. 且**重复稳定** + **变异检出率达标** → `auto_promotable`。

    ★blocker **优先于**运行证据：跑通了但断言全是协议级，仍然只是 `needs_review`
    ——"能跑"和"测得有意义"是两件事（评审 §二 的第三条口径）。
    """
    reasons: list = []

    if rejected:
        codes = tuple(gate_codes or ())
        reason = "结构/闸门拒绝：被拒的产物不落生产用例（终态）"
        if codes:
            reason += "（gate_codes=" + ", ".join(codes) + "）"
        return QualityAssessment(
            status=QualityStatus.REJECTED,
            blockers=(BLOCKER_STRUCTURE,),
            reasons=(reason,),
        )

    blocker_list, warnings = _blockers_from_unknowns(unknowns)
    blockers = list(blocker_list)

    if todo_items:
        if BLOCKER_TODO not in blockers:
            blockers.append(BLOCKER_TODO)
        reasons.append(
            f"还有 {len(tuple(todo_items))} 条未确认占位（`TODO_`）：期望值等人填，"
            "没填就不许进 `cases/`"
        )
    if BLOCKER_PROTOCOL_ONLY in blockers:
        reasons.append(
            "断言**全是协议级**（如 `status_code`）：能挡 4xx/5xx，但**测不出业务错** "
            "→ 交人工补一条文档级断言"
        )
    if BLOCKER_UNKNOWN_CRITICAL in blockers:
        reasons.append(
            "存在未决的降级项（引用/端点/条件对不上，或模型零断言）→ 见 "
            "`.ai/draft/<用例>.unknowns.json`，逐条处置后才可重判"
        )

    status = QualityStatus.NEEDS_REVIEW if blockers else QualityStatus.STATIC_VALID
    if not blockers:
        reasons.append(
            "静态通过：schema + L2 + 已知静态闸门全过 —— ★这只证明**框架接受**，不证明语义正确"
        )

    if runtime is not None and runtime.executed and runtime.passed:
        rate = runtime.mutation_rate()
        if blockers:
            reasons.append("虽有运行证据，但仍有 blocker → **不晋级**（blocker 优先于运行结论）")
        elif not runtime.repeated_stable:
            status = QualityStatus.RUNTIME_VERIFIED
            reasons.append("跑过且通过，但**缺重复执行稳定率**证据 → 只到 `runtime_verified`")
        elif rate is None:
            status = QualityStatus.RUNTIME_VERIFIED
            reasons.append(
                "**没有任何变异样本**（分母 0）→ 不许把「没测检出能力」算成达标，只到 `runtime_verified`"
            )
        elif rate < mutation_required_rate:
            status = QualityStatus.RUNTIME_VERIFIED
            blockers.append(BLOCKER_MUTATION_NOT_PASSED)
            reasons.append(
                f"变异检出率 {rate:.0%} 低于门槛 {mutation_required_rate:.0%} → 只到 `runtime_verified`"
            )
        else:
            status = QualityStatus.AUTO_PROMOTABLE
            reasons.append(
                f"隔离执行通过 + 重复稳定 + 变异检出率 {rate:.0%} ≥ "
                f"{mutation_required_rate:.0%} → 具备晋级**资格**（转正仍是独立动作）"
            )
    elif runtime is not None and runtime.executed and not runtime.passed:
        reasons.append(
            "有隔离执行记录但**未通过** → 归因要按环境/认证/数据准备/请求构造/断言/清理分开报，"
            "不当作可用"
        )
    else:
        if status == QualityStatus.STATIC_VALID:
            reasons.append(
                "**没有**隔离执行证据（`executed=False`）→ 最高只到 `static_valid`："
                "L2 通过**不等于**目标服务跑得通（评审 §二 的口径）"
            )

    return QualityAssessment(
        status=status,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        reasons=tuple(reasons),
    )


def promotion_gaps(assessment: QualityAssessment) -> Tuple[Text, ...]:
    """「**为什么还不能自动晋级**」的完整清单（= `blockers` + **晋级缺口**）。

    ★为什么要跟 `blockers` 分开：`blockers` 是"**需要人工处置**的事"（仅协议断言、`TODO_` 未填、
    未决降级项）；而"**没有运行证据**"是**每一份新产物都有的常态**——若把它塞进 `blockers`，
    每份干净草稿都会变成"待人工"，状态就失去区分度（判据自检 #⑤ 钉住这一点）。
    但对外解释"为什么它还不能进 CI"时，这条必须说出来 → 放在本函数里。
    """
    gaps = list(assessment.blockers)
    if assessment.status != QualityStatus.AUTO_PROMOTABLE:
        if not assessment.blockers:
            gaps.append(BLOCKER_NO_RUNTIME_EVIDENCE)
    return tuple(gaps)
class UnknownView:
    """从 `.ai/draft/<用例>.unknowns.json` 的一条记录里**只取 `.kind`** 的最小视图。

    ★为什么要有它（而不是让 `quality.py` 直接 import 装配器的 `Unknown`）：
    本模块刻意**不依赖装配器**（依赖方向：装配器 → quality，不是反过来）——
    否则"状态判定"会被"产物怎么产生"绑死，改装配器就得担心质量口径漂移。
    """

    __slots__ = ("kind",)

    def __init__(self, kind: Text) -> None:
        self.kind = kind


def unknown_views(payload: Optional[Any]) -> Tuple[Any, ...]:
    """把 `unknowns.json` 的 payload（或裸列表）转成只带 `kind` 的视图元组。

    ★**宽容**：坏 payload / 缺 `items` / 元素不是 dict → 退回空元组（**不抛**）。
    理由：这是"读既有证据"的路径，产物可能是人手改过的；抛异常会让
    `haify review --list` 整条命令崩在"某一份坏文件"上，而用户只是想看看清单。
    """
    if isinstance(payload, dict):
        items = payload.get("items")
    elif isinstance(payload, (list, tuple)):
        items = payload
    else:
        items = None
    if not isinstance(items, (list, tuple)):
        return ()
    views = []
    for item in items:
        if isinstance(item, dict):
            views.append(UnknownView(str(item.get("kind", ""))))
    return tuple(views)


def assess_from_artifacts(
    draft_text: Text,
    unknowns_payload: Optional[Any] = None,
    *,
    runtime: Optional[RuntimeEvidence] = None,
    mutation_required_rate: float = 0.9,
) -> QualityAssessment:
    """从**既有产物**复算质量状态（草稿文本 + `.ai/draft/<用例>.unknowns.json`）。

    ★为什么"复算"而不是"读一份装配时写的质量结果文件"（两条理由都来自本仓的纪律）：

    1. **审核的是"当前那一版内容"**：转正前人会改草稿（填 `TODO_`），
       此时"装配时算出来的状态"**已经过期**。复算与 `reviews.draft_hash` 是同一口径
       ——"确认绑的是当时那一版内容"。
    2. **不新增写盘点**：`unknowns.json` 本来就有（装配器写的），复算不必给每份产物
       再写一份 `*.quality.json`，`bench/write_boundary_scanner.py` 的注册表也不用动。

    判据来源：草稿文本里的 `TODO_`（走 `assertions.has_todo` 的**同一套**口径，
    不另立一份"什么算 TODO_"）+ `unknowns` 的 `kind`。

    ★**本函数只做计算**（连文件都不读）：`unknowns_payload` 由调用方读好传进来，
    这样它仍是纯函数，可以放进自检与单测。
    """
    from interfacetester_ai.assertions import has_todo  # noqa: PLC0415 - 局部 import：避免模块级依赖

    unknowns = unknown_views(unknowns_payload)
    todo_items = ({"case": "draft"},) if has_todo(draft_text or "") else ()
    return assess(
        unknowns=unknowns,
        todo_items=todo_items,
        runtime=runtime,
        mutation_required_rate=mutation_required_rate,
    )


def missing_blockers(
    actual: Sequence[Text], resolved: Sequence[Text]
) -> Tuple[Text, ...]:
    """`actual` 里**没被处置**的 blocker（= `actual - resolved`，保持原顺序、去重）。

    ★这就是"人工确认"这一步的机器判据（WP1.2）：**自由文本备注不能算处置**——
    处置必须**逐条点名**（CLI 的 `--resolve-blocker <码>`），
    否则"我看了"会变成一句没有对象的话，而闸门也就退化成橡皮图章。
    """
    done = {item for item in resolved or ()}
    missing: list = []
    for code in actual or ():
        if code not in done and code not in missing:
            missing.append(code)
    return tuple(missing)
# ---------------------------------------------------------------------------
# 自检（**纯逻辑、不写盘、不出网**）
# ---------------------------------------------------------------------------


class _FakeUnknown:
    """自检用：最小 `Unknown` 替身（本模块只读它的 `.kind`，不依赖装配器）。"""

    def __init__(self, kind: Text) -> None:
        self.kind = kind


def _selftest() -> int:
    """自检：**每条硬规则都要有成对判据**（违规必红 / 合规必绿）。"""
    failures: list = []

    def expect(condition: bool, message: Text) -> None:
        if not condition:
            failures.append(message)

    # ① 非法转换必须被拒（防"改个标签就复活"）
    expect(
        not is_allowed_transition(QualityStatus.REJECTED, QualityStatus.STATIC_VALID),
        "[转换] `rejected` 是终态，不该能变成 `static_valid`",
    )
    expect(
        not is_allowed_transition(QualityStatus.GENERATED, QualityStatus.RUNTIME_VERIFIED),
        "[转换] 不许从 `generated` 直接跳到 `runtime_verified`",
    )
    expect(
        is_allowed_transition(QualityStatus.STATIC_VALID, QualityStatus.RUNTIME_VERIFIED),
        "[转换] 静态通过 → 运行通过 是合法路径",
    )

    # ② 闸门拒 → rejected（终态）
    rejected = assess(rejected=True, gate_codes=("S4",))
    expect(rejected.status == QualityStatus.REJECTED, "[拒绝] 闸门拒必须是 `rejected`")
    expect(not rejected.ok(), "[拒绝] 被拒的产物 `ok()` 必须为假")

    # ③ ★核心：仅协议断言 → needs_review（旧行为是**落 cases/**，这正是本轮要改的）
    protocol = assess(unknowns=[_FakeUnknown("protocol-only")])
    expect(protocol.status == QualityStatus.NEEDS_REVIEW, "[薄用例] 仅协议断言必须待人工")
    expect(BLOCKER_PROTOCOL_ONLY in protocol.blockers, "[薄用例] blocker 码要稳定可见")
    expect(not protocol.is_usable(), "[薄用例] 不许被当成可用")

    # ④ `TODO_` 未填 → needs_review
    expect(
        assess(todo_items=[{"case": "x"}]).status == QualityStatus.NEEDS_REVIEW,
        "[TODO] 没填值必须待人工",
    )

    # ⑤ 干净静态通过 → static_valid（且**仍然不是**可用；且不因"缺运行证据"变成待人工）
    clean = assess()
    expect(clean.status == QualityStatus.STATIC_VALID, "[干净] 无 blocker 应为 static_valid")
    expect(clean.ok(), "[干净] 干净草稿的 `ok()` 必须为真（否则状态失去区分度）")
    expect(not clean.is_usable(), "[干净] 静态通过**不等于**可直接使用")
    expect(
        promotion_gaps(clean) == (BLOCKER_NO_RUNTIME_EVIDENCE,),
        "[干净] 晋级缺口要能说出「没有运行证据」这条",
    )

    # ⑥ ★防假通过：没有执行证据 → 永不 runtime_verified
    expect(
        assess(runtime=RuntimeEvidence(executed=False, passed=False)).status
        == QualityStatus.STATIC_VALID,
        "[假通过] 没执行却越过了 static_valid",
    )
    expect(
        assess(runtime=RuntimeEvidence(executed=False, passed=True)).status
        == QualityStatus.STATIC_VALID,
        "[假通过] `executed=False` 时报 `passed=True` 也必须停在 static_valid",
    )

    # ⑦ 跑通过但**没有变异样本** → 不许晋级（分母 0 不是 100%）
    expect(
        assess(runtime=RuntimeEvidence(executed=True, passed=True, repeated_stable=True)).status
        == QualityStatus.RUNTIME_VERIFIED,
        "[变异] 没有变异样本却晋级了（分母 0 被当成 100%）",
    )
    # ⑧ 跑通过但**不稳定** → 同样不晋级
    expect(
        assess(
            runtime=RuntimeEvidence(
                executed=True, passed=True, repeated_stable=False, mutation_killed=20, mutation_total=20
            )
        ).status
        == QualityStatus.RUNTIME_VERIFIED,
        "[稳定率] 缺重复稳定证据却晋级了",
    )

    # ⑨ 门槛全过 → auto_promotable；检出率不达标 → 停在 runtime_verified
    full = assess(
        runtime=RuntimeEvidence(
            executed=True, passed=True, repeated_stable=True, mutation_killed=19, mutation_total=20
        )
    )
    expect(full.status == QualityStatus.AUTO_PROMOTABLE, "[晋级] 门槛全过应得资格")
    expect(full.is_usable(), "[晋级] 资格态才允许 is_usable()")
    expect(
        assess(
            runtime=RuntimeEvidence(
                executed=True,
                passed=True,
                repeated_stable=True,
                mutation_killed=1,
                mutation_total=10,
            )
        ).status
        == QualityStatus.RUNTIME_VERIFIED,
        "[变异] 检出率不达标不该晋级",
    )
    expect(
        BLOCKER_MUTATION_NOT_PASSED
        in assess(
            runtime=RuntimeEvidence(
                executed=True,
                passed=True,
                repeated_stable=True,
                mutation_killed=1,
                mutation_total=10,
            )
        ).blockers,
        "[变异] 不达标要留下稳定码",
    )

    # ⑩ blocker **优先于**运行证据（跑通了也救不回语义薄）
    expect(
        assess(
            unknowns=[_FakeUnknown("protocol-only")],
            runtime=RuntimeEvidence(
                executed=True,
                passed=True,
                repeated_stable=True,
                mutation_killed=20,
                mutation_total=20,
            ),
        ).status
        == QualityStatus.NEEDS_REVIEW,
        "[优先级] 有 blocker 时不该凭运行证据晋级",
    )

    # ⑪ 未决降级项（quote-miss 这一类）→ needs_review；合并说明（merge-note）**不**拦
    expect(
        assess(unknowns=[_FakeUnknown("quote-miss")]).status == QualityStatus.NEEDS_REVIEW,
        "[未知] 未决降级项必须待人工",
    )
    note_only = assess(unknowns=[_FakeUnknown("merge-note")])
    expect(note_only.status == QualityStatus.STATIC_VALID, "[合并说明] 不该被当成 blocker")
    expect(WARNING_MERGE_NOTE in note_only.warnings, "[合并说明] 但必须可见")

    # ⑫ 文本渲染不崩（CLI 会打它）+ 序列化形状稳定
    expect(protocol.render().startswith("needs_review"), "[渲染] 一行文案要带状态名")
    expect(
        set(clean.to_dict()) == {"status", "blockers", "warnings", "reasons", "usable_without_review"},
        "[序列化] `to_dict()` 的键集是公开面，不许悄悄改",
    )

    # ⑬ 从既有产物复算（WP1.2 的入口）：`unknowns.json` → 状态；`TODO_` 直接成 blocker
    recomputed = assess_from_artifacts(
        "validate:\n- equal:\n  - body.x\n  - 1\n",
        {"items": [{"kind": "protocol-only", "level": "degrade"}]},
    )
    expect(
        recomputed.status == QualityStatus.NEEDS_REVIEW
        and BLOCKER_PROTOCOL_ONLY in recomputed.blockers,
        "[复算] 从 unknowns.json 复算出来的状态不对",
    )
    expect(
        QualityStatus.NEEDS_REVIEW
        == assess_from_artifacts('    - "${ENV(TODO_X)}"\n').status,
        "[复算] 草稿里有 `TODO_` 却没被算成待人工",
    )
    expect(
        assess_from_artifacts("url: /x\n").status == QualityStatus.STATIC_VALID,
        "[复算] 干净草稿应复算为 static_valid",
    )

    # ⑭ 坏 payload 不许把命令打崩（`review --list` 会挨个读产物）
    for bad in (None, "不是字典", {"items": "也不是列表"}, {"items": [1, "x", {}]}):
        try:
            unknown_views(bad)
        except Exception as error:  # noqa: BLE001 - 这里就是要抓"任何异常"
            failures.append(f"[宽容] 坏 payload {bad!r} 抛异常了：{error!r}")
    expect(
        len(unknown_views({"items": [{"kind": "a"}, "坏", {"nope": 1}]})) == 2,
        "[宽容] 非 dict 元素应被跳过、dict 元素应被收下",
    )

    # ⑮ "处置"必须逐条点名：`missing_blockers` 是人工确认这一步的机器判据
    expect(
        missing_blockers(("protocol-only", "unknown-critical"), ())
        == ("protocol-only", "unknown-critical"),
        "[处置] 什么都没声明时，全部 blocker 都算未处置",
    )
    expect(
        missing_blockers(
            ("protocol-only", "unknown-critical"), ("protocol-only",)
        )
        == ("unknown-critical",),
        "[处置] 只声明一部分时，漏掉的那条必须仍然拦住（自由文本不算处置）",
    )
    expect(
        missing_blockers(("protocol-only",), ("protocol-only", "todo-placeholder")) == (),
        "[处置] 多声明不算错（`resolved ⊇ actual` 即可）",
    )
    expect(
        missing_blockers(("a", "a"), ("b",)) == ("a",),
        "[处置] 重复的 blocker 码只报一次",
    )

    if failures:
        for item in failures:
            print("[quality 自检] " + item)
        return 1
    print(
        "[quality 自检] 15 组判据全部通过：非法转换被拒 / 仅协议断言待人工 / "
        "无运行证据不晋级 / blocker 优先于运行结论 / 处置要逐条点名"
    )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())




