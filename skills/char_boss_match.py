"""角色 → 突破 Boss 的确定性解析。

为什么需要它：
    LLM 不知道「蓝砚」的突破 Boss 是谁时，会**按元素属性猜**（蓝砚是风元素 → 猜「无相之风」），
    而正确答案是「秘源机兵·构型械」。猜错的代价是白跑一趟甚至打错 Boss。
    名字本身也不该靠猜 —— 本模块用本地字典把「角色名」确定性地翻译成官方 Boss 名。

判别式（关键）：角色的突破材料通常是
    [特产, 元素宝石, 怪物掉落, **首领专属材料**]
其中只有「首领专属材料」会**唯一映射到某一只首领**。宝石（如自在松石）会同时挂在一堆
首领名下，特产/怪物掉落则挂在几百个杂兵名下 —— 所以判据是
「**该材料唯一映射到一只脚本支持的首领**」，而不是「材料能映射到首领」。

解析链路：
    规则1  角色突破材料 ∩ (材料→敌首) ∩ 脚本首领名单，且该材料只有一个首领候选
    规则2  材料名里直接含首领名（「奇械发条备件·歌裴莉娅」→ 歌裴莉娅的葬送），
           用于补上 boss_drops_dict 缺该材料的情况（枫丹「冰风组曲」两个首领）
    兜底   玩家直接给「材料名」也能翻译成 Boss

数据来源：
    memory/game_dict_baike_full.json → avatars[*].materials（每个角色的突破材料）
    memory/boss_drops_dict.json      → 敌首掉落（含杂兵，故必须用上面的判别式收敛）
    脚本 assets/config/boss-list.json + assets/Pathing → 权威首领名单与支持性
"""

import json
import os
from functools import lru_cache
from typing import NamedTuple, Optional

import config

from skills.boss_pathing_guard import (
    ConfigTransactionError,
    collect_pathing_index,
    load_script_boss_list,
    normalize_boss_name,
    validate_boss_target,
)

# 解析结果类型
KIND_BOSS = "boss"                    # 已确定性解析出一只脚本支持的 Boss
KIND_UNSUPPORTED = "unsupported"      # 解析出 Boss，但脚本明确不支持（只能手动打）
KIND_UNDETERMINED = "undetermined"    # 是角色，但本地数据推不出 Boss（字典缺口）
KIND_NOT_A_CHARACTER = "not_character"  # 名字不在角色表里，按 Boss 名处理

# 角色突破材料字典（按优先级尝试；结构：avatars[*].materials）
# ⚠️ 用绝对路径：以前写的是 "memory/xxx.json"，只要进程工作目录不是仓库根（计划任务、
#    带"起始位置"的快捷方式、从别处 python <路径>\main.py）就会找不到字典，
#    表现是「角色名 → 查不到 → 让玩家去补字典」这种误导性报错。
AVATAR_DICT_CANDIDATES = tuple(
    config.project_path("memory", name) for name in (
        "game_dict_baike_full.json",
        "game_dict_yatta_enhanced.json",
        "game_dict_yatta.json",
    )
)
BOSS_DROPS_PATH = config.project_path("memory", "boss_drops_dict.json")

# 「不支持」候选的内部前缀
_UNSUPPORTED_PREFIX = "【不支持】"


class CharacterBossResult(NamedTuple):
    """角色 → 突破 Boss 的解析结果。"""

    kind: str
    character: Optional[str]
    boss: Optional[str]
    material: Optional[str]
    reason: str


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=8)
def _load_character_materials(avatar_dict_path=None):
    """返回 {角色中文名: [突破材料...]}。"""
    for path in ([avatar_dict_path] if avatar_dict_path else list(AVATAR_DICT_CANDIDATES)):
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        avatars = data.get("avatars")
        if not isinstance(avatars, dict):
            continue
        table = {}
        for info in avatars.values():
            if not isinstance(info, dict):
                continue
            name = info.get("name_zh")
            if name:
                table[name] = list(info.get("materials") or [])
        if table:
            return table
    return {}


@lru_cache(maxsize=4)
def _load_boss_material_index(drops_path=None):
    """返回 {材料名: [敌首名...]}（含杂兵，调用方必须再收敛）。"""
    data = _load_json(drops_path or BOSS_DROPS_PATH)
    if not isinstance(data, dict):
        return {}
    index = {}
    for boss_name, drops in data.items():
        if not isinstance(drops, list):
            continue
        for drop in drops:
            index.setdefault(str(drop), []).append(str(boss_name))
    return index


def _effective_script_dir(script_dir=None):
    """脚本《批量讨伐角色养成材料BOSS》的根目录（`config.BGI_BOSS_CONFIG` 推导）。

    ⚠️ 为什么要单独算出来当缓存键：`_boss_name_tables` 是 lru_cache 的，如果键里只有
    `script_dir=None`（"用配置里的"），那么**配置变了缓存却不会失效** —— 实测后果是
    拿到一次空名单就在整个进程里一直空（Boss 名全部退回"某材料的来源Boss"这种胡话）。
    测试里把 BGI 路径指向临时目录时尤其明显。
    """
    if script_dir:
        return str(script_dir)
    return os.path.dirname(os.path.dirname(os.path.dirname(str(config.BGI_BOSS_CONFIG))))


