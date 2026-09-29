"""建议③：给《智能化改造方案2》补上三处**可在实施前就落地**的护栏。

方案 v2 的设计主体是对的，但它有三处「只写了原则、没写判据」的地方。
本文档把这三处直接**实现成可运行的代码**，使 P0 开工时它们已经是现成的：

| 缺口 | 方案原文 | 本文档的处置 |
| --- | --- | --- |
| **① L3 白名单来源不明** | §2.3「需 `INTERFACETESTER_AI_SANDBOX=on` + BASE_URL 白名单」 | 实现 `L3Gate`：默认**只放行回环地址**，其余必须经 `--allow-host` **显式**放行；附「拒绝即报错、不静默降级」的判据 |
| **② `fix` 产物没过加载** | §4.5「ruamel round-trip（保注释）」 | 实现 `validate_patch_candidate`：补丁在**输出 diff 之前**必须先过 `load_testcase_file`，否则这条建议不允许输出 |
| **③ prompt 一致性无护栏** | §5.1「从内核常量动态渲染」 | 实现 `render_system_prompt` + 与内核常量的**对账断言**（禁止手抄、禁止漂移） |

## 为什么这三处值得先做

它们都是**结构性的**（不依赖模型能力、不依赖 prompt 措辞），
且都符合本仓「宁可响亮报错，也不要静默假通过」的主线：

- ① 是防「LLM 臆造出 `DELETE /api/order/1` 打到生产」的**唯一**屏障；
- ② 是防「贴回去的补丁让用例直接报错」——那会把「自愈」变成「自伤」；
- ③ 是防本仓已经发生过**两次**的同类漂移（《架构与调用链.md》§7 第 42 条：
  「算子数在文档里没有任何约束」）。

## 用法

    python -m bench.ai_guardrails          # 跑全部自检
    pytest tests/ai_guardrails_test.py     # 护栏用例
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:  # 直接 `python bench/ai_guardrails.py` 时 sys.path[0] 是 bench/
    sys.path.insert(0, BASE)

# ---------------------------------------------------------------------------
# ① L3 运行期冒烟闸门 —— **转发壳**（唯一实现在 `interfacetester_ai/l3.py`）
# ---------------------------------------------------------------------------
#
# 为什么留壳而不是把这段删掉：`tests/ai_guardrails_test.py` 与本文件的 `_selftest()`
# 都按 `bench.ai_guardrails.<name>` 引用下面这些名字。壳让「既有 import 不动」，
# 同时保证**只有一份实现**——双源漂移是本仓已经吃过两次的亏（《架构与调用链.md》§7）。
#
# 这也是 T6 对 gates/projection 用过的同一处置：包内是**唯一实现**，bench 留**转发壳**，
# `tests/interfacetester_ai_pkg_test.py` 用 `assertIs` 钉住「壳是转发不是复制」。
from interfacetester_ai.l3 import (  # noqa: E402,F401
    ALLOW_HOST_ENV,
    LOOPBACK_HOSTS,
    SANDBOX_ENV,
    L3Refused,
    check_l3_allowed,
    ensure_l3_allowed,
    is_loopback,
    run_selftest as l3_selftest,
)
from interfacetester_ai.l3 import is_loopback as _is_loopback  # noqa: E402,F401
from interfacetester_ai.l3 import split_hosts as _split_hosts  # noqa: E402,F401


# ---------------------------------------------------------------------------
# ③ prompt 动态渲染 + 一致性对账 —— **转发壳**（唯一实现 `interfacetester_ai/prompts.py`）
# ---------------------------------------------------------------------------
#
# ★搬迁注记（2026-09-25）：渲染实现与 `PROMPT_VERSION` 都收口到包内了。
# 此前 bench 与包内**各写了一个版本号**（那边 "v2.0"、包内 "0"），而缓存键取的是包内那个——
# 于是"缓存命中的到底是哪个 prompt 版本"这件事是错的（典型的同框不一致）。
from interfacetester_ai.prompts import (  # noqa: E402,F401
    OPERATOR_GUIDANCE_NEEDLES,
    PROMPT_VERSION,
    audit_operator_guidance,
    audit_prompt_against_kernel,
    get_comparator_aliases,
    operator_guidance_text,
    render_system_prompt,
)
from interfacetester_ai.prompts import _kernel_constants  # noqa: E402,F401


# ---------------------------------------------------------------------------
# ② fix 补丁必须先过加载（防止「自愈」变「自伤」）
# ---------------------------------------------------------------------------

def validate_patch_candidate(yaml_text: str, work_dir: Optional[str] = None) -> Tuple[bool, str]:
    """补丁候选（一份完整的 YAML 文本）必须先能通过 `load_testcase_file`。

    返回 ``(是否可用, 理由)``。

    为什么必须做这件事：`fix` 用 ruamel round-trip 保注释，
    而本仓有一条**重复键硬闸门**（`loader._DuplicateKeyRejectingSafeLoader`）——
    round-trip 或补丁插入若产生同名键、或把 `<<` 合并键展开成重复键，
    **贴回用例会让 `load_testcase_file` 直接报错**。
    那等于把「自愈建议」变成「自伤」。

    因此判据是：**不过加载检查的补丁，不允许出现在 diff 输出里**。
    与 `emit_yaml.validate_emitted_case` 的「导出即验证」同一取向。
    """
    import tempfile

    from interfacetester.exceptions import MyBaseError

    # 沙箱只放行工作区写：临时目录必须落在工作区内（本仓已踩过这个坑）
    base = work_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp_golden")
    os.makedirs(base, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".yml", dir=base)
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fp:
            fp.write(yaml_text)

        from interfacetester.loader import load_testcase_file

        try:
            load_testcase_file(path)
        except MyBaseError as ex:
            return False, f"补丁候选过不了用例加载：{type(ex).__name__}: {ex}"
        except Exception as ex:  # noqa: BLE001 - 任何异常都视为不可用
            return False, f"补丁候选加载时抛异常：{type(ex).__name__}: {ex}"
        return True, "补丁候选通过了 load_testcase_file"
    finally:
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def _selftest() -> int:
    failures: List[str] = []

    # ① L3 闸门
    ok, _ = check_l3_allowed("http://127.0.0.1:8000", env={})
    if ok:
        failures.append("L3：沙盒默认关时不应放行")
    ok, _ = check_l3_allowed("http://127.0.0.1:8000", env={SANDBOX_ENV: "on"})
    if not ok:
        failures.append("L3：开了沙盒后回环地址应放行")
    ok, reason = check_l3_allowed("https://api.production.com", env={SANDBOX_ENV: "on"})
    if ok:
        failures.append("L3：生产域名在无白名单时不应放行")
    ok, _ = check_l3_allowed(
        "https://api.test.com", env={SANDBOX_ENV: "on", ALLOW_HOST_ENV: "api.test.com"}
    )
    if not ok:
        failures.append("L3：显式白名单的主机应放行")

    # ③ prompt 对账
    problems = audit_prompt_against_kernel()
    if problems:
        failures.extend(problems)

    # ② 补丁校验：合法 YAML 应通过
    good = (
        "config:\n  name: t\n  base_url: http://127.0.0.1:1\n"
        "teststeps:\n  - name: s\n    request:\n      method: GET\n      url: /a\n"
        "    validate:\n      - eq: [status_code, 200]\n"
    )
    ok, _ = validate_patch_candidate(good)
    if not ok:
        failures.append("fix：一份合法用例应当通过补丁校验")
    # 重复键必须被拒（这是本仓的硬闸门）
    dup = (
        "config:\n  name: t\n  base_url: http://127.0.0.1:1\n"
        "teststeps:\n  - name: s\n    request:\n      method: GET\n      url: /a\n"
        "    validate:\n      - eq: [status_code, 200]\n"
        "    validate:\n      - eq: [body.code, 0]\n"
    )
    ok, _ = validate_patch_candidate(dup)
    if ok:
        failures.append("fix：带重复键的补丁必须被拒（否则贴回去会报错）")

    print("=" * 62)
    if failures:
        print(f"自检失败 {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        print("=" * 62)
        return 1
    print("三处护栏自检全部通过：")
    print("  ① L3 闸门：默认拒绝、回环放行、白名单显式放行")
    print("  ② fix 补丁：合法通过、重复键被拒")
    print("  ③ prompt 对账：渲染结果与内核常量一致")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    # T26（方案 v8 §12.8-①）：重定向/管道/CI 环境下，Windows 默认把 stdout/stderr
    # 按 GBK 编码，✓/✗/→ 等字符会抛 UnicodeEncodeError → 退出码 1（假红）。
    # 统一钉为 UTF-8 + errors=replace，保证「实测通过四元组」跨环境可复现。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # 不支持 reconfigure 或流已关闭
            pass
    raise SystemExit(_selftest())
