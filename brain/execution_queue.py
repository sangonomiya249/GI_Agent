"""执行模式：一次性全下发，还是**一条路线一条路线**来。

玩家要的两种方式（`.env` 的 `GROWTH_EXECUTION_MODE`，默认 `all`）：

```text
all（默认）      按原本调度直接全部拉取：energy_task 一个 + free_task 若干
stepwise         先推总路线详情 → 只问第一条"Do this?" → 回 y 就跑这条
                 → 跑完自动推下一条 → 回 y 再跑…直到跑完
```

**没路线的材料直接跳过**：认不出来源的（`waiting_route` / 缺路线）不进队列，
它们的缺口照样显示在明细表里，但不占用批次、也不会让队列卡住。

**为什么状态要落库**：分批次天然跨"好几分钟 + 多次消息往返"，进程重启（玩家关了
Studio / Agent）之后必须还能接着跑下一条，所以队列存在 `settings` 表里
（键 `growth_execution_queue_v1`），而不是放在内存里。

**跑完并不知道材料够没够**：米游社的"已有 / 还差"是算出来的，BetterGI 跑完那一刻
背包并不会立刻刷新（还有同步延迟），所以**不能**用"缺不缺口"来判断这条路线是否成功。
队列只按"任务真的跑完了"推进，并在完成时重新同步一次米游社（§19）。
"""

import json
import threading

from brain import growth_db

QUEUE_KEY = "growth_execution_queue_v1"
_LOCK = threading.RLock()


# ==========================================
# 🌟 模式
# ==========================================

MODE_ALL = "all"
MODE_STEPWISE = "stepwise"
MODES = (MODE_ALL, MODE_STEPWISE)
MODE_LABELS = {
    MODE_ALL: "一次性全部执行",
    MODE_STEPWISE: "分批次（一条一条问）",
}


def mode():
    """当前执行模式（`.env` 的 `GROWTH_EXECUTION_MODE`，认不出就当 all）。"""
    import config

    raw = str(getattr(config, "GROWTH_EXECUTION_MODE", MODE_ALL) or MODE_ALL).strip().lower()
    return raw if raw in MODES else MODE_ALL


def set_mode(value):
    """切模式（写 .env）。写不进去就返回 False，不假装成功。"""
    from skills import env_config

    wanted = str(value or "").strip().lower()
    if wanted not in MODES:
        return False
    try:
        env_config.update_values({"GROWTH_EXECUTION_MODE": wanted})
        import config

        config.GROWTH_EXECUTION_MODE = wanted
        return True
    except Exception as exc:                # noqa: BLE001
        print(f"⚠️ 写 GROWTH_EXECUTION_MODE 失败（{type(exc).__name__}: {exc}）")
        return False


def is_stepwise():
    return mode() == MODE_STEPWISE


# ==========================================
# 🌟 队列
# ==========================================

def _task_command(task):
    """把一个任务转成"只有它自己"的 BetterGI 指令。"""
    kind = task.get("task_type")
    if kind in ("domain",):
        command = {"energy_task": {"action": "run_domain",
                                   "target": task.get("domain") or task.get("route") or task.get("material"),
                                   "count": int(task.get("count") or 1)}}
        if task.get("domain_index"):
            command["energy_task"]["domain_index"] = str(task["domain_index"])
        return command
    if kind == "leyline":
        return {"energy_task": {"action": "run_leyline", "target": task.get("route"),
                                "count": int(task.get("count") or 1)}}
    if kind == "boss":
        return {"energy_task": {"action": "run_boss",
                                "target": task.get("route") or task.get("material"),
                                "count": int(task.get("count") or 1)}}
    action = {"gather": "gather", "hunt": "hunt", "mine": "mine",
              "cook": "cook", "script": "script"}.get(kind)
    if not action:
        return {}
    return {"free_task": [{"action": action, "target": task.get("route") or task.get("material")}]}


