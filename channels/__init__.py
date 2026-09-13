"""聊天通道（接口层）：把"某个聊天软件"接到这套 Agent 上。

目前有：
  * 飞书：`feishu_main.py`（Webhook）
  * QQ 官方机器人：`channels/qq_bot.py`（WebSocket 网关，见 `python main.py qq`）

两者共用同一份消息路由 `channels/agent_router.py`，回复统一走
`api.feishu_api.send_feishu_msg` → `api/channel_router` 分发，所以新增通道
不需要动 Agent 的规划/审批/执行逻辑。

`channels/chat_text.py` 负责"显示层"：把给终端写的 Markdown / ```json 计划块
纯文本化，聊天软件里才看得下去。
"""

__all__ = ["agent_router", "chat_text", "qq_bot"]
