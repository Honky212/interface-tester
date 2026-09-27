# -*- coding: utf-8 -*-
r"""前端的**不变量**用例 —— v8 §5.8 行 1004（**P3d**）。

## 行 1004 的两条验收，这里逐条钉住

| 原文 | 判据 |
| --- | --- |
| **无前端构建链** | ① 仓库里没有 `package.json` / `node_modules/` / 打包器配置；② 页面里没有 `import` / `<script type="module">` |
| **离线可直接打开** | ① **每一页**的外部引用数为 **0**；② `<meta charset>` 与 `<html lang>` 都在 |

## 另加三条（**实测驱动**，不是想出来的）

1. **导航里列出的每一项都必须真能到** —— 加导航项却忘了实现路由，表现是"点进去 404"；
   这条判据把它变成红灯。（P3d 之前 `/audit` 就是**既没页面也没入口**。）
2. **每页恰好一个 `<h1>`、表格都有 `<thead>`、按钮都有文字** ——
   这是"离线环境下也能用"的最低线（没有 CSS 框架兜着时尤其明显）。
3. **★不做自动刷新** —— 判据：页面里**不出现定时器**。
   理由不是"省事"：`/pending` 与 `/fix` 的表单是**长 JSON**，一次自动刷新就**全清空**了，
   而"我以为填好了、其实被刷没了"是最坏的一种丢失。（所以表单页有明确提示。）
"""

import os
import re
import sys
import threading
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.web import (  # noqa: E402
    ALLOWED_ROOTS,
    DEFAULT_HOST,
    fetch,
    make_server,
)

# 能到的每一页（导航里列出的 + 有入口的）
PAGES = ("/", "/metrics", "/audit", "/pending", "/jobs")

# 构建链的**指纹**：这些文件/目录一旦出现，"无构建链"就不成立了
BUILD_CHAIN_FINGERPRINTS = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "node_modules",
    "webpack.config.js",
    "vite.config.js",
    "rollup.config.js",
    "postcss.config.js",
    "tailwind.config.js",
    "tsconfig.json",
)


class _ServerCase(unittest.TestCase):
    def setUp(self):
        self.server = make_server(DEFAULT_HOST, 0, ALLOWED_ROOTS)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def html(self, path):
        status, body = fetch(self.port, path)
        self.assertEqual(status, 200, f"`{path}` 应当可访问")
        return body.decode("utf-8")


class TestNoBuildChain(_ServerCase):
    """★行 1004 的第一条：**无前端构建链**。"""

    def test_repository_has_no_build_chain_files(self):
        offenders = [
            name
            for name in BUILD_CHAIN_FINGERPRINTS
            if os.path.exists(os.path.join(BASE, name))
        ]

        self.assertEqual(
            offenders,
            [],
            f"仓库里出现了构建链指纹：{offenders}——P3d 的前提是**没有构建链**",
        )

    def test_pages_have_no_module_imports(self):
        """页面里不许有 `import` / `<script type="module">`：那是构建链的信号。"""
        for path in PAGES:
            with self.subTest(page=path):
                text = self.html(path)

                self.assertNotIn('<script type="module"', text)
                self.assertNotRegex(text, re.compile(r"(?m)^\s*import\s+[\w{*]"))


class TestOfflineSelfContained(_ServerCase):
    """★行 1004 的第二条：**离线可直接打开**（页面自包含）。"""

    _EXTERNAL = re.compile(r"^(?:https?:)?//", re.I)
    _REF = re.compile(r'(?:src|href)\s*=\s*"([^"]*)"', re.I)

    def test_no_external_references_on_any_page(self):
        for path in PAGES:
            with self.subTest(page=path):
                offenders = [
                    url
                    for url in self._REF.findall(self.html(path))
                    if self._EXTERNAL.match(url)
                ]

                self.assertEqual(
                    offenders, [], f"`{path}` 引用了外部资源：{offenders}（离线打不开）"
                )

    def test_encoding_and_language_declared(self):
        for path in PAGES:
            with self.subTest(page=path):
                text = self.html(path)

                self.assertIn('<meta charset="utf-8">', text.lower())
                self.assertIn('lang="zh-CN"', text)