def build_steps(tasks):
    """把任务列表折成"一批一条路线"的步骤（**跳过没路线的**，并按优先级排）。

    排序：① 刷取优先级 → ② **当前角色优先** → ③ 阶段 → ④ 缺口大的先来。
    第 ② 条很重要：玩家现在的培养目标是奥黛塔，那她缺的「幻造晶鳞石 → 肌生晶石的妖精」
    就该排在**别人角色的**采集（嘟嘟莲）前面 —— 以前同优先级里只按缺口大小排，
    结果别人的材料跑到了最前面，看起来像"目标搞错了"（被反馈过）。

    去重规则：同一条路线只出现一次（`free_task` 里同一个族群的多档材料已经合并过，
    这里再兜一次底，免得队列里出现两条一模一样的路线）。
    """
    steps = []
    seen = set()
    ordered = sorted(tasks or (), key=lambda item: (
        item.get("priority") or 90,
        0 if item.get("owner_current") else 1,          # 当前角色的排在前面
        item.get("phase") or "",
        -int(item.get("missing") or 0),
    ))
    for task in ordered:
        if int(task.get("missing") or 0) <= 0:
            continue
        command = _task_command(task)
        if not command:
            # 没路线的（waiting_route）或认不出的任务类型：跳过，不占批次
            continue
        key = json.dumps(command, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        steps.append({
            "index": len(steps) + 1,
            "command": command,
            "material": task.get("material") or "",
            "item_id": int(task.get("item_id") or 0),
            "task_type": task.get("task_type") or "",
            "task_label": task.get("task_label") or "",
            "route": task.get("route") or "",
            "family": task.get("family") or "",
            "character_name": task.get("character_name") or "",
            "phase_label": task.get("phase_label") or "",
            "missing": int(task.get("missing") or 0),
            "count": int(task.get("count") or 1),
            "resin": int(task.get("resin") or 0),
            "priority_label": task.get("priority_label") or "",
            "count_note": task.get("count_note") or "",
            "status": "pending",
        })
    return steps


def render_steps(plan_or_tasks, limit=12):
    """总路线详情（分批次时先推这个）。"""
    tasks = plan_or_tasks
    if isinstance(plan_or_tasks, dict):
        tasks = (plan_or_tasks.get("execution") or {}).get("tasks") or []
    steps = build_steps(tasks)
    if not steps:
        return ["📋 这一轮没有可执行的路线（材料都在冷却 / 缺路线 / 已满足）"]
    lines = [f"📋 这一轮共 {len(steps)} 条路线，按优先级排："]
    for step in steps[:limit]:
        head = f"{step['index']}. {step['priority_label']}｜{step['task_label']}｜{step['material']}"
        if step.get("route") and step["route"] != step["material"]:
            head += f" → {step['route']}"
        if step.get("resin"):
            head += f"（{step['count']} 趟 / {step['resin']} 体力）"
        lines.append(head)
    if len(steps) > limit:
        lines.append(f"…还有 {len(steps) - limit} 条")
    skipped = [task for task in (tasks or ()) if task.get("status") == "waiting_route"]
    if skipped:
        lines.append(f"（另有 {len(skipped)} 种材料没有可执行路线，已跳过）")
    return lines


def render_current(state):
    """当前这一步的问句（含 y/n 提示）。

    ⚠️ **采集 / 魔物掉落这类路线不报数量**：一条路线会打掉一整片怪、掉一堆材料，
    写"还要 13 个，本趟 1 次"既不准也没用（玩家反馈："没必要这样反馈"）。
    只有吃体力的路线（秘境 / 地脉 / 首领）才报"趟数 / 体力"，因为那边趟数真的是你选的。

    一律用 `.get()`：队列是**跨版本持久化**的（存在 settings 表里），
    旧版本存下的步骤可能少字段 —— 硬下标会在渲染时 KeyError，把推送整条搞崩。
    """
    state = state or load()
    step = current(state)
    if not step:
        return ["✅ 这一轮的路线都跑完了。"]
    total = len(state.get("steps") or []) or 1
    kind = step.get("task_type") or ""
    resin_kind = kind in ("domain", "leyline", "boss")
    lines = [f"▶️ 第 {step.get('index')}/{total} 条路线：{step.get('material') or '—'}"
             f"（{step.get('character_name') or '—'}｜{step.get('phase_label') or '—'}）"]
    if step.get("priority_label"):
        lines.append(f"   · {step['priority_label']}")
    if step.get("route"):
        lines.append(f"   · 路线：{step.get('task_label') or ''} → {step['route']}")
    if resin_kind:
        lines.append(f"   · 还要 {int(step.get('missing') or 0):,} 个，"
                     f"本趟 {int(step.get('count') or 1)} 次")
        if step.get("resin"):
            lines.append(f"   · 预计耗体力 {step['resin']}")
        if step.get("count_note"):
            lines.append(f"   · {step['count_note']}")
    lines.append("👉 y = 就跑这一条　其它内容 = 放弃队列，按你说的来")
    return lines


# ==========================================
# 🌟 状态存取（落库，重启不丢）
# ==========================================

def _load_raw():
    try:
        value = growth_db.get_setting(QUEUE_KEY)
    except Exception:                       # noqa: BLE001
        return None
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:                       # noqa: BLE001
        return None


def _save_raw(state):
    try:
        # 队列本身不写"库存事实"，只记"跑到哪一条了"，所以存 settings 是合适的（§29）
        growth_db.set_setting(QUEUE_KEY, json.dumps(state or {}, ensure_ascii=False))
        return True
    except Exception as exc:                # noqa: BLE001
        print(f"⚠️ 保存执行队列失败（{type(exc).__name__}: {exc}）")
        return False


def load():
    """当前队列状态（没有就返回 `{"steps": [], "cursor": 0, "active": False}`）。"""
    with _LOCK:
        state = _load_raw() or {}
    state.setdefault("steps", [])
    state.setdefault("cursor", 0)
    state.setdefault("active", False)
    return state


def current(state=None):
    """当前待执行的那一步（没有给 `None`）。"""
    state = state if state is not None else load()
    if not state.get("active"):
        return None
    steps = state.get("steps") or []
    cursor = int(state.get("cursor") or 0)
    if 0 <= cursor < len(steps):
        return steps[cursor]
    return None


def start(tasks, plan_id=0, character_id=0):
    """按当前任务列表起一个新队列。返回新状态。"""
    steps = build_steps(tasks)
    state = {
        "steps": steps,
        "cursor": 0,
        "active": bool(steps),
        "plan_id": int(plan_id or 0),
        "character_id": int(character_id or 0),
        "done": [],
    }
    with _LOCK:
        _save_raw(state)
    return state


def _route_keys(steps):
    """这一步"是哪条路线"（不含数量）—— 用来判断队列要不要重建。"""
    return tuple((step.get("task_type") or "", step.get("route") or "",
                  step.get("material") or "") for step in steps or ())


def sync(tasks, plan_id=0, character_id=0):
    """让持久化的队列和**当前计划**对齐（不一致就重建；一致则刷新数字但保留进度）。

    **为什么必须有这个函数**：队列是落库的（要跨"跑完一条 → 推下一条"），但计划随时会变
    —— 体力变了、材料缺口变了、角色换了。实测踩过：启动推送里直接用**上一次存下的**队列，
    结果"路线列表"是新计划的 3 条，而"当前那一条"还是旧队列里的 Boss（写着
    「读不到体力、160 体力」）—— 两边对不上，玩家看到的全是过时数字。

    对齐规则：
      · 路线集合变了（或者队列已经跑完）→ 按新计划**重建**，游标归零；
      · 路线没变 → **保留游标**（别把玩家跑到一半的进度清掉），但把每一步的
        数量 / 体力 / 说明刷新成最新算出来的，免得显示旧数字。
    """
    fresh = build_steps(tasks)
    with _LOCK:
        state = load()
        if state.get("active") and _route_keys(state.get("steps")) == _route_keys(fresh):
            # 路线没变：保留进度，只把数字刷新一遍
            refreshed = []
            for step in state.get("steps") or ():
                current = next((item for item in fresh
                                if _route_keys([item]) == _route_keys([step])), None)
                if current:
                    merged = dict(step)
                    for key in ("count", "resin", "missing", "count_note",
                                "priority_label", "character_name", "phase_label",
                                "status", "command"):
                        if key in current:
                            merged[key] = current[key]
                    refreshed.append(merged)
                else:
                    refreshed.append(step)
            state["steps"] = refreshed
            state["plan_id"] = int(plan_id or state.get("plan_id") or 0)
            _save_raw(state)
            return state
    return start(tasks, plan_id=plan_id, character_id=character_id)


def advance(step_result=None):
    """当前这条跑完了 → 游标往后走一格。返回 `(state, next_step)`。"""
    with _LOCK:
        state = load()
        step = current(state)
        if step is not None:
            done = list(state.get("done") or [])
            done.append({"index": step.get("index"), "material": step.get("material"),
                         "route": step.get("route"), "result": step_result or "done"})
            state["done"] = done[-50:]
            state["cursor"] = int(state.get("cursor") or 0) + 1
            steps = state.get("steps") or []
            if state["cursor"] >= len(steps):
                state["active"] = False
            _save_raw(state)
    return state, current(state)


def skip(reason="等待用户处理其它请求"):
    """弃掉整个队列（玩家回了别的内容时用）。"""
    with _LOCK:
        state = load()
        state["active"] = False
        state["stopped_reason"] = str(reason or "")
        _save_raw(state)
    return state


def preview(tasks):
    """**不改落库状态**地算一份"按当前计划应该是怎样的队列"（界面 / 推送用）。

    为什么需要：`status()` 读的是**上次存下的**队列，计划一变它就是过期数据 ——
    实测踩过：界面显示"共 3 条路线"，当前那一条却是旧队列里的 Boss（还写着
    「读不到体力、160 体力」）。界面要看到的是"按现在这份计划排出来是什么样"。
    """
    fresh = build_steps(tasks)
    with _LOCK:
        state = load()
    if state.get("active") and _route_keys(state.get("steps")) == _route_keys(fresh):
        aligned = dict(state)                    # 路线没变 → 沿用已有进度
    else:
        aligned = {"steps": fresh, "cursor": 0, "active": bool(fresh),
                   "done": state.get("done") or [], "plan_id": state.get("plan_id") or 0}
    return status(aligned)


def status(state=None):
    """给界面用的一份摘要（默认读落库的那份；也可以传一份算好的）。"""
    state = load() if state is None else state
    steps = state.get("steps") or []
    step = current(state)
    return {
        "mode": mode(),
        "mode_label": MODE_LABELS.get(mode(), mode()),
        "active": bool(state.get("active")),
        "total": len(steps),
        "cursor": int(state.get("cursor") or 0),
        "done": len(state.get("done") or []),
        "routes": [{"index": item.get("index"), "material": item.get("material"),
                    "route": item.get("route"), "task_label": item.get("task_label"),
                    "count": item.get("count"), "resin": item.get("resin"),
                    "priority_label": item.get("priority_label"),
                    "character_name": item.get("character_name")} for item in steps],
        "current": ({
            "index": step.get("index"), "material": step.get("material"),
            "route": step.get("route"), "task_label": step.get("task_label"),
            "count": step.get("count"), "resin": step.get("resin"),
            "priority_label": step.get("priority_label"),
        } if step else None),
    }
