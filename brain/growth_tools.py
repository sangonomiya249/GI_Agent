"""养成系统的 Agent 工具层（规格书 §21 / §22）。

**这一层是给"大脑"用的，而且刻意很薄**：每个函数都只是
`brain/growth_planner` / `skills/mys_inventory` 的一层人话包装 ——
没有自己的计算逻辑，也就没有"LLM 算出来的材料数量和真实库存对不上"的可能。

工具清单（与规格书 §21 一一对应）：

| 工具 | 作用 |
| --- | --- |
| `mys_sync_inventory()` | 调米游社 → 存快照 → 返回同步状态 |
| `get_inventory()` | 当前库存 / 最后同步时间 / 是否过期 |
| `get_growth_target(character_id)` | 取指定角色的养成目标 |
| `set_growth_target(...)` | 修改角色目标 |
| `calculate_growth_requirements(character_id)` | 调米游社养成计算器 |
| `get_growth_plan()` | 当前养成计划 |
| `refresh_growth_plan()` | 重新同步 + 重算缺口 |
| `execute_growth_plan()` | 下发可执行的 BetterGI 任务 |

⚠️ 两条硬约束（§38.8 / §38.10）：
  · **LLM 不许自己编材料数量** —— 它只能读这里的返回值；
  · **LLM 不许直接碰 SQLite** —— 它只能调这里的函数，函数内部才碰 `growth_db`。

另外提供 `growth_context_block()`：把"库存 + 当前计划"拼成一段可以直接塞进
system prompt 的文本（形状对齐现有的【展柜外角色参考】注入）。
"""

import json

import config

from brain import growth_db, growth_models, growth_planner, material_planner
from skills import mys_inventory


# ==========================================
# 🌟 库存
# ==========================================


def mys_sync_inventory(uid=None):
    """调米游社 → 拿最新库存 → 存快照。返回同步状态（**失败不抛异常**）。"""
    result = growth_planner.sync_inventory(uid=uid)
    return {
        "ok": bool(result.get("ok")),
        "error": result.get("error") or "",
        "advice": mys_inventory.error_advice(result.get("error_kind")) if not result.get("ok") else "",
        "uid": result.get("uid") or "",
        "item_count": result.get("item_count") or 0,
        "fetched_at": (result.get("inventory") or {}).get("fetched_at") or "",
        "snapshot_file": result.get("snapshot_file") or "",
        "delta": (result.get("delta") or [])[:20],
        "stale": bool(result.get("stale")),
        "last_success_at": result.get("last_success_at") or "",
    }


def get_inventory(item_names=None, limit=40):
    """当前库存：材料数量 + 最后同步时间 + **数据是否过期**。

    `item_names` 给了名字就只回那几种（LLM 问"清心还有多少"时用），
    不然回缺口最大的若干种（全量几千条塞不进上下文）。
    """
    status = mys_inventory.status()
    model = mys_inventory.load_inventory() or {}
    items = list((model.get("items") or {}).values())

    selected = []
    if item_names:
        wanted = {str(name).strip() for name in item_names if str(name or "").strip()}
        selected = [item for item in items if str(item.get("name")) in wanted]
    else:
        selected = sorted(items, key=lambda item: -int(item.get("count") or 0))[: int(limit)]

    return {
        "ok": bool(model),
        "stale": bool(status.get("stale")),
        "warning": status.get("hint") or "",
        "fetched_at": status.get("fetched_at") or "",
        "age_text": status.get("age_text") or "",
        "uid": status.get("uid") or "",
        "server": status.get("server") or "",
        "item_count": status.get("item_count") or 0,
        "items": [
            {
                "item_id": item.get("item_id"),
                "name": item.get("name"),
                "count": item.get("count"),
                "category": item.get("category"),
                "category_label": growth_models.category_label(item.get("category")),
            }
            for item in selected
        ],
    }


def owned_of(item_name):
    """某一种材料现在有多少（找不到返回 `{ok: False}`，**绝不猜数字**）。"""
    item = mys_inventory.find_by_name(item_name)
    if not item:
        return {"ok": False, "name": item_name, "count": 0,
                "error": f"米游社库存里没有「{item_name}」这条材料（可能名字写法不同，或这个号确实没有）"}
    return {"ok": True, "name": item.get("name"), "count": int(item.get("count") or 0),
            "item_id": item.get("item_id"), "category": item.get("category")}


