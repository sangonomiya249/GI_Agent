"""米游社养成计算器（skills/mys_calculator.py）的测试。

重点覆盖"不出网也要能验"的部分：请求体组装、响应解析（各种形状）、
背包条目的字段别名、错误分类，以及**没配 cookie 时一次请求都不发**。

真实接口要账号 cookie 且有风控，所以 HTTP 层全部打桩 —— 真机自检走
`python -m skills.mys_calculator --check`。
"""

import unittest
from unittest.mock import patch

import config
from brain import growth_models
from skills import mys_api, mys_calculator


class ComputeBodyTests(unittest.TestCase):
    """`compute` 的请求体：米游社认的是这套字段名，写错就静默算不出材料。"""

    def test_body_uses_the_documented_field_names(self):
        body = mys_calculator.build_compute_body(10000089, 80, 90)

        self.assertEqual(body["avatar_id"], 10000089)
        self.assertEqual(body["avatar_level_current"], 80)
        self.assertEqual(body["avatar_level_target"], 90)
        self.assertIn("skill_list", body)
        self.assertIn("weapon", body)

    def test_skill_list_carries_current_and_target(self):
        """天赋条目的 id 字段名是 `id`（**不是** `skill_id`）—— 实测确认过。

        写成 `skill_id` 米游社**不报错**，只回三个空 consume 桶（"算了但什么都不缺"），
        是最难查的一种失败，所以这条断言是防回归的重点。
        """
        body = mys_calculator.build_compute_body(
            10000089, 80, 90,
            skills={"10891": {"current": 8, "target": 10}, "10892": {"current": 6, "target": 9}},
        )

        self.assertEqual(
            body["skill_list"],
            [{"id": 10891, "level_current": 8, "level_target": 10},
             {"id": 10892, "level_current": 6, "level_target": 9}],
        )
        self.assertNotIn("skill_id", body["skill_list"][0])

    def test_skills_already_at_target_are_skipped(self):
        """已经达标的天赋不往请求里塞：省一次无效条目，也更不容易被风控盯上。"""
        body = mys_calculator.build_compute_body(
            10000089, 80, 90, skills={"10891": {"current": 10, "target": 10}},
        )

        self.assertEqual(body["skill_list"], [])

    def test_weapon_is_omitted_when_not_included(self):
        body = mys_calculator.build_compute_body(10000089, 80, 90, weapon={})

        self.assertEqual(body["weapon"], {})

    def test_weapon_is_sent_when_target_differs(self):
        body = mys_calculator.build_compute_body(
            10000089, 80, 90, weapon={"id": 12512, "level": 80, "level_target": 90},
        )

        self.assertEqual(body["weapon"], {"id": 12512, "level_current": 80, "level_target": 90})

    def test_weapon_at_target_is_not_sent(self):
        body = mys_calculator.build_compute_body(
            10000089, 80, 90, weapon={"id": 12512, "level": 90, "level_target": 90},
        )

        self.assertEqual(body["weapon"], {})

    def test_the_wire_body_is_not_wrapped_in_a_data_envelope(self):
        """🚨 **最贵的一个坑**：算材料的请求体**不能**包 `{"data": ...}` 外壳。

        米游社计算器前端是这样调的：`batchCost({data: a, headers: {...}})`
        —— 那个 `{data, headers}` 是 **axios 的 config**，不是 HTTP body；
        真正发出去的就是 `a` 本身（平的）。包了外壳会得到 `retcode 0 + 三个空桶`：
        不报错，但一个材料都算不出来（曾经据此误判成"接口已废"）。
        """
        captured = {}

        def fake_request(method, url, query="", body_obj=None, cookie=None, timeout=15,
                         path=None, headers=None):
            captured["body"] = body_obj
            return {"avatar_consume": [{"id": 202, "name": "摩拉", "num": 100}]}

        with patch.object(config, "MYS_COOKIE", "ltuid=1; ltoken=x"), \
             patch.object(mys_api, "_request", fake_request):
            result = mys_calculator.compute_materials(10000089, 80, 90, save=False)

        self.assertIn("avatar_level_target", captured["body"])       # 字段在**顶层**
        self.assertNotIn("data", captured["body"])                   # 没有 data 外壳
        self.assertEqual(result["requirements"][0]["item_name"], "摩拉")

    def test_avatar_from_state_gives_the_compute_body_what_it_needs(self):
        """`fetch_avatar_state()` 的结果要能直接喂给算材料（等级/突破/天赋 group_id/武器）。"""
        profile = mys_calculator.avatar_from_state(
            mys_calculator.parse_avatar_state(AVATAR_STATE))

        self.assertEqual(profile["id"], 10000006)
        self.assertEqual(profile["level"], 71)
        self.assertEqual(profile["promote_level"], 5)
        self.assertEqual(profile["weapon"]["id"], 14402)
        # 天赋键用 group_id（米游社算材料认的是它，不是天赋自己的 id）
        self.assertEqual(sorted(profile["skills"]), ["431", "432"])
        self.assertEqual(
            [{"id": talent["id"], "group_id": talent["group_id"]}
             for talent in profile["_raw"]["skill_list"]],
            [{"id": 431, "group_id": 431}, {"id": 432, "group_id": 432}],
        )


