"""数据源开关 / 主动推送目标的测试（玩家反馈过的两个坑）。

  1. `last_target` 从来没人存 → 主动推送找不到目标，QQ 一条都收不到；
  2. 养成数据源默认展柜，切到养成计算器时要以计算器的计划 + 执行路线为准。
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from brain import growth_planner, growth_tools, memory_manager


class LastTargetTests(unittest.TestCase):
    """`last_target` 必须能存下来、也必须在加载白名单里（漏一个就静默丢）。"""

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="lasttarget-")
        self.patch = patch.object(config, "HISTORY_FILE",
                                  os.path.join(self.temp, "chat_context.json"))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_default_store_has_the_key(self):
        self.assertIn("last_target", memory_manager._default_store())

    def test_round_trip(self):
        store = memory_manager.load_chat_store()
        store["last_target"] = "qq:c2c:USER_A#M1"
        memory_manager.save_chat_store(store)
        self.assertEqual(memory_manager.load_chat_store()["last_target"], "qq:c2c:USER_A#M1")

    def test_loading_old_file_without_the_key_is_fine(self):
        path = config.HISTORY_FILE
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"uid": "1", "messages": []}, handle)
        self.assertEqual(memory_manager.load_chat_store()["last_target"], "")

    def test_agent_router_records_the_target(self):
        """QQ 发来一条消息 → 目标被记下来，之后的主动推送才知道往哪发。"""
        from channels import agent_router

        saved = {}
        store = {"uid": "1", "messages": [], "env_context": "", "env_context_at": "",
                 "wallet": {"mora": 0, "exp_books": 0, "boss_mats": {}}, "pending_task": None}

        with patch.object(agent_router.memory_manager, "load_chat_store", lambda: dict(store)), \
             patch.object(agent_router.memory_manager, "save_chat_store",
                          lambda value: saved.update(value)), \
             patch.object(agent_router, "refresh_store_env_context",
                          lambda *a, **k: (True, "已刷新")) :
            agent_router.handle_message("history", "qq:c2c:USER_A#M1")

        self.assertEqual(saved.get("last_target"), "qq:c2c:USER_A#M1")


class PushExecutionTargetsTests(unittest.TestCase):
    """启动推送：真发（走 send_notice），没有目标时不假装推过。"""

    def _plan(self):
        return {
            "current_character_name": "丽莎",
            "current_phase_label": "角色等级",
            "execution": {"tasks": [{
                "task_type": "boss", "task_label": "Boss 讨伐", "material": "雷光棱镜",
                "route": "无相之雷", "count": 4, "resin": 160, "status": "runnable",
                "priority_label": "① 等级突破首领", "missing": 11,
            }]},
            "estimate": {"text": "预计还要约 24.1 天"},
            "plan_id": 3,
        }

    def test_push_uses_send_notice(self):
        with patch.object(growth_planner, "build_plan", return_value=self._plan()), \
             patch.object(growth_planner.execution_queue, "is_stepwise", return_value=False), \
             patch("skills.bgi_controller.send_notice", return_value=True) as notice:
            result = growth_planner.push_execution_targets(open_id="qq:c2c:USER_A#M1")

        self.assertTrue(result["pushed"])
        self.assertEqual(notice.call_args[0][0], "qq:c2c:USER_A#M1")
        text = notice.call_args[0][1]
        self.assertIn("今天的执行目标", text)
        self.assertIn("无相之雷", text)
        self.assertIn("预计还要约 24.1 天", text)

    def test_push_without_target_only_logs(self):
        with patch.object(growth_planner, "build_plan", return_value=self._plan()), \
             patch("brain.memory_manager.load_chat_store", return_value={}), \
             patch("skills.bgi_controller.send_notice") as notice:
            result = growth_planner.push_execution_targets(open_id="")

        self.assertFalse(result["pushed"])          # 没有目标就如实说没推出去
        notice.assert_not_called()

    def test_stepwise_push_shows_routes_then_first_step(self):
        step = {"index": 1, "material": "雷光棱镜", "route": "无相之雷",
                "task_label": "Boss 讨伐", "character_name": "丽莎",
                "phase_label": "角色等级", "priority_label": "① 等级突破首领",
                "missing": 11, "count": 4, "resin": 160, "command": {}}
        with patch.object(growth_planner, "build_plan", return_value=self._plan()), \
             patch.object(growth_planner.execution_queue, "mode",
                          return_value=growth_planner.execution_queue.MODE_STEPWISE), \
             patch.object(growth_planner.execution_queue, "is_stepwise", return_value=True), \
             patch.object(growth_planner.execution_queue, "load",
                          return_value={"steps": [], "cursor": 0, "active": False}), \
             patch.object(growth_planner.execution_queue, "start",
                          return_value={"steps": [step], "cursor": 0, "active": True}), \
             patch.object(growth_planner.execution_queue, "current", return_value=step), \
             patch("skills.bgi_controller.send_notice", return_value=True) as notice:
            result = growth_planner.push_execution_targets(open_id="qq:c2c:U#M")

        self.assertTrue(result["pushed"])
        self.assertEqual(result["mode"], "stepwise")
        text = notice.call_args[0][1]
        self.assertIn("共 1 条路线", text)
        self.assertIn("y = 就跑这一条", text)


class DataSourceTests(unittest.TestCase):
    """数据源开关：默认展柜，切到计算器时以计算器为准并带上执行路线。"""

    def tearDown(self):
        config.GROWTH_DATA_SOURCE = "showcase"

    def test_default_is_showcase(self):
        for value in ("", None, "showcase", "乱写的"):
            config.GROWTH_DATA_SOURCE = value
            self.assertEqual(growth_tools.growth_data_source(), "showcase")

    def test_calculator_mode(self):
        config.GROWTH_DATA_SOURCE = "calculator"
        self.assertEqual(growth_tools.growth_data_source(), "calculator")

    def test_showcase_block_says_which_source(self):
        config.GROWTH_DATA_SOURCE = "showcase"
        with patch.object(growth_tools, "growth_context_block", return_value="🌱 基础块"):
            block, source = growth_tools.growth_source_block()
        self.assertEqual(source, "showcase")
        self.assertIn("角色展柜", block)

    def test_calculator_block_adds_execution_routes(self):
        plan = {
            "execution": {"tasks": [{
                "task_type": "boss", "task_label": "Boss 讨伐", "material": "雷光棱镜",
                "route": "无相之雷", "count": 4, "resin": 160, "priority_label": "① 等级突破首领",
            }]},
            "estimate": {"text": "预计还要约 24.1 天"},
            "current_resin": {"available": False, "reason": "读不到"},
        }
        config.GROWTH_DATA_SOURCE = "calculator"
        with patch.object(growth_tools, "growth_context_block", return_value="🌱 基础块"), \
             patch.object(growth_tools.growth_planner, "build_plan", return_value=plan):
            block, source = growth_tools.growth_source_block()

        self.assertEqual(source, "calculator")
        self.assertIn("执行路线", block)
        self.assertIn("无相之雷", block)
        self.assertIn("预计还要约 24.1 天", block)
        self.assertIn("不要用角色展柜", block)

    def test_empty_when_growth_is_not_configured(self):
        config.GROWTH_DATA_SOURCE = "calculator"
        with patch.object(growth_tools, "growth_context_block", return_value=""):
            block, source = growth_tools.growth_source_block()
        self.assertEqual(block, "")
        self.assertEqual(source, "calculator")


class StepwiseApprovalTests(unittest.TestCase):
    """分批次：推完第一条路线要**挂上待审批任务**，回 y 才跑得起来。"""

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="stepwise-")
        self.patch = patch.object(config, "HISTORY_FILE",
                                  os.path.join(self.temp, "chat_context.json"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        config.GROWTH_EXECUTION_MODE = "stepwise"
        self.addCleanup(setattr, config, "GROWTH_EXECUTION_MODE", "all")
        self.step = {"index": 1, "material": "雷光棱镜", "route": "无相之雷",
                     "task_label": "Boss 讨伐",
                     "command": {"energy_task": {"action": "run_boss", "target": "无相之雷",
                                                 "count": 4}}}
        self.fake_queue = {
            "steps": [self.step], "cursor": 0, "active": True, "done": [],
        }

    def test_push_arms_the_current_step_for_approval(self):
        store = memory_manager.load_chat_store()
        store["last_target"] = "qq:c2c:USER_A#M1"
        memory_manager.save_chat_store(store)

        with patch.object(growth_planner, "build_plan", return_value={
                "execution": {"tasks": []}, "estimate": {}, "plan_id": 1}), \
             patch.object(growth_planner.execution_queue, "load", return_value=self.fake_queue), \
             patch.object(growth_planner.execution_queue, "current", return_value=self.step), \
             patch("skills.bgi_controller.send_notice", return_value=True):
            growth_planner.push_execution_targets()

        pending = memory_manager.load_chat_store().get("pending_task") or {}
        self.assertEqual(pending.get("bgi_cmd"), self.step["command"])
        self.assertEqual(pending.get("open_id"), "qq:c2c:USER_A#M1")
        self.assertEqual((pending.get("growth_step") or {}).get("material"), "雷光棱镜")

    def test_no_target_means_no_approval(self):
        """推不出去就别挂审批 —— 否则玩家莫名其妙看到"有没有待确认的计划"。"""
        with patch.object(growth_planner, "build_plan", return_value={
                "execution": {"tasks": []}, "estimate": {}, "plan_id": 1}), \
             patch.object(growth_planner.execution_queue, "load", return_value=self.fake_queue), \
             patch.object(growth_planner.execution_queue, "current", return_value=self.step), \
             patch("brain.memory_manager.load_chat_store", return_value={}), \
             patch("skills.bgi_controller.send_notice") as notice:
            growth_planner.push_execution_targets(open_id="")

        notice.assert_not_called()
        pending = memory_manager.load_chat_store().get("pending_task")
        self.assertIsNone(pending)

    def test_router_drops_the_queue_when_the_player_says_something_else(self):
        """回 y 之外的内容 → 放弃队列，并把这句话当新请求（规格原话要求）。"""
        from channels import agent_router

        dropped = []
        store = {"uid": "1", "messages": [], "env_context": "", "env_context_at": "",
                 "wallet": {"mora": 0, "exp_books": 0, "boss_mats": {}},
                 "last_target": "qq:c2c:U#M",
                 "pending_task": {"bgi_cmd": self.step["command"], "open_id": "qq:c2c:U#M",
                                  "uid": "1", "growth_step": {"index": 1}}}
        saved = {}

        with patch.object(agent_router.memory_manager, "load_chat_store", lambda: dict(store)), \
             patch.object(agent_router.memory_manager, "save_chat_store",
                          lambda value: saved.update(value)), \
             patch.object(agent_router, "refresh_store_env_context", lambda *a, **k: (True, "x")), \
             patch.object(agent_router, "reply", lambda *a, **k: None), \
             patch.object(agent_router, "_drop_growth_queue",
                          lambda reason="": dropped.append(reason)), \
             patch.object(agent_router.llm_brain, "ask_agent", lambda *a, **k: None):
            agent_router.handle_message("今天先刷别的", "qq:c2c:U#M")

        self.assertIsNone(saved.get("pending_task"))
        self.assertEqual(len(dropped), 1)

    def test_router_keeps_the_queue_when_the_player_cancels(self):
        from channels import agent_router

        dropped = []
        store = {"uid": "1", "messages": [], "env_context": "", "env_context_at": "",
                 "wallet": {"mora": 0, "exp_books": 0, "boss_mats": {}},
                 "last_target": "qq:c2c:U#M",
                 "pending_task": {"bgi_cmd": self.step["command"], "open_id": "qq:c2c:U#M",
                                  "uid": "1", "growth_step": {"index": 1}}}
        saved = {}

        with patch.object(agent_router.memory_manager, "load_chat_store", lambda: dict(store)), \
             patch.object(agent_router.memory_manager, "save_chat_store",
                          lambda value: saved.update(value)), \
             patch.object(agent_router, "refresh_store_env_context", lambda *a, **k: (True, "x")), \
             patch.object(agent_router, "reply", lambda *a, **k: None), \
             patch.object(agent_router, "_drop_growth_queue",
                          lambda reason="": dropped.append(reason)):
            agent_router.handle_message("取消", "qq:c2c:U#M")

        self.assertIsNone(saved.get("pending_task"))
        self.assertEqual(len(dropped), 1)      # 取消也要把队列收掉


if __name__ == "__main__":
    unittest.main()