# ==========================================
# 🌟 角色目标
# ==========================================


def get_growth_target(character_id=None, character_name=None):
    """取一个角色的养成目标。给名字也能查（名字→id 走米游社档案 / 本地 characters 表）。"""
    resolved = _resolve_character_id(character_id, character_name)
    if not resolved:
        return {"ok": False, "error": "认不出这个角色，请用米游社里的角色名或角色 id"}
    target = growth_db.get_target(resolved)
    if not target:
        return {
            "ok": False,
            "character_id": resolved,
            "error": f"{character_name or resolved} 还没有配置养成目标（可以先 set_growth_target 建一个）",
        }
    return {"ok": True, "target": _target_view(target)}


def set_growth_target(character_id=None, character_name=None, level=None, normal=None,
                      skill=None, burst=None, weapon_level=None, weapon_enabled=None,
                      enabled=None, priority=None, level_priority=None, weapon_priority=None,
                      talent_priority=None):
    """修改（或新建）角色养成目标。

    等级 1~90 任意值（81 合法）；三个天赋各自 1~10；阶段优先级 1/2/3（越小越先做）。
    """
    resolved = _resolve_character_id(character_id, character_name)
    if not resolved:
        return {"ok": False, "error": "认不出这个角色，请用米游社里的角色名或角色 id"}

    values = {}
    if level is not None:
        values["level_target"] = level
    if normal is not None:
        values["normal_target"] = normal
    if skill is not None:
        values["skill_target"] = skill
    if burst is not None:
        values["burst_target"] = burst
    if weapon_level is not None:
        values["weapon_level_target"] = weapon_level
    if weapon_enabled is not None:
        values["weapon_enabled"] = weapon_enabled
    if enabled is not None:
        values["enabled"] = enabled
    if priority is not None:
        values["priority"] = priority
    if level_priority is not None:
        values["level_priority"] = level_priority
    if weapon_priority is not None:
        values["weapon_priority"] = weapon_priority
    if talent_priority is not None:
        values["talent_priority"] = talent_priority
    if not values:
        return {"ok": False, "error": "没有给任何要改的字段"}

    # 角色不在本地库里就先补上（名字从米游社档案取，拿不到就用调用方给的名字）
    avatars = {}
    if not growth_db.get_character(resolved):
        avatars = growth_planner.fetch_avatars() or {}
        avatar = avatars.get(resolved) or {}
        growth_db.upsert_character(
            resolved, name=avatar.get("name") or str(character_name or ""),
            rarity=avatar.get("rarity") or 0, element=avatar.get("element") or "",
            level=avatar.get("level") or 0,
        )

    # 要纳入武器养成却没记下武器 id：从米游社档案里补（`compute` 需要它）
    if values.get("weapon_enabled"):
        existing = growth_db.get_target(resolved) or {}
        if not existing.get("weapon_id"):
            avatars = avatars or (growth_planner.fetch_avatars() or {})
            weapon_id = ((avatars.get(resolved) or {}).get("weapon") or {}).get("id")
            if weapon_id:
                values["weapon_id"] = weapon_id

    target = growth_db.save_target(resolved, growth_models.normalise_target_values(values))
    growth_planner.invalidate_cache(resolved)
    return {"ok": True, "target": _target_view(target)}


def list_growth_targets():
    """全部养成目标（按执行顺序）。"""
    targets = growth_planner.ordered_targets(enabled_only=False)
    return {"ok": True, "sort_mode": growth_planner.sort_mode(),
            "targets": [_target_view(target) for target in targets]}


def _target_view(target):
    target = target or {}
    return {
        "character_id": target.get("character_id"),
        "character_name": target.get("character_name") or "",
        "rarity": target.get("rarity") or 0,
        "enabled": bool(target.get("enabled")),
        "priority": target.get("priority"),
        "level_target": target.get("level_target"),
        "talents": {
            "normal": target.get("normal_target"),
            "skill": target.get("skill_target"),
            "burst": target.get("burst_target"),
        },
        "weapon": {
            "enabled": bool(target.get("weapon_enabled")),
            "level_target": target.get("weapon_level_target"),
        },
        "phase_priority": growth_models.phase_priority_map(target),
        "phase_order": [growth_models.phase_label(phase)
                        for phase in growth_models.ordered_phases(target)],
        "completion_mode": target.get("completion_mode") or "sequential",
    }


