import argparse
import enum
import importlib
import os
import sys
import time
from typing import Dict, List, Text

import pytest
from loguru import logger

from interfacetester import __description__, __version__
from interfacetester import compat
from interfacetester.compat import ensure_cli_args
from interfacetester.make import (
    init_make_parser,
    main_make,
    normalize_module_segment,
)
from interfacetester.utils import ensure_output_encoding, ga4_client, init_logger

SUB_COMMANDS = ("run", "make", "convert")
VERSION_FLAGS = ("-V", "--version")
HELP_FLAGS = ("-h", "--help")


def wants_version(argv: List[Text]) -> bool:
    """`-V` / `--version` 是不是在**问框架版本**（而不是 pytest 的透传参数）。

    NOTICE（0918-8 / L25）：修复前只有「顶层 `-V`」与三个别名（`hrun -V` / `hmake -V` /
    `hconvert -V`）被特判，**子命令形式漏了**：`interfacetester run -V` 会掉进
    pytest 的透传参数里 → `main_run` 判「没有有效用例路径」→ 报错 exit 1。
    这里把判据统一成一条规则，三个别名与三个子命令一起覆盖（测试做成遍历矩阵的不变量）：

      - 顶层：`interfacetester -V` / `--version`；
      - 子命令 + **只**跟版本标志：`interfacetester run -V` / `make --version` / `convert -V`
        （别名会先被改写成这三种形态，所以别名也一并覆盖）。

    刻意**不**扫整条 argv：`hrun <用例> --version` 里的 `--version` 属于 pytest 的透传参数，
    拦下来会改变既有语义（那是「问 pytest 自己的版本」）。
    """
    rest = list(argv[1:])
    if not rest:
        return False
    if rest[0] in VERSION_FLAGS:
        return True
    if rest[0] in SUB_COMMANDS and len(rest) > 1:
        return all(item in VERSION_FLAGS for item in rest[1:])
    return False


def rewrite_alias_to_subcommand(subcommand: Text) -> None:
    """把**别名命令**的 argv 就地改写成对应的子命令形态（三个别名共用）。

    NOTICE（批次 9-1 / 漂移收口）：修复前 `-V` 的特判在**三处各写了一遍**
    （`main_hrun_alias` / `main_make_alias` / `main_convert_alias`），而且各自把
    `["-V", "--version"]` 这个字面量又抄了一份。这正是 L10 踩过的坑的成因：
    `hmake` 漏改了一次（`hmake -V` → `unrecognized arguments: -V`，退出码 2），
    而另外两个别名是对的 —— 三份拷贝必然漏一份。

    现在只有这一处：

      - 「只有一个版本标志」→ 交给**顶层**的 `-V`（`interfacetester -V`），
        与 `wants_version()` 的判据同一份常量；
      - 其余情况 → 插入子命令名，由 `main()` 统一分发。

    刻意**不**在这里处理 `-h/--help`：`run` 的帮助是 **pytest 的帮助**（`pytest.main(["-h"])`），
    与 `make`/`convert` 的 argparse 帮助不是一回事，两者无法共用一条分支 ——
    因此 `main_hrun_alias` 自己先特判帮助标志（与修复前逐字一致）。
    """
    if len(sys.argv) == 2 and sys.argv[1] in VERSION_FLAGS:
        sys.argv = ["interfacetester", "-V"]
        return

    sys.argv.insert(1, subcommand)


def init_parser_run(subparsers):
    sub_parser_run = subparsers.add_parser(
        "run", help="Make InterfaceTester testcases and run with pytest."
    )
    return sub_parser_run


