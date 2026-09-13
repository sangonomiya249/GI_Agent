"""进度消息的"安静模式"（聊天通道少发消息）。

玩家实测：一次带配置的执行会连着发六条（⚙️ 指令已确认 → 🧾 配置事务已提交 → 🗂️ 备份与 Diff →
🚀 正在启动 → 🎉 已触发 → ✅ 已确认开始执行），手机上刷屏；更要命的是官方限制
**同一条玩家消息最多回复 5 次**，额度被启动过程吃光后，20 分钟后真正的完成报告就发不出去了。

所以：聊天通道（QQ）只保留关键节点 —— "✅ 已确认 BetterGI 开始执行任务。" 和完成报告。
"""

import subprocess
import unittest
from unittest.mock import patch

from api import channel_router
from channels import agent_router
from skills import bgi_controller


class SendNoticeTests(unittest.TestCase):
    def setUp(self):
        channel_router.clear()
        self.addCleanup(channel_router.clear)
        self.sent = []
        self._patch = patch.object(
            bgi_controller.feishu_api, "send_feishu_msg",
            lambda target, text: self.sent.append((target, text)) or True,
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_progress_message_is_sent_on_normal_targets(self):
        self.assertTrue(bgi_controller.send_notice("ou_me", "🚀 正在启动", progress=True))
        self.assertEqual(len(self.sent), 1)

    def test_progress_message_is_skipped_on_chat_channels(self):
        channel_router.register_chat_channel("qq:")

        self.assertFalse(bgi_controller.send_notice("qq:c2c:USER_A#M1", "🚀 正在启动", progress=True))
        self.assertEqual(self.sent, [])

    def test_important_message_still_goes_to_chat_channels(self):
        """报错/需要玩家操作的消息不能静默（比如"关不掉 BetterGI，请先手动关闭"）。"""
        channel_router.register_chat_channel("qq:")

        self.assertTrue(bgi_controller.send_notice("qq:c2c:USER_A#M1", "❌ 无法关闭 BetterGI"))

        self.assertEqual(len(self.sent), 1)

    def test_chat_false_is_skipped_on_chat_but_sent_elsewhere(self):
        """`chat=False`：玩家明确说不想在 QQ 上看到的回执（配置写入 / 启动成功）。"""
        channel_router.register_chat_channel("qq:")

        self.assertFalse(
            bgi_controller.send_notice("qq:c2c:USER_A#M1", "🧾 配置已写入", chat=False)
        )
        self.assertEqual(self.sent, [])

        self.assertTrue(bgi_controller.send_notice("ou_me", "🧾 配置已写入", chat=False))
        self.assertEqual(len(self.sent), 1)

    def test_quiet_flag_can_be_disabled_per_channel(self):
        channel_router.register_chat_channel("qq:", concise=True, quiet=False)

        self.assertTrue(bgi_controller.send_notice("qq:c2c:USER_A#M1", "🚀 正在启动", progress=True))
        self.assertTrue(bgi_controller.send_notice("qq:c2c:USER_A#M1", "🧾 配置已写入", chat=False))


class ApprovalProgressTests(unittest.TestCase):
    """点 y 之后那句"⚙️ 指令已确认，正在下发配置…"在聊天通道上也不发。"""

    def setUp(self):
        channel_router.clear()
        self.addCleanup(channel_router.clear)
        self.replies = []
        agent_router.PROCESSED_MESSAGES.clear()
        self.addCleanup(agent_router.PROCESSED_MESSAGES.clear)
        self.store = {
            "uid": "1",
            "messages": [],
            "pending_task": {"bgi_cmd": {"energy_task": {}}, "uid": "1"},
        }
        self._patches = [
            patch.object(agent_router.memory_manager, "load_chat_store", lambda: self.store),
            patch.object(agent_router.memory_manager, "save_chat_store", lambda store: None),
            patch.object(agent_router, "refresh_store_env_context", lambda store, uid, force=False: (True, "ok")),
            patch.object(agent_router, "reply", lambda target, text: self.replies.append((target, text))),
            patch.object(agent_router.bgi_controller, "execute_bgi_task", lambda *args: None),
            patch.object(agent_router.threading, "Thread", lambda *args, **kwargs: type("T", (), {"start": lambda self: None})()),
        ]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)

    def test_progress_reply_skipped_on_chat_channel(self):
        channel_router.register_chat_channel("qq:")

        agent_router.handle_message("y", "qq:c2c:USER_A#M1")

        self.assertEqual(self.replies, [])          # 一句都不发（"已确认开始执行"由哨兵随后发）

    def test_progress_reply_sent_on_normal_channel(self):
        agent_router.handle_message("y", "ou_me")

        self.assertEqual(len(self.replies), 1)
        self.assertIn("⚙️ 指令已确认", self.replies[0][1])


