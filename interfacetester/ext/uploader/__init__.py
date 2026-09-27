""" upload test extension.

If you want to use this extension, you should install the following dependencies first.

- requests_toolbelt
- filetype

Then you can write upload test script as below:

    - test:
        name: upload file
        request:
            url: https://httpbin.org/upload
            method: POST
            headers:
                Cookie: session=AAA-BBB-CCC
            upload:
                file: "data/file_to_upload"
                field1: "value1"
                field2: "value2"
        validate:
            - eq: ["status_code", 200]

For compatibility, you can also write upload test script in old way:

    - test:
        name: upload file
        variables:
            file: "data/file_to_upload"
            field1: "value1"
            field2: "value2"
            m_encoder: ${multipart_encoder(file=$file, field1=$field1, field2=$field2)}
        request:
            url: https://httpbin.org/upload
            method: POST
            headers:
                Content-Type: ${multipart_content_type($m_encoder)}
                Cookie: session=AAA-BBB-CCC
            data: $m_encoder
        validate:
            - eq: ["status_code", 200]

"""

import io
import os
import uuid
from typing import Any, Dict, List, Optional, Set, Text

from interfacetester import exceptions
from interfacetester.models import VariablesMapping, FunctionsMapping, TStep
from interfacetester.parser import (
    function_regex_compile,
    parse_data,
    parse_function_params,
    variable_regex_compile,
)
from loguru import logger

try:
    import filetype

    UPLOAD_READY = True
except ModuleNotFoundError:
    UPLOAD_READY = False
    # NOTICE（0920 批次 3 / **N13**）：导入失败时**必须把这个名字显式绑成 None**。
    #
    # 修复前 `except` 分支只把 `UPLOAD_READY` 置 False，`filetype` / `MultipartEncoder`
    # 两个名字**根本没有被绑定**。于是在「没装 upload extra」的环境里，
    # `get_filetype()` 里那句 `filetype.guess(...)` 抛的是
    # `NameError: name 'filetype' is not defined` —— 与"依赖没装"完全不像，
    # 排查方向直接被带偏。实测（`.tmp_audit/n13_block_plugin.py`，模拟 CI 的 `.[dev,xml]`）：
    # `tests/uploader_test.py` **16 failed**，全部是这句 NameError。
    #
    # 现在绑成 None，`get_filetype()` 就能给出「依赖未安装 + 装法」的可读报错
    # （见那里的 `ensure_upload_ready()` 调用）。
    filetype = None

# NOTICE（0920 批次 6 / **N37**）：**`requests_toolbelt` 不再是硬前置**。
#
# 修复前这里是 `import filetype` + `from requests_toolbelt import MultipartEncoder`
# 一起 try，任一失败就 `UPLOAD_READY = False`。而 `MultipartEncoder`
# **在整个包里从未被使用**（L14 的流式改写之后，用的是自研的
# `Utf8MultipartEncoder`；全文搜索 `MultipartEncoder` 只剩注释与
# `utils.py` 的文档字符串）。也就是说：
#
# ```text
# filetype 装了、编码器完全可用，只因没装 requests_toolbelt
#   -> UPLOAD_READY = False
#   -> multipart_encoder 直接 RuntimeError「依赖未安装」（实测 .tmp_audit/n37_verify2.py）
# ```
#
# 用户被要求装一个框架根本不用的包，否则上传功能整个不可用。
# 现在只把**真正被使用**的 `filetype` 当作硬前置；
# `requests_toolbelt` 若存在则顺手 import（保持对老用户环境的兼容，
# 也避免"历史依赖突然消失"这种意外），**但它的缺席不再影响可用性**。
try:
    from requests_toolbelt import MultipartEncoder  # noqa: F401
except ModuleNotFoundError:
    MultipartEncoder = None  # 可选依赖：保留名字，避免引用处 NameError


def ensure_upload_ready():
    if UPLOAD_READY:
        return

    # NOTICE（0920 批次 6 / **N37**）：文案订正 —— 真正缺的只有 `filetype`
    # （`requests_toolbelt` 已不再是硬前置，见模块顶部的 NOTICE）。
    # 让用户装一个框架不用的包，是"错误信息把排查方向带偏"的典型形态。
    msg = """
    uploader extension dependency uninstalled, install first and try again.
    install with pip:
    $ pip install filetype

    or you can install interfacetester with optional upload dependencies:
    $ pip install "interfacetester[upload]"
    """
    logger.error(msg)
    # NOTICE（0918-4 / M7）：这里不能 sys.exit(1)——在 pytest 进程内直接退出会把整个
    # 测试会话杀掉，其余用例的结果全部丢失，用户只看到一个「进程退出」。
    # 抛异常则只让当前用例报错，其余用例继续跑。
    #（与 step_sql_request.ensure_sql_ready、step_thrift_request.ensure_thrift_ready、
    #  loader.load_debugtalk_functions 的同类修复保持一致；本项目里唯独这里漏改。）
    raise RuntimeError(
        "uploader extension dependencies uninstalled, "
        'install with: pip install "interfacetester[upload]"'
    )


def _looks_like_file_path(value) -> bool:
    """值是否「形似文件路径」——文件不存在时的告警判据（0919-4 / L37）。

    只认三类：绝对路径、含路径分隔符（`/` 或 `\\`）、`os.PathLike`。
    刻意**不**把「裸字符串」（`"123"`、`"value1"`、`"3.14"`）当路径：
    upload 字段本来就允许混写普通表单值（模块 docstring 的示例正是
    `file: 路径` 与 `field1: "value1"` 并存），按裸字符串告警会在合法用例上刷屏——
    告警一旦有假报，就会被维护者整体无视，等于没有。
    代价（如实登记）：不带分隔符的裸文件名（如 `typo.png`）写错时不在告警面内。
    """
    if isinstance(value, os.PathLike):
        return True
    if isinstance(value, bytes):
        return os.path.isabs(value) or b"/" in value or b"\\" in value
    return os.path.isabs(value) or "/" in value or "\\" in value


