# -*- coding: utf-8 -*-
"""debugtalk_draft —— **签名/加密函数草稿**（v8 §5.9 `haify debugtalk`；**P2b**，2026-09-25）。

## 这个模块要解决的那件"静默"的事（§5.9 的"源码级地雷"）

内核 `parser.py` 的函数调用正则：

    function_regex_compile = re.compile(r"\\$\\{([a-zA-Z_]\\w*)\\(([\\$\\w\\.\\-/\\s=,]*)\\)\\}")

**参数字符集里没有引号**。于是 `${f('abc')}` 不会被当成函数调用——它会退化成
"变量替换 + 原样字符串"，而框架**只告警、不报错**（`parser.py` 里紧挨着的
`_function_with_quote_regex_compile` 就是为此加的告警）。后果不是"报错让你改"，
而是**静默产出一个垃圾值**，一路流到断言里去比。

所以本模块的核心判据不是"函数写得对不对"，而是**往返解析**：生成物里的每一处
`${fn(...)}` 都必须被**同一个正则**认作函数调用（`roundtrip_check`）——
用内核的正则本身来验，而不是"照着规则再写一遍"。

## 立场：**没有测试向量，就拒绝交付**（与 P2a 的"业务码不给改"同源）

一个"看起来能用"的签名函数比没有更危险：用例会过，而报文的签名是错的——
上线后由服务端告诉你。所以每个函数**必须**带**已知答案测试**
（`sign(输入) → 期望输出`），我们会**真的执行**它（`dry_run`，在 `.ai/work/` 里
`exec` 一份临时 `debugtalk.py`）。缺向量 → **拒产并说明原因**，不产半成品。

## 对已有 `debugtalk.py`：**追加块 + diff，绝不整文件覆盖**

- 已存在的函数名 → **只提示不改写**（用 `emit_yaml._defined_function_names` 的 AST 口径查重，
  复用它的一段理由：`importlib` 会拿到**别的项目**的函数表）；
- 新函数以**追加块**呈现，并给出 diff（人工自己贴）。

## ★L3 沙盒（§5.9 的"（沙盒开关下）L3"；2026-09-25 收口）

**口径**（一次明确决策，钉在这里；口径变了必须改这里、改判据、改文档三处）：

> **允许执行 · 只跑已知答案测试 · 允许真实加密运算。**

翻译成代码就是三条：

1. **能跑什么，是枚举出来的**：沙盒里**只**做一件事——`exec` 草稿源码，然后拿
   **已知答案测试**去比期望输出。不跑文档、不跑用例、不跑任何别的东西。
   ★注意"允许执行"**不等于**"什么都能执行"：这正是本仓反复强调的那条界线——
   把"能做什么"写成**白名单动作**，而不是"打开一个 shell"。
2. **允许真实加密运算**：`hashlib` / `hmac` / `base64` / `binascii` / `struct` / `zlib` /
   `json` / `re` / `math` / `time` … 这些**纯计算**模块照常可用（签名函数本来就要用它们）。
   落在代码里就是**不拦它们**——而不是"给个开关让你自己关护栏"。
3. **拦的是"误伤"**：出网 / 碰文件系统 / 起进程 / 读环境变量。四类都是"这条命令不该做的事"，
   而不是"用户不许做的事"。

**★诚实登记它挡不住什么**：子进程与父进程**同一用户权限**，所以它**不是**对抗恶意代码的
牢笼（能写恶意代码的人有的是办法绕）。它挡的是**误伤**：草稿里一句 `requests.post(...)`
把测试数据打到真实环境、一句 `open("~/.ssh/id_rsa")` 读到凭据、一句死循环挂住终端。
把边界说清楚，比把"沙箱"三个字说得比实际更硬要好。

**为什么值得单起一个进程**：① 超时能**杀**（进程内跑死循环只能 Ctrl-C）；② 护栏
（禁 `open` / 禁出网 / 清空 `sys.modules` 里的危险模块）装在一个**随时可丢弃**的解释器里，
不需要"卸妆"；③ 子进程的**环境变量白名单**里没有任何 `INTERFACETESTER_AI_*`，凭据不进子进程。
"""

from __future__ import annotations

import ast
import contextlib
import difflib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

from interfacetester_ai import l3 as l3_mod

# 生成物里必须过往返解析的调用形态（判据：内核正则认得它）
ROUNDTRIP_TEMPLATE = "${{{name}({args})}}"

# 草稿块的注释头（人工要看得出这是生成物、以及它为什么长这样）
DRAFT_BANNER = "# --- 由 haify debugtalk 生成的草稿（请人工审阅后再用）---"

# 拒产原因（都要能打给用户）
REFUSE_NO_VECTOR = "no_vector"
REFUSE_BAD_VECTOR = "bad_vector"
REFUSE_QUOTE_IN_ARGS = "quote_in_args"
REFUSE_DUPLICATE = "duplicate_name"


@dataclass(frozen=True)
class TestVector:
    """一条**已知答案测试**：`fn(*args, **kwargs) == expect`。

    ★为什么它必须是**输入**而不是模型生成的：模型自己编一个"期望输出"，
    等于**自己给自己出题、自己判卷**——那正是"看起来通过"的经典形态。
    向量要么来自人工，要么来自**可独立验证**的来源（如 `md5("abc")` 的公开值）。
    """

    args: Tuple[Any, ...] = ()
    kwargs: Dict[Text, Any] = field(default_factory=dict)
    expect: Any = None
    note: Text = ""

    # ★给 pytest 的逃逸口：`Test` 开头的类会被当成测试类收集
    #（`cannot collect test class 'TestVector' because it has a __init__ constructor`）。
    # 名字里带 "Test" 是**语义**需要（它就是"测试向量"）——不该为了工具改名，
    # 该关的是收集器。**无类型注解的类属性不会被 dataclass 当成字段**，所以这是安全的。
    __test__ = False

    def call_text(self) -> Text:
        """人话的调用形态（报告里用）。"""
        parts = [repr(item) for item in self.args]
        parts.extend(f"{key}={value!r}" for key, value in self.kwargs.items())
        return f"{', '.join(parts)} → {self.expect!r}"
    @classmethod
    def from_dict(cls, payload: Dict[Text, Any]) -> "TestVector":
        return cls(
            args=tuple(payload.get("args", ()) or ()),
            kwargs=dict(payload.get("kwargs", {}) or {}),
            expect=payload.get("expect"),
            note=str(payload.get("note", "") or ""),
        )


