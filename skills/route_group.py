"""BetterGI 调度器脚本组（User/ScriptGroup/*.json）的通用读写。

四个跑图类目共用同一套结构（projects[].folderName / name / status）：
  1. **地图素材**（`地方特产` 路线）：LLM 给 `gather` 目标 → 开关对应路线
  2. **敌人与魔物**（`敌人与魔物` 路线）：LLM 给 `hunt` 目标 → 开关对应路线
  3. **锄大地**（`锄地专区` 路线）：LLM 给 `hoe` 目标 → 开关对应路线
  4. **矿物 / 食材与炼金**：`mine` / `cook` 目标 → 开关对应路线

为什么要抽出来：原来这段逻辑写死在 bgi_controller 里只服务地图素材，
再加一路敌人路线就会出现两份几乎一样、但防闪退阈值各自维护的代码。

⚠️ 类目隔离（锄大地 ≠ 敌人与魔物）：新版脚本仓库按类目分目录，两边路线名会重名
（锄地专区里有 `0_0_飞萤` 子目录，还有叫「…三骗骗花」的锄地路线）。所有匹配都带
`own_prefix`，只认本类目前缀下的路线，否则「刷点骗骗花」会跑去锄整张图。

⚠️ 防闪退隔离带：BetterGI 的脚本组里连续 `Disabled` 的脚本超过一定数量会触发 bug，
所以每连续 150 条 Disabled 就强制打开一条（阈值与原实现保持一致）。

⚠️ 一条龙注册：脚本组要出现在一条龙里，必须在一条龙配置的 `TaskDefinitions` 里
有「UUID → 组名」登记。**新格式配置只认 TaskDefinitions 里的 Id** —— 直接把组名当键塞进
`TaskEnabledList` 会被 BetterGI 跳过（源码 `OneDragonFlowViewModel.LoadDisplayTaskListFromConfig`），
所以 `bgi_controller.set_task_enabled` 在启用未登记的组时会**自动补一条登记**
（等价于在界面上点一次"添加"）。`ensure_category_group` 也只会生成**组文件**，
不会凭空造别的 BetterGI 数据。
"""

import copy
import json
import os
import re
from typing import NamedTuple, Sequence

import config

FORCE_ENABLE_AFTER_DISABLED = 150

# 精简组时，完整清单归档到这里（相对脚本组目录）：
#   User/ScriptGroup/.gi_agent_archive/敌人与魔物.json
# 之后的每次选路线都从归档的完整清单里挑，不依赖 UI 里那份被精简过的组。
ARCHIVE_DIR_NAME = ".gi_agent_archive"

# BetterGI 的"按队伍自动挑策略"特殊值：传给 AutoFight 的是整个目录，永远可用
AUTO_SELECT_STRATEGY = "根据队伍自动选择"

# ==========================================
# 🌟 类目隔离：锄大地 ≠ 敌人与魔物
# ==========================================
# BetterGI 新版脚本仓库把路线按类目分目录，脚本组里的 folderName 因此长这样：
#   `敌人与魔物\骗骗花\骗骗花@san`  /  `锄地专区\小怪2000@mno\0_0_飞萤`
# 两边的**路线名会重名**（锄地专区里就有 `0_0_飞萤` 子目录、还有叫
# 「…踏鞴反应炉东三骗骗花」的锄地路线），只按名字匹配必然互相抢路线：
#   - 玩家说「刷点骗骗花」（= 敌人与魔物）时，锄地专区的路线会被一起打开 → 跑了整张图；
#   - 玩家说「锄大地」（= 锄地专区）时，敌人与魔物的飞萤路线会被打开 → 只打一个怪点。
# 所以：**只认类目前缀**，别人的类目前缀下的路线一律不参与匹配。
CATEGORY_FOLDER_PREFIXES = ("地方特产", "敌人与魔物", "矿物", "食材与炼金", "锄地专区")

# 锄地类别的口语说法 → 统一映射成类目名，用于「锄大地」这种不带任何地区/类型的说法
HOE_TARGET_ALIASES = {
    "锄大地": "锄地专区",
    "锄地": "锄地专区",
    "锄地专区": "锄地专区",
    "锄地图": "锄地专区",
    "清怪": "锄地专区",
    "扫怪": "锄地专区",
    "刷怪": "锄地专区",
    "刷全图": "锄地专区",
    "全图": "锄地专区",
    "全图清怪": "锄地专区",
}

# JS 整脚本的口语说法 → 脚本组/脚本名候选（锄地一条龙 = AutoHoeingOneDragon@mno）
JS_SCRIPT_ALIASES = {
    "锄大地": ("锄地一条龙", "AutoHoeingOneDragon", "自动小怪锄地规划", "自动精英锄地规划"),
    "锄地": ("锄地一条龙", "AutoHoeingOneDragon", "自动小怪锄地规划", "自动精英锄地规划"),
    "锄地一条龙": ("锄地一条龙", "AutoHoeingOneDragon"),
    "AAA狗粮批发": ("AAA狗粮批发", "AAA-Artifacts-Bulk-Supply"),
}

# 名字里带这些词的是"整脚本"（JS one-dragon），不是跑图路线，别把口语展开成类目
WHOLE_SCRIPT_MARKERS = ("一条龙",)


def folder_prefix_of(folder_name):
    """取 folderName 的顶层目录（BetterGI 里就是类目名）。"""
    parts = re.split(r"[\\/]", str(folder_name or "").strip())
    return parts[0].strip() if parts else ""


def is_foreign_project(project, own_prefix):
    """这条路线是不是"别的类目"的（是就别碰）。

    走过的坑：锄地专区的 `0_0_飞萤`、`精英400@汐\1-精英` 和敌人与魔物的 `飞萤`
    只靠名字分不开；只按名字匹配就会「说刷骗骗花、结果跑了整张图的锄地路线」。
    没有类目前缀的老版本组（如 `蕈兽\蕈兽@san`、`石珀`）不算"别的类目"，照常参与匹配。
    """
    own_prefix = str(own_prefix or "").strip()
    if not own_prefix:
        return False
    prefix = folder_prefix_of((project or {}).get("folderName"))
    return prefix in CATEGORY_FOLDER_PREFIXES and prefix != own_prefix


def folder_has_skip_token(folder_name, skip_tokens):
    """folderName 是否落在"不跑"的子目录里（低效路线 / 未修正部分）。"""
    if not skip_tokens:
        return False
    text = str(folder_name or "")
    return any(str(token) in text for token in skip_tokens if str(token))


