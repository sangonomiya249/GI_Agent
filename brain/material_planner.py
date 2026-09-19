"""材料缺口计算与 BetterGI 任务生成（规格书 §17 / §18 / §27 / §30 / §31）。

这个文件回答三个问题，**全部用确定性代码回答，不问 LLM**：

1. **还缺多少**：`plan_requirements()`。库存是共享池 —— 两个角色都要 168 个清心、
   库存只有 10，那缺口是 326 而不是"每人 158"（§17）。所以必须**串行分配**：
   先按角色顺序、再按阶段优先级，一个个扣减 `available`。
2. **去哪儿拿**：`resolve_source()`。优先用本地百科字典里那份材料的**开放日程**
   （能直接给出秘境名和"周几开"），特产/魔物/矿物/食材交给现有 `gather_cooldown`
   的路线索引，摩拉和经验书走地脉花，Boss 材料走讨伐脚本。
3. **今天能不能拿**：`available_today()`。秘境按星期判断（§30）；其余资源按
   `gather_cooldown.status()` 的冷却判断（§27）—— 冷却只用来**标注与排序**，
   真正拦截仍然由 `bgi_controller._filter_cooldown` 那道老关口负责（不重复实现，§27）。

**绝不写回库存**：这里算出来的 `planned` 只是"本轮规划中的分配"，只存在于计划里。
BetterGI 跑完必须重新同步米游社才知道真实结果（§19）。
"""

import datetime
import re

import config

from brain import growth_models, material_family
from skills import domain_match, gather_cooldown, mys_inventory, route_group

# ==========================================
# 🌟 任务类型
# ==========================================

TASK_DOMAIN = "domain"
TASK_BOSS = "boss"
TASK_LEYLINE = "leyline"
TASK_GATHER = "gather"
TASK_HUNT = "hunt"
TASK_MINE = "mine"
TASK_COOK = "cook"
TASK_SCRIPT = "script"
TASK_WAITING_ROUTE = "waiting_route"

TASK_TYPE_LABELS = {
    TASK_DOMAIN: "秘境",
    TASK_BOSS: "Boss 讨伐",
    TASK_LEYLINE: "地脉花",
    TASK_GATHER: "地区特产",
    TASK_HUNT: "敌人与魔物",
    TASK_MINE: "矿物",
    TASK_COOK: "食材与炼金",
    TASK_SCRIPT: "整脚本",
    TASK_WAITING_ROUTE: "缺可执行路线",
}

# 任务状态
STATUS_RUNNABLE = "runnable"          # 今天就能跑
STATUS_COOLDOWN = "cooldown"          # 路线还在冷却（§33「材料处于冷却」）
STATUS_CLOSED_TODAY = "closed_today"  # 秘境今天不开（§30）
STATUS_WAITING_ROUTE = "waiting_route"  # 没有可执行路线（§33「路线不存在」）
STATUS_COMPLETE = "complete"              # 总需求已满足，仅用于展示

STATUS_LABELS = {
    STATUS_RUNNABLE: "可执行",
    STATUS_COOLDOWN: "冷却中",
    STATUS_CLOSED_TODAY: "今日未开放",
    STATUS_WAITING_ROUTE: "缺少路线",
    STATUS_COMPLETE: "已齐",
}

# 地脉花的两种类型（BetterGI 全局配置 autoLeyLineOutcropConfig.leyLineOutcropType 的取值）
LEYLINE_MORA = "藏金之花"
LEYLINE_EXP = "启示之花"

# 一次秘境的材料产出量是"随世界等级变化"的（实测 20 树脂一次约 1~2 个金色材料、
# 若干蓝色材料）。所以这里**故意不按"缺口 ÷ 每次产出"去算趟数** ——
# 那需要世界等级，而我们没有可信来源。改成"一次固定跑几趟，跑完重新同步再看缺口"，
# 这正是规格书 §19 的闭环：宁可多跑两轮，也不要假装算准了。
DOMAIN_RUNS_PER_ROUND = 2
LEYLINE_RUNS_PER_ROUND = 6
# 地脉花一次产出的官方量（BetterGI 记账用的同一组数字，见 skills/bgi_controller.py 的"发工资逻辑"）
MORA_PER_LEYLINE = 480_000
EXP_BOOKS_PER_LEYLINE = 40
# 树脂估算（仅供规划参考，**不作为库存事实来源**，§29）
RESIN_PER_DOMAIN = 20
RESIN_PER_LEYLINE = 20
RESIN_PER_BOSS = 40


# ==========================================
# 🌟 缺口计算（§17 共享池）
# ==========================================


def plan_requirements(characters, inventory=None, compute_lookup=None, now=None):
    """算出**每个角色每个阶段**还缺什么材料。

    参数：
        characters: `[{target, current, requirements}]`，按**执行顺序**排好。
            `requirements` 是 `skills.mys_calculator.compute_materials()` 的 `by_phase`：
            `{"character_level": [...], "weapon_level": [...], "talent": [...]}`。
        inventory:  内部库存模型（`skills.mys_inventory.load_inventory()` 的返回）。
        compute_lookup: 可选回调 `(character, phase) -> [需求条目]`，用于"按需现算"。

    返回：
        `[{character_id, character_name, phase, phase_label, materials, complete, ...}]`

    ⚠️ 库存是**共享池**：同一个 item_id 先给排在前面的角色扣，扣不完的才轮到下一个。
    """
    pool = mys_inventory.counts(inventory)
    planned = {}          # item_id -> 本轮已经"分配"出去的数量（**不写回库存**）
    results = []

    for entry in characters or ():
        target = entry.get("target") or {}
        # `entry["requirements"]` 是 `growth_planner.requirements_for()` 的完整返回，
        # 里面 `by_phase` 才是阶段 → 材料列表（写成整个 dict 会让缺口恒为 0）。
        by_phase = (entry.get("requirements") or {}).get("by_phase") or {}

        for phase in growth_models.ordered_phases(target):
            if phase == growth_models.PHASE_WEAPON_LEVEL and not target.get("weapon_enabled"):
                results.append(_phase_row(entry, phase, [], pool, planned, skipped=True))
                continue

            rows = _phase_requirements(entry, phase, by_phase, compute_lookup)
            results.append(_phase_row(entry, phase, rows, pool, planned))
    return results


def _phase_requirements(entry, phase, by_phase, compute_lookup):
    if by_phase:
        return list(by_phase.get(phase) or ())
    if compute_lookup:
        try:
            return list(compute_lookup(entry, phase) or ())
        except Exception as exc:                # noqa: BLE001 —— 现算失败不该中断整轮规划
            print(f"⚠️ 现算材料需求失败（{phase}）：{type(exc).__name__} {exc}")
    return []


