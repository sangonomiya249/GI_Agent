import json
import re
from functools import lru_cache

EXCEPTION_MAP = {
    "草": "深林的记忆",
    "风": "翠绿之影",
    "水": "沉沦之心",
    "火": "炽烈的炎之魔女",
    "冰": "冰风迷途的勇士",
    "下落": "长夜之誓",
}

# 副本名最长 8 个字（和 domain_match._ARTIFACT_NAME_LIMIT 保持一致）：
# 百科那一列有些行是整段说明文字（「地图宝箱概率获取； 精英级敌人及部分BOSS概率掉落； …」），
# 以前会把这 50~91 个字整段当成副本名下发 —— 审批屏显示一段散文，玩家点 y 之后
# resolve_domain_target 直接抛「无法把 '…' 解析成秘境」，整轮被拒。
_NAME_LIMIT = 8
_NAME_MIN = 3
_NOT_IN_A_NAME = "；;。，,、 \n\t"
# 说明文字里常见的通用词：真正的秘境名不会带这些（「圣遗物秘境」「炼武秘境」是标签，不是名字）
_GENERIC_WORDS = (
    "概率", "掉落", "获取", "获得", "奖励", "宝箱", "秘境", "任务", "兑换", "商店",
    "合成", "锻造", "活动", "版本", "见闻", "敌人", "精英", "首领", "概率开", "层",
)
_CN_RE = re.compile(r"^[\u4e00-\u9fff·]+$")


def _looks_like_domain_name(value):
    """这一小段文字像不像秘境名：纯中文、3~8 字、不含句读、不含说明性通用词。"""
    name = str(value or "").strip()
    if not _NAME_MIN <= len(name) <= _NAME_LIMIT:
        return ""
    if any(ch in name for ch in _NOT_IN_A_NAME):
        return ""
    if not _CN_RE.match(name):          # 罗马数字/拉丁字母/特殊符号一律不算名字
        return ""
    if any(word in name for word in _GENERIC_WORDS):
        return ""
    return name


@lru_cache(maxsize=1)
def _known_boss_names():
    """已知首领名（用来把「爆炎树」这种"从首领掉落"的答案排除掉 —— 那不是秘境）。"""
    try:
        from skills.boss_pathing_guard import load_fallback_boss_names

        return frozenset(str(name) for name in (load_fallback_boss_names() or ()))
    except Exception:
        return frozenset()


def _is_known_boss(name):
    return str(name) in _known_boss_names()


def _candidates(value):
    """把一格「获取途径」拆成"像名字的短串"候选（按可信度排序）。

    真实数据里三种写法混在一起：
      · `霜凝的华彩：小精灵的任务…`            → 「名字：说明」，冒号前就是秘境名
      · `地图宝箱概率获取； …； 铭记之谷：…`    → 前面全是散文，真名藏在后面（同样带冒号）
      · `月童的库藏`                            → 整格就是一个短名字
    所以规则是：带冒号的片段取冒号前那一小段；不带冒号的片段**只有整格就是它**时才算名字
    （否则「深境螺旋」这种散文碎片会被误当成秘境）。
    """
    text = str(value or "").strip()
    if not text:
        return []

    out = []
    whole = _looks_like_domain_name(text)
    if whole and not _is_known_boss(whole):
        out.append(whole)

    for fragment in re.split(r"[；;。，,、\s\n\t]+", text):
        if "：" not in fragment and ":" not in fragment:
            continue
        head = re.split(r"[：:]", fragment, maxsplit=1)[0]
        name = _looks_like_domain_name(head)
        if name and not _is_known_boss(name) and name not in out:
            out.append(name)
    return out


def _known_domain_names(raw_json_data):
    """从整份字典里收集"确定是秘境名"的短串（各词条冒号前那一小段）。

    用途：散文型单元格里，"地图宝箱概率获取"这种片段长得也像名字，
    只有拿"全字典里反复作为秘境名出现过的短串"去比，才能把真正的秘境名挑出来。
    """
    names = set()
    for data in (raw_json_data or {}).values():
        for table in (data or {}).get("matched_tables", []) or []:
            for row in (table or {}).get("rows") or []:
                if len(row) >= 2 and row[0] == "获取途径":
                    for name in _candidates(row[1])[:2]:      # 只看最靠前的两个，避免把散文片段当名字
                        names.add(name)
    return names


def get_domain_by_user_intent(user_input, raw_json_data):
    # 1. 掐头去尾，提取核心词 (把 "我要刷绝缘套" 变成 "绝缘")
    keyword = user_input.replace("套", "").strip()
    
    # 2. 查例外字典
    official_name = EXCEPTION_MAP.get(keyword)
    
    # 3. 如果不是例外，遍历 JSON 找子串
    if not official_name:
        for item_id, data in raw_json_data.items():
            title = data.get("title", "")
            if keyword in title:
                official_name = title
                break

    if not official_name:
        return "未找到对应副本"

    # 4. 找到官方全名后，从「获取途径」里挑秘境名
    known = _known_domain_names(raw_json_data)
    for item_id, data in raw_json_data.items():
        if data.get("title") != official_name:
            continue
        for table in data.get("matched_tables", []):
            for row in table.get("rows", []):
                if len(row) < 2 or row[0] != "获取途径":
                    continue
                candidates = _candidates(row[1])
                for name in candidates:          # 优先返回"全字典公认的秘境名"
                    if name in known:
                        return name
                if candidates:                  # 一个都对不上（字典太小）就退回第一个像名字的
                    return candidates[0]

    return "未找到对应副本"