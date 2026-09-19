"""米游社个人战绩：补全"不在展柜的角色"。

**为什么需要**：展柜（Enka）一次只放 8 个角色。玩家问起展柜外的角色时，Agent 只能回
"展柜 JSON 里没有这个角色" —— 其实答案在他的账号里，只是没放进展柜。填了米游社 cookie
就能读到账号下**全部角色**的等级 / 天赋等级 / 武器，材料照旧由本地百科字典算（共用
`env_reader.build_avatar_entry`），所以"展柜外的角色"和展柜里的角色材料形状完全一致。

**接口与签名**：参考喵喵插件（miao-plugin）底层那套 Yunzai runtime 实现
（`model/mys/mysApi.js` + `model/mys/apiTool.js`），国服参数：

    DS = md5(f"salt={salt}&t={t}&r={r}&b={body}&q={query}")
    salt = xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs     （随米游社 App 版本变化，可用 .env 覆盖）
    头  = x-rpc-app_version: 2.40.1 / x-rpc-client_type: 5 / Referer: webstatic.mihoyo.com

**⚠️ 边界与风险（都很实在，别粉饰）**
  · 这不是官方开放接口，是米游社 App/网页自己用的接口，**风控严格**：有时会返回
    "需要验证码"（retcode 10001 / -2016），这时我们**不重试**、安静降级回展柜数据；
  · cookie 是账号级凭据，只从 `.env` 读（`MYS_COOKIE`），日志里永远只打印掩码；
  · 玩家明确要求"别每次启动都拉，不然会触发风控"，所以：
      名单（1 次请求）按 `MYS_CACHE_TTL_HOURS`（默认 48 小时）缓存；
      天赋详情（每个角色 1 次请求）**只在真的被问到**时按需拉，且每轮最多
      `MYS_DETAIL_MAX_PER_TURN` 个；
  · 没配 cookie → 本模块所有对外入口都直接返回"没配置"，一次网络请求都不发。
"""

import datetime
import json
import os
import random
import re
import secrets
import string
import time
import uuid
from hashlib import md5

import config

# ==========================================
# 🌟 异常：把"能自己修"和"修不了"分开，调用方据此决定怎么提示玩家
# ==========================================


class MysError(Exception):
    """米游社数据不可用的统一父类（调用方一律降级回展柜，不影响主流程）。"""


class MysNotConfigured(MysError):
    """没配 `MYS_COOKIE`（功能关闭，不是错误）。"""


class MysAuthError(MysError):
    """cookie 失效 / 未登录：需要玩家重新取一份 cookie。"""


class MysRiskControl(MysError):
    """米游社风控（要验证码/请求异常）：短期别再来，安静降级。"""


class MysTransportError(MysError):
    """网络层失败（超时/DNS/非 JSON）。"""


class MysApiError(MysError):
    """接口返回了非 0 的 retcode。"""

    def __init__(self, retcode, message):
        super().__init__(f"米游社接口 retcode={retcode}：{message}")
        self.retcode = retcode
        self.message = message


# 返回码含义（只列用得上的；社区整理 + 实测）
_RETCODE_MESSAGES = {
    -100: "未登录或 cookie 已失效",
    -101: "账号异常",
    -111: "cookie 已过期",
    -1110: "登录状态失效",
    10001: "触发米游社风控，需要在米游社 App 里完成验证",
    -2016: "请求异常（多半是风控/参数被拒）",
    -5003: "账号数据异常",
    5003: "账号数据异常",
    -110: "请求被拒绝（风控）",
}
_RETCODE_AUTH = {-100, -101, -111, -1110}
_RETCODE_RISK = {10001, -2016, -5003, 5003, -110}


# ==========================================
# 🌟 cookie / 设备号 / 签名 / 区服
# ==========================================


def mask_cookie(cookie):
    """日志用：只留键名和值的首尾，绝不整串打印。"""
    cookie = str(cookie or "")
    if not cookie:
        return "(空)"
    parts = []
    for chunk in cookie.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        value = value.strip()
        shown = "*" * len(value) if len(value) <= 8 else f"{value[:4]}…{value[-4:]}"
        parts.append(f"{key.strip()}={shown}")
    return "; ".join(parts)


def cookie_configured():
    return bool(str(config.MYS_COOKIE or "").strip())


# 米游社 cookie 的三种"代"：
#   v1（ltuid / account_id / ltoken / cookie_token）—— 老接口（战绩、计算器活动）只认这套；
#   v2（*_v2）—— 现在的网页登录只发这套，给新版网关与 App 用；
#   device（_MHYUUID / DEVICEFP*）—— 设备指纹，给风控用。
#
# ⚠️ 实测踩过（玩家报障）：从 www.mihoyo.com 网页复制下来的 cookie **可能只有 v2**，
#    公共接口（getUserGameRolesByCookie）照样能用、能列出角色，于是"看起来配好了"；
#    但**需要 ltoken 的接口全部失败**：
#      · 养成计算器 /v1/sync/avatar/list → retcode -100「请先登录后参与活动」
#      · 战绩 character/list            → retcode 5003
#    v2 的 ltoken_v2 **不能**当 ltoken 用（试过：补成 v1 键名、放进 x-rpc-ltoken 头都不行）。
#    所以必须显式把"缺 ltoken"这件事说出来，而不是让玩家对着"未登录"猜。
_REQUIRED_COOKIE_KEYS = (
    (("ltuid", "account_id"), "账号 id（哪个号）"),
    (("ltoken", "cookie_token"), "长期令牌（战绩接口与养成计算器必须要）"),
)


