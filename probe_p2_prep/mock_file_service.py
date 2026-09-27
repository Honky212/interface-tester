# -*- coding: utf-8 -*-
"""与**接口文档同源**的本地 mock 服务 —— 给"生成 → 真跑一遍"用一个能响应的服务端。

## 它为什么存在（以及它**不是**什么）

生成出来的用例要"真跑"，就需要一个**照文档实现**的服务端。本文件从**文档本身**推导出：

- **路由**：`gen.interface_sections()` 认出的每个接口段的 `METHOD /path`；
- **响应体**：那一节里 `响应示例` 后面的 ```json 块（逐字用文档的例子）；
- 找不到示例时 → 用文档 §1.4 写明的**统一响应格式**信封
  `{"success": true, "errorCode": null, "message": "操作成功", "data": null}`。

★**同源 ≠ 同一份东西**：它只照**文档**写，**不看任何生成的用例**——否则"跑绿"就成了自我循环
（拿用例去凑服务端，再拿服务端证明用例对）。也**不假装**是完整实现：只保证"文档写了的形态"
（路径在、方法对、信封对、字段在）；文档没写的语义（幂等、鉴权、权限）一律不管。

用法：

    python probe_p2_prep/mock_file_service.py --port 8899        # 前台常驻，Ctrl-C 退出

只监听 **127.0.0.1**（与本仓 L3 口径一致：运行域默认只放行回环）。
"""

import argparse
import io
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.gen import interface_sections  # noqa: E402

DOC_DEFAULT = os.path.join(BASE, "project-three", "统一文件服务平台-接入应用接口文档.md")
FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)```", re.S)
ENVELOPE = {"success": True, "errorCode": None, "message": "操作成功", "data": None}

# 文档 §1.4 的统一响应格式（`FileResult`）：mock 的兜底信封 + 未知路径的失败信封
FAIL_ENVELOPE = {"success": False, "errorCode": "FILE_404", "message": "接口不存在", "data": None}


# ---------------------------------------------------------------------------
# ★§9.21 文档驱动的**错误条件引擎**（"负例步骤能不能真跑"的唯一瓶颈）
# ---------------------------------------------------------------------------
# 背景（实测）：模型产出的**负例步骤**（如「路径不匹配时返回错误码」期望 `FILE_1018`）一条也跑不绿，
# 因为 mock 只会照文档的**成功响应**应答 —— 失败的根因**不是用例错**，是"服务端没模拟错误条件"。
#
# ★做法：从文档里**推导**可判定的错误条件（不是硬编码错误码）。**通用性的边界在"条件族"，不在项目**：
#   每加一族，对所有**同措辞**的文档都生效；换一个被测项目**不需要改任何一行代码**（只换文档）。
#
#   ① `relation` 两个请求值之间的关系：「`path` 必须与请求头 `X-Path` 完全一致」（不一致 → FILE_1018）、
#      「`startTime` 大于 `endTime` 时返回 FILE_8002」。
#   ② `required` 非空/必填：「版本ID不能为空 → FILE_1007」——
#      ★**只在该字段于本节字段表里被标成必填时才触发**（否则"可选字段没传"会被误报 ✗）。
#   ③ `limit`    上限/数量：「单文件超过 50MB → FILE_1021」、「最多上传10个文件 → FILE_1016」。
#   ④ `exist`    存在性：「目录/文件/目标版本不存在 → FILE_2001/2003/2005」——
#      ★判据 = **mock 的种子数据就是文档示例里出现过的实体**（真服务上线时有种子数据，mock 用文档替代）；
#        请求里的实体不在种子数据里 → 按文档的"不存在"语义回失败。
#   ⑤ `format`   格式要求：「目录名称必须以/开头并以/结尾 → FILE_4001」、「开头和结尾不能有空格 → FILE_4018」、
#      「不能包含连续的/ → FILE_4003」、「文件名包含非法字符 → FILE_4016」。
#
#   ★值绑定一律"文件驱动"：先取**反引号里的名字**，再按**字段说明的用词**（「开始时间」→ `startTime`）——
#     **绝不**按项目硬编码字段名（本文件里 `X-Path`/`FILE_*` 只出现在注释与自检样例里）。
# ★**判不了的就点名**：要状态/业务状态的（已是当前版本、目录非空、同名已存在、部分失败、内部错误、
#   Header 格式不规范）一律进"未模拟清单"，启动时打印、报告里列出 —— **不许假装模拟了**。
RELATION_WORDS = (
    # (模式, 要求谓词, 是否"要求"式表述)
    # ★必须是"要求"与"违反"两种表述分开：文档写「**必须**与…完全一致」是**要求**，
    #   而 mock 要用的是**违反条件**（不一致才报错）→ 取反；而「`startTime` **大于** `endTime` 时返回 X」
    #   本身就是**违反条件** → 不取反。混为一谈的后果是"正例全红、负例全绿"（方向反了）。
    ("完全一致|完全相同|完全一样|保持完全一致|相同|相等|保持一致", "==", True),
    ("不一致|不相同|不同|不能相同", "!=", True),
    ("不能大于|不超过|不大于|必须小于等于|小于等于", "<=", True),
    ("不能小于|不低于|不小于|必须大于等于|大于等于", ">=", True),
    ("必须大于", ">", True),
    ("必须小于", "<", True),
    # ★裸的「大于/小于」放最后（前面更具体的词先匹配），且它是**违反条件**（不取反）
    ("大于", ">", False),
    ("小于", "<", False),
)
_NEGATE = {"==": "!=", "!=": "==", "<=": ">", ">=": "<", ">": "<=", "<": ">="}
_NOISE_WORDS = ("必须", "不能", "不超过", "不小于", "不低于", "保持一致", "相同", "一致")
# ★`{1,}` 不是 `{2,}`：错误码短到 `FILE_1` 也是合法的（实测：判据用小文档用一位码时，整个引擎
#   **一条候选都收不到** → 静默变成"没有条件" ✗ —— 这种"看起来没事、其实没工作"的形态最难查）
SENTENCE_CODE_RE = re.compile(r"(FILE_[A-Z0-9_]{1,}|code[:：]\s*(\d{3,}))")
BACKTICK_NAME_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_\-]*)`")
# 错误条件引擎**不碰**的句子（说明性文字，不是条件）
NOISE_RE = re.compile(r"例如|样例|示例|如\s*`?/app1", re.I)

# ---------------------------------------------------------------------------
# ★§9.22 新增四族：非空 / 上限 / 存在性 / 格式（全部**文档驱动**，无项目硬编码）
# ---------------------------------------------------------------------------

# ② 非空：句子里出现这些词才算"非空要求"
REQUIRED_WORDS = ("不能为空", "不可为空", "不能缺失", "必填")
# ③ 上限：`超过 50MB` / `最多上传10个文件`（★「最多」与数字之间常有动词：「最多**上传**10个**文件**」）
LIMIT_RE = re.compile(
    r"(?:超过|超出|大于)\s*(\d+(?:\.\d+)?)\s*(MB|GB|KB|B|字节|个|条|次|张)"
    r"|(?:最多|至多|不超过|不小于)\s*[^\d]{0,8}?(\d+(?:\.\d+)?)\s*(MB|GB|KB|B|字节|个|条|次|张)",
    re.I,
)
UNIT_BYTES = {"B": 1, "KB": 1024, "MB": 1024 * 1024, "GB": 1024 * 1024 * 1024}
COUNT_UNITS = ("个", "条", "次", "张")
# ④ 存在性
EXIST_WORDS = ("不存在", "未找到", "无法找到")
# ⑤ 格式：（正则, 判定式名, 说明）——"要求"式表述 → 判定式是**违反条件**
FORMAT_RULES = (
    (r"以/开头.{0,6}以/结尾|以/开头和结尾|以/开头并以/结尾", "slash_wrap", "必须以 / 开头并以 / 结尾"),
    (r"不能包含连续的/|不能包含连续斜杠", "no_double_slash", "不能包含连续的 /"),
    (r"非法字符|特殊字符", "charset", "不能包含 \\/:*?\"<>| 等特殊字符"),
    (r"开头和结尾不能有空格|首尾.{0,4}空格|不能以空格开头", "no_edge_space", "开头和结尾不能有空格"),
)
_CHARSET = set('\\/:*?"<>|')
# 实体词 → 字段名的**同义**（通用：中文里的"列表/数组"、"编号/ID"）
_SYNONYMS = (("列表", "数组"), ("编号", "ID"), ("标识", "ID"), ("名字", "名称"))
# 实体词里的**修饰语**（"目标版本" → "版本"、"指定的目录" → "目录"）
_ENTITY_NOISE = ("目标", "当前", "指定", "原有", "新的", "要", "该", "此")
# 实体词的**尾词**（"目录名称"/"文件名" → "目录"/"文件"，去尾后再按字段说明匹配）
_ENTITY_TAIL_WORDS = ("名称", "名字", "路径", "后缀", "大小")
# 必填列的取值（字段表的第 3 列）
_REQUIRED_CELLS = ("是", "必须", "必填", "必选", "required", "yes", "y", "true")


