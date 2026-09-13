"""角色 / 武器 / 材料 → 秘境（含 domain_index）的确定性解析。

为什么需要它：
    LLM 面对「去打一次蓝砚武器的突破副本」时，会声称「蓝砚及其武器均不在展柜 JSON 中」
    然后填 `target: "蓝砚"` —— 而展柜里明明有蓝砚和她的讨龙英杰谭，
    `bgi_controller` 还会把 "蓝砚" 原样写进 BetterGI 的 `DomainName`（匹配不到秘境 = 白跑）。
    所以这一层由代码自己看展柜：角色 → 武器 → 武器突破素材 → 炼武秘境 + 开放日程。

数据来源：
    memory/game_dict_baike_full.json
      - avatars[*] 的 talent_materials.sources / ascension_material_sources（材料 → 日程）
      - weapons[*] 的 ascension_material_sources 与 materials（武器 → 突破素材族）
    展柜（Enka）：角色当前携带的武器（只有这里能知道"蓝砚带的是哪把武器"）
    脚本侧 DOMAIN_FIX_MAP：百科给的是秘境里的【阶段名】（深没之谷/水光之城/鸣雷城墟…），
      要换算成 BetterGI 认的秘境名（塞西莉亚苗圃/震雷连山密宫…）
"""

import datetime
import json
import re
from functools import lru_cache
from typing import NamedTuple, Optional

import config
from skills.boss_pathing_guard import ConfigTransactionError, normalize_boss_name
from skills.env_reader import DOMAIN_FIX_MAP

GAME_DICT_PATH = config.project_path("memory", "game_dict_baike_full.json")
ARTIFACT_DICT_PATH = config.project_path("memory", "artifact_get_methods_raw.json")
# 圣遗物副本名的长度上限：百科那一列有时整段是说明文字，只取像名字的短串
_ARTIFACT_NAME_LIMIT = 8

# 秘境阶段的开放日程 → BetterGI 的 SundayEverySelectedValue（周日/全开日选哪一档）
_DAY_TO_INDEX = {
    "一": "1", "四": "1", "日": None,   # 周一/周四 → 1；周日三种都开，不单独定档
    "二": "2", "五": "2",
    "三": "3", "六": "3",
}

_STAGE_PATTERN = re.compile(r"(?:炼武|精通|练武)秘境[：:]\s*([^（(【]+?)[ⅠⅡⅢⅣ]")
# 日程可能写成「周二/五/日」「周一、四」「周三,六,日」，也可能每天带「周」字：
# 先把整组抓下来，再逐个取星期字，否则「周二/五/日」只会解析出「二」
_DAY_GROUP_PATTERN = re.compile(r"周([一二三四五六日](?:[/、,，]周?[一二三四五六日])*)")
_DAY_CHAR_PATTERN = re.compile(r"[一二三四五六日]")
_BRACKET_PATTERN = re.compile(r"【([^】]+)】")


class DomainResult(NamedTuple):
    """秘境解析结果。"""

    domain: str
    domain_index: Optional[str]
    material: Optional[str]
    reason: str
    days: tuple = ()

    @property
    def open_days(self):
        """开放日程的展示文本，例如「周二/五/日」；推不出来时是空串。"""
        return "周" + "/".join(self.days) if self.days else ""


def business_weekday(moment=None):
    """业务日期的星期几（对齐项目里的"凌晨 4 点刷新"口径：4 点之前算前一天）。"""
    shifted = (moment or datetime.datetime.now()) - datetime.timedelta(hours=4)
    return "一二三四五六日"[shifted.isoweekday() - 1]


def domain_closed_notice(result, today=None):
    """秘境今日不开放时的提示；开放或推不出日程时返回空串。

    只提示不阻断 —— 玩家的显式指令仍然照做（见 system_rules 的"显式指令优先"）。
    """
    if not result or not result.days:
        return ""
    weekday = today or business_weekday()
    if weekday in result.days:
        return ""

    subject = f"突破素材「{result.material}」" if result.material else f"秘境「{result.domain}」"
    return (
        f"⚠️ 该秘境今日不开放：{subject}的开放日程是 {result.open_days}，今天是周{weekday}。\n"
        f"   ↳ 今天打这一趟拿不到该材料；周日（或全开日）三个档位才全开。"
    )


