"""Boss 讨伐目标守卫：名称对齐 + 寻路能力探测 + 战斗策略探测。

为什么需要它（2026-09-10 BetterGI 日志复盘）：
    BetterGI 的 JS 脚本《批量讨伐角色养成材料BOSS》用
    `assets/Pathing/${boss.name}前往.json` 这个名字去读路径文件。
    名字只要差一个字符，runFile 就会读不到文件、把空字符串交给
    AutoPathingScript.Run("")，最终日志里只剩一句
    "The input does not contain any JSON tokens" —— 用户看到的现象就是"无法寻路"。

    实测翻车案例：配置写的是「秘源机兵构型械」（LLM 照抄了玩家漏打「·」的输入），
    脚本里叫「秘源机兵·构型械」，于是 5 次讨伐全部卡在
        ReadText 异常: Could not find file '...\assets\Pathing\秘源机兵构型械前往.json'
    所以写 config.json 之前，必须先把名字逐字对齐到脚本自己的名单上。

数据来源（按可信度排序）：
    1. `assets/Pathing/` 目录里的文件名 —— 决定"到底能不能寻路"的唯一事实；
    2. `assets/config/boss-list.json` —— 脚本自带名单，含「（不支持）」标注；
    3. `memory/boss_drops_dict.json` —— 兜底，仅用于把名字纠回官方写法。
"""

import difflib
import json
import os

import config
from skills.config_transaction import ConfigTransactionError

# 脚本读取路径文件时用的后缀（顺序重要：长的必须排在前面）
_PATH_SUFFIXES = ("战斗后快速前往", "键鼠前往", "强制传送", "前往")
# 需要"键鼠宏录像"兜底的后缀：这类 Boss 所在分层地图未适配自动寻路
_KEYMOUSE_SUFFIXES = ("键鼠前往", "强制传送")
# 名字里的分隔符：比较时一律忽略（「秘源机兵·构型械」== 「秘源机兵构型械」）
_NAME_SEPARATORS = "·・•‧∙⋅-－—_ 　\t"
# 脚本名单里对不支持 Boss 的标注
_UNSUPPORTED_MARKERS = ("（不支持）", "(不支持)")
# AutoFightParam 的合法特殊值：交给 BetterGI 按当前队伍自动挑策略
_SPECIAL_STRATEGIES = {"", "根据队伍自动选择"}


def _script_dir(script_dir=None):
    """脚本《批量讨伐角色养成材料BOSS》的根目录。"""
    if script_dir:
        return str(script_dir)
    # BGI_BOSS_CONFIG 指向 .../批量讨伐角色养成材料BOSS/assets/config/config.json
    return os.path.dirname(os.path.dirname(os.path.dirname(config.BGI_BOSS_CONFIG)))


def _pathing_dir(script_dir=None):
    return os.path.join(_script_dir(script_dir), "assets", "Pathing")


def _boss_list_path(script_dir=None):
    return os.path.join(_script_dir(script_dir), "assets", "config", "boss-list.json")


def _drops_dict_path(dict_path=None):
    # 绝对路径：工作目录不是仓库根时，相对路径会指到别处（见 config.PROJECT_ROOT 的注释）
    return dict_path or config.project_path("memory", "boss_drops_dict.json")


def normalize_boss_name(name):
    """把 Boss 名压成可比较的形式：去掉分隔符、标注与空白。"""
    text = str(name or "")
    for marker in _UNSUPPORTED_MARKERS:
        text = text.replace(marker, "")
    for ch in _NAME_SEPARATORS:
        text = text.replace(ch, "")
    return text.strip().lower()


def _strip_path_suffix(filename):
    """从路径文件名里取出 Boss 名与类别，取不出就返回 (None, None)。"""
    if not filename.lower().endswith(".json"):
        return None, None
    stem = filename[: -len(".json")]
    for suffix in _PATH_SUFFIXES:
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)], suffix
    return None, None


def collect_pathing_index(script_dir=None):
    """扫描 assets/Pathing，返回 {Boss名: {"routable": bool, "keymouse": bool, "files": [...]}}。"""
    index = {}
    pathing_dir = _pathing_dir(script_dir)
    try:
        entries = os.listdir(pathing_dir)
    except OSError:
        return index

    for entry in entries:
        boss_name, suffix = _strip_path_suffix(entry)
        if not boss_name:
            continue
        item = index.setdefault(boss_name, {"routable": False, "keymouse": False, "files": []})
        item["files"].append(entry)
        if suffix in _KEYMOUSE_SUFFIXES:
            item["keymouse"] = True
        else:
            item["routable"] = True
    return index