def _phase_row(entry, phase, requirements, pool, planned, skipped=False):
    target = entry.get("target") or {}
    rows = []
    complete = True

    for requirement in requirements:
        item_id = int(requirement.get("item_id") or 0)
        required = int(requirement.get("required") or 0)
        if not item_id or required <= 0:
            continue

        item_name = str(requirement.get("item_name") or requirement.get("name") or "")
        # ★ 米游社自己给的缺口优先（`lack_num`：它按你账号的真实背包算过）。
        #   有它就**不要**再拿库存池去扣一次 —— 会重复扣。
        api_lack = requirement.get("lack")
        official_lack = requirement.get("lack_source") == "overall_consume"
        if official_lack or (api_lack is not None and requirement.get("owned") is not None):
            missing = max(0, int(api_lack))
            taken = max(0, required - missing)
        else:
            # 共享池：真实库存 - 已经分配给前面角色的部分（规划中的分配**不进库存**）
            owned_total = int(pool.get(item_id, 0))
            already_planned = int(planned.get(item_id, 0))
            available = max(0, owned_total - already_planned)
            taken = min(available, required)
            missing = required - taken
            planned[item_id] = already_planned + taken

        if missing > 0:
            complete = False
        rows.append({
            "item_id": item_id,
            "item_name": item_name,
            "category": refine_category(item_name, requirement.get("category")),
            "required": required,
            "owned": taken,
            "missing": missing,
            "lack_source": requirement.get("lack_source") or "estimated",
            # `owned_known=False` 时界面要写"未知"，不能写 0（否则看起来像"你一个都没有"）
            "owned_known": bool(requirement.get("owned") is not None or pool),
            "allocated": taken,
            "phases": list(requirement.get("phases") or (phase,)),
        })

    rows.sort(key=lambda row: (-row["missing"], row["item_id"]))
    return {
        "character_id": int(target.get("character_id") or 0),
        "character_name": str(target.get("character_name") or ""),
        "rarity": int(target.get("rarity") or 0),
        "phase": phase,
        "phase_label": growth_models.phase_label(phase),
        "enabled": bool(target.get("enabled", True)),
        "skipped": bool(skipped),
        "complete": bool(complete) and not skipped,
        "materials": rows,
        "missing_count": sum(1 for row in rows if row["missing"] > 0),
        "missing_total": sum(row["missing"] for row in rows),
    }


def phase_missing(rows):
    """把一串阶段结果合并成一张 `{item_id: 总量}` 的缺口表（给"全部还差多少"用）。"""
    totals = {}
    for row in rows or ():
        for material in row.get("materials") or ():
            if material["missing"] <= 0:
                continue
            key = int(material.get("item_id") or 0)
            entry = totals.setdefault(key, {
                "item_id": key,
                "item_name": material["item_name"],
                "category": material["category"],
                "missing": 0,
            })
            entry["missing"] += material["missing"]
    return sorted(totals.values(), key=lambda item: -item["missing"])


# ==========================================
# 🌟 材料 → 来源（§18 / §30 / §31）
# ==========================================


def resolve_source(material, weekday=None):
    """一种材料该去哪儿拿。返回 dict（认不出时 `type="waiting_route"`，绝不瞎猜）。"""
    name = str((material or {}).get("item_name") or (material or {}).get("name") or "").strip()
    category = str((material or {}).get("category") or "")
    base = {
        "material": name,
        "item_id": int((material or {}).get("item_id") or 0),
        "category": category,
        "type": TASK_WAITING_ROUTE,
        "label": TASK_TYPE_LABELS[TASK_WAITING_ROUTE],
        "route": "",
        "domain": "",
        "domain_index": "",
        "open_days": (),
        "reason": "",
    }
    if not name:
        base["reason"] = "材料没有名字，无法定位来源"
        return base

    # ① 摩拉 / 经验书 → 地脉花（唯一确定性来源）
    if category == growth_models.CATEGORY_CURRENCY or "摩拉" in name:
        base.update({
            "type": TASK_LEYLINE,
            "label": TASK_TYPE_LABELS[TASK_LEYLINE],
            "route": LEYLINE_MORA,
            "reason": "摩拉走地脉花「藏金之花」",
        })
        return base
    if category == growth_models.CATEGORY_EXP_BOOK or "经验" in name and category != growth_models.CATEGORY_WEAPON_EXP:
        base.update({
            "type": TASK_LEYLINE,
            "label": TASK_TYPE_LABELS[TASK_LEYLINE],
            "route": LEYLINE_EXP,
            "reason": "角色经验走地脉花「启示之花」",
        })
        return base

    # ② 天赋书 → 秘境：**先问 genshin-db**（直接数据、带权威开放星期），
    #    问不到再退回百科字典索引 + 同系列推算（见 `_schedule_source`）。
    #    顺序反过来的话，推算结果会先把位置占掉，明明有直接数据却报了"推算"。
    gdb_source = _genshin_db_domain(name)
    if gdb_source:
        base.update(gdb_source)
        return base

    # ②.5 天赋书 / 武器突破 / 角色突破 → 秘境（百科字典里有开放日程）
    schedule_source = _schedule_source(name)
    if schedule_source:
        base.update(schedule_source)
        base["type"] = TASK_DOMAIN
        base["label"] = TASK_TYPE_LABELS[TASK_DOMAIN]
        return base

    # ③ 采集类（特产 / 魔物 / 矿物 / 食材）→ 现有 gather_cooldown 的路线索引
    route_source = _route_source(name)
    if route_source:
        base.update(route_source)
        return base

    # ③.5 材料族（§6/§11）：材料自己没路线，但它的**族群**有 —— 这是最关键的一段
    family_source = _family_source(name)
    if family_source:
        base.update(family_source)
        return base

    # ③.6 首领材料（雷光棱镜 → 无相之雷）→ 一条龙的「批量讨伐角色养成材料BOSS」
    boss_source = _boss_source(name)
    if boss_source:
        base.update(boss_source)
        return base

    # ④ 还认不出来就交给共用索引按名称找一类（认出就归那一类，认不出就等路线）
    category_action = _category_action_for(name)
    if category_action:
        base.update({
            "type": category_action,
            "label": TASK_TYPE_LABELS.get(category_action, category_action),
            "route": name,
            "reason": f"按名称归类为「{TASK_TYPE_LABELS.get(category_action, category_action)}」",
        })
        return base

    # ④ 天赋书 / 限时道具：本地没有日程也要把**出处**说清楚（比"找不到获取方式"有用）
    talent_source = _talent_source(name)
    if talent_source:
        base.update(talent_source)
        return base

    base["reason"] = _family_reason(name) or "本地数据里找不到这个材料的获取方式（没有可执行路线）"
    return base


def _family_source(name):
    """材料 → 材料族 → 敌人族群 → BetterGI 路线池（架构文档 §6/§11 的核心）。

    **为什么必须有这一段**：米游社算出来的缺口是**具体材料**（异色结晶石），
    而 BetterGI 的路线是按**族群**组织的（原海异种）。本机路线索引里只有
    `原海异种`、没有 `异色结晶石`，所以走 §③ 永远匹配不上 —— 表现就是
    "同族的低档材料有来源、高档材料却写「没有可执行路线」"（被反馈过）。

    认不出族群、族群没有路线、或本机路线库已经没有这个族群目录时一律返回 None：
    宁可落到 `waiting_route`，也不伪造一条跑不了的路线（§33）。
    """
    try:
        family = material_family.resolve_material_family(name)
    except Exception as exc:                # noqa: BLE001 —— 知识表问题不该让规划崩
        print(f"⚠️ 材料族解析失败（{type(exc).__name__}: {exc}）：{name}")
        return None
    if not family or not family.is_enemy_drop or not family.route:
        return None
    try:
        if not material_family.route_available(family.route):
            return None
    except Exception:                       # noqa: BLE001
        return None
    pool = 0
    try:
        pool = material_family.route_pool_size(family)
    except Exception:                       # noqa: BLE001
        pool = family.route_files
    rank = family.rank_of(name)
    rank_text = f"第 {rank} 档" if rank else ""
    same = [item for item in family.material_names() if item != name]
    same_text = f"，同族还有 {'、'.join(same)}" if same else ""
    return {
        "type": TASK_HUNT,
        "label": TASK_TYPE_LABELS[TASK_HUNT],
        "route": family.route,
        "family": family.name,
        "family_materials": family.material_names(),
        "reason": (f"「{name}」由「{family.name}」掉落{rank_text}{same_text}；"
                   f"按族群跑 BetterGI「{family.route}」（本机 {pool} 条路线）"),
    }


