import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from skills import bgi_controller
from skills.boss_pathing_guard import (
    ConfigTransactionError,
    normalize_boss_name,
    strategy_notice,
    validate_boss_target,
)


class BossPathingGuardTests(unittest.TestCase):
    """回归测试：BetterGI 日志里「无法寻路 / JSON tokens」那类翻车。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.script_dir = self.root / "批量讨伐角色养成材料BOSS"
        self.pathing_dir = self.script_dir / "assets" / "Pathing"
        self.config_dir = self.script_dir / "assets" / "config"
        self.pathing_dir.mkdir(parents=True)
        self.config_dir.mkdir(parents=True)

        # 真实脚本里的名字：注意「秘源机兵·构型械」中间那个 ·
        for filename in (
            "秘源机兵·构型械前往.json",
            "无相之雷前往.json",
            "急冻树前往.json",
            "蕴光月守宫强制传送.json",
            "蕴光月守宫键鼠前往.json",
        ):
            (self.pathing_dir / filename).write_text('{"positions": []}', encoding="utf-8")

        (self.config_dir / "boss-list.json").write_text(
            json.dumps(
                {
                    "bossList": {
                        "蒙德": ["急冻树", "无相之雷", "无相之风（不支持）"],
                        "须弥": ["秘源机兵·构型械"],
                        "挪德卡莱": ["蕴光月守宫"],
                    },
                    "unsupportedBosses": ["无相之风", "黄金王兽", "无相之冰", "深海龙蜥之群"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _validate(self, raw_name):
        return validate_boss_target(raw_name, script_dir=self.script_dir)

    def test_normalizes_missing_middle_dot(self):
        """实测翻车案例：配置写成「秘源机兵构型械」→ 必须纠成脚本里的写法。"""
        name, notices = self._validate("秘源机兵构型械")

        self.assertEqual(name, "秘源机兵·构型械")
        self.assertTrue(any("自动规范化" in notice for notice in notices))
        self.assertTrue((self.pathing_dir / f"{name}前往.json").exists())

    def test_exact_name_passes_without_notice(self):
        name, notices = self._validate("急冻树")

        self.assertEqual(name, "急冻树")
        self.assertEqual(notices, [])

    def test_rejects_unsupported_boss_instead_of_fuzzy_correcting_it(self):
        """「无相之风」不支持，绝不能被模糊匹配成「无相之雷」去打错 Boss。"""
        with self.assertRaises(ConfigTransactionError) as ctx:
            self._validate("无相之风")

        self.assertIn("不支持", str(ctx.exception))

    def test_rejects_near_name_that_is_not_within_separator_tolerance(self):
        with self.assertRaises(ConfigTransactionError) as ctx:
            self._validate("无相之岩")

        self.assertIn("无相之雷", str(ctx.exception))

    def test_warns_when_boss_needs_keymouse_macro_fallback(self):
        name, notices = self._validate("蕴光月守宫")

        self.assertEqual(name, "蕴光月守宫")
        self.assertTrue(any("键鼠" in notice for notice in notices))

    def test_rejects_supported_name_without_any_path_file(self):
        boss_list = json.loads((self.config_dir / "boss-list.json").read_text(encoding="utf-8"))
        boss_list["bossList"]["测试区"] = ["某个没写路径的新BOSS"]
        (self.config_dir / "boss-list.json").write_text(
            json.dumps(boss_list, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaises(ConfigTransactionError) as ctx:
            self._validate("某个没写路径的新BOSS")

        self.assertIn("没有可用的路径文件", str(ctx.exception))

    def test_falls_back_to_local_dict_when_script_is_not_installed(self):
        dict_path = self.root / "boss_drops_dict.json"
        dict_path.write_text(
            json.dumps({"深黯魇语之主": ["掉落"]}, ensure_ascii=False), encoding="utf-8"
        )

        name, notices = validate_boss_target(
            "深黯魇语之主", script_dir=self.root / "并不存在的脚本", dict_path=dict_path
        )

        self.assertEqual(name, "深黯魇语之主")
        self.assertTrue(any("路径文件目录" in notice for notice in notices))

    def test_empty_target_is_rejected(self):
        with self.assertRaises(ConfigTransactionError):
            self._validate("无")

    def test_normalize_ignores_separators_and_unsupported_marker(self):
        self.assertEqual(normalize_boss_name("秘源机兵·构型械"), normalize_boss_name("秘源机兵构型械"))
        self.assertEqual(normalize_boss_name("无相之风（不支持）"), "无相之风")


class StrategyNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.auto_fight_dir = Path(self.temp_dir.name) / "AutoFight"
        self.auto_fight_dir.mkdir()
        (self.auto_fight_dir / "万能战斗策略（萌新推荐）.txt").write_text("", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_existing_strategy_is_silent(self):
        self.assertEqual(strategy_notice("万能战斗策略（萌新推荐）", str(self.auto_fight_dir)), "")

    def test_special_value_is_silent(self):
        self.assertEqual(strategy_notice("根据队伍自动选择", str(self.auto_fight_dir)), "")

    def test_unknown_strategy_is_reported(self):
        notice = strategy_notice("并不存在的策略", str(self.auto_fight_dir))

        # 文案与 BetterGI 的真实报错对齐（AutoFightHandler.cs 抛的就是这句）
        self.assertIn("战斗策略文件不存在", notice)


class BossValidationOrderingTests(unittest.TestCase):
    """名字写错时不该先把正在跑的一条龙杀掉。"""

    def test_invalid_target_is_rejected_before_bettergi_is_closed(self):
        with patch.object(
            bgi_controller, "resolve_boss_target", side_effect=ConfigTransactionError("名称不对")
        ) as guard, patch.object(bgi_controller, "ensure_bettergi_closed") as closer, patch.object(
            bgi_controller.feishu_api, "send_feishu_msg"
        ) as notifier:
            bgi_controller.execute_bgi_task(
                {"energy_task": {"action": "run_boss", "target": "无相之风"}},
                "t",
                {},
                "open_id",
                "uid",
            )

        guard.assert_called_once_with("无相之风")
        closer.assert_not_called()
        self.assertIn("名称不对", notifier.call_args[0][1])


class BossConfigWriteTests(unittest.TestCase):
    """端到端：写进 BetterGI 的 config.json 必须是脚本认得的官方 Boss 名。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        script_dir = self.root / "JsScript" / "批量讨伐角色养成材料BOSS"
        pathing_dir = script_dir / "assets" / "Pathing"
        boss_config_dir = script_dir / "assets" / "config"
        pathing_dir.mkdir(parents=True)
        boss_config_dir.mkdir(parents=True)
        (pathing_dir / "秘源机兵·构型械前往.json").write_text('{"positions": []}', encoding="utf-8")
        (boss_config_dir / "boss-list.json").write_text(
            json.dumps({"bossList": {"须弥": ["秘源机兵·构型械"]}, "unsupportedBosses": []}, ensure_ascii=False),
            encoding="utf-8",
        )

        self.boss_config = boss_config_dir / "config.json"
        self.boss_config.write_text("[]", encoding="utf-8")
        # 讨伐走的脚本组（存在才会被登记进一条龙）
        self.script_group_dir = self.root / "ScriptGroup"
        self.script_group_dir.mkdir()
        (self.script_group_dir / "批量讨伐角色养成材料BOSS.json").write_text(
            json.dumps(
                {"name": "批量讨伐角色养成材料BOSS", "projects": [{"name": "x", "type": "Javascript"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.one_dragon_config = self.root / "OneDragon" / "默认配置.json"
        self.one_dragon_config.parent.mkdir()
        self.one_dragon_config.write_text(
            json.dumps({"TaskEnabledList": {}, "TaskOrder": []}), encoding="utf-8"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_broken_name_is_written_as_official_name(self):
        with patch.object(config, "BGI_BOSS_CONFIG", str(self.boss_config)), patch.object(
            config, "BGI_BACKUP_DIR", str(self.root / "backups")
        ), patch.object(
            config, "BGI_AUTO_CREATE_ROUTE_GROUP", False
        ), patch.object(
            config, "AGENT_TASK_STATE_PATH", str(self.root / "agent_tasks.json")
        ), patch.object(
            config, "BGI_SCRIPT_GROUP_DIR", str(self.script_group_dir)
        ), patch.object(
            bgi_controller, "resolve_one_dragon_config_path", return_value=str(self.one_dragon_config)
        ), patch.object(
            bgi_controller, "ensure_bettergi_closed", return_value=True
        ), patch.object(
            bgi_controller.feishu_api, "send_feishu_msg"
        ):
            bgi_controller.execute_bgi_task(
                {"energy_task": {"action": "run_boss", "target": "秘源机兵构型械", "count": 1}},
                "t",
                {},
                "open_id",
                "uid",
            )

        written = json.loads(self.boss_config.read_text(encoding="utf-8"))
        self.assertEqual(written[0]["name"], "秘源机兵·构型械")
        self.assertEqual(written[0]["totalCount"], 1)
        # 讨伐任务应该是《批量讨伐角色养成材料BOSS》脚本组，而不是凭空冒出来的 AutoBoss
        one_dragon = json.loads(self.one_dragon_config.read_text(encoding="utf-8"))
        definitions = one_dragon.get("TaskDefinitions") or {}
        enabled = [key for key, value in (one_dragon.get("TaskEnabledList") or {}).items() if value]
        names = [definitions.get(key, key) for key in enabled]
        self.assertEqual(names, ["批量讨伐角色养成材料BOSS"])
        self.assertNotIn("AutoBoss", definitions.values())


if __name__ == "__main__":
    unittest.main()