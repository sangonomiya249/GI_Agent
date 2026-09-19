"""构建「材料族」知识表：材料 → 族群 → BetterGI 路线池（架构文档 §6/§7/§11/§22）。

**为什么必须有这一层**：米游社算出来的缺口是**具体材料**（异色结晶石），
而 BetterGI 的路线是按**族群**组织的（原海异种）。两边词表根本不同 ——
实测本机 `material_category_index()` 里有 `原海异种`，却**没有** `异色结晶石`。
中间这层以前不存在，于是"缺异色结晶石"永远落不到路线上（WAITING_ROUTE），
而同族的低档材料反而有路线。补上这一层，就能：
    异海凝珠 / 异海之块 / 异色结晶石 → 原海异种材料族 → 原海异种 → 54 条路线文件

**数据来源**（按 §22 数据覆盖原则，从低到高）：
  1. `mapping.json` —— TeyvatGuide WIKI：4254 种材料的获取途径文本，含"60级以上原海异种掉落"
  2. `memory/game_dict_baike_full.json` —— 百科字典，补 mapping.json 没收录的材料
  3. `memory/boss_drops_dict.json` —— 首领 → 掉落清单，用来校验首领 / 周本族
  4. `skills.gather_cooldown` —— 本机 BetterGI 路线库：族群 ↔ 路线文件
  5. `config/knowledge_overrides.json` —— 人工修正（最高优先级，见 §22）

**不改原始数据**（§22 明确要求）：本脚本只读上面几个文件，产物单独写到
`memory/game_knowledge/material_families.json`。

用法：
    venv\\Scripts\\python.exe -B -X utf8 scripts/build_material_families.py          # 构建
    venv\\Scripts\\python.exe -B -X utf8 scripts/build_material_families.py --report # 只看覆盖率
"""

import argparse
import datetime
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 数据文件都放在 `memory/game_knowledge/`（**不再堆在项目根目录**）。
# `LEGACY_*` 是旧位置：有人（或旧脚本）把它生成回根目录时也还能读到，不至于直接报错。
KNOWLEDGE_DIR = os.path.join(ROOT, "memory", "game_knowledge")
MAPPING_PATH = os.path.join(KNOWLEDGE_DIR, "mapping.json")
LEGACY_MAPPING_PATH = os.path.join(ROOT, "mapping.json")
BAIKE_PATH = os.path.join(ROOT, "memory", "game_dict_baike_full.json")
BOSS_PATH = os.path.join(ROOT, "memory", "boss_drops_dict.json")
OVERRIDE_PATH = os.path.join(ROOT, "config", "knowledge_overrides.json")
GDB_PATH = os.path.join(KNOWLEDGE_DIR, "genshin_db_export.json")
OUT_PATH = os.path.join(KNOWLEDGE_DIR, "material_families.json")

# 材料名可能带前缀，例如百科字典里的「升级材料 「诗文」的教导」
NAME_PREFIX_RE = re.compile(r"^(?:升级材料|突破材料|天赋材料|养成材料)\s*")
# 【40级以上】原海异种掉落； / 60级以上原海异种掉落 / 70级以上多托雷挑战奖励
SOURCE_RE = re.compile(
    r"^(?:【\s*(?P<lv1>\d+)\s*级以上\s*】|(?P<lv2>\d+)\s*级以上)?\s*"
    r"(?P<fam>[^；;，,。:：|]{2,20}?)\s*"
    r"(?P<verb>掉落|挑战奖励|讨伐奖励)\s*[；;。]?$"
)


# ==========================================
# 🌟 材料名 / 来源行 归一化
# ==========================================

def clean_name(name):
    """去掉材料名上的前缀噪音（百科字典的键里混着"升级材料 "这种修饰）。"""
    return NAME_PREFIX_RE.sub("", str(name or "")).strip()


def parse_source_line(line):
    """把一条来源文本解析成 `(族群名, 最低等级, 原文)`；解析不出给 `None`。

    ⚠️ 必须**整行锚定**：来源文本里混着大量玩家吐槽（"掉落几率很小吧，但是可以用破损的
    去合成吧？"）和攻略（"幼岩龙蜥<br>位置：璃月…掉落：不"）。只有整行就是
    "【N级以上】XX掉落" 这种形式的才可信，子串匹配会把吐槽当成族群名。
    """
    text = str(line or "").strip()
    if not text:
        return None
    hit = SOURCE_RE.match(text)
    if not hit:
        return None
    family = LEVEL_PREFIX_RE.sub("", hit.group("fam").strip())
    if not is_plausible_family(family):
        return None
    level = int(hit.group("lv1") or hit.group("lv2") or 0)
    return family, level, text


