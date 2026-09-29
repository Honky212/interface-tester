"""
This module handles compatibility issues between testcase format v2, v3 and v4.
"""
import os
import re
import sys
from typing import Any, Dict, List, Set, Text, Tuple, Union

from loguru import logger

from interfacetester import exceptions
from interfacetester import loader
from interfacetester.loader import load_project_meta
from interfacetester.models import KNOWN_STEP_FIELDS, TStep
from interfacetester.parser import parse_data
from interfacetester.utils import lower_dict_keys, sort_dict_by_custom_order


def convert_variables(
    raw_variables: Union[Dict, Text], test_path: Text
) -> Dict[Text, Any]:
    if isinstance(raw_variables, Dict):
        return raw_variables

    elif isinstance(raw_variables, Text):
        # get variables by function, e.g. ${get_variables()}
        project_meta = load_project_meta(test_path)
        variables = parse_data(raw_variables, {}, project_meta.functions)

        return variables

    else:
        raise exceptions.TestCaseFormatError(
            f"Invalid variables format: {raw_variables}"
        )


def convert_parameters(raw_parameters: Any, test_path: Text) -> Any:
    """把 `config.parameters` 的**字符串形态**（`${func()}`）在生成期求值。

    NOTICE（0920 批次 6 / **N34**）：`variables` 早就有这个能力
    （`convert_variables`，`make.py` 在生成前调用它），但 `parameters` **没有** ——
    模型里 `parameters: Union[VariablesMapping, Text]` **允许**写字符串，
    生成器却把它当字面量渲染：

    ```python
    @pytest.mark.parametrize("param", Parameters("${get_params()}"))   ← 一个字符串！
    ```

    `hmake` **exit 0**（生成物语法合法），而 `parse_parameters` 对着这个字符串调
    `.items()` → 运行期（准确说是**收集期**）

    ```text
    AttributeError: 'str' object has no attribute 'items'
    collected 0 items / 1 error
    ```

    对照组：`variables: ${get_variables()}` 会被正确求值成 `.variables(**{...})`。
    同一份配置里两个兄弟字段口径不一致，属"模型允许、生成器不认"的老毛病。

    处置：与 `variables` **完全同款** —— 字符串就解析成真实对象再渲染；
    dict/list 原样返回（既有路径一字不变）。
    """
    if isinstance(raw_parameters, Text):
        project_meta = load_project_meta(test_path)
        return parse_data(raw_parameters, {}, project_meta.functions)

    return raw_parameters


def _content_type_of(request: Dict) -> Text:
    """取 `request.headers` 里的 `Content-Type`（**大小写不敏感**）。

    NOTICE（0919-2 / 缺陷 4）：HTTP 头名**大小写不敏感**，但修复前这里是按精确键
    `request["headers"]["Content-Type"]` 查的。v2/v3 老用例——尤其 HTTP/2 录制产物——
    普遍写小写 `content-type: application/json`：

    ```text
    headers: {content-type: application/json}   # 取不到 → content_type 为空
    body: {a: 1}                                 # → dict body 落进 `data`
    ```
    于是请求体以 **form-urlencoded** 发出：请求形态被静默改错，而用例照样能过
    （服务端往往只校验状态码）。这正是本仓最忌讳的「静默假通过」。

    判据用仓库已有的 `lower_dict_keys`（不另写一套大小写折叠），
    与 `utils.is_sensitive_key` 那类「一个口径走全仓」的做法一致。

    边界（刻意保留，不做新发明）：`headers` 不是 dict 时返回空串
    （例如 `headers: "abc"` 这种畸形写法）——修复前 `"Content-Type" in "abc"`
    本来就是 False，行为逐字一致，不在本批扩大打击面。
    """
    headers = request.get("headers")
    if not isinstance(headers, dict):
        return ""

    lowered = lower_dict_keys(headers)

    # NOTICE: 必须用「键是否存在」而不是 `.get(...) is None` 判断——后者会把
    # `Content-Type:`（YAML 里留空 → None）这种**写了但值非法**的情况
    # 静默当成「没写这个头」，正是本批要消灭的那类静默降级。
    if "content-type" not in lowered:
        return ""

    content_type = lowered["content-type"]

    if not isinstance(content_type, Text):
        # 这个值决定 body（dict）按 JSON 还是表单发出，**猜不了**——所以响亮报错。
        # 修复前这里是 `content_type.startswith(...)` 直接 AttributeError（裸堆栈、
        # 没有任何可操作信息），属「失败但说不清」，这里换成可操作的格式错误。
        raise exceptions.TestCaseFormatError(
            f"request.headers 的 Content-Type 必须是字符串，实际是 "
            f"{type(content_type).__name__}: {content_type!r}。\n"
            f"  该值决定 body（dict）按 JSON 还是表单发出，无法猜测；"
            f'请改成字符串（例如 "application/json"）或删掉这个头。'
        )

    return content_type


def _convert_request(request: Dict) -> Dict:
    if "body" in request:
        content_type = _content_type_of(request)
        if content_type.startswith("application/json"):
            request["json"] = request.pop("body")
        else:
            request["data"] = request.pop("body")
    return _sort_request_by_custom_order(request)


