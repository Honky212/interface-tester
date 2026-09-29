# 创建用户接口（带 schema 契约）

## POST /api/user

创建用户并返回用户对象。

### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| username | string | 是 | 用户名 |
| email | string | 是 | 邮箱 |
| age | integer | 否 | 年龄 |

### 响应

```json
{"code": 0, "data": {"userId": "U-2026-0001", "nickname": "bob", "tags": []}}
```

- `data` 是**对象**，必含 `userId` 与 `nickname`
- `data.tags` 是**数组**（可为空）

### 业务规则

- 用户名重复时返回 `code: 3001`