class TestNavigationReachable(_ServerCase):
    """★**导航里列出的每一项都必须真能到**（实测驱动）。"""

    def test_every_nav_link_is_reachable(self):
        text = self.html("/")
        nav = re.findall(r"<nav>(.*?)</nav>", text, re.S)

        self.assertTrue(nav, "首页没有 `<nav>`")

        hrefs = re.findall(r'href="([^"]+)"', nav[0])

        self.assertGreaterEqual(len(hrefs), 5, f"导航项太少：{hrefs}")
        for href in hrefs:
            with self.subTest(href=href):
                status, _body = fetch(self.port, href)

                self.assertEqual(status, 200, f"导航里的 `{href}` 到不了")

    def test_audit_page_is_in_the_navigation(self):
        """P3d 之前 `/audit` **既没有页面、也没有入口**——这条钉住它不再退化。"""
        self.assertIn('href="/audit"', self.html("/"))

    def test_run_detail_links_to_fix(self):
        """`/fix` 需要一份 `summary.json`——**运行明细页**正是它最自然的入口。

        （P3d 实测发现：`/fix` 页面早就实现了，却**没有任何页面能到它**。）
        """
        from interfacetester_ai.web import render_run  # noqa: PLC0415

        summary = {
            "success": False,
            "stat": {
                "testcases": {"fail": 1, "success": 0, "total": 1},
                "teststeps": {"failures": 1, "successes": 0, "total": 1},
            },
            "details": [
                {
                    "name": "c",
                    "success": False,
                    "records": [
                        {
                            "name": "s",
                            "step_type": "request",
                            "success": False,
                            "data": {
                                "address": "http://127.0.0.1:80/get",
                                "validators": {
                                    "validate_extractor": [
                                        {
                                            "check": "status_code",
                                            "check_result": "pass",
                                            "check_value": 200,
                                            "comparator": "equal",
                                            "expect": 200,
                                            "message": "",
                                        }
                                    ]
                                },
                            },
                        }
                    ],
                }
            ],
        }
        from interfacetester_ai.web import extract_assertions  # noqa: PLC0415

        rows, problems = extract_assertions(summary)
        html = render_run("logs/x.summary.json", summary, rows, problems)

        self.assertIn("/fix?path=logs/x.summary.json", html)


class TestBasicUsability(_ServerCase):
    """没有 CSS 框架兜着时，"能不能用"的最低线。"""

    def test_exactly_one_h1_per_page(self):
        for path in PAGES:
            with self.subTest(page=path):
                self.assertEqual(self.html(path).count("<h1>"), 1)

    def test_every_table_has_a_thead(self):
        for path in PAGES:
            with self.subTest(page=path):
                text = self.html(path)

                self.assertEqual(
                    text.count("<table>"),
                    text.count("<thead>"),
                    f"`{path}` 里有表格没有表头",
                )

    def test_buttons_have_text(self):
        """按钮**都有文字**（离线环境不该依赖图标字体）。"""
        for path in ("/pending", "/jobs"):
            with self.subTest(page=path):
                for label in re.findall(r"<button[^>]*>(.*?)</button>", self.html(path)):
                    self.assertTrue(label.strip(), f"`{path}` 里有空按钮")


class TestNoAutoRefresh(_ServerCase):
    """★**不做自动刷新**（实测驱动：自动刷新会清空正在填的长表单）。"""

    def test_no_polling_timers_in_pages(self):
        for path in PAGES:
            with self.subTest(page=path):
                text = self.html(path)

                self.assertNotIn("setInterval", text)
                self.assertNotIn("setTimeout", text)

    def test_form_pages_warn_about_losing_input(self):
        """有**提交动作**的页面必须提示：刷新/切页会清空内容。

        ★判据挂在"**有没有按钮**"上，而不是"是不是这两个路径"：
        `/pending` 在清单为空时**根本不渲染表单**（那就没有东西会被刷掉），
        此时要求它显示提示就是**打偏的判据**——它会把"如实显示空状态"判成违规。

        不提示的后果也不小："填了半天被刷没了"会被归因成"这工具不可靠"，
        而它其实是**设计选择**：我们不愿意用自动刷新去换"看起来实时"。
        """
        for path in ("/pending", "/jobs"):
            with self.subTest(page=path):
                text = self.html(path)

                if "<button" not in text:
                    continue  # 没有提交动作 → 没有会被刷掉的东西 → 不该要求提示
                self.assertIn("不会自动刷新", text)

    def test_a_page_with_a_form_really_shows_the_warning(self):
        """★元护栏：证明上一条不是"永远跳过"。

        `/jobs` 页**总是**渲染触发表单（不像 `/pending` 依赖清单是否存在），
        所以它必须有提示——它是上一条判据的**非空样本**。
        """
        text = self.html("/jobs")

        self.assertIn("<button", text, "`/jobs` 应当总是有触发表单")
        self.assertIn("不会自动刷新", text)



if __name__ == "__main__":
    unittest.main()