def _field_expression_is_parseable(name: str, reference: str) -> bool:
    """`<name>=<reference>` 作为函数参数时，parser 能**原样**还原出这个字段名吗？

    NOTICE：判据刻意直接借用 parser 自己的正则与参数解析器（`parse_function_params`），
    **不重写一份字符表**。重写会两头出错：
      - 比 parser 更严 → 拒掉本来能用的字段名（假报，用户没法用）；
      - 比 parser 更松 → 放过会静默出错的字段名（本批要消灭的就是这个）。
    最后那句 `meta["kwargs"].get(name) == reference` 是「**parser 必须逐字还原出这个名字**」
    的通用兜底：只要解析结果与我们要发的字段名有任何出入，就判为不可表达。
    （实测：字段名首尾的空白在更早的 `parse_data` 里就被 strip 掉了，
    所以走到这里时它与表达式里的名字必然一致 —— 这不是漏判，是上游已经归一。）
    """
    probe = "${multipart_encoder(" + f"{name}={reference}" + ")}"
    match = function_regex_compile.fullmatch(probe)
    if not match:
        return False

    try:
        meta = parse_function_params(match.group(2))
    except exceptions.ParamsError:
        return False

    return meta["kwargs"].get(name) == reference


def _plan_field_references(
    upload: VariablesMapping, step_variables: VariablesMapping
) -> Dict[str, str]:
    """为每个 upload 字段决定它在生成表达式里用哪个**变量引用**。

    正常情况就是 `$<字段名>`（逐字保持既有行为，零变化）。
    字段名不能当变量名时（`file-name` / `文件名` / `2file` …），改用生成的
    `$m_upload_N` 别名 —— **字段名本身照旧发出去**，只是取值的引用换成合法变量名。

    NOTICE（0919-7 / 顺带发现 ②，实测后**从「次要」升级为高**）：
    修复前是 `params_list.append(f"{key}=${key}")`，字段名被直接当变量引用。实测三种后果：

    | 字段名 | parser 是否认作变量引用 | 修复前后果 |
    |---|---|---|
    | `file` / `file_name` / `name文件` | ✅ | 正常 |
    | `file-name` / `file.name` / `file name` | ❌ 被切成 `$file` | 响亮报错，但点名的是**另一个**变量（`file not found`） |
    | `文件名` / `2file` / `文件-名` | ❌ 完全不匹配 | **静默**：值原样留字面量 `$文件名`，**文件根本没上传**，请求照发、用例可能通过 |

    第三行是零告警的假通过（比报错严重得多），所以本批不是「补个告警」而是**让它真的能用**。
    """
    references: Dict[str, str] = {}
    unrepresentable: List[Any] = []
    used_aliases: Set[str] = set()
    alias_index = 0
    # NOTICE（批次 6 / **M4**）：别名**不能撞上本次 upload 的任何一个字段名**。
    #
    # 修复前的判据只有 `alias not in step_variables and alias not in used_aliases`，
    # 而 upload 的**值**是在 `prepare_upload_step` 里**之后**才注入 `step_variables` 的。
    # 于是字段名恰好叫 `m_upload_0` 时会撞车，且是**静默错值**：
    #
    # ```text
    # upload: {'m_upload_0': 'AAA', '文件名': 'BBB'}
    #   -> 'm_upload_0' 是合法变量名 → 引用 $m_upload_0
    #   -> '文件名' 不能当变量名 → 生成别名 m_upload_0（此时它还"没人用"）→ 也引用 $m_upload_0
    #   -> 两个字段共用一个引用，实际取值以后注入的那个为准
    #      实测：multipart 里 name="m_upload_0" 发出去的是 **BBB**（期望 AAA），零告警
    # ```
    #
    # 字段发出的是**别的字段的值**，接口收到错参数而用例可能照旧通过 —— 本仓最忌讳的静默错值。
    # 修法：把字段名集合一并排除（不采用"像 `m_encoder` 那样直接报错"的口径：
    # `m_upload_N` 是完全合法的表单字段名，没有理由禁止用户使用）。
    field_names = {str(key) for key in upload}
    reserved_names = field_names | {"m_encoder"}

    for key in upload:
        name = str(key)

        if variable_regex_compile.fullmatch("${" + name + "}"):
            reference = f"${name}"
        else:
            while True:
                alias = f"m_upload_{alias_index}"
                alias_index += 1
                if (
                    alias not in step_variables
                    and alias not in used_aliases
                    and alias not in reserved_names
                ):
                    break
            used_aliases.add(alias)
            reference = f"${alias}"

        if not _field_expression_is_parseable(name, reference):
            unrepresentable.append(key)
            continue

        references[key] = reference

    if unrepresentable:
        detail = "\n".join(f"    upload 字段名 {key!r}" for key in unrepresentable)
        raise exceptions.ParamsError(
            f"upload 字段名无法写进生成的表达式：\n{detail}\n"
            f"  框架会把每个 upload 字段拼成 `<字段名>=<值的变量引用>` 放进 "
            f"${{multipart_encoder(...)}}，所以字段名里不能出现 `,` `=` `$` `(` `)` "
            f"`{{` `}}` `[` `]` 这类会改变参数切分/表达式匹配的字符。\n"
            f"  请改名（例如用 `_` 代替这些字符）。"
        )

    return references


