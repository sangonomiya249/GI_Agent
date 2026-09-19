"""体力换算 / 预计培养天数 / 执行队列（分批次）的测试。

覆盖玩家明确要求的三件事：
  1. 副本 / 地脉的趟数按**当前体力**算（不再硬编码 40 体力那种固定次数）；
  2. 刷取优先级：等级突破首领 → 采集 / 魔物掉落 → 经验地脉 → 武器副本 → 天赋副本 → 摩拉；
  3. 预计培养天数：按每点体力的产出比例，把缺口折成"还要几天"。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from brain import execution_queue, material_planner, resin_math
from skills import mys_resin


class ResinParseTests(unittest.TestCase):
    """每日便笺 → 我们自己的形状。"""

    def test_parses_current_and_recovery(self):
        parsed = mys_resin.parse_resin({"data": {
            "current_resin": 154, "max_resin": 200, "resin_recovery_time": "3720",
            "remain_resin_discount_num": 3, "finished_task_num": 2, "total_task_num": 4,
        }})
        self.assertEqual(parsed["current"], 154)
        self.assertEqual(parsed["max"], 200)
        self.assertEqual(parsed["recovery_text"], "1 小时 2 分")
        self.assertEqual(parsed["weekly_discount_left"], 3)

    def test_junk_payload_is_rejected(self):
        self.assertIsNone(mys_resin.parse_resin({"data": {}}))
        self.assertIsNone(mys_resin.parse_resin({"retcode": -1, "message": "x"}))
        self.assertIsNone(mys_resin.parse_resin(None))

    def test_recovery_text_when_full(self):
        self.assertEqual(mys_resin._format_recovery(0), "已回满")

    def test_probe_skips_request_when_cookie_cannot_read_resin(self):
        """养成计算器那套 v2 cookie 读不了体力 —— 不该发请求，也不该假装体力是 0。"""
        mys_resin.cache_clear()
        with patch("skills.mys_resin.fetch_resin") as fetch, \
             patch("skills.mys_resin.manual_state", return_value=None):
            state = mys_resin.probe(cookie="ltuid_v2=1; ltoken_v2=abc; cookie_token_v2=x")
        fetch.assert_not_called()
        self.assertFalse(state["available"])
        self.assertEqual(state["current"], 0)
        self.assertIn("app", state["reason"])
        mys_resin.cache_clear()

    def test_probe_falls_back_to_manual_resin(self):
        """接口被风控挡着时的主力方案：玩家填过的数字 + 按 8 分钟 1 点回涨推算。"""
        mys_resin.cache_clear()
        record = {"available": True, "current": 27, "max": 0, "base": 27, "recovered": 0,
                  "recorded_at": "2026-09-19 17:00:00", "stale": False,
                  "source": "手动录入（按 8 分钟 1 点推算已回涨）", "reason": ""}
        with patch("skills.mys_resin.manual_state", return_value=dict(record)), \
             patch("skills.mys_resin.fetch_resin") as fetch:
            state = mys_resin.probe(cookie="ltuid_v2=1; ltoken_v2=abc")
        fetch.assert_not_called()                # 没有 v1 就不发请求
        self.assertTrue(state["available"])
        self.assertEqual(state["current"], 27)
        self.assertIn("手动", state["reason"])
        mys_resin.cache_clear()

    def test_manual_state_decays_with_time(self):
        """记下 27 点、过了 2 小时 → 应该按每 8 分钟 1 点回涨。"""
        fake_db = FakeSettings()
        with patch("skills.mys_resin._load_manual", return_value={
                "current": 27, "max": 200, "at": "2026-09-19 10:00:00"}), \
             patch("brain.growth_db", fake_db):
            now = __import__("datetime").datetime(2026, 9, 19, 12, 0, 0)
            state = mys_resin.manual_state(now=now)
        self.assertEqual(state["base"], 27)
        self.assertEqual(state["recovered"], 15)     # 120 分钟 ÷ 8 = 15 点
        self.assertEqual(state["current"], 42)       # 27 + 15
        self.assertFalse(state["stale"])

    def test_manual_state_caps_at_max(self):
        with patch("skills.mys_resin._load_manual", return_value={
                "current": 195, "max": 200, "at": "2026-09-19 10:00:00"}):
            now = __import__("datetime").datetime(2026, 9, 19, 20, 0, 0)
            state = mys_resin.manual_state(now=now)
        self.assertEqual(state["current"], 200)      # 越过上限就按上限

    def test_set_manual_persists(self):
        fake = FakeSettings()
        with patch("brain.growth_db", fake):
            result = mys_resin.set_manual(27, top=200)
        self.assertTrue(result["ok"])
        self.assertEqual(fake.data.get(mys_resin.MANUAL_KEY) is not None, True)
        self.assertIn('"current": 27', fake.data[mys_resin.MANUAL_KEY])

    def test_set_manual_rejects_junk(self):
        self.assertFalse(mys_resin.set_manual("不是数字")["ok"])

    def test_runs_for_respects_resin_and_deficit(self):
        self.assertEqual(mys_resin.runs_for(100, "domain"), 5)          # 20 体力一次
        self.assertEqual(mys_resin.runs_for(100, "boss"), 2)            # 40 体力一次
        self.assertEqual(mys_resin.runs_for(1000, "domain", needed=3), 3)   # 缺口才是瓶颈
        self.assertEqual(mys_resin.runs_for(20, "domain", needed=9), 1)     # 体力才是瓶颈
        self.assertEqual(mys_resin.runs_for(0, "domain"), 0)


class ResinMathTests(unittest.TestCase):
    """缺口 → 体力 → 天数。"""

    def test_green_equivalents_from_name_suffix(self):
        # 天赋书：教导 1 绿 / 指引 3 绿 / 哲学 9 绿（名字后缀就能定，不依赖数据版本）
        self.assertEqual(resin_math.green_equivalent("「坚忍」的教导"), 1)
        self.assertEqual(resin_math.green_equivalent("「坚忍」的指引"), 3)
        self.assertEqual(resin_math.green_equivalent("「坚忍」的哲学"), 9)
        self.assertEqual(resin_math.green_equivalent("「坚忍」的哲学", 76), 684)

    def test_green_equivalents_from_craft_chain(self):
        # 武器素材靠 genshin-db 的合成链：始龀(1) → 裂齿(3) → 断牙(9)
        name = "凛风奔狼的断牙"
        tier = __import__("brain.material_family", fromlist=["x"]).craft_tier(name)
        if tier is None:
            self.skipTest("知识表里没有这个材料的档位（没跑构建脚本？）")
        self.assertEqual(resin_math.green_equivalent(name), tier["green"])

    def test_exp_book_conversion(self):
        self.assertEqual(resin_math.exp_book_equivalent("大英雄的经验", 10), 10)
        self.assertEqual(resin_math.exp_book_equivalent("冒险家的经验", 4), 1)
        self.assertEqual(resin_math.exp_book_equivalent("流浪者的经验", 20), 1)
        self.assertEqual(resin_math.exp_book_equivalent("完全不认识的素材", 5), 0)

    def test_classification(self):
        self.assertEqual(resin_math.classify("摩拉")[0], "mora_leyline")
        self.assertEqual(resin_math.classify("「正义」的哲学", game_type="角色天赋素材")[0],
                         "talent_domain")
        self.assertEqual(resin_math.classify("凛风奔狼的断牙", game_type="武器突破素材")[0],
                         "weapon_domain")
        self.assertEqual(resin_math.classify("大英雄的经验", game_type="角色经验素材")[0],
                         "exp_leyline")
        self.assertEqual(resin_math.classify("雷光棱镜", family_type="world_boss")[0], "boss")
        self.assertEqual(resin_math.classify("落落莓", channel="local")[0], "free")
        self.assertEqual(resin_math.classify("史莱姆原浆", family_type="enemy_drop")[0], "free")
        self.assertEqual(resin_math.classify("扭曲的枯枝", family_type="weekly_boss")[0],
                         "weekly_boss")

    def test_resin_for_matches_the_given_rates(self):
        # 天赋秘境 0.5135 等效绿素材 / 体力 → 9 绿素材 ≈ 17.5 体力
        resin = resin_math.resin_for("「正义」的哲学", 1, kind="talent_domain")
        self.assertAlmostEqual(resin, 9 / 0.5135, places=3)
        # 经验地脉 0.3235 本 / 体力
        self.assertAlmostEqual(resin_math.resin_for("大英雄的经验", 1, kind="exp_leyline"),
                               1 / 0.3235, places=3)
        # 首领 0.0765 / 体力
        self.assertAlmostEqual(resin_math.resin_for("雷光棱镜", 1, kind="boss"),
                               1 / 0.0765, places=3)
        # 采集不耗体力
        self.assertEqual(resin_math.resin_for("落落莓", 60, kind="free"), 0.0)

    def test_estimate_days(self):
        rows = [
            {"character_id": 1, "phase": "talent", "materials": [
                {"item_id": 1, "item_name": "「正义」的哲学", "missing": 10},
                {"item_id": 2, "item_name": "落落莓", "missing": 30},
            ]},
        ]
        result = resin_math.estimate(rows, resin_per_day_value=180)
        # 10 × 9 绿 = 90 绿 ÷ 0.5135 ≈ 176 体力 → 0.98 天
        self.assertGreater(result["total_resin"], 100)
        self.assertAlmostEqual(result["days"], round(result["total_resin"] / 180, 1), places=1)
        self.assertIn("天", result["text"])
        self.assertTrue(any("不占体力" in line for line in [result["text"]]))
        self.assertEqual([item["material"] for item in result["free_materials"]], ["落落莓"])

    def test_estimate_scopes_the_days_to_all_characters(self):
        """玩家问过："24.1 天是只算奥黛塔还是所有角色合计？" → 文案必须说清是**合计**，
        并且能给出每人分摊。"""
        rows = [
            {"character_id": 1, "character_name": "奥黛塔", "materials": [
                {"item_name": "「慈爱」的哲学", "missing": 10}]},
            {"character_id": 2, "character_name": "丽莎", "materials": [
                {"item_name": "「正义」的哲学", "missing": 10}]},
        ]
        result = resin_math.estimate(rows, resin_per_day_value=180)
        self.assertIn("全部 2 个角色合计", result["text"])
        self.assertEqual(len(result["characters"]), 2)
        self.assertAlmostEqual(sum(item["share"] for item in result["characters"]), 100, delta=1)
        # 每人天数各自四舍五入后会累加出偏差（0.97→1.0 × 2 = 2.0 vs 总计 1.9），
        # 所以断言只要求"看起来一致"（差 ≤ 0.3 天），占比才是精确的口径。
        self.assertAlmostEqual(sum(item["days"] for item in result["characters"]),
                               result["days"], delta=0.3)
        # describe 里要能看到占比
        joined = "\n".join(resin_math.describe(result))
        self.assertIn("占比", joined)

    def test_estimate_ignores_completed_rows(self):
        rows = [{"skipped": True, "materials": [{"item_name": "摩拉", "missing": 999999}]},
                {"materials": [{"item_name": "摩拉", "missing": 0}]}]
        result = resin_math.estimate(rows)
        self.assertEqual(result["total_resin"], 0)
        self.assertIn("不需要额外体力", result["text"])

    def test_domain_runs_merge_material_tiers(self):
        task = {
            "task_type": material_planner.TASK_DOMAIN,
            "phase": "talent",
            "material": "「正义」的教导 等 2 种",
            "missing": 7,
            "materials": [
                {"item_name": "「正义」的教导", "missing": 4},
                {"item_name": "「正义」的指引", "missing": 3},
            ],
        }
        # 4 绿 + 3 蓝(=9 绿) = 13 等效绿；20 体力一趟约 10.27 绿，所以要 2 趟。
        self.assertEqual(material_planner._runs_needed(task), 2)

    def test_completed_energy_task_never_gets_a_run(self):
        for kind, extra in (
            (material_planner.TASK_DOMAIN, {"phase": "talent"}),
            (material_planner.TASK_LEYLINE, {"route": material_planner.LEYLINE_EXP}),
            (material_planner.TASK_BOSS, {}),
        ):
            task = {
                "task_type": kind,
                "missing": 0,
                "material": "已齐材料",
                **extra,
            }
            material_planner._apply_runs(
                task,
                resin_state={"available": True, "current": 69},
            )
            self.assertEqual(task["count"], 0)
            self.assertEqual(task["resin"], 0)
            self.assertIn("缺口为 0", task["count_note"])

    def test_non_domain_materials_are_keyed_by_name(self):
        row = {"character_id": 1, "phase": "character_level"}
        source = {"type": material_planner.TASK_HUNT, "item_id": 99}
        first = {"item_id": 99, "item_name": "孢囊晶尘"}
        second = {"item_id": 99, "item_name": "蕈兽孢子"}
        self.assertNotEqual(
            material_planner._task_key(row, first, source),
            material_planner._task_key(row, second, source),
        )

    def test_same_non_domain_material_can_merge_across_phases(self):
        source = {"type": material_planner.TASK_HUNT, "item_id": 99}
        material = {"item_id": 99, "item_name": "孢囊晶尘"}
        level = {"character_id": 1, "phase": "character_level"}
        talent = {"character_id": 1, "phase": "talent"}
        self.assertEqual(
            material_planner._task_key(level, material, source),
            material_planner._task_key(talent, material, source),
        )


class PriorityTests(unittest.TestCase):
    """玩家给定的刷取顺序。"""

    def test_priority_order(self):
        self.assertLess(material_planner.task_priority({"task_type": "boss"}),
                        material_planner.task_priority({"task_type": "hunt"}))
        self.assertLess(material_planner.task_priority({"task_type": "hunt"}),
                        material_planner.task_priority({"task_type": "leyline",
                                                        "route": material_planner.LEYLINE_EXP}))
        self.assertLess(material_planner.task_priority({"task_type": "leyline",
                                                        "route": material_planner.LEYLINE_EXP}),
                        material_planner.task_priority({"task_type": "domain",
                                                        "phase": "weapon_level"}))
        self.assertLess(material_planner.task_priority({"task_type": "domain",
                                                        "phase": "weapon_level"}),
                        material_planner.task_priority({"task_type": "domain", "phase": "talent"}))
        self.assertLess(material_planner.task_priority({"task_type": "domain", "phase": "talent"}),
                        material_planner.task_priority({"task_type": "leyline",
                                                        "route": material_planner.LEYLINE_MORA}))

    def test_energy_task_follows_priority_not_list_order(self):
        """列表顺序里天赋本在前，但优先级说首领先上 —— 必须选首领。"""
        tasks = [
            {"task_type": "domain", "phase": "talent", "material": "「正义」的哲学",
             "status": "runnable", "missing": 10, "count": 2, "domain": "苍白的遗荣",
             "route": "苍白的遗荣", "item_id": 1},
            {"task_type": "boss", "phase": "character_level", "material": "雷光棱镜",
             "status": "runnable", "missing": 5, "count": 2, "route": "无相之雷", "item_id": 2},
        ]
        command = material_planner.tasks_to_bgi_command(tasks)
        self.assertEqual(command["energy_task"]["action"], "run_boss")
        self.assertEqual(command["energy_task"]["target"], "无相之雷")

    def test_zero_run_tasks_are_not_dispatched(self):
        tasks = [
            {"task_type": "domain", "phase": "talent", "material": "「正义」的哲学",
             "status": "runnable", "missing": 10, "count": 0, "domain": "苍白的遗荣",
             "route": "苍白的遗荣", "item_id": 1, "skipped_this_round": True},
            {"task_type": "hunt", "phase": "talent", "material": "史莱姆原浆",
             "status": "runnable", "missing": 4, "count": 1, "route": "史莱姆", "item_id": 3},
        ]
        command = material_planner.tasks_to_bgi_command(tasks)
        self.assertNotIn("energy_task", command)
        self.assertEqual(command["free_task"], [{"action": "hunt", "target": "史莱姆"}])

    def test_allocate_resin_fills_by_priority(self):
        tasks = [
            {"task_type": "boss", "material": "雷光棱镜", "item_id": 1, "missing": 6,
             "status": "runnable"},
            {"task_type": "domain", "phase": "talent", "material": "「正义」的哲学",
             "item_id": 2, "missing": 100, "status": "runnable"},
        ]
        # 120 体力：首领一次 40（缺口 6 个 ÷ 3.06/次 ≈ 2 趟 = 80），剩 40 给天赋本 2 趟
        plan = material_planner.allocate_resin(tasks, resin_state={"available": True, "current": 120})
        self.assertTrue(plan["available"])
        self.assertEqual(plan["by_task"][1], 80)
        self.assertEqual(plan["by_task"][2], 40)
        self.assertEqual(plan["left"], 0)

    def test_allocate_resin_uses_task_key_for_merged_domains(self):
        tasks = [
            {"task_type": "domain", "phase": "weapon_level", "material": "凛风奔狼的断牙 等 2 种",
             "item_id": 11, "task_key": "domain:1:weapon_level:塞西莉亚苗圃",
             "missing": 9, "status": "runnable",
             "materials": [{"item_name": "凛风奔狼的断牙", "missing": 9}]},
            {"task_type": "domain", "phase": "weapon_level", "material": "狮牙斗士的枷锁",
             "item_id": 11, "task_key": "domain:2:weapon_level:塞西莉亚苗圃",
             "missing": 3, "status": "runnable",
             "materials": [{"item_name": "狮牙斗士的枷锁", "missing": 3}]},
        ]
        plan = material_planner.allocate_resin(tasks, resin_state={"available": True, "current": 40})
        self.assertIn("domain:1:weapon_level:塞西莉亚苗圃", plan["by_task"])
        self.assertIn("domain:2:weapon_level:塞西莉亚苗圃", plan["by_task"])

    def test_allocate_resin_without_resin_is_honest(self):
        plan = material_planner.allocate_resin(
            [{"task_type": "boss", "item_id": 1, "missing": 1}],
            resin_state={"available": False, "reason": "读不到"})
        self.assertFalse(plan["available"])
        self.assertIsNone(plan["total"])
        self.assertIn("读不到", plan["reason"])


class FakeSettings:
    """把 `growth_db` 的 settings 换成内存字典（别碰真实数据库）。"""

    def __init__(self):
        self.data = {}
        self.get_setting = self.data.get

    def set_setting(self, key, value):
        self.data[key] = value
        return True


class ExecutionQueueTests(unittest.TestCase):
    """分批次执行：一条一条来，没路线的跳过。"""

    def setUp(self):
        self.fake = FakeSettings()
        self.patch = patch.object(execution_queue, "growth_db", self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.tasks = [
            {"task_type": "boss", "task_label": "Boss 讨伐", "material": "雷光棱镜",
             "route": "无相之雷", "item_id": 1, "missing": 5, "count": 2, "resin": 80,
             "status": "runnable", "priority": 10, "priority_label": "① 等级突破首领",
             "character_name": "丽莎", "phase_label": "角色等级"},
            {"task_type": "domain", "task_label": "秘境", "material": "「正义」的哲学",
             "route": "苍白的遗荣", "domain": "苍白的遗荣", "domain_index": "2",
             "item_id": 2, "missing": 10, "count": 2, "resin": 40, "status": "runnable",
             "priority": 50, "priority_label": "⑤ 天赋副本",
             "character_name": "丽莎", "phase_label": "天赋"},
            {"task_type": "waiting_route", "material": "查不到来源的材料", "route": "",
             "item_id": 3, "missing": 9, "status": "waiting_route", "priority": 90},
        ]

    def test_build_steps_skips_routes_that_do_not_exist(self):
        steps = execution_queue.build_steps(self.tasks)
        self.assertEqual([step["material"] for step in steps], ["雷光棱镜", "「正义」的哲学"])
        self.assertNotIn("查不到来源的材料", [step["material"] for step in steps])

    def test_build_steps_orders_by_priority(self):
        steps = execution_queue.build_steps(list(reversed(self.tasks)))
        self.assertEqual(steps[0]["material"], "雷光棱镜")        # ① 首领排在 ⑤ 天赋本前

    def test_commands_per_task_type(self):
        steps = execution_queue.build_steps(self.tasks)
        self.assertEqual(steps[0]["command"],
                         {"energy_task": {"action": "run_boss", "target": "无相之雷", "count": 2}})
        self.assertEqual(steps[1]["command"],
                         {"energy_task": {"action": "run_domain", "target": "苍白的遗荣",
                                          "count": 2, "domain_index": "2"}})

    def test_free_task_command(self):
        step = execution_queue.build_steps([
            {"task_type": "hunt", "material": "史莱姆原浆", "route": "史莱姆", "item_id": 9,
             "missing": 3, "count": 1, "status": "runnable"},
        ])[0]
        self.assertEqual(step["command"], {"free_task": [{"action": "hunt", "target": "史莱姆"}]})

    def test_state_machine_start_advance_stop(self):
        state = execution_queue.start(self.tasks)
        self.assertTrue(state["active"])
        self.assertEqual(execution_queue.current(state)["material"], "雷光棱镜")

        state, step = execution_queue.advance()
        self.assertEqual(step["material"], "「正义」的哲学")

        state, step = execution_queue.advance()
        self.assertIsNone(step)                                   # 队列跑完
        self.assertFalse(execution_queue.load()["active"])

    def test_queue_survives_a_reload(self):
        execution_queue.start(self.tasks)
        execution_queue.advance()
        reloaded = execution_queue.load()                         # 模拟重启后再读
        self.assertEqual(execution_queue.current(reloaded)["material"], "「正义」的哲学")

    def test_stop_clears_the_queue(self):
        execution_queue.start(self.tasks)
        execution_queue.skip("玩家发了别的要求")
        self.assertIsNone(execution_queue.current())

    def test_render_current_asks_for_confirmation(self):
        state = execution_queue.start(self.tasks)
        text = "\n".join(execution_queue.render_current(state))
        self.assertIn("第 1/2 条路线", text)
        self.assertIn("无相之雷", text)
        self.assertIn("y = 就跑这一条", text)

    def test_render_steps_lists_everything_and_notes_skipped(self):
        lines = execution_queue.render_steps({"execution": {"tasks": self.tasks}})
        joined = "\n".join(lines)
        self.assertIn("共 2 条路线", joined)
        self.assertIn("没有可执行路线", joined)

    def test_sync_rebuilds_when_the_routes_change(self):
        """★ 计划变了（路线不一样）→ 队列必须重建，不能拿旧的那份糊弄玩家。

        实测踩过：路线列表显示 3 条新路线，当前那条却是旧队列里的 Boss
        （还写着「读不到体力、160 体力」）。
        """
        execution_queue.start(self.tasks)                     # 旧队列：2 条
        new_tasks = [dict(self.tasks[0], material="刻晴的材料", route="别的路线")]
        state = execution_queue.sync(new_tasks)

        self.assertEqual([step["material"] for step in state["steps"]], ["刻晴的材料"])
        self.assertEqual(state["cursor"], 0)

    def test_sync_keeps_progress_but_refreshes_numbers(self):
        """路线没变 → 保留进度（游标），但把数字刷新成最新的（别显示旧数字）。"""
        execution_queue.start(self.tasks)
        execution_queue.advance()                             # 跑到第 2 条
        refreshed = [dict(task) for task in self.tasks]
        refreshed[1]["count"] = 9                             # 体力变了 → 趟数变了
        refreshed[1]["resin"] = 360

        state = execution_queue.sync(refreshed)

        self.assertEqual(state["cursor"], 1)                  # 进度没被清掉
        self.assertEqual(execution_queue.current(state)["material"], "「正义」的哲学")
        self.assertEqual(execution_queue.current(state)["count"], 9)
        self.assertEqual(execution_queue.current(state)["resin"], 360)

    def test_preview_does_not_touch_the_stored_queue(self):
        execution_queue.start(self.tasks)
        execution_queue.advance()
        preview = execution_queue.preview([dict(self.tasks[0], material="新目标", route="新路线")])

        self.assertEqual(preview["total"], 1)                 # 视图按新计划
        self.assertEqual(preview["current"]["material"], "新目标")
        stored = execution_queue.load()                       # 落库的那份没被动
        self.assertEqual(stored["cursor"], 1)

    def test_preview_lists_routes(self):
        preview = execution_queue.preview(self.tasks)
        self.assertEqual([row["material"] for row in preview["routes"]],
                         ["雷光棱镜", "「正义」的哲学"])

    def test_build_steps_puts_the_current_character_first(self):
        """同一个优先级里，**当前角色**的路线要排在别人前面（玩家反馈过）。"""
        tasks = [
            {"task_type": "gather", "material": "别人的特产", "route": "嘟嘟莲", "item_id": 1,
             "missing": 99, "count": 1, "status": "runnable", "priority": 20,
             "priority_label": "② 采集"},
            {"task_type": "hunt", "material": "当前角色缺的", "route": "肌生晶石的妖精",
             "item_id": 2, "missing": 13, "count": 1, "status": "runnable", "priority": 20,
             "priority_label": "② 采集", "owner_current": True},
        ]
        steps = execution_queue.build_steps(tasks)
        self.assertEqual(steps[0]["material"], "当前角色缺的")     # 缺口小，但它是当前角色的

    def test_mode_helpers(self):
        with patch.object(execution_queue, "mode", return_value=execution_queue.MODE_STEPWISE):
            self.assertTrue(execution_queue.is_stepwise())
        with patch.object(execution_queue, "mode", return_value=execution_queue.MODE_ALL):
            self.assertFalse(execution_queue.is_stepwise())


if __name__ == "__main__":
    unittest.main()
