import json
import time
from typing import Any, Dict, List, Text, Union
from urllib.parse import quote

import requests
from loguru import logger

from interfacetester import utils
from interfacetester.exceptions import ValidationFailure
from interfacetester.ext.uploader import prepare_upload_step
from interfacetester.models import (
    Hooks,
    IStep,
    MethodEnum,
    StepResult,
    TRequest,
    TStep,
    VariablesMapping,
)
from interfacetester.parser import build_url, parse_variables_mapping
from interfacetester.response import ResponseObject
from interfacetester.runner import ALLURE, InterfaceTester


def call_hooks(
    runner: InterfaceTester, hooks: Hooks, step_variables: VariablesMapping, hook_msg: Text
):
    """call hook actions.

    Args:
        hooks (list): each hook in hooks list maybe in two format.

            format1 (str): only call hook functions.
                ${func()}
            format2 (dict): assignment, the value returned by hook function will be assigned to variable.
                {"var": "${func()}"}

        step_variables: current step variables to call hook, include two special variables

            request: parsed request dict
            response: ResponseObject for current response

        hook_msg: setup/teardown request/testcase

    """
    logger.info(f"call hook actions: {hook_msg}")

    if not isinstance(hooks, List):
        logger.error(f"Invalid hooks format: {hooks}")
        return

    for hook in hooks:
        if isinstance(hook, Text):
            # format 1: ["${func()}"]
            logger.debug(f"call hook function: {hook}")
            runner.parser.parse_data(hook, step_variables)
        elif isinstance(hook, Dict) and len(hook) == 1:
            # format 2: {"var": "${func()}"}
            var_name, hook_content = list(hook.items())[0]
            hook_content_eval = runner.parser.parse_data(hook_content, step_variables)
            logger.debug(
                f"call hook function: {hook_content}, got value: {hook_content_eval}"
            )
            logger.debug(f"assign variable: {var_name} = {hook_content_eval}")
            step_variables[var_name] = hook_content_eval
        else:
            logger.error(f"Invalid hook format: {hook}")


def _has_header(headers: Dict, name: Text) -> bool:
    """headers 里是否有名为 `name` 的头（**大小写不敏感**，RFC 7230 §3.2）。

    NOTICE（0920 / 缺陷 2）：框架要给「用户没写过的头」补默认值，而用户完全可以写
    `authorization` / `AUTHORIZATION` —— 用 `in` 判断会让框架的默认值把用户的显式值
    顶掉（requests 的 CaseInsensitiveDict 合并时后写者优先），且零告警。
    """
    lowered = name.lower()
    return any(
        isinstance(key, Text) and key.lower() == lowered for key in headers
    )


def _setdefault_case_insensitive(headers: Dict, name: Text, value: Any) -> Dict:
    """大小写不敏感的 `setdefault`：用户已写过同名头（任意大小写）→ 不改动、不加键。"""
    if _has_header(headers, name):
        return headers
    headers[name] = value
    return headers


def pretty_format(v) -> str:
    if isinstance(v, dict):
        return json.dumps(v, indent=4, ensure_ascii=False)

    if isinstance(v, requests.structures.CaseInsensitiveDict):
        return json.dumps(dict(v.items()), indent=4, ensure_ascii=False)

    return repr(utils.omit_long_data(v))


