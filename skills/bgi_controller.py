import os
import re
import json
import time
import uuid
import subprocess
import threading
import config
from api import channel_router, feishu_api
from skills import bgi_watcher, route_group
from skills import char_boss_match
from skills.boss_pathing_guard import strategy_notice
from skills.char_boss_match import resolve_boss_target
from skills.config_transaction import ConfigTransactionError, JsonConfigTransaction
from skills.domain_match import (
    build_domain_resin_plan,
    describe_domain_resin_plan,
    resolve_domain_target,
)


def describe_changed_files(paths):
    """把"改了哪些文件"说人话：**同名文件要区分开**。

    玩家实测：手机上收到「配置已写入 BetterGI（地图素材.json, 地图素材.json）」——
    两次其实是两个不同目录下的同名文件（`User\\OneDragon\\地图素材.json` 是一条龙配置，
    `User\\ScriptGroup\\地图素材.json` 是脚本组），只写文件名看着像重复。
    """
    parts = []
    for path in paths or []:
        name = path.name
        text = str(path).replace("/", "\\").lower()
        # 先认"按文件名就能确定的角色"（`狗粮\settings.json` 也在 ScriptGroup 下，
        # 但它其实是脚本自己的设置文件，不该被说成"脚本组"），再退回按目录判断
        if name.lower() == "config.json":
            note = "全局配置"
        elif name.lower() == "settings.json":
            note = "脚本设置"
        elif "\\onedragon\\" in text:
            note = "一条龙"
        elif "\\scriptgroup\\" in text:
            note = "脚本组"
        else:
            note = ""
        entry = f"{name}（{note}）" if note else name
        if entry not in parts:
            parts.append(entry)
    return "、".join(parts) if parts else "（无）"


def send_notice(open_id, text, progress=False, chat=True):
    """统一发通知：`progress=True` 的"进度类"消息在聊天通道（QQ）上不发。

    为什么：一次带配置的执行会连着发"⚙️ 指令已确认"→"🧾 配置事务已提交"→"🗂️ 备份与 Diff"→
    "🚀 正在启动"→"🎉 已触发"→"✅ 已确认开始执行"六条，手机上刷屏，而且官方限制
    **同一条玩家消息最多回复 5 次** —— 额度被启动过程吃光后，20 分钟后真正的完成报告就发不出去了。
    进度消息只留在终端/Studio 日志里（`print` 那行照样打）。

    `chat=False`：玩家**明确说过不想在 QQ 上看到**的那类回执（配置写入回执、启动成功回执）。
    他的要求原话是"qq机器人都不要反馈，只要这一句 ✅ 已确认 BetterGI 开始执行任务"，
    所以这些只在终端 / Studio 出现 —— 回滚 ID 在终端和 Studio 日志里，CLI 的 `rollback` 也会自己列事务。

    ⚠️ 报错类、以及**需要玩家动手**的（关不掉 BetterGI、UAC 授权窗口）一律照发，绝不静默。
    """
    if channel_router.wants_quiet(open_id) and (progress or not chat):
        print("（聊天通道跳过这条消息，仅保留在日志里）")
        return False
    return feishu_api.send_feishu_msg(open_id, text)


def _decode_output(raw):
    """schtasks 在中文 Windows 上输出 GBK，这里做多编码兜底。"""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ==========================================
# 🌟 BetterGI 冷启动保障（热启动无效）
# ==========================================
# BetterGI 是【单实例】程序。已有实例在跑时，再启动 BetterGI.exe --startOneDragon，
# 新进程只会通过 IPC（--instance/childsession）把参数转交给已有实例然后退出，
# 一条龙并不会被真正拉起 —— 表现为“触发了但什么都没发生，日志零新增”。
# 另外 BetterGI 退出时可能把内存里的旧配置回写磁盘，覆盖掉 Agent 刚写的配置。
# 所以下发配置前必须先确保它是关闭状态。
BETTERGI_ACTIVE_GRACE_SECONDS = 120
# 计划任务关闭 BetterGI 后，再多等几秒让它的脚本彻底退出，
# 避免它残留的等待循环把随后冷启动的新实例当成“还没关干净”而误杀
BGI_STOP_SETTLE_SECONDS = 4


def _bettergi_pids():
    """返回正在运行的 BetterGI 进程 PID。

    返回 [] 表示确认没在运行；返回 None 表示【无法判断】（权限受限等）。
    这个区分很重要：tasklist 在权限不足时会直接报 Access denied，
    若把这种情况当成“没在运行”，热启动问题就会被静默放过。
    """
    # 首选 tasklist
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq BetterGI.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
        )
        if result.returncode == 0:
            return [int(pid) for pid in re.findall(r'"BetterGI\.exe","(\d+)"', _decode_output(result.stdout))]
    except OSError:
        pass

    # tasklist 被拒时改用 PowerShell 的 Get-Process（权限要求更低，实测仍可枚举）
    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-Process -Name BetterGI -ErrorAction SilentlyContinue).Id",
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            output = _decode_output(result.stdout).strip()
            if not output:
                # ⚠️ 返回码 0 但**没有任何输出**有两种可能：真的没在跑，或权限被静默抑制了。
                # `[]` 在调用方语义是"确认没在运行"（会直接去改配置！），所以这里必须返回 None
                # （判不出来），交给上层按"不确定"处理 —— 这正是本模块反复强调的失败模式。
                return None
            pids = [int(x) for x in re.findall(r"\d+", output)]
            return pids if pids else None
    except OSError:
        pass

    return None


def _bettergi_log_recently_written(seconds=BETTERGI_ACTIVE_GRACE_SECONDS):
    """日志最近还在更新 —— 只能说明"它醒着"，**不等于在跑任务**。

    ⚠️ 实测的坑：一条龙 12:34:40 就跑完了，BGI 还开着窗口、空闲时也会零零碎碎写日志，
    于是用户 12:36 再下令时被判成"正在跑任务"而拒绝执行 —— 他只好把指令**重下一遍**。
    所以真正判断"是不是在跑"要用 `_bettergi_task_state()`（看日志里终态标记和启动标记谁在后面）。
    """
    try:
        return (time.time() - os.path.getmtime(bgi_watcher.today_log_path())) < seconds
    except OSError:
        return False


# 日志里"任务开始"的痕迹（按这些出现的位置判"最近一次是开始还是结束"）
_BGI_START_MARKERS = (
    '→ "任务启动！"',
    "开始执行地图追踪任务",
    "加载完成，共",
)
# "这次跑完了/被停掉/游戏没了"——都说明当前没有任务在跑
_BGI_TERMINAL_MARKERS = (
    "一条龙和配置组任务结束",
    "任务被取消",
    "主窗体退出",
    "游戏已退出",
)
_GROUP_DONE_RE = re.compile(r'配置组\s*"([^"]+)"\s*执行结束')


def _bettergi_task_state(log_bytes=200_000):
    """BetterGI 是不是真的在跑任务：'running' / 'idle' / 'unknown'。

    只看当天日志的**尾部**（默认最后 200 KB）：谁在最后面 —— 终态标记（跑完了/被停/游戏退出）
    还是启动标记（任务启动/开始追踪/配置组加载）—— 就说明它现在是空着还是在干活。
    """
    try:
        path = bgi_watcher.today_log_path()
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            if size > log_bytes:
                handle.seek(size - log_bytes)
                handle.readline()          # 丢掉被截断的半行
            text = handle.read()
    except OSError:
        return "unknown"

    if not text.strip():
        return "unknown"

    last_terminal = max((text.rfind(marker) for marker in _BGI_TERMINAL_MARKERS), default=-1)
    group_done = None
    for match in _GROUP_DONE_RE.finditer(text):
        group_done = match.end()
    if group_done is not None:
        last_terminal = max(last_terminal, group_done)
    last_start = max((text.rfind(marker) for marker in _BGI_START_MARKERS), default=-1)

    if last_terminal < 0 and last_start < 0:
        return "unknown"
    return "idle" if last_terminal > last_start else "running"


def _bettergi_looks_busy():
    """要不要当成"在跑任务"（宁可多等，也别打断玩家正在跑的一条龙）。"""
    state = _bettergi_task_state()
    if state == "running":
        return True
    if state == "idle":
        return False
    # 判不出来（日志里没有任何标记）：退回"最近有没有写日志"
    return _bettergi_log_recently_written()


def _wait_for_bettergi_idle(open_id, wait_seconds=None, poll_seconds=None):
    """等 BetterGI 把当前任务跑完（轮询），返回 True = 已经空下来（或干脆退出了）。

    玩家实测的需求："一条龙跑完了但被判成还在跑，还得重新下一次指令" ——
    所以这里**不是直接拒绝**，而是等它跑完自动继续，并且只在开始时提醒一次（手机上别刷屏）。
    """
    minutes = getattr(config, "BGI_WAIT_RUNNING_TASK_MINUTES", 20)
    wait_seconds = (minutes * 60) if wait_seconds is None else wait_seconds
    poll_seconds = (
        getattr(config, "BGI_RUNNING_TASK_POLL_SECONDS", 10)
        if poll_seconds is None else poll_seconds
    )
    deadline = time.time() + max(0, wait_seconds)
    notified = False

    while True:
        pids = _bettergi_pids()
        if pids == []:
            print("✅ BetterGI 已经退出，继续执行本轮计划。")
            return True
        if not _bettergi_looks_busy():
            print("✅ BetterGI 已空闲（日志里出现了结束标记），继续执行本轮计划。")
            return True
        if time.time() >= deadline:
            return False
        if not notified:
            notified = True
            print(f"⏳ BetterGI 正在跑任务，先等它跑完（最多等 {minutes} 分钟）…")
            send_notice(
                open_id,
                f"⏳ 检测到 BetterGI 正在跑任务：先等它跑完再自动继续（最多等 {minutes} 分钟），"
                "你不用重新下一次指令。",
            )
        time.sleep(max(1, poll_seconds))


def _wait_until_bettergi_gone(seconds):
    """轮询等待 BetterGI 退出，返回是否已退出。"""
    for _ in range(max(1, int(seconds))):
        time.sleep(1)
        if _bettergi_pids() == []:
            return True
    return _bettergi_pids() == []


def _close_bettergi_direct():
    """直接用 taskkill 关闭（仅当当前终端权限足够时有效）。返回 (是否成功, 错误信息)。"""
    err = ""
    for args in (["taskkill.exe", "/IM", "BetterGI.exe"],
                 ["taskkill.exe", "/F", "/IM", "BetterGI.exe"]):
        try:
            result = subprocess.run(args, capture_output=True)
        except OSError as exc:
            err = str(exc)
            continue
        if result.returncode == 0 and _wait_until_bettergi_gone(10):
            return True, ""
        err = (
            _decode_output(result.stderr) or _decode_output(result.stdout) or err
        ).strip()
        if result.returncode == 0:
            return True, ""
    return False, err


