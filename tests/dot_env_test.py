"""0919-2 / L34：`.env` 解析改用 `python-dotenv`（含保住既有语义的适配层）。

## 修复前实测（自研解析 = 「按分隔符切开就完事」）

```text
KEY="quoted value"         ->  '"quoted value"'          （引号留在值里）
KEY='single quoted'        ->  "'single quoted'"
export KEY=value           ->  {'export KEY': 'value'}   （export 进了**键名**）
KEY=has-inline  # comment  ->  'has-inline  # comment'   （行内注释当成值）
KEY="line1\\nline2"         ->  '"line1\\nline2"'          （转义不生效）
```

这些正是从 docker-compose / shell 习惯迁移过来的用户**必踩**的坑。

## 换库不是「改一行 import」——必须保住三条既有语义

`python-dotenv` 解析本身可靠得多，但它有三处行为与本仓既有语义不同（全部实测）：

| 项 | python-dotenv 默认行为 | 本仓适配 |
|---|---|---|
| `KEY: value`（冒号写法） | **整行丢掉**（只往 stderr 打一句 warning） | 预扫归一成 `KEY=value` |
| `${VAR}` | **插值展开**（`${BASE}-world` → `hello-world`） | `interpolate=False` |
| 畸形行（引号没闭合 / 键名带空格 / 空键名） | **静默丢弃**，既不报错也不给 None | 核对「应有键名」并带行号报错 |

第三条最隐蔽：**「静默丢弃」比「报错」难查得多**——变量会直接从环境里消失，
往往拖到运行期才以别的形式炸出来。本仓一贯取向是「宁可报错，不静默丢弃」。

本文件对三类行为分别钉用例，其中**「不能变」的部分和「要修」的部分同等重要**：
换库最容易的翻车方式不是没修好，而是**悄悄改掉了别处的语义**。
"""

import os
import shutil
import unittest
import uuid

from interfacetester import exceptions, loader


def _env_file(content: str, encoding: str = "utf-8") -> str:
    """把 content 写成一个临时 .env，返回路径（放 logs/ 下，已被 .gitignore 覆盖）。"""
    path = os.path.join(os.getcwd(), "logs", f"tmp_env_{uuid.uuid4().hex[:8]}.env")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding=encoding) as f:
        f.write(content)
    return path


class _DotEnvTestBase(unittest.TestCase):
    """自动清理临时 .env 文件，并隔离 `os.environ` 的副作用。"""

    def setUp(self):
        self._env_backup = dict(os.environ)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _parse(self, content: str) -> dict:
        path = _env_file(content)
        self.addCleanup(os.remove, path)
        return loader.load_dot_env_file(path)


# ---------------------------------------------------------------------------
# 一、要修的部分：四个报告的缺陷
# ---------------------------------------------------------------------------
class TestDotEnvFixesReportedDefects(_DotEnvTestBase):
    def test_quotes_are_stripped(self):
        """`KEY="value"` / `KEY='value'` 的值里不该再带引号。"""
        parsed = self._parse(
            'DOUBLE="quoted value"\nSINGLE=\'single quoted\'\n'
        )
        self.assertEqual(parsed["DOUBLE"], "quoted value")
        self.assertEqual(parsed["SINGLE"], "single quoted")

    def test_export_prefix_does_not_leak_into_the_key_name(self):
        """`export KEY=value` 必须得到键 `KEY`，而不是 `export KEY`。"""
        parsed = self._parse("export EXPORTED=exported-value\n")

        self.assertIn("EXPORTED", parsed)
        self.assertEqual(parsed["EXPORTED"], "exported-value")
        self.assertNotIn(
            "export EXPORTED",
            parsed,
            "export 前缀被当成了键名的一部分——变量名会变成 `export EXPORTED`，"
            "用例里写 ${EXPORTED} 永远解析不到",
        )

    def test_inline_comment_is_stripped(self):
        """`KEY=value  # 注释` 的值里不该带注释。"""
        parsed = self._parse("INLINE=has-inline  # trailing comment\n")
        self.assertEqual(parsed["INLINE"], "has-inline")

    def test_hash_without_leading_space_is_kept(self):
        """反向护栏：值里的 `#`（前面没有空白）是**内容**，不能被当注释切掉。

        收得太狠会把 `PASSWORD=a#b` 截成 `a`——那是比"注释没清掉"更严重的静默错误。
        """
        parsed = self._parse("HASH_NO_SPACE=a#b\n")
        self.assertEqual(parsed["HASH_NO_SPACE"], "a#b")

    def test_double_quoted_escapes_are_processed(self):
        """双引号里的 `\\n` 应当变成真正的换行（shell 习惯）。"""
        parsed = self._parse('ESCAPED="line1\\nline2"\n')
        self.assertEqual(parsed["ESCAPED"], "line1\nline2")


