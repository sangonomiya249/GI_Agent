"""QQ 官方机器人接口（轻量版）：只做"接消息 → 交给 Agent → 回消息"这一件事。

为什么不直接用 LangBot：LangBot 是个完整平台（插件、WebUI、多平台适配、pipeline 编排），
部署一套为了连一个 Agent 太重。官方 QQ 机器人协议本身很简单，用**项目已有依赖**
（`httpx` 异步 HTTP + `websockets`）几十行就能接上，而且能和 `main.py` / 飞书模式共用同一套
Agent 逻辑（见 `channels/agent_router.py`）。

协议要点（QQ 机器人开放平台 v2）：
  1. 取 token：POST https://bots.qq.com/app/getAppAccessToken {appId, clientSecret}
     → {access_token, expires_in}（默认 7200 秒，提前刷新）
  2. 取网关：GET https://api.sgroup.qq.com/websocket  Header: `Authorization: QQBot <token>`
     → {url}
  3. WebSocket：收到 op=10 HELLO（带 heartbeat_interval）→ 发 op=2 IDENTIFY
     {token: "QQBot <token>", intents, shard:[0,1], properties:{}}；按需发 op=1 心跳；
     op=0 是事件推送；op=11 心跳回执；op=7 要求重连；op=9 非法 session（要重新 IDENTIFY）。
  4. 回消息（被动回复，须在 5 分钟内带 msg_id）：
       群聊   POST https://api.sgroup.qq.com/v2/groups/{group_openid}/messages
       单聊   POST https://api.sgroup.qq.com/v2/users/{user_openid}/messages
       频道   POST https://api.sgroup.qq.com/channels/{channel_id}/messages
     体：{"content": "...", "msg_type": 0, "msg_id": "<被动回复凭据>", "msg_seq": n}
     同一条消息的多段回复要递增 msg_seq，否则会被当成重复消息丢弃。

用法：
    python qq_main.py                 # 等价于 python main.py qq
    python qq_main.py --check         # 只校验配置并取一次 token（不连网关）
    python qq_main.py --intents 33554432
"""

import argparse
import asyncio
import inspect
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, NamedTuple, Optional

# 🌟 QQ 接口在国内直连即可；如果本机挂着 Clash 之类的代理，把这两个域名排除掉更稳
#    （和 feishu_main.py 顶部同样的处理）。
for _domain in ("api.sgroup.qq.com", "bots.qq.com", "*.qq.com"):
    _existing = os.environ.get("NO_PROXY", "")
    if _domain not in _existing:
        os.environ["NO_PROXY"] = f"{_existing},{_domain}".strip(",")
os.environ.setdefault("no_proxy", os.environ.get("NO_PROXY", ""))

import httpx  # noqa: E402
from websockets.asyncio.client import connect as ws_connect  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys  # noqa: E402

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from api import channel_router  # noqa: E402
from channels import agent_router, chat_text  # noqa: E402

DEFAULT_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
DEFAULT_API_BASE = "https://api.sgroup.qq.com"
# 官方 v2 文档：GET /gateway → {"url": "wss://api.sgroup.qq.com/websocket"}
DEFAULT_WS_URL = "wss://api.sgroup.qq.com/websocket"

# 取网关地址时按顺序试的路径。
#   ⚠️ `/websocket/` 本身就是 WebSocket 入口，普通 GET 会被 TAPISIX 挡回来：
#      HTTP 426 WebSocket protocol violation: Upgrade header "" does not contain websocket
#   官方 v2 的「获取通用 WSS 接入点」是 `/gateway`（注意：带尾斜杠会 404）。
GATEWAY_PATHS = ("/gateway", "/websocket/")

# 官方 intents 位（v2）。默认只要"群聊/单聊 @我"（GROUP_AND_C2C_EVENT，需要申请开通）
# 加上"频道 @我"（PUBLIC_GUILD_MESSAGES）、"频道私信"（DIRECT_MESSAGE）
# 与"互动事件"（INTERACTION，按钮点击要用）。
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30
INTENT_DIRECT_MESSAGE = 1 << 12
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_INTERACTION = 1 << 26
DEFAULT_INTENTS = (
    INTENT_GROUP_AND_C2C | INTENT_PUBLIC_GUILD_MESSAGES | INTENT_DIRECT_MESSAGE | INTENT_INTERACTION
)

# 互动事件类型（INTERACTION_CREATE 的 d.type）
INTERACTION_BUTTON = 11          # 消息按钮点击（需要 PUT /interactions/{id} 回应）
INTERACTION_QUICK_MENU = 12      # 单聊快捷菜单
INTERACTION_CLEAR_SESSION = 14   # 用户清空了会话历史

# 审批按钮的 data → 等价于玩家回复的文本
BUTTON_APPROVE = "approve"
BUTTON_TEST = "test"
BUTTON_CANCEL = "cancel"
BUTTON_COMMANDS = {
    BUTTON_APPROVE: "y",
    BUTTON_TEST: "t",
    BUTTON_CANCEL: "取消",
}

# 单聊自定义菜单（`PUT /v2/menu`）：官方文档里它**不需要内邀也不需要申请**，
# 展示在单聊窗口底部，点击后把 send_message 的文本自动填进输入框（用户再按一下发送）。
# 拿不到消息按钮权限时，这就是最接近"按钮"的方案。
# ⚠️ name 限制 10 个字符，且**一个汉字算 2 个字符**，所以名字要短。
MENU_ITEMS = (
    {"type": "send_message", "name": "✅ 执行", "send_message": "y"},
    {"type": "send_message", "name": "🧪 测试", "send_message": "t"},
    {"type": "send_message", "name": "🚫 取消", "send_message": "取消"},
)
MENU_NAME_LIMIT = 10

# 单条消息长度上限（官方文档是 1000+ 字符，留点余量分段发）
MAX_CHUNK = 800
# 一条回复最多切几段：再多就是刷屏了，剩下的提示玩家去电脑端看
MAX_CHUNKS = 4
# 单实例锁（防止两个进程用同一个 AppID 抢会话）
LOCK_FILE_NAME = "qq_bot.lock"

# 被动回复凭据 5 分钟有效；msg_seq 记账留 6 分钟就够（过期自动清理）
REPLY_SEQ_TTL = 360
# 官方限制：同一条玩家消息最多回复 5 次（分段的每一段也算一次）
MAX_REPLIES_PER_MESSAGE = 5
# "消息被去重，请检查请求msgseq" —— 说明 (msg_id, msg_seq) 这个组合用过了
DEDUPE_ERROR_CODE = 40054005
# "请求参数msg_id无效或越权" —— 凭据过期（被动回复只有 5 分钟）或不是发给本机器人的
CREDENTIAL_ERROR_CODE = 40034024
# 迟到通知最多排队多少条（防止玩家一直不来、队列无限长）
PENDING_QUEUE_LIMIT = 5
PENDING_FILE_NAME = "qq_pending_notices.json"


class SendOutcome(NamedTuple):
    """一段消息的发送结果：成功 / 失败原因。"""

    ok: bool
    reason: str = ""        # "" | "dedupe" | "credential" | "other"
    response: Any = None


def _classify_failure(response) -> str:
    """把平台的报错翻译成三种可处理的原因。

    * `dedupe`：msg_seq 撞车 → 换一个序号能解决；
    * `credential`：被动凭据无效/过期（或回复额度用光）→ 只能改走主动消息或排队补发；
    * `other`：其它错误（比如按钮没权限）→ 已由降级逻辑处理。
    """
    if response is None or response.status_code in (200, 201, 204):
        return ""
    text = str(getattr(response, "text", "") or "")
    code = ""
    try:
        code = str((response.json() or {}).get("code") or "")
    except Exception:
        code = ""
    if str(DEDUPE_ERROR_CODE) in text or code == str(DEDUPE_ERROR_CODE) or "去重" in text or "msgseq" in text.lower():
        return "dedupe"
    if (
        str(CREDENTIAL_ERROR_CODE) in text
        or code == str(CREDENTIAL_ERROR_CODE)
        or "msg_id" in text
        or "event_id" in text
        or "越权" in text
    ):
        return "credential"
    return "other"


def _is_dedupe_error(response) -> bool:
    """这个响应是不是"msg_seq 重复"（而不是别的问题）。"""
    return _classify_failure(response) == "dedupe"


def _report_send_failure(future) -> None:
    """`send_soon` 丢掉的那个 future 的收尾：失败要打成日志，别静默。

    （协程被取消不算失败：那是机器人正在退出、队列里排着的消息没来得及发。）
    """
    try:
        future.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"❌ QQ 消息发送失败：{exc}")


def _response_brief(response) -> str:
    if response is None:
        return "（没有响应）"
    return f"HTTP {response.status_code} {str(getattr(response, 'text', '') or '')[:200]}"

