"""WebView2 SDK 下载/解包脚本的测试（用构造出来的假 nupkg，不联网）。"""

import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import fetch_webview2


def fake_package(missing=()):
    """造一个结构与官方 NuGet 包一致的 zip（只放我们要的两个目录）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for source in fetch_webview2.WANTED:
            if source in missing:
                continue
            archive.writestr(source, b"fake-" + os.path.basename(source).encode())
        archive.writestr("build/Common.targets", b"<Project />")
    return buffer.getvalue()


class MissingFilesTests(unittest.TestCase):
    def test_reports_every_missing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = fetch_webview2.missing_files(temp_dir)

        self.assertEqual(sorted(missing), sorted(fetch_webview2.WANTED.values()))

    def test_ready_when_all_files_exist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for name in fetch_webview2.WANTED.values():
                Path(temp_dir, name).write_bytes(b"x")

            self.assertEqual(fetch_webview2.missing_files(temp_dir), [])


class ExtractTests(unittest.TestCase):
    def test_extracts_the_three_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            written = fetch_webview2.extract(fake_package(), temp_dir)

            names = sorted(name for name, _size in written)
            self.assertEqual(names, sorted(fetch_webview2.WANTED.values()))
            for name in fetch_webview2.WANTED.values():
                self.assertTrue(os.path.isfile(os.path.join(temp_dir, name)), name)

    def test_missing_entry_raises_with_the_path(self):
        payload = fake_package(missing=("lib/net462/Microsoft.Web.WebView2.Core.dll",))

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(RuntimeError) as context:
                fetch_webview2.extract(payload, temp_dir)

        self.assertIn("Microsoft.Web.WebView2.Core.dll", str(context.exception))

    def test_main_check_mode_reports_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            code = fetch_webview2.main(["--check", "--dir", temp_dir])

        self.assertEqual(code, 1)

    def test_main_uses_the_downloader_and_reports_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(fetch_webview2, "download_package", return_value=fake_package()):
                code = fetch_webview2.main(["--dir", temp_dir])

            self.assertEqual(code, 0)
            self.assertEqual(fetch_webview2.missing_files(temp_dir), [])

    def test_download_failure_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(fetch_webview2, "download_package", side_effect=OSError("没网")):
                code = fetch_webview2.main(["--dir", temp_dir])

        self.assertEqual(code, 1)

    def test_package_url_points_at_official_nuget(self):
        self.assertTrue(fetch_webview2.PACKAGE_URL.startswith("https://api.nuget.org/"))
        self.assertIn(fetch_webview2.VERSION, fetch_webview2.PACKAGE_URL)


if __name__ == "__main__":
    unittest.main()