# ---------------------------------------------------------------------------
# 二、不能变的部分：既有语义
# ---------------------------------------------------------------------------
class TestDotEnvKeepsExistingSemantics(_DotEnvTestBase):
    def test_colon_separator_still_works(self):
        """冒号写法（上游 httprunner 遗留）必须继续可用。

        python-dotenv **不认冒号**：直接换库会让这类变量整行消失，
        而本仓原实现（`elif b":" in line`）是支持的。
        """
        parsed = self._parse("COLON_KEY: colon-value\n")
        self.assertEqual(parsed["COLON_KEY"], "colon-value")

    def test_colon_form_value_with_url_is_not_mangled(self):
        """冒号写法只换**第一个**冒号，值里的 `http://…` 必须原样保留。"""
        parsed = self._parse("ENDPOINT: http://127.0.0.1:8080/api\n")
        self.assertEqual(parsed["ENDPOINT"], "http://127.0.0.1:8080/api")

    def test_dollar_brace_is_not_interpolated(self):
        """`${VAR}` 必须原样保留。

        python-dotenv 默认会把 `${BASE}-world` 展开成 `hello-world`——这是**语义变更**：
        本仓的 `.env` 一直当字面量来源，而 `${...}` 在本框架里是**变量/函数插值**语法
        （`parser.py`），`.env` 里出现它应当留给运行期处理。
        """
        parsed = self._parse("BASE=hello\nDERIVED=${BASE}-world\n")
        self.assertEqual(parsed["BASE"], "hello")
        self.assertEqual(parsed["DERIVED"], "${BASE}-world")

    def test_value_containing_equals_sign_is_intact(self):
        """值里的 `=` 不能被再切一刀（按第一个分隔符切分）。"""
        parsed = self._parse("URL_WITH_EQ=https://h/a?b=1&c=2\n")
        self.assertEqual(parsed["URL_WITH_EQ"], "https://h/a?b=1&c=2")

    def test_whitespace_around_separator_is_trimmed(self):
        parsed = self._parse("SPACED = spaced value\n")
        self.assertEqual(parsed["SPACED"], "spaced value")

    def test_empty_value_is_empty_string(self):
        parsed = self._parse("EMPTY=\n")
        self.assertEqual(parsed["EMPTY"], "")
        self.assertIsNotNone(parsed["EMPTY"])

    def test_comments_and_blank_lines_are_skipped(self):
        parsed = self._parse("# comment\n\nPLAIN=plain-value\n   \n")
        self.assertEqual(parsed, {"PLAIN": "plain-value"})

    def test_non_ascii_values(self):
        parsed = self._parse("NONASCII=中文值\n")
        self.assertEqual(parsed["NONASCII"], "中文值")

    def test_duplicate_key_last_wins(self):
        parsed = self._parse("DUP=first\nDUP=second\n")
        self.assertEqual(parsed["DUP"], "second")

    def test_os_environ_is_written_and_overrides(self):
        """`load_dot_env_file` 必须写 `os.environ`，且**无条件覆盖**（既有语义）。

        NOTICE: 刻意用 `dotenv_values()` 而不是 `load_dotenv()`——后者默认
        `override=False`，遇到已存在的环境变量会**跳过**，与本仓语义不同。
        """
        os.environ["OVERRIDE_ME"] = "old-value"
        path = _env_file("OVERRIDE_ME=new-value\nFRESH_KEY=fresh\n")
        self.addCleanup(os.remove, path)

        parsed = loader.load_dot_env_file(path)

        self.assertEqual(parsed["OVERRIDE_ME"], "new-value")
        self.assertEqual(
            os.environ["OVERRIDE_ME"],
            "new-value",
            "已有环境变量没有被覆盖——`load_dotenv(override=False)` 的语义不对",
        )
        self.assertEqual(os.environ["FRESH_KEY"], "fresh")

    def test_utf8_bom_does_not_pollute_the_first_key(self):
        """带 BOM 的 `.env`（Windows 编辑器常见）不得让第一个键变成 `\\ufeffKEY`。

        与 `cli.py` 里 M23 记过的是同一类坑（BOM 污染第一行）。
        """
        path = _env_file("\ufeffBOMKEY=bomvalue\n")
        self.addCleanup(os.remove, path)

        parsed = loader.load_dot_env_file(path)

        self.assertEqual(parsed, {"BOMKEY": "bomvalue"})

    def test_missing_file_returns_empty_dict(self):
        """既有语义：`.env` 不存在不是错误，返回空 dict。"""
        self.assertEqual(
            loader.load_dot_env_file(os.path.join(os.getcwd(), "tests", "data")), {}
        )


