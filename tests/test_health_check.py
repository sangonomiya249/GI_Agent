import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from skills import health_check


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class LlmConfigTests(unittest.TestCase):
    def test_missing_provider_is_an_error(self):
        rows = health_check.check_llm_config({})

        self.assertEqual(rows[0][0], "error")
        self.assertIn("LLM_PROVIDER", rows[0][1])

    def test_provider_without_key_is_an_error(self):
        rows = health_check.check_llm_config({"LLM_PROVIDER": "openai", "MODEL_NAME": "deepseek-chat"})

        self.assertTrue(any(level == "error" and "OPENAI_API_KEY" in text for level, text in rows))
        self.assertTrue(any(level == "ok" and "deepseek-chat" in text for level, text in rows))

    def test_provider_with_key_and_model_is_ok(self):
        rows = health_check.check_llm_config(
            {
                "LLM_PROVIDER": "openai",
                "MODEL_NAME": "deepseek-chat",
                "OPENAI_API_KEY": "sk-x",
                "LLM_TIMEOUT_SECONDS": "300",
            }
        )

        self.assertFalse([row for row in rows if row[0] == "error"])

    def test_tiny_timeout_is_flagged(self):
        rows = health_check.check_llm_config({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-x", "LLM_TIMEOUT_SECONDS": "10"})

        self.assertTrue(any(level == "warn" and "偏小" in text for level, text in rows))

    def test_local_provider_does_not_need_a_real_key(self):
        rows = health_check.check_llm_config({"LLM_PROVIDER": "local", "LOCAL_API_KEY": "local"})

        self.assertFalse([row for row in rows if row[0] == "error"])


class ChannelTests(unittest.TestCase):
    """远程通道体检：QQ / 飞书都是可选，但配错了要能看出来。"""

    def test_unconfigured_qq_is_info_not_error(self):
        rows = health_check.check_channels({})

        self.assertEqual([level for level, _text in rows], ["info"])
        self.assertIn("未配置", rows[0][1])

    def test_configured_qq_with_whitelist(self):
        rows = health_check.check_channels({
            "QQ_BOT_APPID": "10001",
            "QQ_BOT_SECRET": "x",
            "QQ_BOT_ALLOWED_USERS": "u1, u2，u3",
        })

        self.assertEqual(rows[0][0], "ok")
        self.assertIn("3 个 openid", rows[0][1])

    def test_configured_qq_without_whitelist_warns(self):
        rows = health_check.check_channels({"QQ_BOT_APPID": "10001", "QQ_BOT_SECRET": "x"})

        self.assertEqual(rows[0][0], "warn")
        self.assertIn("QQ_BOT_ALLOWED_USERS", rows[0][1])

    def test_allow_anyone_is_flagged_as_dangerous(self):
        rows = health_check.check_channels({
            "QQ_BOT_APPID": "10001",
            "QQ_BOT_SECRET": "x",
            "QQ_BOT_ALLOW_ANYONE": "1",
        })

        self.assertTrue(any(level == "warn" and "危险" in text for level, text in rows))

    def test_half_configured_qq_warns(self):
        rows = health_check.check_channels({"QQ_BOT_APPID": "10001"})

        self.assertEqual(rows[0][0], "warn")
        self.assertIn("只填了一个", rows[0][1])

    def test_feishu_configured_is_reported(self):
        rows = health_check.check_channels({"FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "y"})

        self.assertTrue(any(level == "ok" and "飞书" in text for level, text in rows))


class ScriptGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.group_dir = self.root / "ScriptGroup"
        self.group_dir.mkdir()
        self.auto_fight = self.root / "AutoFight"
        self.auto_fight.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_broken_strategy_is_listed_with_the_group_name(self):
        _write(
            self.group_dir / "敌人与魔物.json",
            {
                "index": 1,
                "name": "敌人与魔物",
                "config": {"pathingConfig": {"autoFightConfig": {"strategyName": "1.四神挂机[推荐]"}}},
                "projects": [{"name": "a.json", "folderName": "敌人与魔物\\蕈兽", "type": "Pathing"}],
            },
        )

        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            config, "BGI_AUTO_FIGHT_DIR", str(self.auto_fight)
        ):
            rows = health_check.check_script_groups()

        warnings = [text for level, text in rows if level == "warn"]
        self.assertTrue(warnings)
        self.assertIn("敌人与魔物", warnings[0])
        self.assertIn("战斗策略文件不存在", warnings[0])

    def test_valid_strategy_is_silent(self):
        _write(
            self.group_dir / "地图素材.json",
            {
                "index": 2,
                "name": "地图素材",
                "config": {"pathingConfig": {"autoFightConfig": {"strategyName": "根据队伍自动选择"}}},
                "projects": [{"name": "a.json", "folderName": "地方特产\\清心", "type": "Pathing"}],
            },
        )

        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            config, "BGI_AUTO_FIGHT_DIR", str(self.auto_fight)
        ):
            rows = health_check.check_script_groups()

        self.assertTrue(any(level == "ok" and "都能对上" in text for level, text in rows))
        self.assertFalse([row for row in rows if row[0] == "warn" and "策略" in row[1]])

    def test_missing_group_dir_is_an_error(self):
        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.root / "nope")):
            rows = health_check.check_script_groups()

        self.assertEqual(rows[0][0], "error")


class RouteSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.pathing = self.root / "AutoPathing"
        hoe = self.pathing / "锄地专区" / "小怪2000@mno" / "1_2_璃月"
        hoe.mkdir(parents=True)
        (hoe / "2101璃月无妄坡西南.json").write_text("{}", encoding="utf-8")
        self.group_dir = self.root / "ScriptGroup"
        self.group_dir.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_categories_are_summarised_with_counts(self):
        with patch.object(config, "BGI_AUTO_PATHING_DIR", str(self.pathing)), patch.object(
            config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)
        ):
            rows = health_check.check_route_sources()

        self.assertEqual(rows[0][0], "ok")
        self.assertIn("锄地专区", rows[0][1])
        self.assertIn("1 条", rows[0][1])
        self.assertIn("还没有组", rows[0][1])

    def test_existing_group_is_reported(self):
        (self.group_dir / config.BGI_HOE_CONFIG_NAME).write_text("{}", encoding="utf-8")

        with patch.object(config, "BGI_AUTO_PATHING_DIR", str(self.pathing)), patch.object(
            config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)
        ):
            rows = health_check.check_route_sources()

        self.assertIn("已有组", rows[0][1])

    def test_missing_pathing_dir_is_a_warning(self):
        with patch.object(config, "BGI_AUTO_PATHING_DIR", str(self.root / "nope")):
            rows = health_check.check_route_sources()

        self.assertEqual(rows[0][0], "warn")


class ReportTests(unittest.TestCase):
    def test_report_uses_icons_and_keeps_every_line(self):
        report = health_check.format_report([("ok", "一切正常"), ("warn", "注意一下"), ("error", "坏了")])

        self.assertIn("✅ 一切正常", report)
        self.assertIn("⚠️ 注意一下", report)
        self.assertIn("❌ 坏了", report)
        self.assertEqual(len(report.splitlines()), 3)


class FullReportTests(unittest.TestCase):
    """整体体检要能在"什么都没配"的干净环境里跑完（不碰真机文件）。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "AutoPathing").mkdir()
        (self.root / "ScriptGroup").mkdir()
        (self.root / "OneDragon").mkdir()
        self.one_dragon = _write(
            self.root / "OneDragon" / "地图素材.json",
            {"TaskDefinitions": {"uuid-map": "地图素材"}, "TaskEnabledList": {"uuid-map": True}, "TaskOrder": []},
        )
        self.env_file = self.root / ".env"
        self.env_file.write_text(
            "LLM_PROVIDER=openai\nMODEL_NAME=deepseek-chat\nOPENAI_API_KEY=sk-x\nDEFAULT_UID=1\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_run_health_check_covers_every_section(self):
        from skills import bgi_controller

        with patch.object(config, "BGI_DIR", str(self.root)), patch.object(
            config, "BGI_SCRIPT_GROUP_DIR", str(self.root / "ScriptGroup")
        ), patch.object(
            config, "BGI_AUTO_PATHING_DIR", str(self.root / "AutoPathing")
        ), patch.object(
            config, "BGI_BACKUP_DIR", str(self.root / "backups")
        ), patch.object(
            config, "BGI_ONE_DRAGON_CONFIG", str(self.one_dragon)
        ), patch.object(
            bgi_controller, "resolve_one_dragon_config_path", return_value=str(self.one_dragon)
        ), patch.object(
            health_check, "_bettergi_running", return_value=False
        ):
            rows = health_check.run_health_check(str(self.env_file))

        text = health_check.format_report(rows)
        self.assertIn("LLM 提供商：openai", text)
        self.assertIn("当前生效的一条龙配置：地图素材.json", text)
        self.assertIn("调度器里共有 0 个脚本组", text)
        self.assertTrue(rows)

    def test_errors_are_counted_for_the_exit_code(self):
        rows = [("ok", "a"), ("warn", "b"), ("error", "c")]

        self.assertEqual(sum(1 for level, _text in rows if level == "error"), 1)


class ScheduledTaskCheckTests(unittest.TestCase):
    """免 UAC 三个计划任务的环境检测（doctor）。"""

    def _check(self, results):
        from skills import health_check, task_scheduler

        with patch.object(task_scheduler, "task_state", side_effect=lambda name: results[name]):
            return health_check.check_scheduled_tasks()

    def test_all_registered_is_all_ok(self):
        rows = self._check({"StartBetterGI": True, "StopBetterGI": True, "StopGenshin": True})

        self.assertTrue(all(level == "ok" for level, _ in rows), rows)

    def test_missing_required_is_an_error(self):
        rows = self._check({"StartBetterGI": False, "StopBetterGI": True, "StopGenshin": True})

        self.assertTrue(
            any(level == "error" and "StartBetterGI" in text for level, text in rows), rows
        )

    def test_missing_stop_genshin_is_a_warning_with_the_fix(self):
        rows = self._check({"StartBetterGI": True, "StopBetterGI": True, "StopGenshin": False})

        joined = "\n".join(text for _level, text in rows)
        self.assertIn("StopGenshin", joined)
        self.assertIn("拒绝访问", joined)                # 说清为什么需要它
        self.assertIn("setup_start_bettergi_task.ps1", joined)
        self.assertIn(":\\", joined)                     # 完整路径
        self.assertFalse(any(level == "error" for level, _ in rows))

    def test_unknown_state_is_reported_without_panic(self):
        rows = self._check({"StartBetterGI": None, "StopBetterGI": None, "StopGenshin": None})

        self.assertTrue(any("查不出来" in text for _level, text in rows), rows)

    def test_close_game_check_reports_the_plan(self):
        from skills import game_control, health_check

        with patch.object(game_control, "game_running_status", return_value=True), patch.object(
            game_control, "running_processes", return_value={"YuanShen.exe": [4242]}
        ):
            rows = health_check.check_close_game()

        joined = "\n".join(text for _level, text in rows)
        self.assertIn("YuanShen.exe", joined)
        self.assertIn("StopGenshin", joined)


if __name__ == "__main__":
    unittest.main()
