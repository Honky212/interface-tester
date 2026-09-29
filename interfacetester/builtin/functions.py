"""
Built-in functions used in YAML/JSON testcases.
"""

import datetime
import random
import string
import time

from interfacetester import exceptions
from interfacetester.utils import ensure_int_value, ensure_timeout_value


def gen_random_string(str_len):
    """generate random string with specified length

    NOTICE（批次 8 / **L2**）：长度先过 `ensure_int_value` —— `${gen_random_string($len)}`
    里的 `$len` 来自 `.env` / CSV / `${ENV(...)}` 时**永远是字符串**，修复前
    `range("8")` 直接抛 `TypeError: 'str' object cannot be interpreted as an integer`
    （报错里看不出是"长度"，更看不出是"值其实是字符串"）。
    口径与 `utils.ensure_timeout_value` 一致：变量来的数字要转。

    顺带把**负数**从"静默返回空串"改成响亮报错（`range(-3)` 得到 `""`，
    今天会让"随机串"悄悄变成空值）；`0` 仍然返回空串（既有行为不变）。
    """
    length = ensure_int_value(str_len, "random string length", minimum=0)
    return "".join(
        random.choice(string.ascii_letters + string.digits) for _ in range(length)
    )


def get_timestamp(str_len=13):
    """get timestamp string, length can only between 1 and 16

    NOTICE（批次 8 / **L2**）：修复前判据是 `isinstance(str_len, int) and 0 < str_len < 17`，
    于是 `${get_timestamp($len)}`（`len` 从变量/ENV 来）**一律**报
    `timestamp length can only between 0 and 16.` —— 而 `16` 明明合法，
    文案还写成 "between 0 and 16"（判据其实是 1~16）。这把人引向"改长度"，
    真正的病因是"值是字符串"。现在先转 int，报错也带上实际值与真实区间。

    NOTICE（0921 / **长度静默缩水**）：源串从 `str(time.time())` 改成定宽的
    `f"{time.time():.6f}"`。修复前源串是浮点的 `repr`，**位数会浮动** ——
    实测 40 万次采样：17 位 74.0% / 16 位 24.4% / **15 位 1.6%**，
    而小数末位为 0 时更短（`str(1789996932.0)` 只有 11 位）。于是
    `get_timestamp(16)` 会有约 1~2% 的概率**静默返回更短的串**
    （实测抓到 `str(time.time())='1789996932.4137'` → 只返回 **14 位**）。

    这不是"边界"，是会随机变红的抽风源：`tests/parser_test.py` 的
    L2 用例当年就踩到过（"第一版没冻结，注入复跑时同一组注入两次跑出不同的
    红灯集合"），当时靠 `mock.patch("time.time")` 冻结时间把**用例**糊过去了，
    根因一直留着。现在源串恒为 10 位整数秒 + 6 位微秒 = **16 位定宽**，
    `[:length]` 因此对任何 `length <= 16` 都**保证**返回请求的长度
    （末位补 0 也是数字，不是截断出的残缺串）。
    """
    length = ensure_int_value(str_len, "timestamp length", minimum=1, maximum=16)
    # 定宽来源：`%.6f` 恒为 "整数秒.6位微秒"，去掉小数点即 16 位数字
    return f"{time.time():.6f}".replace(".", "")[:length]


def get_current_date(fmt="%Y-%m-%d"):
    """get current date, default format is %Y-%m-%d"""
    return datetime.datetime.now().strftime(fmt)


def sleep(n_secs):
    """sleep n seconds

    NOTICE（0921 / **L2 补漏**）：`sleep` 是批次 8 / L2 那一轮的**漏网之鱼**——
    同模块的 `gen_random_string` / `get_timestamp` 都过了 `ensure_int_value`，
    只有它没加固。修复前实测（`.tmp_audit/verify0921/`）：

        ${sleep($wait)}  wait='2'（来自 .env / CSV / ${ENV(...)}）→
            TypeError: 'str' object cannot be interpreted as an integer
        ${sleep($wait)}  wait=2   → 正常
        ${sleep(${ENV(WAIT_SECS)})} ENV='0' → 正常

    这正是 L2 想消灭的那类「报错看不出根因」：写的是"等待时间"，报的是
    Python 内部的 TypeError，用户会去查变量名而不是去看"值是字符串"。

    **刻意不用 `ensure_int_value`**（尽管 L2 的两个兄弟函数用的是它）：
    `time.sleep` 本来就接受 **float**，而本仓有用例写 `${sleep(0.05)}`
    （`tests/save_tests_observability_test.py::test_failing_case_duration_is_not_zero`
    靠它造可测耗时）。`ensure_int_value` 会拒绝 `0.05`（"非整数的小数会被截断"），
    照 L2 的字面口径改会把那条既有用例直接弄红。这里改用与 `ensure_timeout_value`
    同一套（收 int/float/数字字符串、拒 bool），再补一个**负数**检查——
    `time.sleep(-1)` 的 `ValueError: sleep length must be non-negative`
    同样看不出是"等待时间"写错了。
    """
    seconds = ensure_timeout_value(n_secs, "sleep seconds")
    if seconds < 0:
        raise exceptions.ParamsError(
            f"invalid sleep seconds: {seconds}（应不小于 0）"
        )
    time.sleep(seconds)
