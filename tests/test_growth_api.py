"""角色养成的 Studio 接口测试（studio/server.py 的 `/api/growth/*`）。

对着规格书 §23 的接口清单逐个走一遍，重点验证"接口层不会把事情说错"：
  · 目标能建 / 能改 / 能删 / 能排序，等级 1~90 与三项天赋各自独立；
  · 同步失败时明确回 `stale=True` 与可照着做的建议（**不把 Cookie 吐回前端**）；
  · 没有库存就不假装有库存；
  · 没有可执行任务时 `execute` 不返回成功（不能白开一次 BetterGI）。

不联网、不碰玩家真实的 `memory/`：数据库与库存目录都指到临时目录，
米游社的 HTTP 层全部打桩。
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from brain import growth_db, growth_planner
from skills import mys_api, mys_calculator, mys_inventory
from studio import server


class FakeRunner:
    """顶替 AgentRunner（接口测试不需要真的拉 Agent 子进程）。"""

    def __init__(self):
        self._state = "stopped"

    def start(self):
        self._state = "running"
        return {"ok": True, "pid": 1234}

    def stop(self):
        self._state = "stopped"
        return {"ok": True}

    def send(self, text):
        return {"ok": True}

    def snapshot(self, since=0):
        return {"ok": True, "state": self._state, "pid": None, "seq": 0,
                "lines": [], "partial": "", "plan": None, "started_at": None}

    def clear(self):
        return {"ok": True, "seq": 0}


def avatar_payload(character_id=10000089, name="胡桃", level=80, rarity=5):
    return {"avatars": [{
        "id": character_id, "name": name, "level": level, "rarity": rarity, "element": "Pyro",
        "skills": [{"skill_id": 10891, "level": 8}, {"skill_id": 10892, "level": 8},
                   {"skill_id": 10895, "level": 8}],
        "weapon": {"id": 12512, "name": "护摩之杖", "level": 80},
    }]}


class GrowthApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.db = os.path.join(self.root, "growth.db")
        self.env_path = os.path.join(self.root, ".env")
        with open(self.env_path, "w", encoding="utf-8") as handle:
            handle.write("DEFAULT_UID=100000001\n")

        growth_db.close(self.db)
        self.patches = [
            patch.object(config, "GROWTH_DB_PATH", self.db),
            patch.object(config, "GROWTH_MYS_DIR", self.root),
            patch.object(config, "MYS_UID", ""),
            patch.object(config, "DEFAULT_UID", "100000001"),
            patch.object(config, "MYS_COOKIE", "ltuid=100000001; ltoken=SECRETTOKENVALUE"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

        self.app = server.create_app(runner=FakeRunner(), env_path=self.env_path)
        self.app.config["SHUTDOWN_DELAY"] = None
        self.client = self.app.test_client()
        growth_planner.invalidate_cache()

    def tearDown(self):
        growth_db.close(self.db)
        self.tmp.cleanup()

    def add_target(self, character_id=10000089, name="胡桃", rarity=5, **target):
        body = {"character_id": character_id, "character_name": name, "rarity": rarity,
                "target": target or {"level_target": 90}}
        return self.client.post("/api/growth/targets", json=body).get_json()


class GrowthStateApiTests(GrowthApiCase):
    def test_state_endpoint_returns_the_whole_picture(self):
        self.add_target()

        data = self.client.get("/api/growth").get_json()

        self.assertTrue(data["ok"])
        for key in ("plan", "targets", "inventory", "sort_mode", "sync_history",
                    "executions", "sync_summary", "mys"):
            self.assertIn(key, data)
        self.assertEqual(len(data["targets"]), 1)

    def test_state_endpoint_works_with_no_targets_at_all(self):
        data = self.client.get("/api/growth").get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["targets"], [])
        self.assertEqual(data["plan"]["status"], "IDLE")
        self.assertFalse(data["inventory"]["available"])

    def test_state_endpoint_never_leaks_the_cookie(self):
        data = self.client.get("/api/growth").get_json()

        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("SECRETTOKENVALUE", text)
        self.assertTrue(data["mys"]["configured"])
        self.assertIn("…", data["mys"]["masked"])

    def test_phase_labels_and_status_labels_are_shipped_to_the_frontend(self):
        data = self.client.get("/api/growth").get_json()

        self.assertEqual(data["phase_labels"]["character_level"], "角色等级")
        self.assertIn("runnable", data["status_labels"])


class GrowthTargetApiTests(GrowthApiCase):
    def test_create_then_read_back(self):
        created = self.add_target(level_target=81, normal_target=10, skill_target=9, burst_target=8)

        self.assertTrue(created["ok"], created)
        target = created["target"]
        self.assertEqual(target["level_target"], 81)
        self.assertEqual((target["normal_target"], target["skill_target"], target["burst_target"]),
                         (10, 9, 8))
        self.assertEqual(self.client.get("/api/growth/targets").get_json()["targets"][0]["level_target"], 81)

    def test_level_target_accepts_every_value_from_1_to_90(self):
        for level in (1, 50, 81, 90):
            data = self.add_target(level_target=level)
            self.assertEqual(data["target"]["level_target"], level, level)

    def test_out_of_range_values_are_clamped_not_rejected(self):
        data = self.add_target(level_target=0, normal_target=99, burst_target=-3)

        self.assertTrue(data["ok"], data)
        self.assertEqual(data["target"]["level_target"], 1)
        self.assertEqual(data["target"]["normal_target"], 10)
        self.assertEqual(data["target"]["burst_target"], 1)

    def test_update_only_touches_the_sent_fields(self):
        self.add_target(level_target=90, normal_target=10)

        data = self.client.put("/api/growth/targets/10000089",
                               json={"target": {"normal_target": 6}}).get_json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(data["target"]["normal_target"], 6)
        self.assertEqual(data["target"]["level_target"], 90)         # 没传的字段没被清零

    def test_missing_character_id_is_rejected(self):
        data = self.client.post("/api/growth/targets", json={"target": {"level_target": 90}}).get_json()

        self.assertFalse(data["ok"])
        self.assertIn("character_id", data["error"])

    def test_empty_payload_is_rejected(self):
        data = self.client.post("/api/growth/targets",
                                json={"character_id": 10000089, "target": {}}).get_json()

        self.assertFalse(data["ok"])

    def test_delete(self):
        self.add_target()

        data = self.client.delete("/api/growth/targets/10000089").get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(self.client.get("/api/growth/targets").get_json()["targets"], [])

    def test_delete_unknown_character_is_404(self):
        response = self.client.delete("/api/growth/targets/999999")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()["ok"])

    def test_priority_defaults_follow_rarity(self):
        self.add_target(10000089, "胡桃")
        self.add_target(10000032, "班尼特", rarity=4)

        targets = self.client.get("/api/growth/targets").get_json()["targets"]

        self.assertEqual([row["character_name"] for row in targets], ["胡桃", "班尼特"])

    def test_reorder_writes_the_order_and_switches_sort_mode(self):
        self.add_target(10000089, "胡桃")
        self.add_target(10000032, "班尼特")

        data = self.client.post("/api/growth/targets/reorder",
                                json={"order": [10000032, 10000089]}).get_json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(data["sort_mode"], "custom")
        self.assertEqual([row["character_name"] for row in data["targets"]], ["班尼特", "胡桃"])

    def test_reorder_rejects_a_bad_order(self):
        self.assertFalse(self.client.post("/api/growth/targets/reorder", json={}).get_json()["ok"])
        self.assertFalse(self.client.post("/api/growth/targets/reorder",
                                          json={"order": ["abc"]}).get_json()["ok"])

    def test_sort_mode_endpoint(self):
        data = self.client.post("/api/growth/sort-mode", json={"mode": "custom"}).get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["sort_mode"], "custom")
        self.assertFalse(self.client.post("/api/growth/sort-mode",
                                          json={"mode": "胡说"}).get_json()["ok"])

    def test_weapon_flags_are_stored(self):
        data = self.add_target(weapon_enabled=True, weapon_level_target=81)

        self.assertTrue(data["target"]["weapon_enabled"])
        self.assertEqual(data["target"]["weapon_level_target"], 81)

    def test_phase_priorities_are_stored(self):
        data = self.add_target(level_priority=3, weapon_priority=1, talent_priority=2)

        target = data["target"]
        self.assertEqual((target["level_priority"], target["weapon_priority"],
                          target["talent_priority"]), (3, 1, 2))

    def test_bulk_update_changes_all_targets_and_preserves_owned_state(self):
        first = self.add_target(10000089, "胡桃", owned=1, level_target=90)
        self.add_target(10000032, "班尼特", rarity=4, owned=0, level_target=90)

        data = self.client.post("/api/growth/targets/bulk-update", json={
            "target": {
                "level_target": 81,
                "normal_target": 1,
                "skill_target": 8,
                "burst_target": 8,
                "weapon_enabled": True,
                "weapon_level_target": 80,
            },
        }).get_json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(data["updated"], 2)
        targets = {row["character_id"]: row
                   for row in self.client.get("/api/growth/targets").get_json()["targets"]}
        for target in targets.values():
            self.assertEqual(
                (target["level_target"], target["normal_target"],
                 target["skill_target"], target["burst_target"],
                 target["weapon_level_target"]),
                (81, 1, 8, 8, 80),
            )
        self.assertTrue(targets[10000089]["owned"])
        self.assertFalse(targets[10000032]["owned"])


class GrowthCharactersApiTests(GrowthApiCase):
    """「添加培养目标」的候选列表。

    ⚠️ 以前这一步走"战绩"接口（依赖 cookie），它被风控卡着时整个列表就是空的 ——
    玩家看到的是"添加培养目标一个都没有了"。现在改成：
    养成计算器的「我的角色」（能用就用）→ **完整图鉴**（离线缓存，保证不为空）→ 本地记账。
    """

    def test_the_catalog_keeps_the_list_usable_without_the_account_api(self):
        """账号接口挂了也要有候选角色（这是"列表为空"那个 bug 的回归用例）。

        `owned_known` 是新增的口径：米游社的「我的角色」接口已下线，程序**问不到**
        这个号有哪些角色，所以只能标"未知"，由玩家在添加时选「已拥有 / 未拥有」。
        """
        with patch.object(growth_planner, "available_characters",
                          lambda *a, **k: {
                              10000089: dict(avatar_payload()["avatars"][0], owned=True,
                                             owned_known=True),
                              10000002: {"id": 10000002, "name": "神里绫华", "rarity": 5,
                                         "element": "Ice", "level": 0, "owned": False,
                                         "owned_known": False},
                          }):
            data = self.client.get("/api/growth/characters").get_json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(len(data["characters"]), 2)
        # 已确认拥有的排前面，未知的在后
        self.assertEqual(data["characters"][0]["name"], "胡桃")
        self.assertTrue(data["characters"][0]["owned_known"])
        self.assertFalse(data["characters"][1]["owned_known"])
        self.assertIn("未知", data["note"])

    def test_without_any_account_data_the_list_says_what_it_is(self):
        """"读不到这个号有哪些角色"必须说清楚，而不是让列表看起来是坏的。"""
        with patch.object(growth_planner, "available_characters",
                          lambda *a, **k: {10000002: {"id": 10000002, "name": "神里绫华",
                                                      "rarity": 5, "level": 0, "owned": False,
                                                      "owned_known": False}}):
            data = self.client.get("/api/growth/characters").get_json()

        self.assertTrue(data["ok"])
        self.assertFalse(data["characters"][0]["owned_known"])
        self.assertIn("未知", data["note"])

    def test_a_planner_failure_is_reported_not_raised(self):
        with patch.object(growth_planner, "available_characters",
                          side_effect=mys_api.MysRiskControl("要验证码")):
            data = self.client.get("/api/growth/characters?refresh=1").get_json()

        self.assertFalse(data["ok"])
        self.assertIn("验证码", data["error"])


class GrowthSyncApiTests(GrowthApiCase):
    def _payload(self):
        return {"uid": "100000001", "region": "cn_gf01",
                "item_list": [{"item_id": 104001, "num": 500000, "name": "摩拉"},
                              {"item_id": 100092, "num": 20, "name": "清心"}]}

    def test_successful_sync_reports_counts_and_delta(self):
        with patch.object(mys_calculator, "fetch_my_items", return_value=self._payload()):
            data = self.client.post("/api/growth/sync", json={}).get_json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(data["item_count"], 2)
        self.assertTrue(data["inventory"]["available"])
        self.assertEqual(data["inventory"]["uid"], "100000001")

    def test_second_sync_reports_what_changed(self):
        with patch.object(mys_calculator, "fetch_my_items", return_value=self._payload()):
            self.client.post("/api/growth/sync", json={})

        grown = {"uid": "100000001", "region": "cn_gf01",
                 "item_list": [{"item_id": 104001, "num": 500000, "name": "摩拉"},
                               {"item_id": 100092, "num": 87, "name": "清心"}]}
        with patch.object(mys_calculator, "fetch_my_items", return_value=grown):
            data = self.client.post("/api/growth/sync", json={}).get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["deposited"][0]["name"], "清心")
        self.assertEqual(data["deposited"][0]["delta"], 67)

    def test_backpack_api_being_gone_is_reported_as_skipped_not_failed(self):
        """cookie 完整、接口却说"未登录" → 报"这个数据源不用了"（`skipped`），**不当失败**。

        ⚠️ 为什么必须区分：材料的「已有 / 还差」现在由养成计算器给
        （`/api/growth/plan` 的 `known_inventory`），这条老接口拿不到**不是问题**。
        以前每次都弹红字 + 往历史里塞一条失败，界面上「失败 50 次」全是它。
        """
        with patch.object(mys_calculator, "fetch_my_items", return_value=self._payload()):
            first = self.client.post("/api/growth/sync", json={}).get_json()
        self.assertTrue(first["ok"], first)

        with patch.object(mys_calculator, "fetch_my_items",
                          side_effect=mys_api.MysAuthError("cookie 已过期")):
            data = self.client.post("/api/growth/sync", json={}).get_json()

        self.assertFalse(data["ok"])
        self.assertEqual(data["error_kind"], "backpack_unavailable")
        self.assertTrue(data["skipped"])            # 前端据此不弹红字
        self.assertEqual(data["advice"], "")        # 也不再给一长串"怎么办"

    def test_no_cookie_returns_a_clear_message(self):
        with patch.object(config, "MYS_COOKIE", ""):
            data = self.client.post("/api/growth/sync", json={}).get_json()

        self.assertFalse(data["ok"])
        self.assertEqual(data["error_kind"], "not_configured")
        self.assertIn("MYS_COOKIE", data["error"])

    def test_sync_never_leaks_the_cookie(self):
        with patch.object(mys_calculator, "fetch_my_items",
                          side_effect=mys_api.MysAuthError("cookie 已过期")):
            data = self.client.post("/api/growth/sync", json={}).get_json()

        self.assertNotIn("SECRETTOKENVALUE", json.dumps(data, ensure_ascii=False))

    def test_sync_failure_never_writes_a_zero_inventory(self):
        """接口改版导致解析不到材料时，绝不能写出一份"全是 0"的库存。"""
        with patch.object(mys_calculator, "fetch_my_items", return_value={"item_list": []}):
            data = self.client.post("/api/growth/sync", json={}).get_json()

        self.assertFalse(data["ok"])
        self.assertIsNone(mys_inventory.load_inventory())


class GrowthPlanApiTests(GrowthApiCase):
    def test_plan_get_works_without_any_target(self):
        data = self.client.get("/api/growth/plan").get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["plan"]["status"], "IDLE")

    def test_plan_post_can_sync_first(self):
        with patch.object(mys_calculator, "fetch_my_items", return_value={
            "uid": "100000001", "item_list": [{"item_id": 100092, "num": 20, "name": "清心"}]}):
            data = self.client.post("/api/growth/plan", json={"sync": True}).get_json()

        self.assertTrue(data["ok"], data)
        self.assertTrue(data["sync"]["ok"])
        self.assertIn("lines", data)

    def test_plan_post_reports_a_failed_sync_without_failing_the_plan(self):
        with patch.object(mys_calculator, "fetch_my_items",
                          side_effect=mys_api.MysTransportError("断网")):
            data = self.client.post("/api/growth/plan", json={"sync": True}).get_json()

        self.assertTrue(data["ok"])
        self.assertFalse(data["sync"]["ok"])
        self.assertIn("断网", data["sync"]["error"])

    def test_plan_lines_are_included_for_the_console(self):
        self.add_target()

        data = self.client.post("/api/growth/plan", json={}).get_json()

        self.assertIn("养成计划", "\n".join(data["lines"]))

    def test_inventory_refresh_endpoint_does_not_touch_the_network(self):
        with patch.object(mys_calculator, "fetch_my_items") as fake:
            data = self.client.post("/api/growth/inventory/refresh", json={}).get_json()

        fake.assert_not_called()
        self.assertTrue(data["ok"])


class GrowthExecuteApiTests(GrowthApiCase):
    def test_execute_without_a_runnable_task_does_not_start_bettergi(self):
        """没有可执行任务时必须**明确拒绝**：否则会白开一次 BetterGI。"""
        self.add_target()

        with patch("skills.bgi_controller.execute_bgi_task") as fake:
            # 固定成"一次性执行"，别跟着玩家 .env 里的 all/stepwise 变
            with patch("brain.execution_queue.is_stepwise", return_value=False):
                data = self.client.post("/api/growth/plan/execute", json={}).get_json()

        self.assertFalse(data["ok"])
        self.assertIn("可执行", data["error"])
        fake.assert_not_called()
        self.assertIn("blockers", data)

    def test_execute_without_a_runnable_task_in_stepwise_mode(self):
        """分批次模式下同一条拒绝路径：形状要一致（也带 blockers）。"""
        self.add_target()

        with patch("skills.bgi_controller.execute_bgi_task") as fake:
            with patch("brain.execution_queue.is_stepwise", return_value=True), \
                 patch("brain.execution_queue.current", return_value=None), \
                 patch("brain.execution_queue.start",
                       return_value={"steps": [], "cursor": 0, "active": False}):
                data = self.client.post("/api/growth/plan/execute", json={}).get_json()

        self.assertFalse(data["ok"])
        self.assertTrue(data["stepwise"])
        self.assertIn("可执行", data["error"])
        self.assertIn("blockers", data)
        fake.assert_not_called()

    def test_execute_dispatches_the_command_in_the_background(self):
        self.add_target()
        requirement = {"item_id": 104001, "item_name": "摩拉", "category": "currency",
                       "required": 500000, "phases": ["character_level"]}

        def fake_plan(**kwargs):
            return {
                "status": "WAIT_CONFIRM", "status_label": "等待确认", "plan_id": 9,
                "current_character_id": 10000089, "current_character_name": "胡桃",
                "current_phase": "character_level", "current_phase_label": "角色等级",
                "tasks": [{"task_type": "leyline", "task_label": "地脉花",
                           "character_id": 10000089, "material": "摩拉", "item_id": 104001,
                           "missing": 500000, "status": "runnable", "status_label": "可执行",
                           "route": "藏金之花"}],
                "execution": {"tasks": [{"character_id": 10000089}], "fallback": False, "note": ""},
                "bettergi_cmd": {"energy_task": {"action": "run_leyline", "target": "藏金之花",
                                                 "count": 6}},
                "blockers": [], "summary": {}, "inventory": {}, "characters": [], "phases": [],
                "missing_overview": [], "resin": {}, "compute_errors": [],
                "requirements_placeholder": requirement,
            }

        calls = []

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None, name=None):
                self._target, self._args = target, args

            def start(self):
                calls.append(self._args)

        with patch.object(growth_planner, "build_plan", side_effect=fake_plan), \
             patch("skills.bgi_controller.execute_bgi_task") as executor, \
             patch("threading.Thread", FakeThread):
            # ⚠️ 这些接口的行为受**玩家自己的 .env** 影响（执行方式 all/stepwise）。
            #    测试不该跟着玩家的配置变，所以这里显式固定成"一次性全部执行"。
            with patch("brain.execution_queue.is_stepwise", return_value=False):
                data = self.client.post("/api/growth/plan/execute",
                                        json={"decision": "y"}).get_json()

        self.assertTrue(data["ok"], data)
        self.assertTrue(data["started"])
        self.assertEqual(data["bettergi_cmd"]["energy_task"]["target"], "藏金之花")
        self.assertEqual(len(calls), 1)                     # 后台线程拿到了一次执行任务

    def test_execute_rejects_an_unknown_decision(self):
        response = self.client.post("/api/growth/plan/execute", json={"decision": "nope"})

        self.assertEqual(response.status_code, 400)


class GrowthHistoryApiTests(GrowthApiCase):
    def test_history_endpoint_shape(self):
        data = self.client.get("/api/growth/history").get_json()

        self.assertTrue(data["ok"])
        for key in ("sync", "sync_summary", "executions", "snapshots", "plans"):
            self.assertIn(key, data)
        self.assertEqual(data["sync_summary"]["total"], 0)

    def test_history_records_a_sync(self):
        with patch.object(mys_calculator, "fetch_my_items", return_value={
                "uid": "100000001", "item_list": [{"item_id": 100092, "num": 1, "name": "清心"}]}):
            self.client.post("/api/growth/sync", json={})

        data = self.client.get("/api/growth/history").get_json()

        self.assertEqual(data["sync_summary"]["success"], 1)
        self.assertGreater(data["sync"][0]["snapshot_id"], 0)

    def test_history_limit_is_clamped(self):
        data = self.client.get("/api/growth/history?limit=abc").get_json()

        self.assertTrue(data["ok"])


class GrowthToolsTests(GrowthApiCase):
    """Agent 工具层（brain/growth_tools.py）：同一套事实、不给 LLM 编数字的机会。"""

    def setUp(self):
        super().setUp()
        from brain import growth_tools

        self.tools = growth_tools

    def test_get_inventory_without_a_snapshot_says_stale(self):
        data = self.tools.get_inventory()

        self.assertFalse(data["ok"])
        self.assertTrue(data["stale"])
        self.assertEqual(data["items"], [])

    def test_get_inventory_reports_an_unknown_material_instead_of_guessing(self):
        data = self.tools.owned_of("不存在的材料")

        self.assertFalse(data["ok"])
        self.assertEqual(data["count"], 0)
        self.assertIn("没有", data["error"])

    def test_set_and_get_target_by_name(self):
        with patch.object(growth_planner, "fetch_avatars",
                          lambda *a, **k: {10000089: avatar_payload()["avatars"][0]}):
            saved = self.tools.set_growth_target(character_name="胡桃", level=81,
                                                 normal=10, skill=9, burst=8)
            loaded = self.tools.get_growth_target(character_name="胡桃")

        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["target"]["level_target"], 81)
        self.assertEqual(loaded["target"]["talents"], {"normal": 10, "skill": 9, "burst": 8})

    def test_set_target_without_any_field_is_rejected(self):
        data = self.tools.set_growth_target(character_id=10000089)

        self.assertFalse(data["ok"])

    def test_get_target_for_unknown_character_is_rejected(self):
        with patch.object(growth_planner, "fetch_avatars", lambda *a, **k: {}):
            data = self.tools.get_growth_target(character_name="查无此人")

        self.assertFalse(data["ok"])

    def test_get_plan_is_the_single_source_of_truth(self):
        self.add_target(10000089, "胡桃")

        data = self.tools.get_growth_plan()

        self.assertTrue(data["ok"])
        self.assertIn("status", data)
        self.assertIn("tasks", data)

    def test_context_block_is_empty_when_nothing_is_configured(self):
        with patch.object(config, "MYS_COOKIE", ""):
            self.assertEqual(self.tools.growth_context_block(), "")

    def test_context_block_mentions_the_inventory_and_forbids_guessing(self):
        with patch.object(mys_calculator, "fetch_my_items", return_value={
                "uid": "100000001", "item_list": [{"item_id": 100092, "num": 20, "name": "清心"}]}):
            self.client.post("/api/growth/sync", json={})
        self.add_target(10000089, "胡桃")

        block = self.tools.growth_context_block()

        self.assertIn("角色养成系统", block)
        self.assertIn("米游社", block)
        self.assertIn("不要自己估算", block)

    def test_tools_manifest_matches_the_spec(self):
        names = {row["name"] for row in self.tools.tools_manifest()}

        self.assertEqual(names, {
            "mys_sync_inventory", "get_inventory", "get_growth_target", "set_growth_target",
            "calculate_growth_requirements", "get_growth_plan", "refresh_growth_plan",
            "execute_growth_plan",
            # 分批次执行（`GROWTH_EXECUTION_MODE=stepwise`）：看当前这条 / 推进到下一条
            "growth_execution_step",
            # 记当前体力（米游社体力接口被风控时用）
            "set_current_resin",
        })

    def test_calculate_requirements_without_a_target_is_rejected(self):
        with patch.object(growth_planner, "fetch_avatars", lambda *a, **k: {}):
            data = self.tools.calculate_growth_requirements(character_id=10000089)

        self.assertFalse(data["ok"])


class GrowthDoctorTests(GrowthApiCase):
    def test_doctor_reports_the_growth_system(self):
        data = self.client.get("/api/doctor").get_json()

        self.assertTrue(data["ok"])
        text = "\n".join(row["text"] for row in data["rows"])
        self.assertIn("角色养成", text)

    def test_doctor_warns_when_the_inventory_is_missing(self):
        rows = __import__("skills.health_check", fromlist=["x"]).check_growth({})

        self.assertTrue(any("还没同步过" in text for _, text in rows))


class MysLoginApiTests(GrowthApiCase):
    """扫码登录接口（/api/mys/login/*）。

    这一组是"自动拿到带 ltoken 的 cookie"这条路的接口层：不能真的向米游社要二维码，
    所以把 `skills.mys_login` 的建码 / 轮询 / 换取 token 都打桩，只验接口行为与安全边界。
    """

    QR_START = {"url": "https://user.mihoyo.com/login-plane.html?tk=1#/login/qr",
                "ticket": "TICKET-1", "device": "DEV"}

    def setUp(self):
        super().setUp()

    def _isolate_cookie(self):
        """把"进程里现有的 cookie"固定成空。

        ★ `finish_login()` 现在会把新 cookie 与**进程里现有的** cookie **合并**
          （App 扫码给 v1、网页扫码给 v2，合并才不会互相冲掉）。凡是会走到保存的用例
          都必须先把现有值清掉，否则会把你 `.env` 里那份**真实 cookie** 混进来 ——
          既让断言失真，还可能拿真 cookie 去打米游社。
          只给这些用例加，不要放进 setUp：`status` 那几条要靠 GrowthApiCase 设的 cookie。
        """
        patcher = patch.object(config, "MYS_COOKIE", "")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        # 扫码会话是模块级的全局状态，不清理会串到下一条用例
        from skills import mys_login

        mys_login.cancel_qr_login()
        super().tearDown()

    def test_status_reports_completeness(self):
        data = self.client.get("/api/mys/login/status").get_json()

        self.assertTrue(data["ok"])
        status = data["status"]
        self.assertIn("configured", status)
        self.assertIn("complete", status)
        self.assertIn("missing", status)
        # 本用例的 cookie 只有 ltuid + ltoken（GrowthApiCase 里设的），应当算完整
        self.assertTrue(status["complete"], status)

    def test_start_returns_a_scannable_qr(self):
        """起始接口回的是**二维码本身**（SVG）+ 过期秒数，不再开浏览器窗口。"""
        with patch("skills.mys_login.start_qr_login", return_value=dict(self.QR_START)), \
             patch("skills.mys_login.render_qr_svg", return_value="<svg>QR</svg>") as painter:
            data = self.client.post("/api/mys/login/start").get_json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(data["url"], self.QR_START["url"])
        self.assertEqual(data["svg"], "<svg>QR</svg>")
        painter.assert_called_once()
        self.assertIn("扫码", data["note"])
        self.assertIn("expires_in", data)

    def test_start_reports_why_the_qr_failed(self):
        from skills import mys_login

        with patch("skills.mys_login.start_qr_login",
                   side_effect=mys_login.LoginError("米游社没返回二维码")):
            data = self.client.post("/api/mys/login/start").get_json()

        self.assertFalse(data["ok"])
        self.assertIn("没返回二维码", data["error"])

    def test_poll_without_a_qr_tells_you_to_start_one(self):
        data = self.client.get("/api/mys/login/poll").get_json()

        self.assertFalse(data["ok"])
        self.assertEqual(data["state"], "idle")
        self.assertIn("二维码", data["error"])

    def _with_qr(self):
        """造一个"已经要过二维码"的状态，返回打桩用的上下文。"""
        from skills import mys_login

        mys_login._LIVE_QR.update({"ticket": "TICKET-1", "device": "DEV", "url": "",
                                   "state": "pending", "message": ""})

    def test_poll_reports_pending_and_scanned(self):
        from skills import mys_login

        self._with_qr()
        for raw, expected in (("Created", "pending"), ("Scanned", "scanned")):
            with patch("skills.mys_login.query_qr_login",
                       return_value={"status": raw, "data": {}}):
                data = self.client.get("/api/mys/login/poll").get_json()

            self.assertTrue(data["ok"], data)
            self.assertEqual(data["state"], expected, raw)

    def test_poll_reports_expiry_so_the_frontend_can_ask_for_a_new_one(self):
        self._with_qr()
        with patch("skills.mys_login.query_qr_login",
                   return_value={"status": "Expired", "data": {}}):
            data = self.client.get("/api/mys/login/poll").get_json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(data["state"], "expired")
        self.assertIn("换一张", data["note"])

    def test_poll_exchanges_tokens_and_saves_on_confirmation(self):
        self._with_qr()
        confirmed = {"status": "Confirmed", "data": {"tokens": [], "user_info": {}}}
        with patch("skills.mys_login.query_qr_login", return_value=confirmed), \
             patch("skills.mys_login.exchange_tokens",
                   return_value={"cookie": "ltuid=285006984; ltoken=SECRETVALUE",
                                 "ltuid": "285006984", "stoken_present": True}), \
             patch("skills.mys_login.verify_tokens",
                   return_value={"ok": True, "user": {"aid": "285006984"}, "error": ""}), \
             patch("skills.mys_inventory.sync",
                   return_value={"avatars": [{"name": "旅行者"}]}):
            data = self.client.get("/api/mys/login/poll").get_json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(data["state"], "done")
        self.assertNotIn("SECRETVALUE", json.dumps(data, ensure_ascii=False))
        self.assertTrue(os.path.isfile(self.env_path))
        self.assertIn("ltoken=SECRETVALUE", open(self.env_path, encoding="utf-8").read())
        # 扫码图的就是"材料能算出来"，所以登录成功会顺手同步一次库存
        self.assertEqual(data["inventory"], {"ok": True, "avatars": 1, "error": ""})

    def test_poll_still_succeeds_when_the_inventory_sync_fails(self):
        """库存同步失败（风控/断网）不能把"登录成功"这件事一起吞掉。"""
        self._with_qr()
        with patch("skills.mys_login.query_qr_login",
                   return_value={"status": "Confirmed", "data": {}}), \
             patch("skills.mys_login.exchange_tokens",
                   return_value={"cookie": "ltuid=1; ltoken=SECRETVALUE",
                                 "ltuid": "1", "stoken_present": True}), \
             patch("skills.mys_login.verify_tokens",
                   return_value={"ok": True, "user": {}, "error": ""}), \
             patch("skills.mys_inventory.sync", side_effect=RuntimeError("风控了")):
            data = self.client.get("/api/mys/login/poll").get_json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(data["state"], "done")
        self.assertFalse(data["inventory"]["ok"])
        self.assertIn("风控了", data["inventory"]["error"])

    def test_poll_reports_a_failed_verification_without_saving(self):
        self._with_qr()
        with patch("skills.mys_login.query_qr_login",
                   return_value={"status": "Confirmed", "data": {}}), \
             patch("skills.mys_login.exchange_tokens",
                   return_value={"cookie": "ltuid=285006984; ltoken=SECRETVALUE",
                                 "ltuid": "285006984", "stoken_present": True}), \
             patch("skills.mys_login.verify_tokens",
                   return_value={"ok": False, "error": "米游社说不可用", "user": {}}):
            data = self.client.get("/api/mys/login/poll").get_json()

        self.assertFalse(data["ok"])
        self.assertEqual(data["state"], "failed")
        self.assertIn("不可用", data["error"])
        self.assertNotIn("SECRETVALUE", open(self.env_path, encoding="utf-8").read())

    def test_poll_reports_a_token_exchange_failure(self):
        from skills import mys_login

        self._with_qr()
        with patch("skills.mys_login.query_qr_login",
                   return_value={"status": "Confirmed", "data": {}}), \
             patch("skills.mys_login.exchange_tokens",
                   side_effect=mys_login.LoginError("米游社没返回 ltoken")):
            data = self.client.get("/api/mys/login/poll").get_json()

        self.assertFalse(data["ok"])
        self.assertEqual(data["state"], "failed")
        self.assertIn("ltoken", data["error"])

    def test_poll_never_leaks_the_cookie(self):
        self._with_qr()
        with patch("skills.mys_login.query_qr_login",
                   return_value={"status": "Confirmed", "data": {}}), \
             patch("skills.mys_login.exchange_tokens",
                   return_value={"cookie": "ltuid=285006984; ltoken=SECRETVALUE",
                                 "ltuid": "285006984", "stoken_present": True}), \
             patch("skills.mys_login.verify_tokens",
                   return_value={"ok": True, "user": {"aid": "285006984"}, "error": ""}):
            data = self.client.get("/api/mys/login/poll").get_json()

        self.assertNotIn("SECRETVALUE", json.dumps(data, ensure_ascii=False))

    def test_poll_cancel_drops_the_qr_and_saves_nothing(self):
        from skills import mys_login

        self._with_qr()
        data = self.client.get("/api/mys/login/poll?cancel=1").get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["state"], "cancelled")
        self.assertEqual(mys_login.qr_state()["ticket"], "")   # 这张码不再被复用
        self.assertNotIn("MYS_COOKIE=ltoken", open(self.env_path, encoding="utf-8").read())

    def test_cancel_endpoint(self):
        from skills import mys_login

        self._with_qr()
        data = self.client.post("/api/mys/login/cancel").get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(mys_login.qr_state()["ticket"], "")

    def test_manual_cookie_is_verified_then_saved(self):
        self._isolate_cookie()
        with patch("skills.mys_login.verify_tokens",
                   return_value={"ok": True, "user": {"aid": "1"}, "error": ""}):
            data = self.client.post("/api/mys/login/manual",
                                    json={"cookie": "ltuid=1; ltoken=MANUALVALUE"}).get_json()

        self.assertTrue(data["ok"], data)
        self.assertNotIn("MANUALVALUE", json.dumps(data, ensure_ascii=False))
        self.assertIn("ltoken=MANUALVALUE", open(self.env_path, encoding="utf-8").read())

    def test_manual_v2_cookie_is_verified_through_the_calculator(self):
        """v2 只有一套的 cookie（养成计算器网页会话）**不再当场拒绝**，改用计算器接口验。"""
        self._isolate_cookie()
        with patch("skills.mys_login.verify_calculator_cookie",
                   return_value={"ok": True, "user": {"aid": "1"}, "error": "",
                                 "note": "能读到角色"}):
            data = self.client.post("/api/mys/login/manual",
                                    json={"cookie": "ltuid_v2=1; ltoken_v2=ONLYV2"}).get_json()

        self.assertTrue(data["ok"], data)
        self.assertIn("ltoken_v2=ONLYV2", open(self.env_path, encoding="utf-8").read())

    def test_manual_cookie_is_merged_with_the_existing_one(self):
        """★ 粘一份 v2 不该把原来那份 v1 冲掉（反之亦然）—— 两份各管一半功能。"""
        # 合并后是"v1+v2 都在"，`audit.mode` 会判成 app → 走 v1 那条验证（v1 也认计算器）
        with patch.object(config, "MYS_COOKIE", "ltuid=1; ltoken=KEEPME"), \
             patch("skills.mys_login.verify_tokens",
                   return_value={"ok": True, "user": {}, "error": ""}):
            data = self.client.post("/api/mys/login/manual",
                                    json={"cookie": "ltuid_v2=2; ltoken_v2=NEWV2"}).get_json()

        self.assertTrue(data["ok"], data)
        written = open(self.env_path, encoding="utf-8").read()
        self.assertIn("ltoken=KEEPME", written)        # 老的 v1 留着
        self.assertIn("ltoken_v2=NEWV2", written)      # 新的 v2 也写进去了

    def test_manual_cookie_without_any_token_is_rejected(self):
        self._isolate_cookie()
        data = self.client.post("/api/mys/login/manual",
                                json={"cookie": "ltuid=1"}).get_json()

        self.assertFalse(data["ok"])
        self.assertIn("ltoken", data["error"])

    def test_manual_paste_accepts_a_whole_curl_blob(self):
        """粘"Copy as cURL"也要能用（那是从养成计算器网页拿会话最省事的办法）。"""
        self._isolate_cookie()
        curl = ("curl 'https://api-takumi.mihoyo.com/x' \\\n"
                "  -H 'cookie: ltuid=1; ltoken=FROMCURL' -H 'accept: */*'")
        with patch("skills.mys_login.verify_tokens",
                   return_value={"ok": True, "user": {"aid": "1"}, "error": ""}):
            data = self.client.post("/api/mys/login/manual",
                                    json={"cookie": curl}).get_json()

        self.assertTrue(data["ok"], data)
        self.assertIn("ltoken=FROMCURL", open(self.env_path, encoding="utf-8").read())

    def test_manual_empty_cookie_is_rejected(self):
        response = self.client.post("/api/mys/login/manual", json={"cookie": "  "})

        self.assertEqual(response.status_code, 400)

    def test_manual_cookie_file_is_backed_up(self):
        self._isolate_cookie()
        with patch("skills.mys_login.verify_tokens",
                   return_value={"ok": True, "user": {}, "error": ""}):
            self.client.post("/api/mys/login/manual",
                             json={"cookie": "ltuid=1; ltoken=NEWVALUE"})

        backups = [name for name in os.listdir(self.root) if name.startswith(".env.bak")]
        self.assertTrue(backups, os.listdir(self.root))


if __name__ == "__main__":
    unittest.main()
