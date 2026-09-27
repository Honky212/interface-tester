# -*- coding: utf-8 -*-
"""用例自愈建议的护栏用例 —— v8 §5.8（**P2a**）。

## §5.8 的四条验收，本文件逐条钉住

| 验收 | 用例 |
| --- | --- |
| **零写盘**（目标 YAML mtime 不变） | `TestGateAndZeroWrite::test_target_case_file_mtime_is_unchanged` |
| 补丁必须过 `load_testcase_file` + S 系列 | `TestGateAndZeroWrite::test_downgrading_patch_is_rejected` |
| **业务码变化样本 100% 拒绝给改** | `TestBusinessCodeNeverSuggested::*` |
| **"字段改名"注入样本建议命中 ≥8/10** | `TestFieldRenameInjection::test_rename_samples_hit_at_least_eight_of_ten` |

## 本文件里最要紧的两条

1. **顺序判据**（`test_business_code_is_refused_even_when_value_is_none`）：
   判"不给改"必须在"可建议"**之前**。反过来的话，`body.code`（业务码，且取不到值）
   会先命中"路径失效"从而拿到一条建议——**而那正是最不该碰的一类**。
2. **`--apply` 永不存在**（`test_cli_has_no_apply_flag`）：
   §5.8 明写"`--apply` **永不存在**（人工自己贴）"。这不是因为怕写错，
   而是因为**自愈改错会掩盖真实缺陷**（R3）——失败会被人看见，"修好了"不会。

## ★§9.25 扩触发面之后的**边界**（`TestTriggerScopeBoundary`）

§5.8 把触发条件钉死在两族（「`check_value is None` 的路径类」+「`type_match`/字段名类」），
所以"扩面"是在**这两族之内把覆盖做全**，并**每扩一分配一道更硬的闸**：

| 面 | 硬闸 | 判据 |
| --- | --- | --- |
| **同容器改名**（`body`/`headers`） | 候选**只在同一容器**里找、`new_check` **保留原根**；根不认识 → 拒 | `test_headers_missing_is_suggested_within_headers`（正）+ `…is_never_rewritten_into_a_body_path`（反） |
| **`${var}` 变量族** | 候选唯一 + **新名字的取值路径必须在响应里真能取到值**；`${fn(...)}`/`${code}` 仍拒 | `test_variable_*`（正 1 / 反 3） |
| **值语义类**（期望值不符、字符串数字） | **一律拒**（可能是接口真改了行为） | `test_value_semantics_are_refused` |

★两条**元护栏**（证明闸是"吃劲"的，不是摆设）：把 `container_root` 摘掉 → `headers` 那条
会被改成 `body.*`；把 `find_candidate_paths` 换成"永远有候选" → 取不到值的变量也会拿到补丁。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai import fix as fix_mod  # noqa: E402
from interfacetester_ai.fix import (  # noqa: E402
    FIXABLE_PATH_MISSING,
    FIXABLE_TYPE_MISMATCH,
    FIXABLE_VAR_MISSING,
    REFUSE_OTHER,
    REFUSE_REAL_DEFECT,
    build_patched_text,
    classify_failure,
    container_root,
    find_candidate_names,
    find_candidate_paths,
    is_business_code_check,
    locate_assertion,
    propose_fix_for,
    propose_fixes,
    read_extract_map,
    run_selftest,
    unified_diff,
    validate_patch,
    variable_name,
)

CASE = """config:
  name: demo
teststeps:
  - name: 查询
    request:
      method: GET
      url: /api/user
    validate:
      - eq: ["body.data.token", "abc"]
"""

# 注入样本用的模板（`check` 里没有引号，format 安全）
CASE_TEMPLATE = """config:
  name: demo
teststeps:
  - name: 查询
    request:
      method: GET
      url: /api/user
    validate:
      - eq: ["{check}", "abc"]
"""

# 双前置的**合规基线**：`type_match` 的 expect 是类型名（S7 认）
CASE_TYPED = """config:
  name: demo
teststeps:
  - name: 查询
    request:
      method: GET
      url: /api/user
    validate:
      - type_match: ["body.data.token", "str"]
"""

# 一份"把可判定改成不可判定"的补丁（红线⑦要拦的形态）
DOWNGRADED = """config:
  name: demo