def _needs_jmespath_quotes(segment: Text) -> bool:
    """这一段路径名是否需要加引号（JMESPath 的裸标识符不允许 `-` 等字符）。

    NOTICE（0918-8 / M32）：判据刻意收得很紧——只处理「整段就是一个裸名字，且含 `-`」
    这一种形态：
      - 含 `[` / `]` / `(` / `)` / `'` / `"` / `?` / `@` / `*` / `:` / 空格的段一律不动
        （那是下标、过滤、函数调用，加引号会改变语义甚至让表达式非法）；
      - 合法裸标识符（`^[A-Za-z_][A-Za-z0-9_]*$`）不动 → 既有生成物与既有表达式**零变化**；
      - 只对 `^[A-Za-z_][A-Za-z0-9_-]*$` 且真的含 `-` 的段加引号。
    """
    if not segment or "-" not in segment:
        return False
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", segment))


def _convert_jmespath(raw: Text) -> Text:
    if not isinstance(raw, Text):
        raise exceptions.TestCaseFormatError(f"Invalid jmespath extractor: {raw}")

    # content.xx/json.xx => body.xx
    # 0918-7 / L2：必须是**完整前缀**（`json` 本身，或其后紧跟 `.` / `[`）。
    # 修复前用的是裸 `startswith("json")`，`json_str.id` 会被错改成 `body_str.id`、
    # `content_type.x` 会被错改成 `body_type.x`——v2/v3 兼容路径下静默产生错误表达式。
    # `[` 也要认：`content[0].name` 是合法的取下标写法（回归见 compat_test）。
    if raw == "content" or raw.startswith(("content.", "content[")):
        raw = f"body{raw[len('content'):]}"
    elif raw == "json" or raw.startswith(("json.", "json[")):
        raw = f"body{raw[len('json'):]}"

    raw_list = raw.split(".")
    for i, item in enumerate(raw_list):
        item = item.strip('"')
        # NOTICE（0918-8 / M32）：**任何含特殊字符的裸名字都要加引号**，不能只认两个硬编码名字。
        # 修复前判据是 `item.lower().startswith("content-") or item.lower() == "user-agent"`，
        # 于是其余带 `-` 的名字（`X-Trace` / `X-Request-Id` / `X-Api-Key` …真实接口里遍地都是）
        # 原样留在表达式里 —— 而 `-` 在 JMESPath 里是运算符，运行期直接抛
        # `LexerError: Bad jmespath expression: Unknown token '-'`（且是 **error**，不是可读的断言失败）。
        # 新判据：只对「本身就是裸名字、但含 `-`」的段加引号——
        #   - 下标/过滤/切片段（`[0]`、`[?id=='1']`）含 `[`，不会被误加引号；
        #   - 已经是合法标识符的段（`body`/`status_code`）不加，输出与修复前**逐字一致**；
        #   - `content-*` 与 `user-agent` 走的是同一条新规则，结果不变（见回归用例）。
        if _needs_jmespath_quotes(item):
            # add quotes for some field in white list
            # e.g. headers.Content-Type => headers."Content-Type"
            raw_list[i] = f'"{item}"'

    return ".".join(raw_list)


def _convert_extractors(extractors: Union[List, Dict]) -> Dict:
    """convert extract list(v2) to dict(v3)

    Args:
        extractors: [{"varA": "content.varA"}, {"varB": "json.varB"}]

    Returns:
        {"varA": "body.varA", "varB": "body.varB"}

    """
    v3_extractors: Dict = {}

    if isinstance(extractors, List):
        # [{"varA": "content.varA"}, {"varB": "json.varB"}]
        for extractor in extractors:
            if not isinstance(extractor, Dict):
                # NOTICE（0918-4 / L8）：这里原来是 `logger.error + sys.exit(1)`。
                # `sys.exit` 抛的是 `SystemExit`（BaseException 的子类，**不是** Exception），
                # 批量 hmake 里连 `except Exception` 都拦不住 → 一个坏文件杀掉整批。
                raise exceptions.TestCaseFormatError(f"Invalid extractor: {extractors}")
            for k, v in extractor.items():
                v3_extractors[k] = v
    elif isinstance(extractors, Dict):
        # {"varA": "body.varA", "varB": "body.varB"}
        v3_extractors = extractors
    else:
        raise exceptions.TestCaseFormatError(f"Invalid extractor: {extractors}")

    for k, v in v3_extractors.items():
        v3_extractors[k] = _convert_jmespath(v)

    return v3_extractors


