# -*- coding: utf-8 -*-
import time
from typing import Text
from urllib.parse import quote

from loguru import logger

from interfacetester import exceptions, utils
from interfacetester.exceptions import SqlMethodNotSupport, ValidationFailure
from interfacetester.models import (
    IStep,
    SessionData,
    SqlMethodEnum,
    StepResult,
    TSqlRequest,
    TStep,
)
from interfacetester.response import SqlResponseObject
from interfacetester.runner import ALLURE, InterfaceTester
from interfacetester.step_request import (
    StepRequestExtraction,
    StepRequestValidation,
    call_hooks,
)

try:
    import pymysql
    import sqlalchemy

    SQL_READY = True
except ModuleNotFoundError:
    SQL_READY = False


def ensure_sql_ready():
    if SQL_READY:
        return

    msg = """
    sql extension dependencies uninstalled, install first and try again.
    install with pip:
    $ pip install sqlalchemy pymysql

    or you can install interfacetester with optional sql dependencies:
    $ pip install "interfacetester[sql]"
    """
    logger.error(msg)
    # NOTICE: 这里不能 sys.exit(1)——在 pytest 进程内直接退出会把整个测试会话杀掉，
    # 其余用例的结果全部丢失（与 step_thrift_request.ensure_thrift_ready、
    # loader.load_debugtalk_functions 的同类修复保持一致）。
    # 抛异常则只让当前用例报错，其余用例继续跑。
    raise RuntimeError(
        "sql extension dependencies uninstalled, "
        'install with: pip install "interfacetester[sql]"'
    )


