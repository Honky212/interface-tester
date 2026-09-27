# -*- coding: utf-8 -*-
r"""gen —— **生成流程编排**（§5.1 `haify gen`；**P0a** E 阶段）。

## 它把已就位的积木串成一条链

    doc_quality（生成前体检 S-10）→ 拒 → reports/NEEDS_DOC_FIX.md（交回文档作者）
    slicer（标题层级切片 + 字节预算）
    llm（`FakeTransport` 离线回放 / `HttpTransport` 真跑；缓存 + 审计走端口）
    schema.parse_draft（L1 结构校验）
    assembler.assemble（T28 对账 + 闸门前置 S-4 + 落盘 cases/）
      ↓ 任一层失败
    修正环：报错**截断至 4KB** 回喂，≤2 轮（§5.1）；用尽仍失败 → `.ai/failed/` + NEEDS_HUMAN.md

## 边界纪律（红线③）

本模块**一律不写盘**：落盘全部委托给**登记在册**的模块
（`doc_quality`→`reports/`、`cache`→`.ai/cache/`、`manifest`→`.ai/manifest/`、
`assembler`→`cases/`+`.ai/`）。所以它**不需要**写盘白名单登记——
**编排者的纪律是"不自己动手"**：一旦它自己写文件，"谁有权写哪儿"就多了一个说不清的地方。

## 修正环的四个类别（§5.1 "按报错类别选回喂模板"）

| 类别 | 触发 | 回喂要说什么 |
| --- | --- | --- |
| `l1-format` | 响应不是 JSON（剥围栏后仍不合法） | 只输出一个 JSON 对象，不要解释、不要围栏 |
| `l1-schema` | `DraftValidationError`（字段多/少、类型错、凭据空） | **原文照抄** schema 的报错——它已经点名到 `steps[i].validate[j]` |
| `l2-emit` | `validate_emitted_case` 抛错（加载 / hmake 渲染 / 算子白名单） | 说明"结构过了但框架校验没过"，附框架原文 |
| `gate` | 闸门 REJECT（S 系列） | 闸门编号 + 逐条 finding（人话） |

**★预算超限不重试**（`InputBudgetExceeded`）：那是切片/文档的问题，重试不会变好，
只会**重复花钱**——直接抛出交给调用方（§5.1 的"不静默截断"在流程层的延伸）。

## 为什么一片一个用例（而不是"整篇一个多步用例"）

切片是 §5.1 定的 **LLM 输入单位**，而标题层级块通常正好是"一个接口"。
按片出用例有两个好处：① 每片的 `source_quote` 溯源范围**就是它自己**（不会跨片引用）；
② 单片失败**不拖累**其它片（修正环与 `unknowns` 都是片级的）。
跨片拼 steps 需要判断"这两片讲的是同一个链路吗"——那是语义判断，**不由这里做**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

from interfacetester_ai.doc_quality import DocReport, audit_text, write_needs_doc_fix
from interfacetester_ai.llm import (
    LLMConfig,
    LLMRequest,
    LLMResponseFormatError,
    complete,
    redact_sensitive,
)
from interfacetester_ai.prompts import (
    PROMPT_VERSION,
    InputBudgetExceeded,
    build_prompt_payload,
    render_system_prompt,
)
from interfacetester_ai.schema import CaseDraft, DraftValidationError, QUOTE_FIELD, parse_draft
from interfacetester_ai.slicer import (
    ENDPOINT_RE,
    TITLE_RE,
    Slice,
    effective_budget,
    heading_spans,
    split_document,
)

# 修正环：报错回喂**截断至 4KB**（§5.1）
FEEDBACK_LIMIT = 4096
# 修正环轮数（§5.1：`max_rounds=2`）——即"首发 1 次 + 修正 2 次"
MAX_ROUNDS = 2


class GenerationFailed(Exception):
    """生成失败（不可重试，或修正环用尽）。"""

    def __init__(self, message: Text, rounds: Optional[Sequence["Round"]] = None) -> None:
        self.rounds = list(rounds or [])
        super().__init__(message)


@dataclass(frozen=True)
class Round:
    """修正环的一轮：**类别 + 回喂给模型的原文**（复盘时能看出"它在改什么"）。"""

    index: int
    kind: Text
    message: Text = ""

    def to_dict(self) -> Dict[Text, Any]:
        return {"index": self.index, "kind": self.kind, "message": self.message}


@dataclass
class GenResult:
    """一片文档的生成结果。"""

    case_name: Text
    ok: bool = False
    slice_title: Text = ""
    rounds: List[Round] = field(default_factory=list)
    assembly: Any = None  # AssemblyResult
    draft: Optional[CaseDraft] = None
    response_paths: List[Text] = field(default_factory=list)
    # 内容闸（§六）在**送出去之前**替换掉的凭据（清单给调用方打印/留痕）
    redactions: List[Dict[Text, Any]] = field(default_factory=list)
    # ★"本片没有接口"——**不是失败**（2026-09-25 新增）。两个来源：
    #   ① `skip-static`：调用**前**形态判据说这里没有端点（省下 3 轮调用）；
    #   ② `skip-model`：模型返回 `steps: []`（它自己判断本节没有可测的接口）。
    # 它是"分母修正"的唯一依据：报告与退出码都要把跳过 **和真失败分开算**。
    skipped: bool = False

    def last_kind(self) -> Text:
        return self.rounds[-1].kind if self.rounds else ""

    def skip_reason(self) -> Text:
        """跳过原因原文。**没有它等于静默跳过** —— 本仓最忌讳的形态。"""
        for rnd in self.rounds:
            if rnd.kind in ("skip-static", "skip-model"):
                return rnd.message
        return ""

    def outcome(self) -> Text:
        """三态：`ok` / `skip` / `fail` —— 报告文案与退出码都按它算。"""
        if self.ok:
            return "ok"
        return "skip" if self.skipped else "fail"

    def to_dict(self) -> Dict[Text, Any]:
        payload: Dict[Text, Any] = {
            "case": self.case_name,
            "ok": self.ok,
            "outcome": self.outcome(),
            "skipped": self.skipped,
            "slice": self.slice_title,
            "rounds": [item.to_dict() for item in self.rounds],
            "last_kind": self.last_kind(),
        }
        if self.skipped:
            payload["skip_reason"] = self.skip_reason()
        if self.assembly is not None:
            payload["assembly"] = self.assembly.to_dict()
        return payload


def truncate_feedback(text: Text, limit: int = FEEDBACK_LIMIT) -> Text:
    """回喂文本截断（§5.1 的 4KB）——**截的是给模型看的那份**，原始报错仍完整留给日志。

    NOTICE：这里**允许**截断（与输入预算的"不许截断"不冲突）：输入预算是"模型看什么
    才能正确生成"，截了它等于喂半句话；而报错是"模型要改什么"，4KB 足够覆盖
    schema 的全部问题（它一次报全，但单条很短），超出部分通常是重复堆栈。
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（报错过长，已截断至 {limit} 字符；完整内容见运行日志）"


