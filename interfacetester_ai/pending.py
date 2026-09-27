# -*- coding: utf-8 -*-
"""pending —— PENDING 清单落盘 writer（v8 §8.1 S-6，2026-09-23）。

## 为什么需要这个文件

§3.4 严重级别表把 S3(部分) / S4 的发现定为 **PENDING**：属**语义**判断，
闸门不替用户下结论 → 提请人工。PENDING 的落点（§0.6 技术路线一图）：
`<case>.pending.json`（进人工确认清单）。T1 落地后 S3 的 PENDING 态已存在
（显式 `validate: []` → 1 条 S3 PENDING），落盘对象齐备——本模块补上
「从 GateReport 到清单文件」的最后一步。

## 架构纪律（§0.6：S 系列闸门 = 纯代码、零副作用）

**落盘不是闸门的事**：`gates.check_testcase` 保持零副作用（只返回 report），
本模块是独立的 writer，由**调用方**（装配器 / 生成流程 / 审核台）显式调用。
这正是红线③「按写入目标路径 + 模块白名单」判定的用武之地：
`pending` 是**登记在册的写入者**，写入根固定 `.ai/`
（`bench/write_boundary_scanner.py` 的 MODULE_WRITE_BOUNDARIES 登记为
`("roots", (".ai/",))`）；写盘行的目标表达式必须是**源码可见前缀**
（字面量拼接），变量根会被 T18 扫描器判「不可静态判定」从严违规。

## 清单格式（§5.2 口径）

文件名 `<case>.pending.json`（按用例名命名）；内容为可回读 JSON：

    {"case": <用例名>, "count": <PENDING 条数>,
     "items": [{"code": "S3", "severity": "pending", "where": …,
                "message": …, "hint": …}]}

与 `Finding.to_dict()` 同构（code/severity/where/message/hint），
人工确认台 / Web 表单（P3b）可直接消费。

## 用法（cwd 必须是工作区根——.ai/ 是相对根，§6）

    report = check_testcase(case)
    rel = write_pending_list(report, "login_flow")   # None = 无 PENDING，不落盘
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Text

from interfacetester_ai.gates import GateReport

# 登记在册的写入根（相对工作区根；T18 注册表 pending → ("roots", (".ai/",))）
PENDING_DIR = ".ai/pending"


def pending_items(report: GateReport) -> List[Dict[Text, Any]]:
    """把 report 的 PENDING findings 序列化成清单条目（Finding.to_dict 同构）。"""
    return [f.to_dict() for f in report.pendings]


def pending_path(case_name: Text) -> Text:
    """清单相对路径（§5.2 命名口径：`<case>.pending.json`）。"""
    return PENDING_DIR + "/" + case_name + ".pending.json"


def write_pending_list(report: GateReport, case_name: Text) -> Optional[Text]:
    """有 PENDING 才落盘 `.ai/pending/<case>.pending.json`；返回相对路径或 None。

    - 闸门零副作用纪律的**调用侧**：本函数由装配器 / 生成流程显式调用，
      不会在 check_testcase 里被隐式触发；
    - 无 PENDING（report.pendings 为空）→ 不创建任何文件，返回 None——
      「没有待确认项」不该留一个空清单让人误以为有活要干；
    - cwd 必须是工作区根：`.ai/` 是固定相对根（§6 写盘边界）。
    """
    items = pending_items(report)
    if not items:
        return None

    payload: Dict[Text, Any] = {
        "case": case_name,
        "count": len(items),
        "items": items,
        "note": "PENDING 属语义判断，闸门不替用户下结论——请人工确认后回填"
        "（§3.4 / §5.2；确认留痕见 .ai/reviews/，红线⑥）",
    }
    os.makedirs(".ai/pending", exist_ok=True)  # 写入根字面量（T18 静态可判）
    # 写盘行必须**内联字面量前缀**（T18 白名单纪律：变量/中间变量目标会被判
    # 「不可静态判定」从严违规——登记在册的写入者把根写在源码里，是纪律的一部分）
    with open(".ai/pending/" + case_name + ".pending.json", "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    return pending_path(case_name)


__all__ = ["PENDING_DIR", "pending_items", "pending_path", "write_pending_list"]
