# demo 资产（40 分钟演示专用，离线自包含）

> 本目录的东西**只服务于演示**：不依赖外网、不依赖数据库、不依赖任何已启动的服务。
> 主脚本见 [`../10-Demo脚本-40分钟逐字稿.md`](../10-Demo脚本-40分钟逐字稿.md)。

## 里面有什么

| 文件 | 作用 | 是产物吗 |
| --- | --- | --- |
| `mock_server.py` | **本地 mock**（127.0.0.1:18080）：`POST /api/login` 发 token、`GET /api/user/info` 校验 Bearer、`GET /get` 仿 httpbin | 不是（demo 基础设施） |
| `debugtalk.py` | **项目门牌号**（定位 RootDir）+ 自定义函数 `trace_id()` | 不是 |
| `login.yml` | 演示用例：登录拿 token → 带 token 查询 → 契约断言 | 不是（**要改就改它**） |
| `schemas/user.json` | `jsonschema_match` 用的响应契约 | 不是 |
| `bad_comparator.yml` | **故意写错**：`equall`（算子名多一个 l）→ 演示生成期闸门 | 不是 |
| `bad_builtin.yml` | **故意写错**：算子写成 Python 内置名 `print` → 演示最隐蔽的假通过形态 | 不是 |
| `preflight.ps1` | 现场预检脚本（T-30 跑一次） | 不是 |
| `login_test.py` / `__init__.py` / `logs/` / `__pycache__/` | `hmake`/`hrun` **自动生成** | ✅ 产物，**不要手改** |

## 怎么跑

```powershell
cd D:\tester
chcp 65001 | Out-Null                 # 控制台中文显示（不设会乱码）
$env:PYTHONIOENCODING = "utf-8"

# 终端 A：起 mock，保持运行
python selling-this-product\demo\mock_server.py

# 终端 B：跑用例
hrun selling-this-product\demo\login.yml --html=reports\demo.html --self-contained-html
#   实测：1 passed；报告 reports\demo.html
```

## 现场预检

```powershell
powershell -ExecutionPolicy Bypass -File selling-this-product\demo\preflight.ps1
```

会检查：Python 版本、四个命令入口（`hrun`/`hmake`/`hconvert`/`haify`）、demo 资产是否齐全、
**18080 端口是否空闲**、AI 配置状态、以及一次 0.6 秒的 pytest 冒烟。

> ★**让它原样打印，不要接管道**（`| Select-Object` / `| Select-String`）：Windows PowerShell 5.1 下管道会丢掉走 stderr 的日志行，
> 或让中文显示成乱码（本目录实测踩到）。同理适用于 `hconvert` 与 `hrun`。

## 常见现场问题

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `1 failed`，日志里是 `Max retries exceeded ... WinError 10061` | **mock 没起**（或端口不是 18080） | 起 mock；或改 `login.yml` 的 `base_url` |
| 中文全是乱码 | 控制台编码 | `chcp 65001` + `$env:PYTHONIOENCODING='utf-8'` |
| 18080 被占用 | 上次的 mock 没关 | `Get-Process python \| Stop-Process`（确认没别的重要进程） |
| `hrun` 找不到 | 没装内核 | `python -m pip install -e .` |
| `haify` 找不到 | 没装 AI 包（**只装内核不会有**） | `python -m pip install -e interfacetester_ai --no-build-isolation` |

## 维护注意（改这个目录的人看）

1. **`preflight.ps1` 必须保存为「UTF-8 带 BOM」**：Windows PowerShell 5.1 读无 BOM 的 UTF-8 脚本会把中文当 GBK 解析，**直接报语法错误**（本稿实测踩到）。
   若用编辑器重存后脚本报莫名其妙的解析错误，用这条命令补回 BOM：
   ```powershell
   $p = 'selling-this-product\demo\preflight.ps1'
   $c = [System.IO.File]::ReadAllText($p, [System.Text.UTF8Encoding]::new($false))
   [System.IO.File]::WriteAllText($p, $c, [System.Text.UTF8Encoding]::new($true))
   ```
2. **`login_test.py` / `__init__.py` 是产物**，可以被 `hmake`/`hrun` 重新生成；`logs/`、`__pycache__/` 是运行时垃圾，交付前可删。
3. **`mock_server.py` 端口固定 18080**：改端口时记得同步改 `login.yml` 的 `base_url` 与 `README`/逐字稿里的说明。