def _boss_source(name):
    """首领材料 → `TASK_BOSS`（走一条龙的「批量讨伐角色养成材料BOSS」）。

    首领**不是**魔物族群（`无相之雷`、`急冻树`…在本机「敌人与魔物」目录里没有路线），
    所以不能靠 §③.5。但项目里已经有 `run_boss` 通路和 `boss_pathing_guard` 校验，
    这里只把材料翻译成首领名，**名字是否可执行交给现成校验器判断** ——
    校验不过（脚本标注不支持、名字对不齐）就返回 None，让它继续落到
    `waiting_route`，界面上理由是"这是首领材料"，而不是含糊的"找不到获取方式"。
    """
    try:
        family = material_family.resolve_material_family(name)
    except Exception:                       # noqa: BLE001
        return None
    if not family or family.type != "world_boss":
        return None
    boss = family.name
    if not _boss_target_ok(boss):
        return None
    return {
        "type": TASK_BOSS,
        "label": TASK_TYPE_LABELS[TASK_BOSS],
        "route": boss,
        "family": family.name,
        "reason": f"「{name}」是世界首领「{boss}」的掉落，走「批量讨伐角色养成材料BOSS」",
    }


_BOSS_TARGET_CACHE = {}


def _boss_target_ok(boss):
    """这个首领名 BetterGI 现在能不能跑（结果按名字缓存；问不到就判"不能"，保持诚实）。"""
    key = str(boss or "").strip()
    if not key:
        return False
    cached = _BOSS_TARGET_CACHE.get(key)
    if cached is not None:
        return cached
    ok = False
    try:
        from skills import boss_pathing_guard
        canonical, _notices = boss_pathing_guard.validate_boss_target(key)
        ok = bool(canonical)
    except Exception:                       # noqa: BLE001 —— 校验不过就是不跑，不需要报错刷屏
        ok = False
    _BOSS_TARGET_CACHE[key] = ok
    return ok


def _family_reason(name):
    """认出了族群但没路线时，给一句**有用**的理由（别写"找不到获取方式"就完事）。"""
    try:
        family = material_family.resolve_material_family(name)
    except Exception:                       # noqa: BLE001
        return ""
    if not family:
        return ""
    if family.type == "weekly_boss":
        return (f"「{name}」是周本首领「{family.name}」的奖励，"
                f"每周挑战一次，本地没有可自动执行的路线")
    if family.type == "world_boss":
        return (f"「{name}」是世界首领「{family.name}」掉落，"
                f"但本机 BetterGI 脚本里没有这个首领（可能标了不支持或未订阅）")
    if family.is_enemy_drop and not family.route:
        return f"「{name}」由「{family.name}」掉落，但本机 BetterGI 路线库没有这个族群目录"
    return ""


def _schedule_source(name):
    """用百科字典的开放日程反查秘境与开放星期。"""
    try:
        material_schedule, _weapons, _known = domain_match._load_index()
    except Exception as exc:            # noqa: BLE001 —— 字典缺失不该让规划崩
        print(f"⚠️ 读取秘境日程失败（只影响秘境类材料）：{type(exc).__name__} {exc}")
        return None
    schedule = material_schedule.get(name)
    inferred = False
    if not schedule:
        # 百科字典的秘境索引里**只有「指引」和「哲学」，21 个天赋书系列的「教导」全缺**
        # （实测，见 `_sibling_schedule`），于是"缺教导"会报"找不到获取方式" ——
        # 而教导恰恰是每个天赋 1→2 级都要的材料，几乎人人都有。
        schedule = _sibling_schedule(name, material_schedule)
        inferred = bool(schedule)
    if not schedule:
        return None
    result = domain_match._domain_from_schedule(schedule, name, f"材料「{name}」的开放日程")
    if not result:
        return None
    days_text = result.open_days or "每天"
    tail = "；开放日程按同系列的高档天赋书推算" if inferred else ""
    return {
        "route": result.domain,
        "domain": result.domain,
        "domain_index": result.domain_index or "",
        "open_days": tuple(result.days or ()),
        "reason": f"「{name}」在秘境「{result.domain}」产出（{days_text}）{tail}",
    }


# 天赋书的三个档位；同一系列的三档出自**同一个秘境**，开放星期也完全相同。
TALENT_TIER_SUFFIXES = ("的教导", "的指引", "的哲学")


def _sibling_schedule(name, material_schedule):
    """拿同系列另一个档位的日程来补（「正义」的教导 → 用「正义」的指引的日程）。

    为什么可靠：同一系列的天赋书（教导/指引/哲学）在游戏里就是同一个秘境、
    同样的开放星期，实测 21 个系列全部一致。而字典里教导档整个缺失 ——
    与其让玩家看到"找不到获取方式"，不如用兄弟档的日程去问同一个秘境。
    """
    text = str(name or "")
    base = ""
    for suffix in TALENT_TIER_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            base = text[: -len(suffix)]
            break
    if not base:
        return ""
    for suffix in TALENT_TIER_SUFFIXES:
        sibling = base + suffix
        if sibling == text:
            continue
        schedule = material_schedule.get(sibling)
        if schedule:
            return schedule
    return ""


def _route_source(name):
    """用 `gather_cooldown` 的路线索引判断这个材料属于哪一类采集资源。

    ⚠️ 必须先确认"路线索引里真的有这个材料"：`category_of()` 会做模糊匹配，
    一个根本不存在的名字也可能被归到某一类（实测 "根本不存在的材料" → 特产）。
    本函数存在的前提是**知道路线，不猜路线**（规格书 §33），所以这里要显式核对。
    """
    if not _has_route(name):
        return None
    try:
        category = gather_cooldown.category_of(name)
    except Exception:                   # noqa: BLE001
        category = None
    action = {
        gather_cooldown.CATEGORY_SPECIALTY: TASK_GATHER,
        gather_cooldown.CATEGORY_MINE: TASK_MINE,
        gather_cooldown.CATEGORY_COOK: TASK_COOK,
        gather_cooldown.CATEGORY_HUNT: TASK_HUNT,
    }.get(category)
    if not action:
        return None
    return {
        "type": action,
        "label": TASK_TYPE_LABELS[action],
        "route": name,
        "reason": f"「{name}」在 BetterGI 有{ TASK_TYPE_LABELS[action] }路线",
    }


