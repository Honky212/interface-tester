# 导出接口（BOM 编码文档样本）

## POST /api/export/tasks

> **编码样本**：本文件刻意带 **UTF-8 BOM**（Windows 记事本另存为的常见形态）。
> 它与 `legacy_gbk_doc.md` 一起覆盖「文档编码探测」的两端：BOM 能被 `utf-8-sig` 正确吃下，
> 而**不能**被当作正文内容（否则标题里会多出一个不可见的字符）。

### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| scope | string | 是 | 枚举：order / user / product |
| format | string | 是 | 枚举：csv / xlsx |

### 响应

```json
{"code": 0, "data": {"taskId": "T-2026-0001", "state": "queued"}}
```

- `data.taskId`：字符串
- `data.state`：字符串，枚举：queued / running / done / failed

### 业务规则

- 同一 scope 已有运行中任务时返回 `code: 6001`
