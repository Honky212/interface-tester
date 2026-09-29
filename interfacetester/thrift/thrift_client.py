# -*- coding: utf-8 -*-
from __future__ import absolute_import

import enum
import hashlib
import json
import os

import thriftpy2
from loguru import logger
from thriftpy2.protocol import (
    TBinaryProtocolFactory,
    TCompactProtocolFactory,
    TCyBinaryProtocolFactory,
    TJSONProtocolFactory,
)
from thriftpy2.rpc import make_client
from thriftpy2.transport import (
    TBufferedTransportFactory,
    TCyBufferedTransportFactory,
    TCyFramedTransportFactory,
    TFramedTransportFactory,
)

from interfacetester import exceptions
from interfacetester.models import ProtoType, TransType
from interfacetester.thrift.data_convertor import json2thrift, thrift2dict


def _thrift_module_key(thrift_file: str, include_dirs=None) -> str:
    """算一个**按 IDL 文件唯一**的 thriftpy2 模块名（0920 批次 5 / N27）。

    `thriftpy2` 用 `module_name`（给了就用它、否则用 path）当**进程级缓存键**，
    所以这个键必须满足「同一个 IDL ⇒ 同一个模块；不同 IDL ⇒ 不同模块」。
    修复前用的是 `service_name + "_thrift"`，两个不同 IDL 只要服务同名就会**互相覆盖**
    （第二个文件从不解析）——详见 `ThriftClient.__init__` 的 NOTICE。

    做法：绝对路径（规范化 + 大小写归一，Windows 上 `Foo.thrift` 与 `foo.thrift`
    是同一个文件）+ 短哈希。带上 include_dirs 是因为它会影响解析结果
    （同一份 IDL 配不同 include 目录可能解析出不同结构）。

    NOTICE（**后缀**）：thriftpy2 强制要求模块名以 `_thrift` 结尾
    （否则抛 `ThriftParserError: thriftpy2 can only generate module with '_thrift' suffix`，
    实测踩到）——所以是 `<哈希>_thrift` 的形态，哈希在前、后缀在后。
    同时它必须**以字母或下划线开头**（会被当模块名用），哈希是十六进制，
    所以统一加 `idl_` 前缀。
    """
    normalized = os.path.normcase(os.path.abspath(str(thrift_file)))
    parts = [normalized]
    if include_dirs:
        parts.extend(
            os.path.normcase(os.path.abspath(str(item))) for item in include_dirs
        )
    digest = hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()[:12]  # noqa: S324
    return f"idl_{digest}_thrift"


class RequestFormat(enum.Enum):
    json = 1
    binary = 2


def get_proto_factory(proto_type):
    if proto_type == ProtoType.Binary:
        return TBinaryProtocolFactory()
    if proto_type == ProtoType.CyBinary:
        return TCyBinaryProtocolFactory()
    if proto_type == ProtoType.Compact:
        return TCompactProtocolFactory()
    if proto_type == ProtoType.Json:
        return TJSONProtocolFactory()


def get_trans_factory(trans_type):
    if trans_type == TransType.Buffered:
        return TBufferedTransportFactory()
    if trans_type == TransType.CyBuffered:
        return TCyBufferedTransportFactory()
    if trans_type == TransType.Framed:
        return TFramedTransportFactory()
    if trans_type == TransType.CyFramed:
        return TCyFramedTransportFactory()