def prepare_upload_step(
    step: TStep, step_variables: VariablesMapping, functions: FunctionsMapping
):
    """preprocess for upload test
        replace `upload` info with MultipartEncoder

    Args:
        step: teststep
            {
                "variables": {},
                "request": {
                    "url": "https://httpbin.org/upload",
                    "method": "POST",
                    "headers": {
                        "Cookie": "session=AAA-BBB-CCC"
                    },
                    "upload": {
                        "file": "data/file_to_upload"
                        "md5": "123"
                    }
                }
            }
        functions: functions mapping

    """
    if not step.request.upload:
        return

    # 0919-4 / L39：`m_encoder` 是 `upload:` 机制在步骤变量里占用的**保留名**
    # （下面那行会写入 `${multipart_encoder(...)}` 引用）。upload 字段撞上它时，
    # 用户字段的值会被整段覆盖、发出的请求体里混进函数表达式字符串——
    # 这种冲突没有「两全」的处理方式，必须响亮报错让用户改名。
    if "m_encoder" in step.request.upload:
        raise exceptions.ParamsError(
            "upload 字段名不能叫 m_encoder——它是 upload 机制在步骤变量里的保留名，"
            "框架会往里写 ${multipart_encoder(...)} 引用，你的字段值会被整段覆盖、"
            "发出的请求体里会混进函数表达式字符串。\n"
            "  请给这个字段换一个名字（例如 m_encoder2 / encoder_）。"
        )

    # parse upload info
    step.request.upload = parse_data(step.request.upload, step_variables, functions)

    ensure_upload_ready()

    # 顺带发现 ②（0919-4 §五）：先为每个字段决定变量引用 —— 字段名不能当变量名时
    # 用 `$m_upload_N` 别名，而不是让它静默变成一个字面量（见 _plan_field_references）。
    references = _plan_field_references(step.request.upload, step_variables)

    params_list = []
    for key, value in step.request.upload.items():
        # 0919-4 / L38：upload 键名会被注入步骤变量（供 `${multipart_encoder(k=$k)}`
        # 引用），与既有变量**同名且值不同**时是静默覆盖——步骤里其它引用
        # （headers/url/变量）会拿到 upload 的值，用户却毫无感知。这里补告警；
        # 行为保持不变：仍然覆盖，保证 upload 字段本身拿到正确的值。
        # NOTICE: 值相同时不告警——`upload: {file: $file}` 这种「引用同名变量」是
        # 合法且常见的写法，parse_data 解析后两侧值相同，天然静默。
        if key in step_variables and step_variables[key] != value:
            logger.warning(
                f"upload 字段 {key!r} 与已有变量同名但值不同，变量值会被覆盖：\n"
                f"  变量原值: {step_variables[key]!r}\n"
                f"  upload 值: {value!r}\n"
                f"本步骤里其它引用 ${{{key}}} 的位置（headers/url/变量）都会拿到 "
                f"upload 的值；若这不是本意，请给 upload 字段或变量换一个名字。"
            )
        step_variables[key] = value

        reference = references[key]
        # 用了别名 → 值的真身要放到别名变量里（字段名本身不变）
        if reference != f"${key}":
            step_variables[reference[1:]] = value

        params_list.append(f"{key}={reference}")

    params_str = ", ".join(params_list)
    # 0919-4 / L39：同理，用户在 config/step 变量里自定义的 m_encoder 也会被下面
    # 这行覆盖（upload 机制需要这个名字）。只可能影响「upload 块 + 自定义 m_encoder
    # 变量」这一种组合，给可见告警（保留名是机制的一部分，无法换名）。
    if "m_encoder" in step_variables:
        logger.warning(
            f"变量 m_encoder 已有值（{step_variables['m_encoder']!r}），"
            f"将被 upload 机制的编码器引用覆盖——步骤最终发出的 data 是编码后的 multipart 体。"
        )
    step_variables["m_encoder"] = "${multipart_encoder(" + params_str + ")}"

    # 顺带发现 ①（0919-4 §五）：Content-Type **必须**由 upload 机制拥有
    # （multipart 的 boundary 是编码器生成的，用户写死一个 boundary 只会让请求体与头不匹配），
    # 所以下面这行覆盖用户手写的值是**没有替代方案**的。但「静默覆盖」不行——与 L38 同族：
    # 用户明明在 headers 里写了 Content-Type，生成物里却变成另一套，且零信号。
    framework_content_type = "${multipart_content_type($m_encoder)}"
    overwritten = [
        (header_name, header_value)
        for header_name, header_value in step.request.headers.items()
        if isinstance(header_name, Text)
        and header_name.lower() == "content-type"
        and header_value != framework_content_type
    ]
    if overwritten:
        detail = "\n".join(f"    {name}: {value!r}" for name, value in overwritten)
        logger.warning(
            f"upload 步骤的请求头里已有 Content-Type，会被 upload 机制覆盖：\n"
            f"{detail}\n"
            f"  覆盖后: {framework_content_type}\n"
            f"原因：multipart 的 boundary 由编码器生成，写死的 Content-Type（尤其是写死的 "
            f"boundary）与实际请求体不匹配，必须由机制接管。\n"
            f"  若你写的就是 multipart/form-data，那是正常的，可删掉这行以免误导。"
        )
    step.request.headers["Content-Type"] = framework_content_type

    # NOTICE（0920 / 缺陷 1 的**运行期兜底**）：下面这行会把 `step.request.data` 整个
    # 换成 multipart 编码器。生成期闸门（`make.ensure_upload_body_is_unambiguous`）已经
    # 拦住了「`upload` + `data`/`json`」的手写 YAML，但**运行期**还留着两条不经过生成期
    # 的入口（Python API 直接构造 `TStep`、`hrun` 之外的调用方、引用用例改写 request），
    # 那里原本是**零信号**地丢掉请求体：
    #   - `data` 已有真值 → 被整段覆盖（服务端只收到 multipart，普通字段消失）；
    #   - `json` 已有真值 → `data` 变成编码器后 requests 的 `prepare_body` 走
    #     `if not data and json is not None` 的**假分支**，json 从不出现。
    # 两者都与 YAML 里写的请求体不一致却照样绿，属于本仓要消灭的形态，故在这里**报错**
    # （而不是只告警：没有任何"两全"的语义可推断，用户必须自己选一种请求体）。
    if step.request.req_json is not None:
        raise exceptions.ParamsError(
            "同一个 request 里同时写了 `upload` 与 `json` —— upload 机制会把 `data` "
            "换成 multipart 编码器，而 requests 在 `data` 非空时**整个 json 分支不执行**，"
            "`json` 会被静默丢弃（服务端只收到 multipart）。\n"
            f"  json: {step.request.req_json!r}\n"
            f"  upload: {step.request.upload}\n"
            "  要发 JSON 就删掉 `upload:`；要传文件就把字段写进 `upload:`。"
        )
    if step.request.data not in (None, "", {}):
        logger.warning(
            f"upload 步骤的 request 里已有 `data`，将被 multipart 编码器整体覆盖：\n"
            f"    data: {step.request.data!r}\n"
            f"    upload: {step.request.upload}\n"
            f"  原因：multipart 的请求体由 `upload:` 里的字段生成，`data` 不会被发出。\n"
            f"  若那些普通字段本来就要发出去，请把它们写进 `upload:`（uploader 会把非文件"
            f"标量当普通字段发）；若 `data` 是多余的，请删掉以免误导。"
        )

    step.request.data = "$m_encoder"


