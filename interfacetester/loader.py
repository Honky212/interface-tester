import contextlib
import contextvars
import csv
import importlib
import io
import json
import os
import re
import sys
import types
from typing import Any, Callable, Dict, List, Optional, Text, Tuple, Union

import yaml
from loguru import logger
from pydantic import ValidationError

from interfacetester import builtin, exceptions, utils
from interfacetester.models import (
    KNOWN_CONFIG_FIELDS,
    KNOWN_REQUEST_FIELDS,
    KNOWN_STEP_FIELDS,
    ProjectMeta,
    TestCase,
)

project_meta: Union[ProjectMeta, None] = None

""" 已加载过的项目 meta 缓存，key 为项目 RootDir 的规范化路径。

    同一进程内可能先后处理不同项目（一次 pytest 收集多个项目、CLI 反复调用、
    生成代码 import 期解析 Parameters 等）。修复前只有一个全局 project_meta，
    只要加载过一次，之后传入任何 test_path 都会返回第一个项目的 meta，
    RootDir / .env / debugtalk.py 函数全是错的（例如 parse_parameters 的项目定位失效）。
"""
project_meta_cache: Dict[Text, ProjectMeta] = {}

# NOTICE（0920 批次 2 / **N17**）：记录**当前项目**的 `.env` 写进 `os.environ` 的键值，
# 供切换项目时撤销（`set_os_environ` 只写不撤，`utils.unset_os_environ` 此前是死代码）。
# 只记"框架自己写进去的"，不碰用户外部设置的环境变量。
_project_env_keys: Dict[Text, Any] = {}

# ── 运行期「当前用例所属项目根」 ────────────────────────────────────────────
#
# NOTICE（0919-14 / 登记项①，Schema/XSD 相对路径）：`project_meta` 是**全局**的
# 「当前已加载」项目，跨项目引用时它可能已经被切到**别的项目**上。上传文件（0919-12 / F）
# 靠「把根绑进编码器函数对象」解决了；但 schema / XSD 的解析发生在**断言期**
# （`comparators._resolve_schema_path`），那里拿不到 runner，所以需要一个
# **按执行作用域**传递的「当前用例所属项目根」。
#
# 为什么用 `ContextVar` 而不是再写一个模块级全局：它**天然可嵌套/可恢复**——
# 被引用用例（另一个项目）跑完后，父用例的根会自动回来；模块级全局就得自己
# 存取栈，而且一旦某条路径忘了恢复就会串味（本仓已经吃过几次「全局状态串味」的亏）。
_run_root_dir: "contextvars.ContextVar[Optional[Text]]" = contextvars.ContextVar(
    "interfacetester_run_root_dir", default=None
)


@contextlib.contextmanager
def use_run_root_dir(root_dir: Optional[Text]):
    """在**本次用例的步骤执行**范围内声明「本用例所属项目根」。

    范围之外（CLI 加载、单元测试直接调 comparator 等）读到的仍是全局 `project_meta`，
    因此对不跑用例的调用方**零影响**（有反向护栏用例钉住）。
    """
    token = _run_root_dir.set(root_dir or None)
    try:
        yield
    finally:
        _run_root_dir.reset(token)


def current_run_root_dir() -> Text:
    """当前正在执行的用例所属项目根；不在用例执行范围内时返回空串。"""
    return _run_root_dir.get() or ""


class _DuplicateKeyRejectingSafeLoader(yaml.SafeLoader):
    """`SafeLoader` + **拒绝重复键**（批次 A / H1）。

    NOTICE（为什么必须在**加载层**拦，而不是在模型/生成器层拦）：
    PyYAML 对同一个映射里重复出现的键是「**后者胜、前者静默丢弃**」
    （`BaseConstructor.construct_mapping` 就是 `mapping[key] = value` 无条件覆盖）。
    而「键」本身是**已知字段**（`validate` / `setup_hooks` / `headers` / `request` …），
    所以下面这些既有防线**全都不会响**：

      - `loader.warn_unknown_testcase_fields`（只看**键名**认不认识）；
      - `make.ensure_generatable_teststeps`（拿到的是已经合并完的 dict，前面的段落不存在了）；
      - pydantic 模型校验（同上）。

    实际后果（实测，`.tmp_audit/verify_params.py`）：一个 step 里写两段 `validate:`，
    第一段里那条**故意失败**的断言整体蒸发 → `hmake exit 0`、**零告警**、`hrun` **1 passed**。
    这与 `docs/架构与调用链.md` §7 第 8 条（"模型认、生成器无分支的字段静默消失 → 断言消失 →
    用例假通过"）是**同一个失效形态**，只是发生在更靠前的 YAML 解析层。

    实现要点（两处刻意选择）：
      1. **在 `flatten_mapping` 之前**判重：`<<: *anchor`（YAML 合并键）会把锚点里的键
         插到显式键**前面**，此时「同一个键出现两次」是**合法**的（显式键按语义覆盖合并键）。
         判重放在 flatten 之前，就不会把这种合法写法误判成重复键；
      2. 只对**标量键**判重：非标量键（list/dict）在用例里不会出现，且本文档格式下
         无法可靠比较；直接跳过，避免制造假报。
    """

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            self._ensure_no_duplicate_keys(node)
        # 交给上游 SafeConstructor：它做 flatten_mapping（合并键）+ BaseConstructor 的构造
        return yaml.constructor.SafeConstructor.construct_mapping(self, node, deep=deep)

    def construct_yaml_int(self, node):
        """超长整数标量 → 带**文件与行号**的格式错误，而不是裸 ValueError traceback。

        NOTICE（残留缺陷：一个数字拖垮整批 `hmake`）：
        CPython 3.11+ 给 int↔str 转换设了 4300 位上限（`sys.set_int_max_str_digits`），
        而 PyYAML 的 `construct_yaml_int` 最后一行就是裸 `int(value)`。于是 YAML 里
        一个**未加引号的超长数字**（长订单号/卡号/编号常被这样写）会抛出
        `ValueError: Exceeds the limit (4300) for integer string conversion`：
          - 它是 `ValueError`，**不是** `yaml.YAMLError` → 本文件 `except yaml.YAMLError`
            接不住，于是既没有文件名也没有行号；
          - 它更不是 `MyBaseError` → `make.__make` 的逐文件捕获接不住 →
            实测 `hmake .`（一个坏文件 + 一个好文件）**退出码 1、好文件也没生成**，
            整批被一个标量拖死（与 §7 第 44/48 条「一个坏字段让整批失败」同一形态）。

        转成 `yaml.constructor.ConstructorError`（**带 node.start_mark**）后，
        既有的 `except yaml.YAMLError` 收口会把它包成 `FileFormatError`（MyBaseError），
        于是：报错里有文件与行号，且其余文件照常生成、按「N 个用例生成失败」汇总。

        NOTICE: **光在子类里覆写方法是不够的** —— PyYAML 的构造函数是按
        `yaml_constructors[tag] = SafeConstructor.construct_yaml_int` 登记的「函数对象」，
        注册的是**父类那个函数本身**，子类覆写查不到（`construct_mapping` 之所以能生效，
        是因为 `construct_yaml_map` 走的是 `self.construct_mapping(...)` 的正常方法解析）。
        所以类定义之后必须再 `add_constructor` 一次（它会 copy 注册表，不影响 SafeLoader）。
        """
        try:
            return yaml.constructor.SafeConstructor.construct_yaml_int(self, node)
        except ValueError as ex:
            preview = node.value[:24]
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"构造整数失败：{ex}\n"
                f"  该标量的原文（可能被当成数字解析）: {preview}…\n"
                f"  改法：给它加引号（如 \"{preview}…\"）让它按**字符串**解析——"
                f"长订单号/卡号/编号这类「看着像数字的文本」本来就该加引号。",
                node.start_mark,
            ) from ex

    @staticmethod
    def _ensure_no_duplicate_keys(node) -> None:
        seen: Dict = {}
        duplicates = []

        for key_node, _value_node in node.value:
            # `<<` 合并键本身不是数据键（它由 flatten_mapping 展开），不参与判重
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue

            if not isinstance(key_node, yaml.ScalarNode):
                continue

            key = key_node.value
            line = key_node.start_mark.line + 1
            if key in seen:
                duplicates.append((key, seen[key], line))
            else:
                seen[key] = line

        if not duplicates:
            return

        file_name = node.start_mark.name or "<unknown>"
        details = "\n".join(
            f"    - 键 {key!r}：第 {first_line} 行 与 第 {second_line} 行"
            for key, first_line, second_line in duplicates
        )
        raise exceptions.FileFormatError(
            f"YAML 里出现了**重复的键**（PyYAML 默认「后者胜」，前一个会被**静默丢弃**）：\n"
            f"  file: {file_name}\n"
            f"{details}\n"
            f"  为什么必须报错：被丢弃的那一段可能正是**断言**或**钩子**——用例会因为"
            f"「少校验了一部分」而**假通过**。\n"
            f"  实测形态：一个 step 里写两段 `validate:`，第一段里那条故意失败的断言整体消失，"
            f"`hmake` 退出码 0、零告警、`hrun` 报 1 passed。\n"
            f"  改法：把两段**合并成一个列表**（YAML 里同一个键只能出现一次）；"
            f"确实要分两组断言时，请拆成两个 step。\n"
            f"  NOTICE: 修复前这里是静默的——重复键既不会被告警（键名是认识的），"
            f"也不会被任何生成期闸门拦到（拿到的是合并后的 dict）。"
        )


