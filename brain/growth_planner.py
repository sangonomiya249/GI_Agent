"""养成规划核心：把"角色目标 + 米游社库存 + 养成计算器需求"变成一份可执行的计划。

流程图（规格书 §20 的状态机就落在 `build_plan()` 的返回值里）：

    ① 读角色目标（按 §14 排序）
          ↓
    ② 同步米游社库存（读权威副本，必要时刷新）
          ↓
    ③ 问养成计算器：每个角色还差多少材料（§10，绝不自己算）
          ↓
    ④ 共享池扣减 → 材料缺口 + BetterGI 任务（brain/material_planner.py）
          ↓
    ⑤ 定位"当前角色 / 当前阶段" → bettergi_cmd（等玩家确认后由执行层下发）

**这个文件是养成系统唯一的规划入口**（§38.11）：Studio API、Agent 工具、
首页的"今日养成计划"全都调它，不允许任何地方自己再算一遍缺口。

关键约束（都在测试里盯着）：
  · 当前角色材料不足时**不会**跳到下一个角色（§16）；
  · 当前角色三阶段全部完成才进下一个角色（§16）；
  · BetterGI 只执行、**不回写库存**（§19）；
  · 缺口永远基于"最近一次成功的米游社同步"，并且带 `inventory.stale` 标记（§5/§33）。
"""

import datetime
import json
import threading

import config

from brain import execution_queue, growth_db, growth_models, material_planner, resin_math
from skills import mys_api, mys_calculator, mys_inventory, mys_resin

# ==========================================
# 🌟 缓存：米游社角色档案 + 材料需求
# ==========================================
#
# 为什么要缓存：Studio 每次刷新页面都会问一次计划，而"问米游社算材料"是**网络请求**。
# 不缓存的话，打开一次养成页就是 N 次请求，米游社风控立刻找上门（项目里踩过这个坑）。
# 缓存失效的时机很明确：任何一次库存同步、任何一次目标变更 —— 都调 `invalidate_cache()`。
_CACHE_LOCK = threading.Lock()
_CACHE = {
    "avatars_at": 0.0,
    "avatars_uid": "",
    "avatars": None,
    "compute": {},          # character_id -> {"at": ts, "result": {...}, "signature": "..."}
}

# 计算器字段语义变更时，不能继续复用旧的“消耗=缺口”结果。
# 只要解析逻辑升级，自动让当前进程中的旧结果失效。
_COMPUTE_SCHEMA = "batch-num-total-overall-lack-v2"


def invalidate_cache(character_id=None):
    """让缓存失效（库存同步后 / 目标改了以后必须调）。"""
    with _CACHE_LOCK:
        _CACHE["avatars"] = None
        _CACHE["avatars_at"] = 0.0
        _CACHE["avatars_uid"] = ""
        if character_id is None:
            _CACHE["compute"] = {}
            _day_cache_clear()
        else:
            _CACHE["compute"].pop(str(int(character_id)), None)


def _cache_minutes():
    return float(getattr(config, "GROWTH_COMPUTE_CACHE_MINUTES", 30) or 30)


def _fresh(moment, minutes=None):
    if not moment:
        return False
    age = (datetime.datetime.now() - datetime.datetime.fromtimestamp(moment)).total_seconds()
    return age < (minutes if minutes is not None else _cache_minutes()) * 60


# ==========================================
# 🌟 材料结果的"每天一次"缓存（落在数据库里，跨重启有效）
# ==========================================
#
# 为什么要它：算材料是**每个角色 2 个请求**，而米游社风控很敏感 ——
# 每次关掉再打开就重拉一轮的话，一天开五次就是十几二十个请求，迟早被风控。
# 玩家明确要求："每天打开程序自动拉一次，关掉再打开就别拉了，想更新自己点按钮。"
#
# 做法：把每个角色的算材料结果（含"已有 / 还差"）存进 `settings` 表（键值对，
# 不用改表结构），新鲜度按**业务日**判定（项目里统一是凌晨 4 点换日）。
#   · 同一天再打开 → 直接读库，**一个请求都不发**；
#   · 到了新的一天 → 自动拉一次；
#   · 玩家点「刷新材料数据」→ `force_compute=True`，**无视缓存**立刻重拉。
# 关掉这个行为：`.env` 里 `GROWTH_COMPUTE_DAILY_CACHE=0`（退回原来的 30 分钟内存缓存）。
_COMPUTE_CACHE_KEY = "compute_cache_v1"


def _daily_cache_enabled():
    return bool(getattr(config, "GROWTH_COMPUTE_DAILY_CACHE", True))


def _business_day(moment=None):
    """业务日字符串（凌晨 4 点换日，和项目里其它地方一致）。"""
    return growth_models.business_now(moment).strftime("%Y-%m-%d")


def _day_cache_lookup(character_id):
    """从**数据库**里读某个角色今天算好的结果（没有 / 不是今天就返回 None）。"""
    try:
        blob = growth_db.get_setting(_COMPUTE_CACHE_KEY)
        payload = json.loads(blob) if blob else {}
    except (ValueError, TypeError, OSError):
        return None
    entry = (payload.get("entries") or {}).get(str(int(character_id)))
    if not isinstance(entry, dict):
        return None
    if entry.get("schema") != _COMPUTE_SCHEMA:
        return None                      # 解析逻辑升级过 → 旧结果不能复用
    if entry.get("day") != _business_day():
        return None                      # 不是今天的 → 让上层去拉新的
    return entry