def multipart_encoder(**kwargs):
    """initialize MultipartEncoder with uploading fields.

    NOTICE（0919-12 / 批次 F）：这是**公开入口**（手工写在 YAML / pytest 里的
    `${multipart_encoder(...)}` 走这条）。它不知道「当前用例属于哪个项目」，
    因此 `root_dir=None` → 相对路径回退到 `load_project_meta("")`（历史行为）。

    真正跑用例时不要用这条：`SessionRunner._setup_runner()` 会把
    `bind_multipart_encoder(runner.root_dir)` 注册进该 runner 的 parser 函数表，
    `${multipart_encoder(...)}` 于是解析到**绑定了本项目根**的那个实现
    （见 `install_root_bound_multipart_encoder` 的 NOTICE）。

    Returns:
        MultipartEncoder: initialized MultipartEncoder object

    """
    return _build_multipart_encoder(kwargs, root_dir=None)


def _build_multipart_encoder(fields: Dict, root_dir: Optional[Text] = None):
    """`multipart_encoder` 的真正实现（`root_dir` 决定相对路径的基准）。

    Args:
        fields: 表单字段（字段名 → 值；值是文件的按文件发送，否则按普通字段）。
        root_dir: 相对路径的基准目录。None → 回退到「当前已加载的项目根」
            （`load_project_meta("")`）；跑用例时由 runner 传入自己的项目根。
    """

    def get_filetype(file_path):
        # NOTICE（0920 批次 3 / **N13**）：`filetype` 是可选的（upload extra）。
        # 缺失时不能裸调 `filetype.guess`（修复前是 `NameError`，见模块顶部 NOTICE），
        # 而是回退到「猜不出类型」的既有兜底值 —— 与"文件类型识别不出来"
        # 走的是同一条路径，行为可预期；真正需要依赖的入口（`ensure_upload_ready`）
        # 仍会在 `_build_multipart_encoder` 开头给出"请安装 upload extra"的可读报错。
        if filetype is None:
            return "text/html"
        file_type = filetype.guess(file_path)
        if file_type:
            return file_type.mime
        else:
            return "text/html"

    ensure_upload_ready()
    fields_dict = {}
    for key, value in fields.items():
        # L24（0918-8）：**只有路径类型**才去做「是不是文件」的判断。
        # 修复前无条件调 `os.path.isabs(value)`，于是 `upload: {field: 123}`
        # （YAML 里数字/布尔值很常见）会抛
        # `TypeError: argument of type 'int' is not iterable` 之类的错误——
        # 错误信息完全不涉及 upload，用户只能猜是哪一个字段的问题。
        if not isinstance(value, (str, bytes, os.PathLike)):
            if value is None:
                # YAML 里写了空值（`field:`）→ 视作空表单字段，与写空字符串等价
                logger.debug(f"upload field {key!r} is empty, send it as an empty field")
                fields_dict[key] = ""
            elif hasattr(value, "read") and callable(getattr(value, "read")):
                # NOTICE（0920 批次 4 / **N25**）：**文件对象要按文件发**，不能 `repr`。
                #
                # 修复前没有这一支，文件对象掉进下面的标量兜底 →
                # `fields_dict[key] = value` → 编码器 `f"{value}\r\n"` →
                # 请求体里出现的是 `<_io.BufferedReader name='…'>` **这个 repr 字符串**，
                # 文件内容**从未离开进程**，而且**零告警**（那条兜底只打 DEBUG 日志）。
                # 编码器自己其实是支持文件对象的（`_current_handle` /
                # `_part_length` 都专门处理了 `read`/`fileno`），这条路径白白浪费了。
                #
                # 现在按"文件字段"接管：取名字（`.name` 拿不到就用字段名），
                # 内容交给编码器流式读（不预读进内存）。
                file_name = ""
                raw_name = getattr(value, "name", None)
                if isinstance(raw_name, (str, bytes, os.PathLike)):
                    file_name = os.path.basename(os.fsdecode(raw_name))
                file_name = file_name or f"{key}.bin"
                logger.debug(
                    f"upload field {key!r} is a file object ({type(value).__name__}), "
                    f"send it as a file part (filename={file_name!r})"
                )
                # NOTICE: `get_filetype` 是按**路径**工作的（内部 `filetype.guess(path)`），
                # 而这里手上是**文件对象** —— 传文件名进去会 `FileNotFoundError`
                # （名字是相对的、也不一定存在于 cwd）。文件对象优先按 `.name`
                # 是否是真实文件来猜；猜不到就走"识别不出类型"的既有兜底。
                mime = None
                raw_name = getattr(value, "name", None)
                if isinstance(raw_name, (str, bytes, os.PathLike)) and os.path.isfile(
                    raw_name
                ):
                    mime = get_filetype(raw_name)
                fields_dict[key] = (file_name, value, mime or "text/html")
            elif isinstance(value, (list, tuple, dict, set, frozenset)):
                # 容器类型几乎不可能是「文件路径」，但也无法表达「(filename, fileobj, mime)」
                # 这种结构，因此告警后按 Python 字面量当普通字段发出去（不静默丢失）。
                logger.warning(
                    f"upload field {key!r} is a container ({type(value).__name__}), "
                    f"send it as a plain form field: {value!r}"
                )
                fields_dict[key] = str(value)
            else:
                # int/float/bool 等标量：普通表单字段（与 str 值的处理一致）
                logger.debug(
                    f"upload field {key!r} is not a path ({type(value).__name__}), "
                    "send it as a plain form field"
                )
                fields_dict[key] = value
            continue

        # NOTICE（批次 6 / **M8**）：`bytes` 值统一**解码成路径**再走原有分支。
        #
        # upload 的契约是「字段名 → **本地文件路径**」（`sanitize_case` 也刻意不脱敏 `upload`，
        # 见 `M22`），所以 bytes 在这里只能是"用 bytes 表达的路径"（YAML `!!binary`、
        # debugtalk 返回 `b'…'`）。修复前它直接进 `os.path`：
        #
        # ```text
        # 相对路径 b"a.bin"  -> os.path.join(str_base, b"a.bin")
        #                       TypeError: Can't mix strings and bytes in path components
        #                       ← 与"上传"毫无关系的报错，用户看不出是哪个字段
        # 绝对路径 b"/tmp/x" -> os.path.basename(b"/tmp/x") == b'x'
        #                       -> f'...filename="{filename}"' -> filename="b'x'"
        #                       ← 请求带着**损坏的 multipart 头**发出去
        # ```
        #
        # 用 `os.fsdecode`（不会抛异常，surrogateescape 兜底）：解码后与 str 完全同路径处理，
        # 于是"路径不存在"会走下面那条**响亮**的告警（0919-4 / L37），而不是 FakeError。
        if isinstance(value, bytes):
            value = os.fsdecode(value)

        if os.path.isabs(value):
            # value is absolute file path
            base_dir = None
            _file_path = value
            is_exists_file = os.path.isfile(value)
        else:
            # value is not absolute file path, check if it is relative file path
            # NOTICE（0919-12 / 批次 F，§二.5）：基准从「**当前已加载**的项目根」
            # 改为「**本用例所属项目**的根」（由 runner 绑定进来）。
            # 修复前多项目混跑/嵌套引用时，`load_project_meta("")` 拿到的可能是
            # **另一个项目**的 RootDir：若同名文件在那边存在，就会**静默上传错文件**
            # （0919-4 / L37 已经把「文件不存在」变响亮了，这里是剩下那一半：
            # 「文件存在但是在别人家的根下」）。退回 None 时仍是老行为（仅为兼容
            # 直接调用 `multipart_encoder(...)` 的场景，见其 docstring）。
            base_dir = root_dir
            if not base_dir:
                # 延迟导入（与修复前一致）：避免 uploader ↔ loader 的导入顺序问题
                from interfacetester.loader import load_project_meta

                base_dir = load_project_meta("").RootDir
            _file_path = os.path.join(base_dir, value)
            is_exists_file = os.path.isfile(_file_path)

        if is_exists_file:
            # value is file path to upload
            filename = os.path.basename(_file_path)
            mime_type = get_filetype(_file_path)
            # NOTICE（批次 C / **L14**）：这里**不再把文件读成 bytes**。
            # 修复前是 `with open(...) as f: file_bytes = f.read()` → 与后面 materialize 的
            # 整包 body 叠加，峰值内存约 **2N**（32 MB 文件实测 68 MB）。
            # 现在把**路径**交给编码器，由它按需逐块读取（长度用 `os.path.getsize`，
            # requests 据此设置 Content-Length —— `super_len` 走的正是 `__len__`）。
            # 兼容性：`Utf8MultipartEncoder` 对 str 片段按**路径**处理（懒打开、读完即关），
            # `build_utf8_multipart_body` 的老调用方传 bytes 也照旧工作。
            fields_dict[key] = (filename, _file_path, mime_type)
        else:
            # 0919-4 / L37：文件不存在时不能静默当普通字段发出去——路径打错一个字母
            # 的后果是「请求照常发出、可能 200、用例通过」，正是本仓最忌讳的静默假通过。
            # 只对「形似路径」的值告警（判据见 _looks_like_file_path：裸字符串不算，
            # 避免在合法的「文件 + 普通字段混写」用例上刷假报）；行为保持不变：
            # 原样当普通字段发出。
            if _looks_like_file_path(value):
                # NOTICE（0919-12 / 批次 F）：告警里把**相对路径的基准**打出来。
                # 修复前第 3 条写的是「多项目混跑时按『当前已加载』的项目根解析」——
                # 那是当时已知的限制；现在基准是本用例所属项目的根，写清楚它，
                # 用户才能一眼看出「我期望的那个根」和「实际用的根」是不是同一个。
                basis = (
                    f"本用例所属项目的根目录：{base_dir}"
                    if base_dir
                    else "（绝对路径，无相对基准）"
                )
                logger.warning(
                    f"upload 字段 {key!r} 的值形似文件路径，但文件不存在："
                    f"{os.path.abspath(_file_path)}\n"
                    f"已按**普通表单字段**原样发出——若本意是上传文件，请检查：\n"
                    f"  1) 路径拼写；\n"
                    f"  2) 相对路径的基准 {basis}（即含 debugtalk.py 的那一层）；\n"
                    f"  3) 文件是否真的在**本项目**下（多项目混跑时不再按别的项目根解析）。\n"
                    f"  NOTICE: 修复前这里按「当前已加载」的项目根解析，同名文件若存在于"
                    f"别的项目下会被**静默上传**；现在只认本用例所属项目。"
                )
            else:
                logger.debug(
                    f"upload field {key!r} is not an existing file, "
                    "send it as a plain form field"
                )
            fields_dict[key] = value

    return Utf8MultipartEncoder(fields_dict)


