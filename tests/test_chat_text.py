"""聊天文本纯文本化（channels/chat_text.py）的测试。

用玩家实测那条 QQ 消息（推理正文 + ```json 计划块 + 审批屏）当样本：
不处理时它 2400+ 字符、会被切成 4 条 QQ 消息，纯文本化之后要能压缩到 1 条。
"""

import unittest

from channels import chat_text

# 取自真实 QQ 聊天记录（截断版）：这正是"太多信息"的现场
REAL_WORLD_REPLY = """你好！欢迎回来 👋 你没点名具体目标，我就按【自动优先级扫描】+ 最新展柜帮你排一轮今日计划。

### 🎯 今日主攻目标
**Boss 材料 · 蓝砚「秘刻金纹的源核」**（第一顺位，自动扫描命中）

对账逻辑（严格按展柜缺口 vs 虚拟账本）：
- **展柜核对**：蓝砚 **71 级 < 81 未毕业**，突破 `Boss材料`「秘刻金纹的源核」来源 **【秘源机兵·构型械】**，缺口 `needed` = **20**。
- **停手逻辑**：📦 账本【已刷取Boss材料】= **无**，即已刷 **0 < 20** → **未备齐，可执行**。

---
### 🤖 BGI 执行指令
```json
{
  "energy_task": {
    "action": "run_boss",
    "target": "蓝砚",
    "reason": "玩家未点名，按自动优先级扫描：第一顺位 Boss 材料命中。展柜核对蓝砚71级<81未毕业，突破Boss材料『秘刻金纹的源核』来源【秘源机兵·构型械】，缺口 needed=20；Agent 虚拟账本已刷Boss材料为无（0<20），停手逻辑不触发，可执行。Boss 挑战无日期限制，今日周六可打。target 填角色名，系统按本地字典确定性翻译为官方 Boss 名。按零信任库存原则，摩拉/经验书排在材料之后，本轮不排。"
  },
  "free_task": [
    {
      "action": "gather",
      "target": "清水玉",
      "reason": "展柜第1位角色蓝砚71级<81未毕业，清水玉为其突破特产，纳入每3天轮换采集（不消耗体力）。"
    },
    {
      "action": "gather",
      "target": "慕风蘑菇",
      "reason": "展柜第2位角色芭芭拉60级<81未毕业，慕风蘑菇为其突破特产，纳入每3天轮换采集（不消耗体力）。"
    },
    {
      "action": "gather",
      "target": "沙脂蛹",
      "reason": "展柜第3位角色迪希雅71级<81未毕业，沙脂蛹为其突破特产，纳入每3天轮换采集（不消耗体力）。每次最多列前3位角色的特产，故止于此。"
    }
  ]
}
```

====================
🛑 [系统拦截] 请确认是否执行上述计划？
⚔️ 体力目标：秘源机兵·构型械
🌿 采集目标：清水玉、慕风蘑菇、沙脂蛹
🔎 角色解析：蓝砚 的突破材料「秘刻金纹的源核」→ 「秘源机兵·构型械」
👉 回复 'y' 批准执行
"""


class PlainTextTests(unittest.TestCase):
    def test_drops_json_plan_block(self):
        out = chat_text.to_plain_text(REAL_WORLD_REPLY)

        self.assertNotIn("energy_task", out)
        self.assertNotIn("```", out)
        self.assertNotIn('"action"', out)
        # 正文与审批屏都要留下
        self.assertIn("今日主攻目标", out)
        self.assertIn("秘源机兵·构型械", out)

    def test_no_placeholder_leaks(self):
        """内部占位符绝不能漏到玩家眼前（曾经漏成一行 "CODE0"）。"""
        out = chat_text.to_plain_text(REAL_WORLD_REPLY)

        self.assertNotIn("CODE0", out)
        self.assertNotIn("\x00", out)

    def test_drops_the_heading_that_introduced_the_block(self):
        """「### 🤖 BGI 执行指令」这种只为 JSON 块服务的标题，块删了就一起删。"""
        out = chat_text.to_plain_text("正文一句话。\n\n### 🤖 BGI 执行指令\n```json\n{}\n```\n")

        self.assertEqual(out, "正文一句话。")

    def test_strips_markdown_noise(self):
        out = chat_text.to_plain_text(
            "### 标题\n**加粗** 与 __另一种加粗__ 还有 `行内代码`\n---\n=====\n正常一行"
        )

        self.assertIn("标题", out)
        self.assertNotIn("#", out)
        self.assertNotIn("**", out)
        self.assertNotIn("__", out)
        self.assertNotIn("`", out)
        self.assertNotIn("---", out)
        self.assertNotIn("=====", out)
        self.assertIn("加粗 与 另一种加粗 还有 行内代码", out)

    def test_collapses_blank_lines_and_trailing_spaces(self):
        out = chat_text.to_plain_text("第一行   \n\n\n\n第二行\n\n\n")
        self.assertEqual(out, "第一行\n\n第二行")

    def test_links_become_plain(self):
        out = chat_text.to_plain_text("看 [官方文档](https://bot.q.qq.com/wiki) 即可")
        self.assertEqual(out, "看 官方文档（https://bot.q.qq.com/wiki） 即可")

    def test_keep_code_can_keep_fences(self):
        """排查问题时可以要求保留代码块。"""
        out = chat_text.to_plain_text("说明：\n```json\n{}\n```", keep_code=True)
        self.assertIn("```", out)

    def test_empty_text_is_safe(self):
        self.assertEqual(chat_text.to_plain_text(""), "")
        self.assertEqual(chat_text.to_plain_text(None), "")

    def test_real_world_reply_shrinks_into_one_message(self):
        """实测样本：处理前 2400+ 字符（4 条消息），处理后必须能塞进一条（<800）。"""
        raw = chat_text.to_plain_text(REAL_WORLD_REPLY)

        self.assertLess(len(REAL_WORLD_REPLY), 3000)
        self.assertLess(len(raw), 800, f"压缩后仍有 {len(raw)} 字：{raw}")

    def test_summarize(self):
        self.assertEqual(chat_text.summarize("短句"), "短句")
        self.assertTrue(chat_text.summarize("长" * 400, limit=50).endswith("…"))
        self.assertLessEqual(len(chat_text.summarize("长" * 400, limit=50)), 50)
        # 摘要里不该有 Markdown / 换行
        self.assertNotIn("\n", chat_text.summarize("### 标题\n**加粗**"))


if __name__ == "__main__":
    unittest.main()
