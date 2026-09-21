"""体力（原粹树脂）：从米游社「每日便笺」读**真实**体力，用来决定今天到底刷几次。

**为什么必须有这个模块**：以前副本 / 地脉的趟数是**硬编码**的
（`DOMAIN_RUNS_PER_ROUND = 2`、`LEYLINE_RUNS_PER_ROUND = 6`），跟玩家身上有多少体力
毫无关系 —— 体力只剩 20 也照排 6 趟地脉，等于排了一个跑不完的计划（被反馈过：
"默认消耗是 40 点体力…要按当前读取到的体力换算次数"）。

**⚠️ 这个接口要的是 app 版 cookie**（v1 `ltoken` + `ltuid` + `cookie_token`，
也就是扫码登录给的那一套）。养成计算器那套 v2 cookie（`ltoken_v2` 等）打它会回
「账号数据异常」（实测），所以两种情况都要如实上报，不能假装读到了：

    · 读到了        → `available=True`，给出 `current` / `max`
    · cookie 不对   → `available=False`，`reason` 写明原因，规划退化成"按默认趟数"
    · 风控 / 网络   → 同上（**不重试、不猜体力值**）

接口：
    GET {MYS_RECORD_HOST}/game_record/app/genshin/api/dailyNote?server=&role_id=

返回字段（官方）：
    current_resin（当前体力）、max_resin（上限）、resin_recovery_time（距离下一点的秒数，
    字符串）、remain_resin_discount_num（周本减半剩余次数）等。
"""

import datetime
import json
import threading

import config

from skills import mys_api

# 一次副本 / 地脉 20 体力，一次世界首领 40 体力（游戏内固定值，不是推算）。
RESIN_PER_DOMAIN = 20
RESIN_PER_LEYLINE = 20
RESIN_PER_BOSS = 40

# 体力恢复速度：1 点 / 8 分钟（官方）。用来把"还要几天"换算成现实时间。
RESIN_REGEN_SECONDS = 8 * 60
RESIN_PER_DAY = 24 * 60 * 60 // RESIN_REGEN_SECONDS        # 180

# 体力上限：**到 200 就回满了**，不会再涨。
# ⚠️ 这条必须有：手动录入时拿不到上限（dailyNote 被风控挡着），
#    不封顶的话"按 8 分钟 1 点推算"会一路加上去 ——
#    实测卡片显示过 345 这种不可能的数字（玩家反馈）。
RESIN_MAX_DEFAULT = 200

# 便笺接口是有限流的风控接口：同进程内缓存一小段时间，避免页面轮询把它打爆。
_CACHE_SECONDS = 120
_LOCK = threading.RLock()
_CACHE = {"value": None, "at": None}


def _now():
    return datetime.datetime.now()


def _format_recovery(seconds):
    """`3600` → `1 小时 0 分`；读不出来给空串。"""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return "已回满"
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分"


def parse_resin(payload):
    """把 dailyNote 的 `data` 解析成我们自己的形状（**只认读到的字段**）。"""
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    current = data.get("current_resin")
    if current is None:
        return None
    try:
        current = int(current)
    except (TypeError, ValueError):
        return None
    try:
        top = int(data.get("max_resin") or 0)
    except (TypeError, ValueError):
        top = 0
    recovery = _format_recovery(data.get("resin_recovery_time"))
    return {
        "current": current,
        "max": top,
        "recovery_text": recovery,
        "recovery_seconds": int(data.get("resin_recovery_time") or 0)
        if str(data.get("resin_recovery_time") or "0").isdigit() else 0,
        # 周本减半次数：顺手带上，规划周本时有用（读不到就是 0，不当成事实用）
        "weekly_discount_left": int(data.get("remain_resin_discount_num") or 0),
        "daily_tasks_done": int(data.get("finished_task_num") or 0),
        "daily_tasks_total": int(data.get("total_task_num") or 0),
    }


