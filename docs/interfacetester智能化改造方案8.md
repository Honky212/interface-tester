# interfacetester 智能化改造方案 8

> 版本：v8.0（**复核修正定稿版 · 三次校准**）
> 承接链：《interfacetester智能化改造方案》（v1.0 草案）→《…方案2》（v2.0 源码评审）→《…方案3》（v3.0，面向 4.3.5）→《…方案4》（v4.0 交付形态定稿）→《…方案5》（v5.0 静默陷阱闸门交付版）→《…方案6》（v6.0 合并定稿版）→《…方案7》（v7.0 复核修正定稿版）→ **本文**。
> ★**承接链口径**（2026-09-26 D1 决策）：链中《方案2-对5.0.1对账》《方案2-工作量重估》两份**不在本仓、已作废、不再引用**——v4/v5 引用的"对账文 §1.4/§1.8/§三"与"26~34 人日"**无法复核**（实测 `D:\tester` 与真仓 `D:\learn\interface-tester` 按文件名递归检索**均零命中**；登记见 `docs/ARCHIVED.md`）；**工作量口径以 §8.2 的 33~48 人日（裁剪后 23~29）为唯一来源**。
> ★**本方案已定案的分歧**（2026-09-26）：D2 架构落点（旁路包唯一主线）、D3 风险姿态（不支持自动 apply）、D4 验收① 口径（接口片产出率 ≥86.7%，见 §12.8-⑩ ④）、D6 三处小决策（均采纳默认值）、**D7 生成默认落点（`cases/` → `.ai/draft/`，§十一 D7 + `docs/未做完的待决策项.md` §九）**——逐条见 §十一。
> 核对对象：**interfacetester 5.0.1 实际源码**（源码仓 `D:\learn\interface-tester`；本工作区 `D:\tester` 内亦有同版本源码树，`__init__.py` 实测 `__version__ = "5.0.1"`，两处 34 个 `.py` 经 v8 逐字节比对**完全一致**）+ 本工作区 `.venv` 中的 5.0.1 环境（`Python 3.12.10`；**★v8 修正措辞**：实测 `import interfacetester` 解析到 `D:\tester\interfacetester\__init__.py`，是 **editable 安装**而非 wheel——v7 头部"已安装的 wheel 环境"表述不精确，但因两处源码一致，全部实测命令不受影响）。
> 行文沿用本仓惯例：引用代码只写 `module.py::function()`，**不写行号**（行号会漂）。
> 本文所有数字与判据均在 **§十二 复核记录** 里给出取证命令与实测输出；**未实测的一律标注为"估算"**；**★v8：凡写"实测通过"处必须注明运行环境四元组（命令、环境、输出、退出码）**——在 A 控制台通过不等于在 B 环境通过（§12.8-①）。
> **v8 与 v7 的关系**：v7 的再复核经独立评审抽查约 40 项论断**无一造假**（§12.7 甲组全部成立）；但 v8 换环境再跑后发现 v7 自身仍有 **5 处**口径不实或未覆盖（§0.5）——
> 其中两处恰是 v7 自己新立的 **R14**（"已钉住/已实现"的保真度幻觉）：把 T19/T20 的两道元护栏写成"已完成"、把 T1 落地后才存在的 S3 PENDING 态写成现状；
> 一处动摇"实测通过"这句话的地基：`probe_silent_gate.py` 只在交互控制台通过，**重定向（CI 标准形态）下 UnicodeEncodeError、退出码 1**。
> v8 **不新增大功能、不改任何代码**，只做 §0.5 的 5 处修正。

## 本文要做的四件事

1. **合并**：《方案 3》《方案 4》《方案 5》三份文档的**全部优点**（§一、§二、§五、§七、§九）——v6 已完成，v7/v8 整体保留；
2. **纠错**：把前作中**已被实测证伪的 9 处**修掉（§0.3）——v6 已完成，v7/v8 整体保留；
3. **再复核**：把 v6 的每一条"已实现 / 已钉住 / 已完整"逐条对回代码与真实产物，修掉 **12 处**口径不实或自相矛盾（§0.4）——v7 已完成；**再复核的再复核**：把 v7 的"实测通过 / 已完成"放到**另一个环境**里再跑一遍（重定向、GBK 控制台、退出码、测试函数清单），修掉 **5 处**（§0.5）——**这是本文的实质改动**；
4. **诚实**：把**尚未收敛的分歧**单列（§十一），不假装所有决策都已定案；本文新修的每一处都给出**可复制命令与实测输出**（§12.8），并注明**运行环境**。

> **读法建议**：只看结论 → **§〇**；要知道"哪些话是实测的、哪些是估算" → **§十二**；要知道"v8 到底改了什么" → **§0.5 + §12.8**；要知道"v7 改了什么" → **§0.4 + §12.7**（承接保留）；
> 要开工 → **§八 + §十**；要复核任何数字 → **附录 A**（命令可直接复制）。

---

## 〇、一页结论

### 0.1 三份前作各自的贡献（v6 的取材来源）

| 前作 | 不可替代的贡献 | v6 如何处置 |
| --- | --- | --- |
| **方案 3（v3.0）** | ① **三层架构**（框架层不变 / AI 协作层 / 人类审核层）；② 失败归因的**五分类**（真实缺陷·用例错误·环境·数据·未知）；③ **证据优先**纪律（无证据必须输出"待人工判断"）；④ 诊断结论的**结构化 JSON 形状**；⑤ 八项延伸场景（测试数据、mock、debugtalk、摘要、回归优先级、用例维护、测试计划、规则检查）作为长期路线 | **全部吸收**（§2.1、§5.3、§5.4、§9.3）；但其"方式 C：AI 自动修正（受控）"与"`interfacetester.plugins.ai` 进内核"两条**不采纳**（§0.3-⑦、§十一 D2） |
| **方案 4（v4.0）** | ① **交付形态决策**（CLI 真相源 + Web 薄确认台）；② 只读 import **白名单**与"必须复用不得重造"护栏清单；③ LLM 接入层（预算/缓存/审计/内容闸/prompt 动态渲染）；④ 8 个落地坑 P-1~P-8；⑤ 可机器判定的验收表；⑥ **附录式可复制复核命令** | **全部吸收**；修正其 6 处实测错误（§0.3-①~⑥） |
| **方案 5（v5.0）** | ① 论证"**运行期结构上不可判定的陷阱，只能在生成期拦**"；② G/R/X **三分类**穷举 + 逐条实测证据；③ 11 条生成期闸门 S1~S11；④ **REJECT/PENDING 两级**严重程度；⑤ **成对判据**纪律（MUST_REJECT + MUST_ALLOW + 两道元护栏）；⑥ S2 的"只拦能精确判定的形态"精度设计 | **全部吸收**；修正其 3 处（§0.3-⑧⑨ 与 §8.3 数字口径） |

### 0.2 本方案（v8）的定位（一句话）

> **把"LLM 当作者、代码当闸门、内核当裁判、人来兜语义"这条主线，从"口径可证"再推进到"口径可证、可复跑"——凡是写"实测通过"的地方，必须同时给出命令、环境、输出、退出码；凡是写"已完成"的护栏，必须能指认到测试文件里已经存在的那条用例。**
> 相对 v7，v8 **不新增大功能、不改任何代码**，只做 §0.5 的 5 处修正——因为 v7 的问题**不在方向，在"在哪个环境实测的"与"什么才算已完成"这两条缝**：交互控制台通过 ≠ CI 通过（§0.5-①），目标态写进现状表 ≠ 已实现（§0.5-②④）。

### 0.2a v7 的定位（承接记录，v7 原文）

> **把"LLM 当作者、代码当闸门、内核当裁判、人来兜语义"这条主线，从"口径自洽"再推进到"口径可证"——凡是写"已实现 / 已钉住 / 已完整"的地方，都必须能指出钉住它的那行代码或那条断言。**
> 相对 v6，v7 **不新增大功能、不改任何代码**，只做 §0.4 的 12 处修正——因为 v6 的问题**不在方向，在"自称已收口"与"实际收口"之间的缝**。

### 0.3 v6 相对前作的 9 处修订（v7 全部保留，本节仅作承接记录）

| # | 前作的问题 | 出处 | v6 处置 |
| --- | --- | --- | --- |
| ① | 证据指针缺 `.data.`：写成 `details[i].records[j].validators…` | 方案 4 §4.3/§4.5 | 更正为 `details[i].records[j].data.validators.validate_extractor[k]`（§5.5） |
| ② | 「同输入两次运行输出**逐字节一致**」验收恒红 | 方案 4 §4.3/§7.1 | 改为「**归一化投影后**逐字节一致」（§5.6） |
| ③ | 把 L2 当"离线无害的硬闸门" | 方案 4 §3.2 | 补 L2 的**副作用与隔离**（§3.2、§7-P3） |
| ④ | `git status` 当判据，但工作区非 git 仓 | 方案 4 P-7/T8/P3c | 改为**分层判据**（§5.7） |
| ⑤ | Web 增量三处互斥（8~14 / 8~14 / 6~11），总计两个数（34~48 / 32~45） | 方案 4 §0.3/§2.8/§7.1 | **统一为单一口径**并给出算术（§8.2） |
| ⑥ | §六 末尾 12 行空白（内容缺失） | 方案 4 §六 | 补全为完整章节（§六） |
| ⑦ | `"L2 通过"` 等同于"产物可用" | 方案 4 §4.1/§7.1 | 单独暴露 `formatting` 字段（§3.4） |
| ⑧ | **S3 过度拦截 + 提示的改法自身不可行**（v6 新发现） | 方案 5 §3.1 的 S3 | 重定义 S3 语义（§3.3）+ 补 2 条 MUST_ALLOW（§3.5） |
| ⑨ | 复用型护栏**喂错形态会静默放行**（v6 新发现） | 方案 4 §3.4「必须复用」纪律 | 补"适配器形态自检"（§2.4） |

> 前作之间还有一处**判断分歧**，不是错误但必须表态：方案 5 §1.1~§1.4 对方案 4 的四处修订（①②③④）**全部经 v6 实测为真**，v6 一律采纳方案 5 的口径。

### 0.4 ★ v7 相对 v6 的 12 处修订（v7 的实质改动，全部为再复核实测发现；v8 承接保留）

> 本节是 v7 与 v6 的**唯一实质差异**。每条都给出：v6 怎么说 → 我实测到什么 → v7 怎么改。
> 取证命令与实测输出统一放在 **§12.7**，可直接复制复核。

| # | v6 的说法 | 实测（§12.7 取证） | v7 处置 |
| --- | --- | --- | --- |
| ① | §5.6「归一化投影」自称列出了**完整剔除清单** | 清单只剔了**请求侧** `interfacetester-Request-ID`；但真实 `summary.json` 里 `records[*].data.req_resps[*].response.headers` 带**秒级 `Date`**（`Tue, 22 Sep 2026 15:02:22 GMT`），顶层 `platform` 也随机器变 → **按该清单实现，那条验收照样恒红** | 重写 §5.6 剔除清单（补 `response.headers` 白名单化 / 剔 `Date`、`Set-Cookie`、`Server`；剔 `platform`）＋ 新增 **T17** |
| ② | §2.2 红线③「**LLM 模块里禁止出现 `open(` / `Path.write`**」 | 与 §6 自身要求**直接矛盾**：缓存"命中即回放"要写 `.ai/cache/`、审计"每次调用落 `.ai/manifest/…json`"，这两件事都发生在 AI 接入层 → 这条机器检查**写不出来**，写出来会把自己的缓存/审计判违规 | 红线③ 重写为「**LLM 从不写 `cases/`**」＋ 显式登记 `cache.py` / `manifest.py` 为**允许的写入者**（只许写 `.ai/` 下）＋ 新增 **T18**（**★v8 落地注记 2026-09-23：T18 已按第四批落地**——`bench/write_boundary_scanner.py` 按本行口径实现，验收见 §12.8 末注记与 §十 T18 ✅ 行） |
| ③ | §3.2「该分桶**已被** `tests/golden_baseline_test.py` **钉住**」 | 该文件只钉通过率常量 `GOLDEN_TOTAL=3` / `GOLDEN_PASSED=3`；`failure_buckets` 与 `r['bucket']` **只出现在失败提示字符串里，没有任何 assert**。且 `golden_harness.py` 的 `FAIL_PROJECT="project"` **不可达**（`_classify` 兜底先返回 `make`） | §3.2 改为「**可达三桶**（load/make/compile）」＋ 桶名必须被断言；把 P-9 形态自检**扩展到 `_classify`** ＋ 新增 **T19** |
| ④ | §3.3 的 **S1** 判据写「`not_equal`/`contained_by` 的 expect 为空值…」 | `bench/silent_traps.py` 里 `contained_by` **零出现**：`gate_s1_pseudo_presence` 只实现 `not_equal` / `contains` / `jsonschema_match` 三支，`MUST_REJECT` 也只有这 3 条样本 | §3.3 **删掉 `contained_by`** 并写明**为什么它不构成真空**（`contained_by: [x, []]` 恒假，不存在假通过）＋ 新增 **T20** |
| ⑤ | §3.3 开头称 S 系列「**已实现并实测**」 | 11 条闸门**函数确实都在**，但"**判据 = 实现**"没有任何对账机制——④ 就是这么漏出来的 | 新增纪律「**S 闸门三方对账**：判据 ↔ 代码分支 ↔ MUST_REJECT 样本」，落为 §3.5 **第三道元护栏** |
| ⑥ | §4.2 **G3** 的"实测现象"写「空 `teststeps` 只 warning」 | 内核已由 **N33 硬拒**（`make.py` 里 `not teststeps` 直接抛错，注释原文「空 `teststeps` 必须报错」）→ G3 该列已过期，且与 §3.3 的 S3 docstring 口径**相反** | G3 行按现状改写（空 `teststeps` = 已收口；"没有断言的 step 仍判成功" = 待 S3 闸门承担） |
| ⑦ | §3.3 表第 2/3 行 + §5.6 靠 `S4` 判"extract 的键有没有被下游引用" | S4 只看 `validate` 的 `check` 路径（`referenced = (path in checked) or any(n in c for c in checked)`）。于是**"登录取 token → 下游 `Authorization: Bearer $token`"这个最常见写法永远进 PENDING**，而 §3.3 给的理由（"后续 `$var` 静默拿到 None"）在该形态下**不成立**：token 为 None 会得到 401，下游 `eq: [status_code, 200]` 会**响亮失败** → S4 在此形态是**误报** | §3.3 表第 2/3 行改为「被下游**以任何形式**使用（断言 check、`$var` 出现在 headers/params/body/url）即放行」；S4 判据扩展到这些用法 ＋ 新增 **T21** |
| ⑧ | §5.2 期望值缺失 → 写 `${ENV(TODO_<语义名>)}` | 该用例能过 L1/S/L2，于是会**落 `cases/`**，然后 CI 因 `EnvNotFound` 变红（而 §8.3 又要求先只写 `.ai/draft/`）——两条落点规则打架 | §5.2 明确：**带 `TODO_` 占位的用例只落 `.ai/draft/`，不进 `cases/`** ＋ 新增 **T24** |
| ⑨ | §2.5 选型写「仓库已有 `examples/*/mock_server.py`、`tests/mock_server.py` 先例」 | `examples/*/mock_server.py` **不存在**；全仓只有 `tests/mock_server.py` 与 `project-two/mock_server.py`（`README.md`、`docs/ci/README.md` 指的都是前者） | 选型依据更正为 `tests/mock_server.py`（P3a 复用其 stdlib 写法，不引框架） |
| ⑩ | §12.1「内核规模 = 34 个模块 / **18,840 行**」 | `34` 正确；但 `18,840` 是 **`Measure-Object -Line` 的"非空行"**，**总物理行 = 22,173**（非空非注释 = 15,767）。附录 A 那条命令确实能复现 18,840，但标签写错了口径 | 数字**保留并标口径**：「34 模块 / 18,840 **非空行**（总 22,173 行，非空非注释 15,767）」；P-8 增加"**数字必须带口径标签**" ＋ 新增 **T23** |
| ⑪ | §8.2「裁剪顺序」第 2、5 条 | 第 2 条标签写"砍 P3a + P3b"，给出的却是 `−6~12`（= 砍掉**整个** Web 只留 1 天：`7−1 → 13−1`）；第 5 条 `−0~1` **根本没进合计**（文档自己又说它"本就不在 A 合计内"） | 标签与数字对齐；第 5 条改为显式"**不计入，0 影响**"；合计随之重算（§8.2 已改，且逐步算术仍自洽） |
| ⑫ | §0.3-① / §5.5 把 v4 的错指针出处写作「方案 4 §4.3/§4.5」 | v4 的错指针（`details[i].records[j].validators…`）出现在 **§二**（P3a 看板那条）与 **§4.5**；**§4.3 没有** | 出处更正为「方案 4 **§二/§4.5**」 |

> **另补一处记账口径**（不单列为修订）：修 S3 三态（§10 的 T1~T3 = 0.35 人日，连同扩展 S4 判据的 T21 与探针留档 T25 共 **0.55**）在 v6 的 §8.1 表里**没有独立行**，
> 只在 S-2 的"已完成 0.5"旁边写了一句"须补"。v7 把它单列为 **S-2b**（0.55），并新增 **S-9**（0.3），§8.1 / §8.2 的合计随之重算。

> **v7 没有改的**：v6 的主线（三层架构 / 七条红线 / 五道关顺序 / S1~S11 / 旁路包 / 工作量主表算术）**一条未动**——
> v7 只在 §12.7 甲记下"复核通过"的证据，不改结论。

### 0.5 ★ v8 相对 v7 的 5 处修订（本文的实质改动，全部为换环境再实测发现）

> 本节是 v8 与 v7 的**唯一实质差异**。方法：把 v7 每一句"**实测通过 / 已完成**"拿到**它没测过的环境**里再跑——
> 换重定向、换控制台编码、查退出码、数测试函数。取证命令与实测输出统一放在 **§12.8**，可直接复制复核。
> 独立评审已抽查 v7 约 40 项论断**无一造假**（§12.7 甲组全部成立）；本节 5 条是评审与 v7 **共同漏掉**的。

| # | v7 的说法 | v8 实测（§12.8 取证） | v8 处置 |
| --- | --- | --- | --- |
| ① | §12.1 / 附录 A：「`bench/probe_silent_gate.py` → 对抗验证通过」 | **交互控制台下确实通过**；但**重定向**（CI 收集日志的标准形态）+ 默认 GBK locale 下，`main()` 里 `print("  ✓ 已拦下…")` 的 `✓`（U+2713）无法编码 → `UnicodeEncodeError` → **退出码 1**；其余三个自检脚本同条件退出码 0（输出全为 GBK 可编码字符）。**S-7（CI 门禁）接上去第一天就红——红的不是闸门，是取证脚本自己** | §12.1 / 附录 A / §8.1 S-3 补**运行环境标注**；S-7 显式依赖新 **T26**（修探针输出编码）；§9.3 新增 **G8**；本文立"**实测通过必须四元组齐全：命令、环境、输出、退出码**" |
| ② | §8.1 S-2「`tests/silent_traps_test.py`（14 条 + **4 道元护栏**）｜已完成」；§9.3 G2「4 道元护栏（v7 增补 2 道）」 | 该文件 15 个 `test_` 函数里**已落地的元护栏只有 2 道**（v6 的 `test_gate_actually_detects_injected_fault` / `test_allow_set_would_catch_overblocking`）；**第三道 `test_every_gate_branch_has_a_must_reject_sample`、第四道 `test_classify_buckets_are_pinned` 均不存在**——它们是 T19/T20 的交付物（v7 自己在 §8.1 S-9 / §十里也列为待做） | §3.5 / §8.1 / §9.3 改为「**2 道已落地 + 2 道待做（T19/T20）**」。**这正是 v7 自己新立的 R14（保真度幻觉）的两个新实例**——§3.5 第三道元护栏防的是"判据 ↔ 分支漂移"，防不住"已完成"级别的虚标：文档纪律防不住自己，**"已完成"必须给出当时就能跑的验证命令**。**★v8 修复注记（2026-09-23）**：T19/T20 已当日补齐（`TestGateBranchAccounting` ×4 / `TestClassifyBucketsArePinned` ×1），复跑枚举命令 → `tests/silent_traps_test.py` 现 **22** 个 `def test_`（含 `test_every_gate_branch_has_a_must_reject_sample`），`tests/golden_baseline_test.py` 现 **6** 个（含 `test_classify_buckets_are_pinned`），§3.5 四道元护栏**名实相符**（验证命令与退出码见 §12.8-② 修复注记） |
| ③ | §3.3 / T21 对 S4 只讨论了**误报**方向（「取 token 用于下游 header」被误报 PENDING） | **漏报方向实测成立**：extract 键 `token` 从未被校验，但同 step 断言 `eq: [body.data.token_type, "Bearer"]` 时，`any(n in c for c in checked)` 的**子串匹配**命中（`'token' ⊂ 'body.data.token_type'`）→ **S4 静默不报**（对照组断言 `status_code` 则正常报 1 条 PENDING）。T21 若沿用子串匹配把"被引用"扩展到 `$var` 用法，`$token` 会被 `$token_id` 之类误判——**漏报面随扩展同步放大** | §3.3 S4 判据行加**漏报警示**；§3.3 S3 豁免行加同样警示；T21 判据改为**词边界 / 路径段精确匹配** ＋ 新增对抗样本「`token` vs `token_type` 必须仍报 PENDING」（★均已落地，见 §12.8-③ 修复注记） |
| ④ | §3.4 严重级别表与 §8.1 S-6 写「**S3(部分)** → PENDING 落盘」 | 当前实现 `gate_s3_assertion_vacuum` **一律 REJECT**（probe ②：`validate: []` 仍被拒、输出与①逐字相同）——**S3 的 PENDING 态在代码里不存在**，它是 §3.3 重定义后的**目标态**（T1 交付物）。S-6 的落盘对象「S3(部分) 的 PENDING」因此**暂无可落之物** | §3.4 / §8.1 S-6 加**目标态标注**（「待 T1 落地」）；与 §3.3 三方对账纪律的冲突和 §0.5-② 同病：**判据表写了 PENDING 分支、代码无此分支**（★T1~T3 已落地，见 §12.8-④ 修复注记；**★S-6 落盘亦已于第四批落地**——`interfacetester_ai/pending.py`，见 §12.8 末注记） |
| ⑤ | §0.4-⑩ / §12.1 引用「总物理行 22,173 / 非空 18,840 / **非空非注释 15,767**」——15,767 **未给取证命令**（违反 v7 自己立的"数字必须带口径标签"纪律；独立评审 P2 曾判其无法复核） | 口径破案：`15,767` = `Get-Content` **默认编码（GBK）**逐行、**非空且非 `#` 开头**（18,840 − 3,073 = 15,767，**保留 docstring**）——可复现但 v7 没给命令。**更深一层**：三个数字全部是 GBK 读取口径，而 GBK 解码 UTF-8 中文时会把「尾字节 + 0x0A」当双字节**吞掉换行**——UTF-8 正确口径为**总 22,638 / 非空 19,267 / 非空非# 15,947**（PS 加 `-Encoding UTF8` 后与 Python 逐行计数完全一致；GBK 口径偏差 **−465 行 ≈ −2.1%**，中文注释越密偏差越大） | §12.1 / 附录 A / T23 补**口径命令与编码标注**；v7 三数字保留为「GBK 口径（历史承接）」并注明偏差；**口径标签不仅要写"非空行"，还要写"用什么编码读的"**（T23 验收随之更新） |

> **v8 没有改的**：v7 的全部结论（§0.4 的 12 处修订、§12.7 甲乙两组复核、主线与算术）**一条未动**——
> v8 只在 §12.8 记下"换环境再跑"的证据，不改结论；工作量合计也**不变**（T26 的 0.1 人日并入 S-7 既有的 0.2 之内，§8.1/§8.2 不重算，避免 v6→v7→v8 三轮数字漂移）。
> **★v8 更新（2026-09-23 当日）**：T26 已按判据**落地**（四个 `bench/` 脚本入口统一 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`，带 `try/except` 防御、不影响被 `tests/` import），验收四元组见 §12.8-① 注记。
> **★v8 追加（同日第二批）**：**S-2b（= T1~T3 + T21 + T25）与 T11/T17 也已落地**——`gate_s3` 三态、`gate_s4` 词边界跨步引用、自检 11 拦/17 放、探针输出留档（`probe_s3_review/out_probe_s3_edge.txt`）、归一化投影 `bench/normalize_projection.py`（含响应头白名单与口径纪律）。验收记录见 §12.8-③④ 修复注记与 §十 T1~T3/T11/T17/T21/T25 各行；**T11 落地时新发现一处清单漏项（顶层 `time`），已补进 §5.6**。

### 0.6 技术路线一图

```
 文档/需求 ──► [切片 + 编码探测] ──► LLM(温度0/JSON Schema 约束，只产出 CaseDraft)
                                          │
                                          ▼
                              L1 严格模型校验（extra="forbid"）
                                          │
                                          ▼
                        ★ S 系列生成期闸门（11 条，纯代码，零副作用）★
                          REJECT ──► failed/ + NEEDS_HUMAN.md（不冒成功）
                          PENDING ─► <case>.pending.json（进人工确认清单）
                                          │
                                          ▼
                              L2 导出即验证（有副作用：写 *_test.py + black）
                                          │
                                          ▼
                        装配器写盘（LLM 无权摸文件）──► cases/*.yml
                                          │
                        ┌─────────────────┴─────────────────┐
                        ▼                                   ▼
              L3 运行期冒烟（默认关，子进程 hrun）      人工确认（CLI 或 P3 Web）
                        │                                   │
                        └──────► summary.json / *.run.log ◄──┘
                                          │
                            P1 分析（纯代码切片 → LLM + 引用校验）
                                          ▼
                     reports/analysis.md + reports/summary.md
                                          │
                                   git MR（唯一合并入口）

 ★ 元纪律（v7 补）：本图里每一个"★"（S 系列闸门、复用型护栏、归一化投影、桶分类）
   都必须配"违规必红 / 合规必绿"两条用例；凡写"已含 / 已钉住 / 已实现"，都必须指出那行断言或那个分支。
```

---

## 一、三份前作的共同内核（v6 / v7 不可动摇的五条）

这五条在 v3 / v4 / v5 中**表述不同但实质一致**，且都被源码或实测支持，本文整体继承。

| # | 共同判断 | 三份文档的证据 |
| --- | --- | --- |
| 1 | **LLM 负责"生成"与"分析"，永不负责"判定通过/失败"** | 方案 3 §1「让 LLM 负责生成和分析，不直接负责最终执行判定」；方案 4 §2.5「❌ 在 Web 里直接调用 LLM 做校验」；方案 5 §0.1「代码闸门挡可判定的错，人工确认处理语义的错」 |
| 2 | **执行与判定永远留在内核** | 方案 3 §3.1/§8「不要直接把 LLM 嵌进 `runner.py` 或 `parser.py` 的核心执行链路」；方案 4 红线①+§3.4 只读白名单；方案 5 §3.3「复用不得重造」 |
| 3 | **产物必须过校验才能落盘，半成品不冒充成功** | 方案 3 §5.2「只有通过校验的用例才落盘」+§5.4「不允许直接生成无校验的任意代码」；方案 4 红线⑤；方案 5 §4.1「闸门先筛，L2 后验，写盘最后」 |
| 4 | **高风险动作必须人工确认** | 方案 3 §3.3 人类审核层 +§5.11 三条路径；方案 4 红线⑥ + §2.7 审计留痕；方案 5 §3.4 的 PENDING 级 |
| 5 | **敏感信息统一走 `${ENV(...)}`，绝不硬编码** | 方案 3 §5.4；方案 4 §3.4 `sanitize_case` 单一收口 + §6 内容闸；方案 5 §2.2 G11 复用导入器口径 |

> 补充两条**工程纪律**（方案 4 立、方案 5 执行；v6 加强、v7 加第二条）：
>
> **纪律一：能复用内核护栏就不重造。**
> 理由很硬：内核 34 个模块 / 18,840 非空行（总物理行 22,173；口径见 §0.4-⑩）是**构建仓产物**，其护栏口径是被 `tests/` 与 CI 钉住的；
> 重写一份必然漂移，而漂移的护栏比没有护栏更危险（会让人以为已经查过）。
>
> **纪律二（v7 新增）：凡声称"已被钉住 / 已实现 / 已完整"的护栏，必须给出可执行的钉住点。**
> 理由是 v7 的实测教训：`_classify` 的分桶被写"已被 `golden_baseline_test.py` 钉住"（实际只钉了 3/3 通过率常量），
> S1 的判据写了 `contained_by`（代码零实现）——**这两处都不是"护栏没写"，而是"护栏没被钉住，却看起来被钉住了"**，
> 比 P-9 的"护栏打偏"更隐蔽：打偏还能靠一次对抗样本发现，**声称已钉住**则连去验证的动机都被消掉了。
> 判定方式：每处"复用 / 已含"都必须能回答三元组——**判据在文档哪一行、实现在代码哪个分支、红线样本是哪一条**（§3.5 第三道元护栏）。

---

## 二、架构定稿

### 2.1 三层分工（方案 3 的骨架 + 方案 4 的落点）

```
┌──────────────────────────────────────────────────────────────┐
│ 第一层：框架层 interfacetester 5.0.1（34 模块 / 18,840 行）    │
│   runner / parser / make / loader / cli / converters …       │
│   ★ 永不 import LLM 层（红线①）                               │
│   ★ 执行、提取、断言、判定、汇总 —— 全部在内核                 │
└──────────────▲───────────────────────────────┬───────────────┘
               │ 只读 import 公开面            │ 子进程调用
               │ (§2.3 白名单)                 │ hrun / hmake / hconvert
┌──────────────┴───────────────────────────────┴───────────────┐
│ 第二层：AI 协作层 interfacetester_ai（旁路包，新工程）          │
│   gen / assertions / analyze / summary / fix / debugtalk     │
│   llm / schema / prompts / cache / manifest / evidence       │
│   ★ 唯一实现处：所有 LLM 交互、校验、装配、脱敏、归一化          │
└──────────────▲───────────────────────────────┬───────────────┘
               │                               │
   ┌───────────┴───────────┐      ┌────────────┴───────────────┐
   │ CLI  haify            │      │ Web（可选，P3 起）          │
   │  真相源 + 执行面       │      │  薄渲染层 + 人工确认台      │
   │  CI / 内网 / air-gap  │      │  禁止重写任何执行/校验逻辑   │
   └───────────────────────┘      │  一律 subprocess 调 haify   │
                                  └────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ 第三层：人类审核层                                             │
│   确认 pending 回填 / 逐条 accept-reject fix / 决定是否合并      │
│   ★ 每次确认留痕 .ai/reviews/<ts>-<manifest_hash>.json         │
└──────────────────────────────────────────────────────────────┘
```

**三层的边界（最容易做错的地方）**

- 第二层是**唯一实现处**：Web 与 CLI 是同一个 Core 的两个前端，绝不允许 Web 里出现"另一套校验"。
- 第一层**不知道第二层存在**：反向依赖由"import 图扫描"机器检查（红线①的判定方式）。
- 第三层**不是可选装饰**：它是唯一处理"语义正确性"的环节（§5.4 的证据链也拦不住语义错，见 §9.2）。

### 2.2 七条红线（方案 4 的六条 + v6 新增 1 条；v7 修订第③条的判定方式）

| # | 红线 | 判定方式（可机器检查） |
| --- | --- | --- |
| ① | `runner / parser / make / loader / step* / client` **永不 import LLM 层** | 反向依赖测试：对内核模块做 import 图扫描，禁止出现 `interfacetester_ai` |
| ② | **失败隔离**：LLM 不可达/超时/欠费只能导致"没有建议"，**不得**改变测试结论或 CI 退出码 | 断网用例：`analyze` 退出码 = 3，产物零部分写，`hrun` 退出码不受影响 |
| ③ | **LLM 从不写 `cases/`**：用例落盘一律由装配器（纯代码）完成 | **两层判定 ★v7 修订**：① `cases/` 的写盘调用**只允许出现在装配器模块**（按**目标路径**判别，不按模块名判别）；② 直接处理模型响应的 `llm.py` 内禁止 `open(` / `Path.write`。**`cache.py` / `manifest.py` 是登记在册的允许写入者**，只许写 `.ai/cache/`、`.ai/manifest/`、`.ai/reviews/` |
| ④ | **L3 默认关**：真发请求必须 `INTERFACETESTER_AI_SANDBOX=on` + 主机白名单 + **子进程** | `check_l3_allowed` 三判据；子进程断言 |
| ⑤ | **产物必须过 L1 + S + L2 才允许落 `cases/`**；半成品进 `failed/` + `NEEDS_HUMAN.md` | 生成流程的写盘前置断言 |
| ⑥ | **确认必须留痕**：谁、何时、接受/拒绝什么、依据哪个 `manifest` hash | `.ai/reviews/*.json` 完整性用例 |
| ⑦ | **禁止把"可判定"改成"不可判定"**（v6 新增） | `fix` 的补丁必须过 S 系列闸门的**同一条判据**；把 `type_match: [x, "str"]` 改成 `not_equal: [x, ""]` 的补丁一律拒（§4.3） |

> 红线⑦ 是方案 5 §4.3 的洞察，v6 把它**提为红线**：前六条防的是"生成侧引入缺陷"，第七条防的是"**修复侧引入缺陷**"——
> 而 `fix` 的典型触发场景恰恰是"字段改名导致断言失败"，这是最容易从"能判定"退化成"不能判定"的时刻。

> **★ v7 对红线③ 的修订说明（§0.4-②）**：v6 原文是「`LLM 模块里禁止出现 open( / Path.write`（静态扫描 + 用例）」。
> 实测它与本文 §6 的要求**直接冲突**——§6 要求缓存"命中即回放"（落 `.ai/cache/`）、审计"每次调用落 `.ai/manifest/<ts>-<input-hash>.json`"，
> 而这两件事**必然发生在 AI 接入层**。所以红线③ 的可执行形态不是"某几个模块不许写盘"，而是：
> **① 谁都不许把 LLM 产物直接写进 `cases/`（唯一入口是装配器）；② 只有 `llm.py` 这个"模型响应处理层"不许碰文件系统；③ `cache.py`/`manifest.py` 是登记在册的写入者，且写入根固定为 `.ai/`。**
> 判定方式相应改为"**按写入目标路径 + 模块白名单**"，这样静态扫描才写得出来（T18）。
>
> **★v8 落地注记（2026-09-23，第四批）**：T18 已落地——`bench/write_boundary_scanner.py`
> 用 AST 把"写入类调用"（`open(写mode)` / `write_text` / `mkdir` / `shutil.copy*` / `os.replace` 等）
> 连同**静态可判的目标前缀**（字面量 / f-string 首段 / 拼接左端 / `Path(...)`、`os.path.join(...)` 首参）
> 一起找出来，交给 `MODULE_WRITE_BOUNDARIES` 注册表判定：`llm` 禁写、`cache`/`manifest`/`pending`
> 只许 `.ai/`、`assembler` 写 `cases/` 与 `.ai/`、`diagnosis`/`doc_quality` 只许 `reports/`、
> `workdir` 只许 `.ai/`、**T24 的 `reviews` 只许 `.ai/`**、未登记模块一律违规；目标不可静态判定的**从严判违规**
> （白名单纪律："写哪儿运行期才知道"本身就是要人工审的形态）。同名异义（`str.replace`/`dict.copy`）
> 不判、限定形态（`os.replace`/`shutil.copy`）才判；删除类留给 T8 运行期快照。自检含
> 违规/合规成对夹具 + bench/ 写盘点计数对账 + `interfacetester_ai/` 零违规断言，退出码 0；
> 护栏用例 `tests/write_boundary_test.py` 20 条（见 §12.8 末注记）。
>
> **★v8 T29 追加（2026-09-23，第六批）**：登记写入者新增**第四个**——`diagnosis.py`
> （**只写 `reports/`**：`analysis.md` / `doc_defects.md`，即 §5.3 的失败归因报告与
> **文档缺陷回流清单**）；T18 注册表同步为 6 项（`llm` 禁写 / `cache`·`manifest`·`pending`
> 只许 `.ai/` / `diagnosis` 只许 `reports/` / `assembler` 写 `cases/`+`.ai/`）。
> **扩白名单这件事本身被机器对账盯住**：未登记的 `diagnosis.py` 实测被判 2 处违规
> （`makedirs` + `open`，扫描器输出原文为"未登记模块出现写入类调用（白名单默认禁止）"），
> 显式登记进注册表后才 0 违规——"谁能写盘"必须签字，不是嘴上说说。
> **★v8 T27 追加（2026-09-23，第七批）**：登记写入者再增**第五个**——`doc_quality.py`
> （**只写 `reports/`**：文档缺项清单 `NEEDS_DOC_FIX.md`，即 §5.1 输入侧的"文档体检闸"
> 产物）；T18 注册表同步为 **7 项**（`llm` 禁写 / `cache`·`manifest`·`pending` 只许 `.ai/` /
> `diagnosis`·`doc_quality` 只许 `reports/` / `assembler` 写 `cases/`+`.ai/`）。
> **这次抓出的是"更细的一层"**：未登记的 `doc_quality.py` 被扫出 **3 处**写盘点——其中
> **1 处是模块自检里的夹具写盘**（`open(empty, "wb")` 造 0 字节样例）。处置**不是**把它也登记，
> 而是**把夹具写盘移进 `tests/`**（生产模块里只该留自己的写盘点）——红线③ 管的不只是
> "哪个模块能写"，也管"**它写的是不是它该写的东西**"。

### 2.3 只读 import 白名单（方案 4 §3.4，已逐条核实存在）

**只允许 import 这些内核公开面（其余一律禁止，尤其禁止 import 运行内核 `runner/step*`）**

| 用途 | 公开面 |
| --- | --- |
| IR 与导出 | `converters/__init__.py`：`IRCase` `IRStep` `emit_case` `validate_emitted_case` `build_case_dict` `SUPPORTED_SOURCES` `IMPLEMENTED_SOURCES` |
| 文件名归一 | `converters/emit_yaml.py::slugify` |
| 脱敏 | `converters/ir.py::sanitize_case`（凭据 → `${ENV(...)}` 单一收口） |
| 补丁/用例校验 | `loader.py::load_testcase_file`（唯一允许的"读用例"入口） |
| 生成期常量/护栏 | `make.py`：`BUILTIN_COMPARATOR_NAMES` `ensure_known_comparators` `normalize_module_segment` `ensure_generatable_teststeps` `ensure_json_schema_not_inline` `ensure_generated_python_is_valid` `ensure_generated_python_is_importable` |
| 字段白名单 | `models.py`：`KNOWN_REQUEST_FIELDS` `KNOWN_STEP_FIELDS` `KNOWN_CONFIG_FIELDS` |
| 断言归一 | `response.py`：`uniform_validator` `get_uniform_comparator` `_assertion_expects_null` |
| 脱敏判定 | `utils.py`：`is_sensitive_key` `mask_sensitive_variables` `mask_sensitive_request_data` `mask_sensitive_url` `get_os_environ` |
| 异常基类 | `exceptions.py::MyBaseError` |

> 实测要点（**容易写错、务必照抄**）：`converters/__init__.py` 的 `__all__` **恰好是这 7 个名字**（这就是"公开面"的口径）；
> 注意 `dir(interfacetester.converters)` 另会看到 `emit_yaml`、`ir` 两个**子模块对象**（由 `from ... import` 绑定产生），
> 它们**不在 `__all__` 里、不属公开 API**——白名单判据请按 `__all__` 写，不要按 `dir()` 写（v7 澄清）。
> `IMPLEMENTED_SOURCES = ("curl", "har", "postman", "openapi")`（四元 tuple）；
> 而 `SUPPORTED_SOURCES` 是**四键 dict**（能力声明），两者别混用。
> 判断"某个源能不能用"以 `IMPLEMENTED_SOURCES` 为准，**不要**读 docstring（该文件自己注明过历史的过期口径）。

### 2.4 必须复用、不得重造的护栏（+ v6 的"适配器形态自检" + v7 的"保真自检"）

| 护栏 | 位置 | 在 AI 链路里的角色 |
| --- | --- | --- |
| 导出即验证 | `converters/emit_yaml.py::validate_emitted_case` | 就是 L2 本体 |
| 算子白名单 | `make.py::ensure_known_comparators` | 臆造算子当场拦（内置 22 算子 ∪ debugtalk 函数） |
| 重复键硬闸门 | `loader.py::_DuplicateKeyRejectingSafeLoader` / `_reject_duplicate_json_keys` | `fix` 补丁必须先过它 |
| 内联 schema / XSD 存在性 | `make.py::ensure_json_schema_not_inline` / `ensure_xsd_files_exist` | 生成 `jsonschema_match` 时不许内联含 `$` 的 schema |
| 生成物合法性双兜底 | `make.py::ensure_generated_python_is_valid` / `ensure_generated_python_is_importable` | 非标识符用例名/目录名的最后一道 |
| 模块名归一 | `make.py::normalize_module_segment` | 用例名 → 类名/文件名（防两份文档撞同一个 `*_test.py`） |
| 生成器可生成性 | `make.py::ensure_generatable_teststeps`（含 `UNSUPPORTED_STEP_FIELDS`） | 一次收口"模型认、生成器不认"与请求体并存 |
| 三层写盘护栏 | `cli.py::main_convert`（`.yml` 名 / schema 名 / 生成物模块名）+ 孤儿 schema 报告 | 内核注册阶段必须接入 |
| L3 闸门 / 补丁校验 / prompt 对账 | `bench/ai_guardrails.py` | 直接搬进新包，别重写 |
| **失败分桶** | `bench/golden_harness.py::_classify` | §3.2 的报告口径。**实测可达桶只有三个** `load / make / compile`（`FAIL_PROJECT` 与 `FAIL_OTHER` 不可达，兜底分支先返回 `make`）→ **复用前必须先补"桶名断言"**，否则就是又一次"护栏打偏"（v7，§0.4-③） |

**★ v6 新增纪律：复用必须配"适配器形态自检"（§0.3-⑨）**

实测反例：把**步骤字典直接当成步骤**（即 `[{"eq": [...]}]`，而不是 `[{"validate": [{"eq": [...]}]}]`）喂给 `ensure_known_comparators`，
它**不报错、静默放行**——因为它的循环体是 `for raw_validator in teststep.get("validate") or []`，取不到 `validate` 就一次都不进。

> 这正是本仓自己命名的「**护栏打偏**」：闸门"存在"但"没被调用"。
> 所以凡是"复用内核护栏"的适配层，**必须各配一条形态自检用例**：先喂一个**必然违规**的样本证明它会红，再喂合规样本证明它会绿。
> 这与方案 5 §5.3 的两道元护栏、`tests/golden_baseline_test.py` 的注入式自检是同一套思路，v6 把它**推广到所有复用点**。

**★ v7 追加纪律：复用还必须配"保真自检"（判据 ↔ 分支 ↔ 样本）**

形态自检防的是"**护栏没被调用**"；保真自检防的是"**调用了、也红了，但查的不是文档写的那个东西**"。
两个实测反例（§12.7 乙③④）：

1. `_classify` 的桶名**从未被任何断言钉住**，文档却写着"已被 `golden_baseline_test.py` 钉住"；
2. S1 的判据写了 `contained_by`，代码里**零实现**（`MUST_REJECT` 也只有 3 条，恰好对应实现的 3 个分支）。

→ 所以每个复用点 / 每条闸门的自检**必须是两条**：

- **形态自检**：喂一个必然违规的**错误形态**，必须红；
- **保真自检**：**每一个判据分支都至少有一条 MUST_REJECT 样本，且样本编号与文档判据一一对应**（新增，落为 §3.5 第三道元护栏）。

### 2.5 交付形态：CLI 是真相源，Web 是薄确认台（方案 4 §二，本文整体保留）

**结论（一句话）**：CLI 是唯一真相源与执行面；Web 只是"薄渲染层 + 人工确认台"，**从 P3 起做、只读先行、可永远不做**。

**为什么"Web 优先"是错的**（每条都有本仓证据）

| # | 约束 | 证据 |
| --- | --- | --- |
| 1 | 客户执行环境是内网 + 自建 GitLab + 离线镜像，多一个常驻服务就是多一份部署/端口/证书/值守成本 | `docs/ci/README.md` 明写 GitLab CI 是「客户内网交付**首选**」；坑 3 专讲内网离线依赖 |
| 2 | 用例是入库文本资产，评审链天然是 git（MR / diff / 行级评论）；在 Web 里改 YAML 等于绕开 code review | `tests/generated_artifacts_drift_test.py` 用 **AST 等价**钉住"入库生成物必须与 YAML 一致"；`docs/ci/README.md` 坑 8 讲 `hmake` 会重写入库的 `examples/**/*_test.py` |
| 3 | 执行引擎是 pytest，不能塞进 Web 进程：嵌套 pytest 脆弱，且 `make.py::pytest_files_run_set` 是**模块级全局**（实测 `pytest_files_run_set: Set = set()` 定义在模块顶层，跨调用累积） | 源码实测；内核 `cli.py::main_run` 的做法是"收路径 → `pytest.main`" |
| 4 | 产物是文件（`reports/report.html`、`logs/*.run.log`、`summary.json`），air-gap 现场靠文件流转 | `docs/ci/README.md` 坑 4「总是产出两类报告（junit + html）」 |
| 5 | 本仓可信度建立在"响亮报错 + 明确退出码"上；Web 天然把失败柔化成 UI 提示（HTTP 200 + 红点） | `make.py::ensure_generatable_teststeps`、重复键硬闸门、`ensure_generated_python_is_importable` 全是这条哲学 |

**Web 真正的价值（不在"好看"，在三件 CLI 做不好的事）**

1. **把"待确认项"分发给不写代码的人（最大价值）**：`pending.json` 记的是"这个期望值文档没写"——能回答的是产品/后端/数据，他们不会装 venv、不会跑 CLI。Web 把清单变成表单：填完 → 生成 patch → 自动重跑 L2 → 出 diff 进 MR。
2. **把"逐条确认"从"逐个文件看"变成"一屏批量处置"**：20 份用例 × 5 条断言的 `source_quote` 核对、`fix` 逐条 accept/reject。
3. **看板与度量**：降级率（P1a 核心验收指标）、缓存命中率、花费估算、失败分布趋势——CLI 每次只打印一次，沉淀不了。

**Web 的最小可用面**

- **P3a 只读看板**：① 运行列表 ② 失败明细（读 `summary.json`，**必须走 §5.5 的正确指针** `details[i].records[j].data.validators.validate_extractor[k]`，**不要**用 v4 那种缺 `.data.` 的写法）③ 产物下载 ④ 度量页。硬约束：**零写盘、零执行**；前端纯静态 HTML + 原生 JS（不引框架）。
- **P3b 确认门**：`pending` 表单化回填 → Core 生成 patch → **自动重跑 L2** → 出 diff；`fix` 逐条 accept/reject（reject 必填理由）→ 仍**不写盘**；触发运行走**串行队列**，每作业一个隔离目录，结果落 `.ai/jobs/<job_id>/`。
- **坚决不做**：❌ 在线编辑 YAML 直接落盘；❌ 任何"自动 apply 修复"；❌ 多租户/用户体系/权限矩阵（先用内网反代鉴权 + IP 白名单）；❌ 在 Web 里"直接调 LLM 做校验"。

**Web 化新增的安全与合规面**

1. **越权执行**：Web 触发运行必须过与 CLI **同一个** L3 闸门（`check_l3_allowed`：默认关 + 回环放行 + 白名单显式放行）——"是网页点的"不构成放宽理由。
2. **密钥集中**：服务端集中持 key 意味着"拿到服务就拿到 key"；必须配合内网反代 + IP 白名单 + 只读密钥文件（不给前端任何透传接口）。
3. **审计留痕**：每次人工确认写 `.ai/reviews/<ts>-<manifest_hash>.json`：`{who, when, action: accept|reject, target, reason, evidence_hash}`。
4. **提示注入**：沿用结构性缓解（LLM 无工具、无文件/网络访问、输出受 JSON Schema 约束、外部文本进 `<untrusted-data>` 定界符）。
5. **产物降级可见**：降级率必须**显示在看板上**，不能只写日志。

**选型**：P3a 用 stdlib `http.server` + 静态 HTML（**先例是 `tests/mock_server.py`**——它已是"支持单独启动（`python tests/mock_server.py`）"的常驻入口，直接复用它的 stdlib 写法即可，离线最省事；**注：v6 曾写"`examples/*/mock_server.py`"，实测 `examples/` 下没有该文件**，见 §0.4-⑨）；P3b 用 FastAPI + uvicorn（可选 extra `web`，不选 Django）；日志流用 SSE（比 WebSocket 简单，断流不影响作业）；文件服务只读白名单根（`.ai/`、`reports/`、`logs/`），路径 `resolve()` 后校验前缀防穿越；`haify serve` 默认**只监听 `127.0.0.1`**，对外必须显式 `--host`。

---

## 三、闸门体系（v6 定稿、v7 保留的核心：把"五道关"的顺序与职责定死）

### 3.1 五道关的顺序（每一步的存在理由都是"前一步做不到"）

| 顺序 | 关卡 | 输入 | 联网 | 副作用 | 默认 | 失败动作 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | **L1 严格模型校验** | LLM 输出原始 JSON | 否 | 无 | 开 | 按报错**类别**回喂重试（≤2 轮） |
| 2 | **★ S 系列生成期闸门（11 条）** | 已过 L1 的结构化 dict | 否 | **无** | 开 | REJECT → `failed/` + `NEEDS_HUMAN.md`；PENDING → `<case>.pending.json` |
| 3 | **L2 导出即验证** | `IRCase` | 否 | **有（写 `*_test.py` + black）** | 开 | 同上回喂 |
| 4 | **装配器写盘** | 通过 L2 的产物 | 否 | 有 | 开 | 写 `cases/*.yml`（LLM 无权摸文件） |
| 5 | **L3 运行期冒烟** | 子进程 `hrun` | **是** | 真实请求 | **关** | 需沙盒开关 + 白名单；结果只写报告，**不影响 L1/S/L2 结论** |

**为什么 S 必须插在 L1 之后、L2 之前**（方案 5 §4.1，v6 保留并补一条理由）

1. **L1 之后**：闸门要求输入是**结构化 dict**，不是原始文本。
2. **L2 之前**：L2（`validate_emitted_case`）会**写盘**——其 docstring 明写「副作用是会在同目录生成 `*_test.py`（原本 `hrun` 也会产出），并做一次 black 格式化」。
   闸门必须在"有副作用的那一步"**之前**把废品挡掉，否则每次失败都要付一次写盘 + black（默认 **60s** 超时）的代价。
3. **★ v6 补**：L2 的 `black` 失败**只告警不阻断**（见 §3.4），所以 L2 的信号强度本身就低于 S 系列。
   把 S 放前面，也是让"最容易红的那道关"先红——**报错要响在便宜的地方**。

### 3.2 L1 / L2 / L3 定稿

| 级别 | 内容 | 备注 |
| --- | --- | --- |
| **L1** | LLM 输出 JSON → `schema.py`（`extra="forbid"`）逐字段拒非法；未知键/形态错当场点名 | 报错文本**按类别**回喂重试（≤2 轮） |
| **L2** | `IRCase` → `sanitize_case` → `emit_case` → `validate_emitted_case`（`load_testcase_file` + `make_testcase` 干跑 + black + `compile` + `load_project_meta`） | 源码 docstring 明写「导出即验证」，且异常**原样抛出**（刻意绕开 `main_make()` 的 `sys.exit(1)` 吞错） |
| ★ **L2 的副作用与隔离** | 必须在**工作区内的作业临时目录**（`.ai/work/<job>/`）里跑，跑完清理；作业**串行**或按 job 目录完全隔离 | 清理失败**不得静默**——报告"残留路径" |
| **L3** | 子进程 `hrun` 真发请求 | 需沙盒开关 + 白名单；结果只写报告 |
| ★ **报告口径** | L2 的失败原因必须**分桶** | 分桶已在 `bench/golden_harness.py::_classify` 实现 → **直接复用，不要另写一套**。**★v7 更正两处**：① 实测**可达桶只有 `load / make / compile` 三个**——`FAIL_PROJECT` 与 `FAIL_OTHER` 不可达（`_classify` 的兜底分支先返回 `make`），报告口径请按**三桶**写；② v6 写的"已被 `tests/golden_baseline_test.py` 钉住"**不成立**：该文件只钉通过率常量 `3/3`，`failure_buckets` 与 `r['bucket']` 仅出现在失败提示字符串里，**没有任何 assert**。→ 复用前先补**桶名断言**与 **`_classify` 的形态自检**（T19、§3.5）。**★v8 落地注记（2026-09-23）**：T19 已落地——`tests/golden_baseline_test.py::TestClassifyBucketsArePinned::test_classify_buckets_are_pinned`（代表样本逐条断言归类 + 三桶全可达 + `_classify` 源码不含 `FAIL_PROJECT`/`FAIL_OTHER` + `FAIL_*` 常量值对账），实测通过 |

### 3.3 S 系列：11 条生成期闸门

> 实现位置：`bench/silent_traps.py`（**已实现并实测**）。
> 判据一律是"**可精确判定的形态**"，不是"看起来可疑"——依据本仓明写的取向：
> `builtin/comparators.py` 的注释：「本仓的取向是「**只拦能精确判定的形态**」，**假报会让真报被无视**。」

| 编号 | 闸门 | 判定判据 | 严重级别 | 实现方式 |
| --- | --- | --- | --- | --- |
| **S1** | 伪存在性 / 空值真空 | `not_equal` 的 expect 为空值（`""`/`[]`/`{}`）**且** check 是路径表达式；`contains` 的 expect 为 `""`；`jsonschema_match` 的 schema 为 `{}`。**★v7 更正**：v6 曾把 `contained_by` 也列进本判据，但代码里**零实现**（`gate_s1_pseudo_presence` 只有上述**三个分支**），而且 `contained_by: [x, []]` 是**恒假**、根本不构成"真空通过"——列进来只会制造假报。**判据以实现的三分支为准，文档与代码必须一一对应**（§3.5 第三道元护栏） | **REJECT** | 新写 |
| **S2** | 字典序比较 | 4 个数值算子（`greater_than`/`less_than`/`greater_or_equals`/`less_or_equals`）的 expect 是**像数字**的字符串（`^-?\d+(\.\d+)?$`） | **REJECT** | 新写 |
| **S3** ★ | 断言真空 | **（v6 重定义、v7 扩展、v8 已按三态落地——T1~T3，见下）** | **REJECT / PENDING** | 新写 |
| **S4** | extract 未校验 | `extract` 的键既未被任何断言的 check 引用（**路径精确 / 完整路径段**），也未被任何步骤的 `$var`/`${var}` 模板用法引用（**词边界**；均跨步收集）。**★v8 落地注记（T21）**：旧实现的**子串匹配**（`any(n in c for c in checked)`）曾致 extract 键 `token` 被 check `body.data.token_type` 误判"已引用"而**静默漏报**（实测 §12.8-③）；现已改为路径精确 / 路径段 / 词边界三支判定，漏报对抗样本恢复报出（§12.8-③ 修复注记） | **PENDING** | 新写 |
| **S5** | 引号函数调用 | 任意字符串匹配 `\$\{ident\([^}]*['"]` | **REJECT** | 新写 |
| **S6** | 算子白名单 | 复用 `make.ensure_known_comparators` | **REJECT** | 复用 |
| **S7** | `type_match` 类型名 | expect ∉ 类型白名单 | **REJECT** | 新写 |
| **S8** | 内联 schema 含 `$` 键 | 复用 `make.ensure_json_schema_not_inline` | **REJECT** | 复用 |
| **S9** | 生成器可生成性 / 请求体并存 | 复用 `make.ensure_generatable_teststeps` | **REJECT** | 复用 |
| **S10** | 模块名撞车 | 复用 `make.normalize_module_segment` + 查重 | **REJECT** | 复用 |
| **S11** | 文档编码 | 复用导入器探测顺序 `utf-8-sig → utf-8 → gbk → gb18030` | **REJECT** | 复用 |

**★ S3 的重定义（§0.3-⑧ + §0.4-⑦：v6 定三态、v7 扩展"被引用"）**

前作判据是"请求型 step 的 `validate` 缺失/为空 → REJECT"。实测该判据**误伤多步链路的正常写法**，
且**它自己给出的改法无法执行**：

```
实测（probe_s3_review/probe_s3_edge.py；★v8 2026-09-23 T1~T3/T21 已落地，输出留档 out_probe_s3_edge.txt）：
① 登录步骤「只有 extract、没有 validate」（两步链路最常见写法）
     → findings: []   REJECT=0  PENDING=0   闸门结果: 通过
       （token 被下游 $token 与 check 引用 → S4 零 PENDING；S3 对"有 extract"不再硬拒）
② 按闸门提示写 validate: []（"显式声明意图"）
     → findings: ['S3']   REJECT=0  PENDING=1   闸门结果: 通过
       （三态之②：显式空 → S3 PENDING，如实标注，不再硬拒——提示的改法自此可行）
③ 对照：登录步骤补一条 eq: [status_code, 200]
     → findings: []   REJECT=0  PENDING=0   闸门结果: 通过

修复前的历史输出（v6/v7 实测，留作对照，见 §12.5 / §12.7 甲1）：
① → findings: ['S4','S3']  REJECT=1  PENDING=1  被拒
② → 与①逐字相同（提示的改法不可行）
③ → findings: ['S4']        REJECT=0  PENDING=1  通过
```

同一个 step 同时被判 **S4 PENDING**（"容忍 None 是合理权衡，闸门不替用户判死"）与 **S3 REJECT**，
这与方案 5 §3.4 自己声明的取向直接冲突。**v6 重定义、v7 按下表扩展为：**

| 情形 | 判定 | 理由 |
| --- | --- | --- |
| 请求型 step **既无 `validate` 也无 `extract`** | **REJECT** | 它既不断言也不产出，无论接口返回什么都被判成功——源码注释称其为「最坏的一种假通过」 |
| 请求型 step **有 `extract`、无 `validate`**，且该 extract 的键**被下游以任何形式使用**（断言的 `check`，或 `$var` 出现在下游的 headers / params / body / url 里） | **无 finding（放行）** | 它是多步链路的正常中间步骤，其价值由下游承担——这正是 S4 想要的结果。**★v7 扩展**：v6 只认"被下游**断言**引用"，那会把**最常见**的写法「登录取 token → 下游 `Authorization: Bearer $token`」永久判成 PENDING；而 v6 给的理由（"后续 `$var` 静默拿到 None"）在该形态下**不成立**——token 为 None 会得到 401，下游 `eq: [status_code, 200]` 会**响亮失败**（§12.7 乙⑦）。**★v8 警示**：这条豁免的"被使用"判据若沿用**子串匹配**，`$token` 会被 `$token_id`、check 会被 `token_type` 误判为"已使用"（**漏报**方向，与误报方向同样真实，§12.8-③）——T21 实现必须用**词边界匹配**（`$var` 后不得紧跟标识符字符；check 按路径段比对而非子串） |
| 请求型 step **有 `extract`、无 `validate`**，且该 extract 的键**完全无人使用**（既无断言引用、也无任何 `$var` 用法） | **PENDING（由 S4 报出）** | 后续若有人接上 `$var` 会静默拿到 `None`；"故意保留"是合理权衡，闸门不替用户判死。**前提**：S4 的"被引用"判据必须先按 v7 扩展到 `$var` 用法（T21），否则这一行会成为误报来源 |
| 请求型 step **显式写 `validate: []`** | **PENDING**，并在报告中如实标注"该步骤不做断言" | 显式声明**是有效的设计意图**，不能因为"空列表也是假值"就当成缺失 |
| 引用型 step（`testcase:`，无 `request`） | 放行 | 它是"调用另一个用例"，自己不写 validate 是合法的 |
| `config.skip` 显式声明不跑 | 放行 | 如实上报为 skipped，不算真空 |
| 整份用例**所有请求步骤**都零断言 | **REJECT** | 它会"一路绿到底"，但什么都没测 |

> **修正要点是取消"空/缺失"的二元判断，改成三态**：`缺失` / `显式为空` / `有断言`。
> 前作把它们压成二值（`not validates`），才导致了"提示的改法自己不可行"这个自相矛盾。
> 同时注意上表第 2、3 行**把"有 extract"这一情形按"下游到底用没用它"再分成两支**——
> 这正是 S3（形态判断）与 S4（覆盖判断）的职责分工：**S3 只管"这个步骤自己有没有产出或断言"，S4 才管"产出有没有被用上"。**
> **★v7 提醒**：这里的"用上"必须是**任何形式的使用**，不能只算"被断言引用"——否则最常见的多步登录链路会被判成需要人工确认的 PENDING，
> 而 PENDING 被无视的结局与 REJECT 被整体关掉是同一个（§9.3 G5）。判据的准确定义是 S4 的**头号实现要点**，写代码前先写这条用例：
> 「取 `token` → 下游 header 用 `Authorization: Bearer $token` → 闸门结果必须是**通过且零 PENDING**」。
> **★v8 落地注记（T21）**：这条用例已写进 `tests/silent_traps_test.py::TestS3ThreeStatesAndS4Precision::test_login_only_extract_passes_with_zero_pending`，实测通过（零 finding）。

**★ v7 新增纪律：S 系列必须做"三方对账"（防文档判据与代码分支漂移）**

这条纪律的由来很具体：v6 §3.3 声称 S 系列"**已实现并实测**"，但 S1 的判据里写着 `contained_by`，而代码里这个词**零出现**——
也就是**判据与实现早就漂移了，却没有任何机制会红**（§12.7 乙④）。所以每条闸门都必须能被三方对齐：

| 对账方 | 位置 | 检查方式 |
| --- | --- | --- |
| ① **判据** | 本文 §3.3 表格的该行 | 逐条比对 |
| ② **实现分支** | `bench/silent_traps.py::gate_sN_*` 里的每个 `if` 分支 | 逐分支清点 |
| ③ **红线样本** | `MUST_REJECT` 里编号为 `SN` 的样本 | **每个分支至少一条**，且编号与判据一一对应 |

自动化判据（写进 §3.5 的第三道元护栏）：`test_every_gate_branch_has_a_must_reject_sample`——
对 11 条闸门逐一断言"`MUST_REJECT` 中该编号的样本数 ≥ 该闸门的实现分支数"。任何一方增删而另一方没跟，CI 立刻红。

> 这一条也是 §一"纪律二"在闸门上的具体化：**"已实现"必须能被指认到分支，"已实测"必须能被指认到样本。**

**为什么 S1~S5、S7 必须新写（内核结构上做不到）**

| 闸门 | 内核为什么拦不了 | 生成期为什么拦得住 |
| --- | --- | --- |
| S1 | 运行期只拿到 `None`，"字段缺失"与"值为 null"**无法区分**；硬判失败会破坏 `eq: [x, null]`（该写法已被 `response.py::_assertion_expects_null` 刻意放过） | 看得到 expect 是**空值**，也就看得到"意图是存在且非空" |
| S2 | 字典序对 ISO 日期串是**合法**用法，运行期无法区分意图 | 期望值是否带引号在 YAML 里**静态可见** |
| S3 | "没有断言"在运行期表现为"步骤成功"，无从判断有无意图 | 静态可见 `validate` 是缺失还是显式为空 |
| S4 | 后续步骤拿 `None` 是合法容忍，运行期不知语义 | 静态可见该 extract 的键**有没有被用上**（被断言 check 引用，或被下游 `$var` 用在 header/param/body/url；**★v7 扩展**） |
| S5 | 引号写法**不报错**（正则不匹配即退化为字面量） | 静态扫描即可命中 |
| S7 | `type_match` 在运行期抛 `ParamsError`，但那时已经跑了 | 静态可见 expect 不是类型名 |

**S1 为什么是全场最高危**（实测复现，v6 / v7 两轮复核均通过）

- **算子级**：`not_equal` 的实现就是 `assert check_value != expect_value`；当 `check_value=None`、`expect_value=""` 时 `None != ""` 为**真** → **PASS**。
  同理 `jsonschema_match` 用空 schema `{}`、`contains` 的 expect 为 `""` 也是**真空通过**。
- **端到端**：在 `project-two` 真跑一份带陷阱的用例，`summary.json` 记录 `check_result=pass`、`check_value=None`、`case success=True`，只有一行 warning。
- **源码依据**：`response.py::_warn_value_not_found` 的 docstring 明写这类断言"在字段**根本不存在**时**通过**"，并解释为什么只告警不报错。
- **而这恰是 LLM 最会批量生产的东西**：让模型"给每个字段加存在性校验"，它会兴高采烈地写出一屏 `not_equal: [x, ""]`，**全部静默通过**。人写这个要手滑一次；模型写这个是一次几十条。

### 3.4 两级严重程度 + `formatting` 显式化

| 级别 | 语义 | 适用 | 落盘 |
| --- | --- | --- | --- |
| **REJECT** | 形态可**精确判定**为"意图与实现不符" → 必须拦 | S1/S2/S3(部分)/S5/S6/S7/S8/S9/S10/S11 | 进 `failed/` + `NEEDS_HUMAN.md`，不冒充成功 |
| **PENDING** | 属**语义**判断，闸门不替用户下结论 → 提请人工 | S3(部分)/S4 | 进 `<case>.pending.json`（**★v8 落地注记 2026-09-23：S-6 已落地**——`interfacetester_ai/pending.py`，落 `.ai/pending/<case>.pending.json`，与 §5.2 命名口径一致；闸门零副作用，落盘由调用方显式触发） |

> **★v8 现状标注**：上表的「S3(部分)」指 §3.3 重定义后的**目标态**——v8 落地前 `gate_s3_assertion_vacuum` **一律 REJECT**（probe ②：`validate: []` 仍被拒、输出与①逐字相同，§12.8-④），PENDING 态并不存在；v7（以及 v8 之前的独立评审）都把这行目标态当成了现状——这正是 §3.3 三方对账纪律要防的「**判据表写了分支、代码无此分支**」（§0.5-④）。
> **★v8 落地注记（2026-09-23）**：T1~T3 已落地——`gate_s3` 现为三态（缺失 + 无 extract → REJECT；缺失 + 有 extract → 交给 S4；显式空 → **PENDING**；非空 → 通过），probe ② 不再被拒（仅 1 条 S3 PENDING，输出留档 `probe_s3_review/out_probe_s3_edge.txt`）。**S-6 的落盘对象「S3(部分) 的 PENDING」自此有了可落之物——S-6 落盘亦已于同日第四批落地**（`interfacetester_ai/pending.py`：`write_pending_list(report, case)` → `.ai/pending/<case>.pending.json`，无 PENDING 不落盘；闸门零副作用由调用方显式触发，见 §12.8 末注记）。

**★ `formatting` 字段（§0.3-⑦）**：`make.py::format_pytest_with_black` 的 docstring 明写「超时/失败都**只告警**，不阻断 hmake」。
所以 L2 全绿时，产物可能是**未格式化、甚至 black 根本没跑成**的。这不影响正确性，但意味着 **"L2 通过率"这个指标本身含噪声**。闸门报告需额外输出：

```json
{"formatting": {"ran": true, "reason": "ok|timeout|missing_black|failed"}}
```

> **★v8 落地注记（2026-09-24，第八批 = §8.1 的 S-8 / 待办 T13）**：已实现于
> `interfacetester_ai/gates.py` —— `GateReport.formatting` 字段 + `probe_formatting()`
> + `with_formatting()`，`to_dict()` 里**始终**输出该键（消费者不必判空）。
> **取值口径在原四值之上做了显式扩展（实现补充三值，代码注释与本注记同步标注）**：
>
> | `reason` | `ran` | 含义 | 该采取的动作 |
> | --- | --- | --- | --- |
> | `ok` | `true` | black 跑完且成功 | — |
> | `timeout` | `true` | black 启动后被超时打断 | 调 `INTERFACETESTER_BLACK_TIMEOUT` |
> | `failed` | `true` | black 退出码非 0（产物语法问题等） | 修产物 |
> | `missing_black` | `false` | **black 不存在**（`OSError`） | **装依赖** |
> | `no_files` | `false` | 没有产物要格式化 | **查调用链** |
> | `not_probed` | `false` | 调用方**没探针**（报告默认值） | 接调用方（S-10 / P0a） |
> | `unknown` | `false` | 传入了无法识别的状态（**已降级留痕**） | 查调用方 |
>
> **为什么必须把 `missing_black` 与另两种 `ran=false` 分开**：前者的处置是"装依赖"，
> 后者是"查调用链"——混在一起会让整个字段退化成一个没有动作的布尔值
> （这正是 §9.3 型"字段有了、信息没了"）。
> **★硬约束（落地时发现，值得留档）**：`make.run_black` 是**写盘**操作
> （black 就地重写文件），所以**闸门不允许自己探针**——否则会破坏 S-6 的
> "闸门零副作用"口径。因此 `probe_formatting()` 必须由调用方**显式**调用，
> 报告只负责**携带**结论；未探针就是 `not_probed`（"没人问过"，与"问过且没跑"可分）。
> **元护栏（实测）**：把 `formatting` 从 `to_dict()` 删掉后，`run_selftest()` 返回
> **2**（非 0）——即"字段消失"这件事本身是可检出的，不会静默退化。

### 3.5 反例纪律（闸门能活下来的关键）

**为什么"宁可错杀"是错的**：一个只测"拦得住"的闸门会演化成"宁可错杀"的闸门，
那种闸门在真实项目里的命运是**被整体关掉**——那时它等于不存在。

**判据必须成对，且写进自检**（实测结果）：

```
静默陷阱闸门自检全部通过：
  拦得住（MUST_REJECT）：11 条对抗样本全部命中，编号正确
  不误伤（MUST_ALLOW） ：15 条合法写法全部放行
  批量维度            ：S10 模块名撞车可检出
  文档维度            ：S11 空文档/坏编码可检出
```

> **★v7：这段自检还缺一行。** 上述输出里**没有**"判据保真"这一项——
> 它应该再打印一行：`判据保真：S1~S11 每条判据 ↔ 实现分支 ↔ MUST_REJECT 样本 一一对应（11 条闸门全覆盖）`。
> 缺这一行，就是 S1 的 `contained_by`（文档写了、代码没有）能一路漏到现在的**机制性原因**（§0.4-④⑤、§3.3 三方对账、T20）。
> **★v8 注（2026-09-23，T20 落地）**：对账已以 **pytest 用例形态**落地（`TestGateBranchAccounting` 四用例，CI 级红灯）；自检输出**未**加打印行——对账效力由用例承担，与"打印一行"等价且更硬（用例会数 `findings.append` 调用点，打印行不会）。

**★ v6 新增 2 条 MUST_ALLOW**（对应 §3.3 的 S3 重定义）：

| 新增样本 | 为什么必须放行 |
| --- | --- |
| 「仅提取的中间步骤」（`request` + `extract`，无 `validate`） | 多步登录链路的正常写法，其价值由下游断言承担 |
| 「`validate: []` 显式声明不做断言」 | 有效设计意图；把它当"缺失"是**把三态压成二值**造成的假报 |

**元护栏（防"护栏打偏"；v6 立两道 → v7 目标四道。★v8 现状**：**四道全部落地**——前两道 v6 立于 `tests/silent_traps_test.py`；后两道（★标记）原是 T19/T20 交付物（v7 曾写成"已完成"、实测文件里无此函数，§0.5-②、§12.8-②），**已于 2026-09-23 当日补齐**：`TestGateBranchAccounting` 4 用例入 `tests/silent_traps_test.py`、`TestClassifyBucketsArePinned` 1 用例入 `tests/golden_baseline_test.py`，定向 pytest 28 passed + 56 subtests（见 §12.8-② 修复注记）**）**：

| 元护栏 | 做法 | 作用 |
| --- | --- | --- |
| `test_gate_actually_detects_injected_fault` ✅已落地 | 先验证干净样本**通过**，再注入 `not_equal: [body.token, ""]`，必须**变红** | 证明闸门真的在检查，不是空跑 |
| `test_allow_set_would_catch_overblocking` ✅已落地 | 单独验证 `eq: [body.x, null]` 必须放行 | 证明 ALLOW 集有效，能检出误伤 |
| **★ `test_every_gate_branch_has_a_must_reject_sample`（v7 提出；★v8 实测曾未落地 = T20 交付物；**✅ 2026-09-23 已落地**于 `TestGateBranchAccounting`，含分支对账 / 编号对账 / REJECT 样本对账 / 结构例外探针四个配套用例）** | 对 S1~S11 逐条断言"该闸门在 `MUST_REJECT` 里的样本数 ≥ 该闸门的**实现分支数**"，且编号与 §3.3 判据一一对应 | **证明"文档判据 = 代码分支 = 红线样本"三方没有漂移**——S1 的 `contained_by` 正是因为缺这一道才漏到现在 |
| **★ `test_classify_buckets_are_pinned`（v7 提出，随 §3.2；★v8 实测曾未落地 = T19 交付物；**✅ 2026-09-23 已落地**于 `tests/golden_baseline_test.py::TestClassifyBucketsArePinned`）** | 单独断言 `golden_harness._classify` 的**桶名集合**（`{"load","make","compile"}`，与报告口径一致） | 证明"分桶被钉住"这句话是真的——v6 那句话在 v7 复核实测为假（§12.7 乙③），**现为真** |

> **★ 本文把这条纪律推广到三处**（前作只用在闸门自检上）：
> ① **复用型护栏的适配器**（§2.4 的反例：形态喂错就静默放行）；
> ② **真实样本回归**——前作的自检集是 11 + 15 条**自造**样本，不足以支撑"不误伤"。
> 真实 golden 之所以没暴露 S3 缺陷，只因它们每个请求步骤都手写了 `eq: [status_code, 200]`。
> **所以 golden 扩容到 15 份时，必须把闸门挂上去统计真实误伤率**，并把结果写回 MUST_ALLOW 集。

> **★v8 落地注记（2026-09-24，第十批 = §十 T10）**：已实现 `bench/gate_false_positive_audit.py`——
> 对 15 份 golden 的**期望产物**逐份加载并跑 `check_testcase()`，统计 **REJECT（误伤）** 与
> **PENDING（不是误伤，但如实列出）**；**误伤即退出码非 0 并点名**（哪一份 / 哪个编号 / 消息）。
> **实测：样本 15 / 误伤 0 / 误伤率 0.0% / 带 PENDING 样本 0。**
> 这组数字的第二个用处：T21 加严 S4 判据（"被引用"扩展到 headers/params/body 的 `$var`、
> 按**词边界**匹配）时，文档把它的收益标成**未实测**——现在有了答案：**PENDING 噪声被压到 0**
> （§3.3 / §13 的相关行已回填）。
> **元护栏**：往 golden 目录注入一条**已知**静默陷阱（S1 伪存在性 `not_equal: [x, ""]`）→
> 审计**必须**把它报成误伤并给出编号 **S1**；否则"零误伤"可能只是"审计根本没在跑闸门"。
> ③ **★v7 新增：判据保真**——每个复用点/每条闸门都要能指认"判据 → 分支 → 样本"三元组（§3.3 的三方对账），
> 因为"**声称已被钉住**"比"忘了钉住"更危险：它会连"去验证的动机"一起消掉（§一 纪律二）。

**S2 的精度设计（"不误伤"的具体例子）**：S2 只拦**像数字**的字符串，正则 `^-?\d+(\.\d+)?$`：

| 期望值 | 判定 | 理由 |
| --- | --- | --- |
| `"100"` / `"9"` / `"3.14"` | **拦** | 明显是数字写成了字符串 → 字典序陷阱 |
| `"2026-01-01"` | 放行 | ISO 日期串，字典序 = 时间序，**合法** |
| `"1.2.3"` | 放行 | 语义版本串（两个点，不匹配正则），**合法** |
| `100`（数字） | 放行 | 正确写法 |

---

## 四、静默陷阱全景（方案 5 的证据基础，本文继承）

### 4.1 三分类

| 类别 | 含义 | 数量 | 处置 |
| --- | --- | --- | --- |
| **G 类** | 可在**生成期**判定 → 能做成硬闸门 | **11** | §3.3 的 S 系列闸门 |
| **R 类** | 运行期语义上无法判定，内核**刻意**只告警 | **6** | 闸门**必须放行**（误伤比漏拦更致命） |
| **X 类** | 证据/产物层面（非断言语义） | **4**（＋v7 新增 1 = **5**） | 流程与文档修正 |

### 4.2 G 类：11 条可闸门化的静默陷阱

| 编号 | 陷阱 | 实测现象 | 内核现状 |
| --- | --- | --- | --- |
| **G1** | **伪存在性断言（空值真空）** | `not_equal: [body.x, ""]` 在字段不存在时**通过**，整用例 `success=true` | 只告警（结构上无法判定） |
| **G2** | 数值算子两侧都是字符串 | `greater_than('9','100')` **通过**（字典序），数值上 9<100 | 只告警（日期串是合法用法） |
| **G3** | **断言真空**（用例跑绿却零校验） | 没有断言的 step 仍判成功（**这正是 S3 闸门要承担的**）；而空 `teststeps` **已不再只是 warning**——内核 **N33** 已硬拒（`make.py` 里 `not teststeps` 直接抛错，注释原文「空 `teststeps` 必须报错」） | **部分已收口**（**★v7 更正**：v6 这一格写的"空 `teststeps` 只 warning"已过期，且与 §3.3 的 S3 docstring 口径相反，见 §12.7 乙⑥） |
| **G4** | `extract` 取到的值未被校验 | 后续 `$var` **静默拿到 None** | 只告警 |
| **G5** | 函数参数带引号 | `${f('abc')}` **不被当函数执行**，退化成字面量 | 只告警 |
| **G6** | 算子名拼错恰好撞内置名 | `validate: - print: [...]` 判 **pass**（返回值被忽略） | **已收口**（复用） |
| **G7** | `type_match` 期望值不是类型名 | `type_match: [x, eval]` 表面"类型失败"实为用法错 | **已收口**（复用） |
| **G8** | 内联 JSON Schema 含 `$` 键 | `$schema`/`$ref` → `VariableNotFound`，报错点远离本行 | **已收口**（复用） |
| **G9** | 请求体并存 → 静默丢体 | `upload`+`data` → `data` 整段消失；`data`+`json` → json 被丢 | **已收口**（复用） |
| **G10** | 归一化后撞同一生成物 | 两个 YAML 落到同一个 `*_test.py`，**静默覆盖** | **已收口**（复用） |
| **G11** | 文档编码（BOM/GBK/CRLF） | BOM 让第一行变 `\ufeffcurl …`，**一条都解析不出来** | 导入器已有口径（复用） |

### 4.3 R 类：6 条**必须放行**的合法写法（闸门误伤清单）

| 编号 | 合法写法 | 为什么必须放行 |
| --- | --- | --- |
| R1 | `eq: [body.x, null]` | 明确期望 null（`_assertion_expects_null` 刻意放过） |
| R2 | `type_match: [body.x, "None"]` | 同上 |
| R3 | `greater_than: [body.date, "2026-01-01"]` | 字典序恰好等于时间序，**合法** |
| R4 | `string_equals: ["status_code", "200"]` | 数字被 `str()` 后比较，仓库与现场都在用 |
| R5 | `jsonschema_match` 用 `date-time`/`uri` format | 缺可选依赖，已告警；装 `jsonschema[format]` 可强制 |
| R6 | `extract` 取到 `None` 且后续确实容忍 | 多步链路的合理权衡 |

### 4.4 X 类：4 条证据/产物层面的修正（+ v7 新增 1 条 = 5 条）

| 编号 | 问题 | 处置 |
| --- | --- | --- |
| X1 | 证据指针路径缺 `.data.` | §5.5 已更正 |
| X2 | "逐字节一致"与内建 UUID 冲突 | §5.6 改归一化投影 |
| X3 | L2 通过 ≠ 产物可用（black 降级为告警） | §3.4 单独暴露 |
| X4 | `.ai/` 未进 `.gitignore`；工作区非 git 仓 | §5.7 分层判据 |
| **X5** ★ | **归一化投影的"完整剔除清单"漏剔了响应侧时间戳**：真实 `summary.json` 里 `records[*].data.req_resps[*].response.headers` 带秒级 `Date`（另可能有 `Set-Cookie`/`Server`），顶层 `platform` 也随机器变 → 按 v6 的清单实现，那条"投影后逐字节一致"的验收**照样恒红**（v7 新增，§0.4-①） | §5.6 清单已补全（**T17 已落地**：`bench/normalize_projection.py` + 9 条判据级用例，验收见 §十 T17） |

> **关于"穷举"的诚实说明**：G/R/X 的分类依据是对内核全包做 `静默|假通过` 关键词扫描共 **233** 处命中（仅顶层模块为 **148** 处），再逐条人工三分类。
> 这是**关键词穷举**，不是**语义穷举**——它给出的是"值得逐条看的候选集"，不是"陷阱全集"。
> 所以风险登记册里 G1（漏拦新形态）**永远不能标记为"已消除"**，只能标记为"已缓解"，且新增陷阱必须同步补 MUST_REJECT 样本。

---

## 五、功能点定稿

### 5.1 `haify gen`：文档 → 用例

| 项 | 定稿 |
| --- | --- |
| 输入 | **只收 md/txt**（Word/PDF 列 P0.5，先由用户转 md）；内容级脱敏闸先行（§6）。**★v8 T27 落地注记（2026-09-23，第七批）**：生成前先过「**文档体检闸**」**S-10**（`interfacetester_ai/doc_quality.py`，CLI `python -m interfacetester_ai.doc_quality <doc.md>`）——缺 URL/method/字段表、内部矛盾（同字段两类型、示例与表冲突）、坏编码/空文件 → **拒生成**并落 `reports/NEEDS_DOC_FIX.md` 交回文档作者；只缺可选要素（示例/枚举/状态码）→ **放行 + 提示**（不误伤）。落地时修正了"逐切片判必需项"的口径：**必需项按整篇判**（切片是 LLM 输入单位，字段表常与 URL 分列两节，按切片判会误伤） |
| 契约 | LLM **不吐 YAML**，吐 `CaseDraft` JSON（`extra="forbid"`）。每条 `validate` 必须带 `source_quote`（文档原句） |
| 装配 | `CaseDraft → IRCase/IRStep`（纯代码）→ `sanitize_case` → `emit_case` → `validate_emitted_case`；**LLM 无权摸文件** |
| 切片 | 按文档标题层级切块；**统一按"输入预算字节数"**：`INPUT_BUDGET_BYTES = 12000`（含 system + few-shot），切片留 15% 余量 |
| 修正环 | L1/S/L2 报错文本截断至 4KB 回喂，`max_rounds=2`，**按报错类别选回喂模板**；仍失败 → 进 `failed/` + `NEEDS_HUMAN.md`，**不产半成品** |
| 命名 | 写盘前用 `make.py::normalize_module_segment` **预算模块名并查重** |
| 溯源校验 | 装配器校验 `source_quote` 确实出现在该输入切片中；不命中 → 整条断言降级进 `unknowns`（把"不臆造"变成机器可检查的字段）。**★v8 待办注记（T28）：本行的"句在"校验 ≠ "意对"**——按 R11 口径，`source_quote` 命中只证明"文档里真有这句话"，**不证明"这句话被正确理解"**；字段名 / expect 字面量 / 限定词↔算子 / 示例值四类**确定性对账**由 §十 T28 补（**未落地前本行只做"句在"判定**，边界见 §9.2 的 R16） |
| 验收 | ① golden 集扩到 **15 份**（类别覆盖：单步/多步依赖/含 schema/含 XML/含业务错误码/编码对抗）≤2 轮后 L2 通过率 ≥ **实测基线**（先测后承诺）；② 生成物明文凭据 = 0；③ `source_quote` 不命中 100% 拦截；④ FakeLLM 下全量离线可跑；⑤ 输入预算字节约束成立；⑥ 缓存键含采样参数；⑦ L2 零污染 |

### 5.2 `haify gen --assertions-only`：断言补全

**确定性映射先做（不调模型）**：

| 文档里的说法 | 映射到的算子 | 注意 |
| --- | --- | --- |
| "必填" | `contains` / `type_match` | — |
| "类型是 str/int" | `type_match` | expect 必须是**类型名** |
| "范围 a~b" | `ge` / `le`（**实测确认为合法别名**，归一为 `greater_or_equals` / `less_or_equals`） | 期望写**数字字面量**，防字典序坑 |
| "枚举取值" | `contained_by` | — |

**实测确认的别名表（务必用这张，不要凭印象）**：

| 合法别名 | 归一为 |
| --- | --- |
| `eq` `equals` | `equal` |
| `ne` | `not_equal` |
| `gt` | `greater_than` |
| `ge` | `greater_or_equals` |
| `lt` | `less_than` |
| `le` | `less_or_equals` |
| `str_eq` | `string_equals` |
| `len_eq` | `length_equal` |
| ⚠️ `gte` / `lte` | **不是别名**——实测被 `ensure_known_comparators` 拒（`FunctionNotFound`）。**不要写 `gte`/`lte`** |

- 期望值缺失 → 写 `${ENV(TODO_<语义名>)}`（`parser.py::get_mapping_function` 把 `ENV` 映射到 `utils.get_os_environ`，缺变量会抛 `EnvNotFound` **响亮报错**），并登记 `<case>.pending.json`（**★v8 落地注记 2026-09-23：writer 已落地**——`interfacetester_ai/pending.py::write_pending_list`，落 `.ai/pending/<case>.pending.json`，items 与 `Finding.to_dict()` 同构）。**不用 `??`**（本 fork 不存在该语法，实测 `parser.py`/`loader.py` 无 `??` 处理）。
- **★v7 新增落点规则 → ★2026-09-26 已收紧（§十一 D7）**：`TODO_` 占位用例属「**已知未定**」——它能过 L1/S/L2，但运行期**必然** `EnvNotFound`，所以**只允许落 `.ai/draft/`，不进 `cases/`**（v7 原文只约束了这一类）。
  **现在这条规则覆盖全部产物**：`assemble()` **一律只写 `.ai/draft/`**（无论有无 `TODO_`），
  进 `cases/` 的唯一路径是 `promote_draft()`（`haify review --approve` + `.ai/reviews/` 留痕，T24）。
  理由（v7 的理由 + 2026-09-26 的实证）：一条**必然报错**的用例进了 `cases/`，CI 变红的原因却是
  "还没人确认期望值"——这等于**把人工待办伪装成质量信号**（§0.4-⑧、T24）；
  而**只断言 `status_code` 的薄用例**在零留痕的情况下进了 `cases/`（`cases/T1_2_认证方式.yml`，见
  `docs/未做完的待决策项.md` §九 9.4）——它**不报错**，只是让"没人看过的东西"与"已复核的东西"无法区分。
- **负例护栏（闸门固化，不再只靠 prompt）**：禁止生成"伪存在性断言"（如 `not_equal: [x, ""]`）——`extract` 取不到值时框架只告警，这类断言会**静默通过**。存在性断言必须写成可判定形态（`type_match: [x, "str"]`）。**这条已由 S1 硬闸门承担，prompt 只作辅助**。
- 验收：10 份文档的断言算子/字段合法性 100%（L2 全绿）；`unknowns` 召回人工抽检 ≥8/10；**伪存在性断言 0 条**。

> **★P0b 落地注记（2026-09-25）**：本节的四条确定性映射与"`TODO_` 只落 `.ai/draft/`"规则**已落地**（`interfacetester_ai/assertions.py` + 装配器的**落点硬规则**），并做了两处**实测驱动**的修正：
>
> **(a) 确定性映射不用 `${ENV(TODO_...)}`**——`type_match` 的 expect 必须是类型名（S7 硬闸门），而"文档没说类型"的正确处置是**不断言**（记 skipped），不是塞占位符；`TODO_` 留给"值确实存在、只待人工填"的场合。
> **(b) `TODO_` 落点规则是硬规则**（装配器内置，不靠调用方自觉）：带未确认期望值的用例**只许落 `.ai/draft/`**，并登记 `<case>.pending.json`（`code: TODO`）——否则 CI 变红的原因会是"还没人填值"，等于把人工待办伪装成质量信号。
>
> 实测口径（15 份 golden 逐份过、12 份产出用例 / 3 份被 S3 拒且原因成立、降级 0 条、伪存在性 0 条、可复现）见 §8.1 P0b 行的落地注记。


### 5.3 `haify analyze`：失败诊断（方案 3 的归因学 + 方案 4 的工程化）

| 项 | 定稿 |
| --- | --- |
| 输入装配（纯代码） | 遍历 `summary.json` 的 `details[]`：取 `success=false` 的用例 → 每个 `check_result=="fail"` 的 validator 生成 evidence 包：`{case, step, validator 字典, 同步骤其余断言结果, request 摘要(已脱敏), response body ≤2KB, run.log 路径 + 关键 WARNING 行}` |
| **归因维度（取自方案 3 §5.8）** | `真实缺陷` / `用例错误` / `环境问题` / `测试数据问题` / `文档与实现不符` / `未知待人工确认` —— **六分类，不合并**（**★v8 T29 已落地 2026-09-23**：新增「文档与实现不符（文档缺陷）」为第六类，理由见 §9.2 的 R15；实现与判据见 `interfacetester_ai/diagnosis.py`、验收见 §十 T29 ✅ 行） |
| 输出契约 | `DiagnosisResult = {cause_type: 上述**六类**之一, confidence, evidence_quotes: [...], reason, action}` |
| **证据优先（取自方案 3 §5.9）** | 每个结论**必须**引用日志/响应/断言失败点；**无证据时必须输出"未知待人工确认"，不得强行归因** |
| 引用校验 | 每条 `evidence_quotes` 必须是对应切片的**归一化后逐字子串**；不命中 → 该条强制改写为"未知，需人工"（降级），并在 `analysis.md` 标注降级原因 |
| ★ 归一化管道 | 顺序固定：去 ANSI → CRLF→LF / 空白折叠 → Unicode NFKC → JSON 反向解码 → 引号族统一；**保留偏移映射**以便在报告里定位 |
| 批量与成本 | 同因去重（相同 step + comparator + 期望值差异模式聚合成一类），一次调用 + 逐条引用；切片统一包 `<untrusted-data id=n>` |
| 验收 | 引用校验**未降级占比 ≥70%**；降级 100% 显式标"未知"；同输入两次运行**归一化投影后逐字节一致**；LLM 断网退出码 3 且无部分写；**归一化对抗样本命中率 100%** |
| 三条人工路径（取自方案 3 §5.11） | 「人工确认 / AI 重判 / 继续回看日志」——Web 确认台（P3b）承载 |

> **★v8 T29 落地注记（2026-09-23，第六批代码）**：上表已扩为**六分类**——第六类为
> 「**文档与实现不符（文档缺陷）**」，理由见 §9.2 的 **R15**（五分类下"文档写错"只能被塞进
> 「用例错误 / 真实缺陷」→ **缺陷永远回不到文档作者手里**、同类错误下次继续污染）。
> 已实现于 `interfacetester_ai/diagnosis.py`（**纯代码，不调模型**）：
> - **枚举唯一来源** `CAUSE_TYPES`（6 类）+ `validate_taxonomy()` 对账——**旧五分类会被检出
>   缺「文档与实现不符」**（这就是"分类枚举缺格必红"的机器形态，取向同 T20 的三方对账）；
> - **证据纪律机器化**（本表「证据优先」）：无证据 / 引用不是原文**逐字子串** / `文档与实现不符`
>   缺**双侧证据**（文档原句 + 实际响应）→ **一律降级"未知待人工确认"**（`confidence` 归 0）；
> - **回流清单** `reports/doc_defects.md`（哪一句 / 与实际哪里不符 / 建议改法）——无文档缺陷**不落盘**；
>   该模块是**登记在册的写入者**（T18 注册表新增 `diagnosis → ("roots", ("reports/",))`，见 §6）；
> - **口径边界（诚实）**：`normalize_text` 只做「去 ANSI → CRLF→LF → 空白折叠 → NFKC」的**最小可判子集**；
>   本表「★ 归一化管道」的「JSON 反向解码 / 引号族统一」归 **T7**，不假装完整（R14 纪律）；
> - **仍未做的部分**：`haify analyze` 的接入（evidence 包装配、LLM 调用、`analysis.md` 落地渲染）
>   仍在 **P1a**；"含错误文档" golden 类目随 **T9/P0a**；L3 沙盒探针反查为 P1a 内可选项。
> 验收四元组与护栏用例见 §十 **T29 ✅** 行；本轮为"文档级新增三条待办"中的**第①条落地**。

### 5.4 `haify summary`：结果摘要

- **数字全部由代码从 `summary.json` 渲染进 Jinja2 模板**（total/success/fail、按用例名前缀分组的失败分布、耗时 TopN）；LLM **只写 2~3 句导语**。
- 导语里的数字受**数字白名单正则**复核：模板里没出现的数字禁止出现在导语，违规即弃用纯模板版。
- **无 LLM 时模板版照常产出**——报告永不缺席。
- 验收：数字与 `summary.json` 全等；无 LLM 时 golden 对照通过。

### 5.5 ★ 证据指针的正确路径（§0.3-①，已用真实 `summary.json` 逐字段核对）

| 用途 | **正确**路径 |
| --- | --- |
| 定位某条失败断言 | `details[i].records[j].data.validators.validate_extractor[k]` |
| 该断言的比较子 | `.comparator`（已归一为 `equal`/`not_equal`/…） |
| 实际值 / 期望值 | `.check_value` / `.expect_value` |
| 结果 | `.check_result`（`"pass"` / `"fail"`） |
| 该用例的日志指针 | `details[i].log`（**注意：`log` 是 `records[]` 的兄弟，不在 record 里**） |

**实测结构（照抄即可）**：

```
summary.json 顶层        : success, stat, time, platform, details
details[i]               : name, success, case_id, time, in_out, log, records
details[i].time          : start_at, start_at_iso_format, duration
details[i].records[j]    : name, step_type, success, data, elapsed, content_size, export_vars, attachment
records[j].data          : success, req_resps, stat, address, validators     ← validators 在这里
records[j].data.stat     : content_size, response_time_ms, elapsed_ms
validators.validate_extractor[k] : comparator, check, check_value, expect, expect_value, message, check_result

实测断言：
  'validators' in records[j]            -> False
  'validators' in records[j]['data']    -> True
  'log' in details[i]                   -> True
```

> **★v7 更正出处**：v6 把这处错误记作「方案 4 §4.3/§4.5」，实测 v4 的错指针出现在 **§二**（P3a 看板那条：「读 `summary.json` 的 `details[].records[].validators`」）与 **§4.5**（`fix` 的产物行），**§4.3 里没有**（§12.7 ⑫）。
> 它写的是 `details[i].records[j].validators…`（缺 `.data.`）；`analyze` 与 `fix` 都靠这条指针定位断言，**照抄必错**。

### 5.6 ★ 确定性验收：归一化投影（§0.3-②）

**为什么不能要求"逐字节一致"**：内核给**每个请求**注入

```python
"interfacetester-Request-ID": f"interfacetester-{runner.case_id}-{str(int(time.time() * 1000))[-6:]}"
```

而 `runner.case_id = str(uuid.uuid4())`，`logs/<case_id>.run.log` 的**文件名本身**也是 UUID。
→ `summary.json` 与 `.run.log` **天然不可能逐字节一致**。这条验收从第一天就是红的，而且团队会误以为是"模型不稳定"。

**修订后的判据（★v7 已补全剔除清单；★v8 T11/T17 已落地：`bench/normalize_projection.py` + `tests/normalize_projection_test.py`，2026-09-23）**：

```
投影 out(x) = 剔除 / 归一化下列字段后的规范化 JSON 序列化：

【身份与时间】
  - 顶层 time.*                                           ★T11 落地时新发现的清单漏项：
                                                          summary 级 start_at / duration 每次
                                                          运行必变，不剔则"跨秒两次逐字节
                                                          一致"仍恒红（真实产物实测确认）
  - details[*].case_id
  - details[*].time.*                                   (start_at / duration)
  - details[*].records[*].elapsed / content_size
  - details[*].records[*].data.stat.*                   (response_time_ms / elapsed_ms)
  - details[*].log                                      (含 UUID 的 .run.log 路径)
  - platform                                            (OS / 解释器版本；跨机对照必不同，口径见下)

【请求侧】
  - details[*].records[*].data.req_resps[*].request.headers["interfacetester-Request-ID"]

【响应侧 ★v7 新增——v6 漏了这一条，所以 v6 的清单并不「完整」】
  - details[*].records[*].data.req_resps[*].response.headers 按**白名单**保留：
      只留 `Content-Type` / `Content-Length` / **头名含 `status` 子串的头**（如 `Status` / `X-Status-Code`）——即
`RESPONSE_HEADER_KEEP ∪ {n | "status" in n.lower()}`；判据的**精确形态**见 `interfacetester_ai/projection.py::_keep_response_header`
（★v8 按独立评审 P4 把原措辞"**与 status 相关的头**"改成可执行表述——原句不是标准 HTTP 类别、无法直接转成代码）；
      **剔掉 Date、Set-Cookie、Server、Age、ETag 等易变头**

【必须保留】
  - 断言结果主体（comparator / check / check_value / expect / expect_value / check_result）

判据：对同一输入跑两次，out(x1) == out(x2) 逐字节相等；
      两份只在"清单内易变字段"上不同的输入（Date 跨秒 / UUID / 耗时不同），
      投影后同样逐字节相等（T17 判据的可执行形态，实测见 §十 T17）
```

> **★v7 为什么必须补 `response.headers`（§0.4-①，X5）**：v6 的清单只剔了**请求侧**的 Request-ID，
> 但真实产物里**响应头同样带时间戳**。实测（§12.7 ①）：
> `details[0].records[0].data.req_resps[0].response.headers`
> = `{'Server': 'BaseHTTP/0.6 Python/3.12.10', 'Date': 'Tue, 22 Sep 2026 15:02:22 GMT', 'Content-Type': …, 'Content-Length': …}`。
> `Date` 是**秒级**的：两次运行只要跨过一秒，投影就不一样。**按 v6 的清单实现，这条验收从第一天起仍然是红的**——
> 而且团队会照 v6 自己的判断，把它误当成"模型不稳定"。所以一句话：
> **投影清单必须同时覆盖请求侧与响应侧，否则"归一化投影"只是把恒红从「字节」挪到了「投影」。**

> **口径提醒（v7 新增）**：剔掉 `platform` 后投影才**跨机可比**；若只做**同机**对照，可以保留 `platform`（信息更多）。
> 两种都行，但必须**显式写进用例名**（`test_projection_stable_same_machine` / `..._cross_machine`），不要含糊。

> **投影必须保留 `validators` 主体**——那才是"结论确定"的实质。
> 剔除时间戳不是放松要求，而是**把要求提准**：我们要的是"结论确定"，不是"字节确定"。

### 5.7 ★ "无污染"判据要分层（§0.3-④）

| 环境 | "无污染"判据 | 说明 |
| --- | --- | --- |
| `D:\learn\interface-tester`（**真仓库**） | `git status --porcelain` 只出现预期证据文件 | `.gitignore` 需补 `.ai/cache/ .ai/work/ .ai/jobs/ .tmp_golden/` |
| `D:\tester`（工作区，**非** git 仓，实测无 `.git`、无 `.gitignore`） | 跑前跑后目录快照 diff，只出现预期文件 | 无法用 `git status` |

> 实测确认：真仓 `.gitignore` 覆盖 `.venv/ logs/ reports/ .pytest_cache/ tmp_*/ probe_*/ .tmp_report/ .tmp_audit/ .env`，
> **不含** `.ai/`、`.tmp_golden/`、`bench/`。建议入库 `.ai/manifest/` 与 `.ai/reviews/`（审计证据、体量小、可复盘）。

### 5.8 `haify fix`：用例自愈建议

| 项 | 定稿 |
| --- | --- |
| 触发条件（写进代码，不靠 prompt） | 仅对「`check_value is None` + jmespath 路径类断言」与「`type_match`/字段名类失败」生成结构修改建议；`status_code`/业务码类失败**一律判"疑似真实缺陷、不给改"** ★**落地注记（§9.25，2026-09-27）**：这一行**没有放宽范围**——"扩面"是在**这两族之内把覆盖做全**，并且**每扩一分配一道更硬的闸**：① **容器约束**（`check` 的根 ∈ `body`/`headers`，候选**只在同一容器**里找、`new_check` **保留原根**；根不认识 → 拒）——改前无论根是什么都只在响应体里找候选并拼成 `body.*`，于是 `headers.X-Foo` 会被"修"成 `body.XFoo` ✗（改的是**语义**不是名字）；② **新族 `var_missing`**（`check` 形如 `${var}` 且取不到值 → 在**本 step 的 `extract`** 里找唯一相近变量改 `${new}`）——改前它走"路径失效"分支，会把 `${token}` 改成 `body.token` ✗✗；两条硬闸：候选**唯一** + **新名字的取值路径必须在响应里真能取到值**；`${fn(...)}` 与业务码语义的 `${code}` 仍**不改**（后者按业务码拒）。③ **值语义类**（期望值不符、含"字符串数字 ↔ 数字"这种 S 闸门点名的写法）**刻意仍然不给**：它们"可能是接口真改了行为"，给建议等于教人把 bug 当特性（R3）。④ 连带修掉一个真缺陷：`build_patched_text` **保留引号风格**（`yaml.compose` 的 mark 含引号，直接替换会让 `["${x}", "abc"]` 变成流式序列里的语法错 ✗ → 补丁过不了双前置 → "该给的建议一条也给不出来"）。判据：`tests/fix_test.py::TestTriggerScopeBoundary` **17 条**（正/负成对 + **2 条元护栏**：摘掉容器闸 → 必跨容器；摘掉"可解析"闸 → 取不到值的变量也会拿到补丁） |
| 产物 | unified diff（含 YAML 行号 + `summary.json` 指针 `details[i].records[j].data.validators.validate_extractor[k]`）+ 理由 + 风险提示 |
| ★ **双前置硬判据** | 补丁在**输出 diff 之前**必须先过 ① `loader.load_testcase_file`（重复键硬闸门）② **S 系列闸门**（红线⑦：禁止把"可判定"改成"不可判定"） |
| 写盘 | **零写盘**：退出码恒 0，`--apply` **永不存在**（人工自己贴） |
| 验收 | 零写盘（目标 YAML mtime 不变）；补丁必须过 `load_testcase_file` + S 系列；业务码变化样本 100% 拒绝给改；"字段改名"注入样本建议命中 ≥8/10 |

> **为什么 fix 也要过闸门**：`fix` 的典型触发场景正是「字段改名导致断言失败」。
> 若它把 `type_match: [body.data.token, "str"]` "修"成 `not_equal: [body.data.token, ""]`，
> 那是**从能判定改成不能判定**——自愈反而制造了一个假通过。

### 5.9 `haify debugtalk`：签名/加密函数草稿

- 生成函数草稿 + **已知答案测试**（`sign(输入) → 期望输出` 写进临时 YAML step），跑 L2 干跑 +（沙盒开关下）L3；**测试向量缺失时拒绝交付**并说明原因。
- **解析约束（源码级地雷）**：`parser.py::function_regex_compile` 的函数调用正则参数字符集是 `[\$\w\.\-/\s=,]`——**不含引号**。`${f('abc')}` **不会被识别为函数调用，且只告警不报错**（源码注释明写"带引号的调用会被原样保留成字符串（不报错）"）。
  → 标量参数裸写，字符串参数经 `config.variables` 以 `$var` 传入；生成物必须有往返测试（`${fn(...)}` 100% 被 `parser` 解析为函数调用）。
- 对已存在的 `debugtalk.py`：用 `emit_yaml.py::_defined_function_names` 查重，同名只提示不改写；新函数以**追加块 + diff** 呈现，**绝不整文件覆盖**。
- 验收：每个函数强制带已知答案测试；无测试向量则拒产；往返解析 100%。

### 5.10 （可选）内核注册 `hconvert --from doc`

- 前提：把 `cli.py::main_convert` 的硬编码 if/elif 改为**表驱动分派**，并让新源接入**三层写盘护栏**（实测存在：`.yml` 名 / schema 名 / 生成物模块名，经由 `normalize_module_segment`）与**孤儿 schema 报告**。
- 同步成本：新模块必须进《架构与调用链.md》模块地图（`tests/docs_consistency_test.py::TestArchitectureDocCoversEveryModule`）。
- 结论：**不阻塞任何其他工作，可以永远不做**。旁路包已覆盖全部能力。

---

## 六、LLM 接入层与 Prompt

| 项 | 定稿 |
| --- | --- |
| 协议 | OpenAI 兼容 `POST {base}/chat/completions`，`requests` 直连（内核已有依赖，**不引 LLM SDK**）；支持 `response_format={"type":"json_schema"}` 时启用约束解码，不支持则退化为"JSON mode / 纯文本 + 一次围栏剥离与重试修复" |
| 配置（全环境变量；**★不读项目 `.env`**——那是**用例** `${ENV(...)}` 的来源，属被测项目；本工具的运行期配置只从环境变量取，见第 2291 批实测纠正） | `INTERFACETESTER_AI_BASE_URL / _API_KEY / _MODEL / _TIMEOUT(默认60s；**★本地大模型必须调大**，见 §12.8-⑪) / _RETRIES(默认2) / _MAX_OUTPUT_CASES(默认20) / _ENABLE(默认on，仅对显式调用 haify 生效)`；缺 `BASE_URL/API_KEY` → 打印配置指引 + 退出码 2，**绝不隐式出网** |
| 采样 | `temperature=0` + 显式 `seed`（provider 支持时）+ `max_tokens` 封顶；**采样参数必须进缓存键** |
| 预算（**字节口径**） | `INPUT_BUDGET_BYTES = 12000`（含 system + few-shot + 反例）；输出预算按 `max_tokens`；总预算按任务计并在 CLI/Web 可见；耗尽**确定性停止**（固定退出码 + 报告"已花估算值/剩余"）。★2026-09-26 **实测注记**：`gen` 路径上"预算超限"是**片级失败 → 退出码 1**；`EXIT_BUDGET = 5` **当前无端到端入口**，已按"**保留码**"标注（手册退出码表 + `cli.py` 常量行注释 + 元护栏 `tests/ai_exit_codes_test.py`） |
| 预算计量**不得依赖 `usage`** | 本地模型/内网网关经常不返回 `usage`（或返回不准）。以**请求侧可计算的估算值**为准（字节/字符数 × 模型系数字段），`usage` 只用于**校准与显示** |
| 缓存 | key = `sha256(规范化 JSON{全部请求字段})`（`temperature`/`seed`/`max_tokens`/`response_format`/`base_url`/`model`/`prompt_version`/`system`/`user` 全进）；命中即回放；`--no-cache` 强制实调 |
| 审计 | 每次调用落 `.ai/manifest/<ts>-<input-hash>.json`：model、prompt 版本、输入 hash、采样参数、原始响应、重试轨迹、最终产物路径——LLM 的每次臆造都可复盘 |
| ★ **写盘边界**（v7 新增，落实红线③） | **★最新状态（2026-09-25）：T18 注册表共 13 项**（P3d 收口再增 `export`：**离线静态导出**只写 `.ai/export/`；★与它成对比的是看板 `web` **始终不在册**——导出是**另一个能力**，不是看板偷偷写盘）——`llm` 禁写 / 只写 `.ai/` 的 6 项（`cache`·`manifest`·`pending`·`workdir`·`reviews`·`jobs`）/ 只写 `reports/` 的 3 项（`diagnosis`·`doc_quality`·`analyze`·`summary` 中的 4 项）/ 装配器（`cases/` + `.ai/draft/`）。**★P3b-2 追加（2026-09-25）**：登记写入者**再增一个**——`jobs.py`（**触发运行**，只写 `.ai/jobs/<job_id>/result.json`）。★它与同批的 `web` / `confirm` 构成**有意的对比**：那两个**刻意不登记**（源码里没有任何写调用——看板零写盘、确认门把落盘**委托**给 `reviews`），而 `jobs` **会真的执行 + 真的写盘**，所以**必须**登记。同款纪律**第四次咬合**：未登记的 `jobs.py` 被 T18 判 **4 处违规**（`makedirs`×2 + `open` + `os.replace`），显式登记后 **0 违规**。**以下为历史口径（v7 原文）**：**允许写盘者只有三个**：装配器（写 `cases/`、`.ai/draft/`）、`cache.py`（写 `.ai/cache/`）、`manifest.py`（写 `.ai/manifest/`、`.ai/reviews/`）；**`llm.py` 一律不碰文件系统**（模型响应只以内存结构向上返回）。这既是红线③ 的**可执行形态**，也解释了为什么"缓存/审计要落盘"**不违反**红线③（§0.4-②）。**★v8 落地注记（2026-09-23，第四批）**：本行已机器可查——`bench/write_boundary_scanner.py`（T18）把本行写成 `MODULE_WRITE_BOUNDARIES` 注册表（另含 S-6 新增的 `pending.py` 只许写 `.ai/pending/`），`tests/write_boundary_test.py` 有注册表↔本行对账用例；`interfacetester_ai` 包（T6）在扫描范围内必须零违规。**★v8 T29 追加（2026-09-23，第六批）**：登记写入者**增至四个模块族**——新增 `diagnosis.py`（**只写 `reports/`**：失败归因报告 `analysis.md`、文档缺陷回流清单 `doc_defects.md`）；T18 注册表现共 **6 项**（`llm` 禁写 / `cache`·`manifest`·`pending` 只许 `.ai/` / `diagnosis` 只许 `reports/` / `assembler` 写 `cases/`+`.ai/`），注册表对账用例随之更新为 6 键。**扩白名单的动作被机器盯住**：未登记的 `diagnosis.py` 实测被判 **2 处违规**（`makedirs` + `open`），显式登记后才 0 违规——这就是"谁能写盘"必须签字的地方。**★v8 T27 追加（2026-09-23，第七批）**：登记写入者再增一个——`doc_quality.py`（**只写 `reports/`**：文档缺项清单 `NEEDS_DOC_FIX.md`）；T18 注册表现共 **7 项**。同款纪律**又抓了一次**：未登记的 `doc_quality.py` 被判 **3 处违规**——其中 **1 处是它**自检**里的夹具写盘**（`open(empty, "wb")`），处置是**把夹具写盘移进 `tests/`**（生产模块里只该留自己的写盘点），而不是"把它登记进去了事"。**★v8 T8 追加（2026-09-24，第九批）**：登记写入者**再增一个**——`workdir.py`（L2 作业隔离目录，**只写 `.ai/work/<job_id>/`**；扫描器按**首前缀**判定，故登记根为 `.ai/`）；T18 注册表现共 **8 项**。同款纪律**第三次咬合**：未登记的 `workdir.py` 被 T18 判 **3 处违规**（`makedirs` ×2 + `open`），显式登记后 **0 违规**。**并带出一条新的形态纪律**：写盘点必须**内联成 `.ai` 字面量开头的 `os.path.join(...)`**——写成"先算变量、再 `open(变量)`"会被判"**目标不可静态判定**"（与越界同级的违规）；这条已写进 `workdir.py` 的 docstring 与 §十 T8 ✅ 行。**★v8 P0a-C 追加（2026-09-25）**：写盘边界相关有一处**新形态**落地——`l3.py`（L3 运行期冒烟闸门）从 `bench/ai_guardrails.py` **搬迁**进包（bench 留转发壳，T6 模式）。**它不是"忘了登记的写入者"**（L3 是纯判断函数、**无写盘点**），此次咬合的是**搬迁方向**：若包内不复用而让 `llm.py` 反向 import `bench`，方向就反了（bench 是方案护栏脚本、包是产品）；若两边各留一份，L3 就成了双源——**而 L3 是"臆造请求别打到生产"的唯一屏障**，双源漂移的代价不可逆。同批还把 **prompt 渲染与 `PROMPT_VERSION` 收口到 `prompts.py`**：此前 bench 写 `"v2.0"`、包内写 `"0"`，而**缓存键取的是包内那个**——即"缓存命中的到底是哪个 prompt 版本"当时**是错的**（判据：`tests/interfacetester_ai_pkg_test.py::test_prompt_version_has_exactly_one_source`） |
| 测试替身 | `llm.py` 暴露 transport 注入点；单测一律 FakeLLM（golden 响应回放），**CI 不出网** |
| Prompt 动态渲染 | `prompts.py::render_system_prompt(task)` 从内核常量实时取：request/step/config 合法键、`BUILTIN_COMPARATOR_NAMES`、**别名表**；`PROMPT_VERSION` 进缓存键与 manifest，改动即换版本号；**禁止手抄**（`bench/ai_guardrails.py::audit_prompt_against_kernel` 已实现强制对账） |
| ★ 别名表的取法 | `response.py::get_uniform_comparator` 是**别名归一器，不是校验器**：实测它把未知名字**原样返回**（`gte → gte`、`equals2 → equals2`）。所以"渲染别名表"与"判白名单"**必须用两个不同的函数**：前者用 `get_uniform_comparator`，后者用 `make.ensure_known_comparators`（后者实测会拒 `gte`/`equals2`/`print`） |
| 内容闸 | 键名级 `is_sensitive_key` → 内容级正则（`sk-\w{20,}` / JWT / `-----BEGIN` / `(?i)(password\|secret\|token)\s*[:=]\s*\S+`）→ 命中即替换为 `${ENV(...)}` 占位并打印替换清单；确需原样送 → 用户 `--allow-raw` 显式确认 |

**★ Prompt 的硬规则（取自方案 3 §5.4 + 方案 5 §4.2，合并后必须全含）**

1. 不允许直接生成**无校验的任意代码**；
2. 对字段、算子、请求方法**强制白名单**；
3. **不能凭空制造**未说明的字段和状态码；
4. 敏感信息统一 `${ENV(...)}`；
5. 每条 `validate` 必须附 `source_quote`；
6. **反例必须进 prompt**（正面清单不够）：把 `not_equal: [x, ""]`、`greater_than: [x, "9"]`、`${f('abc')}` 作为**反例**写进 few-shot；
7. **失败诊断部分必须包含"无证据时输出未知"**（方案 3 §5.9 的核心纪律）。

> ⚠️ **prompt 是辅助，不是闸门**：方案 4 §4.2 曾把"禁止伪存在性断言"只写成一句 prompt 约束——v6 / v7 明确：**凡 S 系列能判定的形态，一律用代码闸门拦，prompt 只用来降低触发率**。

---

## 七、已知落地坑（合并方案 4 的 P-1~P-8 与方案 5 的 X1~X4，去重后 9 条 + **v7 新增 P-10** = 10 条）

> 筛选标准：① 会让某个验收标准**达不到**（不是"最好改"）；② 有明确判据可写成用例；③ 修补成本 < 1.5 人日。

| # | 坑 | 命中阶段 | 不修的后果 | 处置成本 |
| --- | --- | --- | --- | --- |
| P-1 | 输入预算口径自相矛盾（6K tokens vs 8KB） | P0a | 切片器与校验器打架，实现当天就卡住 | 0.5 |
| P-2 | 缓存键漏采样参数 → 假确定性 | P0a | "同输入同输出"的回归对照失效 | 0.2 |
| P-3 | L2 校验有副作用 + black 60s → 并发污染 | P0a/P3b | 仓库被生成物污染、多人互相覆盖 | 0.5~1 |
| P-4 | 证据引用逐字匹配被"噪声"打穿 | P1a | 未降级率 ≥70% **达不到**，验收红 | 1 |
| P-5 | 预算计量依赖 usage（本地模型常不返回） | P1/P0 | `--budget-tokens` 形同虚设 | 0.5 |
| P-6 | 文档编码（BOM/GBK/CRLF）→ P0 第一公里 | P0a | 第一份客户文档就读挂 | 0.3 |
| P-7 | `.ai/` 未进 `.gitignore` + 全仓扫描护栏 | P0a | 审计产物/缓存误入库或误入扫描 | 0.1 |
| P-8 | 两个"元护栏"会咬人（数字唯一来源 / 模块地图） | 全阶段 | 文档搬进源码仓后 CI 变红 | 0.3 |
| **P-9** ★ | **复用型护栏喂错形态会静默放行**（v6 新增，见 §2.4） | P0a | 闸门"存在但没被调用"= 护栏打偏，且**看起来全绿** | 0.2 |
| **P-10** ★ | **护栏保真度幻觉**（**v7 新增**）：文档写"已钉住 / 已实现"，实测**没有任何断言或分支撑着**它——两处实例：`_classify` 的桶名被写"已被 `golden_baseline_test.py` 钉住"（实际只钉 3/3）、S1 的判据含 `contained_by`（代码零实现） | P0a | 比 P-9 **更隐蔽**：P-9 还能靠一次形态自检发现，而"**声称已被钉住**"会连"去验证的动机"一起消掉（§一 纪律二、§3.3 三方对账） | 0.3 |

**P-1 输入预算口径（必须二选一，v6 / v7 选字节）**：中文 1 token ≈ 1.5~2 字节 → 6K tokens ≈ 9~12KB，**必然大于 8KB**，两处不可能同时成立。
**统一为字节口径**（可精确计算、跨模型稳定）：`INPUT_BUDGET_BYTES = 12000`；token 只用于**成本估算**，且必须标注"估算值"。
判据：单测对最长切片断言 `len(payload.encode("utf-8")) <= INPUT_BUDGET_BYTES`；超限时**报错 + 记录截断量**，**不允许静默截断**。

**P-2 缓存键**：key = `sha256(规范化 JSON{所有请求字段})`。判据：只改 `temperature` 必须 miss（单测用 FakeLLM 的调用计数断言）。

**P-3 L2 的副作用与并发**：① 生成过程会把 `*_test.py` 洒到 `cases/` 目录；② 多人/多任务同时生成时同一目录生成物互相覆盖；③ Web 并发触发会放大成"偶发错乱"。
处置：L2 一律在**工作区内的**作业临时目录跑（`.ai/work/<job_id>/`，工作区外会被沙箱挡住），跑完清理；**作业串行**（或每 job 独立目录）；产物经人工确认后才复制/合并到 `cases/`。
判据：L2 前后仓库工作树快照**不变**；清理失败必须报"残留路径"。

> **★v8 落地注记（2026-09-24，第九批 = §十 T8）**：已实现于 `interfacetester_ai/workdir.py`——
> `JobWorkspace`（`.ai/work/<job_id>/`，进出即建/删）+ `snapshot_tree()` / `diff_snapshots()` +
> `run_isolated()`（快照不一致 → `WorkspaceContaminated` **点名具体路径**）+
> `WorkspaceResidue`（清理失败报**残留路径**）+ 进程内**串行锁**。
> **口径细节（为什么不是"整个目录树"）**：快照比较的是**源码树**——忽略 `.ai/`、`logs/`、
> `reports/`、`__pycache__` 等**运行期产物目录**。这不是放水：把"本来就该变"的目录算成污染，
> 这条断言会在几天内被人整体忽略，而**被忽略的断言等于不存在**（同 §3.5 的取向）。
> **两处实测**：① 登记前 T18 判 `workdir.py` **3 处违规**，显式登记后 **0**；
> ② 自检连发 200 个 `job_id` **抓到一个真 bug**——时间戳后缀会撞，已改为进程内单调计数器。

**P-4 归一化管道（P1a 的头号风险）**：真实噪声清单——loguru 的 **ANSI 颜色码**、Windows **CRLF**、JSON 字符串里的 `\"` **转义**、**全角/半角标点**差异、中英文引号差异、Unicode 组合字符（NFD）。
处置：去 ANSI → CRLF/CR → LF、空白折叠、NBSP → 空格 → NFKC → JSON 反向解码 → 引号族统一；**同时保留偏移映射**。
判据：4 组对抗样本（ANSI / CRLF / `\"` 转义 / 全角）的引用命中率 = 100%；< 100% 则用例必须红（否则 70% 那个验收数字不可信）。

> **★v8 落地注记（2026-09-24，第九批 = §十 T7）**：已实现于 `interfacetester_ai/normalize.py`——
> 完整管道 + **偏移映射**（`normalize_with_map()` 的 `span()` / `locate()`，报告里能写"该引用位于原文
> `[a, b)`"而不是只说"它归一化后是子串"）；4 组对抗样本实测**引用命中率 100%**；
> `diagnosis.normalize_text` 已改为它的**薄封装**（原先自带一套最小实现——两套归一化各长一点，
> 是这类代码最典型的口径漂移，且症状"真证据被判成没证据"与"模型确实没找到证据"长得一样）。
> **判据成对（本仓纪律）**：只做"合并书写差异"不够——**归一化越强，编造的引用越容易命中**，
> 所以同时钉了 4 条**反例**（不同数字 / 不同汉字 / 大小写 / 连续空行）必须**保持不同**。
> **两条已登记的边界（诚实，R14 纪律）**：① NFKC 用**逐字符**归一化以保住偏移，
> 跨字符组合（如 `e` + U+0301）与整串 NFKC 可能不同——用对抗样本**实测**证明
> "在这四类真实噪声上逐字符 == 整串"，**不声称**永远相等；
> ② JSON 反向解码对"本意就是字面反斜杠"的文本会**过度解码**（Windows 路径 `C:\temp` 的 `\t`），
> 属刻意取舍（真实 I/O 里 JSON 转义远比裸反斜杠常见），且只影响**匹配宽松度**、不改变内容来源。

**P-5 预算计量**：FakeLLM 不返回 `usage` 时预算仍能正确触发停机（单测）。

**P-6 文档编码**：必须**复用导入器同一套编码探测顺序**（`utf-8-sig` → `utf-8` → `gbk` → `gb18030` → 兜底 + **明确报错**），不许直接 `open(path, encoding="utf-8")` 硬读；探测结果记进 manifest。
判据：4 组编码样本全部可读且切片正确；不可读时必须**报错**而不是产出空用例。

**P-7 `.ai/` 的入库与扫描策略**：`.gitignore` 增加 `.ai/cache/`、`.ai/work/`、`.ai/jobs/`、`.tmp_golden/`；**建议入库** `.ai/manifest/` 与 `.ai/reviews/`，并显式声明"哪些是产物、哪些是证据"。

**P-8 两个"元护栏"**：
- `tests/docs_consistency_test.py::TestBaselineNumbersHaveOneSource`（基线数字只有一处事实来源）
- `tests/docs_consistency_test.py::TestBuiltinComparatorCountHasOneSource`（算子数唯一来源）
- `tests/docs_consistency_test.py::TestArchitectureDocCoversEveryModule`（每个包内模块必须出现在《架构与调用链.md》模块地图里）

处置：所有数字**只引用不另立**——golden 数字唯一来源是 `tests/golden_baseline_test.py` 顶部常量，算子数唯一来源是 `make.py::BUILTIN_COMPARATOR_NAMES`；搬进源码仓前先跑一遍 `docs_consistency_test.py`。
**★v7 追加：每个数字必须带口径标签。** 同一份代码可以有多个都正确的行数口径——
`18,840` 是"**非空行**"（`Measure-Object -Line` 的默认行为）、`22,173` 才是**总物理行**、非空非注释是 `15,767`；
写"18,840 行"本身不算错，**不写口径**才让它变成不可复核的数字（§0.4-⑩、T23）。

> **一处现存的真实漂移**：`deploy.md` 的目录结构注释写「内置函数 + **21** 个断言算子」，而实测 `make.py::BUILTIN_COMPARATOR_NAMES` 长度为 **22**。
> **★v7 再补一处**：同一份 `deploy.md` 第 204 行的契约断言自检注释写「**第 19 个算子**（`jsonschema_match`）」——同一个"数字多来源"病的第二个实例。
> 这正是 P-8 说的"数字多来源"：**唯一来源是代码常量**，文档里的数字应当引用它而不是手抄。

**P-9 复用型护栏的形态自检**（v6 新增）：见 §2.4。判据：每个复用点配两条用例——违规样本必须红、合规样本必须绿。

---

## 八、路线图、验收与工作量

### 8.1 里程碑（全部验收标准可机器判定）

| 阶段 | 交付物 | 验收标准（可机器判定） | 人日 |
| --- | --- | --- | --- |
| **S-0 清单** | `bench/SILENT_TRAPS.md`（G/R/X 三分类 + 逐条实测证据） | 已完成 | 0.5 ✅ |
| **S-1 闸门实现** | `bench/silent_traps.py`（S1~S11 + 反例自检集） | 已完成 | 1.5 ✅ |
| **S-2 回归用例** | `tests/silent_traps_test.py`（**22 条用例 + 4 道元护栏全部落地**；第三/第四道 = T19/T20 **已于 2026-09-23 补齐**——`TestGateBranchAccounting` 4 用例，§12.8-② 修复注记） | 已完成（**★v8 2026-09-23 两步扩容**：S-2b 落地后 14→18 条（`TestS3ThreeStatesAndS4Precision` 4 条判据级用例，pytest 18 passed + 31 subtests）；T20 落地后 18→22 条（`TestGateBranchAccounting` 4 用例，与 `golden_baseline_test.py` 定向合并 **28 passed + 56 subtests**））；v7 的"4 道已完成"系虚标，v8 补齐后**名实相符**；**S3 三态与 2 条 MUST_ALLOW 原单列为 S-2b，现已落地** | 0.5 ✅ |
| **S-3 端到端对抗** | `bench/probe_silent_gate.py` | 已完成（**★v8 环境标注**：**交互控制台**下"对抗验证通过"；**重定向 + 默认 GBK locale**下曾 `UnicodeEncodeError`、**退出码 1**——**★v8 当日已修（T26 落地）**：入口 `reconfigure(encoding="utf-8", errors="replace")` 后重定向下退出码 **0**、对抗逻辑不变（仍拦 S1；★T21 落地后 S4 PENDING 按新判据不再出现——token 被下游 `$token` 引用，此为判据①的预期行为），§0.5-①、§12.8-①） | 0.3 ✅ |
| **S-2b 修 S3 三态** ★（**✅ 2026-09-23 已落地**） | 按 §3.3 重定义 S3（缺失 / 显式为空 / 有断言），补 2 条 MUST_ALLOW、改提示文案、扩展 S4 的"被引用"判据（含 headers/params/body/url 的 `$var` 用法 + **词边界**）、探针输出留档（= §10 的 T1~T3 + T21 + T25）——已按判据实现（`gate_s3` 三态 / `gate_s4` 路径精确 + 路径段 + 词边界三支、跨步收集） | **验收全过**：探针三例 ① `findings: []` 通过、② 仅 1 条 S3 PENDING 通过、③ 通过（留档 `out_probe_s3_edge.txt`）；「取 token → 下游 header 用 `$token`」**通过且零 PENDING**；自检 **11 拦/17 放**全绿（退出码 0）；pytest `18 passed + 31 subtests`；漏报对抗样本（token vs token_type / `$token_id`）恢复报出 | 0.55 ✅ |
| **S-4 接入 gen** ★（**✅ 2026-09-25 已落地，随 P0a**） | 写盘前置 `assert_testcase_passes`；REJECT → `failed/` + `NEEDS_HUMAN.md`（**落点细化**：`.ai/failed/<case>/NEEDS_HUMAN.md`——`failed/` 在工作区根会**越出写盘白名单**，细化理由见 `assembler.py` 模块 docstring） | 带陷阱产物不落 `cases/`；零污染——**实测**：`assembler.assemble()` 内 `from interfacetester_ai.gates import check_testcase` → `report = check_testcase(case_dict)` → 不通过则 `write_needs_human(...)`，**写 `cases/` 之前**；本批真模型跑动中 `user_info.md` 被拦、`exit=1`、未落 `cases/`（§12.8-⑩） | 0.3 ✅ |
| **S-5 接入 fix** ★（**✅ 2026-09-25 已落地，随 P2a**） | 补丁**双前置**（`load_testcase_file` + 闸门） | 把可判定断言"修"成不可判定形态的补丁被拒——**实测**：`fix.py::validate_patch()`（`interfacetester_ai/fix.py` L290 起，注释即"双前置（红线⑦）：补丁在**输出 diff 之前**必须过"）先写临时文件走真 `load_testcase_file`（抓**重复键**这类只在 YAML 文本层可见的问题），再过 S 系列；未过则**不出 diff** | 0.2 ✅ |
| **S-6 pending 落盘** ★（**✅ 2026-09-23 已落地，第四批**） | S3(部分)/S4 的 PENDING → `<case>.pending.json`（**★v8 落地注记**：T1 已落地，S3 的 PENDING 态**现已存在**（显式 `validate: []` → 1 条 S3 PENDING），落盘对象齐备——**落盘本身亦已落地**：`interfacetester_ai/pending.py::write_pending_list`（闸门零副作用，writer 由调用方显式调用；落 `.ai/pending/<case>.pending.json`，无 PENDING 不落盘） | 与 §5.2 的清单格式一致——**实测通过**：文件名 `<case>.pending.json` ✓、items 与 `Finding.to_dict()` 同构 ✓、`.ai/` 根形态 ✓（`tests/pending_list_test.py` 7 条全绿，见 §12.8 末注记） | 0.5 ✅ |
| **S-7 CI 门禁** ★v8（**✅ 2026-09-24 已落地，第八批**） | `bench/` 自检 + 闸门用例进 CI（**前置已满足**：T26 已当日落地——四个脚本编码统一，§0.5-①、§12.8-①；本行 0.2 已含 T26 的 0.1）——已实现：**两个平台各加一个 `guardrails` job（同构）**，跑**八条自检**（`bench/` 五个 + `python -m interfacetester_ai.{gates,doc_quality,diagnosis}`），任一非 0 即 job 红；**安装集刻意只装 `test`**（`xml`/`upload` 与护栏无关，装上去反而会掩盖"护栏对 extra 的隐式依赖"——本机**未装任何 extra** 时八条全绿，即为此口径的取证）。**动机（实测）**：改动前两个 CI 文件里七个自检脚本名**零命中**——CI 只跑 `pytest`，于是"有人改了闸门/扫描器、自检不再命中"**没有任何人会看见**（pytest 覆盖的是判据行为，不是"脚本自身还能跑通"）；这正是 §9.3 型"护栏漂移"在**流水线层**的同一形态 | CI 变红能挡住闸门回归——**验收四元组（实测）**：① `cmd /c "set PYTHONIOENCODING= && .venv\Scripts\python.exe <脚本> > f 2>&1"` × 8 → 各脚本打印自身自检结论、**退出码全 0**（八条逐条 `exit=0`）；② 两个 CI 文件 `yaml.safe_load` 通过、job 集合 = `{unit-test, guardrails, package, network-tests, examples-smoke}`、`guardrails` 段落含 `pip install -e ".[test]"` 与 **8** 条自检命令；③ `pytest tests/dependency_consistency_test.py tests/docs_consistency_test.py -q` → **36 passed / 2 failed**（失败仅 §5 的 2 项环境遗留）；④ `docs/ci/README.md` 交付物表与「验证清单」已同步（四 job → 五 job + 第 11 条 `guardrails job 绿`，含"怎么确认它真有在起作用"） | 0.2 ✅ |
| **S-8 X3 显式化** ★v8（**✅ 2026-09-24 已落地，第八批** = §十 **T13**） | 闸门报告输出 `formatting` 字段（§3.4）——已实现：`interfacetester_ai/gates.py` 新增 `FORMATTING_*` 七值枚举 + `probe_formatting()`（**复用内核 `make.run_black` / `get_black_timeout`，不重造**；四条异常分支与 `format_pytest_with_black` 的 except 分支**逐条对应**）+ `GateReport.formatting` 字段与 `with_formatting()`；`to_dict()` **始终**输出该键（未探针 → `not_probed`，消费者不必判空）。**★设计约束（落地时发现的硬约束）**：`run_black` 是**写盘**操作（black 就地重写文件），所以闸门**不允许自己探针**——那会破坏 S-6 的"闸门零副作用"口径（§3.4 落地注记）；状态只能由调用方**显式**探针后挂上 | "L2 通过但 black 没跑"可被看见——**验收（实测）**：① `python -m interfacetester_ai.gates` → 自检输出新增一行「L2 格式化（S-8）：formatting 字段可分 ok/timeout/failed/missing_black/no_files」，退出码 **0**；② `pytest tests/l2_formatting_test.py -q` → **19 passed / 31 subtests**，含**真跑 black** 分支（本机有 black：实测 `x=[1,2,3]` → `x = [1, 2, 3]`）；③ **注入式元护栏（实测）**：把 `formatting` 从 `to_dict()` 删掉 → `run_selftest()` 返回 **2**（非 0，判据变红）；④ `GateReport().to_dict()` 键集合 = `{ok, rejects, pendings, codes, findings, formatting}`（加字段不动既有键） | 0.2 ✅ |
| **S-9 闸门保真对账** ★v7（**✅ 2026-09-23 已落地**，= T19 + T20） | 三方对账用例（**判据 ↔ 代码分支 ↔ MUST_REJECT 样本**）+ `_classify` 桶名断言与形态自检（= §10 的 T19、T20）——已实现：`TestGateBranchAccounting`（分支/编号/样本三边对账 + S0/S10/S11/PENDING 分支行为级探针）与 `TestClassifyBucketsArePinned` | `test_every_gate_branch_has_a_must_reject_sample` 与 `test_classify_buckets_are_pinned` 全绿——**实测通过**（两用例均存在且 PASS，定向 pytest 28 passed + 56 subtests、退出码 0）；文档 §3.3 每条判据都能指认到分支与样本（S0~S11 共 12 个编号全部登记进 `GATE_FINDING_BRANCHES` 对账表，与 `findings.append` 调用点逐一相等） | 0.3 ✅ |
| **S-10 文档体检闸（生成前置）** ★v8（**✅ 2026-09-23 已落地，第七批** = §十 T27） | `interfacetester_ai/doc_quality.py`（与 `gates.py` 同层，**不调模型**）+ `reports/NEEDS_DOC_FIX.md` 产出：**生成前**判定"这份文档配不配生成"——D1 要素完整性（URL/method/字段表＝必需；类型/必填/枚举/状态码/示例＝可选）/ D2 内部矛盾（同字段两种类型、示例与字段表冲突）/ D3 可判定率 / D4 编码可解析（复用 `gate_s11_document_encoding`）；不合格 → **拒生成**（缺项清单交给文档作者）或降级"**只生成骨架 + `unknowns` 清单**"。NOTICE：本行 S-10 是 **§8.1 的阶段编号**，与 §3.3 的闸门 `gate_s10_*`（模块名撞车）**不同源** | 违规必红（缺 URL/method/字段表、同字段两种类型、示例与字段表冲突、坏编码/空文件 → 拒）；合规必绿（健康文档 0 finding；**只缺可选要素 → 放行 + 提示、绝不拒**）；**验收四元组（实测）**见 §十 T27 ✅ 行（CLI exit 0/2 + `tests/doc_quality_test.py` 21 passed） | 0.5~0.8（**暂不计入合计**，口径见 §十 顺序块） |
| **P0-0 骨架与资产搬运** | `interfacetester_ai` 建包（`llm/schema/prompts/cache/manifest/evidence`）+ 搬运 `bench/` 护栏（各配形态自检）+ golden 扩到 15 份 + 归一化管道独立实现（**★v8 落地注记 2026-09-23：T6 骨架已按第四批落地**——包已建（平铺就地布局 + `pyproject.toml`），gates/projection **整体迁入**（bench 留兼容壳、同一性断言防双源漂移）、pending 为 S-6 交付、其余六模块占位含边界纪律；**本行完整版（golden 15 份、L2 隔离等 P0a 范围）仍待做**——骨架 = §十 T6 的 1~2 人日口径，非本行 5.5~7 的完整口径；**★v8 T29 追加（2026-09-23）**：包内模块增至 **11 个**——新增 `diagnosis.py`（**完整实现**：六分类 + 证据纪律 + 文档缺陷回流，非占位），`tests/interfacetester_ai_pkg_test.py` 的骨架清单同步。**★v8 第九批追加（2026-09-24）**：包内模块增至 **13 个**（另有 `__init__`）——新增 `normalize.py`（**T7**：完整归一化管道 + 偏移映射）与 `workdir.py`（**T8**：L2 作业隔离 + 快照零污染断言 + 残留上报），两者均为**完整实现**；`tests/interfacetester_ai_pkg_test.py` 的 `EXPECTED_MODULES` 与包内 docstring 骨架表同步） | 新包内自检全绿；内核零改动（`git diff` 空）；每个复用点都有"违规必红/合规必绿"两条用例；最长切片 ≤ `INPUT_BUDGET_BYTES`；4 组编码/对抗样本可读 | 5.5~7 |
| **P0a 接入层 + 生成闭环** | `haify gen` + 切片器 + CaseDraft 契约 + 修正环 | ① golden 集 15 份 ≤2 轮后 **接口片 → 产出用例 ≥ 86.7%**（★2026-09-26 D4 决策后的口径：原写"L2 通过率 ≥ 实测基线"是**重言式**——闸门在前、落盘产物恒 100%；有效分母是**送进模型的接口片**，见 §12.8-⑩ ④ 与 `docs/模型基线测量方案.md`）；② 生成物明文凭据 = 0；③ `source_quote` 不命中 100% 拦截；④ FakeLLM 下离线全绿；⑤ 输入预算字节约束成立；⑥ 缓存键含采样参数；⑦ L2 零污染 | 5~7（**★v8 注记：待办 T28「引用-断言确定性对账」落于本阶段的装配器内**——对账入口就是装配器那次 `source_quote` 校验，见 §十 T28；**未落地前** §5.1 的溯源校验仍只做"句在"判定）。**★P0a 进度注记（2026-09-25）：阶段 A/B 已落地，C/D/E 未做——本行不标完成** | **A. CaseDraft 契约**（`interfacetester_ai/schema.py`）：字段白名单（`extra="forbid"` 的语义）+ **每条断言必带 `source_quote`**（"不臆造"的字段级要求）+ 一次报全且可定位（点名到 `steps[i].validate[j]`）；**★落地偏差（诚实登记）**：v8 原文写 pydantic，但本包**零三方依赖**（T6 立的纪律）→ **自实现等价严格校验**，偏差只在手段、判据一致。**B. 文档切片器**（`interfacetester_ai/slicer.py`）：按标题层级切块 + **完整标题路径 + 行号范围**（供 `source_quote` 校验与报告定位）+ **字节预算**（`8000 × 85%`）；**超预算单段 → 报错**（不静默截断）、**多段 → 切细**（判据成对）。**验收（实测）**：`pytest tests/case_draft_test.py tests/slicer_test.py -q` → **34 passed / 110 subtests**；两份自检 CLI 在"重定向 + 无 `PYTHONIOENCODING`"下**退出码 0**；**切片器在 15 份真 golden 文档（含 GBK 与 BOM）上全部切完**，且**按行号回填后与原文逐行相等**（"没有内容被丢掉"的机器形态）；`bench/write_boundary_scanner.py` → 包 **0 违规**（两个模块都是纯函数）。**★P0a 进度注记②（2026-09-25）：阶段 C 已落地，D/E 未做——本行仍不标完成**。**C. 接入层**：① `l3.py` **L3 运行期冒烟闸门**（自 `bench/ai_guardrails.py` 搬迁，bench 留转发壳；`llm` 真出网处调用，**拒绝即抛、不静默降级**）；② `prompts.py` **prompt 动态渲染真正落地**（`render_system_prompt()` 从内核常量实时取 + `audit_prompt_against_kernel()` 强制对账 + §六 硬规则 6/7 的**反例**与"无证据输出未知"进 prompt）+ **`PROMPT_VERSION` 单一来源**（此前后端两处不一致，而缓存键取包内那个——收口前"缓存命中对应哪个 prompt 版本"是错的）；③ `llm.py` **模型调用层**：OpenAI 兼容 `/chat/completions`（`requests` 直连、不引 SDK；**实测适配面含 Ollama/LM Studio/vLLM 等回环部署，以及 DashScope 兼容模式/DeepSeek/Moonshot/OpenAI 等云端**）+ **transport 注入点**（`FakeTransport` 离线回放 / `HttpTransport` 真出网）+ 围栏剥离退化路径 + 内容闸 + 缓存/审计**端口注入**（本模块**不写盘**）；**★落地细化（诚实登记）**：§六 写"缺 `BASE_URL`/`API_KEY` → 退出码 2"，实测该口径会**误伤本地回环**（Ollama 类本来就没有 key，强制填假 key 会让"缺配置"信号失效）→ 细化为"**回环免 key、云端必须 key**"，成对判据见 `tests/llm_transport_test.py`。**验收（实测）**：`python -m interfacetester_ai.l3 / .prompts / .llm` 三个自检**退出码 0**；`pytest tests/llm_transport_test.py -q` → **27 passed**（含"**没开沙盒时 L3 挡在发请求之前、请求器一次都没被调用**"、"4xx 不重试"、"改任一采样参数必 miss"、"内容闸不误伤普通文档"、"模块源码无写盘调用"）；`bench/ai_guardrails.py` 自检**仍 exit 0**（转发壳等价）；`bench/write_boundary_scanner.py` → 包 **0 违规**。**★P0a 进度注记③（2026-09-25）：阶段 D 已落地，仅剩 E——本行仍不标完成**。**D. 装配器**（`interfacetester_ai/assembler.py`，登记写盘根 `cases/` + `.ai/`）：`CaseDraft → IRCase`（纯代码，模型到此为止）→ `sanitize_case` → **草稿区** `.ai/draft/` emit + `validate_emitted_case` → **闸门前置**（S-4）→ **通过才复制进 `cases/`** → pending 落盘；**§十 T28 四类确定性对账**（① 字段名**词边界**（`token` ⊄ `token_type`）② expect 字面量出处 ③ 限定词↔算子 **REJECT** ④ 示例值硬编码）+ **覆盖矩阵**（断言数 vs 覆盖字段数）+ `unknowns` 落 `.ai/draft/`。**★三处落地决策（诚实登记）**：**(a) S-4 落点**——`failed/` 细化为 **`.ai/failed/<case>/NEEDS_HUMAN.md`**（工作区根的 `failed/` 会越出写盘白名单；"带陷阱产物不落 `cases/`"这条实质判据不受影响）；**(b) 顺序改为"先草稿区、过闸门才进 `cases/`"**——不是"直接写 `cases/` 被拒再删"：后者没法用"文件不存在"来证明删干净了，而现在**被拒的用例在 `cases/` 里从不曾存在**（可用 `glob` 断言）；**(c) 判据豁免**——`status_code` 等**协议级检查项**与 `type_match` 的**类型名**若不豁免会被误伤（已用 `_PROTOCOL_CHECKS` / `_TYPE_NAMES` 显式列出）。**验收（实测）**：`python -m interfacetester_ai.assembler` → 退出码 **0**（四类对账 + 溯源 + 覆盖矩阵）；`pytest tests/assembler_test.py -q` → **12 passed**（S-4「带陷阱产物不在 `cases/`」用空 schema 触发 S1 实测；产物过内核 `load_testcase_file`；查重硬失败；落点只在 `cases/`+`.ai/`）；`bench/write_boundary_scanner.py` → 包 **0 违规**（本次实测**抓到 4 处"目标不可静态判定"**——正是 T18 第四次咬合，已按 `pending.py` 先例改内联字面量前缀）。**★P0a 进度注记④（2026-09-25）：阶段 E 已落地——代码层收口；验收①⑦ 待真环境**。**E. 收口**：① `cache.py::CacheStore`（`.ai/cache/<key>.json`，命中即回放、**损坏按 miss**）+ `manifest.py::ManifestStore`（`.ai/manifest/<ts>-<input-hash>.json`，**原始响应照录**、每个调用留痕含命中回放那次）——把 D 阶段留的端口补成真实现；② `gen.py` **生成流程编排**（体检 S-10 → **内容闸脱敏** → 切片 → LLM → schema → 装配器；**修正环** ≤2 轮、4 类报错分别回喂、4KB 截断、**预算超限不重试**）；③ `cli.py` + `__main__.py` + `pyproject.toml` 的 `haify` 入口——退出码口径 **0/1（生成）/2（配置）/3（传输）/4（文档）/5（预算）**，`--fake` = **只许回放**（CI 不出网）。**边界**：`gen.py` / `cli.py` **都不写盘**（落盘全委托给登记写入者），故无需扩白名单。**验收（实测）**：`python -m interfacetester_ai` → 无子命令退出码 **2**；缺配置 → **2** 且带配置指引；`pytest tests/gen_pipeline_test.py -q` → **17 passed**（FakeLLM 离线，临时工作区）；**`haify gen bench/golden/auth_token_flow.md --fake`（空缓存）→ 退出码 3「FakeTransport 没有为输入 … 准备响应」**——证明 `--fake` **不会静默假成功**；`bench/write_boundary_scanner.py` → 包 **0 违规**（本阶段又抓到 `cache.py`/`manifest.py` 的 3 处"目标不可静态判定"，按先例改内联字面量前缀）。**★验收逐条对齐（P0a 的 ①~⑦）**：**② 明文凭据 0**（内容闸在**送出去之前**替换，实测 prompt 里无明文且清单不泄露）、**③ `source_quote` 不命中拦截**（T28 溯源 → 降级入 `unknowns`；引用造假 → 100% 不落用例）、**④ FakeLLM 下离线全绿**（17 条）、**⑤ 输入预算字节约束**（挡在发请求之前、`transport.call_count == 0`）、**⑥ 缓存键含采样参数**（第二次跑不再调模型；相同回喂**命中缓存**故重试零成本）**均已实测**；**★①（golden 15 份 ≤2 轮后 L2 通过率 ≥ 实测基线）与 ⑦（L2 零污染）已于 2026-09-25 用真模型实测**（Ollama `gemma4:latest`，四元组与逐篇数字见 **§12.8-⑩**）：11/15 篇有产出、13 条用例、**接口片 → 产出 13/15 = 86.7%**、**源码树零污染**；其中 2 篇被 **S-10 文档体检闸**拦下（`exit=4`、未调模型）、2 篇（**均为编码对抗文档**）被 **S3 断言真空**拒（模型给了零断言草稿——**这正是闸门该拦的**）。★**并发现验收① 本身要改口径**："对最终产物量 L2 通过率"**恒为 100%**（装配器**闸门在前**，没过闸的根本不落盘）→ 是**重言式**；可证伪的判据应是"**接口片 → 产出率**"（本次基线 **86.7%**）——该口径变更**已于 2026-09-26 由 D4 决策人确认**（§12.8-⑩ ④；口径唯一来源 `docs/模型基线测量方案.md`）。代码侧的"只写登记根"已由 `tests/gen_pipeline_test.py::TestEndToEndProducesRealCases::test_new_files_only_under_registered_roots` 钉住。 |
| **P0b 断言补全** | `haify gen --assertions-only` + `pending.json` | 10 份文档断言合法性 100%；`unknowns` 抽检 ≥8/10；**伪存在性断言 0 条** | 2~3（**★P0b 落地注记（2026-09-25）**：`assertions.py` **确定性映射**（类型 / 范围 / 枚举 / 响应说明句 → 白名单算子；**不调模型、可复现**）+ `haify gen --assertions-only`（**不需要 `BASE_URL/API_KEY`**）+ **`TODO_` 落点硬规则**（带未确认期望值的用例只许落 `.ai/draft/`，§5.2）+ `pending.json` 登记（`code: TODO`）。**★两处落地修正（诚实登记，都是实测踩出来的）**：**(a) 确定性映射里不用 `TODO_`**——`type_match` 的 expect **必须是类型名**（**S7 硬闸门**会拦 `${ENV(...)}`，而且拦得**对**）；而"文档没说类型"的正确处置是**不断言**（只记一条 skipped 说明），不是塞占位符——`TODO_` 的正当用途是"**值确实存在、只待人工填**"（模型通路用）。**(b) `_word_boundary_hit` 里的 `.` 必须算边界**——D 阶段把 `.` 也当"词内字符"（本意是防 `token ⊂ token_type`），结果 `userId` 在引用句 `- data.userId：字符串` 里**不命中**，于是**响应说明句推出来的断言全被降级**（实测：`get_user_profile`/`login`/`paged_product_list`/`search_products` 四份共 10 条）；改成"`.` 算边界、`_`/`$` 不算"后降级**归零**，且 `token ⊄ token_type` 的判据**仍绿**。**验收（实测）**：`python -m interfacetester_ai.assertions` → 退出码 **0**；`pytest tests/assertions_test.py -q` → **17 passed**；**15 份 golden 逐份过**——**12 份产出用例**（**断言 0 降级、算子全在 `BUILTIN_COMPARATOR_NAMES`、引用句逐字可溯源、真的走完装配链即 L2 绿**），3 份（`delete_session` / `soap_user_service` / `user_info`）被 **S3 拒**（它们**真的没有可确定断言**：无字段表，或唯一那条被降级）——**闸门拒得对**，不是我们漏了；**伪存在性断言 0 条** ✓；**可复现**（同一文档两次推导 → 断言逐字节相同；两次装配 → 产物逐字节相同，仅用例名不同）。**★仍未做（不虚标）**：`unknowns` 召回的**人工抽检 ≥8/10** 是人工验收动作，本机不做；**模型通路产 `TODO_` 尚未在真模型上验**（本机无 `BASE_URL/API_KEY`）。 |
| **P1a 失败诊断** | `haify analyze` → `reports/analysis.md` | 未降级 ≥70%；降级 100% 标"未知"；投影后逐字节一致；断网退出码 3 且无部分写；归一化对抗样本命中率 100% | 4~6（**★v8 注记：归因六分类与 `doc_defects.md` 回流已随 T29 落地**——`interfacetester_ai/diagnosis.py`（枚举 / 证据纪律 / 回流渲染，见 §十 T29 ✅ 行与 §5.3 落地注记）；**本行其余部分（evidence 包装配、LLM 调用、`analysis.md` 端到端落地）仍待 P1a**）。**★P1a 落地注记（2026-09-25）**：`evidence.py`（证据包装配，**从占位变实体**）+ `analyze.py`（端到端）+ `prompts.render_diagnosis_system_prompt/user_prompt`（六分类清单**从 `CAUSE_TYPES` 实时取**，不手抄——避免"提示说五类、校验要六类"那种漂移）+ CLI `haify analyze`。**★三处口径决定（都是实测踩出来的）**：**(a)「模型自报未知」算未降级**——否则模型越守规矩、"未降级占比"越低，信号正好读反；判定看**类别变化**、不看 `reason` 里的措辞（`normalize_diagnosis` 据此新增"已经是未知就直接放行、只把置信度归 0"的出口）。**(b) 引用侧要 `strip()`**——模型从多行文本里挑引用几乎总带首尾空白/换行，而 `normalize_text` 只折叠**内部**空白（它的投影口径不能动：改了会动 `projection` 的逐字节承诺）→ 不 strip 会把**真证据判成没证据**，而报告里看起来与"模型没找到证据"一模一样（实测：`"  库存不足\r\n"` 不命中）。**(c) `log` 引用要区分「没有可查的原文」与「原文里没有」**——`validate_diagnosis` 的契约只声明 doc/response 两个参数，调用方没提供日志原文时按放行处理（否则会把「证据不足」伪造成「证据造假」）；P1a 落地后 `analyze` 传的是 `pack.haystack()`（响应体 + 日志关键行），于是 log 引用**真的**被逐字校验——**兑现了 T29 留的那行 TODO**。**验收（实测）**：① `python -m interfacetester_ai.evidence` / `.analyze` → 退出码 **0**；② `pytest tests/analyze_pipeline_test.py -q` → **27 passed**；③ **未降级占比**写进报告本身（`kept_ratio`，口径可复算）；④ **编引用 → 降级「未知待人工确认」**（类别落未知 + 置信度归 0 + 标出降级原因）；⑤ **断网 → 退出码 3 且 `reports/` 未创建**（"先渲染后落盘"的顺序决定的，不是碰巧）；⑥ 同输入两次报告**逐字节相同**；⑦ **对抗样本**（ANSI/CRLF）归一化后命中，而编的仍被拒（成对）；⑧ `bench/write_boundary_scanner.py` → 包 **0 违规**（`analyze` 为**第七个**登记写入者 `reports/`；`evidence` **刻意不登记**——它只读不写，而登记本身就是一次"签字"）。**★仍未做（不虚标）**：~~真模型跑一次（本机无 `BASE_URL/API_KEY`）~~ → **★2026-09-26 已跑（M-6）**：`haify analyze` 对真实失败 summary **退出码 1**（模型返回非 JSON、未产出 `reports/analysis.md`；`.ai/manifest/` 有留痕、可复盘），**并发现"analyze 无修正环"**（`gen` 有 ≤2 轮回喂）——见决策清单 §四 M-6；"含错误文档" golden 类目随 T9；L3 沙盒探针反查。 |
| **P1b 结果摘要** | `haify summary` | 数字与 `summary.json` 全等；无 LLM 时模板版仍产出 | 1~2（**★P1b 落地注记（2026-09-25）**：`interfacetester_ai/summary.py`（纯代码渲染 + 导语复核 + 原子写）+ `prompts.render_summary_system_prompt` + CLI `haify summary`。**★偏差（诚实登记）**：本行原文写"渲染进 **Jinja2** 模板"，而本包**零三方依赖**（T6 纪律）→ **自实现等价渲染**（偏差只在手段、判据一致，同 P0a 阶段 A 的处置）。**★两处口径决定（实测踩出来的）**：**(a) 白名单必须"扫模板"、不能凭直觉列字段清单**——`3` 看起来像违规（"哪来的 3？"），其实它是**耗时 TopN 的序号**、真在模板里；凭直觉列清单时正是这种数会被误判成违规，把合规导语白白丢弃（自检自己就踩了一次，已留成判据 `test_topn_ordinal_counts_as_a_template_number`）。**(b)「弃用」必须落到正文**——只把状态改成"已弃用"而导语还留在报告里，等于没弃用（`test_discarded_lead_is_absent_from_report` 是这条的元护栏）。另两条口径标签：耗时取**用例级** `details[i].time.duration`（不是步骤级 `elapsed`——混用就没法比）；失败分组按**第一个 `_` 之前**、没有 `_` 的**各自成组**（中文用例名很常见，不写清就会出现"看着像分组其实没分"）。**验收（实测）**：① `python -m interfacetester_ai.summary` → 退出码 **0**；② `pytest tests/summary_test.py -q` → **25 passed**；③ **数字与 `summary.json` 全等**——含**真产物** `project-two/logs/probe_silent.summary.json` 逐字段对账；④ **无 LLM 时模板版仍产出**（`transport=None` / 断网 / **缺配置**三种情形都有报告，且**都不算错误**——CLI 退出码 **0**，与 `gen`/`analyze` 的"缺配置 → 2"是**故意**的分野：那两件事没有模型就没有产物，摘要的产物本来就是代码渲染的）；⑤ 导语合规 → 采用（围栏剥掉）、模板外数字 → **整段弃用且正文里真的没有它**；⑥ 报告**逐字节稳定**、原子写无 `.tmp` 残留；⑦ `bench/write_boundary_scanner.py` → 包 **0 违规**（`summary` 为**第八个**登记写入者 `reports/`）。**★仍未做（不虚标）**：~~真模型跑一次导语（本机无 key）~~ → **★2026-09-26 已跑（M-6）**：`haify summary` **退出码 0**、产出 `reports/summary.md`，且**导语因"数字未在模板中出现"被判违规、整段弃用**（模板正文照常产出）——§5.4 的导语白名单复核**在真模型上第一次生效**（见决策清单 §四 M-6）；§7 的"看板与度量"（缓存命中率/花费估算/失败分布趋势）仍是 **P3a** 的活） |
| **P2a 自愈建议** | `haify fix`（只读 diff） | 零写盘（mtime 不变）；补丁过双前置；业务码变化 100% 拒绝给改 | 3~4（**★P2a 落地注记（2026-09-25）**：`interfacetester_ai/fix.py` + CLI `haify fix`。**★四层防线都落成机器判据**：① **触发条件写进代码**（§5.8 原文"不靠 prompt"）——`classify_failure` 只放行「路径取不到值」与「`type_match` 类型不符」，**`status_code`/业务码类一律 `REFUSE_REAL_DEFECT`**，且**先判"不给改"再判"可建议"**（顺序本身就是判据：`body.code` 同时满足两个条件，顺序写反就会给它出建议——而那是最不该碰的一类）；② **业务码的末段判定**（`code|status|state|errno|error_code|ret|result_code|returncode`）——只看**末段**才分得开 `body.code`（拦住）与 `body.code_name`（不拦）；③ **双前置在输出 diff 之前**（`load_testcase_file` 验 YAML **文本层**的重复键 + S 系列闸门验"没把可判定改成不可判定"，红线⑦）；④ **零写盘**：目标 YAML 的 mtime 不变，临时补丁只落 `.ai/work/<job_id>/`（`workdir.JobWorkspace`——**已登记**的写入者），所以 **`fix` 刻意不进 T18 注册表**。**★两处实测驱动的口径修正**：**(a) 改名匹配必须双向包含**——只认"完全同名"时"去前缀"方向漏掉；改成「精确优先，其次**双向**包含（`token` ↔ `access_token`）」后 8/10 ✓。**(b) 闸门拦下的补丁算"未命中"**——`body.items.token` → `body.items[].token` 那条：候选找对了，但补丁没过闸门 → **不给建议**（正确行为）；把它算进命中率会让指标变成"我们多敢猜"，所以**命中口径故意严**（只算"给出了合规建议"）。**★产物形态**：`yaml.compose` 取节点 `start_mark`/`end_mark` 的**行号 + 列区间** → 文本级替换**只动那个字面量**（**不做 round-trip**：那会丢注释、重排格式，diff 会退化成"整文件重写"）；同一条断言上改 `check`/`expect` 两处时**从右往左**替换（否则先改左边会挪动右边列号）。**验收（实测）**：① `python -m interfacetester_ai.fix` → 退出码 **0**；② `pytest tests/fix_test.py -q` → **28 passed**；③ **零写盘**：跑完目标 YAML 的 `st_mtime_ns` 与内容都不变、`.ai/work` 无残留；④ **业务码 100% 拒绝**：6 个样本（`status_code`/`body.code`/`body.data.code`/`body.errno`/`data.result.ret`/`body.error_code`）**一条都不给建议、一个 diff 都没有**；⑤ **字段改名 8/10**（10 个注入样本：加/去前缀、`_`↔驼峰、加后缀、加/去层级）；⑥ **降级补丁必被拦**（`not_equal: [x, ""]` → 前置②拒）；⑦ **`--apply` 不存在**（argparse 直接报错）；⑧ **退出码恒 0**（业务码拒绝也算成功——把"正常拒绝"报成非零会让人当故障绕过）；⑨ `bench/write_boundary_scanner.py` → 包 **0 违规**。**★仍未做（不虚标）**：P3b 的确认台（把"人工确认接受建议"接进 `.ai/reviews/` 留痕）属 **P3b**） |
| **P2b debugtalk 草稿** | `haify debugtalk` | 每个函数强制带已知答案测试，无向量则拒产；`${fn(...)}` 100% 可被 `parser` 解析为函数调用 | 3~4（**★P2b 落地注记（2026-09-25）**：`interfacetester_ai/debugtalk_draft.py` + CLI `haify debugtalk`。**★它要防的是"静默"**：`parser.py` 的函数调用正则参数字符集 `[\$\w\.\-\/\s=,]` **不含引号**，于是 `${f('abc')}` 不被当函数调用、退化成"变量替换 + 原样字符串"，而框架**只告警不报错**（`_function_with_quote_regex_compile` 就是为此加的告警）——后果不是"报错让你改"，而是**静默产出垃圾值，一路流到断言里去比**。**★四条纪律**：① **往返解析的判据是内核的正则本身**（不是"我们照规则再写一遍"——那等于把"能不能被解析"从**内核说了算**变成**我说了算**）；② **没有已知答案测试就拒绝交付**（§5.9 原文；立场同 P2a 的"业务码不给改"：一个"看起来能用"的签名函数比没有更危险——用例会过、而报文签名是错的）；③ **已知答案测试真的执行**（干净命名空间里 `exec` **要交付的那份文本**；不比"有没有表"，只比"对不对"；用 `exec` 而非 `import debugtalk` 是复用 `emit_yaml._defined_function_names` 的一条教训：`importlib` + `sys.path` 会拿到**进程里已加载过的别的项目**的函数表）；④ **追加块 + diff，绝不整文件覆盖**（既有源码**逐字保留**、diff **只增不删**；重名**只提示不改写**）。**★两处实测驱动的修正**：**(a) 往返判据必须跟着内核走**——元护栏用"把内核正则**临时改成含引号**"的手法验：改后 `${f('abc')}` 必须被判**合格**；若仍不合格，说明判的是**我们自己那份规则**，于是"内核改了、我们没跟上"这类漂移永远发现不了（而 §5.9 那行地雷正是这样传播的）。**(b) 查重不能只认路径**（**自检抓到的真 bug**）——`existing_names` 原先只在给了 `existing_path` 时才算，于是**传 `source_text` 的场景**（测试 / 不落盘调用）查重**静默失效**：草稿照常被采纳，而人以为查过了；修法是"**两条路径都要有**"（文件走 `emit_yaml` 的 AST、源码走本地 AST）——因为"重名只提示不改写"这条判据**跟文件无关、跟源码有关**。**验收（实测）**：① `python -m interfacetester_ai.debugtalk_draft` → 退出码 **0**；② `pytest tests/debugtalk_draft_test.py -q` → **30 passed**；③ **往返 100%**：裸参形态（无参 / 字面量 / `$var` / `k=v` / `$a.b-c/d`）全过，`${f('abc')}` 被拒**且理由点明"引号"与"不报错"**；④ **无向量拒产**（`no_vector` + 理由引 §5.9）；⑤ **已知答案测试真跑**：`md5("abc")` 通过，**期望值改一个字符 → 立即拒产**（元护栏），源码跑不起来 / 函数不存在也拒；⑥ **追加不覆盖**（原文逐字保留、diff 无删除行、重名只提示）；⑦ `--existing` 指向的文件 **mtime 与内容都不变**（"只呈现"的形态）；⑧ `bench/write_boundary_scanner.py` → 包 **0 违规**（本模块**不进**注册表）。**★仍未做（不虚标）**：~~L3 沙盒~~ **★第十五批（2026-09-25）已落地**——口径由一次明确决策钉住（**允许执行 · 只跑已知答案测试 · 允许真实加密运算**）：`--sandbox` 时 KAT 在**子进程**里跑（`l3.require_sandbox` 闸门在前 → 沙盒没开就**一个子进程都不起**；能超时杀；子进程 env 白名单里**没有** `INTERFACETESTER_AI_*`；`open`/`eval`/`exec`/出网/动态加载全部拦、`hashlib`/`hmac`/`base64` 等纯计算照常可用），四元组与口径见 §12.8-⑨；"从文档自动识别'需要签名函数'并生成 spec"（本命令的输入是**人工给的 spec**）属 P0a 的文档解析范畴） |
| **P3a Web 只读看板** | `haify serve` + 静态页 | 零写盘、零执行；目录穿越用例全被拒；只监听回环 | 1~2（**★P3a 落地注记（2026-09-25）**：`interfacetester_ai/web.py` + CLI `haify serve`。**四页**：运行列表 / 失败明细 / 产物下载 / 度量（首页**同时**显示度量摘要——行 348 要求降级率"必须显示在看板上"，首页才是看板的默认落点）。**★四条验收（实测）**：① **零写盘**——判据不是"我说不写"，而是**取全量快照 → 把页面与写方法全跑一遍 → 再对账**（快照含 `.ai/`、`reports/`、`logs/`）；② **零执行**——只实现 `do_GET`/`do_HEAD`，写方法**显式 405**（★不是让 `BaseHTTPRequestHandler` 回 501：「501 不认得」与「405 不允许」是两句不同的话），模块不 import `pytest`/`make`/`llm`（AST 自查 + 测试双钉）；③ **目录穿越全被拒**——7 类样本**经真实 HTTP** 全拒；④ **只监听回环**——默认 `127.0.0.1`，对外必须显式 `--host` 且打印"反代 + IP 白名单"理由，违规读根**启动即拒**。**★四处实测驱动的修正**：**(a) 不能复用 `workdir.snapshot_tree`**——它为「别污染源码树」而**故意**忽略 `.ai/`、`logs/` 这些运行期产物目录，而它们恰恰是**看板唯一能触碰的目录**；用它的口径，「零写盘」会变成一条**永远为真**的空护栏（比没有更坏：它让人以为验过了）。改为自写 `snapshot_all`（只忽略 `__pycache__`），并用测试里的**元护栏**钉住"两者的口径确实不同"。**(b) T18 扫描器抓到自检写盘违规 5 处**——自检为了造夹具在临时目录里写文件，而**产线模块里出现写调用即违规**（扫描器无法静态区分"写临时目录"与"写工作区"，所以从严）。处置：自检**不再造夹具**（改用**内存数据** + 已存在的产物，缺了就**如实说跳过**），**夹具写入归 `tests/`**——顺带让"零写盘"判据跑在**真实工作区**上（比"我们自己造的那几个文件"更硬）。**(c) 双重前缀 bug**（自检第一轮抓到 10 项）——`rel`（`logs/x.summary.json`）与读根（`logs`）**都是**相对工作区根的，逐个 `join(root, rel)` 拼成 `logs/logs/x.summary.json`，于是"所有文件都不存在"；**这个错误的表现极其危险：页面显示"还没有运行记录"，与真的没跑过用例一模一样**。改为 `resolve_read_path()`：先做路径安全，再用 `commonpath` 判"是否落在某读根内"。**(d) R8 的判据不能写成"必须有红色"**——首页反映真实工作区现状，那里可能一份失败都没有，此时不出现红色是**正确**的（"必须有红色"会逼实现在没失败时也画红，那才是真把失败信息弄脏）；改为：**所有页**禁词零出现 + **必然含失败态**的页（明细页/错误页）必须有 `state-error` + **用内存数据构造"确有失败"的渲染**证明"有失败时首页必红"。**验收（实测）**：① `python -m interfacetester_ai.web` → 退出码 **0**（并**如实打印**"未降级占比：未读到 → 已跳过该判据"）；② `pytest tests/web_readonly_test.py -q` → **56 passed**；③ 写盘扫描器 **0 违规**（本模块**不进**注册表——它没有写盘点）；④ 全量 `--collect-only` = **2183**。**★仍未做（不虚标）**：**P3b**（确认门：pending 回填 + fix 逐条 accept/reject + 触发运行串行队列）、**P3c**（反代鉴权 + 审计 + 离线 wheel）、**P3d**（前端增强）。**另**：缓存命中率与花费估算**当时**写的是"没有落盘来源"——★**第十三批（2026-09-25）更正**：来源**本来就有**（`.ai/manifest/*.json` 这个既有调用账本带 `cached`/`usage`），只需**读出来**，**不必**新开写入者；现已落地为 `read_cache_hit_rate()` / `read_token_usage()` / `read_cost_estimate()` 三行（逐个带来源；分母只算可读留痕、坏文件单独计数、缺 `usage` 不按 0 算、单价没配就点名缺哪个环境变量）——真实数据与四元组见 §12.8-⑦） |
| **P3b Web 确认门** | pending 回填 + fix 确认 + 触发运行 | 每次确认落 `.ai/reviews/*.json`（含 manifest hash）；触发运行必过 L3 闸门；作业串行且目录隔离 | 3~5（**★P3b-1 落地注记（2026-09-25）**：`interfacetester_ai/confirm.py` + `web.py` 的三条新路由（`GET /pending`、`GET /fix?path=…`、`POST /confirm`）。**★这一半刻意"零执行"**——不跑用例、不调模型、不起子进程；「pending 回填 → 自动重跑 L2 → 出 diff」与「触发运行」属 **P3b-2**（那才是真正新增攻击面的那块，要子进程 + L3 闸门 + 串行队列 + T18 登记）。**★干净切法（决定了 P3a 的四条验收一条都不破）**：确认门**自己不写盘**，落痕**委托** `reviews.write_review_record`（T18 在册写入者）——所以 `confirm` **不进**注册表、`web.py` 的源码里**仍然没有**任何写调用（"零写盘"的形态保住了），而 `WebHandler` 里也**仍然没有**执行调用（"零执行"也保住了）。**★三条硬纪律**：① **reject 必填理由**（§2.5 行 339 原文；空 / 纯空白 / 全角空格**一律拒收**，遇 `422`）——理由空着还能 reject 等于把「为什么不要它」**永久丢掉**；② **approver 必填**（复用 `reviews` 的立场："允许空值等于允许一张没签名的单子"）；③ **accept 也留痕**（只记 reject 会让"同意"变成**没有证据的默认值**）。**★另外两条**：**校验不过就一行都不写**（留痕是**证据**，半份证据比没有更坏）；**坏 body 不猜**（猜出来的请求会带着**人的签名**落进留痕，那比报错危险得多）。**★写边界**：`POST /confirm` 是**唯一**接受写的路径，其余路径的写方法仍必须 **405**（自检与测试各有一条"例外没有扩散"的判据）；校验不过回 **422** 而非 200（R8：失败不得被柔化成"成功的页面"）。**★留痕绑内容哈希**（复用 T24）：改一行 → 留痕失效、需重新确认；`accept` 的回填内容**只呈现、不落盘**（人工自己贴）。**★一处实测驱动的修正**：`web.fetch` 助手的 `method` 默认值原本写死 `"GET"`，于是 `fetch(..., body=…)` 发出一个「**带 body 的 GET**」——urllib 照发、服务端照收，但 POST 路由**根本没被碰到**，现象是 **404**（`/confirm` 明明实现了却一直报"期望 422，实得 404"，而单跑一次 POST 又是好的）；改为"有 body → POST，无 body → GET"。**验收（实测）**：① `python -m interfacetester_ai.confirm` → 退出码 **0**；② `pytest tests/confirm_test.py -q` → **31 passed**（含"真落痕 + 内容哈希 + 改一行即失效"与"被拒时不留痕"）；③ 写盘扫描器 **0 违规**；④ 全量 `--collect-only` = **2214**。**★P3b-2 落地注记（2026-09-25）**：`interfacetester_ai/jobs.py` + CLI `haify run` + 看板的 `GET /jobs` · `POST /jobs`。**它是本包唯一会执行代码的部分**。**★与同批 `web`/`confirm` 的**有意对比**：那两个刻意**不进** T18 注册表（源码里没有写调用——看板零写盘、确认门把落盘**委托**给 `reviews`），而 `jobs` **真的执行 + 真的写盘** → **必须登记**（`("roots", (".ai/",))`）。同款纪律**第四次咬合**：未登记的 `jobs.py` 被 T18 判 **4 处违规**（`makedirs`×2 + `open` + `os.replace`），显式登记后 **0 违规**。**★三道闸门（顺序不能反）**：① **运行域**——只跑 `cases/` 下的 `*.yml`/`*.yaml`（`realpath` + `commonpath`，`cases_evil/` 拦得住），且 **`-` 开头一律拒**——那是 **pytest 选项**：`--pyargs` 能跑到任意包、`-p` 能加载任意插件，**放过去就是任意代码执行**（这条比 L3 更基础：L3 管"打哪个**地址**"，它管"跑哪个**文件**"）；② **L3 运行期冒烟**——与 CLI **同一个** `check_l3_allowed`（"是网页点的"不构成放宽理由，R7；**元护栏**：patch 它、本模块结论必须跟着变）；③ **超时有界**。**★闸门在子进程之前**：被拒时**零副作用**——不建目录、不起进程、**连作业号都不分配**（判据查的是**文件系统事实**：被拒前后 `.ai/jobs/` 的目录集合必须不变，而不是"我声明没写"）。**★子进程而非进程内 pytest**（§2.5 表格第 3 条），命令用 `sys.executable -m interfacetester.cli run`——**不用 `hrun`**（安装出来的入口脚本在客户环境里不保证在 PATH）。**★串行**：模块级锁，同一时刻一个作业；每作业一个 `.ai/jobs/<job_id>/`。**★证据**：`result.json` 落**退出码 + L3 结论 + 触发者 + 耗时**（R8：退出码必须**显式**；stdout/stderr 截尾并**显式标注**）。**★刻意**不复用 `workdir.run_isolated`：它的契约是"作业只许写作业目录、工作树前后逐字节一致"——那是 **L2 生成期**的契约；触发运行的产物（`logs/`）**本来就该落盘**，套用会让每次**正常运行**都被判成"污染"。**★一处实测驱动的修正**：真起子进程的测试一开始"通过"了，但**语义是假的**——子进程用**真实 `os.environ`**（`env` 参数只影响闸门判断），而临时工作区的 `sys.path` 找不到 `interfacetester`，于是测出来的是"模块找不到"而不是"pytest 的行为"；修法是测试里显式设 `PYTHONPATH`。**★状态码口径**：闸门拒绝 → **422**；**执行了（无论退出码是否 0）→ 200**——「用例失败」不是「服务失败」，退出码在页面上显式展示。**验收（实测）**：① `python -m interfacetester_ai.jobs` → 退出码 **0**；② `pytest tests/jobs_test.py -q` → **27 passed**（含 **4 条真起子进程**：退出码落盘 / 作业目录隔离 / 闸门拒时零进程）；③ `haify run cases/x.yml` → **exit 2** + 拒因"沙盒未开启"；`--dry-run` + 回环 → **exit 0** + "未执行"；④ 写盘扫描器 **0 违规**；⑤ 全量 `--collect-only` = **2241**。**★仍未做（不虚标）**：P3c、P3d） |
| **P3c 加固** | 反代鉴权 + 审计 + 离线 wheel | 离线环境一键起服务；前端不暴露任何密钥 | 2~4（**★P3c 落地注记（2026-09-25）**：`interfacetester_ai/audit.py` + `web.py` 的加固层 + `tests/packaging_test.py`。**★三件事的诚实范围**：**(1) 反代鉴权 ✅** —— 能做的是**两层**：① **IP 白名单**，判据用 **`client_address`（真实对端）**，★`X-Forwarded-For` **默认不信任**（那是一个**客户端能随便写**的头——`X-Forwarded-For: 127.0.0.1` 就是一句话的事，拿它当白名单依据等于**没有白名单**）；只有**显式** `--trust-proxy` 才读它，且只取**最后一跳（最右）**——反代通常往**末尾追加**，最右才是"反代看到的对端"，最左那格是客户端**自称**的。**元护栏**：显式信任后同一个头**必须**生效（否则"不信任"的判据可能只是因为实现压根没读那个头——那就成了一个永远为真的判据）。② **Bearer token**，从**环境变量**读（传的是**变量名**，不是值——token 走命令行会进 `ps` / shell 历史 / CI 日志），比对用 `hmac.compare_digest`（防时序攻击）。失败区分 **401（缺凭据，带 `WWW-Authenticate: Bearer`）/ 403（有凭据但不够）**——两种情况的下一步完全不同。★**前端不暴露密钥**：有判据断言**页面里永不出现 token**。★**默认全放行**（回环 + 未配鉴权 = 本地开发形态），但**对外监听却没配任何鉴权时会显式警告**——一个**静默**生效的白名单与一个**不存在**的白名单，出问题时是分不清的。**(2) 审计留痕 ✅** —— `audit.py` 把三个**既有证据源**（`.ai/reviews/` · `.ai/jobs/` · `.ai/pending/`）**投影**成一条可检索的时间线。★**刻意不是第二份日志**：双源迟早不一致，而**不一致的审计等于没有审计**；每条事件都带 `source`（可一路指回文件）。★**时间只来自证据**：证据里没有时间就写 `(无时间戳)`，**不拿"现在"填空**（那是**伪造时间**）——而"这份清单躺了三周"恰恰是最该被看见的事。★**分段**：时间未知的**不排进时间轴**（`(无时间戳)` 的字典序比任何数字都小，混排会让未知**冒充"最早发生的事"**）。**(3) 离线 wheel ✅ 已实测（第十二批，2026-09-25；四元组见 §12.8-⑥）** —— 本机 `.venv` **没有** `build` / `wheel` / `setuptools`（`pip wheel .` 需要前两者），但**基础解释器有** setuptools，故**离线打轮已实测**（第十二批）；同时把"离线一键"的**根据**写成机器判据：① **零三方依赖**（`pyproject.dependencies = []` **且** 顶层 import 扫描干净）；② **复用内核依赖的两条件**（包名**已在内核 `requirements.txt` 里** + 该 import **只在函数体内**，即"没装它"时本包仍可导入）；③ **离线部署的说明书就在 `requirements.txt`**（`pip download -r … -d ./wheels` → `pip install --no-index --find-links=./wheels -r …`），不另写一份。另有一条测试**把限制写进代码**：`dist/*.whl` 一旦出现就红，提示改文档并补"产物可安装"的判据——**改测试这个动作本身就是签字**。**★一处实测驱动的判据升级**：初版判据"扫描所有 import 只许 stdlib"**抓到了两个真问题**（`yaml`@`fix.py`、`requests`@`llm.py`），查清后发现它们**都合规**（前者注释写着"内核的**硬依赖** PyYAML，本包**不新增依赖**"，后者是**延迟 import**"包在没装 requests 时仍可被导入"）——原判据**抓得对但定位不准**。升级为**分层判据**：顶层只许 stdlib + 本仓（那才是"缺了就 import 不了"的那种依赖），函数体内允许复用**但必须同时满足两条**（内核已声明 + 延迟），否则"允许清单"会变成一张**谁都能加名字的白纸**。**验收（实测）**：① `python -m interfacetester_ai.audit` → 退出码 **0**；`--render` 在空 `.ai/` 下如实说明"没有读到任何证据"；② `pytest tests/audit_test.py tests/packaging_test.py -q` → **29 passed**；`pytest tests/web_readonly_test.py -q` → **63 passed**（含 7 条加固判据）；③ 写盘扫描器 **0 违规**（`audit` 只读 → **不进** T18）；④ 全量 `--collect-only` = **2277**。**★第十二批（2026-09-25）已把「打 wheel + 离线装」实测掉**（命令/环境/输出/退出码见 §12.8-⑥），并把它变成机器判据：`tests/packaging_test.py::TestOfflineWheelBuildsAndInstalls`（离线打轮 → `--target` 装 → 入口在，**自己找带 setuptools 的解释器**）+ `TestBuiltWheelMatchesSourceTree`（**产物 ↔ 源码树**：模块集合 / 零依赖 / 入口 / RECORD 完整性 / 无包外文件，由 `INTERFACETESTER_AI_WHEEL_PATH` 驱动）；CI 的 `package` job 现在**同时**打两个 wheel 并校验（GitHub + GitLab）。**★第十四批（2026-09-25）已把「真机 `file://` 直开」实测掉**（真浏览器四元组见 §12.8-⑧）：单页能读、但**导航是绝对路径**（点「度量」→ `file:///D:/metrics` → `ERR_FILE_NOT_FOUND`）——所以补了**离线静态导出** `interfacetester_ai/export.py`（`haify export`，落 `.ai/export/`，站内链接改写成相对文件名，页面顶部挂"这是快照"横幅）。**仍未做（不虚标）**：真模型端到端基线数值(★2026-09-25 已做：见 §12.8-⑩)） |
| **P3d 前端** | 静态 HTML + 原生 JS（不引框架） | 无前端构建链；离线可直接打开 | 1~2（**★P3d 落地注记（2026-09-25）**：**★先看再改**——这一批的第一步不是写代码，而是**真的起服务、用浏览器打开**（`http://127.0.0.1:8766/`），把 DOM 与渲染结构抓出来，而不是猜"用户想要什么"。**实测发现两个"到不了"**：① **`/audit` 根本没页面**——`audit.py`（P3c）做完了，但**没接进看板**，只能靠 CLI `--render` 看；② **`/fix` 有页面却没有入口**——它能按 `summary.json` 出建议，但**没有任何页面能点进它**。两处都补上：新增 **`GET /audit` 页**（投影 + 读取时刻 + "时间未知"单列 + 读不到单列），并在**运行明细页**给出 `/fix` 的入口（那里正好有 summary 路径——**最自然的位置**）。**导航从 4 项补到 5 项，且加了一条判据"导航里列出的每一项都必须真能到"**（加导航项却忘了实现路由，表现是"点进去 404"）。**★刻意不做自动刷新**：`/pending` 与 `/fix` 的表单是**长 JSON**，一次自动刷新就**全清空**了——而"我以为填好了、其实被刷没了"是最坏的一种丢失。改为**在表单页明确提示**"本页不会自动刷新：刷新或切页会**清空**你正在改的内容"，并有一条判据断言**页面里不出现定时器**（`setInterval`/`setTimeout`）。**★判据定位的第二次修正**（与 P3c 那次同款）：初版判据"`/pending` 与 `/jobs` 都必须显示那句提示"**打偏了**——`/pending` 在**清单为空时根本不渲染表单**（那就没有东西会被刷掉），此时要求它显示提示，会把**"如实显示空状态"判成违规**。改为**按"有没有 `<button>`"挂判据**，并补一条**元护栏**：`/jobs` **总是**渲染触发表单，所以它必须有提示——它是那条判据的**非空样本**（否则判据可能永远在 `continue`）。**行 1004 的两条验收（机器判据）**：① **无构建链**——仓库里没有 `package.json`/`package-lock.json`/`yarn.lock`/`pnpm-lock.yaml`/`node_modules`/`webpack|vite|rollup|postcss|tailwind` 配置/`tsconfig.json`，且页面里没有 `import` / `<script type="module">`；② **离线可直接打开**——**每一页**的外部引用数 **0**（`http(s)://`、`//` 开头的一律红）+ `<meta charset>` 与 `<html lang>` 齐全。**另加三条可用性最低线**（没有 CSS 框架兜着时尤其明显）：每页**恰好一个 `<h1>`**、表格**都有 `<thead>`**、按钮**都有文字**（离线环境不该依赖图标字体）。**验收（实测）**：① **浏览器实看**——`/` 页导航 5 项（`运行列表 → /`、`度量 → /metrics`、`审计 → /audit`、`待确认 → /pending`、`作业 → /jobs`）、`externalRefs = 0`、`lang=zh-CN`、`charset=UTF-8`、`tables=2/theads=2`；`/audit` 页 `h1=审计时间线`、`投影`/`本页读取时刻`/`时间未知` 三段文案都在、`externalRefs=0`；五个页面 HTTP **全部 200**；② `pytest tests/frontend_test.py -q` → **13 passed**；③ `python -m interfacetester_ai.web` → 退出码 **0**（含"导航可达"判据）；④ 写盘扫描器 **0 违规**；⑤ 全量 `--collect-only` = **2290**。**★第十四批（2026-09-25）已把「真机 `file://` 直开」实测并收口**（四元组见 §12.8-⑧）：**单页自包含 ✅ 但导航 ❌** —— `file://` 下点「度量」跳到 `file:///D:/metrics`（`ERR_FILE_NOT_FOUND`）；因此新增 `interfacetester_ai/export.py`（`haify export` → `.ai/export/`，站内链接**改写成相对文件名**，每页顶部挂"这是快照"横幅，`offline.html` 点名哪些能力需要服务端），导出物**在真浏览器里逐页点得通**。**仍未做（不虚标）**：真模型端到端） |
| **可选：内核注册** | `hconvert --from doc` | 表驱动重构后既有测试全绿 + 过 `docs_consistency_test` | 0.5~1 |

> **★阶段行状态一览（2026-09-25 刷新，逐行可核）**——本表的 ✅ 标记有两种写法：S 系列写在**阶段名后**、
> P 系列写在**"人日"列的落地注记**里，于是"扫表"时容易把**已落地的行看成未做**。此处显式汇总一次，
> 口径仍以本表各行的"落地注记 + 验收四元组"为准：
>
> 共 **24 行** = **23 行已落地** + **1 行可选**。
>
> - **已落地（S 系列）**：S-0~S-3、S-2b、**S-4**、**S-5**（后两条**于本日补标 ✅**）、S-6、S-7、S-8、S-9、S-10；
> - **已落地（P 系列，注记在人日列）**：P0-0、P0a、P0b、P1a、P1b、P2a、P2b、P3a、P3b、P3c、P3d；
> - **可选（不是缺陷）**：`可选：内核注册`——§5.10 明写"**不阻塞任何其他工作，可以永远不做**"。
> - **★一处同类滞后的更正**：P0-0 行里那句"**本行完整版（golden 15 份、L2 隔离等 P0a 范围）仍待做**"是
>   **第九/十批之前的旧注记**——它点名的两件事已交付（golden **3 → 15** 份 = T9；L2 作业隔离 = T8），
>   本行的验收行（包内自检全绿 / 内核零改动 / 每个复用点两条用例 / 最长切片 ≤ `INPUT_BUDGET_BYTES` /
>   4 组编码对抗样本可读）逐条都有对应用例 → **按完整口径视为已落地**。
> - **本日补标的两条取证（一度"实现已在、状态没写上表"）**：
>   `S-4` = `assembler.assemble()` 在写 `cases/` **之前**调 `gates.check_testcase`，不过则
>   `write_needs_human` 落 `.ai/failed/<case>/NEEDS_HUMAN.md`（真模型跑动中 `user_info.md` 实测被拦、`exit=1`）；
>   `S-5` = `fix.py::validate_patch()` 在**输出 diff 之前**过双前置（临时文件走真 `load_testcase_file` + S 系列）。
> - **阶段行层面已无"未落地"**；**未闭合的是决策与人工项**——D4（验收①口径 +
>   "gen + 断言合并"是否立项）/ R11 阈值，见 §十一 与 §12.6。
>   （**D2 架构落点已于 2026-09-26 决策闭合**——确认旁路包为唯一主线、0923 三份标废弃；见 §十一 D2 与 `docs/ARCHIVED.md`。）
>   （**D3 风险姿态已于 2026-09-26 决策闭合**——**不支持自动 apply（最终形态）**，成对判据 `test_cli_has_no_apply_flag` +
>   目标文件 mtime 不变；见 §十一 D3。）

> **本表求和自检（★v7 重算，逐步列出便于复核）**：
> CLI 项 = S-0~S-3（**2.8**，已完成）＋ **S-2b 0.55** ＋ S-4~S-8（**1.4**）＋ **S-9 0.3** ＋ P0-0（5.5~7）＋ P0a（5~7）＋ P0b（2~3）＋ P1a（4~6）＋ P1b（1~2）＋ P2a（3~4）＋ P2b（3~4）
> = **28.55~38.05** 人日（低端逐步：2.8+0.55+1.4+0.3+5.5+5+2+4+1+3+3 = 28.55）；
> Web 项（P3a + P3b + P3c + P3d）= **7~13**。两者与 §8.2 的 A、B 两表**逐项对得上**（A + 已完成 2.8 = 25.75+2.8 → 35.25+2.8）。
> **v6 → v7 的差值 = +0.85 人日**（S-2b 0.55 + S-9 0.3），来源就是 §0.4 的 ④⑤⑦ 三处：改 S3 三态、扩展 S4 判据、闸门保真对账**此前没有独立行**。
> "可选：内核注册"**不计入**上述任何合计。

### 8.2 工作量（**v6 首次做到口径单一、算术自洽；v7 补两行后重算**）

**A. CLI 侧（不含可选的内核注册）**

| 项 | 人日 |
| --- | --- |
| A1 接入层骨架（`llm/schema/prompts/cache/manifest/evidence`）+ 搬运 `bench/` 三处护栏 | 3~4 |
| A2 `haify gen`（切片器 + CaseDraft 契约 + 修正环） | 5~7 |
| A3 `haify gen --assertions-only` + pending 落盘 | 2~3 |
| A4 `haify analyze`（证据装配 + 归一化管道 + 引用校验） | 4~6 |
| A5 `haify summary`（模板渲染 + 数字白名单） | 1~2 |
| A6 `haify fix`（只读 diff + 双前置） | 3~4 |
| A7 `haify debugtalk`（已知答案测试） | 3~4 |
| A8 S 系列闸门接入 gen/fix + CI 门禁（= §8.1 的 S-4~S-8：0.3+0.2+0.5+0.2+0.2） | 1.4 |
| **A8b 修 S3 三态 + 扩展 S4 判据 + 2 条 MUST_ALLOW + 探针留档（= §8.1 的 S-2b；★v7 补行）** | **0.55** |
| A9 golden 集 3 → 15 份（含编码/XML/业务码对抗样本） | 1.5~2 |
| A10 归一化管道独立实现 + 对抗样本用例 | 1 |
| **A11 闸门保真对账 + `_classify` 桶名断言与形态自检（= §8.1 的 S-9；★v7 补行）** | **0.3** |
| **A 合计（低 → 高）** | **25.75 → 35.25；保守取整为 26~35**（低端向上、高端向下） |

> **两表对账（★v7 重算：口径与 v6 同构，只补两行，逐步列出便于复核）**：
> A1 + A9 + A10 = 3~4 + 1.5~2 + 1 = **5.5~7**，对应 §8.1 的 **P0-0** 行；A2~A7 与 §8.1 的 P0a/P0b/P1a/P1b/P2a/P2b 一一对应；
> A8 = §8.1 的 S-4~S-8 = **1.4**；**A8b = §8.1 的 S-2b = 0.55**；**A11 = §8.1 的 S-9 = 0.3**。
> 因此：**§8.1 的 CLI 项求和 = 28.55~38.05 人日（含已完成的 2.8）**，与 **A + 已完成 2.8 = 25.75+2.8 → 35.25+2.8** 完全相等。
> §8.1 的 Web 项求和 = 6~11（P3a+P3b+P3c）+ 前端 1~2 = **7~13**，与 **B 完全相等**。

**B. Web 侧**

| 项 | 人日 |
| --- | --- |
| W1 P3a 只读看板 | 1~2 |
| W2 P3b 确认门（pending 回填 + fix 逐条确认 + 触发运行 + 串行队列） | 3~5 |
| W3 P3c 加固（反代鉴权 + 审计留痕 + 离线 wheel + 文档） | 2~4 |
| W4 前端（静态 HTML + 原生 JS，不引框架） | 1~2 |
| **B 合计（低 → 高）** | **7 → 13，即 7~13**；只读版（W1 + W3）= **3~6** |

**C. 总计与裁剪**

| 口径 | 人日 |
| --- | --- |
| CLI 侧（= A 合计，**保守取整**） | 26~35 |
| Web 侧（= B 合计） | 7~13 |
| **总计** | **33~48**（＝26+7 → 35+13，**不含**下一行） |
| 可选：内核注册 `hconvert --from doc`（可永远不做） | +0.5~1 |
| **裁剪后（纯 CLI + 留痕）** | **22.5~29；保守取整为 23~29**（逐步：低 33−3−6−1−0.5=22.5；高 48−4−12−2−1=29） |

**裁剪顺序（按性价比，逐条给出可省量）**

1. 砍 **P2b**（debugtalk，风险最高："接口 200 但签名错"）：**−3~4**
2. **砍掉整个 Web（P3a + P3b + P3c + P3d），只保留 P3c 里的"审计留痕"1 人日**：**−6~12**（= `7−1 → 13−1`）
   > **★v7 更正**：v6 这一条的标签写的是"砍 P3a + P3b"，但它给的 `−6~12` 其实是按"砍掉**整个** Web 只留 1"算出来的
   > （若真只砍 P3a+P3b，应是 `−（4−1）~−（7−1）` = `−3~6`）。**标签与数字必须对齐**，否则又是一次"口径不唯一"（§0.4-⑪）。
3. **P0b 并入 P0a**（断言生成本就是 `gen` 的装配部分）：**−1~2**
4. golden 样本 **15 → 8**（类别覆盖够即可定基线）：**−0.5~1**
5. 内核注册**永久不做**：**不计入，0 影响**（它本就不在 A、B 合计内；v6 写成"−0~1"却又没把它减进合计，v7 改为显式声明）

> **裁剪后逐步算术（★v7，可逐项复核）**：总计 **33~48** → 低 `33−3−6−1−0.5 = 22.5`、高 `48−4−12−2−1 = 29`
> → **22.5~29**，保守取整（低端向上、高端向下）→ **23~29**。

**口径声明（务必读）**

- **A、B 表内每一行都是"估算"，不是实测**；只有 S-0~S-3 的 **2.8 人日**是**已完成并实测**的（见 §十二）。
- 前作 v4 承接的 **26~34 人日**来自《方案2-工作量重估》，而**该文档不在本工作区**（实测 `docs/` 下不存在，全仓按文件名检索无命中），**因此无法复核**。v6 起不再引用那个数字，改为给出上表的可复核口径。
- 前作 v4 §7.3 声称"裁剪可落到 **18~24**"，与本表算出的 **23~29** 不一致。差异原因：v4 的 Web 增量本身有三处互斥（8~14 / 8~14 / 6~11），**裁剪基数不唯一**，无法作为减法的被减数。本文以 §8.2 的单一口径为准。
- **不含**：模型采购/部署/联调、客户现场样本的脱敏审查、真实模型基线测量。
- 上表所有数字在 **P0a 结束时必须按实测重设一次**（先测后承诺）。

### 8.3 灰度与回滚

- 能力只随 `interfacetester_ai` 安装存在，**删包即回滚**，内核无感；
- `INTERFACETESTER_AI=off` 全局开关（CLI 层短路，优先级最高）；
- 先把生成的用例只写进 `.ai/draft/`（不进 `cases/`），**影子运行两周**统计通过率与降级率，再决定是否放开合并；
- Web 单独开关：`haify serve` 不启动即不存在该形态。

### 8.4 落地顺序（合并方案 3 §7 的四阶段与方案 5 §7.1 的优先序）

| 批次 | 内容 | 理由 |
| --- | --- | --- |
| **第 0 批（最高优先）** | S-4~S-8（约 1.4 人日） | 不依赖模型、不依赖样本、不联网，**却直接决定"LLM 产物是不是又一批假通过"**。必须在接模型**之前**做完——否则模型的第一批产物就是第一批假通过 |
| **第 1 批** | P0a + P0b（生成闭环） | 方案 3 §7「第一阶段：最稳妥、最有价值」 |
| **第 2 批** | P1a + P1b（诊断与摘要） | 方案 3「失败分析：非常适合 AI 参与，前提是证据驱动」 |
| **第 3 批** | P2a（建议，不落地） | 方案 3「第二阶段：半自动修复」中**只做"产出 diff + 人工确认"这一半** |
| **第 4 批（可选）** | P3a/P3b/P3c + 方案 3 §6 的延伸场景（测试数据、mock、回归优先级、用例失效检测、测试计划、规则检查） | 体验层与长期演进 |
| **永久不做** | 方案 3 §5.13「方式 C：AI 自动修正」；内核注册 | 见 §十一 D2 / §5.10 |

---

## 九、风险登记册（合并方案 3 §9、方案 4 §八、方案 5 §八）

### 9.1 通用风险

| # | 风险 | 影响 | 应对 | 归属 |
| --- | --- | --- | --- | --- |
| R1 | 幻觉（编参数/断言/原因） | 用例错、结论错 | L1+S+L2 三重闸 + `source_quote` 机器校验 + 引用降级可见 | P0/P1 |
| R2 | 数据外泄（文档/日志被送出去） | 合规事故 | 键名级 + 内容级双闸 + 本地模型优先 + `--allow-raw` 显式确认 | P0 |
| R3 | **自愈改错掩盖真实缺陷** | 漏掉线上 bug | 只建议不落地 + 业务码类失败禁止给改 + 红线⑦ + `.ai/reviews/` 留痕 | P2a |
| R4 | 证据匹配器不行导致"看起来模型很差" | 误判、返工 | 归一化管道 + 对抗样本用例 | P1a |
| R5 | 生成物污染仓库 / 互相覆盖 | 脏工作树、偶发错乱 | 作业临时目录 + 串行 + 零污染断言 | P0a/P3b |
| R6 | 预算失控（长文档 × 多轮重试） | 成本不可控 | 字节预算 + 确定性停机 + 每次调用 manifest 可复盘 | P0/P1 |
| R7 | **Web 越权执行**（拿 Web 打生产） | 不可逆 | 复用 L3 闸门，Web 不放松 | P3b |
| R8 | **Web 把失败柔化成提示** | 稀释"防假通过"可信度 | 页面显式展示退出码与未降级率；失败不得用"警告"色渲染 | P3a |
| R9 | 密钥集中在服务端 | 单点泄露 | 反代 + IP 白名单 + 只读密钥文件 + 不给前端透传 | P3c |
| R10 | 内核升级（如 5.1）后护栏漂移 | 结论过期 | 每次升级重跑对账流程 + `docs_consistency_test` + golden 基线护栏 | 全阶段 |

### 9.2 ★ v6 新增三条 + v7 新增一条 + v8 新增两条风险（前作未登记）

| # | 风险 | 影响 | 应对 |
| --- | --- | --- | --- |
| **R11** | **"可判定但语义错"**：断言形态完全合法（能过 S 系列与 L2），但语义是错的——例如给每个步骤都补 `eq: [status_code, 200]`、把文档里的**示例值**当期望值硬编码、断言了一个恒真的业务码 | **闸门 100% 放行**，落到人工确认；若人工只看"闸门是否绿"就会整批放过 | ① 验收里加"人工抽检 x 条断言的**语义正确率** ≥ 阈值"；② `source_quote` 校验只证明"文档里真有这句话"，**不证明"这句话被正确理解"**——必须显式告诉使用者这个边界；③ 报告里把"断言数量"与"断言覆盖的字段数"一起展示，便于发现"一屏 status_code"的批量退化 |
| **R12** | **闸门被绕过**（LLM 产物不进闸门直接写盘） | 防线形同虚设 | 闸门必须是**写盘前置断言**（§3.1 顺序）；S-4/S-5 完成后加"绕过检测"用例 |
| **R13** | **R 类残留假通过**（6 条刻意不拦的运行期语义陷阱） | 残留假通过 | **刻意接受**：拦它们就是假报，而"假报会让真报被无视"。这是内核已验证的取舍，**不是缺陷**——但必须在文档里写明，避免使用者误以为"过闸门 = 无假通过" |
| **R14** ★v7 | **"已钉住 / 已实现"的保真度幻觉**：文档声称某护栏被钉住、某判据已实现，实测**既无断言也无分支**（两处实例：`_classify` 的桶名、S1 的 `contained_by`）——**而且没有任何机制会红** | 让"全面受控"变成一句无据的自我评价；比 P-9 的"护栏打偏"更隐蔽：打偏还能靠一次对抗样本发现，**声称已被钉住会把"去验证的动机"一起消掉** | ① §3.3 三方对账用例（判据 ↔ 分支 ↔ 样本）；② §3.5 第三道元护栏 `test_every_gate_branch_has_a_must_reject_sample`；③ 每个复用点补"违规必红 + 合规必绿"两条；④ 文档里凡出现"已钉住 / 已实现 / 已完整"必须写明**钉住点**（§一 纪律二）；⑤ **★v8 自查补强**：v7 立了 R14，自己仍然中招两处（"4 道元护栏已完成"、"S3(部分)→PENDING 当现状"，§0.5-②④）——说明②③④防得住"钉住点漂移"，**防不住"把待办写成已完成"**；所以再补一条：**凡写"已完成"必须给出当时就能跑的验证命令（含环境与退出码）**（§12.8） |
| **R15** ★v8（**本轮新增，文档级**） | **"文档缺陷无归属"**：接口文档本身写错 / 写缺时，§5.3 的五分类**没有"文档错"这一类**——这条失败只能被塞进「用例错误」或「真实缺陷」 | 两层后果：① **文档永远不会被修**（没有缺陷清单，也交不到文档维护者手里）；② 同类错误**下次继续污染**（没有回归样本、没有"改回去会红"的护栏）。这是"**文档不准 → 用例不准**"这条链上**唯一没有机器/流程承接**的一环（R1 管"编"、R11 管"意会错"、**这条管"文档本身错"**） | ① **T29**（**✅ 2026-09-23 已落地**：归因六分类 + `doc_defects.md` 回流，见 §十 T29 ✅ 行与 §5.3 落地注记；"含错误文档" golden 类目随 T9/P0a）；② 回流清单必须含**文档原句引用**（判据同 §5.3 证据优先，无证据一律"未知待人工确认"）；③ 已确认的文档错进 golden，文档修好后重跑（防改回去）；**阶段**：P1a |
| **R16** ★v8（**本轮新增，文档级**） | **"分叉型静默"**：模型**替文档纠错**（按自己对接口的理解生成断言，而非按文档字面）→ 用例**跑绿**，但"**文档 - 用例 - 实现**"三方不一致且无人察觉 | 比 R1（编造）更隐蔽：R1 的产物通常**响亮失败**（字段不存在 / 401 / `EnvNotFound`，错得可见），而分叉的产物是**绿的**；且 `source_quote` 可能**仍然命中**（引用的是相邻那句子），所以 §5.1 的"句在"校验**拦不住它** | ① **T28**（引用-断言**确定性**对账：字段名 / expect 字面量 / 限定词↔算子 / 示例值禁用，四类规则，无规则满足即降级 `unknowns`）；② 报告展示**断言数 vs 覆盖字段数**（治"一屏 `status_code, 200`"的批量退化）；③ 与 **R11** 同族——必须同样**显式告诉使用者边界**："过闸门 ≠ 语义正确"；**阶段**：P0a |

### 9.3 闸门自身的风险（方案 5 §八）

| # | 风险 | 影响 | 应对 | 状态 |
| --- | --- | --- | --- | --- |
| G1 | 闸门**漏拦**新形态的静默陷阱 | 假通过回归 | 陷阱清单用关键词穷举（233 处）建立候选集并逐条三分类；新增陷阱须同步补 MUST_REJECT 样本 | 已缓解（**不可标记"已消除"**，见 §4.4） |
| G2 | 闸门**误伤**合法写法 → 被整体关掉 | 等于没有闸门 | MUST_ALLOW 集 + 4 道元护栏（**★v8 现状：四道全部落地**——v7 增补的 2 道已于 2026-09-23 补齐 = T19/T20，§0.5-② 修复注记）+ **真实样本回归（§3.5）** | 已缓解（**S3 曾实际误伤，见 §3.3**） |
| G3 | 复用内核护栏，内核升级后签名漂移 | 闸门静默失效 | `test_kernel_guardrails_are_still_callable` 钉住可调用性；红即需重新对账 | 已缓解 |
| G4 | 闸门被绕过 | 防线形同虚设 | 同 R12 | **待做** |
| G5 | PENDING 被忽略（没人看清单） | 提取的 None 继续传播 | `<case>.pending.json` + 红线⑥ 留痕 + Web 确认台（P3b） | 沿用 |
| G6 | 闸门只覆盖 11 条，运行期语义陷阱仍在 | 残留假通过 | 同 R13 | 已接受 |
| **G7** ★v7 | **闸门保真漂移**：文档判据写了 N 支、代码只实现 M 支（或反之），而自检样本只覆盖其中一支 | 文档与实现各说各话，"已实现并实测"变成一句空话；且**没有任何机制会红** | §3.3 三方对账 + §3.5 的 `test_every_gate_branch_has_a_must_reject_sample`；**实例就是 S1 的 `contained_by`**（§0.4-④⑤） | **已缓解（v7 新增）** |
| **G8** ★v8 | **取证脚本的编码健壮性**：`bench/probe_silent_gate.py` 输出含 `✓`/`✗`（U+2713/U+2717），在**非交互（重定向/CI）+ 默认 GBK locale** 下 `UnicodeEncodeError`、退出码 1；其余三个自检脚本当前输出恰好全为 GBK 可编码字符、暂不崩，但"输出未声明编码"是同一种口径 | S-7 CI 门禁第一天就红——**红的不是闸门，是取证脚本自己**；更隐蔽的代价是污染"实测通过"的可信度：同一句话在交互控制台为真、在 CI 为假，团队会误以为闸门回归了 | **T26**：探针 `main()` 开头 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`（或把 ✓/✗ 换成 ASCII），四个脚本同批统一；S-7 验收加"**重定向 + 无 `PYTHONIOENCODING` 下退出码全 0**"；本文立"**实测通过四元组：命令、环境、输出、退出码**"（§0.5-①、§12.8-①） | **已修（2026-09-23，T26 当日落地）**：四脚本重定向下退出码全 0，`cmd /c` 字节级重定向后输出为纯 UTF-8（取证见 §12.8-① 注记） |

---

## 十、待办清单（可直接派工）

| # | 事项 | 判据 | 依赖 | 量级 |
| --- | --- | --- | --- | --- |
| **T1** ★（**✅ 2026-09-23 已落地**） | **按 §3.3 重定义 S3 三态语义**（缺失 / 显式为空 / 有断言），给"有 `extract` 的请求步骤"豁免 REJECT——已实现：`gate_s3` 三态分支 + 整例级判据纳入"显式空声明" | `probe_s3_review/probe_s3_edge.py` 三例：① `findings: []` 通过（**不再 REJECT**）、② 仅 1 条 S3 PENDING 通过（**不再 REJECT**）、③ 通过——**实测通过**，输出留档 `out_probe_s3_edge.txt` | 无 | 0.2 ✅ |
| **T2** ★（**✅ 2026-09-23 已落地**） | 把 2 条新 MUST_ALLOW 补进自检集（仅提取的中间步骤 / `validate: []` 显式声明）——已补 `S3-ok3` / `S3-ok4` | 自检从"11 拦 / 15 放"变为"**11 拦 / 17 放**"且全绿——**实测通过**（退出码 0） | T1 | 0.1 ✅ |
| **T3**（**✅ 2026-09-23 已落地**） | 修 §3.3 的提示文案：~~删掉"写 `validate: []`"这个当前不可行的改法~~（T1 落地后该改法已**可行**——显式空得 PENDING 不再被硬拒；文案已改为"显式写 `validate: []` 声明意图（如实标注为 PENDING，不再硬拒）"） | 按提示改写后闸门结果确实变为通过——**实测通过**（探针②：`validate: []` → `findings: ['S3'] PENDING=1`，闸门结果通过） | T1 | 0.05 ✅ |
| **T4**（**✅ 2026-09-24 已落地，第十一批**） | 把"输入预算"统一为**字节口径**，写成**代码常量** + 单测——已实现 `interfacetester_ai/prompts.py`：`INPUT_BUDGET_BYTES = 12000`（+ `INPUT_BUDGET_HEADROOM = 0.15`）+ `measure_bytes()`（**UTF-8 字节**，不是字符数）+ `check_input_budget()` + `build_prompt_payload()`（校验点落在**拼完之后**的 payload 上——分段看都不超、合起来超了是这类预算最典型的漏检形态）+ `InputBudgetExceeded`（带 `overflow`，即 §9.x P-5 的"**记录截断量**"）。**模块里刻意没有截断入口**（"不允许静默截断"的机器形态） | 最长切片 ≤ `INPUT_BUDGET_BYTES`——**验收（实测）**：`pytest tests/ai_budget_cache_test.py -q` → **19 passed / 27 subtests**（含**文档↔代码对账**：从 §5.1/§9.x 抓 `INPUT_BUDGET_BYTES = <num>` 与代码常量比对；`measure_bytes("中文") == 6 != len("中文")`；超限抛错且 `overflow` 正确；等号边界放行；元护栏："模块里不许出现 truncat* 入口"）；`bench/write_boundary_scanner.py` → 包 **0 违规**（纯函数，无写盘点） | 无 | 0.5 ✅ |
| **T5**（**✅ 2026-09-24 已落地，第十一批**） | 缓存键纳入**全部请求字段**——已实现 `interfacetester_ai/cache.py`：`CACHE_KEY_FIELDS`（**9 个字段的显式清单**：base_url / model / prompt_version / system / user / temperature / seed / max_tokens / response_format）+ `build_cache_key()`（sha256 of 规范化 JSON：`sort_keys` + 紧凑分隔符 + `ensure_ascii=False`）+ `CacheKeyFieldsMismatch`。**★判据比原文更严**：原文只说"纳入全部字段"（防**漏**），实现把"**多字段**"也做成一类错误——有人加了采样参数却没同步清单时，那一项改了也**不会 miss**（同一个漏检的镜像方向，且它"不报错但也不生效"，更不容易被发现） | 改 `temperature` 必 miss——**验收（实测）**：`pytest tests/ai_budget_cache_test.py -q` → **19 passed / 27 subtests**，其中：**改任一字段键必变**（9 个 subTest 全覆盖）、`temperature` 单列一条、缺字段报 `missing`、**多字段报 `extra`**（`top_p` 实测）、键序无关且稳定、sha256 十六进制、中文值不因转义换键、**构造键不写盘**（纯函数）；另有**文档↔代码双向对账**（§5.6 写明的 9 个字段 ↔ 清单，且清单里不许有文档从未提及的字段） | 无 | 0.2 ✅ |
| **T6** ★（**✅ 2026-09-23 已落地，第四批**） | 建 `interfacetester_ai` 工程骨架，搬运 `bench/` 三处护栏 + **各配形态自检**（P-9）——已实现：`interfacetester_ai/` 平铺就地布局 + `pyproject.toml`；`gates.py` ← `bench/silent_traps.py`、`projection.py` ← `bench/normalize_projection.py` **整体迁移**（bench 两路径留**兼容壳**转发，`tests/interfacetester_ai_pkg_test.py::TestBenchShims` 以**同一性断言**（`assertIs`）钉住"壳是转发不是复制"）；`pending.py` 即 S-6 交付；`llm/cache/manifest/schema/prompts/evidence` 六模块占位（docstring 含写盘边界纪律，供 T18 扫描零违规） | 新包内自检全绿——**实测通过**（`python -m interfacetester_ai.gates` / `.projection` 与壳 CLI 退出码全 0）；内核零改动（`git diff` 空 → 非 git 仓等价形态：`interfacetester/` 目录未触碰 + 红线① 反向依赖断言 `test_kernel_sources_never_import_ai_package`）；每个复用点都有"违规必红/合规必绿"两条用例——**已具备**：闸门复用点 = `tests/silent_traps_test.py` 22 条（MUST_REJECT/MUST_ALLOW 成对，零改动通过）；写盘边界复用点 = `tests/write_boundary_test.py` 20 条；包形态自检 `tests/interfacetester_ai_pkg_test.py` 9 条 | 无 | 1~2 ✅ |
| **T7**（**✅ 2026-09-24 已落地，第九批**） | 实现**归一化管道**（§5.3「★ 归一化管道」/ §9.x P-4）+ 4 组对抗样本用例——已实现 `interfacetester_ai/normalize.py`：去 ANSI → CRLF/CR → LF、空白折叠、NBSP→空格 → **NFKC** → **JSON 反向解码** → **引号族统一**（顺序按 §5.3 钉死，不许重排）+ **偏移映射**（`normalize_with_map().span()/.locate()`，报告定位用）；**单一来源**：`diagnosis.normalize_text` 改为它的薄封装（原先自带一套最小实现，两套归一化是典型口径漂移）；核心难点是"**过度归一化**"——归一化太强会让**编造的引用**也命中，所以判据**成对**：4 组样本必须合并、4 条反例（数字/汉字/大小写/空行结构）必须**不**合并 | `pytest tests/normalize_test.py -q` → **18 passed / 25 subtests**；`python -m interfacetester_ai.normalize` → 「归一化管道自检全部通过：4 组对抗样本逐字相等 / 引用命中率 100% / 偏移映射 / 4 条反例保持不同」，退出码 **0**；**注入式元护栏（实测）**：把 `_ANSI_RE` 换成永不匹配、或把 `_JSON_ESCAPES` 清空 → 自检**必须**非 0（两条都会红）；`bench/write_boundary_scanner.py` 里本模块**零违规**（无写盘点） | **~~T6~~（已落地 ✅）** | 1 ✅ |
| **T8**（**✅ 2026-09-24 已落地，第九批**） | L2 隔离：**作业临时目录 + 清理 + 零污染断言**（§9.x P-3 / 风险 R5）——已实现 `interfacetester_ai/workdir.py`：`JobWorkspace`（`.ai/work/<job_id>/`，进出即建/删，`write()` 自动建父目录）+ `snapshot_tree()` / `diff_snapshots()`（源码树快照，**忽略运行期产物目录**）+ `run_isolated()`（作业前后快照必须一致，否则抛 `WorkspaceContaminated` 并**点名具体路径**）+ **残留上报**（清理失败 → `WorkspaceResidue` 带残留路径，不静默）+ **进程内串行锁**。**两处设计纪律（都来自已发生的教训）**：① 写盘点必须**内联成 `.ai` 字面量开头**——写成"先算变量再 `open`"会被 T18 判"目标不可静态判定"（与越界同级）；② 自检**不碰文件系统**（生产模块只留自己的写盘点，T27 先例），真实文件系统判据全在 `tests/workdir_test.py`。**写盘边界**：登记为**第 6 个写入者**（`workdir → ("roots", (".ai/",))`，注册表共 **8 项**） | 真仓 `git status` 干净；工作区用目录快照 diff——**验收（实测）**：① `pytest tests/workdir_test.py -q` → **9 passed**（含污染注入点名路径、残留注入带路径、串行锁持有/释放、真仓快照不变）；② `python -m interfacetester_ai.workdir` → 「L2 作业隔离自检全部通过」，退出码 **0**；③ `TestRealWorkspaceStaysClean` 在**真仓**跑两个自检后 `diff_snapshots` **为空**；④ `bench/write_boundary_scanner.py` → 包 **0 违规**（**登记前实测 3 处违规**——与 T27 同款"未登记被机器盯住"）；⑤ 自检**实测抓出一个真 bug**：`job_id` 原用 `time_ns() % 1_000_000` 做后缀，连发 200 次**会撞**，已改为进程内单调计数器 | **~~T6~~（已落地 ✅）** | 0.5~1 ✅ |
| **T9**（**✅ 2026-09-24 已落地，第十批**） | golden 集扩到 **15 份**（含 GBK/BOM 文档、含 XML、含业务码）——已实现：`bench/golden/` **3 → 15 组**（每组 `.md` 文档 + 配对 `.yml` 期望产物），覆盖 P0a 验收① 的六类（单步 / **多步依赖** / **含 schema** / **含 XML** / **含业务错误码** / **编码对抗**）。**编码对抗是"真编码"**：`legacy_gbk_doc.md` 以 **GBK** 保存（实测按 UTF-8 读取**必失败**）、`utf8_bom_doc.md` 带 **UTF-8 BOM**（实测 `utf-8-sig` 读时 BOM 被剥离、不进正文）。**多步依赖样本**带 `extract` + 下游 `Bearer $token`（正好压 T21 的 S4 词边界判据） | 基线常量显式更新；4 组编码样本可读——**验收（实测）**：① `python bench/golden_harness.py` → **样本 15 / 通过 15 / 失败 0 / 通过率 100.0%**，退出码 **0**（逐条 [PASS] 留档）；② `tests/golden_baseline_test.py` 的 `GOLDEN_TOTAL/GOLDEN_PASSED` 已显式更新为 **15/15**（该文件是唯一事实来源）；③ `md=15 / yml=15` 一一配对（`test_golden_docs_have_paired_yaml`）；④ GBK/BOM 两份的可读性由 §9.x P-6 的探测顺序与 `doc_quality` 的 D4 承担 | **~~T6~~（已落地 ✅）** | 1.5~2 ✅ |
| **T10** ★（**✅ 2026-09-24 已落地，第十批**） | **把闸门挂到真实样本上统计误伤率**（§3.5 新增纪律）——已实现 `bench/gate_false_positive_audit.py`：对 15 份 golden 的**期望产物**逐份加载 + 跑 `check_testcase()`，统计 **REJECT（误伤）** 与 **PENDING（非误伤但需列出）**，误伤即退出码非 0 并**点名**（哪份 / 哪个编号 / 消息）。**PENDING 不算误伤**（那是 S4 请人工确认的设计意图），但必须列出来 | 15 份 golden 全部过闸门；任何误伤写回 MUST_ALLOW——**实测结论**：**样本 15 / 误伤 0 / 误伤率 0.0% / 带 PENDING 样本 0**，退出码 **0**。★**这条同时把文档里一处"未实测"变成了实测**：T21 落地时"把'被引用'扩展到 headers/params/body 能消掉多少 PENDING 噪声"标注为**未实测**、且写明"需 golden 扩到 15 份后统计（T10）"——**答案是 0 条 PENDING**（§13 与 §3.3 的相关行已同步回填）。**元护栏**：往 golden 目录注入一条已知静默陷阱（S1 伪存在性 `not_equal: [x, ""]`）→ 审计**必须**把它报成误伤并给出编号 **S1**（`tests/gate_false_positive_audit_test.py` 实测） | **~~T9~~（已落地 ✅）** | 0.2 ✅ |
| **T11**（**✅ 2026-09-23 已落地**） | 实现 `project_summary()` 投影 + 单测（§5.6）——已实现 `bench/normalize_projection.py`（`project_summary()` + 自检 `main()`）+ `tests/normalize_projection_test.py`（9 条判据级用例）；**落地时新发现清单漏项：顶层 `time`（summary 级 start_at/duration）**，已补进 §5.6 并钉死用例。T6 建包时迁入 `interfacetester_ai`（**★已于 2026-09-23 随 T6 迁入**：现为 `interfacetester_ai/projection.py`，bench 路径留兼容壳） | 同输入两次投影后逐字节相等——**实测通过**（`test_same_input_projected_twice_is_byte_identical` + 真实产物幂等用例；pytest 9 passed） | 无 | 0.5 ✅ |
| **T12**（**✅ 2026-09-24 已落地，第十一批**） | `.gitignore` 补 `.ai/cache`、`.ai/work`、`.ai/jobs`、`.tmp_golden/`（**在真仓库 `D:\learn\interface-tester` 做**）——已落地：追加一节「AI 旁路包产物」，并写出 **P-7 要求的"产物 / 证据"分界**——`.ai/cache/`、`.ai/work/`、`.ai/jobs/` 与 `.tmp_golden/` 是**产物**（不入库）；`.ai/manifest/`（调用审计）与 `.ai/reviews/`（人工确认记录）是**证据**，本文件**刻意不忽略**它们。**顺带处置一处**：`git status` 暴露 `.tmp_audit2/`（491 个文件，09/22 那批审计的同类临时目录）未被忽略——既有规则 `.tmp_audit/` 是**精确名**、匹配不到它，已单列一行（**只忽略、不删除**：它可能仍是某批次的证据） | 真仓 `git status` 干净——**验收（实测）**：① `git check-ignore -v` 对 5 条路径**逐条命中**（`.ai/cache/x`→L53、`.ai/work/x`→L54、`.ai/jobs/x`→L55、`.tmp_golden/x`→L56、`.tmp_audit2/x`→L61）；② `git status --short` 现为 **`M .gitignore` + 3 个既有未跟踪目录**（`project-one/` / `project-two/` / `selling-products/`）——三者均 **09/22 及更早**（`project-two` 还被 §5.5 / 甲3 引作**真产物来源**），**不是 AI 产物**，其入库与否属**单独决策**，故如实记录、不擅自处置；③ 真仓 `.gitignore` 变更前后均为 **UTF-8 无 BOM** | 无 | 10 分钟 ✅ |
| **T13**（**✅ 2026-09-24 已落地，第八批 = §8.1 阶段行 `S-8`**） | 闸门报告输出 `formatting` 字段（§3.4）——与 S-8 同一件事，实现与验收见 §8.1 的 `S-8` ✅ 行与 §3.4 落地注记 | "L2 通过但 black 没跑"可被看见——**验收（实测）**：① `python -m interfacetester_ai.gates` → 自检输出新增「L2 格式化（S-8）」一行，退出码 **0**；② `pytest tests/l2_formatting_test.py -q` → **19 passed / 31 subtests**（含真跑 black 分支）；③ 注入式元护栏：删掉字段后 `run_selftest()` 返回 **2** | 无 | 0.2 ✅ |
| **T14**（**🟡 2026-09-25 部分落地**——`unknowns` 抽检**已做**：表 B **14 条** → **判对 14**（含机器交叉检查 13 条一致）；**断言语义（表 A）未判**：该批 13 条断言全是 `equal(status_code)`，待"断言合并"落地后重出产物） | 给 R11 加验收：人工抽检断言语义正确率——**为什么现在做不了**：抽检对象是"**模型生成的断言**"，而生成器（`haify gen`）属 **P0a**；它落地前**没有可抽检的样本**（golden 里的断言是**人写的**，抽检它们等于复核作者，而不是复核 R11 说的"可判定但语义错"）。**已就绪的两个前提**：① R11 的边界已在 §9.2 显式写明（"过闸门 ≠ 语义正确"）；② 报告口径"断言数 vs 覆盖字段数"已排进 T28/P1b（T28 等 P0a）。**★落地时的形态先写死，防走样**：抽检 ≥ x 条 → 记录语义正确率 → 阈值由项目定；样本必须**随机抽**（不许挑"看起来会错的"，否则那个正确率没有意义） | 抽检 ≥ x 条，记录语义正确率；验收阈值由项目定 | **已解除**（`haify gen` 已落地、产物已有） | 0.5（**`unknowns` 部分已做 ✅；断言语义待合并**） |
| **T15**（**✅ 2026-09-24 已落地，第十一批**） | 口径唯一化：以 **v7 §8.2** 为准，**废弃 v4 的三个 Web 数字与 v6 的 32~47**——口径本身 v7 已统一（A 26~35 + B 7~13 = **33~48**；裁剪 23~29），本轮补上**机器判据**（`tests/docs_consistency_test.py::TestDeprecatedWorkloadNumbersOnlyAppearAsHistory`）：旧数字**允许**作为历史对照出现，但**同一行**必须含 `v4`/`v6`/`互斥`/`废弃`/`T15`/`方案 4`/`历史`/`承接`/`裁剪`/`重算` 之一——否则读者会把旧数字当成当前口径。**判据成对**：另一条断言"这些数字**仍在**文档里"（防"全删了所以绿"），第三条是**注入式元护栏**（"当前口径：总计 34~48 人日"必须被抓出） | 全仓检索不再出现"34~48"/"32~45"/"32~47"作为**当前**口径——**验收（实测）**：`pytest tests/docs_consistency_test.py -q` → **19 passed**（含新类 3 条：5 处历史对照全部带标记 + 数字仍在 + 注入句被发现） | 无 | 0.1 ✅ |
| **T16**（**✅ 2026-09-24 已按"标注式处置"结案，第十一批**） | 补齐或删除对《方案2-对5.0.1对账》《方案2-工作量重估》的引用——**处置结论**：两者**都不在本工作区**（全仓按文件名检索**零命中**），因此既不能"补齐"、也不该假装有据；v6 起已改为"**不再引用那个数字**"，v8 把这条结论显式化：凡**本仓不存在**的承接环节，一律标注"**不在本仓、不可复核**"，不再以它为论据（§13 的"缺失文档"行、前作 v4 的 26~34 人日行均已如此标注） | 承接链上每一环都能在本仓找到——**验收**：全仓检索这两份文档名，命中的每一处都处于"**标注为不在本仓 / 不再引用**"的语境；承接链正文（头部）只列**本仓确有的** v1~v8 各版方案 | 无 | 0.2 ✅ |
| **T17** ★v7（**✅ 2026-09-23 已落地**） | **补全归一化投影的剔除清单**：`records[*].data.req_resps[*].response.headers` 走白名单（剔 `Date`/`Set-Cookie`/`Server`），并显式决定 `platform` 是否剔除——已实现于 `bench/normalize_projection.py`：响应头白名单（Content-Type/Content-Length/status 头保留）+ `keep_platform` 口径参数 + 顶层 `time` 剔除（新发现漏项） | 同输入**跨秒**跑两次，投影后**逐字节一致**——**实测通过**：两份只在易变字段上不同的输入（`Date` +1s / UUID / 耗时 / stat 全不同）投影后逐字节相等（`test_projection_stable_cross_machine`）；用例名写明同机/跨机口径（`..._same_machine` / `..._cross_machine`）✓（§5.6、X5） | 无（并入 T11） | 0.25 ✅ |
| **T18** ★v7（**✅ 2026-09-23 已落地，第四批**） | **落地红线③ 的可执行形态**：静态扫描按"**写入目标路径 + 模块白名单**"（`llm.py` 禁写；`cache.py`/`manifest.py` 只许写 `.ai/`；`cases/` 只由装配器写）——已实现：`bench/write_boundary_scanner.py`（AST 定位写入类调用 + 静态可判目标前缀，`MODULE_WRITE_BOUNDARIES` 注册表判定；同名异义 `str.replace`/`dict.copy` 不判、`os.replace`/`shutil.copy` 判；目标不可静态判定从严违规；删除类留给 T8）；自检含违规/合规成对夹具 + bench/ 写盘点**计数对账**（`BENCH_KNOWN_WRITES`）+ `interfacetester_ai/` 零违规。原行写"依赖 T6"，实测扫描器先落地、对包的零违规断言在 T6 建包后自动生效（`TestAiPackageBaseline`） | `cases/` 的写盘调用不出现在装配器之外；`cache`/`manifest` 落盘**不**判违规（§2.2、§6）——**实测通过**（自检退出码 0：合规 6 份 / 违规 4 份 7 处命中类别正确 / bench 对账 4 处相等 / 包 0 违规；`tests/write_boundary_test.py` 20 条含注册表↔§6 对账与注入故障元护栏）（§0.4-②、§6） | ~~T6~~（扫描器本体无依赖；包扫描断言随 T6 生效） | 0.3 ✅ |
| **T19** ★v7（**✅ 2026-09-23 已落地**） | **`_classify` 桶名断言 + 形态自检**；桶口径按**可达三桶**（`load/make/compile`）写（若确要 `project` 桶，先修 `_classify`）——已实现：`tests/golden_baseline_test.py::TestClassifyBucketsArePinned`（代表样本逐条归类断言 + 三桶全可达 + 源码级"无第四桶"（`_classify` 不得引用 `FAIL_PROJECT`/`FAIL_OTHER`）+ `FAIL_*` 常量值对账）。原行写"依赖 T6"，实测 `bench` 可直接 import，**无需建包即可落地** | `test_classify_buckets_are_pinned` 全绿——**实测通过**（用例存在且 PASS；"必然失败的样本会红"以源码断言形态承担：`FAIL_PROJECT`/`FAIL_OTHER` 若变得可达，本用例立即红）（§3.2、§3.5） | ~~T6~~（实测无依赖） | 0.3 ✅ |
| **T20** ★v7（**✅ 2026-09-23 已落地**） | **S 闸门三方对账**：判据 ↔ 代码分支 ↔ `MUST_REJECT` 样本一一对应（`contained_by` 已在 v7 按判据删除）——已实现：`tests/silent_traps_test.py::TestGateBranchAccounting` 四用例（① 分支对账：`GATE_FINDING_BRANCHES` 登记表 ↔ `inspect.getsource` 逐闸门数 `findings.append` 调用点；② 编号对账：`MUST_REJECT` 编号 ⊆ 登记表；③ REJECT 样本对账：运行全样本，每闸门累计 REJECT finding ≥ 其 REJECT 分支数；④ S0/S10/S11 与 PENDING 分支（S3 之②、S4）的逐分支行为级探针——含 S11 第三分支"编码不可识别"**新对抗样本** `b"\xff\xff\xff\xff"` 四编码全败） | `test_every_gate_branch_has_a_must_reject_sample` 全绿——**实测通过**（四用例均 PASS，与 `golden_baseline_test.py` 定向合并 28 passed + 56 subtests、退出码 0）；S1 判据与三个实现分支一致（§3.3） | 无 | 0.2 ✅ |
| **T21** ★v7（**★v8 判据加严**）（**✅ 2026-09-23 已落地**） | **扩展 S4 的"被引用"判据**到 headers / params / body / url 里的 `$var` 用法——已按**词边界 / 路径段精确匹配**实现（禁止子串匹配；跨步收集 checks 与 request 模板文本，`$var`/`${var}` 词边界正则） | ① 「取 `token` → 下游 `Authorization: Bearer $token`」的登录步骤：**通过且零 PENDING**——**实测通过**（`test_login_only_extract_passes_with_zero_pending`，探针① 零 finding）；② **★v8 漏报对抗样本**：extract 键 `token` + 同 step 断言 `eq: [body.data.token_type, "Bearer"]` → **必须仍报 S4 PENDING**——**实测通过**（`test_s4_substring_match_must_not_count_as_used`，恢复报出 1 条 PENDING）；③ 词边界负例 `$token_id` 不算 `$token` 使用——**实测通过**（`test_s4_var_word_boundary_not_prefix_matched`） | T1 | 0.15 ✅ |
| **T22**（**✅ 2026-09-24 已落地，第十一批**） | §4.2 G3 的"实测现象"改为现状（空 `teststeps` 已由 **N33** 硬拒），与 §3.3 的 S3 docstring 口径统一——**已落地**：§4.2 的 G3 行现写"空 `teststeps` **已不再只是 warning**——内核 **N33** 已硬拒（`make.py` 里 `not teststeps` 直接抛错，注释原文「空 `teststeps` 必须报错」）"，§0.5-⑥ 与 §13 的"★ 空 `teststeps`"行同步；与 §3.3 的 S3（针对**有 request 无 validate** 的 step）已明确区分为**两件事** | 文档与 `make.py` 的 N33 口径一致——**验收**：三处（§4.2 G3 / §0.5-⑥ / §13）均含 N33 与"不再只是 warning"，且旧说法"空 `teststeps` 只 warning"不再出现于现状描述 | 无 | 0.05 ✅ |
| **T23**（**✅ 2026-09-24 已落地，第十一批**） | 数字**带口径标签（含读取编码）**：`18,840` / `22,173` / `15,767` 全部标注"`Get-Content` 默认编码（GBK）口径"，并补 **UTF-8 正确口径**：总 `22,638` / 非空 `19,267` / 非空非# `15,947`；顺带修 `deploy.md` 的 21 / 19 两处漂移——**已落地**：§12.1 的数字行带**双口径**（GBK + UTF-8，偏差 −465 行，取证见 §12.8-⑤）；**三处算子漂移全部修掉**：`README.md` 的「第 19 个算子」→「**契约断言算子 `jsonschema_match`**」、`deploy.md` 的「21 个断言算子」→「**22 个**」、「第 19 个算子」→「**算子 `jsonschema_match`**」。**★为什么不改成"第 22 个"（实测）**：`jsonschema_match` 按 `BUILTIN_COMPARATOR_NAMES` 的常量顺序是**第 7 个**（总数 22）——"第 N 个"这种写法**本身就是漂移源**（取决于按什么顺序数），所以直接改用算子名 | P-8 的元护栏能把"口径标签 + 读取编码"一起钉住（§7 P-8）；§12.1 的数字行带全口径后可直接复跑——**验收（实测）**：`python -c "from interfacetester import make; print(len(make.BUILTIN_COMPARATOR_NAMES))"` → **22**；全仓检索「21 个断言算子」「第 19 个算子」**零命中** | 无 | 0.2 ✅ |
| **T24**（**✅ 2026-09-25 已落地，P0b 之后**） | `TODO_` 占位用例**只落 `.ai/draft/`**，人工确认后才转 `cases/` 并留痕——**为什么现在做不了**：本行的验收是"带 `TODO_` 的产物**不出现在** `cases/`" + "`.ai/reviews/` 有确认记录"，而**产物由装配器产生**（P0a）。**已就绪的部分**（v8 原注记说"可复用 T18 扫描器与 S-6 的 pending writer 骨架"——**两句都已兑现**）：① T18 扫描器已就位，且 T8 落地时又验证了一遍"**写盘点必须源码可见**"的形态纪律；② S-6 的 `pending.py` 已是"目标路径 + 白名单"的样板；③ `.ai/draft/`（产物）与 `.ai/reviews/`（证据）的分界已在 **T12 的 `.gitignore`** 里写明。**★落地形态先写死**：装配器把 `TODO_` 产物写入 `.ai/draft/`；转 `cases/` 必须经 `.ai/reviews/*.json` 留痕（P3b 的确认门复用同一套） | 带 `TODO_` 的产物不出现在 `cases/`；`.ai/reviews/` 有确认记录（§5.2） | **P0a**（装配器） | 0.2 ✅（**★已落地（2026-09-25，P0b 之后）**：`interfacetester_ai/reviews.py`（人工确认留痕）+ 装配器 `promote_draft`（草稿转正）+ CLI `haify review --list/--approve`。**两条验收都成立**：① 带 `TODO_` 的产物**不出现在 `cases/`**（`draft_only` 硬规则，`tests/assertions_test.py` 钉住）；② **`.ai/reviews/<case>.json` 有确认记录**（转正必然留痕，`tests/reviews_test.py` 钉住）。**★把"留痕"做强了一档（相对本行原文）**：留痕记的是**草稿内容哈希**而不是路径——"确认过"绑到**具体一版内容**上，`require_review` 比对哈希，**确认后又改一行 → 留痕失效、必须重新确认**（否则"签一次、以后随便改都算签过"，留痕退化成橡皮图章，还不如没有）。另三条纪律：**没写 `approver` 就拒**（留痕的意义就是回答"谁确认的"）、**查重不覆盖人手写的用例**、**原子写**（先 `.tmp` 再 `os.replace`，不留"看起来有效的半个文件"）。**写盘边界**：`reviews` 为**第六个登记写入者**（T18 注册表 `("roots", (".ai/",))`）；其自检**刻意不写盘**（夹具写盘归 `tests/`，T27 那笔教训）。**验收（实测）**：① `python -m interfacetester_ai.reviews` → 退出码 **0**；② `pytest tests/reviews_test.py -q` → **18 passed**；③ 端到端（临时工作区）：带 `TODO_` → 拒（退出码 1、`cases/` 未创建）、无 `approver` → 拒、填好 → `cases/filled.yml` + `.ai/reviews/filled.json`、**确认后再改草稿 → 报"又变了"**；④ `python bench/write_boundary_scanner.py` → 包 **0 违规**、退出码 **0**） |
| **T25**（**✅ 2026-09-23 已落地**） | **探针输出留档**：`probe_s3_review/out_*.txt`（三个用例的 findings / REJECT / PENDING / 闸门结果）——已留档 `out_probe_s3_edge.txt`（T26 同款 `reconfigure` 保证重定向字节级 UTF-8），探针 docstring 同步修正（原"用完即删（.tmp_review/）"不实，本就不在 `.tmp_review/`） | 目录下能找到与 §12.5/§12.7 一致的输出文件——**实测通过**（`probe_s3_review\out_probe_s3_edge.txt` 存在且三例结果与 §3.3 实测块一致） | 无 | 0.05 ✅ |
| **T26** ★v8（**✅ 2026-09-23 已落地**） | **修 `bench/probe_silent_gate.py` 的输出编码健壮性**：`main()` 开头 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`；其余三个自检脚本同批统一（`ai_guardrails.py` 无 `main()`，统一在 `__main__` 块并补模块级 `import sys`）——已按此实现，带 `try/except (AttributeError, ValueError)` 防御（不影响被 `tests/` import） | 四个 bench 自检脚本在**重定向 + 无 `PYTHONIOENCODING` + GBK locale** 下退出码**全 0**——**验收通过**：四行全 `exitcode=0`；`cmd /c` 字节级重定向后输出为**纯 UTF-8**（`✓ 已拦下，命中编号：['S1', 'S4']` 正常可读；★注：T21 落地后 REAL 用例的 S4 PENDING 按新判据不再出现——token 被下游 `$token` 引用，命中编号变为 `['S1']`，S1 拦截不变、探针仍通过），对抗逻辑不变；pytest 全套 `1603 passed`（另有 4 个**与本改动无关**的既有失败，取证见 §12.8-① 注记） | 无 | 0.1（计入 S-7 的 0.2，不重算合计） |
| **T27** ★v8（**✅ 2026-09-23 已落地，第七批**） | **生成前的文档体检闸**（= §8.1 阶段行 **S-10**）——已实现：`interfacetester_ai/doc_quality.py`（**纯代码，不调模型**）：① **D1 要素完整性**（URL/method/字段表＝必需，缺任一即拒；类型/必填/枚举/状态码/示例＝可选，缺只提示）；② **D2 内部矛盾**（同字段两种类型；示例 JSON 与字段表类型冲突——**示例片段也算文档**）；③ **D3 可判定率**（有类型字段占比 < 50% → 建议"只生成骨架 + `unknowns`"，**不拒**）；④ **D4 编码可解析**（**复用 `gate_s11_document_encoding`**：坏编码/空文件 → 拒；BOM / HTML 残留 / 表格残缺 → 只提示）。不合格处置：**拒生成 + 落 `reports/NEEDS_DOC_FIX.md`**（必改/建议改两分区，逐条点名切片与改法）交回文档作者。**★落地口径修正（相对本行原文的"逐切片算四类判据"）**：**D1 必需项按整篇判**、D1 的"文档属性"三项（枚举/状态码/示例）也按整篇判——切片是 **LLM 输入单位**，而"文档能不能生成"是**文档级**问题；实测常见形态"`## 请求` 写 URL+method、`## 字段` 放表"，按切片判会给每片都报"缺字段表"（**误伤**）。判据本身未变，只是判的**粒度**落到文档级（并写进模块 docstring 与 §5.1 注记）。**★写盘边界**：`doc_quality` 作为**第五个登记写入者**进 T18 注册表（`("roots", ("reports/",))`）；**未登记时实测被判 3 处违规——其中 1 处是模块自检里的夹具写盘**（`open(empty, "wb")`），处置是**把夹具写盘移进 `tests/`**（生产模块只该留自己的写盘点），不是登记了事。**仍未做（不虚标）**：把本闸**接进生成流程**（P0a ——"不过体检不许 gen"的前置断言） | **违规必红**：缺 URL/method/字段表 → 拒（`<document>` 级、点名缺失项）；同字段两种类型 → 拒；示例与字段表冲突 → 拒；0 字节/坏编码 → 拒（复用 S11，消息含"S11"）。**合规必绿**：健康文档 **0 finding**；**只缺可选要素（示例）→ 放行 + 提示**；非接口切片（`# 变更记录`）**不误伤**；带 BOM / HTML 残留 / 表格残缺 → 提示不拒；可判定率低 → 提示不拒。**元护栏**：把健康文档的 URL/method 行删掉 → 必须从"放行"变"拒"。**验收四元组（实测）**：① `python -m interfacetester_ai.doc_quality`（无参数=自检；重定向 + GBK locale）→ 输出「文档体检闸自检全部通过（四类判据 D1~D4）：合规必绿 / 违规必红（4 类夹具编码正确）/ 元护栏 / 编码（D4）/ 缺项清单」，退出码 **0**；② `python -m interfacetester_ai.doc_quality <doc.md>` → 健康文档「体检通过」退出码 **0**、拒的文档「未通过 + 清单已写入 reports/NEEDS_DOC_FIX.md」退出码 **2**（`tests/doc_quality_test.py::TestCliExitCodes` 用临时 cwd 跑，不污染工作区）；③ `python -m pytest tests/doc_quality_test.py -q` → **21 passed**、退出码 **0**；④ `python bench/write_boundary_scanner.py` → 合规 **8** 份 / 违规 **6** 份 **10** 处 / 包 **0** 违规、退出码 **0** | 无（**实测无依赖**——判据纯代码，唯一外部依赖是同包的 `gate_s11_document_encoding`；接生成流程属 P0a） | 0.5~0.8 ✅ |
| **T28** ★v8（**✅ 2026-09-25 已落地，P0a 阶段 D**） | **引用-断言确定性对账**（强化 §5.1 的 `source_quote` 校验，落在**装配器内**）：**不做语义相似判断**（那是二次幻觉），只做字面/规则级对账——① 断言**字段名**必须在引用句里出现；② `expect` 字面量必须在引用句**字面出现**（数字/枚举/中文取值）；③ **限定词 ↔ 算子**（句子写"可选"不许生成必填断言、"整数"不许 `type_match str`、"200 成功"不许 expect=201）；④ **禁用示例值当期望值**（R11 点名的坑；除非文档明写"固定值"）；⑤ **覆盖矩阵**（文档要素 → 断言，漏的显式进报告/`unknowns`）；⑥ 报告口径：**断言数 vs 覆盖字段数**并列展示（治"一屏 `status_code, 200`"的批量退化）。不满足 ①~④ 者 → **降级进 `unknowns`**，不硬拦（形态可能合法，只是无据） | **违规必红**：引用句讲 `price` 而断言写 `body.data.amount` → 降级；引用句只说"金额"而 expect 硬编码 `99.9` → 降级；示例段的值被 hardcode → 降级；"可选"↔必填断言 → 拒。**合规必绿**：引用句逐字支持字段 + 期望值 → 通过；文档明写"固定值"时用示例段数值 → 放行；字段名一律**路径段 / 词边界**匹配——**不许子串**（`token` ⊂ `token_type` 已踩过一次，§0.5-③ / T21）。**★已落地（2026-09-25，P0a 阶段 D）— 验收（实测）**：① `python -m interfacetester_ai.assembler` → 「装配器自检全部通过（T28 四类对账 + 溯源 + 覆盖矩阵）」，退出码 **0**；② `python -m pytest tests/assembler_test.py -q` → **12 passed**（临时工作区，不污染本仓）——其中 S-4 的实质判据是「**带陷阱的产物不在 `cases/`**」（用空 schema 触发 S1，`glob("cases/*.yml") == []`）、产物必须过内核 `load_testcase_file`、查重硬失败不覆盖、新文件**只在 `cases/` 与 `.ai/`** 下、两条元护栏（掐掉限定词规则 / 把词边界换成子串 → 自检必红）；③ `bench/write_boundary_scanner.py` → 包 **0 违规**。**★两处落地发现（值得记）**：**(a) 判据太糙会误伤**——`status_code` 这类**协议级检查项**不在文档字段表里、`type_match` 的 expect 是**类型名**而非期望值，两者按"字面量必须有出处"判会**误伤合规产物**，已分别用 `_PROTOCOL_CHECKS` / `_TYPE_NAMES` 显式豁免（理由写进模块注释）；**(b) 对账与闸门是两道不同层的防线**——`not_equal: [x, ""]` 这类陷阱会被装配器的 ② **先摘掉**（它连用例都没进，闸门根本看不到），所以 S-4 的闸门测试必须用「**能过对账、过不了闸门**」的形态（空 schema）——**不能用一个测试同时证明两道防线**。**★仍未做（不虚标）**：`haify gen` 对"含示例值但未标固定"的 golden 文档端到端跑一遍（`hardcode 示例值 0 条` + 该字段进 `unknowns`）**属 P0a 阶段 E**（CLI 与修正环尚未落地）；本文件实际名为 `tests/assembler_test.py`（§十 T28 原文预写的 `case_draft_sourcing_test.py` 未采用——对账落在装配器模块内，测试随模块命名）。**★⑤ 端点溯源（2026-09-25 追加，真模型实测）**：T28 前四类**全是断言级**（字段名 / expect 字面量 / 限定词 / 示例值），**没有一条管 step 的端点** —— 于是模型为「1.1 服务地址」那段（只有环境 URL 表、**没有任何接口**）造出的 `GET ${ENV(BASE_URL)}/api/status` **一路进了 `cases/`**，而它的 `source_quote` **照样命中**（引的是「服务地址」那句**真话**）：**"引用句真"不等于"端点真"**。新增 `check_endpoint_in_doc()` + `endpoint_candidates()`（候选串从最严到最松：原样 → 去 `${ENV(...)}` → 路径段 → `urlsplit` 的 path）→ 不命中则**该 step 整体丢弃**（进 `unknowns`，kind=`endpoint-miss`，DEGRADE 级 —— 一个编造的 url 会让用例**打到不存在的路径上**，而它的失败**看起来和真缺陷一模一样**）。**★两处实测驱动的边界修正**：**(a) 丢弃 ≠ 悄悄成功** —— 初版把"端点全丢"处置成 `draft_only`（退出码 0），但 `draft_only` 的语义是"**已知未定**"（`TODO_` 那条：结构完整、只等填值），而"端点全丢"是"**这份文档生成不出东西**"；`assertions_test::test_cli_reports_a_refusal_when_nothing_is_derivable`（「文档里没有可确定的东西 → **闸门拒（退出码 1）**，而不是产出一条空白用例」）**当场把它打红** —— 判据内容对、**处置的层次错**。改法：这一层**什么都不做**，让产物照常走到闸门（S3 断言真空），由**内核**说那句话。**(b) 界线是"危险度"，不是"有没有出处"** —— `assertions.generate_assertions_only` 在文档取不到端点时用 `url or "/"` **兜底**；把 `/`、`${ENV(BASE_URL)}` 这类**无具体路径段**的 url 也判成"编造"，会让产物变成**零 step**，异常从**更靠后的 L2 出口校验**抛出、**抛穿 CLI**（退出码从 1 变成未捕获异常，实测踩到）→ 新增 `has_actual_path()` 划界：**`/`（不指向具体终点）交给闸门，`/api/status`（会打到不存在的路径上）才丢**。**★成对判据**：编造 url → `ParamsError` + `cases/` 干净 + `unknowns` 里有 `endpoint-miss`；合规端点（含 `${ENV(BASE_URL)}/api/order` 形态）→ 照常落 `cases/`；无路径段 url → 照常产出且**不**记成端点无出处。**★姊妹修正 —— "非接口片"的处置（2026-09-25，真模型实测驱动的**分母修正**）**：同一份文档 15 片里**只有 2 片是真接口**（`POST /oauth2/token`、`POST /api/directory/create`），其余 13 片是标题 / 接口概述 / 服务地址 / 公共请求头 / 响应格式 / 错误码表 / 目录服务接口（标题）/ 附录 / 路径格式规范 / 代码参考 / 常见问题。旧口径下那 13 片**照样被调 3 轮模型**（实测 **43 次调用**），而模型只能编端点（`/api/status`、`/api/placeholder`、`/dummy/success`、`/api/file_service`）或给不出合法结构（`l1-schema`），结局在报告里**和真失败混在一起** —— "15 片里 13 片失败"看起来像工具坏了，**实际是分母错了**。三处改动：**(a) 调用前跳过**（`SLICE_ENDPOINT_RE`：片里须有 `METHOD /path` **同现**，覆盖标题 / 表格 / curl 三种写法且**不限行首** —— 真文档里端点常写在表格单元格、行首是 `|`，原先 `ENDPOINT_RE` 的 `^\s*[-*#>\s]*` 会 **miss**；实测本判据对那份文档给出 `[4, 11]`，**恰好是两个真接口**、零误判。判据**刻意从严**：跳过判错的代价是"漏接口"，不跳过的代价只是"多一次调用"，**两个方向不对等**）；**(b) 模型说没有也算跳过**（`steps: []` → `skip-model`，不回喂、不重试、不算失败）；**(c) ★解析层不再拒 `steps: []`** —— 这是**根因**：契约本来就给了"本节没有完整接口时输出空 `steps`"的出口，而 `parse_draft` 把它判成"结构不合法"，回喂给模型的是"你的 JSON 结构不合法"，于是它**唯一的出路就是编一个 step 出来** —— **把诚实判成错误 = 奖励编造**。"零 step 不能当用例跑"是**运行/落盘语义**（内核 L2 出口校验"零步骤但判成功"），仍由落盘层拦，两道防线都没撤。**实测效果（同一份文档、同一模型）**：产出 **1/15 → 2/15**（`T1_2_认证方式` 由"3 轮闸门拒"变为"**第 2 轮 ok**"）、失败 **13 → 0**、模型调用 **43 → 3 次**、退出码由恒为 1 变为 **0**；报告新增 `-`（跳过）态与**逐片跳过原因**（静默跳过比失败更糟："没做"会伪装成"做完了"），`--no-skip-non-interface` 可强制全部送模型（判据再准也不替用户拍板）。`PROMPT_VERSION` → **v2.3**。**★第三处修正 —— 覆盖率口径（2026-09-25，真模型实测驱动的**分母修正之二**）**：两条真模型产物的**唯一断言**是 `status_code 200`，而**文档里根本没有 `200`** —— 它是靠 `_PROTOCOL_CHECKS` 豁免进来的（豁免本身是对的："返回 200 表示成功"这类话里本来就不会出现 `status_code` 这个词）。问题出在**报告口径**，两处都失真：**①** 覆盖率把它算成"覆盖字段 **1** 个"，于是"覆盖 1 / 文档声明 46"读起来像"覆盖了一点"，**实际是覆盖了 0 个文档事实**（"绿了但什么都没测"的另一种形态）；**②** 那个 **46 是算出来的、不是量出来的**（`covered + uncovered` 反推），文档实际声明 **45** 个，虚高的 1 正好就是它。改法：**(a)** 协议级检查项**不计入 `covered_fields`**，单列 `protocol_assertions`；**(b)** 分母改为**量**出来的（`Coverage.documented_fields` ← `documented_fields(doc_text)`）—— **分母要量，不要算**；**(c)** 这条事实**跟着产物走**：新增 unknown kind `protocol-only`（DEGRADE，**不挡落盘** —— 这类用例能挡 4xx/5xx，**有用，只是薄**，所以是"交人工"而不是"拒"），因为只读交付物（`cases/*.yml` + `unknowns.json`）的人看不到控制台那行。判据成对：`assembler` 自检把"`status_code` ∈ `covered_fields`"改成"∈ `protocol_assertions` 且 ∉ `covered`"，并新增**分母必须取自文档**的判据；`assembler_test` 加 4 条（含"只要有一条文档级断言就不再报 `protocol-only`"）。实测（同一份文档、同一缓存回放）：报告行由「覆盖字段 1 个（文档声明字段 46 个）」变为「**覆盖字段 0 个（文档声明字段 45 个）；另有协议级断言 1 条（status_code）**」。 | **P0a**（必须等 `CaseDraft` + 装配器——**不可提前硬做**：对账入口就是装配器里那次 `source_quote` 校验，独立脚本会与之形成**双源漂移**，T6 已用同一性断言防过同类事） | 0.5 ✅（阶段 D） |
| **T29** ★v8（**✅ 2026-09-23 已落地，第六批**） | **归因六分类 + 文档缺陷回流**（§5.3 与 §8.1 P1a 增强）——已实现：`interfacetester_ai/diagnosis.py`（**纯代码，不调模型**）：① **六分类枚举** `CAUSE_TYPES`（新增第六类「**文档与实现不符（文档缺陷）**」）+ `validate_taxonomy()` 对账（**旧五分类被检出缺一格**）；② **证据纪律机器化**（§5.3）：无证据 / 引用非原文逐字子串 / 文档缺陷缺**双侧证据**（文档原句 + 实际响应）→ **一律降级"未知待人工确认"**（`confidence` 归 0）；③ **回流清单** `reports/doc_defects.md`（哪一句 / 与实际哪里不符 / 建议改法，既是交付物也是回归输入），**无缺陷不落盘**；④ `normalize_text` 只做归一化**最小可判子集**（完整管道归 **T7**，口径边界已在 §5.3 注记标明，不假装完整）；⑤ **写盘边界**：`diagnosis` 作为**第四个登记写入者**进入 T18 注册表（`("roots", ("reports/",))`，§6 与 §2.2 同步）——**未登记时实测被判 2 处违规、显式登记后 0 违规**（"扩白名单必须签字"的机器形态）。**仍未做（不虚标）**：`haify analyze` 接入（evidence 包装配 / LLM 调用 / `analysis.md` 端到端）仍属 **P1a**；"含错误文档" golden 类目随 **T9/P0a**；L3 沙盒探针为 P1a 可选项 | **违规必红**：旧五分类 → `validate_taxonomy` 检出缺「文档与实现不符」（**分类枚举缺格必红**）；文档缺陷缺 doc 侧或 response 侧证据 → 降级；**编出来的引用**（非原文逐字子串）→ 降级；无证据 → 降级（`confidence`=0）；枚举外类别 → 降级；无文档缺陷却创建 `reports/` → 红。**合规必绿**：双侧证据齐全 → 保持「文档与实现不符」；普通类别有 `log` 证据即可保持；引用带 ANSI/CRLF 噪声 → 归一化后仍命中（**不因噪声误伤**）；**元护栏**：把合规样本的响应引文改一个字 → 必须从"保持"变"降级"（证明校验真的在比对原文）；**注册表对账**：`MODULE_WRITE_BOUNDARIES` 6 键、`diagnosis` 根固定 `reports/`。**验收四元组（实测）**：① `python -m interfacetester_ai.diagnosis`（重定向 + GBK locale）→ 输出「归因六分类与文档缺陷回流自检全部通过：分类枚举 6 类齐全 / 缺格必红（元护栏）/ 证据纪律（双侧必绿·缺一侧·编引用·无证据 全降级）/ 回流清单（有缺陷才落盘、根形态正确）」，退出码 **0**；② `python -m pytest tests/diagnosis_taxonomy_test.py -q` → **17 passed + 9 subtests**、退出码 **0**（含文档对账用例 `test_taxonomy_matches_v8_documentation`）；③ `python bench/write_boundary_scanner.py` → 合规样本 **7** 份 / 违规样本 **5** 份 **9** 处命中 / 包 **0** 违规、退出码 **0**；④ 回流清单在临时工作区落盘 `reports/doc_defects.md`、无缺陷时不创建（`test_no_doc_defect_writes_nothing`） | 无（**实测无依赖**——纯代码，不需模型/样本/装配器；`haify analyze` 接入属 P1a） | 0.4 ✅ |

> **★建议落地顺序（T27~T29，2026-09-23 文档级新增）**：**① T29（0.4）→ ② T27（0.5~0.8）→ ③ T28（0.5，等 P0a）**。**进度：① T29 已落地（同日第六批）、② T27 已落地（同日第七批），两者见上表各自 ✅ 行（含实测四元组）；③ T28 未落地**（**必须等 P0a 的装配器**，验收四元组待填实测）。三条理由：
> - **先做零依赖的（① ②）**：T29 的分类枚举/报告渲染/判据用例只需**构造的 findings**，
>   T27 只需**文档解析**——两者都能"当天可验收"，符合本仓"零依赖的先落地"先例
>   （T18/T19/T20 都是**实测**"原行写依赖 X、实测无依赖"之后才落地的；**T29 与 T27 都已按此落地**
>   ——两者的依赖列都从"弱依赖/待实测"改为"**实测无依赖**"，见上表 ✅ 行）；
> - **T29 排第一，因为它止的是正在流血的地方**：文档错的产物现在只能被塞进
>   「用例错误 / 真实缺陷」→ **文档永远不会被修**、同类错误下次继续污染（R15）。
>   它不需要模型、不需要样本、改动最小，收益最直接（**✅ 已落地**——六分类 + 双侧证据纪律
>   + `reports/doc_defects.md` 回流，见 §十 T29 ✅ 行）；
> - **T28 必须等 P0a**：对账的入口是**装配器里那次 `source_quote` 校验**；提前硬做只能
>   做成独立脚本 → 与装配器**双源漂移**（T6 刚用同一性断言防过这类事）——
>   这一条**不写进"可提前"的措辞**。
>
> 配套三项：**R15 / R16 两条风险本轮已写入 §9.2**（不必等落地）；"含错误文档" golden
> 类目随 T9/P0a 的 golden 扩编一并做；`断言数 vs 覆盖字段数` 的报告口径落进 P1b / P3a
> （**随 T28 一起**——T28 未落地前该口径无处可算）。
>
> **★第八批更新（2026-09-24）**：本序列未列的 §8.1 两条阶段行 —— `S-7`（CI 门禁）与
> `S-8`（`formatting` 显式化）**已同日落地**（零依赖，见 §8.1 各自 ✅ 行与 §12.8 第八批段）。
> 它们与"配套三项"里的**报告口径**同类：**把已存在的信息变成可见的门禁/字段**。
> **本序列剩余仍是 ③ T28**（必须等 P0a 的装配器）。
> **★人日与合计口径**：T27~T29 合计 **1.4~1.7 人日**，**暂不计入 §8.1 / §8.2 合计**
> （与 T26 的 0.1 同款处置：v6→v7→v8 已三轮数字漂移，是否入表留待用户决定，
> 避免第四轮漂移）；三条均为**新增行**，**不改任何既有行的验收标准**。

---

## 十一、未收敛的分歧与待决事项（**本文不假装它们已定案**）

### D1 承接链上有两份文档不存在

v4/v5 的"承接链"都包含《方案2-对5.0.1对账》与《方案2-工作量重估》，
但**这两份文档不在本工作区**（实测 `docs/` 下无、全仓按文件名检索无命中）。
→ 后果：v4/v5 反复引用的"对账文 §1.4/§1.8/§三"与"26~34 人日"**无法复核**。v6 起已改为不引用（§8.2），但**原始推导需要补回或明确作废**。
- **★已决策（2026-09-26，D1：方案维护者）**：**作废**。① 本文件头部承接链已加"**承接链口径**"行，把这两份标注为
  **不在本仓、已作废、不再引用**；② 作废理由与检索证据登记在 **`docs/ARCHIVED.md`** §二；③ **工作量口径以 §8.2 的
  33~48 人日（裁剪后 23~29）为唯一来源**，"26~34 人日"只作为历史对照出现（不得当作现行口径）。
  **判据**：`docs/` 与真仓**按文件名递归检索零命中**（可复跑）；T16（§十）此前已按"标注式处置"结案，本条把措辞定死。

### D2 架构落点：改内核 vs 旁路包（**存在文档级冲突**）

- **v3/v4/v5 的立场**：旁路包 `interfacetester_ai`，入口 `haify`，内核 **0 改动**。方案 3 §8 明写「不要直接把 LLM 嵌进 `runner.py` 或 `parser.py` 的核心执行链路」。
- **0923 三份文档的立场（比 v4/v5 更新）**：直接改内核。实测引用：
  - `按文件逐条改造加入AI清单.md` 列出「文件 1~9」全是内核文件，含 `parser_ai = subparsers.add_parser("ai")`；
  - `细化到每个模块如何加入AI0923.md` 给出内核安装目录下的真实路径，并定义入口 `interfacetester ai gen|analyze|fix|summary`；
  - 该文档**自身矛盾**：一面给出内核子命令，一面又写「最合理的方式：新建一个旁路模块，而不是直接改核心 package」。
- **为什么要坚持旁路包（v6 / v7 立场，附理由）**：内核是**构建仓产物**——34 个模块被《架构与调用链.md》的模块地图元护栏覆盖，`examples/**/*_test.py` 是被 AST 等价测试钉住的入库生成物，`converters` 有四个源的跨源一致性测试。改内核会同时触发 `docs_consistency_test`、`generated_artifacts_drift_test`、`wheel_content_test`，且**必须重出 wheel 再分发**；而旁路包**删包即回滚**。
- **★已决策（2026-09-26，D2 架构评审）**：**确认旁路包为唯一主线**——入口 `haify`、内核 **0 改动**（红线①
  `test_kernel_sources_never_import_ai_package` 钉住，实测 `1 passed / exit 0`）；0923 三份已标记为
  **已废弃（历史方案）**（三份头部各加横幅、**原文保留**供追溯），登记见 `docs/ARCHIVED.md`。
  **决策依据**：若按 0923 改内核，方案 5 立的写盘前置闸门（S 系列 / 红线③）会被绕开——闸门只长在旁路包里；
  另：内核是构建仓产物，改内核会同时触发 `docs_consistency_test` / `generated_artifacts_drift_test` /
  `wheel_content_test` 且**必须重出 wheel 再分发**，旁路包**删包即回滚**。
  ~~- **待决**：这需要一次**架构评审**明确表态。若"禁止改内核"是硬约束，则 0923 三份应标记为**已废弃**，否则方案 5 立的写盘前置闸门会被绕开。~~
  （原文保留：本次决策即按此推论执行。）

### D3 风险姿态：是否允许"受控自动修正"（**存在文档级冲突**）

- **v4/v5 的立场**：❌ 任何"自动 apply 修复"——`fix` 永远只有建议，`--apply` 永不存在。
- **冲突方**：方案 3 §5.13「方式 C：AI 自动修正（受控）」；`按文件逐条改造加入AI清单.md` 的 `apply_fix_if_confirmed(...)` → `patch_yaml(case_path, ...)` 真落盘，并有「第三阶段：低风险自动修正」；`prompt 模板与接口设计草案0923.md` 允许响应返回 `"requires_human_confirmation": false`。
- **★已决策（2026-09-26，产品决策 D3）**：**不支持自动 apply——这是最终形态，不是"暂未实现"**。
  成对判据在册：`tests/fix_test.py::test_cli_has_no_apply_flag`（`--apply` 永不存在）+
  `TestGateAndZeroWrite::test_target_case_file_mtime_is_unchanged`（**目标用例 YAML 的 mtime 不变**）；
  `fix.py` 930 行仅 1 处写盘点（写临时文件供真 `load_testcase_file` 做双前置校验，**不是**目标用例）；
  若将来要开口子才需要新增的 `INTERFACETESTER_AI_AUTOFIX` 开关，**全仓零命中**（= 未实现）。
  文档侧已同步：技术版《使用手册-AI协作层》与**非技术版**（Q1 + §五 速查）都写明"没有自动改这个开关"。
  ~~- **待决**：这是**产品决策**，不是技术细节。本文立场是**不支持**——理由：一旦允许自动落盘，"人工确认是唯一语义闸门"这个前提就不成立了（R3/R11 都会失去兜底）。若业务确实需要，最低要求是：只允许"从不可判定改成可判定"方向的补丁，且必须可回滚 + 留痕 + 全量日志。~~
  （原文保留：本次决策即按该立场定案；"若业务确实需要"的三条最低要求仍作为**将来重启此议题的门槛**记录在案。）

### D4 成功标准尚不可证伪

v4 说"先测后承诺"，但把**模型采购/部署/联调**排除在估算外，也没有定义"用哪个模型、跑多少样本、通过率多少算可接受"。
→ ~~待决：需要一份**模型基线测量方案**（模型清单、样本集、指标、阈值）才能让 P0a 的验收真正成立。~~
→ **★已完成（2026-09-26，D4 决策）**：方案已产出 = **`docs/模型基线测量方案.md`**（模型清单 / 样本集与分母口径 /
指标定义 / 阈值 / 复跑命令 / 测量纪律 / 实测结果与逐篇明细）；P0a 验收① 的口径已改为
「**接口片 → 产出用例 ≥ 86.7%**」（§12.8-⑩ ④），"落盘产物 100% 过 L2"保留但**标注为重言式**。
本文 §8.2 只保证**工作量口径可复核**，不保证**技术方案已可行**——后者需要真实模型跑一轮才有结论。

### D5 本文（v7）未做的事

- **没有**修改 `bench/`、`tests/` 与内核的**任何代码**——v7 的 12 处修正**全部落在文档与待办**上
  （S3 三态、S4 判据扩展、投影清单已于 2026-09-23 随 v8 落地——见 §12.8-③④ 修复注记与 §十；
  桶名断言（T19）、三方对账（T20）亦已落地；**红线③ 扫描口径（T18）连同 T6 建包、S-6 落盘
  已于同日第四批落地**——v7 级待办至此全部收口，仅剩 T22~T24 三条文档/流程级小项）；
- **没有**重写 v6 的主线结论（三层架构 / 七条红线 / 五道关顺序 / S1~S11 / 旁路包 / 工作量主表结构），只改 §0.4 列出的 12 处；
- **没有**真实模型联调，因此 §六 的接入层参数（超时、重试、预算）仍为**设计值**，非实测值
  （**★v8 注（2026-09-25）**：已在本地 Ollama 上跑通 15 篇 golden 的真模型一轮（§12.8-⑩⑪）——
  超时/重试已实测**并测出默认 60s 不够**（★2026-09-25 默认值已改为 **180s**）、缓存命中率/token/花费已实测；**预算上限与多模型完整对照仍未实测**，§12.6 有逐条状态）；
- **没有**验证《方案 2》的内容（本次核对范围为方案 3~6 与内核源码 + 本工作区实测）；
- `docs/` 下 `使用说明.md`、`使用教程.md`、`能力清单.md`、`架构与调用链.md`、`按文件逐条改造加入AI清单.md` 等文档**未逐条核对**，对它们只有"是否与本文冲突"级别的粗查（结果见 D2/D3）。

### D6 v7 留下的三处小决策（**2026-09-26 已定案：三项均采纳 v8 默认值**）

| 决策 | v7 采用的默认 | 备选 | 影响面 |
| --- | --- | --- | --- |
| 投影里 `platform` 剔还是留 | **剔**（这样投影跨机可比），用例名写 `..._cross_machine` | 留（信息更多）→ 用例名写 `..._same_machine` | 只影响 §5.6 的用例命名与对照范围 |
| `_classify` 的 `project` 桶 | **按可达三桶写报告口径**，不动内核 | 改 `bench/golden_harness.py` 让 `project` 可达（要补分支 + 补样本，属改 `bench/`） | 影响 §3.2 的桶清单与 T19 |
| `contained_by` 空值要不要拦 | **不拦**（`contained_by: [x, []]` 恒假，不构成真空通过；拦了是假报） | 若产品口径要求"期望值写空集合"也必须拒，则补实现 + 补 MUST_REJECT 样本 | 影响 §3.3 S1 的判据行与 T20 |

- **★已决策（2026-09-26，D6：技术负责人）**：**三项一并确认采纳默认值**，无需再讨论。逐项判据（实测）：
  - **`platform` 剔**：`projection.py::project_summary(..., keep_platform=False)` 默认刨顶层 `platform`，模块自检**成对**断言
    （`False` 必剔 / `True` 必留），用例名口径 `..._cross_machine`；
  - **`_classify` 可达三桶** `{load, make, compile}`：`bench/golden_harness._classify` 只返回这三桶，
    `FAIL_PROJECT` / `FAIL_OTHER` **不可达**——`tests/golden_baseline_test.py::TestClassifyBucketsArePinned`
    钉住桶集合，并断言 `_classify` **源码里不得出现**这两个常量（防悄悄变可达）；
  - **`contained_by` 空值不拦**：`gates.py::gate_s1_pseudo_presence` 只有 `not_equal` 空值与 `jsonschema_match`
    空 schema 两支，**无** `contained_by` 支（`[x, []]` 恒假）；`tests/` 里 7 处 `contained_by` 全是算子本身用例，
    **无一条**要求 S1 拦它。
- **若将来要改**（同机对照 / 让 `project` 桶可达 / 拦空集合）：**先改代码 + 判据，再同步 §3.2 / §3.3 / §5.6 与本行**——
  顺序不能反，否则口径会静默漂移。

### D7 生成默认落点：`cases/` → `.ai/draft/`（**2026-09-26 决策并落地**）

- **起因（实证，不是推测）**：装配器原行为是"结构/闸门通过且无 `TODO_` → **直接复制进 `cases/`**"，
  于是"装配成功"与"进了正式用例目录"是同一件事。取证见 `docs/未做完的待决策项.md` §九 9.4：
  一份**只断言 `status_code`** 的薄用例（`cases/T1_2_认证方式.yml`）躺在 `cases/` 里，
  而 `.ai/reviews/` 里**零留痕**——它和"人工复核过的用例"在调用方看来**没有区别**。
  ★这类缺陷**不报错**：退出码 0、闸门全绿、产物在"正式目录"里——正是本仓最反对的
  「**没做**伪装成**做完了**」。
- **决策**（对应评审《接口测试智能化改造项目评审分析》§五 的落点表 / 升级计划 WP1.1，按
  `未做完的待决策项.md` §九 的"三档"裁剪后只做第一档）：
  1. `assemble()` **只写 `.ai/draft/`**；进 `cases/` 的唯一路径是 `promote_draft()`
     （`haify review --approve` + `.ai/reviews/` 留痕，T24）；
  2. 新增 **`interfacetester_ai/quality.py`**（纯函数、零写盘）：`ok()` 只表示"装配流程完成"，
     "能不能用"由 `quality.status` 回答；`protocol-only` / `merge-skip` / 未决降级项 / `TODO_`
     → `needs_review`；★**没有隔离执行证据永不到 `runtime_verified`**（评审 §二 的口径）；
     ★**人工审核那一步也接上了**（WP1.2，同批）：`quality_of_draft()` 从既有产物**复算**
     "当前那一版"的状态（不新增写盘点；传 `draft_path` 时**重跑静态闸门**，且**闸门是硬结论**
     ——`--resolve-blocker` 覆盖不了），`promote_draft()` **未处置的 blocker 就拒**
     （`--resolve-blocker <码>` 逐条点名；自由文本不算处置），`review --list/--approve` 显示状态；
  3. 顺带修掉评审 §四 P1 的两处实证缺陷（同批）：**条件返回字段**（`avatarUrl` 写明"仅当
     `withAvatar=true` 时返回"）不再被**无条件断言**；**枚举列头同义**（`说明` ≡ `取值范围`/
     `允许值`/`约束`）——`order_create.md` 的 `status` 原先漏了 `contained_by`，
     **同根因**还漏了 `price` 的范围断言。
- **钉住点（实测）**：
  - `tests/assembler_test.py::TestAssembleProducesADraft`（草稿落盘 + `cases/` 干净）、
    `::test_assemble_never_touches_an_existing_case`（装配**不碰**人工用例）；
  - `tests/assembler_test.py::TestProtocolOnlyAssertions::test_the_fact_travels_with_the_artifact`
    （仅协议断言 → `needs_review` + 只落草稿）；
  - `tests/gen_pipeline_test.py::TestEndToEndProducesRealCases`（链路级：落 `.ai/draft/` 且过内核加载）；
  - `tests/quality_test.py`（状态机 / 非法转换被拒 / ★**穷举参数组合**证明"没有运行证据永不到
    `runtime_verified`" / 纯函数零写盘元护栏）；
  - `tests/assertions_test.py::TestConditionalReturnAndColumnSynonyms`（两条 golden 回归**成对**判据）；
  - `tests/reviews_test.py::TestBlockersMustBeResolved` + `::TestReviewSurfacesQualityToHumans`
    （**未处置 blocker → 拒**；逐条点名 → 放行且留痕含 `quality_status`/`blockers`/`resolved_blockers`；
    `--list`/`--approve` 必须显示质量状态；复算**只读**）；
  - **C（只读交付面）**：`tests/web_readonly_test.py::TestPendingPageShowsQuality`（看板待确认页）、
    `tests/audit_test.py::TestQualityIsProjected`（审计时间线的"质量"列，**老留痕标"未记录"**）、
    `tests/export_test.py::TestPendingSnapshotShowsQuality`（离线快照）、
    `tests/reviews_test.py::TestDraftsOverview`（**口径只有 `reviews.drafts_overview()` 一份**，
    且 `with_gate=False` 必须标 `not_probed`——**不猜**）。
- **兼容面（用户可见，已同步）**：CLI 退出码 `0` = "**草稿产出完成**"（不等于可进 CI）；
  `README.md`（含基线数字 2253→2312 passed / 2385→2443 全量）、`docs/使用手册-AI协作层.md`、
  `docs/新手快速上手.md`、`docs/模型基线测量方案.md`，以及 `probe_p0a_llm/measure_p0a_l2.py`
  （产物既收 `cases/` 也收 `.ai/draft/`——**否则重跑基线会得到"0 条产出"这种假结果**）。
- **不在本次范围**：第二档（共享契约解析 `contract.py`/`markdown_contract.py`）与第三档
  （参考服务 / 变异评测 / 自动晋级白名单）**按 §九 的裁剪不做**；`quality.py` 只预留了
  `RuntimeEvidence` 接口面，**当前不产生任何运行证据**（所以 `runtime_verified` /
  `auto_promotable` 在现在这条链路上**不会出现**——这是设计，不是缺陷）。

## 十二、复核记录（本文哪些话是实测的、哪些是估算的）

> 本节的目的是让**每一条技术论断都可被独立复核**。凡未列入本节的，均为设计决策或估算。

### 12.1 环境与资产（实测通过）

| 核实项 | 实测结果 |
| --- | --- |
| 版本 / 解释器 | `interfacetester 5.0.1`，`Python 3.12.10` |
| CLI 入口 | `hrun` `hmake` `hconvert` `interfacetester` 均在 `.venv\Scripts` |
| golden 基线 | `bench/golden_harness.py -v` → login / order_create / user_info **逐条 PASS**，**3/3 = 100.0%** |
| golden 护栏 | `pytest tests/golden_baseline_test.py -q` → **5 passed** |
| 三处护栏自检 | `bench/ai_guardrails.py` → 「三处护栏自检全部通过」 |
| 护栏用例 | `pytest tests/ai_guardrails_test.py -q` → **20 passed, 20 subtests passed** |
| 闸门自检 | `bench/silent_traps.py -v` → 「拦得住 11 条 / 不误伤 15 条」全部通过 |
| 闸门用例 | `pytest tests/silent_traps_test.py -q` → **14 passed, 29 subtests passed** |
| 端到端对抗 | `bench/probe_silent_gate.py`（**交互控制台**）→ 「对抗验证通过」；**★v8**：重定向 + 默认 GBK locale 下曾 `UnicodeEncodeError`、**退出码 1**（§0.5-①、§12.8-①）——**★v8 当日修复（T26 落地）后：重定向下退出码 0、输出纯 UTF-8**；"实测通过"必须注明**运行环境** |
| 三套护栏合并 | `pytest tests/silent_traps_test.py tests/golden_baseline_test.py tests/ai_guardrails_test.py -q` → **39 passed, 49 subtests passed** |
| 内核规模 | 本地源码树 `interfacetester/**/*.py` = **34 个模块**；`18,840` 是**非空行**（`Measure-Object -Line` 口径，与方案 4 声称一致）；**总物理行 22,173**；非空非注释 15,767（**★v7 补口径**，§0.4-⑩）。**★v8 补正**：以上三数均系 `Get-Content` **默认编码（GBK）**读取口径——GBK 解码 UTF-8 中文会吞换行，偏差 **−465 行（−2.1%）**；**UTF-8 正确口径 = 总 22,638 / 非空 19,267 / 非空非# 15,947**，取证命令见 §12.8-⑤ |
| 算子数 | `len(BUILTIN_COMPARATOR_NAMES)` = **22**（tuple） |
| 文档漂移 | `deploy.md` 写「21 个断言算子」→ 与代码常量 **22** 不一致（漂移为真）；**★v7 再补一处**：同文件第 204 行写「**第 19 个算子**（`jsonschema_match`）」，同一病第二个实例 |
| 关键词穷举 | 内核全包 `静默\|假通过` = **233** 处；仅顶层模块 = **148** 处 |
| 工作区 git 状态 | `D:\tester` **无 `.git`、无 `.gitignore`**；`D:\learn\interface-tester` 有 `.git`，其 `.gitignore` **不含** `.ai/`、`.tmp_golden/`、`bench/` |
| 缺失文档 | 《方案2-对5.0.1对账》《方案2-重估》**全仓无命中** |

### 12.2 源码级论断（逐条对照源码）

| 论断 | 实测 |
| --- | --- |
| `converters/__init__.py` 公开面 | 恰好 `IRCase` `IRStep` `IMPLEMENTED_SOURCES` `SUPPORTED_SOURCES` `build_case_dict` `emit_case` `validate_emitted_case`（**★v7 澄清**：这是 `__all__` 的口径；`dir()` 另有 `emit_yaml`、`ir` 两个子模块对象，不属公开 API）；`IMPLEMENTED_SOURCES = ("curl","har","postman","openapi")`（四元 tuple）；`SUPPORTED_SOURCES` 是**四键 dict** |
| `validate_emitted_case` 有副作用 | docstring：「副作用是会在同目录生成 `*_test.py`（原本 `hrun` 也会产出），并做一次 black 格式化」 |
| `format_pytest_with_black` 只告警 | docstring：「超时/失败都只告警，不阻断 hmake」；默认超时 60s |
| `pytest_files_run_set` 全局 | `make.py` 模块顶层 `pytest_files_run_set: Set = set()`，跨调用累积 |
| `not_equal` 对 `None` 的行为 | 实现即 `assert check_value != expect_value` → `None != ""` 为真 → **PASS** |
| `_warn_value_not_found` | docstring 明写 `not_equal: ["…",""]` 在字段**根本不存在**时**通过**；并解释为何只告警 |
| `_assertion_expects_null` | 存在于 `response.py`；`response.py` 内被用于放过 `eq: [x, null]` 一类写法 |
| 4 个数值算子的告警 | `_warn_if_both_operands_are_strings` 分别作用于 `greater_than` / `less_than` / `greater_or_equals` / `less_or_equals` —— 正是 S2 的目标集合 |
| 只拦能精确判定的形态 | `builtin/comparators.py` 注释原文：「本仓的取向是「只拦能精确判定的形态」，假报会让真报被无视」 |
| 函数正则不含引号 | `parser.py::function_regex_compile = re.compile(r"\$\{([a-zA-Z_]\w*)\(([\$\w\.\-/\s=,]*)\)\}")`；注释明写带引号**不报错** |
| `ENV` 缺变量响亮报错 | `parser.py::get_mapping_function` 中 `ENV`/`environ` → `utils.get_os_environ`；后者 `except KeyError: raise exceptions.EnvNotFound(...)` |
| `??` 语法不存在 | `parser.py` / `loader.py` 中 `??` **零命中** |
| 写盘三层护栏 | `cli.py::main_convert` 内依次校验 `.yml` 名 / schema 名 / 生成物模块名（经 `normalize_module_segment`），并有孤儿 schema 报告 |
| 元护栏 | `tests/docs_consistency_test.py` 含 `TestBaselineNumbersHaveOneSource`、`TestBuiltinComparatorCountHasOneSource`、`TestArchitectureDocCoversEveryModule` |
| AST 等价护栏 | `tests/generated_artifacts_drift_test.py` 用 `ast.dump` 归一化比对，并带"非空跑自检" |
| UUID 冲突 | `runner.py::self.case_id = self.case_id or str(uuid.uuid4())`；`step_request.py` 注入 `f"interfacetester-{runner.case_id}-{str(int(time.time() * 1000))[-6:]}"` |
| `docs/ci` 引用 | 「GitLab CI 客户内网交付**首选**」；坑 3 内网离线依赖；坑 4 总是产出两类报告；坑 7 `loader.project_meta` 是**全局**的；坑 8 `hmake` 会重写 `examples/**/*_test.py` |
| ★ 分桶的"钉住" | **v7 更正**：`tests/golden_baseline_test.py` **没有**断言桶名——全文的 `failure_buckets` 与 `r['bucket']` 只出现在失败提示字符串里，真正被钉的只有 `GOLDEN_TOTAL = 3` / `GOLDEN_PASSED = 3`；且 `bench/golden_harness.py` 的 `FAIL_PROJECT` / `FAIL_OTHER` **不可达**（`_classify` 兜底先返回 `make`）→ **可达桶只有 `load/make/compile`**（§0.4-③） |
| ★ S1 的判据分支 | **v7 更正**：`gate_s1_pseudo_presence` 只有 `not_equal` / `contains` / `jsonschema_match` **三个分支**；`contained_by` 在整个 `bench/silent_traps.py` 里**零出现**（§0.4-④） |
| ★ 空 `teststeps` | **v7 更正**：内核 **N33** 已硬拒——`make.py` 注释原文「空 `teststeps` 必须报错」，代码里 `not teststeps` 直接抛错；**不再**是"只 `logger.warning`"（§0.4-⑥） |
| ★ `examples` 下的 mock server | **v7 更正**：`examples/*/mock_server.py` **不存在**；全仓只有 `tests/mock_server.py` 与 `project-two/mock_server.py`（`README.md`、`docs/ci/README.md` 指的都是前者）（§0.4-⑨） |
| ★ S4 的"被引用"判据 | **v7 实测**：只看 `validate` 里断言的 `check` 路径（`referenced = (path in checked) or any(n in c for c in checked)`）→ 实跑「取 `token` → 下游 `Authorization: Bearer $token`」**仍然报 S4 PENDING**（对该形态是误报），§3.3 已改为"任何形式的使用"（§0.4-⑦） |
| ★ 真实 `summary.json` 的响应头 | **v7 实测**：`details[0].records[0].data.req_resps[0].response.headers` = `{'Server': 'BaseHTTP/0.6 Python/3.12.10', 'Date': 'Tue, 22 Sep 2026 15:02:22 GMT', 'Content-Type': …, 'Content-Length': …}` → 秒级 `Date` 使 v6 的投影清单**不完整**（§0.4-①、X5） |
| ★ v4 错指针的出处 | **v7 更正**：在 v4 的 **§二**（P3a 看板那条）与 **§4.5**（`fix` 产物行），**§4.3 里没有**（§0.4-⑫） |

### 12.3 summary.json 结构（用真实产物逐字段核对）

```
'validators' in records[0]           -> False
'validators' in records[0]['data']   -> True
'log' in details[0]                  -> True     （log 是 records 的兄弟）
records[0] keys  : name, step_type, success, data, elapsed, content_size, export_vars, attachment
record.data keys : success, req_resps, stat, address, validators
record.data.stat : content_size, response_time_ms, elapsed_ms
validate_extractor[0] : {comparator, check, check_value, expect, expect_value, message, check_result}
```

→ 方案 4 的 `details[i].records[j].validators…` **确为错误**；方案 5 的更正**确为正确**。

### 12.4 算子别名与白名单（行为实测，非文档抄录）

```
get_uniform_comparator（别名归一器，未知名字原样返回）：
  eq→equal  equals→equal  ne→not_equal  gt→greater_than  ge→greater_or_equals
  lt→less_than  le→less_or_equals  str_eq→string_equals  len_eq→length_equal
  gte→gte   lte→lte   equals2→equals2   print→print        ← 未归一

ensure_known_comparators（真正的白名单闸门，喂正确的步骤形态）：
  eq / equal / ge / le / gt / lt / ne / str_eq / len_eq / contained_by → 通过
  gte / lte / equals2 / ge2 / print → 拒绝（FunctionNotFound）

⚠️ 形态陷阱（P-9 的来源）：
  若传入的"步骤"没有 validate 键（例如把 [{"eq": [...]}] 当成 teststeps），
  ensure_known_comparators **不报错、静默放行** —— 循环体一次都不进。
```

> 说明了**必须用两个不同函数**：`get_uniform_comparator` 不能当校验器用（它放行一切未知名字）。

### 12.5 v6 新发现的 S3 缺陷（可复现；v7 已逐字复现，见 §12.7 甲1；★v8 已于 2026-09-23 修复）

```
运行：.\.venv\Scripts\python.exe probe_s3_review\probe_s3_edge.py

① 登录步骤只有 extract、无 validate（多步链路常见写法）
     findings: ['S4', 'S3']   REJECT=1  PENDING=1   闸门结果: 被拒
② 按闸门建议写 validate: []（"显式声明意图"）
     findings: ['S4', 'S3']   REJECT=1  PENDING=1   闸门结果: 被拒   ← 提示的改法不可行
③ 对照：补一条 eq: [status_code, 200]
     findings: ['S4']         REJECT=0  PENDING=1   闸门结果: 通过
```

根因：`gate_s3_assertion_vacuum` 的判据是 `if step.get("request") and not validates:`——
空列表在 Python 中为假，于是"**显式声明为空**"与"**字段缺失**"被压成同一个二值判断。
§3.3 的重定义即为修复方案。

> **★v8 修复注记（2026-09-23，T1~T3 落地）**：三态已实现（见 §3.3 实测块）——
> ① `findings: []` 通过（token 被下游引用，S4 零 PENDING）、② `['S3'] PENDING=1` 通过
> （提示的改法自此可行）、③ `[]` 通过；输出留档 `probe_s3_review\out_probe_s3_edge.txt`（T25）。

### 12.6 未验证 / 属估算的部分（**请勿当成实测**）

| 项 | 状态 |
| --- | --- |
| §8.2 A/B 表的每一行人日 | **估算**（唯一例外：S-0~S-3 的 2.8 人日已完成实测） |
| 前作的 26~34 人日 | **不可复核**（其来源文档不在本仓） |
| §六 的超时/重试/预算参数 | **★2026-09-25 部分实测（§12.8-⑩⑪）**：超时与重试走真模型跑通（`gemma4:latest` 15 篇 golden）；★并实测出**默认值不够**——`DEFAULT_TIMEOUT = 60s` 会让本地大模型**全部以"传输超时"失败**（症状看起来像"模型不行"），换模型必须同时调 `INTERFACETESTER_AI_TIMEOUT`（已写进 `deploy.md` §5.1）；**缓存命中率 65.4% / token `prompt 79276 ▪ completion 42708` / 花费 0.5002 均已从 `.ai/manifest/` 实测**（§12.8-⑦）。**仍未实测的**：多模型横向对照的完整结论（本机跑不动更大的模型）。**★2026-09-26 预算上限策略已补测**（隔离体检闸 + `--fake`，仓外临时目录）：预算放行→退出码 **3**、片级 payload 超预算→**1**、单片超切片上限→**1**、体检闸在前→**4**、缺配置→**2**；★并发现**口径不符**——`EXIT_BUDGET = 5` 在 `gen` 路径上没有端到端入口（片级预算被记为"片失败"，最终统一 **1**），`tests/` 对该码**零判据**（处置见决策清单 §四 M-4） |
| §8.1 中各阶段的"通过率 ≥ 实测基线" | **★2026-09-25 已测出（§12.8-⑩）**：P0a 的 golden 15 份 → **接口片 13/15 = 86.7% 产出**；★同时发现这条判据的**分母写错了**——"对最终产物量 L2"因"闸门在前"**恒为 100%**（重言式），有效分母是**送进模型的接口片**；口径变更待 D4 决策人确认 |
| §9.2 R11 的"语义正确率"阈值 | **未定**，需项目决策。**★2026-09-25 补**：抽检表已备（表 A，13 条 = 该轮**全部落盘断言**）但**未判**——该批产物 **13 条断言全是 `equal(status_code)`**（零字段级），协议级语义无歧义，**这条阈值在此批产物上测不出**（不是没测，是"无牙可咬"）；须等"`gen`（模型通路）+ 确定性映射合并"落地后重出产物再判（§12.8-⑪ ⑥/⑨） |
| 0923 三份文档与 v4/v5 的冲突细节 | 已逐条 grep 复核关键引用（D2/D3 所引原文均可检索命中），但**未逐行通读全文** |
| §一 表 5 条"共同判断"的原文引用 | 引文来自三份文档正文，已核对；但方案 3 面向 4.3.5，其**版本特定**的细节本文未采纳 |
| **§8.2 新增行 A8b（0.55）/ A11（0.3）的人日** | **估算**——与 A/B 其余行同口径：整张底表是估算，只有已完成的 **2.8** 是实测（§12.1） |
| **§5.6 的"跨机可比"** | v7 只证明了"**含 `Date` 的清单必不收敛**"；跨机对照**未实测**（D6 里把"剔 `platform`"定为默认值，属设计决策）。★v8 注：T11/T17 已落地（`bench/normalize_projection.py`），**跨秒确定性已实测**（对抗样本逐字节相等）；两台真机对照仍未实测 |
| **§3.3 扩展 S4 判据后的收益** | **✅ 已实测（2026-09-24，T10 落地）**：golden 扩到 **15 份**后统计——**PENDING 噪声 = 0**（`bench/gate_false_positive_audit.py` 实测：样本 15 / 误伤 0 / **带 PENDING 样本 0**）。这就是 T21 判据（"被引用"扩展到 headers/params/body、**词边界三支判定**）的**全量收益**：原本会被 S4 报成 PENDING 的合法写法（`extract` 的键在下游 headers 里以 `Bearer $token` 被引用）**全部不再误报** |
| **「probe_silent_gate 对抗验证通过」（v7 §12.1）** | v7 与独立评审都只验证了**交互控制台**场景；**重定向 / CI 场景 v8 才实测**（UnicodeEncodeError、退出码 1，§12.8-①）——"实测通过"从此必须注明运行环境四元组（命令、环境、输出、退出码） |
| **§0.4-⑩ 的 15,767（v7 未给取证命令）** | v8 已补口径（`Get-Content` GBK、非空且非#行、含 docstring）与命令，并发现其含编码系统偏差（−465 行；UTF-8 正确口径 15,947，§12.8-⑤） |
| **§0.4 的 12 处修正本身** | **全部为实测**（命令与输出见 §12.7）；"改完是否真的不再漂移"取决于各待办是否落地——**文档改完 ≠ 代码改完**。★v8 注：T1~T3/T11/T17/T19/T20/T21/T25（连同 T26）已于 2026-09-23 落地；**T18/T6/S-6 亦已于同日第四批落地**（见 §12.8 末注记）——v7 立的 26 条待办仅剩 T4/T5/T7~T10/T12~T16/T22~T24（多为 P0a/文档级流程项） |

### 12.7 ★ v7 复核记录（对 v6 的再复核：**甲组 11 项通过**、**乙组 12 项修正**）

> 本节是 v7 的全部依据。方法：把 v6 里每一句"**已实现 / 已钉住 / 已完整 / 已收口**"拿去对回**代码、测试文件与真实产物**，
> 能跑的就跑，跑不了的就读源码；所有命令都在 `D:\tester` 下、用 `.venv` 的 5.0.1 执行，可逐条复制。

#### 甲、复核**通过**的 11 项（v6 的说法为真，v7 一字未改）

| # | v6 的断言 | 我的复核方式 | 实测结果 |
| --- | --- | --- | --- |
| 甲1 | §12.5 的 S3 缺陷（误伤"仅提取"步骤 + 提示的改法自己不可行） | 实跑 `probe_s3_review\probe_s3_edge.py` | **逐字复现**：① `findings ['S4','S3'] REJECT=1 PENDING=1` 被拒 ② `validate: []` 的输出与①**逐字相同**、仍被拒 ③ 补 `eq: [status_code, 200]` → `['S4'] REJECT=0 PENDING=1` 通过 |
| 甲2 | S3 根因是 `not validates` 把"显式为空"与"缺失"压成二值 | 读 `bench/silent_traps.py` | `if step.get("request") and not validates:` ✓（§12.5 的根因分析为真） |
| 甲3 | §5.5 的证据指针必须带 `.data.` | 用真产物 `project-two/logs/login_flow.summary.json` 逐字段读 | `'validators' in records[0] → False`、`'validators' in records[0]['data'] → True`、`'log' in details[0] → True`；`records[0]`、`data`、`data.stat`、`validate_extractor[0]` 的字段清单与 §5.5 **完全一致** |
| 甲4 | §5.6 的"逐字节一致"必然恒红（UUID / Request-ID） | 读 `runner.py`、`step_request.py` + 看 `project-two/logs/` | `self.case_id = self.case_id or str(uuid.uuid4())`；`f"interfacetester-{runner.case_id}-{str(int(time.time()*1000))[-6:]}"`；`.run.log` 文件名实测就是 UUID ✓ |
| 甲5 | §12.1 的三套护栏数字 | 实跑三份用例合并 | `39 passed, 49 subtests passed`（= 5 + 20 + 14 ✓；subtests 20 + 29 = 49 ✓ **内部自洽**） |
| 甲6 | 闸门自检"拦得住 11 / 不误伤 15" | 实跑 + 读常量 | `MUST_REJECT = 11`、`MUST_ALLOW = 15` ✓（`GateReport.ok` = `not rejects`，所以 PENDING 不破坏 MUST_ALLOW）（★v8 注：S-2b/T2 落地后 `MUST_ALLOW = 17`，见 §12.8-④ 修复注记） |
| 甲7 | 34 模块 / 22 算子 / 关键词 233、148 | pwsh 计数 + python | `34`、`22`、`233`、`148` **全部一致** |
| 甲8 | §8.1 与 §8.2 的口径自洽（v6 的卖点） | 逐项加和复算 | ✓ 在 v6 口径下完全成立：CLI = 27.7~37.2 = A(24.9~34.4) + 已完成 2.8；Web 7~13 = B；总计 32~47（v7 只补两行后重算，结构未动） |
| 甲9 | P-9 的形态陷阱（喂错形态静默放行） | 实跑 `ensure_known_comparators([{'eq':[…]},{'print':['x']}])` | **不报错、静默放行** ✓（v6 描述的输入形态**是准确的**） |
| 甲10 | v4 的 Web 增量三处互斥；`gte`/`lte` 不是别名；`get_uniform_comparator` 不是校验器 | 检索 v4 + 实跑两个函数 | ✓ 全部为真（v4 里 `8~14`、`8~14`、`6~11` 与 `34~48`/`32~45` 均在；`gte→gte`、`equals2→equals2`；`ensure_known_comparators` 拒 `gte`） |
| 甲11 | D2/D3 对 0923 三份文档的引用、`.gitignore` 分层判据、`deploy.md` 的 21 算子漂移 | 检索 | ✓ 全部命中（`parser_ai = subparsers.add_parser("ai")`、`apply_fix_if_confirmed`→`patch_yaml`、"最合理的方式：新建一个旁路模块"、`requires_human_confirmation": false`；真仓 `.gitignore` 不含 `.ai/ .tmp_golden/ bench/`；`deploy.md` 写 21） |

#### 乙、v7 修正的 12 处：证据与取证命令

```powershell
# ① 投影清单不完整：真实产物里响应头带秒级 Date（v6 只剔了请求侧 Request-ID）
.\.venv\Scripts\python.exe -c "import json;d=json.load(open('project-two/logs/login_flow.summary.json',encoding='utf-8'));print(d['details'][0]['records'][0]['data']['req_resps'][0]['response']['headers'])"
#  → {'Server': 'BaseHTTP/0.6 Python/3.12.10', 'Date': 'Tue, 22 Sep 2026 15:02:22 GMT', 'Content-Type': ..., 'Content-Length': ...}
#    另：顶层 platform = {'interfacetester_version': '5.0.1', 'python_version': 'CPython 3.12.10', 'platform': 'Windows-11-...'}
#    → 按 v6 的清单实现，"投影后逐字节一致"跨秒即红（§5.6 / X5 / T17）

# ② 红线③ 与 §6 的缓存、审计写盘直接冲突（§2.2 原文"LLM 模块里禁止 open( / Path.write"，§6 要求 cache/manifest 落 .ai/）

# ③ "分桶已被 golden_baseline_test 钉住"不成立；且 project 桶不可达
Select-String -Path tests\golden_baseline_test.py -Pattern 'bucket|GOLDEN_TOTAL|GOLDEN_PASSED'
#  → GOLDEN_TOTAL = 3 / GOLDEN_PASSED = 3（第 35-36 行）被钉；bucket 只出现在失败提示字符串（第 83、103 行），无任何 assert
Select-String -Path bench\golden_harness.py -Pattern 'FAIL_LOAD|FAIL_MAKE|FAIL_COMPILE|FAIL_PROJECT|FAIL_OTHER'
#  → FAIL_PROJECT="project" 有定义，但 _classify 兜底 return make → 可达桶只有 load/make/compile（§3.2 / T19）
#    ★v8 修复注记（2026-09-23）：T19 已落地——tests\golden_baseline_test.py::TestClassifyBucketsArePinned::test_classify_buckets_are_pinned
#      钉住三桶（代表样本归类断言 + 源码级断言 _classify 不引用 FAIL_PROJECT/FAIL_OTHER + FAIL_* 常量值对账），实测 PASS

# ④ S1 判据里的 contained_by 零实现
(Select-String -Path bench\silent_traps.py -Pattern 'contained_by' | Measure-Object).Count      # → 0
.\.venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'.');from bench.silent_traps import MUST_REJECT;print([c for c,_,_ in MUST_REJECT])"
#  → S1 只有 3 条样本（not_equal / contains / 空 schema），恰好对应实现的三个分支（§3.3 / T20）

# ⑤ ⚠ **分析推演**（本条无命令输出——独立评审 P1 指出：v7 的原声明把它混作"实测"）
#    只有"形态自检"、没有"判据↔分支↔样本"的对账 → ④ 就是这么漏出来的（§3.3 / §3.5 / G7 / R14）

# ⑥ G3 的"空 teststeps 只 warning"已过期
Select-String -Path interfacetester\make.py -Pattern '空 `teststeps` 必须报错' -Context 0,6
#  → 注释原文「空 `teststeps` 必须报错，不能生成一个「零步骤但判成功」的用例」，代码 not teststeps 直接抛错（§4.2 / T22）

# ⑦ S4 只看断言的 check 路径 →「取 token 用于下游 header」被误报 PENDING
.\.venv\Scripts\python.exe probe_s3_review\probe_s3_edge.py
#  → 案例① 的 token 明明被下游 header（Authorization: Bearer $token）用掉了，S4 仍报 PENDING：
#    referenced = (path in checked) or any(n in c for c in checked)，checked 只来自 validate 的 check（§3.3 / T21）
#    ★v8 修复后复跑（T21 已落地）→ 案例① 零 finding：checks 跨步收集 + $token 词边界命中（§12.8-③ 修复注记）

# ⑧ ⚠ **分析推演**（本条无命令输出——独立评审 P1 指出：v7 的原声明把它混作"实测"）
#    §5.2 的 TODO_ 用例会过 L1/S/L2 从而落 cases/，与 §8.3 的"先只进 .ai/draft/"打架（§5.2 / T24）

# ⑨ examples/*/mock_server.py 不存在
Get-ChildItem -Recurse -Filter mock_server.py | Where-Object { $_.FullName -notlike '*\.venv\*' }
#  → 只有 tests\mock_server.py 与 project-two\mock_server.py（§2.5）

# ⑩ 18,840 是"非空行"，总物理行是 22,173
(Get-ChildItem interfacetester -Recurse -Filter *.py).Count                                       # → 34
((Get-ChildItem interfacetester -Recurse -Filter *.py | Get-Content | Measure-Object -Line).Lines)  # → 18840（Measure-Object -Line 跳空行）
(Get-ChildItem interfacetester -Recurse -Filter *.py | ForEach-Object { (Get-Content $_.FullName).Count } | Measure-Object -Sum).Sum   # → 22173（总物理行）（§12.1 / P-8 / T23）

# ⑪ 裁剪顺序第 2、5 条口径：33−3−6−1−0.5 = 22.5；48−4−12−2−1 = 29 → 23~29（v6 的第 5 条 −0~1 从未进合计）（§8.2）

# ⑫ v4 错指针的真实出处是 §二 与 §4.5，不是 §4.3
Select-String -Path 'docs\interfacetester智能化改造方案4.md' -Pattern 'validators'
#  → 第 161 行（§二 P3a 看板的"读 summary.json 的 details[].records[].validators"）与第 331 行（§4.5 的 fix 产物行）；§4.3 无
```

> **v7 的自我约束（★v8 按独立评审 P1 修正）**：本节 12 条里，**10 条**都能在上述命令里看到输出；
> **⑤ 与 ⑧ 两条是"分析推演"**（结构性观察 / 逻辑推演，**没有**对应命令输出），
> v8 已在这两条上显式标注 ⚠——原声明写"**没有一条**是靠'读文档觉得不对'得出的"，
> 那对 ⑤⑧ 来说是**不准确的**（结论本身成立，但取证等级不同）。
> 这也是本文对 §一"纪律二"的自我适用：**说 v6 错了，就得指出错在哪一行**——
> 以及**说'实测'时，就得真的给出那一行输出**（P1 正是拿这条标准量的）。
> ⑤⑧ 两条的**后续**：⑤ 由 T20 落地兑现（判据↔分支↔样本三方对账）、⑧ 由 T24 兑现（TODO_ 落盘纪律）。

### 12.8 ★ v8 复核记录（对 v7 的再复核：**5 处修正，全部为换环境再实测发现**）

> 本节是 v8 的全部依据。方法：把 v7 每一句"**实测通过 / 已完成**"拿到**它没测过的环境**里再跑——
> 换重定向、换控制台编码、查退出码、数测试函数。所有命令都在 `D:\tester` 下执行，可逐条复制；
> 独立评审对 v7 抽查约 40 项论断**无一造假**（§12.7 甲组全部成立），本节 5 条是评审与 v7 **共同漏掉**的。

| # | v7 的说法 | v8 复核方式 | 实测结果 |
| --- | --- | --- | --- |
| ① | §12.1 / 附录 A：「`bench/probe_silent_gate.py` → 对抗验证通过」 | **交互控制台**下跑通过；再用**重定向（CI 收集日志的标准形态）+ 不设 `PYTHONIOENCODING`** 跑，查 `$LASTEXITCODE` | 交互：通过 ✓。重定向：L117 `print("  ✓ 已拦下…")` 的 `✓`（U+2713）→ `UnicodeEncodeError: 'gbk' codec can't encode` → **退出码 1**；同条件其余三脚本退出码 0（输出全为 GBK 可编码字符）。**S-7 接 CI 第一天就红——红的是取证脚本自己**（→ T26、G8）。**★v8 同日修复注记（2026-09-23）**：T26 已落地（四个脚本入口 `reconfigure(encoding="utf-8", errors="replace")`），**复跑本行同命令 → 四行全 `exitcode=0`**；`cmd /c "python bench\probe_silent_gate.py > file 2>&1"` 字节级重定向后输出为**纯 UTF-8**（`✓ 已拦下，命中编号：['S1', 'S4']` 正常可读——★注：T21 落地后该编号演进为 `['S1']`，见 §12.8-③ 修复注记；另：PowerShell `*>` 会按 GBK 二次解码导致显示乱码，取证落盘须用 cmd 字节级重定向）；pytest 全套 `1603 passed, 119 skipped, 1152 subtests`——另有 4 个**与本改动无关**的既有失败：README 总数 1712≠1726（数字过期）、uploader 扩展缺 `filetype` 依赖、`docs/细化到每个模块如何加入AI0923.md` 的 8 个 site-packages 死链（editable 安装后失效）、外网 `test_run_request` 网络断言失败——均非 bench/本方案文档引入。**★v8 数字联动注记（2026-09-23，随 T19/T20）**：+5 用例后全套实测 **1622 passed / 119 skipped / 3 failed / 1179 subtests**（`pytest tests --ignore=examples -q`，210.56s）；其中 README 总数一项已按该测试指引把基线改准（全量 1712→**1744**、确定性口径 1591→**1617**，README「六」与 deploy.md 同源更新——这是 v8 三批代码新增护栏用例的必然联动）；外网断言本轮实测**通过**；剩余 2 项环境遗留不变（0923 文档 8 死链、uploader 缺 `filetype`，已显式写入 README 基线行的"不计入基线"注记） |
| ② | §8.1 S-2「14 条 + **4 道元护栏**｜已完成」；§9.3 G2「4 道元护栏（v7 增补 2 道）」 | 枚举 `tests/silent_traps_test.py` 全部 `def test_` 函数，逐一比对 §3.5 的四道元护栏名 | 15 个测试函数；**已落地元护栏只有 2 道**：`test_gate_actually_detects_injected_fault`、`test_allow_set_would_catch_overblocking`（v6 立）；**`test_every_gate_branch_has_a_must_reject_sample`、`test_classify_buckets_are_pinned` 均不存在**——它们是 T19/T20 交付物。v7 把待办写成了已完成（**R14 自己中招**）。**★v8 修复注记（2026-09-23）**：T19/T20 已当日补齐——复跑本行枚举命令 → `tests/silent_traps_test.py` 现 **22** 个 `def test_`（含 `test_every_gate_branch_has_a_must_reject_sample`（L260）及 `TestGateBranchAccounting` 配套 3 用例）；`test_classify_buckets_are_pinned` 落在 `tests/golden_baseline_test.py::TestClassifyBucketsArePinned`（v8 复核时该文件本无此用例）；定向 pytest 两文件 **28 passed + 56 subtests**、退出码 0——§0.5-② 的"2 道已落地 + 2 道待做"自此改为"四道全部落地" |
| ③ | §3.3 / T21 对 S4 只讨论**误报**方向 | 构造漏报场景：extract 键 `token` 从未被校验，同 step 断言 `eq: [body.data.token_type, "Bearer"]`，调 `gate_s4_extract_without_assertion` 数 S4 findings；对照组断言 `status_code` | 漏报场景 **S4 findings = 0**（`'token' ⊂ 'body.data.token_type'` 子串命中 → 静默）；对照组 **S4 findings = 1**（正常报 PENDING）。**判据只往"多放行"偏**——T21 扩展若沿用子串匹配，漏报面同步放大（→ T21 判据加严）。**★v8 同日修复注记（2026-09-23）**：T21 已落地（路径精确 / 路径段 / 词边界三支 + 跨步引用），**复跑本行命令 → 漏报场景 S4 findings = 1（恢复报出）**；漏报对抗样本已入 pytest（`test_s4_substring_match_must_not_count_as_used`）与自检（`S3-ok3`） |
| ④ | §3.4 / §8.1 S-6：「**S3(部分)** → PENDING 落盘」 | 实跑 `probe_s3_review\probe_s3_edge.py` 三例 + 读 `gate_s3_assertion_vacuum` 源码 | ② `validate: []` 仍 **REJECT**、输出与①（无 validate）**逐字相同**——当前实现**一律 REJECT**，**PENDING 态不存在**；它是 §3.3 重定义后的目标态（T1 交付物）。S-6 写的落盘对象「S3(部分) 的 PENDING」**暂无可落之物**。**★v8 同日修复注记（2026-09-23）**：T1~T3 已落地（三态判定），**复跑本行三例 → ① `findings: []` 通过、② `['S3'] PENDING=1` 通过（不再被拒）、③ `[]` 通过**，输出留档 `probe_s3_review\out_probe_s3_edge.txt`；S-6 的落盘对象自此齐备（**★S-6 落盘本身亦已于同日第四批落地**：`interfacetester_ai/pending.py`，见本节末"第四批落地注记"） |
| ⑤ | §0.4-⑩ / §12.1：总物理行 `22,173` / 非空 `18,840` / 非空非注释 `15,767`——**15,767 未给取证命令** | 用 `Get-Content`（默认编码 / `-Encoding UTF8`）两套口径逐行统计，交叉验证 | GBK 口径：非空 18,840、**非空非# = 15,767**（18,840 − 3,073 = 15,767，**含 docstring**）——数字可复现但 v7 没给命令；UTF-8 口径：**总 22,638 / 非空 19,267 / 非空非# 15,947**。**GBK 比 UTF-8 少 465 行**：UTF-8 中文三字节的尾字节（0x80-0xBF）被 GBK 当双字节首字节，连同后面的 0x0A 一起**吞成一个"字符"——换行被吃掉**，中文注释越密偏差越大（→ T23 口径补全） |

```powershell
# ① 取证：重定向 + 默认编码（模拟 CI 收集日志）下四个自检脚本的退出码
#    （坑：cmd 一行式里的 %ERRORLEVEL% 在整行解析时就展开、拿到的是旧值——必须用 PowerShell $LASTEXITCODE）
Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
foreach ($s in @('probe_silent_gate','golden_harness','silent_traps','ai_guardrails')) {
    & .\.venv\Scripts\python.exe "bench\$s.py" *> $null
    "{0,-18} exitcode={1}" -f $s, $LASTEXITCODE
}
#  → probe_silent_gate  exitcode=1     ← UnicodeEncodeError: 'gbk' codec can't encode '\u2713'（main() 的 L117 附近）
#    golden_harness     exitcode=0
#    silent_traps       exitcode=0
#    ai_guardrails      exitcode=0
#    同一脚本在交互控制台跑则通过（Windows 控制台走 UTF-16 API，Python 交互模式不落 GBK）——
#    这就是 v7 与评审都称"实测通过"、CI 却会红的原因：**两边测的根本不是同一个环境**

# ② 取证：元护栏只有 2 道已落地
Select-String -Path tests\silent_traps_test.py -Pattern '^\s*def (test_\w+)'
#  → 15 个 def；含 test_gate_actually_detects_injected_fault（L117）、test_allow_set_would_catch_overblocking（L142）
#    不含 test_every_gate_branch_has_a_must_reject_sample、test_classify_buckets_are_pinned（= T19/T20 交付物）
#    ★v8 修复后复跑（T19/T20 已落地）→ 22 个 def；含 test_every_gate_branch_has_a_must_reject_sample（L260）
#      （test_classify_buckets_are_pinned 在 tests\golden_baseline_test.py::TestClassifyBucketsArePinned）——§12.8-② 修复注记

# ③ 取证：S4 子串匹配静默漏报
.\.venv\Scripts\python.exe -X utf8 -c "import sys;sys.path.insert(0,'.');from bench.silent_traps import gate_s4_extract_without_assertion,GateReport;steps=[{'name':'login','request':{'method':'POST','url':'/login'},'extract':{'token':'body.data.token'},'validate':[{'eq':['body.data.token_type','Bearer']}]}];r=GateReport();gate_s4_extract_without_assertion(steps,'case',r);print('S4 findings:',len([f for f in r.findings if f.code=='S4']))"
#  → S4 findings: 0        ← token 从未被校验，却因 'token' ⊂ 'body.data.token_type' 被判"已引用"（静默漏报）
#    把 check 换成 'status_code'（对照组）→ S4 findings: 1（正常报 PENDING）
#    ★v8 修复后复跑（T21 已落地）→ 本漏报场景 S4 findings: 1（恢复报出）；对照组仍 1

# ④ 取证：S3 当前一律 REJECT（PENDING 态不存在）
.\.venv\Scripts\python.exe probe_s3_review\probe_s3_edge.py
#  → ② validate: [] 的输出与 ①（无 validate）逐字相同、均 REJECT——三态是 §3.3 的目标态（T1 交付物）
#    ★v8 修复后复跑（T1~T3 已落地）→ ① findings [] 通过；② ['S3'] PENDING=1 通过；③ [] 通过（留档 out_probe_s3_edge.txt）

# ⑤ 取证：15,767 的口径与 GBK/UTF-8 编码偏差
$py = Get-ChildItem interfacetester -Recurse -File -Filter *.py | Where-Object {$_.FullName -notmatch '__pycache__'}
$gbk = $py | ForEach-Object { Get-Content $_.FullName }                                    # 默认编码（GBK）
($gbk | Where-Object {$_.Trim() -ne ''}).Count                                             # → 18840（非空）
($gbk | Where-Object {$_.Trim() -ne '' -and -not $_.TrimStart().StartsWith('#')}).Count    # → 15767（非空非#，含 docstring）
$utf8 = $py | ForEach-Object { Get-Content $_.FullName -Encoding UTF8 }
$utf8.Count                                                                                # → 22638（总物理行）
($utf8 | Where-Object {$_.Trim() -ne ''}).Count                                            # → 19267（非空）
($utf8 | Where-Object {$_.Trim() -ne '' -and -not $_.TrimStart().StartsWith('#')}).Count   # → 15947（非空非#）
#    GBK 比 UTF-8 少 465 行 ≈ −2.1%——口径标签不仅要写"非空行"，还要写"用什么编码读的"（T23）
```

> **v8 的自我约束**（承 §12.7）：本节 5 条同样没有一条是"读文档觉得不对"——每条都有命令与输出；
> 且每条命令都注明**运行环境**（重定向与否 / 控制台编码 / 退出码取法）。这是对 §12.7 自我约束的补强：
> **"实测通过"必须四元组齐全：命令、环境、输出、退出码——缺一个，换个控制台它就是红的。**

> **★v8 第四批落地注记（2026-09-23，= §十 T18 + T6 + §8.1 S-6，v7 级待办的最后三件）**：
> ① **T18 红线③ 写盘白名单静态扫描器**：`bench/write_boundary_scanner.py`（AST 定位写入类调用，
> 按「目标前缀 + 模块白名单」注册表 `MODULE_WRITE_BOUNDARIES` 判定：`llm` 禁写 / `cache`/`manifest`/`pending`
> 只许 `.ai/` / `assembler` 写 `cases/` 与 `.ai/`；未登记模块一律违规；同名异义 `str.replace`/`dict.copy`
> 不判、`os.replace`/`shutil.copy` 判；删除类留给 T8 运行期快照）。验收四元组——命令
> `cmd /c "python bench\write_boundary_scanner.py > file 2>&1"`（重定向 + GBK locale）：输出
> 「红线③ 写盘边界扫描自检全部通过：合规样本 6 份 / 违规样本 4 份 7 处 / bench 对账 4 处 /
> interfacetester_ai 0 违规」，退出码 **0**；护栏用例 `tests/write_boundary_test.py` 20 条
> （违规必红/合规必绿 + 同名异义 + 注册表↔§6 对账 + bench 计数对账 + 注入故障元护栏）。
> ② **T6 建包**：`interfacetester_ai/`（平铺就地布局 + `pyproject.toml`，gates ← `bench/silent_traps.py`、
> projection ← `bench/normalize_projection.py` **整体迁移**，bench 两路径留**兼容壳**转发——同一性断言
> `test_shim_reexports_are_identical_objects` 防双源漂移；`llm/cache/manifest/schema/prompts/evidence`
> 占位含边界纪律 docstring；`pending` 即 S-6）。验收四元组——命令 `python -m interfacetester_ai.gates`
> 与 `python bench\silent_traps.py`（壳 CLI）：均输出「11 条对抗样本全部命中 / 17 条合法写法全部放行」、
> 退出码 **0**；`python -m interfacetester_ai.projection` 退出码 **0**；既有
> `tests/silent_traps_test.py`（22 条）与 `tests/normalize_projection_test.py`（9 条）**零改动**通过
> （import 走壳）；内核零改动：迁移只动 `bench/`、新建 `interfacetester_ai/`、新增
> `tests/`——`interfacetester/` 目录未触碰（`test_kernel_sources_never_import_ai_package` 红线①
> 反向依赖断言钉住，全套回归亦证行为不变）；包形态自检 `tests/interfacetester_ai_pkg_test.py` 9 条。
> ③ **S-6 pending 落盘**：`interfacetester_ai/pending.py`（闸门保持零副作用——§0.6；writer 由调用方
> 显式调用，写盘行内联字面量前缀 `.ai/pending/`，文件名 `<case>.pending.json` 符 §5.2 口径，
> items 与 `Finding.to_dict()` 同构）。验收四元组——`tests/pending_list_test.py` 7 条
> （S3/S4 PENDING 必落盘、无 PENDING 不落盘、闸门零副作用、`.ai/` 根形态）全绿。
> **数字联动**：+36 用例（T18 20 / T6 9 / S-6 7），全套实测 **1658 passed / 119 skipped / 3 failed /
> 1213 subtests**（135.66s，README 修复**前**口径——3 failed = README 总数项〔+36 使过期差额放大，
> 已按测试指引修复〕+ 2 项环境遗留〔0923 死链 / uploader 缺 filetype，不计入基线〕）；确定性口径
> **1653 passed / 119 skipped / 6 deselected / 2 failed / 1213 subtests**（89.36s，README 修复后复跑，
> 仅剩 2 环境遗留）；`--collect-only` 对账 **1780**（= 1653 + 2 + 119 + 6 ✓）；
> README「六」与 deploy.md 已同源更新（1617→1653 / 1744→1780 / 1179→1213）。

> **★v8 第五批（同日，纯文档级——未动任何代码）**：针对「**模型读错文档 / 文档质量低 / 文档本身写错
> → 生成的 YAML 必然不准**」这一问题，新增**三条待办 + 两条风险**（**全部显式标注"未落地、
> 验收四元组待填实测"——不虚标，见 R14 纪律**）：
> - **T27 = §8.1 新增阶段行 S-10「文档体检闸（生成前置）」**：把"这份文档配不配生成"变成纯代码
>   判定（要素完整性 / 内部矛盾 / 可判定率 / 编码可解析），不合格 → **拒生成 + `NEEDS_DOC_FIX.md`
>   交回文档作者**，或降级"骨架 + `unknowns`"——**不把缺失交给模型猜**；
> - **T28「引用-断言确定性对账」**：补 §5.1 的"**句在 ≠ 意对**"（R11 的边界），四类字面/规则级判据
>   （字段名 / expect 字面量 / 限定词↔算子 / 示例值禁用），**落在 P0a 装配器内**（不可提前硬做）；
> - **T29「归因六分类 + `doc_defects.md` 回流」**：补 §5.3——新增「**文档与实现不符（文档缺陷）**」，
>   让文档缺陷**回流给作者**并沉淀为回归样本（"含错误文档" golden 类目）；
> - 风险 **R15**（文档缺陷无归属：五分类里根本没有"文档错"这一格）与 **R16**（分叉型静默：
>   模型"替文档纠错"→ 用例跑绿但文档/用例/实现三方不一致，`source_quote` **拦不住**）；
> - **建议落地顺序 ① T29（0.4）→ ② T27（0.5~0.8）→ ③ T28（0.5，等 P0a）** 及三条理由，
>   已写入 §十 表后的「★建议落地顺序」块；人日合计 **1.4~1.7，暂不计入 §8.1/§8.2 合计**
>   （避免第四轮数字漂移，与 T26 同款处置）。
>
> 本轮**未改任何代码、未改任何既有判据**——因此本方案的测试基线（确定性 **1653 passed / 119 skipped
> / 6 deselected / 2 failed / 1213 subtests**、全量 **1780**）与上述四批代码落地注记**均不变**。

> **★v8 第六批（代码，2026-09-23 同日）：T29 落地**——「归因六分类 + 文档缺陷回流」
> （`interfacetester_ai/diagnosis.py`；**文档级新增三条待办中的第①条，按上述建议顺序先落地**）。
> 落地内容与**验收四元组**见 §十 **T29 ✅** 行；判据与口径边界（归一化只做最小可判子集，完整管道归 T7）
> 见 §5.3 落地注记；**扩白名单的登记动作**（第四个写入者 `diagnosis` → 只写 `reports/`）见 §6 / §2.2 / §9.2 R15。
> **本轮唯一一次"扩白名单"被机器抓住过**：未登记的 `diagnosis.py` 被 T18 扫描器判 **2 处违规**
> （`makedirs` + `open`，"未登记模块出现写入类调用（白名单默认禁止）"）→ 显式登记进
> `MODULE_WRITE_BOUNDARIES` 后才 0 违规——这正是 T18 想要的形态（"谁能写盘"必须签字）。
>
> **数字联动（实测）**：+18 用例（`diagnosis` 17 条 + 写盘边界注册表对账 1 条）、+12 subtests。
> ① 全套 `pytest tests --ignore=examples -q` → **`1676 passed / 119 skipped / 3 failed / 1225 subtests`**
> （105.29s，README 修复**前**口径；3 failed = README 总数项〔+18 使过期差额放大，已按测试指引修复〕
> + 2 环境遗留）；② 确定性口径（6 deselected，README 修复**后**复跑）→
> **`1671 passed / 119 skipped / 6 deselected / 2 failed / 1225 subtests`（84.40s，仅剩 2 环境遗留）**；
> ③ `--collect-only` 对账 **1798**（= 1671 + 2 + 119 + 6 ✓）；④ README「六」与 deploy.md 已同源更新
> （1653→**1671** / 1780→**1798** / 1213→**1225**）；⑤ `docs_consistency_test` 复跑 **17 passed + 1 skipped
> + 1 failed**（唯一 failed 仍是 0923 文档的 8 条 `.venv` 死链环境遗留）。
> **仍未落地**：**T27**（文档体检闸 S-10）与 **T28**（引用-断言对账，等 P0a）——它们的"未落地"标注
> **保持不动**（不虚标，R14 纪律）。
>
> **★过程诚实记录（口径纪律，值得留档）**：本轮复跑确定性口径时曾拿到一次
> **`1665 passed / 124 skipped`** 的**污染读数**——原因是我在 pytest 运行**同时并行跑了另一条
> pytest**（`docs_consistency_test`），工作区资源竞争使 5 个用例转为 skip（passed −5、skipped +5，
> 总数 1798 仍自洽，**只有细看 skip 数才看得出异常**）。**安静重跑后为 1671 / 119 / 2 failed**。
> 教训与 §12.8-① 同源：**基线数字必须在无并发、无干扰下取证**——否则"同一条命令两个数"
> 会重演 v7"两边测的根本不是同一个环境"那类误判。

> **★v8 第七批（代码，2026-09-23 同日）：T27 落地**——「生成前文档体检闸」（**§8.1 阶段行 S-10**，
> `interfacetester_ai/doc_quality.py`；**文档级三条待办中的第②条**，按上述建议顺序落地）。
> 落地内容与**验收四元组**见 §十 **T27 ✅** 行；**口径修正**（D1 必需项**按整篇判**，
> 相对原行的"逐切片"）见 §5.1 落地注记与该行——判据未变，只是判的**粒度**落到文档级
> （切片是 LLM 输入单位，"能不能生成"是文档级问题；按切片判会把"`## 请求` 写 URL、
> `## 字段` 放表"这种常见形态判成"缺字段表"= **误伤**）。
> 写盘边界（**第五个登记写入者** `doc_quality` → 只许 `reports/`）见 §6 / §2.2。
>
> **本轮 T18 抓出的是"更细一层"的东西**：未登记的 `doc_quality.py` 被扫出 **3 处**写盘点，
> 其中 **1 处是模块自检里的夹具写盘**（`open(empty, "wb")` 造 0 字节样例）。
> 处置**不是**"把它也登记"，而是**把夹具写盘移进 `tests/`（用临时目录）**——
> 生产模块里只该留**它自己的**写盘点（红线③ 管的不只是"哪个模块能写"，也管"它写的是不是它该写的"）。
>
> **数字联动（实测）**：+21 用例（`tests/doc_quality_test.py`，**自带独立夹具**、不复用模块自检夹具——
> 共用夹具会让"夹具本身写错"永远发现不了）、+5 subtests。
> ① 全套 `pytest tests --ignore=examples -q` → **`1697 passed / 119 skipped / 3 failed / 1230 subtests`**
> （103.64s，README 修复**前**；3 failed = README 总数项 + 2 环境遗留）；② 确定性口径（README 修复后）
> → **`1692 passed / 119 skipped / 6 deselected / 2 failed / 1230 subtests`（89.28s，仅剩 2 环境遗留）**；
> ③ `--collect-only` 对账 **1819**（= 1692 + 2 + 119 + 6 ✓，**无并发**下取证）；
> ④ README「六」与 deploy.md 同源更新（1671→**1692** / 1798→**1819** / 1225→**1230**）。
> **仍未落地**：**T28**（引用-断言确定性对账，等 P0a 的装配器）——其"未落地"标注保持不动；
> T27 的"**接进生成流程**（不过体检不许 `gen`）"同样属 P0a。

> **★v8 第八批（代码，2026-09-24）：S-7 CI 门禁 + S-8/T13 `formatting` 字段**——
> 两条的共同点是"**把已经存在的东西变可见**"：前者让护栏进流水线，后者让格式化结论进报告。
> 实现与验收见 §8.1 的 `S-7` / `S-8` ✅ 行、§十 `T13` ✅ 行与 §3.4 的落地注记。
>
> **本批抓出两处"已存在、但没人看得见"**：
> 1. **CI 里没有护栏**：改动前 `.github/workflows/test.yml` 与 `.gitlab-ci.yml` 中
>    七个自检脚本名**零命中**——CI 只跑 `pytest`。而 pytest 覆盖的是**判据行为**，
>    不是"**脚本自身还能跑通**"：有人改了闸门 / 扫描器 / 体检闸，只要 pytest 的断言还过，
>    CI 照旧绿。这与 §0.5-④ 抓的"判据表写了分支、代码无此分支"是**同一族错误在另一层的复现**
>    （一个是判据层，一个是流水线层）。
> 2. **`formatting` 在报告里不存在**：`format_pytest_with_black` 的"只告警"是**有意设计**
>    （不阻断 hmake，§3.4），但代价是"L2 通过"这个信号**含噪**——而报告里既没有 `ran`
>    也没有 `reason`，噪声既无法被看见、也无法被统计。
>
> **落地时的硬约束（值得留档）**：`make.run_black` 是**写盘**操作，所以探针**不能**塞进闸门
> （那会破坏 S-6 的"闸门零副作用"口径）。最终设计是"**探针由调用方显式调用、报告只携带结论**"，
> 并把 `not_probed`（没人问过）与 `missing_black`（问过、但没装）**分成两个值**——
> 一个字段若不能指向动作，它就只是装饰（本仓对这类"字段有了、信息没了"的容忍度为零）。
>
> **数字联动（实测）**：+19 用例（`tests/l2_formatting_test.py`，含**真跑 black** 与
> **注入式元护栏**）、+31 subtests。① `--collect-only` → **1838**（= 1711 + 2 + 119 + 6 ✓）；
> ② 更新 README **前**实测 **1710 passed / 3 failed**——多出的那 1 条红正是
> `docs_consistency_test` 的"记录总数 vs 实际收集数"，**基线联动护栏按设计报警**；
> ③ 更新 README / deploy 后为 **1711 passed / 119 skipped / 6 deselected / 2 failed /
> 1261 subtests**（1692→**1711** / 1819→**1838** / 1230→**1261**）；
> ④ `docs/ci/README.md` 同步为**五个 job** + 「验证清单」第 11 条。
>
> **仍未落地（不虚标）**：`S-4`（接入 gen）与 `S-5`（接入 fix）都等 P0a；**`T28`**
> 同样等 P0a；`T14`（R11 语义正确率抽检）需要**人工判断**，不属代码可完成项。

> **★v8 第九批（代码，2026-09-24 同日）：T7 归一化管道 + T8 作业隔离**——
> 按校正清单 §8 的**建议顺序第②块**（"生成闭环主线"，依赖已解除）落地。
> 实现与验收见 §十 `T7` / `T8` ✅ 行、§9.x 的 P-3 / P-4 落地注记。
>
> **本批的共同点：给"看不见的东西"补上可判定的形态**
> 1. **T7（归一化管道）**：引用校验的判据是"**归一化后逐字子串**"，而"归一化"此前只是
>    `diagnosis` 里的一段**最小实现**——真噪声（ANSI / CRLF / JSON 转义 / 全角）会让
>    **真证据被判成"没证据"**，而这个症状与"模型确实没找到证据"**长得一模一样**。
>    现已独立成模块（`normalize.py`，含**偏移映射**），并把判据钉成**成对**：
>    4 组样本必须合并、4 条反例必须**不**合并（**归一化越强，编造的引用越容易命中**）。
> 2. **T8（作业隔离）**：L2 会写盘，"不污染仓库"此前只是一句承诺。现在
>    `run_isolated()` 在作业前后各取一次**源代码树快照**，有差异就抛
>    `WorkspaceContaminated` 并**点名路径**；清理失败报**残留路径**；作业**串行**。
>
> **本批把三处"已发生的教训"真正复用（不是口号）**：
> ① **写盘登记第三次咬合**：未登记的 `workdir.py` 被 T18 判 **3 处违规**，显式登记后 **0**；
> ② **夹具写盘的处置先例**（T27）：`workdir` 的自检因此**刻意不碰文件系统**——
>    真实文件系统判据全部落在 `tests/workdir_test.py`，生产模块只留自己的写盘点；
> ③ **新形态纪律**：写盘点必须**内联成 `.ai` 字面量开头**的 `os.path.join(...)`，
>    否则判"**目标不可静态判定**"（与越界同级）——这条是被扫描器**逼出来**的。
>
> **本批自检抓出的真 bug（留档）**：`job_id` 最初用 `time_ns() % 1_000_000` 做后缀，
> 自检**连发 200 次实测会撞**；改为进程内单调计数器（同时满足"唯一"与"字典序 = 生成序"）。
>
> **数字联动（实测）**：+26 用例、+36 subtests；`--collect-only` → **1864**；
> 确定性口径 → **1737 passed / 119 skipped / 6 deselected / 2 failed / 1297 subtests**
> （89.63s；2 failed 仅 §5 的两项环境遗留）；README / deploy 同源更新
> （1711→**1737** / 1838→**1864** / 1261→**1297**）；**CI 门禁同步扩容**：
> `guardrails` job 由**八条 → 十条**——新护栏**当天**进 CI，否则它从第一天起就"只在本地绿"，
> 等于把 S-7 刚堵上的洞重新挖开（这条已写进 `docs/ci/README.md` 的扩容纪律）。
>
> **仍未落地（不虚标）**：`T9`（golden 3 → 15 份）与 `T10`（闸门误伤率统计，依赖 T9）——
> **本批只做了这把建议序列的前两条**；其余仍是 `T28` / `S-4` / `S-5`（等 P0a）、
> `T14`（需人工抽检）与 §2~§4 的条目。

> **★v8 第十批（代码，2026-09-24 同日）：T9 golden 3→15 + T10 闸门误伤率审计**——
> 按校正清单 §8 的**建议顺序第②块**的后两条落地，至此**该序列四条全部完成**。
> 实现与验收见 §十 `T9` / `T10` ✅ 行、§3.5 的落地注记。
>
> **T9（样本扩容）**：`bench/golden/` **3 → 15 组**，覆盖 P0a 验收① 点名的六类
> （单步 / 多步依赖 / 含 schema / 含 XML / 含业务错误码 / 编码对抗）。
> 两点值得单列：① **编码对抗是"真编码"**——`legacy_gbk_doc.md` 按 GBK 保存
> （实测按 UTF-8 读取**必失败**）、`utf8_bom_doc.md` 带 UTF-8 BOM
> （实测 `utf-8-sig` 读时 BOM 被剥离、**不进正文**）；② **多步依赖样本**带
> `extract` + 下游 `Bearer $token`——它同时压着 T21 的 S4 词边界判据。
>
> **T10（误伤率审计）**：`bench/gate_false_positive_audit.py` 把**闸门**挂到 15 份真实样本上。
> **实测：误伤 0 / 误伤率 0.0% / 带 PENDING 样本 0**。
> ★这条数字顺手**关掉了一处长期挂着的"未实测"**：T21 加严 S4 判据时，
> 文档写"能消掉多少 PENDING 噪声**未实测**，需 golden 扩到 15 份后统计（T10）"——
> 答案就是 **0 条 PENDING**（§3.3 / §13 的相关行已回填，R14 纪律：不再让"未实测"悬着）。
>
> **本批的一个取舍（诚实留档）**：golden 定为 **15 组**而不是"能写多少写多少"——
> 因为 §3.5 的纪律与 P0a 验收① 都以 **15** 为口径（`GOLDEN_TOTAL` 是**唯一事实来源**，
> 写多了会让那个数字失去意义）。原先规划里有一份"响应头断言"样本（`rate_limit_quota`）
> 与 `get_user_profile` 覆盖面重叠，已把响应头断言**并入**后者、删掉前者，
> 从 16 组收敛回 **15 组**。
>
> **数字联动（实测）**：+5 用例（`tests/gate_false_positive_audit_test.py`）；
> `--collect-only` → **1869**；确定性口径 → **1742 passed / 119 skipped / 6 deselected /
> 2 failed / 1297 subtests**；README / deploy 同源更新（1737→**1742** / 1864→**1869**）；
> **CI 门禁再扩容**：`guardrails` job 由**十条 → 十一条**（误伤率审计同样当天进 CI）。
>
> **仍未落地（不虚标）**：`T28` / `S-4` / `S-5`（等 P0a）、`T14`（需人工抽检），
> 以及 §2~§4 里与本序列无关的条目（`T4`/`T5`/`T12`/`T15`/`T16`/`T22`/`T23`/`T24`）。

> **★v8 第十一批（文档 + 代码，2026-09-24 同日）：§3 口径与文档收尾**——
> 校正清单 §3 的一批零依赖项：**T4 / T5 / T12 / T15 / T16 / T22 / T23** 落地，
> **T14 / T24 明确标为"等 P0a"**（各自行里写清了"为什么现在做不了 + 已就绪什么 + 落地形态"）；
> 外加独立评审的 **P1 / P3 / P4 / P5** 四项文档级修正。
>
> **本批的共同点：把只活在文字里的口径变成可执行的东西**
> 1. **T4（输入预算）**：`INPUT_BUDGET_BYTES = 12000` 此前**只在文档里**（§5.1 与 §9.x P-5 各写一遍）——
>    现为 `prompts.py` 的代码常量 + `measure_bytes`（**UTF-8 字节**）/ `check_input_budget` /
>    `build_prompt_payload`（校验落在**拼完之后**）+ `InputBudgetExceeded`（带 `overflow`，即"记录截断量"）；
>    **模块里刻意没有截断入口**（"不允许静默截断"的机器形态）。
> 2. **T5（缓存键）**：`CACHE_KEY_FIELDS`（9 字段显式清单）+ `build_cache_key`（sha256 of 规范 JSON）——
>    **判据比原文更严**：原文防"漏字段"，实现把"**多字段**"也做成一类错误（加了采样参数却没同步清单时，
>    那一项改了**不会 miss**，而且"不报错但也不生效"，比漏字段更难发现）。
> 3. **T15（口径唯一化）**：给"旧数字只能作为历史对照"配上**机器判据**（`docs_consistency_test` 新增一类）——
>    判据成对：旧数字必须带历史标记 **+** 这些数字**仍在**文档里（防"全删了所以绿"）**+** 注入句元护栏。
> 4. **T23（数字带口径）**：三处算子漂移修掉（`README` 的"第 19 个算子"、`deploy` 的"21 个断言算子"与"第 19 个算子"）。
>    **不改成"第 22 个"**（实测）：`jsonschema_match` 按常量顺序是**第 7 个**（总数 22）——
>    "第 N 个"本身就是漂移源，故直接改用**算子名**。
> 5. **T12（真仓 `.gitignore`）**：补 `.ai/cache|work|jobs/` 与 `.tmp_golden/`，并写出 **P-7 的"产物/证据"分界**
>    （`.ai/manifest/`、`.ai/reviews/` 是**证据**、**刻意不忽略**）；顺带发现 `.tmp_audit2/` 未被忽略
>    （既有规则 `.tmp_audit/` 是**精确名**、匹配不到它）→ 单列一行，**只忽略、不删除**。
>
> **独立评审的四项（P1 / P3 / P4 / P5）**：
> - **P1**：§12.7 的乙⑤⑧ 两条**没有命令输出**，却被原声明的"没有一条是靠读文档得出的"覆盖 →
>   已在这两条上加 ⚠ **分析推演** 标注，并把声明改写为"**10 条有输出、2 条是推演**"；
> - **P3**（头部"已安装的 5.0.1 wheel 环境"措辞）：v8 早期已改为 **editable 安装** ✓ 本轮核对确认；
> - **P4**：§5.6 的"**与 status 相关的头**"不是标准 HTTP 类别、无法直接转成代码 →
>   改为可执行表述 `RESPONSE_HEADER_KEEP ∪ {n | "status" in n.lower()}`，`projection.py` 的 docstring 同步；
>   并把 **chunked 响应下 `Content-Length` 不稳**这条边界**显式登记**（保留它的理由：它是"响应体长度"这个
>   **结论相关**信号，剔掉会让"响应体被截断"不可见）；
> - **P5**：附录 B 补**两仓一致性校验命令**（逐字节比对 `D:\learn` 与 `D:\tester` 的内核源码树）——
>   它其实是附录 B **自己的前提**：前提不可验证时，"改哪个仓"就成了一句无据的指引。
>
> **数字联动（实测）**：+22 用例（`tests/ai_budget_cache_test.py` 19 + `docs_consistency_test` 新类 3）；
> `--collect-only` → **1891**；确定性口径 → **1764 passed / 119 skipped / 6 deselected / 2 failed / 1328 subtests**；
> README / deploy 同源更新（1742→**1764** / 1869→**1891** / 1297→**1328**）。
> **★subtests 的口径注意**：本条最初记 **1327**，终验时实测 **1328**——差的 1 个来自
> `docs_consistency_test::test_recorded_total_matches_what_pytest_actually_collects` **由红转绿**：
> 它内部用 `subTest` 逐项列证据，**失败时不计入、通过时才计入**。
> 所以"刷新基线数字"这件事必须**在文档改完之后再跑一次**，否则会差在 subtests 这种细项上。
>
> **仍未落地（不虚标）**：`T14` / `T24`（**等 P0a**）、`T28` / `S-4` / `S-5`（等 P0a），以及 §4 的其余条目。

---

### 12.8-⑥ ★第十二批（2026-09-25）：P3c 的「离线 wheel」实测四元组

> **为什么这一条要单列**：P3c 一直挂着"**打轮没有实测，不虚标**"，理由是"本机没有
> `build`/`wheel`/`setuptools`"。本批发现**那句话只说对了一半**：`.venv` 里确实没有，
> 但**基础解释器**（`sys.base_prefix` = `C:\python312`）**有**——所以"打不出来"
> 其实只是"没换解释器"。下面每一步都是实测（命令 / 环境 / 输出 / 退出码）。

```powershell
# ① 离线打轮（用**基础解释器**：它带 setuptools；--no-index 保证不碰网络）
& 'C:\python312\python.exe' -m pip wheel --no-build-isolation --no-deps --no-index `
    -w D:\tester\.tmp_wheel_probe D:\tester\interfacetester_ai
#  → Created wheel for interfacetester-ai: interfacetester_ai-0.1.0-py3-none-any.whl
#    size=298009 sha256=2995e66e…    exitcode = 0
#  ★坑（实测）：直接在包目录里打会留下 `build/` 与 `interfacetester_ai.egg-info/`
#    —— 那是"改了源码树"，所以测试里改成**先在临时副本上打**（形态纪律①）。

# ② 装进**全新的空 venv**（该 venv 里没有任何 build 工具），全程 --no-index
& 'D:\tester\.venv\Scripts\python.exe' -m venv D:\tester\.tmp_venv_probe
Set-Location C:\
& 'D:\tester\.tmp_venv_probe\Scripts\python.exe' -m pip install --no-index --no-deps `
    'D:\tester\.tmp_wheel_probe\interfacetester_ai-0.1.0-py3-none-any.whl' `
    'D:\tester\interfacetester-5.0.1-py3-none-any.whl'
#  → Successfully installed interfacetester-5.0.1 interfacetester-ai-0.1.0   exitcode = 0

# ③ 在**源码树之外**（C:\）验证"装上了"，而不是"cwd 蒙对了"
& 'D:\tester\.tmp_venv_probe\Scripts\python.exe' -c "import interfacetester_ai as a; print(a.__version__, a.__file__)"
#  → 0.1.0 D:\tester\.tmp_venv_probe\Lib\site-packages\interfacetester_ai\__init__.py   exitcode = 0
#  ★旁证：同一环境里 `import interfacetester`（内核）**会**因缺三方依赖（loguru 等）失败
#    —— 这正是"本包零依赖、内核只是**延迟**复用"这条纪律的实证，不是口号。

# ④ 离线起服务（工作目录是一个**空临时目录**，顺带验"零写盘"）
$p = Start-Process -FilePath 'D:\tester\.tmp_venv_probe\Scripts\python.exe' `
     -ArgumentList '-m','interfacetester_ai','serve','--port','8791' `
     -WorkingDirectory $wd -PassThru -WindowStyle Hidden
Invoke-WebRequest -Uri 'http://127.0.0.1:8791/' -UseBasicParsing        # → 200，3790 bytes
Invoke-WebRequest -Uri 'http://127.0.0.1:8791/audit' -UseBasicParsing  # → 200，2847 bytes
Stop-Process -Id $p.Id -Force
(Get-ChildItem $wd -Recurse -File | Measure-Object).Count              # → 0（零写盘）
```

> **本批落地的机器判据**（`tests/packaging_test.py`，取代了历史的
> `TestBuildToolingIsNotAvailableHere`——它断言"`dist/` 里不许有 wheel"，并留话
> "真打了轮就回来改它"；本批就是回来签字的）：
> - `TestOfflineWheelBuildsAndInstalls::test_offline_build_and_install_round_trip`
>   —— 自己找带 setuptools 的解释器 → **临时副本**打轮 → `--target` 装到源码树之外 →
>   模块集合与源码树相等 → `entry_points.txt` 里有 `interfacetester_ai.cli:main` →
>   **打轮不许在包目录里留 `build/`、`egg-info`**；
> - `TestBuiltWheelMatchesSourceTree`（5 条）—— 产物 ↔ 源码树：模块集合 / 零依赖 /
>   入口 / **RECORD 完整性（逐成员比对 sha256 与字节数）** / 无包外文件；
>   由 `INTERFACETESTER_AI_WHEEL_PATH` 驱动（不给就 skip，同内核 `wheel_content_test.py` 口径）。
>
> **CI**：`.github/workflows/test.yml` 与 `.gitlab-ci.yml` 的 `package` job 现在
> **同时**打两个 wheel（内核 + 旁路包）并各自校验产物内容。
> **数字联动（实测）**：+5 用例 → `--collect-only` **2316**；确定性口径
> **2184 passed / 124 skipped / 6 deselected**（1694 subtests）；README / deploy 同源更新
> （含"基线联动护栏"由红转绿）。



### 12.8-⑦ ★第十三批（2026-09-25）：度量页的「缓存命中率 / token 用量 / 花费估算」接上真实来源

> **起因**：P3a 的落地注记挂着"缓存命中率与花费估算**没有落盘来源**……要真有这两个数，
> 得先在 LLM 层落盘统计"。核对时发现**这句话不成立**：`.ai/manifest/` 里**每条留痕都带**
> `cached` / `attempts` / `usage` / `model`（`manifest.py` 本来就是**登记在册**的写入者）——
> 数据**已经落盘**，缺的只是"读出来"。所以本批**不新增任何写盘点**（红线③），只加只读函数。

**① 用本仓真实留痕实测（四元组）**

```powershell
.\.venv\Scripts\python.exe -c "from interfacetester_ai.web import read_manifest_stats as s; print(s())"
#  → files=133 readable=133 unreadable=0 cached=87
#    prompt_tokens=79276 completion_tokens=42708 usage_entries=46 without_usage=87
#    models=['SimonPu/Qwen3-Coder:30B-Instruct_Q4_K_XL', 'gemma4:latest']        exitcode=0
#  ★交叉自证：cached=87 与 without_usage=87 **恰好相等**——命中缓存那次不花 token，
#    本来就没有 `usage`。也就是说"缺 usage ≠ 缺证据"，两个数互相印证了口径。
```

度量页三行（同一环境实测）：

| 行 | 值 | 来源（页面上照实写） |
| --- | --- | --- |
| 缓存命中率 | **65.4%** | `.ai/manifest/*.json` 的 `cached` 字段（133 条可读留痕） |
| token 用量（留痕合计） | prompt 79276 / completion 42708 | `.ai/manifest/*.json` 的 `usage`（46 条） |
| 花费估算（未配单价） | 「未读到」 | `.ai/manifest/*.json` 的 `usage` × 两个环境变量（**点名**缺哪个） |
| 花费估算（配 `…PROMPT_PER_MTOK=2` / `…COMPLETION_PER_MTOK=8`） | **0.5002** | 同上；来源里带**当时用的单价** |

**② 四条口径，每条都对一个"看起来正常的假数字"**

| 口径 | 防的假数字 |
| --- | --- |
| 分母只用**可读**条数；坏文件单独计数并写进 `note` | 坏 JSON 混进分母 → 命中率被**静默压低**（1/2 变成 1/3） |
| `cached` 只认 `is True` | 字符串 `"true"` / 数字 `1` 被当命中 → 命中率**虚高** |
| token 只累加**真有 `usage`** 的条数，`without_usage` 单独说 | 缺 `usage` 按 0 算 → 一个**偏小的假花费** |
| 单价只从环境变量取（`INTERFACETESTER_AI_PRICE_{PROMPT,COMPLETION}_PER_MTOK`），没配就点名 | 代码里内置价格表 → 一张**会过期的假数字**（币种还未必对） |

**③ 机器判据**：`tests/web_readonly_test.py::TestMetricsSourcing` 新增 **7** 条——
注入 3 条留痕（2 命中）→ 必须 **66.7%** 且**数字真的上了度量页**；坏 JSON → **50.0%**（不是 33.3%）
且 `note` 里说出来；`cached: "true"` → **0.0%**；token 合计 + `without_usage` 计数必须分开；
没配单价 → 「未读到」+ 点名两个变量；**元护栏**：改单价 → 花费跟着变（13.0000 → 10.0000）；
缺 `usage` 时**即使配了单价**也不出数。**没有留痕时**仍然「未读到」+ `source=无数据源`（旧判据保留）。

> **数字联动（实测）**：+7 用例 → `--collect-only` **2323**；确定性口径
> **2191 passed / 124 skipped / 6 deselected**（1696 subtests）；README / deploy 同源更新。
> **仍未做（不虚标）**：度量页目前**按需实时汇总**（每次请求读一遍 `.ai/manifest/`）——
> 留痕上万条时该加缓存/增量索引，现在**没有**做（也没测过性能）。


### 12.8-⑧ ★第十四批（2026-09-25）：真机 `file://` 直开实测 —— 并因此补出「离线静态导出」

> **起因**：P3d 一直挂着"**真机 `file://` 直开未做**（当前只验了『页面自包含』这一必要条件）"。
> 本批把它跑掉，结论是**必要条件成立、充分条件不成立**——于是补了一个模块。

**① 实测（真浏览器 + `file://`，四元组）**

```powershell
# 把五个页面渲染到磁盘（取证探针；`web.py` 自己依旧零写盘）
.\.venv\Scripts\python.exe probe_p3d_export\probe_file_protocol.py
#  → pages = 5（index/metrics/audit/pending/jobs）；每页**绝对路径链接 6 个**（brand + 5 个导航）
#  ★留档：`probe_p3d_export/before_fix_site/`（修复前的 5 页原文）+ 三张真机截图
#    （`index_file_protocol.png` = 单页能读；`export_*_file_protocol.png` = 导出后逐页点得通）
```
真浏览器打开 `file:///D:/tester/.tmp_static_probe/site/index.html`：

| 检查 | 实测结果 |
| --- | --- |
| HTTP 状态 / 标题 | **200** / `运行列表` |
| charset / CSS | `UTF-8`；`h1` 计算字号 `20px`、颜色 `rgb(31,35,40)`（内联样式生效） |
| 内容 | 2 个 `<table>`、10 行、`缓存命中率`/`花费估算`/`未降级占比` 文案都在；`body` 1230 字符 |
| **导航解析后的 href** | `file:///D:/index.html`… 实为 **`file:///D:/metrics` / `file:///D:/audit` / `file:///D:/pending` / `file:///D:/jobs`** |
| **点「度量」** | **`chrome-error://chromewebdata/`**，标题 `file:///D:/metrics`，正文「无法访问您的文件 / **ERR_FILE_NOT_FOUND**」 |

> 根因一句话：导航是**绝对路径**（`href="/metrics"`），在服务端没问题，在 `file://` 下会被解析成
> **文件系统根**。所以"页面自包含"只能保证**单页能读**，**不能保证点得到**。

**② 处置：新增 `interfacetester_ai/export.py`（`haify export`）**

| 步骤 | 做法 |
| --- | --- |
| 渲染 | 复用看板的 `render_*`（**不另写一套**），落 `.ai/export/`（**登记写入根**，T18 静态可判） |
| 链接 | `href="/metrics"` → `href="metrics.html"`；`/run?path=…` → `run-<slug>.html`；`/fix?path=…` → `fix-<slug>.html`；`/jobs?job=…` → `job-<slug>.html` |
| 离线做不了的三类 | `/confirm`、`/file`（以及任何未知站内路由）→ `offline.html`，并把**被改写的路由**在页里**点名列出**（不假装能用） |
| 醒目 | **每页顶部挂横幅**："本页来自离线静态导出，生成时刻 X——它是**快照**，不会自动更新"（快照与活看板长得几乎一样，不说清就会被当成现状） |
| 文件名 | `_slug()` 末尾**必挂 sha1 摘要**：`a/b.json` 与 `a-b.json` 否则会撞同一个文件名（§3.3 G10 的同一形态） |

本仓真实导出（实测）：`.ai/export/` 下 **6 个页面**（index/metrics/audit/pending/jobs/offline）——
本工作区 `logs/` 无 `*.summary.json`，所以没有 `run-*`/`fix-*`，**如实少几个**而不是造假的空页。

**③ 真浏览器复验（同一台机、同一个浏览器）**

| 检查 | 实测结果 |
| --- | --- |
| 打开 `file:///D:/tester/.ai/export/index.html` | **200**，标题 `运行列表`，横幅在（文案含生成时刻） |
| 导航解析后的 href | **全部落在导出目录里**：`file:///D:/tester/.ai/export/index.html` / `metrics.html` / `audit.html` / `pending.html` / `jobs.html` |
| **点「度量」** | 真的打开了 **`file:///D:/tester/.ai/export/metrics.html`**（标题 `度量`、`h1=度量`，且页面里 **65.4%** 与 `.ai/manifest` 来源都在） |

**④ 判据（机器可判，且都在 CI 里）**

- **自检 6 条**（`python -m interfacetester_ai.export`）：零绝对路径 / 链接目标都在产物里 /
  **入口可达全部页** / **幂等**（`stamp` 可注入，产物可 diff）/ 只写 `.ai/export/` /
  **注入绝对链接与悬空链接必须被报出来**（元护栏）；**判据不过就抛 `OfflineBundleNotSafe`，一个字节都不落**。
- **测试 12 条**（`tests/export_test.py`）：上述各条 + 「导出内容清单」「`offline.html` 点名服务端能力」
  「快照横幅」「只写登记根」「`export` 在 T18 而在册 / `web` 始终不在册」+ CLI 一键导出退出码 0。
- **CI**：`guardrails` job 的自检从 **11 条 → 12 条**（两平台同步）。
- **T18 注册表 13 项**（新增 `export` 只许写 `.ai/`）。

**⑤ 本批被自己的护栏抓住的两件事（留档）**

1. **T18 又一次咬合**：`export.py` 自检最初把夹具写在 `logs/` → 被判 **2 处违规**（`makedirs` + `open`）。
   处置不是"把 `logs/` 登记进去"（那等于给导出开一个写 `logs/` 的口子），而是**把夹具挪进自己的根**
   （`.ai/export/_selftest/logs/`）——与 P3a 把夹具从 `web.py` 挪进 `tests/` 同源。
2. **幂等判据抓到我自己的漏参**：复跑那句 `export_site(stamp=tick)` 忘了传 `roots` → 第二次用默认读根，
   `index/metrics` 内容就不同了。**"产物可 diff"因此不只是洁癖——它是"两次导出的输入是否一致"的探测器。**

> **数字联动（实测）**：+12 用例 → `--collect-only` **2335**；确定性口径
> **2191+12 = 2203 passed / 124 skipped / 6 deselected**（subtests 见 README 当前基线行）；
> README / deploy 同源更新。**仍未做（不虚标）**：真模型端到端基线数值(★2026-09-25 已做：见 §12.8-⑩)；
> **真实仓（`D:\learn\interface-tester`）的 `.gitignore` 还需补 `.ai/export/`**（本工作区非 git 仓，改不了那一边）。


### 12.8-⑨ ★第十五批（2026-09-25）：P2b 的 L3 沙盒收口（口径由一次明确决策钉住）

> **起因**：P2b 一直挂着"**仍未做：L3 沙盒**（§5.9 的『（沙盒开关下）L3』）——本命令只做本机干跑"。

**① 口径（决策记录，口径变了必须同时改代码 / 判据 / 文档三处）**

> **允许执行 · 只跑已知答案测试 · 允许真实加密运算。**

翻译成代码三条：**(a)** 沙盒里**只**做一件事——`exec` 草稿源码 + 拿已知答案测试比期望输出
（"能跑什么"是**枚举出来的**，不是"打开一个 shell"）；**(b)** `hashlib` / `hmac` / `base64` /
`binascii` / `struct` / `zlib` / `json` / `re` / `math` / `time` … 这些**纯计算**模块照常可用
（签名函数本来就要用它们）；**(c)** 拦的是**误伤**——出网 / 碰文件 / 起进程 / 读环境。

**② 实现（两个功能点 + 一条闸门）**

| 位置 | 做了什么 |
| --- | --- |
| `l3.py` | 新增 `check_sandbox()` / `require_sandbox()`：**执行型 L3 的开关判据**（不涉及主机名）。★开关的读法与真值表**收在一处**，`check_l3_allowed` 也调它——避免"同一口径散在两处"的经典漂移 |
| `debugtalk_draft.py`（子进程） | `--kat-worker`：载荷走 **stdin**（装护栏后连 `open` 都没有，所以输入必须在装护栏前读完）→ 装护栏（`meta_path` 守卫 + 换掉 `__import__`/`open`/`eval`/`exec` + **清空 `sys.modules` 里的危险模块**）→ `exec` 草稿 → 跑 KAT → 以哨兵行 `@@KAT_REPORT@@` 回报 JSON（草稿里的 `print` 被收进 buffer，**不污染协议**） |
| `debugtalk_draft.py`（父进程） | `run_kat_in_sandbox()`：**闸门在前**（沙盒没开 → `L3Refused`，**一个子进程都不起**）→ `sys.executable -m …` 子进程（`cwd` = 空临时目录、`env` 白名单、超时 `20s` 可杀）→ 解析回报；拿不到回报（崩了/被杀/协议污染）**判失败**，绝不当成"跑过了" |
| `cli.py` | `haify debugtalk --sandbox`；沙盒没开 → **退出码 2**（与 `haify run` 的闸门拒绝同款，因为"要求了一种没被允许的执行方式"与"正常拒产"的下一步动作完全不同） |
| 报告 | 两种模式**必须写进报告**（`本次「已知答案测试」跑在：**沙盒（子进程 + 只跑已知答案测试）**（子进程护栏：…）`）——"验过了"要说是**在哪验的** |

**③ 实测（四元组）**

```powershell
.\.venv\Scripts\python.exe -m interfacetester_ai.debugtalk_draft   # 自检（含沙盒 5 条） → exitcode 0
.\.venv\Scripts\python.exe -m interfacetester_ai.l3                # 闸门自检                 → exitcode 0
& '.venv\Scripts\python.exe' -m pytest tests/debugtalk_draft_test.py -q
#  → 41 passed / 12 subtests（新增 11 条：TestL3Sandbox 9 + TestCliSandboxFlag 2）
```
| 场景 | 实测结果 |
| --- | --- |
| `md5("abc")` + `HMAC-SHA256` + `base64` 的真加密 KAT | **通过**（`900150983cd24fb0d6963f7d28e17f72` / `mUba…ig4=`）——**用户口径的正面样本** |
| 沙盒未开 | `L3Refused`，拒因点名 `INTERFACETESTER_AI_SANDBOX`；**且 `subprocess.run` 一次都没被调用**（元护栏） |
| `import socket` / `import importlib` / `open("x.txt")` | **全部被拦**（拒因写明"本沙盒只跑已知答案测试"） |
| 函数里 `time.sleep(30)` | **超时 1s 被终止**，失败清单如实写"**没有结论**" |
| 期望值写错（`0`×32） | **仍然判不过**（元护栏：沙盒只证明"跑了"，不证明"对了"） |
| 父进程 env 里放 `INTERFACETESTER_AI_API_KEY` | 子进程 `env_keys` 里**没有它**（凭据不进子进程） |
| `debugtalk --sandbox`（沙盒关 / 开） | **退出码 2**（stderr 给开启指引）/ **退出码 0**（stdout 写明"沙盒"） |

**④ 本批被自己的护栏抓到的两个真 bug（留档）**

1. **"禁止 `exec`" 连带把 `import` 也禁了**：`importlib._bootstrap` 的 `_call_with_frames_removed`
   里那句 `exec(code, module.__dict__)` 走的正是 `builtins.exec` —— 于是真加密运算的
   第一条 KAT 直接 `PermissionError`。修法：**导入期间把 `exec` 临时让开**（窗口只在
   `__import__` 内部，草稿自己调 `exec` 仍被拦）。
2. **让开窗口被嵌套 import 提前关掉**：`hashlib` 自己还会 import 别的模块，简单
   `try/finally` 会让**内层的 finally 关掉外层的窗口**；靠临时把调用方帧打出来
   （`<frozen importlib._bootstrap>:_call_with_frames_removed`）才看清。修法：**深度计数**，
   只有最外层收尾时才重新上锁。

**⑤ 诚实边界（写进代码也写进本文）**

- 子进程与父进程**同一用户权限**，所以它**不是**对抗恶意代码的牢笼——它挡的是**误伤**
  （草稿里一句 `requests.post(...)` 打真实环境、一句 `open("~/.ssh/id_rsa")` 读凭据、
  一句死循环挂住终端）。
- **`exec`/`eval` 被拦**，因此**运行时生成代码**的写法（`@dataclass`、`collections.namedtuple`
  之类）在沙盒里会**响亮失败**——这是取舍（拦自我解锁 vs 兼容性），不是遗漏；失败是响亮的，
  不会静默变成"通过"。

> **数字联动（实测）**：+11 用例 → `--collect-only` **2346**；确定性口径
> **2214 passed / 124 skipped / 6 deselected**（**1729** subtests——★注意这一位与"护栏红/绿"联动：
> 基线联动护栏自身用 `subTest` 列证据，**红时不计、绿时才算**，所以"刷新基线数字"必须在文档改完后再跑一次）；
> README / deploy 同源更新。
> **仍未做（不虚标）**：真模型端到端基线数值(★2026-09-25 已做：见 §12.8-⑩)。


### 12.8-⑩ ★第十六批（2026-09-25）：P0a 验收 ①/⑦ 的真模型实测（**并修正验收①的口径**）

> **起因**：验收① 写的是"golden 集 15 份 ≤2 轮后 **L2 通过率 ≥ 实测基线**"，而 §12.6 一直挂着
> "§8.1 中各阶段的『通过率 ≥ 实测基线』：**阈值尚未测出**，须先测后承诺"。本批把它测了——
> 顺带发现**这条判据本身要改口径**（见 ④，比数字更重要）。

**① 环境与命令（四元组）**

```powershell
# Ollama 在跑（127.0.0.1:11434，4 个模型）。先探测时延：gemma4:latest 8.1s / gemma4:12b 9.9s /
#   qwen3.8:27b 28.7s / Qwen3-Coder:30B 61.0s → 取最快且输出正确的 gemma4:latest
$env:INTERFACETESTER_AI_BASE_URL = "http://127.0.0.1:11434/v1"   # ★回环 → **免 API_KEY**
$env:INTERFACETESTER_AI_MODEL    = "gemma4:latest"
$env:INTERFACETESTER_AI_SANDBOX  = "on"                          # L3 闸门：真发请求必须显式开
$env:INTERFACETESTER_AI_TIMEOUT  = "300"; $env:INTERFACETESTER_AI_RETRIES = "1"
.\.venv\Scripts\python.exe probe_p0a_llm\measure_p0a_l2.py       # → exitcode 0；总耗时 285s
```
L2 的尺子**复用仓库自己的** `bench/golden_harness.evaluate_expected_yaml`（= 内核
`validate_emitted_case`：`load_testcase_file` + `make_testcase` 干跑 + `compile()`）——
所以"模型产物"和"人工 golden 基线"是**同一把尺子**，不是另立一套。

**② 结果（15 份 golden 文档 → 真模型 `gen`，修正环 ≤2 轮）**

| 指标 | 实测 |
| --- | --- |
| 有产出 | **11/15 篇**（共 **13 条**用例） |
| 片层面 | 70 片 = **15 片有接口形态** + 55 片非接口（按设计跳过，不算失败） |
| **接口片 → 产出用例** | **13/15 = 86.7%**（2 片失败） |
| L2（对最终产物） | 13/13 = **100%** ← ★**见 ④：这是重言式** |
| 被 **S-10 文档体检闸**拦下 | **2 篇**（`delete_session` / `soap_user_service`，`exit=4`，**0.2s、一次模型都没调**） |
| 模型阶段失败 | **2 篇**，且**都是编码对抗文档**（GBK `legacy_gbk_doc` / BOM `utf8_bom_doc`） |
| 模型调用留痕 | **19 条** `.ai/manifest/`（可逐条复盘） |
| 总耗时 | **285s**（平均 19s/篇；单篇 11.9~69.7s） |
| **源码树被动过** | **（没有）** ← 验收⑦，见 ⑤ |

**③ 那 2 篇"失败"不是坏了，是**闸门按设计拦下的**（`NEEDS_HUMAN.md` 原文摘录）**

- 两篇都被 **S3（断言真空）** 拒：
  `请求步骤 '…' 没有任何断言，也没有 extract —— 这类步骤无论接口返回什么都会被判成功
  （源码注释称其为「最坏的一种假通过」）`＋`整份用例零断言：所有请求步骤都没有 validate。
  它会「一路绿到底」，但什么都没测。`
- BOM 那篇还触发 **T28 的 `quote-miss`**：模型引用了一句**文档里并不存在**的话 →
  **降级进 `unknowns`**（不静默保留）→ 该用例断言数从 1 变 0 → S3 再兜住。
- 换句话说：**这两条"失败"恰恰是"零断言用例没能混进 `cases/`"的证据**。
- （两张编码文档确实被**正确读进来**了：GBK/BOM 探测成功、切片成功、模型真被调了 3 次；
  失败发生在"模型给出了没有断言的草稿"这一步。）→ 编码探测这条链**是通的**，卡的是**模型能力**。

**④ ★口径修正：验收① 的"L2 通过率"是重言式，分母必须换**

`cases/` 里的产物**已经**过了装配器的写盘前置闸门（S-4「**闸门在前**」）——**没过闸的根本不会落盘**。
于是"对最终产物量 L2"**恒为 100%**：它只能证明"落盘的产物是可用的"，
**不能**证明"模型有多能过闸"。而**一条永远为真的判据**，正是 §一 纪律二与 R14 点名的那种
"保真度幻觉"——它会连"去验证的动机"一起消掉。

**改成可证伪的两条**（本批已按 ② 给出实测值）：

1. **接口片 → 产出用例 ≥ 86.7%**（本次实测基线；分母＝送进模型的**有接口形态**的片）；
2. **落盘产物 100% 过 L2 / 内核加载**（保留，但**必须写明它是"闸门在前"的必然结果**，
   不是模型的成绩单）。

这同时正面回答了 §十一 **D4「成功标准尚不可证伪」**：可证伪的口径是"**片产出率**"，
而不是"产物 L2 通过率"。**该口径变更已于 2026-09-26 由 D4 决策人确认**（`docs/未做完的待决策项.md` §一 D4 / §二 R11-2），
并配套产出 **`docs/模型基线测量方案.md`**（模型清单 / 样本集与分母口径 / 指标定义 / 阈值 / 复跑命令 / 测量纪律）。

> **★C-2 立项（2026-09-26，D4 决策人）**：本批登记的"模型只产协议级断言"这个根因（§12.8-⑪ ④）
> **已立项修复、并已接线通过全量基线**——在生成闭环里加「**装配前跑一遍确定性映射并合并**」：**只补不覆盖**（模型已断言的字段保留模型的，
> 冲突进 `unknowns`）、**多步用例不合并**、**只"补全"不"救命"**（模型零可用断言时不合并——那是"模型没干活"的质量信号）、
> **默认开**（`--no-merge-assertions` / `INTERFACETESTER_AI_MERGE_ASSERTIONS=off` 两条回滚）；
> 合并点 = `gen.py::generate_case()` 的 `parse_draft` 之后、`assemble()` 之前（两条通路仍共用同一套下游）。
> 口径/判据/工作量见决策清单 §三 **C-2**；**落地后必须重跑本节的 15 篇真模型并重出表 A**（R11-1 阈值首次可测），
> 且**产出率基线可能下降**（"宁缺勿编"的代价，不是回归）。

**⑤ 验收⑦（L2 零污染）**

把 13 条产物复制到 `.tmp_p0a_l2/` 里跑 L2（**同 golden harness 口径**）：

- 每条产物的 `*_test.py`（black 格式化过）落在**该用例自己的目录里**；
- 全程**源码树快照零变化**（`_snapshot()` 前后 diff 为空）→ "L2 的副作用只在产物所在目录"。

**留档**：`probe_p0a_llm/out_l2_baseline_<模型 slug>.json`（逐篇数字，本轮 = `..._gemma4-latest.json`）、
`probe_p0a_llm/gen_all_<模型 slug>.log`（每篇 stdout 原文，本轮 = `..._gemma4-latest.log`）、
`.tmp_p0a_run_<slug>/`（15 个隔离工作区：19 条留痕 + 13 条产物 + 2 份 `NEEDS_HUMAN.md`）、
`.tmp_p0a_l2_<slug>/`（L2 现场）。★文件名带**模型 slug** 是刻意的：换模型做对照时**不覆盖**上一轮取证。

> **仍未做（不虚标）**：① **单模型单次**（`gemma4:latest`）——换模型/换温度是否可复现**未测**；
> ② 那 55 片"非接口"是按设计跳过，**没有人工抽检**其判定都对；
> ③ `unknowns` 的人工抽检（P0b 验收）仍未做；④ 这 13 条产物的**断言语义正确率**（R11）**未人工抽检**。


### 12.8-⑪ ★第十七批（2026-09-25）：多模型对照的**现实约束** + 三张人工抽检表

> **起因**：要回答"那个 86.7% 是不是模型相关"（消掉 §12.8-⑩ 的"单模型单次"限制），
> 并启动三条**只能由人判**的抽检。

**① 多模型时延实测（本机 Ollama，同一个 `127.0.0.1:11434`）**

| 模型 | 最小调用（16 tokens） | 真实 `gen`（同一篇 `login.md`） | 可用性结论 |
| --- | --- | --- | --- |
| `gemma4:latest` | 8.1s | **14.4s** | ✅ 15 篇全跑完 = **285s** |
| `gemma4:12b` | 9.9s（**正文为空**，16 tokens 全用在推理上） | **>4 分钟**（14 分钟只推进 1 篇） | ❌ 太慢：15 篇预计 1~2 小时 |
| `qwen3.8:27b` | 28.7s（正文同样为空） | **>10 分钟**（第 1 片连续两次撞上 600s 超时） | ❌ 本机不可行 |
| `SimonPu/Qwen3-Coder:30B-Instruct_Q4_K_XL` | 61.0s | （未测） | ❌ 只做时延参考 |

> **★可执行的工程结论（比这张表本身有用）**：**`llm.DEFAULT_TIMEOUT = 60s` 对本地大模型不够**。
> ★**已落地（2026-09-25）：默认值现为 `180s`**（用户决策，见 §12.8-⑭；判据不测偏——`llm_transport_test.py` 里
> 那条断言是跟着常量走的，不是写死 60）。
> 本机实测 `qwen3.8:27b` 单次调用 >600s、`gemma4:12b` >240s——**默认值会让它们全部以
> "传输超时"失败**，而症状看起来像"模型不行"。所以：**换模型时必须同时调
> `INTERFACETESTER_AI_TIMEOUT`**（本次对照用 300s / 1800s 两级），并且"思考型"模型还要把
> `INTERFACETESTER_AI_MAX_OUTPUT_CASES` / `max_tokens` 一起看（实测它们的 `usage` 显示
> completion 被推理吃光、正文为空）。这条已写进 §六 与 `deploy.md` §5.1 的用法提醒。

**② qwen3.8:27b 的对照：**同 3 篇 ⇄ gemma4 的完整对照（并更正一句先写的错话）**

把口径缩到**同 3 篇**（`login` / `order_create` / `user_info`），`INTERFACETESTER_AI_TIMEOUT=1800`
+ `RETRIES=0` 后台跑完。**两个模型的完整对照**：

| 指标 | `gemma4:latest` | `qwen3.8:27b` |
| --- | --- | --- |
| 有产出的文档 | **3/3** | **2/3**（`login` `exit=1`：有一片没产出） |
| 落盘用例 | **4 条** | **2 条** |
| 接口片 → 产出 | **4/4** | **1/2**（1 片失败） |
| L2（对产物） | 4/4 = 100% | 2/2 = 100% |
| 模型调用次数 | **4** | **8**（含重试/多轮） |
| 总耗时 | **57.6s** | **792s ≈ 13.7×** |

> **★更正（R14 纪律：不重写历史，标出被推翻的话）**：本文草稿里先写过一句
> "**同一篇 `login.md` 产出完全相同（各 1 条），成本 ~20×**"——那是基于**中途被我停掉的
> 第一次运行**的残留产物（它确实落过 1 条 `login` 用例）。**跑完整轮后结论变了**：
> 这一轮 `login` 在 qwen 上是 **`exit=1`（掉片，0 产出）**。
> 也就是说：**同一篇文档、同一个模型、两次运行结果不同**——`temperature=0`+`seed=0`
> **不等于**跨运行可复现（本地模型/服务端实现并不保证）。**这条比对照表本身更重要**：
> 它意味着 §12.8-⑩ 的"单模型单次"限制**不能被一次对照消掉**，只能说"在这三次实测里……"。
>
> **结论**：**换模型确实影响产量**（gemma4 4 条 vs qwen 2 条），但**本机成本差 ~14×**，
> 所以"换模型对照"在本机的价值是**证明产量对模型敏感**，而不是"挑一个更好的"——
> 换模型前先解决"**跑得动**"。

> **一条方法论留痕（零污染判据本轮"报红"了，值得记）**：qwen 这一轮的
> `source_tree_touched` 不是空集，而是 **6 条**——全是**我在它跑的同时改的文档**
> （`deploy.md` / 本文 / 待办清单）。**不是产品写盘**，但快照判据**分不清这两者**：
> 所以"取证跑动期间不要改被快照的目录"，否则零污染这条判据会被自己的人为动作污染。
> （`gemma4` 那一轮是干净的空集 ✓。）

**③ 三张人工抽检表（已生成，判定列留空）**

`probe_p0a_llm/make_review_sheets.py` 产出到 `probe_p0a_llm/review_sheets/gemma4-latest/`：

| 表 | 抽检对象 | 条数 | 要人判的那一列 |
| --- | --- | --- | --- |
| **A** | R11 断言语义正确率 | **13** 条（= 全部落盘断言） | `expect` 的取值是否符合引用句与文档**语义**（机检只保证"引用句逐字在文档里"） |
| **B** | `unknowns` 抽检（P0b 验收 ≥8/10） | **14** 条（**够 10 条**，口径可判） | 这条"降级/拒掉"判得对不对（有没有误伤/漏网） |
| **C** | 被跳过的"非接口"片 | **55** 片（带行号范围 + 原文摘录） | 这片**确实不含** `METHOD /path` 吗 |

**★脚本自带的一条独立复核（不是人判，是第二双眼睛）**：重新逐字比对每条落盘断言的
`source_quote` **是否真的在文档里** → **0 条不在**（T28 的生成期判据在产物层面**复核通过**）。

**④ 表 A 一分析就暴露的真问题（比"填表"更重要）**

13 条落盘断言的形态分布：**`equal(status_code)` × 13**——**一条字段级断言都没有**。
所以：

- **R11 在这批产物上几乎"无牙可咬"**：`status_code == 200` 的语义没有歧义，人判也判不出东西；
- 这**不是**抽检表的缺陷，而是**模型能力的实测结果**：`gemma4:latest` 只产出协议级断言
  （unknowns 里那 14 条 `protocol-only`/degrade 提示**说的正是这件事**，机器**自己报出来了**）；
- 而**确定性映射是有牙齿的**（实测）：`haify gen --assertions-only bench/golden/create_user_schema.md`
  → **断言 2 条 / 覆盖字段 2 个（文档声明 3 个）**；`user_info.md` → 0 条（该文档确实没写可断言的事实，
  被 S-4 拦下、`exit=1`）。
- **★由此登记一条真实缺口（本轮不擅自改）**：`gen`（模型通路）与 `--assertions-only`
  （确定性通路）**是两条独立命令，没有任何一处把它们合起来**——于是"模型给协议级断言 + 映射给字段级断言"
  这件本该互补的事，目前**要人手工跑两遍**。建议：在 P0a 的生成闭环里加一步"**装配前跑一遍确定性映射并合并**"
  （口径与判据需 D4 决策人确认后立项）。

**⑤ 这三列判定"做什么用"、"判错了怎么办"（口径写在这里，不靠口头约定）**

- **它们判的是"机器判不了的语义/意图"**，而**不是"放行与否"**：三张表的结论
  **不接入任何自动闸门**——硬防线（S1~S11 / T28 / L1 / L2 / 内核加载）**一个都不依赖人的判定**。
  人判错的后果是"**统计口径偏一点**"，不是"坏东西进仓"。
- **判错有 5 道兜底**：① 口径是**比例**（B 是 ≥8/10），单条判错不改变结论（本轮实测：B 判错 1 条 →
  9/11 = 81.8% 仍达标）；② 本轮 A/B/C 是**全量**（13/14/55），没有抽样误差；
  ③ 每行都留着**机器给的依据**（引用句 / 行号+原文 / 判定理由），判错**永远可被复查**；
  ④ **允许填 `?`（无法判定）**——`?` 不计入分子分母、单列统计（"证据不足就输出未知"，不许猜）；
  ⑤ 口径级结论应走**双人评审**（本仓 v7/v8 的既成做法）。
- **处置流程（已做成工具，不是靠"更仔细"）**：`probe_p0a_llm/score_review_sheets.py`
  按**表头**定位判定列 → 认 `✓ / ✗ / ?` → 出机读结果 `out_review_scores_<模型>.json` +
  **追加**历史 `SCORES_HISTORY.md`（**旧结论不重写**）。
  "改一格 → 重跑"这条链路由 `demo_score_flow.py` **实测演示**：第一次 A/B/C =
  70.0% / 72.7%（P0b 口径：已判 11、判对 8 → 达标）/ 66.7%；把 B 的一条 `✗` 改成 `✓` 后
  → **81.8%**，历史里**两条都在**（旧那条没被改写）。
- **另加一条"别覆盖人工成果"的守卫**：表里一旦出现判定标记，重出表时**不覆盖**，
  改写 `<表名>.new` 并打印提醒——"重跑脚本把人的两小时冲掉"是最不该发生的一种事故。

**⑥ 表 B / 表 C 已填并打分（2026-09-25，**判定人：用户**；机器做第二双眼睛）**

打分（`score_review_sheets.py`）：

| 表 | 条数 | 判对 | 判错 | 无法判定 | 未判 | 正确率 | 口径结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **B** | 14 | **14** | 0 | 0 | 0 | **100%** | **P0b 验收（抽 10、判对 ≥8）→ 达标 ✅** |
| **C** | 55 | **55** | 0 | 0 | 0 | **100%** | 被跳过的片**零漏测** |
| A | 13 | — | — | — | 13 | — | 未判（该批产物全是协议级断言，无信息量可判） |

**★机器交叉检查**（`crosscheck_filled_sheets.py`，用**产物本身**核人判，不是复述人判）：

| 检查 | 结果 |
| --- | --- |
| 表 B 里标 `protocol-only` 的条目 → 去落盘 YAML 数**断言是否真的全是协议级** | **13 条可机检 → 全部一致（0 条不一致）**；另 **1 条**（`G14_utf8_bom_doc…export_tasks`）**用例被 S-4 拒、未落盘 → 无法机检**（如实标注，不算判错） |
| 表 C 里判"不含接口"的片 → 看**正文**里有没有 `METHOD /path` 形态 | **55 片中 0 片**在正文里含接口形态 → **"零漏测"得到佐证**；另有 42 片的**标题**里带接口形态（`## POST /api/login > ### 请求体` 这类**接口章节下的子表**，跳过合理——单列备案） |

> **结论（这次的"100%"是有内容的）**：在这批文档上，**闸门没有误伤**（`protocol-only`/`quote-miss`
> 的降级都判对了）、**跳过没有漏测**（55 片确实不含接口）。
> 也就是说：§12.8-⑩ 里"gemma4 产量低（13 条全是 `status_code`）"**不是闸门太严**，
> 而是**模型确实没给出可断言的字段**——这条现在**有人判 + 机检双重支撑**。
> **★边界照旧**：这 100% 是**人的判定**，机器只做"第二双眼睛"；其中若有凭印象填的，
> 可以改判 `?` 并重跑（流程见 ⑤），历史只追加。

**⑦ 本轮又抓到两个"工具自身"的 bug（留档，都在抽检工具里，不在产品里）**

1. **列错位**：表 C 的"原文摘录"列**自己就是 markdown 表格**（含转义竖线 `\|`），
   打分器原先用 naive `split("|")` → 把一列切成好几列，**判定列整体错位**
   （症状：显示出来的"判定"其实是摘录里的 `类型 \| 必填`）。已改为**按未转义的 `|` 切**。
2. **标识符被截断**：生成表时曾把用例名截到 40 字符 + `…` → 下游交叉检查**找不到文件**
   （表 B 14 条里 7 条因此漏检）。已改为**标识符列不截断**（它是要拿来对账的键），
   并给交叉检查加了"按前缀匹配 + 找不到就回'无法机检'"的容错。

> **仍未做（不虚标）**：① 表 A 仍未判（理由见上）；② 表 B 有 1 条因产物未落盘**无法机检**；
> ③ 本次 100% 是**单一位判定人**的结论（口径级结论宜双人复核）；
> ④ 这两个工具 bug 已修但**未加自动判据**（它们属于一次性取证工具，不进 `tests/`）。


### 12.8-⑫ ★第十八批（2026-09-25）：AI 旁路包的**模块地图 + 护栏**（复核决策清单时发现的真缺口）

> **起因**：复核《未做完的待决策项》时实测发现——`TestArchitectureDocCoversEveryModule` 的扫描根目录是
> `interfacetester/`（**只扫内核**），而**交付给别人用的那一层** `interfacetester_ai/`（31 个 `.py`）
> 在 `架构与调用链.md`、`能力清单.md`、`README.md` 里**一个模块名都没出现**（`gen.py` / `assembler.py` /
> `gates.py` / `export.py` 全部零命中）。也就是说："**新增了一个模块、文档里没有它**"这种腐烂，
> 在内核那边有护栏、在**交付主体这半边反而没有**。

**① 落地内容（文档 + 护栏 + 元护栏）**

- `docs/架构与调用链.md`：§二 拆成 **§2.1 内核包 `interfacetester/`** 与 **§2.2 AI 旁路包 `interfacetester_ai/`**；
  §2.2 = **31 行模块地图**（模块 / 职责 / 关键入口；来源是 `interfacetester_ai/__init__.py` 的模块表，
  **不各写一份**）+ 写盘边界（红线③）与只读 import 白名单（红线①）的成文说明；
  §〇 的导览表与 §十 的维护约定同步指向这两张表。
- `tests/docs_consistency_test.py`：新增 `test_every_ai_package_module_is_mentioned` 与
  `test_ai_package_coverage_guard_would_catch_a_new_module`。
  **★判据按"切片"判**（§2.2 标题 → §三标题之间）：两个包**有同名模块**（`cli.py` / `__init__.py`），
  查全文会让 ai 包漏掉的模块被内核那张表**蒙过去**——那是假绿，正是本文最反对的东西。
- **★注入式元护栏（实测真红）**：把 §2.2 的标题拿掉 → **`2 failed, 3 passed`**；按字节还原（md5 一致）后
  → **`5 passed`**。取证脚本留档 `probe_ai_module_map/inject_ai_section.py`。
  **★第一次注入太弱，值得留痕**：当时把标题改成 `### 2.2 AI 旁路包（改名…）`，而它仍以原标记为**前缀**，
  判据照旧通过——**注入不真，等于没验**（这是"元护栏必须真能红"的又一例）。

**② 数字联动（实测）**：`+2` 用例 → `--collect-only` **2346 → 2348**；确定性口径
**2214 → 2216 passed** / 124 skipped / 6 deselected（`README.md` 与 `deploy.md` 同源更新）。
★`deploy.md` 那处漏改是**基线联动护栏**报出来的（`TestBaselineNumbersHaveOneSource::test_all_baseline_mentions_agree`
→ `2 != 1`，点名 `README.md:231` 与 `deploy.md:228` 不一致）——它按设计"漏一处就变红"。

**③ 验收四元组（命令 / 环境 / 输出 / 退出码）**

```powershell
# 1) 新护栏本身
python -m pytest tests/docs_consistency_test.py::TestArchitectureDocCoversEveryModule -q
#  → 5 passed（3 旧 + 2 新）/ 退出码 0

# 2) 注入式元护栏：注入 → 必须红；还原 → 必须绿（脚本自带字节级还原与 md5 比对）
python probe_ai_module_map/inject_ai_section.py
#  → ① 注入后 2 failed, 3 passed（退出码 1）
#  → ② 还原 md5 一致 True
#  → ③ 复跑 5 passed（退出码 0）/ 脚本退出码 0

# 3) 全量确定性口径（6 条 deselect）
python -m pytest tests -q --deselect ...（见 README「六、测试与质量」）
#  → 2 failed（环境遗留）/ 2216 passed / 124 skipped / 6 deselected / 1729 subtests / 退出码 1（仅因那 2 个环境失败）
```

> **仍未做（不虚标）**：无（这条闭合）。**边界**：本节只保证"**模块被提到**"，不保证"职责描述与代码同步"——
> 后者仍是人的责任，与内核那张表的边界完全一致。

---

### 12.8-⑬ ★第十九批（2026-09-25）：交付说明补齐（C-9）+ **超时文案自带"该调哪个变量"**

> **起因**：复核决策清单后发现两件"小事、但会让人走弯路"的：
> ① **C-9**——交付方跑单测会看到 `2 failed`，而交付文档里**没有一处**说清"这 2 个是什么、要不要管"；
> ② **超时文案**——`DEFAULT_TIMEOUT = 60s` 对**本地大模型**不够（§12.8-⑪ 实测 `qwen3.8:27b` 单次 >600s），
> 而超时的症状只是 trace 里一行 `网络异常 ReadTimeout`、最终报"重试 N 次仍失败"——**看起来像模型不行**。
> 之前只把这条写进 `deploy.md` §5.1 **等人去查**；本批把它挪到**报错处**（报错的地方就是该改配置的地方）。

**① 超时文案（代码 + 自检 + 测试）**

- `interfacetester_ai/llm.py`：新增 `timeout_hint(config)`（进 `__all__`）；`HttpTransport.complete` 记录发生过的
  超时类异常（按**类名**含 `Timeout` 判定，覆盖 `ConnectTimeout`/`ReadTimeout`/`Timeout`），
  **重试耗尽后**在报错里追加提示：本次上限值 + `INTERFACETESTER_AI_TIMEOUT` + "本地大模型 300 起"。
  ★**只在确实超时时附**——非超时也刷这段，真提示就会被淹没（本仓对噪声的一贯态度）。
- `llm.run_selftest()` 新增第 ⑬ 块（成对判据：超时必须有、非超时**必须没有**；sandbox 开关只为本块临时打开并原样还原）。
- `tests/llm_transport_test.py::TestTimeoutHint` **3 条**：点名变量 + **当前上限值**（用 `timeout=123` 反证"不是写死的"）、
  非超时失败**不许带**、提示值取自 `config`（含 `__all__` 导出断言）。

**② 交付说明（C-9，零代码改动）**

- `deploy.md`「验证安装」第 8 条：把原来含糊的"另有 2 个与本仓代码无关的环境遗留失败"改成
  **★"那 2 个 failed"是什么**——逐条点名（文档死链 → 说明这两个文件在**上游真仓**里有、本工作区没有；
  `requests_toolbelt` → 缺 `upload` extra）+ **判定口径**（"不因改本仓代码而变化 → 不计入基线"）+
  全量口径下那 6 个外网用例的说明 + 期望值同步为 **2219 passed**。
- **新增 `docs/使用手册-AI协作层.md`**（面向"拿到包的人"，即决策清单 §6.4/§6.5 第 8 项那份 Quick Start 的初稿）：
  装 / 配（**环境变量、不读项目 `.env`** 及其理由）/ 跑（命令表 + **退出码表**）/ **怎么判断跑对了（证据看哪里）** /
  **§5 已知的 2 个环境遗留失败** / 三条硬边界 / 出问题先看哪。★纪律：**不写会过期的数字**，只写"去哪看"与"怎么判断"，
  并显式指向 `README.md`「六、测试与质量」为唯一数字来源。
- `interfacetester_ai/__init__.py` 的 `llm` 行同步（模块表是 §2.2 那张图的来源）。

**③ 数字联动（实测）**：`+3` 用例（第十九批）+ `+2`（第十八批，见上）→ `--collect-only` **2346 → 2348 → 2351**；
确定性口径 **2214 → 2216 → 2219 passed**（`README.md` 与 `deploy.md` 同源）。
★**注意那"多出来的 1 条"**：更新 `README.md` 之前，**基线联动护栏**（`test_recorded_total_matches_what_pytest_actually_collects`）
会红着，于是同一批跑出的是 `2218 passed + 1 failed`；把本行数字改对之后，那 1 条转绿 → 才是 **2219 passed**。
这不是"数字飘"，正是那条护栏在按设计咬人。

**④ 验收四元组（命令 / 环境 / 输出 / 退出码）**

```powershell
# 1) 模块自检（含新增的第 ⑬ 块）
python -m interfacetester_ai.llm -v
#  → 「[拦下 ok] 超时失败 → 报错自带 `INTERFACETESTER_AI_TIMEOUT` 提示」
#  → 「llm 接入层自检全部通过」+ 汇总行新增「超时提示」一项 / 退出码 0

# 2) 文案判据
python -m pytest tests/llm_transport_test.py -q
#  → 31 passed, 9 subtests passed / 退出码 0

# 3) 全量确定性口径（6 条 deselect）
python -m pytest tests -q --deselect ...（见 README「六、测试与质量」）
#  → 2 failed（环境遗留）/ 2219 passed / 124 skipped / 6 deselected / 1731 subtests / 退出码 1（仅因那 2 个环境失败）
```

> **仍未做（不虚标）**：① ~~`DEFAULT_TIMEOUT` 默认值仍是 60s~~ → **✅ 已于 2026-09-25 改为 `180s`**（用户决策，
> 见 §12.8-⑭；本批"提示跟着报错走"的口径仍然保留——**两者互补**：默认值对多数本地模型够用，
> 提示负责"确实还超时"时告诉人该调哪个变量）；
> ② ~~《使用手册》是**初稿**（覆盖装/配/跑/判断/已知失败/边界），"面向非技术读者"的版本仍未写。~~
> → **✅ 2026-09-25 已补齐**：技术版 `使用手册-AI协作层.md` + **非技术版** `使用手册-AI协作层-非技术版.md`
> （见 §12.8-⑭）。

---

### 12.8-⑭ ★第二十批（2026-09-25）：`DEFAULT_TIMEOUT` **60 → 180**（用户决策落地）+ **非技术版手册**

> **① 超时默认值**：用户决定把 `interfacetester_ai/llm.py::DEFAULT_TIMEOUT` 由 **60 → 180**（秒）。
> 依据是 §12.8-⑪ 的跨模型实测：`qwen3.8:27b` 单次 >600s、`gemma4:12b` >240s——**默认 60s 会让本地大模型
> 全部以"传输超时"失败**，而症状看起来像"模型不行"。
>
> **★改常量之前先查"判据有没有把 60 写死"**（本仓的老教训：常量一改、判据静默测偏）：
> 实测只有一处引用默认值——`tests/llm_transport_test.py` 里那条是 `cfg.timeout == llm.DEFAULT_TIMEOUT`，
> **跟着常量走**，不写死数字；本批新增的超时文案判据用的是**显式夹具**（`timeout=123` / `900` / `60`）并比对
> **夹具自己的值**，同样不耦合默认常量。→ 改默认值**零测试改动**、全套照绿。
>
> **② 文档联动（"默认 60"四处）**：`deploy.md` §5.1 变量表（默认值 `60` → **`180`** + 变更日期与理由）、
> `docs/使用手册-AI协作层.md` §2（同）、§12.8-⑪ 的"可执行工程结论"与 §12.6 的参数行（补"已改 180"）、
> §12.8-⑬ 的"仍未做①"（划掉，指向本批）、决策清单 **AC-1**（建议 → **已采纳并落地**）。
> ★**没有**改动 black 的 60s（那是内核 `INTERFACETESTER_BLACK_TIMEOUT`，与本项无关）——
> 两处都叫"60s"，混起来改会伤到无关行为。
>
> **③ 非技术版手册**：新增 `docs/使用手册-AI协作层-非技术版.md`（面向产品/测试/业务/文档作者）——
> 一句话理解（AI 像实习同学：起草快、但会想当然；判定只由程序做、改正式用例必须人确认）、
> **你会被请去做哪三件事**（填空白 / 确认草稿 / 看报告）、你看到的东西分别是什么、
> **十个 FAQ**（会不会偷偷改我的用例 / 会不会编造期望值 / 要不要装软件 / 报告红字怎么办 / 不管待确认清单会怎样 …）、
> 「我该动哪个」速查、小词典、以及"别删 `.ai/` 里的证据"这条纪律。
> ★纪律：**不写会过期的数字**，只写"是什么、谁来做、去哪看"。

**④ 验收四元组（命令 / 环境 / 输出 / 退出码）**

```powershell
# 1) 默认值与提示的一致性（提示里的"默认值"取自常量，不是抄的）
python -m interfacetester_ai.llm -v
#  → 自检全绿（含第 ⑬ 块「超时失败 → 报错自带 INTERFACETESTER_AI_TIMEOUT 提示」）/ 退出码 0

# 2) 受影响用例（含"跟着常量走"那条）
python -m pytest tests/llm_transport_test.py tests/gen_pipeline_test.py tests/analyze_pipeline_test.py tests/summary_test.py -q
#  → 全绿 / 退出码 0

# 3) 全量确定性口径
python -m pytest tests -q --deselect ...（见 README「六、测试与质量」）
#  → 2 failed（环境遗留）/ 2219 passed / 124 skipped / 6 deselected / 1731 subtests / collect 2351（本批为纯文档+常量，数字不变）

# 4) 新／改文档的链接自检（新手册刻意不用 markdown 链接，避免新增死链）
python -m pytest tests/docs_consistency_test.py -q
#  → 22 passed / 1 skipped / 1 failed（只有那 1 条既有环境遗留）/ 退出码 1（仅因该遗留）
```

> **仍未做（不虚标）**：~~预算上限策略仍未实测（决策清单 M-4）~~ → **✅ 2026-09-26 已补测**（四元组与边界表见决策清单 §四 M-4）；非技术版手册是**初稿**，
> 尚未经"真正的非技术读者"试读反馈（若试读后要改，按本手册自己的维护约定：只改"是什么/谁来做/去哪看"，不抄数字）。

---

## 附录 A：复核命令（本文所有数字与结论的取证方式）

在 `D:\tester` 下执行（`.venv` 里是真 5.0.1）：

```powershell
# 0) 环境自证
.\.venv\Scripts\python.exe -c "import interfacetester,sys;print(interfacetester.__version__, sys.version)"
Get-ChildItem .\.venv\Scripts\*.exe | Select-Object -ExpandProperty Name

# 1) golden 基线（§12.1 的 3/3 = 100%）
.\.venv\Scripts\python.exe bench\golden_harness.py -v
.\.venv\Scripts\python.exe -m pytest tests\golden_baseline_test.py -q          # 期望 5 passed

# 2) 三处前置护栏
.\.venv\Scripts\python.exe bench\ai_guardrails.py                              # 期望「三处护栏自检全部通过」
.\.venv\Scripts\python.exe -m pytest tests\ai_guardrails_test.py -q            # 期望 20 passed, 20 subtests

# 3) 静默陷阱闸门（§3.3 / §3.5）
.\.venv\Scripts\python.exe bench\silent_traps.py -v                            # 期望「拦得住 11 / 不误伤 15」
.\.venv\Scripts\python.exe -m pytest tests\silent_traps_test.py -q             # 期望 14 passed, 29 subtests
.\.venv\Scripts\python.exe bench\probe_silent_gate.py                          # 期望「对抗验证通过」（★v8：**任意环境**——T26 已修复编码，重定向/CI 下亦通过，§12.8-①）

# 3b) ★v8 第四批落地复核（T18 / T6 / S-6，§12.8 末注记；2026-09-23 起有效）
cmd /c "python bench\write_boundary_scanner.py > .tmp_wbs.txt 2>&1"            # 期望「红线③ 写盘边界扫描自检全部通过」，exitcode=0
.\.venv\Scripts\python.exe -m pytest tests\write_boundary_test.py -q           # 期望 20 passed, 13 subtests
python -m interfacetester_ai.gates                                            # 期望「11 条对抗样本全部命中 / 17 条合法写法全部放行」，exitcode=0
python bench\silent_traps.py                                                  # 壳 CLI 转发，同上
.\.venv\Scripts\python.exe -m pytest tests\interfacetester_ai_pkg_test.py -q  # 期望 9 passed, 21 subtests
.\.venv\Scripts\python.exe -m pytest tests\pending_list_test.py -q            # 期望 7 passed
.\.venv\Scripts\python.exe -m interfacetester_ai.diagnosis                    # ★T29：期望「六分类自检全部通过」，exitcode=0
.\.venv\Scripts\python.exe -m pytest tests\diagnosis_taxonomy_test.py -q      # ★T29：期望 17 passed, 9 subtests
.\.venv\Scripts\python.exe -m interfacetester_ai.doc_quality                   # ★T27：期望「文档体检闸自检全部通过」，exitcode=0
.\.venv\Scripts\python.exe -m pytest tests\doc_quality_test.py -q              # ★T27：期望 21 passed
.\.venv\Scripts\python.exe -m pytest tests\l2_formatting_test.py -q            # ★S-8/T13：期望 19 passed, 31 subtests
.\.venv\Scripts\python.exe bench\silent_traps.py                               # ★S-7：八条护栏自检之一，期望 exitcode=0
.\.venv\Scripts\python.exe -m pytest tests\normalize_test.py -q                # ★T7：期望 18 passed, 25 subtests
.\.venv\Scripts\python.exe -m interfacetester_ai.normalize                     # ★T7：期望「归一化管道自检全部通过」，exitcode=0
.\.venv\Scripts\python.exe -m pytest tests\workdir_test.py -q                  # ★T8：期望 9 passed
.\.venv\Scripts\python.exe -m interfacetester_ai.workdir                       # ★T8：期望「L2 作业隔离自检全部通过」，exitcode=0
.\.venv\Scripts\python.exe bench\golden_harness.py                             # ★T9：期望 样本 15 / 通过 15 / 100.0%，exitcode=0
.\.venv\Scripts\python.exe bench\gate_false_positive_audit.py                  # ★T10：期望 误伤率 0.0%，exitcode=0
.\.venv\Scripts\python.exe -m pytest tests\ai_budget_cache_test.py -q          # ★T4/T5：期望 19 passed, 27 subtests
.\.venv\Scripts\python.exe -m pytest tests\docs_consistency_test.py -q         # ★T15：期望 19 passed（含口径唯一化 3 条）
git -C D:\learn\interface-tester check-ignore -v .ai/cache/x .ai/work/x .ai/jobs/x .tmp_golden/x   # ★T12：期望逐条命中

# 4) 三套护栏合并
.\.venv\Scripts\python.exe -m pytest tests\silent_traps_test.py tests\golden_baseline_test.py tests\ai_guardrails_test.py -q
#    期望 39 passed, 49 subtests

# 5) ★ v6 新发现的 S3 缺陷（§12.5；v7 复现见 §12.7 甲1）
.\.venv\Scripts\python.exe probe_s3_review\probe_s3_edge.py

# 6) ★ 证据指针（§5.5）—— 先跑一次带 --save-tests 的用例再查
cd project-two; ..\.venv\Scripts\hrun.exe login_flow.yml --save-tests; cd ..
.\.venv\Scripts\python.exe -c "import json;d=json.load(open('project-two/logs/login_flow.summary.json',encoding='utf-8'));r=d['details'][0]['records'][0];print('on record:', 'validators' in r);print('in data:', 'validators' in r['data']);print('log sibling:', 'log' in d['details'][0])"
#    期望 False / True / True

# 7) ★ 算子数与别名（§5.2 / §12.4）
.\.venv\Scripts\python.exe -c "from interfacetester.make import BUILTIN_COMPARATOR_NAMES as B;print('count=',len(B))"
.\.venv\Scripts\python.exe -c "from interfacetester.make import ensure_known_comparators as E;E([{'name':'s','request':{'url':'/x'},'validate':[{'gte':['status_code',1]}]}])"
#    期望：抛 FunctionNotFound（证明 gte 不是别名）

# 8) 内核规模与关键词穷举
(Get-ChildItem interfacetester -Recurse -Filter *.py).Count
((Get-ChildItem interfacetester -Recurse -Filter *.py | Get-Content | Measure-Object -Line).Lines)   # 期望 18840（口径 = 非空行！总行数见第 9 条）
(Get-ChildItem .venv\Lib\site-packages\interfacetester -Recurse -Filter *.py | Select-String -Pattern '静默|假通过' | Measure-Object).Count   # 期望 233
```

### v7 的补充取证（§0.4 / §12.7 逐条对应）

```powershell
# 9) ★v7：口径标签——"非空行" vs "总物理行"
((Get-ChildItem interfacetester -Recurse -Filter *.py | Get-Content | Measure-Object -Line).Lines)                                        # 18840 = 非空行
(Get-ChildItem interfacetester -Recurse -Filter *.py | ForEach-Object { (Get-Content $_.FullName).Count } | Measure-Object -Sum).Sum     # 22173 = 总物理行
#    期望：18840 / 22173  （对应 §0.4-⑩、T23；★v8 补注：两者均为 Get-Content 默认编码（GBK）口径——UTF-8 正确口径为 19,267 / 22,638，GBK 吞换行偏差 −465 行，取证见 §12.8-⑤）

# 10) ★v7：分桶到底有没有被钉住
Select-String -Path tests\golden_baseline_test.py -Pattern 'bucket|GOLDEN_TOTAL|GOLDEN_PASSED'
#    期望：只有 GOLDEN_TOTAL=3 / GOLDEN_PASSED=3 被 assert；bucket 仅出现在失败提示字符串
Select-String -Path bench\golden_harness.py -Pattern 'FAIL_LOAD|FAIL_MAKE|FAIL_COMPILE|FAIL_PROJECT|FAIL_OTHER'
#    期望：FAIL_PROJECT 有定义但不可达（_classify 兜底 return make）→ 可达三桶（§0.4-③、T19）

# 11) ★v7：S1 的 contained_by 是否实现
(Select-String -Path bench\silent_traps.py -Pattern 'contained_by' | Measure-Object).Count           # 期望 0（§0.4-④、T20）

# 12) ★v7：投影必须覆盖响应侧时间戳
.\.venv\Scripts\python.exe -c "import json;d=json.load(open('project-two/logs/login_flow.summary.json',encoding='utf-8'));print(d['details'][0]['records'][0]['data']['req_resps'][0]['response']['headers'])"
#    期望：headers 里含秒级 'Date'（§0.4-①、X5、T17）

# 13) ★v7：mock server 的真实位置（`examples/*/mock_server.py` 不存在）
Get-ChildItem -Recurse -Filter mock_server.py | Where-Object { $_.FullName -notlike '*\.venv\*' }
#    期望：tests\mock_server.py、project-two\mock_server.py（§0.4-⑨）

# 14) ★v7：S4 只看断言的 check 路径 →「取 token 用于 header」被误报 PENDING
.\.venv\Scripts\python.exe probe_s3_review\probe_s3_edge.py
#    期望：案例① 仍报 S4 PENDING（误报），说明 S4 判据必须扩展（§0.4-⑦、T21）
#    ★v8 修复后复跑 → 案例① 零 finding（T21 已落地：判据扩展到 $var 用法 + 词边界）

# 15) ★v7：空 teststeps 已由内核硬拒（G3 文案过期）
Select-String -Path interfacetester\make.py -Pattern '空 `teststeps` 必须报错' -Context 0,6
#    期望：注释与 not teststeps 抛错分支都在（§0.4-⑥、T22）

# 16) ★v7：v4 错指针的真实出处
Select-String -Path 'docs\interfacetester智能化改造方案4.md' -Pattern 'validators'
#    期望：命中 §二（P3a 看板）与 §4.5（fix 产物行），§4.3 无（§0.4-⑫）

# 17) ★v8：probe_silent_gate 在重定向（CI 口径）下的退出码（§12.8-①、T26、G8）
Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
foreach ($s in @('probe_silent_gate','golden_harness','silent_traps','ai_guardrails')) {
    & .\.venv\Scripts\python.exe "bench\$s.py" *> $null
    "{0,-18} exitcode={1}" -f $s, $LASTEXITCODE
}
#    期望：四行全为 exitcode=0（★v8 2026-09-23 实测：T26 落地后四行全 0；修复前 probe_silent_gate = 1（UnicodeEncodeError: 'gbk' codec can't encode '\u2713'））

# 18) ★v8：15,767 的口径与 GBK/UTF-8 编码偏差（§12.8-⑤、T23）
$py = Get-ChildItem interfacetester -Recurse -File -Filter *.py | Where-Object {$_.FullName -notmatch '__pycache__'}
(($py | ForEach-Object { Get-Content $_.FullName } | Where-Object {$_.Trim() -ne '' -and -not $_.TrimStart().StartsWith('#')}).Count)   # → 15767（GBK 口径，非空非#）
(($py | ForEach-Object { Get-Content $_.FullName -Encoding UTF8 }).Count)                                                                # → 22638（UTF-8 总行；GBK 口径 22173 少 465 行——GBK 吞换行）
#    期望：15767 / 22638；完整口径对照（UTF-8 非空 19,267、非空非# 15,947）见 §12.8-⑤
```

在内核真仓库 `D:\learn\interface-tester` 下核对护栏（只读）：

```powershell
Select-String -Path interfacetester\converters\__init__.py -Pattern 'SUPPORTED_SOURCES|IMPLEMENTED_SOURCES|validate_emitted_case'
Select-String -Path interfacetester\converters\emit_yaml.py -Pattern '副作用|format_pytest_with_black'
Select-String -Path interfacetester\make.py -Pattern 'BUILTIN_COMPARATOR_NAMES|def ensure_known_comparators|def normalize_module_segment|def ensure_generatable_teststeps|pytest_files_run_set'
Select-String -Path interfacetester\response.py -Pattern '_warn_value_not_found|_assertion_expects_null|def uniform_validator'
Select-String -Path interfacetester\builtin\comparators.py -Pattern '只拦能精确判定|_warn_if_both_operands_are_strings|_ensure_string_shaped'
Select-String -Path interfacetester\parser.py -Pattern 'function_regex_compile|引号'
Select-String -Path tests\docs_consistency_test.py -Pattern '^class Test'
```

---

## 附录 B：开发位置与发布路径（避免"改错仓"）

| 要做的事 | 必须在哪个仓 | 原因 |
| --- | --- | --- |
| 内核改动（表驱动分派、`[ai]` extras、模块地图更新、新增护栏） | **`D:\learn\interface-tester`**（有 `.git`、`pyproject.toml`、`tests/`、`deploy.md`） | 内核是**构建仓产物**；改完必须 `python -m build --wheel` 重出 wheel，再分发给用 wheel 的环境 |
| 旁路包 `interfacetester_ai`（本文推荐的主线，内核 0 改动） | **可独立建仓/建目录**（本工作区 `D:\tester` 即可起步） | 只 import 内核公开面，与内核构建解耦，互不阻塞 |
| 只用不改（跑用例、看报告） | 任意装了 wheel 的环境（如 `D:\tester\.venv`） | 与内核源码无关 |

```powershell
# 内核 wheel（在构建仓）
python -m build --wheel            # 产物在 dist\
# 旁路包 wheel（在 ai 仓；★第十二批实测：产物落在你指定的 --outdir）
python -m build --wheel --outdir dist_ai interfacetester_ai
# ★离线打轮（本机没有 build/wheel 时：用**基础解释器**的 setuptools + --no-build-isolation）
#   （`C:\python312\python.exe` 只是**本机**的基础解释器路径，它 = `sys.base_prefix` 下的 python）
& 'C:\python312\python.exe' -m pip wheel --no-build-isolation --no-deps --no-index -w .\dist_ai .\interfacetester_ai
# 离线环境安装（把两个 wheel + requirements 一起带过去；★实测：新空 venv + --no-index 即装得上）
python -m pip install --no-index --no-deps .\interfacetester-5.0.1-py3-none-any.whl
python -m pip install --no-index --no-deps .\interfacetester_ai-0.1.0-py3-none-any.whl
```

> 提醒：`.venv` **不要跨机器拷贝**（Windows venv 硬编码原机路径）；换机器一律重新 `python -m venv` 再装 wheel。

**★两仓一致性校验（v8 按独立评审 P5 补）**：上表的指引隐含前提是"`D:\learn\interface-tester` 与
`D:\tester` 里的内核源码树**同版本**"。这个前提**会漂移**（改了一边忘了另一边）——而漂移后
本文所有"实测"都会静默失效（正是文档自己反复警告的"护栏漂移"模式）。所以补一条可跑的命令：

```powershell
# 两仓内核源码树逐字节比对（34 个 .py 全部一致 → 无输出；有差异 → 逐条列出）
$a = 'D:\learn\interface-tester\interfacetester'
$b = 'D:\tester\interfacetester'
$ra = Get-ChildItem $a -Recurse -Filter *.py | ForEach-Object { $_.FullName.Substring($a.Length) }
foreach ($rel in $ra) {
    $fa = Join-Path $a $rel; $fb = Join-Path $b $rel
    if (-not (Test-Path $fb)) { "only-in-learn: $rel"; continue }
    if ((Get-FileHash $fa -Algorithm SHA256).Hash -ne (Get-FileHash $fb -Algorithm SHA256).Hash) { "DIFF: $rel" }
}
# 期望：无输出（v8 实测两处 34 个 .py 逐字节一致，§一 核对对象）
```

> **为什么把它写进文档而不是只留在复核记录里**：这条命令是**附录 B 自身的前提**。
> 前提不可验证时，附录 B 的"改哪个仓"就成了一句无据的指引——与 R14（"已钉住"的保真度幻觉）同类。

---

## 附录 C：与前作的差异速查

| 主题 | 方案 3（v3.0） | 方案 4（v4.0） | 方案 5（v5.0） | v6.0 | v7.0 | **v8.0（本文）** |
| --- | --- | --- | --- | --- | --- | --- |
| 落点 | `interfacetester_ai` **或** `interfacetester.plugins.ai`（进内核） | 旁路包，内核 0 改动 | 同 v4 | 旁路包，并把"禁止改内核"提为待评审的**硬约束候选**（§十一 D2） | **不变**（v7 只改文档与待办，不动架构） | **架构不变**（v8 除文档外**当日落地三批代码**：T26、S-2b/T11/T17、T19/T20——全部落在 `bench/` 旁路与 `tests/` 护栏用例，内核零改动，立场声明见文末） |
| 架构 | 三层（框架/AI/人类审核） | 三层 + 只读白名单 | 同 v4 | 三层 + 七条红线 + 白名单 + 复用纪律（§二） | **不变**；但红线③ 的判定方式改为"**写入目标路径 + 模块白名单**"，与 §6 的缓存/审计落盘对齐（§0.4-②） | **不变** |
| 校验闸门 | `hmake`/`load_testcase`/语法校验 | L1/L2/L3 | L1 + **S1~S11** + L2 + L3 | 五道关固定顺序：L1 → S → L2 → 写盘 → L3（§3.1） | **不变**；新增"**S 闸门三方对账**"（判据 ↔ 代码分支 ↔ MUST_REJECT 样本，§3.3/§3.5） | **不变**；S4 判据加**词边界要求**（防子串漏报，§0.5-③、T21 加严——★T21 已落地） |
| 静默陷阱 | 未提 | §4.2 一句 prompt 约束 | **三分类 + 11 条硬闸门 + 反例集** | 继承，并重定义 S3 为三态 + 补 2 条 MUST_ALLOW（§3.3/§3.5） | 继承；**S1 判据删掉未实现的 `contained_by`**、**S4 的"被引用"扩展到任何 `$var` 用法**（§0.4-④⑦） | 继承；**补 S4 的漏报方向**（子串匹配实测静默，§0.5-③——★已落地：漏报对抗样本恢复报出，§12.8-③ 修复注记） |
| 防幻觉机制 | 证据优先 + 人工审核 | 人工确认为唯一闸门 | 机器闸门在前、人工在后 | 机器闸门 + 人工 + 明示边界 R11（"可判定但语义错"） | **＋ R14「"已钉住/已实现"的保真度幻觉」**（§9.2） | **＋"实测通过四元组"**（命令/环境/输出/退出码，§12.8）——R14 防不住"把待办写成已完成"，v7 自己中招（§0.5-②④） |
| 证据指针 | 未提 | `records[j].validators…`（**错**） | 更正为 `.data.validators…` | 采纳更正，并给出**完整结构实测**（§5.5） | 采纳；并**把 v4 的错出处更正为 §二/§4.5**（§0.4-⑫） | **不变** |
| 确定性验收 | 未提 | 逐字节一致（**恒红**） | 归一化投影 | 采纳投影，并列出完整剔除清单（§5.6） | **清单补全**：响应头白名单（剔 `Date`/`Set-Cookie`/`Server`）+ `platform`（§0.4-①、X5） | **判据不变**；★T11/T17 已当日落地（`bench/normalize_projection.py` + 9 条用例；落地时新发现并补剔**顶层 `time`**，§5.6） |
| L2 的信号强度 | 当作校验步骤 | 当作硬闸门 | 指出 black 降级 → 暴露 `formatting` | 采纳，并据此把 S 排在 L2 之前（§3.1） | **不变** | **不变** |
| 失败分桶口径 | 未提 | 未提 | 未提 | `load / make / compile / project`，并称"已被 `golden_baseline_test.py` 钉住" | **改为可达三桶 `load/make/compile`**，并补桶名断言与形态自检（§0.4-③、T19） | **不变**；★T19 已落地（2026-09-23：`test_classify_buckets_are_pinned` 钉住三桶与常量值——v7 曾把其交付物写成"已完成"，§0.5-② 修复注记） |
| `git status` 判据 | 未提 | 直接使用（**工作区失效**） | 分层判据 | 采纳分层判据（§5.7） | **不变** | **不变** |
| `fix` 的前置 | 允许方式 C 受控自动修正 | 过 `load_testcase_file` | **双前置**（+ 闸门） | 双前置 + **红线⑦** | **不变** | **不变** |
| 失败归因 | **五分类 + 证据优先 + 结构化输出** | 四分类 | 未展开 | 完整采纳方案 3 的五分类与证据纪律（§5.3） | **不变** | **不变** |
| 延伸场景 | **8 项** | 部分（debugtalk/摘要） | 未提 | 全部纳入长期路线（§8.4 第 4 批） | **不变** | **不变** |
| 工作量 | 未给 | 三处互斥数字，总计 34~48 或 32~45 | +4（2.8 已完成） | A 25~34 + B 7~13 = **32~47**；裁剪 **22~28** | **A 26~35 + B 7~13 = 33~48；裁剪 23~29**（补 S-2b 0.55 / S-9 0.3 两行，§8.2） | **不变 33~48**（T26 的 0.1 计入 S-7 之内，不重算，§0.5 注） |
| 复用护栏 | 未提 | 立"必须复用"纪律 | 严格执行 | 继承 + 新增"**适配器形态自检**"（P-9） | **＋ "保真自检"（判据 ↔ 分支 ↔ 样本，P-10 / §3.3）** | **不变** |
| 数字可复核性 | 无数字 | 附录命令可跑（✅） | 附录命令可跑（✅） | 继承 + §十二 区分"实测/估算/未验证" | **＋ 每个数字带口径标签**（`18,840` = 非空行）**＋ §12.7 的再复核记录** | **＋ 口径标签含读取编码**（`18,840` = GBK 非空行；UTF-8 口径 19,267；15,767 补命令，§12.8-⑤）**＋ §12.8 的换环境再复核** |
| 文档自检 | 无 | 无 | 无 | 修正前作 **9** 处 | 修正 v6 **12** 处，并立"**声称已钉住必须给出钉住点**"的纪律（§0.4、§一 纪律二） | 修正 v7 **5** 处（§0.5、§12.8），并立"**实测通过四元组**"纪律 |

---

> **一句话收尾**：五份前作的方向都是对的——**让 LLM 当作者、让代码当闸门、让内核当裁判、让人来兜语义**——而且它们最了不起的一点是**每一条结论都附了可复制的复核命令**。
> v6 做了三件收口的事：**把互相矛盾的口径统一成一个数**、**把"看似拦得住实则拦不住 / 看似放得过实则拦下"的边界修准**、**把没定案的分歧摆到台面上而不是假装已经定案**。
> v7 做第四件：**把"我已收口"这句话本身也变成可复核的**——归一化投影的清单要真的剔完、"**已被钉住**"要能指出那行断言、"**已实现**"要能指认那个分支。
> v8 做第五件，也是最容易被漏掉的一件：**把"我已实测通过"这句话也钉上环境**——命令、环境、输出、退出码四元组齐全；否则换一个控制台、接一条 CI，它就是红的，而且红的时候你查闸门，永远查不到。
> （第五件还有个不显眼的孪生：**把"已完成"钉到已存在的用例上**——v7 立了 R14 防"声称已钉住"，自己仍把两道待做的元护栏写成了"已完成"；防幻觉的纪律若防不住作者自己，就只是防别人。v8 当日把这两道（T19/T20）真正补齐，"四道元护栏"自此名实相符。）

> 其中最紧急的三条：
> ① ~~**先修 probe_silent_gate 的编码（T26，0.1 人日）**~~——**✅ 已于 2026-09-23 当日落地**（四个脚本入口 `reconfigure(encoding="utf-8", errors="replace")`；验收四元组见 §12.8-① 注记：重定向 + 无 `PYTHONIOENCODING` 下退出码全 0，字节级重定向输出纯 UTF-8）。它曾是"实测通过"这整套说法的**地基**：不修，S-7 接 CI 第一天就红，而红的不是闸门、是取证脚本自己，团队会误以为闸门回归了，白白消耗对闸门的信任；
> ② ~~**再修 S3**~~——**✅ 已于 2026-09-23 当日落地**（S-2b = T1~T3 + T21 + T25：`gate_s3` 三态判定 + `gate_s4` 词边界跨步引用；探针三例 ① `findings: []` 通过、② 仅 1 条 S3 PENDING 通过、③ 通过；自检 11 拦/17 放全绿；pytest 18 passed + 31 subtests；验收见 §8.1 S-2b 与 §12.8-③④ 修复注记）。它曾会把"登录步骤只取 token"这种最正常的写法判死，而它给出的改法自己又走不通——修掉后，最常见多步链路**通过且零 PENDING**，"写 `validate: []`"自此是一条真实可行的改法；
> ③ ~~**补上投影的响应侧时间戳**~~——**✅ 已于 2026-09-23 当日落地**（T11+T17：`bench/normalize_projection.py` + `tests/normalize_projection_test.py` 9 条判据级用例；响应头白名单剔 `Date`/`Set-Cookie`/`Server`，跨秒对抗样本（Date +1s / UUID / 耗时 / stat 全不同）投影后**逐字节相等**，真实产物幂等；验收见 §十 T11/T17）。落地时还揪出一处更隐蔽的漏项——**顶层 `time`**（summary 级 start_at/duration），不剔同样恒红，已补进 §5.6 并钉死用例。
> 这三条都不需要联网、不需要模型、不需要样本，**却直接决定后面所有"AI 生成"的可信度**：闸门不准、取证不真，作者再强也白搭。

> **本文档作者立场声明**：§0.3 的 9 处修订属 v6；**§0.4 的 12 处修订、§12.7 的再复核记录、§3.3 的"S 闸门三方对账"纪律、§3.5 的第三/第四道元护栏、§9.2 的 R14、§9.3 的 G7、§4.4 的 X5、§7 的 P-10 均属 v7 新增**，v6 未提出；
> **§0.5 的 5 处修订、§12.8 的换环境再复核记录、§3.3/§3.4/§3.5 的漏报与现状标注、§9.3 的 G8、§8.1 的环境/目标态标注、待办 T26 均属 v8 新增**，v7 未提出；**★同日第五批（**文档级**）：待办 T27~T29（文档体检闸 / 引用-断言确定性对账 / 归因六分类与文档缺陷回流）、风险 R15/R16、§8.1 新增阶段行 S-10、§5.1/§5.3 的注记、以及 §十 表后的"★建议落地顺序"块——**其中 ① T29（第六批）与 ② T27（第七批）已落地**（`interfacetester_ai/diagnosis.py`：六分类 + 证据纪律 + `reports/doc_defects.md` 回流；`interfacetester_ai/doc_quality.py`：文档体检闸 S-10 + `reports/NEEDS_DOC_FIX.md` 缺项清单；**只剩 T28 未落地**——它**必须等 P0a 的装配器**，验收四元组待填实测）**；**同日第八批**（2026-09-24，**不在 T27~T29 序列内，属 §8.1 阶段行**）：`S-7` CI 门禁（两个平台各加 `guardrails` job，八条自检进 CI，此前 CI 只跑 pytest）+ `S-8`/`T13` 的 `formatting` 字段（七值口径 + 显式探针设计 + 注入式元护栏）；**同日第九批**（2026-09-24，同样在 T27~T29 序列之外、属 §9.x 风险项落地）：`T7` 归一化管道（`interfacetester_ai/normalize.py`，4 组对抗样本 + 偏移映射 + 判据成对）与 `T8` 作业隔离（`interfacetester_ai/workdir.py`，快照零污染断言 + 残留上报 + 串行锁）；**同日第十批**（2026-09-24，§十 T9/T10，**至此校正清单 §8 的 ② 四条全部完成**）：`T9` golden **3 → 15 组**（六类覆盖，含**真 GBK** 与**真 BOM** 文档）+ `T10` 闸门误伤率审计（`bench/gate_false_positive_audit.py`，实测**误伤 0 / PENDING 0**，并把 §13 一处"未实测"回填为实测）；

**同日第十一批**（2026-09-24，§3 口径与文档收尾，**校正清单 §3 的零依赖项**）：`T4` 输入预算代码化（`prompts.py`：`INPUT_BUDGET_BYTES` + 计量/校验/组装，**无截断入口**）+ `T5` 缓存键全字段（`cache.py`：9 字段清单 + sha256 规范化 JSON，**缺/多字段都报错**）+ `T12` 真仓 `.gitignore`（含 **P-7 的"产物/证据"分界**：manifest/reviews 刻意不忽略）+ `T15` 口径唯一化的**机器判据** + `T16`/`T22`/`T23` 结案 + 独立评审 `P1`/`P3`/`P4`/`P5` 四项文档级修正；**`T14`/`T24` 明确标为"等 P0a"**（判据需要真实产物，各自行写明"为什么现在做不了 + 已就绪什么 + 落地形态"）；
> 凡引用前作结论处均已标注出处。**v8 对代码的改动共四批（均应用户要求于 2026-09-23 当日落地）：① T26（四脚本输出编码，验收四元组见 §12.8-① 注记）；② S-2b = T1~T3 + T21 + T25（S3 三态 / S4 词边界 / 探针留档）与 T11/T17（归一化投影，含落地时新发现的清单漏项"顶层 `time`"），验收见 §8.1 S-2b、§十 各 ✅ 行与 §12.8-③④ 修复注记；③ T19/T20（§3.5 后两道元护栏：`TestClassifyBucketsArePinned` 钉桶名 / `TestGateBranchAccounting` 三方对账），验收见 §8.1 S-9、§十 T19/T20 ✅ 行与 §12.8-② 修复注记；④ T18 + T6 + S-6（红线③ 写盘白名单静态扫描器 + `interfacetester_ai` 建包迁移 + PENDING 落盘 writer），验收见 §十 T18/T6 ✅ 行、§8.1 S-6 ✅ 行与 §12.8 末"第四批落地注记"；此外未修改任何代码或既有文档（v7 原文件一字未动）**——红线③ 扫描口径与旁路包骨架
> 分别对应待办 **T18 / T6**（见 §十；**T26、T1~T3、T6、T11、T17、T18、T19、T20、T21、T25 已完成**，连同 §8.1 的 S-6 落盘）——v7 级（§0.4 新增的 T18 与 §8.1 新增的 S-2b/S-6）待办至此**全部收口**，仅剩 P0a/文档级流程项（T4/T5/T7~T10/T12~T16/T22~T24）。
> v8 的收敛判据（自我适用）：**T26 与 S-2b/T11/T17 已落地——本文所有"实测通过"的四元组现已包含重定向/CI 口径（§12.8-① 注记），S3/S4 判据与 §5.6 投影清单已能指认到实现分支与红线用例（§12.8-③④ 修复注记）；T19/T20 亦已落地（2026-09-23），§3.5 的四道元护栏现已全部能指认到测试文件里已存在的用例（§12.8-② 修复注记）——原文"落地后须全部能指认"的条件已达成；T18/T6/S-6 已落地（同日第四批），§2.2/§6 的红线③ 写盘边界与 §3.4 的 PENDING 落盘自此均能指认到扫描器注册表、包内 writer 与已存在的用例（§12.8 末注记）——v7 级待办全部收口。**（★v8 第五批新增的 T27~T29 是**待办**，**未落地**：落地时按第四批纪律补验收四元组实测与数字联动；本判据**不为它们背书"已达成"**。）