class ComputeForTargetTests(unittest.TestCase):
    """`compute_for_target`：目标 → 请求体的落地（含"武器没纳入就不发武器"）。"""

    def _avatar(self):
        return {
            "id": 10000089, "name": "胡桃", "level": 80, "rarity": 5, "element": "Pyro",
            "skills": {"10891": 8, "10892": 8, "10895": 8},
            "weapon": {"id": 12512, "name": "护摩之杖", "level": 80},
        }

    def test_three_talents_map_to_targets_in_order(self):
        """技能 id 升序 = 普攻 → 战技 → 爆发，所以第 1/2/3 个各对应一项目标。"""
        target = {"level_target": 90, "normal_target": 10, "skill_target": 9, "burst_target": 8}

        with patch.object(mys_calculator, "compute_materials") as fake:
            fake.return_value = {"requirements": [], "by_phase": {}}
            mys_calculator.compute_for_target(self._avatar(), target)

        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs["skills"], {
            "10891": {"current": 8, "target": 10},
            "10892": {"current": 8, "target": 9},
            "10895": {"current": 8, "target": 8},
        })

    def test_weapon_target_only_sent_when_enabled_and_chosen(self):
        """只有"勾了武器阶段**并且选了武器**"才把武器发给米游社。

        ⚠️ 只勾不选（`weapon_id = 0`）时以前会把**当前装备的那把**算进来 ——
        玩家看到的就是"莫名其妙多出一堆武器突破材料"（被反馈过）。现在的行为是：
        没指定武器 = 这一阶段不算，不拿装备凑数。
        """
        avatar = self._avatar()
        avatar["weapon"] = {"id": 12512, "name": "祭礼剑", "level": 40}

        target = {"level_target": 90, "weapon_enabled": False, "weapon_level_target": 90}
        with patch.object(mys_calculator, "compute_materials") as fake:
            fake.return_value = {}
            mys_calculator.compute_for_target(avatar, target)
        self.assertEqual(fake.call_args.kwargs["weapon"], {})

        # 勾了但没选武器 → 仍然不算（不拿身上那把凑数）
        target["weapon_enabled"] = True
        with patch.object(mys_calculator, "compute_materials") as fake:
            fake.return_value = {}
            mys_calculator.compute_for_target(avatar, target)
        self.assertEqual(fake.call_args.kwargs["weapon"], {})

        # 选了武器 → 发出去，并且目标等级生效
        target["weapon_id"] = 12512
        with patch.object(mys_calculator, "compute_materials") as fake:
            fake.return_value = {}
            mys_calculator.compute_for_target(avatar, target)
        self.assertEqual(fake.call_args.kwargs["weapon"]["id"], 12512)
        self.assertEqual(fake.call_args.kwargs["weapon"]["level_target"], 90)

    def test_level_targets_are_clamped_to_1_90(self):
        """81 必须合法（规格书 §10 明确要求不能只支持 80/90）。"""
        with patch.object(mys_calculator, "compute_materials") as fake:
            fake.return_value = {}
            mys_calculator.compute_for_target(self._avatar(), {"level_target": 81})
            self.assertEqual(fake.call_args.args[2], 81)

            mys_calculator.compute_for_target(self._avatar(), {"level_target": 999})
            self.assertEqual(fake.call_args.args[2], 90)


