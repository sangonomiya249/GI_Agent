#Requires -Version 5.1
<#
    一键注册 BetterGI「一条龙」计划任务（README 第 4 步的自动化版本）

    背景：BetterGI 需要管理员权限运行，Agent 直接用 schtasks /run 触发一个
    已注册为「使用最高权限运行」的计划任务，就能免 UAC 静默拉起游戏自动化。

    用法（必须以管理员身份运行）：
        powershell -ExecutionPolicy Bypass -File .\scripts\setup_start_bettergi_task.ps1

    可选参数：
        -BgiDir "D:\Games\BetterGI"   手动指定 BetterGI 目录
        -RunTest                      注册后立刻触发一次，验证能否拉起
        -Uninstall                    删除该计划任务
#>
[CmdletBinding()]
param(
    [string]$BgiDir = "",
    [switch]$RunTest,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "StartBetterGI"
$StopTaskName = "StopBetterGI"
# 🌟 关原神用的任务：原神多半是**以管理员权限**启动的（例如被管理员的 BetterGI 拉起），
#    普通权限 taskkill 只会得到 "拒绝访问"，所以同样需要一个同等权限的任务代为关闭。
$StopGenshinTaskName = "StopGenshin"

function Write-Step($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "[x] $m" -ForegroundColor Red }

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Err "本脚本必须以【管理员身份】运行。"
    Write-Host ""
    Write-Host "  请右键点击「Windows PowerShell」->「以管理员身份运行」，然后执行：" -ForegroundColor Gray
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -ForegroundColor Gray
    exit 1
}

# ---------- 卸载分支 ----------
if ($Uninstall) {
    foreach ($name in @($TaskName, $StopTaskName, $StopGenshinTaskName)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Ok "已删除计划任务 $name"
        }
        else {
            Write-Warn "计划任务 $name 不存在，无需删除。"
        }
    }
    exit 0
}

# ---------- 1. 解析 BetterGI 目录：参数 > .env 的 BGI_DIR > 默认安装路径 ----------
if (-not $BgiDir) {
    $envFile = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
    if (Test-Path $envFile) {
        foreach ($line in (Get-Content $envFile -Encoding UTF8)) {
            if ($line -match '^\s*BGI_DIR\s*=\s*(.*)$') {
                $candidate = $Matches[1].Trim().Trim('"')
                if ($candidate) {
                    $BgiDir = $candidate
                    Write-Step "从 .env 读取到 BGI_DIR = $BgiDir"
                    break
                }
            }
        }
    }
}

if (-not $BgiDir) {
    foreach ($guess in @("C:\Program Files\BetterGI", "D:\Program Files\BetterGI", "D:\BetterGI")) {
        if (Test-Path (Join-Path $guess "BetterGI.exe")) {
            $BgiDir = $guess
            Write-Step "自动探测到 BetterGI 目录：$BgiDir"
            break
        }
    }
}

if (-not $BgiDir) {
    $BgiDir = "C:\Program Files\BetterGI"
}

$exePath = Join-Path $BgiDir "BetterGI.exe"
if (-not (Test-Path $exePath)) {
    Write-Err "找不到 BetterGI.exe：$exePath"
    Write-Host "  请用 -BgiDir 参数指定正确的安装目录，例如：" -ForegroundColor Gray
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -BgiDir `"D:\Games\BetterGI`"" -ForegroundColor Gray
    exit 1
}
Write-Ok "BetterGI 可执行文件：$exePath"

# ---------- 2. 注册计划任务 ----------
Write-Step "正在注册计划任务 $TaskName（最高权限 / 交互式登录 / 不因电池停止）..."

$action = New-ScheduledTaskAction `
    -Execute $exePath `
    -Argument "--startOneDragon" `
    -WorkingDirectory $BgiDir

$principal = New-ScheduledTaskPrincipal `
    -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
    -LogonType Interactive `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Principal $principal `
    -Settings $settings `
    -Description "由 GI_Agent 使用：免 UAC 拉起 BetterGI 一条龙" `
    -Force | Out-Null

