# ============================================================
#  把控制台打包成单文件 exe（PyInstaller）
#
#  用法（项目根目录，或直接右键「用 PowerShell 运行」）：
#      powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
#
#  产物：dist\GI-Agent-Console.exe
#      · 把它放在**项目根目录**双击运行（会读取同目录的 .env / memory / logs）；
#      · exe 内部自带 prompts/ 与 memory/boss_drops_dict.json 作为兜底；
#      · GUI 里的「启动」按钮在 exe 模式下会用 `exe --run-cli` 拉起自己跑 CLI，
#        所以不需要另外装 Python。
# ============================================================
param(
    [string]$Name = "GI-Agent-Console",
    [switch]$Console   # 加这个参数可以保留黑窗，方便看报错
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    throw "没找到 Python。请先建好 venv：python -m venv venv"
}
Write-Host "使用解释器：$python" -ForegroundColor Cyan

# 1) 确保 PyInstaller 可用（缺失就装）
& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "没装 PyInstaller，正在安装…" -ForegroundColor Yellow
    & $python -m pip install --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败（检查网络/代理）" }
}

# 2) 打包参数
$windowMode = if ($Console) { "--console" } else { "--noconsole" }
$arguments = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onefile", $windowMode,
    "--name", $Name,
    # CLI 是被 --run-cli 动态拉起来的，显式声明进来
    "--hidden-import", "main",
    # 配置文件兜底（项目根目录里的同名文件优先）
    "--add-data", "prompts;prompts",
    "--add-data", "memory\boss_drops_dict.json;memory",
    "--add-data", ".env.example;.",
    "--add-data", "README.md;.",
    # 只排除明显用不到的重量级依赖，避免把 playwright 整套浏览器驱动打进去
    "--exclude-module", "playwright",
    "--exclude-module", "pytest",
    "gui.py"
)

Write-Host "开始打包（这一步可能要几分钟）…" -ForegroundColor Cyan
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

$exe = Join-Path $root "dist\$Name.exe"
if (-not (Test-Path $exe)) { throw "没找到打包产物：$exe" }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "`n✅ 打包完成：$exe（$size MB）" -ForegroundColor Green
Write-Host @"

接下来：
  1. 把 $Name.exe 复制到项目根目录（和 .env、memory\、prompts\ 放一起）
  2. 双击运行 —— 启动/停止、日志、配置、体检都在界面里
  3. 首次运行若提示缺 .env，从 .env.example 复制一份并填好密钥/UID

注意：exe 只负责界面与调度；真正的游戏自动化仍然由 BetterGI 完成。
"@ -ForegroundColor Gray