def _resolve_character_id(character_id=None, character_name=None):
    """角色 id 或名字 → 角色 id。名字优先查米游社档案，再查本地 `characters` 表。"""
    if character_id:
        try:
            return int(character_id)
        except (TypeError, ValueError):
            pass
    wanted = str(character_name or "").strip()
    if not wanted:
        return 0
    avatars = growth_planner.fetch_avatars() or {}
    for avatar in avatars.values():
        if str(avatar.get("name")) == wanted:
            return int(avatar["id"])
    for row in growth_db.list_characters():
        if str(row.get("name")) == wanted:
            return int(row["character_id"])
    return 0


# ==========================================
# 🌟 材料需求 / 计划
# ==========================================


def calculate_growth_requirements(character_id=None, character_name=None, force=False):
    """调用米游社养成计算器算一个角色的材料需求（**不自己算**，§10）。"""
    resolved = _resolve_character_id(character_id, character_name)
    if not resolved:
        return {"ok": False, "error": "认不出这个角色"}
    target = growth_db.get_target(resolved)
    if not target:
        return {"ok": False, "character_id": resolved, "error": "这个角色还没有养成目标，先 set_growth_target"}

    computed = growth_planner.requirements_for(target, force=force)
    if computed.get("error"):
        return {
            "ok": False,
            "character_id": resolved,
            "error": computed["error"],
            "advice": mys_inventory.error_advice("not_configured")
            if computed.get("error_kind") == "NotConfigured" else "",
        }
    inventory = mys_inventory.load_inventory()
    rows = []
    for entry in computed.get("requirements") or ():
        required = int(entry.get("required") or 0)
        owned = mys_inventory.owned(entry.get("item_id"), model=inventory)
        rows.append({
            "item_id": entry.get("item_id"),
            "item_name": entry.get("item_name"),
            "category": entry.get("category"),
            "category_label": growth_models.category_label(entry.get("category")),
            "required": required,
            "owned": min(required, owned),
            "missing": max(0, required - owned),
            "phases": [growth_models.phase_label(phase) for phase in entry.get("phases") or ()],
        })
    rows.sort(key=lambda row: -row["missing"])
    return {
        "ok": True,
        "character_id": resolved,
        "character_name": target.get("character_name") or "",
        "source": "米游社养成计算器",
        "requirements": rows,
        "missing_summary": material_planner.describe_missing(rows),
    }


def get_growth_plan(lines=True, limit=20):
    """当前养成计划（**唯一口径**，不要在别处再算一遍缺口）。"""
    plan = growth_planner.build_plan()
    payload = {
        "ok": True,
        "status": plan["status"],
        "status_label": plan["status_label"],
        "current_character": plan["current_character_name"],
        "current_phase": plan["current_phase_label"],
        "inventory": plan["inventory"],
        "summary": plan["summary"],
        "tasks": plan["tasks"][: int(limit)],
        "blockers": plan["blockers"],
        "missing_overview": plan["missing_overview"][: int(limit)],
        "bettergi_cmd": plan["bettergi_cmd"],
        "resin": plan["resin"],
    }
    if lines:
        payload["lines"] = growth_planner.plan_lines(plan, limit=limit)
    return payload


def refresh_growth_plan(sync=True, lines=True):
    """重新同步米游社（可选）+ 重算材料缺口与 BetterGI 任务。"""
    sync_result = None
    if sync:
        sync_result = growth_planner.sync_inventory()
    plan = growth_planner.build_plan(force_compute=True)
    payload = {
        "ok": True,
        "plan": get_growth_plan(lines=False) if plan else {},
    }
    if sync_result is not None:
        payload["sync"] = {
            "ok": bool(sync_result.get("ok")),
            "error": sync_result.get("error") or "",
            "item_count": sync_result.get("item_count") or 0,
        }
    if lines:
        payload["lines"] = growth_planner.plan_lines(plan)
    return payload


