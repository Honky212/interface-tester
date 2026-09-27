r"""curl → IR 适配器（P3-a）。

**支持子集**（超出这个子集的写法一律给出告警，绝不静默丢弃）
----------------------------------------------------------
| curl 写法 | 处理 |
| --- | --- |
| `curl <url>` | 起一条步骤；url 原样保留（查询串留在 URL 里，保证编码不被改写） |
| `-X/--request <METHOD>` | → `method` |
| `-H/--header "K: V"` / `"K;"`（空值头） | → `headers`；敏感头自动占位化 |
| `-d/--data/--data-raw/--data-binary/--data-ascii <v>` | → `data`（多个 `-d` 用 `&` 连接，与 curl 行为一致） |
| `--data-urlencode <v>` | → 与 `-d` 同样的字符串拼接，**保留原始编码**（不做二次编码） |
| `-d/--data @file`、`--data-urlencode @file` | **不支持读文件**：`@file` 按字面值保留并**告警**（请改用 `${func()}` 读文件内容） |
| `-F/--form "k=v"` / `"k=@file"` / `"k=<file"` | → 表单字段进 `data`，文件进 `upload`（`@`/`<` 前缀按 curl 语义区分） |
| `-G/--get` | 把 `-d` 的内容拼到 URL 查询串上 |
| `-u/--user user:pass` | → `Authorization: Basic ${AUTH_BASIC}` 占位（**明文不入库**） |
| URL 里带 userinfo（`https://user:pass@host/x`） | 与 `-u/--user` **同义**：userinfo 从 URL 剥掉 → `Authorization: Basic ${AUTH_BASIC}` 占位 + 告警（**明文不入库**） |
| `-b/--cookie "k=v; k2=v2"` | → `cookies`，值全部占位化 |
| `-k/--insecure` | → `request.verify: false` |
| `-x/--proxy <url>` | → `request.proxies` |
| `-L/--location`、`--compressed`、`-s/-S/-v/-i/-N` 等 | 与结果无关，忽略（`-L` 是默认行为） |
| 续行 `\`、行首 `#` 注释、一文件多条命令、单/双引号、Windows 双引号转义 | 支持 |
| `-o/--output`、`--upload-file/-T`、`--cert/--key`、`--cacert`、`--http2`、`-w`、`-f`、`--retry` 等 | **不支持** → 告警（`--cert` 请手写 `request.cert`） |

NOTICE: curl 的语法**永不收敛**，所以这里的原则是「**明确子集 + 明确告警**」：
能映射的映射，不能映射的说清楚，剩下的交给人工补。
"""

import re
import shlex
from base64 import b64encode
from typing import Any, Dict, List, Optional, Text, Tuple

from interfacetester.converters.ir import (
    IRCase,
    IRStep,
    describe_opaque_value,
    merge_variables,
    parse_cookie_header,
    sanitize_body,
    sanitize_case,
    sanitize_cookies,
    sanitize_headers,
    warn_dynamic_values,
    warn_source_expressions,
    without_userinfo,
)

# 选项 → 是否吃掉一个值
_FLAGS_WITH_VALUE = {
    "-X": "method",
    "--request": "method",
    "-H": "header",
    "--header": "header",
    "-d": "data",
    "--data": "data",
    "--data-raw": "data",
    "--data-binary": "data",
    "--data-ascii": "data",
    "--data-urlencode": "data_urlencode",
    "-F": "form",
    "--form": "form",
    "--form-string": "form_string",
    "-u": "user",
    "--user": "user",
    "-b": "cookie",
    "--cookie": "cookie",
    "-x": "proxy",
    "--proxy": "proxy",
    "-A": "user_agent",
    "--user-agent": "user_agent",
    "-e": "referer",
    "--referer": "referer",
    "-o": "output",  # 不支持，但要知道它吃一个值
    "--output": "output",
    "-T": "upload_file",  # 不支持
    "--upload-file": "upload_file",
    "-w": "write_out",  # 不支持
    "--write-out": "write_out",
    "--cert": "cert",  # 不支持
    "--key": "key",
    "--cacert": "cacert",
    "--connect-timeout": "connect_timeout",
    "-m": "max_time",
    "--max-time": "max_time",
    "--retry": "retry",
    "-z": "time_cond",
    "--time-cond": "time_cond",
}

