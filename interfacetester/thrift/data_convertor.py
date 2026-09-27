# -*- coding: utf-8 -*-

from __future__ import division

import json
import traceback
import re
import logging
import base64

from thrift.Thrift import TType

from interfacetester.exceptions import ParamsError

# NOTICE（0919-15 / 登记项③）：bytes 的 JSON 口径**收口到 utils**（数据库 BLOB 列共用），
# 这里 import 进来后再以同名函数对外暴露，保证「只有一份实现」。
from interfacetester.utils import json_safe_bytes as _json_safe_bytes

try:
    from _json import encode_basestring_ascii as c_encode_basestring_ascii
except ImportError:
    c_encode_basestring_ascii = None

ESCAPE = re.compile(r'[\x00-\x1f\\"\b\f\n\r\t]')
ESCAPE_ASCII = re.compile(r'([\\"]|[^\ -~])')
HAS_UTF8 = re.compile(r"[\x80-\xff]")
ESCAPE_DCT = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}
for i in range(0x20):
    ESCAPE_DCT.setdefault(chr(i), "\\u{0:04x}".format(i))
    # ESCAPE_DCT.setdefault(chr(i), '\\u%04x' % (i,))



def istext(s_input):
    """
    既然我们要判断这串内容是不是可以做为Json的value,那为什么不放下试试呢？
    :param s_input:
    :return:

    NOTICE（0919-8 / §三.6b）：这个函数是 **python2 语义**的（py2 里 `str` 就是 bytes，
    所以 `not isinstance(s, bytes)` 意思是「是 unicode 文本」）。py3 下 `str` 永远不是
    `bytes`，因此所有形如 `type(v) in [str] and not istext(v)` 的判据**恒假**。
    它现在**不再**参与 binary 字段的判定（判定已改为按值的类型收口，见 json_safe_bytes），
    仅保留函数本身与 `__main__` 的示例，避免破坏可能的第三方引用。
    """
    return not isinstance(s_input, bytes)


# NOTICE（0919-8 / §三.6b —— thrift `binary` 字段的字节口径，修复记录留在这里）
#
# 修复前 `ThriftJSONEncoder.default` 写的是 `str(o, encoding="utf-8")`：只要 thrift 响应里
# 有一个**非 UTF-8** 的 binary 字段，整个 step 就会以
# `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0` 收场——
# 报错里既没有字段名，也没有「这是 binary」的线索。而 bytes 确实会到这里：
# 真机实测（thriftpy2 0.7.1 + 真实 IDL）`Blob.thrift_spec == {1: (18, 'payload', False)}`，
# `read_val` 对 `TType.BINARY`(18) **直接 `return inbuf.read(sz)`**；`TType.STRING`(11)
# 在 `strict_decode=False` 下解码失败也返回 bytes。
#
# 口径（为什么按「可解码性」分流，而不是一律 base64）：
#   - 能按 UTF-8 解码 → 给文本：报告可读，且保持 0918-8 / M28 已经钉住的
#     `thrift2dict(b"pong") == "pong"` 行为不变（不会把现在通过的用例改红）；
#   - 不能解码 → base64：真正的二进制载荷，base64 是唯一无损选择，而且**看得见**。
# 已知代价：base64 文本与「恰好长得像 base64 的普通字符串」无法区分（要区分得改模型，
# 如 `{"$binary": "..."}`，属独立立项）。
#
# NOTICE（0919-15 / 登记项③）：实现**收口到 `utils.json_safe_bytes`**（数据库 `BLOB` 列
# 要用同一口径，而本模块顶部要 import Apache `thrift`，不能让 DB 路径经由此处）。
# 这里是**同一个对象的别名**（不是包装函数）——这样「只有一份实现」可以被身份断言钉住。
json_safe_bytes = _json_safe_bytes


