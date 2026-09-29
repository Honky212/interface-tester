"""本示例的 `debugtalk.py` **故意只留「取 mock 地址」一个函数**。

XML 断言全部走**框架内核算子**（`xpath_match` / `xpath_count`，A2-1 起内置，需要
`pip install -e ".[xml]"` 装可选 extra `xml`）。

**为什么必须单独开一个目录**：算子查找顺序是「项目 `debugtalk.py` → 框架内置」，
所以只要项目里定义了同名函数，内核算子就永远轮不到 —— `examples/soap/debugtalk.py`
（A1 方案，标准库实现、零依赖）里就有同名算子，**它会遮蔽内核实现**。
于是「想看内核的完整 XPath 1.0」这件事，只能在不定义同名函数的项目里演示。
"""

import os


def soap_base_url():
    """conftest 起好本地 mock 后把地址写进环境变量（autouse 夹具**不能给用例传值**）。"""
    return os.environ.get("SOAP_BASE_URL") or (
        f"http://127.0.0.1:{os.environ.get('SOAP_MOCK_PORT') or 8907}"
    )
