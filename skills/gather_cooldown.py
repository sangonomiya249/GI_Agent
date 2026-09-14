"""世界资源（采集物 / 矿物 / 食材 / 魔物）的刷新冷却检测。

## 四类资源、四套刷新规则

实测（bilibili wiki「新手教程 · 采集物刷新时间」原文 + 玩家脚本组）：

| 类别 | 刷新规则（原文摘要） | 默认值 |
| --- | --- | --- |
| 地区特产 | 「特产…在采集后经过 48 小时刷新」 | 48 小时 |
| 矿物 | 精锻用矿石按档：铁块/白铁块「上次刷新后的次日」、星银矿石「第二日」、水晶块/紫晶块「第三日」（服务器 0 点）；魔晶块「每天 6 点」 | 24 / 48 / 72 小时 |
| 食材与炼金 | 「大部分食材…每日凌晨 0 点刷新」 | 24 小时（近似：按"距上次 24 小时"判，不追服务器 0 点） |
| 敌人与魔物 | 动物/晶蝶类「当天采集后 12 小时刷新以及凌晨四点刷新」；普通魔物社区通行说法也是 12 小时 | 12 小时 |

来源：<https://wiki.biligame.com/ys/新手教程>（社区维护的 wiki，非官方数值）。
四个时长都能在 Studio「配置」页 / `.env` 里改；矿物还能**按材料逐条覆盖**
（`MATERIAL_HOURS`，例如星银矿石 48、水晶块 72）。

## 数据从哪来（为什么不自己记账）

BetterGI 每跑完一条地图追踪路线都会打一行：

    → 脚本执行结束: "01-霜仙花-彩冰镇左上-3个.json", 耗时: 0分24.5秒

而地图追踪的路线的文件名格式是固定的 **`NN-材料名-地点-N个.json`**（实测玩家脚本组里
710 条路线都符合，例如 `01-霜仙花-彩冰镇左上-3个.json`、`04-月莲-茸蕈窟-4个.json`）。
所以"哪种材料、什么时候采的"完全可以确定性地从日志里读出来 —— 而且是**唯一**能覆盖
"玩家自己在 BetterGI 里手动跑"的证据（自己记账本会漏掉手动跑的那些）。

**类别**由这条路线挂在哪个脚本组决定（地图素材 → 特产、矿物 → 矿物、食材与炼金 → 食材、
敌人与魔物 → 魔物）—— 组名就是玩家自己分的类，比任何字典都准。

两个细节：
  · **失败不算**：紧邻上文有「此追踪脚本未正常走完！」或「任务执行失败」的那条不计入冷却
    （实测玩家日志里 `04-便携轴承-…` 被停止快捷键打断时就是这种）；
  · **跨自然日**：BetterGI 的日志按天切文件（`better-genshin-impactYYYYMMDD.log`），
    冷却可能横跨多个文件，所以按天往前扫（默认扫 `ceil(最长冷却小时/24)+1` 天）。

## 冷却规则

上次采集距今 ≥ 这种材料/类别的冷却时长 → **刷新了**，正常执行；还在冷却里 → **本次不排它**，
并告诉玩家还要等多久、上次什么时候采的。

玩家在游戏里手动采过、日志里没有的，可以 `python -m skills.gather_cooldown --manual 霜仙花`
记一笔（写进 `memory/gather_cooldown_manual.json`）；临时想强跑一次，说「强制采集」即可
（`is_forced()`，会跳过冷却拦截，**不会**改动上面的记录）。
"""

import datetime
import glob
import json
import os
import re
import time

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


# ==========================================
# 🌟 冷却类别（四类资源、四套刷新规则）
# ==========================================

CATEGORY_SPECIALTY = "specialty"
CATEGORY_MINE = "mine"
CATEGORY_COOK = "cook"
CATEGORY_HUNT = "hunt"
CATEGORY_ORDER = (CATEGORY_SPECIALTY, CATEGORY_MINE, CATEGORY_COOK, CATEGORY_HUNT)

