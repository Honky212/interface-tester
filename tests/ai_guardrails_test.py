"""建议③的三处护栏用例：L3 闸门 / fix 补丁校验 / prompt 一致性对账。

对应《方案2-对5.0.1对账.md》「建议③」以及 `bench/ai_guardrails.py` 的实现。
本文件把三处**结构性**护栏钉进 CI：

- **L3 闸门**（默认拒绝真发请求）——防「LLM 臆造出 `DELETE /api/order/1` 打到生产」；
- **fix 补丁校验**（不过加载不许输出 diff）——防「自愈变自伤」；
- **prompt 对账**（渲染结果必须等于内核常量）——防本仓已发生两次的同类漂移。

钉法沿用本仓惯例：**每条护栏都要有反向护栏**（正常输入不许被误伤），
否则「假报」会让真报被无视。
"""

import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from bench.ai_guardrails import (  # noqa: E402
    ALLOW_HOST_ENV,
    L3Refused,
    SANDBOX_ENV,
    audit_operator_guidance,
    audit_prompt_against_kernel,
    check_l3_allowed,
    ensure_l3_allowed,
    get_comparator_aliases,
    render_system_prompt,
    validate_patch_candidate,
)

VALID_CASE = (
    "config:\n"
    "  name: t\n"
    "  base_url: http://127.0.0.1:1\n"
    "teststeps:\n"
    "  - name: s\n"
    "    request:\n"
    "      method: GET\n"
    "      url: /a\n"
    "    validate:\n"
    "      - eq: [status_code, 200]\n"
)


class TestL3SandboxGate(unittest.TestCase):
    """① L3 运行期冒烟闸门。"""

    def test_sandbox_is_off_by_default(self):
        """**核心**：没设开关时，一律不放行（默认值必须在安全的那一侧）。"""
        ok, reason = check_l3_allowed("http://127.0.0.1:8000", env={})
        self.assertFalse(ok)
        self.assertIn(SANDBOX_ENV, reason)

    def test_loopback_allowed_when_sandbox_on(self):
        """开了沙盒后，回环地址放行（正常的本地冒烟不能被误伤）。"""
        for url in ("http://127.0.0.1:8000", "http://localhost:80", "http://127.0.0.5:9000"):
            with self.subTest(url=url):
                ok, reason = check_l3_allowed(url, env={SANDBOX_ENV: "on"})
                self.assertTrue(ok, reason)

    def test_non_loopback_refused_without_whitelist(self):
        """**核心**：生产域名在无白名单时必须拒绝，且说明怎么放行。"""
        ok, reason = check_l3_allowed("https://api.production.com", env={SANDBOX_ENV: "on"})
        self.assertFalse(ok)
        self.assertIn("api.production.com", reason)
        self.assertIn(ALLOW_HOST_ENV, reason)

    def test_explicit_whitelist_allows(self):
        """显式白名单可以放行非回环主机（受控放行路径必须真的能用）。"""
        ok, reason = check_l3_allowed(
            "https://api.test.internal", env={SANDBOX_ENV: "on", ALLOW_HOST_ENV: "api.test.internal"}
        )
        self.assertTrue(ok, reason)

    def test_whitelist_supports_multiple_hosts(self):
        """白名单支持逗号/空白分隔的多个主机。"""
        env = {SANDBOX_ENV: "on", ALLOW_HOST_ENV: "a.com, b.com  c.com"}
        for host in ("a.com", "b.com", "c.com"):
            with self.subTest(host=host):
                ok, _ = check_l3_allowed(f"http://{host}", env=env)
                self.assertTrue(ok)
        ok, _ = check_l3_allowed("http://d.com", env=env)
        self.assertFalse(ok)

    def test_env_flag_accepts_common_truthy_forms(self):
        """开关的写法容错（on/1/true/yes），但 false 一律视为关。"""
        for flag in ("on", "1", "true", "yes", "ON", "True"):
            with self.subTest(flag=flag):
                ok, _ = check_l3_allowed("http://127.0.0.1:1", env={SANDBOX_ENV: flag})
                self.assertTrue(ok)
        for flag in ("off", "0", "false", "no", ""):
            with self.subTest(flag=flag):
                ok, _ = check_l3_allowed("http://127.0.0.1:1", env={SANDBOX_ENV: flag})
                self.assertFalse(ok)

    def test_malformed_url_is_refused_not_crashed(self):
        """反向护栏：畸形 base_url 要**拒绝**，而不是抛异常崩掉。"""
        for url in ("", "not-a-url", "http://"):
            with self.subTest(url=url):
                ok, reason = check_l3_allowed(url, env={SANDBOX_ENV: "on"})
                self.assertFalse(ok)
                self.assertTrue(reason)

    def test_ensure_l3_allowed_raises_never_skips(self):
        """**核心**：拒绝必须是**异常**，不能静默降级成「跳过」。

        静默跳过会让用户以为冒烟跑过了（本仓最忌讳的假通过形态）。
        """
        with self.assertRaises(L3Refused):
            ensure_l3_allowed("https://api.production.com", env={SANDBOX_ENV: "on"})
        # 合法情形不抛
        ensure_l3_allowed("http://127.0.0.1:8000", env={SANDBOX_ENV: "on"})


