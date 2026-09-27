# 表 A：R11 断言语义正确率抽检（模型 `gemma4:latest`）

> 共 **52** 条**落盘断言**（落盘的才会被执行，所以逐条判）。
> **要你判的那一列**：`expect` 的取值**是否符合引用句与文档的语义**——
> 机检只能保证「引用句逐字在文档里」，**判不了「语义对不对」**（那是 R11 的范围）。
> **标记**：`✓` 判对 ／ `✗` 判错 ／ **`?` 无法判定**（不懂业务就写 `?`——它**不计入分子分母**，单列统计，本仓立场是「证据不足就输出未知」，不许猜）。**判错不用慌**：改一格 → 重跑 `score_review_sheets.py` → 新结论**追加**进 `SCORES_HISTORY.md`，**旧结论不重写**。

| # | 用例 | 步骤 | 断言（算子/检查项/期望） | 引用句 source_quote | 引用句逐字在文档里（脚本独立复核） | **语义一致？人工填 ✓/✗** | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `G01_auth_token_flow___GET__api_user_me` | 查询当前用户 | `equal(status_code, 200)` | 用登录得到的令牌查询当前用户（**依赖上一步的 `token`**）。 | **是** |✓| |
| 2 | `G01_auth_token_flow___POST__api_login` | 执行登录请求 | `equal(status_code, 200)` | 登录并返回访问令牌。 | **是** |✓| |
| 3 | `G01_auth_token_flow___POST__api_login` | 执行登录请求 | `type_match(body.username, 'str')` | \| username \| string \| 是 \| 用户名 \| | **是** |✓| |
| 4 | `G01_auth_token_flow___POST__api_login` | 执行登录请求 | `type_match(body.password, 'str')` | \| password \| string \| 是 \| 密码 \| | **是** |✓| |
| 5 | `G02_biz_error_order___POST__api_order_create` | 调用创建订单接口 | `equal(status_code, 200)` | HTTP 仍可能是 200 | **是** |✓| |
| 6 | `G02_biz_error_order___POST__api_order_create` | 调用创建订单接口 | `type_match(body.sku, 'str')` | \| sku \| string \| 是 \| 商品编码 \| | **是** |✓| |
| 7 | `G02_biz_error_order___POST__api_order_create` | 调用创建订单接口 | `type_match(body.quantity, 'int')` | \| quantity \| integer \| 是 \| 数量，1 ~ 99 \| | **是** |✓| |
| 8 | `G02_biz_error_order___POST__api_order_create` | 调用创建订单接口 | `greater_or_equals(body.quantity, 1)` | \| quantity \| integer \| 是 \| 数量，1 ~ 99 \| | **是** |✓| |
| 9 | `G02_biz_error_order___POST__api_order_create` | 调用创建订单接口 | `less_or_equals(body.quantity, 99)` | \| quantity \| integer \| 是 \| 数量，1 ~ 99 \| | **是** |✓| |
| 10 | `G03_create_user_schema___POST__api_user` | 调用创建用户接口 | `equal(status_code, 200)` | 创建用户并返回用户对象。 | **是** |✓| |
| 11 | `G03_create_user_schema___POST__api_user` | 调用创建用户接口 | `type_match(body.username, 'str')` | \| username \| string \| 是 \| 用户名 \| | **是** |✓| |
| 12 | `G03_create_user_schema___POST__api_user` | 调用创建用户接口 | `type_match(body.email, 'str')` | \| email \| string \| 是 \| 邮箱 \| | **是** |✓| |
| 13 | `G05_get_user_profile___GET__api_user_profile` | 按 userId 查询用户档案 | `equal(status_code, 200)` | 按 userId 查询用户档案。 | **是** |✓| |
| 14 | `G05_get_user_profile___GET__api_user_profile` | 按 userId 查询用户档案 | `type_match(params.userId, 'str')` | \| userId \| string \| 是 \| 用户 ID \| | **是** |✓| |
| 15 | `G05_get_user_profile___GET__api_user_profile` | 按 userId 查询用户档案 | `type_match(body.code, 'int')` | - `code`：整数，0 表示成功 | **是** |✓| |
| 16 | `G05_get_user_profile___GET__api_user_profile` | 按 userId 查询用户档案 | `type_match(body.data.userId, 'str')` | - `data.userId`：字符串 | **是** |✓| |
| 17 | `G05_get_user_profile___GET__api_user_profile` | 按 userId 查询用户档案 | `type_match(body.data.nickname, 'str')` | - `data.nickname`：字符串，昵称 | **是** |✓| |
| 18 | `G05_get_user_profile___GET__api_user_profile` | 按 userId 查询用户档案 | `type_match(body.data.avatarUrl, 'str')` | - `data.avatarUrl`：字符串，仅当 `withAvatar=true` 时返回 | **是** |✓| ⚠ 引用句明写「**仅当 `withAvatar=true` 时返回**」——**条件返回的字段被无条件断言**，该字段缺失时会**误报失败**；建议改判 `?` 或交人工确认。根因：映射器的"可选字段不断言"只覆盖**字段表的必填列**，覆盖不到**响应说明句里的"仅当…时返回"**（待补） |
| 19 | `G07_login___POST__api_login` | 登录并返回访问令牌。 | `equal(status_code, 200)` | 登录并返回访问令牌。 | **是** |✓| |
| 20 | `G07_login___POST__api_login` | 登录并返回访问令牌。 | `type_match(body.username, 'str')` | \| username \| string \| 是 \| 用户名 \| | **是** |✓| |
| 21 | `G07_login___POST__api_login` | 登录并返回访问令牌。 | `type_match(body.password, 'str')` | \| password \| string \| 是 \| 密码 \| | **是** |✓| |
| 22 | `G07_login___POST__api_login` | 登录并返回访问令牌。 | `type_match(body.code, 'int')` | - `code`：整数，0 表示成功，非 0 为业务错误码 | **是** |✓| |
| 23 | `G07_login___POST__api_login` | 登录并返回访问令牌。 | `type_match(body.data.token, 'str')` | - `data.token`：字符串，访问令牌 | **是** |✓| |
| 24 | `G07_login___POST__api_login` | 登录并返回访问令牌。 | `type_match(body.data.userId, 'int')` | - `data.userId`：整数 | **是** |✓| |
| 25 | `G08_order_create___POST__api_order` | 创建订单 | `equal(status_code, 200)` | 创建订单。 | **是** |✓| |
| 26 | `G08_order_create___POST__api_order` | 创建订单 | `type_match(body.orderId, 'str')` | \| orderId \| string \| 是 \| 订单号 \| | **是** |✓| |
| 27 | `G08_order_create___POST__api_order` | 创建订单 | `type_match(body.status, 'str')` | \| status \| string \| 是 \| 枚举：pending / paid / cancelled \| | **是** |✓| ⚠ 引用句**含枚举值**，却只断言了类型（**未生成 `contained_by`**）——与本表 #46（`body.event` 生成了 `contained_by`）**不一致**，属映射器**覆盖不全**（待查） |
| 28 | `G09_paged_product_list___GET__api_product_list` | 分页查询商品列表 | `equal(status_code, 200)` | 分页查询商品列表。 | **是** |✓| |
| 29 | `G09_paged_product_list___GET__api_product_list` | 分页查询商品列表 | `type_match(params.page, 'int')` | \| page \| integer \| 是 \| 页码，从 1 开始 \| | **是** |✓| |
| 30 | `G09_paged_product_list___GET__api_product_list` | 分页查询商品列表 | `greater_or_equals(params.page, 1)` | \| page \| integer \| 是 \| 页码，从 1 开始 \| | **是** |✓| |
| 31 | `G09_paged_product_list___GET__api_product_list` | 分页查询商品列表 | `type_match(params.pageSize, 'int')` | \| pageSize \| integer \| 是 \| 每页条数，1 ~ 100 \| | **是** |✓| |
| 32 | `G09_paged_product_list___GET__api_product_list` | 分页查询商品列表 | `greater_or_equals(params.pageSize, 1)` | \| pageSize \| integer \| 是 \| 每页条数，1 ~ 100 \| | **是** |✓| |
| 33 | `G09_paged_product_list___GET__api_product_list` | 分页查询商品列表 | `less_or_equals(params.pageSize, 100)` | \| pageSize \| integer \| 是 \| 每页条数，1 ~ 100 \| | **是** |✓| |
| 34 | `G09_paged_product_list___GET__api_product_list` | 分页查询商品列表 | `type_match(body.data.total, 'int')` | - `data.total`：整数，总条数 | **是** |✓| |
| 35 | `G09_paged_product_list___GET__api_product_list` | 分页查询商品列表 | `type_match(body.data.list, 'list')` | - `data.list`：数组，当前页数据 | **是** |✓| |
| 36 | `G10_search_products___GET__api_product_search` | 执行商品搜索 | `equal(status_code, 200)` | 按关键词与标签搜索商品。 | **是** |✓| |
| 37 | `G10_search_products___GET__api_product_search` | 执行商品搜索 | `type_match(params.keyword, 'str')` | \| keyword \| string \| 是 \| 关键词 \| | **是** |✓| |
| 38 | `G10_search_products___GET__api_product_search` | 执行商品搜索 | `type_match(body.data.hits, 'int')` | - `data.hits`：整数，命中数 | **是** |✓| |
| 39 | `G10_search_products___GET__api_product_search` | 执行商品搜索 | `type_match(body.data.list, 'list')` | - `data.list`：数组 | **是** |✓| |
| 40 | `G12_update_order_status___PUT__api_order__orderNo__status` | 更新订单状态 | `equal(status_code, 200)` | 更新订单状态（**幂等 PUT**） | **是** |✓| |
| 41 | `G12_update_order_status___PUT__api_order__orderNo__status` | 更新订单状态 | `type_match(params.orderNo, 'str')` | \| orderNo \| string \| 是 \| 订单号 \| | **是** |✓| |
| 42 | `G12_update_order_status___PUT__api_order__orderNo__status` | 更新订单状态 | `type_match(body.status, 'str')` | \| status \| string \| 是 \| 枚举：pending / paid / cancelled \| | **是** |✓| ⚠ 同 #27：引用句含枚举值，但**未生成 `contained_by`**（映射器覆盖不全，待查） |
| 43 | `G13_user_info___GET__api_user_info` | 查询当前登录用户的信息 | `equal(status_code, 200)` | 查询当前登录用户的信息。 | **是** |✓| |
| 44 | `G13_user_info___前置` | Login | `equal(status_code, 200)` | 需先调用 `POST /api/login` 获取 token | **是** |✓| |
| 45 | `G15_webhook_notify___POST__api_webhook_notify` | 发送订单事件通知 | `equal(status_code, 200)` | 向业务方投递订单事件通知（**含嵌套对象**）。 | **是** |✓| |
| 46 | `G15_webhook_notify___POST__api_webhook_notify` | 发送订单事件通知 | `type_match(body.event, 'str')` | \| event \| string \| 是 \| 枚举：order.created / order.paid / order.cance… | **是** |✓| |
| 47 | `G15_webhook_notify___POST__api_webhook_notify` | 发送订单事件通知 | `contained_by(body.event, ['order.created', 'order.paid', 'order.cancelled'])` | \| event \| string \| 是 \| 枚举：order.created / order.paid / order.cance… | **是** |✓| |
| 48 | `G15_webhook_notify___POST__api_webhook_notify` | 发送订单事件通知 | `type_match(body.payload, 'dict')` | \| payload \| object \| 是 \| 事件负载 \| | **是** |✓| |
| 49 | `G15_webhook_notify___POST__api_webhook_notify` | 发送订单事件通知 | `type_match(body.payload.orderNo, 'str')` | \| payload.orderNo \| string \| 是 \| 订单号 \| | **是** |✓| |
| 50 | `G15_webhook_notify___POST__api_webhook_notify` | 发送订单事件通知 | `type_match(body.payload.amount, 'int')` | \| payload.amount \| integer \| 是 \| 金额（分） \| | **是** |✓| |
| 51 | `G15_webhook_notify___POST__api_webhook_notify` | 发送订单事件通知 | `type_match(body.data.delivered, 'bool')` | - `data.delivered`：布尔，是否已投递 | **是** |✓| |
| 52 | `G15_webhook_notify___POST__api_webhook_notify` | 发送订单事件通知 | `type_match(body.data.attempt, 'int')` | - `data.attempt`：整数，投递次数 | **是** |✓| |
