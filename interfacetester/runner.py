import os
import threading
import time
import uuid

import requests

from datetime import datetime, timezone
from typing import Dict, List, Optional, Text

try:
    import allure

    ALLURE = allure
except ModuleNotFoundError:
    ALLURE = None

from loguru import logger

from interfacetester import loader
from interfacetester.client import HttpSession
from interfacetester.config import Config
from interfacetester.exceptions import ParamsError, ValidationFailure
from interfacetester.loader import load_project_meta
from interfacetester.models import (
    ProjectMeta,
    StepResult,
    TConfig,
    TestCaseInOut,
    TestCaseSummary,
    TestCaseTime,
    VariablesMapping,
)
from interfacetester.parser import Parser
from interfacetester.utils import (
    LOGGER_FORMAT,
    ensure_timeout_value,
    ga4_client,
    mask_sensitive_variables,
    merge_variables,
)

# OAuth2 取 token 的超时（秒）。token 端点通常很快，未显式配置 config.timeout 时
# 用这个比业务请求更短的默认值；配置了 config.timeout 则以配置为准。
OAUTH2_TOKEN_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# 用例日志 sink 的登记表（0918-8 / M30）
#
# 背景：引用用例（step 里的 `RunTestCase`）会经 `with_case_id(runner.case_id)` 继承
# 父用例的 case_id，而 `.run.log` 的路径由 case_id 拼出 —— 父子两个 runner 于是给
# **同一个文件**各挂一个 loguru sink：子用例的每一行日志都写两遍
# （0918-8 复核时实测：115 行、`run step begin: child step` 出现两次、`status_code: 200`
# 出现四次；本批用真 `hmake` + `hrun` 复现：287 行 → 修好后 175 行），
# 读日志的人会以为步骤跑了两次，Allure 里同一个文件也被挂两次。
#
# 处置：同一路径只保留**一个** sink，由最外层用例（属主）负责挂载与移除，
# 引用用例复用它（refcount 记账）。这样子用例的日志仍然按时间顺序落进同一个文件，
# 既不重复也不丢——比「给子用例另起一个文件名」更贴近「一个用例一条完整日志」的预期。
_run_log_sinks: Dict[str, Dict[str, object]] = {}
_run_log_sinks_lock = threading.Lock()


def _is_live_log_sink(sink_id) -> bool:
    """sink 是否仍在 loguru 的处理器表里。

    NOTICE: loguru 没有「列出当前 handler」的公开 API，只能读 `_core.handlers`；
    取不到内部结构时按「仍在」处理（沿用既有行为），取到时就以真实状态为准 ——
    `utils.init_logger()` 会 `logger.remove()` 掉所有 sink，此时复用登记表里的
    旧 sink id 会让日志**静默丢失**，宁可重新挂一个也不能丢。
    """
    handlers = getattr(getattr(logger, "_core", None), "handlers", None)
    if not isinstance(handlers, dict):
        return True
    return sink_id in handlers


