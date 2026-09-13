"""「使用说明」页的数据测试：上手清单的三态判断 + 该添加哪些调度器。

全部用临时目录与假的探测函数，不碰真机。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from studio import guide, server


def _group(name, strategy="根据队伍自动选择"):
    return {
        "name": name,
        "index": 1,
        "config": {"pathingConfig": {"autoFightConfig": {"strategyName": strategy}}},
        "projects": [{"name": "a.json", "folderName": "x", "type": "Pathing"}],
    }


class ChecklistTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bgi_dir = self.root / "BetterGI"
        (self.bgi_dir / "User" / "ScriptGroup").mkdir(parents=True)
        (self.bgi_dir / "User" / "AutoPathing" / "锄地专区" / "小怪2000@mno" / "1_2_璃月").mkdir(parents=True)
        (self.bgi_dir / "BetterGI.exe").write_text("", encoding="utf-8")
        (self.bgi_dir / "User" / "AutoFight").mkdir(parents=True)
        (self.bgi_dir / "User" / "AutoFight" / "根据队伍自动选择.txt").write_text("", encoding="utf-8")

        # 关键脚本组都建好 + 策略合法
        for name in ("地图素材", "敌人与魔物", "锄大地", "矿物", "食材与炼金",
                     "批量讨伐角色养成材料BOSS", "狗粮AAA"):
            (self.bgi_dir / "User" / "ScriptGroup" / f"{name}.json").write_text(
                json.dumps(_group(name), ensure_ascii=False), encoding="utf-8"
            )
        self.one_dragon = self.bgi_dir / "User" / "OneDragon" / "地图素材.json"
        self.one_dragon.parent.mkdir(parents=True)
        self.one_dragon.write_text(
            json.dumps(
                {
                    "TaskDefinitions": {f"uuid-{i}": name for i, name in enumerate(
                        ("地图素材", "敌人与魔物", "锄大地", "矿物", "食材与炼金",
                         "批量讨伐角色养成材料BOSS", "狗粮AAA"))},
                    "TaskEnabledList": {},
                    "TaskOrder": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.env_file = self.root / ".env"
        self.env_file.write_text(
            "LLM_PROVIDER=openai\nMODEL_NAME=deepseek-chat\nOPENAI_API_KEY=sk-x\nDEFAULT_UID=100000000\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _patches(self, tasks_exist=True):
        from skills import bgi_controller

        return [
            patch.object(config, "BGI_DIR", str(self.bgi_dir)),
            patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.bgi_dir / "User" / "ScriptGroup")),
            patch.object(config, "BGI_AUTO_PATHING_DIR", str(self.bgi_dir / "User" / "AutoPathing")),
            patch.object(config, "BGI_AUTO_FIGHT_DIR", str(self.bgi_dir / "User" / "AutoFight")),
            patch.object(bgi_controller, "resolve_one_dragon_config_path", return_value=str(self.one_dragon)),
            patch.object(guide, "scheduled_task_state", return_value=tasks_exist),
        ]

    def _guide(self, tasks_exist=True):
        with self._exit_stack(self._patches(tasks_exist)):
            return guide.build_guide(env_path=str(self.env_file))

    class _exit_stack:
        """把一堆 patch 当 with 用（unittest 的 ExitStack 也行，这里图省事）。"""

        def __init__(self, patchers):
            self.patchers = patchers

        def __enter__(self):
            for patcher in self.patchers:
                patcher.start()
            return self

        def __exit__(self, *exc):
            for patcher in reversed(self.patchers):
                patcher.stop()
            return False

    def test_everything_ready_reports_all_ok(self):
        data = self._guide()

        statuses = {step["id"]: step["status"] for step in data["steps"]}
        self.assertEqual(statuses["bettergi"], "ok")
        self.assertEqual(statuses["llm"], "ok")
        self.assertEqual(statuses["uid"], "ok")
        self.assertEqual(statuses["one_dragon"], "ok")
        self.assertEqual(statuses["script_groups"], "ok")
        self.assertEqual(statuses["strategies"], "ok")
        self.assertEqual(statuses["pathing"], "ok")
        self.assertEqual(statuses["scheduled"], "ok")
        self.assertEqual(data["summary"]["todo"], 0)
        self.assertEqual(data["summary"]["warn"], 0)

    def test_missing_scheduled_tasks_are_a_todo_with_fix(self):
        data = self._guide(tasks_exist=False)

        step = [row for row in data["steps"] if row["id"] == "scheduled"][0]
        self.assertEqual(step["status"], "todo")
        self.assertIn("setup_start_bettergi_task.ps1", step["fix"])

    def test_unknown_scheduled_state_is_reported_as_unknown(self):
        with self._exit_stack(self._patches(True)[:-1]), patch.object(
            guide, "scheduled_task_state", return_value=None
        ):
            data = guide.build_guide(env_path=str(self.env_file))

        step = [row for row in data["steps"] if row["id"] == "scheduled"][0]
        self.assertEqual(step["status"], "unknown")

    def test_missing_llm_key_is_a_todo(self):
        self.env_file.write_text("LLM_PROVIDER=openai\nMODEL_NAME=deepseek-chat\n", encoding="utf-8")

        data = self._guide()

        step = [row for row in data["steps"] if row["id"] == "llm"][0]
        self.assertEqual(step["status"], "todo")
        self.assertIn("OPENAI_API_KEY", step["detail"])

    def test_missing_uid_is_a_todo(self):
        self.env_file.write_text(
            "LLM_PROVIDER=openai\nMODEL_NAME=m\nOPENAI_API_KEY=sk-x\n", encoding="utf-8"
        )

        data = self._guide()

        self.assertEqual([r for r in data["steps"] if r["id"] == "uid"][0]["status"], "todo")

    def test_missing_script_groups_are_listed(self):
        for name in ("矿物", "食材与炼金"):
            (self.bgi_dir / "User" / "ScriptGroup" / f"{name}.json").unlink()

        data = self._guide()

        step = [row for row in data["steps"] if row["id"] == "script_groups"][0]
        self.assertEqual(step["status"], "warn")
        self.assertIn("矿物", step["detail"])
        self.assertIn("食材与炼金", step["detail"])

    def test_broken_strategy_is_warned_with_group_names(self):
        (self.bgi_dir / "User" / "ScriptGroup" / "敌人与魔物.json").write_text(
            json.dumps(_group("敌人与魔物", strategy="1.四神挂机[推荐]"), ensure_ascii=False),
            encoding="utf-8",
        )

        data = self._guide()

        step = [row for row in data["steps"] if row["id"] == "strategies"][0]
        self.assertEqual(step["status"], "warn")
        self.assertIn("敌人与魔物", step["detail"])

    def test_missing_bettergi_dir_is_a_todo(self):
        with patch.object(config, "BGI_DIR", str(self.root / "nope")):
            data = guide.build_guide(env_path=str(self.env_file))

        self.assertEqual([r for r in data["steps"] if r["id"] == "bettergi"][0]["status"], "todo")

    def test_guide_never_raises_even_if_a_check_explodes(self):
        with patch.object(guide, "_pathing_step", side_effect=RuntimeError("boom")):
            data = guide.build_guide(env_path=str(self.env_file))

        self.assertTrue(data["ok"])
        self.assertTrue(any(step["status"] == "unknown" for step in data["steps"]))

    def test_ps_scripts_are_documented(self):
        data = self._guide()

        paths = {row["path"] for row in data["ps_scripts"]}
        self.assertIn("scripts/setup_start_bettergi_task.ps1", paths)
        self.assertIn("scripts/stop_bettergi.ps1", paths)
        self.assertIn("scripts/stop_genshin.ps1", paths)
        for row in data["ps_scripts"]:
            self.assertTrue(row["run"])
            self.assertTrue(row["notes"])

    def test_ps_commands_use_absolute_paths(self):
        """实测踩过：管理员 PowerShell 默认在 C:\\WINDOWS\\system32，相对路径会"参数不存在"。"""
        for row in self._guide()["ps_scripts"]:
            with self.subTest(script=row["path"]):
                self.assertIn(":\\", row["run"])
                self.assertNotIn('"scripts\\', row["run"])

    def test_scheduled_tasks_are_listed_with_state(self):
        """说明书页要逐条列出三个计划任务（含关原神用的 StopGenshin）。"""
        rows = self._guide()["scheduled_tasks"]
        by_name = {row["name"]: row for row in rows}

        self.assertEqual(set(by_name), {"StartBetterGI", "StopBetterGI", "StopGenshin"})
        self.assertIn("原神", by_name["StopGenshin"]["purpose"])
        self.assertFalse(by_name["StopGenshin"]["required"])
        for row in rows:
            self.assertIn(row["state"], {"registered", "missing", "unknown"})

    def test_scheduled_step_mentions_stop_genshin(self):
        step = [row for row in self._guide(tasks_exist=False)["steps"] if row["id"] == "scheduled"][0]

        self.assertEqual(step["status"], "todo")
        self.assertIn("StopGenshin", step["fix"])
        self.assertIn(":\\", step["fix"])                 # 修复命令也是完整路径

    def test_missing_stop_genshin_only_is_a_warning(self):
        results = {"StartBetterGI": True, "StopBetterGI": True, "StopGenshin": False}
        with patch.object(guide, "scheduled_task_state", side_effect=lambda name: results[name]):
            step = [row for row in guide.build_guide(env_path=str(self.env_file))["steps"]
                    if row["id"] == "scheduled"][0]

        self.assertEqual(step["status"], "warn")
        self.assertIn("StopGenshin", step["detail"])


class ExpectedTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.group_dir = Path(self.temp_dir.name)
        (self.group_dir / "地图素材.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_states_cover_registered_group_only_and_missing(self):
        rows = guide.expected_tasks(
            group_dir=str(self.group_dir),
            registered_names={"地图素材", "敌人与魔物"},
        )
        by_name = {row["name"]: row for row in rows}

        self.assertEqual(by_name["地图素材"]["state"], "registered")
        # 敌人与魔物登记了但没有组文件 → 仍算 registered（登记是运行的关键）
        self.assertEqual(by_name["敌人与魔物"]["state"], "registered")
        # 有组文件、没登记 → group_only（Agent 执行时会自动登记）
        self.assertEqual(by_name["地图素材"]["has_group"], True)
        self.assertEqual(by_name["矿物"]["state"], "missing")
        self.assertTrue(by_name["锄大地"]["auto_group"])

    def test_env_can_rename_a_group(self):
        (self.group_dir / "自定义锄地.json").write_text("{}", encoding="utf-8")

        rows = guide.expected_tasks(
            group_dir=str(self.group_dir),
            registered_names=(),
            env_values={"BGI_HOE_CONFIG_NAME": "自定义锄地.json"},
        )

        hoe = [row for row in rows if row["name"] == "锄大地"][0]
        self.assertTrue(hoe["has_group"])

    def test_every_expected_task_has_a_purpose(self):
        for row in guide.EXPECTED_TASKS:
            self.assertTrue(row["name"])
            self.assertTrue(row["purpose"])


class ScheduledTaskProbeTests(unittest.TestCase):
    def test_schtasks_hit_wins(self):
        with patch.object(guide, "_schtasks_query", return_value=True):
            self.assertIs(guide.scheduled_task_state("StartBetterGI"), True)

    def test_falls_back_to_powershell(self):
        with patch.object(guide, "_schtasks_query", return_value=None), patch.object(
            guide, "_powershell_task_probe", return_value=False
        ):
            self.assertIs(guide.scheduled_task_state("StartBetterGI"), False)

    def test_both_unknown_gives_none(self):
        with patch.object(guide, "_schtasks_query", return_value=None), patch.object(
            guide, "_powershell_task_probe", return_value=None
        ):
            self.assertIsNone(guide.scheduled_task_state("StartBetterGI"))


class GuideEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_file = Path(self.temp_dir.name) / ".env"
        self.env_file.write_text("LLM_PROVIDER=openai\nOPENAI_API_KEY=sk\nDEFAULT_UID=1\n", encoding="utf-8")
        self.client = server.create_app(env_path=str(self.env_file)).test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_endpoint_returns_steps_and_tasks(self):
        with patch.object(guide, "scheduled_task_state", return_value=True):
            data = self.client.get("/api/guide").get_json()

        self.assertTrue(data["ok"])
        self.assertTrue(data["steps"])
        self.assertTrue(data["expected_tasks"])
        self.assertIn("paths", data)
        self.assertTrue(all("status" in step for step in data["steps"]))

    def test_guide_page_is_served(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn('data-page="guide"', html)
        self.assertIn("一条龙要添加哪些调度器", html)
        self.assertIn("setup_start_bettergi_task.ps1", html)
        self.assertIn("btn-guide-repair", html)

    def test_repair_endpoint_reports_lines(self):
        from skills import bgi_controller

        def fake_repair(force=False):
            print("🧹 已移除 Agent 添加但跑不起来的任务项：AutoBoss")
            print("🔧 已关闭《批量讨伐角色养成材料BOSS》的自定义配置项：showEditorOnStart=False")
            return 0

        with patch.object(bgi_controller, "repair_agent_tasks", side_effect=fake_repair):
            data = self.client.post("/api/repair").get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["code"], 0)
        self.assertTrue(any("AutoBoss" in line for line in data["lines"]))

    def test_repair_endpoint_reports_refusal(self):
        from skills import bgi_controller

        with patch.object(bgi_controller, "repair_agent_tasks", return_value=1):
            data = self.client.post("/api/repair").get_json()

        self.assertFalse(data["ok"])
        self.assertEqual(data["code"], 1)


if __name__ == "__main__":
    unittest.main()