def _stop_pid_file():
    """与 scripts/stop_bettergi.ps1 约定的 PID 记录文件路径。

    用【仓库路径】而不是 %TEMP%/%LOCALAPPDATA%：两边都能从自身位置确定性推导，
    不依赖环境变量，跨会话/跨权限也一致（计划任务以最高权限运行时环境变量可能不同）。
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, "scripts", "stop_bettergi.pids")


def _write_stop_pid_file(pids):
    """把要关闭的 PID 写下来，供同等权限的计划任务精确关闭。

    ⚠️ 必须精确到 PID：计划任务脚本若按【进程名】关闭，会踩到竞态 ——
    它杀完旧实例后等待循环还没退出，此时新实例已经冷启动，它会把新实例一起杀掉
    （表现为“刚拉起来约 1 秒又被关掉”）。
    """
    path = _stop_pid_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"created={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
            for pid in pids:
                f.write(f"{pid}\n")
        return path
    except OSError as exc:
        print(f"⚠️ 无法写入关闭目标文件 {path}: {exc}")
        return ""


def _close_bettergi_via_task():
    """通过同等权限的计划任务 StopBetterGI 关闭 BetterGI（免 UAC）。"""
    for task_path in (r"\StopBetterGI", "StopBetterGI"):
        try:
            probe = subprocess.run(["schtasks.exe", "/query", "/tn", task_path], capture_output=True)
            if probe.returncode != 0:
                continue
            run = subprocess.run(["schtasks.exe", "/run", "/tn", task_path], capture_output=True)
        except OSError:
            continue
        if run.returncode == 0 and _wait_until_bettergi_gone(25):
            return True
    return False


def ensure_bettergi_closed(open_id):
    """确保 BetterGI 处于关闭状态，返回 True 表示可以继续执行。

    - 没在运行                → 直接放行
    - 在跑任务（日志里"启动"在后面）→ **等它跑完再继续**（最多 `BGI_WAIT_RUNNING_TASK_MINUTES` 分钟），
      而不是直接拒绝 —— 实测玩家会因为一次误判被迫重新下一遍指令
    - 在运行但已空闲          → 关闭它，然后冷启动

    ⚠️ "在跑任务"用 `_bettergi_task_state()` 判定（看日志里的终态/启动标记），
    不再用"日志最近有写入" —— 一条龙跑完后 BGI 开着窗口也会零碎写日志（实测误判）。
    """
    pids = _bettergi_pids()

    if pids is None:
        print("⚠️ 无法确认 BetterGI 是否在运行（tasklist 与 Get-Process 都被拒绝）。")
        print("   将继续执行；若随后一条龙没反应，请先手动关闭 BetterGI 再试。")
        return True

    if not pids:
        return True

    # ① 可能正在跑任务 → 先等（用户明确要求的"重试"，别让他重新下令）
    if _bettergi_looks_busy():
        print(f"⏳ 检测到 BetterGI 正在运行（PID {pids}）且像是在跑任务，先等它跑完…")
        if not _wait_for_bettergi_idle(open_id):
            minutes = getattr(config, "BGI_WAIT_RUNNING_TASK_MINUTES", 20)
            msg = (
                f"⛔ 等了 {minutes} 分钟，BetterGI 仍在跑任务，本轮先不执行（免得打断它）。\n"
                "   你可以：等它跑完再让我执行（我会自动继续，不用重新下令）；\n"
                "   或者手动关掉 BetterGI 后再说一次。\n"
                "   原因：BetterGI 是单实例程序，热启动无法触发新的一条龙。"
            )
            print(msg)
            feishu_api.send_feishu_msg(open_id, msg)
            return False
        pids = _bettergi_pids()
        if pids == []:
            return True
        if not _bettergi_looks_busy():
            print("🔄 它已经空闲下来了，接着关闭它并冷启动…")
        else:
            return False

    print(f"🔄 检测到 BetterGI 正在运行（PID {pids}，空闲状态）。")
    print("   它不支持热启动：重复启动不会触发一条龙，退出时还可能回写旧配置。")
    print("   正在先关闭它，稍后冷启动…")

    ok, err = _close_bettergi_direct()
    if ok:
        print("✅ BetterGI 已关闭。")
        return True

    if err:
        print(f"   直接结束失败：{err}")
    print("   BetterGI 通常以管理员权限运行，普通终端没有权限结束它。")

    # 🌟 关键：把要关的 PID 精确记下来再交给计划任务。
    # 若计划任务按进程名关，会误杀我们随后冷启动的新实例。
    pid_file = _write_stop_pid_file(pids)
    if pid_file:
        print("   改用计划任务 StopBetterGI（与 BetterGI 同等权限、按 PID 精确关闭）…")

        if _close_bettergi_via_task():
            # 等计划任务脚本收尾退出，避免它残留的等待循环盯上即将启动的新实例
            time.sleep(BGI_STOP_SETTLE_SECONDS)
            print("✅ 已通过计划任务 StopBetterGI 关闭 BetterGI。")
            return True

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    msg = (
        "❌ 无法关闭 BetterGI（当前终端权限不足，且没有可用的 StopBetterGI 计划任务）。\n"
        "   请任选其一后重试：\n"
        "   1) 以【管理员身份】运行一次下面这条命令，它会注册 StartBetterGI / StopBetterGI / StopGenshin\n"
        "      （关 BGI、关原神各一个），之后 Agent 就能免 UAC 自动关闭它们了（只需做一次）。\n"
        "      ⚠️ 用完整路径：管理员 PowerShell 的默认目录是 C:\\WINDOWS\\system32，相对路径会找不到文件。\n"
        f'      powershell -ExecutionPolicy Bypass -File "{repo}\\scripts\\setup_start_bettergi_task.ps1"\n'
        "   2) 手动关闭 BetterGI 窗口后再说一次。"
    )
    print(msg)
    feishu_api.send_feishu_msg(open_id, msg)
    return False


# 🌟 一轮计划执行的互斥锁：一轮里可能"等 BetterGI 跑完"（最长 20 分钟），
#    等待期间玩家再发一条指令时，不能让两个线程同时改配置 / 各冷启动一次。
_ROUND_LOCK = threading.Lock()


def _filter_gather_cooldown(items, force=False):
    """把采集目标里"还在 48 小时冷却里"的挑出来。返回 (保留的目标, 说明行)。

    · 目标是材料名（「霜仙花」）→ 直接查冷却；
    · 目标是角色名（「蓝砚」「奥黛塔的突破材料」）→ 先解析成 TA 的采集物再查；
    · 解析不出来的目标**保留**（交给下游按老逻辑处理：找不到路线会提示玩家），
      但会在说明里写清楚"认不出"，免得静默消失；
    · `force=True`（玩家说了「强制采集」）→ 全部保留，只把状态写进说明。
    """
    try:
        from skills import gather_cooldown
    except Exception as exc:        # noqa: BLE001
        print(f"⚠️ 采集冷却检查不可用（跳过）：{exc}")
        return list(items), []

    kept, notices = [], []
    for item in items:
        target = str(item or "").strip()
        if not target:
            continue
        material, note = gather_cooldown.resolve_material(target)
        if not material:
            kept.append(item)
            notices.append(f"⚠️ 「{target}」：{note}")
            continue
        result = gather_cooldown.status(material)
        if result["cooling"]:
            if force:
                kept.append(item)
                notices.append(gather_cooldown.describe(result) + "（你要求强制采集，照跑）")
            else:
                notices.append(gather_cooldown.describe(result))
            continue
        kept.append(item)
    return kept, notices


def focus_game_for_launch():
    """启动一条龙之后把原神切到前台一次（BGI 的模拟输入要求游戏在前台）。

    实测（玩家日志）：一条龙跑起来后如果前台是 QQ / Studio / 搜索框，BGI 每秒打一行
    「当前获取焦点的窗口为: X，不是原神，暂停」—— 表现为"卡死"，其实在等原神回前台。
    玩家点 y 就是"开始跑图"的意思，所以这一次主动切前台是符合他意图的（可用
    `BGI_FOCUS_ON_LAUNCH=0` 关掉）。运行中反复抢焦点是另一回事，见 `BGI_FOCUS_GUARD`。
    """
    if not config.BGI_FOCUS_ON_LAUNCH:
        return False
    try:
        from skills import window_focus

        return window_focus.focus_game_once("刚触发一条龙")
    except Exception as exc:        # noqa: BLE001 —— 切不了前台也不能影响启动
        print(f"🪟 ⚠️ 切前台失败（不影响启动）：{type(exc).__name__} {exc}")
        return False


def _launch_bettergi(open_id):
    """优先用计划任务免 UAC 拉起 BetterGI 一条龙；任务不存在时回退为直接启动。"""
    print("🚀 正在通过任务计划启动 BetterGI 一条龙...")
    send_notice(open_id, "🚀 正在通过任务计划启动 BetterGI 一条龙...", progress=True)

    # 🌟 先探测任务是否存在，这样能给出准确的失败原因，而不是笼统的一句“找不到指定的文件”
    task_exists = False
    for task_path in (r"\StartBetterGI", "StartBetterGI"):
        probe = subprocess.run(
            ["schtasks.exe", "/query", "/tn", task_path],
            capture_output=True,
        )
        if probe.returncode == 0:
            task_exists = True
            break

    if task_exists:
        last_error = "未知错误"
        for task_path in (r"\StartBetterGI", "StartBetterGI"):
            result = subprocess.run(
                ["schtasks.exe", "/run", "/tn", task_path],
                capture_output=True,
            )
            if result.returncode == 0:
                print("🎉 任务计划已触发，BetterGI 正在执行一条龙。")
                # ⚠️ 这条在 QQ 上**不发**：玩家要求"qq机器人都不要反馈，只要那一句
                #    ✅ 已确认 BetterGI 开始执行任务"（那句由哨兵确认真的开跑之后发）。
                #    终端 / Studio / 飞书 照旧能看到"启没启动"。
                send_notice(
                    open_id,
                    "🎉 已触发一条龙（任务计划 StartBetterGI）。\n"
                    "　 正在等 BetterGI 真正开始跑，确认后再回你一条。",
                    chat=False,
                )
                focus_game_for_launch()
                bgi_watcher.start_completion_watch(open_id)
                return True
            last_error = (_decode_output(result.stderr) or _decode_output(result.stdout) or "未知错误").strip()

        print(f"❌ 计划任务存在但触发失败: {last_error}")
    else:
        from skills.game_control import setup_task_hint

        print("❌ 未找到名为 StartBetterGI 的计划任务（README 第 4 步尚未完成）。")
        print(f"💡 一键修复：以管理员身份运行 {setup_task_hint()}")

    # 🌟 兜底：计划任务不可用时，直接拉起 BetterGI（非管理员会弹 UAC 授权窗口）
    exe_path = os.path.join(config.BGI_DIR, config.BGI_EXE)
    if not os.path.exists(exe_path):
        msg = f"❌ 计划任务不可用，且找不到 BetterGI 可执行文件: {exe_path}"
        print(msg)
        feishu_api.send_feishu_msg(open_id, msg)
        return False

    print(f"🔁 回退方案：直接启动 {exe_path} --startOneDragon（可能弹出 UAC 授权窗口）")
    send_notice(open_id, "🔁 回退方案：正在直接启动 BetterGI 一条龙...", progress=True)
    try:
        subprocess.Popen(
            [exe_path, "--startOneDragon"],
            cwd=config.BGI_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        msg = f"❌ 直接启动 BetterGI 失败: {exc}"
        print(msg)
        feishu_api.send_feishu_msg(open_id, msg)
        return False

    print("🎉 已直接拉起 BetterGI 一条龙（若弹出 UAC 窗口请手动点“是”）。")
    # 同上：QQ 上不发（回退方案要玩家点 UAC 时也一样 —— 没点的话，哨兵 3 分钟后会发
    # 「仍未检测到一条龙运行迹象」，那条提示里已经写明 UAC 这个可能原因）
    send_notice(
        open_id,
        "🎉 已直接拉起 BetterGI（走的是回退方案）。\n"
        "　 如果弹出 UAC 授权窗口，需要你手动点「是」；确认开始跑之后再回你一条。",
        chat=False,
    )
    focus_game_for_launch()
    bgi_watcher.start_completion_watch(open_id)
    return True


# ==========================================
# 🌟 BetterGI 一条龙配置定位（关键修复）
# ==========================================
# 坑 1：一条龙任务开关存在 User/OneDragon/<配置名>.json 里，而“当前生效的是哪一份配置”
#       由 User/config.json 的 selectedOneDragonFlowConfigName 决定。若照着 .env 里
#       写死的文件名去改，很可能改到用户根本没在用的那份配置上 —— 所有修改静默失效。
# 坑 2：TaskEnabledList 的键是【任务 Id】，不是显示名。JS 脚本类任务（例如「地图素材」）
#       的 Id 是订阅时分配的 UUID，必须拿显示名到 TaskDefinitions 里反查；
#       直接写 TaskEnabledList["地图素材"] 会被 BetterGI 当作无关键忽略。
_ONE_DRAGON_TASK_KEYS = {
    "run_domain": ("自动秘境",),
    "run_leyline": ("自动地脉花",),
    # 🌟 Boss 讨伐走的是《批量讨伐角色养成材料BOSS》这个 **JS 脚本组**（Agent 会把
    #    队伍/策略/次数写进它的 assets/config/config.json），**不是** BetterGI 内置的
    #    「自动首领讨伐」：
    #      · 内置那条读的是 `OneDragonFlowConfig.AutoBossName`，Agent 从来没写过它，
    #        开起来只会打印"一条龙配置内未选择需要讨伐的首领，跳过"；
    #      · 早先这里写过占位名 "AutoBoss" / "突破材料"，在旧格式下是"死键"（BGI 会跳过），
    #        但一旦自动登记生效，就会在玩家的一条龙里凭空多出一个跑不动的 AutoBoss 任务。
    "run_boss": ("批量讨伐角色养成材料BOSS",),
}


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_one_dragon_config_path():
    """返回 BetterGI 当前真正生效的一条龙配置文件路径。"""
    selected = None
    try:
        if os.path.exists(config.BGI_GLOBAL_CONFIG):
            selected = (_read_json(config.BGI_GLOBAL_CONFIG) or {}).get(
                "selectedOneDragonFlowConfigName"
            )
    except Exception as exc:
        print(f"⚠️ 读取 BetterGI 全局配置失败，无法判断当前一条龙配置: {exc}")

    one_dragon_dir = os.path.dirname(config.BGI_ONE_DRAGON_CONFIG)
    if selected:
        candidate = os.path.join(one_dragon_dir, f"{selected}.json")
        if os.path.exists(candidate):
            if os.path.abspath(candidate) != os.path.abspath(config.BGI_ONE_DRAGON_CONFIG):
                print(
                    f"🎯 BetterGI 当前生效的一条龙配置是【{selected}】，已自动改为写入该文件"
                    f"（.env 指定的 {os.path.basename(config.BGI_ONE_DRAGON_CONFIG)} 并非当前配置，改它没有用）。"
                )
            return candidate
        print(f"⚠️ BetterGI 选中的一条龙配置【{selected}】不存在，回退到 .env 指定的配置。")

    return config.BGI_ONE_DRAGON_CONFIG


def resolve_task_key(bgi_config, candidates):
    """把任务显示名解析成 TaskEnabledList 真正使用的键（脚本组任务是 UUID）。"""
    definitions = bgi_config.get("TaskDefinitions") or {}
    if isinstance(definitions, dict):
        for candidate in candidates:
            if candidate in definitions:
                return candidate
            for task_id, name in definitions.items():
                if name == candidate:
                    return task_id
    return candidates[0]


def task_definitions(bgi_config):
    definitions = (bgi_config or {}).get("TaskDefinitions")
    return definitions if isinstance(definitions, dict) else {}


def uses_task_definitions(bgi_config):
    """这条一条龙配置是不是"新格式"（TaskDefinitions 非空 → 键必须是任务 Id）。

    源码依据（`OneDragonFlowViewModel.LoadDisplayTaskListFromConfig`）：

        bool isOldFormat = TaskDefinitions == null || TaskDefinitions.Count == 0;
        foreach (var key in orderedKeys) {
            if (!TaskEnabledList.TryGetValue(key, out var enabled)) continue;
            if (isOldFormat) { taskItem = new OneDragonTaskItem(key) { IsEnabled = enabled }; }
            else {
                if (!TaskDefinitions.TryGetValue(key, out var name)) continue;   // ★ 直接跳过
                taskItem = new OneDragonTaskItem(name, key) { IsEnabled = enabled };
            }
        }

    也就是说：**新格式下往 TaskEnabledList 里塞「组名: true」会被直接 continue 掉**，
    你以为启用了，其实 BetterGI 连这个任务都不认识 —— 「骗骗花」就是这么静默失效的。
    运行阶段则是 `Path.Combine(User\\ScriptGroup, $"{task.Name}.json")` 按**任务名**取脚本组，
    所以只要 TaskDefinitions 里有「新 Id → 组名」，任务就能正常跑。
    """
    return bool(task_definitions(bgi_config))


def register_task(bgi_config, name):
    """把脚本组登记成一条龙任务，返回 (任务 Id, 是否新建)。

    等价于玩家在 BetterGI「一条龙」界面点一次"添加脚本组"：
    `TaskDefinitions[新 UUID] = 组名`。已经登记过就直接复用原 Id（避免出现重复项）。
    """
    name = str(name)
    definitions = bgi_config.get("TaskDefinitions")
    if not isinstance(definitions, dict):
        definitions = {}
        bgi_config["TaskDefinitions"] = definitions

    for task_id, value in list(definitions.items()):
        if str(value) == name:
            return str(task_id), False

    task_id = str(uuid.uuid4())
    definitions[task_id] = name
    return task_id, True


def script_group_file(name, group_dir=None):
    """这个任务名在 `User\\ScriptGroup` 下有没有对应的脚本组文件。

    运行阶段 BetterGI 就是按名字去取 `User\\ScriptGroup\\<任务名>.json` 的，
    所以"有文件"是"这条任务真的能跑"的唯一凭据。
    """
    name = str(name or "").strip()
    if not name:
        return ""
    directory = group_dir or config.BGI_SCRIPT_GROUP_DIR
    path = os.path.join(directory, f"{name}.json")
    return path if os.path.isfile(path) else ""


def set_task_enabled(bgi_config, enabled, candidates):
    """按任务 Id 设置开关；启用时确保该任务出现在 TaskOrder 中，否则不会被调度执行。

    🌟 启用一个**没登记过的脚本组**时会自动补登记（新 UUID）—— 否则那次写入是死配置：
    新格式配置会按源码直接跳过没有 TaskDefinitions 条目的键。

    ⚠️ 但**只对"真的有脚本组文件"的任务这么做**。踩过的坑：`run_boss` 早先用占位名
    `"AutoBoss"`，自动登记生效后就在玩家的一条龙里凭空多出一个跑不动的 AutoBoss 任务
    （BGI 内置任务表里没有这个名字 → 脚本组文件也不存在 → 执行时直接报错跳过）。
    BetterGI 内置任务（自动秘境/自动地脉花/自动首领讨伐…）**一律不代登记**：
    它们各自读自己的配置字段，替玩家打开一个没配好的内置任务只会添乱。
    返回 (任务 Id, 是否新建了登记项)。
    """
    bgi_config.setdefault("TaskEnabledList", {})
    candidates = tuple(candidates or ())
    key = resolve_task_key(bgi_config, candidates)
    created = False

    if key not in task_definitions(bgi_config) and candidates:
        target = candidates[0]
        new_format = uses_task_definitions(bgi_config)
        if new_format and enabled:
            if script_group_file(target):
                key, created = register_task(bgi_config, target)
            else:
                # 不是脚本组（BGI 内置任务名，或写错的占位名）：不代登记、也不写死键
                return key, False
        elif new_format and not enabled:
            # 新格式下这种名字键本来就是死的：顺手清掉（多半是历史版本写坏的）
            bgi_config["TaskEnabledList"].pop(key, None)
            return key, False
        # 旧格式（TaskDefinitions 为空）里"名字就是键"，BetterGI 自己也是这么兜底的 → 照旧写

    bgi_config["TaskEnabledList"][key] = enabled
    if enabled:
        order = bgi_config.setdefault("TaskOrder", [])
        if isinstance(order, list) and key not in order:
            order.append(key)
    return key, created


def tasks_enabled_now(bgi_config, names):
    """这些任务名里，此刻**真的开着**的有哪些。

    为什么要单独看状态：`set_task_enabled()` 返回 `(任务键, 是否新建了登记项)`，
    拿它做 if 判断**恒为真** —— 不先确认状态就会给每个不存在/本来关着的任务都印一句"已关闭"。
    """
    enabled_list = (bgi_config or {}).get("TaskEnabledList")
    if not isinstance(enabled_list, dict):
        return []
    old_format = not uses_task_definitions(bgi_config)
    definitions = task_definitions(bgi_config)
    active = []
    for name in dict.fromkeys(str(item) for item in (names or ())):
        key = resolve_task_key(bgi_config, (name,))
        if not old_format and key not in definitions:
            continue                      # 新格式下这种键 BetterGI 直接跳过，不用动它
        if bool(enabled_list.get(key)):
            active.append(name)
    return sorted(active)


def prune_task_list(bgi_config):
    """清掉 TaskEnabledList / TaskOrder 里"没有 TaskDefinitions 条目"的死键，返回清理条数。

    BetterGI 会直接跳过这些键，留着只会让人以为"任务已经加进去了"。
    只在**新格式**（TaskDefinitions 非空）下清理；旧格式里名字键是合法的。
    """
    definitions = task_definitions(bgi_config)
    if not definitions:
        return 0

    removed = 0
    enabled_list = bgi_config.get("TaskEnabledList")
    if isinstance(enabled_list, dict):
        for key in [k for k in enabled_list if k not in definitions]:
            enabled_list.pop(key, None)
            removed += 1
    order = bgi_config.get("TaskOrder")
    if isinstance(order, list):
        kept = [key for key in order if key in definitions]
        removed += len(order) - len(kept)
        bgi_config["TaskOrder"] = kept
    return removed


# ==========================================
# 🌟 Agent 自己登记过的一条龙任务：用一个状态文件记住，下一轮没排它就摘掉
# ==========================================
AGENT_TASK_STATE = config.project_path("memory", "agent_registered_tasks.json")


def agent_task_state_path():
    """状态文件路径：优先读 config，方便测试指到临时目录（别写坏玩家真实文件）。"""
    return getattr(config, "AGENT_TASK_STATE_PATH", "") or AGENT_TASK_STATE


def _agent_managed_task_names():
    """Agent **独占**的一条龙任务名：三个内置动作（自动秘境 / 自动地脉花 / 批量讨伐…BOSS）。

    这三个只有 Agent 会开（玩家手开一个没配好的内置秘境，BGI 也只会打印"未选择秘境，跳过"），
    所以它们**无条件**参与"本轮没排就关掉"—— 玩家实测的坑就是这么来的：他的一条龙里本来就登记着
    「批量讨伐角色养成材料BOSS」，Agent 打 Boss 那轮把它打开了，之后每次启动一条龙都顺带打一次 Boss。

    类目组（地图素材/敌人与魔物/锄大地/矿物/食材与炼金）**不在这里**：玩家自己也会手动开它们跑一条龙，
    所以那些只按状态文件里"Agent 开过的"名单清（见 load_agent_enabled_tasks）；
    Agent 自己加进去的整脚本组走 added 名单（见 load_agent_tasks）。三类互不越界。
    """
    names = set()
    for candidates in _ONE_DRAGON_TASK_KEYS.values():
        names.update(str(item) for item in candidates)
    return {name for name in names if name}


def load_agent_tasks(path=None):
    """Agent 自己往一条龙里加过哪些组（玩家手动加的不算）。"""
    data = _load_agent_state(path)
    if isinstance(data, list):          # 旧格式：就是一个名字数组
        return [str(name) for name in data if str(name).strip()]
    if isinstance(data, dict):
        return [str(name) for name in (data.get("added") or []) if str(name).strip()]
    return []


def load_agent_enabled_tasks(path=None):
    """Agent **开启过**（但不一定是它加进去的）哪些组。

    为什么单独记一份：玩家自己就有一条龙任务（例如「批量讨伐角色养成材料BOSS」），
    Agent 打 Boss 那轮会把它打开 —— 但它不在"Agent 加过的"名单里，于是以前**永远不会被关掉**，
    之后每次一条龙都顺带跑一次 Boss（实测：玩家只让 Agent 去刷材料，结果每次启动都去打急冻树）。
    """
    data = _load_agent_state(path)
    if isinstance(data, dict):
        return [str(name) for name in (data.get("enabled") or []) if str(name).strip()]
    return []


def _load_agent_state(path=None):
    try:
        with open(path or agent_task_state_path(), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save_agent_tasks(names, path=None, enabled=None):
    """保存状态：`added` = Agent 加进去的任务，`enabled` = Agent 开过的任务。

    ⚠️ `path` 必须留在第二个位置参数：老调用方是 `save_agent_tasks(names, path)` 这种位置写法
    （测试里就有），插到前面会让路径被当成 enabled、把状态写进默认文件。
    """
    target = path or agent_task_state_path()
    current = _load_agent_state(target)
    previous_enabled = []
    if isinstance(current, dict):
        previous_enabled = list(current.get("enabled") or [])
    payload = {
        "added": sorted({str(name) for name in names if str(name).strip()}),
        "enabled": sorted({str(name) for name in (enabled if enabled is not None else previous_enabled) if str(name).strip()}),
    }
    try:
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def retire_task(bgi_config, name):
    """把 Agent 自己加过的任务项从一条龙里摘掉（三个地方同时清）。"""
    definitions = task_definitions(bgi_config)
    ids = [key for key, value in list(definitions.items()) if str(value) == str(name)]
    if not ids:
        return False

    enabled_list = bgi_config.get("TaskEnabledList")
    order = bgi_config.get("TaskOrder")
    for task_id in ids:
        definitions.pop(task_id, None)
        if isinstance(enabled_list, dict):
            enabled_list.pop(task_id, None)
        if isinstance(order, list):
            bgi_config["TaskOrder"] = [key for key in order if key != task_id]
            order = bgi_config["TaskOrder"]
    return True


def _apply_route_group(
    bgi_config,
    group_path,
    targets,
    label,
    task_candidates,
    fields=None,
    both_ways=False,
    group=None,
    own_prefix=None,
    skip_tokens=(),
):
    """按目标开关一个调度器脚本组里的路线（地图素材 / 敌人与魔物 / 锄大地 共用）。

    组文件不存在时返回 None。命中 0 条时**不启用**该组的一条龙任务
    —— 否则 BetterGI 会跑一个只有防闪退隔离带的空组。
    group 参数可以让调用方传入"完整清单"（归档版），从而改写 UI 里那份被精简过的组。
    own_prefix / skip_tokens：类目隔离（锄大地 ≠ 敌人与魔物）与"低效/不跑"子目录过滤。
    """
    if group is None:
        group = route_group.load_group(group_path)
    else:
        group = dict(group)
    if not group:
        return None

    group["_path"] = str(group_path)
    result = route_group.apply_targets(
        group,
        targets,
        fields or route_group.DEFAULT_MATCH_FIELDS,
        both_ways,
        own_prefix=own_prefix,
        skip_tokens=skip_tokens,
    )
    candidates = tuple(task_candidates or (result.name,))

    # 🌟 战斗策略校验：组里配的策略名在 User/AutoFight 下没有对应 txt 时，
    # BetterGI 走到怪点会抛「战斗策略文件不存在」并中断整条路线。
    strategy_warning = route_group.group_strategy_notice(group)
    if strategy_warning:
        print(strategy_warning)

    if result.matched:
        task_key, task_created = set_task_enabled(bgi_config, True, candidates)
        task_note = f"已启用一条龙任务「{result.name}」(Id: {task_key})" + _registration_notice(
            result.name, task_created, bgi_config
        )
    else:
        set_task_enabled(bgi_config, False, candidates)
        task_note = f"没有命中任何路线，已停用一条龙任务「{result.name}」"
        task_created = False

    return {
        "path": str(group_path),
        "data": group,
        "label": label,
        "name": result.name,
        "matched": result.matched,
        "task_created": bool(task_created),
        "notice": (
            f"{route_group.describe_result(result, '、'.join(str(t) for t in targets))}"
            f"\n   ↳ {task_note}"
            + (f"\n   ↳ {strategy_warning}" if strategy_warning else "")
        ),
        "strategy_warning": strategy_warning,
    }


def _enemy_targets_with_drops(targets, drops_path=None):
    """把材料名也当成敌人目标的别名（「原素花蜜」→ 骗骗花系）。

    玩家更常说材料名而不是敌人类别名，这个映射来自 memory/boss_drops_dict.json。
    """
    expanded = list(targets)
    for target in targets:
        for enemy in route_group.enemy_names_from_drops(
            target, drops_path or char_boss_match.BOSS_DROPS_PATH
        ):
            if enemy not in expanded:
                expanded.append(enemy)
    return expanded


def _apply_category_groups(
    bgi_config, items, category, group_dir=None, preferred_path=None, max_group_size=None
):
    """把一类自由任务目标翻译成脚本组并开关路线（敌人与魔物 / 矿物 / 食材与炼金 共用）。

    返回 [{"path","data","label","notice"}]，或 [{"warning": "…"}] 表示没接上。
    选择顺序（**小组优先**）：
      1. 按目标名找**已分好类的组**（蕈兽 / 骗骗花 / 石珀 / 虹滴晶…），
         在体量合格的前提下挑命中最多、体积最小的那个；
      2. 只有找不到小组时才用该类目的总组（如「敌人与魔物」），且超过安全体量按
         BGI_ROUTE_GROUP_POLICY 处理（默认 shrink：自动精简 + 归档）；
      3. 都没命中就给出可操作提示。
    无论走哪条路，只要没用总组，就把总组的一条龙任务关掉，避免上一次残留的启用状态
    让它继续跑上千条路线。
    """
    group_dir = group_dir or config.BGI_SCRIPT_GROUP_DIR
    preferred_path = preferred_path or route_group.category_group_path(category, group_dir)
    limit = config.BGI_MAX_ROUTE_GROUP_SIZE if max_group_size is None else max_group_size
    targets = [str(item).strip() for item in (items or []) if str(item).strip()]
    if not targets:
        return []

    # 🌟 类目隔离 + 口语展开：锄大地 → 锄地专区，且只认本类目前缀下的路线
    #    （锄地专区里有 `0_0_飞萤`、`…三骗骗花` 这种路线名，不隔离会被「敌人与魔物」抢走）
    search_terms = route_group.category_search_terms(category, targets)
    own_prefix = category.folder_prefix
    skip_tokens = route_group.effective_skip_tokens(category, targets)

    preferred = route_group.load_group(preferred_path)
    preferred_name = str(
        (preferred or {}).get("name") or os.path.splitext(os.path.basename(preferred_path))[0]
    )
    registered = _registered_task_names()
    preferred_registered = preferred_name in registered

    def disable_preferred():
        if preferred:
            set_task_enabled(bgi_config, False, (preferred_name,))

    # 1) 先找按目标分好的小组
    expanded = (
        _enemy_targets_with_drops(search_terms) if category.expand_drops else list(search_terms)
    )
    hits = route_group.find_groups_for_targets(
        group_dir,
        search_terms,
        category.match_fields,
        exclude=(config.BGI_MAP_CONFIG, preferred_path),
        both_ways=category.both_ways,
        own_prefix=own_prefix,
        skip_tokens=skip_tokens,
    )
    if not hits and expanded != search_terms:
        search_terms = expanded
        hits = route_group.find_groups_for_targets(
            group_dir,
            search_terms,
            category.match_fields,
            exclude=(config.BGI_MAP_CONFIG, preferred_path),
            both_ways=category.both_ways,
            own_prefix=own_prefix,
            skip_tokens=skip_tokens,
        )

    # 🌟 只有"登记过一条龙"的小组才算真的能用：新格式配置里没登记的任务会被 BetterGI
    #    直接跳过（`OneDragonFlowManager.LoadDisplayTaskListFromConfig` 的 `continue`）。
    #    所以：优先用已登记的小组；小组没登记但**总组登记了**，就退到总组去挑（第 2 步），
    #    这样不用往玩家的一条龙里塞新任务也能跑；两边都没登记时才现场登记（见 set_task_enabled）。
    registered_hits = [
        (path, data)
        for path, data in hits
        if str((data or {}).get("name") or os.path.splitext(os.path.basename(path))[0]) in registered
    ]
    if registered_hits:
        hits = registered_hits
    elif preferred_registered:
        hits = []

    if hits:
        ranked = route_group.rank_groups_for_targets(
            [
                (
                    path,
                    data,
                    route_group.count_matches(
                        data,
                        search_terms,
                        category.match_fields,
                        both_ways=category.both_ways,
                        own_prefix=own_prefix,
                        skip_tokens=skip_tokens,
                    ),
                )
                for path, data in hits
            ],
            limit,
        )
        usable = [item for item in ranked if not route_group.is_oversized(item[1], limit)]
        if usable:
            results = []
            for path, data, _matched in usable:
                applied = _apply_route_group(
                    bgi_config,
                    path,
                    search_terms,
                    label=f"{category.action}_{os.path.splitext(os.path.basename(path))[0]}",
                    task_candidates=(
                        str(data.get("name") or os.path.splitext(os.path.basename(path))[0]),
                    ),
                    fields=category.match_fields,
                    both_ways=category.both_ways,
                    own_prefix=own_prefix,
                    skip_tokens=skip_tokens,
                )
                if applied:
                    results.append(applied)
            if results:
                disable_preferred()
                return results

    # 2) 兜底：用该类目的总组
    #    注意：一律从**完整清单**（归档优先）里挑路线 —— 组被精简过之后，UI 里那份
    #    只剩上次要打的路线，直接用它就会漏掉新目标。
    if preferred:
        full, _source, from_archive = route_group.load_full_group(preferred_path)
        matched = route_group.count_matches(
            full,
            search_terms,
            category.match_fields,
            both_ways=category.both_ways,
            own_prefix=own_prefix,
            skip_tokens=skip_tokens,
        )
        if matched:
            oversized = route_group.is_oversized(full, limit)
            policy = "allow" if config.BGI_ALLOW_LARGE_ROUTE_GROUP else config.BGI_ROUTE_GROUP_POLICY

            if oversized and policy == "shrink":
                shrunk = route_group.shrink_to_matches(
                    full,
                    search_terms,
                    category.match_fields,
                    both_ways=category.both_ways,
                    own_prefix=own_prefix,
                    skip_tokens=skip_tokens,
                )
                _preferred_key, preferred_created = set_task_enabled(
                    bgi_config, True, (preferred_name,)
                )
                strategy_warning = route_group.group_strategy_notice(shrunk)
                if strategy_warning:
                    print(strategy_warning)
                action = {
                    "path": preferred_path,
                    "data": shrunk,
                    "label": category.action,
                    "name": preferred_name,
                    "task_created": bool(preferred_created),
                    "strategy_warning": strategy_warning,
                    "notice": (
                        f"「{preferred_name}」原 {route_group.group_size(full)} 条路线，"
                        f"已精简为命中「{'、'.join(targets)}」的 {route_group.group_size(shrunk)} 条"
                        f"（全部 Enabled，不再产生禁用日志）"
                        f"\n   ↳ 完整清单归档在 {route_group.archive_path_for(preferred_path)}，"
                        f"下次换目标会自动从归档里重新挑（不用手工拆组）"
                        f"\n   ↳ 精简原因：大组里躺着几百条 Disabled，跑一半按停止时日志会瞬间爆发"
                        f"（实测 638/773 行/秒）导致 BetterGI 在 WPF 层栈溢出崩溃"
                        + (f"\n   ↳ {strategy_warning}" if strategy_warning else "")
                        + _registration_notice(preferred_name, preferred_created, bgi_config)
                    ),
                }
                if not from_archive:
                    action["archive"] = {
                        "path": route_group.archive_path_for(preferred_path),
                        "data": full,
                        "label": f"{category.action}_archive",
                    }
                return [action]

            if oversized and policy == "refuse":
                disable_preferred()
                return [
                    {
                        "warning": route_group.oversized_warning(
                            full, preferred_path, limit
                        )
                    }
                ]

            if oversized:
                print(
                    f"⚠️ 「{preferred_name}」{route_group.group_size(full)} 条路线超过安全上限 "
                    f"{limit}，按 policy={policy} 照旧整组开关（崩溃风险自负）。"
                )
            applied = _apply_route_group(
                bgi_config,
                preferred_path,
                search_terms,
                label=category.action,
                task_candidates=(preferred_name,),
                fields=category.match_fields,
                both_ways=category.both_ways,
                group=full,
                own_prefix=own_prefix,
                skip_tokens=skip_tokens,
            )
            if applied:
                return [applied]
        disable_preferred()

    # 3) 真的没有：给出可操作的提示，不要静默吞掉
    available = route_group.available_group_names(group_dir)
    repo_hint = (
        f"repo/pathing/{category.folder_prefix}" if category.folder_prefix else f"repo/pathing/{category.label}"
    )
    return [
        {
            "warning": (
                f"⚠️ 没找到能覆盖「{'、'.join(targets)}」的{category.label}路线组，本次不排它。\n"
                f"   ↳ 请在 BetterGI 的「调度器」里把 {repo_hint} 下对应的目录"
                f"建成一个组（命名 `{category.group_filename}` 或按材料分开建）并加入一条龙。\n"
                f"   ↳ 目标写法：{category.hint}\n"
                f"   ↳ 当前调度器里已有的组：{'、'.join(available) if available else '（读不到）'}"
            )
        }
    ]


def _apply_enemy_groups(bgi_config, hunt_items, group_dir=None, preferred_path=None, max_group_size=None):
    """兼容旧调用：等价于按"敌人与魔物"类目处理。"""
    category = route_group.category_specs()["hunt"]
    return _apply_category_groups(
        bgi_config,
        hunt_items,
        category,
        group_dir=group_dir,
        preferred_path=preferred_path,
        max_group_size=max_group_size,
    )


def _registered_task_names():
    """一条龙配置里已登记的任务显示名（只有登记过的组才能被真正执行）。"""
    try:
        path = resolve_one_dragon_config_path()
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        definitions = data.get("TaskDefinitions") or {}
        return {str(name) for name in definitions.values()}
    except Exception:
        return set()


def js_script_settings():
    """从 .env 读 JS 脚本自定义配置（JSON）；坏值就当空字典，绝不因此中断执行。"""
    raw = str(getattr(config, "BGI_JS_SCRIPT_SETTINGS", "") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        print(f"⚠️ BGI_JS_SCRIPT_SETTINGS 不是合法 JSON，已忽略：{raw}")
        return {}
    return data if isinstance(data, dict) else {}


def group_path_for_task(name, group_dir=None):
    """一条龙任务名 → 调度器脚本组文件路径（BetterGI 就是按名字取 <名字>.json）。"""
    directory = group_dir or config.BGI_SCRIPT_GROUP_DIR
    return os.path.join(directory, f"{name}.json")


def apply_js_project_settings(group_path, settings=None, project_names=None):
    """把 JS 脚本自定义配置写进脚本组里的 Javascript 项目。

    踩过的坑：《批量讨伐角色养成材料BOSS》默认 `showEditorOnStart = true`，
    运行时先弹一个遮罩式"BOSS 配置编辑器"，**必须手点"保存并关闭"脚本才会继续** ——
    挂机时等于卡死。它的 `settings.json` 里明明白白写着可以用这个开关关掉，
    BetterGI 也把组里项目的 `jsScriptSettingsObject` 原样喂给 JS 的 `settings` 对象
    （`ScriptProject.ExecuteAsync` → `engine.AddHostObject("settings", context)`）。

    返回 (组路径, 组数据, 本次真正改动的键) ；没有改动时返回 None。
    """
    settings = js_script_settings() if settings is None else dict(settings)
    if not settings:
        return None

    group = route_group.load_group(group_path)
    if not group:
        return None

    changed = {}
    for project in group.get("projects") or []:
        if str(project.get("type") or "").lower() != "javascript":
            continue
        if project_names and str(project.get("name") or "") not in project_names:
            continue

        current = project.get("jsScriptSettingsObject")
        merged = dict(current) if isinstance(current, dict) else {}
        for key, value in settings.items():
            if merged.get(key) != value:
                merged[key] = value
                changed[key] = value
        project["jsScriptSettingsObject"] = merged

    if not changed:
        return None
    return str(group_path), group, changed


def describe_js_settings_change(name, changed):
    """给玩家看的一行说明（别让它像个莫名其妙的配置改动）。"""
    detail = "、".join(f"{key}={value}" for key, value in changed.items())
    return (
        f"🔧 已关闭《{name}》的自定义配置项：{detail}"
        f"\n   ↳ 这样脚本启动时不会再弹出需要手动点「保存并关闭」的配置编辑器"
        f"（Boss 列表由 Agent 直接写 assets/config/config.json，不需要编辑器）。"
        f"\n   ↳ 想手动编辑：在 BetterGI 里右键该脚本 →「修改JS脚本自定义配置」→ 勾回 showEditorOnStart。"
    )


def _registration_notice(name, created, bgi_config=None):
    """任务登记情况的一句话说明。

    - `created=True`：这次帮玩家登记好了（等价于在 BetterGI「一条龙」界面点一次添加）；
    - 已经在配置里登记过：不啰嗦；
    - 旧格式配置（没有 TaskDefinitions）：名字键本来就是合法写法，不用提醒；
    - 没能登记：才提示去界面加一次。
    """
    name = str(name or "")
    if created:
        return (
            f"\n   ↳ 🆕「{name}」之前没登记进一条龙，已自动添加为任务项"
            f"（等价于在 BetterGI「一条龙」界面点一次添加；重启 BetterGI 后可在列表里看到它）。"
        )
    if bgi_config is not None and not uses_task_definitions(bgi_config):
        return ""
    if not name or name in _registered_task_names():
        return ""
    return (
        f"\n   ↳ ⚠️「{name}」没登记进一条龙，这次也没能自动登记，"
        f"请在 BetterGI 的「一条龙」界面把它添加/勾选一次，否则写进配置也不会被执行。"
    )


def _apply_js_script_groups(bgi_config, items, group_dir=None):
    """启用 JS 脚本组（狗粮 / 采集水下这类整脚本任务）。

    这类任务没有 folderName 路线可开关，只能把所在脚本组在一条龙里启用。
    **只在被点名时启用，不在没点名时停用** —— 玩家自己的日常一条龙里可能常驻狗粮。
    返回 [{"notice": …}] 或 [{"warning": …}]。
    """
    group_dir = group_dir or config.BGI_SCRIPT_GROUP_DIR
    registered = _registered_task_names()
    results = []
    for target in [str(x).strip() for x in (items or []) if str(x).strip()]:
        hit = route_group.find_js_script_group(target, group_dir, registered)
        if not hit:
            rows = route_group.available_js_script_groups(group_dir, registered)
            listing = "、".join(
                f"{name}（脚本：{script}{'' if reg else '，未登记一条龙'}）" for name, script, reg in rows
            )
            results.append(
                {
                    "warning": (
                        f"⚠️ 没找到与「{target}」匹配的 JS 脚本组，本次不排它。\n"
                        f"   ↳ 请在 BetterGI 的「调度器」里把对应脚本建成一个组并加入一条龙。\n"
                        f"   ↳ 当前已有的 JS 脚本组：{listing or '（读不到）'}"
                    )
                }
            )
            continue

        group_name, path, script_name, is_registered = hit
        task_key, task_created = set_task_enabled(bgi_config, True, (group_name,))
        notice = f"🎬 JS 脚本组「{group_name}」已启用（脚本：{script_name}，Id: {task_key}）"
        notice += _registration_notice(group_name, task_created, bgi_config)
        results.append(
            {
                "notice": notice,
                "group": group_name,
                "script": script_name,
                "path": path,
                "task_created": bool(task_created),
            }
        )
    return results


def repair_agent_tasks(
    config_path=None,
    backup_dir=None,
    running_check=None,
    group_dir=None,
    apply_js_settings=True,
    force=False,
):
    """维护命令：清掉 Agent 加过又跑不起来的任务项，并顺手关掉 JS 脚本的启动编辑器。

    典型场景：
      · 早先 `run_boss` 用占位名 `AutoBoss` 自动登记了一条任务（BGI 内置任务表里没有这个名字、
        也没有 `User\\ScriptGroup\\AutoBoss.json`），它会挂在玩家的一条龙列表里，跑了也只是空转；
      · 《批量讨伐角色养成材料BOSS》默认 `showEditorOnStart = true`，运行时先弹一个必须手点
        "保存并关闭"的配置编辑器，挂机时会卡在那里。

    返回 0=无需处理或已修好，1=BetterGI 正在运行/写盘失败。
    """
    path = config_path or resolve_one_dragon_config_path()
    backups = backup_dir or config.BGI_BACKUP_DIR

    if running_check is None:
        from skills import health_check

        running_check = health_check.bettergi_running_status
    status = running_check()
    if status is True:
        print("⚠️ BetterGI 正在运行：先关掉它再执行 repair，否则它退出时会用旧配置覆盖这次修改。")
        return 1
    if status is None and not force:
        print(
            "⚠️ 无法确认 BetterGI 是否在运行（进程枚举被拦/无输出）。\n"
            "   建议先手动关掉 BetterGI 再执行；确认它已经关了就加 --force：\n"
            "       python main.py repair --force"
        )
        return 1

    if not os.path.isfile(path):
        print(f"❌ 找不到一条龙配置：{path}")
        return 1

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    definitions = task_definitions(data)
    removed_names = []
    for name in load_agent_tasks():
        if name in {str(value) for value in definitions.values()} and retire_task(data, name):
            removed_names.append(name)

    pruned = prune_task_list(data)

    # 🌟 顺手关掉 Agent 遗留的开关：它开过（状态文件记着）或本来就归它管的那几个，
    #    现在还开着就关掉 —— 玩家实测的坑正是这个：打完 Boss 那轮开的《批量讨伐角色养成材料BOSS》
    #    一直挂着 enabled，之后每次启动一条龙都顺带打一次 Boss。
    #    玩家自己手动开的组不在状态文件里，这里不会碰（下一个 Agent 轮次也不会）。
    disabled_names = tasks_enabled_now(
        data, set(load_agent_enabled_tasks()) | _agent_managed_task_names()
    )
    for name in disabled_names:
        set_task_enabled(data, False, (name,))

    js_change = None
    if apply_js_settings:
        boss_name = _ONE_DRAGON_TASK_KEYS["run_boss"][0]
        js_change = apply_js_project_settings(group_path_for_task(boss_name, group_dir))

    if not removed_names and not pruned and not disabled_names and not js_change:
        print("✅ 没有需要清理的一条龙任务项。")
        return 0

    transaction = JsonConfigTransaction(backups)
    try:
        transaction.stage_json(path, data, "repair_one_dragon")
        if js_change:
            transaction.stage_json(js_change[0], js_change[1], "repair_js_settings")
        result = transaction.commit()
    except ConfigTransactionError as exc:
        print(f"❌ 写盘失败（未改动任何文件）：{exc}")
        return 1

    if removed_names:
        print(f"🧹 已移除 Agent 添加但跑不起来的任务项：{'、'.join(removed_names)}")
    if disabled_names:
        print(
            f"🧹 已关闭 Agent 留下的开关：{'、'.join(disabled_names)}\n"
            f"   ↳ 这些都是「Agent 开过、之后没再排」的调度器，留着会让每次一条龙都顺带跑它们。\n"
            f"      想一直跑就在 BetterGI 里手动打开（手动开的不会被 Agent 再关掉）。"
        )
    if pruned:
        print(f"🧹 已清理 {pruned} 条无效键（TaskDefinitions 里没有对应 Id）")
    if js_change:
        print(describe_js_settings_change(_ONE_DRAGON_TASK_KEYS["run_boss"][0], js_change[2]))
    print(f"🗂️ 备份与 Diff：{result.backup_dir}｜回滚：rollback {result.backup_dir.name}")
    save_agent_tasks([name for name in load_agent_tasks() if name not in set(removed_names)])
    print("✅ 完成。")
    return 0


def execute_bgi_task(bgi_cmd, decision_lower, store, open_id, uid):
    """执行一轮 BGI 计划（单轮互斥，见 `_ROUND_LOCK`）。"""
    if not _ROUND_LOCK.acquire(blocking=False):
        # 为什么需要它：一轮里可能**等 BetterGI 跑完**（最长 BGI_WAIT_RUNNING_TASK_MINUTES 分钟），
        # 等待期间玩家完全可以再发一条指令 —— 两个线程同时改同一份一条龙配置、
        # 再各冷启动一次 BetterGI，就会互相覆盖（还可能启动两次）。
        msg = (
            "⏳ 上一轮还在处理中（可能正在等 BetterGI 把当前任务跑完），这条指令先不执行。\n"
            "   等它结束再发一次即可（上一轮会自己继续，不用重新下令）。"
        )
        print(msg)
        send_notice(open_id, msg)
        return
    try:
        _execute_bgi_task_locked(bgi_cmd, decision_lower, store, open_id, uid)
    finally:
        _ROUND_LOCK.release()


def _execute_bgi_task_locked(bgi_cmd, decision_lower, store, open_id, uid):
    """异步执行 BGI 逻辑（从原 feishu_main.py 拷贝，路径改为 config 常量）。"""
    try:
        bgi_cmd = bgi_cmd if isinstance(bgi_cmd, dict) else {}
        # 🌟 防呆：LLM 可能输出 "energy_task": null / "free_task": null。
        # .get(key, {}) 只在【键缺失】时兜底，键存在但值为 None 时依旧返回 None，
        # 随后 energy_task.get(...) 就会抛 AttributeError。此处统一归一化。
        energy_task = bgi_cmd.get("energy_task")
        if not isinstance(energy_task, dict):
            energy_task = {}
        free_tasks = bgi_cmd.get("free_task")
        if not isinstance(free_tasks, list):
            free_tasks = []
        free_tasks = [t for t in free_tasks if isinstance(t, dict)]

        # 🌟 防误判：LLM 会把「异种合成魔兽」「圣骸兽」这类**敌人路线名**当成 Boss
        # （run_boss），走到下面的 Boss 守卫就会被判"名称无法对齐"而整轮作废。
        # 这里先认出来是敌人路线就改判成 hunt，且必须早于 gather/hunt 列表的解析。
        redirect_notice = route_group.redirect_run_boss_to_hunt(bgi_cmd)
        if redirect_notice:
            print(redirect_notice)
            energy_task = bgi_cmd.get("energy_task") or {}
            free_tasks = bgi_cmd.get("free_task") or []

        target_domain = energy_task.get("target", "无")
        # 🌟 自由任务类目改判：LLM 会把「久雨莲」这类**食材/矿物**当成角色突破特产写成
        # gather，也会把「狗粮」这类**整脚本任务**写成 gather。这里在收集目标之前
        # 先按"哪一类里真的有路线/哪个脚本组真的存在"改判一遍（只在声明类目里 0 条时才改）。
        for reclassify_notice in route_group.reclassify_free_tasks(
            free_tasks, registered_names=_registered_task_names()
        ):
            print(reclassify_notice)

        gather_items = [t.get("target") for t in free_tasks if t.get("action") == "gather"]
        script_items = [t.get("target") for t in free_tasks if t.get("action") == "script"]
        category_items = {
            spec.action: [t.get("target") for t in free_tasks if t.get("action") == spec.action]
            for spec in route_group.category_specs().values()
        }
        hunt_items = category_items.get("hunt", [])
        gather_str = "、".join([str(x) for x in gather_items if x]) if gather_items else "无"
        hunt_str = "、".join([str(x) for x in hunt_items if x]) if hunt_items else "无"
        extra_str = "、".join(
            f"{spec.label}:{'/'.join(str(x) for x in category_items[spec.action] if x)}"
            for spec in route_group.category_specs().values()
            if spec.action != "hunt" and any(category_items.get(spec.action))
        )

        # 🌟 Boss 讨伐目标必须先翻译 + 逐字对齐再往下走：
        # 1) LLM 可能只给角色名（「蓝砚」），甚至按元素属性猜错 Boss（猜成「无相之风」）——
        #    这里用本地字典把角色名/材料名确定性翻译成官方 Boss 名；
        # 2) 脚本是用 `assets/Pathing/${boss.name}前往.json` 拼路径文件名的，名字差一个
        #    字符（实测是少了一个「·」）就会读不到文件、把空字符串丢给
        #    AutoPathingScript.Run("")，日志里只留一句 JSON 解析失败 —— 现象就是"无法寻路"。
        # 解析不出来就抛异常 = 不写配置、不启动，让上层回去问玩家，绝不按元素猜。
        # 放在 ensure_bettergi_closed 之前，是为了名字写错时不要先把正在跑的一条龙杀掉。
        if energy_task.get("action") == "run_boss":
            target_domain, boss_notices = resolve_boss_target(target_domain)
            for notice in boss_notices:
                print(notice)

        # 🌟 秘境目标同样必须先翻译：LLM 会填角色名（「去打蓝砚武器的突破副本」→
        # target="蓝砚"），而下面 DomainName 是原样写入的 —— "蓝砚" 不是秘境名，
        # 写进去 BetterGI 自动秘境直接匹配不到（白跑）。这里由代码自己看展柜：
        # 角色 → 当前武器 → 武器突破素材 → 炼武秘境，并顺手把 domain_index 也定了。
        elif energy_task.get("action") == "run_domain":
            domain_result, domain_notices = resolve_domain_target(target_domain, uid=uid)
            target_domain = domain_result.domain
            if domain_result.domain_index:
                # 回写进 energy_task，让下面的 SundayEverySelectedValue 用推导值而不是猜
                energy_task["domain_index"] = domain_result.domain_index
            for notice in domain_notices:
                print(notice)

        # 🌟 必须先于读取/写入任何配置执行：
        # BetterGI 在跑的话，热启动不会触发一条龙，且它退出时会回写旧配置覆盖我们的修改
        if not ensure_bettergi_closed(open_id):
            return

        config_path = resolve_one_dragon_config_path()
        map_config_path = config.BGI_MAP_CONFIG
        transaction = JsonConfigTransaction(config.BGI_BACKUP_DIR)

        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                bgi_config = json.load(f)

            # 🌟 本轮用到的任务名 / Agent 本轮新登记的任务名：
            #    收尾时用它们决定"上一轮自己加的任务项要不要摘掉"，以及把本次新加的记进状态文件
            used_task_names = set()
            newly_registered = set()
            # "一条路线都没命中"的说明（用来给玩家解释为什么这次什么都没跑）
            empty_notes = []
            # 🌟 动手之前先拍一张"哪些任务本来就是开着的"快照 —— 这是区分
            #    "Agent 开的" 和 "玩家自己开的" 的唯一依据（玩家中途手动开的组不该被它关掉）。
            names_before = [str(value) for value in task_definitions(bgi_config).values()]
            if not uses_task_definitions(bgi_config):
                # 旧格式（TaskDefinitions 为空）里"名字就是键"，键本身也是任务名
                names_before += [str(key) for key in (bgi_config.get("TaskEnabledList") or {})]
            enabled_at_start = set(tasks_enabled_now(bgi_config, names_before))

            if energy_task.get("action") == "run_domain":
                bgi_config["DomainName"] = target_domain
                _domain_key, _domain_created = set_task_enabled(
                    bgi_config, True, _ONE_DRAGON_TASK_KEYS["run_domain"]
                )
                used_task_names.add(_ONE_DRAGON_TASK_KEYS["run_domain"][0])
                if _domain_created:
                    newly_registered.add(_ONE_DRAGON_TASK_KEYS["run_domain"][0])
                set_task_enabled(bgi_config, False, _ONE_DRAGON_TASK_KEYS["run_leyline"])
                set_task_enabled(bgi_config, False, _ONE_DRAGON_TASK_KEYS["run_boss"])
            # 🌟 新增：解析并覆写周日/全开时的材料顺位 (1, 2, 或 3)
                # 如果没有传这个值（比如圣遗物本），默认给 "1" 防呆
                domain_index = energy_task.get("domain_index", "1")
                bgi_config["SundayEverySelectedValue"] = str(domain_index)

                # 🌟 「只刷 N 次」：次数不在一条龙配置里，而在全局 User/config.json 的
                # autoDomainConfig（specifyResinUse + 各树脂刷取次数）。
                # 不写这里的话，BetterGI 默认 SpecifyResinUse=false = 刷到体力耗尽
                # —— 实测就是这样把 1 次变成了"用尽所有浓缩树脂和原粹树脂"。
                resin_plan = build_domain_resin_plan(energy_task.get("count"))
                global_config_path = config.BGI_GLOBAL_CONFIG
                if resin_plan and os.path.exists(global_config_path):
                    with open(global_config_path, "r", encoding="utf-8") as f:
                        global_config = json.load(f)

                    global_config.setdefault("autoDomainConfig", {}).update(resin_plan)
                    transaction.stage_json(global_config_path, global_config, "global_config")
                    print(describe_domain_resin_plan(energy_task.get("count")))
                elif not resin_plan:
                    print(describe_domain_resin_plan(energy_task.get("count")))

            elif energy_task.get("action") == "run_leyline":
                set_task_enabled(bgi_config, False, _ONE_DRAGON_TASK_KEYS["run_domain"])
                _leyline_key, _leyline_created = set_task_enabled(
                    bgi_config, True, _ONE_DRAGON_TASK_KEYS["run_leyline"]
                )
                used_task_names.add(_ONE_DRAGON_TASK_KEYS["run_leyline"][0])
                if _leyline_created:
                    newly_registered.add(_ONE_DRAGON_TASK_KEYS["run_leyline"][0])
                set_task_enabled(bgi_config, False, _ONE_DRAGON_TASK_KEYS["run_boss"])
                bgi_config["LeyLineOneDragonMode"] = True

                global_config_path = config.BGI_GLOBAL_CONFIG
                if os.path.exists(global_config_path):
                    with open(global_config_path, "r", encoding="utf-8") as f:
                        global_config = json.load(f)

                    if "autoLeyLineOutcropConfig" not in global_config:
                        global_config["autoLeyLineOutcropConfig"] = {}

                    global_config["autoLeyLineOutcropConfig"]["leyLineOutcropType"] = target_domain

                    transaction.stage_json(global_config_path, global_config, "global_config")
                    print(f"🌍 全局配置已更新：今日地脉目标锁定为【{target_domain}】")
                else:
                    print(f"⚠️ 找不到全局配置文件 {global_config_path}，无法设置地脉种类！")

            elif energy_task.get("action") == "run_boss":
                set_task_enabled(bgi_config, False, _ONE_DRAGON_TASK_KEYS["run_domain"])
                set_task_enabled(bgi_config, False, _ONE_DRAGON_TASK_KEYS["run_leyline"])
                # 讨伐用的是《批量讨伐角色养成材料BOSS》脚本组（下面会写它的 config），
                # 不是 BetterGI 内置的「自动首领讨伐」—— 后者读的是 ini 里的 AutoBossName，
                # 没配就会打印"未选择需要讨伐的首领，跳过"。
                boss_task_name = _ONE_DRAGON_TASK_KEYS["run_boss"][0]
                boss_key, boss_created = set_task_enabled(
                    bgi_config, True, _ONE_DRAGON_TASK_KEYS["run_boss"]
                )
                used_task_names.add(boss_task_name)
                if boss_created:
                    newly_registered.add(boss_task_name)
                boss_registration = _registration_notice(boss_task_name, boss_created, bgi_config)
                if boss_registration:
                    print(boss_registration)

                # 🌟 关掉脚本自带的"启动时打开配置编辑器"（否则挂机时会卡在一个
                #    必须手点"保存并关闭"的遮罩窗口上）；改动走同一个配置事务。
                boss_settings = apply_js_project_settings(
                    group_path_for_task(boss_task_name)
                )
                if boss_settings:
                    settings_path, settings_data, settings_changed = boss_settings
                    transaction.stage_json(settings_path, settings_data, "boss_js_settings")
                    print(describe_js_settings_change(boss_task_name, settings_changed))
                if not script_group_file(boss_task_name):
                    print(
                        f"⚠️ 调度器里找不到脚本组《{boss_task_name}》"
                        f"（User\\ScriptGroup\\{boss_task_name}.json），"
                        f"这条任务不会被执行 —— 请在 BetterGI 里订阅并把它建成同名脚本组。"
                    )
                elif uses_task_definitions(bgi_config) and boss_key not in task_definitions(bgi_config):
                    print(
                        f"⚠️ 脚本组《{boss_task_name}》存在但没登记进一条龙，"
                        f"请在 BetterGI 的一条龙界面添加一次（然后重跑本轮即可）。"
                    )

                boss_config_path = config.BGI_BOSS_CONFIG
                
                # 🌟 修复：带默认值的“自愈型”配置逻辑
                user_team = ""
                # 默认使用 README 中推荐的策略兜底，防止小白连外挂都没打开过
                user_strategy = "万能战斗策略（萌新推荐）" 
                user_timeout = 240
                
                if os.path.exists(boss_config_path):
                    try:
                        with open(boss_config_path, "r", encoding="utf-8") as f:
                            existing_data = json.load(f)
                            if isinstance(existing_data, list) and len(existing_data) > 0:
                                first_boss = existing_data[0]
                                user_team = first_boss.get("team", "")
                                fight_param = first_boss.get("fightParam", {})
                                user_strategy = fight_param.get("strategyName", user_strategy)
                                user_timeout = fight_param.get("timeout", 240)
                    except Exception as e:
                        print(f"⚠️ 读取原有 Boss 配置失败，将使用默认推荐配置兜底: {e}")
                else:
                    print("💡 未检测到 Boss 配置文件，正在为您自动创建默认兜底配置...")

                # 🌟 策略名对不上时 BetterGI 只会留一句「未匹配到任何战斗脚本」，
                # 这里提前提醒（不阻断：策略也可能来自脚本仓库导入）
                strategy_hint = strategy_notice(user_strategy)
                if strategy_hint:
                    print(strategy_hint)

                # 🌟 讨伐次数：玩家显式指定（如"打一次"→ count: 1）时按指定值，
                # 未指定则保持原有默认 100
                raw_count = energy_task.get("count")
                try:
                    # 先转 float 再取整：模型可能输出 2.0 这类浮点数，直接 int("2.0") 会失败
                    run_count = (
                        int(float(str(raw_count).strip()))
                        if raw_count not in (None, "")
                        else 100
                    )
                except (TypeError, ValueError):
                    print(f"⚠️ 无法解析 count={raw_count!r}，回退为默认 100 次。")
                    run_count = 100
                if run_count <= 0:
                    run_count = 100

                # 动态生成新配置
                boss_data = [{
                    "name": target_domain,
                    "totalCount": run_count,
                    "remainingCount": run_count,
                    "team": user_team,
                    "returnToStatueAfterEachRound": True,
                    "farmMode": "一次性",
                    "lastFarmTime": None,
                    "dailyLimitCount": run_count,
                    "dailyRemainingCount": run_count,
                    "fightParam": {
                        "timeout": user_timeout, 
                        "strategyName": user_strategy 
                    },
                }]

                # 🌟 修复：不再暴力建文件夹！先检查外挂脚本的根基在不在
                boss_dir = os.path.dirname(boss_config_path)
                if os.path.exists(boss_dir):
                    transaction.stage_json(boss_config_path, boss_data, "boss_config")
                        
                    display_team = user_team if user_team else "当前驻场队伍"
                    print(f"👹 Boss 模块接管：已生成 {target_domain} 的讨伐配置（队伍: '{display_team}', 策略: '{user_strategy}', 次数: {run_count}）。")
                else:
                    # 如果连文件夹都没有，说明他根本没下载这个脚本，或者路径不对
                    raise ConfigTransactionError(
                        "未找到 Boss 脚本的运行环境；请先在 BetterGI 中订阅《批量讨伐角色养成材料BOSS》脚本，"
                        "并至少手动运行一次。"
                    )

            # 🌟 自动建组（锄大地这种 400+ 条路线，手工在 UI 里建组太折磨）：
            #    调度器里没有该类目的总组时，按 User\AutoPathing\<类目> 生成一份，
            #    用**独立事务**先落盘（同一套备份/回滚机制），下面的选路线逻辑才能读到它。
            #    注意：JsonConfigTransaction 提交后仍持有暂存项，不能复用同一实例再提交。
            group_transaction = JsonConfigTransaction(config.BGI_BACKUP_DIR)
            generated_any = False
            for spec in route_group.category_specs().values():
                generated = route_group.ensure_category_group(spec)
                if not generated:
                    continue
                generated_path, generated_data, generated_notice = generated
                os.makedirs(os.path.dirname(generated_path), exist_ok=True)
                group_transaction.stage_json(
                    generated_path, generated_data, f"auto_group_{spec.action}"
                )
                print(generated_notice)
                generated_any = True
            if generated_any:
                try:
                    generated_commit = group_transaction.commit()
                    print(
                        f"🧾 自动建组已落盘（回滚请用 rollback 命令）\n"
                        f"🗂️ 备份与 Diff：{generated_commit.backup_dir}"
                    )
                except ConfigTransactionError as exc:
                    print(f"⚠️ 自动建组写盘失败：{exc}")

            # 🌟 覆写地图素材（地方特产路线）
            #    ⚠️ 先过一遍**采集物 48 小时冷却**：地区特产采完 48 小时才刷新，
            #    48 小时内采过的材料这次不排（否则就是白跑一趟）。
            #    数据来自 BetterGI 自己的日志（所以玩家手动跑过的也算），见 skills/gather_cooldown.py。
            cooldown_blocked = []
            requested_gather = [str(x) for x in gather_items if x]
            if gather_items:
                force_gather = bool(bgi_cmd.get("force_gather")) or bool(bgi_cmd.get("force"))
                kept_items, cooldown_lines = _filter_gather_cooldown(
                    gather_items, force=force_gather
                )
                cooldown_blocked = [line for line in cooldown_lines if line.startswith("⏳")]
                if cooldown_lines:
                    print("⏳ 采集物冷却检查：\n   " + "\n   ".join(cooldown_lines))
                if cooldown_blocked and not force_gather:
                    send_notice(
                        open_id,
                        "⏳ 这些采集物还没刷新，本次不采：\n　 "
                        + "\n　 ".join(cooldown_blocked)
                        + "\n　 （想强跑就说「强制采集」；游戏里自己采过的可以登记："
                        "python -m skills.gather_cooldown --manual 霜仙花）",
                    )
                gather_items = kept_items
                if requested_gather and not gather_items:
                    # 点名的采集物全在 48 小时冷却里 → 本轮确实一个路线都不会跑，
                    # 记一笔，让下面的"空计划保护"拦住这次冷启动（否则 BGI 只会
                    # 打印 `没有配置,退出执行!` 然后退出，白开一次）。
                    empty_notes.append(
                        f"⏳ 「{'、'.join(requested_gather)}」都还在 48 小时冷却里，没有可跑的采集路线。"
                    )

            if gather_items:
                gather_category = route_group.all_category_specs()["gather"]
                map_result = _apply_route_group(
                    bgi_config,
                    map_config_path,
                    gather_items,
                    label="地图素材",
                    task_candidates=("地图素材",),
                    own_prefix=gather_category.folder_prefix,
                )
                if map_result:
                    transaction.stage_json(map_config_path, map_result["data"], "map_materials")
                    print(f"🗺️ {map_result['notice']}")
                    if map_result.get("matched"):
                        used_task_names.add(map_result["name"])
                        if map_result.get("task_created"):
                            newly_registered.add(map_result["name"])
                    else:
                        # 一条路线都没命中：**不能算"本轮排过"**，否则空计划会被当成有活干
                        empty_notes.append(map_result["notice"])
                else:
                    set_task_enabled(bgi_config, False, ("地图素材",))
                    empty_notes.append(
                        f"⚠️ 「{'、'.join(str(x) for x in gather_items)}」在地图素材组里没有找到路线"
                        "（也没读到组文件），本次不采集。"
                    )
            else:
                set_task_enabled(bgi_config, False, ("地图素材",))

            # 🌟 覆写敌人与魔物 / 锄大地 / 矿物 / 食材与炼金路线（参照地图素材那一套）：
            # 玩家说"刷点花蜜/打点蕈兽"时开对应敌人的怪点；说"锄大地"时开锄地专区的扫图路线。
            # 两类用目录前缀严格隔离，路线名重合也不会互相抢（见 route_group.is_foreign_project）。
            # 小组优先；只有找不到小组才用该类目的总组，超大组按策略精简/拒绝/放行。
            for spec in route_group.category_specs().values():
                items = [x for x in category_items.get(spec.action, []) if x]
                if not items:
                    # 只关 agent 自己那个总组，别的按目标分的组由玩家自己管
                    set_task_enabled(bgi_config, False, (os.path.splitext(spec.group_filename)[0],))
                    continue
                for category_result in _apply_category_groups(bgi_config, items, spec):
                    if category_result.get("warning"):
                        print(category_result["warning"])
                        # 这一类也没接上（比如材料根本不在你的路线组里）→ 记一笔，
                        # 配合下面的"空计划保护"：别白冷启动一次 BetterGI。
                        empty_notes.append(category_result["warning"].splitlines()[0])
                        continue
                    # 精简组时先把完整清单归档（归档是新文件，目录要先建出来）
                    archive = category_result.get("archive")
                    if archive:
                        os.makedirs(os.path.dirname(archive["path"]), exist_ok=True)
                        transaction.stage_json(archive["path"], archive["data"], archive["label"])
                    transaction.stage_json(
                        category_result["path"], category_result["data"], category_result["label"]
                    )
                    print(f"🧭 {spec.label}路线已重置：{category_result['notice']}")
                    if category_result.get("name") and category_result.get("matched"):
                        used_task_names.add(category_result["name"])
                        if category_result.get("task_created"):
                            newly_registered.add(category_result["name"])

            # 🌟 JS 脚本组（狗粮 / 采集水下这类）：只启用，不自动停用
            #    ⚠️ 两个 target 可能解析到**同一个脚本组**（「狗粮」和「AAA狗粮批发」是代码里声明的
            #    别名；模型重复输出同一个 target 也会），而同一个文件 stage 两次会抛
            #    ConfigTransactionError: Configuration file staged twice —— 它在 commit 之前抛出，
            #    整轮配置一个都写不进去、BetterGI 也不会启动。所以这里按文件路径去重。
            staged_settings_paths = set()
            for script_result in _apply_js_script_groups(bgi_config, script_items):
                print(script_result.get("warning") or script_result["notice"])
                if script_result.get("warning") and not script_result.get("group"):
                    empty_notes.append(script_result["warning"].splitlines()[0])
                if script_result.get("group"):
                    used_task_names.add(script_result["group"])
                    if script_result.get("task_created"):
                        newly_registered.add(script_result["group"])
                    # 同样关掉脚本自带的启动编辑器 / 悬浮面板（BGI 会按脚本的 settings.json 过滤）
                    script_settings = apply_js_project_settings(
                        script_result.get("path") or group_path_for_task(script_result["group"])
                    )
                    if script_settings:
                        settings_path, settings_data, settings_changed = script_settings
                        normalized = os.path.normcase(os.path.abspath(settings_path))
                        if normalized in staged_settings_paths:
                            print(f"（脚本组「{script_result['group']}」的 JS 设置已经在前面处理过，跳过重复写入）")
                            continue
                        staged_settings_paths.add(normalized)
                        transaction.stage_json(settings_path, settings_data, "js_script_settings")
                        print(describe_js_settings_change(script_result["group"], settings_changed))

            # 🌟 收尾清理 1：上一轮由 Agent 自己加进一条龙、这轮又没排的组，摘掉。
            #    不然它会一直挂着 enabled，玩家下一次直接从 BetterGI 跑一条龙时会莫名其妙多跑一套。
            previous_agent_tasks = set(load_agent_tasks())
            retired = sorted(previous_agent_tasks - used_task_names)
            for name in retired:
                if retire_task(bgi_config, name):
                    print(f"🧹 已移除上一轮由 Agent 添加的一条龙任务项「{name}」（本轮没排它）")

            # 🌟 收尾清理 1.5：Agent **开过**（或本来就归它管）但这轮没排的调度器，关掉。
            #    玩家实测的坑：他的一条龙里本来就登记着「批量讨伐角色养成材料BOSS」，
            #    Agent 打 Boss 那轮把它打开了 —— 因为它不是"Agent 加的"，以前**永远不会被关**，
            #    于是之后每次启动一条龙都顺带打一次 Boss（那次跑去打了上个版本的急冻树）。
            #    三类来源：内置动作（无条件）+ 状态文件里 Agent 开过的 + Agent 加过的（已摘）。
            stale_enabled = tasks_enabled_now(
                bgi_config,
                (set(load_agent_enabled_tasks()) | _agent_managed_task_names())
                - used_task_names
                - set(retired),
            )
            for name in stale_enabled:
                set_task_enabled(bgi_config, False, (name,))
                print(
                    f"🧹 已关闭上一轮由 Agent 开启、本轮没排的调度器「{name}」\n"
                    f"   ↳ 否则每次启动一条龙都会顺带跑它；想一直跑就在 BetterGI 里手动打开，"
                    f"或者直接告诉我“这轮也带上它”。"
                )

            # 🌟 记状态：`enabled` = "哪些开关该算在 Agent 头上"。
            #    = 上轮记过、而且这次动手前确实还开着的（继续算它的）
            #      ∪ 本轮排进计划的（它开的）
            #      − 刚刚已关掉的（关完就别再记账，否则下次又去关一遍、和玩家手动开启打架）。
            #    为什么要跟"动手前的快照"求交：玩家自己手动开的组不该被算成 Agent 开的，
            #    否则下一轮 Agent 会把它关掉，玩家再打开、又关掉……来回打架。
            agent_enabled = (
                (set(load_agent_enabled_tasks()) & enabled_at_start)
                | used_task_names
            ) - set(stale_enabled)
            save_agent_tasks(
                (previous_agent_tasks - set(retired)) | newly_registered,
                enabled=agent_enabled,
            )

            # 🌟 收尾清理 2：新格式配置里，TaskEnabledList / TaskOrder 中"没有 TaskDefinitions
            #    条目的键"BetterGI 会直接跳过（见 LoadDisplayTaskListFromConfig），
            #    留着只会让人以为任务已经加进去了。顺手删掉，并说明删了什么。
            pruned = prune_task_list(bgi_config)
            if pruned:
                print(
                    f"🧹 已清理 {pruned} 条无效的一条龙任务项"
                    f"（TaskDefinitions 里没有对应 Id 的键，BetterGI 本来就会跳过它们）"
                )

            transaction.stage_json(config_path, bgi_config, "one_dragon")
            transaction_result = transaction.commit()
            changed_files = describe_changed_files(transaction_result.changed_files)
            transaction_notice = (
                f"🧾 配置事务已提交：{changed_files}\n"
                f"🗂️ 备份与 Diff：{transaction_result.backup_dir}"
            )
            print(transaction_notice)
            # 玩家要求：QQ 上只保留「✅ 已确认 BetterGI 开始执行任务。」这一句，
            # 所以配置回执也不发（回滚 ID 在终端/Studio 日志里，CLI 的 rollback 会自己列事务）
            send_notice(open_id, transaction_notice, chat=False)

            extra_note = f"，其它路线：{extra_str}" if extra_str else ""
            script_note = (
                f"，整脚本：{'、'.join(str(x) for x in script_items if x)}" if script_items else ""
            )
            print(f"\n📝 BetterGI 配置已动态覆写！今日死磕：{target_domain}，顺路采集：{gather_str}，敌人讨伐：{hunt_str}{extra_note}{script_note}")

            # 发工资逻辑
            wallet = store.get("wallet", {"mora": 0, "exp_books": 0, "boss_mats": {}})
            if energy_task.get("action") == "run_leyline":
                if target_domain == "藏金之花":
                    wallet["mora"] += 480000
                    print(f"💰 记账成功：虚拟钱包入账 480,000 摩拉！当前存款：{wallet['mora']}")
                else:
                    wallet["exp_books"] += 40
                    print(f"📕 记账成功：虚拟钱包入账 40 本经验书！当前存款：{wallet['exp_books']}")
            elif energy_task.get("action") == "run_boss":
                boss_name = target_domain
                wallet["boss_mats"][boss_name] = wallet["boss_mats"].get(boss_name, 0) + 12
                print(f"👹 记账成功：虚拟仓库入账 12 个 {boss_name} 掉落材料！当前已积攒：{wallet['boss_mats'][boss_name]} 个")

            store["wallet"] = wallet
            # Persist store is responsibility of caller if needed

            # 启动或测试
            if decision_lower == 'y':
                if not used_task_names and empty_notes:
                    # 🌟 空计划保护（实测踩过）：目标是「金蕨」而这种材料根本不在你的脚本组里，
                    #    以前照样冷启动 BetterGI —— 日志里就是 `启用任务总数量: 0 / 没有配置,退出执行!`
                    #    （12:36:45、12:37:09 各一次），白开一次 BGI、还让人以为任务跑了。
                    #    ⚠️ 只在"玩家确实点了名、但一条路线都没命中"时才拦（empty_notes 非空）；
                    #    如果这轮本来就没指定任何目标（玩家自己开好了一条龙），保持老行为照常启动。
                    msg = (
                        "🈳 这次点名的目标在 BetterGI 里没有任何可执行的路线，已跳过启动（不白开一次 BGI）。\n"
                        + "\n".join(f"   {note}" for note in empty_notes[:4])
                        + "\n   ↳ 确实想跑的话：先在 BetterGI 里订阅对应路线"
                        "（例如 地方特产\\<地区>\\<材料>）并让它进脚本组，再让我排一次。\n"
                        "   ↳ 采集物清单：python -m skills.gather_cooldown --materials"
                    )
                    print(msg)
                    send_notice(open_id, msg)
                    return
                _launch_bettergi(open_id)
            else:
                print("🛠️ [测试模式] 配置文件覆写与虚拟账本更新已完成！成功跳过游戏启动环节。")
                send_notice(open_id, "🛠️ [测试模式] 配置已改好（没启动游戏）。可以手动检查 BetterGI 里的配置。")

        else:
            print(f"❌ 找不到配置文件: {config_path}，请检查路径。跳过执行。")
            feishu_api.send_feishu_msg(open_id, f"❌ 找不到配置文件: {config_path}，请检查路径。跳过执行。")

    except Exception as e:
        print(f"❌ 发生错误: {e}")
        feishu_api.send_feishu_msg(open_id, f"❌ 执行配置时发生错误: {str(e)}")
