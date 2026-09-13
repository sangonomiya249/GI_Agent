"""完成监视器（skills/bgi_watcher.py）的回归测试。

玩家实测的崩溃：`start_completion_watch` 的监视线程里，跨天换日志文件那句
`tail = _LogTail(...)` 让 `tail` 变成闭包的**局部变量**，下一句读 `tail.path` 直接
`UnboundLocalError: local variable 'tail' referenced before assignment` ——
线程当场死掉，完成报告永远发不出来（而且只在"跨天换文件"时触发，平时看不出来）。
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import config
from skills import bgi_watcher


class RefreshTailTests(unittest.TestCase):
    """`_refresh_tail`：盯"今天的日志文件"，跨天就换。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        # 文件名只是"两天不同的日志文件"，用固定假日期，避免和真实当天日期混淆
        self.day1 = os.path.join(self.root, "better-genshin-impact-假日期A.log")
        self.day2 = os.path.join(self.root, "better-genshin-impact-假日期B.log")
        with open(self.day1, "w", encoding="utf-8") as handle:
            handle.write("昨天的内容\n")

    def test_first_call_creates_a_tail(self):
        holder = [None]

        tail, switched = bgi_watcher._refresh_tail(holder, make_path=lambda: self.day1)

        self.assertTrue(switched)
        self.assertEqual(tail.path, self.day1)
        self.assertIs(holder[0], tail)

    def test_same_path_does_not_switch(self):
        holder = [None]
        first, _ = bgi_watcher._refresh_tail(holder, make_path=lambda: self.day1)

        same, switched = bgi_watcher._refresh_tail(holder, make_path=lambda: self.day1)

        self.assertFalse(switched)
        self.assertIs(same, first)

    def test_path_change_switches_and_resets_offset(self):
        holder = [None]
        bgi_watcher._refresh_tail(holder, make_path=lambda: self.day1)

        with open(self.day2, "w", encoding="utf-8") as handle:
            handle.write("新一天的内容\n")
        tail, switched = bgi_watcher._refresh_tail(holder, make_path=lambda: self.day2)

        self.assertTrue(switched)
        self.assertEqual(tail.path, self.day2)
        self.assertEqual(tail.offset, 0, "新文件要从头读，否则会漏掉开头的结束标记")

    def test_missing_file_is_tolerated(self):
        holder = [None]
        absent = os.path.join(self.root, "not-there.log")

        tail, switched = bgi_watcher._refresh_tail(holder, make_path=lambda: absent)

        self.assertTrue(switched)
        self.assertEqual(tail.offset, 0)
        self.assertEqual(tail.read_new(), "")


class ParseTerminalTests(unittest.TestCase):
    def test_cancelled_run_is_flagged(self):
        """实测原文（玩家自己的日志，17:43:02 / 17:43:03）：
        被取消的那次**照样**打了「配置组 X 执行结束」，一秒后才说"任务被取消"。"""
        text = (
            '→ 脚本执行结束: "批量讨伐角色养成材料BOSS", 耗时: 1分0.949秒\n'
            '配置组 "批量讨伐角色养成材料BOSS" 执行结束\n'
            "任务被取消，退出执行\n"
        )

        info = bgi_watcher.parse_terminal(text)

        self.assertTrue(info["cancelled"])
        self.assertTrue(info["finished"], "旧行为仍然认为'结束了'—— 所以调用方必须先看 cancelled")

    def test_stop_hotkey_is_flagged(self):
        info = bgi_watcher.parse_terminal(
            '检测到您配置的停止快捷键"Up"按下，停止当前执行任务\n'
        )

        self.assertTrue(info["cancelled"])

    def test_cancel_token_line_is_not_a_cancel_signal(self):
        """「已获取取消令牌」是 BGI 正常起任务时就打的，不能当取消。"""
        info = bgi_watcher.parse_terminal("已获取取消令牌\n配置组 \"地图素材\" 执行结束\n")

        self.assertFalse(info["cancelled"])
        self.assertTrue(info["finished"])

    def test_clean_run_is_not_cancelled(self):
        info = bgi_watcher.parse_terminal("一条龙和配置组任务结束\n")

        self.assertFalse(info["cancelled"])
        self.assertTrue(info["flow_done"])