@lru_cache(maxsize=4)
def _load_index(data_path=None):
    """扫描百科字典，建立 (材料→日程) / (武器→素材) / (秘境名集合) 三张表。"""
    path = data_path or GAME_DICT_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, {}, set()

    material_schedule = {}
    weapons = {}

    def collect(name, sources):
        for material, info in (sources or {}).items():
            if not isinstance(info, dict):
                continue
            schedule = info.get("schedule")
            if not schedule:
                continue
            # 同一种材料可能有多条记录：优先保留信息最全的那条（带【秘境名】）
            old = material_schedule.get(material)
            if old is None or ("【" in schedule and "【" not in old):
                material_schedule[material] = schedule
        return

    for node in (data.get("avatars") or {}).values():
        if not isinstance(node, dict) or not node.get("name_zh"):
            continue
        collect(node["name_zh"], node.get("talent_materials", {}).get("sources"))
        collect(node["name_zh"], node.get("ascension_material_sources"))

    for node in (data.get("weapons") or {}).values():
        if not isinstance(node, dict) or not node.get("name_zh"):
            continue
        collect(node["name_zh"], node.get("ascension_material_sources"))
        weapons[node["name_zh"]] = {
            "materials": list(node.get("materials") or []),
            "sources": node.get("ascension_material_sources") or {},
        }

    return material_schedule, weapons, set(material_schedule)


def extract_domain(schedule):
    """从日程串里取出 (秘境名, [开放星期])。

    处理三种写法：
      1. 【塞西莉亚苗圃】（周二/五/日）】炼武秘境：深没之谷Ⅳ   → 直接用【】里的名字
      2. （周一/四/日）】炼武秘境：水光之城Ⅲ/Ⅳ                → 用阶段名查 DOMAIN_FIX_MAP
      3. 【菫色之庭（周一/四/日）】…                          → 去掉名字里粘上的日程
    """
    if not schedule or "秘境" not in str(schedule):
        return None, []

    text = str(schedule)
    name = None
    match = _BRACKET_PATTERN.search(text)
    if match and not match.group(1).startswith("（"):
        name = re.sub(r"（[^）]*）$", "", match.group(1)).strip()

    if not name:
        for keyword, real_name in DOMAIN_FIX_MAP.items():
            if keyword in text:
                name = real_name
                break

    if not name:
        stage = _STAGE_PATTERN.search(text)
        if stage:
            name = DOMAIN_FIX_MAP.get(stage.group(1).strip())

    days = []
    for group in _DAY_GROUP_PATTERN.findall(text):
        days.extend(_DAY_CHAR_PATTERN.findall(group))
    # 去重但保留顺序，避免「周二/二」这种重复
    seen = set()
    unique_days = [day for day in days if not (day in seen or seen.add(day))]
    return (name or None), unique_days


def domain_index_from_days(days):
    """开放日程 → domain_index（周一/四/日→1，周二/五/日→2，周三/六/日→3）。"""
    for day in days or []:
        index = _DAY_TO_INDEX.get(day)
        if index:
            return index
    return None


def _domain_from_schedule(schedule, material=None, reason=""):
    domain, days = extract_domain(schedule)
    if not domain:
        return None
    return DomainResult(
        domain, domain_index_from_days(days), material, reason, tuple(days or ())
    )


