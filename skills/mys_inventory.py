"""米游社库存同步：把"背包里到底有多少材料"落成本地可信数据。

**这个文件存在的唯一理由**：BetterGI 永远不知道它捡到了多少东西，所以真实库存
只能从米游社读（规格书 §3）。任何"BGI 预计获得 50 → 库存 +50"的写法都是错的。

数据流：

    mys_calculator.fetch_my_items()         ← 米游社背包接口（含 cookie 签名）
              │
              ▼
    normalise_items()                       ← 统一成内部模型（不暴露米游社原始 JSON）
              │
              ├─→ memory/mys/inventory_latest.json          （权威副本，人可读可 diff）
              ├─→ memory/mys/snapshots/inventory_<时间>.json （历史，用于"两次之间涨了多少"）
              └─→ growth.db: inventory_snapshots / inventory_items / sync_history（账本）

**失败时绝不假装成功**：同步失败会保留仓库里上一次成功的 `inventory_latest.json`，
但 `status()` 会明确标出 `stale=True` 和"这份不是实时数据"（规格书 §5 / §33）。
"""

import datetime
import json
import os

import config

from brain import growth_db, growth_models
from skills import mys_api, mys_calculator

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
_SNAPSHOT_STAMP = "%Y%m%d_%H%M%S"
# 历史快照保留份数：一天同步十几次也不至于把 memory/ 撑爆
MAX_SNAPSHOTS = 200


class InventoryError(mys_api.MysError):
    """库存同步失败（继承 MysError，调用方可以统一按"米游社不可用"处理）。"""


class InventoryAccountMismatch(InventoryError):
    """读回来的库存和配置的 UID / 区服对不上 —— **宁可失败也不串号**。"""


# ==========================================
# 🌟 路径
# ==========================================


def mys_dir():
    return getattr(config, "GROWTH_MYS_DIR", "") or config.project_path("memory", "mys")


def latest_path():
    return os.path.join(mys_dir(), "inventory_latest.json")


def snapshots_dir():
    return os.path.join(mys_dir(), "snapshots")


def character_dir():
    return os.path.join(mys_dir(), "character")


def calculator_dir():
    return os.path.join(mys_dir(), "calculator")


# 养成计算器给的「已有」材料（`available_material`）落在这里。
# ⚠️ 它和 `inventory_latest.json`（那份"完整背包快照"）**不是一回事**：
#   计算器只给**部分**材料，所以单独存一个文件，绝不能拿它冒充完整背包 ——
#   否则界面上会显示"你只有 4 种材料"。
def known_path():
    return os.path.join(mys_dir(), "known_materials.json")


def save_known_materials(items, source="calculator"):
    """把"已知已有的材料"落盘（原子写；失败只提示，不影响算材料）。"""
    payload = {
        "fetched_at": datetime.datetime.now().strftime(_TIME_FORMAT),
        "source": source,
        "items": list(items or ()),
    }
    target = known_path()
    try:
        _ensure_dirs()
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        return payload
    except OSError as exc:
        print(f"⚠️ 已知材料写入失败（不影响本次算材料）：{exc}")
        return payload


