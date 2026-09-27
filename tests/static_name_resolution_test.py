"""0919-1 / L30：静态名字解析闸门（补上「CI 没有跑 lint」这个短板）。

## 为什么需要这条闸门

`项目存在的问题0919.txt` §A.1 记的 `cli.py` 缺 `Dict` import 是这样一类缺陷：

```python
from typing import List, Text          # ← 没有 Dict
...
    used_yaml_targets: Dict[str, str] = {}   # cli.py:236
```

**局部变量的注解在运行期不求值**，所以它不崩、测不出来；但任何静态检查工具都会报 F821。
它真正的价值不是这一个 bug，而是**暴露了 CI 里没有任何名字解析检查**——
pyproject 无 ruff/mypy/pre-commit，两个 CI 配置里也没有 lint 步骤。

本闸门用标准库 `symtable` 做最小可用版本：对每个作用域，凡是 `is_global()` 但在
模块级从未绑定、也不是内置名的名字，就是"引用了不存在的全局名"。
这不是完整 lint（不做未使用变量、不查类型），只覆盖"名字根本不存在"这一类。

## 为什么只认 `is_global()`、不认 `is_free()`

`is_free()` 会命中**闭包变量**（外层函数里的局部名），那些在运行期是合法的，
把它们当报错会产生大量假报——闸门一旦有假报就会被绕过，等于没有。
闭包变量在词法上都能解析，所以漏掉它们不会漏掉真问题。

## 自检

闸门本身也可能写错（例如过滤条件写宽了，永远返回空列表）。
`test_gate_detects_an_injected_undefined_name` 用一段**故意**有未定义名字的源码
做正向自检：闸门必须报出来。没这条，"一直绿"可能只是闸门坏了。
"""

import builtins
import os
import symtable
import unittest

PACKAGE_DIR = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "interfacetester"
)

# 模块级由解释器隐式提供的名字：symtable 不会把它们算作「模块级绑定」，
# 但运行期一定存在，不算未定义。
_IMPLICIT_MODULE_GLOBALS = frozenset(
    {
        "__file__",
        "__name__",
        "__doc__",
        "__package__",
        "__spec__",
        "__loader__",
        "__builtins__",
        "__path__",
        "__cached__",
        "__annotations__",
        "__dict__",
    }
)


def find_undefined_globals(source: str, filename: str):
    """返回 [(作用域名, 名字)]：引用了不存在全局名的地方。

    判定规则（见模块 docstring）：
      - 只看 `is_global()`（显式全局引用），不看 `is_free()`（闭包变量，合法）；
      - 模块级绑定 = 赋值 / import / def / class（`is_namespace()`）；
      - 再排除内置名与解释器隐式提供的模块属性。
    """
    table = symtable.symtable(source, filename, "exec")

    module_bound = {
        symbol.get_name()
        for symbol in table.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }

    problems = []

    def walk(current):
        for symbol in current.get_symbols():
            name = symbol.get_name()
            if not symbol.is_global():
                continue
            if name in module_bound or name in _IMPLICIT_MODULE_GLOBALS:
                continue
            if hasattr(builtins, name):
                continue
            problems.append((current.get_name(), name))

        for child in current.get_children():
            walk(child)

    walk(table)
    return problems


def _iter_package_sources():
    for dirpath, dirnames, filenames in os.walk(PACKAGE_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                path = os.path.join(dirpath, filename)
                yield path


class TestNoUndefinedGlobalNames(unittest.TestCase):
    """`interfacetester/` 里不得出现「引用了不存在的全局名」（F821 那一类）。"""

    def test_package_has_no_undefined_global_names(self):
        failures = []
        scanned = 0

        for path in _iter_package_sources():
            scanned += 1
            with open(path, encoding="utf-8") as f:
                source = f.read()

            for scope, name in find_undefined_globals(source, path):
                rel = os.path.relpath(path, os.path.dirname(PACKAGE_DIR))
                failures.append(f"{rel} :: {scope}() -> {name!r}")

        self.assertGreater(scanned, 30, "扫描到的文件太少，闸门可能没真的在遍历源码")
        self.assertEqual(
            failures,
            [],
            "发现引用了不存在的全局名（F821）——这类问题运行期**不会报错**"
            "（局部变量注解不求值），只会让静态检查变红：\n  " + "\n  ".join(failures),
        )

    def test_gate_detects_an_injected_undefined_name(self):
        """闸门自检：注入一个未定义的名字，必须被报出来（否则"一直绿"没有意义）。"""
        broken = "def f():\n    x: MissingName[str] = {}\n    return x\n"
        problems = find_undefined_globals(broken, "<injected>")
        self.assertEqual([name for _scope, name in problems], ["MissingName"])

    def test_gate_does_not_flag_closure_or_implicit_names(self):
        """反向自检：合法的闭包变量与解释器隐式名**不能**被误报。

        假报是闸门的头号死因——一旦有假报，维护者就会绕过它。
        """
        legit = (
            "import os\n"
            "\n"
            "CONST = 1\n"
            "\n"
            "def outer():\n"
            "    captured = 2\n"
            "    def inner():\n"
            "        return captured + CONST + os.sep\n"
            "    return inner\n"
            "\n"
            "HERE = __file__\n"
        )
        self.assertEqual(find_undefined_globals(legit, "<legit>"), [])
