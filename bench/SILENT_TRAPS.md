# 静默陷阱 → 代码闸门：完整清单（方案5 的依据）

本文件是《interfacetester智能化改造方案5.md》的证据底稿。
所有条目均在 interfacetester 5.0.1 上**实测复现**过（环境：D:\interface-tester\.venv，Python 3.12.10）。

分类：
- **G 类（Gate-able）**：可在**生成期**判定，能做成硬闸门 → 方案5 的 S 系列闸门
- **R 类（Runtime-residual）**：运行期语义上无法判定，只能告警 → 属于内核既有设计，闸门**不得**误伤
- **X 类（Cross-cutting）**：不是断言语义问题，而是"证据/产物"层面

---

## 一、G 类：可在生成期判定的静默陷阱

### G1. 伪存在性断言（空值真空）★最高危

**现象**：`not_equal: [x, ""]` 这类"字段存在且非空"的写法，在字段**根本不存在**时**通过**。

**实测**（comparator 级 + 端到端双层复现）：

| 算子 | check_value=None 时 | 结论 |
|---|---|---|
| `not_equal` | **PASS** | 静默假通过 |
| `jsonschema_match`（空 schema `{}`） | **PASS** | 静默假通过 |
| `contains`（expect 为 `""`） | **PASS** | 真空（vacuous truth） |
| `equal` / `contains` / `contained_by` / `type_match` / `length_equal` / `string_equals` / `startswith` / `endswith` / `greater_than` / `less_than` / `regex_match` / `xpath_count` | FAIL / TypeError | 已响亮 |

**端到端实测**（project-two 真实跑一次）：

```yaml
- not_equal: [body.data.nonexistent_field, ""]
```
→ `1 passed`；summary.json 记录
`check_result=pass, check_value=None, case success=True`

**源码依据**：`response.py::_warn_value_not_found` 的 docstring 明写
「`not_equal: ["body.data.access_token", ""]` 这类断言在字段根本不存在时**通过**」，
并解释**为什么只告警不报错**：jmespath 对「字段缺失」与「值为 null」都返回 `None`，
框架**无法区分**，硬判失败会破坏 `eq: [body.x, null]` 这类合法写法。

**为什么生成期可判定 / 运行期不可判定**：
运行期拿到的是 `None`，歧义已无法消除；
但生成期**看得到 YAML 原文**——`not_equal: [body.x, ""]` 的**意图**是"存在且非空"，
这在静态上是可以识别的（期望值为空串/空集合 且 算子对 None 不敏感 → 意图与实现不符）。
所以这是**唯一能消灭它的位置**。

**闸门判据 S1**：
- 期望值为「空值」（`""` / `[]` / `{}`）且算子 ∈ `{not_equal, contained_by}` → **拒绝**
- 算子 == `jsonschema_match` 且 schema 为「空真空」（`{}` 或等价） → **拒绝**
- 算子 == `contains` 且期望值为 `""` → **拒绝**
- 提示改法：存在性断言写 `type_match: [x, "str"]`

---

### G2. 数值断言两侧都是字符串（字典序比较）

**实测**：
```
greater_than('9','100')      -> PASS   <-- 字典序 '9'>'1'，数值 9<100
less_than('10','9')          -> PASS   <-- 字典序 '1'<'9'，数值 10>9
greater_or_equals('9','100') -> PASS
greater_than(9,100)          -> FAIL   (正确)
```

**源码依据**：`comparators.py::_warn_if_both_operands_are_strings` 明写
「反方向（一侧是 int）会抛 TypeError → 可读失败；也就是说**提示只覆盖了失败方向，通过方向静默**」，
且刻意**不做硬校验**（因为字典序对 ISO 日期串是合法用法）。

**为什么生成期可判定**：YAML 里期望值是否带引号是**静态可见**的。
若期望值写成字符串字面量（`"100"`）且算子 ∈ 4 个数值算子 → 生成期即可判定"这大概率是拼错"。

**闸门判据 S2**：算子 ∈ `{greater_than, less_than, greater_or_equals, less_or_equals}`
且 expect 是**字符串字面量**且形态像数字 → **拒绝**（提示去掉引号写数字），
但**放行**形态像日期/版本号的字符串（`2026-09-20` / `v1.2.3`），避免误伤合法用法。

---

### G3. 断言真空：整条用例没有任何有效断言

**现象**：用例"跑绿"但**什么都没校验**。