class ChangedFilesTests(unittest.TestCase):
    """「配置已写入」这句话得说清是哪些文件（玩家实测：两个同名文件看着像重复）。"""

    def test_same_name_in_different_roles_is_distinguished(self):
        from pathlib import Path

        text = bgi_controller.describe_changed_files([
            Path(r"C:\Program Files\BetterGI\User\OneDragon\地图素材.json"),
            Path(r"C:\Program Files\BetterGI\User\ScriptGroup\地图素材.json"),
        ])

        self.assertIn("地图素材.json（一条龙）", text)
        self.assertIn("地图素材.json（脚本组）", text)

    def test_known_roles(self):
        from pathlib import Path

        text = bgi_controller.describe_changed_files([
            Path(r"C:\Program Files\BetterGI\User\config.json"),
            Path(r"C:\Program Files\BetterGI\User\ScriptGroup\狗粮.json"),
            Path(r"C:\Program Files\BetterGI\User\ScriptGroup\狗粮\settings.json"),
        ])

        self.assertIn("config.json（全局配置）", text)
        self.assertIn("狗粮.json（脚本组）", text)
        self.assertIn("settings.json（脚本设置）", text)

    def test_duplicate_paths_are_deduped(self):
        from pathlib import Path

        path = Path(r"C:\Program Files\BetterGI\User\OneDragon\地图素材.json")
        self.assertEqual(bgi_controller.describe_changed_files([path, path]), "地图素材.json（一条龙）")

    def test_empty_input(self):
        self.assertEqual(bgi_controller.describe_changed_files([]), "（无）")


class LaunchNoticeTests(unittest.TestCase):
    """玩家要求：QQ 上**只**保留「✅ 已确认 BetterGI 开始执行任务。」

    原话："qq机器人都不要反馈，只要这一句 ✅ 已确认 BetterGI 开始执行任务。"
    所以启动成功回执（无论是计划任务还是回退方案）都不再发到聊天通道；
    终端 / Studio / 飞书 照旧能看出"启没启动"。哨兵那句 ✅ 由 `start_completion_watch` 发，
    走的是 `feishu_api.send_feishu_msg`，不受这里影响。
    """

    def setUp(self):
        channel_router.clear()
        self.addCleanup(channel_router.clear)
        self.sent = []
        self._patch = patch.object(
            bgi_controller.feishu_api, "send_feishu_msg",
            lambda target, text: self.sent.append(text) or True,
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._watch_patch = patch.object(bgi_controller.bgi_watcher, "start_completion_watch", lambda *a, **k: None)
        self._watch_patch.start()
        self.addCleanup(self._watch_patch.stop)

    def test_task_trigger_message_is_not_sent_on_quiet_channel(self):
        channel_router.register_chat_channel("qq:")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

        with patch.object(bgi_controller.subprocess, "run", return_value=ok):
            self.assertTrue(bgi_controller._launch_bettergi("qq:c2c:U1#M1"))

        joined = "\n".join(self.sent)
        self.assertNotIn("已触发一条龙", joined)
        self.assertNotIn("正在通过任务计划启动", joined)

    def test_task_trigger_message_is_sent_on_the_computer(self):
        """电脑端（终端 / Studio / 飞书）必须还能看出"启没启动"。"""
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

        with patch.object(bgi_controller.subprocess, "run", return_value=ok):
            self.assertTrue(bgi_controller._launch_bettergi("ou_me"))

        joined = "\n".join(self.sent)
        self.assertIn("已触发一条龙", joined)
        self.assertIn("StartBetterGI", joined)

    def test_fallback_launch_message_is_not_sent_on_quiet_channel(self):
        channel_router.register_chat_channel("qq:")
        missing = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"cannot find the file")

        with patch.object(bgi_controller.subprocess, "run", return_value=missing), patch.object(
            bgi_controller.subprocess, "Popen", lambda *a, **k: None
        ), patch.object(bgi_controller.os.path, "exists", return_value=True):
            self.assertTrue(bgi_controller._launch_bettergi("qq:c2c:U1#M1"))

        self.assertNotIn("已直接拉起", "\n".join(self.sent))

    def test_launch_failure_is_still_reported_on_quiet_channel(self):
        """启动**失败**必须报（这正是删掉成功回执后唯一的兜底）。"""
        channel_router.register_chat_channel("qq:")
        missing = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"cannot find the file")

        with patch.object(bgi_controller.subprocess, "run", return_value=missing), patch.object(
            bgi_controller.os.path, "exists", return_value=False
        ):
            self.assertFalse(bgi_controller._launch_bettergi("qq:c2c:U1#M1"))

        self.assertIn("❌", "\n".join(self.sent))


if __name__ == "__main__":
    unittest.main()
