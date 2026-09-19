"""预计培养时间：把材料缺口换算成"还要刷几天"。

**换算口径（玩家给定，见需求）**——每 1 点体力能换到：

| 来源 | 每点体力产出 | 说明 |
| --- | --- | --- |
| 天赋秘境 | 0.5135 | **等效绿色**天赋素材（所以要先把指引 / 哲学折成绿） |
| 武器突破秘境 | 0.5090 | 等效绿色武器突破素材（断牙 / 裂齿也要折成绿） |
| 经验地脉「启示之花」 | 0.3235 本 | 大英雄的经验（其它经验书按经验值折算） |
| 摩拉地脉「藏金之花」 | 3050 摩拉 | 世界 9 |
| 世界 9 首领 | 0.0765 个 | 角色突破材料 |

体力恢复 1 点 / **8 分钟** → 一天 180 点。

**为什么要算这个**：规划给出"缺 62 个幻造晶鳞石"这种数字，玩家其实想知道的是
"这要刷几天"。有了它，界面上就能直接说"预计 3.4 天"，而不是让人自己心算。

⚠️ 这是**参考天数**，不是承诺：
  · 产出比例是玩家实测给的数，不是官方公示值；
  · 秘境产出随世界等级浮动、还有周本 / 活动 / 合成台掺杂；
  · **采集物 / 普通魔物掉落不耗体力**，所以它们不参与天数（但界面会单独说明"这些不占体力"）；
  · 秘境只在特定星期开放 → 实际天数会**大于**按体力算出来的天数。
"""

import math

import config

from brain import material_family

# ---- 产出比例（每 1 点体力）----
RATE_TALENT_DOMAIN = 0.5135         # 等效绿色天赋素材
RATE_WEAPON_DOMAIN = 0.5090         # 等效绿色武器突破素材
RATE_EXP_LEYLINE = 0.3235           # 本「大英雄的经验」
RATE_MORA_LEYLINE = 3050.0          # 摩拉（世界 9）
RATE_WORLD_BOSS = 0.0765            # 角色突破材料（世界 9）

# 体力：1 点 / 8 分钟，一天 180 点
RESIN_PER_DAY = 24 * 60 * 60 // (8 * 60)

# 经验书折算成「大英雄的经验」（按经验值 20000 : 5000 : 1000）
EXP_BOOK_EQUIVALENT = {
    "大英雄的经验": 1.0,
    "冒险家的经验": 0.25,
    "流浪者的经验": 0.05,
}

# 归类用的关键字（游戏自己的 `game_type` 字段，比名字可靠）
TALENT_TYPES = ("角色天赋素材", "天赋素材")
WEAPON_TYPES = ("武器突破素材",)
EXP_TYPES = ("角色经验素材", "经验素材")
MORA_NAMES = ("摩拉",)