@lru_cache(maxsize=8)
def _boss_name_tables(script_dir):
    """返回 (支持的官方名: {规范化: 官方名}, 不支持: {规范化: 官方名})。

    键必须是**已经解析过的**目录（见 `_effective_script_dir`），不能是 None。
    """
    pathing_index = collect_pathing_index(script_dir)  # 只有这些名字真的能寻路
    supported_list, unsupported_list = load_script_boss_list(script_dir)
    supported, unsupported = {}, {}
    for name in list(pathing_index.keys()) + supported_list:
        supported.setdefault(normalize_boss_name(name), name)
    for name in unsupported_list:
        unsupported.setdefault(normalize_boss_name(name), name)
    return supported, unsupported


def _canonicalize_dropper(dropper, supported, unsupported):
    """把 boss_drops_dict 里的敌首名收敛到脚本名单。

    返回官方名、"【不支持】官方名"，或 None（既不是首领，也不是脚本认识的名字）。
    「无相之风 贝特」→ 无相之风（去掉个人名后缀）；
    「纯水精灵·洛蒂娅」→ 纯水精灵（去掉「·」后缀）；
    「冰风组曲」这类一对多的组合名 → None。
    """
    normalized = normalize_boss_name(dropper)
    for table, prefix in ((supported, ""), (unsupported, _UNSUPPORTED_PREFIX)):
        if normalized in table:
            return prefix + table[normalized]
    for table, prefix in ((supported, ""), (unsupported, _UNSUPPORTED_PREFIX)):
        for key, name in table.items():
            if key and (dropper.startswith(name + " ") or dropper.startswith(name + "·")):
                return prefix + name
    return None


def find_character(raw_name, avatar_dict_path=None):
    """在角色表里找这个名字：先精确，再取「名字出现在输入里」的最长者。

    「蓝砚」「蓝砚的突破Boss」「给蓝砚打点突破材料」都能找到 蓝砚。
    """
    table = _load_character_materials(avatar_dict_path)
    if not table:
        return None

    text = str(raw_name or "").strip()
    if not text:
        return None
    if text in table:
        return text

    normalized = normalize_boss_name(text)
    for name in table:
        if normalize_boss_name(name) == normalized:
            return name

    # 子串匹配：角色名出现在玩家给出的自然语言里
    hits = [name for name in table if len(name) >= 2 and name in text]
    if hits:
        return max(hits, key=len)
    return None


def _resolve_by_materials(materials, material_index, supported, unsupported):
    """规则1 + 规则2：返回 (官方名, 命中材料, 说明) 或 (None, None, 原因)。"""
    # 规则1：某材料唯一映射到一只首领（宝石/特产/怪物掉落会被这条自动排除）
    by_material = {}
    for material in materials:
        candidates = set()
        for dropper in material_index.get(material, []):
            canonical = _canonicalize_dropper(dropper, supported, unsupported)
            if canonical:
                candidates.add(canonical)
        if len(candidates) == 1:
            by_material[material] = candidates.pop()

    distinct = set(by_material.values())
    if len(distinct) == 1:
        boss = distinct.pop()
        material = next(m for m, b in by_material.items() if b == boss)
        return boss, material, "突破材料唯一映射到该首领"
    if len(distinct) > 1:
        detail = "、".join(f"{m}→{b}" for m, b in sorted(by_material.items()))
        return None, None, f"多个材料指向不同首领（{detail}），需要玩家确认"

    # 规则2：材料名里直接带首领名（奇械发条备件·歌裴莉娅 → 歌裴莉娅的葬送）
    prefixed = {}
    for material in materials:
        normalized_material = normalize_boss_name(material)
        for key, name in supported.items():
            if len(key) < 3:
                continue
            # 用首领名的前缀去材料名里找（材料名不会写全「歌裴莉娅的葬送」）
            for length in range(len(key), 2, -1):
                if key[:length] in normalized_material:
                    prefixed.setdefault(name, material)
                    break
    if len(prefixed) == 1:
        boss, material = next(iter(prefixed.items()))
        return boss, material, "材料名里含首领名"
    if prefixed:
        return None, None, f"材料名同时匹配多个首领（{'、'.join(sorted(prefixed))}），需要玩家确认"

    # 区分「本来就不需要首领材料」（旅行者/奇偶）与「字典缺数据」
    for material in materials:
        for dropper in material_index.get(material, []):
            if _canonicalize_dropper(dropper, supported, unsupported):
                return None, None, "突破材料里没有任何材料能唯一映射到脚本首领（本地字典可能缺该材料）"
    return None, None, "该角色的突破材料不来自任何首领（旅行者/奇偶这类不需要首领材料）"