class ParseComputeTests(unittest.TestCase):
    """`compute` 响应解析：**桶名是实测确认的**（`avatar_consume` 等），但仍走宽松识别。

    写死路径的实现会在米游社改版时静默返回空材料 —— 而"静默返回空材料"会直接导致
    "什么都缺 0 个"的假完成。所以这里既要认实测形状，也要能容忍改名。
    """

    def test_parses_the_real_payload_shape(self):
        """实测的响应：`data` 下三个桶，条目字段是 `{item_id, num, ...}`。"""
        payload = {
            "avatar_consume": [
                {"item_id": 104001, "num": 1250000, "name": "摩拉"},
                {"item_id": 202001, "num": 48, "name": "大英雄的经验"},
            ],
            "avatar_skill_consume": [
                {"item_id": 104301, "num": 38, "name": "「诗文」的哲学"},
            ],
            "weapon_consume": [
                {"item_id": 114001, "num": 6, "name": "狮牙斗士的镣铐"},
            ],
            "reliquary_consume": [],
            "skills_consume": [],
        }

        requirements, by_phase = mys_calculator.parse_compute(payload)
        by_id = {row["item_id"]: row for row in requirements}

        self.assertEqual(by_id[104001]["required"], 1250000)
        self.assertEqual(by_id[104001]["phases"], ["character_level"])
        self.assertEqual(by_id[104301]["phases"], ["talent"])
        self.assertEqual(by_id[114001]["phases"], ["weapon_level"])
        self.assertEqual(len(by_phase["talent"]), 1)

    def test_parses_a_realistic_payload(self):
        payload = {
            "avatar_level": {
                "item_list": [
                    {"item_id": 104001, "num": 1250000},
                    {"item_id": 202001, "name": "大英雄的经验", "num": 48},
                ],
            },
            "avatar_skill": {
                "item_list": [{"item_id": 104301, "name": "「诗文」的哲学", "num": 38}],
            },
            "avatar_weapon": {
                "item_list": [{"item_id": 114001, "name": "狮牙斗士的镣铐", "num": 6}],
            },
        }

        requirements, by_phase = mys_calculator.parse_compute(payload)
        by_id = {row["item_id"]: row for row in requirements}

        self.assertEqual(by_id[104001]["required"], 1250000)
        self.assertEqual(by_id[104001]["phases"], ["character_level"])
        self.assertEqual(by_id[104301]["phases"], ["talent"])
        self.assertEqual(by_id[114001]["phases"], ["weapon_level"])
        self.assertEqual(len(by_phase["talent"]), 1)

    def test_flat_item_list_defaults_to_character_level(self):
        """认不出阶段的材料归到角色等级（compute 的主用途），而不是丢掉。"""
        requirements, _ = mys_calculator.parse_compute(
            {"item_list": [{"item_id": 104001, "num": 100}]}
        )

        self.assertEqual(requirements[0]["phases"], ["character_level"])

    def test_buckets_are_kept_apart_and_summed_in_the_flat_view(self):
        """三个桶是**三笔互不重叠的花费**（实测：摩拉在等级/天赋/武器桶里分别是
        1,099,365 / 4,257,500 / 746,165）。所以：
          · `by_phase[阶段]` 必须是**这一桶自己的量**；
          · 扁平视图里同一材料跨阶段**相加**（那才是"练完一共要多少摩拉"）。
        以前这里按 item_id 取最大值，三个桶全变成 4,257,500 —— 总量翻三倍，
        而且每个阶段的数字都是错的（属于"看着像对的、其实全错"）。"""
        payload = {
            "avatar_consume": [{"id": 202, "name": "摩拉", "num": 1099365}],
            "avatar_skill_consume": [{"id": 202, "name": "摩拉", "num": 4257500}],
            "weapon_consume": [{"id": 202, "name": "摩拉", "num": 746165}],
        }

        requirements, by_phase = mys_calculator.parse_compute(payload)

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]["required"], 1099365 + 4257500 + 746165)
        self.assertEqual(requirements[0]["phases"],
                         ["character_level", "weapon_level", "talent"])
        self.assertEqual([row["required"] for row in by_phase["character_level"]], [1099365])
        self.assertEqual([row["required"] for row in by_phase["weapon_level"]], [746165])
        self.assertEqual([row["required"] for row in by_phase["talent"]], [4257500])

    def test_lack_num_becomes_owned_and_missing(self):
        """`lack_num` 是米游社按**真实背包**算的"还差多少" —— 有值就必须用它。

        玩家给的实测样本：奥黛塔的「幻造晶鳞石」`num=62, lack_num=13`
        （也就是已有 49）。我们以前完全忽略这个字段，界面永远显示"已有 未知"。
        """
        payload = {
            "avatar_consume": [{"id": 202, "name": "摩拉", "num": 1000, "lack_num": 0}],
            "avatar_skill_consume": [
                {"id": 112148, "name": "幻造晶鳞石", "num": 62, "lack_num": 13},
            ],
        }

        requirements, by_phase = mys_calculator.parse_compute(payload)
        by_id = {row["item_id"]: row for row in requirements}

        self.assertEqual(by_id[112148]["required"], 62)
        self.assertEqual(by_id[112148]["lack"], 13)
        self.assertEqual(by_id[112148]["owned"], 49)          # 62 - 13
        self.assertEqual(by_id[202]["owned"], 1000)           # lack=0 → 已有足够
        self.assertEqual(by_phase["talent"][0]["owned"], 49)

    def test_all_zero_lack_num_means_unknown_not_complete(self):
        """⚠️ 全是 0 时**不能**当成"你都不缺" —— 那是我们最怕的"假完成"。

        实测：我们的请求长期拿到全 0（米游社只在它认得背包上下文时才算），
        这时候必须退回"按总需求算"，并让界面显示"已有 未知"。
        """
        payload = {"avatar_consume": [{"id": 202, "name": "摩拉", "num": 1000, "lack_num": 0}]}

        requirements, _ = mys_calculator.parse_compute(payload)

        self.assertIsNone(requirements[0]["owned"])           # None = 不知道
        self.assertIsNone(requirements[0]["lack"])            # 也别写 0（那会被读成"不缺"）

    def test_available_material_turns_into_owned_and_lack(self):
        """`/v3/batch_compute` 的 `available_material` = **你已有多少** → 还差 = 所需 - 已有。

        实测（奥黛塔）：「慈爱」的指引 所需 21 / 已有 16 → 还差 5；教导 所需 3 / 已有 9 → 还差 0。
        米游社只给它认得的那几种材料，**缺席不等于 0**（缺席 = 不知道，界面要显示"未知"）。
        """
        payload = {
            "avatar_skill_consume": [
                {"id": 104366, "name": "「慈爱」的指引", "num": 21, "lack_num": 0},
                {"id": 104365, "name": "「慈爱」的教导", "num": 3, "lack_num": 0},
                {"id": 112148, "name": "幻造晶鳞石", "num": 62, "lack_num": 0},
            ],
            "available_material": [{"id": 104366, "name": "「慈爱」的指引", "num": 16},
                                   {"id": 104365, "name": "「慈爱」的教导", "num": 9}],
        }

        requirements, _ = mys_calculator.parse_compute(payload)
        by_id = {row["item_id"]: row for row in requirements}

        self.assertEqual((by_id[104366]["owned"], by_id[104366]["lack"]), (16, 5))
        self.assertEqual((by_id[104365]["owned"], by_id[104365]["lack"]), (3, 0))
        # 没出现在 available_material 里 → 不知道（不能当成 0，也不能当成"够用"）
        self.assertIsNone(by_id[112148]["owned"])

    def test_available_material_is_not_parsed_as_a_requirement(self):
        """`available_material` 的形状和材料记录一样（`{id,name,num}`），必须跳过。

        混进来的话，界面上会凭空多出「已有 9 个教导」这种"需求"，凭空多刷一轮。
        """
        payload = {
            "avatar_skill_consume": [{"id": 104365, "name": "「慈爱」的教导", "num": 3}],
            "available_material": [{"id": 104365, "name": "「慈爱」的教导", "num": 9},
                                   {"id": 999999, "name": "只存在于背包里的材料", "num": 5}],
        }

        requirements, _ = mys_calculator.parse_compute(payload)

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]["item_id"], 104365)
        self.assertEqual(requirements[0]["required"], 3)      # 需求来自 consume 桶，不是 9

    def test_v3_batch_uses_overall_lack_and_phase_num(self):
        """v3 阶段桶的 num 是需求，overall_consume.lack_num 是账号缺口。"""
        payload = {
            "avatar_skill_consume": [
                {"id": 104366, "name": "「慈爱」的指引", "num": 5, "lack_num": 21},
                {"id": 104367, "name": "「慈爱」的哲学", "num": 76, "lack_num": 64},
            ],
            "overall_consume": [
                {"id": 104366, "name": "「慈爱」的指引", "num": 5, "lack_num": 5},
                {"id": 104367, "name": "「慈爱」的哲学", "num": 76, "lack_num": 64},
            ],
        }

        requirements, _ = mys_calculator.parse_compute(payload)
        rows = {row["item_id"]: row for row in requirements}

        self.assertEqual(
            (rows[104366]["lack"], rows[104366]["required"], rows[104366]["owned"]),
            (5, 5, 0),
        )
        self.assertEqual(
            (rows[104367]["lack"], rows[104367]["required"], rows[104367]["owned"]),
            (64, 76, 12),
        )

    def test_same_item_in_one_bucket_takes_the_max(self):
        """同一个桶里重复上报同一材料时取最大值（那是真的重复，不是两笔花费）。"""
        payload = {
            "avatar_consume": [{"id": 202, "name": "摩拉", "num": 100},
                               {"id": 202, "name": "摩拉", "num": 250}],
        }

        requirements, by_phase = mys_calculator.parse_compute(payload)

        self.assertEqual(requirements[0]["required"], 250)
        self.assertEqual(by_phase["character_level"][0]["required"], 250)

    def test_names_and_categories_are_filled_in(self):
        requirements, _ = mys_calculator.parse_compute(
            {"avatar_level": {"item_list": [{"item_id": 104001, "num": 100}]}}
        )

        # 名字缺了也要有分类（靠 item_id 段判），否则材料分组会全是"其它"
        self.assertEqual(requirements[0]["category"], "currency")

    def test_snake_case_and_camel_case_keys_both_recognised(self):
        payload = {
            "avatarLevel": {"itemList": [{"itemId": 104001, "count": 20}]},
            "avatar_skill": {"cost": [{"material_id": 112001, "quantity": 8}]},
        }

        requirements, _ = mys_calculator.parse_compute(payload)
        by_id = {row["item_id"]: row for row in requirements}

        self.assertEqual(by_id[104001]["required"], 20)
        self.assertEqual(by_id[112001]["phases"], ["talent"])

    def test_broken_payload_yields_nothing_instead_of_raising(self):
        for payload in (None, {}, {"x": None}, {"item_list": []}, "字符串"):
            requirements, by_phase = mys_calculator.parse_compute(payload)
            self.assertEqual(requirements, [])
            self.assertEqual(by_phase["talent"], [])