# ---------------------------------------------------------------------------
# 三、不许静默丢弃
# ---------------------------------------------------------------------------
class TestDotEnvNeverSilentlyDropsLines(_DotEnvTestBase):
    """畸形行必须**报错**，不能静默消失。

    python-dotenv 对下面这些行既不报错也不返回 `None`，而是**整行丢掉**。
    换库若不做适配，这些变量会从「有个歪值」变成「干脆不存在」。
    """

    def test_line_without_any_separator_raises(self):
        with self.assertRaises(exceptions.FileFormatError) as ctx:
            self._parse("GOOD=1\nBROKEN_LINE\n")
        self.assertIn("BROKEN_LINE", str(ctx.exception))
        self.assertIn("第 2 行", str(ctx.exception))

    def test_unclosed_quote_raises_instead_of_vanishing(self):
        with self.assertRaises(exceptions.FileFormatError) as ctx:
            self._parse('GOOD=1\nKEY="unclosed\n')
        self.assertIn("静默丢弃", str(ctx.exception))

    def test_key_with_space_raises_instead_of_vanishing(self):
        with self.assertRaises(exceptions.FileFormatError) as ctx:
            self._parse("GOOD=1\nKEY WITH SPACE=v\n")
        self.assertIn("静默丢弃", str(ctx.exception))

    def test_empty_key_name_raises_instead_of_vanishing(self):
        with self.assertRaises(exceptions.FileFormatError) as ctx:
            self._parse("GOOD=1\n=value\n")
        self.assertIn("静默丢弃", str(ctx.exception))

    def test_error_message_names_the_offending_line(self):
        """报错要能定位到行——否则用户只能一行行猜。"""
        with self.assertRaises(exceptions.FileFormatError) as ctx:
            self._parse("A=1\nB=2\nBAD LINE HERE\n")
        message = str(ctx.exception)
        self.assertIn("第 3 行", message)
        self.assertIn("BAD LINE HERE", message)


