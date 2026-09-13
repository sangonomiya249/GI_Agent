"""计划任务助手（skills/task_scheduler.py）的测试。

它是"免 UAC 启动 / 关闭"的唯一事实来源，Studio 说明书页和 CLI 体检都读它。
"""

import os
import unittest
from unittest.mock import patch

from skills import task_scheduler


class TaskListTests(unittest.TestCase):
    def test_three_tasks_are_declared(self):
        names = [item["name"] for item in task_scheduler.TASKS]

        self.assertEqual(names, ["StartBetterGI", "StopBetterGI", "StopGenshin"])

    def test_stop_genshin_is_optional_the_others_are_required(self):
        by_name = {item["name"]: item for item in task_scheduler.TASKS}

        self.assertTrue(by_name["StartBetterGI"]["required"])
        self.assertTrue(by_name["StopBetterGI"]["required"])
        self.assertFalse(by_name["StopGenshin"]["required"])
        self.assertIn("原神", by_name["StopGenshin"]["purpose"])

    def test_setup_hint_is_an_absolute_path_that_exists(self):
        """实测踩过：管理员 PowerShell 默认在 C:\\WINDOWS\\system32，相对路径会"参数不存在"。"""
        hint = task_scheduler.setup_hint()

        self.assertIn(":\\", hint)
        path = hint.split('"')[1]
        self.assertTrue(os.path.isfile(path), f"提示里的脚本不存在：{path}")

    def test_each_task_points_at_its_script(self):
        repo = task_scheduler.repo_root()
        for item in task_scheduler.TASKS:
            with self.subTest(task=item["name"]):
                self.assertTrue(os.path.isfile(os.path.join(repo, item["script"])))


class StateProbeTests(unittest.TestCase):
    def test_registered_task_is_true(self):
        with patch.object(task_scheduler, "_schtasks_query", return_value=True):
            self.assertIs(task_scheduler.task_state("StopGenshin"), True)

    def test_schtasks_unknown_falls_back_to_powershell(self):
        with patch.object(task_scheduler, "_schtasks_query", return_value=None), patch.object(
            task_scheduler, "_powershell_task_probe", return_value=False
        ):
            self.assertIs(task_scheduler.task_state("StopGenshin"), False)

    def test_states_covers_every_task(self):
        with patch.object(task_scheduler, "_schtasks_query", return_value=True):
            states = task_scheduler.states()

        self.assertEqual(set(states), set(task_scheduler.TASK_NAMES))
        self.assertEqual(task_scheduler.missing_tasks(), [])

    def test_missing_tasks_reports_only_confirmed_absent(self):
        results = {"StartBetterGI": True, "StopBetterGI": False, "StopGenshin": None}
        with patch.object(task_scheduler, "task_state", side_effect=lambda name: results[name]):
            self.assertEqual(task_scheduler.missing_tasks(), ["StopBetterGI"])

    def test_describe_states_uses_icons(self):
        results = {"StartBetterGI": True, "StopBetterGI": False, "StopGenshin": None}
        with patch.object(task_scheduler, "task_state", side_effect=lambda name: results[name]):
            text = task_scheduler.describe_states()

        self.assertIn("✅StartBetterGI", text)
        self.assertIn("❌StopBetterGI", text)
        self.assertIn("❔StopGenshin", text)

    def test_non_windows_is_unknown(self):
        with patch.object(os, "name", "posix"):
            self.assertIsNone(task_scheduler.task_state("StopGenshin"))


if __name__ == "__main__":
    unittest.main()
