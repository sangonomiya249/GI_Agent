"""磁盘占用体检：告诉你"这东西为什么这么大"，以及哪些是可选的。

用法：
    python scripts/disk_report.py

只看不删。会统计：仓库各部分、venv 里最胖的包、本机 BetterGI 有多大、
浏览器档案目录（.studio-profile）当前多大 —— 并给出"能省多少"的结论。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config  # noqa: E402  （顺带让 stdout 变 UTF-8）

BROWSER_PROFILE = os.path.join(PROJECT_ROOT, ".studio-profile")

# 运行期/开发期才用得到的可选依赖：(包名, 大约多少 MB, 何时可以删)
OPTIONAL_PACKAGES = (
    ("playwright", 101, "只有「重建本地字典」（爬米游社百科/yatta）时才需要；日常跑 Agent 用不到"),
    ("lark_oapi", 37, "只有用飞书通道时才需要；只用 QQ / 终端可以卸掉（代码已改成按需 import）"),
)


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def mb(value):
    return value / 1024 / 1024


def main():
    print("=" * 68)
    print("仓库各部分（顶层）")
    print("=" * 68)
    rows = []
    for entry in sorted(os.listdir(PROJECT_ROOT)):
        path = os.path.join(PROJECT_ROOT, entry)
        if os.path.isdir(path):
            rows.append((dir_size(path), entry + "/"))
        else:
            try:
                rows.append((os.path.getsize(path), entry))
            except OSError:
                pass
    rows.sort(reverse=True)
    for size, name in rows[:15]:
        print(f"   {mb(size):8.1f} MB  {name}")
    print(f"   {mb(sum(size for size, _ in rows)):8.1f} MB  合计（含 venv 与运行期缓存）")

    print()
    print("=" * 68)
    print("venv 里最胖的包（Python 依赖，占了仓库的大头）")
    print("=" * 68)
    site = os.path.join(PROJECT_ROOT, "venv", "Lib", "site-packages")
    packages = {}
    if os.path.isdir(site):
        for entry in os.listdir(site):
            path = os.path.join(site, entry)
            packages[entry] = dir_size(path) if os.path.isdir(path) else os.path.getsize(path)
        for size, name in sorted(
            ((value, key) for key, value in packages.items()), reverse=True
        )[:12]:
            print(f"   {mb(size):8.1f} MB  {name}")

        optional_total = 0
        print()
        print("可以卸掉的可选依赖：")
        for name, approx, why in OPTIONAL_PACKAGES:
            hit = next((key for key in packages if key.lower() == name.lower()), None)
            if hit:
                optional_total += packages[hit]
                print(f"   {mb(packages[hit]):8.1f} MB  {name} —— {why}")
                print(f"              （卸掉：venv\\Scripts\\pip uninstall {name}）")
            else:
                print(f"   {'未安装':>11s}  {name} —— {why}")
        if optional_total:
            print(f"\n   ↳ 卸掉上面这些可以省约 {mb(optional_total):.1f} MB")

    print()
    print("=" * 68)
    print("对照：BetterGI 本体")
    print("=" * 68)
    bgi = config.BGI_DIR
    if os.path.isdir(bgi):
        print(f"   {mb(dir_size(bgi)):8.1f} MB  {bgi}")
        top = []
        for entry in os.listdir(bgi):
            path = os.path.join(bgi, entry)
            top.append((dir_size(path) if os.path.isdir(path) else os.path.getsize(path), entry))
        for size, name in sorted(top, reverse=True)[:6]:
            print(f"   {mb(size):8.1f} MB    └─ {name}")
    else:
        print("   （没找到 BetterGI 目录）")

    print()
    print("=" * 68)
    print("浏览器档案 / WebView2")
    print("=" * 68)
    if os.path.isdir(BROWSER_PROFILE):
        print(f"   {mb(dir_size(BROWSER_PROFILE)):8.1f} MB  .studio-profile（Studio 窗口的浏览器档案，可随手删）")
        print("              ↳ 里面是 Edge/WebView2 的缓存与组件；关掉 Studio 后整目录删除即可，下次自动重建")
    webview = r"C:\Program Files (x86)\Microsoft\EdgeWebView"
    if os.path.isdir(webview):
        print(f"   {mb(dir_size(webview)):8.1f} MB  系统 WebView2 运行时（{webview}）")
        print("              ↳ **不是我们的体积**：由 Windows/Edge 安装、所有用 WebView2 的程序共享")
    print()
    print("结论：本程序自己的代码 + exe + 本地字典通常只有十几 MB；")
    print("      占地方的是 Python 依赖（venv）和浏览器运行时，两者都不是程序本体。")


if __name__ == "__main__":
    main()