def unicode_2_utf8_keep_native(para):
    # NOTICE: 这是 python2 时代的编码转换残留，python3 里 str 本身就是 unicode，
    # 主体逻辑已退化为「按容器递归拷贝」。保留函数与既有返回语义，仅清理死代码。
    if type(para) is str:
        return para

    if type(para) is list:
        for i in range(len(para)):
            para[i] = unicode_2_utf8_keep_native(para[i])
        return para
    elif type(para) is dict:
        newpara = {}
        for (key, value) in para.items():
            key = unicode_2_utf8_keep_native(key)
            value = unicode_2_utf8_keep_native(value)
            newpara[key] = value
        return newpara
    elif type(para) is tuple:
        return tuple(unicode_2_utf8_keep_native(list(para)))
    else:
        # NOTICE: 原实现在这里还有一个 `elif type(para) is str: return para.encode("utf-8")`，
        # 但函数开头已经对 str 直接 return，该分支永远不可达（且 py3 下 encode 后类型会变），已删除。
        if isinstance(para, dict):
            # dict 的子类（上面的 type(para) is dict 不覆盖）→ 转成普通 dict 再递归
            logging.debug("unicode_2_utf8_keep_native: dict subclass %s", type(para))
            return unicode_2_utf8_keep_native(dict(para))
        else:
            return para


def encode_basestring(s):
    """Return a JSON representation of a Python string"""

    def replace(match):
        return ESCAPE_DCT[match.group(0)]

    return '"' + ESCAPE.sub(replace, s) + '"'


def py_encode_basestring_ascii(s):
    """Return an ASCII-only JSON representation of a Python string

    NOTICE（0919-17 / ①）：删掉 py2 时代的解码分支。原写法

        if isinstance(s, str) and HAS_UTF8.search(s) is not None:
            s = s.decode("utf-8")

    是 py2 语义的残留：py2 的 `str` 就是 bytes，先 decode 成 unicode 再转义；
    py3 的 `str` **没有** `.decode`，于是任何含 U+0080~U+00FF 码点的串都会在这里抛
    `AttributeError: 'str' object has no attribute 'decode'`
    （`HAS_UTF8 = [\\x80-\\xff]` 按**码点**匹配——注意 `café` 的 é=U+00E9 命中，
    而中文 U+4E2D 反而**不**命中，这个范围是 Latin-1 补充区）。

    为什么主路径一直没炸：CPython 上 `c_encode_basestring_ascii`（C 加速器）恒为真，
    本函数只在**没有 `_json` C 模块的解释器**（如 PyPy）里被下面的
    `encode_basestring_ascii = ... or ...` 选中——崩溃条件是
    「非 CPython + ensure_ascii + 值含 Latin-1 区字符」三者同时成立。

    修法与 py3 标准库 `json.encoder.py_encode_basestring_ascii` **对齐**（它没有这个分支）：
    下面的 ESCAPE_ASCII（`[^\\ -~]`）本就会把非 ASCII 码点转成 `\\uXXXX`，
    不需要预先 decode。与标准库的逐字节一致由测试钉住（oracle 断言，
    `tests/step_thrift_request_test.py::TestPyEncodeBasestringAsciiPy3Fallback`）。
    """

    def replace(match):
        s = match.group(0)
        try:
            return ESCAPE_DCT[s]
        except KeyError:
            n = ord(s)
            if n < 0x10000:
                return "\\u{0:04x}".format(n)
                # return '\\u%04x' % (n,)
            else:
                # surrogate pair
                n -= 0x10000
                s1 = 0xD800 | ((n >> 10) & 0x3FF)
                s2 = 0xDC00 | (n & 0x3FF)
                return "\\u{0:04x}\\u{1:04x}".format(s1, s2)
                # return '\\u%04x\\u%04x' % (s1, s2)

    return '"' + str(ESCAPE_ASCII.sub(replace, s)) + '"'


encode_basestring_ascii = c_encode_basestring_ascii or py_encode_basestring_ascii


# NOTICE（批次 D / T1）：thriftpy2 给 `binary` 字段的 ttype 是 **18**，而本模块顶部 import 的
# 是 **Apache thrift** 的 `TType` —— 实测 0.24.0 里 `hasattr(TType, "BINARY") is False`
# （原作者注释里写的 `TType.BINARY` 正是撞了这个 AttributeError 才被改成 `TType.BYTE`(3)，
# 于是 binary 字段永远匹配不到）。所以这里写**字面量**并起个名字，别再去找 `TType.BINARY`。
# 真机形状（thriftpy2 0.7.1，`.tmp_audit/idl/t1t2.thrift`）：
#     1: (18, 'payload', False)                  # binary —— **3 元组**，没有 ttype_info 位
#     2: (15, 'blobs', 18, False)                # list<binary> —— 元素类型是裸 int
_THRIFT_BINARY_TTYPE = 18

