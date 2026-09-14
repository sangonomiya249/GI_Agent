"""版本检查（`skills/update_check.py`）的测试：GitHub release 对不上网络也不能崩。

口径（和模块注释一致）：
* 离线 / 限流 / 仓库没发布过 release → 只影响那一行提示，`check()` 永不抛异常；
* 结果缓存 `UPDATE_CHECK_HOURS` 小时，`force=True` 才重查；
* 只读：不下载、不改文件。
"""

import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from skills import update_check


def release(tag="v1.5.0", name="", body="", url="", published="2026-09-15T10:00:00Z"):
    return {
        "tag": tag,
        "name": name or tag,
        "url": url or f"https://github.com/sangonomiya249/GI_Agent/releases/tag/{tag}",
        "notes": body,
        "published_at": published,
        "prerelease": False,
    }


class VersionParsingTests(unittest.TestCase):
    def test_common_shapes(self):
        cases = (
            ("v1.0.0", (1, 0, 0)),
            ("1.0.0", (1, 0, 0)),
            ("1.2", (1, 2)),
            ("v2.0.1-rc.1", (2, 0, 1)),
            ("v1.0.0-3-gabc1234", (1, 0, 0)),        # git describe：后面那截不是版本
            ("", ()),
            ("不知道什么鬼", ()),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(update_check.parse_version(text)[0], expected)

    def test_newer_same_older(self):
        self.assertEqual(update_check.compare_versions("v1.5.0", "1.0.0")[0], "newer")
        self.assertEqual(update_check.compare_versions("v1.0.0", "1.0.0")[0], "same")
        self.assertEqual(update_check.compare_versions("v1.0.0", "1.2.0")[0], "older")
        self.assertEqual(update_check.compare_versions("v1.0.0", "本地版")[0], "unknown")

    def test_local_commits_after_the_tag_are_not_an_update(self):
        """本地 `git describe` 出来的 `v1.0.0-3-gabc1234` = 已经在 tag 之后提交了 3 次。"""
        status, detail = update_check.compare_versions("v1.0.0", "v1.0.0-3-gabc1234")

        self.assertEqual(status, "same")
        self.assertIn("提交", detail)


class LocalVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "PROJECT_ROOT", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_version_file_wins(self):
        with open(os.path.join(self.tmp.name, "VERSION"), "w", encoding="utf-8") as handle:
            handle.write("2.3.4\n\n")

        self.assertEqual(update_check.local_version(), ("2.3.4", "VERSION"))

    def test_falls_back_to_git_describe(self):
        with patch.object(update_check, "_git_describe", return_value="v9.9.9-2-gdeadbee"):
            self.assertEqual(
                update_check.local_version(), ("v9.9.9-2-gdeadbee", "git describe")
            )

    def test_unknown_when_nothing_is_available(self):
        with patch.object(update_check, "_git_describe", return_value=""):
            self.assertEqual(update_check.local_version(), ("", "未知"))

    def test_update_hint_mentions_git_for_a_checkout(self):
        os.makedirs(os.path.join(self.tmp.name, ".git"), exist_ok=True)

        self.assertIn("git pull", update_check.update_hint())

    def test_update_hint_talks_about_zip_for_a_plain_folder(self):
        self.assertIn("zip", update_check.update_hint())


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime.datetime(2026, 9, 15, 20, 0, 0)
        patches = [
            patch.object(config, "PROJECT_ROOT", self.tmp.name),
            patch.object(config, "UPDATE_CHECK", True),
            patch.object(config, "UPDATE_CHECK_HOURS", 6),
            patch.object(config, "UPDATE_REPO", "sangonomiya249/GI_Agent"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with open(os.path.join(self.tmp.name, "VERSION"), "w", encoding="utf-8") as handle:
            handle.write("1.0.0\n")

    def test_update_available(self):
        calls = []

        def fetch(slug):
            calls.append(slug)
            return release(tag="v1.5.0", name="修了一堆坑", body="· 修了 A\n· 修了 B"), ""

        result = update_check.check(now=self.now, fetch=fetch)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "newer")
        self.assertTrue(result["update_available"])
        self.assertEqual(result["local_version"], "1.0.0")
        self.assertEqual(result["latest_version"], "v1.5.0")
        self.assertIn("有新版本", result["headline"])
        self.assertIn("v1.5.0", result["headline"])
        self.assertEqual(calls, ["sangonomiya249/GI_Agent"])
        self.assertFalse(result["from_cache"])

    def test_already_up_to_date(self):
        result = update_check.check(
            now=self.now, fetch=lambda slug: (release(tag="v1.0.0"), "")
        )

        self.assertEqual(result["status"], "same")
        self.assertFalse(result["update_available"])
        self.assertIn("已是最新", result["headline"])

    def test_repo_without_any_release_is_not_a_crash(self):
        result = update_check.check(
            now=self.now, fetch=lambda slug: (None, "这个仓库还没有发布过 release")
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertIn("还没有发布过 release", result["error"])
        self.assertIn("检查更新失败", result["headline"])

    def test_offline_is_reported_but_harmless(self):
        def boom(_slug):
            raise ConnectionError("网络断了")

        result = update_check.check(now=self.now, fetch=boom)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertIn("ConnectionError", result["error"])
        self.assertIn("检查更新失败", result["headline"])

    def test_result_is_cached_and_force_refetches(self):
        calls = []

        def fetch(slug):
            calls.append(slug)
            return release(tag="v1.5.0"), ""

        first = update_check.check(now=self.now, fetch=fetch)
        cached = update_check.check(
            now=self.now + datetime.timedelta(hours=1),
            fetch=lambda slug: (_ for _ in ()).throw(AssertionError("不该重查")),
        )
        forced = update_check.check(now=self.now, force=True, fetch=fetch)

        self.assertFalse(first["from_cache"])
        self.assertEqual(len(calls), 2)                  # 第一次 + force
        self.assertTrue(cached["from_cache"])
        self.assertEqual(cached["latest_version"], "v1.5.0")
        self.assertFalse(forced["from_cache"])

    def test_cache_expires_after_the_ttl(self):
        update_check.check(now=self.now, fetch=lambda slug: (release(tag="v1.5.0"), ""))
        calls = []

        after_ttl = update_check.check(
            now=self.now + datetime.timedelta(hours=7),
            fetch=lambda slug: (calls.append(slug) or release(tag="v1.6.1"), ""),
        )

        self.assertEqual(calls, ["sangonomiya249/GI_Agent"])
        self.assertEqual(after_ttl["latest_version"], "v1.6.1")

    def test_other_repo_does_not_reuse_the_cache(self):
        update_check.check(now=self.now, fetch=lambda slug: (release(tag="v1.5.0"), ""))
        calls = []

        with patch.object(config, "UPDATE_REPO", "someone/else"):
            update_check.check(
                now=self.now, fetch=lambda slug: (calls.append(slug) or release(tag="v1.5.0"), "")
            )

        self.assertEqual(calls, ["someone/else"])

    def test_disabled_switch_never_calls_github(self):
        with patch.object(config, "UPDATE_CHECK", False):
            result = update_check.check(
                now=self.now,
                fetch=lambda slug: (_ for _ in ()).throw(AssertionError("关掉后不该联网")),
            )

        self.assertEqual(result["status"], "disabled")
        self.assertIn("已关闭", result["headline"])

    def test_broken_cache_file_is_ignored(self):
        path = os.path.join(self.tmp.name, "memory", "update_check.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{不是 json")

        self.assertEqual(update_check.load_cache(), {})
        result = update_check.check(now=self.now, fetch=lambda slug: (release(), ""))

        self.assertEqual(result["status"], "newer")       # 坏缓存被忽略 → 真去查了一次
        self.assertEqual(result["latest_version"], "v1.5.0")


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "PROJECT_ROOT", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        with open(os.path.join(self.tmp.name, "VERSION"), "w", encoding="utf-8") as handle:
            handle.write("1.0.0\n")

    def test_newer_lines_show_tag_url_and_how_to_update(self):
        result = update_check.check(
            now=datetime.datetime(2026, 9, 15, 20, 0, 0),
            fetch=lambda slug: (release(tag="v1.5.0", body="· 修了 A", url="https://x/y"), ""),
        )

        text = "\n".join(update_check.format_lines(result))

        self.assertIn("有新版本", text)
        self.assertIn("v1.5.0", text)
        self.assertIn("https://x/y", text)
        self.assertIn("更新方式", text)
        self.assertIn("修了 A", text)

    def test_error_lines_say_the_agent_still_works(self):
        result = update_check.check(now=datetime.datetime.now(), fetch=lambda slug: (None, "断网了"))

        text = "\n".join(update_check.format_lines(result))

        self.assertIn("断网了", text)
        self.assertIn("照常用", text)

    def test_cli_json_output(self):
        buffer = io.StringIO()
        with patch.object(update_check, "fetch_latest", return_value=(release(tag="v1.5.0"), "")):
            with contextlib.redirect_stdout(buffer):
                code = update_check.main(["--force", "--json"])

        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "newer")
        self.assertTrue(payload["update_available"])

    def test_cli_returns_1_when_the_check_fails(self):
        buffer = io.StringIO()
        with patch.object(update_check, "fetch_latest", return_value=(None, "断网了")):
            with contextlib.redirect_stdout(buffer):
                code = update_check.main([])

        self.assertEqual(code, 1)
        self.assertIn("断网了", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
