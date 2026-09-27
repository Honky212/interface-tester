"""IR → 标准 InterfaceTester YAML（交给现有 `hmake` 链路）+ 「导出即验证」。

两条硬要求（评估文档 3.6）
--------------------------
1. **输出标准 YAML，而不是直接生成 Python** —— 导入器对运行时零侵入，格式与手写用例完全一致；
2. **「导出即验证」**：写完盘立刻 `load_testcase()` + `hmake` 干跑，跑不通就报错 ——
   这既是质量保证，也顺带保证了「IR 字段一定在 `TRequest` 认识的范围里」。
"""

import json
import os
from typing import Any, Dict, List, Optional, Text, Tuple

import yaml

from interfacetester import exceptions
from interfacetester.converters.ir import IRCase, IRStep

# 生成文件的头部注释（YAML 注释不会被 safe_dump 写出来，所以手工拼接）
_HEADER_TEMPLATE = (
    "# 由 `hconvert` 从 {source} 生成（InterfaceTester 导入器）\n"
    "# 用法：hrun {yaml_name}\n"
    "# NOTICE:\n"
    "#   - 敏感信息（Cookie/Authorization/token 等）已替换成 ${{...}} 占位，\n"
    "#     占位符的值定义在 config.variables 里（默认从环境变量读）——**不要**把明文写回来；\n"
    "#   - 生成后请人工过一遍：契约/录制里造出来的骨架值（时间戳/UUID/路径 id/示例值）需要按业务替换；\n"
    "#   - 生成的 *_test.py 由 hmake 产出，改用例请改这个 YAML。\n"
)


# 导入目录里的项目标记。
# NOTICE: 框架用「从用例路径**向上找 debugtalk.py**」来定位项目根目录 RootDir
# （`loader.locate_project_root_directory`），而生成用例里的文件引用（`schemas/xxx.json`）
# 是**相对 RootDir** 解析的 —— 所以导入目录必须有 debugtalk.py，否则 RootDir 会退化成 cwd，
# schema 文件就找不到了（P3-a 实测：HAR 导入的用例在 `hrun` 时直接报「schema 文件不存在」）。
_DEBUGTALK_TEMPLATE = '''"""项目标记 + 自定义函数入口（由 `hconvert` 生成）。

为什么这个文件必须存在
----------------------
框架从用例路径**向上查找** `debugtalk.py` 来确定项目根目录（RootDir）；
导入生成的用例里，JSON Schema 走的是**文件引用**（`schemas/xxx.json`），
而文件引用是相对 RootDir 解析的。没有这个文件，RootDir 会退化成「当前工作目录」，
换台机器/换个目录跑就会报「schema 文件不存在」。

下面两个 helper 是 Postman 动态变量的落点（`{{$guid}}` / `{{$randomInt}}`）：
导入器会把它们映射成 `${uuid4_str()}` / `${random_int()}`。用不到也可以留着。
"""

import random
import uuid


def uuid4_str():
    """Postman `{{$guid}}` / `{{$randomUUID}}` 的对应实现。"""
    return str(uuid.uuid4())


def random_int(start=1, end=1000):
    """Postman `{{$randomInt}}` 的对应实现（含两端）。"""
    return random.randint(int(start), int(end))


def noop():
    """占位函数（留着是为了让模板看起来不像空文件）。"""
    return True
'''


def build_request_dict(step: IRStep) -> Dict[str, Any]:
    """IRStep → YAML 的 `request` 字典。**只输出 `TRequest` 认识的字段**。

    NOTICE（缺陷 1，**emit 层单点归一**）：四个适配器都会在 multipart 步骤上
    **同时**写 `data`（普通字段）与 `upload`（文件字段），而运行期
    `ext/uploader.prepare_upload_step()` 会把 `step.request.data` 整个替换成
    `"$m_encoder"` —— `data` 因此被**静默丢弃**，服务端只收到文件、收不到普通字段
    （实测三源一致：`data={'note': 'hello'}` + `upload={'file': ...}` →
    发出的 multipart 体里只有 `file`，零告警）。

    IR 侧保持原样（适配器不必改），在**唯一出口**这里归一：

    - `data` 是 **dict** → 并进 `upload`（uploader 把非文件标量当普通字段发，
      与 `examples/body_forms/README.md` 的约定一致）；`upload` 里已有的同名键优先
      （文件字段不被普通字段顶掉）；
    - `data` 是**字符串**（raw 体）+ `upload` → **无法等价表达**（upload 机制只认
      `upload:` 里的字段），响亮报错，而不是静默丢掉一半；
    - 没有 `upload` 时 `data` 照旧原样输出。
    """
    request: Dict[str, Any] = {"method": step.method, "url": step.url}
    if step.params:
        request["params"] = step.params
    if step.headers:
        request["headers"] = step.headers
    if step.cookies:
        request["cookies"] = step.cookies
    if step.json_body is not None:
        request["json"] = step.json_body

    if step.upload:
        upload = dict(step.upload)
        if step.data is not None:
            if isinstance(step.data, dict):
                for key, value in step.data.items():
                    upload.setdefault(key, value)
            else:
                raise exceptions.ParamsError(
                    "同一个 request 里不能同时写 `upload`（文件/字段）与字符串 `data`（raw 体）——"
                    "框架的 upload 机制会把 `data` 整个替换成 multipart 编码器，"
                    "字符串 body 会被**静默丢弃**，两者无法等价表达。\n"
                    f"  data（字符串，前 80 字符）: {str(step.data)[:80]!r}\n"
                    f"  upload: {step.upload}\n"
                    "  改法（二选一）：\n"
                    "  1) 要发 multipart → 把普通字段写进 `upload:`，删掉 `data:`；\n"
                    "  2) 要发 raw 体 → 只用 `data:`，删掉 `upload:`。"
                )
        request["upload"] = upload
    elif step.data is not None:
        request["data"] = step.data

    if step.verify is not None:
        request["verify"] = step.verify
    if step.proxies:
        request["proxies"] = step.proxies
    return request


