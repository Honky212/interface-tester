# 导入器（`hconvert`）：curl / HAR / Postman / OpenAPI → 标准 YAML 用例

> **一句话**：把「复制为 cURL」、浏览器录制的 HAR、Postman 集合、以及 OpenAPI/Swagger 契约
> 变成可跑的 InterfaceTester 用例，生成的是**标准 YAML**（走现有 `hmake` 链路），对运行内核**零侵入**。

```bash
# curl：一个文件里可以放多条命令（续行 \ 与 # 注释都支持）
hconvert --from curl --in examples/data/curl/curl_examples.txt --out converted/

# HAR：浏览器 DevTools 导出（Network → 右键 → Save all as HAR）
hconvert --from har --in demo.har --out converted/ --report converted/import-report.md

# Postman：导出 Collection v2.1（默认每个顶层 folder 一个用例文件）
hconvert --from postman --in postman_collection.json --out converted/

# OpenAPI/Swagger：契约 → 骨架用例（默认每个 tag 一个用例文件）
hconvert --from openapi --in petstore_demo.yaml --out converted/
```

| 参数 | 说明 |
| --- | --- |
| `--from` | `curl` / `har` / `postman` / `openapi` |
| `--in` / `--out` | 输入文件 / 输出目录（默认 `./converted`） |
| `--name` | 用例名（同时决定 YAML 文件名） |
| `--assertions` | HAR / Postman / OpenAPI 专用：`status+schema`（默认）/ `status` / `none` |
| `--single-file` | Postman：整个集合合成一个用例（默认每个顶层 folder 一个）；OpenAPI：所有 tag 合成一个用例（默认每个 tag 一个） |
| `--include-tags` / `--exclude-tags` / `--methods` | OpenAPI 专用：按 tag / HTTP 方法筛选 |
| `--include-hosts` / `--exclude-hosts` | HAR 专用：按 host 子串过滤（先 include 再 exclude） |
| `--no-dedupe` | HAR 专用：保留完全重复的录制（默认去重） |
| `--no-validate` | 跳过**导出即验证**（默认执行） |
| `--report` | 额外输出一份 markdown 导入报告（做了什么、丢了什么、要人工补什么） |

**输出目录会长这样**（HAR 导入）：

```
converted/
├── debugtalk.py                 # 项目标记（自动创建，见下面「为什么」）
├── demo.yml                     # 用例
├── demo_test.py                 # hmake 产物（导出即验证时生成，后续 hrun 也会用它）
└── schemas/
    ├── demo_01_get_get.json     # 从录制响应推断的「形状」schema
    └── demo_02_post_post.json
```

Postman 导入（默认按顶层 folder 拆分）：

```
converted/
├── debugtalk.py
├── postman_collection.yml                    # 根级请求
├── postman_collection_folder1.yml            # folder1（含嵌套 folder2 的请求）
├── postman_collection_folder3.yml            # folder3
└── schemas/
    ├── postman_collection_folder1_01_get_with_params.json
    └── postman_collection_01_get_request_headers.json
```

---

## 一、三条设计原则（决定了它的边界）

1. **输出 YAML 而不是 Python** —— 复用 `hmake`，导入器不碰 `make.py`/`step_*.py`/`comparators.py`；
   想改用例就改 YAML，与手写用例完全同构。
2. **IR 字段与 `TRequest` 对齐** —— `KNOWN_REQUEST_FIELDS` 是唯一事实来源（有单测锁住），
   不会生成框架不认的字段（pydantic 会静默忽略未知字段，那种坑最难查）。
3. **导出即验证**：写完盘立刻 `load_testcase()` + 渲染干跑 + 语法检查 + 项目定位，
   任何一步不过就报错。**生成物一定能被 `hmake` 处理**，否则宁可不产出。

### 为什么输出目录会自动生成 `debugtalk.py`

框架用「**从用例路径向上找 `debugtalk.py`**」来确定项目根目录（RootDir），
而生成用例里的 JSON Schema 是**文件引用**（`schemas/xxx.json`），按 RootDir 解析。
没有这个文件，RootDir 会退化成当前工作目录，换个目录跑就报「schema 文件不存在」
（P3-a 实测踩到，这也是唯一一处「工具会主动新增文件」的地方）。

---

## 二、支持子集（**冻结**，超出即告警）

