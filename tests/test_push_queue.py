"""跨进程推送队列的测试（QQ 收不到推送的那个坑）。

玩家反馈过两次："展柜数据本地思考完 QQ 收不到"、"启动推送收不到"。
根因：推送是在 CLI / Studio 进程里发的，而 QQ 的发送实现只注册在**机器人进程**，
`channel_router.try_send()` 找不到发送器 → 调用方又拿 `qq:...` 当飞书 id 去发 →
**消息凭空消失，日志里连错都没有**。

修法：`api/notice_queue.py` 做跨进程文件队列，机器人进程定期取走发送。
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from api import channel_router, notice_queue
from brain import execution_queue
from channels import qq_bot


class NoticeQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "HISTORY_FILE",
                               os.path.join(self.tmp.name, "chat_context.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        notice_queue.clear()

    def test_enqueue_then_drain(self):
        self.assertTrue(notice_queue.enqueue("qq:c2c:U#M1", "第一条"))
        self.assertTrue(notice_queue.enqueue("qq:c2c:U#M1", "第二条"))

        taken = notice_queue.drain()
        self.assertEqual([text for _target, text in taken], ["第一条", "第二条"])
        self.assertEqual(notice_queue.drain(), [])          # 取走即删除，不会重复发

    def test_drain_can_filter_one_target(self):
        notice_queue.enqueue("qq:c2c:A#M1", "给 A 的")
        notice_queue.enqueue("qq:c2c:B#M2", "给 B 的")

        taken = notice_queue.drain(target="qq:c2c:A#M1")
        self.assertEqual([text for _target, text in taken], ["给 A 的"])
        self.assertEqual(len(notice_queue.peek()), 1)       # B 的那条还在

    def test_queue_is_bounded(self):
        for index in range(notice_queue.QUEUE_LIMIT + 5):
            notice_queue.enqueue("qq:c2c:U#M", f"第 {index} 条")
        items = notice_queue.peek()
        self.assertEqual(len(items), notice_queue.QUEUE_LIMIT)
        self.assertEqual(items[-1]["text"], f"第 {notice_queue.QUEUE_LIMIT + 4} 条")

    def test_junk_entries_are_ignored(self):
        self.assertFalse(notice_queue.enqueue("", "没目标"))
        self.assertFalse(notice_queue.enqueue("qq:c2c:U#M", ""))
        path = os.path.join(self.tmp.name, notice_queue.QUEUE_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{这不是 JSON")
        self.assertEqual(notice_queue.peek(), [])           # 坏文件按空队列处理，不炸

    def test_file_is_written_atomically(self):
        notice_queue.enqueue("qq:c2c:U#M", "内容")
        path = os.path.join(self.tmp.name, notice_queue.QUEUE_FILE)
        data = json.load(open(path, encoding="utf-8"))
        self.assertEqual(data["items"][0]["target"], "qq:c2c:U#M")
        # 不留临时文件
        leftovers = [name for name in os.listdir(self.tmp.name) if ".tmp-" in name]
        self.assertEqual(leftovers, [])


class RouterEnqueueTests(unittest.TestCase):
    """本进程发不了的 QQ 目标 → 入队（而不是当成飞书 id 去发）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "HISTORY_FILE",
                               os.path.join(self.tmp.name, "chat_context.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        notice_queue.clear()
        channel_router.clear()          # 确保没有 QQ 发送器（模拟 CLI / Studio 进程）

    def tearDown(self):
        channel_router.clear()

    def test_qq_target_is_queued_when_this_process_cannot_send(self):
        self.assertFalse(channel_router.owns("qq:c2c:USER#M1"))
        self.assertTrue(channel_router.belongs_to_another_process("qq:c2c:USER#M1"))
        self.assertTrue(channel_router.try_send("qq:c2c:USER#M1", "启动推送"))
        self.assertEqual([text for _t, text in notice_queue.peek() and
                          [(i["target"], i["text"]) for i in notice_queue.peek()]], ["启动推送"])

    def test_feishu_target_still_falls_through(self):
        """非 QQ 目标还是返回 False（继续走原来的飞书逻辑），别乱入队。"""
        self.assertFalse(channel_router.belongs_to_another_process("ou_abc123"))
        self.assertFalse(channel_router.try_send("ou_abc123", "飞书消息"))
        self.assertEqual(notice_queue.peek(), [])

    def test_registered_sender_wins_over_queue(self):
        """机器人进程自己注册了发送器时，走发送器，**不入队**。"""
        seen = []
        channel_router.register_sender(channel_router.QQ_PREFIX,
                                       lambda target, text: seen.append(text) or True)
        self.assertFalse(channel_router.belongs_to_another_process("qq:c2c:U#M"))
        self.assertTrue(channel_router.try_send("qq:c2c:U#M", "直接发"))
        self.assertEqual(seen, ["直接发"])
        self.assertEqual(notice_queue.peek(), [])
        channel_router.unregister_sender(channel_router.QQ_PREFIX)


class QqBotDrainTests(unittest.TestCase):
    """机器人进程：把队列取走并真的发出去；发不出去就退回它自己的补发队列。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "HISTORY_FILE",
                               os.path.join(self.tmp.name, "chat_context.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        notice_queue.clear()
        self.client = qq_bot.QQBotClient(
            qq_bot.QQBotConfig(appid="10001", secret="s3cret", allowed_users="user1"))
        self.client._pending_path = lambda: os.path.join(self.tmp.name, "pending.json")

    def tearDown(self):
        channel_router.clear()

    def test_drain_sends_through_the_bot(self):
        notice_queue.enqueue("qq:c2c:USER#M1", "📋 今天的执行目标")
        sent = []
        with patch.object(self.client, "send_threadsafe",
                          lambda target, text: sent.append((target, text)) or True):
            count = self.client.drain_external_pushes()

        self.assertEqual(count, 1)
        self.assertEqual(sent[0][0], "qq:c2c:USER#M1")
        self.assertIn("执行目标", sent[0][1])
        self.assertEqual(notice_queue.peek(), [])

    def test_failed_send_falls_back_to_the_bot_queue(self):
        notice_queue.enqueue("qq:c2c:USER#M1", "发不出去的推送")
        with patch.object(self.client, "send_threadsafe", return_value=False):
            count = self.client.drain_external_pushes()

        self.assertEqual(count, 0)
        self.assertTrue(self.client._pending, "发不出去时必须退回补发队列，不能丢")

    def test_input_only_mode_echoes_locally(self):
        """单向模式（QQ_BOT_REPLY_MODE=off）：不发 QQ，但内容要在本地留痕。"""
        notice_queue.enqueue("qq:c2c:USER#M1", "单向模式下的推送")
        echoed = []
        self.client.config = qq_bot.QQBotConfig(appid="1", secret="s", reply_mode="off")
        with patch.object(channel_router, "echo_locally",
                          lambda label, text: echoed.append(text)):
            count = self.client.drain_external_pushes()

        self.assertEqual(count, 1)
        self.assertEqual(echoed, ["单向模式下的推送"])


class FreeRouteFeedbackTests(unittest.TestCase):
    """采集 / 魔物掉落路线不报"还要 N 个、本趟 M 次"（一条路线打一片怪，数字没意义）。"""

    def _step(self, **kwargs):
        step = {"index": 1, "material": "幻造晶鳞石", "route": "肌生晶石的妖精",
                "task_label": "敌人与魔物", "character_name": "奥黛塔",
                "phase_label": "天赋", "priority_label": "② 采集 / 普通魔物掉落（不耗体力）",
                "missing": 13, "count": 1, "resin": 0, "command": {}}
        step.update(kwargs)
        return step

    def test_free_route_text_is_short(self):
        step = self._step()
        state = {"steps": [step], "cursor": 0, "active": True}
        with patch.object(execution_queue, "current", return_value=step):
            text = "\n".join(execution_queue.render_current(state))

        self.assertIn("第 1/1 条路线", text)
        self.assertIn("肌生晶石的妖精", text)
        self.assertNotIn("还要 13 个", text)      # ← 玩家要求删掉的正是这一行
        self.assertNotIn("本趟", text)
        self.assertIn("y = 就跑这一条", text)

    def test_resin_route_keeps_the_numbers(self):
        step = self._step(task_type="boss", material="雷光棱镜", route="无相之雷",
                          task_label="Boss 讨伐", missing=11, count=4, resin=160)
        state = {"steps": [step], "cursor": 0, "active": True}
        with patch.object(execution_queue, "current", return_value=step):
            text = "\n".join(execution_queue.render_current(state))

        self.assertIn("还要 11 个", text)          # 体力任务保留（趟数是你选的）
        self.assertIn("本趟 4 次", text)
        self.assertIn("预计耗体力 160", text)


if __name__ == "__main__":
    unittest.main()