def classify_error(error: BaseException) -> Tuple[Text, Text]:
    """把异常翻成 `(类别, 回喂文本)`——这是"按报错类别选模板"的机器形态。"""
    if isinstance(error, LLMResponseFormatError):
        return "l1-format", truncate_feedback(
            f"{error}\n\n请**只输出一个 JSON 对象**：不要解释性文字、不要 markdown 围栏、不要"
            "把自己写的注释留在 JSON 里。"
        )
    if isinstance(error, DraftValidationError):
        problems = "\n".join(f"  - {item}" for item in error.problems)
        return "l1-schema", truncate_feedback(
            f"你的 JSON 结构不合法（{len(error.problems)} 处）：\n{problems}\n\n"
            "逐条修正上面每一项后重新输出**完整**的 JSON（不要只输出被改的部分）。"
        )
    return "l2-emit", truncate_feedback(
        f"产物过不了框架校验：{type(error).__name__}: {error}\n\n"
        "结构与字段名也许合法，但算子/取值/写法不被框架接受；请按上面的原文修正。"
    )


# ---------------------------------------------------------------------------
# "这片像不像接口" —— **跳过判据**（2026-09-25 真模型实测新增）
# ---------------------------------------------------------------------------

# ★为什么需要它（实测数据）：`project-three/统一文件服务平台…` 15 片里**只有 2 片是真接口**
# （`POST /oauth2/token`、`POST /api/directory/create`），其余 13 片是标题 / 接口概述 /
# 服务地址 / 公共请求头 / 响应格式 / 错误码表 / 目录服务接口（标题）/ 附录 / 路径格式规范 /
# 代码参考 / 常见问题。改动前，那 13 片**照样被送去调模型**（每片 3 轮 = 39 次调用），
# 而模型对它们的回应只有两种：**编一个不存在的端点**（`/api/status`、`/api/placeholder`、
# `/dummy/success`、`/api/file_service`）或**给不出合法结构**（`l1-schema`）。
# 结局在报告里**和真失败混在一起**，于是"15 片里 13 片失败"看起来像工具坏了 ——
# 实际是**分母错了**：它们本就不该产出用例。
#
# ★判据为什么**要严**（必须 `METHOD` + 路径**同现**，而不是"有表格 / 有 URL"就放行）：
#   跳过判错的代价是**漏掉真接口**（用户拿不到用例，还不知道为什么），
#   而不跳过的代价只是**多一次模型调用**（可接受）。所以宁可多调一次，也不能把
#   "有字段表但其实是公共请求头约定"的片当接口跳过 —— **两个方向的代价不对等**。
#   实测：本判据对那份文档给出 `[4, 11]`，**恰好就是那两个真接口**，零误判。
#
# ★形态覆盖（真文档里三种写法都要认，且**不限行首**——表格里的端点行首是 `|`）：
#   - 标题：`## POST /api/order`
#   - 表格：`| **URL** | `POST /api/directory/create` |`
#   - curl：`curl -X POST https://host/api/directory/create`
SLICE_ENDPOINT_RE = ENDPOINT_RE  # ★单一来源在 `slicer.ENDPOINT_RE`（`citation` 也要用同一份）


# ★**交叉引用行**（(c)④-①，2026-09-26）：真实文档里那种"**指向本文档别的章节**"的句子，
#   例如（实测原文）：
#       - > 本节包含元数据查询接口；文件重命名接口（`POST /api/metadata/rename`）见 [3.5 文件重命名](#35-文件重命名)。
#   它**含端点形态**，但那端点**不是本段自己的**声明——模型照它给出别的段的 url、0 断言，
#   白烧一次调用（实测：`## 5. 元数据服务接口` 的片就是这样被送进模型的）。
#
# ★判据**刻意保守**（只认 markdown **内部锚点链接** `](#`）：
#   真接口声明行几乎不可能带 `](#`，所以这条规则**不会误剔真端点**；
#   而"宁可多调一次"的立论仍在——**只有整片端点全在交叉引用行上**时才跳过（见下）。
_CROSS_REF_LINK_RE = re.compile(r"\]\(#")


def _line_is_cross_reference(line: Text) -> bool:
    """这一行是不是"指向本文档别处"的交叉引用（★必须**同时**含端点形态与内部锚点链接）。"""
    return bool(SLICE_ENDPOINT_RE.search(line or "")) and bool(
        _CROSS_REF_LINK_RE.search(line or "")
    )


def slice_looks_like_interface(slice_text: Text) -> bool:
    """该切片里有没有**具体的端点**（`METHOD` + 路径同现）——决定要不要送模型。

    ★它**不是**"这片有没有接口"的**语义**判断（那是模型的事），而是**确定性形态判据**：
    找不到端点形态 → 这片里没有可写的接口要素 → **跳过，别烧模型**。

    返回 False 的片会在报告里**逐片列出原因**（不静默跳过），并可用
    `--no-skip-non-interface` 强制全部送模型（把选择权交回调用方 —— 判据再准也不该
    由它替用户拍板）。

    ★**负向特例**（(c)④-①）：若该片里**所有**端点形态都出现在**交叉引用行**上
    （`… 见 [3.5 xxx](#35-xxx)。`），那这一片**没有自己的端点** → 与"没有端点"同等处理
    （跳过；那个真端点在它自己的段里会被正常处理 ✓）。
    ★**成对约束**：只要片里**还有一条**非交叉引用的端点行（标题式 / 表格式 / curl），
    就**照旧送模型**——不许因为片里有一句交叉引用就把整个真接口片跳掉。
    """
    text = slice_text or ""
    if not SLICE_ENDPOINT_RE.search(text):
        return False
    endpoint_lines = [line for line in text.split("\n") if SLICE_ENDPOINT_RE.search(line)]
    if endpoint_lines and all(_line_is_cross_reference(line) for line in endpoint_lines):
        return False
    return True


# ---------------------------------------------------------------------------
# prompt 组装（§5.1 的**输入预算**在这里生效）
# ---------------------------------------------------------------------------

USER_TEMPLATE = """以下是一份接口文档的**一个章节**（标题路径：{title}）。

要求：只针对这一节写到的接口生成用例草稿；本节没写的事实**不要编造**——
拿不准就少写一条断言（系统会把它列进 unknowns 交人工确认）。

每条 validate 必须带 {quote}（本节原文里的**原句**，逐字）。

文档内容：
---
{body}
---
"""

# 输出封顶（§六 的 `max_tokens`）：给足写一份用例的量，又不至于让模型开始"自由发挥"
DEFAULT_MAX_TOKENS = 4096


# ★**服务地址候选**（`config.base_url` 的出处）：产物里的 `url` 是**相对路径**，
#   而内核 `parser.build_url()` 要求 `base_url` 是**字面量 URL** —— `base_url: ${ENV(BASE_URL)}`
#   会被当成"没写"（`netloc` 空 → `ParamsError: base url missed!`，2026-09-26 实测）。
#   所以"生成 → 真跑"之间**必须**有人给出地址；而文档 §1.1 通常就写着（开发/测试/生产）。
#   本函数把它捞出来，好让 CLI 的提示能具体到"**你想跑哪个环境**"。
BASE_URL_SECTION_RE = re.compile(r"服务地址|基础\s*url|服务端地址|base\s*url", re.IGNORECASE)
URL_IN_TEXT_RE = re.compile(r"https?://[^\s`|)\"'，。]+")
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")