def resolve_character_boss(raw_name, script_dir=None, avatar_dict_path=None, drops_path=None):
    """把角色名解析成突破 Boss。返回 CharacterBossResult。"""
    character = find_character(raw_name, avatar_dict_path)
    if not character:
        return CharacterBossResult(
            KIND_NOT_A_CHARACTER, None, None, None, f"'{raw_name}' 不在角色表里"
        )

    materials = _load_character_materials(avatar_dict_path).get(character, [])
    if not materials:
        return CharacterBossResult(
            KIND_UNDETERMINED, character, None, None, f"本地字典里没有 {character} 的突破材料数据"
        )

    supported, unsupported = _boss_name_tables(_effective_script_dir(script_dir))
    if not supported and not unsupported:
        return CharacterBossResult(
            KIND_UNDETERMINED, character, None, None, "没找到脚本的首领名单，无法校验突破 Boss"
        )

    boss, material, why = _resolve_by_materials(
        materials, _load_boss_material_index(drops_path), supported, unsupported
    )
    if not boss:
        return CharacterBossResult(KIND_UNDETERMINED, character, None, None, why)

    if boss.startswith(_UNSUPPORTED_PREFIX):
        name = boss[len(_UNSUPPORTED_PREFIX):]
        return CharacterBossResult(
            KIND_UNSUPPORTED,
            character,
            name,
            material,
            f"{character} 的突破 Boss 是「{name}」，但脚本没有它的路径（标注为不支持）",
        )

    return CharacterBossResult(
        KIND_BOSS, character, boss, material, f"突破材料「{material}」→ {why}"
    )


def resolve_boss_from_materials(materials, script_dir=None, drops_path=None):
    """给一组突破材料，返回 (官方 Boss 名, 命中材料) 或 (None, None)。

    供 env_reader 这类"按位置取第 N 个材料"的旧逻辑替换用：
    位置会取到元素宝石（自在松石挂在 8 只首领名下）或怪物掉落（导能绘卷挂在水丘丘萨满名下），
    实测 127 个角色里有 12 个因此拿到了**错误的** Boss 名。
    """
    if not materials:
        return None, None

    supported, unsupported = _boss_name_tables(_effective_script_dir(script_dir))
    if not supported and not unsupported:
        return None, None

    boss, material, _why = _resolve_by_materials(
        list(materials), _load_boss_material_index(drops_path), supported, unsupported
    )
    if not boss:
        return None, None
    if boss.startswith(_UNSUPPORTED_PREFIX):
        return boss[len(_UNSUPPORTED_PREFIX):], material
    return boss, material


def resolve_material_boss(raw_name, script_dir=None, drops_path=None):
    """玩家直接给材料名时，翻译成 Boss 名；返回 (官方名, 材料) 或 (None, None)。"""
    material = str(raw_name or "").strip()
    if not material:
        return None, None

    supported, unsupported = _boss_name_tables(_effective_script_dir(script_dir))
    candidates = set()
    for dropper in _load_boss_material_index(drops_path).get(material, []):
        canonical = _canonicalize_dropper(dropper, supported, unsupported)
        if canonical and not canonical.startswith(_UNSUPPORTED_PREFIX):
            candidates.add(canonical)
    if len(candidates) == 1:
        return candidates.pop(), material
    return None, None


def resolve_boss_target(raw_target, script_dir=None, avatar_dict_path=None, drops_path=None):
    """把 LLM/玩家给出的目标翻译成脚本认得的 Boss 名。

    依次尝试：角色名 → 材料名 → 直接当 Boss 名校验。
    返回 (官方 Boss 名, notices)；无法安全执行时抛 ConfigTransactionError，
    调用方因此不会写任何配置、也不会启动 BetterGI。
    """
    raw_target = str(raw_target or "").strip()
    notices = []

    result = resolve_character_boss(raw_target, script_dir, avatar_dict_path, drops_path)
    if result.kind == KIND_BOSS:
        notices.append(
            f"🔎 角色解析：{result.character} 的{result.reason} → 「{result.boss}」"
        )
        # 再过一遍路径校验，保证脚本真的能寻路到这个 Boss
        boss, guard_notices = validate_boss_target(result.boss, script_dir=script_dir)
        return boss, notices + guard_notices

    if result.kind == KIND_UNSUPPORTED:
        raise ConfigTransactionError(
            f"❌ {result.reason}。\n"
            f"   ↳ 脚本《批量讨伐角色养成材料BOSS》只支持它 assets/Pathing 里有路线的首领，"
            f"这个只能手动打或换角色。"
        )

    if result.kind == KIND_UNDETERMINED:
        raise ConfigTransactionError(
            f"❌ 无法确定 {result.character} 的突破 Boss：{result.reason}。\n"
            f"   ↳ 请不要按元素属性推断。请让玩家直接给【Boss 名】或【突破材料名】，"
            f"或先把该角色的突破材料补进 memory 字典。"
        )

    # 不是角色名：可能是材料名，也可能本来就是 Boss 名
    material_boss, material = resolve_material_boss(raw_target, script_dir, drops_path)
    if material_boss:
        notices.append(f"🔎 材料解析：突破材料「{material}」→ 「{material_boss}」")
        boss, guard_notices = validate_boss_target(material_boss, script_dir=script_dir)
        return boss, notices + guard_notices

    boss, guard_notices = validate_boss_target(raw_target, script_dir=script_dir)
    return boss, notices + guard_notices