@dataclass(frozen=True)
class FunctionRequest:
    """一个待生成/待校验的函数草稿（含它的**已知答案测试**）。"""

    name: Text
    args: Tuple[Text, ...] = ()
    doc: Text = ""  # 它为什么被需要（哪份文档 / 哪条用例引用了它）
    body: Text = "raise NotImplementedError"  # 函数体草稿（人工要改）
    vectors: Tuple[TestVector, ...] = ()

    def signature(self) -> Text:
        """函数签名（草稿块的 def 行）。"""
        positional = ", ".join(self.args)
        return f"def {self.name}({positional}):"

    def call_expr(self, index: int = 0) -> Text:
        """第 `index` 条向量的**往返解析用**调用表达式（`${fn(a, b)}`）。

        ★参数按 §5.9 的约束**裸写**：标量直接写字面量，字符串**不能带引号**
        （带引号就退化成"变量替换 + 原样字符串"，而且**不报错**）。
        所以字符串要经 `config.variables` 以 `$var` 传入——这里用 `$var` 形态占位。
        """
        if not self.vectors:
            return ROUNDTRIP_TEMPLATE.format(name=self.name, args="")
        vector = self.vectors[index % len(self.vectors)]
        args = [_bare_arg(item) for item in vector.args]
        args.extend(f"{key}={_bare_arg(value)}" for key, value in vector.kwargs.items())
        return ROUNDTRIP_TEMPLATE.format(name=self.name, args=", ".join(args))

    @classmethod
    def from_dict(cls, payload: Dict[Text, Any]) -> "FunctionRequest":
        return cls(
            name=str(payload.get("name", "") or ""),
            args=tuple(str(item) for item in payload.get("args", ()) or ()),
            doc=str(payload.get("doc", "") or ""),
            body=str(payload.get("body", "") or "raise NotImplementedError"),
            vectors=tuple(
                TestVector.from_dict(item)
                for item in payload.get("vectors", ()) or ()
                if isinstance(item, dict)
            ),
        )


