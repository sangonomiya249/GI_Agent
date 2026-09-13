# ============================================================
#  生成 GI-Agent-Studio.exe —— 真正的原生桌面程序
#
#  用法（项目根目录）：
#      powershell -ExecutionPolicy Bypass -File scripts\build_studio_exe.ps1
#      powershell -ExecutionPolicy Bypass -File scripts\build_studio_exe.ps1 -NoEmbed
#
#  做什么：
#    1) 确认 WebView2 SDK（缺了就下载官方 NuGet 包，scripts\fetch_webview2.py）
#    2) 生成图标（纯 Python，不需要 Pillow）
#    3) 用 Roslyn csc 把 scripts\native\StudioHost.cs 编成 WinForms 程序，
#       并把 WebView2 的两个托管程序集 + 原生 WebView2Loader.dll **嵌进 exe**（单文件）
#
#  产物：GI-Agent-Studio.exe（约 2-3 MB，放在项目根目录双击运行）
# ============================================================
param(
    [string]$Name = "GI-Agent-Studio",
    [string]$Icon = "assets\gi-agent.ico",
    [switch]$NoEmbed,          # 打成 exe + 旁边三个 dll（方便排查）
    [switch]$Console           # 保留控制台窗口看报错
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($message) { Write-Host "[*] $message" -ForegroundColor Cyan }
function Ok($message)   { Write-Host "[+] $message" -ForegroundColor Green }

# ---------- 1. 解释器与依赖 ----------
$sdkDir = Join-Path $root "build\webview2"
$coreDll = Join-Path $sdkDir "Microsoft.Web.WebView2.Core.dll"
$formsDll = Join-Path $sdkDir "Microsoft.Web.WebView2.WinForms.dll"
$loaderDll = Join-Path $sdkDir "WebView2Loader.dll"

$sdkReady = (Test-Path $coreDll) -and (Test-Path $formsDll) -and (Test-Path $loaderDll)
if (-not $sdkReady) {
    $python = Join-Path $root "venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $python) { throw "没找到 Python。请先建好 venv：python -m venv venv" }

    Step "准备 WebView2 SDK…"
    & $python "scripts\fetch_webview2.py"
    if ($LASTEXITCODE -ne 0) { throw "WebView2 SDK 不可用（需要能访问 api.nuget.org，或手动把 3 个文件放到 build\webview2）" }
}

foreach ($file in @($coreDll, $formsDll, $loaderDll)) {
    if (-not (Test-Path $file)) { throw "缺少 $file" }
}

if (-not (Test-Path $Icon)) {
    Step "生成图标 $Icon …"
    & $python "scripts\make_icon.py" $Icon
}

# ---------- 2. 找 Roslyn 编译器（VS 自带）----------
$csc = @(
    "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\Roslyn\csc.exe",
    "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\Roslyn\csc.exe",
    "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\Roslyn\csc.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $csc) {
    # 退回到 .NET Framework 自带的编译器
    $csc = @(
        "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $csc) { throw "没找到 csc.exe（装个 Visual Studio 或 .NET Framework 4.x 即可）" }
Ok "编译器：$csc"

# ---------- 3. 参考程序集 ----------
# 注意：VS 里的 `v4.X` 目录只是"最新版"别名（可能只有 RedistList 没有 dll），
# 必须逐个确认 System.dll 真在里面，否则会报 CS0006。
$refCandidates = @(
    "C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.8",
    "C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.7.2",
    "C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.7.1",
    "C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.6.2",
    "C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.X"
)
$refRoot = $refCandidates |
    Where-Object { Test-Path (Join-Path $_ "System.dll") } |
    Select-Object -First 1

if ($refRoot) {
    Ok "参考程序集：$refRoot"
    $frameworkRefs = @(
        (Join-Path $refRoot "System.dll"),
        (Join-Path $refRoot "System.Core.dll"),
        (Join-Path $refRoot "System.Drawing.dll"),
        (Join-Path $refRoot "System.Windows.Forms.dll")
    )
} else {
    $frameworkRefs = @("System.dll", "System.Core.dll", "System.Drawing.dll", "System.Windows.Forms.dll")
}

# ---------- 4. 编译 ----------
$source = Join-Path $root "scripts\native\StudioHost.cs"
$output = Join-Path $root "$Name.exe"
$target = if ($Console) { "exe" } else { "winexe" }

$arguments = @(
    "/nologo",
    "/target:$target",
    "/platform:x64",
    "/optimize+",
    "/langversion:latest",
    "/out:`"$output`""
)
foreach ($reference in $frameworkRefs) { $arguments += "/reference:`"$reference`"" }
$arguments += "/reference:`"$coreDll`""
$arguments += "/reference:`"$formsDll`""
if (Test-Path $Icon) { $arguments += "/win32icon:`"$Icon`"" }

if (-not $NoEmbed) {
    # 单文件：托管程序集与原生加载器都嵌进 exe，运行时自解压
    $arguments += "/resource:`"$coreDll`",Microsoft.Web.WebView2.Core.dll"
    $arguments += "/resource:`"$formsDll`",Microsoft.Web.WebView2.WinForms.dll"
    $arguments += "/resource:`"$loaderDll`",WebView2Loader.dll"
}
$arguments += "`"$source`""

Step "编译 $Name.exe …"
& $csc @arguments
if ($LASTEXITCODE -ne 0) { throw "编译失败（csc 返回 $LASTEXITCODE）" }
if (-not (Test-Path $output)) { throw "没找到产物：$output" }

if ($NoEmbed) {
    Step "复制 WebView2 依赖到 exe 旁边…"
    foreach ($file in @($coreDll, $formsDll, $loaderDll)) {
        Copy-Item $file (Join-Path $root (Split-Path $file -Leaf)) -Force
    }
}

$size = [math]::Round((Get-Item $output).Length / 1MB, 2)
Ok "已生成：$output（$size MB）"
Write-Host @"

用法：
  1. 双击 $Name.exe —— 原生窗口（WinForms + WebView2 控件），不是浏览器窗口
  2. exe 要放在项目根目录（要能找到 venv\、app_web.py、.env、studio\）
  3. 界面里点「⏻ 退出 Studio」或直接关窗口，后端 Python 进程会一起退出
  4. 想临时用回浏览器模式：python app_web.py（会自动用 Edge --app 打开）

排错：
  · 启动失败会弹窗并把 studio.log / studio-host.log 的尾部贴出来
  · 需要 WebView2 运行时（Win10/11 与装了 Edge 的机器都有）：
    https://developer.microsoft.com/microsoft-edge/webview2/
"@ -ForegroundColor Gray
