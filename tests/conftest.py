"""pytest 全局装置：为测试启动本地 mock 服务，让 tests/ 不依赖外网。

- 启动逻辑在 ``tests/mock_server.py``（HTTP 80 + HTTPS 443 的 httpbin 兼容子集）；
- 必须在这里（conftest 导入阶段、收集测试之前）启动：这样测试模块才能在 import 期用
  ``mock_server.https_available()`` 决定是否 skip 依赖 https 的用例；
- 端口被占用 / 取不到自签名证书时不会抛异常，只打印说明，相关用例按需 skip。
"""

from mock_server import start_servers, startup_notes

# 导入期即启动（见模块 docstring），启动结果通过 startup_notes() 读取
start_servers()


def pytest_configure(config):
    """本地 https mock 用自签名证书（用例 verify=False）——统一忽略相关告警。

    NOTICE: pytest 每个用例都会重置 warnings 过滤器，所以只能在这里加；
    否则每个 https 用例都会打印一次 InsecureRequestWarning。
    """
    config.addinivalue_line(
        "filterwarnings",
        "ignore:Unverified HTTPS request:urllib3.exceptions.InsecureRequestWarning",
    )


def pytest_report_header(config):
    """把本地 mock 的启动情况打到测试头部（排查环境问题用）。

    NOTICE: 不能用 pytest_configure + terminalreporter.write_line —— 该阶段
    terminalreporter 插件可能尚未注册；且 `-q` 模式下整个头部都会被省略，
    想看这段说明请去掉 `-q`（见 docs/缺陷修复日志0916-7.md 复核方式第 8 条）。
    """
    return [f"[mock-httpbin] {note}" for note in startup_notes()]
