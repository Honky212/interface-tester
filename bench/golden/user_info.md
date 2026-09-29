# 用户信息查询

## 前置

需先调用 `POST /api/login` 获取 token，后续请求在 `Authorization` 头里带 `Bearer <token>`。

## GET /api/user/info

查询当前登录用户的信息。

### 请求头

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| Authorization | 是 | `Bearer <token>` |

### 响应

```json
{"code": 0, "data": {"username": "admin", "role": "admin", "email": "a@example.com"}}
```

### 业务规则

- token 缺失或过期返回 `code: 4010`