def _table_cells(sentence):
    """把错误码表的一行拆成单元格（`FILE_2001 | 目录不存在 | 指定的目录不存在` → 3 段）。

    ★必须要求**至少两个 `|`**：正文里也会出现单个 `|`（`文件名不能包含非法字符：\\/:*?"<>|`）——
      误当表格行的话，`cells[1]` 会变成「不能包含非法字符：…」这种半句话，
      实体词直接被切碎 → **白丢一条可判定条件**（实测踩到：`FILE_4016` 在重命名接口漏绑）。
    """
    text = sentence.strip().strip("|").strip()
    if text.count("|") < 2:
        return []
    return [cell.strip() for cell in text.split("|")]


def _entity_of(sentence, tails):
    """从「XX不存在」/「XX不能为空」里取出实体词 XX（优先用**错误码表的消息列**）。

    ★两处细节都是被实测逼出来的：① 句子可能带**项目符号**（`- 目录名称开头和结尾不能有空格`）→ 先剥；
      ② 关键字可能有**多个**（「目录名称**开头**和结尾**不能**有空格」）→ 取**最靠前**的那个切，
      否则切出来的实体词是「- 目录名称开头和结尾」这种 ✗（它匹配不到任何字段 → 白丢一条可判定条件）。
    """
    cells = _table_cells(sentence)
    head = cells[1] if len(cells) >= 2 else sentence
    head = re.sub(r"^[\s\-*·•\d.、)]+", "", head or "").strip()
    hits = [(head.find(tail), tail) for tail in tails if head.find(tail) > 0]
    if not hits:
        return ""
    index, _tail = min(hits)
    word = head[:index].strip(" `*：:的")
    word = re.split(r"[，,。；;（(]", word)[-1].strip()
    return re.sub(r"[和与及、]$", "", word).strip()


def _entity_words(entity):
    """实体词的**可匹配形态**（去修饰语 + 同义替换 + 去尾词）。"""
    words = {entity}
    for noise in _ENTITY_NOISE:
        words |= {word.replace(noise, "") for word in list(words)}
    for source, target in _SYNONYMS:
        words |= {word.replace(source, target) for word in list(words)}
    # 「目录名称」→「目录」、「文件路径」→「文件」：**去掉尾词**再试（顺序：长词优先）
    for tail in _ENTITY_TAIL_WORDS:
        words |= {word[: -len(tail)] for word in list(words) if word.endswith(tail)}
    return sorted((word for word in words if len(word) >= 2), key=len, reverse=True)


def _fields_for_entity(entity, index, *, skip_lists=False):
    """实体词 → 候选字段名（按**字段说明**里是否出现实体词打分；匹配不到 → 空）。

    `skip_lists=True` 时排除**列表字段**（存在性族用：`files` 这种"一批文件"不是"一个实体"）。
    """
    words = _entity_words(entity)
    scored = []
    for name, (_location, type_text, note) in index.items():
        if skip_lists and _is_list_field(name, type_text, note):
            continue
        note = note or ""
        best = 0
        for word in words:
            if word in note:
                best = max(best, len(word))
        if best:
            scored.append((best, name))
    if not scored:
        return []
    top = max(score for score, _ in scored)
    return [name for score, name in scored if score == top]


def _is_list_field(name, type_text, note):
    """是不是**列表**字段：看**文档的类型列**（`MultipartFile[]` / `Array` / `List`）为主。

    ★不用"名字以 s 结尾"这种猜法 —— 实测它会把 `status` 当成列表 ✗（`status`.endswith("s") 为真）。
    """
    if re.search(r"\[\]|array|list", type_text or "", re.I):
        return True
    return bool(re.search(r"数组|列表", note or ""))


# ---------------------------------------------------------------------------
# ★§9.26 上传路径：**multipart 里的真实文件大小**（"有没有大小这个事实"的分水岭）
# ---------------------------------------------------------------------------
# 为什么必须量它（实测 T3_1 形态）：文档写「单文件超过 50MB → FILE_1021」，
#   · 用例**真发了 multipart 文件** → 服务端**有**"大小"这个事实 → 该不该报错是**可判定**的 ✓；
#   · 用例只发文件名（`files=@report.pdf` / 查询参数）→ 服务端**无从判定** ✗
#     → 那时**不触发**才是对的（跑不绿**不是用例错**，是请求里没有事实）。
# 所以"上传路径"要真跑起来，缺的不是提示词，是**这条能把文件大小量出来的能力**。
_MULTIPART_HEAD = b"MIME-Version: 1.0\r\n"


def multipart_file_sizes(raw_body, content_type):
    """从 multipart 请求体里量出**每个部件**的字节数 → `[(字段名, 文件名, 大小字节)]`。

    只用标准库 `email` 解析（不引新依赖）；不是 multipart / 解析不出 → `[]`。
    """
    text = str(content_type or "")
    if "multipart/" not in text.lower() or "boundary=" not in text:
        return []
    boundary = text.split("boundary=", 1)[1].strip().strip('"').split(";")[0].strip()
    if not boundary:
        return []
    raw = raw_body if isinstance(raw_body, (bytes, bytearray)) else b""
    if not raw:
        return []
    import email.parser  # noqa: PLC0415 - 标准库，延迟 import 保持模块顶部干净

    head = f"Content-Type: multipart/form-data; boundary={boundary}\r\n".encode() + _MULTIPART_HEAD
    head += b"\r\n"
    try:
        message = email.parser.BytesParser().parsebytes(head + raw)
    except Exception:  # noqa: BLE001 - 解析不了就当"没有这个事实"（判不了就不触发）
        return []
    parts = []
    for part in message.walk():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        parts.append((str(name), str(part.get_filename() or ""), len(payload)))
    return parts


def _row_index(section_text):
    """该节的 `请求字段/头`：`名字 → (location, 类型, 说明)`（复用语料层的字段表解析，避免另一套口径）。

    ★只收**请求侧**的行（**有「必填」列**的那种）：响应数据表是 `字段名｜类型｜说明`，没有必填列 ——
      判据与产品 `assertions` 的 B2 口径**同一个**。不过滤的话，响应字段（`dirPath`/`status`/…）
      会被绑成"请求字段"，报告里的绑定就是错的 ✗。
    ★带上**类型列**：它是判断"这个字段是不是列表/大小"的**文档依据**（比猜字段名可靠 ✓）。
    """
    from interfacetester_ai.assertions import read_field_rows  # noqa: PLC0415

    index = {}
    for row in read_field_rows(section_text):
        if row.name and row.required_text:
            index.setdefault(row.name, (row.location, row.type_text or "", row.note or ""))
    return index


