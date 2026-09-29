# 订单接口

## POST /api/order

创建订单。

### 请求体（JSON）

| 字段 | 类型 | 必填 | 取值范围 |
| --- | --- | --- | --- |
| clientOrderId | string | 是 | 客户端订单号（★刻意不叫 `orderId`：响应里它在 `data.orderId`，**末段撞名**会产出取错层级的断言，见 §9.32） |
| price | number | 是 | 0 ~ 99999 |
| status | string | 是 | 枚举：pending / paid / cancelled |

### 响应

```json
{"code": 0, "status": "pending", "data": {"orderId": "SO-20260922-0001"}}
```

### 业务规则

- 库存不足时返回 `code: 1001`，`message: 库存不足`
- `price` 必须落在 0 ~ 99999 之间，超出返回 `code: 1002`