def init_parser_convert(subparsers):
    """`hconvert`：把 curl / HAR 等外部资产转成标准 YAML 用例（P3-a）。"""
    from interfacetester import converters  # noqa: PLC0415 - 只在用到时导入

    sub_parser_convert = subparsers.add_parser(
        "convert",
        help="Convert external assets (curl / HAR) into InterfaceTester YAML testcases.",
    )
    sub_parser_convert.add_argument(
        "--from",
        dest="from_source",
        required=True,
        choices=list(converters.SUPPORTED_SOURCES),
        help="source format: " + "; ".join(f"{k}={v}" for k, v in converters.SUPPORTED_SOURCES.items()),
    )
    sub_parser_convert.add_argument(
        "--in", dest="in_path", required=True, help="input file (curl txt / HAR json)"
    )
    sub_parser_convert.add_argument(
        "--out",
        dest="out_dir",
        default="",
        help="output directory (default: ./converted)",
    )
    sub_parser_convert.add_argument("--name", dest="case_name", default="", help="testcase name")
    sub_parser_convert.add_argument(
        "--assertions",
        dest="assertions",
        default="status+schema",
        choices=["status+schema", "status", "none"],
        help="HAR/Postman: which assertions to generate (curl always keeps none)",
    )
    sub_parser_convert.add_argument(
        "--single-file",
        dest="split_folders",
        action="store_false",
        help="Postman: merge the whole collection into one testcase (default: one per top-level folder); "
        "OpenAPI: merge all tags into one testcase (default: one per tag)",
    )
    sub_parser_convert.add_argument(
        "--include-tags",
        dest="include_tags",
        default="",
        help="OpenAPI: only import operations with these tags (comma separated)",
    )
    sub_parser_convert.add_argument(
        "--exclude-tags",
        dest="exclude_tags",
        default="",
        help="OpenAPI: skip operations with these tags (comma separated)",
    )
    sub_parser_convert.add_argument(
        "--methods",
        dest="methods",
        default="",
        help="OpenAPI: only import these HTTP methods (comma separated, e.g. get,post)",
    )
    sub_parser_convert.add_argument(
        "--include-hosts",
        dest="include_hosts",
        default="",
        help="HAR: only import requests whose host contains one of these (comma separated)",
    )
    sub_parser_convert.add_argument(
        "--exclude-hosts",
        dest="exclude_hosts",
        default="",
        help="HAR: skip requests whose host contains one of these (comma separated)",
    )
    sub_parser_convert.add_argument(
        "--no-dedupe", dest="dedupe", action="store_false", help="HAR: keep duplicate recordings"
    )
    sub_parser_convert.add_argument(
        "--no-validate",
        dest="validate",
        action="store_false",
        help="skip the export-time validation (load_testcase + hmake dry-run)",
    )
    sub_parser_convert.add_argument(
        "--report", dest="report_path", default="", help="write an import report (markdown) here"
    )
    return sub_parser_convert


