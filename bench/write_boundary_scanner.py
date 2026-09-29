# -*- coding: utf-8 -*-
"""红线③ 写盘白名单静态扫描器（v7 §0.4-② 修订口径；v8 §十 T18 落地）。

## 为什么需要这个文件

v6 原文的红线③ 是「LLM 模块里禁止出现 `open(` / `Path.write`（静态扫描 + 用例）」，
但 §6 自己要求缓存「命中即回放」写 `.ai/cache/`、审计「每次调用落
`.ai/manifest/…json``——这两件事**必然发生在 AI 接入层**，按 v6 口径这条机器
检查写出来会把自己的缓存/审计判违规（v7 §0.4-② 实测结论）。

所以红线③ 的可执行形态是**按「写入目标路径 + 模块白名单」**判定：

  ① 谁都不许把 LLM 产物直接写进 `cases/`（唯一入口是装配器）；
  ② `llm.py`（模型响应处理层）一律不写盘——模型响应只以内存结构向上返回（§6）；
  ③ `cache.py` / `manifest.py` / `pending.py` 是**登记在册**的写入者，写入根固定 `.ai/`；
  ④ 未登记模块：任何写入类调用都违规（白名单默认禁止，§6「写盘边界」）。

## 口径边界（诚实说明）

- 只判**写入类调用**（创建/修改文件内容或目录）：`open`（mode 含 w/a/x/+）、
  `Path.write_text / write_bytes / mkdir`、`os.mkdir / makedirs`、
  `shutil.copy / copy2 / copytree / move`、`os.rename / replace / symlink`。
- **删除类**（`remove` / `unlink` / `rmtree`）**不纳入静态口径**：运行期删除越界
  由 T8 的「工作区目录快照 diff」承担（§十 T8）；静态拦清理代码只会教人无视红线。
- `open` 只读形态（mode 缺省 / 纯 r / rb）不判——红线③ 管的是**写**，不是读。
- 写入目标**不可静态判定**（变量 / `os.path.join(...)` 调用）时**从严判违规**：
  白名单纪律下，「目标写哪儿运行期才知道」本身就是需要人工审的形态。
  登记在册的写入者因此必须把路径前缀写成**源码可见形态**（字面量 / f-string /
  字符串拼接的最左前缀）。
- 目标前缀按**路径段**对齐：`.ai` 允许根匹配 `.ai/…` 或恰好 `.ai`，
  不会误放行 `.aix/…`；分隔符统一按 `/` 归一（同时接受 `\\`）。

## 判据表（模块白名单注册表）

`MODULE_WRITE_BOUNDARIES` 与 §6「写盘边界」行一一对应，可被测试对账
（文档 ↔ 注册表 ↔ 违规/合规夹具三方对账，同 §3.5 元护栏的取向）。
`bench/` 是工作台而非生产包，其既有写盘点登记在 `BENCH_KNOWN_WRITES`
（审计工具的沙箱写，目标为 `.tmp_golden/` 内临时文件）——多一条新写盘、
少一条登记，对账都会红。

## 用法

    python bench/write_boundary_scanner.py     # 自检：夹具 + bench/ + interfacetester_ai/
    from bench.write_boundary_scanner import scan_tree   # CI / 测试复用
"""

from __future__ import annotations

import ast
import os
import sys
from typing import Any, Dict, List, NamedTuple, Optional, Text, Tuple

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)


# ---------------------------------------------------------------------------
# 判据表（§6「写盘边界」↔ 注册表，唯一来源）
# ---------------------------------------------------------------------------

# 写入类调用 → 目标参数位置
#   "receiver" = 方法接收者（Path.write_text / Path("x").mkdir）
#   "auto_mkdir" = 双形态：os.mkdir(x) 取 args[0]；Path 实例 .mkdir() 取接收者
#   数字 = 位置参数下标（copy(src, dst) 的目标是 args[1]）
WRITE_CALL_TARGET_ARG: Dict[Text, Any] = {
    "open": 0,
    "write_text": "receiver",
    "write_bytes": "receiver",
    "mkdir": "auto_mkdir",
    "makedirs": 0,
    "copy": 1,
    "copy2": 1,
    "copytree": 1,
    "move": 1,
    "rename": 1,
    "replace": 1,
    "symlink": 1,
}

