# -*- coding: utf-8 -*-
"""归一化投影（方案 v8 §5.6；待办 T11 + T17 当日落地，2026-09-23）。

用途：把 runner 产出的 `summary.json` 投影成**结论确定**的可比形态——
剔除身份 / 时间 / 易变字段后做规范化 JSON 序列化，使
「同输入两次运行，out(x1) == out(x2) **逐字节相等**」成为一条
**真实验收**，而不是 v4 那条从第一天就红的"逐字节一致"（X2）。

剔除清单（§5.6，v7 补全响应侧白名单 / T17）：
  【身份与时间】 details[*].case_id / details[*].time.* / details[*].log（UUID 文件名）
                 details[*].records[*].elapsed / content_size
                 details[*].records[*].data.stat.*（response_time_ms / elapsed_ms / content_size）
                 顶层 time.*（★T11 落地时新发现的清单漏项：summary 级 start_at / duration
                            同样每次运行必变，不剔则"跨秒两次逐字节一致"仍恒红）
                 platform（跨机口径默认剔；同机口径可保留，见下）
  【请求侧】     req_resps[*].request.headers["interfacetester-Request-ID"]（UUID + 毫秒尾）
  【响应侧】     req_resps[*].response.headers 走**白名单**：
                 只留 Content-Type / Content-Length / 与 status 相关的头；
                 剔 Date（秒级）、Set-Cookie、Server、Age、ETag 等易变头（X5 / §12.7 乙①）
  【必须保留】   断言结果主体（data.validators：comparator / check / check_value /
                 expect / expect_value / check_result）——剔时间戳不是放松要求，
                 而是把要求提准：要的是"结论确定"，不是"字节确定"。

NOTICE（口径纪律，§5.6「口径提醒」）：
  - `keep_platform=False`（默认）→ **跨机可比**；
  - `keep_platform=True`  → **同机**对照（信息更多）。
  两种都行，但用例名必须写明口径（`..._cross_machine` / `..._same_machine`），
  不许含糊——这是 T17 判据的一部分。

T6（建 `interfacetester_ai` 包）落地时本模块应整体迁入该包（P0-0 的
"归一化管道独立实现"）——**已迁入（2026-09-23，v8 §十 T6）**：本模块现为
`interfacetester_ai/projection.py`，`bench/normalize_projection.py` 保留
兼容壳转发（单一来源，无双源漂移）。
"""

import copy
import json
import sys
from typing import Any, Dict, Text

__all__ = ["project_summary", "run_selftest"]

# 响应头白名单（§5.6【响应侧】）：只保留"结论相关"的稳定头，全小写比较。
RESPONSE_HEADER_KEEP = frozenset({"content-type", "content-length"})

# 请求侧必剔的头（内核注入的 UUID + 时间戳，§5.6【请求侧】），全小写比较。
REQUEST_HEADER_DROP = frozenset({"interfacetester-request-id"})


def _keep_response_header(name: Any) -> bool:
    """响应头白名单判定：`Content-Type` / `Content-Length` / **头名含 `status` 子串的头**。

    ★精确表述（v8 按独立评审 P4 改）：白名单 = `RESPONSE_HEADER_KEEP ∪ {n | "status" in n.lower()}`
    ——`Status` / `X-Status-Code` 会被保留，`Date` / `Set-Cookie` / `Server` 不会。
    原文档措辞"**与 status 相关的头**"不是标准 HTTP 类别、无法直接转成代码，故此处给出可执行形态；
    §5.6 的正文已同步（判据与实现必须同一句话）。

    ★已知边界（P4 第二点）：chunked 响应下 `Content-Length` 可能缺失、或表示**分块长度**——
    但它在**跨机投影**里仍稳定（两边都缺、或都取到同一个分块长度即一致），
    且它是"响应体长度"这个**结论相关**的信号；剔除它反而会让"响应体被截断"这类差异不可见。
    **处置：保留，并把该边界登记在此处与 §5.6。**
    """
    n = str(name).lower()
    return n in RESPONSE_HEADER_KEEP or "status" in n


