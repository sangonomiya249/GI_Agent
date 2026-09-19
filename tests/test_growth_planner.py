"""养成规划（brain/growth_planner.py + brain/material_planner.py）的测试。

这一组用例直接对着规格书 §35 的清单写，尤其是那几条"错了会悄悄规划错"的：

  · 81 → 90 必须合法（不能只支持 80/90）；
  · 天赋三项目标各自独立（1/1/1 → 10/9/8）；
  · 角色顺序：默认 5 星优先，用户可以覆盖；
  · 阶段优先级：角色>武器>天赋，以及用户改成 天赋>角色>武器 / 武器>天赋>角色；
  · 两个角色共享同一种材料时缺口**不能重复计算**（§17）；
  · 当前角色材料不足时**不能跳到下一角色**（§16）；
  · 当前角色三阶段全部完成后才能进入下一个角色；
  · 路线冷却 / 秘境今天不开 / 路线不存在时的处理；
  · BetterGI 执行失败不污染库存；执行后必须重新同步；
  · 旧库存不会被误认为实时库存。

全部打桩：不联网、不碰玩家的 memory/。
"""

import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from brain import growth_db, growth_models, growth_planner, material_planner
from skills import mys_api, mys_calculator, mys_inventory


# ==========================================
# 🌟 假数据：米游社档案 / 材料需求
# ==========================================

MORA = {"item_id": 104001, "item_name": "摩拉", "category": "currency"}
EXP_BOOK = {"item_id": 202001, "item_name": "大英雄的经验", "category": "exp_book"}
QINGXIN = {"item_id": 100092, "item_name": "清心", "category": "specialty"}
TALENT_BOOK = {"item_id": 104301, "item_name": "「诗文」的哲学", "category": "talent_book"}
WEAPON_MAT = {"item_id": 114001, "item_name": "狮牙斗士的镣铐", "category": "weapon_ascension"}


def requirement(spec, count, phases):
    row = dict(spec)
    row["required"] = count
    row["phases"] = list(phases)
    row["name"] = spec["item_name"]
    return row


def avatar(character_id, name, level, rarity, skills=None, weapon=None):
    """一条"米游社养成计算器角色档案"。

    ⚠️ 天赋字段名必须是 `talents`（`{技能id: 等级}`）—— 那是 `growth_models.current_levels()`
    认的形状；写成 `skills` 会被当成"这个角色没有天赋数据"，`is_complete()` 于是永远判"没完成"。
    """
    return {
        "id": character_id, "name": name, "level": level, "rarity": rarity, "element": "Pyro",
        "talents": skills if skills is not None else {"10891": 8, "10892": 8, "10895": 8},
        "skills": skills if skills is not None else {"10891": 8, "10892": 8, "10895": 8},
        "weapon": weapon if weapon is not None else {"id": 12512, "name": "护摩之杖", "level": 80},
    }


def requirements_by_phase(rows):
    """`[{item..., phases: [...]}]` → `{phase: [...]}`（形状对齐 mys_calculator）. """
    by_phase = {phase: [] for phase in growth_models.PHASES}
    for row in rows:
        for phase in row["phases"]:
            by_phase[phase].append(row)
    return by_phase


def inventory(items):
    """`{item_id: count}` → 内部库存模型（**时间戳是刚刚**，所以不会触发"过期"标记）。"""
    model = mys_inventory.empty_inventory(uid="100000001", server="cn_gf01")
    model["fetched_at"] = datetime.datetime.now().strftime(mys_inventory._TIME_FORMAT)
    model["items"] = {
        mys_inventory.item_key(item_id): mys_inventory.make_item(
            item_id, name={"104001": "摩拉", "100092": "清心"}.get(str(item_id), ""), count=count
        )
        for item_id, count in items.items()
    }
    return model