def fetch_resin(uid=None, server=None, cookie=None, timeout=15, path=None):
    """读一次真实体力（**每次都发请求**；要缓存请用 `current_resin()`）。"""
    uid = str(uid or config.DEFAULT_UID or "").strip()
    if not uid:
        return {"available": False, "reason": "没配 DEFAULT_UID，读不了体力", "current": 0, "max": 0}
    server = server or mys_api.server_from_uid(uid)
    if not server:
        return {"available": False, "reason": f"UID {uid} 认不出服务器（国服 / 国际服）",
                "current": 0, "max": 0}
    url = f"{config.MYS_RECORD_HOST}/game_record/app/genshin/api/dailyNote"
    query = f"server={server}&role_id={uid}"
    try:
        payload = mys_api._request("GET", url, query=query, cookie=cookie, timeout=timeout,
                                   path=path)
    except mys_api.MysRiskControl as exc:
        # 风控不是"没体力"，是"这次问不到"—— 如实说，绝不当成 0
        return {"available": False, "current": 0, "max": 0,
                "reason": f"米游社风控拦了体力查询（{exc}）：这个接口要 app 版 cookie（扫码登录那套），"
                          f"养成计算器的 v2 cookie 不行"}
    except mys_api.MysAuthError as exc:
        return {"available": False, "current": 0, "max": 0,
                "reason": f"cookie 无效或权限不够（{exc}）；体力接口要 app 版 cookie"}
    except Exception as exc:                # noqa: BLE001 —— 网络问题不该让规划崩
        return {"available": False, "current": 0, "max": 0,
                "reason": f"体力查询失败（{type(exc).__name__}: {exc}）"}

    parsed = parse_resin(payload)
    if not parsed:
        return {"available": False, "current": 0, "max": 0,
                "reason": "体力接口返回了看不懂的数据（米游社改版？）"}
    parsed.update({
        "available": True,
        "uid": uid,
        "server": server,
        "fetched_at": _now().strftime(mys_api._TIME_FORMAT),
        "source": "米游社每日便笺（dailyNote）",
        "reason": "",
    })
    return parsed


def current_resin(uid=None, server=None, cookie=None, force=False, timeout=15, path=None):
    """带短缓存的体力（页面轮询 / 多次规划用这个）。"""
    now = _now()
    with _LOCK:
        cached = _CACHE.get("value")
        at = _CACHE.get("at")
        if cached and at and not force:
            age = (now - at).total_seconds()
            if age < _CACHE_SECONDS:
                value = dict(cached)
                value["age_seconds"] = round(age, 1)
                value["cached"] = True
                return value
    value = fetch_resin(uid=uid, server=server, cookie=cookie, timeout=timeout, path=path)
    value["cached"] = False
    value["age_seconds"] = 0.0
    with _LOCK:
        _CACHE["value"] = dict(value)
        _CACHE["at"] = now
    return value


def cache_clear():
    """清缓存（刚跑完 BetterGI / 玩家手动点刷新时调用）。"""
    with _LOCK:
        _CACHE["value"] = None
        _CACHE["at"] = None


def runs_for(resin, kind, needed=None, resin_each=None):
    """这些体力够跑几趟。

    `needed` 给了就取"缺口需要的趟数"和"体力允许的趟数"的**较小值**
    （缺口 2 趟、体力只够 1 趟 → 1 趟；缺口 1 趟、体力很多 → 1 趟）。
    """
    each = int(resin_each or (RESIN_PER_BOSS if kind == "boss" else RESIN_PER_DOMAIN))
    if each <= 0:
        return 0
    affordable = max(0, int(resin or 0)) // each
    if needed is None:
        return affordable
    return max(0, min(affordable, int(needed)))


def describe(resin_state):
    """一行人类可读的体力状态（审批屏 / 界面用）。"""
    state = resin_state or {}
    if not state.get("available"):
        return f"⚠️ 体力：读不到（{state.get('reason') or '原因未知'}）—— 本轮的趟数按缺口排，跑完以游戏内为准"
    text = f"💧 体力：{state.get('current')}"
    if state.get("max"):
        text += f"/{state['max']}"
    if state.get("base") is not None:
        text += f"（手动记的 {state['base']}，按 8 分钟/点推算）"
    else:
        recovery = state.get("recovery_text")
        if recovery and recovery != "已回满":
            text += f"（下一点还要 {recovery}）"
    return text


