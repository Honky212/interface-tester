# AI 生成用例、失败分析、修正建议的 Prompt 模板与接口设计草案

> ⚠️ **已废弃（历史方案，2026-09-26）**：本文档允许模型响应返回 `"requires_human_confirmation": false`（即**自动落地修正**），
> 与本项目**现行实现冲突**——`haify fix` 只给建议、**零写盘**、`--apply` 不存在，人工确认是唯一语义闸门
> （该分歧已于 2026-09-26 **定案**：**不支持**自动 apply，见 `docs/未做完的待决策项.md` §一 D3）。
> 现行方案入口与边界见 `docs/interfacetester智能化改造方案8.md` §2.1 / §2.2。
> **原文保留供追溯，请勿据此改动内核**（登记：`docs/ARCHIVED.md`）。

> 目标：把 AI 能力分成三类：
> 1. 生成用例
> 2. 失败分析
> 3. 修正建议
>
> 同时给出这三类能力的统一接口设计，并给出可直接落地的 Prompt 模板。

---

## 1. 总体设计

### 1.1 设计原则

- AI 不直接执行接口测试，只负责输出“草稿 / 分析 / 建议”
- interfacetester 继续负责“真实执行、断言、判定、汇总”
- AI 产物必须经过“schema 校验 + loader 校验 + make 校验”
- 失败分析必须基于证据，而不是猜测
- 修正建议默认不直接落盘，必须人工确认

### 1.2 三类 AI 能力的职责边界

| 能力 | 输入 | 输出 | 最终落地方式 |
| --- | --- | --- | --- |
| 生成用例 | 接口文档 | 用例草稿 / YAML | 经过框架校验后落盘 |
| 失败分析 | summary.json + logs + 请求/响应 | 原因归因 + 证据 + 建议 | 生成分析报告，不改测试结论 |
| 修正建议 | 失败证据 + 用例原文 | diff / 建议说明 | 先建议，人工确认再应用 |

---

## 2. 通用接口设计草案

## 2.1 通用请求体

```json
{
  "request_id": "uuid",
  "task_type": "generate_case | analyze_failure | suggest_fix",
  "model": "gpt-4o-mini",
  "prompt_version": "v1.0",
  "input": {},
  "metadata": {
    "project": "project-two",
    "case_name": "登录用例",
    "timestamp": "2026-09-23T10:00:00Z"
  }
}
```

### 字段说明

- `request_id`：每次调用唯一标识
- `task_type`：任务类型
- `model`：模型名
- `prompt_version`：prompt 版本，保证可追溯
- `input`：业务输入内容
- `metadata`：工程上下文

---

## 2.2 通用响应体

```json
{
  "request_id": "uuid",
  "status": "success | failed | needs_human_review",
  "result": {},
  "warnings": [],
  "errors": [],
  "confidence": 0.92,
  "evidence": [
    "断言 body.code 期望 0，实际 1001",
    "响应中 message 为 用户名或密码错误"
  ],
  "requires_human_confirmation": true
}
```

### 说明

- `status`：任务状态
- `result`：结构化结果
- `warnings`：AI 输出中的潜在风险
- `errors`：结构化错误信息
- `confidence`：置信度
- `requires_human_confirmation`：是否必须人工确认

---

## 3. 生成用例接口设计草案

## 3.1 接口定义

```http
POST /api/ai/generate-case
Content-Type: application/json
```

### 请求体

```json
{
  "task_type": "generate_case",
  "input": {
    "doc_text": "# 登录接口\nPOST /api/login ...",
    "case_name": "登录用例",
    "base_url_env": "BASE_URL",
    "context": {
      "project_root": "D:/interface/project-two",
      "framework": "interfacetester"
    }
  }
}
```

### 响应体

```json
{
  "status": "success",
  "result": {
    "case_name": "登录用例",
    "base_url_env": "BASE_URL",
    "steps": [
      {
        "name": "登录获取 token",
        "method": "POST",
        "url": "/api/login",
        "headers": {
          "Content-Type": "application/json"
        },
        "json": {
          "username": "${ENV(USERNAME)}",
          "password": "${ENV(PASSWORD)}"
        },
        "extract": {
          "token": "body.data.token"
        },
        "validate": [
          {"comparator": "eq", "check": "status_code", "expect": 200},
          {"comparator": "eq", "check": "body.code", "expect": 0}
        ]
      }
    ]
  },
  "warnings": [],
  "requires_human_confirmation": false
}
```

---

## 3.2 Prompt 模板：生成用例

### System Prompt

