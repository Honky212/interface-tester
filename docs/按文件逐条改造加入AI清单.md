# 按文件逐条改造加入 AI 清单

> ⚠️ **已废弃（历史方案，2026-09-26）**：本文档主张**直接修改内核**（`interfacetester/`，例如
> `parser_ai = subparsers.add_parser("ai")`），与本项目**现行方案冲突**——现行方案是**旁路包**
> `interfacetester_ai`（入口 `haify`、**内核 0 改动**、删包即回滚），见 `docs/interfacetester智能化改造方案8.md` §2.1 / §2.2。
> **原文保留供追溯，请勿据此改动内核**（登记：`docs/ARCHIVED.md`）。

> 目标：按现有源码结构，把 AI 能力逐层接进 interfacetester，并且明确每个文件新增什么函数、函数签名、数据结构字段，以及三层伪代码落地方式。

---

## 1. 总体方案

### 设计原则

- AI 只负责：生成、分析、修正建议
- interfacetester 继续负责：执行、断言、汇总、判定
- 人类负责：最终确认与高风险修正

### 结构原则

```text
文档输入
   ↓
AI 生成草稿
   ↓
schema 校验
   ↓
loader / make 校验
   ↓
框架生成 YAML / 执行用例
   ↓
runner 产出失败证据
   ↓
AI 分析失败原因
   ↓
AI 给修正建议
   ↓
人工确认后应用
```

---

## 2. 文件逐条改造清单

## 文件 1：cli.py

### 作用
命令行入口，用于对 AI 功能做独立入口划分。

### 新增函数

```python
def main_ai(args) -> int:
    """ai 统一入口"""


def main_ai_gen(args) -> int:
    """根据接口文档生成测试用例"""


def main_ai_analyze(args) -> int:
    """读取 summary + logs，分析失败归因"""


def main_ai_fix(args) -> int:
    """生成修正建议，不直接落盘"""


def main_ai_summary(args) -> int:
    """生成报告摘要"""
```

### 函数签名模板

```python
def main_ai(args: argparse.Namespace) -> int:
    ...


def main_ai_gen(args: argparse.Namespace) -> int:
    ...


def main_ai_analyze(args: argparse.Namespace) -> int:
    ...


def main_ai_fix(args: argparse.Namespace) -> int:
    ...
```

### 需要新增的参数

```python
# 例如：
parser_ai = subparsers.add_parser("ai")
ai_subparsers = parser_ai.add_subparsers(dest="ai_command")
```

### 设计意图

- 不打破原有 `run/make/convert` 命令
- AI 功能作为新一层入口而存在
- 易于 CI 接入和人工调用

---

## 文件 2：converters/__init__.py

### 作用
注册新的转化源，支持“接口文档 -> YAML”。

### 新增内容

```python
SUPPORTED_SOURCES = {
    "curl": "curl 命令",
    "har": "HAR 1.2",
    "postman": "Postman Collection",
    "openapi": "OpenAPI 3.x",
    "doc": "接口文档（Markdown / 文本）",
}

IMPLEMENTED_SOURCES = ("curl", "har", "postman", "openapi", "doc")
```

### 新增函数

无必须新增函数，但建议新增：

```python
def register_ai_source() -> None:
    """注册文档-ai source"""
```

### 设计意图

- 让 AI 文档生成走统一转换入口
- 符合现有 converters 设计风格

---

## 文件 3：converters/doc_adapter.py

### 作用
新增“AI / 文档 -> IRCase”适配器。

### 新增函数

```python
def convert_doc_file(doc_text: str, case_name: str = "", source: str = "") -> IRCase:
    """把接口文档文本转换为标准 IRCase"""


def build_doc_case_draft(doc_text: str) -> dict:
    """把文档转为结构化草稿，供 AI 生成器使用"""
```

### 返回结构建议

```python
from interfacetester.converters.ir import IRCase, IRStep

case = IRCase(
    name="登录用例",
    steps=[
        IRStep(
            name="获取 token",
            method="POST",
            url="/api/login",
            headers={"Content-Type": "application/json"},
            json={"username": "${ENV(USERNAME)}"},
        )
    ],
)
```

### 设计意图

- 以“文档输入”为起点
- 让 AI 生成的内容最终仍是框架可识别的 IR 格式

---

## 文件 4：models.py

### 作用
补齐 AI 的 structured schema，避免 LLM 输出脏数据。

### 新增结构

