"""米游社「养成计算器」接口封装：**算材料需求** + **读背包真实库存**。

这是"材料拉取"的入口。规格书里两条最重要的规则都由这个文件兜住：

1. **需求只问米游社，不自己算**（§10）：角色 80→90、天赋 8→10/9/8、武器 80→90 各需要
   多少材料，一律调用米游社养成计算器，绝不在代码里硬编码需求表 —— 硬编码的表会随着
   游戏版本过期，而且过期的表现是"悄悄地规划错"，比报错难查一百倍。
2. **库存只信米游社**（§3）：返回的是背包里"现在真实有多少"，不是 BetterGI 捡了多少。

---

## 接口事实（写清楚出处，方便米游社改版时定位）

米游社养成计算器是网页活动 `act.mihoyo.com/ys/event/calculator`，它调的是一套
**静态资源域**上的接口（社区项目 miao-plugin 的 `models/mys/mysApi.js` 里就叫
`getAvatarList` / `compute` / `getMyItemList`）：

| 用途 | 方法 | 路径 |
| --- | --- | --- |
| 账号下全部角色（含天赋/武器等级） | GET | `/event/elevator/get-avatar-list` |
| 算某个角色的养成材料 | POST | `/event/elevator/compute` |
| 读背包真实库存 | GET | `/event/elevator/get-my-item-list` |

`compute` 的请求体（米游社前端自己的形状）：

```json
{
  "avatar_id": 10000089,
  "avatar_level_current": 80,
  "avatar_level_target": 90,
  "skill_list": [
    {"skill_id": 10891, "level_current": 8, "level_target": 10},
    {"skill_id": 10892, "level_current": 8, "level_target": 10},
    {"skill_id": 10895, "level_current": 8, "level_target": 10}
  ],
  "weapon": {"id": 12512, "level_current": 80, "level_target": 90}
}
```

响应里材料按用途分桶（`avatar_skill` / `avatar_weapon` 各自下一步就是对应阶段的材料），
本模块把**所有桶递归扫一遍**，按字符串键名匹配（见 `_PHASE_KEYS`）——
这样米游社把桶挪个位置、或者把 mora 单独放一层，都不至于整段失效。

## ⚠️ 这套接口的边界（老实说）

* **不是官方开放接口**：风控与签名都跟米游社 App 版本走，`MYS_SALT` / `MYS_APP_VERSION` 失效
  时这里会返回 401/风控错误码，改 `.env` 即可，不用改代码。
* **签名用 App 那套（DS）**：这几条路径在静态域上，实测接受带 DS 的请求；
  `_query_with_ds` 会把 DS 一起带上，服务端不校验也不会因此出错。
* **响应形状可能随版本变**：所以解析全部走"宽松识别 + 明确报错"，
  并且每次原始响应都会落一份到 `memory/mys/calculator/`（规格书 §6）——
  接口改了，看这份文件就知道变成什么样了。
* 自检：`python -m skills.mys_calculator --check` 会依次试"角色列表 / 背包 / 一次真实 compute"，
  把每一步的真实响应打出来。这是排查"材料拉不到"的第一现场。
* **万一路径不对**（现象是 `HTTP 404` + `404 page not found` —— 说明 cookie 与签名都没问题，
  只是这条路径不存在）：跑 `python -m skills.mys_calculator --probe`，
  它会用你的 cookie 把下面这张候选表挨个试一遍，把"哪条真的活着"直接打出来。
"""

import datetime
import json
import os

import config

from brain import growth_models
from skills import mys_api

# ==========================================
# 🌟 端点与常量
# ==========================================

# 养成计算器所在的域名（国际服是 sg-public-api.hoyolab.com，这里只做国服）。
# ⚠️ 注意是 `api-takumi.mihoyo.com`（**不是** `api-takumi-static.mihoyo.com`）——
#    后者是静态资源域，打上去只会得到一个 Go 的 `404 page not found`。
CALC_HOST = getattr(config, "MYS_CALC_HOST", "") or "https://api-takumi.mihoyo.com"

# 所有计算器接口共用的路径前缀。这一串是米游社养成计算器自己的活动命名空间
# （前端 `bundle_*.js` 里就是 `var m="/event/e20200928calculate"`）。
CALC_PREFIX = getattr(config, "MYS_CALC_PREFIX", "") or "/event/e20200928calculate"

# 三条接口都可以用 .env 覆盖 —— 米游社改版时**不用改代码**，也方便 `--probe`
# 探到正确路径后直接固定下来（见 `ENDPOINT_CANDIDATES` 上面的说明）。
#
# ⚠️⚠️ 2026-09 实测过一轮，结论如下（**别再凭印象改**）：
#   · `POST /v1/avatar/list`       ✅ 图鉴（角色列表）；`is_all` 才给全量，否则只有第一页 10 个
#   · `POST /v2/compute`           ✅ **算材料**（分三个桶：等级 / 天赋 / 武器）。
#        曾经误判成"接口废了"—— 真因是**请求体包了 `{"data": ...}` 外壳**，见 `compute_materials`。
#   · `POST /v1/sync/avatar/list`  ❌ **真的废了**：带完整 cookie 也一律 `-100 请先登录后参与活动`
#        （所以"账号里有哪些角色 / 背包"这件事**没法从这条拿**）
#   · `GET  /v1/sync/avatar/detail?avatar_id=`       ✅ 单个角色真实状态（等级/突破/天赋/武器/圣遗物），
#        还能判"这号有没有这个角色"（没有的返回 `-1 伙伴不存在`）
#   · `GET  /v1/avatar_cultivation/detail?avatar_id=` ✅ 养成方案（**只有天赋**材料，作兜底）
PATH_AVATAR_LIST = getattr(config, "MYS_CALC_PATH_AVATAR_LIST", "") or "/v1/avatar/list"
PATH_COMPUTE = getattr(config, "MYS_CALC_PATH_COMPUTE", "") or "/v2/compute"
# ★ `/v3/batch_compute` 比 `/v2/compute` 多给两样东西（实测）：
#   · `available_material` = **这个号已有的材料数量**（部分材料才有，别的缺席）；
#   · `overall_consume`     = 全部材料的合计。
#   有了 available_material 就能算出"还差多少"，所以**默认走 v3**，失败再退回 v2。
PATH_BATCH_COMPUTE = getattr(config, "MYS_CALC_PATH_BATCH_COMPUTE", "") or "/v3/batch_compute"
# ⚠️ 真的废了（保留常量只为 `--probe` 对照；别再拿它当数据源）
PATH_MY_ITEMS = getattr(config, "MYS_CALC_PATH_MY_ITEMS", "") or "/v1/sync/avatar/list"
# 单个角色的真实养成状态（GET，参数放 query：`avatar_id` + 账号三件套）
PATH_AVATAR_STATE = getattr(config, "MYS_CALC_PATH_AVATAR_STATE", "") or "/v1/sync/avatar/detail"
# 养成方案（GET，同上）
PATH_AVATAR_PLAN = getattr(config, "MYS_CALC_PATH_AVATAR_PLAN", "") or "/v1/avatar_cultivation/detail"

# 米游社"这个号没有这个角色"的报错：`retcode -1` + message 里带这段。用它判定"拥有关系"。
_NOT_OWNED_MARK = "伙伴不存在"

DEFAULT_LANG = "zh-cn"

# 探针用的"公认存在的角色"：单角色那两条 GET 必须带 `avatar_id`，
# 不带的话米游社回 `-100 / system error`，探针会把它误读成"路径不对"。
_PROBE_AVATAR_ID = 10000002          # 神里绫华

# 请求体的外层包装。
# ⚠️ **不是所有接口都要这层包装**，这一点曾经坑了很久：
#   · 图鉴 `/v1/avatar/list`：要（平铺会 10041）；
#   · 算材料 `/v2/compute`、`/v3/batch_compute`：**不要**（包了会得到 `retcode 0 + 空桶`）。
# 所以包装由调用方按接口决定（`_request_json(wrap=...)`），别再全局套一层。
def _wrap_body(payload):
    return {"data": payload or {}}


# 候选端点表（`--probe` 用）：`(GET 路径, POST 路径, 说明)`，None 表示这一族只有一种方法。
#
# ⚠️ 前两条是**实测确认过的**（写这一版时真跑通了），后面的只作为"米游社再次改版"时的备选。
ENDPOINT_CANDIDATES = (
    ("/v1/avatar/list", "/v2/compute",
     "★ 图鉴 / ★ 算材料（算材料**不能包 data 外壳**，见 compute_materials）"),
    ("/v1/sync/avatar/detail", "/v3/batch_compute",
     "★ 单个角色真实状态（GET，要 avatar_id）/ 批量算材料（body 同 compute，多了 items 数组）"),
    ("/v1/avatar_cultivation/detail", "/v1/sync/avatar/list",
     "养成方案 + 天赋材料（GET，要 avatar_id）／已废的角色列表（对照）"),
    ("/event/elevator/get-avatar-list", "/event/elevator/compute",
     "早期社区写法（已实测 404，只作对照）"),
    ("/v1/avatar_cultivation", "/v1/furniture/compute",
     "我的养成（快速列表）/ 摆设计算"),
)

# 阶段标记里的关键字：材料桶的键名（或它所在路径）里出现这些词，就归到该阶段。
# 用"包含"而不是"相等"，是因为米游社把键名从 camelCase 改成 snake_case 也不用改代码。
_PHASE_KEYS = (
    (growth_models.PHASE_CHARACTER_LEVEL, ("avatar", "character", "role", "level")),
    (growth_models.PHASE_WEAPON_LEVEL, ("weapon", "arms")),
    (growth_models.PHASE_TALENT, ("skill", "talent")),
)

# "这个桶是武器材料还是天赋材料"的判定顺序（先判更具体的，避免 weapon 桶里
# 带个 level 就被算成角色等级）
_PHASE_ORDER = (
    growth_models.PHASE_WEAPON_LEVEL,
    growth_models.PHASE_TALENT,
    growth_models.PHASE_CHARACTER_LEVEL,
)

_ITEM_LIST_KEYS = ("item_list", "itemlist", "items", "list", "cost", "costs",
                   "materials", "material_list", "materiallist", "consume")