> **素材编码**（0918-8 / M23）：一律按 **UTF-8** 读，且**接受带 BOM** 的素材
> （PowerShell 5.1 的 `Set-Content -Encoding UTF8`、部分编辑器的"UTF-8"都会写 BOM）。
> 修复前 BOM 会让 curl 素材**一条命令都解析不出来**，且真实诊断被吞掉、只留一句
> 「请检查输入内容」——排查成本极高。
>
> **解析不出请求时的诊断**（同批）：转换器攒下的告警（"第 N 行的内容不是 curl 命令，已跳过：…"、
> "过滤后没有任何可导入的请求：请检查 --include-hosts…"）现在**一定会打出来**。

### 形态护栏：坏素材不该让**整批**崩（批次 E / L1 + L2）

四个源都把「素材形态与规范不符」当成**字段级**问题处理，口径与 HAR 侧早已建立的规则一致：
**坏字段/坏条目只影响它自己那一层 —— 跳过并逐条告警，其余字段照常导入**。

| 素材里写成了什么 | 修复前 | 现在 |
| --- | --- | --- |
| 某个「对象列表」字段写成**对象**（漏了 `-`）：Postman `url.variable`/`body.formdata`、OpenAPI `parameters`/`servers`/`security` | OpenAPI `parameters` 会**静默丢掉全部参数**（含 `required`）；`servers`/`security` 抛裸 `KeyError: 0`；Postman 抛裸 `AttributeError` | 告警里**直接写明「漏了列表符号 `-`」+ 正确写法**，并跳过这一段（其余字段照常导入） |
| 列表里混进**非对象条目**（如 `header: ["X: 1"]`） | 裸 `AttributeError: 'str' object has no attribute 'get'` | 跳过该条目 + 报出**下标**，其余条目照常导入 |
| HAR 文档根 / `log` / `log.entries` 形态不对；OpenAPI `info`/`paths` 形态不对 | 裸 `AttributeError` / `KeyError` | 点名报错（HAR 那条还会提示 `entries` 必须挂在 `log` 下面） |
| `security: [bearerAuth]`（漏了每项的 `{}`） | **逐字符**迭代 → 认证头一个都不生成、**零告警**（静默无认证） | 点名告警并跳过认证（提示正确写法 `- bearerAuth: []`） |
| curl 里 `-d` 与 `-F` **同时出现** | `-d` 的内容被 `-F` 静默覆盖（真实 `curl.exe` 对该组合直接 exit 2） | 点名告警 + **把被丢弃的内容打出来**；行为上按 `-F` 处理（不跟着 curl 失败：多行素材里混进一条坏命令不该让整批失败） |
| `allOf` **自引用**（`allOf: [{$ref: 自己}]`） | `RecursionError`（普通 `$ref` 循环有深度兜底，`allOf` 递归没有） | 到 `MAX_SCHEMA_DEPTH` 就**停止下钻并告警**（与「截断必须可见」同口径） |

**写盘路径的两种「静默」也一并收口**（批次 E / L6 + L7，`hconvert`）：

- `--out` 指到**已存在的文件** → `exit 2` + 点名（修复前裸 `FileExistsError`）；
  `--report` 指到**目录** → 点名报错（**用例文件已经生成成功、仍然保留**）；
  用例名超长等写盘 `OSError` 同样收口成可读报错。
- **跨两次运行**落到同一个 `.yml`：内容**逐字相同**（同源重跑）静默；**内容不同**则点名告警
  （含上次修改时间与「换 `--out` / `--single-file` / 先备份」三条改法）。运行结束时还会报告
  输出目录里**上一次留下的、本次没人引用**的 `schemas/**` 文件（**只报告，不删除**）。

### curl