def run_step_request(runner: InterfaceTester, step: TStep) -> StepResult:
    """run teststep: request"""
    step_result = StepResult(
        name=step.name,
        step_type="request",
        success=False,
    )
    start_time = time.time()

    # parse
    functions = runner.parser.functions_mapping
    step_variables = runner.merge_step_variables(step.variables)
    prepare_upload_step(step, step_variables, functions)
    # parse variables
    step_variables = parse_variables_mapping(step_variables, functions)

    request_dict = step.request.model_dump()
    request_dict.pop("upload", None)
    parsed_request_dict = runner.parser.parse_data(request_dict, step_variables)

    request_headers = parsed_request_dict.pop("headers", {})
    # 处理 X-Path 便捷字段：变量已在 parse_data 中解析，这里做 URL 编码
    x_path = parsed_request_dict.pop("x_path", None)
    if x_path:
        request_headers["X-Path"] = quote(x_path, safe="/-_.~")
    # omit pseudo header names for HTTP/1, e.g. :authority, :method, :path, :scheme
    request_headers = {
        key: request_headers[key] for key in request_headers if not key.startswith(":")
    }
    # NOTICE（0920 / 缺陷 2）：HTTP 头名**大小写不敏感**，所以「用户有没有写过这个头」
    # 必须按小写比较。修复前这里是 `setdefault("interfacetester-Request-ID", ...)`：
    # 用户写 `interfacetester-request-id` 时 setdefault 认为"没有"，于是同时存在两个
    # 键；requests 的 CaseInsensitiveDict 把它们合并成**后写者优先**，用户值被吃掉。
    request_headers = _setdefault_case_insensitive(
        request_headers,
        "interfacetester-Request-ID",
        f"interfacetester-{runner.case_id}-{str(int(time.time() * 1000))[-6:]}",
    )
    parsed_request_dict["headers"] = request_headers

    # 自动注入 OAuth2 Bearer token（配置了 config.oauth2 且未手动指定 Authorization 时）
    # NOTICE（0920 / 缺陷 2，同族）：`"Authorization" not in request_headers` 是**大小写
    # 敏感**的。用户（或导入器）写小写 `authorization` 时，框架会再补一个大写
    # `Authorization`；两个键交给 requests 后按 CaseInsensitiveDict 合并，**框架的
    # token 覆盖用户显式写的那个**——发出的凭据与用户在 YAML 里写的不同，且零告警。
    oauth2_token = runner.get_oauth2_token()
    if oauth2_token and not _has_header(request_headers, "Authorization"):
        request_headers["Authorization"] = f"Bearer {oauth2_token}"

    step_variables["request"] = parsed_request_dict

    # setup hooks
    if step.setup_hooks:
        call_hooks(runner, step.setup_hooks, step_variables, "setup request")

    # prepare arguments
    config = runner.get_config()
    method = parsed_request_dict.pop("method")
    url_path = parsed_request_dict.pop("url")
    url = build_url(config.base_url, url_path)
    # step 级 verify 优先；仅在未显式设置（None）时回落到 config.verify，
    # 这样 RequestWithOptionalArgs.set_verify() / YAML request.verify 才会真正生效。
    if parsed_request_dict.get("verify") is None:
        parsed_request_dict["verify"] = config.verify

    # step 级 timeout 优先 → config.timeout → HttpSession 默认值（120s / 环境变量）。
    # NOTICE:
    # - 一律用 `is None` 判断：`timeout: 0`（立即超时）是合法值，用 `or` 会被吞掉；
    # - config 也没有时必须把该键移除，**不能把 None 传给 requests**——requests 对
    #   `timeout=None` 的语义是「无限等待」，比保留 120s 默认值危险得多。
    step_timeout = parsed_request_dict.get("timeout")
    if step_timeout is None:
        step_timeout = config.timeout

    if step_timeout is None:
        parsed_request_dict.pop("timeout", None)
    else:
        parsed_request_dict["timeout"] = utils.ensure_timeout_value(
            step_timeout, "request.timeout"
        )

    parsed_request_dict["json"] = parsed_request_dict.pop("req_json", {})

    # 网络层可选参数：None 表示「未设置」，必须从 kwargs 里移除——requests 对显式 None
    # 与「不传该参数」的语义并不相同，尤其 stream=None 会被当成 False，导致响应体被立刻
    # 读完、拿不到底层 socket 地址（client.py 的 client/server IP:Port 就依赖这一点）。
    for optional_network_key in ("proxies", "cert", "stream"):
        if parsed_request_dict.get(optional_network_key) is None:
            parsed_request_dict.pop(optional_network_key, None)

    # YAML 里 cert 写成两元素列表（[cert, key]）时统一转成元组，与 requests 的约定一致
    if isinstance(parsed_request_dict.get("cert"), list):
        parsed_request_dict["cert"] = tuple(parsed_request_dict["cert"])

    # log request
    # M4（0918-2）：这条日志（以及下面同一份文本的 Allure 附件）会进 .run.log 与 stdout，
    # 修复前把 headers/body 里的凭据原样打了出来。展示用文本走脱敏副本，
    # **真正发出去的请求仍用未脱敏的 parsed_request_dict**（下一行 session.request）。
    request_print = "====== request details ======\n"
    request_print += f"url: {utils.mask_sensitive_url(url)}\n"
    request_print += f"method: {method}\n"
    for k, v in parsed_request_dict.items():
        request_print += f"{k}: {pretty_format(utils.mask_sensitive_request_data(v))}\n"

    logger.debug(request_print)
    if ALLURE is not None:
        ALLURE.attach(
            request_print,
            name="request details",
            attachment_type=ALLURE.attachment_type.TEXT,
        )
    resp = runner.session.request(method, url, **parsed_request_dict)

    # NOTICE（批次 C / L14）：上传用的 `Utf8MultipartEncoder` 现在是**流式**的
    # （文件不再预先读进内存），它在读取过程中懒打开文件句柄、读完即关。
    # 请求正常发完时句柄已经关掉；这里再做一次防御性关闭，覆盖「发送中途失败/未读完」
    # 的情况（`close()` 幂等；普通 str/dict 的 data 没有 close → 用 getattr 判可调用）。
    request_data = parsed_request_dict.get("data")
    close_request_data = getattr(request_data, "close", None)
    if callable(close_request_data):
        try:
            close_request_data()
        except Exception as ex:  # noqa: BLE001
            logger.debug(f"failed to close request data: {ex}")

    # log response
    # NOTICE（0920 批次 2 / **N15**）：这条文本与下面的 Allure 附件同样是**持久化产物**
    # （`.run.log` / stdout / 报告），修复前响应头与响应体**完全未脱敏** ——
    # 服务端返回的 `Set-Cookie: sessionid=…` / `{"access_token": …}` 明文入库，
    # 而同一份请求的请求侧早已是 `******`（同一条记录里一半糊、一半不糊）。
    # 展示副本按键名脱敏；**断言素材不受影响**：下一行的 `ResponseObject(resp, …)`
    # 拿的是原始 `resp`，extract / validate 仍打向未脱敏的真值。
    response_print = "====== response details ======\n"
    response_print += f"status_code: {resp.status_code}\n"
    response_print += f"headers: {pretty_format(utils.mask_sensitive_request_data(dict(resp.headers)))}\n"

    try:
        resp_body = resp.json()
    except (requests.exceptions.JSONDecodeError, json.decoder.JSONDecodeError):
        resp_body = resp.content

    response_print += f"body: {pretty_format(utils.mask_sensitive_request_data(resp_body))}\n"
    logger.debug(response_print)
    if ALLURE is not None:
        ALLURE.attach(
            response_print,
            name="response details",
            attachment_type=ALLURE.attachment_type.TEXT,
        )
    resp_obj = ResponseObject(resp, runner.parser)
    step_variables["response"] = resp_obj

    # teardown hooks
    if step.teardown_hooks:
        call_hooks(runner, step.teardown_hooks, step_variables, "teardown request")

    # extract
    extractors = step.extract
    extract_mapping = resp_obj.extract(extractors, step_variables)
    step_result.export_vars = extract_mapping

    variables_mapping = step_variables
    variables_mapping.update(extract_mapping)

    # validate
    validators = step.validators
    try:
        resp_obj.validate(validators, variables_mapping)
        step_result.success = True
    except ValidationFailure as ex:
        # NOTICE（0919-18 / H1）：把**已经建好的 StepResult** 交给调用方
        # （`runner.__run_step` 会用 `ex.step_result` 把这一步记进报告）。
        # 下面的 `finally` 会继续往同一个对象里填 `data`（validators + 请求响应记录），
        # 所以 runner 最终拿到的是**填好的**那份。
        # 修复前这个对象随异常一起被丢掉：失败用例的 summary.json 里连一条记录都没有，
        # 排查时看不到「哪条断言挂了」。
        ex.step_result = step_result
        raise
    finally:
        session_data = runner.session.data
        session_data.success = step_result.success
        session_data.validators = resp_obj.validation_results

        # save step data
        step_result.data = session_data
        step_result.elapsed = time.time() - start_time
        # NOTICE（批次 A / M7）：`StepResult.content_size` 修复前**全仓没有任何写入点**，
        # 于是 summary.json 的 `records[].content_size` 恒为 0 —— 而同一条记录里的
        # `data.stat.content_size` 是**真实值**（实测 0 vs 44），报告内部自相矛盾。
        # 这与本仓已经修过的同类口径（`response_length: 0 bytes` 这种「看起来像答案的假数据」、
        # `content_size_is_unknown` 那条「未知不能写成 0」）是同一条原则，只是漏在了这一层。
        step_result.content_size = session_data.stat.content_size

    return step_result