def _filter_request_headers(request: Any) -> None:
    """请求头只剔内核注入的身份字段（interfacetester-Request-ID）。"""
    if not isinstance(request, dict):
        return
    headers = request.get("headers")
    if isinstance(headers, dict):
        for key in [k for k in headers if str(k).lower() in REQUEST_HEADER_DROP]:
            headers.pop(key, None)


def _filter_response_headers(response: Any) -> None:
    """响应头走白名单：剔 Date / Set-Cookie / Server / Age / ETag 等易变头。"""
    if not isinstance(response, dict):
        return
    headers = response.get("headers")
    if isinstance(headers, dict):
        for key in [k for k in headers if not _keep_response_header(k)]:
            headers.pop(key, None)


def project_summary(summary: Dict, *, keep_platform: bool = False) -> Text:
    """把一份 summary 字典投影为**结论确定**的规范化 JSON 字符串。

    Args:
        summary: `summary.json` 反序列化后的字典（runner 产物）
        keep_platform: False=跨机口径（剔顶层 platform，默认）；
                       True=同机口径（保留，信息更多）。用例名必须写明口径。

    Returns:
        规范化 JSON 文本（`sort_keys` + 紧凑分隔符）——同输入两次投影
        **逐字节相等**；两份只在"清单内易变字段"上不同的输入，投影后也
        **逐字节相等**（这正是 T17 判据的可执行形态）。
    """
    doc = copy.deepcopy(summary)
    if not isinstance(doc, dict):
        return _canonical(doc)

    # 【身份与时间】顶层：time（★T11 新发现漏项）与 platform（跨机口径）
    if isinstance(doc.get("time"), dict):
        doc["time"].clear()
    if not keep_platform:
        doc.pop("platform", None)

    details = doc.get("details")
    if not isinstance(details, list):
        return _canonical(doc)
    for d in details:
        if not isinstance(d, dict):
            continue
        d.pop("case_id", None)
        d.pop("log", None)
        if isinstance(d.get("time"), dict):
            d["time"].clear()
        records = d.get("records")
        if not isinstance(records, list):
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            rec.pop("elapsed", None)
            rec.pop("content_size", None)
            data = rec.get("data")
            if not isinstance(data, dict):
                continue
            if isinstance(data.get("stat"), dict):
                data["stat"].clear()
            req_resps = data.get("req_resps")
            if not isinstance(req_resps, list):
                continue
            for rr in req_resps:
                if not isinstance(rr, dict):
                    continue
                _filter_request_headers(rr.get("request"))
                _filter_response_headers(rr.get("response"))
    return _canonical(doc)


