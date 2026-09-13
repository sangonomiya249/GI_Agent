"""原神窗口的前后台管理（BGI 的兜底）。

**为什么需要**：BetterGI 的模拟输入走的是前置 SendInput（它自己的字符串就是
`前台 SendInput：游戏需要保持前台`），而且每秒检查一次前台窗口：

    [WRN] BetterGenshinImpact.GameTask.Common.TaskControl
    当前获取焦点的窗口为: GI-Agent-Studio，不是原神，暂停

玩家实测（2026-09-12）：一条龙跑到一半，日志里 **73 次**「不是原神，暂停」——
前台分别是 QQ（37 次）、**GI-Agent-Studio（22 次，我们自己的窗口）**、SearchHost（12 次）。
表现为"卡死"，其实是在等原神回到前台。

这里只做两件 BGI 自己做不好的事：
  1. **启动后把原神切到前台一次**（`focus_game_window`）：玩家点 y 就是"开始跑图"的意思，
     不把游戏推到前台，BGI 一启动就暂停；
  2. **运行中检测前台**（`foreground_state`）：让哨兵能说出"正在因为前台问题暂停"，
     而不是干等到超时（可选地反复切回，见 `BGI_FOCUS_GUARD`，默认关 —— 免得和玩家抢鼠标）。

实现全走 Win32（ctypes，无新依赖）。**所有函数都"失败即返回 None/False"**：
拿不到窗口、系统不让切前台、非 Windows —— 一律安静降级，绝不让主流程炸掉。
"""

import ctypes
import os

import config

# Windows 常量
SW_RESTORE = 9
SW_SHOW = 5
GAME_WINDOW_CLASS = "UnityWndClass"      # 原神（Unity）主窗口类名，国服/国际服都一样
GAME_TITLE_HINTS = ("原神", "Genshin Impact", "云·原神", "云原神")


def _user32():
    """拿 user32（非 Windows 或受限环境下返回 None）。"""
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.user32
    except Exception:        # noqa: BLE001 —— 受限环境拿不到也不该炸
        return None


def _window_text(user32, hwnd):
    try:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except Exception:        # noqa: BLE001
        return ""


def _window_class(user32, hwnd):
    try:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value
    except Exception:        # noqa: BLE001
        return ""


def game_process_name():
    """游戏进程名（从 BetterGI 的 `genshinStartConfig.installPath` 推，推不出就用 YuanShen）。"""
    try:
        import json

        path = os.path.join(config.BGI_DIR, "User", "config.json")
        with open(path, "r", encoding="utf-8") as handle:
            install_path = str(
                (json.load(handle).get("genshinStartConfig") or {}).get("installPath") or ""
            )
    except Exception:        # noqa: BLE001
        install_path = ""

    name = os.path.basename(install_path.replace("\\", "/")).strip()
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name or "YuanShen"


def _is_game_window(class_name, title):
    """这个窗口是不是**原神**（不是别的 Unity 游戏）。

    ⚠️ 实测踩过的坑：只看窗口类名 `UnityWndClass` 是不够的 —— 家里同时开着
    《明日方舟：终末地》（Endfield）也是 Unity，类名一模一样，于是 `foreground_is_game()`
    会误判"原神在前台"（于是既不去切前台，也把 BGI 的暂停当成正常）。所以类名 + **标题**都要对。
    标题写法：国服「原神」、国际服「Genshin Impact」、云原神「云·原神」。
    """
    if class_name != GAME_WINDOW_CLASS or not title:
        return False
    return any(hint in title for hint in GAME_TITLE_HINTS)


def find_game_window():
    """找原神主窗口句柄；找不到返回 None。

    类名 `UnityWndClass` + 标题里带「原神 / Genshin Impact」才算（理由见 `_is_game_window`）。
    """
    user32 = _user32()
    if user32 is None:
        return None

    found = []

    def _callback(hwnd, _param):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            class_name = _window_class(user32, hwnd)
            title = _window_text(user32, hwnd)
            if _is_game_window(class_name, title):
                found.append(hwnd)
        except Exception:    # noqa: BLE001
            pass
        return True

    try:
        callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_callback)
        user32.EnumWindows(callback, None)
    except Exception:        # noqa: BLE001
        return None

    return found[0] if found else None