class ParseAvatarTests(unittest.TestCase):
    def test_parses_avatars_with_skills_and_weapon(self):
        payload = {"avatars": [{
            "id": 10000089, "name": "胡桃", "level": 80, "rarity": 5, "element": "Pyro",
            "skills": [{"skill_id": 10891, "level": 8}, {"skill_id": 10892, "level": 6}],
            "weapon": {"id": 12512, "name": "护摩之杖", "level": 80},
        }]}

        avatars = mys_calculator.parse_avatars(payload)

        self.assertEqual(len(avatars), 1)
        self.assertEqual(avatars[0]["id"], 10000089)
        self.assertEqual(avatars[0]["skills"], {"10891": 8, "10892": 6})
        self.assertEqual(avatars[0]["weapon"]["level"], 80)

    def test_accepts_list_nested_in_dict(self):
        payload = {"data": {"list": [{"id": 10000032, "name": "班尼特"}]}}

        avatars = mys_calculator.parse_avatars(payload)

        self.assertEqual(avatars[0]["id"], 10000032)
        self.assertEqual(avatars[0]["name"], "班尼特")

    def test_catalog_avatar_level_is_rarity_not_training_level(self):
        """图鉴的 `avatar_level` 是**基础等级**（五星=5），不能当成练度。

        搞混的后果很具体：拿 5 当"当前等级"，米游社就会算出"从 5 级练到 90 级"的一整套材料
        —— 数字看着正常，其实全错。所以这里 `level` 必须是 0（= 未知），`rarity` 才是 5。
        """
        payload = {"list": [{"id": 10000150, "name": "奥黛塔", "avatar_level": 5,
                             "max_level": 90}]}

        avatar = mys_calculator.parse_avatars(payload)[0]

        self.assertEqual(avatar["rarity"], 5)
        self.assertEqual(avatar["base_level"], 5)
        self.assertEqual(avatar["level"], 0)
        self.assertEqual(avatar["max_level"], 90)

    def test_real_skill_list_shape(self):
        """实测的 `skill_list` 形状：`{id, name, pos_name, max_level, is_proud}`。

        只有能升级的战斗天赋算天赋（`max_level > 1`）；命座/被动（`max_level == 1`）不算。
        """
        payload = {"list": [{
            "id": 10000150, "name": "奥黛塔", "avatar_level": 5, "max_level": 90,
            "skill_list": [
                {"id": 11501, "name": "普通攻击·xxx", "pos_name": "普通攻击",
                 "max_level": 10, "is_proud": False},
                {"id": 11502, "name": "元素战技", "pos_name": "元素战技",
                 "max_level": 10, "is_proud": False},
                {"id": 11505, "name": "元素爆发", "pos_name": "元素爆发",
                 "max_level": 10, "is_proud": False},
                {"id": 15021, "name": "命座", "pos_name": "", "max_level": 1, "is_proud": True},
            ],
        }]}

        avatar = mys_calculator.parse_avatars(payload)[0]
        talents = mys_calculator.parse_talents(avatar["_raw"])
        slots = mys_calculator.talents_by_slot(avatar["_raw"])

        self.assertEqual([t["id"] for t in talents], [11501, 11502, 11505])   # 命座被排除
        self.assertEqual(slots["normal"]["id"], 11501)
        self.assertEqual(slots["skill"]["id"], 11502)
        self.assertEqual(slots["burst"]["id"], 11505)
        # 图鉴没有等级 → skills 是空的（表示"未知"），不许编一个 1 出来
        self.assertEqual(avatar["skills"], {})

    def test_slot_mapping_does_not_depend_on_id_order(self):
        """槽位由米游社自己给（`pos_name`），不靠"id 升序"猜 —— 顺序打乱也要对。"""
        raw = {"skill_list": [
            {"id": 90003, "pos_name": "元素爆发", "max_level": 10},
            {"id": 90001, "pos_name": "普通攻击", "max_level": 10},
            {"id": 90002, "pos_name": "元素战技", "max_level": 10},
        ]}

        slots = mys_calculator.talents_by_slot(raw)

        self.assertEqual([slots[key]["id"] for key in ("normal", "skill", "burst")],
                         [90001, 90002, 90003])

    def test_skill_map_uses_the_slot_mapping(self):
        avatar = {
            "id": 10000150,
            "skills": {"90001": 8, "90002": 8, "90003": 8},
            "_raw": {"skill_list": [
                {"id": 90003, "pos_name": "元素爆发", "max_level": 10},
                {"id": 90001, "pos_name": "普通攻击", "max_level": 10},
                {"id": 90002, "pos_name": "元素战技", "max_level": 10},
            ]},
        }

        mapping = mys_calculator.skill_map_for_targets(
            avatar, {"normal_target": 10, "skill_target": 9, "burst_target": 8}
        )

        self.assertEqual(mapping["90001"]["target"], 10)
        self.assertEqual(mapping["90002"]["target"], 9)
        self.assertEqual(mapping["90003"]["target"], 8)

    def test_entries_without_an_id_are_skipped(self):
        avatars = mys_calculator.parse_avatars({"avatars": [{"name": "没有 id"}, "坏数据"]})

        self.assertEqual(avatars, [])

    def test_find_avatar_by_id(self):
        payload = {"avatars": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]}

        self.assertEqual(mys_calculator.find_avatar(payload, 2)["name"], "B")
        self.assertIsNone(mys_calculator.find_avatar(payload, 3))

    def test_skill_ids_are_sorted_numerically(self):
        """按 id 升序 = 普攻 → 战技 → 爆发（按名字排会乱）。"""
        avatar = {"skills": {"10895": 8, "10891": 8, "10892": 8}}

        self.assertEqual(mys_calculator.skill_ids_from_avatar(avatar),
                         ["10891", "10892", "10895"])