# 标记：`bind_multipart_encoder` 造出来的实现（用于区分「框架绑定的」与
# 「用户在 debugtalk.py 里自己定义的」——后者不能被框架悄悄顶掉）。
_ROOT_BOUND_MARKER = "__interfacetester_root_bound__"


def bind_multipart_encoder(root_dir: Text):
    """返回一个**绑定了 `root_dir`** 的 `multipart_encoder`（同签名，可被 parser 调用）。

    NOTICE（0919-12 / 批次 F）：为什么不把 root_dir 塞进 `${multipart_encoder(...)}` 表达式？
      · 表达式里的 kwargs 就是 **upload 字段名**，加任何「保留参数名」都会与用户字段撞车
        （与 `m_encoder` 那个保留名同类的坑，而这次还能撞在**字段**上）；
      · 路径里可能有引号/反斜杠/空格，写进表达式字符串要重新实现一套转义，风险更高。
    所以改绑**函数对象本身**：表达式一个字都不用变。
    """

    def _root_bound_multipart_encoder(**kwargs):
        return _build_multipart_encoder(kwargs, root_dir=root_dir)

    setattr(_root_bound_multipart_encoder, _ROOT_BOUND_MARKER, root_dir)
    return _root_bound_multipart_encoder


def install_root_bound_multipart_encoder(functions: Dict, root_dir: Text) -> bool:
    """把「绑定本用例项目根」的 `multipart_encoder` 注册进函数表；返回是否注册成功。

    NOTICE（0919-12 / 批次 F，§二.5）：`parser.get_mapping_function` **先查函数表**，
    所以注册进 `functions_mapping` 就能让 `${multipart_encoder(...)}` 解析到绑定版，
    表达式本身不用改（`upload:` 机制与手工写的表达式**同一条口径**）。

    刻意**不**顶掉用户自己的实现：`debugtalk.py` 里若定义了同名函数，
    尊重它（记 debug 日志），否则就是「框架悄悄替换了用户的函数」这种最难查的问题。
    """
    if not root_dir:
        return False

    existing = functions.get("multipart_encoder")
    if existing is not None and not getattr(existing, _ROOT_BOUND_MARKER, None):
        logger.debug(
            "functions_mapping 里已有用户定义的 multipart_encoder，"
            "保持用户的实现（框架不再绑定项目根）：{}",
            existing,
        )
        return False

    functions["multipart_encoder"] = bind_multipart_encoder(root_dir)
    return True