def load_known_materials():
    """读回"已知已有的材料"（没有就返回空）。"""
    try:
        with open(known_path(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {"fetched_at": "", "source": "", "items": []}
    if not isinstance(payload, dict):
        return {"fetched_at": "", "source": "", "items": []}
    payload.setdefault("items", [])
    return payload


def _ensure_dirs():
    for directory in (mys_dir(), snapshots_dir(), character_dir(), calculator_dir()):
        os.makedirs(directory, exist_ok=True)


# ==========================================
# 🌟 内部库存模型（规格书 §4.2）
# ==========================================


def item_key(item_id):
    return f"item_{int(item_id)}"


def make_item(item_id, name="", count=0, category="", icon=""):
    """一条内部库存条目。**对外只出现这个形状**，米游社原始 JSON 不往上层传。"""
    item_id = int(item_id)
    category = str(category or "") or growth_models.classify_item(name=name, item_id=item_id)
    return {
        "item_id": item_id,
        "name": str(name or ""),
        "count": max(0, int(count or 0)),
        "category": category,
        "icon": str(icon or ""),
    }


def empty_inventory(uid="", server="", fetched_at=""):
    return {
        "uid": str(uid or ""),
        "server": str(server or ""),
        "fetched_at": str(fetched_at or ""),
        "source": "mys",
        "items": {},
    }


# 米游社背包条目的字段名在不同版本里换过（id / item_id、num / count / cnt），
# 这里一次性都认下来 —— 接口小改版不用动上层。
_ITEM_ID_FIELDS = ("item_id", "id", "material_id")
_ITEM_COUNT_FIELDS = ("num", "count", "cnt", "quantity", "number")
_ITEM_NAME_FIELDS = ("name", "item_name", "name_zh")
_ITEM_ICON_FIELDS = ("icon", "icon_url", "img")


def _first(raw, names, default=None):
    for name in names:
        if name in raw and raw[name] not in (None, ""):
            return raw[name]
    return default


def parse_item(raw):
    """把米游社的一条背包记录转成内部条目；认不出来返回 None（不猜）。"""
    if not isinstance(raw, dict):
        return None
    item_id = _first(raw, _ITEM_ID_FIELDS)
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return None

    count = _first(raw, _ITEM_COUNT_FIELDS, 0)
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0

    name = str(_first(raw, _ITEM_NAME_FIELDS, "") or "")
    icon = str(_first(raw, _ITEM_ICON_FIELDS, "") or "")
    return make_item(item_id, name=name, count=count, icon=icon)


def normalise_items(raw_items):
    """原始列表 → `{item_<id>: 条目}`。同 id 出现多次时**取最大值**（不会被小值覆盖）。"""
    items = {}
    for raw in raw_items or ():
        item = parse_item(raw)
        if item is None:
            continue
        key = item_key(item["item_id"])
        old = items.get(key)
        if old is None or item["count"] > old["count"]:
            items[key] = item
    return items


def normalise_payload(payload, uid="", server="", fetched_at="", raw_items=None):
    """把一次同步的结果组装成内部库存模型。"""
    if raw_items is None:
        raw_items = mys_calculator.extract_item_list(payload)
    model = empty_inventory(
        uid=uid or mys_calculator.extract_uid(payload),
        server=server or mys_calculator.extract_server(payload),
        fetched_at=fetched_at or datetime.datetime.now().strftime(_TIME_FORMAT),
    )
    model["items"] = normalise_items(raw_items)
    return model


# ==========================================
# 🌟 落盘 / 读盘
# ==========================================


def _atomic_write_json(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def save_inventory(model, path=None):
    """写权威副本（原子写：写到一半断电也不会留下半截 JSON）。"""
    _ensure_dirs()
    return _atomic_write_json(path or latest_path(), model)


def load_inventory(path=None):
    """读权威副本。文件不存在 / 坏了都返回 None —— 调用方据此判定"从来没同步过"。"""
    try:
        with open(path or latest_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        return None
    return data


def save_snapshot(model, moment=None, path=None):
    """写一份带时间戳的历史快照，并顺手清理过老的（保留 `MAX_SNAPSHOTS` 份）。"""
    _ensure_dirs()
    stamp = (moment or datetime.datetime.now()).strftime(_SNAPSHOT_STAMP)
    target = path or os.path.join(snapshots_dir(), f"inventory_{stamp}.json")
    _atomic_write_json(target, model)
    _prune_snapshots()
    return target


def _prune_snapshots():
    try:
        rows = sorted(
            (row for row in os.listdir(snapshots_dir()) if row.startswith("inventory_") and row.endswith(".json")),
        )
    except OSError:
        return
    for name in rows[:-MAX_SNAPSHOTS]:
        try:
            os.remove(os.path.join(snapshots_dir(), name))
        except OSError:
            pass


def list_snapshots(limit=30):
    """历史快照文件名（新的在前）。"""
    try:
        rows = sorted(
            (row for row in os.listdir(snapshots_dir()) if row.startswith("inventory_") and row.endswith(".json")),
            reverse=True,
        )
    except OSError:
        return []
    return rows[: int(limit)]


def load_snapshot_file(name):
    """按文件名读一份历史快照（只认 snapshots 目录里的文件名，不接受任意路径）。"""
    safe = os.path.basename(str(name or ""))
    if not safe.startswith("inventory_") or not safe.endswith(".json"):
        return None
    return load_inventory(os.path.join(snapshots_dir(), safe))


def save_character_detail(character_id, payload):
    """把米游社原始角色详情存一份（调试 / 接口改版排查用，规格书 §6）。"""
    _ensure_dirs()
    return _atomic_write_json(os.path.join(character_dir(), f"{int(character_id)}.json"), payload)


def load_character_detail(character_id):
    try:
        with open(os.path.join(character_dir(), f"{int(character_id)}.json"), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# ==========================================
# 🌟 账号对齐（防串号）
# ==========================================


def expected_uid():
    return str(getattr(config, "MYS_UID", "") or getattr(config, "DEFAULT_UID", "") or "").strip()


def check_account(model, expected=None):
    """校验库存归属：UID 不一致就抛异常。

    为什么要拦：cookie 名下可能有多个原神角色（官服 + B 服 / 大小号）。读错号的背包
    会让整套规划建立在不存在的材料上，而且**表面上一切正常** —— 是最难查的一类 bug。
    """
    wanted = str(expected if expected is not None else expected_uid()).strip()
    got = str((model or {}).get("uid") or "").strip()
    if wanted and got and wanted != got:
        raise InventoryAccountMismatch(
            f"读到的库存属于 UID {got}，但配置里要的是 {wanted}。"
            "请把对应账号的 cookie 换成这个号的，或把 MYS_UID / DEFAULT_UID 改成 "
            f"{got}。（宁可不同步，也不用错号的库存做规划）"
        )
    return True


# ==========================================
# 🌟 同步主流程
# ==========================================


def sync(uid=None, server=None, force=True, save=True, path=None, db_path=None):
    """同步一次库存并落盘。返回 `{ok, inventory, snapshot_id, delta, error, ...}`。

    **永远不抛异常给调用方**（Studio 的启动路径不能因为米游社抽风就崩），
    失败信息放在 `ok=False` + `error` 里，并把"上一次成功快照还在不在"一并说清楚。

    ⚠️⚠️ **2026-09 起：米游社关掉了"背包"这个数据源**。这个函数以前靠
    `POST /v1/sync/avatar/list`（"我的角色 / 背包"）拿账号里已有的材料数量，
    现在那个接口带 cookie 也一律返回 `-100 请先登录后参与活动`（实测过十几种
    拼法：纯 v1 cookie / 纯 v2 / 都带、`x-rpc-ltoken` 头、`uid`/`region` 放 query
    或 body……全一样）；同一个 cookie 打**单个角色**的
    `/v1/sync/avatar/detail` 却是好的 —— 所以**不是 cookie 的问题**。
    于是这里如实报 `backpack_unavailable`，而不是再喊"cookie 失效"骗人去重新扫码。

    材料**需求**完全不受影响：那条链路走 `mys_calculator.compute_materials()`
    （米游社的算材料接口给分阶段的完整需求），见
    `brain/growth_planner.requirements_for()`。缺口会退化成"按总需求刷"。
    """
    started_at = datetime.datetime.now().strftime(_TIME_FORMAT)
    uid = str(uid if uid is not None else expected_uid()).strip()
    result = {
        "ok": False,
        "started_at": started_at,
        "finished_at": "",
        "uid": uid,
        "server": str(server or ""),
        "inventory": None,
        "snapshot_id": 0,
        "snapshot_file": "",
        "delta": [],
        "error": "",
        "error_kind": "",
        "stale": False,
        "last_success_at": "",
        # `skipped=True` = 这条数据源已经不可用，但**不是错误**（不记历史、不弹提示）
        "skipped": False,
    }

    if not mys_api.cookie_configured():
        result["error"] = "没有配置米游社 Cookie（MYS_COOKIE）。库存同步整体关闭。"
        result["error_kind"] = "not_configured"
        result["stale"] = True
        result["last_success_at"] = _last_success_time(db_path)
        result["finished_at"] = datetime.datetime.now().strftime(_TIME_FORMAT)
        return result

    sync_id = growth_db.start_sync(uid, path=db_path)
    try:
        payload = mys_calculator.fetch_my_items(uid=uid, server=server)
        model = normalise_payload(payload, uid=uid, server=server)
        if not model["items"]:
            raise InventoryError(
                "米游社返回的背包是空的（可能是接口改版，或这个号还没做过"
                "「养成计算器」的背包读取授权）。"
            )
        check_account(model, expected=uid or None)

        # ⚠️ 先写库账本再写文件：写文件失败（磁盘满）时不会留下"库里有、文件没有"的错位
        snapshot_id = growth_db.record_snapshot(
            uid=model["uid"], server=model["server"], fetched_at=model["fetched_at"],
            items=model["items"], source="mys", success=True, path=db_path,
        )
        previous = growth_db.previous_snapshot(snapshot_id, path=db_path)
        delta = growth_db.inventory_delta(snapshot_id, path=db_path, limit=50)

        if save:
            result["snapshot_file"] = save_snapshot(model)
            save_inventory(model, path=path)

        growth_db.finish_sync(sync_id, True, snapshot_id=snapshot_id, path=db_path)
        result.update({
            "ok": True,
            "inventory": model,
            "snapshot_id": snapshot_id,
            "delta": delta,
            "item_count": len(model["items"]),
            "previous_at": (previous or {}).get("fetched_at", ""),
        })
    except mys_api.MysError as exc:
        result["error"] = str(exc)
        result["error_kind"] = _error_kind(exc)
        # 背包接口已经被米游社关掉了（2026-09 实测）。判断依据很明确：
        # cookie 本身是**完整**的（audit 通过），这个接口却还说"未登录" ——
        # 那就是接口不可用，不是玩家的 cookie 过期。别让人白跑一趟扫码。
        if result["error_kind"] == "auth" and mys_api.audit_cookie()["ok"]:
            result["error_kind"] = "backpack_unavailable"
        if result["error_kind"] == "backpack_unavailable":
            # ⚠️ 这一种**不记失败、也不弹错**：
            #   · 「已有材料」现在由**养成计算器**给（`available_material`，每次算材料都会刷新，
            #     见 `mys_calculator.parse_available_material`），这条老接口拿不到并不缺数据；
            #   · 以前每次都往同步历史塞一条失败，界面上「失败 50 次」全是它 —— 纯噪音。
            result["skipped"] = True
            result["error"] = ("背包接口已不可用（米游社不向程序开放）；"
                               "材料的「已有 / 还差」现在由养成计算器提供。")
            # ⚠️ 顺手把"开始时插的那条记录"**删掉**：
            #   这次不算一次尝试，留着只会变成"失败 + 空错误"的孤儿行（踩过）。
            growth_db.abort_sync(sync_id, path=db_path)
        else:
            # 真的失败了（网络 / 风控 / 空的背包数据）→ 如实记一条
            growth_db.finish_sync(sync_id, False, error=str(exc), path=db_path)
    except Exception as exc:            # noqa: BLE001 —— 同步失败不该把 Studio 打崩
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["error_kind"] = "unexpected"
        growth_db.finish_sync(sync_id, False, error=result["error"], path=db_path)

    if not result["ok"]:
        # 保留上一次成功快照，但**明确标注它不是实时数据**
        cached = load_inventory(path=path)
        result["inventory"] = cached
        result["stale"] = True
        result["last_success_at"] = (cached or {}).get("fetched_at") or _last_success_time(db_path)
        result["cached"] = bool(cached)
        # 认证类失败时顺手把"cookie 缺什么"说清楚 —— 缺 ltoken 的 cookie 能列出角色、
        # 看起来一切正常，但所有需要登录的接口都会失败，光看"未登录"根本猜不到原因。
        if result.get("error_kind") in ("auth", "api"):
            audit = mys_api.audit_cookie()
            if not audit["ok"]:
                result["cookie_audit"] = audit
                result["error"] = f"{result['error']}\n{audit['hint']}"

    result["finished_at"] = datetime.datetime.now().strftime(_TIME_FORMAT)
    return result


def _error_kind(exc):
    if isinstance(exc, InventoryAccountMismatch):
        return "account_mismatch"
    if isinstance(exc, InventoryError):
        return "inventory"
    if isinstance(exc, mys_api.MysNotConfigured):
        return "not_configured"
    if isinstance(exc, mys_api.MysAuthError):
        return "auth"
    if isinstance(exc, mys_api.MysRiskControl):
        return "risk_control"
    if isinstance(exc, mys_api.MysTransportError):
        return "transport"
    if isinstance(exc, mys_api.MysApiError) and "avatar/list" in str(exc):
        # 被关掉的那个"我的角色 / 背包"接口抛的就是这条
        return "backpack_unavailable"
    return "api"

def _last_success_time(db_path=None):
    snapshot = growth_db.latest_snapshot(path=db_path)
    return str((snapshot or {}).get("fetched_at") or "")


def error_advice(kind):
    """按错误类型给一句能照着做的建议（Studio 直接显示，不用前端拼文案）。"""
    return {
        "not_configured": "在「配置 → 米游社」里填 MYS_COOKIE（扫码登录最省事，见 docs/MYS_COOKIE.md）。",
        "auth": "Cookie 无效或已过期：到「配置 → 米游社个人战绩」点「扫码登录」重新取一份。",
        "risk_control": "米游社要验证码了：打开米游社 App 点两下完成验证，过几小时再同步。",
        "transport": "网络不通或超时：检查代理 / 网络后重试。",
        "account_mismatch": "cookie 对应的号和你配的 UID 不是同一个，改 UID 或换 cookie。",
        "api": "米游社接口返回异常，稍后重试；反复失败请看 docs/GROWTH.md 的接口变更一节。",
        "inventory": "背包读回来是空的：稍后重试；仍然为空请把消息反馈给开发者。",
        # ★ 现在最常见的一种：米游社把"我的角色 / 背包"接口关了（2026-09 实测）。
        #    这不是 cookie 的问题，所以**必须**说清楚，否则玩家会一直去重新扫码。
        #    也别写太长 —— 这段会直接出现在 Agent 的回复里。
        "backpack_unavailable":
            "米游社关掉了「我的角色 / 背包」接口（/v1/sync/avatar/list 带 cookie 也回 -100），"
            "所以拿不到你背包里已有的材料数量。\n"
            "**这不影响养成规划**：材料需求照常算得出来（等级 / 天赋 / 武器都有），"
            "只是缺口按「总需求」算 —— 需要多少就刷多少，可能比真实缺口多一些。",
        "empty_compute":
            "算材料接口这次一个材料都没算出来（它不报错，只回空桶）。"
            "常见原因：这个角色在米游社计算器里打不开 / 参数没凑齐。"
            "自检：python -m skills.mys_calculator --check。",
    }.get(str(kind or ""), "同步失败，稍后重试。")


# ==========================================
# 🌟 查询：给 Planner / Studio / LLM 工具用
# ==========================================


def _stale_hours():
    return float(getattr(config, "GROWTH_INVENTORY_STALE_HOURS", 12) or 12)


def is_stale(fetched_at, now=None, max_age_hours=None):
    """这份时间戳算不算过期（没有时间戳 = 过期：宁可提醒玩家，也别让他信旧数据）。"""
    age = _age_hours(fetched_at, now=now)
    if age is None:
        return True
    limit = _stale_hours() if max_age_hours is None else float(max_age_hours)
    return age >= limit


def _age_hours(fetched_at, now=None):
    moment = growth_db.parse_time(fetched_at)
    if moment is None:
        return None
    return ((now or datetime.datetime.now()) - moment).total_seconds() / 3600.0


def describe_age(fetched_at, now=None):
    age = _age_hours(fetched_at, now=now)
    if age is None:
        return "从未同步"
    if age < 1:
        return f"{max(0, int(age * 60))} 分钟前"
    if age < 48:
        return f"{age:.1f} 小时前"
    return f"{age / 24:.1f} 天前"


def status(path=None, db_path=None, now=None):
    """库存状态摘要（**不发任何网络请求**，Studio 每次刷页面都能安全调）。

    `stale=True` 的含义是"这份数据不能当实时数据用"，前端必须把它显示出来。
    """
    model = load_inventory(path=path)
    fetched_at = (model or {}).get("fetched_at") or ""
    if not model:
        last = _last_success_time(db_path=db_path)
        return {
            "configured": mys_api.cookie_configured(),
            "available": False,
            "stale": True,
            "fetched_at": last,
            "age_text": describe_age(last, now=now),
            "item_count": 0,
            "uid": "",
            "server": "",
            "hint": "还没有同步过米游社库存。",
        }
    age = _age_hours(fetched_at, now=now)
    stale = is_stale(fetched_at, now=now)
    return {
        "configured": mys_api.cookie_configured(),
        "available": True,
        "stale": bool(stale),
        "fetched_at": fetched_at,
        "age_text": describe_age(fetched_at, now=now),
        "item_count": len(model.get("items") or {}),
        "uid": model.get("uid") or "",
        "server": model.get("server") or "",
        "stale_hours": _stale_hours(),
        "hint": (
            f"这份不是实时数据（{describe_age(fetched_at, now=now)}同步的），"
            "材料缺口可能已经不准，建议先同步。"
            if stale else ""
        ),
    }


def counts(model=None):
    """`{item_id: count}`（最常用的形状，Planner 每次都要）。"""
    model = model if model is not None else load_inventory() or {}
    result = {}
    for item in (model.get("items") or {}).values():
        try:
            result[int(item.get("item_id"))] = int(item.get("count") or 0)
        except (TypeError, ValueError):
            continue
    return result


def owned(item_id, model=None):
    """某个材料现在有多少（库存里没有 = 0）。"""
    try:
        key = item_key(item_id)
    except (TypeError, ValueError):
        return 0
    model = model if model is not None else load_inventory() or {}
    return int(((model.get("items") or {}).get(key) or {}).get("count") or 0)


def find_by_name(name, model=None):
    """按材料名找库存条目（LLM 只给得起名字时用）。"""
    wanted = str(name or "").strip()
    if not wanted:
        return None
    model = model if model is not None else load_inventory() or {}
    for item in (model.get("items") or {}).values():
        if str(item.get("name") or "").strip() == wanted:
            return item
    return None


def material_lines(item_ids, model=None, limit=50):
    """把若干 item_id 渲染成"名字 × 数量"给 LLM 看的行。"""
    model = model if model is not None else load_inventory() or {}
    lines = []
    for item_id in list(item_ids or ())[: int(limit)]:
        entry = (model.get("items") or {}).get(item_key(item_id)) or {}
        if not entry:
            continue
        lines.append(f"{entry.get('name') or item_id} × {entry.get('count') or 0}")
    return lines
