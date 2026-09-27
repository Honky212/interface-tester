# 表 B：`unknowns` 抽检（P0b 验收：≥8/10）（模型 `gemma4:latest`）

> 共 **14** 条。验收口径是「抽 10 条、判对 ≥8 条」——本轮**总数 够**，
> 不足 10 时口径需人工确认（可否跨轮次/跨模型合并计数）。
> **要你判的那一列**：这条「降级 / 拒掉」的判定**对不对**（有没有误伤、有没有漏网）。
> **标记**：`✓` 判对 ／ `✗` 判错 ／ **`?` 无法判定**（不懂业务就写 `?`——它**不计入分子分母**，单列统计，本仓立场是「证据不足就输出未知」，不许猜）。**判错不用慌**：改一格 → 重跑 `score_review_sheets.py` → 新结论**追加**进 `SCORES_HISTORY.md`，**旧结论不重写**。

| # | 用例 | kind | level | where | 机器给的判定与理由 | 建议动作（机器给） | **判定正确？人工填 ✓/✗** | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `G01_auth_token_flow___GET__api_user_me` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 4 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 2 | `G01_auth_token_flow___POST__api_login` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 4 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 3 | `G02_biz_error_order___POST__api_order_cr…` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 2 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 4 | `G03_create_user_schema___POST__api_user` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 3 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 5 | `G05_get_user_profile___GET__api_user_pro…` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 2 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 6 | `G07_login___POST__api_login` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 2 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 7 | `G08_order_create___POST__api_order` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 3 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 8 | `G09_paged_product_list___GET__api_produc…` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 2 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 9 | `G10_search_products___GET__api_product_s…` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 3 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 10 | `G12_update_order_status___PUT__api_order…` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 2 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 11 | `G13_user_info___GET__api_user_info` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 1 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 12 | `G13_user_info___前置` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 1 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
| 13 | `G14_utf8_bom_doc___POST__api_export_task…` | quote-miss | degrade | steps[0].validate[0] | source_quote 不是文档里的原句（逐字比对失败）：'编码样本：本文件刻意带 UTF-8 BOM（Windows 记事本另存为的常见形态）。它与 `legacy_gbk_doc.md` 一起覆盖「文档编码探测」的两' | 引用必须是文档**原句**（逐字）；这是「不臆造」的机器检查点 |✓| |
| 14 | `G15_webhook_notify___POST__api_webhook_n…` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 4 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 |✓| |
