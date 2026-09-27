# -*- coding: utf-8 -*-
r"""jobs —— **触发运行**（v8 §2.5 行 339 / §5.8 行 1002 的 P3b-2；2026-09-25）。

## 这是本包**唯一**会执行代码的部分

P3a 的看板、P3b-1 的确认门都是"只读 / 只记录"，本模块**真的会跑用例**。
所以它必须被单独对待：

| 纪律 | 做法 |
| --- | --- |
| **登记为写入者** | 写入根 `.ai/jobs/`（T18 注册表）——与 `web.py`/`confirm.py` **刻意不登记**相反 |
| **闸门在子进程之前** | ★`check_run_allowed` 判完，才建目录、才起进程 |
| **子进程而非进程内 pytest** | 见下 |
| **串行 + 目录隔离** | 同一时刻一个作业；每作业一个 `.ai/jobs/<job_id>/` |

## ★为什么是**子进程**（§2.5 表格第 3 条）

1. **嵌套 pytest 脆弱**：看板是常驻进程，在里面跑 `pytest.main` 会让
   "看板崩了"与"用例失败了"纠缠在一起；
2. **`make.py::pytest_files_run_set` 是模块级全局**（实测定义在模块顶层），
   跨调用**累积**——同进程里跑第二次会被第一次的残留影响；
3. **挂死 / 崩溃不该拖垮看板**：子进程有超时，看板只需 Kill 它。

## ★为什么不复用 `workdir.run_isolated`

它的契约是「作业**只许**写作业目录，工作树前后**逐字节一致**」——那是
**L2 生成期**的契约（生成过程不许污染仓库）。而**触发运行**的产物
（`logs/*.summary.json`、`reports/report.html`）**本来就该落盘**：套用那个断言，
每次**正常运行**都会被判成"污染"。

所以本模块只沿用它的**思想**（隔离目录 + 串行 + 残留可查），契约各自写清——
**同一个机制不该被套在两件契约相反的事上**。

## ★三道闸门（顺序不能反）

1. **运行域**：只允许 `cases/` 下的 `*.yml` / `*.yaml`，且**不许**以 `-` 开头。
   这两条合起来堵住两件事：用网页跑**任意 pytest 目标**、用**参数注入**改 pytest 行为
   （例如塞一个 `--pyargs` 或 `-p` 进去）。★这条比 L3 更基础：
   L3 管"打哪个**地址**"，这条管"跑哪个**文件**"。
2. **L3 运行期冒烟**：`l3.check_l3_allowed` —— 与 CLI **同一个**闸门，
   "是网页点的"**不构成放宽理由**（§2.5 安全面 1 / R7）。
3. **超时有界**：不接受"跑到天荒地老"。

## ★命令形态

    [sys.executable, "-m", "interfacetester.cli", "run", *paths]

用 `sys.executable` 而不是 `hrun`：后者是安装出来的入口脚本，**不保证在 PATH 里**
（客户环境常见）。复用内核 CLI 的语义是 §2.5 的要求——
"Web 与 CLI 是**同一个 Core** 的两个前端，绝不允许 Web 里出现另一套校验"。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

from interfacetester_ai.workdir import new_job_id

# 登记在册的写入根（相对工作区根；T18 注册表 jobs → ("roots", (".ai/",))）
JOBS_DIR = ".ai/jobs"

# 允许的运行域（用例形态）——★Web 触发的运行**只能**跑这里面的东西
CASE_ROOTS: Tuple[Text, ...] = ("cases",)
CASE_SUFFIXES: Tuple[Text, ...] = (".yml", ".yaml")

# 作业号前缀（复用 `workdir.new_job_id` 的形态）
RUN_PREFIX = "run"

# 超时（秒）：默认 5 分钟，上限 1 小时
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 3600

# 报告里保留的 stdout / stderr 尾部长度（★截尾要**说明**，不假装是全文）
LOG_TAIL = 8000

# ★拒因码（每条都要能直接打在页面上）
REFUSE_NO_PATH = "no_path"
REFUSE_BAD_PATH = "bad_path"
REFUSE_ARG_INJECTION = "arg_injection"
REFUSE_L3 = "l3_refused"
REFUSE_BAD_TIMEOUT = "bad_timeout"

# ★串行锁：**同一时刻只允许一个触发运行**。
# NOTICE：它与 `workdir` 的作业锁**刻意分开**——两类作业的产物域不同
# （L2 生成写 `.ai/work/`，触发运行写 `logs/`），而"必须串行"的只是**各自内部**。
# 真正会互相踩的是**两次触发运行**：它们会写**同一份** `logs/<case>.summary.json`。
_SERIAL_LOCK = threading.Lock()


@dataclass(frozen=True)
class JobRequest:
    """一次触发运行的请求。"""

    paths: Tuple[Text, ...] = ()
    base_url: Text = ""  # ★交给 L3 闸门判的地址
    label: Text = ""
    timeout: int = DEFAULT_TIMEOUT
    triggered_by: Text = ""  # ★谁触发的（留痕纪律：触发也是"人工动作"）

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "paths": list(self.paths),
            "base_url": self.base_url,
            "label": self.label,
            "timeout": self.timeout,
            "triggered_by": self.triggered_by,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "JobRequest":
        if not isinstance(payload, dict):
            return cls()
        raw = payload.get("paths")
        paths: Tuple[Text, ...] = ()
        if isinstance(raw, list):
            paths = tuple(str(item) for item in raw)
        elif isinstance(raw, str) and raw.strip():
            paths = (raw.strip(),)
        timeout = payload.get("timeout", DEFAULT_TIMEOUT)
        return cls(
            paths=paths,
            base_url=str(payload.get("base_url", "") or ""),
            label=str(payload.get("label", "") or ""),
            timeout=int(timeout) if isinstance(timeout, int) else DEFAULT_TIMEOUT,
            triggered_by=str(payload.get("triggered_by", "") or ""),
        )


@dataclass
class JobResult:
    """一次触发运行的结果。★**被拒也要能说清**（`refused=True` + 拒因）。"""

    job_id: Text = ""
    ok: bool = False
    refused: bool = False  # ★是否**没有执行**（闸门拒了）
    code: Text = ""
    reason: Text = ""
    l3_reason: Text = ""  # L3 闸门的结论原文
    cmd: Tuple[Text, ...] = ()
    exit_code: Optional[int] = None
    started_at: Text = ""
    finished_at: Text = ""
    duration: float = 0.0
    job_dir: Text = ""
    stdout: Text = ""
    stderr: Text = ""
    timeout_hit: bool = False
    triggered_by: Text = ""  # ★谁触发的（**被拒时也要记**——"谁试过"同样有价值）

    def status_text(self) -> Text:
        """人话状态。★被拒 ≠ 失败（前者是**结论**，后者是执行结果）。"""
        if self.refused:
            return "已拒绝（未执行）"
        if self.timeout_hit:
            return "超时"
        return "成功" if self.ok else "失败"

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "job_id": self.job_id,
            "ok": self.ok,
            "refused": self.refused,
            "code": self.code,
            "reason": self.reason,
            "l3_reason": self.l3_reason,
            "cmd": list(self.cmd),
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": self.duration,
            "job_dir": self.job_dir,
            "timeout_hit": self.timeout_hit,
            "triggered_by": self.triggered_by,
            "status": self.status_text(),
        }


# ---------------------------------------------------------------------------
# ★闸门（必须在创建子进程**之前**）
# ---------------------------------------------------------------------------

def case_path_ok(path: Text) -> Tuple[bool, Text]:
    """`path` 是否落在允许的运行域内。返回 `(是否允许, 理由)`。

    ★两条判据缺一不可：

    - **运行域**：`realpath` 之后必须仍在 `cases/` 里（用 `commonpath`，**不是**
      `startswith`——`cases_evil/` 不是 `cases/`）。`realpath` 顺带把
      "用符号链接指到域外"也拦住了（链接解析后就不在 `cases/` 里了）；
    - **不是参数**：以 `-` 开头的**一律拒**。pytest 把 `-` 开头的当**选项**，
      放过去就等于"用网页给 pytest 传参数"——`--pyargs` 能跑到任意包、
      `-p` 能加载任意插件，那是**任意代码执行**。
    """
    raw = (path or "").strip()
    if not raw:
        return False, "路径为空"

    if raw.startswith("-"):
        return False, (
            f"`{raw}` 以 `-` 开头：那是 **pytest 选项**，不是用例路径。"
            "放它过去等于让网页能给 pytest 传参数（`--pyargs` / `-p` 都能扩大执行面）"
        )

    if not raw.lower().endswith(CASE_SUFFIXES):
        return False, f"`{raw}` 不是用例文件（只接受 {' / '.join(CASE_SUFFIXES)}）"

    target = os.path.realpath(raw)
    for root in CASE_ROOTS:
        root_abs = os.path.realpath(root)
        try:
            if os.path.commonpath([root_abs, target]) == root_abs:
                return True, ""
        except ValueError:  # 不同盘符（Windows）
            continue
    return False, (
        f"`{raw}` 不在允许的运行域内（{'、'.join(CASE_ROOTS)}/）——"
        "Web 触发的运行只跑用例文件，不接受任意目标"
    )


def check_run_allowed(
    request: JobRequest, env: Optional[Dict[Text, Text]] = None
) -> Tuple[bool, Text, Text]:
    """`(是否允许, 拒因码, 理由)`。★**必须在创建子进程之前**调用。

    ★为什么返回"拒因码"而不是只有一句人话：页面与日志要能**按码分类**
    （"闸门拒了多少次、都因为什么"是可度量的），只有人话时这件事做不到。
    """
    if not request.paths:
        return False, REFUSE_NO_PATH, "没有要跑的用例路径——空作业不执行（跑什么？）"

    for path in request.paths:
        ok, why = case_path_ok(path)
        if ok:
            continue
        code = (
            REFUSE_ARG_INJECTION if path.strip().startswith("-") else REFUSE_BAD_PATH
        )
        return False, code, why

    if request.timeout <= 0 or request.timeout > MAX_TIMEOUT:
        return False, REFUSE_BAD_TIMEOUT, (
            f"timeout={request.timeout} 不在 (0, {MAX_TIMEOUT}] 内——"
            "不接受「跑到天荒地老」的作业"
        )

    # ★★ L3：与 CLI **同一个**闸门。"是网页点的"**不构成放宽理由**（§2.5 安全面 1 / R7）
    from interfacetester_ai.l3 import check_l3_allowed  # noqa: PLC0415

    allowed, l3_reason = check_l3_allowed(request.base_url, env)
    if not allowed:
        return False, REFUSE_L3, l3_reason
    return True, "", l3_reason


def build_cmd(request: JobRequest) -> Tuple[Text, ...]:
    """构造子进程命令。★`sys.executable -m interfacetester.cli run …`。

    不用 `hrun`：它是安装出来的入口脚本，**不保证在 PATH 里**（客户环境常见）。
    """
    import sys  # noqa: PLC0415

    return (sys.executable, "-m", "interfacetester.cli", "run", *request.paths)


# ---------------------------------------------------------------------------
# 执行（★闸门在前；写盘只在 `.ai/jobs/`）
# ---------------------------------------------------------------------------

def _stamp() -> Text:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def run_job(
    request: JobRequest,
    *,
    env: Optional[Dict[Text, Text]] = None,
    dry_run: bool = False,
) -> JobResult:
    """执行一次触发运行。

    ★顺序（**前三步都是前置，任何一步不过就绝不产生副作用**）：
    ① `check_run_allowed`（运行域 + L3 + 超时）→ ② 拿**串行锁** → ③ 建作业目录；
    **然后**才起子进程。

    ★被拒时**不建目录、不起进程**，只返回 `refused=True` + 拒因——
    "拒绝"必须发生在**任何副作用之前**。否则"拒了但还是留了个空目录"，
    会让人以为跑过了（本仓最忌讳的就是这种形态）。

    ★`dry_run=True`：**只到闸门为止**——能回答"如果真跑，会跑什么、闸门放不放"，
    但一行都不执行。它也是本模块自检的主力（自检不真起子进程）。
    """
    result = JobResult()
    result.triggered_by = request.triggered_by

    allowed, code, reason = check_run_allowed(request, env)
    if not allowed:
        result.refused = True
        result.code = code
        result.reason = reason
        result.l3_reason = reason if code == REFUSE_L3 else ""
        return result

    result.l3_reason = reason  # 放行时，reason 就是 L3 闸门的结论原文
    result.job_id = new_job_id(RUN_PREFIX)
    result.cmd = build_cmd(request)
    result.started_at = _stamp()

    if dry_run:
        result.ok = True
        result.reason = "dry-run：闸门通过，**未执行**"
        result.finished_at = _stamp()
        return result

    with _SERIAL_LOCK:
        # ★写入根写成**内联字面量前缀**（T18 纪律：目标必须可静态判定；
        #   中间变量目标会被判「不可静态判定」从严违规）——与 `pending.py` / `reviews.py` 同款
        os.makedirs(".ai/jobs/" + result.job_id, exist_ok=True)
        result.job_dir = JOBS_DIR + "/" + result.job_id

        started = time.time()
        try:
            completed = subprocess.run(
                list(result.cmd),
                capture_output=True,
                timeout=request.timeout,
                check=False,
            )
            result.exit_code = completed.returncode
            result.ok = completed.returncode == 0
            result.stdout = completed.stdout.decode("utf-8", "replace")
            result.stderr = completed.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired as expired:
            # ★超时**不是"没结论"**：它是"这个作业没能按时给出结论"——
            #   要显式落盘（否则看板上它会和"还在跑"混在一起）
            result.timeout_hit = True
            result.reason = f"超时（>{request.timeout}s），子进程已被终止"
            result.stdout = (expired.stdout or b"").decode("utf-8", "replace")
            result.stderr = (expired.stderr or b"").decode("utf-8", "replace")
        except OSError as error:
            result.reason = f"无法启动子进程：{type(error).__name__}: {error}"
            result.stderr = result.reason

        result.duration = time.time() - started
        result.finished_at = _stamp()
        write_job_record(result)
    return result


def write_job_record(result: JobResult) -> Text:
    """把作业结果落 `.ai/jobs/<job_id>/result.json`（**原子写**）。返回相对路径。

    ★为什么**退出码也要落盘**（而不是只打在终端）：R8 要求页面**显式展示退出码**，
    而"看板进程重启后还能查"才叫**证据**——只打在终端上的东西活不过一次重启。

    ★stdout / stderr **截尾**（各留最后 `LOG_TAIL` 字符）并**显式标注**
    `*_truncated`：截尾本身没问题，**不说明**才成问题（人会以为看的是全文）。
    """
    payload = result.to_dict()
    payload["stdout"] = result.stdout[-LOG_TAIL:]
    payload["stderr"] = result.stderr[-LOG_TAIL:]
    payload["stdout_truncated"] = len(result.stdout) > LOG_TAIL
    payload["stderr_truncated"] = len(result.stderr) > LOG_TAIL
    payload["note"] = "本文件是**证据**（跑什么、退出码、耗时、L3 结论）——刻意不被忽略"

    # ★内联字面量前缀（T18 静态可判）+ 原子写（先 `.tmp` 再 `os.replace`）
    #   ——与 `pending.py` / `reviews.py` 完全同款
    os.makedirs(".ai/jobs/" + result.job_id, exist_ok=True)
    with open(
        ".ai/jobs/" + result.job_id + "/result.json.tmp", "w", encoding="utf-8"
    ) as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    os.replace(
        ".ai/jobs/" + result.job_id + "/result.json.tmp",
        ".ai/jobs/" + result.job_id + "/result.json",
    )
    return JOBS_DIR + "/" + result.job_id + "/result.json"


def list_jobs() -> List[Dict[Text, Any]]:
    """列 `.ai/jobs/*/result.json`（**只读**），按作业号**倒序**。

    ★作业号形态是 `run-YYYYmmdd-HHMMSS-<pid>-<nnnn>`，所以**字典序 = 时间序**
    （`workdir.new_job_id` 的 docstring 把这条写成设计目标）。
    """
    if not os.path.isdir(JOBS_DIR):
        return []
    found: List[Dict[Text, Any]] = []
    for name in sorted(os.listdir(JOBS_DIR), reverse=True):
        if not os.path.isdir(os.path.join(JOBS_DIR, name)):
            continue
        entry: Dict[Text, Any]
        try:
            with open(JOBS_DIR + "/" + name + "/result.json", encoding="utf-8") as fp:
                entry = json.load(fp)
        except FileNotFoundError:
            # ★没有 `result.json` 的作业目录**不跳过**：它意味着
            #   "跑了一半 / 被中断"——那**恰恰**是最需要被看见的状态
            entry = {
                "job_id": name,
                "status": "无 result.json（可能被中断）",
                "ok": False,
                "refused": False,
            }
        except (OSError, ValueError) as error:
            entry = {"job_id": name, "error": f"{type(error).__name__}: {error}"}
        if not isinstance(entry, dict):
            entry = {"job_id": name, "error": "`result.json` 不是对象"}
        found.append(entry)
    return found


def read_job(job_id: Text) -> Optional[Dict[Text, Any]]:
    """读单个作业的结果（**只读**；不存在 → `None`）。

    ★`job_id` 来自 URL，所以先做**形态校验**（只许 `[\\w.-]`）：它要被拼进路径，
    放手就等于开了一道目录穿越。这是与 `web.py` 读根白名单**互相独立**的第二层
    ——两层都按"自己足够"来写，不靠对方兜着。
    """
    if not re.match(r"^[\w.\-]+$", job_id or ""):
        return None
    for entry in list_jobs():
        if str(entry.get("job_id")) == job_id:
            return entry
    return None


def render_job_report(result: JobResult) -> Text:
    """把一次运行渲染成人看的报告（**纯文本，不写盘**）。"""
    lines = [
        "# 触发运行报告（§5.8 P3b-2）",
        "",
        f"- 作业号：`{result.job_id or '(未分配)'}`",
        f"- 状态：**{result.status_text()}**",
    ]

    if result.refused:
        lines.append(f"- 拒因码：`{result.code}`")
        lines.append(f"- 拒因：{result.reason}")
        lines.append("")
        lines.append(
            "> **没有执行**：闸门在**任何副作用之前**就拒了——没建作业目录、没起子进程。"
        )
    else:
        lines.append(f"- 命令：`{' '.join(result.cmd)}`")
        lines.append(f"- 退出码：**{result.exit_code}**")
        lines.append(
            f"- 耗时：{result.duration:.2f}s（{result.started_at} → {result.finished_at}）"
        )
        if result.l3_reason:
            lines.append(f"- L3 闸门：{result.l3_reason}")
        if result.job_dir:
            lines.append(f"- 作业目录：`{result.job_dir}`")
        if result.timeout_hit:
            lines.append("- ★**超时**：子进程已被终止（这不是「没结论」，是「没按时给出结论」）")
        if result.reason and not result.timeout_hit:
            lines.append(f"- 说明：{result.reason}")

    if result.stdout:
        lines += [
            "",
            f"## stdout（尾部 {LOG_TAIL} 字符）",
            "",
            "```",
            result.stdout[-LOG_TAIL:].rstrip(),
            "```",
        ]
    if result.stderr:
        lines += [
            "",
            f"## stderr（尾部 {LOG_TAIL} 字符）",
            "",
            "```",
            result.stderr[-LOG_TAIL:].rstrip(),
            "```",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 自检（★**不真起子进程**：闸门判据 + `dry_run` 就够把安全面覆盖住）
# ---------------------------------------------------------------------------

def run_selftest(verbose: bool = False) -> int:
    """jobs 自检。★**不执行任何用例**（也不需要 `cases/` 存在）。

    自检的覆盖面刻意压在**闸门**上：这个模块的风险全在"什么会被放行"，
    而不在"放行之后跑得对不对"（后者是内核 CLI 的职责，已在别处验过）。
    真正起子进程的路径放在 `tests/jobs_test.py`（那需要真夹具）。
    """
    from unittest import mock  # noqa: PLC0415

    failures: List[Text] = []
    notes: List[Text] = []

    # ===== ① 运行域：只跑 cases/ 下的 *.yml ==============================
    for good in ("cases/login_flow.yml", "cases/sub/a.yaml", r"cases\win.yml"):
        ok, why = case_path_ok(good)
        if not ok:
            failures.append(f"[运行域] `{good}` 应当被允许：{why}")

    for bad, expect in (
        ("../etc/passwd.yml", False),
        ("cases_evil/x.yml", False),  # ★前缀混淆：cases_evil 不是 cases 的子目录
        ("cases/x.py", False),  # 不是用例后缀
        ("/etc/passwd.yml", False),
        ("", False),
        ("cases/run_all.txt", False),
    ):
        ok, why = case_path_ok(bad)
        if ok is not expect:
            failures.append(f"[运行域] `{bad}` 结论不对（应当 {'放行' if expect else '拒绝'}）：{why}")

    # ===== ② ★参数注入：`-` 开头一律拒（`--pyargs` / `-p` 能扩大执行面）====
    for injected in ("--pyargs", "-p", "--rootdir=/", "-k"):
        ok, why = case_path_ok(injected)
        if ok:
            failures.append(f"[参数注入] `{injected}` 竟然被当成用例路径放行了")
        if "选项" not in why:
            failures.append(f"[参数注入] `{injected}` 的拒绝理由没点明它是选项：{why}")

    # ===== ③ 闸门总口径：拒因码 + L3 ====================================
    allowed, code, reason = check_run_allowed(JobRequest())
    if allowed or code != REFUSE_NO_PATH:
        failures.append(f"[闸门] 空路径应当以 `{REFUSE_NO_PATH}` 拒绝：{code}")

    allowed, code, _reason = check_run_allowed(JobRequest(paths=("cases/x.yml",)))
    if allowed or code != REFUSE_L3:
        failures.append(f"[闸门] 沙盒未开时应当以 `{REFUSE_L3}` 拒绝：{code}")

    allowed, code, reason = check_run_allowed(JobRequest(paths=("-p",)))
    if allowed or code != REFUSE_ARG_INJECTION:
        failures.append(f"[闸门] 参数注入的拒因码应当是 `{REFUSE_ARG_INJECTION}`：{code}")

    for bad_timeout in (0, -1, MAX_TIMEOUT + 1):
        allowed, code, _reason = check_run_allowed(
            JobRequest(paths=("cases/x.yml",), timeout=bad_timeout)
        )
        if allowed or code != REFUSE_BAD_TIMEOUT:
            failures.append(f"[闸门] timeout={bad_timeout} 应当拒绝：{code}")

    # ===== ④ ★「拒绝」必须发生在**任何副作用之前** ======================
    #   判据比"我没写"硬：查的是**事实**——被拒时连作业号都不该分配
    #   （分配了就会在别处被当成"跑过"）。
    refused = run_job(JobRequest(paths=("cases/x.yml",)))
    if not refused.refused:
        failures.append(f"[副作用] 沙盒未开时应当被拒：{refused.to_dict()}")
    if refused.job_dir or refused.job_id or refused.cmd:
        failures.append(f"[副作用] 被拒时不该分配作业号/目录/命令：{refused.to_dict()}")

    # ★真判据（查**事实**，不是查声明）：被拒前后 `.ai/jobs/` 的目录集合必须**不变**
    before_dirs = set(os.listdir(JOBS_DIR)) if os.path.isdir(JOBS_DIR) else set()
    run_job(JobRequest(paths=("cases/x.yml",)))
    after_dirs = set(os.listdir(JOBS_DIR)) if os.path.isdir(JOBS_DIR) else set()
    if after_dirs != before_dirs:
        failures.append(
            "[副作用] 被拒之后 `.ai/jobs/` 多出了东西："
            f"{sorted(after_dirs - before_dirs)}"
        )

    injected = run_job(JobRequest(paths=("--pyargs",)))
    if not injected.refused or injected.code != REFUSE_ARG_INJECTION:
        failures.append(f"[副作用] 参数注入应当被拒：{injected.to_dict()}")

    # ===== ⑤ dry-run：闸门通过但**一行都不执行** ========================
    ok_env = {"INTERFACETESTER_AI_SANDBOX": "on"}
    request = JobRequest(paths=("cases/x.yml",), base_url="http://127.0.0.1:80")

    allowed, code, reason = check_run_allowed(request, ok_env)
    if not allowed:
        failures.append(f"[dry-run] 回环 + 沙盒开启应当放行：{code} / {reason}")

    dry = run_job(request, env=ok_env, dry_run=True)
    if dry.refused or not dry.ok:
        failures.append(f"[dry-run] 应当通过闸门：{dry.to_dict()}")
    if "未执行" not in dry.reason:
        failures.append(f"[dry-run] 说明里必须点明「未执行」：{dry.reason}")
    if dry.job_dir or dry.exit_code is not None:
        failures.append("[dry-run] 不该建作业目录、不该有退出码（dry-run 就是「不跑」）")

    # ===== ⑥ ★元护栏：L3 必须是**同一个**闸门（不是另写一份）===========
    #   手法：patch `l3.check_l3_allowed` → 本模块的结论**必须**跟着变。
    #   若不变，说明这里另写了一套判断——那正是 §2.5 明令禁止的
    #   「Web 里出现另一套校验」。
    import interfacetester_ai.l3 as l3_module  # noqa: PLC0415

    with mock.patch.object(
        l3_module, "check_l3_allowed", lambda url, env=None: (True, "自检放行")
    ):
        allowed, code, reason = check_run_allowed(
            JobRequest(paths=("cases/x.yml",), base_url="http://example.com")
        )
        if not allowed:
            failures.append(
                f"[元护栏] patch 掉 L3 闸门后仍被拒——说明用的不是同一个闸门：{code}"
            )
        elif "自检放行" not in reason:
            failures.append(f"[元护栏] L3 的结论原文没有透传出来：{reason}")

    # ===== ⑦ 命令形态 + 只读接口 =========================================
    cmd = build_cmd(JobRequest(paths=("cases/a.yml", "cases/b.yml")))
    if len(cmd) != 6 or cmd[1:4] != ("-m", "interfacetester.cli", "run"):
        failures.append(f"[命令] 形态不对：{cmd}")
    if cmd[-2:] != ("cases/a.yml", "cases/b.yml"):
        failures.append(f"[命令] 路径没有按序放在末尾：{cmd}")

    for weird in ("../x", "a/b", "", "x y"):
        if read_job(weird) is not None:
            failures.append(f"[job_id 形态] `{weird}` 不该被当作合法作业号")

    have_jobs = os.path.isdir(JOBS_DIR)
    if not have_jobs and list_jobs():
        failures.append("[只读] 目录不存在时返回了内容？")
    notes.append(
        f"`.ai/jobs/` {'存在：' + str(len(list_jobs())) + ' 个作业' if have_jobs else '不存在（尚未触发过真运行）'}"
    )

    # ===== ⑧ 报告渲染：被拒与执行两种形态都要能读 =========================
    refused_text = render_job_report(
        JobResult(refused=True, code=REFUSE_L3, reason="沙盒未开")
    )
    if "没有执行" not in refused_text or REFUSE_L3 not in refused_text:
        failures.append("[报告] 被拒的报告没写清「没有执行」/ 拒因码")

    done_text = render_job_report(JobResult(job_id="run-1", ok=True, exit_code=0))
    if "退出码：**0**" not in done_text:
        failures.append("[报告] 执行的报告必须**显式展示退出码**（R8）")

    for note in notes:
        print(f"[jobs 自检] 说明：{note}")

    print("=" * 66)
    if failures:
        print(f"触发运行自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("触发运行自检全部通过（§5.8 行 1002，**P3b-2**）：")
    print("  ★闸门在**子进程之前**：运行域 → L3 → 超时；被拒时**零副作用**")
    print("          （不建目录、不起进程、连作业号都不分配）")
    print("  运行域：只跑 cases/ 下的 *.yml / *.yaml（commonpath 判，`cases_evil/` 拦得住）")
    print("  参数注入：`-` 开头**一律拒**（`--pyargs` / `-p` 能扩大执行面）")
    print("  ★L3 是**同一个**闸门：元护栏证明 patch 它、结论就跟着变")
    print("          （§2.5：Web 与 CLI 是同一个 Core，不许另写一套校验）")
    print("  子进程：`sys.executable -m interfacetester.cli run …`（不用 PATH 里的 hrun）")
    print("  串行  ：同一时刻一个作业；每作业一个 `.ai/jobs/<job_id>/`")
    print("  证据  ：`result.json` 落**退出码 + L3 结论 + 耗时**（截尾显式标注）")
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

    parser = argparse.ArgumentParser(description="触发运行自检（P3b-2）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "CASE_ROOTS",
    "CASE_SUFFIXES",
    "DEFAULT_TIMEOUT",
    "JOBS_DIR",
    "LOG_TAIL",
    "MAX_TIMEOUT",
    "REFUSE_ARG_INJECTION",
    "REFUSE_BAD_PATH",
    "REFUSE_BAD_TIMEOUT",
    "REFUSE_L3",
    "REFUSE_NO_PATH",
    "RUN_PREFIX",
    "JobRequest",
    "JobResult",
    "build_cmd",
    "case_path_ok",
    "check_run_allowed",
    "list_jobs",
    "read_job",
    "render_job_report",
    "run_job",
    "run_selftest",
    "write_job_record",
]


if __name__ == "__main__":
    raise SystemExit(main())





