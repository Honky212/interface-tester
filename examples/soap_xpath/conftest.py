"""示例工程夹具：**复用** `../soap/mock_soap.py` 的本地 SOAP mock（离线，不依赖外网）。

为什么复用而不是复制一份：两个示例演示的是**同一个被测服务**，同一份报文契约只维护一处，
才不会出现「改了一个示例的报文、另一个示例还是老契约」这种长期维护负担。
（照抄到自己项目时：把那 149 行的 mock 一起拿走，或把 `base_url` 指向真实服务。）

三条硬约束（与 `examples/soap/conftest.py` 同源，决定这里只能这么写）：

1. 生成的用例方法签名固定（`def test_start(self)`）→ **只有 `autouse=True` 的夹具生效**；
2. `autouse` 夹具**不能给用例传值** → mock 地址只能写 `os.environ`，由 `debugtalk.py` 的
   `soap_base_url()` 读（用例里写 `${soap_base_url()}`）；
3. 端口用 `0`（系统分配空闲端口）→ CI 上不会撞端口；想固定端口就设 `SOAP_MOCK_PORT`。
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SOAP_EXAMPLE_DIR = os.path.join(os.path.dirname(HERE), "soap")
sys.path.insert(0, SOAP_EXAMPLE_DIR)

from mock_soap import serve  # noqa: E402

BASE_URL_VAR = "SOAP_BASE_URL"


@pytest.fixture(autouse=True, scope="session")
def soap_mock():
    httpd, base_url = serve(port=int(os.environ.get("SOAP_MOCK_PORT") or 0))
    os.environ[BASE_URL_VAR] = base_url
    print(
        f"[soap_xpath] 本地 mock 已就绪：{base_url}"
        "（复用 examples/soap/mock_soap.py，SOAP_MOCK_PORT 可固定端口）",
        flush=True,
    )
    try:
        yield
    finally:
        httpd.shutdown()
        httpd.server_close()


def pytest_collection_modifyitems(config, items):
    """`contrast_*` 文件是**故意失败**的对照演示 → 默认跳过。

    想看「原始 XPath 的前缀是字面量」的真实后果，用环境变量显式打开：

    ```bash
    # Windows PowerShell
    $env:SOAP_RUN_CONTRAST=1; python -m interfacetester run examples/soap_xpath/contrast_prefix_literal.yml
    # Linux / macOS
    SOAP_RUN_CONTRAST=1 python -m interfacetester run examples/soap_xpath/contrast_prefix_literal.yml
    ```

    NOTICE: 按环境变量而不是「是否被显式指定」判断 —— 按文件名跳的规则对「单独跑这个文件」
    同样生效，那样文档里写的命令就跑不出效果（同 `examples/soap/conftest.py`）。
    """
    if (os.environ.get("SOAP_RUN_CONTRAST") or "").strip().lower() in ("1", "true", "yes", "on"):
        return

    skip_contrast = pytest.mark.skip(
        reason="对照演示用例（故意失败）：设 SOAP_RUN_CONTRAST=1 再单独跑本例，见 examples/soap_xpath/README.md"
    )
    for item in items:
        path = os.path.basename(str(getattr(item, "fspath", "")))
        if path.startswith("contrast"):
            item.add_marker(skip_contrast)
