"""「使用说明」页的数据：把"该配什么"变成一份能看状态的清单。

界面上的说明文字是静态的（写在 web/index.html 里），这里只负责**实时状态**：
每一步做没做、缺什么、怎么补。所有检查都包了 try/except —— 说明书页面自己绝不能报错。
"""

import os
import subprocess

import config

from skills import health_check, route_group

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_TODO = "todo"
STATUS_UNKNOWN = "unknown"
# 可选功能（没配不算问题，只是"没启用"）—— 别在说明书里报红，否则玩家以为哪里没配好
STATUS_INFO = "info"

# 一条龙里常见要添加的调度器（名字必须与 User\ScriptGroup\<名字>.json 完全一致）
EXPECTED_TASKS = (
    {
        "name": "地图素材",
        "purpose": "采角色突破特产（清心 / 清水玉 / 慕风蘑菇…）",
        "env_field": "BGI_MAP_CONFIG_NAME",
        "auto_group": False,
        "auto_register": True,
    },
    {
        "name": "敌人与魔物",
        "purpose": "按魔物刷固定怪点（骗骗花 / 蕈兽 / 刀镡…）",
        "env_field": "BGI_ENEMY_CONFIG_NAME",
        "auto_group": False,
        "auto_register": True,
    },
    {
        "name": "锄大地",
        "purpose": "锄地专区整片扫图（小怪2000 / 精英400 / 挪德卡莱）",
        "env_field": "BGI_HOE_CONFIG_NAME",
        "auto_group": True,
        "auto_register": True,
    },
    {
        "name": "矿物",
        "purpose": "挖矿（水晶块 / 紫晶块 / 星银矿石…）",
        "env_field": "BGI_MINE_CONFIG_NAME",
        "auto_group": False,
        "auto_register": True,
    },
    {
        "name": "食材与炼金",
        "purpose": "食材与炼金材料（禽肉 / 鱼肉 / 久雨莲…）",
        "env_field": "BGI_COOK_CONFIG_NAME",
        "auto_group": False,
        "auto_register": True,
    },
    {
        "name": "批量讨伐角色养成材料BOSS",
        "purpose": "突破材料 Boss 讨伐（Agent 会写它的 assets/config/config.json）",
        "auto_group": False,
        "auto_register": True,
    },
    {
        "name": "狗粮AAA",
        "purpose": "整脚本任务：AAA 狗粮批发（只捡狗粮）",
        "auto_group": False,
        "auto_register": True,
    },
)

PS_SCRIPTS = (
    {
        "path": "scripts/setup_start_bettergi_task.ps1",
        "purpose": "注册三个计划任务：StartBetterGI（免 UAC 拉起一条龙）+ StopBetterGI（以同等权限关闭 BetterGI）"
                   " + StopGenshin（以同等权限关闭原神，供「帮我关闭原神」用）",
        "run": r'powershell -ExecutionPolicy Bypass -File "{repo}\scripts\setup_start_bettergi_task.ps1"',
        "notes": "必须以【管理员身份】运行一次；⚠️ 要用**完整路径**（管理员 PowerShell 默认在 C:\\WINDOWS\\system32，"
                 "相对路径会报「实际参数…不存在」）。可选参数 -BgiDir \"D:\\Games\\BetterGI\"、-RunTest（注册后试跑）、"
                 "-Uninstall（删除这三个任务）。",
    },
    {
        "path": "scripts/stop_bettergi.ps1",
        "purpose": "由计划任务 StopBetterGI 调用：按 PID 精确关闭 BetterGI（先温和 /PID，10 秒不退再 /F）",
        "run": r'powershell -ExecutionPolicy Bypass -File "{repo}\scripts\stop_bettergi.ps1"          # 手动：按进程名关全部加 -All',
        "notes": "故意只杀记录在 scripts\\stop_bettergi.pids 里的 PID：按进程名杀会误杀 Agent 刚冷启动的新实例（表现是「刚拉起来 1 秒又被关掉」）。",
    },
    {
        "path": "scripts/stop_genshin.ps1",
        "purpose": "由计划任务 StopGenshin 调用：按 PID 精确关闭原神（先温和 /PID，12 秒不退再 /F）",
        "run": r'powershell -ExecutionPolicy Bypass -File "{repo}\scripts\stop_genshin.ps1"           # 手动：按进程名关全部加 -All',
        "notes": "只杀 PID 文件里记录、且**当前进程名在白名单**（YuanShen / GenshinImpact）的目标；"
                 "PID 记录超过 5 分钟视为过期（防 PID 复用误杀）。原神进度在服务器上，强杀不丢档。",
    },
)


