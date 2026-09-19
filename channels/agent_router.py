"""会话消息路由：把"玩家发来一句话"变成"Agent 思考/审批/执行"。

飞书（`feishu_main.py`）和 QQ（`channels/qq_bot.py`）**共用**这一份逻辑，
区别只在"往哪个会话回话"—— 回复统一走 `feishu_api.send_feishu_msg(target, ...)`，
由 `api/channel_router` 按 target 前缀分发到实际通道。

原本这段逻辑写死在 `feishu_main._handle_message_impl` 里；抽出来是为了加 QQ 接口时
不必复制一份审批 / 记忆 / 拦截逻辑（那些逻辑最容易改出 bug）。
"""

import os
import threading
import time

import config
from api import channel_router, feishu_api
from brain import llm_brain, memory_manager
from skills import bgi_controller, game_control
from skills.env_reader import refresh_store_env_context

# 消息去重：同一个 message_id 只处理一次（通道重连/重试时会重复推）
PROCESSED_MESSAGES = {}
MESSAGE_TTL = 300
# ⚠️ 飞书的每条 webhook 都起一个线程、QQ 的事件也在别的线程里派发，所以这个字典会被并发读写。
# 没有锁时"边收集过期项边删除"会抛 RuntimeError: dictionary changed size during iteration，
# 消息被静默丢掉（飞书侧只在日志里留一行错误）。
_PROCESSED_LOCK = threading.Lock()

APPROVE_WORDS = ("y", "t", "yes", "确认", "执行", "同意", "批准")
# "取消"要能真的把待审批计划扔掉：否则它会被当成"新要求"喂回大模型重新规划
CANCEL_WORDS = ("取消", "取消计划", "算了", "不用了", "cancel", "stop")
EXIT_WORDS = ("exit", "quit", "退出")


def is_message_processed(message_id: str) -> bool:
    """检查消息是否已处理过（去重机制）"""
    if not message_id:
        return False
    current_time = time.time()
    with _PROCESSED_LOCK:
        expired = [mid for mid, ts in list(PROCESSED_MESSAGES.items()) if current_time - ts > MESSAGE_TTL]
        for mid in expired:
            del PROCESSED_MESSAGES[mid]

        if message_id in PROCESSED_MESSAGES:
            print(f"⚠️ 消息 {message_id} 已处理过，跳过重复处理")
            return True
        PROCESSED_MESSAGES[message_id] = current_time
        return False


def reply(target: str, text: str) -> None:
    """统一的回话入口（实际由 api/channel_router 决定走飞书还是 QQ）。"""
    feishu_api.send_feishu_msg(target, text)


def _drop_growth_queue(reason=""):
    """放弃分批次执行队列（玩家取消 / 改说别的时调用）。失败只打日志，不影响对话。"""
    try:
        from brain import execution_queue

        if execution_queue.is_stepwise():
            execution_queue.skip(reason)
            print(f"🚫 分批次队列已放弃：{reason}")
    except Exception as exc:                # noqa: BLE001
        print(f"⚠️ 放弃分批次队列失败（{type(exc).__name__}: {exc}）")