# NOTICE: 见上面 `construct_yaml_int` 的说明——必须显式重新登记，子类覆写才会生效。
# `add_constructor` 会先 copy 一份注册表挂在本子类上，因此**不影响** yaml.SafeLoader 本身。
_DuplicateKeyRejectingSafeLoader.add_constructor(
    "tag:yaml.org,2002:int",
    _DuplicateKeyRejectingSafeLoader.construct_yaml_int,
)


def _load_yaml_file(yaml_file: Text) -> Dict:
    """load yaml file and check file content format"""
    with open(yaml_file, mode="rb") as stream:
        try:
            # NOTICE（0919-1 / L32）：用例 YAML 一律按**数据**解析，用 SafeLoader。
            #
            # 修复前是 `yaml.FullLoader`。PyYAML 5.1+ 已经堵掉了 `FullLoader` 的 RCE 面
            # （实测 6.0.3：`!!python/object/apply` / `!!python/object/new` /
            # `!!python/module` / `!!python/object` 在 FullLoader 下同样抛 ConstructorError），
            # 所以这**不是**一个可利用的漏洞。但 FullLoader 仍比 SafeLoader 多认一批标签——
            # 实测 `!!python/tuple` 与 `!!python/name:os.system` 在 SafeLoader 下被拒、
            # 在 FullLoader 下被放行（后者会构造出一个真实的可调用对象引用）。
            # 用例文件可能来自 `hconvert` 导入的外部素材（HAR 是别人录的、Postman 集合是别人给的），
            # 按最小权限原则，只放行「纯数据」标签。
            #
            # 兼容性：实测仓库内 127 个 YAML/JSON 用例与夹具在 SafeLoader 下全部正常加载；
            # 当时基线（884 passed / 8 skipped）在切换后无任何变化，
            # 即现有用例集不依赖任何 FullLoader 专有标签。
            #
            # 批次 A / H1：Loader 换成 `_DuplicateKeyRejectingSafeLoader`（SafeLoader 的子类，
            # **只多一条「重复键即报错」**）——重复键会让一整段断言/钩子静默蒸发。
            yaml_content = yaml.load(stream, Loader=_DuplicateKeyRejectingSafeLoader)
        except yaml.YAMLError as ex:
            err_msg = f"YAMLError:\nfile: {yaml_file}\nerror: {ex}"
            logger.error(err_msg)
            # 0918-7 / L1：修复前这里抛的是**类**而不是实例（`raise exceptions.FileFormatError`），
            # err_msg 只进了日志——上层捕获到的异常 `str()` 为空，YAML 的行号/语法错误细节全丢
            # （旁边 JSON 分支一直是 `FileFormatError(err_msg)`，两侧口径不一致）。
            raise exceptions.FileFormatError(err_msg) from ex

        # NOTICE（0920 批次 6 / **N35**）：YAML 1.1 会把 `01234` / `off` 这类标量
        # **静默改义**（见 `warn_yaml_scalar_coercions`）。这里只告警、不改解析器。
        warn_yaml_scalar_coercions(yaml_content, yaml_file)

        return yaml_content


