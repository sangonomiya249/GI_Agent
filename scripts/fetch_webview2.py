"""下载并解包 Microsoft.Web.WebView2 SDK（只取编译原生窗口宿主需要的几个文件）。

为什么要它：要让 Studio 从"Edge 的 --app 窗口"变成**真正的桌面程序**，
需要在自己的 WinForms 窗口里嵌一个 WebView2 控件。这个控件对应的托管程序集
（`Microsoft.Web.WebView2.Core.dll` / `Microsoft.Web.WebView2.WinForms.dll`）
和原生加载器 `WebView2Loader.dll` 都在官方 NuGet 包里，很小（约 2MB）。

用法：
    python scripts/fetch_webview2.py            # 下载到 build/webview2/
    python scripts/fetch_webview2.py --check     # 只检查是否已经就绪

产物：
    build/webview2/Microsoft.Web.WebView2.Core.dll
    build/webview2/Microsoft.Web.WebView2.WinForms.dll
    build/webview2/WebView2Loader.dll
"""

import argparse
import io
import os
import sys
import urllib.request
import zipfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_DIR = os.path.join(PROJECT_ROOT, "build", "webview2")
VERSION = "1.0.2903.40"
PACKAGE_URL = (
    "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/{version}/"
    "microsoft.web.webview2.{version}.nupkg"
).format(version=VERSION)

WANTED = {
    "lib/net462/Microsoft.Web.WebView2.Core.dll": "Microsoft.Web.WebView2.Core.dll",
    "lib/net462/Microsoft.Web.WebView2.WinForms.dll": "Microsoft.Web.WebView2.WinForms.dll",
    "runtimes/win-x64/native/WebView2Loader.dll": "WebView2Loader.dll",
}


def missing_files(target_dir=None):
    directory = target_dir or TARGET_DIR
    return [name for name in WANTED.values() if not os.path.isfile(os.path.join(directory, name))]


def download_package(url=PACKAGE_URL, timeout=120):
    request = urllib.request.Request(url, headers={"User-Agent": "gi-agent/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def extract(payload, target_dir=None):
    directory = target_dir or TARGET_DIR
    os.makedirs(directory, exist_ok=True)
    written = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        for source, target in WANTED.items():
            if source not in names:
                raise RuntimeError("包里没有这个文件：" + source)
            with archive.open(source) as handle:
                data = handle.read()
            with open(os.path.join(directory, target), "wb") as out:
                out.write(data)
            written.append((target, len(data)))
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description="下载 WebView2 SDK（原生窗口宿主用）")
    parser.add_argument("--check", action="store_true", help="只检查，不下载")
    parser.add_argument("--dir", default="", help="输出目录（默认 build/webview2）")
    args = parser.parse_args(argv)
    target_dir = args.dir or TARGET_DIR

    missing = missing_files(target_dir)
    if not missing:
        print("WebView2 SDK 已就绪：" + target_dir)
        return 0
    if args.check:
        print("缺少：" + "、".join(missing))
        return 1

    print("正在下载 Microsoft.Web.WebView2 " + VERSION + " …")
    try:
        payload = download_package()
    except Exception as exc:
        print("下载失败：" + str(exc))
        print("（需要能访问 api.nuget.org；也可以手动下载 nupkg 后把三个文件放进 " + target_dir + "）")
        return 1

    written = extract(payload, target_dir)
    for name, size in written:
        print("  " + name + "  " + str(round(size / 1024)) + " KB")
    print("完成：" + target_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
