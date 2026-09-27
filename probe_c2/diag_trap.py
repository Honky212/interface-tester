# -*- coding: utf-8 -*-
"""C-2 接线诊断：TRAP_DRAFT 在合并开/关下的差异（在临时目录跑，不污染工作区）。"""

import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tests"))

from gen_pipeline_test import CONFIG, DOC, TRAP_DRAFT  # noqa: E402

from interfacetester_ai.cache import CacheStore  # noqa: E402
from interfacetester_ai.gen import generate_cases  # noqa: E402
from interfacetester_ai.llm import FakeTransport  # noqa: E402


def run(merge: bool):
    transport = FakeTransport(responder=lambda request: json.dumps(TRAP_DRAFT, ensure_ascii=False))
    results = generate_cases(DOC, transport=transport, config=CONFIG, cache=CacheStore(), merge=merge)
    for item in results:
        if item.skipped:
            continue
        print(f"\n--- merge={merge} | ok={item.ok} | rounds={[r.kind for r in item.rounds]}")
        assembly = item.assembly
        if assembly is None:
            print("   （无 assembly）")
            continue
        print("   gate_codes:", getattr(assembly, "gate_codes", None))
        print("   rejected  :", list(getattr(assembly, "rejected", []) or []))
        data = assembly.to_dict()
        print("   unknowns  :", [u["message"][:90] for u in data.get("unknowns", [])])


with tempfile.TemporaryDirectory(prefix="c2_diag_") as tmp:
    os.chdir(tmp)
    run(merge=False)
    run(merge=True)