```python
class CaseDraft(BaseModel):
    case_name: str
    base_url_env: str = "BASE_URL"
    steps: List[StepDraft] = []
    unknowns: List[UnknownField] = []


class StepDraft(BaseModel):
    name: str
    method: str
    url: str
    headers: Dict[str, str] = {}
    params: Dict[str, Any] = {}
    json: Dict[str, Any] = {}
    data: Dict[str, Any] = {}
    extract: Dict[str, str] = {}
    validate: List[AssertionDraft] = []
    depends_on_extract: List[str] = []


class AssertionDraft(BaseModel):
    comparator: str
    check: str
    expect: Any
    source_quote: str = ""
    from_doc: bool = True


class UnknownField(BaseModel):
    where: str
    why: str


class DiagnosisResult(BaseModel):
    case_name: str
    cause_type: str
    confidence: float
    evidence: List[str]
    reason: str
    suggestion: str


class FixSuggestion(BaseModel):
    case_name: str
    step_name: str
    before: str
    after: str
    reason: str
    risk: str
    requires_human_confirmation: bool = True
```

### 关键字段设计

- `source_quote`：用例断言引用文档原句
- `confidence`：归因可信度
- `cause_type`：类别枚举值
- `requires_human_confirmation`：是否必须人工确认

### 设计意图

- 让大模型输出“结构化 JSON”，而不是裸文本
- 让框架可以精确校验 AI 输出的每一个字段

---

## 文件 5：loader.py

### 作用
挡住 AI 生成中“未知字段”和“静默丢失”的问题。

### 新增函数

```python
def validate_ai_generated_case(case_data: Dict) -> None:
    """AI 用例草稿的强校验"""


def warn_unknown_testcase_fields_for_ai(raw_case: Dict) -> List[str]:
    """把 warning 升级为 failure"""


def ensure_ai_assertion_allowed(assertion: Dict) -> None:
    """确保断言算子在合法白名单中"""
```

### 关键逻辑

```python
def validate_ai_generated_case(case_data: Dict) -> None:
    if not isinstance(case_data, dict):
        raise exceptions.TestCaseFormatError("AI case draft must be a dict")

    for step in case_data.get("steps", []):
        if "method" not in step or "url" not in step:
            raise exceptions.TestCaseFormatError("AI step missing method/url")

        for assertion in step.get("validate", []):
            ensure_ai_assertion_allowed(assertion)
```

### 设计意图

- AI 输出不允许“字段名写错但被吞掉”
- 严格校验是 AI 接入的第一道防线

---

## 文件 6：make.py

### 作用
把 AI 生草稿变成最终可以执行的测试文件，并进行重试修正。

### 新增函数

```python
def generate_case_from_ai_draft(draft: Dict) -> str:
    """把 AI draft 转成 YAML 文本"""


def validate_emitted_ai_case(case_path: str) -> None:
    """校验生成后的用例能否被框架加载"""


def repair_ai_case(draft: Dict, error_msg: str) -> Dict:
    """根据失败信息回喂修正"""


def make_ai_case(doc_text: str, case_name: str) -> str:
    """统一入口：doc -> draft -> YAML -> 校验"""
```

### 伪代码

```python
def make_ai_case(doc_text: str, case_name: str) -> str:
    draft = llm_generate_case(doc_text, case_name)
    case_obj = CaseDraft.model_validate(draft)
    yaml_text = generate_case_from_ai_draft(case_obj.model_dump())

    case_path = write_temp_case(yaml_text)
    validate_ai_generated_case(load_yaml(case_path))
    validate_emitted_ai_case(case_path)
    return case_path
```

### 设计意图

- AI 生成物进入的是标准 YAML 路径
- 不是直接输出自由文本脚本
- 保持与现有框架一致

---

## 文件 7：runner.py

### 作用
为 AI 分析失败提供可用证据。

### 新增函数

```python
def extract_failure_evidence(self) -> List[Dict]:
    """提取失败证据"""


def build_ai_analysis_payload(self) -> Dict:
    """组装 AI 分析输入"""


def summarize_failure_records(self) -> Dict:
    """轻量汇总失败记录"""
```

### 返回的结构示例

```python
{
    "case_name": "登录用例",
    "step_name": "登录获取 token",
    "assertion": {
        "comparator": "eq",
        "check": "body.code",
        "expect": 0,
        "actual": 1001,
    },
    "request": {
        "method": "POST",
        "url": "/api/login",
        "body": {"username": "admin"},
    },
    "response": {
        "status_code": 200,
        "body": {"code": 1001, "message": "用户名或密码错误"},
    },
    "log_path": "logs/xxxx.run.log",
}
```

### 设计意图

- AI 看到的是结构化且脱敏的失败证据
- 不再盲看原始日志文本

---

## 文件 8：utils.py

### 作用
做敏感值脱敏，保证 AI 访问时不泄露真实密码、token 等。

### 新增函数