def _request_step_type(step: TStep) -> Text:
    """request step 的 `type()` 串（`request-<方法>`）。

    NOTICE（批次 A / M7）：修复前写的是 `f"request-{self.__step.request.method}"`，
    而 `MethodEnum` 是 `class MethodEnum(Text, Enum)` —— Python 3.11+ 里
    `f"{MethodEnum.GET}"` 走 `str()` 得到的是 **`MethodEnum.GET`**（不是 `GET`），
    于是失败 step 在报告里的 `step_type` 是 `request-MethodEnum.GET`（实测），
    与 SQL/thrift 侧的 `sql-request-<sql>` / `thrift-request-<psm>-<method>` 风格也不一致。

    这里取 `.value`；**保留** `isinstance` 兜底是为了容忍「直接给 `TRequest.method`
    赋一个普通字符串」的用法（`TRequest` 没开 `validate_assignment`，那条路径拿到的是 str）。

    NOTICE: 成功路径（`run_step_request` 里的 `step_type="request"`）**刻意不动**——
    SQL/thrift 两个 step 模块同样是「成功记短名、失败记富名」（`"sql"` vs
    `sql-request-…`、`"thrift"` vs `thrift-request-…`），那是 0919-18 批次有意建立的口径，
    且有护栏用例（`tests/step_sql_request_test.py::test_failed_step_is_recorded_with_its_own_type`）
    钉住失败侧的富名。本次只修「枚举 repr 泄漏」这一个 bug。
    """
    method = step.request.method
    return f"request-{method.value if isinstance(method, MethodEnum) else method}"