class ScriptFailureTests(unittest.TestCase):
    """脚本自己跳过的失败（讨伐失败/领奖失败）必须出现在报告里。

    玩家实测（2026-09-12 21:07，Boss 脚本）：日志里
        `已进入征讨之花领奖界面 → 领取失败，可能是原粹树脂不足 → ❌讨伐『恒常机关阵列』失败`
    整条流程照样打「配置组执行结束」，旧报告只有一句 🎉 完成 —— 白跑一趟完全看不出来。
    """

    LOG = (
        '已进入征讨之花领奖界面\n'
        '领取失败，可能是原粹树脂不足，尝试关闭领取界面\n'
        '领取奖励失败：未成功回到主界面\n'
        '❌讨伐『恒常机关阵列』失败: Error: 未成功回到主界面\n'
        '💀战斗失败，跳过当前BOSS 无相之岩\n'
        '配置组 "批量讨伐角色养成材料BOSS" 执行结束\n'
    )

    def test_failures_are_extracted(self):
        found = bgi_watcher.script_failures(self.LOG)

        self.assertTrue(any("恒常机关阵列" in item for item in found), found)
        self.assertTrue(any("未成功回到主界面" in item for item in found), found)
        self.assertTrue(any("无相之岩" in item for item in found), found)

    def test_duplicates_are_collapsed(self):
        found = bgi_watcher.script_failures(self.LOG)

        self.assertEqual(len(found), len(set(found)))

    def test_report_and_title_mention_the_failures(self):
        info = bgi_watcher.parse_terminal(self.LOG)
        lines = bgi_watcher.build_report(self.LOG, {}, 60)

        self.assertEqual(len(info["script_failures"]), 3)
        joined = "\n".join(lines)
        self.assertIn("脚本内报错", joined)
        self.assertIn("恒常机关阵列", joined)
        self.assertIn("白扣了体力", joined)

    def test_clean_log_has_no_failures(self):
        clean = '配置组 "地图素材" 执行结束\n已回到主界面，领取奖励结束\n'

        self.assertEqual(bgi_watcher.script_failures(clean), [])
        self.assertNotIn("脚本内报错", "\n".join(bgi_watcher.build_report(clean, {}, 10)))


class AllGroupsDoneTests(unittest.TestCase):
    def test_no_expectation_falls_back_to_old_behaviour(self):
        info = {"groups": ["地图素材"]}

        self.assertTrue(bgi_watcher.all_groups_done(info, []))

    def test_first_of_two_groups_is_not_the_end(self):
        expected = [frozenset({"地图素材"}), frozenset({"敌人与魔物"})]

        self.assertFalse(bgi_watcher.all_groups_done({"groups": ["地图素材"]}, expected))
        self.assertTrue(
            bgi_watcher.all_groups_done({"groups": ["地图素材", "敌人与魔物"]}, expected)
        )

    def test_group_file_may_use_another_internal_name(self):
        """一条龙任务叫「狗粮AAA」，但组名（日志里打的）可能是文件里的 name。"""
        expected = [frozenset({"狗粮AAA", "狗粮"})]

        self.assertTrue(bgi_watcher.all_groups_done({"groups": ["狗粮"]}, expected))


class ExpectedGroupNamesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.group_dir = os.path.join(self.root, "ScriptGroup")
        os.makedirs(self.group_dir)
        with open(os.path.join(self.group_dir, "狗粮AAA.json"), "w", encoding="utf-8") as handle:
            json.dump({"name": "狗粮"}, handle, ensure_ascii=False)
        with open(os.path.join(self.group_dir, "地图素材.json"), "w", encoding="utf-8") as handle:
            json.dump({"name": "地图素材"}, handle, ensure_ascii=False)
        self.one_dragon = os.path.join(self.root, "OneDragon", "地图素材.json")
        os.makedirs(os.path.dirname(self.one_dragon))
        with open(self.one_dragon, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "TaskDefinitions": {
                        "uuid-map": "地图素材",
                        "uuid-dog": "狗粮AAA",
                        "uuid-off": "矿物",
                        "uuid-mail": "领取邮件",
                    },
                    "TaskEnabledList": {
                        "uuid-map": True,
                        "uuid-dog": True,
                        "uuid-off": False,
                        "uuid-mail": True,
                    },
                },
                handle,
                ensure_ascii=False,
            )

        from skills import bgi_controller

        patches = [
            patch.object(config, "BGI_SCRIPT_GROUP_DIR", self.group_dir),
            patch.object(
                bgi_controller, "resolve_one_dragon_config_path", return_value=self.one_dragon
            ),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_only_enabled_tasks_are_expected_and_aliases_are_read(self):
        groups = bgi_watcher.expected_group_names()

        self.assertEqual(len(groups), 2)                      # 关掉的「矿物」不算
        self.assertIn(frozenset({"地图素材"}), groups)
        self.assertIn(frozenset({"狗粮AAA", "狗粮"}), groups)

    def test_builtin_tasks_are_not_counted(self):
        """BetterGI 内置任务（领取邮件/合成树脂…）不打「配置组」行，算进来就永远等不到"全跑完"。"""
        names = {alias for group in bgi_watcher.expected_group_names() for alias in group}

        self.assertNotIn("领取邮件", names)

    def test_unreadable_config_falls_back_to_empty(self):
        with patch("builtins.open", side_effect=OSError("no file")):
            self.assertEqual(bgi_watcher.expected_group_names(), [])


class WatchWorkerTests(unittest.TestCase):
    """真跑一遍监视线程：结束标记出现时必须发出报告，且不能崩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = self.tmp.name
        # ⚠️ 别把日志文件名写死成具体日期：`today_log_path()` 是按**当天日期**拼的，
        #    写死 `...20260912.log` 的测试只要跨过午夜就必然失败（实测凌晨踩到：
        #    监视线程去找 0913 的文件，而测试写的是 0912，于是一条消息都发不出来）。
        self.log_path = os.path.join(self.log_dir, os.path.basename(bgi_watcher.today_log_path()))
        with open(self.log_path, "w", encoding="utf-8") as handle:
            handle.write("历史日志：一条龙和配置组任务结束\n")     # 触发前就有的旧标记

        self.sent = []
        self._patches = [
            patch.object(config, "BGI_LOG_DIR", self.log_dir),
            patch.object(config, "BGI_TASK_PROGRESS_DIR", os.path.join(self.log_dir, "progress")),
            patch.object(bgi_watcher, "POLL_INTERVAL_SECONDS", 0.02),
            patch.object(bgi_watcher, "START_GRACE_SECONDS", 999),
            patch.object(bgi_watcher, "_FINAL_CONFIRM_SECONDS", 0.02),
            # 🌟 这两条是**测试隔离**用的，别删：
            #    · expected_group_names() 会去读"当前生效的一条龙配置" —— 开发机上那儿真有 2 个组，
            #      于是"只看到 1 个组结束"就永远不算跑完，测试静默等 5 秒后失败；
            #    · foreground_is_game() 会探测**真实桌面**前台窗口 —— 开发时前台是浏览器/编辑器，
            #      于是先冒出一条「原神不在前台，BGI 正在暂停」，把断言打乱。
            #    这两个功能的专项测试在 WorkerFocusTests / AllGroupsDoneTests 里，各自打桩。
            patch.object(bgi_watcher, "expected_group_names", lambda: []),
            patch.object(bgi_watcher, "foreground_is_game", lambda: True),
            patch.object(bgi_watcher.feishu_api, "send_feishu_msg",
                         lambda target, text: self.sent.append(text) or True),
        ]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        """收尾：把本用例启的监视线程清干净。

        ⚠️ 踩过的坑：用例结束后那个线程还活着，补丁一撤，它下一轮 `_refresh_tail` 就会去读
        **真实的** BetterGI 日志目录 —— 真实日志里有「一条龙和配置组任务结束」，
        于是它把 🎉 报告投进了**下一个用例**的 sent 列表里，造成随机失败（实测踩到两个）。
        """
        with bgi_watcher._watch_lock:
            bgi_watcher._active_generation += 1          # 让残留线程下一轮直接返回
        for thread in threading.enumerate():
            if thread.name == "bgi-completion-watch":
                thread.join(timeout=2)

    def test_worker_reports_cancellation_not_completion(self):
        """玩家按停止快捷键那次：日志里有「配置组 X 执行结束」也有「任务被取消」→ 必须报⛔。"""
        bgi_watcher.start_completion_watch("ou_me", timeout_seconds=30)
        time.sleep(0.1)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write('配置组 "批量讨伐角色养成材料BOSS" 执行结束\n')
            handle.write("任务被取消，退出执行\n")

        self._wait_for_report(match="⛔")

        self.assertFalse([text for text in self.sent if "🎉" in text])

    def test_first_group_of_two_does_not_end_the_watch(self):
        expected = [frozenset({"地图素材"}), frozenset({"敌人与魔物"})]
        with patch.object(bgi_watcher, "expected_group_names", lambda: expected):
            bgi_watcher.start_completion_watch("ou_me", timeout_seconds=30)
            time.sleep(0.1)
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write('配置组 "地图素材" 执行结束\n')

            time.sleep(0.5)                              # 已经轮询过好几遍
            self.assertFalse(
                [text for text in self.sent if "🎉" in text],
                "第一个组跑完就报整轮完成了（另一个组还在跑）",
            )

            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write('配置组 "敌人与魔物" 执行结束\n')
            self._wait_for_report(match="🎉")

        self.assertIn("敌人与魔物", self.sent[-1])

    def test_late_cancel_line_is_not_reported_as_completion(self):
        """竞态复现：先看到「配置组 X 执行结束」，稍后才有「任务被取消」。

        轮询刚好卡在这两行之间时，旧代码当场报 🎉 完成 —— 所以判定终态前会再读一次日志。
        这里把"再看一眼"的窗口放到 0.3 秒，晚到的取消行 0.05 秒后写入，模拟实测的 1 秒差。
        """
        with patch.object(bgi_watcher, "_FINAL_CONFIRM_SECONDS", 0.3):
            bgi_watcher.start_completion_watch("ou_me", timeout_seconds=30)
            time.sleep(0.1)
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write('配置组 "地图素材" 执行结束\n')
            time.sleep(0.05)                 # 晚到的"其实被取消了"这行
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write("任务被取消，退出执行\n")

            self._wait_for_report(match="⛔")

        self.assertFalse([text for text in self.sent if "🎉" in text])

    def test_worker_warns_when_a_script_skipped_a_target(self):
        """有脚本内报错时标题要是 ⚠️ 而不是 🎉（别让玩家以为一切正常）。"""
        bgi_watcher.start_completion_watch("ou_me", timeout_seconds=30)
        time.sleep(0.1)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write("❌讨伐『恒常机关阵列』失败: Error: 未成功回到主界面\n")
            handle.write('配置组 "批量讨伐角色养成材料BOSS" 执行结束\n')

        self._wait_for_report(match="⚠️")

        self.assertIn("恒常机关阵列", self.sent[-1])

    def _wait_for_report(self, match=None, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.sent and (match is None or match in self.sent[-1]):
                return
            time.sleep(0.05)
        self.fail(f"监视线程没发出预期的报告（match={match!r}，已发={self.sent}）")

    def test_worker_reports_and_does_not_crash(self):
        before = threading.active_count()
        bgi_watcher.start_completion_watch("ou_me", timeout_seconds=30)

        # 触发之后才写入"本轮的结束标记"（监视器只认触发之后的新增内容）
        time.sleep(0.1)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write("2026-09-12 22:00:00 | INFO | 一条龙和配置组任务结束\n")

        # ⚠️ 要等的是**完成报告**，不能等"随便一条消息"：⓪ 的
        #    「✅ 已确认 BetterGI 开始执行任务。」会先落地，等它会把断言卡在中间态上。
        self._wait_for_report(match="🎉")

        self.assertIn("一条龙", self.sent[-1])
        # 线程应当已经正常收尾（不是带着异常死掉）
        for thread in threading.enumerate()[before:]:
            if thread.name == "bgi-completion-watch":
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive(), "监视线程没有正常结束")

    def test_cross_midnight_switch_does_not_crash(self):
        """把"今天的日志"换成另一个文件，模拟跨天 —— 这正是崩溃的触发条件。"""
        second = os.path.join(self.log_dir, "better-genshin-impact-第二天.log")
        with open(second, "w", encoding="utf-8") as handle:
            handle.write("")

        state = {"file": self.log_path}

        def fake_today():
            return state["file"]

        with patch.object(bgi_watcher, "today_log_path", fake_today):
            bgi_watcher.start_completion_watch("ou_me", timeout_seconds=30)
            time.sleep(0.1)
            state["file"] = second                     # 跨天：换文件
            with open(second, "a", encoding="utf-8") as handle:
                handle.write("2026-09-13 00:05:00 | INFO | 一条龙和配置组任务结束\n")

            self._wait_for_report(match="🎉")

        self.assertIn("一条龙", self.sent[-1])


if __name__ == "__main__":
    unittest.main()