_ITEM_ID_KEYS = ("item_id", "id", "material_id")
_ITEM_COUNT_KEYS = ("num", "count", "cnt", "quantity", "need_num", "number")
_ITEM_NAME_KEYS = ("name", "item_name", "name_zh")
_ITEM_ICON_KEYS = ("icon", "icon_url", "img")
# `lack_num` = 米游社按真实背包算出来的"还差多少"（有值就用它，比我们自己猜准）
_ITEM_LACK_KEYS = ("lack_num", "lack", "lack_count")


class CalculatorError(mys_api.MysError):
    """养成计算器不可用（响应形状认不出来等）。"""


# ==========================================
# 🌟 请求
# ==========================================


def calc_headers(query="", body="", cookie=None, path=None):
    """计算器请求头：和 App 那套一致（含 DS），额外带语言、wiki 标识与正确的 Referer。"""
    headers = mys_api.build_headers(
        f"{CALC_HOST}", query=query, body=body, cookie=cookie, path=path
    )
    headers.update({
        "x-rpc-language": DEFAULT_LANG,
        "x-rpc-wiki_app": "ys",
        "Accept": "application/json, text/plain, */*",
        # Referer 必须是计算器那一页：米游社的活动接口会拿它做来源校验
        "Referer": getattr(
            config,
            "MYS_CALC_WEB_URL",
            "https://act.mihoyo.com/ys/event/calculator/index.html",
        ),
        "Origin": "https://act.mihoyo.com",
    })
    return headers


def _base_query(uid=None, server=None, lang=DEFAULT_LANG):
    uid = str(uid or "").strip()
    server = str(server or "").strip() or mys_api.server_from_uid(uid)
    return {
        "game_biz": "hk4e_cn",
        "uid": uid,
        "region": server,
        "lang": str(lang or DEFAULT_LANG),
    }


def _encode_query(params):
    """按固定顺序拼 query（**DS 签的是这串原文**，顺序变了签名就对不上）。"""
    parts = []
    for key, value in params.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return "&".join(parts)


def full_path(path):
    """把接口路径拼成 `/event/e20200928calculate/...`（已是完整路径的就原样返回）。"""
    text = str(path or "")
    if not text:
        return CALC_PREFIX
    if text.startswith(CALC_PREFIX):
        return text
    return f"{CALC_PREFIX}{text}"


def _request_json(method, path, query="", body_obj=None, cookie=None, timeout=20,
                  snapshot_path=None, wrap=True):
    """发一次请求并返回 `data`；同时把**原始响应**落一份（规格书 §6 的排错依据）。

    复用 `mys_api._request` 的签名与错误分类，保证"Cookie 失效 / 风控 / 网络失败"
    在整个项目里是同一套异常类型，上层只需要认 `MysError`。

    `wrap=True` 把请求体包成 `{"data": {...}}`（图鉴那几条**要**这层包装）；
    算计材料的接口**不要**（传 `wrap=False`），见 `compute_materials()` 的注释。
    """
    request_body = _wrap_body(body_obj) if (wrap and body_obj is not None) else body_obj
    body_text = json.dumps(request_body, ensure_ascii=False) if request_body is not None else ""
    payload = mys_api._request(
        method=method,
        url=f"{CALC_HOST}{full_path(path)}",
        query=query,
        body_obj=request_body,
        cookie=cookie,
        timeout=timeout,
        headers=calc_headers(query, body_text, cookie=cookie),
    )
    if snapshot_path:
        _dump_raw(snapshot_path, payload)
    return payload


def raw_call(host, method, path, query="", body_obj=None, cookie=None, timeout=20, headers=None):
    """发一次请求，返回 `{"status", "retcode", "message", "data", "text", "url"}`，**不抛异常**。

    为什么需要它：排查"接口路径对不对"时，最要紧的是 **HTTP 状态码**和**响应正文预览**；
    而 `mys_api._request` 会把 404（正文不是 JSON）压成一句 `MysTransportError`，
    正好把这两条关键信息吃掉。`--probe` 就是靠这个函数看到真相的。
    """
    import httpx

    body = json.dumps(body_obj, ensure_ascii=False) if body_obj is not None else ""
    request_headers = headers or calc_headers(query, body, cookie=cookie)
    url = f"{host}{path}"
    target = f"{url}?{query}" if query else url
    result = {"status": 0, "retcode": None, "message": "", "data": {}, "text": "",
              "url": target, "error": ""}

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            if str(method).upper() == "POST":
                response = client.post(target, content=body.encode("utf-8"),
                                       headers=request_headers)
            else:
                response = client.get(target, headers=request_headers)
    except Exception as exc:            # noqa: BLE001 —— 网络层异常统一收进来
        result["error"] = f"{type(exc).__name__} {exc}"
        return result

    result["status"] = int(response.status_code)
    result["text"] = response.text[:2000]
    try:
        payload = response.json()
    except ValueError:
        result["error"] = f"不是 JSON（HTTP {response.status_code}）"
        return result

    result["retcode"] = payload.get("retcode")
    result["message"] = str(payload.get("message") or "")
    result["data"] = payload.get("data") or {}
    result["payload_keys"] = sorted(payload)[:12]
    return result


def _dump_raw(path, payload):
    """把原始响应用原子写落盘；失败只提示，绝不影响这次调用。"""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        print(f"⚠️ 米游社原始响应落盘失败（不影响本次结果）：{exc}")


def _cache_dir():
    return getattr(config, "GROWTH_MYS_DIR", "") or config.project_path("memory", "mys")


# ==========================================
# 🌟 角色列表（含天赋 / 武器等级）
# ==========================================


def fetch_avatar_list(uid=None, server=None, cookie=None, timeout=20, lang=DEFAULT_LANG,
                      save=True, owned_only=False, avatar_ids=None):
    """养成计算器里能算的全部角色档案（等级 / 天赋 id 与名字 / 武器）。

    两条路径（对应计算器界面上的两个页签）：

    · `owned_only=False`（默认）→ `POST /v1/avatar/list`：**全角色图鉴**
      （实测无需登录即可取到：10 条 / 页，含 `skill_list` 的 `id` 与 `pos_name`）。
      关键价值是拿到**计算器口径的天赋 id**（`11501` 这种），才能正确喂给 `/v2/compute`；
      它的 `avatar_level` 是"基础等级"（五星=5），**不是**你账号里的等级。
    · `owned_only=True` → `POST /v1/sync/avatar/list`：**你账号里拥有的角色**
      （带 `region` / `uid`，未登录会返回 `retcode -100`），这里的等级才是真实等级。
    """
    payload = {"is_all": True, "page": 1, "size": 200, "lang": str(lang or DEFAULT_LANG)}
    path = PATH_MY_ITEMS if owned_only else PATH_AVATAR_LIST
    if owned_only:
        payload.pop("is_all", None)
        payload.update({
            "avatar_ids": [int(item) for item in (avatar_ids or ())] or [],
            "region": str(server or "").strip() or mys_api.server_from_uid(uid),
            "uid": str(uid or "").strip(),
        })
        if not payload["avatar_ids"]:
            # 不带 avatar_ids 时米游社不知道要哪些角色 —— 先拿全量图鉴的 id 列表
            payload["avatar_ids"] = [row["id"] for row in
                                     parse_avatars(fetch_avatar_list(uid=uid, server=server,
                                                                     cookie=cookie, save=False))]
    snapshot = os.path.join(
        _cache_dir(), "calculator",
        "my_avatars.json" if owned_only else "avatar_list.json",
    ) if save else None
    return _request_json("POST", path, body_obj=_wrap_body(payload), cookie=cookie,
                         timeout=timeout, snapshot_path=snapshot)


def parse_avatars(payload):
    """从角色列表响应里取出角色数组（宽松：`list` / `avatars` / 嵌套一层都认）。

    返回统一形状：`[{id, name, level, rarity, element, skills:{id:level}, weapon:{...}}]`。

    ⚠️ "等级"有两种含义，别混：
      · 全量图鉴（`/v1/avatar/list`）给的是 `avatar_level` = **基础等级**（五星 = 5），
        它代表稀有度，不是你的练度 —— 所以这里用它填 `rarity`，`level` 留 0；
      · 账号角色（`/v1/sync/avatar/list`）才带真实等级。
    """
    raw_list = _find_avatar_array(payload)
    if raw_list is None:
        return []

    avatars = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        avatar_id = _as_int(_first(raw, ("id", "avatar_id", "character_id")))
        if not avatar_id:
            continue
        base_level = _as_int(_first(raw, ("avatar_level",)))
        max_level = _as_int(_first(raw, ("max_level",)))
        # 图鉴的 avatar_level 是稀有度（5 / 4），而 max_level 是等级上限（90）。
        # 真实练度只可能出现在 sync 接口里（那里有 level / level_current 字段）。
        real_level = _first(raw, ("level", "level_current", "avatar_level_current"))
        rarity = _as_int(_first(raw, ("rarity", "star", "rank")))
        if not rarity and max_level >= 90 and 4 <= base_level <= 5:
            rarity = base_level
        avatars.append({
            "id": avatar_id,
            "name": str(_first(raw, ("name", "name_zh", "avatar_name")) or ""),
            "level": _as_int(real_level) if real_level is not None else 0,
            "base_level": base_level,
            "max_level": max_level or 90,
            "rarity": rarity,
            "element": str(_first(raw, ("element", "element_name", "property",
                                        "element_attr_id")) or ""),
            "skills": parse_skills(raw),
            "weapon": parse_weapon(raw),
            # 原始条目留一份：`talents_by_slot()` 要用它读米游社给的槽位名（pos_name）
            "_raw": raw,
        })
    return avatars


# 角色数组可能出现的键名（大小写不敏感）：米游社换过一次命名风格
_AVATAR_LIST_KEYS = ("avatars", "avatar_list", "avatarlist", "list", "roles", "characters")


def _find_avatar_array(payload, depth=0):
    """递归找"看起来像角色数组"的那个列表（最多钻 3 层）。

    为什么不用一个写死的路径：米游社把角色列表从 `data.avatars` 改成 `data.list`
    或 `data.data.list` 时，**写死路径的实现会静默返回空列表** ——
    表现是"同步成功但一个角色都没有"，比报错难查得多。
    """
    if depth > 3:
        return None
    if isinstance(payload, list):
        return payload if payload and isinstance(payload[0], dict) else None
    if not isinstance(payload, dict):
        return None

    for key, value in payload.items():
        if str(key).lower() in _AVATAR_LIST_KEYS and isinstance(value, list):
            if not value or isinstance(value[0], dict):
                return value
    for value in payload.values():
        found = _find_avatar_array(value, depth + 1)
        if found is not None:
            return found
    return None