def effective_skip_tokens(category, targets):
    """本次要跳过的子目录关键词（玩家显式点名"低效/全部"时不跳）。"""
    skip = tuple(getattr(category, "skip_tokens", ()) or ())
    if not skip:
        return ()
    allow_words = tuple(getattr(category, "skip_allow_words", ()) or ())
    text = "".join(str(t) for t in (targets or []))
    if allow_words and any(word in text for word in allow_words):
        return ()
    return skip


def category_search_terms(category, targets):
    """把玩家的说法展开成实际参与匹配的词（锄大地 → 锄地专区 等）。

    ⚠️ 带「一条龙」的目标不展开：那是**整脚本**的名字（锄地一条龙 = AutoHoeingOneDragon），
    展开成 `锄地专区` 会让 Agent 去跑 300 条跑图路线，而不是跑那个脚本。
    """
    terms = [str(t).strip() for t in (targets or []) if str(t).strip()]
    aliases = getattr(category, "target_aliases", None) or ()
    pairs = aliases.items() if isinstance(aliases, dict) else aliases
    if not pairs:
        return terms

    expanded = list(terms)
    for term in terms:
        if any(marker in term for marker in WHOLE_SCRIPT_MARKERS):
            continue
        for spoken, canonical in pairs:
            if str(spoken) in term and str(canonical) not in expanded:
                expanded.append(str(canonical))
    return expanded



def strategy_path(strategy_name, auto_fight_dir=None):
    """按 BetterGI 的规则把策略名解析成文件路径（**平铺**，不递归）。

    源码依据 AutoFightParam.cs:131 → `User\\AutoFight\\` + name + `.txt`；
    所以放在 `User\\AutoFight\\群友分享\\` 里的策略，名字必须写成
    `群友分享\\四神队(进阶版)`，写成 `四神队(进阶版)` 就会报"战斗策略文件不存在"。
    """
    name = str(strategy_name or "").strip()
    if not name or name == AUTO_SELECT_STRATEGY:
        return None
    directory = auto_fight_dir or config.BGI_AUTO_FIGHT_DIR
    return os.path.join(directory, f"{name}.txt")


def find_repo_strategy(strategy_name, repos_dir=None):
    """在脚本仓库里找同名策略文件（找到了说明"仓库有、但没导入 User/AutoFight"）。"""
    name = str(strategy_name or "").strip()
    if not name:
        return None
    root = repos_dir or os.path.join(config.BGI_DIR, "Repos")
    if not os.path.isdir(root):
        return None
    target = f"{name}.txt"
    for current, _dirs, files in os.walk(root):
        if target in files and "combat" in current.replace("\\", "/").lower():
            return os.path.join(current, target)
    return None


def strategy_notice(strategy_name, auto_fight_dir=None, repos_dir=None):
    """校验战斗策略名；返回告警文案，没问题返回空串。

    踩过的坑：组里配了 `1.四神挂机[推荐]`，但该文件只在脚本仓库
    （Repos/*/repo/combat/）里、没进 `User\\AutoFight\\`，
    于是路径追踪走到怪点就抛 `战斗策略文件不存在`，整个脚本立刻结束
    —— 看起来就像"战斗打不起来/识别不到战斗结束"。
    """
    name = str(strategy_name or "").strip()
    if not name or name == AUTO_SELECT_STRATEGY:
        return ""

    path = strategy_path(name, auto_fight_dir)
    if os.path.isfile(path):
        return ""

    repo_hit = find_repo_strategy(name, repos_dir)
    hint = (
        f"\n   ↳ 脚本仓库里其实有：{repo_hit}（BetterGI 只认 User\\AutoFight 下的文件，"
        f"把 txt 复制过去、或在「战斗策略」界面导入即可）"
        if repo_hit
        else f"\n   ↳ 可用策略：{available_strategy_names(auto_fight_dir) or '（读不到）'}"
    )
    return (
        f"⚠️ 战斗策略「{name}」在 {os.path.dirname(path)} 下找不到 {os.path.basename(path)}："
        f"BetterGI 会在怪点抛「战斗策略文件不存在」并中断整条跑图路线。{hint}"
    )


def _js_match_score(target_norm, text):
    """候选文本与目标的匹配分：完全相等 > 目标名是候选子串 > 候选名是目标子串。"""
    if not text or not target_norm:
        return 0
    if text == target_norm:
        return 3
    if target_norm in text:
        return 2
    if len(text) >= 2 and text in target_norm:
        return 1
    return 0


def _js_target_candidates(target_norm):
    """JS 脚本的目标候选写法（把「锄大地」这类口语翻译成「锄地一条龙 / AutoHoeingOneDragon」）。"""
    candidates = [target_norm] if target_norm else []
    for spoken, names in JS_SCRIPT_ALIASES.items():
        if normalize_match_text(spoken) in target_norm:
            for name in names:
                candidate = normalize_match_text(name)
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
    return candidates


def find_js_script_group(target, group_dir=None, registered_names=(), exact_only=False):
    """按名字找"JS 脚本组"（组里的项目 type 是 Javascript），返回 (组名, 路径, 脚本名, 是否已登记一条龙)。

    为什么单独一类：狗粮（狗粮ABE / AAA狗粮批发）、采集水下这类**不是跑图路线**，
    而是整个 JS 脚本挂在调度器组里跑。它们没有 folderName 路线可开关，
    只能把所在脚本组在一条龙里启用/停用。

    打分选最优（而不是"先扫到就算"）—— 否则「AAA狗粮批发」会被名字更短的
    「狗粮」组抢走。同时优先返回**已经在一条龙里登记过**的组，因为没登记的组
    就算写进 TaskEnabledList 也不会被执行。
    """
    target = str(target or "").strip()
    if not target:
        return None

    directory = group_dir or config.BGI_SCRIPT_GROUP_DIR
    target_norm = normalize_match_text(target)
    candidates = _js_target_candidates(target_norm)
    registered = {str(name) for name in (registered_names or [])}

    best = None
    for path in list_group_files(directory):
        group = load_group(path)
        if not group:
            continue
        js_projects = [
            project
            for project in (group.get("projects") or [])
            if str(project.get("type") or "").lower() == "javascript"
        ]
        if not js_projects:
            continue

        group_name = str(group.get("name") or os.path.splitext(os.path.basename(path))[0])
        for project in js_projects:
            script_name = str(project.get("name") or group_name)
            score = 0
            for haystack in (script_name, group_name, str(project.get("folderName") or "")):
                score = max(
                    score,
                    *(
                        _js_match_score(candidate, normalize_match_text(haystack))
                        for candidate in candidates
                    ),
                )
            if not score:
                continue
            if exact_only and score < 3:
                continue

            registered_bonus = 1 if group_name in registered else 0
            key = (score + registered_bonus, score, len(normalize_match_text(script_name)))
            if best is None or key > best[0]:
                best = (key, group_name, path, script_name, group_name in registered)

    if not best:
        return None
    return best[1], best[2], best[3], best[4]