| curl 写法 | 处理 |
| --- | --- |
| `curl <url>` | 一条步骤；裸域名自动补 `http://`（与 curl 一致）并告警 |
| `-X/--request` | → `method` |
| `-H/--header`（含 `"K;"` 空值头） | → `headers`；敏感头占位化 |
| `-d/--data/--data-raw/--data-binary/--data-ascii` | → `data`（**保留原始编码**，多个 `-d` 用 `&` 连接）；Content-Type 是 JSON 且能解析时转成结构化的 `json` |
| `--data-urlencode` | 同 `-d`，**不做二次编码** |
| `-F/--form`（`k=v`、`k=@file`、`k=<file`） | 普通字段与文件**都进 `upload`**（导出时 `data` 会被并入 `upload`，见下方「multipart 的 `data` 语义」）；`;type=` / `;filename=` 等附加参数无法表达 → **剥离并告警**（0920 批次 3 / N18：修复前条件是 `not remainder.startswith("@")`，**值是文件时恰恰不剥**，`-F 'file=@./a.jpg;type=image/jpeg'` 会把 `;type=` 留在**文件路径**里、运行期找不到文件且导入期零告警 —— 而 Postman/Insomnia 的「Copy as cURL」导出的正是这个形态）。文件路径**成对的首尾引号**（`@"/tmp/x.png"`）同样会被剥掉。**`upload` 里是本地文件路径，一律原样保留、不做占位**（0918-8 / M22：文件名不是凭据；修复前 `private_key=@/path/key.pem` 会被占位成 `${BODY_PRIVATE_KEY}`，生成的用例运行期必然失败） |
| `-G/--get` | `-d` 的内容并到 URL 查询串 |
| `-u/--user` | → `Authorization: Basic ${AUTH_BASIC}`（**占位，不落明文**） |
| URL 里带 userinfo（`https://user:pass@host/x`） | 与 `-u/--user` **同义**（0918-8 / H10）：userinfo 从 URL 剥掉、`base_url` 变干净，凭据转成 `Authorization: Basic ${AUTH_BASIC}` 占位 + 告警。**修复前明文落在 `config.base_url` 里**（四源都中，且零告警） |
| `-b/--cookie` | → `cookies`（值全部占位化）。**curl 双语义**（0920 批次 7 / N44）：含 `=` 当内联串，**不含 `=` 当 cookie 文件**（Netscape 格式）—— 文件形态导入器**不读文件**（避免把宿主机状态写进产物），而是**响亮告警**并给出改法；修复前一律按串解析，文件形态解析出空 dict、**cookie 全部消失且零告警** |
| `-k/--insecure` | → `request.verify: false` |
| `-x/--proxy` | → `request.proxies`（http/https 同指） |
| `-A/--user-agent`、`-e/--referer` | → 对应请求头 |
| 续行 `\`、`#` 注释、一文件多命令、单/双引号、Windows 双引号转义 | 支持 |
| `-o/--output`、`-T/--upload-file`、`--cert/--key/--cacert`、`--http2`、`-w`、`--retry`、`-z` | **不支持 → 告警并给出替代写法**（如 `--cert` → `request.cert`） |
| 其它未知参数 | 告警「无法识别，已忽略（请人工确认）」 |

### HAR（1.2）

| HAR 字段 | 处理 |
| --- | --- |
| `request.method/url` | origin → `config.base_url`，path → 步骤 `url`；host 不同则保留绝对 URL 并告警 |
| `request.queryString[]` | → `params`；**有重名键时退回把查询串留在 URL 里**（字典装不下重名） |
| `request.headers[]` | → `headers`；HTTP/2 伪头（`:authority` 等）与**派生头**（`Host`/`Content-Length`/`Connection`/`Cookie`/`Accept-Encoding`/`Sec-Fetch-*`）**丢弃**并告警 |
| `request.cookies[]` | → `cookies`（值全部占位化） |
| `request.postData` | JSON → `json`；`x-www-form-urlencoded` → `data`；`multipart/form-data` → 字段与文件**都进 `upload`**（导出时 `data` 会被并入 `upload`，见下方「multipart 的 `data` 语义」）；其它 → 原样 `data` |
| `response.status` | → `eq: [status_code, N]`（忠实回放录到的状态码）；**3xx 是重定向中间态** → 跳过该条目并告警（0920 / 缺陷 3） |
| `response.content.text`（**含 base64**） | 解码后若是 JSON → 生成**形状 schema** + `jsonschema_match`（走文件引用）。**`content.compression` 会先解压**（0920 批次 7 / N42）：HAR 规范里它是数字（"压缩了多少字节"，社区约定**非 0 即 gzip**），Chrome/Firefox 常见组合就是 base64 + 压缩；导出工具也可能写算法名（`"gzip"`/`"deflate"`/`"br"`）。修复前完全不看这个字段 → gzip 字节按 UTF-8 解失败，被当成"二进制"**静默丢掉形状断言**，告警还说成"base64 二进制/非 UTF-8"（把用户引向错误结论） |
| 静态资源/`_resourceType`/`OPTIONS`/`status == 0` | 过滤（会汇总告警说明过滤了多少、为什么）。**`_resourceType` 不是 HAR 规范字段**（Chrome 的扩展），各工具词汇不同（0920 批次 7 / N43）：Chrome/Firefox 用 `stylesheet`/`script`/`image`…，Fiddler 首字母大写，**Charles Proxy 按扩展名给**（`css`/`js`/`png`…）—— 修复前只认 Chrome 那 8 个词，Charles 录制的 CSS/JS/图片**全部漏进用例**。⚠️ 刻意**不**过滤 `json`/`xml`/`text`/`document`/`xhr`/`fetch`（那是**接口请求**或接口的响应类型） |
| 重复录制（方法+URL+请求体相同） | 默认去重（`--no-dedupe` 保留）。**NOTICE（0920 批次 4 / N20）**：去重键覆盖 `postData.text` **与** `postData.params`（字段名/值/fileName，按顺序）。修复前只看 `text`，而表单/multipart 的录制**常常只有 `params`、没有 `text`** → 键退化成 `(method, url, "")`，两条**内容完全不同**的请求被判重复，第二条**整条丢掉**，告警还写着"完全相同"（把用户引向错误结论） |