def multipart_content_type(m_encoder) -> Text:
    """prepare Content-Type for request headers

    Args:
        m_encoder: MultipartEncoder object

    Returns:
        content type

    """
    ensure_upload_ready()
    return m_encoder.content_type


class Utf8MultipartEncoder(object):
    """UTF-8 安全的 multipart/form-data 编码器（**流式**）。

    requests_toolbelt.MultipartEncoder 在中文文件名等非 ASCII 场景下可能因
    latin-1 编码触发 UnicodeEncodeError；这里改用以 UTF-8 手动构建 body 的方式规避。

    该对象实现了 requests 作为 ``data`` 参数所需的 ``read``/``__len__`` 接口，
    并暴露 ``content_type`` 属性供 ``multipart_content_type`` 使用。

    NOTICE（批次 C / **L14**）：修复前它把整包 body **先 materialize 成 bytes**
    （`self.body`）再套一层 `io.BytesIO` —— 上传 N 字节文件的峰值内存约 **2N**
    （文件字节一份 + 整包一份），而 `upload` extra 的卖点正是「**大文件** multipart」
    （实测 32 MB 文件的 tracemalloc 峰值 68 MB，`requests_toolbelt` 只 3 MB）。
    现在改成**片段列表 + 按需 `read()`**：文件字段**不再进内存**（长度由
    `os.path.getsize` 得出，requests 的 `super_len` 走的正是 `__len__`，
    由此设置 Content-Length），头部/尾部的字节片段本身很小。

    NOTICE: `.body` 属性**保留**（既有调用方与 `tests/uploader_test.py` 在用），
    但它会 materialize 整包 —— **流式路径不要用它**。
    """

    def __init__(self, fields):
        self._parts, self.content_type, self._length = _build_utf8_multipart_parts(
            fields
        )
        self._cursor = 0
        self._offset = 0
        self._open_handle = None
        self._open_handle_index = None

    # ------------------------------------------------------------------ 流式接口
    def read(self, size=-1):
        """按 requests / urllib3 的约定逐块读取（`size=-1` 表示读到底）。"""
        if size is None or size < 0:
            chunks = []
            while True:
                chunk = self.read(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)

        remaining = size
        chunks = []
        while remaining > 0 and self._cursor < len(self._parts):
            piece = self._read_from_current_part(remaining)
            if piece is None:
                continue  # 当前片段已读完（游标已前进）
            chunks.append(piece)
            remaining -= len(piece)
        return b"".join(chunks)

    def _read_from_current_part(self, remaining: int):
        """从当前片段读至多 `remaining` 字节；片段读完时前进游标并关闭句柄。"""
        part = self._parts[self._cursor]

        if isinstance(part, bytes):
            piece = part[self._offset : self._offset + remaining]
            self._offset += len(piece)
            if self._offset >= len(part):
                self._cursor += 1
                self._offset = 0
            return piece

        handle = self._current_handle()
        if handle is None:  # pragma: no cover - 极端 IO 错误：跳过该片段
            self._cursor += 1
            self._offset = 0
            return None

        piece = handle.read(remaining)
        if not piece:
            self._close_current_handle()
            self._cursor += 1
            self._offset = 0
            return None
        return piece

    def _current_handle(self):
        part = self._parts[self._cursor]
        if isinstance(part, str):
            # 路径片段：**懒打开**，读完即关（见 `_close_current_handle`）
            if self._open_handle_index != self._cursor:
                self._close_current_handle()
                self._open_handle = open(part, "rb")  # noqa: SIM115
                self._open_handle_index = self._cursor
            return self._open_handle
        # 调用方自己传进来的文件对象
        return part

    def _close_current_handle(self):
        if self._open_handle is not None:
            try:
                self._open_handle.close()
            finally:
                self._open_handle = None
                self._open_handle_index = None

    def close(self):
        """关闭内部打开的句柄（幂等）。

        NOTICE: 正常路径上片段读完就自动关了；这里只是给「发送中途失败」留的兜底，
        `step_request` 在请求结束后会防御性地调一次（`hasattr(data, "close")`）。
        """
        self._close_current_handle()

    # ------------------------------------------------------- 可回绕（N10）
    def tell(self) -> int:
        """当前逻辑读位置（`_cursor` 之前的片段长度之和 + 当前片段内偏移）。

        NOTICE（0920 批次 2 / **N10**）：`tell` / `seek` 的存在决定了 requests
        **能不能回绕请求体**。`requests.models.PreparedRequest.prepare_body` 只在
        请求体支持 `tell` 时才记 `self._body_position`，而
        `requests.sessions.Session.resolve_redirects` 只在 `_body_position is not None`
        时才 `rewind_body`：

        ```python
        rewindable = prepared_request._body_position is not None and (...)
        if rewindable:
            rewind_body(prepared_request)
        ```

        修复前的编码器**既没有 `tell` 也没有 `seek`**（只有 `read`/`__len__`/`__iter__`），
        于是 `rewindable=False`：遇到 307/308（要求原样重发请求体）时，
        requests 会把**已经读完**的编码器原样再发一次 —— `read()` 立刻返回 `b""`，
        而 `Content-Length` 仍是原始长度。实测（`.tmp_audit/up_audit/probe_upload_redirect_e2e.py`）：

        ```text
        服务端看到:  /upload  Content-Length=185 收到 185 字节
                     /final   Content-Length=185 收到   0 字节   ← 空体
        用例结果:    PASSED（重定向后的响应是 200，断言通过）
        ```

        即"上传静默变成 0 字节、用例还绿"。L14 的流式改写之前用的是 `io.BytesIO`
        （天然支持 tell/seek），改成流式编码器时**丢掉了可回绕性** —— 属回归。
        这里补上：`tell` 返回逻辑位置，`seek(0)` 复位到开头（`requests` 只用 0）。
        """
        return sum(
            (_part_length(part) or 0) for part in self._parts[: self._cursor]
        ) + self._offset

    def seek(self, offset: int, whence: int = 0) -> int:
        """把读位置复位到指定偏移；**只支持回到开头**（`requests` 只做这一种）。

        不支持任意偏移是有意的：中间片段可能是**懒打开的文件路径**，
        任意定位需要重新扫描并丢弃句柄；而 requests 的重定向回绕只用 `seek(0)`。
        其它偏移抛 `OSError`，与"不可 seek"的语义保持一致（不会静默给出错位置）。
        """
        if whence != 0 or offset != 0:
            raise OSError(
                "Utf8MultipartEncoder 只支持 seek(0)（重定向回绕用的就是它）"
            )
        self._close_current_handle()
        self._cursor = 0
        self._offset = 0
        return 0

    def seekable(self) -> bool:
        """声明可 seek —— `rewind_body` 之前 requests 会查它。"""
        return True

    def __len__(self):
        return self._length

    def __iter__(self):
        """逐个片段产出（**惰性**，不再一次产出整包）。"""
        while True:
            chunk = self.read(65536)
            if not chunk:
                return
            yield chunk

    @property
    def body(self) -> bytes:
        """整包 bytes（**materialize**，仅供兼容既有调用方与测试）。"""
        chunks = []
        for part in self._parts:
            if isinstance(part, bytes):
                chunks.append(part)
            elif isinstance(part, str):
                with open(part, "rb") as f:
                    chunks.append(f.read())
            else:
                chunks.append(part.read())
        return b"".join(chunks)