def available_js_script_groups(group_dir=None, registered_names=()):
    """列出所有"JS 脚本组"：返回 [(组名, 脚本名, 是否已登记一条龙)]。"""
    directory = group_dir or config.BGI_SCRIPT_GROUP_DIR
    registered = {str(name) for name in (registered_names or [])}
    rows = []
    for path in list_group_files(directory):
        group = load_group(path)
        if not group:
            continue
        js_projects = [
            project
            for project in (group.get("projects") or [])
            if str(project.get("type") or "").lower() == "javascript"
        ]
        if not js_projects:
            continue
        group_name = str(group.get("name") or os.path.splitext(os.path.basename(path))[0])
        for project in js_projects:
            rows.append((group_name, str(project.get("name") or group_name), group_name in registered))
    return rows


def normalize_match_text(text):
    """比较用：去掉空白与常见标点，便于「狗粮」「狗粮ABE」这类叫法对上。"""
    return re.sub(r"[\s\-_/\\|、，,。.：:（）()\[\]【】]", "", str(text or "")).lower()


def available_strategy_names(auto_fight_dir=None):
    """User/AutoFight 下可用的策略名（含子目录前缀，与 BetterGI 的写法一致）。"""
    directory = auto_fight_dir or config.BGI_AUTO_FIGHT_DIR
    names = []
    for current, _dirs, files in os.walk(directory):
        for filename in files:
            if not filename.lower().endswith(".txt"):
                continue
            relative = os.path.relpath(os.path.join(current, filename), directory)
            names.append(os.path.splitext(relative)[0])
    return "、".join(sorted(names))


def group_strategy_notice(group, auto_fight_dir=None, repos_dir=None):
    """校验一个脚本组 config.pathingConfig 里的战斗策略名。"""
    pathing = ((group or {}).get("config") or {}).get("pathingConfig") or {}
    if not pathing.get("autoFightEnabled", True):
        return ""
    strategy = (pathing.get("autoFightConfig") or {}).get("strategyName")
    return strategy_notice(strategy, auto_fight_dir, repos_dir)


# ==========================================
# 🌟 路线类目：敌人与魔物 / 矿物 / 食材与炼金
# ==========================================
class RouteCategory(NamedTuple):
    """一个"自由任务类目"的定义（free_task 的 action → 脚本组）。"""

    action: str            # LLM 输出的 free_task action，如 hunt / mine / cook / hoe
    label: str             # 中文名，用于提示
    group_filename: str    # 首选脚本组文件名（BetterGI 调度器里那份）
    match_fields: tuple    # 参与匹配的字段
    both_ways: bool        # 是否允许"目录名是目标名子串"（材料名反查敌人时需要）
    expand_drops: bool     # 是否用掉落字典把材料名反查成敌人名
    hint: str              # 给玩家/模型看的目标写法提示
    # 🌟 类目隔离与适配（默认值保证老调用不受影响）
    folder_prefix: str = ""        # 该类目在 folderName / AutoPathing 里的顶层目录名
    target_aliases: tuple = ()     # (口语说法, 类目名) 映射，tuple 便于 NamedTuple 默认值
    skip_tokens: tuple = ()        # folderName 命中这些词的路线默认不跑（低效/不跑）
    skip_allow_words: tuple = ()   # 玩家说了这些词就不跳（低效/全部）
    auto_create: bool = False      # 总组不存在时，是否按 AutoPathing 目录自动生成


def _hoe():
    """锄大地类目：「锄地专区」路线（小怪2000@mno / 精英400@汐 / 挪德卡莱锄地小怪）。"""
    return RouteCategory(
        "hoe",
        "锄大地",
        config.BGI_HOE_CONFIG_NAME,
        ENEMY_MATCH_FIELDS,
        True,
        False,
        "地区名或类型名（如 蒙德 / 璃月 / 稻妻 / 须弥 / 枫丹 / 纳塔 / 挪德卡莱 / 小怪 / 精英 / 传奇；"
        "只说「锄大地」就把锄地专区整片图排上）",
        "锄地专区",
        tuple(HOE_TARGET_ALIASES.items()),
        ("低效", "不跑"),
        ("低效", "不跑", "全部", "所有", "全跑"),
        True,
    )


def category_specs():
    """返回 {action: RouteCategory}。路径每次从 config 读，方便测试与 .env 覆盖。"""
    return {
        "hunt": RouteCategory(
            "hunt",
            "敌人与魔物",
            config.BGI_ENEMY_CONFIG_NAME,
            ENEMY_MATCH_FIELDS,
            True,
            True,
            "敌人类别名或它的掉落材料（如 蕈兽 / 骗骗花 / 原素花蜜）",
            "敌人与魔物",
        ),
        "hoe": _hoe(),
        "mine": RouteCategory(
            "mine",
            "矿物",
            config.BGI_MINE_CONFIG_NAME,
            ENEMY_MATCH_FIELDS,
            True,
            False,
            "矿物名（如 水晶块 / 紫晶块 / 星银矿石 / 白铁块 / 铁块 / 魔晶矿 / 萃凝晶 / 虹滴晶）",
            "矿物",
        ),
        "cook": RouteCategory(
            "cook",
            "食材与炼金",
            config.BGI_COOK_CONFIG_NAME,
            ENEMY_MATCH_FIELDS,
            True,
            False,
            "食材/炼金材料名（如 禽肉 / 鱼肉 / 螃蟹 / 蘑菇 / 薄荷 / 甜甜花 / 胡萝卜 / 松茸）",
            "食材与炼金",
        ),
    }



def category_group_path(category, group_dir=None):
    """类目的首选脚本组文件路径。"""
    directory = group_dir or config.BGI_SCRIPT_GROUP_DIR
    return os.path.join(directory, category.group_filename)


def free_task_actions():
    """所有"自由任务"action（gather 保持原有独立实现，其余走类目机制）。"""
    return ("gather", *category_specs().keys())