CATEGORY_LABELS = {
    CATEGORY_SPECIALTY: "地区特产",
    CATEGORY_MINE: "矿物",
    CATEGORY_COOK: "食材与炼金",
    CATEGORY_HUNT: "敌人与魔物",
}
# 默认冷却时长（小时）—— 来源见模块开头的表；都能在 .env / Studio 配置页改
CATEGORY_DEFAULT_HOURS = {
    CATEGORY_SPECIALTY: 48,
    CATEGORY_MINE: 72,
    CATEGORY_COOK: 24,
    CATEGORY_HUNT: 12,
}
# 类别 → 读哪个 .env 键（老键 GATHER_COOLDOWN_HOURS 继续当"地区特产"的时长，保持兼容）
CATEGORY_ENV_KEYS = {
    CATEGORY_SPECIALTY: "GATHER_COOLDOWN_HOURS",
    CATEGORY_MINE: "MINE_COOLDOWN_HOURS",
    CATEGORY_COOK: "COOK_COOLDOWN_HOURS",
    CATEGORY_HUNT: "HUNT_COOLDOWN_HOURS",
}
# 类别 → 这个类别的路线来自哪个脚本组（玩家自己的分组就是最可靠的分类依据）
CATEGORY_CONFIG_KEYS = {
    CATEGORY_SPECIALTY: "BGI_MAP_CONFIG",
    CATEGORY_MINE: "BGI_MINE_CONFIG",
    CATEGORY_COOK: "BGI_COOK_CONFIG",
    CATEGORY_HUNT: "BGI_ENEMY_CONFIG",
}
# 依据（给 Studio 页面当脚注用，别让人以为这些数字是官方给的）
CATEGORY_NOTES = {
    CATEGORY_SPECIALTY: "游戏里标【XX区域特产】的材料；wiki：采集后 48 小时刷新",
    CATEGORY_MINE: "精锻用矿石按档：铁/白铁→次日、星银→第二日、水晶/紫晶→第三日（服务器 0 点）；魔晶块每天 6 点",
    CATEGORY_COOK: "大部分食材每日凌晨 0 点刷新（这里按「距上次 24 小时」近似）",
    CATEGORY_HUNT: "普通魔物击败后 12 小时刷新（wiki 明确写了动物/晶蝶类 12 小时+凌晨 4 点）",
}
# 矿物按材料分档（wiki 的矿石刷新表）。这里没有的矿物用类别默认 72 小时。
MATERIAL_HOURS = {
    "铁块": 24, "白铁块": 24,          # 「上次刷新后的次日」
    "星银矿石": 48,                     # 「第二日」
    "水晶块": 72, "紫晶块": 72,          # 「第三日」
    "魔晶块": 24,                       # 「每天 6 点」
    "电气水晶": 48,                     # wiki 把它归在「特产、元素物质…48 小时」
    # 夜泊石 / 石珀 属于矿石点（wiki 把它们列在"矿石破坏后掉落"里），按矿物默认 72 小时
    "夜泊石": 72, "石珀": 72,
}


def category_default_hours(category):
    """类别的默认冷却时长（小时）。"""
    return CATEGORY_DEFAULT_HOURS.get(str(category or ""), CATEGORY_DEFAULT_HOURS[CATEGORY_SPECIALTY])


def category_hours(category, now=None):
    """类别的实际冷却时长：优先 `.env` 里那个键，其次默认值。"""
    env_key = CATEGORY_ENV_KEYS.get(str(category or ""))
    if not env_key:
        return category_default_hours(category)
    value = getattr(config, env_key, None)
    if value is None:
        return category_default_hours(category)
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return category_default_hours(category)
    return hours if hours > 0 else category_default_hours(category)


def cooldown_hours():
    """地区特产的冷却时长（老接口，保持兼容）。"""
    return category_hours(CATEGORY_SPECIALTY)


def max_cooldown_hours():
    """所有类别里最长的冷却（决定"往前扫几天日志"）。"""
    return max(category_hours(item) for item in CATEGORY_ORDER)


