# UniFSP（统一文件服务平台）迁移样例

当前 `examples/unifsp/` 完整清单
├── debugtalk.py          # 4 个原函数 + 9 个签名/加密工具函数
├── debugtalk.py.bak      # 原始 debugtalk.py 备份
├── conftest.py           # 多环境切换（--env）+ 环境信息打印
├── .env / .env.example
├── auth_login.yml        # 登录
├── directory_create.yml  # 创建目录
├── file_upload.yml       # 上传文件
├── file_delete.yml       # 删除文件
├── md5_sign_demo.yml     # 签名用法示例（本次新增）
├── test_files/测试文件.txt
├── README.md             # 已更新（多环境切换 + 签名示例）
└── *_test.py             # hmake 自动生成的 pytest 用例

把 `unifsp_api_test` 项目里的典型用例翻译成 interfacetester YAML 用例，演示二次开发后的三个框架能力：

- **OAuth2 自动认证**：`config.oauth2` 配置后，所有请求自动携带 `Authorization: Bearer`（token 缓存 + 自动刷新）
- **X-Path 便捷支持**：`request.x_path` 自动 URL 编码（兼容中文路径）
- **UTF-8 中文文件名上传**：`upload` 字段走 UTF-8 multipart，不再触发 `UnicodeEncodeError`
- **多环境切换**：`conftest.py` 提供 `--env=test|prod` 开关，不改 YAML 一键切环境
- **签名/加密工具库**：`debugtalk.py` 沉淀 md5/hmac/base64 等通用函数，`${md5_sign(...)}` 直接调用

## 用例清单

| 文件 | 对应原用例 | 说明 |
|---|---|---|
| `auth_login.yml` | TC-AUTH-001 | 登录：显式调用 token 端点并断言 |
| `directory_create.yml` | TC-DIR-001 | 创建目录（自动认证 + X-Path） |
| `file_upload.yml` | TC-FILE-001 | 上传文件（中文文件名） |
| `file_delete.yml` | TC-DEL-001 | 删除文件（创建→上传→删除完整流程） |
| `md5_sign_demo.yml` | 教学示例 | 演示 `${md5_sign(...)}` 签名生成与注入（UniFSP 本身不需要签名） |

## 前置准备

1. 激活虚拟环境：

   ```powershell
   D:\interface-tester\.venv\Scripts\Activate.ps1
   ```

2. 配置环境变量（复制 `.env.example` 为 `.env` 并填写真实值）：

   ```powershell
   Copy-Item .env.example .env
   ```

   填写的 `UNIFSP_CLIENT_ID` 会同时用作应用根路径 `/{client_id}/`。

## 运行

```powershell
# 登录 / 认证
hrun examples/unifsp/auth_login.yml

# 创建目录
hrun examples/unifsp/directory_create.yml

# 上传文件
hrun examples/unifsp/file_upload.yml

# 删除文件（完整流程）
hrun examples/unifsp/file_delete.yml

# 生成 HTML 报告
hrun examples/unifsp/file_delete.yml --html=reports/unifsp.html --self-contained-html
```

## 多环境切换

`conftest.py` 提供了 `--env` 开关，通过「环境变量 → debugtalk 函数 → YAML 引用」这条链路切换运行环境，**YAML 用例无需任何修改**：

```powershell
# 不传 --env：用 .env 里的 UNIFSP_BASE_URL（当前默认 test）
hrun examples/unifsp/auth_login.yml

# 切到生产环境
hrun examples/unifsp/auth_login.yml --env=prod
```

各环境地址在 `conftest.py` 的 `ENV_BASE_URL` 字典里维护（`test` / `prod`），可按需增删。

## 签名工具函数示例

`debugtalk.py` 沉淀了通用签名/加密函数（`md5` / `sha256` / `hmac_sha256` / `md5_sign` / `base64_encode` 等），在 YAML 里用 `${函数名(...)}` 调用即可，参考 `md5_sign_demo.yml`：

```yaml
config:
    variables:
        app_path: ${app_path()}
        timestamp: ${get_timestamp()}
        sign_secret: ${ENV(UNIFSP_CLIENT_SECRET)}   # 签名秘钥
        sign_params:                                # 参与签名的参数（key 自动升序拼接）
            path: $app_path
            timestamp: $timestamp
        sign: ${md5_sign($sign_params, $sign_secret)}   # 签名 = md5(参数+秘钥)

teststeps:
  - name: 带签名请求
    request:
        url: /api/xxx
        method: POST
        headers:
            X-Sign: $sign        # 签名放进请求头
        json:
            path: $app_path
            timestamp: $timestamp
            sign: $sign          # 签名放进请求体
```

> 注意：函数参数里不能写引号/花括号，需先用 `variables` 定义好参数（如上 `sign_params`、`sign_secret`），再以 `$变量名` 形式传给函数。

## 关键映射（相对原 unifsp_api_test 的差异）

| 原项目写法 | interfacetester 写法 |
|---|---|
| `Config.BASE_URL`（含 `/index`） | `base_url: ${api_base()}`（debugtalk.py 自动去 `/index`） |
| `UniFSPClient.get_token()` 手动认证 | `config.oauth2` 自动认证，无需写登录步骤 |
| `_get_headers(x_path)` 手拼 X-Path | `request.x_path: $path`（自动 URL 编码） |
| `_build_multipart_body` 手写 multipart | `upload:` 字段（框架内置 UTF-8 multipart） |
| 断言 `result["success"]` | 断言 `body.success`（服务端真实字段，非 `obj/code/msg` 别名） |

## 注意事项

1. **业务失败一律 HTTP 200**：断言写 `body.success` / `body.errorCode`，不要断言 400/500 状态码。
2. **`base_url` 与 `token_url` 分离**：token 端点在根路径 `/oauth2/token`，不能带 `/index`。
3. 上传/删除会真实改动服务端数据，建议先在测试环境执行。

