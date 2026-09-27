# 登录与当前用户接口

## POST /api/login

登录并返回访问令牌。

### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| username | string | 是 | 用户名 |
| password | string | 是 | 密码 |

### 响应

```json
{"code": 0, "data": {"token": "eyJhbGciOi...", "expiresIn": 7200}}
```

## GET /api/user/me

用登录得到的令牌查询当前用户（**依赖上一步的 `token`**）。

### 请求头

| 头 | 值 | 必填 | 说明 |
| --- | --- | --- | --- |
| Authorization | `Bearer <token>` | 是 | 上一步返回的访问令牌 |

### 响应

```json
{"code": 0, "data": {"userId": 1001, "role": "admin"}}
```

### 业务规则

- 令牌缺失或过期时返回 `code: 4010`