class TestFixPatchValidation(unittest.TestCase):
    """② fix 补丁必须先过 `load_testcase_file`。"""

    def test_valid_patch_is_accepted(self):
        ok, reason = validate_patch_candidate(VALID_CASE)
        self.assertTrue(ok, reason)

    def test_duplicate_key_patch_is_rejected(self):
        """**核心**：本仓有重复键硬闸门，round-trip 产出的重复键必须在**输出 diff 前**被拦。

        否则「自愈建议」贴回去会让用例直接报错——把自愈变成自伤。
        """
        dup = VALID_CASE + "    validate:\n      - eq: [body.code, 0]\n"
        ok, reason = validate_patch_candidate(dup)
        self.assertFalse(ok)
        self.assertIn("重复", reason)

    def test_unparseable_patch_is_rejected(self):
        """语法坏掉的 YAML 必须被拒（不能把坏补丁当有效建议输出）。"""
        ok, _ = validate_patch_candidate("config: [unclosed\n")
        self.assertFalse(ok)

    def test_patch_validation_does_not_leave_files(self):
        """校验是**只读**的：不能在校验目录里留下临时文件。"""
        import glob

        probe_dir = os.path.join(BASE, ".tmp_golden")
        before = set(glob.glob(os.path.join(probe_dir, "*.yml"))) if os.path.isdir(probe_dir) else set()
        validate_patch_candidate(VALID_CASE)
        after = set(glob.glob(os.path.join(probe_dir, "*.yml"))) if os.path.isdir(probe_dir) else set()
        self.assertEqual(before, after, "补丁校验留下了临时文件")