def build_case_dict(ir_case: IRCase) -> Dict[str, Any]:
    """IRCase → YAML 用例字典（`config` + `teststeps`）。"""
    config: Dict[str, Any] = {"name": ir_case.name}
    if ir_case.base_url:
        config["base_url"] = ir_case.base_url
    if ir_case.variables:
        config["variables"] = ir_case.variables

    teststeps: List[Dict[str, Any]] = []
    for step in ir_case.steps:
        teststep: Dict[str, Any] = {"name": step.name}
        if step.variables:
            teststep["variables"] = step.variables
        teststep["request"] = build_request_dict(step)
        if step.extracts:
            teststep["extract"] = step.extracts
        if step.assertions:
            teststep["validate"] = step.assertions
        teststeps.append(teststep)

    return {"config": config, "teststeps": teststeps}


def dump_case_yaml(ir_case: IRCase, yaml_name: Text) -> Text:
    """渲染成 YAML 文本（含头部注释；保持键顺序、允许中文）。"""
    case_dict = build_case_dict(ir_case)
    body = yaml.safe_dump(
        case_dict,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=200,
    )
    header = _HEADER_TEMPLATE.format(source=ir_case.source or "导入素材", yaml_name=yaml_name)
    return header + body


def _defined_function_names(debugtalk_path: Text) -> set:
    """列出 `debugtalk.py` 里定义的函数名（**静态解析，不 import**）。

    NOTICE: 这里刻意**不用** `loader.load_debugtalk_functions()` —— 它靠
    `importlib.import_module("debugtalk")` + `sys.path`，在「同一个进程里已经加载过别的项目」
    时会拿到**另一个项目**的函数表（框架自己的用例也踩过这个坑）。
    导入器只需要知道「这个文件里有没有这两个函数」，用 AST 最稳且无副作用。
    """
    import ast  # noqa: PLC0415

    try:
        with open(debugtalk_path, mode="r", encoding="utf-8") as fp:
            tree = ast.parse(fp.read(), filename=debugtalk_path)
    except (OSError, SyntaxError):
        return set()

    names = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def ensure_project_root(
    out_dir: Text, required_functions: Optional[List[Text]] = None
) -> Tuple[Optional[Text], List[Text]]:
    """确保输出目录是一个「项目根」（含 `debugtalk.py`），否则 schema 引用会解析不到。

    Args:
        required_functions: 生成物会用到的函数名（例如 Postman 动态变量映射出的
            `uuid4_str`/`random_int`）。已存在的 `debugtalk.py` **不会被改写** ——
            只在缺少这些函数时给出告警 + 代码片段，让用户自己决定。

    Returns:
        ``(新写入的 debugtalk.py 路径或 None, 告警)``
    """
    debugtalk_path = os.path.join(out_dir, "debugtalk.py")
    required_functions = required_functions or []
    warnings: List[Text] = []

    if not os.path.isfile(debugtalk_path):
        with open(debugtalk_path, "w", encoding="utf-8") as fp:
            fp.write(_DEBUGTALK_TEMPLATE)
        return debugtalk_path, warnings

    if required_functions:
        defined = _defined_function_names(debugtalk_path)
        missing = [name for name in required_functions if name not in defined]
        if missing:
            warnings.append(
                f"输出目录已存在 `debugtalk.py`，但缺少生成物用到的函数 {missing} —— "
                "**不会被自动改写**，请手工补上（片段见 docs/convert/README.md「Postman 动态变量」）：\n"
                "    import random, uuid\n"
                "    def uuid4_str():\n        return str(uuid.uuid4())\n"
                "    def random_int(start=1, end=1000):\n        return random.randint(int(start), int(end))"
            )
    return None, warnings