def _has_route(name):
    """BetterGI 的路线索引里到底有没有这个材料名。

    判据是 `gather_cooldown.material_category_index()`（**材料名 → 类别**，由它出现在哪个
    脚本组目录下决定）—— 不是 `route_material_index()`（那是"路线文件名 → 材料名"，
    键是文件名，拿材料名去查永远查不到）。

    索引整个是空的时候（BetterGI 还没同步脚本仓库 / 目录被清过）返回 True：
    那种情况下"查不到"说明不了任何事，一律判"没路线"会让所有采集任务变成 WAITING_ROUTE。
    """
    wanted = str(name or "").strip()
    if not wanted:
        return False
    try:
        index = gather_cooldown.material_category_index()
    except Exception as exc:            # noqa: BLE001 —— 路线目录缺失时不该让规划崩
        print(f"⚠️ 读取 BetterGI 路线索引失败（{type(exc).__name__}）：{exc}")
        return False
    if not index:
        return True
    return wanted in index


def _category_action_for(name):
    """按目标名字在各类路线里找一找（`route_group.category_for_target`）。

    同样要求"路线索引里真的有它"，否则返回 None —— 让调用方落到 `waiting_route`，
    而不是伪造一条不存在的路线（§33：不要让 LLM/代码猜路线）。
    """
    if not _has_route(name):
        return None
    try:
        category = route_group.category_for_target(name)
    except Exception:                   # noqa: BLE001
        return None
    action = {
        "gather": TASK_GATHER,
        "mine": TASK_MINE,
        "cook": TASK_COOK,
        "hunt": TASK_HUNT,
        "hoe": TASK_SCRIPT,
    }.get(category)
    return action


def _talent_source(name):
    """天赋书 / 限时道具：本地查不到日程时，至少把**出处**告诉玩家。

    为什么需要：百科字典的秘境索引只覆盖到「指引 / 哲学」两档，21 个系列的**「教导」全缺**
    （兄弟档能补的已经用 `_sibling_schedule` 补了）；而像「坚忍」（精通秘境：隐修）、
    「慈爱」（精通秘境：默想）这种较新的系列，本地**整个没有** —— `domain_match` 也解析不出来
    （实测抛 `ConfigTransactionError`）。可是 `mapping.json` 里明明写着它出自哪个秘境。
    所以理由写成"出自精通秘境「隐修」，但本地没有它的开放日程"，而不是含糊的
    "本地数据里找不到这个材料的获取方式"。

    ⚠️ **不猜开放日程**（§30）：没有日程就不放行 —— 按"每天开放"放行会让任务显示"可执行"，
    玩家白跑一趟才知道今天不开。所以这里返回的是 `TASK_WAITING_ROUTE`，只是理由更具体。
    """
    try:
        slot = material_family.knowledge_slot(name)
    except Exception:                       # noqa: BLE001
        return None
    if not slot:
        return None
    text = str(slot.get("source_text") or "").strip()
    if not text:
        return None
    domain = ""
    hit = re.search(r"秘境\s*[:：]\s*([^\s（()）]+)", text)
    if hit:
        domain = hit.group(1)
    if domain:
        return {
            "type": TASK_WAITING_ROUTE,
            "route": "",
            "domain": domain,
            "reason": (f"「{name}」出自精通秘境「{domain}」，"
                       f"但本地没有这个秘境的开放日程（BetterGI 的秘境名单里也确认不了它），"
                       f"需要你自己安排"),
        }
    if "活动" in text:
        return {
            "type": TASK_WAITING_ROUTE,
            "route": "",
            "reason": f"「{name}」是限时活动奖励，没有可刷的路线（每次活动自己留意）",
        }
    return None


def _genshin_db_domain(name):
    """天赋书的秘境来源（genshin-db 的 `domains.daysOfWeek`，含**开放星期**）。

    为什么要第二条路：百科字典的秘境索引里 21 个系列的「教导」档**全缺**
    （`_sibling_schedule` 能补一部分），较新的系列整个没有。genshin-db 的秘境数据
    带 `daysOfWeek`，实测与项目现有索引一致（箴铭 = 周二/五/日），所以这里能直接
    给出秘境**和**开放日，而不是靠兄弟档推算。

    `route` / `domain` 用**入口名**（苍白的遗荣），与现有索引的口径一致
    —— BetterGI 的秘境名单认的是入口名，不是「精通秘境：箴铭 IV」这种关卡名。
    """
    try:
        slot = material_family.talent_domain(name)
    except Exception as exc:                # noqa: BLE001
        print(f"⚠️ 读天赋书日程失败（{type(exc).__name__}: {exc}）：{name}")
        return None
    if not slot:
        return None
    entrance = slot.get("entrance") or ""
    if not entrance:
        return None
    days = tuple(slot.get("days") or ())
    try:
        domain_index = domain_match.domain_index_from_days(days) or ""
    except Exception:                       # noqa: BLE001
        domain_index = ""
    days_text = "周" + "/".join(days) if days else "每天"
    stage_text = f"「{slot['stage']}」" if slot.get("stage") else ""
    return {
        "type": TASK_DOMAIN,
        "label": TASK_TYPE_LABELS[TASK_DOMAIN],
        "route": entrance,
        "domain": entrance,
        "domain_index": str(domain_index or ""),
        "open_days": days,
        "reason": (f"「{name}」出自精通秘境{stage_text}（入口「{entrance}」），"
                   f"开放日 {days_text}；日程来自 genshin-db"),
    }


# ==========================================
# 🌟 分类修正：BetterGI 路线里真的有这种材料，就按那一类算
# ==========================================


def refine_category(item_name, category=None):
    """用 BetterGI 的路线索引修正材料分类。

    **为什么需要**：光看名字 / id 分不出「清心」是特产还是 Boss 材料（名字带"心"、
    特产 id 段又和别的东西混在一起），但 BetterGI 的路线目录是**事实**：
    `地方特产/璃月/清心` 一定说明它是特产。有了它，分类与"能不能跑"就一致了。
    """
    name = str(item_name or "").strip()
    if not name:
        return category or growth_models.CATEGORY_OTHER
    try:
        route_category = gather_cooldown.category_of(name)
    except Exception:                   # noqa: BLE001
        route_category = None
    return {
        gather_cooldown.CATEGORY_SPECIALTY: growth_models.CATEGORY_SPECIALTY,
        gather_cooldown.CATEGORY_MINE: growth_models.CATEGORY_OTHER,
        gather_cooldown.CATEGORY_COOK: growth_models.CATEGORY_FOOD,
        gather_cooldown.CATEGORY_HUNT: growth_models.CATEGORY_MONSTER_DROP,
    }.get(route_category) or category or growth_models.CATEGORY_OTHER


# ==========================================
# 🌟 今天能不能拿（§30 / §27 / §33）
# ==========================================