def _split_hosts(raw: str):
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def main_convert(args) -> int:
    """执行 `hconvert`：转换 → 写盘 → 导出即验证 → 打印摘要与告警。"""
    from interfacetester import converters  # noqa: PLC0415
    from interfacetester.converters import (  # noqa: PLC0415
        curl_adapter,
        emit_yaml,
        har_adapter,
        openapi_adapter,
        postman_adapter,
    )

    if args.from_source not in converters.IMPLEMENTED_SOURCES:
        logger.error(
            f"`--from {args.from_source}` 还没有实现（当前可用：{list(converters.IMPLEMENTED_SOURCES)}）。"
            "路线图见 docs/convert/README.md。"
        )
        return 2

    in_path = args.in_path
    if not os.path.isfile(in_path):
        logger.error(f"输入文件不存在：{in_path}")
        return 2

    out_dir = args.out_dir or os.path.join(os.getcwd(), "converted")
    case_name = args.case_name or os.path.splitext(os.path.basename(in_path))[0]
    required_functions = []

    try:
        if args.from_source == "curl":
            # NOTICE（0918-8 / M23）：用 `utf-8-sig` 读——PowerShell 5.1 的
            # `Set-Content -Encoding UTF8`、以及部分编辑器的"UTF-8"都会写 BOM，
            # 而 BOM 会让第一行变成 `\ufeffcurl …`，于是**一条都解析不出来**（且原来的报错完全看不出原因）。
            with open(in_path, mode="r", encoding="utf-8-sig") as fp:
                content = fp.read()
            ir_cases = [curl_adapter.convert_curl_file(content, case_name=case_name, source=in_path)]
        elif args.from_source == "har":
            ir_cases = [
                har_adapter.convert_har_file(
                    in_path,
                    case_name=case_name,
                    assertion_mode=args.assertions,
                    include_hosts=_split_hosts(args.include_hosts),
                    exclude_hosts=_split_hosts(args.exclude_hosts),
                    dedupe=args.dedupe,
                )
            ]
        elif args.from_source == "postman":
            ir_cases = postman_adapter.convert_postman_file(
                in_path,
                case_name=case_name,
                split_folders=args.split_folders,
                assertion_mode=args.assertions,
            )
            required_functions = postman_adapter.required_helper_functions(ir_cases)
        else:
            ir_cases = openapi_adapter.convert_openapi_file(
                in_path,
                case_name=case_name,
                split_tags=args.split_folders,
                assertion_mode=args.assertions,
                include_tags=_split_hosts(args.include_tags),
                exclude_tags=_split_hosts(args.exclude_tags),
                include_methods=_split_hosts(args.methods),
            )
            required_functions = []
    except Exception as ex:  # noqa: BLE001 - 解析失败要给出可读原因
        logger.error(f"解析 {in_path} 失败：{type(ex).__name__}: {ex}")
        return 2

    # NOTICE（0918-8 / M23）：**先留一份原始用例**再过滤空用例。
    # 修复前是「先过滤、再 `for case in ir_cases or []` 打告警」——那个循环**永远不执行**
    # （ir_cases 刚刚被滤空），于是转换器辛苦攒下的诊断（"第 1 行的内容不是 curl 命令，
    # 已跳过：\ufeffcurl …"、"过滤后没有任何可导入的请求：请检查 --include-hosts"）
    # 全被丢掉，用户只剩一句泛泛的「请检查输入内容」。
    all_cases = list(ir_cases)
    ir_cases = [case for case in all_cases if case.steps]
    if not ir_cases:
        for case in all_cases:
            for warning in case.all_warnings():
                logger.warning(warning)

        logger.error(
            "没有解析出任何请求，未生成文件。请检查输入内容或过滤参数（--include-hosts）。"
        )
        return 1

    written_cases = []
    total_steps = total_assertions = 0

    # NOTICE（批次 E / **L6**）：输出目录必须在写盘前**先校验**。
    # 修复前 `--out` 指到一个**已存在的文件**时，`emit_yaml.emit_case` 里的
    # `os.makedirs(out_dir, exist_ok=True)` 会抛 `FileExistsError` —— 而这个异常
    # 逃出了包住转换段的那个 `try`（它只包住"解析"），用户看到的是一整屏 traceback。
    # 这是**用法错误**（与 `--from`/`--out` 一类参数同级），所以按 usage error 返回 2。
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        logger.error(
            f"`--out` 指向的路径已存在且**不是目录**：{out_dir}\n"
            "  请改成目录（框架会 `makedirs` 建出来），或换一个路径。"
        )
        return 2
    out_dir_abs = os.path.abspath(out_dir)
    # 0918-8 / M21：**生成物路径冲突**必须在写盘前拦下来。
    # `_slug` 把 `[\\/:*?"<>|\s]+` 统一换成 `_`，所以**不同的用例名会落到同一个文件名**——
    # 实测 Postman 两个顶层 folder 叫 `user admin` 与 `user/admin` 时都生成 `col_user_admin.yml`：
    # 后者**整体覆盖**前者（前一个用例的步骤凭空消失），而日志里两行都打印同一个路径、
    # 末尾还写「共 2 个用例文件」，与磁盘实际不符，退出码 0。
    # 同源问题：schema 文件 `schemas/{stem}_{NN}_{step}.json` 由同一个 slug 派生，
    # 覆盖后前一个用例会引用到**别人的** schema（断言错对象）。
    # NOTICE（0919-1 / L30）：这个注解曾在很长一段时间里**缺 `Dict` 导入**
    # （第 5 行只 import 了 `List, Text`）。局部变量注解运行期不求值，所以它不崩、
    # 测试也测不出来，只有静态检查会报 F821——它暴露的真正短板是 CI 里没有任何名字解析检查。
    # 现在 `tests/static_name_resolution_test.py` 用标准库 `symtable` 把这一类钉成了闸门。
    used_yaml_targets: Dict[str, str] = {}
    # NOTICE（批次 5 / H5）：**schema 文件也要纳入同一个护栏**。
    # 上面那段注解一直写着"同源问题：schema 文件 `schemas/{stem}_{NN}_{step}.json` 由同一个
    # slug 派生，覆盖后前一个用例会引用到**别人的** schema（断言错对象）"，但护栏只登记了
    # `.yml` 目标 —— 于是"两个用例的 schema 同名"这条路径**没有任何提示**。
    # 0919-22 已把 `_case_stem` 改成保留非 ASCII（修掉中文 folder 的主因），
    # 但两个**不同**用例名仍可能归一到同一个词干（`用户 管理` 与 `用户-管理`），
    # 而它们的 `.yml` 文件名是不同的 —— 所以这层护栏仍然必要。
    used_extra_targets: Dict[str, str] = {}
    # NOTICE（批次 9-1 / 漂移收口）：**第三层护栏——生成物模块名**。
    # 前两层（`.yml` 名、schema 名）各自用的是「写盘时的那个名字规则」，
    # 但改用例的真正落点是 `hmake` 生成的 `*_test.py`，而它的名字由**另一条规则**
    # （`make.normalize_module_segment`：`-`/`.`/空格 一律折成 `_`）派生 ——
    # 于是 `用户 管理.yml` 与 `用户-管理.yml` 在上一层**不冲突**（`-` 不在 slugify 的
    # 归一集合里），到了 `make_testcase` 才撞成同一个 `用户_管理_test.py`。
    # 那条撞名检查会**响亮报错**（不是静默覆盖），但报错发生在导入流程更晚的位置，
    # 用户拿到的是「生成物撞名」而不是「你导入的两个用例会落到同一个文件」。
    # 现在用**同一个规则**在这里提前判一次，两层护栏口径一致。
    used_module_targets: Dict[str, str] = {}
    for ir_case in ir_cases:
        steps, assertions, _ = emit_yaml.summarize(ir_case)
        total_steps += steps
        total_assertions += assertions

        yaml_name = f"{emit_yaml.slugify(ir_case.name)}.yml"
        target = os.path.normcase(os.path.join(out_dir, yaml_name))
        collided_with = used_yaml_targets.get(target)
        if collided_with is not None:
            logger.error(
                f"两个用例会生成到**同一个文件**：`{yaml_name}`——第二个会整体覆盖第一个"
                f"（前一个用例的步骤与 schema 都会丢失）。\n"
                f"  已占用它的用例: {collided_with}\n"
                f"  本次用例: {ir_case.name}\n"
                f"原因：文件名会把 `/ \\ : * ? \" < > |` 与空白统一归一成 `_`，"
                f"所以这两个用例名落到了同一个文件。\n"
                f"修法（任选其一）：\n"
                f"  1) 改素材里的名字（Postman 是 folder 名、OpenAPI 是 tag 名）让它们不同；\n"
                f"  2) 用 `--single-file` 把整个集合合并成一个用例（不会再有文件名冲突）；\n"
                f"  3) 分两次导入，用不同的 `--out` 目录。"
            )
            return 1

        used_yaml_targets[target] = ir_case.name

        # schema 等附加文件：同名即"后者覆盖前者 + 前者断言断到别人的形状"，必须拦
        for extra_rel in ir_case.extra_files or {}:
            extra_target = os.path.normcase(os.path.join(out_dir, *extra_rel.split("/")))
            occupied = used_extra_targets.get(extra_target)
            if occupied is not None:
                logger.error(
                    f"两个用例会生成到**同一个 schema 文件**：`{extra_rel}`"
                    f"——后写的会覆盖先写的，而两个用例的断言都指向它"
                    f"（先写的那个用例会**断到别人的响应形状**）。\n"
                    f"  已占用它的用例: {occupied}\n"
                    f"  本次用例: {ir_case.name}\n"
                    f"原因：schema 文件名里的用例词干会把 `/ \\ : * ? \" < > |`、空白等"
                    f"归一成 `_`，所以这两个用例名落到了同一个词干。\n"
                    f"修法（任选其一）：\n"
                    f"  1) 改素材里的名字（Postman 是 folder 名、OpenAPI 是 tag 名）让它们不同；\n"
                    f"  2) 用 `--single-file` 把整个集合合并成一个用例（不会再有词干冲突）；\n"
                    f"  3) 分两次导入，用不同的 `--out` 目录。"
                )
                return 1
            used_extra_targets[extra_target] = ir_case.name

        # 第三层：`hmake` 生成的 `*_test.py` 模块名（规则见 make.normalize_module_segment）。
        # NOTICE（批次 9-1 / 漂移收口）：**放在最后**是刻意的 —— 三层护栏的粒度是
        # 从具体到宽泛（用例文件 → schema 文件 → 生成物模块名），同一份素材同时撞多层时
        # 应当报**更具体**的那一层。实测：Postman folder `用户.管理` 与 `用户 管理`
        # 同时撞 schema 名与模块名，前两层已经能给出"断到别人的 schema"这个更严重的结论，
        # 由它先报（既有用例 `test_schema_file_collision_is_refused_before_writing`
        # 正是钉这一点）。本层补的是**另外那些**前两层拦不住的输入：
        # `用户-管理` 与 `用户 管理` —— `.yml` 名不同（`-` 不被 slugify 归一）、
        # schema 词干也不同（`safe_file_stem` 保留 `-`），却在 `hmake` 里撞成同一个模块。
        module_target = os.path.normcase(
            os.path.join(out_dir, normalize_module_segment(yaml_name[: -len(".yml")]))
        )
        module_occupied = used_module_targets.get(module_target)
        if module_occupied is not None:
            logger.error(
                f"两个用例会生成到**同一个 `*_test.py`**：`{yaml_name}` 与 "
                f"`{module_occupied}` 归一后是同一个模块名"
                f"（`-`/`.`/空格都会被折成 `_`，见 make.normalize_module_segment）。\n"
                f"  已占用它的用例: {module_occupied}\n"
                f"  本次用例: {ir_case.name}\n"
                f"后果：`hmake` 阶段会报「生成物撞名」并失败，两个用例只有一个能跑。\n"
                f"修法（任选其一）：\n"
                f"  1) 改素材里的名字让它们不同（Postman 是 folder 名、OpenAPI 是 tag 名）；\n"
                f"  2) 用 `--single-file` 把整个集合合并成一个用例；\n"
                f"  3) 分两次导入，用不同的 `--out` 目录。"
            )
            return 1
        used_module_targets[module_target] = ir_case.name

        # NOTICE（批次 E / **L7**）：三层护栏只覆盖**单次运行内**的撞名。
        # 跨两次运行落到同一个 `.yml`（同名的另一份素材、或换了 `--from`）时，
        # 修复前是**静默整体覆盖**：上一次的用例与 schema 都不见了，退出码 0、零告警。
        # 这里在写盘前比一次内容：**内容相同**（同源重跑，最常见）不打扰；
        # 内容不同则点名告警（含上一次文件的修改时间），让人知道东西被换掉了。
        if os.path.isfile(target):
            try:
                with open(target, encoding="utf-8") as fp:
                    previous_content = fp.read()
            except OSError:
                previous_content = None
            incoming_content = emit_yaml.dump_case_yaml(ir_case, yaml_name)
            if previous_content is not None and previous_content != incoming_content:
                logger.warning(
                    f"**覆盖了上一次运行留下的文件**：`{target}`（内容不同）。\n"
                    f"  上一次的用例名/步骤已经不存在于这个文件里了"
                    f"（上次修改时间：{_mtime_text(target)}）。\n"
                    f"  如果这不是你想要的：请换 `--out` 目录，或用 `--single-file` 合并导入，"
                    f"或先备份该文件。"
                )

        # NOTICE（批次 E / **L6**）：写盘异常必须收口成**可读的报错**，不能让 traceback
        # 冒出去 —— `try` 原来只包住了"解析"那一段。实测三种触发方式都会裸崩：
        # `--out` 是已存在的文件（上面的前置校验已拦）、**用例名超长**（Errno 63）、
        # 目录不可写/被占用。
        try:
            written = emit_yaml.emit_case(
                ir_case,
                out_dir,
                yaml_name=yaml_name,
                required_functions=required_functions,
            )
        except OSError as ex:
            logger.error(
                f"写盘失败：{target}\n"
                f"  根因: {type(ex).__name__}: {ex}\n"
                f"  本次已成功生成 {len(written_cases)} 个用例文件（它们仍然可用）。\n"
                f"  提示：常见原因是「目录不可写」「文件名过长（用例名来自素材，"
                f"可用 --case-name 缩短）」「同名文件被其它程序占用」。"
            )
            return 1
        written_cases.append((ir_case, written))
        logger.info(
            f"已生成用例：{written['yaml']}（{steps} 个步骤、{assertions} 条断言、"
            f"{len(written['extra'])} 个 schema 文件）"
        )
        for warning in written.get("warnings") or []:
            logger.warning(warning)

    # NOTICE（批次 E / **L7**）：上一次运行留下的 schema 文件会成为**孤儿**。
    # 本次没写、但目录里还在的 `schemas/*.json` —— 它们已经没有任何用例引用，
    # 而修复前完全没人提（用户不知道哪些文件可以删）。只**报告**，不擅自删除。
    orphaned = _orphaned_schema_files(out_dir, written_cases)
    if orphaned:
        logger.warning(
            f"输出目录里还有 {len(orphaned)} 个**上一次运行留下的 schema 文件**"
            f"（本次没有任何用例引用它们，未删除）：{orphaned[:5]}"
            + ("（更多略）" if len(orphaned) > 5 else "")
        )

    if args.validate:
        for ir_case, written in written_cases:
            try:
                emit_yaml.validate_emitted_case(written["yaml"])
            except Exception as ex:  # noqa: BLE001 - 导出即验证失败必须显式失败
                logger.error(
                    f"导出即验证失败（生成物无法被 load_testcase/hmake 处理）："
                    f"{written['yaml']}\n{type(ex).__name__}: {ex}"
                )
                return 1
        logger.info("导出即验证通过：load_testcase + hmake 干跑 + 语法检查")
    else:
        logger.warning("已跳过导出即验证（--no-validate）：生成物未经 load_testcase/hmake 检查")

    logger.info(
        f"共 {len(written_cases)} 个用例文件、{total_steps} 个步骤、{total_assertions} 条断言"
    )
    for ir_case, _written in written_cases:
        for warning in ir_case.all_warnings():
            logger.warning(f"[{ir_case.name}] {warning}")

    if args.report_path:
        # NOTICE（批次 E / **L6**）：`--report` 指到**目录**时，修复前 `open(..., "w")` 抛
        # 裸 `IsADirectoryError`（整屏 traceback），而这时用例其实**已经全部生成成功**了。
        try:
            with open(args.report_path, "w", encoding="utf-8") as fp:
                fp.write(_build_report(written_cases, in_path, args))
        except OSError as ex:
            logger.error(
                f"写导入报告失败：{args.report_path}\n"
                f"  根因: {type(ex).__name__}: {ex}\n"
                f"  用例文件**已经全部生成成功**（{len(written_cases)} 个），"
                f"只是报告没写出来；`--report` 需要是一个**文件**路径。"
            )
            return 1
        logger.info(f"导入报告：{args.report_path}")

    return 0