def execute_growth_plan(decision="y", step=""):
    """下发当前计划里可执行的 BetterGI 任务。

    ⚠️ **不下发任何库存数字**：BetterGI 跑完后由完成监视触发"重新同步米游社"，
    真实结果以那次同步为准（§19）。

    `GROWTH_EXECUTION_MODE=stepwise` 时**只下发当前这一条路线**：
    队列里的第一条（或 `step="next"` 推进后的那一条），跑完由完成监视自动推下一条。
    """
    plan = growth_planner.build_plan(record=True)
    execution = plan.get("execution") or {}
    tasks = execution.get("tasks") or []
    command = plan.get("bettergi_cmd") or {}

    from brain import execution_queue

    step_info = None
    if execution_queue.is_stepwise():
        if str(step or "").strip().lower() == "next":
            execution_queue.advance("manual")
        state = execution_queue.load()
        if not execution_queue.current(state):
            state = execution_queue.start(tasks, plan_id=plan.get("plan_id") or 0,
                                          character_id=plan.get("current_character_id") or 0)
        current_step = execution_queue.current(state)
        if not current_step:
            return {"ok": False, "error": "分批次：这一轮没有可执行的路线",
                    "lines": execution_queue.render_steps(execution)}
        tasks = [dict(current_step, status="runnable")]
        command = current_step.get("command") or {}
        step_info = {"index": current_step.get("index"),
                     "total": len(state.get("steps") or []),
                     "material": current_step.get("material"),
                     "route": current_step.get("route")}

    if not tasks or not command:
        return {
            "ok": False,
            "error": "当前没有可执行的 BetterGI 任务",
            "blockers": plan.get("blockers") or [],
        }

    growth_planner.register_watcher_hook()
    growth_planner.note_execution(
        plan_id=plan.get("plan_id") or 0,
        character_id=plan.get("current_character_id") or 0,
        phase=plan.get("current_phase") or "",
    )

    from skills import bgi_controller

    store = {}
    try:
        from brain import memory_manager

        store = memory_manager.load_chat_store()
    except Exception:                   # noqa: BLE001
        store = {}

    bgi_controller.execute_bgi_task(command, str(decision or "y").lower(), store,
                                    "CLI_USER", config.DEFAULT_UID)
    return {
        "ok": True,
        "bettergi_cmd": command,
        "tasks": tasks,
        "fallback": bool(execution.get("fallback")),
        "stepwise": bool(step_info),
        "step": step_info,
        "note": ("任务已下发；BetterGI 跑完后会自动重新同步米游社并重算缺口（不写回任何库存数字）。"
                 + (f"　分批次模式：这是第 {step_info['index']}/{step_info['total']} 条路线，"
                    f"跑完会自动推下一条。" if step_info else "")),
    }


# ==========================================
# 🌟 注入给大模型的上下文
# ==========================================


def growth_context_block(plan=None):
    """把"库存 + 当前养成计划"拼成一段可以塞进 prompt 的文本。

    形状对齐现有的【展柜外角色参考】：**先说数据来源和时间**，再给结论，
    最后明确写"不要自己算材料"。没配 cookie / 没同步过时返回空串（不打扰模型）。
    """
    status = mys_inventory.status()
    if not status.get("configured") and not status.get("available"):
        return ""

    lines = ["\n\n🌱 【角色养成系统（数据来源：米游社养成计算器）】"]
    if status.get("available"):
        lines.append(
            f"库存：{'⚠️ 非实时（' + status['age_text'] + '同步的）' if status.get('stale') else '✅ ' + status['age_text'] + '同步'}"
            f"，{status.get('item_count')} 种材料，UID {status.get('uid') or '—'}"
        )
    else:
        lines.append("库存：还没有同步过（材料缺口算不出来）")

    try:
        plan = plan if plan is not None else growth_planner.build_plan(record=False)
    except Exception as exc:            # noqa: BLE001 —— 注入失败不该影响对话
        lines.append(f"养成计划：暂时取不到（{type(exc).__name__}）")
        return "\n".join(lines)

    if not plan.get("characters"):
        lines.append("角色目标：还没有配置任何角色（可以说「把胡桃拉到 90，天赋 10/10/10，武器 90」）。")
        return "\n".join(lines)

    lines.append(f"计划状态：{plan.get('status_label')}｜当前角色："
                 f"{plan.get('current_character_name') or '无'}｜阶段：{plan.get('current_phase_label') or '—'}")
    for row in plan.get("characters", [])[:6]:
        mark = "✅" if row["complete"] else "⏳"
        lines.append(
            f"{mark} {row['character_name']}（{'★' * int(row['rarity'])}）"
            f"当前 {row['current_summary']} → 目标 {row['target_summary']}"
            + (f"，还缺 {row['missing_total']} 个材料" if row["missing_total"] else "")
        )
    for task in (plan.get("tasks") or [])[:8]:
        lines.append(f"· [{task['status_label']}] {task['task_label']}｜{task['material']}"
                     f"（缺 {task['missing']:,}）")
    if plan.get("blockers"):
        lines.append("受阻原因：" + "；".join(blocker["message"] for blocker in plan["blockers"][:3]))

    lines.append(
        "说明：以上材料数量**全部来自米游社养成计算器与背包接口**，"
        "不要自己估算或改动；BetterGI 的拾取数量不可信，库存只在米游社同步后更新。"
    )
    return "\n".join(lines)