# NOTICE（批次 D / T3）：报错与告警里要点名「IDL 类型」，而不是只说一个数字。
# 取值来自 Apache thrift 的 `TType`（实测 0.24.0），18 是 thriftpy2 侧的 binary。
_THRIFT_TYPE_NAMES = {
    1: "void",
    2: "bool",
    3: "byte",
    4: "double",
    6: "i16",
    8: "i32",
    10: "i64",
    11: "string",
    12: "struct",
    13: "map",
    14: "set",
    15: "list",
    18: "binary",
}


def _thrift_type_name(ttype):
    return _THRIFT_TYPE_NAMES.get(ttype, f"ttype={ttype}")


# NOTICE（批次 D / T3）：bool 字段拿到的**字符串**只看这两种形态才告警 ——
#   - 看起来是「假」的写法（`"false"`/`"no"`/`"off"`/`"0"`…）→ Python 结果是 **True**，与直觉相反；
#   - 根本不是布尔词汇的字符串（`"abc"`）→ 用户想表达什么无从判断；
# 而 `"true"`/`"yes"`/`"1"`/空串 这些形态 Python 的结果与直觉一致，**不告警**
# （本仓的原则是「只拦能精确判定的形态」—— 假报会让真报被无视）。
_BOOL_TRUTHY_TEXTS = {"true", "t", "yes", "y", "on", "1"}
_BOOL_FALSEY_TEXTS = {"false", "f", "no", "n", "off", "0"}


def _struct_field_map(thrift_spec):
    """`thrift_spec` → `{字段名: (ttype, ttype_info)}`（跳过 `None` 占位项）。

    真机形状（thriftpy2 0.7.1，实测 `.tmp_audit/idl/t1t2.thrift` 的 `Req`）::

        1:  (18, 'payload', False)                      # binary（3 元组，无 ttype_info）
        3:  (11, 'message', False)                      # string
        9:  (13, 'codes', (8, 11), False)               # map<i32, string>
        10: (15, 'items', (12, <class Inner>), False)   # list<struct>
        11: (12, 'inner', <class Inner>, False)         # struct

    即「3 元组 = 基础类型（无 ttype_info）」与「4 元组 = 带 ttype_info」两种形态并存。
    """
    fields = {}
    for field in (thrift_spec or {}).values():
        if field is None:
            continue
        if len(field) <= 3:
            field_ttype, field_name, field_ttype_info = field[0], field[1], None
        else:
            field_ttype, field_name, field_ttype_info = field[0], field[1], field[2]
        fields[field_name] = (field_ttype, field_ttype_info)
    return fields


