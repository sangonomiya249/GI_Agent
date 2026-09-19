"""养成系统的领域模型与常量。

这一层**只放"是什么"**：阶段名、排序规则、目标校验、物品分类、状态机枚举。
所有"怎么算"（材料缺口、任务生成）在 `material_planner.py`，
所有"什么时候做"（角色顺序、阶段顺序、状态流转）在 `growth_planner.py`。

⚠️ 刻意不依赖米游社原始 JSON 的任何字段名 —— 米游社接口改版时只动
`skills/mys_calculator.py` 与 `skills/mys_inventory.py`，这个文件不受影响（规格书 §38.14）。
"""

import datetime

# ==========================================
# 🌟 养成阶段（一个角色内部的三段）
# ==========================================

PHASE_CHARACTER_LEVEL = "character_level"
PHASE_WEAPON_LEVEL = "weapon_level"
PHASE_TALENT = "talent"

PHASES = (PHASE_CHARACTER_LEVEL, PHASE_WEAPON_LEVEL, PHASE_TALENT)

PHASE_LABELS = {
    PHASE_CHARACTER_LEVEL: "角色等级",
    PHASE_WEAPON_LEVEL: "武器等级",
    PHASE_TALENT: "天赋",
}

# 默认优先级：角色等级 > 武器等级 > 天赋（规格书 §19）
DEFAULT_PHASE_PRIORITY = {
    PHASE_CHARACTER_LEVEL: 1,
    PHASE_WEAPON_LEVEL: 2,
    PHASE_TALENT: 3,
}


def phase_label(phase):
    return PHASE_LABELS.get(str(phase or ""), str(phase or ""))


def phase_priority_map(target):
    """从一条目标记录里取出三个阶段的优先级（缺项用默认值补齐）。"""
    target = target or {}
    return {
        PHASE_CHARACTER_LEVEL: _as_int(target.get("level_priority"), 1),
        PHASE_WEAPON_LEVEL: _as_int(target.get("weapon_priority"), 2),
        PHASE_TALENT: _as_int(target.get("talent_priority"), 3),
    }


def ordered_phases(target):
    """按该角色的阶段优先级排出[先做, 后做, 最后做]。

    并列时按固定顺序（角色等级→武器等级→天赋）稳定输出 —— 不然"两个都是 1"会随
    dict 顺序漂移，同一份目标每次规划出来的顺序都不一样。
    """
    priorities = phase_priority_map(target)
    order = {phase: index for index, phase in enumerate(PHASES)}
    return sorted(PHASES, key=lambda phase: (priorities[phase], order[phase]))


# ==========================================
# 🌟 养成状态机（规格书 §20）
# ==========================================

STATE_IDLE = "IDLE"
STATE_SYNCING = "SYNCING"
STATE_PLANNING = "PLANNING"
STATE_WAIT_CONFIRM = "WAIT_CONFIRM"
STATE_EXECUTING = "EXECUTING"
STATE_WAIT_BGI = "WAIT_BGI"
STATE_RESYNC = "RESYNC"
STATE_REPLANNING = "REPLANNING"
STATE_NEXT_CHARACTER = "NEXT_CHARACTER"
STATE_COMPLETE = "COMPLETE"
STATE_BLOCKED = "BLOCKED"

STATE_LABELS = {
    STATE_IDLE: "空闲",
    STATE_SYNCING: "同步米游社库存",
    STATE_PLANNING: "规划中",
    STATE_WAIT_CONFIRM: "等待确认",
    STATE_EXECUTING: "执行中",
    STATE_WAIT_BGI: "等 BetterGI 跑完",
    STATE_RESYNC: "重新同步库存",
    STATE_REPLANNING: "重新规划",
    STATE_NEXT_CHARACTER: "进入下一个角色",
    STATE_COMPLETE: "全部完成",
    STATE_BLOCKED: "受阻（等材料 / 等路线）",
}