**源码依据**（多处）：
- `client.py`：「**没有断言的 step 还会被判成功**（最坏的一种"假通过"）」
- `har_adapter.py`：「`assertion_mode != "none"` 却**一条断言都没有** → 不能静默」
- `curl_adapter.py`：「用户拿到一堆"没有 validate"的用例，容易误以为已经校验过了」
- `make.py`：`teststeps` 为空只 `logger.warning`，`main_make` 仍 exit 0
- `make.py`：目录扫描里 `teststeps` 缺失 → 只 warning
- `docs/能力清单.md`：「残留边界：目录扫描里**解析失败**的 `.yml/.yaml` 只跳过、不判失败」

**闸门判据 S3**（生成期）：
- 每个 `teststeps[]` 必须有非空 `validate`（或显式声明 `validate: []` 是合法的"只跑不断言"）
- 整个用例**零断言** → **拒绝**
- `extract` 有键但 `validate` 为空 → **拒绝**（典型的"提取了却没校验"）

---

### G4. 引用降级链：`extract` 成功但值为 None，被后续步骤当有效值用

**源码依据**：`response.py`：「取值未命中（None）不能静默——**后续步骤会拿到 None 而毫无提示**」

**闸门判据 S4**：`extract` 的目标路径，若没有任何一条 `validate` 对它做过存在性/类型断言
→ **警告并要求确认**（因为后续 `$var` 会拿到 None）。
不硬拒（多步链路里确有权衡），标记为 `pending` 进人工清单。

---

### G5. 函数调用带引号 → 不被当函数执行，且只告警

**实测**：
```
${f(abc)}    findall=[('f','abc')]   quote_detect=False   正常
${f('abc')}  findall=[]              quote_detect=True    静默退化为字面量
${f("abc")}  findall=[]              quote_detect=True    同上
```

**源码依据**：`parser.py` 正则参数字符集 `[\$\w\.\-/\s=,]` **不含引号**，
注释明写「带引号的调用会被原样保留成字符串（**不报错**）」。

**闸门判据 S5**：扫描所有字符串值，匹配 `\$\{ident\([^}]*['\"]`
→ **拒绝**并给出改法（裸词或经 `$var` 传入）。

---

### G6. 伪类型断言 / 算子名拼错恰好撞内置名

**源码依据**：`parser.py` + `response.py`：
「名字撞上 `print` 这类可变参内置函数时旧实现会**静默通过**——
这是最难查的一类假通过（拼错的名字恰好等于某个内置名即可触发）」。
`make.py::ensure_known_comparators` + `response.validate(allow_builtins=False)` 已收口。

**闸门判据 S6**：复用 `make.py::ensure_known_comparators`，
在**生成期**对 LLM 产物跑一次（不得等运行期）。已由内核实现 → **闸门只做调用，不重造**。

---

### G7. `type_match` 的 expect 不是类型名

**源码依据**：`comparators.py::type_match` 的 `get_type()`：
修复前 `__builtins__[name]` 会把**任意内置名**当类型 →
`type_match: [body.x, eval]` 表面是"类型断言失败"，实际是用法错误。
现已收口为 `ParamsError`。

**闸门判据 S7**：`type_match` 的 expect ∈ `{int,str,list,dict,float,bool,None,"None","NoneType",...}`
白名单，其余 → **拒绝**。

---

### G8. 内联 JSON Schema 含 `$` 键

**源码依据**：`make.py::ensure_json_schema_not_inline`：
`parser.parse_data` 会解析 dict 的键，`$schema`/`$ref` → `VariableNotFound`。

**闸门判据 S8**：直接复用内核该函数（不重造）。

---

### G9. `upload` / `data` / `json` 三者并存 → 请求体静默丢弃

**源码依据**：`make.py`：
- 「`data` 与 `json` 不能同时给（requests 会**静默丢掉** json）」
- 「`upload` 与 `data`/`json` 并存同样是**静默丢体**」
- 表格明确：`upload` + `data`(dict) → 「`data` 整段**静默消失**」

**闸门判据 S9**：直接复用内核 `ensure_generatable_teststeps`（不重造）。

---

### G10. 归一化后撞同一个生成物 / 模块名

**源码依据**：`make.py`：
「两个不同的 YAML 归一化后落到同一个 `_test.py`。修复前这种碰撞是**静默**的」
+ `cli.py::main_convert` 三层写盘护栏（`.yml` 名 / schema 名 / **生成物模块名**）。

**闸门判据 S10**：`gen` 写盘前用 `normalize_module_segment` **预算模块名并查重**。

---

### G11. 文档编码（BOM/GBK/CRLF）→ 静默读挂

**源码依据**：`cli.py` M23：「BOM 会让第一行变成 `\ufeffcurl …`，于是**一条都解析不出来**」；
本仓历史取证目录有成对样例（`curl_bom.txt` / `curl_gbk.txt` / `har_gbk.json` / `openapi_bom.yaml`）。

**闸门判据 S11**：读文档复用导入器同一套探测顺序
`utf-8-sig → utf-8 → gbk → gb18030 → 兜底+明确报错`，探测结果记 manifest。