class ThriftJSONDecoder(json.JSONDecoder):
    def __init__(self, *args, **kwargs):
        self._thrift_class = kwargs.pop("thrift_class")
        super(ThriftJSONDecoder, self).__init__(*args, **kwargs)

    def decode(self, json_str):
        if isinstance(json_str, dict):
            dct = json_str
        else:
            dct = super(ThriftJSONDecoder, self).decode(json_str)
        return self._convert(
            dct,
            TType.STRUCT,
            # (self._thrift_class, self._thrift_class.thrift_spec))
            self._thrift_class,
        )

    def _convert(self, val, ttype, ttype_info, path=""):
        """把 JSON 值转成 thrift struct 实例（**请求方向**，只被 `json2thrift` 用到）。

        NOTICE（批次 D / T1 + T2）：本方法此前有两处「静默」，已在对应分支就地注明：
          - **T2**：STRUCT 只按 IDL 的 `thrift_spec` 遍历，于是 JSON 里**多出来的键**
            被静默丢弃（`if val is None or field_name not in val: continue`）——
            请求照发、服务端拿到 `None`、用例还可能因为没断这个字段而「通过」；
          - **T1**：`binary` 字段（thriftpy2 的 ttype = **18**）没有分支，直接落到末尾的
            `TypeError: Unrecognized thrift field type: 18`，而且是在**建连与 setup hooks
            之后**才炸（报错里没有字段名）。

        `path` 只为**报错**服务（如 `req.items[0].messag`），不参与任何转换语义；
        默认值保证既有调用方（含测试）不受影响。
        """
        if ttype == TType.STRUCT:
            if val is None:
                ret = None
            else:
                thrift_class = ttype_info
                fields = _struct_field_map(ttype_info.thrift_spec)
                where = path or "<顶层>"
                if not isinstance(val, dict):
                    raise ParamsError(
                        f"thrift 参数 {where} 期望是 JSON 对象（{dict.__name__}），"
                        f"实际是 {type(val).__name__}: {val!r}\n"
                        f"  struct: {thrift_class.__name__}"
                        f"（IDL 里的字段：{sorted(fields)}）"
                    )
                # NOTICE（批次 D / T2）：**先拦未知字段，再转换**。判据是「键的集合」，
                # 与 IDL 完全对齐（大小写敏感、不认识就报错、不做模糊匹配）。
                unknown = [key for key in val if key not in fields]
                if unknown:
                    written = "、".join(f"{key}={val[key]!r}" for key in unknown)
                    raise ParamsError(
                        f"thrift 参数里有 IDL 中不存在的字段：{unknown}\n"
                        f"  位置: {where}\n"
                        f"  struct: {thrift_class.__name__}"
                        f"（IDL 里的字段：{sorted(fields)}）\n"
                        f"  你写的是: {written}\n"
                        "  NOTICE: 字段名必须与 IDL **完全一致**。修复前这类键会被**静默丢弃**——"
                        "请求照发、服务端收到的是 None，而用例只要没断这个字段就照样「通过」"
                        "（实测 `with_params(messag='hi')`：服务端 message=None、用例绿、"
                        "`summary.success=true`），而请求日志打印的是**你自己写的 params**，"
                        "看上去一切正常。"
                    )
                ret = thrift_class()
                for field_name, (field_ttype, field_ttype_info) in fields.items():
                    if field_name not in val:
                        continue
                    child_path = f"{path}.{field_name}" if path else field_name
                    setattr(
                        ret,
                        field_name,
                        self._convert(
                            val[field_name], field_ttype, field_ttype_info, child_path
                        ),
                    )
        elif ttype == TType.LIST:
            if type(ttype_info) != tuple:  # 说明是基础类型了, 无法在细分
                (element_ttype, element_ttype_info) = (ttype_info, None)
            else:
                (element_ttype, element_ttype_info) = ttype_info
            if val is not None:
                ret = [
                    self._convert(x, element_ttype, element_ttype_info, f"{path}[{i}]")
                    for i, x in enumerate(val)
                ]
            else:
                ret = None

        elif ttype == TType.SET:
            if type(ttype_info) != tuple:  # 说明是基础类型了, 无法在细分
                (element_ttype, element_ttype_info) = (ttype_info, None)
            else:
                (element_ttype, element_ttype_info) = ttype_info
            if val is not None:
                ret = set(
                    [
                        self._convert(
                            x, element_ttype, element_ttype_info, f"{path}[{i}]"
                        )
                        for i, x in enumerate(val)
                    ]
                )
            else:
                ret = None

        elif ttype == TType.MAP:
            # key处理
            if type(ttype_info[0]) == tuple:
                key_ttype, key_ttype_info = ttype_info[0]
            else:
                key_ttype, key_ttype_info = ttype_info[0], None

            # value处理
            if type(ttype_info[1]) != tuple:  # 说明value为基础类型, 已不可在细分
                val_ttype = ttype_info[1]
                val_ttype_info = None
            else:
                val_ttype, val_ttype_info = ttype_info[1]

            if val is not None:
                ret = dict(
                    [
                        (
                            self._convert(
                                k, key_ttype, key_ttype_info, f"{path}[{k!r}].key"
                            ),
                            self._convert(
                                v, val_ttype, val_ttype_info, f"{path}[{k!r}]"
                            ),
                        )
                        for (k, v) in val.items()
                    ]
                )
            else:
                ret = None
        elif ttype == TType.STRING:
            if isinstance(val, str):
                ret = val.encode("utf8")
            elif val is None:
                ret = None
            else:
                # NOTICE（批次 D / T3）：**行为不变，只告警** —— 非字符串的值仍然走
                # `str(val)`（修复前就是这样），但结果会作为**文本**发给服务端，
                # 而 dict/list 会变成 `"{'a': 1}"` 这种 Python repr，几乎必然是笔误。
                if isinstance(val, (bytes, bytearray, memoryview)):
                    self._warn_scalar_coercion(
                        "string-from-bytes",
                        ttype,
                        path,
                        val,
                        str(val),
                        "bytes 进 string 字段会先被 `str()` 成 `\"b'…'\"` 这种文本"
                        "（带 b 前缀与引号）。要发原始字节请把 IDL 该字段声明成 `binary`。",
                    )
                else:
                    self._warn_scalar_coercion(
                        "string-from-non-text",
                        ttype,
                        path,
                        val,
                        str(val),
                        "非字符串的值会被 `str()` 成文本发出去（dict/list 会是 Python repr，"
                        "如 `\"{'a': 1}\"`）。改法：在 JSON/YAML 里加引号写成字符串。",
                    )
                ret = str(val)
            # 判断string字段是否是base64编码后的string, 如果是则此处需要对该string字段进行b64decode, 还原成原本的字符串
            # todo : 留待实现

        elif ttype == _THRIFT_BINARY_TTYPE:
            # NOTICE（批次 D / T1）：**请求方向的 binary 字段**（响应方向早已按值类型收口，
            # 见 `json_safe_bytes` 与 `ThriftJSONEncoder`；这里补的是漏掉的那一半）。
            #
            # 修复前没有这个分支 → 落到末尾的
            # `TypeError: Unrecognized thrift field type: 18`，而这个错误：
            #   ① 不含字段名，只说「类型 18 不认识」；
            #   ② 发生在**建连与 setup hooks 之后**（真机实测：`make_client` 已调用、
            #      hook 已执行），前序副作用已经发生；
            #   ③ `.run.log` 到 hook 就断了，看不出是哪个字段。
            #
            # 口径（与 `TType.STRING` 分支一致）：JSON 字符串 → UTF-8 字节；
            # bytes/bytearray/memoryview 原样（`send_request` 会先 `json.dumps`，所以这条
            # 只对直接调 `json2thrift` 的调用方有意义）。**其余类型一律报错**——
            # 不像 STRING 分支那样 `str(val)` 兜底：把 `123` 或一个 dict 悄悄变成
            # `b"{'a': 1}"` 发出去，正是本仓最忌讳的「静默错值」。
            #
            # ⚠️ 如实登记的**限制**：真正的二进制载荷（图片/序列化对象）目前**没有**写法——
            # `with_params(...)` 的内容必须先过 `json.dumps`，而 JSON 里没有 bytes。
            # 要支持它得先约定一种表示（如 `{"$base64": "..."}`），属**未支持项**
            # （与 `docs/架构与调用链.md` §7 第 39 行同族的「口径收口」待办，需单独立项）。
            if val is None:
                ret = None
            elif isinstance(val, str):
                ret = val.encode("utf8")
            elif isinstance(val, (bytes, bytearray, memoryview)):
                ret = bytes(val)
            else:
                raise ParamsError(
                    f"thrift 的 binary 字段 {path or '<顶层>'} 只接受字符串"
                    f"（按 UTF-8 编码成字节）或 bytes，实际是 "
                    f"{type(val).__name__}: {val!r}\n"
                    "  NOTICE: 修复前 binary 字段会让整个 step 以 "
                    "`TypeError: Unrecognized thrift field type: 18` 收场"
                    "（报错里既没有字段名，也发生在建连与 setup hooks 之后）。\n"
                    "  ⚠️ 限制：**真正的二进制载荷**（图片、protobuf 等）目前没有可用写法——"
                    "`with_params(...)` 的内容要先经过 `json.dumps`，而 JSON 里没有 bytes；"
                    "需要时请先用文本/base64 与对端约定，或改用 HTTP step。"
                )
        elif ttype == TType.DOUBLE:
            if val is not None:
                ret = self._to_float(val, ttype, path)
            else:
                ret = None
        elif ttype == TType.I64:
            if val is not None:
                ret = self._to_int(val, ttype, path)
            else:
                ret = None
        elif ttype == TType.I32 or ttype == TType.I16 or ttype == TType.BYTE:
            if val is not None:
                ret = self._to_int(val, ttype, path)
            else:
                ret = None
        elif ttype == TType.BOOL:
            if val is not None:
                # NOTICE（批次 D / T3）：**行为一个字不改**（仍是 `bool(val)`），
                # 只在两种「几乎必然是笔误」的形态上告警。口径与批次 C / L11 同源：
                # 只做能**精确判定**的提示，绝不硬拦（硬拦会打断现在能跑的用例）。
                if not isinstance(val, bool):
                    if isinstance(val, str):
                        text = val.strip().lower()
                        if text in _BOOL_FALSEY_TEXTS:
                            # 「看起来是假、实际为真」—— 这是 T3 记的那个现场
                            self._warn_scalar_coercion(
                                "bool-from-falsey-string",
                                ttype,
                                path,
                                val,
                                bool(val),
                                "这个写法在 Python 里是**非空字符串 = 真**："
                                "服务端收到的是 True（「关掉开关」变成「打开」的现场）。"
                                "改法：布尔字面量写 `false`/`true`（不要加引号）。",
                            )
                        elif text and text not in _BOOL_TRUTHY_TEXTS:
                            self._warn_scalar_coercion(
                                "bool-from-nonboolean-string",
                                ttype,
                                path,
                                val,
                                bool(val),
                                "这个字符串既不是 `true` 也不是 `false`，"
                                "Python 按「非空即真」处理 → 服务端收到 True。"
                                "改法：布尔字面量写 `false`/`true`（不要加引号）。",
                            )
                        # `"true"`/`"1"`/`"yes"`/空串 等形态**不告警**：
                        # Python 的结果与直觉一致，告警只会变成噪声（假报会让真报被无视）。
                    elif isinstance(val, (int, float)) and val not in (0, 1):
                        self._warn_scalar_coercion(
                            "bool-from-nonbinary-number",
                            ttype,
                            path,
                            val,
                            bool(val),
                            "布尔字段只认 `true`/`false`（0/1 也按 False/True 处理），"
                            "其余非零数字一律会变成 True。",
                        )
                ret = bool(val)
            else:
                ret = None
        else:
            raise TypeError("Unrecognized thrift field type: %s" % ttype)
        return ret

    # ------------------------------------------------------------------
    # 批次 D / T3：标量强转「过宽」的可见化（**只告警，不改行为**）
    #
    # 修复前的现场（真机实测，`.tmp_audit/out_real_thrift2.txt`）：
    #     {"flag": "false"}  -> 服务端收到 flag=True     （「关掉开关」变成「打开」）
    #     {"count": 3.9}     -> 服务端收到 count=3       （小数被**静默截断**）
    #     {"count": "3.7"}   -> 裸 `ValueError: invalid literal for int() with base 10: '3.7'`
    #                           （报错里既没有字段名，也没有位置）
    # 三者的共同点是：**没有任何信号**告诉用户「这个值被改过了」。
    #
    # 为什么选「只告警」而不是硬类型校验（用户拍板）：
    #   `count: "7"` 这种写法在 YAML/CSV/变量插值里很常见（`$count` 出来就是字符串），
    #   一刀切拒绝会打断现在能跑的用例；而 `"false"` → True 与 `3.9` → 3 这两类
    #   **一定会让服务端收到与用户意图不同的值**，值得立刻可见。
    #   判定只覆盖「能精确判定是笔误」的形态，不做类型洁癖 —— 假报会让真报被无视。
    # ------------------------------------------------------------------
    def _to_int(self, val, ttype, path):
        """`int()` 强转（行为与修复前一致）：失败时给**点名报错**，截断小数时告警。"""
        try:
            converted = int(val)
        except (TypeError, ValueError) as ex:
            raise self._conversion_error(val, ttype, path, "整数", ex) from ex
        if isinstance(val, float) and converted != val:
            self._warn_scalar_coercion(
                "int-truncates-float",
                ttype,
                path,
                val,
                converted,
                "小数部分被**截断**（不是四舍五入）。"
                "改法：想发小数就把 IDL 字段改成 `double`；想要整数就直接写整数。",
            )
        return converted

    def _to_float(self, val, ttype, path):
        """`float()` 强转（行为与修复前一致）：失败时给点名报错。"""
        try:
            return float(val)
        except (TypeError, ValueError) as ex:
            raise self._conversion_error(val, ttype, path, "小数", ex) from ex

    @staticmethod
    def _conversion_error(val, ttype, path, expected, ex):
        return ParamsError(
            f"thrift 参数 {path or '<顶层>'} 的值无法转成{expected}"
            f"（IDL 类型 {_thrift_type_name(ttype)}）：{val!r}（{type(val).__name__}）\n"
            f"  根因: {type(ex).__name__}: {ex}\n"
            "  NOTICE: 修复前这里抛的是**裸** `ValueError: invalid literal for int() …`"
            "——报错里既没有字段名、也没有位置，看不出是哪个参数写错了。"
        )

    def _warn_scalar_coercion(self, kind, ttype, path, val, converted, fix):
        """标量强转告警：**每次请求内同类只报一次**（避免 `list<i32>` 里刷屏）。"""
        warned = getattr(self, "_coercion_warnings", None)
        if warned is None:
            warned = self._coercion_warnings = set()
        key = (ttype, kind)
        if key in warned:
            return
        warned.add(key)

        from loguru import logger  # noqa: PLC0415

        logger.warning(
            f"thrift 参数 {path or '<顶层>'} 的值会被**强转**（行为不变，只提示）：\n"
            f"  IDL 类型: {_thrift_type_name(ttype)}\n"
            f"  你写的值: {val!r}（{type(val).__name__}）\n"
            f"  实际发送: {converted!r}\n"
            f"  {fix}\n"
            "  NOTICE: 同类告警每次请求只报一次；相关口径见 `docs/架构与调用链.md` §7。"
        )