class TestDotEnvMultiLineValueDiagnosis(_DotEnvTestBase):
    """0919-2 / 缺陷 5：多行引号值的报错必须能定位到**真正的原因**。

    ## 现场（实测）

    python-dotenv **本身支持**多行值：

    ```text
    KEY2="line1
    line2"          ->  {'KEY2': 'line1\\nline2'}      （dotenv 直接跑，实测）
    ```

    但本框架的预扫是**逐行**的，第 2 行 `line2"` 既没有 `=` 也没有 `:`，
    于是走到「格式错误」分支，报出：

    ```text
    .env format error: 第 2 行既没有 `=` 也没有 `:`。
      该行: line2"
    ```

    —— 这是一次**响亮失败**（符合本仓哲学，也仍然是本批保持的行为），
    但错误文案**完全不会让人想到**是「多行引号值被拆开了」：用户看到的是一个
    莫名其妙的 `line2"`，会去查引号、查空格，就是不会想到「这个框架不支持多行值」。

    本批**只改文案、不改行为**（不扩大接受的语法面）：报错时会指出
    「上一处赋值（第 N 行 `KEY=...`）的引号没有闭合，所以本行被当成续行」，
    并给出**已验证可用**的替代写法。
    """

    def test_multi_line_value_error_points_at_the_unclosed_quote(self):
        with self.assertRaises(exceptions.FileFormatError) as ctx:
            self._parse('KEY1=v1\nKEY2="line1\nline2"\n')

        message = str(ctx.exception)
        # 仍然点名出错的行（既有行为不能丢）
        self.assertIn("第 3 行", message)
        self.assertIn("line2", message)
        # 新增：指出真正的原因所在的那一行与那段内容
        self.assertIn("第 2 行", message)
        self.assertIn('KEY2="line1', message)
        self.assertIn("没有闭合", message)
        # 新增：给出可操作的替代写法（而不是只说"格式错误"）
        self.assertIn("单行", message)

    def test_the_advice_in_the_message_actually_works(self):
        """提示里推荐的写法必须**真的可用** —— 提示一旦说谎，比没有提示更糟。"""
        parsed = self._parse('KEY2="line1\\nline2"\n')
        self.assertEqual(parsed["KEY2"], "line1\nline2")

    def test_multi_line_value_is_still_rejected(self):
        """反向护栏：本批**不**扩大语法面，多行值仍然响亮报错（不静默、也不放行）。"""
        with self.assertRaises(exceptions.FileFormatError):
            self._parse('KEY="line1\nline2"\n')

    def test_single_quote_apostrophe_is_not_misdiagnosed(self):
        """反向护栏：合法值里的单个引号不得被误判成「引号没闭合」。

        `NOTE=don't panic` 只有一个引号却是完全合法的值。判据取的是
        「值**以引号开头**且找不到配对闭合引号」，不是数引号个数 ——
        否则这条提示就会在合法用例上胡说，等于没有。
        """
        parsed = self._parse("NOTE=don't panic\nQUOTED=\"it's fine\"\n")
        self.assertEqual(parsed["NOTE"], "don't panic")
        self.assertEqual(parsed["QUOTED"], "it's fine")

    def test_unrelated_format_error_gets_no_multiline_hint(self):
        """反向护栏：与引号无关的格式错误不得被套上「多行值」的提示。"""
        with self.assertRaises(exceptions.FileFormatError) as ctx:
            self._parse("GOOD=1\nBAD LINE HERE\n")

        message = str(ctx.exception)
        self.assertIn("BAD LINE HERE", message)
        self.assertNotIn("没有闭合", message)

    def test_hint_survives_comment_and_blank_lines_between(self):
        """多行值的续行之间可能夹着空行/注释，提示仍要能定位到那个未闭合的赋值行。"""
        with self.assertRaises(exceptions.FileFormatError) as ctx:
            self._parse('KEY1=v1\nKEY2="line1\n\n# a comment\nline2"\n')

        self.assertIn("第 2 行", str(ctx.exception))