### Postman（Collection v2.1）

| Postman | 处理 |
| --- | --- |
| `info.name` / `info.schema` | 用例名；schema 不是 **v2.1** 时告警（仍按 v2.1 解析） |
| `item[]`（嵌套 folder） | **默认每个顶层 folder 一个用例文件**（folder → `config.name`），根级请求单独一个；`--single-file` 合成一个用例（步骤名带完整 folder 路径） |
| `request.url`（字符串或对象） | 对象形式按 `protocol`+`host`+**`port`**+`path`+`query`+`variable` 重建；**路径变量 `:path` 用 `url.variable` 求值**（求不到则保留原样并告警）；`disabled` 的 query 跳过。**只给 `raw`（第三方导出常见）时按 `raw` 回填 host/path**（0918-8 / M24：修复前会拼成 `https:///?page=2`，host 与 path 双双丢失且只给一句无信息量的告警） |
| `request.header[]` | `disabled: true` 跳过；派生头（Host/Content-Length/Connection/Accept-Encoding/Sec-Fetch-*）丢弃。**`Cookie` 例外**：它是 Postman 里 cookie 的**唯一载体**，会解析进 `request.cookies`（0918-8 / M33：修复前当派生头丢掉且没有兜底 → cookie 静默消失，生成的用例发的是另一个请求）；与 `request.cookie` 数组**合并**，冲突时采用请求头的值并告警 |
| `request.body.mode` | `raw`（`options.raw.language=json` **或 `Content-Type: application/json`** 且能解析 → `json`，否则 `data`；XML 文本会提示 SOAP 写法）、`urlencoded` → `data`、`formdata`（text 与 **file 都进 `upload`**，导出时 `data` 会被并入 `upload`）、`file`/`graphql` → 告警跳过。**NOTICE（0918-8 / H12）**：`options.raw.language` 是 Postman 的可选提示，导出的集合里经常缺失或写成 `text`；只看它会把 JSON 体当原始字符串放进 `data`，而 `sanitize_body` 只认 dict/list → 体里的 `password`/`token` **明文写进 YAML**。现在以 `Content-Type` 为准（与 curl/HAR 两个源口径一致），并告警说明 |
| `request.auth` / `collection.auth` | `bearer` → `Bearer ${AUTH_TOKEN}`、`basic` → `Basic ${AUTH_BASIC}`、`apikey` → 头占位（`in: query` 会提示手工加到 params）、`oauth2` → 告警并指向内置 `config.oauth2`、其它 → 告警忽略 |
| `item.response[]`（保存的示例响应） | 取第一条 → `eq [status_code, code]` + 响应**形状** `jsonschema_match`；**多个示例会告警**（只按第一条生成） |
| `item.event[]`（pre-request / test 脚本） | **无法无损迁移** → 告警 + 附脚本首行，便于人工改写成 `debugtalk.py` 函数 / hooks |
| `variable[]`（collection / **folder** / item 级） | → **不重名时**统一进 `config.variables`（**越具体越优先**：collection → 外层 folder → 内层 folder → item）；**跨 folder/item 重名时**按作用域写进**步骤级 `variables:`**（0919-31 / L25：运行期优先级 `step > extract > config`，实测同一文件里同名变量各步各值；`config.variables` 里保留兜底值）。名字像凭据的默认值改写成 `${ENV(...)}`（**config 与步骤级两处都改写** —— 步骤级变量同样是明文可见的位置）。**变量的「值」里嵌套的 `{{...}}` 同样翻译**（0919-28 / L6：修复前原样搬运，生成 `apiRoot: '{{protocol}}://{{host}}/v1'` —— 运行期三种后果：`base url missed!` / `InvalidURL` / **`NameResolutionError` 被吞成假绿**）。⚠️ `variable[]` 里**形态不对**的条目会跳过并告警，不再让整份集合导入失败（0919-29 / L24）。<br>**批次 A / H4 + H5**：① **容器型的值**（`value` 是对象/数组）内部现在也按字段名脱敏 —— 修复前只按**变量名**判定、只处理标量，于是 `{key: "loginPayload", value: {password: "明文"}}` 这种「变量名不像凭据、值里却有 `password`」的形态**明文进 YAML**（而同一份字典当**请求体**时会被正确占位，属漏用既有机制）；② 剪枝时**必须连变量的值一起扫** —— 「只被另一个变量的值引用」的变量（`baseHost` 只出现在 `apiRoot = {{baseHost}}/v1` 里）修复前会被整条删掉，生成物**必然**跑不起来（`VariableNotFound: ['baseHost']`），而 `hconvert` 还打印「导出即验证通过」exit 0（`validate_emitted_case` 按设计不解析变量，抓不到）。步骤级变量也是 `${...}` 的载体，同样纳入扫描。<br>**0920 批次 4 / N24**：**非 ASCII 变量名**（`{{域名}}`）现在会被转义成合法标识符（`_u57df_u540d`）。修复前 `_to_python_placeholder` 刻意保留 CJK，于是生成 `${域名}` —— 而运行期变量正则只认 `[A-Za-z_][0-9A-Za-z_]*`，这个引用**根本不会被解析**（原样发给服务端）；同时剪枝用的扫描正则也只认 ASCII → 变量被判"没被引用"、**连定义都被删掉**。两条叠加 = 生成物必然跑不通而导入期零告警（ASCII 名字的对照组完全正常） |
| `{{var}}` | → `${var}`（**只翻译写法，不内联值**，值统一放 `config.variables`）；集合里没定义的 → `${ENV(var)}` + 告警。**引用未定义名字**（含变量值里引用的）都给告警，不静默 |
| `{{baseUrl}}`/`{{host}}` 等**只出现在 host** 的变量 | **同样登记进 `config.variables`**（0919-23 / H6）。修复前变量收集只扫 steps、**不扫 `base_url`** → 变量被过滤掉，生成物是 `base_url: https://${baseUrl}` 而 `variables` 段整个消失，运行期 `VariableNotFound`（按告警提示设了环境变量也无效） |
| `{{$guid}}` / `{{$randomUUID}}` / `{{$timestamp}}` / `{{$randomInt}}` | → `${uuid4_str()}` / `${uuid4_str()}` / `${get_timestamp(10)}`（框架内置，**10 位秒级**——Postman 的 `{{$timestamp}}` 就是秒）/ `${random_int()}` |
| 其它 `{{$randomXxx}}` | 告警 + 占位 `${PM_XXX}`（默认从环境变量读），**不静默产出跑不通的用例** |

