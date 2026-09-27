"""项目夹具：数据管理的会话级准备 + 用例级回收（T1 / T2）。

这份文件就是「测试数据管理约定」的 T1（会话夹具）与 T2（清理夹具）模板，
配套的造数规范在 `debugtalk.py`（T3），环境矩阵在 `.env.test` / `.env.prod`（T5）。

**三条硬约束先记住（决定这里只能这么写）**

1. 框架生成的用例方法签名是固定的（`def test_start(self)` / `(self, param)`），
   **普通 fixture 注入不进去，只有 `autouse=True` 的夹具才会生效**；
2. `autouse` 夹具**不能给用例传值**（用例不接受参数）→ 传值只有两条路：
   夹具写 `os.environ`，由 `debugtalk.py` 的函数读；或者在 YAML 里直接 `${func()}`；
3. 因此这里的职责是**准备/回收环境**，而不是「把数据塞给用例」。

**执行顺序**（同一进程内）
--------------------------
``hrun`` 先 ``make``（此时已加载项目 `.env` 与 `debugtalk.py`）→ 再起 pytest 会话 →
导入本 conftest → 运行 session 夹具 → 逐用例运行 → 每个用例前后运行 function 夹具。
所以夹具里写 `os.environ` 的时机**晚于** `.env` 的加载，能覆盖 `.env` 里的同名值。
"""

import os
import sys
import uuid

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data_store import DataStore, DANGEROUS_HEADER, DANGEROUS_VALUE, serve  # noqa: E402

ENV_NAME_VAR = "DM_ENV_NAME"
GATE_VAR = "DM_ALLOW_DANGEROUS_OPS"
BASE_URL_VAR = "DM_BASE_URL"
TOKEN_VAR = "DM_TOKEN"
RUN_PREFIX_VAR = "DM_RUN_PREFIX"
CASE_PREFIX_VAR = "DM_CASE_PREFIX"
DISABLE_CLEANUP_VAR = "DM_DISABLE_CLEANUP"

DEFAULT_ENV_NAME = "test"


def env_name() -> str:
    return (os.environ.get(ENV_NAME_VAR) or DEFAULT_ENV_NAME).strip().lower()


def dangerous_ops_allowed() -> bool:
    """危险操作（全库清空）是否允许：**生产环境强制关闭**。"""
    if env_name() == "prod":
        return False
    return (os.environ.get(GATE_VAR) or "on").strip().lower() in ("on", "1", "true", "yes")


def _debugtalk():
    """拿到项目 debugtalk 模块（框架已把它加进 sys.path，这里直接 import）。"""
    try:
        import debugtalk  # noqa: PLC0415

        return debugtalk
    except Exception:  # noqa: BLE001 - 拿不到就退化为「没有登记表」，不影响用例执行
        return None


def _store_request(method: str, url: str, token: str, **kwargs) -> requests.Response:
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("Authorization", f"Bearer {token}")
    return requests.request(method, url, headers=headers, timeout=5, **kwargs)


