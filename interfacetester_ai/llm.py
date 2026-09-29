# -*- coding: utf-8 -*-
r"""llm —— **模型调用层**（§六；**P0a** C 阶段）。

## 边界纪律（红线③，§6 写盘边界）

本模块**一律不写盘**（`bench/write_boundary_scanner.py` 登记为 `("forbid", ())`）：
模型响应只以**内存结构**向上返回。缓存落盘是 `cache.py` 的事、审计落盘是 `manifest.py` 的
事——两者都以**端口**形式注入本模块（`cache.get/put`、`audit.record`），所以
"命中即回放"与"每次调用留痕"都不需要本模块碰文件系统。

## 协议与适配面（§六 的协议行）

OpenAI 兼容 `POST {base}/chat/completions`，`requests` 直连（**不引 LLM SDK**）。实测适配面：

| 部署 | base_url | key |
| --- | --- | --- |
| **Ollama** | `http://127.0.0.1:11434/v1` | **不需要**（本地回环，L3 默认放行，只需 `SANDBOX=on`） |
| LM Studio / llama.cpp / vLLM / Xinference | `http://127.0.0.1:<port>/v1` | 不需要 |
| DashScope 兼容模式（qwen）/ DeepSeek / Moonshot / 智谱 / 硅基流动 / OpenRouter | 各家 `/v1` | 需要 |
| OpenAI 官方 / Azure OpenAI | 官方 `https://api.openai.com/v1`；Azure 路径不同（`api-version`） | 需要 |

**★能力差异决定了本模块的形状**（不假设服务端）：

1. **约束解码**：`json_schema` 只有部分服务支持（Ollama 一般只到 `json_object`）→
   所以 `response_format` **默认不设**，靠"prompt + `schema.parse_draft` 严格校验 + 修正环"，
   需要时可显式打开（服务端不支持就退化，§六 的退化路径）；
2. **`seed`** 常被忽略 → 确定性**不靠服务端 seed**，靠**缓存键 + `FakeTransport` 回放**；
3. **`usage`** 常不返回（本地模型/内网网关）→ 预算计量**不依赖 usage**（§六 明写），
   usage 只用于**校准与显示**。

## 配置（**只从环境变量取**）

`INTERFACETESTER_AI_BASE_URL / _API_KEY / _MODEL / _TIMEOUT(60s) / _RETRIES(2) /
_MAX_OUTPUT_CASES(20) / _ENABLE(on)`；缺 `BASE_URL`/`MODEL` → 抛 `LLMConfigMissing`
（CLI 打印配置指引 + **退出码 2**，**绝不隐式出网**）。

**★实测纠正（2026-09-25）**：本节原先照抄方案 §6 的"**写进项目 `.env` 即可被
loader 带起**"——**那是错的**，而且错得很隐蔽。实测：`haify gen` 在
`cli.py` 里**先**调 `LLMConfig.from_env()`、**后**才进 `generate_cases`
（内核 `load_test_file` 才带起 `.env`）；顺序反了，所以项目 `.env` 里的
`INTERFACETESTER_AI_*` **永远不会**进到配置里。症状最坏的一种：用户照指引写好
`.env`、再跑还是"缺配置"，反复试之后把 key 塞进全局环境变量或命令行——
**凭据暴露面反而变大**。

**为什么不改成"支持 `.env`"**：那份 `.env` 是**用例**里 `${ENV(...)}` 的来源
（属于**被测项目**的配置）。把工具自己的凭据混进去，会让它被用例读到，也更容易
被提交进版本库；而本模块的内容闸（把 key 换 `${ENV(...)}`）整条立场的立论就是
"**凭据只在环境里、不落文件**"。所以修的是**文案**，不是功能。

**★落地细化（诚实登记）**：§六 写"缺 `BASE_URL`/`API_KEY` → 退出码 2"。实测该口径对
**本地回环**服务会**误伤**——Ollama / LM Studio / vLLM 本来就**没有** API key，
强制要求会逼用户填假 key，而假 key 恰恰让"缺配置"这个信号彻底失效。
所以细化为：**缺 `API_KEY` 时，若 `base_url` 是回环地址则放行，否则报错**
（判据在 `run_selftest` 与 `tests/llm_transport_test.py` 里成对给出）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Text, Tuple

from interfacetester_ai.l3 import is_loopback
from interfacetester_ai.prompts import PROMPT_VERSION

AI_BASE_URL_ENV = "INTERFACETESTER_AI_BASE_URL"
AI_API_KEY_ENV = "INTERFACETESTER_AI_API_KEY"
AI_MODEL_ENV = "INTERFACETESTER_AI_MODEL"
AI_TIMEOUT_ENV = "INTERFACETESTER_AI_TIMEOUT"
AI_RETRIES_ENV = "INTERFACETESTER_AI_RETRIES"
AI_MAX_OUTPUT_CASES_ENV = "INTERFACETESTER_AI_MAX_OUTPUT_CASES"
AI_ENABLE_ENV = "INTERFACETESTER_AI_ENABLE"

DEFAULT_TIMEOUT = 180
DEFAULT_RETRIES = 2
DEFAULT_MAX_OUTPUT_CASES = 20

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_CONFIG_HINT = (
    "配置指引（**只认环境变量**——本工具**不读** `.env` 里的 AI 配置）：\n"
    '  PowerShell：  $env:INTERFACETESTER_AI_BASE_URL = "http://127.0.0.1:11434/v1"\n'
    '                $env:INTERFACETESTER_AI_MODEL    = "qwen2.5:14b"\n'
    '                $env:INTERFACETESTER_AI_SANDBOX  = "on"   # L3 闸门：真发请求必须显式开启\n'
    "  cmd.exe：     set INTERFACETESTER_AI_BASE_URL=http://127.0.0.1:11434/v1\n"
    "  Linux/macOS： export INTERFACETESTER_AI_BASE_URL=http://127.0.0.1:11434/v1\n"
    f"  API key：     {AI_API_KEY_ENV} —— 回环地址可留空；云端必填\n"
    "\n"
    "★为什么不读项目 `.env`：那份 `.env` 是**用例**里 `${ENV(...)}` 的来源\n"
    "  （属于**被测项目**的配置）。把工具自己的凭据混进去，会让它被用例读到，也更容易\n"
    "  被提交进版本库——AI 配置是**本工具的运行期配置**，只从环境变量取。\n"
    "★即使 base_url 是回环（本地 Ollama / LM Studio / vLLM），也**必须** SANDBOX=on：\n"
    "  L3 区分的是「有没有人**显式**说要真发请求」，不是「地址是否安全」。\n"
    "未配置时本工具**不会隐式出网**——这是刻意的默认值。"
)


class LLMConfigMissing(Exception):
    """配置缺失/非法 → CLI 应打印指引并**退出码 2**（§六 的配置行）。"""

    def __init__(self, problems: Sequence[Text], hint: Text = "") -> None:
        self.problems = list(problems)
        self.hint = hint or _CONFIG_HINT
        detail = "\n".join(f"  - {item}" for item in self.problems)
        super().__init__(f"AI 调用配置不完整（{len(self.problems)} 处）：\n{detail}\n\n{self.hint}")


@dataclass(frozen=True)
class LLMConfig:
    """一次调用的**环境面**配置（与"这一次请求"分离，见 `LLMRequest`）。"""

    base_url: Text
    model: Text
    api_key: Text = ""
    timeout: int = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    max_output_cases: int = DEFAULT_MAX_OUTPUT_CASES
    enabled: bool = True

    @property
    def host(self) -> Text:
        """`base_url` 的主机名（判"是否本地"用；解析不了返回空串）。"""
        try:
            return urllib.parse.urlsplit(self.base_url or "").hostname or ""
        except ValueError:
            return ""

    def is_local(self) -> bool:
        """是否指向**回环**服务（Ollama / LM Studio / vLLM 这类"没有 key"的部署）。"""
        return is_loopback(self.host)

    def endpoint(self) -> Text:
        """补成 `{base}/chat/completions`（容忍尾斜杠，也容忍 base 已经带全路径）。"""
        base = (self.base_url or "").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def cache_fields(self) -> Dict[Text, Any]:
        """配置里**进缓存键**的部分（`base_url` 与 `model`——与 §5.6 的字段清单一致）。"""
        return {"base_url": self.base_url, "model": self.model}

    @classmethod
    def from_env(cls, env: Optional[Mapping[Text, Text]] = None) -> "LLMConfig":
        """从环境变量构造；缺必填项 → `LLMConfigMissing`（**绝不**用默认值兜）。"""
        env = os.environ if env is None else env
        problems: List[Text] = []

        base_url = (env.get(AI_BASE_URL_ENV) or "").strip()
        model = (env.get(AI_MODEL_ENV) or "").strip()
        api_key = (env.get(AI_API_KEY_ENV) or "").strip()

        if not base_url:
            problems.append(f"缺 {AI_BASE_URL_ENV}")
        if not model:
            problems.append(f"缺 {AI_MODEL_ENV}")

        # ★细化（见模块 docstring）：回环地址不要 key；非回环必须给 key
        if base_url and not api_key:
            try:
                host = urllib.parse.urlsplit(base_url).hostname or ""
            except ValueError:
                host = ""
            if not is_loopback(host):
                problems.append(
                    f"缺 {AI_API_KEY_ENV}（且 {base_url!r} 不是回环地址——"
                    "云端服务必须有 key；本地服务请用 http://127.0.0.1:<port>/v1）"
                )

        if problems:
            raise LLMConfigMissing(problems)

        def _int(name: Text, default: int) -> int:
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                problems.append(f"{name} 必须是整数，实为 {raw!r}")
                return default

        timeout = _int(AI_TIMEOUT_ENV, DEFAULT_TIMEOUT)
        retries = _int(AI_RETRIES_ENV, DEFAULT_RETRIES)
        max_cases = _int(AI_MAX_OUTPUT_CASES_ENV, DEFAULT_MAX_OUTPUT_CASES)
        if problems:
            raise LLMConfigMissing(problems)

        flag = (env.get(AI_ENABLE_ENV) or "on").strip().lower()

        return cls(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout=timeout,
            retries=retries,
            max_output_cases=max_cases,
            enabled=flag in _TRUTHY,
        )


@dataclass(frozen=True)
class LLMRequest:
    """一次模型请求（**采样参数全在这里**——因为它们必须进缓存键）。

    ★`cache_fields()` 的键集必须与 `cache.CACHE_KEY_FIELDS` **恰好相等**：
    少一个 → 改了那一项还能命中旧缓存（症状是"换参数没效果"）；
    多一个 → 有人加了采样参数却没同步清单（同一个漏检的镜像方向）。
    两个方向都由 `cache.build_cache_key` 报错，单测另有"两个集合相等"的元护栏。
    """

    system: Text
    user: Text
    model: Text
    base_url: Text
    prompt_version: Text = PROMPT_VERSION
    temperature: float = 0.0
    seed: int = 0
    max_tokens: int = 0
    response_format: Optional[Text] = None

    def cache_fields(self) -> Dict[Text, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "system": self.system,
            "user": self.user,
            "temperature": self.temperature,
            "seed": self.seed,
            "max_tokens": self.max_tokens,
            "response_format": self.response_format,
        }

    def messages(self) -> List[Dict[Text, Text]]:
        """OpenAI 兼容的 messages（system + user；few-shot 由调用方并进 system，见 §5.1）。"""
        messages: List[Dict[Text, Text]] = []
        if self.system:
            messages.append({"role": "system", "content": self.system})
        messages.append({"role": "user", "content": self.user})
        return messages

    def input_hash(self) -> Text:
        """输入 hash（审计文件名与 `FakeTransport` 回放键都用它）——**只含输入**。"""
        import hashlib  # noqa: PLC0415

        payload = f"{self.prompt_version}\x00{self.system}\x00{self.user}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class LLMResponseFormatError(Exception):
    """响应不是合法 JSON（剥围栏后仍解析不了）→ 归入"修正环"要回喂的报错类别。"""


@dataclass(frozen=True)
class LLMResponse:
    """一次调用的结果（**内存结构**；落盘由 cache/manifest 端口负责）。"""

    text: Text
    cached: bool = False
    attempts: int = 1
    usage: Optional[Dict[Text, Any]] = None
    trace: Tuple[Text, ...] = field(default_factory=tuple)

    def json(self) -> Any:
        """剥围栏后解析 JSON（§六 的退化路径：不支持约束解码时靠这一步兜住）。"""
        return parse_json_response(self.text)


def strip_code_fence(text: Text) -> Text:
    """剥掉 markdown 代码围栏（```json … ``` / ``` … ```）。

    NOTICE：这是**退化路径**的一部分（§六）：约束解码只有部分服务支持，
    其余服务常把 JSON 包在围栏里（哪怕 prompt 说了"不要围栏"）。
    所以剥围栏是**必须**的，而不是"顺手做的好事"。
    """
    stripped = (text or "").strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _first_json_object(text: Text) -> Optional[Text]:
    """从文本里截出**第一个完整的 JSON 对象/数组**（前后有解释性文字时用）。

    用括号配平扫描而不是正则：JSON 可以任意嵌套，正则做不对这件事
    （而且做不对的时候表现是"偶尔成功"，最难查）。字符串内的括号要跳过。
    """
    start = -1
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"' and depth > 0:
            in_string = True
        elif char in "{[" and depth == 0 and start < 0:
            start = index
            depth = 1
        elif char in "{[" and start >= 0:
            depth += 1
        elif char in "}]" and start >= 0:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_json_response(text: Text) -> Any:
    """剥围栏 → 直接解析；失败 → 截第一个 JSON 块再解析；仍失败 → 抛错（**不返回 None**）。

    为什么"抛错"而不是"返回 None"：调用方要拿它去驱动修正环（把错误文本回喂），
    一个静默的 `None` 会让上游以为"模型返回了空内容"，而真正的原因是格式。
    """
    candidate = strip_code_fence(text)
    try:
        return json.loads(candidate)
    except ValueError:
        block = _first_json_object(candidate)
        if block is not None:
            try:
                return json.loads(block)
            except ValueError as ex:
                raise LLMResponseFormatError(f"响应里的 JSON 块解析失败：{ex}") from ex
        preview = candidate[:200].replace("\n", "\\n")
        raise LLMResponseFormatError(f"响应不是合法 JSON（前 200 字符）：{preview}")


# ---------------------------------------------------------------------------
# 内容闸（§六 的"★ 内容闸"行）—— 键名级 → 内容级
# ---------------------------------------------------------------------------

# 内容级正则：**只在键名判不出来的时候用**（键名级是第一道，见 `scan_sensitive_keys`）。
# 顺序固定，报告里按它归类（"哪一类凭据被替换了"比"有一处被替换了"有用得多）。
SENSITIVE_PATTERNS: Tuple[Tuple[Text, Text], ...] = (
    ("OPENAI_KEY", r"sk-[A-Za-z0-9_\-]{20,}"),
    ("JWT", r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"),
    ("PEM", r"-----BEGIN [A-Z ]+-----"),
    ("AUTH_HEADER", r"(?i)authorization\s*[:=]\s*(?:basic|bearer)\s+\S+"),
    ("CREDENTIAL", r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*\S+"),
)

_SENSITIVE_RES: Tuple[Tuple[Text, "re.Pattern"], ...] = tuple(
    (name, re.compile(pattern)) for name, pattern in SENSITIVE_PATTERNS
)


def scan_sensitive_keys(mapping: Any, prefix: Text = "") -> List[Text]:
    """**键名级**扫描：复用内核 `utils.is_sensitive_key`（唯一口径，不另写关键词表）。

    NOTICE：为什么必须复用——内核那份已经处理了 `X-Api-Key` 这类**分隔符归一化**
    （`-`/`.`/空白 → `_` 再匹配），自己再抄一份必然漏掉某个别名，
    而漏判的方向是**明文外泄**（判错的方向只是显示上糊一点，代价不对称）。
    """
    from interfacetester.utils import is_sensitive_key  # noqa: PLC0415

    found: List[Text] = []
    if isinstance(mapping, Mapping):
        for key, value in mapping.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if is_sensitive_key(key):
                found.append(path)
            found.extend(scan_sensitive_keys(value, path))
    elif isinstance(mapping, (list, tuple)):
        for index, item in enumerate(mapping):
            found.extend(scan_sensitive_keys(item, f"{prefix}[{index}]"))
    return found


def scan_sensitive(text: Text) -> List[Dict[str, Any]]:
    """**内容级**扫描：返回 `[{kind, sample, span}]`，**不修改文本**。

    `sample` 刻意只留前 8 个字符 + 长度——清单本身也会被打印/落盘，
    把命中的凭据原文抄进清单等于**换个地方泄一次**。
    """
    findings: List[Dict[str, Any]] = []
    for kind, pattern in _SENSITIVE_RES:
        for match in pattern.finditer(text or ""):
            hit = match.group(0)
            findings.append(
                {
                    "kind": kind,
                    "sample": f"{hit[:8]}…（{len(hit)} 字符）",
                    "span": [match.start(), match.end()],
                }
            )
    return findings


def redact_sensitive(text: Text) -> Tuple[Text, List[Dict[str, Any]]]:
    """命中即替换为 `${ENV(<KIND>)}` 占位，返回 `(新文本, 替换清单)`。

    §六 的口径：**先替换再送**，并打印替换清单；确需原样送 → 用户显式 `--allow-raw`
    （那个开关在 CLI 层，本模块只提供"能替换"这一半——**默认安全**）。
    """
    findings = scan_sensitive(text)
    if not findings:
        return text, []

    redacted = text
    for kind, pattern in _SENSITIVE_RES:
        redacted = pattern.sub("${ENV(%s)}" % kind, redacted)
    return redacted, findings


# ---------------------------------------------------------------------------
# transport（注入点）—— §六 的"测试替身"行
# ---------------------------------------------------------------------------

class LLMTransportError(Exception):
    """传输层错误（网络/HTTP/响应结构）。`retryable` 区分"值得重试"与"重试无用"。"""

    def __init__(self, message: Text, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


def timeout_hint(config: "LLMConfig") -> Text:
    """超时类失败要附的**可执行提示**（★跨模型实测的教训：别把"超时"读成"模型不行"）。

    **为什么把提示写进错误文案**：默认上限 `60s` 对**本地大模型**普遍不够，而超时在
    trace 里只是一行 `网络异常 ReadTimeout`，最终报"重试 N 次仍失败"——**症状看起来像模型不行**。
    实测（§12.8-⑪）：`qwen3.8:27b` 单次调用 **>600s**、`gemma4:12b` **>240s**；
    换模型/换机器时必须同时调 `INTERFACETESTER_AI_TIMEOUT`。
    **报错处就是该改配置的地方**，所以提示跟着报错走（而不是只写在文档里等人去查）。
    """
    return (
        f"\n★疑似超时：本次每次调用的上限 = {config.timeout}s"
        f"（来自 `{AI_TIMEOUT_ENV}`；未设置时用默认值 {DEFAULT_TIMEOUT}s）。"
        "**本地大模型（Ollama / vLLM 等）常见需要 300s 起、慢的到 1800s** —— "
        f"换模型或换机器时请显式设置 `{AI_TIMEOUT_ENV}`（见 `deploy.md` §5.1）；"
        "云端快模型保持默认即可。"
    )


class Transport(Protocol):  # noqa: F821 - typing.Protocol（3.8+）
    """传输协议：任何带 `complete(request, config)` 的对象都能当替身（不必继承）。"""

    def complete(self, request: LLMRequest, config: LLMConfig) -> LLMResponse:  # pragma: no cover
        ...


class FakeTransport:
    """**回放替身**：单测 / CI 不出网的机器形态（§六 的"测试替身"行）。

    - `responses`：`{输入 hash 或 "*": 响应文本}`；也可传 `responder=callable(request)`；
    - `calls`：收到的**全部请求**——"命中缓存就不该再调模型"这类判据靠它断言；
    - `failures`：前 N 次抛 `LLMTransportError`（用来测重试轨迹与 `attempts`）。

    NOTICE（为什么它**不过 L3 闸门**）：L3 闸门的作用是"**别把请求打到真环境**"，
    而替身根本不发请求。把闸门塞进替身会让 CI 必须设 `SANDBOX=on` 才能跑单测——
    那是"为了绿而放宽闸门"，本仓的取向相反：闸门跟着**真出网**走（见 `HttpTransport`）。
    """

    def __init__(
        self,
        responses: Optional[Mapping[Text, Text]] = None,
        *,
        responder: Optional[Callable[[LLMRequest], Text]] = None,
        failures: int = 0,
    ) -> None:
        self.responses = dict(responses or {})
        self.responder = responder
        self.failures = failures
        self.calls: List[LLMRequest] = []

    def complete(self, request: LLMRequest, config: LLMConfig) -> LLMResponse:  # noqa: ARG002
        self.calls.append(request)
        if self.failures > 0:
            self.failures -= 1
            raise LLMTransportError("FakeTransport 预设的失败（用于测重试轨迹）", retryable=True)

        if self.responder is not None:
            return LLMResponse(text=self.responder(request), attempts=1)

        key = request.input_hash()
        if key in self.responses:
            return LLMResponse(text=self.responses[key], attempts=1)
        if "*" in self.responses:
            return LLMResponse(text=self.responses["*"], attempts=1)
        raise LLMTransportError(
            f"FakeTransport 没有为输入 {key} 准备响应（CI 不出网：请补 golden 回放）"
        )

    @property
    def call_count(self) -> int:
        """调用次数（判据"缓存命中时不再调用模型"直接读它）。"""
        return len(self.calls)


class HttpTransport:
    """`requests` 直连 OpenAI 兼容端点（**不引 LLM SDK**，内核已有 `requests`）。

    ★L3 闸门在**这里**——它是"真发请求"的唯一出口（见 `FakeTransport` 的 NOTICE）。

    重试策略（§六 的 `_RETRIES(默认2)`）：
    - 网络异常 / 5xx → **重试**（幂等性由调用方保证：我们发的是"生成草稿"，无副作用）；
    - 4xx → **不重试**（配置或请求有问题，重试只会重复付费并拖长时间）。

    采样参数照发（`temperature` / `seed` / `max_tokens`）：**服务端是否支持是它的事**——
    我们不假设，也不因此去掉参数（去掉会让"进缓存键的字段"与"实际发出的字段"分家）。
    """

    def __init__(self, *, requester: Optional[Callable[..., Any]] = None) -> None:
        self._requester = requester  # 测试可注入（避免真出网）

    def _post(self) -> Callable[..., Any]:
        if self._requester is not None:
            return self._requester
        import requests  # noqa: PLC0415 - 延迟 import：包在"没装 requests"时仍可被导入

        return requests.post

    @staticmethod
    def build_payload(request: LLMRequest) -> Dict[Text, Any]:
        """请求体（**可单测**：不碰网络也能断言"采样参数真的发出去了"）。"""
        payload: Dict[Text, Any] = {
            "model": request.model,
            "messages": request.messages(),
            "temperature": request.temperature,
            "seed": request.seed,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.response_format:
            payload["response_format"] = {"type": request.response_format}
        return payload

    def complete(self, request: LLMRequest, config: LLMConfig) -> LLMResponse:
        from interfacetester_ai.l3 import ensure_l3_allowed  # noqa: PLC0415

        ensure_l3_allowed(config.base_url)  # ★真出网前的 L3 闸门（拒绝即抛，不静默降级）

        post = self._post()
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        payload = self.build_payload(request)
        trace: List[Text] = []
        attempts = 0
        timeouts: List[Text] = []

        for attempt in range(1, config.retries + 2):
            attempts = attempt
            try:
                response = post(
                    config.endpoint(), json=payload, headers=headers, timeout=config.timeout
                )
            except Exception as ex:  # noqa: BLE001 - 网络层什么都可能抛
                kind = type(ex).__name__
                if "Timeout" in kind:  # requests 的 ConnectTimeout / ReadTimeout / Timeout
                    timeouts.append(kind)
                trace.append(f"第 {attempt} 次：网络异常 {kind}: {ex}")
                continue

            status = getattr(response, "status_code", 0)
            if status >= 500:
                trace.append(f"第 {attempt} 次：HTTP {status}（服务端错误，可重试）")
                continue
            if status >= 400:
                body = str(getattr(response, "text", ""))[:300]
                raise LLMTransportError(
                    f"HTTP {status}（**不重试**：4xx 通常是配置或请求问题，重试只会重复付费）：{body}",
                    retryable=False,
                )

            data = response.json()
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as ex:
                raise LLMTransportError(
                    f"响应结构不是 OpenAI 兼容形态（缺 choices[0].message.content）：{ex}；"
                    f"原始响应前 200 字符：{str(data)[:200]}"
                ) from ex

            return LLMResponse(
                text=text,
                attempts=attempts,
                usage=data.get("usage") if isinstance(data, Mapping) else None,
                trace=tuple(trace),
            )

        message = f"重试 {config.retries} 次仍失败（共尝试 {attempts} 次）：\n  " + "\n  ".join(trace)
        if timeouts:
            # ★提示只在"确实超时"时附上：非超时也刷这段，真提示就会被无视
            message += timeout_hint(config)
        raise LLMTransportError(message, retryable=True)


# ---------------------------------------------------------------------------
# 编排：缓存 → transport → 缓存写回 → 审计（**本模块不碰文件系统**）
# ---------------------------------------------------------------------------

def complete(
    request: LLMRequest,
    *,
    transport: Transport,
    config: Optional[LLMConfig] = None,
    cache: Optional[Any] = None,
    audit: Optional[Any] = None,
    no_cache: bool = False,
) -> LLMResponse:
    """一次模型调用。`LLMResponse.cached` 说明这次是**回放**还是**实调**。

    协作对象都是**注入的**（duck typing，不 import 具体实现）：
    - `transport`：`FakeTransport`（CI）或 `HttpTransport`（真跑）；
    - `cache`：需 `get(key) -> Optional[Text]` / `put(key, text)`；
    - `audit`：需 `record(entry: dict)`。

    ★两处口径（都是"看起来多余、去掉就出事"的）：
    1. **缓存键只由 `request.cache_fields()` 决定**——所以"改 `temperature` 必 miss"
       不靠实现自觉，靠 `cache.CACHE_KEY_FIELDS` 那张清单（T5）；
    2. **命中缓存也要写审计**：那次也是"这次产物基于哪份输入"的证据，
       不记的话复盘时会以为"模型又跑了一遍"，而两次产物其实来自同一份回放。
    """
    from interfacetester_ai.cache import build_cache_key  # noqa: PLC0415

    key = build_cache_key(request.cache_fields())
    entry: Dict[Text, Any] = {
        "cache_key": key,
        "input_hash": request.input_hash(),
        "prompt_version": request.prompt_version,
        "model": request.model,
        "base_url": request.base_url,
        "temperature": request.temperature,
        "seed": request.seed,
        "max_tokens": request.max_tokens,
        "response_format": request.response_format,
    }

    if cache is not None and not no_cache:
        hit = cache.get(key)
        if hit is not None:
            response = LLMResponse(text=hit, cached=True, attempts=0, trace=("cache-hit",))
            if audit is not None:
                audit.record({**entry, "cached": True, "attempts": 0})
            return response

    response = transport.complete(
        request, config or LLMConfig(base_url=request.base_url, model=request.model)
    )

    if cache is not None and not no_cache:
        cache.put(key, response.text)

    if audit is not None:
        audit.record(
            {
                **entry,
                "cached": response.cached,
                "attempts": response.attempts,
                "usage": response.usage,
                "trace": list(response.trace),
                # 原始响应照录：LLM 的每次臆造都要能复盘（§六 的审计行）
                "response_text": response.text,
            }
        )

    return response


def run_selftest(verbose: bool = False) -> int:
    """跑 llm 自检（**纯逻辑、不写盘**：替身 + 内存缓存）。返回 0=全绿；非 0=失败项数。"""
    from interfacetester_ai.cache import CACHE_KEY_FIELDS, build_cache_key  # noqa: PLC0415

    failures: List[Text] = []

    # ① 缓存键字段集必须与 T5 的清单**恰好相等**（采样参数漏一个 = 改了也不 miss）
    fields = set(
        LLMRequest(
            system="s", user="u", model="m", base_url="http://127.0.0.1:1/v1"
        ).cache_fields()
    )
    if fields != set(CACHE_KEY_FIELDS):
        failures.append(
            f"[缓存键字段不一致] LLMRequest 给 {sorted(fields)}，清单是 {sorted(CACHE_KEY_FIELDS)}"
        )

    # ② 配置：缺必填 → 报（且消息里告诉人怎么配）
    try:
        LLMConfig.from_env({})
    except LLMConfigMissing as error:
        if AI_BASE_URL_ENV not in str(error) or "配置指引" not in str(error):
            failures.append(f"[配置报错不给指引] {error}")
        elif verbose:
            print("  [拦下 ok] 空环境 → LLMConfigMissing（带配置指引）")
    else:
        failures.append("[漏放] 空环境下竟然构造出了配置")

    # ③ ★细化口径成对：回环不要 key（放行）／非回环必须 key（拒绝）
    cfg_local = LLMConfig.from_env(
        {AI_BASE_URL_ENV: "http://127.0.0.1:11434/v1", AI_MODEL_ENV: "qwen2.5:14b"}
    )
    if not cfg_local.is_local():
        failures.append("[配置错] Ollama 形态（回环）没被识别为本地")
    if cfg_local.endpoint() != "http://127.0.0.1:11434/v1/chat/completions":
        failures.append(f"[配置错] endpoint 拼错：{cfg_local.endpoint()}")
    try:
        LLMConfig.from_env(
            {AI_BASE_URL_ENV: "https://api.example.com/v1", AI_MODEL_ENV: "gpt-4o-mini"}
        )
    except LLMConfigMissing as error:
        if AI_API_KEY_ENV not in str(error):
            failures.append(f"[配置报错不点名] 缺 key 的报错没提到 {AI_API_KEY_ENV}：{error}")
    else:
        failures.append("[漏放] 云端 base_url 没有 key 竟然放行了")

    # ④ endpoint 容忍尾斜杠与"已带全路径"
    if LLMConfig(base_url="http://x/v1/", model="m").endpoint() != "http://x/v1/chat/completions":
        failures.append("[endpoint 错] 尾斜杠没被规范化")
    if LLMConfig(base_url="http://x/v1/chat/completions", model="m").endpoint() != (
        "http://x/v1/chat/completions"
    ):
        failures.append("[endpoint 错] 已带全路径时被重复拼接")

    # ⑤ 围栏剥离（退化路径：约束解码不是所有服务都支持）
    parse_cases = (
        ('```json\n{"a": 1}\n```', {"a": 1}, "json 围栏"),
        ('```\n{"a": 1}\n```', {"a": 1}, "无语言围栏"),
        ('{"a": 1}', {"a": 1}, "无围栏"),
        ('好的，这是结果：\n{"a": 1}\n希望有帮助', {"a": 1}, "前后有解释性文字"),
    )
    for raw, want, label in parse_cases:
        try:
            got = parse_json_response(raw)
        except LLMResponseFormatError as error:
            failures.append(f"[解析错] {label} 解析失败：{error}")
            continue
        if got != want:
            failures.append(f"[解析错] {label} 得到 {got!r}，期望 {want!r}")

    # ⑥ 真坏响应必须**抛错**（不返回 None——上游要拿错误去驱动修正环）
    try:
        parse_json_response("这不是 JSON，只是一段话")
    except LLMResponseFormatError:
        pass
    else:
        failures.append("[漏放] 非 JSON 响应没有抛错（返回 None 会让上游误判为空内容）")

    # ⑦ 内容闸：命中 + 替换 + 清单
    secret = 'token: "abc" 以及 sk-abcdefghijklmnopqrstuvwx0'
    kinds = {item["kind"] for item in scan_sensitive(secret)}
    if "OPENAI_KEY" not in kinds or "CREDENTIAL" not in kinds:
        failures.append(f"[内容闸漏] 应命中 OPENAI_KEY 与 CREDENTIAL，实为 {kinds}")
    redacted, listed = redact_sensitive(secret)
    if "sk-abcdefghijklmnopqrstuvwx0" in redacted or "${ENV(OPENAI_KEY)}" not in redacted:
        failures.append(f"[内容闸没换掉] {redacted}")
    if not listed:
        failures.append("[内容闸无清单] 替换了却没给出清单")
    if any("abcdefghijkl" in str(item.get("sample", "")) for item in listed):
        failures.append("[清单不自保] 替换清单里留了凭据原文（等于换个地方再泄一次）")

    # ⑧ 键名级复用内核口径（`X-Api-Key` 的短横线归一化是内核已经解决过的坑）
    if scan_sensitive_keys({"X-Api-Key": "v", "items": {}}) != ["X-Api-Key"]:
        failures.append("[键名级错] X-Api-Key 应当命中（且 items 不该）")

    # ⑨ 缓存：**命中时不得再调模型**（用替身的调用计数断言，不靠"看起来对"）
    request = LLMRequest(system="s", user="u", model="m", base_url="http://127.0.0.1:1/v1")
    transport = FakeTransport({"*": '{"ok": true}'})

    class _MemCache:
        def __init__(self) -> None:
            self.data: Dict[Text, Text] = {}

        def get(self, key: Text) -> Optional[Text]:
            return self.data.get(key)

        def put(self, key: Text, text: Text) -> None:
            self.data[key] = text

    class _MemAudit:
        def __init__(self) -> None:
            self.entries: List[Dict[Text, Any]] = []

        def record(self, entry: Dict[Text, Any]) -> None:
            self.entries.append(entry)

    cache, audit = _MemCache(), _MemAudit()
    first = complete(request, transport=transport, cache=cache, audit=audit)
    if first.cached or transport.call_count != 1:
        failures.append(f"[缓存错] 首次应当实调一次（cached={first.cached}）")
    second = complete(request, transport=transport, cache=cache, audit=audit)
    if not second.cached or transport.call_count != 1:
        failures.append(
            f"[缓存错] 第二次应命中回放且**不再调模型**（实调 {transport.call_count} 次）"
        )
    if len(audit.entries) != 2:
        failures.append(f"[审计错] 两次调用都应留痕，实为 {len(audit.entries)} 条")

    # ⑩ 改**任一**采样参数必 miss（T5 的判据在这里体现为"替身又被调了一次"）
    warmer = LLMRequest(
        system="s", user="u", model="m", base_url="http://127.0.0.1:1/v1", temperature=0.7
    )
    complete(warmer, transport=transport, cache=cache)
    if transport.call_count != 2:
        failures.append(f"[缓存错] 只改 temperature 就应 miss（实调 {transport.call_count} 次）")
    if build_cache_key(request.cache_fields()) == build_cache_key(warmer.cache_fields()):
        failures.append("[缓存键错] 采样参数不同却算出同一个键")

    # ⑪ `no_cache` 强制实调
    before = transport.call_count
    complete(request, transport=transport, cache=cache, no_cache=True)
    if transport.call_count != before + 1:
        failures.append("[no_cache 失效] 指定 no_cache 时仍走了缓存")

    # ⑫ 采样参数**真的进请求体**（"发了没发"不能靠猜）
    payload = HttpTransport.build_payload(
        LLMRequest(
            system="s",
            user="u",
            model="m",
            base_url="http://127.0.0.1:1/v1",
            temperature=0.25,
            seed=7,
            max_tokens=99,
            response_format="json_object",
        )
    )
    for key, want in (("temperature", 0.25), ("seed", 7), ("max_tokens", 99)):
        if payload.get(key) != want:
            failures.append(f"[请求体缺项] {key} 应为 {want!r}，实为 {payload.get(key)!r}")
    if payload.get("response_format") != {"type": "json_object"}:
        failures.append("[请求体缺项] response_format 没有发出去")
    if payload["messages"][0]["role"] != "system":
        failures.append("[请求体错] messages 结构不对")

    # ⑬ ★超时文案（2026-09-25）：**超时类**失败必须自带"调哪个变量、调到多少"的提示；
    #    而**非超时**失败不许带 —— 提示到处刷，真提示就会被无视（本仓对"噪声"的一贯态度）。
    cfg_timeout = LLMConfig(base_url="http://127.0.0.1:11434/v1", model="m", timeout=60, retries=0)
    probe = LLMRequest(system="s", user="u", model="m", base_url=cfg_timeout.base_url)
    from interfacetester_ai.l3 import SANDBOX_ENV  # noqa: PLC0415

    class _ReadTimeout(Exception):
        """模仿 requests 的 `ReadTimeout`（判据只看**类名**，所以不必 import requests）。"""

    def _raise_timeout(url, **kwargs):  # noqa: ARG001
        raise _ReadTimeout("read timed out")

    def _raise_conn(url, **kwargs):  # noqa: ARG001
        raise ConnectionError("refused")

    # 只为让 L3 闸门放行 —— 请求器是**注入的替身**，一次网都不出；用完原样还原。
    _sandbox_backup = os.environ.get(SANDBOX_ENV)
    os.environ[SANDBOX_ENV] = "on"
    try:
        for raiser, want_hint, label in (
            (_raise_timeout, True, "超时失败"),
            (_raise_conn, False, "非超时失败"),
        ):
            try:
                HttpTransport(requester=raiser).complete(probe, cfg_timeout)
            except LLMTransportError as error:
                text = str(error)
                if want_hint:
                    if AI_TIMEOUT_ENV not in text or str(cfg_timeout.timeout) not in text:
                        failures.append(f"[超时提示缺项] 没点名变量或当前上限：{text}")
                    elif verbose:
                        print(f"  [拦下 ok] {label} → 报错自带 `{AI_TIMEOUT_ENV}` 提示")
                elif AI_TIMEOUT_ENV in text:
                    failures.append(f"[超时提示误报] {label} 也附了超时提示（会把真提示淹掉）")
            else:
                failures.append(f"[漏报] {label} 竟然没抛 LLMTransportError")
    finally:
        if _sandbox_backup is None:
            os.environ.pop(SANDBOX_ENV, None)
        else:
            os.environ[SANDBOX_ENV] = _sandbox_backup

    print("=" * 66)
    if failures:
        print(f"llm 接入层自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("llm 接入层自检全部通过：")
    print("  配置    ：缺必填 → 报错+指引；回环免 key、云端必须 key（★细化口径，判据成对）")
    print("  协议    ：OpenAI 兼容 payload（temperature/seed/max_tokens/response_format 照发）")
    print("  退化解码：围栏剥离 4 形态 + 前后噪声截取；真坏响应**抛错**不返回 None")
    print("  内容闸  ：键名级复用内核 is_sensitive_key；内容级 5 类正则命中即换 ${ENV(...)}")
    print("  缓存    ：命中即回放（**不再调模型**）+ 改任一采样参数必 miss + no_cache 可强制实调")
    print("  审计    ：每次调用留痕（含命中回放那次）——本模块**不写盘**（端口注入）")
    print(
        f"  超时提示：超时类失败自带「调 `{AI_TIMEOUT_ENV}`（本地大模型 300 起）」；"
        "**非超时失败不附**（防噪声淹没真提示）"
    )
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

    parser = argparse.ArgumentParser(description="LLM 接入层（P0a）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "AI_API_KEY_ENV",
    "AI_BASE_URL_ENV",
    "AI_ENABLE_ENV",
    "AI_MAX_OUTPUT_CASES_ENV",
    "AI_MODEL_ENV",
    "AI_RETRIES_ENV",
    "AI_TIMEOUT_ENV",
    "DEFAULT_MAX_OUTPUT_CASES",
    "DEFAULT_RETRIES",
    "DEFAULT_TIMEOUT",
    "SENSITIVE_PATTERNS",
    "FakeTransport",
    "HttpTransport",
    "LLMConfig",
    "LLMConfigMissing",
    "LLMRequest",
    "LLMResponse",
    "LLMResponseFormatError",
    "LLMTransportError",
    "Transport",
    "complete",
    "parse_json_response",
    "redact_sensitive",
    "run_selftest",
    "timeout_hint",
    "scan_sensitive",
    "scan_sensitive_keys",
    "strip_code_fence",
]


if __name__ == "__main__":
    raise SystemExit(main())