# 合法流转：只用来做"别跳步"的自检，不强制（执行层可以因失败直接回 IDLE）
STATE_FLOW = {
    STATE_IDLE: (STATE_SYNCING, STATE_PLANNING),
    STATE_SYNCING: (STATE_PLANNING, STATE_BLOCKED),
    STATE_PLANNING: (STATE_WAIT_CONFIRM, STATE_BLOCKED),
    STATE_WAIT_CONFIRM: (STATE_EXECUTING, STATE_PLANNING),
    STATE_EXECUTING: (STATE_WAIT_BGI, STATE_BLOCKED),
    STATE_WAIT_BGI: (STATE_RESYNC, STATE_BLOCKED),
    STATE_RESYNC: (STATE_REPLANNING, STATE_BLOCKED),
    STATE_REPLANNING: (STATE_WAIT_CONFIRM, STATE_NEXT_CHARACTER, STATE_BLOCKED, STATE_COMPLETE),
    STATE_NEXT_CHARACTER: (STATE_PLANNING, STATE_COMPLETE),
    STATE_BLOCKED: (STATE_SYNCING, STATE_PLANNING, STATE_WAIT_CONFIRM),
    STATE_COMPLETE: (STATE_IDLE, STATE_PLANNING),
}


def state_label(state):
    return STATE_LABELS.get(str(state or ""), str(state or ""))


# ==========================================
# 🌟 库存条目分类（给材料分组 / 挑刷法用）
# ==========================================

CATEGORY_CURRENCY = "currency"
CATEGORY_EXP_BOOK = "exp_book"
CATEGORY_WEAPON_EXP = "weapon_exp"
CATEGORY_ASCENSION = "ascension_material"
CATEGORY_TALENT_BOOK = "talent_book"
CATEGORY_BOSS_MATERIAL = "boss_material"
CATEGORY_WEEKLY_BOSS = "weekly_boss"
CATEGORY_SPECIALTY = "specialty"
CATEGORY_MONSTER_DROP = "monster_drop"
CATEGORY_WEAPON_ASCENSION = "weapon_ascension"
CATEGORY_GEM = "gem"
CATEGORY_FOOD = "food"
CATEGORY_OTHER = "other"

CATEGORY_LABELS = {
    CATEGORY_CURRENCY: "货币",
    CATEGORY_EXP_BOOK: "经验书",
    CATEGORY_WEAPON_EXP: "武器经验",
    CATEGORY_ASCENSION: "角色突破材料",
    CATEGORY_TALENT_BOOK: "天赋材料",
    CATEGORY_BOSS_MATERIAL: "Boss 材料",
    CATEGORY_WEEKLY_BOSS: "周本材料",
    CATEGORY_SPECIALTY: "地区特产",
    CATEGORY_MONSTER_DROP: "魔物掉落",
    CATEGORY_WEAPON_ASCENSION: "武器突破材料",
    CATEGORY_GEM: "元素宝石",
    CATEGORY_FOOD: "食材",
    CATEGORY_OTHER: "其它",
}

# 名字里出现这些词就归到对应类别（顺序即优先级，先匹配到的赢）。
# 用"名字"而不是"item_id 段"分类，是为了米游社改物品编号时这里不用跟着改。
_CATEGORY_KEYWORDS = (
    (CATEGORY_CURRENCY, ("摩拉", "创世结晶", "原石", "星尘", "星辉")),
    (CATEGORY_EXP_BOOK, ("大英雄的经验", "冒险家的经验", "流浪者的经验", "经验书")),
    (CATEGORY_WEAPON_EXP, ("精锻用魔矿", "精锻用良矿", "精锻用杂矿", "武器经验")),
    (CATEGORY_WEEKLY_BOSS, ("周本", "战狂", "东风之翎", "东风之爪", "东风的吐息", "北风之环",
                            "北风之尾", "北风狼的灵魂", "吞天之鲸", "血玉之枝", "鎏金之鳞",
                            "龙王之冕", "万古之痛", "禅那院", "生长的秘钥", "无光丝线",
                            "无光质块", "原初玉块", "寰宇之种", "奇械发条", "丝织之印",
                            "月华之光", "梦魔之血", "诸王之心")),
    (CATEGORY_TALENT_BOOK, ("教导", "指引", "哲学", "心得", "之启")),
    (CATEGORY_GEM, ("碎屑", "断片", "宝石", "晶石", "原石碎")),
    (CATEGORY_WEAPON_ASCENSION, ("雾虚花粉", "雾虚草", "雾虚灯芯", "弓术", "祭品", "法器",
                                 "长柄", "双手剑", "单手剑")),
    (CATEGORY_BOSS_MATERIAL, ("之角", "之尾", "之心", "之齿", "之翼", "之颅", "未熟之玉",
                              "秘刻金纹的源核", "玛瑙", "晶核")),
    (CATEGORY_MONSTER_DROP, ("面具", "箭簇", "卷轴", "徽记", "花蕊", "史莱姆", "黏液",
                             "刀谭", "绘卷", "浮游", "孢子", "菌核", "晶化骨髓", "棱镜",
                             "号角", "指爪", "鳞", "羽", "钩", "齿", "带", "油", "布匹",
                             "兽肉", "香辛果", "鬼兜虫")),
    # ⚠️ 这一条必须靠后：像「清心」「风车菊」「琉璃袋」这类特产的命名毫无规律，
    #    宁可落到 other（只影响分组显示），也不要猜成 Boss 材料而排错任务。
    (CATEGORY_SPECIALTY, ("莲", "花", "果", "莓", "菇", "草", "叶", "芽", "藤", "石")),
)