def available_today(source, weekday=None, cooldown=None):
    """`(status, note)` —— 今天这个来源能不能跑。

    · 秘境：按开放日程判断星期（周二/五开放的秘境周三就是拿不到）；
    · 采集类：看 `gather_cooldown` 的冷却，但**只标注不阻断**（§27 明确说冷却复用现成实现）；
    · 认不出路线：`waiting_route`。
    """
    source = source or {}
    kind = source.get("type")

    if kind == TASK_DOMAIN:
        days = tuple(source.get("open_days") or ())
        if not days:
            return STATUS_RUNNABLE, ""
        today = weekday or domain_match.business_weekday()
        if today in days:
            return STATUS_RUNNABLE, ""
        return (
            STATUS_CLOSED_TODAY,
            f"该秘境今天（周{today}）不开放，开放日是 周{'/'.join(days)}；"
            f"今天改跑别的材料，等开放日再刷（规格书 §30）",
        )

    if kind in (TASK_GATHER, TASK_HUNT, TASK_MINE, TASK_COOK):
        state = cooldown if cooldown is not None else _cooldown_status(source.get("route"))
        if state and state.get("cooling"):
            remaining = state.get("hours_left_text") or f"{state.get('hours_left', 0):.1f} 小时"
            return (
                STATUS_COOLDOWN,
                f"「{source.get('material')}」的路线还在冷却，约 {remaining} 后刷新；本轮先跳过",
            )
        return STATUS_RUNNABLE, ""

    if kind == TASK_WAITING_ROUTE:
        return STATUS_WAITING_ROUTE, source.get("reason") or "没有可执行的 BetterGI 路线"

    return STATUS_RUNNABLE, ""


def _cooldown_status(material):
    """查一种材料的冷却状态；查不动就返回 None（当成"可跑"）。"""
    name = str(material or "").strip()
    if not name:
        return None
    try:
        return gather_cooldown.status(name)
    except Exception:                   # noqa: BLE001 —— 日志读不到不该挡住规划
        return None


# ==========================================
# 🌟 生成任务
# ==========================================


def build_tasks(phase_rows, planned=None, weekday=None, include_completed=False,
                cooldown_lookup=None, resin_state=None):
    """把阶段缺口转成"可执行的 BetterGI 任务"列表。

    普通材料仍是一种材料一个任务；秘境材料会按"同角色 + 同阶段 + 同秘境"
    合并成一条任务（不同品质的天赋书 / 武器材料本来就是同一个副本掉）。
    任务里带上 `status` / `status_note`，
    **不在这里决定跑不跑** —— 那是 `growth_planner` 的事。

    `resin_state` 是 `skills.mys_resin` 的返回值：给了就**按真实体力算趟数**，
    读不到就退化成默认趟数（`count_note` 里会写明是默认值）。
    """
    planned = planned if planned is not None else {}
    merged = {}
    for row in phase_rows or ():
        if row.get("skipped"):
            continue
        for material in row.get("materials") or ():
            # 已齐材料也保留在详情表中：米游社网页会显示“消耗 N / 所需 0”。
            # include_completed 仍保留给旧调用方，但默认详情需要完整材料清单。
            if material["missing"] <= 0 and not include_completed:
                continue
            source = resolve_source(material, weekday=weekday)
            key = _task_key(row, material, source)
            existing = merged.get(key)
            if existing is not None:
                # 同一种材料在多个阶段都缺（摩拉几乎一定如此）：只出一条任务 ——
                # 库存是共享池，跑一趟就同时补上那些阶段，出两条会让玩家跑两遍。
                #
                # ⚠️ 但数字必须**累加**（`required` / `missing` / `owned` 一起加）：
                #    等级要 648,000 摩拉、天赋要 3,180,000 摩拉，是**两笔**花费。
                #    以前只把 `missing` 取最大值、`required` 留在第一阶段，
                #    结果界面上出现"需要 647,990 / 缺 3,182,500"这种自相矛盾的组合 ——
                #    玩家看半天也分不清哪个是哪个（被反馈过）。
                if not any(item.get("phase") == row["phase"] for item in existing["phases"]):
                    existing["phases"].append({"phase": row["phase"],
                                               "phase_label": row["phase_label"],
                                               "missing": material["missing"]})
                existing["required"] += int(material["required"] or 0)
                existing["missing"] += int(material["missing"] or 0)
                existing["owned"] += int(material.get("owned") or 0)
                existing["owned_known"] = bool(existing.get("owned_known")
                                               or material.get("owned_known"))
                existing["allocated"] += int(material["missing"] or 0)
                existing.setdefault("materials", []).append(_task_material(material, source))
                existing["material"] = _task_material_label(existing["materials"])
                existing["task_key"] = _task_key_text(key)
                _apply_runs(existing)          # 总量变了，趟数要跟着重算
                continue

            if int(material["missing"] or 0) <= 0:
                status, note = STATUS_COMPLETE, "材料已满足，无需补刷"
            else:
                status, note = available_today(source, weekday=weekday,
                                               cooldown=cooldown_lookup(source) if cooldown_lookup else None)
            task = {
                "task_type": source["type"],
                "task_label": source["label"],
                "phase": row["phase"],
                "phase_label": row["phase_label"],
                "phases": [{"phase": row["phase"], "phase_label": row["phase_label"],
                            "missing": material["missing"]}],
                "character_id": row["character_id"],
                "character_name": row["character_name"],
                "material": source["material"],
                "item_id": source["item_id"],
                "task_key": _task_key_text(key),
                "category": material["category"],
                "required": int(material["required"] or 0),
                # `owned` = 这个材料你已经有几个（来自米游社的 `lack_num`，或者库存池）。
                # `owned_known=False` 时界面要写"未知"而不是 0。
                "owned": int(material.get("owned") or 0),
                "owned_known": bool(material.get("owned_known")),
                "missing": int(material["missing"] or 0),
                "allocated": planned.get(source["item_id"], material["missing"]),
                "route": source["route"],
                "domain": source.get("domain", ""),
                "domain_index": source.get("domain_index", ""),
                # 材料族（§6）：同族的三档材料（异海凝珠/异海之块/异色结晶石）是**同一条路线**
                # 掉的，界面上要把这件事说出来，下发指令时也只该跑一条路线。
                "family": source.get("family", ""),
                "family_materials": list(source.get("family_materials") or ()),
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "status_note": note,
                "reason": source["reason"],
                "materials": [_task_material(material, source)],
                "count": 0,
                "resin": 0,
            }
            _apply_runs(task)
            merged[key] = task

    tasks = list(merged.values())

    # 按**玩家给定的刷取优先级**排：① 等级突破首领 → ② 采集 / 普通魔物掉落（不耗体力）
    # → ③ 经验地脉「启示之花」→ ④ 武器突破副本 → ⑤ 天赋副本 → ⑥ 摩拉地脉。
    # 同优先级里再按"能不能跑 / 阶段 / 缺口大小"排。
    order = {STATUS_RUNNABLE: 0, STATUS_COOLDOWN: 1, STATUS_CLOSED_TODAY: 2, STATUS_WAITING_ROUTE: 3}
    tasks.sort(key=lambda item: (order.get(item["status"], 9), task_priority(item),
                                 item["phase"], -item["missing"], item["item_id"]))

    # 按当前体力把这一轮的趟数分下去（读不到体力时 `available=False`，保持默认趟数）
    allocation = allocate_resin(tasks, resin_state=resin_state)
    for task in tasks:
        budget = allocation["by_task"].get(_allocation_key(task)) if allocation.get("available") else None
        _apply_runs(task, resin_state=resin_state, budget=budget)
        task["priority"] = task_priority(task)
        task["priority_label"] = priority_label(task)
        # 这一轮排 0 趟（体力不够分到它）→ 标记出来，下发指令时跳过，但明细表照常显示
        task["skipped_this_round"] = bool(
            allocation.get("available")
            and task["task_type"] in (TASK_DOMAIN, TASK_LEYLINE, TASK_BOSS)
            and int(task.get("count") or 0) <= 0
            and int(task.get("missing") or 0) > 0
        )
        if task["skipped_this_round"]:
            note = task.get("count_note") or ""
            current_now = int((resin_state or {}).get("current") or 0)
            each = resin_each(task["task_type"])
            if current_now < each:
                # 最容易让人困惑的一种情况：体力够一次动作，但**凑不出你设的那一档**
                note += (f"；当前只有 {current_now} 点，而你的树脂策略一次要 {each} 点"
                         f"（改设置里的「树脂策略」或等体力回涨）")
            else:
                note += "；本轮体力不够，先排在后面（下一轮体力回来了再刷）"
            task["count_note"] = note
    return tasks


