import datetime
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

import config
from skills import bgi_controller, route_group


def _group(name, projects):
    return {"name": name, "projects": projects}


def _project(folder, filename="route.json", status="Disabled"):
    return {"name": filename, "folderName": folder, "type": "Pathing", "status": status}


class RouteGroupApplyTests(unittest.TestCase):
    def test_matching_routes_are_enabled_and_others_disabled(self):
        group = _group(
            "敌人与魔物",
            [
                _project("蕈兽\\蕈兽@张三", "01-蕈兽.json", "Enabled"),
                _project("骗骗花", "01-骗骗花.json"),
                _project("愚人众", "01-愚人众.json"),
            ],
        )

        result = route_group.apply_targets(group, ["蕈兽"], route_group.ENEMY_MATCH_FIELDS)

        self.assertEqual(result.matched, 1)
        self.assertEqual(result.enabled, 1)
        self.assertEqual(group["projects"][0]["status"], "Enabled")
        self.assertEqual(group["projects"][1]["status"], "Disabled")
        self.assertEqual(group["projects"][2]["status"], "Disabled")

    def test_anti_crash_isolation_band_forces_a_route_every_150_disabled(self):
        # 每连续 150 条 Disabled 后强制打开 1 条：302 条会触发 2 次
        projects = [_project("别的敌人", f"{i:03d}.json") for i in range(302)]
        projects.append(_project("蕈兽", "hit.json"))
        group = _group("敌人与魔物", projects)

        result = route_group.apply_targets(group, ["蕈兽"], route_group.ENEMY_MATCH_FIELDS)

        self.assertEqual(result.matched, 1)
        self.assertEqual(len(result.forced), 2)
        self.assertEqual(result.enabled, 3)

    def test_exactly_150_disabled_routes_do_not_trigger_the_band(self):
        projects = [_project("别的敌人", f"{i:03d}.json") for i in range(150)]
        projects.append(_project("蕈兽", "hit.json"))
        group = _group("敌人与魔物", projects)

        result = route_group.apply_targets(group, ["蕈兽"], route_group.ENEMY_MATCH_FIELDS)

        self.assertEqual(result.forced, ())
        self.assertEqual(result.enabled, 1)

    def test_enemy_fields_also_match_the_script_file_name(self):
        group = _group(
            "敌人与魔物",
            [_project("提瓦特钓鱼玳师", "璃月-蕈兽-沉玉谷-2个.json")],
        )

        result = route_group.apply_targets(group, ["蕈兽"], route_group.ENEMY_MATCH_FIELDS)

        self.assertEqual(result.matched, 1)

    def test_material_reverse_lookup_matches_folder_segment(self):
        """「原素花蜜」→ 电气骗骗花，而组里目录只写「骗骗花\\骗骗花@san」。"""
        group = _group("骗骗花", [_project("骗骗花\\骗骗花@san", "骗骗花-天衡山上.json")])

        result = route_group.apply_targets(
            group,
            ["原素花蜜", "电气骗骗花", "炽热骗骗花"],
            route_group.ENEMY_MATCH_FIELDS,
            both_ways=True,
        )

        self.assertEqual(result.matched, 1)

    def test_forward_only_matching_does_not_do_the_reverse_lookup(self):
        """不带 both_ways 时保持原语义：不会因为「骗骗花」是「电气骗骗花」的子串而误命中。"""
        group = _group("骗骗花", [_project("骗骗花\\骗骗花@san", "骗骗花-天衡山上.json")])

        result = route_group.apply_targets(group, ["电气骗骗花"])

        self.assertEqual(result.matched, 0)

    def test_map_material_fields_still_only_look_at_folder(self):
        """地图素材原有语义不能变：只看 folderName。"""
        group = _group("地图素材", [_project("地方特产\\璃月\\清心", "清心-1个.json")])

        result = route_group.apply_targets(group, ["清心"])

        self.assertEqual(result.matched, 1)


class RouteGroupDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.group_dir = Path(self.temp_dir.name)
        (self.group_dir / "蕈兽.json").write_text(
            json.dumps(_group("蕈兽", [_project("蕈兽\\蕈兽@张三", "01-蕈兽.json")]), ensure_ascii=False),
            encoding="utf-8",
        )
        (self.group_dir / "骗骗花.json").write_text(
            json.dumps(_group("骗骗花", [_project("骗骗花", "01-骗骗花.json")]), ensure_ascii=False),
            encoding="utf-8",
        )
        (self.group_dir / "地图素材.json").write_text(
            json.dumps(_group("地图素材", [_project("地方特产\\璃月\\清心", "清心.json")]), ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_finds_existing_group_by_enemy_name(self):
        hits = route_group.find_groups_for_targets(str(self.group_dir), ["蕈兽"])

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1]["name"], "蕈兽")

    def test_excluded_groups_are_skipped(self):
        hits = route_group.find_groups_for_targets(
            str(self.group_dir), ["清心"], exclude=(str(self.group_dir / "地图素材.json"),)
        )

        self.assertEqual(hits, [])

    def test_available_group_names_lists_what_is_installed(self):
        names = route_group.available_group_names(str(self.group_dir))

        self.assertIn("蕈兽", names)
        self.assertIn("地图素材", names)

    def test_missing_dir_is_not_fatal(self):
        self.assertEqual(route_group.list_group_files(str(self.group_dir / "不存在")), [])
        self.assertIsNone(route_group.load_group(str(self.group_dir / "不存在.json")))