def parse_skills(raw):
    """天赋等级 `{skill_id: level}`（**只保留能升级的三个战斗天赋**）。

    计算器的 `skill_list` 里同时有"天赋"和"命座/被动"，靠 `max_level` 区分：
    能升级的战斗天赋是 `max_level > 1`（10），被动/命座是 `max_level == 1`。
    计算器前端自己也是这么筛的（`filterSkills: t.max_level !== 1`）。

    ⚠️ 图鉴响应里**没有等级**（只有 id / name / max_level）——那种情况下这里返回
    `{}`，表示"等级未知"，而不是编一个 1 出来（编出来的后果是每次规划都多刷一堆天赋书）。
    """
    container = None
    for key, value in (raw or {}).items():
        # 大小写 / 下划线都不敏感：skills / skillList / skill_list / talents 都认
        if _normalise_key(key) in ("skills", "skilllist", "talents", "talentlist") \
                and isinstance(value, list):
            container = value
            break
    if container is None:
        return {}

    skills = {}
    for item in container:
        if not isinstance(item, dict):
            continue
        skill_id = _as_int(_first(item, ("skill_id", "id", "talent_id")))
        if not skill_id:
            continue
        if _as_int(_first(item, ("max_level",)), 10) <= 1:
            continue                       # 被动 / 命座：不能升级，别混进天赋
        level = _first(item, ("level", "level_current", "level_target"))
        if level is None:
            continue                       # 图鉴没有等级：留空，让上层知道"未知"
        skills[str(skill_id)] = _as_int(level)
    return skills


def parse_talents(raw):
    """天赋的**完整档案**：`[{id, name, pos, max_level}]`。

    `pos` 是米游社自己给的槽位（`普通攻击` / `元素战技` / `元素爆发`）——
    有了它就不用再靠"技能 id 升序 = 普攻/战技/爆发"去猜了（那个假设对旅行者这类
    多形态角色并不成立）。
    """
    skills = []
    for key, value in (raw or {}).items():
        if _normalise_key(key) in ("skills", "skilllist", "talentlist", "talents") \
                and isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                skill_id = _as_int(_first(item, ("skill_id", "id", "talent_id")))
                if not skill_id:
                    continue
                if _as_int(_first(item, ("max_level",)), 10) <= 1:
                    continue
                skills.append({
                    "id": skill_id,
                    "name": str(_first(item, ("name", "skill_name")) or ""),
                    "pos": str(_first(item, ("pos_name", "pos")) or ""),
                    "max_level": _as_int(_first(item, ("max_level",)), 10),
                })
            break
    return skills


# 米游社给的天赋槽位（`pos_name`）→ 内部的三项
_POS_TO_SLOT = (
    ("普通攻击", "normal"),
    ("元素战技", "skill"),
    ("元素爆发", "burst"),
)


def talents_by_slot(raw):
    """把天赋档案按"普攻 / 战技 / 爆发"归位：`{"normal": {...}, "skill": {...}, ...}`。

    认不出槽位时按顺序兜底（和 `skill_ids_from_avatar` 的假设一致）。
    """
    ordered = parse_talents(raw)
    result = {}
    for talent in ordered:
        for keyword, slot in _POS_TO_SLOT:
            if keyword and keyword in talent["pos"]:
                result.setdefault(slot, talent)
                break
    if len(result) < 3:
        leftovers = [talent for talent in ordered if talent not in result.values()]
        for slot in ("normal", "skill", "burst"):
            if slot in result:
                continue
            if leftovers:
                result[slot] = leftovers.pop(0)
    return result


def _normalise_key(key):
    """键名归一化：去掉下划线和大小写差异（`itemList` / `item_list` → `itemlist`）。"""
    return str(key or "").replace("_", "").replace("-", "").lower()


def parse_weapon(raw):
    """武器档案 `{id, name, level, rarity}`（没装武器时返回 `{}`）。"""
    weapon = (raw or {}).get("weapon")
    if not isinstance(weapon, dict):
        return {}
    weapon_id = _as_int(_first(weapon, ("id", "weapon_id", "item_id")))
    if not weapon_id:
        return {}
    return {
        "id": weapon_id,
        "name": str(_first(weapon, ("name", "weapon_name")) or ""),
        "level": _as_int(_first(weapon, ("level", "level_current"))),
        "rarity": _as_int(_first(weapon, ("rarity", "star", "rank"))),
    }


def find_avatar(payload, avatar_id):
    """在角色列表响应里找一个角色（认不出来返回 None）。"""
    wanted = _as_int(avatar_id)
    for avatar in parse_avatars(payload):
        if avatar["id"] == wanted:
            return avatar
    return None


def avatar_uid(payload, default=""):
    return str(extract_uid(payload) or default or "")