class StepRequestValidation(IStep):
    def __init__(self, step: TStep):
        self.__step = step

    def __getattr__(self, name: Text):
        """动态分发自定义断言算子（`assert_<算子名>`）。

        P0 修复背景：`make.py` 会把 YAML 里 `validate` 的算子名**无条件**渲染成
        `.assert_<算子名>(...)` 链式调用，而本类原先只有 18 个硬编码的 `assert_*` 方法，
        于是任何自定义算子（写在项目 `debugtalk.py` 里、签名为
        `(check_value, expect_value, message="")` 的函数）都会在 pytest 收集阶段以
        `AttributeError` 整文件导入失败（连用例都收集不起来）。

        分发规则：把 `assert_<算子名>` 还原成标准 validator 形态
        `{算子名: [check, expect, message]}` 追加进 `step.validators`，
        后续由 `ResponseObject.validate` → `parser.get_mapping_function` 按同一名字取实现
        （项目 `debugtalk.py` 函数优先，然后是框架内置、Python 内置）。

        NOTICE：
        - 只有 `assert_` 前缀（且后面还有名字）才分发；其它名字一律抛 `AttributeError`，
          否则 `hasattr`/`repr`/dunder/copy 之类的探测都会被污染（`StepRequestValidation`
          在断言链里被大量探测）；
        - `self.__step` 会被编译成 `_StepRequestValidation__step`：若该属性不存在
          （例如未执行 `__init__`），上面的 `AttributeError` 分支会立刻兜住，不会递归回本方法；
        - 算子名拼错**不会静默通过**：运行期由 `parser.get_mapping_function` 抛
          `FunctionNotFound`；`make.py` 还会在 hmake 阶段提前拦一次
          （见 `make.ensure_known_comparators`）。
        """
        if not name.startswith("assert_") or name == "assert_":
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            )

        operator = name[len("assert_") :]

        def assert_method(
            jmes_path: Text, expected_value: Any, message: Text = ""
        ) -> "StepRequestValidation":
            self.__step.validators.append(
                {operator: [jmes_path, expected_value, message]}
            )
            return self

        assert_method.__name__ = name
        assert_method.__qualname__ = f"{type(self).__qualname__}.{name}"
        assert_method.__doc__ = (
            f"dynamic assert method for comparator: {operator}\n"
            f"等价于 validator {{{operator!r}: [jmes_path, expected_value, message]}}"
        )
        return assert_method

    def assert_equal(
        self, jmes_path: Text, expected_value: Any, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append({"equal": [jmes_path, expected_value, message]})
        return self

    def assert_not_equal(
        self, jmes_path: Text, expected_value: Any, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"not_equal": [jmes_path, expected_value, message]}
        )
        return self

    def assert_greater_than(
        self, jmes_path: Text, expected_value: Union[int, float], message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"greater_than": [jmes_path, expected_value, message]}
        )
        return self

    def assert_less_than(
        self, jmes_path: Text, expected_value: Union[int, float], message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"less_than": [jmes_path, expected_value, message]}
        )
        return self

    def assert_greater_or_equals(
        self, jmes_path: Text, expected_value: Union[int, float], message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"greater_or_equals": [jmes_path, expected_value, message]}
        )
        return self

    def assert_less_or_equals(
        self, jmes_path: Text, expected_value: Union[int, float], message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"less_or_equals": [jmes_path, expected_value, message]}
        )
        return self

    def assert_length_equal(
        self, jmes_path: Text, expected_value: int, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"length_equal": [jmes_path, expected_value, message]}
        )
        return self

    def assert_length_greater_than(
        self, jmes_path: Text, expected_value: int, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"length_greater_than": [jmes_path, expected_value, message]}
        )
        return self

    def assert_length_less_than(
        self, jmes_path: Text, expected_value: int, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"length_less_than": [jmes_path, expected_value, message]}
        )
        return self

    def assert_length_greater_or_equals(
        self, jmes_path: Text, expected_value: int, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"length_greater_or_equals": [jmes_path, expected_value, message]}
        )
        return self

    def assert_length_less_or_equals(
        self, jmes_path: Text, expected_value: int, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"length_less_or_equals": [jmes_path, expected_value, message]}
        )
        return self

    def assert_string_equals(
        self, jmes_path: Text, expected_value: Any, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"string_equals": [jmes_path, expected_value, message]}
        )
        return self

    def assert_startswith(
        self, jmes_path: Text, expected_value: Text, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"startswith": [jmes_path, expected_value, message]}
        )
        return self

    def assert_endswith(
        self, jmes_path: Text, expected_value: Text, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"endswith": [jmes_path, expected_value, message]}
        )
        return self

    def assert_regex_match(
        self, jmes_path: Text, expected_value: Text, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"regex_match": [jmes_path, expected_value, message]}
        )
        return self

    def assert_contains(
        self, jmes_path: Text, expected_value: Any, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"contains": [jmes_path, expected_value, message]}
        )
        return self

    def assert_contained_by(
        self, jmes_path: Text, expected_value: Any, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"contained_by": [jmes_path, expected_value, message]}
        )
        return self

    def assert_type_match(
        self, jmes_path: Text, expected_value: Any, message: Text = ""
    ) -> "StepRequestValidation":
        self.__step.validators.append(
            {"type_match": [jmes_path, expected_value, message]}
        )
        return self

    def assert_jsonschema_match(
        self, jmes_path: Text, expected_value: Any, message: Text = ""
    ) -> "StepRequestValidation":
        """第 19 个内置算子（P2-b）：断言检查项符合 JSON Schema。

        NOTICE: 这个方法是**显式**写出来的，尽管 `__getattr__` 也能兜住它 —— 显式写出来的好处：
        ① IDE 里有签名提示与文档；② `make.BUILTIN_COMPARATOR_NAMES` 之外多一处可读的落点；
        ③ 使用者能在 `StepRequestValidation` 的源码里看到「schema 只支持函数返回值 / 文件路径」的约定。

        `expected_value` 可以是 schema（dict）或 schema 文件路径（str）；
        详细约定与失败信息格式见 `builtin/comparators.py::jsonschema_match`。
        """
        self.__step.validators.append(
            {"jsonschema_match": [jmes_path, expected_value, message]}
        )
        return self

    def assert_xpath_match(
        self, jmes_path: Text, expected_value: Any, message: Text = ""
    ) -> "StepRequestValidation":
        """第 20 个内置算子（A2-1）：XML 响应体的 XPath 断言。需要可选依赖 `xml`（lxml）。

        NOTICE: 与 `assert_jsonschema_match` 一样是**显式**写出来的，理由相同：IDE 提示 +
        把「两种选择器写法 / 缺 lxml 怎么办」的约定放在可读的落点。

        `expected_value` 形态：`"选择器"`（只断存在）或 `["选择器", "期望文本"]`；
        选择器既可以是前缀无关的简化写法（`code` / `{URI}code` / `item[@id='1']`），
        也可以是完整 XPath 1.0（`//svc:code/text()`、`count(//item)`、`ancestor::` …）。
        详细约定、命名空间策略与失败信息格式见 `builtin/comparators.py::xpath_match`。
        """
        self.__step.validators.append(
            {"xpath_match": [jmes_path, expected_value, message]}
        )
        return self

    def assert_xpath_count(
        self, jmes_path: Text, expected_value: Any, message: Text = ""
    ) -> "StepRequestValidation":
        """第 21 个内置算子（A2-1）：XML 节点个数断言。需要可选依赖 `xml`（lxml）。

        `expected_value` 必须是 `[选择器, 个数]`；个数接受数字或数字字符串。
        详细约定见 `builtin/comparators.py::xpath_count`。
        """
        self.__step.validators.append(
            {"xpath_count": [jmes_path, expected_value, message]}
        )
        return self

    def assert_xml_schema_match(
        self, jmes_path: Text, expected_value: Any, message: Text = ""
    ) -> "StepRequestValidation":
        """第 22 个内置算子（A2-2）：用 XSD 契约校验 XML 响应体。需要可选依赖 `xml`（lxml）。

        NOTICE: 同样显式写出来（理由与 `assert_xpath_match` 一致）。

        `expected_value` 两种形态：XSD 文件路径（整个响应体就是那个元素），或
        `{"xpath": "QueryResponse", "xsd": "schemas/query.xsd"}`（**推荐**：客户 XSD 通常只描述
        Body 里的那层元素，先定位再校验）。路径相对**项目根目录**解析，可写 `${func()}`。
        详细约定、命名空间策略与失败信息格式见 `builtin/comparators.py::xml_schema_match`。
        """
        self.__step.validators.append(
            {"xml_schema_match": [jmes_path, expected_value, message]}
        )
        return self

    def struct(self) -> TStep:
        return self.__step

    def name(self) -> Text:
        return self.__step.name

    def type(self) -> Text:
        return _request_step_type(self.__step)

    def run(self, runner: InterfaceTester):
        return run_step_request(runner, self.__step)