def json2thrift(json_str, thrift_class):
    logging.debug(json_str)
    return json.loads(
        json_str, cls=ThriftJSONDecoder, thrift_class=thrift_class, strict=False
    )


def dumper(obj):
    try:
        return json.dumps(obj, default=lambda o: o.__dict__, sort_keys=True, indent=2)
    except Exception:
        # NOTICE: 修复前是裸 except:，会把 KeyboardInterrupt/SystemExit 一并吞掉
        return obj.__dict__


class ThriftJSONEncoder(json.JSONEncoder):
    """
    add by braver
    """

    def __init__(
        self,
        skipkeys=False,
        ensure_ascii=True,
        check_circular=True,
        allow_nan=True,
        indent=None,
        separators=None,
        default=None,
        sort_keys=False,
        **kw
    ):

        super(ThriftJSONEncoder, self).__init__(
            skipkeys=skipkeys,
            ensure_ascii=ensure_ascii,
            check_circular=check_circular,
            allow_nan=allow_nan,
            indent=indent,
            separators=separators,
            default=default,
            sort_keys=sort_keys,
        )
        self.skip_nonutf8_value = kw.get(
            "skip_nonutf8_value", False
        )  # 默认不skip忽略非utf-8编码的字段

    def encode(self, o):
        """Return a JSON string representation of a Python data structure.
         JSONEncoder().encode({"foo": ["bar", "baz"]})
        '{"foo": ["bar", "baz"]}'

        NOTICE（0918-8 / M28）：删掉了 python2 时代的 `_encoding = self.encoding` 分支。
        python3 的 `json.JSONEncoder` **没有** `encoding` 属性，而字符串走的正是这个分支，
        因此 thrift 方法返回字符串（如 `string ping()`）时 `thrift2dict()` 必然抛
        `AttributeError: 'ThriftJSONEncoder' object has no attribute 'encoding'`，
        整个 thrift step 直接失败；而 `int` / `bool` / `list` 等返回值走 C 编码器，反而正常
        （这也是该分支长期没被发现的原因）。
        py3 里 str 本身就是 unicode，不需要再 decode；此处与标准库
        `json.encoder.JSONEncoder.encode` 的实现对齐。
        """
        # This is for extremely simple cases and benchmarks.

        if isinstance(o, str):
            if self.ensure_ascii:
                return encode_basestring_ascii(o)
            else:
                return encode_basestring(o)
            # This doesn't pass the iterator directly to ''.join() because the
            # exceptions aren't as detailed.  The list call should be roughly
            # equivalent to the PySequence_Fast that ''.join() would do.
        chunks = self.iterencode(o, _one_shot=True)
        if not isinstance(chunks, (list, tuple)):
            chunks = list(chunks)
        # add by braver
        # todo: fix 'utf8' codec can't decode byte 0x91 in position 3: invalid start byte"
        if self.skip_nonutf8_value:  # 缺省为false
            tmp_chunks = []
            for chunk in chunks:
                try:
                    tmp_chunks.append(unicode_2_utf8_keep_native(chunk))
                except Exception as err:
                    logging.debug(traceback.format_exc())
            return "".join(tmp_chunks)

        # 保留老的逻辑, /usr/lib/python2.7/package/json/__init__.py dumps接口
        return "".join(chunks)

    def default(self, o):
        # NOTICE（0918-8 / M28）：`default` 是「非 struct 返回值」的兜底路径，
        # 与 `encode` 的 str 分支是同一个问题的两面（原来只有 bytes 一种非 struct
        # 标量能通过，其余都会抛 TypeError）：
        #   - `binary` 返回值可能是 bytearray；
        #   - thrift 的 `set<...>` 返回值在 py3 里是 set，json 不认识，
        #     这里按 struct 内 LIST/SET 字段的既有口径统一转成数组。
        if isinstance(o, (bytes, bytearray)):
            # NOTICE（0919-8 / §三.6b）：修复前是 `str(o, encoding="utf-8")`，
            # 非 UTF-8 的 binary 载荷会在这里抛 UnicodeDecodeError（见 json_safe_bytes）。
            return json_safe_bytes(o)
        if isinstance(o, (set, frozenset)):
            return list(o)
        if not hasattr(o, "thrift_spec"):
            return super(ThriftJSONEncoder, self).default(o)

        spec = getattr(o, "thrift_spec")
        ret = {}
        for tag, field in spec.items():
            if field is None:
                continue
            # (tag, field_ttype, field_name, field_ttype_info, default) = field
            field_name = field[1]
            default = field[-1]
            field_type = field[0]
            if field_name in o.__dict__:
                val = o.__dict__[field_name]
                # NOTICE（0919-8 / §三.6b）：这里原来有两段「把 binary 字段 base64 编码」
                # 的分支，**两段都不可能执行**，是 python2 时代的双重死代码：
                #   1) 判据写的是 `type(val) in [str] and not istext(val)`，而
                #      `istext(s) = not isinstance(s, bytes)` → 等价于「是 str 且是 bytes」，
                #      python3 里恒假（实测 str/bytes/bytearray/int/None 全部为假）；
                #   2) 外层 ttype 判据用的是 `TType.BYTE`(3)，而本模块的 TType 来自
                #      **Apache `thrift`** 包；真正代表 binary 的是 **thriftpy2** 的
                #      `TType.BINARY = 18`（thriftpy2 在 `read_val` 里对 18 直接
                #      `return inbuf.read(sz)` 给出 bytes，只有写线路时才映射成 STRING(11)）。
                #      所以即使 `[str]` 那一半成立，3 也永远匹配不到 binary 字段。
                #      （原作者注释写着 `[TType.STRING, TType.BINARY]`，应是先撞上
                #      `AttributeError: TType has no attribute BINARY` 才改成 BYTE 的。）
                # 现在**按值的类型收口**，不再猜 ttype：bytes/bytearray 一律经
                # `json_safe_bytes` 处理（struct 字段、list<binary>、set<binary>、
                # 裸返回值走的是同一条路径），既不会 UnicodeDecodeError，也不会漏编码。
                if field_type in [TType.LIST, TType.SET]:  # 数组类型
                    if val:  # val为非空数组/Set
                        val = list(val)  # 统一转成数组(list/set)
                # if val != default:
                ret[field_name] = val
        if "request_id" in o.__dict__:
            ret["request_id"] = o.__dict__["request_id"]
        if "rpc_latency" in o.__dict__:
            ret["rpc_latency"] = o.__dict__["rpc_latency"]
        return ret


def thrift2json(obj, skip_nonutf8_value=False):
    return json.dumps(
        obj,
        cls=ThriftJSONEncoder,
        ensure_ascii=False,
        skip_nonutf8_value=skip_nonutf8_value,
    )


def thrift2dict(obj):
    # NOTICE: 原实现用 `str` 当变量名，遮蔽了内置 str，已改名（行为不变）
    raw_json = thrift2json(obj)
    return json.loads(raw_json)


if __name__ == "__main__":
    print(istext("Всего за {$price$}, а доставка - бесплатно!"))
    print(istext(b"\xe4\xb8\xad\xe6\x96\x87"))
    print(
        istext(
            '{"web_uri":"ad-site-i18n-sg/202103185d0d723d88b7f642452dac73","height":336,"width":336,"file_name":""}'
        )
    )