#### Postman 动态变量需要的 helper

`{{$guid}}` / `{{$randomInt}}` 会映射到下面两个函数，`hconvert` 生成的 `debugtalk.py` 里**已经带上了**：

```python
import random
import uuid


def uuid4_str():
    return str(uuid.uuid4())


def random_int(start=1, end=1000):
    return random.randint(int(start), int(end))
```

NOTICE: 若输出目录**已存在** `debugtalk.py`（例如先导过 curl 用例），导入器**不会改写它** ——
只会在检测到缺少这两个函数时打印告警 + 上面这段代码，由你决定加不加。

### OpenAPI 3.x / Swagger 2.0（契约 → 骨架用例）

| 契约里的东西 | 处理 |
| --- | --- |
| `openapi: 3.x` | 按下面的规则解析 |
| `swagger: "2.0"` | **先归一化成 3.0 形状**：`schemes`+`host`+`basePath` → server、`definitions` → `components.schemas`、`parameters(in: body)` → `requestBody`、`parameters(in: formData)` → `requestBody`（`type: file` → `string/binary`）、`responses[].schema` → `content`、`securityDefinitions` → `securitySchemes`、**`$ref` 前缀一并重写**（`#/definitions/X` → `#/components/schemas/X`）；未覆盖字段告警 |
| `parameters` 用 **`$ref` 引用**（`{"$ref": "#/parameters/UserBody"}`，顶层复用段落） | **同样能转成 `requestBody`**（0919-23 / H7）。修复前按 `parameter.get("in")` 分流时 `$ref` **尚未解析**（`in` 为空）→ body 参数被当成"其它参数"，**整个请求体静默丢掉**（生成物只有 method/url，零告警）；同一个契约内联写 `in: body` 却正常。修法涉及**时序**：`$ref` 前缀重写必须早于分流 |
| `servers[].url` + `variables[].default` | → `config.base_url`（变量用 default 代入）；多个 servers 取第一个并告警；缺 default 时保留 `{var}` 并告警 |
| `$ref`（本地 `#/...`） | **解析并内联**（生成的 schema 文件是独立的，指不回原契约）；**外部文件 `$ref` 不支持** → 告警 |
| `allOf` | 合并（属性并集 + `required` 并集） |
| `oneOf` / `anyOf` | 保留（JSON Schema 原生支持） |
| `nullable: true`（3.0 写法，**不是** JSON Schema 关键字） | 转成 `type: [T, "null"]`，否则断言会比契约更严 |
| `security` + `securitySchemes` | `http/bearer` → `Bearer ${AUTH_TOKEN}`；`http/basic` → `Basic ${AUTH_BASIC}`；`apiKey` → 头占位（在 query/cookie 时**提示手工加到 params/cookies**）；`oauth2`/`openIdConnect` → 告警并指向内置 `config.oauth2` |
| `parameters`（path/query/header/cookie） | `required` 的必带；可选的**仅当有 example/default/enum** 时带上，其余汇总告警；取值优先级 `example` → `default` → `enum[0]` → **按类型造值**（并告警）。**查询参数按契约的 `style`/`explode` 序列化**（0920 批次 3 / N19）：`form+explode=false` → `ids=1,2,3`；`deepObject` → `filter[a]=1`；`pipeDelimited` → `x|y`。修复前 `style`/`explode` 完全不看，例值原样塞进 `params`，于是**契约说 A、请求发的是 B**（`ids=1,2,3` 实发成 `ids=1&ids=2&ids=3`、deepObject 的值被吞掉）。`form+explode=true` 的数组/对象语义是**重复键/平铺**，`params` 字典表达不了 → **降级并告警**（写明契约的真实语义与改法） |
| 路径参数 `{petId}` | 同上；值放进 `config.variables`（如 `petId: pet-001`），URL 写 `/pets/${petId}`，换真实 id 只改一处；同名冲突时自动加后缀 |
| `requestBody` | `application/json`：有 `example`/`examples` 直接用；否则按 schema **造骨架**并告警。`x-www-form-urlencoded` → `data`；`multipart/form-data` → 普通字段与 `format: binary` 字段**都进 `upload`**（导出时 `data` 会被并入 `upload`）；其它 content-type → 告警跳过。**schema 写成 `$ref` 时同样解析后再造值**（0918-8 / M20：修复前每个字段都退化成字面量 `"string"`、`example`/`default` 全丢，且 `binary` 判不出来 → multipart 变成 urlencoded、`upload` 段消失） |
| `responses` | 取**最小的 2xx**（无 2xx 时取最小数字码并告警）→ `eq [status_code, N]`；有 JSON content → 生成**契约断言 schema**（文件引用 + `jsonschema_match`）；无 content（如 204）/ 非 JSON → 只断状态码并告警 |
| `deprecated: true` | 仍然导入并告警 |