def _convert_validators(validators: List) -> List:
    """把 v2/v3 形态的 `validate` 归一成「检查项已转换」的形态。

    NOTICE（批次 E / **L9**）：本函数**只做转换，不做校验** —— 形态不对的 validator 必须
    **原样放过**，交给 `make.ensure_known_comparators` 去报（那里能给出**用例/步骤/原文**的
    定位）。修复前这里会先崩，于是「承诺的带定位报错」根本不可达：

    | `validate` 里的形态 | 修复前 | 现在 |
    |---|---|---|
    | `{"eq": "notalist"}` | `TypeError: 'str' object does not support item assignment` | 放过 → 生成期点名报错 |
    | `{"eq": []}` | `IndexError: list index out of range` | 放过 → 生成期点名报错 |
    | `["e"]`（字符串条目） | `AttributeError: 'str' object has no attribute 'keys'` | 放过 → 生成期点名报错 |
    """
    for v in validators:
        if not isinstance(v, dict):
            # 形态问题不在这里报（见上面的 NOTICE）
            continue

        if "check" in v and "expect" in v:
            # format1: {"check": "content.abc", "assert": "eq", "expect": 201}
            v["check"] = _convert_jmespath(v["check"])

        elif len(v) == 1:
            # format2: {'eq': ['status_code', 201]}
            comparator = list(v.keys())[0]
            expression = v[comparator]
            if isinstance(expression, list) and expression:
                expression[0] = _convert_jmespath(expression[0])

    return validators


def _sort_request_by_custom_order(request: Dict) -> Dict:
    custom_order = [
        "method",
        "url",
        "params",
        "headers",
        "cookies",
        "data",
        "json",
        "files",
        "timeout",
        "allow_redirects",
        "proxies",
        "verify",
        "stream",
        "auth",
        "cert",
    ]
    return sort_dict_by_custom_order(request, custom_order)


def _sort_step_by_custom_order(step: Dict) -> Dict:
    custom_order = [
        "name",
        "variables",
        "request",
        "testcase",
        # M11（0918-1）：retry 紧跟在 step 声明之后，与手写用例
        # `RunRequest(name).with_retry(...).get(url)` 的顺序一致
        "retry_times",
        "retry_interval",
        "setup_hooks",
        "teardown_hooks",
        "extract",
        "validate",
        "validate_script",
    ]
    return sort_dict_by_custom_order(step, custom_order)


# teststep 里会被识别的字段：**以模型为准**（`models.KNOWN_STEP_FIELDS` = `TStep.model_fields`
# ∪ YAML 惯用别名），本模块只**追加**自己独有的 v2/v3 历史字段名。
#
# NOTICE（0918-1 修复）：这里原先是**一份手写清单**，与 `models.TStep` 漂移了 5 个字段
# （`validators`/`retry_times`/`retry_interval`/`sql_request`/`thrift_request`）。
# 后果不是「少告警」，而是**静默丢弃**：`_ensure_step_attachment` 是按本白名单**重建**
# teststep 的，没进白名单的字段在这里就被删掉了，用户拿到的只是一句
# 「存在无法识别的字段：['retry_interval', 'retry_times']」——而 `retry_times` 恰恰是
# `docs/能力清单.md` 标注为 ✅ 的能力（模型有字段、`runner.__run_step` 真消费、
# `RunRequest.with_retry` 也存在，唯独生成器拿不到它，因为字段在这之前就没了）。
#
# 所以这份清单必须**从模型派生**：模型认什么，转换层就必须原样保留什么。
# 至于「保留下来但生成器表达不出来」的字段，由 `make.ensure_generatable_teststeps`
# 统一报错（宁可报错，也不能静默丢弃）。
#
# NOTICE（批次 9-1 / 漂移收口）：**这里不能再重复写一遍 `set(TStep.model_fields) | {...}`**。
# 修复前 `models.KNOWN_STEP_FIELDS`（loader 的未知字段告警用）与这个常量各写了一份
# 「模型字段 ∪ 别名」，两份只差一个字面量（`api`）—— 于是「往模型加字段」需要同时改两处，
# 漏掉其中任何一处都会回到上面那种静默丢弃。现在只有一条派生链：
#
#     TStep.model_fields ──► models.KNOWN_STEP_FIELDS ──► compat.STEP_KNOWN_FIELDS（+api）
#
# 回归护栏见 `tests/compat_test.py::TestStepFieldWhitelistSingleSource`
# （断言两者的差集**恰好**是 `{"api"}`；模型加了字段却没进 models 那份时立刻变红）。
STEP_KNOWN_FIELDS = set(KNOWN_STEP_FIELDS) | {
    "api",  # 只属于 v2/v3：由 `ensure_testcase_v4` 先把它转成 `testcase` 引用
}

# `_ensure_step_attachment` 重建 teststep 时会**原样透传**的 retry 配置字段。
# 生成器据此 emit `.with_retry(...)`，见 `make.make_teststep_chain_style`。
STEP_RETRY_FIELDS = ("retry_times", "retry_interval")


def _warn_unknown_step_fields(step: Dict) -> None:
    """对无法识别的 teststep 字段给出告警（它们会在格式转换时被丢弃）。

    NOTICE: `_ensure_step_attachment` 是按白名单重建 teststep 的，白名单外的字段
    到这里就被丢掉了——用户把 `validate` 写成 `validates` 之类只会得到「断言没生效」
    这种极难排查的现象。这里在丢弃前给出明确告警（只告警不报错，避免破坏既有用例）。
    """
    unknown_step_fields = sorted(set(step) - STEP_KNOWN_FIELDS)
    if unknown_step_fields:
        logger.warning(
            f"teststep {step.get('name')!r} 中存在无法识别的字段："
            f"{unknown_step_fields}，它们会被忽略（不会写进生成的用例）。\n"
            f"已支持的 teststep 字段：{sorted(STEP_KNOWN_FIELDS)}"
        )


