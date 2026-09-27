# 表 B：`unknowns` 抽检（P0b 验收：≥8/10）（模型 `gemma4:latest`）

> 共 **8** 条。验收口径是「抽 10 条、判对 ≥8 条」——本轮**总数 不足 10**，
> 不足 10 时口径需人工确认（可否跨轮次/跨模型合并计数）。
> **要你判的那一列**：这条「降级 / 拒掉」的判定**对不对**（有没有误伤、有没有漏网）。
> **标记**：`✓` 判对 ／ `✗` 判错 ／ **`?` 无法判定**（不懂业务就写 `?`——它**不计入分子分母**，单列统计，本仓立场是「证据不足就输出未知」，不许猜）。**判错不用慌**：改一格 → 重跑 `score_review_sheets.py` → 新结论**追加**进 `SCORES_HISTORY.md`，**旧结论不重写**。

| # | 用例 | kind | level | where | 机器给的判定与理由 | 建议动作（机器给） | **判定正确？人工填 ✓/✗** | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `G01_auth_token_flow___GET__api_user_me` | merge-note | degrade | merge | [未合并] body.username [type_match 'str']：引用行在第 11 行，不在本接口段（20~22、24~28、30~34、36~38 行）内 |  | | |
| 2 | `G01_auth_token_flow___GET__api_user_me` | merge-note | degrade | merge | [未合并] body.password [type_match 'str']：引用行在第 12 行，不在本接口段（20~22、24~28、30~34、36~38 行）内 |  | | |
| 3 | `G01_auth_token_flow___GET__api_user_me` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 4 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 | | |
| 4 | `G06_legacy_gbk_doc___GET__api_legacy_user_info` | merge-skip | degrade | merge | 模型的草稿**零断言**——合并**不救它**（这是「模型没干活」的质量信号，工具替它补上就等于把 R14 换个马甲）；确定性映射在本文档另可给出 4 条字段级断言，请走 `haify gen --assertions-only` 或交人工 |  | | |
| 5 | `G13_user_info___GET__api_user_info` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 1 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 | | |
| 6 | `G13_user_info___前置` | protocol-only | degrade | case | 这份用例的断言**全部是协议级检查项**（status_code），没有一条验证文档里写的事实（文档声明字段 1 个） | 协议级断言能挡住 4xx/5xx，但**测不出业务错**；请人工补一条文档级断言，或请文档作者把成功响应写成可断言的事实 | | |
| 7 | `G14_utf8_bom_doc___POST__api_export_tasks` | merge-skip | degrade | merge | 模型的草稿**零断言**——合并**不救它**（这是「模型没干活」的质量信号，工具替它补上就等于把 R14 换个马甲）；确定性映射在本文档另可给出 6 条字段级断言，请走 `haify gen --assertions-only` 或交人工 |  | | |
| 8 | `G14_utf8_bom_doc___POST__api_export_tasks` | quote-miss | degrade | steps[0].validate[0] | source_quote 不是文档里的原句（逐字比对失败）：'编码样本：本文件刻意带 UTF-8 BOM（Windows 记事本另存为的常见形态）。' | 引用必须是文档**原句**（逐字）；这是「不臆造」的机器检查点 | | |
