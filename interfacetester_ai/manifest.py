# -*- coding: utf-8 -*-
"""manifest —— 审计层（§六 的"审计"行；**P0a** E 阶段落地）。

## 边界纪律（红线③，§6 写盘边界）

本模块是**登记在册的写入者**，写入根固定 `.ai/`（`bench/write_boundary_scanner.py`
登记为 `("roots", (".ai/",))`）。

## 它记什么（§六 的审计行）

每次模型调用落 `.ai/manifest/<ts>-<input-hash>.json`：**model / prompt 版本 / 输入 hash /
采样参数 / 原始响应 / 重试轨迹**——"LLM 的每一次臆造从此可复盘"。

★两条容易漏的：
1. **命中缓存的那次也要写**（`cached: true`）——不写的话，复盘时会以为"模型又跑了一遍"，
   而两次产物其实来自同一份回放（`llm.complete` 已按此口径调用本端口）；
2. **原始响应必须照录**（不是只记 hash）——"它到底吐了什么"是复盘**唯一**的一手材料，
   只留 hash 等于把证据换成指纹。

## 人工确认留痕

`.ai/reviews/*.json`（谁、何时、接受/拒绝什么、依据哪个 manifest hash——红线⑥）。
本模块只定根常量；写入留给确认台（P3b），**不预造接口**。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Text

AI_MANIFEST_ROOT = ".ai/manifest"
AI_REVIEWS_ROOT = ".ai/reviews"


def _default_stamp() -> Text:
    """时间戳（`%Y%m%dT%H%M%S%f`）：**到微秒**，避免同一次运行里连续两次调用撞名。"""
    return datetime.now().strftime("%Y%m%dT%H%M%S%f")


class ManifestStore:
    """`.ai/manifest/<ts>-<input-hash>.json` 的写入（`llm.complete` 的 `audit` 端口实现）。"""

    def __init__(self, clock: Optional[Callable[[], Text]] = None) -> None:
        self._clock = clock or _default_stamp
        self.entries: List[Dict[Text, Any]] = []  # 内存副本（测试与 CLI 报告用）

    def name_for(self, entry: Mapping[Text, Any], stamp: Optional[Text] = None) -> Text:
        """文件名：`<ts>-<input-hash>.json`（`input_hash` 缺失时退化为 `unknown`）。"""
        suffix = str(entry.get("input_hash") or "unknown")
        return f"{stamp or self._clock()}-{suffix}.json"

    def record(self, entry: Mapping[Text, Any]) -> Text:
        """落盘一条调用记录，返回相对路径。**调用方保证 entry 是可 JSON 化的。**"""
        os.makedirs(".ai/manifest", exist_ok=True)  # 写入根字面量（T18 静态可判）
        # ★时间戳**只取一次**：文件名与 `recorded_at` 必须是同一个时间——取两次会让
        # "这条记录何时发生"出现两个互不相同的答案（实测踩到：还顺带让注入式时钟耗尽）。
        stamp = self._clock()
        name = self.name_for(entry, stamp)
        payload = dict(entry)
        payload.setdefault("recorded_at", stamp)
        # 写盘行必须**内联字面量前缀**（T18：写成变量会被判「目标不可静态判定」）
        with open(".ai/manifest/" + name, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2, default=str)
            fp.write("\n")
        self.entries.append(payload)
        return AI_MANIFEST_ROOT + "/" + name


def run_selftest(verbose: bool = False) -> int:
    """manifest 自检（临时目录里跑真实落盘，不污染本仓）。"""
    import tempfile  # noqa: PLC0415

    failures: List[Text] = []
    ticks = iter(["20260925T010203000001", "20260925T010203000002"])

    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="ai_manifest_") as tmp:
        os.chdir(tmp)
        try:
            store = ManifestStore(clock=lambda: next(ticks))
            entry = {
                "input_hash": "abc123",
                "model": "qwen2.5:14b",
                "prompt_version": "v2.0",
                "temperature": 0.0,
                "response_text": '{"ok": true}',
                "cached": False,
            }
            first = store.record(entry)
            if not os.path.isfile(first):
                failures.append(f"[落点错] 审计没落在预期路径：{first}")
            if not first.startswith(".ai/manifest/"):
                failures.append(f"[落点错] 审计落在登记根之外：{first}")
            if not first.endswith("-abc123.json"):
                failures.append(f"[命名错] 文件名应含输入 hash：{first}")

            with open(first, encoding="utf-8") as fp:
                payload = json.load(fp)
            # ★原始响应必须照录（复盘的一手材料）
            if payload.get("response_text") != '{"ok": true}':
                failures.append("[内容缺项] 原始响应没有被记进审计（复盘就没了唯一一手材料）")
            for key in ("model", "prompt_version", "temperature", "recorded_at"):
                if key not in payload:
                    failures.append(f"[内容缺项] 审计里缺 {key}")

            # 连续两次调用不许撞名（时间戳到微秒）
            second = store.record({**entry, "cached": True})
            if second == first:
                failures.append("[命名错] 两次调用撞了同一个文件名")
            if len(store.entries) != 2:
                failures.append("[内存副本错] entries 应累计 2 条")
        finally:
            os.chdir(old_cwd)

    print("=" * 66)
    if failures:
        print(f"manifest 自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("manifest 自检全部通过：")
    print("  落盘：`.ai/manifest/<ts>-<input-hash>.json`；连续调用不撞名（微秒时间戳）")
    print("  内容：**原始响应照录** + model/prompt_version/采样参数/重试轨迹（§六 审计行）")
    print(f"  留痕：人工确认目录常量已定（{AI_REVIEWS_ROOT}，红线⑥；写入留给确认台 P3b）")
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

    parser = argparse.ArgumentParser(description="审计层（P0a）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "AI_MANIFEST_ROOT",
    "AI_REVIEWS_ROOT",
    "ManifestStore",
    "run_selftest",
]


if __name__ == "__main__":
    raise SystemExit(main())