# multipart 头参数（字段名/文件名/mime）里的这三个字符必须转义：
#   - 裸 `"` 会**提前结束** `name="..."`，后面的内容变成头的一部分；
#   - 裸 CR/LF 会**拆行** —— 等于让字段名/文件名在 multipart 体内**伪造出一行头**
#     （实测文件名 `na\r\nX-Evil: 1"q` 就能插进去）。
# 转义成 `%22`/`%0D`/`%0A`，与 urllib3 的 `format_multipart_header_param`
# （以及被本模块替换掉的 `requests_toolbelt`）**逐字对齐**。
_MULTIPART_HEADER_ESCAPES = (('"', "%22"), ("\r", "%0D"), ("\n", "%0A"))


def _escape_multipart_header_param(value: Any) -> Text:
    """转义 multipart 头参数里的 `"` / CR / LF（批次 C / **L14**）。

    NOTICE（为什么这是缺陷而不是洁癖）：修复前是 f-string 直接插入
    `name="{key}"` / `filename="{filename}"`，实测（`probe_toolbelt_compare.py`）：

        字段名 `"a\\nb"`  → `Content-Disposition: form-data; name="a<真实换行>b"`  ← **头被拆行**
        文件名 `na\\r\\nX-Evil: 1"q` → 在 multipart 体内**伪造出一行头**

    而它替换掉的 `requests_toolbelt` 两个方向都会转义成 `%22/%0D/%0A` ——
    **自研版本丢掉了这层转义**。字段名形态可端到端复现（YAML 里写 `"a\\nb": v`），
    文件名形态需要 Linux/macOS 上的非常规文件名（Windows 不允许那种文件名）。
    """
    text = value if isinstance(value, str) else str(value)
    for raw, escaped in _MULTIPART_HEADER_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _is_existing_file_path(value: Any) -> bool:
    """三元组里的第二项是不是「一个**存在的文件路径**」（而不是文件对象/内容）。"""
    if isinstance(value, (str, bytes, os.PathLike)):
        try:
            return os.path.isfile(value)
        except (OSError, ValueError, TypeError):
            return False
    return False