def audit_cookie(cookie=None):
    """检查 cookie 完不完整。返回 `{"ok", "missing": [{keys, why}], "mode", "has_v2", "hint"}`。

    两种"够用"的形态（`mode` 字段标出来）：

      · `mode="app"`        —— 有 v1 `ltoken`（**扫码登录**拿到的）。养成计算器要的
        单角色状态 / 算材料 / 战绩接口都认它；
      · `mode="calculator"`—— 只有网页那套（`ltoken_v2` / `cookie_token_v2` /
        `account_id_v2`）。这是**在养成计算器网页上登录**得到的会话。
        ⚠️ 实测：`ltoken_v2` **不能**当 v1 `ltoken` 用（补成 v1 键名、放 `x-rpc-ltoken`
        头都不行），所以这类 cookie 只标"能用"，不假装它等于 v1。

    `ok=False` 表示"连账号 id 都没有"——那种 cookie 一定会被拒。
    """
    text = str(cookie if cookie is not None else config.MYS_COOKIE or "")
    if not text.strip():
        return {"ok": False, "configured": False, "missing": [], "has_v2": False,
                "mode": "", "hint": "没有配置 MYS_COOKIE"}

    keys = set()
    for chunk in text.split(";"):
        if "=" in chunk:
            keys.add(chunk.strip().split("=", 1)[0].strip())

    has_v1 = bool({"ltoken", "cookie_token"} & keys)
    has_v2 = any(key.endswith("_v2") for key in keys)
    has_id = bool({"ltuid", "account_id"} & keys) or bool(
        {"ltuid_v2", "account_id_v2"} & keys)

    # 有账号 id + 任意一种令牌 = 够用（v1 更完整，优先报 app 形态）
    missing = []
    if not has_id:
        missing.append({"keys": ["ltuid", "account_id"], "why": "账号 id（哪个号）"})
    if not (has_v1 or has_v2):
        missing.append({"keys": ["ltoken", "cookie_token"],
                        "why": "长期令牌（养成计算器/战绩接口必须要）"})

    mode = "app" if has_v1 else ("calculator" if has_v2 else "")
    hint = ""
    if missing:
        names = "、".join("/".join(item["keys"]) for item in missing)
        hint = (
            f"这份 cookie 缺 {names}，需要登录的接口一定会被拒。\n"
            "怎么补：① **从养成计算器网页复制**（推荐）—— 打开 "
            "https://act.mihoyo.com/ys/event/calculator/index.html ，登录后 "
            "F12 → Network → 找一条发往 api-takumi.mihoyo.com 的请求 → 右键 "
            "「Copy as cURL」，把整段粘到 Studio 的「手动粘贴 cookie」（支持 cURL）；"
            "② 或者用**扫码登录**换一份 v1 ltoken。"
        )
    elif mode == "calculator":
        hint = ("这是**养成计算器网页**那套会话（v2）。能用来算材料；"
                "如果某个接口说未登录，就再补一份 v1 ltoken（扫码登录）。")
    return {"ok": not missing, "configured": True, "missing": missing,
            "has_v2": has_v2, "has_v1": has_v1, "mode": mode, "hint": hint}


def cookie_account_id(cookie=None):
    """从 cookie 里取 ltuid / account_id（米游社两套 id 都认，v2 优先）。"""
    text = str(cookie if cookie is not None else config.MYS_COOKIE or "")
    for key in ("ltuid_v2", "account_id_v2", "ltuid", "account_id"):
        match = re.search(rf"{key}=(\d{{4,12}})", text)
        if match:
            return match.group(1)
    return ""


def server_from_uid(uid):
    """UID → 米游社区服名（国服：官服 cn_gf01 / B服 cn_qd01）。

    规则同 Yunzai runtime：看 uid 去掉后 8 位剩下的前缀（5 开头是 B 服）。
    """
    text = str(uid or "").strip()
    if not text.isdigit() or len(text) < 9:
        return "cn_gf01"
    return "cn_qd01" if text[:-8] == "5" else "cn_gf01"


def make_ds(query="", body="", salt=None, now=None, nonce=None):
    """生成 DS 签名：`{t},{r},{md5(salt=…&t=…&r=…&b=…&q=…)}`。

    r 是 6 位随机数（Yunzai 取 100000~999999）。`nonce`/`now` 只给测试用。
    """
    salt = salt or config.MYS_SALT
    moment = int(now if now is not None else time.time())
    value = random.randint(100000, 999999) if nonce is None else int(nonce)
    main = f"salt={salt}&t={moment}&r={value}&b={body or ''}&q={query or ''}"
    return f"{moment},{value},{md5(main.encode('utf-8')).hexdigest()}"


# ==========================================
# 🌟 米游社 App 那套（扫码登录拿 stoken → 换 ltoken 必须用它）
# ==========================================
#
# 为什么单独一套：米游社把"App 接口"和"网页接口"分得很开。
#   · 网页那套 salt（MYS_SALT）能给战绩/计算器接口用；
#   · 但 `passport-api` 上的**游戏账号扫码登录**（createQRLogin / queryQRLoginStatus）
#     必须用 **App 那套** salt + app_version，否则报"参数不合法"。
# 这里的默认值取自社区实现（xiaoyao-cvs-plugin 的 model/mys/mysTool.js），
# 米游社改版时改 .env 的 MYS_APP_SALT / MYS_APP_VERSION / MYS_APP_ID 即可。