def all_category_specs():
    """全部类目（含地图素材），用于"这个目标到底属于哪一类"的探测。"""
    specs = category_specs()
    specs["gather"] = RouteCategory(
        "gather",
        "地图素材",
        config.BGI_MAP_CONFIG_NAME,
        DEFAULT_MATCH_FIELDS,
        False,
        False,
        "角色突破特产名（如 清心 / 清水玉 / 慕风蘑菇 / 沙脂蛹）",
        "地方特产",
    )
    return specs


def category_match_source(spec, group_dir=None):
    """拿"该类目能用来匹配的全量路线"：总组（归档优先），总组还不存在时用可自动生成的路线。

    为什么要这层：改判（reclassify）发生在真正写配置之前，那时「锄大地」组可能还没建出来。
    如果这里返回空，玩家说「锄大地」被写成 hunt 时就改判不动，最后只能报"没找到路线组"。
    """
    preferred = category_group_path(spec, group_dir)
    full, source, _archived = load_full_group(preferred)
    if full:
        return full, source

    if getattr(spec, "auto_create", False) and config.BGI_AUTO_CREATE_ROUTE_GROUP:
        projects = build_category_projects(spec)
        if projects:
            return {"name": os.path.splitext(os.path.basename(preferred))[0], "projects": projects}, None
    return None, None


def category_preferred_match_count(spec, targets, group_dir=None):
    """只看**该类目的总组**（地图素材 / 敌人与魔物 / 锄大地 / 矿物 / 食材与炼金）的命中数。"""
    targets = category_search_terms(spec, targets)
    if not targets:
        return 0
    full, _source = category_match_source(spec, group_dir)
    if not full:
        return 0
    return count_matches(
        full,
        targets,
        spec.match_fields,
        spec.both_ways,
        own_prefix=spec.folder_prefix,
        skip_tokens=effective_skip_tokens(spec, targets),
    )


def category_small_group_match_count(spec, targets, group_dir=None):
    """再看玩家按材料分好的小组（如 石珀.json / 蕈兽.json）能匹配多少条。"""
    targets = category_search_terms(spec, targets)
    if not targets:
        return 0
    directory = group_dir or config.BGI_SCRIPT_GROUP_DIR
    preferred = category_group_path(spec, directory)
    for _path, group in find_groups_for_targets(
        directory,
        targets,
        spec.match_fields,
        exclude=(config.BGI_MAP_CONFIG, preferred),
        both_ways=spec.both_ways,
        own_prefix=spec.folder_prefix,
        skip_tokens=effective_skip_tokens(spec, targets),
    ):
        matched = count_matches(
            group,
            targets,
            spec.match_fields,
            spec.both_ways,
            own_prefix=spec.folder_prefix,
            skip_tokens=effective_skip_tokens(spec, targets),
        )
        if matched:
            return matched
    return 0


def category_match_count(spec, targets, group_dir=None, include_small=True):
    """该类目能不能服务这些目标（0 = 没路线）。

    先看总组，再看玩家按材料分的小组。**不去扫别的类目的总组** —— 否则
    「久雨莲」会被 `食材与炼金.json` 命中，从而误判成"地图素材里也有"，改判永不触发。
    同理，锄地专区的路线也不会被「敌人与魔物」抢走（见 is_foreign_project）。
    """
    matched = category_preferred_match_count(spec, targets, group_dir)
    if matched or not include_small:
        return matched
    return category_small_group_match_count(spec, targets, group_dir)


def category_for_target(target, group_dir=None, exclude=(), order=None):
    """探测一个目标实际属于哪一类，返回 (spec, 命中数) 或 None。

    两轮探测，避免误判：
      1. 先只按各类目的**总组**判断（最准确：地图素材/敌人与魔物/锄大地/矿物/食材与炼金）；
      2. 第一轮全落空时，再按玩家按材料分好的**小组**判断（如 石珀.json 这种没有总组归类的）。
    默认顺序：敌人与魔物 → 锄大地 → 食材与炼金 → 矿物 → 地图素材。
    敌人排最前是为了让「骗骗花」这类既能当敌人名、又可能出现在锄地路线名里的目标归到敌人；
    锄大地紧随其后，「锄大地/清怪/精英/传奇」这类说法只有锄地专区接得住。
    调用方只应在**声明类目里没有路线**时才用它，避免把声明正确的目标改判走。
    """
    specs = all_category_specs()
    actions = [
        a
        for a in (order or ("hunt", "hoe", "cook", "mine", "gather"))
        if a not in exclude and a in specs
    ]

    for action in actions:
        matched = category_preferred_match_count(specs[action], [target], group_dir)
        if matched:
            return specs[action], matched

    for action in actions:
        matched = category_small_group_match_count(specs[action], [target], group_dir)
        if matched:
            return specs[action], matched
    return None