class SessionRunner(object):
    config: Config
    teststeps: List[object]  # list of Step

    parser: Parser = None
    session: HttpSession = None
    case_id: Text = ""
    root_dir: Text = ""
    thrift_client = None
    # 框架自建 thrift_client 时记录其「目标指纹」（idl/service/ip/port/...）；
    # None 表示 client 由 with_thrift_client() 外部注入，配置未知（0918-4 / M2）
    thrift_config = None
    db_engine = None
    # 框架自建 db_engine 时记录其 db_config 指纹；None 表示 engine 由 with_db_engine() 外部注入
    db_config = None

    __config: TConfig
    __project_meta: ProjectMeta = None
    # NOTICE: 不要用可变对象（list/dict）做类级默认值——会跨实例共享。
    # 这里统一用 None 占位，由 _setup_runner 为每个实例建立独立容器。
    __export: Optional[List[Text]] = None
    __step_results: Optional[List[StepResult]] = None
    __session_variables: Optional[VariablesMapping] = None
    __is_referenced: bool = False
    # NOTICE（0919-18 / H1）：**本用例是否抛过异常**这一事实的独立来源。
    #
    # 修复前 `get_summary()` 的 `summary_success` **只**由 `self.__step_results` 推导，
    # 而失败的那一步根本不会被记进去（`__run_step` 在重试耗尽时直接 `raise`，
    # 跳过了 `append`）。于是「2 步用例第 2 步断言失败」的 summary.json 是
    # `success: true` / `testcases: {success: 1, fail: 0}` / `records: []`——
    # 退出码是对的（1），但**机器可读产物在说谎**。
    #
    # 现在两个事实来源都参与：① 本标记（任何异常逃出用例 → False，覆盖
    # `__parse_config` / 钩子 / 非 step 异常）；② 逐个 step 的 `success`
    # （覆盖 step 内部把 `success=False` 记进结果、却没有抛异常的情形）。
    __failed: bool = False
    # time
    __start_at: float = 0
    __duration: float = 0
    # log
    __log_path: Text = ""

    def _setup_runner(self):
        self.__config = self.config.struct()
        # 为当前实例建立独立容器（见类属性处的 NOTICE），避免跨实例共享同一个可变对象
        self.__session_variables = dict(self.__session_variables or {})
        self.__export = list(self.__export or [])
        self.__step_results = list(self.__step_results or [])
        self.__start_at = 0
        self.__duration = 0
        self.__is_referenced = self.__is_referenced or False
        self.__failed = False

        self.__project_meta = self.__project_meta or load_project_meta(
            self.__config.path
        )
        self.case_id = self.case_id or str(uuid.uuid4())
        self.root_dir = self.root_dir or self.__project_meta.RootDir
        self.__log_path = os.path.join(self.root_dir, "logs", f"{self.case_id}.run.log")

        self.session = self.session or HttpSession()
        if self.parser is None:
            self.parser = Parser(None)

        # NOTICE（0919-12 / 批次 F，§二.5）：把**本用例所属项目**的根目录绑进
        # `multipart_encoder`，让 upload 的相对路径按「本项目」解析。
        # 修复前 `multipart_encoder` 内部用 `load_project_meta("")`（当前**已加载**的
        # 项目），多项目混跑/嵌套引用时可能拿到别的项目的 RootDir——同名文件若在那边
        # 存在，就会**静默上传错文件**。
        #
        # 三处刻意的选择：
        #  1) 绑在**函数对象**上而不是写进 `${multipart_encoder(...)}` 表达式：表达式里的
        #     kwargs 就是 upload 字段名，加任何「保留参数名」都会与用户字段撞车
        #     （见 `uploader.bind_multipart_encoder` 的 NOTICE）；路径本身也难安全地
        #     嵌进表达式字符串。表达式因此一个字都不用改。
        #  2) 绑在 `_setup_runner` 而不是 upload step 内部：**手工写的**
        #     `${multipart_encoder(...)}` 与 `upload:` 机制于是走同一条口径，不留第二条路径。
        #  3) 写进**每个 runner 一份**的函数表浅拷贝，而不是 `project_meta.functions`
        #     （那是按项目缓存、被多个 runner 共享的对象）——否则绑定会跨用例/跨 runner 残留。
        mapping = self.parser.functions_mapping
        # NOTICE（批次 7 / **L15**）：**无条件**拷一份，判据不能是"这张表是不是项目表"。
        #
        # 修复前是 `if not mapping or mapping is self.__project_meta.functions:` 才拷贝 ——
        # 于是**调用方自己传进来一张共享表**时（Python API：`Parser(mapping)` 后多个 runner
        # 共用它），绑定会被直接写进那张共享表，下一个 runner 再绑一次就把它**顶掉**：
        # A 用例的相对路径 upload 会按 **B 项目**的根解析 → **静默上传错文件**
        # （与 0919-12 修掉的那半个是同一个后果，只是入口不同）。
        # 现在无论表从哪来都先拷贝：`install_root_bound_multipart_encoder` 只作用于
        # 本 runner 的私有副本，跨用例/跨 runner 不再残留。
        mapping = dict(mapping) if mapping else dict(self.__project_meta.functions)
        self.parser.functions_mapping = mapping
        from interfacetester.ext.uploader import install_root_bound_multipart_encoder

        install_root_bound_multipart_encoder(mapping, self.root_dir)

    def with_session(self, session: HttpSession) -> "SessionRunner":
        self.session = session
        return self

    def __acquire_run_log_sink(self):
        """为当前用例的 `.run.log` 取一个 sink。

        Returns:
            int: 本 runner **自己挂载**的 sink id（需要由它移除）；
            None: 复用了别人（父用例）已挂载的同一个文件，本 runner 不拥有它。
        """
        key = os.path.abspath(self.__log_path)
        with _run_log_sinks_lock:
            entry = _run_log_sinks.get(key)
            if entry is not None and _is_live_log_sink(entry["sink_id"]):
                entry["refcount"] = int(entry["refcount"]) + 1
                logger.debug(
                    "reuse log sink of the same .run.log "
                    f"(referenced testcase): {self.__log_path}"
                )
                return None

        sink_id = logger.add(self.__log_path, format=LOGGER_FORMAT, level="DEBUG")
        with _run_log_sinks_lock:
            _run_log_sinks[key] = {"sink_id": sink_id, "refcount": 1}
        return sink_id

    def __release_run_log_sink(self, sink_id) -> None:
        """归还 sink：引用用例只减计数，最后一个使用者才真正 `logger.remove`。"""
        key = os.path.abspath(self.__log_path)
        with _run_log_sinks_lock:
            entry = _run_log_sinks.get(key)
            if entry is None:
                # 登记表里没有（例如外部已清理）→ 没东西可还，避免把别人的计数减错
                return
            entry["refcount"] = int(entry["refcount"]) - 1
            if int(entry["refcount"]) > 0:
                return
            # 先摘登记、再移除 sink：即使 remove 抛异常也不会留下「指向已死 sink」的登记
            _run_log_sinks.pop(key, None)

        try:
            logger.remove(entry["sink_id"])
        except ValueError:
            # sink 已被外部（如 utils.init_logger 的 logger.remove()）移除
            logger.debug(
                f"log sink {entry['sink_id']} already removed, skip removing: "
                f"{self.__log_path}"
            )

    def get_config(self) -> TConfig:
        return self.__config

    def set_referenced(self) -> "SessionRunner":
        self.__is_referenced = True
        return self

    def with_case_id(self, case_id: Text) -> "SessionRunner":
        self.case_id = case_id
        return self

    def with_variables(self, variables: VariablesMapping) -> "SessionRunner":
        self.__session_variables = variables
        return self

    def with_export(self, export: List[Text]) -> "SessionRunner":
        self.__export = export
        return self

    def with_thrift_client(self, thrift_client) -> "SessionRunner":
        self.thrift_client = thrift_client
        # 外部注入的 client 无法获知其目标配置，标记为「配置未知」，
        # 这样 thrift step 不会因配置比对而误判为「目标变化后重建」
        # （与 with_db_engine 的处置完全一致）。
        self.thrift_config = None
        return self

    def with_db_engine(self, db_engine) -> "SessionRunner":
        self.db_engine = db_engine
        # 外部注入的 engine 无法获知其连接配置，标记为“配置未知”，
        # 这样 sql step 不会因 db_config 比对而误判为「配置变化后重建」。
        self.db_config = None
        return self

    def get_oauth2_token(self) -> Text:
        """获取 OAuth2 Client Credentials access_token，过期自动刷新。

        仅当用例 config 里配置了 oauth2 时生效；否则返回 None。
        为避免污染用例的 session 请求记录，这里用独立的 requests 调用。
        """
        oauth2 = self.__config.oauth2
        if oauth2 is None:
            return None

        # 缓存未过期则直接复用
        if oauth2.access_token and time.time() < oauth2.expires_at:
            return oauth2.access_token

        # 解析配置里的变量/函数引用，例如 ${ENV(UNIFSP_CLIENT_ID)}、$var、${func()}
        parser = self.parser
        variables = self.__config.variables
        if parser is not None:
            token_url = parser.parse_data(oauth2.token_url, variables)
            client_id = parser.parse_data(oauth2.client_id, variables)
            client_secret = parser.parse_data(oauth2.client_secret, variables)
            scope = parser.parse_data(oauth2.scope, variables)
        else:
            token_url = oauth2.token_url
            client_id = oauth2.client_id
            client_secret = oauth2.client_secret
            scope = oauth2.scope

        if not token_url or not client_id:
            logger.warning(
                "oauth2 配置不完整（缺少 token_url/client_id），跳过自动认证"
            )
            return None

        data = {
            "grant_type": oauth2.grant_type,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scope:
            data["scope"] = scope

        try:
            # token 请求超时：config.timeout 显式配置时以它为准，否则用较短的默认值
            token_timeout = self.__config.timeout
            if token_timeout is None:
                token_timeout = OAUTH2_TOKEN_TIMEOUT_SECONDS

            resp = requests.post(token_url, data=data, timeout=token_timeout)
            resp.raise_for_status()
            token_data = resp.json()
        except Exception as ex:
            logger.error(f"获取 OAuth2 access_token 失败: {ex}")
            raise

        access_token = token_data.get("access_token")
        if not access_token:
            raise ParamsError(
                f"OAuth2 token 响应缺少 access_token 字段: {token_data}"
            )

        oauth2.access_token = access_token
        # 提前 60 秒过期，避免临界点使用过期 token
        oauth2.expires_at = time.time() + int(token_data.get("expires_in", 3600)) - 60
        logger.info("OAuth2 access_token 获取成功")
        return access_token

    def __parse_config(self, param: Dict = None) -> None:
        # parse config variables
        self.__config.variables.update(self.__session_variables)
        if param:
            self.__config.variables.update(param)
        self.__config.variables = self.parser.parse_variables(self.__config.variables)

        # parse config name
        self.__config.name = self.parser.parse_data(
            self.__config.name, self.__config.variables
        )

        # parse config base url
        self.__config.base_url = self.parser.parse_data(
            self.__config.base_url, self.__config.variables
        )

        # parse config timeout：支持字面量与 $var/${ENV(...)} 引用（与 base_url 一致）。
        # 引用解析出来是字符串，这里统一规范成数字，避免字符串超时传到 requests 才报错。
        if self.__config.timeout is not None:
            parsed_timeout = self.parser.parse_data(
                self.__config.timeout, self.__config.variables
            )
            self.__config.timeout = ensure_timeout_value(
                parsed_timeout, "config.timeout"
            )

    def get_export_variables(self, strict: bool = False) -> Dict:
        """收集本用例要导出的变量。

        Args:
            strict: **是否把"变量缺失"当成错误**。
                - ``True``：缺失即抛 ``ParamsError`` —— 用于**运行时变量传递**路径
                  （被引用用例的 `export` 要交给外层 step，契约没被满足就必须响亮失败，
                  见 `step_testcase.run_step_testcase`）；
                - ``False``（默认）：缺失只告警并跳过 —— 用于**报告/展示**路径
                  （`get_summary`）。

        NOTICE（0918-3 / H4）：默认值必须是**宽松**的，这是本方法的修复要点。
        `get_summary()` 由 `--save-tests` 生成的 `session_fixture` 在 **teardown** 里调用，
        修复前它在这里抛 `ParamsError`，后果是：

            $ hrun case.yml --save-tests
            1 passed ... 1 error   (exit code 1)     ← 用例本身是过的
            E  ParamsError: failed to export variable never_set_var from session variables {}
            logs/case.summary.json                   ← 没有生成

        即"用例通过 → 多一个 ERROR + 非零退出码 + **整份汇总丢失**"。
        真实场景（step 失败 → export 的变量没提取到）更糟：**真失败时汇总反而崩掉、
        证据全丢**。所以报告路径一律降级为告警，严格性只在变量传递路径上保留。
        """
        export_var_names = self.__export or self.__config.export
        export_vars_mapping = {}
        missing_var_names = []

        for var_name in export_var_names:
            if var_name not in self.__session_variables:
                if strict:
                    raise ParamsError(
                        f"failed to export variable {var_name} from session variables "
                        f"{self.__session_variables}"
                    )
                missing_var_names.append(var_name)
                continue

            export_vars_mapping[var_name] = self.__session_variables[var_name]

        if missing_var_names:
            # 不能静默吞掉：缺失的导出变量必须留下痕迹（否则是另一种"丢证据"）
            logger.warning(
                f"以下变量声明了 export 但本次运行没有产生，已跳过：{missing_var_names}\n"
                f"  case: {self.__config.name}（{self.case_id}）\n"
                f"  常见原因：提取该变量的 step 没有被执行（前面的 step 失败），"
                f"或 extract 路径写错。\n"
                f"  NOTICE: 汇总/报告仍会正常生成（不再因此丢掉整份 summary）；"
                f"被引用用例的 export 仍按严格模式校验。"
            )

        return export_vars_mapping

    def get_summary(self, strict_export: bool = False) -> TestCaseSummary:
        """get testcase result summary

        Args:
            strict_export: 透传给 `get_export_variables`。
                默认 ``False``（报告路径不该因缺少导出变量而崩，见 H4）；
                `step_testcase.run_step_testcase` **不再**依赖本方法的 export_vars
                （运行期取值直接走 `get_export_variables`，见批次 E 的 NOTICE），
                传 ``True`` 只是为了让它拿到的 `success` / `step_results` 与运行期一致。

        NOTICE（0919-13 / 批次 E，§二.2）：**本方法返回的是「报告对象」，其中的
        `export_vars` 一律是脱敏后的展示副本**，不能拿去做运行期变量传递。

        修复前的耦合是：`step_testcase` 用 `summary.in_out.export_vars` 当**运行期取值源**
        （再经 `runner.py:464` 的 `self.__session_variables.update(step_result.export_vars)`
        传给后续步骤），于是「脱敏展示副本」与「运行期取值」是同一个对象，
        只能选择不脱敏（0919-3 §三 的分析）——`config_vars` 早就脱敏了，
        `export_vars` 却一直明文进 summary.json / HTML 报告，两边的口径是分裂的。

        现在拆开了：
          - **运行期取值**：`get_export_variables(strict=...)`（未脱敏，原值）；
          - **展示副本**：本方法里的 `in_out.export_vars` 与每个
            `StepResult.export_vars`（都过 `mask_sensitive_variables`）。

        为什么每个 step 的 `export_vars` 也要脱敏：`step_results` 会整体进
        summary.json 的 `records`（`compat.py:534-535`）——只脱敏 `in_out` 会造成
        「看起来脱敏了，其实没有」，正是本仓最忌讳的一类假象。

        为什么用**拷贝**：`self.__step_results` 里的对象是运行期对象，
        `self.__session_variables.update(step_result.export_vars)` 直接读它们；
        原地脱敏会让下游拿到 `******`（0919-3 §三 已论证）。`model_copy` 是浅拷贝，
        `data` 等大对象仍是同一引用，不会复制请求响应记录。

        NOTICE（批次 A / H3）：`log` 字段只在**日志文件真的存在**时才写路径。
        修复前无条件写 `self.__log_path`，而配置解析阶段失败的用例从没挂上 sink
        （`logs/` 下一个 `.run.log` 都没有），于是 summary.json 里留下一条
        **指向不存在文件**的记录——排查时按图索骥必然落空。
        """
        start_at_timestamp = self.__start_at
        start_at_iso_format = (
            datetime.fromtimestamp(start_at_timestamp, timezone.utc)
            .replace(tzinfo=None)
            .isoformat()
        )

        summary_success = True
        for step_result in self.__step_results:
            if not step_result.success:
                summary_success = False
                break

        # NOTICE（0919-18 / H1）：上面那个循环只是**其中一个**事实来源（step 自己把
        # success 记成 False 的情形）。真正让失败用例被报成通过的是「异常逃出用例」这条路：
        # 修复前 `summary_success` 只看 `__step_results`，而失败的那一步不会进这个列表，
        # 空列表 → `True` → summary.json 里 `success: true` / `fail: 0` / `records: []`。
        # 现在把「本用例抛过异常」并进来（见 `__failed` 的 NOTICE）。
        summary_success = summary_success and not self.__failed

        # 展示副本：脱敏 export_vars，但**不动**运行期对象
        display_step_results = [
            step_result.model_copy(
                update={
                    "export_vars": mask_sensitive_variables(step_result.export_vars)
                }
            )
            for step_result in self.__step_results
        ]

        return TestCaseSummary(
            name=self.__config.name,
            success=summary_success,
            case_id=self.case_id,
            time=TestCaseTime(
                start_at=self.__start_at,
                start_at_iso_format=start_at_iso_format,
                duration=self.__duration,
            ),
            in_out=TestCaseInOut(
                # 两者都只用于报告展示：对疑似密钥的变量脱敏；
                # 运行期取值请用 `get_export_variables()`。
                config_vars=mask_sensitive_variables(self.__config.variables),
                export_vars=mask_sensitive_variables(
                    self.get_export_variables(strict=strict_export)
                ),
            ),
            log=self.__log_path if os.path.exists(self.__log_path) else "",
            step_results=display_step_results,
        )

    def merge_step_variables(self, variables: VariablesMapping) -> VariablesMapping:
        # override variables
        # step variables > extracted variables from previous steps
        variables = merge_variables(variables, self.__session_variables)
        # step variables > testcase config variables
        variables = merge_variables(variables, self.__config.variables)

        # parse variables
        return self.parser.parse_variables(variables)

    def __record_failed_step(self, step, ex: Exception) -> None:
        """把一个**失败的 step** 记进 `self.__step_results`（含可得的证据）。

        NOTICE（0919-18 / H1）：修复前失败的那一步**完全不进报告**——
        `__run_step` 在重试耗尽时直接 `raise`，跳过了 `append`，于是
        `summary.json` 的 `records` 里只剩失败之前的那些步骤，
        排查时看不到「哪条断言挂了」，`stat.teststeps.total` 也少算。

        证据从**抛异常那一层已经建好的 StepResult** 上取（`step_request` /
        `step_sql_request` / `step_thrift_request` 的 `finally` 里已经把
        `step_result.data`（含 `validators` 与请求响应记录）填好了，
        只是那份对象随异常一起被丢掉）。三个 step 模块现在把该对象挂在异常上
        （`ex.step_result`），这里只负责把它落盘成报告。

        刻意**不复用那个对象本身**，而是新建一份：
          - 同一份 `StepResult` 会同时出现在**被引用用例的子 runner** 与父 runner
            的报告里（子用例失败时异常会穿到父用例的 testcase step），共享对象会让
            两边的展示互相污染；
          - 名称/类型按**本 step** 记（引用用例失败时，报的是「那个 testcase step」，
            而不是子用例里具体哪一步——子用例自己的记录在它自己的 summary 里）。
        拿不到附带的 StepResult 时（例如异常发生在建 `StepResult` 之前、
        `ParamsError`、钩子失败），仍然记一条 **name/type 正确、`data` 为空**的失败记录：
        宁可「记录在、细节少」，也不要「失败不进报告」。
        """
        inner = getattr(ex, "step_result", None)
        step_type = self.__safe_step_type(step)
        if isinstance(inner, StepResult):
            step_result = StepResult(
                name=step.name(),
                step_type=step_type,
                success=False,
                data=inner.data,
                elapsed=inner.elapsed,
                content_size=inner.content_size,
                export_vars=dict(inner.export_vars or {}),
                attachment=inner.attachment,
            )
        else:
            step_result = StepResult(
                name=step.name(), step_type=step_type, success=False
            )

        self.__step_results.append(step_result)
        self.__failed = True

    @staticmethod
    def __safe_step_type(step) -> Text:
        """取 `step.type()`，失败时降级为 `"unknown"` 并**响亮**告警。

        NOTICE（0919-18 / H1 连带）：`step_type` 只用于展示，但它是在 `except` 块里取的
        ——一旦它自己抛异常，就会把原本的「断言失败（ValidationFailure）」替换成
        「用例 error」，而且**失败记录没写进去**、`__failed` 也没置位，
        等于把 H1 又变回假通过。

        这不是假想：本批第一次接上失败记录时 SQL 用例就撞了——
        `StepSqlRequestValidation` 继承 `StepRequestValidation.type()`，而 SQL step 上
        `request` 是 None，直接
            AttributeError: 'NoneType' object has no attribute 'method'。
        （该潜伏缺陷已在本批一并修掉，见 `step_sql_request._sql_step_type` 的 NOTICE；
        这里保留兜底，是因为「报告路径不能连累用例」这条不变量对**任何**自定义
        `IStep.type()` 实现都要成立。）
        """
        try:
            return step.type()
        except Exception as ex:
            try:
                step_name = step.name()
            except Exception:
                step_name = "<取名字也失败>"
            logger.warning(
                f"读 step.type() 失败（报告里记为 'unknown'）：{step_name} → "
                f"{type(ex).__name__}: {ex}"
            )
            return "unknown"

    def __run_step(self, step):
        """run teststep, step maybe any kind that implements IStep interface

        Args:
            step (Step): teststep

        """
        logger.info(f"run step begin: {step.name()} >>>>>>")

        # run step
        try:
            for i in range(step.retry_times + 1):
                try:
                    if ALLURE is not None:
                        with ALLURE.step(f"step: {step.name()}"):
                            step_result: StepResult = step.run(self)
                    else:
                        step_result: StepResult = step.run(self)
                    break
                except ValidationFailure:
                    if i == step.retry_times:
                        raise
                    else:
                        logger.warning(
                            f"run step {step.name()} validation failed,wait {step.retry_interval} sec and try again"
                        )
                        time.sleep(step.retry_interval)
                        logger.info(
                            f"run step retry ({i + 1}/{step.retry_times} time): {step.name()} >>>>>>"
                        )
        except Exception as ex:
            # NOTICE（0919-18 / H1）：重试耗尽（或非断言类异常）时，先把这一步记进
            # 报告再往外抛——修复前这里是**直接异常穿出**，报告里这一步凭空消失。
            # 刻意**不**在这条路径上更新 `__session_variables`：失败的 step 不导出变量，
            # 与修复前的行为保持一致（只补「报告可见性」，不改变量传递语义）。
            self.__record_failed_step(step, ex)
            raise

        # save extracted variables to session variables
        self.__session_variables.update(step_result.export_vars)
        # update testcase summary
        self.__step_results.append(step_result)

        logger.info(f"run step end: {step.name()} <<<<<<\n")

    def test_start(self, param: Dict = None) -> "SessionRunner":
        """main entrance, discovered by pytest"""
        ga4_client.send_event("test_start")
        print("\n")

        # NOTICE（批次 A / H3）：**用例级失败事实必须覆盖 `test_start` 的每一个阶段**。
        #
        # 修复前的顺序是「`_setup_runner` → `__parse_config` →（挂 sink）→ try: 跑 step」，
        # 而 `__failed = True` 只在那个 `try` 之内设置，`__duration` 更是在 `try/finally`
        # **之外**赋值。于是「配置解析阶段就失败」这一类异常（`config.timeout` 非法、
        # `config.variables` 里的 `${ENV(...)}` 缺失、`config.base_url` 解析失败……）
        # 会同时造成四件事（实测 `.tmp_audit/kernel/p2`，真 CLI）：
        #
        #   1. `summary.json` 的 `success: true` / `testcases.success: 1` —— 与 pytest 的
        #      `1 failed`、退出码 1 **直接矛盾**（机器可读产物在说谎）；
        #   2. `logs/` 里**一个 `.run.log` 都没有**（sink 还没挂），而 summary 的 `log`
        #      字段却指向那个文件（一条指向不存在文件的记录）；
        #   3. `details[].time.duration` 恒为 0（那行赋值在 `finally` 之后）；
        #   4. 连带 `compat._generate_conftest_for_summary` 的统计把失败用例算成通过。
        #
        # 现在：把 `_setup_runner` / 挂 sink / `__parse_config` / 跑 step 全放进同一个
        # `try`，任何异常都先 `__failed = True` 再抛出；`__duration` 与 sink 归还都放进
        # `finally`。sink 的挂载**提前到 `__parse_config` 之前**，于是配置阶段失败也留下
        # `.run.log`（这正是本仓「失败必须留证据」的口径）。
        log_sink_id = None
        sink_acquired = False
        try:
            self._setup_runner()
            # _setup_runner 会把 __start_at 清 0，所以计时起点只能放在它之后
            self.__start_at = time.time()

            # 记录 sink 句柄，finally 里必须移除：否则同一进程每跑一个用例就新增一个 sink，
            # 后续用例的日志会同时写进之前所有用例的 .run.log（内容重复）并泄漏文件句柄。
            # NOTICE（0918-8 / M30）：返回 None 表示「复用了父用例（引用用例）的 sink」——
            # 此时**不能**移除它，也不能重复挂 Allure 附件（同一个文件挂两次）。
            log_sink_id = self.__acquire_run_log_sink()
            sink_acquired = True

            self.__parse_config(param)

            if ALLURE is not None and not self.__is_referenced:
                # update allure report meta
                ALLURE.dynamic.title(self.__config.name)
                ALLURE.dynamic.description(f"TestCase ID: {self.case_id}")

            logger.info(
                f"Start to run testcase: {self.__config.name}, TestCase ID: {self.case_id}"
            )

            # run step in sequential order
            #
            # NOTICE（0919-14 / 登记项①）：在**本用例的步骤执行范围**内声明
            # 「本用例所属项目根」，供**断言期**的路径解析使用
            # （`comparators._resolve_schema_path` 找不到 runner，只能靠这个作用域）。
            # 修复前它读全局 `loader.project_meta` = 「**当前已加载**」的项目——
            # 跨项目引用时可能拿到别的项目，于是**用别的项目的 schema 去校验**
            # （同名 schema 存在时静默用错；严格的那个方向表现为「莫名失败」，
            # 宽松的那个方向表现为「**莫名通过**」）。
            # 被引用用例有自己的 runner 与自己的作用域，退出时自动恢复父用例的根。
            with loader.use_run_root_dir(self.root_dir):
                # NOTICE（0920 批次 2 / **N16**）：**被引用用例**的 `config.skip` 要真的生效。
                #
                # `config.skip` 对独立收集的用例是 `@pytest.mark.skip`（由 `hmake` 生成），
                # 但引用走的是 `step_testcase.run_step_testcase` 里的**直接调用**
                # `test_start()` —— pytest 标记对直接调用无效，于是"声明了 skip 的用例"
                # 被引用时会**照常执行**（改数据/打生产/把父用例弄红），且父用例报告里
                # 没有任何提示。这里在运行期兜住：被引用 + 声明了 skip → 不跑步骤。
                #
                # 只对被引用用例生效：独立用例的 skip 已经由 pytest 标记处理，
                # 若这里也拦，pytest 侧会把它记成 passed 而不是 skipped（报告口径回退）。
                if self.__is_referenced and self.__config.skip:
                    logger.warning(
                        f"被引用用例 `{self.__config.name}` 声明了 `config.skip`"
                        f"（{self.__config.skip!r}）→ **已跳过它的步骤**"
                        f"（pytest 的 skip 标记对『被引用』这种直接调用无效，"
                        f"所以由运行期执行；若你确实要跑它，请删掉该用例的 `config.skip`）"
                    )
                    return self

                # NOTICE（0919-18 / H1）：`__run_step` 已经会把**失败的 step** 记进报告；
                # 这一层再兜一次「用例级」事实：任何异常都让本用例的 `success` 为 False。
                # 修复前只靠 `__step_results` 推导，失败用例会报成 success=true。
                for step in self.teststeps:
                    self.__run_step(step)
        except Exception:
            # 批次 A / H3：用例级事实来源。覆盖 `_setup_runner`（项目定位/debugtalk 导入）、
            # 挂 sink（logs 目录不可写）、`__parse_config`、setup hooks、step 及其它任何路径。
            self.__failed = True
            raise
        finally:
            if self.__start_at:
                # 批次 A / H3：放在 `finally` 里，失败用例的耗时才不会被记成 0
                self.__duration = time.time() - self.__start_at

            if self.__log_path:
                logger.info(f"generate testcase log: {self.__log_path}")
            if ALLURE is not None and log_sink_id is not None:
                # 只有 sink 属主挂附件：引用用例的 .run.log 与父用例是**同一个文件**，
                # 两边各挂一次会让报告里出现两份完全相同、且都还在增长的日志。
                ALLURE.attach.file(
                    self.__log_path,
                    name="all log",
                    attachment_type=ALLURE.attachment_type.TEXT,
                )
            # 释放数据库连接，避免连接池泄漏
            if self.db_engine is not None:
                try:
                    self.db_engine.close()
                except Exception:
                    pass
                self.db_engine = None
                self.db_config = None

            # 释放 thrift 长连接（与上面的 db_engine 同理；修复前 thrift client 从不释放，
            # 每个用例都会留下一个未关闭的 TCP 连接）。
            # NOTICE: thrift_client 可能是外部经 with_thrift_client() 注入的对象，
            # 不保证有 close()，因此按可调用性判断。
            if self.thrift_client is not None:
                close_thrift_client = getattr(self.thrift_client, "close", None)
                if callable(close_thrift_client):
                    try:
                        close_thrift_client()
                    except Exception as ex:
                        logger.debug(f"failed to close thrift client: {ex}")
                self.thrift_client = None
                self.thrift_config = None

            # 归还日志 sink（上面那行 "generate testcase log" 仍会写入本用例日志）。
            # 引用用例只是减计数，属主（最外层用例）才真正移除。
            # NOTICE: 只有**真的取过** sink 才归还——`_setup_runner` 就失败时既没挂 sink，
            # 也不该去减别人（父用例）的 refcount。
            if sink_acquired:
                self.__release_run_log_sink(log_sink_id)

        return self


class InterfaceTester(SessionRunner):
    # split SessionRunner to keep consistent with golang version
    pass