# @提及标记：`<@!1234>` / `<@1234>`（QQ 群里 @机器人 时官方会带上）
_MENTION_RE = re.compile(r"<@!?[^>]{0,64}>")

CONVERSATION_GROUP = "group"
CONVERSATION_C2C = "c2c"
CONVERSATION_GUILD = "guild"


def _button(button_id: str, label: str, data: str, style: int = 1, modal: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """一个回调按钮（action.type=1：点击后平台把 data 回调给我们）。

    * `permission.type=2` 所有人可点 —— 真正的白名单校验在后端（`_allowed`）；
    * `group_id` 让同一组按钮"点一个其余变灰"，避免重复点"执行"下发两次；
    * `unsupport_tips` 给低版本 QQ 兜底提示（让他们直接回 y / t）。
    """
    action: Dict[str, Any] = {
        "type": 1,
        "permission": {"type": 2},
        "data": data,
        "unsupport_tips": "QQ 版本过低，直接在群里回复 y（执行）或 t（仅改配置）即可",
    }
    if modal:
        action["modal"] = modal
    return {
        "id": button_id,
        "render_data": {"label": label, "visited_label": f"{label} ✓", "style": style},
        "action": action,
        "group_id": "gi-agent-approval",
    }


def approval_keyboard() -> Dict[str, Any]:
    """审批屏底部的按钮：执行 / 仅改配置 / 取消。

    点了就等于回复 `y` / `t` / `取消`（见 `BUTTON_COMMANDS`），走的是同一套审批逻辑。
    "执行"会真的拉起 BetterGI，所以配了二次确认弹窗（官方 modal，确认文案 ≤4 字）。
    """
    return {
        "content": {
            "rows": [
                {
                    "buttons": [
                        _button(
                            BUTTON_APPROVE, "✅ 执行", BUTTON_APPROVE, style=1,
                            modal={
                                "content": "将关闭 BetterGI 并写入配置，然后启动一条龙。确定吗？",
                                "confirm_text": "执行",
                                "cancel_text": "再想想",
                            },
                        ),
                        _button(BUTTON_TEST, "🧪 仅改配置", BUTTON_TEST, style=0),
                        _button(BUTTON_CANCEL, "🚫 取消", BUTTON_CANCEL, style=0),
                    ]
                }
            ]
        }
    }


def is_approval_message(text: str) -> bool:
    """这条消息是不是"审批屏"（只有它才挂按钮）。

    审批屏由 `brain/llm_brain.build_approval_message` 生成，开头就是 `🛑 [系统拦截]`。
    """
    from brain import llm_brain

    return llm_brain.APPROVAL_HEADER in str(text or "")


class QQBotError(RuntimeError):
    """QQ 接口层面的错误（配置缺失、鉴权失败、网关异常…）。"""


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name) or default).strip()


def _parse_menu_commands(raw: str) -> Dict[str, str]:
    """解析 `QQ_BOT_MENU_COMMANDS`：`菜单功能ID=approve,另一个=test`（也认中文逗号）。"""
    mapping: Dict[str, str] = {}
    for item in str(raw or "").replace("，", ",").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if key and value in BUTTON_COMMANDS:
            mapping[key] = value
    return mapping


def _header_kwarg(connector) -> str:
    """websockets 14+ 的额外请求头参数叫 `additional_headers`，老版本叫 `extra_headers`。

    返回该参数名；都认不出来（例如测试里的假连接器）就返回空串，调用方跳过传头。
    """
    try:
        params = inspect.signature(connector).parameters
    except (TypeError, ValueError):  # pragma: no cover - 假对象/内建函数
        return ""
    for name in ("additional_headers", "extra_headers"):
        if name in params:
            return name
    return ""


REPLY_MODE_COMPACT = "compact"
REPLY_MODE_FULL = "full"
# 单向模式：只收指令，不回话。Agent 的审批屏 / 完成报告只打到本地终端与 Studio 日志
# （见 api/channel_router.set_input_only）。适合"手机发指令、人就在电脑前看着"的用法。
REPLY_MODE_OFF = "off"
_REPLY_MODES = (REPLY_MODE_COMPACT, REPLY_MODE_FULL, REPLY_MODE_OFF)


class QQBotConfig:
    """从 .env 读出来的 QQ 机器人配置。"""

    def __init__(self, appid: str = "", secret: str = "", intents: Optional[int] = None,
                 allowed_users: str = "", allow_anyone: bool = False,
                 token_url: str = "", api_base: str = "", reconnect_delay: float = 5.0,
                 ws_url: str = "", reply_mode: str = REPLY_MODE_COMPACT,
                 buttons: bool = True, menu_commands: str = ""):
        self.appid = str(appid or "").strip()
        self.secret = str(secret or "").strip()
        self.intents = DEFAULT_INTENTS if intents is None else int(intents)
        self.allowed_users = [
            item.strip() for item in str(allowed_users or "").replace("，", ",").split(",") if item.strip()
        ]
        self.allow_anyone = bool(allow_anyone)
        self.token_url = token_url or DEFAULT_TOKEN_URL
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.ws_url = ws_url or DEFAULT_WS_URL
        # compact（默认）：审批消息只发"要执行什么"，完整推理留在电脑端日志
        # full：连推理正文一起发（手机上会刷屏、还可能被切成好几条）
        # off：单向模式 —— 一句都不回，全部只在本地显示（审批在电脑上做）
        mode = str(reply_mode or "").strip().lower()
        self.reply_mode = mode if mode in _REPLY_MODES else REPLY_MODE_COMPACT
        # 审批屏是否挂按钮（执行 / 仅改配置 / 取消）。平台没开通按钮权限时会自动退回纯文本。
        self.buttons = bool(buttons)
        # 单聊快捷菜单的功能 ID → 按钮标识（`QQ_BOT_MENU_COMMANDS="我的ID=approve,另一个ID=test"`）
        self.menu_commands = _parse_menu_commands(menu_commands)
        self.reconnect_delay = float(reconnect_delay or 5.0)

    @property
    def concise(self) -> bool:
        return self.reply_mode != REPLY_MODE_FULL

    @property
    def replies_disabled(self) -> bool:
        """单向模式：只收指令、不回话（回话改成本地回显）。"""
        return self.reply_mode == REPLY_MODE_OFF

    @classmethod
    def from_env(cls) -> "QQBotConfig":
        raw_intents = _env("QQ_BOT_INTENTS")
        intents = None
        if raw_intents:
            try:
                intents = int(raw_intents, 0)
            except ValueError:
                print(f"⚠️ QQ_BOT_INTENTS 不是数字，改用默认值：{raw_intents}")
        return cls(
            appid=_env("QQ_BOT_APPID"),
            secret=_env("QQ_BOT_SECRET"),
            intents=intents,
            allowed_users=_env("QQ_BOT_ALLOWED_USERS"),
            allow_anyone=_env("QQ_BOT_ALLOW_ANYONE") == "1",
            token_url=_env("QQ_BOT_TOKEN_URL") or DEFAULT_TOKEN_URL,
            api_base=_env("QQ_BOT_API_BASE") or DEFAULT_API_BASE,
            ws_url=_env("QQ_BOT_WS_URL") or DEFAULT_WS_URL,
            reply_mode=_env("QQ_BOT_REPLY_MODE") or REPLY_MODE_COMPACT,
            buttons=_env("QQ_BOT_BUTTONS") != "0",
            menu_commands=_env("QQ_BOT_MENU_COMMANDS"),
            reconnect_delay=float(_env("QQ_BOT_RECONNECT_SECONDS") or 5.0),
        )

    @property
    def configured(self) -> bool:
        return bool(self.appid and self.secret)

    def target_prefix(self) -> str:
        return channel_router.QQ_PREFIX

    def describe(self) -> str:
        return (
            f"AppID {self.appid or '（未配置）'}｜intents={self.intents}"
            f"｜白名单={'、'.join(self.allowed_users) if self.allowed_users else ('不限（危险）' if self.allow_anyone else '未设置')}"
        )


def strip_mentions(content: str) -> str:
    """QQ 群里 @机器人 的消息会带 `<@!1234>` / `<@1234>` 这类提及标记，去掉后再交给 Agent。

    （频道里还会带 `@机器人` 之类纯文本昵称，这种无法可靠识别，交给大模型忽略即可。
    注意别用"删掉 `>` 之前所有内容"这种粗暴做法——那会把消息正文一起吃掉。）
    """
    text = _MENTION_RE.sub("", str(content or ""))
    return text.strip()