# 不接受值的开关
_BOOLEAN_FLAGS = {
    "-G": "get",
    "--get": "get",
    "-k": "insecure",
    "--insecure": "insecure",
    "-L": "location",
    "--location": "location",
    "-i": "include",
    "--include": "include",
    "-s": "silent",
    "--silent": "silent",
    "-S": "show_error",
    "--show-error": "show_error",
    "-v": "verbose",
    "--verbose": "verbose",
    "-f": "fail",
    "--fail": "fail",
    "-N": "no_buffer",
    "--no-buffer": "no_buffer",
    "--compressed": "compressed",
    "--http1.1": "http1_1",
    "--http2": "http2",
    "--http2-prior-knowledge": "http2",
    "-4": "ipv4",
    "--ipv4": "ipv4",
    "-6": "ipv6",
    "--ipv6": "ipv6",
    "--no-keepalive": "no_keepalive",
    "--raw": "raw",
    "-O": "remote_name",
    "--remote-name": "remote_name",
    "-I": "head",
    "--head": "head",
    "-j": "junk_session_cookies",
    "--junk-session-cookies": "junk_session_cookies",
}

# 明确不支持、需要告警的开关（值型开关在这里再列一次，便于给「替代写法」）
_UNSUPPORTED_HINTS: Dict[Text, Text] = {
    "output": "`-o/--output` 是「把响应存文件」，用例里请用 extract + 文件断言",
    "upload_file": "`-T/--upload-file` 是「PUT 上传整个文件」，请改写成 `request.upload`",
    "write_out": "`-w/--write-out` 是 curl 的输出模板，用例请用 extract/validate",
    "cert": "客户端证书请写进 `request.cert`（路径）",
    "key": "客户端证书私钥请与 `request.cert` 一起写（`cert: [cert, key]`）",
    "cacert": "自定义 CA 请用 `request.verify: <ca 路径>`（或用 config.verify）",
    "connect_timeout": "连接超时请用 `request.timeout`（框架统一控制整体超时）",
    "max_time": "最大耗时时请用 `request.timeout`",
    "retry": "重试请用 `retry_times`/`retry_interval`（仅对断言失败重试）",
    "time_cond": "`-z/--time-cond` 无对应能力，请人工处理",
    "form_string": "`--form-string`（不解析 @/<）等价于普通表单字段，已按字面值处理",
    "http2": "框架不支持强制 HTTP/2（requests 走 HTTP/1.1），该开关已忽略",
    "ipv4": "IP 版本选择无对应能力，已忽略",
    "ipv6": "IP 版本选择无对应能力，已忽略",
    "no_keepalive": "连接复用由 requests 管理，该开关已忽略",
    "raw": "`--raw` 关闭 HTTP 编码，用例里无对应能力，已忽略",
    "head": "`-I/--head` 已按 `-X HEAD` 处理",
    "remote_name": "`-O` 是保存文件，已忽略",
}


def split_commands(content: Text) -> List[Tuple[int, Text]]:
    """把文件切成多条 curl 命令，返回 ``[(起始行号, 命令文本)]``。

    处理：续行 `\\`、行首 `#` 注释、空行；其它命令（不以 curl 开头）会被跳过（调用方告警）。
    """
    commands: List[Tuple[int, Text]] = []
    buffer: List[Text] = []
    start_line = 0

    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not buffer and (not line or line.startswith("#")):
            continue

        continued = line.endswith("\\")
        if continued:
            line = line[:-1].rstrip()
        buffer.append(line)

        if not continued:
            commands.append((start_line or line_number, " ".join(buffer).strip()))
            buffer = []
            start_line = 0
        elif not start_line:
            start_line = line_number

    if buffer:  # 文件末尾没有换行/反斜杠不完整
        commands.append((start_line or 1, " ".join(buffer).strip()))

    return commands


def tokenize(command: Text) -> List[Text]:
    """按 POSIX 规则切词（支持单/双引号与反斜杠转义）。"""
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _strip_curl_prefix(tokens: List[Text]) -> Tuple[List[Text], List[Text]]:
    """去掉命令名（`curl` / `/usr/bin/curl` / `curl.exe`），返回 ``(选项, 警告)``。"""
    warnings: List[Text] = []
    if not tokens:
        return [], warnings
    head = tokens[0].lower().replace("\\", "/").rsplit("/", 1)[-1]
    if head in ("curl", "curl.exe"):
        return tokens[1:], warnings
    return tokens, [f"命令不是以 curl 开头（实际是 {tokens[0]!r}），已尝试按 curl 参数解析"]