def pass_device_id(path=None):
    """扫码登录那套要的 **32 位大写**设备号（社区实现是 `randomString(32).toUpperCase()`）。

    为什么单独造一个、不直接用 `device_id()`：
      · passport 的 `queryQRLoginStatus` 对设备号很挑 —— 用网页那套的小写 uuid
        会被判成 `-3503 请求失败，当前设备或网络环境存在风险`（实测踩过）；
      · 生成一次就存进快照复用：每次登录都换设备号反而更像机器人。
    """
    snapshot = load_snapshot(path) or {}
    value = str(snapshot.get("pass_device_id") or "").strip()
    if len(value) == 32:
        return value
    alphabet = string.ascii_uppercase + string.digits
    value = "".join(secrets.choice(alphabet) for _ in range(32))
    snapshot["pass_device_id"] = value
    save_snapshot(snapshot, path)
    return value


def _pass_device_label(seed, length):
    """从设备号推出一串稳定的 `[a-z0-9]` 标签（设备名 / 设备型号用）。

    社区实现里这两个字段就是随机小写串（型号 16 位、名字 1~10 位），
    并没有真实的机型语义 —— 这里做成"由设备号推导"，好处是同一次安装
    每次跑都一致，方便复现和排查。
    """
    digest = md5(f"{seed}:{length}".encode("utf-8")).hexdigest()
    return digest[:length]


def app_headers(query="", body="", cookie=None, path=None, device=None):
    """**passport（扫码登录）那套**请求头。

    这几个值实测都不能少（少一个就 `-3005 参数不合法`）：
      · `x-rpc-app_id` 是**字符串** `bll8iq97cem8`（不是数字）；
      · `x-rpc-client_type` = `2`（Android）；
      · `User-Agent` = `okhttp/4.8.0`；
      · DS 用 `MYS_APP_SALT`（社区里叫 passSalt，**不是**网页那个 salt）。

    另外三个头是**防 `-3503 设备或网络环境存在风险`**的：
      · `x-rpc-device_id` 必须是 32 位大写（`pass_device_id()`）；
      · `x-rpc-device_fp`（设备指纹）不能省；
      · `x-rpc-device_model` / `x-rpc-device_name` 是随机小写串。
    这一组取值与社区实现（xiaoyao-cvs-plugin `getHeaders(..., "pass")`）对齐。
    """
    resolved = device or pass_device_id(path)
    headers = {
        "x-rpc-device_id": resolved,
        "x-rpc-device_name": _pass_device_label(resolved, 6),
        "x-rpc-device_model": _pass_device_label(resolved, 16),
        "x-rpc-device_fp": config.MYS_DEVICE_FP,
        "x-rpc-app_id": config.MYS_APP_ID,
        "x-rpc-app_version": config.MYS_APP_VERSION_APP,
        "x-rpc-game_biz": "bbs_cn",
        "x-rpc-sys_version": config.MYS_APP_SYS_VERSION,
        "x-rpc-client_type": "2",
        "x-rpc-channel": "appstore",
        "x-rpc-sdk_version": "1.3.1.2",
        "x-rpc-aigis": "",
        "Content-Type": "application/json;",
        "User-Agent": "okhttp/4.8.0",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "Keep-Alive",
    }
    if cookie:
        headers["Cookie"] = str(cookie)
    return headers


def app_ds(query="", body="", now=None, nonce=None):
    """passport 那套 DS 签名（用 `MYS_APP_SALT`）。

    算法与网页那套**完全相同**（`md5("salt=…&t=…&r=…&b=…&q=…")`），只是 salt 不同 ——
    所以这里直接复用 `make_ds`，换的只是默认 salt。
    """
    return make_ds(query=query, body=body, salt=config.MYS_APP_SALT, now=now, nonce=nonce)


def app_request(method, url, query="", body_obj=None, cookie=None, timeout=15, device=None):
    """发一次 **passport（扫码登录）那套**请求并返回 `data`。

    DS 放在**请求头**里（不是 query 参数）—— 这一点和网页那套相反，实测过。
    """
    if not cookie_configured() and cookie is None and "passport-api" not in str(url):
        raise MysNotConfigured("未配置 MYS_COOKIE")
    import httpx

    body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False) if body_obj is not None else ""
    headers = app_headers(query, body, cookie=cookie, device=device)
    headers["DS"] = app_ds(query, body)
    target = f"{url}?{query}" if query else url
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            if body:
                response = client.post(target, content=body.encode("utf-8"), headers=headers)
            else:
                response = client.get(target, headers=headers)
    except Exception as exc:            # noqa: BLE001
        raise MysTransportError(f"网络失败：{type(exc).__name__} {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise MysTransportError(
            f"返回的不是 JSON（HTTP {response.status_code}）：{response.text[:120]!r}"
        ) from exc
    retcode = payload.get("retcode")
    if retcode != 0:
        message = str(payload.get("message") or "")
        if retcode in _RETCODE_AUTH:
            raise MysAuthError(_RETCODE_MESSAGES.get(retcode, message or "登录状态失效"))
        if retcode in _RETCODE_RISK:
            raise MysRiskControl(_RETCODE_MESSAGES.get(retcode, message or "触发风控"))
        raise MysApiError(retcode, message or "未知错误")
    return payload.get("data") or {}


