import unittest
from unittest.mock import patch

import config
from brain.llm_brain import ask_agent, build_model_messages
from skills import mys_api


class PromptAssemblyTests(unittest.TestCase):
    """回归测试：新鲜展柜必须紧贴最后一条用户消息，压过历史里的旧结论。"""

    def test_env_context_sits_right_before_the_latest_user_message(self):
        history = [
            {"role": "user", "content": "去打蓝砚的突破 Boss"},
            {"role": "assistant", "content": "蓝砚不在展柜 JSON 中……"},
            {"role": "user", "content": "去打一次蓝砚武器的突破副本"},
        ]

        messages = build_model_messages("系统规则", "展柜数据（抓取于 现在）", history)

        self.assertEqual(messages[0], {"role": "system", "content": "系统规则"})
        self.assertEqual(messages[-1], history[-1])
        self.assertEqual(messages[-2]["content"], "展柜数据（抓取于 现在）")
        # 旧断言排在展柜数据之前
        self.assertEqual(messages[2], history[1])

    def test_empty_history_still_carries_the_env_context(self):
        messages = build_model_messages("系统规则", "展柜数据", [])

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1], {"role": "system", "content": "展柜数据"})

    def test_history_is_not_mutated(self):
        history = [{"role": "user", "content": "hi"}]
        build_model_messages("s", "env", history)

        self.assertEqual(history, [{"role": "user", "content": "hi"}])


class MysNoticeInjectionTests(unittest.TestCase):
    """米游社参考只在"玩家提到了展柜外角色"时注入，绝不整份塞进上下文。"""

    def _run(self, user_text, notice):
        captured = {}

        def fake_chat(_client, _model, messages, temperature=0.7):
            captured["messages"] = messages
            return "好的"

        store = {"env_context": "展柜数据", "env_context_names": ["蓝砚"], "wallet": {"mora": 0, "exp_books": 0, "boss_mats": {}}}
        with patch("brain.llm_brain._make_client", return_value=object()), patch(
            "brain.llm_brain.complete_chat", side_effect=fake_chat
        ), patch("brain.memory_manager.save_chat_store"), patch.object(
            config, "MYS_COOKIE", "ltuid=1;ltoken=x"
        ), patch.object(mys_api, "showcase_gap_notice", return_value=notice) as gap:
            ask_agent([{"role": "user", "content": user_text}], store, "100000000", "CLI_USER")

        return captured["messages"], gap

    def test_notice_is_appended_to_the_env_context(self):
        messages, gap = self._run("胡桃要什么材料", "【展柜外角色参考】（来源：米游社个人战绩）胡桃 Lv.80")

        gap.assert_called_once()
        self.assertEqual(gap.call_args[0][0], "胡桃要什么材料")      # 只看最后一条用户消息
        self.assertEqual(gap.call_args[0][1], ["蓝砚"])              # 展柜里都有谁
        self.assertIn("来源：米游社个人战绩", messages[-2]["content"])

    def test_no_notice_no_change(self):
        messages, _gap = self._run("今天打什么", "")

        # 提示词里本来就写着"除非它出现在【展柜外角色参考】里"，所以断言要认真正注入的那段
        self.assertNotIn("来源：米游社个人战绩", messages[-2]["content"])

    def test_mys_failure_does_not_break_the_plan(self):
        """米游社挂了也要正常规划（不能因为读不到 cookie 数据就让对话失败）。"""
        captured = {}

        def fake_chat(_client, _model, messages, temperature=0.7):
            captured["messages"] = messages
            return "好的"

        store = {"env_context": "展柜数据", "env_context_names": [], "wallet": {"mora": 0, "exp_books": 0, "boss_mats": {}}}
        with patch("brain.llm_brain._make_client", return_value=object()), patch(
            "brain.llm_brain.complete_chat", side_effect=fake_chat
        ), patch("brain.memory_manager.save_chat_store"), patch.object(
            config, "MYS_COOKIE", "ltuid=1;ltoken=x"
        ), patch.object(mys_api, "showcase_gap_notice", side_effect=RuntimeError("boom")):
            ask_agent([{"role": "user", "content": "胡桃"}], store, "1", "CLI_USER")

        self.assertIn("展柜数据", captured["messages"][-2]["content"])


if __name__ == "__main__":
    unittest.main()
