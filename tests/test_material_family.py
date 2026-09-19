"""材料族（材料 → 族群 → BetterGI 路线）的测试。

覆盖三件事：
  1. 离线构建脚本的来源行解析 —— 整行锚定，别把玩家吐槽当族群名；
  2. `brain.material_family` 的解析 API —— 认不出必须给 None（禁止猜，§33）；
  3. `brain.material_planner` 接上族群之后的行为 —— 同族多档落同一条路线、下发指令去重。
"""

import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from brain import material_family, material_planner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _requirement(item_id, name, required, missing, owned=0, category="monster_drop"):
    return {
        "item_id": item_id,
        "item_name": name,
        "category": category,
        "required": required,
        "owned": owned,
        "missing": missing,
        "owned_known": True,
        "lack_source": "overall_consume",
    }


def _row(materials, phase="talent", character_id=1, character_name="测试角色"):
    return {
        "character_id": character_id,
        "character_name": character_name,
        "phase": phase,
        "phase_label": "天赋",
        "skipped": False,
        "complete": False,
        "materials": materials,
    }


class SourceLineParsingTests(unittest.TestCase):
    """来源行 → 族群：只有整行就是「【N级以上】XX掉落」才可信。"""

    def setUp(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import build_material_families as builder
        self.builder = builder

    def test_reads_level_and_family(self):
        parsed = self.builder.parse_source_line("【60级以上】原海异种掉落；")
        self.assertEqual(parsed[0], "原海异种")
        self.assertEqual(parsed[1], 60)

    def test_reads_family_without_level(self):
        parsed = self.builder.parse_source_line("异种合成魔兽掉落")
        self.assertEqual(parsed[0], "异种合成魔兽")
        self.assertEqual(parsed[1], 0)

    def test_reads_weekly_boss_reward(self):
        parsed = self.builder.parse_source_line("70级以上多托雷挑战奖励")
        self.assertEqual(parsed[0], "多托雷")
        self.assertEqual(parsed[1], 70)

    def test_rejects_player_complaint(self):
        # 玩家吐槽里也有"掉落"两个字，整行锚定就是为它准备的
        self.assertIsNone(self.builder.parse_source_line("掉落几率很小吧，但是可以用破损的去合成吧？"))
        self.assertIsNone(self.builder.parse_source_line("希望官方公示掉落概率，这玩意我清了一天丘丘人营地都没几个"))

    def test_rejects_domain_and_region_junk(self):
        self.assertIsNone(self.builder.parse_source_line("*周日 炼武秘境 全素材随机掉落"))
        self.assertIsNone(self.builder.parse_source_line("四级世界等级掉落"))
        self.assertIsNone(self.builder.parse_source_line("纳塔地区「征讨领域」掉落"))

    def test_strips_level_prefix_inside_family(self):
        parsed = self.builder.parse_source_line("75级无相之水掉落")
        self.assertEqual(parsed[0], "无相之水")

    def test_generic_monster_drop_is_not_a_family(self):
        # 「怪物掉落」这种泛指不给族 —— 给了等于承认"任意怪都掉"，等于没信息
        self.assertIsNone(self.builder.parse_source_line("怪物掉落"))


class RouteMatchingTests(unittest.TestCase):
    """族群名 → BetterGI 路线目录名。"""

    def setUp(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import build_material_families as builder
        self.builder = builder

    def test_direct_hit(self):
        vocab = {"原海异种"}
        route, count, _why = self.builder.match_route("原海异种", vocab, {"原海异种": ["a.json"]})
        self.assertEqual(route, "原海异种")
        self.assertEqual(count, 1)

    def test_alias_hit(self):
        vocab = {"丘丘人射手"}
        route, _count, why = self.builder.match_route("丘丘射手", vocab, {"丘丘人射手": ["a.json"]})
        self.assertEqual(route, "丘丘人射手")
        self.assertIn("别名", why)

    def test_containment_hit(self):
        vocab = {"蕈兽"}
        route, _count, _why = self.builder.match_route("活化状态下蕈兽", vocab, {"蕈兽": ["a.json"]})
        self.assertEqual(route, "蕈兽")

    def test_no_match_is_honest(self):
        route, count, why = self.builder.match_route("无相之雷", {"原海异种"}, {})
        self.assertEqual(route, "")
        self.assertEqual(count, 0)
        self.assertIn("没有对应", why)


class KnowledgeFileTests(unittest.TestCase):
    """已经生成的知识表：几个必须成立的例子。"""

    def setUp(self):
        if not material_family.available():
            self.skipTest("memory/game_knowledge/material_families.json 还没生成")

    def test_sea_creature_family(self):
        # 需求里点名的例子：缺异色结晶石 → 原海异种这一大类
        family = material_family.resolve_material_family("异色结晶石")
        self.assertIsNotNone(family)
        self.assertEqual(family.name, "原海异种")
        self.assertEqual(family.route, "原海异种")
        self.assertTrue(family.is_enemy_drop)
        self.assertEqual(family.material_names(), ["异海凝珠", "异海之块", "异色结晶石"])
        self.assertEqual(family.rank_of("异色结晶石"), 3)

    def test_new_area_material(self):
        # 至冬的新材料（百科字典里没有，只有 mapping.json 有）
        family = material_family.resolve_material_family("幻造晶鳞石")
        self.assertIsNotNone(family)
        self.assertEqual(family.name, "肌生晶石的妖精")
        self.assertEqual(family.material_names(), ["幻造萤屑", "幻造裂晶", "幻造晶鳞石"])

    def test_weekly_boss_family_has_no_route(self):
        family = material_family.resolve_material_family("扭曲的枯枝")
        self.assertIsNotNone(family)
        self.assertEqual(family.type, "weekly_boss")
        self.assertEqual(family.route, "")
        self.assertFalse(family.has_route)

    def test_unknown_material_is_none(self):
        self.assertIsNone(material_family.resolve_material_family("根本不存在的材料"))
        self.assertIsNone(material_family.resolve_material_family(""))
        self.assertIsNone(material_family.resolve_material_family(None))

    def test_accepts_requirement_dict(self):
        family = material_family.resolve_material_family({"item_id": 104370, "item_name": "史莱姆原浆"})
        self.assertIsNotNone(family)
        self.assertEqual(family.name, "史莱姆")

    def test_group_materials_merges_same_family(self):
        groups = material_family.group_materials(["史莱姆凝液", "史莱姆原浆", "摩拉", "异色结晶石"])
        keys = [item["key"] for item in groups]
        self.assertIn("family:史莱姆", keys)
        self.assertIn("material:摩拉", keys)      # 认不出族的材料各自一组，行为与以前一致
        slime = [item for item in groups if item["key"] == "family:史莱姆"][0]
        self.assertEqual(slime["materials"], ["史莱姆凝液", "史莱姆原浆"])

    def test_describe_mentions_family_and_route(self):
        text = material_family.describe("异色结晶石")
        self.assertIn("原海异种", text)
        self.assertIn("第 3 档", text)
        self.assertEqual(material_family.describe("根本不存在的材料"), "")


class KnowledgeOverrideTests(unittest.TestCase):
    """知识表读的是临时文件 —— 别碰仓库里那份真数据。"""

    def setUp(self):
        self.temp = tempfile.mkdtemp(prefix="family-")
        self.path = os.path.join(self.temp, "material_families.json")
        payload = {
            "version": 1,
            "families": {
                "测试族": {
                    "family_id": "测试族", "name": "测试族", "type": "enemy_drop",
                    "channel": "mob", "route": "测试族群", "route_files": 2,
                    "materials": [{"material": "低档料", "min_level": 0, "rank": 1},
                                  {"material": "高档料", "min_level": 60, "rank": 2}],
                },
            },
            "materials": {
                "低档料": {"material": "低档料", "family": "测试族", "type": "enemy_drop",
                           "channel": "mob", "route": "测试族群", "rank": 1},
                "高档料": {"material": "高档料", "family": "测试族", "type": "enemy_drop",
                           "channel": "mob", "route": "测试族群", "rank": 2},
            },
            "stats": {"families": 1},
        }
        with io.open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        self.patch = patch.object(material_family, "KNOWLEDGE_PATH", self.path)
        self.patch.start()
        material_family.refresh()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.patch.stop()
        material_family.refresh()

    def test_resolves_from_temp_table(self):
        family = material_family.resolve_material_family("高档料")
        self.assertIsNotNone(family)
        self.assertEqual(family.id, "测试族")
        self.assertEqual(family.materials, (("低档料", 0, 1), ("高档料", 60, 2)))

    def test_missing_file_means_unavailable(self):
        with patch.object(material_family, "KNOWLEDGE_PATH", os.path.join(self.temp, "nope.json")):
            material_family.refresh()
            self.assertFalse(material_family.available())
            self.assertIsNone(material_family.resolve_material_family("高档料"))


class ResolveSourceTests(unittest.TestCase):
    """规划器的来源解析：族群路线、首领、周本三条岔路。"""

    def setUp(self):
        self.family_patch = patch.object(
            material_planner.material_family, "resolve_material_family"
        )
        self.family_mock = self.family_patch.start()
        self.addCleanup(self.family_patch.stop)

    def _family(self, name="原海异种", type_="enemy_drop", route="原海异种",
                materials=(("异海凝珠", 0, 1), ("异海之块", 40, 2), ("异色结晶石", 60, 3))):
        return material_family.MaterialFamily(
            id=name, name=name, type=type_, channel="mob", route=route,
            route_files=54, materials=materials,
        )

    def test_enemy_drop_uses_family_route(self):
        self.family_mock.return_value = self._family()
        with patch.object(material_planner.material_family, "route_available", return_value=True), \
             patch.object(material_planner.material_family, "route_pool_size", return_value=54):
            source = material_planner.resolve_source({"item_name": "异色结晶石", "item_id": 7})
        self.assertEqual(source["type"], material_planner.TASK_HUNT)
        self.assertEqual(source["route"], "原海异种")           # 路线是**族群**，不是材料名
        self.assertEqual(source["family"], "原海异种")
        self.assertIn("同族还有", source["reason"])
        self.assertIn("54", source["reason"])

    def test_family_without_live_route_stays_waiting(self):
        # 知识表里有、本机路线库删了那一组 → 不许伪造路线
        self.family_mock.return_value = self._family(route="")
        source = material_planner.resolve_source({"item_name": "异色结晶石", "item_id": 7})
        self.assertEqual(source["type"], material_planner.TASK_WAITING_ROUTE)
        self.assertIn("原海异种", source["reason"])

    def test_weekly_boss_reason_is_specific(self):
        self.family_mock.return_value = self._family(name="多托雷", type_="weekly_boss", route="",
                                                     materials=(("扭曲的枯枝", 70, 2),))
        source = material_planner.resolve_source({"item_name": "扭曲的枯枝", "item_id": 8})
        self.assertEqual(source["type"], material_planner.TASK_WAITING_ROUTE)
        self.assertIn("周本", source["reason"])
        self.assertIn("多托雷", source["reason"])

    def test_world_boss_becomes_boss_task_when_executable(self):
        self.family_mock.return_value = self._family(name="无相之雷", type_="world_boss", route="",
                                                     materials=(("雷光棱镜", 30, 1),))
        with patch.object(material_planner, "_boss_target_ok", return_value=True):
            source = material_planner.resolve_source({"item_name": "雷光棱镜", "item_id": 9})
        self.assertEqual(source["type"], material_planner.TASK_BOSS)
        self.assertEqual(source["route"], "无相之雷")

    def test_world_boss_without_script_stays_waiting(self):
        self.family_mock.return_value = self._family(name="无相之风", type_="world_boss", route="",
                                                     materials=(("自在松石", 30, 1),))
        with patch.object(material_planner, "_boss_target_ok", return_value=False):
            source = material_planner.resolve_source({"item_name": "自在松石", "item_id": 10})
        self.assertEqual(source["type"], material_planner.TASK_WAITING_ROUTE)
        self.assertIn("首领", source["reason"])


class TalentBookTests(unittest.TestCase):
    """天赋书：同系列补日程 + 查不到日程时把出处说清楚。"""

    def test_sibling_schedule_fills_missing_tier(self):
        index = {"「正义」的指引": "【苍白的遗荣（周二/五/日）】精通秘境：箴铭Ⅱ/Ⅲ/Ⅳ"}
        self.assertEqual(material_planner._sibling_schedule("「正义」的教导", index),
                         index["「正义」的指引"])

    def test_sibling_schedule_prefers_own_entry(self):
        index = {"「正义」的教导": "自己的", "「正义」的指引": "兄弟的"}
        self.assertEqual(material_planner._sibling_schedule("「正义」的教导", index), "兄弟的")

    def test_sibling_schedule_ignores_plain_materials(self):
        self.assertEqual(material_planner._sibling_schedule("落落莓", {"落落莓": "x"}), "")

    def test_talent_source_names_domain_when_schedule_unknown(self):
        with patch.object(material_planner.material_family, "knowledge_slot",
                          return_value={"channel": "talent", "source_text": "精通秘境：隐修"}):
            source = material_planner.resolve_source({"item_name": "「坚忍」的教导", "item_id": 3})
        self.assertEqual(source["type"], material_planner.TASK_WAITING_ROUTE)   # 不猜日程，不放行
        self.assertIn("隐修", source["reason"])

    def test_talent_source_names_limited_event(self):
        with patch.object(material_planner.material_family, "knowledge_slot",
                          return_value={"channel": "talent", "source_text": "限时活动奖励"}):
            source = material_planner.resolve_source({"item_name": "智识之冕", "item_id": 4})
        self.assertEqual(source["type"], material_planner.TASK_WAITING_ROUTE)
        self.assertIn("限时活动", source["reason"])

    def test_talent_source_silent_without_slot(self):
        with patch.object(material_planner.material_family, "knowledge_slot", return_value=None):
            self.assertIsNone(material_planner._talent_source("随便什么"))


class GenshinDbTests(unittest.TestCase):
    """genshin-db 带来的三样东西：权威秘境日程、族群成员怪物、交叉校验。"""

    def setUp(self):
        if not material_family.available():
            self.skipTest("memory/game_knowledge/material_families.json 还没生成")

    def test_talent_domain_has_open_days(self):
        slot = material_family.talent_domain("「正义」的教导")
        self.assertIsNotNone(slot, "genshin-db 应该知道「正义」的教导出自哪个秘境")
        self.assertEqual(slot["entrance"], "苍白的遗荣")
        self.assertEqual(slot["stage"], "箴铭")
        self.assertEqual(slot["days"], ("二", "五", "日"))
        self.assertEqual(slot["origin"], "genshin-db")

    def test_talent_domain_unknown_material(self):
        self.assertIsNone(material_family.talent_domain("异色结晶石"))   # 不是天赋书
        self.assertIsNone(material_family.talent_domain("根本不存在的书"))

    def test_planner_uses_authoritative_schedule(self):
        source = material_planner._genshin_db_domain("「正义」的教导")
        self.assertIsNotNone(source)
        self.assertEqual(source["type"], material_planner.TASK_DOMAIN)
        self.assertEqual(source["route"], "苍白的遗荣")          # 用入口名，不是关卡名
        self.assertEqual(source["open_days"], ("二", "五", "日"))
        self.assertEqual(source["domain_index"], "2")            # 周二/五/日 → 2
        self.assertIn("genshin-db", source["reason"])

    def test_family_member_enemies(self):
        # §8 的 enemy_families：重甲蟹 / 膨膨兽 掉的是同一套三档材料
        enemies = material_family.get_family_enemies("原海异种")
        names = [item["name"] for item in enemies]
        self.assertIn("重甲蟹", names)
        self.assertIn("膨膨兽", names)
        crab = [item for item in enemies if item["name"] == "重甲蟹"][0]
        self.assertIn("异色结晶石", crab["drops"])

    def test_cross_check_has_no_conflict(self):
        # 数据完整性守卫：genshin-db 的来源文本与 mapping.json 推导必须一致，
        # 哪天重跑构建脚本把族群推歪了，这个测试会立刻炸。
        check = material_family.cross_check()
        self.assertGreater(check.get("agree", 0), 100)
        self.assertEqual(check.get("conflict_count"), 0,
                         f"与 genshin-db 的族群推导出现冲突：{check.get('conflicts')}")


class GenshinDbBuilderTests(unittest.TestCase):
    """构建脚本里的 genshin-db 处理（用合成数据，不碰真包）。"""

    def setUp(self):
        import sys
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import build_material_families as builder
        self.builder = builder

    def test_build_talent_domains_keeps_days_and_strips_prefix(self):
        gdb = {"domains": [
            {"stage": "精通秘境：箴铭 IV", "entrance": "苍白的遗荣", "region": "枫丹",
             "domain_text": "天赋培养素材", "days": ["周二", "周五", "周日"],
             "rewards": ["摩拉", "「正义」的哲学", "「正义」的指引", "「正义」的教导"]},
            {"stage": "祝圣秘境：椛狩 I", "entrance": "椛染之庭", "days": ["周一"],
             "rewards": ["绝缘之旗印"]},                       # 圣遗物本：没有天赋书
            {"stage": "精通秘境：隐修 IV", "entrance": "某入口", "days": [],
             "rewards": ["「坚忍」的教导"]},                     # 没日程的不要
        ]}
        out = self.builder.build_talent_domains(gdb)
        self.assertEqual(set(out), {"「正义」的教导", "「正义」的指引", "「正义」的哲学"})
        slot = out["「正义」的教导"]
        self.assertEqual(slot["stage"], "箴铭")                 # 前缀与档位数字都清掉
        self.assertEqual(slot["entrance"], "苍白的遗荣")
        self.assertEqual(set(slot["days"]), {"二", "五", "日"})   # 汉字口径，和 business_weekday 一致

    def test_attach_enemy_families(self):
        families = {"测试族": {"name": "测试族", "materials": []}}
        materials = {"低档料": {"family": "测试族"}, "高档料": {"family": "测试族"}}
        gdb = {"enemies": {
            "某怪": {"name": "某怪", "category_text": "异种魔兽", "enemy_type": "NORMAL",
                     "drops": [{"name": "低档料", "rate": 0.5}, {"name": "高档料", "rate": 0.1},
                               {"name": "无关材料", "rate": 1}]},
            "空掉落怪": {"name": "空掉落怪", "drops": []},
        }}
        attached = self.builder.attach_enemy_families(families, materials, gdb)
        self.assertEqual(attached, 1)
        members = families["测试族"]["enemies"]
        self.assertEqual([m["name"] for m in members], ["某怪"])
        self.assertEqual(members[0]["drops"], ["低档料", "高档料"])   # 只留属于本族的
        self.assertEqual(families["测试族"]["enemy_count"], 1)

    def test_cross_check_reports_conflicts(self):
        materials = {"料A": {"family": "族甲"}, "料B": {"family": "族乙"}}
        gdb = {"materials": {
            "料A": {"sources": ["10级以上族甲掉落"]},
            "料B": {"sources": ["20级以上别的族掉落"]},            # 冲突
            "料C": {"sources": ["30级以上族丙掉落"]},              # 我们这边没有
            "料D": {"sources": ["钓鱼获得"]},                     # 解析不出族群
        }}
        check = self.builder.cross_check_families(materials, gdb)
        self.assertEqual(check["agree"], 1)
        self.assertEqual(check["conflict_count"], 1)
        self.assertEqual(check["conflicts"][0]["material"], "料B")
        self.assertEqual(check["only_in_genshin_db_count"], 1)


class BuildTasksFamilyTests(unittest.TestCase):
    """同族多档材料：落在同一条路线上，下发指令只跑一次。"""

    def setUp(self):
        self.family_patch = patch.object(
            material_planner.material_family, "resolve_material_family"
        )
        self.family_mock = self.family_patch.start()
        self.addCleanup(self.family_patch.stop)
        self.family_mock.return_value = material_family.MaterialFamily(
            id="原海异种", name="原海异种", type="enemy_drop", channel="mob",
            route="原海异种", route_files=54,
            materials=(("异海凝珠", 0, 1), ("异海之块", 40, 2), ("异色结晶石", 60, 3)),
        )
        self.route_patch = patch.object(material_planner.material_family,
                                        "route_available", return_value=True)
        self.route_patch.start()
        self.addCleanup(self.route_patch.stop)

    def test_same_family_shares_one_route(self):
        rows = [_row([_requirement(1, "异海凝珠", 10, 4),
                      _requirement(2, "异海之块", 20, 6),
                      _requirement(3, "异色结晶石", 30, 9)])]
        tasks = material_planner.build_tasks(rows)
        self.assertEqual(len(tasks), 3)                       # 明细表仍然一条材料一行
        self.assertEqual({task["route"] for task in tasks}, {"原海异种"})
        self.assertEqual({task["family"] for task in tasks}, {"原海异种"})

    def test_command_dedupes_same_route(self):
        rows = [_row([_requirement(1, "异海凝珠", 10, 4),
                      _requirement(2, "异海之块", 20, 6),
                      _requirement(3, "异色结晶石", 30, 9)])]
        tasks = material_planner.build_tasks(rows)
        command = material_planner.tasks_to_bgi_command(tasks)
        self.assertEqual(command["free_task"], [{"action": "hunt", "target": "原海异种"}])

    def test_different_routes_are_kept(self):
        rows = [_row([_requirement(1, "异海凝珠", 10, 4)])]
        tasks = material_planner.build_tasks(rows)
        other = dict(tasks[0])
        other.update({"item_id": 99, "material": "落落莓", "route": "落落莓", "family": ""})
        command = material_planner.tasks_to_bgi_command(tasks + [other])
        self.assertEqual(len(command["free_task"]), 2)


if __name__ == "__main__":
    unittest.main()
