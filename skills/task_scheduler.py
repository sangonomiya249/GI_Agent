"""计划任务（免 UAC 的启动 / 关闭助手）—— 唯一的事实来源。

**为什么需要计划任务**：BetterGI 和原神通常都是**以管理员权限**运行的，而 Agent 在普通权限
的终端/机器人里跑。于是：
  · 直接 `taskkill` → `拒绝访问`（低完整性进程不能结束高完整性进程）；
  · 直接启动 BetterGI.exe → 单实例 + 可能弹 UAC，热启动不会触发新的一条龙。
用一个「以最高权限运行」的计划任务代劳，就能免 UAC 完成（Agent 触发任务 → 任务以管理员身份干活）。

三个任务：
  · `StartBetterGI`  —— 免 UAC 拉起 BetterGI 一条龙
  · `StopBetterGI`   —— 以同等权限关闭 BetterGI（热启动前必须先关）
  · `StopGenshin`    —— 以同等权限关闭原神（"帮我关闭原神"指令用；原神被管理员的 BGI 拉起时普通 taskkill 关不掉）

⚠️ 实测踩过的坑：注册脚本要用**绝对路径**调用。
管理员 PowerShell 打开时默认在 `C:\\WINDOWS\\system32`，在那里写
`-File scripts\\setup_start_bettergi_task.ps1` 只会得到"实际参数…不存在"。
所以本模块的 `setup_hint()` 永远返回绝对路径，界面上也照这个显示。
"""

import os
import subprocess

import config

# 任务名 → 用途（required=False 表示"没有也能用，只是对应功能会退化"）
TASKS = (
    {
        "name": "StartBetterGI",
        "purpose": "免 UAC 拉起 BetterGI 一条龙（Agent 执行任务时用）",
        "required": True,
        "script": "scripts/setup_start_bettergi_task.ps1",
    },
    {
        "name": "StopBetterGI",
        "purpose": "以同等权限关闭 BetterGI（热启动无效，必须先关再冷启动）",
        "required": True,
        "script": "scripts/stop_bettergi.ps1",
    },
    {
        "name": "StopGenshin",
        "purpose": "以管理员权限关闭原神（「帮我关闭原神」用；原神被管理员的 BGI 拉起时普通 taskkill 关不掉）",
        "required": False,
        "script": "scripts/stop_genshin.ps1",
    },
)
TASK_NAMES = tuple(item["name"] for item in TASKS)


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setup_script_path():
    return os.path.join(repo_root(), "scripts", "setup_start_bettergi_task.ps1")


def setup_hint():
    """注册这三个任务的命令（**绝对路径**，可直接粘贴进管理员 PowerShell）。"""
    return f'powershell -ExecutionPolicy Bypass -File "{setup_script_path()}"'


def _powershell_path():
    return os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )


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
        except Exception:        # noqa: BLE001
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

    `-ErrorAction SilentlyContinue` 会把"拒绝访问"和"任务不存在"混在一起，
    所以这里显式取异常文案再判断。
    """
    powershell = _powershell_path()
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
    except Exception:        # noqa: BLE001
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


def task_state(task_name):
    """计划任务是否存在：True / False / None（查不出来）。"""
    if os.name != "nt":
        return None
    state = _schtasks_query(task_name)
    if state is not None:
        return state
    return _powershell_task_probe(task_name)


def states():
    """{任务名: True/False/None}。"""
    return {name: task_state(name) for name in TASK_NAMES}


def missing_tasks():
    """确认不存在的任务名（None 不算）。"""
    return [name for name, state in states().items() if state is False]


def describe_states():
    """一行状态描述（给 doctor / 说明书页用）。"""
    icons = {True: "✅", False: "❌", None: "❔"}
    return "｜".join(
        f"{icons[state]}{name}" for name, state in states().items()
    )


def _main(argv=None):
    """手动检查：python -m skills.task_scheduler"""
    import argparse

    parser = argparse.ArgumentParser(description="检查免 UAC 的计划任务")
    parser.parse_args(argv)

    rows = states()
    for name, state in rows.items():
        label = {True: "已注册", False: "缺失", None: "查不出来（权限受限？）"}[state]
        purpose = next(item["purpose"] for item in TASKS if item["name"] == name)
        print(f"{name:16s} {label:16s} {purpose}")
    if any(state is False for state in rows.values()):
        print("\n注册/补齐（需要管理员身份，注意用完整路径）：")
        print(f"  {setup_hint()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