def reclassify_free_tasks(free_tasks, group_dir=None, registered_names=()):
    """把「类目写错」的自由任务就地改判到真正能跑的那一类。

    踩过的坑 1：LLM 把 `久雨莲`（在 repo/pathing/食材与炼金 下）当成角色突破特产写成
    `gather`，于是去「地图素材」里找 → 0 条命中 → 任务被停用、什么都没采。
    踩过的坑 2：`狗粮` 不是跑图路线，而是整个 JS 脚本挂在调度器组里跑；
    LLM 同样会写成 `gather` → 同样 0 条命中。

    只在**声明类目里一条路线都没有**时才改判，声明正确的（如 清心 → gather）不受影响。
    改判顺序：四个跑图类目 → JS 脚本组（action 变成 `script`）。

    返回提示文案列表。
    """
    notices = []
    if not isinstance(free_tasks, list):
        return notices

    specs = all_category_specs()
    for task in free_tasks:
        if not isinstance(task, dict):
            continue
        action = str(task.get("action") or "").strip()
        target = str(task.get("target") or "").strip()
        if not target or action not in specs:
            continue

        declared = specs[action]
        # gather（地图素材）的执行只用那一个总组；其余类目还会用玩家按材料分的小组
        declared_ok = category_match_count(
            declared, [target], group_dir, include_small=(action != "gather")
        )
        if declared_ok:
            continue

        # 先看"名字跟某个 JS 脚本组/脚本完全一样"的情况：这时它就是整脚本任务，
        # 不能被跑图类目里偶尔冒出来的同名路线抢走（实测「狗粮」会被敌人组里
        # 1 条同名路线命中，从而误判成敌人讨伐）。
        exact_js = find_js_script_group(target, group_dir, registered_names, exact_only=True)
        if exact_js:
            group_name, _path, script_name, registered = exact_js
            task["action"] = "script"
            note = (
                f"「{target}」是 JS 脚本组「{group_name}」（脚本：{script_name}），已按整脚本任务处理"
            )
            notices.append(
                f"🔄 「{target}」不是跑图路线，已改判为 JS 脚本组「{group_name}」"
                f"（脚本：{script_name}；已在一条龙登记：{'是' if registered else '否'}）。"
            )
            reason = str(task.get("reason") or "")
            task["reason"] = f"{reason}（{note}）" if reason else note
            continue

        found = category_for_target(target, group_dir, exclude=(action,))
        if found:
            better, matched = found
            task["action"] = better.action
            note = (
                f"原按「{declared.label}」下发，但该类目里没有「{target}」的路线，"
                f"已自动改判为「{better.label}」"
            )
            notices.append(
                f"🔄 「{target}」在「{declared.label}」里没有路线，已改判为「{better.label}」（命中 {matched} 条）。"
            )
        else:
            js_hit = find_js_script_group(target, group_dir, registered_names)
            if not js_hit:
                continue
            group_name, _path, script_name, registered = js_hit
            task["action"] = "script"
            note = (
                f"原按「{declared.label}」下发，但「{target}」是 JS 脚本组「{group_name}」"
                f"（脚本：{script_name}），已自动改判"
            )
            notices.append(
                f"🔄 「{target}」不是跑图路线，已改判为 JS 脚本组「{group_name}」"
                f"（脚本：{script_name}；已在一条龙登记：{'是' if registered else '否'}）。"
            )

        reason = str(task.get("reason") or "")
        task["reason"] = f"{reason}（{note}）" if reason else note
    return notices

# 匹配用的字段：folderName 对所有组都有；敌人组的敌人名也常出现在脚本文件名里
DEFAULT_MATCH_FIELDS = ("folderName",)
ENEMY_MATCH_FIELDS = ("folderName", "name")


class RouteGroupResult(NamedTuple):
    """一次开关操作的结果。"""

    path: str
    name: str
    matched: int          # 命中目标的路线数
    enabled: int          # 最终 Enabled 的路线总数（含防闪退隔离带）
    total: int
    forced: tuple         # 被防闪退强制打开的脚本名


