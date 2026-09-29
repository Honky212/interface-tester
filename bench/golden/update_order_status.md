# 更新订单状态

## PUT /api/order/{orderNo}/status

更新订单状态（**幂等 PUT**）。

### 路径参数

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| orderNo | string | 是 | 订单号 |

### 请求体（JSON）

| 字段 | 类型 | 必填 | 取值范围 |
| --- | --- | --- | --- |
| status | string | 是 | 枚举：pending / paid / cancelled |

### 响应

```json
{"code": 0, "status": "paid", "data": {"orderNo": "SO-2026-0002"}}
```

### 业务规则

- 已发货订单不允许改为 cancelled：`code: 1004`
