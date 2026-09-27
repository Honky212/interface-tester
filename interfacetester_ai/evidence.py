# -*- coding: utf-8 -*-
"""evidence —— **证据包装配**（v8 §5.3 的"输入装配（纯代码）"，**P1a**，2026-09-25）。

## 这个模块在整条链上的位置

`haify analyze` 的形状是 **纯代码装配 → 一次模型调用 → 纯代码校验/渲染**：

    ① 读 `summary.json`（runner 产物）
    ② **本模块**：失败断言 → evidence 包（纯代码，不调模型、不下结论）
    ③ `llm`：一次调用，逐条要引用
    ④ `diagnosis`：证据纪律校验（引用必须逐字、无证据 → 降级"未知"）
    ⑤ 渲染 `reports/analysis.md` / `reports/doc_defects.md`

★**②这一步为什么不能省**：§5.3 的"**证据优先**"要成立，前提是**证据真的被送进模型**。
只把"失败了"这个消息丢给模型，它必然会**编**一个原因——那正是 R3 要防的。

## 脱敏（合规红线，**统一口径**）

请求摘要里的凭据必须替换。判定"什么是凭据"**走内核 `utils.is_sensitive_key`**——
`interfacetester/converters/ir.py` 的注释里记着这个教训：那里曾经有两份**窄清单**，
漏判了 Cookie / 查询串 / `X-Signature` / 裸 `token`。
**新增一个脱敏源就必须继承同一口径**，否则同一个洞会再开一次。

## 本模块**不写盘**（只读日志、只返回对象）

读 `run.log` 是**读**，不受 T18 约束（红线③ 管的是写入目标）；
写盘由 `analyze`/`diagnosis` 做，各自在其登记根下。

## 边界（诚实）

- 响应体 **≤2KB**（§5.3 明写）——超长就**截断并在包里标明**（不静默丢，也不假装完整）；
- 日志只取**关键行**（WARNING/ERROR/FAIL/assert/Traceback），且**带行号**便于报告定位；
  日志路径不存在**不算错**——很多 CI 环境不落盘。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Text, Tuple

# §5.3：response body ≤2KB
RESPONSE_BODY_LIMIT = 2048
# 日志"关键行"上限（够定位就行；证据包是**喂给模型**的，不是全量日志搬运）
LOG_LINE_LIMIT = 40

# `run.log` 里的关键行（失败定位用）
LOG_KEYWORD_RE = re.compile(r"WARNING|ERROR|FAIL|assert|Traceback|Exception", re.IGNORECASE)

# 脱敏后的占位（形态与内核 IR 一致：`${...}` 占位，明文不出现）
REDACTED = "${REDACTED}"


def _redact_mapping(mapping: Any) -> Dict[Text, Any]:
    """把 dict 里**敏感键**的值换成占位（键名判定走内核 `is_sensitive_key`）。

    ★为什么是"值换占位"而不是"删掉整个键"：删掉会让模型看不到"这里有认证"，
    于是它可能把 401 归因成"接口写错了"——**保留键名、去掉值**才既合规又不丢信息。
    """
    from interfacetester.utils import is_sensitive_key  # noqa: PLC0415

    out: Dict[Text, Any] = {}
    if not isinstance(mapping, dict):
        return out
    for key, value in mapping.items():
        name = str(key)
        if is_sensitive_key(name):
            out[name] = REDACTED
        elif isinstance(value, dict):
            out[name] = _redact_mapping(value)
        else:
            out[name] = value
    return out


def _clip(text: Text, limit: int) -> Text:
    """截断并**标明**（`…[已截断 N 字符]`）——静默截断会让模型以为"原文就这样"。"""
    if not isinstance(text, str):
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[已截断 {len(text) - limit} 字符]"


@dataclass(frozen=True)
class EvidencePack:
    """**一个失败断言**的证据包（§5.3 的输入装配单位）。

    字段与 §5.3 原文一一对应：`case` / `step` / `validator 字典` /
    `同步骤其余断言结果` / `request 摘要(已脱敏)` / `response body ≤2KB` /
    `run.log 路径 + 关键 WARNING 行`。
    """

    case: Text
    step: Text
    step_type: Text = ""  # 内核 `validators` 的键（validate / validate_extractor / …）
    validator: Dict[Text, Any] = field(default_factory=dict)
    siblings: Tuple[Dict[Text, Any], ...] = ()
    request: Dict[Text, Any] = field(default_factory=dict)
    response: Dict[Text, Any] = field(default_factory=dict)
    log_path: Text = ""
    log_lines: Tuple[Text, ...] = ()

    def assertion_line(self) -> Text:
        """人话描述这条失败断言（报告与 prompt 都用它）。"""
        v = self.validator
        return f"{v.get('comparator', '?')}: [{v.get('check', '?')}, {v.get('expect', '?')}]"

    def response_text(self) -> Text:
        """响应侧**引用校验的 haystack**（§5.3："必须是对应切片的逐字子串"）。"""
        body = self.response.get("body")
        return body if isinstance(body, str) else ""

    def haystack(self) -> Text:
        """整包可引用的原文（响应体 + 关键日志行）——`log` 类引用在这里校验。"""
        parts = [self.response_text()]
        parts.extend(self.log_lines)
        return "\n".join(part for part in parts if part)

    def to_dict(self) -> Dict[Text, Any]:
        return {
            "case": self.case,
            "step": self.step,
            "step_type": self.step_type,
            "failed_assertion": dict(self.validator),
            "other_assertions": [dict(item) for item in self.siblings],
            "request": dict(self.request),
            "response": dict(self.response),
            "log_path": self.log_path,
            "log_lines": list(self.log_lines),
        }


# ---------------------------------------------------------------------------
# 装配：`summary.json` → evidence 包（纯代码，§5.3 的"输入装配"）
# ---------------------------------------------------------------------------

def _iter_validators(data: Dict[Text, Any]) -> List[Tuple[Text, Dict[Text, Any]]]:
    """摊平 `data["validators"]`——内核把它按 **step_type 分组**存（`{step_type: [断言…]}`）。"""
    out: List[Tuple[Text, Dict[Text, Any]]] = []
    validators = data.get("validators") if isinstance(data, dict) else None
    if not isinstance(validators, dict):
        return out
    for step_type, items in validators.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                out.append((str(step_type), item))
    return out


def _response_digest(response: Any, *, body_limit: int) -> Dict[Text, Any]:
    """响应摘要：`status_code` + 少量头 + **body ≤2KB**。

    ★头为什么也要留一点：`Content-Type` 决定"响应是不是 JSON"，而"非 JSON 上写了
    `body.x`"正是 §六 硬规则 6 点名的坑——没有它，模型会把这类失败**归因错**。
    """
    if not isinstance(response, dict):
        return {}
    headers = response.get("headers")
    keep: Dict[Text, Any] = {}
    if isinstance(headers, dict):
        keep = {
            str(k): v
            for k, v in headers.items()
            if str(k).lower() in ("content-type", "content-length")
        }
    return {
        "status_code": response.get("status_code"),
        "content_type": response.get("content_type"),
        "headers": keep,
        "body": _clip(response.get("body"), body_limit),
    }


def _request_digest(request: Any) -> Dict[Text, Any]:
    """请求摘要（**已脱敏**）：method / url / headers / 参数。"""
    if not isinstance(request, dict):
        return {}
    return {
        "method": request.get("method"),
        "url": request.get("url"),
        "headers": _redact_mapping(request.get("headers")),
        "params": _redact_mapping(request.get("params")),
    }


def key_log_lines(log_path: Text, *, limit: int = LOG_LINE_LIMIT) -> Tuple[Text, ...]:
    """读 `run.log` 的关键行（**带行号**）。**路径不存在 → 空元组**（不算错）。

    ★"不存在不算错"是个故意选择：很多 CI 环境不落盘日志。而"没有日志"应该表现为
    **这一路证据少一份**（`diagnosis` 据此把结论降级），而不是让整条分析链崩掉。
    """
    if not log_path or not os.path.exists(log_path):
        return ()
    lines: List[Text] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fp:
            for number, line in enumerate(fp, 1):
                if LOG_KEYWORD_RE.search(line):
                    lines.append(f"{number}: {line.rstrip()}")
                if len(lines) >= limit:
                    break
    except OSError:
        return ()
    return tuple(lines)


def build_evidence_packs(
    summary: Any,
    *,
    body_limit: int = RESPONSE_BODY_LIMIT,
    log_line_limit: int = LOG_LINE_LIMIT,
) -> List[EvidencePack]:
    """`summary.json` → evidence 包列表（**只取失败断言**）。

    口径（§5.3）：`details[]` 里 `success == false` 的用例 → 每个
    `check_result == "fail"` 的断言 **一包**；同步骤其余断言进 `siblings`。

    ★`siblings` 不是凑数：**"别的断言都过了、只有这条没过"** 是极强的信号——
    它往往指向"这条断言写错了"（用例错误），而不是"接口坏了"（真实缺陷）。
    少了它，模型只能看到孤零零一条失败。
    """
    packs: List[EvidencePack] = []
    details = summary.get("details") if isinstance(summary, dict) else None
    if not isinstance(details, list):
        return packs

    for detail in details:
        if not isinstance(detail, dict) or detail.get("success") is not False:
            continue  # 只看失败的用例
        case = str(detail.get("name") or detail.get("case_id") or "")
        log_path = str(detail.get("log") or "")
        records = detail.get("records")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            data = record.get("data")
            if not isinstance(data, dict):
                continue
            flat = _iter_validators(data)
            if not flat:
                continue
            req_resps = data.get("req_resps")
            first = req_resps[0] if isinstance(req_resps, list) and req_resps else {}
            first = first if isinstance(first, dict) else {}
            step = str(record.get("name") or "")

            for step_type, validator in flat:
                if validator.get("check_result") != "fail":
                    continue
                packs.append(
                    EvidencePack(
                        case=case,
                        step=step,
                        step_type=step_type,
                        validator=dict(validator),
                        siblings=tuple(dict(v) for v in flat_values(flat) if v is not validator),
                        request=_request_digest(first.get("request")),
                        response=_response_digest(first.get("response"), body_limit=body_limit),
                        log_path=log_path,
                        log_lines=key_log_lines(log_path, limit=log_line_limit),
                    )
                )
    return packs


def flat_values(
    flat: Sequence[Tuple[Text, Dict[Text, Any]]],
) -> List[Dict[Text, Any]]:
    """摊平结果里的"断言本体"列表（`siblings` 用）。"""
    return [item for _step_type, item in flat]


# ---------------------------------------------------------------------------
# 自检（**不写盘**：只喂内存里的 summary 夹具）
# ---------------------------------------------------------------------------

_SELFTEST_SUMMARY: Dict[Text, Any] = {
    "success": False,
    "details": [
        {
            "name": "下单_正常",
            "success": False,
            "log": "",  # 不存在 → log_lines 为空（不抛，见 key_log_lines 的说明）
            "records": [
                {
                    "name": "下单",
                    "data": {
                        "validators": {
                            "validate_extractor": [
                                {
                                    "comparator": "equal",
                                    "check": "status_code",
                                    "expect": 200,
                                    "expect_value": 200,
                                    "check_result": "pass",
                                },
                                {
                                    "comparator": "equal",
                                    "check": "body.code",
                                    "expect": 0,
                                    "expect_value": 1001,
                                    "check_result": "fail",
                                },
                            ]
                        },
                        "req_resps": [
                            {
                                "request": {
                                    "method": "POST",
                                    "url": "/api/order",
                                    "headers": {
                                        "Authorization": "Bearer SECRET-abc",
                                        "Cookie": "sid=SECRET",
                                        "Accept": "application/json",
                                    },
                                },
                                "response": {
                                    "status_code": 200,
                                    "headers": {
                                        "Content-Type": "application/json",
                                        "Date": "Fri, 25 Sep 2026 01:00:00 GMT",
                                    },
                                    "content_type": "application/json",
                                    "body": '{"code": 1001, "message": "库存不足"}',
                                },
                            }
                        ],
                    },
                }
            ],
        },
        {"name": "查询_正常", "success": True, "records": []},
    ],
}


def run_selftest(verbose: bool = False) -> int:
    """evidence 自检。返回 0=全绿；非 0=失败项数。**不写盘**（夹具在内存里）。"""
    failures: List[Text] = []
    packs = build_evidence_packs(_SELFTEST_SUMMARY)

    # ① 只给**失败断言**打包（成功的用例、通过的断言都不该出现）
    if len(packs) != 1:
        failures.append(f"[取包] 应当只出 1 包（1 条 fail），实际 {len(packs)}")
    pack = packs[0] if packs else None

    # ② 失败断言本体 + `siblings` 是"**同步骤其余**断言"（不含自己）
    if pack:
        if pack.validator.get("check") != "body.code":
            failures.append(f"[断言] 包里的失败断言不对：{pack.validator}")
        if [v.get("check") for v in pack.siblings] != ["status_code"]:
            failures.append(f"[siblings] 应当是 status_code（且不含自己）：{pack.siblings}")
        if pack.case != "下单_正常" or pack.step != "下单":
            failures.append(f"[定位] case/step 不对：{pack.case} / {pack.step}")

    # ③ ★**脱敏**（合规红线）：凭据的值必须换成占位，**键名保留**。
    #    判据故意覆盖多个来源（Authorization / Cookie）——内核 `ir.py` 的教训
    #    就是"窄清单会漏"，我们继承 `is_sensitive_key` 就是为了不再漏。
    if pack:
        headers = pack.request.get("headers", {})
        for name in ("Authorization", "Cookie"):
            if headers.get(name) != REDACTED:
                failures.append(f"[脱敏] {name} 没被替换成占位：{headers.get(name)!r}")
        if headers.get("Accept") != "application/json":
            failures.append("[脱敏] 非敏感头被误删（'保留键名'才既合规又不丢信息）")

    # ④ 响应头只留"结论相关"的（`Content-Type` 留、`Date` 剔）
    if pack:
        kept = pack.response.get("headers", {})
        if "Content-Type" not in kept:
            failures.append("[响应头] Content-Type 应当保留（它决定'响应是不是 JSON'）")
        if any(str(k).lower() == "date" for k in kept):
            failures.append("[响应头] 易变的 Date 不该进证据包")

    # ⑤ 响应体 ≤2KB 且必要时**标明截断**（静默截断会让模型以为"原文就这样"）
    long_body = "x" * (RESPONSE_BODY_LIMIT + 500)
    if "已截断" not in _clip(long_body, RESPONSE_BODY_LIMIT):
        failures.append("[截断] 超长响应体应当标明截断，而不是静默丢掉")
    if _clip("short", RESPONSE_BODY_LIMIT) != "short":
        failures.append("[截断] 短文本不该被动")

    # ⑥ 日志取不到**不算错**（很多 CI 不落盘）
    if pack and pack.log_lines:
        failures.append(f"[日志] 路径不存在时应当是空元组：{pack.log_lines}")

    print("=" * 66)
    if failures:
        print(f"evidence 自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("证据包装配自检全部通过（§5.3 的\"输入装配（纯代码）\"）：")
    print("  取包  ：只给 `success=false` 里 `check_result==\"fail\"` 的断言打包")
    print("  同步骤：`siblings` 带**其余**断言结果（'只有这条没过'是强信号）")
    print("  脱敏  ：走内核 `utils.is_sensitive_key`（Authorization/Cookie/裸 token 一起覆盖）")
    print("  响应  ：头只留 Content-Type，body ≤2KB 且**标明截断**（不静默丢）")
    print("  日志  ：关键行带行号；**取不到不算错**（只意味着证据少一份）")
    print("=" * 66)
    return 0


def main() -> int:
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="证据包装配（P1a）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "LOG_KEYWORD_RE",
    "LOG_LINE_LIMIT",
    "REDACTED",
    "RESPONSE_BODY_LIMIT",
    "EvidencePack",
    "build_evidence_packs",
    "flat_values",
    "key_log_lines",
    "run_selftest",
]


if __name__ == "__main__":
    raise SystemExit(main())


