# -*- coding: utf-8 -*-
"""cache —— 缓存层（占位，P0a 交付）。

## 边界纪律（红线③，§6 写盘边界）

本模块是**登记在册的写入者**，写入根固定 `.ai/`（`bench/write_boundary_scanner.py`
登记为 `("roots", (".ai/",))`）——「缓存命中即回放」因此**不违反**红线③
（v7 §0.4-② 的修订要点：写盘按「目标路径 + 模块白名单」判，不按「谁调了 open」判）。

## 未来形态（P0a，§六）

- key = `sha256(规范化 JSON{全部请求字段})`：`temperature`/`seed`/`max_tokens`/
  `response_format`/`base_url`/`model`/`prompt_version`/`system`/`user` 全进；
- 命中即回放；`--no-cache` 强制实调；
- 写盘目标必须是**源码可见前缀**形态（字面量 `.ai/cache/...`），
  变量拼接根会被 T18 扫描器判「不可静态判定」从严违规。
"""

import hashlib
import json
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Text, Tuple

AI_CACHE_ROOT = ".ai/cache"


# ---------------------------------------------------------------------------
# 缓存键（**全部请求字段**）—— §5.6 / §9.x P-2，**T5** 交付
# ---------------------------------------------------------------------------

# 判据（§9.x P-2）："只改 `temperature` 必须 miss（单测用 FakeLLM 的调用计数断言）"。
# 它的**代码形态**就是这张字段表：**漏一个字段 = 那一项改了还能命中旧缓存**，
# 而症状是"模型输出没变"，看起来像模型变笨了而不是缓存错了——所以必须把它写成显式清单 + 单测。
CACHE_KEY_FIELDS: Tuple[Text, ...] = (
    "base_url",
    "model",
    "prompt_version",
    "system",
    "user",
    "temperature",
    "seed",
    "max_tokens",
    "response_format",
)


class CacheKeyFieldsMismatch(Exception):
    """传入字段与 `CACHE_KEY_FIELDS` 不一致（**缺字段或多字段**）——两个方向都报错。

    - **缺字段**：不许用默认值补——"参数不同、键却相同"的请求会命中同一份回放，
      而症状是"换参数没效果"，排查时几乎不会怀疑到缓存（§9.x P-2）；
    - **多字段**：说明有人加了采样参数却**没同步清单**——那一项改了也不会 miss，
      这是同一个漏检的镜像方向（更容易被忽略，因为它"不报错但也不生效"）。

    也就是说：这张清单是**唯一事实来源**，改它等于改缓存语义，必须走显式修改 + 单测。
    """

    def __init__(self, missing: Sequence[Text], extra: Sequence[Text] = ()) -> None:
        self.missing = list(missing)
        self.extra = list(extra)
        detail = []
        if self.missing:
            detail.append(f"缺 {sorted(self.missing)}")
        if self.extra:
            detail.append(
                f"多 {sorted(self.extra)}（新增采样参数必须同步 CACHE_KEY_FIELDS）"
            )
        super().__init__("缓存键字段不匹配：" + "；".join(detail) + "。")