def _canonical(obj: Any) -> Text:
    """规范化 JSON 序列化：`sort_keys` + 紧凑分隔符——逐字节可比的保证。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 自检（投影后必须相等 / 必须保留 / 必须剔除——与闸门自检同构的成对纪律）
# ---------------------------------------------------------------------------

def _sample_summary(date: Text, case_id: Text, rid: Text, elapsed_ms: float) -> Dict:
    """对抗样本：按真实产物字段路径构造（§12.7 乙①/甲3 的实测结构）。"""
    return {
        "success": True,
        "time": {"start_at": 1790089342.6866713, "duration": 0.0241},
        "stat": {"testcases": {"total": 1, "success": 1, "fail": 0},
                 "teststeps": {"total": 2, "failures": 0, "successes": 2}},
        "platform": {"interfacetester_version": "5.0.1", "python_version": "CPython 3.12.10",
                     "platform": "Windows-11-10.0.26200-SP0"},
        "details": [{
            "name": "login_flow",
            "success": True,
            "case_id": case_id,
            "log": "logs/" + case_id + ".run.log",
            "time": {"start_at": 1790089342.68, "start_at_iso_format": "2026-09-22T15:02:22.68",
                     "duration": 0.0241},
            "in_out": {"config_vars": {}, "export_vars": {}},
            "records": [{
                "name": "login",
                "step_type": "request",
                "success": True,
                "elapsed": 0.123,
                "content_size": 72,
                "attachment": "",
                "export_vars": {"token": "******"},
                "data": {
                    "success": True,
                    "address": {"client_ip": "N/A", "client_port": 0, "server_ip": "N/A",
                                "server_port": 0},
                    "stat": {"content_size": 72, "response_time_ms": 7.18, "elapsed_ms": elapsed_ms},
                    "req_resps": [{
                        "request": {
                            "method": "POST", "url": "/api/login",
                            "headers": {"Content-Type": "application/json",
                                        "Accept": "*/*", "User-Agent": "python-requests/2.31",
                                        "interfacetester-Request-ID": rid},
                            "json": {"username": "admin", "password": "123456"},
                        },
                        "response": {
                            "status_code": 200,
                            "headers": {"Server": "BaseHTTP/0.6 Python/3.12.10",
                                        "Date": date,
                                        "Content-Type": "application/json",
                                        "Content-Length": "87"},
                        },
                    }],
                    "validators": [{
                        "comparator": "eq", "check": "status_code", "check_value": 200,
                        "expect": 200, "expect_value": 200, "check_result": "pass",
                    }],
                },
            }],
        }],
    }


def run_selftest(verbose: bool = False) -> int:
    """跑投影自检。返回 0=全绿；非 0=失败项数。"""
    import os

    failures = []
    x1 = _sample_summary("Tue, 22 Sep 2026 15:02:22 GMT",
                         "7fcfc1d6-05a8-412c-8826-fd15469e68a2",
                         "interfacetester-x1-150222", 7.18)
    x2 = _sample_summary("Tue, 22 Sep 2026 15:02:23 GMT",
                         "00000000-0000-0000-0000-000000000000",
                         "interfacetester-x2-260223", 8.04)

    out1, out2 = project_summary(x1), project_summary(x2)
    if out1 != out2:
        failures.append("[漏剔] 跨秒对抗样本投影后不一致（Date/case_id/Request-ID/elapsed/stat 未剔干净）")
    if "check_result" not in out1 or "comparator" not in out1:
        failures.append("[误剔] 断言结果主体（validators）必须保留在投影里")
    for banned in ('"Date"', '"Server"', "interfacetester-Request-ID", '"case_id"',
                   '"start_at"', '"response_time_ms"'):
        if banned in out1:
            failures.append("[漏剔] 投影输出仍含易变字段：" + banned)

    same1 = project_summary(x1, keep_platform=True)
    if "platform" not in same1:
        failures.append("[口径] keep_platform=True 必须保留顶层 platform（同机口径）")
    if "platform" in out1:
        failures.append("[口径] keep_platform=False（跨机默认）必须剔除顶层 platform")

    real = os.path.join("project-two", "logs", "login_flow.summary.json")
    if os.path.exists(real):
        with open(real, encoding="utf-8") as f:
            doc = json.load(f)
        if project_summary(doc) != project_summary(doc):
            failures.append("[不确定] 真实产物两次投影不一致（幂等性被破坏）")

    print("=" * 66)
    if failures:
        print("归一化投影自检**失败** " + str(len(failures)) + " 项：")
        for f in failures:
            print("  - " + f)
        print("=" * 66)
        return len(failures)

    print("归一化投影自检全部通过：")
    print("  跨秒确定性：两份只在易变字段上不同的输入，投影后逐字节相等（T17 判据）")
    print("  主体保留  ：断言结果（validators）完整保留在投影输出里")
    print("  白名单    ：响应头 Date/Server 剔除，Content-Type/Content-Length 保留")
    print("  口径纪律  ：跨机默认剔 platform；同机口径（keep_platform=True）保留")
    if os.path.exists(real):
        print("  真实产物  ：login_flow.summary.json 两次投影逐字节相等（幂等）")
    print("=" * 66)
    return 0


def main() -> int:
    # T26（方案 v8 §12.8-①）同款：重定向/管道/CI 下 Windows 默认 GBK 编码会崩。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    return run_selftest(verbose=True)


if __name__ == "__main__":
    raise SystemExit(main())