def foreground_state():
    """前台窗口的信息：{"hwnd", "title", "class", "is_game"}；判不出来返回 None。"""
    user32 = _user32()
    if user32 is None:
        return None
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        class_name = _window_class(user32, hwnd)
        title = _window_text(user32, hwnd)
    except Exception:        # noqa: BLE001
        return None
    return {
        "hwnd": hwnd,
        "title": title,
        "class": class_name,
        "is_game": _is_game_window(class_name, title),
    }


def foreground_is_game():
    """前台是不是原神。True / False / **None（判不出来）**。"""
    state = foreground_state()
    if state is None:
        return None
    return bool(state["is_game"])


def describe_foreground():
    """给日志/消息用的一句话。"""
    state = foreground_state()
    if state is None:
        return "无法确认前台窗口（非 Windows 或受限环境）"
    label = state["title"] or state["class"] or "(无标题)"
    return f"当前前台窗口：{label}" + ("（原神）" if state["is_game"] else "（不是原神）")


def focus_game_window():
    """把原神窗口切到前台（还原最小化 → SetForegroundWindow）。成功返回 True。

    `SetForegroundWindow` 受 Windows 的"前台锁定"限制：非前台进程直接把别的窗口拉到前台
    往往无效，所以这里用经典的 `AttachThreadInput` 把自己的输入队列挂到当前前台线程上，
    再调用 `SetForegroundWindow` + `BringWindowToTop`。
    """
    user32 = _user32()
    if user32 is None:
        return False
    hwnd = find_game_window()
    if not hwnd:
        return False

    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)

        kernel32 = ctypes.windll.kernel32
        current_thread = kernel32.GetCurrentThreadId()
        foreground = user32.GetForegroundWindow()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0

        attached = False
        if foreground_thread and foreground_thread != current_thread:
            attached = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
    except Exception:        # noqa: BLE001
        return False

    state = foreground_state()
    return bool(state and state["is_game"])


def focus_game_once(reason=""):
    """尽力把原神切到前台并打印结果（给启动路径用）。"""
    state = foreground_state()
    if state is not None and state["is_game"]:
        print(f"🪟 原神已经在最前台{f'（{reason}）' if reason else ''}。")
        return True

    print(f"🪟 {describe_foreground()} → 正在把原神切到前台…")
    if focus_game_window():
        print("🪟 已把原神切到前台。")
        return True

    print(
        "🪟 ⚠️ 没能把原神切到前台（窗口没找到，或 Windows 拦住了切换）。\n"
        "   ↳ BetterGI 会一直「不是原神，暂停」，直到原神重新拿到焦点。\n"
        "   ↳ 根治办法：BetterGI 设置里打开「失去焦点时自动切回原神」"
        "（配置项 otherConfig.restoreFocusOnLostEnabled），让它自己切。"
    )
    return False


def _main(argv=None):
    """自检：python -m skills.window_focus [--focus]

    `--focus` 会**真的把原神窗口拉到前台**（会打断你当前的操作，故意做成要手动加参数才动手）。
    """
    import argparse

    parser = argparse.ArgumentParser(description="原神窗口前后台自检")
    parser.add_argument("--focus", action="store_true", help="真的把原神切到前台试一次（会抢焦点）")
    args = parser.parse_args(argv)

    print(f"🪟 游戏进程名（从 BetterGI 配置推）：{game_process_name()}")
    hwnd = find_game_window()
    print(f"🪟 原神主窗口句柄：{hwnd if hwnd else '没找到'}")
    print(f"🪟 {describe_foreground()}")
    print(f"🪟 前台是不是原神：{foreground_is_game()}")

    if args.focus:
        focus_game_once("手动自检")
    else:
        print("（只做检查；想真试一次切前台加 --focus）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
