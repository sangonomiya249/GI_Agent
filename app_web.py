"""GI Agent Studio 启动器：本地 Flask 服务 + Edge/WebView2 的"应用窗口"。

双击 `GI-Agent-Studio.exe`（或 `启动GI-Agent-Studio.bat`）时跑的就是这个。

为什么要"浏览器内核 + 本地服务"而不是自绘界面：
  * 界面能做成真正的现代布局（侧栏 / 卡片 / 主题切换 / 实时日志），而不是 ttk 那种 90 年代观感；
  * Edge / WebView2 在 Windows 上**必定存在**（BetterGI 自己也要 WebView2），不引入新依赖；
  * 服务端只用 Flask（项目 requirements 里本来就有），离线可用、不需要 npm/构建步骤；
  * `--app=` 模式打开的是**没有地址栏、没有标签页**的独立窗口，看起来就是一个桌面程序。

用法：
    python app_web.py                 # 起服务 + 打开应用窗口，关掉窗口即退出
    python app_web.py --no-window     # 只起服务，打印 URL（手机/另一台机器也能看）
    python app_web.py --browser       # 用系统默认浏览器打开
    python app_web.py --port 8848     # 固定端口
"""

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 运行期产物统一放 logs/（以前散在根目录：studio.log / studio-access.log / studio-host.log）
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
PROFILE_DIR = os.path.join(PROJECT_ROOT, ".studio-profile")
LOG_PATH = os.path.join(LOG_DIR, "studio.log")
ACCESS_LOG_PATH = os.path.join(LOG_DIR, "studio-access.log")
ACCESS_LOG_MAX_BYTES = 2 * 1024 * 1024

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def log(message):
    """打日志：有控制台就打印，没有（pythonw / 双击 exe）就写 logs/studio.log。

    ⚠️ pythonw.exe 下 `sys.stdout` 是 None，直接 print() 会抛 AttributeError
    把进程当场干掉 —— 表现就是"双击 exe 什么也没发生"。
    """
    text = str(message)
    stream = getattr(sys, "stdout", None)
    if stream is not None:
        try:
            print(text, flush=True)
        except Exception:
            pass
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {text}\n")
    except Exception:
        pass


def find_browser():
    """找一个能用 `--app=` 模式开窗口的 Chromium 内核浏览器。"""
    for path in EDGE_CANDIDATES:
        if os.path.isfile(path):
            return path
    for name in ("msedge", "chrome"):
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(directory, f"{name}.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def open_app_window(url):
    """用 Chromium 的 --app 模式打开独立窗口；返回 Popen（关掉窗口即进程退出）。"""
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return None

    os.makedirs(PROFILE_DIR, exist_ok=True)
    command = [
        browser,
        f"--app={url}",
        "--window-size=1400,920",
        "--start-fullscreen",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
    ]
    try:
        return subprocess.Popen(command, close_fds=True)
    except Exception:
        webbrowser.open(url)
        return None


def serve(app, host, port):
    """在后台线程跑 werkzeug（拿得到真实端口，也方便主线程等窗口关闭）。"""
    from werkzeug.serving import make_server

    # 访问日志写到 logs/studio-access.log（不刷 stdout）：排查"窗口开了但页面没加载"时，
    # 看到 GET / 与 GET /api/state 就说明界面确实连上来了；平时也没必要刷屏。
    # ⚠️ 必须限量：以前它是无限增长的（实测已经 2.8 MB），所以改成 2 MB 滚动两个文件。
    logger = logging.getLogger("werkzeug")
    logger.setLevel(logging.INFO)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = RotatingFileHandler(
            ACCESS_LOG_PATH, maxBytes=ACCESS_LOG_MAX_BYTES, backupCount=1, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
    except Exception:
        logger.setLevel(logging.ERROR)

    server = make_server(host, port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def background_warmup(app):
    """后台加载重量级模块，避免应用窗口在冷启动时白等几十秒。"""
    started = time.time()
    log("后台预热开始，应用窗口已先行打开。")
    try:
        from studio.server import warmup

        warmup()
        app.config["ENV_WARMING"] = False
        log(f"后台预热完成，耗时 {time.time() - started:.1f} 秒")
    except Exception as exc:
        app.config["ENV_WARMING"] = False
        log(f"后台预热失败：{exc}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="GI Agent Studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = 自动挑一个空闲端口")
    parser.add_argument("--no-window", action="store_true", help="只起服务，不开窗口")
    parser.add_argument("--browser", action="store_true", help="用系统默认浏览器打开")
    parser.add_argument(
        "--exit-when-idle",
        type=float,
        default=1800.0,
        help="多久没有页面请求就自动退出（秒）；0 = 永不自动退出",
    )
    args = parser.parse_args(argv)

    from studio.server import create_app

    port = args.port or free_port()
    app = create_app()
    server = serve(app, args.host, port)
    url = f"http://{args.host}:{port}/"

    log("=" * 62)
    log("  GI Agent Studio")
    log(f"  界面地址：{url}")
    log(f"  项目目录：{PROJECT_ROOT}")
    log("  关掉应用窗口即可退出；也可在界面里点「退出 Studio」。")
    log("=" * 62)

    threading.Thread(target=background_warmup, args=(app,), daemon=True).start()

    if args.no_window:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    elif args.browser:
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        window = open_app_window(url)
        if window is None:
            log("没找到 Edge/Chrome，已用默认浏览器打开；关闭本进程即退出。")
        else:
            browser = window.args[0] if isinstance(window.args, list) and window.args else "browser"
            log(f"已用 {os.path.basename(browser)} 打开应用窗口（PID {window.pid}）")

        # ⚠️ 不能 wait() 这个进程：Edge/Chrome 在"已有实例"或新 profile 的情况下会把
        # 请求移交给真正的浏览器进程后自己退出，wait() 会立刻返回，于是服务被关掉、
        # 窗口变成"无法连接"。改成：服务一直开着，直到
        #   1) 玩家在界面里点「退出 Studio」，或 2) 长时间没有任何请求，或 3) Ctrl+C。
        try:
            idle = float(args.exit_when_idle)
            while True:
                time.sleep(1)
                if idle > 0:
                    last = app.config.get("LAST_REQUEST", [time.time()])[0]
                    if time.time() - last > idle:
                        log(f"已经 {int(idle)} 秒没有页面请求，自动退出。")
                        break
        except KeyboardInterrupt:
            pass

    stop_children(app)
    server.shutdown()
    log("Studio 已退出。")
    return 0


def stop_children(app):
    """退出前把子进程收掉（Agent / QQ 机器人 / 飞书服务端）。

    ⚠️ 关窗口这条路由宿主处理（它会 `taskkill /T` 连子进程一起杀），但
    **空闲自动退出**和 **Ctrl+C** 只走到这里 —— 以前只 shutdown 了 HTTP 服务，
    Agent / 机器人会变成孤儿继续跑（QQ 那个还握着单实例锁，下次启动会被拒）。
    """
    for key in ("RUNNER", "CHANNELS"):
        target = app.config.get(key)
        if target is None:
            continue
        try:
            if key == "RUNNER":
                target.stop()
            else:
                target.stop_all()
        except Exception as exc:      # pragma: no cover - 退出路径不该再抛异常
            log(f"停止子进程失败（{key}）：{exc}")


if __name__ == "__main__":
    raise SystemExit(main())
