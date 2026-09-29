# -*- coding: utf-8 -*-
import platform
import time
from typing import Text, Union

from loguru import logger

from interfacetester import exceptions, utils
from interfacetester.exceptions import ValidationFailure
from interfacetester.models import (
    IStep,
    ProtoType,
    SessionData,
    StepResult,
    TConfigThrift,
    TransType,
    TStep,
    TThriftRequest,
)
from interfacetester.response import ThriftResponseObject
from interfacetester.runner import ALLURE, InterfaceTester
from interfacetester.step_request import (
    StepRequestExtraction,
    StepRequestValidation,
    call_hooks,
)

try:
    import thriftpy2

    from thrift.Thrift import TType

    THRIFT_READY = True
except ModuleNotFoundError:
    THRIFT_READY = False


def ensure_thrift_platform_supported() -> None:
    """校验当前平台是否支持 thrift（Windows 上 thriftpy2 不可用）。

    NOTICE: 修复前平台检查只在真正发起请求（run_step_thrift_request →
    ensure_thrift_ready）时才执行，用例构造阶段完全看不出来；这里抽成独立函数，
    由 RunThriftRequest.__init__ 提前调用，让问题在「构造 thrift step」时就暴露。
    """
    if platform.system() == "Windows":
        # NOTICE: 不要用 assert 做平台检查，python -O 会把 assert 整体剥离，
        # 导致 Windows 上继续走到 thrift 依赖分支，报出无关的 ImportError。
        raise RuntimeError(
            "Sorry,thrift not support Windows for now；"
            "请在 Linux/macOS 上运行该用例，或改用 HTTP step"
        )


def ensure_thrift_ready():
    ensure_thrift_platform_supported()
    if THRIFT_READY:
        return

    msg = """
    thrift extension dependencies uninstalled, install first and try again.
    install with pip:
    $ pip install cython thriftpy2 thrift

    or you can install interfacetester with optional thrift dependencies:
    $ pip install "interfacetester[thrift]"
    """
    logger.error(msg)
    # NOTICE: 这里不能 sys.exit(1)——在 pytest 进程内直接退出会把整个测试会话杀掉，
    # 其余用例的结果全部丢失（与 loader.load_debugtalk_functions 的同类修复保持一致）。
    # 抛异常则只让当前用例报错，其余用例继续跑。
    raise RuntimeError(
        "thrift extension dependencies uninstalled, "
        'install with: pip install "interfacetester[thrift]"'
    )


def ensure_no_unwired_thrift_fields(parsed_request_dict, config_thrift) -> None:
    """拦截「设了但完全不生效」的 thrift 字段（0919-10 / 批次 C）。

    NOTICE: 0919-3 复核确认了三处**死配置**（`docs/仍然存在的问题0919-3-复核结论.md`
    §三.1 / §三.4）：用户配了它们，框架一个字都不读，请求静默落到 `ip:port` 兜底
    （默认 `127.0.0.1:9000`），零信号。按批次 C 的拍板（保留 thrift，不实现服务发现），
    这里沿用 `make.py` 那一套 `UNSUPPORTED_*` 闸门风格：**非默认取值就响亮报错**，
    并告诉用户替代写法。

    刻意**不**拦的两类（避免假报）：
      - `psm` / `env` / `cluster`：它们**确实被读取**了——用于请求日志文本与
        `RunThriftRequest.type()` 的名字，只是不参与连接目标解析。口径已写进
        `models.py` 的字段 NOTICE 与 `能力清单.md` §3.9；
      - step 级 `thrift_client`：这是**唯一有效**的注入入口
        （`with_thrift_client(...)`），只有 config 级的那个从未被读取。
    """
    errors = []

    step_target = parsed_request_dict.get("target")
    if step_target:
        errors.append(f"step 级 `target={step_target!r}`（`TThriftRequest.target`）")
    config_target = getattr(config_thrift, "target", None)
    if config_target:
        errors.append(f"config 级 `target={config_target!r}`（`config.thrift.target`）")
    if getattr(config_thrift, "thrift_client", None) is not None:
        errors.append(
            f"config 级 `thrift_client={config_thrift.thrift_client!r}`"
            f"（`config.thrift.thrift_client`）"
        )

    if not errors:
        return

    raise exceptions.ParamsError(
        "以下 thrift 配置**从未被实现/读取**，配了不会生效：\n  - "
        + "\n  - ".join(errors)
        + "\n  请改成：\n"
        "  1) 连接目标用 step 级 `with_ip(...)` / `with_port(...)`，"
        "或 `config.thrift().ip(...)` / `.port(...)`；\n"
        "  2) 需要服务发现 / 命名服务（`sd://psm?cluster=...&env=...` 这类）时，"
        "请在项目 `debugtalk.py` 里自己解析成 ip/port 再传进来"
        "——框架不内置任何命名服务客户端（见 docs/缺陷修复日志0919-3-复核结论.md §六.1）；\n"
        "  3) thrift client 注入只用 **step 级** `with_thrift_client(...)`，"
        "config 级的那份不会被读取。\n"
        "  NOTICE: 修复前这些字段被**静默忽略**——请求照常发出、可能 200、用例通过，"
        "只有把日志和期望的调用目标对着看才发现打到了别处。"
    )


