"""`overview()`（Studio 资源冷却页用的汇总）的测试。"""
import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from skills import gather_cooldown


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.log_dir = os.path.join(self.root, "log")
        os.makedirs(self.log_dir)
        # ⚠️ 日志文件名必须用**今天**的日期：`log_paths()` 是按"今天往前 N 天"拼文件名的，
        #    写死某个日期的话，换个日子跑这条用例就读不到日志了（实测踩过）。
        self.today = datetime.date.today()
        self.now = datetime.datetime.combine(self.today, datetime.time(12, 0, 0))

        group_path = os.path.join(self.root, "地图素材.json")
        with open(group_path, "w", encoding="utf-8") as handle:
            json.dump({
                "name": "地图素材",
                "projects": [
                    {"name": "01-霜仙花-彩冰镇-3个.json", "folderName": "地方特产\\挪德卡莱\\霜仙花"},
                    {"name": "02-霜仙花-凯雷丝之翼-9个.json", "folderName": "地方特产\\挪德卡莱\\霜仙花"},
                    {"name": "01-慕风蘑菇-晨曦酒庄-7个.json", "folderName": "地方特产\\蒙德\\慕风蘑菇\\无草神@某人"},
                ],
            }, handle, ensure_ascii=False)

        self.manual = os.path.join(self.root, "manual.json")
        for name, value in (
            ("BGI_LOG_DIR", self.log_dir),
            ("BGI_MAP_CONFIG", group_path),
            # ⚠️ 其余三个类目的组也要指空：不然会读到开发机真实的矿物/食材/魔物组，
            #    用例就会随本机配置飘（实测：真实的「敌人与魔物」组里有巡陆艇，材料数就多一个）。
            ("BGI_MINE_CONFIG", ""),
            ("BGI_COOK_CONFIG", ""),
            ("BGI_ENEMY_CONFIG", ""),
        ):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (
            ("manual_state_path", lambda: self.manual),
        ):
            patcher = patch.object(gather_cooldown, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        gather_cooldown._ROUTE_TOTALS_CACHE_KEY["key"] = None
        gather_cooldown._ROUTE_INDEX_KEY["key"] = None

    def _write_log(self, entries):
        path = os.path.join(
            self.log_dir, f"better-genshin-impact{self.today.strftime('%Y%m%d')}.log"
        )
        lines = []
        for moment, route in entries:
            lines.append(f"[{moment}.000] [INF] BetterGenshinImpact.Service.ScriptService")
            lines.append(f'→ 脚本执行结束: "{route}", 耗时: 0分10.0秒')
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def test_overview_reports_cooling_and_ready(self):
        self._write_log([
            ("11:30:00", "01-霜仙花-彩冰镇-3个.json"),
            ("11:40:00", "02-霜仙花-凯雷丝之翼-9个.json"),
        ])

        data = gather_cooldown.overview(now=self.now)

        rows = {row["material"]: row for row in data["materials"]}
        self.assertTrue(rows["霜仙花"]["cooling"])
        self.assertAlmostEqual(rows["霜仙花"]["hours_left"], 47.5, delta=0.2)
        self.assertEqual(rows["霜仙花"]["ran_routes"], 2)
        self.assertEqual(rows["霜仙花"]["total_routes"], 2)
        # 慕风蘑菇没采过 → 可以采；名字要按"先文件名"读（目录末段是"无草神"）
        self.assertFalse(rows["慕风蘑菇"]["cooling"])
        self.assertFalse(rows["慕风蘑菇"]["known"])
        self.assertEqual(data["summary"]["cooling"], 1)
        self.assertEqual(data["summary"]["total"], 2)

    def test_cooling_rows_come_first(self):
        self._write_log([("11:00:00", "01-慕风蘑菇-晨曦酒庄-7个.json")])

        data = gather_cooldown.overview(now=self.now)

        self.assertEqual(data["materials"][0]["material"], "慕风蘑菇", "冷却中的要排最前面")
        self.assertTrue(data["materials"][0]["cooling"])

    def test_partial_collection_is_not_cooling(self):
        """只跑了一条隔离带路线（1/2）→ 不算跑完，可以继续跑。"""
        self._write_log([("11:00:00", "01-霜仙花-彩冰镇-3个.json")])

        data = gather_cooldown.overview(now=self.now)

        row = next(item for item in data["materials"] if item["material"] == "霜仙花")
        self.assertTrue(row["partial"])
        self.assertFalse(row["cooling"])
        self.assertEqual(row["ran_routes"], 1)
        self.assertEqual(data["summary"]["partial"], 1)

    def test_manual_records_are_included(self):
        gather_cooldown.mark_manual("沙脂蛹")     # 组里没有的路线也能登记

        data = gather_cooldown.overview(now=self.now)

        names = {row["material"] for row in data["materials"]}
        self.assertIn("沙脂蛹", names)
        row = next(item for item in data["materials"] if item["material"] == "沙脂蛹")
        self.assertTrue(row["cooling"])
        self.assertTrue(row["manual"])
        self.assertEqual(data["summary"]["manual"], 1)

    def test_no_logs_is_fine(self):
        data = gather_cooldown.overview(now=self.now)

        self.assertTrue(data["materials"])
        self.assertEqual(data["summary"]["cooling"], 0)
        self.assertEqual(data["events"], 0)
        self.assertEqual(data["hours"], gather_cooldown.category_hours("specialty"))
        self.assertEqual(data["min_route_percent"], 80)

    def test_four_categories_with_their_own_hours(self):
        """四类资源各有各的刷新时长：特产 48 / 矿物 72 / 食材 24 / 魔物 12（默认值）。"""
        with open(os.path.join(self.root, "矿物.json"), "w", encoding="utf-8") as handle:
            json.dump({"name": "矿物", "projects": [
                {"name": "01-水晶块-层岩-7个.json", "folderName": "矿物\\水晶块"},
                {"name": "01-星银矿石-雪山-5个.json", "folderName": "矿物\\星银矿石"},
            ]}, handle, ensure_ascii=False)
        with open(os.path.join(self.root, "敌人与魔物.json"), "w", encoding="utf-8") as handle:
            json.dump({"name": "敌人与魔物", "projects": [
                {"name": "01-巡陆艇-空寂走廊-3个.json", "folderName": "敌人与魔物\\巡陆艇"},
            ]}, handle, ensure_ascii=False)

        with patch.object(config, "BGI_MINE_CONFIG", os.path.join(self.root, "矿物.json")), patch.object(
            config, "BGI_ENEMY_CONFIG", os.path.join(self.root, "敌人与魔物.json")
        ):
            gather_cooldown._ROUTE_TOTALS_CACHE_KEY["key"] = None
            gather_cooldown._ROUTE_INDEX_KEY["key"] = None
            data = gather_cooldown.overview(now=self.now)

            self.assertEqual(gather_cooldown.category_of("霜仙花"), "specialty")
            self.assertEqual(gather_cooldown.category_of("水晶块"), "mine")
            self.assertEqual(gather_cooldown.category_of("巡陆艇"), "hunt")
            # 矿物按材料分档（wiki：铁/白铁次日、星银第二日、水晶/紫晶第三日）
            self.assertEqual(gather_cooldown.hours_for("水晶块"), 72)
            self.assertEqual(gather_cooldown.hours_for("星银矿石"), 48)
            self.assertEqual(gather_cooldown.hours_for("霜仙花"), 48)
            self.assertEqual(gather_cooldown.hours_for("巡陆艇"), 12)

        labels = [section["label"] for section in data["sections"]]
        self.assertIn("地区特产", labels)
        self.assertIn("矿物", labels)
        self.assertIn("敌人与魔物", labels)
        hours = {section["label"]: section["hours"] for section in data["sections"]}
        self.assertEqual(hours["矿物"], 72)
        self.assertEqual(hours["敌人与魔物"], 12)

    def test_hunt_cooldown_is_twelve_hours(self):
        """魔物 12 小时：11:00 刷过 → 13:00 还在冷却，23:30 就该刷新了。"""
        self._write_log([("11:00:00", "01-巡陆艇-空寂走廊-3个.json")])
        enemy_group = os.path.join(self.root, "敌人与魔物.json")
        with open(enemy_group, "w", encoding="utf-8") as handle:
            json.dump({"name": "敌人与魔物", "projects": [
                {"name": "01-巡陆艇-空寂走廊-3个.json", "folderName": "敌人与魔物\\巡陆艇"},
            ]}, handle, ensure_ascii=False)

        with patch.object(config, "BGI_ENEMY_CONFIG", enemy_group):
            gather_cooldown._ROUTE_TOTALS_CACHE_KEY["key"] = None
            gather_cooldown._ROUTE_INDEX_KEY["key"] = None
            early = gather_cooldown.overview(now=self.now.replace(hour=13))
            late = gather_cooldown.overview(now=self.now.replace(hour=23, minute=30))

        row_early = next(r for r in early["materials"] if r["material"] == "巡陆艇")
        row_late = next(r for r in late["materials"] if r["material"] == "巡陆艇")
        self.assertTrue(row_early["cooling"], "2 小时后还在 12 小时冷却里")
        self.assertFalse(row_late["cooling"], "12.5 小时后应该刷新了")
        self.assertEqual(row_early["hours"], 12)

    def test_category_hours_follow_the_env(self):
        with patch.object(config, "HUNT_COOLDOWN_HOURS", 6), patch.object(
            config, "MINE_COOLDOWN_HOURS", 48
        ):
            self.assertEqual(gather_cooldown.category_hours("hunt"), 6)
            self.assertEqual(gather_cooldown.category_hours("mine"), 48)
            self.assertEqual(gather_cooldown.hours_for("紫晶块"), 72, "按材料的覆盖表优先")
            self.assertEqual(gather_cooldown.hours_for("没听过的矿物"), 48)

    def test_resolve_material_respects_the_category(self):
        """「蕈兽」在魔物类别里按魔物解析；跨类别一律认不出来（免得张冠李戴）。"""
        with open(os.path.join(self.root, "敌人与魔物.json"), "w", encoding="utf-8") as handle:
            json.dump({"name": "敌人与魔物", "projects": [
                {"name": "01-蕈兽-须弥-3个.json", "folderName": "敌人与魔物\\蕈兽"},
            ]}, handle, ensure_ascii=False)
        with patch.object(config, "BGI_ENEMY_CONFIG", os.path.join(self.root, "敌人与魔物.json")):
            gather_cooldown._ROUTE_INDEX_KEY["key"] = None

            self.assertEqual(gather_cooldown.resolve_material("蕈兽", category="hunt")[0], "蕈兽")
            self.assertEqual(gather_cooldown.resolve_material("霜仙花", category="hunt")[0], "")
            _, note = gather_cooldown.resolve_material("霜仙花", category="hunt")
            self.assertIn("魔物", note)

    def test_band_switch_toggles_the_judgement(self):
        self._write_log([("11:00:00", "01-霜仙花-彩冰镇-3个.json")])

        with patch.object(config, "BGI_FORCE_ENABLE_BAND", False):
            data = gather_cooldown.overview(now=self.now)

        row = next(item for item in data["materials"] if item["material"] == "霜仙花")
        self.assertFalse(data["band_enabled"])
        self.assertFalse(row["partial"], "关掉隔离带就是正常冷却：跑过一条即算采过")
        self.assertTrue(row["cooling"])


if __name__ == "__main__":
    unittest.main()