def device_id(path=None):
    """稳定的设备号：存在快照里复用。每次请求都换设备号更像机器人，更容易触发风控。"""
    snapshot = load_snapshot(path) or {}
    value = str(snapshot.get("device_id") or "").strip()
    return value or str(uuid.uuid4())


def build_headers(url, query="", body="", cookie=None, path=None):
    """米游社 App（国服）请求头。cookie 只进 Cookie 头，绝不进日志。"""
    return {
        "Cookie": str(cookie if cookie is not None else config.MYS_COOKIE or ""),
        "DS": make_ds(query, body),
        "x-rpc-app_version": config.MYS_APP_VERSION,
        "x-rpc-client_type": config.MYS_CLIENT_TYPE,
        "x-rpc-device_id": device_id(path),
        "x-rpc-device_name": "GI-Agent",
        "x-rpc-sys_version": "12",
        "x-rpc-channel": "mihoyo",
        "X-Requested-With": "com.mihoyo.hyperion",
        "User-Agent": f"miHoYoBBS/{config.MYS_APP_VERSION}",
        "Referer": "https://webstatic.mihoyo.com/",
        "Origin": "https://webstatic.mihoyo.com",
        "Accept": "application/json",
    }


# ==========================================
# 🌟 HTTP
# ==========================================


def _request(method="GET", url="", query="", body_obj=None, cookie=None, timeout=15,
             path=None, headers=None):
    """发一次米游社请求，返回 `data` 字段；失败抛 MysError 子类。"""
    if not cookie_configured() and cookie is None:
        raise MysNotConfigured("未配置 MYS_COOKIE")

    import httpx

    body = json.dumps(body_obj, ensure_ascii=False) if body_obj is not None else ""
    headers = headers or build_headers(url, query, body, cookie=cookie, path=path)
    target = f"{url}?{query}" if query else url

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            if body:
                response = client.post(target, content=body.encode("utf-8"), headers=headers)
            else:
                response = client.get(target, headers=headers)
    except Exception as exc:        # noqa: BLE001 —— httpx 异常种类多，统一转成我们自己的类型
        raise MysTransportError(f"网络失败：{type(exc).__name__} {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise MysTransportError(
            f"返回的不是 JSON（HTTP {response.status_code}）：{response.text[:120]!r}"
        ) from exc

    retcode = payload.get("retcode")
    message = str(payload.get("message") or "")
    if retcode != 0:
        if retcode in _RETCODE_AUTH:
            raise MysAuthError(_RETCODE_MESSAGES.get(retcode, message or "登录状态失效"))
        if retcode in _RETCODE_RISK:
            raise MysRiskControl(_RETCODE_MESSAGES.get(retcode, message or "触发风控"))
        raise MysApiError(retcode, message or "未知错误")

    return payload.get("data") or {}


def web_request(method="POST", url="", body_obj=None, headers=None, timeout=15):
    """给**网页版通行证**接口用的裸请求：返回 `(payload, 响应头)`。

    **为什么要单独一条**：网页扫码登录拿到的令牌藏在 **`Set-Cookie` 响应头**里
    （`ltoken_v2` / `ltuid_v2` / `cookie_token_v2` 那一套，也就是养成计算器要的会话），
    而 `_request()` 只回 `data` 字段，响应头直接丢掉了 —— 拿不到就换不出 cookie。

    这条通道**不签名、不带 cookie**（登录本身就是要换 cookie），所以不能拿它去打业务接口。
    """
    import httpx

    body = json.dumps(body_obj, ensure_ascii=False) if body_obj is not None else ""
    merged = {"Content-Type": "application/json", "User-Agent": _WEB_USER_AGENT}
    merged.update(headers or {})
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            if str(method).upper() == "POST":
                response = client.post(url, content=body.encode("utf-8"), headers=merged)
            else:
                response = client.get(url, headers=merged)
    except Exception as exc:                # noqa: BLE001
        raise MysTransportError(f"网络失败：{type(exc).__name__} {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise MysTransportError(
            f"返回的不是 JSON（HTTP {response.status_code}）：{response.text[:120]!r}"
        ) from exc
    return payload, response


def cookies_from_response(response):
    """把响应里的 `Set-Cookie` 抠成 `{键: 值}`。

    httpx 的 `response.headers` 是**多值**的：`get_list("set-cookie")` 才能一条不落地拿到，
    用 `response.headers.get("set-cookie")` 只会得到第一条 —— 而网页登录要的
    `ltoken_v2` / `ltuid_v2` / `cookie_token_v2` 往往分散在好几条里（踩过这个坑）。
    """
    out = {}
    try:
        raw_list = response.headers.get_list("set-cookie")
    except AttributeError:                  # 兼容其它 HTTP 客户端
        raw_list = [response.headers.get("set-cookie", "")]
    for raw in raw_list:
        for chunk in str(raw or "").split(";"):
            chunk = chunk.strip()
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key or key.lower() in ("path", "expires", "domain", "max-age",
                                          "samesite", "secure", "httponly"):
                continue
            if value:
                out[key] = value
    return out