```text
你是 interfacetester 的测试用例生成器。

任务：根据接口文档生成结构化测试用例草稿，最终必须能转换成 interfacetester YAML。

硬性规则：
1. 只输出合法 JSON，不要 markdown 代码块，不要解释文字。
2. 输出必须符合固定 schema，字段必须严格存在。
3. 只允许使用已知的请求字段：method, url, params, headers, cookies, data, json, timeout, verify, allow_redirects, upload, proxies, cert, stream。
4. 只允许使用已知断言算子：eq, ne, str_eq, type_match, gt, ge, lt, le, len_eq, len_gt, len_ge, len_lt, len_le, contains, contained_by, startswith, endswith, regex_match, jsonschema_match, xpath_match, xpath_count, xml_schema_match。
5. 不能凭空造字段；文档未写明的信息不要臆造。
6. 账号/密码/域名等敏感信息一律写成 ${ENV(变量名)}；不能出现明文密码、token、密钥。
7. 每条 validate 必须附上 source_quote，引用文档中的原句。
8. 若有信息不确定，写入 unknowns，不要强行填充。
9. 不能输出 markdown 或自然语言解释。

返回结构：
{
  "case_name": "...",
  "base_url_env": "BASE_URL",
  "steps": [
    {
      "name": "...",
      "method": "POST",
      "url": "/api/login",
      "headers": {},
      "json": {},
      "extract": {},
      "validate": [
        {"comparator": "eq", "check": "status_code", "expect": 200, "source_quote": "..."}
      ]
    }
  ],
  "unknowns": []
}
```

### User Prompt

```text
接口文档如下：

[接口文档正文]

请生成 interfacetester 用例草稿，要求：
- 提取关键请求参数
- 按步骤组织
- 生成必要断言
- 不要用未声明字段
- 把敏感字段转成 ${ENV(...)}
- 生成 source_quote
```

### 生成准则

- 如果字段说明不明确，放进 `unknowns`
- 如果文档写了必填字段，生成 `validate` / `extract`
- 如果是 token 类流程，优先生成 `extract` 之后的下一个步骤
- 如果接口是登录-鉴权链路，优先保留 token 传递链路

---

## 4. 失败分析接口设计草案

## 4.1 接口定义

```http
POST /api/ai/analyze-failure
Content-Type: application/json
```

### 请求体

```json
{
  "task_type": "analyze_failure",
  "input": {
    "case_name": "登录用例",
    "summary": {
      "success": false,
      "fail": 1
    },
    "failure_records": [
      {
        "step_name": "登录获取 token",
        "assertion": {
          "comparator": "eq",
          "check": "body.code",
          "expect": 0,
          "actual": 1001
        },
        "request": {
          "method": "POST",
          "url": "/api/login"
        },
        "response": {
          "status_code": 200,
          "body": {
            "code": 1001,
            "message": "用户名或密码错误"
          }
        }
      }
    ],
    "log_paths": ["logs/xxx.run.log"]
  }
}
```

### 响应体

```json
{
  "status": "success",
  "result": {
    "case_name": "登录用例",
    "cause_type": "用例错误",
    "confidence": 0.88,
    "evidence": [
      "断言 body.code 期望 0，实际 1001",
      "响应 message 为 用户名或密码错误",
      "请求参数与文档中账号/密码设置一致"
    ],
    "reason": "该失败更像是测试数据/期望值不一致，而不是接口实现缺陷。",
    "suggestion": "检查测试账号是否失效，或用例期望值是否过期。"
  },
  "requires_human_confirmation": true
}
```

---

## 4.2 Prompt 模板：失败分析

### System Prompt

```text
你是 interfacetester 的失败分析助手。

任务：根据失败的断言、响应、日志和请求信息，判断失败归因。

必须遵守：
1. 只能在以下类别中选择：缺陷、用例错误、环境问题、数据问题、未知。
2. 每一条结论必须基于证据，不能空想。
3. 不能说“接口有问题”而没有请求/响应/日志依据。
4. 若证据不足，输出 cause_type = "未知"。
5. 不允许修改测试结论，AI 只是给出分析结果。
6. 输出必须是合法 JSON，不能有额外说明。

返回结构：
{
  "case_name": "...",
  "cause_type": "缺陷 | 用例错误 | 环境问题 | 数据问题 | 未知",
  "confidence": 0.0,
  "evidence": ["..."],
  "reason": "...",
  "suggestion": "..."
}
```

### User Prompt

```text
失败数据如下：

[失败记录 JSON]

请判断：
- 是接口缺陷？
- 是测试用例写错？
- 是环境问题？
- 是数据问题？
- 还是证据不足，待人工确认？

要求：
- 先给出证据列表
- 再给出原因和建议
- 若证据不足，输出 cause_type = "未知"
```

### 分析规则