# item_id 的官方分段。**先按 id 判、再按名字判**：
# 「清心」这种特产名字里带"心"，按名字会被误判成 Boss 材料（"之心"那一档），
# 但它的 id（100092）落在特产段里 —— id 比名字可靠，所以 id 优先。
_ITEM_ID_RANGES = (
    (10_000, 19_999, CATEGORY_GEM),              # 元素晶石（碎屑 / 断片 / 块）
    (20_000, 29_999, CATEGORY_BOSS_MATERIAL),    # 角色突破 Boss 材料
    (30_000, 39_999, CATEGORY_MONSTER_DROP),     # 魔物掉落
    (40_000, 51_999, CATEGORY_FOOD),             # 食材 / 料理
    (52_000, 59_999, CATEGORY_SPECIALTY),        # 地区特产（清心 = 100092 也在下面那段）
    (100_000, 100_999, CATEGORY_SPECIALTY),      # 地区特产（蒙德 / 璃月 / 稻妻…）
    (101_000, 101_999, CATEGORY_SPECIALTY),      # 地区特产（须弥 / 枫丹 / 纳塔…）
    (104_001, 104_001, CATEGORY_CURRENCY),       # 摩拉（104 段里的特例，必须排在下面之前）
    (104_000, 104_999, CATEGORY_TALENT_BOOK),    # 天赋书
    (105_000, 105_999, CATEGORY_WEAPON_EXP),     # 精锻用魔矿
    (112_000, 112_999, CATEGORY_WEEKLY_BOSS),    # 周本材料
    (113_000, 114_999, CATEGORY_WEAPON_ASCENSION),  # 武器突破素材
    (121_000, 122_999, CATEGORY_CURRENCY),       # 货币类
    (201_000, 201_999, CATEGORY_EXP_BOOK),       # 冒险家的经验 / 流浪者的经验
    (202_000, 202_999, CATEGORY_EXP_BOOK),       # 大英雄的经验
)


def classify_item(name=None, item_id=None):
    """把一件物品归到一个大类：**先看 item_id 段（可靠），再看名字关键词**。"""
    try:
        value = int(item_id)
    except (TypeError, ValueError):
        value = None
    if value is not None:
        for low, high, category in _ITEM_ID_RANGES:
            if low <= value <= high:
                return category

    text = str(name or "")
    for category, keywords in _CATEGORY_KEYWORDS:
        for keyword in keywords:
            if keyword and keyword in text:
                return category
    return CATEGORY_OTHER


def category_label(category):
    return CATEGORY_LABELS.get(str(category or ""), CATEGORY_LABELS[CATEGORY_OTHER])


# ==========================================
# 🌟 目标校验（规格书 §10 / §11 / §12）
# ==========================================

LEVEL_MIN = 1
LEVEL_MAX = 90
TALENT_MIN = 1
TALENT_MAX = 10


def clamp_level(value, default=90):
    """角色 / 武器等级目标：**1~90 的任意整数**（81、83 都必须合法，不是只能选 80/90）。"""
    return max(LEVEL_MIN, min(LEVEL_MAX, _as_int(value, default)))


def clamp_talent(value, default=10):
    """单项天赋目标：1~10（三个天赋各自独立，不允许只存一个"天赋等级"）。"""
    return max(TALENT_MIN, min(TALENT_MAX, _as_int(value, default)))


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return int(default)


