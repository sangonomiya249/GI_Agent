"""地区特产（采集物）的 48 小时刷新冷却检测。

## 数据从哪来（为什么不自己记账）

BetterGI 每跑完一条地图追踪路线都会打一行：

    → 脚本执行结束: "01-霜仙花-彩冰镇左上-3个.json", 耗时: 0分24.5秒

而地图追踪的路线的文件名格式是固定的 **`NN-材料名-地点-N个.json`**（实测玩家脚本组里
710 条路线都符合，例如 `01-霜仙花-彩冰镇左上-3个.json`、`04-月莲-茸蕈窟-4个.json`）。
所以"哪种材料、什么时候采的"完全可以确定性地从日志里读出来 —— 而且是**唯一**能覆盖
"玩家自己在 BetterGI 里手动跑"的证据（自己记账本会漏掉手动跑的那些）。

两个细节：
  · **失败不算**：紧邻上文有「此追踪脚本未正常走完！」或「任务执行失败」的那条不计入冷却
    （实测玩家日志里 `04-便携轴承-…` 被停止快捷键打断时就是这种）；
  · **跨自然日**：BetterGI 的日志按天切文件（`better-genshin-impactYYYYMMDD.log`），
    48 小时会横跨 3 个文件，所以按天往前扫（默认扫 `ceil(冷却小时/24)+1` 天）。

## 冷却规则

地区特产 48 小时刷新（`GATHER_COOLDOWN_HOURS`，可用 .env 调）。所以：

  · 上次采集距今 ≥ 48 小时 → **刷新了**，正常执行；
  · 还在 48 小时内 → **没刷新**，本次不采集，并告诉玩家还要等多久、上次是什么时候采的。

玩家在游戏里手动采过、日志里没有的，可以 `python -m skills.gather_cooldown --manual 霜仙花`
记一笔（写进 `memory/gather_cooldown_manual.json`）；临时想强跑一次，说「强制采集」即可
（`is_forced()`，会跳过冷却拦截，**不会**改动上面的记录）。
"""

import datetime
import glob
import json
import os
import re

import config

# 路线名：`01-霜仙花-彩冰镇左上-3个.json`
# ⚠️ 前缀不止是纯数字：实测还有 `09A-清心-层岩巨渊-32朵.json`、`A01-清水玉-…`，
#    原来只认 `\d+-`，这些路线的材料就整个读不出来（连带日志解析也漏掉）。
_ROUTE_MATERIAL_RE = re.compile(
    r"^\s*[A-Za-z]{0,2}\d+[A-Za-z]{0,2}\s*[-－—_]\s*([^\-－—_]+?)\s*[-－—_]"
)
# 日志行：→ 脚本执行结束: "01-霜仙花-彩冰镇左上-3个.json", 耗时: ...
_ROUTE_DONE_RE = re.compile(r'脚本执行结束:\s*"([^"]+\.json)"')
_FAILURE_RE = re.compile(r"未正常走完|任务执行失败")
# 新路线开始（用来清掉"上一条路线的失败标记"，避免算到后面那条路上）
_ROUTE_START_RE = re.compile(r"开始执行地图追踪任务")
_TIME_RE = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\.\d+\]")
_LOG_NAME_RE = re.compile(r"better-genshin-impact(\d{8})\.log$")

# 强制采集的说法（玩家明确表示"我知道没刷新，还是要采"）
FORCE_WORDS = (
    "强制采集", "强制跑", "强行采", "无视冷却", "不管冷却", "不用管冷却",
    "我知道没刷新", "我知道没刷", "我知道", "还是要采", "也要采",
)
# 从角色名里剥掉的尾巴
_TARGET_TAIL_RE = re.compile(r"(的)?(普通|角色)?(突破)?材料$")
# 路线总数缓存（材料 → 组里几条路线）；按 组文件 mtime 失效
_ROUTE_TOTALS = {}
_ROUTE_TOTALS_CACHE_KEY = {"key": None}
_ROUTE_TOTALS_FILE = {"path": ""}


def cooldown_hours():
    return getattr(config, "GATHER_COOLDOWN_HOURS", 48)