def run_step_thrift_request(runner: InterfaceTester, step: TStep) -> StepResult:
    """run teststep:thrift request"""
    start_time = time.time()

    step_result = StepResult(
        name=step.name,
        step_type="thrift",
        success=False,
    )
    step_variables = runner.merge_step_variables(step.variables)
    # parse
    request_dict = step.thrift_request.model_dump()
    parsed_request_dict = runner.parser.parse_data(request_dict, step_variables)
    config = runner.get_config()

    # NOTICE（0918-4 / L5）：数值字段一律用 `is None` 判断「未设置」，不用 `or`：
    #   - `or` 会把显式 `port: 0` / `timeout: 0` 当成未设置吞掉；
    #   - 更隐蔽的另一半：当 step 侧字段的**模型默认值本身是真值**时（`timeout` 原为
    #     `int = 10`），`or` 的回退分支是**死代码**——`config.thrift.timeout` 永远不生效
    #     （实测 config=30、step 不写 → 实际用 10）。为此把 `TThriftRequest.timeout`
    #     默认值改成 None（config 侧默认仍是 10，未配置时结果不变）。
    # HTTP 侧（step_request.py:138-152）早就是这个口径，这里对齐。
    # 字符串字段（psm/env/cluster/idl_path/method/service_name）保留 `or`：
    # 空串表示「未设置」的语义是对的。
    #
    # NOTICE（0918-8 / M31）：`TConfig.thrift` 是 `Optional[...] = None`，而 Python API
    # 写法（`config = Config("x")` 不调 `config.thrift()`）完全合法——修复前下面这 12 行
    # 会直接抛 `AttributeError: 'NoneType' object has no attribute 'psm'`，
    # 错误信息完全不涉及「配置块缺失」，用户只能猜。
    # 这里统一回落到 `TConfigThrift()` 的默认值（与「config 里什么都不配」等价），
    # 而不是报错：thrift 的全部关键字段都可以只写在 step 上（`with_idl_path/.with_ip/.with_port`），
    # 强制要求一个 config 块会把这个合法用法一并堵死。
    config_thrift = config.thrift
    if config_thrift is None:
        logger.debug(
            "config.thrift is not set, fall back to TConfigThrift defaults "
            "(step-level fields still win)"
        )
        config_thrift = TConfigThrift()
    parsed_request_dict["psm"] = parsed_request_dict["psm"] or config_thrift.psm
    # L13（0918-6）：`env`/`cluster` 与 L5 的 timeout/port 用的是同一口径——
    # 只要 step 侧字段的模型默认值是**真值**，`or` 的回退就是死代码。
    # 这两个字段的 step 侧默认值已改成 None，因此这里必须用 `is None`
    #（不再是 `or`，否则显式传空串也会被当成「未设置」）。
    if parsed_request_dict["env"] is None:
        parsed_request_dict["env"] = config_thrift.env
    if parsed_request_dict["cluster"] is None:
        parsed_request_dict["cluster"] = config_thrift.cluster
    parsed_request_dict["idl_path"] = (
        parsed_request_dict["idl_path"] or config_thrift.idl_path
    )
    parsed_request_dict["include_dirs"] = (
        parsed_request_dict["include_dirs"] or config_thrift.include_dirs
    )
    parsed_request_dict["method"] = (
        parsed_request_dict["method"] or config_thrift.method
    )
    parsed_request_dict["service_name"] = (
        parsed_request_dict["service_name"] or config_thrift.service_name
    )
    parsed_request_dict["ip"] = parsed_request_dict["ip"] or config_thrift.ip
    if parsed_request_dict["port"] is None:
        parsed_request_dict["port"] = config_thrift.port
    parsed_request_dict["proto_type"] = (
        parsed_request_dict["proto_type"] or config_thrift.proto_type
    )
    parsed_request_dict["trans_type"] = (
        parsed_request_dict["trans_type"] or config_thrift.trans_type
    )
    if parsed_request_dict["timeout"] is None:
        parsed_request_dict["timeout"] = config_thrift.timeout

    # parsed_request_dict["headers"].setdefault(
    #     "interfacetester-Request-ID",
    #     f"interfacetester-{self.__case_id}-{str(int(time.time() * 1000))[-6:]}",
    # )
    step_variables["thrift_request"] = parsed_request_dict

    # 闸门：拦截「设了但完全不生效」的字段（0919-10 / 批次 C）
    ensure_no_unwired_thrift_fields(parsed_request_dict, config_thrift)

    psm = parsed_request_dict["psm"]

    # ------------------------------------------------------------------
    # 解析 thrift client（0918-4：M1 校验 + M2 目标变化时重建）
    # ------------------------------------------------------------------
    step_thrift_client = parsed_request_dict["thrift_client"]

    # M1：字符串形式的 thrift_client 从不解析（`with_thrift_client` 的类型标注写了
    # `Union["ThriftClient", str]`，但历史上从未实现过字符串语义，也没有任何文档）。
    # 修复前这个字符串会被当成 client 直接赋给 runner.thrift_client——它是**真值**，
    # 于是既跳过建连、也跳过 ensure_thrift_ready() 的平台/依赖检查，直到
    # `runner.thrift_client.send_request(...)` 才炸出
    # `AttributeError: 'str' object has no attribute 'send_request'`，
    # 此时 setup_hooks 已经跑过了，错误位置离真正原因很远。
    # 处置原则与本项目的其它闸门一致：**宁可报错，也不让它烂到运行期**。
    if isinstance(step_thrift_client, str):
        raise exceptions.ParamsError(
            f"thrift_client 不能是字符串，它必须是一个 client 对象："
            f"{step_thrift_client!r}\n"
            f"  如需在用例里按名字取得 client，请写成 `${{你的函数()}}` 让框架先解析，"
            f"例如 `with_thrift_client(\"${{get_thrift_client()}}\")`，"
            f"其中 `get_thrift_client` 定义在项目 debugtalk.py 里并返回 client 对象；\n"
            f"  或者直接用 Python 传入对象：`.with_thrift_client(my_client)`。\n"
            f"  NOTICE: 修复前这里不会报错，而是等到发请求时才抛 "
            f"`AttributeError: 'str' object has no attribute 'send_request'`。"
        )

    if step_thrift_client is not None and not callable(
        getattr(step_thrift_client, "send_request", None)
    ):
        # 同一类问题的另一半：传进来的对象用不了，也会在发请求时才以裸
        # AttributeError 暴露（且此时钩子已执行）。这里提前说清楚。
        raise exceptions.ParamsError(
            f"thrift_client 必须提供可调用的 send_request(params, method) 方法，"
            f"实际拿到 {type(step_thrift_client).__name__}: {step_thrift_client!r}"
        )

    # 目标指纹：这几个字段共同决定「连的是哪个服务」。
    # 换了目标就必须重建连接，否则第二个 step 的配置被静默忽略、请求全打到第一个服务上。
    thrift_config = {
        "idl_path": parsed_request_dict["idl_path"],
        "service_name": parsed_request_dict["service_name"],
        "ip": parsed_request_dict["ip"],
        "port": parsed_request_dict["port"],
        "include_dirs": tuple(parsed_request_dict["include_dirs"] or ()),
        "timeout": parsed_request_dict["timeout"],
        "proto_type": parsed_request_dict["proto_type"],
        "trans_type": parsed_request_dict["trans_type"],
    }

    # M2：修复前只判 `if not runner.thrift_client`，同用例内第二个 step 换
    # idl/service/ip/port 会被**静默忽略**（db 侧同类 bug 已在 0916-7 修掉，thrift 漏修）。
    # runner.thrift_config 为 None 说明 client 是外部经 `with_thrift_client()` 注入的
    # （配置未知），此时沿用「一直复用」的旧语义——与 db 侧 `with_db_engine()` 完全一致。
    if (
        runner.thrift_client is not None
        and runner.thrift_config is not None
        and runner.thrift_config != thrift_config
    ):
        logger.info(
            "thrift target changed, rebuild thrift client: "
            f"{thrift_config['ip']}:{thrift_config['port']}/"
            f"{thrift_config['service_name']}"
        )
        close_previous = getattr(runner.thrift_client, "close", None)
        if callable(close_previous):
            try:
                close_previous()
            except Exception as ex:
                logger.warning(f"failed to close previous thrift client: {ex}")
        runner.thrift_client = None
        runner.thrift_config = None

    if not runner.thrift_client:
        runner.thrift_client = step_thrift_client

    if not runner.thrift_client:
        ensure_thrift_ready()
        from interfacetester.thrift.thrift_client import ThriftClient

        runner.thrift_client = ThriftClient(
            thrift_file=parsed_request_dict["idl_path"],
            service_name=parsed_request_dict["service_name"],
            ip=parsed_request_dict["ip"],
            port=parsed_request_dict["port"],
            include_dirs=parsed_request_dict["include_dirs"],
            timeout=parsed_request_dict["timeout"],
            proto_type=parsed_request_dict["proto_type"],
            trans_type=parsed_request_dict["trans_type"],
        )
        runner.thrift_config = thrift_config

    # setup hooks
    if step.setup_hooks:
        call_hooks(runner, step.setup_hooks, step_variables, "setup request")

    # log request
    thrift_request_print = "====== thrift request details ======\n"
    thrift_request_print += f"psm: {psm}\n"
    for k, v in parsed_request_dict.items():
        v = utils.omit_long_data(v)
        thrift_request_print += f"{k}: {repr(v)}\n"
    thrift_request_print += "\n"
    if ALLURE is not None:
        ALLURE.attach(
            thrift_request_print,
            name="thrift request details",
            attachment_type=ALLURE.attachment_type.TEXT,
        )

    # thrift request
    # NOTICE（0919-9 / §三.3）：发请求前**换一份新的 `SessionData`**，与 HTTP 侧对齐
    # （`client.py:233` 的 `HttpSession.request` 就是这么做的：每发一次请求就
    # `self.data = SessionData()`）。位置也刻意一致——在 **setup hooks 之后、发请求之前**。
    #
    # 修复前 thrift/SQL 都直接复用 `runner.session.data` 上现有的那个对象，于是
    # `StepResult.data` 在同一个用例内互相污染（实测）：
    #   - 连续两个 thrift step：`step_results[0].data is step_results[1].data` 为真，
    #     前一步的报告里显示的是**后一步**的 validators；
    #   - thrift 紧跟 HTTP step：thrift 那条记录里带着上一个 HTTP 请求的
    #     `req_resps` / `stat` / `address`。
    # 影响面只在报告层（`compat.py:534-535` 把 `step_results` dump 成 summary.json 的
    # `records`，HTML/Allure 同源），**不改变断言结果**；但它会把排查引向错误的对象，
    # 所以按 HTTP 的既有口径收口，而不是让三个 step 各写各的。
    runner.session.data = SessionData()
    resp = runner.thrift_client.send_request(
        parsed_request_dict["params"], parsed_request_dict["method"]
    )
    resp_obj = ThriftResponseObject(resp, parser=runner.parser)
    step_variables["thrift_response"] = resp_obj

    # log response
    thrift_response_print = "====== thrift response details ======\n"
    # NOTICE（0918-8 / M28）：thrift 方法**不一定**返回 struct。`string ping()` /
    # `i32 add(1, 2)` 这类返回值是 str / int，`resp.items()` 会抛
    # `AttributeError: 'str' object has no attribute 'items'`——错误信息里
    # 完全看不出问题出在「thrift 返回值形态」上。这里按形态分别打印；
    # 断言/提取仍走 `ThriftResponseObject`（jmespath 对裸值用 `@` 取整体）。
    if isinstance(resp, dict):
        for k, v in resp.items():
            v = utils.omit_long_data(v)
            thrift_response_print += f"{k}: {repr(v)}\n"
    else:
        thrift_response_print += (
            f"<{type(resp).__name__}> {utils.omit_long_data(resp)}\n"
        )
    if ALLURE is not None:
        ALLURE.attach(
            # NOTICE（0918-4 / L4）：这里原来贴的是 `thrift_request_print`
            #（复制粘贴错变量），于是报告里「request details」与「response details」
            # 两个附件内容一模一样，排查时看不到任何响应信息。
            thrift_response_print,
            name="thrift response details",
            attachment_type=ALLURE.attachment_type.TEXT,
        )

    # teardown hooks
    if step.teardown_hooks:
        call_hooks(runner, step.teardown_hooks, step_variables, "teardown request")

    def log_thrift_req_resp_details():
        err_msg = "\n{} THRIFT DETAILED REQUEST & RESPONSE {}\n".format(
            "*" * 32, "*" * 32
        )
        err_msg += thrift_request_print + thrift_response_print
        logger.error(err_msg)

    # extract
    extractors = step.extract
    # 传入 step_variables：extract 路径里可以写变量/函数（与 HTTP step 保持一致）
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
        # NOTICE（0919-18 / H1）：把已建好的 StepResult 交给 `runner.__run_step`，
        # 让失败的 thrift step 也进报告（下面的 finally 会填好 data）。
        # 与 `step_request.py` 同一口径，详见那里的 NOTICE。
        ex.step_result = step_result
        log_thrift_req_resp_details()
        raise
    finally:
        session_data = runner.session.data
        session_data.success = step_result.success
        session_data.validators = resp_obj.validation_results

        # save step data
        step_result.data = session_data
        step_result.elapsed = time.time() - start_time
        # 批次 A / M7：`StepResult.content_size` 修复前无任何写入点（恒为 0），
        # 与同一记录里的 `data.stat.content_size` 自相矛盾。口径见 `step_request.py`。
        step_result.content_size = session_data.stat.content_size
    return step_result