def _resolve_weapon(weapon_name, index, material_schedule):
    """武器 → 突破秘境。先看武器自己的来源表，再退回它素材族里任一材料的日程。"""
    weapons = index
    info = weapons.get(weapon_name)
    if not info:
        return None

    for material, detail in info["sources"].items():
        if not isinstance(detail, dict):
            continue
        result = _domain_from_schedule(
            detail.get("schedule"),
            material,
            f"武器「{weapon_name}」的突破素材「{material}」",
        )
        if result:
            return result

    # 有些武器（狼的末路/祭礼残章…）的来源表是坏的，但它 materials 里列了素材族，
    # 用素材族里任一有日程的材料反查秘境
    for material in info["materials"]:
        schedule = material_schedule.get(material)
        if not schedule:
            continue
        result = _domain_from_schedule(
            schedule, material, f"武器「{weapon_name}」的突破素材「{material}」"
        )
        if result:
            return result
    return None


def _character_weapon_name(character, avatars):
    for avatar in avatars or []:
        if avatar.get("name") == character:
            return (avatar.get("weapon") or {}).get("name")
    return None


def _character_weapon_with_source(character, avatars, uid=None):
    """TA 带的是哪把武器 + 这份资料从哪来。

    展柜只有 8 个角色，展柜里没有就问米游社（配了 `MYS_COOKIE` 才有人答，见 docs/MYS_COOKIE.md）。
    返回 (武器名, 来源文案)，都没有则 (None, "")。
    """
    name = _character_weapon_name(character, avatars)
    if name:
        return name, "展柜"

    try:
        from skills import mys_api

        if not mys_api.cookie_configured():
            return None, ""
        snapshot = mys_api.get_snapshot(uid=uid)
        avatar = mys_api.find_by_name(snapshot, character)
        weapon = (avatar or {}).get("weapon") or {}
        weapon_name = str(weapon.get("name") or "")
        if weapon_name and weapon_name != "未装备":
            return weapon_name, "米游社个人战绩"
    except Exception as exc:        # noqa: BLE001 —— 米游社挂了不该让秘境解析失败
        print(f"⚠️ 米游社查「{character}」的武器失败（继续按展柜判断）：{exc}")
    return None, ""


@lru_cache(maxsize=4)
def _load_character_names(data_path=None):
    path = data_path or GAME_DICT_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ()
    return tuple(
        node["name_zh"]
        for node in (data.get("avatars") or {}).values()
        if isinstance(node, dict) and node.get("name_zh")
    )


def _match_name(raw, candidates):
    """先精确、再忽略分隔符、最后唯一子串；找不到返回 None。"""
    if raw in candidates:
        return raw
    target = normalize_boss_name(raw)
    normalized = {normalize_boss_name(name): name for name in candidates}
    if target in normalized:
        return normalized[target]
    hits = [name for name in candidates if len(name) >= 2 and name in raw]
    if hits:
        return max(hits, key=len)
    return None


@lru_cache(maxsize=2)
def _artifact_domain_names(data_path=None):
    """圣遗物秘境名集合。

    `run_artifact` 会被上层（llm_brain/artifact_match）改写成 `run_domain` + 副本名下发，
    所以这些副本名必须直接放行，不能被当成"未知目标"拒掉。
    """
    path = data_path or ARTIFACT_DICT_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()

    names = set()
    for item in data.values():
        tables = (item or {}).get("matched_tables") or []
        for table in tables:
            for row in (table or {}).get("rows") or []:
                if len(row) < 2 or row[0] != "获取途径":
                    continue
                value = str(row[1]).strip()
                match = re.match(r"^([^：:]+)[：:]", value)
                # 百科有些行是「圣遗物秘境：临瀑之城」，也有些只写「月童的库藏」，两种都要认
                name = match.group(1).strip() if match else value
                if 2 <= len(name) <= _ARTIFACT_NAME_LIMIT and not any(
                    ch in name for ch in "；;。，,、 "
                ):
                    names.add(name)
    return frozenset(names)


def _showcase_avatars(uid=None, avatars=None):
    """优先用调用方给的展柜（空列表也算给了），其次同进程最近一次抓取，最后自己抓一次。"""
    if avatars is not None:
        return avatars

    from skills.env_reader import fetch_env_data, latest_env_data

    cached = latest_env_data(uid)
    if isinstance(cached, dict) and cached.get("avatars"):
        return cached["avatars"]

    if uid:
        # fetch_env_data 会把结果写回进程缓存，后面的解析调用就不必再打 Enka
        try:
            fresh = fetch_env_data(uid)
        except Exception:
            return []
        if isinstance(fresh, dict) and fresh.get("avatars"):
            return fresh["avatars"]
    return []