def scan_days():
    """要往前扫几天日志（48 小时横跨 3 个自然日）。"""
    return int(cooldown_hours() // 24) + 1


def manual_state_path():
    return config.project_path("memory", "gather_cooldown_manual.json")


# ==========================================
# 🌟 日志解析
# ==========================================


def log_paths(days=None):
    """最近几天的 BGI 日志（存在才返回，按时间正序）。"""
    days = scan_days() if days is None else max(1, int(days))
    today = datetime.date.today()
    paths = []
    for offset in range(days):
        stamp = (today - datetime.timedelta(days=offset)).strftime("%Y%m%d")
        path = os.path.join(config.BGI_LOG_DIR, f"better-genshin-impact{stamp}.log")
        if os.path.isfile(path):
            paths.append(path)
    return sorted(paths)


def parse_day(path):
    """解析一天的日志 → [{material, route, at, failed}]。

    ⚠️ BGI 的日志是"时间戳一行、正文一行"，所以要边扫边记住当前时间戳（不能只看匹配行）。
    """
    match = _LOG_NAME_RE.search(os.path.basename(path))
    if not match:
        return []
    day = match.group(1)
    try:
        date = datetime.datetime.strptime(day, "%Y%m%d").date()
    except ValueError:
        return []

    events = []
    current = None
    pending_failure = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []

    for line in lines:
        text = line.strip()
        stamp = _TIME_RE.match(text)
        if stamp:
            hour, minute, second = (int(value) for value in stamp.groups())
            current = datetime.time(hour, minute, second)

        # ⚠️ 失败标记（「此追踪脚本未正常走完！」）本身不带路线名，只能按"紧邻下文"归属：
        #    它作用于**紧接着的下一条**路线结束行；新路线一开始就作废（避免算到后面的路线上）。
        if _FAILURE_RE.search(text):
            pending_failure = True
            continue
        if _ROUTE_START_RE.search(text):
            pending_failure = False
            continue

        found = _ROUTE_DONE_RE.search(text)
        if not found or current is None:
            continue
        route_file = os.path.basename(found.group(1))
        events.append({
            "material": material_for_route(route_file),
            "route": route_file,
            "at": datetime.datetime.combine(date, current),
            "failed": pending_failure,
        })
        pending_failure = False
    return events


def scan_events(days=None):
    """最近几天的全部路线完成事件（含失败的，按时间正序）。"""
    events = []
    for path in log_paths(days):
        events.extend(parse_day(path))
    events.sort(key=lambda item: item["at"])
    return events


# ==========================================
# 🌟 手动记录（玩家自己在游戏里采过）
# ==========================================


def load_manual():
    """{材料名: ISO 时间} —— 玩家手动登记过的"刚采过"。"""
    try:
        with open(manual_state_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def save_manual(state):
    target = manual_state_path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return True
    except OSError as exc:
        print(f"⚠️ 冷却记录写入失败：{exc}")
        return False


def mark_manual(material, when=None):
    """手动记为"刚采过"（游戏里自己采的，日志看不到）。"""
    state = load_manual()
    state[str(material)] = (when or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    return save_manual(state)


def clear_manual(material=None):
    """清掉手动记录（重新以日志为准）。"""
    state = load_manual()
    if material is None:
        state = {}
    else:
        state.pop(str(material), None)
    return save_manual(state)


# ==========================================
# 🌟 冷却状态
# ==========================================


def manual_collected(material):
    """玩家手动登记的「刚采过」时间；没有记录或格式不对返回 None。"""
    raw = load_manual().get(str(material or "").strip())
    if not raw:
        return None
    try:
        return datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def last_collected(material, days=None):
    """某种材料最近一次**成功**采集的时间；没有记录返回 None。"""
    material = str(material or "").strip()
    if not material:
        return None

    latest = None
    for event in scan_events(days):
        if event["failed"] or event["material"] != material:
            continue
        if latest is None or event["at"] > latest:
            latest = event["at"]

    when = manual_collected(material)
    if when and (latest is None or when > latest):
        latest = when
    return latest


def _route_totals_cache():
    return _ROUTE_TOTALS


def route_totals():
    """{材料名: 脚本组里这种材料一共有几条路线}（按组文件 mtime 缓存）。

    为什么要它：**防闪退隔离带**会给"没被点名"的材料也打开一条路线（每连续 150 条 Disabled
    强制开一条）。玩家实测的坑：只跑了 1 条隔离带的路线，整种材料就被算成"采过了"、白等 48 小时
    （万相石 1/16、晶化骨髓 1/6、琉鳞石 1/6、星螺 1/5）。所以必须拿"跑了几条 / 一共几条"来判。
    """
    candidates = _collect_group_paths()
    key = tuple(
        (path, (os.path.getmtime(path) if os.path.isfile(path) else None))
        for path in candidates
    )
    if _ROUTE_TOTALS_CACHE_KEY.get("key") == key:
        return _ROUTE_TOTALS

    totals = {}
    try:
        from skills import route_group
    except Exception:        # noqa: BLE001
        return _ROUTE_TOTALS

    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        group = route_group.load_group(path) or {}
        for project in group.get("projects") or []:
            material = material_from_project(project)
            if material and _looks_like_material(material):
                totals[material] = totals.get(material, 0) + 1

    _ROUTE_TOTALS.clear()
    _ROUTE_TOTALS.update(totals)
    _ROUTE_TOTALS_CACHE_KEY["key"] = key
    return _ROUTE_TOTALS


def session_routes(material, latest, window_hours=3):
    """上一次"采集那一趟"里，这种材料成功跑完的**不同路线**数（去重）。"""
    if latest is None:
        return 0
    window = latest - datetime.timedelta(hours=window_hours)
    routes = set()
    for event in scan_events():
        if event["failed"] or event["material"] != material:
            continue
        if window <= event["at"] <= latest:
            routes.add(event["route"])
    return len(routes)


def _minimum_ratio():
    percent = getattr(config, "GATHER_COOLDOWN_MIN_ROUTE_PERCENT", 80)
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        percent = 80.0
    return max(1.0, min(100.0, percent)) / 100.0


def band_enabled():
    """防闪退隔离带开着吗？（见 config.BGI_FORCE_ENABLE_BAND）

    它决定了"部分采集"要不要特殊对待：
      · 开着 → 组里会穿插"没被点名的材料"的路线，只跑了 1 条很可能就是隔离带顺带跑的，
        必须按"跑了几条/一共几条"判断，否则整种材料被误记成"采过"（玩家实测的坑）；
      · 关着 → 不会穿插，跑过的就是真的要采的，退回**正常冷却**（跑过任意一条即算采过）。
    """
    return bool(getattr(config, "BGI_FORCE_ENABLE_BAND", True))


def status(material, now=None):
    """单种材料的冷却状态。

    返回 {material, last_at, hours_ago, hours_left, cooling, known, total_routes, ran_routes,
          partial, manual}
      · cooling=True → 还在冷却，不该采集；
      · known=False  → 没有任何采集记录（当作"刷新了"，可以采）；
      · partial=True → 上次只跑了这种材料的一部分路线（多半是**防闪退隔离带**顺带跑的，
                       或中途停了）→ **不算采完**，可以继续采。
                       只在隔离带开着时才会判（关掉隔离带就是"正常冷却"）。
      · manual=True  → 这次记录来自**手动登记**（游戏里自己采的），不算"部分采集"。
    """
    now = now or datetime.datetime.now()
    material = str(material or "").strip()
    last = last_collected(material)
    result = {
        "material": material,
        "last_at": None,
        "hours_ago": None,
        "hours_left": 0.0,
        "cooling": False,
        "known": last is not None,
        "total_routes": route_totals().get(material, 0),
        "ran_routes": 0,
        "partial": False,
        "manual": False,
    }
    if last is None:
        return result

    elapsed = (now - last).total_seconds() / 3600
    left = cooldown_hours() - elapsed
    total = result["total_routes"]
    ran = session_routes(material, last)
    # 🌟 手动登记（`--manual 霜仙花`）= 玩家明确说"这种材料我刚采过了"，
    #    所以**不受"跑了几条路线"的比例规则约束** —— 那条规则是给**日志里的隔离带副作用**
    #    擦屁股的（见 band_enabled()）。不特判的话，手动登记那次日志里当然没有路线记录，
    #    ran 会是 0 → 被判成"只采了一部分"→ 反而**不冷却**，跟这功能的用法完全相反。
    manual_at = manual_collected(material)
    from_manual = manual_at is not None and manual_at >= last
    if from_manual and total:
        ran = total
    partial = bool(
        band_enabled()
        and not from_manual
        and total
        and total >= 2
        and ran / total < _minimum_ratio()
    )

    result.update({
        "last_at": last,
        "hours_ago": elapsed,
        "hours_left": max(0.0, left),
        "ran_routes": ran,
        "partial": partial,
        "manual": from_manual,
        # 只跑了一部分 → 还有没采的，不算冷却（否则会白等 48 小时）
        "cooling": left > 0 and not partial,
    })
    return result


def human_hours(hours):
    """3.4 小时 → 「3 小时 24 分」；不足 1 小时给分钟；顺便处理"20 小时 60 分"这种进位。"""
    if hours is None:
        return "未知"
    if hours < 1:
        return f"{int(round(hours * 60))} 分钟"
    whole = int(hours)
    minutes = int(round((hours - whole) * 60))
    if minutes >= 60:               # 四舍五入会凑出 60 分，要进位
        whole += 1
        minutes = 0
    if whole >= 24:
        days, rest = divmod(whole, 24)
        return f"{days} 天 {rest} 小时" if rest else f"{days} 天"
    return f"{whole} 小时 {minutes} 分" if minutes else f"{whole} 小时"


def describe(result):
    """一行话描述某种材料的冷却状态。"""
    material = result.get("material") or "?"
    if not result.get("known"):
        return f"✅ {material}：没有采集记录（按已刷新处理，可以采）"
    last = result.get("last_at")
    when = last.strftime("%m-%d %H:%M") if isinstance(last, datetime.datetime) else "（时间未知）"
    total = result.get("total_routes") or 0
    ran = result.get("ran_routes") or 0
    if result.get("partial"):
        # 只跑了一部分：多半是防闪退隔离带顺带开的那一条，或中途停了 —— 不能算采完
        detail = f"{ran}/{total} 条路线" if total else f"{ran} 条路线"
        return (
            f"✅ {material}：{when} 只采了 {detail}（其余还没采过），**不算采完**，现在可以继续采"
        )
    if result["cooling"]:
        return (
            f"⏳ {material}：{when} 采过（{human_hours(result['hours_ago'])}前），"
            f"还没刷新，还要等约 {human_hours(result['hours_left'])}"
        )
    return (
        f"✅ {material}：{when} 采过（{human_hours(result['hours_ago'])}前），"
        f"已刷新（{cooldown_hours()} 小时冷却已过），可以采"
    )


def cooling_materials(materials=None):
    """给定材料里还在冷却的（不给就统计"最近采过且仍冷却"的采集类材料）。

    ⚠️ 不带参数时**只统计路线清单里的材料**（地图素材/矿物/食材与炼金）：
    否则敌人与魔物那些魔物名（巡陆艇、蕈兽…）也会混进来 —— 它们不是 48 小时刷新的特产。
    """
    if materials is None:
        vocabulary = route_material_vocabulary()
        seen = []
        for event in scan_events():
            material = event["material"]
            if event["failed"] or material in seen:
                continue
            if vocabulary and material not in vocabulary:
                continue
            seen.append(material)
        for material in load_manual():
            if material not in seen:
                seen.append(material)
        materials = seen
    results = [status(material) for material in materials]
    return [result for result in results if result["cooling"]]


# ==========================================
# 🌟 目标解析：材料名 / 角色名 → 采集物材料
# ==========================================


# 材料名的合法性：2~8 个字，允许「紫晶块[大剑]」这种带武器类型后缀的写法
_MATERIAL_NAME_RE = re.compile(r"^[\u4e00-\u9fff·]{2,8}(\[[^\]]{1,6}\])?$")
# 这些是地区/分类名，不是材料（folderName 只到目录层级时会抠出它们）
_NOT_MATERIALS = {
    "璃月", "蒙德", "稻妻", "须弥", "枫丹", "纳塔", "挪德卡莱", "至冬", "坎瑞亚",
    "地方特产", "矿物", "食材与炼金", "锄地专区", "敌人与魔物",
}
# 目录里"分组/变体"标签，不是材料名 —— 实测玩家脚本组里到处都是这种层级：
#   地方特产\蒙德\慕风蘑菇\无草神@Tool_tingsu        → 材料是「慕风蘑菇」，不是「无草神」
#   地方特产\须弥\沙脂蛹\1. 高成功率路线            → 材料是「沙脂蛹」，不是那条路线名
#   地方特产\璃月\清水玉\清水玉@…\A组鼋背           → 材料是「清水玉」
#   地方特产\璃月\夜泊石\夜泊石地下@烤鱼            → 归到「夜泊石」（同一材料的地下路线）
_LABEL_WORDS = re.compile(r"路线|地下|补充|收集|草神|未修正|低效|高效|鼋背|静态|动态|\d")
# 路线作者写错的材料名 → 游戏里的正式名（本地百科字典核对过：
# 字典里只有「蒲公英籽」「珊瑚真珠」，没有「蒲公英」「珊瑚珍珠」）
_MATERIAL_ALIASES = {
    "蒲公英": "蒲公英籽",
    "珊瑚珍珠": "珊瑚真珠",
}


def _looks_like_material(name):
    name = str(name or "").strip()
    if not name or name in _NOT_MATERIALS:
        return False
    if _LABEL_WORDS.search(name):        # 变体标签（无草神 / 1. 高成功率路线 / A组鼋背…）
        return False
    return bool(_MATERIAL_NAME_RE.match(name))


def _canonical_material(name):
    name = str(name or "").strip()
    return _MATERIAL_ALIASES.get(name, name)


def _material_from_route_name(route_name):
    """从路线**文件名**里读材料：`01-慕风蘑菇-晨曦酒庄-7个.json` → 慕风蘑菇。

    文件名是最可靠的来源（约定就是 `NN-材料名-…`），所以**优先于目录**。
    实测：慕风蘑菇/沙脂蛹 这类路线放在 `…\\材料\\无草神@作者` 这种多级目录下，
    只看目录最后一段会读出「无草神」。
    """
    match = _ROUTE_MATERIAL_RE.match(str(route_name or ""))
    if not match:
        return ""
    candidate = _canonical_material(match.group(1))
    return candidate if _looks_like_material(candidate) else ""


def _material_from_folder(folder):
    """从 `folderName` 由深到浅找"像材料的那一段"（跳过作者后缀与分组/变体标签）。"""
    parts = [part for part in str(folder or "").replace("/", "\\").split("\\") if part]
    for part in reversed(parts):
        candidate = _canonical_material(part.split("@")[0])
        if not candidate or not _looks_like_material(candidate):
            continue
        return candidate
    return ""


def material_from_project(project):
    """从脚本组的一条路线里抠出材料名（先文件名、再目录，见上面两个函数的注释）。"""
    project = project or {}
    return (
        _material_from_route_name(project.get("name"))
        or _material_from_folder(project.get("folderName"))
    )


# 路线文件名 → 材料名（由"采集类目"的脚本组现算，按组文件 mtime 缓存）
_ROUTE_INDEX = {}
_ROUTE_INDEX_KEY = {"key": None}


def _collect_group_paths():
    return (
        getattr(config, "BGI_MAP_CONFIG", ""),
        getattr(config, "BGI_MINE_CONFIG", ""),
        getattr(config, "BGI_COOK_CONFIG", ""),
    )


def route_material_index():
    """{路线文件名: 材料名}（地图素材 / 矿物 / 食材与炼金三个组）。

    为什么需要它：日志里只有一条路线的**文件名**，而有些路线的文件名不带材料
    （`1灵濛山.json`、`3药蝶谷.json`）或前缀不是纯数字（`09A-清心-…`）——
    这时就得回查脚本组，看这个文件挂在哪个材料目录下。
    """
    candidates = _collect_group_paths()
    key = tuple(
        (path, (os.path.getmtime(path) if os.path.isfile(path) else None))
        for path in candidates
    )
    if _ROUTE_INDEX_KEY.get("key") == key:
        return _ROUTE_INDEX

    try:
        from skills import route_group
    except Exception:        # noqa: BLE001
        return _ROUTE_INDEX

    index = {}
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        group = route_group.load_group(path) or {}
        for project in group.get("projects") or []:
            name = os.path.basename(str(project.get("name") or ""))
            material = material_from_project(project)
            if name and material:
                index.setdefault(name, material)

    _ROUTE_INDEX.clear()
    _ROUTE_INDEX.update(index)
    _ROUTE_INDEX_KEY["key"] = key
    return _ROUTE_INDEX


def material_for_route(route_name):
    """给一个路线文件名，猜出它属于哪种材料（先文件名、再回查脚本组；都没有给空串）。"""
    name = os.path.basename(str(route_name or ""))
    material = _material_from_route_name(name)
    if material:
        return material
    return route_material_index().get(name, "")


def route_material_vocabulary():
    """脚本组路线里出现过的材料名（= BGI 真能去采的东西）。

    只统计"采集类目"的三个组（地图素材 / 矿物 / 食材与炼金）：敌人与魔物那些组的
    `folderName` 末尾是魔物名（巡陆艇、蕈兽…），它们不是 48 小时刷新的特产。
    """
    return set(route_material_index().values())


def _specialty_index():
    """{角色名: 特产材料名}（本地百科字典，覆盖 300+ 角色）。"""
    index = {}
    try:
        from skills.env_reader import load_yatta_dict_safely

        for entry in load_yatta_dict_safely().values():
            name = str((entry or {}).get("name") or "")
            if not name:
                continue
            for material in entry.get("materials") or []:
                if material.get("type") == "特产":
                    index.setdefault(name, str(material.get("name") or ""))
                    break
    except Exception as exc:        # noqa: BLE001
        print(f"⚠️ 读取角色特产索引失败（不影响按材料名检查）：{exc}")
    return index


def _showcase_specialty_index():
    """{角色名: 特产材料名} —— 取自展柜（新角色只有这里可能有）。"""
    index = {}
    try:
        from skills import env_reader

        data = env_reader.latest_env_data()
        if not isinstance(data, dict):
            return index
        for avatar in data.get("avatars") or []:
            name = str(avatar.get("name") or "")
            if not name:
                continue
            for material in avatar.get("materials") or []:
                if material.get("type") == "特产":
                    index[name] = str(material.get("name") or "")
                    break
    except Exception:        # noqa: BLE001
        return index
    return index


def specialty_of(character):
    """角色 → TA 的采集物（特产）。展柜优先（新角色只有那儿有），再查本地字典。"""
    character = str(character or "").strip()
    if not character:
        return "", ""
    showcase = _showcase_specialty_index()
    if character in showcase and showcase[character]:
        return showcase[character], "展柜数据"
    index = _specialty_index()
    if character in index and index[character]:
        return index[character], "本地百科字典"
    return "", ""


def resolve_material(target):
    """把玩家/模型给的目标解析成采集物材料名。返回 (材料名, 说明)；认不出来给 ("", 原因)。"""
    text = str(target or "").strip()
    if not text:
        return "", "目标为空"
    text = _TARGET_TAIL_RE.sub("", text).strip()          # 「蓝砚的突破材料」→「蓝砚」
    text = re.sub(r"^(去|帮我|采集|采|摘|捡)+", "", text).strip()

    vocabulary = route_material_vocabulary()
    if text in vocabulary:
        return text, "材料名"

    material, source = specialty_of(text)
    if material:
        return material, f"角色解析（{source}）"

    # 目标里嵌着材料名（「采集霜仙花」残渣 / 「霜仙花的路线」）
    for material in sorted(vocabulary, key=len, reverse=True):
        if material and material in text:
            return material, "材料名（模糊匹配）"

    return "", (
        f"认不出「{text}」是哪种采集物：本地字典里没有这个角色，"
        "也不是地图素材组里的材料名 —— 请直接说材料名（例如「霜仙花」）"
    )


def is_forced(text):
    """玩家是不是明确要求"就算没刷新也采"。"""
    normalized = re.sub(r"[\s，。！？,.!?]", "", str(text or ""))
    return any(word in normalized for word in FORCE_WORDS)


# ==========================================
# 🌟 给审批屏 / 提示词用的文本
# ==========================================


def notice_lines(targets):
    """给审批屏的计划行：每个采集目标一行状态。

    返回 (lines, blocked_materials)；blocked = 还在冷却、本次不该采的。
    """
    lines = []
    blocked = []
    for target in targets or ():
        material, note = resolve_material(target)
        if not material:
            lines.append(f"⚠️ 采集目标「{target}」：{note}")
            continue
        result = status(material)
        tag = f"（来自{note}）" if note != "材料名" else ""
        if result["cooling"]:
            blocked.append(material)
            lines.append("⏳ " + describe(result).replace("⏳ ", "") + tag)
        else:
            lines.append("✅ " + describe(result).replace("✅ ", "") + tag)
    return lines, blocked


def cooldown_block(now=None, hours=48):
    """给大模型的"采集物冷却"上下文（只列还在冷却里的，避免刷屏）。

    玩家/模型据此可以：① 认出"霜仙花还没刷新"；② 挑一个已经刷新的材料；
    ③ 在拿不准角色对应哪种采集物时，直接问玩家。
    """
    rows = cooling_materials()
    if not rows:
        return ""
    lines = [
        f"\n\n⏳ 【采集物冷却】（地区特产 48 小时刷新，下面是**当前还没刷新**的）"
    ]
    for result in rows[:12]:
        lines.append("   · " + describe(result))
    if len(rows) > 12:
        lines.append(f"   · …还有 {len(rows) - 12} 种")
    lines.append(
        "   ↳ 玩家点名的采集物如果在这张表里：**不要**安排采集，直接告诉他还要等多久"
        "（可以顺便建议一个已刷新的）；玩家明确说「强制采集」时才照做。\n"
        "   ↳ 玩家说的是角色名而不是材料名时，按展柜/展柜外角色数据里该角色的「特产」来对应；"
        "拿不准就问玩家材料名，别猜。"
    )
    return "\n".join(lines)


def _main(argv=None):
    """命令行：查看冷却 / 手动登记 / 清记录。

        python -m skills.gather_cooldown                  # 最近采过、且还没刷新的
        python -m skills.gather_cooldown --check 霜仙花    # 查一种材料
        python -m skills.gather_cooldown --manual 霜仙花   # 手动记为"刚采过"（游戏里自己采的）
        python -m skills.gather_cooldown --clear 霜仙花    # 清掉手动记录
        python -m skills.gather_cooldown --materials      # 列出能采的材料名
    """
    import argparse

    parser = argparse.ArgumentParser(description="地区特产 48 小时冷却检测")
    parser.add_argument("--check", metavar="材料/角色", help="查一种材料（或角色）的冷却状态")
    parser.add_argument("--manual", metavar="材料", help="手动记为刚采过")
    parser.add_argument("--clear", metavar="材料", nargs="?", const="", help="清掉手动记录（不给名字=全清）")
    parser.add_argument("--materials", action="store_true", help="列出地图素材组里的材料名")
    args = parser.parse_args(argv)

    print(f"冷却时长：{cooldown_hours()} 小时；扫描日志 {len(log_paths())} 天")

    if args.materials:
        names = sorted(route_material_vocabulary())
        print(f"共 {len(names)} 种：{'、'.join(names)}")
        return 0

    if args.manual:
        material, note = resolve_material(args.manual)
        material = material or args.manual
        mark_manual(material)
        print(f"✅ 已把「{material}」记为刚采过" + (f"（{note}）" if note != "材料名" else ""))
        print("   " + describe(status(material)))
        return 0

    if args.clear is not None:
        clear_manual(args.clear or None)
        print("✅ 已清掉手动记录" if not args.clear else f"✅ 已清掉「{args.clear}」的手动记录")
        return 0

    if args.check:
        material, note = resolve_material(args.check)
        if not material:
            print("❌ " + note)
            return 1
        print(("🔎 解析：" + note + " → 「" + material + "」") if note != "材料名" else "")
        print(describe(status(material)))
        return 0

    rows = cooling_materials()
    if not rows:
        print("✅ 当前没有还在冷却的采集物（最近都没采过，或都已过 48 小时）")
    else:
        print(f"⏳ 还在冷却的有 {len(rows)} 种：")
        for result in sorted(rows, key=lambda item: item["hours_left"]):
            print("   " + describe(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
