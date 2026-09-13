import os

# 🌟 强行让飞书域名走直连，无视本地 Clash 代理，必须放在最上面！
os.environ["NO_PROXY"] = "open.feishu.cn,*.feishu.cn"
os.environ["no_proxy"] = "open.feishu.cn,*.feishu.cn"

import json
import threading
import time
from flask import Flask
from dotenv import load_dotenv

# ⚠️ 飞书 SDK 是**可选依赖**（约 37 MB）。这里没装就给人话提示并退出，
#    而不是丢一段 ImportError 堆栈 —— 只用 QQ 的人完全可以不装它。
try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import *
    from lark_oapi.adapter.flask import *
except ImportError as exc:  # pragma: no cover - 取决于是否安装可选依赖
    print(
        "❌ 这个入口需要飞书官方 SDK `lark_oapi`（可选依赖，约 37 MB），当前没装上：\n"
        f"   {exc}\n"
        "   ↳ 装上即可：pip install lark_oapi\n"
        "   ↳ 只用 QQ 机器人 / 终端的话不需要它，直接 `python main.py` 或 `python main.py qq`。"
    )
    raise SystemExit(1)

# 引入我们刚才拆分出来的各个核心模块
import config
from api import feishu_api
from channels import agent_router

load_dotenv()

# ================= 飞书配置与缓存区 =================
VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
ENCRYPT_KEY = ""  # 没开加密留空

app = Flask(__name__)

# ================= 核心路由与拦截 =================
def _handle_message_impl(msg_content: str, open_id: str) -> None:
    """消息的实际路由处理器。

    🌟 真正的逻辑已经搬到 `channels/agent_router.py`（QQ 通道共用同一份），
    这里只是保留旧名字，避免外部脚本/习惯调用失效。
    """
    agent_router.handle_message(msg_content, open_id)


def process_message_async(data: P2ImMessageReceiveV1) -> None:
    """异步入口，防止阻塞飞书 Webhook 响应"""
    try:
        msg_content = json.loads(data.event.message.content).get("text", "").strip()
        open_id = data.event.sender.sender_id.open_id
        message_id = data.event.message.message_id

        print(f"\n👤 旅行者 (飞书): {msg_content}")
        agent_router.handle_message_async(msg_content, open_id, message_id)
    except Exception as e:
        print(f"❌ 异步处理消息时出错: {e}")

# ================= Flask 路由绑定 =================
def handle_message(data: P2ImMessageReceiveV1, **kwargs) -> None:
    threading.Thread(target=process_message_async, args=(data,), daemon=True).start()

event_handler = lark.EventDispatcherHandler.builder(ENCRYPT_KEY, VERIFICATION_TOKEN, lark.LogLevel.DEBUG) \
    .register_p2_im_message_receive_v1(handle_message) \
    .build()

@app.route("/webhook/event", methods=["POST"])
def webhook_event():
    resp = event_handler.do(parse_req())
    return parse_resp(resp)

if __name__ == "__main__":
    # 🌟 飞书未配置时直接退出，避免白跑一个无法收发消息的服务端
    if not feishu_api.is_feishu_configured():
        print("[提示] 未配置飞书（FEISHU_APP_ID / FEISHU_APP_SECRET），无需启动飞书服务端。")
        print("   想通过飞书对话控制 Agent，请在 .env 填好这两项后重试；")
        print("   只想在本机终端使用，请直接运行：python main.py")
        raise SystemExit(0)

    print("🚀 原神智能体启动中...")
    print("\n" + "="*40)
    print("✨ Agent 架构重构完成！飞书服务端已启动。")
    print("✨ 已启用：高内聚低耦合模块化 + 异步不阻塞。")
    print("="*40 + "\n")
    app.run(port=5000)