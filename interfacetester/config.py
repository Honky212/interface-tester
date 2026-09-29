import copy
import inspect
import os
from typing import Text, Union

from loguru import logger

from interfacetester.models import TConfig, TConfigThrift, TConfigDB, TOAuth2, ProtoType, TransType, VariablesMapping
from interfacetester.utils import ensure_bool_value


class ConfigThrift(object):
    def __init__(self, config: TConfig) -> None:
        self.__config = config
        self.__config.thrift = TConfigThrift()

    def psm(self, psm: Text) -> "ConfigThrift":
        self.__config.thrift.psm = psm
        return self

    def env(self, env: Text) -> "ConfigThrift":
        self.__config.thrift.env = env
        return self

    def cluster(self, cluster: Text) -> "ConfigThrift":
        self.__config.thrift.cluster = cluster
        return self

    # NOTICE（0919-10 / 批次 C，§三.5）：补上这两个**真正生效**的 setter。
    # `run_step_thrift_request` 的合并逻辑一直在读
    # `config_thrift.idl_path` / `config_thrift.include_dirs`（若无 step 级取值就用
    # config 的），但 `ConfigThrift` 从来没提供过对应的方法——于是 config 级的
    # idl_path/include_dirs 是「读得到、设不进去」，Python API 用户只能写 step 级。
    #
    # 刻意**不**补 `target()` / `thrift_client()`：那两个字段是死的（config 级
    # thrift_client 从未被读取、target 没有实现），补 setter 等于主动提供一个
    # 「设置了但没用」的 API。它们现在由 `ensure_no_unwired_thrift_fields()`
    # 在运行期非默认即报错（见 models.py 的字段 NOTICE）。
    def idl_path(self, idl_path: Text) -> "ConfigThrift":
        self.__config.thrift.idl_path = idl_path
        return self

    def include_dirs(self, include_dirs) -> "ConfigThrift":
        self.__config.thrift.include_dirs = include_dirs
        return self

    def service_name(self, service_name: Text) -> "ConfigThrift":
        self.__config.thrift.service_name = service_name
        return self

    def method(self, method: Text) -> "ConfigThrift":
        self.__config.thrift.method = method
        return self

    def ip(self, ip: Text) -> "ConfigThrift":
        self.__config.thrift.ip = ip
        return self

    def port(self, port: int) -> "ConfigThrift":
        self.__config.thrift.port = port
        return self

    def timeout(self, timeout: int) -> "ConfigThrift":
        self.__config.thrift.timeout = timeout
        return self

    def proto_type(self, proto_type: ProtoType) -> "ConfigThrift":
        self.__config.thrift.proto_type = proto_type
        return self

    def trans_type(self, trans_type: TransType) -> "ConfigThrift":
        self.__config.thrift.trans_type = trans_type
        return self

    def struct(self) -> TConfig:
        return self.__config


class ConfigDB(object):
    def __init__(self, config: TConfig):
        self.__config = config
        self.__config.db = TConfigDB()

    def psm(self, psm):
        self.__config.db.psm = psm
        return self

    def user(self, user):
        self.__config.db.user = user
        return self

    def password(self, password):
        self.__config.db.password = password
        return self

    def ip(self, ip):
        self.__config.db.ip = ip
        return self

    def port(self, port: int):
        self.__config.db.port = port
        return self

    def database(self, database: Text):
        self.__config.db.database = database
        return self

    def struct(self) -> TConfig:
        return self.__config


class ConfigOAuth2(object):
    """OAuth2 Client Credentials 配置链式构造器（由 Config.oauth2() 返回）。"""

    def __init__(self, config: TConfig) -> None:
        self.__config = config
        self.__config.oauth2 = TOAuth2()

    def token_url(self, token_url: Text) -> "ConfigOAuth2":
        self.__config.oauth2.token_url = token_url
        return self

    def client_id(self, client_id: Text) -> "ConfigOAuth2":
        self.__config.oauth2.client_id = client_id
        return self

    def client_secret(self, client_secret: Text) -> "ConfigOAuth2":
        self.__config.oauth2.client_secret = client_secret
        return self

    def scope(self, scope: Text) -> "ConfigOAuth2":
        self.__config.oauth2.scope = scope
        return self

    def grant_type(self, grant_type: Text) -> "ConfigOAuth2":
        """设置授权类型（默认 `client_credentials`）。

        NOTICE（批次 B / M10）：`TOAuth2.grant_type` 一直在模型里、运行期
        `runner.get_oauth2_token()` 也**真的会读它**，但 Python API 侧一直没有 setter、
        YAML 侧生成器也不渲染（`make_config_chain_style` 只 emit 4 个子字段）——
        于是这个字段成了"设了没用/写了被丢"的典型。现在两侧都补齐。

        边界如实说明：框架只保证 **Client Credentials** 的语义（`client_id` +
        `client_secret` + 可选 `scope`）。选其它 `grant_type` 时会**原样发给 token 端点**
        （不再被静默改成 `client_credentials`），端口是否接受、是否需要额外参数由你的服务决定。
        """
        self.__config.oauth2.grant_type = grant_type
        return self

    def struct(self) -> TConfig:
        return self.__config