def documented_base_urls(doc_text: Text) -> List[Tuple[Text, Text]]:
    """文档里写明的**服务地址候选**：`[(环境, url)]`（按文档顺序，按 url 去重）。"""
    out: List[Tuple[Text, Text]] = []
    seen = set()
    for span in heading_spans(doc_text):
        if not BASE_URL_SECTION_RE.search(span.title):
            continue
        for line in span.text.split("\n"):
            row = _TABLE_ROW_RE.match(line)
            if row:
                cells = [cell.strip().strip("`") for cell in row.group(1).split("|")]
                urls = [item for cell in cells for item in URL_IN_TEXT_RE.findall(cell)]
                if not urls:
                    continue
                label = next((cell for cell in cells if cell and not URL_IN_TEXT_RE.search(cell)), "")
                for url in urls:
                    if url not in seen:
                        seen.add(url)
                        out.append((label or "(未命名)", url))
                continue
            for url in URL_IN_TEXT_RE.findall(line):
                if url not in seen:
                    seen.add(url)
                    out.append(("(未命名)", url))
    return out


def build_user_prompt(
    slice_text: Text, title: Text = "", feedback: Optional[Tuple[Text, Text]] = None
) -> Text:
    """user prompt（+ 上一轮的回喂，若在修正环里）。"""
    prompt = USER_TEMPLATE.format(title=title or "(未命名)", quote=QUOTE_FIELD, body=slice_text)
    if feedback:
        kind, text = feedback
        prompt += (
            f"\n\n[上一轮被拦下：{kind}]\n{text}\n\n"
            "请**只重发修正后的完整 JSON**（不要解释你改了什么）。"
        )
    return prompt


def build_request(
    system: Text,
    user: Text,
    *,
    config: LLMConfig,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    response_format: Optional[Text] = None,
    seed: int = 0,
) -> LLMRequest:
    """组装一次请求；**输入预算在最终 payload 上校验**（§5.1：分段不超、合起来超也要报）。

    超预算 → `InputBudgetExceeded` 直接抛（**不重试**）——那是切片/文档的问题，
    重试只会重复花钱。
    """
    build_prompt_payload(system, user=user)  # ← 校验点（只用其副作用；prompt 以 messages 发出）
    return LLMRequest(
        system=system,
        user=user,
        model=config.model,
        base_url=config.base_url,
        prompt_version=PROMPT_VERSION,
        temperature=0.0,
        seed=seed,
        max_tokens=max_tokens,
        response_format=response_format,
    )


def _title_segment(title_path: Text) -> Text:
    """标题路径的**末段**（`# 登录 > ## POST /api/login` → `POST /api/login`），去 `#` 前缀。"""
    segment = (title_path or "").split(" > ")[-1].strip()
    return segment.lstrip("#").strip() or "case"


def _safe_case_name(raw: Text) -> Text:
    """用例名 → **可落盘**的模块段（§5.1："写盘前用 `normalize_module_segment` 预算模块名"）。"""
    from interfacetester.make import normalize_module_segment  # noqa: PLC0415

    return normalize_module_segment(raw or "") or "case"


def _gate_feedback(assembly: Any) -> Text:
    """闸门拒绝时回喂给模型的话（编号 + 逐条发现；**读 `.ai/failed/` 里那份人话**）。

    为什么读文件而不是自己拼：`write_needs_human()` 已经把它整理成"人接着要干什么"，
    自己再拼一份就是**同一种信息的第二个来源**（本仓吃过这类亏）。
    """
    parts = [f"写盘前置闸门拒绝（编号：{', '.join(assembly.gate_codes) or '无'}）"]
    path = getattr(assembly, "needs_human_path", "")
    if path and path.startswith(".ai/failed/") and not path.startswith(".."):
        try:
            with open(path, encoding="utf-8") as fp:
                parts.append(fp.read())
        except OSError:
            parts.append("（未能读取 NEEDS_HUMAN 详情）")
    return truncate_feedback("\n".join(parts))


# ---------------------------------------------------------------------------
# 生成（一片 → 一个用例；含修正环）
# ---------------------------------------------------------------------------