teststeps:
  - name: 查询
    request:
      method: GET
      url: /api/user
    validate:
      - not_equal: ["body.data.token", ""]
"""


def validator(check, check_value=None, comparator="equal", **kw):
    payload = {
        "comparator": comparator,
        "check": check,
        "check_value": check_value,
        "expect_value": None,
        "check_result": "fail",
    }
    payload.update(kw)
    return payload


class _TempWorkspace(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="ai_fix_")
        os.chdir(self._tmp.name)
        os.makedirs("cases", exist_ok=True)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def write_case(self, text=CASE, name="demo"):
        path = os.path.join("cases", name + ".yml")
        with open(path, "w", encoding="utf-8") as fp:
            fp.write(text)
        return path


class TestTriggerConditions(unittest.TestCase):
    """**触发条件写进代码，不靠 prompt**（§5.8 原文）。"""

    def test_protocol_status_code_is_refused(self):
        self.assertEqual(classify_failure(validator("status_code", 500)), REFUSE_REAL_DEFECT)

    def test_business_code_is_refused_even_when_value_is_none(self):
        """★**顺序判据**：先判"不给改"，再判"可建议"。

        `body.code` 既"是业务码"又"取不到值"，两个条件同时成立——
        若顺序写反，它会拿到一条建议，而那正是最不该碰的一类。
        """
        self.assertEqual(classify_failure(validator("body.code", None)), REFUSE_REAL_DEFECT)

    def test_path_missing_is_fixable(self):
        self.assertEqual(
            classify_failure(validator("body.data.token", None)), FIXABLE_PATH_MISSING
        )

    def test_type_mismatch_is_fixable(self):
        self.assertEqual(
            classify_failure(validator("body.data.age", 18, "type_match")),
            FIXABLE_TYPE_MISMATCH,
        )

    def test_other_failures_are_not_touched(self):
        """触发条件之外的（普通值不符）**不越界**给建议。"""
        self.assertEqual(classify_failure(validator("body.name", "x")), REFUSE_OTHER)

    def test_business_code_segment_matching(self):
        """末段判定：`body.data.code` 算，`body.code_name` 不算（只看末段才分得开）。"""
        self.assertTrue(is_business_code_check("body.data.code"))
        self.assertTrue(is_business_code_check("data.result.ret"))
        self.assertFalse(is_business_code_check("body.code_name"))
        self.assertFalse(is_business_code_check("body.data.token"))


class TestMinimalEdit(unittest.TestCase):
    """行号定位 + **只动那一处**（自愈建议的最小区分度）。"""

    def test_locate_returns_the_right_line(self):
        spot = locate_assertion(CASE, step_name="查询", check="body.data.token")

        self.assertIsNotNone(spot)
        self.assertEqual(spot.check_line, 9)  # type: ignore[union-attr]
        self.assertEqual(spot.operator, "eq")  # type: ignore[union-attr]

    def test_only_that_line_changes(self):
        spot = locate_assertion(CASE, step_name="查询", check="body.data.token")
        patched = build_patched_text(CASE, spot, new_check="body.data.access_token")  # type: ignore[arg-type]

        before, after = CASE.splitlines(), (patched or "").splitlines()
        self.assertEqual(len(before), len(after))
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(changed, [8], "应当只改第 9 行（0-based 8）")
        self.assertIn("access_token", after[8])

    def test_diff_has_exactly_one_minus_and_one_plus(self):
        spot = locate_assertion(CASE, step_name="查询", check="body.data.token")
        patched = build_patched_text(CASE, spot, new_check="body.data.access_token")
        diff = unified_diff(CASE, patched or "", path="cases/demo.yml")

        self.assertIn("--- a/cases/demo.yml", diff)
        self.assertIn("+++ b/cases/demo.yml", diff)
        self.assertEqual(
            sum(
                1
                for line in diff.splitlines()
                if line.startswith("-") and not line.startswith("---")
            ),
            1,
        )
        self.assertEqual(
            sum(
                1
                for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            ),
            1,
        )

    def test_locate_returns_none_when_step_name_mismatches(self):
        """同一个 check 路径可能出现在多个 step 里——step 名必须也对得上。"""
        self.assertIsNone(locate_assertion(CASE, step_name="别的步骤", check="body.data.token"))

    def test_locate_returns_none_on_broken_yaml(self):
        """解析不了 → 返回 None（**不猜行号**），而不是抛异常。"""
        self.assertIsNone(locate_assertion("这不是 YAML: [::", step_name="x", check="y"))


class TestGateAndZeroWrite(_TempWorkspace):
    """★★ 两条硬验收：**双前置**与**零写盘**。"""

    def test_compliant_patch_passes_both_gates(self):
        spot = locate_assertion(CASE_TYPED, step_name="查询", check="body.data.token")
        patched = build_patched_text(CASE_TYPED, spot, new_check="body.data.access_token")

        passed, why = validate_patch(patched or "")

        self.assertTrue(passed, why)

    def test_downgrading_patch_is_rejected(self):
        """★红线⑦：把**可判定**改成**不可判定**的补丁必须被双前置拦下。

        `not_equal: [x, ""]` 是伪存在性断言——取不到值时**静默通过**。
        §5.8 说这种"自愈"制造的是**假通过**，比原来的失败更危险。
        """
        passed, why = validate_patch(DOWNGRADED)

        self.assertFalse(passed)
        self.assertIn("前置", why)

    def test_broken_patch_is_rejected(self):
        passed, _why = validate_patch("config: [这不是映射")

        self.assertFalse(passed)

    def test_target_case_file_mtime_is_unchanged(self):
        """★**零写盘**：跑完之后目标用例文件的 mtime 与内容都不变。"""
        path = self.write_case()
        before_mtime = os.stat(path).st_mtime_ns
        with open(path, encoding="utf-8") as fp:
            before_text = fp.read()

        propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="details[0].records[0].data.validators.validate[0]",
            validator=validator("body.data.token", None),
            case_dir="cases",
            response_body='{"data": {"access_token": "xyz"}}',
        )

        self.assertEqual(os.stat(path).st_mtime_ns, before_mtime, "目标用例文件被写了")
        with open(path, encoding="utf-8") as fp:
            self.assertEqual(fp.read(), before_text)

    def test_no_workdir_residue(self):
        """临时补丁落在 `.ai/work/`，退出即删（不留残留目录）。"""
        spot = locate_assertion(CASE_TYPED, step_name="查询", check="body.data.token")
        validate_patch(
            build_patched_text(CASE_TYPED, spot, new_check="body.data.access_token") or ""
        )

        leftovers = os.listdir(".ai/work") if os.path.isdir(".ai/work") else []
        self.assertEqual(leftovers, [])


class TestBusinessCodeNeverSuggested(_TempWorkspace):
    """★验收：**业务码变化样本 100% 拒绝给改**（且连 diff 都不给）。"""

    def test_business_code_failure_gets_no_diff(self):
        self.write_case()
        result = propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="details[0].records[0].data.validators.validate[0]",
            validator=validator("body.code", 1001),
            case_dir="cases",
            response_body='{"code": 1001}',
        )

        self.assertFalse(result.is_suggested())
        self.assertEqual(result.diff, "")
        self.assertIn("疑似真实缺陷", result.reason)

    def test_all_business_code_samples_are_refused(self):
        """★"100% 拒绝"的机器形态：**一批**业务码样本，一条都不许建议。"""
        self.write_case()
        samples = [
            ("status_code", 500),
            ("body.code", 1001),
            ("body.data.code", 0),
            ("body.errno", 9),
            ("data.result.ret", -1),
            ("body.error_code", "E400"),
        ]
        for index, (check, value) in enumerate(samples):
            with self.subTest(check=check):
                result = propose_fix_for(
                    case_name="demo",
                    step_name="查询",
                    pointer=f"details[0].records[0].data.validators.validate[{index}]",
                    validator=validator(check, value),
                    case_dir="cases",
                )
                self.assertFalse(result.is_suggested(), f"{check} 不该给建议")
                self.assertEqual(result.diff, "")

    def test_batch_entry_refuses_business_codes(self):
        """批量入口守同一条规矩。"""
        self.write_case()
        summary = {
            "details": [
                {
                    "name": "demo",
                    "success": False,
                    "records": [
                        {
                            "name": "查询",
                            "data": {
                                "validators": {
                                    "validate": [
                                        validator("status_code", 500),
                                        validator("body.code", 1001),
                                    ]
                                },
                                "req_resps": [{"response": {"body": '{"code": 1001}'}}],
                            },
                        }
                    ],
                }
            ]
        }

        result = propose_fixes(summary, case_dir="cases")

        self.assertEqual(len(result.suggestions), 2)
        self.assertEqual(result.suggested(), [])
        self.assertEqual(len(result.refused()), 2)


class TestFieldRenameInjection(_TempWorkspace):
    """★验收：**"字段改名"注入样本建议命中 ≥8/10**。

    10 个样本覆盖真实的改名形态：加前缀 / 去前缀 / 改连接符（`_`↔ 驼峰）/
    加后缀 / 加一层 / 去一层。

    ★"命中"的口径**故意严**：**给出建议**才算命中；"定位不到 / 多个候选不替人挑"
    算**未命中**。把"猜对了"和"没敢猜"混进命中率，指标就没意义了——
    而这里的"没敢猜"恰恰是**对的**行为（宁可让人看一眼，也不改歪用例）。
    """

    SAMPLES = [
        ("body.data.token", '{"data": {"access_token": "x"}}', "body.data.access_token"),
        ("body.data.access_token", '{"data": {"token": "x"}}', "body.data.token"),
        ("body.data.user_name", '{"data": {"userName": "x"}}', "body.data.userName"),
        ("body.data.userName", '{"data": {"user_name": "x"}}', "body.data.user_name"),
        ("body.data.id", '{"data": {"userId": 1}}', "body.data.userId"),
        ("body.data.user.id", '{"data": {"id": 1}}', "body.data.id"),
        ("body.data.total_count", '{"data": {"totalCount": 3}}', "body.data.totalCount"),
        ("body.data.sign", '{"data": {"signature": "x"}}', "body.data.signature"),
        ("body.items.token", '{"items": [{"token": "x"}]}', "items-project"),
        # 找不到任何线索 → **期望不给**（这条是"宁可不说"的正样本）
        ("body.data.uid", '{"data": {"a": 1}}', ""),
    ]

    def test_rename_samples_hit_at_least_eight_of_ten(self):
        """`items-project` 这个期望值表示"**唯一敢验的是它没乱改**"。

        `body.items.token` 那条：候选找到了（`items[].token`），但补丁**没过双前置**
        ——所以 `haify fix` **不给建议**。这是**正确**的行为（红线⑦ 优先于"给点建议"），
        于是它在命中统计里算**未命中**：把"闸门拦下"混进命中率，会让"命中率"变成
        "我们多敢猜"的度量——那不是我们要的。
        """
        hits = 0
        details = []
        for index, (check, body, expected) in enumerate(self.SAMPLES):
            with self.subTest(check=check):
                self.write_case(CASE_TEMPLATE.format(check=check), name=f"c{index}")
                result = propose_fix_for(
                    case_name=f"c{index}",
                    step_name="查询",
                    pointer="details[0].records[0].data.validators.validate[0]",
                    validator=validator(check, None),
                    case_dir="cases",
                    response_body=body,
                )
                suggested = result.is_suggested()
                if expected == "items-project":
                    # 只验"它没去乱改别的行"：要么不给，要么改对了路径
                    if not suggested or result.new_check == "body.items[].token":
                        hits += 1
                elif suggested and (not expected or result.new_check == expected):
                    hits += 1
                details.append(f"{check} → {result.new_check or '(不给)'}")

        self.assertGreaterEqual(hits, 8, f"命中 {hits}/10：{details}")

    def test_ambiguous_candidates_are_not_picked(self):
        """★多个候选 → **不替人挑**（这条比"匹配写得更聪明"更可靠）。"""
        self.write_case(CASE_TEMPLATE.format(check="body.data.token"), name="demo")

        result = propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="details[0].records[0].data.validators.validate[0]",
            validator=validator("body.data.token", None),
            case_dir="cases",
            response_body='{"data": {"access_token": "x", "refresh_token": "y"}}',
        )

        self.assertFalse(result.is_suggested())
        self.assertIn("不替人挑", result.reason)

    def test_candidates_prefer_exact_match_over_contains(self):
        self.assertEqual(
            find_candidate_paths({"a": {"token": 1}, "b": {"access_token": 2}}, "token"),
            ["a.token"],
        )


class TestCliFix(_TempWorkspace):
    """`haify fix` 的 CLI 契约。"""

    def test_cli_has_no_apply_flag(self):
        """★§5.8："`--apply` **永不存在**（人工自己贴）"。

        `--apply` 不是"还没做"，是**明确不做**：自愈改错会掩盖真实缺陷（R3）。
        """
        from interfacetester_ai.cli import main  # noqa: PLC0415

        with self.assertRaises(SystemExit):
            main(["fix", "summary.json", "--apply"])

    def test_cli_exit_code_is_zero_even_without_suggestions(self):
        """★**退出码恒 0**："给不出建议"不是错误（业务码拒绝是**正确输出**）。"""
        from interfacetester_ai.cli import EXIT_OK, main  # noqa: PLC0415

        self.write_case()
        summary = {
            "details": [
                {
                    "name": "demo",
                    "success": False,
                    "records": [
                        {
                            "name": "查询",
                            "data": {
                                "validators": {"validate": [validator("body.code", 1001)]},
                                "req_resps": [{"response": {"body": '{"code": 1001}'}}],
                            },
                        }
                    ],
                }
            ]
        }
        with open("summary.json", "w", encoding="utf-8") as fp:
            json.dump(summary, fp, ensure_ascii=False)

        self.assertEqual(main(["fix", "summary.json"]), EXIT_OK)

    def test_cli_missing_summary_is_a_config_error(self):
        from interfacetester_ai.cli import EXIT_CONFIG, main  # noqa: PLC0415

        self.assertEqual(main(["fix", "nope.json"]), EXIT_CONFIG)


class TestPackageFormAndSelftest(unittest.TestCase):
    """模块形态与自检。"""

    def test_fix_is_not_a_registered_writer(self):
        """★零写盘的**形态**：`fix` **不进** T18 注册表（它自己不写盘）。

        （临时补丁是 `workdir.JobWorkspace` 写的，那是**已登记**的写入者。）
        """
        from bench.write_boundary_scanner import MODULE_WRITE_BOUNDARIES  # noqa: PLC0415

        self.assertNotIn("fix", MODULE_WRITE_BOUNDARIES)

    def test_selftest_is_green(self):
        self.assertEqual(run_selftest(), 0)

    def test_selftest_turns_red_when_business_code_becomes_fixable(self):
        """元护栏：把"业务码不给改"放宽 → 自检必红（否则这条纪律是装饰）。"""
        import interfacetester_ai.fix as fix_module  # noqa: PLC0415

        with mock.patch.object(fix_module, "is_business_code_check", lambda *a, **k: False):
            self.assertNotEqual(fix_module.run_selftest(), 0)


CASE_HEADERS = """config:
  name: demo
