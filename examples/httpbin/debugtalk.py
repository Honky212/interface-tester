import os
import random
import string
import time
import uuid

from urllib.parse import urlparse

from loguru import logger

from interfacetester.utils import HTTP_BIN_URL


def get_httpbin_server():
    return HTTP_BIN_URL


def get_httpbin_host():
    """mock 服务在 Host 头里的样子（与 requests 实际发出的 Host 头一致）。

    NOTICE: 用例里别写死 ``"127.0.0.1"``——mock 端口可配置（见 docs/ci/README.md），
    端口一变 Host 头就变成 ``127.0.0.1:8000``，写死的断言会假失败。
    这里**不能直接用 ``urlparse(...).netloc``**：URL 写成 ``http://127.0.0.1:80`` 时
    netloc 是 ``127.0.0.1:80``，而 requests 对「scheme 默认端口」发出的 Host 是
    ``127.0.0.1``（实测：直接返回 netloc 会让 basic.yml 假失败）。
    """
    parsed = urlparse(HTTP_BIN_URL)
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port in (None, default_port):
        return parsed.hostname
    return f"{parsed.hostname}:{parsed.port}"


def setup_testcase(variables):
    logger.info(f"setup_testcase, variables: {variables}")
    variables["request_id_prefix"] = str(int(time.time()))


def teardown_testcase():
    logger.info("teardown_testcase.")


def setup_teststep(request, variables):
    logger.info(f"setup_teststep, request: {request}, variables: {variables}")
    request.setdefault("headers", {})
    request_id_prefix = variables["request_id_prefix"]
    request["headers"]["interfacetester-Request-ID"] = request_id_prefix + "-" + str(uuid.uuid4())


def teardown_teststep(response):
    logger.info(f"teardown_teststep, response status code: {response.status_code}")


def sum_two(m, n):
    return m + n


def sum_status_code(check_value, expect_value, message=""):
    """自定义断言算子（custom comparator）：状态码各位数字之和 == 期望值。

    NOTICE: 自定义算子必须满足框架的调用约定 ——
    `ResponseObject.validate` 会调用 `sum_status_code(check_value, expect_value, message)`，
    并且**忽略返回值**，所以判定必须靠 `assert` 抛出 AssertionError，
    而不是 `return True/False`（签名见 make.ensure_known_comparators 的报错提示）。

    YAML 写法：`validate: - sum_status_code: ["status_code", 2]`（200 → 2+0+0 = 2）

    这也是**「validate_script（Python 脚本断言）不受支持」时的官方替代路径**：
    复杂断言写成本函数这样的 debugtalk.py 算子即可，不必改框架。
    """
    sum_value = 0
    for digit in str(check_value):
        sum_value += int(digit)

    assert sum_value == expect_value, (
        message
        or f"sum of digits of {check_value} is {sum_value}, expected {expect_value}"
    )


def is_status_code_200(status_code):
    return status_code == 200


os.environ["TEST_ENV"] = "PRODUCTION"


def skip_test_in_production_env():
    """skip this test in production environment"""
    return os.environ["TEST_ENV"] == "PRODUCTION"


def get_user_agent():
    return ["iOS/10.1", "iOS/10.2"]


def gen_app_version():
    return [{"app_version": "2.8.5"}, {"app_version": "2.8.6"}]


def get_account():
    return [
        {"username": "user1", "password": "111111"},
        {"username": "user2", "password": "222222"},
    ]


def get_account_in_tuple():
    return [("user1", "111111"), ("user2", "222222")]


def gen_random_string(str_len):
    random_char_list = []
    for _ in range(str_len):
        random_char = random.choice(string.ascii_letters + string.digits)
        random_char_list.append(random_char)

    random_string = "".join(random_char_list)
    return random_string


def setup_hook_add_kwargs(request):
    request["key"] = "value"


def setup_hook_remove_kwargs(request):
    request.pop("key")


def teardown_hook_sleep_N_secs(response, n_secs):
    """sleep n seconds after request"""
    if response.status_code == 200:
        time.sleep(0.1)
    else:
        time.sleep(n_secs)


def hook_print(msg):
    print(msg)


def modify_request_json(request, os_platform):
    request["json"]["os_platform"] = os_platform


def setup_hook_httpntlmauth(request):
    if "httpntlmauth" in request:
        from requests_ntlm import HttpNtlmAuth

        auth_account = request.pop("httpntlmauth")
        request["auth"] = HttpNtlmAuth(
            auth_account["username"], auth_account["password"]
        )


def alter_response(response):
    response.status_code = 500
    response.headers["Content-Type"] = "html/text"
    response.body["headers"]["Host"] = "127.0.0.1:8888"
    response.new_attribute = "new_attribute_value"
    response.new_attribute_dict = {"key": 123}


def alter_response_302(response):
    response.status_code = 500
    response.headers["Content-Type"] = "html/text"
    response.text = "abcdef"
    response.new_attribute = "new_attribute_value"
    response.new_attribute_dict = {"key": 123}


def alter_response_error(response):
    # NameError
    not_defined_variable


def gen_variables():
    return {"var_a": 1, "var_b": 2}
