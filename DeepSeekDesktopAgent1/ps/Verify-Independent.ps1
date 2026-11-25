#Requires -Version 7.0
<#
  Verify-Independent.ps1 —— 与 agent-desktop / DeepSeek 完全无关的第三方复核
  A. 用 C#(P/Invoke user32.EnumWindows) 枚举真实窗口，和 agent-desktop list-windows 逐条对账
  B. -E2E：让 Agent 真正执行“打开记事本并输入随机标记”，再用 .NET UI Automation（System.Windows.Automation）
     独立读取记事本里的文字，确认和标记完全一致。
  这两条路径使用的是 Windows 自带 API，不经过 agent-desktop，因此能证明“结果是真的”，而不是 Agent 自己说的。
#>
[CmdletBinding()]
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$E2E
)
$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$env:DESKAGENT_HOME = $Root
$env:PYTHONPATH = $Root
$script:fail = 0
function Check([bool]$ok, [string]$name, [string]$detail = '') {
    if ($ok) { Write-Host "[PASS] $name $detail" -ForegroundColor Green }
    else { Write-Host "[FAIL] $name $detail" -ForegroundColor Red; $script:fail++ }
}

$csharp = '
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class DAWin
{
    private delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumProc p, IntPtr l);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr h, StringBuilder sb, int max);
    [DllImport("user32.dll")] private static extern int GetWindowTextLength(IntPtr h);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();

    public static List<string[]> List()
    {
        var res = new List<string[]>();
        IntPtr fg = GetForegroundWindow();
        EnumWindows((h, l) =>
        {
            if (!IsWindowVisible(h)) return true;
            int len = GetWindowTextLength(h);
            if (len == 0) return true;
            var sb = new StringBuilder(len + 1);
            GetWindowText(h, sb, sb.Capacity);
            uint pid; GetWindowThreadProcessId(h, out pid);
            res.Add(new string[] { h.ToInt64().ToString(), pid.ToString(), sb.ToString(), (h == fg) ? "1" : "0" });
            return true;
        }, IntPtr.Zero);
        return res;
    }
}
'
if (-not ('DAWin' -as [type])) { Add-Type -TypeDefinition $csharp -Language CSharp }

$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { $py = (Get-Command python).Source }
$exe = & $py -c "import da_core;p=da_core.find_agent_desktop();print(p or '')"
Check ([bool]$exe) '找到 agent-desktop' "$exe"
if (-not $exe) { exit 1 }

Write-Host "`n=== A. 窗口清单对账：Win32 EnumWindows  vs  agent-desktop list-windows ===" -ForegroundColor Cyan
$win32 = [DAWin]::List() | ForEach-Object { [pscustomobject]@{ Handle = [int64]$_[0]; Pid = [int]$_[1]; Title = $_[2]; Fg = ($_[3] -eq '1') } }
$raw = (& $exe list-windows) -join "`n"
$resp = $raw | ConvertFrom-Json
Check ($resp.ok -eq $true) 'agent-desktop list-windows 返回 ok'
$agentWins = @($resp.data)
Write-Host ("    Win32 可见有标题窗口：{0} 个；agent-desktop 报告：{1} 个" -f $win32.Count, $agentWins.Count)

$ghost = @()
foreach ($w in $agentWins) {
    if ($w.visible -eq $false -or $w.minimized -eq $true) { continue }
    $hit = $win32 | Where-Object { $_.Pid -eq [int]$w.pid -and ($_.Title -eq $w.title -or [string]::IsNullOrEmpty($w.title)) }
    if (-not $hit) { $ghost += $w }
}
Check ($ghost.Count -eq 0) 'agent-desktop 报告的每个可见窗口都能在 Win32 中找到（无“幻觉窗口”）' ("未对上：" + (($ghost | ForEach-Object { $_.title }) -join ' | '))