def _doc_exceptions(doc_text):
    """文档写明的**例外接口**：`{接口名: {"codes": {...}, "op": "startswith"|""}}`。

    例：「…唯一例外是 **创建目录** 接口，其 `path`（新目录路径）只需以 `X-Path` 开头即可」
    → `{"创建目录": {"codes": {"FILE_1018"}, "op": "startswith"}}`：
    该接口的这条要求**放宽成前缀**（不再要求完全一致）。

    ★为什么必须读它：不读就会把"文档允许的写法"判成错误 → **假失败** ✗
      （实测：确定性通路 `_api_directory_create` 由绿转红）。
    ★为什么不是"直接丢掉这条规则"：放宽后的条件**仍可判定**（不以 X-Path 开头 → 仍要报错）——
      丢掉等于把可模拟的能力扔掉 ✗。
    """
    exceptions = {}
    for line in (doc_text or "").split("\n"):
        if not re.search(r"例外|除外|不适用", line):
            continue
        # ★按**行**读，不按分句：实测「…否则会返回 `FILE_1018` 错误。唯一例外是 **创建目录** 接口…」
        #   把码和豁免拆到了两句话里 —— 按分句处理会拿到**空码集** → 豁免形同不存在 ✗
        codes = set()
        for match in SENTENCE_CODE_RE.finditer(line):
            codes.add(match.group(0).split(":")[-1].strip())
        op = "startswith" if "开头" in line else ""
        # 方向从**「例外」之后的小句**里取（`path` … 只需以 `X-Path` 开头）；
        # 取整行的话 backtick 名单里会先混进"要求句"里的字段 ✗（实测：方向被搞反）
        clause = line[line.find("例外") :].split("，", 1)[-1]
        names = BACKTICK_NAME_RE.findall(clause) or BACKTICK_NAME_RE.findall(line)
        subject, prefix = (names[0], names[1]) if len(names) >= 2 else ("", "")
        for name in re.findall(r"\*\*(.+?)\*\*", line):
            name = name.strip()
            if name:
                exceptions.setdefault(
                    name, {"codes": set(), "op": op, "subject": subject, "prefix": prefix}
                )["codes"] |= codes
    return exceptions


def _compare(left, right, op):
    """按谓词比较两个**请求值**（数字优先；ISO 时间串按字典序比较同样正确）。"""
    if op == "startswith":  # ★放宽形态：**违反条件** = 不已 right 开头
        return not str(left).startswith(str(right))

    def key(text):
        try:
            return float(text)
        except (TypeError, ValueError):
            return str(text)

    try:
        first, second = key(left), key(right)
        return {
            "==": first == second,
            "!=": first != second,
            "<=": first <= second,
            ">=": first >= second,
            ">": first > second,
            "<": first < second,
        }[op]
    except (TypeError, KeyError):  # 数字与字符串混比 → **判不了就不触发**（不乱报）
        return False


def _bindable_candidates(doc_text):
    """**全篇**候选句子：`(句子, 错误码, 关系词, 谓词)` —— 含**公共错误码表**那种行
    （如 `| FILE_8002 | 开始时间不能大于结束时间 | … |`，它不在任何接口节里，
    但"开始时间/结束时间"这两个**说明**能在具体接口的字段表里找到 → 应绑到那些接口上）。"""
    out = []
    for sentence in re.split(r"[\n。；;]", doc_text or ""):
        sentence = sentence.strip().lstrip("-*#> |").strip().rstrip("|").strip()
        if not sentence or NOISE_RE.search(sentence):
            continue
        code_hit = SENTENCE_CODE_RE.search(sentence)
        if not code_hit:
            continue
        relation = next(
            (
                (word, (_NEGATE[op] if obligation else op))
                for word, op, obligation in RELATION_WORDS
                if re.search(word, sentence)
            ),
            None,
        )
        if relation is not None and _negated_context(sentence, relation[0]):
            # ★证句自带否定（「源目录名称和目标目录名称**不能**相同」）→ 方向要再反一次，
            #   否则「不同」会被判成错误、「相同」反而放过 —— 方向完全反了（实测踩到 FILE_4009）。
            relation = (relation[0], _NEGATE[relation[1]])
        code = code_hit.group(0).split(":")[-1].strip()
        # ★没有关系词也要**收进来**（带空关系词）——它们进不了绑定，但必须出现在
        #   「**不模拟**」清单里（否则"含错误码的条件"会**静默消失** ✗）。
        out.append((sentence, code, relation[0] if relation else "", relation[1] if relation else ""))
    return out


def build_error_rules(doc_text):
    """→ `(rules_by_route, unbound)`：可判定的错误条件（**五族**） + **点名**不能模拟的。

    `rules_by_route[(METHOD, path)] = [规则…]`（规则形状见 `_build_rule`）；
    `unbound = [(path, 句子)]` —— 含错误码但**本 mock 判不了**的条件（逐条点名，不许假装）。

    ★两趟：① 每个接口**自己的**句子；② **全篇候选**（公共错误码表）按各节字段表**逐节试绑**
      —— 判不了就不绑（宁可不模拟，也不乱报）。
    """
    lines = doc_text.split("\n")
    rules_by_route = {}
    unbound = []
    candidates = _bindable_candidates(doc_text)
    exceptions = _doc_exceptions(doc_text)  # ★文档显式豁免的接口（豁免句不绑、也不判"以 / 结尾"）
    for section in interface_sections(doc_text):
        if not section.method or not section.url:
            continue
        exempt = next((item for name, item in exceptions.items() if name in (section.title_path or "")), None)
        text = "\n".join(lines[section.start_line - 1 : section.end_line])
        pure = section.url.split("?")[0].rstrip("/") or "/"
        key = (section.method.upper(), pure)
        index = _row_index(text)
        rules = rules_by_route.setdefault(key, [])
        seen = {rule["code"] for rule in rules}
        # ① 本节自己的句子（含反引号名字那种，如「`path` 必须与请求头 `X-Path` 完全一致」）
        for sentence, code, word, op in candidates:
            if sentence not in text:
                continue
            rule = _build_rule(sentence, code, word, op, index, text, exempt)
            if rule is None or _has_rule(rules, rule):
                continue  # ★同一句话在文档里可能出现多次（每个接口都抄一遍）→ 只绑一次
            rules.append(rule)
            seen.add(code)
        # ② **全篇候选**（公共错误码表）：按**字段说明**试绑（节里没有那条原文）
        for sentence, code, word, op in candidates:
            if sentence in text or code in seen:
                continue
            rule = _build_rule(sentence, code, word, op, index, text, exempt)
            if rule is None:
                continue
            rules.append(rule)
            seen.add(code)
        # ③ **点名**：含错误码、但绑不上的条件（本 mock 不模拟）
        for sentence, code, _word, _op in candidates:
            if sentence in text and not any(rule["quote"] == sentence for rule in rules):
                unbound.append((pure, sentence))
        # ④ 方向冲突的关系规则 → 留硬证据那条（见 `_drop_weak_conflicts`）
        rules_by_route[key] = _drop_weak_conflicts(rules)
    # ④ **不许静默消失**：公共错误码表里那些"哪个接口都没绑上"的条件，也要点名。
    #    ★实测踩到过：`FILE_1016`（最多上传10个文件）因为正则没覆盖"最多**上传**10个"而绑定失败，
    #      它既不在接口节里（不被①③收），也没进不模拟清单 → **整条条件凭空消失** ✗
    bound_quotes = {rule["quote"] for items in rules_by_route.values() for rule in items}
    listed = {sentence for _where, sentence in unbound}
    for sentence, _code, _word, _op in candidates:
        if sentence in bound_quotes or sentence in listed:
            continue
        unbound.append(("(全篇未绑上)", sentence))
    return rules_by_route, unbound


def _has_rule(rules, rule):
    """同一路由里是否已有**等价**规则（同族 + 同码 + 同绑定，不比 `quote`/`exact`）。"""
    return any(
        all(
            existing.get(key) == rule.get(key)
            for key in (set(existing) | set(rule)) - {"quote", "exact"}
        )
        for existing in rules
    )


