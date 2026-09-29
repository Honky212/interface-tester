# -*- coding: utf-8 -*-
"""§9.22 错误条件引擎的机器判据（`probe_p2_prep/mock_file_service.py`）。

为什么这些判据长这样：

- **注入式小文档**：每族的最小文档自给自足 —— 换一份大文档、它的措辞一变，判据就假红/假绿；
  小文档只留**这一族**需要的那几句话，红了就是这一族坏了。
- **成对**：每族都验两面 —— 负例必须**命中**、正例必须**不命中**。只验前者会漏"误报"，
  而误报更坏（它把**正确的用例**判红 —— 本轮就在真实文档上踩到三次）。
- **元护栏**：判据必须能被证明"会红"（抹掉一个词表项 → 该族少一条）。
"""

import ast
import io
import os
import re
import sys
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "probe_p2_prep"))
sys.path.insert(0, BASE)

import mock_file_service as mock  # noqa: E402

REAL_DOC = os.path.join(BASE, "project-three", "统一文件服务平台-接入应用接口文档.md")


def make_doc(extra_table_rows="", *, version_required="否", example="`/app1/`", notes="", only_rows=None):
    """一份**最小**接口文档：上传接口 + 公共错误码表（判据只依赖这里出现的东西）。

    `only_rows` 给出时，公共错误码表**只留这几行** —— 每族一条独立文档，避免"别的族先命中"
    把判据变成假的（实测：`path=app1` 既违规格式、又是"不存在的目录"，先命中的是存在性那族 ✗）。
    """
    rows = only_rows if only_rows is not None else (
        "| `FILE_41` | 目录不存在 | 指定的目录不存在 |\n"
        "| `FILE_42` | 文件不存在 | 指定的文件不存在 |\n"
        "| `FILE_43` | 版本不能为空 | 版本参数缺失 |\n"
        "| `FILE_44` | 目录名称必须以/开头并以/结尾 | 路径格式错误 |\n"
        "| `FILE_45` | 最多上传2个文件 | 上传文件数量超限 |\n"
        "| `FILE_46` | 单文件超过 1MB | 上传文件大小超过限制 |\n"
    ) + extra_table_rows
    return f"""# 接口文档

## 1. 公共错误码

| 错误码 | 含义 | 说明 |
|-------|------|------|
{rows}
## 2.1 上传文件

| **URL** | `POST /api/file/upload` |
|---------|-------------------------|

**请求参数**：

| 参数名 | 类型 | 必填 | 说明 | 示例值 |
|--------|------|------|------|--------|
| `path` | String | 是 | 目录路径 | {example} |
| `files` | MultipartFile[] | 是 | 文件数组（最多2个） | - |
| `version` | String | {version_required} | 版本号 | `v1` |

{notes}
**响应示例**：

```json
{{"success": true, "data": {{"path": "/app1/"}}}}
```
"""


class FakeHandler:
    """只实现引擎要用到的那几样（`headers` / `path` / 原始体），不起服务。"""

    def __init__(self, query="", headers=None, raw_body=b"", content_type=""):
        self.path = "/api/file/upload" + (("?" + query) if query else "")
        self.headers = headers or {}
        self._raw_body = raw_body  # ★§9.26：multipart 的**文件大小**只能从原始字节里量
        self._content_type = content_type