# ---------------------------------------------------------------------------
# 四、批次 2 / M1：同一行同时出现 `=` 与 `:` —— 判据取「**先出现的那个**」
# ---------------------------------------------------------------------------
class TestDotEnvSeparatorPrecedence(_DotEnvTestBase):
    r"""`.env` 里 `KEY: value` 只要**值里带一个 `=`**，整个项目 hmake/hrun 就硬失败。

    ## 现场（实测，真 CLI）

    ```text
    .env: B64_SECRET: dGhpc2lzYQ==          （冒号写法 + base64 结尾的 `==`）
    hmake good.yml  ->  EXIT 1
      FileFormatError: .env format error: 有 1 行被解析器**静默丢弃**了
        第 1 行 -> 期望的键名 'B64_SECRET: dGhpc2lzYQ'    ← 用户文件里不存在的字符串
    对照组：B64_SECRET=dGhpc2lzYQ==（等号写法）-> EXIT 0
            PLAIN: value（冒号写法但值里没有 `=`）-> EXIT 0
    ```

    ## 根因

    预扫的分支判据写的是 `"=" in line` —— 问的是"**整行**里有没有 `=`"，
    而不是"分隔符出现在键名之后"：

    ```text
    'B64_SECRET: dGhpc2lzYQ=='   ->  判成等号写法，键名 = 'B64_SECRET: dGhpc2lzYQ'   ← 错
    ```

    键名算错 → 预扫记下的「应有键名」与 python-dotenv 的结果对不上 →
    走"不许静默丢弃"分支报错，**永远指向那个虚构的键名**。
    判据改成「先出现的分隔符为准」后，同一行得到 `{'B64_SECRET': 'dGhpc2lzYQ=='}`。

    同一坑还覆盖 `KEY: a=b`、`ENDPOINT: http://h/p?x=1&y=2`（query 必然带 `=`）等常见写法。

    ## 与"修复前"的关系（诚实说明，不把话说满）

    实测取出 HEAD 的自研解析（`bytes.split(b"=", 1)`）跑同一批输入：它**不报错**，
    但键名同样算错成 `'B64_SECRET: dGhpc2lzYQ'` —— 也就是说
    `${B64_SECRET}` 在修复前**同样取不到值**，只是**静默**。所以严格说：

    - 这次改的**不是**"本来能用、被改坏了"，而是"本来**静默**算错 → 重写后变成**响亮**报错；
      按'先出现的分隔符为准'之后才第一次真正可用"；
    - 真正的**退化**只有一条：重写后这个响亮报错会让**整个项目** exit 1
      （HEAD 至少还能把用例跑起来），影响面从"某个变量取不到"扩大到"所有用例都跑不起来"。

    本类同时钉住**方向不能反过来**：`=` 在前时仍按等号写法，值里的 `:` 原样保留。
    """

    def test_colon_form_with_equals_in_value_parses(self):
        """M1 现场：冒号写法 + 值尾 `==`（修复前抛 FileFormatError）。"""
        parsed = self._parse("B64_SECRET: dGhpc2lzYQ==\n")
        self.assertEqual(parsed, {"B64_SECRET": "dGhpc2lzYQ=="})

    def test_colon_form_with_query_string_url_parses(self):
        """冒号写法 + URL 带 query：query 里必然有 `=`，是最容易被忽略的一类。

        NOTICE: 既有用例 `test_colon_form_value_with_url_is_not_mangled` 的 URL
        **恰好没有 query**，所以它在修复前也是绿的 —— 那条用例没盖住这个坑，
        这里补上带 query 的形态。
        """
        parsed = self._parse("ENDPOINT: http://127.0.0.1:8080/api?x=1&y=2\n")
        self.assertEqual(parsed["ENDPOINT"], "http://127.0.0.1:8080/api?x=1&y=2")

    def test_colon_form_with_equals_and_colons_in_value(self):
        """值里同时有 `=` 与 `:`：两者都属于值，一个都不能被切掉。"""
        parsed = self._parse("MIX: a=b:c=d\n")
        self.assertEqual(parsed, {"MIX": "a=b:c=d"})

    def test_export_prefix_with_colon_form_and_equals_in_value(self):
        """`export` 前缀 + 冒号写法 + 值里含 `=`：三条适配同时生效。"""
        parsed = self._parse("export EXPORTED: e=v\n")
        self.assertEqual(parsed, {"EXPORTED": "e=v"})

    def test_equals_form_with_colon_in_value_is_unchanged(self):
        """反向护栏：`=` 在前时**不能**被判成冒号写法。"""
        parsed = self._parse("URL_WITH_EQ=https://h/a?b=1&c=2\n")
        self.assertEqual(parsed, {"URL_WITH_EQ": "https://h/a?b=1&c=2"})

    def test_earliest_separator_wins_when_equals_comes_first(self):
        """反向护栏：`A=B: C` 的键是 `A`，值是 `B: C`（口径的另一个方向）。"""
        parsed = self._parse("A=B: C\n")
        self.assertEqual(parsed, {"A": "B: C"})

    def test_mixed_separator_file_loads_as_a_whole(self):
        """整文件（冒号行与等号行混排）必须整体可解析。

        修复前这里是 **5 行一起**被丢弃 → 一次报 5 条"静默丢弃"，
        用户根本看不出是哪一种写法出的问题。
        """
        parsed = self._parse(
            "B64_SECRET: dGhpc2lzYQ==\n"
            "KEY: a=b\n"
            "ENDPOINT: http://127.0.0.1:8080/api?x=1&y=2\n"
            "PLAIN: value\n"
            "URL_WITH_EQ=https://h/a?b=1&c=2\n"
        )
        self.assertEqual(
            parsed,
            {
                "B64_SECRET": "dGhpc2lzYQ==",
                "KEY": "a=b",
                "ENDPOINT": "http://127.0.0.1:8080/api?x=1&y=2",
                "PLAIN": "value",
                "URL_WITH_EQ": "https://h/a?b=1&c=2",
            },
        )

    def test_drop_message_no_longer_blames_mixed_separators(self):
        """报错文案不许再指向一个**已经修好**的原因。

        「冒号与等号混用」曾被写进"静默丢弃"的常见原因列表；判据改成
        "先出现的分隔符为准"之后它**不再是**原因（`KEY: a=b` 现在能正常解析）。
        留着这句话会让用户朝错误方向改文件 —— 与"静默丢弃"本身一样难查，
        而本仓的取向是「错误提示一旦说谎，就等于没有」。
        """
        with self.assertRaises(exceptions.FileFormatError) as ctx:
            self._parse("GOOD=1\nKEY WITH SPACE=v\n")

        message = str(ctx.exception)
        self.assertIn("静默丢弃", message)
        self.assertNotIn("混用", message)