LEVEL_PREFIX_RE = re.compile(r"^\d+\s*级")

# 这些不是"族群"，是数据里的杂项：秘境名（天赋书走秘境）、世界等级、地区名，
# 以及玩家吐槽被整行锚定漏进来的情况。
JUNK_FAMILY_RE = re.compile(
    r"[*？?，,、（）()【】]|秘境|世界等级|征讨领域|获得途径|全素材|地区「|随机"
)
JUNK_FAMILIES = {"动物", "纳塔", "怪物", "掉落", "怪物掉落", "材料", "魔物", "敌人"}


def is_plausible_family(name):
    """这个字符串像不像一个真的族群名。"""
    text = str(name or "").strip()
    if not text or len(text) > 12:
        return False
    if text in JUNK_FAMILIES or JUNK_FAMILY_RE.search(text):
        return False
    return True


# ==========================================
# 🌟 族群名 → BetterGI 路线词表
# ==========================================

# 别名：数据里的写法 → 本机 BetterGI「敌人与魔物」目录里的目录名。
# 每条都得有理由，不能凭感觉写（§33：不猜路线）。
ROUTE_ALIASES = {
    "愚人众": "愚人众先遣队",            # 徽记系由愚人众通用掉落，先遣队是最常见的愚人众路线组
    "愚人众·萤术士": "萤术士",
    "萤术士": "萤术士",
    "愚人众·役人": "愚人众风役人",
    "愚人众·藏镜仕女": "愚人众风役人",
    "役人": "愚人众风役人",
    "先遣队": "愚人众先遣队",
    "兽境群狼": "兽境之狼",
    "兽境猎犬": "兽境之狼",
    "丘丘暴徒": "丘丘王",                # 暴徒（大丘丘）与丘丘王同组路线
    "丘丘王": "丘丘王",
    "丘丘人": "丘丘人",
    "深渊法师": "深渊法师",
    "深渊使徒": "深渊法师",
    "深渊咏者": "深渊法师",
    "遗迹守卫": "遗迹守卫",
    "遗迹重机": "遗迹守卫",
    "遗迹猎者": "遗迹守卫",
    "遗迹机兵": "遗迹机兵",
    "盗宝团": "盗宝团",
    "蕈兽": "蕈兽",
    "龙蜥": "龙蜥",
    "小型深海龙蜥": "小型深海龙蜥",
    "飘浮灵": "飘浮灵",
    "史莱姆": "史莱姆",
    "骗骗花": "骗骗花",
    "野伏众": "野伏众",
    "镀金旅团": "镀金旅团",
    "黑蛇众": "黑蛇众",
    "圣骸兽": "圣骸兽",
    "原海异种": "原海异种",
    "隙境原体": "隙境原体",
    "发条机关": "发条机关",
    "元能构装体": "元能构装体",
    "巡陆艇": "巡陆艇",
    "浊水幻灵": "浊水幻灵",
    "大灵显化身": "大灵显化身",
    "深黯钓客": "深黯钓客",
    "霜夜灵嗣": "霜夜灵嗣",
    "蕴光异兽": "蕴光异兽",
    "荒野狂猎": "荒野狂猎",
    "纳塔龙众": "纳塔龙众",
    "部族龙形武士": "部族龙形武士",
    "丘丘游侠": "丘丘游侠",
    "丘丘萨满": "丘丘萨满",
    "丘丘射手": "丘丘人射手",            # BetterGI 的目录名是「丘丘人射手」，不是「丘丘射手」
    "丘丘人射手": "丘丘人射手",
    "愚人众·先遣队": "愚人众先遣队",
    "愚人众特辖队": "愚人众特辖队",
    "异种合成魔兽": "异种合成魔兽",
    "肌生晶石的妖精": "肌生晶石的妖精",
    "雷火大蕈": "雷火大蕈",
    "秘源机兵": "秘源机兵",
    "魔像禁卫": "魔像禁卫",
    "熔岩游像": "熔岩游像",
    "炉壳山鼬": "炉壳山鼬",
    "荒野树妖": "荒野树妖",
    "玄文兽": "玄文兽",
    "辖域守护者": "辖域守护者",
    "遗迹龙兽": "遗迹龙兽",
    "飞萤": "飞萤",
    "奇怪的丘丘人": "奇怪的丘丘人",
    "债务处理人": "债务处理人",
    "冬国仕女": "冬国仕女",
}

# 首领 / 周本的写法 → 统一名（这些是「一个 Boss」而不是"族群"）
BOSS_SUFFIX_RE = re.compile(r"^(?:\d+级)?(?:以上)?(.+?)(?:挑战奖励|掉落)?$")