def growth_data_source():
    """当前养成数据源：`showcase`（角色展柜，默认）或 `calculator`（养成计算器）。"""
    raw = str(getattr(config, "GROWTH_DATA_SOURCE", "showcase") or "showcase").strip().lower()
    return raw if raw in ("showcase", "calculator") else "showcase"


def growth_source_block(plan=None):
    """按**数据源配置**决定给大模型注入哪一段（玩家要求：默认展柜，可选养成计算器）。

    为什么要有这个开关：聊天里的材料判断一直建立在**角色展柜**上（展柜只放 8 个角色、
    也没有"已有/还差"），而那套数字是估出来的。切成 `calculator` 之后，规划一律以
    **米游社养成计算器**的真实缺口 + **今天该跑的执行路线**为准，模型不许再拿展柜猜材料。

    返回：(block 文本, source 说明)。`showcase` 时 block 仍是原来那段参考
    （行为不变），只是额外说明"当前数据源是展柜"。
    """
    source = growth_data_source()
    if source == "calculator":
        block = growth_context_block(plan=plan)
        if not block:
            return "", source
        plan = plan if plan is not None else growth_planner.build_plan(record=False)
        lines = [block, "", "【执行路线（今天要跑的）】"]
        tasks = (plan.get("execution") or {}).get("tasks") or []
        if tasks:
            for task in tasks[:10]:
                head = f"· {task.get('priority_label') or ''}｜{task.get('task_label')}｜{task.get('material')}"
                if task.get("route") and task["route"] != task["material"]:
                    head += f" → {task['route']}"
                if task.get("resin"):
                    head += f"（{task.get('count')} 趟 / {task.get('resin')} 体力）"
                lines.append(head)
        else:
            lines.append("· （这一轮没有可执行路线：材料已满足 / 没路线 / 都在冷却）")
        estimate = plan.get("estimate") or {}
        if estimate.get("text"):
            lines.append(f"· {estimate['text']}")
        resin = plan.get("current_resin") or {}
        if resin.get("available"):
            lines.append(f"· 当前体力：{resin.get('current')}/{resin.get('max')}")
        else:
            lines.append(f"· 体力：读不到（{resin.get('reason') or '未查询'}）—— 趟数先按缺口排")
        lines.append("说明：**数据源＝养成计算器**。回答养成问题时一律以上面的计划和路线为准，"
                     "不要用角色展柜里的等级/材料去推算缺口 —— 展柜没有「已有/还差」这两个数。")
        return "\n".join(lines), source

    # showcase（默认）：保持原行为，只把"当前数据源"标出来，方便模型知道自己站在哪
    block = growth_context_block(plan=plan)
    if not block:
        return "", source
    return (block + "\n说明：当前数据源＝**角色展柜**（Enka）。材料数量若与上面的养成计算器"
            "参考结果冲突，以养成计算器为准；展柜只用来判断等级/武器/天赋进度。"), source


def tools_manifest():
    """工具清单（给文档 / 自检用；不做 function-calling，纯声明）。"""
    return [
        {"name": "mys_sync_inventory", "summary": "同步米游社库存并保存快照"},
        {"name": "get_inventory", "summary": "当前库存 / 最后同步时间 / 是否过期"},
        {"name": "get_growth_target", "summary": "取指定角色的养成目标"},
        {"name": "set_growth_target", "summary": "修改角色养成目标"},
        {"name": "calculate_growth_requirements", "summary": "调米游社养成计算器算材料需求"},
        {"name": "get_growth_plan", "summary": "获取当前养成计划"},
        {"name": "refresh_growth_plan", "summary": "重新同步并重算材料缺口"},
        {"name": "execute_growth_plan", "summary": "下发可执行的 BetterGI 任务"},
        {"name": "growth_execution_step",
         "summary": "分批次执行：看当前这条路线 / 推进到下一条（stepwise 模式）"},
        {"name": "set_current_resin",
         "summary": "记一次当前体力（米游社体力接口被风控时的主力方案）"},
    ]