# 同名异义限定：这些名字只有「限定形态」才算写盘调用——
#   os.replace / os.rename / os.symlink（str.replace 是字符串方法，同名异义）
#   shutil.copy / copy2 / copytree / move（dict.copy / set.copy 同名异义）
# 不满足限定形态的调用直接跳过，不判违规（避免把 x.replace(a, b) 报成写盘）。
_REQUIRES_QUALIFIER: Dict[Text, Text] = {
    "replace": "os",
    "rename": "os",
    "symlink": "os",
    "copy": "shutil",
    "copy2": "shutil",
    "copytree": "shutil",
    "move": "shutil",
}


def _qualified_as(call: ast.Call, qualifier: Text) -> bool:
    """调用是否呈 `qualifier.func(...)` 形态（如 os.replace / shutil.copy）。"""
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == qualifier
    )

# 模块名（文件名去扩展名）→ (规则, 允许根)
#   "forbid"：该模块一律不写盘（红线③-②）
#   "roots"  ：只许写列出的根下（红线③-③，前缀按路径段对齐）
# 未列出的模块 → 任何写入类调用都违规（红线③-④，白名单默认禁止）
MODULE_WRITE_BOUNDARIES: Dict[Text, Tuple[Text, Tuple[Text, ...]]] = {
    "llm": ("forbid", ()),
    "cache": ("roots", (".ai/",)),
    "manifest": ("roots", (".ai/",)),
    "pending": ("roots", (".ai/",)),  # S-6：PENDING 清单 writer（§3.4/§8.1 S-6）
    # T29：AI 层报告（reports/analysis.md、reports/doc_defects.md）——第四个登记写入者，
    # 登记本身即"扩白名单必须显式签字"的机器形态（未登记的 diagnosis.py 曾实测被判 2 处违规）
    "diagnosis": ("roots", ("reports/",)),
    # T27（S-10）：文档体检的缺项清单 reports/NEEDS_DOC_FIX.md——第五个登记写入者
    "doc_quality": ("roots", ("reports/",)),
    # T8：**L2 作业隔离目录**（`.ai/work/<job_id>/`）。实现只碰 `.ai/work/`，
    # 但扫描器按**首前缀**判定（`os.path.join(".ai", ...)` → 前缀 `.ai`），
    # 所以登记根写 `.ai/`——"实际只写子目录"由 workdir.py 自己的形态纪律保证。
    "workdir": ("roots", (".ai/",)),
    "assembler": ("roots", ("cases/", ".ai/")),  # 装配器：cases/ 与 .ai/draft/（§6）
    # T24：人工确认留痕（`.ai/reviews/<case>.json`，**证据**——T12/P-7 刻意不忽略）
    "reviews": ("roots", (".ai/",)),
    # P1a：失败分析报告（`reports/analysis.md`。`doc_defects.md` 的写盘在 diagnosis 里）
    "analyze": ("roots", ("reports/",)),
    # P1b：结果摘要（`reports/summary.md`）
    "summary": ("roots", ("reports/",)),
    # P3b-2：**触发运行**作业（`.ai/jobs/<job_id>/result.json`）。
    # ★与 `web` / `confirm` 的对比是有意的：那两个**刻意不登记**（源码里没有写调用，
    # 落盘全委托给别处），而 `jobs` 会**真的执行 + 真的写盘**，所以**必须**登记。
    # 写入根与 `workdir` 同域（`.ai/`），但契约不同：`workdir` 是"作业只许写作业目录"，
    # 而触发运行的产物（`logs/`、`reports/`）**本来就该落盘**（见 `jobs.py` 的 docstring）。
    "jobs": ("roots", (".ai/",)),
    # P3d 收口：**离线静态导出**（`.ai/export/*.html`）。
    # ★与 `web` 的对比同样是有意的：看板**依旧零写盘**（不进本表），导出是**另一个能力**——
    #   它只会写 `.ai/export/` 这一个字面量前缀，所以 T18 能静态判定。
    "export": ("roots", (".ai/",)),
}

# bench/ 工作台的既有写盘点（(相对路径, 函数名) → 出现次数；对账口径——不含行号，
# 避免 benign 的行号漂移破坏对账；新增写盘 / 写盘点消失都会让计数对不上而红）
BENCH_KNOWN_WRITES: Dict[Tuple[Text, Text], int] = {
    ("ai_guardrails.py", "makedirs"): 1,  # 沙箱基目录（work_dir/.tmp_golden）
    ("ai_guardrails.py", "open"): 1,  # 沙箱临时用例（tempfile.mkstemp 产物）
    ("golden_harness.py", "makedirs"): 2,  # golden 回放沙箱目录（work_root / case_dir）
    ("golden_harness.py", "copy2"): 2,  # golden 目录整份复制（保持相对引用）
}