def _ensure_step_attachment(step: Dict) -> Dict:
    _warn_unknown_step_fields(step)

    # NOTICE（批次 E / **L10**）：`name` 缺失时修复前是裸 `KeyError: 'name'`
    # —— 报错里既没有用例、也没有「这个 step 里到底有什么字段」，用户无从定位。
    # 现在给出点名报错（含该 step 现有的字段列表）。
    if "name" not in step:
        raise exceptions.TestCaseFormatError(
            "teststep 缺少 `name` 字段（每个 step 都必须写 `name`）。\n"
            f"  该 step 里现有的字段：{sorted(step)}\n"
            "  修法：在 YAML 的该 step 下补一行 `name: 随便一个可读的名字`。"
        )

    test_dict = {
        "name": step["name"],
    }

    if "request" in step:
        test_dict["request"] = _convert_request(step["request"])

    if "variables" in step:
        test_dict["variables"] = step["variables"]

    if "setup_hooks" in step:
        test_dict["setup_hooks"] = step["setup_hooks"]

    if "teardown_hooks" in step:
        test_dict["teardown_hooks"] = step["teardown_hooks"]

    if "extract" in step:
        test_dict["extract"] = _convert_extractors(step["extract"])

    if "export" in step:
        test_dict["export"] = step["export"]

    # M11（0918-1）：retry 配置必须原样传到生成器。
    # 修复前这两个字段不在白名单里，会在此处被丢弃，于是 `retry_times` 从 YAML 到生成物
    # 全程消失（详见 STEP_KNOWN_FIELDS 处的说明）。
    for retry_field in STEP_RETRY_FIELDS:
        if retry_field in step:
            test_dict[retry_field] = step[retry_field]

    # `validators` 是模型字段名，`validate` 是 YAML 里的惯用别名；两者都接受并归一到 `validate`
    # （生成器 `make_teststep_chain_style` 只认 `validate`）。
    # NOTICE: 修复前只认 `validate`，写 `validators` 会被本函数丢弃——而 `validators`
    # 是 `TStep` 的**正式字段名**，pydantic 会接受它，属于「模型认、转换层丢」的静默失败。
    raw_validators = step.get("validate", step.get("validators"))
    if raw_validators is not None:
        if not isinstance(raw_validators, List):
            raise exceptions.TestCaseFormatError(
                f"Invalid teststep validate: {raw_validators}"
            )
        test_dict["validate"] = _convert_validators(raw_validators)

    if "validate_script" in step:
        test_dict["validate_script"] = step["validate_script"]

    return test_dict


def ensure_testcase_v4_api(api_content: Dict) -> Dict:
    logger.info("convert api in v2/v3 to testcase format v4")

    teststep = {
        "request": _convert_request(api_content["request"]),
    }
    teststep.update(_ensure_step_attachment(api_content))

    teststep = _sort_step_by_custom_order(teststep)

    config = {"name": api_content["name"]}
    extract_variable_names: List = list(teststep.get("extract", {}).keys())
    if extract_variable_names:
        config["export"] = extract_variable_names

    return {
        "config": config,
        "teststeps": [teststep],
    }


def ensure_testcase_v4(test_content: Dict) -> Dict:
    logger.info("ensure compatibility with testcase format v2/v3")

    v3_content = {"config": test_content["config"], "teststeps": []}

    if "teststeps" not in test_content:
        # NOTICE（0918-4 / L8）：`sys.exit(1)` → 抛异常，理由见 _convert_extractors。
        raise exceptions.TestCaseFormatError(f"Miss teststeps: {test_content}")

    if not isinstance(test_content["teststeps"], list):
        raise exceptions.TestCaseFormatError(
            f'teststeps should be list type, got {type(test_content["teststeps"])}: '
            f'{test_content["teststeps"]}'
        )

    for step in test_content["teststeps"]:
        teststep = {}

        if "request" in step:
            pass
        elif "api" in step:
            teststep["testcase"] = step.pop("api")
        elif "testcase" in step:
            teststep["testcase"] = step.pop("testcase")
        else:
            raise exceptions.TestCaseFormatError(f"Invalid teststep: {step}")

        teststep.update(_ensure_step_attachment(step))

        teststep = _sort_step_by_custom_order(teststep)
        v3_content["teststeps"].append(teststep)

    return v3_content