class TestDotEnvColonFormDoesNotBlockTheProject(unittest.TestCase):
    """M1 的**影响面**：一行冒号写法让整个项目的 hmake/hrun 硬失败。

    上面那组钉的是解析层；这里走**真实入口** `loader.load_project_meta`
    —— 它是 `hmake` / `hrun` 加载项目 `.env` 与 `debugtalk.py` 的必经之路。
    修复前它在 `.env` 上抛 `FileFormatError`，调用方只能 exit 1；
    这里证明修复后：① 项目能加载；② 那个变量**真的能用**
    （既进了 `os.environ`，也在 `project_meta.env` 里）。
    """

    def setUp(self) -> None:
        self._env_backup = dict(os.environ)
        self.addCleanup(self._restore_env)
        loader.reset_project_meta()
        self.addCleanup(loader.reset_project_meta)

        self.tmp_dir = os.path.join(
            os.getcwd(), "logs", f"tmp_proj_{uuid.uuid4().hex[:8]}"
        )
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)

        # 项目标记：load_project_meta 靠 debugtalk.py 定位 RootDir
        with open(
            os.path.join(self.tmp_dir, "debugtalk.py"), "w", encoding="utf-8"
        ) as f:
            f.write("")

        self.yml_path = os.path.join(self.tmp_dir, "case.yml")
        with open(self.yml_path, "w", encoding="utf-8") as f:
            f.write(
                "config:\n"
                "    name: m1_probe\n"
                "    base_url: http://127.0.0.1:1\n"
                "teststeps:\n"
                "    -\n"
                "        name: s\n"
                "        request:\n"
                "            method: GET\n"
                "            url: /get\n"
            )

        with open(
            os.path.join(self.tmp_dir, ".env"), "w", encoding="utf-8"
        ) as f:
            f.write("B64_SECRET: dGhpc2lzYQ==\n")

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_project_with_colon_env_loads_and_exposes_the_variable(self):
        meta = loader.load_project_meta(
            self.yml_path, reload=True, keep_loaded_meta_for_projectless=False
        )

        self.assertEqual(
            meta.env.get("B64_SECRET"),
            "dGhpc2lzYQ==",
            "冒号写法 + 值里含 `=` 的变量没有进入 project_meta.env",
        )
        self.assertEqual(
            os.environ.get("B64_SECRET"),
            "dGhpc2lzYQ==",
            "变量没有写进 os.environ —— 用例里 ${ENV(B64_SECRET)} 会取不到值",
        )