def split_message(text: str, limit: int = MAX_CHUNK, max_chunks: int = MAX_CHUNKS) -> List[str]:
    """把长回复切段。

    切法：**先在换行处切**（聊天里按段落看最舒服），整段没有换行时才退到句号/逗号/空格，
    最后才硬切 —— 别把一行劈成两半（实测过：审批屏的「🔎 角色解析：蓝砚」和后半句
    被切到了两条消息里，看着莫名其妙）。

    超过 `max_chunks` 段就截断，并在末尾说明"剩下的看电脑端"，免得刷屏。
    """
    text = str(text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 5:
            # 这一段基本没有换行：退而求其次按句子/标点切
            cut = max(window.rfind("。"), window.rfind("；"), window.rfind("，"), window.rfind(" "))
        if cut < limit // 5:
            cut = limit          # 一整行都超长，只能硬切
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
        if len(chunks) >= max_chunks:
            remaining = ""
            note = _truncated_note(text, chunks)
            # 给"已省略"那句留出位置，别把最后一段顶过单条上限
            chunks[-1] = chunks[-1][: max(0, limit - len(note))].rstrip() + note
            break
    if remaining:
        chunks.append(remaining)
    return chunks


def _truncated_note(original: str, chunks: List[str]) -> str:
    return f"\n…（内容较长，已省略后续 {max(0, len(original) - sum(len(c) for c in chunks))} 字；完整内容见电脑端日志）"


def build_target(kind: str, conversation_id: str, msg_id: str = "", event_id: str = "") -> str:
    """会话标识。

    * `qq:<kind>:<id>#<msg_id>` —— 回复某条消息（被动回复凭据，5 分钟有效）
    * `qq:<kind>:<id>#event:<event_id>` —— 回复某次互动（按钮点击），凭据要放进 `event_id` 字段

    官方要求这两种凭据"二选一"，字段名不一样（`msg_id` / `event_id`），所以这里带前缀区分。
    """
    base = f"{channel_router.QQ_PREFIX}{kind}:{conversation_id}"
    if event_id:
        return f"{base}#event:{event_id}"
    return f"{base}#{msg_id}" if msg_id else base


def parse_target(target: str) -> Dict[str, str]:
    """解析会话标识，返回 {kind, id, msg_id, event_id}；不是 QQ 会话就抛错。"""
    text = str(target or "")
    if not text.startswith(channel_router.QQ_PREFIX):
        raise QQBotError(f"不是 QQ 会话：{text}")
    body = text[len(channel_router.QQ_PREFIX):]
    raw_credential = ""
    if "#" in body:
        body, raw_credential = body.split("#", 1)
    kind, _, conversation_id = body.partition(":")
    if not kind or not conversation_id:
        raise QQBotError(f"QQ 会话格式不对：{text}")

    msg_id = ""
    event_id = ""
    if raw_credential.startswith("event:"):
        event_id = raw_credential[len("event:"):]
    else:
        msg_id = raw_credential
    return {"kind": kind, "id": conversation_id, "msg_id": msg_id, "event_id": event_id}


class QQBotClient:
    """一个 QQ 机器人的最小实现：取 token、连网关、收事件、回消息。"""

    def __init__(self, config: QQBotConfig, api_base: str = "", token_url: str = "",
                 connector: Any = None, on_message: Any = None):
        self.config = config
        self.api_base = (api_base or config.api_base).rstrip("/")
        self.token_url = token_url or config.token_url
        self._access_token = ""
        self._token_expire_at = 0.0
        self._connector = connector or ws_connect      # 方便测试替换
        self._on_message = on_message or self._default_on_message
        self._http: Optional[httpx.AsyncClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._seq: Optional[int] = None
        self._session_id = ""
        self._task: Optional[asyncio.Task] = None
        # 平台没开通按钮权限时会置 False（只提示一次，之后不再尝试）
        self._buttons_enabled = bool(config.buttons)
        # 被动回复序号记账：凭据 → (已用到第几个 msg_seq, 记账时间)
        self._reply_seq: Dict[str, Any] = {}
        # 发不出去的"迟到通知"排队（按会话），玩家下次说话时补发
        self._pending: Dict[str, List[str]] = {}
        self._pending_lock = threading.Lock()

    # ---------------- HTTP ----------------
    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def fetch_access_token(self, force: bool = False) -> str:
        """取（或复用）access_token；快过期时自动续。"""
        now = time.time()
        if not force and self._access_token and now < self._token_expire_at - 60:
            return self._access_token
        if not self.config.configured:
            raise QQBotError("没配置 QQ_BOT_APPID / QQ_BOT_SECRET")

        client = await self._client()
        response = await client.post(
            self.token_url,
            json={"appId": self.config.appid, "clientSecret": self.config.secret},
        )
        if response.status_code != 200:
            raise QQBotError(f"取 token 失败：HTTP {response.status_code} {response.text[:200]}")
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise QQBotError(f"取 token 失败：{payload}")
        expires = payload.get("expires_in") or payload.get("expiresIn") or 7200
        try:
            expires_seconds = float(expires)
        except (TypeError, ValueError):
            expires_seconds = 7200.0
        self._access_token = token
        # 官方有时返回倒数字符串，这里做个兜底：太小就按 2 小时算
        self._token_expire_at = now + (expires_seconds if expires_seconds > 120 else 7200.0)
        print(f"🔑 已获取 QQ access_token（有效期 {int(self._token_expire_at - now)} 秒）")
        return token

    async def request_gateway(self) -> str:
        """取 WSS 接入点。

        ⚠️ 官方 v2 的接口是 `GET /gateway`（[文档](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/gateway.get.html)）。
        别拿 `/websocket/` 当接口用 —— 那个路径本身就是 WebSocket 入口，普通 GET 会被
        网关挡回来：`HTTP 426 WebSocket protocol violation: Upgrade header "" does not contain websocket`。
        两个路径都拿不到时，直接退回官方 WSS 地址（连的时候带上 Authorization 头就行）。
        """
        token = await self.fetch_access_token()
        client = await self._client()
        failures = []
        for path in GATEWAY_PATHS:
            try:
                response = await client.get(
                    f"{self.api_base}{path}",
                    headers={"Authorization": f"QQBot {token}"},
                )
            except Exception as exc:
                failures.append(f"{path}: {exc}")
                continue
            if response.status_code != 200:
                failures.append(f"{path}: HTTP {response.status_code} {response.text[:120].strip()}")
                continue
            try:
                url = str((response.json() or {}).get("url") or "")
            except Exception as exc:
                failures.append(f"{path}: 响应不是 JSON（{exc}）")
                continue
            if url:
                return url
            failures.append(f"{path}: 响应里没有 url")
        print("⚠️ 取网关地址的接口没成功：" + "；".join(failures))
        print(f"   ↳ 直接连官方地址：{self.config.ws_url}（WebSocket 握手时带上 Authorization 头）")
        return self.config.ws_url

    # ---------------- WebSocket 连接参数 ----------------
    def _connect_kwargs(self) -> Dict[str, Any]:
        """WebSocket 连接参数：除了 ping/大小，还要把 access_token 放进请求头。

        官方的「通用 WSS 接入点」直接连时需要 `Authorization: QQBot <token>`；
        从接口拿到的地址带不带都行，带上更保险。
        """
        kwargs: Dict[str, Any] = {"ping_interval": None, "max_size": 2 ** 22}
        header_kwarg = _header_kwarg(self._connector)
        if header_kwarg and self._access_token:
            kwargs[header_kwarg] = {"Authorization": f"QQBot {self._access_token}"}
        return kwargs

    # ---------------- 发消息 ----------------
    def _reply_endpoint(self, kind: str, conversation_id: str) -> str:
        if kind == CONVERSATION_GROUP:
            return f"{self.api_base}/v2/groups/{conversation_id}/messages"
        if kind == CONVERSATION_C2C:
            return f"{self.api_base}/v2/users/{conversation_id}/messages"
        if kind in (CONVERSATION_GUILD, "channel"):
            return f"{self.api_base}/channels/{conversation_id}/messages"
        raise QQBotError(f"不支持的会话类型：{kind}")

    async def send_message(self, target: str, text: str, keyboard: Optional[Dict[str, Any]] = None) -> bool:
        """给 QQ 会话回消息（自动分段 + 按凭据分配 msg_seq）。

        传了 `keyboard` 就用"Markdown + 内嵌键盘"（msg_type=2）发 —— 官方文档里按钮是挂在
        markdown 消息底部的（[消息按钮](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/msg-btn.html)）。
        按钮只挂最后一段，免得每条分段都挂一排。

        ⚠️ `msg_seq` 不是"这次消息的第几段"，而是**对同一条玩家消息的第几次回复**：
        官方规定 `msg_id + msg_seq` 不能重复（重复就是 `HTTP 400 40054005 消息被去重`），
        而且同一条消息最多回复 5 次。所以"计划已撤销" + "新的审批屏"这种一次发两条的场景，
        必须用 1、2，而不是两条都用 1 —— 见 `_next_seq`。
        """
        info = parse_target(target)
        chunks = split_message(text)
        if not chunks:
            return True

        token = await self.fetch_access_token()
        client = await self._client()
        endpoint = self._reply_endpoint(info["kind"], info["id"])
        headers = {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.config.appid,
        }

        want_buttons = bool(keyboard) and self._buttons_enabled and self._can_have_buttons(info["kind"])
        ok = True
        for index, chunk in enumerate(chunks, start=1):
            buttons = keyboard if (want_buttons and index == len(chunks)) else None
            if await self._deliver(chunk, buttons, client, endpoint, headers, info):
                continue
            ok = False
        return ok

    async def _deliver(self, chunk, buttons, client, endpoint, headers, info) -> bool:
        """把一段话送到玩家手里：被动回复 → （序号冲突就换号）→ 主动消息 → 排队等下次补发。

        最后两级是为"迟到的通知"准备的：一条龙跑 23 分钟，完成报告发出来时那条被动回复凭据
        （5 分钟有效）早就过期了，而且对同一条玩家消息的 5 次回复额度也可能用光。
        这种消息绝不能静默丢掉 —— 先试主动消息（官方允许，只是有频控），
        再不行就排队，等玩家下次说话时用新凭据补发。
        """
        seq = self._next_seq(info)
        if seq is None:
            print(
                f"⚠️ 这条消息已经回复满 {MAX_REPLIES_PER_MESSAGE} 次（官方上限）。"
                "改用主动消息，再不行就排队补发。"
            )
            return await self._send_late(chunk, buttons, client, endpoint, headers, info)

        outcome = await self._send_chunk(client, endpoint, headers, info, chunk, seq, buttons)
        if outcome.ok:
            self._commit_seq(info, seq)
            return True

        if outcome.reason == "dedupe":
            # 平台说这个序号用过了：可能是机器人重启过（计数器在内存里）或上一条没记账。
            # 把这个序号记成"已用"，换下一个再发一次。
            self._commit_seq(info, seq)
            retry_seq = self._next_seq(info)
            print(f"↻ msg_seq={seq} 被判重复，改用 msg_seq={retry_seq} 重发")
            if retry_seq is not None:
                outcome = await self._send_chunk(client, endpoint, headers, info, chunk, retry_seq, buttons)
                if outcome.ok:
                    self._commit_seq(info, retry_seq)
                    return True

        if outcome.reason in ("dedupe", "credential"):
            return await self._send_late(chunk, buttons, client, endpoint, headers, info)
        return False

    async def _send_late(self, chunk, buttons, client, endpoint, headers, info) -> bool:
        """被动回复这条路走不通时的兜底：① 主动消息 ② 排队等玩家下次说话。"""
        # ⚠️ 这里必须看 .ok：SendOutcome 是个 NamedTuple，直接当布尔用永远是 True
        outcome = await self._send_chunk(client, endpoint, headers, info, chunk, None, buttons, active=True)
        if outcome.ok:
            print("↳ 被动凭据不可用（多半超过 5 分钟或额度用光），已改用主动消息发送。")
            return True
        print("↳ 主动消息也发不出去（平台可能没给主动消息额度），已排队：玩家下次说话时用新凭据补发。")
        self.queue_notice(info, chunk)
        return False

    def conversation_key(self, info: Dict[str, str]) -> str:
        """会话标识（不带凭据）：排队补发按"会话"存，不按某条消息。"""
        return f"{info['kind']}:{info['id']}"

    # ---------------- 迟到通知排队 ----------------
    def queue_notice(self, info: Dict[str, str], text: str) -> None:
        """把发不出去的通知按会话排进队列（带数量上限，免得越堆越多）。"""
        key = self.conversation_key(info)
        queue = self._pending.setdefault(key, [])
        queue.append(str(text))
        del queue[:-PENDING_QUEUE_LIMIT]
        self._save_pending()
        print(f"   ↳ 已排队 {len(queue)} 条待补发通知（{key}）")

    def flush_pending(self, target: str) -> None:
        """玩家又说话了：把之前排队的通知用这次的凭据补发（本函数要在后台线程里调）。"""
        info = parse_target(target)
        key = self.conversation_key(info)
        # ⚠️ 必须先复制一份再遍历：补发失败的条目会走 _send_late → queue_notice，
        # 那是往**同一个列表对象**里 append —— 边遍历边改会让同一条通知被反复投递、
        # 队列被自己复制满（实测 2 条变成 5 次发送 + 队列里全是重复项）。
        queue = list(self._pending.get(key) or [])
        if not queue:
            return
        print(f"\n📮 补发 {len(queue)} 条之前发不出去的通知…")
        remaining: List[str] = []
        for index, text in enumerate(queue):
            try:
                if not self.send_threadsafe(target, text):
                    remaining.append(text)
            except Exception as exc:      # pragma: no cover - 网络问题
                print(f"⚠️ 补发失败：{exc}")
                remaining.extend(queue[index:])
                break
        # 去重 + 保持顺序，免得同一句被重试多次堆起来
        remaining = list(dict.fromkeys(remaining))
        if remaining:
            self._pending[key] = remaining
        else:
            self._pending.pop(key, None)
        self._save_pending()

    def _pending_path(self) -> str:
        return os.path.join(PROJECT_ROOT, "memory", PENDING_FILE_NAME)

    # ---------------- 跨进程推送队列（别的进程排进来的通知） ----------------

    PUSH_DRAIN_INTERVAL = 15.0

    def _start_push_drain(self) -> None:
        """起一个后台线程，定期把 `memory/agent_push_queue.json` 里的推送发出去。"""
        if getattr(self, "_push_thread", None):
            return
        self._push_stop = threading.Event()
        self._push_thread = threading.Thread(
            target=self._push_drain_loop, name="qq-push-drain", daemon=True)
        self._push_thread.start()

    def _push_drain_loop(self) -> None:
        """定期取走队列里的推送并发送；发不出去就退回自己的补发队列（玩家下次说话时补）。"""
        while not getattr(self, "_push_stop", None) or not self._push_stop.is_set():
            try:
                self.drain_external_pushes()
            except Exception as exc:        # noqa: BLE001 —— 后台线程不能因为一条消息死掉
                print(f"⚠️ 取外部推送失败（{type(exc).__name__}: {exc}）")
            time.sleep(self.PUSH_DRAIN_INTERVAL)

    def drain_external_pushes(self, limit=5) -> int:
        """取走并发送跨进程推送，返回成功发出的条数。"""
        from api import notice_queue

        items = notice_queue.drain(limit=limit)
        if not items:
            return 0
        sent = 0
        for target, text in items:
            if self.config.replies_disabled:
                # 单向模式：不发到 QQ，但内容一定要留痕（否则这条推送就消失了）
                channel_router.echo_locally("QQ（单向模式）", text)
                sent += 1
                continue
            try:
                if self.send_threadsafe(target, text):
                    sent += 1
                    print(f"📤 已把跨进程推送发给 {target}")
                else:
                    # 主动消息也没发出去（平台没给额度）→ 交给原来的补发队列
                    self.queue_notice(parse_target(target), text)
            except Exception as exc:        # noqa: BLE001
                print(f"⚠️ 发送跨进程推送失败（{target}）：{exc}")
                self.queue_notice(parse_target(target), text)
        return sent

    def _save_pending(self) -> None:
        """把待补发队列落盘（**原子写** + 加锁）。

        写它的线程至少有两个：`flush_pending` 的后台线程和事件循环线程（queue_notice）。
        原来直接 `open(path, "w")` 截断再 dump，两边重叠就会写出半截 JSON，
        下次启动 `_load_pending` 把它当"没有" —— 攒下的迟到通知静默丢掉。
        """
        with self._pending_lock:
            try:
                path = self._pending_path()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                temp_path = f"{path}.tmp"
                with open(temp_path, "w", encoding="utf-8") as handle:
                    json.dump(self._pending, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, path)
            except OSError as exc:            # pragma: no cover - 磁盘问题不该影响机器人
                print(f"⚠️ 待补发通知存盘失败：{exc}")
            finally:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except (OSError, UnboundLocalError):
                    pass

    def _load_pending(self) -> None:
        try:
            with open(self._pending_path(), "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._pending = {str(k): [str(item) for item in (v or [])] for k, v in data.items()}
                if self._pending:
                    print(f"📮 有 {sum(len(v) for v in self._pending.values())} 条待补发通知（玩家说话时会自动补发）")
        except FileNotFoundError:
            pass
        except Exception as exc:          # pragma: no cover - 文件坏了就当时没有
            print(f"⚠️ 待补发通知读取失败（忽略）：{exc}")

    # ---------------- msg_seq 记账 ----------------
    def _seq_key(self, info: Dict[str, str]) -> str:
        """按"被动回复凭据"记账：同一条玩家消息（或同一次按钮点击）共用一个计数器。"""
        if info.get("event_id"):
            return f"{info['kind']}:{info['id']}#event:{info['event_id']}"
        if info.get("msg_id"):
            return f"{info['kind']}:{info['id']}#{info['msg_id']}"
        return ""      # 主动消息（没有凭据）：序号从 1 开始，不记账

    def _next_seq(self, info: Dict[str, str]) -> Optional[int]:
        """下一个可用的 msg_seq；额度用完返回 None。"""
        key = self._seq_key(info)
        if not key:
            return 1
        now = time.time()
        used, _at = self._reply_seq.get(key, (0, now))
        if used >= MAX_REPLIES_PER_MESSAGE:
            return None
        return used + 1

    def _commit_seq(self, info: Dict[str, str], seq: int) -> None:
        """把某个序号记成"已用"（发成功、或平台说它重复时都要记）。"""
        key = self._seq_key(info)
        if not key:
            return
        used, _at = self._reply_seq.get(key, (0, time.time()))
        self._reply_seq[key] = (max(used, int(seq)), time.time())
        self._purge_seq()

    def _purge_seq(self) -> None:
        """凭据 5 分钟就过期了，过期的记账顺手清掉，免得字典无限长。"""
        now = time.time()
        for key in [k for k, (_seq, at) in self._reply_seq.items() if now - at > REPLY_SEQ_TTL]:
            self._reply_seq.pop(key, None)

    @staticmethod
    def _can_have_buttons(kind: str) -> bool:
        """按钮只在群聊/单聊上有意义：频道走的是另一套发送接口，点了我们也没法回。"""
        return kind in (CONVERSATION_GROUP, CONVERSATION_C2C)

    async def _send_chunk(self, client, endpoint, headers, info, chunk, seq, keyboard,
                          active: bool = False) -> "SendOutcome":
        """发一段，返回 `SendOutcome`（是否成功 + 失败原因）。

        带按钮时逐级降级：**markdown+按钮 → 纯文本+按钮 → 纯文本**。
        分成三级是因为两种失败原因不一样：平台可能只是不吃 群聊 markdown（但按钮是好的），
        也可能压根没开通按钮权限。只有两条带按钮的路都失败，才认定"按钮不可用"并记住，
        免得之后每一条消息都白试三遍。

        `active=True`：不带被动凭据的主动消息（用于迟到通知）。
        """
        modes: List[Any] = []
        if keyboard and self._buttons_enabled:
            modes = [("markdown", True), ("text", True)]
        modes.append(("text", False))

        last_response = None
        button_failed = False
        for mode, with_buttons in modes:
            body = self._build_body(info, chunk, seq, mode, with_buttons, keyboard, active=active)
            try:
                response = await client.post(endpoint, json=body, headers=headers)
            except Exception as exc:
                print(f"❌ QQ 消息发送异常：{exc}")
                return SendOutcome(False, "other", None)
            if response.status_code in (200, 201, 204):
                if with_buttons:
                    return SendOutcome(True, "", response)     # 带按钮发成功了
                if button_failed and last_response is not None:
                    # 两条带按钮的路都失败、最后靠纯文本才发出去 → 认定按钮不可用
                    self._buttons_unavailable(last_response, fallback="纯文本")
                return SendOutcome(True, "", response)
            last_response = response
            if with_buttons:
                button_failed = True

        reason = _classify_failure(last_response)
        if reason == "dedupe":
            print(f"⚠️ QQ 消息被判重复：{_response_brief(last_response)}")
        elif reason == "credential":
            print(f"⚠️ 被动回复凭据不可用：{_response_brief(last_response)}")
        else:
            print(f"❌ QQ 消息发送失败：{_response_brief(last_response)}")
        return SendOutcome(False, reason, last_response)

    @staticmethod
    def _build_body(info: Dict[str, str], chunk: str, seq: Optional[int], mode: str = "text",
                    with_buttons: bool = False, keyboard=None, active: bool = False) -> Dict[str, Any]:
        """组装消息体。

        * `mode="markdown"`：`msg_type=2` + `markdown.content`（官方按钮示例就是这么发的）；
          注意传了 markdown 就不能再传 `content`；
        * `mode="text"`：`msg_type=0` + `content`。
        * 被动回复凭据：普通消息用 `msg_id`，按钮点击后用 `event_id`（官方要求二选一）；
          `active=True` 时两个都不带 —— 那是主动消息（迟到通知用）。
        """
        if with_buttons and keyboard:
            if mode == "markdown":
                body: Dict[str, Any] = {
                    "msg_type": 2,
                    "markdown": {"content": chunk},
                    "keyboard": keyboard,
                }
            else:
                body = {"msg_type": 0, "content": chunk, "keyboard": keyboard}
        else:
            body = {"content": chunk, "msg_type": 0}

        if not active:
            body["msg_seq"] = seq if seq is not None else 1
            if info.get("msg_id"):
                body["msg_id"] = info["msg_id"]
            elif info.get("event_id"):
                body["event_id"] = info["event_id"]
        return body

    def _buttons_unavailable(self, response, fallback: str = "纯文本") -> None:
        """按钮发不出去：提示一次原因，然后本次运行不再尝试（省得每条消息都白试一遍）。"""
        self._buttons_enabled = False
        print(
            f"⚠️ QQ 按钮发送失败：HTTP {response.status_code} {response.text[:160].strip()}\n"
            f"   ↳ 已退回{fallback}（直接回复 y / t 或打字说「取消」一样能用）。"
            "按钮需要在 QQ 开放平台开通：自定义按钮=【内邀开通】，模板按钮=【申请使用】；\n"
            "   ↳ 确认平台没给权限，可以设 QQ_BOT_BUTTONS=0 关掉，免得每次都白试。"
        )

    def send_threadsafe(self, target: str, text: str) -> bool:
        """给**其它线程**（Agent 的逻辑跑在普通线程里）用的投递入口。

        审批屏会自动挂按钮（执行 / 仅改配置 / 取消）：点一下等于回复 `y` / `t` / `取消`。

        ⚠️ 如果调用方其实就在事件循环线程里（比如事件处理函数内部），这里会退化成
        "排进队列但不等待" —— 否则 `future.result()` 会等自己派发出去的协程，自己等自己
        30 秒，消息永远发不出去，还会把心跳饿死（实测踩过，见 `send_soon`）。
        """
        if self._in_event_loop_thread():
            print("⚠️ 在事件循环线程里调了 send_threadsafe，已改成「排进队列不等待」")
            return self.send_soon(target, text)
        if self._loop is None or self._loop.is_closed():
            print("⚠️ QQ 事件循环还没起来，消息没发出去。")
            return False
        keyboard = approval_keyboard() if (self.config.buttons and is_approval_message(text)) else None
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.send_message(target, text, keyboard=keyboard), self._loop
            )
            return bool(future.result(timeout=30))
        except Exception as exc:
            print(f"❌ QQ 消息投递失败：{exc}")
            return False

    def send_soon(self, target: str, text: str) -> bool:
        """从事件循环线程里发消息：**只排队、不等待**（供事件处理函数使用）。

        为什么需要它：`handle_event` 是在 `run_once` 的 `async for` 里同步调用、`handle_interaction`
        是在这个循环里 await 的，两者都在事件循环线程上。这时如果用 `send_threadsafe`，
        它的 `future.result(timeout=30)` 会阻塞住事件循环，而被投递的协程恰恰要在这个循环里才能跑 ——
        自己等自己，30 秒后超时返回 False，那条消息**永远发不出去**（实测：白名单提示 100% 丢失，
        而且这 30 秒里心跳也发不出去）。
        """
        if self._loop is None or self._loop.is_closed():
            print("⚠️ QQ 事件循环还没起来，消息没发出去。")
            return False
        keyboard = approval_keyboard() if (self.config.buttons and is_approval_message(text)) else None
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.send_message(target, text, keyboard=keyboard), self._loop
            )
        except Exception as exc:
            print(f"❌ QQ 消息投递失败：{exc}")
            return False
        future.add_done_callback(_report_send_failure)
        return True

    def _in_event_loop_thread(self) -> bool:
        """当前线程是不是这个客户端的 asyncio 事件循环线程。"""
        if self._loop is None:
            return False
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return False
        return running is self._loop

    async def ack_interaction(self, interaction_id: str, code: int = 0) -> bool:
        """回应互动事件：`PUT /interactions/{interaction_id}`。

        官方要求"收到事件后尽快回应"（指令回调类 3 秒），否则玩家手机上一直转圈到超时。
        所以按钮处理的顺序是：先 ack，再去做（可能的）耗时规划。
        """
        interaction_id = str(interaction_id or "")
        if not interaction_id:
            return False
        token = await self.fetch_access_token()
        client = await self._client()
        try:
            response = await client.put(
                f"{self.api_base}/interactions/{interaction_id}",
                json={"code": int(code)},
                headers={"Authorization": f"QQBot {token}", "X-Union-Appid": self.config.appid},
            )
        except Exception as exc:
            print(f"⚠️ 互动回应失败：{exc}")
            return False
        if response.status_code not in (200, 201, 204):
            print(f"⚠️ 互动回应失败：HTTP {response.status_code} {response.text[:160]}")
            return False
        return True

    # ---------------- 单聊自定义菜单（拿不到消息按钮时的替代方案） ----------------
    @staticmethod
    def _menu_name_len(name: str) -> int:
        """官方口径：一个中文汉字算 2 个字符（`name` 上限 10）。"""
        return sum(2 if ord(char) > 0x2E7F else 1 for char in str(name or ""))

    @classmethod
    def menu_is_ours(cls, menu: Dict[str, Any]) -> bool:
        """这份菜单是不是本程序设的（用 send_message 内容比对）。"""
        items = (menu or {}).get("items") or []
        wanted = {(item["name"], item["send_message"]) for item in MENU_ITEMS}
        got = {(str(item.get("name")), str(item.get("send_message") or "")) for item in items}
        return bool(items) and wanted <= got

    async def fetch_menu(self) -> Dict[str, Any]:
        """查询全局自定义菜单（官方：仅 C2C 单聊场景）。"""
        token = await self.fetch_access_token()
        client = await self._client()
        response = await client.get(
            f"{self.api_base}/v2/menu",
            headers={"Authorization": f"QQBot {token}", "X-Union-Appid": self.config.appid},
        )
        if response.status_code != 200:
            raise QQBotError(f"查询菜单失败：HTTP {response.status_code} {response.text[:160]}")
        return response.json() or {}

    async def setup_menu(self, items=None) -> bool:
        """把「✅ 执行 / 🧪 测试 / 🚫 取消」做成**单聊窗口底部**的自定义菜单。

        为什么需要它：消息按钮（`keyboard`）要平台【内邀开通】/【申请使用】，很多人拿不到；
        而自定义菜单这条路径**没有权限门槛**，点击后把文本自动填进输入框（玩家再按一下发送），
        按钮的 data 正好就是我们的审批词 `y` / `t` / `取消`，所以后端一行都不用改。

        官方文档：`PUT /v2/menu`（[修改全局自定义菜单](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_menu.put.html)），
        注意 name 上限 10 字符（一个汉字算 2 个），items 最多 10 个，仅单聊生效。
        """
        wanted = list(items or MENU_ITEMS)
        for item in wanted:
            length = self._menu_name_len(item.get("name") or "")
            if length > MENU_NAME_LIMIT:
                raise QQBotError(f"菜单名「{item.get('name')}」太长（{length} > {MENU_NAME_LIMIT} 字符）")

        token = await self.fetch_access_token()
        client = await self._client()
        response = await client.put(
            f"{self.api_base}/v2/menu",
            json={"menu": {"items": wanted}},
            headers={"Authorization": f"QQBot {token}", "X-Union-Appid": self.config.appid},
        )
        if response.status_code not in (200, 201, 204):
            print(f"❌ 设置单聊菜单失败：HTTP {response.status_code} {response.text[:200]}")
            return False
        version = ""
        try:
            version = str((response.json() or {}).get("version") or "")
        except Exception:
            pass
        print(f"✅ 已设置单聊自定义菜单（版本 {version or '?'}）："
              + " / ".join(str(item["name"]) for item in wanted))
        print("   效果：在 QQ 单聊窗口底部会出现这三个按钮，点一下就把对应指令填进输入框（再按发送）。")
        return True

    async def ensure_menu(self) -> bool:
        """启动时顺手检查：没有菜单就自动配上；已有别人的菜单就不动（提示怎么覆盖）。"""
        try:
            current = await self.fetch_menu()
        except Exception as exc:
            print(f"ℹ️ 单聊菜单检查跳过（{exc}）")
            return False
        menu = current.get("menu") or {}
        if self.menu_is_ours(menu):
            return True
        if menu.get("items"):
            print("ℹ️ 这台机器人已经有自定义菜单（不是本程序设的），没动它。")
            print("   想换成 GI Agent 的「✅ 执行 / 🧪 测试 / 🚫 取消」：python main.py qq --setup-menu")
            return False
        return await self.setup_menu()

    # ---------------- 收消息 ----------------
    def _allowed(self, user_id: str) -> bool:
        if self.config.allow_anyone:
            return True
        if not self.config.allowed_users:
            return False
        return str(user_id) in self.config.allowed_users

    def _default_on_message(self, text: str, target: str, user_id: str, kind: str) -> None:
        # 先补发"之前发不出去的迟到通知"（在后台线程里做：send_threadsafe 会阻塞等结果）
        threading.Thread(target=self.flush_pending, args=(target,), daemon=True).start()
        agent_router.handle_message_async(text, target)

    @staticmethod
    def _interaction_target(payload: Dict[str, Any], scene: str, event_id: str):
        """从互动事件里取出"回哪儿"和"是谁点的"。

        群聊用 `group_openid` + `group_member_openid`，单聊用 `user_openid`；
        凭据用事件 id（`event_id` 字段），官方允许 `INTERACTION_CREATE` 事件这么回复。
        """
        if scene == "group":
            group_openid = str(payload.get("group_openid") or "")
            user_id = str(payload.get("group_member_openid") or "")
            return build_target(CONVERSATION_GROUP, group_openid, event_id=event_id), user_id
        if scene == "c2c":
            user_openid = str(payload.get("user_openid") or "")
            return build_target(CONVERSATION_C2C, user_openid, event_id=event_id), user_openid
        channel_id = str(payload.get("channel_id") or "")
        return build_target(CONVERSATION_GUILD, channel_id, event_id=event_id), str(
            ((payload.get("data") or {}).get("resolved") or {}).get("user_id") or ""
        )

    async def handle_interaction(self, event: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """处理互动事件（按钮点击 / 清空会话）。

        文档：[互动事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html)
        —— 仅 type=11（消息按钮）与 type=12（快捷菜单）必须回应，收到就得快点 ack，
        所以这里先 ack 再干活。按钮点了什么，由 `d.data.resolved.button_data` 告诉我们。
        """
        payload = event.get("d") or {}
        event_id = str(event.get("id") or payload.get("id") or "")
        interaction_id = str(payload.get("id") or "")
        kind = payload.get("type")
        scene = str(payload.get("scene") or "")
        resolved = (payload.get("data") or {}).get("resolved") or {}
        button_data = str(resolved.get("button_data") or "")

        if kind not in (INTERACTION_BUTTON, INTERACTION_QUICK_MENU, INTERACTION_CLEAR_SESSION):
            return None

        if scene == "guild":
            # 频道消息走另一套发送接口，回不了被动消息：ack 掉，但绝不能"静默执行"危险操作
            await self.ack_interaction(interaction_id, 4)
            print("⚠️ 收到频道场景的互动事件：暂不支持（请用群聊/单聊，或直接回复 y / t）")
            return None

        target, user_id = self._interaction_target(payload, scene, event_id)

        if kind == INTERACTION_CLEAR_SESSION:
            # 玩家清了会话历史：顺手清掉 Agent 记忆（ack 后平台会显示"会话已清空"小灰条）
            await self.ack_interaction(interaction_id, 0)
            print("\n🧹 QQ 会话被清空，同步清掉 Agent 记忆")
            agent_router.handle_message_async("clear", target, interaction_id)
            return {"text": "clear", "target": target, "user_id": user_id, "allowed": "1", "kind": "clear_session"}

        if kind == INTERACTION_QUICK_MENU:
            # 单聊快捷菜单（在开放平台管理端配置，属于"不用内邀也能有按钮"的正路）
            feature_id = str(resolved.get("feature_id") or "")
            mapped = self.config.menu_commands.get(feature_id) or feature_id
            if mapped not in BUTTON_COMMANDS:
                await self.ack_interaction(interaction_id, 0)
                print(
                    f"ℹ️ 收到快捷菜单回调（feature_id={feature_id!r}）：没配映射，已忽略。\n"
                    "   ↳ 想让它生效：把菜单项的功能 ID 设成 approve / test / cancel，"
                    "或设 QQ_BOT_MENU_COMMANDS=你的ID=approve"
                )
                return None
            button_data = mapped

        command = BUTTON_COMMANDS.get(button_data)
        if command is None:
            await self.ack_interaction(interaction_id, 1)
            print(f"⚠️ 未知的按钮 data：{button_data!r}（可能是旧版本消息留下的按钮）")
            return None

        await self.ack_interaction(interaction_id, 0)

        label = {"y": "✅ 执行", "t": "🧪 仅改配置", "取消": "🚫 取消"}.get(command, command)
        source = "快捷菜单" if kind == INTERACTION_QUICK_MENU else "按钮"
        print(f"\n👆 旅行者点了一下{source}：{label}（等价于回复 {command!r}）")
        if not self._allowed(user_id):
            print(f"⛔ 未授权用户：{user_id}（把它加进 .env 的 QQ_BOT_ALLOWED_USERS 才能指挥 Agent）")
            # 这是 async 上下文（在事件循环里跑），直接 await 就行，别用会阻塞的 send_threadsafe
            if not self.config.replies_disabled:
                await self.send_message(target, "⛔ 你这台 Agent 没有把你加入白名单，我不能执行。")
            return {"text": command, "target": target, "user_id": user_id, "allowed": "0", "button": button_data}

        # 走和"玩家打字"完全相同的入口（含去重）：同一次点击不会被处理两遍
        agent_router.handle_message_async(command, target, interaction_id)
        return {"text": command, "target": target, "user_id": user_id, "allowed": "1", "button": button_data}

    def handle_event(self, event: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """把一个事件翻译成 (文本, 会话标识)；不认识的事件返回 None。

        ⚠️ `INTERACTION_CREATE` 不在这里处理：它需要先发 HTTP ack（3 秒要求），
        所以由 `run_once` 直接 await `handle_interaction`。
        """
        event_type = str(event.get("t") or "")
        data = event.get("d") or {}
        if event_type == "INTERACTION_CREATE":
            return None

        if event_type == "GROUP_AT_MESSAGE_CREATE":
            group_openid = str(data.get("group_openid") or "")
            user_id = str((data.get("author") or {}).get("member_openid") or "")
            target = build_target(CONVERSATION_GROUP, group_openid, str(data.get("id") or ""))
        elif event_type == "C2C_MESSAGE_CREATE":
            user_openid = str((data.get("author") or {}).get("user_openid") or "")
            user_id = user_openid
            target = build_target(CONVERSATION_C2C, user_openid, str(data.get("id") or ""))
        elif event_type in ("AT_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"):
            channel_id = str(data.get("channel_id") or "")
            user_id = str((data.get("author") or {}).get("id") or "")
            target = build_target(CONVERSATION_GUILD, channel_id, str(data.get("id") or ""))
        else:
            return None

        text = strip_mentions(str(data.get("content") or "")).strip()
        if not text:
            return None

        print(f"\n👤 旅行者 (QQ/{event_type}): {text}")
        if not self._allowed(user_id):
            print(f"⛔ 未授权用户：{user_id}（把它加进 .env 的 QQ_BOT_ALLOWED_USERS 才能指挥 Agent）")
            if self.config.replies_disabled:
                # 单向模式：连白名单提示也不发（上面的 ID 已经在本地日志里，照着填即可）
                print("   🔇 单向模式：这条提示也不发到 QQ —— 直接用上面这串 ID 填 QQ_BOT_ALLOWED_USERS。")
            else:
                # ⚠️ 这里在事件循环线程上：只能用 send_soon（不等待），否则自己等自己 30 秒
                self.send_soon(
                    target,
                    "⛔ 这台 Agent 没有把你加入白名单，我不能接受指令。\n"
                    f"如果你就是机主：把下面的 ID 填进 .env 的 `QQ_BOT_ALLOWED_USERS` 再重启：\n`{user_id}`",
                )
            return {"text": text, "target": target, "user_id": user_id, "allowed": "0"}

        self._on_message(text, target, user_id, event_type)
        return {"text": text, "target": target, "user_id": user_id, "allowed": "1"}

    # ---------------- 主循环 ----------------
    async def run_forever(self) -> None:
        """连网关并在断开后自动重连；被取消时干净退出。"""
        self._loop = asyncio.get_running_loop()
        self._load_pending()
        channel_router.register_sender(channel_router.QQ_PREFIX, self.send_threadsafe)
        # 发给 QQ 的文本先纯文本化（去 Markdown / 去 ```json 计划块），否则一条审批会被切成好几条
        channel_router.register_formatter(channel_router.QQ_PREFIX, chat_text.to_plain_text)
        # 手机聊天通道：审批只发精简版 + 跳过"正在下发配置…""备份目录…"这类进度消息
        # （不这样做的话，启动过程就吃掉官方"每条玩家消息最多回 5 次"的额度，完成报告发不出去）
        channel_router.register_chat_channel(
            channel_router.QQ_PREFIX, concise=self.config.concise, quiet=True
        )
        # 🌟 单向模式（QQ_BOT_REPLY_MODE=off）：只收指令，回话一律不发到 QQ，
        #    改成本地回显 —— 审批屏、完成报告、报错都只在终端 / Studio 日志里出现。
        channel_router.set_input_only(
            channel_router.QQ_PREFIX, self.config.replies_disabled
        )
        print(f"🤖 QQ 机器人已启动：{self.config.describe()}")
        print("   让 QQ 里 @机器人 或私聊它即可；Ctrl+C 退出。")
        if self.config.replies_disabled:
            print(
                "   🔇 单向模式：指令照收，但 Agent 的回话不再发到 QQ ——\n"
                "      审批屏 / 完成报告 / 报错都只在这里（Studio 的日志页同样能看到）。\n"
                "      要从手机上收到回复，把 .env 的 QQ_BOT_REPLY_MODE 改回 compact 即可。"
            )
        elif self.config.concise:
            print("   （回复精简模式：QQ 只发要执行什么，完整推理在电脑端日志；想全发设 QQ_BOT_REPLY_MODE=full）")
        if self.config.buttons and not self.config.replies_disabled:
            print("   （审批屏会挂按钮：✅ 执行 / 🧪 仅改配置 / 🚫 取消；平台没开通按钮权限时会自动退回纯文本）")
        if not self.config.replies_disabled:
            await self.ensure_menu()      # 单聊自定义菜单：不需要内邀，属于按钮的替代方案

        # ★ 把别的进程（CLI / Studio / 完成监视）排进来的推送取走发掉。
        #   为什么要有这个循环：QQ 的发送能力只在这个进程里（要 client 与凭据），
        #   别的进程直接 `send_feishu_msg("qq:...")` 会**静默失败**——玩家反馈过
        #   "启动推送收不到 / 展柜思考完 QQ 收不到"。那边现在会写进
        #   `memory/agent_push_queue.json`，这里定期取走。
        self._start_push_drain()

        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"⚠️ QQ 连接中断：{exc}；{self.config.reconnect_delay:.0f} 秒后重连…")
                await asyncio.sleep(self.config.reconnect_delay)

    async def run_once(self) -> None:
        """连一次网关并处理事件，直到连接断开。"""
        url = await self.request_gateway()
        await self.fetch_access_token()      # 保证 _connect_kwargs() 里有 token 可用
        print(f"🔗 连接网关：{url}")
        async with self._connector(url, **self._connect_kwargs()) as websocket:
            heartbeat_task = None
            try:
                async for raw in websocket:
                    payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                    op = payload.get("op")
                    if op == 10:      # HELLO
                        interval = (payload.get("d") or {}).get("heartbeat_interval", 30000) / 1000.0
                        await self._identify_or_resume(websocket)
                        heartbeat_task = asyncio.create_task(self._heartbeat(websocket, interval))
                    elif op == 0:     # 事件
                        if payload.get("s") is not None:
                            self._seq = payload["s"]
                        event_name = str(payload.get("t") or "")
                        if event_name == "READY":
                            # 记录 session_id：断线后可以用 op=6 RESUME 接着收事件，
                            # 不用每次重连都重新 IDENTIFY（频繁 identify 会被平台限流）。
                            session_id = str((payload.get("d") or {}).get("session_id") or "")
                            if session_id:
                                self._session_id = session_id
                                print(f"♻️ 会话已建立（session_id 已记录，断线可恢复）")
                        elif event_name == "INTERACTION_CREATE":
                            # 按钮点击要先 ack（官方 3 秒要求），所以是 await 而不是丢线程
                            try:
                                await self.handle_interaction(payload)
                            except Exception as exc:
                                print(f"⚠️ 处理互动事件失败：{exc}")
                        else:
                            self.handle_event(payload)
                    elif op == 11:    # 心跳回执
                        pass
                    elif op == 7:     # 要求重连
                        print("↻ 服务端要求重连")
                        return
                    elif op == 9:     # 非法 session
                        print("⚠️ session 无效，重新鉴权")
                        self._session_id = ""
                        self._seq = None
                        await self._identify_or_resume(websocket)
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()

    async def _identify_or_resume(self, websocket) -> None:
        token = await self.fetch_access_token()
        if self._session_id and self._seq is not None:
            await websocket.send(json.dumps({
                "op": 6,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self._session_id,
                    "seq": self._seq,
                },
            }))
            print("♻️ 已请求恢复会话")
            return
        await websocket.send(json.dumps({
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": self.config.intents,
                "shard": [0, 1],
                "properties": {"$os": "windows", "$sdk": "gi-agent", "$device": "pc"},
            },
        }))
        print(f"✅ 已发送 IDENTIFY（intents={self.config.intents}）")

    async def _heartbeat(self, websocket, interval: float) -> None:
        while True:
            await asyncio.sleep(max(5.0, interval))
            try:
                await websocket.send(json.dumps({"op": 1, "d": self._seq}))
            except Exception:
                return

    async def probe_gateway(self, url: str = "") -> str:
        """连一次网关、读第一帧（应当是 op=10 HELLO）就断开。

        `--check` 用它来确认"网关真的连得上"——光能取 token 不代表能用
        （实测过：token 正常，但取网关地址的接口路径写错时一样起不来）。
        """
        target = url or await self.request_gateway()
        await self.fetch_access_token()
        async with self._connector(target, **self._connect_kwargs()) as websocket:
            raw = await asyncio.wait_for(websocket.recv(), timeout=20)
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if int(payload.get("op", -1)) != 10:
            raise QQBotError(f"网关没有按预期先发 HELLO：{str(raw)[:120]}")
        return str(raw)

    async def check(self) -> bool:
        """校验配置：取 token → 取网关地址 → 真连一次网关（收到 HELLO 就断开）。"""
        if not self.config.configured:
            print("❌ 没配置 QQ_BOT_APPID / QQ_BOT_SECRET")
            return False
        print(f"配置：{self.config.describe()}")
        try:
            await self.fetch_access_token(force=True)
            url = await self.request_gateway()
            hello = await self.probe_gateway(url)
        except QQBotError as exc:
            print(f"❌ {exc}")
            return False
        except Exception as exc:
            print(f"❌ 校验失败：{exc}")
            return False
        finally:
            await self.aclose()
        print(f"✅ 配置可用，网关地址：{url}")
        print(f"✅ 网关握手成功，首帧：{hello[:120]}")
        return True