def resin_per_day():
    """一天能回多少体力（默认 180；`.env` 的 `RESIN_PER_DAY` 可以覆盖）。"""
    try:
        value = int(getattr(config, "RESIN_PER_DAY", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else RESIN_PER_DAY


# 天赋书档位：**从名字就能定**，不依赖任何数据版本。
# 「坚忍」「慈爱」这些比 genshin-db 还新的系列，合成表里没有，但后缀一定在。
TALENT_SUFFIX_GREEN = {"的教导": 1, "的指引": 3, "的哲学": 9}


def green_equivalent(item_name, count=None, unknown=None):
    """这个材料折算成几个**绿色（最低档）素材**。

    三档回退，越靠前越可信：
      1. `craft_tiers`（genshin-db 的合成配方链）—— 最权威，武器素材只能靠它；
      2. 天赋书名字后缀（教导 / 指引 / 哲学 = 1 / 3 / 9）—— 新系列也能定，永远可靠；
      3. 都认不出就当它是**最低档**（green=1），并把名字记进 `unknown`
         —— 宁可低估天数，也不要拿一个虚高的档位去吓玩家。
    """
    name = str(item_name or "").strip()
    if not name:
        return 1.0
    green = None
    try:
        tier = material_family.craft_tier(name)
    except Exception:                       # noqa: BLE001 —— 知识表缺失不该让估算崩
        tier = None
    if tier:
        green = float(tier.get("green") or 0) or None
    if green is None:
        for suffix, value in TALENT_SUFFIX_GREEN.items():
            if name.endswith(suffix):
                green = float(value)
                break
    if green is None:
        green = 1.0
        if unknown is not None:
            unknown.append(name)
    if count is None:
        return green
    return green * float(count or 0)


def exp_book_equivalent(item_name, count=None):
    """经验书折算成几本「大英雄的经验」。"""
    name = str(item_name or "").strip()
    factor = EXP_BOOK_EQUIVALENT.get(name)
    if factor is None:
        # 名字里带"经验"但不在表里（新书）：按一本大英雄算，宁可高估也不漏掉
        factor = 1.0 if "经验" in name else 0.0
    if count is None:
        return factor
    return factor * float(count or 0)


def classify(item_name, game_type="", family_type="", channel=""):
    """这个材料属于哪条**体力线**。返回 `(kind, label)`；不耗体力给 `("free", …)`。"""
    name = str(item_name or "").strip()
    game = str(game_type or "")
    if name in MORA_NAMES or game in ("通用货币",):
        return "mora_leyline", "摩拉地脉「藏金之花」"
    if any(word in game for word in TALENT_TYPES):
        return "talent_domain", "天赋秘境"
    if any(word in game for word in WEAPON_TYPES):
        return "weapon_domain", "武器突破秘境"
    if any(word in game for word in EXP_TYPES) or name in EXP_BOOK_EQUIVALENT:
        return "exp_leyline", "经验地脉「启示之花」"
    if str(family_type or "") == "world_boss" or str(channel or "") == "worldboss":
        return "boss", "世界首领"
    if str(family_type or "") in ("weekly_boss",):
        return "weekly_boss", "周本（每周一次，不按体力算）"
    if str(family_type or "") in ("enemy_drop",) or str(channel or "") in ("mob", "local"):
        return "free", "采集 / 怪物掉落（不耗体力）"
    return "unknown", "没认出来源"


def rate_for(kind):
    """这一条线每点体力的产出（折算成它自己的单位）。"""
    return {
        "talent_domain": RATE_TALENT_DOMAIN,
        "weapon_domain": RATE_WEAPON_DOMAIN,
        "exp_leyline": RATE_EXP_LEYLINE,
        "mora_leyline": RATE_MORA_LEYLINE,
        "boss": RATE_WORLD_BOSS,
    }.get(kind)


def amount_in_units(item_name, count, kind, unknown=None):
    """把缺口数量换成那条件线的计价单位（绿素材 / 大英雄本 / 摩拉 / 突破材料）。"""
    if kind == "talent_domain":
        return green_equivalent(item_name, count, unknown=unknown)
    if kind == "weapon_domain":
        return green_equivalent(item_name, count, unknown=unknown)
    if kind == "exp_leyline":
        return exp_book_equivalent(item_name, count)
    return float(count or 0)


def resin_for(item_name, count, kind=None, game_type="", family_type="", channel="", unknown=None):
    """这个缺口要花多少体力（不耗体力的线返回 0，认不出返回 None）。"""
    if kind is None:
        kind, _label = classify(item_name, game_type, family_type, channel)
    rate = rate_for(kind)
    if not rate:
        return 0.0 if kind in ("free", "weekly_boss") else None
    units = amount_in_units(item_name, count, kind, unknown=unknown)
    return units / rate


def estimate(rows, resin_per_day_value=None, breakdown_limit=6):
    """把"阶段行"里的缺口换算成参考天数。

    参数 `rows` 是 `material_planner.plan_requirements()` 出来的阶段行
    （每行有 `materials: [{item_name, missing, category, …}]`）。

    返回：
        {
          "days": 3.4,                 # 参考天数（按体力算）
          "total_resin": 612,          # 一共要多少体力
          "resin_per_day": 180,
          "lines": [{kind, label, resin, units, unit_label, materials: [...]}, …],
          "free_materials": [...],     # 不耗体力的材料（采集 / 魔物掉落）
          "unknown": [...],            # 认不出来源的（如实列出，不并入天数）
          "text": "预计还要约 3.4 天（体力 612，按每天 180 算）",
        }
    """
    per_day = int(resin_per_day_value or resin_per_day())
    buckets = {}
    free, unknown = [], []
    unknown_tier = []
    # 按角色分开记账：玩家问过"24.1 天是只算奥黛塔还是所有角色合计"，
    # 所以除了总数，还要能报出"每个角色各占多少"。
    per_character = {}
    for row in rows or ():
        if row.get("skipped"):
            continue
        character = str(row.get("character_name") or row.get("character_id") or "—")
        for material in row.get("materials") or ():
            missing = int(material.get("missing") or 0)
            if missing <= 0:
                continue
            name = str(material.get("item_name") or material.get("name") or "").strip()
            slot = {}
            try:
                slot = material_family.knowledge_slot(name) or {}
            except Exception:               # noqa: BLE001
                slot = {}
            kind, label = classify(
                name,
                game_type=slot.get("game_type") or "",
                family_type=material.get("family_type") or slot.get("type") or "",
                channel=slot.get("channel") or "",
            )
            if kind in ("free", "weekly_boss"):
                free.append({"material": name, "missing": missing, "label": label})
                continue
            resin = resin_for(name, missing, kind=kind, unknown=unknown_tier)
            if resin is None:
                unknown.append({"material": name, "missing": missing})
                continue
            bucket = buckets.setdefault(kind, {"kind": kind, "label": label, "resin": 0.0,
                                               "units": 0.0, "materials": []})
            units = amount_in_units(name, missing, kind)
            bucket["resin"] += resin
            bucket["units"] += units
            bucket["materials"].append({
                "material": name, "missing": missing, "units": round(units, 1), "resin": round(resin),
            })
            book = per_character.setdefault(character, {"character": character, "resin": 0.0})
            book["resin"] += resin

    lines = sorted(buckets.values(), key=lambda item: -item["resin"])
    # ⚠️ 顺序要紧：**先**把未取整的数留一份（`resin_exact`）再取整显示。
    #    天数统一从 exact 算 —— 逐项向上取整后再算，会让"总计"和"每人分摊之和"
    #    差出零点几天（玩家一眼就能看出对不上）。
    total_resin_exact = sum(line["resin"] for line in lines)
    for line in lines:
        line["resin_exact"] = line["resin"]
        line["resin"] = int(math.ceil(line["resin"]))
        line["units"] = round(line["units"], 1)
        line["unit_label"] = {
            "talent_domain": "等效绿天赋素材",
            "weapon_domain": "等效绿武器素材",
            "exp_leyline": "本大英雄的经验",
            "mora_leyline": "摩拉",
            "boss": "个突破材料",
        }.get(line["kind"], "")
        line["materials"].sort(key=lambda item: -item["resin"])
        line["materials"] = line["materials"][:breakdown_limit]

    total_resin = sum(line["resin"] for line in lines)
    days = round(total_resin_exact / per_day, 1) if per_day else 0.0
    characters = sorted(per_character.values(), key=lambda item: -item["resin"])
    for item in characters:
        item["resin_exact"] = item["resin"]
        item["resin"] = int(math.ceil(item["resin"]))
        item["days"] = round(item["resin_exact"] / per_day, 1) if per_day else 0.0
        # 占比用**未取整**的体力算：每人天数各自四舍五入之后再加，会和总计对不上
        # （0.97 天显示成 1.0，两个人就是 2.0 vs 总计 1.9）。占比不会有这个问题。
        item["share"] = int(round(item["resin_exact"] / total_resin_exact * 100)) \
            if total_resin_exact else 0
    who = f"全部 {len(characters)} 个角色合计" if len(characters) > 1 else (
        characters[0]["character"] if characters else "当前目标")
    text = (f"{who}预计还要约 {days} 天（要 {total_resin:,} 体力，按每天 {per_day} 体力折算）"
            if total_resin else "按当前缺口不需要额外体力")
    if free:
        text += f"；另有 {len(free)} 种采集 / 怪物掉落材料不占体力"
    if unknown:
        text += f"；{len(unknown)} 种材料认不出来源，没算进天数"
    if unknown_tier:
        text += f"；{len(set(unknown_tier))} 种材料档位未知，按最低档折算（天数可能偏低）"
    return {
        "days": days,
        "total_resin": total_resin,
        "resin_per_day": per_day,
        "regen_minutes": 8,
        "characters": characters,
        "scope": who,
        "lines": lines,
        "free_materials": free,
        "unknown": unknown,
        "unknown_tier": sorted(set(unknown_tier)),
        "text": text,
        "note": "参考天数：**算的是所有启用的角色合计**（不只是当前这一个）；"
                "产出比例按玩家实测（天赋秘境 0.5135 / 武器秘境 0.5090 / "
                "经验地脉 0.3235 本 / 摩拉 3050 / 首领 0.0765，每点体力）；"
                "秘境只在特定星期开放，实际天数通常更长。",
    }


def describe(estimate_result, limit=4, characters=5):
    """给界面 / 推送用的一行行文字。"""
    data = estimate_result or {}
    if not data.get("total_resin"):
        return [data.get("text") or "按当前缺口不需要额外体力"]
    lines = [f"⏳ {data['text']}"]
    for line in (data.get("lines") or [])[:limit]:
        lines.append(f"   · {line['label']}：{line['resin']:,} 体力"
                     f"（约 {line['units']:,} {line['unit_label']}）")
    if len(data.get("lines") or []) > limit:
        lines.append(f"   · 还有 {len(data['lines']) - limit} 条体力线未列出")
    # 每个角色各占多少（玩家问过"这 24 天是只算一个人还是全部"）
    roster = data.get("characters") or []
    if len(roster) > 1:
        parts = [f"{item['character']} {item['share']}%" for item in roster[:characters]]
        rest = len(roster) - characters
        lines.append("   · 占比：" + "、".join(parts) + (f"，另 {rest} 人略" if rest > 0 else ""))
    return lines