class ItemListTests(unittest.TestCase):
    """背包条目：字段别名要都认（米游社改过字段名）。"""

    def test_extracts_nested_item_list(self):
        payload = {"item_list": [{"item_id": 104001, "num": 500}], "other": 1}

        items = mys_calculator.extract_item_list(payload)

        self.assertEqual(len(items), 1)

    def test_extracts_deeply_nested_items(self):
        payload = {"data": {"buckets": [{"items": [{"item_id": 1, "num": 2}]}]}}

        self.assertEqual(len(mys_calculator.extract_item_list(payload)), 1)

    def test_alias_field_names(self):
        payload = {"list": [{"id": 10, "count": 5}, {"material_id": 11, "quantity": 6}]}

        items = mys_calculator.extract_item_list(payload)

        self.assertEqual(len(items), 2)

    def test_empty_or_broken_payload_is_safe(self):
        for payload in (None, {}, [], {"item_list": "不是数组"}):
            self.assertEqual(mys_calculator.extract_item_list(payload), [])


class NoCookieTests(unittest.TestCase):
    """没配 cookie 时**一次请求都不发**（和 mys_api 的策略保持一致）。"""

    def test_fetch_avatar_list_raises_not_configured(self):
        with patch.object(config, "MYS_COOKIE", ""):
            with self.assertRaises(mys_api.MysNotConfigured):
                mys_calculator.fetch_avatar_list(uid="100000000")

    def test_fetch_my_items_raises_not_configured(self):
        with patch.object(config, "MYS_COOKIE", ""):
            with self.assertRaises(mys_api.MysNotConfigured):
                mys_calculator.fetch_my_items(uid="100000000")

    def test_compute_raises_not_configured(self):
        with patch.object(config, "MYS_COOKIE", ""):
            with self.assertRaises(mys_api.MysNotConfigured):
                mys_calculator.compute_materials(10000089, 80, 90)