**为什么契约断言也用 `jsonschema_match` 而不是逐字段断言**：契约描述的是**结构**（类型/必填/枚举），
逐字段生成 `eq` 会把示例值固化成期望；用 JSON Schema 断言既贴契约又不容易假失败。

> 契约断言 schema 会被写到 `schemas/<case>_<NN>_<step>.json`（**真 JSON**，按扩展名解析），
> 其中 `$ref` 已内联、`nullable` 已转换 —— 单测用黄金文件锁住了这两件事，防止静默变化。

> **NOTICE（0919-23 / H5）：`<case>` 词干会保留非 ASCII**（中文 folder / tag 原样进文件名，
> 例如 `schemas/col_用户管理_01_list.json`），因为用例自身的 `.yml` 文件名一直是中文。
> 修复前词干用 `[^0-9A-Za-z]+` 归一，**中文被整段抹掉** → 两个中文 folder 的用例落到
> **同一个 schema 文件**上互相覆盖，先写的那个用例会断到**别人的**响应形状。
> 两个不同用例名若仍归一到同一词干，`hconvert` 会在写盘前**响亮报错并 exit 1**
> （而不是静默覆盖）—— 修法提示就在报错里。

> **NOTICE（批次 9-1 / H5 收口）：`<step>` 词干同样保留非 ASCII**（与 `<case>` 用同一个
> `ir.safe_file_stem`）。修复前 H5 只改了 `<case>`，HAR 与 OpenAPI 的**步骤**词干仍是
> 老写法 `re.sub(r"[^0-9A-Za-z]+", "_", step_name)`：
>
> ```text
> "GET /pets — 查询宠物列表"  ->  "get_pets"        （中文整段消失，只剩序号能区分）
> "GET /pets — 查询宠物详情"  ->  "get_pets"        ← 两个不同步骤的词干一模一样
> ```
>
> 后果**不是**覆盖（文件名里还有 `_<NN>` 序号，所以不会互相踩），而是
> **文件名读不出业务含义**，且同一子系统里 Postman 用一套规则、HAR/OpenAPI 用另一套。
> 现在三处统一：`schemas/pets_01_get_pets_查询宠物列表.json`。
> 纯 ASCII 的步骤名结果与修复前**逐字节一致**，所以黄金文件只在中英文混排的步骤名上变化
> （`openapi_petstore.yml` 已按新规则更新）。