# --------------------------------------------------------------------------- T1
@pytest.fixture(autouse=True, scope="session")
def dm_environment():
    """T1 会话级准备：起数据服务 + 登录拿 token + 建立「本次运行」的唯一前缀。

    NOTICE: `autouse=True` 是硬要求（见模块 docstring 约束 1）；值只能通过 `os.environ` 传给用例。
    """
    # 1) 起本地数据服务（port=0 → 系统分配空闲端口，避免 CI 上端口冲突）
    httpd, base_url = serve(port=int(os.environ.get("DM_STORE_PORT") or 0))
    os.environ[BASE_URL_VAR] = base_url

    # 2) 登录拿 token：真实项目里对应「会话级登录」，token 缓存给所有用例复用
    resp = requests.post(
        f"{base_url}/login",
        json={"username": "demo", "password": "demo"},
        timeout=5,
    )
    resp.raise_for_status()
    os.environ[TOKEN_VAR] = resp.json()["token"]

    # 3) 本次运行的唯一前缀：所有数据都挂在它下面（按运行隔离，见 debugtalk 的 T3 规范）
    run_prefix = f"dm-{env_name()}-{uuid.uuid4().hex[:8]}"
    os.environ[RUN_PREFIX_VAR] = run_prefix

    # 4) 危险操作闸门：生产环境强制关闭（示例工程最想讲清的一条）
    os.environ[GATE_VAR] = "on" if dangerous_ops_allowed() else "off"

    print(
        f"[data-management] env={env_name()} base_url={base_url} "
        f"run_prefix={run_prefix} dangerous_ops={os.environ[GATE_VAR]}",
        flush=True,
    )
    try:
        yield
    finally:
        # 会话级回收：把本次运行造的数据按运行前缀删干净（**按前缀**，不会误伤历史数据）
        try:
            _store_request(
                "DELETE",
                f"{base_url}/records",
                os.environ.get(TOKEN_VAR, ""),
                params={"prefix": run_prefix},
            )
        except Exception as ex:  # noqa: BLE001 - 回收失败不该让整个会话报错
            print(f"[data-management] 会话级回收失败（忽略）：{type(ex).__name__}: {ex}", flush=True)
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------- T2
@pytest.fixture(autouse=True)
def dm_case_isolation(request):
    """T2 用例级隔离与回收：**每个用例**都有独立的隔离前缀，跑完按前缀回收自己的数据。

    - 隔离前缀写进 `os.environ`，用例通过 `${case_prefix()}` 取值（夹具不能直接传值）；
    - 回收分两步：先跑 debugtalk 里「造数时成对登记」的清理动作（T3），再按前缀兜底扫一遍
      （防止有人直接发请求造数、没走登记）；
    - **回收失败只告警、不抛出**：清理是「环境维护」，不该把用例判失败——
      但必须让它在输出里可见（否则会变成「谁也不知道数据没清干净」）。
    """
    case_prefix = (
        f"{os.environ.get(RUN_PREFIX_VAR, 'dm-norun')}-"
        f"{request.node.name[:32]}-{uuid.uuid4().hex[:6]}"
    )
    os.environ[CASE_PREFIX_VAR] = case_prefix
    try:
        yield
    finally:
        if (os.environ.get(DISABLE_CLEANUP_VAR) or "").strip().lower() in ("1", "true", "yes"):
            print(
                f"[data-management] {DISABLE_CLEANUP_VAR}=1 → 跳过用例级回收"
                f"（这是刻意留下的「数据污染」演示开关）",
                flush=True,
            )
            return

        problems = []

        # 2.1) debugtalk 里登记的清理动作（造数与清理成对注册，见 T3）
        module = _debugtalk()
        if module is not None and hasattr(module, "run_registered_cleanups"):
            problems.extend(module.run_registered_cleanups() or [])

        # 2.2) 按前缀兜底回收
        try:
            resp = _store_request(
                "DELETE",
                f"{os.environ.get(BASE_URL_VAR, '')}/records",
                os.environ.get(TOKEN_VAR, ""),
                params={"prefix": case_prefix},
            )
            if resp.status_code != 200:
                problems.append(f"scoped cleanup HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as ex:  # noqa: BLE001
            problems.append(f"scoped cleanup {type(ex).__name__}: {ex}")

        if problems:
            print(
                "[data-management] 用例级回收有失败项（**只告警不阻断**）：\n  - "
                + "\n  - ".join(problems),
                flush=True,
            )


# --------------------------------------------------------------------------- T2（危险操作闸门）
def pytest_collection_modifyitems(config, items):
    """两个跳过规则，都是「不写清就会在客户现场翻车」的那类：

    1. **危险操作用例**（文件名含 `dangerous`）在生产环境（`DM_ENV_NAME=prod`）或被显式关闭时跳过；
    2. **SQL 用例**（`sql/` 目录）在未安装 `sql` extra 时跳过——框架本身遇到缺依赖是**报错**
       （`ensure_sql_ready()` 抛异常），这里主动 skip，用例才不会被误判成「功能坏了」。
    """
    skip_dangerous = pytest.mark.skip(
        reason=f"危险操作用例：当前环境 env={env_name()}、"
        f"{GATE_VAR}={os.environ.get(GATE_VAR, 'off')}（生产环境强制跳过，见 conftest.py）"
    )
    skip_sql = pytest.mark.skip(
        reason='SQL 示例需要 sql extra：pip install -e ".[sql]"（sqlalchemy + pymysql）'
    )

    try:
        import pymysql  # noqa: F401
        import sqlalchemy  # noqa: F401

        sql_ready = True
    except ImportError:
        sql_ready = False

    for item in items:
        path = str(getattr(item, "fspath", ""))
        if "dangerous" in os.path.basename(path) and not dangerous_ops_allowed():
            item.add_marker(skip_dangerous)
        if f"{os.sep}sql{os.sep}" in path and not sql_ready:
            item.add_marker(skip_sql)


__all__ = ["DataStore", "DANGEROUS_HEADER", "DANGEROUS_VALUE", "dangerous_ops_allowed", "env_name"]
