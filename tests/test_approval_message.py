"""审批消息组装（brain/llm_brain.build_approval_message）的测试。

背景：QQ 上一条审批消息曾经是"推理正文 + ```json 计划块 + 审批屏"，2400+ 字符，
被切成 4 条消息还把某一行劈成两半。聊天通道现在只发精简版，完整推理打到电脑端日志。
"""

import unittest

from brain import llm_brain

AI_REPLY = (
    "你好！欢迎回来 👋 我按自动优先级扫描排了一轮。\n\n"
    "### 🎯 今日主攻目标\n**蓝砚「秘刻金纹的源核」**\n\n"
    "对账逻辑（严格按展柜缺口 vs 虚拟账本）：" + "展柜核对蓝砚 71 级 < 81 未毕业，账本已刷 0 < 20，故可执行。" * 12 + "\n\n"
    "```json\n{\"energy_task\": {\"action\": \"run_boss\", \"target\": \"蓝砚\"}, "
    "\"free_task\": [{\"action\": \"gather\", \"target\": \"清水玉\"}]}\n```"
)
PLAN_TEXT = "⚔️ 体力目标：秘源机兵·构型械\n🌿 采集目标：清水玉"
GUARD_TEXT = "\n🔎 角色解析：蓝砚 的突破材料「秘刻金纹的源核」→ 「秘源机兵·构型械」"


class ApprovalMessageTests(unittest.TestCase):
    def test_full_message_keeps_reasoning_and_json(self):
        """飞书 / 终端照旧：推理正文 + 计划块 + 审批屏，一个都不少。"""
        message = llm_brain.build_approval_message(AI_REPLY, PLAN_TEXT, GUARD_TEXT)

        self.assertTrue(message.startswith("你好！欢迎回来"))
        self.assertIn("```json", message)
        self.assertIn(llm_brain.APPROVAL_HEADER, message)
        self.assertIn("⚔️ 体力目标：秘源机兵·构型械", message)
        self.assertIn("🔎 角色解析", message)
        self.assertIn("'y'", message)

    def test_concise_message_drops_reasoning_and_json(self):
        """聊天通道：只留"要执行什么"，手机上一条就能看完。"""
        message = llm_brain.build_approval_message(AI_REPLY, PLAN_TEXT, GUARD_TEXT, concise=True)

        self.assertNotIn("欢迎回来", message)
        self.assertNotIn("json", message)
        self.assertTrue(message.startswith(llm_brain.APPROVAL_HEADER))
        self.assertIn("⚔️ 体力目标：秘源机兵·构型械", message)
        self.assertIn("🔎 角色解析", message)
        self.assertIn("y = 执行", message)
        self.assertLess(len(message), 400)

    def test_concise_message_without_guard_notices(self):
        message = llm_brain.build_approval_message(AI_REPLY, PLAN_TEXT, concise=True)

        self.assertIn("🌿 采集目标：清水玉", message)
        self.assertNotIn("None", message)
        self.assertNotIn("🔎", message)

    def test_concise_shrinks_the_number_of_qq_messages(self):
        """把"太长"这件事量化：精简版必须能塞进 QQ 的一条消息（800 字）。"""
        from channels import qq_bot

        full = llm_brain.build_approval_message(AI_REPLY, PLAN_TEXT, GUARD_TEXT)
        concise = llm_brain.build_approval_message(AI_REPLY, PLAN_TEXT, GUARD_TEXT, concise=True)

        self.assertGreater(len(qq_bot.split_message(full)), 1)
        self.assertEqual(len(qq_bot.split_message(concise)), 1)


if __name__ == "__main__":
    unittest.main()
