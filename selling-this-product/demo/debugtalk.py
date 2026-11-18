"""demo 项目的「门牌号」+ 一个自定义函数。

两个作用（现场讲这两个就够了）：
  1. **门牌号**：框架从用例路径向上找 `debugtalk.py`，**找到的那个目录就是项目根（RootDir）**——
     `schemas/user.json` 这类相对路径都以它为准。没有它，RootDir 会退化成"你敲命令时所在的目录"。
  2. **自定义函数库**：这里定义的函数，YAML 里用 `${函数名(参数)}` 直接调用。
     签名/加密/HMAC/造数……企业里那些"框架不支持"的东西，绝大多数写在这里就够了。
"""

import time


def trace_id(prefix="TRACE"):
    """给每个请求打一个可追溯的链路号（YAML 里写 `${trace_id()}` 或 `${trace_id(REQ)}`）。"""
    return f"{prefix}-{int(time.time() * 1000)}"