class ThriftClient(object):
    def __init__(
        self,
        thrift_file,
        service_name,
        ip,
        port,
        include_dirs=None,
        # 单位：**秒** —— 本框架对外一律是秒，换算成 thriftpy2 的毫秒在下面
        # `make_client` 那一行（见那里的 NOTICE）。
        #
        # NOTICE（批次 3 / H2）：这里原先写 3000，0916-8 为了「与模型默认值 10 对齐」
        # 改成了 10，并在注释里把 3000 说成「50 分钟的默认超时」——**单位判断反了**：
        # 3000 是 thriftpy2 的毫秒 = **3 秒**（正是它自己的默认值），而 10 毫秒 = 10 ms。
        # 于是那次「对齐」把默认超时从 3 秒改成了 **10 毫秒**，方向是**退化**；
        # 配置侧的 `TConfigThrift.timeout = 10` 也一直是 10 ms。现在值仍是 10（=10 秒），
        # 把 ×1000 补在库边界上，两条路径一起修好。
        timeout=10,
        proto_type=ProtoType.CyBinary,
        trans_type=TransType.CyBuffered,
    ):
        self.thrift_file = thrift_file
        self.include_dirs = include_dirs
        self.service_name = service_name
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.proto_type = proto_type
        self.trans_type = trans_type
        try:
            # NOTICE（0920 批次 5 / **N27**）：`module_name` 必须是**按 IDL 文件唯一**的
            # 缓存键，不能只用 `service_name`。
            #
            # thriftpy2 的缓存键是 `cache_key = module_name or os.path.normpath(path)`
            # （`thriftpy2/parser/parser.py`），而修复前这里传的是
            # `str(self.service_name) + "_thrift"` —— **两个不同项目/版本的 `.thrift`
            # 只要声明的服务同名，第二次 `load` 就会命中缓存、拿到第一个 IDL 的模块**，
            # 那个文件**从未被解析**。实测（`.tmp_audit/n27_check.py`，thriftpy2 0.7.1）：
            #
            # ```text
            # one.thrift: struct Req {1: string name, 2: i32 count}   service Foo
            # two.thrift: struct Req {1: i64 amount, 2: string label} service Foo
            #
            # load(one, module_name="Foo_thrift").Req -> ['count', 'name']
            # load(two, module_name="Foo_thrift").Req -> ['count', 'name']   ← 错！应是 amount/label
            # 两个模块对象是同一个吗: True
            # ```
            #
            # 后果：字段名相同时**静默用错类型编码**（把 int 写进 string 字段）；
            # 字段名不同时用户拿到的是**另一个 IDL** 的字段列表，报错完全无法定位。
            # 现在把 IDL 绝对路径（与 include_dirs）一起编进键，保证"同一个 IDL ⇒
            # 同一个模块、不同 IDL ⇒ 不同模块"。
            module_key = _thrift_module_key(self.thrift_file, self.include_dirs)
            logger.debug(
                "init thrift module: thrift_file=%s, module_name=%s",
                thrift_file,
                module_key,
            )
            self.thrift_module = thriftpy2.load(
                self.thrift_file,
                module_name=module_key,
                include_dirs=self.include_dirs,
            )
            self.thrift_service_obj = getattr(self.thrift_module, self.service_name)
            logger.debug(
                "init thrift client: service_name=%s, ip=%s, port=%s",
                self.thrift_service_obj,
                ip,
                port,
            )
            self.client = make_client(
                self.thrift_service_obj,
                self.ip,
                int(self.port),
                # NOTICE（批次 3 / H2）：**单位换算在这一行**，别删。
                #
                # 本框架的 `timeout` 一直是**秒**（`models.TConfigThrift.timeout` 的注释、
                # `RunThriftRequest.with_timeout` 的文档、构造函数的默认值都这么写），
                # 而 thriftpy2 的 `make_client(timeout=...)` 把它**原样**交给
                # `TSocket(socket_timeout=timeout)`，`TSocket.__init__` 里做的是
                # `socket_timeout / 1000` —— 也就是**毫秒**：
                #   thriftpy2/transport/socket.py:33  "@param socket_timeout  socket timeout in ms"
                #   thriftpy2/transport/socket.py:51  self.socket_timeout = socket_timeout / 1000
                #   thriftpy2/rpc.py:24,52-53         timeout: int = 3000 -> TSocket(socket_timeout=timeout)
                # 所以这里必须 ×1000（顺带：`connect_timeout` 未显式传时由 TSocket 取同一个值，
                # 因此建连超时也一起变对）。
                #
                # 修复前直接透传：默认 10 变成 **10 毫秒**，任何回包超过 10 ms 的服务都以
                # `TimeoutError: timed out` 失败，而**报错里没有任何 timeout 数值**，
                # 排查时根本看不出是超时配置的问题。
                # 真机实测（thriftpy2 0.7.1 + 一个「接受连接但永不回数据」的 TCP 服务）：
                #   timeout=10   -> 0.018s 就超时（若单位是秒应在 10s 之后）
                #   timeout=3000 -> 3.002s 超时（正说明底层按毫秒解释）
                #
                # 边界：`timeout=0` 会得到 `TSocket(socket_timeout=0)`，而 TSocket 用
                # `if socket_timeout` 判空 → **不设超时（等于无限等待）**；修复前后一致，
                # 未改变语义（写 0 的用例本来就表示"不限时"）。
                timeout=self.timeout * 1000,
                proto_factory=get_proto_factory(self.proto_type),
                trans_factory=get_trans_factory(self.trans_type),
            )
        except Exception as e:
            self.thrift_module = None
            self.thrift_service_obj = None
            self.client = None
            logger.exception("init thrift module and client failed: {}".format(e))
            # NOTICE（0919-8 / §三.2）：修复前这里**只记日志、不重抛**，于是：
            #   1) 构造函数「成功」返回一个 client 为 None 的半成品对象；
            #   2) `run_step_thrift_request` 继续往下走，**setup hooks 先被调用**；
            #   3) 直到 `send_request` 才炸出
            #      `AttributeError: 'NoneType' object has no attribute 'ping_args'`
            #      —— 这条信息里既没有 IDL 路径，也没有真正的根因
            #      （IDL 路径写错 / IDL 语法错误 / service_name 不在 IDL 里 /
            #      include_dirs 少了被 include 的 .thrift / **建连失败**），与「钩子已经跑过、
            #      前序请求已经发出去」完全脱节，排查成本极高。
            # 现在按本仓「宁可报错，也不让它烂到运行期」的口径：在**构造点**抛，
            # 并把「配了什么」和「根因是什么」一起说清楚；原始异常用 `from e`
            # 保留在异常链里，traceback 里依然能看到 FileNotFoundError 等真实类型。
            # 刻意接受的副作用：失败不再发生在 setup hooks 之后——构造即失败，
            # 与 `RunThriftRequest.__init__` 的平台检查（0916-8 fail fast）同一口径。
            raise exceptions.ParamsError(
                "初始化 thrift client 失败（IDL 加载 / service 解析 / 建连）。\n"
                f"  配置: idl_path={self.thrift_file!r}, "
                f"include_dirs={self.include_dirs!r}, "
                f"service_name={self.service_name!r}, "
                f"ip:port={self.ip}:{self.port}\n"
                f"  根因: {type(e).__name__}: {e}\n"
                "  请依次检查：\n"
                "  1) idl_path 指向的文件是否存在（相对路径的基准是**运行进程的当前目录**，"
                "建议写绝对路径）；\n"
                "  2) IDL 本身的语法是否正确、`include` 进来的其它 .thrift 是否都在 "
                "include_dirs 里；\n"
                f"  3) service_name 必须在 IDL 里有同名的 `service` 声明"
                f"（当前是 {self.service_name!r}）；\n"
                f"  4) 网络/服务侧：{self.ip}:{self.port} 上是否有 thrift 服务在监听"
                "——`thriftpy2.make_client` 会**立刻建连**（真机实测：rpc.py 里直接调 "
                "`transport.open()`），连不上就在这里失败，而不是等到发请求。\n"
                "  NOTICE: 修复前这里不报错——会带着一个 client 为 None 的对象继续执行"
                "（此时 setup hooks 已经跑完），直到发请求时才抛出与根因无关的 "
                "`AttributeError: 'NoneType' object has no attribute '..._args'`。"
            ) from e
        finally:
            thriftpy2.parser.parser.thrift_stack = []

    def get_client(self):
        return self.client

    def _resolve_request_struct_class(self, request_method):
        """取出「方法唯一那个 struct 参数」的类；取不到时给出**可执行**的报错。

        NOTICE（0919-10 / 批次 C，§六.1）：`send_request` 一直是这么取请求结构体的：

            getattr(service_obj, method + "_args").thrift_spec[1][2]

        也就是「方法第 1 个参数的嵌套类型」。真机实测（thriftpy2 0.7.1 + 真实 IDL）
        这条路径**只对「方法唯一参数是 struct」的写法成立**，其余写法会抛出与真实原因
        完全脱节的错误：

        | IDL 写法 | `thrift_spec` | 修复前 |
        | --- | --- | --- |
        | `Blob get_blob(1: BlobReq req)` | `{1: (12, 'req', <class BlobReq>, False)}` | ✅ 正常 |
        | `string ping(1: string message)` | `{1: (11, 'message', False)}` → `[1][2]` 是 `False` | ❌ `AttributeError: 'bool' object has no attribute 'thrift_spec'` |
        | `string ping()`（无参） | `{}` | ❌ `KeyError: 1` |
        | `i32 add(1: i32 a, 2: i32 b)` | 两个字段 | ❌ 调用时参数个数不匹配 |
        | 方法名写错 | 没有 `xxx_args` 属性 | ❌ `AttributeError: ... has no attribute 'xxx_args'` |

        本批**不**扩展支持（那要重写请求编码），而是把这四类各给一条点名原因的错误
        —— 与 §三.2 同一口径：宁可报错，也不让用户看到一条与根因无关的信息。
        """
        if self.thrift_service_obj is None:
            raise exceptions.ParamsError(
                "thrift service 未初始化成功，无法解析请求结构体；"
                "client 构造阶段本应已报错，请检查 idl_path/service_name。"
            )

        args_cls = getattr(self.thrift_service_obj, request_method + "_args", None)
        available = sorted(
            name[: -len("_args")]
            for name in dir(self.thrift_service_obj)
            if name.endswith("_args")
        )
        if args_cls is None or not hasattr(args_cls, "thrift_spec"):
            raise exceptions.ParamsError(
                f"IDL 的 service {self.service_name!r} 里没有方法 {request_method!r}"
                f"（找不到 {request_method}_args）。\n"
                f"  可用的方法：{available}\n"
                f"  hint: `with_method(...)` 里写的是 IDL 中的方法名，注意大小写。"
            )

        spec = args_cls.thrift_spec or {}
        if not spec:
            raise exceptions.ParamsError(
                f"thrift 方法 {request_method!r} **没有参数**，本框架目前不支持这类方法。\n"
                "  NOTICE: 本框架的 thrift step 只支持「**方法唯一参数是 struct**」的 IDL，"
                "例如 `Blob get_blob(1: BlobReq req)`；请求体由 `with_params(...)` 提供。\n"
                "  可选做法：给 IDL 方法包一个 struct 参数，或改用 HTTP step。"
            )
        if len(spec) > 1:
            params = [field[1] for field in spec.values() if field]
            raise exceptions.ParamsError(
                f"thrift 方法 {request_method!r} 有 **{len(spec)} 个参数**"
                f"（{params}），本框架只支持**单个 struct 参数**的方法。\n"
                "  NOTICE: 框架只把 `with_params(...)` 的内容编成**一个**参数传给方法，"
                "参数个数不匹配时底层会抛出一条与真实原因无关的 TypeError。\n"
                "  可选做法：把多个参数合并成一个 struct；或改用 HTTP step。"
            )

        # NOTICE（批次 8 / **L11**）：**不能**写死 `spec[1]`。thrift 的 field id 由 IDL 作者
        # 指定，`Blob get_blob(2: BlobReq req)` 是**完全合法**的；修复前 `spec[1]` 在 id ≠ 1 时
        # 直接抛裸 `KeyError: 1`（实测 `.tmp_report/probe_l11_l12.py`：struct 参数 id=2 与
        # 标量参数 id=3 两种形态都只得到这一句），与本函数要消灭的「与根因无关的报错」同族。
        # 语义上这里要的是「方法**唯一**那个参数」（多参已在上面被 `len(spec) > 1` 拦掉），
        # 所以按**唯一字段**取，不关心它的 id 是多少。
        field = next(iter(spec.values()))
        nested = field[2] if len(field) > 2 else None
        if not hasattr(nested, "thrift_spec"):
            raise exceptions.ParamsError(
                f"thrift 方法 {request_method!r} 的第 1 个参数 {field[1]!r} 不是 struct"
                f"（ttype={field[0]}），本框架不支持。\n"
                "  NOTICE: 本框架的 thrift step 只支持「**方法唯一参数是 struct**」的 IDL，"
                "例如 `Blob get_blob(1: BlobReq req)`，请求体由 `with_params(...)` 提供；"
                "标量参数的写法（如 `string ping(1: string message)`）在修复前会抛"
                " `AttributeError: 'bool' object has no attribute 'thrift_spec'`。\n"
                "  可选做法：给该参数换成 struct；或改用 HTTP step。"
            )
        return nested

    def send_request(self, request_data, request_method=""):
        # NOTICE（批次 8 / **L12**）：`close()` 会把 `self.client` 置空（这是"重复 close 安全"的
        # 实现方式），而修复前这里**没有判空** → 复用已关闭的 client 时抛裸
        # `AttributeError: 'NoneType' object has no attribute 'get_blob'`
        # （实测 `.tmp_report/probe_l11_l12.py`），看不出"其实是 client 已经被关了"。
        # 最典型的现场：client 是 `with_thrift_client(...)` **外部注入**的，而 runner 在
        # **每个用例结束时**都会调用它的 `close()`（`runner.py` 释放长连接那段）——
        # 下一个用例复用同一个对象就会走到这里。现在给一条点名原因 + 可选做法的错误。
        if getattr(self, "client", None) is None:
            raise exceptions.ParamsError(
                "thrift client 已经关闭，不能再发请求（`close()` 之后这个对象不可复用）。\n"
                "  常见原因：client 是经 `with_thrift_client(...)` **外部注入**的，"
                "而 runner 在**每个用例结束时**都会关闭它（释放长连接）——"
                "下一个用例再复用同一个对象就会走到这里。\n"
                "  可选做法：① 每个用例各注入一个新 client；"
                "② 需要跨用例复用连接时，自己管理连接生命周期（不要用注入的 client）；"
                "③ 单个用例内的多次 thrift 调用不受影响。"
            )
        thrift_req_cls = self._resolve_request_struct_class(request_method)
        request_obj = json2thrift(json.dumps(request_data), thrift_req_cls)
        logger.debug(
            "send thrift request: request_method=%s, request_obj=%s",
            request_method,
            request_obj,
        )
        response_obj = getattr(self.client, request_method)(request_obj)
        logger.debug("thrift response = %s", response_obj)
        return thrift2dict(response_obj)

    def close(self) -> None:
        """关闭 thrift 连接（幂等）。

        NOTICE: 修复前没有 close()，连接只能等解释器退出时由 __del__ 关闭；而 __del__ 里
        直接 ``self.client.close()``，在初始化失败（``client`` 为 None，见 __init__ 的
        except 分支）时会抛 AttributeError，只留下 "Exception ignored in: ..." 这种
        无法排查的噪音。这里统一走 close()，未初始化成功时直接返回。
        """
        client = getattr(self, "client", None)
        if client is None:
            return

        try:
            client.close()
        except Exception as ex:
            logger.debug(f"failed to close thrift client: {ex}")
        finally:
            # 置空以便重复调用安全，也避免 runner 释放后又被误用
            self.client = None

    def __del__(self):
        # NOTICE: __del__ 可能在解释器退出阶段被调用（此时模块全局已被清理），
        # 抛出的异常无人处理、只会打印 "Exception ignored in"，因此全部吞掉。
        try:
            self.close()
        except Exception:
            pass