def _mtime_text(path: str) -> str:
    """文件的「上次修改时间」文案（取不到就返回 `未知`，永不因此失败）。"""
    try:
        return time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))
        )
    except OSError:
        return "未知"


def _orphaned_schema_files(out_dir: str, written_cases) -> List[str]:
    """本次没写、但输出目录里还留着的 `schemas/**` 文件（相对路径，排序后返回）。

    NOTICE（批次 E / **L7**）：跨次运行时，上一次的 schema 文件不会被清理 ——
    它们已经不被任何用例引用，而用户完全看不出来。这里只**报告**（不删除：
    框架不该擅自删用户目录里的东西）。
    """
    written_paths = {
        os.path.normcase(os.path.abspath(path))
        for _case, written in written_cases
        for path in (written.get("extra") or {}).values()
    }

    schemas_dir = os.path.join(out_dir, "schemas")
    if not os.path.isdir(schemas_dir):
        return []

    orphans: List[str] = []
    for root, _dirs, files in os.walk(schemas_dir):
        for name in files:
            absolute = os.path.join(root, name)
            if os.path.normcase(os.path.abspath(absolute)) in written_paths:
                continue
            orphans.append(os.path.relpath(absolute, out_dir).replace(os.sep, "/"))
    return sorted(orphans)