def ensure_cli_args(args: List) -> List:
    """ensure compatibility with deprecated cli args in v2"""
    # NOTICE（0920 批次 5 / **N32**）：`--failfast` 的处理口径订正。
    #
    # 实测（本机 pytest）：**pytest 自己并没有 `--failfast` 这个参数** ——
    # 直接传给它只会得到
    #     python -m pytest: error: unrecognized arguments: --failfast
    # 真正等价的是 `-x` / `--exitfirst`。所以"把它摘掉"这个动作本身是对的
    # （不摘的话用户会撞上一个启动期 usage error），
    # **错的是原来的告知方式**：
    #   ① 说它是 "deprecated argument"，暗示"以前支持、现在废弃"——
    #      而它从来不是本框架的参数，用户会以为是自己记错了；
    #   ② **完全没提替代写法** `-x`，于是"首败即停"这个诉求被静默降级成"全量跑"
    #      （CI 里后续用例的副作用照样执行、失败计数与耗时全部走偏）。
    #
    # 现在的口径：**翻译**成 pytest 认的 `-x`（保住用户要的"首败即停"），
    # 而不是丢掉这个诉求。若参数里已经有 `-x`/`--exitfirst` 就只移除、不重复添加。
    if "--failfast" in args:
        already_exits_first = "-x" in args or "--exitfirst" in args
        args.pop(args.index("--failfast"))
        if not already_exits_first:
            args.append("-x")
        logger.warning(
            "`--failfast` 不是 pytest 的参数（pytest 只认 `-x` / `--exitfirst`），"
            f"已自动改用 `-x`{'（参数里本来就有，未重复添加）' if already_exits_first else ''}"
            " —— 即「首个失败就停」照旧生效。\n"
            "  NOTICE: 修复前这条只说「remove deprecated argument: --failfast」，"
            "**既没解释也没补上替代**，等于把你要的失败模式**静默**降级成「整份跑完」"
            "（CI 里后续用例的副作用照样执行）。"
        )

    # convert --report-file to --html
    if "--report-file" in args:
        logger.warning("replace deprecated argument --report-file with --html")
        index = args.index("--report-file")
        args[index] = "--html"
        args.append("--self-contained-html")

    # keep compatibility with --save-tests in v2
    if "--save-tests" in args:
        logger.warning(
            "generate conftest.py keep compatibility with --save-tests in v2"
        )
        args.pop(args.index("--save-tests"))
        _generate_conftest_for_summary(args)

    return args


# `--save-tests` 生成的 conftest.py 的**首行**标记。
# 用途：判断「这个 conftest.py 是不是我们生成的」——只有是我们生成的文件才允许被刷新，
# 用户自己的 conftest.py **绝不允许**覆盖（见 _generate_conftest_for_summary）。
CONFTEST_MARKER = "# NOTICE: Generated By InterfaceTester."

# 拒绝覆盖用户 conftest.py 时，把「本该写进去的内容」落到这个后缀的旁边文件里，
# 让用户能直接比对/合并，而不是拿到一句"拒绝了"就没有下文。
CONFTEST_BLOCKED_SUFFIX = ".interfacetester-new"


def is_generated_conftest(conftest_path: Text) -> bool:
    """判断已存在的 conftest.py 是否由 InterfaceTester 生成。

    判定口径是**首行严格等于** `CONFTEST_MARKER`。为什么不做子串匹配：
    用户完全可能在文档字符串里提到这句话（例如"本文件曾被 Generated By InterfaceTester
    覆盖过"），子串匹配会把它误判成"我们的文件"然后放心覆盖 —— 那正好是要防的事。
    """
    try:
        with open(conftest_path, mode="r", encoding="utf-8") as f:
            first_line = f.readline().strip()
    except (OSError, UnicodeDecodeError):
        # 读不了就按"不是我们的"处理（保守方向：宁可拒绝，也不覆盖别人的文件）
        return False

    return first_line == CONFTEST_MARKER


""" 会**吃掉一个取值**的 pytest 选项（批次 7 / M2 建立，批次 B / M3 收口成唯一一份）。

    `cli.main_run` 与 `compat._generate_conftest_for_summary` 都用它来判"哪个 token 是用例路径"。
    修复前两份判据各自维护：`cli.main_run` 已经按这份清单过滤（M2 的修复），
    而 conftest 生成器还在用 `os.path.exists(arg)` 猜 —— 于是**选项的值**会被当成用例路径
    （见 `_generate_conftest_for_summary` 的 M3 NOTICE）。
    新增插件选项时在这里追加一处即可。
"""
PYTEST_OPTIONS_WITH_VALUE: Set[Text] = {
    # fmt: off
    "-c", "--config-file", "--rootdir", "-k", "-m", "-n", "-o", "--override-ini",
    "--junitxml", "--html", "--log-file", "--log-level", "--log-format",
    "--basetemp", "--confcutdir", "--deselect", "--ignore", "--ignore-glob",
    "--import-mode", "--capture", "-W", "--pythonwarnings", "-p", "--plugin",
    "--tb", "--maxfail", "--dist", "--timeout", "--reruns", "--cov-report",
    "--html-report", "--alluredir", "--report-log", "--durations",
    # fmt: on
}


def filter_test_paths(args: List) -> Tuple[List, List]:
    """把命令行参数切成「用例路径」与「其余参数」两份。

    NOTICE（为什么判据是"上一个 token 是不是取值的选项"而不是"路径存不存在"）：
    `run good.yml -c pytest.ini` / `--rootdir .` / `--junitxml reports/x.xml` 里
    **选项的值**也是（或可以是）存在的路径。修复前 `cli.main_run` 按"存在即路径"判定，
    轻则让选项失值（pytest `error: argument -c/--config-file: expected one argument`
    → **exit 4、一个用例都不跑**），重则把那个目录下所有 YAML 都生成并执行。

    NOTICE（`--opt=value` 形式天然安全）：它是单 token、以 `-` 开头，会直接进 `remaining`。
    """
    value_follows = False
    test_paths: List[Text] = []
    remaining: List[Text] = []

    for item in args:
        if value_follows:
            # 这是上一个选项的**值**：无论它是不是存在的路径，都不能当成用例路径
            remaining.append(item)
            value_follows = False
        elif item.startswith("-") and item in PYTEST_OPTIONS_WITH_VALUE:
            remaining.append(item)
            value_follows = True
        elif not os.path.exists(item):
            # item is not file/folder path
            remaining.append(item)
        else:
            # item is file/folder path
            test_paths.append(item)

    return test_paths, remaining


