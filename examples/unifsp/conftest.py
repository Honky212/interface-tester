# -*- coding: utf-8 -*-
"""
UniFSP（统一文件服务平台）接口测试 - pytest 共享夹具。

作用：多环境切换 + 运行环境信息打印。

如何生效：
- 本文件放在用例目录 examples/unifsp/ 下，pytest 会自动加载；
- 通过「环境变量」这条链路影响 YAML 用例：
    conftest 覆盖 os.environ["UNIFSP_BASE_URL"]
    → debugtalk.py 的 api_base()/token_url() 读取它
    → YAML 里的 ${api_base()}、${token_url()} 拿到切换后的值。
- 因此【YAML 用例无需任何修改】，只是多了一个 --env 开关。

用法：
    hrun examples/unifsp/auth_login.yml --env=prod   # 切到生产环境
    hrun examples/unifsp/                              # 不传 --env 则用 .env 配置

说明：
- 框架已内置 OAuth2 Client Credentials 自动认证 + token 缓存，
  这里【不再重复】造 token fixture，避免和 config.oauth2 冲突；
- 测试目录由各用例用 ${test_dir()} 动态生成（带时间戳），
  无需统一的目录准备/清理 fixture。
"""
import os

import pytest

# 各环境 API 根地址（--env 优先于 .env；值可按实际环境调整）
ENV_BASE_URL = {
    "test": "http://unifsp-test.example.com/index",
    "prod": "http://unifsp.example.com/index",
}


def pytest_addoption(parser):
    parser.addoption(
        "--env",
        action="store",
        default=None,
        help="运行环境：test / prod（不传则使用 .env 里的 UNIFSP_BASE_URL）",
    )


@pytest.fixture(scope="session", autouse=True)
def switch_env(pytestconfig):
    """按 --env 切换 UNIFSP_BASE_URL，并打印当前环境。"""
    from loguru import logger

    env = pytestconfig.getoption("--env")
    if env:
        if env not in ENV_BASE_URL:
            raise ValueError(f"未知环境: {env}，可选：{sorted(ENV_BASE_URL)}")
        os.environ["UNIFSP_BASE_URL"] = ENV_BASE_URL[env]

    logger.info(f"[conftest] 运行环境 base_url = {os.environ.get('UNIFSP_BASE_URL')}")
    yield