class StepRequestExtraction(IStep):
    def __init__(self, step: TStep):
        self.__step = step

    def with_jmespath(self, jmes_path: Text, var_name: Text) -> "StepRequestExtraction":
        self.__step.extract[var_name] = jmes_path
        return self

    # def with_regex(self):
    #     # TODO: extract response html with regex
    #     pass
    #
    # def with_jsonpath(self):
    #     # TODO: extract response json with jsonpath
    #     pass

    def validate(self) -> StepRequestValidation:
        return StepRequestValidation(self.__step)

    def struct(self) -> TStep:
        return self.__step

    def name(self) -> Text:
        return self.__step.name

    def type(self) -> Text:
        return _request_step_type(self.__step)

    def run(self, runner: InterfaceTester):
        return run_step_request(runner, self.__step)


class RequestWithOptionalArgs(IStep):
    def __init__(self, step: TStep):
        self.__step = step

    def with_params(self, **params) -> "RequestWithOptionalArgs":
        self.__step.request.params.update(params)
        return self

    def with_headers(self, **headers) -> "RequestWithOptionalArgs":
        self.__step.request.headers.update(headers)
        return self

    def with_x_path(self, x_path: Text) -> "RequestWithOptionalArgs":
        """设置 X-Path 请求头（运行时解析变量后自动做 URL 编码，兼容中文路径）。

        统一文件服务平台等接口要求 X-Path 与请求体 path 一致，
        且含中文时需 URL 编码。此处仅保存原始值（可含 $var/${func()}），
        真正的解析与编码在 run_step_request 里完成。
        """
        self.__step.request.x_path = x_path
        return self

    def with_cookies(self, **cookies) -> "RequestWithOptionalArgs":
        self.__step.request.cookies.update(cookies)
        return self

    def with_data(self, data) -> "RequestWithOptionalArgs":
        self.__step.request.data = data
        return self

    def with_json(self, req_json) -> "RequestWithOptionalArgs":
        self.__step.request.req_json = req_json
        return self

    def set_timeout(self, timeout: float) -> "RequestWithOptionalArgs":
        self.__step.request.timeout = timeout
        return self

    def set_verify(self, verify: bool) -> "RequestWithOptionalArgs":
        """设置 step 级 TLS 校验开关（step 级优先于 config 级）。

        NOTICE（0921 / **verify 链式 setter 绕过校验**）：`TRequest` 没有
        `validate_assignment=True`，所以这里**必须**自己转 —— 修复前
        `.set_verify("false")` 会把字符串 `'false'` 一直送到 `requests`，
        而它是**真值**：requests 把它当 CA 证书路径
        （`OSError: ... invalid path: false`），且 TLS 关闭告警被吞掉
        （`client._warn_if_tls_verification_disabled` 判 `if verify:`）。
        `.env` / `${ENV(...)}` 出来的值永远是字符串，手写 Python API 很容易踩到。
        口径见 `utils.ensure_bool_value`（与 YAML 构造期校验同一套取值集合）。
        """
        self.__step.request.verify = utils.ensure_bool_value(verify, "request.verify")
        return self

    def set_allow_redirects(self, allow_redirects: bool) -> "RequestWithOptionalArgs":
        self.__step.request.allow_redirects = allow_redirects
        return self

    def with_proxies(self, **proxies) -> "RequestWithOptionalArgs":
        """设置请求级代理，如 .with_proxies(http="http://127.0.0.1:8888")。

        与 with_params/with_headers 一致：多次调用是累加（按协议键合并），不是覆盖。
        """
        merged_proxies = dict(self.__step.request.proxies or {})
        merged_proxies.update(proxies)
        self.__step.request.proxies = merged_proxies
        return self

    def set_cert(self, cert) -> "RequestWithOptionalArgs":
        """设置客户端证书：证书路径，或 [cert_path, key_path]。"""
        self.__step.request.cert = cert
        return self

    def set_stream(self, stream: bool) -> "RequestWithOptionalArgs":
        """设置是否流式读取响应（默认由 client 设为 True，用于取 socket 地址）。"""
        self.__step.request.stream = stream
        return self

    def upload(self, **file_info) -> "RequestWithOptionalArgs":
        self.__step.request.upload.update(file_info)
        return self

    def teardown_hook(
        self, hook: Text, assign_var_name: Text = None
    ) -> "RequestWithOptionalArgs":
        if assign_var_name:
            self.__step.teardown_hooks.append({assign_var_name: hook})
        else:
            self.__step.teardown_hooks.append(hook)

        return self

    def extract(self) -> StepRequestExtraction:
        return StepRequestExtraction(self.__step)

    def validate(self) -> StepRequestValidation:
        return StepRequestValidation(self.__step)

    def struct(self) -> TStep:
        return self.__step

    def name(self) -> Text:
        return self.__step.name

    def type(self) -> Text:
        return _request_step_type(self.__step)

    def run(self, runner: InterfaceTester):
        return run_step_request(runner, self.__step)