def _build_report(written_cases, in_path: str, args) -> str:
    """生成 markdown 导入报告（给人看：做了什么、丢了什么、要人工补什么）。"""
    from interfacetester.converters import emit_yaml  # noqa: PLC0415

    total_steps = sum(emit_yaml.summarize(case)[0] for case, _ in written_cases)
    total_assertions = sum(emit_yaml.summarize(case)[1] for case, _ in written_cases)
    total_warnings = sum(len(case.all_warnings()) for case, _ in written_cases)

    lines = [
        "# 导入报告",
        "",
        f"- 输入：`{in_path}`（源格式 `{args.from_source}`）",
        f"- 输出：{len(written_cases)} 个用例文件、{total_steps} 个步骤、{total_assertions} 条断言",
        f"- 模式：assertions={args.assertions}"
        + ("（curl 不生成断言）" if args.from_source == "curl" else "")
        + ("" if args.split_folders else "，--single-file（整个集合合成一个用例）"),
        "",
        "## 生成的文件",
        "",
    ]
    for case, written in written_cases:
        steps, assertions, _ = emit_yaml.summarize(case)
        lines.append(f"- `{written['yaml']}` — {steps} 步 / {assertions} 断言（用例名：{case.name}）")
        for relative_path in written["extra"]:
            lines.append(f"  - `{relative_path}`（schema 走文件引用，符合 P2-b 约定）")
    if written_cases and written_cases[0][1].get("debugtalk"):
        lines.append(f"- `{written_cases[0][1]['debugtalk']}`（项目标记，自动创建）")
    lines.append("")

    lines.append(f"## 需要人工确认的点（共 {total_warnings} 条）")
    lines.append("")
    any_warning = False
    for case, _written in written_cases:
        warnings = case.all_warnings()
        if not warnings:
            continue
        any_warning = True
        lines.append(f"### {case.name}")
        lines.append("")
        lines.extend(
            f"{index}. {warning}" for index, warning in enumerate(warnings, start=1)
        )
        lines.append("")
    if not any_warning:
        lines.append("无（没有触发任何降级/占位/过滤规则）")
        lines.append("")

    lines.append("## 下一步")
    lines.append("")
    lines.append("1. 打开生成的 YAML 过一遍：动态值改 `extract`/`${func()}`，敏感值用环境变量注入；")
    lines.append("2. 跑一次：`hrun <用例文件>`；")
    lines.append("3. 断言偏弱（只断状态码/结构）时，按业务补关键字段断言。")
    return "\n".join(lines) + "\n"


