"""body_forms 示例的项目函数。

只需要一个函数：把 mock httpbin 的地址交给用例（与 ``examples/httpbin`` 的约定一致）。
地址来自 ``interfacetester.utils.HTTP_BIN_URL``，跟随 ``INTERFACETESTER_HTTP_BIN_PORT``
环境变量——80/443 是特权端口（被占用/无权限绑定时），改端口即可，用例不用动。
"""
from interfacetester.utils import HTTP_BIN_URL


def get_httpbin_server():
    return HTTP_BIN_URL