$fgWin32 = $win32 | Where-Object Fg | Select-Object -First 1
$fgAgent = $agentWins | Where-Object { $_.is_focused } | Select-Object -First 1
if ($fgWin32 -and $fgAgent) {
    Check ($fgWin32.Pid -eq [int]$fgAgent.pid) '前台窗口一致' ("Win32: $($fgWin32.Title)  |  agent-desktop: $($fgAgent.title)")
} else {
    Write-Host '    [INFO] 前台窗口对比跳过（其中一方没有前台窗口）' -ForegroundColor Yellow
}
$missed = @($win32 | Where-Object { $p = $_; -not ($agentWins | Where-Object { [int]$_.pid -eq $p.Pid }) })
Write-Host ("    [INFO] Win32 有而 agent-desktop 未列出的窗口进程数：{0}（工具窗口/不可访问窗口属正常）" -f $missed.Count)

if ($E2E) {
    Write-Host "`n=== B. 端到端：让 Agent 打开记事本并输入随机标记，再用 UIA 独立读取 ===" -ForegroundColor Cyan
    if (-not $env:DEEPSEEK_API_KEY) {
        $keyFile = Join-Path $Root 'secrets\deepseek.key'
        if (Test-Path -LiteralPath $keyFile) {
            $sec = (Get-Content -LiteralPath $keyFile -Raw).Trim() | ConvertTo-SecureString
            $env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $sec).Password
        }
    }
    if (-not $env:DEEPSEEK_API_KEY) { Write-Warning '缺少 DeepSeek 密钥，先运行 ai-key。'; exit 2 }
    Write-Warning '将会真的打开记事本并写入文字。若记事本里有未保存的重要内容，请先保存/关闭（Agent 发现原值不符会拒绝写入，但请不要依赖这点）。'
    $null = Read-Host '按回车继续，Ctrl+C 取消'
    $marker = 'DeskAgent-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    $before = @(Get-Process -Name notepad -ErrorAction SilentlyContinue | ForEach-Object Id)
    & $py (Join-Path $Root 'agent.py') "打开记事本并输入 $marker"
    Start-Sleep -Seconds 1

    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $AE = [System.Windows.Automation.AutomationElement]
    $TS = [System.Windows.Automation.TreeScope]
    $found = $false
    $seen = @()
    foreach ($p in Get-Process -Name notepad -ErrorAction SilentlyContinue) {
        $cond = [System.Windows.Automation.PropertyCondition]::new($AE::ProcessIdProperty, $p.Id)
        foreach ($win in $AE::RootElement.FindAll($TS::Children, $cond)) {
            $ct = [System.Windows.Automation.ControlType]
            $editCond = [System.Windows.Automation.OrCondition]::new(
                [System.Windows.Automation.PropertyCondition]::new($AE::ControlTypeProperty, $ct::Edit),
                [System.Windows.Automation.PropertyCondition]::new($AE::ControlTypeProperty, $ct::Document))
            foreach ($ed in $win.FindAll($TS::Descendants, $editCond)) {
                $text = $null
                $pat = $null
                if ($ed.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pat)) { $text = $pat.Current.Value }
                elseif ($ed.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$pat)) { $text = $pat.DocumentRange.GetText(-1) }
                if ($null -ne $text) { $seen += $text; if ($text.Trim() -eq $marker) { $found = $true } }
            }
        }
    }
    Check $found "UIA 独立读取到记事本内容 == 标记 [$marker]" ("实际读到：" + (($seen | ForEach-Object { "'" + $_ + "'" }) -join ', '))
    $newNotepad = @(Get-Process -Name notepad -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id })
    Write-Host ("    [INFO] 本次新出现的 notepad 进程：{0} 个（请手动关闭记事本；脚本不会替你关闭或保存）" -f $newNotepad.Count)
    Write-Host '    [INFO] 审计日志校验：' -NoNewline
    & $py (Join-Path $Root 'agent.py') --verify-audit
}

Write-Host ''
if ($script:fail -eq 0) { Write-Host '独立验证：全部通过。' -ForegroundColor Green; exit 0 }
else { Write-Host "独立验证：$($script:fail) 项未通过。" -ForegroundColor Red; exit 1 }
