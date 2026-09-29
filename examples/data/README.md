# `examples/data/` 是什么？——**导入素材**样例 + 测试夹具

> **一句话**：这个目录里混着两类东西：
> **① 导入素材**（Postman / HAR / curl / OpenAPI 文件）——**四个源都能用 `hconvert` 直接导入**（P3-a/b/c）；
> **② 本仓库测试用的夹具与运行产物**（`a-b.c/`、`a_b_c/`、`debugtalk.py`、`logs/` 等）——
> 被 `tests/` 直接引用，**不要删改**。
>
> 导入能力的支持子集与路线图见 **`docs/convert/README.md`**。

---

## 一、导入素材（现状：curl / HAR / Postman / OpenAPI 都可一键导入）

| 文件 | 大小 | 内容 | 现状 |
| --- | --- | --- | --- |
| `curl/curl_examples.txt` | 750 B | 6 条 curl：裸域名、`?a=b&c=d`、`-H JSON + Authorization` 配 `-d '{json}'`、`-F file1=@file1.txt`（多文件上传）、`-d 'shipment[to_address][id]=...'`（嵌套方括号）、`--data "key1=value+1&key2=value%3A2"`（已编码 + 空格写成 `+`） | ✅ **可导入**：`hconvert --from curl --in examples/data/curl/curl_examples.txt --out converted/`（6 条全支持；它本身还被 `tests/make_test.py` 当作**上传文件**夹具使用） |
| `har/demo.har` | 15,955 B | HAR **1.2**，3 条 entries，host 均为 `postman-echo.com`，GET + POST（响应体是 base64） | ✅ **可导入**：`hconvert --from har --in examples/data/har/demo.har --out converted/`（自动生成状态码 + 响应**形状** schema 断言）；注意它直连公网，内网跑不通，导入后请把 `base_url` 改成内网地址 |
| `postman/postman_collection.json` | 11,637 B | Postman **Collection v2.1**；3 层 folder；`Get with params`（含路径变量 `:path` 与 2 个**保存的示例响应**）、`Post form-data`（含 `intro.txt`/`logo.jpeg` 文件字段）、`Post x-www-form-urlencoded`、`Post raw json`、`Post raw text`、`Get request headers`（含 disabled 头） | ✅ **可导入**：`hconvert --from postman --in examples/data/postman/postman_collection.json --out converted/` → **3 个用例文件 / 6 个步骤 / 4 条断言**（每个顶层 folder 一个文件）；`base_url` 同样指向公网，需按需替换 |
| `openapi/petstore_demo.yaml` | 5.5 KB | **本仓库自带的 OpenAPI 3.0.3 示例契约**（不是上游素材）：刻意覆盖 `$ref`/`allOf`/`oneOf`/`nullable`、`servers` 变量、`securitySchemes`（bearer + apiKey）、路径参数（有/无 example）、必填/可选 query、`requestBody`（有 example / 需造骨架 / 表单）、响应（200/201/204/4xx、无 content） | ✅ **可导入**：`hconvert --from openapi --in examples/data/openapi/petstore_demo.yaml --out converted/` → **2 个用例文件 / 6 个步骤 / 11 条断言**（每个 tag 一个文件）；Swagger 2.0 契约也能导入（先归一化） |
| `postman/intro.txt` | 212 B | 上游 README 文案 | ⚠️ **有误导性**，见下 |
| `postman/logo.jpeg` | 323 KB | Postman 素材图 | 与框架能力无关 |

### ⚠️ `postman/intro.txt` 里宣称的能力，本框架**不支持**

原文：`... supports HTTP(S)/HTTP2/WebSocket/RPC network protocols, covering API testing,
performance testing and digital experience monitoring (DEM) test types.`

这是**上游产品的宣传语**，不是本框架的能力清单。实际情况（依据 `docs/能力清单.md` 5.2）：

- **协议**：只做 **HTTP/HTTPS**（基于 `requests`）；**gRPC / WebSocket / MQTT / 私有 TCP 不支持**，
  Thrift 是**实验性且未做真实连通性验证**的扩展；
- **性能测试**：不做（无并发压测、无吞吐/时延统计口径），只有单用例级的响应耗时**断言**
  （检查项 `elapsed_ms`/`elapsed_s`）；
- **DEM**：不在范围内。

> 判断「能不能做某件事」，请以 `docs/能力清单.md` 为准，不要以本目录的素材文案为准。

---

## 二、本仓库测试夹具与运行产物（被 `tests/` 引用，**不要删除**）

| 路径 | 用途 / 引用方 |
| --- | --- |
| `a-b.c/1.yml`、`a-b.c/2 3.yml` | 目录名含 `.`/`-`、文件名含**空格**的路径夹具（`tests/{make,compat,cli}_test.py`）；两者的 `base_url` 都是 `https://postman-echo.com`（**跑它们需要外网**，且 `2 3.yml` 用 `testcase: a-b.c/1.yml` 演示用例引用） |
| `a-b.c/中文case.yml` | **0 字节**，只为覆盖中文文件名路径（`tests/make_test.py::convert_testcase_path`） |
| `a_b_c/__init__.py`、`T1_test.py`、`T2_3_test.py` | `hmake` 生成物的**期望结果**（`tests/cli_test.py` 断言其存在） |
| `debugtalk.py` | 该示例项目的函数库（`get_interfacetester_version` / `sum_two` / `get_variables`） |
| `profile.yml`、`profile_override.yml` | 多环境 profile 示例 |
| `sqlite.db` | SQL 扩展示例库（`RunSqlRequest`） |
| `.csv` | **0 字节**，文件名特殊字符夹具（`tests/make_test.py::ensure_file_abs_path_valid`） |
| `logs/` | **运行产物**（`hrapp`/`hrun` 的 `.run.log`），可安全清理 |
| `__pycache__/` | Python 字节码缓存，可安全清理 |

---

## 三、想用这些素材怎么办

1. **一键导入（curl / HAR / Postman / OpenAPI）**：`hconvert --from curl|har|postman|openapi --in <素材> --out converted/`，
   生成标准 YAML + （有响应样本/契约时）契约断言 schema；支持子集与注意事项见 `docs/convert/README.md`；
2. **手工迁移兜底**：照 `examples/postman_echo/`、`examples/httpbin/` 的写法把请求改写成 YAML——
   `request.method` / `url` / `params` / `headers` / `json` / `data` / `upload` 字段见
   `docs/能力清单.md` 的「请求字段全表」（导入器覆盖不到的写法，如外部 `$ref`、JS 脚本，仍需手工）；
3. **不要**在用例里长期依赖 `postman-echo.com`（公网）：内网 CI 会红，改打本地 mock 或自建服务
   （`tests/conftest.py` + `tests/mock_server.py` 是现成模板）——HAR/Postman 素材导入后请顺手把 `base_url` 换掉。