def run_step_sql_request(runner: InterfaceTester, step: TStep) -> StepResult:
    """run teststep:sql request"""
    start_time = time.time()

    step_result = StepResult(
        name=step.name,
        step_type="sql",
        success=False,
    )
    step_variables = runner.merge_step_variables(step.variables)
    # parse
    request_dict = step.sql_request.model_dump()
    parsed_request_dict = runner.parser.parse_data(request_dict, step_variables)
    config = runner.get_config()
    # NOTICE（0918-4 / L5）：数值字段一律用 `is None` 判断「未设置」，不用 `or`：
    #   - `or` 会把显式 `port: 0` 当成未设置吞掉；
    #   - 更隐蔽的另一半：`TSqlRequest.db_config` 原先沿用 `TConfigDB`（port 默认 3306，
    #     是**真值**），于是 `... or config.db.port` 的回退分支是**死代码**——
    #     `config.db.port` 永远不生效（实测 config=3307、step 不写 → 实际连 3306）。
    #     为此引入 `TSqlStepDbConfig`（port 默认 None），config 侧默认仍是 3306。
    # HTTP 侧（step_request.py:138-152）早就是这个口径，这里对齐。
    # 字符串字段保留 `or`（空串表示「未设置」的语义是对的）。
    parsed_request_dict["db_config"]["psm"] = (
        parsed_request_dict["db_config"]["psm"] or config.db.psm
    )
    parsed_request_dict["db_config"]["user"] = (
        parsed_request_dict["db_config"]["user"] or config.db.user
    )
    parsed_request_dict["db_config"]["password"] = (
        parsed_request_dict["db_config"]["password"] or config.db.password
    )
    parsed_request_dict["db_config"]["ip"] = (
        parsed_request_dict["db_config"]["ip"] or config.db.ip
    )
    if parsed_request_dict["db_config"]["port"] is None:
        parsed_request_dict["db_config"]["port"] = config.db.port
    parsed_request_dict["db_config"]["database"] = (
        parsed_request_dict["db_config"]["database"] or config.db.database
    )

    db_config = parsed_request_dict["db_config"]
    # NOTICE（0920 批次 5 / **N29**）：`psm` 是**死配置**，必须在目标不可用时说清楚。
    #
    # 上面的合并逻辑确实把 `psm` 读进了 `db_config`，但下面拼 DSN 时**只**用
    # `user/password/ip/port/database` —— `psm` **从头到尾没有第二处读取**
    # （对照 thrift 侧：那里的 `psm` 至少进日志与 `type()` 命名，见
    # `step_thrift_request.py:305,419`，所以那边刻意不拦）。
    # 于是「只配了 `psm` 没配 `ip`」时，DSN 会拼成
    #
    #     mysql+pymysql://:password@None:3306/None?charset=utf8mb4
    #
    # 连接打向字面量主机 `None`，报错看起来像 DNS/网络问题，
    # 而 `docs/能力清单.md` 把 `psm` 列为受支持字段、限制说明里也没提它。
    #
    # 口径（与 thrift 侧的 `ensure_no_unwired_thrift_fields` 同族，但更保守）：
    #   · 配了 `psm` 但**没有** `ip` → 响亮报错，并给出两条替代写法
    #     （写 ip / 在 debugtalk.py 里自己解析服务发现）；
    #   · `psm` 与 `ip` 同时给了 → 放行，但**告警说明 `psm` 不参与连接**，
    #     并把它打进请求日志（与 thrift 侧口径一致），避免"配了以为生效"。
    if db_config.get("psm"):
        if not db_config.get("ip"):
            raise exceptions.ParamsError(
                "配置了 `psm`（服务发现标识）但**没有** `ip`，而框架**不实现任何命名服务**"
                "—— `psm` 不参与连接目标解析，拼出来的 DSN 会指向字面量主机 `None`"
                "（报错看起来像 DNS 问题，实际是配置没生效）。\n"
                f"  psm: {db_config['psm']!r}\n"
                "  改法（二选一）：\n"
                "  1) 直接写连接地址：`ip` / `port`（step 级 `with_db_config(...)` "
                "或 `config.db` 都行）；\n"
                "  2) 需要服务发现时，在项目 `debugtalk.py` 里把 `psm` 解析成 ip/port 再传进来"
                "（框架不内置命名服务客户端，见 docs/能力清单.md §3.9 的口径）。"
            )
        logger.warning(
            f"`psm={db_config['psm']!r}` **只用于日志展示、不参与连接解析**"
            f"（实际连接目标由 ip/port 决定："
            f"{db_config.get('ip')}:{db_config.get('port')}）。"
            f"若本意是走服务发现，请按 docs/能力清单.md §3.9 的说明自行解析。"
        )

    # 同一用例内可能有多个数据源：只有 db_config 与当前连接一致时才复用，
    # 不一致时先关闭旧连接再按新配置重建。
    # NOTICE: 原实现只判 `if not runner.db_engine`，导致后续步骤 with_db_config() 换库/换主机
    # 被静默忽略，全部打到第一次的连接上。
    # runner.db_config 为 None 说明 engine 是 with_db_engine() 外部注入的（配置未知），
    # 此时沿用「一直复用」的旧语义。
    if (
        runner.db_engine is not None
        and runner.db_config is not None
        and runner.db_config != db_config
    ):
        logger.info(
            "db_config changed, rebuild db engine: "
            f"{db_config.get('ip')}:{db_config.get('port')}/{db_config.get('database')}"
        )
        try:
            runner.db_engine.close()
        except Exception as ex:
            logger.warning(f"failed to close previous db engine: {ex}")
        runner.db_engine = None
        runner.db_config = None

    if not runner.db_engine:
        ensure_sql_ready()
        from interfacetester.database.engine import DBEngine

        # NOTICE（批次 D / T4）：userinfo 这里**必须**用 `quote(x, safe="")`，不能用
        # `quote_plus`。`quote_plus` 把空格编成 `+`（那是 **query string** 的约定），
        # 而这个连接串的消费者 SQLAlchemy 解析 userinfo 用的是 `unquote`
        # （`sqlalchemy/engine/url.py` 只对 `username` / `password` 调 `unquote`），
        # **不会**把 `+` 还原成空格 → 驱动收到的是被改过的凭据。
        # 实测（真 sqlalchemy 2.0.54，全字符扫描）：只有空格会坏 ——
        # `password='p@ss word'` → `make_url().password == 'p@ss+word'`（≠ 用例里写的值），
        # 而 `@ % / : + 中文` 都往返一致。失败形态最难查：认证失败/连接被拒，
        # 但用例里写的口令明明是对的，且除了「口令里有空格」没有任何线索。
        #
        # ⚠️ **不要顺手把库名那一段也编码**：SQLAlchemy **不解码** path 段
        # （实测 `make_url("…//my%20db").database == 'my%20db'`，而原样写 `my db`
        # 拿到的正是 `'my db'`）——编了反而把库名改坏。口径就是「**编码 userinfo，
        # 原样透传 path/query**」，与 SQLAlchemy 的解析口径一一对应。
        db_uri = (
            f"mysql+pymysql://{quote(db_config['user'] or '', safe='')}:"
            f"{quote(db_config['password'] or '', safe='')}@{db_config['ip']}:"
            f"{db_config['port']}/{db_config['database']}?charset=utf8mb4"
        )
        runner.db_engine = DBEngine(db_uri)
        runner.db_config = db_config

    # parsed_request_dict["headers"].setdefault(
    #     "interfacetester-Request-ID",
    #     f"interfacetester-{self.__case_id}-{str(int(time.time() * 1000))[-6:]}",
    # )

    # setup hooks
    if step.setup_hooks:
        call_hooks(runner, step.setup_hooks, step_variables, "setup request")

    # log request（对 db_config 中的 password 脱敏，避免明文密码落日志）
    sql_request_print = "====== sql request details ======\n"
    sql_request_print += f"sql: {step.sql_request.sql}\n"
    # NOTICE（0920 批次 5 / N29）：`psm` 打进日志（与 thrift 侧 `thrift_request_print`
    # 的口径一致）—— 它是死配置，至少要让用户看见"我配的这个东西去哪了"。
    if db_config.get("psm"):
        sql_request_print += (
            f"psm: {db_config['psm']}（仅日志展示，不参与连接解析）\n"
        )
    for k, v in parsed_request_dict.items():
        if k == "db_config" and isinstance(v, dict) and v.get("password"):
            v = {**v, "password": "******"}
        v = utils.omit_long_data(v)
        sql_request_print += f"{k}: {repr(v)}\n"

    sql_request_print += "\n"

    if ALLURE is not None:
        ALLURE.attach(
            sql_request_print,
            name="sql request details",
            attachment_type=ALLURE.attachment_type.TEXT,
        )
    logger.info(f"Executing SQL: {parsed_request_dict['sql']}")
    # NOTICE（0919-9 / §三.3）：与 thrift step、HTTP 侧统一口径——执行前**换一份新的
    # `SessionData`**（HTTP 是 `client.py:233` 的 `HttpSession.request` 里做的）。
    # 修复前这里复用 `runner.session.data` 上现有的对象，于是同用例内多步之间互相污染：
    # 实测「两个 SQL step」时 `step_results[0].data is step_results[1].data` 为真
    # （前一步的报告显示后一步的断言结果），「HTTP step 后接 SQL step」时 SQL 那条记录里
    # 残留着上一个 HTTP 请求的 `req_resps`/`stat`/`address`。只影响报告，不影响断言。
    runner.session.data = SessionData()
    if step.sql_request.method == SqlMethodEnum.FETCHONE:
        sql_resp = runner.db_engine.fetchone(parsed_request_dict["sql"])
    elif step.sql_request.method == SqlMethodEnum.INSERT:
        sql_resp = runner.db_engine.insert(parsed_request_dict["sql"])
    elif step.sql_request.method == SqlMethodEnum.FETCHMANY:
        sql_resp = runner.db_engine.fetchmany(
            parsed_request_dict["sql"], parsed_request_dict["size"]
        )
    elif step.sql_request.method == SqlMethodEnum.FETCHALL:
        sql_resp = runner.db_engine.fetchall(parsed_request_dict["sql"])
    elif step.sql_request.method == SqlMethodEnum.UPDATE:
        sql_resp = runner.db_engine.update(parsed_request_dict["sql"])
    elif step.sql_request.method == SqlMethodEnum.DELETE:
        sql_resp = runner.db_engine.delete(parsed_request_dict["sql"])
    else:
        raise SqlMethodNotSupport(
            f"step.sql_request.method {parsed_request_dict['method']} not support"
        )

    # log response
    sql_response_print = "====== sql response details ======\n"
    if isinstance(sql_resp, dict):
        for k, v in sql_resp.items():
            v = utils.omit_long_data(v)
            sql_response_print += f"{k}: {repr(v)}\n"
    elif isinstance(sql_resp, list):
        sql_response_print += f"count: {len(sql_resp)}\n"
        sql_response_print += "-" * 34 + "\n"
        for el in sql_resp:
            for k, v in el.items():
                v = utils.omit_long_data(v)
                sql_response_print += f"{k}: {repr(v)}\n"
            sql_response_print += "-" * 34 + "\n"
    elif sql_resp is None:
        sql_response_print += "None\n"
    if ALLURE is not None:
        ALLURE.attach(
            sql_response_print,
            name="sql response details",
            attachment_type=ALLURE.attachment_type.TEXT,
        )

    resp_obj = SqlResponseObject(sql_resp, parser=runner.parser)
    step_variables["sql_response"] = resp_obj

    # teardown hooks
    if step.teardown_hooks:
        call_hooks(runner, step.teardown_hooks, step_variables, "teardown request")

    def log_sql_req_resp_details():
        err_msg = "\n{} SQL DETAILED REQUEST & RESPONSE {}\n".format("*" * 32, "*" * 32)
        err_msg += sql_request_print + sql_response_print
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
        # 让失败的 SQL step 也进报告（下面的 finally 会填好 data）。
        # 与 `step_request.py` 同一口径，详见那里的 NOTICE。
        ex.step_result = step_result
        log_sql_req_resp_details()
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


