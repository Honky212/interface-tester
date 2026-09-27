# 用户登录接口

## POST /api/login

登录并返回访问令牌。

### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| username | string | 是 | 用户名 |
| password | string | 是 | 密码 |

### 响应

```json
{"code": 0, "message": "ok", "data": {"token": "eyJhbGciOi...", "userId": 1001}}
```

- `code`：整数，0 表示成功，非 0 为业务错误码
- `data.token`：字符串，访问令牌
- `data.userId`：整数

### 业务规则

- 用户名或密码错误时返回 `code: 1001`