def main_run(extra_args) -> enum.IntEnum:
    ga4_client.send_event("run")
    # keep compatibility with v2
    extra_args = ensure_cli_args(extra_args)

    # NOTICE（批次 7 / **M2**）：判据**不能**是"这个 token 恰好是已存在的路径"。
    #
    # `run good.yml -c pytest.ini` / `--rootdir .` / `--junitxml reports/x.xml` 里
    # **选项的值**也是（或可以是）存在的路径，修复前会被当成用例路径摘走：
    #   - 轻则参数失值 → pytest `error: argument -c/--config-file: expected one argument`
    #     → **exit 4、一个用例都不跑**；
    #   - 重则把那个目录下所有 YAML 都生成并执行（审计员实测多生成了 `sub\b_test.py`）。
    # 现在按"**上一个 token 是不是一个取值的 pytest 选项**"判定。
    # `--opt=value` 形式天然安全（单 token、以 `-` 开头，会直接进 `extra_args_new`）。
    #
    # NOTICE（批次 B / **M3**）：判据与选项清单已抽到 `compat.filter_test_paths` /
    # `compat.PYTEST_OPTIONS_WITH_VALUE`，与 `_generate_conftest_for_summary` **共用一份**
    # —— 修复前两处各有一份，conftest 生成器那份还是"第一个存在的路径"，于是
    # `hrun -c pytest.ini case.yml --save-tests` 的 summary 会落到 `logs/pytest.summary.json`。
    tests_path_list, extra_args_new = compat.filter_test_paths(extra_args)

    if len(tests_path_list) == 0:
        # has not specified any testcase path
        logger.error(f"No valid testcase path in cli arguments: {extra_args}")
        sys.exit(1)

    testcase_path_list = main_make(tests_path_list)
    if not testcase_path_list:
        logger.error("No valid testcases found, exit 1.")
        sys.exit(1)

    if "--tb=short" not in extra_args_new:
        # NOTICE（0918-8 / L26）：framework flag 必须插在**第一个 `--` 之前**。
        # `--` 在 pytest 里的语义是「后面全是文件参数」，修复前直接 append 会变成
        # `pytest -- --tb=short <生成物>` → pytest 把 `--tb=short` 当成文件名，
        # 报 `file or directory not found: --tb=short` 并以 exit 4 退出，**一个用例都不跑**。
        insert_at = (
            extra_args_new.index("--") if "--" in extra_args_new else len(extra_args_new)
        )
        extra_args_new.insert(insert_at, "--tb=short")

    extra_args_new.extend(testcase_path_list)

    # NOTICE（批次 9-1 收尾 / 间歇性假失败的根因）：**生成完必须作废 import 缓存**。
    #
    # 现象：`hrun <用例> --save-tests` 会**间歇性**地整轮白跑 —— pytest 报
    #   ImportError while loading conftest '<项目>/conftest.py'
    #   ModuleNotFoundError: No module named '<项目目录名>.conftest'
    # 然后 exit 4、`summary.json` 不生成（实测复现率：单次 CLI 调用 3%~8%）。
    #
    # 根因（`.tmp_report/probe_conftest_single.py` 抓到的现场）：
    #   ① `--save-tests` 的 `compat._generate_conftest_for_summary` **先**
    #      `load_project_meta()`（compat.py:558）——它把项目根挂上 `sys.path` 并 import
    #      `debugtalk`，于是 CPython 为「项目目录」建了一个 `FileFinder` 并**缓存了当时的目录列表**
    #      （那会儿还只有 `case.yml`/`debugtalk.py`）；
    #   ② 之后才写 `conftest.py`，`main_make` 再写 `case_test.py` 与 `__init__.py`；
    #   ③ `FileFinder.find_spec` **只在目录 mtime 变化时**才重新列目录
    #      （`if mtime != self._path_mtime: self._fill_cache()`），而那一刻
    #      `_path_mtime` 与 `st_mtime` 相等 → 沿用 ① 的旧列表 → `conftest` 在缓存里「不存在」；
    #   ④ 因为 `__init__.py` 存在，conftest 必须按**包限定名** `<目录名>.conftest` 导入，
    #      走的正是这个带缓存的查找路径 → 报「子模块不存在」。
    #   铁证：失败时 `finder._path_cache == ['__pycache__','case.yml','debugtalk.py']`
    #   而 `os.listdir` 是 6 项；紧接着 `importlib.invalidate_caches()` 后**同一个 import 立刻成功**。
    #
    # 为什么修在这里：`main_make()` 已经跑完（所有生成物都落盘了），而 pytest 是这些生成物
    # **唯一**的消费者 —— 在此处作废一次，pytest 后续对 `conftest.py`/`*_test.py`/`__init__.py`
    # 的查找都会重新列目录。放在更早的地方没用（后面还会写文件），放在 `make` 里则要按文件调用多次。
    #
    # NOTICE: 直接调 `make_testcase()` 之后**自己**调 `pytest.main()` 的库使用者不受本行保护
    # （那属于另一条入口）；本行只覆盖 CLI/`hrun` 这条链路。
    importlib.invalidate_caches()

    logger.info(f"start to run tests with pytest. InterfaceTester version: {__version__}")
    return pytest.main(extra_args_new)