def _step(step_id, title, status, detail, fix=""):
    return {"id": step_id, "title": title, "status": status, "detail": detail, "fix": fix}


def _bettergi_step():
    if not os.path.isdir(config.BGI_DIR):
        return _step(
            "bettergi",
            "BetterGI 已安装",
            STATUS_TODO,
            f"找不到目录：{config.BGI_DIR}",
            "在「配置」页把 BGI_DIR 指到 BetterGI 安装目录（默认 C:\\Program Files\\BetterGI）。",
        )
    exe = os.path.join(config.BGI_DIR, str(config.BGI_EXE or "BetterGI.exe"))
    if not os.path.isfile(exe):
        return _step("bettergi", "BetterGI 已安装", STATUS_WARN, f"目录在，但没找到 {exe}", "检查 BGI_EXE 配置。")
    return _step("bettergi", "BetterGI 已安装", STATUS_OK, config.BGI_DIR)


def _llm_step(env_values):
    rows = health_check.check_llm_config(env_values)
    errors = [text for level, text in rows if level == "error"]
    warns = [text for level, text in rows if level == "warn"]
    if errors:
        return _step("llm", "LLM 模型与密钥已配置", STATUS_TODO, "；".join(errors), "在「配置 → LLM 模型」里填提供商、模型名和对应密钥。")
    detail = "；".join(text for _level, text in rows if _level == "ok") or "已配置"
    return _step("llm", "LLM 模型与密钥已配置", STATUS_WARN if warns else STATUS_OK, detail)


def _uid_step(env_values):
    """只看 .env（不再回退 os.environ）：环境变量可能是这份 .env 之外的旧值，会骗人。"""
    uid = str(env_values.get("DEFAULT_UID") or "").strip()
    if not uid:
        return _step("uid", "填了原神 UID", STATUS_TODO, "没填 UID 时抓不到展柜数据", "在「配置 → 玩家与记忆」填写 DEFAULT_UID。")
    return _step("uid", "填了原神 UID", STATUS_OK, f"UID {uid}")


def _one_dragon_step(env_values):
    from skills import bgi_controller

    try:
        path = bgi_controller.resolve_one_dragon_config_path()
    except Exception as exc:  # pragma: no cover - 环境相关
        return _step("one_dragon", "一条龙配置可读", STATUS_WARN, str(exc))
    if not os.path.isfile(path):
        return _step(
            "one_dragon",
            "一条龙配置可读",
            STATUS_TODO,
            f"找不到 {path}",
            "先在 BetterGI 里创建一条龙配置并至少打开一次。",
        )

    names = set()
    try:
        import json

        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        names = {str(value) for value in (data.get("TaskDefinitions") or {}).values()}
    except Exception:  # pragma: no cover - 环境相关
        pass

    if not names:
        return _step(
            "one_dragon",
            "一条龙配置可读",
            STATUS_WARN,
            f"{os.path.basename(path)} 里没有任务登记（TaskDefinitions 为空）",
            "在 BetterGI 的「一条龙」界面把需要的脚本组添加进去。",
        )
    return _step("one_dragon", "一条龙配置可读", STATUS_OK, f"{os.path.basename(path)}｜已登记 {len(names)} 个任务")


def _script_group_step():
    missing = [row["name"] for row in EXPECTED_TASKS if not os.path.isfile(group_path(row["name"]))]
    if missing:
        return _step(
            "script_groups",
            "关键调度器脚本组存在",
            STATUS_TODO if len(missing) == len(EXPECTED_TASKS) else STATUS_WARN,
            "缺：" + "、".join(missing),
            "在 BetterGI 的「调度器」里把对应的脚本/仓库目录建成同名脚本组（锄大地缺失时 Agent 会自动生成）。",
        )
    return _step("script_groups", "关键调度器脚本组存在", STATUS_OK, f"{len(EXPECTED_TASKS)} 个都在")


