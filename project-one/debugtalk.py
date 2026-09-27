"""project-one 的项目文件：debugtalk.py

作用：
1. 这是「门牌号」——框架靠它识别「project-one 就是一个项目根（RootDir）」。
   schemas/、data/ 等相对路径都会以这个文件所在目录为基准解析。
2. 在这里定义自定义函数，YAML 用例里用 ${函数名(参数)} 调用。

暂时不需要自定义函数的话，这个文件留空也没问题（关键是「文件存在」）。
"""


def gen_token(prefix: str = "tk") -> str:
    """示例自定义函数：生成一个 token。YAML 里用 ${gen_token(abc)} 调用。"""
    import time

    return f"{prefix}-{int(time.time())}"