def handle_message(msg_content: str, target: str) -> None:
    """处理一条玩家消息（阻塞：大模型思考在后台线程里跑，这里立刻返回）。

    `target` 是"会话标识"，回复会原样交给它对应的通道。
    """
    store = memory_manager.load_chat_store()
    uid = store.get("uid", config.DEFAULT_UID)
    messages = store.get("messages", [])

    # ★ 记住"上次跟谁说话" —— 主动推送（启动推送执行目标、跑完推下一条路线）全靠它。
    #   以前**没有任何地方写过这个键**，于是 `growth_planner._push_growth_notice()` 找不到目标，
    #   只能打一行日志：玩家看到的就是"本地处理完了，QQ 一条都没收到"（被反馈过）。
    if target:
        store["last_target"] = str(target)

    # 🌟 每条消息都刷新展柜（失败时保留旧缓存，不覆盖成错误信息）
    _refreshed, env_notice = refresh_store_env_context(store, uid)
    print(env_notice)
    memory_manager.save_chat_store(store)

    user_input = str(msg_content or "").strip().lower()

    # ---------- 1. 系统快捷指令 ----------
    if user_input in EXIT_WORDS:
        reply(target, "👋 服务端运行中，无需手动退出。")
        return

    if user_input == "clear":
        store = {
            "uid": uid,
            "env_context": "",
            "env_context_at": "",
            "messages": [],
            "wallet": {"mora": 0, "exp_books": 0, "boss_mats": {}},
            "pending_task": None,
        }
        if os.path.exists(config.HISTORY_FILE):
            os.remove(config.HISTORY_FILE)
        print("🧹 记忆已清空。")
        reply(target, "🧹 记忆已清空。")
        return

    if user_input == "refresh":
        print("🔄 正在刷新最新展柜上下文...")
        _ok, notice = refresh_store_env_context(store, uid, force=True)
        memory_manager.save_chat_store(store)
        reply(target, notice)
        return

    if user_input == "history":
        reply(target, f"📚 当前历史消息数：{len(messages)}")
        return

    # ---------- 1.5 系统操作：关闭原神（关键词快通道，不用等大模型） ----------
    if not store.get("pending_task"):
        close_intent = game_control.detect_intent(msg_content)
        if close_intent:
            plan_text = "\n".join(game_control.plan_lines(close_intent))
            reply(
                target,
                f"{game_control.APPROVAL_HEADER}\n{plan_text}\n\n👉 y = 执行　其它内容 = 放弃",
            )
            store["pending_task"] = {
                "system_task": close_intent,
                "open_id": target,
                "uid": uid,
            }
            memory_manager.save_chat_store(store)
            print("🛑 已生成关闭原神的审批（等玩家回 y）")
            return

    # ---------- 1.6 没有待审批计划时收到"y / t / 确认" ----------
    # ⚠️ 实测踩过：这类消息以前会掉进"普通聊天"被喂给大模型，于是它凭空又规划了一遍打 Boss
    #    （还会把玩家刚批准的那件事的痕迹覆盖掉）。审批词只对"待确认的计划"有意义，没计划就直说。
    #    注意只管批准词：`取消` 保持原样（可能有别的意思，见 test_cancel_without_pending_task_is_just_chat）。
    if not store.get("pending_task") and user_input in APPROVE_WORDS:
        print("ℹ️ 收到审批词，但当前没有待审批的计划，已忽略。")
        reply(target, "ℹ️ 现在没有待确认的计划（上一条可能已经执行过了）。要做什么直接说就行～")
        return

    # ---------- 2. 是否有待审批的自动化任务 ----------
    pending_task = store.get("pending_task")
    if pending_task and pending_task.get("system_task") and user_input not in APPROVE_WORDS \
            and user_input not in CANCEL_WORDS:
        # 待审批的是"关原神"这种系统操作：玩家回别的内容就当作放弃这次操作，
        # 不能把它当成"新的 BGI 任务要求"（那会让模型凭空规划一个跑图计划）
        store["pending_task"] = None
        memory_manager.save_chat_store(store)
        reply(target, "🚫 已放弃上一次的系统操作（关闭原神）。如需执行请重新说一次。")
        pending_task = None

    # ★ 待审批的是**分批次的一条路线**，而玩家回的是别的内容：
    #   放弃整条队列（不然它会一直挂着'第 N 条'，之后每次说话都被重新问一遍），
    #   然后把这句话当成新请求交给大模型（规格原话："再参考这一条请求来执行"）。
    if pending_task and pending_task.get("growth_step") and user_input not in APPROVE_WORDS \
            and user_input not in CANCEL_WORDS:
        print("\n⏭️ 玩家没有确认这一条路线，放弃分批次队列，按新请求处理。")
        store["pending_task"] = None
        memory_manager.save_chat_store(store)
        _drop_growth_queue("玩家改说了别的")
        reply(target, "⏭️ 已放弃分批次队列（本次没有启动 BetterGI）。下面按你说的来 ——")
        pending_task = None

    if pending_task:
        bgi_cmd = pending_task.get("bgi_cmd")
        stored_uid = pending_task.get("uid", uid)
        # ★ 分批次执行（`GROWTH_EXECUTION_MODE=stepwise`）挂上来的那一步：
        #   玩家回 y → 跑这一条（下面照常走审批）；回"取消"或说别的 → **放弃整个队列**，
        #   按玩家这次说的来（规格："若用户回答 no，或者发送其它请求再参考这一条请求来执行"）。
        is_growth_step = bool(pending_task.get("growth_step"))

        if user_input in CANCEL_WORDS:
            # 取消 = 什么都不做（不写配置、不启动 BetterGI），也不去打扰大模型
            print("\n🚫 计划已取消。")
            store["pending_task"] = None
            memory_manager.save_chat_store(store)
            if is_growth_step:
                _drop_growth_queue("玩家取消了分批次执行")
            reply(target, "🚫 已取消本轮计划：没有写任何配置，也没有启动 BetterGI。")
            return

        if user_input in APPROVE_WORDS:
            decision_lower = "t" if user_input == "t" else "y"
            print(f"🛑 收到审批结果: {decision_lower}")
            if not channel_router.wants_quiet(target):
                # 聊天通道别发这句：手机上属于"进度消息"，还会占掉官方"每条消息最多回 5 次"的额度
                reply(target, "⚙️ 指令已确认，正在下发配置给 BetterGI...")

            store["pending_task"] = None
            memory_manager.save_chat_store(store)

            system_task = pending_task.get("system_task")
            if system_task:
                # 🛑 系统操作（关原神）：不动 BetterGI 配置，直接执行
                threading.Thread(
                    target=game_control.execute,
                    args=(system_task, target),
                ).start()
                return

            threading.Thread(
                target=bgi_controller.execute_bgi_task,
                args=(bgi_cmd, decision_lower, store, target, stored_uid),
            ).start()
            return

        print("\n🚫 审批已驳回。正在将你的要求反馈给大脑重新规划...")
        store["pending_task"] = None
        feedback = f"我拒绝了刚才的执行申请。我的新要求是：{msg_content}。请根据我的新要求重新评估，并输出新的 JSON 指令。"
        messages.append({"role": "user", "content": feedback})
        messages = memory_manager.trim_history(messages)
        store["messages"] = messages
        memory_manager.save_chat_store(store)
        reply(target, "🚫 计划已撤销。正在根据您的要求重新评估...")
        # 不 return：继续走下面的"交给大脑思考"

    # ---------- 3. 普通聊天 ----------
    if not str(msg_content or "").strip():
        return

    if not pending_task:
        messages.append({"role": "user", "content": msg_content})
        messages = memory_manager.trim_history(messages)
        store["messages"] = messages
        memory_manager.save_chat_store(store)

    print("🧠 正在唤醒大模型思考...")
    threading.Thread(
        target=llm_brain.ask_agent, args=(messages, store, uid, target)
    ).start()


def handle_message_async(msg_content: str, target: str, message_id: str = "") -> None:
    """去重 + 抛到后台线程（Webhook / WebSocket 都该用这个入口，别阻塞收消息的循环）。"""
    if message_id and is_message_processed(message_id):
        return

    def runner():
        try:
            handle_message(msg_content, target)
        except Exception as exc:  # 单条消息出错不能把服务带崩
            print(f"❌ 处理消息时出错: {exc}")

    threading.Thread(target=runner, daemon=True).start()


__all__ = [
    "PROCESSED_MESSAGES",
    "MESSAGE_TTL",
    "channel_router",
    "handle_message",
    "handle_message_async",
    "is_message_processed",
    "reply",
]