def set_current_resin(value, top=None):
    """记一次当前体力（玩家说"现在体力 27"时用）。

    **为什么需要玩家说**：米游社体力接口对某些账号一直被风控挡着（本账号实测
    `5003 账号数据异常`，8 种 cookie/DS/客户端类型组合全被拒）。但体力按 **8 分钟 1 点**
    线性回涨 —— 报一个准数，程序就能往后推算，几小时内的趟数都能按真实体力算，
    而不是硬编码"秘境 2 趟、地脉 6 趟"。
    """
    try:
        from skills import mys_resin
    except Exception as exc:            # noqa: BLE001
        return {"ok": False, "error": f"体力模块不可用：{exc}"}
    result = mys_resin.set_manual(value, top=top)
    if not result.get("ok"):
        return result
    state = result.get("state") or {}
    return {"ok": True, "state": state,
            "line": f"💧 记下了：当前体力 {state.get('current')}"
                    f"（按 8 分钟 1 点往后推算，趟数会按它算）"}


def growth_execution_step(action="show", open_id=""):
    """分批次执行：看当前这条 / 推进到下一条（`GROWTH_EXECUTION_MODE=stepwise` 时用）。

    玩家要的流程是：
        先推总路线详情 → 问第一条"跑吗" → 回 y 就跑这条 → 跑完推下一条 → …
    这个工具负责"说"的那一半（展示 / 推进）；"跑"的那一半仍然走
    `execute_growth_plan()`（它会只下发当前这一条路线）。

    `action`：
      · `show`    —— 看当前队列与这一条路线（不动任何状态）；
      · `start`   —— 按最新计划重新起一个队列；
      · `next`    —— 当前这条跑完了，推进到下一条并返回它的文案；
      · `stop`    —— 放弃队列（玩家回了别的要求时用）。
    """
    from brain import execution_queue

    state = execution_queue.load()
    if action == "start":
        plan = growth_planner.build_plan(record=True)
        tasks = (plan.get("execution") or {}).get("tasks") or []
        execution_queue.start(tasks, plan_id=plan.get("plan_id") or 0,
                              character_id=plan.get("current_character_id") or 0)
        state = execution_queue.load()
    elif action == "next":
        state, _step = execution_queue.advance("manual")
    elif action == "stop":
        return {"ok": True, "action": "stop", "state": execution_queue.status(),
                "lines": ["🚫 已放弃分批次队列。"]}
    else:
        # `show`：跟当前计划对齐后再看（别拿上一次存下的旧队列表述）
        state = execution_queue.load()
        if not execution_queue.current(state):
            plan = growth_planner.build_plan(record=True)
            tasks = (plan.get("execution") or {}).get("tasks") or []
            state = execution_queue.sync(tasks, plan_id=plan.get("plan_id") or 0,
                                         character_id=plan.get("current_character_id") or 0)

    lines = execution_queue.render_current(state)
    return {"ok": True, "action": action, "mode": execution_queue.mode(),
            "state": execution_queue.status(), "lines": lines,
            "current": execution_queue.current(state)}


def _main(argv=None):  # pragma: no cover - 手动自检
    import argparse

    parser = argparse.ArgumentParser(description="养成系统自检 / 手动操作")
    parser.add_argument("--plan", action="store_true", help="打印当前养成计划")
    parser.add_argument("--sync", action="store_true", help="同步米游社库存")
    parser.add_argument("--inventory", metavar="材料名", nargs="*", help="查库存（不给名字就列最多的）")
    parser.add_argument("--target", metavar="角色", help="查看某个角色的目标")
    parser.add_argument("--tools", action="store_true", help="列出 Agent 工具")
    args = parser.parse_args(argv)

    if args.tools:
        print(json.dumps(tools_manifest(), ensure_ascii=False, indent=2))
        return 0
    if args.sync:
        print(json.dumps(mys_sync_inventory(), ensure_ascii=False, indent=2))
        return 0
    if args.inventory is not None:
        print(json.dumps(get_inventory(args.inventory), ensure_ascii=False, indent=2))
        return 0
    if args.target:
        print(json.dumps(get_growth_target(character_name=args.target), ensure_ascii=False, indent=2))
        return 0

    plan = get_growth_plan()
    print("\n".join(plan.get("lines") or []))
    return 0 if plan.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
