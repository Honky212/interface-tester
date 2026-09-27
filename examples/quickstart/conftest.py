"""示例工程夹具：让 `contrast_*` 对照用例**默认跳过**，需要时用环境变量打开。

为什么需要这个文件
------------------
`contrast_*.yml` 是**故意失败**的演示——教你看「一条用例红了长什么样」。
如果它们跟着默认一起跑，这个示例就永远是红的、当不成「跑通了」的样板。
所以这里按**环境变量**（不是「有没有被显式指定」）决定跳不跳：

    # Windows PowerShell
    $env:QUICKSTART_RUN_CONTRAST = "1"; hrun examples/quickstart
    # macOS / Linux
    QUICKSTART_RUN_CONTRAST=1 hrun examples/quickstart

NOTICE（为什么用环境变量、而不是「是否单独指定了该文件」）：
按文件名跳的规则对「单独跑这个文件」同样生效（`hrun contrast_assert_fail.yml`
也会被 skip），那样文档里写的命令就跑不出效果。

NOTICE（这正好是《docs/使用说明.md》第四节讲的夹具写法）：
框架生成的用例方法签名固定（`def test_start(self)`），普通 fixture **注入不进去**；
而 `pytest_collection_modifyitems` 是 pytest 的钩子，不受这个限制——所以
「按条件 skip 用例」这类需求就写在这里。
"""

import os

import pytest

# 打开对照演示的环境变量（1 / true / yes / on 任一即可，大小写不敏感）
CONTRAST_ENV = "QUICKSTART_RUN_CONTRAST"
_TRUTHY = ("1", "true", "yes", "on")


def contrast_enabled() -> bool:
    return (os.environ.get(CONTRAST_ENV) or "").strip().lower() in _TRUTHY


def pytest_collection_modifyitems(config, items):
    """把 `contrast_*` 开头的用例默认标记为 skip（除非环境变量打开）。"""
    if contrast_enabled():
        return

    skip_contrast = pytest.mark.skip(
        reason=(
            "对照演示用例（故意失败）：默认跳过。要看效果就设 "
            f"{CONTRAST_ENV}=1 再跑，见 examples/quickstart/README.md"
        )
    )
    for item in items:
        name = os.path.basename(str(getattr(item, "fspath", "")))
        if name.startswith("contrast"):
            item.add_marker(skip_contrast)
