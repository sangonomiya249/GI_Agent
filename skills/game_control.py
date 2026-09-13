"""关闭原神（以及可选地关掉 BetterGI）—— 玩家说"帮我关闭原神"就走这里。

**为什么单独一个模块**：和"写改 BetterGI 配置然后跑一条龙"是两件完全不同的事（不写配置、不启动、
只结束进程），所以不塞进 `bgi_controller`。

**关闭策略（先礼后兵）**
  ① `taskkill /PID <pid>`：不带 `/F` 时 Windows 发的是 WM_CLOSE，等于点窗口右上角的叉 ——
     游戏自己收尾（原神 PC 端会弹一次"确定退出"确认框时也能被它收掉）；
  ② 等 `GAME_CLOSE_GRACE_SECONDS`（默认 10 秒）还没退 → `taskkill /F /PID <pid>`：
     等价于任务管理器结束进程。原神的进度存在服务器上，强杀不会丢档，只是下次启动多一次
     "上次未正常退出"的提示；
  ③ 复核一遍进程是否真的没了，**如实报告**（判不出来就说判不出来，绝不说"已关闭"）。

**安全边界**
  · 只按**进程名白名单**匹配（`YuanShen.exe` / `GenshinImpact.exe` / BetterGI 配置里那个启动程序名），
    绝不按窗口标题通配或按父子关系去杀；
  · 一律**先出计划、等玩家回 y** 再动手（走和改配置同一套审批）；
  · 如果 BetterGI 正在跑任务，默认**连它一起关掉**（不然它会在没有游戏的情况下报错/空转），
    并且在审批文案里写明 —— 玩家点 y 之前能看到。
"""

import os
import re
import subprocess
import time

import config

# 关进程名的白名单（配置读不出来时的兜底；国服叫 YuanShen，国际服叫 GenshinImpact）
GAME_EXE_FALLBACKS = ("YuanShen.exe", "GenshinImpact.exe")
# 正常关闭的宽限时间（秒）；到点还没退就强杀
GAME_CLOSE_GRACE_SECONDS = getattr(config, "GAME_CLOSE_GRACE_SECONDS", 10)
# 句子里出现这些词就不当成"要关游戏"（"关掉游戏声音/画质/全屏"这类误伤太尴尬）
_NOT_A_CLOSE_COMMAND = (
    "声音", "音效", "音乐", "音量", "画质", "特效", "窗口", "全屏", "手柄",
    "自动拾取", "剧情", "宏", "后台", "攻略", "公告", "更新",
)
# 审批屏抬头（和 llm_brain 的审批保持同一种口气）
APPROVAL_HEADER = "🛑 [系统操作] 请确认是否执行？"


def _decode(raw):
    """taskkill/Get-Process 的输出在中文 Windows 上是 GBK。"""
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


def game_exe_names():
    """要关的进程名（去重、保持顺序）：BetterGI 配置里的启动程序 + 常见名字。"""
    names = []
    try:
        import json

        path = os.path.join(config.BGI_DIR, "User", "config.json")
        with open(path, "r", encoding="utf-8") as handle:
            install_path = str(
                (json.load(handle).get("genshinStartConfig") or {}).get("installPath") or ""
            )
        name = os.path.basename(install_path.replace("\\", "/")).strip()
        if name.lower().endswith(".exe") and name:
            names.append(name)
    except Exception:        # noqa: BLE001 —— 读不到就用兜底名字
        pass

    for fallback in GAME_EXE_FALLBACKS:
        if fallback.lower() not in {item.lower() for item in names}:
            names.append(fallback)
    return names