def _resolve_conftest_root_dir(
    test_path: Text, fallback_root_dir: Text
) -> Tuple[Text, Text]:
    """决定 `--save-tests` 的 conftest.py 写到哪里：返回 `(目录, 告警文本或空串)`。

    NOTICE（批次 B / **M4**）：这是「不许写到祖先目录」的落点。
    `load_project_meta(test_path)` 在找不到项目标记时会给一个**兜底** RootDir
    （当前已加载的 meta，或 `os.getcwd()`），那可能是一个与用例毫不相干的**祖先目录**
    —— 实测事故是把 conftest.py 写到了**仓库根**，而那份 conftest 的 `summary_path`
    是硬编码的，于是**另一个进程**的 pytest 会话加载它之后把 summary 写进了别人的路径。

    口径：
      - 向上找得到 `debugtalk.py` → 用那个项目根（**既有行为，一个字不变**）；
      - 找不到（无标记项目）→ 用**用例自己所在目录**（文件 → 它的目录；目录 → 它自己），
        并给一条告警说明「为什么不是 cwd」。
    """
    try:
        debugtalk_path, project_root_directory = loader.locate_project_root_directory(
            test_path
        )
    except exceptions.MyBaseError:
        # 路径本身取不到（理论上不会走到：调用方刚确认过存在）→ 退回既有行为
        return fallback_root_dir, ""

    if debugtalk_path:
        return fallback_root_dir, ""

    own_dir = test_path if os.path.isdir(test_path) else os.path.dirname(test_path)
    own_dir = os.path.abspath(own_dir)

    if os.path.normcase(own_dir) == os.path.normcase(
        os.path.abspath(fallback_root_dir)
    ):
        return fallback_root_dir, ""

    return own_dir, (
        f"--save-tests: 用例路径向上**没有找到项目标记**（`debugtalk.py`），"
        f"因此不在「当前项目根」写 conftest.py（那可能是与用例无关的目录），"
        f"改为写到用例自己所在的目录。\n"
        f"  用例路径: {test_path}\n"
        f"  conftest.py 落点: {own_dir}\n"
        f"  summary.json 也会落在该目录的 logs/ 下。\n"
        f"  NOTICE: 修复前这里会用 `load_project_meta()` 的兜底 RootDir（可能是 cwd 或祖先目录），"
        f"实测把 conftest.py 写到过**仓库根**——而它的 summary 路径是硬编码的，"
        f"会连带**劫持同目录树下其它 pytest 会话**的汇总输出。\n"
        f"  想要确定的项目根，请在用例目录（或其上层）放一个 `debugtalk.py`。"
    )