def _drop_weak_conflicts(rules):
    """同一对字段上**方向冲突**的关系规则：留硬证据（整段说明原样出现）那条，丢软证据（前缀兜底）的。

    ★为什么必须有它（实测）：文档同时写了
      「新文件名不能与原文件名相同 → FILE_4013」（两侧都**原样**命中说明 ✓）和
      「新文件**后缀**必须与原文件保持一致 → FILE_4014」（靠前缀兜底、落在**同一对字段**上，方向相反 ✗）。
      两条都在文档里，但后者是**绑错了**：它说的是"后缀"，而重命名接口根本没有后缀字段 ✗。
      不处理 → 正常的重命名请求被判 `FILE_4014` ✗（确定性通路由绿转红）。
    """
    by_pair = {}
    for rule in rules:
        if rule.get("kind") == "relation":
            by_pair.setdefault(frozenset((rule["left"], rule["right"])), []).append(rule)
    drop = set()
    for items in by_pair.values():
        if len({rule["op"] for rule in items}) < 2:
            continue  # 方向一致 → 不冲突（可能只是同一对字段的两条不同码）
        if any(rule.get("exact") for rule in items):
            drop |= {id(rule) for rule in items if not rule.get("exact")}
    return [rule for rule in rules if id(rule) not in drop]


def _negated_context(sentence, pattern):
    """关系词**前面**是不是否定（「…**不能**相同」）——是的话，要求方向要再反一次。

    ★不反的后果（实测）：`源目录名称和目标目录名称不能相同` 被绑成 `source != target`，
      于是**正确请求**（两者不同）被判成错误 ✗，而**违反请求**（两者相同）反而放过 ✗。
    """
    matched = re.search(pattern, sentence)
    if matched is None:
        return False
    return bool(re.search(r"不能|不得|不允许|禁止|严禁|不可|勿", sentence[: matched.start()][-8:]))


def _build_rule(sentence, code, word, op, index, text, exempt=None):
    """按族依次试建一条规则：**关系 → 非空 → 上限 → 存在性 → 格式**（更具体的族先试）。

    `exempt` 是**本节被文档显式豁免**的那条要求（见 `_doc_exceptions`；`None` = 没豁免）：
    豁免句本身不绑，被豁免的那条要求**放宽**（如"完全一致"→"以…开头"），且不再判"目录名以 / 结尾"。

    判不了 → `None`（调用方会把它算进"不模拟"清单，**绝不乱报**）。
    """
    if exempt and re.search(r"例外|除外|不适用", sentence):
        return None
    if exempt and exempt.get("op") == "startswith" and code in exempt.get("codes", ()):
        # ★豁免句自己写明了放宽后的**方向**（「其 `path` … 只需以 `X-Path` 开头」）：
        #   按它建规则，而不是按要求句里的字段顺序 —— 顺序反了会变成"X-Path 以 path 开头" ✗
        subject, prefix = exempt.get("subject", ""), exempt.get("prefix", "")
        if subject in index and prefix in index:
            return {
                "kind": "relation",
                "left": subject,
                "right": prefix,
                "op": "startswith",
                "code": code,
                "quote": sentence,
            }
        return None
    # ① relation：两个请求值之间的关系（反引号名字优先，再按字段说明切句子）
    if word:
        bound = _bind_pair(sentence, word, BACKTICK_NAME_RE.findall(sentence), index)
        if bound is not None and bound[0] != bound[1]:
            # ★同字段自比（`dirName != dirName`）是**绑错了**：恒不成立/恒成立，白占一条 ✗
            return {
                "kind": "relation",
                "left": bound[0],
                "right": bound[1],
                "op": op,
                "exact": bound[2],
                "code": code,
                "quote": sentence,
            }
    # ② required：非空要求（★只绑**本节标成必填**的字段——否则"可选字段没传"会被误报）
    if any(item in sentence for item in REQUIRED_WORDS):
        entity = _entity_of(sentence, REQUIRED_WORDS)
        fields = [
            name for name in _fields_for_entity(entity, index) if _is_required_field(name, text)
        ]
        if fields:
            return {
                "kind": "required",
                "field": fields[0],
                "entity": entity,
                "code": code,
                "quote": sentence,
            }
    # ③ limit：上限（数量 / 大小）
    limit = _limit_of(sentence, index)
    if limit is not None:
        limit.update({"kind": "limit", "code": code, "quote": sentence})
        return limit
    # ④ exist：存在性（值不在 mock 的**种子数据**里 → 按文档语义回失败）
    #    ★`skip_lists`："一批文件"不是"一个实体"（`files` 不该被"文件不存在"绑走）
    #    ★`_entity_kind` 形状闸：「目录不存在」只绑**目录类**字段、「文件不存在」只绑**文件类**字段 ——
    #      不闸的话「目录不存在」和「文件不存在」都会绑到 `path` 上，谁先判谁赢 ✗
    #      （实测：不存在的**目录**被回成 FILE_2003 而不是 FILE_2001）
    if any(item in sentence for item in EXIST_WORDS):
        entity = _entity_of(sentence, EXIST_WORDS)
        kind = _entity_kind(entity)
        fields = [
            name
            for name in _fields_for_entity(entity, index, skip_lists=True)
            if kind == "" or _field_kind(name, index[name][2]) in (kind, "")
        ]
        if fields:
            return {
                "kind": "exist",
                "fields": fields,
                "entity": entity,
                "code": code,
                "quote": sentence,
            }
    # ⑤ format：格式要求（"要求"式表述 → 判定式是**违反条件**）
    for pattern, check, _desc in FORMAT_RULES:
        if not re.search(pattern, sentence):
            continue
        if exempt and check == "slash_wrap":
            continue  # 本节被文档豁免（创建目录的 path 只需以 X-Path 开头）
        entity = _entity_of(sentence, ("必须", "不能", "不可", "包含", "开头", "结尾"))
        fields = _fields_for_entity(entity, index) if entity else []
        # ★文档**自己的示例值**都违反这条格式要求 → 这条要求不是针对这个字段的，别绑。
        #   实测：`dirName`（说明「目录名称（模糊查询）」）的示例值是 `doc` ✗，而错误码表写着
        #   「目录名称必须以/开头并以/结尾 → FILE_4001」——照绑的话，**文档自己的例子**会被判错误 ✗✗
        #   （实测：查询目录的用例由绿转红）。同 §9.17 的纪律：文档自相矛盾时，以**示例**为准。
        fields = [name for name in fields if not _example_violates(name, text, check)]
        if fields:
            return {
                "kind": "format",
                "field": fields[0],
                "check": check,
                "entity": entity,
                "code": code,
                "quote": sentence,
            }
    return None


def _entity_kind(entity):
    """实体是**目录类**还是**文件类**（判存在性时用它给字段分形状）。"""
    text = entity or ""
    if "目录" in text:
        return "dir"
    if "文件" in text:
        return "file"
    return ""


def _field_kind(name, note):
    """字段是**目录类**还是**文件类**（看**文档的说明列**，不猜业务）。"""
    text = f"{name} {note or ''}"
    if "目录" in text or re.search(r"dir", name or "", re.I):
        return "dir"
    if "文件" in text or re.search(r"file", name or "", re.I):
        return "file"
    return ""


def _example_violates(name, section_text, check):
    """文档里这个字段的**示例值**是不是违反了这条格式要求（`""` = 没示例 → 不算违反）。

    ★判它有什么用：文档有时自相矛盾（错误码表说"目录名称必须以/开头并以/结尾"，
      而查询接口的 `dirName` 示例值偏偏是 `doc`）——照绑会把**文档自己的例子**判成错误 ✗。
    """
    example = _field_example(name, section_text)
    return bool(example) and _format_violated(example, check)


