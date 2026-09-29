# -*- coding: utf-8 -*-
r"""l3 —— **L3 运行期冒烟闸门**（§六 的配置行 / 红线③ 的近邻；**P0a** C 阶段）。

## 它防的是什么

「LLM 臆造出 `DELETE /api/order/1` 打到生产」——这是**唯一**一道屏障（不是 prompt、
不是 schema：模型完全可以吐出一个语法完美、但在生产上删数据的用例）。

## 判据（三条必须**同时**满足）

1. `INTERFACETESTER_AI_SANDBOX` 明确为开（`on/1/true/yes`）—— **默认关**；
2. `base_url` 能解析出主机名；
3. 主机是**回环**地址，**或**在 `INTERFACETESTER_AI_ALLOW_HOST` 显式清单里。

为什么不"猜"环境（例如按域名里有没有 `test`）：那是**启发式**，猜错的方向是
"以为打的是测试环境，实际打了生产"——**代价不可逆**。所以只认**显式**声明。

## ★搬迁注记（T6 模式，2026-09-25）

本模块的实现**逐字搬迁**自 `bench/ai_guardrails.py` 的 ①（那里留**转发壳**，
`tests/interfacetester_ai_pkg_test.py` 的同一性断言钉住"壳是转发不是复制"）。

为什么必须搬：`llm.py` 的 `HttpTransport` 要调 `ensure_l3_allowed`——
如果包去 import `bench`，方向就反了（bench 是"方案护栏脚本"，包是产品）；
如果两边各留一份实现，就是**双源漂移**（本仓已经吃过两次这类亏）。
搬进包 + bench 转发 = 与 `gates` / `projection` 完全相同的处置。

## 纪律

- **不写盘**（纯判断函数）；
- `check_l3_allowed` **永不抛异常**（返回 `(bool, 理由)`，好测、可换处置）；
  `ensure_l3_allowed` 才是抛异常版本——"拒绝"必须**响亮**，不许静默降级
  （静默降级会让用户以为"冒烟跑过了"，实际一步没跑）。
"""

from __future__ import annotations

import ipaddress
import os
import re
import urllib.parse
from typing import Dict, Optional, Set, Text, Tuple

# 默认放行：只允许**回环**地址。这不是"保守"，而是把默认值定在安全的那一侧——
# 要让请求打到别处，必须有人**显式**说出口。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

SANDBOX_ENV = "INTERFACETESTER_AI_SANDBOX"
ALLOW_HOST_ENV = "INTERFACETESTER_AI_ALLOW_HOST"

# 开关的"真值"写法（大小写不敏感）；其余一律视为**关**
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class L3Refused(Exception):
    """L3（运行期冒烟）被拒绝执行。

    NOTICE：这**必须**是异常而不是"跳过"——静默降级会让用户以为
    "冒烟跑过了"，而实际上一步都没跑（这正是本仓最忌讳的假通过形态）。
    """


def is_loopback(host: Text) -> bool:
    """判断 host 是否回环（含整个 `127.0.0.0/8` 段与 `localhost`）。"""
    normalized = (host or "").strip().strip("[]").lower()
    if not normalized:
        return False
    if normalized in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


# 兼容别名（搬迁前的私有名，bench 与既有用例按 `_is_loopback` 引用）
_is_loopback = is_loopback


def split_hosts(raw: Text) -> Set[Text]:
    """解析 `--allow-host` / 环境变量里的主机清单（逗号或空白分隔）。"""
    return {part.strip().lower() for part in re.split(r"[,\s]+", raw or "") if part.strip()}


_split_hosts = split_hosts


