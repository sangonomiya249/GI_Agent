#Requires -Version 5.1
<#
    关闭 BetterGI —— 供 Agent 通过计划任务 StopBetterGI 以管理员权限调用。

    为什么需要它？
    BetterGI 以【管理员权限】运行，Agent 所在终端通常是普通权限，直接 taskkill 会
    报 "Access denied"。用同等权限的计划任务代为关闭，就能免 UAC 完成。

    ⚠️ 关键设计：只杀【指定的 PID】，绝不按进程名杀。
    按进程名杀会踩到这个竞态：
        Agent 杀掉旧实例 → 立刻冷启动新实例 → 本脚本的等待循环还没退出 →
        它看到“还有 BetterGI 在跑”（其实是刚起来的新实例）→ 继续往下强制结束 → 把新实例杀了
    表现就是“刚拉起来约 1 秒又被关掉”。只认 PID 就不会误伤新实例。

    PID 由 Agent 写在 %LOCALAPPDATA%\GI_Agent\stop_bettergi.pids 里。
    退出码：0 = 目标已结束（或本来就不在了）；1 = 仍有目标没结束
#>
param(
    [string]$PidFile = "",
    [switch]$All          # 手动调用时才用：按进程名关闭所有 BetterGI
)

$ErrorActionPreference = "SilentlyContinue"

$stateDir = $PSScriptRoot
$logFile = Join-Path $stateDir "stop_bettergi.log"
if (-not $PidFile) { $PidFile = Join-Path $stateDir "stop_bettergi.pids" }

function Write-Log([string]$msg) {
    try {
        if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
        Add-Content -Path $logFile -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg) -Encoding UTF8
    }
    catch { }
}

# ---------- 解析目标 PID ----------
$targets = @()
if ($All) {
    $targets = @(Get-Process -Name BetterGI -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
}
elseif (Test-Path $PidFile) {
    $fresh = $true
    foreach ($line in (Get-Content $PidFile -ErrorAction SilentlyContinue)) {
        $t = $line.Trim()
        if ($t -match '^created=(.+)$') {
            try {
                # 超过 5 分钟的 PID 记录视为过期，避免 PID 被复用时误杀
                if (((Get-Date) - [datetime]::Parse($Matches[1])).TotalSeconds -gt 300) { $fresh = $false }
            }
            catch { }
        }
        elseif ($t -match '^\d+$') { $targets += [int]$t }
    }
    if (-not $fresh) {
        Write-Log "PID 记录已过期，跳过（避免误杀复用 PID 的新进程）"
        exit 0
    }
}

if ($targets.Count -eq 0) {
    Write-Log "没有指定目标 PID，不做任何事（绝不按进程名乱杀）"
    exit 0
}

function Get-AliveTargets {
    $alive = @()
    foreach ($p in $targets) {
        $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -eq 'BetterGI') { $alive += $p }
    }
    return $alive
}

$initial = Get-AliveTargets
if ($initial.Count -eq 0) {
    Write-Log ("目标 PID [{0}] 已不在运行，无需处理" -f ($targets -join ","))
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

Write-Log ("开始关闭 BetterGI PID [{0}]" -f ($initial -join ","))

# 1) 先尝试正常关闭（等价于点窗口右上角 X，让 BetterGI 自己保存配置）
foreach ($p in Get-AliveTargets) { taskkill.exe /PID $p 2>&1 | Out-Null }
for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Seconds 1
    if ((Get-AliveTargets).Count -eq 0) {
        Write-Log "已正常关闭"
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        exit 0
    }
}

# 2) 10 秒不响应则强制结束（仍只针对原 PID）
foreach ($p in Get-AliveTargets) { taskkill.exe /F /PID $p 2>&1 | Out-Null }
for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Seconds 1
    if ((Get-AliveTargets).Count -eq 0) {
        Write-Log "已强制关闭"
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        exit 0
    }
}

Write-Log ("关闭失败，仍存活: [{0}]" -f ((Get-AliveTargets) -join ","))
exit 1