class EndpointTests(unittest.TestCase):
    """端点与查询参数：这几条**都是实测确认过的**（200 + retcode 0），改它们等于改事实。"""

    def test_paths(self):
        self.assertEqual(mys_calculator.PATH_AVATAR_LIST, "/v1/avatar/list")
        self.assertEqual(mys_calculator.PATH_COMPUTE, "/v2/compute")
        self.assertEqual(mys_calculator.PATH_MY_ITEMS, "/v1/sync/avatar/list")
        # ★ 现在真正在用的两条（2026-09 起）
        self.assertEqual(mys_calculator.PATH_AVATAR_STATE, "/v1/sync/avatar/detail")
        self.assertEqual(mys_calculator.PATH_AVATAR_PLAN, "/v1/avatar_cultivation/detail")

    def test_retired_my_items_endpoint_says_so_instead_of_pretending(self):
        """背包/角色列表那条接口已经废了：配了 cookie 也要明确说"接口没了"。

        ⚠️ 以前它返回 -100「未登录」，上层就报成"cookie 失效"，
        玩家会一遍遍去重新扫码 —— 那正是这个用例要挡住的事。
        """
        with patch.object(config, "MYS_COOKIE", "ltuid=1; ltoken=x"):
            with self.assertRaises(mys_api.MysApiError) as caught:
                mys_calculator.fetch_my_items(uid="100000000")

        self.assertIn("avatar/list", str(caught.exception))
        self.assertIn("背包", str(caught.exception))


# ==========================================
# 🌟 单个角色的真实状态（sync/avatar/detail）
# ==========================================

# 实测响应（2026-09，丽莎 10000006）
AVATAR_STATE = {
    "avatar": {"id": 10000006, "name": "丽莎", "avatar_level": 4, "max_level": 90,
               "element_attr_id": 5, "promote_level": 5, "level_current": 71},
    "level": 71,
    "skill_list": [
        {"id": 10060, "group_id": 431, "name": "指尖雷暴", "pos_name": "普通攻击",
         "max_level": 10, "level_current": 1},
        {"id": 10061, "group_id": 432, "name": "苍雷", "pos_name": "元素战技",
         "max_level": 10, "level_current": 4},
    ],
    "weapon": {"id": 14402, "name": "流浪乐章", "level_current": 20, "max_level": 90},
    "reliquary_list": [{"id": 3254, "name": "黄金乐曲的变奏", "level_current": 0}],
}

# 没有这个角色时（实测）：retcode -1 + 这段 message
NOT_OWNED_MESSAGE = "code:[-1002] Msg:[伙伴不存在~]"