def scan_days():
    """要往前扫几天日志（最长冷却 72 小时横跨 4 个自然日）。"""
    return int(max_cooldown_hours() // 24) + 1


# ==========================================
# 🌟 手动登记 / 日志解析
# ==========================================


def manual_state_path():
    return config.project_path("memory", "gather_cooldown_manual.json")


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


def last_collected(material, days=None, events=None):
    """某种材料最近一次**成功**采集的时间；没有记录返回 None。

    `events` 可以直接传进来复用（见 `session_routes` 的说明）。
    """
    material = str(material or "").strip()
    if not material:
        return None

    latest = None
    for event in (scan_events(days) if events is None else events):
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

    组的来源见 `_group_scan()`：四个资源总组 **+ 玩家自建的魔物 / 材料小组**
    （蕈兽.json、虹滴晶.json…），这样"打蕈兽"这种目标也有可比的分母。
    """
    key = _group_scan_key(force=_ROUTE_TOTALS_CACHE_KEY.get("key") is None)
    if _ROUTE_TOTALS_CACHE_KEY.get("key") == key:
        return _ROUTE_TOTALS

    key, entries = _group_scan()
    if entries is None:          # 组读不了：保持上次结果
        return _ROUTE_TOTALS

    totals = {}
    for _category, _path, project in entries:
        material = _material_from_project_checked(project)
        if material and _looks_like_material(material):
            totals[material] = totals.get(material, 0) + 1

    _ROUTE_TOTALS.clear()
    _ROUTE_TOTALS.update(totals)
    _ROUTE_TOTALS_CACHE_KEY["key"] = key
    return _ROUTE_TOTALS


def session_routes(material, latest, window_hours=3, events=None):
    """上一次"采集那一趟"里，这种材料成功跑完的**不同路线**数（去重）。

    `events` 可以直接传进来复用（Studio 的资源冷却页要一次算几十种材料，
    每个都重扫一遍日志会慢几十倍 —— 见 `overview()`）。
    """
    if latest is None:
        return 0
    window = latest - datetime.timedelta(hours=window_hours)
    routes = set()
    for event in (scan_events() if events is None else events):
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


def status(material, now=None, events=None, category=None, totals=None):
    """单种材料（或魔物）的冷却状态。

    返回 {material, category, category_label, hours, last_at, hours_ago, hours_left, cooling,
          known, total_routes, ran_routes, partial, manual}
      · cooling=True → 还在冷却，不该刷；
      · known=False  → 没有任何记录（当作"刷新了"，可以去）；
      · partial=True → 上次只跑了这种材料的一部分路线（多半是**防闪退隔离带**顺带跑的，
                       或中途停了）→ **不算跑完**，可以继续；
                       只在隔离带开着时才会判（关掉隔离带就是"正常冷却"）。
      · manual=True  → 这次记录来自**手动登记**（游戏里自己跑过的），不算"部分采集"。

    冷却时长按**材料**定：矿物还按材料分档（铁块 24 / 星银矿石 48 / 水晶块 72…），
    见 `hours_for()`；类别由脚本组决定，见 `category_of()`。
    `category` 由调用方给定时**以调用方为准** —— 玩家说的是「打蕈兽」时，
    类别就是魔物（12 小时），不能因为词表里查不到就按特产 48 小时算。

    `events` 可以直接传进来复用（见 `overview()`）；`totals` 同理（路线总数表，
    不传就自己查一次 —— 一次算几十种材料时别让它查几十遍）。
    """
    now = now or datetime.datetime.now()
    material = str(material or "").strip()
    category = str(category or "").strip() or category_of(material)
    hours = hours_for(material, category)
    last = last_collected(material, events=events)
    result = {
        "material": material,
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, category),
        "hours": hours,
        "last_at": None,
        "hours_ago": None,
        "hours_left": 0.0,
        "cooling": False,
        "known": last is not None,
        "total_routes": (route_totals() if totals is None else totals).get(material, 0),
        "ran_routes": 0,
        "partial": False,
        "manual": False,
    }
    if last is None:
        return result

    elapsed = (now - last).total_seconds() / 3600
    left = hours - elapsed
    total = result["total_routes"]
    ran = session_routes(material, last, events=events)
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
        # 只跑了一部分 → 还有没跑的，不算冷却（否则会白等一个刷新周期）
        "cooling": left > 0 and not partial,
    })
    return result


def category_sections(rows):
    """把 `status()` 的结果按类别分组（Studio 页面 / CLI 都用它排版）。

    返回 [{key, label, hours, note, materials:[...], summary:{...}}]，顺序固定为
    地区特产 → 矿物 → 食材与炼金 → 敌人与魔物；每个类别内部：**冷却中的在前**。
    """
    sections = []
    for category in CATEGORY_ORDER:
        items = [row for row in rows if row.get("category") == category]
        if not items:
            continue
        items.sort(key=lambda row: (
            not row["cooling"], row["hours_left"], -(row["total_routes"] or 0), row["material"],
        ))
        sections.append({
            "key": category,
            "label": CATEGORY_LABELS.get(category, category),
            "hours": category_hours(category),
            "default_hours": category_default_hours(category),
            "note": CATEGORY_NOTES.get(category, ""),
            "materials": items,
            "summary": {
                "total": len(items),
                "cooling": sum(1 for row in items if row["cooling"]),
                "ready": sum(1 for row in items if not row["cooling"]),
                "partial": sum(1 for row in items if row["partial"]),
                "manual": sum(1 for row in items if row["manual"]),
            },
        })
    return sections


def overview(now=None):
    """一次算出**四类资源全部材料**的冷却状态（Studio 的「资源冷却」页 / CLI 都用它）。

    为什么要单独一个函数：`status()` 内部会扫日志，上百种材料各扫一遍就是上百倍的
    重复 IO（实测 60+ 种材料在地图素材这种大组上要好几秒）。这里把日志解析一次，
    再逐个算状态。

    返回 {now, hours（兼容字段=特产时长）, category_hours, min_route_percent, band_enabled,
          scanned_days, events, summary, sections:[按类别分组], materials:[平铺、冷却在前],
          totals}
    """
    now = now or datetime.datetime.now()
    events = scan_events()
    totals = route_totals()
    manual = load_manual()

    names = set(route_material_vocabulary()) | set(manual)
    rows = [status(name, now=now, events=events, totals=totals) for name in sorted(names)]
    rows.sort(key=lambda row: (not row["cooling"], row["hours_left"], -(row["total_routes"] or 0), row["material"]))

    summary = {
        "total": len(rows),
        "cooling": sum(1 for row in rows if row["cooling"]),
        "ready": sum(1 for row in rows if not row["cooling"]),
        "partial": sum(1 for row in rows if row["partial"]),
        "manual": sum(1 for row in rows if row["manual"]),
        "with_routes": sum(1 for row in rows if row["total_routes"]),
    }
    return {
        "now": now,
        # 兼容老字段：页面/测试里用过的 hours（= 地区特产时长）
        "hours": category_hours(CATEGORY_SPECIALTY),
        "category_hours": {item: category_hours(item) for item in CATEGORY_ORDER},
        "min_route_percent": int(round(_minimum_ratio() * 100)),
        "band_enabled": band_enabled(),
        "scanned_days": len(log_paths()),
        "events": len(events),
        "summary": summary,
        "sections": category_sections(rows),
        "materials": rows,
        "totals": totals,
    }


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
    """一行话描述某种材料/魔物的冷却状态。"""
    material = result.get("material") or "?"
    category = CATEGORY_LABELS.get(result.get("category"), "")
    tag = f"（{category}）" if category else ""
    if not result.get("known"):
        return f"✅ {material}{tag}：没有记录（按已刷新处理，可以去）"
    last = result.get("last_at")
    when = last.strftime("%m-%d %H:%M") if isinstance(last, datetime.datetime) else "（时间未知）"
    total = result.get("total_routes") or 0
    ran = result.get("ran_routes") or 0
    if result.get("partial"):
        # 只跑了一部分：多半是防闪退隔离带顺带开的那一条，或中途停了 —— 不能算跑完
        detail = f"{ran}/{total} 条路线" if total else f"{ran} 条路线"
        return (
            f"✅ {material}{tag}：{when} 只跑了 {detail}（其余还没跑过），**不算跑完**，现在可以继续"
        )
    if result["cooling"]:
        return (
            f"⏳ {material}{tag}：{when} 采过（{human_hours(result['hours_ago'])}前），"
            f"还没刷新，还要等约 {human_hours(result['hours_left'])}"
        )
    return (
        f"✅ {material}{tag}：{when} 采过（{human_hours(result['hours_ago'])}前），"
        f"已刷新（{result.get('hours', cooldown_hours())} 小时冷却已过），可以去"
    )


def cooling_materials(materials=None):
    """给定材料里还在冷却的（不给就统计"最近跑过且仍冷却"的全部四类资源）。

    ⚠️ 不带参数时**只统计路线清单里的材料**（四类脚本组里的）：日志里还可能出现过
    已经不存在的路线，那些材料账对不上就别列。
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
    results = [status(material, totals=route_totals()) for material in materials]
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
    """从脚本组的一条路线里抠出材料名（先文件名、再目录，见上面两个函数的注释）。

    ⚠️ 索引扫描用的是 `_material_from_project_checked()`：它多一道"文件名读出来的材料
    得能和目录对上"的校验（否则 `01-那夏镇下方-4个.json` 会被读成地名）。
    """
    project = project or {}
    return (
        _material_from_route_name(project.get("name"))
        or _material_from_folder(project.get("folderName"))
    )


# 路线文件名 → 材料名，以及 材料名 → 类别（由各脚本组现算，按组文件 mtime 缓存）
_ROUTE_INDEX = {}
_ROUTE_CATEGORIES = {}
_ROUTE_INDEX_KEY = {"key": None}
# `AutoPathing/敌人与魔物/<魔物名>` 的目录名单（按目录 mtime 缓存）
_ENEMY_NAMES = {"key": None, "names": frozenset()}
# 组扫描键的短时缓存（见 `_group_scan_key`）：路径指纹 + 上次算出的键 + 算出来的时刻
_SCAN_KEY_CACHE = {"paths": None, "key": None, "at": 0.0}
_SCAN_KEY_TTL = 0.5

# 路线目录第一段能当"类目前缀"用的（`矿物\虹滴晶`、`地方特产\须弥\…`）
FOLDER_CATEGORY_PREFIXES = {
    "地方特产": CATEGORY_SPECIALTY,
    "矿物": CATEGORY_MINE,
    "食材与炼金": CATEGORY_COOK,
    "敌人与魔物": CATEGORY_HUNT,
}
# 这些前缀**故意不分类**：锄地专区是"整张图扫一遍"，不是某种资源的刷新，
# 它的路线名（`0_0_飞萤`、`精英400`）混进冷却词表只会变成噪音。
_SKIP_FOLDER_PREFIXES = frozenset({"锄地专区"})


def _collect_group_paths():
    """四类资源各自的脚本组路径（顺序与 CATEGORY_ORDER 对应）。"""
    return tuple(
        (category, getattr(config, key, ""))
        for category, key in (
            (CATEGORY_SPECIALTY, CATEGORY_CONFIG_KEYS[CATEGORY_SPECIALTY]),
            (CATEGORY_MINE, CATEGORY_CONFIG_KEYS[CATEGORY_MINE]),
            (CATEGORY_COOK, CATEGORY_CONFIG_KEYS[CATEGORY_COOK]),
            (CATEGORY_HUNT, CATEGORY_CONFIG_KEYS[CATEGORY_HUNT]),
        )
    )


def _enemy_base_dir():
    """`<AutoPathing>/敌人与魔物` —— BetterGI 路线仓库里魔物的权威目录。"""
    root = str(getattr(config, "BGI_AUTO_PATHING_DIR", "") or "")
    return os.path.join(root, "敌人与魔物") if root else ""


def enemy_route_names():
    """魔物名单：`AutoPathing/敌人与魔物/<魔物名>` 这一层目录（按目录 mtime 缓存）。

    为什么要它：**玩家自己按魔物建的小组**（`蕈兽.json` / `骗骗花.json` / `飘浮灵.json`）
    里，路线目录就是 `蕈兽\\蕈兽@作者`，**没有** `敌人与魔物\\` 这个前缀 ——
    光看组文件分不出它属于哪一类，于是"打蕈兽"会被判成"认不出"（玩家实测报过）。
    """
    base = _enemy_base_dir()
    key = (base, os.path.getmtime(base) if base and os.path.isdir(base) else None)
    if _ENEMY_NAMES.get("key") == key:
        return _ENEMY_NAMES["names"]

    names = set()
    if base and os.path.isdir(base):
        try:
            for entry in os.listdir(base):
                if os.path.isdir(os.path.join(base, entry)):
                    names.add(entry)
        except OSError:
            names = set()
    _ENEMY_NAMES["key"] = key
    _ENEMY_NAMES["names"] = frozenset(names)
    return _ENEMY_NAMES["names"]


def _group_dir_files():
    """脚本组目录里的 `*.json`：`{文件名: mtime}`（一次 `scandir` 拿全）。

    为什么用 `scandir` + `DirEntry.stat()`：Windows 的目录枚举结果里本来就带着 mtime，
    实测 0.22ms；换成 `listdir` + 逐个 `getmtime` 是 4ms（近 20 倍）——
    而这个键会被查几千次（日志里每条路线结束、每种材料的冷却状态都要问一次）。
    只返回**文件名**（不拼绝对路径）：`abspath` 每次都要规范化当前目录，
    在几十万次调用下比 scandir 本身还贵。
    """
    group_dir = str(getattr(config, "BGI_SCRIPT_GROUP_DIR", "") or "")
    files = {}
    if not group_dir or not os.path.isdir(group_dir):
        return files
    try:
        with os.scandir(group_dir) as entries:
            for entry in entries:
                if not entry.name.lower().endswith(".json"):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    files[entry.name] = entry.stat().st_mtime
                except OSError:
                    continue
    except OSError:
        return {}
    return files


def _extra_group_names(files=None):
    """脚本组目录里**其它**组（玩家按魔物 / 材料自己建的那些）的**文件名**。

    为什么必须带上它们：「敌人与魔物」总组里可能只有巡陆艇（玩家只订了这一个），
    蕈兽 / 骗骗花 / 飘浮灵 都在玩家自建的小组里；只读四个总组的话，
    这些**说得出口、也真的能跑**的名字会被判成"认不出这种魔物"。
    """
    files = _group_dir_files() if files is None else files
    if not files:
        return ()
    skip_names = {
        os.path.basename(str(getattr(config, key, "") or ""))
        for key in CATEGORY_CONFIG_KEYS.values()
    }
    return tuple(
        name for name in sorted(files) if name not in skip_names
    )


def _extra_group_paths():
    """同上，返回完整路径（只在**真的要读组**时调用）。"""
    group_dir = str(getattr(config, "BGI_SCRIPT_GROUP_DIR", "") or "")
    if not group_dir:
        return ()
    skip = {os.path.abspath(path) for _, path in _collect_group_paths() if path}
    paths = []
    for name in _extra_group_names():
        path = os.path.join(group_dir, name)
        if os.path.abspath(path) in skip:
            continue
        paths.append(path)
    return tuple(paths)


def _group_scan_key(force=False):
    """只 stat 一遍组文件得到的缓存键（**轻**：不读组文件内容）。

    ⚠️ 别把"读组文件"塞进这里：日志解析会对每一条路线结束事件问一次材料名
    （`material_for_route`），实测 160 条事件 × 读 29 个组文件 = 36 秒。

    这个函数每秒会被问几百次，所以：
      · 先比一次**便宜的路径指纹**（配置路径 + 目录名），路径变了立刻重算；
      · 路径没变时复用 `_SCAN_KEY_TTL` 秒内的结果 —— 组文件内容变化最多晚这么久才被发现，
        对"小时级"的冷却判定完全无所谓，但对"一条日志几千行"的解析是数量级的差别。
    """
    paths = (
        tuple(str(getattr(config, key, "") or "") for key in CATEGORY_CONFIG_KEYS.values()),
        str(getattr(config, "BGI_SCRIPT_GROUP_DIR", "") or ""),
        _enemy_base_dir(),
    )
    now = time.monotonic()
    cache = _SCAN_KEY_CACHE
    if (
        not force
        and cache["key"] is not None
        and cache["paths"] == paths
        and now - cache["at"] < _SCAN_KEY_TTL
    ):
        return cache["key"]

    dir_files = _group_dir_files()
    parts = []
    for category, path in _collect_group_paths():
        name = os.path.basename(str(path or ""))
        if name and name in dir_files:
            mtime = dir_files[name]
        elif path and os.path.isfile(path):
            mtime = os.path.getmtime(path)
        else:
            mtime = None
        parts.append((str(category), str(path), mtime))
    for name in _extra_group_names(dir_files):
        parts.append(("", name, dir_files.get(name)))
    base = _enemy_base_dir()
    parts.append(
        ("enemy", base, os.path.getmtime(base) if base and os.path.isdir(base) else None)
    )
    key = tuple(parts)
    cache.update({"paths": paths, "key": key, "at": now})
    return key


def reset_caches():
    """丢掉所有按 mtime 缓存的索引（改过组文件路径/内容之后调用；运行期不用管）。"""
    _ROUTE_INDEX_KEY["key"] = None
    _ROUTE_TOTALS_CACHE_KEY["key"] = None
    _SCAN_KEY_CACHE.update({"paths": None, "key": None, "at": 0.0})


def _category_for_project(material, folder):
    """这条路线属于哪一类资源；**定不出来就给 None**（宁可不查，也别猜错类别）。

    依据依次是：
    ① 目录第一段的类目前缀（`矿物\\虹滴晶`、`地方特产\\须弥\\…`、`敌人与魔物\\巡陆艇`）；
    ② 矿物分档表 `MATERIAL_HOURS` 里有的材料（`石珀`、`夜泊石` 这类目录不带前缀的）；
    ③ `AutoPathing/敌人与魔物/<魔物名>` 的魔物名单（`蕈兽`、`骗骗花`、`飘浮灵`）。

    定不出类别的（狗粮 / 锄大地 / 作者自建的杂组）**不进冷却词表**：
    它们的路线名（`狗粮`、`精英400`、`01-那夏镇下方`）跑到冷却面板与提示词里没法看。
    """
    parts = [part for part in str(folder or "").replace("/", "\\").split("\\") if part]
    if parts:
        if parts[0] in _SKIP_FOLDER_PREFIXES:
            return None
        category = FOLDER_CATEGORY_PREFIXES.get(parts[0])
        if category:
            return category

    material = str(material or "").strip()
    if not material:
        return None
    if material in MATERIAL_HOURS:
        return CATEGORY_MINE
    if material in enemy_route_names():
        return CATEGORY_HUNT
    return None


def _material_from_project_checked(project):
    """材料名：文件名优先，但**必须能和目录对上**，对不上就以目录为准。

    为什么要这道校验：玩家自建小组里路线文件名常常不带材料名 ——
    `01-那夏镇下方-4个.json` 挂在 `矿物\\虹滴晶` 下，按"文件名优先"会读出
    「那夏镇下方」，那是个地名，不是材料。
    """
    project = project or {}
    from_name = _material_from_route_name(project.get("name"))
    folder = str(project.get("folderName") or "")
    from_folder = _material_from_folder(folder)
    if from_name and (not from_folder or from_name == from_folder or from_name in folder):
        return from_name
    return from_folder or from_name


def _group_scan():
    """(缓存键, [(类别, 组路径, 路线项目)]) —— 所有"能定出资源类别"的组与路线。

    两类来源：四个资源**总组**（类别由配置直接决定）+ 目录里**其它组**的
    能定出类别的路线（见 `_category_for_project`）。
    组文件读不出来时返回 `(键, None)` —— 调用方据此**保持上次结果**，别把索引清空。
    """
    key = _group_scan_key()
    try:
        from skills import route_group
    except Exception:        # noqa: BLE001
        return key, None

    entries = []
    for category, path in _collect_group_paths():
        if not path or not os.path.isfile(path):
            continue
        group = route_group.load_group(path) or {}
        for project in group.get("projects") or []:
            entries.append((category, path, project))

    for path in _extra_group_paths():
        if not os.path.isfile(path):
            continue
        group = route_group.load_group(path) or {}
        for project in group.get("projects") or []:
            category = _category_for_project(
                _material_from_project_checked(project), project.get("folderName")
            )
            if category:
                entries.append((category, path, project))

    return key, entries


def _refresh_route_index():
    # 键被手动清空（`reset_caches()` / 测试）→ 强制重扫，不吃那个 0.5 秒的短时缓存
    key = _group_scan_key(force=_ROUTE_INDEX_KEY.get("key") is None)
    if _ROUTE_INDEX_KEY.get("key") == key:
        return

    key, entries = _group_scan()
    if entries is None:          # 组读不了：保持上次索引，别清空
        return

    index, categories = {}, {}
    for category, _path, project in entries:
        name = os.path.basename(str(project.get("name") or ""))
        material = _material_from_project_checked(project)
        if not material:
            continue
        if name:
            index.setdefault(name, material)
        # 一个材料可能同时出现在多个组里（比如石珀既在矿物组又在特产组）——
        # 按 CATEGORY_ORDER 的顺序扫，setdefault 保留**先命中的**那个类别（特产优先）。
        # 时长另有单品覆盖表 MATERIAL_HOURS，所以类别取哪个都不影响矿物按矿种算。
        categories.setdefault(material, category)

    _ROUTE_INDEX.clear()
    _ROUTE_INDEX.update(index)
    _ROUTE_CATEGORIES.clear()
    _ROUTE_CATEGORIES.update(categories)
    _ROUTE_INDEX_KEY["key"] = key


def route_material_index():
    """{路线文件名: 材料名}（四类资源的脚本组都算）。

    为什么需要它：日志里只有一条路线的**文件名**，而有些路线的文件名不带材料
    （`1灵濛山.json`、`3药蝶谷.json`）或前缀不是纯数字（`09A-清心-…`）——
    这时就得回查脚本组，看这个文件挂在哪个材料目录下。
    """
    _refresh_route_index()
    return _ROUTE_INDEX


def material_category_index():
    """{材料名: 类别}（由它出现在哪个脚本组决定）。"""
    _refresh_route_index()
    return _ROUTE_CATEGORIES


def category_of(material):
    """这种材料属于哪一类（特产 / 矿物 / 食材与炼金 / 敌人与魔物）。

    类别**只看它挂在哪个脚本组**：那是玩家自己分的类，比任何字典都准
    （实测百科字典里连霜仙花、久雨莲这些新地区的特产都没有）。
    查不到的（例如手动登记的材料）按"地区特产"处理。
    """
    material = str(material or "").strip()
    if not material:
        return CATEGORY_SPECIALTY
    return material_category_index().get(material, CATEGORY_SPECIALTY)


def hours_for(material, category=None):
    """这种材料的冷却时长（小时）：先看按材料覆盖的表，再按类别。"""
    category = category or category_of(material)
    override = MATERIAL_HOURS.get(str(material or "").strip())
    if override and category in (CATEGORY_MINE, CATEGORY_SPECIALTY):
        # 矿物按材料分档（铁/星银/水晶…）；石珀、夜泊石这类"既算特产又是矿点"的也照此
        return int(override)
    return category_hours(category)


def material_for_route(route_name):
    """给一个路线文件名，猜出它属于哪种材料（先文件名、再回查脚本组；都没有给空串）。

    ⚠️ 文件名和脚本组**对不上**时以脚本组为准：`01-那夏镇下方-4个.json` 挂在
    `矿物\\虹滴晶` 下，文件名读出来的是地名 —— 不这么办，这种路线的采集记录会整条丢掉。
    """
    name = os.path.basename(str(route_name or ""))
    from_name = _material_from_route_name(name)
    known = route_material_index().get(name, "")
    if known and from_name and known != from_name:
        return known
    return from_name or known or ""


def route_material_vocabulary(categories=None):
    """脚本组路线里出现过的材料名（= BGI 真能去刷的东西）。

    默认返回**四类全部**（特产 / 矿物 / 食材与炼金 / 敌人与魔物里的魔物名）；
    传 `categories=("specialty",)` 就只要地区特产。
    """
    mapping = material_category_index()
    if categories is None:
        return set(mapping)
    wanted = {str(item) for item in categories}
    return {material for material, category in mapping.items() if category in wanted}


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


# 各类别里"被查的东西"该叫什么（写提示语用）
CATEGORY_KINDS = {
    CATEGORY_SPECIALTY: "采集物",
    CATEGORY_MINE: "矿物",
    CATEGORY_COOK: "食材",
    CATEGORY_HUNT: "魔物",
}
# 各类别查不到时给的例子（照着说就能对上）
CATEGORY_EXAMPLES = {
    CATEGORY_SPECIALTY: "霜仙花",
    CATEGORY_MINE: "水晶块",
    CATEGORY_COOK: "久雨莲",
    CATEGORY_HUNT: "蕈兽",
}


def resolve_material(target, category=None):
    """把玩家/模型给的目标解析成材料名（或魔物名）。返回 (名字, 说明)；认不出来给 ("", 原因)。

    `category` 给定时**只在这个类别里找**（「刷点蕈兽」不该被当成特产去查），
    并且不会去做"角色名 → 特产"的翻译（那只对特产有意义）。

    词表来自脚本组（见 `_group_scan()`）：四个资源总组 **+ 玩家自建的魔物/材料小组**
    （蕈兽.json、骗骗花.json、虹滴晶.json…）—— 少了后者，"打蕈兽"会被判成认不出。
    调用方拿到认不出的目标时**保留**它，并且只在地区特产这一类里提示（见 `notice_lines`）。
    """
    text = str(target or "").strip()
    if not text:
        return "", "目标为空"
    text = _TARGET_TAIL_RE.sub("", text).strip()          # 「蓝砚的突破材料」→「蓝砚」
    text = re.sub(r"^(去|帮我|采集|采|摘|捡|刷|打|挖|跑)+", "", text).strip()

    category = str(category or "").strip() or None
    vocabulary = route_material_vocabulary((category,) if category else None)
    if text in vocabulary:
        return text, ("魔物名" if category == CATEGORY_HUNT else "材料名")

    # 「角色名 → TA 的特产」只对地区特产有意义
    if category in (None, CATEGORY_SPECIALTY):
        material, source = specialty_of(text)
        if material:
            return material, f"角色解析（{source}）"

    # 目标里嵌着地名（「采集霜仙花」残渣 / 「霜仙花的路线」）
    for material in sorted(vocabulary, key=len, reverse=True):
        if material and material in text:
            return material, ("魔物名（模糊匹配）" if category == CATEGORY_HUNT else "材料名（模糊匹配）")

    kind = CATEGORY_KINDS.get(category or CATEGORY_SPECIALTY, "采集物")
    where = CATEGORY_LABELS.get(category, "地图素材组") if category else "地图素材组"
    # 例子尽量挑**本机真的能跑**的名字，而且必须和玩家输入的不同 ——
    # 原来会写成「请直接说魔物名（例如「蕈兽」）」，而他输入的就是「蕈兽」，看着像死循环。
    example = next(
        (name for name in sorted(vocabulary) if name and name != text),
        CATEGORY_EXAMPLES.get(category or CATEGORY_SPECIALTY, "霜仙花"),
    )
    if category == CATEGORY_HUNT:
        hint = f"本地字典里没有这个角色，也不是{where}里的魔物名 —— 请直接说魔物名（例如「{example}」）"
    else:
        hint = (
            f"本地字典里没有这个角色，也不是{where}里的材料名 —— "
            f"请直接说材料名（例如「{example}」）"
        )
    return "", f"认不出「{text}」是哪种{kind}：{hint}"


def is_forced(text):
    """玩家是不是明确要求"就算没刷新也采"。"""
    normalized = re.sub(r"[\s，。！？,.!?]", "", str(text or ""))
    return any(word in normalized for word in FORCE_WORDS)


# ==========================================
# 🌟 给审批屏 / 提示词用的文本
# ==========================================


def notice_lines(targets, category=None):
    """给审批屏的计划行：每个目标一行状态。

    返回 (lines, blocked_materials)；blocked = 还在冷却、本次不该跑的。
    `category` 会给目标解析限定类别（刷怪时只按魔物名解析，别把「蕈兽」当特产查）。

    ⚠️ 认不出的目标**只有地区特产会提示**：特产词表是"脚本组里 59 种草"这种封闭集合，
    认不出基本就是名字写错了，值得说一句；而魔物 / 矿物 / 食材这两边的名字是开放的
    （玩家自建小组、掉落名、地名混着来），我们的词表定不了它是无效目标 ——
    这时候报「认不出「蕈兽」是哪种魔物（例如「蕈兽」）」只会让人以为指令错了
    （玩家实测报过这个 bug），所以**静默跳过**，交给下游"找不到路线"去说。
    """
    lines = []
    blocked = []
    kind = CATEGORY_KINDS.get(category or CATEGORY_SPECIALTY, "采集")
    for target in targets or ():
        material, note = resolve_material(target, category=category)
        if not material:
            if category in (None, CATEGORY_SPECIALTY):
                lines.append(f"⚠️ {kind}目标「{target}」：{note}")
            continue
        result = status(material, category=category)
        tag = "" if note.startswith(("材料名", "魔物名")) else f"（来自{note}）"
        if result["cooling"]:
            blocked.append(material)
            lines.append("⏳ " + describe(result).replace("⏳ ", "") + tag)
        else:
            lines.append("✅ " + describe(result).replace("✅ ", "") + tag)
    return lines, blocked


def cooldown_block(now=None):
    """给大模型的"世界资源冷却"上下文（只列还在冷却里的，避免刷屏）。

    四类资源都算（特产 / 矿物 / 食材 / 魔物），每行都带类别与时长，模型据此可以：
    ① 认出"霜仙花还没刷新"；② 挑一个已经刷新的替代；③ 拿不准角色对应哪种采集物时问玩家。
    """
    rows = cooling_materials()
    if not rows:
        return ""
    hours = " / ".join(
        f"{CATEGORY_LABELS[item]} {category_hours(item)}h" for item in CATEGORY_ORDER
    )
    lines = [
        f"\n\n⏳ 【资源冷却】（{hours}；下面是**当前还没刷新**的）"
    ]
    for result in rows[:12]:
        lines.append("   · " + describe(result))
    if len(rows) > 12:
        lines.append(f"   · …还有 {len(rows) - 12} 项")
    lines.append(
        "   ↳ 玩家点名的目标如果在这张表里：**不要**安排，直接告诉他还要等多久"
        "（可以顺便建议一个已刷新的）；玩家明确说「强制采集 / 强制跑」时才照做。\n"
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

    parser = argparse.ArgumentParser(description="世界资源（特产/矿物/食材/魔物）冷却检测")
    parser.add_argument("--check", metavar="材料/角色/魔物", help="查一个目标的冷却状态")
    parser.add_argument("--manual", metavar="材料", help="手动记为刚采过")
    parser.add_argument("--clear", metavar="材料", nargs="?", const="", help="清掉手动记录（不给名字=全清）")
    parser.add_argument("--materials", action="store_true", help="按类别列出脚本组里能刷的东西")
    parser.add_argument("--category", metavar="类别",
                        help="配合 --check / --materials：specialty / mine / cook / hunt")
    args = parser.parse_args(argv)

    category = str(args.category or "").strip() or None
    hours = " / ".join(f"{CATEGORY_LABELS[item]} {category_hours(item)}h" for item in CATEGORY_ORDER)
    print(f"冷却时长：{hours}；往前扫 {scan_days()} 天日志（本次找到 {len(log_paths())} 个文件）")

    if args.materials:
        vocabulary = route_material_vocabulary((category,) if category else None)
        mapping = material_category_index()
        groups = {}
        for name in sorted(vocabulary):
            groups.setdefault(mapping.get(name, category or CATEGORY_SPECIALTY), []).append(name)
        print(f"共 {len(vocabulary)} 项：")
        for item in CATEGORY_ORDER:
            names = groups.get(item)
            if names:
                print(f"  【{CATEGORY_LABELS[item]}】{len(names)} 项：{'、'.join(names)}")
        return 0

    if args.manual:
        material, note = resolve_material(args.manual, category=category)
        material = material or args.manual
        mark_manual(material)
        print(f"✅ 已把「{material}」记为刚采过" + (f"（{note}）" if "材料名" not in note else ""))
        print("   " + describe(status(material)))
        return 0

    if args.clear is not None:
        clear_manual(args.clear or None)
        print("✅ 已清掉手动记录" if not args.clear else f"✅ 已清掉「{args.clear}」的手动记录")
        return 0

    if args.check:
        material, note = resolve_material(args.check, category=category)
        if not material:
            print("❌ " + note)
            return 1
        print(("🔎 解析：" + note + " → 「" + material + "」") if "材料名" not in note else "")
        print(describe(status(material)))
        return 0

    data = overview()
    if not data["summary"]["cooling"]:
        print("✅ 当前没有还在冷却的目标（最近都没跑过，或都已过刷新时间）")
        return 0
    print(f"⏳ 还在冷却的有 {data['summary']['cooling']} 项：")
    for section in data["sections"]:
        items = [row for row in section["materials"] if row["cooling"]]
        if not items:
            continue
        print(f"  【{section['label']}】刷新 {section['hours']} 小时")
        for result in items:
            print("   " + describe(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
