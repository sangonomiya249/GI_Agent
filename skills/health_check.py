"""环境体检：一条命令看清楚「这套 Agent 现在到底能不能跑、哪里坏了」。

控制台（gui.py）的「维护工具 → 环境体检」按钮和 `python main.py doctor` 都调它。
只读，不写任何配置：BetterGI 的目录、当前生效的一条龙配置、各脚本组路线数、
战斗策略是否真的存在、AutoPathing 里有哪些类目、展柜缓存新鲜度、备份事务数量。

返回 [(级别, 文案)]，级别取 "ok" / "warn" / "error" / "info"。
"""

import json
import os
import subprocess

import config

from skills import route_group


def _ok(text):
    return ("ok", text)


def _warn(text):
    return ("warn", text)


def _error(text):
    return ("error", text)


def _info(text):
    return ("info", text)


def _provider_key_field(provider):
    provider = str(provider or "").lower()
    return {
        "github": "GITHUB_TOKEN",
        "deepseek": "OPENAI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "nvidia": "NVIDIA_API_KEY",
        "custom": "CUSTOM_API_KEY",
        "local": "LOCAL_API_KEY",
    }.get(provider, "")


def check_llm_config(env_values):
    """LLM 配置：提供商对应的密钥 / 模型名 / 超时。

    只看传进来的 `.env` 字典（不读 os.environ）—— 这样控制台里显式指定的配置文件、
    以及单元测试都能得到确定的结果；.env 没写就应当报"没配"。
    """
    rows = []
    env_values = env_values or {}
    provider = str(env_values.get("LLM_PROVIDER") or "").strip()
    model = str(env_values.get("MODEL_NAME") or "").strip()

    if not provider:
        rows.append(_error("LLM_PROVIDER 没配：Agent 起不来（.env 里至少要写提供商和密钥）"))
    else:
        key_field = _provider_key_field(provider)
        if key_field:
            value = str(env_values.get(key_field) or "").strip()
            if value:
                rows.append(_ok(f"LLM 提供商：{provider}（{key_field} 已填写）"))
            else:
                rows.append(_error(f"LLM 提供商是 {provider}，但 {key_field} 是空的 → 调用会直接报错"))
        else:
            rows.append(_warn(f"LLM 提供商「{provider}」不是已知的几个，请确认 llm_brain 支持它"))

    if model:
        rows.append(_ok(f"模型名：{model}"))
    else:
        rows.append(_warn("MODEL_NAME 没配，会退回默认模型名"))

    timeout = str(env_values.get("LLM_TIMEOUT_SECONDS") or "")
    if timeout:
        try:
            if int(timeout) < 60:
                rows.append(_warn(f"LLM_TIMEOUT_SECONDS={timeout} 偏小：推理型模型首次响应可能要几十秒"))
        except ValueError:
            rows.append(_warn(f"LLM_TIMEOUT_SECONDS 不是整数：{timeout}"))
    return rows