class Config(object):
    def __init__(self, name: Text) -> None:
        """NOTICE（0921-2 / **发现 2**）：`inspect.stack()` 定位失败时**回退 cwd**，
        与 `parser.parse_parameters` 的写法对齐。

        修复前这里是**裸调用** `inspect.stack()[1]`，而 `parser.py:783` 对同一件事
        （定位 caller path）写了 `try/except (IndexError, ValueError)` + 回退 cwd。
        值得注意的是 `parser.py` 那句注释自称"与 Config 的实现保持一致"——
        但 Config 从来没有那层兜底，于是"一致"只存在于注释里。

        `inspect.stack()` 的健壮性边界（实测确认为真）：某些执行环境下**拿不到调用帧**
        （例如解释器内嵌、`exec` 编译出的伪文件名、部分 C 扩展回调），
        `stack()[1]` 会抛 `IndexError`。此时修复前是**直接崩**在
        `Config(...)` 上，报错是 `IndexError: list index out of range` ——
        完全看不出"这是项目根定位失败"，用户会去查自己的用例。

        回退到 cwd 与 `loader.locate_project_root_directory` 找不到
        `debugtalk.py` 时的既有兜底口径一致（那里也是 `os.getcwd()`）。
        """
        try:
            caller_frame = inspect.stack()[1]
            caller_path = caller_frame.filename
        except (IndexError, ValueError) as ex:
            logger.warning(f"failed to locate caller path, fallback to cwd: {ex}")
            caller_path = os.getcwd()

        self.__name: Text = name
        self.__base_url: Text = ""
        self.__variables: VariablesMapping = {}
        self.__config = TConfig(name=name, path=caller_path)

    @property
    def name(self) -> Text:
        return self.__config.name

    @property
    def path(self) -> Text:
        return self.__config.path

    def variables(self, **variables) -> "Config":
        self.__variables.update(variables)
        return self

    def base_url(self, base_url: Text) -> "Config":
        self.__base_url = base_url
        return self

    def verify(self, verify: bool) -> "Config":
        """设置用例级 TLS 校验开关。

        NOTICE（0921 / **verify 链式 setter 绕过校验**）：`TConfig` 没有
        `validate_assignment=True`，所以这里**必须**自己转 —— 修复前
        `Config("x").verify("false")` 会把字符串 `'false'` 原样存进模型，
        而它是**真值**：`requests` 将其当成 CA 证书路径并抛
        `OSError: ... invalid path: false`，同时把 TLS 关闭告警吞掉
        （`client._warn_if_tls_verification_disabled` 判 `if verify:`）。
        口径见 `utils.ensure_bool_value`。
        """
        self.__config.verify = ensure_bool_value(verify, "config.verify")
        return self

    def timeout(self, timeout: float) -> "Config":
        """设置用例级请求超时（秒）。

        仅当 step 未显式设置 ``request.timeout`` 时生效；两者都没有时回落到
        HttpSession 的默认超时（120s，或环境变量 INTERFACETESTER_TIMEOUT 指定值）。
        """
        self.__config.timeout = timeout
        return self

    def export(self, *export_var_name: Text) -> "Config":
        # 去重且保持声明顺序：修复前用 set() 去重，导出顺序随机，
        # 生成的 `.export(*[...])` 顺序不稳定（引用用例的 export 顺序可能影响下游变量解析）。
        self.__config.export = list(
            dict.fromkeys([*self.__config.export, *export_var_name])
        )
        return self

    def skip(self, reason: Union[bool, Text] = True) -> "Config":
        """声明「本用例跳过」（`skip: "原因"` / `skip: true`）。

        NOTICE（0920 批次 2 / **N16**）：这个方法的存在是为了让 `config.skip`
        **进入运行期对象**。此前 `skip` 只被 `hmake` 消费成
        `@pytest.mark.skip`，于是**被引用用例**（`step_testcase` 直接调 `test_start()`）
        里的 `skip` 完全不生效——声明"未就绪"的用例被引用时照跑。
        `hmake` 现在会额外生成 `.skip(...)`，运行期据此真正跳过。
        """
        self.__config.skip = reason
        return self

    def struct(self) -> TConfig:
        self._sync_config()
        return self.__config

    def thrift(self) -> ConfigThrift:
        self._sync_config()
        return ConfigThrift(self.__config)

    def db(self) -> ConfigDB:
        self._sync_config()
        return ConfigDB(self.__config)

    def oauth2(self) -> ConfigOAuth2:
        self._sync_config()
        return ConfigOAuth2(self.__config)

    def _sync_config(self) -> None:
        self.__config.name = self.__name
        self.__config.base_url = self.__base_url
        self.__config.variables = copy.copy(self.__variables)
