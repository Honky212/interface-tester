import os
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Text, Union

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

Name = Text
Url = Text
BaseUrl = Union[HttpUrl, Text]
VariablesMapping = Dict[Text, Any]
FunctionsMapping = Dict[Text, Callable]
Headers = Dict[Text, Text]
Cookies = Dict[Text, Text]
Verify = bool
Hooks = List[Union[Text, Dict[Text, Text]]]
Export = List[Text]
Validators = List[Dict]
Env = Dict[Text, Any]

# NOTICE（批次 3 / H3）：请求 / 响应的**记录体**类型。
#
# JSON 的顶层允许是标量——RFC 8259 里 `123`、`true`、`1.5`、`"hello"` 都是**合法** JSON 文档，
# 实际服务端就会返回裸数字 / 裸布尔。而 `RequestData` / `ResponseData` 是**记录用**模型
# （只进 summary.json / HTML 报告 / Allure，不参与断言、也不参与真实收发），
# 修复前两个字段写成 `Union[Text, bytes, List, Dict, None]` —— **不含 int/float/bool**，
# 于是服务端返回 `123` 或 `true` 时 pydantic 在这里抛
# `ValidationError: 4 validation errors for ResponseData`：
# **请求其实已经发出去了**（服务端可能已改数据），框架却把整条用例的结果丢弃，
# 抛出的错还与用户 YAML 毫无关系（实测 `hrun` exit 1、报告里没有响应记录、
# 后续 extract / 断言全部不执行）。请求侧同源：YAML 里 `data: "123"` 这种数字样文本体，
# 经 `client.py` 的 `json.loads` 变成 int 后同样撞上。
#
# NOTICE（批次 3 / H3）：`bool` **必须出现在这个联合类型里**。
#
# `bool` 是 `int` 的子类，而 pydantic v2 的 int 校验器**接受** `True` 并把它变成 `1`
# （实测：`Union[int, float, Text, bytes, List, Dict, None]` 下 `body=True` -> `1`）。
# 所以漏掉 `bool` 不会报错，只会**静默**把布尔记录成数字，报告与 summary.json 一起失真。
#
# 至于**位置**：只要 `bool` 在，写在 `int` 前面还是后面都一样 —— pydantic v2 的 smart union
# 按「精确类型匹配」挑中 `bool`（实测 `Union[int, bool, ...]` 下 `body=True` -> `True`）。
# 写在最前只是读起来显眼，**不是**必需。回归用例见
# `tests/client_test.py::TestScalarRecordBody`（"true 不被记成 1" 那条护栏在
# 「联合类型里漏掉 bool」的注入下确实变红；见 `docs/缺陷修复日志0919-21.md` §五）。
RecordBody = Union[bool, int, float, Text, bytes, List, Dict, None]