def _sql_step_type(step: TStep) -> Text:
    """SQL step 的 `type()` 串（`RunSqlRequest` 与其 validate/extract 链共用一份口径）。

    NOTICE（0919-18 / H1 连带，**潜伏缺陷**）：`StepSqlRequestValidation` /
    `StepSqlRequestExtraction` 继承的是 `StepRequestValidation` / `StepRequestExtraction`
    的 `type()`，那两个读的是 `self.__step.request.method`——而 SQL step 上
    `request` 是 **None**（数据在 `sql_request` 里），于是抛

        AttributeError: 'NoneType' object has no attribute 'method'

    修复前没有调用方读 `type()`，所以它一直是潜伏的；H1 的失败记录代码
    （`runner.__record_failed_step` 要写 `step_type`）第一次读它就在 SQL 用例上炸了，
    而且是在 `except` 里炸——把「断言失败」变成了「用例 error」，失败记录反而没写进去。
    """
    return f"sql-request-{step.sql_request.sql}"


class StepSqlRequestValidation(StepRequestValidation):
    def __init__(self, step: TStep):
        self.__step = step
        super().__init__(step)

    def type(self) -> Text:
        return _sql_step_type(self.__step)

    def run(self, runner: InterfaceTester):
        return run_step_sql_request(runner, self.__step)