def _field_example(name, section_text):
    """字段表那一行的**示例值**（取最后一列里像取值的那格；没有 → `""`）。"""
    pattern = re.compile(r"^\|\s*`?" + re.escape(name) + r"`?\s*\|")
    for line in (section_text or "").split("\n"):
        if not pattern.match(line.strip()):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        for cell in reversed(cells[2:]):
            if cell and cell not in ("-", "是", "否"):
                return cell
    return ""


def _is_required_field(name, section_text):
    """该字段在**本节字段表**里是不是必填（第 3 列 ∈ 是/必须/…）。

    ★为什么必须有这道闸：文档写「版本不能为空」，但版本在**预览**接口里是可选参数（不传=最新版）——
      不加闸就会把"合法的不传"误报成错误 ✗。
    """
    pattern = re.compile(r"^\|\s*`?" + re.escape(name) + r"`?\s*\|")
    for line in (section_text or "").split("\n"):
        if not pattern.match(line.strip()):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[2].lower() in _REQUIRED_CELLS:
            return True
    return False


def _limit_of(sentence, index):
    """上限条件 → `{"field", "threshold", "unit", "per_item"}`（判不了 → None）。"""
    match = LIMIT_RE.search(sentence)
    if match is None:
        return None
    if match.group(1) is not None:
        amount, unit = float(match.group(1)), match.group(2).upper()
    else:
        amount, unit = float(match.group(3)), match.group(4).upper()
    if unit in COUNT_UNITS:
        # 数量上限 → 绑**列表字段**（按文档的**类型列**判，别猜名字）
        lists = _list_fields(index)
        if not lists:
            return None
        return {"field": lists[0], "threshold": amount, "unit": "count", "per_item": True}
    # 大小上限 → 优先绑**说明里写明"大小/字节"的字段**；没有就绑列表字段
    # （请求里若给不出大小 → 不可判 → **不触发**，绝不乱报）
    bytes_limit = amount * UNIT_BYTES.get(unit, 1)
    per_item = "单文件" in sentence or "每个" in sentence or "每张" in sentence
    sizes = [
        name
        for name, (_location, _type, note) in index.items()
        if re.search(r"大小|字节", note or "")
    ]
    if sizes:
        return {"field": sizes[0], "threshold": bytes_limit, "unit": "size", "per_item": per_item}
    lists = _list_fields(index)
    if not lists:
        return None
    return {"field": lists[0], "threshold": bytes_limit, "unit": "size", "per_item": True}


def _list_fields(index):
    """看起来是**列表**的请求字段（口径只有一处：`_is_list_field`）。"""
    return [name for name, (location, type_text, note) in index.items() if _is_list_field(name, type_text, note)]


def build_known_values(doc_text):
    """mock 的**种子数据** = 文档里出现过的实体值（JSON 示例里的字符串 + 表格「示例值」列）。

    ★为什么存在性判定能通用：真服务上线时有种子数据；mock 用**文档示例**替代它。
      于是"请求里的实体不在种子数据里 → 按文档的『XX不存在』语义回失败"这条规则
      与**项目无关** —— 换一份文档，种子数据自动跟着换。
    """
    known = set()
    for match in FENCE_RE.finditer(doc_text or ""):
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            continue
        known |= _string_leaves(payload)
    # 表格各列里**像取值**的单元格（示例值列那种；`文件名`/`完整路径` 这类说明文字不算）
    for line in (doc_text or "").split("\n"):
        for cell in _table_cells(line):
            value = cell.strip().strip("`")
            if _looks_like_value(value):
                known.add(value)
    # 反引号里的**路径/文件名/版本号**形态
    for token in re.findall(r"`([^`\n]{2,60})`", doc_text or ""):
        token = token.strip()
        if token and ("/" in token or "." in token or re.fullmatch(r"v\d+", token, re.I)):
            known.add(token)
    return known


def _looks_like_value(text):
    """像"一个实体值"吗？（路径/文件名/编号/版本号）——纯中文说明词、纯标点都不算。

    ★为什么要滤：种子数据混进说明文字（`完整路径`/`修改人`）会让"不存在"永远判不出来 ✗；
      混进表格分隔符（`-----`）则是最典型的脏数据。
    """
    text = (text or "").strip()
    if not text or len(text) > 60 or re.fullmatch(r"[-|:\s]+", text):
        return False
    return bool(re.search(r"[/\.\d]", text) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\-]{2,}", text))


def is_condition_statement(sentence):
    """这句话是**条件陈述**，还是**示例 JSON 里的碎片**（`"errorCode": "FILE_2003",`）？

    ★分它只为**报告口径**：两者都算"不模拟"，但不能混在一起数——碎片的"不模拟"是**必然**的，
      条件陈述的"不模拟"才是**能力边界**（要状态/业务状态/请求里给不出的事实）。
    """
    text = (sentence or "").strip()
    if re.fullmatch(r"`?FILE_[A-Z0-9_]+`?", text):
        return False
    if text.startswith('"') or '"errorCode"' in text or re.match(r"^[\w\"]+\s*[:：]\s*[\"{\[]", text):
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _string_leaves(payload):
    """递归取 JSON 里的**字符串叶子**（数字/布尔不算实体）。"""
    if isinstance(payload, str):
        return {payload}
    if isinstance(payload, dict):
        out = set()
        for value in payload.values():
            out |= _string_leaves(value)
        return out
    if isinstance(payload, list):
        out = set()
        for value in payload:
            out |= _string_leaves(value)
        return out
    return set()


def _bind_pair(sentence, word, names, index):
    """把一句话绑成**两个请求值** `(left, right, exact)`：先按反引号里的名字，再按字段**说明**的用词。

    `exact=True` = 两侧都是**整段说明原样出现**（硬证据）；`False` = 用了前缀兜底（软证据）。
    ★这个标记有用：同一对字段上若出现**方向相反**的两条规则 → 留硬证据那条（见 `_drop_weak_conflicts`）。
    """
    cells = _table_cells(sentence)
    if cells:
        # ★错误码表的行会走到这里：**先把"错误码"那一格去掉** —— 留着的话左半句变成
        #   「`FILE_4091` | 源目录名称…」，任何字段说明都对不上 → 白丢一条可判定条件 ✗（实测踩到）
        sentence = "，".join(cells[1:])
    known = [name for name in names if name in index]
    if len(known) >= 2:
        return known[0], known[1], True
    if len(known) == 1:
        for name, (_location, _type, note) in index.items():
            if name != known[0] and note and note in sentence:
                return known[0], name, True
    # 没有反引号名字（如错误码表的「开始时间不能大于结束时间」）→ 按**说明**把句子切成两半
    matched = re.search(word, sentence)
    if not matched:
        return None
    head, _sep, tail = sentence.partition(matched.group(0))
    left, left_exact = _match_by_note(head, index)
    right, right_exact = _match_by_note(tail, index)
    if left and right:
        return left, right, (left_exact and right_exact)
    # ★「A 和/与 B 不能相同」这类：关系词在**句尾**，两个实体在**前半句**里由连词分开
    #   （实测：源目录名称 != 目标目录名称、新文件名 != 原文件名 —— 不这样切就白丢两条可判定条件）
    for separator in ("和", "与", "及", "、"):
        if separator not in head:
            continue
        first, _sep2, second = head.partition(separator)
        first_hit, first_exact = _match_by_note(first, index)
        second_hit, second_exact = _match_by_note(second, index)
        if first_hit and second_hit:
            return first_hit, second_hit, (first_exact and second_exact)
    return None