def generate_case(
    slice_: Slice,
    doc_text: Text,
    *,
    transport: Any,
    config: LLMConfig,
    cache: Any = None,
    audit: Any = None,
    case_name: Text = "",
    base_url: Text = "",
    max_rounds: int = MAX_ROUNDS,
    merge: bool = True,
    owner_ranges: Optional[Sequence[Tuple[int, int]]] = None,
) -> GenResult:
    """一片文档 → 一个用例（**修正环**：1 次首发 + 至多 `max_rounds` 次修正）。

    每一轮都把"上一轮的拦下原因"回喂给模型（**按类别选模板**），而不是重发同样的 prompt
    ——后者只会得到同样的错误（本仓对重试的一贯口径：**重试必须改变输入**）。
    """
    from interfacetester_ai.assembler import Unknown, assemble  # noqa: PLC0415
    from interfacetester_ai.assertions import derive_assertions  # noqa: PLC0415
    from interfacetester_ai.merge import merge_derived_assertions, merge_enabled  # noqa: PLC0415

    system = render_system_prompt("gen")
    name = _safe_case_name(case_name or _title_segment(slice_.title_path))
    result = GenResult(case_name=name, slice_title=slice_.title_path)
    feedback: Optional[Tuple[Text, Text]] = None

    # ★§9.33（登记项 3）：模型通路同样要把「**文档级**降级说明」带出去（此前两条通路都丢）。
    #   放在**环外**只算一次（逐轮重算纯属浪费），且与确定性通路**同一份判据**。
    doc_extras = _doc_degradation_unknowns(doc_degradation_notes(doc_text))

    for index in range(1, max_rounds + 2):
        user = build_user_prompt(slice_.text, slice_.title_path, feedback)
        request = build_request(system, user, config=config)  # ← 预算超限在这里直接抛
        response = complete(request, transport=transport, config=config, cache=cache, audit=audit)

        try:
            draft = parse_draft(response.json())
            # ★用例名以**片标题**为准：模型自报的名字不可复现
            # （同一份文档两次生成得到不同名 = 落盘撞车或产物对不上）
            draft = replace(draft, case_name=name)
        except (LLMResponseFormatError, DraftValidationError) as error:
            kind, text = classify_error(error)
            feedback = (kind, text)
            result.rounds.append(Round(index, kind, text))
            continue

        # ★模型说"本节没有接口"（2026-09-25 新增）：`steps: []` 是**合法且正确**的答案。
        # 本仓的 `--assertions-only` 就是这么表达"没有可确定的东西"的（`to_draft_payload`
        # 明写"没有任何断言时写 `validate: []` 是合法的"）。
        #
        # 改动前的处置是**送进 `assemble`**：零 step 的产物在 L2 出口校验抛 `ParamsError`
        # （内核原话："零步骤但判成功… 绿了但什么都没测"）→ 归类成 `l2-emit` → **回喂模型
        # 重试 2 轮**（重试不会变好：它已经答对了）→ 报告里还和真失败混在一起。
        # 现在的处置：**记为跳过（不是失败）**，不回喂、不重试、不落盘。
        if not draft.steps:
            result.skipped = True
            result.rounds.append(
                Round(index, "skip-model", "模型判断本节没有可测的接口（steps 为空）")
            )
            return result

        # ★C-2（2026-09-26 立项、已接线）：**装配前**把确定性映射的字段级断言并进草稿。
        # 口径：**只补不覆盖**（模型已断言的字段保留模型的，冲突进 `unknowns`）、
        # **多步用例不合并**、**按 `line_no ∈ 片行号范围` 绑定**（片外的可见记下）。
        # 两条回滚：`--no-merge-assertions`（`merge=False`）/ `INTERFACETESTER_AI_MERGE_ASSERTIONS=off`。
        # 为什么必须在装配**之前**：装配器要按 T28 对账这些断言——先合再装，才算"走同一套下游"。
        #
        # ★接线时实测发现的第四条口径（2026-09-26）：**合并只"补全"、不"救命"**。
        # 实测：陷阱样本（模型给 `validate: []` 的零断言草稿）在合并后**不再被 S3 拦**——
        # 因为确定性映射把字段级断言填了进去，草稿就不再是"真空"了。可 S3 在这里的角色是
        # **"模型没干活"的质量信号**，被工具悄悄抹掉，等于把 R14 那种幻觉换了个马甲。
        # 所以：**模型零断言 → 不合并**（照旧被 S3 拦、照旧留 NEEDS_HUMAN），但把"确定性映射
        # 其实另有 N 条可用"写成可见 note —— 该走 `--assertions-only` 或人工，不是让工具替它过关。
        extra_unknowns: List[Any] = list(doc_extras)
        if merge_enabled(cli_flag=merge):
            if _model_has_usable_assertion(draft, doc_text):
                merged = merge_derived_assertions(
                    draft,
                    doc_text,
                    slice_start=slice_.start_line,
                    slice_end=slice_.end_line,
                    owner_ranges=owner_ranges,
                )
                draft = merged.draft
                extra_unknowns = [
                    Unknown(kind="merge-note", where="merge", message=note)
                    for note in merged.notes()
                ]
            else:
                derivable = len(derive_assertions(doc_text)[0])
                if derivable:
                    extra_unknowns = [
                        Unknown(
                            kind="merge-skip",
                            where="merge",
                            message=(
                                f"模型的草稿**零断言**——合并**不救它**（这是「模型没干活」的质量信号，"
                                f"工具替它补上就等于把 R14 换个马甲）；确定性映射在本文档另可给出 "
                                f"{derivable} 条字段级断言，请走 `haify gen --assertions-only` 或交人工"
                            ),
                        )
                    ]

        try:
            assembly = assemble(
                draft,
                doc_text,
                base_url=base_url,
                extra_unknowns=extra_unknowns,
                # ★WP2.2：证据绑定的边界 = **本片**（模型只看到这一片，它的断言证据
                #   必须出自这一片；片外的**接口段**不许当证据，片外的**共享节**允许）。
                scope=(slice_.start_line, slice_.end_line),
            )
        except Exception as error:  # noqa: BLE001 - 出口校验（加载 / hmake 渲染 / 算子白名单）
            kind, text = classify_error(error)
            feedback = (kind, text)
            result.rounds.append(Round(index, kind, text))
            continue

        result.draft = draft
        result.assembly = assembly
        if assembly.ok():
            result.ok = True
            # ★2026-09-26（§九 第一档②）：装配成功 = **草稿已落**，不是"用例可用"。
            #   文案里必须说清"待人工确认后转正"，否则退出码 0 又会被读成"可以直接进 CI"。
            result.rounds.append(
                Round(
                    index,
                    "ok",
                    f"草稿已落 {assembly.draft_yaml_path}"
                    f"（质量状态 {assembly.quality.status}；转正需 `haify review --approve`）",
                )
            )
            return result

        # 闸门 REJECT（S 系列）：产物没进 cases/，`.ai/failed/` 里已有人话版原因
        kind = "gate"
        text = _gate_feedback(assembly)
        feedback = (kind, text)
        result.rounds.append(Round(index, kind, text))

    return result


def _model_has_usable_assertion(draft: CaseDraft, doc_text: Text) -> bool:
    """模型草稿里有没有**至少一条过得了 T28** 的断言（判"合并该不该动手"）。

    ★为什么用这三个函数而不是自己写一套：`quote_hits` / `check_field_in_quote` /
    `check_expect_in_quote` 就是**装配器自己**用的那套（T28 ①/② 与溯源），
    在 `gen` 里另写一份判据 = 立第二个"什么算可用断言"的口径，本仓吃过这种亏。
    """
    from interfacetester_ai.assembler import (  # noqa: PLC0415
        check_expect_in_quote,
        check_field_in_quote,
        quote_hits,
    )

    for step in draft.steps:
        for item in step.validate:
            if not quote_hits(item.source_quote, doc_text):
                continue
            if check_field_in_quote(item, item.source_quote, "merge-probe") is not None:
                continue
            if check_expect_in_quote(item, item.source_quote, "merge-probe") is not None:
                continue
            return True
    return False


def model_input_slices(doc_text: Text) -> List[Slice]:
    """**送给模型的输入单位**（2026-09-26 修订）：**接口段**（含子标题），不再只是扁平片。

    起因（实测）：`slicer` 的片是**扁平**的——每个标题一片、**不含子标题**。文档若把字段表
    写在 `### 请求体` 这类**子标题**下，父片的正文里就**没有那张表** → 模型看不到字段、
    只能猜（这是"引用大面积写歪"的一种成因，另一处实测见 `probe_p2_prep/out_quote_loss.txt`）。
    确定性通路早在 §9.6 就改成了**按标题层级算跨度**；本函数让模型通路**与它同单位**，
    于是两条通路的"一片"含义一致，`scope`（证据绑定边界）也自然对齐。

    口径（三条，缺一不可）：

    1. `interface_sections()` 的每个**接口段**（含全部子标题、去重后**最具体**的那一段）
       在**不超预算**时作为一个输入单位——超预算的段**退回**扁平片（让切片器按空行再切，
       不静默截断，§5.1）；
    2. **未被任何接口段覆盖**的扁平片**照旧保留** —— 这样"能写用例的地方一个都不少"
       （文档开头、或无标题段落里写了端点的情况），也让"本节没有接口 → 跳过"的既有判据
       继续作用在它们身上；
    3. 与接口段**重叠**的扁平片**不再单独送**（它的内容已经在段里，重复送=重复烧钱且产出重复用例）。
    """
    lines = (doc_text or "").split("\n")
    limit = effective_budget()
    sections = interface_sections(doc_text)

    def _text_of(start: int, end: int) -> Text:
        return "\n".join(lines[start - 1 : end]).rstrip("\n")

    out: List[Slice] = []
    covered: List[Tuple[int, int]] = []
    for section in sections:
        text = _text_of(section.start_line, section.end_line)
        if len(text.encode("utf-8")) > limit:
            continue  # 太长 → 交给下面的扁平片（切片器会按空行切细）
        covered.append((section.start_line, section.end_line))
        out.append(
            Slice(
                index=len(out) + 1,
                title_path=section.title_path,
                text=text,
                start_line=section.start_line,
                end_line=section.end_line,
                byte_size=len(text.encode("utf-8")),
            )
        )

    for item in split_document(doc_text):
        # 与任何接口段**重叠** → 它的内容已经在段里（段含子标题），不再单独送
        if any(item.start_line <= stop and start <= item.end_line for start, stop in covered):
            continue
        out.append(item)
    # ★**按文档顺序**输出：结果列表的顺序要与文档一致（报告、CLI 汇总、判据都依赖它）
    out.sort(key=lambda item: (item.start_line, item.end_line))
    return [replace(item, index=position + 1) for position, item in enumerate(out)]