---

## 二、R 类：运行期无法判定（闸门**不得**误伤）

这些是内核**刻意**保留的告警行为，理由充分。方案5 的闸门必须**放行**它们，
否则会把"合法用法"打成"错误"，制造假报（本仓明确反对：假报会让真报被无视）。

| 编号 | 现象 | 为什么必须放行 |
|---|---|---|
| R1 | `eq: [body.x, null]` | 明确期望 null，合法 |
| R2 | `type_match: [body.x, "None"]` | 同上，`_assertion_expects_null` 已放过 |
| R3 | 数值算子比 ISO 日期串 `greater_than: [d, "2026-01-01"]` | 字典序恰好等于时间序，合法 |
| R4 | `string_equals: ["status_code", "200"]` | 数字被 str() 后比较，仓库与现场都在用 |
| R5 | `jsonschema_match` 用了 `date-time`/`uri` 等 format | 缺可选依赖，已告警；装 `jsonschema[format]` 可强制 |
| R6 | `extract` 取到 None 且后续确实容忍 | 多步链路的合理写法 |

**闸门设计的核心纪律**：
> **只拦「能精确判定的形态」**——这句是内核自己的口径（`comparators.py`：
> 「本仓的取向是「只拦能精确判定的形态」，假报会让真报被无视」）。
> 方案5 的 S1~S11 每一条都必须配**反例自检**（合法写法必须被放行）。

---

## 三、X 类：证据/产物层面

### X1. `analyze` 的证据指针路径错误

**实测**（dump 真实 summary.json）：
```
records[0] keys = ['name','step_type','success','data','elapsed','content_size','export_vars','attachment']
'validators' in rec          -> False
'validators' in rec['data']  -> True
```
方案4 §4.5 写的 `details[i].records[j].validators...` **缺 `.data.`**，照抄必错。
正确：`details[i].records[j].data.validators.validate_extractor[k]`。

### X2. "逐字节一致"与框架内建 UUID 冲突

`step_request.py` 给**每个请求**注入
`f"interfacetester-{runner.case_id}-{str(int(time.time()*1000))[-6:]}"`，
`case_id = uuid.uuid4()`；`.run.log` 文件名本身也是 UUID。
→ `summary.json` 与 `.run.log` **天然不可能逐字节一致**。
验收必须改为「**归一化投影后**逐字节一致」。

### X3. L2 通过 ≠ 产物可用

`format_pytest_with_black` 对 black 超时/失败**只告警不阻断**
（docstring：「超时/失败都只告警，不阻断 hmake」）。
→ "L2 通过率"含噪声，闸门要单独暴露"格式化未执行"。

### X4. `.ai/` 需显式进 `.gitignore`

立仓时的实测口径：`.gitignore` 覆盖 `.venv/ __pycache__/ *.whl .ai/ logs/ reports/ .tmp_*/ .env`，
而 `bench/` 是 **CI 护栏，必须入库**（`.github/workflows/test.yml` 与 `.gitlab-ci.yml` 的
guardrails job 直接调它）。
注：`.gitignore` 另外保留 `/project-three/`、`/selling-this-product/`、`/probe_*/` 三条规则作为
**二次保险** —— 这三类产物不随仓库分发，万一被拷进来也不会误提交。

---

## 四、闸门分层落位

| 闸门 | 拦截时机 | 失败动作 | 实现方式 |
|---|---|---|---|
| S1 伪存在性 | 生成期（装配后、写盘前） | 硬拒 + 给改法 | **新写** |
| S2 字典序 | 同上 | 硬拒（数字形态） | **新写** |
| S3 断言真空 | 同上 | 硬拒 | **新写** |
| S4 extract 未校验 | 同上 | pending 人工 | **新写** |
| S5 引号函数 | 同上 | 硬拒 + 改法 | **新写** |
| S6 算子白名单 | 同上 | 硬拒 | 复用 `ensure_known_comparators` |
| S7 type_match 名 | 同上 | 硬拒 | **新写** |
| S8 内联 schema | 同上 | 硬拒 | 复用 `ensure_json_schema_not_inline` |
| S9 请求体并存 | 同上 | 硬拒 | 复用 `ensure_generatable_teststeps` |
| S10 模块名撞车 | 写盘前 | 硬拒 | 复用 `normalize_module_segment` + 查重 |
| S11 文档编码 | 读文档时 | 明确报错 | 复用导入器探测顺序 |

**总原则**：S1~S5、S7 **新写**（内核没有，因为内核只在运行期看得到值，看不到意图）；
S6/S8/S9/S10/S11 **复用**内核既有护栏（不重造——方案4 §3.4 的纪律）。