def _part_length(value: Any) -> Optional[int]:
    """估算文件片段的字节数（**不读内容**）；拿不到时返回 None（调用方退回读进内存）。"""
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, (str, os.PathLike)):
        try:
            return os.path.getsize(value)
        except OSError:
            return None

    # 已打开的文件对象：先 stat，其次 seek 到末尾量一次再复位
    try:
        return os.fstat(value.fileno()).st_size - (value.tell() or 0)
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        pass
    try:
        position = value.tell()
        value.seek(0, os.SEEK_END)
        size = value.tell()
        value.seek(position)
        return size - position
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return None


def _build_utf8_multipart_parts(fields):
    """构建 multipart 片段列表：`(parts, content_type, total_length)`。

    片段要么是 `bytes`（头/普通字段/结尾），要么是**文件路径（str）或文件对象** ——
    后者**不进内存**，由 `Utf8MultipartEncoder.read()` 逐块读。
    """
    boundary = uuid.uuid4().hex
    utf8 = "utf-8"
    parts: List[Any] = []
    total = 0

    def add_bytes(chunk: bytes) -> None:
        nonlocal total
        parts.append(chunk)
        total += len(chunk)

    def add_file(fileobj: Any) -> None:
        nonlocal total
        length = _part_length(fileobj)
        if length is None:
            # 长度未知（不可 seek 的流等）→ 只能读进内存，保证 `__len__`（Content-Length）正确
            content = fileobj.read() if hasattr(fileobj, "read") else fileobj
            logger.debug(
                f"upload: 片段长度无法预先得到，已读进内存（{len(content)} bytes）"
                f"—— 只有这一种情况会 materialize"
            )
            add_bytes(content)
            return
        parts.append(fileobj)
        total += length

    for key, value in fields.items():
        add_bytes(f"--{boundary}\r\n".encode(utf8))

        if isinstance(value, tuple) and len(value) == 3:
            filename, fileobj, mime_type = value
            # NOTICE（批次 6 / M8）：显式三元组里的 `filename` 也要归一 —— 传 bytes 时
            # `f'{filename}'` 会写出 `filename="b'x.bin'"`（repr 当文件名），请求带着
            # 损坏的 multipart 头发出去，且**零告警**。
            if isinstance(filename, bytes):
                filename = os.fsdecode(filename)
            add_bytes(
                (
                    f"Content-Disposition: form-data; "
                    f'name="{_escape_multipart_header_param(key)}"; '
                    f'filename="{_escape_multipart_header_param(filename)}"\r\n'
                ).encode(utf8)
            )
            add_bytes(
                f"Content-Type: {_escape_multipart_header_param(mime_type)}\r\n\r\n".encode(
                    utf8
                )
            )
            if hasattr(fileobj, "read") or _is_existing_file_path(fileobj):
                add_file(fileobj)
            else:
                add_bytes(
                    fileobj if isinstance(fileobj, bytes) else str(fileobj).encode(utf8)
                )
            add_bytes(b"\r\n")
        else:
            add_bytes(
                (
                    f"Content-Disposition: form-data; "
                    f'name="{_escape_multipart_header_param(key)}"\r\n\r\n'
                ).encode(utf8)
            )
            add_bytes(f"{value}\r\n".encode(utf8))

    add_bytes(f"--{boundary}--\r\n".encode(utf8))
    return parts, f"multipart/form-data; boundary={boundary}", total