def _bare_arg(value: Any) -> Text:
    """把参数渲染成**裸写**形态（§5.9：参数不能带引号）。

    - 数字/布尔 → 字面量；
    - 字符串 → `$<名字>`（经 `config.variables` 传入；`$` 后只留 `\\w`，空格与标点换下划线）；
    - 其它 → 同样退化成 `$` 变量形态（**不是** `repr`：`repr` 会带引号，那正是地雷）。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    word = re.sub(r"[^0-9a-zA-Z_]+", "_", str(value)).strip("_") or "arg"
    if word[0].isdigit():
        word = "v_" + word
    return "$" + word


# ---------------------------------------------------------------------------
# ★ 往返解析（判据 = **内核的正则本身**）
# ---------------------------------------------------------------------------

def roundtrip_check(call_expr: Text) -> Tuple[bool, Text]:
    """`${fn(...)}` 是不是**内核认的**函数调用 → `(是否通过, 说明)`。

    ★判据用**内核自己的正则**（`parser.function_regex_compile`），
    不是"我照着规则再写一遍"：重新实现一份"看起来一样"的校验，等于把
    "能不能被解析"从**内核说了算**变成**我说了算**——§5.9 那行地雷的麻烦正在这儿。

    反面形态（必须判不合格）：`${f('abc')}` —— 参数带引号，内核**不认得**它，
    只会原样留成字符串，而且**只告警、不报错**。
    """
    from interfacetester.parser import (  # noqa: PLC0415
        _function_with_quote_regex_compile,
        function_regex_compile,
    )

    expr = str(call_expr or "").strip()
    if not expr:
        return False, "调用表达式为空"

    match = function_regex_compile.fullmatch(expr)
    if match:
        return True, f"内核正则认得：函数 `{match.group(1)}`，参数 `{match.group(2)}`"

    if _function_with_quote_regex_compile.match(expr):
        return False, (
            "参数**带引号**——内核的参数字符集是 `[\\$\\w\\.\\-\\/\\s=,]`，**不含引号**，"
            "所以它不会被当成函数调用、会被原样留成字符串（**只告警、不报错**）。"
            "请裸写标量，字符串经 `config.variables` 以 `$var` 传入"
        )
    return False, f"内核正则不认这个形态：`{expr}`（形如 `${{fn(a, b)}}` 才认）"


def check_request_roundtrip(request: FunctionRequest) -> List[Text]:
    """一个函数草稿的**所有**调用形态的往返结论（空 = 全通过）。"""
    problems: List[Text] = []
    for index in range(max(1, len(request.vectors))):
        expr = request.call_expr(index)
        ok, why = roundtrip_check(expr)
        if not ok:
            problems.append(f"`{expr}`：{why}")
    return problems


# ---------------------------------------------------------------------------
# 已知答案测试（**真的执行**；见下方安全说明）
# ---------------------------------------------------------------------------

def dry_run(requests: Sequence[FunctionRequest], *, source: Text) -> List[Text]:
    """执行每个函数的**已知答案测试**。返回失败清单（空 = 全过）。

    **安全说明（诚实登记）**：`source` 是**人工提供**的函数草稿源码，本函数用
    `exec` 在**新命名空间**里跑它。这与"框架 import 项目的 `debugtalk.py`"是**同一等级**
    的执行面（`hrun` 本来就会执行那个文件），所以本命令**没有新开一条攻击面**——
    但它确实**会执行代码**，因此：① 只在本机开发时跑；② 不要拿来源不明的草稿喂它。

    ★为什么"真的执行"而不是"检查有没有写向量"：判据要是"**函数与它的期望输出对得上**"，
    而不是"有没有那张表"。一张期望值写错的表比没有表更坏——它让人以为验过了。

    ★为什么 `exec` 而不是 `import debugtalk`：`emit_yaml._defined_function_names`
    的注释里记着这个坑——`importlib` + `sys.path` 会拿到**进程里已加载过的别的项目**的
    函数表。这里只要一个干净命名空间：无缓存、无副作用、不会串项目。
    """
    failures: List[Text] = []
    namespace: Dict[Text, Any] = {}
    try:
        exec(compile(source, "<draft>", "exec"), namespace)  # noqa: S102 - 见上方安全说明
    except Exception as error:  # noqa: BLE001
        return [f"草稿源码无法执行：{type(error).__name__}: {error}"]

    for request in requests:
        func = namespace.get(request.name)
        if not callable(func):
            failures.append(f"`{request.name}`：草稿里没有这个可调用函数")
            continue
        for index, vector in enumerate(request.vectors):
            try:
                got = func(*vector.args, **vector.kwargs)
            except Exception as error:  # noqa: BLE001
                failures.append(
                    f"`{request.name}` 向量[{index}] 抛异常：{type(error).__name__}: {error}"
                )
                continue
            if got != vector.expect:
                failures.append(
                    f"`{request.name}` 向量[{index}]：期望 {vector.expect!r}，实际 {got!r}"
                )
    return failures


# ---------------------------------------------------------------------------
# L3 沙盒：子进程 + **只跑已知答案测试** + 允许真实加密运算
# （口径见模块 docstring；本节判据都在 run_selftest 里）
# ---------------------------------------------------------------------------

SANDBOX_SENTINEL = "@@KAT_REPORT@@"
SANDBOX_TIMEOUT_SECONDS = 20.0
SANDBOX_MODE = "沙盒（子进程 + 只跑已知答案测试）"
INPROCESS_MODE = "本机干跑（进程内 exec）"
SANDBOX_WORKER_FLAG = "--kat-worker"

# 沙盒子进程里**禁止**的顶层模块。★取向：凡是"能出网 / 能碰文件 / 能起进程 / 能读环境 /
# 能动态加载（可绕过上面几条）"的，一个都不给；而**纯计算**模块
# （`hashlib` / `hmac` / `base64` / `binascii` / `struct` / `zlib` / `json` / `re` / `math`
# / `time` / `datetime` / `decimal` / `statistics` / `collections` / `itertools` …）
# **一律放行**——"允许真实加密运算"这条口径就落在这张表的**缺口**上。
SANDBOX_DENIED_MODULES = (
    # 出网 / 套接字
    "socket", "ssl", "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib",
    "telnetlib", "xmlrpc", "asyncio", "webbrowser", "socketserver", "selectors",
    "requests", "httpx", "urllib3", "aiohttp", "websockets", "paramiko",
    # 进程 / 系统调用 / 信号
    "subprocess", "multiprocessing", "ctypes", "cffi", "signal", "resource",
    "pty", "tty", "pipes", "fcntl", "termios",
    # 文件系统
    "os", "io", "pathlib", "shutil", "tempfile", "glob", "fileinput", "linecache",
    "mmap", "stat", "filecmp", "dbm", "shelve", "sqlite3", "tarfile", "zipfile",
    "gzip", "bz2", "lzma", "configparser",
    # 动态加载 / 反序列化（这几条能**绕过**上面的护栏，所以必须一起拦）
    "importlib", "imp", "runpy", "marshal", "pickle", "copyreg", "code", "codeop",
    "compileall",
    # 环境 / 终端 / 凭据
    "getpass", "pdb",
)
_DENIED_SET = frozenset(SANDBOX_DENIED_MODULES)


def _deny(what: Text) -> None:
    """沙盒里被拦动作的统一话术（**拒因必须能打给人看**）。"""
    raise PermissionError(
        f"[沙盒] 禁止 {what}：本沙盒只跑**已知答案测试**——出网 / 文件 / 进程 / "
        "环境变量一律不放行；纯计算（含 hashlib/hmac/base64 等真加密运算）照常可用。"
    )


def _kat_worker() -> int:
    """子进程入口（`--kat-worker`）：读载荷 → 装护栏 → exec 草稿 → 跑 KAT → 回报 JSON。

    ★载荷走 **stdin**（不是临时文件）：子进程装完护栏后**连 `open` 都没有**，
    所以"读自己的输入"这件事必须在装护栏之前完成——stdin 是最顺的形态。
    """
    import builtins  # noqa: PLC0415

    raw = sys.stdin.read()
    payload: Dict[Text, Any] = json.loads(raw) if raw.strip() else {}
    source = str(payload.get("source") or "")
    requests = payload.get("requests") or []

    # ① 先记下"装护栏前"的事实（装完之后这些 API 就不该再被调用）
    workdir = os.getcwd()
    env_keys = sorted(os.environ)

    # ② 抓住真货的引用，然后把危险入口**换成拒绝**（顺序：先抓引用、再替换）
    real_exec = builtins.exec
    real_import = builtins.__import__
    real_stdout = sys.stdout
    buffer = io.StringIO()

    class _Gate:
        """meta_path 守卫（鸭子类型即可：import 机制只要求有 `find_spec`）。"""

        def find_spec(self, name, path=None, target=None):  # noqa: ANN001, ARG002
            top = str(name).split(".")[0]
            if top in _DENIED_SET:
                raise ImportError(
                    f"[沙盒] 禁止 import {name!r}：`{top}` 属于被拦类别"
                    "（出网 / 文件 / 进程 / 环境 / 动态加载）"
                )
            return None

    # ★导入窗口的深度计数：`hashlib` 自己会再 import 别的模块（嵌套 import），
    #   若用简单的 try/finally，**内层的 finally 会把外层的窗口提前关掉**，
    #   于是外层剩下的 import 又撞上"禁止 exec"（实测踩到，靠 DEBUG 打出调用方才看清）。
    import_depth = {"n": 0}

    def _guard_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001, A002
        top = str(name).split(".")[0]
        if top in _DENIED_SET:
            raise ImportError(
                f"[沙盒] 禁止 import {name!r}：`{top}` 属于被拦类别"
                "（出网 / 文件 / 进程 / 环境 / 动态加载）"
            )
        # ★导入期间**必须把 `exec` 临时让开**：`importlib` 自己就用 `builtins.exec`
        #   （`_bootstrap` 的 `_call_with_frames_removed` 里那句 `exec(code, module.__dict__)`）——
        #   否则连 `import hashlib` 都会被自己的护栏拦住（真加密运算第一条 KAT 直接 PermissionError）。
        #   **让开的窗口只在 import 内部**，草稿代码自己调 `exec` 仍然被拦。
        import_depth["n"] += 1
        builtins.exec = real_exec
        try:
            return real_import(name, globals, locals, fromlist, level)
        finally:
            import_depth["n"] -= 1
            if import_depth["n"] == 0:
                builtins.exec = _blocked_exec

    def _blocked_open(*_args, **_kwargs):
        _deny("open（文件系统）")

    def _blocked_eval(*_args, **_kwargs):
        _deny("eval")

    def _blocked_exec(*_args, **_kwargs):
        _deny("exec")  # ★防"自我解锁"：草稿里再 exec 一段把护栏拆掉

    sys.meta_path.insert(0, _Gate())
    builtins.__import__ = _guard_import
    builtins.open = _blocked_open
    builtins.eval = _blocked_eval
    builtins.exec = _blocked_exec

    # ③ 清掉**已经加载**的危险模块：留在 `sys.modules` 里就是现成的后门
    #    （`sys.modules["os"]` 不需要 import 就能用）
    purged = [name for name in list(sys.modules) if name.split(".")[0] in _DENIED_SET]
    for name in purged:
        sys.modules.pop(name, None)

    guards = ["meta_path", "__import__", "open", "eval", "exec", f"purged:{len(purged)}"]

    # ④ 只做这一件事：exec 草稿源码 → 跑已知答案测试（**不做别的**）
    results: List[Dict[Text, Any]] = []
    exec_error = ""
    namespace: Dict[Text, Any] = {}
    sys.stdout = buffer  # 草稿里的 print 一律收进 buffer，**不污染协议**
    try:
        try:
            real_exec(compile(source, "<draft>", "exec"), namespace)  # noqa: S102
        except Exception as error:  # noqa: BLE001
            exec_error = f"草稿源码无法执行：{type(error).__name__}: {error}"
        else:
            for item in requests:
                name = str(item.get("name") or "")
                func = namespace.get(name)
                if not callable(func):
                    results.append(
                        {
                            "name": name,
                            "index": -1,
                            "ok": False,
                            "detail": f"`{name}`：草稿里没有这个可调用函数",
                        }
                    )
                    continue
                for index, vector in enumerate(item.get("vectors") or []):
                    try:
                        got = func(*(vector.get("args") or []), **(vector.get("kwargs") or {}))
                    except Exception as error:  # noqa: BLE001
                        results.append(
                            {
                                "name": name,
                                "index": index,
                                "ok": False,
                                "detail": f"`{name}` 向量[{index}] 抛异常："
                                f"{type(error).__name__}: {error}",
                            }
                        )
                        continue
                    expect = vector.get("expect")
                    ok = got == expect
                    results.append(
                        {
                            "name": name,
                            "index": index,
                            "ok": ok,
                            "detail": ""
                            if ok
                            else f"`{name}` 向量[{index}]：期望 {expect!r}，实际 {got!r}",
                        }
                    )
    finally:
        sys.stdout = real_stdout

    report: Dict[Text, Any] = {
        "guards": guards,
        "workdir": workdir,
        "env_keys": env_keys,
        "modules_purged": len(purged),
        "exec_error": exec_error,
        "results": results,
        "captured_stdout": buffer.getvalue()[-2000:],
    }
    real_stdout.write(
        SANDBOX_SENTINEL + " " + json.dumps(report, ensure_ascii=False, default=str) + "\n"
    )
    return 0


# ---------------------------------------------------------------------------
# L3 沙盒（**父进程侧**）：闸门 → 起子进程 → 解析回报
# ---------------------------------------------------------------------------

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 子进程环境变量**白名单**。★这里**没有** `INTERFACETESTER_AI_*`——凭据不进子进程。
#   `SYSTEMROOT`/`TEMP` 是 Windows 上解释器启动要用的；`PYTHONPATH` 让 `-m` 找得到本包。
SANDBOX_ENV_ALLOW = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP")


@dataclass
class SandboxRun:
    """一次沙盒执行的结果（父进程侧）。"""

    mode: Text
    failures: List[Text] = field(default_factory=list)
    report: Dict[Text, Any] = field(default_factory=dict)
    detail: Text = ""

    @property
    def ok(self) -> bool:
        return not self.failures


def _sandbox_payload(
    requests: Sequence[FunctionRequest], source: Text
) -> Dict[Text, Any]:
    """把"要跑的东西"编成 JSON：**只有草稿源码 + 已知答案测试**（没有别的可跑）。"""
    return {
        "source": source,
        "requests": [
            {
                "name": item.name,
                "vectors": [
                    {
                        "args": list(vector.args),
                        "kwargs": dict(vector.kwargs),
                        "expect": vector.expect,
                    }
                    for vector in item.vectors
                ],
            }
            for item in requests
        ],
    }


def _parse_report(stdout: Text) -> Optional[Dict[Text, Any]]:
    """从子进程 stdout 里取**最后一条**哨兵行并解析。

    ★倒着找：草稿里的 `print` 已被收进 buffer，但**别的**东西仍可能往 stdout 写
    （解释器自己、将来加的代码），所以协议要能容忍前面有噪声。
    """
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(SANDBOX_SENTINEL):
            try:
                parsed = json.loads(line[len(SANDBOX_SENTINEL) :].strip())
            except ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def run_kat_in_sandbox(
    requests: Sequence[FunctionRequest],
    *,
    source: Text,
    env: Optional[Dict[Text, Text]] = None,
    timeout: float = SANDBOX_TIMEOUT_SECONDS,
    python: Optional[Text] = None,
) -> SandboxRun:
    """在 **L3 沙盒**里跑已知答案测试（口径见模块 docstring）。**只跑 KAT，不做别的。**

    ★四条判据：
    1. **闸门在前**：`require_sandbox()` 不过就抛 `L3Refused`——**子进程一个都不起**
       （"拒绝了、可已经跑了一半"是最坏的形态）；
    2. **超时能杀**：跑飞了在 `timeout` 秒后被终止，失败清单里**如实写"超时"**；
    3. **回报必须解析得到**：拿不到回报（崩了 / 被杀 / 协议被污染）→ 判失败并附 stderr 尾巴，
       **绝不当成"跑过了"**；
    4. **子进程 env 里没有任何 `INTERFACETESTER_AI_*`**，且 `cwd` 是一个**空临时目录**。
    """
    l3_mod.require_sandbox(env=env)  # ① 闸门在前：不过就抛，零副作用

    payload = _sandbox_payload(requests, source)
    child_env = {name: os.environ[name] for name in SANDBOX_ENV_ALLOW if name in os.environ}
    child_env["PYTHONPATH"] = BASE
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"

    argv = [
        python or sys.executable,
        "-m",
        "interfacetester_ai.debugtalk_draft",
        SANDBOX_WORKER_FLAG,
    ]
    with tempfile.TemporaryDirectory(prefix="ai_kat_") as workdir:
        try:
            proc = subprocess.run(  # noqa: S603
                argv,
                input=json.dumps(payload, ensure_ascii=False, default=str),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=workdir,
                env=child_env,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return SandboxRun(
                mode=SANDBOX_MODE,
                failures=[
                    f"沙盒**超时**（{timeout:g}s）：子进程已被终止——"
                    "死循环 / 长阻塞挂不住终端，但这一条 KAT **没有结论**"
                ],
                detail="timeout",
            )
        except OSError as error:
            return SandboxRun(
                mode=SANDBOX_MODE,
                failures=[f"沙盒**起不来**（{type(error).__name__}: {error}）"],
                detail="spawn-error",
            )

    report = _parse_report(proc.stdout)
    if report is None:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        return SandboxRun(
            mode=SANDBOX_MODE,
            failures=[
                f"沙盒**没有回报**（退出码 {proc.returncode}）——不当成「跑过了」。"
                f"stderr / stdout 尾巴：{tail}"
            ],
            detail="no-report",
        )

    failures: List[Text] = []
    if report.get("exec_error"):
        failures.append(str(report["exec_error"]))
    for item in report.get("results") or []:
        if not item.get("ok"):
            failures.append(str(item.get("detail") or "未说明原因"))
    return SandboxRun(
        mode=SANDBOX_MODE,
        failures=failures,
        report=report,
        detail=f"exit={proc.returncode}",
    )


# ---------------------------------------------------------------------------
# 渲染与落点（**追加块 + diff，绝不整文件覆盖**）
# ---------------------------------------------------------------------------


def render_draft_block(requests: Sequence[FunctionRequest]) -> Text:
    """渲染草稿块（把每个函数的**已知答案测试**也写进注释里——人工一眼能看到验过什么）。"""
    lines = [DRAFT_BANNER, ""]
    for request in requests:
        lines.append(f"# 来源：{request.doc or '（未标注）'}")
        lines.append(request.signature())
        for body_line in (request.body or "raise NotImplementedError").splitlines():
            lines.append(f"    {body_line}")
        lines.append("")
        lines.append("# 已知答案测试（本命令已执行过，见下面的验收）：")
        for vector in request.vectors:
            lines.append(f"#   {request.name}({vector.call_text()})")
        lines.append("")
    return "\n".join(lines)


def append_to_existing(existing_text: Text, draft_block: Text) -> Text:
    """把草稿块**追加**到已有源码末尾（**绝不整文件覆盖**）。

    ★为什么连"要不要补一个换行"都值得写出来：`debugtalk.py` 是**人对函数的唯一记录**
    ——里面可能有手写的备注、别的项目的函数、已经上线并调好的签名实现。
    "生成器整文件重写"会把它们抹掉，而且在 diff 里**看得出来但不刺眼**。
    """
    if not existing_text:
        return draft_block
    separator = "" if existing_text.endswith("\n") else "\n"
    return f"{existing_text}{separator}\n\n{draft_block}"


def _function_names_in_source(text: Text) -> List[Text]:
    """从**源码文本**取函数名（AST；`source_text` 场景用）。"""
    try:
        tree = ast.parse(text or "")
    except (SyntaxError, ValueError):
        return []
    return sorted(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def existing_function_names(source_path: Text, *, source_text: Optional[Text] = None) -> List[Text]:
    """已有 `debugtalk.py` 里的函数名。**两条路径都要有**。

    ★为什么不能只认路径：**"重名只提示不改写"这条判据跟文件无关，跟源码有关**。
    调用方把源码传进来（不落盘的场景：测试、`--stdin`）时，如果这里只认路径，
    查重就会**静默失效**——草稿会照常被采纳，而人以为查过了。
    （这个 bug 是自检抓出来的：`source_text` 场景下重名提示没出现。）
    """
    if source_path and os.path.exists(source_path):
        from interfacetester.converters.emit_yaml import _defined_function_names  # noqa: PLC0415

        return sorted(_defined_function_names(source_path))
    if source_text:
        return _function_names_in_source(source_text)
    return []


def render_diff(before: Text, after: Text, *, path: Text) -> Text:
    """已有文件 → 追加后的 diff（带行号；人工自己贴）。"""
    return "".join(
        difflib.unified_diff(
            (before or "").splitlines(keepends=True),
            (after or "").splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


# ---------------------------------------------------------------------------
# 裁决与主入口
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DraftDecision:
    """一个函数的裁决（**产** / **拒**）。"""

    name: Text
    accepted: bool
    reason: Text = ""
    code: Text = ""  # 原因码（`no_vector` / `quote_in_args` / `bad_vector` / `duplicate_name`）
    roundtrip: Tuple[Text, ...] = ()

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "name": self.name,
            "accepted": self.accepted,
            "code": self.code,
            "reason": self.reason,
            "roundtrip": list(self.roundtrip),
        }


@dataclass
class DraftResult:
    """一次草稿生成的结果。"""

    decisions: List[DraftDecision] = field(default_factory=list)
    draft_block: Text = ""
    appended: Text = ""
    diff: Text = ""
    existing_path: Text = ""
    existing_names: Tuple[Text, ...] = ()
    # 本次 KAT 是在**哪种模式**下跑的（"本机干跑" or "L3 沙盒"）——报告里必须写出来：
    # "验过了"却不说是**在哪验的**，等于把两种差异巨大的证据混成一句话。
    dry_run_mode: Text = ""
    sandbox_report: Dict[Text, Any] = field(default_factory=dict)

    def accepted(self) -> List[DraftDecision]:
        return [item for item in self.decisions if item.accepted]

    def rejected(self) -> List[DraftDecision]:
        return [item for item in self.decisions if not item.accepted]

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "accepted": [item.name for item in self.accepted()],
            "rejected": [item.to_dict() for item in self.rejected()],
            "existing": list(self.existing_names),
            "has_diff": bool(self.diff),
        }


def load_requests(spec: Any) -> List[FunctionRequest]:
    """从 JSON **字典 / 路径 / 文本** 读函数请求（形如 `{"functions": [...]}`）。"""
    payload = spec
    if isinstance(spec, str):
        text = spec
        if os.path.exists(spec):
            with open(spec, encoding="utf-8") as fp:
                text = fp.read()
        try:
            payload = json.loads(text)
        except ValueError:
            return []
    if not isinstance(payload, dict):
        return []
    items = payload.get("functions")
    if not isinstance(items, list):
        return []
    return [FunctionRequest.from_dict(item) for item in items if isinstance(item, dict)]


def decide(
    request: FunctionRequest,
    *,
    existing_names: Sequence[Text] = (),
    dry_run_failures: Sequence[Text] = (),
) -> DraftDecision:
    """一个函数的裁决。**三条拒产理由**（每条都要能直接打给用户）：

    1. **没有已知答案测试**（§5.9："测试向量缺失时拒绝交付"）；
    2. **往返解析不过**（`${fn(...)}` 内核不认——典型是参数带引号）；
    3. **已知答案测试不过**（函数与期望输出对不上，或草稿源码跑不起来）。

    ★**重名不算拒**：§5.9 说"同名只提示不改写"——已经有人写过这个函数，
    那是"你该先去看一眼"，不是"这个请求无效"。
    """
    name = request.name or "?"
    name_failures = [item for item in dry_run_failures if f"`{name}`" in item]

    if not request.vectors:
        return DraftDecision(
            name=name,
            accepted=False,
            code=REFUSE_NO_VECTOR,
            reason=(
                "**没有已知答案测试**——按 §5.9「测试向量缺失时拒绝交付」。"
                "一个'看起来能用'的签名函数比没有更危险：用例会过，而报文签名是错的"
            ),
        )

    roundtrip = tuple(check_request_roundtrip(request))
    if roundtrip:
        return DraftDecision(
            name=name,
            accepted=False,
            code=REFUSE_QUOTE_IN_ARGS,
            reason="调用形态**过不了内核的往返解析**；" + "；".join(roundtrip),
            roundtrip=roundtrip,
        )

    if name_failures:
        return DraftDecision(
            name=name,
            accepted=False,
            code=REFUSE_BAD_VECTOR,
            reason="已知答案测试**没通过**：" + "；".join(name_failures),
            roundtrip=roundtrip,
        )

    if name in set(existing_names):
        return DraftDecision(
            name=name,
            accepted=True,
            code=REFUSE_DUPLICATE,
            reason=(
                f"`{name}` **已存在于 `debugtalk.py`**——按 §5.9「同名只提示不改写」："
                "草稿块里仍然给出它，但请**先看已有的那份**再决定要不要贴"
            ),
            roundtrip=roundtrip,
        )

    return DraftDecision(
        name=name, accepted=True, reason="已验（往返解析 + 已知答案测试）", roundtrip=roundtrip
    )


def draft_debugtalk(
    spec: Any,
    *,
    existing_path: Text = "",
    source_text: Optional[Text] = None,
    skip_dry_run: bool = False,
    sandbox: bool = False,
    env: Optional[Dict[Text, Text]] = None,
    timeout: float = SANDBOX_TIMEOUT_SECONDS,
) -> DraftResult:
    """主入口：spec（JSON 字典 / 路径 / 文本）→ 裁决 → 草稿块 + 追加 diff。**不写盘**。

    ★**为什么不写盘**：§5.9 要求的是"以**追加块 + diff** 呈现"——呈现给**人**，
    由人自己贴（和 P2a 同款立场：产物给人看，落地动作留给人）。
    因此本模块**不进** T18 注册表。

    ★干跑用的是**要交付的那份文本**（`render_draft_block`），不是另一份"能跑的版本"：
    验的必须是**交付物本身**，否则"验过了"与"交付的能不能跑"就是两件事。

    ★`sandbox=True` → 走 **L3 沙盒**（§5.9 的"（沙盒开关下）L3"）：已知答案测试在
    **子进程**里跑（能超时杀、护栏随进程丢弃、凭据不进子进程），而**不是**在本进程里 `exec`。
    沙盒没开 → `L3Refused`（**响亮**，且**一个子进程都不起**）。默认 `False` = 本机干跑（原行为）。
    """
    requests = load_requests(spec)
    result = DraftResult()

    if source_text is None and existing_path and os.path.exists(existing_path):
        with open(existing_path, encoding="utf-8", errors="replace") as fp:
            source_text = fp.read()
    source_text = source_text or ""
    result.existing_path = existing_path or ""
    result.existing_names = tuple(
        existing_function_names(existing_path, source_text=source_text)
    )

    failures: List[Text] = []
    if requests and not skip_dry_run:
        # ★两种模式验的是**同一份文本**（`render_draft_block(requests)`）：
        #   模式只改变"在哪跑"，不改变"验什么"。
        source = render_draft_block(requests)
        if sandbox:
            run = run_kat_in_sandbox(requests, source=source, env=env, timeout=timeout)
            failures = run.failures
            result.sandbox_report = run.report
            result.dry_run_mode = run.mode
        else:
            failures = dry_run(requests, source=source)
            result.dry_run_mode = INPROCESS_MODE
    elif requests:
        result.dry_run_mode = "已跳过（`--skip-dry-run`：**不建议**，等于没验过）"

    result.decisions = [
        decide(request, existing_names=result.existing_names, dry_run_failures=failures)
        for request in requests
    ]

    accepted = [request for request, decision in zip(requests, result.decisions) if decision.accepted]
    result.draft_block = render_draft_block(accepted) if accepted else ""
    if result.draft_block:
        result.appended = append_to_existing(source_text, result.draft_block)
        if source_text:
            result.diff = render_diff(
                source_text,
                result.appended,
                path=(existing_path or "debugtalk.py").replace(os.sep, "/"),
            )
    return result


def render_draft_report(result: DraftResult) -> Text:
    """渲染给人看的报告。**只返回文本，不落盘**。"""
    lines = [
        "# debugtalk 函数草稿（由 interfacetester_ai.debugtalk_draft 生成）",
        "",
        "> **只呈现、不落地**：本命令**不写** `debugtalk.py`（§5.9：「追加块 + diff 呈现」，人工自己贴）。",
        "> 只有过了两道机器判据的函数才会出现在草稿块里：**往返解析**（内核正则认得 `${fn(...)}`）",
        "> 与**已知答案测试**（真的执行过函数、比过期望输出）。",
        f"> 本次「已知答案测试」跑在：**{result.dry_run_mode or '（没跑）'}**"
        + (
            "（子进程护栏：" + "、".join(result.sandbox_report.get("guards") or []) + "）"
            if result.sandbox_report
            else ""
        ),
        "",
    ]
    if result.existing_path:
        lines.append(f"- 已有文件：`{result.existing_path}`（**不会被改写**）")
        lines.append(f"- 其中已有函数：{', '.join(result.existing_names) or '（无）'}")
        lines.append("")
    lines.append(f"- 采纳 **{len(result.accepted())}** 个 / 拒绝 **{len(result.rejected())}** 个")
    lines.append("")

    for decision in result.decisions:
        flag = "✅ 采纳" if decision.accepted else "⛔ 拒绝"
        lines += [f"## {flag} · `{decision.name}`", "", f"- 理由：{decision.reason}"]
        for item in decision.roundtrip:
            lines.append(f"- 往返：{item}")
        lines.append("")

    if result.draft_block:
        lines += [
            "## 草稿块（请人工审阅后自己贴）",
            "",
            "```python",
            result.draft_block.rstrip(),
            "```",
            "",
        ]
    if result.diff:
        lines += ["## 追加 diff", "", "```diff", result.diff.rstrip(), "```", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 自检（**不写盘**：只在内存里生成 + `exec` 临时草稿源码）
# ---------------------------------------------------------------------------

_SELFTEST_SPEC: Dict[Text, Any] = {
    "functions": [
        {
            "name": "sign_md5",
            "args": ["payload"],
            "doc": "接口要求 X-Signature：md5(payload)",
            "body": "import hashlib\nreturn hashlib.md5(str(payload).encode()).hexdigest()",
            # ★这条向量是**可独立验证**的：`md5("abc")` 是公开已知值
            "vectors": [{"args": ["abc"], "expect": "900150983cd24fb0d6963f7d28e17f72"}],
        },
        # ① 没有向量 → 必须拒产
        {"name": "no_vectors_fn", "args": ["x"], "body": "return x"},
        # ② 期望输出对不上 → 必须拒产
        {
            "name": "bad_fn",
            "args": ["x"],
            "body": "return 'wrong'",
            "vectors": [{"args": ["1"], "expect": "right"}],
        },
    ]
}

_SELFTEST_EXISTING = """def old_fn():
    return 1
