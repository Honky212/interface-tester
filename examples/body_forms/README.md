# body_forms：五种请求体/传参形态（可离线复现）

一个用例覆盖五类最常见的接口报文形态，全部打到本地 mock httpbin 的**回显接口**上，
回显里能直接看到「服务端实际收到了什么」——适合当报文格式的速查与冒烟。

| # | 场景 | 框架写法 | 回显里断言什么 |
|---|---|---|---|
| 1 | JSON 通过 body 传参 | `request.json` | `body.json.*`、`Content-Type: application/json` |
| 2 | Query String 通过 params 传参 | `request.params` | `body.args.*`（**值全是字符串**，中文已解码） |
| 3 | 获取 token：body 的 x-www-form-urlencoded | `request.data`（字符串）+ 显式 `Content-Type` | `body.form.*` |
| 4 | 上传文件：body 的 form-data | `request.upload`（需 `[upload]` extra） | 请求头 `Content-Type` 以 `multipart/form-data` 开头 |
| 5 | 删除文件：body 的 raw 传参 | `request.data`（原样字符串）+ 显式 `Content-Type` | `body.data` 与发出的 raw 体逐字一致 |

## 运行

```bash
# 1) 先起本地 mock（默认 80/443；端口被占/无权限时换端口即可，用例自动跟随）
python tests/mock_server.py

# 2) 跑用例（另开一个终端）
hrun examples/body_forms/body_forms.yml
```

换端口示例（非 root/非管理员环境）：

```bash
export INTERFACETESTER_HTTP_BIN_PORT=8000   # PowerShell: $env:INTERFACETESTER_HTTP_BIN_PORT="8000"
python tests/mock_server.py &
hrun examples/body_forms/body_forms.yml
```

上传一步需要可选依赖（未安装时会得到可操作的报错提示，而不是裸堆栈）：

```bash
pip install -e ".[upload]"
```

## 每类的关键点（容易踩的坑都在用例注释里）

1. **json**：框架自动序列化并设置 `Content-Type: application/json`（无 charset）；
   带 `-` 的头名在 jmespath 断言里要加引号：`body.headers."Content-Type"`。
2. **params**：值会被 URL 编码（中文安全）；回显的 query **值全是字符串**——
   `page: 1` 回显成 `"1"`，断言别写整数。
3. **urlencoded（获取 token）**：`data` 给字符串 + 手动 `Content-Type`；
   真实 OAuth2 服务推荐用内置能力，YAML 里一个字段都不用手写：
   ```yaml
   config:
       oauth2:
           token_url: https://auth.example.com/oauth/token
           client_id: "${ENV(CLIENT_ID)}"
           client_secret: "${ENV(CLIENT_SECRET)}"
           grant_type: client_credentials
   ```
   框架会自动发 form-urlencoded 的 token 请求、缓存并在后续请求注入 `Authorization: Bearer`。
4. **upload（form-data）**：`upload` 里的**路径值**按文件字段上传、**标量值**按普通表单字段发送；
   **`upload` 与 `data` 互斥**（multipart 编码器会整体替换 data），普通表单字段写进 `upload` 即可。
5. **raw**：`data` 给字符串就是 raw 体，框架原样发送；`Content-Type` 按体的真实格式写；
   raw 体里同样可以用 `$var` / `${func()}`（示例第 5 步引用了第 1 步提取的 `creator`）。