def _strategy_step():
    broken = []
    for path in route_group.list_group_files(config.BGI_SCRIPT_GROUP_DIR):
        group = route_group.load_group(path)
        if not group:
            continue
        if route_group.group_strategy_notice(group):
            strategy = (
                ((group.get("config") or {}).get("pathingConfig") or {})
                .get("autoFightConfig", {})
                .get("strategyName")
            )
            broken.append(f"{group.get('name') or os.path.basename(path)}（{strategy}）")
    if broken:
        return _step(
            "strategies",
            "各脚本组的战斗策略文件都存在",
            STATUS_WARN,
            f"{len(broken)} 个组的策略在 User\\AutoFight 下找不到：{'、'.join(broken[:6])}"
            + ("…" if len(broken) > 6 else ""),
            "把组里的 strategyName 改成「根据队伍自动选择」，或把策略 txt 复制进 User\\AutoFight。",
        )
    return _step("strategies", "各脚本组的战斗策略文件都存在", STATUS_OK, "全部对得上")


def _pathing_step():
    rows = []
    for action, spec in route_group.category_specs().items():
        if not spec.folder_prefix:
            continue
        directory = os.path.join(config.BGI_AUTO_PATHING_DIR, spec.folder_prefix)
        count = len(route_group.route_files_under(directory)) if os.path.isdir(directory) else 0
        rows.append(f"{spec.label} {count}")
    if not os.path.isdir(config.BGI_AUTO_PATHING_DIR):
        return _step(
            "pathing",
            "脚本仓库的路径追踪已同步",
            STATUS_WARN,
            f"没有 {config.BGI_AUTO_PATHING_DIR}",
            "在 BetterGI 的「脚本仓库」里订阅并同步一次（跑过任一跑图任务后就会生成）。",
        )
    return _step("pathing", "脚本仓库的路径追踪已同步", STATUS_OK, "｜".join(rows))