def lock_path() -> str:
    return os.path.join(PROJECT_ROOT, "memory", LOCK_FILE_NAME)


_LOCK_HANDLE: Optional[int] = None


def acquire_single_instance(appid: str = "", force: bool = False) -> bool:
    """确保同一个机器人只跑一个进程（用系统文件锁，进程挂了锁会自动释放）。

    为什么要管：两个进程用同一个 AppID 连网关会互相抢会话（后一个 IDENTIFY 会把前一个踢下线），
    表现出来就是"机器人时好时坏 / 同一条消息回两遍"，而且两边还会各写一份记忆文件。
    """
    global _LOCK_HANDLE
    if force:
        return True
    path = lock_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = os.open(path, os.O_CREAT | os.O_RDWR)
    except (OSError, ValueError) as exc:   # 目录不可写/路径非法之类：不拦着用户用
        print(f"⚠️ 单实例锁不可用（{exc}），继续启动。")
        return True

    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        else:                      # pragma: no cover - 本机是 Windows
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        print("⚠️ 已经有一个 QQ 机器人进程在运行 —— 同一个 AppID 跑两份会互相抢会话（消息时有时无 / 回两遍）。")
        print("   先关掉那个（看它的控制台窗口，或任务管理器里的 python.exe）；确实要用新的就加 --force。")
        return False

    try:
        os.ftruncate(handle, 0)
        os.write(handle, f"pid={os.getpid()} appid={appid}\n".encode("utf-8"))
    except OSError:
        pass
    # 注意：Windows 会把"被锁住的那一小段"保护起来，别的进程读不到这个文件内容
    # （正好也是锁生效的证据）。这行内容只在自己持锁/机器人退出后才有参考价值。
    _LOCK_HANDLE = handle          # 一直持有到进程退出（关了句柄锁就没了）
    return True


