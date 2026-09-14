"""① "BetterGI 到底在不在跑任务"的判定 + 等待重试；② 空计划保护。

玩家实测的两个坑（QQ 记录 09-13）：

1. 12:34:40 一条龙就跑完了，BGI 窗口还开着、空闲时也会零碎写日志；
   12:36:16 他再下一条指令（金蕨），Agent 只凭"日志最近有写入"就判成
   「⛔ 检测到 BetterGI 正在运行、且日志仍在更新（很可能正在跑任务）」直接拒绝，
   他只好把同一句话**重下一遍**。
   → 现在改成看日志里的**终态标记**（谁在后面），并且不是拒绝而是**等它跑完自动继续**。

2. 那次点名的「金蕨」根本不在他的脚本组里，结果还是冷启动了 BetterGI，
   日志里留下 `启用任务总数量: 0 / 没有配置,退出执行!`（12:36:45、12:37:09 各一次）
   —— 白开一次 BGI，还让人以为任务跑了。
   → 空计划保护：点名的目标一条路线都没命中时，跳过启动并说明原因。
"""

import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import config
from skills import bgi_controller, bgi_watcher


class BettergiTaskStateTests(unittest.TestCase):
    """`_bettergi_task_state`：看当天日志尾部"终态标记"和"启动标记"谁在后面。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "better-genshin-impact20260913.log")
        patcher = patch.object(bgi_watcher, "today_log_path", lambda: self.log)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, text):
        with open(self.log, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_finished_run_is_idle_even_though_the_log_keeps_growing(self):
        """实测原文（玩家日志）：12:34:40 一条龙结束，12:36 空闲时 BGI 仍在写零碎日志。"""
        self._write(
            "[12:24:01.100] INFO 开始执行地图追踪任务\n"
            "[12:34:40.456] INFO 一条龙和配置组任务结束\n"
            "[12:36:16.100] INFO 检测到快捷键按下\n"
        )

        self.assertEqual(bgi_controller._bettergi_task_state(), "idle")
        self.assertTrue(
            bgi_controller._bettergi_log_recently_written(),
            "日志确实是刚写过的（这正是老判定的误判来源）",
        )
        self.assertFalse(
            bgi_controller._bettergi_looks_busy(),
            "跑完了就不该再判成'在跑任务'，否则玩家要重新下一次指令",
        )

    def test_started_run_is_running(self):
        self._write(
            "[16:23:00.000] INFO 一条龙和配置组任务结束\n"
            "[16:23:46.000] INFO 任务启动！\n"
            '[16:23:47.000] INFO → "任务启动！"\n'
        )

        self.assertEqual(bgi_controller._bettergi_task_state(), "running")
        self.assertTrue(bgi_controller._bettergi_looks_busy())

    def test_single_group_done_marker_counts_as_idle(self):
        """`配置组 "X" 执行结束` 也是终态（实测：一条龙跑完/被停后都会打它）。"""
        self._write(
            "[12:24:01.100] INFO 开始执行地图追踪任务\n"
            '[12:34:40.456] INFO 配置组 "地图素材" 执行结束\n'
        )

        self.assertEqual(bgi_controller._bettergi_task_state(), "idle")

    def test_cancelled_run_is_idle(self):
        self._write(
            "[16:23:00.000] INFO 任务启动！\n[16:23:46.000] INFO 任务被取消\n"
        )

        self.assertEqual(bgi_controller._bettergi_task_state(), "idle")

    def test_empty_and_missing_log_are_unknown(self):
        self._write("")
        self.assertEqual(bgi_controller._bettergi_task_state(), "unknown")

        os.remove(self.log)
        self.assertEqual(bgi_controller._bettergi_task_state(), "unknown")

    def test_unknown_falls_back_to_log_recency(self):
        """判不出来时宁可多等：退回"日志最近有没有写入"。"""
        self._write("没有任何标记的一行\n")

        with patch.object(
            bgi_controller, "_bettergi_log_recently_written", lambda seconds=None: True
        ):
            self.assertTrue(bgi_controller._bettergi_looks_busy())
        with patch.object(
            bgi_controller, "_bettergi_log_recently_written", lambda seconds=None: False
        ):
            self.assertFalse(bgi_controller._bettergi_looks_busy())


class _FakeClock:
    """每次读时间前进固定秒数（测试里不让真的 sleep）。"""

    def __init__(self, step=5.0, start=1000.0):
        self.now = start
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


def _clear_dict_caches():
    """清掉"按 BGI 路径缓存"的字典表（见 EmptyPlanGuardTests.setUp 的说明）。"""
    from skills import char_boss_match, env_reader

    for func in (
        char_boss_match._boss_name_tables,
        char_boss_match._load_character_materials,
        char_boss_match._load_boss_material_index,
    ):
        func.cache_clear()
    env_reader._YATTA_CACHE.update({"key": None, "value": None})


class WaitForBettergiIdleTests(unittest.TestCase):
    """`_wait_for_bettergi_idle`：等它跑完再继续，而不是让玩家重新下令。"""

    def test_returns_immediately_when_not_running(self):
        with patch.object(bgi_controller, "_bettergi_pids", lambda: []), patch.object(
            bgi_controller, "send_notice"
        ) as notice:
            self.assertTrue(bgi_controller._wait_for_bettergi_idle("ou_x"))

        notice.assert_not_called()

    def test_waits_until_the_task_finishes(self):
        busy = [True, True, False]
        with patch.object(bgi_controller, "_bettergi_pids", lambda: [4321]), patch.object(
            bgi_controller, "_bettergi_looks_busy", lambda: busy.pop(0)
        ), patch.object(bgi_controller.time, "sleep", lambda _s: None), patch.object(
            bgi_controller, "send_notice"
        ) as notice:
            self.assertTrue(bgi_controller._wait_for_bettergi_idle("ou_x"))

        self.assertEqual(notice.call_count, 1, "只提醒一次，别在手机上刷屏")
        self.assertIn("等它跑完", notice.call_args[0][1])

    def test_gives_up_after_the_timeout_with_a_single_notice(self):
        clock = _FakeClock(step=5.0)
        with patch.object(bgi_controller, "_bettergi_pids", lambda: [4321]), patch.object(
            bgi_controller, "_bettergi_looks_busy", lambda: True
        ), patch.object(bgi_controller.time, "time", clock), patch.object(
            bgi_controller.time, "sleep", lambda _s: None
        ), patch.object(bgi_controller, "send_notice") as notice:
            done = bgi_controller._wait_for_bettergi_idle("ou_x", wait_seconds=30)

        self.assertFalse(done)
        self.assertEqual(notice.call_count, 1)


class EnsureBettergiClosedTests(unittest.TestCase):
    """`ensure_bettergi_closed`：跑完了/空闲 → 关掉它继续；真在跑 → 先等。"""

    def test_unknown_process_state_is_not_fatal(self):
        with patch.object(bgi_controller, "_bettergi_pids", lambda: None):
            self.assertTrue(bgi_controller.ensure_bettergi_closed("ou_x"))

    def test_idle_bettergi_is_closed_and_we_continue(self):
        with patch.object(bgi_controller, "_bettergi_pids", lambda: [99]), patch.object(
            bgi_controller, "_bettergi_looks_busy", lambda: False
        ), patch.object(
            bgi_controller, "_close_bettergi_direct", lambda: (True, "")
        ), patch.object(bgi_controller, "send_notice") as notice:
            self.assertTrue(bgi_controller.ensure_bettergi_closed("ou_x"))

        notice.assert_not_called()

    def test_busy_bettergi_is_waited_out_then_closed(self):
        with patch.object(
            bgi_controller, "_bettergi_pids", side_effect=[[99], [99]]
        ), patch.object(
            bgi_controller, "_bettergi_looks_busy", side_effect=[True, False]
        ), patch.object(
            bgi_controller, "_wait_for_bettergi_idle", lambda open_id, **kw: True
        ), patch.object(
            bgi_controller, "_close_bettergi_direct", lambda: (True, "")
        ):
            self.assertTrue(bgi_controller.ensure_bettergi_closed("ou_x"))

    def test_timeout_refuses_with_an_explanation(self):
        with patch.object(bgi_controller, "_bettergi_pids", lambda: [99]), patch.object(
            bgi_controller, "_bettergi_looks_busy", lambda: True
        ), patch.object(
            bgi_controller, "_wait_for_bettergi_idle", lambda open_id, **kw: False
        ), patch.object(bgi_controller.feishu_api, "send_feishu_msg") as sender:
            self.assertFalse(bgi_controller.ensure_bettergi_closed("ou_x"))

        self.assertIn("仍在跑任务", sender.call_args[0][1])


class RoundLockTests(unittest.TestCase):
    """一轮还没跑完时，第二条指令不该挤进来。

    为什么需要：为了"等 BetterGI 跑完"最多会占住一轮 20 分钟，等待期间玩家完全可以再发一条
    指令 —— 两个线程会同时改同一份一条龙配置、还各冷启动一次 BetterGI（互相覆盖）。
    """

    def setUp(self):
        self.addCleanup(self._release_lock)

    @staticmethod
    def _release_lock():
        if bgi_controller._ROUND_LOCK.locked():
            bgi_controller._ROUND_LOCK.release()

    def test_second_round_is_rejected_while_the_first_runs(self):
        started = threading.Event()
        release = threading.Event()

        def slow(*_args, **_kwargs):
            started.set()
            release.wait(5)

        with patch.object(
            bgi_controller, "_execute_bgi_task_locked", slow
        ), patch.object(bgi_controller, "send_notice") as notice:
            first = threading.Thread(
                target=bgi_controller.execute_bgi_task, args=({}, "y", {}, "ou_a", "1")
            )
            first.start()
            self.assertTrue(started.wait(5), "第一轮没能开始")
            try:
                bgi_controller.execute_bgi_task({}, "y", {}, "ou_b", "1")
            finally:
                release.set()
                first.join(5)

        self.assertIn("上一轮还在处理中", notice.call_args[0][1])


class EmptyPlanGuardTests(unittest.TestCase):
    """点名的目标一条路线都没命中 → 不冷启动 BetterGI。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        # ⚠️ 这些用例把 BGI 路径指向临时目录，而 Boss 名/百科表是**按这些路径缓存**的：
        #    不清缓存的话，后面 test_char_boss_match 会拿到一份"Boss 名全是胡话"的表
        #    （实测踩过：莫娜 → 「涤净青金的来源Boss」）。
        self.addCleanup(_clear_dict_caches)
        self.group_dir = os.path.join(self.root, "ScriptGroup")
        self.pathing_dir = os.path.join(self.root, "AutoPathing")
        os.makedirs(self.group_dir)
        os.makedirs(self.pathing_dir)
        self.one_dragon = os.path.join(self.root, "默认配置.json")
        with open(self.one_dragon, "w", encoding="utf-8") as handle:
            json.dump(
                {"TaskDefinitions": {}, "TaskEnabledList": {}, "TaskOrder": []}, handle
            )

        self.patches = [
            patch.object(config, "BGI_SCRIPT_GROUP_DIR", self.group_dir),
            patch.object(config, "BGI_AUTO_PATHING_DIR", self.pathing_dir),
            patch.object(
                config, "BGI_MAP_CONFIG", os.path.join(self.group_dir, "地图素材.json")
            ),
            patch.object(
                config, "BGI_ENEMY_CONFIG", os.path.join(self.group_dir, "敌人与魔物.json")
            ),
            patch.object(
                config, "BGI_MINE_CONFIG", os.path.join(self.group_dir, "矿物.json")
            ),
            patch.object(
                config, "BGI_COOK_CONFIG", os.path.join(self.group_dir, "食材与炼金.json")
            ),
            patch.object(config, "BGI_HOE_CONFIG", os.path.join(self.group_dir, "锄大地.json")),
            patch.object(config, "BGI_BACKUP_DIR", os.path.join(self.root, "backups")),
            patch.object(config, "BGI_GLOBAL_CONFIG", os.path.join(self.root, "config.json")),
            patch.object(config, "BGI_BOSS_CONFIG", os.path.join(self.root, "boss.json")),
            # 别碰真的 memory/*.json
            patch.object(bgi_controller, "load_agent_tasks", lambda: []),
            patch.object(bgi_controller, "load_agent_enabled_tasks", lambda: []),
            patch.object(bgi_controller, "save_agent_tasks", lambda *a, **kw: None),
            patch.object(
                bgi_controller, "resolve_one_dragon_config_path", lambda: self.one_dragon
            ),
            patch.object(bgi_controller, "ensure_bettergi_closed", lambda open_id: True),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, bgi_cmd):
        with patch.object(
            bgi_controller, "_launch_bettergi"
        ) as launch, patch.object(bgi_controller, "send_notice") as notice, patch.object(
            bgi_controller, "feishu_api"
        ) as feishu:
            bgi_controller.execute_bgi_task(bgi_cmd, "y", {}, "ou_x", "100000000")
        return launch, notice, feishu

    def test_named_target_without_routes_skips_the_launch(self):
        """实测：目标是「金蕨」，他的脚本组里一条含"蕨"的路线都没有。"""
        launch, notice, feishu = self._run(
            {"free_task": [{"action": "gather", "target": "金蕨"}]}
        )

        launch.assert_not_called()
        self.assertFalse(
            feishu.send_feishu_msg.called, "不应该走到异常分支（说明是别的地方炸了）"
        )
        sent = "\n".join(call[0][1] for call in notice.call_args_list)
        self.assertIn("已跳过启动", sent)

    def test_all_targets_on_cooldown_skip_the_launch(self):
        """点名的采集物都在冷却里 → 也不该白开一次 BGI。"""
        with patch.object(
            bgi_controller,
            "_filter_cooldown",
            lambda items, force=False, category=None: (
                [], ["⏳ 「霜仙花」还在冷却中（约 20.0 小时后刷新）"]
            ),
        ):
            launch, notice, _feishu = self._run(
                {"free_task": [{"action": "gather", "target": "霜仙花"}]}
            )

        launch.assert_not_called()
        sent = "\n".join(call[0][1] for call in notice.call_args_list)
        self.assertIn("已跳过启动", sent)

    def test_no_specified_target_keeps_launching(self):
        """这轮本来就没点名任何目标（玩家自己开好了一条龙）→ 保持老行为，照常启动。"""
        launch, _notice, _feishu = self._run({})

        launch.assert_called_once()

    def test_matched_route_still_launches(self):
        with open(
            os.path.join(self.group_dir, "地图素材.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump(
                {
                    "name": "地图素材",
                    "projects": [
                        {
                            "name": "01-霜仙花-彩冰镇左上-3个.json",
                            "folderName": "地方特产\\挪德卡莱\\霜仙花",
                            "type": "Pathing",
                            "status": "Disabled",
                        }
                    ],
                },
                handle,
            )
        with patch.object(
            bgi_controller,
            "_filter_cooldown",
            lambda items, force=False, category=None: (list(items), []),
        ):
            launch, _notice, _feishu = self._run(
                {"free_task": [{"action": "gather", "target": "霜仙花"}]}
            )

        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