def _write_doc_defects_report(doc_text: Text) -> Text:
    """§9.17 **文档缺陷回流**：把"响应数据表 vs 它自己的成功响应示例"的冲突写成回流清单。

    - 判据来自 `assertions.find_doc_defects()`（与"不产那条必然失败的 schema"**同一个判据**）；
    - 写盘走**已登记的写入者** `diagnosis.write_doc_defects()`（红线③：本模块**没有**写盘调用）；
    - **无缺陷 → 清掉陈旧清单**（陈旧留痕会与现状自相矛盾）；返回落盘路径或 `""`。
    """
    from interfacetester_ai.assertions import find_doc_defects  # noqa: PLC0415
    from interfacetester_ai.diagnosis import (  # noqa: PLC0415
        CAUSE_DOC_DEFECT,
        Diagnosis,
        clear_stale_doc_defects,
        write_doc_defects,
    )

    defects = find_doc_defects(doc_text)
    if not defects:
        if clear_stale_doc_defects():
            print("[haify gen] 文档缺陷回流清单已清掉（本轮没有形状冲突）")
        return ""
    items = [
        Diagnosis(
            case=item.where,
            step="响应数据表 vs 成功响应示例",
            cause_type=CAUSE_DOC_DEFECT,
            reason=item.message,
            action=item.action,
            confidence=1.0,
            evidence_quotes=[
                {"source": "doc", "text": item.table_quote},
                {"source": "doc", "text": item.example_quote},
            ],
        )
        for item in defects
    ]
    written = write_doc_defects(items) or ""
    if written:
        print(
            f"[haify gen] {len(items)} 条**文档自相矛盾**已回流 → {written}"
            "（响应数据表与它自己的「成功响应」示例冲突；对应断言**没有**进用例）"
        )
    return written


def generate_cases(
    doc_text: Text,
    *,
    transport: Any,
    config: LLMConfig,
    cache: Any = None,
    audit: Any = None,
    doc_check: bool = True,
    case_name_prefix: Text = "",
    max_rounds: int = MAX_ROUNDS,
    base_url: Text = "",
    skip_non_interface: bool = True,
    merge: bool = True,
) -> List[GenResult]:
    """整篇文档 → **每片一个用例**（§5.1：切片是 LLM 的输入单位）。

    生成前先过**文档体检闸**（§8.1 S-10 / T27）：不合格就**拒生成**并把缺项清单交回
    文档作者（`reports/NEEDS_DOC_FIX.md`）——先改文档再生成，别拿不合格的文档去烧模型。
    """
    _write_doc_defects_report(doc_text)  # ★§9.17 文档缺陷回流（同一份清单，两种通路共用）
    if doc_check:
        report: DocReport = audit_text(doc_text)
        if not report.ok:
            path = write_needs_doc_fix(report)  # 委托给登记写入者（reports/）
            raise GenerationFailed(
                f"文档体检未通过（{len(report.rejects)} 项必改；清单："
                f"{path or 'reports/NEEDS_DOC_FIX.md'}）——先改文档，重试生成没有意义"
            )

    # ★内容闸（§六）：**先脱敏、再切片**。为什么顺序不能反——
    # 切片是"送给模型的文本"，里面若还留着凭据，脱敏就等于没做；
    # 而且溯源校验拿的是**同一份**（脱敏后）文档，两侧一致才不会把合规产物误判成"引用不命中"。
    redacted_doc, doc_redactions = redact_sensitive(doc_text)
    # ★2026-09-26：输入单位 = **接口段**（含子标题），见 `model_input_slices`。
    #   "本节没有接口 → 跳过"的判据仍然作用在**未被段覆盖**的片上（行为不变）。
    slices = model_input_slices(redacted_doc)
    if not slices:
        raise GenerationFailed("文档切不出任何内容（空文档或只有空白）")

    results: List[GenResult] = []
    for item in slices:
        segment = _title_segment(item.title_path)
        raw = f"{case_name_prefix}_{segment}" if case_name_prefix else segment
        # ★调用**前**的跳过（2026-09-25 新增）：形态判据说这片里没有端点，就别花 3 轮
        #   调用让模型去编一个出来。理由、代价对比与实测数据见 `SLICE_ENDPOINT_RE` 上方。
        if skip_non_interface and not slice_looks_like_interface(item.text):
            result = GenResult(case_name=raw, slice_title=item.title_path, skipped=True)
            result.rounds.append(
                Round(
                    1,
                    "skip-static",
                    "本节没有 `METHOD /path` 形态的端点（标题 / 概述 / 字段表 / 附录这类"
                    "章节不描述具体接口），已跳过；用 `--no-skip-non-interface` 可强制送模型",
                )
            )
            result.redactions = list(doc_redactions)
            results.append(result)
            continue
        try:
            # ★归属范围（2026-09-26 修正）：本接口段**及其名下所有子节**。
            # 为什么不能只用 `item` 自己的行号范围：golden 写法的字段表在 `## POST /x` 之后的
            # `### 请求体` 里，而那个子节被判为"非接口片"跳过 → 只按片范围绑必然绑不上
            # （实测：15 篇 129 条全"未合并"、落盘产物仍是清一色 status_code）。
            owner_ranges = [
                (other.start_line, other.end_line)
                for other in slices
                if other.title_path == item.title_path
                or other.title_path.startswith(item.title_path + " > ")
            ]
            result = generate_case(
                item,
                doc_text,
                transport=transport,
                config=config,
                cache=cache,
                audit=audit,
                case_name=raw,
                base_url=base_url,
                max_rounds=max_rounds,
                merge=merge,
                owner_ranges=owner_ranges,
            )
        except InputBudgetExceeded as error:
            # ★单片超预算**不许中断整篇**（2026-09-25 实测纠正）：
            #   原先把 `InputBudgetExceeded` 直接抛穿整个 `generate_cases`，于是
            #   **一个长切片会让其它所有片都跑不到** —— 这与本模块"单片失败不拖累
            #   其它片"的立论**直接冲突**。实测：给 system 补上输出契约（+1541 字节）
            #   后，一个贴着预算边的切片让整篇 gen 在第 31 次调用处中断、0 条产出。
            #   ★仍然**不重试**（重试不会变好，只会重复花钱）；只是**不牵连别人**。
            result = GenResult(case_name=raw, slice_title=item.title_path)
            result.rounds.append(Round(1, "budget", str(error)))
        result.redactions = list(doc_redactions)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# `--assertions-only`：**只做确定性映射**（§5.2"确定性映射先做（不调模型）"）