teststeps:
  - name: 查询
    request:
      method: GET
      url: /api/user
    validate:
      - eq: ["headers.X-Apikey", "k"]
"""

CASE_VARS = """config:
  name: demo
teststeps:
  - name: 查询
    request:
      method: GET
      url: /api/user
    extract:
      accessToken: body.data.access_token
    validate:
      - eq: ["${token}", "abc"]
"""


class TestTriggerScopeBoundary(_TempWorkspace):
    """★§9.25 **触发面的边界**：该给的两族给全、越界的一律拒（成对 + 元护栏）。

    背景：把"能改的面"放宽，最容易发生的事就是**自愈改错、掩盖真缺陷**（R3）——
    失败会被人看见，"修好了"不会。所以这里每一条"能给"都配一条"不许给"。
    """

    # ---- ① 容器约束：候选只在同一容器里找 ----

    def test_container_root_is_recognized(self):
        self.assertEqual(container_root("headers.X-Api-Key"), "headers")
        self.assertEqual(container_root("body.data.token"), "body")
        self.assertEqual(container_root("status_code"), "", "协议项不是容器")
        self.assertEqual(container_root("text.token"), "", "`text` 不是字段容器")

    def test_headers_missing_is_suggested_within_headers(self):
        """正例：`headers.X-Apikey` 拼错 → 响应头里唯一相近名 `X-Api-Key` → 给补丁。"""
        self.write_case(CASE_HEADERS)

        result = propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="p",
            validator=validator("headers.X-Apikey", None),
            response_headers={"X-Api-Key": "k"},
        )

        self.assertTrue(result.is_suggested(), result.reason)
        self.assertEqual(result.new_check, "headers.X-Api-Key")
        self.assertIn("headers.X-Api-Key", result.diff)

    def test_headers_missing_is_never_rewritten_into_a_body_path(self):
        """★**回归判据**（改前的真缺陷）：响应体里恰有相近字段时，**绝不许**改成 `body.*`。

        改前无论 `check` 的根是什么，候选都只在响应体里找、并拼成 `body.*` ——
        `headers.X-Apikey` 会被"修"成 `body.xapikey` ✗（改的不是名字，是语义）。
        """
        self.write_case(CASE_HEADERS)

        result = propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="p",
            validator=validator("headers.X-Apikey", None),
            response_body='{"xapikey": "in-body"}',  # ← 响应体里**有**，但它在别的容器里
            response_headers={"X-Api-Key": "k"},
        )

        self.assertTrue(result.is_suggested())
        self.assertTrue(
            result.new_check.startswith("headers."), f"不该跨容器改成 {result.new_check!r}"
        )

    def test_headers_without_response_headers_is_refused(self):
        """拿不到"响应头"这个事实 → 拒（**不去 body 里猜**）。"""
        self.write_case(CASE_HEADERS)

        result = propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="p",
            validator=validator("headers.X-Apikey", None),
            response_body='{"xapikey": "in-body"}',
            response_headers=None,
        )

        self.assertFalse(result.is_suggested())
        self.assertIn("没留下 `headers` 的事实", result.reason)

    def test_unknown_root_is_refused(self):
        """根不是 `body`/`headers` → 拒：那不是字段名问题（本命令不越界改别的写法）。"""
        self.write_case(CASE.replace("body.data.token", "text.token"))

        result = propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="p",
            validator=validator("text.token", None),
            response_body='{"token": "x"}',
        )

        self.assertFalse(result.is_suggested())
        self.assertIn("根", result.reason)


    # ---- ② `${var}` 变量族（§9.25 新增）----

    def test_variable_name_helper(self):
        self.assertEqual(variable_name("${accessToken}"), "accessToken")
        self.assertEqual(variable_name("${ token }"), "token")
        self.assertEqual(variable_name("${fn(a)}"), "", "函数调用不是变量名")
        self.assertEqual(variable_name("body.data.token"), "")

    def test_variable_rename_is_suggested_when_unique_and_resolvable(self):
        """正例：`${token}` 取不到值，`extract` 里唯一相近变量 `accessToken` **真能取到值**。"""
        self.write_case(CASE_VARS)

        result = propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="p",
            validator=validator("${token}", None),
            response_body='{"data": {"access_token": "x"}}',
        )

        self.assertTrue(result.is_suggested(), result.reason)
        self.assertEqual(result.new_check, "${accessToken}")
        self.assertIn("${accessToken}", result.diff)
        self.assertNotIn("body.", result.new_check, "★绝不许把变量引用换成响应字段")

    def test_variable_is_never_rewritten_into_a_body_path(self):
        """★成对（改前真缺陷）：响应体里**有** `token` 字段时，也**不许**改成 `body.token`。

        做法：让 `extract` 里唯一候选的取值路径**在响应里取不到值**（硬闸②）——
        此时正确行为是**拒**，而不是退回"响应体里找同名"✗。
        """
        self.write_case(CASE_VARS)

        result = propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="p",
            validator=validator("${token}", None),
            response_body='{"data": {"token": "x"}}',  # 名字对得上，但 extract 的路径取不到
        )

        self.assertFalse(result.is_suggested())
        self.assertIn("也取不到值", result.reason)
        self.assertEqual(result.new_check, "")

    def test_variable_without_extract_is_refused(self):
        """本 step 没有 `extract`（或读不到）→ 判不了 → 拒。"""
        self.write_case(
            CASE_VARS.replace("    extract:\n      accessToken: body.data.access_token\n", "")
        )

        result = propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="p",
            validator=validator("${token}", None),
            response_body='{"token": "x"}',
        )

        self.assertFalse(result.is_suggested())
        self.assertIn("读不到 `extract`", result.reason)

    def test_business_code_variable_is_refused(self):
        """`${code}` 是**业务码语义** → 按业务码拒（**不能靠改变量名绕过**）。"""
        self.assertEqual(classify_failure(validator("${code}", None)), REFUSE_REAL_DEFECT)
        self.write_case(CASE_VARS)

        result = propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="p",
            validator=validator("${code}", None),
            response_body='{"data": {"access_token": "x"}}',
        )

        self.assertFalse(result.is_suggested())
        self.assertIn("疑似真实缺陷", result.reason)

    def test_function_call_variable_is_not_touched(self):
        """`${fn(1)}` 归 `other`：不替人改函数调用（那是另一个语义层级）。"""
        self.assertEqual(classify_failure(validator("${fn(1)}", None)), REFUSE_OTHER)

    def test_variable_with_value_but_mismatch_is_refused(self):
        """变量**取到了值**却对不上 → 值语义 → 不给改。"""
        self.assertEqual(classify_failure(validator("${token}", "abc")), REFUSE_OTHER)

    def test_extract_map_reader_reads_only_the_named_step(self):
        """`read_extract_map` 只认**名字对上**的那个 step；读不到 → `{}`（调用方据此拒）。"""
        self.assertEqual(read_extract_map(CASE_VARS, "查询"), {"accessToken": "body.data.access_token"})
        self.assertEqual(read_extract_map(CASE_VARS, "别的步骤"), {})
        self.assertEqual(read_extract_map("不是合法 YAML: [", "查询"), {})

    def test_candidate_names_prefers_exact_then_contains(self):
        self.assertEqual(find_candidate_names({"X-Api-Key": "k"}, "xapikey"), ["X-Api-Key"])
        self.assertEqual(find_candidate_names({"accessToken": "p"}, "token"), ["accessToken"])
        self.assertEqual(find_candidate_names({"a": "1"}, "token"), [])

    # ---- ③ 值语义仍然不给改（边界，防"顺手扩"）----

    def test_value_semantics_are_refused(self):
        """`equal: [body.age, "18"]`（实际 `18`，字符串数字）→ **不给改**。

        它看起来很像"用例写错了"，但**同样可能**是接口把类型改了（R3）：
        给这类失败出"改断言"的建议，等于教人把 bug 当特性。
        """
        self.assertEqual(classify_failure(validator("body.data.age", 18)), REFUSE_OTHER)

        self.write_case(CASE)
        result = propose_fix_for(
            case_name="demo",
            step_name="查询",
            pointer="p",
            validator=validator("body.data.age", 18),
            response_body='{"data": {"age": 18}}',
        )

        self.assertFalse(result.is_suggested())
        self.assertIn("不越界", result.reason)

    # ---- ④ 元护栏：闸必须**吃劲**（摘掉它 → 判据必红）----

    def test_meta_guardrail_container_gate_is_load_bearing(self):
        """把容器闸摘掉（`container_root` 恒为 `body`）→ 那次判定**必须跨容器**。

        没有这一条，"容器约束"可能只是注释里的愿望：判据一直绿，因为没人试过摘掉它。
        """
        self.write_case(CASE_HEADERS)

        def _run():
            return propose_fix_for(
                case_name="demo",
                step_name="查询",
                pointer="p",
                validator=validator("headers.X-Apikey", None),
                response_body='{"xapikey": "in-body"}',
                response_headers={"X-Api-Key": "k"},
            )

        with mock.patch.object(fix_mod, "container_root", lambda check: "body"):
            broken = _run()
        healthy = _run()

        self.assertTrue(broken.is_suggested() and healthy.is_suggested())
        self.assertTrue(broken.new_check.startswith("body."), "摘掉闸之后确实会跨容器 ✗")
        self.assertTrue(healthy.new_check.startswith("headers."), "闸在时应当留在 headers")

    def test_meta_guardrail_resolvable_gate_is_load_bearing(self):
        """把"取值路径必须能取到值"那道闸摘掉 → 取不到值的变量也会拿到补丁 ✗。"""
        self.write_case(CASE_VARS)

        def _run():
            return propose_fix_for(
                case_name="demo",
                step_name="查询",
                pointer="p",
                validator=validator("${token}", None),
                response_body='{"data": {"token": "x"}}',
            )

        with mock.patch.object(
            fix_mod, "find_candidate_paths", lambda *args, **kwargs: ["data.access_token"]
        ):
            broken = _run()
        healthy = _run()

        self.assertTrue(broken.is_suggested(), "摘掉闸之后就乱给了")
        self.assertFalse(healthy.is_suggested(), "闸在时应当拒绝")


if __name__ == "__main__":
    unittest.main()