def _task_key(row, material, source):
    """Merge domain tiers from the same route into one resin task."""
    if source.get("type") == TASK_DOMAIN:
        return (
            TASK_DOMAIN,
            int(row.get("character_id") or 0),
            str(row.get("phase") or ""),
            str(source.get("domain") or source.get("route") or ""),
        )
    # 非秘境材料恢复为“一个材料一条任务”：
    # 敌人掉落 / 特产 / 矿物等没有副本品质期望，不能因为共享族群路线
    # 被拼成“某材料 等 2 种”。同名材料跨阶段仍要共享库存、合并缺口，
    # 所以这里按材料名隔离，不把角色和阶段放进 key。
    return (
        "material",
        str(material.get("item_name") or material.get("name") or "").strip(),
    )


def _task_key_text(key):
    if isinstance(key, tuple):
        return ":".join(str(part) for part in key)
    return str(key)


def _task_material(material, source):
    return {
        "item_id": int(material.get("item_id") or source.get("item_id") or 0),
        "item_name": str(material.get("item_name") or source.get("material") or ""),
        "missing": int(material.get("missing") or 0),
        "required": int(material.get("required") or 0),
        "owned": int(material.get("owned") or 0),
    }


def _task_material_label(materials):
    rows = [row for row in materials or () if row.get("item_name")]
    if not rows:
        return ""
    if len(rows) == 1:
        return rows[0]["item_name"]
    return f"{rows[0]['item_name']} 等 {len(rows)} 种"


def resin_each(kind):
    """一次动作要多少体力 —— **跟着玩家的树脂策略走**（`DOMAIN_RESIN_PREFERENCE`）。

    为什么不能写死 20：BetterGI 的 `autoDomainConfig` 里有「原粹树脂20 / 原粹树脂40 /
    浓缩树脂」三种策略，选 40 或浓缩时**一次就是 40 体力**。写死 20 会让程序说
    "够 1 趟"、而 BetterGI 那边因为凑不出 40 点一次都跑不了 —— 两边对不上（实测踩过：
    玩家策略是 40、身上 27 点，程序却排了 1 趟）。
    """
    if kind == TASK_BOSS:
        return RESIN_PER_BOSS
    # ⚠️ 这里**不能**吞异常：以前写成 try/except + 默认 20，而 `config` 当时根本没导入
    #    （NameError）→ 一路静默退回 20，玩家的「原粹树脂40」策略完全没生效，
    #    程序说"够 1 趟"、BetterGI 那边凑不出 40 点一次都跑不了（实际踩过）。
    preference = str(getattr(config, "DOMAIN_RESIN_PREFERENCE", "") or "").strip()
    if "40" in preference or "浓缩" in preference:
        return 40
    return RESIN_PER_DOMAIN