def emit_case(
    ir_case: IRCase,
    out_dir: Text,
    yaml_name: Optional[Text] = None,
    required_functions: Optional[List[Text]] = None,
) -> Dict[Text, Any]:
    """把 IR 写到磁盘：YAML 用例 + 附带的 schema 文件。

    Returns:
        ``{"yaml": <用例路径>, "extra": {<相对路径>: <绝对路径>}}``
    """
    os.makedirs(out_dir, exist_ok=True)
    debugtalk_path, debugtalk_warnings = ensure_project_root(out_dir, required_functions)
    yaml_name = yaml_name or f"{_slug(ir_case.name)}.yml"
    yaml_path = os.path.join(out_dir, yaml_name)

    with open(yaml_path, "w", encoding="utf-8") as fp:
        fp.write(dump_case_yaml(ir_case, yaml_name))

    written_extra: Dict[Text, Text] = {}
    for relative_path, content in (ir_case.extra_files or {}).items():
        absolute_path = os.path.join(out_dir, *relative_path.split("/"))
        os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
        with open(absolute_path, "w", encoding="utf-8") as fp:
            if isinstance(content, str):
                fp.write(content)
            elif relative_path.lower().endswith(".json"):
                # NOTICE: 扩展名决定解析器（`_load_schema_file` 也是这么判的）：
                # `.json` 必须写**真 JSON**，写成 YAML 会让运行期报 JSONDecodeError（实测踩过）。
                json.dump(content, fp, ensure_ascii=False, indent=2)
            else:
                yaml.safe_dump(content, fp, allow_unicode=True, sort_keys=False)
        written_extra[relative_path] = absolute_path

    return {
        "yaml": yaml_path,
        "extra": written_extra,
        "debugtalk": debugtalk_path or "",
        "warnings": debugtalk_warnings,
    }


def validate_emitted_case(yaml_path: Text) -> Text:
    """**导出即验证**：`load_testcase()` + 渲染/hmake 干跑，返回生成的 Python 源码。

    NOTICE:
    - 这里刻意**不走 `main_make()`**：它在校验失败时会 `sys.exit(1)`，会把具体原因吞成一句
      「exit 1」。改成直接调用 `load_testcase_file` → `load_test_file` → `make_testcase`
      （内部已包含算子白名单、内联 schema 拦截等所有护栏），异常能原样带出来；
    - 副作用是会在同目录生成 `*_test.py`（原本 `hrun` 也会产出），并做一次 black 格式化；
      想避免副作用用 `--no-validate`。
    """
    from interfacetester.loader import load_project_meta, load_test_file, load_testcase_file  # noqa: PLC0415
    from interfacetester.make import format_pytest_with_black, make_testcase  # noqa: PLC0415

    # 1) 模型层校验（pydantic + 未知字段告警）
    load_testcase_file(yaml_path)

    # 2) 渲染干跑：真实走 hmake 的渲染与全部生成期护栏（异常带具体信息）
    testcase_dict = load_test_file(yaml_path)
    if not isinstance(testcase_dict, dict):
        raise exceptions.MyBaseError(f"用例文件内容不是字典：{yaml_path}")
    testcase_dict.setdefault("config", {})["path"] = os.path.abspath(yaml_path)
    generated_path = make_testcase(testcase_dict)

    # 3) 与 main_make 一致的格式化（保持生成物风格稳定）
    format_pytest_with_black(generated_path)

    if not os.path.isfile(generated_path):
        raise exceptions.MyBaseError(f"生成物不存在：{generated_path}")

    with open(generated_path, mode="r", encoding="utf-8") as fp:
        source = fp.read()

    # 4) 生成物必须是合法 Python（语法级兜底）
    compile(source, generated_path, "exec")

    # 5) 项目定位也要能过（RootDir / debugtalk）
    load_project_meta(yaml_path, keep_loaded_meta_for_projectless=False)
    return source


def slugify(text: Text) -> Text:
    """把用例名转成安全的文件名（**供 CLI 与 emit 共用**）。"""
    return _slug(text)


def _slug(text: Text) -> Text:
    """把用例名转成安全的文件名（保留中文，替换空白与路径分隔符）。"""
    import re  # noqa: PLC0415

    slug = re.sub(r"[\\/:*?\"<>|\s]+", "_", text.strip())
    return slug.strip("_") or "imported_case"


def summarize(ir_case: IRCase) -> Tuple[int, int, int]:
    """``(步骤数, 断言数, 告警数)``，供 CLI 打印一行摘要。"""
    assertions = sum(len(step.assertions) for step in ir_case.steps)
    return len(ir_case.steps), assertions, len(ir_case.all_warnings())
