# -*- coding: utf-8 -*-
r"""`python -m interfacetester_ai ...` ≡ `haify ...`（§5.1 / §六）。

**为什么留这个入口**：console_script（`haify`）要 `pip install` 之后才存在；
而 `-m` 只要源码在 `sys.path` 里就能跑——CI 与"没装包的开发机"上，后者更可靠。
两个入口指向**同一个** `cli.main`，不存在两套行为（本仓对双源的一贯态度）。
"""

from __future__ import annotations

import sys

from interfacetester_ai.cli import main

if __name__ == "__main__":
    sys.exit(main())