def _expand_short_cluster(token: Text) -> List[Text]:
    """把 `-sS` 这类短开关簇展开成 `['-s', '-S']`（只在每个字符都是已知布尔开关时）。"""
    if token.startswith("--") or len(token) <= 2:
        return [token]
    letters = token[1:]
    if all(f"-{letter}" in _BOOLEAN_FLAGS for letter in letters):
        return [f"-{letter}" for letter in letters]
    return [token]


def _parse_cookie_string(cookie_text: Text) -> Dict[Text, Text]:
    """`k=v; k2=v2` → dict（实现已上移到 `ir.parse_cookie_header`，三个源共用）。"""
    return parse_cookie_header(cookie_text)


def _parse_form_field(raw: Text) -> Tuple[Optional[Text], Optional[Text], Optional[Text], List[Text]]:
    """解析 `-F` 的值，返回 ``(字段名, 值, 文件路径, 告警)``。

    curl 语义：`name=value` 字面值；`name=@file` 从文件读内容当**文件**上传；
    `name=<file` 从文件读内容当**字段值**；末尾可用 `;type=...` / `;filename=...` 传元数据。
    """
    warnings: List[Text] = []
    name, _, remainder = raw.partition("=")
    if not name or not remainder:
        return None, None, None, [f"`-F {raw}` 不是 `名字=值` 形式，已跳过（请人工处理）"]

    # NOTICE（0920 批次 3 / **N18**）：附加参数（`;type=` / `;filename=`）必须在
    # **两个分支**都剥离。修复前的条件是 `";" in remainder and not remainder.startswith("@")`
    # —— 也就是"值是文件时**恰恰不剥**"。后果（`.tmp_audit/verify_sub_f1_f3.py` 实测）：
    #
    #     -F 'file=@./photo.jpg;type=image/jpeg'
    #       -> upload={'file': './photo.jpg;type=image/jpeg'}   ← 路径里带着 ;type=
    #       -> warnings=[]                                       ← 而且**零告警**
    #
    # 于是运行期按那个"路径"去找文件必然找不到（用例起不来），而导入期没有任何信号；
    # 同函数里 `-F 'k=v;type=text/plain'` 却会正常告警 —— 同一种附加参数、两种口径。
    # 更要紧的是：Postman / Insomnia 的「Copy as cURL」导出的正是
    # `--form 'file=@"/path/x.png";type=image/png'` 这个形态，属**默认会踩**。
    meta: List[Text] = []
    is_file_field = remainder.startswith("@")
    if ";" in remainder:
        remainder, _, meta_text = remainder.partition(";")
        meta = [part for part in meta_text.split(";") if part]

    if meta:
        detail = ";".join(meta)
        if is_file_field:
            warnings.append(
                f"`-F {raw}` 的附加参数（{detail}）无法表达，已**忽略**："
                f"框架只支持 `upload: {{字段名: 文件路径}}`；"
                f"文件类型/文件名由服务端按实际上传的文件推断。\n"
                f"  已按文件名 {remainder[1:]!r} 导入（附加参数不参与请求）；"
                f"若必须指定 type/filename，请改用 `data:` 原始体或自定义函数。"
            )
        else:
            warnings.append(
                f"`-F {raw}` 的附加参数（{detail}）无法表达，"
                "框架只支持 `upload: {字段名: 文件路径}`；文件名/类型由服务端按实际文件推断"
            )

    if remainder.startswith("@"):
        file_path = remainder[1:]
        # NOTICE（0920 批次 3 / N18 连带）：curl 允许给文件名加引号，而
        # Postman / Insomnia 的「Copy as cURL」导出的**正是**这个形态：
        #     --form 'file=@"/path/x.png";type=image/png'
        # 修复前引号会被原样留在路径里（`'"/path/x.png"'`），运行期同样找不到文件。
        # 这里剥掉**成对**的首尾引号；只有单侧引号时不猜（原样保留）。
        if (
            len(file_path) >= 2
            and file_path[0] == file_path[-1]
            and file_path[0] in ("'", '"')
        ):
            file_path = file_path[1:-1]
        return name, None, file_path, warnings
    if remainder.startswith("<"):
        warnings.append(
            f"`-F {raw}` 用的是 `<file`（把文件内容当字段值），已按字面值处理："
            "请确认是否需要改成 `${func()}` 读取文件内容"
        )
        return name, remainder[1:], None, warnings
    return name, remainder, None, warnings


