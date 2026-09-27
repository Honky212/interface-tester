# 商品搜索

## GET /api/product/search

按关键词与标签搜索商品。

### 查询参数

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| keyword | string | 是 | 关键词 |
| tags | string | 否 | 标签，多个用逗号分隔 |
| sort | string | 否 | 枚举：price_asc / price_desc / default |

### 响应

```json
{"code": 0, "data": {"keyword": "phone", "hits": 12, "list": []}}
```

- `data.hits`：整数，命中数
- `data.list`：数组

### 业务规则

- 关键词为空时返回 `code: 4001`
