"""控制台的冒烟测试：能建出窗口、切页、状态灯和日志分级都对。

没有图形环境（无显示/无 tkinter）时自动跳过，不会让整套测试挂掉。
这里**不**启动真的 Agent 子进程（那会去调大模型），进程桥接由人工验证。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import tkinter

    TK_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - 取决于环境
    tkinter = None
    TK_IMPORT_ERROR = exc


class ConsoleAppTests(unittest.TestCase):
    def setUp(self):
        if tkinter is None:
            self.skipTest(f"没有 tkinter：{TK_IMPORT_ERROR}")
        try:
            import gui
        except Exception as exc:  # pragma: no cover - 取决于环境
            self.skipTest(f"gui 模块导入失败：{exc}")
        self.gui = gui
        self.temp_dir = tempfile.TemporaryDirectory()
        try:
            with patch.object(gui, "STATE_PATH", str(Path(self.temp_dir.name) / ".gui_state.json")):
                self.app = gui.ConsoleApp()
        except tkinter.TclError as exc:  # pragma: no cover - 没有显示时
            self.temp_dir.cleanup()
            self.skipTest(f"没有可用的图形环境：{exc}")
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_all_tabs_exist(self):
        tabs = [self.app.notebook.tab(index, "text") for index in range(self.app.notebook.index("end"))]

        self.assertEqual(tabs, ["运行", "配置 (.env)", "维护工具", "帮助"])

    def test_config_groups_are_listed_and_switchable(self):
        from skills import env_config

        self.assertEqual(self.app.group_list.size(), len(env_config.FIELD_GROUPS))
        for index, group in enumerate(env_config.FIELD_GROUPS):
            self.app.group_list.selection_clear(0, "end")
            self.app.group_list.selection_set(index)
            self.app._show_group()
            self.app.update()
            self.assertEqual(set(self.app._config_vars), {field.key for field in group.fields})

    def test_editing_a_group_then_saving_writes_env(self):
        from skills import env_config

        temp_env = Path(self.temp_dir.name) / ".env"
        temp_env.write_text("LLM_PROVIDER=openai\nMODEL_NAME=old-model\n", encoding="utf-8")

        # ⚠️ 表单的值来自 `_env_values`（窗口初始化时从**真实** .env 读的），不是每次重读 ENV_PATH。
        #    只 patch ENV_PATH 的话，这个用例会依赖开发机自己的 .env（真实值是 deepseek 时就失败）。
        #    而且 `_show_group()` 开头会"收集上一页的输入框"——那一收集就把真实值盖回来了，
        #    所以先把可见变量清空（等价于"刚切到这一页、还没有旧改动"）。
        with patch.object(self.gui, "ENV_PATH", str(temp_env)), patch.object(
            self.app, "_env_values", env_config.load_env(str(temp_env))
        ), patch.object(self.gui.messagebox, "showinfo"):
            self.app._config_vars.clear()
            self.app.group_list.selection_clear(0, "end")
            self.app.group_list.selection_set(0)
            self.app._show_group()
            self.app._config_vars["MODEL_NAME"].set("new-model")
            self.app._save_config_form()

        saved = env_config.load_env(str(temp_env))
        self.assertEqual(saved["MODEL_NAME"], "new-model")
        self.assertEqual(saved["LLM_PROVIDER"], "openai")
        backups = [name for name in os.listdir(self.temp_dir.name) if ".env.bak-" in name]
        self.assertEqual(len(backups), 1)

    def test_status_lamp_and_banner_follow_the_state(self):
        self.app._set_state("waiting")
        self.app.update()
        self.assertIn("等你确认", self.app.status_label.cget("text"))
        self.assertIn("批准", self.app.banner.cget("text"))

        self.app._set_state("running")
        self.app.update()
        self.assertIn("运行中", self.app.status_label.cget("text"))
        self.assertEqual(self.app.banner.cget("text"), "")

    def test_log_levels_are_classified(self):
        classify = self.gui.ConsoleApp._classify

        self.assertEqual(classify("❌ 发生错误: boom"), "error")
        self.assertEqual(classify("⚠️ 战斗策略文件不存在"), "warn")
        self.assertEqual(classify("🤖 Agent: 方案如下"), "agent")
        self.assertEqual(classify("🛑 [系统拦截] 请确认是否执行上述计划？"), "prompt")
        self.assertEqual(classify("普通一行日志"), "info")

    def test_approval_prompt_switches_the_lamp(self):
        self.app._set_state("running")
        self.app._append_log("🛑 [系统拦截] 请确认是否执行上述计划？\n")

        self.assertEqual(self.app._state, "waiting")

    def test_send_input_without_a_process_does_not_crash(self):
        with patch.object(self.gui.messagebox, "showinfo") as info:
            self.app.proc = None
            self.app._send_input("y")

        info.assert_called_once()


class HelperTests(unittest.TestCase):
    def test_child_command_points_at_main_py_in_source_mode(self):
        import gui

        command = gui.child_command()

        self.assertIn("-u", command)
        self.assertTrue(command[-1].endswith("main.py"))

    def test_child_command_uses_run_cli_when_frozen(self):
        import gui

        with patch.object(gui.sys, "frozen", True, create=True):
            command = gui.child_command()

        self.assertEqual(command[1:], ["--run-cli"])

    def test_help_text_mentions_the_essentials(self):
        import gui

        for keyword in ("y", "t", "refresh", "rollback", "build_exe.ps1"):
            self.assertIn(keyword, gui.HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