def parse_curl_command(
    command: Text, line_number: int = 0, name: Text = ""
) -> IRStep:
    """把一条 curl 命令解析成 `IRStep`（不支持的写法进 `warnings`）。"""
    step = IRStep(name=name or f"curl line {line_number or 1}")
    step.source = f"第 {line_number} 行" if line_number else ""

    tokens = tokenize(command)
    if not tokens:
        return step

    option_tokens, prefix_warnings = _strip_curl_prefix(tokens)
    step.warnings.extend(prefix_warnings)

    method: Optional[Text] = None
    url: Optional[Text] = None
    headers: Dict[Text, Text] = {}
    cookies: Dict[Text, Text] = {}
    data_parts: List[Text] = []
    form_fields: Dict[Text, Any] = {}
    upload_files: Dict[Text, Text] = {}
    proxy: Optional[Text] = None
    basic_auth: Optional[Text] = None
    use_get = False
    insecure = False

    index = 0
    while index < len(option_tokens):
        raw_token = option_tokens[index]
        index += 1

        if not raw_token.startswith("-"):
            if url is None:
                url = raw_token
            else:
                step.add_warning(
                    f"命令里有多个位置参数（{raw_token!r}），curl 只取第一个作为 URL，已忽略其余"
                )
            continue

        expanded = _expand_short_cluster(raw_token)
        if len(expanded) > 1:
            option_tokens[index:index] = expanded
            continue

        token = raw_token
        extra_hint = ""
        if token.startswith("--") and "=" in token:
            token, _, extra_hint = token.partition("=")

        if token in _BOOLEAN_FLAGS:
            flag = _BOOLEAN_FLAGS[token]
            if flag == "get":
                use_get = True
            elif flag == "insecure":
                insecure = True
            elif flag == "head":
                method = method or "HEAD"
            if flag in _UNSUPPORTED_HINTS and flag != "form_string":
                step.add_warning(f"`{token}`：{_UNSUPPORTED_HINTS[flag]}")
            continue

        if token in _FLAGS_WITH_VALUE:
            kind = _FLAGS_WITH_VALUE[token]
            if not extra_hint:
                if index >= len(option_tokens):
                    step.add_warning(f"`{token}` 后面缺少取值，已忽略")
                    continue
                extra_hint = option_tokens[index]
                index += 1

            if kind == "method":
                method = extra_hint.upper()
            elif kind == "header":
                name_part, sep, value_part = extra_hint.partition(":")
                if sep:
                    headers[name_part.strip()] = value_part.strip()
                else:  # `-H "X-Empty;"` → 空值头
                    headers[extra_hint.strip().rstrip(";")] = ""
            elif kind in ("data", "data_urlencode"):
                # 0918-7 / L9：curl 的 `-d @file` / `--data-urlencode @file` 语义是
                # 「读文件内容作为请求体」。修复前 `@data.json` 被当**字面量**塞进
                # request.data 且无任何告警，生成的用例会把字符串 "@data.json" 原样发给服务端。
                # 导入器不读文件（与 `-F` 的 `<file` 处理一致：宁可显式告警，也不猜）。
                if extra_hint.startswith("@"):
                    step.add_warning(
                        f"`{extra_hint}` 用了 `@file`（curl 语义：读文件内容作为请求体），"
                        "已按字面值保留——请把 data 改成项目 debugtalk.py 里的 "
                        "`${func()}` 读取文件内容，或手工编辑生成的 YAML"
                    )
                data_parts.append(extra_hint)
                if kind == "data_urlencode":
                    step.add_warning(
                        f"`--data-urlencode {extra_hint}` 的编码**原样保留**（框架不会再编码一次）"
                    )
            elif kind == "form":
                field_name, field_value, file_path, form_warnings = _parse_form_field(extra_hint)
                step.warnings.extend(form_warnings)
                if field_name:
                    if file_path:
                        upload_files[field_name] = file_path
                    else:
                        form_fields[field_name] = field_value
            elif kind == "form_string":
                field_name, _, remainder = extra_hint.partition("=")
                if field_name and remainder:
                    form_fields[field_name] = remainder
            elif kind == "user":
                basic_auth = extra_hint
            elif kind == "cookie":
                # NOTICE（0920 批次 7 / **N44**）：`-b/--cookie` 在 curl 里是**双语义**的：
                #
                #     -b 'sid=abc; theme=dark'   → 内联 cookie 串
                #     -b cookies.txt             → **从文件读** cookie（Netscape 格式）
                #
                # 修复前一律按"cookie 串"解析 —— 文件形态里没有 `=`，
                # `parse_cookie_header` 返回空 dict，于是 **cookie 全部消失且零告警**
                # （实测 `.tmp_audit/n44_check.py`：`cookies={}`、`warnings=[]`）。
                # 回放时少了整个会话凭据，表现为"莫名的 401/未登录"，
                # 而 curl 命令里明明写着 `-b cookies.txt`。
                #
                # 判据：**含 `=` 才是内联串；否则按文件名处理**（与 curl 的判据一致 ——
                # curl 也是"含 `=` 当串，否则当文件"）。
                # 导入器**不读文件**（读文件会把宿主机状态带进产物，且文件未必存在），
                # 所以这里**响亮告警**并给出改法，而不是静默丢掉。
                if "=" in extra_hint:
                    cookies.update(_parse_cookie_string(extra_hint))
                else:
                    step.add_warning(
                        f"`-b/--cookie {extra_hint}` 被当作**cookie 文件**（curl 语义："
                        f"不含 `=` 即视为文件名）→ 导入器**不读文件**，"
                        f"因此这组 cookie **没有进入用例**（回放会少掉整个会话凭据）。\n"
                        f"  改法（二选一）：\n"
                        f"  1) 把 cookie 内容直接写进命令：`-b \"sid=abc; theme=dark\"`；\n"
                        f"  2) 保留文件读入 → 在项目 `debugtalk.py` 里读该文件并返回 cookie 字典，"
                        f"再在用例里用变量引用（导入器不代劳，避免把宿主机状态写进产物）。"
                    )
            elif kind == "proxy":
                proxy = extra_hint
            elif kind == "user_agent":
                headers["User-Agent"] = extra_hint
            elif kind == "referer":
                headers["Referer"] = extra_hint
            else:
                hint = _UNSUPPORTED_HINTS.get(kind)
                step.add_warning(
                    f"`{token}` 暂不支持，已忽略" + (f"：{hint}" if hint else "")
                )
            continue

        step.add_warning(f"无法识别的参数 `{token}`，已忽略（请人工确认它是否影响用例）")

    if url is None:
        step.add_warning("命令里没有 URL，已跳过")
        step.url = ""  # 用空串表示「没解析出 URL」，避免被 IRStep 的默认 "/" 掩盖
        return step

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        # curl 对裸域名默认补 http://
        step.add_warning(f"URL 没有协议头（{url}），已按 curl 行为补成 http://{url}")
        url = f"http://{url}"

    # -G：把 -d 的内容并到查询串上
    if use_get and data_parts:
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}{'&'.join(data_parts)}"
        data_parts = []
        step.add_warning("`-G`：`-d` 的内容已并到 URL 查询串（与 curl 行为一致）")

    step.url = url
    step.method = method or ("POST" if (data_parts or form_fields or upload_files) else "GET")

    # NOTICE（批次 E / **L5**）：`-d` 与 `-F` 同时出现时，修复前是**静默丢掉 `-d` 的内容**
    # （`form_fields` 那一段会把 `step.data` 整体覆盖掉），而真实 `curl.exe` 对这种组合
    # **直接 exit 2 拒绝**（"You can only select one HTTP request method" 一类的用法错误）。
    # 口径：显式丢弃 + 点名告警（把丢掉的内容打出来），不学 curl 直接失败 ——
    # 一个多行的 curl 文件里混进一条坏命令，不该让整批导入失败（与本模块
    # 「不支持的参数只告警」的既有取向一致）。
    if data_parts and (form_fields or upload_files):
        dropped_body = "&".join(data_parts)
        # NOTICE（0920 批次 4 / **N21**）：**不能把被丢弃的体原文打进告警**。
        # 告警会进 stdout 与 `hconvert --report` 的 markdown，而这条正是在回显
        # 用户的请求体（`-d 'user=bob&password=…&token=…'`）—— 明文凭据。
        # 与 M13（URL userinfo 不进告警）同一口径：只给**字段名**，值一律不出现。
        step.add_warning(
            "`-d`/`--data` 与 `-F`/`--form` **同时出现**：真实 curl 会直接拒绝这种组合"
            "（exit 2），这里按 `-F` 处理 —— `-d` 的内容已被**丢弃**：\n"
            f"    {describe_opaque_value(dropped_body)}\n"
            "  请确认你要发的是表单（`-F`）还是请求体（`-d`），删掉另一个。"
        )
        data_parts = []

    if data_parts:
        body_text = "&".join(data_parts)
        content_type = next(
            (value for key, value in headers.items() if key.lower() == "content-type"), ""
        )
        if "json" in str(content_type).lower():
            import json as _json

            try:
                parsed = _json.loads(body_text)
            except ValueError:
                step.add_warning(
                    "Content-Type 是 JSON 但请求体不是合法 JSON，已按原始字符串放进 `data`"
                )
                step.data = body_text
            else:
                step.json_body = parsed
                step.add_warning(
                    "`-d` 的 JSON 已转成结构化的 `request.json`（等价请求，"
                    "并且便于对敏感字段做占位与后续参数化）"
                )
        else:
            step.data = body_text

    if form_fields:
        if upload_files:
            # 混合表单：普通字段与文件都进 multipart。
            # NOTICE（0920 / 缺陷 1c 文案订正）：这里 IR 侧照旧写 `data` + `upload`
            # （`data` 只作 IR 的中间表示），**由 `emit_yaml.build_request_dict` 在
            # 导出时把 `data` 并进 `upload`**。修复前这条告警写的是"普通字段放 data、
            # 文件放 upload，框架的 upload 会一起组 multipart"—— 那是**假承诺**：
            # 运行期 `prepare_upload_step` 会把 `data` 整个换成 $m_encoder，
            # 普通字段**根本不会被发出**（实测服务端只收到文件部分）。
            step.data = form_fields
            step.add_warning(
                "`-F` 里既有普通字段又有文件：普通字段与文件字段都会进入 `upload`，"
                "由框架统一组 multipart（导出时 `data` 会被并入 `upload`；"
                "`data` 本身不会被单独发出）"
            )
        else:
            step.data = form_fields
    if upload_files:
        step.upload = upload_files

    if insecure:
        step.verify = False
        step.add_warning("`-k/--insecure` → `request.verify: false`（自签名证书场景）")

    if proxy:
        step.proxies = {"http": proxy, "https": proxy}
        step.add_warning(f"`-x/--proxy {proxy}` → `request.proxies`（http/https 都指向它）")

    if basic_auth:
        # NOTICE（批次 9-1 / 与 H10 同一条链路）：**必须先 base64**。
        # 修复前这里放的是 `-u` 的**原样字符串**（`Basic alice:s3cret`），而下游
        # `sanitize_headers` 会把整个值换成 `Basic ${AUTH_BASIC}` 占位 —— 产物上看不出来，
        # 所以这个错误一直没被发现。它与两个已成立的口径都对不上：
        #   1) `curl -u` 的真实语义就是 `Authorization: Basic base64(user:pass)`；
        #   2) `ir.sanitize_case` 处理 URL userinfo 时（ir.py 注释写着"与 `-u/--user`
        #      走同一条链路：先放 base64"）确实做了 base64。
        # 后果不是泄漏（值照样被占位），而是**两条路径的内部表示不同**：占位变量
        # `AUTH_BASIC` 的语义在一条路径上是「base64」、在另一条上是「user:pass 原文」。
        # 谁照着警告文案去填环境变量，就会填错。
        # 回归见 `tests/sensitive_data_leak_test.py::TestUserinfoCredentials` 的
        # `test_base64_matches_the_decoded_userinfo`（对 userinfo 与 `-u` 两种写法都断言）。
        encoded = b64encode(basic_auth.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
        step.add_warning(
            "`-u/--user` → `Authorization: Basic <base64>`；用户名密码已**占位化**，"
            "不会明文写进 YAML"
        )

    # 动态值提示（只提示不改写）
    step.warnings.extend(warn_dynamic_values(headers, "请求头"))

    # 敏感信息占位化（必须在最后：先做完所有映射，再统一替换）
    step.headers, header_vars, header_warnings = sanitize_headers(headers)
    step.warnings.extend(warning for warning in header_warnings if warning not in step.warnings)

    step.cookies, cookie_vars, cookie_warnings = sanitize_cookies(cookies)
    step.warnings.extend(cookie_warnings)

    if isinstance(step.json_body, (dict, list)):
        step.json_body, body_vars, body_warnings = sanitize_body(step.json_body)
        step.warnings.extend(body_warnings)
    elif isinstance(step.data, dict):
        step.data, body_vars, body_warnings = sanitize_body(step.data)
        step.warnings.extend(body_warnings)
    else:
        body_vars = {}

    step.variables = merge_variables(header_vars, cookie_vars, body_vars)
    step.warnings.extend(warn_dynamic_values(step.params, "查询串"))
    return step


def convert_curl_file(
    content: Text, case_name: Text = "curl imported case", source: Text = ""
) -> IRCase:
    """把一份 curl 文件（多条命令）转成一个 `IRCase`（每条命令一个 step）。"""
    case = IRCase(name=case_name, source=source)
    seen_origins: List[Text] = []

    from interfacetester.converters.ir import origin_of, split_url

    for command_index, (line_number, command) in enumerate(split_commands(content), start=1):
        if not command.lower().startswith(("curl", "/", "curl.exe")):
            case.add_warning(
                f"第 {line_number} 行的内容不是 curl 命令，已跳过：{command[:60]}..."
            )
            continue

        step = parse_curl_command(
            command,
            line_number=line_number,
            name=f"step {command_index}（第 {line_number} 行）",
        )
        if not step.url:
            case.add_warning(f"第 {line_number} 行的命令没有解析出 URL，已跳过")
            continue

        origin = origin_of(step.url)
        base_url, path = split_url(step.url)

        if not case.base_url:
            case.base_url = base_url
            seen_origins.append(origin)
            step.url = path  # 用第一条命令的 origin 当 base_url，该步骤自身也改写成相对路径
        elif origin != case.base_url:
            # 多主机：保留绝对 URL（框架允许 request.url 写绝对地址），并提示
            if origin not in seen_origins:
                seen_origins.append(origin)
            step.add_warning(
                # NOTICE（批次 6 / M13）：**告警文本里的 URL 必须抹掉 userinfo**。
                # 这段告警在 `sanitize_case` 剥 userinfo **之前**生成，修复前会把
                # `https://alice:S3cr3t@host` 的口令原样抄进 stdout 与 `--report` 报告。
                f"该步骤的 host（{without_userinfo(origin)}）与用例 "
                f"`base_url`（{without_userinfo(case.base_url)}）不同，"
                f"已保留绝对 URL：{without_userinfo(step.url)}"
            )
        else:
            step.url = path

        case.steps.append(step)

    if case.steps:
        case.variables = merge_variables(
            case.variables, *(step.variables for step in case.steps)
        )
        # 变量提升到 config 后，步骤里不必再重复
        for step in case.steps:
            step.variables = {}

    if not case.steps:
        case.add_warning("文件里没有解析出任何 curl 命令")
    else:
        # 0917-1：curl 命令里**没有响应体**，因此这个源**永远生成不了内容断言**。
        # 此前是静默的：用户拿到一堆"没有 validate"的用例，容易误以为已经校验过了。
        case.add_warning(
            "curl 命令里没有响应内容 → 本用例只生成了**请求**、没有生成任何断言"
            "（生成物里没有 validate 段，跑起来必然「通过」）。\n"
            "  hint: 断言请按被测接口补：JSON 用 jsonschema_match、XML/SOAP 用 xpath_match"
            "（见 docs/soap/README.md）、简单片段用 `contains`；"
            "或者改用能录到响应的 HAR/Postman 源（`hconvert --from har`）。"
        )

    # M5（0918-2）：统一收口的脱敏必须放在**最后**——curl 的查询串留在 `url` 上、
    # 非 JSON 体是字符串、`X-Signature`/裸 `token` 不在旧白名单里，
    # 这些都只有在收口点才被覆盖（详见 converters/ir.py::sanitize_case）。
    #
    # 批次 9-0（M9）：素材里若出现 `${...}` 字面量，登记一条告警（只告警、不擅自改写）。
    # 必须在 `sanitize_case` **之后**调用：`sanitize_*` 会自己往值里写 `${...}` 占位符，
    # 扫原始素材才不会把「导入器自己生成的占位符」也报成风险。
    result = sanitize_case(case)
    warn_source_expressions([result], content, "curl 素材")
    return result
