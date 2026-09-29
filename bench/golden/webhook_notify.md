# 回调通知（Webhook）

## POST /api/webhook/notify

向业务方投递订单事件通知（**含嵌套对象**）。

### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| event | string | 是 | 枚举：order.created / order.paid / order.cancelled |
| payload | object | 是 | 事件负载 |
| payload.orderNo | string | 是 | 订单号 |
| payload.amount | integer | 是 | 金额（分） |

### 响应

```json
{"code": 0, "data": {"delivered": true, "attempt": 1}}
```

- `data.delivered`：布尔，是否已投递
- `data.attempt`：整数，投递次数

### 业务规则

- 重复事件返回 `code: 5001`，`data.delivered: true`（幂等）
