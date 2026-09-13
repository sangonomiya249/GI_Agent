import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from skills import bgi_controller, env_reader
from skills.boss_pathing_guard import ConfigTransactionError
from skills.domain_match import (
    RESIN_ALL_COUNT_FIELDS,
    DomainResult,
    build_domain_resin_plan,
    business_weekday,
    describe_domain_resin_plan,
    domain_closed_notice,
    domain_index_from_days,
    extract_domain,
    resolve_domain_target,
)


class DomainExtractionTests(unittest.TestCase):
    def test_reads_domain_name_from_brackets(self):
        domain, days = extract_domain("【塞西莉亚苗圃】（周二/五/日）】炼武秘境：深没之谷Ⅳ")

        self.assertEqual(domain, "塞西莉亚苗圃")
        self.assertEqual(domain_index_from_days(days), "2")

    def test_maps_stage_name_when_brackets_are_missing(self):
        # 无锋剑这类武器只写了阶段名（水光之城），要靠 DOMAIN_FIX_MAP 换算
        domain, days = extract_domain("（周一/四/日）】炼武秘境：水光之城Ⅲ/Ⅳ")

        self.assertEqual(domain, "塞西莉亚苗圃")
        self.assertEqual(domain_index_from_days(days), "1")

    def test_maps_minglei_stage_used_by_18_weapons(self):
        domain, days = extract_domain("（周二/五/日）】炼武秘境：鸣雷城墟Ⅳ")

        self.assertEqual(domain, "震雷连山密宫")
        self.assertEqual(domain_index_from_days(days), "2")

    def test_strips_schedule_glued_into_the_name(self):
        domain, _days = extract_domain("【菫色之庭（周一/四/日）】精通秘境：初雷幽谷Ⅳ")

        self.assertEqual(domain, "菫色之庭")

    def test_sunday_alone_does_not_pick_an_index(self):
        self.assertIsNone(domain_index_from_days(["日"]))
        self.assertIsNone(domain_index_from_days([]))