class AvatarStateTests(unittest.TestCase):
    def test_parses_the_real_shape(self):
        state = mys_calculator.parse_avatar_state(AVATAR_STATE)

        self.assertTrue(state["owned"])
        self.assertEqual(state["name"], "丽莎")
        self.assertEqual(state["level"], 71)
        self.assertEqual(state["promote_level"], 5)
        self.assertEqual([t["level"] for t in state["talents"]], [1, 4])
        self.assertEqual(state["weapon"]["name"], "流浪乐章")
        self.assertEqual(len(state["reliquary_list"]), 1)

    def test_parses_nested_detail_envelope_without_zeroing_state(self):
        wrapped = {
            "retcode": 0,
            "data": {
                "detail": {
                    "avatar": {
                        "id": 10000904,
                        "name": "哥伦比娅",
                        "avatar_level": 5,
                        "max_level": 90,
                    },
                    "level_current": 90,
                    "skill_list": [
                        {"id": 12531, "group_id": 12531, "level": 6, "max_level": 10},
                        {"id": 12532, "group_id": 12532, "level": 10, "max_level": 10},
                        {"id": 12535, "group_id": 12539, "level": 10, "max_level": 10},
                    ],
                    "weapon": {"id": 14522, "name": "帷间夜曲", "level": 90},
                },
            },
        }

        state = mys_calculator.parse_avatar_state(wrapped, avatar_id=10000904)

        self.assertEqual(state["level"], 90)
        self.assertEqual([talent["level"] for talent in state["talents"]], [6, 10, 10])
        self.assertEqual(state["weapon"]["level"], 90)

    def test_a_character_the_account_does_not_have_is_not_an_error(self):
        """「伙伴不存在」是**正常结果**：整号扫描时靠它区分有没有这个角色。"""
        error = mys_api.MysApiError(-1, NOT_OWNED_MESSAGE)
        with patch.object(mys_calculator, "_request_json", side_effect=error):
            state = mys_calculator.fetch_avatar_state(10000131, uid="1", server="cn_gf01")

        self.assertFalse(state["owned"])
        self.assertEqual(state["level"], 0)
        self.assertEqual(state["talents"], [])

    def test_other_errors_still_raise(self):
        with patch.object(mys_calculator, "_request_json",
                          side_effect=mys_api.MysRiskControl("要验证码")):
            with self.assertRaises(mys_api.MysRiskControl):
                mys_calculator.fetch_avatar_state(10000006, uid="1", server="cn_gf01")

    def test_query_carries_the_account_and_the_avatar(self):
        seen = {}

        def fake_request(method, path, query="", body_obj=None, cookie=None, timeout=20,
                         snapshot_path=None):
            seen.update({"method": method, "path": path, "query": query})
            return AVATAR_STATE

        with patch.object(mys_calculator, "_request_json", fake_request), \
             patch.object(config, "MYS_COOKIE", "ltuid=1; ltoken=x"):
            mys_calculator.fetch_avatar_state(10000006, uid="100000001", server="cn_gf01")

        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["path"], mys_calculator.PATH_AVATAR_STATE)
        self.assertIn("uid=100000001", seen["query"])
        self.assertIn("region=cn_gf01", seen["query"])
        self.assertIn("avatar_id=10000006", seen["query"])
        self.assertIn("game_biz=hk4e_cn", seen["query"])


# ==========================================
# 🌟 养成方案 + 材料需求（avatar_cultivation/detail）
# ==========================================

# 实测响应（2026-09，丽莎 10000006）：天赋 1/1/1 → 10/10/9，材料正好等于
# 两个天赋升到 10 + 一个升到 9 的总和（摩拉 1,652,500×2 + 952,500 = 4,257,500）。
CULTIVATION_PLAN = {
    "detail": {
        "avatar": {"id": 10000006, "name": "丽莎", "avatar_level": 4, "element_attr_id": 5,
                   "weapon_cat_id": 10},
        "current_level": 0,
        "target_level": 0,
        "is_focus_avatar": False,
        "is_finish_skill_upgrade": False,
        "skill_upgrade_list": [
            {"target_level": 10, "priority": 1, "current_level": 1,
             "skill": {"id": 10061, "group_id": 432, "name": "苍雷", "pos_name": "元素战技"}},
            {"target_level": 10, "priority": 2, "current_level": 1,
             "skill": {"id": 10062, "group_id": 439, "name": "蔷薇的雷光", "pos_name": "元素爆发"}},
            {"target_level": 9, "priority": 3, "current_level": 1,
             "skill": {"id": 10060, "group_id": 431, "name": "指尖雷暴", "pos_name": "普通攻击"}},
        ],
        "skill_consume_materials": [
            {"id": 202, "name": "摩拉", "num": 4257500, "lack_num": 0, "level": 3},
            {"id": 104319, "name": "智识之冕", "num": 2, "lack_num": 0, "level": 5},
            {"id": 104309, "name": "「诗文」的哲学", "num": 98, "lack_num": 0, "level": 4},
        ],
        "lack_materails": [],       # 米游社自己的字段名笔误（少个 i）
        "lineup_recommend": [],
        "weapon_recommend": [],
    }
}


