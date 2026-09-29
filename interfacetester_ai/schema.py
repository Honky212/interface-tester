# -*- coding: utf-8 -*-
r"""schema —— **CaseDraft 契约**（§5.1 的 L1；**P0a** 交付）。

## 为什么需要它

生成的唯一入口是 `CaseDraft`，**不是 YAML**：

    LLM 只吐 CaseDraft JSON（字段白名单 + 每条断言必须带 source_quote）
      → 纯代码装配（CaseDraft → IRCase/IRStep → emit_case）
      → L2 出口校验（validate_emitted_case）

三个直接好处：

1. **模型摸不到文件系统**（红线③）——它连 YAML 都吐不出来，只能吐一个**被 schema 卡住**的 JSON；
2. **"每条断言都有出处"升级为字段级要求**：`source_quote` 是**必填**，不是"最好有"；
3. **报错可定位**：结构错在解析期就被点名（哪个 step、哪条断言、哪个字段），
   不必等到 emit 或跑用例才发现。

## ★落地偏差（诚实登记，R14 纪律）

v8 §5.1 写的是「pydantic `extra="forbid"`」。本包**零三方依赖**
（`pyproject.toml` 的 `dependencies = []` 是 T6 立的纪律），所以这里**自己实现**严格校验：
`extra="forbid"` 的语义 = **字段白名单 + 多一个就报错**，用显式常量实现同一件事。
**偏差只在实现手段，判据一致**（多字段、缺字段、类型错、空值都报）。

## 纪律

- **不写盘**（装配器负责落盘；本模块只有纯函数）；
- **只做结构与类型校验**：`comparator` 合不合法由**闸门**负责（S6 复用内核
  `make.ensure_known_comparators`）——两处各管一段，避免"两个地方都判一半"；
- 错误消息必须点名**哪个 step / 哪条断言 / 哪个字段**（否则报错等于没报）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Text, Tuple

# 字段白名单：与 §5.1 的 CaseDraft 契约一一对应（`extra="forbid"` 的语义载体）
CASE_FIELDS = frozenset({"case_name", "steps"})
STEP_FIELDS = frozenset(
    {"name", "method", "url", "headers", "params", "json", "data", "upload", "extract", "validate"}
)
ASSERTION_FIELDS = frozenset({"comparator", "check", "expect", "source_quote"})

# 必需字段（缺即报错）
STEP_REQUIRED_FIELDS = ("name", "method", "url", "validate")
ASSERTION_REQUIRED_FIELDS = ("comparator", "check", "expect", "source_quote")

# 断言里**必须逐字来自文档**的那一个字段——它是"不臆造"的机器形态
QUOTE_FIELD = "source_quote"


def draft_contract_text() -> Text:
    """`CaseDraft` 的**紧凑输出契约**（给 prompt 用；**从字段白名单生成，禁止手抄**）。

    ★为什么必须有它（2026-09-25 实测纠正）：system prompt 一直写着"只输出符合
    **给定的** JSON Schema 的一个 JSON 对象"，**但那份 Schema 从来没给过**
    （`response_format` 也是 `None`）——模型只能猜。实测（Ollama `gemma4:latest`
    + `project-three` 的 8KB 接口文档）：15 片全部猜出别的形状
    （`{"case_name":…,"steps":[…]}` / `{"testcase":…}` / `{"type":"r…"}`），
    修正环又只告诉它"字段不认识"、**不告诉它正确形状**，3 轮都改不对 → **0/15 产出**。

    ★为什么是"紧凑文本"而不是真 JSON Schema：预算 8000 字节且**含 system**
    （`build_prompt_payload` 在最终 payload 上校验），完整 Schema 会把文档切片挤掉；
    而模型要的只是**形状**（键名 + 嵌套 + 必填），不是 `$defs`/`pattern` 那些。

    ★末句"没有完整接口时输出 `[]`"是实测加的：原先模型会为「服务地址表」「错误码表」
    「FAQ」这类**非接口片段**硬造接口（`"url": "placeholder"`）——那不是"模型太弱"，
    是**没告诉它"允许交白卷"**。
    """
    return (
        "## 输出形状（**严格**：多一个/少一个字段都会被拒；顶层**只能**有这两个键）\n"
        "{\n"
        '  "case_name": "<用例名>",\n'
        '  "steps": [\n'
        "    {\n"
        '      "name": "<步骤名>", "method": "POST", "url": "/api/xxx",\n'
        '      "headers": {"Content-Type": "application/json"},\n'
        '      "params": {}, "json": {}, "data": "", "upload": {}, "extract": {},\n'
        "      // 上面这 6 个（headers/params/json/data/upload/extract）都是**可选**的，\n"
        "      // 用到哪个写哪个，不要为了凑字段而写空对象\n"
        '      "validate": [\n'
        '        {"comparator": "equal", "check": "status_code", "expect": 200,\n'
        f'         "{QUOTE_FIELD}": "<本节原文里的原句，逐字>"}}\n'
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n"
        f"· 顶层键 ∈ {sorted(CASE_FIELDS)}\n"
        f"· step 必填 ∈ {list(STEP_REQUIRED_FIELDS)}；可选 ∈ "
        f"{sorted(STEP_FIELDS.difference(STEP_REQUIRED_FIELDS))}\n"
        f"· 每条 validate **只允许** ∈ {sorted(ASSERTION_FIELDS)}"
        f"（{QUOTE_FIELD} 必填；空字符串算缺）\n"
        "★本节**没有完整接口**时，把 `steps` 写成**空数组**"
        '（顶层仍是对象：`{"case_name": "…", "steps": []}`）——**这是合法的**，'
        "系统会记为「本节没有接口」并跳过，不会算你犯错。**不要**为「服务地址」"
        "「错误码表」「附录/FAQ」这类片段硬造接口，尤其**不要编占位 url**"
        "（`/api/xxx`、`/api/status`、`/dummy/...`）——编出来的端点会打到不存在的路径上。\n\n"
    )



class DraftValidationError(Exception):
    """`CaseDraft` 结构不合法。`problems` 是一串**可定位**的问题描述。"""

    def __init__(self, problems: Sequence[Text], where: Text = "CaseDraft") -> None:
        self.problems = list(problems)
        self.where = where
        detail = "\n".join(f"  - {item}" for item in self.problems)
        super().__init__(f"{where} 结构不合法（{len(self.problems)} 处）：\n{detail}")


@dataclass(frozen=True)
class DraftAssertion:
    """一条断言草案。

    `source_quote` **必填**：它是"这条断言不是凭空想的"唯一可检查凭据——
    装配器会拿它去与输入切片做**归一化后的逐字比对**，不命中就降级进 `unknowns`。
    """

    comparator: Text
    check: Text
    expect: Any
    source_quote: Text

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "comparator": self.comparator,
            "check": self.check,
            "expect": self.expect,
            QUOTE_FIELD: self.source_quote,
        }


@dataclass(frozen=True)
class DraftStep:
    """一个请求步骤草案（`extract` 可选，其余必需）。"""

    name: Text
    method: Text
    url: Text
    validate: Tuple[DraftAssertion, ...] = ()
    headers: Optional[Dict[Text, Any]] = None
    params: Optional[Dict[Text, Any]] = None
    json: Optional[Any] = None
    data: Optional[Text] = None
    # ★§9.20（2026-09-27）：**文件上传字段**（`multipart/form-data`）→ `request.upload`。
    #   内核侧一直支持（`IRStep.upload` → YAML `upload:`），缺的是**草案契约**这一环 ——
    #   于是"上传类接口"的产物只能把文件字段丢掉（还点了名）。值一律 `${ENV(TODO_…)}`：
    #   文件**路径**编不出来，必须人工/环境给（与"不猜"同一条纪律）。
    upload: Optional[Dict[Text, Text]] = None
    extract: Optional[Dict[Text, Text]] = None

    def request_keys(self) -> List[Text]:
        """本步骤实际发出的请求字段（供"断言与请求形态是否匹配"之类的收口判断）。"""
        keys = ["method", "url"]
        for name in ("headers", "params", "json", "data", "upload"):
            if getattr(self, name) is not None:
                keys.append(name)
        return keys


@dataclass(frozen=True)
class CaseDraft:
    """一份用例草案：`case_name` + `steps`。**装配器的唯一输入。**"""

    case_name: Text
    steps: Tuple[DraftStep, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "case_name": self.case_name,
            "steps": [
                {
                    "name": step.name,
                    "method": step.method,
                    "url": step.url,
                    **{
                        name: getattr(step, name)
                        for name in ("headers", "params", "json", "data", "upload", "extract")
                        if getattr(step, name) is not None
                    },
                    "validate": [item.to_dict() for item in step.validate],
                }
                for step in self.steps
            ],
        }

    def assertions(self) -> List[DraftAssertion]:
        """展平所有断言（报告口径"断言数 vs 覆盖字段数"用它）。"""
        return [item for step in self.steps for item in step.validate]


def _unexpected_fields(payload: Mapping[Text, Any], allowed: frozenset) -> List[Text]:
    return sorted(name for name in payload if name not in allowed)


def _missing_fields(payload: Mapping[Text, Any], required: Sequence[Text]) -> List[Text]:
    return [name for name in required if name not in payload]


def _problems_for(problems: Sequence[Text], where: Text) -> bool:
    """`problems` 里是否已有属于 `where` 的问题（有就不再构造对象，避免级联噪声）。"""
    prefix = f"{where}："
    return any(item.startswith(prefix) for item in problems)


def _parse_assertion(
    raw: Any, where: Text, problems: List[Text]
) -> Optional[DraftAssertion]:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}：断言必须是对象，实为 {type(raw).__name__}")
        return None

    extra = _unexpected_fields(raw, ASSERTION_FIELDS)
    if extra:
        problems.append(
            f"{where}：出现未定义字段 {extra}（字段白名单：{sorted(ASSERTION_FIELDS)}）"
        )
    missing = _missing_fields(raw, ASSERTION_REQUIRED_FIELDS)
    if missing:
        problems.append(
            f"{where}：缺必需字段 {missing}（{QUOTE_FIELD} 是必填，不是可选项）"
        )

    quote = raw.get(QUOTE_FIELD)
    if QUOTE_FIELD in raw and (not isinstance(quote, str) or not quote.strip()):
        problems.append(f"{where}：{QUOTE_FIELD} 必须是非空字符串（空凭据 = 没有凭据）")

    for name in ("comparator", "check"):
        value = raw.get(name)
        if name in raw and (not isinstance(value, str) or not value.strip()):
            problems.append(f"{where}：{name} 必须是非空字符串")

    if _problems_for(problems, where):
        return None

    return DraftAssertion(
        comparator=str(raw["comparator"]),
        check=str(raw["check"]),
        expect=raw["expect"],
        source_quote=str(quote),
    )


def _parse_step(raw: Any, index: int, problems: List[Text]) -> Optional[DraftStep]:
    where = f"steps[{index}]"
    if not isinstance(raw, Mapping):
        problems.append(f"{where}：步骤必须是对象，实为 {type(raw).__name__}")
        return None

    extra = _unexpected_fields(raw, STEP_FIELDS)
    if extra:
        problems.append(
            f"{where}：出现未定义字段 {extra}（字段白名单：{sorted(STEP_FIELDS)}）"
        )
    missing = _missing_fields(raw, STEP_REQUIRED_FIELDS)
    if missing:
        problems.append(f"{where}：缺必需字段 {missing}")

    for name in ("name", "method", "url"):
        value = raw.get(name)
        if name in raw and (not isinstance(value, str) or not value.strip()):
            problems.append(f"{where}：{name} 必须是非空字符串")

    assertions: Tuple[DraftAssertion, ...] = ()
    if "validate" in raw:
        validate_raw = raw.get("validate")
        if not isinstance(validate_raw, list):
            problems.append(
                f"{where}：validate 必须是数组（空数组是合法写法：显式声明不做断言）"
            )
        else:
            parsed = [
                _parse_assertion(item, f"{where}.validate[{position}]", problems)
                for position, item in enumerate(validate_raw)
            ]
            assertions = tuple(item for item in parsed if item is not None)

    if _problems_for(problems, where):
        return None

    return DraftStep(
        name=str(raw["name"]),
        method=str(raw["method"]).upper(),
        url=str(raw["url"]),
        validate=assertions,
        headers=dict(raw["headers"]) if isinstance(raw.get("headers"), Mapping) else None,
        params=dict(raw["params"]) if isinstance(raw.get("params"), Mapping) else None,
        json=raw.get("json"),
        data=raw.get("data") if isinstance(raw.get("data"), str) else None,
        upload=dict(raw["upload"]) if isinstance(raw.get("upload"), Mapping) else None,
        extract=dict(raw["extract"]) if isinstance(raw.get("extract"), Mapping) else None,
    )


def parse_draft(payload: Any) -> CaseDraft:
    """把模型产物（已 `json.loads` 的 dict）解析成 `CaseDraft`；不合法 → `DraftValidationError`。

    **严格**是本模块的目的：多一个字段、少一个字段、类型不对、凭据为空，全都要报，
    而且要**一次报全**（而不是遇错就返回第一条）——模型靠这串问题去改，报得越全修得越快。
    """
    problems: List[Text] = []
    if not isinstance(payload, Mapping):
        raise DraftValidationError([f"顶层必须是对象，实为 {type(payload).__name__}"])

    extra = _unexpected_fields(payload, CASE_FIELDS)
    if extra:
        problems.append(f"顶层：出现未定义字段 {extra}（字段白名单：{sorted(CASE_FIELDS)}）")
    missing = _missing_fields(payload, ("case_name", "steps"))
    if missing:
        problems.append(f"顶层：缺必需字段 {missing}")

    name = payload.get("case_name")
    if "case_name" in payload and (not isinstance(name, str) or not name.strip()):
        problems.append("顶层：case_name 必须是非空字符串")

    steps: Tuple[DraftStep, ...] = ()
    # ★`steps: []` **是合法的**（2026-09-25 判据修正，原来在这里报"不能为空"）。
    #
    # 为什么改（实测因果链）：契约本来就给了"本节没有完整接口"的出口（见本模块
    # `draft_contract_text` 末段：输出空 `steps`，别为「服务地址」「错误码表」硬造接口），
    # 但**解析层把这个出口堵死了** —— 于是模型唯一能做的表达就是"编一个 step 出来"：
    #   `project-three` 实测里它对「服务地址」「公共请求头」「成功响应」「常见错误码」
    #   这些章节造了 `/api/status`、`/api/placeholder`、`/dummy/success`、
    #   `/api/file_service` ——**每一个都是编的**。把"诚实地说没有"判成"格式错误"，
    # 等于**奖励编造、惩罚诚实**。
    #
    # 为什么这样分层才对：`steps: []` 的**形状**是合法的（"零个 step"不是形状错）；
    # 内核拒的是"零 step 的**用例**"（`teststeps` 空列表 → 零步骤但判成功），那是
    # **运行/落盘语义**——由**落盘层**管。两道防线都还在：
    #   ① `gen.generate_case` 见到空 `steps` → 记 `skip-model`（跳过，不进装配）；
    #   ② 即便走进装配（如 `--assertions-only` 通路），内核 L2 出口校验照样硬拒。
    if "steps" in payload:
        steps_raw = payload.get("steps")
        if not isinstance(steps_raw, list):
            problems.append("顶层：steps 必须是数组")
        else:
            parsed = [
                _parse_step(item, index, problems) for index, item in enumerate(steps_raw)
            ]
            steps = tuple(item for item in parsed if item is not None)

    if problems:
        raise DraftValidationError(problems)

    return CaseDraft(case_name=str(name), steps=steps)


def run_selftest(verbose: bool = False) -> int:
    """跑契约自检（**纯逻辑、不写盘**）。返回 0=全绿；非 0=失败项数。

    NOTICE（为什么自检不写盘）：生产模块里只该留**它自己的**写盘点——
    本模块一个都没有（纯函数），所以自检也不能为了"造夹具"而写文件（T27 的教训）；
    需要真实产物/文件系统的判据在 `tests/case_draft_test.py`。
    """
    failures: List[Text] = []

    good = {
        "case_name": "登录",
        "steps": [
            {
                "name": "登录",
                "method": "post",
                "url": "/api/login",
                "json": {"username": "${ENV(USERNAME)}"},
                "extract": {"token": "body.data.token"},
                "validate": [
                    {
                        "comparator": "eq",
                        "check": "body.code",
                        "expect": 0,
                        "source_quote": '{"code": 0, "message": "ok"}',
                    }
                ],
            }
        ],
    }
    parsed = parse_draft(good)
    if parsed.steps[0].method != "POST":
        failures.append("[解析错] method 应归一为大写 POST")
    if parsed.to_dict()["steps"][0]["name"] != "登录":
        failures.append("[往返错] to_dict 丢字段")
    if len(parsed.assertions()) != 1:
        failures.append("[展平错] assertions() 应返回 1 条")

    bad_cases: List[Tuple[Text, Any, Text]] = [
        ("缺 case_name", {"steps": good["steps"]}, "缺必需字段"),
        ("多字段", {**good, "extra_key": 1}, "出现未定义字段"),
        # ★"空 steps" 从这份**必须拒**的清单里移走了（2026-09-25 判据修正）：
        #   它是模型**诚实地说"本节没有完整接口"**的唯一表达（契约末段明写允许），
        #   原先在这里被拒 → 模型收到"结构不合法"的回喂 → 只好**编一个端点出来**
        #   （实测：`/api/status`、`/api/placeholder`、`/dummy/success`、
        #   `/api/file_service` 全是编的）。"零 step 不能当用例跑"是**运行/落盘语义**
        #   （内核 L2 出口校验管），不是解析层该管的事 —— 它现在由下面那条
        #   **必须接受**的判据盯着（判据要成对，不能只在"必须拒"里删了事）。
        (
            "step 缺必需字段",
            {"case_name": "x", "steps": [{"name": "n", "method": "get", "url": "/a"}]},
            "缺必需字段",
        ),
        (
            "断言缺凭据",
            {
                "case_name": "x",
                "steps": [
                    {
                        "name": "n",
                        "method": "get",
                        "url": "/a",
                        "validate": [{"comparator": "eq", "check": "body.code", "expect": 0}],
                    }
                ],
            },
            QUOTE_FIELD,
        ),
        (
            "凭据为空串",
            {
                "case_name": "x",
                "steps": [
                    {
                        "name": "n",
                        "method": "get",
                        "url": "/a",
                        "validate": [
                            {
                                "comparator": "eq",
                                "check": "body.code",
                                "expect": 0,
                                QUOTE_FIELD: "   ",
                            }
                        ],
                    }
                ],
            },
            "非空字符串",
        ),
        (
            "断言多字段",
            {
                "case_name": "x",
                "steps": [
                    {
                        "name": "n",
                        "method": "get",
                        "url": "/a",
                        "validate": [
                            {
                                "comparator": "eq",
                                "check": "body.code",
                                "expect": 0,
                                QUOTE_FIELD: "x",
                                "confidence": 0.9,
                            }
                        ],
                    }
                ],
            },
            "未定义字段",
        ),
    ]
    for label, payload, fragment in bad_cases:
        try:
            parse_draft(payload)
        except DraftValidationError as error:
            if fragment not in str(error):
                failures.append(
                    f"[消息不含关键信息] {label}：期望提到 {fragment!r}，实为 {error}"
                )
            if verbose:
                print(f"  [拦下 ok] {label}")
        else:
            failures.append(f"[漏放] {label}：不合法的草案竟然被接受了")

    # 空白 validate 是**合法**写法（显式声明不做断言）——判据必须成对，不能只测"拦得住"
    empty_ok = {
        "case_name": "显式空",
        "steps": [{"name": "n", "method": "get", "url": "/a", "validate": []}],
    }
    try:
        if parse_draft(empty_ok).steps[0].validate:
            failures.append("[误伤] 空 validate 应解析为空元组")
    except DraftValidationError as error:
        failures.append(f"[误伤] 空 validate 是合法写法，却被拒：{error}")

    # ★"空 steps" 必须被**接受**（2026-09-25 判据修正的另一半）。
    #   为什么要有这半边：只把"必须拒"里的那条删掉，这条判据就变成**单向**的了 ——
    #   某天有人"顺手"把拒绝加回来，自检不会有任何提示，而模型的诚实出口就又没了
    #   （代价是它改去编端点）。它的**运行语义**（零 step 不能当用例跑）由落盘层负责，
    #   这里只钉住"**解析层不许拦它**"。
    try:
        if parse_draft({"case_name": "x", "steps": []}).steps != ():
            failures.append("[解析错] 空 steps 应解析为空元组")
    except DraftValidationError as error:
        failures.append(f"[误伤] 空 steps 是「本节没有接口」的合法表达，却被拒：{error}")

    print("=" * 66)
    if failures:
        print(f"CaseDraft 契约自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("CaseDraft 契约自检全部通过：")
    print("  严格解析：字段白名单（多一个即报）+ 必需字段 + 类型 + 空值（一次报全）")
    print(f"  凭据必填：每条断言必须带非空 {QUOTE_FIELD}（'不臆造'的机器形态）")
    print(f"  可定位  ：{len(bad_cases)} 类结构错全部被拦下，且消息点名到 step / 断言 / 字段")
    print("  不误伤  ：空 validate（显式声明不做断言）是合法写法，正常放行")
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

    parser = argparse.ArgumentParser(description="CaseDraft 契约（P0a）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "ASSERTION_FIELDS",
    "ASSERTION_REQUIRED_FIELDS",
    "CASE_FIELDS",
    "QUOTE_FIELD",
    "STEP_FIELDS",
    "STEP_REQUIRED_FIELDS",
    "CaseDraft",
    "DraftAssertion",
    "DraftStep",
    "DraftValidationError",
    "draft_contract_text",
    "parse_draft",
    "run_selftest",
]


if __name__ == "__main__":
    raise SystemExit(main())