# 网页版请求用的 UA（米游社网页端；`x-rpc-client_type=4` 配合它才是扫码登录那套）
_WEB_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def fetch_roles(uid=None, cookie=None, timeout=15, path=None):
    """cookie 名下的原神角色（uid / 区服 / 昵称 / 等级）。"""
    data = _request(
        url=f"{config.MYS_API_HOST}/binding/api/getUserGameRolesByCookie",
        query="game_biz=hk4e_cn",
        cookie=cookie,
        timeout=timeout,
        path=path,
    )
    roles = []
    for item in data.get("list") or []:
        if str(item.get("game_biz") or "hk4e_cn") != "hk4e_cn":
            continue
        roles.append({
            "uid": str(item.get("game_uid") or ""),
            "server": str(item.get("region") or "") or server_from_uid(item.get("game_uid")),
            "nickname": str(item.get("nickname") or ""),
            "level": item.get("level"),
        })
    return roles


def fetch_character_list(uid, server=None, cookie=None, timeout=20, path=None):
    """账号下全部角色（等级 / 武器 / 命座），**不含**天赋等级。返回 (角色列表, 区服)。"""
    server = server or server_from_uid(uid)
    data = _request(
        method="POST",
        url=f"{config.MYS_RECORD_HOST}/game_record/app/genshin/api/character/list",
        body_obj={"role_id": str(uid), "server": server},
        cookie=cookie,
        timeout=timeout,
        path=path,
    )
    return list(data.get("avatars") or []), server


def fetch_character_detail(uid, avatar_ids, server=None, cookie=None, timeout=20, path=None):
    """指定角色的详情（**含天赋等级** `skills[].level`）。"""
    ids = []
    for item in avatar_ids or ():
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    server = server or server_from_uid(uid)
    data = _request(
        method="POST",
        url=f"{config.MYS_RECORD_HOST}/game_record/app/genshin/api/character/detail",
        body_obj={"role_id": str(uid), "server": server, "character_ids": ids},
        cookie=cookie,
        timeout=timeout,
        path=path,
    )
    return list(data.get("avatars") or [])


# ==========================================
# 🌟 快照缓存（memory/mys_characters.json）
# ==========================================
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_DETAIL_BATCH = 20


def snapshot_path():
    return getattr(config, "MYS_CACHE_PATH", "") or config.project_path(
        "memory", "mys_characters.json"
    )


