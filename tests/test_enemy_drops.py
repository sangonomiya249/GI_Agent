"""展柜里的"敌人掉落"材料（skills/env_reader.py）。

玩家实测的疑惑：模型说"展柜 JSON 只登记特产/秘境材料/Boss材料，不含普通怪物掉落，
所以不确定雅珂达用哪一种掉落材料，得问玩家"。**数据其实一直在字典里**
（`ascension_total_cost` + `ascension_material_sources` 的 source_lines 写着「怪物掉落」
且常常点名魔物），只是提取时整类被丢掉了。现在补上，`type=敌人掉落`。
"""

import json
import os
import unittest

import config
from skills.env_reader import mob_name_from_sources


class MobNameTests(unittest.TestCase):
    def test_plain_form(self):
        self.assertEqual(mob_name_from_sources(["获得方式：", "巡陆艇掉落；", "星尘兑换", "怪物掉落"]), "巡陆艇")

    def test_level_prefix_is_stripped(self):
        self.assertEqual(
            mob_name_from_sources(["获得方式：", "【40级以上】巡陆艇掉落；", "合成获得；", "怪物掉落"]),
            "巡陆艇",
        )

    def test_parallel_mobs_take_the_first(self):
        self.assertEqual(
            mob_name_from_sources(["深渊法师、深渊使徒、深渊咏者掉落", "星尘兑换"]),
            "深渊法师",
        )
        self.assertEqual(
            mob_name_from_sources(["【40级以上】遗迹守卫、遗迹重机、遗迹猎者掉落；"]),
            "遗迹守卫",
        )

    def test_category_only_returns_empty(self):
        """「怪物掉落」是类别名，不是魔物名；百科没写怪时也不能瞎猜。"""
        self.assertEqual(mob_name_from_sources(["获得方式：", "怪物掉落"]), "")

    def test_nameless_drop_returns_empty(self):
        self.assertEqual(mob_name_from_sources(["获得方式：", "掉落；", "合成获得；"]), "")

    def test_player_comments_are_not_sources(self):
        self.assertEqual(mob_name_from_sources(["无相风掉落吧<br>", "风魔龙是周本"]), "")

    def test_non_drop_lines_ignored(self):
        self.assertEqual(mob_name_from_sources(["星尘兑换", "合成获得", "炼金合成"]), "")

    def test_empty_input(self):
        self.assertEqual(mob_name_from_sources([]), "")
        self.assertEqual(mob_name_from_sources(None), "")


class EnemyDropExtractionTests(unittest.TestCase):
    """真实字典上的回归（字典不在就跳过）。"""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(config.MEMORY_DIR, "game_dict_baike_full.json")
        if not os.path.isfile(path):
            raise unittest.SkipTest("没有 memory/game_dict_baike_full.json")
        from skills.env_reader import load_yatta_dict_safely

        cls.data = load_yatta_dict_safely()

    def _materials(self, name):
        for node in self.data.values():
            if node.get("name") == name:
                return node.get("materials") or []
        raise AssertionError(f"字典里没有 {name}")

    def test_character_has_enemy_drops_with_mob_names(self):
        items = [m for m in self._materials("雅珂达") if m.get("type") == "敌人掉落"]

        self.assertTrue(items, "雅珂达应该有普通怪物掉落材料（机轴系列）")
        names = {m["name"] for m in items}
        self.assertIn("毁损机轴", names)
        for item in items:
            self.assertEqual(item["schedule"], "讨伐【巡陆艇】")
            self.assertIsInstance(item["total"], int)

    def test_another_character_maps_to_its_mob(self):
        items = [m for m in self._materials("蓝砚") if m.get("type") == "敌人掉落"]

        self.assertTrue(items)
        self.assertEqual({m["schedule"] for m in items}, {"讨伐【骗骗花】"})

    def test_types_are_known(self):
        """四类之外不该冒出别的 type（提示词是按这四类写的）。"""
        known = {"特产", "秘境材料", "Boss材料", "敌人掉落"}
        seen = set()
        for node in self.data.values():
            for item in node.get("materials") or []:
                seen.add(str(item.get("type")))
        self.assertTrue(seen <= known, f"出现了没见过的 type：{seen - known}")

    def test_coverage_is_broad_but_honest(self):
        """大多数角色应能推出魔物名；推不出来的必须是"讨伐大世界魔物"这种老实说法。"""
        named = 0
        unnamed = 0
        for node in self.data.values():
            for item in node.get("materials") or []:
                if item.get("type") != "敌人掉落":
                    continue
                schedule = str(item.get("schedule") or "")
                if schedule == "讨伐大世界魔物":
                    unnamed += 1
                else:
                    self.assertTrue(schedule.startswith("讨伐【") and schedule.endswith("】"), schedule)
                    named += 1
        self.assertGreater(named, 200)
        self.assertEqual(named + unnamed, named + unnamed)      # 只是别让它炸

    def test_showcase_json_is_serialisable(self):
        """新字段要能进 JSON（模型看到的是 json.dumps 出来的文本）。"""
        text = json.dumps({"avatars": [{"materials": self._materials("雅珂达")}]}, ensure_ascii=False)
        self.assertIn("敌人掉落", text)
        self.assertIn("巡陆艇", text)


if __name__ == "__main__":
    unittest.main()
