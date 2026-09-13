"""共用消息路由（channels/agent_router.py）的测试。

这块原来写死在 feishu_main 里，现在飞书和 QQ 共用，所以必须单独锁住行为：
快捷指令、待审批任务的"同意/驳回"、消息去重、以及回复到底走哪个通道。
全部用假对象顶替大模型 / BetterGI 执行，不碰真的记忆文件（临时目录）。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import config
from api import channel_router
from channels import agent_router


class SyncThread:
    """顶替 threading.Thread：立刻在当前线程跑，方便断言（不用 sleep 等线程）。"""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


class AgentRouterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history = os.path.join(self.tmp.name, "chat_memory.json")

        self.replies = []
        self.ask_calls = []
        self.exec_calls = []
        self.pending_store = {}
        self.real_reply = agent_router.reply   # 打桩前的真函数（通道分发测试要用）
        agent_router.PROCESSED_MESSAGES.clear()
        self.addCleanup(agent_router.PROCESSED_MESSAGES.clear)

        self._patches = [
            patch.object(config, "HISTORY_FILE", self.history),
            patch.object(agent_router.threading, "Thread", SyncThread),
            # 回复只记录，不真的发飞书
            patch.object(agent_router, "reply", lambda target, text: self.replies.append((target, text))),
            # 展柜刷新会连网，这里固定成"刷新成功"
            patch.object(agent_router, "refresh_store_env_context", self._refresh),
            patch.object(agent_router.memory_manager, "load_chat_store", self._load_store),
            patch.object(agent_router.memory_manager, "save_chat_store", self._save_store),
            patch.object(agent_router.llm_brain, "ask_agent", self._ask_agent),
            patch.object(agent_router.bgi_controller, "execute_bgi_task", self._execute),
        ]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)

    # ---------- 假对象 ----------
    def _refresh(self, store, uid, force=False):
        store["env_context"] = "（假展柜）"
        return True, "✅ 展柜已刷新"

    def _load_store(self):
        if not self.pending_store:
            self.pending_store = {"uid": config.DEFAULT_UID, "messages": [], "pending_task": None}
        return self.pending_store

    def _save_store(self, store):
        self.pending_store = store

    def _ask_agent(self, messages, store, uid, target):
        self.ask_calls.append({"messages": list(messages), "uid": uid, "target": target})

    def _execute(self, bgi_cmd, decision, store, target, uid):
        self.exec_calls.append({"cmd": bgi_cmd, "decision": decision, "target": target, "uid": uid})

    def _replies_text(self):
        return [text for _target, text in self.replies]


class ShortcutCommandTests(AgentRouterTestBase):
    def test_exit_command(self):
        agent_router.handle_message("退出", "ou_me")
        self.assertEqual(self.replies, [("ou_me", "👋 服务端运行中，无需手动退出。")])
        self.assertEqual(self.ask_calls, [])

    def test_history_command_counts_messages(self):
        self.pending_store = {"uid": config.DEFAULT_UID, "messages": [{"role": "user", "content": "x"}], "pending_task": None}
        agent_router.handle_message("history", "ou_me")
        self.assertTrue(any("1" in text for text in self._replies_text()), self.replies)
        self.assertEqual(self.ask_calls, [])

    def test_clear_command_wipes_memory_file(self):
        self.pending_store = {
            "uid": config.DEFAULT_UID,
            "messages": [{"role": "user", "content": "旧消息"}],
            "pending_task": {"bgi_cmd": {"x": 1}},
            "wallet": {},
        }
        with open(self.history, "w", encoding="utf-8") as handle:
            handle.write("{}")

        agent_router.handle_message("clear", "ou_me")

        # clear 的做法是直接删掉记忆文件（下次 load 会重建空记忆），并回报玩家
        self.assertFalse(os.path.exists(self.history))
        self.assertIn("🧹 记忆已清空。", self._replies_text())
        self.assertEqual(self.ask_calls, [])

    def test_refresh_command_forces_store_refresh(self):
        calls = []

        def refresh(store, uid, force=False):
            calls.append(force)
            return True, "✅ 展柜已刷新"

        with patch.object(agent_router, "refresh_store_env_context", refresh):
            agent_router.handle_message("refresh", "ou_me")

        # 每条消息先做一次普通刷新，refresh 指令再强制刷一次（绕过 TTL）
        self.assertEqual(calls, [False, True])
        self.assertIn("✅ 展柜已刷新", self._replies_text())
        self.assertEqual(self.ask_calls, [])

    def test_every_message_refreshes_store_context(self):
        agent_router.handle_message("今天体力怎么花", "ou_me")
        self.assertEqual(self.pending_store["env_context"], "（假展柜）")


class PendingApprovalTests(AgentRouterTestBase):
    def _with_pending(self, uid=None):
        uid = uid or config.DEFAULT_UID
        self.pending_store = {
            "uid": uid,
            "messages": [],
            "pending_task": {"bgi_cmd": {"energy_task": {"key": "run_domain"}}, "uid": uid},
        }

    def test_approve_word_executes_task(self):
        self._with_pending()
        agent_router.handle_message("y", "ou_me")

        self.assertEqual(len(self.exec_calls), 1)
        self.assertEqual(self.exec_calls[0]["decision"], "y")
        self.assertEqual(self.exec_calls[0]["cmd"], {"energy_task": {"key": "run_domain"}})
        self.assertIn("⚙️ 指令已确认", self._replies_text()[0])
        self.assertIsNone(self.pending_store["pending_task"])
        self.assertEqual(self.ask_calls, [])   # 审批不该再叫一次大模型

    def test_t_means_test_mode(self):
        self._with_pending()
        agent_router.handle_message("t", "ou_me")
        self.assertEqual(self.exec_calls[0]["decision"], "t")

    def test_chinese_approve_words(self):
        for word in ("确认", "执行", "同意", "批准", "yes"):
            with self.subTest(word=word):
                self.exec_calls.clear()
                self.replies.clear()
                self._with_pending()
                agent_router.handle_message(word, "ou_me")
                self.assertEqual(len(self.exec_calls), 1, word)

    def test_rejection_replans_with_feedback(self):
        self._with_pending()
        agent_router.handle_message("我要改成刷绝缘本", "ou_me")

        self.assertEqual(self.exec_calls, [])
        self.assertIn("🚫 计划已撤销", self._replies_text()[0])
        # 驳回后应该把"我的新要求"喂回大模型重新规划
        self.assertEqual(len(self.ask_calls), 1)
        contents = [item["content"] for item in self.ask_calls[0]["messages"]]
        self.assertTrue(any("我要改成刷绝缘本" in text for text in contents), contents)

    def test_pending_uid_is_used_for_execution(self):
        self._with_pending(uid="100000000")
        agent_router.handle_message("y", "qq:group:G1#M1")
        self.assertEqual(self.exec_calls[0]["uid"], "100000000")
        self.assertEqual(self.exec_calls[0]["target"], "qq:group:G1#M1")

    def test_cancel_word_drops_the_plan_without_touching_anything(self):
        """"取消"（QQ 审批按钮里的 🚫 取消）必须真的什么都不做。"""
        self._with_pending()

        agent_router.handle_message("取消", "qq:group:G1#event:EV1")

        self.assertEqual(self.exec_calls, [])
        self.assertEqual(self.ask_calls, [])          # 也不该去打扰大模型重新规划
        self.assertIsNone(self.pending_store["pending_task"])
        self.assertIn("已取消本轮计划", self._replies_text()[0])

    def test_cancel_aliases(self):
        for word in ("取消计划", "算了", "不用了", "cancel", "stop"):
            with self.subTest(word=word):
                self.exec_calls.clear()
                self.replies.clear()
                self._with_pending()
                agent_router.handle_message(word, "ou_me")
                self.assertEqual(self.exec_calls, [], word)
                self.assertIsNone(self.pending_store["pending_task"], word)

    def test_cancel_without_pending_task_is_just_chat(self):
        """没有待审批计划时说"取消"，就照常交给大模型（不吞掉消息）。"""
        agent_router.handle_message("取消", "ou_me")

        self.assertEqual(len(self.ask_calls), 1)
        self.assertEqual(self.replies, [])


class ChatTests(AgentRouterTestBase):
    def test_plain_message_goes_to_brain(self):
        agent_router.handle_message("帮我刷绝缘本", "ou_me")

        self.assertEqual(len(self.ask_calls), 1)
        contents = [item["content"] for item in self.ask_calls[0]["messages"]]
        self.assertIn("帮我刷绝缘本", contents)
        self.assertEqual(self.ask_calls[0]["target"], "ou_me")

    def test_empty_message_is_ignored(self):
        agent_router.handle_message("   ", "ou_me")
        self.assertEqual(self.ask_calls, [])

    def test_history_is_saved_for_each_message(self):
        agent_router.handle_message("第一个问题", "ou_me")
        agent_router.handle_message("第二个问题", "ou_me")
        contents = [item["content"] for item in self.pending_store["messages"]]
        self.assertEqual(contents, ["第一个问题", "第二个问题"])


class SyncDedupeTests(AgentRouterTestBase):
    def test_handle_message_async_runs_the_message(self):
        agent_router.handle_message_async("帮我刷绝缘本", "qq:group:G1#M1", "MSG1")
        self.assertEqual(len(self.ask_calls), 1)

    def test_duplicate_message_id_is_skipped(self):
        agent_router.handle_message_async("帮我刷绝缘本", "qq:group:G1#M1", "MSG1")
        agent_router.handle_message_async("帮我刷绝缘本", "qq:group:G1#M1", "MSG1")
        self.assertEqual(len(self.ask_calls), 1)

    def test_message_without_id_is_not_deduped(self):
        agent_router.handle_message_async("一", "ou_me", "")
        agent_router.handle_message_async("二", "ou_me", "")
        self.assertEqual(len(self.ask_calls), 2)

    def test_processed_messages_expire(self):
        self.assertFalse(agent_router.is_message_processed("OLD"))
        with patch.object(agent_router.time, "time", return_value=agent_router.PROCESSED_MESSAGES["OLD"] + agent_router.MESSAGE_TTL + 1):
            self.assertFalse(agent_router.is_message_processed("NEW"))
        self.assertNotIn("OLD", agent_router.PROCESSED_MESSAGES)

    def test_errors_do_not_escape_the_thread(self):
        with patch.object(agent_router, "handle_message", side_effect=RuntimeError("炸了")):
            agent_router.handle_message_async("随便", "ou_me")   # 不该抛出来


class ChannelRoutingTests(AgentRouterTestBase):
    """回复的落点：QQ 会话必须走 QQ 通道，飞书目标保持原样。"""

    def setUp(self):
        super().setUp()
        channel_router.clear()
        self.addCleanup(channel_router.clear)
        self.qq_sent = []
        channel_router.register_sender(
            channel_router.QQ_PREFIX,
            lambda target, text: self.qq_sent.append((target, text)) or True,
        )

    def test_reply_routes_to_qq_channel(self):
        """Agent 的 reply() → feishu_api → channel_router → QQ 通道（端到端走真函数）。"""
        self.real_reply("qq:group:G1#M1", "⚙️ 已确认")
        self.assertEqual(self.qq_sent, [("qq:group:G1#M1", "⚙️ 已确认")])

    def test_shortcut_reply_lands_in_qq(self):
        with patch.object(agent_router, "reply", self.real_reply):
            agent_router.handle_message("退出", "qq:c2c:USER_A#M2")
        self.assertEqual(self.qq_sent, [("qq:c2c:USER_A#M2", "👋 服务端运行中，无需手动退出。")])

    def test_feishu_target_falls_through(self):
        from api import feishu_api
        # 没配飞书时会静默跳过并返回 False（说明没被 QQ 通道截胡）
        # ⚠️ FEISHU_REPLY_MODE 也要显式打桩：本机 .env 里可能设成 off（单向模式），
        #    否则这条用例会随开发机的配置时好时坏。
        with patch.dict(
            "os.environ",
            {"FEISHU_APP_ID": "", "FEISHU_APP_SECRET": "", "FEISHU_REPLY_MODE": "on"},
            clear=False,
        ):
            self.assertFalse(feishu_api.send_feishu_msg("ou_me", "hello"))
        self.assertEqual(self.qq_sent, [])


if __name__ == "__main__":
    unittest.main()
