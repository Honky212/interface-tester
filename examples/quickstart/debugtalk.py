"""最小用例工程的「门牌号」+ 一个示例函数。

本目录是 [`../../docs/使用说明.md`](../../docs/使用说明.md) 第二节
「从零搭一个用例工程」的**可运行版本**——照着那一节做出来的样子就是这里。

这个文件存在的**首要原因不是放函数，而是定位项目根（RootDir）**：
框架从用例路径向上查找 `debugtalk.py`，**找到的那个目录就是项目根**，
`schemas/xxx.json`、`data/xx.csv` 这类相对路径都相对它解析。
没有它，RootDir 会退化成「你敲命令时所在的目录」——换个目录跑就报「文件不存在」。
"""

import os

# 公开的测试服务：能上外网时用它。
# 换成你自己的接口地址也可以——唯一约束是「这个地址能返回 200」；
# 内网/离线环境请改成本地可达地址（在框架仓库里可以先起
# `python tests/mock_server.py`，再用 http://127.0.0.1:8000）。
DEFAULT_BASE_URL = "https://httpbin.org"


def base_url():
    """用例里写 `${base_url()}`。

    优先读环境变量 `BASE_URL`（通常来自同目录的 `.env`），
    没有时回落到 `DEFAULT_BASE_URL`——所以**不做任何准备**也能跑通
    `hrun hello_func.yml`。
    """
    return os.environ.get("BASE_URL") or DEFAULT_BASE_URL