### multipart 的 `data` 语义（0920 / 缺陷 1）

四个适配器在 multipart 步骤上都会写 `data`（普通字段）+ `upload`（文件）；而运行期
`ext/uploader.prepare_upload_step()` 会把 `step.request.data` **整个替换成** multipart 编码器。
两者叠加的后果是 **`data` 被静默丢弃**：

```text
# 导入：-F 'note=hello' -F 'file=@a.txt'
data: {note: hello}          # 用户写的普通字段
upload: {file: a.txt}
# 实际发出：multipart 体里**只有 file** —— note 从未出现，且 hmake / 运行期零告警
```

现在的口径（**导出时归一，运行期兜底**）：

| 层 | 行为 |
| --- | --- |
| 导出（`emit_yaml.build_request_dict`） | `data` 是 **dict** → 并进 `upload`（普通字段与文件都由 uploader 组 multipart）；`data` 是**字符串** + `upload` → **报错**（无法等价表达） |
| 生成期（`make.ensure_upload_body_is_unambiguous`） | 手写 YAML 里 `upload` 与 `data`/`json` **任一并存** → 报错（`data` 会被覆盖、`json` 会被 requests 丢弃） |
| 运行期（`prepare_upload_step`） | `json` 非空 → 报错；`data` 非空 → **告警**（说明会被覆盖、怎么改） |

所以生成的 YAML 里，multipart 步骤**只有 `upload:`**，普通字段与文件都在里面 ——
「`data` 不会被单独发出」这一点在生成物上是**自明的**。

---

## 三、断言策略：为什么是「状态码 + 形状」而不是逐字段

录制/示例响应是**某一次真实结果**：里面的 `id`、时间戳、trace id 都会变。
把具体值写成期望 = 把随机数据固化成断言 → **必然假失败**。所以默认只断：

1. **状态码**（录到 200 就断 200；Postman 用**保存的示例响应**的 `code`）；
2. **响应形状**：由响应推断出的 JSON Schema，只含 `type` + `required`，
   数组只按第一个元素推断，**服务端生成的头（`headers` 子树）只断类型不断必填**；
   schema 以**文件引用**方式断言（P2-b 的硬约定：内联 schema 会被 `parse_data` 当变量解析）。

想加强断言：在生成的 YAML 上补关键业务字段（`eq`/`contains`/`regex_match`…），
或在 debugtalk.py 里写业务语义算子。

**两个「生成不出断言」的场景（0917-1 起都会明确告警，不再静默）**：

| 场景 | 行为 | 告警内容 |
| --- | --- | --- |
| **响应不是 JSON**（XML/HTML/纯文本；SOAP 老接口尤其常见） | 只生成状态码断言（不擅自造内容断言） | 「响应体不是 JSON…已**只断状态码**、没有生成形状断言」+ 建议：XML/SOAP 用 `xpath_match`/`xpath_count`/`soap_fault`（`docs/soap/README.md`），纯文本用 `contains: ["text", "…"]` |
| **curl 源** | **一条断言都不生成**（curl 命令里没有响应体，生成物里没有 `validate` 段 → 跑起来必然「通过」） | 「curl 命令里没有响应内容 → 本用例只生成了**请求**、没有生成任何断言」+ 建议改用能录到响应的 HAR/Postman 源 |

> 这两条此前是**静默**的：用户拿到「迁移完成」的用例，实际等于没校验（0917-1 修复）。

---

## 四、合规：敏感信息**默认占位，绝不落明文**

| 位置 | 处理 |
| --- | --- |
| `Authorization`（Bearer / Basic / 其它） | 保留 scheme，值换成 `${AUTH_TOKEN}` / `${AUTH_BASIC}` / `${HEADER_*}` |
| 其它敏感头（`Cookie`、`X-Api-Key`、`X-Csrf-Token`…） | 整值换成 `${HEADER_*}` |
| Cookie | 按名字逐个换成 `${COOKIE_*}` |
| JSON / 表单体里字段名像凭据的（`password`/`token`/`sign`/`secret`/`csrf`…） | 值换成 `${BODY_*}` |