class PlannerCase(unittest.TestCase):
    """把数据库指到临时目录，并给 build_plan 打上"米游社档案 + 材料需求"的桩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.db = os.path.join(self.root, "growth.db")
        growth_db.close(self.db)
        self.patches = [
            patch.object(config, "GROWTH_DB_PATH", self.db),
            patch.object(config, "GROWTH_MYS_DIR", self.root),
            patch.object(config, "MYS_UID", ""),
            patch.object(config, "DEFAULT_UID", "100000001"),
            patch.object(config, "GROWTH_COMPUTE_MAX_PER_PLAN", 20),
            # ★ 体力相关的两件事必须固定，否则测试会跟着**玩家自己的配置**变：
            #   · `DOMAIN_RESIN_PREFERENCE`（20/40/浓缩）决定一次秘境要多少体力；
            #   · 玩家在界面上记过的"当前体力"存在真实数据库里，会让趟数被体力卡住。
            #   两个都会让"应该有一个 energy_task"这类断言时通时不通（踩过）。
            patch.object(config, "DOMAIN_RESIN_PREFERENCE", "原粹树脂20"),
            patch("skills.mys_resin.probe",
                  return_value={"available": False, "current": 0, "max": 0,
                                "reason": "测试里固定读不到体力"}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        growth_planner.invalidate_cache()
        self.addCleanup(growth_planner.invalidate_cache)

        self.avatars = {}
        self.requirements = {}

    def tearDown(self):
        """**必须在这里显式关连接**：`addCleanup` 与 `TemporaryDirectory.cleanup` 的执行
        顺序在 Windows 上会让 sqlite 还握着库文件，删临时目录直接 PermissionError
        （整个用例会被判失败，而失败原因和被测逻辑毫无关系）。"""
        growth_db.close(self.db)
        self.tmp.cleanup()

    def add_character(self, character_id, name, level, rarity, **target):
        growth_db.upsert_character(character_id, name, rarity, "Pyro", level, path=self.db)
        values = {"level_target": 90, "normal_target": 10, "skill_target": 10, "burst_target": 10}
        values.update(target)
        # 和生产路径一致：目标值先过一遍 normalise_target_values（1~90 / 1~10 / 优先级 1~3）
        return growth_db.save_target(
            character_id, growth_models.normalise_target_values(values), path=self.db
        )

    def set_avatars(self, *rows):
        """把"米游社角色档案"打桩成固定内容（`_cached_avatars()` 返回 `{id: 档案}`）。

        ⚠️ `build_plan` **只读内存缓存**（`_cached_avatars()`），不再调 `fetch_avatars()`
        —— 战绩接口被风控卡着，规划一次不该去打它。所以这里必须打桩
        `_cached_avatars`；只打桩 `fetch_avatars` 的话缓存永远是空的，
        当前状态会退回本地库（表现：天赋全 0、已完成角色被判成没完成）。
        `fetch_avatars` 也一起打上，别的路径仍可能问它。
        """
        for row in rows:
            self.avatars[row["id"]] = row
        for name, fake in (("_cached_avatars", lambda: dict(self.avatars)),
                           ("fetch_avatars", lambda *a, **k: self.avatars)):
            patcher = patch.object(growth_planner, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def set_requirements(self, character_id, rows):
        """给某个角色设定"养成计算器返回的材料需求"。

        ⚠️ 必须用 `new=` 直接换掉函数（不能 `patch(..., side_effect=...)` ——
        那种写法在同一个 patch 里给不出 `return_value` 时拿到的是 MagicMock 本身，
        测试会一直看到空需求，然后以"缺口全是 0"的样子假通过）。
        """
        self.requirements[character_id] = requirements_by_phase(rows)
        original = growth_planner.requirements_for

        def fake(target, avatar=None, force=False, uid=None, server=None,
                 allow_network=True):
            cid = int((target or {}).get("character_id") or 0)
            if cid in self.requirements:
                return {"character_id": cid, "requirements": [], "by_phase": self.requirements[cid],
                        "error": "", "error_kind": ""}
            return original(target, avatar=avatar, force=force, uid=uid, server=server)

        patcher = patch.object(growth_planner, "requirements_for", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, **kwargs):
        """调真正的 `build_plan`，但把所有路径都钉在临时目录上。

        ⚠️ `growth_db` 的函数**不认识 config 补丁**（它们接受显式 path 参数），
        所以这里必须把 `path` / `db_path` 一起传下去 —— 否则规划会去读玩家真实的
        `memory/growth.db`（表现是"测试里一个角色都没有"，而且会写坏玩家的库）。
        """
        kwargs.setdefault("path", self.db)
        kwargs.setdefault("db_path", self.db)
        kwargs.setdefault("record", False)
        return growth_planner.build_plan(**kwargs)


# ==========================================
# 🌟 目标校验（§10 / §11 / §12）
# ==========================================


class TargetValueTests(PlannerCase):
    def test_level_target_accepts_any_value_between_1_and_90(self):
        """规格书 §10：必须支持 81、83 这种任意值，不能只给 80/90 两个选项。"""
        for level in (1, 2, 80, 81, 82, 89, 90):
            target = self.add_character(10000089, "胡桃", 80, 5, level_target=level)
            self.assertEqual(target["level_target"], level)

    def test_level_target_is_clamped(self):
        self.assertEqual(self.add_character(1, "A", 1, 5, level_target=0)["level_target"], 1)
        self.assertEqual(self.add_character(2, "B", 1, 5, level_target=999)["level_target"], 90)
        self.assertEqual(self.add_character(3, "C", 1, 5, level_target="坏值")["level_target"], 90)

    def test_three_talents_are_independent(self):
        """规格书 §11：10/9/8 和 1/10/10 都必须能存下来。"""
        target = self.add_character(10000089, "胡桃", 80, 5,
                                    normal_target=10, skill_target=9, burst_target=8)
        self.assertEqual(
            (target["normal_target"], target["skill_target"], target["burst_target"]), (10, 9, 8)
        )

        target = growth_db.save_target(
            10000089, {"normal_target": 1, "skill_target": 10, "burst_target": 10}, path=self.db
        )
        self.assertEqual(
            (target["normal_target"], target["skill_target"], target["burst_target"]), (1, 10, 10)
        )

    def test_talent_targets_are_clamped_to_1_10(self):
        target = self.add_character(10000089, "胡桃", 80, 5,
                                    normal_target=0, skill_target=99, burst_target=-5)
        self.assertEqual(
            (target["normal_target"], target["skill_target"], target["burst_target"]), (1, 10, 1)
        )

    def test_weapon_target_is_clamped_like_level(self):
        target = self.add_character(10000089, "胡桃", 80, 5,
                                    weapon_enabled=1, weapon_level_target=81)
        self.assertTrue(target["weapon_enabled"])
        self.assertEqual(target["weapon_level_target"], 81)

    def test_priority_fields_are_clamped_to_1_3(self):
        target = self.add_character(10000089, "胡桃", 80, 5,
                                    level_priority=0, weapon_priority=9, talent_priority=-1)
        self.assertEqual(
            (target["level_priority"], target["weapon_priority"], target["talent_priority"]),
            (1, 3, 1),
        )

    def test_default_phase_priority_is_level_then_weapon_then_talent(self):
        target = self.add_character(10000089, "胡桃", 80, 5)
        self.assertEqual(growth_models.ordered_phases(target),
                         ["character_level", "weapon_level", "talent"])


# ==========================================
# 🌟 角色排序（§14 / §15）
# ==========================================


class OrderingTests(PlannerCase):
    def test_default_order_is_five_star_first(self):
        self.add_character(10000032, "班尼特", 70, 4, priority=3)
        self.add_character(10000089, "胡桃", 70, 5, priority=3)
        self.add_character(10000102, "钟离", 70, 5, priority=3)

        names = [row["character_name"] for row in growth_planner.ordered_targets(path=self.db)]

        self.assertEqual(names, ["胡桃", "钟离", "班尼特"])

    def test_user_priority_overrides_rarity(self):
        """规格书 §15：用户手动把 4 星排到第一位，就要真的排第一位。"""
        self.add_character(10000032, "班尼特", 70, 4, priority=1)
        self.add_character(10000089, "胡桃", 70, 5, priority=2)

        names = [row["character_name"] for row in growth_planner.ordered_targets(path=self.db)]

        self.assertEqual(names, ["班尼特", "胡桃"])

    def test_reorder_writes_priority_and_switches_to_custom_mode(self):
        self.add_character(10000089, "胡桃", 70, 5)
        self.add_character(10000032, "班尼特", 70, 4)

        growth_db.reorder_targets([10000032, 10000089], path=self.db)
        growth_planner.set_sort_mode(growth_models.SORT_MODE_CUSTOM, path=self.db)

        names = [row["character_name"] for row in growth_planner.ordered_targets(path=self.db)]
        self.assertEqual(names, ["班尼特", "胡桃"])
        self.assertEqual(growth_planner.sort_mode(path=self.db), "custom")

    def test_disabled_characters_are_excluded_from_ordering(self):
        self.add_character(10000089, "胡桃", 70, 5, enabled=0)
        self.add_character(10000032, "班尼特", 70, 4)

        names = [row["character_name"] for row in growth_planner.ordered_targets(path=self.db)]

        self.assertEqual(names, ["班尼特"])

    def test_sort_targets_is_stable_for_ties(self):
        rows = [
            {"character_id": 3, "priority": 1, "rarity": 5},
            {"character_id": 1, "priority": 1, "rarity": 5},
            {"character_id": 2, "priority": 1, "rarity": 5},
        ]

        ids = [row["character_id"] for row in growth_models.sort_targets(rows)]

        self.assertEqual(ids, [1, 2, 3])


# ==========================================
# 🌟 阶段优先级（§13）
# ==========================================


class PhasePriorityTests(PlannerCase):
    def test_level_then_weapon_then_talent(self):
        target = self.add_character(10000089, "胡桃", 80, 5,
                                    level_priority=1, weapon_priority=2, talent_priority=3)
        self.assertEqual(growth_models.ordered_phases(target),
                         ["character_level", "weapon_level", "talent"])

    def test_talent_then_level_then_weapon(self):
        target = self.add_character(10000089, "胡桃", 80, 5,
                                    level_priority=2, weapon_priority=3, talent_priority=1)
        self.assertEqual(growth_models.ordered_phases(target),
                         ["talent", "character_level", "weapon_level"])

    def test_weapon_then_talent_then_level(self):
        target = self.add_character(10000089, "胡桃", 80, 5,
                                    level_priority=3, weapon_priority=1, talent_priority=2)
        self.assertEqual(growth_models.ordered_phases(target),
                         ["weapon_level", "talent", "character_level"])

    def test_ties_are_stable_not_random(self):
        """两个阶段都是 1 时按固定顺序（角色→武器→天赋），不能每次规划都换顺序。"""
        target = {"level_priority": 1, "weapon_priority": 1, "talent_priority": 3}

        self.assertEqual(growth_models.ordered_phases(target),
                         ["character_level", "weapon_level", "talent"])
        self.assertEqual(growth_models.ordered_phases(target),
                         ["character_level", "weapon_level", "talent"])

    def test_plan_phases_follow_the_users_priority(self):
        self.add_character(10000089, "胡桃", 80, 5,
                           level_priority=2, weapon_priority=3, talent_priority=1,
                           weapon_enabled=1)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [
            requirement(TALENT_BOOK, 38, ["talent"]),
            requirement(MORA, 1000, ["character_level"]),
            requirement(WEAPON_MAT, 6, ["weapon_level"]),
        ])

        plan = self.build()

        self.assertEqual(plan["current_phase"], "talent")


# ==========================================
# 🌟 材料缺口（§17 共享池）
# ==========================================


class MaterialGapTests(PlannerCase):
    def test_missing_is_required_minus_owned(self):
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(QINGXIN, 80, ["character_level"])])

        plan = self.build(inventory=inventory({100092: 20}))
        row = next(row for row in plan["phases"] if row["phase"] == "character_level")
        material = next(item for item in row["materials"] if item["item_id"] == 100092)

        self.assertEqual((material["required"], material["owned"], material["missing"]), (80, 20, 60))

    def test_two_characters_sharing_a_material_do_not_double_count(self):
        """规格书 §17：A 要 168、B 要 168、库存 10 → 总缺口 326，**不是** 158+158。

        第一个角色的缺口是 168-10=158，库存已经被它占满，第二个角色的缺口仍是完整的 168。
        """
        self.add_character(10000089, "胡桃", 80, 5, priority=1)
        self.add_character(10000102, "钟离", 80, 5, priority=2)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5), avatar(10000102, "钟离", 80, 5))
        self.set_requirements(10000089, [requirement(QINGXIN, 168, ["character_level"])])
        self.set_requirements(10000102, [requirement(QINGXIN, 168, ["character_level"])])

        plan = self.build(inventory=inventory({100092: 10}))
        # 每个角色都有三个阶段的记录（`phases` 是全量），所以这里按 (角色, 阶段) 精确取想要的
        rows = [row for row in plan["phases"]
                if row["character_id"] in (10000089, 10000102)
                and row["phase"] == "character_level"]

        self.assertEqual(rows[0]["missing_total"], 158)     # 168 - 10
        self.assertEqual(rows[1]["missing_total"], 168)     # 库存已被第一个角色用完
        self.assertEqual(rows[0]["missing_total"] + rows[1]["missing_total"], 326)

    def test_shared_material_is_allocated_not_multiplied(self):
        """同一份库存只被扣一次：`owned` 之和不能超过真实库存。"""
        self.add_character(10000089, "胡桃", 80, 5, priority=1)
        self.add_character(10000102, "钟离", 80, 5, priority=2)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5), avatar(10000102, "钟离", 80, 5))
        self.set_requirements(10000089, [requirement(QINGXIN, 100, ["character_level"])])
        self.set_requirements(10000102, [requirement(QINGXIN, 100, ["character_level"])])

        plan = self.build(inventory=inventory({100092: 60}))
        character_level = [row for row in plan["phases"]
                           if row["phase"] == "character_level" and row["character_id"] in (10000089, 10000102)]
        owned = [row["materials"][0]["owned"] for row in character_level]
        missing = [row["materials"][0]["missing"] for row in character_level]

        self.assertEqual(owned, [60, 0])          # 库存给了第一个角色
        self.assertEqual(sum(owned), 60)          # 没有凭空复制
        self.assertEqual(missing, [40, 100])      # 缺口 = 各自的 100 - 分到的
        self.assertEqual(sum(owned) + sum(missing), 200)

    def test_completed_characters_do_not_consume_the_pool(self):
        """已经达标的角色不该占用材料（否则当前角色会凭空多出缺口）。"""
        self.add_character(10000089, "胡桃", 90, 5, priority=1)          # 已完成
        self.add_character(10000102, "钟离", 80, 5, priority=2)
        self.set_avatars(
            avatar(10000089, "胡桃", 90, 5, skills={"10891": 10, "10892": 10, "10895": 10},
                   weapon={"id": 1, "level": 90}),
            avatar(10000102, "钟离", 80, 5),
        )
        self.set_requirements(10000089, [requirement(QINGXIN, 168, ["character_level"])])
        self.set_requirements(10000102, [requirement(QINGXIN, 168, ["character_level"])])

        plan = self.build(inventory=inventory({100092: 10}))
        zhongli = next(row for row in plan["phases"] if row["character_id"] == 10000102)

        self.assertTrue(plan["characters"][0]["complete"])
        self.assertEqual(zhongli["missing_total"], 158)     # 库存没被已完成的胡桃吃掉

    def test_material_shared_across_phases_produces_one_task(self):
        """摩拉在角色等级和天赋两个阶段都缺：只出一条任务，跑一趟两边都补上。"""
        self.add_character(10000089, "胡桃", 80, 5, weapon_enabled=1)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [
            requirement(MORA, 1000, ["character_level", "talent"]),
        ])

        plan = self.build(inventory=inventory({104001: 0}))
        mora_tasks = [task for task in plan["tasks"] if task["item_id"] == 104001]

        self.assertEqual(len(mora_tasks), 1)
        self.assertEqual(len(mora_tasks[0]["phases"]), 2)

    def test_weapon_phase_is_skipped_when_not_included(self):
        self.add_character(10000089, "胡桃", 80, 5, weapon_enabled=0)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(WEAPON_MAT, 6, ["weapon_level"])])

        plan = self.build(inventory=inventory({}))
        weapon_rows = [row for row in plan["phases"] if row["phase"] == "weapon_level"]

        self.assertTrue(weapon_rows[0]["skipped"])
        self.assertNotIn("weapon_level", [task["phase"] for task in plan["tasks"]])


# ==========================================
# 🌟 单角色顺序模式（§16）
# ==========================================


class SequentialTests(PlannerCase):
    def test_only_the_current_character_gets_tasks(self):
        """规格书 §16：不能变成"所有角色先刷经验书"。"""
        self.add_character(10000089, "胡桃", 80, 5, priority=1)
        self.add_character(10000032, "班尼特", 70, 4, priority=2)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5), avatar(10000032, "班尼特", 70, 4))
        self.set_requirements(10000089, [requirement(EXP_BOOK, 48, ["character_level"])])
        self.set_requirements(10000032, [requirement(EXP_BOOK, 20, ["character_level"])])

        plan = self.build(inventory=inventory({}))

        self.assertEqual(plan["current_character_id"], 10000089)
        self.assertTrue(all(task["character_id"] == 10000089 for task in plan["tasks"]))
        self.assertFalse(plan["execution"]["fallback"])

    def test_character_moves_on_only_after_all_three_phases_done(self):
        self.add_character(10000089, "胡桃", 80, 5, priority=1, weapon_enabled=1)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        # 等级缺摩拉、天赋缺书
        self.set_requirements(10000089, [
            requirement(MORA, 1000, ["character_level"]),
            requirement(TALENT_BOOK, 38, ["talent"]),
        ])

        plan = self.build(inventory=inventory({104001: 1000}))     # 摩拉够了，天赋还缺

        self.assertEqual(plan["current_character_id"], 10000089)
        self.assertIn("talent", [task["phase"] for task in plan["tasks"]])

    def test_next_character_is_reached_when_current_is_complete(self):
        self.add_character(10000089, "胡桃", 90, 5, priority=1)        # 目标 90，已经到 90
        self.add_character(10000032, "班尼特", 70, 4, priority=2)
        self.set_avatars(
            avatar(10000089, "胡桃", 90, 5, skills={"10891": 10, "10892": 10, "10895": 10}),
            avatar(10000032, "班尼特", 70, 4),
        )
        self.set_requirements(10000089, [])                     # 胡桃没有缺口
        self.set_requirements(10000032, [requirement(EXP_BOOK, 20, ["character_level"])])

        plan = self.build(inventory=inventory({}))

        self.assertTrue(plan["characters"][0]["complete"])
        self.assertEqual(plan["current_character_id"], 10000032)
        self.assertTrue(any(task["character_id"] == 10000032 for task in plan["tasks"]))

    def test_plan_reports_complete_when_everyone_is_done(self):
        self.add_character(10000089, "胡桃", 90, 5, weapon_enabled=1)
        self.set_avatars(avatar(
            10000089, "胡桃", 90, 5,
            skills={"10891": 10, "10892": 10, "10895": 10},
            weapon={"id": 12512, "level": 90},
        ))
        self.set_requirements(10000089, [])

        plan = self.build(inventory=inventory({}))

        self.assertEqual(plan["status"], growth_models.STATE_COMPLETE)
        self.assertEqual(plan["tasks"], [])
        self.assertEqual(plan["current_character_id"], 0)
        self.assertIn("完成", plan["status_label"])

    def test_target_already_reached_does_not_ask_the_calculator(self):
        """90 → 90 这类"目标已经达成"的目标：**不问米游社**（§10 要求它合法且不报错）。"""
        self.add_character(10000089, "胡桃", 90, 5)
        self.set_avatars(avatar(10000089, "胡桃", 90, 5,
                                skills={"10891": 10, "10892": 10, "10895": 10}))

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "compute_for_target") as fake:
            plan = self.build(inventory=inventory({}))

        fake.assert_not_called()
        self.assertEqual(plan["status"], growth_models.STATE_COMPLETE)


# ==========================================
# 🌟 材料来源与可执行性（§27 / §30 / §31 / §33）
# ==========================================


class SourceTests(unittest.TestCase):
    def test_mora_and_exp_books_go_to_leylines(self):
        self.assertEqual(material_planner.resolve_source(MORA)["route"], material_planner.LEYLINE_MORA)
        self.assertEqual(material_planner.resolve_source(EXP_BOOK)["route"], material_planner.LEYLINE_EXP)

    def test_talent_book_resolves_to_a_domain_with_open_days(self):
        source = material_planner.resolve_source(TALENT_BOOK)

        self.assertEqual(source["type"], material_planner.TASK_DOMAIN)
        self.assertTrue(source["domain"])
        self.assertTrue(source["open_days"])

    def test_specialty_resolves_to_a_gather_route(self):
        source = material_planner.resolve_source(QINGXIN)

        self.assertEqual(source["type"], material_planner.TASK_GATHER)
        self.assertEqual(source["route"], "清心")

    def test_unknown_material_is_marked_waiting_route_not_guessed(self):
        """规格书 §33：缺路线时标记 WAITING_ROUTE，**不要让 LLM 猜路线**。"""
        source = material_planner.resolve_source(
            {"item_id": 0, "item_name": "", "category": "other"}
        )

        self.assertEqual(source["type"], material_planner.TASK_WAITING_ROUTE)
        self.assertTrue(source["reason"])

    def test_material_without_any_known_source_is_not_guessed_into_an_action(self):
        """一个既不在秘境日程、也不在任何采集路线里的材料：不能瞎猜成某种任务。"""
        source = material_planner.resolve_source(
            {"item_id": 900001, "item_name": "ZZZ-并不存在的物品", "category": "other"}
        )

        self.assertIn(source["type"], (material_planner.TASK_WAITING_ROUTE,
                                       material_planner.TASK_GATHER,
                                       material_planner.TASK_HUNT,
                                       material_planner.TASK_MINE,
                                       material_planner.TASK_COOK,
                                       material_planner.TASK_SCRIPT))
        # 无论是哪种，都必须给出理由（不能是一条没有解释的任务）
        self.assertTrue(source["reason"])

    def test_availability_of_a_domain_follows_the_weekday(self):
        source = material_planner.resolve_source(TALENT_BOOK)
        open_day = source["open_days"][0]

        status, note = material_planner.available_today(source, weekday=open_day)
        self.assertEqual(status, material_planner.STATUS_RUNNABLE)

        closed_day = next(day for day in "一二三四五六日" if day not in source["open_days"])
        status, note = material_planner.available_today(source, weekday=closed_day)
        self.assertEqual(status, material_planner.STATUS_CLOSED_TODAY)
        self.assertIn("不开放", note)

    def test_cooldown_marks_a_gather_route_as_blocked(self):
        source = material_planner.resolve_source(QINGXIN)

        status, note = material_planner.available_today(
            source, cooldown={"cooling": True, "hours_left": 20.5, "hours_left_text": "20 小时 30 分"}
        )

        self.assertEqual(status, material_planner.STATUS_COOLDOWN)
        self.assertIn("20 小时 30 分", note)

    def test_no_cooldown_means_runnable(self):
        source = material_planner.resolve_source(QINGXIN)

        status, _ = material_planner.available_today(source, cooldown={"cooling": False})

        self.assertEqual(status, material_planner.STATUS_RUNNABLE)


class TaskRoutingTests(PlannerCase):
    def test_same_material_in_two_phases_sums_its_numbers(self):
        """同一材料跨阶段只出一条任务，但**数字必须累加**。

        摩拉在「角色等级」和「天赋」里是**两笔**花费（实测：奥黛塔 等级 647,990
        + 天赋 3,182,500）。以前只把 `missing` 取最大值、`required` 留在第一阶段，
        于是界面上出现"需要 647,990 / 缺 3,182,500"这种自相矛盾的组合 ——
        玩家根本分不清哪个是哪个（被反馈过）。
        """
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [
            requirement(MORA, 647990, ["character_level"]),
            requirement(MORA, 3182500, ["talent"]),
        ])

        plan = self.build(inventory=inventory({}))
        mora = next(task for task in plan["tasks"] if task["item_id"] == MORA["item_id"])

        self.assertEqual(mora["required"], 647990 + 3182500)
        self.assertEqual(mora["missing"], 647990 + 3182500)
        self.assertEqual(mora["owned"], 0)                 # 没有背包数据就是 0
        self.assertEqual(len(mora["phases"]), 2)            # 两个阶段都记着

    def test_closed_domain_is_not_put_into_the_bettergi_command(self):
        """秘境今天不开 → 任务只做标注，不下发（否则白跑一趟）。"""
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(TALENT_BOOK, 38, ["talent"])])
        source = material_planner.resolve_source(TALENT_BOOK)
        closed_day = next(day for day in "一二三四五六日" if day not in source["open_days"])
        plan = self.build(inventory=inventory({}), weekday=closed_day)

        self.assertTrue(all(task["status"] != material_planner.STATUS_RUNNABLE
                            for task in plan["tasks"]))
        self.assertEqual(plan["bettergi_cmd"], {})
        self.assertEqual(plan["status"], growth_models.STATE_BLOCKED)
        self.assertTrue(any(blocker["kind"] == "closed_today" for blocker in plan["blockers"]))

    def test_missing_route_becomes_a_blocker(self):
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [
            requirement({"item_id": 999999, "item_name": "查不到来源的材料"}, 10, ["character_level"]),
        ])

        plan = self.build(inventory=inventory({}))

        self.assertEqual(plan["tasks"][0]["status"], material_planner.STATUS_WAITING_ROUTE)
        self.assertTrue(any(blocker["kind"] == "waiting_route" for blocker in plan["blockers"]))

    def test_fallback_runs_other_characters_when_the_current_one_is_blocked(self):
        """规格书 §30 最后一句：当前角色今天拿不到 → 去刷别的（但不跳过当前角色）。"""
        self.add_character(10000089, "胡桃", 80, 5, priority=1)
        self.add_character(10000032, "班尼特", 70, 4, priority=2)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5), avatar(10000032, "班尼特", 70, 4))
        self.set_requirements(10000089, [requirement(TALENT_BOOK, 38, ["talent"])])
        self.set_requirements(10000032, [requirement(QINGXIN, 50, ["character_level"])])

        source = material_planner.resolve_source(TALENT_BOOK)
        closed_day = next(day for day in "一二三四五六日" if day not in source["open_days"])
        plan = self.build(inventory=inventory({}), weekday=closed_day)

        self.assertEqual(plan["current_character_id"], 10000089)      # 当前角色没被跳过
        self.assertTrue(plan["execution"]["fallback"])
        self.assertTrue(all(task["character_id"] == 10000032
                            for task in plan["execution"]["tasks"]))
        self.assertIn("清心", str(plan["bettergi_cmd"]))

    def test_bettergi_command_shape_matches_the_controller(self):
        """`energy_task` 只能有一个（秘境/地脉/Boss 互斥），自由任务走 `free_task`。"""
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [
            requirement(MORA, 500000, ["character_level"]),
            requirement(QINGXIN, 50, ["character_level"]),
        ])

        plan = self.build(inventory=inventory({}))
        command = plan["bettergi_cmd"]

        self.assertEqual(command["energy_task"]["action"], "run_leyline")
        self.assertEqual(command["energy_task"]["target"], material_planner.LEYLINE_MORA)
        self.assertGreater(command["energy_task"]["count"], 0)
        self.assertEqual(command["free_task"], [{"action": "gather", "target": "清心"}])

    def test_domain_command_carries_domain_index_for_sunday(self):
        """武器突破材料 → 秘境任务，并带上 `domain_index`（BetterGI 周日/全开日用）。"""
        self.add_character(10000089, "胡桃", 80, 5, weapon_enabled=1)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(WEAPON_MAT, 6, ["weapon_level"])])

        source = material_planner.resolve_source(WEAPON_MAT)
        self.assertEqual(source["type"], material_planner.TASK_DOMAIN)
        # ⚠️ 在**开放日**规划才会生成秘境任务（否则是 closed_today，不下发）
        weekday = source["open_days"][0] if source["open_days"] else None
        plan = self.build(inventory=inventory({}), weekday=weekday)
        command = plan["bettergi_cmd"]

        self.assertIn("energy_task", command)
        self.assertEqual(command["energy_task"]["action"], "run_domain")
        self.assertEqual(command["energy_task"]["target"], source["domain"])
        if source["domain_index"]:
            self.assertEqual(command["energy_task"]["domain_index"], str(source["domain_index"]))


# ==========================================
# 🌟 库存新鲜度（§35.23）
# ==========================================


class InventoryFreshnessTests(PlannerCase):
    def test_stale_inventory_is_flagged_not_hidden(self):
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(QINGXIN, 80, ["character_level"])])

        # 先写一份"30 小时前同步"的库存文件（超过 GROWTH_INVENTORY_STALE_HOURS=12）
        old = inventory({100092: 20})
        old["fetched_at"] = (datetime.datetime.now() - datetime.timedelta(hours=30)).strftime(
            mys_inventory._TIME_FORMAT
        )
        mys_inventory.save_inventory(old, path=os.path.join(self.root, "inventory_latest.json"))

        # 不给 inventory → build_plan 自己从盘上读，于是 freshness 判定走真实代码路径
        plan = self.build()

        self.assertTrue(plan["inventory"]["available"])
        self.assertTrue(plan["inventory"]["stale"])
        self.assertIn("不是实时数据", plan["inventory"]["hint"])
        self.assertTrue(any(blocker["kind"] == "inventory_stale" for blocker in plan["blockers"]))

    def test_missing_inventory_is_reported_as_unsynced(self):
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(QINGXIN, 80, ["character_level"])])

        plan = self.build(inventory=None)

        self.assertFalse(plan["inventory"]["available"])
        self.assertTrue(plan["inventory"]["stale"])
        # 没有库存 ≠ 缺口为 0：需求全算成缺口，并明确标注数据不可用
        self.assertEqual(plan["inventory"]["item_count"], 0)

    def test_fresh_inventory_is_not_flagged(self):
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(QINGXIN, 80, ["character_level"])])

        plan = self.build(inventory=inventory({100092: 20}))

        self.assertTrue(plan["inventory"]["available"])
        self.assertFalse(plan["inventory"]["stale"])


# ==========================================
# 🌟 米游社不可用时的行为（§35.11 / §35.12）
# ==========================================


class MysUnavailableTests(PlannerCase):
    def test_no_cookie_blocks_the_plan_with_an_actionable_message(self):
        self.add_character(10000089, "胡桃", 80, 5)
        with patch.object(mys_api, "cookie_configured", return_value=False):
            plan = self.build()

        self.assertEqual(plan["status"], growth_models.STATE_BLOCKED)
        self.assertTrue(plan["compute_errors"])
        self.assertTrue("MYS_COOKIE" in plan["compute_errors"][0]["error"]
                        or "米游社 Cookie" in plan["compute_errors"][0]["error"])
        self.assertTrue(any(blocker["kind"] == "compute_failed" for blocker in plan["blockers"]))

    def test_listing_avatars_without_a_cookie_sends_no_request(self):
        with patch.object(mys_api, "cookie_configured", return_value=False), \
             patch.object(mys_calculator, "fetch_avatar_list") as fake:
            self.assertEqual(growth_planner.fetch_avatars(force=True), {})

        fake.assert_not_called()
    def test_plan_endpoint_error_is_reported_per_character(self):
        """两条路都拿不到时要按角色如实报错（这里让算材料返回空、方案接口报风控）。"""
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        empty = {"requirements": [], "by_phase": {}, "empty": True, "body": {}}

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "compute_for_target", return_value=empty), \
             patch.object(mys_calculator, "fetch_avatar_plan",
                          side_effect=mys_api.MysRiskControl("要验证码")):
            plan = self.build()

        self.assertEqual(plan["compute_errors"][0]["kind"], "MysRiskControl")
        self.assertIn("验证码", plan["compute_errors"][0]["error"])

    def test_a_catalog_profile_without_a_level_is_not_trusted(self):
        """图鉴档案的 `level` 常常是 0（`base_level=5` 其实是稀有度）。

        拿它当"当前等级"去算材料，米游社直接回 `-500001`，整个角色就报错了
        （实测：用户的奥黛塔目标就是这样挂的）。所以这种档案必须标成"未知"，
        让上层去 `/v1/sync/avatar/detail` 拿真实等级。
        """
        current = growth_planner.current_state(
            10000150, target={"level_target": 90, "character_id": 10000150},
            avatars={10000150: {"id": 10000150, "name": "奥黛塔", "level": 0, "rarity": 5}},
        )

        self.assertFalse(current["known"])
        self.assertEqual(current["level"], 0)

    def test_the_sync_endpoint_state_is_used_for_the_plan_row(self):
        """算需求时拿到的真实状态要回写成"当前状态"（否则进度条按 0 级算）。"""
        converted = growth_planner._state_to_current({
            "level": 71, "promote_level": 5,
            "talents": [{"pos_name": "普通攻击", "level": 1},
                        {"pos_name": "元素战技", "level": 4},
                        {"pos_name": "元素爆发", "level": 7}],
            "weapon": {"id": 14402, "name": "流浪乐章", "level_current": 20},
        })

        self.assertEqual(converted["level"], 71)
        self.assertEqual(converted["talents"],
                         {"normal": 1, "skill": 4, "burst": 7})
        self.assertEqual(converted["weapon_level"], 20)
        self.assertEqual(converted["weapon_name"], "流浪乐章")
        self.assertTrue(converted["known"])

    def test_talents_fall_back_to_order_when_pos_name_is_missing(self):
        """新角色的天赋 `pos_name` 可能是空串 —— 那时按返回顺序认槽位。

        实测：奥黛塔的 7 条天赋 `pos_name` 全是空的，只按名字认会得到 `0/0/0`
        （界面观感很差，进度条也全 0）。真实顺序就是 普攻 → 战技 → 爆发。
        """
        converted = growth_planner._state_to_current({
            "level": 81,
            "talents": [
                {"pos_name": "", "level": 1, "max_level": 10},     # 普攻
                {"pos_name": "", "level": 10, "max_level": 10},    # 战技
                {"pos_name": "", "level": 6, "max_level": 10},     # 爆发
                {"pos_name": "", "level": 0, "max_level": 1},      # 固有天赋，不算
            ],
            "weapon": {"id": 11425, "name": "海渊终曲", "level_current": 90},
        })

        self.assertEqual(converted["talents"], {"normal": 1, "skill": 10, "burst": 6})

    def test_pos_name_wins_over_the_order(self):
        """有 `pos_name` 时以它为准（顺序不可靠时不能瞎猜）。"""
        converted = growth_planner._state_to_current({
            "level": 90,
            "talents": [
                {"pos_name": "元素爆发", "level": 7, "max_level": 10},
                {"pos_name": "普通攻击", "level": 3, "max_level": 10},
                {"pos_name": "元素战技", "level": 9, "max_level": 10},
            ],
            "weapon": {},
        })

        self.assertEqual(converted["talents"], {"normal": 3, "skill": 9, "burst": 7})

    def test_the_same_day_never_refetches_even_after_a_restart(self):
        """**玩家明确要求的口径**：每天自动拉一次；关掉程序再打开**不要再拉**。

        算材料是每个角色 2 个请求，米游社风控很敏感 ——
        "每次开程序都拉一轮"迟早被风控盯上（项目里踩过）。
        做法：结果存进数据库，新鲜度按**业务日**判定；`invalidate_cache()`（重启时内存缓存清空）
        之后仍然能从库里读回来。
        """
        self.add_character(10000089, "胡桃", 80, 5)
        target = {"character_id": 10000089, "character_name": "胡桃", "enabled": True,
                  "level_target": 90, "normal_target": 10, "skill_target": 10, "burst_target": 10}
        calls = {"state": 0, "compute": 0}
        # ⚠️ 必须把档案也喂进缓存：`requirements_for` 的当天缓存是按**签名**命中的，
        #    签名里含当前等级/武器/天赋 —— 所以缓存里的档案必须和打桩的 state **一致**
        #    （这里两边都给"无天赋 + 武器 80"）。只打桩 state、不喂档案的话，
        #    第二次调用拿到的"当前状态"不一样 → 签名变了 → 缓存必然不命中。
        #    生产里扫描成功后会落库并合并进缓存，不会出现这种不一致。
        self.set_avatars(avatar(10000089, "胡桃", 80, 5, skills={},
                                weapon={"id": 12512, "name": "护摩之杖", "level": 80}))

        def fake_state(*args, **kwargs):
            calls["state"] += 1
            # ⚠️ `weapon` 必须带**等级**：新流程里 `/v1/sync/avatar/detail` 的返回要"可用"
            #    才会拿去算材料（`owned=True` 但等级/天赋/武器全空 = 响应不完整，
            #    不能当成"账号里没有这个角色"，也不能拿它当起始状态）。
            #    桩里给空 weapon 的话，流程会退回本地档案、根本走不到算材料那一步。
            return {"avatar_id": 10000089, "owned": True, "name": "胡桃", "level": 80,
                    "promote_level": 5, "element_attr_id": 1, "talents": [],
                    "weapon": {"id": 12512, "name": "护摩之杖", "level": 80}}

        def fake_compute(*args, **kwargs):
            calls["compute"] += 1
            requirement = {"item_id": 104003, "item_name": "大英雄的经验", "required": 245,
                           "phases": ["character_level"], "owned": 100, "lack": 145}
            return {"empty": False, "body": {}, "available": {},
                    "requirements": [requirement],
                    "by_phase": {"character_level": [requirement], "weapon_level": [], "talent": []}}

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "fetch_avatar_state", side_effect=fake_state), \
             patch.object(mys_calculator, "compute_for_target", side_effect=fake_compute):
            growth_planner.requirements_for(target, force=True)
            self.assertEqual(calls, {"state": 1, "compute": 1})     # 今天第一次：真的拉

            growth_planner.invalidate_cache()                      # ← 模拟"关掉程序再打开"
            calls.update(state=0, compute=0)
            result = growth_planner.requirements_for(target)

        self.assertEqual(calls, {"state": 0, "compute": 0})         # 一个请求都不发
        self.assertEqual(len(result["requirements"]), 1)            # 但结果照样有
        self.assertEqual(result["requirements"][0]["lack"], 145)    # 「还差」也在

    def test_a_new_day_fetches_again(self):
        """到了新的一天（业务日变了）→ 自动重新拉一次。"""
        self.add_character(10000089, "胡桃", 80, 5)
        target = {"character_id": 10000089, "character_name": "胡桃", "enabled": True,
                  "level_target": 90}
        calls = {"compute": 0}

        def fake_compute(*args, **kwargs):
            calls["compute"] += 1
            return {"empty": False, "body": {}, "available": {}, "requirements": [],
                    "by_phase": {"character_level": [], "weapon_level": [], "talent": []}}

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "fetch_avatar_state",
                          return_value={"avatar_id": 10000089, "owned": True, "level": 80,
                                        "talents": [],
                                        "weapon": {"id": 12512, "level": 80}}), \
             patch.object(mys_calculator, "compute_for_target", side_effect=fake_compute):
            # 先造一条"昨天的"记录
            growth_db.set_setting(growth_planner._COMPUTE_CACHE_KEY, json.dumps({
                "entries": {"10000089": {"schema": growth_planner._COMPUTE_SCHEMA,
                                         "day": "2000-01-01", "at": 0,
                                         "signature": "whatever",
                                         "result": {"requirements": [{"item_id": 1}],
                                                    "by_phase": {}}}},
            }, ensure_ascii=False))
            growth_planner.invalidate_cache()
            growth_planner.requirements_for(target)

        self.assertEqual(calls["compute"], 1)          # 新的一天要重新算

    def test_manual_refresh_ignores_the_day_cache(self):
        """点「刷新材料数据」= `force=True` → 无视缓存立刻重拉。"""
        self.add_character(10000089, "胡桃", 80, 5)
        target = {"character_id": 10000089, "character_name": "胡桃", "enabled": True,
                  "level_target": 90}
        calls = {"compute": 0}

        def fake_compute(*args, **kwargs):
            calls["compute"] += 1
            return {"empty": False, "body": {}, "available": {}, "requirements": [],
                    "by_phase": {"character_level": [], "weapon_level": [], "talent": []}}

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "fetch_avatar_state",
                          return_value={"avatar_id": 10000089, "owned": True, "level": 80,
                                        "talents": [],
                                        "weapon": {"id": 12512, "level": 80}}), \
             patch.object(mys_calculator, "compute_for_target", side_effect=fake_compute):
            growth_planner.invalidate_cache()
            growth_planner.requirements_for(target, force=True)
            growth_planner.requirements_for(target)                 # 当天第二次：读缓存
            growth_planner.requirements_for(target, force=True)     # 手动刷新：重拉

        self.assertEqual(calls["compute"], 2)

    def test_requirements_come_from_the_compute_endpoint_first(self):
        """算材料接口（/v2/compute）是首选：它给**分阶段**的完整需求（等级+天赋+武器）。"""
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        computed = {
            "empty": False,
            "body": {"avatar_level_target": 90},
            "requirements": [{"item_id": 104003, "item_name": "大英雄的经验", "required": 245,
                              "phases": ["character_level"]}],
            "by_phase": {"character_level": [{"item_id": 104003, "item_name": "大英雄的经验",
                                              "required": 245, "phases": ["character_level"]}],
                         "weapon_level": [], "talent": []},
        }

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "compute_for_target", return_value=computed), \
             patch.object(mys_calculator, "fetch_avatar_plan") as plan_call:
            result = growth_planner.requirements_for(
                {"character_id": 10000089, "character_name": "胡桃"}, force=True)

        plan_call.assert_not_called()                  # 算材料成功就不必再要方案
        self.assertEqual(len(result["requirements"]), 1)
        self.assertEqual(result["by_phase"]["character_level"][0]["item_id"], 104003)
        self.assertEqual(result["phase_note"], "")     # 分阶段的需求不需要额外解释

    def test_the_plan_endpoint_is_the_fallback_when_compute_is_empty(self):
        """算材料给空桶时退回养成方案接口 —— 它只有天赋，必须写清楚覆盖范围。"""
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        empty = {"requirements": [], "by_phase": {}, "empty": True, "body": {}}
        plan_payload = {
            "character_name": "胡桃", "current_level": 0, "target_level": 0,
            "skills": [{"name": "蝶引来生", "pos_name": "元素战技",
                        "current_level": 8, "target_level": 10, "priority": 1}],
            "materials": [{"item_id": 104309, "item_name": "「勤劳」的哲学",
                           "required": 38, "lack": 0, "rarity": 4}],
        }

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "compute_for_target", return_value=empty), \
             patch.object(mys_calculator, "fetch_avatar_plan", return_value=plan_payload):
            result = growth_planner.requirements_for(
                {"character_id": 10000089, "character_name": "胡桃"}, force=True)

        self.assertEqual(len(result["requirements"]), 1)
        self.assertEqual(result["requirements"][0]["phases"], [growth_models.PHASE_TALENT])
        self.assertIn("只覆盖天赋", result["phase_note"])
        self.assertIn("天赋 10", result["target_text"])


# ==========================================
# 🌟 完成后重新同步（§19）
# ==========================================


class AfterTaskSyncTests(PlannerCase):
    def test_after_task_sync_triggers_a_fresh_inventory_sync_and_replan(self):
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(QINGXIN, 80, ["character_level"])])

        with patch.object(growth_planner, "sync_inventory") as fake_sync, \
             patch.object(growth_planner, "build_plan", return_value={"status": "X"}):
            fake_sync.return_value = {"ok": True, "item_count": 400, "delta": []}
            result = growth_planner.after_task_sync(path=self.db, db_path=self.db,
                                                   plan_id=7, character_id=10000089,
                                                   phase="character_level")

        fake_sync.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertIn("重新同步", result["note"])

    def test_after_task_sync_records_execution_history(self):
        growth_planner.after_task_sync(path=self.db, db_path=self.db, plan_id=3,
                                       character_id=10000089, phase="talent",
                                       status="FINISHED")

        rows = growth_db.list_executions(path=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["character_id"], 10000089)
        self.assertEqual(rows[0]["phase"], "talent")

    def test_after_task_sync_can_be_disabled_by_config(self):
        with patch.object(config, "GROWTH_POST_TASK_SYNC", False):
            result = growth_planner.after_task_sync(path=self.db, db_path=self.db)

        self.assertTrue(result["skipped"])
        self.assertIn("GROWTH_POST_TASK_SYNC", result["reason"])

    def test_failed_resync_keeps_previous_snapshot_and_says_so(self):
        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "fetch_my_items", side_effect=mys_api.MysTransportError("断网")):
            result = growth_planner.after_task_sync(path=self.db, db_path=self.db)

        self.assertFalse(result["sync"]["ok"])
        self.assertIn("断网", result["note"])

    def test_bettergi_completion_hook_is_registered_idempotently(self):
        from skills import bgi_watcher

        growth_planner.register_watcher_hook()
        growth_planner.register_watcher_hook()

        hooks = [hook for hook in bgi_watcher._COMPLETION_HOOKS
                 if hook is growth_planner._on_bettergi_finished]
        self.assertEqual(len(hooks), 1)

    def test_note_execution_remembers_the_current_phase(self):
        growth_planner.note_execution(plan_id=5, character_id=10000089, phase="talent")

        self.assertEqual(growth_planner.last_execution(),
                         {"plan_id": 5, "character_id": 10000089, "phase": "talent"})


# ==========================================
# 🌟 执行失败不污染库存（§35.13 / §40）
# ==========================================


class ExecutionIsolationTests(PlannerCase):
    def test_failed_resync_never_invents_inventory_numbers(self):
        """BetterGI 跑挂了：库存一个字都不许变（只信下一次成功的米游社同步）。"""
        model = inventory({100092: 20})
        mys_inventory.save_inventory(model, path=os.path.join(self.root, "inventory_latest.json"))

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "fetch_my_items", side_effect=mys_api.MysRiskControl("风控")):
            growth_planner.after_task_sync(path=self.db, db_path=self.db)

        after = mys_inventory.load_inventory(path=os.path.join(self.root, "inventory_latest.json"))
        self.assertEqual(after["items"], model["items"])

    def test_plan_never_writes_back_to_the_inventory(self):
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(QINGXIN, 80, ["character_level"])])

        model = inventory({100092: 20})
        path = os.path.join(self.root, "inventory_latest.json")
        mys_inventory.save_inventory(model, path=path)

        self.build(inventory=model)

        self.assertEqual(mys_inventory.load_inventory(path=path)["items"], model["items"])


# ==========================================
# 🌟 计划的落库与历史
# ==========================================


class PlanPersistenceTests(PlannerCase):
    def test_plan_is_recorded_with_its_tasks(self):
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(QINGXIN, 80, ["character_level"])])

        plan = self.build(record=True)

        self.assertGreater(plan["plan_id"], 0)
        tasks = growth_db.list_plan_tasks(plan["plan_id"], path=self.db)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["material"], "清心")
        self.assertEqual(growth_db.latest_plan(path=self.db)["id"], plan["plan_id"])

    def test_plan_lines_are_human_readable(self):
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(QINGXIN, 80, ["character_level"])])

        lines = growth_planner.plan_lines(self.build(inventory=inventory({100092: 20})))

        text = "\n".join(lines)
        self.assertIn("胡桃", text)
        self.assertIn("清心", text)
        self.assertIn("库存", text)

    def test_status_text_is_one_line(self):
        self.add_character(10000089, "胡桃", 80, 5)
        self.set_avatars(avatar(10000089, "胡桃", 80, 5))
        self.set_requirements(10000089, [requirement(QINGXIN, 80, ["character_level"])])

        text = growth_planner.status_text(path=self.db, db_path=self.db)

        self.assertIn("胡桃", text)
        self.assertNotIn("\n", text)


class OwnershipScanTests(PlannerCase):
    def _catalog(self):
        return {
            10000006: {"id": 10000006, "name": "丽莎", "rarity": 4,
                       "element": "Electro", "level": 0},
            10000002: {"id": 10000002, "name": "神里绫华", "rarity": 5,
                       "element": "Cryo", "level": 0},
        }

    def test_available_characters_confirms_owned_and_unowned_from_detail(self):
        calls = []

        def fake_state(character_id, **_kwargs):
            calls.append(character_id)
            return {"avatar_id": character_id, "owned": character_id == 10000006}

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "fetch_catalog_all", return_value=self._catalog()), \
             patch.object(mys_calculator, "fetch_avatar_state", side_effect=fake_state):
            avatars = growth_planner.available_characters(uid="100000001")

        self.assertEqual(calls, [10000002, 10000006])
        self.assertTrue(avatars[10000006]["owned"])
        self.assertTrue(avatars[10000006]["owned_known"])
        self.assertFalse(avatars[10000002]["owned"])
        self.assertTrue(avatars[10000002]["owned_known"])
        self.assertTrue(growth_planner.ownership_scan_status()["complete"])

    def test_completed_scan_is_reused_until_refresh(self):
        catalog = self._catalog()
        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "fetch_catalog_all", return_value=catalog), \
             patch.object(mys_calculator, "fetch_avatar_state",
                          return_value={"owned": True}):
            growth_planner.available_characters(uid="100000001")

        with patch.object(mys_calculator, "fetch_catalog_all", return_value=catalog), \
             patch.object(mys_calculator, "fetch_avatar_state") as fetch_state:
            avatars = growth_planner.available_characters(uid="100000001")

        fetch_state.assert_not_called()
        self.assertTrue(avatars[10000006]["owned"])

    def test_missing_cookie_does_not_scan_or_claim_unknown_roles(self):
        with patch.object(mys_api, "cookie_configured", return_value=False), \
             patch.object(mys_calculator, "fetch_catalog_all", return_value=self._catalog()), \
             patch.object(mys_calculator, "fetch_avatar_state") as fetch_state:
            avatars = growth_planner.available_characters(uid="100000001")

        fetch_state.assert_not_called()
        self.assertFalse(avatars[10000006]["owned"])
        self.assertFalse(avatars[10000006]["owned_known"])

    def test_manual_override_wins_over_the_last_scan(self):
        growth_planner.set_manual_ownership(10000006, False)

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator, "fetch_catalog_all", return_value=self._catalog()), \
             patch.object(mys_calculator, "fetch_avatar_state",
                          return_value={"owned": True}):
            avatars = growth_planner.available_characters(uid="100000001", force=True)

        self.assertFalse(avatars[10000006]["owned"])
        self.assertTrue(avatars[10000006]["owned_known"])


if __name__ == "__main__":
    unittest.main()
