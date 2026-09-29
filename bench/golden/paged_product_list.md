# 商品列表（分页）

## GET /api/product/list

分页查询商品列表。

### 查询参数

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 页码，从 1 开始 |
| pageSize | integer | 是 | 每页条数，1 ~ 100 |

### 响应

```json
{"code": 0, "data": {"total": 128, "list": [{"sku": "SKU-0001", "price": 9900}]}}
```

- `data.total`：整数，总条数
- `data.list`：数组，当前页数据
- `data.list[].sku`：字符串
- `data.list[].price`：整数，单位分

### 业务规则

- `pageSize` 超过 100 时返回 `code: 1002`
