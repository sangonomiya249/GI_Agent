import os
import json

# ⚠️ `lark_oapi`（飞书官方 SDK）**故意不在模块顶层 import**：
#    它占 37 MB 左右，而这个模块是**所有回话的必经之路**（agent_router / bgi_controller /
#    llm_brain 都会 import 它）——顶层 import 等于让"只用 QQ 的玩家"也必须装着它。
#    现在改成用到时再 import：不装也跑得动，只是飞书通道不可用（下面会给人话提示）。
#    想省这 37 MB：`pip uninstall lark_oapi`（README「体积与瘦身」一节有说明）。
_lark = None
_lark_import_error = ""

# 未配置飞书时的提示只打一次，避免每条消息都刷屏
_skip_notice_shown = False


def _load_lark():
    """按需加载飞书 SDK；没装就返回 None（并记住原因，只提示一次）。"""
    global _lark, _lark_import_error
    if _lark is not None:
        return _lark
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        _lark = {
            "lark": lark,
            "CreateMessageRequest": CreateMessageRequest,
            "CreateMessageRequestBody": CreateMessageRequestBody,
        }
    except Exception as exc:        # noqa: BLE001 —— 没装/装坏了都走这里
        _lark_import_error = str(exc)
        return None
    return _lark


def is_feishu_configured() -> bool:
    """飞书推送是否可用（发送消息只需要 app_id + app_secret）。"""
    return bool(
        os.getenv("FEISHU_APP_ID", "").strip()
        and os.getenv("FEISHU_APP_SECRET", "").strip()
    )


# 终端 / Studio 这类"本地目标"：本来就不会真的发到飞书（也不该发），
# 调用方的 print 已经把它们显示出来了，所以单向模式下也不用再回显一遍。
LOCAL_TARGETS = frozenset({"CLI_USER", "STUDIO", "LOCAL", ""})


def feishu_replies_enabled() -> bool:
    """飞书是否回话。`FEISHU_REPLY_MODE=off` → 单向模式（只收指令，不回话）。

    和 QQ 的 `QQ_BOT_REPLY_MODE=off` 是一个意思：指令照收，Agent 的回话只在本地显示。
    """
    raw = str(os.getenv("FEISHU_REPLY_MODE", "") or "").strip().lower()
    return raw not in ("off", "0", "none", "silent", "quiet", "单向")


def send_feishu_msg(open_id: str, text: str) -> bool:
    """向飞书用户发消息的封装函数。

    未配置飞书（FEISHU_APP_ID / FEISHU_APP_SECRET 为空）时静默跳过，
    不再抛 "app_id or app_secret not found"，CLI 终端输出不受影响。
    返回是否真的发送成功。

    🌟 通道分发：Agent 里所有回话都从这里出去。如果目标其实是别的通道的会话
    （例如 QQ 的 `qq:group:xxx#msgid`），就先交给那个通道（见 api/channel_router.py），
    这样 QQ 接口可以直接复用整套 LLM 规划 / 审批 / 事务逻辑，而不必复制一份。
    """
    global _skip_notice_shown

    from api import channel_router

    if channel_router.try_send(open_id, text):
        return True

    if channel_router.owns(open_id):
        # 这个目标明确属于别的通道（例如 QQ），只是这条没发出去。
        # ⚠️ 不能继续往飞书发：那会拿 `qq:group:xxx#msgid` 当飞书的 open_id 去调接口，
        # 只会换来一个 "invalid receive_id" 报错。
        return False

    # 🌟 单向模式：不回话，但内容原样打到本地（否则审批屏只发不显，就没法批准了）
    if not feishu_replies_enabled():
        if str(open_id) not in LOCAL_TARGETS:
            channel_router.echo_locally("飞书", text)
        return True

    if not is_feishu_configured():
        if not _skip_notice_shown:
            _skip_notice_shown = True
            print("[提示] 未配置飞书（FEISHU_APP_ID / FEISHU_APP_SECRET），已跳过消息推送；终端输出不受影响。")
        return False

    sdk = _load_lark()
    if sdk is None:
        print(
            "❌ 配置了飞书，但没装官方 SDK `lark_oapi`（约 37 MB，已改成可选依赖）。\n"
            f"   ↳ 装上即可：pip install lark_oapi（当前错误：{_lark_import_error}）"
        )
        return False

    lark = sdk["lark"]
    try:
        lark_client = (
            lark.Client.builder()
            .app_id(os.getenv("FEISHU_APP_ID", ""))
            .app_secret(os.getenv("FEISHU_APP_SECRET", ""))
            .build()
        )
        request = (
            sdk["CreateMessageRequest"].builder()
            .receive_id_type("open_id")
            .request_body(
                sdk["CreateMessageRequestBody"].builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        response = lark_client.im.v1.message.create(request)
        if not response.success():
            print(f"❌ 飞书消息发送失败: code={response.code} msg={response.msg}")
            return False
        return True
    except Exception as e:
        print(f"❌ 飞书消息发送失败: {e}")
        return False
