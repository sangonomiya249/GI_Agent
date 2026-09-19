"""材料族解析：材料 → 族群 → BetterGI 路线池（架构文档 §6/§7/§11/§23）。

**为什么需要这一层**：米游社算出来的缺口永远是**具体材料**（异色结晶石），
而 BetterGI 的路线是按**族群**组织的（原海异种）。两边词表不同 ——
本机 `material_category_index()` 里有 `原海异种`，却**没有** `异色结晶石`。
少了中间这层，"缺异色结晶石"就永远匹配不到路线（WAITING_ROUTE），
而同族的低档材料（异海凝珠）反而有路线，界面上看就是"有的有来源、有的没有"。

接上之后：
    异海凝珠 / 异海之块 / 异色结晶石
        → resolve_material_family() → 「原海异种」材料族
        → get_routes_by_family()    → 54 条 BetterGI 路线文件

知识表由 `scripts/build_material_families.py` 离线生成（见那个文件的说明），
本模块**只读** `memory/game_knowledge/material_families.json`，
并可选地叠加 `config/knowledge_overrides.json` 里的人工修正（§22 覆盖原则）。

路线是**执行层**的事，族群是**游戏知识**的事，两者不混（§23）：
本模块只回答"这个材料属于哪个族群"，"哪条路线能跑"交给 `get_routes_by_family()`
去问 BetterGI 本机路线库。
"""

import io
import json
import os
import threading

from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWLEDGE_PATH = os.path.join(ROOT, "memory", "game_knowledge", "material_families.json")
OVERRIDE_PATH = os.path.join(ROOT, "config", "knowledge_overrides.json")

_LOCK = threading.RLock()
_KNOWLEDGE = None
_UNMATCHED_INDEX = None
_UNMATCHED_SOURCE = None


# ==========================================
# 🌟 知识表装载
# ==========================================

