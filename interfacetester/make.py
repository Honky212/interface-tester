import ast
import builtins
import hashlib
import importlib.util
import inspect
import json
import keyword
import os
import string
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Set, Text, Tuple, get_args

import jinja2
from loguru import logger

from interfacetester import __version__, exceptions
from interfacetester.builtin import comparators as builtin_comparators
from interfacetester.compat import (
    convert_parameters,
    convert_variables,
    ensure_path_sep,
    ensure_testcase_v4,
    ensure_testcase_v4_api,
)
from interfacetester.loader import (
    convert_relative_project_root_dir,
    load_folder_files,
    load_project_meta,
    load_test_file,
    load_testcase,
    relative_to_root_dir,
)
from interfacetester.models import TConfig, TRequest, TStep
from interfacetester.response import uniform_validator
from interfacetester.utils import ga4_client

""" cache converted pytest files, avoid duplicate making
"""
pytest_files_made_cache_mapping: Dict[Text, Text] = {}

""" save generated pytest files to run, except referenced testcase
"""
pytest_files_run_set: Set = set()

""" 生成物路径 → **源用例**绝对路径（0918-8 / M16）。

    用途：发现「两个不同的 YAML 归一化后落到同一个 `_test.py`」。修复前这种碰撞是**静默**的：
    先到的用例生成、后到的被缓存短路掉，于是后者既不生成也不执行，退出码仍是 0。
"""
pytest_files_source_mapping: Dict[Text, Text] = {}


class MakeFailure(object):
    """一个「生成失败」的记录（0918-4 / L8）。

    批量 hmake 不再「首个失败即终止整批」：每个失败都记一条，全部处理完后
    在 `main_make` 末尾统一列出并以退出码 1 收尾。
    """

    __slots__ = ("path", "reason")

    def __init__(self, path: Text, reason: Text) -> None:
        self.path = path
        self.reason = reason


""" 本次 `main_make` 调用里失败的用例（每调用一次清空一次，见 main_make 开头）。
"""
make_failures: List[MakeFailure] = []


""" 本次 `main_make` 调用里「不像用例、被跳过」的文件（0919-19 / H4 规则③，
    同样每调用一次清空一次）。

    WHY: 目录批量里 `hconvert` 生成的 `schemas/*.json`、Postman 集合 JSON、OpenAPI
    契约 yaml 都是**合法非用例文件**，不能因为"生成不出用例"就判失败；但也不能
    像修复前那样**零信号**——`hmake <目录>` 会打印若干条 warning、然后"全绿"退出，
    看起来像"都跑过了"。这里记下来，由 `main_make` 末尾汇总成一行。
"""
make_skipped_files: List[Text] = []


def _looks_like_testcase_intent(test_content: Any) -> bool:
    """内容特征判据（0919-19 / H4 规则②）：看得出"这是想写用例"才算用例。

    NOTICE: 修复前 `__make` 把目录里**所有** `.yml/.yaml/.json` 都当用例处理——
    这既是"合法非用例文件被误判"的来源，也是"坏用例被静默跳过"的来源。
    现在按内容区分：**含 `config` 或 `teststeps`** 才算有用例意图。
    """
    if not isinstance(test_content, Dict):
        return False
    return "config" in test_content or "teststeps" in test_content