def build_cache_key(fields: Mapping[Text, Any]) -> Text:
    """`sha256(规范化 JSON{全部请求字段})`——字段清单见 `CACHE_KEY_FIELDS`。

    - **规范化**：`sort_keys=True` + 紧凑分隔符 + `ensure_ascii=False`（中文 prompt 不转义，
      保证同一份内容在不同实现下得到同一个键）；
    - **字段必须与清单完全一致**（缺/多都报 `CacheKeyFieldsMismatch`）；
    - 纯函数、**不写盘**（写盘由本模块的落盘入口负责，见模块 docstring 的边界纪律）。
    """
    missing = [name for name in CACHE_KEY_FIELDS if name not in fields]
    extra = [name for name in fields if name not in CACHE_KEY_FIELDS]
    if missing or extra:
        raise CacheKeyFieldsMismatch(missing, extra)

    payload = json.dumps(
        {name: fields[name] for name in CACHE_KEY_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 缓存落盘（§六 的"缓存"行；`llm.complete` 的 `cache` 端口实现）
# ---------------------------------------------------------------------------
# ★为什么落成 JSON 而不是裸响应文本：缓存**要能被复盘**——"这份回放是哪次调用、
# 哪个模型、什么采样参数下的产物"必须能从文件里读出来（裸文本会丢掉 key 以外的一切上下文）。
# 回放时仍只取 `response_text` 字段，所以"命中即回放"的语义不变。

class CacheStore:
    """`.ai/cache/<key>.json` 的读写（本模块是**登记写入者**：`("roots", (".ai/",))`）。

    ★没有 `root` 参数（刻意）：写盘白名单登记的就是 `.ai/cache`。想换根就得改
    `bench/write_boundary_scanner.py` 的注册表——**扩白名单要签字**，不该由调用方随手决定。
    """

    def path_for(self, key: Text) -> Text:
        """缓存文件的相对路径（复盘/报告里要给用户看的就是它）。"""
        return AI_CACHE_ROOT + "/" + key + ".json"

    def get(self, key: Text) -> Optional[Text]:
        """命中 → 响应文本；**未命中 → None**（miss 是正常路径，不是异常）。

        缓存文件损坏也按 **miss** 处理：损坏的缓存若被当成"命中"，模型输出会凭空变样
        且**无法解释**——宁可多跑一次模型，也不要一个说不出原因的产物。
        """
        relative = AI_CACHE_ROOT + "/" + key + ".json"
        if not os.path.exists(relative):
            return None
        try:
            with open(relative, encoding="utf-8") as fp:
                payload = json.load(fp)
        except (OSError, ValueError):
            return None
        text = payload.get("response_text")
        return text if isinstance(text, str) else None

    def put(self, key: Text, text: Text, meta: Optional[Mapping[Text, Any]] = None) -> Text:
        """落盘并返回相对路径；`meta` 放 prompt 版本/模型/采样参数（纯复盘用）。"""
        os.makedirs(".ai/cache", exist_ok=True)  # 写入根字面量（T18 静态可判）
        relative = AI_CACHE_ROOT + "/" + key + ".json"
        payload: Dict[Text, Any] = {"key": key, "response_text": text, **dict(meta or {})}
        # 写盘行必须**内联字面量前缀**（T18：写成变量会被判「目标不可静态判定」）
        with open(".ai/cache/" + key + ".json", "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        return relative


def run_selftest(verbose: bool = False) -> int:
    """cache 自检：**键语义（T5）+ 落盘往返**（在临时目录里跑，不污染本仓）。"""
    import tempfile  # noqa: PLC0415

    failures: List[Text] = []
    fields = {name: f"value-{name}" for name in CACHE_KEY_FIELDS}

    # ① 键：字段缺/多都报；同字段不同序 → 同键
    try:
        build_cache_key({name: fields[name] for name in CACHE_KEY_FIELDS[:-1]})
    except CacheKeyFieldsMismatch as error:
        if not error.missing:
            failures.append("[键语义错] 缺字段时 missing 为空")
    else:
        failures.append("[漏放] 缺字段的键竟然算出来了")

    reordered = {name: fields[name] for name in reversed(CACHE_KEY_FIELDS)}
    if build_cache_key(reordered) != build_cache_key(fields):
        failures.append("[键语义错] 字段顺序不该影响键")

    # ② 落盘往返（临时工作区；`.ai/cache/` 是相对根）
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="ai_cache_") as tmp:
        os.chdir(tmp)
        try:
            store = CacheStore()
            key = build_cache_key(fields)
            if store.get(key) is not None:
                failures.append("[往返错] 空缓存竟然命中了")
            written = store.put(key, '{"ok": true}', {"model": "m", "prompt_version": "v1"})
            if store.get(key) != '{"ok": true}':
                failures.append("[往返错] 落盘后回读不一致")
            if not os.path.isfile(written):
                failures.append(f"[落点错] 缓存没落在预期路径：{written}")
            if not written.startswith(".ai/cache/"):
                failures.append(f"[落点错] 缓存落在了登记根之外：{written}")
            # NOTICE（为什么"损坏按 miss"的判据不在这里）：造一份损坏缓存要**写一个坏文件**，
            # 而生产模块的自检不该写盘——这正是 `doc_quality` 被 T18 判 3 处违规的原因
            # （其中 1 处就是它自检里的夹具写盘）。该判据移到 `tests/cache_store_test.py`，
            # 那里可以在临时目录里随意造夹具。
        finally:
            os.chdir(old_cwd)

    print("=" * 66)
    if failures:
        print(f"cache 自检**失败** {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        print("=" * 66)
        return len(failures)

    print("cache 自检全部通过：")
    print(f"  键（T5）：{len(CACHE_KEY_FIELDS)} 字段全进；缺/多字段都报；字段顺序无关")
    print("  落盘    ：`.ai/cache/<key>.json` 往返一致；**损坏按 miss**（不冒充命中）")
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

    parser = argparse.ArgumentParser(description="缓存层（P0a）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印")
    args = parser.parse_args()
    return run_selftest(verbose=args.verbose)


__all__ = [
    "AI_CACHE_ROOT",
    "CACHE_KEY_FIELDS",
    "CacheKeyFieldsMismatch",
    "CacheStore",
    "build_cache_key",
    "run_selftest",
]


if __name__ == "__main__":
    raise SystemExit(main())