def _tasklist_table():
    """**一次** tasklist 拿全部进程 → {"yuanshen.exe": [pid…]}；判不出来返回 None。

    为什么不按进程名一个个查：`tasklist /FI IMAGENAME eq X` 每个名字一次调用，
    加上 PowerShell 兜底每次要起一个进程（实测整条 `plan_lines` 要好几秒）。
    一次性取全表只花几百毫秒，再在本地按白名单过滤。
    """
    try:
        result = subprocess.run(["tasklist.exe", "/FO", "CSV", "/NH"], capture_output=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None

    table = {}
    for line in _decode(result.stdout).splitlines():
        parts = [part.strip().strip('"') for part in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            table.setdefault(parts[0].lower(), []).append(int(parts[1]))
    return table


def _pids_via_powershell(exe_name):
    """tasklist 被拒时的兜底；返回 None 表示判不出来。"""
    stem = exe_name[:-4] if exe_name.lower().endswith(".exe") else exe_name
    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                f"(Get-Process -Name {stem} -ErrorAction SilentlyContinue).Id",
            ],
            capture_output=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    output = _decode(result.stdout).strip()
    if not output:
        # 返回码 0 但没有输出：可能真没跑，也可能权限被静默抑制 → 说不准
        return None
    pids = [int(x) for x in re.findall(r"\d+", output)]
    return pids or None


def running_processes():
    """{进程名: [pid…] 或 None}；None = 判不出来。

    ⚠️ 绝不能把"查不到"当成"没在运行" —— 那样会谎报"已关闭"。
    """
    names = game_exe_names()
    table = _tasklist_table()
    if table is not None:
        return {name: list(table.get(name.lower(), [])) for name in names}

    return {name: _pids_via_powershell(name) for name in names}


def game_running_status():
    """True / False / None（判不出来）—— 原神在不在跑。"""
    states = list(running_processes().values())
    if any(pids for pids in states if pids):
        return True
    if all(pids == [] for pids in states):
        return False
    return None


def _taskkill(pid, force):
    args = ["taskkill.exe", "/PID", str(pid)] + (["/F"] if force else [])
    try:
        result = subprocess.run(args, capture_output=True)
    except OSError as exc:
        return False, str(exc)
    message = (_decode(result.stderr) or _decode(result.stdout)).strip()
    return result.returncode == 0, message


# ==========================================
# 🌟 权限不够时的兜底：计划任务 StopGenshin（和 StopBetterGI 一个套路）
# ==========================================
STOP_GENSHIN_TASK_NAMES = (r"\StopGenshin", "StopGenshin")


def stop_pid_file():
    """和 `scripts/stop_genshin.ps1` 约定的 PID 记录文件（仓库路径，跨权限也一致）。"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, "scripts", "stop_genshin.pids")


def _write_stop_pid_file(pids):
    """写下要关闭的 PID，交给同等权限的计划任务精确关闭。

    ⚠️ 只写 PID，**不写进程名**：计划任务脚本会自己校验"这个 PID 现在的进程名在白名单里"，
    双保险避免 PID 复用误杀无关进程。
    """
    path = stop_pid_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"created={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
            for pid in pids:
                handle.write(f"{pid}\n")
        return path
    except OSError as exc:
        print(f"⚠️ 无法写入关闭目标文件 {path}: {exc}")
        return ""


def setup_task_hint():
    """注册计划任务的命令 —— **必须是绝对路径**。

    实测踩过：管理员 PowerShell 的默认工作目录是 `C:\\WINDOWS\\system32`，
    在那里写 `-File scripts\\setup_start_bettergi_task.ps1` 只会得到
    "实际参数…不存在"，玩家/我们都会以为脚本有问题。所以提示里永远给绝对路径。
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(repo, "scripts", "setup_start_bettergi_task.ps1")
    return f'powershell -ExecutionPolicy Bypass -File "{script}"'


def _close_via_scheduled_task(pids, wait_seconds=25):
    """用计划任务 StopGenshin 关闭原神（它以最高权限运行）。返回 (是否成功, 说明)。"""
    if not _write_stop_pid_file(pids):
        return False, "无法写入 PID 记录文件"
    for task_path in STOP_GENSHIN_TASK_NAMES:
        try:
            probe = subprocess.run(
                ["schtasks.exe", "/query", "/tn", task_path], capture_output=True
            )
            if probe.returncode != 0:
                continue
            run = subprocess.run(["schtasks.exe", "/run", "/tn", task_path], capture_output=True)
        except OSError as exc:
            return False, f"调用计划任务失败：{exc}"
        if run.returncode != 0:
            continue
        print(f"🛑 已通过计划任务 {task_path} 请求关闭原神（最多等 {wait_seconds} 秒）…")
        status = _wait_until_gone(wait_seconds)
        if status is False:
            return True, "权限不足，已改走计划任务（管理员权限）关闭原神"
        if status is None:
            return True, "已通过计划任务请求关闭原神，但最后状态判不出来（请自己确认一眼）"
        return False, "计划任务已触发，但原神仍然在运行"
    return False, (
        "计划任务 StopGenshin 不存在或触发失败。请以【管理员身份】运行一次（注意用完整路径，"
        "管理员 PowerShell 默认在 C:\\WINDOWS\\system32）：\n"
        f"     {setup_task_hint()}"
    )


def _wait_until_gone(seconds):
    """等原神进程彻底退出；返回 True/False/None（判不出来）。"""
    deadline = time.time() + max(0, float(seconds))
    while True:
        status = game_running_status()
        if status is not True:
            return status
        if time.time() >= deadline:
            return True
        time.sleep(1)


def close_game(grace_seconds=None, force=None):
    """关闭原神。返回结果字典（**永远不抛异常**，调用方直接拿去报信）。

    字段：opened（关之前是否在跑）、closed（进程名→PID）、forced（强杀的）、
          still_running（没关掉的）、unknown（判不出来）、via_task（是否靠计划任务兜底）、
          fallback_note（兜底说明）、summary（人话）
    """
    grace = GAME_CLOSE_GRACE_SECONDS if grace_seconds is None else grace_seconds
    force = True if force is None else force

    before = running_processes()
    running = {name: pids for name, pids in before.items() if pids}
    if not running:
        unknown = any(pids is None for pids in before.values())
        if unknown:
            # 判不出来时也试一次计划任务兜底（它按 PID 走，没目标就什么都不做）
            ok, note = _close_via_scheduled_task([])
            _ = ok
            return {
                "opened": False, "closed": {}, "forced": {}, "still_running": {},
                "unknown": True, "via_task": False, "fallback_note": note,
                "summary": f"查不到原神进程：可能没在运行，也可能是权限受限（说不准）。{note}",
            }
        return {
            "opened": False, "closed": {}, "forced": {}, "still_running": {},
            "unknown": False, "via_task": False, "fallback_note": "",
            "summary": "原神当前没有在运行（不需要关闭）。",
        }

    closed, forced, still = {}, {}, {}
    denied = False
    for name, pids in running.items():
        for pid in pids:
            ok, message = _taskkill(pid, force=False)
            if ok:
                closed.setdefault(name, []).append(pid)
            else:
                denied = denied or ("拒绝访问" in message or "Access is denied" in message)
                print(f"⚠️ 正常关闭 {name}({pid}) 失败：{message or '未知原因'}，稍后强杀")

    # 等正常关闭生效；还活着的再强杀（原神会把进度交给服务器，强杀不丢档）
    status = _wait_until_gone(grace)
    if status is True and force:
        remaining = {name: pids for name, pids in running_processes().items() if pids}
        for name, pids in remaining.items():
            for pid in pids:
                ok, message = _taskkill(pid, force=True)
                if ok:
                    forced.setdefault(name, []).append(pid)
                else:
                    still.setdefault(name, []).append(pid)
                    denied = denied or ("拒绝访问" in message or "Access is denied" in message)
                    print(f"⚠️ 强制关闭 {name}({pid}) 失败：{message or '未知原因'}")
        status = _wait_until_gone(5)

    if status is True:
        still = {name: pids for name, pids in running_processes().items() if pids}

    # 🌟 权限不够的兜底：原神多半是**以管理员权限**启动的（被管理员的 BetterGI/启动器拉起），
    #    这时普通权限 taskkill 只会得到"拒绝访问" —— 改走同等权限的计划任务 StopGenshin。
    via_task = False
    fallback_note = ""
    if still or status is None or denied:
        leftover = [pid for pids in (still or {}).values() for pid in pids]
        if leftover or status is None or denied:
            ok, fallback_note = _close_via_scheduled_task(leftover)
            print(f"🛑 计划任务兜底：{fallback_note}")
            if ok:
                via_task = True
                if leftover:
                    status = _wait_until_gone(5)
                    still = (
                        {name: pids for name, pids in running_processes().items() if pids}
                        if status is True else {}
                    )

    parts = []
    if closed:
        parts.append("、".join(f"{name}({'/'.join(map(str, pids))})" for name, pids in closed.items()) + " 正常关闭")
    if forced:
        parts.append("、".join(f"{name}({'/'.join(map(str, pids))})" for name, pids in forced.items()) + " 强制结束")
    if still:
        parts.append("、".join(f"{name}({'/'.join(map(str, pids))})" for name, pids in still.items()) + " 仍然在运行")
    if via_task and not still:
        parts.append("（权限不足，已改由计划任务 StopGenshin 以管理员权限关闭）")
    if not parts:
        parts.append("没能关闭原神进程")
    if not still and not via_task and not closed and not forced:
        parts.append("没能关闭原神进程")

    summary = "；".join(parts)
    if status is None:
        summary += "（最后状态判不出来，请自己确认一眼）"
    if still and fallback_note:
        summary += f"\n　 ↳ {fallback_note}"

    return {
        "opened": True,
        "closed": closed,
        "forced": forced,
        "still_running": still,
        "unknown": status is None,
        "via_task": via_task,
        "fallback_note": fallback_note,
        "summary": summary,
    }


# ==========================================
# 🌟 BetterGI：可选地一起关掉
# ==========================================


def bettergi_busy():
    """BetterGI 是不是正忙着跑任务。判不出来返回 False。

    ⚠️ 判定沿用 `bgi_controller`：看日志尾部的**终态标记**（一条龙和配置组任务结束 /
    任务被取消 / 主窗体退出）和启动标记谁在后面。以前只看"日志最近有没有更新"，
    而 BGI 跑完一条龙后窗口还开着、空闲时也会零碎写日志 —— 实测就误报过一次
    （12:34:40 跑完，12:36 还被判成"正在跑任务"）。
    """
    try:
        from skills import bgi_controller

        if bgi_controller._bettergi_pids() == []:
            return False
        return bgi_controller._bettergi_looks_busy()
    except Exception:        # noqa: BLE001 —— 判不出来就当没在跑（不阻塞关游戏）
        return False


def close_bettergi():
    """关闭 BetterGI；返回 (是否成功, 人话说明)。复用 bgi_controller 里那套（直接 + 计划任务）。"""
    from skills import bgi_controller

    pids = bgi_controller._bettergi_pids()
    if pids == []:
        return True, "BetterGI 本来就没在运行"
    if pids is None:
        return False, "查不到 BetterGI 进程（可能没有管理员权限），没法确认它是否还开着"

    ok, error = bgi_controller._close_bettergi_direct()
    if ok:
        return True, f"已关闭 BetterGI（PID {'/'.join(map(str, pids))}）"
    if bgi_controller._close_bettergi_via_task():
        return True, f"已通过计划任务 StopBetterGI 关闭 BetterGI（PID {'/'.join(map(str, pids))}）"
    return False, f"关闭 BetterGI 失败（{error or '权限不足'}）：请在电脑上手动关掉，或以管理员身份运行一次 scripts\\setup_start_bettergi_task.ps1"


# ==========================================
# 🌟 意图识别（关键词快通道；LLM 那条走 system_task）
# ==========================================
_CLOSE_VERBS = r"(关掉|关闭|关了|关一下|关|退出|退掉|退了|退)"
_TARGET_RE = re.compile(r"(原神|游戏|genshin)", re.I)
_VERB_RE = re.compile(_CLOSE_VERBS)
# 动词独立出现在句尾（"把原神和 bettergi 都关了"这种带连接词的说法）
_TRAILING_VERB_RE = re.compile(r"(关掉|关闭|关了|关|退出|退掉|退了|退)(掉|闭|了)?$")
# 否定：别关 / 不要关 / 先别关 / 不用关 / 不关 …（"别/不"在动词前 5 个字内就算）
_NEGATION = re.compile(r"(别|不要|不用|先别|暂时别|先不|不想|不)[^，。！？,.!?]{0,5}" + _CLOSE_VERBS)
# 疑问句（那是在问状态，不是命令）
_QUESTION_TAIL = ("吗", "么", "呢", "没")
_BGI_WORDS = ("bgi", "bettergi", "外挂", "挂机工具", "脚本工具")
_ALSO_WORDS = ("一起", "都", "连带", "顺便", "还有", "以及", "和")


def _normalize(text):
    return re.sub(r"[\s，。！？、,.!?~～:：;；'\"“”‘’()（）]", "", str(text or "")).lower()


def _looks_like_close_command(normalized):
    """句子里有没有"关闭原神"这个命令。

    不写成一条大正则：玩家会说"把原神和 bettergi 都关了"（宾语和动词隔着连接词），
    也会说"帮我关一下原神"（动词在前）。所以按**位置关系**判：
      · 目标词（原神/游戏）前后 6 个字内有动词 → 算；
      · 动词独立出现在句尾，且句子里有目标词 → 算（覆盖"…都关了"）。
    """
    for match in _TARGET_RE.finditer(normalized):
        after = normalized[match.end():match.end() + 6]
        before = normalized[max(0, match.start() - 6):match.start()]
        if _VERB_RE.search(after) or _VERB_RE.search(before):
            return True
    if _TARGET_RE.search(normalized) and _TRAILING_VERB_RE.search(normalized):
        return True
    return False


def detect_intent(text):
    """从玩家一句话里识别"关原神"的意图；没有就返回 None。

    只认**明确提到原神/游戏 + 关闭动词**的说法，并且挡掉三类误伤：
      · 否定（"别关原神"/"先不关"）；
      · 疑问（"原神关了吗" —— 那是在问状态）；
      · 游戏内设置（"关掉游戏声音/画质/全屏"）。
    剩下的模糊说法（"我不想玩了"）交给大模型出 `system_task`，两条路都会走审批。
    """
    normalized = _normalize(text)
    if not normalized:
        return None
    if _NEGATION.search(normalized):
        print("🛑 识别到「别关/不关」这类否定，忽略关闭意图。")
        return None
    if normalized.endswith(_QUESTION_TAIL):
        return None
    if any(word in normalized for word in _NOT_A_CLOSE_COMMAND):
        return None
    if not _looks_like_close_command(normalized):
        return None

    mentions_bgi = any(word in normalized for word in _BGI_WORDS)
    also = any(word in normalized for word in _ALSO_WORDS)
    return normalize_intent({
        "action": "close_game",
        # 明确提到"一起关/都关 + bgi"才算明确要求连它一起关；否则只在它正忙时自动捎带（见 execute）
        "close_bettergi": bool(mentions_bgi and also),
        "source": "关键词识别",
    })


def normalize_intent(raw):
    """把（LLM 给的 / 关键词给的）意图规整成统一结构。"""
    raw = raw if isinstance(raw, dict) else {}
    action = str(raw.get("action") or "close_game").strip().lower()
    if action not in ("close_game",):
        action = "close_game"
    return {
        "action": action,
        "close_bettergi": bool(raw.get("close_bettergi")),
        "source": str(raw.get("source") or "大模型规划"),
    }


def plan_lines(intent):
    """审批屏上的计划文本（玩家点 y 之前能看清会发生什么）。"""
    intent = normalize_intent(intent)
    lines = ["🛑 系统操作：关闭原神"]

    status = game_running_status()
    if status is True:
        pids = "、".join(
            f"{name} {'/'.join(map(str, pids_list))}"
            for name, pids_list in running_processes().items() if pids_list
        )
        lines.append(f"· 正在运行：{pids}")
    elif status is False:
        lines.append("· 原神当前**没有**在运行（执行时会直接告诉你不用关）")
    else:
        lines.append("· 查不到原神进程（可能没在运行，也可能权限受限）")

    lines.append(
        f"· 关闭方式：先正常关闭（等 {GAME_CLOSE_GRACE_SECONDS} 秒），没退再强制结束"
        "　—— 原神进度在服务器上，强杀不会丢档，只是下次启动会提示上次未正常退出"
    )
    lines.append(
        "· 权限不够时（原神被管理员的 BetterGI/启动器拉起）：自动改走计划任务 StopGenshin"
        "（没注册过的话，按提示以管理员身份跑一次注册脚本即可，命令里会给完整路径）"
    )

    close_bgi, reason = _should_close_bettergi(intent)
    if close_bgi:
        lines.append(f"· 顺带关闭 BetterGI：{reason}")
    elif intent["close_bettergi"]:
        lines.append("· 顺带关闭 BetterGI：你要求的，会一起关")

    lines.append("· 关掉之后要重新启动游戏并登录才能继续跑任务")
    return lines


def _should_close_bettergi(intent):
    """要不要连 BetterGI 一起关，以及为什么。"""
    if intent.get("close_bettergi"):
        return True, "你要求的"
    if bettergi_busy():
        return True, "检测到它正在跑任务（日志里最后一次是「任务启动」），留着它会在没有游戏的情况下报错/空转"
    return False, ""


def execute(intent, open_id=""):
    """执行关闭（**已经过审批**）。返回给玩家看的报告文本，并顺手发出去。"""
    intent = normalize_intent(intent)
    print(f"🛑 执行系统操作：关闭原神（来源：{intent['source']}）")

    lines = []
    trouble = False
    close_bgi, reason = _should_close_bettergi(intent)
    if close_bgi:
        ok, message = close_bettergi()
        trouble = trouble or not ok
        lines.append(("✅ " if ok else "⚠️ ") + message + (f"（{reason}）" if reason else ""))

    result = close_game()
    if result["opened"]:
        ok = not result["still_running"] and not result["unknown"]
        trouble = trouble or not ok
        lines.append(("🛑 原神已关闭：" if ok else "⚠️ 关闭原神的结果不确定：") + result["summary"])
    else:
        lines.append("ℹ️ " + result["summary"])
        trouble = trouble or result["unknown"]

    if trouble:
        lines.append(
            "↳ 还是关不掉的话：手动 Alt+F4；或者以【管理员身份】跑一次注册脚本"
            "（注册 StopGenshin 计划任务，之后免 UAC 就能关）：\n"
            f"     {setup_task_hint()}"
        )

    report = "🧾 关闭操作完成：\n" + "\n".join(f"　 {line}" for line in lines)
    print(report)
    if open_id:
        try:
            from skills.bgi_controller import send_notice

            send_notice(open_id, report)
        except Exception as exc:        # noqa: BLE001 —— 发不出去也不能让执行失败
            print(f"⚠️ 关闭结果通知发送失败：{exc}")
    return report


def _main(argv=None):
    """手动执行（也方便排错）：python -m skills.game_control [--close] [--with-bgi]"""
    import argparse

    parser = argparse.ArgumentParser(description="关闭原神（默认只看看状态）")
    parser.add_argument("--close", action="store_true", help="真的关掉原神")
    parser.add_argument("--with-bgi", action="store_true", help="连 BetterGI 一起关")
    args = parser.parse_args(argv)

    intent = normalize_intent({"action": "close_game", "close_bettergi": args.with_bgi,
                               "source": "命令行"})
    print("\n".join(plan_lines(intent)))
    if not args.close:
        print("\n（只做检查；想真关加 --close）")
        return 0
    execute(intent)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
