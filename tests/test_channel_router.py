"""通道分发（api/channel_router.py）的测试。

核心约定：Agent 的所有回话都走 `feishu_api.send_feishu_msg(target, text)`；
target 带前缀（`qq:`）时改由对应通道投递，飞书目标（没有前缀）保持原样。
"""

import unittest

from api import channel_router, feishu_api


class ChannelRouterTests(unittest.TestCase):
    def setUp(self):
        channel_router.clear()
        self.sent = []

    def tearDown(self):
        channel_router.clear()

    def _sender(self, name):
        def sender(target, text):
            self.sent.append((name, target, text))
            return True

        return sender

    def test_default_has_no_prefixes(self):
        self.assertEqual(channel_router.registered_prefixes(), ())
        self.assertIsNone(channel_router.sender_for("ou_123"))

    def test_register_and_sender_for(self):
        sender = self._sender("qq")
        channel_router.register_sender(channel_router.QQ_PREFIX, sender)

        self.assertEqual(channel_router.registered_prefixes(), ("qq:",))
        self.assertIs(channel_router.sender_for("qq:group:abc#1"), sender)
        # 飞书目标不带前缀 → 没有归属通道（继续走飞书）
        self.assertIsNone(channel_router.sender_for("ou_123"))
        self.assertIsNone(channel_router.sender_for(""))

    def test_try_send_returns_false_for_foreign_target(self):
        channel_router.register_sender(channel_router.QQ_PREFIX, self._sender("qq"))
        self.assertFalse(channel_router.try_send("ou_123", "hello"))
        self.assertEqual(self.sent, [])

    def test_send_feishu_msg_dispatches_to_registered_channel(self):
        """端到端：Agent 调 send_feishu_msg 回 QQ 会话时不该走飞书。"""
        channel_router.register_sender(channel_router.QQ_PREFIX, self._sender("qq"))

        ok = feishu_api.send_feishu_msg("qq:group:abc#msg1", "⚙️ 已确认")

        self.assertTrue(ok)
        self.assertEqual(self.sent, [("qq", "qq:group:abc#msg1", "⚙️ 已确认")])

    def test_longest_prefix_wins(self):
        channel_router.register_sender("qq:", self._sender("generic"))
        specific = self._sender("qq-group")
        channel_router.register_sender("qq:group:", specific)

        self.assertIs(channel_router.sender_for("qq:group:abc"), specific)
        self.assertIs(channel_router.sender_for("qq:c2c:abc"), channel_router.sender_for("qq:x"))

    def test_unregister_and_clear(self):
        channel_router.register_sender("qq:", self._sender("qq"))
        channel_router.unregister_sender("qq:")
        self.assertEqual(channel_router.registered_prefixes(), ())
        # 注销后目标回到飞书（try_send 返回 False）
        self.assertFalse(channel_router.try_send("qq:group:a", "x"))

    def test_register_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            channel_router.register_sender("", self._sender("qq"))
        with self.assertRaises(ValueError):
            channel_router.register_sender("qq:", None)

    def test_sender_exception_is_swallowed(self):
        """通道炸了不能把主流程（LLM 规划 / 事务）带崩。"""
        def boom(target, text):
            raise RuntimeError("网络不通")

        channel_router.register_sender("qq:", boom)
        self.assertFalse(channel_router.try_send("qq:group:a", "x"))

    def test_formatter_rewrites_text_on_the_way_out(self):
        seen = []
        channel_router.register_sender("qq:", lambda target, text: seen.append(text) or True)
        channel_router.register_formatter("qq:", lambda text: text.replace("```", "").strip())

        channel_router.try_send("qq:group:a", "  ```json\n{}\n```  ")

        self.assertEqual(seen, ["json\n{}"])

    def test_formatter_is_not_applied_to_other_channels(self):
        seen = []
        channel_router.register_sender("qq:", lambda target, text: seen.append(text) or True)
        channel_router.register_formatter("qq:", lambda text: "改过了")

        self.assertFalse(channel_router.try_send("ou_me", "原样"))
        self.assertEqual(seen, [])

    def test_formatter_failure_falls_back_to_original_text(self):
        seen = []
        channel_router.register_sender("qq:", lambda target, text: seen.append(text) or True)

        def boom(_text):
            raise RuntimeError("正则炸了")

        channel_router.register_formatter("qq:", boom)
        channel_router.try_send("qq:group:a", "原样发出去")

        self.assertEqual(seen, ["原样发出去"])
        self.assertEqual(channel_router.format_for("qq:group:a", "原样发出去"), "原样发出去")

    def test_concise_flag(self):
        self.assertFalse(channel_router.wants_concise("qq:group:a"))
        channel_router.register_chat_channel("qq:", concise=True)

        self.assertTrue(channel_router.wants_concise("qq:group:a"))
        self.assertTrue(channel_router.wants_concise("qq:c2c:u#1"))
        # 飞书 / 终端不是"聊天通道"，照旧发全文
        self.assertFalse(channel_router.wants_concise("ou_me"))

    def test_quiet_flag(self):
        """进度类消息（"正在下发配置…""备份目录…"）在聊天通道上不发。"""
        self.assertFalse(channel_router.wants_quiet("qq:group:a"))
        channel_router.register_chat_channel("qq:", quiet=True)

        self.assertTrue(channel_router.wants_quiet("qq:group:a"))
        self.assertFalse(channel_router.wants_quiet("ou_me"))

    def test_concise_and_quiet_are_independent(self):
        channel_router.register_chat_channel("qq:", concise=True, quiet=False)
        self.assertTrue(channel_router.wants_concise("qq:group:a"))
        self.assertFalse(channel_router.wants_quiet("qq:group:a"))

        channel_router.register_chat_channel("qq:", concise=False, quiet=True)
        self.assertFalse(channel_router.wants_concise("qq:group:a"))
        self.assertTrue(channel_router.wants_quiet("qq:group:a"))

    def test_concise_can_be_turned_off(self):
        channel_router.register_chat_channel("qq:", concise=True)
        channel_router.register_chat_channel("qq:", concise=False)
        self.assertFalse(channel_router.wants_concise("qq:group:a"))

    def test_register_formatter_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            channel_router.register_formatter("qq:", None)
        with self.assertRaises(ValueError):
            channel_router.register_chat_channel("")

    def test_longest_prefix_wins_for_formatters_too(self):
        channel_router.register_formatter("qq:", lambda text: "通用")
        channel_router.register_formatter("qq:group:", lambda text: "群聊专用")

        self.assertEqual(channel_router.format_for("qq:group:a", "x"), "群聊专用")
        self.assertEqual(channel_router.format_for("qq:c2c:a", "x"), "通用")

    def test_clear_resets_everything(self):
        channel_router.register_sender("qq:", lambda target, text: True)
        channel_router.register_formatter("qq:", lambda text: text)
        channel_router.register_chat_channel("qq:")

        channel_router.clear()

        self.assertEqual(channel_router.registered_prefixes(), ())
        self.assertEqual(channel_router.format_for("qq:group:a", "x"), "x")
        self.assertFalse(channel_router.wants_concise("qq:group:a"))
        self.assertFalse(channel_router.wants_quiet("qq:group:a"))


if __name__ == "__main__":
    unittest.main()