def knowledge(force=False):
    """读知识表（进程内缓存一次；`force=True` 强制重读）。读不到给空表，绝不抛。"""
    global _KNOWLEDGE
    with _LOCK:
        if _KNOWLEDGE is not None and not force:
            return _KNOWLEDGE
        data = {"families": {}, "materials": {}, "stats": {}, "available": False}
        try:
            if os.path.exists(KNOWLEDGE_PATH):
                with io.open(KNOWLEDGE_PATH, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    data.update(loaded)
                    data["available"] = bool(loaded.get("materials"))
        except Exception as exc:            # noqa: BLE001 —— 缺表只该让族群解析失效，不该让规划崩
            print(f"⚠️ 材料族知识表读不了（{type(exc).__name__}: {exc}），族群解析本次不可用")
        _KNOWLEDGE = data
        return _KNOWLEDGE


def refresh():
    """清掉缓存（数据重建/人工修正后调用）。"""
    global _KNOWLEDGE, _UNMATCHED_INDEX, _UNMATCHED_SOURCE
    with _LOCK:
        _KNOWLEDGE = None
        _UNMATCHED_INDEX = None
        _UNMATCHED_SOURCE = None
    return knowledge()


def available():
    """知识表到底有没有内容（没有时上层应保持"缺少路线"的诚实结论，而不是假装有）。"""
    return bool(knowledge().get("available"))


def stats():
    """给界面/诊断用的一行统计。"""
    data = knowledge()
    counts = dict(data.get("stats") or {})
    counts["available"] = bool(data.get("available"))
    counts["built_at"] = data.get("built_at") or ""
    return counts


# ==========================================
# 🌟 材料 → 族群（§11 核心 API）
# ==========================================

@dataclass(frozen=True)
class MaterialFamily:
    """一个材料族：同一种怪掉的多个档位材料合成一族（§7）。"""

    id: str
    name: str
    type: str
    channel: str
    route: str
    route_files: int = 0
    route_note: str = ""
    materials: tuple = field(default_factory=tuple)      # ((材料名, 最低等级, 档位), …)
    source_texts: tuple = field(default_factory=tuple)

    @property
    def is_enemy_drop(self):
        return self.type == "enemy_drop"

    @property
    def has_route(self):
        """这个族有没有可执行的 BetterGI 路线（首领/周本族天生没有，走另一套功能）。"""
        return bool(self.route)

    def material_names(self):
        return [item[0] for item in self.materials]

    def rank_of(self, material):
        name = _material_name(material)
        for member, _level, rank in self.materials:
            if member == name:
                return rank
        return 0


def _material_name(material):
    """材料可以是名字、`{"item_name": …}` 这种行、或者数字 id。"""
    if isinstance(material, dict):
        for key in ("item_name", "material", "name", "material_name"):
            value = material.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    return str(material or "").strip()


def resolve_material_family(material):
    """材料 → `MaterialFamily`；认不出来给 `None`（**不猜**，§33）。

    接受材料名、`{"item_name": …}` 行、或数字 item_id（知识表按名字建索引，
    id 查不到就给 None —— 上层仍然能按名字再试一次）。
    """
    name = _material_name(material)
    if not name:
        return None
    data = knowledge()
    slot = (data.get("materials") or {}).get(name)
    if not slot:
        slot = _override_slot(name)
    if not slot:
        return None
    family_id = slot.get("family") or ""
    raw = (knowledge().get("families") or {}).get(family_id)
    if not raw:
        return None
    return _to_family(raw, slot)


def _override_slot(name):
    """人工修正：`knowledge_overrides.json` 里的 `material_family_overrides`（§22）。"""
    try:
        if not os.path.exists(OVERRIDE_PATH):
            return None
        # `utf-8-sig`：有人在 Windows 上用记事本/PowerShell 存过这个文件，会带 BOM，
        # 严格 utf-8 读会直接抛 JSONDecodeError（踩过一次）。
        with io.open(OVERRIDE_PATH, encoding="utf-8-sig") as handle:
            overrides = json.load(handle)
    except Exception:                       # noqa: BLE001
        return None
    for item in (overrides.get("material_family_overrides") or []):
        names = [str(x).strip() for x in (item.get("materials") or [])]
        if name in names:
            return {"material": name,
                    "family": item.get("family_id") or item.get("family"),
                    "type": item.get("type") or "enemy_drop",
                    "channel": item.get("channel") or "mob",
                    "route": item.get("route") or "",
                    "origin": "knowledge_overrides.json"}
    return None


def _to_family(raw, slot=None):
    route = (slot or {}).get("route") or raw.get("route") or ""
    members = []
    for item in (raw.get("materials") or []):
        if isinstance(item, dict):
            members.append((str(item.get("material") or ""), int(item.get("min_level") or 0),
                            int(item.get("rank") or 0)))
        elif isinstance(item, str):
            members.append((item, 0, 0))
    return MaterialFamily(
        id=str(raw.get("family_id") or raw.get("name") or ""),
        name=str(raw.get("name") or ""),
        type=str(raw.get("type") or ""),
        channel=str(raw.get("channel") or ""),
        route=str(route),
        route_files=int(raw.get("route_files") or 0),
        route_note=str(raw.get("route_note") or ""),
        materials=tuple(members),
        source_texts=tuple(str(x) for x in (raw.get("source_texts") or ())),
    )


def family_by_id(family_id):
    """按族名/族 id 取族（族 id 离线生成时就是中文名，见构建脚本的说明）。"""
    raw = (knowledge().get("families") or {}).get(str(family_id or "").strip())
    return _to_family(raw) if raw else None


def get_family_enemies(family_id):
    """这一族的**成员怪物**（架构文档 §8 的 `enemy_families`）。

    数据来自 genshin-db 的掉落表 —— 比"族群名"更具体：要跑路线时知道该找哪些怪，
    也解释了为什么同族材料是"一趟全掉"（重甲蟹、膨膨兽掉的是同一套三档材料）。
    """
    raw = (knowledge().get("families") or {}).get(str(family_id or "").strip())
    if not raw:
        return []
    out = []
    for item in (raw.get("enemies") or ()):
        if isinstance(item, dict):
            out.append({"name": str(item.get("name") or ""),
                        "category_text": str(item.get("category_text") or ""),
                        "drops": list(item.get("drops") or ())})
        elif isinstance(item, str):
            out.append({"name": item, "category_text": "", "drops": []})
    return out


def talent_domain(material):
    """天赋书 → 秘境（含**开放星期**），认不出给 `None`。

    为什么单独一份：本地百科字典的秘境索引里 21 个系列的「教导」档全缺，
    较新的系列整个没有。genshin-db 的 `domains.daysOfWeek` 是权威且完整的
    （v6.2 覆盖到的系列），实测与项目现有索引一致（箴铭 = 周二/五/日）。

    返回：`{"stage": "箴铭", "entrance": "苍白的遗荣", "days": ["二","五","日"], …}`
    —— `days` 是单个汉字，和 `domain_match.business_weekday()` 同一个口径。
    """
    name = _material_name(material)
    if not name:
        return None
    slot = (knowledge().get("talent_domains") or {}).get(name)
    if not slot:
        return None
    return {
        "material": name,
        "stage": str(slot.get("stage") or ""),
        "stage_full": str(slot.get("stage_full") or ""),
        "entrance": str(slot.get("entrance") or ""),
        "region": str(slot.get("region") or ""),
        "days": tuple(str(x) for x in (slot.get("days") or ())),
        "origin": str(slot.get("origin") or "genshin-db"),
    }


def craft_tier(material):
    """这个材料在第几档（1 = 最低档的绿色素材），连带等效绿素材个数。

    数据来自 genshin-db 的合成配方链（3 低换 1 高）。算"还要刷几天"要用它把
    「指引 63 个」折成「189 个绿素材」，才能套体力产出比例。
    """
    name = _material_name(material)
    if not name:
        return None
    slot = (knowledge().get("craft_tiers") or {}).get(name)
    if not slot:
        return None
    return {
        "material": name,
        "tier": int(slot.get("tier") or 1),
        "green": int(slot.get("green") or 1),
        "filter_text": str(slot.get("filter_text") or ""),
    }


def cross_check():
    """构建时与 genshin-db 的交叉校验结果（诊断用）。"""
    return dict(knowledge().get("cross_check") or {})


def knowledge_slot(material):
    """知识表里这个材料的**原始条目**（channel / source_text / type）。

    给"写理由文案"用：有些材料没有族群路线（天赋书出自秘境、智识之冕是限时活动），
    但知识表里写着它的出处 —— 与其跟玩家说"找不到获取方式"，不如把出处说出来。

    查两处：`materials`（有族群的）和 `unmatched`（**没有**族群但知道出处的，例如
    「坚忍」的教导 → 精通秘境：隐修）。分两张表只是因为构建时族群解析不了，
    出处信息本身是完整的。
    """
    name = _material_name(material)
    if not name:
        return None
    data = knowledge()
    slot = (data.get("materials") or {}).get(name)
    if slot:
        return slot
    unmatched = _unmatched_index()
    if name in unmatched:
        return unmatched[name]
    return _override_slot(name)


def _unmatched_index():
    """`{材料名: 原始条目}` —— `unmatched` 列表的索引（按容器 id 缓存，别每次重建 4000 条）。"""
    global _UNMATCHED_INDEX, _UNMATCHED_SOURCE
    with _LOCK:
        data = knowledge()
        if _UNMATCHED_INDEX is not None and _UNMATCHED_SOURCE is data:
            return _UNMATCHED_INDEX
        index = {}
        for item in (data.get("unmatched") or ()):
            if not isinstance(item, dict):
                continue
            key = str(item.get("material") or "").strip()
            if key and key not in index:
                index[key] = {
                    "material": key,
                    "channel": item.get("channel") or "other",
                    "type": item.get("type") or "",
                    # 游戏自己的分类（"武器突破素材"/"角色天赋素材"…）：估算天数要用
                    "game_type": item.get("game_type") or "",
                    "source_text": item.get("source_text") or "",
                    "origin": data.get("generated_from", {}).get("mapping") or "mapping.json",
                }
        _UNMATCHED_INDEX = index
        _UNMATCHED_SOURCE = data
        return index


def get_family_materials(family_id):
    """这一族里所有档位的材料名（按档位从低到高）。"""
    family = family_by_id(family_id)
    return family.material_names() if family else []


def siblings(material):
    """同族的**全部**档位材料（含自己）。界面上要告诉玩家"刷这一族能一起补"。"""
    family = resolve_material_family(material)
    return family.material_names() if family else []


# ==========================================
# 🌟 族群 → BetterGI 路线池（§23）
# ==========================================

def route_name_of(material):
    """这个材料该按哪个**族群目录**去刷（空串 = 没有可跑的族群路线）。"""
    family = resolve_material_family(material)
    return family.route if family else ""


def route_available(route_name):
    """BetterGI **现在**真的有这个族群目录吗。

    知识表里的路线是**构建时的快照**，而路线库会变（玩家删过、更新过脚本仓库）。
    所以每次用之前都要问一遍本机目录 —— 但**目录整个读不到**时返回 True：
    那种情况下"查不到"说明不了任何事，一律判 False 会把所有任务变成"缺少路线"。
    """
    wanted = str(route_name or "").strip()
    if not wanted:
        return False
    try:
        from skills import gather_cooldown
        names = set(gather_cooldown.enemy_route_names() or ())
    except Exception as exc:                # noqa: BLE001
        print(f"⚠️ 读取 BetterGI 魔物目录失败（{type(exc).__name__}）：{exc}")
        return True
    if not names:
        return True
    return wanted in names


def get_routes_by_family(family_id, contains=None):
    """这一族在 BetterGI 里**具体有哪些路线文件**（§23 的 Route Pool）。

    `contains` 是路线文件名的筛选词（例如 `"原海异种"`），给界面展示用。
    """
    route = str(family_id or "").strip()
    if not route:
        return []
    family = family_by_id(family_id)
    if family and family.route:
        route = family.route
    try:
        from skills import gather_cooldown
        index = gather_cooldown.route_material_index() or {}
    except Exception as exc:                # noqa: BLE001
        print(f"⚠️ 读取 BetterGI 路线索引失败（{type(exc).__name__}）：{exc}")
        return []
    needle = str(contains or "").strip()
    out = []
    for route_file, material in index.items():
        names = {str(material or "").strip(), str(route_file or "")}
        if route in names or (needle and any(needle in item for item in names)):
            out.append(str(route_file))
    return sorted(out)


def route_pool_size(material_or_family):
    """这个族有几条路线可跑（先问本机，问不到就用知识表里的快照数字）。"""
    family = None
    if isinstance(material_or_family, MaterialFamily):
        family = material_or_family
    elif str(material_or_family or "") in (knowledge().get("families") or {}):
        family = family_by_id(material_or_family)
    else:
        family = resolve_material_family(material_or_family)
    if not family or not family.route:
        return 0
    live = get_routes_by_family(family.id)
    return len(live) if live else family.route_files


# ==========================================
# 🌟 给规划器用：把一批材料按族分组
# ==========================================

def group_materials(materials):
    """把一批材料名按族分组，用于**合并同一个族的缺口**（§6 的核心诉求）。

    返回 `[{"family": MaterialFamily|None, "key": 分组键, "materials": [名字, …]}, …]`。
    认不出族的材料各自单独一组（`key` 就是材料名），行为与以前完全一致。
    """
    groups = {}
    order = []
    for material in materials or ():
        name = _material_name(material)
        if not name:
            continue
        family = resolve_material_family(name)
        key = f"family:{family.id}" if family and family.is_enemy_drop else f"material:{name}"
        if key not in groups:
            groups[key] = {"family": family, "key": key, "materials": []}
            order.append(key)
        groups[key]["materials"].append(name)
    return [groups[key] for key in order]


def describe(material):
    """一句人话："「异色结晶石」属于「原海异种」族（第 3 档），同族还有 …"。"""
    family = resolve_material_family(material)
    if not family:
        return ""
    name = _material_name(material)
    rank = family.rank_of(name)
    tail = ""
    others = [item for item in family.material_names() if item != name]
    if others:
        tail = f"，同族还有 {'、'.join(others)}"
    where = f"，走 BetterGI「{family.route}」路线" if family.route else ""
    return f"「{name}」属于「{family.name}」族（第 {rank} 档）{tail}{where}"
