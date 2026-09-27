# 用户档案接口

## GET /api/user/profile

按 userId 查询用户档案。

### 查询参数

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| userId | string | 是 | 用户 ID |
| withAvatar | string | 否 | 传 `true` 时返回头像地址 |

### 响应

```json
{"code": 0, "message": "ok", "data": {"userId": "1001", "nickname": "alice", "avatarUrl": "https://cdn.example.com/a.png"}}
```

- `code`：整数，0 表示成功
- `data.userId`：字符串
- `data.nickname`：字符串，昵称
- `data.avatarUrl`：字符串，仅当 `withAvatar=true` 时返回

### 业务规则

- 用户不存在时返回 `code: 2001`，`message: 用户不存在`