class DomainResolveTests(unittest.TestCase):
    """回归测试：LLM 填 target="蓝砚" 时，由代码自己解析成秘境，别写进 DomainName。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.dict_path = self.root / "game_dict_baike_full.json"
        self.dict_path.write_text(
            json.dumps(
                {
                    "avatars": {
                        "lan-yan": {
                            "name_zh": "蓝砚",
                            "materials": ["清水玉", "自在松石", "秘刻金纹的源核"],
                            "talent_materials": {
                                "sources": {
                                    "「勤劳」的哲学": {
                                        "schedule": "【太山府】（周二/五/日）】精通秘境：深炎之底Ⅳ"
                                    }
                                }
                            },
                        }
                    },
                    "weapons": {
                        "thrilling-tales": {
                            "name_zh": "讨龙英杰谭",
                            "materials": ["摩拉", "凛风奔狼的始龀", "凛风奔狼的怀乡"],
                            "ascension_material_sources": {
                                "凛风奔狼的怀乡": {
                                    "schedule": "【塞西莉亚苗圃】（周二/五/日）】炼武秘境：深没之谷Ⅳ"
                                }
                            },
                        },
                        # 这把武器的来源表是空的，只能靠 materials 里的素材族反查（真实数据里的狼的末路）
                        "wolfs-gravestone": {
                            "name_zh": "狼的末路",
                            "materials": ["摩拉", "狮牙斗士的枷锁", "狮牙斗士的理想"],
                            "ascension_material_sources": {},
                        },
                        "fading-twilight": {
                            "name_zh": "西风猎弓",
                            "materials": ["摩拉", "狮牙斗士的理想"],
                            "ascension_material_sources": {
                                "狮牙斗士的理想": {
                                    "schedule": "【塞西莉亚苗圃】（周三/六/日）】炼武秘境：渴水的废都Ⅳ"
                                }
                            },
                        },
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.artifact_path = self.root / "artifact_get_methods_raw.json"
        self.artifact_path.write_text(
            json.dumps(
                {
                    "1": {
                        "matched_tables": [
                            {"rows": [["稀有度", "5星"], ["获取途径", "缘觉塔"]]}
                        ]
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.showcase = [{"name": "蓝砚", "level": 60, "weapon": {"name": "讨龙英杰谭", "level": 20}}]

    def tearDown(self):
        self.temp_dir.cleanup()

    def _resolve(self, raw, avatars=None):
        return resolve_domain_target(
            raw,
            avatars=avatars if avatars is not None else self.showcase,
            data_path=str(self.dict_path),
            artifact_path=str(self.artifact_path),
        )

    def test_character_name_resolves_through_equipped_weapon(self):
        result, notices = self._resolve("蓝砚")

        self.assertEqual(result.domain, "塞西莉亚苗圃")
        self.assertEqual(result.domain_index, "2")
        self.assertTrue(any("讨龙英杰谭" in notice for notice in notices))

    def test_off_showcase_character_resolves_through_mys_weapon(self):
        """展柜里没有的角色（展柜只放 8 个）：从米游社个人战绩里读 TA 的武器再推秘境。"""
        from skills import mys_api

        snapshot = {
            "avatars": [{
                "id": "10000002", "name": "蓝砚", "level": 80,
                "weapon": {"id": "thrilling-tales", "name": "讨龙英杰谭", "level": 90},
            }]
        }
        with patch.object(config, "MYS_COOKIE", "ltuid=1;ltoken=x"), patch.object(
            mys_api, "get_snapshot", return_value=snapshot
        ):
            result, notices = self._resolve("蓝砚", avatars=[])      # 展柜是空的

        self.assertEqual(result.domain, "塞西莉亚苗圃")
        self.assertTrue(any("米游社个人战绩" in notice for notice in notices), notices)

    def test_off_showcase_character_without_cookie_tells_the_truth(self):
        with patch.object(config, "MYS_COOKIE", ""):
            with self.assertRaises(ConfigTransactionError) as caught:
                self._resolve("蓝砚", avatars=[])

        self.assertIn("不在当前展柜数据里", str(caught.exception))
        self.assertIn("MYS_COOKIE", str(caught.exception))

    def test_character_name_inside_natural_language(self):
        result, _notices = self._resolve("去打一次蓝砚武器的突破副本")

        self.assertEqual(result.domain, "塞西莉亚苗圃")

    def test_weapon_name_resolves(self):
        result, notices = self._resolve("讨龙英杰谭")

        self.assertEqual(result.domain, "塞西莉亚苗圃")
        self.assertTrue(any("武器解析" in notice for notice in notices))

    def test_weapon_with_broken_source_table_falls_back_to_its_material_family(self):
        result, _notices = self._resolve("狼的末路")

        self.assertEqual(result.domain, "塞西莉亚苗圃")
        self.assertEqual(result.domain_index, "3")

    def test_material_name_resolves(self):
        result, _notices = self._resolve("「勤劳」的哲学")

        self.assertEqual(result.domain, "太山府")
        self.assertEqual(result.domain_index, "2")

    def test_plain_domain_name_is_accepted_without_index(self):
        result, _notices = self._resolve("塞西莉亚苗圃")

        self.assertEqual(result.domain, "塞西莉亚苗圃")
        # 一个秘境有 3 个阶段对应 3 组开放日，光有秘境名推导不出档位，不能瞎编
        self.assertIsNone(result.domain_index)

    def test_artifact_domain_passes_through(self):
        result, _notices = self._resolve("缘觉塔")

        self.assertEqual(result.domain, "缘觉塔")

    def test_character_missing_from_showcase_asks_for_weapon_or_material(self):
        with self.assertRaises(ConfigTransactionError) as ctx:
            self._resolve("蓝砚", avatars=[])

        message = str(ctx.exception)
        self.assertIn("不在当前展柜数据里", message)
        self.assertIn("武器名", message)

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(ConfigTransactionError) as ctx:
            self._resolve("随便编的秘境")

        self.assertIn("无法把", str(ctx.exception))

    def test_empty_target_is_rejected(self):
        with self.assertRaises(ConfigTransactionError):
            self._resolve("")


class DomainConfigWriteTests(unittest.TestCase):
    """端到端：LLM 给 target="蓝砚" 时，写进 BetterGI 的 DomainName 必须是秘境名。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.one_dragon = self.root / "OneDragon" / "默认配置.json"
        self.one_dragon.parent.mkdir()
        self.one_dragon.write_text(
            json.dumps({"TaskEnabledList": {}, "TaskOrder": []}), encoding="utf-8"
        )

        # 用一份假展柜顶掉同进程缓存，测试不打 Enka
        self._saved_cache = dict(env_reader._LATEST_ENV_DATA)
        env_reader._LATEST_ENV_DATA.update(
            {
                "uid": "100000000",
                "fetched_at": datetime.datetime.now(),
                "data": {
                    "avatars": [
                        {"name": "蓝砚", "level": 60, "weapon": {"name": "讨龙英杰谭", "level": 20}}
                    ]
                },
            }
        )

    def tearDown(self):
        env_reader._LATEST_ENV_DATA.clear()
        env_reader._LATEST_ENV_DATA.update(self._saved_cache)
        self.temp_dir.cleanup()

    def test_character_target_becomes_a_real_domain_name(self):
        with patch.object(
            bgi_controller,
            "resolve_one_dragon_config_path",
            return_value=str(self.one_dragon),
        ), patch.object(
            bgi_controller, "ensure_bettergi_closed", return_value=True
        ), patch.object(
            bgi_controller.feishu_api, "send_feishu_msg"
        ), patch.object(
            config, "BGI_BACKUP_DIR", str(self.root / "backups")
        ), patch.object(
            config, "BGI_AUTO_CREATE_ROUTE_GROUP", False
        ), patch.object(
            config, "AGENT_TASK_STATE_PATH", str(self.root / "agent_tasks.json")
        ):
            bgi_controller.execute_bgi_task(
                {"energy_task": {"action": "run_domain", "target": "蓝砚", "count": 1}},
                "t",
                {},
                "open_id",
                "100000000",
            )

        written = json.loads(self.one_dragon.read_text(encoding="utf-8"))
        self.assertEqual(written["DomainName"], "塞西莉亚苗圃")
        # domain_index 也由代码从日程推出来，不再靠模型猜
        self.assertEqual(written["SundayEverySelectedValue"], "2")