def multipart_body(fields, boundary="X-BOUNDARY-926"):
    """手工拼一份 multipart 请求体 → `(原始字节, Content-Type)`（判据自己造事实，不依赖框架）。"""
    chunks = []
    for name, filename, content in fields:
        payload = content.encode("utf-8") if isinstance(content, str) else content
        chunks.append(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        chunks.append((disposition + "\r\n").encode())
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        chunks.append(payload + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def build(doc_text):
    return mock.build_error_rules(doc_text)


def rules_for(doc_text, code):
    rules, _unbound = build(doc_text)
    return [rule for items in rules.values() for rule in items if rule.get("code") == code]


def fires(doc_text, query, body=None, headers=None):
    """按**文档**跑一次判定：命中 → 错误码；不命中 → None。"""
    rules, _unbound = build(doc_text)
    key = ("POST", "/api/file/upload")
    code, _quote = mock.evaluate_error_rules(
        rules.get(key), FakeHandler(query, headers), body, mock.build_known_values(doc_text)
    )
    return code


def fires_with_body(doc_text, raw_body, content_type, query="path=%2Fapp1%2F"):
    """带**原始请求体**跑一次判定（multipart 场景用）。"""
    rules, _unbound = build(doc_text)
    key = ("POST", "/api/file/upload")
    code, _quote = mock.evaluate_error_rules(
        rules.get(key),
        FakeHandler(query, raw_body=raw_body, content_type=content_type),
        None,
        mock.build_known_values(doc_text),
    )
    return code


class TestExistFamily(unittest.TestCase):
    """④ 存在性：值不在**种子数据**（文档示例里出现过的实体）里 → 按文档的"不存在"语义回失败。"""

    def test_binds_to_directory_field(self):
        rules = rules_for(make_doc(), "FILE_41")
        self.assertTrue(rules, "「目录不存在」没绑上任何字段")
        self.assertEqual(rules[0]["kind"], "exist")
        self.assertIn("path", rules[0]["fields"])

    def test_unknown_value_fires_and_known_value_does_not(self):
        doc = make_doc()
        self.assertEqual(fires(doc, "path=%2Fnope%2F"), "FILE_41")  # 负例：不在种子里 ✓
        self.assertIsNone(fires(doc, "path=%2Fapp1%2F"))  # 正例：正是文档里的示例值 ✓

    def test_reference_values_are_not_entities(self):
        """`@report.pdf`（客户端引用）不是实体值 —— 拿它判"不存在"会满屏假红 ✗。"""
        self.assertIsNone(fires(make_doc(), "path=%2Fapp1%2F&files=@report.pdf"))


class TestUploadPathMultipartSize(unittest.TestCase):
    """★§9.26 上传路径：**"请求里有没有『大小』这个事实"就是分水岭**（T3_1 那个形态的固化）。

    文档写「单文件超过 1KB → FILE_46」（迷你文档把真实文档的 50MB 缩到 1KB，判据才跑得动）：

    - 真发了 multipart 文件（2KB）→ **命中** ✓（服务端拿到了事实，可判定）；
    - 只发文件名（`files=@big_over_1kb.zip`，请求里**没有文件体**）→ **不命中** ✓
      —— 这正是 T3_1 的形态：**跑不绿不是用例错，是请求里没有事实** ✗。
    """

    LIMIT_DOC = "| `FILE_46` | 单文件超过 1KB | 上传文件大小超过限制 |\n"

    def setUp(self):
        self.doc = make_doc(only_rows=self.LIMIT_DOC)

    def test_helper_measures_each_part(self):
        raw, content_type = multipart_body(
            [("files", "big.bin", b"x" * 2048), ("path", "", "/app1/")]
        )

        parts = mock.multipart_file_sizes(raw, content_type)

        self.assertEqual(
            {name: size for name, _filename, size in parts},
            {"files": 2048, "path": len("/app1/")},
        )

    def test_non_multipart_body_has_no_size_facts(self):
        """JSON 请求体里没有"文件大小"这个事实（不是 multipart → 空表）。"""
        self.assertEqual(mock.multipart_file_sizes(b'{"a": 1}', "application/json"), [])
        self.assertEqual(mock.multipart_file_sizes(b"", ""), [])
        self.assertEqual(mock.multipart_file_sizes(b"junk", "multipart/form-data; boundary="), [])

    def test_oversized_file_is_detectable(self):
        """真发了 2KB 文件 → 超过文档写的 1KB → **命中**（负例步骤**可验证** ✓）。"""
        raw, content_type = multipart_body([("files", "big.bin", b"x" * 2048)])

        self.assertEqual(fires_with_body(self.doc, raw, content_type), "FILE_46")

    def test_file_within_limit_does_not_fire(self):
        raw, content_type = multipart_body([("files", "small.bin", b"x" * 512)])

        self.assertIsNone(fires_with_body(self.doc, raw, content_type))

    def test_names_only_is_not_detectable(self):
        """★T3_1 的形态：请求里**只有文件名**、没有大小事实 → 不触发（也绝不乱报）。

        要让它可验证，得让用例**带上大小**（真发 multipart 文件，或给一个大小取值）。
        """
        self.assertIsNone(fires(self.doc, "path=%2Fapp1%2F&files=@big_over_1kb.zip"))

    def test_numeric_size_value_still_works(self):
        """成对：**数值型大小**（`files=2KB`）这一路没有被 multipart 改动带坏。"""
        self.assertEqual(fires(self.doc, "path=%2Fapp1%2F&files=2KB"), "FILE_46")
        self.assertIsNone(fires(self.doc, "path=%2Fapp1%2F&files=512B"))


class TestRequiredFamily(unittest.TestCase):
    """② 非空：**只在该字段于本节字段表里标成必填时**才绑 —— 否则"可选字段没传"会被误报 ✗。"""

    def test_optional_field_does_not_get_a_rule(self):
        self.assertEqual(rules_for(make_doc(version_required="否"), "FILE_43"), [])

    def test_required_field_gets_a_rule_and_fires_when_missing(self):
        doc = make_doc(version_required="是")
        rules = rules_for(doc, "FILE_43")
        self.assertTrue(rules, "必填的 version 没绑上「版本不能为空」")
        self.assertEqual(rules[0]["field"], "version")
        self.assertEqual(fires(doc, "path=%2Fapp1%2F"), "FILE_43")  # 缺必填 → 命中 ✓
        self.assertIsNone(fires(doc, "path=%2Fapp1%2F&version=v1"))  # 传了 → 不命中 ✓


class TestFormatFamily(unittest.TestCase):
    """⑤ 格式：「要求」式表述 → 判定的是**违反条件**。

    ★用**只含这一族**的小文档：真实文档里 `path=app1` 既违规格式、又是"不存在的目录"，
      两族都会成立、先命中的是存在性那族 —— 判据会变成假的（实测踩到）。
    """

    FORMAT_ONLY = (
        "| `FILE_44` | 目录名称必须以/开头并以/结尾 | 路径格式错误 |\n"
        "| `FILE_45` | 目录名称不能包含连续的/ | 路径格式错误 |\n"
    )

    def test_fires_when_path_is_not_slash_wrapped(self):
        doc = make_doc(only_rows=self.FORMAT_ONLY)
        self.assertEqual(fires(doc, "path=app1"), "FILE_44")
        self.assertEqual(fires(doc, "path=%2Fapp1%2F%2Fx%2F"), "FILE_45")  # 连续 // 是**另一条** ✓
        self.assertIsNone(fires(doc, "path=%2Fapp1%2F"))

    def test_field_whose_own_example_violates_the_rule_is_not_bound(self):
        """★文档自相矛盾时以**示例**为准：示例值本身就违反那条格式要求 → 这条要求不是针对它的。"""
        doc = make_doc(only_rows=self.FORMAT_ONLY, example="`doc`")
        self.assertEqual(rules_for(doc, "FILE_44"), [])
        self.assertIsNone(fires(doc, "path=doc"))


class TestLimitFamily(unittest.TestCase):
    """③ 上限：数量（列表长度）与大小（字节）；**值不齐 → 不可判 → 不触发**。"""

    def test_count_limit_fires_over_threshold(self):
        doc = make_doc()
        self.assertEqual(fires(doc, "path=%2Fapp1%2F&files=@a&files=@b&files=@c"), "FILE_45")
        self.assertIsNone(fires(doc, "path=%2Fapp1%2F&files=@a&files=@b"))

    def test_size_limit_does_not_fire_without_a_size_fact(self):
        """★"宁可漏报"的核心：请求里给不出大小 → **不触发**（也绝不乱报）。"""
        doc = make_doc()
        self.assertIsNone(fires(doc, "path=%2Fapp1%2F&files=@huge.zip"))

    def test_size_limit_fires_when_size_is_given(self):
        doc = make_doc()
        self.assertEqual(fires(doc, "path=%2Fapp1%2F&files=2MB"), "FILE_46")


POLARITY_DOC = """# 接口文档

## 1. 公共错误码

| 错误码 | 含义 | 说明 |
|-------|------|------|
| `FILE_49` | 源目录名称和目标目录名称不能相同 | 目录移动参数冲突 |

## 2.1 更新目录

| **URL** | `POST /api/directory/update` |
|---------|-----------------------------|

**请求参数**：

| 参数名 | 类型 | 必填 | 说明 | 示例值 |
|--------|------|------|------|--------|
| `sourceDirName` | String | 是 | 源目录完整路径 | `/app1/a/` |
| `targetDirName` | String | 是 | 目标目录完整路径 | `/app1/b/` |

**响应示例**：

```json
{"success": true}
```
"""


class TestRelationPolarity(unittest.TestCase):
    """① 关系：证句**自带否定**（"不能相同"）时，要求方向要再反一次。

    ★不反的后果（实测）：「源目录 != 目标目录」被判成错误、而「两者相同」反而放过 —— 方向完全反了。
    """

    def _fire(self, source, target):
        rules, _unbound = build(POLARITY_DOC)
        body = {"sourceDirName": source, "targetDirName": target}
        code, _quote = mock.evaluate_error_rules(
            rules.get(("POST", "/api/directory/update")),
            FakeHandler("", {}),
            body,
            mock.build_known_values(POLARITY_DOC),
        )
        return code

    def test_rule_direction_is_equal_not_not_equal(self):
        rules = rules_for(POLARITY_DOC, "FILE_49")
        self.assertTrue(rules, "「源目录名称和目标目录名称不能相同」没绑上")
        self.assertEqual(rules[0]["op"], "==", "「不能相同」被判反了方向")

    def test_equal_fires_and_different_does_not(self):
        self.assertEqual(self._fire("/app1/a/", "/app1/a/"), "FILE_49")  # 违反条件：相同 ✓
        self.assertIsNone(self._fire("/app1/a/", "/app1/b/"))  # 正确请求 ✓


EXEMPT_DOC = """# 接口文档

## 1. 约定

> **注意**: `X-Path` 值必须与请求参数中的 `path` 字段完全一致，否则会返回 `FILE_48` 错误。唯一例外是 **创建目录** 接口，其 `path` 只需以 `X-Path` 开头即可。

## 2.1 创建目录

| **URL** | `POST /api/directory/create` |
|---------|-----------------------------|

**请求参数**：

| 参数名 | 类型 | 必填 | 说明 | 示例值 |
|--------|------|------|------|--------|
| `path` | String | 是 | 新目录的完整路径 | `/app1/a/` |
| `X-Path` | String | 是 | 父目录路径 | `/app1/` |

**响应示例**：

```json
{"success": true}
```
"""


class TestDocumentedExemption(unittest.TestCase):
    """文档写明的**豁免**：被点名的接口放宽成"以…开头"，而不是"完全一致"（也不丢掉这条规则）。"""

    def _fire(self, path, x_path):
        rules, _unbound = build(EXEMPT_DOC)
        code, _quote = mock.evaluate_error_rules(
            rules.get(("POST", "/api/directory/create")),
            FakeHandler("", {"X-Path": x_path}),
            {"path": path},
            mock.build_known_values(EXEMPT_DOC),
        )
        return code

    def test_relaxed_to_prefix_with_exception_order(self):
        rules = rules_for(EXEMPT_DOC, "FILE_48")
        self.assertTrue(rules, "豁免句把这条规则整个弄丢了（应该放宽成前缀）")
        self.assertEqual(rules[0]["op"], "startswith")
        self.assertEqual((rules[0]["left"], rules[0]["right"]), ("path", "X-Path"))

    def test_prefix_ok_but_not_prefixed_fires(self):
        self.assertIsNone(self._fire("/app1/a/", "/app1/"))  # 文档允许：以 X-Path 开头 ✓
        self.assertEqual(self._fire("/other/a/", "/app1/"), "FILE_48")  # 违反 → 命中 ✓


class TestNoSilentDrop(unittest.TestCase):
    """**不许静默消失**：含错误码的条件要么绑上、要么进"不模拟"清单（§9.22 踩过 FILE_1016）。"""

    def test_every_code_candidate_is_bound_or_listed(self):
        doc = make_doc(extra_table_rows="| `FILE_47` | 目标版本已是当前版本，无需回滚 | 状态冲突 |\n")
        rules, unbound = build(doc)
        bound = {rule["quote"] for items in rules.values() for rule in items}
        listed = {sentence for _where, sentence in unbound}
        for sentence, _code, _word, _op in mock._bindable_candidates(doc):
            self.assertTrue(
                sentence in bound or sentence in listed,
                f"这条含错误码的条件既没绑上也没点名（静默消失）：{sentence[:60]}",
            )

    def test_fragment_classifier(self):
        self.assertFalse(mock.is_condition_statement('"errorCode": "FILE_41",'))
        self.assertFalse(mock.is_condition_statement("`FILE_41`"))
        self.assertTrue(mock.is_condition_statement("目录不存在时返回 `FILE_41`"))


class TestEngineDiscipline(unittest.TestCase):
    """元护栏：引擎要是**纯函数**、判据要能"会红"、只 import 产品的一个函数。"""

    def test_baseline_is_deterministic(self):
        doc = io.open(REAL_DOC, encoding="utf-8", errors="replace").read() if os.path.isfile(REAL_DOC) else make_doc()
        self.assertEqual(build(doc), build(doc), "同一份文档两次推导结果不同（不可复现）")

    def test_meta_guardrail_removing_a_word_shrinks_the_family(self):
        """抹掉关系词表里的一项 → 该族必须**少一条**（否则"通过"可能只是判据没在检查）。"""
        if not os.path.isfile(REAL_DOC):
            self.skipTest("真文档不在（fixture 缺失）")
        original = mock.RELATION_WORDS
        try:
            mock.RELATION_WORDS = tuple(item for item in original if item[0] != "大于")
            damaged = [rule for items in build(io.open(REAL_DOC, encoding="utf-8", errors="replace").read())[0].values() for rule in items]
        finally:
            mock.RELATION_WORDS = original
        healthy = [
            rule
            for items in build(io.open(REAL_DOC, encoding="utf-8", errors="replace").read())[0].values()
            for rule in items
        ]
        before = len([rule for rule in healthy if rule.get("op") == ">"])
        after = len([rule for rule in damaged if rule.get("op") == ">"])
        self.assertGreater(before, after, "抹掉「大于」后仍绑出 > 规则 —— 判据打偏了")

    def test_no_self_comparison_rules(self):
        """同一字段自比（`dirName != dirName`）是**绑错了**：恒成立/恒不成立，白占一条 ✗。"""
        if not os.path.isfile(REAL_DOC):
            self.skipTest("真文档不在（fixture 缺失）")
        rules, _unbound = build(io.open(REAL_DOC, encoding="utf-8", errors="replace").read())
        for items in rules.values():
            for rule in items:
                if rule.get("kind") == "relation":
                    self.assertNotEqual(rule["left"], rule["right"], f"自比规则：{rule}")


class TestRealDocumentSmoke(unittest.TestCase):
    """真文档冒烟：可模拟条数要够多，**不模拟的条件陈述**要少（能力边界看得见）。"""

    def setUp(self):
        if not os.path.isfile(REAL_DOC):
            self.skipTest("真文档不在（fixture 缺失）")
        self.doc = io.open(REAL_DOC, encoding="utf-8", errors="replace").read()

    def test_five_families_all_bound(self):
        rules, _unbound = build(self.doc)
        kinds = {rule["kind"] for items in rules.values() for rule in items}
        expected = {"relation", "required", "limit", "exist", "format"}
        self.assertEqual(kinds, expected, f"这几族没绑上：{expected - kinds}")

    def test_unbound_statements_are_few_and_named(self):
        rules, unbound = build(self.doc)
        bound = sum(len(items) for items in rules.values())
        distinct = {
            sentence.strip()
            for _where, sentence in unbound
            if mock.is_condition_statement(sentence)
        }
        self.assertGreaterEqual(bound, 100, f"可模拟条数骤降（{bound}）—— 绑定逻辑坏了？")
        # ★12 是**基线**不是目标：现在判不了的只剩"要状态/业务状态"和"请求里给不出事实"两类，
        #   数它涨没涨是为了发现"悄悄退化成判不了" —— 涨了要么是文档变了、要么是绑定坏了。
        self.assertLessEqual(len(distinct), 14, f"判不了的条件陈述变多了：{len(distinct)} 条")
        for sentence in distinct:
            self.assertTrue(sentence, "不模拟清单里有空条目")



class TestFenceOwnership(unittest.TestCase):
    """★§9.33（登记项 1）：挑"响应示例"必须**先看归属**，不能只看"前面 160 字里有没有『响应』"。

    **真现场**：请求体表的「说明」列里出现"响应/返回"两字 → 它前面的**请求体示例**会被当成
    响应示例回给用例 → 症状是**断言全 `check_value: None`**（不是报错，是**静默假失败**），
    并毁掉此后所有负例探针的可信度。判据必须**先看标签/标题**（这一块到底在讲请求还是响应）。
    """

    SECTION = """#### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| name | string | 是 | 文件名（响应里会返回 `fileName`） |

请求体示例：

```json
{"name": "report.pdf"}
```

#### 成功响应

成功响应示例：

```json
{"success": true, "data": {"fileName": "report.pdf"}}
```
"""

    # 只有**请求**示例：标签写着"请求体示例"，哪怕 160 字窗口里出现"响应"两字也不许当响应用
    REQUEST_ONLY = """#### 请求体（JSON）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| name | string | 是 | 文件名（响应里会返回同名字段） |

请求体示例：

```json
{"name": "report.pdf"}
```
"""

    def test_request_example_is_never_used_as_the_response(self):
        payload = mock._response_body(self.SECTION)

        self.assertIs(payload.get("success"), True)  # ★是响应那一块
        self.assertIn("fileName", payload.get("data") or {})  # 而不是请求那一块（`name`）

    def test_label_alone_excludes_the_request_block(self):
        """★成对：只有请求示例时 → 退回统一信封，**不许**把请求体当响应回。"""
        self.assertEqual(mock._response_body(self.REQUEST_ONLY), dict(mock.ENVELOPE))

    def test_exclusion_is_what_does_it(self):
        """★元护栏：把"请求侧标签"这条判据掐掉（词表换成永不命中）→ 请求块**立刻**被当成响应。

        证明拦住它的正是这条判据，而不是别的什么顺手挡住的。
        """
        with patch.object(mock, "REQUEST_WORDS_RE", re.compile(r"$^")):
            payload = mock._response_body(self.REQUEST_ONLY)

        self.assertEqual(payload, {"name": "report.pdf"})

    def test_window_fallback_still_works_without_any_labels(self):
        """★旧口径**保留为兜底**：标签/标题都没提"响应"、但窗口里出现过 → 仍然认它。"""
        text = '响应如下：\n\n示例：\n\n```json\n{"success": true}\n```\n'

        self.assertEqual(mock._response_body(text), {"success": True})


class TestExistBeatsFormat(unittest.TestCase):
    """★§9.33（登记项 2）：同一请求同时"违规格式"且"实体不在种子数据里"时的**裁决**。

    · 裁决顺序：非空 → 存在性 → 上限 → 关系 → 格式（`KIND_PRIORITY`，**同族内保持构建顺序**）；
    · **但"只差首尾空白/大小写"的值不算另一个实体** —— 那是写错（该报格式错），归一化之后
      能命中种子 → 存在性**不成立**。
    两条一起才说明这套口径不会把 `format` 族打成假红/假绿
    （`probe_p2_prep/probe_negative_cases.py` 的 **18/18** 靠的就是它）。
    """

    FORMAT_RULE = {
        "kind": "format",
        "field": "path",
        "check": "no_edge_space",
        "code": "FILE_4018",
        "quote": "开头和结尾不能有空格（FILE_4018）",
    }
    EXIST_RULE = {
        "kind": "exist",
        "fields": ["path"],
        "code": "FILE_2001",
        "quote": "目录不存在（FILE_2001）",
    }

    def _fire(self, rules, query, known):
        code, _quote = mock.evaluate_error_rules(rules, FakeHandler(query), None, known)
        return code

    def test_unknown_entity_with_a_format_problem_reports_existence(self):
        """★裁决：两条都成立 → 报**存在性**（`exist` 优先于 `format`）。"""
        code = self._fire([self.FORMAT_RULE, self.EXIST_RULE], "path=%2Fnope-xyz", {"/app1/"})

        self.assertEqual(code, "FILE_2001")

    def test_known_entity_with_a_format_problem_reports_format(self):
        """★成对（这条才是关键）：实体在种子里（只差首尾空格）→ 存在性不成立 → 报**格式**。"""
        code = self._fire([self.FORMAT_RULE, self.EXIST_RULE], "path=%20%2Fapp1%2F%20", {"/app1/"})

        self.assertEqual(code, "FILE_4018")

    def test_whitespace_and_case_are_not_another_entity(self):
        """归一化只做两样：**首尾空白 + 大小写**；真·另一个实体照样命中。"""
        rules = [self.EXIST_RULE]

        self.assertIsNone(self._fire(rules, "path=%2FAPP1%2F", {"/app1/"}))
        self.assertEqual(self._fire(rules, "path=%2Fnope%2F", {"/app1/"}), "FILE_2001")

    def test_priority_order_is_pinned(self):
        """★裁决顺序本身是判据：改了顺序必须有人复核（顺序即语义）。"""
        self.assertEqual(
            mock.KIND_PRIORITY, ("required", "exist", "limit", "relation", "format")
        )


class TestFailureStatusFollowsTheDoc(unittest.TestCase):
    """★§9.33（登记项 2）：失败响应的 **HTTP 状态**以文档为准（没写才用示例文档的 `200`）。

    两派写法**都真实存在**（都取自本仓文档，不是臆造）：

    · 示例文档：「…返回 `errorCode: FILE_1018`（**HTTP 状态码仍为 `200`**）」；
    · 转换文档（`project-three/接口文档示例转换文档测试1.md:180`）：
      「…失败响应 `errorCode: FILE_4001`（**HTTP `500`**，依据 TC-DIR-003）」。
    """

    QUOTE_500 = (
        "- `path` 不以 `/` 开头、或不以 `/` 结尾：失败响应 `errorCode: FILE_4001`"
        "（HTTP `500`，依据 TC-DIR-003 / TC-DIR-004）"
    )
    QUOTE_200 = (
        "- 请求头 `X-Path` 与请求体 `path` 不一致时：**失败响应**返回 `errorCode: FILE_1018`"
        "（HTTP 状态码仍为 `200`）"
    )

    def test_documented_500_is_honored(self):
        self.assertEqual(mock.documented_failure_status(self.QUOTE_500), 500)

    def test_documented_200_stays_200(self):
        self.assertEqual(mock.documented_failure_status(self.QUOTE_200), 200)

    def test_no_status_anywhere_defaults_to_200(self):
        """★成对：**没写**就 200（示例文档写明的默认），不许自己发明一个 500。"""
        self.assertEqual(
            mock.documented_failure_status("目录不存在 → `FILE_2001`", "本节也没写状态码"), 200
        )