def _thrift_step_type(step: TStep) -> Text:
    """thrift step 的 `type()` 串（`RunThriftRequest` 与其 validate/extract 链共用）。

    NOTICE（0919-18 / H1 连带，**潜伏缺陷**）：与 `step_sql_request._sql_step_type`
    完全同源——`StepThriftRequestValidation` / `StepThriftRequestExtraction` 继承的
    `type()` 读 `self.__step.request.method`，而 thrift step 上 `request` 是 None
    （数据在 `thrift_request` 里），会抛
        AttributeError: 'NoneType' object has no attribute 'method'。
    """
    request = step.thrift_request
    return f"thrift-request-{request.psm}-{request.method}"


class StepThriftRequestValidation(StepRequestValidation):
    def __init__(self, step: TStep):
        self.__step = step
        super().__init__(step)

    def type(self) -> Text:
        return _thrift_step_type(self.__step)

    def run(self, runner: InterfaceTester):
        return run_step_thrift_request(runner, self.__step)


class StepThriftRequestExtraction(StepRequestExtraction):
    def __init__(self, step: TStep):
        self.__step = step
        super().__init__(step)

    def type(self) -> Text:
        return _thrift_step_type(self.__step)

    def run(self, runner: InterfaceTester):
        return run_step_thrift_request(runner, self.__step)

    def validate(self) -> StepThriftRequestValidation:
        return StepThriftRequestValidation(self.__step)


