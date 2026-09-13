#Requires -Version 5.1
<#
    关闭原神 —— 供 Agent 通过计划任务 StopGenshin 以管理员权限调用。

    为什么需要它？
    原神通常是 **以管理员权限** 启动的（例如由以管理员身份运行的 BetterGI / 启动器拉起），
    于是普通权限的终端跑 `taskkill` 只会得到：
        ERROR: 无法终止 PID 为 xxx 的进程。原因: 拒绝访问。
    用同等权限的计划任务代为关闭，就能免 UAC 完成（和 StopBetterGI 一个套路）。

    安全性（两条硬规则）：
      1. **只杀 PID 文件里指定的 PID**，且该 PID 当前的进程名必须在白名单里
         （YuanShen / GenshinImpact / 云原神…），绝不按进程名通配乱杀；
      2. PID 记录超过 5 分钟视为过期 —— Windows 会复用 PID，过期记录可能误杀无关进程。

    PID 由 Agent 写在仓库的 scripts\stop_genshin.pids 里。
    退出码：0 = 目标已结束（或本来就不在了）；1 = 仍有目标没结束
#>
param(
    [string]$PidFile = "",
    [switch]$All,
    [string[]]$ProcessNames = @("YuanShen", "GenshinImpact")
)

$ErrorActionPreference = "SilentlyContinue"

$stateDir = $PSScriptRoot
$logFile = Join-Path $stateDir "stop_genshin.log"
if (-not $PidFile) { $PidFile = Join-Path $stateDir "stop_genshin.pids" }

function Write-Log([string]$msg) {
    try {
        if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
        Add-Content -Path $logFile -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg) -Encoding UTF8
    }
    catch { }
}

function Get-WhitelistedProcesses {
    $found = @()
    foreach ($name in $ProcessNames) {
        $found += @(Get-Process -Name $name -ErrorAction SilentlyContinue)
    }
    return $found
}

# ---------- 解析目标 PID ----------
$targets = @()
if ($All) {
    $targets = @(Get-WhitelistedProcesses | Select-Object -ExpandProperty Id)
    Write-Log ("按进程名收集到目标 PID [{0}]" -f ($targets -join ","))
}
elseif (Test-Path $PidFile) {
    $fresh = $true
    foreach ($line in (Get-Content $PidFile -ErrorAction SilentlyContinue)) {
        $t = $line.Trim()
        if ($t -match '^created=(.+)$') {
            try {
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
        if ($proc -and ($ProcessNames -contains $proc.ProcessName)) { $alive += $p }
    }
    return $alive
}

$initial = Get-AliveTargets
if ($initial.Count -eq 0) {
    Write-Log ("目标 PID [{0}] 已不在运行，无需处理" -f ($targets -join ","))
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

Write-Log ("开始关闭原神 PID [{0}]" -f ($initial -join ","))

# 1) 先正常关闭（等价于点窗口右上角的 X；游戏进度在服务器上，正常关闭更干净）
foreach ($p in Get-AliveTargets) { taskkill.exe /PID $p 2>&1 | Out-Null }
for ($i = 0; $i -lt 12; $i++) {
    Start-Sleep -Seconds 1
    if ((Get-AliveTargets).Count -eq 0) {
        Write-Log "已正常关闭"
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        exit 0
    }
}

# 2) 12 秒不响应则强制结束（仍只针对原 PID；原神进度在服务器，强杀不会丢档）
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
