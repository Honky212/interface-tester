# 现场预检脚本（40 分钟 demo 专用）
#
# 用法（T-30 分钟跑一次，T-5 分钟再跑一次）：
#     powershell -File selling-this-product\demo\preflight.ps1
# 或：
#     .\selling-this-product\demo\preflight.ps1
#
# 它只做只读检查：不装包、不写文件、不起服务。

$ErrorActionPreference = "Continue"
chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$script:fail = 0
function Ok($m)   { Write-Host "  [OK]   $m" -ForegroundColor Green }
function Bad($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red; $script:fail++ }
function Warn($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }

function Test-PortBusy($port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try   { $null = $client.Connect("127.0.0.1", $port); return $true }
    catch { return $false }
    finally { $client.Dispose() }
}

Write-Host "`n=== InterfaceTester demo 预检 =====================" -ForegroundColor Cyan

Write-Host "`n1) Python 与代码位置"
try {
    $pv = (python -V 2>&1) -join " "
    Ok "$pv"
    Ok (python -c "import interfacetester, interfacetester_ai; print('kernel', interfacetester.__version__, '/ ai', interfacetester_ai.__version__)" 2>&1)
} catch { Bad "python 不可用" }

Write-Host "`n2) 四个命令入口（都要能打出 5.0.1 / AI 版本）"
foreach ($c in @("hrun", "hmake", "hconvert", "haify")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
        # 不把子进程输出捕进变量：直接打印，避免 Windows 控制台解码差异导致中文变乱码
        Write-Host -NoNewline "  [OK]   $c -V  ->  "
        & $c -V
    } else {
        Bad "$c 不存在：pip install -e . / pip install -e interfacetester_ai --no-build-isolation"
    }
}

Write-Host "`n3) demo 资产是否齐全"
$assets = @(
    "selling-this-product\demo\login.yml",
    "selling-this-product\demo\mock_server.py",
    "selling-this-product\demo\debugtalk.py",
    "selling-this-product\demo\schemas\user.json",
    "selling-this-product\demo\bad_comparator.yml",
    "selling-this-product\demo\bad_builtin.yml",
    "examples\data_management\01_create_and_read.yml",
    "examples\quickstart\contrast_false_pass.yml",
    "examples\quickstart\contrast_assert_fail.yml",
    "examples\data\curl\curl_examples.txt"
)
foreach ($a in $assets) {
    if (Test-Path $a) { Ok $a } else { Bad "缺文件：$a" }
}

Write-Host "`n4) 端口（demo mock 用 18080；必须空闲）"
if (Test-PortBusy 18080) {
    Warn "18080 已被占用 —— 可能上次的 mock 还在跑：Get-Process python | Stop-Process；或改用别的端口（同时改 login.yml）"
} else { Ok "18080 空闲" }
if (Test-PortBusy 8765) { Warn "8765 已被占用（可选步骤 haify serve 的默认端口，不影响主流程）" } else { Ok "8765 空闲（haify serve 备用）" }

Write-Host "`n5) AI 配置状态（决定第 6 段怎么讲）"
if ($env:INTERFACETESTER_AI_BASE_URL -and $env:INTERFACETESTER_AI_MODEL) {
    Ok "已配模型：$($env:INTERFACETESTER_AI_MODEL) @ $($env:INTERFACETESTER_AI_BASE_URL)"
    Warn "若要做「没配模型 → 退出码 2」的演示，先临时清空：Remove-Item Env:INTERFACETESTER_AI_BASE_URL, Env:INTERFACETESTER_AI_MODEL"
} else {
    Ok "未配模型 —— 第 6 段默认走「确定性通路 + 退出码 2 安全闸门」，不依赖模型"
}

Write-Host "`n6) 快速冒烟（0.6 秒级，跑得动说明依赖齐）"
try {
    $r = (python -m pytest tests\response_test.py tests\comparators_test.py tests\parser_test.py -q -p no:cacheprovider 2>&1 | Select-Object -Last 1)
    Ok "pytest 冒烟：$r"
} catch { Bad "pytest 冒烟失败（缺依赖？）" }

Write-Host "`n=== 结论 ===========================================" -ForegroundColor Cyan
if ($script:fail -eq 0) {
    Write-Host "全部通过 —— 可以开讲。记得：两个终端（mock + 命令），字号调大。" -ForegroundColor Green
} else {
    Write-Host "有 $script:fail 项未通过 —— **先修**，不要硬上。" -ForegroundColor Red
}
Write-Host ""