class AvatarPlanTests(unittest.TestCase):
    def test_parses_targets_and_materials(self):
        plan = mys_calculator.parse_avatar_plan(CULTIVATION_PLAN)

        self.assertEqual(plan["character_name"], "丽莎")
        self.assertEqual([(s["pos_name"], s["current_level"], s["target_level"])
                          for s in plan["skills"]],
                         [("元素战技", 1, 10), ("元素爆发", 1, 10), ("普通攻击", 1, 9)])
        self.assertEqual([m["item_name"] for m in plan["materials"]],
                         ["摩拉", "「诗文」的哲学", "智识之冕"])   # 按需求量倒序
        self.assertEqual(plan["materials"][0]["required"], 4257500)
        self.assertEqual(plan["materials"][0]["rarity"], 3)

    def test_requirements_are_filed_under_the_talent_phase(self):
        """接口字段就叫 `skill_consume_materials`（技能消耗）—— 归到天赋阶段是有依据的。"""
        plan = mys_calculator.parse_avatar_plan(CULTIVATION_PLAN)
        requirements, by_phase = mys_calculator.plan_requirements(plan)

        self.assertEqual(len(requirements), 3)
        self.assertEqual(len(by_phase[growth_models.PHASE_TALENT]), 3)
        self.assertEqual(by_phase[growth_models.PHASE_CHARACTER_LEVEL], [])
        self.assertTrue(all(entry["phases"] == [growth_models.PHASE_TALENT]
                            for entry in requirements))
        self.assertTrue(all(entry["source"] == "mys_plan" for entry in requirements))

    def test_materials_without_a_count_are_dropped(self):
        payload = {"detail": {"avatar": {"id": 1, "name": "X"},
                              "skill_consume_materials": [
                                  {"id": 0, "name": "没有 id", "num": 5},
                                  {"id": 9, "name": "数量为 0", "num": 0},
                                  {"id": 10, "name": "正常", "num": 3}]}}
        plan = mys_calculator.parse_avatar_plan(payload)

        self.assertEqual([m["item_name"] for m in plan["materials"]], ["正常"])

    def test_empty_plan_yields_no_requirements(self):
        """米游社给不出材料时必须是**空**，不能编出一份假需求。"""
        plan = mys_calculator.parse_avatar_plan({"detail": {"avatar": {"id": 1}}})
        requirements, by_phase = mys_calculator.plan_requirements(plan)

        self.assertEqual(plan["materials"], [])
        self.assertEqual(requirements, [])
        self.assertTrue(all(not value for value in by_phase.values()))

    def test_query_carries_the_avatar_id(self):
        seen = {}

        def fake_request(method, path, query="", body_obj=None, cookie=None, timeout=20,
                         snapshot_path=None):
            seen.update({"method": method, "path": path, "query": query})
            return CULTIVATION_PLAN

        with patch.object(mys_calculator, "_request_json", fake_request), \
             patch.object(config, "MYS_COOKIE", "ltuid=1; ltoken=x"):
            mys_calculator.fetch_avatar_plan(10000006, uid="100000001", server="cn_gf01")

        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["path"], mys_calculator.PATH_AVATAR_PLAN)
        self.assertIn("avatar_id=10000006", seen["query"])

    def test_host_is_the_api_domain_not_the_static_one(self):
        """`api-takumi-static.mihoyo.com` 是静态资源域，打上去只会得到 404（实测踩过）。"""
        self.assertEqual(mys_calculator.CALC_HOST, "https://api-takumi.mihoyo.com")

    def test_prefix_is_the_calculator_namespace(self):
        """前缀来自计算器自己前端 bundle 里的 `var m="/event/e20200928calculate"`。"""
        self.assertEqual(mys_calculator.CALC_PREFIX, "/event/e20200928calculate")
        self.assertEqual(mys_calculator.full_path("/v2/compute"),
                         "/event/e20200928calculate/v2/compute")

    def test_full_path_is_idempotent(self):
        already = "/event/e20200928calculate/v1/avatar/list"

        self.assertEqual(mys_calculator.full_path(already), already)

    def test_post_bodies_are_wrapped_in_data(self):
        """计算器的 POST 接口都要 `{"data": {...}}`：实测不包会得到 retcode -500001。"""
        self.assertEqual(mys_calculator._wrap_body({"a": 1}), {"data": {"a": 1}})

    def test_query_contains_the_required_keys(self):
        query = mys_calculator._encode_query(
            mys_calculator._base_query(uid="100000000", server="cn_gf01")
        )

        self.assertIn("game_biz=hk4e_cn", query)
        self.assertIn("uid=100000000", query)
        self.assertIn("region=cn_gf01", query)
        self.assertIn("lang=zh-cn", query)

    def test_server_is_derived_from_uid_when_missing(self):
        query = mys_calculator._encode_query(mys_calculator._base_query(uid="500000001"))

        self.assertIn("region=cn_qd01", query)

    def test_headers_carry_the_ds_signature_and_wiki_marker(self):
        headers = mys_calculator.calc_headers(query="uid=1", cookie="ltuid=1")

        self.assertIn("DS", headers)
        self.assertEqual(headers["x-rpc-wiki_app"], "ys")
        self.assertEqual(headers["x-rpc-language"], "zh-cn")
        self.assertEqual(headers["Cookie"], "ltuid=1")
        # Referer 必须是计算器那一页：米的接口会拿它做来源校验
        self.assertIn("/ys/event/calculator/", headers["Referer"])


if __name__ == "__main__":
    unittest.main()