def check_sandbox(env: Optional[Dict[Text, Text]] = None) -> Tuple[bool, Text]:
    """**执行型 L3** 的开关判据：沙盒开了吗？（**不涉及主机名**——这类执行不出网）

    ★为什么单列一个函数、而不是硬塞一个假 `base_url` 去复用 `check_l3_allowed`：
    后者的语义是"**要不要把请求发到某个主机**"，它必然需要一个主机名。而"在沙盒里跑一段
    **本机、无网络**的已知答案测试"根本没有主机可判——为了复用而伪造一个回环地址进去，
    正是本仓反复批评的"形似而实不至"。两者真正共用的只有**那一个开关**：
    所以开关的读法与真值表收在这里（单一来源），`check_l3_allowed` 也调它。
    """
    env = os.environ if env is None else env
    flag = (env.get(SANDBOX_ENV) or "").strip().lower()
    if flag not in _TRUTHY:
        return (
            False,
            f"沙盒未开启：请显式设置 {SANDBOX_ENV}=on（当前"
            f"{'未设置' if flag == '' else repr(flag)}）。",
        )
    return True, f"{SANDBOX_ENV}=on"


def require_sandbox(env: Optional[Dict[Text, Text]] = None) -> None:
    """`check_sandbox` 的抛异常版本（**拒绝必须响亮**，不许静默降级）。"""
    ok, reason = check_sandbox(env=env)
    if not ok:
        raise L3Refused(reason + "执行型 L3 会**真的跑代码**，默认不执行。")


def check_l3_allowed(
    base_url: Text,
    env: Optional[Dict[Text, Text]] = None,
) -> Tuple[bool, Text]:
    """L3 闸门：判断某个 `base_url` 是否允许被"真发请求"。返回 `(是否放行, 理由)`。

    **永不抛异常**，调用方据返回值决定是否抛 `L3Refused`（这样它既好测，
    又能被上层换成别的处置）。
    """
    env = os.environ if env is None else env

    # ★开关的读法只有一处（`check_sandbox`）：这里只**追加**一句"为什么这次要拦"，
    #   避免两个函数各写一份真值表（那正是"同一口径散在两处"的经典漂移源）。
    ok, reason = check_sandbox(env=env)
    if not ok:
        return False, reason + "L3 会真发请求，默认不执行。"

    try:
        parsed = urllib.parse.urlsplit(base_url or "")
    except ValueError as ex:
        return False, f"base_url 无法解析：{base_url!r}（{ex}）"

    host = parsed.hostname
    if not host:
        return False, f"base_url 里没有主机名：{base_url!r}"

    if is_loopback(host):
        return True, f"{host} 是回环地址，允许 L3"

    allowed = split_hosts(env.get(ALLOW_HOST_ENV, ""))
    if host.lower() in allowed:
        return True, f"{host} 在显式白名单里，允许 L3"

    return (
        False,
        f"主机 {host!r} 既不是回环地址，也不在白名单里。\n"
        f"  若确认这是**测试环境**，请显式放行："
        f'{ALLOW_HOST_ENV}="{host}"（或 CLI 传 --allow-host {host}）。\n'
        f"  不要为了「跑通」而把它加到生产域名上——L3 的请求会留下真实痕迹。",
    )


def ensure_l3_allowed(base_url: Text, env: Optional[Dict[Text, Text]] = None) -> None:
    """`check_l3_allowed` 的抛异常版本（供 CLI / transport 直接调用）。"""
    ok, reason = check_l3_allowed(base_url, env=env)
    if not ok:
        raise L3Refused(reason)