class StepSqlRequestExtraction(StepRequestExtraction):
    def __init__(self, step: TStep):
        self.__step = step
        super().__init__(step)

    def type(self) -> Text:
        return _sql_step_type(self.__step)

    def run(self, runner: InterfaceTester):
        return run_step_sql_request(runner, self.__step)

    def validate(self) -> StepSqlRequestValidation:
        return StepSqlRequestValidation(self.__step)


class RunSqlRequest(IStep):
    def __init__(self, name: Text):
        self.__step = TStep(name=name)
        self.__step.sql_request = TSqlRequest()

    def with_variables(self, **variables) -> "RunSqlRequest":
        self.__step.variables.update(variables)
        return self

    def with_db_config(
        self, user=None, password=None, ip=None, port=None, database=None, psm=None
    ):
        # NOTICE（0918-4 / L5）：一律用 `is not None`。原来写的是 `if port:`，
        # 显式 `port=0` 会被当成「没传」而静默忽略（与 `or` 回退同一个坏习惯）。
        if user is not None:
            self.__step.sql_request.db_config.user = user
        if password is not None:
            self.__step.sql_request.db_config.password = password
        if ip is not None:
            self.__step.sql_request.db_config.ip = ip
        if port is not None:
            self.__step.sql_request.db_config.port = port
        if database is not None:
            self.__step.sql_request.db_config.database = database
        if psm is not None:
            self.__step.sql_request.db_config.psm = psm
        return self

    def fetchone(self, sql) -> "RunSqlRequest":
        self.__step.sql_request.method = SqlMethodEnum.FETCHONE
        self.__step.sql_request.sql = sql
        return self

    def fetchmany(self, sql, size) -> "RunSqlRequest":
        self.__step.sql_request.method = SqlMethodEnum.FETCHMANY
        self.__step.sql_request.sql = sql
        self.__step.sql_request.size = size
        return self

    def fetchall(self, sql) -> "RunSqlRequest":
        self.__step.sql_request.method = SqlMethodEnum.FETCHALL
        self.__step.sql_request.sql = sql
        return self

    def update(self, sql) -> "RunSqlRequest":
        self.__step.sql_request.method = SqlMethodEnum.UPDATE
        self.__step.sql_request.sql = sql
        return self

    def delete(self, sql) -> "RunSqlRequest":
        self.__step.sql_request.method = SqlMethodEnum.DELETE
        self.__step.sql_request.sql = sql
        return self

    def insert(self, sql) -> "RunSqlRequest":
        self.__step.sql_request.method = SqlMethodEnum.INSERT
        self.__step.sql_request.sql = sql
        return self

    def with_retry(self, retry_times, retry_interval) -> "RunSqlRequest":
        self.__step.retry_times = retry_times
        self.__step.retry_interval = retry_interval
        return self

    def teardown_hook(
        self, hook: Text, assign_var_name: Text = None
    ) -> "RunSqlRequest":
        if assign_var_name:
            self.__step.teardown_hooks.append({assign_var_name: hook})
        else:
            self.__step.teardown_hooks.append(hook)

        return self

    def setup_hook(self, hook: Text, assign_var_name: Text = None) -> "RunSqlRequest":
        if assign_var_name:
            self.__step.setup_hooks.append({assign_var_name: hook})
        else:
            self.__step.setup_hooks.append(hook)

        return self

    def struct(self) -> TStep:
        return self.__step

    def name(self) -> Text:
        return self.__step.name

    def type(self) -> Text:
        return _sql_step_type(self.__step)

    def run(self, runner) -> StepResult:
        return run_step_sql_request(runner, self.__step)

    def extract(self) -> StepSqlRequestExtraction:
        return StepSqlRequestExtraction(self.__step)

    def validate(self) -> StepSqlRequestValidation:
        return StepSqlRequestValidation(self.__step)

    def with_jmespath(
        self, jmes_path: Text, var_name: Text
    ) -> "StepSqlRequestExtraction":
        self.__step.extract[var_name] = jmes_path
        return StepSqlRequestExtraction(self.__step)