def _schtasks_query(task_name):
    """schtasks 查询：True / False / None（判不出来）。"""
    for task_path in (f"\\{task_name}", task_name):
        try:
            probe = subprocess.run(
                ["schtasks.exe", "/query", "/tn", task_path],
                capture_output=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return None
        if probe.returncode == 0:
            return True
        text = ((probe.stdout or b"") + (probe.stderr or b"")).decode("utf-8", "replace").lower()
        # 只有这一句才说明"任务确实不存在"；其它错误（Access denied / 找不到路径）都不能当不存在
        if "cannot find the file" in text or "找不到指定的文件" in text:
            return False
    return None


def _powershell_task_probe(task_name):
    """PowerShell 第二意见（schtasks 被安全软件/沙箱拦住时用）。

    注意：`-ErrorAction SilentlyContinue` 会把错误藏起来 —— 那样"拒绝访问"和"任务不存在"
    就分不开了（实测在受限环境里两种都会返回空）。这里显式取异常文案：
      · `No MSFT_ScheduledTask ...`  → 任务确实不存在（正常机器上的标准提示）
      · `拒绝访问 / Access is denied` → 判不出来，返回 None
    """
    powershell = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    if not os.path.isfile(powershell):
        return None
    script = (
        f"try {{ Get-ScheduledTask -TaskName '{task_name}' -ErrorAction Stop | Out-Null; 'YES' }} "
        "catch { 'ERR: ' + $_.Exception.Message }"
    )
    try:
        probe = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    if probe.returncode != 0:
        return None

    output = ((probe.stdout or b"") + (probe.stderr or b"")).decode("utf-8", "replace").strip()
    upper = output.upper()
    if upper.endswith("YES"):
        return True
    if "MSFT_SCHEDULEDTASK" in upper and "NOT FOUND" in upper:
        return False
    if "未找到" in output or "不存在" in output:
        return False
    return None


def _schtasks_query(task_name):
    """schtasks 查询（实现在 `skills/task_scheduler.py`，这里保留薄封装方便测试打桩）。"""
    from skills import task_scheduler

    return task_scheduler._schtasks_query(task_name)


def _powershell_task_probe(task_name):
    """PowerShell 第二意见（实现在 `skills/task_scheduler.py`）。"""
    from skills import task_scheduler

    return task_scheduler._powershell_task_probe(task_name)


def scheduled_task_state(task_name):
    """计划任务是否存在：True / False / None（查不出来）。

    探测逻辑与 CLI 的 doctor 共用 `skills/task_scheduler.py`，别各写一套
    （两边对"拒绝访问 ≠ 不存在"的判断必须一致，否则一边说缺一边说在）。
    """
    if os.name != "nt":
        return None
    state = _schtasks_query(task_name)
    if state is not None:
        return state
    return _powershell_task_probe(task_name)


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _scheduled_tasks_detail():
    """三个计划任务的逐条状态（说明书页用表格/列表展示）。"""
    from skills import task_scheduler

    rows = []
    for item in task_scheduler.TASKS:
        state = scheduled_task_state(item["name"])
        rows.append({
            "name": item["name"],
            "purpose": item["purpose"],
            "required": item["required"],
            "state": {True: "registered", False: "missing", None: "unknown"}[state],
            "script": item["script"],
        })
    return rows


def _scheduled_step():
    from skills import task_scheduler

    states = {item["name"]: scheduled_task_state(item["name"]) for item in task_scheduler.TASKS}
    start = states.get("StartBetterGI")
    stop = states.get("StopBetterGI")
    genshin = states.get("StopGenshin")

    hint = task_scheduler.setup_hint()
    if start is None or stop is None or genshin is None:
        return _step(
            "scheduled",
            "免 UAC 的计划任务已注册",
            STATUS_UNKNOWN,
            f"查不到计划任务状态（schtasks 与 PowerShell 都没给出结果，通常是权限受限）：{task_scheduler.describe_states()}",
            f"在管理员 PowerShell 里确认：Get-ScheduledTask -TaskName {', '.join(task_scheduler.TASK_NAMES)}；"
            f"缺了就跑一次（注意用完整路径）：\n{hint}",
        )

    names = "、".join(task_scheduler.TASK_NAMES)
    if start and stop and genshin:
        return _step(
            "scheduled",
            "免 UAC 的计划任务已注册",
            STATUS_OK,
            f"{names} 都在（含关原神用的 StopGenshin）",
        )
    if start and stop:
        # 核心两个都在，只是关原神那步没配：算"建议"，不算待办
        return _step(
            "scheduled",
            "免 UAC 的计划任务已注册",
            STATUS_WARN,
            "StartBetterGI + StopBetterGI 都在；缺 StopGenshin（关原神用）"
            "—— 原神被管理员权限的 BetterGI 拉起时，普通权限关不掉它。",
            f"以管理员身份跑一次（注意用完整路径）：\n{hint}",
        )
    missing = [name for name, ok in states.items() if not ok]
    return _step(
        "scheduled",
        "免 UAC 的计划任务已注册",
        STATUS_TODO,
        "缺：" + "、".join(missing),
        f"以管理员身份运行一次（注意用完整路径，管理员 PowerShell 默认在 C:\\WINDOWS\\system32）：\n{hint}\n"
        f"它会注册 {names} 三个任务：免 UAC 拉起一条龙 / 关闭 BetterGI / 关闭原神。\n"
        "没有它们，Agent 只能直接启动 BetterGI（可能弹 UAC，也关不掉管理员权限的实例、关不掉原神）。",
    )


def _runtime_step():
    try:
        import flask  # noqa: F401

        flask_ok = True
    except Exception:
        flask_ok = False
    tkinter_ok = True
    try:
        import tkinter  # noqa: F401
    except Exception:
        tkinter_ok = False

    detail = f"Flask {'可用' if flask_ok else '缺失'}｜tkinter {'可用' if tkinter_ok else '缺失（旧版控制台不可用）'}"
    return _step("runtime", "运行环境完整", STATUS_OK if flask_ok else STATUS_WARN, detail)


def group_path(name, group_dir=None):
    directory = group_dir or config.BGI_SCRIPT_GROUP_DIR
    return os.path.join(directory, f"{name}.json")


def expected_tasks(group_dir=None, registered_names=None, env_values=None):
    """该添加哪些调度器 + 每个的实时状态。

    组名可以用 `.env` 改（`BGI_MAP_CONFIG_NAME` 等）—— 那种情况下按改过的名字去查文件/登记，
    并把实际名字回给界面，否则玩家改了名会看到一堆"缺脚本组"的假告警。
    """
    env_values = env_values or {}
    if registered_names is None:
        from skills import bgi_controller

        try:
            registered_names = bgi_controller._registered_task_names()
        except Exception:  # pragma: no cover - 环境相关
            registered_names = set()
    registered_names = {str(name) for name in (registered_names or ())}

    rows = []
    for row in EXPECTED_TASKS:
        item = dict(row)
        candidates = [item["name"]]
        field = item.get("env_field")
        if field:
            configured = str(env_values.get(field) or "").strip()
            if configured:
                candidates.append(os.path.splitext(os.path.basename(configured))[0])

        has_group = any(os.path.isfile(group_path(name, group_dir)) for name in candidates)
        registered = any(name in registered_names for name in candidates)

        item["has_group"] = has_group
        item["registered"] = registered
        item["candidates"] = candidates
        item["actual_name"] = next(
            (name for name in candidates if name in registered_names or os.path.isfile(group_path(name, group_dir))),
            item["name"],
        )
        if registered:
            item["state"] = "registered"
        elif has_group:
            item["state"] = "group_only"
        else:
            item["state"] = "missing"
        rows.append(item)
    return rows


def _mys_step(env_values):
    """米游社个人战绩（可选步骤）。没配就是"可选未启用"，不是待办 —— 别在说明书里报红。"""
    values = env_values or {}
    cookie = str(values.get("MYS_COOKIE") or "").strip() or config.MYS_COOKIE
    if not cookie:
        return _step(
            "mys",
            "米游社个人战绩（可选）",
            STATUS_INFO,
            "未配置：展柜一次只放 8 个角色，问起展柜外的角色时 Agent 只能回“展柜里没有 TA”。",
            "想查展柜外的角色：拿一份米游社 cookie 填进 .env 的 MYS_COOKIE（步骤见 docs/MYS_COOKIE.md）。",
        )

    from skills import mys_api

    snapshot = mys_api.load_snapshot()
    count = len((snapshot or {}).get("avatars") or [])
    if count:
        detail = (
            f"已配置（{mys_api.mask_cookie(cookie)}）｜缓存 {count} 个角色｜"
            f"更新于 {mys_api.describe_snapshot_age(snapshot)}"
        )
        return _step("mys", "米游社个人战绩（可选）", STATUS_OK, detail)
    return _step(
        "mys",
        "米游社个人战绩（可选）",
        STATUS_INFO,
        f"已配置（{mys_api.mask_cookie(cookie)}），但还没拉过角色名单。",
        "第一次遇到展柜外角色时会自动拉；也可以现在跑 python -m skills.mys_api --check 验证 cookie。",
    )


def build_guide(env_path=None, group_dir=None, env_values=None):
    """返回说明书页面要用的实时数据。"""
    from skills import env_config

    if env_values is None:
        env_path = env_path or config.project_path(".env")
        env_values = env_config.load_env(env_path)

    steps = []
    for factory in (
        lambda: _bettergi_step(),
        lambda: _llm_step(env_values),
        lambda: _uid_step(env_values),
        lambda: _one_dragon_step(env_values),
        lambda: _script_group_step(),
        lambda: _strategy_step(),
        lambda: _pathing_step(),
        lambda: _mys_step(env_values),
        lambda: _scheduled_step(),
        lambda: _runtime_step(),
    ):
        try:
            steps.append(factory())
        except Exception as exc:  # pragma: no cover - 说明书页面必须永远能打开
            steps.append(_step("error", "检查项执行失败", STATUS_UNKNOWN, str(exc)))

    todo = sum(1 for step in steps if step["status"] == STATUS_TODO)
    warn = sum(1 for step in steps if step["status"] == STATUS_WARN)
    unknown = sum(1 for step in steps if step["status"] == STATUS_UNKNOWN)
    optional = sum(1 for step in steps if step["status"] == STATUS_INFO)

    return {
        "ok": True,
        "steps": steps,
        "summary": {"total": len(steps), "todo": todo, "warn": warn, "unknown": unknown,
                    "optional": optional,
                    "done": len(steps) - todo - warn - unknown - optional},
        "expected_tasks": expected_tasks(group_dir=group_dir, env_values=env_values),
        # `run` 里用 {repo} 占位，这里统一换成**绝对路径** —— 实测踩过：管理员 PowerShell 默认在
        # C:\WINDOWS\system32，界面里给相对路径会让人复制过去就报"实际参数不存在"。
        "ps_scripts": [
            {**row, "run": row["run"].format(repo=_repo_root())} for row in PS_SCRIPTS
        ],
        "scheduled_tasks": _scheduled_tasks_detail(),
        "paths": {
            "bgi_dir": config.BGI_DIR,
            "script_group_dir": config.BGI_SCRIPT_GROUP_DIR,
            "one_dragon_dir": os.path.join(config.BGI_DIR, "User", "OneDragon"),
            "auto_pathing_dir": config.BGI_AUTO_PATHING_DIR,
            "env_path": env_path,
        },
    }