def _day_cache_store(character_id, signature, result):
    """把算好的结果写进数据库（同一天再打开就直接用它）。"""
    try:
        blob = growth_db.get_setting(_COMPUTE_CACHE_KEY)
        payload = json.loads(blob) if blob else {}
    except (ValueError, TypeError, OSError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    entries = payload.setdefault("entries", {})
    # `raw` 是给排查用的原始响应，体积大、界面也不用 —— 存库前丢掉
    slim = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    for key in ("state", "plan"):
        if isinstance(slim.get(key), dict):
            slim[key].pop("raw", None)
    entries[str(int(character_id))] = {
        "schema": _COMPUTE_SCHEMA,
        "day": _business_day(),
        "at": datetime.datetime.now().timestamp(),
        "signature": signature,
        "result": slim,
    }
    payload["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        growth_db.set_setting(_COMPUTE_CACHE_KEY,
                              json.dumps(payload, ensure_ascii=False))
    except Exception as exc:            # noqa: BLE001 —— 写不进去不影响这次规划
        print(f"⚠️ 材料结果落库失败（不影响本次）：{type(exc).__name__} {exc}")


def _day_cache_clear():
    """清空这份缓存（目标改了 / 手动刷新时用）。"""
    try:
        growth_db.set_setting(_COMPUTE_CACHE_KEY, "")
    except Exception:                   # noqa: BLE001
        pass


# ==========================================
# 🌟 ① 角色目标
# ==========================================


def sort_mode(path=None):
    """排序模式（默认 / 自定义 / 自动），存在 growth.db 的 settings 里。"""
    mode = growth_db.get_setting("character_sort_mode", growth_models.SORT_MODE_DEFAULT, path=path)
    return mode if mode in growth_models.SORT_MODES else growth_models.SORT_MODE_DEFAULT


def set_sort_mode(mode, path=None):
    if mode not in growth_models.SORT_MODES:
        return sort_mode(path=path)
    growth_db.set_setting("character_sort_mode", mode, path=path)
    return mode


def ordered_targets(path=None, enabled_only=True):
    """按当前排序模式排好的角色目标（养成顺序就是这个顺序）。"""
    return growth_models.sort_targets(
        growth_db.list_targets(path=path, enabled_only=enabled_only), mode=sort_mode(path=path)
    )


# ==========================================
# 🌟 ② 米游社档案（角色当前状态 / 天赋 id）
# ==========================================


def available_characters(force=False, uid=None):
    """"添加培养目标"那个列表要用的角色候选。

    完整图鉴负责提供候选；拥有状态通过单角色详情接口逐个确认，并持久化到
    `settings`。批量的「我的角色」接口已下线，`force=True` 才会重新检查全部角色。
    """
    merged = {}
    wanted_uid = str(uid or _uid()).strip()
    catalog = {}
    try:
        catalog = mys_calculator.fetch_catalog_all(uid=wanted_uid)
    except mys_api.MysError as exc:
        print(f"⚠️ 图鉴不可用：{exc}")

    # The batch "my characters" endpoint is retired. Confirm ownership through
    # the single-avatar endpoint and reuse the persisted result afterwards.
    known = scan_owned_characters(catalog, force=force, uid=wanted_uid)
    state_profiles = _ownership_states(_ownership_payload())
    for character_id, avatar in catalog.items():
        character_id = int(character_id)
        value = known.get(character_id)
        profile = state_profiles.get(character_id) if value else None
        if profile:
            enriched = dict(avatar)
            for key in ("id", "name", "level", "rarity", "element"):
                if profile.get(key) not in ("", None, 0):
                    enriched[key] = profile[key]
            if profile.get("talents"):
                enriched["talents"] = profile["talents"]
            if profile.get("weapon"):
                enriched["weapon"] = profile["weapon"]
            avatar = enriched
        merged.setdefault(character_id, dict(
            avatar,
            owned=bool(value),
            owned_known=value is not None,
        ))

    # ③ 本地记账过的角色（完全离线）
    try:
        for row in growth_db.list_characters() or ():
            character_id = int(row.get("character_id") or 0)
            if character_id and character_id not in merged:
                value = known.get(character_id)
                merged[character_id] = {
                    "id": character_id, "name": row.get("name") or "",
                    "rarity": int(row.get("rarity") or 0),
                    "element": row.get("element") or "",
                    "level": int(row.get("level") or 0),
                    "owned": bool(value), "owned_known": value is not None,
                }
    except Exception:                   # noqa: BLE001 —— 兜底失败不该让列表报错
        pass

    for avatar in merged.values():
        avatar.setdefault("owned", False)
        avatar.setdefault("owned_known", False)
    return merged


# 「这个号有没有这个角色」的知识库：`{character_id: True/False}`。
# ⚠️ 米游社关掉了"我的角色"接口，问不到完整名单 —— 只能**遇到才记**：
#   · 算材料时问 `/v1/sync/avatar/detail`，它明确回 `owned` 或"伙伴不存在"；
#   · 或者玩家在「添加角色」里自己选。
# 存在 `settings` 表（键值对），跨重启有效。
_OWNERSHIP_KEY = "owned_characters_v1"


def _ownership_payload():
    """Read both the current dict form and the legacy JSON-string form."""
    raw = growth_db.get_setting(_OWNERSHIP_KEY)
    payload = raw
    for _ in range(2):
        if not isinstance(payload, str):
            break
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _ownership_entries(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, value in value.items():
        try:
            character_id = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        result[character_id] = bool(value)
    return result


def _ownership_map_from_payload(payload):
    result = _ownership_entries((payload or {}).get("entries"))
    # A manual choice is an explicit correction and wins over the last scan.
    result.update(_ownership_entries((payload or {}).get("manual_entries")))
    return result


def _ownership_states(payload):
    states = (payload or {}).get("states")
    if not isinstance(states, dict):
        return {}
    result = {}
    for key, value in states.items():
        try:
            character_id = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            result[character_id] = dict(value)
    return result


def _save_ownership_payload(payload):
    growth_db.set_setting(_OWNERSHIP_KEY, payload)


def ownership_map():
    return _ownership_map_from_payload(_ownership_payload())


def ownership_scan_status():
    """Return non-secret status for the Studio ownership badge/note."""
    payload = _ownership_payload()
    try:
        configured = bool(mys_api.cookie_configured())
    except Exception:                       # noqa: BLE001
        configured = False
    return {
        "configured": configured,
        "complete": bool(payload.get("scan_complete")),
        "uid": str(payload.get("scan_uid") or ""),
        "scanned_at": str(payload.get("scanned_at") or ""),
        "error": str(payload.get("scan_error") or ""),
    }


def scan_owned_characters(catalog, force=False, uid=None):
    """Confirm ownership for catalogue ids using the single-avatar endpoint.

    The retired batch endpoint is intentionally not used here. A normal call only
    fills missing ids after a completed scan; `force=True` rechecks every id.
    """
    catalog = catalog or {}
    character_ids = sorted({
        int(character_id)
        for character_id in catalog
        if str(character_id).strip().lstrip("-").isdigit()
    })
    if not character_ids:
        return ownership_map()
    payload = _ownership_payload()
    entries = _ownership_entries(payload.get("entries"))
    states = _ownership_states(payload)
    wanted_uid = str(uid or _uid()).strip()
    same_uid = str(payload.get("scan_uid") or "") == wanted_uid
    missing_owned_states = [
        character_id for character_id, owned in entries.items()
        if owned and character_id in character_ids and character_id not in states
    ]
    complete = bool(payload.get("scan_complete")) and same_uid and all(
        character_id in entries for character_id in character_ids
    ) and not missing_owned_states
    if complete and not force:
        return _ownership_map_from_payload(payload)

    try:
        if not mys_api.cookie_configured():
            return _ownership_map_from_payload(payload)
    except Exception:                       # noqa: BLE001
        return _ownership_map_from_payload(payload)

    scan_ids = character_ids if (force or not same_uid or not payload.get("scan_complete")) \
        else sorted(set(
            [character_id for character_id in character_ids if character_id not in entries]
            + missing_owned_states
        ))
    error = ""
    for character_id in scan_ids:
        try:
            state = mys_calculator.fetch_avatar_state(
                character_id, uid=wanted_uid, server=None
            )
        except mys_api.MysError as exc:
            error = f"{type(exc).__name__}: {exc}"
            break
        if not isinstance(state, dict) or "owned" not in state:
            error = f"角色 {character_id} 的拥有状态响应无法识别"
            break
        entries[character_id] = bool(state.get("owned"))
        if state.get("owned"):
            states[character_id] = _avatar_cache_from_state(state)
        else:
            states.pop(character_id, None)

    payload.update({
        "entries": {str(key): value for key, value in entries.items()},
        "states": {str(key): value for key, value in states.items()},
        "scan_uid": wanted_uid,
        "scan_complete": not error and all(
            character_id in entries for character_id in character_ids
        ),
        "scanned_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scan_error": error,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save_ownership_payload(payload)
    return _ownership_map_from_payload(payload)


def _avatar_cache_from_state(state):
    """Small JSON-safe avatar profile for the add-character picker."""
    state = state or {}
    weapon = dict(state.get("weapon") or {})
    talents = {}
    for talent in state.get("talents") or ():
        if not isinstance(talent, dict):
            continue
        group_id = int(talent.get("group_id") or talent.get("id") or 0)
        max_level = int(talent.get("max_level") or 0)
        if not group_id or max_level <= 1:
            continue
        talents[str(group_id)] = int(talent.get("level") or 0)
    return {
        "id": int(state.get("avatar_id") or 0),
        "name": str(state.get("name") or ""),
        "level": int(state.get("level") or 0),
        "rarity": int(state.get("rarity") or 0),
        "element": str(state.get("element_attr_id") or ""),
        "talents": talents,
        "weapon": {
            "id": int(weapon.get("id") or 0),
            "name": str(weapon.get("name") or ""),
            "level": int(weapon.get("level") or weapon.get("level_current") or 0),
        },
    }


def _valid_account_profile(profile):
    """Return whether a cached profile contains real account progress."""
    profile = profile or {}
    if int(profile.get("level") or 0) <= 0:
        return False
    talents = profile.get("talents") or {}
    weapon = profile.get("weapon") or {}
    return bool(talents) or int(
        weapon.get("level") or weapon.get("level_current") or 0
    ) > 0


def _merge_account_profile(avatar, profile):
    """Merge persisted account progress into the static catalogue entry."""
    merged = dict(avatar or {})
    profile = profile or {}
    for key in ("id", "name", "level", "rarity", "element", "talents", "weapon"):
        value = profile.get(key)
        if value not in ("", None, 0, {}, []):
            merged[key] = value
    return merged


def _merge_persisted_account_profiles(avatars):
    """Reuse account states collected by the add-character picker."""
    merged = dict(avatars or {})
    for character_id, profile in _ownership_states(_ownership_payload()).items():
        if _valid_account_profile(profile):
            merged[character_id] = _merge_account_profile(
                merged.get(character_id) or {}, profile
            )
    return merged


def remember_ownership(character_id, owned):
    """Remember an ownership fact obtained from an account query."""
    try:
        character_id = int(character_id)
    except (TypeError, ValueError):
        return
    payload = _ownership_payload()
    entries = _ownership_entries(payload.get("entries"))
    value = bool(owned)
    if entries.get(character_id) is value:
        return                          # 没变化，别白写一次库
    entries[character_id] = value
    payload["entries"] = {str(key): item for key, item in entries.items()}
    payload["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _save_ownership_payload(payload)
    except Exception:                   # noqa: BLE001 —— 记不住不影响算材料
        pass


def set_manual_ownership(character_id, owned):
    """Persist a UI correction without losing the account scan."""
    try:
        character_id = int(character_id)
    except (TypeError, ValueError):
        return
    payload = _ownership_payload()
    entries = _ownership_entries(payload.get("entries"))
    manual = _ownership_entries(payload.get("manual_entries"))
    value = bool(owned)
    entries[character_id] = value
    manual[character_id] = value
    payload["entries"] = {str(key): item for key, item in entries.items()}
    payload["manual_entries"] = {str(key): item for key, item in manual.items()}
    payload["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _save_ownership_payload(payload)
    except Exception:                   # noqa: BLE001 —— 记不住不影响算材料
        pass


def fetch_avatars(force=False, uid=None, save=True):
    """拉一次养成计算器的角色档案（含天赋 id 与等级），带缓存。返回 `{角色id: 档案}`。

    没配 cookie 返回空字典；网络失败返回上一次的缓存（拿不到就空字典）——
    规划会退化成"只有目标、没有当前状态"，而不是整页报错（Studio 上会显示成"未同步"）。
    """
    wanted_uid = str(uid or _uid()).strip()
    with _CACHE_LOCK:
        if (
            not force
            and _CACHE["avatars"] is not None
            and _fresh(_CACHE["avatars_at"])
            and _CACHE.get("avatars_uid", "") == wanted_uid
        ):
            return _CACHE["avatars"]
    if not mys_api.cookie_configured():
        return {}

    try:
        snapshot = mys_api.load_snapshot()
        snapshot_uid = str((snapshot or {}).get("uid") or "").strip()
        uid_changed = bool(wanted_uid and snapshot_uid and snapshot_uid != wanted_uid)

        # The calculator catalogue contains every character.  The account
        # game-record snapshot is the ownership source for this picker.
        if force or uid_changed:
            snapshot = mys_api.refresh_snapshot(
                uid=wanted_uid or None,
                path=mys_api.snapshot_path(),
            )
        else:
            snapshot = mys_api.get_snapshot(
                uid=wanted_uid or None,
                refresh_if_stale=True,
                path=mys_api.snapshot_path(),
            )
        if not snapshot or not snapshot.get("avatars"):
            raise mys_api.MysApiError(
                -1,
                "米游社账号接口没有返回已拥有角色，请重新登录后再同步",
            )
        owned_rows = []
        for row in snapshot.get("avatars") or []:
            row = dict(row)
            # `mys_api` stores talents as {skill_id: level}; the calculator
            # parser consumes its normalised `skills[]` shape.
            if not row.get("skills") and isinstance(row.get("talents"), dict):
                row["skills"] = [
                    {"id": skill_id, "level": level, "max_level": 10}
                    for skill_id, level in row["talents"].items()
                ]
            owned_rows.append(row)
        payload = {"avatars": owned_rows}
        avatars = mys_calculator.parse_avatars(payload)
        by_id = {avatar["id"]: avatar for avatar in avatars}
        if save:
            for avatar in avatars:
                _record_character(avatar)
        with _CACHE_LOCK:
            _CACHE["avatars"] = by_id
            _CACHE["avatars_at"] = datetime.datetime.now().timestamp()
            _CACHE["avatars_uid"] = wanted_uid
        return by_id
    except mys_api.MysError as exc:
        print(f"⚠️ 米游社角色档案不可用（不影响本地已有数据）：{exc}")
        with _CACHE_LOCK:
            if _CACHE.get("avatars_uid", "") == wanted_uid:
                return _CACHE["avatars"] or {}
            return {}


def _record_character(avatar):
    """把角色档案写进 characters 表（名字/星级/等级都是规划与展示要用的）。"""
    try:
        growth_db.upsert_character(
            avatar["id"], name=avatar.get("name", ""), rarity=avatar.get("rarity", 0),
            element=avatar.get("element", ""), level=avatar.get("level", 0),
        )
        weapon = avatar.get("weapon") or {}
        if weapon.get("id"):
            growth_db.upsert_weapon(weapon["id"], name=weapon.get("name", ""),
                                    rarity=weapon.get("rarity", 0))
    except Exception as exc:            # noqa: BLE001 —— 记账失败不该影响规划
        print(f"⚠️ 角色档案落库失败：{type(exc).__name__} {exc}")


def _cached_avatars():
    """**只读内存缓存**的角色档案（不发请求）。

    当前状态的权威来源是 `/v1/sync/avatar/detail`，本地缓存只是"有没有历史数据"的提示。
    """
    with _CACHE_LOCK:
        return dict(_CACHE.get("avatars") or {})


def current_state(character_id, target=None, avatars=None, path=None):
    """一个角色的当前养成状态。

    优先用米游社档案（有天赋 id 与武器等级）；米游社档案里没有这个角色时，
    退回本地 `characters` 表里仅有的等级。

    ⚠️ 本地表没有天赋 / 武器等级，不能把它们假设成目标值。否则像"可莉等级 90，
    但天赋 8/7/7、武器 70"这种角色会被伪造成"目标已达成"，直接跳过真实材料缺口。
    """
    # ⚠️ 这里**不发网络请求**：当前状态的真实来源是 `/v1/sync/avatar/detail`
    #    （`requirements_for` 会去问，并把结果回写进 `entry["current"]`）。
    #    以前这里会调 `fetch_avatars()` → 打"战绩"接口 → 那个接口被风控卡着，
    #    于是每规划一次就刷几行"账号数据异常"的警告，纯噪音。
    avatars = avatars if avatars is not None else _cached_avatars()
    avatars = _merge_persisted_account_profiles(avatars)
    avatar = (avatars or {}).get(int(character_id)) or {}
    if avatar:
        level, talents, weapon_level = growth_models.current_levels(avatar)
        # ⚠️ 图鉴档案里的 `level` 很可能是 **0**（甚至 `base_level=5` 是稀有度）——
        #    那是"图鉴条目"，不是账号里的真实等级。真实等级只在
        #    `/v1/sync/avatar/detail` 里。这里如果拿 0 去算，"从 0 级开始练"会让
        #    米游社直接回 `-500001`（实测），计划就整个报错了。
        #    所以：档案里等级是 0 时不要信它，返回"未知"（`known: False`），
        #    让上层去问米游社拿真实状态。
        if int(level or 0) > 0:
            return {
                "level": level,
                "talents": talents,
                "weapon_level": weapon_level,
                "weapon_id": (avatar.get("weapon") or {}).get("id") or 0,
                "weapon_name": (avatar.get("weapon") or {}).get("name") or "",
                "source": "mys",
                "known": True,
            }

    row = growth_db.get_character(character_id, path=path) or {}
    target = target or {}
    level = int(row.get("level") or 0)
    return {
        "level": level,
        "talents": {"normal": 0, "skill": 0, "burst": 0},
        "weapon_level": 0,
        "weapon_id": int(target.get("weapon_id") or 0),
        "weapon_name": "",
        "source": "local",
        "known": False,
    }


# ==========================================
# 🌟 ③ 养成计算器：材料需求
# ==========================================


def _target_signature(target):
    """目标的"指纹"：变了就要重新问米游社。"""
    keys = ("level_target", "normal_target", "skill_target", "burst_target",
            "weapon_enabled", "weapon_id", "weapon_level_target")
    return "|".join(str((target or {}).get(key, "")) for key in keys)


def _still_has_work(target, current):
    """按"当前状态 vs 目标"判断这个角色**还该不该有材料缺口**。

    compute 返回空的时候用它区分两种情况：
      · 目标本来就达成了（90 → 90）→ 空是正常的；
      · 明明还差得远却返回空       → 接口静默失败，必须报错（否则假完成）。
    """
    target = target or {}
    current = current or {}
    talents = current.get("talents") or {}
    if int(current.get("level") or 0) < int(target.get("level_target") or 90):
        return True
    for key, field in (("normal", "normal_target"), ("skill", "skill_target"),
                       ("burst", "burst_target")):
        if int(talents.get(key) or 0) < int(target.get(field) or 10):
            return True
    if target.get("weapon_enabled") and target.get("weapon_id"):
        if int(current.get("weapon_level") or 0) < int(target.get("weapon_level_target") or 90):
            return True
    return False


# 算材料一个材料都没算出来时的解释。
# ⚠️ 注意：这**不是**"接口废了"—— 2026-09 那一轮之所以一直回空桶，是我们自己的请求体
# 包了 `{"data": ...}` 外壳（米游社前端是把 `{data, headers}` 当 axios config 传的，
# 真正发出去的 body 是平的）。修好之后 `/v2/compute` 一直是好的。
# 所以这里说的是"这次没算出来"，让人去自查参数/角色，而不是说接口不可用。
_EMPTY_HINT = (
    "米游社这次没算出一个材料（它不报错，只回三个空的 consume 桶）。"
    "通常是这个角色在米游社养成计算器里打不开，或者起始状态没凑齐。"
    "自检：python -m skills.mys_calculator --check"
)


def _state_to_current(state):
    """`fetch_avatar_state()` → 计划里"当前状态"那一行的形状（供进度条用）。

    槽位优先用 `pos_name` 认（普通攻击 / 元素战技 / 元素爆发）。
    ⚠️ **新角色的 `pos_name` 可能是空串**（实测：奥黛塔 7 条天赋的 `pos_name` 全是空的），
    那样按名字一个都对不上、界面上会显示 `0/0/0`（虽然不影响算材料，但观感很差）。
    所以认不出名字时按**返回顺序**兜底：前三个 `max_level > 1` 的就是
    普攻 → 战技 → 爆发（米游社就是这么排的，`group_id` 也是升序）。
    """
    state = state or {}
    slots = {}
    ordered = []
    for talent in state.get("talents") or ():
        pos = str(talent.get("pos_name") or "")
        level = int(talent.get("level") or 0)
        matched = None
        for keyword, slot in (("普通攻击", "normal"), ("元素战技", "skill"),
                              ("元素爆发", "burst")):
            if keyword and keyword in pos:
                matched = slot
                break
        if matched:
            slots.setdefault(matched, level)
        if int(talent.get("max_level") or 0) > 1:
            ordered.append(level)
    if len(slots) < 3:
        for slot, level in zip(("normal", "skill", "burst"), ordered):
            slots.setdefault(slot, level)

    weapon = state.get("weapon") or {}
    return {
        "level": int(state.get("level") or 0),
        "talents": {slot: slots.get(slot, 0) for slot in ("normal", "skill", "burst")},
        "weapon_level": int(weapon.get("level_current") or weapon.get("level") or 0),
        "weapon_id": int(weapon.get("id") or 0),
        "weapon_name": str(weapon.get("name") or ""),
        "source": "mys_sync",
        "known": True,
    }


def _plan_target_text(plan):
    """把米游社给的养成目标写成一句人话（"等级 71 → 90｜天赋 10/10/9"）。"""
    plan = plan or {}
    parts = []
    level = int(plan.get("target_level") or 0)
    if level:
        current = int(plan.get("current_level") or 0)
        parts.append(f"等级 {current or '?'} → {level}")
    targets = [int(item.get("target_level") or 0) for item in (plan.get("skills") or ())]
    if targets:
        parts.append("天赋 " + "/".join(str(value) for value in targets[:3]))
    return "｜".join(parts)


def requirements_for(target, avatar=None, force=False, uid=None, server=None,
                     allow_network=True):
    """问米游社算一个角色的材料需求。

    缓存策略（**玩家明确要求的口径**）：
      · **每天自动拉一次**：同一天里不管开关程序几次，都直接用库里存的结果，**不发请求**；
      · 到了新的一天（业务日，凌晨 4 点换日）→ 自动重新拉一次；
      · 玩家点「刷新材料数据」→ `force=True`，**无视缓存**立刻重拉。
    为什么这么做：算材料是每个角色 2 个请求，米游社风控很敏感 ——
    "每次开程序都拉一轮"迟早会被风控盯上（项目里踩过）。

    米游社区不可用时返回 `{"by_phase": {}, "requirements": [], "error": ...}` ——
    **不抛异常**：规划要继续跑（至少能告诉玩家"材料算不出来"）。
    """
    target = target or {}
    character_id = int(target.get("character_id") or 0)
    # 档案只用**缓存**（不发请求）：真正的当前状态待会儿问 `/v1/sync/avatar/detail`。
    avatar = avatar or _cached_avatars().get(character_id) or {}
    # A successful ownership scan persists the real level/talents/weapon.
    # Reuse it before asking the detail endpoint so a transient empty response
    # cannot turn a known account state into Lv 0 / 0-0-0.
    persisted_profile = _ownership_states(_ownership_payload()).get(character_id) or {}
    if _valid_account_profile(persisted_profile):
        avatar = _merge_account_profile(avatar, persisted_profile)
    current = current_state(character_id, target=target,
                            avatars={character_id: avatar} if avatar else {})

    signature = "|".join([
        _COMPUTE_SCHEMA,
        _target_signature(target),
        str(current.get("level")),
        str(current.get("weapon_level")),
        str(sorted((current.get("talents") or {}).items())),
    ])
    key = str(character_id)

    def _usable(entry):
        return bool(entry and entry.get("signature") == signature
                    and entry.get("result") is not None)

    if not force:
        # ① 进程内缓存（同一次运行里刷新页面用）
        with _CACHE_LOCK:
            cached = _CACHE["compute"].get(key)
        if _usable(cached) and (_daily_cache_enabled()
                                or _fresh(cached.get("at"))):
            return dict(cached["result"], _cache_hit=True)
        # ② **数据库里的当天结果**（跨重启；这就是"关掉再打开不再拉"的实现）
        if _daily_cache_enabled():
            stored = _day_cache_lookup(character_id)
            if _usable(stored):
                result = stored["result"]
                with _CACHE_LOCK:
                    _CACHE["compute"][key] = {"signature": signature,
                                              "at": float(stored.get("at") or 0),
                                              "result": result}
                return dict(result, _cache_hit=True)

    # 规划预算只限制新的网络请求。缓存里已有的角色仍然要正常参与本轮规划；
    # 否则前 8 个角色会永久占住预算，后面的角色永远不会被算到。
    if not allow_network:
        return {
            "character_id": character_id,
            "requirements": [],
            "by_phase": {},
            "error": "本轮规划的材料需求量预算已用完，等待下一次重新规划。",
            "error_kind": "compute_budget",
            "deferred": True,
        }

    if not mys_api.cookie_configured():
        return {
            "character_id": character_id, "requirements": [], "by_phase": {},
            "error": "没有配置米游社 Cookie，算不出材料需求（养成计算器需要它）",
            "error_kind": "not_configured",
        }

    try:
        # ★ 首选：**算材料接口**（`/v2/compute`）—— 它给的是**分阶段**的完整需求
        #   （等级桶 / 天赋桶 / 武器桶），正好对上我们的三个阶段。
        #
        # ⚠️ 起始状态**必须**是账号里的真实状态：本地那份"档案"来自公开图鉴，
        #    `level` 常常是 0（`base_level=5` 其实是稀有度）。拿 0 当起始等级去算，
        #    米游社直接回 `-500001`（实测），整个角色就报错了。
        #    所以顺序是：先问 `/v1/sync/avatar/detail`（真实等级 + 天赋 group_id + 武器），
        #    它说"没这个角色"或网络失败时，才退回本地档案。
        state = None
        profile = {}
        try:
            state = mys_calculator.fetch_avatar_state(character_id, uid=uid or _uid(),
                                                      server=server)
        except mys_api.MysError:
            state = None                      # 拿不到就退回本地档案，不影响后面兜底
        state_valid = bool(state and state.get("owned") and (
            int(state.get("level") or 0) > 0
            and (
                state.get("talents")
                or int(
                    (state.get("weapon") or {}).get("level")
                    or (state.get("weapon") or {}).get("level_current")
                    or 0
                ) > 0
            )
        ))
        if state_valid:
            profile = mys_calculator.avatar_from_state(state)
            # 顺带记住"这个号有他"——「添加角色」那个列表要靠它分「已拥有 / 未拥有」
            remember_ownership(character_id, True)
        elif state is not None:
            # `owned=True` with no real progress is an incomplete response,
            # not proof that the account does not own the character.
            if not state.get("owned"):
                remember_ownership(character_id, False)
            if current.get("known") and avatar:
                profile = avatar
        elif current.get("known") and avatar:
            profile = avatar
        computed = None
        if profile:
            computed = mys_calculator.compute_for_target(profile, target, uid=uid or _uid(),
                                                         server=server)
        if state_valid:
            result_current = _state_to_current(state)
        elif profile:
            result_current = current_state(
                character_id, target=target, avatars={character_id: profile}
            )
        else:
            result_current = None
        if computed and not computed.get("empty"):
            result = {
                "character_id": character_id,
                "requirements": computed.get("requirements") or [],
                "by_phase": computed.get("by_phase") or {},
                "body": computed.get("body") or {},
                "state": state or {},
                "current": result_current,
                "phase_note": "",
                "target_text": "",
                "error": "",
                "error_kind": "",
            }
        else:
            # 备选：养成方案接口。它**只覆盖天赋**（实测），但至少不会什么都不给。
            plan = mys_calculator.fetch_avatar_plan(character_id, uid=uid or _uid(),
                                                    server=server)
            requirements, by_phase = mys_calculator.plan_requirements(plan)
            if requirements:
                result = {
                    "character_id": character_id,
                    "requirements": requirements,
                    "by_phase": by_phase,
                    "body": {},
                    "plan": plan,
                    "state": state or {},
                    "phase_note": ("这份需求来自米游社的养成方案接口（"
                                   + (_plan_target_text(plan) or "目标未设")
                                   + "），**只覆盖天赋**：角色等级突破材料与经验书它不给。"),
                    "target_text": _plan_target_text(plan),
                    "error": "",
                    "error_kind": "",
                }
            elif not profile:
                result = {
                    "character_id": character_id, "requirements": [], "by_phase": {},
                    "error": (f"米游社没有给出角色 {character_id} 的材料需求，"
                              "本地也没有这个角色的档案。"),
                    "error_kind": "no_avatar",
                }
            else:
                result = {
                    "character_id": character_id,
                    "requirements": (computed or {}).get("requirements") or [],
                    "by_phase": (computed or {}).get("by_phase") or {},
                    "body": (computed or {}).get("body") or {},
                    "error": _EMPTY_HINT,
                    "error_kind": "empty_compute",
                }
    except mys_api.MysError as exc:
        result = {
            "character_id": character_id, "requirements": [], "by_phase": {},
            "error": str(exc), "error_kind": type(exc).__name__,
        }

    with _CACHE_LOCK:
        _CACHE["compute"][key] = {
            "at": datetime.datetime.now().timestamp(),
            "signature": signature,
            "result": result,
        }
    # 只有**真的问过米游社**（这次没走上面任何一条缓存）才落库 ——
    # 这样"当天只拉一次"跨重启也成立：关掉再打开时直接读这份结果。
    if error_free(result) and _daily_cache_enabled():
        _day_cache_store(character_id, signature, result)
    result["_cache_hit"] = False
    return result


def error_free(result):
    """这次算材料是不是**成功**的（成功才值得存下来给今天后面复用）。"""
    return not (result or {}).get("error") and bool((result or {}).get("requirements"))


# ==========================================
# 🌟 ④ 规划主入口
# ==========================================


def _uid():
    return str(getattr(config, "MYS_UID", "") or getattr(config, "DEFAULT_UID", "") or "")


def build_plan(path=None, db_path=None, inventory=None, avatars=None, force_compute=False,
               weekday=None, include_disabled=False, record=True, sync_if_empty=False,
               compute_budget_limit=None):
    """生成当前养成计划（**唯一的规划入口**）。

    返回的 dict 直接喂给 Studio 前端 / Agent 工具 / 数据库（`plans` + `plan_tasks`）。

    两条容易写错、但被测试盯着的规则：
      · **只有当前角色的缺口会转成 BetterGI 任务**（§16）—— 不能变成
        "所有角色先刷经验书、再刷武器材料、最后刷天赋"；
      · 当前角色的材料**认不出路线 / 全在冷却 / 秘境今天不开**时，不会假装没事，
        也不会跳过它：报告里给 `blockers`，`execution.fallback` 会指出
        "实在没事干时可以顺手做的别的角色任务"（§30 的最后一句要求）。
    """
    targets = ordered_targets(path=path, enabled_only=not include_disabled)
    if inventory is None:
        inventory = mys_inventory.load_inventory()
        if inventory is None and sync_if_empty:
            sync = mys_inventory.sync(path=path, db_path=db_path)
            inventory = sync.get("inventory")

    # 这里也只用缓存：**规划一次不该去拉"战绩"接口**（它被风控卡着，只会白等 + 刷警告）。
    # 每个角色的真实状态由 `requirements_for()` 问 `/v1/sync/avatar/detail` 拿到。
    avatars = avatars if avatars is not None else _cached_avatars()
    avatars = _merge_persisted_account_profiles(avatars)

    # 每个角色的当前状态 + 材料需求（每个"还没完成"的角色算一次，见下面的预算控制）。
    characters = []
    compute_errors = []
    deferred_characters = []
    if compute_budget_limit is None:
        budget_limit = int(getattr(config, "GROWTH_COMPUTE_MAX_PER_PLAN", 8) or 8)
    else:
        budget_limit = int(compute_budget_limit or 0)
    budget_limit = max(0, budget_limit)
    budget = budget_limit
    compute_network_used = 0
    compute_deferred = 0
    budget_used_up = False

    for target in targets:
        character_id = int(target.get("character_id") or 0)
        avatar = (avatars or {}).get(character_id) or {}
        current = current_state(character_id, target=target, avatars=avatars, path=path)
        complete = growth_models.is_complete(target, current)
        entry = {
            "target": target,
            "current": current,
            "complete": bool(complete),
            "avatar": avatar,
            "requirements": {"by_phase": {}},
            "compute_error": "",
            "computed": False,
        }

        if not complete:
            # 🎯 **有预算就给每个"还没完成"的角色算一次需求**（每个角色 1 次米游社请求，
            # 上限 `GROWTH_COMPUTE_MAX_PER_PLAN`，默认 8）。
            #
            # 为什么不是"只算当前角色"：材料是**共享池**（规格书 §17）—— 只算当前角色的话，
            # 页面上别的角色永远显示"不缺材料"，玩家看到的是假的完成度；而且"先把这个角色
            # 补完再下一个"的规则是**执行顺序**，不是"不许知道别人缺什么"。
            # 预算用完的角色会明确标注"本轮没算"，不会假装不缺（`compute_error`）。
            computed = requirements_for(
                target,
                avatar=avatar,
                force=force_compute,
                uid=_uid(),
                allow_network=not budget_used_up and budget > 0,
            )
            entry["requirements"] = computed
            entry["computed"] = not computed.get("deferred")
            entry["compute_error"] = computed.get("error") or ""
            # 缓存命中不占本轮网络预算；只有真的发起一次新的计算请求才扣 1。
            if not computed.get("_cache_hit") and not computed.get("deferred"):
                budget -= 1
                compute_network_used += 1
                if budget <= 0:
                    budget_used_up = True
            if computed.get("deferred"):
                compute_deferred += 1
                deferred_characters.append({
                    "character_id": character_id,
                    "character_name": target.get("character_name") or "",
                })
            # 算需求时顺带问到了账号里的真实状态，用它覆盖本地那份
            # （本地档案的 level 常常是 0，会让"还差多少"和进度条都算错）
            if computed.get("current"):
                entry["current"] = computed["current"]
                entry["complete"] = bool(
                    growth_models.is_complete(target, entry["current"]))
            if entry["compute_error"] and not computed.get("deferred"):
                compute_errors.append({
                    "character_id": character_id,
                    "character_name": target.get("character_name") or "",
                    "error": entry["compute_error"],
                    "kind": computed.get("error_kind") or "",
                })
        else:
            # 🎯 目标已经达成：**不问米游社**（省请求），也不该产出任何任务。
            # 这就是"90 → 90 必须合法、且不该报错"那条要求的落点（规格书 §10）。
            entry["requirements"] = {"character_id": character_id, "requirements": [],
                                     "by_phase": {}, "error": "", "error_kind": "",
                                     "already_complete": True}
            entry["computed"] = True
        characters.append(entry)

    # 库存是共享池：只给"要做的角色"扣减 —— 已完成的角色不该占用材料。
    # 但**只有当前角色**（第一个没完成的）算出来的需求会转成任务。
    active = [entry for entry in characters if not entry["complete"]]
    all_phase_rows = material_planner.plan_requirements(active, inventory=inventory)
    current_entry = next((entry for entry in characters if not entry["complete"]), None)
    current_id = int(current_entry["target"].get("character_id") or 0) if current_entry else 0
    current_rows = [row for row in all_phase_rows if row["character_id"] == current_id]
    # ★ 先读一次真实体力：趟数要按它算（读不到就是 available=False，退化成默认趟数）
    resin_state = mys_resin.probe()
    # Studio 详情要像米游社网页一样显示“所需为 0”的已齐材料；执行器会再按缺口过滤。
    tasks = material_planner.build_tasks(current_rows, weekday=weekday, include_completed=True,
                                         resin_state=resin_state)

    inspection = _inspect_current(current_rows, current_entry, characters)
    fallback = _fallback_tasks(tasks, characters, all_phase_rows, current_id, weekday=weekday,
                               resin_state=resin_state)

    status = _plan_status(characters, tasks, current_entry, compute_errors,
                          inspection, compute_deferred)
    command = material_planner.tasks_to_bgi_command(fallback["tasks"])
    summary = material_planner.summarise(current_rows, tasks)
    inventory_status = _status_of(inventory)

    plan = {
        "generated_at": growth_models.business_now().strftime("%Y-%m-%dT%H:%M:%S"),
        "status": status,
        "status_label": growth_models.state_label(status),
        "sort_mode": sort_mode(path=path),
        "current_character_id": current_id,
        "current_character_name": (current_entry["target"].get("character_name") or "") if current_entry else "",
        "current_phase": inspection["phase"],
        "current_phase_label": growth_models.phase_label(inspection["phase"]) if inspection["phase"] else "",
        "characters": [_character_view(entry, all_phase_rows) for entry in characters],
        "phases": all_phase_rows,
        "tasks": tasks,
        "bettergi_cmd": command,
        "execution": {
            "tasks": fallback["tasks"],
            "fallback": fallback["fallback"],
            "note": fallback["note"],
        },
        "summary": summary,
        "inventory": inventory_status,
        # ★ 养成计算器告诉我们的「你已有多少」（`available_material`）。
        #   米游社只给它认得的那几种材料，所以这是个**部分**清单 —— 但它是真的，
        #   而且每次算材料都会刷新。界面拿它代替"库存未同步"那种吓人的说法。
        "known_inventory": _known_inventory(characters),
        "compute_errors": compute_errors,
        "compute_deferred": deferred_characters,
        "compute_budget": {
            "limit": budget_limit,
            "used": compute_network_used,
            "deferred": compute_deferred,
        },
        "missing_overview": material_planner.phase_missing(all_phase_rows),
        "resin": material_planner.resin_estimate(current_rows),
        # ★ 预计培养时间（见 brain/resin_math.py）：把缺口按体力产出比例折成"还要几天"。
        #   玩家要的是这个数，而不是"缺 62 个幻造晶鳞石"。
        "estimate": resin_math.estimate(all_phase_rows),
        # 当前真实体力（读不到就是 available=False + 原因，绝不当成 0）
        "current_resin": resin_state,
        # ★ 执行模式 & 分批次队列状态（`all` / `stepwise`，见 brain/execution_queue.py）
        "execution_mode": execution_queue.mode(),
        "execution_mode_label": execution_queue.MODE_LABELS.get(execution_queue.mode(), ""),
        # 用 `preview()`（不改落库状态）：显示"按**现在这份计划**排出来的队列"，
        # 而不是上次存下的那份（否则会出现"列表 3 条新路线、当前那条是旧 Boss"）
        "execution_queue": execution_queue.preview((fallback or {}).get("tasks") or []),
        "today_lines": material_planner.today_plan_lines(current_rows, tasks),
        "blockers": _blockers(status, current_rows, tasks, compute_errors,
                              inventory_status, compute_deferred),
    }

    if record:
        _record_plan(plan, path=path, db_path=db_path)
    return plan


def _inspect_current(current_rows, current_entry, characters):
    """当前角色"卡在哪一步"。

    ⚠️ 取的是**阶段优先级里第一个还没完成的阶段**（`plan_requirements` 已经按优先级产出），
    **不是** `tasks[0].phase` —— 任务是按"可执行优先"排过序的，拿它当"当前阶段"会在
    "天赋优先但今天不开本"时显示成天赋，而玩家看到的执行动作其实是角色等级。
    """
    if current_entry is None:
        return {"phase": "", "reason": "complete" if characters else "empty"}
    open_rows = [row for row in current_rows if not row.get("skipped") and not row.get("complete")]
    if not open_rows:
        return {"phase": "", "reason": "all_phases_done"}
    return {"phase": open_rows[0]["phase"], "reason": "open"}


def _fallback_tasks(tasks, characters, all_phase_rows, current_id, weekday=None, resin_state=None):
    """当前角色的**体力目标做完了**（或没得跑）时，顺手做下个角色的。

    玩家的要求分两种情况，这里一起处理：

      ① 「如果体力刷取目标都完成了，就去按优先级刷取下个角色的体力目标
         （在上个角色还存在采集物或者普通魔物掉落物缺失情况下）」——
         也就是：采集 / 魔物掉落**不耗体力**，可以和后面对并行；
         但**吃体力的**那些（秘境 / 地脉 / 首领）一旦当前角色排不出来，
         就该把体力花在下个角色身上，别让体力溢出。

      ② 「当前培养角色的体力刷取目标没有开（副本日期数不对）→ 也先刷下个角色…
         等到日期再回来补刷」—— 秘境今天不开就走同一条路（`closed_today` 不算可执行）。

    ⚠️ 当前角色**还有**可执行的体力目标时绝不启用（先把当前角色补完）；
    而且当前角色的非体力任务（采集 / 魔物）始终保留，不会因为去帮别人而丢掉。
    """
    runnable = [task for task in tasks if task["status"] == material_planner.STATUS_RUNNABLE
                and int(task.get("missing") or 0) > 0 and not task.get("skipped_this_round")]
    energy_kinds = (material_planner.TASK_DOMAIN, material_planner.TASK_LEYLINE,
                    material_planner.TASK_BOSS)
    own_energy = [task for task in runnable if task["task_type"] in energy_kinds]
    own_free = [task for task in runnable if task["task_type"] not in energy_kinds]
    # 打个标记：队列/推送要按"当前角色优先"排（见 execution_queue.build_steps）
    for task in own_energy + own_free:
        task["owner_current"] = True

    if own_energy:
        return {"tasks": runnable, "fallback": False, "note": ""}

    # 当前角色没有可执行的**体力**目标了：看看后面的角色有没有
    others = [row for row in all_phase_rows
              if row["character_id"] != current_id and not row.get("skipped")]
    other_energy, other_free = [], []
    if others:
        other_tasks = material_planner.build_tasks(others, weekday=weekday, include_completed=True,
                                                  resin_state=resin_state)
        other_runnable = [task for task in other_tasks
                          if task["status"] == material_planner.STATUS_RUNNABLE
                          and int(task.get("missing") or 0) > 0
                          and not task.get("skipped_this_round")]
        other_energy = [task for task in other_runnable if task["task_type"] in energy_kinds]
        # 不耗体力的（采集 / 魔物）照做 —— 它们和"体力花在谁身上"没有冲突
        other_free = [task for task in other_runnable if task["task_type"] not in energy_kinds]
        # 体力一次只能花在一个地方：只接**最高优先级**那条体力线，其余留给下一轮
        if other_energy:
            best = material_planner.task_priority(other_energy[0])
            for task in other_energy:
                best = min(best, material_planner.task_priority(task))
            other_energy = [task for task in other_energy
                            if material_planner.task_priority(task) == best]

    if not other_energy and not other_free:
        # 谁都没有可做的 → 保留当前角色的采集 / 魔物（不耗体力）任务
        return {"tasks": own_free, "fallback": False, "note": ""}

    # ★ 当前角色**还有自己的采集 / 魔物任务**时，只把别的角色的**体力目标**接进来
    #   （那些不吃体力、插进来只会让"今天先做谁"变糊）。实在什么都没有了，才连别人的
    #   采集一起去刷 —— 那是规格书 §30 的兜底，不能丢。
    #   踩过的表现：奥黛塔缺「幻造晶鳞石」，队列第一条却是**别人角色**的「嘟嘟莲」。
    extra_free = [] if own_free else other_free
    reason = _fallback_reason(tasks)
    picked = other_energy[0] if other_energy else (extra_free[0] if extra_free else None)
    where = (f"优先 {material_planner.priority_label(picked)}" if picked else "")
    return {
        # 当前角色的**采集 / 魔物**照做（不耗体力），体力花在下个角色身上
        "tasks": own_free + other_energy + extra_free,
        "fallback": True,
        "note": (f"{reason}所以这轮改做后面角色的可执行材料（{where}）——"
                 f"当前角色的目标不会被跳过，等它能跑了立刻回来接着做。"),
    }


def _fallback_reason(tasks):
    """为什么当前角色排不出体力目标 —— 用最贴切的那条原因（别写含糊的套话）。"""
    closed = [task for task in tasks
              if task["status"] == material_planner.STATUS_CLOSED_TODAY
              and task["task_type"] in (material_planner.TASK_DOMAIN,)]
    if closed:
        return f"当前角色的秘境今天不开放（{closed[0].get('status_note') or '日程不符'}），"
    no_resin = [task for task in tasks if task.get("skipped_this_round")]
    if no_resin:
        return "当前角色的体力目标这一轮排不上（体力不够分），"
    return "当前角色的材料今天拿不到（秘境未开放 / 路线冷却 / 缺路线），"


def _known_inventory(characters):
    """把每个角色需求里"已知已有数量"的材料汇总成一份清单（给界面显示）。

    数据来自养成计算器的 `available_material`（`mys_calculator.parse_available_material`）：
    **只有米游社认得的那几种材料**会出现，其余是"不知道"。所以这里如实标出总数，
    界面照实说"其中 N 种知道你已有多少"，不假装这是完整背包。
    """
    merged = {}
    for entry in characters or ():
        for row in (entry.get("requirements") or {}).get("requirements") or ():
            if row.get("owned") is None:
                continue
            item_id = int(row.get("item_id") or 0)
            if not item_id:
                continue
            current = merged.setdefault(item_id, {
                "item_id": item_id,
                "item_name": row.get("item_name") or "",
                "owned": 0,
                "required": 0,
                "lack": 0,
                "characters": [],
            })
            current["owned"] = max(current["owned"], int(row["owned"]))
            current["required"] = max(current["required"], int(row.get("required") or 0))
            current["lack"] = max(current["lack"], int(row.get("lack") or 0))
            name = entry.get("target", {}).get("character_name") or ""
            if name and name not in current["characters"]:
                current["characters"].append(name)
    items = sorted(merged.values(), key=lambda row: (-row["owned"], row["item_id"]))
    if items:
        # 落一份盘：概览页/其它地方不用重算就能知道"哪些材料的已有数量是已知的"
        try:
            mys_inventory.save_known_materials(items)
        except Exception:               # noqa: BLE001 —— 写盘失败不影响这次规划
            pass
    return {
        "count": len(items),
        "items": items,
        "source": "养成计算器（available_material）",
        "note": ("「已有 / 还差」由养成计算器提供 —— 它只给**部分**材料的已有数量，"
                 "没给的那些按「所需」计（宁多刷不少刷）。"),
    }


def _status_of(inventory):
    """从手上这份库存（可能是调用方传进来的）算状态，**不去读盘**。

    ⚠️ `stale` 必须按这份数据自己的时间戳算：写死 False 会让"用一份两天前的库存做规划"
    看起来一切正常 —— 那正是最需要提示玩家的场景（§5「禁止把缓存伪装成实时数据」）。
    """
    if not inventory:
        return {
            "configured": mys_api.cookie_configured(), "available": False, "stale": True,
            "fetched_at": "", "age_text": "从未同步", "item_count": 0,
            # ⚠️ 措辞很重要：老背包接口没了**不代表**缺口算不出来 ——
            #    「已有 / 还差」现在走养成计算器的 `available_material`（见 known_inventory）。
            #    以前这里写"缺口算不出来"，界面上看着像整个功能坏了。
            "hint": ("「已有 / 还差」由养成计算器提供（每次算材料都会刷新），"
                     "不依赖已下线的背包接口。"),
        }
    fetched_at = inventory.get("fetched_at") or ""
    stale = mys_inventory.is_stale(fetched_at)
    return {
        "configured": mys_api.cookie_configured(),
        "available": True,
        "stale": stale,
        "fetched_at": fetched_at,
        "age_text": mys_inventory.describe_age(fetched_at),
        "item_count": len(inventory.get("items") or {}),
        "uid": inventory.get("uid") or "",
        "server": inventory.get("server") or "",
        "stale_hours": getattr(config, "GROWTH_INVENTORY_STALE_HOURS", 12),
        "hint": (
            f"这份不是实时数据（{mys_inventory.describe_age(fetched_at)}同步的），"
            "材料缺口可能已经不准，建议先同步。"
            if stale else ""
        ),
    }


def _character_view(entry, phase_rows):
    """角色在计划里的展示行（Studio 的列表就用它）。"""
    target = entry["target"]
    character_id = int(target.get("character_id") or 0)
    rows = [row for row in phase_rows if row["character_id"] == character_id]
    pending = bool(entry.get("compute_error") or not entry.get("computed"))
    total = None if pending else sum(row["missing_total"] for row in rows)
    return {
        "character_id": character_id,
        "character_name": target.get("character_name") or "",
        "rarity": int(target.get("rarity") or 0),
        "element": target.get("element") or "",
        "enabled": bool(target.get("enabled")),
        "priority": int(target.get("priority") or 0),
        "target": target,
        "current": entry["current"],
        "target_summary": growth_models.target_summary(target),
        "current_summary": growth_models.current_summary(entry["current"]),
        "complete": bool(entry["complete"]),
        "compute_error": entry.get("compute_error") or "",
        "phases": [
            {
                "phase": row["phase"],
                "phase_label": row["phase_label"],
                "complete": row["complete"],
                "skipped": row["skipped"],
                "missing_total": row["missing_total"],
                "materials": row["materials"],
            }
            for row in rows
        ],
        "missing_total": total,
        "compute_pending": pending,
        "progress": _progress(entry, character_id, phase_rows),
    }


def _progress(entry, character_id, phase_rows):
    """三个阶段各自的完成度百分比（规格书 §29 的进度条）。"""
    target = entry["target"]
    current = entry["current"]
    talents = current.get("talents") or {}
    rows = {row["phase"]: row for row in phase_rows if row["character_id"] == character_id}

    def ratio(now_value, goal_value, phase):
        if rows.get(phase, {}).get("skipped"):
            return None
        goal = int(goal_value or 0)
        if goal <= 0:
            return 100
        return max(0, min(100, int(round(int(now_value or 0) * 100 / goal))))

    return {
        growth_models.PHASE_CHARACTER_LEVEL: ratio(
            current.get("level"), target.get("level_target"), growth_models.PHASE_CHARACTER_LEVEL),
        growth_models.PHASE_WEAPON_LEVEL: ratio(
            current.get("weapon_level"), target.get("weapon_level_target"),
            growth_models.PHASE_WEAPON_LEVEL),
        growth_models.PHASE_TALENT: int(round(sum([
            ratio(talents.get("normal"), target.get("normal_target"), growth_models.PHASE_TALENT) or 0,
            ratio(talents.get("skill"), target.get("skill_target"), growth_models.PHASE_TALENT) or 0,
            ratio(talents.get("burst"), target.get("burst_target"), growth_models.PHASE_TALENT) or 0,
        ]) / 3)),
    }


def _plan_status(characters, tasks, current_entry, compute_errors, inspection=None,
                 compute_deferred=0):
    """计划整体处于状态机的哪一步（规格书 §20）。"""
    if not characters:
        return growth_models.STATE_IDLE
    if current_entry is None:
        return growth_models.STATE_COMPLETE
    runnable = [task for task in tasks
                if task["status"] == material_planner.STATUS_RUNNABLE
                and int(task.get("missing") or 0) > 0]
    if runnable:
        return growth_models.STATE_WAIT_CONFIRM
    if tasks:
        # 有缺口、但今天一个都跑不了（冷却 / 未开放 / 缺路线）
        return growth_models.STATE_BLOCKED
    if compute_errors or compute_deferred:
        return growth_models.STATE_BLOCKED
    return growth_models.STATE_WAIT_CONFIRM


def _blockers(status, phase_rows, tasks, compute_errors, inventory_status,
              compute_deferred=0):
    """把"为什么没得跑"讲清楚 —— 玩家最常问的就是这个（§32）。"""
    blockers = []
    if inventory_status.get("stale"):
        blockers.append({
            "kind": "inventory_stale",
            "message": (inventory_status.get("hint")
                        or "米游社库存不是最新的，材料缺口可能已经不准（建议先同步）。"),
        })
    for error in compute_errors:
        blockers.append({
            "kind": "compute_failed",
            "character_id": error["character_id"],
            "message": f"{error['character_name'] or error['character_id']} 的材料需求算不出来：{error['error']}",
        })
    if compute_deferred:
        blockers.append({
            "kind": "compute_budget",
            "message": (f"本轮材料计算预算已用完，还有 {compute_deferred} 个角色待计算；"
                        "这不代表它们的材料已经齐了。"),
        })
    waiting = [task for task in tasks if task["status"] == material_planner.STATUS_WAITING_ROUTE]
    for task in waiting[:5]:
        blockers.append({
            "kind": "waiting_route",
            "material": task["material"],
            "message": f"「{task['material']}」缺少 BetterGI 可执行路线（不要猜路线，先补脚本组）。",
        })
    cooldown = [task for task in tasks if task["status"] == material_planner.STATUS_COOLDOWN]
    for task in cooldown[:5]:
        blockers.append({
            "kind": "cooldown",
            "material": task["material"],
            "message": task.get("status_note") or f"「{task['material']}」的路线还在冷却。",
        })
    closed = [task for task in tasks if task["status"] == material_planner.STATUS_CLOSED_TODAY]
    for task in closed[:5]:
        blockers.append({
            "kind": "closed_today",
            "material": task["material"],
            "message": task.get("status_note") or f"「{task['material']}」的秘境今天不开放。",
        })
    if status == growth_models.STATE_BLOCKED and not blockers:
        blockers.append({"kind": "unknown", "message": "当前没有可执行的任务，等下一轮同步后再看。"})
    return blockers


def _record_plan(plan, path=None, db_path=None):
    """把计划写进 growth.db（`plans` + `plan_tasks`），失败只提示。"""
    try:
        plan_id = growth_db.create_plan(
            status=plan["status"],
            current_character_id=plan["current_character_id"],
            current_phase=plan["current_phase"],
            summary=f"{plan['status_label']}｜{plan['summary'].get('runnable', 0)} 个可执行任务",
            detail={"tasks": plan["tasks"], "summary": plan["summary"],
                    "missing_overview": plan["missing_overview"]},
            path=db_path,
        )
        growth_db.replace_plan_tasks(plan_id, [
            {
                "character_id": task["character_id"],
                "phase": task["phase"],
                "task_type": task["task_type"],
                "material": task["material"],
                "item_id": task["item_id"],
                "required": task["required"],
                "missing": task["missing"],
                "status": task["status"],
                "note": task.get("status_note") or task.get("route") or "",
            }
            for task in plan["tasks"]
        ], path=db_path)
        plan["plan_id"] = plan_id
    except Exception as exc:            # noqa: BLE001 —— 记账失败不该让页面打不开
        print(f"⚠️ 养成计划落库失败（不影响本次规划）：{type(exc).__name__} {exc}")
        plan["plan_id"] = 0


# ==========================================
# 🌟 ⑤ 同步 / 执行后重算（§19 的核心闭环）
# ==========================================


def sync_inventory(path=None, db_path=None, uid=None):
    """同步米游社库存并让缓存失效。返回 `mys_inventory.sync()` 的结果。

    ⚠️ 这条走的是**已经下线的背包接口**，真实环境里永远是 `skipped=True`：
    材料的「已有 / 还差」现在由养成计算器在算材料时一起给（`known_inventory`）。
    所以"跳过"这种情况**不打印警告** —— 否则每次启动都吓人一跳。
    """
    result = mys_inventory.sync(uid=uid or _uid(), path=path, db_path=db_path)
    if result.get("ok"):
        invalidate_cache()
        _record_requirements(result)
    elif not result.get("skipped"):
        print(f"⚠️ 米游社库存同步失败：{result.get('error')}")
    return result


def _record_requirements(sync_result):
    """把每个角色的材料需求落一份到 `material_requirements`（§25）。"""
    try:
        inventory = sync_result.get("inventory")
        for target in growth_db.list_targets(enabled_only=True):
            computed = requirements_for(target)
            if computed.get("error"):
                continue
            rows = []
            for phase, entries in (computed.get("by_phase") or {}).items():
                for entry in entries or ():
                    required = int(entry.get("required") or 0)
                    owned = mys_inventory.owned(entry.get("item_id"), model=inventory)
                    rows.append({
                        "phase": phase,
                        "item_id": entry.get("item_id"),
                        "item_name": entry.get("item_name") or entry.get("name"),
                        "required": required,
                        "owned": min(required, owned),
                        "missing": max(0, required - owned),
                    })
            if rows:
                growth_db.replace_requirements(target["id"], rows)
    except Exception as exc:            # noqa: BLE001
        print(f"⚠️ 材料需求落库失败（不影响规划）：{type(exc).__name__} {exc}")


def after_task_sync(path=None, db_path=None, plan_id=0, character_id=0, phase="",
                    status="FINISHED", result="", uid=None):
    """BetterGI 任务结束后调用：**重新同步米游社 → 重新计算缺口**（§19）。

    ⚠️ 这是整个系统的核心机制：BetterGI 拾取了多少永远不可信，
    所以这里只把"跑完了"当成一次触发信号，真实结果一律重新从米游社读。
    """
    started = growth_models.business_now().strftime("%Y-%m-%dT%H:%M:%S")
    if not getattr(config, "GROWTH_POST_TASK_SYNC", True):
        return {
            "ok": True,
            "skipped": True,
            "reason": "GROWTH_POST_TASK_SYNC=0（配置里关掉了任务后自动同步）",
        }

    sync_result = sync_inventory(path=path, db_path=db_path, uid=uid)
    plan = build_plan(path=path, db_path=db_path)
    try:
        growth_db.record_execution(
            plan_id=plan_id, character_id=character_id, phase=phase, status=status,
            result=result or "BetterGI 任务结束，已重新同步米游社并重算缺口",
            started_at=started, path=db_path,
        )
    except Exception as exc:            # noqa: BLE001
        print(f"⚠️ 执行历史落库失败：{type(exc).__name__} {exc}")

    return {
        "ok": True,
        "sync": {
            "ok": bool(sync_result.get("ok")),
            "error": sync_result.get("error") or "",
            "item_count": sync_result.get("item_count") or 0,
            "delta": (sync_result.get("delta") or [])[:10],
        },
        "plan": plan,
        "note": (
            "已重新同步米游社库存并重算缺口（BetterGI 的拾取数量从不写回库存）。"
            if sync_result.get("ok") else
            f"重新同步失败：{sync_result.get('error')}；缺口仍按上一次成功快照计算。"
        ),
    }


# ==========================================
# 🌟 ⑥ 与 BetterGI 的联动（"执行完必须重新同步"这条规则的落点）
# ==========================================
#
# 触发点是 `skills/bgi_watcher.start_completion_watch` 的收尾通知：
# 那条链路本来就能正确判断"这一轮到底跑完没有"（跨天换日志、取消竞态、
# 多配置组、原神退出的兜底静默都在里面）。养成系统只是订阅它，
# **不再自己写一套"跑完了没"的判断** —— 那一定是两套判定互相打架。
_HOOK_LOCK = threading.Lock()
_LAST_EXECUTION = {}


def register_watcher_hook():
    """把"BetterGI 跑完 → 重新同步米游社 → 重算缺口"挂到完成监视线程上（幂等）。"""
    from skills import bgi_watcher

    bgi_watcher.register_completion_hook(_on_bettergi_finished)
    return _on_bettergi_finished


def _on_bettergi_finished(open_id="CLI_USER"):
    """完成回调：**只做"重新同步 + 重算"**，不写任何库存数字。

    ★ 分批次模式（`GROWTH_EXECUTION_MODE=stepwise`）下，这里还会**推进队列并推送下一条**
    —— 玩家要的就是"跑完一条自动问下一条"。注意：**不能**用"材料还缺不缺"来判断这条
    路线成不成功（米游社的已有/还差是算出来的，跑完那一刻背包还没刷新），
    所以推进只看"任务真的结束了"。
    """
    try:
        result = after_task_sync(
            plan_id=_LAST_EXECUTION.get("plan_id", 0),
            character_id=_LAST_EXECUTION.get("character_id", 0),
            phase=_LAST_EXECUTION.get("phase", ""),
            status="FINISHED",
            result="BetterGI 任务结束（由完成监视触发）",
        )
        summary = result.get("sync") or {}
        if summary.get("ok"):
            print(f"🔄 养成系统已重新同步米游社库存（{summary.get('item_count')} 种材料），"
                  f"并重算材料缺口。")
            for row in (summary.get("delta") or [])[:5]:
                sign = "+" if row["delta"] > 0 else ""
                print(f"   · {row['name']}：{row['before']} → {row['after']}（{sign}{row['delta']}）")
        else:
            print(f"⚠️ 养成系统重新同步米游社失败：{summary.get('error')}"
                  f"（缺口仍按上一次成功快照计算）")
        _advance_stepwise_queue()
        return result
    except Exception as exc:            # noqa: BLE001 —— 联动失败不能影响监视线程
        print(f"⚠️ BetterGI 完成后的养成联动失败：{type(exc).__name__} {exc}")
        return None


def _advance_stepwise_queue(notify=True):
    """分批次：把队列往后推一格，并把下一条路线推给玩家（没下一条就收尾）。"""
    if not execution_queue.is_stepwise():
        return None
    state, step = execution_queue.advance("finished")
    lines = execution_queue.render_current(state) if step else ["✅ 这一轮的路线都跑完了，辛苦了！"]
    text = "\n".join(lines)
    print("\n" + text)
    if notify:
        try:
            _push_growth_notice(text)
        except Exception as exc:        # noqa: BLE001
            print(f"⚠️ 推送下一条路线失败（{type(exc).__name__}: {exc}）")
    return {"state": state, "step": step, "text": text}


def _resolve_push_target(open_id=""):
    """这次推送该发给谁（显式给了就用它；否则从记忆里找"上次说话的会话"）。"""
    target = str(open_id or "").strip()
    if target:
        return target
    try:
        # ⚠️ 必须在这里 import：模块顶部没导入 `memory_manager`（避免循环导入），
        #    漏了这一行会抛 NameError → 被调用方的 except 吞掉 → 推送**永远发不出去**
        #    而只剩一行日志，正是玩家反馈的"QQ 收不到"（测试抓出来过）。
        from brain import memory_manager

        store = memory_manager.load_chat_store()
        target = str(store.get("last_target") or store.get("open_id") or "").strip()
        if not target:
            # 兜底：以前的待审批任务里存着会话标识（`qq:c2c:xxx#msg`）——
            # 老版本没有 `last_target` 这个键，靠它也能把目标找回来。
            pending = store.get("pending_task") or {}
            target = str(pending.get("open_id") or "").strip()
        return target
    except Exception as exc:                # noqa: BLE001
        print(f"⚠️ 取推送目标失败（{type(exc).__name__}: {exc}）")
        return ""


def _push_growth_notice(text, open_id=""):
    """把一段话**主动推**给玩家（走现有通知链路）。

    ⚠️ 这就是玩家反馈的那个坑：展柜那条路"本地思考完成后 QQ 机器人收不到"。
    原因不是链路坏了，而是**根本没人调它** —— 本地路径直接把答案 `print` 出来就结束了。
    所以这里显式走 `bgi_controller.send_notice`（它会按通道规则发到 QQ / 飞书），
    并且把 `open_id` 一路传下去；没有目标时只打日志，绝不假装推过了。
    """
    target = _resolve_push_target(open_id)
    if not target:
        print("（没有推送目标 —— 玩家还没跟 Agent 说过话，这条只留在日志里）")
        return False
    try:
        from skills import bgi_controller

        # progress=False + chat=True：这是**需要玩家看到并回话**的消息，不该被"静默"规则吞掉
        sent = bool(bgi_controller.send_notice(target, text, progress=False, chat=True))
        if sent:
            _LAST_PUSH_TARGET["value"] = target
        return sent
    except Exception as exc:                # noqa: BLE001
        print(f"⚠️ 推送失败（{type(exc).__name__}: {exc}）")
        return False


def push_execution_targets(open_id="", record=False, path=None, db_path=None):
    """把"今天要执行什么"推给玩家（**启动 Agent 和分批次推进时都走它**）。

    为什么每次启动都要推（玩家明确要求）：米游社那边的"已有 / 还差"是**算出来**的，
    BetterGI 跑完那一刻背包还没刷新，所以"今天该跑什么"不能靠玩家自己记 ——
    必须每次启动都把执行目标摊到玩家脸上。

    两种执行模式的文案不一样：
      · `all`      → 这一轮的目标清单 + 预计培养天数；
      · `stepwise` → 先给总路线详情，再直接问第一条路线（回 y 就跑它）。
    """
    plan = build_plan(path=path, db_path=db_path, record=record)
    tasks = (plan.get("execution") or {}).get("tasks") or []
    lines = ["📋 今天的执行目标"]
    if plan.get("current_character_name"):
        lines.append(f"👉 当前角色：{plan['current_character_name']}｜{plan.get('current_phase_label') or '—'}")
    estimate = plan.get("estimate") or {}
    if estimate.get("text"):
        lines.append(f"⏳ {estimate['text']}")
    for line in resin_math.describe(estimate)[1:]:
        lines.append(line)

    if execution_queue.is_stepwise():
        lines.append("")
        lines.extend(execution_queue.render_steps({"execution": {"tasks": tasks}}))
        # ★ 用 `sync()` 而不是 `load() + start()`：队列是落库的，直接用上次存下的那份
        #   会出现"路线列表是新的、当前那条却是旧的"（实测：列表 3 条新路线，
        #   当前那条还写着「读不到体力、160 体力」）。sync 会按路线集合判断要不要重建。
        state = execution_queue.sync(tasks, plan_id=plan.get("plan_id") or 0,
                                     character_id=plan.get("current_character_id") or 0)
        lines.append("")
        lines.extend(execution_queue.render_current(state))
    else:
        if not tasks:
            lines.append("（这一轮没有可执行的任务：材料已满足 / 没路线 / 都在冷却）")
        for task in tasks[:10]:
            head = f"· {task.get('priority_label') or ''}｜{task.get('task_label')}｜{task.get('material')}"
            if task.get("route") and task["route"] != task["material"]:
                head += f" → {task['route']}"
            if task.get("resin"):
                head += f"（{task.get('count')} 趟 / {task.get('resin')} 体力）"
            lines.append(head)
        waiting = [task for task in tasks if task.get("status") == "waiting_route"]
        if waiting:
            lines.append(f"（{len(waiting)} 种材料没有可执行路线，本轮跳过）")
        skipped = [task for task in tasks if task.get("count") == 0 and task.get("missing")]
        if skipped:
            lines.append(f"（{len(skipped)} 个目标这轮体力不够 / 打不开，下一轮再排）")

    text = "\n".join(lines)
    print("\n" + text)
    # 这次要发给谁：先解析出来（**别复用上一次推送的目标** —— 残留值会让这次推送失败时
    # 仍然挂上一个审批，玩家就莫名其妙看到"待确认"）
    target = _resolve_push_target(open_id)
    pushed = _push_growth_notice(text, open_id=target)
    if execution_queue.is_stepwise():
        # 只有**真的推出去**才挂审批：推不出去却挂了审批，玩家回 y 会跑一条他根本没看到的路线
        _arm_step_approval(pushed_target=target if pushed else "", plan=plan)
    return {"ok": True, "pushed": pushed, "text": text, "mode": execution_queue.mode(),
            "tasks": len(tasks)}


# 最近一次"真的推出去了"的目标（审批挂到那个会话上）
_LAST_PUSH_TARGET = {"value": ""}


def _last_push_target():
    return _LAST_PUSH_TARGET.get("value") or ""


def _arm_step_approval(pushed_target, plan=None):
    """分批次：把**当前这一条路线**挂成"待审批任务"，玩家回 y 就跑它。

    为什么必须挂：玩家要的流程是"先推总路线详情 → 问第一条跑不跑 → 回 y 就跑"。
    QQ / 飞书那套审批是看 `store["pending_task"]` 的 —— 不挂进去，玩家回 y 时
    `agent_router` 只会回一句"现在没有待确认的计划"，流程直接断掉。

    ⚠️ 只挂**当前这一条**（不是整批）：回 y 执行的就是它，跑完完成监视会推下一条。
    """
    target = str(pushed_target or "").strip()
    if not target:
        return False
    step = execution_queue.current()
    if not step or not step.get("command"):
        return False
    try:
        from brain import memory_manager

        store = memory_manager.load_chat_store()
        store["pending_task"] = {
            "bgi_cmd": step.get("command") or {},
            "open_id": target,
            "uid": str(store.get("uid") or ""),
            "growth_step": {"index": step.get("index"), "material": step.get("material"),
                            "route": step.get("route")},
        }
        memory_manager.save_chat_store(store)
        print(f"▶️ 已把「第 {step.get('index')} 条路线：{step.get('material')}」挂成待确认任务"
              f"（回 y 就跑它）")
        return True
    except Exception as exc:                # noqa: BLE001 —— 挂审批失败不该让推送失败
        print(f"⚠️ 挂分批次审批失败（{type(exc).__name__}: {exc}）")
        return False


def note_execution(plan_id=0, character_id=0, phase=""):
    """记下"这次执行的是哪份计划的哪个角色阶段"，供完成回调写执行历史。"""
    _LAST_EXECUTION.update({
        "plan_id": int(plan_id or 0),
        "character_id": int(character_id or 0),
        "phase": str(phase or ""),
    })
    return dict(_LAST_EXECUTION)


def last_execution():
    return dict(_LAST_EXECUTION)


# ==========================================
# 🌟 给 Studio / Agent 用的文本视图
# ==========================================


def plan_lines(plan, limit=14):
    """把计划渲染成人类可读的行（终端、审批屏、LLM 工具返回都用它）。"""
    plan = plan or {}
    lines = [f"📋 养成计划：{plan.get('status_label') or '—'}"
             f"（排序：{growth_models.SORT_MODE_LABELS.get(plan.get('sort_mode'), '')}）"]
    inventory = plan.get("inventory") or {}
    if inventory.get("available"):
        stamp = f"{inventory.get('age_text')}同步（{inventory.get('fetched_at')}）"
        lines.append(("⚠️ 库存" if inventory.get("stale") else "✅ 库存") + f"：{stamp}，{inventory.get('item_count')} 种材料")
    else:
        lines.append("⚠️ 库存：还没同步过米游社（材料缺口无法计算）")

    if plan.get("current_character_name"):
        lines.append(f"👉 当前：{plan['current_character_name']}｜{plan.get('current_phase_label') or '—'}")
    elif plan.get("status") == growth_models.STATE_COMPLETE:
        lines.append("🎉 全部角色的目标都已完成。")

    for task in (plan.get("tasks") or [])[: int(limit)]:
        head = f"· [{task['status_label']}] {task['task_label']}｜{task['material']}"
        if task.get("route") and task["route"] != task["material"]:
            head += f" → {task['route']}"
        head += f"（缺 {task['missing']:,}）"
        lines.append(head)
        if task.get("status_note"):
            lines.append(f"    ↳ {task['status_note']}")

    summary = plan.get("summary") or {}
    if summary:
        lines.append(
            f"合计：可执行 {summary.get('runnable', 0)} 个 / 受阻 {summary.get('blocked', 0)} 个，"
            f"预计体力 {summary.get('resin', 0)}（仅规划参考）"
        )
    return lines


def status_text(plan=None, path=None, db_path=None):
    """一行摘要（Agent 工具 / 首页卡片）。"""
    plan = plan if plan is not None else build_plan(path=path, db_path=db_path, record=False)
    return (
        f"{plan.get('status_label')}｜当前角色：{plan.get('current_character_name') or '无'}"
        f"｜阶段：{plan.get('current_phase_label') or '—'}"
        f"｜可执行任务：{(plan.get('summary') or {}).get('runnable', 0)}"
    )