class MethodEnum(Text, Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    PATCH = "PATCH"


class ProtoType(Enum):
    Binary = 1
    CyBinary = 2
    Compact = 3
    Json = 4


class TransType(Enum):
    Buffered = 1
    CyBuffered = 2
    Framed = 3
    CyFramed = 4


# configs for thrift rpc
class TConfigThrift(BaseModel):
    psm: Optional[Text] = None
    # NOTICE（0918-6 / L13）：`env`/`cluster` 的默认值从 None 改为「有意义的默认值」。
    # 原因与 L5 的 timeout 同源：step 侧字段的默认值如果是**真值**，
    # `parsed[...] or config.thrift.env` 的回退分支就是**死代码**，config 里的值永远不生效。
    # 现在 step 侧默认 None（=未设置）、config 侧保留有效默认值，
    # 于是「两边都不配」的结果与修复前完全一致（仍是 prod / default），
    # 而「只在 config 里配」终于生效。
    env: Text = "prod"
    cluster: Text = "default"
    # NOTICE（0919-10 / 批次 C，§三.1/3.4）：config 级的 `target` 与 `thrift_client`
    # **从未被 `run_step_thrift_request` 读取过**：
    #   - 合并逻辑只把 `config_thrift.psm/env/cluster/idl_path/include_dirs/method/
    #     service_name/ip/port/proto_type/trans_type/timeout` 拷进 step，
    #     既没有 `target`，也没有 `thrift_client`；
    #   - step 级 `thrift_client` 是**有效**的（`with_thrift_client(...)` 的唯一入口），
    #     config 级这个则完全无效。
    # 两者都保留字段（便于报错点名），但由 `ensure_no_unwired_thrift_fields()`
    # 在运行 thrift step 时**非默认即报错**，不再静默忽略。
    target: Optional[Text] = None  # ⚠️ 未实现（配置即报错）
    include_dirs: Optional[List[Text]] = None
    thrift_client: Any = None  # ⚠️ config 级无效（只用 step 级 with_thrift_client）
    # NOTICE（批次 3 / H2）：单位是**秒**（10 = 10 秒），换算成 thriftpy2 的毫秒
    # 在 `thrift_client.ThriftClient.__init__` 里统一做（`timeout * 1000`）。
    # 修复前这里虽然写着"秒"，但值被原样透传给 thriftpy2 的 `TSocket(socket_timeout=)`
    # ——那边按**毫秒**解释，于是默认 10 实际是 10 毫秒（见 `docs/缺陷修复日志0919-21.md`）。
    timeout: int = 10  # sec
    idl_path: Optional[Text] = None
    method: Optional[Text] = None
    ip: Text = "127.0.0.1"
    port: int = 9000
    service_name: Optional[Text] = None
    proto_type: ProtoType = ProtoType.Binary
    trans_type: TransType = TransType.Buffered


# configs for db
class TConfigDB(BaseModel):
    psm: Optional[Text] = None
    user: Optional[Text] = None
    password: Optional[Text] = None
    ip: Optional[Text] = None
    port: int = 3306
    database: Optional[Text] = None


class TThriftRequest(BaseModel):
    """rpc request model"""

    method: Text = ""
    params: Dict = {}
    thrift_client: Any = None
    idl_path: Text = ""  # idl local path
    # NOTICE（0918-4 / L5）：默认值改成 None（原来 `int = 10`）。
    # 10 是**真值**，会让 `run_step_thrift_request` 里
    # `parsed["timeout"] or config.thrift.timeout` 的回退分支变成死代码——
    # `config.thrift.timeout` 永远不生效（实测 config=30、step 不写 → 实际用 10）。
    # 改成 None 后 `is None` 判断能正常工作；`TConfigThrift.timeout` 默认仍是 10，
    # 因此「两边都不配」的结果与修复前完全一致。
    timeout: Optional[int] = None  # sec（单位是**秒**；进 thriftpy2 前由 ThriftClient ×1000）
    # NOTICE（0919-10 / 批次 C，§三.1 家族）：这里**删掉了** `transport` 字段与
    # `TransportEnum`。它在全仓内只出现在定义处——不在合并逻辑、不在目标指纹、
    # 也从不进 `ThriftClient(...)` 的构造参数（client 用的是 `trans_type`），
    # 是 `trans_type` 的**纯冗余平行开关**（两边都有 BUFFERED/FRAMED），
    # 而且日志里会把两个都打出来，用户按名字很容易挑错那一个。
    # 删除后误用是**响亮**的：`TThriftRequest().transport = ...` 直接
    # `ValueError: object has no field "transport"`，而不是「设了没反应」。
    include_dirs: List[Union[Text, None]] = []  # param of thriftpy2.load
    # NOTICE（0919-10 / 批次 C，§三.1）：**这个字段没有实现，且不会实现**。
    # 原注释写的是 `tcp://{ip}:{port} or sd://psm?cluster=xx&env=xx`，读起来像
    # 「配了就走服务发现」，但 `run_step_thrift_request` 的合并逻辑从不处理它、
    # `ThriftClient.__init__` 也不收这个参数——配了只会静默落到 `ip:port`
    # （默认 127.0.0.1:9000）。现在由 `ensure_no_unwired_thrift_fields()` 在
    # 实际运行该 step 时**当场报错**（非空即报），不再静默忽略；
    # 保留字段本身是为了让报错能点名「你配的是这个」，而不是抛一个无关的 TypeError。
    # 需要服务发现时请在 debugtalk.py 里解析出 ip/port 再传给 `with_ip/with_port`
    # —— 仓内没有命名服务客户端依赖，框架不会内置（见 docs/缺陷修复日志0919-3-复核结论.md §六.1）。
    target: Text = ""  # ⚠️ 未实现（配置即报错），见上方 NOTICE
    # NOTICE（0918-6 / L13）：默认值由 "prod"/"default" 改成 None（=未设置）。
    # 这两个默认值原本是**真值**，于是
    # `parsed["env"] or config.thrift.env` 的回退分支成为**死代码**——
    # `config.thrift.env`/`config.thrift.cluster` 永远不生效（与 L5 的 timeout 同一根因）。
    # 改成 None 后 `is None` 判断能正常工作；`TConfigThrift` 侧保留 "prod"/"default"，
    # 因此「两边都不配」的结果与修复前完全一致。
    # NOTICE: 这两个字段**只用于日志文本**，不进 ThriftClient 的构造参数。
    env: Optional[Text] = None
    cluster: Optional[Text] = None
    # NOTICE（0919-10 / 批次 C，§三.1）：`psm` 与 `env`/`cluster` 同属「**只用于日志**」
    # 的一类——它会被合并进 `parsed_request_dict`，用于请求日志文本与
    # `RunThriftRequest.type()` 的名字，但**不参与连接目标解析**（框架没有
    # 服务发现实现）。`ConfigThrift.psm()` 是公开 setter，所以这里**不报错**，
    # 只把口径写清楚（`能力清单.md` §3.9 同步登记）。
    psm: Text = ""
    service_name: Optional[Text] = None
    ip: Optional[Text] = None
    port: Optional[int] = None
    proto_type: Optional[ProtoType] = None
    trans_type: Optional[TransType] = None


class SqlMethodEnum(Text, Enum):
    FETCHONE = "FETCHONE"
    FETCHMANY = "FETCHMANY"
    FETCHALL = "FETCHALL"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class TSqlStepDbConfig(TConfigDB):
    """SQL **step 级**的库配置：所有字段默认「未设置」，由 `config.db` 补齐。

    NOTICE（0918-4 / L5）：不能让 step 沿用 `TConfigDB` 的 `port: int = 3306`。
    3306 是**真值**，会让 `run_step_sql_request` 里
    `parsed["port"] or config.db.port` 的回退分支变成死代码——
    `config.db.port` 永远不生效（实测 config=3307、step 不写 → 实际连 3306）。
    这里把 port 改成 `Optional[int] = None`，配合 `is None` 判断；
    `TConfigDB`（config 侧）保持 `3306`，因此「两边都不配」的结果与修复前一致。
    """

    port: Optional[int] = None


class TSqlRequest(BaseModel):
    """sql request model"""

    db_config: TSqlStepDbConfig = TSqlStepDbConfig()
    method: Optional[SqlMethodEnum] = None
    sql: Optional[Text] = None
    size: int = 0  # limit nums of sql result


class TOAuth2(BaseModel):
    """OAuth2 Client Credentials 认证配置。

    配置后，用例的所有请求会自动携带 ``Authorization: Bearer <access_token>``。
    token 由 runner 在首次请求前获取，并在过期前自动刷新（缓存于会话内）。

    NOTICE: 缓存字段挂在「用例自己的配置对象」上：同一用例被多次调用（如 parametrize
    的多组参数）会复用同一个 token，不同用例之间不共享（各自有独立的 Config 实例）。
    """

    token_url: Text = ""  # token 端点，如 http://host/oauth2/token
    client_id: Text = ""
    client_secret: Text = ""
    scope: Text = ""  # 可选
    grant_type: Text = "client_credentials"
    # 运行期由 runner 填充的缓存字段（不参与用例编写）
    access_token: Optional[Text] = None
    expires_at: float = 0  # token 过期时间戳（epoch 秒）


class TConfig(BaseModel):
    name: Name
    verify: Verify = False
    # 请求超时（秒）：None 表示「未显式设置」，运行时回落到 HttpSession 的默认超时
    # （120s，或环境变量 INTERFACETESTER_TIMEOUT 指定的值）。
    #
    # NOTICE（批次 7 / **L16**）：类型里**必须允许字符串引用**（`${ENV(...)}` / `$var`）。
    # 修复前是 `Optional[float]`，于是加载期就被 pydantic 拒掉：
    # `timeout: ${ENV(TIMEOUT)}` → `float_parsing` 错误，用户看不出"该字段不支持引用"；
    # 而 `runner._setup_runner` 里的注释**声称支持**（与 `base_url` 同款口径）、
    # 并且那段代码确实会把解析结果规范成数字（`ensure_timeout_value`）——
    # 也就是"运行期支持、加载期先被拒"的自相矛盾。
    # 现在与 `base_url` 同款：**允许写引用**，解析与类型规范都留给运行时
    # （引用解析出来是字符串，`ensure_timeout_value` 会转成 float，非法值给可读报错）。
    timeout: Optional[Union[float, Text]] = None

    @field_validator("timeout", mode="before")
    @classmethod
    def _normalize_config_timeout(cls, value):
        """数字样字符串照旧转 `float`；`${...}` 引用原样保留（运行期解析后再规范）。

        NOTICE（批次 7 / L16）：放宽类型之后**不能丢掉原有的标量强转**。
        `make.py` 生成的是 `Config(...).timeout(30.0)`，而 YAML 里写的是带引号的 `"30"`
        —— 修复前靠 pydantic 的 `float` 校验把 `"30"` 变成 `30.0`；类型放宽成
        `Union[float, Text]` 之后，`"30"` 会被 smart union 判成 `Text` 原样保留，
        生成物就变成 `.timeout("30")`（既有用例 `test_quoted_scalars_are_coerced` 立刻抓到）。
        所以这里显式保留"能转就转"的行为，只对**含 `${` 的引用**放行。
        """
        if isinstance(value, str) and "${" not in value:
            try:
                return float(value)
            except ValueError:
                # 非法字面量：原样交给运行期的 `ensure_timeout_value` 给可读报错
                return value
        return value
    base_url: BaseUrl = ""
    # Text: prepare variables in debugtalk.py, ${gen_variables()}
    variables: Union[VariablesMapping, Text] = {}
    parameters: Union[VariablesMapping, Text] = {}
    # setup_hooks: Hooks = []
    # teardown_hooks: Hooks = []
    # NOTICE: 用 default_factory 而不是 `= []`——虽然 pydantic v2 会深拷贝可变默认值，
    # 但显式声明「每个实例一份」更不容易被后续改动（如换回 v1 / 手写 dataclass）踩坑。
    export: Export = Field(default_factory=list)
    path: Optional[Text] = None
    # configs for other protocols
    thrift: Optional[TConfigThrift] = None
    db: TConfigDB = TConfigDB()
    # OAuth2 Client Credentials 认证
    oauth2: Optional[TOAuth2] = None

    # NOTICE（0920 批次 2 / **N16**）：`skip` 从"只由 hmake 消费"改成一等字段。
    #
    # 修复前它**不在模型里**（只出现在 `KNOWN_CONFIG_FIELDS` 里避免误报告警），
    # 而 `hmake` 把它渲染成生成物上的 `@pytest.mark.skip(reason=...)`。
    # 这对**独立收集的用例**是够的，但对**被引用用例**完全无效：
    # `step_testcase.run_step_testcase` 是**直接调用**子用例的 `test_start()`，
    # pytest 的 skip 标记对"直接调用"没有任何作用，运行期也没有任何地方读 `skip`
    # —— 于是「声明了 skip 的用例」在被别人引用时会**照常执行**（实测：单独跑
    # `1 skipped`，被 parent 引用后 `1 failed`，里面的步骤真的跑了）。
    #
    # 现在把它落进模型（引用链路上能读到它），由 `runner.test_start` 在被引用时
    # 真正跳过。类型用 `Union[bool, Text]`：`skip: true` 与 `skip: "原因"` 都合法
    # （`make_config_skip` 一直是这么解释的，两侧口径保持一致）。
    skip: Union[bool, Text] = False


class TRequest(BaseModel):
    """requests.Request model"""

    # NOTICE（0918-8 / M25）：**必须允许用字段名（而不只是别名）赋值**。
    # `req_json` 的别名是 `json`，而 pydantic v2 默认只认别名 —— 于是 YAML 里写
    # `request: {req_json: {...}}` 时：模型层静默忽略（extra="ignore"）、
    # known-fields 告警又因为 `req_json` 是**模型字段**而保持沉默
    # （告警文本里还把 `req_json` 列为"已支持"）→ 请求**没有 body**，而 hmake exit 0、零告警。
    # 生成器侧同步支持该字段名（见 `make.make_request_chain_style`），两侧口径一致。
    model_config = ConfigDict(populate_by_name=True)

    method: MethodEnum
    url: Url
    # 与链式 with_params(**kwargs) 保持一致：允许非字符串值（requests 编码时会 str()），
    # 修复前 YAML 写 params: {id: 1} 会被 pydantic 拒绝，而链式写法却能通过。
    params: Dict[Text, Any] = {}
    headers: Headers = {}
    # NOTICE（批次 8 收尾 / **M14**）：`json` 的接受面与 H3 收口的 `RecordBody` 对齐（**除 `bytes`**）。
    # 合法 JSON 的**顶层可以是任意值**（RFC 8259："a JSON value"），而修复前这里是
    # `Union[Dict, List, Text]` —— YAML 写 `json: 123` / `true` / `1.5` / 显式 `null` 全部在
    # **加载期**被拒（`pydantic_core.ValidationError: 3 validation errors for TRequest`），
    # 而三个候选类型也推不出"为什么不支持标量"（实测 `.tmp_report/probe_m14.py`）。
    # 与 H3 的关系：H3 把**记录层**（`RequestData`/`ResponseData.body`）的裸标量收口了，
    # 请求构造层却仍然表达不了 —— 同一个关注点上上下不一致，这里补齐。
    #
    # `bool` **必须在联合类型里**（与 `RecordBody` 同一条理由：漏掉它时 pydantic 的 int
    # 校验器会把 `True` 静默变成 `1`，报告与真实请求体一起失真）。
    # 刻意**不含 `bytes`**：`json=` 最终走 `json.dumps`，bytes 在那里必然是 TypeError；
    # 要发二进制请用 `data:`（`RecordBody` 含 bytes 是因为它是"记录"口径，不参与收发）。
    req_json: Union[Dict, List, Text, bool, int, float, None] = Field(
        None, alias="json"
    )
    data: Optional[Union[Text, Dict[Text, Any]]] = None
    cookies: Cookies = {}
    # step 级超时（秒）：None 表示「未显式设置」，运行时依次回落到 config.timeout 与
    # HttpSession 默认值（120s / INTERFACETESTER_TIMEOUT）。用 None 而不是写死 120，
    # 是为了让 config 级配置有机会生效（与上面 verify 的语义保持一致）；
    # NOTICE: 判断时一律用 `is None`，`timeout: 0`（立即超时）是合法值，用 `or` 会被吞掉。
    timeout: Optional[float] = None
    allow_redirects: bool = True
    # step 级 verify：None 表示「未显式设置」，运行时回落到 config.verify；
    # 显式 True/False 时以 step 为准（修复前会被 config.verify 无条件覆盖）。
    verify: Optional[Verify] = None
    upload: Dict = {}  # used for upload files
    x_path: Optional[Text] = None  # X-Path 请求头，运行时解析变量后做 URL 编码
    # 网络层可选参数（直接透传给 requests）：
    # NOTICE: 这三个字段原先在 TRequest 里不存在，而 pydantic 默认 `extra="ignore"`，
    # 于是 YAML 里写了 `proxies:` / `cert:` / `stream:` 会被**静默丢弃**——用户以为配了代理，
    # 实际请求直连，排查成本极高。现在补成正式字段（未知字段另有告警，见 loader）。
    # None 表示「未设置」，运行时必须从传给 requests 的 kwargs 里移除：
    # 尤其 stream=None 会被 requests 当作 False（立刻读完响应体，拿不到 socket 地址）。
    proxies: Optional[Dict[Text, Text]] = None
    cert: Optional[Union[Text, List[Text]]] = None  # 证书路径，或 [cert, key] 两元素
    stream: Optional[bool] = None


# request 中会被识别并写进生成用例的字段名（含 TRequest 的别名 "json"）。
# 用途：对无法识别的字段给出告警，避免「写了但被静默忽略」。
KNOWN_REQUEST_FIELDS = set(TRequest.model_fields) | {"json"}

KNOWN_CONFIG_FIELDS = set(TConfig.model_fields) | {
    # `skip` 现在是 `TConfig` 的正式字段（0920 批次 2 / N16），这条保留只为兼容
    # 历史写法——集合本身由 `model_fields` 派生，重复列出不会有害。
    "skip",
}

class TStep(BaseModel):
    # NOTICE（0918-8 / M25）：与 `TRequest` 同理——`validators` 是正式字段名、`validate` 是别名。
    # 默认配置下 pydantic 只认别名，于是「用 loader API 直接加载 `validators:` 写法」会被静默忽略
    # （CLI 路径由 `compat._ensure_step_attachment` 兜住了，所以只有 API 路径暴露）。
    #
    # NOTICE（0919-1 / L33）：这里必须同时打开 `validate_assignment`，否则下面
    # `retry_times` / `retry_interval` 的 `ge=0` **只对 YAML 路径生效**、对生成物路径无效。
    # 根因：`RunRequest.__init__` 建的是 `TStep(...)` **实例**，而 `with_retry` 是
    # **直接赋值改属性**（`self.__step.retry_times = retry_times`，见 step_request.py），
    # pydantic v2 默认**不校验赋值**。实测（详见 `docs/缺陷修复日志0919-1.md`）：
    #   - 只加 `ge=0`：YAML 路径报清晰的 ValidationError；
    #     `hmake` 生成的 `.with_retry(retry_times=-1)` **仍然**在运行期炸成
    #     `UnboundLocalError: cannot access local variable 'step_result'`（runner.py:440）。
    #   - 加上 `validate_assignment=True`：两条路径都在**赋值点**给出清晰报错。
    model_config = ConfigDict(populate_by_name=True, validate_assignment=True)

    name: Name
    request: Union[TRequest, None] = None
    testcase: Union[Text, Callable, None] = None
    variables: VariablesMapping = {}
    setup_hooks: Hooks = []
    teardown_hooks: Hooks = []
    # used to extract request's response field
    extract: VariablesMapping = {}
    # used to export session variables from referenced testcase
    export: Export = []
    validators: Validators = Field([], alias="validate")
    validate_script: List[Text] = []
    # NOTICE（0919-1 / L33）：`ge=0` 不是「锦上添花」，是**把误导性报错换成根因报错**。
    # 修复前无约束，`retry_times: -1` 会让 `range(retry_times + 1)` 变成空循环
    # （runner.py:419），循环体一次都不执行 → `step_result` 从未绑定 →
    # 在 `self.__session_variables.update(step_result.export_vars)`（runner.py:440）
    # 抛 `UnboundLocalError`——报错与根因（写了个负的重试次数）完全无关，
    # 而且指向的是框架内部变量名。`retry_interval: -5` 同理，
    # 会变成 `time.sleep(-5)` → `ValueError: sleep length must be non-negative`（runner.py:434）。
    retry_times: int = Field(0, ge=0)
    retry_interval: int = Field(0, ge=0)  # sec
    thrift_request: Union[TThriftRequest, None] = None
    sql_request: Union[TSqlRequest, None] = None


# step 中会被识别并写进生成用例的字段名。
# 用途：对无法识别的字段给出告警，避免「写了但被静默忽略」。
#
# NOTICE（2026-09-17 实测补充）：**只有一个地方例外**——YAML 里的钩子字段必须写**复数**
# `setup_hooks:` / `teardown_hooks:`（`make.py` 只认复数，生成的 Python 代码里方法名才是单数
# `.setup_hook()` / `.teardown_hook()`）。写成单数会被静默丢弃、钩子**根本不会执行**，
# 用例会「因为什么都没做」而通过——这是最难查的一类假通过。
KNOWN_STEP_FIELDS = set(TStep.model_fields) | {
    # validators 的 YAML 别名（pydantic 字段名是 validators，YAML 里写 validate）
    "validate",
}


class TestCase(BaseModel):
    config: TConfig
    teststeps: List[TStep]


class ProjectMeta(BaseModel):
    debugtalk_py: Text = ""  # debugtalk.py file content
    debugtalk_path: Text = ""  # debugtalk.py file path
    dot_env_path: Text = ""  # .env file path
    functions: FunctionsMapping = {}  # functions defined in debugtalk.py
    env: Env = {}
    # NOTICE（0919-16 / 批次 G，§三.6c）：默认值必须用 `default_factory`，
    # 不能写成 `RootDir: Text = os.getcwd()`。
    # 后者在**类创建时**（也就是 import 期）求值一次，之后整个进程都固化成那个值：
    #   - 框架内所有正常路径都会覆盖它（`loader.py:893` 在加载项目时赋值），
    #     所以**日常跑用例看不出问题**；
    #   - 但任何在 import 之后 `os.chdir()` 的进程（第三方 conftest、debugtalk、
    #     或把框架当库用的脚本），新构造出来的 `ProjectMeta()` 会拿到一个**过期的 cwd**；
    #     与 `load_project_meta("")` 的「什么都没加载时造一个默认 meta」分支叠加，
    #     就可能把相对路径解析到别处。
    # `default_factory` 每次实例化都调一次，语义是「**构造时**的当前工作目录」——
    # 与 `os.getcwd()` 这个默认值的本意一致。行为差异只在「import 之后 chdir 过」时出现。
    RootDir: Text = Field(default_factory=os.getcwd)


class TestsMapping(BaseModel):
    project_meta: ProjectMeta
    testcases: List[TestCase]


class TestCaseTime(BaseModel):
    start_at: float = 0
    start_at_iso_format: Text = ""
    duration: float = 0


class TestCaseInOut(BaseModel):
    config_vars: VariablesMapping = {}
    export_vars: Dict = {}


class RequestStat(BaseModel):
    content_size: float = 0
    response_time_ms: float = 0
    elapsed_ms: float = 0


class AddressData(BaseModel):
    client_ip: Text = "N/A"
    client_port: int = 0
    server_ip: Text = "N/A"
    server_port: int = 0


class RequestData(BaseModel):
    method: MethodEnum = MethodEnum.GET
    url: Url
    headers: Headers = {}
    cookies: Cookies = {}
    body: RecordBody = {}


class ResponseData(BaseModel):
    status_code: int
    headers: Dict
    cookies: Cookies
    encoding: Union[Text, None] = None
    content_type: Text
    body: RecordBody


class ReqRespData(BaseModel):
    request: RequestData
    response: ResponseData


class SessionData(BaseModel):
    """request session data, including request, response, validators and stat data"""

    success: bool = False
    # in most cases, req_resps only contains one request & response
    # while when 30X redirect occurs, req_resps will contain multiple request & response
    req_resps: List[ReqRespData] = []
    stat: RequestStat = RequestStat()
    address: AddressData = AddressData()
    validators: Dict = {}


class StepResult(BaseModel):
    """teststep data, each step maybe corresponding to one request or one testcase"""

    name: Text = ""  # teststep name
    step_type: Text = ""  # teststep type, request or testcase
    success: bool = False
    data: Optional[Union[SessionData, List["StepResult"]]] = None
    elapsed: float = 0.0  # teststep elapsed time
    content_size: float = 0  # response content size
    export_vars: VariablesMapping = {}
    attachment: Text = ""  # teststep attachment


StepResult.model_rebuild()


class IStep(object):
    def name(self) -> str:
        raise NotImplementedError

    def type(self) -> str:
        raise NotImplementedError

    def struct(self) -> TStep:
        raise NotImplementedError

    def run(self, runner) -> StepResult:
        # runner: InterfaceTester
        raise NotImplementedError


class TestCaseSummary(BaseModel):
    name: Text
    success: bool
    case_id: Text
    time: TestCaseTime
    in_out: TestCaseInOut = {}
    log: Text = ""
    step_results: List[StepResult] = []


class PlatformInfo(BaseModel):
    interfacetester_version: Text
    python_version: Text
    platform: Text


class Stat(BaseModel):
    total: int = 0
    success: int = 0
    fail: int = 0


class TestSuiteSummary(BaseModel):
    success: bool = False
    stat: Stat = Stat()
    time: TestCaseTime = TestCaseTime()
    platform: PlatformInfo
    testcases: List[TestCaseSummary]