class EnemyDropsMappingTests(unittest.TestCase):
    def test_material_name_maps_back_to_enemies(self):
        with tempfile.TemporaryDirectory() as tmp:
            drops = Path(tmp) / "boss_drops_dict.json"
            drops.write_text(
                json.dumps(
                    {
                        "电气骗骗花": ["原素花蜜", "摩拉"],
                        "蕈兽": ["蕈兽孢子"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                route_group.enemy_names_from_drops("原素花蜜", str(drops)), ["电气骗骗花"]
            )
            self.assertEqual(route_group.enemy_names_from_drops("不存在", str(drops)), [])
            self.assertEqual(route_group.enemy_names_from_drops("", str(drops)), [])

    def test_missing_drops_file_is_not_fatal(self):
        self.assertEqual(route_group.enemy_names_from_drops("原素花蜜", "memory/没有这个文件.json"), [])


class BossToHuntRedirectTests(unittest.TestCase):
    """LLM 会把敌人路线名（异种合成魔兽/圣骸兽）当成 Boss，必须自动改判成 hunt。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.group_dir = Path(self.temp_dir.name)
        (self.group_dir / "敌人与魔物.json").write_text(
            json.dumps(
                _group(
                    "敌人与魔物",
                    [
                        _project("异种合成魔兽\\异种合成魔兽@张三", "01-合成魔兽.json"),
                        _project("圣骸兽", "01-圣骸兽.json"),
                        _project("镀金旅团", "01-镀金旅团.json"),
                    ],
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.preferred = str(self.group_dir / "敌人与魔物.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _find(self, target):
        return route_group.find_enemy_route_target(
            target,
            group_dir=str(self.group_dir),
            preferred_path=self.preferred,
            drops_path=str(self.group_dir / "没有这个字典.json"),
        )

    def _redirect(self, bgi_cmd):
        return route_group.redirect_run_boss_to_hunt(
            bgi_cmd,
            group_dir=str(self.group_dir),
            preferred_path=self.preferred,
            drops_path=str(self.group_dir / "没有这个字典.json"),
        )

    def test_enemy_name_is_recognized_as_route_not_boss(self):
        hit = self._find("异种合成魔兽")

        self.assertIsNotNone(hit)
        self.assertEqual(hit[1]["name"], "敌人与魔物")
        self.assertEqual(hit[2], 1)

    def test_redirect_moves_boss_task_into_hunt(self):
        bgi_cmd = {
            "energy_task": {"action": "run_boss", "target": "异种合成魔兽", "count": 1},
            "free_task": [],
        }

        notice = self._redirect(bgi_cmd)

        self.assertIn("改判", notice)
        self.assertEqual(bgi_cmd["energy_task"], {})
        self.assertEqual(len(bgi_cmd["free_task"]), 1)
        self.assertEqual(bgi_cmd["free_task"][0]["action"], "hunt")
        self.assertEqual(bgi_cmd["free_task"][0]["target"], "异种合成魔兽")

    def test_redirect_keeps_existing_free_tasks(self):
        bgi_cmd = {
            "energy_task": {"action": "run_boss", "target": "圣骸兽"},
            "free_task": [{"action": "gather", "target": "清心"}],
        }

        self._redirect(bgi_cmd)

        self.assertEqual([t["action"] for t in bgi_cmd["free_task"]], ["gather", "hunt"])

    def test_real_boss_is_not_redirected(self):
        bgi_cmd = {"energy_task": {"action": "run_boss", "target": "急冻树"}, "free_task": []}

        self.assertIsNone(self._redirect(bgi_cmd))
        self.assertEqual(bgi_cmd["energy_task"]["target"], "急冻树")

    def test_boss_with_a_similar_enemy_route_is_not_stolen(self):
        """「秘源机兵构型械」是真首领，敌人组里的「秘源机兵」杂兵路线不能把它抢走。"""
        (self.group_dir / "敌人与魔物.json").write_text(
            json.dumps(
                _group("敌人与魔物", [_project("秘源机兵\\秘源机兵@张三", "01.json")]),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        bgi_cmd = {
            "energy_task": {"action": "run_boss", "target": "秘源机兵构型械"},
            "free_task": [],
        }

        self.assertIsNone(self._redirect(bgi_cmd))
        self.assertEqual(bgi_cmd["energy_task"]["target"], "秘源机兵构型械")

    def test_other_actions_are_left_alone(self):
        for action in ("run_domain", "run_leyline", "run_artifact"):
            bgi_cmd = {"energy_task": {"action": action, "target": "圣骸兽"}, "free_task": []}
            self.assertIsNone(self._redirect(bgi_cmd))

    def test_unknown_name_is_not_redirected(self):
        bgi_cmd = {"energy_task": {"action": "run_boss", "target": "随便编的敌人"}, "free_task": []}

        self.assertIsNone(self._redirect(bgi_cmd))


class BossToHuntEndToEndTests(unittest.TestCase):
    """端到端：run_boss + 敌人名 → 一条龙里启用敌人组、不写 Boss 配置。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.group_dir = self.root / "ScriptGroup"
        self.group_dir.mkdir()
        (self.group_dir / "敌人与魔物.json").write_text(
            json.dumps(
                _group(
                    "敌人与魔物",
                    [
                        _project("异种合成魔兽\\异种合成魔兽@张三", "01.json", "Enabled"),
                        _project("圣骸兽", "02.json", "Enabled"),
                    ],
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.group_dir / "地图素材.json").write_text(
            json.dumps(_group("地图素材", [_project("地方特产\\璃月\\清心", "清心.json")]), ensure_ascii=False),
            encoding="utf-8",
        )
        self.one_dragon = self.root / "OneDragon" / "地图素材.json"
        self.one_dragon.parent.mkdir()
        self.one_dragon.write_text(
            json.dumps(
                {
                    "TaskDefinitions": {"uuid-enemy": "敌人与魔物", "uuid-map": "地图素材"},
                    "TaskEnabledList": {"uuid-enemy": False, "uuid-map": True},
                    "TaskOrder": ["uuid-map", "uuid-enemy"],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _execute(self, bgi_cmd, agent_tasks=(), enabled_tasks=None, target="open_id", sent=None):
        """跑一遍真实的 execute_bgi_task（测试模式），返回写入后的一条龙配置。

        `sent` 传一个列表时，顺手把发给该通道的消息收集起来（用于断言"QQ 上只发几条"）。
        """
        state_path = str(self.root / "memory" / "agent_registered_tasks.json")
        if sent is not None:
            feishu_patch = patch.object(
                bgi_controller.feishu_api, "send_feishu_msg",
                lambda channel, text: sent.append(text) or True,
            )
        else:
            feishu_patch = patch.object(bgi_controller.feishu_api, "send_feishu_msg")
        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            config, "BGI_ENEMY_CONFIG", str(self.group_dir / "敌人与魔物.json")
        ), patch.object(
            config, "BGI_MAP_CONFIG", str(self.group_dir / "地图素材.json")
        ), patch.object(
            config, "BGI_BACKUP_DIR", str(self.root / "backups")
        ), patch.object(
            config, "BGI_AUTO_CREATE_ROUTE_GROUP", False
        ), patch.object(
            config, "BGI_AUTO_PATHING_DIR", str(self.root / "AutoPathing")
        ), patch.object(
            config, "AGENT_TASK_STATE_PATH", state_path
        ), patch.object(
            bgi_controller, "resolve_one_dragon_config_path", return_value=str(self.one_dragon)
        ), patch.object(
            bgi_controller, "ensure_bettergi_closed", return_value=True
        ), feishu_patch:
            bgi_controller.save_agent_tasks(
                list(agent_tasks), state_path, enabled=enabled_tasks
            )
            bgi_controller.execute_bgi_task(bgi_cmd, "t", {}, target, "100000000")
            state = bgi_controller.load_agent_tasks(state_path)
            enabled_state = bgi_controller.load_agent_enabled_tasks(state_path)
        return json.loads(self.one_dragon.read_text(encoding="utf-8")), state, enabled_state

    def _add_task(self, task_id, name, enabled=True):
        """往玩家自己的一条龙里塞一个已登记任务（模拟"玩家本来就挂着这个任务"）。"""
        written = json.loads(self.one_dragon.read_text(encoding="utf-8"))
        written["TaskDefinitions"][task_id] = name
        written["TaskEnabledList"][task_id] = enabled
        written["TaskOrder"].append(task_id)
        self.one_dragon.write_text(
            json.dumps(written, ensure_ascii=False), encoding="utf-8"
        )
        return written

    @staticmethod
    def _enabled_names(written):
        definitions = written.get("TaskDefinitions") or {}
        return {
            definitions.get(key, key)
            for key, value in (written.get("TaskEnabledList") or {}).items()
            if value
        }

    def test_qq_target_gets_no_config_receipt(self):
        """玩家要求：QQ 上点 y 之后只留「✅ 已确认 BetterGI 开始执行任务。」

        配置回执（🧾 配置已写入 / 🗂️ 备份与 Diff）不再发到聊天通道 ——
        回滚 ID 在终端与 Studio 日志里，CLI 的 rollback 也会自己列事务。
        """
        from api import channel_router

        channel_router.clear()
        self.addCleanup(channel_router.clear)
        channel_router.register_chat_channel("qq:")
        sent = []

        self._execute(
            {"energy_task": {}, "free_task": [{"action": "hunt", "target": "异种合成魔兽"}]},
            target="qq:c2c:U1#M1",
            sent=sent,
        )

        joined = "\n".join(sent)
        self.assertNotIn("配置已写入", joined)
        self.assertNotIn("配置事务已提交", joined)
        self.assertNotIn("备份与 Diff", joined)

    def test_computer_target_still_gets_the_config_receipt(self):
        sent = []

        self._execute(
            {"energy_task": {}, "free_task": [{"action": "hunt", "target": "异种合成魔兽"}]},
            target="ou_me",
            sent=sent,
        )

        joined = "\n".join(sent)
        self.assertIn("配置事务已提交", joined)
        self.assertIn("备份与 Diff", joined)

    def test_agent_registered_task_is_retired_when_not_requested_again(self):
        """上一轮 Agent 自己加的「圣骸兽」这一轮没排 → 摘掉，避免下次一条龙莫名多跑一套。"""
        written, state, _ = self._execute(
            {
                "energy_task": {},
                "free_task": [{"action": "hunt", "target": "异种合成魔兽"}],
            },
            agent_tasks=("圣骸兽",),
        )

        names = set(written["TaskDefinitions"].values())
        self.assertIn("敌人与魔物", names)      # 本轮要用的还留着
        self.assertNotIn("圣骸兽", names)       # 上一轮的被摘掉
        self.assertEqual(state, [])             # 状态文件同步更新

    def test_agent_registered_task_is_kept_when_still_requested(self):
        written, state, _ = self._execute(
            {"energy_task": {}, "free_task": [{"action": "hunt", "target": "异种合成魔兽"}]},
            agent_tasks=("敌人与魔物",),
        )

        self.assertIn("敌人与魔物", set(written["TaskDefinitions"].values()))
        self.assertEqual(state, ["敌人与魔物"])

    def test_boss_task_left_on_by_an_earlier_round_is_switched_off(self):
        """玩家实测的坑：他自己的一条龙里登记着《批量讨伐角色养成材料BOSS》且开着，
        Agent 打 Boss 那轮顺手开了它；之后他**只让 Agent 刷材料**，结果每次启动一条龙都顺带打一次
        Boss（那次跑去打了上个版本的急冻树）。这个任务不是"Agent 加的"，所以以前永远关不掉。"""
        self._add_task("uuid-boss", "批量讨伐角色养成材料BOSS", enabled=True)

        written, _, enabled_state = self._execute(
            {"energy_task": {}, "free_task": [{"action": "hunt", "target": "异种合成魔兽"}]}
        )

        names = self._enabled_names(written)
        self.assertNotIn("批量讨伐角色养成材料BOSS", names)   # ★ 本轮没排它 → 关掉
        self.assertIn("敌人与魔物", names)                    # 本轮要用的开着
        self.assertEqual(enabled_state, ["敌人与魔物"])        # 记下"这轮开过谁"

    def test_agent_enabled_category_group_left_over_is_switched_off(self):
        """类目组同理：Agent 上一轮开的「地图素材」这轮没排 → 关掉（玩家自己开的才不管）。"""
        written, _, _ = self._execute(
            {"energy_task": {}, "free_task": [{"action": "hunt", "target": "异种合成魔兽"}]},
            enabled_tasks=["地图素材", "敌人与魔物"],
        )

        names = self._enabled_names(written)
        self.assertNotIn("地图素材", names)
        self.assertIn("敌人与魔物", names)

    def test_gather_on_cooldown_is_not_scheduled(self):
        """采集物还在 48 小时冷却里 → 本轮不碰「地图素材」组，只提示玩家（玩家实测需求）。

        ⚠️ 不能拿"任务有没有 enabled"来断言：测试夹具里 地图素材 本来就是开着的。
        该看的是**组文件有没有被改写**（路线该保持 Disabled）。
        """
        from skills import gather_cooldown

        cooling = {
            "material": "清心", "last_at": datetime.datetime(2026, 9, 13, 12, 34),
            "hours_ago": 1.0, "hours_left": 47.0, "cooling": True, "known": True,
        }
        with patch.object(gather_cooldown, "status", return_value=cooling):
            self._execute({
                "energy_task": {},
                "free_task": [{"action": "gather", "target": "清心"}],
            })

        group = json.loads((self.group_dir / "地图素材.json").read_text(encoding="utf-8"))

        self.assertEqual([p["status"] for p in group["projects"]], ["Disabled"])

    def test_gather_is_scheduled_when_refreshed(self):
        from skills import gather_cooldown

        ready = {
            "material": "清心", "last_at": None, "hours_ago": None,
            "hours_left": 0.0, "cooling": False, "known": False,
        }
        with patch.object(gather_cooldown, "status", return_value=ready):
            self._execute({
                "energy_task": {},
                "free_task": [{"action": "gather", "target": "清心"}],
            })

        group = json.loads((self.group_dir / "地图素材.json").read_text(encoding="utf-8"))

        self.assertEqual([p["status"] for p in group["projects"]], ["Enabled"])

    def test_forced_gather_ignores_the_cooldown(self):
        """玩家说「强制采集」→ 计划里带 force_gather，执行层不再拦。"""
        from skills import gather_cooldown

        cooling = {
            "material": "清心", "last_at": datetime.datetime(2026, 9, 13, 12, 34),
            "hours_ago": 1.0, "hours_left": 47.0, "cooling": True, "known": True,
        }
        with patch.object(gather_cooldown, "status", return_value=cooling):
            self._execute({
                "energy_task": {},
                "force_gather": True,
                "free_task": [{"action": "gather", "target": "清心"}],
            })

        group = json.loads((self.group_dir / "地图素材.json").read_text(encoding="utf-8"))

        self.assertEqual([p["status"] for p in group["projects"]], ["Enabled"])

    def test_player_owned_group_that_agent_never_touched_stays_on(self):
        """玩家自己开着、Agent 从没开过的整脚本组（例如「狗粮AAA」）不许被关。"""
        self._add_task("uuid-dog", "狗粮AAA", enabled=True)

        written, _, _ = self._execute(
            {"energy_task": {}, "free_task": [{"action": "hunt", "target": "异种合成魔兽"}]}
        )

        self.assertIn("狗粮AAA", self._enabled_names(written))

    def test_state_stops_tracking_groups_that_are_already_off(self):
        """状态文件的 `enabled` 只是"Agent 开过谁"的账本，别记着不放了。

        玩家把某个被关掉的组又手动打开（或者根本没开过）时，如果账本还留着它，
        下一轮 Agent 会再关一次 —— 玩家开、Agent 关，来回打架。
        规则：动手前它就已经关着的，本轮不再记账。
        """
        self._add_task("uuid-dog", "狗粮AAA", enabled=False)

        written, _, enabled_state = self._execute(
            {"energy_task": {}, "free_task": [{"action": "hunt", "target": "异种合成魔兽"}]},
            enabled_tasks=["狗粮AAA"],
        )

        self.assertNotIn("狗粮AAA", enabled_state)       # 账本把它划掉了
        self.assertNotIn("狗粮AAA", self._enabled_names(written))
        self.assertEqual(enabled_state, ["敌人与魔物"])   # 只留本轮真排的

    def test_domain_task_left_on_by_an_earlier_round_is_switched_off(self):
        """内置动作同理：上一轮开的《自动秘境》这轮只刷材料 → 关掉。"""
        self._add_task("uuid-domain", "自动秘境", enabled=True)

        written, _, _ = self._execute(
            {"energy_task": {}, "free_task": [{"action": "hunt", "target": "异种合成魔兽"}]}
        )

        self.assertNotIn("自动秘境", self._enabled_names(written))

    def test_enemy_target_switches_the_enemy_group_on(self):
        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            config, "BGI_ENEMY_CONFIG", str(self.group_dir / "敌人与魔物.json")
        ), patch.object(
            config, "BGI_MAP_CONFIG", str(self.group_dir / "地图素材.json")
        ), patch.object(
            config, "BGI_BACKUP_DIR", str(self.root / "backups")
        ), patch.object(
            config, "BGI_AUTO_CREATE_ROUTE_GROUP", False
        ), patch.object(
            config, "AGENT_TASK_STATE_PATH", str(self.root / "agent_tasks.json")
        ), patch.object(
            bgi_controller, "resolve_one_dragon_config_path", return_value=str(self.one_dragon)
        ), patch.object(
            bgi_controller, "ensure_bettergi_closed", return_value=True
        ), patch.object(
            bgi_controller.feishu_api, "send_feishu_msg"
        ):
            bgi_controller.execute_bgi_task(
                {"energy_task": {"action": "run_boss", "target": "异种合成魔兽"}, "free_task": []},
                "t",
                {},
                "open_id",
                "100000000",
            )

        enemy_group = json.loads(
            (self.group_dir / "敌人与魔物.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [p["status"] for p in enemy_group["projects"]], ["Enabled", "Disabled"]
        )
        tasks = json.loads(self.one_dragon.read_text(encoding="utf-8"))
        self.assertTrue(tasks["TaskEnabledList"]["uuid-enemy"])
        self.assertFalse(tasks["TaskEnabledList"]["uuid-map"])


class EnemyGroupSelectionTests(unittest.TestCase):
    """超大路线组会被 BetterGI 跑到 WPF 栈溢出，所以要优先小组、并拒绝超大组。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.group_dir = Path(self.temp_dir.name)
        self.preferred = self.group_dir / "敌人与魔物.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, name, projects):
        path = self.group_dir / name
        path.write_text(
            json.dumps(_group(os.path.splitext(name)[0], projects), ensure_ascii=False),
            encoding="utf-8",
        )
        return str(path)

    def _run(self, hunt_items, allow_large=False, max_size=300, policy="allow", registered=()):
        bgi_config = {"TaskEnabledList": {}, "TaskOrder": []}
        with patch.object(config, "BGI_MAP_CONFIG", str(self.group_dir / "地图素材.json")), patch.object(
            config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)
        ), patch.object(config, "BGI_ENEMY_CONFIG", str(self.preferred)), patch.object(
            config, "BGI_ALLOW_LARGE_ROUTE_GROUP", allow_large
        ), patch.object(
            config, "BGI_ROUTE_GROUP_POLICY", policy
        ), patch.object(
            bgi_controller, "_registered_task_names", return_value=set(registered)
        ):
            results = bgi_controller._apply_enemy_groups(
                bgi_config,
                hunt_items,
                group_dir=str(self.group_dir),
                preferred_path=str(self.preferred),
                max_group_size=max_size,
            )
        return bgi_config, results

    def test_small_specialised_group_wins_over_the_huge_one(self):
        self._write("敌人与魔物.json", [_project("骗骗花\\骗骗花@x", f"{i}.json") for i in range(400)])
        self._write("骗骗花.json", [_project("骗骗花\\骗骗花@x", "01.json")])

        # 玩家自己在一条龙里加过「骗骗花」这个组（登记过才会被执行）
        bgi_config, results = self._run(["骗骗花"], registered={"骗骗花"})

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "骗骗花")
        self.assertEqual(results[0]["path"], str(self.group_dir / "骗骗花.json"))
        # 总组必须被关掉，否则上次残留的启用状态还会让它跑 400 条
        self.assertIs(bgi_config["TaskEnabledList"]["敌人与魔物"], False)

    def test_env_override_allows_the_large_group(self):
        self._write("敌人与魔物.json", [_project("骗骗花", f"{i}.json") for i in range(400)])

        bgi_config, results = self._run(["骗骗花"], allow_large=True)

        self.assertEqual(len(results), 1)
        self.assertNotIn("warning", results[0])
        self.assertIs(bgi_config["TaskEnabledList"]["敌人与魔物"], True)
        # allow 策略下是原地开关：组还是 400 条，命中的那条 Enabled
        self.assertEqual(len(results[0]["data"]["projects"]), 400)

    def test_shrink_policy_keeps_only_matched_routes_and_archives_the_rest(self):
        self._write(
            "敌人与魔物.json",
            [_project("骗骗花", "hit.json")] + [_project("别的敌人", f"{i}.json") for i in range(399)],
        )

        bgi_config, results = self._run(["骗骗花"], policy="shrink")

        self.assertEqual(len(results), 1)
        action = results[0]
        self.assertEqual(len(action["data"]["projects"]), 1)
        self.assertEqual(action["data"]["projects"][0]["status"], "Enabled")
        self.assertIn("精简", action["notice"])
        # 完整清单要归档，供下次换目标时重新挑
        self.assertEqual(action["archive"]["path"], route_group.archive_path_for(str(self.preferred)))
        self.assertEqual(len(action["archive"]["data"]["projects"]), 400)
        self.assertIs(bgi_config["TaskEnabledList"]["敌人与魔物"], True)

    def test_shrunk_group_is_rebuilt_from_the_archive_for_a_new_target(self):
        self._write(
            "敌人与魔物.json",
            [_project("骗骗花", "p.json"), _project("蕈兽", "m.json")]
            + [_project("别的敌人", f"{i}.json") for i in range(398)],
        )

        _cfg, first = self._run(["骗骗花"], policy="shrink")
        self.assertEqual(len(first[0]["data"]["projects"]), 1)

        # 模拟"归档 + 精简后落盘"（事务提交的效果）
        archive = Path(first[0]["archive"]["path"])
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(json.dumps(first[0]["archive"]["data"], ensure_ascii=False), encoding="utf-8")
        self.preferred.write_text(json.dumps(first[0]["data"], ensure_ascii=False), encoding="utf-8")

        # 换目标：要能从归档的完整清单里重新挑出蕈兽，而不是被精简后的组挡住
        _cfg2, second = self._run(["蕈兽"], policy="shrink")

        self.assertEqual(len(second), 1)
        names = [p["folderName"] for p in second[0]["data"]["projects"]]
        self.assertEqual(names, ["蕈兽"])

    def test_refuse_policy_blocks_the_large_group(self):
        self._write("敌人与魔物.json", [_project("骗骗花", f"{i}.json") for i in range(400)])

        bgi_config, results = self._run(["骗骗花"], policy="refuse")

        self.assertIn("warning", results[0])
        self.assertIs(bgi_config["TaskEnabledList"]["敌人与魔物"], False)

    def test_small_preferred_group_is_still_used_when_no_specialised_one_exists(self):
        self._write("敌人与魔物.json", [_project("蕈兽", "01.json"), _project("骗骗花", "02.json")])

        _bgi_config, results = self._run(["蕈兽"])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "敌人与魔物")
        self.assertEqual(results[0]["matched"], 1)


class StrategyValidationTests(unittest.TestCase):
    """回归测试：组里配的策略名在 User/AutoFight 里没有文件 → BetterGI 会在怪点中断。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.auto_fight = self.root / "AutoFight"
        self.auto_fight.mkdir()
        (self.auto_fight / "万能战斗策略（萌新推荐）.txt").write_text("", encoding="utf-8")
        (self.auto_fight / "群友分享").mkdir()
        (self.auto_fight / "群友分享" / "四神队(进阶版).txt").write_text("", encoding="utf-8")

        self.repos = self.root / "Repos"
        combat = self.repos / "bettergi-scripts-list-main" / "repo" / "combat"
        combat.mkdir(parents=True)
        (combat / "1.四神挂机[推荐].txt").write_text("", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _notice(self, name):
        return route_group.strategy_notice(
            name, auto_fight_dir=str(self.auto_fight), repos_dir=str(self.repos)
        )

    def test_existing_strategy_is_silent(self):
        self.assertEqual(self._notice("万能战斗策略（萌新推荐）"), "")

    def test_auto_select_and_empty_are_silent(self):
        self.assertEqual(self._notice("根据队伍自动选择"), "")
        self.assertEqual(self._notice(""), "")

    def test_subfolder_strategy_needs_the_prefix(self):
        # 平铺解析：写全相对路径才对，写裸名字会报"文件不存在"
        self.assertEqual(self._notice("群友分享\\四神队(进阶版)"), "")
        self.assertIn("找不到", self._notice("四神队(进阶版)"))

    def test_missing_strategy_points_at_the_repo_copy(self):
        notice = self._notice("1.四神挂机[推荐]")

        self.assertIn("1.四神挂机[推荐]", notice)
        self.assertIn("战斗策略文件不存在", notice)
        self.assertIn("脚本仓库里其实有", notice)

    def test_group_level_check_skips_disabled_auto_fight(self):
        group = _group("敌人与魔物", [_project("蕈兽", "01.json")])
        group["config"] = {
            "pathingConfig": {
                "autoFightEnabled": False,
                "autoFightConfig": {"strategyName": "不存在的策略"},
            }
        }

        self.assertEqual(
            route_group.group_strategy_notice(
                group, auto_fight_dir=str(self.auto_fight), repos_dir=str(self.repos)
            ),
            "",
        )

    def test_group_level_check_reports_broken_strategy(self):
        group = _group("敌人与魔物", [_project("蕈兽", "01.json")])
        group["config"] = {
            "pathingConfig": {
                "autoFightEnabled": True,
                "autoFightConfig": {"strategyName": "1.四神挂机[推荐]"},
            }
        }

        notice = route_group.group_strategy_notice(
            group, auto_fight_dir=str(self.auto_fight), repos_dir=str(self.repos)
        )

        self.assertIn("1.四神挂机[推荐]", notice)


class RouteCategoryTests(unittest.TestCase):
    """矿物 / 食材与炼金 和 敌人与魔物 共用同一套类目机制。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.group_dir = Path(self.temp_dir.name)
        (self.group_dir / "石珀.json").write_text(
            json.dumps(
                _group("石珀", [_project("石珀", "01.json"), _project("石珀", "02.json")]),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_category_specs_cover_every_category(self):
        specs = route_group.category_specs()

        self.assertEqual(set(specs), {"hunt", "hoe", "mine", "cook"})
        self.assertEqual(specs["mine"].group_filename, config.BGI_MINE_CONFIG_NAME)
        self.assertEqual(specs["cook"].group_filename, config.BGI_COOK_CONFIG_NAME)
        self.assertEqual(specs["hoe"].group_filename, config.BGI_HOE_CONFIG_NAME)
        # 锄大地必须带类目前缀，否则会跟「敌人与魔物」互相抢路线
        self.assertEqual(specs["hoe"].folder_prefix, "锄地专区")
        self.assertEqual(specs["hunt"].folder_prefix, "敌人与魔物")
        for action in ("mine", "cook", "hoe"):
            self.assertIn(action, route_group.free_task_actions())

    def test_mine_reuses_the_material_group(self):
        bgi_config = {"TaskEnabledList": {}, "TaskOrder": []}
        with patch.object(config, "BGI_MAP_CONFIG", str(self.group_dir / "地图素材.json")), patch.object(
            config, "BGI_MINE_CONFIG", str(self.group_dir / "矿物.json")
        ), patch.object(
            bgi_controller, "_registered_task_names", return_value={"石珀"}
        ):
            results = bgi_controller._apply_category_groups(
                bgi_config, ["石珀"], route_group.category_specs()["mine"], group_dir=str(self.group_dir)
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "石珀")
        self.assertEqual(results[0]["matched"], 2)

    def test_category_without_any_group_gives_actionable_warning(self):
        bgi_config = {"TaskEnabledList": {}, "TaskOrder": []}
        with patch.object(config, "BGI_MAP_CONFIG", str(self.group_dir / "地图素材.json")), patch.object(
            config, "BGI_COOK_CONFIG", str(self.group_dir / "食材与炼金.json")
        ), patch.object(
            bgi_controller, "_registered_task_names", return_value={"食材与炼金"}
        ):
            results = bgi_controller._apply_category_groups(
                bgi_config, ["禽肉"], route_group.category_specs()["cook"], group_dir=str(self.group_dir)
            )

        self.assertIn("warning", results[0])
        self.assertIn("食材与炼金", results[0]["warning"])
        self.assertIn("禽肉", results[0]["warning"])
        # 提示里要带上可用组名和目标写法，玩家才知道怎么补
        self.assertIn("石珀", results[0]["warning"])
        self.assertIn("禽肉 / 鱼肉", results[0]["warning"])


class FreeTaskReclassifyTests(unittest.TestCase):
    """回归测试：久雨莲（在 食材与炼金 下）被 LLM 写成 gather，必须自动改判。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.group_dir = Path(self.temp_dir.name)
        self._write(
            "地图素材.json",
            [_project("地方特产\\璃月\\清心", "清心-1.json")],
        )
        self._write(
            "食材与炼金.json",
            [_project("食材与炼金\\久雨莲\\久雨莲@张三", "01-久雨莲-厄里那斯-7个.json")],
        )
        self._write(
            "矿物.json",
            [_project("矿物\\水晶块\\水晶块@李四", "01-水晶块.json")],
        )
        self._write(
            "敌人与魔物.json",
            [_project("敌人与魔物\\蕈兽\\蕈兽@王五", "01-蕈兽.json")],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, name, projects):
        (self.group_dir / name).write_text(
            json.dumps(_group(os.path.splitext(name)[0], projects), ensure_ascii=False),
            encoding="utf-8",
        )

    def _reclassify(self, target, action):
        task = {"action": action, "target": target, "reason": "玩家要求"}
        with patch.object(config, "BGI_MAP_CONFIG", str(self.group_dir / "地图素材.json")), patch.object(
            config, "BGI_MINE_CONFIG", str(self.group_dir / "矿物.json")
        ), patch.object(
            config, "BGI_COOK_CONFIG", str(self.group_dir / "食材与炼金.json")
        ), patch.object(
            config, "BGI_ENEMY_CONFIG", str(self.group_dir / "敌人与魔物.json")
        ), patch.object(
            config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)
        ):
            notices = route_group.reclassify_free_tasks([task], group_dir=str(self.group_dir))
        return task, notices

    def test_ingredient_written_as_gather_is_moved_to_cook(self):
        task, notices = self._reclassify("久雨莲", "gather")

        self.assertEqual(task["action"], "cook")
        self.assertEqual(len(notices), 1)
        self.assertIn("食材与炼金", notices[0])
        self.assertIn("久雨莲", notices[0])
        # reason 里要留下改判痕迹，方便审批时核对
        self.assertIn("改判", task["reason"])

    def test_mineral_written_as_gather_is_moved_to_mine(self):
        task, _notices = self._reclassify("水晶块", "gather")

        self.assertEqual(task["action"], "mine")

    def test_enemy_written_as_gather_is_moved_to_hunt(self):
        task, _notices = self._reclassify("蕈兽", "gather")

        self.assertEqual(task["action"], "hunt")

    def test_correct_declaration_is_left_alone(self):
        for target, action in (("清心", "gather"), ("久雨莲", "cook"), ("水晶块", "mine"), ("蕈兽", "hunt")):
            task, notices = self._reclassify(target, action)
            self.assertEqual(task["action"], action, f"{target} 不该被改判")
            self.assertEqual(notices, [])

    def test_unknown_target_is_left_alone(self):
        task, notices = self._reclassify("随便编的材料", "gather")

        self.assertEqual(task["action"], "gather")
        self.assertEqual(notices, [])

    def test_non_free_task_actions_are_ignored(self):
        task = {"action": "run_boss", "target": "久雨莲"}
        notices = route_group.reclassify_free_tasks([task], group_dir=str(self.group_dir))

        self.assertEqual(task["action"], "run_boss")
        self.assertEqual(notices, [])


class PlanSummaryTests(unittest.TestCase):
    """审批屏必须把 script（整脚本任务，如狗粮）也列出来，否则玩家看不出要跑什么。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.group_dir = Path(self.temp_dir.name)
        (self.group_dir / "狗粮AAA.json").write_text(
            json.dumps(
                _group(
                    "狗粮AAA",
                    [
                        {
                            "name": "狗粮ABE路线，自动拾取分解",
                            "folderName": "AutoArtifacts_A_B_Extra",
                            "type": "Javascript",
                            "status": "Enabled",
                        }
                    ],
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _lines(self, registered=()):
        return route_group.plan_summary_lines(
            {},
            [{"action": "script", "target": "狗粮AAA", "reason": "玩家要求"}],
            group_dir=str(self.group_dir),
            registered_names=registered,
        )

    def test_script_task_is_listed_with_group_and_script(self):
        text = "\n".join(self._lines(registered=("狗粮AAA",)))

        self.assertIn("整脚本任务", text)
        self.assertIn("狗粮AAA", text)
        self.assertIn("狗粮ABE路线，自动拾取分解", text)
        self.assertIn("已登记一条龙", text)

    def test_unregistered_group_is_flagged(self):
        text = "\n".join(self._lines(registered=("地图素材",)))

        # 现在会自动补登记，所以提示是"未登记：执行时会自动登记"，不再吓唬玩家
        self.assertIn("未登记：执行时会自动登记", text)

    def test_unknown_script_target_is_flagged(self):
        lines = route_group.plan_summary_lines(
            {},
            [{"action": "script", "target": "并不存在的脚本"}],
            group_dir=str(self.group_dir),
        )

        self.assertTrue(any("没找到对应的 JS 脚本组" in line for line in lines))

    def test_empty_plan_still_shows_something(self):
        lines = route_group.plan_summary_lines({}, [])

        self.assertEqual(lines[0], "⚔️ 体力目标：无")
        self.assertIn("采集目标：无", lines[1])

    def test_all_free_task_categories_are_listed(self):
        free_tasks = [
            {"action": "gather", "target": "清心"},
            {"action": "hunt", "target": "蕈兽"},
            {"action": "hoe", "target": "锄大地"},
            {"action": "mine", "target": "水晶块"},
            {"action": "cook", "target": "禽肉"},
        ]

        text = "\n".join(route_group.plan_summary_lines({}, free_tasks))

        for expected in ("清心", "蕈兽", "锄大地", "水晶块", "禽肉"):
            self.assertIn(expected, text)


class HoeCategoryTests(unittest.TestCase):
    """锄大地（锄地专区）必须和敌人与魔物严格分开：两边路线名会重名。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.group_dir = self.root / "ScriptGroup"
        self.group_dir.mkdir()
        self.pathing_dir = self.root / "AutoPathing"
        projects = {
            "1_2_璃月": ["2101璃月无妄坡西南.json", "2201璃月明蕴镇西北.json"],
            "0_0_飞萤": ["A01-蒙德-龙脊雪山-眠龙谷-北-3只.json"],
            "9_0_低效路线(不跑）": ["25012璃月遁玉陵.json"],
        }
        for folder, names in projects.items():
            directory = self.pathing_dir / "锄地专区" / "小怪2000@mno" / folder
            directory.mkdir(parents=True)
            for name in names:
                (directory / name).write_text("{}", encoding="utf-8")

        # 敌人与魔物里也有叫「飞萤」的魔物目录（名字和锄地专区的子目录撞车）
        self._write_group(
            "敌人与魔物.json",
            [
                _project("敌人与魔物\\飞萤\\飞萤@san", "A01-飞萤.json"),
                _project("敌人与魔物\\骗骗花\\骗骗花@san", "01-骗骗花.json"),
            ],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_group(self, name, projects):
        (self.group_dir / name).write_text(
            json.dumps(_group(os.path.splitext(name)[0], projects), ensure_ascii=False),
            encoding="utf-8",
        )

    def _ensure_generated_group(self):
        """跑一遍自动建组，并把结果落盘（模拟 bgi_controller 的行为）。"""
        hoe = route_group.category_specs()["hoe"]
        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            config, "BGI_AUTO_PATHING_DIR", str(self.pathing_dir)
        ), patch.object(config, "BGI_AUTO_CREATE_ROUTE_GROUP", True):
            generated = route_group.ensure_category_group(hoe, group_dir=str(self.group_dir))
        self.assertIsNotNone(generated, "锄大地组应该被自动建出来")
        path, data, notice = generated
        (self.group_dir / os.path.basename(path)).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return path, data, notice

    def _count(self, action, targets):
        spec = route_group.category_specs()[action]
        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            config, "BGI_ENEMY_CONFIG", str(self.group_dir / "敌人与魔物.json")
        ), patch.object(config, "BGI_AUTO_PATHING_DIR", str(self.pathing_dir)), patch.object(
            config, "BGI_HOE_CONFIG", str(self.group_dir / config.BGI_HOE_CONFIG_NAME)
        ):
            return route_group.category_preferred_match_count(spec, targets, str(self.group_dir))

    def test_auto_created_group_keeps_the_category_prefix(self):
        path, data, notice = self._ensure_generated_group()

        self.assertEqual(os.path.basename(path), config.BGI_HOE_CONFIG_NAME)
        self.assertEqual(len(data["projects"]), 4)
        self.assertTrue(all(p["folderName"].startswith("锄地专区\\") for p in data["projects"]))
        self.assertIn("锄地专区", notice)
        # 结构必须和 BetterGI 脚本组一致（type/status/schedule 一个都不能少）
        self.assertEqual(data["projects"][0]["type"], "Pathing")
        self.assertEqual(data["projects"][0]["status"], "Disabled")
        self.assertEqual(data["projects"][0]["schedule"], "Daily")

    def test_auto_create_is_skipped_when_the_group_already_exists(self):
        hoe = route_group.category_specs()["hoe"]
        self._write_group(config.BGI_HOE_CONFIG_NAME, [])

        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            config, "BGI_AUTO_PATHING_DIR", str(self.pathing_dir)
        ), patch.object(config, "BGI_AUTO_CREATE_ROUTE_GROUP", True):
            self.assertIsNone(
                route_group.ensure_category_group(hoe, group_dir=str(self.group_dir))
            )

    def test_hunt_does_not_steal_hoe_routes_even_with_the_same_name(self):
        """「飞萤」在两边都有：敌人与魔物只算自己那 1 条，锄地专区也只算自己那 1 条。"""
        self._ensure_generated_group()

        self.assertEqual(self._count("hunt", ["飞萤"]), 1)
        # 锄大地 = 锄地专区全部路线，但默认跳过「低效/不跑」子目录（4 - 1 = 3）
        self.assertEqual(self._count("hoe", ["锄大地"]), 3)
        self.assertEqual(self._count("hoe", ["飞萤"]), 1)

    def test_small_group_scan_is_also_isolated(self):
        """按目标找"小组"时也不能串类目：否则说锄大地会翻出敌人与魔物的组。"""
        self._ensure_generated_group()
        fields = route_group.ENEMY_MATCH_FIELDS

        hunt_hits = route_group.find_groups_for_targets(
            str(self.group_dir), ["飞萤"], fields, own_prefix="敌人与魔物"
        )
        hoe_hits = route_group.find_groups_for_targets(
            str(self.group_dir), ["飞萤"], fields, own_prefix="锄地专区"
        )

        self.assertEqual([Path(path).name for path, _g in hunt_hits], ["敌人与魔物.json"])
        self.assertEqual([Path(path).name for path, _g in hoe_hits], [config.BGI_HOE_CONFIG_NAME])

    def test_hoe_skips_low_efficiency_folders_unless_asked(self):
        self._ensure_generated_group()

        self.assertEqual(self._count("hoe", ["锄大地"]), 3)
        self.assertEqual(self._count("hoe", ["锄大地 连低效一起跑"]), 4)
        # 单点地区：只跑 1_2_璃月 的 2 条，低效目录里那条璃月路线不算
        self.assertEqual(self._count("hoe", ["璃月"]), 2)

    def test_reclassify_moves_hoe_written_as_hunt(self):
        """LLM 把「锄大地」写成 hunt 时要改判成 hoe，而不是报"没有路线"。"""
        task = {"action": "hunt", "target": "锄大地", "reason": "玩家要求"}
        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            config, "BGI_ENEMY_CONFIG", str(self.group_dir / "敌人与魔物.json")
        ), patch.object(config, "BGI_AUTO_PATHING_DIR", str(self.pathing_dir)), patch.object(
            config, "BGI_AUTO_CREATE_ROUTE_GROUP", True
        ), patch.object(config, "BGI_MAP_CONFIG", str(self.group_dir / "地图素材.json")):
            notices = route_group.reclassify_free_tasks([task], group_dir=str(self.group_dir))

        self.assertEqual(task["action"], "hoe")
        self.assertEqual(len(notices), 1)
        self.assertIn("锄大地", notices[0])

    def test_reclassify_moves_a_specific_monster_written_as_hoe(self):
        """反过来：具体魔物被写成 hoe 时要改判回 hunt（锄地专区里没有骗骗花的独立路线）。"""
        task = {"action": "hoe", "target": "骗骗花", "reason": "玩家要求"}
        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            config, "BGI_ENEMY_CONFIG", str(self.group_dir / "敌人与魔物.json")
        ), patch.object(config, "BGI_AUTO_PATHING_DIR", str(self.pathing_dir)), patch.object(
            config, "BGI_AUTO_CREATE_ROUTE_GROUP", True
        ), patch.object(config, "BGI_MAP_CONFIG", str(self.group_dir / "地图素材.json")):
            notices = route_group.reclassify_free_tasks([task], group_dir=str(self.group_dir))

        self.assertEqual(task["action"], "hunt")
        self.assertEqual(len(notices), 1)
        self.assertIn("敌人与魔物", notices[0])

    def test_reclassify_sends_the_whole_script_name_to_script(self):
        """「锄地一条龙」是 JS 整脚本：即使被写成 hoe，也要改判成 script（跑脚本，不是跑 300 条路线）。"""
        (self.group_dir / "锄地一条龙.json").write_text(
            json.dumps(
                _group(
                    "锄地一条龙",
                    [
                        {
                            "name": "锄地一条龙",
                            "folderName": "AutoHoeingOneDragon",
                            "type": "Javascript",
                            "status": "Disabled",
                        }
                    ],
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        task = {"action": "hoe", "target": "锄地一条龙", "reason": "玩家要求"}
        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            config, "BGI_ENEMY_CONFIG", str(self.group_dir / "敌人与魔物.json")
        ), patch.object(config, "BGI_AUTO_PATHING_DIR", str(self.pathing_dir)), patch.object(
            config, "BGI_AUTO_CREATE_ROUTE_GROUP", True
        ), patch.object(config, "BGI_MAP_CONFIG", str(self.group_dir / "地图素材.json")):
            notices = route_group.reclassify_free_tasks([task], group_dir=str(self.group_dir))

        self.assertEqual(task["action"], "script")
        self.assertIn("锄地一条龙", notices[0])

    def test_hoe_targets_do_not_leak_into_hunt_routes(self):
        """锄大地/清怪这类说法不该命中敌人与魔物的路线（否则会去只打一种怪）。"""
        for target in ("锄大地", "清怪", "刷精英"):
            self.assertEqual(self._count("hunt", [target]), 0, target)

    def test_controller_writes_only_hoe_routes_and_switches_the_hoe_task(self):
        """端到端：说「璃月」时只动锄地专区的璃月路线，敌人与魔物那份原封不动。"""
        self._ensure_generated_group()
        enemy_before = (self.group_dir / "敌人与魔物.json").read_text(encoding="utf-8")
        bgi_config = {"TaskEnabledList": {}, "TaskOrder": []}

        with patch.object(config, "BGI_MAP_CONFIG", str(self.group_dir / "地图素材.json")), patch.object(
            config, "BGI_ENEMY_CONFIG", str(self.group_dir / "敌人与魔物.json")
        ), patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)):
            results = bgi_controller._apply_category_groups(
                bgi_config,
                ["璃月"],
                route_group.category_specs()["hoe"],
                group_dir=str(self.group_dir),
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["matched"], 2)
        self.assertIs(bgi_config["TaskEnabledList"]["锄大地"], True)
        # 敌人与魔物这一次没被排，也不该被这次调用"顺手打开"
        self.assertNotEqual(bgi_config["TaskEnabledList"].get("敌人与魔物"), True)
        # 命中的 2 条 Enabled，其余（飞萤/低效）Disabled
        statuses = [p["status"] for p in results[0]["data"]["projects"]]
        self.assertEqual(statuses.count("Enabled"), 2)
        self.assertEqual(
            (self.group_dir / "敌人与魔物.json").read_text(encoding="utf-8"), enemy_before
        )


class HoeScriptAliasTests(unittest.TestCase):
    """锄地一条龙（AutoHoeingOneDragon）是 JS 整脚本，走 script 类目。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.group_dir = Path(self.temp_dir.name)
        (self.group_dir / "锄地一条龙.json").write_text(
            json.dumps(
                _group(
                    "锄地一条龙",
                    [
                        {
                            "name": "锄地一条龙",
                            "folderName": "AutoHoeingOneDragon",
                            "type": "Javascript",
                            "status": "Disabled",
                        }
                    ],
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_spoken_name_resolves_to_the_js_script_group(self):
        hit = route_group.find_js_script_group("锄大地", str(self.group_dir), ("锄地一条龙",))

        self.assertIsNotNone(hit)
        group_name, _path, script_name, registered = hit
        self.assertEqual(group_name, "锄地一条龙")
        self.assertEqual(script_name, "锄地一条龙")
        self.assertTrue(registered)

    def test_plain_script_name_still_works(self):
        hit = route_group.find_js_script_group("锄地一条龙", str(self.group_dir))

        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "锄地一条龙")

    def test_whole_script_name_is_not_expanded_into_a_route_category(self):
        """「锄地一条龙」是整脚本名，不能被展开成锄地专区的 300 条跑图路线。"""
        hoe = route_group.category_specs()["hoe"]

        self.assertEqual(route_group.category_search_terms(hoe, ["锄地一条龙"]), ["锄地一条龙"])
        # 普通说法照旧展开
        self.assertIn("锄地专区", route_group.category_search_terms(hoe, ["锄大地"]))


class TaskRegistrationEndToEndTests(unittest.TestCase):
    """端到端：玩家说「骗骗花」，小组存在但没登记 → 自动登记并启用（不再是死配置）。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.group_dir = Path(self.temp_dir.name)
        self._write_group(
            "骗骗花.json",
            [
                _project("敌人与魔物\\骗骗花\\骗骗花@san", "骗骗花-天衡山上.json"),
                _project("敌人与魔物\\骗骗花\\骗骗花@san", "骗骗花-归离院右下.json"),
            ],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_group(self, name, projects):
        (self.group_dir / name).write_text(
            json.dumps(_group(os.path.splitext(name)[0], projects), ensure_ascii=False),
            encoding="utf-8",
        )

    def _run(self, bgi_config):
        registered = set((bgi_config.get("TaskDefinitions") or {}).values())
        with patch.object(config, "BGI_MAP_CONFIG", str(self.group_dir / "地图素材.json")), patch.object(
            config, "BGI_ENEMY_CONFIG", str(self.group_dir / "敌人与魔物.json")
        ), patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            bgi_controller, "_registered_task_names", return_value=registered
        ):
            return bgi_controller._apply_category_groups(
                bgi_config,
                ["骗骗花"],
                route_group.category_specs()["hunt"],
                group_dir=str(self.group_dir),
            )

    def test_unregistered_group_gets_registered_and_enabled(self):
        """总组没登记、小组也没登记 → 用小组并现场把「骗骗花」登记进一条龙。"""
        bgi_config = {
            "TaskDefinitions": {"uuid-map": "地图素材"},
            "TaskEnabledList": {"uuid-map": False},
            "TaskOrder": ["uuid-map"],
        }

        results = self._run(bgi_config)

        self.assertEqual(results[0]["name"], "骗骗花")
        self.assertEqual(results[0]["matched"], 2)
        new_ids = [key for key, name in bgi_config["TaskDefinitions"].items() if name == "骗骗花"]
        self.assertEqual(len(new_ids), 1)
        self.assertTrue(bgi_config["TaskEnabledList"][new_ids[0]])
        self.assertIn(new_ids[0], bgi_config["TaskOrder"])
        # 提示要说明"已自动登记"，而不是让玩家自己去界面点一次
        self.assertIn("已自动添加为任务项", results[0]["notice"])

    def test_registered_total_group_is_preferred_over_an_unregistered_small_group(self):
        """总组登记过、小组没登记 → 退到总组（不往玩家配置里塞新任务）。"""
        self._write_group(
            "敌人与魔物.json", [_project("敌人与魔物\\骗骗花\\骗骗花@san", "骗骗花-天衡山上.json")]
        )
        bgi_config = {
            "TaskDefinitions": {"uuid-enemy": "敌人与魔物", "uuid-map": "地图素材"},
            "TaskEnabledList": {"uuid-enemy": False, "uuid-map": False},
            "TaskOrder": ["uuid-map", "uuid-enemy"],
        }

        results = self._run(bgi_config)

        self.assertEqual(results[0]["name"], "敌人与魔物")
        self.assertTrue(bgi_config["TaskEnabledList"]["uuid-enemy"])
        self.assertNotIn("骗骗花", bgi_config["TaskDefinitions"].values())

    def test_already_registered_group_reuses_its_id(self):
        bgi_config = {
            "TaskDefinitions": {"uuid-flower": "骗骗花", "uuid-map": "地图素材"},
            "TaskEnabledList": {"uuid-flower": False, "uuid-map": False},
            "TaskOrder": ["uuid-map"],
        }

        results = self._run(bgi_config)

        self.assertTrue(bgi_config["TaskEnabledList"]["uuid-flower"])
        self.assertNotIn("已自动添加为任务项", results[0]["notice"])
        self.assertEqual(len([k for k, v in bgi_config["TaskDefinitions"].items() if v == "骗骗花"]), 1)


class TaskRegistrationTests(unittest.TestCase):
    """一条龙任务登记：新格式配置只认 TaskDefinitions 里的 Id（否则任务被静默跳过）。

    实测踩的坑：LLM/旧代码把「组名: true」直接塞进 TaskEnabledList，BetterGI 的
    `LoadDisplayTaskListFromConfig` 在非旧格式下 `TaskDefinitions.TryGetValue` 失败就
    `continue` —— 玩家以为启用了，其实一条龙根本不认识这个任务（「骗骗花」就是这样失效的）。
    """

    def _new_format_config(self, name="敌人与魔物"):
        return {
            "TaskDefinitions": {"uuid-enemy": name, "uuid-map": "地图素材"},
            "TaskEnabledList": {"uuid-enemy": False, "uuid-map": True},
            "TaskOrder": ["uuid-map", "uuid-enemy"],
        }

    def test_enabling_an_unregistered_group_registers_it_with_a_new_id(self):
        config = self._new_format_config()

        key, created = bgi_controller.set_task_enabled(config, True, ("骗骗花",))

        self.assertTrue(created)
        self.assertEqual(config["TaskDefinitions"][key], "骗骗花")
        self.assertTrue(config["TaskEnabledList"][key])
        self.assertIn(key, config["TaskOrder"])
        self.assertNotIn("骗骗花", config["TaskEnabledList"])  # 不再写死名字键

    def test_registration_is_reused_for_an_already_registered_group(self):
        config = self._new_format_config()

        key, created = bgi_controller.set_task_enabled(config, True, ("敌人与魔物",))

        self.assertEqual(key, "uuid-enemy")
        self.assertFalse(created)
        self.assertTrue(config["TaskEnabledList"]["uuid-enemy"])

    def test_second_enable_does_not_duplicate_the_registration(self):
        config = self._new_format_config()

        first, created_first = bgi_controller.set_task_enabled(config, True, ("骗骗花",))
        second, created_second = bgi_controller.set_task_enabled(config, True, ("骗骗花",))

        self.assertEqual(first, second)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(list(config["TaskDefinitions"].values()).count("骗骗花"), 1)

    def test_legacy_config_without_definitions_still_uses_name_keys(self):
        """旧格式（TaskDefinitions 为空）里名字就是键，BetterGI 也是这么兜底的。"""
        config = {"TaskEnabledList": {}, "TaskOrder": []}

        key, created = bgi_controller.set_task_enabled(config, True, ("敌人与魔物",))

        self.assertEqual(key, "敌人与魔物")
        self.assertFalse(created)
        self.assertTrue(config["TaskEnabledList"]["敌人与魔物"])

    def test_disabling_an_unregistered_group_does_not_create_a_dead_key(self):
        config = self._new_format_config()
        config["TaskEnabledList"]["骗骗花"] = True  # 历史版本写坏的名字键

        key, created = bgi_controller.set_task_enabled(config, False, ("骗骗花",))

        self.assertFalse(created)
        self.assertNotIn("骗骗花", config["TaskEnabledList"])
        self.assertNotIn("骗骗花", config["TaskDefinitions"])
        self.assertEqual(key, "骗骗花")

    def test_prune_removes_dead_keys_and_order_entries(self):
        config = self._new_format_config()
        config["TaskEnabledList"]["骗骗花"] = True
        config["TaskOrder"].append("骗骗花")

        removed = bgi_controller.prune_task_list(config)

        self.assertEqual(removed, 2)
        self.assertNotIn("骗骗花", config["TaskEnabledList"])
        self.assertNotIn("骗骗花", config["TaskOrder"])
        self.assertEqual(config["TaskOrder"], ["uuid-map", "uuid-enemy"])

    def test_prune_is_a_noop_for_legacy_configs(self):
        config = {"TaskEnabledList": {"敌人与魔物": True}, "TaskOrder": ["敌人与魔物"]}

        self.assertEqual(bgi_controller.prune_task_list(config), 0)
        self.assertTrue(config["TaskEnabledList"]["敌人与魔物"])

    def test_registration_notice_explains_what_happened(self):
        created_note = bgi_controller._registration_notice("骗骗花", True)

        self.assertIn("已自动添加为任务项", created_note)
        self.assertNotIn("⚠️", created_note)

        with patch.object(bgi_controller, "_registered_task_names", return_value=set()):
            failed_note = bgi_controller._registration_notice("骗骗花", False)
        self.assertIn("⚠️", failed_note)

    def test_retire_task_removes_registration_and_switch(self):
        config = self._new_format_config()
        key, _created = bgi_controller.set_task_enabled(config, True, ("骗骗花",))

        retired = bgi_controller.retire_task(config, "骗骗花")

        self.assertTrue(retired)
        self.assertNotIn(key, config["TaskDefinitions"])
        self.assertNotIn(key, config["TaskEnabledList"])
        self.assertNotIn(key, config["TaskOrder"])
        # 玩家自己的任务原封不动
        self.assertEqual(config["TaskDefinitions"]["uuid-enemy"], "敌人与魔物")

    def test_retire_task_is_a_noop_for_unknown_names(self):
        config = self._new_format_config()

        self.assertFalse(bgi_controller.retire_task(config, "从没登记过的组"))

    def test_agent_task_state_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "memory" / "agent_registered_tasks.json")

            bgi_controller.save_agent_tasks(["骗骗花", "骗骗花", "蕈兽"], path)
            loaded = bgi_controller.load_agent_tasks(path)

        self.assertEqual(sorted(loaded), ["蕈兽", "骗骗花"])
        self.assertEqual(len(loaded), 2)   # 去重

    def test_agent_task_state_survives_a_missing_file(self):
        self.assertEqual(bgi_controller.load_agent_tasks("不存在的路径/state.json"), [])

    def test_second_positional_argument_is_the_path(self):
        """回归：`save_agent_tasks(names, path)` 的老写法必须还写进 path。

        踩过的坑：把 `enabled` 插到 `path` 前面，那个位置参数就被当成 enabled 的**字符串**去迭代，
        结果 added/enabled 全写进了**玩家真实的状态文件**（added 被覆盖、enabled 变成一串路径字符）。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "memory" / "agent_registered_tasks.json")

            with patch.object(
                config, "AGENT_TASK_STATE_PATH", str(Path(temp_dir) / "default.json")
            ):
                bgi_controller.save_agent_tasks(["蕈兽"], path)

            self.assertEqual(bgi_controller.load_agent_tasks(path), ["蕈兽"])
            self.assertFalse((Path(temp_dir) / "default.json").exists())
            self.assertEqual(bgi_controller.load_agent_enabled_tasks(path), [])

    def test_agent_enabled_state_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "state.json")

            bgi_controller.save_agent_tasks(["蕈兽"], path, enabled=["敌人与魔物", "敌人与魔物"])
            self.assertEqual(bgi_controller.load_agent_tasks(path), ["蕈兽"])
            self.assertEqual(bgi_controller.load_agent_enabled_tasks(path), ["敌人与魔物"])

    def test_old_list_format_is_still_readable(self):
        """旧版状态文件就是一个名字数组 → 还能读，enabled 视为空。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(json.dumps(["蕈兽"], ensure_ascii=False), encoding="utf-8")

            self.assertEqual(bgi_controller.load_agent_tasks(str(path)), ["蕈兽"])
            self.assertEqual(bgi_controller.load_agent_enabled_tasks(str(path)), [])

    def test_tasks_enabled_now_reports_only_what_is_really_on(self):
        """清理日志靠它：`set_task_enabled()` 返回 (键, 是否新建) 恒为真，不能拿来做判断。"""
        config_data = self._new_format_config()
        config_data["TaskDefinitions"]["uuid-boss"] = "批量讨伐角色养成材料BOSS"
        config_data["TaskEnabledList"]["uuid-boss"] = True
        config_data["TaskDefinitions"]["uuid-off"] = "自动秘境"
        config_data["TaskEnabledList"]["uuid-off"] = False

        active = bgi_controller.tasks_enabled_now(
            config_data,
            ["批量讨伐角色养成材料BOSS", "自动秘境", "从来没登记过的组"],
        )

        self.assertEqual(active, ["批量讨伐角色养成材料BOSS"])
        # 旧的返回形状（元组）别再被当成布尔用
        self.assertEqual(
            bgi_controller.set_task_enabled(config_data, False, ("自动秘境",)),
            ("uuid-off", False),
        )

    def test_agent_managed_names_are_the_three_builtin_actions(self):
        names = bgi_controller._agent_managed_task_names()

        self.assertEqual(names, {"自动秘境", "自动地脉花", "批量讨伐角色养成材料BOSS"})
        # 类目组和玩家自己的整脚本组不归它无条件管
        self.assertNotIn("地图素材", names)
        self.assertNotIn("敌人与魔物", names)
        self.assertNotIn("狗粮AAA", names)

    def test_unknown_config_shapes_do_not_crash(self):
        self.assertFalse(bgi_controller.uses_task_definitions({}))
        self.assertFalse(bgi_controller.uses_task_definitions({"TaskDefinitions": None}))
        self.assertEqual(bgi_controller.prune_task_list({}), 0)
        self.assertEqual(bgi_controller.task_definitions({"TaskDefinitions": "oops"}), {})

    def test_only_real_script_groups_may_be_auto_registered(self):
        """踩过的坑：`run_boss` 用占位名 AutoBoss 被自动登记，玩家一条龙里凭空多出一条空转任务。"""
        cfg = self._new_format_config()

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(config, "BGI_SCRIPT_GROUP_DIR", temp_dir):
                key, created = bgi_controller.set_task_enabled(cfg, True, ("AutoBoss", "突破材料"))

        self.assertFalse(created)
        self.assertEqual(key, "AutoBoss")
        self.assertNotIn("AutoBoss", cfg["TaskDefinitions"].values())
        self.assertNotIn("AutoBoss", cfg["TaskEnabledList"])

    def test_builtin_task_name_is_never_auto_registered(self):
        cfg = self._new_format_config()

        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "骗骗花.json").write_text("{}", encoding="utf-8")
            with patch.object(config, "BGI_SCRIPT_GROUP_DIR", temp_dir):
                _key, created = bgi_controller.set_task_enabled(cfg, True, ("自动首领讨伐",))

        self.assertFalse(created)
        self.assertNotIn("自动首领讨伐", cfg["TaskDefinitions"].values())

    def test_script_group_with_a_file_is_registered(self):
        cfg = self._new_format_config()

        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "骗骗花.json").write_text("{}", encoding="utf-8")
            with patch.object(config, "BGI_SCRIPT_GROUP_DIR", temp_dir):
                key, created = bgi_controller.set_task_enabled(cfg, True, ("骗骗花",))

        self.assertTrue(created)
        self.assertEqual(cfg["TaskDefinitions"][key], "骗骗花")

    def test_script_group_file_helper(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "骗骗花.json").write_text("{}", encoding="utf-8")

            self.assertTrue(bgi_controller.script_group_file("骗骗花", temp_dir))
            self.assertEqual(bgi_controller.script_group_file("AutoBoss", temp_dir), "")
            self.assertEqual(bgi_controller.script_group_file("", temp_dir), "")


