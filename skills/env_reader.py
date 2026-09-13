import datetime
import json
import os
import requests
import re
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

# 🌟 Boss 名必须由「唯一映射到首领的突破材料」推出，不能按位置猜（旧逻辑 12/127 角色是错的）
from skills.char_boss_match import resolve_boss_from_materials

# 秘境阶段名 → BetterGI 认得的秘境名。
# 百科给的是秘境里的【阶段名】（深没之谷/水光之城/鸣雷城墟…），BetterGI 的自动秘境
# 要的是秘境名（塞西莉亚苗圃/震雷连山密宫…）。提到模块级是因为 domain_match 也要用。
DOMAIN_FIX_MAP = {
    "水光之城": "塞西莉亚苗圃", "深没之谷": "塞西莉亚苗圃", "渴水的废都": "塞西莉亚苗圃",
    "雷云祭坛": "震雷连山密宫", "鸣雷废墟": "震雷连山密宫", "古雷试炼场": "震雷连山密宫",
    "沉沙之渊": "砂流之庭", "砂之祭场": "砂流之庭", "流沙之葬": "砂流之庭",
    "云垢": "有顶塔", "思惑": "有顶塔", "引业": "有顶塔",
    "机思": "深潮的余响", "匠理": "深潮的余响", "奇械": "深潮的余响",
    "冥见": "深古瞭望所", "究观": "深古瞭望所", "测度": "深古瞭望所",
    "明辉": "失落的月庭", "祷颂": "失落的月庭", "祭月": "失落的月庭",

    "霜凝祭坛": "忘却之峡", "冰封废渊": "忘却之峡", "沉睡之国": "忘却之峡",
    "炽炎祭场": "太山府", "深炎之底": "太山府", "焚尽之环": "太山府",
    "初雷幽谷": "菫色之庭", "真葛废都": "菫色之庭", "菫染之国": "菫色之庭",
    "圆镜": "昏识塔", "律藏": "昏识塔", "妙语": "昏识塔",
    "旋韵": "苍白的遗荣", "箴铭": "苍白的遗荣", "琅诵": "苍白的遗荣",
    "转竟": "蕴火的幽墟", "旋复": "蕴火的幽墟", "空华": "蕴火的幽墟",
    "墟都": "无光的深都", "覆巢": "无光的深都", "遗荫": "无光的深都",
    # 鸣雷城墟：百科里 18 把武器（磐岩结绿/息灾/匣里灭辰/昭心…）的炼武秘境只写了
    # 阶段名、没写秘境名，按同族「雷云祭坛/鸣雷废墟/古雷试炼场」的归属补上
    "鸣雷城墟": "震雷连山密宫",
}


def mob_name_from_sources(lines):
    """从「【40级以上】巡陆艇掉落；」这类来源描述里抠出魔物名。

    只认"整行就是一个魔物名 + 掉落"的写法：
      · `巡陆艇掉落；`             → 巡陆艇
      · `【40级以上】巡陆艇掉落；`  → 巡陆艇（去掉等级前缀）
      · `遗迹守卫、遗迹重机、遗迹猎者掉落；` → 遗迹守卫（并列时取第一个，路线也是按魔物名找的）
      · `怪物掉落`                 → 空（这是类别，不是魔物名）
      · `掉落；`                   → 空（百科本身没写是哪种怪）
      · `无相风掉落吧<br>`          → 空（混了标点/HTML，是玩家评论，不是官方来源）
    抠不到就返回空串，调用方退回"讨伐大世界魔物"这种说法，绝不瞎猜。
    """
    for line in lines or []:
        text = str(line).strip().rstrip("；;。，,、 \t")
        if not text.endswith("掉落") or text == "怪物掉落":
            continue
        name = text[: -len("掉落")]
        name = re.sub(r"^(【[^】]*】|\d+级)", "", name).strip("【】 ")
        name = name.split("、")[0].split("/")[0].strip()
        if 2 <= len(name) <= 10 and not any(word in name for word in ("合成", "兑换", "获得", "购买")):
            return name
    return ""


_YATTA_CACHE = {"key": None, "value": None}