# ==========================================
# 🌟 「只刷 N 次」→ BetterGI 自动秘境的树脂策略
# ==========================================
# BetterGI 的自动秘境只有两种模式（GameTask/AutoDomain/AutoDomainTask.cs:290）：
#   SpecifyResinUse = false → 日志「用尽所有浓缩树脂和原粹树脂后结束」= 刷到干
#   SpecifyResinUse = true  → 按 AutoDomainConfig 里各树脂的「刷取次数」逐次消费，
#                             次数用完即 isLastTurn（AutoDomainTask.cs:1277-1316）
# 所以「只打一次」必须写**全局** User/config.json → autoDomainConfig；
# 一条龙那层只有 partyName/domainName/sundaySelectedValue，没有树脂策略。
RESIN_ALL_COUNT_FIELDS = (
    "originalResinUseCount",
    "originalResin20UseCount",
    "originalResin40UseCount",
    "condensedResinUseCount",
    "transientResinUseCount",
    "fragileResinUseCount",
)
# 树脂名 → 对应的次数字段（字段含义见 BetterGI AutoDomainConfig.cs 的注释）
_RESIN_NAME_TO_FIELD = {
    "原粹树脂": "originalResinUseCount",
    "原粹树脂20": "originalResin20UseCount",
    "原粹树脂40": "originalResin40UseCount",
    "浓缩树脂": "condensedResinUseCount",
    "须臾树脂": "transientResinUseCount",
    "脆弱树脂": "fragileResinUseCount",
}


def build_domain_resin_plan(runs, preference=None):
    """把「刷 N 次」翻译成 autoDomainConfig 的补丁。

    runs 为 None / 0 / 负数 / 解析失败时返回 None —— 表示不该动树脂策略，
    保持 BetterGI 原本的「刷到体力耗尽」。
    """
    try:
        count = int(float(str(runs).strip()))
    except (TypeError, ValueError):
        return None
    if count <= 0:
        return None

    resin = (preference or config.DOMAIN_RESIN_PREFERENCE or "原粹树脂20").strip()
    field = _RESIN_NAME_TO_FIELD.get(resin)
    if not field:
        resin, field = "原粹树脂20", "originalResin20UseCount"

    # 其余树脂次数必须清零：不然上一次留下的次数会让它多刷几轮
    plan = {name: 0 for name in RESIN_ALL_COUNT_FIELDS}
    plan[field] = count
    plan["specifyResinUse"] = True
    plan["resinPriorityList"] = [resin]
    return plan


def describe_domain_resin_plan(runs, preference=None):
    """审批屏/终端用的文案；没给次数时给出"会刷干体力"的提醒。"""
    plan = build_domain_resin_plan(runs, preference)
    if not plan:
        return (
            "⚠️ 本次没指定刷取次数：会沿用 BetterGI 现有的树脂策略"
            "（若 specifyResinUse 仍为 false，就是【刷到体力耗尽】为止）。"
            "要限定次数请说「打一次 / 打 N 次」。"
        )
    resin = plan["resinPriorityList"][0]
    return (
        f"⏳ 已按「只刷 {plan[_RESIN_NAME_TO_FIELD[resin]]} 次」设置树脂策略：{resin}"
        f"（specifyResinUse=true，其余树脂次数清零；可用 rollback 还原）"
    )