class DomainOpenDayTests(unittest.TestCase):
    """「该秘境今日不开放」的提示：只提醒，不阻断玩家的显式指令。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.dict_path = self.root / "game_dict_baike_full.json"
        self.dict_path.write_text(
            json.dumps(
                {
                    "avatars": {"lan-yan": {"name_zh": "蓝砚", "materials": []}},
                    "weapons": {
                        "thrilling-tales": {
                            "name_zh": "讨龙英杰谭",
                            "materials": ["凛风奔狼的怀乡"],
                            "ascension_material_sources": {
                                "凛风奔狼的怀乡": {
                                    "schedule": "【塞西莉亚苗圃】（周二/五/日）】炼武秘境：深没之谷Ⅳ"
                                }
                            },
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.showcase = [{"name": "蓝砚", "weapon": {"name": "讨龙英杰谭"}}]

    def tearDown(self):
        self.temp_dir.cleanup()

    def _resolve(self, today):
        return resolve_domain_target(
            "蓝砚",
            avatars=self.showcase,
            data_path=str(self.dict_path),
            today=today,
        )

    def test_closed_day_is_noticed(self):
        result, notices = self._resolve("四")

        self.assertEqual(result.open_days, "周二/五/日")
        closed = [notice for notice in notices if "今日不开放" in notice]
        self.assertEqual(len(closed), 1)
        self.assertIn("周二/五/日", closed[0])
        self.assertIn("周四", closed[0])

    def test_open_day_has_no_notice(self):
        _result, notices = self._resolve("二")

        self.assertFalse([notice for notice in notices if "今日不开放" in notice])

    def test_sunday_is_open_for_every_stage(self):
        _result, notices = self._resolve("日")

        self.assertFalse([notice for notice in notices if "今日不开放" in notice])

    def test_unknown_schedule_yields_no_notice(self):
        # 圣遗物秘境常驻开放，没有日程可判
        self.assertEqual(domain_closed_notice(None), "")
        self.assertEqual(
            domain_closed_notice(DomainResult("缘觉塔", None, None, "圣遗物秘境")), ""
        )

    def test_business_weekday_uses_the_four_hour_offset(self):
        # 周五凌晨 2 点算周四（业务日期对齐凌晨 4 点刷新）
        self.assertEqual(business_weekday(datetime.datetime(2026, 9, 11, 2, 0)), "四")
        self.assertEqual(business_weekday(datetime.datetime(2026, 9, 11, 10, 0)), "五")


class DomainResinPlanTests(unittest.TestCase):
    """「只刷 N 次」必须写进 User/config.json 的 autoDomainConfig，否则会刷干体力。"""

    def test_default_uses_20_resin_per_run(self):
        plan = build_domain_resin_plan(1, preference="原粹树脂20")

        self.assertTrue(plan["specifyResinUse"])
        self.assertEqual(plan["originalResin20UseCount"], 1)
        self.assertEqual(plan["resinPriorityList"], ["原粹树脂20"])
        # 其余树脂次数必须清零，否则上一轮残留的次数会让它多刷
        for field in RESIN_ALL_COUNT_FIELDS:
            if field != "originalResin20UseCount":
                self.assertEqual(plan[field], 0)

    def test_condensed_resin_preference(self):
        plan = build_domain_resin_plan(3, preference="浓缩树脂")

        self.assertEqual(plan["condensedResinUseCount"], 3)
        self.assertEqual(plan["originalResin20UseCount"], 0)
        self.assertEqual(plan["resinPriorityList"], ["浓缩树脂"])

    def test_unknown_preference_falls_back_to_20_resin(self):
        plan = build_domain_resin_plan(2, preference="不存在的树脂")

        self.assertEqual(plan["originalResin20UseCount"], 2)

    def test_missing_count_means_do_not_touch_resin_config(self):
        for value in (None, "", 0, -1, "abc"):
            self.assertIsNone(build_domain_resin_plan(value, preference="原粹树脂20"))

    def test_missing_count_is_explained_loudly(self):
        notice = describe_domain_resin_plan(None)

        self.assertIn("刷到体力耗尽", notice)

    def test_plan_notice_mentions_the_count(self):
        notice = describe_domain_resin_plan(1, preference="原粹树脂20")

        self.assertIn("只刷 1 次", notice)
        self.assertIn("原粹树脂20", notice)


class DomainResinWriteTests(unittest.TestCase):
    """端到端：run_domain + count=1 时，两处配置都要被正确覆写。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.one_dragon = self.root / "OneDragon" / "地图素材.json"
        self.one_dragon.parent.mkdir()
        self.one_dragon.write_text(
            json.dumps({"TaskEnabledList": {}, "TaskOrder": []}), encoding="utf-8"
        )
        self.global_config = self.root / "config.json"
        self.global_config.write_text(
            json.dumps(
                {
                    "autoDomainConfig": {
                        "specifyResinUse": False,
                        "condensedResinUseCount": 5,
                        "originalResin20UseCount": 4,
                    }
                }
            ),
            encoding="utf-8",
        )

        self._saved_cache = dict(env_reader._LATEST_ENV_DATA)
        env_reader._LATEST_ENV_DATA.update(
            {
                "uid": "100000000",
                "fetched_at": datetime.datetime.now(),
                "data": {
                    "avatars": [
                        {"name": "蓝砚", "level": 60, "weapon": {"name": "讨龙英杰谭", "level": 20}}
                    ]
                },
            }
        )

    def tearDown(self):
        env_reader._LATEST_ENV_DATA.clear()
        env_reader._LATEST_ENV_DATA.update(self._saved_cache)
        self.temp_dir.cleanup()

    def _run(self, energy_task):
        with patch.object(
            bgi_controller, "resolve_one_dragon_config_path", return_value=str(self.one_dragon)
        ), patch.object(
            bgi_controller, "ensure_bettergi_closed", return_value=True
        ), patch.object(
            bgi_controller.feishu_api, "send_feishu_msg"
        ), patch.object(
            config, "BGI_GLOBAL_CONFIG", str(self.global_config)
        ), patch.object(
            config, "BGI_BACKUP_DIR", str(self.root / "backups")
        ), patch.object(
            config, "BGI_AUTO_CREATE_ROUTE_GROUP", False
        ), patch.object(
            config, "AGENT_TASK_STATE_PATH", str(self.root / "agent_tasks.json")
        ):
            bgi_controller.execute_bgi_task(energy_task, "t", {}, "open_id", "100000000")

    def test_one_run_writes_resin_limit_and_domain(self):
        self._run(
            {
                "energy_task": {
                    "action": "run_domain",
                    "target": "蓝砚",
                    "count": 1,
                    "domain_index": "2",
                }
            }
        )

        written = json.loads(self.one_dragon.read_text(encoding="utf-8"))
        self.assertEqual(written["DomainName"], "塞西莉亚苗圃")
        self.assertEqual(written["SundayEverySelectedValue"], "2")

        domain_cfg = json.loads(self.global_config.read_text(encoding="utf-8"))["autoDomainConfig"]
        self.assertTrue(domain_cfg["specifyResinUse"])
        self.assertEqual(domain_cfg["originalResin20UseCount"], 1)
        # 旧的浓缩树脂次数（5）必须被清掉，不然它会先刷 5 次浓缩
        self.assertEqual(domain_cfg["condensedResinUseCount"], 0)

    def test_missing_count_leaves_resin_config_untouched(self):
        self._run({"energy_task": {"action": "run_domain", "target": "蓝砚"}})

        domain_cfg = json.loads(self.global_config.read_text(encoding="utf-8"))["autoDomainConfig"]
        self.assertFalse(domain_cfg["specifyResinUse"])
        self.assertEqual(domain_cfg["condensedResinUseCount"], 5)
        self.assertEqual(domain_cfg["originalResin20UseCount"], 4)


if __name__ == "__main__":
    unittest.main()