def run_selftest(verbose: bool = False) -> int:
    """跑 L3 闸门自检（**纯逻辑、不写盘**）。返回 0=全绿；非 0=失败项数。"""
    from typing import List  # noqa: PLC0415

    failures: List[Text] = []

    def expect(label: Text, url: Text, env: Dict[Text, Text], want: bool) -> None:
        ok, reason = check_l3_allowed(url, env=env)
        if ok != want:
            failures.append(
                f"[判据错] {label}：期望{'放行' if want else '拒绝'}，实为{'放行' if ok else '拒绝'}（{reason}）"
            )
        elif verbose:
            print(f"  [ok] {label} → {'放行' if ok else '拒绝'}")

    # ① 默认必须在安全的那一侧
    expect("沙盒未设置 → 拒", "http://127.0.0.1:8000", {}, False)
    expect("沙盒关 → 拒", "http://127.0.0.1:8000", {SANDBOX_ENV: "off"}, False)

    # ② 开了沙盒后，本地冒烟不能被误伤（**判据的另一半**）
    for url in ("http://127.0.0.1:8000", "http://localhost:80", "http://127.0.0.5:9000"):
        expect(f"回环放行（{url}）", url, {SANDBOX_ENV: "on"}, True)

    # ③ 非回环在无白名单时必须拒，且理由要告诉人**怎么放行**
    ok, reason = check_l3_allowed("https://api.production.com", env={SANDBOX_ENV: "on"})
    if ok:
        failures.append("[判据错] 生产域名在无白名单时被放行了")
    elif "api.production.com" not in reason or ALLOW_HOST_ENV not in reason:
        failures.append(f"[理由不足] 拒绝理由没有说明主机名与放行方式：{reason}")

    # ④ 显式白名单是**受控**放行路径，必须真的能用（多主机、逗号/空白分隔）
    whitelist = {SANDBOX_ENV: "on", ALLOW_HOST_ENV: "a.com, b.com  c.com"}
    for host in ("a.com", "b.com", "c.com"):
        expect(f"白名单放行（{host}）", f"http://{host}", whitelist, True)
    expect("白名单外仍拒（d.com）", "http://d.com", whitelist, False)

    # ⑤ 开关写法容错
    for flag in ("on", "1", "true", "yes", "ON", "True"):
        expect(f"真值写法 {flag!r}", "http://127.0.0.1:1", {SANDBOX_ENV: flag}, True)
    for flag in ("off", "0", "false", "no", ""):
        expect(f"假值写法 {flag!r}", "http://127.0.0.1:1", {SANDBOX_ENV: flag}, False)

    # ⑥ 畸形 base_url 要**拒绝**，而不是抛异常崩掉
    for bad in ("", "not a url", "http://", ":::://x"):
        ok, reason = check_l3_allowed(bad, env={SANDBOX_ENV: "on"})
        if ok:
            failures.append(f"[判据错] 畸形 base_url 竟被放行：{bad!r}")
        elif verbose:
            print(f"  [ok] 畸形地址被拒：{bad!r} → {reason}")

    # ⑦ ensure_* 的语义：拒绝必须**响亮**（异常），放行必须静默
    try:
        ensure_l3_allowed("https://api.production.com", env={SANDBOX_ENV: "on"})
    except L3Refused:
        pass
    else:
        failures.append("[判据错] ensure_l3_allowed 在应拒绝时没有抛 L3Refused")
    try:
        ensure_l3_allowed("http://127.0.0.1:1", env={SANDBOX_ENV: "on"})
    except L3Refused as error:
        failures.append(f"[判据错] ensure_l3_allowed 误伤了回环地址：{error}")

    print("=" * 66)
    if failures:
        print(f"L3 闸门自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("L3 闸门自检全部通过：")
    print("  默认安全：沙盒未开（或写法为假值）→ 一律拒绝")
    print("  回环放行：127.0.0.0/8 与 localhost 在沙盒开启后正常放行（不误伤本地冒烟）")
    print(f"  受控放行：非回环必须 {ALLOW_HOST_ENV} 显式列名，理由里附放行指引")
    print("  响亮拒绝：ensure_l3_allowed 拒绝时抛 L3Refused，绝不静默降级")
    print("=" * 66)
    return 0


def main() -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="L3 运行期冒烟闸门（P0a）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "ALLOW_HOST_ENV",
    "LOOPBACK_HOSTS",
    "SANDBOX_ENV",
    "L3Refused",
    "check_l3_allowed",
    "check_sandbox",
    "ensure_l3_allowed",
    "is_loopback",
    "require_sandbox",
    "run_selftest",
    "split_hosts",
]


if __name__ == "__main__":
    raise SystemExit(main())