def resolve_domain_target(    raw, uid=None, avatars=None, data_path=None, artifact_path=None, today=None
):
    """把 角色名 / 武器名 / 材料名 / 秘境名 解析成 (DomainResult, notices)。

    解析不出来抛 ConfigTransactionError —— 调用方因此不会写任何配置，
    `DomainName` 也不会被写成「蓝砚」这种非法值。
    notices 里会带上"今日不开放"的提醒（不阻断，显式指令照样执行）。
    """
    raw = str(raw or "").strip()
    if not raw:
        raise ConfigTransactionError("❌ 没有解析到秘境目标（energy_task.target 为空）。")

    material_schedule, weapon_index, known_materials = _load_index(data_path)

    def finish(result, notices):
        """统一收尾：补上"今日不开放"的提醒。"""
        closed = domain_closed_notice(result, today)
        return result, (notices + [closed] if closed else notices)

    # 1) 本来就是秘境名：能认，但光有秘境名推不出 domain_index（一个秘境有 3 个阶段、
    #    对应 3 组开放日），所以 index 留空交给调用方的默认值
    if raw in DOMAIN_FIX_MAP.values():
        return finish(DomainResult(raw, None, None, f"直接指定秘境「{raw}」"), [])

    if raw in _artifact_domain_names(artifact_path):
        # 圣遗物秘境常驻开放，没有日程可判
        return finish(DomainResult(raw, None, None, f"圣遗物秘境「{raw}」"), [])

    # 2) 角色名 → 当前武器 → 突破秘境
    character = _match_name(raw, _load_character_names(data_path))
    if character:
        weapon_name, weapon_source = _character_weapon_with_source(
            character, _showcase_avatars(uid, avatars), uid=uid
        )
        if not weapon_name:
            raise ConfigTransactionError(
                f"❌ 「{character}」不在当前展柜数据里，无法知道 TA 带的是哪把武器。\n"
                f"   ↳ 武器突破秘境取决于武器本身。请让玩家直接给【武器名】或【武器突破材料名】，"
                f"或者先把该角色放进游戏内的角色展柜并输入 refresh"
                f"（想查展柜外的角色，可以在 .env 里填 MYS_COOKIE，见 docs/MYS_COOKIE.md）。"
            )
        result = _resolve_weapon(weapon_name, weapon_index, material_schedule)
        if not result:
            raise ConfigTransactionError(
                f"❌ 知道 {character} 带的是「{weapon_name}」，但字典里查不到这把武器的突破秘境。\n"
                f"   ↳ 请让玩家提供【武器突破材料名】（例如「凛风奔狼的怀乡」）。"
            )
        notices = [
            f"🔎 角色解析：{character} 当前武器「{weapon_name}」"
            + (f"（来自{weapon_source}）" if weapon_source else "")
            + f"→ {result.reason} → 秘境【{result.domain}】"
            + (f"，domain_index={result.domain_index}" if result.domain_index else "")
        ]
        return finish(result, notices)


    # 3) 武器名 → 突破秘境
    weapon_name = _match_name(raw, tuple(weapon_index))
    if weapon_name:
        result = _resolve_weapon(weapon_name, weapon_index, material_schedule)
        if not result:
            raise ConfigTransactionError(
                f"❌ 「{weapon_name}」的突破材料在字典里没有可用的秘境日程，"
                f"请让玩家提供【材料名】。"
            )
        notices = [
            f"🔎 武器解析：{weapon_name} → {result.reason} → 秘境【{result.domain}】"
            + (f"，domain_index={result.domain_index}" if result.domain_index else "")
        ]
        return finish(result, notices)

    # 4) 材料名 → 秘境
    material = _match_name(raw, tuple(known_materials))
    if material:
        result = _domain_from_schedule(
            material_schedule.get(material), material, f"材料「{material}」"
        )
        if result:
            notices = [
                f"🔎 材料解析：{material} → 秘境【{result.domain}】"
                + (f"，domain_index={result.domain_index}" if result.domain_index else "")
            ]
            return finish(result, notices)

    raise ConfigTransactionError(
        f"❌ 无法把 '{raw}' 解析成秘境：既不是秘境名，也匹配不到角色 / 武器 / 材料。\n"
        f"   ↳ 请给【秘境名】（如「塞西莉亚苗圃」）、【武器名】或【武器突破材料名】。\n"
        f"   ↳ 严禁编造秘境名 —— 会被原样写进 BetterGI 的 DomainName，匹配不到就是白跑。"
    )