MANUAL_KEY = "resin_manual_v1"


def set_manual(value, top=0, uid=None, when=None):
    """手动记一次体力（玩家在界面上填 / 对 Agent 说一句）。

    **为什么需要手动**：米游社的每日便笺接口对这个账号一直是 `5003 账号数据异常`
    （实测 8 种 cookie/DS/客户端类型组合全被拒），所以"读真实体力"这条路走不通。
    但体力是**按 8 分钟 1 点线性回涨**的 —— 玩家报一个准数（比如"现在 27"），
    程序就能自己往后推算，几小时内都够用。比"读不到就瞎排趟数"强得多。

    存进 `settings` 表（跨重启保留），并且记录"记下来的时刻"用于回涨推算。
    """
    try:
        current = max(0, int(value))
    except (TypeError, ValueError):
        return {"ok": False, "error": "体力得是数字"}
    # 上限默认按游戏上限 200：玩家填了 200 以上（抄错/看错）时也按 200 记，
    # 免得后面回涨推算从一个不可能的数字开始。
    limit = int(top or 0) or RESIN_MAX_DEFAULT
    record = {
        "current": min(current, limit),
        "max": limit,
        "uid": str(uid or config.DEFAULT_UID or ""),
        "at": (when or _now()).strftime(mys_api._TIME_FORMAT),
    }
    try:
        from brain import growth_db

        growth_db.set_setting(MANUAL_KEY, json.dumps(record, ensure_ascii=False))
    except Exception as exc:                # noqa: BLE001 —— 存不下来也不该让界面崩
        return {"ok": False, "error": f"保存失败：{type(exc).__name__} {exc}"}
    with _LOCK:
        _CACHE["value"] = None              # 让下一次读取重新算
        _CACHE["at"] = None
    return {"ok": True, "record": record, "state": manual_state()}


def _load_manual():
    try:
        from brain import growth_db

        raw = growth_db.get_setting(MANUAL_KEY)
    except Exception:                       # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except Exception:                       # noqa: BLE001
        return None
    return record if isinstance(record, dict) else None


def manual_state(now=None):
    """手动记的体力**推算到此刻**是多少（按 8 分钟回涨 1 点）。

    返回 `None` 表示没有记录。`stale` 为 True 表示记下来太久了（超过 12 小时），
    回涨推算已经没什么意义（大概率早就回满了）。
    """
    record = _load_manual()
    if not record:
        return None
    now = now or _now()
    try:
        stamp = datetime.datetime.strptime(str(record.get("at") or ""), mys_api._TIME_FORMAT)
    except (TypeError, ValueError):
        stamp = None
    base = int(record.get("current") or 0)
    # ⚠️ 没记上限时按**游戏上限 200** 封顶：不封顶的话回涨会一路加上去
    #    （玩家实测看到过 345 —— 那是不可能的数字，也会让"够几趟"算错）。
    top = int(record.get("max") or 0) or RESIN_MAX_DEFAULT
    elapsed = int((now - stamp).total_seconds()) if stamp else 0
    recovered = max(0, elapsed) // RESIN_REGEN_SECONDS
    current = min(base + recovered, top)
    full = current >= top
    return {
        "available": True,
        "current": int(current),
        "max": int(top),
        "base": base,
        "full": bool(full),
        "recorded_at": record.get("at") or "",
        # 回满之后就不再"回涨"了（免得显示"已回涨 47 点"这种看不懂的数字）
        "recovered": int(min(recovered, max(0, top - base))),
        "age_seconds": max(0, elapsed),
        "stale": bool(stamp and (now - stamp).total_seconds() > 12 * 3600),
        "source": ("体力已回满（按 8 分钟 1 点推算）" if full
                   else "手动录入（按 8 分钟 1 点推算已回涨）"),
        "reason": "",
        "cached": False,
    }


