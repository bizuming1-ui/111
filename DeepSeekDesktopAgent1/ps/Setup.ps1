#Requires -Version 7.0
<#
  Setup.ps1 —— 一键部署 DeepSeekDesktopAgent（目录：D:\DeepSeekDesktopAgent）
  做的事：解除文件锁定 → 检查 Python → 检查/安装 agent-desktop → 建 venv → 跑离线回归测试 → 写入 $PROFILE
  用法：  pwsh -File D:\DeepSeekDesktopAgent\ps\Setup.ps1
#>
[CmdletBinding()]
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$SkipProfile
)
$ErrorActionPreference = 'Stop'
function Step([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Pass([string]$m) { Write-Host "    [PASS] $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "    [WARN] $m" -ForegroundColor Yellow }

if (-not $IsWindows) { throw '本系统控制的是 Windows 桌面，请在 Windows 上运行。' }
Write-Host "PowerShell $($PSVersionTable.PSVersion)  目标目录 $Root"

Step '1/7 检查目录完整性'
foreach ($f in 'agent.py', 'da_core.py', 'da_policy.py', 'da_tools.py', 'requirements.txt', 'tests\test_all.py', 'tests\mock_agent_desktop.py', 'ps\DeskAgent.ps1') {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $f))) { throw "缺少文件：$f（请重新运行安装脚本）" }
}
Pass '所有文件齐全'

Step '2/7 解除下载文件的 Mark-of-the-Web 锁定'
Get-ChildItem -LiteralPath $Root -Recurse -File -Include *.ps1, *.py | Unblock-File
Pass 'Unblock-File 完成'

Step '3/7 检查 Python（需要 3.10+）'
function Find-Python {
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $cands += , @('py', '-3') }
    foreach ($n in 'python', 'python3') { if (Get-Command $n -ErrorAction SilentlyContinue) { $cands += , @($n) } }
    foreach ($c in $cands) {
        try {
            $exe = $c[0]; $pre = @(); if ($c.Count -gt 1) { $pre = $c[1..($c.Count - 1)] }
            $out = & $exe @pre -c "import sys;print('OK' if sys.version_info>=(3,10) else 'OLD');print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out[0] -eq 'OK') { return $out[1] }
        } catch { }
    }
    return $null
}
$py = Find-Python
if (-not $py) { throw '没有找到 Python 3.10+（或只有 Microsoft Store 占位程序）。请运行：winget install Python.Python.3.12  然后重开 pwsh 再运行本脚本。' }
Pass "Python：$py"

Step '4/7 检查 agent-desktop（Rust 原生 UI Automation 执行器）'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = $Root
$exe = & $py -c "import da_core;p=da_core.find_agent_desktop();print(p or '')"
if (-not $exe) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw '没有找到 npm。请先安装 Node.js LTS：winget install OpenJS.NodeJS.LTS' }
    Warn '未找到 agent-desktop，正在执行 npm install -g agent-desktop ……'
    npm install -g agent-desktop
    if ($LASTEXITCODE -ne 0) { throw 'npm 安装 agent-desktop 失败' }
    $exe = & $py -c "import da_core;p=da_core.find_agent_desktop();print(p or '')"
    if (-not $exe) { throw '安装后仍找不到 agent-desktop.exe；请设置环境变量 AGENT_DESKTOP_EXE 指向它。' }
}
Pass "agent-desktop：$exe"
$ver = & $exe version
Write-Host "    $ver"
if ($ver -notmatch '"version"\s*:\s*"2\.') { Warn '契约版本不是 2.x，部分字段可能不兼容；agent.py 会给出 contract_warning。' }

Step '5/7 创建虚拟环境并安装依赖'
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPy)) { & $py -m venv (Join-Path $Root '.venv'); if ($LASTEXITCODE -ne 0) { throw 'venv 创建失败' } }
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip 安装依赖失败' }
Pass '依赖已安装'

Step '6/7 运行离线回归测试（不触碰你的桌面）'
$env:DESKAGENT_HOME = $Root
& $venvPy (Join-Path $Root 'tests\test_all.py') 2>&1 | Select-Object -Last 6
if ($LASTEXITCODE -ne 0) { throw '回归测试失败：代码在你的机器上未通过自检，请把上面的输出发给我。' }
Pass '回归测试全部通过'

Step '7/7 写入 PowerShell 配置文件'
if ($SkipProfile) { Warn '已跳过 $PROFILE' }
else {
    $line = ". '$Root\ps\DeskAgent.ps1'   # DeskAgent"
    New-Item -ItemType File -Force -Path $PROFILE | Out-Null
    $cur = Get-Content -LiteralPath $PROFILE -Raw -ErrorAction SilentlyContinue
    if ($null -eq $cur) { $cur = '' }
    if ($cur -notmatch '# DeskAgent') { Add-Content -LiteralPath $PROFILE -Value "`n$line"; Pass "已写入 $PROFILE" }
    else { Pass '$PROFILE 已包含 DeskAgent' }
}

Write-Host "`n部署完成。下一步（在新的 pwsh 窗口，或先执行  . '$Root\ps\DeskAgent.ps1' ）：" -ForegroundColor Green
Write-Host '  ai-key                       # 保存 DeepSeek API Key（只需一次）'
Write-Host '  ai-doctor                    # 体检：agent-desktop / UIA 权限 / 窗口枚举'
Write-Host '  ai -DryRun "打开记事本并输入 Hello"   # 先干跑'
Write-Host '  ai "打开记事本并输入 Hello"           # 真正执行，结尾会打印“执行回执”'
Write-Host '  ai-verify -E2E               # 用独立的 Win32/UIA 路径复核结果'
