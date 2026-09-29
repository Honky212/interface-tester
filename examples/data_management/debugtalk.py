"""造数规范（T3）：唯一 ID、按用例隔离前缀、造数与清理**成对登记**、清理失败只告警。

为什么这些函数长这样
--------------------
1. **用例不接受参数**（框架生成的签名是 `def test_start(self)`），所以夹具只能用
   `os.environ` 传值 → 这里所有「运行前缀 / 用例前缀 / token」都从环境变量读；
2. **数据必须自带隔离前缀**：并行、重跑、多人共用一个环境时，唯一 ID 只保证「不撞 id」，
   真正让「按用例回收」成立的是**前缀**（`<运行前缀>-<用例前缀>-<业务后缀>`）；
3. **造数与清理成对发生**：`create_record()` 在返回数据的同时把回收动作登记进 `_CLEANUPS`，
   由 conftest 的用例级夹具统一执行 —— 这样「忘了写清理」在结构上就不可能发生；
4. **清理失败只告警不阻断**：清理属于环境维护，失败不该把用例判失败，但必须在输出里可见。

环境变量（由 `conftest.py` 的 session 夹具写入，见 T1/T5）
--------------------------------------------------------
| 变量 | 含义 |
| --- | --- |
| `DM_BASE_URL` | 数据服务地址（示例里是本地 mock，真实项目里是被测服务） |
| `DM_TOKEN` | 会话级登录拿到的 token |
| `DM_RUN_PREFIX` | 本次运行的唯一前缀（`dm-<env>-<随机>`） |
| `DM_CASE_PREFIX` | 当前用例的隔离前缀（`<运行前缀>-<用例名>-<随机>`） |
| `DM_ALLOW_DANGEROUS_OPS` | 危险操作闸门（生产环境强制 `off`） |
"""

import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

DANGEROUS_HEADER = "X-Allow-Dangerous"
DANGEROUS_VALUE = "on"

# 「造数 → 登记清理」的登记表（由 conftest 的用例级夹具消费）
_CLEANUPS: List[Tuple[str, Callable[[], Any]]] = []


# --------------------------------------------------------------------- 环境读取
def api_base() -> str:
    """被测服务地址：优先取夹具写进环境的地址（见 T1）。"""
    return os.environ.get("DM_BASE_URL") or "http://127.0.0.1:8899"