def _looks_like_testcase_text(test_file: Text) -> bool:
    """规则②的**文本版**：连解析都失败（拿不到 dict 内容）时，按源文本判「用例意图」。

    NOTICE（残留缺陷：坏用例被静默跳过 → CI 全绿）：
    `__make` 原来在「解析阶段就失败」时一律传 `looks_like_case=False`，理由是
    「无法判意图（可能是模板/契约/非用例 YAML）」——这个保守取向本身是对的，
    但它把**真正的坏用例**一起放过了：目录扫描时，一个缩进写错的用例 YAML、
    或一个值根本构造不出来的 YAML（超长整数 → CPython 4300 位上限）
    都只是「已跳过 1 个不像用例的文件」+ **退出码 0**，CI 里等于用例凭空消失。

    判据与 `_looks_like_testcase_intent` **完全同一**（只看 `config` / `teststeps`
    这两个键），只是搬到文本层：只有**行首的映射键**才算（YAML 的 `config:`、
    JSON 的 `"config":`，允许前面有列表符号 `-`），因此：
      - 用例里常见的两种写法都能认出；
      - 契约/模板类文件（OpenAPI 的 `paths:`、Postman 的 `"item":`、
        JSON Schema 的 `$schema:`）不会被误判成用例 → 维持「只告警不记失败」；
      - 读不出来（二进制/不存在）→ False，行为与修复前逐字一致。
    """
    try:
        with open(test_file, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read(64 * 1024)
    except (OSError, ValueError):
        return False

    markers = []
    for key in ("config", "teststeps"):
        for quote in ("", '"', "'"):
            markers.append(f"{quote}{key}{quote}:")

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("-"):
            stripped = stripped.lstrip("-").strip()
        for marker in markers:
            if stripped.startswith(marker):
                return True
    return False


def _fail_or_skip(
    test_file: Text,
    reason: Text,
    *,
    explicitly_named: bool,
    looks_like_case: bool,
    label: Text = "test file",
) -> None:
    """不可用的文件该**记失败**还是只**记跳过**（0919-19 / H4 的三条规则）。

    - `explicitly_named`（命令行**显式点名**的文件）：任何失败都记失败。
      `hmake case.yml` 是 CI 里最常见的用法，而修复前这条路径只 warning + continue，
      **退出码恒为 0**（实测 `hmake broken.yml` → exit 0、`hmake empty.yml` → exit 0），
      同一批里"未知算子"却是 exit 1——口径不一致；
    - `looks_like_case`（目录扫描 + 内容看得出用例意图）：记失败——意图明显却结构
      不合法，那是**坏用例**，不是"目录里混了别的文件"；
    - 其余（目录扫描 + 不像用例）：只告警 + 计入 `make_skipped_files`，
      由 `main_make` 末尾汇总（规则③），让"全绿但什么都没跑"至少可见。
    """
    if explicitly_named or looks_like_case:
        _record_make_failure(test_file, reason)
        return

    logger.warning(f"Invalid {label}: {test_file}\nreason: {reason}")
    make_skipped_files.append(test_file)


def _record_make_failure(path: Text, reason: Text, unexpected: bool = False) -> None:
    """记录一个用例生成失败，并立刻打一条可见的错误日志。

    NOTICE: 这里**必须**立刻打日志——失败不再中止整批，日志是针对单个文件的
    唯一直接信号（末尾还会再汇总一次）。`unexpected=True` 表示非 `MyBaseError`
    的意外异常（框架 bug），额外打 traceback 以便定位。
    """
    make_failures.append(MakeFailure(path, reason))

    if unexpected:
        logger.error(
            f"unexpected error while making test file: {path}\n{reason}",
            backtrace=False,
        )
        logger.opt(exception=True).debug(f"traceback for {path}")
    else:
        logger.error(f"failed to make test file: {path}\n{reason}")

""" 内置断言算子名：从 `builtin/comparators.py` 动态取，避免与手工清单漂移。
    供 `ensure_known_comparators()` 在报错信息里列出可用算子。
"""
BUILTIN_COMPARATOR_NAMES: Tuple[Text, ...] = tuple(
    sorted(
        name
        for name, value in vars(builtin_comparators).items()
        if inspect.isfunction(value) and not name.startswith("_")
    )
)

# 生成物**首行**的标记前缀（`__TEMPLATE__` 第一行就是这个）。
#
# NOTICE（0921-2 / **发现 3**）：与 `compat.CONFTEST_MARKER` 同一套判据形状 ——
# 「首行严格匹配」而不是「子串匹配」。`compat.is_generated_conftest` 的 docstring
# 已经把理由写透了：
#
#     用户完全可能在文档字符串里提到这句话（例如"本文件曾被 Generated By
#     InterfaceTester 覆盖过"），子串匹配会把它误判成"我们的文件"然后放心覆盖
#     —— 那正好是要防的事。
#
# 而 `ensure_generated_file_may_be_overwritten` 用的恰恰是被否决的**子串匹配**
# （`"Generated By InterfaceTester" in head`），于是同一件事在仓库里有两套口径，
# 且宽松的那套用在**更容易被手写**的 `*_test.py` 上。实测（`.tmp_audit/verify0921_2/`）：
#
#     手写文件 `# my notes: ... Generated By InterfaceTester\n...` → 子串判据**放行覆盖**
#     手写文件 `"""... Generated By InterfaceTester."""`          → 子串判据**放行覆盖**
#
# 现在改成首行严格匹配。安全性由事实保证：仓库 34 个已入库生成物的标记**全部在第 1 行**
# （`git grep` 可验），所以这条收紧不会挡住任何正常重跑。
GENERATED_FILE_MARKER_PREFIX = "# NOTE: Generated By InterfaceTester"

__TEMPLATE__ = jinja2.Template(
    """# NOTE: Generated By InterfaceTester {{ version }}
# FROM: {{ testcase_path }}

{%- if parameters or skip %}
import pytest
{% endif %}
from interfacetester import InterfaceTester, Config, Step, RunRequest

{%- if parameters %}
from interfacetester import Parameters
{%- endif %}

{%- if reference_testcase %}
from interfacetester import RunTestCase
{%- endif %}

{%- for import_str in imports_list %}
{{ import_str }}
{%- endfor %}

class {{ class_name }}(InterfaceTester):

    {% if parameters and skip %}
    @pytest.mark.parametrize("param", Parameters({{ parameters }}))
    @pytest.mark.skip(reason={{ skip }})
    def test_start(self, param):
        super().test_start(param)

    {% elif parameters %}
    @pytest.mark.parametrize("param", Parameters({{ parameters }}))
    def test_start(self, param):
        super().test_start(param)

    {% elif skip %}
    @pytest.mark.skip(reason={{ skip }})
    def test_start(self):
        super().test_start()
    {% endif %}

    config = {{ config_chain_style }}

    teststeps = [
        {% for step_chain_style in teststeps_chain_style %}
            {{ step_chain_style }},
        {% endfor %}
    ]

if __name__ == "__main__":
    {{ class_name }}().test_start()

"""
)

# NOTICE（0920 批次 5 / **N28**）：生成物模板在**模块级**用到的名字。
# 被引用用例的 import 别名一旦撞上其中之一，就会把模板自己的名字顶掉
# （`as Config` 覆盖 `from interfacetester import … Config`），
# 而生成物语法合法 → `hmake` exit 0 → 直到 pytest 收集期才以
# `TypeError: TestCaseConfig() takes no arguments` 这种**看不出根因**的形式炸掉。
#
# 这份清单必须与上面的 `__TEMPLATE__` 保持一致：
#   - 模板固定 import 的：InterfaceTester / Config / Step / RunRequest
#   - 条件 import 的：pytest / Parameters / RunTestCase（一并列入——
#     别名遮蔽不区分"这次有没有 import"，保守占用没有代价）
#   - 模板自己注入的：sys / Path（`sys.path.insert` 那段）
#
# 有单测 `test_reserved_names_match_the_generated_template` 盯着这份清单
# （从模板源码里抽 import 出来的名字来对账），所以改模板忘了改这里会立刻红。
_GENERATED_TEMPLATE_RESERVED_NAMES = frozenset(
    {
        "InterfaceTester",
        "Config",
        "Step",
        "RunRequest",
        "RunTestCase",
        "Parameters",
        "pytest",
        "sys",
        "Path",
    }
)


def ensure_json_schema_not_inline(teststeps: List[Dict], testcase_path: Text = "") -> None:
    """在 hmake 阶段拦住「内联 JSON Schema」，把运行期异常变成生成期报错。

    背景（P2-b 实测）：`parser.parse_data` 会连 dict 的 **key** 一起解析
    （`parser.py:423-428`），而 JSON Schema 必带的 `$schema`/`$ref`/`$id`/`$defs` 都是 dict key，
    会命中变量正则 → 运行期抛 `VariableNotFound: schema not found in {}`。
    这个报错既晚（要跑用例才发现）又难懂（完全看不出是 schema 的键被当变量了），
    所以在生成前精确拦一次：**只拦「内联 dict + 含 `$` 前缀键」这一种**（可精确判定），
    不含 `$` 键的极简内联 schema 仍然放行（那是能跑的）。

    Args:
        teststeps (list): 用例的 `teststeps`
        testcase_path (str): 用例路径，仅用于报错定位
    """
    for teststep in teststeps or []:
        for raw_validator in teststep.get("validate") or []:
            try:
                validator = uniform_validator(raw_validator)
            except exceptions.MyBaseError:
                # 形态非法交给别的校验去报，这里只管 schema 引用方式
                continue

            if validator["assert"] != "jsonschema_match":
                continue

            expect = validator["expect"]
            if isinstance(expect, dict) and builtin_comparators._json_schema_has_keyword_keys(
                expect
            ):
                keyword_keys = sorted(
                    key
                    for key in expect
                    if isinstance(key, str) and key.startswith("$")
                )
                raise exceptions.ParamsError(
                    f"jsonschema_match 不支持内联 schema（含 `$` 前缀的键会在运行期被当成变量解析）\n"
                    f"testcase: {testcase_path}\n"
                    f"step: {teststep.get('name')}\n"
                    f"validator: {raw_validator}\n"
                    f"检测到的问题键: {keyword_keys}\n"
                    f"两种正确写法：\n"
                    f'  1) schema 放 debugtalk.py，YAML 写 {{jsonschema_match: ["body", "${{get_user_schema()}}"]}}（首选）\n'
                    f'  2) 写 schema 文件路径：{{jsonschema_match: ["body", "schemas/user.json"]}}'
                    f"（相对项目根目录解析）"
                )


def ensure_xsd_files_exist(teststeps: List[Dict], testcase_path: Text = "") -> None:
    """在 hmake 阶段校验 `xml_schema_match` 引用的 **XSD 文件存在**（A2-2）。

    与 P2-b「生成期就报错」同一哲学：XSD 路径写错时不该等到跑用例才发现（那时错误发生在
    断言内部，还得先跑一遍请求）。

    **只检查能静态判定的值**：`"schemas/x.xsd"` 或 `{"xpath": …, "xsd": "schemas/x.xsd"}`；
    值里含 `$`（`${func()}` / `$变量`）时**跳过** —— 那种路径只有运行期才知道，
    硬检查会误报（**宁可漏报也不假报**；需要跳过检查的写法，正好就是用 `${func()}` 返回路径）。

    **不编译 XSD**：生成阶段不该依赖 `xml` extra（装没装 lxml 都要能 `hmake`）；
    编译错误仍由运行期给出（XSD 自身不合法 → `RuntimeError`）。

    Args:
        teststeps (list): 用例的 `teststeps`
        testcase_path (str): 用例路径，仅用于报错定位
    """
    for teststep in teststeps or []:
        for raw_validator in teststep.get("validate") or []:
            try:
                validator = uniform_validator(raw_validator)
            except exceptions.MyBaseError:
                # 形态非法交给别的校验去报，这里只管 XSD 引用
                continue

            if validator["assert"] != "xml_schema_match":
                continue

            xsd_path = __static_xsd_path(validator["expect"])
            if not xsd_path:
                continue

            if xsd_path.startswith("<"):
                # 与 P2-b 的「内联 schema」同源：**错误的引用形态**在生成期就拦，且给出正确写法
                raise exceptions.ParamsError(
                    f"xml_schema_match 只接受 **XSD 文件路径**，不支持内联 XSD 字符串\n"
                    f"testcase: {testcase_path}\n"
                    f"step: {teststep.get('name')}\n"
                    f"validator: {raw_validator}\n"
                    f"原因：`xs:include` / `xs:import` 的相对路径要按 **XSD 文件所在目录** 解析，"
                    f"内联字符串没有「所在目录」。\n"
                    f'正确写法：把 XSD 存成文件，例如 '
                    f'{{xml_schema_match: ["body", {{"xpath": "//svc:QueryResponse", '
                    f'"xsd": "schemas/query.xsd"}}]}}'
                )

            try:
                builtin_comparators._resolve_schema_path(
                    xsd_path,
                    kind="XSD",
                    hint="XSD 只能用**文件路径**（`xs:include` 的相对路径要按文件所在目录解析）；"
                    "若这个文件是运行期才生成的，把路径写成 ${func()} 返回即可跳过本检查",
                )
            except RuntimeError as ex:
                raise exceptions.ParamsError(
                    f"xml_schema_match 引用的 XSD 文件不存在（hmake 阶段提前报错）\n"
                    f"testcase: {testcase_path}\n"
                    f"step: {teststep.get('name')}\n"
                    f"validator: {raw_validator}\n"
                    f"{ex}"
                ) from ex


def __static_xsd_path(expect: Any) -> Text:
    """从 `xml_schema_match` 的第二个参数里取「能静态判定的 XSD 路径」；取不到返回空串。"""
    if isinstance(expect, str):
        candidate = expect.strip()
    elif isinstance(expect, dict):
        value = expect.get("xsd")
        candidate = value.strip() if isinstance(value, str) else ""
    else:
        candidate = ""
    if not candidate or "$" in candidate:
        return ""
    return candidate


def ensure_known_comparators(
    teststeps: List[Dict], functions_mapping: Dict = None, testcase_path: Text = ""
) -> None:
    """在 hmake 阶段校验 `validate` 里的断言算子名，拼错立即报错。

    P0 修复背景：`StepRequestValidation` 现在用 `__getattr__` 动态分发自定义算子，
    于是「算子名拼错」不再在生成/导入阶段暴露，而是拖到用例执行时才由
    `parser.get_mapping_function` 抛 `FunctionNotFound`。这里在生成前过一遍白名单，
    把「拼错立即报错」这个性质找回来（报错同时给出用例/步骤/可用算子）。

    **白名单口径（0918-1 收紧）：只认两类**
      ① 项目 `debugtalk.py` 里定义的函数（自定义算子）；
      ② 框架内置算子（`BUILTIN_COMPARATOR_NAMES`，从 `builtin/comparators.py` 自动派生）。

    收紧的原因（H3）：修复前这里复用了 `parser.get_mapping_function` 的**全部**解析顺序，
    其中最后一级是回落到 **Python 内置函数**，于是 `validate: - print: ["status_code", 1]`
    这样的写法会**通过白名单并生成 `.assert_print(...)`**；而运行期算子被调用为
    `assert_func(check_value, expect_value, message)` 且**返回值被忽略**，
    `print` 恰好可变参 → 实测判为 **pass**（还顺手往 stdout 打了日志）。
    即「拼错的名字恰好等于某个内置名」= 静默假通过，是最难查的一类失败。

    运行期 `ResponseObject.validate` 同步改为 `allow_builtins=False`（只认上述两类），
    因此两侧口径仍然**完全一致**：不会拦住任何运行期本来能用的算子。

    Args:
        teststeps (list): 用例的 `teststeps`
        functions_mapping (dict): 项目 `debugtalk.py` 的函数表（`project_meta.functions`）
        testcase_path (str): 用例相对 RootDir 的路径，仅用于报错定位
    """
    functions_mapping = functions_mapping or {}

    for teststep in teststeps or []:
        for raw_validator in teststep.get("validate") or []:
            # NOTICE（批次 9-1 / 漂移收口）：**这里与两个兄弟闸门刻意不同**。
            # `ensure_json_schema_not_inline` / `ensure_xsd_files_exist` 对形态非法的
            # validator 是 `except MyBaseError: continue` —— 它们只关心**一种**算子的
            # 引用方式，形态不对不是它们的事，交给「别的校验」去报。
            # 本函数是**最后一道闸门**（`make_testcase` 里它之后就到代码生成），而且它
            # 必须拿到算子名才能判白名单 —— 没有「别的校验」会报这个错：
            # `load_testcase` 的 pydantic 只校验到 `List[Dict]`（
            # `{"eq": [...], "contains": [...]}` 这种双键、`{"eq": "notalist"}` 这种
            # 形态错误都能通过模型），到了运行期才在 `ResponseObject.validate` 里抛
            # `ParamsError: invalid validator` —— 那时的报错**没有用例/步骤定位**，
            # 而且表现为 pytest 的 error 而不是可读的 failed。
            # 所以这里**接着报错、但把定位信息补上**（与下面「未知算子」那条同一个格式），
            # 而不是 `continue` 把问题放行到运行期。
            try:
                validator = uniform_validator(raw_validator)
            except exceptions.MyBaseError as ex:
                raise exceptions.ParamsError(
                    f"validate 的写法非法：{ex}\n"
                    f"每条断言只能是两种形态之一：\n"
                    f'  1) {{算子名: [检查项, 期望值]}} 或 {{算子名: [检查项, 期望值, 消息]}}（推荐）\n'
                    f'  2) {{"check": 检查项, "comparator"/"assert": 算子名, "expect": 期望值, "message": 消息}}\n'
                    f"NOTICE: 形态错误必须在生成期拦下 —— 运行期才发现的话，"
                    f"报错里没有用例/步骤信息，而且 pytest 记的是 error 不是 failed。\n"
                    f"testcase: {testcase_path}\n"
                    f"step: {teststep.get('name')}\n"
                    f"validator: {raw_validator}"
                ) from ex

            assert_method = validator["assert"]

            if assert_method in functions_mapping:
                continue
            if assert_method in BUILTIN_COMPARATOR_NAMES:
                continue

            if hasattr(builtins, assert_method):
                # 最容易被误判成「拼错」的一类：名字本身是 Python 内置函数
                reason = (
                    f"{assert_method!r} 是 **Python 内置函数**，不能作为断言算子。\n"
                    f"内置函数只用于 `$var`/`${{func()}}` 取值（如 `${{len($x)}}`），"
                    f"**不参与 validate**；算子会被调用为 "
                    f"`(check_value, expect_value, message)` 且返回值被忽略，"
                    f"所以像 `print` 这种可变参内置函数会让断言**静默通过**。\n"
                    f"如果你本意是自定义断言，请在项目 debugtalk.py 里用别的名字定义它。"
                )
            else:
                reason = (
                    f"unknown assert comparator: {assert_method}\n"
                    f"自定义断言算子请定义在项目 debugtalk.py 里，签名 "
                    f'(check_value, expect_value, message="")'
                )

            raise exceptions.FunctionNotFound(
                f"{reason}\n"
                f"testcase: {testcase_path}\n"
                f"step: {teststep.get('name')}\n"
                f"validator: {raw_validator}\n"
                f"内置断言算子（{len(BUILTIN_COMPARATOR_NAMES)} 个）: "
                f"{', '.join(BUILTIN_COMPARATOR_NAMES)}"
            )


""" 生成器**认识**的 teststep 字段（与 `make_teststep_chain_style` 的分支一一对应）。
    用途：`ensure_generatable_teststeps` 用它做「模型认了、生成器却表达不出来」的交叉校验。
"""
GENERATOR_SUPPORTED_STEP_FIELDS: Tuple[Text, ...] = (
    "name",
    "variables",
    "request",
    "testcase",
    "retry_times",
    "retry_interval",
    "setup_hooks",
    "teardown_hooks",
    "extract",
    "export",
    "validate",
    "validators",
)

""" 「模型认、运行期有实现、但**生成器无分支**」的 teststep 字段：命中即报错。

    NOTICE（0918-1）：这几个字段是同一个模式的三次复发——
    模型层（`models.TStep`）定义、白名单收录（loader 与 compat 都认，因此**不告警**）、
    运行期也有真实实现（或曾被实现），唯独 `make.py` 没有对应分支，于是**静默丢弃**。
    历史实例：`validate_script` 静默丢失导致 `examples/httpbin/validate.yml` 里
    `assert status_code == 201`（故意要失败的断言）在生成物中消失，用例因此假通过。

    处置原则：**宁可报错，也不静默丢弃**（与 `ensure_json_schema_not_inline`、
    `ensure_xsd_files_exist` 同一哲学：能静态判定的问题在生成期就拦）。
"""
UNSUPPORTED_STEP_FIELDS: Dict[Text, Text] = {
    "validate_script": (
        "`validate_script`（Python 脚本断言）生成器**不支持**——修复前它会被静默丢弃，"
        "用例里的断言凭空消失、反而假通过。\n"
        "  替代写法（二选一）：\n"
        "  1) 用内置算子表达：`validate: - eq: [status_code, 200]`"
        "（22 个内置算子见 docs/能力清单.md）；\n"
        "  2) 复杂逻辑写成项目 debugtalk.py 里的**自定义算子**，"
        "签名 `(check_value, expect_value, message=\"\")`，"
        "然后 `validate: - 你的算子名: [check, expect]`。"
    ),
    "sql_request": (
        "`sql_request`（SQL step）**不支持写在 YAML 里**——`make.py` 只渲染 "
        "`request` / `testcase` 两种 step。\n"
        "  替代写法：手写 pytest 风格用例（`Step(RunSqlRequest(...))`），"
        "可参考 examples/data_management/sql/04_sql_data_management_test.py。"
    ),
    "thrift_request": (
        "`thrift_request`（Thrift step）**不支持写在 YAML 里**——`make.py` 只渲染 "
        "`request` / `testcase` 两种 step。\n"
        "  替代写法：手写 pytest 风格用例（`Step(RunThriftRequest(...))`）。"
    ),
}


def ensure_generatable_teststeps(
    teststeps: List[Dict], testcase_path: Text = ""
) -> None:
    """交叉校验：**凡是模型认了的 step 字段，生成器要么能生成，要么必须报错。**

    这是 0918-1 批次的核心：H2（`validate_script` 静默丢弃）与 M11
    （`retry_times`/`retry_interval` 静默丢弃）本质是同一个 bug 的两个出口——
    「模型/白名单认了，生成器不认，且不告警」。逐条打补丁只能治标，
    所以这里把「**字段可生成性**」做成一道统一的生成期闸门：

    1. `UNSUPPORTED_STEP_FIELDS` 里的字段一旦出现（且取值非空）→ 立即报错；
    2. step 必须能判定出类型（`request` 或 `testcase`），否则报错并列出支持的字段；
    3. 字段与 step 类型必须匹配——`export` 只对引用用例有意义，
       `extract`/`validate` 只对请求 step 有意义。写错位置时生成器会产出**合法 Python
       但方法不存在**的链式调用，失败点在 pytest 收集阶段（整个文件 import 失败），
       比在生成期报错难查得多。

    配套不变量测试见 `tests/make_test.py::TestStepFieldGeneratability`：
    `STEP_KNOWN_FIELDS` 里的每个字段都必须「被生成器支持」或「被本函数拒绝」，
    两者不能有交集之外的空隙——防止下一次再出现静默丢弃。

    Args:
        teststeps (list): 用例的 `teststeps`
        testcase_path (str): 用例路径，仅用于报错定位
    """
    # NOTICE（0920 批次 5 / **N33**）：**空 `teststeps` 必须报错**，不能生成一个
    # 「零步骤但判成功」的用例。
    #
    # 修复前 `for teststep in teststeps or []` 对空列表直接跳过 → 生成
    # `teststeps = []` → `hmake` exit 0、零告警、`hrun` 报 **1 passed**、
    # `summary.json` 里 `success: true` 而 `records` 是 **0 条**：
    #
    # ```text
    # summary.success: True | stat: {'testcases': {'total': 1, 'success': 1, 'fail': 0},
    #                                'teststeps': {...全 0}} | records: 0
    # ```
    #
    # 即「**绿了但什么都没测**」—— 正是本仓 H2 对「参数化数据集为空」立下的那条口径
    # （`parser.parse_parameters` 明确报错，理由就是"绿了但什么都没测"）。
    # 步骤被删空/被注释掉、或上游生成器把列表清空时，CI 会一路绿到底。
    #
    # 反向边界：`teststeps` 键**缺失**（None）不算这条错误——那种用例由下面的
    # 「既没有 request 也没有 testcase」等既有闸门处理；这里只拦"**显式给了空列表**"。
    if teststeps is not None and isinstance(teststeps, list) and not teststeps:
        raise exceptions.ParamsError(
            "`teststeps` 是**空列表** —— 生成出来的用例会「零步骤但判成功」"
            "（hmake exit 0、hrun 1 passed、summary.success=true，records 却是 0 条），"
            "也就是**绿了但什么都没测**。\n"
            f"  用例: {testcase_path}\n"
            "  改法：补上至少一个 teststep；若这个用例暂时不想跑，"
            "请显式写 `config.skip: \"原因\"`（那会以 skipped 的形式如实上报，"
            "而不是伪装成通过）。"
        )

    for teststep in teststeps or []:
        if not isinstance(teststep, dict):
            continue

        step_name = teststep.get("name")
        location = f"testcase: {testcase_path}\nstep: {step_name}"

        # 1) 生成器无分支的字段：报错，绝不静默丢弃
        for field_name, hint in UNSUPPORTED_STEP_FIELDS.items():
            if teststep.get(field_name):
                raise exceptions.ParamsError(
                    f"{field_name}: {hint}\n"
                    f"{location}\n"
                    f"step 内容: {teststep}"
                )

        # 1b) 批次 B / **M6**：`data` 与 `json` 不能同时给（requests 会**静默丢掉** json）
        ensure_request_body_is_unambiguous(teststep, location)

        # 1c) 0920 / **缺陷 1b**：`upload` 与 `data`/`json` 并存同样是静默丢体
        #     （upload 机制会把 `data` 整个换成 $m_encoder；`json` 又会被 requests 丢掉）
        ensure_upload_body_is_unambiguous(teststep, location)

        # 2) step 类型必须可判定（生成器只认 request / testcase）
        # NOTICE: `api` 是 v2/v3 的写法，由 `ensure_testcase_v4` 转换成 `testcase`
        # （见 compat.ensure_testcase_v4 的 `elif "api" in step` 分支）。
        # 本函数刻意跑在 compat **之前**（要看到用户原始写法），所以必须把 `api`
        # 也算作合法指针，否则会把合法的 v3 用例误判成「无效 step」。
        has_request = bool(teststep.get("request"))
        has_testcase = bool(teststep.get("testcase")) or bool(teststep.get("api"))
        if not has_request and not has_testcase:
            raise exceptions.ParamsError(
                f"Invalid teststep: 既没有 `request` 也没有 `testcase`，"
                f"生成器无法判断 step 类型。\n"
                f"{location}\n"
                f"生成器支持的 step 字段: {sorted(GENERATOR_SUPPORTED_STEP_FIELDS)}\n"
                f"step 内容: {teststep}"
            )

        # 2b) 批次 B / **M2**：`request` 与 `testcase`（或 v2/v3 的 `api`）**并存**必须报错。
        #
        # 修复前这里**刻意容忍**该形态（下面第 3 条注释的原话是"同时写了两者时按 request step
        # 校验"），但"容忍"的实际后果不是"按 request 处理"，而是**整条引用用例被静默丢弃**：
        #   - `compat._ensure_step_attachment` 是按白名单**重建** step 的，而它**没有
        #     `testcase` 分支**；`testcase` 只由 `ensure_testcase_v4` 的
        #     `elif "testcase" in step:` 写入 —— 那个分支前面是 `if "request" in step: pass`，
        #     于是两个键并存时 `testcase` 从未进入重建后的 step；
        #   - 生成器 `make_teststep_chain_style` 也是 `if teststep.get("request")` 优先。
        # 实测（`.tmp_audit/verify_claims.py`，真 CLI）：`hmake` exit 0，生成物里
        # **`RunTestCase` 与 `called` 一个字都没有**，只剩 `.post("/main")`；服务端也确认
        # 被引用接口**一次都没被请求**。而白名单 `STEP_KNOWN_FIELDS` 刻意容忍两者并存、
        # 未知字段告警也不响（两个键都是认识的）→ **零告警的假通过**。
        #
        # 这也是本仓 §7 第 8 条（"模型认、生成器无分支 → 静默丢弃 → 断言消失"）的同一族，
        # 只是形态是"两个键互相顶掉"而不是"单字段无分支"。
        if has_request and has_testcase:
            conflicting = [
                field_name
                for field_name in ("request", "testcase", "api")
                if teststep.get(field_name)
            ]
            raise exceptions.ParamsError(
                f"同一个 step 里同时写了 {conflicting} —— 生成器只认一种 step 类型，"
                f"而它**优先按 `request` 处理**：`testcase` 会被**静默丢弃**，"
                f"被引用的整条用例（含它内部的断言）**一步都不会执行**，"
                f"父用例照样变绿（假通过）。\n"
                f"  改法（二选一）：\n"
                f"  1) 想「先调子用例、再打主接口」→ **拆成两个 step**"
                f"（前一个只写 `testcase:`，后一个只写 `request:`）；\n"
                f"  2) 只想打接口 → 删掉 `testcase:` 那一行。\n"
                f"  NOTICE: 修复前这个形态是**静默**的——`hmake` 退出码 0、生成物里连 "
                f"`RunTestCase` 都没有、零告警（两个键都是已知字段，未知字段告警不会响）。\n"
                f"{location}\n"
                f"step 内容: {teststep}"
            )

        # 3) 字段与 step 类型必须匹配。
        #
        # NOTICE（批次 B / M2 更新）：走到这里时 `request` 与 `testcase` **不可能并存**
        # ——上面第 2b 条已经把它们拦掉了，所以下面两条分支是**互斥**的。
        # 修复前这里的注释写的是"同时写了两者时按 request step 校验"，
        # 而那正是"整条引用用例被静默丢弃"的入口。
        if has_request:
            if teststep.get("export"):
                raise exceptions.ParamsError(
                    f"`export` 只能写在**引用用例**（`testcase`）的 step 上，"
                    f"请求 step 上写 `export` 会生成 `.export(...)` 调用——"
                    f"而 `RequestWithOptionalArgs` 没有这个方法，"
                    f"失败点在 pytest 收集阶段（整个生成文件 import 失败）。\n"
                    f"当前 testcase 要导出变量请用 `config.export`。\n"
                    f"{location}\n"
                    f"step 内容: {teststep}"
                )
        else:
            misplaced_fields = [
                field_name
                for field_name in ("extract", "validate", "validators")
                if teststep.get(field_name)
            ]
            if misplaced_fields:
                raise exceptions.ParamsError(
                    f"{misplaced_fields} 只能写在**请求**（`request`）的 step 上。"
                    f"引用用例的 step 会生成 `RunTestCase(...).call(...)` 链，"
                    f"而 `StepRefCase` 没有 `extract`/`validate` 方法，"
                    f"失败点在 pytest 收集阶段（整个生成文件 import 失败）。\n"
                    f"要在引用用例上做断言/取值，请在**被引用用例内部**写 "
                    f"`extract`/`validate`，再用 step 级 `export` 把变量带出来。\n"
                    f"{location}\n"
                    f"step 内容: {teststep}"
                )


""" 生成器**认识**的 config 字段（与 `make_config_chain_style` / `make_config_skip` /
    测试模板里的 `parameters` 一一对应）。用途同 `GENERATOR_SUPPORTED_STEP_FIELDS`：
    给 config 层也建一道「模型认了、生成器必须能表达」的交叉校验。
"""
GENERATOR_SUPPORTED_CONFIG_FIELDS: Tuple[Text, ...] = (
    "name",
    "variables",
    "base_url",
    "verify",
    "timeout",
    "export",
    "oauth2",
    "skip",  # 模板的 @pytest.mark.skip，见 make_config_skip
    "parameters",  # 模板的 @pytest.mark.parametrize
)

""" 「模型认、白名单认、生成器无分支」的 config 字段：命中即报错。

    NOTICE（0918-8 / L15）：这是 §六.1 那条「闸门只做到 step 层」的一半补齐。
    `config.thrift` / `config.db` 都在 `TConfig` 里、也都在 `KNOWN_CONFIG_FIELDS` 里
    （所以**连 known-fields 告警都没有**），但 `make_config_chain_style` 不渲染它们
    ——YAML 里写了就被静默忽略，用户以为配好了 thrift/数据库连接。

    为什么是**报错**而不是补上生成：YAML 根本无法表达 thrift / SQL 的 step
    （`thrift_request` / `sql_request` 已被 `ensure_generatable_teststeps` 明确拒绝），
    所以即使生成出 `config.thrift(...)` 也没有任何 step 会用到它——那是"看起来支持"的
    假能力。真正的用法是手写 pytest 风格用例（`Config(...).thrift()/.db()`）。

    `path` 不在此列：它是**调用方注入**的结构字段（`__make` 与引用用例都会写），
    不是用户可写的配置项，见 `STRUCTURAL_CONFIG_FIELDS`。
"""
UNSUPPORTED_CONFIG_FIELDS: Dict[Text, Text] = {
    "thrift": (
        "`config.thrift` 在 YAML 里**不会生效**：`make.py` 不渲染它，而 YAML 也无法表达\n"
        "  thrift step（`thrift_request` 同样被生成器拒绝），生成出来也没有 step 用得上。\n"
        "  正确用法：手写 pytest 风格用例，`Config(...).thrift().service_name(...)...`\n"
        "  + `Step(RunThriftRequest(...))`（见 docs/能力清单.md 的「扩展协议：Thrift」）。"
    ),
    "db": (
        "`config.db` 在 YAML 里**不会生效**：`make.py` 不渲染它，而 YAML 也无法表达\n"
        "  SQL step（`sql_request` 同样被生成器拒绝）。\n"
        "  正确用法：手写 pytest 风格用例，`Config(...).db().user(...).ip(...)...`\n"
        "  或 `RunSqlRequest(...).with_db_config(...)`"
        "（见 examples/data_management/sql/04_sql_data_management_test.py）。"
    ),
}

""" 由调用方注入的结构字段：不参与 config 层的交叉校验（不是用户可写的配置项）。"""
STRUCTURAL_CONFIG_FIELDS: Tuple[Text, ...] = ("path",)


def ensure_generatable_config(config: Dict, testcase_path: Text = "") -> None:
    """config 层的「字段可生成性」交叉校验（0918-8 / L15，§六.1 的另一半）。

    判据与 step 层完全一致：**凡是模型认了的 config 字段，生成器要么能生成，要么必须报错。**
    修复前 config 层没有任何闸门，于是 `config.thrift` / `config.db` 属于
    「模型认、白名单认、生成器不认、还不告警」——最难查的一类静默丢弃。

    配套不变量测试见 `tests/make_test.py::TestConfigFieldGeneratability`。
    """
    if not isinstance(config, dict):
        return

    for field_name, hint in UNSUPPORTED_CONFIG_FIELDS.items():
        if config.get(field_name):
            raise exceptions.ParamsError(
                f"config.{field_name}: {hint}\n"
                f"testcase: {testcase_path}\n"
                f"config 内容: {config}"
            )

    ensure_oauth2_fields_are_known(config, testcase_path)


# oauth2 子字段白名单：由模型派生（`TOAuth2.model_fields`），避免第二份手写清单漂移。
# `access_token` / `expires_at` 是运行期填充的缓存字段，不参与用例编写，但出现在
# YAML 里也不算错（只是没意义）——所以一并放行，只拦**真的不认识**的键。
def _known_oauth2_fields() -> Set[Text]:
    from interfacetester.models import TOAuth2

    return set(TOAuth2.model_fields)


def ensure_oauth2_fields_are_known(config: Dict, testcase_path: Text = "") -> None:
    """批次 B / **M10**：`config.oauth2` 的**嵌套子字段**也要交叉校验。

    NOTICE: 修复前 config 层的闸门只遍历**顶层**字段（`thrift` / `db` / `oauth2` 整体），
    嵌套子字段既不校验也不告警——pydantic 的 `extra="ignore"` 会把它们**静默丢掉**。
    最典型的两个后果（都是实测过的）：
      - `grant_type: password` 被丢 → 实际发 `client_credentials`（已改为渲染，见
        `make_config_chain_style` 的 M10 NOTICE）；
      - **拼错键名**（`clientid` / `client_secret_key` / `tokenurl`…）被丢 →
        生成物里少了 `.client_id(...)`，运行期只留一句「oauth2 配置不完整，跳过自动认证」，
        用例带着**没有认证**的请求继续跑。

    判据是白名单（从 `TOAuth2.model_fields` 派生），只拦真的不认识的键——不许假报。
    """
    oauth2 = config.get("oauth2")
    if not isinstance(oauth2, dict) or not oauth2:
        return

    known = _known_oauth2_fields()
    unknown = sorted(set(oauth2) - known)
    if not unknown:
        return

    raise exceptions.ParamsError(
        f"config.oauth2 里有**无法识别**的子字段：{unknown}，它们会被**静默忽略**"
        f"（pydantic 的 extra=\"ignore\"）。\n"
        f"  已支持的 oauth2 子字段: {sorted(known)}\n"
        f"  常见原因是**键名拼错**（例如 `clientid` 少了下划线、`tokenurl` 少了下划线）——"
        f"那样生成物里会少一个 `.client_id(...)` / `.token_url(...)`，"
        f"运行期只剩一句「oauth2 配置不完整，跳过自动认证」，用例会带着**没有认证**的请求继续跑。\n"
        f"testcase: {testcase_path}\n"
        f"config.oauth2 内容: {oauth2}"
    )


def __ensure_absolute(path: Text) -> Text:
    # NOTICE（0918-4 / H1）：`.\` 与 `./` 都只有 **2** 个字符。修复前 Windows 分支
    # 写的是 `path[3:]`，多删一个字符：`.\ref.yml` → `ef.yml`，然后以
    # `ERROR | path not exist: ef.yml` 退出——报错里的文件名和用户写的完全对不上，
    # 是那种「看一眼报错完全猜不到原因」的失败。
    if path.startswith(("./", ".\\")):
        path = path[2:]

    path = ensure_path_sep(path)
    # NOTICE: 生成用例时必须按「这个文件自己所属的项目」定位 RootDir（keep...=False），
    # 不能沿用当前已加载的别的项目的 meta（见 loader.load_project_meta 的说明）。
    project_meta = load_project_meta(path, keep_loaded_meta_for_projectless=False)

    if os.path.isabs(path):
        absolute_path = path
    else:
        absolute_path = os.path.join(project_meta.RootDir, path)

    if not os.path.isfile(absolute_path):
        # NOTICE（0918-4）：这里原来是 `sys.exit(1)`。在 pytest 进程内 / 批量 hmake 里
        # 直接退出会「杀掉整批」——其余文件的结果全部丢失（见 L8）。
        # 改成抛异常后，退出码语义由调用方（main_make）统一决定，行为更可控。
        raise exceptions.FileNotFound(f"Invalid testcase file path: {absolute_path}")

    # 0918-8 / M17：归一化（`.\x`、`a\..\b`、重复分隔符都吃掉）。
    # 下游用 `relative_to_root_dir` 派生生成文件名与类名，脏路径会让派生结果从中间截断
    # （实测 `hmake .\nested\case.yml` → 生成物落到 `nested\d\case_test.py`）。
    return os.path.normpath(absolute_path)


def normalize_module_segment(name: Text) -> Text:
    """把一个**源用例路径的路径段**归一成合法的 pytest 模块段（`ensure_file_abs_path_valid` 的规则）。

    NOTICE（批次 9-1 / 漂移收口）：这段规则原先只内联在 `ensure_file_abs_path_valid` 里，
    而「源文件名 → 生成物模块名」这条映射在别处还被**重新推导**过一次
    （`cli.main_convert` 的写盘冲突护栏用的是 `emit_yaml.slugify`），于是同一个源目录
    会得到两套名字，冲突只在更晚的阶段（`make_testcase` 的生成物撞名检查）才被发现。
    现在规则只在这里一份，两个护栏都调它。

    NOTICE: 这里**保持逐字节不变**（含「数字开头补 `T`」「`.` 开头的段不动」两条特例）——
    34 个已入库生成物的文件名与类名都由它派生，
    见 `tests/generated_artifacts_drift_test.py`；改这里等于批量改生成物。

    刻意**不**与 `emit_yaml.slugify` 合并：两者管的是**不同的东西**——
      - 本函数：`<源用例路径>` → `<生成物模块路径>`（要满足 Python import 的约束，
        所以要把 `-`/`.`/空格都折成 `_`，还要给数字开头的段补 `T`）；
      - `emit_yaml.slugify`：`<展示用用例名>` → `<输出 YAML 文件名>`（用户可见，
        保留大小写与 `-`/`.`，只挡掉真正的非法字符与空白）。
    两者**都不是** schema 文件名规则（那是 `ir.safe_file_stem`，见 H5）。

    NOTICE（批次 B / **M1**）：**"折掉空格/点/连字符"并不等于"合法标识符"**。
    修复前这里只处理那三个字符，于是文件名/目录名里出现 `+ @ & ' ( ) ! % , ; = [ ] ^ ` { } ~ $ #`
    等 20 个可见 ASCII 时，派生出来的模块段不是标识符（实测 95 个可见 ASCII 里恰好 20 个中招），
    后果是双向的（两个方向都实测过）：
      - `hmake case+1.yml` 生成 `class TestCaseCase+1(InterfaceTester):` → **SyntaxError**，
        而 `hmake` 的退出码是 **0**（"假成功"：产物根本 import 不了）；
      - `hconvert` 因为**合法的 Postman folder 名**（`Auth + Token`）整体 `exit 1` ——
        `validate_emitted_case` 最后一步的 `compile()` 抓到同一个根因（"假失败"）。
    现在：先把不能出现在标识符里的字符折成 `_`（用 Python 自己的判据 `f"a{ch}".isidentifier()`，
    因此**中文/日文等非 ASCII 字母照旧保留**，与既有行为一致），再挡住**关键字**
    （`class`/`import` 这类段会让 `from class.leaf_test import …` 直接语法错误）。

    NOTICE: **兼容性**——对既有取值逐字节不变：`用户 管理` / `a-b` / `19` / `1.2.3 report` / `查询`
    等规则本来就能折干净的段一个字符都不动（34 个已入库生成物的文件名与类名由它派生，
    护栏是 `tests/generated_artifacts_drift_test.py`）。
    """
    if not name:
        # 0918-8 / M17：重复分隔符会切出空段，原来在 `name[0]` 直接
        # `IndexError: string index out of range`（实测 `hmake nested//case.yml`）。
        # 入口已经归一化，这里是**库函数的兜底**（`ensure_file_abs_path_valid` 是公开函数）。
        return ""

    if name[0] in string.digits:
        # ensure file name not startswith digit
        # 19 => T19, 2C => T2C
        name = f"T{name}"

    if name.startswith("."):
        # avoid ".csv" been converted to "_csv"
        return name

    # handle cases when directory name includes dot/hyphen/space
    normalized = name.replace(" ", "_").replace(".", "_").replace("-", "_")

    # 批次 B / M1：把**其余**不能出现在标识符里的字符也折成 `_`。
    # 判据用 Python 自己的：`f"a{ch}".isidentifier()` 为真表示该字符能作为标识符的后续字符
    # （字母/数字/下划线/非 ASCII 字母都算），否则折掉。
    normalized = "".join(ch if f"a{ch}".isidentifier() else "_" for ch in normalized)

    # 关键字（`class`/`import`/`for`…）是合法标识符但**不能做模块段**：
    # `from class.leaf_test import …` 是语法错误。加一个下划线后缀（与 `.csv` 的既有特例同风格）。
    if keyword.iskeyword(normalized):
        normalized = f"{normalized}_"

    return normalized


def ensure_file_abs_path_valid(file_abs_path: Text) -> Text:
    """ensure file path valid for pytest, handle cases when directory name includes dot/hyphen/space

    Args:
        file_abs_path: absolute file path

    Returns:
        ensured valid absolute file path

    """
    project_meta = load_project_meta(
        file_abs_path, keep_loaded_meta_for_projectless=False
    )
    raw_abs_file_name, file_suffix = os.path.splitext(file_abs_path)
    file_suffix = file_suffix.lower()

    # NOTICE: 这里相对化的是「去掉扩展名的派生路径」，不能再用
    # convert_relative_project_root_dir()（它会按这个不存在的路径重新定位项目）。
    raw_file_relative_name = relative_to_root_dir(raw_abs_file_name, project_meta)
    if raw_file_relative_name == "":
        return file_abs_path

    path_names = []
    for name in raw_file_relative_name.rstrip(os.sep).split(os.sep):
        normalized = normalize_module_segment(name)
        if not normalized:
            continue
        path_names.append(normalized)

    new_file_path = os.path.join(
        project_meta.RootDir, f"{os.sep.join(path_names)}{file_suffix}"
    )
    return new_file_path


def ensure_generated_file_may_be_overwritten(
    testcase_python_abs_path: Text, testcase_path: Text
) -> None:
    """拒绝用生成物**覆盖手写的** `*_test.py`（0920 批次 6 / **N40**）。

    ## 修复前的现场

    `make_testcase` 无条件 `open(..., "w")` —— 手写的 `case_test.py` 被**静默覆盖**，
    没有告警、没有备份、exit 0。实测（`.tmp_audit/n40_check.py`）：

    ```text
    覆盖前: # hand-written, NOT generated\\ndef test_important(): assert True
    覆盖后: # NOTE: Generated By InterfaceTester 4.3.5\\n# FROM: …
    ```

    ## 为什么这条该修（本仓自己给了判据）

    `conftest.py` **早就有**同样的保护（`compat.py` 的 `--save-tests` 逻辑明确写着
    "修复前会**直接覆盖**，用户手写的 fixture 会永久丢失"）。
    而 `*_test.py` 恰恰是**更容易被手写**的那个文件名 —— 保护却只做了 `conftest.py`。

    ## 判据：看文件**首行**是不是生成器标记

    生成物的固定特征是注释 `# NOTE: Generated By InterfaceTester {version}`，
    就写在文件最开头（`__TEMPLATE__` 的第一行）。所以：

    - 文件**不存在** → 放行（首次生成）；
    - 文件存在、且**首行**是该标记 → 放行（这是我们自己上次生成的，重跑应该覆盖）；
    - 文件存在、且**首行不是**该标记 → 判定为手写，**报错**并给出改法。

    NOTICE（0921-2 / **发现 3**，判据从"子串"收紧成"首行"）：修复前判据是
    `"Generated By InterfaceTester" in head`（前 400 字节的子串匹配）——
    一个手写文件只要在开头**提到**这句话就会被放行覆盖。理由与实测反例见
    `GENERATED_FILE_MARKER_PREFIX` 的 NOTICE（`compat.is_generated_conftest`
    早就论证过子串匹配是错的，这里对齐它）。

    NOTICE（为什么报错而不是只告警）：覆盖是**不可逆**的数据丢失，
    与本仓对"静默丢数据"的一贯口径一致；而合法的重跑路径（首行含标记）不受影响，
    所以不会挡住正常的 `hmake` 工作流（仓库 34 个已入库生成物的标记**都在第 1 行**）。
    """
    if not os.path.isfile(testcase_python_abs_path):
        return

    try:
        with open(testcase_python_abs_path, encoding="utf-8", errors="replace") as f:
            # 只看**首行**：标记就是生成物的第 1 行（`__TEMPLATE__` 的第一行）
            first_line = f.readline().strip()
    except OSError:
        # 读不了（权限等）→ 交给后面的写入去报错，这里不制造假报
        return

    # NOTICE（0921-2 / 发现 3）：判据是**首行严格匹配**，不是子串匹配。
    # 修法理由与实测反例见 `GENERATED_FILE_MARKER_PREFIX` 处的 NOTICE。
    if first_line.startswith(GENERATED_FILE_MARKER_PREFIX):
        return

    raise exceptions.ParamsError(
        f"目标生成物已存在，且**看起来是手写的**（没有生成器标记）：\n"
        f"  生成物: {testcase_python_abs_path}\n"
        f"  源用例: {testcase_path}\n"
        f"  生成物开头: {first_line[:80]!r}\n"
        f"  本框架拒绝覆盖它 —— 那会**永久丢失**你手写的代码（无备份、无撤销）。\n"
        f"  改法（三选一）：\n"
        f"  1) 手写用例请换个文件名（例如 `{os.path.splitext(os.path.basename(testcase_python_abs_path))[0]}_manual.py`）；\n"
        f"  2) 确实要重新生成 → 先删除或改名这个文件，再跑 `hmake`；\n"
        f"  3) 想改用例逻辑 → 改**源 YAML**（`{testcase_path}`），生成物本来就不该手改。\n"
        f"  hint: 生成物第一行是 `# NOTE: Generated By InterfaceTester <version>`；"
        f"有这个标记的文件会被正常覆盖（重跑不受影响）。"
    )


def __ensure_testcase_module(path: Text):
    """ensure pytest files are in python module, generate __init__.py on demand"""
    init_file = os.path.join(os.path.dirname(path), "__init__.py")
    if os.path.isfile(init_file):
        return

    with open(init_file, "w", encoding="utf-8") as f:
        f.write("# NOTICE: Generated By InterfaceTester. DO NOT EDIT!\n")


def _project_module_prefix(parent_root_dir: Text, ref_root_dir: Text) -> Text:
    """给「另一个项目的被引用用例」算一个**模块名前缀**（0920 批次 3 / N7+N8）。

    目标：让生成物里的模块名在**整个运行环境内唯一**，从而
      · 不再出现两个项目同名文件都映射到 `child_test`（N7：执行了错用例）；
      · 不再依赖"hmake 泄漏的 `sys.path`"（N8：换进程 import 失败）。

    取值规则（优先可读、其次唯一）：
      1. 两个项目根是**兄弟目录**（同一个父目录）→ 直接用被引用项目的目录名
         （`.../xproj/projB` → `projB`）。这是 monorepo 里最常见的形态；
      2. 否则 → 用「相对父项目根**再上一级**」的路径段（这样 `../projB` 会变成 `projB`），
         非法字符交给 `normalize_module_segment`；
      3. 兜底：路径算不出可用段时，用项目名 + 路径哈希，保证**一定唯一**。

    NOTICE（必须产出**普通标识符**，不能有 `..`/`.`）：生成物里写的是
    `from {prefix}.{module} import ...` —— 前缀一旦以 `.` 开头就变成**相对导入**，
    而生成物本身是顶层模块（pytest 以 rootdir 收集），会直接
    `ImportError: attempted relative import beyond top-level package`
    （实测：第一版用 `relpath` 得到的 `../projB` 被归一成 `_projB`，生成物立刻不可导入）。
    所以这里一律把段拼成 `a_b` 形式，并靠生成物里的 `sys.path.insert` 定位项目根。
    """
    parent_abs = os.path.abspath(parent_root_dir)
    ref_abs = os.path.abspath(ref_root_dir)

    # 1) 兄弟目录：用目录名（最可读）
    if os.path.normcase(os.path.dirname(ref_abs)) == os.path.normcase(parent_abs):
        segment = normalize_module_segment(os.path.basename(ref_abs))
        if segment:
            return segment

    # 2) 一般情况：相对「父项目根的上一级」的路径段
    #    （兄弟目录已在上一步处理；这里覆盖嵌套/多层目录的形态）
    for base in (os.path.dirname(parent_abs), parent_abs):
        try:
            relative = os.path.relpath(ref_abs, base)
        except ValueError:  # Windows 跨盘符
            continue
        if relative.startswith(".."):
            continue
        segments = [
            normalize_module_segment(part)
            for part in relative.replace("\\", "/").split("/")
            if part and part not in (".", "..")
        ]
        segments = [segment for segment in segments if segment]
        if segments:
            return "_".join(segments)

    # 3) 兜底：项目名 + 路径哈希（一定唯一，只是不好看）
    digest = hashlib.md5(ref_abs.encode("utf-8")).hexdigest()[:8]  # noqa: S324
    return f"{normalize_module_segment(os.path.basename(ref_abs)) or 'proj'}_{digest}"


def __ref_testcase_alias(
    alias_by_path: Dict[Text, Text],
    used_aliases: Dict[Text, Text],
    ref_testcase_abs_path: Text,
    cls_name: Text,
    ref_relative_script_path: Text,
) -> Text:
    """为被引用用例挑选一个**在本文件内唯一**的 import 别名（0918-8 / H9）。

    规则（刻意保证「不撞名就一字不改」，避免 34 个已入库生成物漂移）：
      1. 同一个被引用用例被引用多次 → 复用第一次的别名（不是冲突）；
      2. `cls_name` 尚未被本文件用过 → 就用它（与修复前**完全一致**）；
      3. 真的撞名（`a/login.yml` 与 `b/login.yml` 都叫 `Login`）→ 用「相对目录 + 原名」消歧
         （`a` + `Login` → `ALogin`），并在日志里说明，提示可以改名避免歧义；
      4. 极端情况（消歧后仍撞，如同一目录的不同大小写写法）→ 追加序号兜底。
    """
    cached = alias_by_path.get(ref_testcase_abs_path)
    if cached is not None:
        return cached

    # NOTICE（残留缺陷：别名撞上 Python **关键字** → 生成物是语法错误的）：
    # 被引用用例的类名是「`TestCase` + 文件名首字母大写」，于是
    # `none.yml` / `true.yml` / `false.yml` 派生出的别名正好是关键字 `None` / `True` / `False`：
    #     from none_test import TestCaseNone as None      # SyntaxError: invalid syntax
    #     ... RunTestCase("ref").call(None)
    # 与批次 B/29（`normalize_module_segment` 挡关键字）是同一类问题，只是那次的判据
    # 只作用在**类名/模块名**上，管不到这里的 **import 别名**（走的是另一条拼装路径）。
    # 实测（`.tmp_audit2/p14_refcase.py`，逐字对比）：`child.yml` / `login.yml` /
    # `class.yml`(`Class` 不是关键字) / `config.yml`(撞模板保留名 → `ConfigRef`) 都能编译，
    # 唯独 `none.yml` / `true.yml` 编译失败。
    # 处置沿用本函数既有的口径：**只在真的会坏时**加 `Ref` 后缀
    # （与撞名消歧用的 `ConfigRef` 同款），因此既有可用生成物零漂移。
    if keyword.iskeyword(cls_name):
        logger.warning(
            f"被引用用例的类名 {cls_name} 是 Python 关键字，"
            f"import 别名改为 {cls_name}Ref（否则生成物是 SyntaxError）。"
            f"想避免改名，给被引用用例换个不撞上关键字的文件名即可：{ref_testcase_abs_path}"
        )
        cls_name = f"{cls_name}Ref"

    alias = cls_name
    if alias in used_aliases:
        directory = os.path.dirname(ref_relative_script_path)
        sanitized = "".join(ch if ch.isalnum() else "_" for ch in directory)
        prefix = "".join(
            part[:1].upper() + part[1:] for part in sanitized.split("_") if part
        )
        alias = f"{prefix}{cls_name}" if prefix else f"{cls_name}Ref"
        logger.info(
            f"被引用用例的类名撞名，已用目录前缀消歧："
            f"{used_aliases.get(cls_name)} 与 {ref_testcase_abs_path} 都叫 {cls_name} "
            f"→ 本文件内分别用不同别名（后者为 {alias}）。\n"
            f"  NOTICE: 修复前后一条 import 会**遮蔽**前一条，两个 step 都调用同一个类，"
            f"前面的用例被静默替换。想避免这种消歧，给其中一个用例改名即可。"
        )

    candidate = alias
    index = 2
    while candidate in used_aliases:
        candidate = f"{alias}{index}"
        index += 1

    alias_by_path[ref_testcase_abs_path] = candidate
    used_aliases[candidate] = ref_testcase_abs_path
    return candidate


def convert_testcase_path(testcase_abs_path: Text) -> Tuple[Text, Text]:
    """convert single YAML/JSON testcase path to python file"""
    testcase_new_path = ensure_file_abs_path_valid(testcase_abs_path)

    dir_path = os.path.dirname(testcase_new_path)
    file_name, _ = os.path.splitext(os.path.basename(testcase_new_path))
    testcase_python_abs_path = os.path.join(dir_path, f"{file_name}_test.py")

    # convert title case, e.g. request_with_variables => RequestWithVariables
    name_in_title_case = file_name.title().replace("_", "")

    return testcase_python_abs_path, name_in_title_case


# 调用 black 的超时上限（秒），可用环境变量 INTERFACETESTER_BLACK_TIMEOUT 覆盖。
# black 挂住时 hmake/hrun 会永久挂死在 subprocess.run 上，必须给它一个上限。
BLACK_TIMEOUT_SECONDS = 60
BLACK_TIMEOUT_ENV = "INTERFACETESTER_BLACK_TIMEOUT"

# 传给 black 的 `--target-version`，可用环境变量覆盖（设为 none/空 表示不传）。
#
# NOTICE（0918-5 / M12）：为什么必须显式传，以及为什么取 py38 而不是当前解释器的版本。
#
# 1) **不传会刷警告**。black 26.x 默认会「从 pyproject.toml 的项目元数据推断 target 版本」，
#    而本仓库 `requires-python = ">=3.8"` **没有上界** → 推断出 black 支持的全部版本
#    （实测 py38..py315）→ 最大值比运行解释器新 → 每次格式化都打印：
#      Warning: Python 3.12 cannot parse code formatted for Python 3.15. To fix this:
#      run Black with Python 3.15, set --target-version to py312, or use --fast ...
#    实测该警告**纯粹是噪声**：传不传 flag，仓库里三个真实生成物的输出**逐字节相同**
#    （生成物里没有会随 target 变化的结构），black 退出码也一直是 0。
#    触发条件还要同时满足「有 .git」+「根上有 pyproject.toml」——实测逐个变量隔离确认：
#      有 pyproject 但无 .git → 不警告；有 .git 但无 pyproject → 不警告；两者都有 → 警告。
#    所以它在纯临时目录里复现不出来，必须在 git 检出里才看得到。
#
# 2) **为什么是 py38**：
#    - `pyproject.toml` 声明 `requires-python = ">=3.8"`，而 black 的声明下限是
#      `black>=22.3`——22.3 的 TargetVersion 里就有 PY38，因此这个取值对**所有被允许的
#      black 版本**都合法，不需要探测 black 支持哪些版本，也就不会随 black 升级漂移；
#    - 语义上也对得上：生成物要能在项目声明支持的最低 Python 上跑；
#    - 反面方案是取「当前解释器版本」（black 自己的提示就是这么建议的），但那需要在进程内
#      `import black` 读 `TargetVersion` 做能力探测，实测约 **105 ms**，而单文件 hmake
#      全程约 690 ms（+15%）——为消一条纯噪声不值得。
#    - 万一将来的 black 移除了 PY38：失败是**非致命**的（`format_pytest_with_black` 会
#      告警并跳过格式化，hmake 继续）；实测 black 26.5 的 TargetVersion 里连 PY33 都还在。
BLACK_TARGET_VERSION = "py38"
BLACK_TARGET_VERSION_ENV = "INTERFACETESTER_BLACK_TARGET_VERSION"
# 显式关闭的取值（回到「让 black 自己推断」的原行为）
BLACK_TARGET_VERSION_DISABLED = {"", "none", "off", "false", "0"}

# black 的缓存目录开关与其默认落点（见 get_black_env）
BLACK_CACHE_DIR_ENV = "BLACK_CACHE_DIR"
BLACK_CACHE_DIR_NAME = "interfacetester-black-cache"


def get_black_timeout() -> float:
    """获取 black 调用超时（秒）：优先环境变量，未设置用默认值，非法/非正值告警后回落。"""
    raw_value = os.environ.get(BLACK_TIMEOUT_ENV)
    if raw_value is None or raw_value.strip() == "":
        # 未显式配置 → 静默使用默认值
        return float(BLACK_TIMEOUT_SECONDS)

    try:
        timeout = float(raw_value)
    except ValueError:
        logger.warning(
            f"invalid {BLACK_TIMEOUT_ENV}: {raw_value!r}, "
            f"fallback to {BLACK_TIMEOUT_SECONDS}s"
        )
        return float(BLACK_TIMEOUT_SECONDS)

    if timeout <= 0:
        # 0 或负数不是合法超时（也不是「无限等待」的合法表达）→ 回落默认值
        logger.warning(
            f"invalid {BLACK_TIMEOUT_ENV}: {raw_value!r}, "
            f"fallback to {BLACK_TIMEOUT_SECONDS}s"
        )
        return float(BLACK_TIMEOUT_SECONDS)

    return timeout


def get_black_target_version() -> Optional[Text]:
    """获取传给 black 的 `--target-version` 取值；返回 None 表示不传该参数。

    优先级：环境变量 `INTERFACETESTER_BLACK_TARGET_VERSION` → 默认 ``BLACK_TARGET_VERSION``。
    显式设为 ``none``/``off``/``false``/``0``/空串 时返回 None（回到 black 自己的推断）。

    取值理由见 ``BLACK_TARGET_VERSION`` 上方的 NOTICE（M12）。
    """
    raw_value = os.environ.get(BLACK_TARGET_VERSION_ENV)
    if raw_value is None:
        return BLACK_TARGET_VERSION

    value = raw_value.strip()
    if value.lower() in BLACK_TARGET_VERSION_DISABLED:
        return None

    return value


def get_black_command(*python_paths: Text) -> List[Text]:
    """构造 black 命令：优先用当前解释器里的 black，回落到 PATH 上的 black。

    NOTICE: 修复前固定调用 PATH 上的 ``black``（``subprocess.run(["black", ...])``），
    未激活 venv 时报「missing dependency tool: black」——但 black 是本项目声明的运行时
    依赖（pyproject.toml 的 dependencies），理应用当前解释器（即装了这个包的那个环境）
    里的那一份。仅当当前解释器确实没装 black 时，才回落到 PATH，兼容把 black 装在
    别处的环境。

    NOTICE（0918-5 / M12）：中间插入显式的 ``--target-version``（默认 py38），
    避免 black 从项目元数据推断出「无上界」的版本集合后每次刷版本警告。
    理由与取值见 ``BLACK_TARGET_VERSION``。
    """
    target_version = get_black_target_version()
    version_args = (
        ["--target-version", target_version] if target_version else []
    )

    if importlib.util.find_spec("black") is not None:
        return [sys.executable, "-m", "black", *version_args, *python_paths]

    return ["black", *version_args, *python_paths]


def get_black_env() -> Dict[Text, Text]:
    """构造 black 子进程的环境变量：把它的缓存目录指到可写位置。

    NOTICE（0916-8 实测）：
    - black 的缓存目录默认在用户缓存目录（black 内部用
      ``platformdirs.user_cache_dir("black")``）；受限环境（只读 HOME、企业锁定机器、
      CI 只读缓存盘）里写不进去时，black 不是报错而是**卡住**，进而让 hmake/hrun 一直等；
    - 更干净的 ``--no-cache`` 只在较新的 black 里才有（实测 22.3/22.8/23.1/23.7/24.1
      的 wheel 里都没有该选项，本项目 pyproject 的下限正是 22.3），因此改用**所有版本都支持**
      的 ``BLACK_CACHE_DIR``：hmake 每次格式化的是刚生成的文件，缓存收益本就极小，
      指到临时目录即可彻底避开「写用户缓存目录」这条失败路径；
    - 用户已显式设置 BLACK_CACHE_DIR 时尊重用户设置，不覆盖。
    """
    env = dict(os.environ)
    if env.get(BLACK_CACHE_DIR_ENV):
        return env

    env[BLACK_CACHE_DIR_ENV] = os.path.join(
        tempfile.gettempdir(), BLACK_CACHE_DIR_NAME
    )
    return env


def run_black(timeout: float, *python_paths: Text) -> None:
    """执行一次 black（带超时，缓存目录指到可写位置）。

    - ``stdin=subprocess.DEVNULL``：避免继承了异常的 stdin 时阻塞；
    - ``env=get_black_env()``：避免 black 卡在不可写的用户缓存目录上；
    - black 自身的输出仍直连当前进程，用户看到的提示与修复前一致。
    """
    if not python_paths:
        return

    subprocess.run(
        get_black_command(*python_paths),
        check=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=get_black_env(),
    )


def format_pytest_with_black(*python_paths: Text):
    """用 black 格式化生成的 pytest 用例（超时/失败都只告警，不阻断 hmake）。

    NOTICE: 这里对 black 的三重加固（详见 docs/待处理项方案.md 批次 1）：
    1. 全程带超时（默认 60s，见 ``get_black_timeout``）：修复前 ``subprocess.run`` 没有
       timeout，black 一旦挂住（例如缓存目录不可写时 black 会卡在写缓存上），
       hmake/hrun 就永久挂死且没有任何提示；
    2. 优先用当前解释器里的 black（见 ``get_black_command``），不再强依赖 PATH；
    3. 多文件一次调用失败时逐个文件重试：修复前逐个格式化的分支是列表推导，
       第一个文件报错后续文件就全部不再格式化。
    """
    if not python_paths:
        return

    logger.info("format pytest cases with black ...")
    timeout = get_black_timeout()

    try:
        run_black(timeout, *python_paths)
        return
    except subprocess.TimeoutExpired:
        logger.warning(
            f"black 超时（{timeout}s），已跳过格式化：生成的 _test.py 语法正确但未格式化。\n"
            "hint: 生成的用例可直接运行；缓存目录已由框架指到临时目录，"
            f"若仍超时可用 {BLACK_TIMEOUT_ENV}（秒）调整本超时上限。"
        )
        return
    except subprocess.CalledProcessError as ex:
        logger.warning(
            f"black formatting failed (exit code {ex.returncode}), skip formatting: {ex}"
        )
    except OSError:
        logger.warning(
            "missing dependency tool: black, skip formatting; "
            "install with: pip install black"
        )
        return

    # 整体格式化失败（常见原因：某个生成文件有语法错误）→ 逐个文件重试，
    # 避免「一个文件失败导致其余文件都不格式化」
    if len(python_paths) <= 1:
        return

    logger.warning("format files one by one ...")
    for path in python_paths:
        try:
            run_black(timeout, path)
        except subprocess.TimeoutExpired:
            logger.warning(f"black 超时（{timeout}s），停止逐个格式化：{path}")
            return
        except subprocess.CalledProcessError as ex:
            logger.warning(f"failed to format {path}: {ex}")
        except OSError:
            logger.warning(
                "missing dependency tool: black, skip formatting; "
                "install with: pip install black"
            )
            return


def ensure_python_literal(
    value: Any, prefer_double_quote: bool = True, where: Text = ""
) -> Text:
    """把值渲染成合法的 Python 字面量文本（生成的 _test.py 里直接用）。

    NOTICE: 修复前是手写引号拼接（如 ``f'"{value}"'``），只要值里出现引号、
    反斜杠或换行，生成的 _test.py 就是语法错误（SyntaxError），用户拿到的是
    一个完全不能用的用例文件。

    NOTICE（0918-8 / M19）：这里同时是**所有渲染调用的唯一漏斗**，所以字面量可复原性
    检查放在这里——修复前只有「以 `**kwargs` 落盘」的那几处（variables/params/headers/
    cookies/proxies/upload）做了检查，`json` / `data` / `cert` / `x_path` / `validate` 的
    期望值**全都漏了**：

        实测 `request: {json: {when: 2020-01-01}}` → 生成 `.with_json({"when": datetime.date(2020, 1, 1)})`
             → hmake **exit 0、零告警**，而生成的文件在 pytest 收集期就 `NameError: name 'datetime' is not defined`
             （`validate: - eq: [body.when, 2020-01-01]` 同理）。

    Args:
        value: 待渲染的值，通常是 str/int/float/bool/None/list/dict。
        prefer_double_quote: 字符串是否优先用双引号输出，默认 True。
            生成的测试文件历史上统一用双引号（仓库里的 examples/**/*_test.py 已提交），
            为保持输出风格一致，这里用 ``json.dumps`` 渲染字符串——JSON 字符串是
            Python 字符串字面量的子集，转义规则兼容且一定合法。
        where: 告警里的「位置」文案（调用方有更好的上下文就传进来，便于用户定位）。

    Returns:
        Python 字面量文本。
    """
    __ensure_value_literal(value, where or "用例里", "")

    # NOTICE（批次 E / **L8**）：`repr()` 本身也可能抛 —— 典型是超大整数
    # （CPython 3.11+ 的 int→str 有 4300 位上限）与自定义对象的坏 `__repr__`。
    # 修复前它会以裸 `ValueError: Exceeds the limit (4300 digits) …` 冒到 hmake，
    # 报错里没有「哪个用例、哪个字段」。现在收口成点名报错并给出可操作改法。
    try:
        if prefer_double_quote and isinstance(value, Text):
            return json.dumps(value, ensure_ascii=False)
        return repr(value)
    except Exception as ex:  # noqa: BLE001 - 任何渲染失败都要变成可读报错
        raise exceptions.ParamsError(
            f"{where or '用例里'} 的值无法渲染成 Python 字面量："
            f"{_safe_repr(value)}（类型：{type(value).__name__}）\n"
            f"  根因: {type(ex).__name__}: {ex}\n"
            "  修法：把它改成字符串（YAML 里加引号），或换一个能表示的值。"
        ) from ex


# --------------------------------------------------------------------------- 强转回写（0918-8 / M18）
# 只有**标量**（bool / 数值）才需要回写，理由见 `__coerce_scalar_fields_into` 的 NOTICE。
_SCALAR_FIELD_TYPES = (bool, int, float)


def __annotation_is_scalar(annotation: Any) -> bool:
    """字段注解是不是「bool / 数值」标量（含 `Optional[...]` / `Union[...]` 递归）。"""
    if annotation in _SCALAR_FIELD_TYPES:
        return True
    return any(__annotation_is_scalar(arg) for arg in get_args(annotation))


def __coerce_scalar_fields_into(raw: Dict, model_obj: Any, model_cls: Any) -> None:
    """把模型**强转过的标量值**回写进「渲染用的原始 dict」（原地修改）。

    NOTICE（0918-8 / M18）：修复前是「**校验**用模型、**渲染**用原始 dict」——
    pydantic 已经把手写的字符串转成了正确类型，渲染却仍读原始字符串：

        实测 `config: {verify: "false"}` → 生成 `.verify("false")`
             （**非空字符串 = 真值**，于是 TLS 校验反而被打开，与用户意图相反）；
        实测 `request: {timeout: "30"}` → 生成 `.set_timeout("30")`
             （requests 会在运行期报错）；
        实测 `retry_times: "3"` → 生成 `.with_retry(retry_times="3")`。

    **只在类型真的不对（写成了字符串）时回写**：本来写对的 YAML 一个字符都不会变，
    因此既有生成物**零漂移**（由 `tests/generated_artifacts_drift_test.py` 守着）。
    刻意只处理标量：dict/list 字段回写会把「没写的键」变成默认值，
    进而给每个请求都生成 `.with_params(**{})` 之类的噪声。
    """
    for name, field in model_cls.model_fields.items():
        if name not in raw or not __annotation_is_scalar(field.annotation):
            continue

        if not isinstance(raw[name], Text):
            # 类型本来就对（`false` / `30`）→ 不动，保证零漂移
            continue

        coerced = getattr(model_obj, name, None)
        if coerced is None:
            continue

        raw[name] = coerced


def ensure_kwargs_values_literal(mapping: Any, where: Text) -> None:
    """校验「以 **kwargs 落盘」的映射：键必须是字符串，值必须能写成 Python 字面量。

    NOTICE: ``**{...}`` 形式的键最终是 Python 关键字参数，非字符串键会在导入生成文件时
    直接抛 ``TypeError: keywords must be strings``；而 ``repr()`` 只对字面量类型
    （str/int/float/bool/None/list/tuple/dict 等）保证可复原——YAML 里未加引号的日期
    会被解析成 ``datetime.date``，repr 输出 ``datetime.date(2020, 1, 1)``，
    生成的 _test.py 没有 import datetime，一导入就 NameError。

    这里在 hmake 阶段就把问题暴露出来，而不是等用户跑用例时才看到莫名其妙的报错。
    """
    if not isinstance(mapping, dict):
        logger.warning(
            f"{where} 不是键值对形式（实际类型：{type(mapping).__name__}），"
            "生成的测试文件里会以 **{...} 展开，导入时会报 TypeError；"
            "请在 YAML 里改成键值对写法（debugtalk.py 函数也需返回 dict）"
        )
        return

    for key, value in mapping.items():
        if not isinstance(key, Text):
            logger.warning(
                f"{where} 的键 {key!r} 不是字符串，生成的 **{{...}} "
                "会在导入测试文件时抛 TypeError: keywords must be strings；"
                "请在 YAML 里给键加上引号"
            )
            continue

        # NOTICE（0918-8 / M19）：值的「字面量可复原性」检查**已上移到唯一漏斗**
        # `ensure_python_literal()`（每个调用点紧接着就会渲染这个映射），
        # 所以这里不再重复校验——否则同一个坏值会打出两条告警。


# 生成物（`*_test.py`）里**不需要任何 import 就能用**的名字集合。
# NOTICE（残留缺陷修复）：判断「渲染出来的表达式能否导入」只能以这份为准——
# 生成物的模板只会 import `pytest` / `interfacetester` / `typing`，
# 所以 `datetime.date(...)`（YAML 未加引号的日期）这类表达式落盘即 NameError，
# 而 `frozenset({...})` 这类**内建**调用是安全的，不能一起拦。
_BUILTIN_NAMES = frozenset(dir(builtins))


def __ensure_value_literal(value: Any, where: Text, key_path: Text) -> None:
    """递归校验值（含嵌套 dict/list）能否用 Python 字面量表示，不能则告警。

    NOTICE（0918-8 / M19）：从 `ensure_python_literal()` 调用，覆盖**所有**渲染路径。

    NOTICE（**残留缺陷：只告警不阻断 → 落盘一个 import 必炸的文件**）：
    修复前这条路径 `hmake` **退出码 0、产物照样写出去**，而生成物里是
    `{"when": datetime.date(2020, 1, 1)}` 这种引用了**未 import 模块**的表达式，
    `import` 阶段直接 `NameError: name 'datetime' is not defined`。实测覆盖 7 种写法
    （`request.json` 直/嵌套、`params`、`data`、`config.variables`、`validate` 期望值、
    非字符串键的 `**{...}`；`.nan` / `.inf` 同理——它们渲染成裸名字 `nan` / `inf`）。

    这与同文件 `ensure_generated_python_is_valid()` 写死的承诺直接冲突——那句承诺是
    「**只保证写出去的 `*_test.py` 一定能被 import**」，而 `compile()` 只查语法、
    查不出「名字没有 import」。批次 B/29 已经把同类问题（类名里的 `+ @`）升级成
    「阻止落盘 + exit 1」，本函数当时只做到「告警」，于是留下同一个失败模式的尾巴。

    现在的判据（比「`ast.literal_eval` 能否复原」更精确）：
      - 渲染文本里出现了**内建名字之外**的标识符（`datetime.date(...)`、裸 `nan`）
        → 生成物**必然** NameError → 点名报错并阻止落盘；
      - 只是 `literal_eval` 不认但**能正常求值**的形态（`frozenset({...})` 这类
        内建调用）→ 维持原有「告警不阻断」，不改变任何现在能跑的用例。
    """
    if isinstance(value, dict):
        for sub_key, sub_value in value.items():
            child = f"{key_path}.{sub_key}" if key_path else f"{sub_key}"
            __ensure_value_literal(sub_value, where, child)
        return

    if isinstance(value, (list, tuple, set, frozenset)):
        for index, element in enumerate(value):
            child = f"{key_path}[{index}]" if key_path else f"[{index}]"
            __ensure_value_literal(element, where, child)
        return

    if __is_python_literal(value):
        return

    location = f"{where} 中 {key_path} 的值" if key_path else f"{where}："
    message = (
        f"{location} {_safe_repr(value)} 无法用 Python 字面量表示"
        f"（类型：{type(value).__name__}），生成的测试文件导入时会报 NameError。"
        f"请在 YAML 里给该值加引号改成字符串（如 '2020-01-01'）。"
    )
    logger.warning(message)

    if __rendered_literal_is_usable(value):
        # 渲染出来的表达式能 import（例如内建类型的构造调用）——维持「只告警」。
        return

    # NOTICE（本批修复：**必须带上异常链** `from ex`）。
    # 判据 `__rendered_literal_is_usable()` 内部把 `repr()` 的 `ValueError`（CPython 3.11+
    # 的 int↔str 4300 位上限）吞成了 `return False`，于是「根因」在报错里彻底消失：
    # 超长整数与「未加引号的日期」拿到的是**同一句话**（都指向"给值加引号"），
    # 而前者真正的根因是 `ValueError: Exceeds the limit (4300 digits) for integer
    # string conversion`——它才是用户需要看到的（也解释了为什么 5000 位整数不行、
    # 4000 位却可以）。护栏 `TestBatchEHugeIntegerLiteral` 明确要求 `__cause__` 是
    # `ValueError`，这里把根因挂回去。
    #
    # 取根因的方式是**重新求值一次 repr()**（而不是改 `__rendered_literal_is_usable`
    # 的签名去回传），因为那个函数还有第二类 `return False`（语法不合法/非内建名字），
    # 它们**没有**异常可挂——回传签名会让「没有根因」这件事也要编一个假异常。
    # 这次 `repr()` 若仍抛，异常本身就是要的根因；若不抛（说明是另外两类），
    # `cause` 保持 None，报错文案照旧。
    cause = None
    try:
        repr(value)
    except Exception as ex:  # noqa: BLE001 - 这里就是要拿这个根因
        cause = ex

    error = exceptions.ParamsError(
        f"{message}\n"
        f"  已阻止生成该用例文件（hmake 退出码 1）：写出去也是一个**导入即失败**的产物，"
        f"而 `hmake` 报成功、`hrun`/pytest 只留下一条与 YAML 脱节的 NameError。\n"
        f"  常见来源：YAML 里未加引号的日期/时间戳（`2020-01-01`、`2020-01-01 08:30:00`）"
        f"会被 YAML 1.1 解析成 `datetime.date` / `datetime.datetime`；"
        f"`.nan` / `.inf` 会被解析成浮点特殊值；"
        f"**超长整数**（超过 4300 位）连 `repr()` 都求不出来。\n"
        f"  改法：给该值加引号（如 `\"2020-01-01\"`），或在需要日期语义的地方用字符串"
        f"并在 debugtalk.py 里转换；超长数字（长订单号/卡号）请按字符串处理。"
    )
    if cause is not None:
        raise error from cause
    raise error


def __rendered_literal_is_usable(value: Any) -> bool:
    """`repr(value)` 的文本在生成物里（**没有任何额外 import**）能否直接求值。

    NOTICE: `ast.literal_eval` 的口径比「能不能 import」更严——`frozenset({1})`
    复不原但写进生成物完全能用（`frozenset` 是内建名字）；反过来
    `datetime.date(2020, 1, 1)` 与 `nan` 都引用了生成物里**不存在**的名字。
    本函数只回答后者：出现非内建名字（或干脆不是合法表达式）→ False。
    """
    try:
        rendered = repr(value)
    except Exception:  # noqa: BLE001 - repr 都求不出来的值当然不可用
        return False

    try:
        tree = ast.parse(rendered, mode="eval")
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in _BUILTIN_NAMES:
            return False
    return True


# 模块级**由导入机制注入**的名字：它们不是内建，但任何模块里都直接可用。
# NOTICE: 少了这一份，`ensure_generated_python_is_importable()` 会把生成物里的
# `sys.path.insert(0, str(Path(__file__).parent))`（跨项目引用用例时模板会加这段）
# 误判成「未定义的名字 `__file__`」而拦下**合法**生成物——实测踩过，故单列白名单。
_MODULE_INJECTED_NAMES = frozenset(
    {
        "__name__",
        "__file__",
        "__doc__",
        "__package__",
        "__loader__",
        "__spec__",
        "__builtins__",
        "__debug__",
    }
)


def ensure_generated_python_is_importable(content: Text, generated_path: Text) -> None:
    """落盘前的**导入性**兜底：生成物除了语法合法，还不能引用未导入/未定义的名字。

    NOTICE: 与 `ensure_generated_python_is_valid()`（只 `compile()`，管语法）配成一对，
    把该文件文档里那句「保证写出去的文件一定能被 import」真正补齐 ——
    `compile("datetime.date(2020, 1, 1)")` 是**成功**的，但导入时必然
    `NameError: name 'datetime' is not defined`（值不可字面量化时就是这么落盘的）。

    判据是**保守**的（宁可漏报也不假报）：把「模块里所有被绑定的名字」
    （import、def/class、赋值目标、形参、except as、comprehension 目标、global/nonlocal）
    与内建名字一起当作可用集合，只报**哪里都没绑过**的 Load 名字。
    于是同名遮蔽、条件导入等形态都会被判成"可用"——不会拦住现在能跑的用例。
    """
    try:
        tree = ast.parse(content, filename=generated_path)
    except SyntaxError:
        return  # 交给 ensure_generated_python_is_valid 统一报（口径只留一处）

    bound = set(_BUILTIN_NAMES) | set(_MODULE_INJECTED_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            bound.add(node.id)

    undefined = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in bound:
                undefined.append(node.id)

    if undefined:
        first = undefined[0]
        raise exceptions.ParamsError(
            f"生成物**导入时会报 NameError**，已阻止落盘：\n"
            f"  生成物路径: {generated_path}\n"
            f"  未定义的名字: {sorted(set(undefined))[:5]}（共 {len(undefined)} 处引用）\n"
            f"  常见原因：YAML 值被解析成了不能写成 Python 字面量的对象"
            f"（未加引号的日期 → `datetime.date`、`.nan`/`.inf` → 浮点特殊值），"
            f"渲染后引用了生成物里没有 import 的模块。\n"
            f"  改法：给该值加引号改成字符串（如 \"2020-01-01\"）。"
        )



def _safe_repr(value: Any, limit: int = 200) -> Text:
    """`repr()` 的**安全版**：本身不会抛，且过长的表示会被截断。

    NOTICE（批次 E / **L8**）：告警路径原来直接写 `{value!r}`，而 `repr()` 是**第二次**
    求值（`__is_python_literal` 里那次在 `try` 内、这次不在）—— 于是「触发告警的那种值」
    恰好是能让 `repr()` 抛的值时，**告警自己先崩了**：
    实测一个 5000 位的大整数在 CPython 3.11+ 下 `repr(value)` 抛
    `ValueError: Exceeds the limit (4300 digits) for integer string conversion`，
    用户看到的不是「这个值不能用字面量表示」，而是一条与根因无关的 ValueError。
    与 `utils.omit_long_data` 的取向一致：**展示用**的文本要能被安全截断。
    """
    try:
        text = repr(value)
    except Exception as ex:  # noqa: BLE001 - 展示兜底，任何异常都不该掩盖真正的告警
        return f"<{type(value).__name__} 无法 repr：{type(ex).__name__}: {ex}>"

    if len(text) > limit:
        return f"{text[:limit]}…（共 {len(text)} 字符）"
    return text


def __is_python_literal(value: Any) -> bool:
    """判断值 repr 出来后能否被 Python 解析回同一个值。"""
    try:
        ast.literal_eval(repr(value))
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return False

    return True


def make_config_chain_style(config: Dict) -> Text:
    config_chain_style = f"Config({ensure_python_literal(config['name'])})"

    if config["variables"]:
        variables = config["variables"]
        ensure_kwargs_values_literal(variables, "config 的 variables")
        config_chain_style += (
            f".variables(**{ensure_python_literal(variables, where='config 的 variables')})"
        )

    if "base_url" in config:
        config_chain_style += f".base_url({ensure_python_literal(config['base_url'])})"

    if "verify" in config:
        config_chain_style += f".verify({ensure_python_literal(config['verify'])})"

    # NOTICE: 用 `is not None` 而不是真值判断——timeout: 0（立即超时）是合法配置
    if config.get("timeout") is not None:
        config_chain_style += f".timeout({ensure_python_literal(config['timeout'])})"

    if "export" in config:
        config_chain_style += f".export(*{ensure_python_literal(config['export'])})"

    # NOTICE（0920 批次 2 / **N16**）：`skip` 必须**同时**出现在两个地方：
    #   ① 模板上的 `@pytest.mark.skip`（`make_config_skip`）—— 管**独立收集**的用例；
    #   ② 这里的 `.skip(...)` —— 让值进入运行期 `TConfig`，管**被引用**的用例。
    # 修复前只有 ①，而引用走的是直接 `test_start()` 调用，pytest 标记不生效，
    # 于是「声明 skip 的用例」被引用时会照常执行（实测：单独跑 1 skipped，
    # 被 parent 引用后 1 failed，里面的步骤真的跑了）。
    if config.get("skip"):
        skip_reason = make_config_skip(config)
        config_chain_style += f".skip({skip_reason})"

    if config.get("oauth2"):
        oauth2 = config["oauth2"]
        # NOTICE: 生成的 _test.py 会把 client_secret 原样落盘（用户常把生成文件一起提交），
        # 明文密钥会因此进入版本库。运行期 runner.get_oauth2_token() 会解析
        # ${ENV(VAR)} / $var / ${func()}，所以推荐在用例里写引用而非明文；
        # 这里仅告警并给出改法，不自动改写，以免静默改变既有用例的行为。
        client_secret = oauth2.get("client_secret") or ""
        if isinstance(client_secret, Text) and "$" not in client_secret:
            logger.warning(
                "config.oauth2.client_secret 是明文，会被原样写进生成的测试文件"
                "（可能随代码一起提交而泄露）。建议改为环境变量引用：\n"
                '    client_secret: ${ENV(OAUTH2_CLIENT_SECRET)}\n'
                "运行期会自动解析该引用，无需改动其它配置。"
            )
        # NOTICE（批次 B / M10 连带）：这里**不能**再直接取 `oauth2['client_id']`。
        # 修复前用下标取值，而 `load_testcase` 的模型给了默认空串、**不会**把默认值回写进
        # 原始 dict —— 于是 `oauth2: {token_url: ...}`（漏写 client_id）会让 `hmake`
        # 抛裸 `KeyError: 'client_id'`：退出码 1 是对的，但报错里既没有用例名、
        # 也不说"缺哪个字段、该补什么"（实测）。现在给出带字段名的可读报错，
        # 判据与运行期 `runner.get_oauth2_token` 的「配置不完整」完全一致（缺 token_url/client_id 才算缺）。
        missing = [
            field_name
            for field_name in ("token_url", "client_id")
            if not oauth2.get(field_name)
        ]
        if missing:
            raise exceptions.ParamsError(
                f"config.oauth2 缺少必填字段：{missing}\n"
                f"  oauth2 内容: {oauth2}\n"
                f"  改法：补齐 `token_url` 与 `client_id`（`client_secret` 可空，"
                f"但公开客户端以外的场景通常也要给）。\n"
                f"  NOTICE: 修复前这里抛的是裸 `KeyError: 'client_id'`——"
                f"看不出是用例哪一处配置的问题。"
            )

        config_chain_style += (
            f".oauth2().token_url({ensure_python_literal(oauth2['token_url'])})"
            f".client_id({ensure_python_literal(oauth2['client_id'])})"
            f".client_secret({ensure_python_literal(oauth2.get('client_secret') or '')})"
        )
        if oauth2.get("scope"):
            config_chain_style += f".scope({ensure_python_literal(oauth2['scope'])})"
        # NOTICE（批次 B / **M10**）：`grant_type` 修复前**模型认、生成器无分支、闸门也不查**
        # （`ensure_generatable_config` 只遍历 config 的**顶层**字段，嵌套子字段没人看），
        # 于是 YAML 里写 `grant_type: password` 被静默忽略、实际发的是 `client_credentials`
        # —— 而运行期 `runner.get_oauth2_token` **真的会读**这个字段（不是死字段）。
        # 实测：token 端点收到的表单是 `grant_type=client_credentials&...`，
        # `hmake` exit 0、零告警；若端点同时接受两种授权，用例会带着**错误的授权方式**绿着通过。
        # 现在按「能生效的字段就渲染」处理（与 `scope` 同款：非默认才 emit，
        # 因此既有用例的生成物逐字节不变），并在下面把未知子字段也拦掉。
        if oauth2.get("grant_type"):
            config_chain_style += (
                f".grant_type({ensure_python_literal(oauth2['grant_type'])})"
            )

    return config_chain_style


def make_config_skip(config: Dict) -> Text:
    """返回模板里 @pytest.mark.skip(reason=...) 的 reason 字面量；None 表示不跳过。

    NOTICE: 修复前只要 config 里出现 skip 键（哪怕 skip: false）都会生成 skip 标记，
    语义与常见预期相反；另外 skip 为字符串时会被原样拼进 reason=xx 生成非法 Python。
    """
    if not config.get("skip"):
        # 未配置 skip，或显式 skip: false / skip: null → 不跳过
        return None

    skip_reason = config["skip"]
    if not isinstance(skip_reason, Text):
        # skip: true 等非字符串真值 → 无条件跳过
        skip_reason = "skip unconditionally"

    return ensure_python_literal(skip_reason)


def make_request_chain_style(request: Dict, where: Text = "request") -> Text:
    method = request["method"].lower()
    url = request["url"]
    request_chain_style = f".{method}({ensure_python_literal(url, where=f'{where}.url')})"

    # NOTICE: 每个 `ensure_python_literal` 都把 `where` 传下去——它是「值无法渲染成
    # 字面量」时报错里的**定位**（哪个步骤的哪个字段）。`ensure_kwargs_values_literal`
    # 早就带了同样的定位，渲染这一路以前没带，于是报错只能说「用例里」。
    if "params" in request:
        params = request["params"]
        ensure_kwargs_values_literal(params, f"{where}.params")
        params_literal = ensure_python_literal(params, where=f"{where}.params")
        request_chain_style += f".with_params(**{params_literal})"

    if "headers" in request:
        headers = request["headers"]
        ensure_kwargs_values_literal(headers, f"{where}.headers")
        headers_literal = ensure_python_literal(headers, where=f"{where}.headers")
        request_chain_style += f".with_headers(**{headers_literal})"

    if "cookies" in request:
        cookies = request["cookies"]
        ensure_kwargs_values_literal(cookies, f"{where}.cookies")
        cookies_literal = ensure_python_literal(cookies, where=f"{where}.cookies")
        request_chain_style += f".with_cookies(**{cookies_literal})"

    if "data" in request:
        data = request["data"]
        request_chain_style += f".with_data({ensure_python_literal(data, where=f'{where}.data')})"

    # NOTICE（0918-8 / M25）：`req_json` 是 `TRequest` 的**正式字段名**（`json` 是它的别名）。
    # 修复前这里只认 `json`，而 `KNOWN_REQUEST_FIELDS` 里两个名字都在
    # → 写 `req_json:` 时既不报错、也不生成 `.with_json(...)`（请求静默**没有 body**），
    # 而 known-fields 告警还把 `req_json` 列进"已支持的字段"。现在两个名字都接受。
    if "json" in request or "req_json" in request:
        req_json = request["json"] if "json" in request else request["req_json"]
        request_chain_style += f".with_json({ensure_python_literal(req_json, where=f'{where}.json')})"

    if "timeout" in request:
        timeout = request["timeout"]
        request_chain_style += (
            f".set_timeout({ensure_python_literal(timeout, where=f'{where}.timeout')})"
        )

    if "verify" in request:
        verify = request["verify"]
        request_chain_style += (
            f".set_verify({ensure_python_literal(verify, where=f'{where}.verify')})"
        )

    if "allow_redirects" in request:
        allow_redirects = request["allow_redirects"]
        allow_redirects_literal = ensure_python_literal(
            allow_redirects, where=f"{where}.allow_redirects"
        )
        request_chain_style += f".set_allow_redirects({allow_redirects_literal})"

    if "proxies" in request:
        proxies = request["proxies"]
        ensure_kwargs_values_literal(proxies, f"{where}.proxies")
        proxies_literal = ensure_python_literal(proxies, where=f"{where}.proxies")
        request_chain_style += f".with_proxies(**{proxies_literal})"

    if "cert" in request:
        request_chain_style += (
            f".set_cert({ensure_python_literal(request['cert'], where=f'{where}.cert')})"
        )

    if "stream" in request:
        stream_literal = ensure_python_literal(
            request["stream"], where=f"{where}.stream"
        )
        request_chain_style += f".set_stream({stream_literal})"

    if "upload" in request:
        upload = request["upload"]
        ensure_kwargs_values_literal(upload, f"{where}.upload")
        upload_literal = ensure_python_literal(upload, where=f"{where}.upload")
        request_chain_style += f".upload(**{upload_literal})"

    if "x_path" in request:
        request_chain_style += (
            f".with_x_path({ensure_python_literal(request['x_path'], where=f'{where}.x_path')})"
        )

    return request_chain_style


def make_teststep_chain_style(teststep: Dict) -> Text:
    step_name = ensure_python_literal(teststep["name"])
    if teststep.get("request"):
        step_info = f"RunRequest({step_name})"
    elif teststep.get("testcase"):
        step_info = f"RunTestCase({step_name})"
    else:
        raise exceptions.TestCaseFormatError(f"Invalid teststep: {teststep}")

    if "variables" in teststep:
        variables = teststep["variables"]
        # NOTICE: 位置文案先算出来——f-string 的表达式里不能出现反斜杠（Python<3.12 直接语法错误），
        # 而这里需要嵌套引号，所以不要写在表达式内部。
        variables_where = f'步骤 "{teststep["name"]}" 的 variables'
        ensure_kwargs_values_literal(variables, variables_where)
        step_info += (
            f".with_variables(**{ensure_python_literal(variables, where=variables_where)})"
        )

    # M11（0918-1）：补上 retry 的 emit。
    # 修复前 `retry_times`/`retry_interval` 在 compat 的重建里就被丢掉了
    # （不在 STEP_KNOWN_FIELDS），生成器这里也没有分支 → YAML 里写重试**完全不生效**，
    # 而 docs/能力清单.md 把它标为 ✅。现在字段能到达这里，这里负责生成。
    # NOTICE: 位置必须在 `.get(url)` / `.call(...)` **之前**——`with_retry` 定义在
    # `RunRequest`/`RunTestCase` 上并返回自身，而 `.get()` 之后拿到的是
    # `RequestWithOptionalArgs`，那时已经没有 `with_retry` 了
    # （与仓库里手写的 examples/postman_echo/.../request_with_retry_test.py 顺序一致）。
    # 只在显式配置了非 0 次重试时生成，避免给所有用例加噪声。
    if teststep.get("retry_times"):
        retry_interval = teststep.get("retry_interval") or 0
        step_info += (
            f".with_retry(retry_times={ensure_python_literal(teststep['retry_times'])}, "
            f"retry_interval={ensure_python_literal(retry_interval)})"
        )

    if "setup_hooks" in teststep:
        setup_hooks = teststep["setup_hooks"]
        for hook in setup_hooks:
            if isinstance(hook, Text):
                step_info += f".setup_hook({ensure_python_literal(hook)})"
            elif isinstance(hook, Dict) and len(hook) == 1:
                assign_var_name, hook_content = list(hook.items())[0]
                step_info += (
                    f".setup_hook({ensure_python_literal(hook_content)}, "
                    f"{ensure_python_literal(assign_var_name)})"
                )
            else:
                raise exceptions.TestCaseFormatError(f"Invalid setup hook: {hook}")

    if teststep.get("request"):
        step_info += make_request_chain_style(
            teststep["request"], where=f'步骤 "{teststep["name"]}" 的 request'
        )
    elif teststep.get("testcase"):
        testcase = teststep["testcase"]
        call_ref_testcase = f".call({testcase})"
        step_info += call_ref_testcase

    if "teardown_hooks" in teststep:
        teardown_hooks = teststep["teardown_hooks"]
        for hook in teardown_hooks:
            if isinstance(hook, Text):
                step_info += f".teardown_hook({ensure_python_literal(hook)})"
            elif isinstance(hook, Dict) and len(hook) == 1:
                assign_var_name, hook_content = list(hook.items())[0]
                step_info += (
                    f".teardown_hook({ensure_python_literal(hook_content)}, "
                    f"{ensure_python_literal(assign_var_name)})"
                )
            else:
                raise exceptions.TestCaseFormatError(f"Invalid teardown hook: {hook}")

    if "extract" in teststep:
        # request step
        step_info += ".extract()"
        for extract_name, extract_path in teststep["extract"].items():
            # 保持历史输出的单引号风格（examples/**/*_test.py 已提交）
            step_info += (
                f".with_jmespath({ensure_python_literal(extract_path, False)}, "
                f"{ensure_python_literal(extract_name, False)})"
            )

    if "export" in teststep:
        # reference testcase step
        export: List[Text] = teststep["export"]
        step_info += f".export(*{ensure_python_literal(export)})"

    # NOTICE（0918-8 / M25）：`validators` 是 `TStep` 的**正式字段名**（`validate` 是它的别名）。
    # CLI 路径由 `compat._ensure_step_attachment` 归一成 `validate`，但**用 loader/API 直接构造**
    # 用例时 `validators` 会被静默丢掉；这里两个名字都接受，口径与 M25 的 `req_json` 一致。
    validators = teststep.get("validate", teststep.get("validators"))
    if validators is not None:
        step_info += ".validate()"

        for v in validators:
            validator = uniform_validator(v)
            assert_method = validator["assert"]
            check = validator["check"]
            if '"' in check:
                # e.g. body."user-agent" => 'body."user-agent"'
                # 含双引号时交给 repr，由它选单引号形式并转义，避免手写拼接出非法字面量
                check = ensure_python_literal(check, False)
            else:
                check = ensure_python_literal(check)
            expect = ensure_python_literal(
                validator["expect"],
                where=f'步骤 "{teststep["name"]}" 的 validate 期望值',
            )
            message = validator["message"]
            if message:
                step_info += (
                    f".assert_{assert_method}({check}, {expect}, "
                    f"{ensure_python_literal(message)})"
                )
            else:
                step_info += f".assert_{assert_method}({check}, {expect})"

    return f"Step({step_info})"


def make_testcase(
    testcase: Dict,
    dir_path: Text = None,
    _chain: List[Tuple[Text, Text]] = None,
) -> Text:
    """convert valid testcase dict to pytest file path

    Args:
        testcase (dict): testcase dict
        dir_path (str): 生成物输出目录（默认按用例自身位置派生）
        _chain (list): **内部参数，调用方不要传**。当前这条生成调用链上已经处理过的用例，
            元素是 `(生成物路径, 源用例路径)` 二元组（从最外层用例往下），
            用于检测 `testcase:` 引用成环（M34）。见本函数内 `reference_chain` 处的 NOTICE。
    """
    # 0918-1：**字段可生成性**交叉校验（H2 / M11 的统一闸门）。
    # 必须放在 `ensure_testcase_v4` **之前**：compat 的 `_ensure_step_attachment` 是按
    # 白名单**重建** teststep 的，跑完它之后 `validate_script` 之类的原始信息可能已经不完整；
    # 而且这里要报的是「用户 YAML 里写了什么」，越早看越准。
    # （`api` 这个 v2/v3 写法已在校验内部按合法指针处理，见 ensure_generatable_teststeps。）
    raw_config = testcase.get("config")
    if isinstance(raw_config, dict):
        # 0918-8 / L15（§六.1）：config 层同样要有闸门——`config.thrift` / `config.db`
        # 修复前是「模型认、白名单认、生成器不认、零告警」，配了等于没配。
        ensure_generatable_config(
            raw_config, raw_config.get("path", "") if raw_config else ""
        )
    ensure_generatable_teststeps(
        testcase.get("teststeps"),
        raw_config.get("path", "") if isinstance(raw_config, dict) else "",
    )

    # ensure compatibility with testcase format v2/v3
    testcase = ensure_testcase_v4(testcase)

    # validate testcase format
    testcase_obj = load_testcase(testcase)

    # 0918-8 / M18：校验用模型、渲染用原始 dict → 类型强转的结果被丢掉。
    # 把「写成字符串的标量」（`verify: "false"` / `timeout: "30"` / `retry_times: "3"`）
    # 换回模型强转后的值，避免生成出语义反向或运行期报错的代码。
    __coerce_scalar_fields_into(testcase["config"], testcase_obj.config, TConfig)
    for raw_step, step_obj in zip(
        testcase.get("teststeps") or [], testcase_obj.teststeps
    ):
        __coerce_scalar_fields_into(raw_step, step_obj, TStep)
        raw_request = raw_step.get("request")
        if isinstance(raw_request, Dict) and step_obj.request is not None:
            __coerce_scalar_fields_into(raw_request, step_obj.request, TRequest)

    testcase_abs_path = __ensure_absolute(testcase["config"]["path"])
    logger.info(f"start to make testcase: {testcase_abs_path}")

    testcase_python_abs_path, testcase_cls_name = convert_testcase_path(
        testcase_abs_path
    )
    if dir_path:
        testcase_python_abs_path = os.path.join(
            dir_path, os.path.basename(testcase_python_abs_path)
        )

    # 0918-8 / M16：**两个不同的用例映射到同一个生成物**时必须报错，不能静默只生成第一个。
    # 根因：`ensure_file_abs_path_valid` 把空格/点/连字符都归一成下划线，
    # 于是 `login-v2.yml` 与 `login_v2.yml` 都生成 `login_v2_test.py`。
    # 修复前这里只有缓存命中就 `return`——第二个用例**既不生成也不执行**，且 exit 0、零告警
    # （实测 `hrun <目录>` 只收到 1 个用例）。这里改成显式报错，并指出两个源文件。
    existing_source = pytest_files_source_mapping.get(testcase_python_abs_path)
    if existing_source is not None and os.path.normcase(
        existing_source
    ) != os.path.normcase(testcase_abs_path):
        raise exceptions.TestCaseFormatError(
            f"两个用例会生成到**同一个**测试文件：第二个不会被生成，也不会被执行。\n"
            f"  已占用该生成物的用例: {existing_source}\n"
            f"  本次用例: {testcase_abs_path}\n"
            f"  生成物路径: {testcase_python_abs_path}\n"
            f"原因：生成文件名会把空格 / 点 / 连字符归一成下划线"
            f"（`make.ensure_file_abs_path_valid`），所以这两个用例名落到了同一个文件。\n"
            f"修法：给其中一个改名（例如 `login-v2.yml` → `login_v2_b.yml`），"
            f"或把它们放到不同目录。"
        )
    pytest_files_source_mapping[testcase_python_abs_path] = testcase_abs_path

    global pytest_files_made_cache_mapping
    if testcase_python_abs_path in pytest_files_made_cache_mapping:
        return testcase_python_abs_path

    # 0919-2 / M34：**引用成环**必须在生成期拦下来，不能让它无限递归。
    #
    # 修复前的现场：`a.yml` 引用 `b.yml`、`b.yml` 又引用 `a.yml` →
    # `make_testcase` 递归调用自身（下面 teststeps 循环里那一处），而防重入用的
    # `pytest_files_made_cache_mapping` 要到**文件写盘之后**才登记（本函数末尾），
    # 所以环上的文件永远命不中缓存 → `RecursionError: maximum recursion depth exceeded`，
    # 报错与「引用成环」这个根因毫无关系（顺带还会打印上千行交错日志）。
    #
    # 修法：把「当前调用链」当成**参数**沿递归往下传，而不是用模块级全局变量记录。
    # 这样天然是**异常安全**的——链随调用栈增长，异常展开时自动消失，
    # 不需要 try/finally 去清理，也就不存在「失败一次之后残留脏状态、
    # 下次把无关失败误报成循环引用」那类问题。
    reference_chain = list(_chain or [])
    this_key = os.path.normcase(testcase_python_abs_path)
    cycle_at = next(
        (
            index
            for index, (python_path, _source_path) in enumerate(reference_chain)
            if os.path.normcase(python_path) == this_key
        ),
        None,
    )
    if cycle_at is not None:
        # 报错里展示**源用例**（`.yml`）而不是生成物（`_test.py`）——
        # 用户改的是前者，指着 `a_test.py` 说「这里成环了」等于让他去猜。
        chain_display = " -> ".join(
            os.path.basename(source_path)
            for _python_path, source_path in reference_chain[cycle_at:]
        )
        raise exceptions.TestCaseFormatError(
            f"检测到 testcase **循环引用**（A 引用 B、B 又引用 A 这一类）。\n"
            f"  引用链: {chain_display} -> {os.path.basename(testcase_abs_path)}\n"
            f"  环上的用例: {testcase_abs_path}\n"
            f"原因：`testcase:` 引用会**递归生成**被引用用例，环会让生成过程无限递归。\n"
            f"  修复前的表现是 `RecursionError: maximum recursion depth exceeded`，"
            f"与「引用成环」这个根因完全对不上。\n"
            f"修法（任选其一）：\n"
            f"  1) 打断环：让其中一个用例不再引用对方；\n"
            f"  2) 抽出公共用例：把两边都要用的步骤放进第三个用例，两边各自引用它。"
        )
    reference_chain.append((testcase_python_abs_path, testcase_abs_path))

    config = testcase["config"]
    # NOTICE: testcase_python_abs_path 此刻还没写盘，用它的项目 meta（已经由上面的
    # testcase_abs_path 正确定位）做相对化，避免按不存在的路径重新定位项目。
    testcase_project_meta = load_project_meta(
        testcase_abs_path, keep_loaded_meta_for_projectless=False
    )

    # P0：断言算子白名单校验 —— 自定义算子由 StepRequestValidation.__getattr__ 动态分发后，
    # 「拼错算子名」不会再在生成阶段暴露，必须在这里主动拦一次（见 ensure_known_comparators）。
    ensure_known_comparators(
        testcase.get("teststeps"),
        testcase_project_meta.functions,
        relative_to_root_dir(testcase_abs_path, testcase_project_meta),
    )

    # P2-b：内联 JSON Schema 会在运行期炸（`$` 键被当变量），在生成前精确拦住
    ensure_json_schema_not_inline(
        testcase.get("teststeps"),
        relative_to_root_dir(testcase_abs_path, testcase_project_meta),
    )

    # A2-2：xml_schema_match 引用的 XSD 文件必须存在（路径写错不该拖到跑用例才发现）
    ensure_xsd_files_exist(
        testcase.get("teststeps"),
        relative_to_root_dir(testcase_abs_path, testcase_project_meta),
    )

    config["path"] = relative_to_root_dir(
        testcase_python_abs_path, testcase_project_meta
    )
    config["variables"] = convert_variables(
        config.get("variables", {}), testcase_abs_path
    )
    # NOTICE（0920 批次 6 / **N34**）：`parameters` 的字符串形态同样要在**生成期**求值。
    # 修复前只有 `variables` 走了这一步，`parameters: ${func()}` 会被原样渲染成
    # `Parameters("${func()}")` → `hmake` exit 0、收集期
    # `AttributeError: 'str' object has no attribute 'items'`。
    if config.get("parameters") is not None:
        config["parameters"] = convert_parameters(
            config["parameters"], testcase_abs_path
        )

    # prepare reference testcase
    imports_list = []
    # 0920 批次 3 / **N7+N8**：跨项目引用时，被引用用例所在**项目根**要进生成物的 `sys.path`。
    # 收集起来，最后统一渲染成 `sys.path.insert(0, ...)`（见下面的 import_deps）。
    ref_project_roots: List[Text] = []
    teststeps = testcase["teststeps"]
    # 0918-8 / H9：同一用例里引用的**不同**被引用用例，import 别名必须互不相同。
    # 别名一直由文件名派生（`convert_testcase_path`），所以 `a/login.yml` 与 `b/login.yml`
    # 都叫 `Login`：生成物里第 2 条 import 会**遮蔽**第 1 条，两个 step 都调用同一个类
    # ——**前面的用例被静默替换成后面的**（实测 hmake/hrun 全绿、"call A" 一步都没跑）。
    # 处置：**只在真的撞名时**加「目录前缀」消歧，保证既有生成物**零漂移**
    # （仓库里 34 个已入库生成物 + AST 漂移不变量都依赖这一点）。
    ref_alias_by_path: Dict[Text, Text] = {}
    # NOTICE（0920 批次 5 / **N28**）：`used_aliases` 必须**预置模板自己用到的名字**。
    #
    # 生成物的模块命名空间里已经有一批框架名字（模板顶部 import 的
    # `Config` / `Step` / `RunRequest` / `RunTestCase` / `Parameters` / `pytest` /
    # `InterfaceTester` / `sys` / `Path`）。若被引用用例的文件名恰好等于其中之一
    # （`config.yml` → 类名 `Config`），生成的
    # `from sub.config_test import TestCaseConfig as Config` 就会**把框架的 `Config` 顶掉**：
    #
    # ```python
    # from interfacetester import InterfaceTester, Config, Step, RunRequest
    # from sub.config_test import TestCaseConfig as Config      # ← 遮蔽上面那个
    # config = Config("parent").base_url("...")                  # ← 实际调用被引用的类
    # ```
    #
    # 后果（实测 `.tmp_audit/n28_check.py`）：生成物**语法合法**（`ensure_generated_python_is_valid`
    # 过），`hmake` **exit 0**、日志说"generated testcase"，而 pytest 收集期直接
    # `TypeError: TestCaseConfig() takes no arguments`（exit 2）——报错指向被引用的类，
    # 看不出根因是"别名撞了框架名字"。
    #
    # 修法：把这些名字当成"已被占用"，让 `__ref_testcase_alias` 走它的消歧分支
    # （目录前缀）。**只有真的撞上时才改名** —— 既有生成物零漂移。
    used_aliases: Dict[Text, Text] = {}
    for reserved in _GENERATED_TEMPLATE_RESERVED_NAMES:
        used_aliases[reserved] = "<generated template>"
    for teststep in teststeps:
        if not teststep.get("testcase"):
            continue

        # make ref testcase pytest file
        ref_testcase_path = __ensure_absolute(teststep["testcase"])
        test_content = load_test_file(ref_testcase_path)

        if not isinstance(test_content, Dict):
            raise exceptions.TestCaseFormatError(f"Invalid teststep: {teststep}")

        # api in v2/v3 format, convert to v4 testcase
        if "request" in test_content and "name" in test_content:
            test_content = ensure_testcase_v4_api(test_content)

        test_content.setdefault("config", {})["path"] = ref_testcase_path
        # 0919-2 / M34：把当前调用链传下去，环在下面一层的入口处被拦下
        ref_testcase_python_abs_path = make_testcase(
            test_content, _chain=reference_chain
        )

        # override testcase export
        ref_testcase_export: List = test_content["config"].get("export", [])
        if ref_testcase_export:
            step_export: List = teststep.setdefault("export", [])
            step_export.extend(ref_testcase_export)
            # 去重且保持声明顺序（修复前用 set()，同名导出变量的顺序随机，
            # 生成的 .export(*[...]) 顺序不稳定，也让生成文件无法稳定复现）
            teststep["export"] = list(dict.fromkeys(step_export))

        # prepare ref testcase class name
        ref_testcase_cls_name = pytest_files_made_cache_mapping[
            ref_testcase_python_abs_path
        ]

        # prepare import ref testcase
        ref_testcase_python_relative_path = convert_relative_project_root_dir(
            ref_testcase_python_abs_path
        )
        ref_module_name, _ = os.path.splitext(ref_testcase_python_relative_path)
        ref_module_name = ref_module_name.replace(os.sep, ".")

        # NOTICE（0920 批次 3 / **N7 + N8**）：跨项目引用时，**模块名必须带上项目限定**。
        #
        # `convert_relative_project_root_dir` 算的是「相对**被引用用例自己那个项目**的
        # RootDir」——于是两个不同项目里的 `child.yml` **都**得到 `child_test`：
        #
        #     projB/child.yml → child_test        projC/child.yml → child_test
        #     from child_test import TestCaseChild as Child
        #     from child_test import TestCaseChild as ChildRef   ← 同一个模块！
        #
        # 两条后果（都实测过）：
        #   **N7**：第 2 条 import 命中 `sys.modules` 里已加载的同一个模块，两个别名绑定
        #           **同一个类** → 名为"call childB"的步骤实际跑的是 **childC 的用例**
        #           （取决于激活顺序，另一端表现为**假通过**）；
        #   **N8**：生成物只有在 `hrun`（**同进程**跑过 hmake、ref 项目根还留在 `sys.path`）
        #           里才能 import；换一个进程 `pytest <生成物>` → `ModuleNotFoundError`
        #           —— 而"hmake 然后 pytest"正是 CI 的两步法。
        #
        # 修法：被引用用例**不在本用例所属项目**时，模块名前缀上它的项目目录名
        # （`projB/child.yml` → `projB.child_test`），**并把那个项目根加进生成物的 sys.path**。
        # 只有跨项目时才改写，所以同项目引用（仓库里 34 个已入库生成物全部如此）**零漂移**。
        ref_project_meta = load_project_meta(ref_testcase_python_abs_path)
        same_project = os.path.normcase(
            os.path.abspath(ref_project_meta.RootDir)
        ) == os.path.normcase(os.path.abspath(testcase_project_meta.RootDir))
        if not same_project:
            project_prefix = _project_module_prefix(
                testcase_project_meta.RootDir, ref_project_meta.RootDir
            )
            ref_module_name = f"{project_prefix}.{ref_module_name}"
            # NOTICE（本轮修复：**要插入的是项目根的父目录**，不是项目根自己）。
            #
            # 上面刚把模块名改成了 `projB3.child_test` —— 这是个**包限定名**，
            # 于是 import 它是「先找到包 `projB3`，再找子模块 `child_test`」，
            # 而包的搜索路径是 `sys.path` 上的**父目录**。
            # 修复前这里插入的是 `ref_project_meta.RootDir`（= `.../projB3` 自己），
            # 于是 `sys.path` 上存在的是「包的内部」，Python 永远找不到名为 `projB3` 的包：
            #
            #     生成物里：sys.path.insert(0, ".../projB3")      ← 插错了层级
            #               from projB3.child_test import ...     ← 需要找的是父目录
            #     独立进程 → ModuleNotFoundError: No module named 'projB3'
            #
            # 这正好是 N8 声称要修掉的那个现象（"换一个进程 pytest <生成物> 就 import 失败"）
            # —— 修复只做了一半：模块名加了前缀，`sys.path` 却没跟着上移一级。
            # 之所以测试里一直没暴露：测试自己 `sys.path.insert(0, self.tmp_dir)`
            # （临时目录恰好是**所有项目的共同父目录**）把这层缺失补上了，
            # 于是"同一进程 + 测试垫的路径"下能过，而真实 CI 两步法（hmake 然后另起
            # pytest 进程）会失败。
            #
            # 同项目引用**不受影响**（走不到这个分支，语料里 34 个已入库生成物零漂移）。
            bootstrap_dir = os.path.dirname(os.path.abspath(ref_project_meta.RootDir))
            if bootstrap_dir not in ref_project_roots:
                ref_project_roots.append(bootstrap_dir)
            logger.info(
                f"被引用用例 `{ref_testcase_path}` 位于**另一个项目**"
                f"（{ref_project_meta.RootDir}）→ 生成物里用 `{ref_module_name}` 引用，"
                f"并在文件头把它所在项目的**父目录** `{bootstrap_dir}` 加进 `sys.path`"
                f"（`{project_prefix}` 是包名，包的搜索路径是父目录；否则同名文件会在"
                f"`sys.modules` 里互相覆盖，且换进程 import 会失败）。"
            )

        ref_alias = __ref_testcase_alias(
            ref_alias_by_path,
            used_aliases,
            ref_testcase_python_abs_path,
            ref_testcase_cls_name,
            ref_testcase_python_relative_path,
        )
        teststep["testcase"] = ref_alias

        import_expr = (
            f"from {ref_module_name} import TestCase{ref_testcase_cls_name}"
            f" as {ref_alias}"
        )
        if import_expr not in imports_list:
            imports_list.append(import_expr)

    testcase_path = convert_relative_project_root_dir(testcase_abs_path)
    # current file compared to ProjectRootDir
    diff_levels = len(testcase_path.split(os.sep))
    if len(imports_list) > 0 and diff_levels > 0:
        parent = ".parent" * diff_levels
        extra_paths = "".join(
            f"\nsys.path.insert(0, {ensure_python_literal(root)})"
            for root in ref_project_roots
        )
        import_deps = f"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__){parent})){extra_paths}
"""
        imports_list.insert(0, import_deps)

    parameters = config.get("parameters")
    if parameters:
        # 参数化内容也要走字面量渲染：dict/list 与 str() 输出一致，
        # 但字符串引用（如 ${parameterize(x.csv)}）会变成合法字面量而不是裸表达式。
        if isinstance(parameters, dict):
            ensure_kwargs_values_literal(parameters, "config 的 parameters")
        elif isinstance(parameters, (list, tuple)):
            # 列表形式：每个元素应当是「参数名: 值」的字典
            for index, parameter in enumerate(parameters):
                if isinstance(parameter, dict):
                    ensure_kwargs_values_literal(
                        parameter, f"config 的 parameters[{index}]"
                    )
        parameters = ensure_python_literal(parameters, where="config 的 parameters")

    data = {
        "version": __version__,
        "testcase_path": testcase_path,
        "class_name": f"TestCase{testcase_cls_name}",
        "imports_list": imports_list,
        "config_chain_style": make_config_chain_style(config),
        "skip": make_config_skip(config),
        "parameters": parameters,
        "reference_testcase": any(step.get("testcase") for step in teststeps),
        "teststeps_chain_style": [
            make_teststep_chain_style(step) for step in teststeps
        ],
    }
    content = __TEMPLATE__.render(data)

    # NOTICE（批次 B / **M1**）：**落盘之前**先编译一次 —— 「生成物必须是合法 Python」。
    #
    # 为什么要有这道兜底：修复前 `hmake` 对生成物**没有任何语法校验**，而 black 解析失败
    # 只被降级成一条 WARNING（`format_pytest_with_black` 的既有设计：格式化失败不阻断）。
    # 于是「文件名/目录名里有 `+`」这类输入会产出
    # `class TestCaseCase+1(InterfaceTester):` 这种**语法错误**的文件，而 **`hmake` 退出码是 0**
    # （实测）—— 用户拿着一个"生成成功"的产物，直到 `hrun`（exit 2、收集期 SyntaxError）
    # 才发现，甚至可能只跑 `hmake` 的 CI 步骤全程绿灯。
    #
    # 与 `hconvert` 的口径对齐：`converters/emit_yaml.validate_emitted_case` 的最后一步
    # 就是 `compile(source, generated_path, "exec")`（注释自称"语法级兜底"）——
    # 生成器这一侧一直缺同样的一道。
    #
    # NOTICE: 校验放在**写盘之前**，所以失败时不会留下半成品（也顺带省掉 black 那一趟）。
    # 失败按 `ParamsError` 抛（MyBaseError 家族）→ 由 `_record_make_failure` 记进
    # 「N 个用例生成失败」汇总、退出码 1，与其它生成期闸门的语义完全一致。
    ensure_generated_python_is_valid(content, testcase_python_abs_path)
    # NOTICE（残留缺陷：`compile()` 只查语法，查不出「名字没 import」）：
    # 值不可字面量化时（未加引号的日期 → `datetime.date(2020, 1, 1)`、`.nan`/`.inf` → 裸名字）
    # 生成物语法完全合法、`compile()` 通过，却在 import 的第一行就 NameError ——
    # 修复前 `hmake` 退出码 0 并且**已经落盘**，与上面那句「保证写出去的文件一定能被 import」矛盾。
    # 这里补上同口径的**导入性**兜底（值层面的点名报错在 `__ensure_value_literal`，
    # 这一道是最后一道无损检查，管住键、以及未来任何新的渲染路径）。
    ensure_generated_python_is_importable(content, testcase_python_abs_path)

    # ensure new file's directory exists
    dir_path = os.path.dirname(testcase_python_abs_path)
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    ensure_generated_file_may_be_overwritten(testcase_python_abs_path, testcase_path)

    with open(testcase_python_abs_path, "w", encoding="utf-8") as f:
        f.write(content)

    pytest_files_made_cache_mapping[testcase_python_abs_path] = testcase_cls_name
    __ensure_testcase_module(testcase_python_abs_path)

    logger.info(f"generated testcase: {testcase_python_abs_path}")

    return testcase_python_abs_path


def ensure_request_body_is_unambiguous(teststep: Dict, location: Text) -> None:
    """批次 B / **M6**：请求体不能既是 `data` 又是 `json`。

    NOTICE（为什么这是"假通过/静默错请求"而不是"用户随便写写"）：
    两个都是 `TRequest` 的**正式字段**（`KNOWN_REQUEST_FIELDS` 都认），所以
    未知字段告警不响；生成器也照渲染 `.with_data(...)` 与 `.with_json(...)`；
    真正决定行为的是 `requests.PreparedRequest.prepare_body`：

        if not data and json is not None:      # ← data 为真时整个 json 分支**不执行**
            content_type = "application/json"
            body = complexjson.dumps(json, ...)

    实测（`.tmp_audit/kernel/probe_data_and_json.py`，服务端视角）：
    YAML 写 `data: "a=1&b=2"` + `json: {from_json: true}` → 服务端只收到 `a=1&b=2`，
    `json` 体从未出现，而 `hmake` 与运行期**零告警**。
    请求体与 YAML 里写的**不一致**却没有任何信号 —— 正是本仓 §7 要消灭的那类形态。

    NOTICE（判据为什么是"data 为真"）：完全对齐 requests 的实际语义，避免假报：
      - `data` 真值 + `json` 非 None → **冲突**（json 被丢，报错）；
      - `data` 空（`""` / `{}` / 未写）+ `json` → 正常（走的正是 json 分支）；
      - `data` 真值 + `json: null`（显式 null）→ 正常（`json is None`，requests 不写 body）。
    """
    request = teststep.get("request")
    if not isinstance(request, dict):
        return

    # `json` 的别名是 `json`、字段名是 `req_json`（`populate_by_name=True` 两个都收）
    has_json = request.get("json") is not None or request.get("req_json") is not None
    if not request.get("data") or not has_json:
        return

    raise exceptions.ParamsError(
        f"同一个 request 里同时写了 `data` 与 `json` —— requests 的语义是"
        f"「`data` 非空时**整个 json 分支不执行**」，也就是 `json` 会被**静默丢弃**。\n"
        f"  实测：YAML 写 `data: \"a=1&b=2\"` + `json: {{from_json: true}}`，"
        f"服务端只收到 `a=1&b=2`，而 `hmake` 与运行期都没有任何告警 —— "
        f"请求体与用例里写的不一致却看不出来。\n"
        f"  改法（二选一）：\n"
        f"  1) 要发 JSON 体 → 删掉 `data:`；\n"
        f"  2) 要发表单/文本体 → 删掉 `json:`。\n"
        f"  hint: URL 查询串请用 `params:`（不是 `data:`）—— `data:` 是**请求体**。\n"
        f"{location}\n"
        f"request 内容: {request}"
    )


def ensure_upload_body_is_unambiguous(teststep: Dict, location: Text) -> None:
    """0920 / **缺陷 1b**：`upload` 与 `data` / `json` **并存**必须报错。

    NOTICE（为什么这是"静默丢体"而不是"用户随便写写"）：
    `ext/uploader.prepare_upload_step()` 会无条件把 `step.request.data` 换成
    `"$m_encoder"`（multipart 编码器），于是：

    | 用户写的组合 | 实际发出去的 | 后果 |
    | --- | --- | --- |
    | `upload` + `data`（dict）| 只有 `upload` 里的字段 | `data` 整段**静默消失** |
    | `upload` + `data`（str） | 同上（字符串体无从表达） | 同上 |
    | `upload` + `json` | multipart 体，`json` 从不出现 | requests 的 `prepare_body` 里 `data` 为真 → json 分支不执行 |

    三种组合在 `hmake` 与运行期都是**零告警**，服务端收到的体与 YAML 里写的**不一致**
    ——正是本仓要消灭的那类形态（与 M6 的 `data`+`json` 同族，只是另一个出口）。

    NOTICE（判据与放行边界）：
    - `upload` 为空 → 放行（`upload: {}` 等于没写，`uploader` 自己会 `return`）；
    - `data` 为**空值**（`""` / `{}` / 未写）→ 放行（requests 的判据同样是"真值"）；
    - `data` 是 dict 且 `upload` 非空 → **也报错**。运行期确实会丢，但**导入器侧已经
      在 emit 层归一过**（`emit_yaml.build_request_dict` 把 dict `data` 并进 `upload`），
      所以这条报错只会在用户**手写** YAML 时出现；手写时"并进去"与"删掉"哪个是用户
      本意无法判断，故按本仓口径响亮报错并给出两种改法。
    """
    request = teststep.get("request")
    if not isinstance(request, dict):
        return
    if not request.get("upload"):
        return

    # `json` 的别名是 `json`、字段名是 `req_json`（`populate_by_name=True` 两个都收）
    has_json = request.get("json") is not None or request.get("req_json") is not None
    has_data = bool(request.get("data"))

    if not has_json and not has_data:
        return

    conflicts = []
    if has_data:
        conflicts.append(f"`data`: {request.get('data')!r}")
    if has_json:
        conflicts.append(
            f"`json`: {request.get('json') if request.get('json') is not None else request.get('req_json')!r}"
        )

    raise exceptions.ParamsError(
        f"同一个 request 里同时写了 `upload` 与 {'、'.join(conflicts)} —— "
        f"upload 机制会把请求体整个换成 multipart 编码器（`data` 被覆盖、`json` 被 requests 丢弃），"
        f"用户写的请求体与**实际发出的**不一致，且 `hmake` 与运行期都没有任何告警。\n"
        f"  实测：`upload: {{file: a.txt}}` + `data: {{note: hello}}` → 服务端只收到 `file`；\n"
        f"        `upload: {{file: a.txt}}` + `json: {{a: 1}}` → 服务端收到 multipart，`json` 从不出现。\n"
        f"  改法（二选一）：\n"
        f"  1) 要发 multipart → 普通字段直接写进 `upload:`（uploader 会把非文件标量当普通字段发），"
        f"删掉 `data:` / `json:`；\n"
        f"  2) 要发 JSON / raw 体 → 删掉 `upload:`（框架的 upload 机制与其它请求体互斥）。\n"
        f"{location}\n"
        f"request 内容: {request}"
    )


def ensure_generated_python_is_valid(content: Text, generated_path: Text) -> None:
    """生成物必须是**合法 Python**（批次 B / M1 的语法级兜底，落盘前调用）。

    NOTICE: 这是"最后一道无损检查"——它不关心语义（那是模型与生成期闸门的事），
    只保证「写出去的 `*_test.py` 一定能被 import」。修好 `normalize_module_segment`
    之后正常路径不会再触发它；留着它是为了**下一次**生成器改动出问题时，
    失败发生在生成期而不是 pytest 收集期（后者报的是
    `collected 0 items / 1 error` + 生成物里的 SyntaxError，与用户的 YAML 完全脱节）。
    """
    try:
        compile(content, generated_path, "exec")
    except SyntaxError as ex:
        raise exceptions.ParamsError(
            f"生成物**不是合法 Python**，已阻止落盘（避免留下不能 import 的文件）：\n"
            f"  生成物路径: {generated_path}\n"
            f"  语法错误: {type(ex).__name__}: {ex}\n"
            f"  出错位置: 第 {ex.lineno} 行\n"
            f"  常见原因：**源用例的文件名/目录名里有不能出现在 Python 标识符里的字符**"
            f"（`+ @ & ' ( ) ! % , ; = [ ] ^ ` {{ }} ~ $ #` 等），"
            f"它们会进入生成物的**类名**或 `import` 目标。\n"
            f"  改法：把源文件/目录名里的这些字符换成 `_`、`-`、空格或中文，再重跑。\n"
            f"  NOTICE: 修复前这类问题是**静默**的——`hmake` 退出码 0、产物却在 `hrun` 里"
            f"报 `collected 0 items / 1 error`（SyntaxError）；"
            f"`hconvert` 侧则表现为整体 exit 1（同一个根因）。"
        ) from ex


def __make(tests_path: Text):
    """make testcase(s) with testcase/folder absolute path
        generated pytest file path will be cached in pytest_files_made_cache_mapping

    Args:
        tests_path: should be in absolute path

    """
    logger.info(f"make path: {tests_path}")
    test_files = []
    # 规则①（0919-19 / H4）：命令行**显式点名**的文件（`hmake case.yml`）与目录扫描出来的
    # 文件口径不同——前者任何失败都记失败，后者按内容特征判意图（见 `_fail_or_skip`）。
    explicitly_named = os.path.isfile(tests_path)
    if os.path.isdir(tests_path):
        files_list = load_folder_files(tests_path)
        test_files.extend(files_list)
    elif os.path.isfile(tests_path):
        test_files.append(tests_path)
    else:
        raise exceptions.TestcaseNotFound(f"Invalid tests path: {tests_path}")

    for test_file in test_files:
        if test_file.lower().endswith("_test.py"):
            pytest_files_run_set.add(test_file)
            continue

        try:
            test_content = load_test_file(test_file)
        except (exceptions.FileNotFound, exceptions.FileFormatError) as ex:
            # NOTICE（0919-19 / H4）：修复前这里**只有 warning + continue**，于是
            # `hmake broken.yml`（YAML 缩进错）/ `hmake empty.yml`（0 字节）都返回 0，
            # 而"未知算子"那类（走 make_testcase）却是 1。现在按下述规则分流：
            # 显式点名 → 记失败；目录扫描 → 解析失败时**无法判意图**（可能是模板/
            # 契约/非用例 YAML），只告警并计入"跳过"汇总（规则③）。
            _fail_or_skip(
                test_file,
                f"{type(ex).__name__}: {ex}",
                explicitly_named=explicitly_named,
                # NOTICE（残留缺陷修复）：解析失败**不等于**没有用例意图——按源文本判一次
                # （判据与 `_looks_like_testcase_intent` 同一），否则一个写坏的用例 YAML
                # 只会留下一条「已跳过」告警 + 退出码 0，CI 全绿而用例凭空消失。
                looks_like_case=_looks_like_testcase_text(test_file),
            )
            continue

        if not isinstance(test_content, Dict):
            _fail_or_skip(
                test_file,
                "test content not in dict format.",
                explicitly_named=explicitly_named,
                looks_like_case=_looks_like_testcase_text(test_file),
            )
            continue

        # api in v2/v3 format, convert to v4 testcase
        if "request" in test_content and "name" in test_content:
            test_content = ensure_testcase_v4_api(test_content)

        if "config" not in test_content:
            _fail_or_skip(
                test_file,
                "missing config part.",
                explicitly_named=explicitly_named,
                looks_like_case=_looks_like_testcase_intent(test_content),
                label="testcase file",
            )
            continue
        elif not isinstance(test_content["config"], Dict):
            _fail_or_skip(
                test_file,
                f"config should be dict type, got {test_content['config']}",
                explicitly_named=explicitly_named,
                looks_like_case=_looks_like_testcase_intent(test_content),
                label="testcase file",
            )
            continue

        # ensure path absolute
        test_content.setdefault("config", {})["path"] = test_file

        # invalid format
        if "teststeps" not in test_content:
            logger.warning(f"Invalid testcase file: {test_file}")

        # testcase
        try:
            testcase_pytest_path = make_testcase(test_content)
            pytest_files_run_set.add(testcase_pytest_path)
        except exceptions.MyBaseError as ex:
            # NOTICE（0918-4 / L8）：修复前这里只捕 `TestCaseFormatError`，于是
            # 「非格式类」异常（拼错的算子名 → FunctionNotFound、不支持的字段 → ParamsError、
            # 引用路径不存在 → FileNotFound）会穿透出去，在 `main_make` 里变成
            # 「首个失败即终止整批」——排在其后的文件**从未被处理**，
            # 用户只看到一句与那些文件无关的错误 + exit 1。
            _record_make_failure(test_file, f"{type(ex).__name__}: {ex}")
            continue
        except Exception as ex:  # noqa: BLE001
            # 非 MyBaseError 的意外异常（框架 bug）同样不能拖垮整批：
            # 记完整 traceback，其余文件继续，退出码由 main_make 统一给。
            _record_make_failure(
                test_file, f"{type(ex).__name__}: {ex}", unexpected=True
            )
            continue


def main_make(tests_paths: List[Text]) -> List[Text]:
    if not tests_paths:
        return []

    ga4_client.send_event("make")

    make_failures.clear()
    # 0919-19 / H4：同样每次调用清空（规则③的"跳过"计数）
    make_skipped_files.clear()
    # 0918-8 / M16：每次调用重新记账「生成物 → 源用例」，避免上一次调用的记录造成误报
    # （生成物缓存 pytest_files_made_cache_mapping 本身是进程级、跨调用累积的既定行为）。
    pytest_files_source_mapping.clear()
    generated_before = len(pytest_files_run_set)

    for tests_path in tests_paths:
        tests_path = ensure_path_sep(tests_path)
        if not os.path.isabs(tests_path):
            tests_path = os.path.join(os.getcwd(), tests_path)
        # 0918-8 / M17：入口就归一化。`.\x` / `a//b` / `..` 这些冗余成分会让下游
        # `relative_to_root_dir` 的路径派生错位（生成物落到错目录，或直接 IndexError）。
        tests_path = os.path.normpath(tests_path)

        try:
            __make(tests_path)
        except exceptions.MyBaseError as ex:
            # 整条路径都不可用（路径不存在、项目 debugtalk.py 导入失败等）
            logger.error(f"failed to make path: {tests_path}\n{type(ex).__name__}: {ex}")
            make_failures.append(MakeFailure(tests_path, f"{type(ex).__name__}: {ex}"))

    # format pytest files
    pytest_files_format_list = pytest_files_made_cache_mapping.keys()
    format_pytest_with_black(*pytest_files_format_list)

    # 规则③（0919-19 / H4）：把"被跳过"的文件数写进末尾汇总。
    # NOTICE: 必须在 `make_failures` 的 `sys.exit(1)` **之前**打印，否则有失败时这行就丢了。
    if make_skipped_files:
        yml_like = [
            path
            for path in make_skipped_files
            if path.lower().endswith((".yml", ".yaml"))
        ]
        detail = "\n".join(f"  - {path}" for path in make_skipped_files)
        logger.warning(
            f"已跳过 {len(make_skipped_files)} 个不像用例的文件"
            f"（其中 {len(yml_like)} 个是 .yml/.yaml）：它们**既没有生成、也不会执行**。\n"
            f"{detail}\n"
            f"  说明：目录批量里出现 `schemas/*.json`、集合 JSON、契约 yaml 是正常的；"
            f"但如果你期望其中某个文件是用例，请检查它的 `config`/`teststeps` 是否写对。"
        )

    # 规则③的延伸（0919-19 / H4）：整条路径**零产出**时也要有一行。
    # 修复前 `hmake <空目录>` 是"只打印 path、然后静默 exit 0"，看不出什么都没发生。
    if (
        len(pytest_files_run_set) == generated_before
        and not make_failures
        and not make_skipped_files
    ):
        logger.warning(
            f"没有从这些路径生成任何用例（既没有成功、也没有失败、也没有跳过）："
            f"{tests_paths}\n  目录里是不是没有 `.yml/.yaml/.json` 用例文件？"
        )

    if make_failures:
        # NOTICE（0918-4 / L8）：**先处理完所有文件，再统一失败**。
        # 退出码语义没变（仍然是 1），但「一个坏文件 = 整批静默丢掉」变成了
        # 「坏文件逐条列出 + 其余文件照常生成」。
        detail = "\n".join(f"  - {item.path}\n      {item.reason}" for item in make_failures)
        logger.error(
            f"{len(make_failures)} 个用例生成失败（已处理完全部输入，"
            f"其余 {len(pytest_files_run_set)} 个用例生成成功）：\n{detail}"
        )
        sys.exit(1)

    return list(pytest_files_run_set)


def init_make_parser(subparsers):
    """make testcases: parse command line options and run commands."""
    parser = subparsers.add_parser(
        "make",
        help="Convert YAML/JSON testcases to pytest cases.",
    )
    parser.add_argument(
        "testcase_path", nargs="*", help="Specify YAML/JSON testcase file/folder path"
    )

    return parser