def check_bettergi(env_values):
    """BetterGI 本体、关键目录、当前生效的一条龙配置。"""
    rows = []
    bgi_dir = config.BGI_DIR
    if os.path.isdir(bgi_dir):
        rows.append(_ok(f"BetterGI 目录：{bgi_dir}"))
    else:
        rows.append(_error(f"BetterGI 目录不存在：{bgi_dir}（改 .env 的 BGI_DIR）"))
        return rows

    exe = os.path.join(bgi_dir, str((env_values or {}).get("BGI_EXE") or config.BGI_EXE))
    rows.append(_ok(f"主程序：{exe}") if os.path.isfile(exe) else _warn(f"没找到主程序：{exe}"))

    try:
        from skills import bgi_controller

        one_dragon = bgi_controller.resolve_one_dragon_config_path()
        if os.path.isfile(one_dragon):
            rows.append(_ok(f"当前生效的一条龙配置：{os.path.basename(one_dragon)}"))
            with open(one_dragon, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            definitions = data.get("TaskDefinitions") or {}
            enabled = [
                name
                for task_id, name in definitions.items()
                if (data.get("TaskEnabledList") or {}).get(task_id)
            ]
            rows.append(_info(f"一条龙里已登记 {len(definitions)} 个任务，当前勾选：{'、'.join(enabled) or '（无）'}"))
        else:
            rows.append(_warn(f"一条龙配置读不到：{one_dragon}"))
    except Exception as exc:  # pragma: no cover - 环境相关
        rows.append(_warn(f"解析一条龙配置失败：{exc}"))

    if _bettergi_running():
        rows.append(_info("BetterGI 现在正在运行：Agent 执行前会自动关掉它再改配置"))
    else:
        rows.append(_info("BetterGI 当前没有运行"))
    return rows


def check_channels(env_values):
    """远程通道（可选）：飞书 / QQ。没配也能用，只是只能在终端或 Studio 里对话。"""
    values = env_values or {}
    rows = []

    appid = str(values.get("QQ_BOT_APPID") or "").strip()
    secret = str(values.get("QQ_BOT_SECRET") or "").strip()
    allowed = [item for item in str(values.get("QQ_BOT_ALLOWED_USERS") or "").replace("，", ",").split(",") if item.strip()]
    anyone = str(values.get("QQ_BOT_ALLOW_ANYONE") or "").strip() == "1"

    if appid and secret:
        if anyone:
            rows.append(_warn("QQ 机器人：已配置，但 QQ_BOT_ALLOW_ANYONE=1 —— 任何 QQ 用户都能指挥 Agent（危险）"))
        elif allowed:
            rows.append(_ok(f"QQ 机器人：已配置（AppID {appid}），只认白名单里的 {len(allowed)} 个 openid"))
        else:
            rows.append(_warn(
                "QQ 机器人：AppID/密钥有了但没设 QQ_BOT_ALLOWED_USERS —— 会拒收所有指令"
                "（机器人会先把对方 openid 回给他，抄进 .env 再重启）"
            ))
        rows.append(_info("启动 QQ 通道：python main.py qq（只校验 AppID/密钥用 --check）"))
    elif appid or secret:
        rows.append(_warn("QQ 机器人：QQ_BOT_APPID / QQ_BOT_SECRET 只填了一个，两个都要填才能启动"))
    else:
        rows.append(_info("QQ 机器人：未配置（可选）。想用手机遥控就填 QQ_BOT_APPID / QQ_BOT_SECRET，见 docs/QQ_BOT.md"))

    if str(values.get("FEISHU_APP_ID") or "").strip() and str(values.get("FEISHU_APP_SECRET") or "").strip():
        rows.append(_ok("飞书机器人：已配置（Webhook 模式，需要内网穿透）"))
    return rows


def check_script_groups():
    """调度器脚本组：路线数 + 战斗策略校验。"""
    rows = []
    group_dir = config.BGI_SCRIPT_GROUP_DIR
    if not os.path.isdir(group_dir):
        rows.append(_error(f"调度器脚本组目录不存在：{group_dir}"))
        return rows

    files = route_group.list_group_files(group_dir)
    rows.append(_ok(f"调度器里共有 {len(files)} 个脚本组：{group_dir}"))

    broken = []
    for path in files:
        group = route_group.load_group(path)
        if not group:
            rows.append(_warn(f"{os.path.basename(path)} 读不出来（JSON 坏了？）"))
            continue
        name = group.get("name") or os.path.basename(path)
        notice = route_group.group_strategy_notice(group)
        if notice:
            # 一路 .get 到底：某个组写成 "autoFightConfig": null 时，链式取值会抛 AttributeError，
            # 而本函数被 run_health_check 无条件调用 —— 整份体检报告就没了。
            pathing = (group.get("config") or {}).get("pathingConfig") or {}
            fight = pathing.get("autoFightConfig") or {}
            strategy = fight.get("strategyName")
            broken.append((str(name), str(strategy)))

    if broken:
        names = "、".join(f"{name}（{strategy}）" for name, strategy in broken)
        rows.append(
            _warn(
                f"{len(broken)} 个组的战斗策略在 User\\AutoFight 下找不到文件：{names}\n"
                f"    走到怪点会抛「战斗策略文件不存在」并中断整条路线 —— "
                f"改成「根据队伍自动选择」，或把 txt 复制进 User\\AutoFight。"
            )
        )
    else:
        rows.append(_ok("所有脚本组的战斗策略都能对上文件"))
    return rows


def check_route_sources():
    """AutoPathing 里已解包的类目（锄地专区 这类）路线数。"""
    rows = []
    root = config.BGI_AUTO_PATHING_DIR
    if not os.path.isdir(root):
        rows.append(_warn(f"没找到路径追踪目录：{root}（还没同步脚本仓库时是正常的）"))
        return rows

    summaries = []
    for action, spec in route_group.category_specs().items():
        prefix = spec.folder_prefix
        if not prefix:
            continue
        directory = os.path.join(root, prefix)
        if not os.path.isdir(directory):
            continue
        count = len(route_group.route_files_under(directory))
        group_name = os.path.splitext(spec.group_filename)[0]
        exists = os.path.isfile(os.path.join(config.BGI_SCRIPT_GROUP_DIR, spec.group_filename))
        state = "已有组" if exists else "还没有组（执行时会自动生成）"
        summaries.append(f"{prefix}({spec.label}) {count} 条 / {state}")

    rows.append(_ok("路径追踪目录：" + "；".join(summaries)) if summaries else _warn("路径追踪目录里没有已知类目"))
    return rows


def check_mys_showcase(env_values=None):
    """米游社个人战绩（可选）：没配就一句"未配置"，配了就报告缓存新鲜度（不发请求）。"""
    rows = []
    values = env_values or {}
    cookie = str(values.get("MYS_COOKIE") or "").strip() or config.MYS_COOKIE
    if not cookie:
        rows.append(_info(
            "米游社个人战绩：未配置（可选）。展柜只有 8 个角色，填 MYS_COOKIE 才能查展柜外的角色，见 docs/MYS_COOKIE.md"
        ))
        return rows

    try:
        from skills import mys_api

        snapshot = mys_api.load_snapshot()
        count = len((snapshot or {}).get("avatars") or [])
        age = mys_api.describe_snapshot_age(snapshot)
        if count:
            rows.append(_ok(
                f"米游社个人战绩：已配置（cookie {mys_api.mask_cookie(cookie)}），"
                f"缓存 {count} 个角色，更新于 {age}，TTL {config.MYS_CACHE_TTL_HOURS} 小时"
            ))
        else:
            rows.append(_warn(
                "米游社个人战绩：已配置 cookie，但还没拉过名单"
                "（第一次遇到展柜外角色时才会拉；也可以现在跑 python -m skills.mys_api --check）"
            ))
    except Exception as exc:  # pragma: no cover - 环境相关
        rows.append(_warn(f"读取米游社缓存失败：{exc}"))
    return rows


def check_window_focus():
    """原神窗口前后台相关的 BetterGI 设置（"卡死"的头号原因）。"""
    rows = []
    try:
        import json

        path = os.path.join(config.BGI_DIR, "User", "config.json")
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:  # noqa: BLE001 - 环境相关
        rows.append(_warn(f"读取 BetterGI 全局配置失败（无法检查窗口焦点设置）：{exc}"))
        return rows

    other = data.get("otherConfig") or {}
    restore = other.get("restoreFocusOnLostEnabled")
    capture = str(data.get("captureMode") or "")
    restart = (other.get("autoRestartConfig") or {}).get("enabled")

    if restore:
        rows.append(_ok("BetterGI：已开启「失去焦点时自动切回原神」（前台丢失时它会自己切回来）"))
    else:
        rows.append(_warn(
            "BetterGI：未开启「失去焦点时自动切回原神」（otherConfig.restoreFocusOnLostEnabled=false）"
            "—— 原神一旦不是前台窗口，BGI 会**每秒打印「不是原神，暂停」并停在那里**，"
            "看起来就是卡死。建议在 BetterGI 设置里打开它。"
        ))

    if capture == "BitBlt":
        rows.append(_info(
            "BetterGI 截图方式：BitBlt（默认）。Win11 的「窗口化游戏优化」或悬浮窗遮挡会让它识别失败，"
            "识别不到东西时可以改成 WindowsGraphicsCapture 试试。"
        ))
    elif capture:
        rows.append(_info(f"BetterGI 截图方式：{capture}"))

    if not restart:
        rows.append(_info(
            "BetterGI 自动重启：未开启（otherConfig.autoRestartConfig）—— 任务失败/游戏掉线时不会自动拉起。"
        ))

    from skills import window_focus

    try:
        rows.append(_info(window_focus.describe_foreground()))
    except Exception as exc:  # noqa: BLE001 - 环境相关
        rows.append(_info(f"前台窗口：无法确认（{exc}）"))
    return rows


def check_scheduled_tasks():
    """免 UAC 的三个计划任务（环境检测）。

    为什么要有这一项：BetterGI 和原神常常以管理员权限运行，Agent 在普通权限里跑 ——
    没有这些任务就"启动会弹 UAC / 关不掉 BetterGI / 关不掉原神"，而且现象都很像"卡死"。
    """
    rows = []
    try:
        from skills import task_scheduler

        states = task_scheduler.states()
    except Exception as exc:  # noqa: BLE001 - 环境相关
        rows.append(_warn(f"检查计划任务失败：{exc}"))
        return rows

    missing_required = []
    missing_optional = []
    unknown = []
    for item in task_scheduler.TASKS:
        name = item["name"]
        state = states.get(name)
        if state is True:
            rows.append(_ok(f"计划任务 {name}：已注册（{item['purpose']}）"))
        elif state is False:
            (missing_required if item["required"] else missing_optional).append(name)
        else:
            unknown.append(name)

    if missing_required:
        rows.append(_error(
            f"计划任务缺失：{'、'.join(missing_required)} —— Agent 启动一条龙可能弹 UAC，也关不掉管理员权限的 BetterGI。"
        ))
    if missing_optional:
        rows.append(_warn(
            f"计划任务缺失：{'、'.join(missing_optional)} —— 「帮我关闭原神」会失败"
            "（原神被管理员的 BetterGI 拉起时，普通权限 taskkill 只会得到「拒绝访问」）。"
        ))
    if unknown:
        rows.append(_info(
            f"计划任务状态查不出来：{'、'.join(unknown)}（schtasks 与 PowerShell 都没给出结果，通常权限受限）"
        ))
    if missing_required or missing_optional or unknown:
        rows.append(_info(
            "以【管理员身份】运行一次下面这条命令即可注册/补齐（注意用完整路径，"
            "管理员 PowerShell 默认在 C:\\WINDOWS\\system32）：\n     " + task_scheduler.setup_hint()
        ))
    return rows


def check_close_game():
    """"帮我关闭原神"能不能真的关掉（环境检测：进程 + 权限 + 兜底任务）。"""
    rows = []
    try:
        from skills import game_control

        status = game_control.game_running_status()
        if status is True:
            running = "、".join(
                f"{name} {'/'.join(map(str, pids))}"
                for name, pids in game_control.running_processes().items() if pids
            )
            rows.append(_info(f"原神正在运行：{running}（可以说「帮我关闭原神」）"))
        elif status is False:
            rows.append(_info("原神当前没有在运行"))
        else:
            rows.append(_info("原神进程状态查不出来（权限受限）"))

        rows.append(_info(f"关闭方式：先正常关闭（等 {config.GAME_CLOSE_GRACE_SECONDS} 秒），没退再强制结束；"
                          "权限不够时自动改走计划任务 StopGenshin"))
    except Exception as exc:  # noqa: BLE001 - 环境相关
        rows.append(_warn(f"检查关闭原神能力失败：{exc}"))
    return rows


def check_memory_and_backups():
    """展柜缓存新鲜度 + 配置事务数量。"""
    rows = []
    try:
        from brain import memory_manager
        from skills.env_reader import env_context_age_note

        store = memory_manager.load_chat_store()
        rows.append(_info(f"展柜缓存：{env_context_age_note(store)}"))
    except Exception as exc:  # pragma: no cover - 环境相关
        rows.append(_warn(f"读取展柜缓存失败：{exc}"))

    try:
        from skills.config_recovery import list_transactions

        transactions = list_transactions(config.BGI_BACKUP_DIR)
        if transactions:
            latest = transactions[0]
            rows.append(
                _ok(
                    f"可回滚的配置事务 {len(transactions)} 个（最近：{latest.get('id')} "
                    f"{latest.get('status')} {latest.get('created_at')}）"
                )
            )
        else:
            rows.append(_info("还没有配置事务记录（第一次执行任务后才会出现）"))
    except Exception as exc:  # pragma: no cover - 环境相关
        rows.append(_warn(f"读取配置事务失败：{exc}"))
    return rows


def bettergi_running_status():
    """True / False / **None（判不出来）**。

    三条线索按可靠性排序：
      1. `tasklist`：最准，但进程枚举在某些受限环境（沙箱、部分安全软件）会返回
         "ERROR: Access denied" —— 实测踩过：把"查不到"当成"没在跑"，于是在 BetterGI
         运行时改了它的配置；
      2. BetterGI 日志刚刚被写过（90 秒内）：说明它肯定在跑（空闲时不写日志，所以这条
         只能证明"在跑"，不能证明"没跑"）；
      3. 都判不出来 → None，交给调用方决定（repair 会要求加 --force）。
    """
    if os.name != "nt":
        return None

    try:
        completed = subprocess.run(
            ["tasklist", "/NH"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (completed.stdout or "").lower()
        if completed.returncode == 0 and output.strip():
            target = str(config.BGI_EXE or "BetterGI.exe").strip().lower()
            if target and target in output:
                return True
            if "bettergi" in output:
                return True
            return False
    except Exception:
        pass

    if _log_written_recently(config.BGI_DIR, within_seconds=90):
        return True
    return None


def bettergi_running_label():
    """给界面看的三个字：正在运行 / 没有运行 / 无法确认。"""
    status = bettergi_running_status()
    if status is True:
        return "正在运行"
    if status is False:
        return "没有运行"
    return "无法确认"


def _log_written_recently(bgi_dir, within_seconds=90):
    """BetterGI 的日志文件最近有没有被写过（运行中会持续写）。"""
    import time as _time

    directory = os.path.join(str(bgi_dir), "log")
    if not os.path.isdir(directory):
        return False
    newest = 0.0
    try:
        for name in os.listdir(directory):
            if not name.lower().endswith(".log"):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(directory, name)))
            except OSError:
                continue
    except OSError:
        return False
    return bool(newest) and (_time.time() - newest) <= within_seconds


def _bettergi_running():
    """兼容旧调用：只在**确认**在跑时返回 True（判不出来时按 False 处理）。"""
    return bettergi_running_status() is True


def run_health_check(env_path=None):
    """跑完整套体检，返回 [(级别, 文案)]。"""
    from skills import env_config

    # 用仓库路径而不是 os.getcwd()：从别处启动时 cwd 不是仓库根，会读不到 .env，
    # 于是 doctor 误报「LLM_PROVIDER 没配 / 密钥是空的」
    env_values = env_config.load_env(env_path or config.project_path(".env"))
    rows = []
    rows.extend(check_llm_config(env_values))
    rows.extend(check_channels(env_values))
    rows.extend(check_mys_showcase(env_values))
    rows.extend(check_bettergi(env_values))
    rows.extend(check_scheduled_tasks())
    rows.extend(check_window_focus())
    rows.extend(check_close_game())
    rows.extend(check_script_groups())
    rows.extend(check_route_sources())
    rows.extend(check_memory_and_backups())
    return rows


def format_report(rows, icon_map=None):
    """把体检结果格式化成终端/文本框可读的多行文本。"""
    icons = icon_map or {"ok": "✅", "warn": "⚠️", "error": "❌", "info": "ℹ️"}
    return "\n".join(f"{icons.get(level, '•')} {text}" for level, text in rows)


def main():
    rows = run_health_check()
    print(format_report(rows))
    failed = sum(1 for level, _text in rows if level == "error")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - 手动执行
    raise SystemExit(main())