def load_yatta_dict_safely():
    """按需精准提取：168特产 + 严格锁定最高级(Ⅳ)的秘境材料 + 自动补全秘境名称

    ⚠️ 结果按「文件路径 + mtime + Boss 脚本路径」缓存：这份字典有几 MB，一轮对话里会被材料提取、
    米游社补全、名字识别各叫一次 —— 每次都重新解析既慢又会把日志刷满。

    ⚠️ 缓存键里为什么还要 Boss 脚本路径：表里的 `boss_name` 是靠脚本的首领名单
    （`assets/Pathing` + `boss-list.json`）反查出来的。BGI 路径不对时（没装 / 指向别处）
    反查会全部失败、退化成「涤净青金的来源Boss」这种胡话 —— 一旦把这份错表缓存住，
    进程里后面所有角色都会一直是错的（实测：测试里换个目录就污染后面所有用例）。
    """
    dict_path = config.project_path("memory", "game_dict_baike_full.json")
    try:
        stamp = os.path.getmtime(dict_path)
    except OSError:
        stamp = None
    cache_key = (dict_path, stamp, str(getattr(config, "BGI_BOSS_CONFIG", "")))
    if stamp is not None and _YATTA_CACHE["key"] == cache_key and _YATTA_CACHE["value"] is not None:
        return _YATTA_CACHE["value"]

    id_to_zh = {}
    if not os.path.exists(dict_path):
        print(f"⚠️ 未找到字典文件: {dict_path}")
        return id_to_zh
        
    schedule_pattern = re.compile(r'周[一二三四五六日]')
    
    try:
        with open(dict_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        def dig_data(node):
            if isinstance(node, dict):
                if "url" in node and "name_zh" in node:
                    for part in str(node["url"]).split("/"):
                        if part.isdigit():
                            final_materials = []
                            
                            # 1. 角色特产 (168 锚定)
                            total_cost = node.get("ascension_total_cost", {}).get("materials", {})
                            for m_name, m_count in total_cost.items():
                                if m_count == 168:
                                    final_materials.append({
                                        "name": m_name,
                                        "type": "特产",
                                        "schedule": "大世界采集"
                                    })
                                    break 

                            # 2. 秘境材料扫描 (修复双黄蛋：引入 seen_mats 去重)
                            source_categories = [
                                node.get("talent_materials", {}).get("sources", {}),
                                node.get("ascension_material_sources", {})
                            ]
                            
                            seen_mats = set()
                            for source_dict in source_categories:
                                for m_name, m_info in source_dict.items():
                                    # 去重逻辑
                                    if m_name in seen_mats:
                                        continue
                                        
                                    sched = m_info.get("schedule")
                                    if sched and schedule_pattern.search(sched):
                                        sched = sched.strip()
                                        # 🌟 核心修复：放宽匹配条件！
                                        # 只要字符串里【包含】中文的 Ⅳ 或者英文的 IV，且不是泛指的 /Ⅳ 即可
                                        if ('Ⅳ' in sched or 'IV' in sched) and ('/Ⅳ' not in sched):
                                            seen_mats.add(m_name) # 记录已处理的材料
                                            
                                            found_fix = False
                                            for keyword, real_name in DOMAIN_FIX_MAP.items():
                                                if keyword in sched:
                                                    sched = f"【{real_name}】{sched}"
                                                    found_fix = True
                                                    break
                                            
                                            final_materials.append({
                                                "name": m_name,
                                                "type": "秘境材料",
                                                "schedule": sched
                                            })

                            # 3. 提取大世界 Boss 名称及材料 (增强版正则与回退机制)
                            # 🌟 3. 提取大世界 Boss 名称及材料 (反向查表优先，正则兜底)
                            boss_mat_name = None
                            boss_name = None
                            try:
                                # 🌟 用「唯一映射到首领」的材料定 Boss，且优先用平的 materials 字段：
                                # 本地字典里的 ascension_materials_detail.tiers 对部分角色是**错的**
                                # （莫娜的 tier 里写着温迪的塞西莉亚花、丽莎的 tier 里是冰系角色的
                                # 钩钩果、埃洛伊的 tier 是某个单手剑角色的数据），只有 materials 是对的。
                                # 旧逻辑还按位置取第 2 个材料，会取到元素宝石或怪物掉落，实测 127 个
                                # 角色里有 12 个因此拿到错误的 Boss 名（莫娜→无相之风、行秋→秘源机兵·构型械…），
                                # 再把这份错误上下文喂给大模型，就会诱导它按元素去猜 Boss。
                                char_materials = list(node.get("materials") or [])
                                if not char_materials:
                                    tiers = node.get("ascension_materials_detail", {}).get("tiers", [])
                                    if len(tiers) > 1:
                                        char_materials = list(tiers[1].get("materials", {}).keys())

                                if char_materials:
                                    resolved_boss, resolved_mat = resolve_boss_from_materials(char_materials)
                                    if resolved_boss:
                                        boss_name, boss_mat_name = resolved_boss, resolved_mat
                                    else:
                                        # 兜底：旧版正则匹配（万一字典没更新全）
                                        boss_mat_name = (
                                            char_materials[1] if len(char_materials) > 1 else char_materials[0]
                                        )
                                        sources = node.get("ascension_material_sources", {}).get(boss_mat_name, {}).get("source_lines", [])
                                        for line in sources:
                                            match = re.search(r'(?:【?\d+级以上】?)(.*?)掉落', line)
                                            if match:
                                                boss_name = match.group(1).strip()
                                                break

                                        if not boss_name:
                                            for line in sources:
                                                if "掉落" in line and "获得" not in line:
                                                    boss_name = line.split("掉落")[0].replace("获得方式：", "").strip("【】 ")
                                                    break

                                        if not boss_name:
                                            boss_name = f"{boss_mat_name}的来源Boss"
                            except Exception:
                                pass

                            # 4. 🌟 敌人掉落（普通怪物掉落）：靠讨伐大世界魔物获得的突破材料
                            #    字典里本来就有 —— `ascension_total_cost` 里那些打怪掉的材料，
                            #    它们的 `ascension_material_sources[name].source_lines` 写着
                            #    「怪物掉落」，而且常常直接点名魔物：
                            #      雅珂达的机轴 → 「【40级以上】巡陆艇掉落； | 星尘兑换 | 怪物掉落」
                            #    旧逻辑只挑"168 个的特产"和"带秘境日程的材料"，把这一类整类丢掉了，
                            #    于是大模型在展柜里只看得到 特产/秘境/Boss，误以为"没有普通怪物掉落"，
                            #    只能回头问玩家"是哪种材料/哪只魔物"。
                            for m_name, m_count in (total_cost or {}).items():
                                if m_name in seen_mats or m_name in ("摩拉",) or m_name == boss_mat_name:
                                    continue
                                info = (node.get("ascension_material_sources") or {}).get(m_name) or {}
                                lines = [str(x) for x in (info.get("source_lines") or [])]
                                if not any("怪物掉落" in line for line in lines):
                                    continue
                                mob = mob_name_from_sources(lines)
                                final_materials.append({
                                    "name": m_name,
                                    "type": "敌人掉落",
                                    "schedule": f"讨伐【{mob}】" if mob else "讨伐大世界魔物",
                                    # 字典里是"突破到满级的总需求"，不是当前缺口（缺口只有 Boss 材料算得出来）
                                    "total": m_count,
                                })

                            id_to_zh[part] = {
                                "name": node["name_zh"],
                                "materials": final_materials,
                                "boss_mat_name": boss_mat_name, 
                                "boss_name": boss_name          
                            }
                            break
                for value in node.values():
                    dig_data(value)
            elif isinstance(node, list):
                for item in node:
                    dig_data(item)
                    
        dig_data(data)
        print(f"📚 百科字典加载完毕：已根据七国规则完成秘境名称自动校准与Boss寻址。")
        _YATTA_CACHE.update({"key": cache_key, "value": id_to_zh})
        return id_to_zh
        
    except Exception as e:
        print(f"❌ 解析百科字典失败: {e}")
        return {}


def _build_retry_session():
    retry = Retry(total=3, connect=3, read=3, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=frozenset(["GET"]), raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# 等级段 → 到 81 级还缺多少 Boss 材料（百科的突破总消耗是整段累加的，这里按当前等级取缺口）
BOSS_MATERIAL_NEEDED_BY_LEVEL = ((40, 46), (50, 44), (60, 40), (70, 32), (81, 20))


def boss_material_needed(level):
    """按当前等级算"到 81 级还缺多少 Boss 材料"；已毕业（≥81）返回 0。"""
    try:
        level = int(level or 0)
    except (TypeError, ValueError):
        return 0
    for threshold, needed in BOSS_MATERIAL_NEEDED_BY_LEVEL:
        if level < threshold:
            return needed
    return 0


def build_weapon_entry(weapon_id, level, yatta_dict, name=None):
    """按武器 id 组装 weapon 字段（材料从百科字典里取）。"""
    data = (yatta_dict or {}).get(str(weapon_id)) or {}
    return {
        "name": name or data.get("name") or f"未知武器({weapon_id})",
        "level": int(level or 0),
        "materials": list(data.get("materials", [])),
    }


def build_avatar_entry(avatar_id, level, yatta_dict, name=None, weapon=None, skills=None):
    """把「角色 id + 等级 + 武器 + 天赋」组装成展柜 JSON 里的一条 avatar。

    🌟 Enka（展柜）和米游社（个人战绩）共用这一份 —— 否则"从米游社补进来的角色"
    材料字段形状会不一样，材料提取 / domain_match / 提示词就都得写两套分支。
    角色名以字典为准（米游社也给名字，但字典里的和展柜里的写法一致）。
    """
    data = (yatta_dict or {}).get(str(avatar_id)) or {}
    level = int(level or 0)

    materials = list(data.get("materials", []))
    boss_name = data.get("boss_name")
    needed = boss_material_needed(level)
    if boss_name and needed:
        materials.append({
            "name": data.get("boss_mat_name", "Boss材料"),
            "type": "Boss材料",
            "schedule": f"【{boss_name}】",
            "needed": needed,
        })

    return {
        "name": name or data.get("name") or f"未知角色({avatar_id})",
        "level": level,
        "materials": materials,
        "weapon": weapon or {"name": "未装备", "level": 0, "materials": []},
        "skills": skills or {},
    }


def fetch_enka_data(uid):
    url = f"https://enka.network/api/uid/{uid}"
    headers = {"User-Agent": "GenshinAgent/1.0 (Testing)"}
    proxy_url = os.getenv("GENSHIN_PROXY", "http://127.0.0.1:7890")
    proxies = {"http": proxy_url, "https": proxy_url}
    use_proxy = os.getenv("ENKA_USE_PROXY", "1") != "0"
    
    yatta_dict = load_yatta_dict_safely()
    print(f"🔄 正在拉取 UID {uid} 的展柜数据...")

    try:
        data = None
        plans = [(proxies, "代理")] if use_proxy else []
        # ⚠️ `proxies=None` 并不等于"直连"：requests 会继续合并环境变量/系统代理，
        #    所以"代理"这条路挂了以后，"直连"兜底可能还在走那个已死的代理。
        #    显式传空字典才是真的不经过任何代理。
        plans.append(({}, "直连"))

        last_error = ""
        for p_proxies, p_name in plans:
            try:
                with _build_retry_session() as session:
                    response = session.get(url, headers=headers, proxies=p_proxies, timeout=(5, 20))
                response.raise_for_status()
                data = response.json()
                break
            except requests.RequestException as exc:
                # 裸 except 会把真实原因（代理没开 / 超时 / 429 / 返回非 JSON）全折成一句
                # "网络连通失败"，排查时完全看不出是哪种；这里至少把最后一个原因留下来。
                last_error = f"{p_name}失败：{type(exc).__name__} {exc}"

        if not data:
            return {"error": f"网络连通失败（{last_error or '未发起请求'}）"}
        
        raw_list = data.get("avatarInfoList", [])
        clean_avatars = []
        for avatar in raw_list:
            avatar_id = str(avatar.get("avatarId", ""))
            level = avatar.get("propMap", {}).get("4001", {}).get("val", "0")

            weapon_info = None
            for equip in avatar.get("equipList", []):
                if "weapon" in equip:
                    weapon_info = build_weapon_entry(
                        equip.get("itemId", ""),
                        equip.get("weapon", {}).get("level", 0),
                        yatta_dict,
                    )
                    break

            # 🌟 共用装配：材料和 Boss 缺口都由 build_avatar_entry 算，
            #    米游社那条路径走的是同一个函数（形状必须一致）
            clean_avatars.append(
                build_avatar_entry(
                    avatar_id,
                    level,
                    yatta_dict,
                    weapon=weapon_info,
                    skills=avatar.get("skillLevelMap", {}),
                )
            )

        print(f"✅ 成功读取 {len(clean_avatars)} 名角色及装备数据。")
        return {"avatars": clean_avatars}
        
    except Exception as e:
        return {"error": str(e)}

# ==========================================
# 🌟 展柜上下文的新鲜度管理
# ==========================================
# 踩过的坑：env_context 以前只在「为空」时抓一次，之后就永久使用这份缓存
# （还会写进 memory/chat_context.json 跨会话保留），而且缓存里没有任何时间戳。
# 实测后果：玩家让 Agent「打一次蓝砚的武器突破副本」，而喂给大模型的上下文里
# 那 24 个角色根本没有蓝砚 —— 它回「蓝砚及其武器均不在展柜 JSON 中」不是幻觉，
# 是输入过期（实时展柜里蓝砚 60 级、带着讨龙英杰谭）。
ENV_CONTEXT_KEY = "env_context"
ENV_CONTEXT_AT_KEY = "env_context_at"
# 展柜里都有谁（名字列表）：米游社补全用它判断"提到的角色是否已在展柜里"
ENV_CONTEXT_NAMES_KEY = "env_context_names"
_ENV_CONTEXT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 最近一次抓取的展柜结构化数据（同进程内存缓存）：
# 让 domain_match 这类解析器不必重新打一次 Enka，就能拿到"蓝砚带的是哪把武器"
_LATEST_ENV_DATA = {"uid": None, "fetched_at": None, "data": None}


def latest_env_data(uid=None, max_age_minutes=None):
    """取最近一次抓取的展柜数据；没有、uid 不符或过期则返回 None。"""
    entry = _LATEST_ENV_DATA
    if not entry.get("data"):
        return None
    if uid and entry.get("uid") and str(uid) != str(entry["uid"]):
        return None
    if max_age_minutes is not None:
        moment = entry.get("fetched_at")
        if not moment or datetime.datetime.now() - moment > datetime.timedelta(minutes=max_age_minutes):
            return None
    return entry["data"]


def fetch_env_data(uid):
    """抓一次展柜结构化数据，并更新进程内缓存。

    解析器（domain_match 等）直接复用这份，避免同一次规划里重复打 Enka
    ——不然每个调用点各抓一次，既慢又容易被 Enka 限流。
    """
    env_data = fetch_enka_data(uid)
    if isinstance(env_data, dict) and env_data.get("error"):
        raise RuntimeError(env_data["error"])

    _LATEST_ENV_DATA.update(
        {"uid": str(uid), "fetched_at": datetime.datetime.now(), "data": env_data}
    )
    return env_data


def fetch_env_context(uid):
    """抓展柜并组装带抓取时间的上下文。

    返回 (文本, 抓取时间字符串, 角色数)；网络失败时抛 RuntimeError。
    """
    env_data = fetch_env_data(uid)
    fetched_at_dt = _LATEST_ENV_DATA["fetched_at"]
    fetched_at = fetched_at_dt.strftime(_ENV_CONTEXT_TIME_FORMAT)
    avatar_count = len(env_data.get("avatars") or []) if isinstance(env_data, dict) else 0

    text = (
        f"以下是玩家 UID {uid} 的展柜数据（JSON，抓取于 {fetched_at}）：\n"
        f"{json.dumps(env_data, ensure_ascii=False)}\n"
        f"【以此为准】本段是刚刚抓取的展柜，优先级高于历史对话中任何关于展柜的旧结论"
        f"（包括你自己之前说过的「某角色不在展柜」）。要找的角色/武器请先在本段里查；"
        f"确认本段确实没有时，才让玩家输入 refresh 重新抓取。"
    )
    return text, fetched_at, avatar_count


def parse_env_context_time(fetched_at):
    try:
        return datetime.datetime.strptime(str(fetched_at), _ENV_CONTEXT_TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def describe_env_context_age(fetched_at, now=None):
    """把抓取时间说成人话；没有时间戳就明说不知道。"""
    moment = parse_env_context_time(fetched_at)
    if moment is None:
        return "抓取时间未知"

    now = now or datetime.datetime.now()
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 1:
        return "刚刚抓取"
    if minutes < 60:
        return f"{minutes} 分钟前抓取"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} 小时前抓取"
    return f"{hours / 24:.1f} 天前抓取"


def env_context_age_note(store, now=None):
    """展柜新鲜度提示，用于注入给大模型或打印给玩家。"""
    return f"展柜数据{describe_env_context_age((store or {}).get(ENV_CONTEXT_AT_KEY), now)}"


def refresh_store_env_context(store, uid, force=False, ttl_minutes=None):
    """抓最新展柜写进 store，并把抓取时间一起记下来。

    - force=True：忽略 TTL，立刻抓（CLI 启动、玩家输入 refresh 时用）
    - 否则：缓存比 TTL 新就跳过（飞书每条消息都调，避免把 Enka 打限流）
    返回 (是否可用, 提示文案)。**失败时保留旧缓存**：以前的写法会把好数据直接覆盖成
    「展柜数据暂不可用」，等于一次网络抖动就把整份展柜丢掉。
    """
    old_text = store.get(ENV_CONTEXT_KEY) or ""
    old_at = store.get(ENV_CONTEXT_AT_KEY)

    if not force and old_text:
        moment = parse_env_context_time(old_at)
        ttl = config.ENV_CONTEXT_TTL_MINUTES if ttl_minutes is None else ttl_minutes
        if moment is not None and datetime.datetime.now() - moment < datetime.timedelta(minutes=ttl):
            return True, f"⏳ 展柜数据仍然新鲜（{describe_env_context_age(old_at)}），跳过刷新"

    try:
        text, fetched_at, avatar_count = fetch_env_context(uid)
    except Exception as e:
        # ⚠️ 上一次失败时 store 里存的是一句错误占位（不是真的缓存），这时说"继续使用缓存数据"
        # 会让玩家以为展柜还能用；所以只有确认是"真缓存"（带抓取时间）才这么讲。
        if old_text and parse_env_context_time(old_at) is not None:
            return False, (
                f"⚠️ 展柜刷新失败（{e}），继续使用缓存数据（{describe_env_context_age(old_at)}）"
            )
        store[ENV_CONTEXT_KEY] = f"展柜数据暂不可用：{e}"
        store[ENV_CONTEXT_AT_KEY] = ""
        return False, f"❌ 展柜数据拉取失败: {e}"

    store[ENV_CONTEXT_KEY] = text
    store[ENV_CONTEXT_AT_KEY] = fetched_at
    # 🌟 顺手把"展柜里都有谁"记下来：米游社补全（skills/mys_api.py）靠它判断
    #    "玩家提到的角色是不是已经在展柜里"。已在展柜里的不重复注入，也不多发请求。
    try:
        names = [
            str(item.get("name"))
            for item in (env_data.get("avatars") or [])
            if item.get("name")
        ]
        store[ENV_CONTEXT_NAMES_KEY] = names
    except Exception:                                   # noqa: BLE001 —— 记不住就算了，别影响刷新
        pass
    return True, f"✅ 展柜数据已刷新：{avatar_count} 名角色（{fetched_at}）"


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import config

    res = fetch_enka_data(config.DEFAULT_UID)
    print(json.dumps(res, ensure_ascii=False, indent=4))