# ---------------------------------------------------------------------------

# ★端点形态的**两种写法**（2026-09-26 补，§9.6 实测；旧名 `ENDPOINT_RE` 保留为兼容别名：
#   它的语义是「行首形态」那一支，`__all__` 里还有它，但新代码请用 `endpoint_matches`）：
#
#   - 行首形态：`## POST /api/order`、`- GET /api/x`（golden 与标题式文档）
#   - **表格形态**：`| **URL** | `POST /api/directory/create` |`（**真实客户文档**）
#
#   为什么必须补表格形态：真实文档 17 个端点里 16 个写在表格里，而原来只认行首 →
#   17 个端点**一个都认不出**，`--assertions-only` 取到的"第一个端点"是文档里唯一那句
#   散文行（认证接口），再把**全篇**字段表挂上去 → 产出一条"打认证接口、带着别的接口字段"
#   的用例，**还能过全部闸门**（`static_valid`）。模型通路的 `SLICE_ENDPOINT_RE` 本来就宽松。
ENDPOINT_TABLE_RE = re.compile(
    r"\|\s*\*{0,2}\s*URL\s*\*{0,2}\s*\|\s*[`*]*\s*"
    r"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b[\s:`|*]*?"
    r"(/[^\s`|)\"']+|https?://[^\s`|)\"']+)",
    re.IGNORECASE,
)
ENDPOINT_LINE_RE = re.compile(
    r"^\s*[-*#>\s]*\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b\s+(/[^\s`|)\"']+|\S+)",
    re.IGNORECASE | re.MULTILINE,
)


def endpoint_matches(text: Text) -> List[Tuple[Text, Text]]:
    """文档里所有 `(METHOD, url)`，**表格形态优先**。

    ★为什么**表格优先**（实测）：`### 3.5 文件重命名` 段里有一句交叉引用散文
    `…文件重命名接口（`POST /api/metadata/rename`）见 [3.5 …]`；若只按"文档顺序取第一个"，
    某些段会先命中那句散文。表格行是**该段自己的端点声明**，优先级更高。
    """
    found: List[Tuple[Text, Text]] = []
    for pattern in (ENDPOINT_TABLE_RE, ENDPOINT_LINE_RE):
        for hit in pattern.finditer(text or ""):
            found.append((hit.group(1).upper(), hit.group(2).strip("`* ")))
    return found


@dataclass(frozen=True)
class InterfaceSection:
    """文档里的**一个接口段**（`--assertions-only` 逐片产出的单位）。

    ★为什么必须分段（§9.6 实测）：真实文档 17 个端点里 16 个写在表格里，
    而"整篇取第一个端点 + 全篇推导断言"会产出一条**打认证接口、却带着别人字段**的用例。
    分段是"归属"的前提：**每段只用自己的端点与自己的字段**。
    """

    method: Text
    url: Text
    title_path: Text
    start_line: int
    end_line: int


def interface_sections(doc_text: Text) -> List[InterfaceSection]:
    """按标题层级取**接口段**：从端点所在的标题行，到"**下一个同级或更高级标题**"之前。

    ★为什么不直接用 `split_document()` 的片（实测踩到）：那些片是**扁平**的
    （每个标题一片、超预算再按空行切），**不含子标题**——而字段表恰恰常挂在
    `### 请求体` 这种子标题下 → 按片取会把字段表切掉，结果是"每个接口 0 条断言"。
    所以这里**按层级算跨度**（段 = 标题 + 它的全部子标题内容）。

    ★去重：**只保留最具体的那一段**。父标题（如 `# 订单接口`、`## 2. 目录服务接口`）的跨度
    包含子段，按"跨度包含其它段"判定丢弃——否则同一份文档会产出**重复用例**（父段一条 + 子段一条）。

    取不到端点的段不要（那些是非接口段：概述 / 错误码表 / 附录…）。
    """
    candidates: List[InterfaceSection] = []
    for span in heading_spans(doc_text):
        method, url = extract_endpoint(span.text)
        if not url:
            continue
        candidates.append(
            InterfaceSection(
                method=method,
                url=url,
                title_path=span.title,
                start_line=span.start_line,
                end_line=span.end_line,
            )
        )

    spans = [(item.start_line, item.end_line) for item in candidates]
    return [
        item
        for item in candidates
        if not any(
            item.start_line <= start
            and stop <= item.end_line
            and (start, stop) != (item.start_line, item.end_line)
            for start, stop in spans
        )
    ]


def _in_section(item: Any, section: InterfaceSection) -> bool:
    """断言的**引用句行号**是否落在该接口段内（★归属判据的**唯一口径**）。

    ★`line_no == 0`（没有行号）一律算**段外**：`0` 不落在任何段里，
    把它当段内 = **替它编一个归属**（本仓最反对的形态）。
    """
    line = getattr(item, "line_no", 0) or 0
    return bool(line) and section.start_line <= line <= section.end_line



ENDPOINT_RE = re.compile(
    r"^\s*[-*#>\s]*\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b\s+(\S+)",
    re.IGNORECASE | re.MULTILINE,
)


def extract_endpoint(doc_text: Text) -> Tuple[Text, Text]:
    """从文档里取第一个 `METHOD /path`（用例的两个必要要素，§5.1）。

    取不到就返回 `("", "")`——**不猜**（猜出来的 url 会让整条断言指到不存在的路径上）。
    """
    hits = endpoint_matches(doc_text)
    return hits[0] if hits else ("", "")


# ---------------------------------------------------------------------------
# ★§9.33（登记项 3）：**文档级降级说明**必须可见
# ---------------------------------------------------------------------------
# 背景（实测暴露的可见性缺口）：`derive_assertions()` 的第二个返回值是"我们**没有**断言
# 什么"的逐条说明（请求侧字段、判不出的类型、失败响应示例……），可达 10~15 条；
# 但**两条通路都把 `_skipped` 丢掉了**——产物上完全看不出"文档里声明了、工具却没断"的东西。
# 这是本仓最忌讳的形态：不是响亮报错，而是**静默**。
#
# ★为什么是"文档级"而不是"逐段"：`skipped` 的契约是 `List[str]`（**没有行号**），
#   无法归属到某个接口段——**猜它属于哪一段比不标更坏**（§9.32 的教训同款）。
#   所以：作为**一条**聚合说明随**每段**草稿落地，并在消息里写明"对所有接口段都适用"。
#   要按段归属，得先给 `skipped` 的契约加行号——独立一件事（登记）。
DOC_DEGRADATION = "doc-degradation"