- 优先判断“断言是不是写错”，如：
  - 期望值过旧
  - 取值路径写错
  - 使用了错误变量
  - 返回码正确但 JSON 结构不符
- 若返回值明显不符合文档规定，才归结为“缺陷”
- 若 `status_code` 与返回体都明显正常，但断言路径写错，则归类“用例错误”
- 若 `base_url`、token、数据库环境不就绪，则归类“环境问题”

---

## 5. 修正建议接口设计草案

## 5.1 接口定义

```http
POST /api/ai/suggest-fix
Content-Type: application/json
```

### 请求体

```json
{
  "task_type": "suggest_fix",
  "input": {
    "case_name": "登录用例",
    "step_name": "登录获取 token",
    "original_yaml": "...原始 YAML ...",
    "failure": {
      "assertion": {
        "comparator": "eq",
        "check": "body.data.username",
        "expect": "admin",
        "actual": null
      },
      "error": "JMESPath not found"
    }
  }
}
```

### 响应体

```json
{
  "status": "success",
  "result": {
    "case_name": "登录用例",
    "step_name": "登录获取 token",
    "before": "body.data.username",
    "after": "body.data.userName",
    "reason": "响应中字段名已从 username 改为 userName，路径需要同步修正。",
    "risk": "若真实接口未变更，此修改可能掩盖真实缺陷。",
    "requires_human_confirmation": true
  }
}
```

---

## 5.2 Prompt 模板：修正建议

### System Prompt

```text
你是 interfacetester 的用例修正助手。

任务：根据失败信息和原始用例，给出“最可能的修正建议”，但不能直接修改文件。

硬性规则：
1. 只输出 JSON，不要自然语言解释。
2. 输出结构必须包含 before / after / reason / risk / requires_human_confirmation。
3. 对于明显的真实缺陷，不能直接给“修正用例”建议，必须要求人工确认。
4. 对于字段名、路径、变量名等明显写错的问题，可以给出候选修正。
5. 若无法判断，返回 requires_human_confirmation = true，reason = "无法判断，需人工确认"。
6. 输出不能假定接口改了，必须同时给出风险说明。
```

### User Prompt

```text
原始 YAML 用例：

[原始用例 YAML]

失败信息：

[失败 JSON]

请给出最小修正建议：
- 修改什么字段/路径
- 为什么这么改
- 这个改动的风险是什么
- 是否必须人工确认
```

### 修正建议原则

- 只给“候选修正”，不直接落盘
- 对“真实缺陷”要保留风险提示
- 对“断言路径写错”可以给出明显的 `before -> after`
- 如果返回体结构不一致，则建议返回“待人工确认”而不直接覆盖

---

## 6. 接口统一校验设计

为避免 AI 输出脏数据，三类接口都应有统一层：

```python
class AIResponseSchema(BaseModel):
    request_id: str
    status: str
    result: dict
    warnings: List[str] = []
    errors: List[str] = []
    confidence: float = 0.0
    evidence: List[str] = []
    requires_human_confirmation: bool = True
```

### 统一校验规则

- `status` 只能是 `success | failed | needs_human_review`
- `result` 必须是对象
- `confidence` 必须是 0~1 的浮点数
- `evidence` 如果是分析任务，最好非空
- `requires_human_confirmation` 不能省略

---

## 7. 推荐的落地实现结构

```text
interfacetester_ai/
├── __init__.py
├── client.py
├── schemas.py
├── prompts.py
├── generate_case.py
├── analyze_failure.py
├── suggest_fix.py
├── validate.py
└── cache.py
```

### 主要模块职责

- `client.py`：统一访问模型
- `schemas.py`：请求/响应结构定义
- `prompts.py`：Prompt 模板管理
- `generate_case.py`：生成用例
- `analyze_failure.py`：失败分析
- `suggest_fix.py`：修正建议
- `validate.py`：AI 输出校验
- `cache.py`：缓存 prompt / response

---

## 8. 推荐的 prompt 版本管理方式

建议用版本化管理：

```python
PROMPT_VERSION = "v1.0"
```

并在每次调用时将：

- model
- prompt_version
- 输入 hash
- 时间戳
- 用户项目名

写进审计日志，保证每次 AI 调用可回溯。

---

## 9. 最后总结

这三类 Prompt 的设计核心是：

1. 生成用例：结构化 JSON + 规则约束 + source_quote
2. 失败分析：证据优先 + 分类别 + 不能空想
3. 修正建议：建议优先 + 人工确认 + 风险说明

它们的共同特点是：

- 都必须返回结构化结果
- 都必须遵循 schema 校验
- 都不能绕过 interfacetester 原本的真实执行/判定逻辑
- 都必须支持回滚和人工审核

这才是“AI 与 interfacetester 协同”的正确落地方式。
