"""采集物 48 小时冷却检测（skills/gather_cooldown.py + 执行前的拦截）的测试。

玩家实测的需求：说「去采集霜仙花」时不能无脑跑 —— 地区特产 48 小时才刷新，
还在冷却里就该提示"还没刷新"，而不是白跑一趟。

数据来源是真实格式：BetterGI 日志里每条路线跑完会打
    → 脚本执行结束: "01-霜仙花-彩冰镇左上-3个.json", 耗时: 0分24.5秒
"""

import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from skills import bgi_controller, gather_cooldown

_MODULE_TMP = None
_MODULE_PATCHERS = []


def setUpModule():
    """把「脚本组目录 / 路线目录」指到空目录。

    冷却词表现在还会扫**玩家自建的小组**（蕈兽.json / 虹滴晶.json / 骗骗花.json…），
    不隔离的话用例会读到开发机真实的组：词表里平白多出魔物、断言随本机配置飘。
    """
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    for name, value in (
        ("BGI_SCRIPT_GROUP_DIR", os.path.join(_MODULE_TMP.name, "ScriptGroup")),
        ("BGI_AUTO_PATHING_DIR", os.path.join(_MODULE_TMP.name, "AutoPathing")),
    ):
        os.makedirs(value, exist_ok=True)
        patcher = patch.object(config, name, value)
        patcher.start()
        _MODULE_PATCHERS.append(patcher)
    gather_cooldown._ROUTE_INDEX_KEY["key"] = None
    gather_cooldown._ROUTE_TOTALS_CACHE_KEY["key"] = None


def tearDownModule():
    for patcher in _MODULE_PATCHERS:
        patcher.stop()
    _MODULE_PATCHERS.clear()
    if _MODULE_TMP is not None:
        _MODULE_TMP.cleanup()


def _write_log(path, entries):
    """按 BGI 的真实格式写一份日志：时间戳一行、正文一行。

    entries: [(时间 "HH:MM:SS", 路线名, 是否失败)]
    """
    lines = []
    for moment, route, failed in entries:
        lines.append(f"[{moment}.123] [INF] [Primary:S1:P1:T1] BetterGenshinImpact.Service.ScriptService")
        if failed:
            lines.append("[{}] [WRN] [Primary:S1:P1:T1] BetterGenshinImpact.GameTask.Common.TaskControl".format(moment))
            lines.append("此追踪脚本未正常走完！")
        lines.append(f'→ 脚本执行结束: "{route}", 耗时: 0分24.5秒')
        lines.append("------------------------------")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


class LogParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = self.tmp.name
        today = datetime.date.today().strftime("%Y%m%d")
        self.log_path = os.path.join(self.log_dir, f"better-genshin-impact{today}.log")
        patcher = patch.object(config, "BGI_LOG_DIR", self.log_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_route_name_gives_the_material(self):
        _write_log(self.log_path, [
            ("12:25:34", "01-霜仙花-彩冰镇左上-3个.json", False),
            ("12:26:59", "02-霜仙花-凯雷丝之翼-9个.json", False),
            ("12:27:39", "04-月莲-茸蕈窟-4个.json", False),
        ])

        events = gather_cooldown.parse_day(self.log_path)
        materials = [event["material"] for event in events]

        self.assertEqual(materials, ["霜仙花", "霜仙花", "月莲"])
        self.assertTrue(all(not event["failed"] for event in events))
        self.assertEqual(events[0]["at"].hour, 12)
        self.assertEqual(events[0]["at"].minute, 25)

    def test_failed_routes_do_not_count(self):
        """被停止快捷键打断的路线（「未正常走完」）不能算采过，否则会白等 48 小时。"""
        _write_log(self.log_path, [
            ("16:23:44", "04-便携轴承-蓝珀湖左上1-9个.json", True),
            ("16:38:30", "01-万相石-厄布拉神柱-26个.json", False),
        ])

        events = gather_cooldown.parse_day(self.log_path)

        self.assertTrue(events[0]["failed"])
        self.assertFalse(events[1]["failed"])
        self.assertIsNone(gather_cooldown.last_collected("便携轴承"))
        self.assertIsNotNone(gather_cooldown.last_collected("万相石"))

    def test_missing_directory_is_tolerated(self):
        with patch.object(config, "BGI_LOG_DIR", os.path.join(self.tmp.name, "没有这个目录")):
            self.assertEqual(gather_cooldown.scan_events(), [])
            self.assertIsNone(gather_cooldown.last_collected("霜仙花"))


class PartialCollectionTests(unittest.TestCase):
    """防闪退隔离带的误判（玩家实测）：只跑了 1 条隔离带路线，整种材料却被记成"采过"。

    实测数据：万相石 1/16、晶化骨髓 1/6、琉鳞石 1/6、星螺 1/5、珊瑚真珠 2/6 —— 都不该算采完。
    """

    def setUp(self):
        self.now = datetime.datetime(2026, 9, 13, 12, 0, 0)
        for name, value in (
            ("last_collected", self.now - datetime.timedelta(hours=1)),
            ("route_totals", {"霜仙花": 7, "万相石": 16, "晶化骨髓": 6}),
            ("session_routes", 1),
        ):
            patcher = patch.object(gather_cooldown, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_single_band_route_does_not_start_a_cooldown(self):
        with patch.object(gather_cooldown, "session_routes", return_value=1):
            result = gather_cooldown.status("万相石", now=self.now)

        self.assertTrue(result["partial"])
        self.assertFalse(result["cooling"])
        self.assertIn("不算跑完", gather_cooldown.describe(result))
        self.assertIn("1/16", gather_cooldown.describe(result))

    def test_full_collection_still_cools_down(self):
        with patch.object(gather_cooldown, "session_routes", return_value=7):
            result = gather_cooldown.status("霜仙花", now=self.now)

        self.assertFalse(result["partial"])
        self.assertTrue(result["cooling"])
        self.assertIn("还没刷新", gather_cooldown.describe(result))

    def test_mostly_collected_cools_down(self):
        """6 条里跑了 5 条（83%）→ 算采完（给"某条路线坏了"留余地）。"""
        with patch.object(gather_cooldown, "session_routes", return_value=5):
            result = gather_cooldown.status("晶化骨髓", now=self.now)

        self.assertFalse(result["partial"])
        self.assertTrue(result["cooling"])

    def test_half_collected_is_not_cooling(self):
        """只跑了一半 → 还有没采的，可以继续采（别让玩家白等 48 小时）。"""
        with patch.object(gather_cooldown, "session_routes", return_value=3):
            result = gather_cooldown.status("晶化骨髓", now=self.now)

        self.assertTrue(result["partial"])
        self.assertFalse(result["cooling"])

    def test_single_route_material_is_not_partial(self):
        """只有一条路线的材料不存在"部分采集"，照常冷却。"""
        with patch.object(gather_cooldown, "route_totals", return_value={"独苗": 1}), patch.object(
            gather_cooldown, "last_collected",
            return_value=self.now - datetime.timedelta(hours=1),
        ), patch.object(gather_cooldown, "session_routes", return_value=1):
            result = gather_cooldown.status("独苗", now=self.now)

        self.assertFalse(result["partial"])
        self.assertTrue(result["cooling"])


class RouteTotalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        group_path = os.path.join(self.tmp.name, "地图素材.json")
        with open(group_path, "w", encoding="utf-8") as handle:
            json.dump({
                "name": "地图素材",
                "projects": [
                    {"name": "01-霜仙花-彩冰镇左上-3个.json", "folderName": "地方特产\\挪德卡莱\\霜仙花"},
                    {"name": "02-霜仙花-凯雷丝之翼-9个.json", "folderName": "地方特产\\挪德卡莱\\霜仙花"},
                    {"name": "01-星螺-瑶光滩-17个.json", "folderName": "地方特产\\璃月\\星螺"},
                    {"name": "1. 高成功率路线", "folderName": "地方特产\\璃月"},
                ],
            }, handle, ensure_ascii=False)

        for name, value in (
            ("BGI_MAP_CONFIG", group_path),
            ("BGI_MINE_CONFIG", os.path.join(self.tmp.name, "没有.json")),
            ("BGI_COOK_CONFIG", os.path.join(self.tmp.name, "没有.json")),
        ):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        gather_cooldown._ROUTE_TOTALS_CACHE_KEY["key"] = None

    def test_counts_routes_per_material(self):
        totals = gather_cooldown.route_totals()

        self.assertEqual(totals.get("霜仙花"), 2)
        self.assertEqual(totals.get("星螺"), 1)
        # 目录层级抠出来的"璃月"不是材料名，不能混进来
        self.assertNotIn("璃月", totals)
        self.assertNotIn("高成功率路线", totals)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 9, 13, 12, 0, 0)
        patches = [
            patch.object(gather_cooldown, "last_collected"),
            # 这些用例只验证"时间算得对不对"，把"跑了几条路线"的判定打桩成"没有路线信息"
            # （total=0 → 不判部分采集），免得被开发机上真实的采集记录带偏。
            patch.object(gather_cooldown, "route_totals", return_value={}),
            patch.object(gather_cooldown, "session_routes", return_value=0),
        ]
        self.last = patches[0].start()
        for item in patches[1:]:
            item.start()
        for item in patches:
            self.addCleanup(item.stop)

    def test_unknown_material_is_treated_as_refreshed(self):
        self.last.return_value = None

        result = gather_cooldown.status("霜仙花", now=self.now)

        self.assertFalse(result["cooling"])
        self.assertFalse(result["known"])

    def test_within_cooldown_is_blocked(self):
        self.last.return_value = self.now - datetime.timedelta(hours=23)

        result = gather_cooldown.status("霜仙花", now=self.now)

        self.assertTrue(result["cooling"])
        self.assertEqual(round(result["hours_left"]), 25)

    def test_after_cooldown_is_refreshed(self):
        self.last.return_value = self.now - datetime.timedelta(hours=49)

        result = gather_cooldown.status("霜仙花", now=self.now)

        self.assertFalse(result["cooling"])
        self.assertEqual(result["hours_left"], 0.0)

    def test_description_is_human_readable(self):
        self.last.return_value = self.now - datetime.timedelta(hours=23)

        text = gather_cooldown.describe(gather_cooldown.status("霜仙花", now=self.now))

        self.assertIn("霜仙花", text)
        self.assertIn("还没刷新", text)
        self.assertIn("还要等", text)


class ForceTests(unittest.TestCase):
    def test_force_wording(self):
        for text in ("强制采集霜仙花", "我知道没刷新，照跑", "无视冷却去采霜仙花"):
            with self.subTest(text=text):
                self.assertTrue(gather_cooldown.is_forced(text))

    def test_normal_wording_is_not_force(self):
        for text in ("去采集霜仙花", "帮我采点月莲", "打一次秘境"):
            with self.subTest(text=text):
                self.assertFalse(gather_cooldown.is_forced(text))


class ResolveTests(unittest.TestCase):
    def test_material_name_passes_through(self):
        material, note = gather_cooldown.resolve_material("霜仙花")

        self.assertEqual(material, "霜仙花")
        self.assertEqual(note, "材料名")

    def test_character_name_resolves_to_its_specialty(self):
        """玩家说的是角色名（「蓝砚的突破材料」）时，用 TA 的 168 特产去查冷却。"""
        material, note = gather_cooldown.resolve_material("蓝砚的突破材料")

        self.assertEqual(material, "清水玉")
        self.assertIn("角色解析", note)

    def test_unknown_character_gets_an_honest_answer(self):
        """本地字典没有的新角色（例如奥黛塔）：说清认不出来，请玩家给材料名 —— 绝不瞎猜。"""
        material, note = gather_cooldown.resolve_material("奥黛塔")

        self.assertEqual(material, "")
        self.assertIn("请直接说材料名", note)


class ManualRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(gather_cooldown, "manual_state_path",
                               return_value=os.path.join(self.tmp.name, "manual.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        # ⚠️ 别读开发机上真实的 BGI 日志 / 路线组：日志窗口是"最近 3 小时"，
        #    真实日志里有没有这种材料的记录会随时间变，用例就会时好时坏。
        #    这里显式打桩成"日志里一条路线都没有"，正好覆盖"手动登记"最重要的那条路径。
        for name, value in (("route_totals", {"霜仙花": 7}), ("session_routes", 0)):
            stub = patch.object(gather_cooldown, name, return_value=value)
            stub.start()
            self.addCleanup(stub.stop)

    def test_manual_collection_starts_a_cooldown(self):
        """手动登记 = 玩家说「游戏里我自己采过了」→ 必须真的开始 48 小时冷却。

        实测踩过的坑：这一条以前靠「日志里 3 小时内有霜仙花路线」才碰巧通过；
        日志一过期（同一份代码、换个时间跑）就退化 —— 手动登记那次日志里当然没有路线记录，
        于是被判成「只采了一部分路线」（ran=0/7），**反而不冷却**，跟这个功能完全相反。
        """
        gather_cooldown.mark_manual("霜仙花")

        result = gather_cooldown.status("霜仙花", now=datetime.datetime.now())

        self.assertTrue(result["cooling"], "手动登记的「刚采过」必须真的进冷却")
        self.assertTrue(result["manual"])
        self.assertFalse(result["partial"], "手动登记不该被当成「部分采集」")
        self.assertIn("还没刷新", gather_cooldown.describe(result))

    def test_log_record_much_later_than_manual_wins(self):
        """日志里有更新的采集记录时，仍按日志判（手动记录不再「压住」比例规则）。"""
        gather_cooldown.mark_manual("霜仙花", when=datetime.datetime(2026, 9, 1, 8, 0, 0))
        logged = datetime.datetime(2026, 9, 13, 10, 0, 0)

        with patch.object(gather_cooldown, "last_collected", return_value=logged), patch.object(
            gather_cooldown, "session_routes", return_value=1
        ):
            result = gather_cooldown.status("霜仙花", now=logged + datetime.timedelta(hours=1))

        self.assertFalse(result["manual"])
        self.assertTrue(result["partial"], "日志里只跑了 1/7 条 → 仍按隔离带规则判")

    def test_clear_removes_the_record(self):
        gather_cooldown.mark_manual("霜仙花")
        gather_cooldown.clear_manual("霜仙花")

        self.assertEqual(gather_cooldown.load_manual(), {})

    def test_state_file_is_valid_json(self):
        gather_cooldown.mark_manual("月莲")

        with open(gather_cooldown.manual_state_path(), encoding="utf-8") as handle:
            data = json.load(handle)

        self.assertIn("月莲", data)


class MaterialExtractionTests(unittest.TestCase):
    """材料名从哪来 —— 全部用玩家脚本组里的**真实形态**做用例。

    实测踩到的坑（玩家报的）：说「去采集慕风蘑菇 / 沙脂蛹」时 Agent 说"认不出这种采集物"，
    可这两种材料的路线明明就在地图素材组里。原因：老逻辑只看 `folderName` 的最后一段，
    而他的组里是这种多级目录：

        地方特产\\蒙德\\慕风蘑菇\\无草神@Tool_tingsu      ← 最后一段是"无草神"（变体标签）
        地方特产\\须弥\\沙脂蛹\\1. 高成功率路线          ← 最后一段是"1. 高成功率路线"
        地方特产\\璃月\\清水玉\\清水玉@某人\\A组鼋背       ← 最后一段是"组名"

    现在改成**先读路线文件名**（`NN-材料名-地点-…` 是约定，最可靠），再从目录由深到浅
    跳过作者后缀与这些分组/变体标签。
    """

    def _material(self, name, folder):
        return gather_cooldown.material_from_project({"name": name, "folderName": folder})

    # ---------- 文件名（优先） ----------

    def test_route_filename_wins_over_a_variant_label_folder(self):
        self.assertEqual(
            self._material("01-慕风蘑菇-晨曦酒庄-7个.json",
                           "地方特产\\蒙德\\慕风蘑菇\\无草神@Tool_tingsu"),
            "慕风蘑菇",
        )

    def test_prefix_may_have_letters(self):
        cases = (
            ("09A-清心-层岩巨渊-32朵.json", "地方特产\\璃月\\清心", "清心"),
            ("A01-清水玉-沉玉谷上谷-水面鼋背-6个.json",
             "地方特产\\璃月\\清水玉\\清水玉@起个名字好难的喵\\A组鼋背", "清水玉"),
            ("E01-紫晶块-稻妻-清籁岛-天云峠-6个.json",
             "矿物\\紫晶块\\紫晶块[大剑]@蜜柑魚", "紫晶块"),
        )
        for name, folder, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(self._material(name, folder), expected)

    # ---------- 目录（兜底，文件名读不出来时） ----------

    def test_folder_walk_skips_group_and_variant_labels(self):
        cases = (
            ("1灵濛山.json", "地方特产\\璃月\\清水玉\\清水玉@MOMO", "清水玉"),
            ("1. 高成功率路线.json", "地方特产\\须弥\\沙脂蛹\\1. 高成功率路线", "沙脂蛹"),
            ("A02-清水玉-沉玉谷上谷-鼋背-6个.json",
             "地方特产\\璃月\\清水玉\\清水玉@某人\\B组静态", "清水玉"),
        )
        for name, folder, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(self._material(name, folder), expected)

    def test_underground_routes_merge_into_the_material(self):
        """`夜泊石地下@烤鱼` 是"夜泊石的另一种路线"，不是另一种材料。"""
        self.assertEqual(
            self._material("02-夜泊石-地下矿区-9个.json", "地方特产\\璃月\\夜泊石\\夜泊石地下@烤鱼"),
            "夜泊石",
        )

    def test_typo_names_are_normalised(self):
        """路线作者写错的材料名归到正式名（本地百科字典里只有「蒲公英籽」「珊瑚真珠」）。"""
        self.assertEqual(self._material("01-蒲公英-蒙德-5个.json", "地方特产\\蒙德\\蒲公英"),
                         "蒲公英籽")
        self.assertEqual(self._material("01-珊瑚珍珠-稻妻-5个.json", "地方特产\\稻妻\\珊瑚珍珠"),
                         "珊瑚真珠")

    def test_region_and_category_names_are_not_materials(self):
        for folder in ("地方特产\\璃月", "地方特产", "地方特产\\须弥", "矿物"):
            with self.subTest(folder=folder):
                self.assertEqual(self._material("没有材料名的路线.json", folder), "")

    def test_missing_fields_are_tolerated(self):
        self.assertEqual(gather_cooldown.material_from_project(None), "")
        self.assertEqual(gather_cooldown.material_from_project({}), "")
        self.assertEqual(gather_cooldown.material_from_project({"name": "x.json"}), "")


class RouteMaterialIndexTests(unittest.TestCase):
    """`material_for_route` + 路线索引：日志里只有文件名，名字不规矩时回查脚本组。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = self.tmp.name
        today = datetime.date.today().strftime("%Y%m%d")
        self.log_path = os.path.join(self.log_dir, f"better-genshin-impact{today}.log")
        for name, value in (("BGI_LOG_DIR", self.log_dir),):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        gather_cooldown._ROUTE_INDEX_KEY["key"] = None

    def test_letter_prefixed_route_name_in_the_log(self):
        """实测日志里真的有 `09A-清心-…` / `A01-清水玉-…` 这种命名。"""
        _write_log(self.log_path, [
            ("12:10:00", "09A-清心-层岩巨渊-32朵.json", False),
            ("12:20:00", "A01-清水玉-沉玉谷上谷-水面鼋背-6个.json", False),
        ])

        events = gather_cooldown.parse_day(self.log_path)

        self.assertEqual([event["material"] for event in events], ["清心", "清水玉"])

    def test_unparsable_name_falls_back_to_the_group_index(self):
        _write_log(self.log_path, [("12:30:00", "1灵濛山.json", False)])

        with patch.object(gather_cooldown, "route_material_index",
                          return_value={"1灵濛山.json": "清水玉"}):
            events = gather_cooldown.parse_day(self.log_path)

        self.assertEqual(events[0]["material"], "清水玉")
        self.assertEqual(events[0]["route"], "1灵濛山.json")


class FilterTests(unittest.TestCase):
    """执行前的拦截：`bgi_controller._filter_cooldown`（四类资源都会过一遍）。"""

    def setUp(self):
        patcher = patch.object(gather_cooldown, "status")
        self.status = patcher.start()
        self.addCleanup(patcher.stop)

    def _cooling(self, material):
        return {
            "material": material, "last_at": datetime.datetime(2026, 9, 13, 12, 34),
            "hours_ago": 1.0, "hours_left": 47.0, "cooling": True, "known": True,
        }

    def _ready(self, material):
        return {
            "material": material, "last_at": None, "hours_ago": None,
            "hours_left": 0.0, "cooling": False, "known": False,
        }

    def test_cooling_material_is_dropped_with_an_explanation(self):
        self.status.side_effect = lambda material, **kwargs: self._cooling(material)

        kept, lines = bgi_controller._filter_cooldown(["霜仙花"])

        self.assertEqual(kept, [])
        self.assertTrue(any("霜仙花" in line for line in lines))

    def test_refreshed_material_is_kept(self):
        self.status.side_effect = lambda material, **kwargs: self._ready(material)

        kept, lines = bgi_controller._filter_cooldown(["霜仙花"])

        self.assertEqual(kept, ["霜仙花"])
        self.assertEqual(lines, [])

    def test_force_keeps_it_and_says_so(self):
        self.status.side_effect = lambda material, **kwargs: self._cooling(material)

        kept, lines = bgi_controller._filter_cooldown(["霜仙花"], force=True)

        self.assertEqual(kept, ["霜仙花"])
        self.assertTrue(any("强制" in line for line in lines))

    def test_unresolvable_target_is_kept_but_flagged(self):
        """认不出的**特产**目标不能静默丢掉（下游会按老逻辑提示玩家找不到路线）。"""
        with patch.object(
            gather_cooldown, "resolve_material",
            side_effect=lambda target, category=None: ("", "认不出来"),
        ):
            kept, lines = bgi_controller._filter_cooldown(["奥黛塔"])

        self.assertEqual(kept, ["奥黛塔"])
        self.assertTrue(any("认不出来" in line for line in lines))

    def test_unresolvable_mob_is_kept_without_a_warning(self):
        """魔物这边认不出就**别吭声**：玩家的组五花八门，我们的词表定不了它是不是无效目标。

        实测（玩家 QQ 记录）：说「打蕈兽 / 打骗骗花 / 去采集虹滴晶」时每条都被回一句
        「认不出「蕈兽」是哪种魔物（例如「蕈兽」）」—— 那些名字其实都能跑，纯属噪音。
        """
        with patch.object(
            gather_cooldown, "resolve_material",
            side_effect=lambda target, category=None: ("", "认不出来"),
        ):
            kept, lines = bgi_controller._filter_cooldown(["刀镡"], category="hunt")

        self.assertEqual(kept, ["刀镡"])
        self.assertEqual(lines, [])

    def test_category_is_passed_through(self):
        """刷怪时只按魔物名解析（别把「蕈兽」当特产去查），并且按魔物的时长算冷却。"""
        seen = {}

        def fake_status(material, **kwargs):
            seen["material"] = material
            seen["hours_category"] = kwargs.get("category")
            return self._ready(material)

        self.status.side_effect = fake_status
        with patch.object(
            gather_cooldown, "resolve_material",
            side_effect=lambda target, category=None: (seen.setdefault("category", category), "魔物名"),
        ):
            bgi_controller._filter_cooldown(["蕈兽"], category="hunt")

        self.assertEqual(seen.get("category"), "hunt")
        self.assertEqual(seen.get("hours_category"), "hunt")

    def test_old_alias_still_works(self):
        """老名字 `_filter_gather_cooldown` 还留着（老脚本/老文档里在用）。"""
        self.status.side_effect = lambda material, **kwargs: self._ready(material)

        kept, lines = bgi_controller._filter_gather_cooldown(["霜仙花"])

        self.assertEqual(kept, ["霜仙花"])


class ExtraGroupTests(unittest.TestCase):
    """玩家**自建**的小组（蕈兽.json / 骗骗花.json / 虹滴晶.json…）。

    为什么单独立一组用例：玩家实测「去打蕈兽 / 只打骗骗花 / 去打飘浮灵 / 去采集虹滴晶」
    四条指令全都回了「认不出…」—— 因为这些名字只在玩家自建的组里，
    而冷却词表当时只读四个"总组"（总组里只有巡陆艇）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.group_dir = os.path.join(self.root, "ScriptGroup")
        self.pathing_dir = os.path.join(self.root, "AutoPathing")
        os.makedirs(self.group_dir)
        # 真实情况：`AutoPathing/敌人与魔物/<魔物名>` 是魔物的权威名单
        os.makedirs(os.path.join(self.pathing_dir, "敌人与魔物", "蕈兽"))

        self._write_group("蕈兽.json", [
            # 文件名不带材料名（作者随手写的），材料得从目录 `蕈兽\蕈兽@某人` 读
            {"name": "须弥-二净甸天臂池西南-5个.json", "folderName": "蕈兽\\蕈兽@某人"},
        ])
        self._write_group("虹滴晶.json", [
            # 文件名读出来的是地名「那夏镇下方」，必须让位给目录里的「虹滴晶」
            {"name": "01-那夏镇下方-4个.json", "folderName": "矿物\\虹滴晶"},
        ])
        self._write_group("狗粮.json", [
            {"name": "01-狗粮-某地-3个.json", "folderName": "狗粮\\狗粮@某人"},
        ])

        for name, value in (
            ("BGI_SCRIPT_GROUP_DIR", self.group_dir),
            ("BGI_AUTO_PATHING_DIR", self.pathing_dir),
            ("BGI_MAP_CONFIG", ""),
            ("BGI_MINE_CONFIG", ""),
            ("BGI_COOK_CONFIG", ""),
            ("BGI_ENEMY_CONFIG", ""),
        ):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        gather_cooldown._ROUTE_INDEX_KEY["key"] = None
        gather_cooldown._ROUTE_TOTALS_CACHE_KEY["key"] = None

    def _write_group(self, filename, projects):
        with open(os.path.join(self.group_dir, filename), "w", encoding="utf-8") as handle:
            json.dump({"name": filename[:-5], "projects": projects}, handle, ensure_ascii=False)

    def test_mob_group_lands_in_the_hunt_category(self):
        self.assertIn("蕈兽", gather_cooldown.route_material_vocabulary(("hunt",)))
        self.assertEqual(gather_cooldown.category_of("蕈兽"), "hunt")
        self.assertEqual(gather_cooldown.hours_for("蕈兽"), 12)

    def test_mineral_group_lands_in_the_mine_category(self):
        self.assertIn("虹滴晶", gather_cooldown.route_material_vocabulary(("mine",)))
        self.assertEqual(gather_cooldown.category_of("虹滴晶"), "mine")
        self.assertEqual(gather_cooldown.hours_for("虹滴晶"), 72)
        self.assertEqual(gather_cooldown.route_totals().get("虹滴晶"), 1)

    def test_unclassifiable_group_is_skipped(self):
        """定不出类别的组（狗粮这种）**不进词表**：否则面板里会出现「狗粮」这种名字。"""
        every = gather_cooldown.route_material_vocabulary()
        self.assertNotIn("狗粮", every)
        self.assertNotIn("狗粮", gather_cooldown.material_category_index())

    def test_targets_resolve_through_their_own_groups(self):
        for text, category, expected in (
            ("去打蕈兽", "hunt", "蕈兽"),
            ("去采集虹滴晶", "mine", "虹滴晶"),
        ):
            material, note = gather_cooldown.resolve_material(text, category=category)
            self.assertEqual(material, expected, text)
            self.assertTrue(note, text)

    def test_route_name_follows_the_group_when_they_disagree(self):
        """文件名读出的是地名 → 以脚本组为准（`01-那夏镇下方-4个.json` → 虹滴晶）。"""
        self.assertEqual(gather_cooldown.material_for_route("01-那夏镇下方-4个.json"), "虹滴晶")

    def test_mob_cooldown_is_visible_in_the_plan_lines(self):
        events = [{
            "material": "蕈兽", "route": "须弥-二净甸天臂池西南-5个.json",
            "at": datetime.datetime.now() - datetime.timedelta(hours=2), "failed": False,
        }]

        with patch.object(gather_cooldown, "scan_events", return_value=events):
            lines, blocked = gather_cooldown.notice_lines(["蕈兽"], category="hunt")

        self.assertEqual(blocked, ["蕈兽"])
        self.assertTrue(any("还没刷新" in line for line in lines), lines)

    def test_unresolvable_mob_gets_no_plan_line(self):
        lines, blocked = gather_cooldown.notice_lines(["不存在的魔物"], category="hunt")

        self.assertEqual(lines, [])
        self.assertEqual(blocked, [])

    def test_unresolvable_specialty_still_gets_a_plan_line(self):
        """特产那边保持原样：词表是封闭集合，认不出就是名字写错了，值得说一句。"""
        lines, blocked = gather_cooldown.notice_lines(["去采集没听过的东西"])

        self.assertEqual(blocked, [])
        self.assertTrue(any(line.startswith("⚠️") for line in lines), lines)


class FullCatalogueTests(unittest.TestCase):
    """材料清单来自**路线仓库的全量目录**（归档里的那个组），不是当前那份被精简过的组。

    玩家实测两件事（QQ 记录）：
      · 「食材与炼金」当时只列出久雨莲 —— 因为 `食材与炼金.json` 那份被 Agent 精简过；
      · 然后他说「去采集竹笋」**真的跑成功了**（3 条路线，7 分 47 秒）——
        组文件被改写成只有 3 条竹笋，全量清单（2342 条 / 59 种材料）在 `.gi_agent_archive/` 里。
    所以：清单/索引读归档全量，**分母**（比例判定）读当前那份组。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.group_dir = os.path.join(self.root, "ScriptGroup")
        self.pathing_dir = os.path.join(self.root, "AutoPathing")
        os.makedirs(self.group_dir)

        # 路线仓库：食材与炼金 4 个材料目录（一个是整包脚本，不算材料）+ 特产/矿物/魔物
        for name in ("久雨莲", "甜甜花", "薄荷", "提瓦特食材一条龙"):
            os.makedirs(os.path.join(self.pathing_dir, "食材与炼金", name))
        os.makedirs(os.path.join(self.pathing_dir, "地方特产", "璃月", "清心"))
        os.makedirs(os.path.join(self.pathing_dir, "地方特产", "璃月", "琉璃袋"))
        os.makedirs(os.path.join(self.pathing_dir, "矿物", "铁块"))
        os.makedirs(os.path.join(self.pathing_dir, "敌人与魔物", "蕈兽"))

        # 当前那份组：只剩上次跑的久雨莲（模拟 Agent 精简过的样子）
        self._write_group("食材与炼金.json", [
            {"name": "01-久雨莲-厄里那斯-7个.json", "folderName": "食材与炼金\\久雨莲"},
        ])
        # 归档里的全量清单：还有甜甜花 / 薄荷；另外塞两条**地名当材料**的脏数据
        self._write_group(os.path.join(".gi_agent_archive", "食材与炼金.json"), [
            {"name": "01-久雨莲-厄里那斯-7个.json", "folderName": "食材与炼金\\久雨莲"},
            {"name": "01-甜甜花-蒙德-9个.json", "folderName": "食材与炼金\\甜甜花"},
            {"name": "02-薄荷-璃月-12个.json", "folderName": "食材与炼金\\薄荷"},
            {"name": "12-苔古荒原上方-6个.json", "folderName": "食材与炼金\\兽肉\\某作者"},
        ], mkdir=True)
        self._write_group("地图素材.json", [
            {"name": "09A-清心-层岩巨渊-32朵.json", "folderName": "地方特产\\璃月\\清心"},
            {"name": "03-琉璃袋-层岩巨渊-8朵.json", "folderName": "地方特产\\璃月\\琉璃袋"},
        ])

        for name, value in (
            ("BGI_SCRIPT_GROUP_DIR", self.group_dir),
            ("BGI_AUTO_PATHING_DIR", self.pathing_dir),
            ("BGI_MAP_CONFIG", os.path.join(self.group_dir, "地图素材.json")),
            ("BGI_COOK_CONFIG", os.path.join(self.group_dir, "食材与炼金.json")),
            ("BGI_MINE_CONFIG", ""),
            ("BGI_ENEMY_CONFIG", ""),
        ):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        gather_cooldown._PATHING_CACHE.update({"key": None, "names": {}})
        gather_cooldown._ROUTE_INDEX_KEY["key"] = None
        gather_cooldown._ROUTE_TOTALS_CACHE_KEY["key"] = None

    def _write_group(self, relative, projects, mkdir=False):
        path = os.path.join(self.group_dir, relative)
        if mkdir:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"name": os.path.basename(relative)[:-5], "projects": projects},
                      handle, ensure_ascii=False)
        return path

    def test_pathing_walk_reads_materials_at_the_right_depth(self):
        self.assertEqual(gather_cooldown.pathing_materials("cook"), ["久雨莲", "甜甜花", "薄荷"])
        self.assertEqual(gather_cooldown.pathing_materials("specialty"), ["清心", "琉璃袋"])
        self.assertEqual(gather_cooldown.pathing_materials("mine"), ["铁块"])
        self.assertEqual(gather_cooldown.pathing_materials("hunt"), ["蕈兽"])

    def test_whole_package_scripts_are_not_materials(self):
        """「提瓦特食材一条龙」是整包脚本，不是材料。"""
        self.assertNotIn("提瓦特食材一条龙", gather_cooldown.pathing_materials("cook"))

    def test_vocabulary_comes_from_the_archive_not_the_shrunk_group(self):
        vocabulary = gather_cooldown.route_material_vocabulary()

        self.assertIn("甜甜花", vocabulary)      # 只在归档里
        self.assertIn("薄荷", vocabulary)
        self.assertIn("久雨莲", vocabulary)      # 两边都有
        self.assertIn("琉璃袋", vocabulary)      # 地图素材组（还没被精简过）

    def test_place_names_are_not_materials(self):
        """归档里那条 `12-苔古荒原上方-6个.json` 挂在兽肉目录下 → 材料是「兽肉」，不是地名。

        ⚠️ 这里故意只造了「兽肉」这个目录但没有仓库目录？—— 有：`食材与炼金/兽肉` 没建，
        所以这条会被仓库校验挡掉（宁缺勿滥），不会变成「苔古荒原上方」。
        """
        vocabulary = gather_cooldown.route_material_vocabulary()

        self.assertNotIn("苔古荒原上方", vocabulary)
        self.assertNotIn("苔古荒原上方", gather_cooldown.material_category_index())

    def test_route_index_still_maps_the_shrunk_route_name(self):
        """日志里只有文件名，且文件名不含材料（`01-久雨莲-…` 之外的那类）→ 靠归档索引回查。"""
        self.assertEqual(
            gather_cooldown.material_for_route("02-薄荷-璃月-12个.json"), "薄荷"
        )

    def test_totals_use_the_current_group_as_the_denominator(self):
        """分母只看**当前那份组**：跑竹笋时组里就 3 条，不能拿全量 2342 条当分母。"""
        totals = gather_cooldown.route_totals()

        self.assertEqual(totals.get("久雨莲"), 1)     # 当前组里就 1 条
        self.assertIsNone(totals.get("甜甜花"))       # 归档里有、当前组里没有 → 不设分母
        self.assertEqual(totals.get("清心"), 1)
        self.assertEqual(totals.get("琉璃袋"), 1)

    def test_sections_list_the_whole_category(self):
        data = gather_cooldown.overview()

        sections = {section["key"]: section for section in data["sections"]}
        names = {row["material"] for row in sections["cook"]["materials"]}
        self.assertTrue({"久雨莲", "甜甜花", "薄荷"} <= names, names)

    def test_missing_pathing_dir_falls_back_to_the_old_reading(self):
        """没装 BetterGI / 读不到仓库目录时，退回老的"文件名优先"读法，别把清单清空。"""
        with patch.object(config, "BGI_AUTO_PATHING_DIR", os.path.join(self.root, "没有这个目录")):
            gather_cooldown._PATHING_CACHE.update({"key": None, "names": {}})
            gather_cooldown._ROUTE_INDEX_KEY["key"] = None

            self.assertEqual(gather_cooldown.pathing_materials_all(), {})
            self.assertIn("久雨莲", gather_cooldown.route_material_vocabulary())


class PromptBlockTests(unittest.TestCase):
    def test_block_lists_cooling_materials_and_the_rules(self):
        with patch.object(gather_cooldown, "cooling_materials", return_value=[{
            "material": "霜仙花", "last_at": datetime.datetime(2026, 9, 13, 12, 34),
            "hours_ago": 1.0, "hours_left": 47.0, "cooling": True, "known": True,
        }]):
            block = gather_cooldown.cooldown_block()

        self.assertIn("资源冷却", block)
        self.assertIn("霜仙花", block)
        self.assertIn("48h", block)
        self.assertIn("72h", block)
        self.assertIn("12h", block)
        self.assertIn("强制", block)
        self.assertIn("别猜", block)

    def test_no_block_when_nothing_is_cooling(self):
        with patch.object(gather_cooldown, "cooling_materials", return_value=[]):
            self.assertEqual(gather_cooldown.cooldown_block(), "")


if __name__ == "__main__":
    unittest.main()