def main():
    """API test: parse command line options and run commands."""
    # NOTICE（0918-8 / L27）：编码要**在最前面**定下来。
    # 修复前只有 `init_logger()` 里做，而这条路径上有相当一部分输出在它之前就写完了：
    # 「未知子命令」提示（M13 新增的中文 hint）、`-h`、argparse 自身的用法错误……
    # 它们都直接写 stderr，于是同一次调用里 stdout=UTF-8、stderr=cp936
    # （实测 `interfacetester debug` 的 stderr 在管道里解不出 UTF-8）。
    # `init_logger()` 里那次调用保留：直接调它的库使用者同样受保护（幂等）。
    ensure_output_encoding()

    parser = argparse.ArgumentParser(description=__description__)
    # 标志名来自常量（与 wants_version / 三个别名同源），不再各抄一份字面量
    parser.add_argument(
        *VERSION_FLAGS, dest="version", action="store_true", help="show version"
    )

    subparsers = parser.add_subparsers(help="sub-command help")
    init_parser_run(subparsers)
    sub_parser_make = init_make_parser(subparsers)
    sub_parser_convert = init_parser_convert(subparsers)

    # 先归一化「裸用例路径」这一种写法：`interfacetester <路径> [更多 pytest 参数]`
    # 等价于 `hrun <路径> ...`（`main_hrun_alias` 就是这么做的）。
    #
    # NOTICE（0918-5 / M13）：修复前完全没有这一支——
    #   - `interfacetester <路径>`（只有 2 个 argv）落到那条没有 else 的 elif 链，
    #     然后**静默 exit 0**：什么都不做，用户以为跑成功了；
    #   - 带上额外参数时又变成 argparse 的 `invalid choice: '<路径>'`（exit 2）。
    # 统一在最前面转换一次，两种形式（以及任意 arity）行为一致。
    # 子命令与 `-h/-V` 优先：即使当前目录恰好有同名文件也不会被当成路径。
    if (
        len(sys.argv) >= 2
        and sys.argv[1] not in (*SUB_COMMANDS, *VERSION_FLAGS, *HELP_FLAGS)
        and os.path.exists(sys.argv[1])
    ):
        sys.argv.insert(1, "run")

    # NOTICE（0918-8 / L25）：版本查询统一在**子命令分发之前**处理——
    # 判据见 `wants_version()`（顶层 / 子命令 + 只跟版本标志；别名已先被改写成子命令形态）。
    # 放在这里是刻意的：一旦进了 `main_run`，`-V` 就变成 pytest 的透传参数，
    # 只会得到「没有有效用例路径」这种与版本毫无关系的报错。
    if wants_version(sys.argv):
        print(f"{__version__}")
        sys.exit(0)

    if len(sys.argv) == 1:
        # interfacetester
        parser.print_help()
        sys.exit(0)
    elif len(sys.argv) == 2:
        # print help for sub-commands
        argument = sys.argv[1]
        if argument in VERSION_FLAGS:
            # interfacetester -V
            print(f"{__version__}")
        elif argument in HELP_FLAGS:
            # interfacetester -h
            parser.print_help()
        elif argument == "run":
            # interfacetester run
            pytest.main(["-h"])
        elif argument == "make":
            # interfacetester make
            sub_parser_make.print_help()
        elif argument == "convert":
            # interfacetester convert / hconvert
            sub_parser_convert.print_help()
        else:
            # 未知子命令：必须报错并非零退出（用 2 与 argparse 的用法错误保持一致）。
            # NOTICE（0918-5 / M13）：修复前这里是一条**没有 else 的 elif 链**，
            # 未匹配时直接落到结尾的 `sys.exit(0)`——`interfacetester debug` 零输出、
            # 退出码 0，用户会以为跑成功了。
            print(
                f"error: unknown command: {argument}\n"
                f"hint: 可用命令为 run / make / convert（也可直接给用例路径，"
                f"等价于 hrun <路径>）",
                file=sys.stderr,
            )
            parser.print_help()
            sys.exit(2)

        sys.exit(0)
    elif (
        len(sys.argv) == 3 and sys.argv[1] == "run" and sys.argv[2] in HELP_FLAGS
    ):
        # interfacetester run -h
        pytest.main(["-h"])
        sys.exit(0)

    extra_args = []
    if len(sys.argv) >= 2 and sys.argv[1] == "run":
        args, extra_args = parser.parse_known_args()
    else:
        args = parser.parse_args()

    if args.version:
        print(f"{__version__}")
        sys.exit(0)

    # set log level
    # NOTICE（0918-8 / L25）：必须同时认 `--log-level DEBUG`（分写）与 `--log-level=DEBUG`
    # （等号）。修复前只按**精确 token** 找 `--log-level`，等号形式完全命中不了 →
    # 静默回落到 INFO，用户以为自己开了 DEBUG（`--log-level=DEBUG` 是 argparse/GNU 的常见写法，
    # 而且 `hrun --log-level=DEBUG <路径>` 里它属于**透传参数**，不会走 argparse 的解析）。
    level = "INFO"
    for index, item in enumerate(extra_args):
        if item == "--log-level":
            if index + 1 < len(extra_args):
                level = extra_args[index + 1]
            break
        if item.startswith("--log-level="):
            level = item.split("=", 1)[1]
            break

    init_logger(level)

    if sys.argv[1] == "run":
        sys.exit(main_run(extra_args))
    elif sys.argv[1] == "make":
        main_make(args.testcase_path)
    elif sys.argv[1] == "convert":
        sys.exit(main_convert(args))