class RepairCommandTests(unittest.TestCase):
    """`python main.py repair`：清掉 Agent 自己加过、又跑不起来的任务项。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.one_dragon = self.root / "OneDragon" / "地图素材.json"
        self.one_dragon.parent.mkdir()
        self.state_path = str(self.root / "memory" / "agent_registered_tasks.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_config(self, definitions):
        self.one_dragon.write_text(
            json.dumps(
                {
                    "TaskDefinitions": definitions,
                    "TaskEnabledList": {key: True for key in definitions},
                    "TaskOrder": list(definitions),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _repair(self, running=False, apply_js_settings=False):
        self.output = io.StringIO()
        with patch.object(config, "AGENT_TASK_STATE_PATH", self.state_path), patch.object(
            config, "BGI_BACKUP_DIR", str(self.root / "backups")
        ), patch.object(
            config, "BGI_SCRIPT_GROUP_DIR", str(self.root / "ScriptGroup")
        ), redirect_stdout(self.output):
            return bgi_controller.repair_agent_tasks(
                config_path=str(self.one_dragon),
                running_check=lambda: running,
                apply_js_settings=apply_js_settings,
            )

    def test_repair_disables_agent_leftover_switches(self):
        """玩家实测的坑：打完 Boss 那轮开的《批量讨伐角色养成材料BOSS》一直挂着，
        之后每次启动一条龙都顺带打一次 Boss。repair 要能一次性关掉它。"""
        self._write_config(
            {"uuid-boss": "批量讨伐角色养成材料BOSS", "uuid-dog": "狗粮AAA"}
        )

        code = self._repair()

        written = json.loads(self.one_dragon.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertFalse(written["TaskEnabledList"]["uuid-boss"])
        self.assertTrue(written["TaskEnabledList"]["uuid-dog"], "玩家自己的组不该被关")
        self.assertIn("已关闭 Agent 留下的开关", self.output.getvalue())

    def test_repair_disables_groups_recorded_as_agent_enabled(self):
        """状态文件里记着"Agent 开过"的类目组（例如敌人与魔物）同样要关。"""
        self._write_config({"uuid-enemy": "敌人与魔物", "uuid-map": "地图素材"})
        bgi_controller.save_agent_tasks([], self.state_path, enabled=["敌人与魔物"])

        self._repair()

        written = json.loads(self.one_dragon.read_text(encoding="utf-8"))
        self.assertFalse(written["TaskEnabledList"]["uuid-enemy"])
        self.assertTrue(written["TaskEnabledList"]["uuid-map"])       # 没记过的组照旧

    def test_repair_keeps_switches_that_are_already_off(self):
        self._write_config({"uuid-boss": "批量讨伐角色养成材料BOSS"})
        self.one_dragon.write_text(
            json.dumps(
                {
                    "TaskDefinitions": {"uuid-boss": "批量讨伐角色养成材料BOSS"},
                    "TaskEnabledList": {"uuid-boss": False},
                    "TaskOrder": ["uuid-boss"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        code = self._repair()

        self.assertEqual(code, 0)
        self.assertIn("没有需要清理", self.output.getvalue())
        self.assertFalse((self.root / "backups").exists(), "没改动就不该建备份事务")

    def test_repair_removes_agent_added_unrunnable_task(self):
        self._write_config({"uuid-boss": "AutoBoss", "uuid-map": "地图素材"})
        bgi_controller.save_agent_tasks(["AutoBoss"], self.state_path)

        code = self._repair()

        written = json.loads(self.one_dragon.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertNotIn("AutoBoss", written["TaskDefinitions"].values())
        self.assertIn("地图素材", written["TaskDefinitions"].values())
        self.assertEqual(bgi_controller.load_agent_tasks(self.state_path), [])
        # 必须有备份，能回滚
        self.assertTrue(list((self.root / "backups").iterdir()))

    def test_repair_keeps_player_owned_tasks(self):
        self._write_config({"uuid-hand": "玩家自己加的组"})
        bgi_controller.save_agent_tasks(["AutoBoss"], self.state_path)

        self._repair()

        written = json.loads(self.one_dragon.read_text(encoding="utf-8"))
        self.assertIn("玩家自己加的组", written["TaskDefinitions"].values())

    def test_repair_refuses_while_bettergi_is_running(self):
        self._write_config({"uuid-boss": "AutoBoss"})
        bgi_controller.save_agent_tasks(["AutoBoss"], self.state_path)

        code = self._repair(running=True)

        written = json.loads(self.one_dragon.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertIn("AutoBoss", written["TaskDefinitions"].values())   # 没动

    def test_repair_reports_nothing_to_do(self):
        self._write_config({"uuid-map": "地图素材"})
        bgi_controller.save_agent_tasks([], self.state_path)

        self.assertEqual(self._repair(), 0)
        self.assertFalse((self.root / "backups").exists())

    def test_repair_also_disables_the_js_editor_switch(self):
        """repair 顺手把 Boss 脚本的"启动时打开配置编辑器"关掉（挂机时它要手点才继续）。"""
        self._write_config({"uuid-boss": "批量讨伐角色养成材料BOSS"})
        bgi_controller.save_agent_tasks([], self.state_path)
        group_dir = self.root / "ScriptGroup"
        group_dir.mkdir()
        group_file = group_dir / "批量讨伐角色养成材料BOSS.json"
        group_file.write_text(
            json.dumps(
                {
                    "name": "批量讨伐角色养成材料BOSS",
                    "projects": [
                        {"name": "批量讨伐角色养成材料BOSS", "type": "Javascript",
                         "jsScriptSettingsObject": None}
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with patch.object(config, "BGI_JS_SCRIPT_SETTINGS", '{"showEditorOnStart": false}'):
            code = self._repair(apply_js_settings=True)

        written = json.loads(group_file.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(written["projects"][0]["jsScriptSettingsObject"], {"showEditorOnStart": False})


class JsSettingsTests(unittest.TestCase):
    """关掉 JS 脚本自带的"启动配置编辑器"。

    实测：《批量讨伐角色养成材料BOSS》的 settings.json 里 `showEditorOnStart` 默认 true，
    启动时会弹一个遮罩式编辑器，**必须手点"保存并关闭"脚本才继续** → 挂机等于卡死。
    BetterGI 会把脚本组项目里的 `jsScriptSettingsObject` 当作 JS 的 `settings` 对象。
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.group_dir = Path(self.temp_dir.name)
        self.group_path = self.group_dir / "批量讨伐角色养成材料BOSS.json"
        self._write(
            [
                {"name": "批量讨伐角色养成材料BOSS", "folderName": "批量讨伐角色养成材料BOSS",
                 "type": "Javascript", "status": "Enabled", "jsScriptSettingsObject": None},
                {"name": "某条路线.json", "folderName": "地方特产\\清心", "type": "Pathing",
                 "status": "Enabled"},
            ]
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, projects):
        self.group_path.write_text(json.dumps({"name": "批量讨伐角色养成材料BOSS", "projects": projects},
                                             ensure_ascii=False), encoding="utf-8")

    def test_editor_switch_is_written_into_the_js_project(self):
        result = bgi_controller.apply_js_project_settings(
            str(self.group_path), {"showEditorOnStart": False}
        )

        self.assertIsNotNone(result)
        _path, data, changed = result
        self.assertEqual(changed, {"showEditorOnStart": False})
        self.assertEqual(data["projects"][0]["jsScriptSettingsObject"], {"showEditorOnStart": False})
        # 跑图项目不能被顺手塞设置
        self.assertNotIn("jsScriptSettingsObject", data["projects"][1])

    def test_existing_settings_are_merged_not_replaced(self):
        self._write(
            [
                {"name": "批量讨伐角色养成材料BOSS", "type": "Javascript",
                 "jsScriptSettingsObject": {"某人自己的开关": True}},
            ]
        )

        result = bgi_controller.apply_js_project_settings(
            str(self.group_path), {"showEditorOnStart": False}
        )

        settings = result[1]["projects"][0]["jsScriptSettingsObject"]
        self.assertEqual(settings, {"某人自己的开关": True, "showEditorOnStart": False})

    def test_second_call_reports_no_change(self):
        first = bgi_controller.apply_js_project_settings(
            str(self.group_path), {"showEditorOnStart": False}
        )
        self.group_path.write_text(
            json.dumps(first[1], ensure_ascii=False), encoding="utf-8"
        )

        self.assertIsNone(
            bgi_controller.apply_js_project_settings(
                str(self.group_path), {"showEditorOnStart": False}
            )
        )

    def test_default_settings_come_from_config(self):
        with patch.object(config, "BGI_JS_SCRIPT_SETTINGS", '{"showEditorOnStart": false}'):
            result = bgi_controller.apply_js_project_settings(str(self.group_path))

        self.assertEqual(result[2], {"showEditorOnStart": False})

    def test_broken_json_in_env_is_ignored(self):
        with patch.object(config, "BGI_JS_SCRIPT_SETTINGS", "{不是 json"):
            self.assertEqual(bgi_controller.js_script_settings(), {})
            self.assertIsNone(bgi_controller.apply_js_project_settings(str(self.group_path)))

    def test_missing_group_file_is_a_noop(self):
        self.assertIsNone(
            bgi_controller.apply_js_project_settings(str(self.group_dir / "没有这个组.json"))
        )

    def test_only_named_projects_can_be_targeted(self):
        result = bgi_controller.apply_js_project_settings(
            str(self.group_path),
            {"showEditorOnStart": False},
            project_names={"别的脚本"},
        )

        self.assertIsNone(result)

    def test_boss_run_writes_the_editor_switch(self):
        """端到端：run_boss 时把脚本组的编辑器开关一起写掉（同一个事务里）。"""
        one_dragon = self.group_dir / "OneDragon" / "地图素材.json"
        one_dragon.parent.mkdir()
        one_dragon.write_text(
            json.dumps(
                {
                    "TaskDefinitions": {"uuid-boss": "批量讨伐角色养成材料BOSS"},
                    "TaskEnabledList": {"uuid-boss": False},
                    "TaskOrder": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        boss_config = self.group_dir / "boss-config" / "config.json"
        boss_config.parent.mkdir()
        boss_config.write_text("[]", encoding="utf-8")

        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(self.group_dir)), patch.object(
            config, "BGI_BOSS_CONFIG", str(boss_config)
        ), patch.object(
            config, "BGI_BACKUP_DIR", str(self.group_dir / "backups")
        ), patch.object(
            config, "BGI_AUTO_CREATE_ROUTE_GROUP", False
        ), patch.object(
            config, "AGENT_TASK_STATE_PATH", str(self.group_dir / "agent_tasks.json")
        ), patch.object(
            config, "BGI_JS_SCRIPT_SETTINGS", '{"showEditorOnStart": false}'
        ), patch.object(
            bgi_controller, "resolve_one_dragon_config_path", return_value=str(one_dragon)
        ), patch.object(
            bgi_controller, "ensure_bettergi_closed", return_value=True
        ), patch.object(
            bgi_controller.feishu_api, "send_feishu_msg"
        ):
            bgi_controller.execute_bgi_task(
                {"energy_task": {"action": "run_boss", "target": "秘源机兵·构型械", "count": 1}},
                "t",
                {},
                "open_id",
                "1",
            )

        written = json.loads(self.group_path.read_text(encoding="utf-8"))
        self.assertEqual(
            written["projects"][0]["jsScriptSettingsObject"], {"showEditorOnStart": False}
        )


class ForceBandSwitchTests(unittest.TestCase):
    """防闪退隔离带的开关（玩家要求做成配置项，好自己验证那个 bug 存不存在）。"""

    def _group(self, count_a=3, count_b=20):
        projects = [
            {"name": f"{index:02d}-材料A-地点-9个.json", "folderName": "地方特产\\璃月\\材料A"}
            for index in range(count_a)
        ] + [
            {"name": f"{index:02d}-材料B-地点-9个.json", "folderName": "地方特产\\璃月\\材料B"}
            for index in range(count_b)
        ]
        return {"name": "测试组", "projects": projects}

    def _apply(self, band, interval):
        group = self._group()
        with patch.object(config, "BGI_FORCE_ENABLE_BAND", band), patch.object(
            config, "BGI_FORCE_ENABLE_AFTER_DISABLED", interval
        ):
            spacing = route_group.force_enable_band_interval()
            result = route_group.apply_targets(group, ["材料A"])
            other_enabled = [
                item["name"] for item in group["projects"]
                if item["status"] == "Enabled" and "材料A" not in item["name"]
            ]
        return spacing, result, other_enabled

    def test_band_off_leaves_everything_else_disabled(self):
        spacing, result, other_enabled = self._apply(band=False, interval=150)

        self.assertEqual(spacing, 0)
        self.assertEqual(other_enabled, [], "关掉隔离带后不该再穿插别的材料")
        self.assertEqual(result.matched, 3)
        self.assertEqual(list(result.forced), [])

    def test_band_on_inserts_at_the_configured_interval(self):
        spacing, result, other_enabled = self._apply(band=True, interval=3)

        self.assertEqual(spacing, 3)
        self.assertEqual(len(other_enabled), 5)
        self.assertEqual(len(result.forced), 5)

    def test_default_interval_does_not_trigger_in_a_small_group(self):
        spacing, result, other_enabled = self._apply(band=True, interval=150)

        self.assertEqual(spacing, 150)
        self.assertEqual(other_enabled, [])
        self.assertEqual(list(result.forced), [])

    def test_broken_interval_falls_back_to_the_default(self):
        with patch.object(config, "BGI_FORCE_ENABLE_BAND", True), patch.object(
            config, "BGI_FORCE_ENABLE_AFTER_DISABLED", "不是数字"
        ):
            self.assertEqual(route_group.force_enable_band_interval(), 150)


class GatherCooldownModeTests(unittest.TestCase):
    """冷却判定的两种模式：隔离带开着用"比例判定"，关掉就是"正常冷却"。"""

    def _status(self, band, ran, total=6):
        from skills import gather_cooldown

        now = datetime.datetime(2026, 9, 13, 12, 0, 0)
        with patch.object(config, "BGI_FORCE_ENABLE_BAND", band), patch.object(
            gather_cooldown, "last_collected", return_value=now - datetime.timedelta(hours=1)
        ), patch.object(
            gather_cooldown, "route_totals", return_value={"材料": total}
        ), patch.object(gather_cooldown, "session_routes", return_value=ran):
            return gather_cooldown.status("材料", now=now)

    def test_band_on_single_route_is_not_collected(self):
        result = self._status(band=True, ran=1)

        self.assertTrue(result["partial"])
        self.assertFalse(result["cooling"])

    def test_band_off_falls_back_to_normal_cooldown(self):
        """关掉隔离带 → 跑过就算采过（玩家要的"正常冷却"）。"""
        result = self._status(band=False, ran=1)

        self.assertFalse(result["partial"])
        self.assertTrue(result["cooling"])


if __name__ == "__main__":
    unittest.main()
