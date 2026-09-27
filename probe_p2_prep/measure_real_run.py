# -*- coding: utf-8 -*-
"""真实端到端复跑之后的**interface 覆盖率**统计 —— **只读，不调模型**。

用法：

    python probe_p2_prep/measure_real_run.py <工作区目录> [文档路径]

它回答用户最关心的那一个问题：**"文档里的接口，有多少产出了可运行的用例？"**

- **分母** = `gen.interface_sections()` 识别出的**接口段**（确定性口径，与 `--assertions-only` 同源）；
- **分子** = 最终产物（`.ai/draft/*.yml`）里**端点落在这 17 段上**的段数（同一段多条只算 1）；
- 另附：T28 降级统计（从 `run.log` 取那行 + 按 `unknowns` 的 kind 汇总）、`quote-bound` 次数
  （证据绑定发生了多少次 = "模型引用写歪、工具救回来"的次数）。
"""

import glob
import io
import os
import re
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from interfacetester_ai.gen import interface_sections  # noqa: E402

DOC_DEFAULT = os.path.join(BASE, "project-three", "统一文件服务平台-接入应用接口文档.md")
URL_RE = re.compile(r"^\s*url:\s*(\S+)\s*$", re.M)

# ★统一的 url 归一：去掉变量前缀、查询串与尾斜杠 —— 两侧（文档段 / 产物）用同一口径
ENV_PREFIX_RE = re.compile(r"^\$\{[^}]*\}")


def normalize_url(url):
    text = (url or "").strip().strip("'\"")
    text = ENV_PREFIX_RE.sub("", text)
    text = text.split("?", 1)[0].rstrip("/")
    return text


def draft_urls(workspace):
    found = []
    for path in sorted(glob.glob(os.path.join(workspace, ".ai", "draft", "*.yml"))):
        text = io.open(path, encoding="utf-8", errors="replace").read()
        urls = [normalize_url(item) for item in URL_RE.findall(text)]
        found.append((os.path.basename(path), [item for item in urls if item]))
    return found


def unknowns_summary(workspace):
    kinds = Counter()
    bound = 0
    for path in glob.glob(os.path.join(workspace, ".ai", "draft", "*.unknowns.json")):
        text = io.open(path, encoding="utf-8", errors="replace").read()
        for kind in re.findall(r'"kind":\s*"([^"]+)"', text):
            kinds[kind] += 1
        bound += text.count('"kind": "quote-bound"')
    return kinds, bound


def log_lines(workspace):
    path = os.path.join(workspace, "run.log")
    if not os.path.exists(path):
        return []
    text = io.open(path, encoding="utf-8", errors="replace").read()
    return [line for line in text.splitlines() if "haify gen" in line or "T28" in line]


def main():  # noqa: C901
    for stream in (sys.stdout, sys.stderr):
        try:  # ★Windows 控制台默认 GBK：不重设的话 '✔' 这类字符会让脚本**自己崩掉**
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    workspace = sys.argv[1] if len(sys.argv) > 1 else ""
    doc_path = sys.argv[2] if len(sys.argv) > 2 else DOC_DEFAULT
    if not workspace or not os.path.isdir(workspace):
        print("用法：python probe_p2_prep/measure_real_run.py <工作区目录> [文档路径]")
        return 1
    doc = io.open(doc_path, encoding="utf-8", errors="replace").read()
    sections = interface_sections(doc)
    by_url = {}
    for section in sections:
        by_url.setdefault(normalize_url(section.url), section)

    drafts = draft_urls(workspace)
    hit = set()
    unmatched = []
    for name, urls in drafts:
        matched = False
        for url in urls:
            section = by_url.get(url)
            if section is not None:
                hit.add(section.url)
                matched = True
        if not matched:
            unmatched.append((name, urls))

    total = len(sections)
    covered = len(hit)
    rate = f"{covered / total:.0%}" if total else "n/a"

    print("=" * 70)
    print(f"工作区：{workspace}")
    print(f"文档  ：{os.path.basename(doc_path)}")
    print("=" * 70)
    print(f"\n## 一、**接口覆盖率**（用户最关心的那个数）\n")
    print(f"  文档识别出的接口段（分母）  ：{total}")
    print(f"  产出了草稿的接口段（分子）  ：{covered}")
    print(f"  **接口覆盖率**              ：{rate}")
    print(f"  产物文件数                  ：{len(drafts)}")
    if unmatched:
        print(f"\n  产物里**对不上任何接口段**的：{len(unmatched)}")
        for name, urls in unmatched[:10]:
            print(f"    - {name}  urls={urls}")

    print("\n## 二、逐段对照\n")
    for section in sections:
        mark = "[OK]" if section.url in hit else "[--]"
        print(f"  {mark} {section.method:5} {section.url}")

    kinds, bound = unknowns_summary(workspace)
    print("\n## 三、`unknowns` 的原因分布（全部产物合计）\n")
    for kind, count in kinds.most_common():
        print(f"  {count:>4}  {kind}")
    print(f"\n  ★其中 `quote-bound`（**模型引用写歪、工具绑定救回**）：{bound} 条")

    lines = log_lines(workspace)
    if lines:
        print("\n## 四、CLI 收尾那几行\n")
        for line in lines[-8:]:
            print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
