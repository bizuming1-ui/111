# DeskAgent.ps1 —— 在 PowerShell 7 里用自然语言控制电脑
# 加载方式：  . D:\DeepSeekDesktopAgent\ps\DeskAgent.ps1   （Setup.ps1 已写入 $PROFILE，之后每次打开 pwsh 自动加载）
#
# 命令：
#   ai "打开记事本并输入 Hello"     执行一次自然语言任务（省略引号内容 = 进入交互模式）
#   ai -DryRun "打开计算器"          干跑：模型照常规划，但不对桌面做任何动作
#   ai-key                           安全保存 DeepSeek API Key（DPAPI 加密，仅当前 Windows 用户可解）
#   ai-stop / ai-resume              急停 / 解除急停（另开一个 pwsh 窗口也可用）
#   ai-doctor                        环境体检
#   ai-test                          运行离线回归测试
#   ai-audit                         校验审计日志哈希链是否被篡改
#   ai-verify [-E2E]                 用与 agent-desktop 无关的 Win32/UIA 路径独立验证
#   ai-voice                         说一句话 → 识别 → 你确认 → 执行（可选，需要 Windows 语音识别）

$global:DeskAgentRoot = Split-Path -Parent $PSScriptRoot

function Get-DeskPython {
    $venv = Join-Path $global:DeskAgentRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw '找不到 Python。请先运行：pwsh -File D:\DeepSeekDesktopAgent\ps\Setup.ps1'
}

function Initialize-DeskEnv {
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [Console]::OutputEncoding = $utf8
    [Console]::InputEncoding = $utf8
    $global:OutputEncoding = $utf8
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:DESKAGENT_HOME = $global:DeskAgentRoot
    if (-not $env:DEEPSEEK_API_KEY) {
        $keyFile = Join-Path $global:DeskAgentRoot 'secrets\deepseek.key'
        if (Test-Path -LiteralPath $keyFile) {
            try {
                $sec = (Get-Content -LiteralPath $keyFile -Raw).Trim() | ConvertTo-SecureString
                $env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $sec).Password
            } catch {
                Write-Warning "读取已保存的密钥失败（可能换了 Windows 用户），请重新运行 ai-key。"
            }
        }
    }
}

function ai {
    [CmdletBinding(PositionalBinding = $false)]
    param(
        [Parameter(Position = 0, ValueFromRemainingArguments = $true)][string[]]$Task,
        [switch]$DryRun,
        [ValidateRange(1, 200)][int]$MaxSteps = 40
    )
    Initialize-DeskEnv
    if (Test-Path -LiteralPath (Join-Path $global:DeskAgentRoot 'STOP')) {
        Write-Warning '急停开关处于开启状态（STOP 文件存在）。确认安全后运行 ai-resume 再继续。'
        return
    }
    if (-not $env:DEEPSEEK_API_KEY) {
        Write-Warning '没有 DeepSeek API Key。请先运行：ai-key'
        return
    }
    $py = Get-DeskPython
    $argv = @((Join-Path $global:DeskAgentRoot 'agent.py'), '--max-steps', "$MaxSteps")
    if ($DryRun) { $argv += '--dry-run' }
    if ($Task -and $Task.Count -gt 0) { $argv += ($Task -join ' ') }
    & $py @argv
}

function ai-key {
    $sec = Read-Host 'DeepSeek API Key（输入不可见）' -AsSecureString
    if ($sec.Length -eq 0) { Write-Warning '未输入，已取消。'; return }
    $dir = Join-Path $global:DeskAgentRoot 'secrets'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $sec | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $dir 'deepseek.key') -Encoding ascii
    try { icacls $dir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null } catch { }
    $env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $sec).Password
    Write-Host '密钥已用 Windows DPAPI 加密保存（仅你当前的 Windows 账户能解密），并已载入当前会话。' -ForegroundColor Green
}

function ai-stop {
    Set-Content -LiteralPath (Join-Path $global:DeskAgentRoot 'STOP') -Value (Get-Date -Format o)
    Write-Host '已开启急停：Agent 将在下一个动作之前停止。用 ai-resume 解除。' -ForegroundColor Yellow
}

function ai-resume {
    Remove-Item -LiteralPath (Join-Path $global:DeskAgentRoot 'STOP') -Force -ErrorAction SilentlyContinue
    Write-Host '已解除急停。' -ForegroundColor Green
}

function ai-doctor {
    Initialize-DeskEnv
    & (Get-DeskPython) (Join-Path $global:DeskAgentRoot 'agent.py') --doctor
}

function ai-test {
    Initialize-DeskEnv
    & (Get-DeskPython) (Join-Path $global:DeskAgentRoot 'tests\test_all.py')
}

function ai-audit {
    Initialize-DeskEnv
    & (Get-DeskPython) (Join-Path $global:DeskAgentRoot 'agent.py') --verify-audit
}

function ai-verify {
    param([switch]$E2E)
    Initialize-DeskEnv
    $script = Join-Path $global:DeskAgentRoot 'ps\Verify-Independent.ps1'
    if ($E2E) { & $script -E2E } else { & $script }
}

function ai-voice {
    param([ValidateRange(3, 30)][int]$Seconds = 8)
    try { Add-Type -AssemblyName System.Speech -ErrorAction Stop }
    catch { Write-Warning '这个 PowerShell 无法加载 System.Speech，请改用文字：ai "指令"'; return }
    $rec = $null
    $res = $null
    try {
        $rec = [System.Speech.Recognition.SpeechRecognitionEngine]::new()
        $rec.LoadGrammar([System.Speech.Recognition.DictationGrammar]::new())
        $rec.SetInputToDefaultAudioDevice()
        Write-Host "请在 $Seconds 秒内说出指令……（需已安装对应语言的 Windows 语音识别）" -ForegroundColor Cyan
        $res = $rec.Recognize([TimeSpan]::FromSeconds($Seconds))
    } catch {
        Write-Warning "语音识别不可用：$($_.Exception.Message)"
        return
    } finally {
        if ($rec) { $rec.Dispose() }
    }
    if (-not $res -or [string]::IsNullOrWhiteSpace($res.Text)) { Write-Warning '没有识别到语音。'; return }
    Write-Host ("识别结果：{0}  (置信度 {1:N2})" -f $res.Text, $res.Confidence) -ForegroundColor Green
    $ans = Read-Host '直接回车=执行；输入文字=改成这句再执行；n=取消'
    if ($ans -eq 'n') { return }
    $text = if ([string]::IsNullOrWhiteSpace($ans)) { $res.Text } else { $ans }
    ai $text
}

Write-Host 'DeskAgent 已加载：ai "指令" | ai-key | ai-stop | ai-doctor | ai-test | ai-verify | ai-voice' -ForegroundColor DarkGray