def load_snapshot(path=None):
    try:
        with open(path or snapshot_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_snapshot(snapshot, path=None):
    """原子写快照（临时文件 + os.replace）—— 半截文件会让下次启动读到坏数据。"""
    target = path or snapshot_path()
    directory = os.path.dirname(target)
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        print(f"⚠️ 米游社快照写入失败（不影响本轮）：{exc}")


def snapshot_time(snapshot):
    try:
        return datetime.datetime.strptime(str((snapshot or {}).get("fetched_at")), _TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def snapshot_age_hours(snapshot, now=None):
    moment = snapshot_time(snapshot)
    if moment is None:
        return None
    return ((now or datetime.datetime.now()) - moment).total_seconds() / 3600


def snapshot_is_stale(snapshot, max_age_hours=None, now=None):
    """没有快照 / 时间戳坏了 / 超过 TTL 都算"该刷新了"。"""
    age = snapshot_age_hours(snapshot, now=now)
    if age is None:
        return True
    ttl = config.MYS_CACHE_TTL_HOURS if max_age_hours is None else max_age_hours
    return age >= ttl


def describe_snapshot_age(snapshot, now=None):
    age = snapshot_age_hours(snapshot, now=now)
    if age is None:
        return "从未拉取"
    if age < 1:
        return f"{int(age * 60)} 分钟前"
    if age < 48:
        return f"{age:.1f} 小时前"
    return f"{age / 24:.1f} 天前"


# ==========================================
# 🌟 拉取 / 查询
# ==========================================


def _pick_role(roles, uid=None):
    wanted = str(uid or config.MYS_UID or "").strip()
    if wanted:
        for role in roles:
            if role.get("uid") == wanted:
                return role
    return roles[0] if roles else None


def refresh_snapshot(uid=None, cookie=None, with_details=(), path=None):
    """拉一次全角色名单（+ 可选的天赋详情）并写快照。返回快照。

    只发 1 次名单请求；`with_details` 里的角色各多 1 次详情请求。
    """
    cookie = cookie if cookie is not None else config.MYS_COOKIE

    roles = fetch_roles(uid=uid, cookie=cookie, path=path)
    role = _pick_role(roles, uid=uid)
    if not role:
        raise MysAuthError("cookie 名下没有原神角色（或 cookie 已失效）")

    raw_avatars, server = fetch_character_list(
        role["uid"], role.get("server"), cookie=cookie, path=path
    )

    previous = load_snapshot(path) or {}
    previous_details = {}
    for item in previous.get("avatars") or []:
        if item.get("talents_at"):
            previous_details[str(item.get("id"))] = item

    avatars = []
    for item in raw_avatars:
        avatar_id = str(item.get("id") or "")
        if not avatar_id:
            continue
        old = previous_details.get(avatar_id) or {}
        weapon = item.get("weapon") or {}
        avatars.append({
            "id": avatar_id,
            "name": str(item.get("name") or ""),
            "level": int(item.get("level") or 0),
            "rarity": item.get("rarity"),
            "element": item.get("element"),
            "fetter": item.get("fetter"),
            "constellation": item.get("actived_constellation_num"),
            "weapon": {
                "id": str(weapon.get("id") or ""),
                "name": str(weapon.get("name") or ""),
                "level": int(weapon.get("level") or 0),
                "affix": weapon.get("affix_level"),
            },
            # 天赋要单独一次请求：这里先沿用旧快照里已拉到的，缺的按需补
            "talents": old.get("talents") or {},
            "talents_at": old.get("talents_at"),
        })

    snapshot = {
        "fetched_at": datetime.datetime.now().strftime(_TIME_FORMAT),
        "uid": role["uid"],
        "server": server or role.get("server"),
        "nickname": role.get("nickname"),
        "device_id": device_id(path),
        "roles": roles,
        "avatars": avatars,
    }

    wanted = [str(item) for item in (with_details or ()) if item]
    if wanted:
        attach_details(snapshot, wanted, cookie=cookie, path=path)

    save_snapshot(snapshot, path=path)
    return snapshot


def _find_avatar(snapshot, avatar_id):
    for avatar in (snapshot or {}).get("avatars") or []:
        if str(avatar.get("id")) == str(avatar_id):
            return avatar
    return {}


def attach_details(snapshot, avatar_ids, cookie=None, path=None):
    """给快照里的若干角色补天赋等级（分批请求；失败只记账，不影响别的角色）。"""
    pending = [str(item) for item in avatar_ids or () if item]
    for start in range(0, len(pending), _DETAIL_BATCH):
        batch = pending[start:start + _DETAIL_BATCH]
        try:
            details = fetch_character_detail(
                snapshot.get("uid"), batch, server=snapshot.get("server"),
                cookie=cookie, path=path,
            )
        except MysError as exc:
            print(f"⚠️ 天赋详情拉取失败（{len(batch)} 个角色，不影响其它）：{exc}")
            break
        by_id = {str(item.get("id")): item for item in details}
        stamp = datetime.datetime.now().strftime(_TIME_FORMAT)
        for avatar in snapshot.get("avatars") or []:
            detail = by_id.get(str(avatar.get("id")))
            if detail:
                avatar["talents"] = talents_from_detail(detail)
                avatar["talent_names"] = talent_names_from_detail(detail)
                avatar["talents_at"] = stamp
    return snapshot


def talents_from_detail(detail):
    """把 `character/detail` 的 skills 列表整理成 `{技能id: 等级}`。

    ⚠️ 形状要和**展柜（Enka）那条路径**一致：Enka 给的就是 `skillLevelMap = {技能id: 等级}`。
    技能 id 是游戏里按 普攻→战技→爆发 的顺序分配的，所以按 id 排序展示才是正确顺序
    （按技能名排序会变成乱七八糟的顺序）。
    """
    talents = {}
    for skill in (detail or {}).get("skills") or []:
        skill_id = skill.get("skill_id")
        if skill_id is None:
            continue
        talents[str(skill_id)] = skill.get("level")
    return talents


def talent_names_from_detail(detail):
    """`{技能id: 技能名}` —— 只用于给人看的展示。"""
    names = {}
    for skill in (detail or {}).get("skills") or []:
        skill_id = skill.get("skill_id")
        if skill_id is None:
            continue
        names[str(skill_id)] = str(skill.get("skill_name") or skill.get("name") or skill_id)
    return names


def get_snapshot(uid=None, max_age_hours=None, refresh_if_stale=True, with_details=(), path=None):
    """取快照；**过期才刷新**（玩家要求："别每次启动都拉，1~3 天一次"）。"""
    snapshot = load_snapshot(path)
    wanted = [str(item) for item in (with_details or ()) if item]

    if snapshot and not snapshot_is_stale(snapshot, max_age_hours=max_age_hours):
        missing = [item for item in wanted if not _find_avatar(snapshot, item).get("talents_at")]
        if missing:
            attach_details(snapshot, missing, cookie=config.MYS_COOKIE, path=path)
            save_snapshot(snapshot, path=path)
        return snapshot

    if not refresh_if_stale:
        return snapshot or None
    return refresh_snapshot(uid=uid, with_details=wanted, path=path)


def find_by_name(snapshot, name):
    """按角色名精确查找。"""
    wanted = str(name or "").strip()
    if not wanted:
        return {}
    for avatar in (snapshot or {}).get("avatars") or []:
        if str(avatar.get("name")) == wanted:
            return avatar
    return {}


def owned_names(snapshot=None):
    snapshot = snapshot if snapshot is not None else load_snapshot()
    return {
        str(item.get("name"))
        for item in (snapshot or {}).get("avatars") or []
        if item.get("name")
    }


def avatar_entry(avatar_id, snapshot=None, yatta_dict=None):
    """把米游社的一条角色记录转成展柜 JSON 里的那种 avatar（材料由本地字典算）。

    形状必须和展柜一致 —— 见 `env_reader.build_avatar_entry` 的说明。
    """
    from skills import env_reader

    snapshot = snapshot if snapshot is not None else load_snapshot() or {}
    avatar = _find_avatar(snapshot, avatar_id)
    if not avatar:
        return None
    yatta_dict = yatta_dict if yatta_dict is not None else env_reader.load_yatta_dict_safely()

    weapon = avatar.get("weapon") or {}
    weapon_entry = None
    if weapon.get("id"):
        weapon_entry = env_reader.build_weapon_entry(
            weapon["id"], weapon.get("level"), yatta_dict, name=weapon.get("name")
        )

    return env_reader.build_avatar_entry(
        avatar_id,
        avatar.get("level"),
        yatta_dict,
        name=avatar.get("name"),
        weapon=weapon_entry,
        skills=avatar.get("talents") or {},
    )


def _format_talents(talents, names=None):
    """天赋等级：按技能 id 排序（= 普攻→战技→爆发），有技能名就带上名字。"""
    if not talents:
        return "天赋等级这次没拉到"
    pairs = []
    for key, value in talents.items():
        try:
            order = int(key)
        except (TypeError, ValueError):
            order = 10 ** 9
        pairs.append((order, str(key), value))
    pairs.sort()
    if names:
        return "天赋 " + "／".join(f"{names.get(key, key)} {value}" for _order, key, value in pairs)
    return "天赋 " + "/".join(str(value) for _order, _key, value in pairs)


def format_avatar_line(avatar, talent_names=None):
    """一行人类可读的角色资料（给大模型看的"展柜外角色参考"）。"""
    level = int((avatar or {}).get("level") or 0)
    tail = "" if level >= 81 else f"，未毕业（还差 {81 - level} 级到 81）"
    parts = [f"{avatar.get('name')} Lv.{level}{tail}"]
    parts.append(_format_talents(avatar.get("skills") or {}, talent_names))

    weapon = avatar.get("weapon") or {}
    if weapon.get("name") and weapon.get("name") != "未装备":
        parts.append(f"武器 {weapon['name']} Lv.{weapon.get('level')}")

    materials = []
    for item in avatar.get("materials") or []:
        text = f"{item.get('type')} {item.get('name')}"
        if item.get("schedule"):
            text += f"（{item.get('schedule')}）"
        if item.get("needed"):
            text += f" 缺 {item.get('needed')} 个"
        materials.append(text)

    line = "｜".join(parts)
    if materials:
        line += "\n     材料：" + "；".join(materials)
    return line


# ==========================================
# 🌟 按需注入：玩家提到的"展柜外角色"
# ==========================================
MIN_NAME_LENGTH = 2
# 短句里允许单字角色名（「魈」「琴」这类）：句子里没有其它字时才算，长句里不猜
SHORT_TEXT_LENGTH = 10
_NAME_POOL_CACHE = {"value": None}


def mentioned_characters(text, candidates):
    """从玩家这句话里挑出可能被提到的角色名。

    长名字优先（避免"蓝砚"被"砚"这种子串规则吃掉，也避免重复计数）。
    单字角色名（魈/琴）只在**整句很短**时才认 —— 否则"钢琴""琴酒"都会命中。
    """
    text = str(text or "")
    if not text:
        return []
    single_ok = len(text.strip()) <= SHORT_TEXT_LENGTH

    hits = []
    for name in sorted({str(item) for item in candidates or () if item}, key=len, reverse=True):
        if not name or name not in text:
            continue
        if len(name) < MIN_NAME_LENGTH and not single_ok:
            continue
        if any(name != longer and name in longer for longer in hits):
            continue
        hits.append(name)
    return hits


def character_name_pool(use_cache=True):
    """"玩家提到了谁"的识别用名字集合：本地百科字典里的全部角色 + 快照里的名字。

    字典解析很贵（几 MB JSON），所以缓存一次 —— 它只在程序启动时变。
    """
    if use_cache and _NAME_POOL_CACHE["value"] is not None:
        return _NAME_POOL_CACHE["value"]

    names = set(owned_names())
    try:
        from skills import env_reader

        for entry in env_reader.load_yatta_dict_safely().values():
            name = str((entry or {}).get("name") or "")
            if name and not name.startswith("未知"):
                names.add(name)
    except Exception as exc:        # noqa: BLE001
        print(f"⚠️ 读取角色名字典失败（只影响『提到谁』的识别）：{exc}")

    _NAME_POOL_CACHE["value"] = names
    return names


def showcase_gap_notice(user_text, showcase_names=(), uid=None, max_details=None, path=None):
    """玩家提到展柜外角色时，返回一段"展柜外角色参考"文本；没有则返回空串。

    - 没配 cookie → 空串（功能整体关闭，不打扰玩家）；
    - 提到的人都已经在展柜里 → 空串（展柜才是权威，不重复注入、不白花请求）；
    - 名单过期才刷新（默认 48 小时），天赋详情只给**被提到的**角色按需拉；
    - 拿不到数据时返回一句"参考不可用"，并明确要求模型**不要编造**等级/材料。
    """
    if not cookie_configured():
        return ""

    mentioned = mentioned_characters(user_text, character_name_pool())
    in_showcase = {str(item) for item in showcase_names or ()}
    targets = [name for name in mentioned if name not in in_showcase]
    if not targets:
        return ""

    try:
        snapshot = get_snapshot(uid=uid, path=path)
    except MysError as exc:
        print(f"⚠️ 米游社参考数据不可用：{exc}")
        return (
            "\n\n👥 【展柜外角色参考】这次没拿到（"
            + str(exc)
            + "）\n展柜里没有的角色，其等级/天赋/材料这次查不到："
            "不要凭猜测编造，也不要断言玩家没有这个角色；"
            "可以请玩家稍后再问，或把 TA 放进游戏展柜。"
        )
    except Exception as exc:        # noqa: BLE001
        print(f"⚠️ 米游社参考数据获取失败（不影响本轮）：{type(exc).__name__} {exc}")
        return ""

    if not snapshot:
        return ""

    limit = config.MYS_DETAIL_MAX_PER_TURN if max_details is None else max_details
    budget = max(0, int(limit))
    lines = []
    unknown = []

    for name in targets:
        avatar = find_by_name(snapshot, name)
        if not avatar:
            unknown.append(name)
            continue
        if not avatar.get("talents_at") and budget > 0:
            budget -= 1
            attach_details(snapshot, [avatar.get("id")], cookie=config.MYS_COOKIE, path=path)
            save_snapshot(snapshot, path=path)
        entry = avatar_entry(avatar.get("id"), snapshot=snapshot)
        if entry:
            lines.append(format_avatar_line(entry, talent_names=avatar.get("talent_names")))

    if not lines and not unknown:
        return ""

    header = (
        f"\n\n👥 【展柜外角色参考】（来源：米游社个人战绩，快照 {snapshot.get('fetched_at')}"
        f"，账号 {snapshot.get('nickname') or snapshot.get('uid')}）\n"
        "展柜一次只能放 8 个角色；下面是玩家这句话里提到、但**不在展柜**的角色，"
        "资料直接读自他自己的账号："
    )
    body = ["· " + line for line in lines]
    if unknown:
        body.append(
            "· " + "、".join(unknown)
            + "：米游社数据显示这个号里没有 TA，所以没法规划 TA 的养成"
            "（注意这和『展柜里没有』不是一回事）。"
        )
    footer = (
        "\n说明：这些角色确实不在展柜 JSON 里，但资料来自玩家账号，可以直接按上面的材料排期；"
        "**不要再回答『展柜里没有这个角色』**。写成『天赋等级这次没拉到』的，不要编造具体数字。"
    )
    return header + "\n" + "\n".join(body) + footer


def status_line(now=None, path=None):
    """给 Studio / doctor / 终端看的一行状态（不打印 cookie 本体）。"""
    if not cookie_configured():
        return "米游社：未配置（不读展柜外角色）"
    snapshot = load_snapshot(path)
    count = len((snapshot or {}).get("avatars") or [])
    return (
        f"米游社：已配置（cookie {mask_cookie(config.MYS_COOKIE)}），"
        f"缓存 {count} 个角色，更新于 {describe_snapshot_age(snapshot, now=now)}"
    )


# ==========================================
# 🌟 命令行自检：python -m skills.mys_api --check / --refresh / --show 胡桃
# ==========================================


def _main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="米游社个人战绩自检 / 手动刷新")
    parser.add_argument("--check", action="store_true", help="只验证 cookie 能不能用（1 次请求）")
    parser.add_argument("--audit", action="store_true",
                        help="只看 cookie 完不完整（含没含 ltoken），**不发任何请求**")
    parser.add_argument("--refresh", action="store_true", help="强制拉一次全角色名单")
    parser.add_argument("--show", metavar="角色名", help="打印某个角色的资料（会补天赋详情）")
    args = parser.parse_args(argv)

    if args.audit:
        audit = audit_cookie()
        if not audit["configured"]:
            print("❌ 没有配置 MYS_COOKIE（在 .env 或 Studio 的「配置 → 米游社」里填）")
            return 1
        if audit["ok"]:
            print(f"✅ cookie 看起来完整（含 ltuid / ltoken）；掩码：{mask_cookie(config.MYS_COOKIE)}")
            return 0
        print("❌ cookie 不完整：")
        for item in audit["missing"]:
            print(f"   · 缺 {'/'.join(item['keys'])} —— {item['why']}")
        print()
        for line in audit["hint"].splitlines():
            print(f"   {line}")
        return 1

    if not cookie_configured():
        print("❌ 没有配置 MYS_COOKIE（在 .env 里填；拿法见 docs/MYS_COOKIE.md）")
        return 1

    # 先白送一条"cookie 缺 ltoken"的提醒：不然 --check 过了、后面拉名单却一直报未登录，
    # 玩家只会以为是风控。这是实测踩过、最难自己查出来的一种情况。
    audit = audit_cookie()
    if not audit["ok"]:
        print("⚠️ cookie 不完整，需要登录的接口预计会失败：")
        for line in audit["hint"].splitlines():
            print(f"   {line}")
        print()

    if args.check:
        try:
            roles = fetch_roles()
        except MysError as exc:
            print(f"❌ cookie 不可用：{exc}")
            return 1
        if not roles:
            print("❌ cookie 有效，但名下没有原神角色（hk4e_cn）")
            return 1
        for role in roles:
            print(f"✅ {role['nickname']}｜UID {role['uid']}｜{role['server']}｜冒险等阶 {role.get('level')}")
        return 0

    if args.refresh:
        try:
            snapshot = refresh_snapshot()
        except MysError as exc:
            print(f"❌ 刷新失败：{exc}")
            return 1
        print(
            f"✅ 已刷新：{snapshot.get('nickname')}（UID {snapshot.get('uid')}）"
            f"共 {len(snapshot.get('avatars') or [])} 个角色"
        )
        return 0

    if args.show:
        snapshot = load_snapshot()
        avatar = find_by_name(snapshot, args.show)
        if not avatar:
            try:
                snapshot = get_snapshot(uid=None)
            except MysError as exc:
                print(f"❌ 拉取失败：{exc}")
                return 1
            avatar = find_by_name(snapshot, args.show)
        if not avatar:
            print(f"❌ 这个号里没有「{args.show}」（或名字写法不一致）")
            return 1
        if not avatar.get("talents_at"):
            attach_details(snapshot, [avatar.get("id")])
            save_snapshot(snapshot)
        entry = avatar_entry(avatar.get("id"), snapshot=snapshot)
        print(f"👤 {entry['name']}（账号 {snapshot.get('nickname')}）")
        print("   " + format_avatar_line(entry).replace("\n", "\n   "))
        return 0

    print(status_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