class TestDotEnvParsingIsIsolated(unittest.TestCase):
    """解析与写环境分离：`_parse_dot_env_file` 不得产生 `os.environ` 副作用。

    分离之后，「解析正确性」和「写环境语义」可以各自独立测（见上面的 override 用例），
    也是把 `dotenv_values`（不写环境）和 `load_dotenv`（写环境）区分开的落点。
    """

    def test_parse_helper_does_not_touch_os_environ(self):
        backup = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(backup)))

        path = _env_file("SIDE_EFFECT_KEY=value\n")
        self.addCleanup(os.remove, path)

        parsed = loader._parse_dot_env_file(path)

        self.assertEqual(parsed, {"SIDE_EFFECT_KEY": "value"})
        self.assertNotIn(
            "SIDE_EFFECT_KEY",
            os.environ,
            "`_parse_dot_env_file` 产生了 os.environ 副作用，破坏了解析/写环境的职责分离",
        )


class TestDotEnvCrossProjectIsolation(unittest.TestCase):
    """0920 批次 2 / **N17**：切到"没有 `.env`"的项目时，必须清掉上一个项目的键。

    ## 修复前的现场（`.tmp_audit/verify_env_iso.py`，真 `hrun`）

    ```text
    projA 有 .env: PROJ=projA     → os.environ["PROJ"] = "projA"
    projC 没有 .env               → 什么也不做（"PROJ" 仍是 "projA"）
    projC 的 ${ENV(PROJ)}         → 静默解析成 "projA" → 请求打到 A 的环境/凭据上

    只跑 projC      : EnvNotFound: PROJ        （正确：该项目确实没定义）
    projA + projC   : 没有报错，请求 URL 里是 proj=projA  ← **串环境**
    ```

    根因：`.env` 靠写 `os.environ` 生效（**全局副作用**），而 `set_os_environ` 只写不撤
    （`utils.unset_os_environ` 此前全仓无调用）。修复前只有 M7 补了
    "换回**已缓存**项目时重新应用它的 `.env`" 那半边；
    "切到**没有** `.env` 的项目时清掉上一个项目的键" 一直是空的。

    `docs/能力清单.md` 声称"同进程多项目互不串环境 ✅"—— 本组用例把这个承诺钉住。
    """

    def setUp(self):
        self._env_backup = dict(os.environ)
        self.addCleanup(self._restore)
        # 每个用例从"没有上一个项目"的干净状态开始
        loader._project_env_keys = {}

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        loader._project_env_keys = {}

    def _project_with_env(self, content: str) -> str:
        """造一个带 `.env` 的项目目录，返回 `.env` 路径。"""
        directory = os.path.join(
            os.getcwd(), "logs", f"tmp_proj_{uuid.uuid4().hex[:8]}"
        )
        os.makedirs(directory, exist_ok=True)
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        env_path = os.path.join(directory, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(content)
        return env_path

    def _project_without_env(self) -> str:
        directory = os.path.join(
            os.getcwd(), "logs", f"tmp_proj_{uuid.uuid4().hex[:8]}"
        )
        os.makedirs(directory, exist_ok=True)
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return os.path.join(directory, ".env")  # 不创建这个文件

    def test_previous_project_keys_are_removed_when_next_project_has_no_env(self):
        """核心判据：A 有 `.env` → C 没有 → C 加载后 `PROJ` **必须消失**。"""
        env_a = self._project_with_env("PROJ=projA\n")
        loader.apply_project_dot_env(env_a)
        self.assertEqual(os.environ.get("PROJ"), "projA")

        # C 没有 .env
        loader.apply_project_dot_env(self._project_without_env())

        self.assertNotIn(
            "PROJ",
            os.environ,
            "切到没有 .env 的项目后，上一个项目的环境变量仍在 —— 这就是 N17 的串环境",
        )

    def test_switching_between_two_projects_uses_each_own_values(self):
        """两个项目各有自己的 `.env`：各自的键各自生效。"""
        env_a = self._project_with_env("PROJ=projA\nONLY_A=1\n")
        env_b = self._project_with_env("PROJ=projB\nONLY_B=2\n")

        loader.apply_project_dot_env(env_a)
        self.assertEqual(os.environ.get("PROJ"), "projA")
        self.assertEqual(os.environ.get("ONLY_A"), "1")

        loader.apply_project_dot_env(env_b)
        self.assertEqual(os.environ.get("PROJ"), "projB", "没有换成 B 的值")
        self.assertNotIn("ONLY_A", os.environ, "A 独有的键没有被清掉")
        self.assertEqual(os.environ.get("ONLY_B"), "2")

    def test_externally_set_variable_is_not_clobbered(self):
        """反向护栏：用户**自己**在 shell 里设的同名变量不许被框架清掉。

        撤销只针对"值仍等于框架写进去的那个值"的键 —— 外部改过就归外部管。
        """
        env_a = self._project_with_env("PROJ=projA\n")
        loader.apply_project_dot_env(env_a)
        # 用户在被测环境里手动覆盖
        os.environ["PROJ"] = "user-override"

        loader.apply_project_dot_env(self._project_without_env())

        self.assertEqual(
            os.environ.get("PROJ"),
            "user-override",
            "框架把用户手动设置的环境变量也清掉了（越权）",
        )

    def test_project_without_env_sees_nothing_from_the_previous_one(self):
        """端到端的判据：C 的 `${ENV(...)}` 必须**取不到** A 的值（而不是取到 A 的值）。

        `utils.get_os_environ` 就是 `${ENV(NAME)}` 的运行期实现（取不到抛 `EnvNotFound`）。
        """
        from interfacetester import utils

        env_a = self._project_with_env("PROJ=projA\n")
        loader.apply_project_dot_env(env_a)
        self.assertEqual(utils.get_os_environ("PROJ"), "projA")

        loader.apply_project_dot_env(self._project_without_env())

        with self.assertRaises(exceptions.EnvNotFound):
            utils.get_os_environ("PROJ")


class TestDotEnvExistingExampleStillLoads(unittest.TestCase):
    """仓库自带的示例 `.env` 必须照旧解析（跨用例的实测夹具回归）。"""

    def test_examples_httpbin_test_env(self):
        path = os.path.join(os.getcwd(), "examples", "httpbin", "test.env")
        before = dict(os.environ)
        try:
            parsed = loader.load_dot_env_file(path)
        finally:
            os.environ.clear()
            os.environ.update(before)

        self.assertEqual(parsed["UserName"], "test")
        self.assertEqual(parsed["PROJECT_KEY"], "AAABBBCCC")
        self.assertEqual(
            parsed["content_type"], "application/json; charset=UTF-8"
        )