def route_vocabulary():
    """本机 BetterGI「敌人与魔物」目录下的族群名（读不到就给空集合，不影响构建）。"""
    try:
        from skills import gather_cooldown
        return set(gather_cooldown.enemy_route_names() or ())
    except Exception as exc:                # noqa: BLE001 —— 路线库缺失不该让构建失败
        print(f"⚠️ 读不到 BetterGI 魔物目录（{type(exc).__name__}: {exc}），族群将不带路线")
        return set()


def route_files_by_family():
    """`{族群名: [路线文件名, …]}` —— 本机路线库里每个族群有多少条可跑的路线。"""
    out = {}
    try:
        from skills import gather_cooldown
        index = gather_cooldown.route_material_index() or {}
    except Exception:                       # noqa: BLE001
        return out
    for route_file, material in index.items():
        name = str(material or "").strip()
        if name:
            out.setdefault(name, []).append(str(route_file))
    return out


def match_route(family, vocabulary, files):
    """族群名 → `(路线名, 路线条数, 说明)`。认不出给 `("", 0, 原因)`。"""
    name = str(family or "").strip()
    if not name:
        return "", 0, "族群名为空"
    candidates = []
    alias = ROUTE_ALIASES.get(name)
    if alias and alias != name:
        candidates.append((alias, f"别名映射「{name}」→「{alias}」"))
    candidates.append((name, "族群名直接命中"))
    # 去掉后缀再试一次（"愚人众特辖队分队" 这种写法）
    for suffix in ("掉落", "的分队", "众"):
        if name.endswith(suffix) and len(name) > len(suffix):
            candidates.append((name[: -len(suffix)], f"去掉后缀「{suffix}」后命中"))
    for candidate, why in candidates:
        if candidate in vocabulary:
            return candidate, len(files.get(candidate) or ()), why
    # 词表里的名字是数据里名字的子串（或反过来）也算命中，例如 "活化状态下蕈兽" → "蕈兽"
    for vocab_name in sorted(vocabulary, key=len, reverse=True):
        if vocab_name and (vocab_name in name or name in vocab_name):
            return vocab_name, len(files.get(vocab_name) or ()), f"与路线组「{vocab_name}」名称包含"
    return "", 0, "BetterGI 路线库里没有对应的族群目录"


# ==========================================
# 🌟 三份数据源 → 材料 → 族群
# ==========================================

CHANNEL_TYPES = {
    "mob": "enemy_drop",
    "worldboss": "world_boss",
    "weeklyboss": "weekly_boss",
    "talent": "talent_book",
    "local": "specialty",
    "other": "other",
}


# 记录实际读到的数据文件路径（写进产物的 `generated_from`，方便排查"到底读了哪一份"）
_SOURCES = {}


def load_mapping_families():
    """① mapping.json（TeyvatGuide WIKI）：材料 → 通道 + 来源文本。"""
    path = MAPPING_PATH
    if not os.path.exists(path) and os.path.exists(LEGACY_MAPPING_PATH):
        # 旧位置（项目根目录）兼容：有人把它生成回根目录时不该直接报"没有这个文件"
        print(f"ℹ️ 用的是旧位置的 mapping.json（{os.path.relpath(LEGACY_MAPPING_PATH, ROOT)}）；"
              f"建议挪到 {os.path.relpath(MAPPING_PATH, ROOT)}")
        path = LEGACY_MAPPING_PATH
    if not os.path.exists(path):
        print(f"⚠️ 没有 {os.path.relpath(MAPPING_PATH, ROOT)}，跳过这一路数据源")
        return {}
    _SOURCES["mapping"] = path
    with io.open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    channels = data.get("material_channels") or {}
    result = {}
    for raw_name, info in channels.items():
        name = clean_name(raw_name)
        if not name or not isinstance(info, dict):
            continue
        channel = str(info.get("channel") or "other")
        source = str(info.get("source") or "").strip()
        entry = {
            "material": name,
            "channel": channel,
            "type": CHANNEL_TYPES.get(channel, "other"),
            "material_type": str(info.get("type") or ""),
            "source_text": source,
            "origin": "mapping.json",
        }
        parsed = None
        for piece in re.split(r"[|｜]", source):
            parsed = parse_source_line(piece.strip())
            if parsed:
                break
        if parsed:
            entry["family"] = parsed[0]
            entry["min_level"] = parsed[1]
            entry["source_line"] = parsed[2]
        result[name] = entry
    return result