def load_script_boss_list(script_dir=None):
    """读取脚本自带的 boss-list.json。

    返回 (supported: list[str], unsupported: set[str])；文件缺失时返回 ([], set())。
    """
    try:
        with open(_boss_list_path(script_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return [], set()

    supported, unsupported = [], set()
    for region_names in (data.get("bossList") or {}).values():
        if not isinstance(region_names, list):
            continue
        for raw in region_names:
            name = str(raw)
            if any(marker in name for marker in _UNSUPPORTED_MARKERS):
                unsupported.add(normalize_boss_name(name))
            else:
                supported.append(name.strip())
    for raw in data.get("unsupportedBosses") or []:
        unsupported.add(normalize_boss_name(raw))
    return supported, unsupported


def load_fallback_boss_names(dict_path=None):
    """兜底名单：memory/boss_drops_dict.json 的键（官方写法）。"""
    try:
        with open(_drops_dict_path(dict_path), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [str(key) for key in data] if isinstance(data, dict) else []


def _match_candidate(raw_name, candidates):
    """在候选名单里找 raw_name 的官方写法，返回 (官方名, 匹配方式) 或 (None, None)。"""
    if not candidates:
        return None, None

    for candidate in candidates:
        if candidate == raw_name:
            return candidate, "exact"

    target = normalize_boss_name(raw_name)
    if not target:
        return None, None

    normalized = {}
    for candidate in candidates:
        normalized.setdefault(normalize_boss_name(candidate), candidate)
    if target in normalized:
        return normalized[target], "normalized"

    # 唯一子串匹配：LLM 可能少写/多写后半段（如「无相之岩」→「无相」）
    substrings = [key for key in normalized if key and (target in key or key in target)]
    if len(substrings) == 1:
        return normalized[substrings[0]], "substring"

    close = difflib.get_close_matches(target, list(normalized), n=1, cutoff=0.75)
    if close:
        return normalized[close[0]], "fuzzy"
    return None, None


def _suggestions(raw_name, candidates, limit=5):
    target = normalize_boss_name(raw_name)
    normalized = {}
    for candidate in candidates:
        normalized.setdefault(normalize_boss_name(candidate), candidate)
    close = difflib.get_close_matches(target, list(normalized), n=limit, cutoff=0.4)
    return [normalized[key] for key in close]


def validate_boss_target(raw_name, script_dir=None, dict_path=None):
    """校验并规范化 Boss 讨伐目标名。

    返回 (官方名, notices)；无法安全执行时抛 ConfigTransactionError（调用方的事务
    因此不会写入任何东西，也就不会白跑一趟或先把 BetterGI 杀掉）。

    校验顺序：脚本是否标注不支持 → 名字能否对齐 → 是否存在路径文件。

    只有「去掉分隔符后完全一致」这一种情况会自动纠正（如
    「秘源机兵构型械」→「秘源机兵·构型械」）。子串/模糊匹配一律拒绝：
    无相之风/无相之冰/无相之火只差一个字，自动纠错的代价是去打错 Boss，
    所以宁可报错让人确认。
    """
    raw_name = str(raw_name or "").strip()
    if not raw_name or raw_name == "无":
        raise ConfigTransactionError(
            "❌ 没有解析到 Boss 讨伐目标（energy_task.target 为空），已跳过执行。"
        )

    pathing_index = collect_pathing_index(script_dir)
    supported, unsupported = load_script_boss_list(script_dir)

    # 不支持的 Boss 必须最先拦：否则「无相之风」会被模糊匹配纠成「无相之雷」
    if normalize_boss_name(raw_name) in unsupported:
        raise ConfigTransactionError(
            f"❌ '{raw_name}' 被脚本明确标注为「不支持」（脚本里没有任何路径文件）。\n"
            f"   ↳ 已知不支持的 Boss：无相之风、黄金王兽、无相之冰、深海龙蜥之群，只能手动打。\n"
            f"   ↳ 如果你其实是想刷某个角色的突破材料，请直接把【角色名】作为 target"
            f"（如「蓝砚」），系统会用本地字典翻译成正确的 Boss。"
        )

    # 候选池：能寻路的名字优先，其次是脚本名单，最后是掉落字典
    pathing_available = bool(pathing_index)
    candidates = list(pathing_index.keys()) + supported
    name_source_is_dict = False
    if not candidates:
        candidates = load_fallback_boss_names(dict_path)
        name_source_is_dict = bool(candidates)

    if not candidates:
        # 脚本没订阅/没跑过：这里不做判断，交给 bgi_controller 里的目录检查兜底
        return raw_name, ["⚠️ 未找到《批量讨伐角色养成材料BOSS》脚本目录，已跳过 Boss 名称与寻路校验。"]

    canonical, match_kind = _match_candidate(raw_name, candidates)
    notices = []

    if not canonical:
        hints = _suggestions(raw_name, candidates)
        hint_text = "、".join(hints) if hints else "（无可参考的相近名字）"
        raise ConfigTransactionError(
            f"❌ Boss 名称无法对齐到脚本名单：'{raw_name}'\n"
            f"   ↳ 脚本自带的相近名字：{hint_text}\n"
            f"   ↳ 脚本只认 assets/config/boss-list.json 里的官方写法，"
            f"名字差一个字符就会读不到路径文件（日志表现为「无法寻路」）。\n"
            f"   ↳ 如果这其实是【敌人路线】（repo/pathing/敌人与魔物，例如 异种合成魔兽 / 圣骸兽 / "
            f"镀金旅团），请改用 free_task 的 hunt，不要走 run_boss。"
        )

    if match_kind in ("substring", "fuzzy"):
        hints = _suggestions(raw_name, candidates)
        hint_text = "、".join(hints) if hints else "（无可参考的相近名字）"
        raise ConfigTransactionError(
            f"❌ Boss 名称无法确认：'{raw_name}' 只是近似于脚本里的名字，已拒绝执行。\n"
            f"   ↳ 脚本里相近的名字：{hint_text}\n"
            f"   ↳ 请用官方全名重新下达指令，避免打错 Boss。"
        )

    if match_kind != "exact":
        source = "本地掉落字典" if name_source_is_dict else "脚本名单"
        notices.append(f"🔧 Boss 名称已按{source}自动规范化：'{raw_name}' → '{canonical}'")

    if not pathing_available:
        # 脚本没订阅/没跑过：只做了名字纠正，寻路与否交给 bgi_controller 的目录检查兜底
        notices.append(
            f"⚠️ 未找到脚本的路径文件目录（assets/Pathing），无法校验 '{canonical}' "
            f"是否真的能寻路；请先在 BetterGI 中订阅并至少运行一次《批量讨伐角色养成材料BOSS》。"
        )
        return canonical, notices

    info = pathing_index.get(canonical)
    if info is None:
        # 名字能对上名单但没有路径文件：多半是脚本版本旧/名单比路径新
        hints = _suggestions(canonical, list(pathing_index.keys()))
        hint_text = "、".join(hints) if hints else "（无）"
        raise ConfigTransactionError(
            f"❌ '{canonical}' 在脚本里没有可用的路径文件，无法自动寻路。\n"
            f"   ↳ 路径目录里相近的名字：{hint_text}\n"
            f"   ↳ 请在 BetterGI 中把脚本《批量讨伐角色养成材料BOSS》更新到最新版后重试。"
        )

    if info["keymouse"] and not info["routable"]:
        notices.append(
            f"⚠️ '{canonical}' 所在分层地图未适配自动寻路，脚本会用"
            f"「强制传送 + 键鼠宏录像」兜底：需保持 1920×1080 分辨率，"
            f"且行走位建议用成男/成女体型，否则容易跑偏。"
        )
    return canonical, notices


def strategy_notice(strategy_name, auto_fight_dir=None):
    """检查战斗策略名在 BetterGI 那边能否解析成文件。

    真正的解析规则（含"仓库有但没导入"的提示）在 route_group 里，这里只是转发，
    避免两处实现各写一套。
    """
    from skills.route_group import strategy_notice as _notice

    return _notice(strategy_name, auto_fight_dir=auto_fight_dir)