def main_hrun_alias():
    """command alias
    hrun = interfacetester run
    """
    # `run` 的 `-h` 是 **pytest** 的帮助（不是 argparse 的），必须在这里先特判；
    # 其余（含 `-V`）统一交给 `rewrite_alias_to_subcommand`。
    if len(sys.argv) == 2 and sys.argv[1] in HELP_FLAGS:
        pytest.main(["-h"])
        sys.exit(0)

    rewrite_alias_to_subcommand("run")
    main()


def main_make_alias():
    """command alias
    hmake = interfacetester make
    """
    # NOTICE（0918-5 / L10）：修复前这里**无条件**插入 "make"，于是 `hmake -V` 变成
    # `interfacetester make -V` → argparse 报 `unrecognized arguments: -V` 且退出码 2；
    # 而 `hrun -V`、`hconvert -V` 都特判了 `-V`。三个别名口径不一致，唯独 hmake 漏改。
    # 现在三个别名共用同一个改写函数（`-V` 的判据与 `wants_version()` 同源），
    # 协作者再想漏也漏不掉；回归见 `tests/cli_test.py::TestVersionFlagMatrix`。
    rewrite_alias_to_subcommand("make")
    main()


def main_convert_alias():
    """command alias
    hconvert = interfacetester convert
    """
    rewrite_alias_to_subcommand("convert")
    main()


if __name__ == "__main__":
    main()
