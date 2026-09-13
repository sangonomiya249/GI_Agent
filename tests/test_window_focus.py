"""原神窗口前后台兜底（skills/window_focus.py + 哨兵里的处理）的测试。

实测背景（玩家 2026-09-12 的 BGI 日志）：一条龙跑起来后，前台窗口是 QQ / GI-Agent-Studio /
SearchHost 时，BGI 每秒打一行「当前获取焦点的窗口为: X，不是原神，暂停」——
73 次，从 18:26 一直暂停到 21:05，看起来就是卡死。
"""

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import config
from skills import bgi_watcher, window_focus


class WindowProbeTests(unittest.TestCase):
    """探测函数必须在拿不到窗口时安静降级（受限环境不能炸）。"""

    def test_other_unity_games_are_not_mistaken_for_genshin(self):
        """实测：家里同时开着《明日方舟：终末地》(Endfield)，类名也是 UnityWndClass。

        只看类名的话会把别的 Unity 游戏当成原神 —— 既不切前台，也把 BGI 的暂停当正常。
        """
        self.assertTrue(window_focus._is_game_window("UnityWndClass", "原神"))
        self.assertTrue(window_focus._is_game_window("UnityWndClass", "Genshin Impact"))
        self.assertTrue(window_focus._is_game_window("UnityWndClass", "云·原神"))
        self.assertFalse(window_focus._is_game_window("UnityWndClass", "Endfield"))
        self.assertFalse(window_focus._is_game_window("UnityWndClass", ""))
        self.assertFalse(window_focus._is_game_window("Chrome_WidgetWin_1", "原神"))

    def test_non_windows_returns_none(self):
        with patch.object(os, "name", "posix"):
            self.assertIsNone(window_focus.find_game_window())
            self.assertIsNone(window_focus.foreground_state())
            self.assertIsNone(window_focus.foreground_is_game())
            self.assertFalse(window_focus.focus_game_window())

    def test_missing_game_window_returns_none(self):
        with patch.object(window_focus, "_user32", return_value=None):
            self.assertIsNone(window_focus.find_game_window())

    def test_game_process_name_comes_from_bgi_config(self):
        with patch("builtins.open", side_effect=OSError("no file")):
            self.assertEqual(window_focus.game_process_name(), "YuanShen")

    def test_describe_foreground_is_honest_when_unknown(self):
        with patch.object(os, "name", "posix"):
            self.assertIn("无法确认", window_focus.describe_foreground())


class FocusLostHandlingTests(unittest.TestCase):
    """哨兵发现"前台不是原神"时的行为。"""

    def setUp(self):
        self.sent = []
        patcher = patch.object(
            bgi_watcher, "_announce",
            lambda title, body, open_id: self.sent.append(title + "\n" + "\n".join(body)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("BGI_FOCUS_GUARD", False), ("BGI_FOCUS_GUARD_STRIKES", 2),
                            ("BGI_FOCUS_GUARD_MAX_TRIES", 3)):
            item = patch.object(config, name, value)
            item.start()
            self.addCleanup(item.stop)
        title_patch = patch.object(bgi_watcher, "describe_foreground",
                                   lambda: "当前前台窗口：QQ（不是原神）")
        title_patch.start()
        self.addCleanup(title_patch.stop)

    def test_first_strike_is_quiet(self):
        tries, notified = bgi_watcher._handle_focus_lost("ou_me", 1, 0, 15, False)

        self.assertEqual((tries, notified), (0, False))
        self.assertEqual(self.sent, [])

    def test_notice_is_sent_once_after_threshold(self):
        tries, notified = bgi_watcher._handle_focus_lost("ou_me", 2, 0, 30, False)

        self.assertTrue(notified)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("原神不在前台", self.sent[0])
        self.assertIn("QQ", self.sent[0])
        self.assertIn("失去焦点时自动切回原神", self.sent[0])

        # 后续巡检不再重复刷屏
        _tries, again = bgi_watcher._handle_focus_lost("ou_me", 3, tries, 45, notified)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(again)

    def test_no_focus_stealing_when_guard_is_off(self):
        """默认不抢焦点（玩家可能正在用电脑）—— 只提示，不动他的窗口。"""
        with patch.object(bgi_watcher, "focus_game_window") as focus:
            bgi_watcher._handle_focus_lost("ou_me", 5, 0, 75, False)

        focus.assert_not_called()

    def test_guard_restores_focus_when_enabled(self):
        with patch.object(config, "BGI_FOCUS_GUARD", True), patch.object(
            bgi_watcher, "focus_game_window", return_value=True
        ) as focus:
            tries, _notified = bgi_watcher._handle_focus_lost("ou_me", 2, 0, 30, True)

        focus.assert_called_once()
        self.assertEqual(tries, 1)

    def test_guard_gives_up_after_the_limit(self):
        with patch.object(config, "BGI_FOCUS_GUARD", True), patch.object(
            bgi_watcher, "focus_game_window", return_value=False
        ) as focus:
            tries, _notified = bgi_watcher._handle_focus_lost("ou_me", 2, 2, 30, True)

        self.assertEqual(focus.call_count, 1)
        self.assertEqual(tries, 3)
        self.assertTrue(any("停止自动切换" in text for text in self.sent))

    def test_guard_stops_trying_after_limit(self):
        with patch.object(config, "BGI_FOCUS_GUARD", True), patch.object(
            bgi_watcher, "focus_game_window", return_value=True
        ) as focus:
            bgi_watcher._handle_focus_lost("ou_me", 2, 3, 30, True)

        focus.assert_not_called()


class WorkerFocusTests(unittest.TestCase):
    """端到端：监视线程发现前台不是原神时要提示，回到前台后继续。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = os.path.join(self.tmp.name, "better-genshin-impact20260912.log")
        open(self.log_path, "w", encoding="utf-8").close()

        self.sent = []
        self.foreground = False
        patches = [
            patch.object(config, "BGI_LOG_DIR", self.tmp.name),
            patch.object(config, "BGI_TASK_PROGRESS_DIR", os.path.join(self.tmp.name, "progress")),
            patch.object(bgi_watcher, "POLL_INTERVAL_SECONDS", 0.02),
            patch.object(bgi_watcher, "START_GRACE_SECONDS", 999),
            patch.object(bgi_watcher, "_FINAL_CONFIRM_SECONDS", 0.02),
            patch.object(config, "BGI_FOCUS_GUARD_STRIKES", 2),
            patch.object(bgi_watcher, "foreground_is_game", lambda: self.foreground),
            patch.object(bgi_watcher, "describe_foreground", lambda: "当前前台窗口：QQ（不是原神）"),
            patch.object(
                bgi_watcher.feishu_api, "send_feishu_msg",
                lambda target, text: self.sent.append(text) or True,
            ),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        with bgi_watcher._watch_lock:
            bgi_watcher._active_generation += 1
        for thread in threading.enumerate():
            if thread.name == "bgi-completion-watch":
                thread.join(timeout=2)

    def test_paused_on_lost_focus_is_reported(self):
        bgi_watcher.start_completion_watch("ou_me", timeout_seconds=30)

        deadline = time.time() + 5
        while time.time() < deadline and not any("原神不在前台" in text for text in self.sent):
            time.sleep(0.05)

        self.assertTrue(
            any("原神不在前台" in text for text in self.sent),
            f"没提示前台丢失：{self.sent}",
        )


if __name__ == "__main__":
    unittest.main()