def doc_degradation_notes(doc_text: Text, *, query_scopes: Sequence[Any] = ()) -> List[Text]:
    """本文档的**降级说明**（"声明了但判不出可断言事实"），**去重保序**。

    ★**唯一来源**：`derive_assertions()` 的第二个返回值——判据侧只维护那一份，
      这里不重写词表（CLI 的终端摘要与 `unknowns` 都用它，避免两处口径漂移）。
    """
    from interfacetester_ai.assertions import derive_assertions  # noqa: PLC0415

    _assertions, notes = derive_assertions(doc_text, query_scopes=query_scopes)
    return [
        text
        for text in dict.fromkeys((note or "").strip() for note in notes)
        if text
    ]


def _doc_degradation_unknowns(notes: Sequence[Text]) -> List[Any]:
    """把降级说明翻成**一条** `Unknown`（`kind=DOC_DEGRADATION`，`level=degrade`）。"""
    from interfacetester_ai.assembler import Unknown  # noqa: PLC0415

    unique = [text for text in dict.fromkeys(note.strip() for note in notes) if text]
    if not unique:
        return []
    body = "\n".join(f"    {index}) {note}" for index, note in enumerate(unique, 1))
    return [
        Unknown(
            kind=DOC_DEGRADATION,
            where="（文档级）",
            message=(
                f"**文档级**降级说明：本文档有 {len(unique)} 条「**文档声明了、但判不出可断言的内容**」"
                f"的东西，工具**没有替文档拍板**（逐条如下）——\n{body}"
            ),
            hint=(
                "这些不是错误，是「判不了」。要么按《保障 interfacetester_ai 稳定生成 yaml 用例的"
                "接口文档示例.md》把文档补成可判（类型 / 固定值 / 取值 / 必填 / 成功响应结构），"
                "要么人工补断言。★本条对**所有接口段**都适用（每段草稿都会带一份，便于逐段审阅）；"
                "之所以是文档级而不是逐段：判据里**没有行号**，猜它属于哪一段比不标更坏"
            ),
        )
    ]


def generate_assertions_only_all(
    doc_text: Text, *, case_name: Text = "", base_url: Text = ""
) -> List[Any]:
    """`haify gen --assertions-only` 的**逐片**版本：**每个接口段一条草稿**。

    ★为什么不再"整篇一条"（§9.6 实测的烟枪级缺陷）：整篇通路只取**第一个**端点、却把
    **全篇**字段表挂上去 → 一份打认证接口的用例里塞满了创建目录/文件上传的字段断言，
    而且**能过全部闸门**（`static_valid`）。逐片之后：**每段只用自己的端点与自己的字段**。

    ★**归属闸门（①，2026-09-26）**：断言的**引用句行号必须落在本段内**；段外的**丢弃**并
    **写进 `unknowns`**（可见，不静默）——这是"静默错位 → 响亮可见"的那道安全网。

    其余口径不变：不调模型、可复现、走与模型产物**完全相同的下游**（`parse_draft` → 装配器 →
    T28 对账 → S 闸门 → 落点规则），所以闸门一条都不会绕过。
    """
    # ★§9.17：文档级缺陷（响应表 vs 成功响应示例）**回流**成清单——与"不产那条必败断言"同一判据
    _write_doc_defects_report(doc_text)
    from interfacetester_ai.assembler import Unknown, assemble  # noqa: PLC0415
    from interfacetester_ai.assertions import (  # noqa: PLC0415
        PUBLIC_SECTION_RE,
        derive_assertions,
        read_field_rows,
        request_payload_from_rows,
        to_draft_payload,
    )
    from interfacetester_ai.schema import parse_draft  # noqa: PLC0415

    sections = interface_sections(doc_text)
    # ★§9.16：**GET / HEAD 段没有请求体** → 这些段里的"请求体表"其实是**查询参数**：
    #   ① 不产 `body.*` 断言（否则实测产出必红的 `body.path`）；
    #   ② 值落进产物的 `params`（而不是 `json`）。
    query_scopes = [
        (item.start_line, item.end_line)
        for item in sections
        if (item.method or "").strip().upper() in ("GET", "HEAD")
    ]
    all_assertions, doc_level_notes = derive_assertions(doc_text, query_scopes=query_scopes)
    # ★§9.33（登记项 3）：**降级说明必须可见**——此前这里是 `_skipped`（直接丢掉），
    #   于是"文档声明了、工具却没断"的东西在产物上完全看不到（静默）。
    #   它是**文档级**的（判据里没有行号），故随**每段**草稿各带一份。
    doc_extras = _doc_degradation_unknowns(doc_level_notes)
    lines = (doc_text or "").split("\n")
    # ★§9.16：字段表只解析**一次**，逐段复用（逐段重解析是 O(段数 × 行数)，真文档上明显变慢）。
    field_rows = read_field_rows(doc_text, query_scopes=query_scopes)
    # ★「公共请求头」这类**公共节**的请求头，每个接口都要带（只继承请求头，见 `request_payload_from_rows`）
    public_scopes = [
        (span.start_line, span.end_line)
        for span in heading_spans(doc_text)
        if PUBLIC_SECTION_RE.search(span.title or "")
    ]
    results: List[Any] = []

    for section in sections:
        kept = [item for item in all_assertions if _in_section(item, section)]
        dropped = [item for item in all_assertions if not _in_section(item, section)]
        section_text = "\n".join(lines[section.start_line - 1:section.end_line])

        # ★§9.16：把**本段**请求字段表装进产物的 `request`（`headers`/`params`/`json`/`data`）。
        #   值一律有出处（示例值列 / 请求体示例 / `TODO_` 占位），取不到的**响亮点名**（notes）。
        request_payload, request_notes = request_payload_from_rows(
            field_rows,
            doc_text,
            start_line=section.start_line,
            end_line=section.end_line,
            public_scopes=public_scopes,
            method=section.method or "",
        )

        extra: List[Any] = list(doc_extras) + [
            Unknown(
                kind="request-value",
                where=section.url,
                message=note,
                hint=(
                    "请求值也**必须有出处**：文档给示例值最省事；否则请人工填好环境变量后再转正"
                    "（`${ENV(TODO_…)}` 在运行期会**响亮报错**）"
                ),
            )
            for note in request_notes
        ]
        if dropped:
            examples = "、".join(
                f"{item.check}（第 {item.line_no} 行）" for item in dropped[:3]
            )
            extra.append(
                Unknown(
                    kind="out-of-section",
                    where=section.url,
                    message=(
                        f"本接口段（第 {section.start_line}~{section.end_line} 行）之外，还有 "
                        f"{len(dropped)} 条断言的引用句**不属于本段**，已**丢弃**：{examples}"
                        + ("…" if len(dropped) > 3 else "")
                    ),
                    hint=(
                        "归属不确定的断言**不进本条用例**——请确认它们属于哪个接口，"
                        "或把文档拆成「一份文件一个接口」（§9.6 的归属闸门）"
                    ),
                )
            )

        name = _safe_case_name((case_name + "_" if case_name else "") + (section.url or "case"))
        payload = to_draft_payload(
            section_text,
            case_name=name,
            url=section.url,
            method=section.method or "GET",
            assertions=kept,
            request=request_payload,
        )
        draft = parse_draft(payload)
        # ★装配时传**本段文本**（不是整篇）——两个口径因此都对齐：
        #   ① 溯源校验收严到"引用句必须在本段内"（与归属闸门同一口径）；
        #   ② 覆盖矩阵的分母变成**本段的**文档字段数——否则"覆盖 12 个 / 文档声明 79 个"
        #      是"分子按段、分母按整篇"，覆盖率会被**严重低估**（B2 落地时实测发现）。
        results.append(
            assemble(
                draft,
                section_text,
                base_url=base_url,
                extra_unknowns=extra,
                scope=(section.start_line, section.end_line),
            )
        )
    return results