def probe(uid=None, server=None, cookie=None, force=False, timeout=15, path=None):
    """规划时用的体力读取：**先试接口，读不到就用手动记的**（都没有才如实说没有）。

    为什么要先看 cookie 支不支持再发请求：每日便笺要 app 版 cookie，而养成计算器那套
    打过去一定回"账号数据异常" —— 每次规划都白打一次风控接口没有任何好处。
    就算支持，这个账号实测也会被 `5003` 拦（见模块头部说明），所以手动那条路是主力。
    """
    try:
        audit = mys_api.audit_cookie(cookie)
    except Exception:                       # noqa: BLE001
        audit = {}
    if audit.get("has_v1"):
        state = current_resin(uid=uid, server=server, cookie=cookie, force=force,
                              timeout=timeout, path=path)
        if state.get("available"):
            return state
        api_reason = state.get("reason") or ""
    else:
        api_reason = ("体力接口要 app 版 cookie（v1 ltoken）："
                      f"现在这份是 {audit.get('mode') or '未配置'} 形态")

    manual = manual_state()
    if manual:
        manual = dict(manual)
        manual["api_reason"] = api_reason
        manual["reason"] = (f"接口读不到（{api_reason}），"
                            f"按你手动记的 {manual['base']} 点推算"
                            f"（已回涨 {manual['recovered']} 点）")
        with _LOCK:
            _CACHE["value"] = dict(manual)
            _CACHE["at"] = _now()
        return manual

    state = {"available": False, "current": 0, "max": 0, "cached": False,
             "age_seconds": 0.0, "reason": (api_reason + "；也还没手动记过体力 —— "
                                            "在养成页填一下当前体力，趟数就能按它算")}
    with _LOCK:
        _CACHE["value"] = dict(state)
        _CACHE["at"] = _now()
    return state


def refresh(uid=None, server=None, cookie=None, timeout=15, path=None):
    """**手动拉取一次实时体力**（界面上那个「拉取实时体力」按钮走这里）。

    与 `probe()` 的区别只在"要不要真的打接口"：
      · `probe()` 是规划时顺手读的：cookie 形态不支持就**不发请求**（省得白打风控接口）；
      · `refresh()` 是玩家**主动点的**：不管三七二十一先真打一次，
        这样才能回答"现在到底能不能读到实时体力"，而不是拿缓存/推算糊弄。

    返回 `{"ok", "from_api", "state", "note"}`：
      · `from_api=True`  → 真的读到了实时体力（state 就是它）；
      · `from_api=False` → 接口读不到，`note` 写明原因，`state` 退回"手动记的 + 回涨推算"。
    """
    cache_clear()
    if uid is None and not _cookie_has_v1(cookie):
        # 连 v1 令牌都没有：明确告诉玩家"这份 cookie 读不了体力"，别假装试过了
        state = probe(uid=uid, server=server, cookie=cookie, force=True,
                      timeout=timeout, path=path)
        return {"ok": bool(state.get("available")), "from_api": False, "state": state,
                "note": state.get("api_reason") or state.get("reason") or "读不到实时体力"}

    state = current_resin(uid=uid, server=server, cookie=cookie, force=True,
                          timeout=timeout, path=path)
    if state.get("available"):
        return {"ok": True, "from_api": True, "state": state,
                "note": f"✅ 已拉取实时体力：{state.get('current')}"
                        + (f"/{state['max']}" if state.get("max") else "")}
    fallback = probe(uid=uid, server=server, cookie=cookie, force=True,
                     timeout=timeout, path=path)
    return {"ok": bool(fallback.get("available")), "from_api": False, "state": fallback,
            "note": f"⚠️ 米游社没给实时体力（{state.get('reason') or '未知原因'}）；"
                    f"{'改用你手动记的那份推算' if fallback.get('available') else '也还没手动记过体力'}"}


def _cookie_has_v1(cookie=None):
    try:
        return bool(mys_api.audit_cookie(cookie).get("has_v1"))
    except Exception:                       # noqa: BLE001
        return False


def status():
    """给界面/体检用的一份快照（不主动联网）。"""
    with _LOCK:
        cached = _CACHE.get("value")
    if not cached:
        return {"available": False, "cached": False,
                "reason": "还没查过体力（会被规划自动查询）"}
    state = dict(cached)
    state["cached"] = True
    return state
