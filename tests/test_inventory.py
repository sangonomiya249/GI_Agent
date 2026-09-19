"""米游社库存同步（skills/mys_inventory.py）的测试。

覆盖规格书里最要紧的几条：
  · 库存统一成内部模型（**不把米游社原始 JSON 泄漏给上层**）；
  · 快照正确保存、历史快照可以读（§35.21 / §35.22）；
  · 同步失败保留上一次成功快照，并且**明确标记不是实时数据**（§35.11 / §35.23）；
  · Cookie 失效有可操作的提示（§35.12）；
  · UID 对不上时宁可失败也不串号。
"""

import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from brain import growth_models
from skills import mys_api, mys_inventory


class TempInventoryCase(unittest.TestCase):
    """把库存文件与数据库都指到临时目录（绝不写玩家真实的 memory/）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.latest = os.path.join(self.root, "inventory_latest.json")
        self.db = os.path.join(self.root, "growth.db")
        self._patches = [
            patch.object(config, "GROWTH_MYS_DIR", self.root),
            patch.object(config, "GROWTH_DB_PATH", self.db),
            patch.object(config, "MYS_UID", ""),
            patch.object(config, "DEFAULT_UID", ""),
            patch.object(config, "GROWTH_INVENTORY_STALE_HOURS", 12),
        ]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)
        from brain import growth_db

        growth_db.close(self.db)
        self.addCleanup(lambda: growth_db.close(self.db))

    def snapshot_payload(self, counts=None, fetched_at=None):
        counts = counts if counts is not None else {104001: 1250000, 100092: 87}
        return {
            "uid": "100000001",
            "region": "cn_gf01",
            "item_list": [
                {"item_id": item_id, "num": count, "name": _NAMES.get(item_id, "")}
                for item_id, count in counts.items()
            ],
            "fetched_at": fetched_at,
        }


_NAMES = {104001: "摩拉", 100092: "清心", 202001: "大英雄的经验", 104301: "「诗文」的哲学"}


class ItemModelTests(unittest.TestCase):
    """内部库存模型：字段别名、分类、同名取大。"""

    def test_parse_item_accepts_alias_field_names(self):
        for raw in (
            {"item_id": 104001, "num": 5},
            {"id": 104001, "count": 5},
            {"material_id": 104001, "quantity": 5},
        ):
            item = mys_inventory.parse_item(raw)
            self.assertEqual(item["item_id"], 104001, raw)
            self.assertEqual(item["count"], 5, raw)

    def test_parse_item_rejects_records_without_an_id(self):
        self.assertIsNone(mys_inventory.parse_item({"num": 5}))
        self.assertIsNone(mys_inventory.parse_item("不是字典"))
        self.assertIsNone(mys_inventory.parse_item(None))

    def test_category_is_filled_from_name_or_id(self):
        self.assertEqual(mys_inventory.parse_item({"item_id": 104001, "num": 1})["category"],
                         growth_models.CATEGORY_CURRENCY)
        self.assertEqual(mys_inventory.parse_item(
            {"item_id": 999999, "num": 1, "name": "「诗文」的哲学"})["category"],
            growth_models.CATEGORY_TALENT_BOOK)

    def test_duplicate_ids_keep_the_larger_count(self):
        items = mys_inventory.normalise_items([
            {"item_id": 104001, "num": 10},
            {"item_id": 104001, "num": 99},
        ])

        self.assertEqual(items["item_104001"]["count"], 99)

    def test_normalise_payload_builds_the_internal_model(self):
        payload = {"uid": "1", "region": "cn_gf01",
                   "item_list": [{"item_id": 104001, "num": 7, "name": "摩拉"}]}

        model = mys_inventory.normalise_payload(payload, uid="1")

        self.assertEqual(model["uid"], "1")
        self.assertEqual(model["server"], "cn_gf01")
        self.assertTrue(model["fetched_at"])
        self.assertEqual(list(model["items"]), ["item_104001"])


class SnapshotFileTests(TempInventoryCase):
    """快照落盘 / 读取（§35.21 / §35.22）。"""

    def test_save_and_load_round_trip(self):
        model = mys_inventory.normalise_payload(self.snapshot_payload(), uid="100000001")

        mys_inventory.save_inventory(model, path=self.latest)
        loaded = mys_inventory.load_inventory(path=self.latest)

        self.assertEqual(loaded["uid"], "100000001")
        self.assertEqual(loaded["items"]["item_104001"]["count"], 1250000)

    def test_atomic_write_leaves_no_tmp_file(self):
        mys_inventory.save_inventory(
            mys_inventory.normalise_payload(self.snapshot_payload()), path=self.latest
        )

        self.assertTrue(os.path.isfile(self.latest))
        self.assertFalse(os.path.exists(self.latest + ".tmp"))

    def test_missing_or_broken_file_is_tolerated(self):
        self.assertIsNone(mys_inventory.load_inventory(path=self.latest))

        with open(self.latest, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")

        self.assertIsNone(mys_inventory.load_inventory(path=self.latest))

    def test_model_without_items_is_treated_as_missing(self):
        """只有 uid 没有 items 的文件当成"没同步过" —— 别让空对象假装成有效库存。"""
        with open(self.latest, "w", encoding="utf-8") as handle:
            json.dump({"uid": "1", "items": []}, handle)

        self.assertIsNone(mys_inventory.load_inventory(path=self.latest))

    def test_history_snapshot_can_be_read_back(self):
        model = mys_inventory.normalise_payload(self.snapshot_payload(), uid="100000001")

        path = mys_inventory.save_snapshot(model, moment=datetime.datetime(2026, 9, 18, 14, 3, 12))
        name = os.path.basename(path)

        self.assertTrue(name.startswith("inventory_20260918_140312"))
        self.assertIn(name, mys_inventory.list_snapshots())
        loaded = mys_inventory.load_snapshot_file(name)
        self.assertEqual(loaded["items"]["item_100092"]["count"], 87)

    def test_load_snapshot_file_refuses_path_traversal(self):
        """只认 snapshots 目录里的文件名，不接受任意路径（本机服务也不留这个口子）。"""
        self.assertIsNone(mys_inventory.load_snapshot_file("../../.env"))
        self.assertIsNone(mys_inventory.load_snapshot_file("other.json"))
        self.assertIsNone(mys_inventory.load_snapshot_file(""))

    def test_old_snapshots_are_pruned(self):
        model = mys_inventory.normalise_payload(self.snapshot_payload())
        with patch.object(mys_inventory, "MAX_SNAPSHOTS", 3):
            for index in range(6):
                mys_inventory.save_snapshot(
                    model, moment=datetime.datetime(2026, 9, 18, 10, 0, index)
                )

        self.assertEqual(len(mys_inventory.list_snapshots()), 3)


class AccountMismatchTests(TempInventoryCase):
    """防串号：cookie 名下有多个号时，读错号的背包会让整套规划建立在不存在的材料上。"""

    def test_uid_mismatch_raises(self):
        model = mys_inventory.normalise_payload(self.snapshot_payload(), uid="111111111")

        with self.assertRaises(mys_inventory.InventoryAccountMismatch):
            mys_inventory.check_account(model, expected="100000001")

    def test_matching_uid_passes(self):
        model = mys_inventory.normalise_payload(self.snapshot_payload(), uid="100000001")

        self.assertTrue(mys_inventory.check_account(model, expected="100000001"))

    def test_expected_uid_falls_back_to_default_uid(self):
        with patch.object(config, "MYS_UID", ""), patch.object(config, "DEFAULT_UID", "100000001"):
            self.assertEqual(mys_inventory.expected_uid(), "100000001")


class SyncFailureTests(TempInventoryCase):
    """同步失败：保留旧快照 + 明确标记不是实时数据（§35.11 / §35.23）。"""

    def _good_sync(self):
        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator_stub(), "fetch_my_items", return_value=self.snapshot_payload()):
            return mys_inventory.sync(path=self.latest, db_path=self.db)

    def test_successful_sync_writes_file_db_and_history(self):
        from brain import growth_db

        result = self._good_sync()

        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(os.path.isfile(self.latest))
        self.assertGreater(result["snapshot_id"], 0)
        self.assertEqual(len(growth_db.snapshot_items(result["snapshot_id"], path=self.db)), 2)
        summary = growth_db.sync_summary(path=self.db)
        self.assertEqual((summary["success"], summary["failed"]), (1, 0))

    def test_not_configured_returns_a_clear_message_without_requests(self):
        with patch.object(mys_api, "cookie_configured", return_value=False):
            result = mys_inventory.sync(path=self.latest, db_path=self.db)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "not_configured")
        self.assertIn("MYS_COOKIE", result["error"])

    def test_auth_failure_on_a_complete_cookie_means_the_backpack_api_is_gone(self):
        """**这条是重点**：cookie 完整时这个接口还说"未登录"，那就是接口被米游社关了 ——
        不能再报"cookie 失效"，否则玩家会一遍遍去重新扫码（实测踩过）。"""
        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_api, "audit_cookie",
                          return_value={"ok": True, "missing": [], "hint": ""}), \
             patch.object(mys_calculator_stub(), "fetch_my_items",
                          side_effect=mys_api.MysAuthError("未登录或 cookie 已失效")):
            result = mys_inventory.sync(path=self.latest, db_path=self.db)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "backpack_unavailable")
        self.assertIn("背包", mys_inventory.error_advice("backpack_unavailable"))
        self.assertNotIn("重新取一份", result["error"])

    def test_a_skipped_sync_leaves_no_history_row_behind(self):
        """跳过的同步**不能留下记录**。

        ⚠️ `sync()` 一开始会先插一条 `success=0` 的记录，做完才更新。
        如果"跳过"这条路不把记录删掉，它就会永远停在"失败 + 空错误" ——
        界面上显示成一串莫名其妙的"失败"（实测踩过：6 条孤儿记录，
        谁也看不出是哪来的）。所以这里断言：跳过之后历史里**一条都不多**。
        """
        from brain import growth_db

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_api, "audit_cookie",
                          return_value={"ok": True, "missing": [], "hint": ""}), \
             patch.object(mys_calculator_stub(), "fetch_my_items",
                          side_effect=mys_api.MysAuthError("未登录或 cookie 已失效")):
            result = mys_inventory.sync(path=self.latest, db_path=self.db)

        self.assertTrue(result["skipped"])
        rows = growth_db.list_sync_history(path=self.db) if hasattr(
            growth_db, "list_sync_history") else []
        self.assertEqual(rows, [])
        self.assertEqual(growth_db.sync_summary(path=self.db)["total"], 0)

    def test_expired_cookie_is_still_classified_as_auth(self):
        """cookie 本身就缺 ltoken 时，仍然老老实实报"cookie 有问题"。"""
        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_api, "audit_cookie",
                          return_value={"ok": False, "missing": [], "hint": "缺 ltoken"}), \
             patch.object(mys_calculator_stub(), "fetch_my_items",
                          side_effect=mys_api.MysAuthError("cookie 已过期")):
            result = mys_inventory.sync(path=self.latest, db_path=self.db)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "auth")
        self.assertIn("重新", mys_inventory.error_advice("auth"))

    def test_risk_control_is_classified(self):
        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator_stub(), "fetch_my_items",
                          side_effect=mys_api.MysRiskControl("要验证码")):
            result = mys_inventory.sync(path=self.latest, db_path=self.db)

        self.assertEqual(result["error_kind"], "risk_control")
        self.assertIn("验证码", mys_inventory.error_advice("risk_control"))

    def test_failed_sync_keeps_the_previous_snapshot_but_marks_it_stale(self):
        first = self._good_sync()
        self.assertTrue(first["ok"])
        before = mys_inventory.load_inventory(path=self.latest)

        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator_stub(), "fetch_my_items",
                          side_effect=mys_api.MysTransportError("网络失败")):
            result = mys_inventory.sync(path=self.latest, db_path=self.db)

        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["inventory"]["items"], before["items"])   # 旧数据还在
        self.assertTrue(result["last_success_at"])

    def test_empty_inventory_is_an_error_not_a_silent_zero(self):
        """接口改版导致解析不出材料时，绝不能把库存写成 0 —— 那会让所有材料都"缺"。"""
        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator_stub(), "fetch_my_items", return_value={"item_list": []}):
            result = mys_inventory.sync(path=self.latest, db_path=self.db)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "inventory")
        self.assertFalse(os.path.exists(self.latest))

    def test_unexpected_exception_is_caught(self):
        with patch.object(mys_api, "cookie_configured", return_value=True), \
             patch.object(mys_calculator_stub(), "fetch_my_items",
                          side_effect=RuntimeError("谁知道呢")):
            result = mys_inventory.sync(path=self.latest, db_path=self.db)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "unexpected")


class StatusTests(TempInventoryCase):
    """`status()`：**不发任何网络请求**，并且必须标出"这不是实时数据"。"""

    def test_status_without_a_snapshot(self):
        status = mys_inventory.status(path=self.latest, db_path=self.db)

        self.assertFalse(status["available"])
        self.assertTrue(status["stale"])
        self.assertEqual(status["item_count"], 0)
        self.assertIn("还没有同步过", status["hint"])

    def test_fresh_snapshot_is_not_stale(self):
        model = mys_inventory.normalise_payload(self.snapshot_payload(), uid="1")
        model["fetched_at"] = datetime.datetime.now().strftime(mys_inventory._TIME_FORMAT)
        mys_inventory.save_inventory(model, path=self.latest)

        status = mys_inventory.status(path=self.latest, db_path=self.db)

        self.assertTrue(status["available"])
        self.assertFalse(status["stale"])
        self.assertEqual(status["item_count"], 2)
        self.assertEqual(status["hint"], "")

    def test_old_snapshot_is_stale_with_an_explanation(self):
        model = mys_inventory.normalise_payload(self.snapshot_payload(), uid="1")
        model["fetched_at"] = (datetime.datetime.now() - datetime.timedelta(hours=30)).strftime(
            mys_inventory._TIME_FORMAT
        )
        mys_inventory.save_inventory(model, path=self.latest)

        status = mys_inventory.status(path=self.latest, db_path=self.db)

        self.assertTrue(status["stale"])
        self.assertIn("不是实时数据", status["hint"])
        self.assertEqual(status["age_text"], "30.0 小时前")

    def test_broken_timestamp_counts_as_stale(self):
        model = mys_inventory.normalise_payload(self.snapshot_payload(), uid="1")
        model["fetched_at"] = "不知道什么时候"
        mys_inventory.save_inventory(model, path=self.latest)

        self.assertTrue(mys_inventory.status(path=self.latest, db_path=self.db)["stale"])

    def test_describe_age(self):
        self.assertEqual(mys_inventory.describe_age(""), "从未同步")
        self.assertEqual(mys_inventory.describe_age("坏值"), "从未同步")
        now = datetime.datetime.now()
        self.assertIn("分钟前", mys_inventory.describe_age(
            (now - datetime.timedelta(minutes=5)).strftime(mys_inventory._TIME_FORMAT)))


class LookupTests(TempInventoryCase):
    def _save(self, counts):
        model = mys_inventory.normalise_payload(self.snapshot_payload(counts), uid="1")
        mys_inventory.save_inventory(model, path=self.latest)
        return model

    def test_counts_and_owned(self):
        model = self._save({104001: 500, 100092: 20})

        self.assertEqual(mys_inventory.counts(model)[104001], 500)
        self.assertEqual(mys_inventory.owned(100092, model=model), 20)
        self.assertEqual(mys_inventory.owned(999999, model=model), 0)

    def test_find_by_name(self):
        model = self._save({100092: 20})

        self.assertEqual(mys_inventory.find_by_name("清心", model=model)["count"], 20)
        self.assertIsNone(mys_inventory.find_by_name("不存在", model=model))
        self.assertIsNone(mys_inventory.find_by_name("", model=model))

    def test_material_lines(self):
        model = self._save({100092: 20, 104001: 500})

        lines = mys_inventory.material_lines([100092, 104001, 999999], model=model)

        self.assertEqual(lines, ["清心 × 20", "摩拉 × 500"])


class DeltaTests(TempInventoryCase):
    """两次快照之间的增减（"这次刷了多少"要说得出数字）。"""

    def test_inventory_delta_between_two_snapshots(self):
        from brain import growth_db

        first = mys_inventory.normalise_payload(self.snapshot_payload({100092: 20}), uid="1")
        first_id = growth_db.record_snapshot("1", "cn_gf01", first["fetched_at"], first["items"],
                                            path=self.db)
        second = mys_inventory.normalise_payload(self.snapshot_payload({100092: 87}), uid="1")
        second["fetched_at"] = growth_db.now_text()
        second_id = growth_db.record_snapshot("1", "cn_gf01", second["fetched_at"], second["items"],
                                             path=self.db)

        delta = growth_db.inventory_delta(second_id, path=self.db)

        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0]["name"], "清心")
        self.assertEqual((delta[0]["before"], delta[0]["after"], delta[0]["delta"]), (20, 87, 67))
        self.assertEqual(growth_db.previous_snapshot(second_id, path=self.db)["id"], first_id)


def mys_calculator_stub():
    """拿到 `skills.mys_calculator` 模块本体（打桩它的 fetch_my_items）。"""
    from skills import mys_calculator

    return mys_calculator


class ErrorAdviceTests(unittest.TestCase):
    def test_every_kind_has_actionable_advice(self):
        for kind in ("not_configured", "auth", "risk_control", "transport",
                     "account_mismatch", "api", "inventory", ""):
            self.assertTrue(mys_inventory.error_advice(kind))


if __name__ == "__main__":
    unittest.main()