def normalise_target_values(values):
    """把外部传进来的目标字段规范化（越界一律夹到合法区间，坏值用默认值）。

    只处理"传进来的"字段 —— 没传的不补，保持 PUT 的"局部更新"语义。
    """
    values = dict(values or {})
    result = {}
    for key, value in values.items():
        if key == "level_target":
            result[key] = clamp_level(value, 90)
        elif key in ("normal_target", "skill_target", "burst_target"):
            result[key] = clamp_talent(value, 10)
        elif key == "weapon_level_target":
            result[key] = clamp_level(value, 90)
        elif key == "enabled":
            result[key] = 1 if _truthy(value) else 0
        elif key == "weapon_enabled":
            result[key] = 1 if _truthy(value) else 0
        elif key == "weapon_id":
            result[key] = max(0, _as_int(value, 0))
        elif key == "priority":
            result[key] = max(1, _as_int(value, 1))
        elif key in ("level_priority", "weapon_priority", "talent_priority"):
            result[key] = max(1, min(3, _as_int(value, 1)))
        elif key == "completion_mode":
            mode = str(value or "sequential").strip().lower()
            result[key] = mode if mode in ("sequential", "parallel") else "sequential"
    return result


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "y", "是")


# ==========================================
# 🌟 角色排序（规格书 §14 / §15）
# ==========================================

SORT_MODE_DEFAULT = "default"
SORT_MODE_CUSTOM = "custom"
SORT_MODE_AUTO = "auto"        # 第二阶段才实现评分

SORT_MODES = (SORT_MODE_DEFAULT, SORT_MODE_CUSTOM, SORT_MODE_AUTO)
SORT_MODE_LABELS = {
    SORT_MODE_DEFAULT: "默认（5 星优先）",
    SORT_MODE_CUSTOM: "自定义（按我拖的顺序）",
    SORT_MODE_AUTO: "自动规划（第二阶段）",
}

# 规格书 §16 里的"新出的 5 星角色"：没有权威的"新角色"数据源，
# 所以默认顺序把这一档留空（排序规则本身是：priority ASC → rarity DESC → character_id ASC）。
RARITY_NEW_FIVE_STAR = 0


def sort_key(target):
    """默认排序键：用户 priority ASC → 星级 DESC → character_id ASC。"""
    target = target or {}
    return (
        _as_int(target.get("priority"), 9999),
        -_as_int(target.get("rarity"), 0),
        _as_int(target.get("character_id"), 0),
    )


def sort_targets(targets, mode=SORT_MODE_DEFAULT):
    """按排序模式排好角色。

    · default：priority ASC → 星级 DESC → character_id ASC；
    · custom：**同一套键** —— 用户拖拽时我们已经把顺序写进 `priority` 了
      （见 `growth_db.reorder_targets`），所以"自定义"就是"尊重 priority"；
      `character_id` 只作为稳定兜底，避免顺序抖动；
    · auto：第二阶段，先按 default 排（不假装有评分算法）。
    """
    items = list(targets or ())
    mode = str(mode or SORT_MODE_DEFAULT)
    if mode == SORT_MODE_CUSTOM:
        return sorted(items, key=lambda row: (_as_int(row.get("priority"), 9999),
                                              _as_int(row.get("character_id"), 0)))
    return sorted(items, key=sort_key)


# ==========================================
# 🌟 小工具
# ==========================================


def business_now(moment=None):
    """业务时间：对齐项目里"凌晨 4 点刷新"的口径（和 domain_match.business_weekday 一致）。"""
    return (moment or datetime.datetime.now()) - datetime.timedelta(hours=4)


def business_weekday(moment=None):
    return "一二三四五六日"[business_now(moment).isoweekday() - 1]


def current_levels(inventory_avatar=None):
    """从米游社同步下来的角色资料里取"当前状态"：`(等级, {天赋}, 武器等级)`。

    天赋的键统一成 `normal/skill/burst` —— 米游社给的是按技能 id 排好的 `{技能id: 等级}`，
    而这个顺序就是游戏内的 普攻 → 战技 → 爆发（见 `skills/mys_api.talents_from_detail` 的说明）。
    """
    avatar = inventory_avatar or {}
    level = _as_int(avatar.get("level"), 0)

    # `talents` 是"技能id → 等级"的字典（米游社战绩接口与养成计算器都归一化成这个形状）；
    # `skills` 只在调用方直接塞了计算器原始列表时才出现，按"第几个就是第几项"兜底。
    talents = avatar.get("talents")
    if not isinstance(talents, dict):
        talents = _talents_from_skill_list(avatar.get("skills"))

    ordered = []
    for key in sorted(talents, key=lambda item: _as_int(item, 10 ** 9)):
        ordered.append(_as_int(talents[key], 0))
    while len(ordered) < 3:
        ordered.append(0)

    weapon = avatar.get("weapon") or {}
    return (
        level,
        {"normal": ordered[0], "skill": ordered[1], "burst": ordered[2]},
        _as_int(weapon.get("level") or weapon.get("level_current"), 0),
    )


