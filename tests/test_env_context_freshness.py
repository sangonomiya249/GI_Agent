import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from brain import memory_manager
from skills import env_reader


class EnvContextFreshnessTests(unittest.TestCase):
    """回归测试：展柜快照被永久当"最新"用，导致大模型回「XX 不在展柜 JSON 中」。"""

    def setUp(self):
        self.store = {}
        self.fake_payload = {"avatars": [{"name": "蓝砚", "level": 60}]}

    def _patch_fetch(self, value):
        return patch.object(env_reader, "fetch_enka_data", return_value=value)

    def test_refresh_writes_context_with_timestamp(self):
        with self._patch_fetch(self.fake_payload):
            ok, notice = env_reader.refresh_store_env_context(self.store, "100000000", force=True)

        self.assertTrue(ok)
        self.assertIn("蓝砚", self.store["env_context"])
        self.assertIn("抓取于", self.store["env_context"])
        self.assertIsNotNone(env_reader.parse_env_context_time(self.store["env_context_at"]))
        self.assertIn("1 名角色", notice)

    def test_failed_refresh_keeps_previous_cache(self):
        with self._patch_fetch(self.fake_payload):
            env_reader.refresh_store_env_context(self.store, "uid", force=True)
        cached_text = self.store["env_context"]
        cached_at = self.store["env_context_at"]

        with self._patch_fetch({"error": "网络连通失败"}):
            ok, notice = env_reader.refresh_store_env_context(self.store, "uid", force=True)

        self.assertFalse(ok)
        # 以前会把好数据覆盖成「展柜数据暂不可用」，一次网络抖动就丢掉整份展柜
        self.assertEqual(self.store["env_context"], cached_text)
        self.assertEqual(self.store["env_context_at"], cached_at)
        self.assertIn("继续使用缓存数据", notice)

    def test_failed_first_fetch_marks_context_unavailable(self):
        with self._patch_fetch({"error": "网络连通失败"}):
            ok, _notice = env_reader.refresh_store_env_context(self.store, "uid", force=True)

        self.assertFalse(ok)
        self.assertIn("暂不可用", self.store["env_context"])
        self.assertEqual(self.store["env_context_at"], "")

    def test_ttl_skips_fresh_cache(self):
        with self._patch_fetch(self.fake_payload):
            env_reader.refresh_store_env_context(self.store, "uid", force=True)

        with self._patch_fetch(self.fake_payload) as fetch:
            ok, notice = env_reader.refresh_store_env_context(self.store, "uid", ttl_minutes=10)
            fetch.assert_not_called()

        self.assertTrue(ok)
        self.assertIn("跳过刷新", notice)

    def test_expired_cache_is_refetched(self):
        with self._patch_fetch(self.fake_payload):
            env_reader.refresh_store_env_context(self.store, "uid", force=True)
        self.store["env_context_at"] = (
            datetime.datetime.now() - datetime.timedelta(minutes=30)
        ).strftime("%Y-%m-%d %H:%M:%S")

        with self._patch_fetch(self.fake_payload) as fetch:
            env_reader.refresh_store_env_context(self.store, "uid", ttl_minutes=10)
            fetch.assert_called_once()

    def test_age_note_is_human_readable(self):
        now = datetime.datetime(2026, 9, 10, 15, 0, 0)

        self.assertEqual(env_reader.describe_env_context_age("", now), "抓取时间未知")
        self.assertEqual(
            env_reader.describe_env_context_age("2026-09-10 14:59:30", now), "刚刚抓取"
        )
        self.assertEqual(
            env_reader.describe_env_context_age("2026-09-10 14:30:00", now), "30 分钟前抓取"
        )
        self.assertEqual(
            env_reader.describe_env_context_age("2026-09-10 12:00:00", now), "3.0 小时前抓取"
        )
        self.assertEqual(
            env_reader.describe_env_context_age("2026-09-08 15:00:00", now), "2.0 天前抓取"
        )

    def test_store_round_trip_preserves_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "chat_context.json"
            with patch.object(config, "HISTORY_FILE", str(history)):
                memory_manager.save_chat_store(
                    {
                        "uid": "1",
                        "env_context": "展柜数据",
                        "env_context_at": "2026-09-10 14:00:00",
                        "messages": [],
                        "wallet": {"mora": 0, "exp_books": 0, "boss_mats": {}},
                        "pending_task": None,
                    }
                )
                loaded = memory_manager.load_chat_store()

        # 时间戳必须活过 load/save，否则新鲜度判断又会退化成「未知」
        self.assertEqual(loaded["env_context_at"], "2026-09-10 14:00:00")
        self.assertEqual(loaded["env_context"], "展柜数据")


if __name__ == "__main__":
    unittest.main()