def _match_by_note(text, index, *, min_shared=2):
    """在句子的某一半里，找**说明文字**对得上的那个字段（如「开始时间」→ `startTime`）。

    ★两级匹配（都是被实测逼出来的）：
      ① **整段说明出现**（`fragment in text`）——最可靠，直接命中；
      ② 退一步：把这半句**去掉尾词/修饰语**后，看它是不是某个字段说明的**前缀**
         ——同一份文档里"同一件事"会换词写：`sourceDirName` 的说明是「源目录**完整路径**」，
         而证句里写的是「源目录**名称**」 ✗（不这样匹配就白丢"源目录 != 目标目录"这条可判定条件）。
      ★为什么不用"最长公共子串"：它对**短说明**太宽松 —— 「新文件**后缀**必须与原文件保持一致」
        会跟 `newFileName`（说明「新文件**名**」）对上，把"后缀必须一致"错绑成"文件名必须不同" ✗
        （实测：正常的重命名请求被判 `FILE_4014`）。前缀判据不会：那半句去掉尾词后是
        「新文件后缀必须与原文件」，没有任何说明以它开头 ✓。

    返回 `(字段名, exact)`；`exact=False` 表示用了**前缀兜底**（软证据）。
    """
    for name, (_location, _type, note) in index.items():
        for fragment in re.split(r"[，,、（(）)]", note or ""):
            fragment = fragment.strip()
            if len(fragment) >= 2 and fragment in text:
                return name, True
    words = _entity_words(_strip_stopwords(text))
    best = (0, None)
    for name, (_location, _type, note) in index.items():
        for word in words:
            if len(word) >= min_shared and (note or "").startswith(word):
                if len(word) > best[0]:
                    best = (len(word), name)
    return best[1], False


def _strip_stopwords(text):
    """去掉句尾的**要求用语**（「…不能」「…必须」「…的」）—— 只留实体，便于前缀匹配。"""
    pattern = re.compile(r"(不能|不可|不得|不允许|必须|需要|应当|的|和|与|及|或)+$")
    previous = None
    while previous != text:
        previous, text = text, pattern.sub("", text or "")
    return text


def _request_values(handler, body, name):
    """从**收到的请求**里取一个名字的值（头 → 请求体 → 查询串）。"""
    for header, value in handler.headers.items():
        if header.lower() == name.lower():
            return value
    if isinstance(body, dict) and name in body:
        return body[name]
    for pair in (handler.path.partition("?")[2] or "").split("&"):
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        if key == name:
            return unquote(value)
    return None


def _request_values_all(handler, body, name):
    """把一个名字的**所有**取值取出来（列表字段可能一次给多个：`files=a&files=b`）。"""
    values = []
    for header, value in handler.headers.items():
        if header.lower() == name.lower():
            values.append(value)
    if isinstance(body, dict) and name in body:
        item = body[name]
        values.extend(item if isinstance(item, list) else [item])
    for pair in (handler.path.partition("?")[2] or "").split("&"):
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        if key == name:
            values.append(unquote(value))
    return values


def _looks_like_reference(value):
    """`@report.pdf` / `${ENV(X)}` 这类是**引用**（客户端把文件/变量交给框架去解析），不是实体值。"""
    return isinstance(value, str) and (value.startswith("@") or "${" in value or value.startswith("$"))


