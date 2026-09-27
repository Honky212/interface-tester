"""导入器包：把外部资产（curl / HAR / Postman / OpenAPI）转换成标准 InterfaceTester YAML。

设计约束见 `docs/convert/README.md`；这里只做转出，**不碰运行内核**：
生成的 YAML 由现有 `hmake` 链路渲染成 pytest 用例。

四个源都已交付（`IMPLEMENTED_SOURCES`）—— NOTICE（批次 9-1）：本行原先写着
「已交付（P3-a）：curl、HAR。计划中（P3-b/P3-c）：postman、openapi」，
而 `IMPLEMENTED_SOURCES` 早已是四个全在，属「docstring 与代码常量各说一套」的过期口径。
判断某个源能不能用，**以 `IMPLEMENTED_SOURCES` 为准**（CLI 的 `--from` 取值也由它把关）。
"""

from interfacetester.converters.emit_yaml import (  # noqa: F401
    build_case_dict,
    emit_case,
    validate_emitted_case,
)
from interfacetester.converters.ir import IRCase, IRStep  # noqa: F401

# 支持的源 → 人类可读说明；CLI 的 `--from` 取值就来自这里
SUPPORTED_SOURCES = {
    "curl": "curl 命令（「复制为 cURL」）",
    "har": "HAR 1.2（浏览器 DevTools 导出）",
    "postman": "Postman Collection v2.1（导出为 v2.1 的集合文件）",
    "openapi": "OpenAPI 3.x / Swagger 2.0 契约（契约 → 骨架用例）",
}

IMPLEMENTED_SOURCES = ("curl", "har", "postman", "openapi")

__all__ = [
    "IRCase",
    "IRStep",
    "IMPLEMENTED_SOURCES",
    "SUPPORTED_SOURCES",
    "build_case_dict",
    "emit_case",
    "validate_emitted_case",
]