# ---------------------------------------------------------------------------
# 判定核心
# ---------------------------------------------------------------------------


class Violation(NamedTuple):
    """一处越界写盘点。"""

    path: Text  # 相对路径（/ 分隔）
    lineno: int
    func: Text  # 写入类调用名
    target: Text  # 静态可判前缀；不可判时为 "?"
    reason: Text


def _callee_name(call: ast.Call) -> Optional[Text]:
    """取调用名：open / write_text / makedirs / replace …（Name 或 Attribute）."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _open_mode(call: ast.Call) -> Text:
    """取 open() 的 mode（默认 'r'）。不可静态判定时返回 'w?'（从严按写处理）。"""
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        return str(call.args[1].value)
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    if any(kw.arg == "mode" for kw in call.keywords):
        return "w?"  # mode=变量 → 不可判，从严
    return "r"  # 缺省 mode = 只读


def _target_prefix(node: Optional[ast.AST]) -> Tuple[Optional[Text], bool]:
    """提取写盘目标的「最左静态前缀」。

    支持：字符串字面量 / f-string 首段常量 / `字面量 + 变量` 的左结合拼接 /
    构造调用首实参（`Path("cases")`、`os.path.join(".ai", …)`——根由首参决定）。
    其余形态（纯变量 / 下标）返回 (None, False) = 不可静态判定。
    """
    if node is None:
        return None, False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, True
    if isinstance(node, ast.JoinedStr):
        if (
            node.values
            and isinstance(node.values[0], ast.Constant)
            and isinstance(node.values[0].value, str)
        ):
            return node.values[0].value, True
        return None, False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _target_prefix(node.left)  # 左结合：前缀由最左操作数决定
    if isinstance(node, ast.Call):
        # 构造/拼接调用：根由首实参决定（Path("cases") / os.path.join(".ai", x)）
        if node.args:
            return _target_prefix(node.args[0])
        return None, False
    return None, False


def _root_matches(prefix: Text, root: Text) -> bool:
    """允许根与目标前缀按路径段对齐（'.ai' 放行 '.ai/x' 与 '.ai'，拒绝 '.aix'）。"""
    norm = prefix.replace("\\", "/").rstrip("/")
    r = root.replace("\\", "/").rstrip("/")
    return norm == r or norm.startswith(r + "/")


def _judge(
    module: Text,
    relpath: Text,
    lineno: int,
    func: Text,
    prefix: Optional[Text],
    decided: bool,
    registry: Optional[Dict[Text, Tuple[Text, Tuple[Text, ...]]]] = None,
) -> List[Violation]:
    """按注册表判定一个写盘点。返回违规列表（空 = 合规）。"""
    rules = MODULE_WRITE_BOUNDARIES if registry is None else registry
    rule = rules.get(module)
    shown = prefix if (prefix and decided) else "?"

    if rule is None:
        return [
            Violation(
                relpath, lineno, func, shown,
                "未登记模块出现写入类调用（白名单默认禁止，§6 写盘边界）",
            )
        ]
    kind, roots = rule
    if kind == "forbid":
        return [
            Violation(
                relpath, lineno, func, shown,
                f"{module}.py 一律不写盘（模型响应只以内存结构向上返回，§6）",
            )
        ]
    if not decided:
        return [
            Violation(
                relpath, lineno, func, shown,
                "写入目标不可静态判定——白名单下必须把路径前缀写成源码可见形态",
            )
        ]
    p = str(prefix).replace("\\", "/")
    for root in roots:
        if _root_matches(p, root):
            return []
    return [
        Violation(
            relpath, lineno, func, shown,
            f"越界写入：目标 {p} 不在允许根 {list(roots)} 内",
        )
    ]


def scan_source(
    source: Text,
    filename: Text,
    relpath: Optional[Text] = None,
    registry: Optional[Dict[Text, Tuple[Text, Tuple[Text, ...]]]] = None,
) -> List[Violation]:
    """扫描一段 Python 源码，返回全部越界写盘点。"""
    rel = relpath or filename
    module = os.path.splitext(os.path.basename(filename))[0]
    try:
        tree = ast.parse(source)
    except SyntaxError as ex:  # pragma: no cover - 夹具/被扫文件语法坏
        return [Violation(rel, getattr(ex, "lineno", 0) or 0, "<syntax-error>", "?",
                          f"源码不可解析：{ex}")]

    violations: List[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = _callee_name(node)
        if func is None or func not in WRITE_CALL_TARGET_ARG:
            continue
        if func in _REQUIRES_QUALIFIER and not _qualified_as(
            node, _REQUIRES_QUALIFIER[func]
        ):
            continue  # str.replace / dict.copy 等同名异义调用不判
        if func == "open":
            mode = _open_mode(node)
            if not any(c in mode for c in "wax+"):
                continue  # 只读 open 不判（红线③ 管写不管读）

        spec = WRITE_CALL_TARGET_ARG[func]
        target_node: Optional[ast.AST]
        if spec == "receiver":
            if not isinstance(node.func, ast.Attribute):
                continue
            target_node = node.func.value
        elif spec == "auto_mkdir":
            # 双形态：os.mkdir(x) → 目标 args[0]；Path 实例 .mkdir() → 目标接收者
            if _qualified_as(node, "os") or isinstance(node.func, ast.Name):
                target_node = node.args[0] if node.args else None
            elif isinstance(node.func, ast.Attribute):
                target_node = node.func.value
            else:
                continue
        else:
            idx = int(spec)
            if len(node.args) > idx:
                target_node = node.args[idx]
            else:  # 关键字形态（path=/dst=…）
                target_node = None
                for kw in node.keywords:
                    if kw.arg in ("dst", "path", "filename", "name", "dir"):
                        target_node = kw.value
                        break
        prefix, decided = _target_prefix(target_node)
        violations.extend(
            _judge(module, rel, node.lineno, func, prefix, decided, registry)
        )
    return violations


def scan_tree(
    root: Text,
    registry: Optional[Dict[Text, Tuple[Text, Tuple[Text, ...]]]] = None,
) -> List[Violation]:
    """递归扫描目录下全部 .py（跳过 __pycache__），返回越界写盘点。"""
    out: List[Violation] = []
    base = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fname in sorted(filenames):
            if not fname.endswith(".py"):
                continue
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, base).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8") as fp:  # 只读，不越红线
                    source = fp.read()
            except (OSError, UnicodeDecodeError):
                continue
            out.extend(scan_source(source, fname, rel, registry))
    return out


# ---------------------------------------------------------------------------
# 成对夹具（违规必红 / 合规必绿——同 §3.5 元护栏的取向）
# ---------------------------------------------------------------------------

# 合规组：注册表里每个登记模块各一份「写在自己允许根内」的样本 + 只读 llm + 无 IO 模块
SELFTEST_CLEAN: Dict[Text, Text] = {
    "llm.py": '''# llm 只读文件（读不管，红线③ 只管写）
def read_fewshot(path):
    with open(path, "rb") as fp:
        return fp.read()
''',
    "cache.py": '''import json
def put(key, doc):
    with open(".ai/cache/" + key + ".json", "w", encoding="utf-8") as fp:
        json.dump(doc, fp)
''',
    "manifest.py": '''from pathlib import Path
def dump(name, text):
    Path(".ai/manifest").mkdir(parents=True, exist_ok=True)
    Path(f".ai/manifest/{name}.json").write_text(text, encoding="utf-8")
''',
    "pending.py": '''import json
import os
def write_pending(case, items):
    os.makedirs(".ai/pending", exist_ok=True)
    with open(".ai/pending/" + case + ".pending.json", "w", encoding="utf-8") as fp:
        json.dump(items, fp, ensure_ascii=False, indent=2)
''',
    "diagnosis.py": '''import os
def write_doc_defects(text):
    os.makedirs("reports", exist_ok=True)
    with open("reports/doc_defects.md", "w", encoding="utf-8") as fp:
        fp.write(text)
''',
    "doc_quality.py": '''import os
def write_needs_doc_fix(text):
    os.makedirs("reports", exist_ok=True)
    with open("reports/NEEDS_DOC_FIX.md", "w", encoding="utf-8") as fp:
        fp.write(text)
''',
    "assembler.py": '''from pathlib import Path
def emit(case_name, text):
    Path("cases").mkdir(exist_ok=True)
    Path("cases/" + case_name + ".yml").write_text(text, encoding="utf-8")
    Path(".ai/draft").mkdir(parents=True, exist_ok=True)
    Path(f".ai/draft/{case_name}.yml").write_text(text, encoding="utf-8")
''',
    "transport.py": '''def parse(resp):
    return resp["choices"][0]["message"]["content"]
''',
}

# 违规组：{文件名: (源码, 期望违规数, 违规类别关键词)}
# 同名文件在 clean/violation 两组分别出现 → 同一注册表规则的两个方向都被钉住。
SELFTEST_VIOLATIONS: Dict[Text, Tuple[Text, int, Text]] = {
    "llm.py": (
        '''def save(resp):
    with open("resp.json", "w") as fp:
        fp.write(resp)
def save2(resp):
    from pathlib import Path
    Path("resp.json").write_text(resp)
''',
        2,
        "一律不写盘",
    ),
    "cache.py": (
        '''def emit(case, text):
    with open("cases/" + case + ".yml", "w") as fp:
        fp.write(text)
def wipe(root):
    with open(root, "w") as fp:
        fp.write("")
''',
        2,
        "不在允许根",
    ),
    "manifest.py": (
        '''from pathlib import Path
Path("reports").mkdir(exist_ok=True)
''',
        1,
        "不在允许根",
    ),
    "diagnosis.py": (
        '''import os
def leak(text):
    os.makedirs(".ai/analysis", exist_ok=True)
    with open(".ai/analysis/out.md", "w") as fp:
        fp.write(text)
''',
        2,
        "不在允许根",
    ),
    "doc_quality.py": (
        '''def leak(path, text):
    with open("cases/" + path + ".md", "w") as fp:
        fp.write(text)
''',
        1,
        "不在允许根",
    ),
    "helper.py": (
        '''from pathlib import Path
def export(name, text):
    Path("cases/" + name + ".yml").write_text(text)
def backup(src):
    import shutil
    shutil.copy(src, "backups/" + src)
''',
        2,
        "未登记模块",
    ),
}


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------


def run_selftest() -> int:
    """跑扫描器自检。返回 0=全绿；非 0=失败项数。"""
    failures: List[Text] = []

    for fname, source in SELFTEST_CLEAN.items():
        bad = scan_source(source, fname)
        if bad:
            failures.append(f"[误报] 合规样本 {fname} 被判违规：{bad}")

    for fname, (source, want, keyword) in SELFTEST_VIOLATIONS.items():
        bad = scan_source(source, fname)
        if len(bad) != want:
            failures.append(
                f"[漏判/多判] 违规样本 {fname}：判出 {len(bad)} 处，期望 {want} 处"
            )
        elif not any(keyword in v.reason for v in bad):
            failures.append(f"[错因] 违规样本 {fname}：原因未命中类别「{keyword}」：{bad}")

    # bench/ 真扫描 ↔ 登记表对账（新增写盘 / 写盘点消失，计数都会对不上）
    bench_bad = scan_tree(os.path.join(BASE, "bench"))
    got: Dict[Tuple[Text, Text], int] = {}
    for v in bench_bad:
        key = (v.path, v.func)
        got[key] = got.get(key, 0) + 1
    if got != dict(BENCH_KNOWN_WRITES):
        failures.append(
            f"[对账] bench/ 写盘点与 BENCH_KNOWN_WRITES 不一致："
            f"实测 {got}；登记 {dict(BENCH_KNOWN_WRITES)}"
        )

    # interfacetester_ai/（T6 建包后自动生效）：生产包必须零违规
    pkg = os.path.join(BASE, "interfacetester_ai")
    if os.path.isdir(pkg):
        pkg_bad = scan_tree(pkg)
        if pkg_bad:
            failures.append(f"[越界] interfacetester_ai/ 存在写盘违规：{pkg_bad}")
        print(f"  生产包扫描        ：interfacetester_ai/ {len(pkg_bad)} 违规")
    else:
        print("  生产包扫描        ：interfacetester_ai/ 不存在（T6 未建包，跳过）")

    print("=" * 66)
    if failures:
        print(f"红线③ 写盘边界扫描自检**失败** {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        print("=" * 66)
        return len(failures)

    print("红线③ 写盘边界扫描自检全部通过：")
    print(f"  合规样本（必绿）  ：{len(SELFTEST_CLEAN)} 份全部放行")
    print(
        f"  违规样本（必红）  ：{len(SELFTEST_VIOLATIONS)} 份共 "
        f"{sum(w for _, w, _ in SELFTEST_VIOLATIONS.values())} 处全部命中，类别正确"
    )
    print(f"  bench/ 对账       ：写盘点与登记表逐一相等（{len(BENCH_KNOWN_WRITES)} 处）")
    print("=" * 66)
    return 0


def main() -> int:
    # NOTICE（T26 同款纪律）：重定向 + GBK locale 下也要字节级可读、退出码可靠
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    return run_selftest()


if __name__ == "__main__":
    raise SystemExit(main())