class TestPromptConsistency(unittest.TestCase):
    """③ prompt 与内核常量的一致性对账。"""

    def test_prompt_matches_kernel_constants(self):
        """**核心**：渲染结果必须与内核常量完全一致（漏一个即失败）。"""
        problems = audit_prompt_against_kernel()
        self.assertEqual(problems, [], "prompt 与内核常量不一致：\n" + "\n".join(problems))

    def test_prompt_contains_x_path(self):
        """反向护栏：`x_path` 是新版 `TRequest` 的实际字段，
        v1 的手抄清单漏过它——动态渲染必须自动带上。"""
        prompt = render_system_prompt()
        self.assertIn("x_path", prompt)

    def test_prompt_contains_all_22_comparators(self):
        """22 个内置算子必须一个不少地出现在 prompt 里。"""
        from interfacetester.make import BUILTIN_COMPARATOR_NAMES

        prompt = render_system_prompt()
        missing = [name for name in BUILTIN_COMPARATOR_NAMES if name not in prompt]
        self.assertEqual(missing, [], f"prompt 里缺少这些算子：{missing}")

    def test_prompt_warns_about_no_double_question_mark(self):
        """`??` 语法在本 fork **不存在**，prompt 必须显式警告（v1 的坑）。"""
        prompt = render_system_prompt()
        self.assertIn("??", prompt)
        self.assertIn("没有", prompt)

    def test_prompt_forbids_quoted_function_args(self):
        """函数参数不能带引号（`parser` 的正则不接受引号），prompt 必须写明。"""
        prompt = render_system_prompt()
        self.assertIn("不能带引号", prompt)

    def test_prompt_mentions_non_json_body_trap(self):
        """非 JSON 响应上 `body` 是 bytes —— prompt 必须给出这条负面示例。"""
        prompt = render_system_prompt()
        self.assertIn("非 JSON", prompt)

    def test_alias_table_is_derived_from_kernel(self):
        """别名表必须从 `get_uniform_comparator` 反推，而不是手抄。"""
        aliases = get_comparator_aliases()
        self.assertIn("equal", aliases)
        self.assertIn("eq", aliases["equal"])

    def test_audit_catches_a_removed_constant(self):
        """**判据自检**：从 prompt 里抹掉一个算子名，对账必须报出来。

        没有这一条，「对账通过」可能只是因为对账根本没在检查任何东西
        （本仓把这类叫「护栏打偏」）。
        """
        from interfacetester.make import BUILTIN_COMPARATOR_NAMES

        victim = sorted(BUILTIN_COMPARATOR_NAMES)[0]
        prompt = render_system_prompt()
        # 只删第一次出现，模拟「漏了一个」
        damaged = prompt.replace(victim, "", 1)
        self.assertNotEqual(prompt, damaged)
        problems = audit_prompt_against_kernel(damaged)
        self.assertTrue(
            any(victim in p for p in problems),
            f"对账没能发现被抹掉的算子 {victim!r}：{problems}",
        )


class TestOperatorGuidance(unittest.TestCase):
    """§9.23：**算子用法的通用正例**（它说的是内核语义，与具体被测项目无关）。

    为什么这条要单独有判据：它是"模型爱写 `contains: [body, 'FILE_1018']`"这个实测现象的**唯一**通用对策
    （判据层③-b 只能事后摘掉断言，摘掉不等于模型学会了）。判据有三条：
    ① 正例在场；② 说清了"`contains` 测的是**键**"这个内核语义；③ **元护栏**——删掉它必须被抓住。
    """

    def test_guidance_is_present_in_system_prompt(self):
        prompt = render_system_prompt()
        self.assertEqual(
            audit_operator_guidance(prompt),
            [],
            "system prompt 里缺算子用法正例（`contains` 对字典测的是键 → 断取值要用 equal）",
        )

    def test_guidance_states_kernel_semantics(self):
        """正例必须给出**正确写法**（不是只说"别用 contains"）。"""
        prompt = render_system_prompt()
        self.assertIn("body.errorCode", prompt)
        self.assertIn("键", prompt)

    def test_audit_catches_removed_guidance(self):
        """**判据自检**：抹掉正例里的关键串，对账必须报出来（否则"通过"可能只是没在检查）。

        本仓把这类叫「护栏打偏」——判据必须能被证明**会红**。
        """
        prompt = render_system_prompt()
        damaged = prompt.replace("body.errorCode", "")
        self.assertNotEqual(prompt, damaged)
        self.assertIn("body.errorCode", audit_operator_guidance(damaged))

    def test_guidance_is_not_project_specific(self):
        """正例里**不许**出现某个被测项目的专名（通用性是口径，不是愿望）。"""
        prompt = render_system_prompt()
        for word in ("unifsp", "统一文件", "FILE_1018 错误码表"):
            self.assertNotIn(word, prompt, f"prompt 里混进了项目专名：{word}")


if __name__ == "__main__":
    unittest.main()