def _generate_conftest_for_summary(args: List):
    """生成 `--save-tests` 用的 conftest.py（写到用例所属项目的根目录）。

    NOTICE（0918-3 / M6）：**绝不覆盖用户自己的 conftest.py**。
    修复前这里是无条件的 `open(path, "w")`，于是用户手写的 fixture（例如
    `examples/postman_echo/request_methods/conftest.py` 里的 `testcase_fixture`）
    会被整体摧毁、无备份、无提示。

    现在的三分支：
      ① 文件不存在 → 正常生成（原行为）；
      ② 首行是我们的 marker → 允许刷新（升级时才拿得到新版模板）；
      ③ 存在但不是我们的 → **拒绝**，把本该写入的内容落到
         `<conftest.py>.interfacetester-new` 供用户比对/合并，然后以退出码 1 明确失败。

    NOTICE（批次 B / **M3**）：用例路径改用与 `cli.main_run` **同一判据**
    （`filter_test_paths`）。修复前是"argv 里第一个存在的 token"，而**选项的值**
    本身也可以是存在的路径，于是被当成用例路径（实测三组，退出码全是 0）：
      - `hrun -c pytest.ini case.yml --save-tests` → summary 落到
        `logs/pytest.summary.json`（名字成了选项的值）；
      - `hrun --junitxml reports/out.xml … --save-tests` → `logs/reports/out.summary.json`；
      - `hrun -c sub/pytest.ini case.yml --save-tests` → 在**用户没指定的嵌套项目** `sub/`
        里写 conftest.py，并且**本次运行没有产出任何 summary.json**。
    """

    test_paths, _remaining = filter_test_paths(args)
    if not test_paths:
        logger.error(
            f"--save-tests 需要至少一个用例路径（文件或目录），但从参数里一个都没解析出来。\n"
            f"  args: {args}\n"
            f"  NOTICE: 判据是「上一个 token 是不是会吃掉一个取值的 pytest 选项」"
            f"（见 compat.PYTEST_OPTIONS_WITH_VALUE）—— 选项的值不会被当成用例路径。"
        )
        sys.exit(1)

    test_path = test_paths[0]
    conftest_content = '''# NOTICE: Generated By InterfaceTester.
import json
import os
import time

import pytest
from loguru import logger

from interfacetester.utils import get_platform, ExtendJSONEncoder


@pytest.fixture(scope="session", autouse=True)
def session_fixture(request):
    """setup and teardown each task"""
    logger.info("start running testcases ...")

    start_at = time.time()

    yield

    logger.info("task finished, generate task summary for --save-tests")

    summary = {
        "success": True,
        "stat": {
            "testcases": {"total": 0, "success": 0, "fail": 0},
            "teststeps": {"total": 0, "failures": 0, "successes": 0},
        },
        "time": {"start_at": start_at, "duration": time.time() - start_at},
        "platform": get_platform(),
        "details": [],
    }

    for item in request.node.items:
        # 0918-3（H4 家族）：不能因为**一个** item 取不到汇总就丢掉整份 summary。
        # 两个真实场景：
        #   ① 项目根同时存在「非本框架」的 pytest/unittest 用例（很常见：框架用例 + 工具脚本
        #      放同一个仓库）——它们的 `item.instance` 没有 `get_summary`，
        #      修复前会直接 AttributeError，让 session fixture 报错、summary 全丢；
        #   ② 某个用例的汇总本身出问题（历史上有 H4 的 ParamsError）。
        # 现在的口径：非框架用例跳过（debug），取汇总失败记 error 后跳过，其余继续。
        get_summary = getattr(getattr(item, "instance", None), "get_summary", None)
        if not callable(get_summary):
            logger.debug(f"--save-tests: 跳过非 InterfaceTester 用例 {item.nodeid}")
            continue

        try:
            testcase_summary = get_summary()
        except Exception as ex:
            # NOTICE（两层转义，实测踩过两次）：
            # 1) 换行必须写成**双反斜杠 + n**：本模板是 compat.py 里的普通三引号字符串，
            #    写单个反斜杠 n 会在 compat.py 被解析时变成**真实换行**；
            #    若它出现在 f-string 里 → 跨行 → SyntaxError: unterminated f-string literal；
            #    若它出现在**注释**里 → 注释被从中间截断，后半段变成裸代码 → 同样 SyntaxError。
            # 2) 花括号必须写**单层**：本模板只用 `.replace()` 做占位符替换（没走 format），
            #    写成双层会原样留在生成文件里、不被插值。
            logger.error(
                f"--save-tests: 收集用例汇总失败，已跳过该用例（其余用例与汇总文件不受影响）\\n"
                f"  nodeid: {item.nodeid}\\n"
                f"  {type(ex).__name__}: {ex}"
            )
            continue

        summary["success"] &= testcase_summary.success

        # NOTICE（批次 A / M8）：统计必须按**记录里实际的成功/失败条数**算，
        # 不能再用 `len(step_results) - 1` 去猜。
        #
        # 修复前失败分支写的是「成功数 += 条数 - 1、失败数 += 1」，即**硬编码了**
        # 「一个失败用例 = 恰好一条失败 step，其余全成功」这个假设。而 H1 的修复
        # 刚刚引入了「`success == False` 且 `step_results == []`」这种**合法**组合
        # （用例在 step 之外失败：配置解析失败、`teststeps` 装配抛异常…），
        # 于是算出 `successes = 0 - 1 = -1`（实测 summary.json 里真的出现负数），
        # 同时 `failures: 1` 与 `records: []` 也自相矛盾。
        #
        # 现在逐条数：`total` = 记录的条数，`successes`/`failures` = 其中两者的条数。
        # 用例级失败但一条 step 都没跑时，`testcases.fail` 增 1、`teststeps` 三项都不动——
        # 这是**如实**的（确实没有任何 step 被执行过），比编一个失败数更可信。
        #
        # NOTICE: 本模板是 compat.py 里的普通三引号字符串，只用 `.replace()` 做占位符替换
        # （没走 format/f-string）——**不要**在这里写 f-string 或双层花括号，
        # 换行也要写成双反斜杠 + n，理由见上面「两层转义」那条 NOTICE。
        step_total = len(testcase_summary.step_results)
        step_successes = 0
        for step_result in testcase_summary.step_results:
            if step_result.success:
                step_successes += 1
        step_failures = step_total - step_successes

        summary["stat"]["testcases"]["total"] += 1
        summary["stat"]["teststeps"]["total"] += step_total
        summary["stat"]["teststeps"]["successes"] += step_successes
        summary["stat"]["teststeps"]["failures"] += step_failures
        if testcase_summary.success:
            summary["stat"]["testcases"]["success"] += 1
        else:
            summary["stat"]["testcases"]["fail"] += 1

        testcase_summary_json = testcase_summary.model_dump()
        testcase_summary_json["records"] = testcase_summary_json.pop("step_results")
        summary["details"].append(testcase_summary_json)

    summary_path = r"{{SUMMARY_PATH_PLACEHOLDER}}"
    summary_dir = os.path.dirname(summary_path)
    os.makedirs(summary_dir, exist_ok=True)

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=4, ensure_ascii=False, cls=ExtendJSONEncoder)

    logger.info(f"generated task summary: {summary_path}")

'''

    project_meta = load_project_meta(test_path)
    project_root_dir = project_meta.RootDir

    # NOTICE（批次 B / **M4**）：**不许写到祖先目录**。
    #
    # `load_project_meta(test_path)` 在「向上找不到任何项目标记（没有 debugtalk.py / .env）」时
    # 会沿用「当前已加载的 meta / cwd」——那可能是一个**与用例毫不相干**的祖先目录。
    # 实测事故（`.tmp_audit/`）：对 `.tmp_audit/kernel/p4`（无标记）跑 `--save-tests`，
    # conftest.py 落到了**仓库根** `本仓库根目录\conftest.py`，
    # 而那份 conftest 里的 `summary_path` 是**硬编码**的 —— 于是**另一个进程**的 pytest 会话
    # （从 rootdir 往下会加载它）把 summary 写进了 p4 的路径：
    # 读到的 `details[].name` 是别的项目的用例名。也就是说，
    # 「写到一个无关目录」还会**劫持同树下其它 pytest 会话**。
    #
    # 现在的口径：找不到项目标记时，**以用例自己所在的目录**为落点
    # （文件 → 它所在目录；目录 → 它自己），并给出明确告警说明为什么。
    # 这既保留了 `--save-tests` 在无标记目录下的可用性，又不会把文件写到祖先去。
    project_root_dir, root_dir_reason = _resolve_conftest_root_dir(
        test_path, project_root_dir
    )
    if root_dir_reason:
        logger.warning(root_dir_reason)

    conftest_path = os.path.join(project_root_dir, "conftest.py")

    test_path = os.path.abspath(test_path)
    logs_dir_path = os.path.join(project_root_dir, "logs")

    # NOTICE（批次 B / M4 连带）：相对路径必须按**同一个** `project_root_dir` 算。
    # 修复前这里调 `convert_relative_project_root_dir(test_path)`，而它会**重新定位**项目
    # （内部又是 `load_project_meta`）——无项目标记时重新定位拿到的是 cwd 兜底的那个根，
    # 与上面刚选定的落点**不是同一个根**，于是相对路径带着整条 `..\..` 前缀，
    # summary 会落到 `logs/` 下的一串怪目录里。
    # 现在直接用已选定的根做 `relpath`（用例一定在该根之下：它要么来自向上找到的项目根，
    # 要么就是用例自己所在目录）。
    test_path_relative_path = os.path.relpath(test_path, project_root_dir)

    if os.path.isdir(test_path):
        file_foder_path = os.path.join(logs_dir_path, test_path_relative_path)
        dump_file_name = "all.summary.json"
    elif len(test_paths) > 1:
        # 多文件：与目录一致落到 `logs/all.summary.json`（名字与"包含多个用例"相符）
        file_foder_path = logs_dir_path
        dump_file_name = "all.summary.json"
    else:
        file_relative_folder_path, test_file = os.path.split(test_path_relative_path)
        file_foder_path = os.path.join(logs_dir_path, file_relative_folder_path)
        test_file_name, _ = os.path.splitext(test_file)
        dump_file_name = f"{test_file_name}.summary.json"

    summary_path = os.path.join(file_foder_path, dump_file_name)
    conftest_content = conftest_content.replace(
        "{{SUMMARY_PATH_PLACEHOLDER}}", summary_path
    )

    dir_path = os.path.dirname(conftest_path)
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    # M6（0918-3）：只有「不存在」或「是我们生成的」才允许写。
    if os.path.exists(conftest_path) and not is_generated_conftest(conftest_path):
        side_path = conftest_path + CONFTEST_BLOCKED_SUFFIX
        try:
            with open(side_path, "w", encoding="utf-8") as f:
                f.write(conftest_content)
        except OSError as ex:
            logger.warning(f"failed to write side conftest file {side_path}: {ex}")

        logger.error(
            f"--save-tests 需要往项目根写 conftest.py，但该文件已存在且**不是本框架生成的**，"
            f"为避免覆盖你自己的 fixture，已中止（你的文件**没有被改动**）。\n"
            f"  已存在的文件: {conftest_path}\n"
            f"  本该写入的内容: {side_path}\n"
            f"  NOTICE: 修复前这里会**直接覆盖**，用户手写的 fixture 会永久丢失。\n"
            f"  三种处理方式（任选其一）：\n"
            f"    1) 把 {os.path.basename(side_path)} 里的 `session_fixture` 合并进你的 conftest.py"
            f"（合并后本框架会把它当作你的文件，不再改写）；\n"
            f"    2) 去掉 `--save-tests`（不需要 summary.json 时这是最省事的做法）；\n"
            f"    3) 把 conftest.py 移到子目录，让项目根不再有同名文件。"
        )
        sys.exit(1)

    with open(conftest_path, "w", encoding="utf-8") as f:
        f.write(conftest_content)

    logger.info("generated conftest.py to generate summary.json")


def ensure_path_sep(path: Text) -> Text:
    """ensure compatibility with different path separators of Linux and Windows"""
    if "/" in path:
        path = os.sep.join(path.split("/"))

    if "\\" in path:
        path = os.sep.join(path.split("\\"))

    return path