def warn_yaml_scalar_coercions(yaml_content: Any, yaml_file: Text) -> List[Text]:
    """告警：YAML 1.1 把用户写的**文本**悄悄改成了别的类型/值（0920 批次 6 / N35）。

    判据（精确、不靠猜）：拿**标量的原始文本**与 PyYAML 解析出来的值比，只看这两类：

    | 原始文本 | PyYAML 解析成 | 为什么算被改坏 |
    | --- | --- | --- |
    | `01234` / `0755` | `668` / `493`（**八进制**） | 邮编/订单号/权限位这类"带前导 0 的编号"是**文本**，不是八进制数 |
    | `yes`/`no`/`on`/`off`/`true`/`false`（任意大小写） | `True` / `False` | YAML 1.1 的布尔词表比 YAML 1.2 / JSON 宽得多；服务端常按**字符串**收 |

    `1.10` → `1.1` 这类**不**告警：数值语义上等价，且无法与"用户确实想要 1.1"区分
    （告警会变成噪音，噪音会让真告警被忽略）。

    NOTICE（为什么是**告警**而不是改解析器）：把 Loader 换成 YAML 1.2 语义会**改变
    所有既有用例**的解释（`no` 从 False 变回字符串），风险远大于收益，且本仓
    127 个既有 YAML 全依赖现状。这里的口径与本仓一贯做法一致 ——
    **不静默改写用户数据，但要让"被改写了"这件事可见**，并给出改法（加引号）。
    """
    problems: List[Text] = []

    # 只匹配"会被 YAML 1.1 改义"的标量文本。
    #
    # NOTICE（**刻意排除 `true`/`false`**）：它们是 YAML 1.2 与 JSON 的**标准布尔**写法，
    # 用户写 `flag: true` 就是想要布尔值 —— 对它告警属于**噪音**，
    # 而噪音会让真正该看的告警被忽略。要抓的是 YAML 1.1 **独有**的那批词
    # （`yes`/`no`/`on`/`off`/`y`/`n`）：用户写 `state: no` 时，
    # 十有八九想要的是**字符串 "no"**（服务端按枚举收），却被变成布尔 `False`。
    octal_re = re.compile(r"^[-+]?0[0-7]+$")
    yaml11_only_bool_words = {"yes", "no", "on", "off", "y", "n"}

    # 判据必须看**原文**（解析后的值已经看不出原文长什么样），所以扫描 YAML 文本本身。
    try:
        with open(yaml_file, encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError:
        return problems

    scalar_re = re.compile(
        r"""^\s*(?:-\s+)?(?P<key>[^\s:#][^:]*?)\s*:\s*(?P<value>[^\s#]+)\s*(?:\#.*)?$"""
    )

    for line_no, line in enumerate(raw_lines, start=1):
        match = scalar_re.match(line.rstrip("\n"))
        if not match:
            continue
        key = match.group("key").strip().strip("'\"")
        text = match.group("value").strip()
        if not text or text[0] in "[{&*!|>":
            continue
        # 已经有引号 = 用户显式声明成文本，不会被强转
        if text[0] in ("'", '"'):
            continue

        reason = ""
        if octal_re.match(text):
            reason = f"会被当成**八进制数**解析成 {int(text, 8)}"
        elif text.lower() in yaml11_only_bool_words:
            reason = f"会被当成**布尔值**解析成 {text.lower() in ('yes', 'on', 'y')}"

        if reason:
            problems.append(
                f"第 {line_no} 行 `{key}: {text}` —— {reason}"
            )

    if problems:
        detail = "\n".join(f"    - {item}" for item in problems[:8])
        more = f"\n    （另有 {len(problems) - 8} 处）" if len(problems) > 8 else ""
        logger.warning(
            f"用例里的标量会被 YAML 1.1 规则**静默改义**（{yaml_file}）：\n"
            f"{detail}{more}\n"
            f"  后果：请求体/变量里发出去的**不是你在 YAML 里写的那个值**"
            f"（例如 `zipcode: 01234` 变成 `668`），而且生成物里已经是改过的值，"
            f"光看 YAML 看不出来。\n"
            f"  改法：**加引号**声明成文本 —— `zipcode: \"01234\"`、`state: \"no\"`。\n"
            f"  NOTICE: 本框架刻意**不改** YAML 解析器（那会改变所有既有用例的语义），"
            f"只把「被改义」这件事变可见。"
        )

    return problems


def _reject_duplicate_json_keys(pairs):
    """`object_pairs_hook`：JSON 里出现重复键时**报错**（0920 批次 6 / **N41**）。

    NOTICE: YAML 分支有 `_DuplicateKeyRejectingSafeLoader`（批次 A / H1），
    而 JSON 分支一直是裸 `json.load` —— 而 `json.load` 的语义同样是
    **后者胜、前者静默丢弃**。两种受支持的用例格式，一侧有护栏、一侧没有：

    ```json
    {"teststeps": [{"name": "s", "validate": [{"eq": ["status_code", 999]}],
                                  "validate": [{"eq": ["status_code", 200]}]}]}
    ```

    前一段（含**故意失败**的断言）整体消失 → `hmake` exit 0、零告警、`hrun` 1 passed
    —— 与 YAML 侧被修掉的那个假通过形态**一字不差**，只是换个输入格式。

    这里用 `object_pairs_hook` 在**解析期**判重：能拿到精确的键名，
    且不依赖解析后的 dict（那时前一个键已经不存在了）。
    """
    seen: Dict[Text, int] = {}
    duplicates = []
    for key, _value in pairs:
        if key in seen:
            duplicates.append(key)
        else:
            seen[key] = 1

    if duplicates:
        raise exceptions.FileFormatError(
            f"JSON 里出现了**重复的键**（`json.load` 默认「后者胜」，前一个会被**静默丢弃**）：\n"
            f"  重复键: {sorted(set(duplicates))}\n"
            f"  为什么必须报错：被丢弃的那一段可能正是**断言**或**钩子**——用例会因为"
            f"「少校验了一部分」而**假通过**（YAML 侧早就是同样的口径）。\n"
            f"  改法：把两段**合并成一个列表**（JSON 里同一个键只能出现一次）；"
            f"确实要分两组断言时，请拆成两个 step。"
        )

    return dict(pairs)


def _load_json_file(json_file: Text) -> Dict:
    """load json file and check file content format"""
    with open(json_file, mode="rb") as data_file:
        try:
            # NOTICE（0920 批次 6 / N41）：带上 `object_pairs_hook` 判重键，
            # 与 YAML 分支的 `_DuplicateKeyRejectingSafeLoader` 对齐（见上面的说明）。
            json_content = json.load(data_file, object_pairs_hook=_reject_duplicate_json_keys)
        except exceptions.FileFormatError:
            raise
        except json.JSONDecodeError as ex:
            err_msg = f"JSONDecodeError:\nfile: {json_file}\nerror: {ex}"
            raise exceptions.FileFormatError(err_msg)
        except ValueError as ex:
            # NOTICE（残留缺陷，与 YAML 侧 `construct_yaml_int` 同源）：
            # JSON 里一个**未加引号的超长整数**会让 `json.load` 抛**裸** `ValueError`
            # （CPython 3.11+ 的 int↔str 4300 位上限）——它不是 `JSONDecodeError`
            # （后者是它的**子类**，所以 `except json.JSONDecodeError` 接不住它），
            # 于是同样地：报错没有文件名、且不是 MyBaseError → 整批 `hmake` 被拖死。
            raise exceptions.FileFormatError(
                f"JSON 解析失败:\nfile: {json_file}\nerror: {ex}\n"
                f"  若原因是「整数位数超过 4300」：JSON 里没有超长数字这一说，"
                f"请把它写成字符串（加引号）——长订单号/卡号/编号本来就该是字符串。"
            ) from ex

        return json_content


def load_test_file(test_file: Text) -> Dict:
    """load testcase/testsuite file content"""
    if not os.path.isfile(test_file):
        raise exceptions.FileNotFound(f"test file not exists: {test_file}")

    file_suffix = os.path.splitext(test_file)[1].lower()
    if file_suffix == ".json":
        test_file_content = _load_json_file(test_file)
    elif file_suffix in [".yaml", ".yml"]:
        test_file_content = _load_yaml_file(test_file)
    else:
        # '' or other suffix
        raise exceptions.FileFormatError(
            f"testcase/testsuite file should be YAML/JSON format, invalid format file: {test_file}"
        )

    return test_file_content


def warn_unknown_testcase_fields(testcase: Dict) -> None:
    """对无法识别的 config / request / step 字段给出告警。

    NOTICE: pydantic 默认 ``extra="ignore"``，模型里没有的字段会被**静默丢弃**——
    例如 YAML 里写 `proxies:`（修复前 TRequest 没有该字段）时，用户以为配了代理、
    实际请求直连，排查成本极高。这里在用例加载期显式暴露出来。

    为什么只告警不报错：既有用例里可能残留历史字段（v2/v3 时代的写法），
    直接报错属破坏性变更；目标是让问题在 hmake 阶段可见（与 0916-7 对
    非字面量值的处置原则一致）。

    NOTICE（2026-09-17 新增 step 级检查）：此前只检查 config 与 request，
    **step 级的未知键完全不告警**——最典型的后果是钩子写成单数 `teardown_hook:`
    （`make.py` 只认复数），钩子被静默丢弃、**根本不会执行**，用例反而因为
    「什么都没做」而通过（实测：单数 + 钩子里 raise → 1 passed；复数 → 1 failed）。

    NOTICE（0920 批次 6 / **N36**）：**顶层**同样要检查。修复前这个函数把 config、
    每个 step、每个 request 都查了，唯独没查**顶层** —— 而它存在的意义正是
    「让写了却被忽略的字段可见」，**顶层恰恰是最容易整块写错的地方**：

    ```yaml
    config: {name: x, base_url: …}
    variables: {token: abc}     # ← 顶层！模型 extra="ignore" 直接丢，零告警
    teststeps: […]
    ```

    用户以为配了变量（其实应该写在 `config.variables` 下），用例照跑、变量取不到，
    排查时只会看到 `VariableNotFound`，而 YAML 里明明"写了"。
    """
    # 顶层：只认 `config` / `teststeps` 两个键（v2/v3 的 `api` 由 compat 在上游升级，
    # 走不到这里；`TestCase` 模型也是这两个字段）。
    top_level_keys = {"config", "teststeps"}
    unknown_top_fields = sorted(set(testcase) - top_level_keys)
    if unknown_top_fields:
        message = (
            f"用例**顶层**存在无法识别的字段：{unknown_top_fields}，它们会被忽略"
            f"（不会写进生成的用例，也不会执行）。\n"
            f"用例顶层只认：{sorted(top_level_keys)}\n"
        )
        # 最常见的两类误放：应该写进 config 的
        moved = sorted(set(unknown_top_fields) & KNOWN_CONFIG_FIELDS)
        if moved:
            message += (
                f"hint: {moved} 是 **config 级**字段——请写进 `config:` 下面"
                f"（例如 `config:\\n  {moved[0]}: …`）。"
                f"放在顶层会被**静默丢弃**，运行期表现为取不到值/配置不生效。"
            )
        logger.warning(message)

    config = testcase.get("config")
    if isinstance(config, dict):
        unknown_config_fields = sorted(set(config) - KNOWN_CONFIG_FIELDS)
        if unknown_config_fields:
            logger.warning(
                f"config 中存在无法识别的字段：{unknown_config_fields}，它们会被忽略"
                f"（不会写进生成的用例）。\n"
                f"已支持的 config 字段：{sorted(KNOWN_CONFIG_FIELDS)}"
            )

    for step in testcase.get("teststeps") or []:
        if not isinstance(step, dict):
            continue

        unknown_step_fields = sorted(set(step) - KNOWN_STEP_FIELDS)
        if unknown_step_fields:
            message = (
                f"步骤 {step.get('name')!r} 中存在无法识别的字段：{unknown_step_fields}，"
                f"它们会被忽略（既不会写进生成的用例，也不会执行）。\n"
                f"已支持的 step 字段：{sorted(KNOWN_STEP_FIELDS)}"
            )
            singular_hooks = sorted(set(unknown_step_fields) & {"setup_hook", "teardown_hook"})
            if singular_hooks:
                message += (
                    f"\nhint: {singular_hooks} 是**单数**写法——YAML 里必须用复数 "
                    "`setup_hooks:` / `teardown_hooks:`（生成的 Python 代码里方法名才是单数 "
                    "`.setup_hook()` / `.teardown_hook()`）。写成单数钩子**不会执行**，"
                    "用例会因为「什么都没做」而通过。"
                )
            logger.warning(message)

        request = step.get("request")
        if not isinstance(request, dict):
            continue

        unknown_request_fields = sorted(set(request) - KNOWN_REQUEST_FIELDS)
        if unknown_request_fields:
            logger.warning(
                f"步骤 {step.get('name')!r} 的 request 中存在无法识别的字段："
                f"{unknown_request_fields}，它们会被忽略（既不会写进生成的用例，"
                f"也不会真的发给请求）。\n"
                f"已支持的 request 字段：{sorted(KNOWN_REQUEST_FIELDS)}\n"
                "hint: 代理/客户端证书/流式读取请分别用 proxies / cert / stream；"
                "Basic 认证请用 headers + debugtalk 生成 Authorization。"
            )


def load_testcase(testcase: Dict) -> TestCase:
    # 0917-1：空文件 / 内容不是 YAML 映射时，`yaml.safe_load` 返回 None，
    # 原来会在 `testcase.get("config")` 抛 `AttributeError: 'NoneType' object has no attribute 'get'`
    # （看不出是哪个文件、也不知道是空文件）。这里先给出明确错误。
    if not isinstance(testcase, dict):
        raise exceptions.FileFormatError(
            f"用例内容不是 YAML/JSON 映射（实际是 {type(testcase).__name__}: {testcase!r}）——"
            "常见原因：文件是**空文件**、内容被注释掉了，或 YAML 顶层不是 `config: / teststeps:` 结构。"
        )

    # 先把「写了但会被忽略」的字段暴露出来，再做模型校验
    warn_unknown_testcase_fields(testcase)

    try:
        # validate with pydantic TestCase model
        testcase_obj = TestCase.model_validate(testcase)
    except ValidationError as ex:
        err_msg = f"TestCase ValidationError:\nerror: {ex}\ncontent: {testcase}"
        raise exceptions.TestCaseFormatError(err_msg)

    return testcase_obj


def load_testcase_file(testcase_file: Text) -> TestCase:
    """load testcase file and validate with pydantic model"""
    testcase_content = load_test_file(testcase_file)
    testcase_obj = load_testcase(testcase_content)
    testcase_obj.config.path = testcase_file
    return testcase_obj


def _parse_dot_env_file(dot_env_path: Text) -> Dict[Text, Text]:
    """解析 `.env` 内容（`load_dot_env_file` 的纯解析部分，**不碰 `os.environ`**）。

    0919-2 / L34：改用 `python-dotenv` 做实际解析，但在外面包三层适配，
    保住本仓原有的三条不变量。修复前的自研解析只做 `bytes.split(b"=", 1)`，
    于是全是「按分隔符切开就完事」，踩了三个坑（实测）：

    ```text
    KEY="quoted value"         ->  '"quoted value"'          （引号留在值里）
    KEY='single quoted'        ->  "'single quoted'"
    export KEY=value           ->  {'export KEY': 'value'}   （export 进了**键名**）
    KEY=has-inline  # comment  ->  'has-inline  # comment'   （注释当成值）
    KEY="line1\\nline2"         ->  '"line1\\nline2"'          （转义不生效）
    ```

    ### 适配一：冒号写法继续可用

    上游 httprunner 的 `.env` 解析支持 `KEY: value`（原实现的 `elif b":" in line`），
    而 python-dotenv **不认冒号**——实测它把整行丢掉、只往 stderr 打一句
    `could not parse statement starting at line N`。直接换库 = 这些变量**静默消失**。
    这里先把冒号写法的行归一成 `KEY=value` 再交给它
    （只换**第一个**冒号，值里可能含 `http://…`）。

    归属判据见 `_normalize_dot_env_lines`：**取先出现的那个分隔符**，
    而不是 `"=" in line`——后者会让值里带 `=` 的冒号写法（`SECRET: dGhpc2lzYQ==`）
    被误判成等号写法，进而让整个项目 hmake/hrun 硬失败（批次 2 / M1）。

    ### 适配二：`${...}` 原样保留

    python-dotenv 默认做**插值展开**（实测 `DERIVED=${BASE}-world` → `'hello-world'`），
    而本仓的 `.env` 一直被当作「字面量来源」，插值属于语义变更。用 `interpolate=False` 关掉。

    ### 适配三：**不许静默丢弃**

    这是最容易漏的一条。python-dotenv 对「看起来不像赋值」的行**既不报错也不返回 None**，
    而是**整行丢掉**——实测下面这些在它眼里都返回 `{}`：

    ```text
    =value                  （空键名）
    KEY="abc                （引号没闭合）
    KEY WITH SPACE=v        （键名带空格）
    ```

    而修复前的实现虽然语义粗糙，却是**照单全收**的（`KEY="abc` 会得到 `KEY` → `'"abc'`），
    也就是说换库会让这些变量从「有个歪值」变成「**干脆不存在**」——往往是更难查的失败
    （运行期才以 `EnvNotFound` 之类冒出来）。
    所以这里不用「有没有 None 值」做判据（那只能覆盖一部分），而是在预扫阶段把
    **每个赋值行应当产出的键名**记下来，解析完与 dotenv 的结果核对：
    少了哪个键，就带**行号**报错。宁可报错，也不静默丢。

    ### 其他

    用 `utf-8-sig` 读：带 BOM 的 `.env`（Windows 编辑器常见写法）会让第一个键变成
    `\\ufeffKEY`，与 `cli.py` 里 M23 记过的是同一类坑。

    Raises:
        exceptions.FileFormatError: 行内既无 `=` 也无 `:`；或某行被解析器静默丢弃。
    """
    from dotenv import dotenv_values  # noqa: PLC0415 - 仅此函数需要

    with open(dot_env_path, mode="r", encoding="utf-8-sig") as fp:
        raw_lines = fp.read().splitlines()

    normalized_lines, expected_keys = _normalize_dot_env_lines(raw_lines, dot_env_path)

    parsed = dotenv_values(
        stream=io.StringIO("\n".join(normalized_lines)),
        interpolate=False,
    )

    # 兜底：dotenv 对个别畸形行给的是 `None` 值（例如裸键名），这同样不能放过
    for key, value in parsed.items():
        if value is None:
            raise exceptions.FileFormatError(
                f".env format error: 键 {key!r} 没有解析出值（该行不是合法的 `KEY=value`）。\n"
                f"  file: {dot_env_path}"
            )

    dropped = [
        (line_no, key) for line_no, key in expected_keys if key not in parsed
    ]
    if dropped:
        detail = "\n".join(f"    第 {line_no} 行 -> 期望的键名 {key!r}" for line_no, key in dropped)
        raise exceptions.FileFormatError(
            f".env format error: 有 {len(dropped)} 行被解析器**静默丢弃**了"
            f"（这类变量会直接从环境里消失，比报错难查得多）。\n"
            f"  file: {dot_env_path}\n"
            f"{detail}\n"
            # NOTICE（批次 2 / M1）：原来这里写着「冒号与等号混用」。该说法已经是**假的** ——
            # 分隔符按「先出现的那个」判之后，混用不再是被丢弃的原因（`K: a=b` 能正常解析）。
            # 报错文案点错原因等于没有报错，所以换成实测确认过的三条成因。
            f"  常见原因：键名带空格、键名为空（`=value`）、引号没闭合或引号后还有多余字符。"
        )

    return {key: value for key, value in parsed.items()}


def _unterminated_quote(value: Text) -> Optional[Text]:
    """值是否**引号没有闭合**（返回那个引号字符，否则 `None`）。

    NOTICE（0919-2 / 缺陷 5）：判据只认「值**以引号开头**、且找不到配对的闭合引号」
    这一种形态 —— 与 python-dotenv 的语义一致。**不能**简单数引号个数：
    `NOTE=don't panic` 只有一个引号却是**完全合法**的值，数个数会把合法用例误判成多行值
    （而错误提示一旦说谎，就等于没有）。

    本函数**只用于给报错文案定位**，不参与解析 —— 不扩大本框架接受的语法面
    （见 `_normalize_dot_env_lines` 的说明）。
    """
    if not value:
        return None

    quote = value[0]
    if quote not in ('"', "'"):
        return None

    index = 1
    while index < len(value):
        char = value[index]
        if char == "\\":  # 反斜杠转义：跳过下一个字符
            index += 2
            continue
        if char == quote:
            return None  # 闭合了
        index += 1

    return quote


def _normalize_dot_env_lines(
    raw_lines: List[Text], dot_env_path: Text
) -> Tuple[List[Text], List[Tuple[int, Text]]]:
    """`.env` 预扫：归一化冒号写法，并记下每个赋值行**应当**产出的键名。

    分隔符归属口径（批次 2 / M1）：**先出现的那个**为准 ——
    `=` 在前按等号写法，`:` 在前按冒号写法。两种写法各自只切**第一个**分隔符，
    另一侧的符号留在值里。

    Returns:
        (归一化后的行, [(行号, 期望键名), ...])
    """
    normalized_lines = []
    expected_keys = []

    # 最近一个「引号没有闭合」的赋值行（用于把「续行被当成格式错误」诊断准，缺陷 5）
    pending_multiline: Optional[Tuple[int, Text]] = None

    for line_no, raw_line in enumerate(raw_lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            # 空行与注释行**不**清除 pending —— 多行值的续行之间也可能夹杂它们
            normalized_lines.append(raw_line)
            continue

        # NOTICE（批次 2 / M1）：分隔符判据取「**先出现的那个**」，**不能**写成 `"=" in line`。
        #
        # `"=" in line` 只看整行里有没有 `=`，于是只要**值**里带一个 `=`，
        # 冒号写法就被误判成等号写法。实测 `B64_SECRET: dGhpc2lzYQ==`（base64 结尾）
        # 会算出 `key_part = 'B64_SECRET: dGhpc2lzYQ'` —— 一个用户文件里**根本不存在**的键名；
        # 预扫的「应有键名」与 python-dotenv 的结果对不上，于是走下面的
        # 「不许静默丢弃」分支：**整个项目**的 hmake/hrun 全部 exit 1，
        # 而报错文案指的是那个虚构键名，看不出根因是冒号写法。
        # 同一坑覆盖 `KEY: a=b`（带签名 URL）、`ENDPOINT: http://h/p?x=1`（query 带 `=`）等常见写法。
        #
        # 判据等价于「`line.split("=", 1)[0]` 里是否含 `:`」，但用 `find` 写更直白：
        #   - 第一个分隔符是 `=` -> 等号写法，值里的 `:` 原样保留（`URL=https://h/a?b=1`）
        #   - 第一个分隔符是 `:` -> 冒号写法，只换它，值里的 `=`、`://`、`:` 原样保留
        #
        # 口径说明（不扩大语法面）：这只改变了「同一行 `=` 与 `:` 都出现」时的归属。
        # 实测修复前（自研解析）这类行的键名是 `'B64_SECRET: dGhpc2lzYQ'` 这种**永远取不到**的垃圾键
        # （`${B64_SECRET}` 解析不到，只是**静默**不报错），所以没有任何**可用**写法被改变。
        eq_index = line.find("=")
        colon_index = line.find(":")
        if eq_index != -1 and (colon_index == -1 or eq_index < colon_index):
            normalized_lines.append(raw_line)
            key_part, value_part = line.split("=", 1)
        elif colon_index != -1:
            # 冒号写法（上游遗留）：走到这里说明第一个 `:` 前面没有 `=`，
            # 所以第一个冒号**就是**分隔符，只换它即可（值里的 `http://…` 不受影响）
            normalized_lines.append(line.replace(":", "=", 1))
            key_part, value_part = line.split(":", 1)
        else:
            # 与修复前的口径一致：既没有 `=` 也没有 `:` 就是格式错误
            #
            # NOTICE（0919-2 / 缺陷 5）：修复前这里只有一句「第 N 行既没有 `=` 也没有 `:`」，
            # 而多行引号值（python-dotenv 支持、本框架不支持）恰好就是以这个形态撞上来的：
            #     KEY2="line1
            #     line2"          ← 这一行既没有 = 也没有 : ，报错却完全看不出是被拆开了
            # 现在带上一句**定位到具体行**的提示（判据见 `_unterminated_quote`）。
            # 注意：本批**只改文案不改行为** —— 仍然报错，不扩大接受的语法面。
            hint = ""
            if pending_multiline is not None:
                prev_no, prev_line = pending_multiline
                hint = (
                    f"\n  提示：第 {prev_no} 行 `{prev_line}` 的引号**没有闭合**，"
                    f"所以本行被当作它的**续行**了。\n"
                    f"  多行引号值（python-dotenv 本身支持）在本框架里不可用：预扫是逐行处理的，"
                    f"而它正是「不许静默丢弃」那条不变量的实现，多行值会让它无法确定哪些行是赋值行。\n"
                    f'  请改成**单行**写法，用字面量反斜杠 n 表示换行：'
                    f'KEY="line1\\nline2"'
                )
            raise exceptions.FileFormatError(
                f".env format error: 第 {line_no} 行既没有 `=` 也没有 `:`。\n"
                f"  file: {dot_env_path}\n"
                f"  该行: {line}\n"
                # 同上的文案订正：本行**一个分隔符都没有**，「混用」在这里也说不通
                f"  常见原因：漏写了 `=`（或 `:`）、或本行是上一行引号的续行。{hint}"
            )

        # 记录「值以引号开头但没闭合」的行（只用于下一条报错文案的定位）
        pending_multiline = (
            (line_no, line) if _unterminated_quote(value_part.strip()) else None
        )

        key = key_part.strip()
        # `export KEY=value` 是合法写法，dotenv 会把 export 前缀去掉，这里对齐口径
        if key[:7].lower() == "export ":
            key = key[7:].strip()
        expected_keys.append((line_no, key))

    return normalized_lines, expected_keys


def load_dot_env_file(dot_env_path: Text) -> Dict:
    """load .env file.

    Args:
        dot_env_path (str): .env file path

    Returns:
        dict: environment variables mapping

            {
                "UserName": "debugtalk",
                "Password": "123456",
                "PROJECT_KEY": "ABCDEFGH"
            }

    Raises:
        exceptions.FileFormatError: If .env file format is invalid.

    NOTICE（0919-2 / L34）：解析交给 `python-dotenv`（见 `_parse_dot_env_file` 的说明）。
    这里刻意用 `dotenv_values()` 而**不是** `load_dotenv()`：前者只返回 dict、不碰
    `os.environ`，这样仍由下面的 `utils.set_os_environ` 统一写入——
    保持本仓「**无条件覆盖**已有环境变量」的既有语义
    （`load_dotenv()` 默认 `override=False`，行为并不相同）。
    """
    if not os.path.isfile(dot_env_path):
        return {}

    logger.info(f"Loading environment variables from {dot_env_path}")
    env_variables_mapping = _parse_dot_env_file(dot_env_path)

    utils.set_os_environ(env_variables_mapping)
    return env_variables_mapping


def apply_project_dot_env(dot_env_path: Optional[Text]) -> Dict[Text, Any]:
    """应用某个项目的 `.env`，**并先撤销上一个项目写进 `os.environ` 的键**。

    NOTICE（0920 批次 2 / **N17**）：`.env` 是靠"写进 `os.environ`"生效的**全局副作用**，
    而 `utils.set_os_environ` 只写不撤（`utils.unset_os_environ` 全仓**没有任何调用**）。
    于是同一进程里跑多项目时，**没有 `.env` 的项目会静默继承上一个项目的环境**：

    ```text
    projA 有 .env: PROJ=projA   → os.environ["PROJ"] = "projA"
    projC 没有 .env             → 什么也不做，"PROJ" 仍是 "projA"
    projC 的 ${ENV(PROJ)}       → 静默解析成 "projA"，请求打到 **A 的环境/凭据** 上
    ```

    实测（`.tmp_audit/verify_env_iso.py`）：`projC` **单独**跑报 `EnvNotFound: PROJ`；
    在 `projA` 之后同进程再跑，这个错误**消失**、请求真的发出去了 ——
    "有没有前置项目"会改变用例的行为，这是最难查的一类不一致。
    修复前只有 M7 补了"换回**已缓存**项目时重新应用它的 `.env`"那半边，
    "切到**没有** `.env` 的项目时清掉上一个项目的键"这半边一直是空的。

    撤销时**只**撤销"值仍等于框架写进去的那个值"的键：用户在被测环境里手动覆盖过
    同名变量时不动它，避免误伤外部配置。
    """
    global _project_env_keys

    for key, value in list(_project_env_keys.items()):
        if os.environ.get(key) == value:
            os.environ.pop(key, None)
            logger.debug(f"Unset OS environment variable from previous project: {key}")
    _project_env_keys = {}

    if not dot_env_path:
        # 本项目没有 `.env` → 保持"没有就是没有"（这正是修复前漏掉的一步）
        return {}

    dot_env = load_dot_env_file(dot_env_path)
    if dot_env:
        _project_env_keys = dict(dot_env)
    return dot_env


def load_csv_file(csv_file: Text) -> List[Dict]:
    """load csv file and check file content format

    Args:
        csv_file (str): csv file path, csv file content is like below:

    Returns:
        list: list of parameters, each parameter is in dict format

    Examples:
        >>> cat csv_file
        username,password
        test1,111111
        test2,222222
        test3,333333

        >>> load_csv_file(csv_file)
        [
            {'username': 'test1', 'password': '111111'},
            {'username': 'test2', 'password': '222222'},
            {'username': 'test3', 'password': '333333'}
        ]

    """
    if not os.path.isabs(csv_file):
        global project_meta
        if project_meta is None:
            raise exceptions.MyBaseFailure("load_project_meta() has not been called!")

        # make compatible with Windows/Linux
        csv_file = os.path.join(project_meta.RootDir, *csv_file.split("/"))

    if not os.path.isfile(csv_file):
        # file path not exist
        raise exceptions.CSVNotFound(csv_file)

    csv_content_list = []

    # NOTICE（批次 B / **M9**）：必须用 `utf-8-sig` 读，与 `.env`（`load_dot_env_file`）、
    # `cli.main_convert`、`converters/*` 三处**已有的同一口径**对齐。
    #
    # 用 `utf-8` 时 BOM（`\ufeff`）会留在**第一列的列名**里，于是列名变成 `\ufeffusername`：
    #   - `${parameterize(accounts.csv)}` 这条路径会在 `parser.parse_parameters` 里
    #     按参数名取键 → 收集期裸 `KeyError: 'username'`（pytest exit 2，**整个文件**跑不起来，
    #     而报错里一个字都没提编码/BOM，CSV 里明明写着 `username`）；
    #   - 而 BOM 是 Excel「CSV UTF-8」与 PowerShell `Set-Content -Encoding UTF8` 的**默认产物**
    #     —— 也就是最容易遇到的那一类 CSV。
    # `.env` 侧早有护栏（`tests/dot_env_test.py::test_utf8_bom_does_not_pollute_the_first_key`），
    # CSV 读取点当时漏了。
    with open(csv_file, encoding="utf-8-sig") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            csv_content_list.append(row)

    return csv_content_list


def load_folder_files(folder_path: Text, recursive: bool = True) -> List:
    """load folder path, return all files endswith .yml/.yaml/.json/_test.py in list.

    Args:
        folder_path (str): specified folder path to load
        recursive (bool): load files recursively if True

    Returns:
        list: files endswith yml/yaml/json
    """
    if isinstance(folder_path, (list, set)):
        files = []
        for path in set(folder_path):
            files.extend(load_folder_files(path, recursive))

        return files

    if not os.path.exists(folder_path):
        return []

    file_list = []

    for dirpath, dirnames, filenames in os.walk(folder_path):
        # NOTICE（0918-4 / L3）：`os.walk` 的返回顺序取决于文件系统，不排序的话
        # 「目录批量 hmake」的生成顺序/日志顺序/失败报告顺序都不可复现
        # （同一份用例在两次运行里可能得到不同顺序）。这里按名称排序固定下来。
        dirnames.sort()
        filenames_list = []

        for filename in sorted(filenames):
            if not filename.lower().endswith((".yml", ".yaml", ".json", "_test.py")):
                continue

            filenames_list.append(filename)

        for filename in filenames_list:
            file_path = os.path.join(dirpath, filename)
            file_list.append(file_path)

        if not recursive:
            break

    return file_list


def load_module_functions(module) -> Dict[Text, Callable]:
    """load python module functions.

    Args:
        module: python module

    Returns:
        dict: functions mapping for specified python module

            {
                "func1_name": func1,
                "func2_name": func2
            }

    """
    module_functions = {}

    for name, item in vars(module).items():
        if isinstance(item, types.FunctionType):
            module_functions[name] = item

    return module_functions


def load_builtin_functions() -> Dict[Text, Callable]:
    """load builtin module functions"""
    return load_module_functions(builtin)


def locate_file(start_path: Text, file_name: Text) -> Text:
    """locate filename and return absolute file path.
        searching will be recursive upward until system root dir.

    Args:
        file_name (str): target locate file name
        start_path (str): start locating path, maybe file path or directory path

    Returns:
        str: located file path. None if file not found.

    Raises:
        exceptions.FileNotFound: If failed to locate file.

    """
    if os.path.isfile(start_path):
        start_dir_path = os.path.dirname(start_path)
    elif os.path.isdir(start_path):
        start_dir_path = start_path
    else:
        raise exceptions.FileNotFound(f"invalid path: {start_path}")

    file_path = os.path.join(start_dir_path, file_name)
    if os.path.isfile(file_path):
        # ensure absolute
        return os.path.abspath(file_path)

    # system root dir
    # Windows, e.g. 'E:\\'
    # Linux/Darwin, '/'
    parent_dir = os.path.dirname(start_dir_path)
    if parent_dir == start_dir_path:
        raise exceptions.FileNotFound(f"{file_name} not found in {start_path}")

    # locate recursive upward
    return locate_file(parent_dir, file_name)


def locate_debugtalk_py(start_path: Text) -> Text:
    """locate debugtalk.py file

    Args:
        start_path (str): start locating path,
            maybe testcase file path or directory path

    Returns:
        str: debugtalk.py file path, None if not found

    """
    try:
        # locate debugtalk.py file.
        debugtalk_path = locate_file(start_path, "debugtalk.py")
    except exceptions.FileNotFound:
        debugtalk_path = None

    return debugtalk_path


def locate_project_root_directory(test_path: Text) -> Tuple[Text, Text]:
    """locate debugtalk.py path as project root directory

    Args:
        test_path: specified testfile path

    Returns:
        (str, str): debugtalk.py path, project_root_directory

    """

    def prepare_path(path):
        if not os.path.exists(path):
            err_msg = f"path not exist: {path}"
            logger.error(err_msg)
            raise exceptions.FileNotFound(err_msg)

        if not os.path.isabs(path):
            path = os.path.join(os.getcwd(), path)

        return path

    test_path = prepare_path(test_path)

    # locate debugtalk.py file
    debugtalk_path = locate_debugtalk_py(test_path)

    if debugtalk_path:
        # The folder contains debugtalk.py will be treated as project RootDir.
        project_root_directory = os.path.dirname(debugtalk_path)
    else:
        # debugtalk.py not found, use os.getcwd() as project RootDir.
        project_root_directory = os.getcwd()

    return debugtalk_path, project_root_directory


def load_debugtalk_functions() -> Dict[Text, Callable]:
    """load project debugtalk.py module functions
        debugtalk.py should be located in project root directory.

    Returns:
        dict: debugtalk module functions mapping
            {
                "func1_name": func1,
                "func2_name": func2
            }

    """
    # load debugtalk.py module
    #
    # NOTICE（0919-16 / 批次 G，B.8）：这里用的是**固定模块名** `debugtalk` +
    # `_activate_project_root_dir()` 把项目根置顶 `sys.path`。
    # 已知的边缘场景（未复现，仅记录）：若运行环境里存在**同名的第三方包** `debugtalk`
    # （`site-packages/debugtalk.py` 或同名包），而项目根的 `sys.path` 置顶**失效**
    # （例如调用方自己动过 `sys.path`、或用了 `PYTHONPATH` 指到别处），
    # `import_module("debugtalk")` 可能加载到**别人的模块**，于是 debugtalk 函数
    # 静默变成另一个实现——报错会出现在几百行之外，很难与本行联系起来。
    #
    # 处置（本批只加注释，不改行为）：
    #   · 换 `importlib.util.spec_from_file_location(<绝对路径>)` 可以彻底消除这个窗口，
    #     但它会改变模块的 `__name__`/`__spec__`（`importlib.reload` 的行为、
    #     以及 debugtalk 里 `from debugtalk import ...` 这类自引用都会受影响），
    #     属于**行为变更**，收益（一个未复现的边缘场景）不匹配风险；
    #   · `_activate_project_root_dir()`（见上文 M15 的说明）已经负责在切换项目时
    #     丢弃「来自其它 RootDir 的 debugtalk」，那才是实际会踩到的主路径。
    # 结论：登记为**已知边界**，需要时再按上面的方案评估。
    try:
        imported_module = importlib.import_module("debugtalk")
    except Exception as ex:
        # NOTICE: 不要 sys.exit(1)——在 pytest 进程内直接退出会丢掉其余用例的执行结果。
        # 这里抛异常让当前用例报错，其余用例继续跑；CLI 场景由上层（main_make 等）捕获后退出。
        err_msg = f"error occurred in debugtalk.py: {type(ex).__name__}: {ex}"
        logger.error(err_msg)
        raise exceptions.MyBaseError(err_msg) from ex

    # reload to refresh previously loaded module
    imported_module = importlib.reload(imported_module)
    return load_module_functions(imported_module)


def reset_project_meta() -> None:
    """清空 project_meta 及其 RootDir 缓存（测试或需要「从零开始」的场景使用）。"""
    global project_meta
    project_meta = None
    project_meta_cache.clear()


def _normalize_path(path: Text) -> Text:
    """规范化路径，用于比较与做缓存 key（Windows 大小写不敏感、分隔符统一）。"""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _is_path_in_root_dir(test_path: Text, root_dir: Text) -> bool:
    """判断 test_path 是否属于 root_dir 这个项目。

    判断规则：
        1. 路径解析后落在 root_dir 内（绝对路径，或相对 cwd 就能落到项目内的路径）；
        2. 相对路径按「相对项目根」再解析一次且该文件真实存在
           （用例里引用其它文件常写成 a-b.c/1.yml 这种相对项目根的路径，
           直接用 cwd 解析会落到项目外，从而误判为「另一个项目」）。
    """
    if not test_path or not root_dir:
        return False

    normalized_root_dir = _normalize_path(root_dir)
    normalized_path = _normalize_path(test_path)

    if normalized_path == normalized_root_dir or normalized_path.startswith(
        normalized_root_dir + os.sep
    ):
        return True

    if not os.path.isabs(test_path):
        # 仅相对路径会走到这里（多一次 exists 判断）
        joined_test_path = test_path.replace("/", os.sep).replace("\\", os.sep)
        return os.path.exists(os.path.join(root_dir, joined_test_path))

    return False


def _is_nearest_project_root(test_path: Text, root_dir: Text) -> bool:
    """`root_dir` 是否仍是 `test_path` 向上**最近的**项目标记所在目录（0918-4 / M15）。

    背景：`load_project_meta` 有一条「同一项目内直接复用已加载 meta」的快路径，
    它原来只判断「test_path 是否落在已加载 RootDir **之内**」。但项目是可以嵌套的
    （子目录里也可以有自己的 `debugtalk.py`），于是「先加载父项目，再加载嵌套子项目」
    会让子项目拿到**父项目**的 RootDir / .env / debugtalk 函数。

    实测（修复前）：
        1) 全新进程直接加载嵌套项目 → RootDir = 嵌套项目      ✅
        2) 先加载父项目，再加载同一嵌套项目 → RootDir = 父项目  ❌ 复用了旧 meta

    做法：从 `test_path` 所在目录向上走，**在到 `root_dir` 为止的这段路径里**
    找有没有更近的 `debugtalk.py`。找到 → `root_dir` 不是最近的，不能复用；
    走到 `root_dir` 都没找到 → 它就是最近的，可以复用。

    NOTICE（本函数的取舍点）：快路径原本承诺「不访问磁盘」（见 `load_project_meta`
    的 docstring），而这个检查需要 `isfile` 判断。实测代价可以忽略：
    `locate_project_root_directory()` 单次约 10.7 µs，而 `load_project_meta` 在一次
    27 个用例的测试运行里只被调用 **2** 次（O(用例数)，不是热路径）。
    用几微秒换掉「嵌套项目静默拿到父项目配置」；并且**只对真实存在的路径**做检查——
    路径不存在时（hmake 的派生路径：去掉扩展名的名字、还没写盘的生成文件路径）
    直接返回 True，保持原有宽松语义，不会把派生路径误判成「另一个项目」。
    """
    if not test_path or not root_dir:
        return False

    # 路径不存在（派生路径）→ 无从判断嵌套关系，保持「同项目内复用」的原有语义
    if not os.path.exists(test_path):
        return True

    normalized_root_dir = _normalize_path(root_dir)

    if os.path.isdir(test_path):
        current_dir = _normalize_path(test_path)
    else:
        current_dir = _normalize_path(os.path.dirname(test_path))

    while True:
        if os.path.isfile(os.path.join(current_dir, "debugtalk.py")):
            # 找到最近的项目标记：只有它等于 root_dir 时才算「同一个项目」
            return current_dir == normalized_root_dir

        if current_dir == normalized_root_dir:
            # 一路走到 root_dir 都没有更近的标记 → root_dir 就是最近的（或它本身
            # 没有 debugtalk.py，属于「无标记项目」，沿用原有宽松行为）
            return True

        parent_dir = os.path.dirname(current_dir)
        if parent_dir == current_dir:
            # 到文件系统根了还没遇到 root_dir：test_path 其实不在 root_dir 之下，
            # 交给调用方走完整定位流程判断
            return False

        current_dir = parent_dir


def _activate_project_root_dir(project_root_directory: Text) -> None:
    """让 import 解析到当前项目的 RootDir。

    NOTICE: 同一进程先加载过 A 项目的 debugtalk.py 后，sys.modules 里缓存的
    ``debugtalk`` 模块会遮蔽 B 项目——``importlib.import_module("debugtalk")``
    返回的是 A 的模块，``reload`` 也只是重载 A 的文件，于是 B 项目拿到的是 A 项目的函数。
    这里在切换 RootDir 时把该 RootDir 置顶 sys.path，并丢弃来自其它 RootDir 的
    ``debugtalk`` 模块缓存，保证重新从磁盘导入本项目自己的 debugtalk.py。
    """
    normalized_root_dir = _normalize_path(project_root_directory)

    # 同一 RootDir 在 sys.path 里只保留一份，并置顶（后加载的项目优先）
    duplicated_indexes = [
        index
        for index, path in enumerate(sys.path)
        if path and os.path.isabs(path) and _normalize_path(path) == normalized_root_dir
    ]
    for index in reversed(duplicated_indexes):
        sys.path.pop(index)
    sys.path.insert(0, project_root_directory)

    imported_module = sys.modules.get("debugtalk")
    if imported_module is None:
        return

    module_file = getattr(imported_module, "__file__", None)
    if module_file and _is_path_in_root_dir(module_file, project_root_directory):
        # 缓存的模块就来自本项目，保持复用
        return

    # 来自其它 RootDir（或来源不明）→ 丢弃，让其按新的 sys.path 重新导入
    sys.modules.pop("debugtalk", None)


def load_project_meta(
    test_path: Text,
    reload: bool = False,
    keep_loaded_meta_for_projectless: bool = True,
) -> ProjectMeta:
    """load testcases, .env, debugtalk.py functions.
        testcases folder is relative to project_root_directory
        by default, project_meta will be loaded only once, unless set reload to true.

    Args:
        test_path (str): test file/folder path, locate project RootDir from this path.
        reload: reload project meta if set true, default to false
        keep_loaded_meta_for_projectless: 当 test_path 向上找不到任何项目标记（没有
            debugtalk.py）时，是否沿用「当前已加载的 meta」。
            - True（默认，保持既有语义）：供 `parse_parameters` 这类「按调用方文件定位、
              定位不到就沿用现状」的场景使用；
            - False：`hmake`/`hrun` 生成用例时必须传 False——生成文件要落在 test_path
              **自己所属的项目**里，沿用别的项目的 RootDir 会让相对化失败。
              实测：同一进程先处理过「有 debugtalk.py 的项目」，再对无标记目录跑 hmake
              会以 `path not exist: .../case`（路径还是去掉扩展名的）退出。

    Returns:
        project loaded api/testcases definitions,
            environments and debugtalk.py functions.

    NOTICE:
        缓存以 RootDir 为粒度（project_meta_cache）。同一项目内重复调用直接命中快路径
        （只做路径字符串与「最近项目标记」判断，不重新加载 debugtalk/.env）；
        传入的 test_path 属于另一个项目时必须按它重新定位，
        否则会拿到上一个项目的 RootDir / .env / debugtalk 函数。

        NOTICE（0918-4 / M15）：快路径原先只判断「落在已加载 RootDir 之内」，
        因此**嵌套项目**（子目录里也有 debugtalk.py）会被父项目吞掉，实测：
        先加载父项目再加载嵌套子项目 → 子项目拿到父项目的 RootDir。
        现在额外要求「该 RootDir 仍是最近的项目标记」（`_is_nearest_project_root`）。
    """
    global project_meta

    if project_meta is None:
        # 外部把全局 meta 置空（测试、重新初始化）→ 视为缓存整体失效，
        # 保持「None 表示尚未加载 / 下次调用重新加载」的既有语义
        project_meta_cache.clear()

    if not test_path:
        # 没有路径信息（如 uploader 里的 load_project_meta("")）：只能返回当前已加载的 meta
        if project_meta is None or reload:
            project_meta = ProjectMeta()

        return project_meta

    if (
        project_meta
        and not reload
        and _is_path_in_root_dir(test_path, project_meta.RootDir)
        and _is_nearest_project_root(test_path, project_meta.RootDir)
    ):
        # 快路径：同一项目内重复调用，直接复用已加载的 meta。
        # NOTICE（0918-4 / M15）：第二个条件是新加的——只判断「落在 RootDir 之内」
        # 会把**嵌套项目**吞进父项目（详见 _is_nearest_project_root 的说明）。
        return project_meta

    debugtalk_path, project_root_directory = locate_project_root_directory(test_path)

    if (
        debugtalk_path is None
        and project_meta is not None
        and not reload
        and keep_loaded_meta_for_projectless
    ):
        # 传入路径向上找不到 debugtalk.py → 它不属于任何「有项目标记的项目」，
        # 无从判断应该用哪个 RootDir（如 tests/ 下的脚本、仓库根目录下的临时用例）。
        # 此时沿用当前已加载的 meta，避免把已加载项目的 RootDir/functions 丢掉。
        return project_meta

    if not reload:
        cached_project_meta = project_meta_cache.get(
            _normalize_path(project_root_directory)
        )
        if cached_project_meta is not None:
            # 换回先前加载过的项目：复用 meta，并把该 RootDir 重新置顶 sys.path
            _activate_project_root_dir(cached_project_meta.RootDir)
            # NOTICE（批次 7 / **M7**）：**必须重新应用该项目的 `.env`**。
            #
            # 快路径修复前直接 return —— 而 `.env` 是靠"写进 `os.environ`"生效的**全局**副作用，
            # 于是 A→B→A 这种多项目调用里，换回 A 时环境里留着的还是 **B 的** `.env`：
            # A 的用例读到 B 的 `${ENV(host)}` / 账号 / token，**静默跑在错环境上**
            # （请求若恰好成功，用例绿着通过）。`hrun <父目录>` 在 monorepo 下必中。
            # 慢路径（下面 `load_dot_env_file`）每次都会应用，快路径必须补齐这半边，
            # 否则"同一项目第二次进入"与"第一次进入"的环境不同 —— 最难查的一类不一致。
            # `dot_env_path` 为空（项目没有 `.env`）时不动，保持"没有就没有"的语义。
            #
            # NOTICE（0920 批次 2 / **N17**）：这里改走 `apply_project_dot_env` ——
            # 它除了应用本项目的 `.env`，还会**先撤销上一个项目的键**。
            # 修复前"切到没有 `.env` 的项目"这条路径什么也不做，
            # 于是那个项目静默继承了上一个项目的环境（详见该函数的 NOTICE）。
            cached_project_meta.env = apply_project_dot_env(
                cached_project_meta.dot_env_path
            )
            project_meta = cached_project_meta
            return project_meta

    project_meta = ProjectMeta()

    # add project RootDir to sys.path
    _activate_project_root_dir(project_root_directory)

    # load .env file
    # NOTICE:
    # environment variable maybe loaded in debugtalk.py
    # thus .env file should be loaded before loading debugtalk.py
    dot_env_path = os.path.join(project_root_directory, ".env")
    # NOTICE（0920 批次 2 / **N17**）：走 `apply_project_dot_env` —— 切换项目时
    # **先撤销上一个项目的 `.env` 键**，避免"没有 `.env` 的项目静默继承上一个项目的环境"。
    dot_env = apply_project_dot_env(dot_env_path)
    if dot_env:
        project_meta.env = dot_env
        project_meta.dot_env_path = dot_env_path

    if debugtalk_path:
        # load debugtalk.py functions
        debugtalk_functions = load_debugtalk_functions()
    else:
        debugtalk_functions = {}

    # locate project RootDir and load debugtalk.py functions
    project_meta.RootDir = project_root_directory
    project_meta.functions = debugtalk_functions
    project_meta.debugtalk_path = debugtalk_path

    project_meta_cache[_normalize_path(project_root_directory)] = project_meta

    return project_meta


def relative_to_root_dir(abs_path: Text, project_meta: ProjectMeta) -> Text:
    r"""把绝对路径转成相对 ``project_meta.RootDir`` 的路径（**不重新定位项目**）。

    NOTICE: 与 `convert_relative_project_root_dir()` 的区别在于「项目从哪来」：
    后者会按传入路径**重新定位**项目，因此只适合「路径真实存在」的场景。而 `hmake`
    需要相对化的往往是**派生路径**——去掉扩展名的名字、还没写盘的生成文件路径——
    这些路径重新定位时可能命中别的项目、甚至因为不存在而直接抛 `FileNotFound`
    （实测：同进程先处理过别的项目后，对无 debugtalk.py 的目录跑 hmake 会以
    `path not exist: .../case` 退出）。这里直接用调用方已定位好的 project_meta。

    NOTICE（0918-4 / M8）：归属判断必须与 `_is_path_in_root_dir` **同口径**——
    两边都用 `_normalize_path`（normcase + normpath + abspath）并带上 `os.sep`。
    修复前这里是裸的 `abs_path.startswith(project_meta.RootDir)`，两个方向都会错：
      - `<root>2/x.yml` 被误判成**项目内**（`<root>` 是 `<root>2` 的前缀），
        随后返回一个假相对路径 `'\\x.yml'`，错误被推到很远的地方才暴露；
      - Windows 上大小写不同（`D:\\...` 与 `d:\\...`）被误判成**项目外**而直接报错。

    NOTICE（0918-8 / M17）：取值改用 `os.path.relpath`，**不再拿原始字符串切片**。
    修复前是 `abs_path[len(project_meta.RootDir) + 1:]`（理由是"保持既有输出格式、避免生成物漂移"），
    于是「归属判断用规范化路径、取值却切原始串」两个口径不一致。
    只要输入里有冗余成分（`.\`、`..`、重复分隔符），偏移量就对不上，返回的相对路径会从中间截断：

        实测 `hmake .\nested\case.yml`（RootDir = …\nested）→ 返回 `d\case`
             → 生成物落到 `nested\d\case_test.py`（**错目录**，而日志仍说"成功"）；
        实测 `hmake nested//case.yml` → 切出空段 → `IndexError: string index out of range`。

    `os.path.relpath` 内部对两侧都做 normpath（吃掉 `.`/`..`/重复分隔符），
    且**保留输入的大小写**，因此对「本来就干净」的路径，结果与修复前**逐字一致**
    （既有生成物零漂移，由 `tests/generated_artifacts_drift_test.py` 守着）。
    """
    normalized_abs_path = _normalize_path(abs_path)
    normalized_root_dir = _normalize_path(project_meta.RootDir)

    inside_root_dir = (
        normalized_abs_path == normalized_root_dir
        or normalized_abs_path.startswith(normalized_root_dir + os.sep)
    )
    if not inside_root_dir:
        raise exceptions.ParamsError(
            f"failed to convert absolute path to relative path based on project_meta.RootDir\n"
            f"abs_path: {abs_path}\n"
            f"project_meta.RootDir: {project_meta.RootDir}"
        )

    relative_path = os.path.relpath(os.path.abspath(abs_path), project_meta.RootDir)
    if relative_path == os.curdir:
        # 传入的就是 RootDir 本身：`ensure_file_abs_path_valid` 依赖「空串 = 没有可派生的名字」
        return ""

    return relative_path


def convert_relative_project_root_dir(abs_path: Text) -> Text:
    """convert absolute path to relative path, based on project_meta.RootDir

    Args:
        abs_path: absolute path

    Returns: relative path based on project_meta.RootDir

    """
    _project_meta = load_project_meta(abs_path)
    return relative_to_root_dir(abs_path, _project_meta)