def release_single_instance() -> None:
    """放开单实例锁（正常退出/测试用）。"""
    global _LOCK_HANDLE
    if _LOCK_HANDLE is not None:
        try:
            os.close(_LOCK_HANDLE)
        except OSError:
            pass
        _LOCK_HANDLE = None


def _ensure_utf8_output() -> None:
    """把标准输出/错误切到 UTF-8（编码失败只替换字符），并改成行缓冲。

    1) 这个项目到处都在 print emoji。控制台是 GBK 代码页、或输出被重定向到文件、
       或被计划任务接管管道时（`python main.py qq > bot.log`），一句 emoji 就会抛
       UnicodeEncodeError 把机器人整个搞挂 —— 长跑的服务不该因为日志编码死掉。
    2) 输出到管道/文件时 Python 默认是块缓冲（攒满 8KB 才写），日志会"看起来卡住"；
       机器人必须能实时看到连上了没有，所以显式开行缓冲。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            kwargs = {"line_buffering": True, "write_through": True}
            if str(getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
                kwargs.update({"encoding": "utf-8", "errors": "replace"})
            reconfigure(**kwargs)
        except Exception:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv=None) -> int:
    _ensure_utf8_output()

    parser = argparse.ArgumentParser(description="QQ 官方机器人接口（轻量版）")
    parser.add_argument("--check", action="store_true", help="校验配置：取 token → 取网关 → 真连一次网关")
    parser.add_argument("--intents", type=lambda value: int(value, 0), default=None,
                        help=f"覆盖 intents（默认 {DEFAULT_INTENTS}）")
    parser.add_argument("--allow-anyone", action="store_true", help="允许任何 QQ 用户下指令（危险）")
    parser.add_argument("--no-buttons", action="store_true", help="审批屏不挂按钮（纯文本 y / t）")
    parser.add_argument("--force", action="store_true", help="无视单实例锁，硬启动第二个机器人进程")
    parser.add_argument("--setup-menu", action="store_true",
                        help="给单聊设置自定义菜单（✅ 执行 / 🧪 测试 / 🚫 取消，不需要内邀）")
    parser.add_argument("--show-menu", action="store_true", help="打印当前的自定义菜单")
    args = parser.parse_args(argv)

    config = QQBotConfig.from_env()
    if args.intents is not None:
        config.intents = args.intents
    if args.allow_anyone:
        config.allow_anyone = True
    if args.no_buttons:
        config.buttons = False

    if not config.configured:
        print("[提示] 未配置 QQ 机器人（QQ_BOT_APPID / QQ_BOT_SECRET），无法启动。")
        print("   1) 到 https://q.qq.com 建一个机器人，拿到 AppID 与 AppSecret（密钥）")
        print("   2) 填进 .env：QQ_BOT_APPID=... / QQ_BOT_SECRET=...")
        print("   3) 群聊/单聊消息需要开通「群聊/单聊」权限（intents 里的 GROUP_AND_C2C_EVENT）")
        print("   4) 把自己的 openid 填进 QQ_BOT_ALLOWED_USERS（先随便发一句，机器人会把 ID 告诉你）")
        return 1

    if args.check:
        # --check 是短命探针（只取 token + 握手，不发 IDENTIFY），不占单实例锁：
        # 机器人正跑着的时候也应该能随时体检
        return 0 if asyncio.run(QQBotClient(config).check()) else 1

    if args.show_menu or args.setup_menu:
        async def _menu():
            client = QQBotClient(config)
            try:
                if args.setup_menu:
                    return 0 if await client.setup_menu() else 1
                current = await client.fetch_menu()
                menu = current.get("menu") or {}
                items = menu.get("items") or []
                print(f"当前自定义菜单（版本 {current.get('version', '?')}）：")
                if not items:
                    print("   （空）—— 想配就运行：python main.py qq --setup-menu")
                    return 0
                for item in items:
                    print(f"   {item.get('name')} → type={item.get('type')} "
                          f"send_message={item.get('send_message')!r}")
                print("   想换成 GI Agent 的菜单：python main.py qq --setup-menu")
                return 0
            finally:
                await client.aclose()

        return asyncio.run(_menu())

    if not acquire_single_instance(config.appid, force=args.force):
        return 1

    try:
        asyncio.run(QQBotClient(config).run_forever())
    except KeyboardInterrupt:
        print("\n👋 QQ 机器人已退出。")
    finally:
        release_single_instance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