def _talents_from_skill_list(skills):
    """把计算器原始技能列表 `[{skill_id, level}]` 转成 `{技能id: 等级}`。"""
    if not isinstance(skills, (list, tuple)):
        return {}
    result = {}
    for index, item in enumerate(skills):
        if not isinstance(item, dict):
            continue
        skill_id = item.get("skill_id") or item.get("id") or index + 1
        level = item.get("level")
        if level is None:
            level = item.get("level_current")
        result[str(skill_id)] = _as_int(level, 0)
    return result


def is_complete(target, current):
    """当前状态是否已经达到目标（三阶段全部达标才算这个角色完成）。

    `current` 是 `{"level": 80, "talents": {...}, "weapon_level": 80}` 这种形状。
    """
    target = target or {}
    current = current or {}
    if _as_int(current.get("level"), 0) < _as_int(target.get("level_target"), 90):
        return False
    talents = current.get("talents") or {}
    if _as_int(talents.get("normal"), 0) < _as_int(target.get("normal_target"), 10):
        return False
    if _as_int(talents.get("skill"), 0) < _as_int(target.get("skill_target"), 10):
        return False
    if _as_int(talents.get("burst"), 0) < _as_int(target.get("burst_target"), 10):
        return False
    if target.get("weapon_enabled"):
        if _as_int(current.get("weapon_level"), 0) < _as_int(target.get("weapon_level_target"), 90):
            return False
    return True


def phase_complete(target, phase, current):
    """单个阶段是否已完成（武器阶段只在 `weapon_enabled` 时才参与判断）。"""
    target = target or {}
    current = current or {}
    talents = current.get("talents") or {}
    if phase == PHASE_CHARACTER_LEVEL:
        return _as_int(current.get("level"), 0) >= _as_int(target.get("level_target"), 90)
    if phase == PHASE_WEAPON_LEVEL:
        if not target.get("weapon_enabled"):
            return True
        return _as_int(current.get("weapon_level"), 0) >= _as_int(target.get("weapon_level_target"), 90)
    if phase == PHASE_TALENT:
        return (
            _as_int(talents.get("normal"), 0) >= _as_int(target.get("normal_target"), 10)
            and _as_int(talents.get("skill"), 0) >= _as_int(target.get("skill_target"), 10)
            and _as_int(talents.get("burst"), 0) >= _as_int(target.get("burst_target"), 10)
        )
    return True


def target_summary(target):
    """目标的一行摘要：`Lv 90 · 天赋 10/10/10`（Studio 列表里的"目标"列）。

    ⚠️ 以前是裸的 `90/10/10/10` —— 谁看都不知道四个数字各是什么
    （等级？天赋？天赋三个槽位的顺序？），被玩家反馈过。带上字段名就没歧义了。
    """
    target = target or {}
    level = _as_int(target.get("level_target"), 90)
    talents = "{}/{}/{}".format(
        _as_int(target.get("normal_target"), 10),
        _as_int(target.get("skill_target"), 10),
        _as_int(target.get("burst_target"), 10),
    )
    return f"Lv {level} · 天赋 {talents}"


def current_summary(current):
    """当前状态的一行摘要：`Lv 81 · 天赋 1/10/6`。

    槽位顺序固定是 **普攻 / 战技 / 爆发**（和游戏里一样），标签写在前面。
    """
    current = current or {}
    talents = current.get("talents") or {}
    level = _as_int(current.get("level"), 0)
    slots = "{}/{}/{}".format(
        _as_int(talents.get("normal"), 0),
        _as_int(talents.get("skill"), 0),
        _as_int(talents.get("burst"), 0),
    )
    return f"Lv {level} · 天赋 {slots}"