"""


def run_selftest(verbose: bool = False) -> int:
    """debugtalk_draft 自检。返回 0=全绿；非 0=失败项数。**不写盘**。"""
    failures: List[Text] = []

    # ① ★往返解析：判据是**内核的正则**，不是我们自己写的一份
    ok, why = roundtrip_check("${sign_md5($payload)}")
    if not ok:
        failures.append(f"[往返] 裸参调用应当被内核认作函数调用：{why}")
    bad, why_bad = roundtrip_check("${sign_md5('abc')}")
    if bad:
        failures.append("[往返] 参数带引号的调用竟然被判合格——那正是 §5.9 的静默地雷")
    if "引号" not in why_bad:
        failures.append(f"[往返] 拒绝理由应当点明'引号'：{why_bad}")

    # ② 裸参渲染：字符串参数必须退化成 `$变量`（**不带引号**），数字保持字面量
    if _bare_arg("hello world") != "$hello_world":
        failures.append(f"[裸参] 字符串应当变成 `$变量`：{_bare_arg('hello world')}")
    if _bare_arg(18) != "18":
        failures.append(f"[裸参] 数字应当是字面量：{_bare_arg(18)}")

    # ③ 裁决：三条拒产 + 一条采纳
    result = draft_debugtalk(_SELFTEST_SPEC, source_text=_SELFTEST_EXISTING)
    codes = {item.name: item.code for item in result.decisions}
    if [item.name for item in result.accepted()] != ["sign_md5"]:
        failures.append(f"[裁决] 应当只采纳 sign_md5：{result.to_dict()}")
    if codes.get("no_vectors_fn") != REFUSE_NO_VECTOR:
        failures.append(f"[裁决] 无向量必须拒产：{codes.get('no_vectors_fn')}")
    if codes.get("bad_fn") != REFUSE_BAD_VECTOR:
        failures.append(f"[裁决] 已知答案测试不过必须拒产：{codes.get('bad_fn')}")

    # ④ 干跑**真的执行**：把期望值改一个字 → 必须从"采纳"变"拒产"（元护栏）
    broken = json.loads(json.dumps(_SELFTEST_SPEC, ensure_ascii=False))
    broken["functions"][0]["vectors"][0]["expect"] = "0" * 32
    broken_result = draft_debugtalk(broken, source_text=_SELFTEST_EXISTING)
    if broken_result.accepted():
        failures.append("[元护栏] 期望值改错后仍然被采纳——说明干跑没真的执行")

    # ⑤ **追加、不覆盖**：原文逐字保留，且 diff 是"只增不删"
    if not result.appended.startswith(_SELFTEST_EXISTING):
        failures.append("[追加] 已有源码没有被逐字保留（这是'整文件覆盖'的苗头）")
    removed = [
        line
        for line in result.diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]
    if removed:
        failures.append(f"[追加] diff 里出现了删除行：{removed}")

    # ⑥ 重名：**只提示不改写**（仍是采纳，但理由里要点出"已存在"）
    dup_spec = {"functions": [dict(_SELFTEST_SPEC["functions"][0], name="old_fn")]}
    dup = draft_debugtalk(dup_spec, source_text=_SELFTEST_EXISTING)
    if not dup.accepted():
        failures.append("[重名] 同名不该判成拒产（§5.9：只提示不改写）")
    elif "已存在" not in dup.decisions[0].reason:
        failures.append(f"[重名] 理由里应当提示已存在：{dup.decisions[0].reason}")

    # ⑦ 草稿块里**带着已知答案测试**（人工能一眼看到验过什么）
    if "900150983cd24fb0d6963f7d28e17f72" not in result.draft_block:
        failures.append("[草稿块] 已知答案测试没有写进注释")

    # ⑧ ★L3 沙盒（§5.9 的"（沙盒开关下）L3"；口径：允许执行 · 只跑已知答案测试 · 允许真实加密运算）
    #    ① 闸门在前：沙盒没开 → `L3Refused`（**一个子进程都不起**）
    try:
        run_kat_in_sandbox([], source="", env={})
    except l3_mod.L3Refused as refused:
        if l3_mod.SANDBOX_ENV not in str(refused):
            failures.append(f"[沙盒] 拒因没点名开关 {l3_mod.SANDBOX_ENV}：{refused}")
    else:
        failures.append("[沙盒] 沙盒没开竟然也跑了——闸门没生效（这是最坏的一种假通过）")

    #    ② 开了就跑得通：**真加密运算**（hashlib）+ 已知答案测试（md5("abc") 是公开可验证值）
    crypto_source = (
        "def sign_kat(payload):\n"
        "    import hashlib\n"
        '    return hashlib.md5(payload.encode("utf-8")).hexdigest()\n'
    )
    crypto = [
        FunctionRequest(
            name="sign_kat",
            args=("payload",),
            vectors=(TestVector(args=("abc",), expect="900150983cd24fb0d6963f7d28e17f72"),),
        )
    ]
    sandbox_env = {l3_mod.SANDBOX_ENV: "on"}
    run = run_kat_in_sandbox(crypto, source=crypto_source, env=sandbox_env)
    if not run.ok:
        failures.append(f"[沙盒] 真加密运算的已知答案测试没过：{run.failures}")
    guards = list(run.report.get("guards") or [])
    for needed in ("meta_path", "__import__", "open", "exec"):
        if needed not in guards:
            failures.append(f"[沙盒] 护栏没装上：{guards}")
    leaked = [
        key
        for key in (run.report.get("env_keys") or [])
        if key.startswith("INTERFACETESTER_AI_")
    ]
    if leaked:
        failures.append(f"[沙盒] 子进程 env 里出现了 {leaked}——凭据不该进子进程")

    #    ③ 出网 / 文件必须被拦（**拦不住就是"假沙箱"**，比没有更坏）
    probes = {
        "出网": "def probe():\n    import socket\n    return socket.gethostname()\n",
        "文件": "def probe():\n    return open('x.txt').read()\n",
    }
    for label, probe_source in probes.items():
        blocked = run_kat_in_sandbox(
            [FunctionRequest(name="probe", vectors=(TestVector(),))],
            source=probe_source,
            env=sandbox_env,
        )
        if blocked.ok:
            failures.append(f"[沙盒] {label}竟然没被拦：{probe_source!r}")
        elif "沙盒" not in " ".join(blocked.failures):
            failures.append(f"[沙盒] {label}被拦了，但拒因没说是沙盒：{blocked.failures}")

    #    ④ 超时必须**如实**报成"没结论"，而不是静默通过
    sleepy = run_kat_in_sandbox(
        [FunctionRequest(name="sleepy", vectors=(TestVector(),))],
        source="def sleepy():\n    import time\n    time.sleep(30)\n",
        env=sandbox_env,
        timeout=1.0,
    )
    if sleepy.ok or "超时" not in " ".join(sleepy.failures):
        failures.append(f"[沙盒] 超时没有被如实报出：{sleepy.failures}")

    #    ⑤ 元护栏：期望值写错 → **沙盒里也必须判不过**（别把"跑起来了"当成"对上了"）
    wrong = run_kat_in_sandbox(
        [
            FunctionRequest(
                name="sign_kat",
                args=("payload",),
                vectors=(TestVector(args=("abc",), expect="0" * 32),),
            )
        ],
        source=crypto_source,
        env=sandbox_env,
    )
    if wrong.ok:
        failures.append("[元护栏] 沙盒里期望值写错竟然判过——沙盒只证明'跑了'，不证明'对了'")

    print("=" * 66)
    if failures:
        print(f"debugtalk_draft 自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("debugtalk 草稿自检全部通过（§5.9，**只呈现不落地**）：")
    print("  往返  ：判据用**内核正则本身**——`${fn($a)}` 认，`${fn('a')}` **不认**（静默地雷）")
    print("  向量  ：**没有已知答案测试就拒绝交付**（并说明原因）")
    print("  干跑  ：**真的执行**函数、比期望输出（期望改错 → 必拒，元护栏钉住）")
    print("  追加  ：已有 `debugtalk.py` **逐字保留**，只出追加 diff（绝不整文件覆盖）")
    print("  重名  ：只提示不改写（仍是采纳，但要求人先看已有的那份）")
    print("=" * 66)
    return 0


def main() -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    # ★沙盒子进程入口：**必须在 argparse 之前**分流（子进程不接受任何 CLI 参数，
    #   它的输入只有 stdin 那一份载荷）。
    if SANDBOX_WORKER_FLAG in sys.argv[1:]:
        return _kat_worker()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="debugtalk 函数草稿（P2b）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "DRAFT_BANNER",
    "INPROCESS_MODE",
    "REFUSE_BAD_VECTOR",
    "REFUSE_DUPLICATE",
    "REFUSE_NO_VECTOR",
    "REFUSE_QUOTE_IN_ARGS",
    "ROUNDTRIP_TEMPLATE",
    "SANDBOX_DENIED_MODULES",
    "SANDBOX_MODE",
    "SANDBOX_SENTINEL",
    "SANDBOX_TIMEOUT_SECONDS",
    "DraftDecision",
    "DraftResult",
    "FunctionRequest",
    "SandboxRun",
    "TestVector",
    "append_to_existing",
    "check_request_roundtrip",
    "decide",
    "draft_debugtalk",
    "dry_run",
    "existing_function_names",
    "load_requests",
    "render_diff",
    "run_kat_in_sandbox",
    "render_draft_block",
    "render_draft_report",
    "roundtrip_check",
    "run_selftest",
]


if __name__ == "__main__":
    raise SystemExit(main())
