# 订单（业务错误码）

## POST /api/order/create

创建订单；库存不足时返回**业务错误码**（HTTP 仍可能是 200）。

### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| sku | string | 是 | 商品编码 |
| quantity | integer | 是 | 数量，1 ~ 99 |

### 响应

```json
{"code": 0, "message": "ok", "data": {"orderNo": "SO-2026-0002", "amount": 19900}}
```

### 业务规则

- 库存不足：`code: 1001`，`message: 库存不足`
- 数量超出 1 ~ 99：`code: 1002`，`message: 数量非法`
- 商品编码不存在：`code: 1003`