占位符的默认值统一写在 `config.variables` 里，形如 `${ENV(AUTH_TOKEN)}` ——
**所以生成的用例只要设了环境变量就能直接跑，而不需要把凭据写回 YAML**。
单测里有一条断言锁死这件事：素材里的明文 token **绝不出现在生成物中**。

---

## 五、导入之后一定要人工过一遍的点（工具只提示、不改写）

1. **动态值**：时间戳 / UUID / 长随机串 / ISO 时间会被**识别并告警**，但不会自动改写
   （自动改写会让用例失去原本的语义）。建议改成 `extract` 出来的变量或 `${func()}`。
2. **路径参数**：录到的是那一次的具体 id，通常要改成变量。
3. **断言的强弱**：形状断言只保证结构，业务正确性要自己补。
4. **录制里的一次性操作**：创建/删除类接口重放会改变环境数据（配合 `docs/../examples/data_management/` 的清理约定）。
5. **素材里的 `${...}`（0918-9 / M9，**重要**）**：素材里任何位置的 `${...}` 都会被**原样**
   写进生成的 YAML，并在运行期被**当作表达式执行**（`${func()}` 会真的调用函数）。
   导入器现在会为此**告警**，但**不擅自改写**——因为 `$$` 转义只挡得住外层的 `$`，
   里面的 `$变量` 照样会被替换（实测 `$${eval($p)}` → `${eval(PAYLOAD)}`，语义已经坏了）。
   **判据**：如果你只从可信来源（自己团队的 Postman 集合 / 自己的浏览器录制）导入，
   这条告警通常是"有意引用的框架变量"；如果素材来自外部（第三方供应商、issue 附件、
   网上下载的集合），**先把 `${...}` 逐条看清楚再运行**。
   运行期那一侧另有兜底：`${...}` 只能调用内置函数**白名单**（`eval`/`exec`/`open`/
   `__import__`/`getattr`/`input` 等一律拒绝并报错），详见 `docs/能力清单.md`「变量与函数」。

---

## 六、测试与质量保障

| 手段 | 位置 |
| --- | --- |
| 适配器单测（脏写法逐条）+ 黄金文件快照 | `tests/converters_test.py`（curl/HAR）+ `tests/postman_converter_test.py` + `tests/openapi_converter_test.py` |
| **跨源语义对照**（同一请求 → 三种源 → 产物必须语义等价） | `tests/converters_cross_source_test.py`（0918-8 §15.3）：JSON / 表单 / multipart 三种体、查询串、头、cookie、断言差异逐项对照；夹具自带**非空洞自检**（故意降级一个源，等价判据必须看得出差异）。它当场抓到了 **H12**（Postman JSON 体明文入库）与 **M33**（Postman cookie 静默丢失） |
| 黄金文件 | `tests/golden/converters/`（`UPDATE_GOLDEN=1` 重跑可更新） |
| **素材里的表达式护栏**（任何源都不允许产出"可执行的东西"） | `tests/converters_cross_source_test.py::TestBatch0918_9SourceExpressionGuard`（四源都要求：导入期告警 + 运行期拒绝执行）+ `tests/cli_test.py::TestBatch0918_9ImportedExpressionRefused`（真实 `hconvert`/`hrun`：报错、退出码非 0、payload 未被执行；并附**非空洞自检**——打开逃生口后同一份用例必须真的把 marker 写出来） |
| **端到端**：curl / HAR / Postman / OpenAPI → 生成 → `main_run` 跑通（本地 mock，离线） | 三个测试文件里的 `TestConvertCli` |
| 导出即验证 | 每次 `hconvert` 默认执行（`--no-validate` 可跳过） |

---

## 七、路线图

| 源 | 状态 | 说明 |
| --- | --- | --- |
| curl | ✅ P3-a | 见上 |
| HAR | ✅ P3-a | 见上 |
| Postman（Collection v2.1） | ✅ P3-b | 见上 |
| **OpenAPI 3.x / Swagger 2.0** | ✅ P3-c | 见上（契约 → 骨架用例） |

四个源已全部交付（P3 完成）。新增一个源的成本 ≈ 1~2 天（写一个 adapter 产出 IR）+ 复用 `emit_yaml`（已就绪）。

后续可选方向（未做，按需立项）：

- Postman 环境文件（`*.postman_environment.json`）导入、`item.request.description` → YAML 注释、GraphQL body、`pm.*` 脚本的半自动改写建议；
- OpenAPI：外部 `$ref`（多文件契约）解析、`x-*` 扩展语义（如 `x-example`）、安全方案多选时的交互式选择；
- 通用：`--dry-run` 预览模式、导入后的「生成用例数/断言数」统计进 CI 报告。
