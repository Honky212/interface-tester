"""示例工程夹具：会话级拉起本地 SOAP mock（离线，不依赖外网）。

三条硬约束（与 `examples/data_management/conftest.py` 同源，决定这里只能这么写）：

1. 框架生成的用例方法签名是固定的（`def test_start(self)`）→ **只有 `autouse=True` 的夹具生效**；
2. `autouse` 夹具**不能给用例传值** → mock 地址只能写 `os.environ`，由 `debugtalk.py` 的
   `soap_base_url()` 读（用例里写 `${soap_base_url()}`）；
3. 端口用 `0`（系统分配空闲端口）→ CI 上不会撞端口；想固定端口就设 `SOAP_MOCK_PORT`。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mock_soap import serve  # noqa: E402

BASE_URL_VAR = "SOAP_BASE_URL"


@pytest.fixture(autouse=True, scope="session")
def soap_mock():
    httpd, base_url = serve(port=int(os.environ.get("SOAP_MOCK_PORT") or 0))
    os.environ[BASE_URL_VAR] = base_url
    print(f"[soap] 本地 mock 已就绪：{base_url}（SOAP_MOCK_PORT 可固定端口）", flush=True)
    try:
        yield
    finally:
        httpd.shutdown()
        httpd.server_close()


def pytest_collection_modifyitems(config, items):
    """`contrast_*` 文件是**故意失败/故意假通过**的对照演示 → 默认跳过。

    想看 A0（只用字符串断言）在 XML 上的真实行为，用环境变量显式打开：

    ```bash
    # Windows PowerShell
    $env:SOAP_RUN_CONTRAST=1; hrun examples/soap/contrast_string_assert.yml
    # Linux / macOS
    SOAP_RUN_CONTRAST=1 hrun examples/soap/contrast_string_assert.yml
    ```

    NOTICE: 这里**按环境变量**判断，而不是「是否被显式指定」——因为按文件名跳的规则对
    「单独跑这个文件」同样生效（`hrun xxx.yml` 也会被 skip），那样文档里写的命令就跑不出效果。
    """
    if (os.environ.get("SOAP_RUN_CONTRAST") or "").strip().lower() in ("1", "true", "yes", "on"):
        return

    skip_contrast = pytest.mark.skip(
        reason="对照演示用例（故意假通过）：设 SOAP_RUN_CONTRAST=1 再单独跑本例，见 examples/soap/README.md"
    )
    for item in items:
        path = os.path.basename(str(getattr(item, "fspath", "")))
        if path.startswith("contrast"):
            item.add_marker(skip_contrast)