class RunRequest(object):
    def __init__(self, name: Text):
        self.__step = TStep(name=name)

    def with_variables(self, **variables) -> "RunRequest":
        self.__step.variables.update(variables)
        return self

    def with_retry(self, retry_times, retry_interval) -> "RunRequest":
        self.__step.retry_times = retry_times
        self.__step.retry_interval = retry_interval
        return self

    def setup_hook(self, hook: Text, assign_var_name: Text = None) -> "RunRequest":
        if assign_var_name:
            self.__step.setup_hooks.append({assign_var_name: hook})
        else:
            self.__step.setup_hooks.append(hook)

        return self

    def get(self, url: Text) -> RequestWithOptionalArgs:
        self.__step.request = TRequest(method=MethodEnum.GET, url=url)
        return RequestWithOptionalArgs(self.__step)

    def post(self, url: Text) -> RequestWithOptionalArgs:
        self.__step.request = TRequest(method=MethodEnum.POST, url=url)
        return RequestWithOptionalArgs(self.__step)

    def put(self, url: Text) -> RequestWithOptionalArgs:
        self.__step.request = TRequest(method=MethodEnum.PUT, url=url)
        return RequestWithOptionalArgs(self.__step)

    def head(self, url: Text) -> RequestWithOptionalArgs:
        self.__step.request = TRequest(method=MethodEnum.HEAD, url=url)
        return RequestWithOptionalArgs(self.__step)

    def delete(self, url: Text) -> RequestWithOptionalArgs:
        self.__step.request = TRequest(method=MethodEnum.DELETE, url=url)
        return RequestWithOptionalArgs(self.__step)

    def options(self, url: Text) -> RequestWithOptionalArgs:
        self.__step.request = TRequest(method=MethodEnum.OPTIONS, url=url)
        return RequestWithOptionalArgs(self.__step)

    def patch(self, url: Text) -> RequestWithOptionalArgs:
        self.__step.request = TRequest(method=MethodEnum.PATCH, url=url)
        return RequestWithOptionalArgs(self.__step)