def _size_of(value):
    """尽力把一个取值解释成**字节数**（数字 / `50MB` 这种串 / 带 `size` 的对象）；判不了 → None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(MB|GB|KB|B)?\s*", value, re.I)
        if match:
            unit = (match.group(2) or "B").upper()
            return float(match.group(1)) * UNIT_BYTES.get(unit, 1)
        return None
    if isinstance(value, dict):
        for key in ("size", "fileSize", "length", "bytes"):
            if key in value:
                return _size_of(value[key])
    return None


def _format_violated(value, check):
    """格式族：**要求**式表述 → 这里判的是**违反条件**（成立就回失败）。"""
    if check == "slash_wrap":
        return not (value.startswith("/") and value.endswith("/"))
    if check == "no_double_slash":
        return "//" in value
    if check == "charset":
        return any(char in _CHARSET for char in value)
    if check == "no_edge_space":
        return value != value.strip()
    return False


# ★§9.33（登记项 2）：同一请求**同时满足多族条件**时的裁决顺序。
#   改动前是**纯按构建顺序**（谁先建出来谁先判），于是 `format` 常抢在 `exist` 前面报出去 ——
#   「目录名格式不对」盖住了「目录不存在」，而后者是**前置**：不存在就谈不上格式。
#   顺序即语义：非空 → 存在性 → 上限 → 关系 → 格式。**同族内保持构建顺序**（稳定排序）。
KIND_PRIORITY = ("required", "exist", "limit", "relation", "format")

# ★§9.33（登记项 2）：**失败响应的 HTTP 状态**必须以文档为准。
#   实测两派写法**都真实存在**（不是笔误）：
#     · 示例文档：「失败响应返回 `errorCode: FILE_1018`（**HTTP 状态码仍为 `200`**）」
#     · 转换文档（`接口文档示例转换文档测试1.md:180`）：「…（**HTTP `500`**，依据 TC-DIR-003）」
#   改动前 mock **一律回 200** —— 文档写 500 时，产物里那条 `status_code` 断言**必红**，
#   而根因是"mock 没照文档实现"，**不是用例错**（最坏的一种红：怪错了人）。
#   口径：**引用句里写了就用它**（逐条最精确）→ 退到本节 → 再退到 200（示例文档写明的默认）。
_HTTP_STATUS_RE = re.compile(r"HTTP\s*状态码?\s*`?(\d{3})|HTTP\s*`?(\d{3})", re.I)


def documented_failure_status(quote, section_text="", default=200):
    """取文档写明的失败 HTTP 状态：**引用句** → **本节** → `default`（示例文档的默认 `200`）。"""
    for text in (quote or "", section_text or ""):
        found = _HTTP_STATUS_RE.search(text)
        if found:
            return int(found.group(1) or found.group(2))
    return default


def _normalize_entity(value):
    """判"这个实体在不在种子数据里"之前的归一化：**首尾空白 + 大小写**（只做这两样）。

    ★§9.33（登记项 2）的根因就在这：`" /app1/documents/ "` 与种子值 `"/app1/documents/"`
    只差首尾空格 —— 那是**写错**（该报格式错），不是**另一个实体**（才该报不存在）。
    不归一化会出现"怪错规则"：文档明写「开头和结尾不能有空格 → `FILE_4018`」，
    mock 却回 `FILE_2001`（目录不存在）—— 从而把 `format` 族的成对证明**打成假绿/假红**。
    """
    return value.strip().casefold()


def _by_kind_priority(rules):
    """按 `KIND_PRIORITY` **稳定**排序（同族内保持构建顺序，便于阅读与复现）。"""
    order = {name: index for index, name in enumerate(KIND_PRIORITY)}
    return sorted(
        rules or (), key=lambda rule: order.get(rule.get("kind", "relation"), len(order))
    )


def evaluate_error_rules(rules, handler, body, known=None):
    """命中第一条**成立**的条件 → `(错误码, 原句)`；都不成立 → `(None, "")`。

    ★五族各判各的；**值不齐一律算"本次不可判"**（不是"没触发"，也不报）—— 宁可漏报，绝不乱报。
    ★§9.33（登记项 2）：裁决按 `KIND_PRIORITY`（`exist` 优先于 `format`），不再纯按构建顺序。
    """
    if known is None:
        known = KNOWN_VALUES  # 不给就取启动时从文档推出来的**种子数据**（真服务上线时它=真数据）
    # ★§9.33：种子也按同一口径归一化（否则"只差空格"的值仍会被判成不存在）
    known_norm = {_normalize_entity(item) for item in known if isinstance(item, str)}
    for rule in _by_kind_priority(rules):
        kind = rule.get("kind", "relation")
        if kind == "relation":
            left = _request_values(handler, body, rule["left"])
            right = _request_values(handler, body, rule["right"])
            if left is None or right is None:
                continue  # 值不齐 → 这条条件这次**不可判**
            if _compare(left, right, rule["op"]):
                return rule["code"], rule["quote"]
        elif kind == "required":
            if _request_values(handler, body, rule["field"]) in (None, "", [], {}):
                return rule["code"], rule["quote"]
        elif kind == "limit":
            values = _request_values_all(handler, body, rule["field"])
            if rule["unit"] == "count":
                if len(values) > rule["threshold"]:
                    return rule["code"], rule["quote"]
            else:
                sizes = [_size_of(value) for value in values]
                # ★§9.26：把 multipart 里**同名字段的真实文件大小**也算进来 ——
                #   只有真发了文件才有这个事实；只给文件名时这里为空 → **不触发**（正确答案）
                content_type = getattr(handler, "_content_type", "")
                raw_body = getattr(handler, "_raw_body", b"")
                sizes += [
                    size
                    for name, _filename, size in multipart_file_sizes(raw_body, content_type)
                    if name == rule["field"]
                ]
                for size in sizes:
                    if size is not None and size > rule["threshold"]:
                        return rule["code"], rule["quote"]
        elif kind == "exist":
            for field in rule["fields"]:
                for value in _request_values_all(handler, body, field):
                    if not isinstance(value, str) or _looks_like_reference(value):
                        continue
                    if _normalize_entity(value) not in known_norm:
                        return rule["code"], rule["quote"]
        elif kind == "format":
            for value in _request_values_all(handler, body, rule["field"]):
                if isinstance(value, str) and _format_violated(value, rule["check"]):
                    return rule["code"], rule["quote"]
    return None, ""


def build_routes(doc_text):
    """`{(METHOD, path): 响应体}` —— 全部从文档推导。"""
    lines = doc_text.split("\n")
    routes = {}
    for section in interface_sections(doc_text):
        if not section.method or not section.url:
            continue
        text = "\n".join(lines[section.start_line - 1 : section.end_line])
        routes[(section.method.upper(), section.url.split("?")[0].rstrip("/") or "/")] = (
            _response_body(text)
        )
    return routes


# ★§9.33：示例围栏的**归属判据**词表（`_fence_context` / `_pick_json_fence` 用）
_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$")
RESPONSE_WORDS_RE = re.compile(r"响应|返回|response|result", re.I)
REQUEST_WORDS_RE = re.compile(r"请求|入参|参数|request|payload", re.I)
FAIL_WORDS_RE = re.compile(r"失败|错误|error|fail", re.I)
SUCCESS_WORDS_RE = re.compile(r"成功|success", re.I)


def _fence_context(section_text, start):
    """围栏**之前**的上下文 → `(最近的小节标题, 最近的一行"标签", 紧邻 160 字)`。

    ★为什么不能只看"前面 160 字里有没有『响应』"（§9.33 登记项 1 的真现场）：
      请求体表的「说明」列里只要出现"响应/返回"两字，它前面的**请求体示例**就会被当成
      响应示例回给用例 —— 症状是**断言全 `check_value: None`**（不是报错，是**静默假失败**），
      而且会毁掉此后所有负例探针的可信度。
      判据必须**先看归属**（"这一节/这行标签到底在讲请求还是响应"），再退到就近说明。
    """
    head = (section_text or "")[:start]
    heading = ""
    label = ""
    for line in head.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        found = _HEADING_RE.match(stripped)
        if found:
            heading = found.group(1).strip()
            label = ""  # 标题之后、围栏之前的那行才是"标签"
        else:
            label = stripped.strip("*`：: ")
    return heading, label, head[-160:]


def _iter_json_fences(section_text):
    """本节的 JSON 围栏 → `(payload, heading, label, window)`（解析不了的跳过）。"""
    for match in FENCE_RE.finditer(section_text or ""):
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        heading, label, window = _fence_context(section_text, match.start())
        yield payload, heading, label, window


def _pick_json_fence(section_text, want, *, avoid=None):
    """按**归属优先**的阶梯挑一个示例围栏（§9.33 修的就是"只有第 ③ 步"的那个旧口径）。

    阶梯（**顺序即口径**）：

    ① 紧邻的**标签行**命中 `want`（`成功响应示例：` / `失败响应：`）→ 就是它（最强信号）；
    ② 标签行命中 `avoid`（`请求体示例：`）→ **排除**（哪怕说明里提到"响应"两字）；
    ③ 小节**标题**命中 `want` → 认定；
    ④ 标题命中 `avoid` → 排除；
    ⑤ 都没说 → 退到"紧邻 160 字里出现过 `want`"（**旧口径，保留为兜底**）；
    ⑥ 一个都没有 → `None`（调用方给默认信封）。
    """
    fallback = None
    for payload, heading, label, window in _iter_json_fences(section_text):
        if want.search(label):
            return payload
        if avoid is not None and avoid.search(label):
            continue
        if want.search(heading):
            return payload
        if avoid is not None and avoid.search(heading):
            continue
        if fallback is None and want.search(window):
            fallback = payload
    return fallback


def _failure_body(code, section_text, message):
    """失败响应：**沿用该节失败响应示例的形状**（有的话），把 `errorCode` 换成命中规则的码。

    ★错误码**同时**放进 `message`：断言常写成 `contains: [body, 'FILE_1018']`，
    而"整包 contains 一个值"是否命中由内核语义决定 —— 两种写法都覆盖，免得判据悬空。
    """
    payload = _pick_json_fence(section_text, FAIL_WORDS_RE, avoid=SUCCESS_WORDS_RE)
    if payload is not None:
        payload = dict(payload)
        payload["success"] = False
        payload["errorCode"] = code
        payload["message"] = f"{message}（{code}）"
        return payload
    return {"success": False, "errorCode": code, "message": f"{message}（{code}）", "data": None}


def _response_body(section_text):
    """那一节的 **响应示例**；没有就用统一响应格式的信封（§1.4）。"""
    payload = _pick_json_fence(section_text, RESPONSE_WORDS_RE, avoid=REQUEST_WORDS_RE)
    return payload if payload is not None else dict(ENVELOPE)


def make_handler(routes, cookie_values=None, response_headers=None):
    """`cookie_values` / `response_headers`：文档声明、由 mock **按同一份判据**下发的响应事实（§9.29/§9.33）。"""
    cookie_values = dict(cookie_values or {})
    response_headers = dict(response_headers or {})

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            # ★§9.33：文档声明的响应头**优先**下发（它就是断言里的期望值）；
            #   `Content-Length` 由我们按真实字节算，**不许**被文档覆盖。
            declared = {str(name).lower() for name in response_headers}
            for name, value in response_headers.items():
                if str(name).lower() == "content-length":
                    continue
                self.send_header(name, value)
            if "content-type" not in declared:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            # ★§9.29：文档声明了响应 cookie → **真的按声明下发** ——
            #   否则"cookie 断言"在这台 mock 上永远无从验证（与 §9.26 的"大小事实"同一条道理：
            #   **先让事实存在，判据才谈得上判**）。
            #   值来自 `assertions.cookie_mock_values`（**与断言的期望同一份判据**，
            #   否则"mock 用一个断言不认的值"会造出假失败 ✗）。
            for name, value in cookie_values.items():
                self.send_header("Set-Cookie", f"{name}={value}; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            """按 `Content-Length` 把请求体读出来（JSON 能解析就给对象，否则给原文）。

            ★§9.26：**原始字节**与 `Content-Type` 都留在 `self` 上 —— multipart 的
              **文件大小**只能从原始字节里量（JSON 解析对它没有意义）。
            """
            self._content_type = self.headers.get("Content-Type") or ""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self._raw_body = b""
                return None
            raw = self.rfile.read(length)
            self._raw_body = raw
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return raw.decode("utf-8", "replace")

        def _record(self, body, triggered=""):
            """★§9.16 **回读**：把收到的请求照录到 JSONL —— 这是"参数真的发出去了"的**唯一证明**。

            为什么不靠"用例跑绿"来证明：mock 对缺参数的请求**照样会回成功响应**（它不校验业务），
            于是"没发参数"这件事在结果上是**绿**的 —— 典型的静默假通过。
            ★§9.21：命中错误条件时把**哪条规则**一起照录（negative 步的"为什么绿/为什么红"才有据）。
            """
            if not RECORD_PATH[0]:
                return
            interesting = {
                key: value
                for key, value in self.headers.items()
                if key.lower()
                not in ("host", "accept-encoding", "connection", "user-agent", "content-length")
            }
            entry = {
                "method": self.command,
                "path": self.path,
                "headers": interesting,
                "body": body,
                "triggered_rule": triggered,
            }
            with RECORD_LOCK:
                with open(RECORD_PATH[0], "a", encoding="utf-8") as fp:
                    fp.write(json.dumps(entry, ensure_ascii=False) + "\n")

        def _dispatch(self):
            body = self._read_body()
            path = self.path.split("?")[0].rstrip("/") or "/"
            key = (self.command, path)
            # ★§9.21 先问**错误条件引擎**：命中就回失败响应（负例步骤才有可能真跑）
            code, quote = evaluate_error_rules(ERROR_RULES.get(key), self, body, KNOWN_VALUES)
            if code:
                section = SECTIONS.get(key, "")
                self._record(body, triggered=f"{code} ← {quote[:60]}")
                # ★§9.33：HTTP 状态**照文档**（引用句里写了 `HTTP 500` 就用 500；没写就 200）
                self._respond(
                    documented_failure_status(quote, section),
                    _failure_body(code, section, quote[:60]),
                )
                return
            self._record(body)
            if key in routes:
                self._respond(200, routes[key])
                return
            known = {method for method, _ in routes if _ == path}
            if known:
                # ★方法不对**不许**假装成功（否则"真跑"就成了摆设）
                self._respond(
                    405,
                    {
                        "success": False,
                        "errorCode": "FILE_405",
                        "message": f"文档里该路径是 {'/'.join(sorted(known))}，不是 {self.command}",
                        "data": None,
                    },
                )
                return
            self._respond(404, dict(FAIL_ENVELOPE))

        do_GET = _dispatch
        do_POST = _dispatch
        do_PUT = _dispatch
        do_DELETE = _dispatch
        do_PATCH = _dispatch

        def log_message(self, fmt, *args):  # 静音（需要看请求时用 --verbose）
            if VERBOSE[0]:
                sys.stderr.write("  [mock] %s %s\n" % (self.command, self.path))

    return Handler


VERBOSE = [False]
# ★§9.16：收到请求照录到哪里（`--record` / 环境变量 `MOCK_RECORD`；空 = 不记录）
RECORD_PATH = [""]
RECORD_LOCK = threading.Lock()
# ★§9.21：文档推导出来的**错误条件规则**与**每节的原文**（失败响应用它拼形状）
ERROR_RULES = {}
SECTIONS = {}
UNBOUND_RULES = []
# ★§9.22：**种子数据**（文档里出现过的实体值）——存在性族用它判"这个实体存不存在"
KNOWN_VALUES = set()
# ★§9.29：文档**响应 Cookie 表**声明的 cookie → **mock 该下发的值**（与断言的期望**同源**，见
#   `assertions.cookie_mock_values`）——每个响应都按它下发 `Set-Cookie`。
#   （**先让事实存在**，cookie 断言才谈得上判；同 §9.26 的"文件大小"）
COOKIE_VALUES = {}
# ★§9.33：同上，响应**头**（`response_header_values`）——每个响应都按它下发。
RESPONSE_HEADERS = {}


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="与接口文档同源的本地 mock 服务")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--doc", default=DOC_DEFAULT)
    parser.add_argument("--verbose", action="store_true", help="打印每个请求")
    parser.add_argument(
        "--record",
        default=os.environ.get("MOCK_RECORD", ""),
        help="把**收到的请求**照录成 JSONL（§9.16 的回读证据；默认取环境变量 MOCK_RECORD）",
    )
    args = parser.parse_args()
    VERBOSE[0] = args.verbose
    RECORD_PATH[0] = args.record
    if RECORD_PATH[0]:
        # 每次启动清空（否则上一轮的记录会混进来，得出假结论）
        with open(RECORD_PATH[0], "w", encoding="utf-8"):
            pass
        print(f"[mock] 请求回读 → {RECORD_PATH[0]}")

    doc_text = io.open(args.doc, encoding="utf-8", errors="replace").read()
    routes = build_routes(doc_text)
    if not routes:
        print("没能从文档里推出任何路由——文档路径对吗？")
        return 1
    # ★§9.21 错误条件引擎：可判定的条件 + **点名**不能模拟的（不许假装模拟了）
    ERROR_RULES.clear()
    SECTIONS.clear()
    UNBOUND_RULES.clear()
    rules_by_route, unbound = build_error_rules(doc_text)
    ERROR_RULES.update(rules_by_route)
    UNBOUND_RULES.extend(unbound)
    KNOWN_VALUES.clear()
    KNOWN_VALUES.update(build_known_values(doc_text))
    # ★§9.29：响应 Cookie 表 → 这台 mock 每次响应都按声明下发 Set-Cookie（值由断言侧同一判据决定）
    from interfacetester_ai.assertions import cookie_mock_values, response_header_values  # noqa: PLC0415

    COOKIE_VALUES.clear()
    COOKIE_VALUES.update(cookie_mock_values(doc_text))
    # ★§9.33：响应头表 → 这台 mock 每次响应都按声明下发这些头（值同源，见断言侧）
    RESPONSE_HEADERS.clear()
    for name, value in response_header_values(doc_text).items():
        RESPONSE_HEADERS[name] = value
    lines = doc_text.split("\n")
    for section in interface_sections(doc_text):
        if section.method and section.url:
            pure = section.url.split("?")[0].rstrip("/") or "/"
            SECTIONS[(section.method.upper(), pure)] = "\n".join(
                lines[section.start_line - 1 : section.end_line]
            )
    bound = sum(len(items) for items in ERROR_RULES.values())
    by_kind = {}
    for items in ERROR_RULES.values():
        for rule in items:
            by_kind[rule.get("kind", "relation")] = by_kind.get(rule.get("kind", "relation"), 0) + 1
    families = "、".join(f"{name} {count}" for name, count in sorted(by_kind.items()))
    print(f"[mock] 错误条件：**可模拟 {bound} 条**（{len(ERROR_RULES)} 个接口；{families}）")
    print(f"[mock] 种子数据（文档里出现过的实体值）：{len(KNOWN_VALUES)} 个 → 存在性族按它判'存不存在'")
    if COOKIE_VALUES:
        print(
            f"[mock] 响应 cookie：**按文档下发 {len(COOKIE_VALUES)} 个**"
            f"（{'、'.join(f'{k}={v}' for k, v in list(COOKIE_VALUES.items())[:4])}）→ 每个响应都带 `Set-Cookie`"
        )
    if RESPONSE_HEADERS:
        print(
            f"[mock] 响应头：**按文档下发 {len(RESPONSE_HEADERS)} 个**"
            f"（{'、'.join(f'{k}: {v}' for k, v in list(RESPONSE_HEADERS.items())[:4])}）"
        )
    if unbound:
        statements = [item for item in unbound if is_condition_statement(item[1])]
        print(
            f"[mock] ★**不模拟** {len(unbound)} 条 = **条件陈述 {len(statements)} 条**（能力边界：要状态/"
            f"业务状态，或请求里给不出事实）+ 示例碎片 {len(unbound) - len(statements)} 条（JSON 里的错误码行）："
        )
        for where, sentence in statements[:8]:
            print(f"        · {where}：{sentence[:72]}")
        if len(statements) > 8:
            print(f"        · …（条件陈述共 {len(statements)} 条）")
    print(f"[mock] 依据文档推出 {len(routes)} 条路由（{os.path.basename(args.doc)}）：")
    for (method, path) in sorted(routes, key=lambda item: item[1]):
        print(f"  {method:5} {path}")
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(routes, COOKIE_VALUES, RESPONSE_HEADERS)
    )
    print(f"[mock] 监听 http://127.0.0.1:{args.port}（Ctrl-C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock] 退出")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