```python
def mask_sensitive_data_for_ai(data: Any) -> Any:
    """递归替换敏感字段为 ******"""


def sanitize_for_ai_payload(payload: Dict) -> Dict:
    """对发送给 AI 的数据做统一脱敏"""


def is_sensitive_key_for_ai(key_name: str) -> bool:
    """识别常见敏感字段"""
```

### 脱敏字段建议

```python
SENSITIVE_KEYS = {
    "password",
    "token",
    "authorization",
    "secret",
    "cookie",
    "sessionid",
    "x-api-key",
    "auth",
}
```

### 设计意图

- 最小暴露原则
- AI 不能拿到真实凭证

---

## 文件 9：compat.py

### 作用
读取 `summary.json`，准备失败分析输入。

### 新增函数

```python
def load_summary_report(summary_path: str) -> Dict:
    """读取 summary.json"""


def extract_failed_cases(summary: Dict) -> List[Dict]:
    """筛出失败的用例"""


def build_ai_summary_input(summary: Dict) -> Dict:
    """组装 AI 用分析输入"""
```

### 返回字段示例

```python
{
    "total": 128,
    "success": 119,
    "fail": 9,
    "failed_cases": [
        {"case_name": "登录用例"},
        {"case_name": "支付用例"},
    ],
}
```

### 设计意图

- AI 分析应基于 framework 的 structured summary，而不是手工统计
- 稳定、可审计、便于回归

---

## 文件 10：新增 AI 模块（推荐）

### 路径建议

```text
interfacetester_ai/
├── __init__.py
├── client.py
├── schema.py
├── prompts.py
├── generate.py
├── analyze.py
├── fix.py
├── report.py
└── cache.py
```

### 新增函数示例

```python
# generate.py

def generate_case_from_doc(doc_text: str, case_name: str) -> dict:
    ...

# analyze.py

def analyze_failure(summary: dict, logs: list) -> dict:
    ...

# fix.py

def suggest_fix(case: dict, failure: dict) -> dict:
    ...
```

### 设计意图

- 把 AI 能力从核心框架中解耦
- 更容易替换模型供应商
- 更容易做审计和缓存

---

## 3. 三层伪代码落地

## 伪代码 A：生成层

```python
def make_ai_case(doc_text: str, case_name: str) -> str:
    draft = llm_generate_case(doc_text, case_name)
    case_obj = CaseDraft.model_validate(draft)

    yaml_text = generate_case_from_ai_draft(case_obj.model_dump())
    case_path = write_temp_yaml(yaml_text)

    validate_ai_generated_case(load_yaml(case_path))
    validate_emitted_ai_case(case_path)

    return case_path
```

### 目的
让 LLM 输出的内容先经过 schema 校验，再经过框架校验。

---

## 伪代码 B：分析层

```python
def analyze_failure(summary_path: str, log_dir: str) -> DiagnosisResult:
    summary = load_summary_report(summary_path)
    failures = extract_failed_cases(summary)

    evidence_payload = []
    for item in failures:
        evidence_payload.append(extract_failure_evidence_from_runner(item, log_dir))

    payload = sanitize_for_ai_payload({"failures": evidence_payload})
    result = llm_analyze(payload)
    diagnosis = DiagnosisResult.model_validate(result)
    return diagnosis
```

### 目的
结合失败证据判断“缺陷/用例错误/环境问题/数据问题/未知”。

---

## 伪代码 C：修复层

```python
def suggest_fix(case_path: str, failure_payload: dict) -> FixSuggestion:
    payload = sanitize_for_ai_payload(failure_payload)
    suggestion = llm_suggest_fix(payload)
    return FixSuggestion.model_validate(suggestion)


def apply_fix_if_confirmed(case_path: str, suggestion: FixSuggestion) -> None:
    if suggestion.requires_human_confirmation:
        print("待人工确认")
        return

    patch_yaml(case_path, suggestion.before, suggestion.after)
```

### 目的
默认只给修正建议，不直接改用例；高风险修正必须经人工确认。

---

## 4. 最终落地顺序建议

### 第一阶段：最稳妥
1. 文档生成用例
2. schema + loader 强校验
3. summary.json 分析
4. 人工确认修正

### 第二阶段：半自动修复
1. AI 提供 diff
2. 人工确认后落盘
3. 回归验证

### 第三阶段：低风险自动修正
1. 仅修正字段名、路径、变量引用这种明确问题
2. 需可回滚
3. 必须保留审计日志

---

## 5. 一句话总结

“真正的 AI 接入点，不是在框架执行层直接插大模型，而是在生成前、失败后和修正建议层加 AI，同时使用 schema 校验、loader 校验和人工确认来守住框架的可信性。”