def generate_assertions_only(
    doc_text: Text, *, case_name: Text = "", base_url: Text = ""
) -> Any:
    """`haify gen --assertions-only`：文档 → **确定性断言** → 走与模型产物**相同的下游**。

    - **不调模型、不花钱、可复现**（同一份文档跑一百次得到逐字节相同的断言）；
    - 返回装配器的 `AssemblyResult`——所以闸门（S 系列）、T28 对账、`TODO_` 落点规则、
      `unknowns`/`pending` 落盘**一条都不会绕过**（"新增通路不新增链路"）。

    ★它**故意不做**的事（§5.2 的边界）：文档没写的东西照样不会出现——
    包括"这个接口还有没有别的错误码"这类问题，它不猜，只把"没断言什么"留给 `unknowns`。
    """
    from interfacetester_ai.assembler import assemble  # noqa: PLC0415
    from interfacetester_ai.assertions import to_draft_payload  # noqa: PLC0415
    from interfacetester_ai.schema import parse_draft  # noqa: PLC0415

    results = generate_assertions_only_all(doc_text, case_name=case_name, base_url=base_url)
    if results:
        # ★兼容口径（2026-09-26 起它取**第一个接口段**，不再是"整篇混装"）：
        #   逐片调用请看 `generate_assertions_only_all`（CLI 已改用它）。
        return results[0]

    # 文档里**一个接口段都没有** → 保持"响亮拒绝"的老行为：交给闸门去说
    # "零断言不算通过"（调用方依赖"产不出东西 = 拒绝"这个结论，不许在这里悄悄返回空）。
    method, url = extract_endpoint(doc_text)
    # 命名与 `gen` 保持一致：前缀 + 端点段（`--case-name-prefix p` → `p_api_order_create`）
    name = _safe_case_name((case_name + "_" if case_name else "") + (url or "case"))
    payload = to_draft_payload(doc_text, case_name=name, url=url or "/", method=method or "GET")
    draft = parse_draft(payload)
    return assemble(draft, doc_text, base_url=base_url)


def run_selftest(verbose: bool = False) -> int:
    """gen 自检（**纯逻辑、不写盘**）。返回 0=全绿；非 0=失败项数。
    NOTICE：`generate_*` 的**写盘段**（装配器落盘）不在自检里跑——那要真工作区，
    判据在 `tests/gen_pipeline_test.py`（临时目录 + FakeTransport 离线）。
    """
    failures: List[Text] = []

    # ① 回喂截断：短的别动，长的截断并**说明截了**
    short = "结构不合法：缺 source_quote"
    if truncate_feedback(short) != short:
        failures.append("[截断错] 短文本不该被改动")
    cut = truncate_feedback("x" * (FEEDBACK_LIMIT + 100))
    if len(cut) <= FEEDBACK_LIMIT or "已截断" not in cut:
        failures.append("[截断错] 超长文本应截断并注明（否则模型不知道自己没看全）")

    # ② 报错分类：三类必须**各不相同**（这就是"按类别选模板"的机器形态）
    kinds = {
        classify_error(LLMResponseFormatError("不是 JSON"))[0],
        classify_error(DraftValidationError(["steps[0]：缺必需字段 ['url']"]))[0],
        classify_error(RuntimeError("hmake 渲染失败"))[0],
    }
    if kinds != {"l1-format", "l1-schema", "l2-emit"}:
        failures.append(f"[分类错] 三类报错应分到三个类别，实为 {kinds}")

    # ③ 回喂文本要**带上原始报错**（不含细节的模板等于让模型猜）
    _, schema_text = classify_error(DraftValidationError(["steps[0]：缺必需字段 ['url']"]))
    if "steps[0]" not in schema_text:
        failures.append("[回喂缺细节] schema 报错没有原文带给模型")

    # ④ 标题末段与用例名归一
    if _title_segment("# 登录 > ## POST /api/login") != "POST /api/login":
        failures.append("[标题解析错] 末段没取对")
    if not _safe_case_name("下单 接口"):
        failures.append("[命名错] 用例名归一后不该为空")

    # ⑤ 修正轮的 prompt 必须带上一轮原因（否则"重试"就是重发同样的话）
    clean = build_user_prompt("正文", "标题")
    with_feedback = build_user_prompt("正文", "标题", ("l1-schema", "缺 source_quote"))
    if "[上一轮被拦下" not in with_feedback or "[上一轮被拦下" in clean:
        failures.append("[prompt 错] 修正轮必须带上上一轮被拦下的原因")

    # ⑥ 预算校验真的接在 build_request 上（超预算要抛，不许悄悄发出去）
    cfg = LLMConfig(base_url="http://127.0.0.1:1/v1", model="m")
    try:
        build_request("sys", "u" * 20000, config=cfg)
    except InputBudgetExceeded:
        pass
    else:
        failures.append("[预算漏检] 超预算的 payload 竟然组装成功了")
    if build_request("sys", "u", config=cfg).seed != 0:
        failures.append("[采样错] seed 应当显式带上（T5 的缓存键要用它）")

    print("=" * 66)
    if failures:
        print(f"gen 自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("gen 自检全部通过：")
    print("  修正环：报错分 4 类、回喂**带原始报错**、超长截断并注明（4KB，§5.1）")
    print("  预算  ：组装请求时校验最终 payload（超限抛错，**不重试**——重试不会变好）")
    print("  命名  ：用例名以**片标题**为准并过 `normalize_module_segment`（可复现、可落盘）")
    print("  边界  ：本模块**不写盘**（落盘全委托给登记写入者）")
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

    parser = argparse.ArgumentParser(description="生成流程编排（P0a）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "ENDPOINT_RE",
    "FEEDBACK_LIMIT",
    "MAX_ROUNDS",
    "SLICE_ENDPOINT_RE",
    "USER_TEMPLATE",
    "GenResult",
    "GenerationFailed",
    "Round",
    "build_request",
    "build_user_prompt",
    "classify_error",
    "extract_endpoint",
    "generate_assertions_only",
    "generate_case",
    "generate_cases",
    "run_selftest",
    "slice_looks_like_interface",
    "truncate_feedback",
]


if __name__ == "__main__":
    raise SystemExit(main())