def extract_uid(payload):
    """从任意响应里抠出 uid（各接口放的位置不太一样）。"""
    if not isinstance(payload, dict):
        return ""
    for key in ("uid", "role_id", "game_uid"):
        value = payload.get(key)
        if value:
            return str(value)
    for key in ("role", "player", "game_role"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            for sub in ("uid", "role_id", "game_uid"):
                if nested.get(sub):
                    return str(nested[sub])
    return ""


def extract_server(payload, default=""):
    if not isinstance(payload, dict):
        return str(default or "")
    for key in ("region", "server"):
        value = payload.get(key)
        if value:
            return str(value)
    return str(default or "")


# ==========================================
# 🌟 算材料需求（核心）
# ==========================================


def build_compute_body(avatar_id, level_current, level_target, skills=None,
                       skill_current=None, skill_target=None, weapon=None, weapon_id=None,
                       weapon_level_current=None, weapon_level_target=None,
                       element_attr_id=0, promote_level=0, from_user_sync=True):
    """组装 `compute` 的请求体（形状**照抄米游社计算器前端**）。

    `skills` 传 `{skill_id: {"current": 8, "target": 10}}`（推荐，能对得上米游社的技能 id）。

    ⚠️ 三个"不报错但算不出来"的坑（都实测踩过，改这里等于改事实）：
      1. 天赋条目里的 id 字段名必须是 **`id`**（不是 `skill_id`），且值要用
         **`group_id`**（`/v1/sync/avatar/detail` 的 `skill_list[].group_id`），
         不是天赋自己的 `id`；
      2. 等级的字段名是 **`avatar_level_current` / `avatar_level_target`**
         （写成 `level_current` / `level_target` → 一样返回空桶）；
      3. **请求体不能包 `{"data": ...}` 外壳** —— 见 `compute_materials()` 的注释。
    """
    body = {
        "avatar_id": _as_int(avatar_id),
        "avatar_level_current": _as_int(level_current),
        "avatar_level_target": _as_int(level_target),
    }
    if _as_int(element_attr_id):
        body["element_attr_id"] = _as_int(element_attr_id)
    if _as_int(promote_level):
        body["avatar_promote_level"] = _as_int(promote_level)
    # 前端的算法：UI 里的"当前等级"和游戏里真实等级一致 → from_user_sync = true。
    # 我们本来就拿真实状态当起点，所以恒为 true。
    body["from_user_sync"] = bool(from_user_sync)

    skill_entries = []
    if isinstance(skills, dict):
        for skill_id, payload in skills.items():
            payload = payload if isinstance(payload, dict) else {}
            current = payload.get("current")
            target = payload.get("target")
            if target is None or _as_int(target) <= 0:
                continue
            if _as_int(current) == _as_int(target):
                continue        # 已达标的天赋不往请求里塞，省一次无效条目
            skill_entries.append({
                "id": _as_int(skill_id),
                "level_current": _as_int(current),
                "level_target": _as_int(target),
            })
    body["skill_list"] = skill_entries

    weapon_id = _as_int(weapon_id if weapon_id is not None else (weapon or {}).get("id"))
    weapon_current = _as_int(
        weapon_level_current if weapon_level_current is not None
        else ((weapon or {}).get("level") or (weapon or {}).get("level_current"))
    )
    weapon_target = _as_int(
        weapon_level_target if weapon_level_target is not None else (weapon or {}).get("level_target")
    )
    if weapon_id and weapon_target and weapon_current != weapon_target:
        body["weapon"] = {
            "id": weapon_id,
            "level_current": weapon_current,
            "level_target": weapon_target,
        }
    else:
        body["weapon"] = {}
    return body


def parse_available_material(data):
    """`available_material` → `{item_id: 已有数量}`。

    ⚠️ 这是**部分**数据：只有米游社认得的那几种材料会出现，其余材料**缺席**
    （不是 0）。所以调用方必须区分"缺席"和"0" —— 缺席就是"不知道"，
    不能当成"一个都没有"（也不能当成"够用"）。
    """
    owned = {}
    if not isinstance(data, dict):
        return owned                     # 响应形状不对时安静返回空，不炸
    for row in (data.get("available_material") or ()):
        if not isinstance(row, dict):
            continue
        item_id = _as_int(_first(row, _ITEM_ID_KEYS))
        if item_id:
            owned[item_id] = _as_int(_first(row, _ITEM_COUNT_KEYS))
    return owned


def compute_materials(avatar_id, level_current, level_target, skills=None, weapon=None,
                      uid=None, server=None, cookie=None, timeout=20, save=True, **kwargs):
    """问米游社：这个角色从当前状态到目标状态，一共差多少材料。

    返回：
        {
          "avatar_id": 10000089,
          "requirements": [ {item_id, item_name, category, required, lack, owned, phases}, ... ],
          "by_phase": {"character_level": [...], "weapon_level": [...], "talent": [...]},
          "raw": {...},          # 原始响应（调试用，别往前端传）
          "body": {...},         # 实际发出去的请求体（排查签名/参数问题要看它）
        }

    **走 `/v3/batch_compute`**（它多给 `available_material` = 你已有的材料），
    拿不到就退回 `/v2/compute`。
    """
    params = _base_query(uid=uid, server=server)
    query = _encode_query(params)
    body = build_compute_body(avatar_id, level_current, level_target, skills=skills,
                              weapon=weapon, **kwargs)
    path = None
    if save:
        path = os.path.join(
            _cache_dir(), "calculator",
            f"{_as_int(avatar_id)}_{_as_int(level_current)}_to_{_as_int(level_target)}.json",
        )
    # ⚠️⚠️ **不要包 `{"data": {...}}` 外壳**（整条链路最关键的一处，2026-09 实测确认）。
    #   米游社计算器前端是这样调的：`batchCost({data: a, headers: {...}})`
    #   —— 那个 `{data, headers}` 是 **axios 的 config**，不是 HTTP body；
    #   真正发出去的 body 就是 `a` 本身（**平的**）。
    #   包了外壳会得到 `retcode 0 + 三个空桶`：不报错，但一个材料都算不出来。
    batch_body = {"items": [body], "lang": DEFAULT_LANG,
                  "region": params["region"], "uid": params["uid"]}
    data = None
    try:
        payload = _request_json("POST", PATH_BATCH_COMPUTE, query=query, body_obj=batch_body,
                                cookie=cookie, timeout=timeout, snapshot_path=path, wrap=False)
        items = (payload or {}).get("items") or []
        if items:
            # `available_material` / `overall_consume` 在**外层**，一起带上给解析用
            data = dict(items[0])
            # v3 的字段语义和旧 compute 不同：
            # num = 本次还要消耗/缺口，lack_num = 页面显示的总所需。
            # 解析器用这个标记区分两种响应，避免把两列显示成同一个数。
            data["available_material"] = payload.get("available_material") or []
            data["overall_consume"] = payload.get("overall_consume") or []
    except mys_api.MysError:
        data = None

    if data is None:
        # 退回 v2（没有 available_material，缺口只能按总需求算）
        data = _request_json("POST", PATH_COMPUTE, body_obj=body, cookie=cookie,
                             timeout=timeout, snapshot_path=path, wrap=False)

    requirements, by_phase = parse_compute(data)
    return {
        "avatar_id": _as_int(avatar_id),
        "query": params,
        "body": body,
        "requirements": requirements,
        "by_phase": by_phase,
        "available": parse_available_material(data),
        "raw": data,
        # 🚨 `retcode 0 + 三个 consume 桶全是空` ≈ "这次什么都没算出来"。
        #    参数不对时米游社**不报错**，就这么返回空数组 —— 所以这里必须显式标记，
        #    否则上层会把"没算出来"当成"材料都齐了"，直接跳过这个角色。
        "empty": not requirements,
    }

    requirements, by_phase = parse_compute(data)
    return {
        "avatar_id": _as_int(avatar_id),
        "query": params,
        "body": body,
        "requirements": requirements,
        "by_phase": by_phase,
        "raw": data,
        # 🚨 `retcode 0 + 三个 consume 桶全是空` ≈ "这次什么都没算出来"。
        # 未登录时米游社**不报错**，就这么返回空数组 —— 所以这里必须显式标记，
        # 否则上层会把"没算出来"当成"材料都齐了"，直接跳过这个角色。
        "empty": not requirements,
    }


def parse_compute(data):
    """把 `compute` 的响应解析成 `(requirements, by_phase)`。

    **递归 + 关键字**而不是写死路径：米游社的桶名改过一次（camelCase → snake_case），
    写死路径的实现会静默返回空材料 —— 而"静默返回空材料"会直接导致
    "什么都缺 0 个"的假完成。所以这里宁可多扫几层。
    """
    rows = {}
    # ⚠️ 这几个键**形状和材料记录一样**（`{id, name, num}`），但语义完全不同：
    #   · `available_material` = 你**已有**的数量（不是需求！混进来会凭空多出一堆材料）；
    #   · `overall_consume` / `overall_material_consume` = 各桶的合计（混进来会翻倍）；
    #   · `single_role_result` = 每个角色的汇总。
    # 所以扫的时候必须跳过它们。
    _SKIP = {"availablematerial", "overallconsume", "overallmaterialconsume",
             "singleroleresult"}

    def visit(node, trail=()):
        if isinstance(node, dict):
            for key, value in node.items():
                # 键名归一化后进 trail：这样阶段判定对 camelCase / snake_case 都成立
                marker = _normalise_key(key)
                if marker in _SKIP:
                    continue
                phase = _phase_from_trail(trail + (marker,))
                if _looks_like_item(value):
                    for raw in value:
                        _merge(rows, raw, phase)
                    continue
                visit(value, trail + (marker,))
        elif isinstance(node, list):
            phase = _phase_from_trail(trail)
            for item in node:
                if _looks_like_item_records(item):
                    _merge(rows, item, phase)
                else:
                    visit(item, trail)

    visit(data)

    # ★ 缺口（还差）有两个来源，按可信度取：
    #   ① 米游社自己算的 `lack_num`（它认得背包上下文时才有意义）；
    #   ② `/v3/batch_compute` 的 `available_material`（**你已有多少**）→ 还差 = 所需 - 已有。
    #   ⚠️ 两个都没有时**不能**当成"你都不缺"：退回"按总需求算"，
    #   并让 `owned=None`（界面显示"未知"），否则会出现"材料已齐"的假完成。
    lack_known = any(
        int(bucket_row.get("lack") or 0) > 0
        for phase_rows in rows.values() for bucket_row in phase_rows.values()
    )
    available = parse_available_material(data)
    overall_lack = _parse_overall_lack(data)
    phase_lack = _allocate_phase_lack(rows, overall_lack)

    requirements = {}
    by_phase = {phase: [] for phase in growth_models.PHASES}
    for phase in growth_models.PHASES:
        for item_id, row in sorted(rows.get(phase, {}).items()):
            required = int(row["required"])
            if required <= 0:
                continue
            if item_id in phase_lack:
                lack = min(required, max(0, int(phase_lack[item_id].get(phase, 0))))
                owned = max(0, required - lack)
            else:
                owned, lack = _owned_and_lack(item_id, required, row.get("lack"), lack_known,
                                              available)
            entry = {
                "item_id": item_id,
                "item_name": row["name"],
                "name": row["name"],
                "category": growth_models.classify_item(name=row["name"], item_id=item_id),
                "required": required,
                "lack": lack,
                "owned": owned,                 # None = 不知道（拿不到背包数据）
                "lack_source": ("overall_consume" if item_id in overall_lack else "item_lack_num"),
                "phases": [phase],
                "icon": row["icon"],
            }
            by_phase[phase].append(entry)

            flat = requirements.setdefault(item_id, {
                "item_id": item_id,
                "item_name": row["name"],
                "name": row["name"],
                "category": growth_models.classify_item(name=row["name"], item_id=item_id),
                "required": 0,
                "lack": 0 if owned is not None else None,
                "owned": 0 if owned is not None else None,
                "lack_source": entry["lack_source"],
                "phases": [],
                "icon": row["icon"],
            })
            flat["required"] += required
            if owned is not None:
                # 同一材料跨阶段：需求相加、缺口相加、已有的按总量重算（不能把它加两次）
                flat["lack"] = int(flat.get("lack") or 0) + int(lack or 0)
                flat["owned"] = max(0, flat["required"] - flat["lack"])
                if entry["lack_source"] == "overall_consume":
                    flat["lack_source"] = "overall_consume"
            flat["phases"].append(phase)

    ordered = []
    for item_id in sorted(requirements):
        entry = requirements[item_id]
        entry["phases"] = sorted(entry["phases"],
                                 key=lambda phase: growth_models.PHASES.index(phase))
        ordered.append(entry)
    return ordered, by_phase


def _parse_overall_lack(data):
    """Read the account-wide missing counts from v3 overall_consume."""
    result = {}
    if not isinstance(data, dict):
        return result
    records = data.get("overall_consume") or []
    if isinstance(records, dict):
        records = records.get("items") or records.get("list") or []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        item_id = _as_int(_first(raw, _ITEM_ID_KEYS))
        if not item_id:
            continue
        keys = {_normalise_key(key) for key in raw}
        if any(_normalise_key(key) in keys for key in _ITEM_LACK_KEYS):
            result[item_id] = max(0, _as_int(_first(raw, _ITEM_LACK_KEYS)))
    return result


def _allocate_phase_lack(rows, overall_lack):
    """Allocate one account-wide shortage across phases without double counting."""
    allocations = {}
    for item_id, lack_total in (overall_lack or {}).items():
        remaining = max(0, int(lack_total or 0))
        per_phase = {}
        for phase in growth_models.PHASES:
            row = rows.get(phase, {}).get(item_id)
            required = int((row or {}).get("required") or 0)
            if required <= 0:
                continue
            per_phase[phase] = min(required, remaining)
            remaining -= per_phase[phase]
        allocations[item_id] = per_phase
    return allocations


def _owned_and_lack(item_id, required, api_lack, lack_known, available):
    """算出某个材料的 `(已有, 还差)`；两个都算不出来时返回 `(None, None)`。

    优先级：米游社的 `lack_num` > `available_material`（已有数量）> 都不知道。
    "都不知道"**不等于**"已有 0" —— 那是"背包数据拿不到"，上层要显示"未知"。
    """
    if lack_known:
        lack = max(0, _as_int(api_lack))
        return max(0, required - lack), lack
    if item_id in available:
        owned = min(required, max(0, _as_int(available[item_id])))
        return owned, required - owned
    if _as_int(api_lack) > 0:
        lack = _as_int(api_lack)
        return max(0, required - lack), lack
    return None, None


def _looks_like_item(value):
    """`[{item_id, num}, ...]` 这种"材料数组"。"""
    if not isinstance(value, list) or not value:
        return False
    return _looks_like_item_records(value[0])


def _looks_like_item_records(value):
    """是不是一条"材料记录"（同时有 id 和数量字段）。键名大小写/下划线不敏感。"""
    if not isinstance(value, dict):
        return False
    keys = {_normalise_key(key) for key in value}
    has_id = any(_normalise_key(name) in keys for name in _ITEM_ID_KEYS)
    has_count = any(_normalise_key(name) in keys for name in _ITEM_COUNT_KEYS)
    return has_id and has_count


def _phase_from_trail(trail):
    """路径上的键名 → 阶段。认不出来时归到"角色等级"（compute 的主用途就是拉角色）。"""
    joined = " ".join(trail)
    for phase in _PHASE_ORDER:
        if phase == growth_models.PHASE_CHARACTER_LEVEL:
            continue
        keywords = dict(_PHASE_KEYS)[phase]
        if any(keyword in joined for keyword in keywords):
            return phase
    return growth_models.PHASE_CHARACTER_LEVEL

def _merge(rows, raw, phase):
    """把一条材料记录并进结果表：**按 (阶段, 材料) 分开存**。

    为什么不按键合并：米游社把三个桶的消耗分得很清楚（`avatar_consume` 是升级/突破、
    `avatar_skill_consume` 是天赋、`weapon_consume` 是武器），同一个摩拉在三个桶里是
    **三笔不同的花费**。合并成一个数就只能取最大或相加，两种都是错的
    （取最大 → 三个阶段显示同一个离谱的数；相加 → 无法还原到阶段）。
    同一个材料在**同一个桶里**出现多次时才取最大值（那是真的重复上报）。
    """
    item_id = _as_int(_first(raw, _ITEM_ID_KEYS))
    if not item_id:
        return
    count = _as_int(_first(raw, _ITEM_COUNT_KEYS))
    lack_count = _as_int(_first(raw, _ITEM_LACK_KEYS))
    bucket = rows.setdefault(phase, {})
    row = bucket.setdefault(item_id, {
        "name": str(_first(raw, _ITEM_NAME_KEYS) or ""),
        "required": 0,
        "lack": 0,
        "lack_present": False,
        "icon": str(_first(raw, _ITEM_ICON_KEYS) or ""),
    })
    if not row["name"]:
        row["name"] = str(_first(raw, _ITEM_NAME_KEYS) or "")
    row["required"] = max(row["required"], count)
    row["lack"] = max(int(row.get("lack") or 0), lack_count)
    row["lack_present"] = row.get("lack_present") or any(
        _normalise_key(key) in {_normalise_key(name) for name in _ITEM_LACK_KEYS}
        for key in raw
    )


# ==========================================
# 🌟 读背包真实库存
# ==========================================


def fetch_my_items(uid=None, server=None, cookie=None, timeout=20, lang=DEFAULT_LANG, save=True,
                   avatar_ids=None):
    """~~读"我的角色"（含背包材料）~~ —— **这条路已经被米游社关掉了，别再用来当数据源。**

    ⚠️ 实测（2026-09）：`POST /v1/sync/avatar/list` 无论怎么发都是
    `-100 请先登录后参与活动`（不带 cookie）/ `10041 网络出小差了`：
    cookie 换成纯 v1 / 纯 v2 / 两者都带、带 `x-rpc-ltoken` 头、把 `uid`/`region`
    放进 query 或 body、`avatar_ids` 用数字或字符串、只传一个 id……**全都一样**。
    同一个 cookie 打 `/v1/sync/avatar/detail` 却是好的（能读到角色真实等级），
    所以不是登录态的问题，是这个"列表 + 背包"接口本身不再对普通调用方开放。

    现在要数据请用：
      · `fetch_avatar_state(avatar_id)` → 单个角色的真实状态（还能判"有没有这个角色"）；
      · `fetch_avatar_plan(avatar_id)`  → 养成方案 + 材料需求（权威需求来源）。

    保留这个函数只是为了让 `--probe` 还能把它当对照打一下，**不返回可用数据**。
    """
    if not mys_api.cookie_configured():
        # 「没配 cookie」这个结论与接口死活无关，先如实报它（上层靠这个类型做"功能整体关闭"）
        raise mys_api.MysNotConfigured("未配置 MYS_COOKIE")
    raise mys_api.MysApiError(
        -100,
        "米游社不向程序开放「我的角色 / 背包」接口（/v1/sync/avatar/list，"
        "带任何 cookie 都回 -100）。",
    )


def fetch_catalog_page(page=1, uid=None, server=None, cookie=None, timeout=20,
                       lang=DEFAULT_LANG, save=False):
    """取图鉴的**第 N 页**（每页 10 条）。

    ⚠️ 翻页参数在 **query** 里（`?page=2`）—— 实测：body 里的
    `page`/`size`/`offset` 全都被当没看见（永远回第一页），只有 query 的 `page` 生效。
    """
    params = dict(_base_query(uid=uid, server=server, lang=lang))
    query = _encode_query(params)
    if int(page or 1) > 1:
        query = f"{query}&page={int(page)}"
    path = None
    if save:
        path = os.path.join(_cache_dir(), "calculator", f"catalog_{int(page)}.json")
    return _request_json(
        "POST", PATH_AVATAR_LIST, query=query,
        body_obj={"uid": str(uid or "").strip(),
                  "region": str(server or "").strip() or mys_api.server_from_uid(uid),
                  "lang": str(lang or DEFAULT_LANG)},
        cookie=cookie, timeout=timeout, snapshot_path=path,
    )


def catalog_path():
    """整份图鉴缓存的落盘位置（Studio 的"添加培养目标"列表读它）。"""
    return os.path.join(_cache_dir(), "calculator", "catalog_all.json")


def fetch_catalog_all(pages=0, uid=None, server=None, cookie=None, timeout=20,
                      lang=DEFAULT_LANG, max_age_hours=168):
    """把图鉴**翻完**，返回 `{角色id: 档案}`（带缓存，默认 7 天）。

    为什么要翻完：图鉴一次只给 10 条（`total` 118~146），而"添加培养目标"需要一个
    **完整的候选列表**；只取第一页的话，玩家想练的角色多半不在里面（"一个都没有"就是这么来的）。
    图鉴是公开数据（不需要账号），翻一次缓存一周，不折腾风控。
    """
    import json as _json
    import time as _time

    target = catalog_path()
    if max_age_hours and os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8") as handle:
                cached = _json.load(handle)
            age_hours = (_time.time() - float(cached.get("fetched_ts") or 0)) / 3600
            if cached.get("avatars") and age_hours < float(max_age_hours):
                return {int(key): value for key, value in cached["avatars"].items()}
        except (OSError, ValueError, TypeError):
            pass                      # 缓存坏了就重翻，不报错

    collected = {}
    limit = int(pages) if int(pages or 0) > 0 else 30      # 30 页 = 300 条，覆盖全图鉴绰绰有余
    for page in range(1, limit + 1):
        try:
            payload = fetch_catalog_page(page, uid=uid, server=server, cookie=cookie,
                                        timeout=timeout, lang=lang)
        except mys_api.MysError:
            if page == 1:
                raise
            break                      # 翻到一半失败就用手上的，别把整件事搞崩
        batch = parse_avatars(payload)
        if not batch:
            break
        fresh = len([1 for avatar in batch if int(avatar["id"]) not in collected])
        for avatar in batch:
            collected.setdefault(int(avatar["id"]), avatar)
        if fresh == 0:
            break                      # 这页没有新角色 = 翻到头了

    if collected:
        _dump_catalog(collected)
    return collected


def _dump_catalog(avatars):
    import json as _json
    import time as _time

    target = catalog_path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            _json.dump({
                "fetched_ts": _time.time(),
                "fetched_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "avatars": {str(key): value for key, value in avatars.items()},
            }, handle, ensure_ascii=False)
        os.replace(tmp, target)
    except OSError as exc:
        print(f"⚠️ 图鉴缓存写入失败（不影响本次）：{exc}")


def _catalog_avatar_ids(uid=None, server=None, cookie=None, timeout=20):
    """图鉴里的角色 id 列表（兜底用；要全量请用 `fetch_catalog_all()`）。"""
    try:
        catalog = fetch_avatar_list(uid=uid, server=server, cookie=cookie,
                                    timeout=timeout, save=False)
    except mys_api.MysError:
        return []
    return [avatar["id"] for avatar in parse_avatars(catalog)]


# ==========================================
# 🌟 单个角色的真实状态（`/v1/sync/avatar/detail`）
# ==========================================
#
# 为什么用它：角色的**列表**接口（`/v1/sync/avatar/list`）已经被米游社关掉了
# （带 cookie 也一律 `-100 请先登录后参与活动`），但**单个**角色的这条还在，
# 而且信息更全：等级、突破、三个天赋、武器、五个圣遗物。
#
# 它同时是**唯一可靠的"这号有没有这个角色"**判据：没有的角色返回
# `retcode -1` + `code:[-1002] Msg:[伙伴不存在~]`（实测）。所以整号扫描就是
# 拿图鉴 id 一个个问 —— 请求多，必须缓存（见 `scan_owned_avatars`）。


def fetch_avatar_state(avatar_id, uid=None, server=None, cookie=None, timeout=20,
                       lang=DEFAULT_LANG):
    """问一个角色的真实养成状态。返回 `parse_avatar_state()` 的结果。

    **不抛"没有这个角色"的异常**：那是正常结果（`owned=False`），调用方要据此过滤。
    其它错误（网络 / 风控 / cookie）照旧抛 `MysError`。
    """
    params = dict(_base_query(uid=uid, server=server, lang=lang))
    params["avatar_id"] = _as_int(avatar_id)
    query = _encode_query(params)
    try:
        data = _request_json("GET", PATH_AVATAR_STATE, query=query, cookie=cookie,
                             timeout=timeout)
    except mys_api.MysApiError as exc:
        if exc.retcode == -1 and _NOT_OWNED_MARK in str(exc):
            return {"avatar_id": _as_int(avatar_id), "owned": False, "name": "",
                    "level": 0, "promote_level": 0, "talents": [], "weapon": {},
                    "reliquary_list": [], "raw": {}}
        raise
    return parse_avatar_state(data, avatar_id=avatar_id)


def parse_avatar_state(data, avatar_id=0):
    """把 `sync/avatar/detail` 的响应整理成内部形状。"""
    detail = data if isinstance(data, dict) else {}
    # Unwrap the extra envelope used by some versions of the endpoint.
    for _ in range(3):
        nested = None
        for key in ("data", "detail", "avatar_detail"):
            candidate = detail.get(key) if isinstance(detail, dict) else None
            if isinstance(candidate, dict) and (
                "avatar" in candidate
                or "skill_list" in candidate
                or "level" in candidate
                or "level_current" in candidate
                or "data" in candidate
                or "detail" in candidate
            ):
                nested = candidate
                break
        if nested is None:
            break
        detail = nested
    avatar = detail.get("avatar") or {}
    if not isinstance(avatar, dict):
        avatar = {}
    raw_skills = (
        detail.get("skill_list")
        or detail.get("skills")
        or avatar.get("skill_list")
        or avatar.get("skills")
        or ()
    )
    talents = []
    for raw in raw_skills:
        if not isinstance(raw, dict):
            continue
        talents.append({
            "id": _as_int(raw.get("id") or raw.get("skill_id")),
            "name": str(raw.get("name") or ""),
            "pos_name": str(raw.get("pos_name") or ""),
            "group_id": _as_int(raw.get("group_id") or raw.get("groupId")),
            "level": _as_int(
                raw.get("level_current")
                or raw.get("level")
                or raw.get("current_level")
            ),
            "max_level": _as_int(raw.get("max_level") or raw.get("maxLevel")),
        })
    weapon = (
        detail.get("weapon")
        or avatar.get("weapon")
        or detail.get("weapon_info")
        or {}
    )
    if not isinstance(weapon, dict):
        weapon = {}
    return {
        "avatar_id": _as_int(avatar.get("id") or avatar_id),
        "owned": True,
        "name": str(avatar.get("name") or ""),
        "rarity": _as_int(avatar.get("avatar_level")),
        "element_attr_id": _as_int(avatar.get("element_attr_id")),
        "level": _as_int(
            detail.get("level")
            or detail.get("level_current")
            or detail.get("avatar_level_current")
            or avatar.get("level_current")
            or avatar.get("level")
        ),
        "promote_level": _as_int(
            detail.get("promote_level")
            or detail.get("promoteLevel")
            or avatar.get("promote_level")
        ),
        "max_level": _as_int(avatar.get("max_level")) or 90,
        "talents": talents,
        "weapon": dict(weapon),
        "reliquary_list": list(
            detail.get("reliquary_list")
            or detail.get("reliquaries")
            or avatar.get("reliquary_list")
            or ()
        ),
        "raw": data if isinstance(data, dict) else {},
    }


# ==========================================
# 🌟 养成方案 + 材料需求（`/v1/avatar_cultivation/detail`）
# ==========================================
#
# 这是**材料需求的权威来源**（老的 `/v2/compute` 现在只会返回三个空桶，实测点了
# 十几种请求体都是一样的空 —— 那个计算接口不动了）。
#
# 它的语义很好用：给一个 `avatar_id`，米游社按**你账号里这个角色的真实状态**给出
# "还差什么"：
#   · `current_level / target_level`：养成配置里的等级目标；
#   · `skill_upgrade_list`：三个天赋的当前等级 → 目标等级（当前值就是账号里的真实值）；
#   · `skill_consume_materials`：**全部所需材料**（摩拉 / 经验书 / 天赋书 / 周本材料…），
#     每条还带 `lack_num`（缺口）。
#
# ⚠️ 两点如实说明：
#   · 材料是**合并的一份**（等级 + 天赋 + 武器都混在一起），没有分阶段。
#     我们把它统一挂在「角色等级」这一阶段下，并带上 `phase_note` 说清楚 ——
#     **不猜分配比例**（猜一个错的阶段划分比"粗一点"更糟）。
#   · `lack_num` 目前实测**恒为 0**（两个号都一样），说明"背包缺口"那套没生效 ——
#     不要把它当"材料都够了"。真要用得等米游社那边的背包读取授权开了再说。


def fetch_avatar_plan(avatar_id, uid=None, server=None, cookie=None, timeout=20,
                      lang=DEFAULT_LANG, save=False):
    """拉一个角色的养成方案（目标 + 材料需求）。返回 `parse_avatar_plan()` 的结果。"""
    params = dict(_base_query(uid=uid, server=server, lang=lang))
    params["avatar_id"] = _as_int(avatar_id)
    query = _encode_query(params)
    path = None
    if save:
        path = os.path.join(_cache_dir(), "calculator", f"plan_{_as_int(avatar_id)}.json")
    data = _request_json("GET", PATH_AVATAR_PLAN, query=query, cookie=cookie,
                         timeout=timeout, snapshot_path=path)
    return parse_avatar_plan(data, avatar_id=avatar_id)


def parse_avatar_plan(data, avatar_id=0):
    """把 `avatar_cultivation/detail` 的响应整理成内部形状。

    顺带修一个米游社自己的字段名笔误：缺口列表叫 `lack_materails`（少个 i），
    这里两种拼法都认。
    """
    detail = (data or {}).get("detail") if isinstance(data, dict) else {}
    detail = detail if isinstance(detail, dict) else {}
    avatar = detail.get("avatar") or {}

    skills = []
    for raw in detail.get("skill_upgrade_list") or ():
        if not isinstance(raw, dict):
            continue
        skill = raw.get("skill") or {}
        skills.append({
            "id": _as_int(skill.get("id")),
            "group_id": _as_int(skill.get("group_id")),
            "name": str(skill.get("name") or ""),
            "pos_name": str(skill.get("pos_name") or ""),
            "icon": str(skill.get("icon") or ""),
            "current_level": _as_int(raw.get("current_level")),
            "target_level": _as_int(raw.get("target_level")),
            "priority": _as_int(raw.get("priority")),
        })
    skills.sort(key=lambda item: item["priority"] or 99)

    materials = []
    for raw in detail.get("skill_consume_materials") or ():
        if not isinstance(raw, dict):
            continue
        item_id = _as_int(raw.get("id"))
        required = _as_int(raw.get("num"))
        if not item_id or required <= 0:
            continue
        materials.append({
            "item_id": item_id,
            "item_name": str(raw.get("name") or ""),
            "name": str(raw.get("name") or ""),
            "required": required,
            "lack": _as_int(raw.get("lack_num")),
            "rarity": _as_int(raw.get("level")),
            "icon": str(raw.get("icon") or raw.get("icon_url") or ""),
            "wiki_url": str(raw.get("wiki_url") or ""),
        })
    materials.sort(key=lambda item: (-item["required"], item["item_id"]))

    return {
        "avatar_id": _as_int(avatar.get("id") or avatar_id),
        "character_name": str(avatar.get("name") or ""),
        "rarity": _as_int(avatar.get("avatar_level")),
        "element_attr_id": _as_int(avatar.get("element_attr_id")),
        "weapon_cat_id": _as_int(avatar.get("weapon_cat_id")),
        "current_level": _as_int(detail.get("current_level")),
        "target_level": _as_int(detail.get("target_level")),
        "skills": skills,
        "materials": materials,
        "is_focus_avatar": bool(detail.get("is_focus_avatar")),
        "is_finish_skill_upgrade": bool(detail.get("is_finish_skill_upgrade")),
        "weapon_recommend": list(detail.get("weapon_recommend") or ()),
        "lineup_recommend": list(detail.get("lineup_recommend") or ()),
        "raw": detail,
    }


def plan_requirements(plan):
    """养成方案 → `(requirements, by_phase)`，形状与 `parse_compute()` 一致。

    这些材料挂在**天赋**阶段下 —— 不是猜的：接口里那份列表就叫
    `skill_consume_materials`（技能升级消耗），同一份响应里的 `skill_upgrade_list`
    也是三个天赋的"当前等级 → 目标等级"。
    ⚠️ 也就是说它**只覆盖天赋**：角色的等级突破材料 / 经验书，这个接口不给
    （实测：把 `level_current` / `level_target` / `target_level` 传进去，
    材料一分不变）。别把它当成"这个角色的全部需求"。
    """
    phase = growth_models.PHASE_TALENT
    requirements = []
    by_phase = {name: [] for name in growth_models.PHASES}
    for material in (plan or {}).get("materials") or ():
        entry = {
            "item_id": int(material["item_id"]),
            "item_name": material["item_name"],
            "name": material["item_name"],
            "category": growth_models.classify_item(name=material["item_name"],
                                                   item_id=material["item_id"]),
            "required": int(material["required"]),
            "phases": [phase],
            "icon": material.get("icon") or "",
            "lack": int(material.get("lack") or 0),
            "source": "mys_plan",
        }
        requirements.append(entry)
        by_phase[phase].append(entry)
    return requirements, by_phase



def extract_item_list(payload):
    """从任意响应里找出"材料数组"（背包响应、compute 响应都适用）。"""
    found = []
    wanted = {_normalise_key(key) for key in _ITEM_LIST_KEYS}

    def visit(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if _normalise_key(key) in wanted and _looks_like_item(value):
                    found.extend(value)
                    continue
                visit(value, depth + 1)
        elif isinstance(node, list):
            if _looks_like_item(node):
                found.extend(node)
            else:
                for item in node:
                    visit(item, depth + 1)

    visit(payload)
    return found


def inventory_from_calculator(uid=None, server=None, **kwargs):
    """读背包并转成 `mys_inventory` 认得的原始条目列表。"""
    payload = fetch_my_items(uid=uid, server=server, **kwargs)
    return extract_item_list(payload)


# ==========================================
# 🌟 需求 → 阶段映射的小工具
# ==========================================


def avatar_from_state(state):
    """把 `fetch_avatar_state()` 的结果转成"角色档案"形状（给 `compute_for_target` 用）。

    为什么要转：算材料那条路要的是**账号里的真实状态** + 天赋的 `group_id`；
    以前这些来自"/v1/sync/avatar/list"，那条废了之后就没有档案可用了
    （战绩接口也被风控卡着）。`fetch_avatar_state()` 给的信息其实更全，
    所以在这里转一次，后面所有逻辑（`skill_map_for_targets` 等）原样复用。

    ⚠️ 天赋 id 用的是 **`group_id`**（米游社算材料认的是它，不是天赋自己的 `id`）。
    """
    state = state or {}
    talents = list(state.get("talents") or ())
    raw_skills = [{
        "id": _as_int(talent.get("group_id")),
        "group_id": _as_int(talent.get("group_id")),
        "name": str(talent.get("name") or ""),
        "pos_name": str(talent.get("pos_name") or ""),
        "max_level": _as_int(talent.get("max_level")) or 10,
        "level_current": _as_int(talent.get("level")),
    } for talent in talents]
    return {
        "id": _as_int(state.get("avatar_id")),
        "name": str(state.get("name") or ""),
        "level": _as_int(state.get("level")),
        "promote_level": _as_int(state.get("promote_level")),
        "rarity": _as_int(state.get("rarity")),
        "element_attr_id": _as_int(state.get("element_attr_id")),
        "weapon": dict(state.get("weapon") or {}),
        # `skills` 的键是天赋 id、值是当前等级（旧档案形状）
        "skills": {str(_as_int(t.get("group_id"))): _as_int(t.get("level")) for t in talents},
        "_raw": {"skill_list": raw_skills},
        "_state": state,
    }


def skill_ids_from_avatar(avatar):
    """从计算器档案里取"战斗天赋"的 id 列表。

    优先用米游社自己给的槽位（`pos_name` = 普通攻击 / 元素战技 / 元素爆发）**按槽位排序** ——
    这比"按技能 id 升序"可靠得多（旅行者这类多形态角色的 id 顺序并不对应槽位）。
    没有槽位信息时退回 id 升序（游戏内技能 id 通常是按 普攻 → 战技 → 爆发 分配的）。
    """
    raw = (avatar or {}).get("_raw")
    slots = talents_by_slot(raw) if raw else {}
    if all(slot in slots for slot in ("normal", "skill", "burst")):
        return [str(slots[slot]["id"]) for slot in ("normal", "skill", "burst")]

    skills = (avatar or {}).get("skills") or {}
    ordered = sorted(skills, key=lambda item: _as_int(item, 10 ** 9))
    return [str(item) for item in ordered]


def skill_map_for_targets(avatar, target):
    """把"三项目标等级"落到具体技能 id 上：`{skill_id: {"current": 8, "target": 10}}`。

    技能 id 与槽位的对应关系**由米游社自己给出**（`pos_name`：
    普通攻击 → `normal_target`、元素战技 → `skill_target`、元素爆发 → `burst_target`），
    拿不到槽位信息时才退回"第 1/2/3 个技能 id"。
    """
    slots = talents_by_slot((avatar or {}).get("_raw")) if (avatar or {}).get("_raw") else {}
    slot_priority = ("normal", "skill", "burst")
    targets = {
        "normal": growth_models.clamp_talent((target or {}).get("normal_target"), 10),
        "skill": growth_models.clamp_talent((target or {}).get("skill_target"), 10),
        "burst": growth_models.clamp_talent((target or {}).get("burst_target"), 10),
    }
    skills = (avatar or {}).get("skills") or {}
    result = {}

    if all(slot in slots for slot in slot_priority):
        for slot in slot_priority:
            skill_id = str(slots[slot]["id"])
            result[skill_id] = {
                "current": _as_int(skills.get(skill_id)),
                "target": targets[slot],
            }
        return result

    ids = skill_ids_from_avatar(avatar)
    for index, skill_id in enumerate(ids[:3]):
        slot = slot_priority[index] if index < len(slot_priority) else "burst"
        result[str(skill_id)] = {
            "current": _as_int(skills.get(skill_id)),
            "target": targets[slot],
        }
    return result


def compute_for_target(avatar, target, uid=None, server=None, **kwargs):
    """按一条角色养成目标问米游社算材料 —— 养成系统每一步都走这里。

    `avatar` 是 `fetch_avatar_list` → `parse_avatars` 里的一条，**或者**
    `fetch_avatar_state()` 的返回（后者更好：它带账号里的真实等级、突破等级、
    天赋 `group_id` 与武器，正好是米游社要的字段）。
    `target` 是 `growth_targets` 里的一行。武器阶段只在该目标启用时才发给米游社
    （不然米游社也会把"武器 1→90"的材料算进来，多出一堆根本不需要的缺口）。

    ⚠️ 起始等级必须来自**账号角色**（`/v1/sync/avatar/detail`）。全量图鉴的
    `avatar_level` 是稀有度（五星 = 5），拿它当"当前等级"会让米游社算出
    "从 5 级练到 90 级"的一整套材料 —— 数字看着正常，但全是错的。
    """
    avatar = avatar or {}
    target = target or {}
    weapon = dict(avatar.get("weapon") or {})
    # 武器阶段：玩家手动指定了武器就用指定武器；没有指定时，用账号详情里的当前装备武器。
    # 批量目标默认只说"武器到 90"，不会给每个角色填 weapon_id；这时如果忽略当前装备，
    # 只差武器等级的角色会被误判成没有材料缺口。
    weapon_id = int(target.get("weapon_id") or 0)
    if target.get("weapon_enabled") and (weapon_id or weapon.get("id")):
        if weapon_id:
            weapon["id"] = weapon_id
        weapon["level_target"] = growth_models.clamp_level(target.get("weapon_level_target"), 90)
    else:
        weapon = {}

    result = compute_materials(
        avatar.get("id") or avatar.get("avatar_id"),
        avatar.get("level") or 1,
        growth_models.clamp_level(target.get("level_target"), 90),
        skills=skill_map_for_targets(avatar, target),
        weapon=weapon,
        uid=uid,
        server=server,
        # 突破等级 / 元素 / `from_user_sync` 都是米游社前端会带的字段
        # （不带你也能算，但带上更贴近"网页那次算得出材料"的请求）
        promote_level=avatar.get("promote_level") or 0,
        element_attr_id=avatar.get("element_attr_id") or 0,
        **kwargs,
    )
    if result.get("empty"):
        # 米游社在"参数不完整"时**不报错**，只返回空的 consume 桶。
        # 不显式说出来，上层就会把它当成"材料都齐了"（假完成）—— 那是最坏的一种错。
        print(
            "⚠️ 养成计算器返回了空材料清单（retcode 0 但三个 consume 桶都是空的）。\n"
            "   自检：python -m skills.mys_calculator --check"
        )
    return result


# ==========================================
# 🌟 小工具
# ==========================================


def _first(raw, names, default=None):
    """按别名取值，**键名的大小写和下划线都不敏感**（`itemId` / `item_id` / `ItemID` 都认）。

    米游社的字段名换过一次命名风格（camelCase ↔ snake_case），把两种都认下来，
    接口小改版就不用改解析代码 —— 否则表现是"同步成功但材料是 0 个"。
    """
    if not isinstance(raw, dict):
        return default
    wanted = {_normalise_key(name) for name in names}
    for key, value in raw.items():
        if _normalise_key(key) in wanted and value not in (None, ""):
            return value
    return default


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return int(default)


# ==========================================
# 🌟 端点探针：python -m skills.mys_calculator --probe
# ==========================================


def probe_endpoints(uid=None, hosts=None, cookie=None, timeout=15):
    """把候选端点挨个试一遍，返回 `[{host, path, method, status, retcode, ...}]`。

    **这是"接口到底在哪条路径上"的实测手段** —— 不猜、不靠文档，直接用玩家自己的 cookie 问。
    判读方式：
      · `404` + 正文 `404 page not found` → 这条路径不存在（继续试下一条）；
      · `retcode != 0`（-100/10001…）     → 路径是对的，但 cookie/风控有问题；
      · `retcode == 0` 且有 data          → **就是这条**。
    """
    hosts = hosts or [CALC_HOST]
    server = mys_api.server_from_uid(uid) if uid else ""
    results = []
    for host in hosts:
        for get_path, post_path, label in ENDPOINT_CANDIDATES:
            query = _encode_query(_base_query(uid=uid, server=server, lang=DEFAULT_LANG))
            # 单角色那两条 GET **必须带 avatar_id**，不带只会得到 -100 / system error，
            # 探针会因此误判成"路径不对"。这里补一个公认存在的角色（神里绫华）。
            if "detail" in str(get_path):
                query = f"{query}&avatar_id={_PROBE_AVATAR_ID}"
            for method, path, body in (
                ("GET", get_path, None),
                ("POST", post_path, _wrap_body({"avatar_id": 10000089,
                                                "avatar_level_current": 1,
                                                "avatar_level_target": 2,
                                                "skill_list": [], "weapon": {}})),
            ):
                if not path:
                    continue
                raw = raw_call(host, method, full_path(path), query=query, body_obj=body,
                               cookie=cookie, timeout=timeout)
                data = raw.get("data")
                empty_data = _probe_data_is_empty(path, data)
                results.append({
                    "host": host, "path": path, "method": method, "label": label,
                    "status": raw["status"], "retcode": raw["retcode"],
                    "message": raw["message"], "error": raw["error"],
                    "keys": sorted(data)[:12] if isinstance(data, dict) else [],
                    "empty": raw["status"] == 200 and raw["retcode"] == 0 and empty_data,
                    "preview": (raw.get("text") or "").strip()[:160],
                    "alive": raw["status"] == 200,
                    "useful": raw["status"] == 200 and raw["retcode"] == 0 and not empty_data,
                })
    return results


def _probe_data_is_empty(path, data):
    """这次的 data 算不算"什么都没给"。

    ⚠️ 两条**必须分开判**：算计材料的接口即使 `retcode 0`，也可能只是回一个
    带三个空 consume 桶的壳子（`/v2/compute` 现在就是这样）—— 用 `parse_compute()`
    按真实解析逻辑判断"有没有材料"，比看字典空不空准得多。
    其它接口只看"data 是不是空壳"。
    """
    if data in (None, {}, []):
        return True
    if "compute" in str(path):
        requirements, _ = parse_compute(data)
        return not requirements
    return False


def describe_probe(results):
    """把探针结果渲染成给人看的报告（哪条可用、下一步怎么做）。"""
    lines = ["🔎 端点探针结果（用你自己的 cookie 实测，不猜）", ""]
    for row in results:
        if row["status"] == 404:
            verdict = "❌ 路径不存在（404）"
        elif row["error"]:
            verdict = f"⚠️ {row['error']}"
        elif row["empty"]:
            verdict = "⚠️ 接口在、但**什么都没返回**（空的 data —— 这种最坑，别当它可用）"
        elif row["useful"]:
            verdict = f"✅ 可用（retcode=0，返回了 {'/'.join(row['keys'][:4])}）"
        elif row["retcode"]:
            verdict = f"⚠️ 路径存在，但接口拒绝：retcode={row['retcode']} {row['message']}"
        else:
            verdict = f"❔ HTTP {row['status']}"
        lines.append(f"{row['method']:4} {row['path']}  →  {verdict}")
        lines.append(f"     （{row['label']}｜host {row['host']}）")
        if row["preview"] and not row["useful"]:
            lines.append(f"     响应预览：{row['preview']}")
    lines.append("")

    working = [row for row in results if row["useful"]]
    if working:
        lines.append("✅ 有能用的路径，按下面这样固定下来（写进 .env，代码不用改）：")
        for row in working:
            lines.append(f"   {row['method']:4} {row['path']}   （{row['label']}）")
        lines.append("")
        lines.append("   把可用的那两条填进 .env：")
        get_row = next((r for r in working if r["method"] == "GET"), None)
        post_row = next((r for r in working if r["method"] == "POST"), None)
        if get_row:
            lines.append(f"   MYS_CALC_HOST={get_row['host']}")
            lines.append(f"   MYS_CALC_PATH_AVATAR_LIST={get_row['path']}")
        if post_row:
            lines.append(f"   MYS_CALC_PATH_COMPUTE={post_row['path']}")
        lines.append("")
        lines.append("   ↳ 没有 MYS_CALC_PATH_* 这两项的话，就把它们当作新配置项加到 .env，"
                     "然后重启 Studio（代码已经支持这两个键）。")
    else:
        lines.append("❌ 候选路径**全都没命中**（都是 404 / 拒绝）。这说明：")
        lines.append("   ① 如果全是 404：这条链路换了完全不同的命名空间。请在浏览器里打开")
        lines.append("      https://act.mihoyo.com/ys/event/calculator/index.html ，F12 →")
        lines.append("      Network → 筛 XHR，看它到底在请求哪个 URL，然后把路径发我；")
        lines.append("   ② 如果都是 retcode=-100/-111/10001：路径对、但 cookie 或风控有问题，")
        lines.append("      先跑 `python -m skills.mys_api --check`。")
    return "\n".join(lines)


# ==========================================
# 🌟 命令行自检：python -m skills.mys_calculator --check
# ==========================================


def _main(argv=None):
    """自检：把"角色列表 / 背包 / 一次真实 compute"三步都跑一遍并打印真实响应形状。

    排查"材料拉取不到"时先跑这个 —— 三步里哪一步先炸，问题就在那一层
    （cookie / 风控 / 接口路径 / 响应形状），比猜快得多。
    出现 `HTTP 404` 就改用 `--probe`（那是路径问题，不是 cookie 问题）。
    """
    import argparse

    parser = argparse.ArgumentParser(description="米游社养成计算器自检")
    parser.add_argument("--check", action="store_true", help="校验 cookie + 依次试三个接口")
    parser.add_argument("--probe", action="store_true",
                        help="把候选端点挨个试一遍，找出哪条路径真的存在（404 时用这个）")
    parser.add_argument("--catalog", action="store_true",
                        help="只读全量角色图鉴（不需要 cookie，用来看接口通不通 + 天赋 id）")
    parser.add_argument("--uid", default="", help="要查的 UID（默认用 .env 里的）")
    parser.add_argument("--avatar", default="", help="试算哪个角色（角色名或 id；默认第一个 5 星）")
    parser.add_argument("--level-target", type=int, default=90, help="试算的目标等级（默认 90）")
    parser.add_argument("--with-weapon", action="store_true",
                        help="自检时把武器也纳入试算（默认只算角色等级 + 天赋）")
    parser.add_argument("--host", default="", help="--probe 时额外要试的域名（可重复用逗号分隔）")
    args = parser.parse_args(argv)

    if not mys_api.cookie_configured():
        print("❌ 没有配置 MYS_COOKIE（在 .env 或 Studio 的「配置 → 米游社」里填）")
        return 1

    # ⚠️ 不传 uid 就走配置里的（`MYS_UID` → `DEFAULT_UID`）：单角色那两条 GET
    #    是**账号维度**的，uid 为空会一律回 `-100 / system error`，
    #    探针会把"参数不全"误报成"接口拒绝"。
    uid = str(args.uid or getattr(config, "MYS_UID", "")
              or getattr(config, "DEFAULT_UID", "") or "").strip()

    if args.catalog:
        # 图鉴接口**不需要 cookie**，最适合用来确认"这套接口现在通不通"。
        print(f"🔎 全量角色图鉴：{CALC_HOST}{full_path(PATH_AVATAR_LIST)}")
        try:
            payload = fetch_avatar_list(uid=uid, save=True)
        except mys_api.MysError as exc:
            print(f"❌ 失败（{type(exc).__name__}）：{exc}")
            return 1
        avatars = parse_avatars(payload)
        print(f"✅ 解析出 {len(avatars)} 个角色")
        for avatar in avatars[:6]:
            slots = talents_by_slot(avatar.get("_raw"))
            print(f"   · {avatar['name']}（id {avatar['id']}，★{avatar['rarity']}）"
                  f"天赋槽位：{ {slot: slots[slot]['id'] for slot in slots} }")
        return 0 if avatars else 1

    if args.probe:
        hosts = [CALC_HOST]
        for extra in str(args.host or "").split(","):
            extra = extra.strip().rstrip("/")
            if extra and extra not in hosts:
                hosts.append(extra)
        print(f"🔎 开始探针：{len(ENDPOINT_CANDIDATES)} 组候选 × {len(hosts)} 个域名")
        print(f"   查询参数：{_encode_query(_base_query(uid=uid))}")
        print("   （每一步都会真的发请求，风控严的号别连着跑很多次）\n")
        results = probe_endpoints(uid=uid, hosts=hosts)
        print(describe_probe(results))
        return 0 if any(row["useful"] for row in results) else 1

    print(f"🔎 计算器域：{CALC_HOST}{CALC_PREFIX}")
    print(f"🔎 查询参数：{_encode_query(_base_query(uid=uid))}")

    # 先做一件最省事、收益最大的事：检查 cookie 里有没有 ltoken。
    # 缺 ltoken 时**不需要发任何请求**就能断定"需要登录的接口一定会失败"，
    # 而且现象会伪装成"未登录 / 账号数据异常"，让玩家对着 cookie 干瞪眼。
    audit = mys_api.audit_cookie()
    if audit["ok"]:
        print("✅ cookie 检查：ltuid / ltoken 都在")
    else:
        print("⚠️ cookie 检查：不完整 ——")
        for line in audit["hint"].splitlines():
            print(f"   {line}")
        print("   （公共接口仍可能通过，所以下面继续试；但需要登录的那两步预计会失败。）")
    print()

    # ① 图鉴（公开接口，最适合判断"这套接口现在通不通"）
    try:
        catalog = fetch_avatar_list(uid=uid, save=False)
    except mys_api.MysError as exc:
        print(f"❌ 角色图鉴失败（{type(exc).__name__}）：{exc}")
        if "404" in str(exc) or "不是 JSON" in str(exc):
            print("   ↳ 这是**路径问题**（404 说明 cookie 与签名都对，只是这条路径不存在）。")
            print("     接着跑：python -m skills.mys_calculator --probe")
        else:
            print("   ↳ 先跑 `python -m skills.mys_api --check`。")
        return 1
    catalog_avatars = parse_avatars(catalog)
    print(f"✅ ① 角色图鉴：{len(catalog_avatars)} 个（公开接口，不需要 cookie）")

    # ② 单个角色的真实状态（这是"账号里几级 / 有没有这个角色"的唯一来源）
    probe_id = _as_int(args.avatar) or (catalog_avatars[0]["id"] if catalog_avatars else 0)
    try:
        state = fetch_avatar_state(probe_id, uid=uid, server=_base_query(uid=uid)["region"])
    except mys_api.MysError as exc:
        print(f"❌ ② 角色状态失败（{type(exc).__name__}）：{exc}")
        return 1
    if not state["owned"]:
        print(f"⚠️ ② 角色状态：这个号没有角色 {probe_id}（接口是好的，这是正常回答）")
    else:
        print(f"✅ ② 角色状态：{state['name']} Lv.{state['level']}"
              f"（突破 {state['promote_level']}）天赋 "
              f"{[t['level'] for t in state['talents']]}｜武器 "
              f"{(state['weapon'] or {}).get('name', '（没带）')}")

    # ③ 真算一次材料（整条链路的关键：请求体形状对不对比什么都重要）
    profile = avatar_from_state(state) if state["owned"] else (
        catalog_avatars[0] if catalog_avatars else {})
    print(f"\n🧮 ③ 试算：{profile.get('name') or probe_id} "
          f"{profile.get('level') or '?'} → {args.level_target}")
    try:
        result = compute_materials(
            profile.get("id") or probe_id,
            profile.get("level") or 1,
            args.level_target,
            skills=skill_map_for_targets(profile, {"normal_target": 10, "skill_target": 10,
                                                   "burst_target": 10}),
            weapon={} if not args.with_weapon else (profile.get("weapon") or {}),
            promote_level=profile.get("promote_level") or 0,
            element_attr_id=profile.get("element_attr_id") or 0,
            uid=uid,
        )
    except mys_api.MysError as exc:
        print(f"❌ 算材料失败（{type(exc).__name__}）：{exc}")
        return 1

    phases = {phase: len(rows) for phase, rows in (result["by_phase"] or {}).items() if rows}
    print(f"✅ 解析出 {len(result['requirements'])} 种材料，阶段分布：{phases}")
    for item in result["requirements"][:12]:
        print(f"   · [{growth_models.category_label(item['category'])}] {item['item_name']}"
              f" × {item['required']}（阶段 {item['phases']}）")
    if not result["requirements"]:
        print("⚠️ 一个材料都没解析出来 —— 八成是**请求体形状**变了（不是接口废了）。")
        print("   对照 www 的 bundle：body 必须是**平的**（不能包 data 外壳），等级字段是")
        print("   `avatar_level_current` / `avatar_level_target`，天赋 id 用 `group_id`。")
        print(f"   响应顶层键：{sorted(result['raw'])[:20] if isinstance(result['raw'], dict) else type(result['raw'])}")
        return 1

    # ④ 这一步只是为了把"真正的失败原因"说清楚：背包接口确实被米游社关了
    print("\n④ 背包（我的角色 / 背包）：", end="")
    try:
        fetch_my_items(uid=uid, save=False)
        print("✅ 居然通了（米游社又开放了？值得同步一次库存）")
    except mys_api.MysError as exc:
        print(f"❌ 不可用 —— {exc}")
        print("   ↳ 这条**不是 cookie 的问题**，米游社对它一律回 -100。")
        print("     影响：拿不到背包里已有的材料数量，缺口按「总需求」算（需求本身照常算得出来）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