def load_group(path):
    """读一个脚本组 json；不存在或格式不对时返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        return None
    return data


def project_haystack(project, fields=DEFAULT_MATCH_FIELDS):
    """把参与匹配的字段拼成一个字符串（仅用于展示）。"""
    return " ".join(str(project.get(field) or "") for field in fields)


def _value_tokens(value):
    """把 folderName 拆成可比较的片段。

    BetterGI 里的 folderName 形如 `骗骗花\\骗骗花@san`（目录\\路线包@作者），
    材料名反查出来的敌人叫「电气骗骗花」，只有拆成 `骗骗花` 才能对上。
    """
    text = str(value or "")
    if not text:
        return []
    tokens = [text]
    for part in re.split(r"[\\/]", text):
        part = part.strip()
        if not part:
            continue
        tokens.append(part)
        if "@" in part:
            tokens.append(part.split("@", 1)[0].strip())
    return [token for token in tokens if token]


def _value_matches(value, targets, both_ways):
    tokens = _value_tokens(value)
    if not tokens:
        return False
    for item in targets:
        item = str(item)
        if not item:
            continue
        for token in tokens:
            if item in token:
                return True
            # 双向包含：材料名反查出来的敌人叫「电气骗骗花」，而组里的目录只写「骗骗花」
            if both_ways and len(token) >= 2 and token in item:
                return True
    return False


def _is_pathing_project(project):
    """只有 type=Pathing 的项目才是"跑图路线"。

    踩过的坑：狗粮那些 JS 脚本组里的项目 type 是 Javascript，
    如果它们也参与路线匹配，`狗粮`/`采集水下` 就会被某个"路线"命中，从而判成跑图任务。
    """
    type_ = str((project or {}).get("type") or "").strip().lower()
    return not type_ or type_ == "pathing"


def matches_targets(
    project, targets, fields=DEFAULT_MATCH_FIELDS, both_ways=False, own_prefix=None, skip_tokens=()
):
    """路线是否命中任一目标。

    both_ways=False（地图素材原语义）：目标名是 folderName 的子串。
    both_ways=True（敌人/锄地/矿物/食材）：再允许 folderName 是目标名的子串。
    JS 脚本项目（type=Javascript）一律不算路线。

    own_prefix（类目隔离）：只认本类目前缀下的路线。锄地专区里有叫「飞萤」「骗骗花」的
    子目录和路线，敌人与魔物里也有同名的魔物目录 —— 不隔离就会互相抢路线。
    skip_tokens：folderName 落在"低效/不跑"子目录里的路线默认不选。
    """
    if not _is_pathing_project(project):
        return False
    if is_foreign_project(project, own_prefix):
        return False
    if folder_has_skip_token((project or {}).get("folderName"), skip_tokens):
        return False
    return any(
        _value_matches(project.get(field), targets, both_ways) for field in fields
    )


def group_size(group):
    return len((group or {}).get("projects") or [])


def archive_path_for(group_path):
    """完整清单的归档路径（和脚本组同目录下的隐藏子目录）。"""
    group_path = str(group_path)
    return os.path.join(
        os.path.dirname(group_path), ARCHIVE_DIR_NAME, os.path.basename(group_path)
    )


def load_full_group(group_path):
    """拿到"完整清单"的组数据。

    返回 (group, 来源路径, 是否来自归档)。Agent 精简过组之后，UI 里那份只剩本次要打的
    路线，真正的全量清单在这里，后续选路线都从归档里挑。
    """
    archive = archive_path_for(group_path)
    archived = load_group(archive)
    if archived:
        return archived, archive, True
    return load_group(group_path), str(group_path), False


def shrink_to_matches(
    group, targets, fields=DEFAULT_MATCH_FIELDS, both_ways=False, own_prefix=None, skip_tokens=()
):
    """把组精简成"只有命中目标的路线"，全部 Enabled。

    为什么要精简：BetterGI 会为每条**被禁用**的脚本写一行日志；组里躺着几百条 Disabled
    时，一旦在跑的过程中按停止快捷键，日志会瞬间爆发（实测 638/773 行/秒），
    随后在 WPF 层栈溢出崩溃（0xc00000fd / System.StackOverflowException）。
    精简后组里全是 Enabled，既不产生禁用日志，也不再需要防闪退隔离带。

    保留原组的 index / name / config，只替换 projects。
    """
    matched = [
        dict(project)
        for project in (group.get("projects") or [])
        if matches_targets(project, targets, fields, both_ways, own_prefix, skip_tokens)
    ]
    for project in matched:
        project["status"] = "Enabled"

    shrunk = {key: value for key, value in (group or {}).items() if key != "projects"}
    shrunk.pop("_path", None)
    shrunk["projects"] = matched
    return shrunk


def is_shrunk(group, source_is_archive):
    """判断当前组文件是不是已经被 Agent 精简过（归档存在即视为已精简）。"""
    return bool(source_is_archive)


def is_oversized(group, max_size=None):
    """组是否超过安全体量（超过就不要放进一条龙跑）。"""
    limit = config.BGI_MAX_ROUTE_GROUP_SIZE if max_size is None else max_size
    return group_size(group) > limit


def oversized_warning(group, path=None, max_size=None):
    """超大组的告警文案（含实测到的崩溃原因）。"""
    limit = config.BGI_MAX_ROUTE_GROUP_SIZE if max_size is None else max_size
    name = (group or {}).get("name") or os.path.basename(str(path or ""))
    return (
        f"🚫 路线组「{name}」有 {group_size(group)} 条路线，超过安全上限 {limit}，本次不启用它。\n"
        f"   ↳ 原因：BetterGI 会为每条被禁用脚本写一行日志，超大组会让日志框瞬间涌入上千行，"
        f"停止任务时在 WPF 层栈溢出崩溃（实测 0xc00000fd / System.StackOverflowException）。\n"
        f"   ↳ 建议按敌人拆成小组（例如 蕈兽 / 骗骗花 / 刀谭），或把 {name} 在一条龙里取消勾选。\n"
        f"   ↳ 确实要用大组：在 .env 里设 BGI_ALLOW_LARGE_ROUTE_GROUP=1（风险自负）。"
    )


def rank_groups_for_targets(hits, max_size=None):
    """按"安全优先 → 命中多优先 → 体积小优先"排序候选组。"""
    limit = config.BGI_MAX_ROUTE_GROUP_SIZE if max_size is None else max_size

    def key(item):
        path, group, matched = item
        return (is_oversized(group, limit), -matched, group_size(group))

    return sorted(hits, key=key)


def force_enable_band_interval():
    """防闪退隔离带的间隔（0 = **不用隔离带**）。

    两个配置项（Studio「配置」页 / .env 都能改）：
      · `BGI_FORCE_ENABLE_BAND=0` → 完全不插隔离带，未命中的路线一律 Disabled；
      · `BGI_FORCE_ENABLE_AFTER_DISABLED=N` → 每连续 N 条 Disabled 强制打开一条（默认 150）。

    玩家要求做成开关，是为了**自己验证"连续禁用会不会真的触发 BGI 的 bug"**：
    关掉之后日志里就不会再有"穿插其它材料"的路线；同时资源冷却也会切回"简单模式"
    （跑过任意一条就算采过 —— 这正是关闭隔离带时该有的语义，见 skills/gather_cooldown.py）。

    ⚠️ 已知的真实风险（实测，与阈值无关）：BGI 会给每条 Disabled 写一行日志，几百条的大组在
    跑一半按停止时日志会瞬间爆发（实测 550~700 行/秒），随后可能在 WPF 层栈溢出崩溃
    （0xc00000fd）。关掉隔离带后这个风险回到原始状态，请自行评估。
    """
    if not getattr(config, "BGI_FORCE_ENABLE_BAND", True):
        return 0
    interval = getattr(config, "BGI_FORCE_ENABLE_AFTER_DISABLED", FORCE_ENABLE_AFTER_DISABLED)
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = FORCE_ENABLE_AFTER_DISABLED
    return max(0, interval)


def apply_targets(
    group, targets, fields=DEFAULT_MATCH_FIELDS, both_ways=False, own_prefix=None, skip_tokens=()
):
    """按目标开关组内路线，返回 RouteGroupResult（就地修改 group）。

    命中 → Enabled；未命中 → Disabled。防闪退隔离带是否插入、间隔多少，由
    `force_enable_band_interval()` 决定（`BGI_FORCE_ENABLE_BAND=0` 就完全不插）。
    """
    projects = group.get("projects") or []
    matched = enabled = consecutive_disabled = 0
    forced = []
    interval = force_enable_band_interval()

    for project in projects:
        if matches_targets(project, targets, fields, both_ways, own_prefix, skip_tokens):
            project["status"] = "Enabled"
            matched += 1
            enabled += 1
            consecutive_disabled = 0
            continue

        if interval and consecutive_disabled >= interval:
            project["status"] = "Enabled"
            enabled += 1
            consecutive_disabled = 0
            forced.append(project.get("name") or "")
        else:
            project["status"] = "Disabled"
            consecutive_disabled += 1

    return RouteGroupResult(
        path=str(group.get("_path", "")),
        name=str(group.get("name") or ""),
        matched=matched,
        enabled=enabled,
        total=len(projects),
        forced=tuple(forced),
    )


def list_group_files(group_dir):
    """列出目录下所有脚本组文件（按文件名排序，结果稳定）。"""
    try:
        names = sorted(os.listdir(group_dir))
    except OSError:
        return []
    return [os.path.join(group_dir, name) for name in names if name.lower().endswith(".json")]


def count_matches(
    group, targets, fields=DEFAULT_MATCH_FIELDS, both_ways=False, own_prefix=None, skip_tokens=()
):
    return sum(
        1
        for project in (group.get("projects") or [])
        if matches_targets(project, targets, fields, both_ways, own_prefix, skip_tokens)
    )


def find_groups_for_targets(
    group_dir,
    targets,
    fields=ENEMY_MATCH_FIELDS,
    exclude=(),
    both_ways=False,
    own_prefix=None,
    skip_tokens=(),
):
    """在目录里找包含这些目标的脚本组，返回 [(路径, 组数据)]。

    用途：玩家说的敌人没有专门的「敌人与魔物」组时，退回到按敌人名找人自己
    建过的那些组（例如 蕈兽.json / 骗骗花.json / 刀谭.json）。
    """
    hits = []
    excluded = {os.path.abspath(path) for path in exclude if path}
    for path in list_group_files(group_dir):
        if os.path.abspath(path) in excluded:
            continue
        group = load_group(path)
        if not group:
            continue
        if count_matches(group, targets, fields, both_ways, own_prefix, skip_tokens):
            group["_path"] = path
            hits.append((path, group))
    return hits


def describe_result(result, target_text):
    """给终端/审批屏看的一行说明。"""
    text = f"「{result.name}」共 {result.total} 条路线，命中「{target_text}」{result.matched} 条，当前启用 {result.enabled} 条"
    if result.forced:
        text += f"（含防闪退隔离带 {len(result.forced)} 条）"
    return text


def _is_known_plan_target(name):
    """名字是否已经是脚本能处理的目标（首领 / 已知角色）。

    这两类**不能**被改判成 hunt。踩过的坑：「秘源机兵构型械」是货真价实的首领，
    但敌人与魔物组里也有「秘源机兵」的杂兵路线，不先判 Boss 就会把真 Boss 抢走。
    """
    name = str(name or "").strip()
    if not name:
        return False

    try:
        from skills.boss_pathing_guard import validate_boss_target

        try:
            validate_boss_target(name)
            return True
        except Exception:
            pass
    except Exception:
        pass

    try:
        from skills.char_boss_match import find_character

        if find_character(name):
            return True
    except Exception:
        pass

    return False


def find_enemy_route_target(
    target, group_dir=None, preferred_path=None, drops_path=None, known_target_check=True
):
    """判断一个名字是否属于「敌人与魔物」路线，返回 (组路径, 组数据, 命中数) 或 None。

    用途：LLM 会把「异种合成魔兽」「圣骸兽」这类**敌人路线名**当成 Boss（run_boss），
    走到 Boss 守卫那里就会被判"名称无法对齐"而整轮作废。这里先认出来是敌人路线，
    再由调用方改判成 hunt。

    ⚠️ 已知首领 / 已知角色直接返回 None：它们走原本的 run_boss 通路，
    不能被敌人组里的相近名字抢走。
    """
    target = str(target or "").strip()
    if not target:
        return None
    if known_target_check and _is_known_plan_target(target):
        return None

    group_dir = group_dir or config.BGI_SCRIPT_GROUP_DIR
    preferred_path = preferred_path or config.BGI_ENEMY_CONFIG

    preferred = load_group(preferred_path)
    if preferred:
        matched = count_matches(
            preferred, [target], ENEMY_MATCH_FIELDS, both_ways=True
        )
        if matched:
            return preferred_path, preferred, matched

    excluded = (config.BGI_MAP_CONFIG, preferred_path)
    for terms in ([target], [target, *enemy_names_from_drops(target, drops_path or _drops_path())]):
        if len(terms) > 1 and terms[1:] == []:
            continue
        hits = find_groups_for_targets(
            group_dir, terms, ENEMY_MATCH_FIELDS, exclude=excluded, both_ways=True
        )
        if hits:
            path, group = hits[0]
            return path, group, count_matches(group, terms, ENEMY_MATCH_FIELDS, both_ways=True)
    return None


def _drops_path():
    try:
        from skills.char_boss_match import BOSS_DROPS_PATH

        return BOSS_DROPS_PATH
    except Exception:  # pragma: no cover - 字典模块缺失时降级
        return "memory/boss_drops_dict.json"


def redirect_run_boss_to_hunt(bgi_cmd, group_dir=None, preferred_path=None, drops_path=None):
    """把被误判成 run_boss 的敌人路线改判成 hunt（就地改写 bgi_cmd）。

    命中则清空 energy_task、往 free_task 追加一条 hunt，并返回提示文案；
    没命中（真的不是敌人路线）返回 None，交给调用方按原逻辑报错。
    """
    if not isinstance(bgi_cmd, dict):
        return None

    energy_task = bgi_cmd.get("energy_task")
    if not isinstance(energy_task, dict) or energy_task.get("action") != "run_boss":
        return None

    target = str(energy_task.get("target") or "").strip()
    hit = find_enemy_route_target(target, group_dir, preferred_path, drops_path)
    if not hit:
        return None

    path, group, matched = hit
    free_tasks = bgi_cmd.get("free_task")
    if not isinstance(free_tasks, list):
        free_tasks = []
    free_tasks = [task for task in free_tasks if isinstance(task, dict)]
    free_tasks.append(
        {
            "action": "hunt",
            "target": target,
            "reason": (
                f"「{target}」命中敌人路线组「{group.get('name')}」，"
                f"它不是首领挑战，已自动改判为敌人讨伐（不消耗体力）。"
            ),
        }
    )
    bgi_cmd["free_task"] = free_tasks
    bgi_cmd["energy_task"] = {}

    return (
        f"🔄 「{target}」是敌人路线（组「{group.get('name')}」，命中 {matched} 条），"
        f"已从 Boss 讨伐改判为敌人讨伐，不再走 run_boss。"
    )


def available_group_names(group_dir, limit=None):
    """目录里可用的脚本组名（给"找不到组"时的提示用）。"""
    names = []
    for path in list_group_files(group_dir):
        group = load_group(path)
        names.append(str((group or {}).get("name") or os.path.basename(path)))
    return names if limit is None else names[:limit]


# ==========================================
# 🌟 自动建组：锄地专区 400+ 条，手工建组太折磨
# ==========================================
def route_files_under(root):
    """列出目录下（含子目录）的路线 json：返回 [(相对目录, 文件名)]，按路径排序。"""
    rows = []
    root = str(root or "")
    if not root or not os.path.isdir(root):
        return rows
    for current, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            if not name.lower().endswith(".json"):
                continue
            relative = os.path.relpath(current, root)
            relative = "" if relative == "." else relative.replace("/", "\\")
            rows.append((relative, name))
    return rows


def build_category_projects(category, pathing_root=None):
    """扫 `User\\AutoPathing\\<类目>` 生成 projects 列表（BetterGI 脚本组的项目结构）。

    ⚠️ 这里**不过滤低效路线** —— 组里放全量清单，用的时候才按 skip_tokens 跳过，
    这样玩家哪天说"这次连低效一起跑"也能立刻跑起来。
    """
    root = pathing_root or config.BGI_AUTO_PATHING_DIR
    prefix = str(getattr(category, "folder_prefix", "") or "").strip()
    if not prefix:
        return []

    projects = []
    for relative, name in route_files_under(os.path.join(root, prefix)):
        folder_name = f"{prefix}\\{relative}" if relative else prefix
        projects.append(
            {
                "name": name,
                "folderName": folder_name,
                "jsScriptSettingsObject": None,
                "index": len(projects) + 1,
                "type": "Pathing",
                "status": "Disabled",
                "schedule": "Daily",
                "runNum": 1,
                "allowJsNotification": True,
                "allowJsHTTPHash": "",
            }
        )
    return projects


def next_group_index(group_dir):
    """新的脚本组 index（BetterGI 用它排序，取现有最大值 +1）。"""
    biggest = 0
    for path in list_group_files(group_dir):
        group = load_group(path)
        if not group:
            continue
        try:
            biggest = max(biggest, int(group.get("index") or 0))
        except (TypeError, ValueError):
            continue
    return biggest + 1


def template_group_config(group_dir, prefer=()):
    """从已有脚本组抄一份 config（pathingConfig/shellConfig），保证字段齐全。

    优先抄**战斗策略合法**的组（`根据队伍自动选择` 或 User\\AutoFight 里真有那个 txt）：
    实测十几个组配的 `1.四神挂机[推荐]` 在 User\\AutoFight 下并不存在，抄过来会让
    新组一走到怪点就抛「战斗策略文件不存在」。
    """
    candidates = list(prefer) + list_group_files(group_dir)
    fallback = None
    for path in candidates:
        if not path or not os.path.isfile(str(path)):
            continue
        group = load_group(path)
        if not group:
            continue
        config_data = group.get("config")
        if not isinstance(config_data, dict) or not config_data.get("pathingConfig"):
            continue
        if not any(_is_pathing_project(project) for project in (group.get("projects") or [])):
            continue
        if fallback is None:
            fallback = copy.deepcopy(config_data)
        if not group_strategy_notice(group):
            return copy.deepcopy(config_data)
    return fallback


def ensure_category_group(category, group_dir=None, pathing_root=None):
    """类目的总组不存在时，按 `User\\AutoPathing\\<类目>` 自动生成一份。

    返回 (路径, 组数据, 提示文案)；不需要生成（已存在 / 未开启 / 没路线文件）时返回 None。
    调用方负责落盘（走配置事务，可回滚）。
    """
    if not getattr(category, "auto_create", False):
        return None
    if not config.BGI_AUTO_CREATE_ROUTE_GROUP:
        return None

    directory = group_dir or config.BGI_SCRIPT_GROUP_DIR
    path = category_group_path(category, directory)
    if os.path.exists(path):
        return None

    projects = build_category_projects(category, pathing_root)
    if not projects:
        return None

    name = os.path.splitext(os.path.basename(path))[0]
    prefer = [
        os.path.join(directory, config.BGI_ENEMY_CONFIG_NAME),
        os.path.join(directory, config.BGI_MAP_CONFIG_NAME),
    ]
    group = {
        "index": next_group_index(directory),
        "name": name,
        "config": template_group_config(directory, prefer) or {},
        "projects": projects,
    }
    notice = (
        f"🧩 调度器里还没有「{name}」脚本组，已按 {os.path.join(pathing_root or config.BGI_AUTO_PATHING_DIR, category.folder_prefix)} "
        f"自动生成：共 {len(projects)} 条{category.label}路线（文件名 {os.path.basename(path)}）。\n"
        f"   ↳ 路线按类目目录前缀隔离：锄地专区的「飞萤 / 骗骗花」等路线不会被「敌人与魔物」抢走，反之亦然。\n"
        f"   ↳ 启用时 bgi_controller 会自动把它登记进一条龙（TaskDefinitions 补一条新 Id），不用手工添加。"
    )
    return path, group, notice



def enemy_names_from_drops(material, drops_path):
    """材料名 → 会掉落它的敌人名列表（用于把「刷点花蜜」翻译成敌人组）。

    数据源是 memory/boss_drops_dict.json（键是敌首/敌怪名，值是掉落物）。
    """
    if not material:
        return []
    try:
        with open(drops_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    material = str(material).strip()
    hits = [
        str(enemy)
        for enemy, drops in data.items()
        if isinstance(drops, Sequence)
        and not isinstance(drops, str)
        and any(str(drop) == material for drop in drops)
    ]
    return hits


# ==========================================
# 🌟 审批屏摘要：本轮到底要跑什么
# ==========================================
FREE_TASK_LABELS = (
    ("gather", "采集目标", "🌿"),
    ("hunt", "敌人讨伐", "👹"),
    ("hoe", "锄大地", "🗺️"),
    ("mine", "矿物", "⛏️"),
    ("cook", "食材与炼金", "🍳"),
    ("script", "整脚本任务", "🎬"),
)


def plan_summary_lines(energy_task, free_tasks, group_dir=None, registered_names=()):
    """审批屏/审批文本用的「本轮将执行」摘要。

    ⚠️ 必须把 `script`（整脚本任务，如狗粮）也列出来 —— 否则玩家在审批时看到的
    只有体力/采集/敌人/矿物/食材，压根看不出这一轮到底要跑什么。

    `script` 这一类还会把解析结果写上：命中哪个脚本组、跑的是哪个脚本、
    以及**该组有没有登记进一条龙**（没登记的话写进配置也不会被执行，等于白跑）。
    """
    energy_task = energy_task if isinstance(energy_task, dict) else {}
    free_tasks = [t for t in (free_tasks or []) if isinstance(t, dict)]

    target = str(energy_task.get("target") or "").strip()
    lines = [f"⚔️ 体力目标：{target or '无'}"]

    for action, label, icon in FREE_TASK_LABELS:
        items = [
            str(t.get("target")).strip()
            for t in free_tasks
            if t.get("action") == action and str(t.get("target") or "").strip()
        ]
        if not items:
            continue

        if action == "script":
            described = []
            for item in items:
                hit = find_js_script_group(item, group_dir, registered_names)
                if not hit:
                    described.append(f"{item}（⚠️ 没找到对应的 JS 脚本组）")
                    continue
                group_name, _path, script_name, registered = hit
                state = "已登记一条龙" if registered else "未登记：执行时会自动登记"
                described.append(f"{item} → 组「{group_name}」/ 脚本「{script_name}」（{state}）")
            lines.append(f"{icon} {label}：" + "，".join(described))
        else:
            lines.append(f"{icon} {label}：" + "、".join(items))

    if len(lines) == 1:
        lines.append("🌿 采集目标：无")
    return lines