class RunThriftRequest(IStep):
    def __init__(self, name: Text):
        # fail fast：Windows 上 thrift 不可用，构造阶段就直接报错，而不是等用例跑到
        # 该 step 时才失败（那时错误会混在运行期信息里，且已经浪费了前序步骤的执行）。
        ensure_thrift_platform_supported()
        self.__step = TStep(name=name)
        self.__step.thrift_request = TThriftRequest()

    def with_variables(self, **variables) -> "RunThriftRequest":
        self.__step.variables.update(variables)
        return self

    def with_retry(self, retry_times, retry_interval) -> "RunThriftRequest":
        self.__step.retry_times = retry_times
        self.__step.retry_interval = retry_interval
        return self

    # NOTICE（0919-15 / 登记项②）：补齐 step 侧缺的 setter —— 只补**真正生效**的那些。
    #
    # 0919-3 复核（§三.5）列出的 step 侧缺口有 7 个字段，按「合并逻辑是否真的会读」分成两类：
    #   - **生效**（本批补）：`service_name` / `timeout` / `include_dirs`
    #     —— 合并逻辑都会「step 优先、否则回落 config」，所以 step 级 setter 有意义；
    #     修复前想按 step 设这三种，只能直接给属性赋值（`request.struct().thrift_request.X = ...`），
    #     绕过了公开 API。
    #   - **只用于日志**（刻意**不**补）：`env` / `cluster` / `psm` —— 它们只进请求日志文本与
    #     `RunThriftRequest.type()` 的名字，不参与寻址；而 config 侧（`ConfigThrift`）**已经有**
    #     对应 setter。给「display-only」字段补 step setter 只会让人以为它能影响连接，
    #     与 0919-10 / 批次 C 的收口方向相反。有闸门用例钉住这个决定。
    def with_service_name(self, service_name: str) -> "RunThriftRequest":
        self.__step.thrift_request.service_name = service_name
        return self

    def with_timeout(self, timeout: int) -> "RunThriftRequest":
        """step 级超时（**秒**）。未设置时回落 `config.thrift.timeout`（默认 10 = 10 秒）。

        NOTICE（批次 3 / H2）：单位是秒，且**只在这里、以及 config/ThriftClient 的
        `timeout` 参数上**是秒；换算成 thriftpy2 的毫秒在 `ThriftClient.__init__`
        调用 `make_client` 那一处统一做（`timeout * 1000`）。写 0 表示**不限时**。
        """
        self.__step.thrift_request.timeout = timeout
        return self

    def with_include_dirs(self, *include_dirs) -> "RunThriftRequest":
        """设置 `thriftpy2.load` 的 `include_dirs`（**整体替换**，不是追加）。

        NOTICE: `with_idl_path(idl_path, idl_root_path)` 也会设置它（`[idl_root_path]`），
        后调用的那个生效；需要多个 include 根时用本方法一次给全。
        """
        self.__step.thrift_request.include_dirs = list(include_dirs)
        return self

    def teardown_hook(
        self, hook: Text, assign_var_name: Text = None
    ) -> "RunThriftRequest":
        if assign_var_name:
            self.__step.teardown_hooks.append({assign_var_name: hook})
        else:
            self.__step.teardown_hooks.append(hook)

        return self

    def setup_hook(
        self, hook: Text, assign_var_name: Text = None
    ) -> "RunThriftRequest":
        if assign_var_name:
            self.__step.setup_hooks.append({assign_var_name: hook})
        else:
            self.__step.setup_hooks.append(hook)

        return self

    def with_params(self, **params) -> "RunThriftRequest":
        self.__step.thrift_request.params.update(params)
        return self

    def with_method(self, method) -> "RunThriftRequest":
        self.__step.thrift_request.method = method
        return self

    def with_idl_path(self, idl_path, idl_root_path) -> "RunThriftRequest":
        self.__step.thrift_request.idl_path = idl_path
        self.__step.thrift_request.include_dirs = [idl_root_path]
        return self

    def with_thrift_client(
        self, thrift_client: Union["ThriftClient", str]
    ) -> "RunThriftRequest":
        self.__step.thrift_request.thrift_client = thrift_client
        return self

    def with_ip(self, ip: str) -> "RunThriftRequest":
        self.__step.thrift_request.ip = ip
        return self

    def with_port(self, port: int) -> "RunThriftRequest":
        self.__step.thrift_request.port = port
        return self

    def with_proto_type(self, proto_type: ProtoType) -> "RunThriftRequest":
        self.__step.thrift_request.proto_type = proto_type
        return self

    def with_trans_type(self, trans_type: TransType) -> "RunThriftRequest":
        self.__step.thrift_request.trans_type = trans_type
        return self

    def struct(self) -> TStep:
        return self.__step

    def name(self) -> Text:
        return self.__step.name

    def type(self) -> Text:
        return _thrift_step_type(self.__step)

    def run(self, runner) -> StepResult:
        return run_step_thrift_request(runner, self.__step)

    def extract(self) -> StepThriftRequestExtraction:
        return StepThriftRequestExtraction(self.__step)

    def validate(self) -> StepThriftRequestValidation:
        return StepThriftRequestValidation(self.__step)

    def with_jmespath(
        self, jmes_path: Text, var_name: Text
    ) -> "StepThriftRequestExtraction":
        self.__step.extract[var_name] = jmes_path
        return StepThriftRequestExtraction(self.__step)