def load_baike_families():
    """② 百科字典：`ascension_material_sources` / `talent_materials` 里的来源行。

    只在 mapping.json 没收录这个材料时用来补漏（字典比 mapping 旧，
    缺少至冬/挪德卡莱的新材料）。
    """
    if not os.path.exists(BAIKE_PATH):
        return {}
    with io.open(BAIKE_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    out = {}
    for group in ("avatars", "weapons"):
        for node in (data.get(group) or {}).values():
            if not isinstance(node, dict):
                continue
            for key, value in node.items():
                if not (key.endswith("_material_sources") or key == "talent_materials"):
                    continue
                if not isinstance(value, dict):
                    continue
                for raw_name, info in value.items():
                    name = clean_name(raw_name)
                    if not name:
                        continue
                    lines = []
                    if isinstance(info, dict):
                        lines = [x for x in (info.get("source_lines") or []) if isinstance(x, str)]
                        if isinstance(info.get("schedule"), str):
                            lines.append(info["schedule"])
                    elif isinstance(info, list):
                        lines = [x for x in info if isinstance(x, str)]
                    for line in lines:
                        parsed = parse_source_line(line)
                        if not parsed:
                            continue
                        slot = out.setdefault(name, {"material": name, "candidates": [],
                                                     "origin": "game_dict_baike_full.json"})
                        slot["candidates"].append({"family": parsed[0], "min_level": parsed[1],
                                                   "source_line": parsed[2]})
    return out


def load_boss_drops():
    """③ 首领 → 掉落清单（校验用：一个材料若出现在某首领的掉落里，说明它有首领来源）。"""
    if not os.path.exists(BOSS_PATH):
        return {}
    with io.open(BOSS_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def load_overrides():
    """⑤ 人工修正（`config/knowledge_overrides.json`，§22）。"""
    if not os.path.exists(OVERRIDE_PATH):
        return {}
    try:
        # `utf-8-sig`：Windows 上用记事本 / PowerShell 存过的文件会带 BOM，
        # 严格 utf-8 读会抛 JSONDecodeError（踩过一次）。
        with io.open(OVERRIDE_PATH, encoding="utf-8-sig") as handle:
            return json.load(handle)
    except Exception as exc:                # noqa: BLE001
        print(f"⚠️ knowledge_overrides.json 读不了（{type(exc).__name__}: {exc}），按没有处理")
        return {}


def build_craft_tiers(gdb):
    """材料档位链 → **等效绿色素材**数量（合成配方 3 低换 1 高）。

    为什么要它：体力产出比例是按"等效绿素材"给的
    （天赋秘境 0.5135 绿天赋素材 / 体力、武器秘境 0.5090 绿武器素材 / 体力），
    可缺口里给的是"指引 63 个、断牙 12 个"这种**非最低档**的数量。
    换算成绿素材，才能套那个比例算"还要刷几天"。

    数据来源：genshin-db 的 `crafts` —— 实测配方就是 `指引 = 3× 教导`、
    `哲学 = 3× 指引`、`裂齿 = 3× 始龀`，所以沿配方链往上数层数就是档位。

    只跟**单一原料 × 3** 的配方（那是货真价实的档位升级）；那种"三种材料互相转化"
    的配方不跟（它们不是升级，跟了会把档位算飞）。
    """
    crafts = (gdb or {}).get("crafts") or {}
    up = {}
    for name, entry in crafts.items():
        if not isinstance(entry, dict):
            continue
        recipe = [r for r in (entry.get("recipe") or []) if isinstance(r, dict)]
        if len(recipe) != 1:
            continue
        ingredient = recipe[0]
        if int(ingredient.get("count") or 0) != 3:
            continue
        if not ingredient.get("name"):
            continue
        up[clean_name(name)] = (clean_name(ingredient["name"]), str(entry.get("filter_text") or ""))

    # 沿链往上推：基础档（没有升级配方指向它）= 1 绿
    def green_of(name, seen=None):
        seen = seen or set()
        if name in seen:
            return 1                       # 环形配方（不该有）：按基础档兜底，别死循环
        seen.add(name)
        lower = up.get(name)
        if not lower:
            return 1
        return 3 * green_of(lower[0], seen)

    out = {}
    names = set(up) | {item[0] for item in up.values()}
    for name in names:
        green = green_of(name)
        # 档位 = 往上乘了几次 3（green=1 → 第 1 档，3 → 第 2 档，9 → 第 3 档…）
        tier, walked = 1, 1
        while walked < green:
            walked *= 3
            tier += 1
        out[name] = {
            "material": name,
            "tier": tier,                  # 1 = 最低档（绿）
            "green": green,                # 等效绿素材个数
            "filter_text": (up.get(name) or ("", ""))[1],
            "origin": "genshin-db",
        }
    return out


def build_genshin_db(gdb):
    """genshin-db 导出里我们要用的三块：天赋书日程、族群成员怪物、档位链。"""
    return {
        "talent_domains": build_talent_domains(gdb),
        "craft_tiers": build_craft_tiers(gdb),
    }


def load_genshin_db():
    """genshin-db 的导出（`scripts/export_genshin_db.js` 生成）。

    ⚠️ 实测 v5.2.7（游戏数据 v6.2）**比 mapping.json 旧**：至冬/挪德卡莱的新材料
    （幻造晶鳞石、嵌合种、扭曲的枯枝…）它一个都没有。所以：
      · 族群**补漏**排在 mapping.json / 百科字典之后；
      · 但它有两块 mapping.json 没有的数据 —— 秘境 `daysOfWeek`（天赋书日程）
        和怪物掉落表（族群 → 成员怪物），这两块照用。
    """
    if not os.path.exists(GDB_PATH):
        return {}
    try:
        with io.open(GDB_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:                # noqa: BLE001
        print(f"⚠️ genshin-db 导出读不了（{type(exc).__name__}: {exc}），本次跳过")
        return {}
    return data if isinstance(data, dict) else {}


TALENT_BOOK_RE = re.compile(r"^「[^」]+」的(?:教导|指引|哲学)$")
STAGE_NUMBER_RE = re.compile(r"\s*[ⅠⅡⅢⅣIVX]+\s*$")


def build_talent_domains(gdb):
    """天赋书 → 秘境（含**开放星期**）。

    为什么用 genshin-db 这一份：本地百科字典的秘境索引里 21 个系列的「教导」档全缺，
    较新的系列（「坚忍」的精通秘境：隐修）整个没有。genshin-db 的 `domains` 里
    每个秘境条目都带 `daysOfWeek` 和奖励清单（哪些天赋书），是权威且完整的
    （v6.2 覆盖到的系列）。实测它与项目现有索引**一致**（箴铭 = 周二/五/日），
    也就是说它既能补缺，也能当校验。
    """
    domains = (gdb or {}).get("domains") or []
    out = {}
    for entry in domains:
        if not isinstance(entry, dict):
            continue
        # 导出脚本已经把「周二」剥成「二」，但这里再归一化一次 —— 手写 / 换导出器
        # 时不该因为多了个「周」字就整条丢掉日程。
        days = [str(x).replace("周", "").strip() for x in (entry.get("days") or [])]
        days = [x for x in days if x in "一二三四五六日"]
        if not days:
            continue                                     # 没日程的（圣遗物本等）不用
        stage = str(entry.get("stage") or "")
        base = STAGE_NUMBER_RE.sub("", stage)
        # "精通秘境：箴铭 IV" → 关卡名「箴铭」（前缀"精通秘境："单独去掉，
        # 否则理由里会写成"精通秘境「精通秘境：箴铭」"）。
        stage_name = base.split("：")[-1].split(":")[-1].strip() or base
        for reward in (entry.get("rewards") or []):
            name = clean_name(reward)
            if not TALENT_BOOK_RE.match(name):
                continue
            slot = out.setdefault(name, {
                "material": name,
                "stage": stage_name,
                "stage_full": base,
                "entrance": str(entry.get("entrance") or ""),
                "region": str(entry.get("region") or ""),
                "days": days,
                "domain_text": str(entry.get("domain_text") or ""),
                "origin": "genshin-db",
            })
            # 同一本书出现在 I~IV 里，日程与入口都一样；保留最高档的写法当展示名
            if len(str(entry.get("stage") or "")) > len(str(slot.get("stage_full") or "")):
                slot["stage_full"] = stage
    return out


def attach_enemy_families(families, materials, gdb):
    """族群 → 成员怪物（架构文档 §8 的 `enemy_families`）。

    数据来自 genshin-db 的 `enemies[*].rewardPreview`（实测 313 只里 291 只有掉落表，
    还带掉率）。它同时是**独立校验**：重甲蟹、膨膨兽掉的都是异海凝珠/之块/结晶石，
    正好验证了"这三档属于同一个族群"的推导。
    """
    enemies = (gdb or {}).get("enemies") or {}
    attached = 0
    for name, entry in enemies.items():
        if not isinstance(entry, dict):
            continue
        drops = [str(d.get("name") or "") for d in (entry.get("drops") or []) if d.get("name")]
        if not drops:
            continue
        by_family = {}
        for drop in drops:
            slot = materials.get(drop)
            if slot and slot.get("family"):
                by_family.setdefault(slot["family"], []).append(drop)
        for family_name, family_drops in by_family.items():
            family = families.get(family_name)
            if family is None:
                continue
            members = family.setdefault("enemies", [])
            if any(item["name"] == name for item in members):
                continue
            members.append({
                "name": str(name),
                "category_text": str(entry.get("category_text") or ""),
                "enemy_type": str(entry.get("enemy_type") or ""),
                "drops": sorted(set(family_drops)),
            })
            attached += 1
    for family in families.values():
        if family.get("enemies"):
            family["enemies"].sort(key=lambda item: (-len(item["drops"]), item["name"]))
            family["enemy_count"] = len(family["enemies"])
    return attached


def cross_check_families(materials, gdb):
    """拿 genshin-db 的 `materials.sources` 交叉校验材料族推导。

    为什么值得做：`sources` 是**另一条独立数据链**（GenshinData / wiki），
    跟 `mapping.json`（TeyvatGuide）不是同一个来源。两边都指向同一个族群，
    才说明这条推导站得住；不一致的要列出来人工看。
    """
    gdb_materials = (gdb or {}).get("materials") or {}
    agree, conflict, only_gdb = 0, [], []
    for name, entry in gdb_materials.items():
        if not isinstance(entry, dict):
            continue
        parsed = None
        for line in (entry.get("sources") or []):
            parsed = parse_source_line(str(line).strip())
            if parsed:
                break
        if not parsed:
            continue
        family = parsed[0]
        slot = materials.get(clean_name(name))
        if not slot:
            only_gdb.append({"material": name, "family": family, "min_level": parsed[1],
                             "source": parsed[2]})
            continue
        if slot.get("family") == family:
            agree += 1
        elif slot.get("family"):
            conflict.append({"material": name, "ours": slot["family"], "genshin_db": family,
                             "genshin_db_source": parsed[2]})
    return {"agree": agree, "conflict_count": len(conflict), "conflicts": conflict[:60],
            "only_in_genshin_db": only_gdb[:400], "only_in_genshin_db_count": len(only_gdb)}


# ==========================================
# 🌟 构建
# ==========================================

def build():
    vocabulary = route_vocabulary()
    files = route_files_by_family()
    mapping = load_mapping_families()
    baike = load_baike_families()
    bosses = load_boss_drops()
    overrides = load_overrides()
    gdb = load_genshin_db()

    bosses_by_material = {}
    for boss_name, drops in (bosses or {}).items():
        for drop in drops or ():
            bosses_by_material.setdefault(clean_name(drop), set()).add(str(boss_name))

    families = {}
    materials = {}
    unmatched = []
    stats = {"mapping_materials": len(mapping), "baike_only": 0, "genshin_db_only": 0,
             "with_family": 0, "with_route": 0}

    def add_member(family_name, family_type, channel, material, min_level, source_text):
        family = families.get(family_name)
        if family is None:
            route, route_count, why = match_route(family_name, vocabulary, files)
            family = {
                "family_id": family_name,      # 离线环境没有拼音库，用中文名做稳定 id
                "name": family_name,
                "type": family_type,
                "channel": channel,
                "route": route,
                "route_files": route_count,
                "route_note": why,
                "materials": [],
                "source_texts": [],
            }
            families[family_name] = family
        member = {"material": material, "min_level": int(min_level or 0)}
        if member not in family["materials"]:
            family["materials"].append(member)
        if source_text and source_text not in family["source_texts"]:
            family["source_texts"].append(source_text)
        return family

    for name, entry in mapping.items():
        channel = entry.get("channel") or "other"
        family_name = entry.get("family")
        if not family_name:
            # mapping.json 认出来了但文本解析不出族群（绝大多数是"其他"通道：摩拉、经验书、
            # 天赋书、特产 —— 这些由地脉花/秘境/采集路线单独处理，不需要族群）
            unmatched.append({"material": name, "channel": channel,
                              "source_text": entry.get("source_text") or "",
                              # 游戏自己的分类（"武器突破素材"/"角色天赋素材"/"角色经验素材"…）：
                              # 算"还要刷几天"时靠它决定套哪个体力产出比例。
                              "game_type": entry.get("material_type") or "",
                              "reason": "来源文本里没有「XX掉落」形式的族群"})
            continue
        family_type = entry.get("type") or "other"
        family = add_member(family_name, family_type, channel, name,
                            entry.get("min_level"), entry.get("source_line") or entry.get("source_text"))
        materials[name] = {
            "material": name,
            "family": family_name,
            "type": family_type,
            "channel": channel,
            "game_type": entry.get("material_type") or "",
            "min_level": int(entry.get("min_level") or 0),
            "source_text": entry.get("source_text") or "",
            "route": family["route"],
            "origin": entry.get("origin") or "mapping.json",
        }

    # 百科字典补漏：mapping.json 没有的材料
    for name, entry in baike.items():
        if name in materials or not entry.get("candidates"):
            continue
        # 多档材料在字典里会给同一个族群多次，取最低等级那档作为主族
        best = sorted(entry["candidates"], key=lambda item: (item["min_level"], len(item["family"])))[0]
        if best["family"] in ("怪物", "掉落", "怪物掉落"):
            continue                       # 泛指"怪物掉落"，不是真族群
        stats["baike_only"] += 1
        family = add_member(best["family"], "enemy_drop", "mob", name,
                            best["min_level"], best["source_line"])
        materials[name] = {
            "material": name,
            "family": best["family"],
            "type": "enemy_drop",
            "channel": "mob",
            "min_level": int(best["min_level"] or 0),
            "source_text": best["source_line"],
            "route": family["route"],
            "origin": entry.get("origin"),
        }

    # genshin-db 补漏（排在百科字典之后：它更权威但更旧，v6.2 之后的新材料它没有）
    gdb_materials = (gdb or {}).get("materials") or {}
    for name, entry in gdb_materials.items():
        name = clean_name(name)
        if not name or name in materials or not isinstance(entry, dict):
            continue
        parsed = None
        for line in (entry.get("sources") or []):
            parsed = parse_source_line(str(line).strip())
            if parsed:
                break
        if not parsed:
            continue
        stats["genshin_db_only"] += 1
        family = add_member(parsed[0], "enemy_drop", "mob", name, parsed[1], parsed[2])
        materials[name] = {
            "material": name,
            "family": parsed[0],
            "type": "enemy_drop",
            "channel": "mob",
            "min_level": int(parsed[1] or 0),
            "source_text": parsed[2],
            "route": family["route"],
            "origin": "genshin-db",
        }

    # 人工修正最高优先级：可以把材料塞进已有族，也可以直接指定路线
    for item in (overrides.get("material_family_overrides") or []):
        family_name = str(item.get("family_id") or item.get("family") or "").strip()
        if not family_name:
            continue
        for raw_name in (item.get("materials") or []):
            name = clean_name(raw_name)
            if not name:
                continue
            family = families.get(family_name)
            if family is None:
                route, route_count, why = match_route(family_name, vocabulary, files)
                family = {"family_id": family_name, "name": family_name,
                          "type": str(item.get("type") or "enemy_drop"),
                          "channel": str(item.get("channel") or "mob"),
                          "route": route, "route_files": route_count, "route_note": why,
                          "materials": [], "source_texts": ["人工修正"]}
                families[family_name] = family
            if not any(m["material"] == name for m in family["materials"]):
                family["materials"].append({"material": name, "min_level": 0})
            old = materials.get(name)
            if old and old.get("family") != family_name:
                old_family = families.get(old["family"])
                if old_family:
                    old_family["materials"] = [m for m in old_family["materials"]
                                               if m["material"] != name]
            materials[name] = {
                "material": name, "family": family_name,
                "type": family["type"], "channel": family["channel"],
                "min_level": int((old or {}).get("min_level") or 0),
                "source_text": (old or {}).get("source_text") or "人工修正",
                "route": family["route"], "origin": "knowledge_overrides.json",
            }

    # 档位排序：同一族里按最低等级排（0 → 40 → 60），就是游戏里的 1/2/3 档
    for family in families.values():
        family["materials"].sort(key=lambda item: (item["min_level"], item["material"]))
        for index, member in enumerate(family["materials"], start=1):
            member["rank"] = index
            slot = materials.get(member["material"])
            if slot:
                slot["rank"] = index
                slot["family_materials"] = [m["material"] for m in family["materials"]]
        family["material_count"] = len(family["materials"])
        family["boss_names"] = sorted(bosses_by_material.get(family["materials"][0]["material"], ())) \
            if family["materials"] else []

    # 统一补 `game_type`：genshin-db 的 `type_text` 是最全的一份
    # （"武器突破素材"/"角色天赋素材"/"角色经验素材"/"角色培养素材"…），
    # 算"还要刷几天"时靠它决定套哪个体力产出比例。
    gdb_type = {}
    for key, entry in (gdb.get("materials") or {}).items():
        if isinstance(entry, dict) and entry.get("type_text"):
            gdb_type[clean_name(key)] = str(entry["type_text"])
    filled = 0
    for name, slot in materials.items():
        if not slot.get("game_type") and gdb_type.get(name):
            slot["game_type"] = gdb_type[name]
            filled += 1
    for item in unmatched:
        if not item.get("game_type") and gdb_type.get(item.get("material")):
            item["game_type"] = gdb_type[item["material"]]
            filled += 1
    stats["game_type_filled"] = filled

    stats["with_family"] = len(materials)
    stats["with_route"] = sum(1 for slot in materials.values() if slot.get("route"))
    stats["families"] = len(families)
    stats["families_with_route"] = sum(1 for family in families.values() if family.get("route"))

    # genshin-db 的两块独有数据 + 交叉校验
    talent_domains = build_talent_domains(gdb)
    craft_tiers = build_craft_tiers(gdb)
    stats["talent_domains"] = len(talent_domains)
    stats["craft_tiers"] = len(craft_tiers)
    stats["enemies_attached"] = attach_enemy_families(families, materials, gdb)
    stats["families_with_enemies"] = sum(1 for f in families.values() if f.get("enemies"))
    check = cross_check_families(materials, gdb)
    stats["cross_check_agree"] = check["agree"]
    stats["cross_check_conflict"] = check["conflict_count"]

    knowledge = {
        "version": 1,
        "built_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_from": {
            "mapping": os.path.relpath(_SOURCES.get("mapping", MAPPING_PATH),
                                       ROOT).replace("\\", "/"),
            "baike": os.path.relpath(BAIKE_PATH, ROOT).replace("\\", "/"),
            "boss_drops": os.path.relpath(BOSS_PATH, ROOT).replace("\\", "/"),
            "overrides": os.path.relpath(OVERRIDE_PATH, ROOT).replace("\\", "/"),
            "genshin_db": os.path.relpath(GDB_PATH, ROOT).replace("\\", "/") if gdb else "",
            "genshin_db_version": (gdb or {}).get("package_version") or "",
            "genshin_db_data_version": (gdb or {}).get("data_version") or "",
            "bettergi_route_vocabulary": len(vocabulary),
        },
        "route_vocabulary": sorted(vocabulary),
        "families": families,
        "materials": materials,
        # 天赋书 → 秘境（含开放星期），来自 genshin-db 的 domains.daysOfWeek
        "talent_domains": talent_domains,
        # 材料档位链 → 等效绿色素材（算"还要刷几天"要用），来自 genshin-db 的 crafts
        "craft_tiers": craft_tiers,
        "unmatched": unmatched,
        # 与 genshin-db 的交叉校验结果（两边独立数据链，用来发现推导错误）
        "cross_check": check,
        "stats": stats,
    }
    return knowledge


def write(knowledge):
    folder = os.path.dirname(OUT_PATH)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with io.open(OUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(knowledge, handle, ensure_ascii=False, indent=1)
    return OUT_PATH


def main():
    parser = argparse.ArgumentParser(description="构建材料族知识表")
    parser.add_argument("--report", action="store_true", help="只打印覆盖率，不写文件")
    args = parser.parse_args()

    knowledge = build()
    stats = knowledge["stats"]
    print(f"材料 {stats['with_family']} 种有族群（其中 {stats['with_route']} 种能落到 BetterGI 路线）")
    print(f"族群 {stats['families']} 个（其中 {stats['families_with_route']} 个有路线，"
          f"{stats.get('families_with_enemies', 0)} 个认得成员怪物）")
    print(f"  来自 mapping.json：{stats['mapping_materials']}；"
          f"百科字典补漏：{stats['baike_only']}；genshin-db 补漏：{stats.get('genshin_db_only', 0)}")
    print(f"  天赋书日程（genshin-db）：{stats.get('talent_domains', 0)} 条；"
          f"族群成员怪物：{stats.get('enemies_attached', 0)} 条")
    print(f"  与 genshin-db 交叉校验：一致 {stats.get('cross_check_agree', 0)}，"
          f"冲突 {stats.get('cross_check_conflict', 0)}")
    conflicts = (knowledge.get("cross_check") or {}).get("conflicts") or []
    for item in conflicts[:10]:
        print(f"    ⚠️ {item['material']}：我们「{item['ours']}」 vs genshin-db「{item['genshin_db']}」"
              f"（{item['genshin_db_source']}）")

    no_route = sorted(f["name"] for f in knowledge["families"].values() if not f["route"])
    if no_route:
        print(f"\n没有 BetterGI 路线的族群 {len(no_route)} 个：")
        print("  " + "、".join(no_route[:60]))

    if args.report:
        print("\n（--report：不写文件）")
        return
    path = write(knowledge)
    print(f"\n已写入 {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
