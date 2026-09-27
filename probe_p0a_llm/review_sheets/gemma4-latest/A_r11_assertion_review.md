# 表 A：R11 断言语义正确率抽检（模型 `gemma4:latest`）

> 共 **13** 条**落盘断言**（落盘的才会被执行，所以逐条判）。
> **要你判的那一列**：`expect` 的取值**是否符合引用句与文档的语义**——
> 机检只能保证「引用句逐字在文档里」，**判不了「语义对不对」**（那是 R11 的范围）。
> **标记**：`✓` 判对 ／ `✗` 判错 ／ **`?` 无法判定**（不懂业务就写 `?`——它**不计入分子分母**，单列统计，本仓立场是「证据不足就输出未知」，不许猜）。**判错不用慌**：改一格 → 重跑 `score_review_sheets.py` → 新结论**追加**进 `SCORES_HISTORY.md`，**旧结论不重写**。

| # | 用例 | 步骤 | 断言（算子/检查项/期望） | 引用句 source_quote | 引用句逐字在文档里（脚本独立复核） | **语义一致？人工填 ✓/✗** | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `G01_auth_token_flow___GET__api_user_me` | 查询当前用户 | `equal(status_code, 200)` | 用登录得到的令牌查询当前用户（**依赖上一步的 `token`**）。 | **是** |✓| |
| 2 | `G01_auth_token_flow___POST__api_login` | 执行登录请求 | `equal(status_code, 200)` | 登录并返回访问令牌。 | **是** |✓| |
| 3 | `G02_biz_error_order___POST__api_order_create` | 调用创建订单接口 | `equal(status_code, 200)` | HTTP 仍可能是 200 | **是** |✓| |
| 4 | `G03_create_user_schema___POST__api_user` | 调用创建用户接口 | `equal(status_code, 200)` | 创建用户并返回用户对象。 | **是** |✓| |
| 5 | `G05_get_user_profile___GET__api_user_profile` | 按 userId 查询用户档案 | `equal(status_code, 200)` | 按 userId 查询用户档案。 | **是** |✓| |
| 6 | `G07_login___POST__api_login` | 登录并返回访问令牌。 | `equal(status_code, 200)` | 登录并返回访问令牌。 | **是** |✓| |
| 7 | `G08_order_create___POST__api_order` | 创建订单 | `equal(status_code, 200)` | 创建订单。 | **是** |✓| |
| 8 | `G09_paged_product_list___GET__api_product_list` | 分页查询商品列表 | `equal(status_code, 200)` | 分页查询商品列表。 | **是** |✓| |
| 9 | `G10_search_products___GET__api_product_search` | 执行商品搜索 | `equal(status_code, 200)` | 按关键词与标签搜索商品。 | **是** |✓| |
| 10 | `G12_update_order_status___PUT__api_order__orderNo__status` | 更新订单状态 | `equal(status_code, 200)` | 更新订单状态（**幂等 PUT**） | **是** |✓| |
| 11 | `G13_user_info___GET__api_user_info` | 查询当前登录用户的信息 | `equal(status_code, 200)` | 查询当前登录用户的信息。 | **是** |✓| |
| 12 | `G13_user_info___前置` | Login | `equal(status_code, 200)` | 需先调用 `POST /api/login` 获取 token | **是** |✓| |
| 13 | `G15_webhook_notify___POST__api_webhook_notify` | 发送订单事件通知 | `equal(status_code, 200)` | 向业务方投递订单事件通知（**含嵌套对象**）。 | **是** |✓| |