# ---------- 3. 注册「关闭」计划任务 ----------
# BetterGI 以管理员权限运行，普通终端 taskkill 会 Access denied；
# 而 BetterGI 又是单实例程序，不先关掉就无法冷启动新的一条龙。
# 所以需要一个同等权限的任务来代为关闭。
$stopScript = Join-Path $PSScriptRoot "stop_bettergi.ps1"
if (-not (Test-Path $stopScript)) {
    Write-Err "找不到关闭脚本：$stopScript"
    exit 1
}

Write-Step "正在注册计划任务 $StopTaskName（最高权限 / 用于自动关闭 BetterGI）..."

$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$stopAction = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$stopScript`""

$stopSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $StopTaskName `
    -Action $stopAction `
    -Principal $principal `
    -Settings $stopSettings `
    -Description "由 GI_Agent 使用：以管理员权限关闭 BetterGI（热启动无效，需先关再冷启动）" `
    -Force | Out-Null

# ---------- 3.5 注册「关闭原神」计划任务 ----------
# 原神一般由管理员权限的启动器/BetterGI 拉起，普通终端 taskkill 会 "拒绝访问"；
# 有了这个任务，"帮我关闭原神" 才能在权限不足时照样关掉（免 UAC）。
$stopGenshinScript = Join-Path $PSScriptRoot "stop_genshin.ps1"
if (-not (Test-Path $stopGenshinScript)) {
    Write-Err "找不到关闭原神的脚本：$stopGenshinScript"
    exit 1
}

Write-Step "正在注册计划任务 $StopGenshinTaskName（最高权限 / 用于关闭原神）..."

$stopGenshinAction = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$stopGenshinScript`""

Register-ScheduledTask `
    -TaskName $StopGenshinTaskName `
    -Action $stopGenshinAction `
    -Principal $principal `
    -Settings $stopSettings `
    -Description "由 GI_Agent 使用：以管理员权限关闭原神（原神以管理员运行时普通 taskkill 会拒绝访问）" `
    -Force | Out-Null

# ---------- 4. 回读验证 ----------
$task = Get-ScheduledTask -TaskName $TaskName
$stopTask = Get-ScheduledTask -TaskName $StopTaskName
$stopGenshinTask = Get-ScheduledTask -TaskName $StopGenshinTaskName

Write-Host ""
Write-Ok "注册成功！以下三个任务都已建好："
Write-Host ""
Write-Host "  [$TaskName]  ← Agent 用来自动拉起一条龙"
Write-Host "    运行身份 : $($task.Principal.UserId)（RunLevel=$($task.Principal.RunLevel)）"
Write-Host "    执行程序 : $($task.Actions.Execute) $($task.Actions.Arguments)"
Write-Host "    起始于   : $($task.Actions.WorkingDirectory)"
Write-Host ""
Write-Host "  [$StopTaskName]  ← Agent 用来自动关闭 BetterGI（热启动前清理）"
Write-Host "    运行身份 : $($stopTask.Principal.UserId)（RunLevel=$($stopTask.Principal.RunLevel)）"
Write-Host "    执行程序 : powershell -File scripts\stop_bettergi.ps1"
Write-Host ""
Write-Host "  [$StopGenshinTaskName]  ← 用来关闭原神（「帮我关闭原神」指令用）"
Write-Host "    运行身份 : $($stopGenshinTask.Principal.UserId)（RunLevel=$($stopGenshinTask.Principal.RunLevel)）"
Write-Host "    执行程序 : powershell -File scripts\stop_genshin.ps1"

# ---------- 5. 可选：立刻试跑一次 ----------
if ($RunTest) {
    Write-Host ""
    Write-Step "正在触发一次以验证（若 BetterGI 已被拉起属正常现象）..."
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    if ($info.LastTaskResult -eq 0) {
        Write-Ok "触发成功，LastTaskResult = 0。BetterGI 应已启动一条龙。"
    }
    else {
        Write-Warn "任务已触发，但 LastTaskResult = $($info.LastTaskResult)，请检查 BetterGI 是否能正常启动。"
    }
}

Write-Host ""
Write-Host "现在回到 GI_Agent 里重新执行一次指令，审批时输入 y 即可：" -ForegroundColor Green
Write-Host "  · BetterGI 没开        → 直接冷启动一条龙" -ForegroundColor Green
Write-Host "  · BetterGI 开着但空闲  → 自动关闭后冷启动" -ForegroundColor Green
Write-Host "  · BetterGI 正在跑任务  → 拒绝执行，等你跑完" -ForegroundColor Green