def _apply_runs(task, resin_state=None, budget=None):
    """给任务算"这一轮跑几趟"和体力估算。

    **趟数优先由真实体力决定**（玩家要求）：体力剩 20 就只跑 1 趟地脉，不再硬编码 6 趟。
    体力读不到时退化成原来的默认趟数，并在 `count_note` 里**写明"按默认排的"**，
    让玩家知道自己看到的是估算而不是事实。

    `budget` 是体例分配器给这个任务的体力额度（`allocate_resin()` 算出来的）；
    给了就以它为准 —— 它是"按优先级分完一轮"之后的结果。
    """
    kind = task["task_type"]
    # 已齐任务不参与本轮路线安排。这里必须在所有类型分支之前拦截：
    # 体力可用时，后面的“当前体力够几趟”只是能力上限，不能变成实际排程。
    if int(task.get("missing") or 0) <= 0:
        task["count"] = 0
        task["resin"] = 0
        task["count_note"] = "缺口为 0，本轮不安排路线"
        return
    current = int((resin_state or {}).get("current") or 0) if (resin_state or {}).get("available") else None
    if kind == TASK_DOMAIN:
        need = _runs_needed(task)
        each = resin_each(kind)
        if budget is not None:
            task["count"] = max(0, int(budget) // each)
            task["resin"] = task["count"] * each
            task["count_note"] = (
                f"按当前体力排 {task['count']} 趟（一次 {each} 体力）"
                f"；跑完重新同步库存再看还缺多少"
            )
        elif current is not None:
            task["count"] = min(need, current // each) if need else current // each
            task["resin"] = task["count"] * each
            task["count_note"] = (
                f"当前体力 {current}，够 {current // each} 趟（一次 {each} 体力）"
                f"，按缺口需要 {need} 趟 → 本轮排 {task['count']} 趟"
            )
        else:
            task["count"] = max(1, need or DOMAIN_RUNS_PER_ROUND)
            task["resin"] = task["count"] * each
            task["count_note"] = (
                f"⚠️ 读不到体力，按缺口期望排 {task['count']} 趟"
                f"（按材料品质折成等效绿色后估算）；跑完重新同步库存再看还缺多少"
            )
    elif kind == TASK_LEYLINE:
        need = _runs_needed(task)
        each = resin_each(kind)
        if budget is not None:
            task["count"] = max(0, int(budget) // each)
            task["resin"] = task["count"] * each
            task["count_note"] = f"按当前体力排 {task['count']} 趟（一次 {each} 体力）"
        elif current is not None:
            task["count"] = min(need, current // each) if need else current // each
            task["resin"] = task["count"] * each
            task["count_note"] = (
                f"当前体力 {current}，够 {current // each} 趟（一次 {each} 体力）"
                f"，按缺口需要 {need} 趟 → 本轮排 {task['count']} 趟"
            )
        else:
            task["count"] = LEYLINE_RUNS_PER_ROUND
            task["resin"] = LEYLINE_RUNS_PER_ROUND * each
            per = MORA_PER_LEYLINE if task["route"] == LEYLINE_MORA else EXP_BOOKS_PER_LEYLINE
            unit = "摩拉" if task["route"] == LEYLINE_MORA else "本经验书"
            task["count_note"] = (f"⚠️ 读不到体力，按默认 {LEYLINE_RUNS_PER_ROUND} 趟排"
                                  f"（一次地脉花约 {per:,} {unit}）")
    elif kind == TASK_BOSS:
        need = _runs_needed(task)
        affordable = None
        if budget is not None:
            affordable = max(0, int(budget) // RESIN_PER_BOSS)
        elif current is not None:
            affordable = current // RESIN_PER_BOSS
        if affordable is None:
            task["count"] = max(1, need or 1)
            task["count_note"] = "⚠️ 读不到体力，按缺口排 Boss 次数"
        else:
            task["count"] = max(0, min(need or affordable, affordable))
            task["count_note"] = (f"首领一次 {RESIN_PER_BOSS} 体力，按缺口与当前体力排 {task['count']} 次")
        task["resin"] = task["count"] * RESIN_PER_BOSS
    elif kind in (TASK_GATHER, TASK_HUNT, TASK_MINE, TASK_COOK, TASK_SCRIPT):
        task["count"] = 1
        task["resin"] = 0                      # 采集 / 魔物路线**不耗体力**
        task["count_note"] = "路线跑一遍（不耗体力），实际获得量以米游社同步为准"
    else:
        task["count"] = 0
        task["resin"] = 0
        task["count_note"] = ""


def _runs_needed(task):
    """按缺口算"还得跑几趟"（用体力产出比例倒推；算不出给 0）。"""
    try:
        from brain import resin_math
    except Exception:                       # noqa: BLE001
        return 0
    kind = task.get("task_type")
    missing = int(task.get("missing") or 0)
    if missing <= 0:
        return 0
    if kind == TASK_BOSS:
        per_run = RESIN_PER_BOSS * resin_math.RATE_WORLD_BOSS
    elif kind == TASK_DOMAIN:
        rate = (resin_math.RATE_WEAPON_DOMAIN if task.get("phase") == "weapon_level"
                else resin_math.RATE_TALENT_DOMAIN)
        per_run = resin_each(kind) * rate
    elif kind == TASK_LEYLINE:
        if task.get("route") == LEYLINE_MORA:
            per_run = resin_each(kind) * resin_math.RATE_MORA_LEYLINE
        else:
            per_run = resin_each(kind) * resin_math.RATE_EXP_LEYLINE
    else:
        return 0
    if per_run <= 0:
        return 0
    slot = {}
    try:
        slot = material_family.knowledge_slot(task.get("material")) or {}
    except Exception:                       # noqa: BLE001
        slot = {}
    if kind == TASK_DOMAIN:
        # 同一个秘境一次会同时产出整条材料线，合并后的任务要按全组缺口折算。
        materials = task.get("materials") or []
        if materials:
            units = sum(
                resin_math.green_equivalent(
                    row.get("item_name") or task.get("material"),
                    int(row.get("missing") or 0),
                )
                for row in materials
            )
        else:
            units = resin_math.green_equivalent(task.get("material"), missing)
    elif kind == TASK_LEYLINE and task.get("route") != LEYLINE_MORA:
        units = resin_math.exp_book_equivalent(task.get("material"), missing)
    else:
        units = missing
    return max(1, int(units / per_run + 0.9999))


# ==========================================
# 🌟 刷取优先级（玩家给定，见需求）
# ==========================================

# 玩家原话的刷取顺序：
#   ① 等级突破 boss ② 等级突破采集物 / 普通魔物掉落物（含天赋和武器的）
#   ③ 经验书地脉花「启示之花」 ④ 角色武器的突破副本 ⑤ 天赋副本 ⑥ 摩拉
# 数字越小越先刷。采集 / 魔物掉落**不耗体力**，所以它们可以和后面对地并行，
# 只是排位靠前（先把不花体力的顺手做掉）。
PRIORITY_BOSS = 10
PRIORITY_FREE = 20          # 采集 / 普通魔物掉落（不耗体力）
PRIORITY_EXP_LEYLINE = 30
PRIORITY_WEAPON_DOMAIN = 40
PRIORITY_TALENT_DOMAIN = 50
PRIORITY_MORA_LEYLINE = 60

PRIORITY_LABELS = {
    PRIORITY_BOSS: "① 等级突破首领",
    PRIORITY_FREE: "② 采集 / 普通魔物掉落（不耗体力）",
    PRIORITY_EXP_LEYLINE: "③ 经验地脉「启示之花」",
    PRIORITY_WEAPON_DOMAIN: "④ 武器突破副本",
    PRIORITY_TALENT_DOMAIN: "⑤ 天赋副本",
    PRIORITY_MORA_LEYLINE: "⑥ 摩拉地脉「藏金之花」",
}


def task_priority(task):
    """这个任务排第几（数字越小越先）。认不出排最后。"""
    kind = (task or {}).get("task_type")
    if kind == TASK_BOSS:
        return PRIORITY_BOSS
    if kind in (TASK_GATHER, TASK_HUNT, TASK_MINE, TASK_COOK, TASK_SCRIPT):
        return PRIORITY_FREE
    if kind == TASK_LEYLINE:
        return PRIORITY_MORA_LEYLINE if task.get("route") == LEYLINE_MORA else PRIORITY_EXP_LEYLINE
    if kind == TASK_DOMAIN:
        return PRIORITY_WEAPON_DOMAIN if task.get("phase") == "weapon_level" else PRIORITY_TALENT_DOMAIN
    return 90


def priority_label(task):
    return PRIORITY_LABELS.get(task_priority(task), "⑦ 其它")


def allocate_resin(tasks, resin_state=None, resin_total=None):
    """按优先级把**当前体力**分给这一轮的任务（玩家要求"按当前体力换算次数"）。

    · 只有秘境 / 地脉 / 首领吃体力；采集 / 魔物掉落不参与分配。
    · 每个任务先算"按缺口需要几趟"，再取"体力够几趟"，取小值。
    · 按优先级从上往下分，分完为止 —— 所以**前面的优先刷满**，后面的可能这轮排 0 趟
      （排 0 趟的任务不会下发指令，等下一轮体力回来了再排）。

    返回 `{"total", "available", "allocated", "left", "by_task": {task_key: resin}}`。
    """
    state = resin_state or {}
    available = bool(state.get("available"))
    total = int(resin_total if resin_total is not None else (state.get("current") or 0)) if available else None
    allocation = {}
    if total is None:
        # 读不到体力：不算预算，让 `_apply_runs` 走"默认趟数"那条路（并在说明里标注）
        return {"total": None, "available": False, "allocated": 0, "left": 0,
                "reason": state.get("reason") or "读不到体力", "by_task": allocation}

    left = max(0, int(total))
    for task in sorted(tasks or (), key=task_priority):
        # ⚠️ 只给**今天真的能跑**的任务分体力：秘境今天不开（closed_today）/ 路线冷却中
        #    的任务分了也白分 —— 它还占掉额度，后面的任务就排不上了（踩过：20 点体力被
        #    分给一个没开放的秘境，真正能跑的摩拉地脉反倒排 0 趟）。
        if task.get("status") != STATUS_RUNNABLE:
            continue
        kind = task.get("task_type")
        each = resin_each(kind)
        if not each or kind in (TASK_GATHER, TASK_HUNT, TASK_MINE, TASK_COOK, TASK_SCRIPT):
            continue                                # 不耗体力
        if int(task.get("missing") or 0) <= 0:
            continue
        need_runs = max(1, _runs_needed(task))
        runs = min(need_runs, left // each)
        allocation[_allocation_key(task)] = runs * each
        left -= runs * each
    return {"total": int(total), "available": True,
            "allocated": int(total) - left, "left": left, "reason": "", "by_task": allocation}


def _allocation_key(task):
    key = (task or {}).get("task_key")
    if key:
        return str(key)
    return (task or {}).get("item_id")


# ==========================================
# 🌟 转成 BetterGI 能吃的指令（skills/bgi_controller.execute_bgi_task 的格式）
# ==========================================


def tasks_to_bgi_command(tasks, free_action_map=None):
    """把任务列表转成 `{"energy_task": {...}, "free_task": [...]}`。

    形状必须和 `bgi_controller._execute_bgi_task_locked` 认的一致：
      · `energy_task.action` ∈ {run_domain, run_leyline, run_boss}
        （这三个是**互斥**的独占任务：秘境 / 地脉花 / Boss 讨伐，一轮只能选一个）；
      · `free_task` 是采集/讨伐/挖矿/食材/整脚本这类可以叠加的自由任务。
    """
    free_action_map = free_action_map or {
        TASK_GATHER: "gather",
        TASK_HUNT: "hunt",
        TASK_MINE: "mine",
        TASK_COOK: "cook",
        TASK_SCRIPT: "script",
    }
    runnable = [task for task in tasks or ()
                if task.get("status") == STATUS_RUNNABLE
                and int(task.get("missing") or 0) > 0
                # 体力不够、这轮排 0 趟的任务不下发（明细表照常显示）
                and not task.get("skipped_this_round")]

    # ★ 独占的体力任务（秘境 / 地脉 / 首领一轮只能选一个）按**玩家的刷取优先级**挑，
    #   不再按列表顺序"谁先出现谁上" —— 列表顺序是排序结果，但排序键里还混着
    #   阶段与缺口大小，靠它挑会挑错（比如先挑到天赋本，而不是更优先的突破首领）。
    energy_candidates = [task for task in runnable
                         if task["task_type"] in (TASK_DOMAIN, TASK_LEYLINE, TASK_BOSS)
                         and int(task.get("count") or 0) > 0]
    energy_candidates.sort(key=lambda item: (task_priority(item), item["phase"], -item["missing"]))

    energy_task = {}
    free_tasks = []
    if energy_candidates:
        top = energy_candidates[0]
        kind = top["task_type"]
        if kind == TASK_DOMAIN:
            energy_task = {
                "action": "run_domain",
                "target": top["domain"] or top["route"] or top["material"],
                "count": top["count"],
            }
            if top.get("domain_index"):
                energy_task["domain_index"] = str(top["domain_index"])
        elif kind == TASK_LEYLINE:
            energy_task = {
                "action": "run_leyline",
                "target": top["route"],
                "count": top["count"],
            }
        else:
            energy_task = {"action": "run_boss", "target": top["route"] or top["material"],
                           "count": top.get("count") or 1}

    for task in runnable:
        kind = task["task_type"]
        if kind in (TASK_DOMAIN, TASK_LEYLINE, TASK_BOSS):
            continue                            # 体力任务已在上面按优先级挑了
        if kind in free_action_map:
            # ⚠️ 去重：同一个族群的多个档位（史莱姆凝液/清/原浆）会解析到**同一条**
            # 「史莱姆」路线，不去重就会下发三条一模一样的 hunt 指令 —— 玩家把同一条
            # 路线跑三遍，收益一点没多（架构文档 §6 说的正是这个问题）。
            item = {
                "action": free_action_map[kind],
                "target": task["route"] or task["material"],
            }
            if item not in free_tasks:
                free_tasks.append(item)

    command = {}
    if energy_task:
        command["energy_task"] = energy_task
    if free_tasks:
        command["free_task"] = free_tasks
    return command


def summarise(phase_rows, tasks=None):
    """给人看的一行摘要 + 体力估算（Studio 卡片 / 审批屏用）。"""
    rows = list(phase_rows or ())
    tasks = list(tasks if tasks is not None else build_tasks(rows))
    runnable = [task for task in tasks
                if task["status"] == STATUS_RUNNABLE
                and int(task.get("missing") or 0) > 0]
    return {
        "phases": len(rows),
        "materials": sum(len(row.get("materials") or ()) for row in rows),
        "missing_kinds": sum(1 for row in rows for material in row.get("materials") or ()
                             if material["missing"] > 0),
        "open_phases": [row["phase"] for row in rows if not row.get("complete") and not row.get("skipped")],
        "tasks": len(tasks),
        "runnable": len(runnable),
        "blocked": len(tasks) - len(runnable),
        "resin": sum(task.get("resin") or 0 for task in runnable),
        "waiting_route": [task["material"] for task in tasks
                          if task["status"] == STATUS_WAITING_ROUTE],
    }


def describe_tasks(tasks, limit=12):
    """若干行人类可读的任务描述（终端 / LLM 工具返回用）。"""
    lines = []
    for task in list(tasks or ())[: int(limit)]:
        head = f"[{task['status_label']}] {task['task_label']}｜{task['material']}"
        if task.get("route") and task["route"] != task["material"]:
            head += f" → {task['route']}"
        head += f"（缺口 {task['missing']}）"
        lines.append(head)
        note = "；".join(part for part in (task.get("status_note"), task.get("count_note")) if part)
        if note:
            lines.append(f"    ↳ {note}")
    return lines


def resin_estimate(phase_rows):
    """按阶段拆的体力估算（§29：**只是规划参考**，不作为库存事实来源）。"""
    totals = {}
    for row in phase_rows or ():
        tasks = build_tasks([row])
        totals[row["phase"]] = sum(task.get("resin") or 0
                                   for task in tasks if task["status"] == STATUS_RUNNABLE)
    totals["total"] = sum(totals.values())
    return totals


def describe_missing(rows, limit=20):
    """`[{"name","missing"}]` → "摩拉 1,250,000；经验书 48" 这种给 LLM 看的文本。"""
    parts = []
    for item in list(rows or ())[: int(limit)]:
        name = item.get("item_name") or item.get("name") or "?"
        parts.append(f"{name} {int(item.get('missing') or 0):,}")
    return "；".join(parts) if parts else "没有缺口"


def today_plan_lines(phase_rows, tasks=None, limit=10):
    """「今日养成计划」的文本块（Studio 首页模块 + LLM 工具共用）。"""
    tasks = list(tasks if tasks is not None else build_tasks(phase_rows))
    if not tasks:
        return ["✅ 当前没有待补的材料。"]
    lines = []
    for row in list(phase_rows or ())[: int(limit)]:
        if row.get("skipped"):
            continue
        head = f"{row['character_name']}｜{row['phase_label']}"
        lines.append(head)
        for material in (row.get("materials") or [])[:6]:
            if material["missing"] <= 0:
                continue
            mark = "✓" if material["missing"] == 0 else "○"
            lines.append(f"  {mark} {material['item_name']} 缺 {material['missing']:,}")
    summary = summarise(phase_rows, tasks)
    lines.append(f"今日预计体力：{summary['resin']}（可执行任务 {summary['runnable']} 个）")
    return lines or ["✅ 当前没有待补的材料。"]


def now_text(moment=None):
    return (moment or datetime.datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