def auth_header() -> Dict[str, str]:
    """带 token 的请求头：token 由 session 夹具登录后写入环境变量。"""
    token = os.environ.get("DM_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def run_prefix() -> str:
    return os.environ.get("DM_RUN_PREFIX") or "dm-norun"


def case_prefix() -> str:
    """当前用例的隔离前缀：**所有本用例造的数据都必须带它**（T3 的核心约定）。"""
    return os.environ.get("DM_CASE_PREFIX") or f"{run_prefix()}-unknown"


def env_name() -> str:
    return os.environ.get("DM_ENV_NAME") or "test"


def dangerous_ops_allowed() -> bool:
    return (os.environ.get("DM_ALLOW_DANGEROUS_OPS") or "off").strip().lower() in (
        "on",
        "1",
        "true",
        "yes",
    )


# --------------------------------------------------------------------- 唯一 ID
def unique_id(prefix: str = "id") -> str:
    """唯一 ID = 时间戳 + 随机串。

    NOTICE: 只有 ID 唯一是**不够**的——它解决「不撞车」，不解决「谁回收」。
    回收靠的是前缀（见 `case_prefix()`），两者要一起用。
    """
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


def unique_name(suffix: str) -> str:
    """业务数据的名字：`<用例前缀>-<后缀>-<唯一串>`（可被 `?prefix=` 精确回收）。"""
    return f"{case_prefix()}-{suffix}-{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------- 造数与回收
def create_record(suffix: str = "data", payload: Any = None) -> str:
    """造一条数据，**并登记它的回收动作**，返回记录 id。

    YAML 里这样用（先定义变量再传进来，别在参数里写引号——见《能力清单》5.1）：

        variables:
            suffix: alpha
            record_id: "${create_record($suffix)}"
    """
    name = unique_name(suffix)
    resp = requests.post(
        f"{api_base()}/records",
        json={"name": name, "payload": payload},
        headers=auth_header(),
        timeout=5,
    )
    resp.raise_for_status()
    record = resp.json()
    record_id = record["id"]
    _CLEANUPS.append((f"record {record_id}({name})", lambda rid=record_id: delete_record(rid)))
    return record_id


def delete_record(record_id: str) -> None:
    """按 id 回收单条数据（幂等：已被删过就直接返回）。

    NOTICE: 示例服务没有实现「按 id 删除」，这里用「先查名字、再按名字前缀删」等价实现；
    真实项目里通常就是 `DELETE /records/<id>`。关键是：**只删自己造的那一条**。
    """
    record = requests.get(f"{api_base()}/records/{record_id}", headers=auth_header(), timeout=5)
    if record.status_code == 404:
        return  # 已经被清理过 → 幂等，不报错
    record.raise_for_status()
    name = record.json()["name"]
    scoped = requests.delete(
        f"{api_base()}/records",
        params={"prefix": name},
        headers=auth_header(),
        timeout=5,
    )
    scoped.raise_for_status()


def run_registered_cleanups() -> List[str]:
    """执行登记表里的回收动作，返回失败描述列表（**调用方只告警、不抛出**）。

    由 `conftest.py` 的用例级夹具在用例结束后调用 —— 这样「造数与回收成对」是结构保证，
    而不是靠写用例的人记得写 teardown。
    """
    failures: List[str] = []
    while _CLEANUPS:
        description, action = _CLEANUPS.pop()
        try:
            action()
        except Exception as ex:  # noqa: BLE001 - 清理失败只告警，绝不让用例判失败
            failures.append(f"{description}: {type(ex).__name__}: {ex}")
    return failures


def cleanup_my_data(prefix: Optional[str] = None) -> int:
    """按前缀回收本次用例造的数据，返回删除条数（T2 的兜底清理，可单独调用）。"""
    target = prefix or case_prefix()
    resp = requests.delete(
        f"{api_base()}/records",
        params={"prefix": target},
        headers=auth_header(),
        timeout=5,
    )
    resp.raise_for_status()
    return resp.json()["deleted"]


def count_records(prefix: Optional[str] = None) -> int:
    """数一下库里（匹配前缀的）记录条数——用例用它来断言「上一条用例已清理干净」。"""
    resp = requests.get(
        f"{api_base()}/records",
        params={"prefix": prefix if prefix is not None else run_prefix()},
        headers=auth_header(),
        timeout=5,
    )
    resp.raise_for_status()
    return resp.json()["count"]


# --------------------------------------------------------------- 危险操作（T2 闸门）
def reset_store() -> Dict[str, Any]:
    """**危险操作**：清空整个数据服务。生产/共享环境会被服务端 403 拒绝。

    NOTICE: 这才是「危险操作」的定义——**影响范围超出本次运行**。
    按前缀删自己造的数据（`cleanup_my_data()`）不属于危险操作，任何时候都允许。
    """
    headers = dict(auth_header())
    if dangerous_ops_allowed():
        headers[DANGEROUS_HEADER] = DANGEROUS_VALUE
    resp = requests.delete(f"{api_base()}/records", headers=headers, timeout=5)
    if resp.status_code == 403:
        return {"deleted": 0, "refused": True, "detail": resp.json()}
    resp.raise_for_status()
    return {"deleted": resp.json()["deleted"], "refused": False}


def auth_login(username: str = "demo", password: str = "demo") -> str:
    """用例内登录（示例：会话夹具已登录过；这里演示「用例内主动重登」的写法）。"""
    resp = requests.post(
        f"{api_base()}/login",
        json={"username": username, "password": password},
        timeout=5,
    )
    resp.raise_for_status()
    return resp.json()["token"]
