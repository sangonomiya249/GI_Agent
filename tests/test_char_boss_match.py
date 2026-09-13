import json
import tempfile
import unittest
from pathlib import Path

from skills.boss_pathing_guard import ConfigTransactionError
from skills.char_boss_match import (
    KIND_BOSS,
    KIND_UNDETERMINED,
    KIND_UNSUPPORTED,
    find_character,
    resolve_character_boss,
    resolve_boss_target,
)


class CharacterBossMatchTests(unittest.TestCase):
    """回归测试：LLM 不知道角色突破 Boss 时按元素属性瞎猜（蓝砚 → 无相之风）。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        # 模拟 BetterGI 里的脚本目录
        self.script_dir = self.root / "批量讨伐角色养成材料BOSS"
        pathing_dir = self.script_dir / "assets" / "Pathing"
        config_dir = self.script_dir / "assets" / "config"
        pathing_dir.mkdir(parents=True)
        config_dir.mkdir(parents=True)
        for filename in (
            "秘源机兵·构型械前往.json",
            "急冻树前往.json",
            "无相之雷前往.json",
            "歌裴莉娅的葬送前往.json",
        ):
            (pathing_dir / filename).write_text('{"positions": []}', encoding="utf-8")
        (config_dir / "boss-list.json").write_text(
            json.dumps(
                {
                    "bossList": {
                        "蒙德": ["急冻树", "无相之雷", "无相之风（不支持）"],
                        "须弥": ["秘源机兵·构型械"],
                        "枫丹": ["歌裴莉娅的葬送"],
                    },
                    "unsupportedBosses": ["无相之风", "无相之冰"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # 模拟角色突破材料字典
        self.avatar_dict = self.root / "game_dict_baike_full.json"
        self.avatar_dict.write_text(
            json.dumps(
                {
                    "avatars": {
                        "lan-yan": {
                            "name_zh": "蓝砚",
                            "materials": ["清水玉", "自在松石", "原素花蜜", "秘刻金纹的源核"],
                        },
                        "venti": {
                            "name_zh": "温迪",
                            "materials": ["塞西莉亚花", "自在松石", "史莱姆原浆", "飓风之种"],
                        },
                        "chiori": {
                            "name_zh": "千织",
                            "materials": ["血斛", "坚牢黄玉", "浮游晶化核", "奇械发条备件·歌裴莉娅"],
                        },
                        "traveler": {
                            "name_zh": "旅行者",
                            "materials": ["风车菊", "璀璨原钻", "不祥的面具"],
                        },
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        # 模拟敌首掉落字典（含杂兵名，用来验证判别式能不能收敛）
        self.drops_dict = self.root / "boss_drops_dict.json"
        self.drops_dict.write_text(
            json.dumps(
                {
                    "秘源机兵·构型械": ["秘刻金纹的源核", "最胜紫晶", "摩拉"],
                    "无相之风 贝特": ["飓风之种", "自在松石", "摩拉"],
                    "无相之雷 阿莱夫": ["自在松石", "摩拉"],
                    "急冻树": ["自在松石", "摩拉"],
                    "丘丘人": ["摩拉", "不祥的面具"],
                    "骗骗花": ["原素花蜜"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _resolve(self, raw):
        return resolve_boss_target(
            raw,
            script_dir=self.script_dir,
            avatar_dict_path=str(self.avatar_dict),
            drops_path=str(self.drops_dict),
        )

    def test_resolves_character_to_official_boss_name(self):
        """蓝砚 的突破 Boss 是秘源机兵·构型械，不是按风元素猜的无相之风。"""
        name, notices = self._resolve("蓝砚")

        self.assertEqual(name, "秘源机兵·构型械")
        self.assertTrue(any("角色解析" in notice for notice in notices))

    def test_resolves_character_name_inside_natural_language(self):
        name, _ = self._resolve("给蓝砚打点突破材料")

        self.assertEqual(name, "秘源机兵·构型械")

    def test_gem_material_does_not_pollute_the_verdict(self):
        """自在松石 同时挂在多个首领名下，不能因此判成歧义。"""
        result = resolve_character_boss(
            "蓝砚", script_dir=self.script_dir, avatar_dict_path=str(self.avatar_dict),
            drops_path=str(self.drops_dict),
        )

        self.assertEqual(result.kind, KIND_BOSS)
        self.assertEqual(result.material, "秘刻金纹的源核")

    def test_character_whose_boss_is_unsupported_is_reported_clearly(self):
        """温迪的突破 Boss 是无相之风 → 必须明确说"脚本不支持"，而不是猜成别的。"""
        with self.assertRaises(ConfigTransactionError) as ctx:
            self._resolve("温迪")

        message = str(ctx.exception)
        self.assertIn("无相之风", message)
        self.assertIn("不支持", message)

    def test_material_name_carrying_boss_name_resolves(self):
        """枫丹「冰风组曲」材料在掉落字典里是一对多，靠材料名里的首领名收敛。"""
        result = resolve_character_boss(
            "千织", script_dir=self.script_dir, avatar_dict_path=str(self.avatar_dict),
            drops_path=str(self.drops_dict),
        )

        self.assertEqual(result.kind, KIND_BOSS)
        self.assertEqual(result.boss, "歌裴莉娅的葬送")

    def test_character_without_boss_material_asks_instead_of_guessing(self):
        result = resolve_character_boss(
            "旅行者", script_dir=self.script_dir, avatar_dict_path=str(self.avatar_dict),
            drops_path=str(self.drops_dict),
        )

        self.assertEqual(result.kind, KIND_UNDETERMINED)
        self.assertIn("不需要首领材料", result.reason)

        with self.assertRaises(ConfigTransactionError) as ctx:
            self._resolve("旅行者")
        self.assertIn("不要按元素属性推断", str(ctx.exception))

    def test_material_input_resolves_to_boss(self):
        name, notices = self._resolve("秘刻金纹的源核")

        self.assertEqual(name, "秘源机兵·构型械")
        self.assertTrue(any("材料解析" in notice for notice in notices))

    def test_plain_boss_name_still_goes_through_the_name_guard(self):
        name, notices = self._resolve("急冻树")

        self.assertEqual(name, "急冻树")
        self.assertEqual(notices, [])

    def test_find_character_returns_none_for_unknown_names(self):
        self.assertIsNone(find_character("并不存在的角色", str(self.avatar_dict)))
        self.assertEqual(find_character("蓝砚的突破Boss", str(self.avatar_dict)), "蓝砚")


class EnvReaderBossNameTests(unittest.TestCase):
    """展柜上下文里的 boss_name 必须正确 —— 错误的上下文会诱导 LLM 按元素猜 Boss。"""

    @classmethod
    def setUpClass(cls):
        if not Path("memory/game_dict_baike_full.json").exists():
            raise unittest.SkipTest("缺少 memory/game_dict_baike_full.json")
        from skills.env_reader import load_yatta_dict_safely

        cls.table = load_yatta_dict_safely()
        cls.name_to_id = {
            name: avatar_id
            for avatar_id, name in json.loads(
                Path("memory/avatar_dict.json").read_text(encoding="utf-8")
            ).items()
        }

    def test_boss_name_is_derived_from_the_boss_material(self):
        # 这些角色以前拿到的都是错的（莫娜→无相之风、行秋→秘源机兵·构型械、丽莎→风蚀沙虫）
        expected = {
            "莫娜": "纯水精灵",
            "行秋": "纯水精灵",
            "丽莎": "无相之雷",
            "埃洛伊": "无相之冰",
            "蓝砚": "秘源机兵·构型械",
            "千织": "歌裴莉娅的葬送",
        }
        for name, boss in expected.items():
            entry = self.table.get(self.name_to_id.get(name), {})
            self.assertEqual(entry.get("boss_name"), boss, f"{name} 的展柜 Boss 名不对")

    def test_every_resolvable_character_agrees_with_the_resolver(self):
        mismatches = []
        for name, avatar_id in self.name_to_id.items():
            entry = self.table.get(avatar_id) or {}
            reported = entry.get("boss_name")
            if not reported:
                continue
            result = resolve_character_boss(name)
            truth = result.boss if result.kind in ("boss", "unsupported") else None
            if truth and reported != truth:
                mismatches.append((name, reported, truth))

        self.assertEqual(mismatches, [])


if __name__ == "__main__":
    unittest.main()
