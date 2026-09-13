"""体积与可选依赖：飞书 SDK 不该是硬依赖，磁盘体检工具要能跑。"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class OptionalDependencyTests(unittest.TestCase):
    def test_feishu_sdk_is_not_imported_at_module_level(self):
        """`api/feishu_api.py` 是所有回话的必经之路；顶层 import 37 MB 的飞书 SDK

        等于让"只用 QQ 的玩家"也必须装着它。这里钉住：只能懒加载。
        """
        source = (PROJECT_ROOT / "api" / "feishu_api.py").read_text(encoding="utf-8")
        top_level = [
            line for line in source.splitlines()
            if re.match(r"^(import|from)\s+lark_oapi", line)
        ]

        self.assertEqual(top_level, [], f"飞书 SDK 又被写成顶层 import 了：{top_level}")
        self.assertIn("def _load_lark", source)

    def test_feishu_entry_explains_a_missing_sdk(self):
        """`feishu_main.py` 没装 SDK 时要给人话提示，而不是丢 ImportError 堆栈。"""
        source = (PROJECT_ROOT / "feishu_main.py").read_text(encoding="utf-8")

        self.assertIn("pip install lark_oapi", source)
        self.assertIn("可选依赖", source)

    def test_core_modules_load_without_the_feishu_sdk(self):
        """把 lark_oapi 屏蔽掉，核心链路照样能 import（在子进程里验，避免污染本进程）。"""
        script = (
            "import builtins\n"
            "real = builtins.__import__\n"
            "def fake(name, *a, **k):\n"
            "    if name == 'lark_oapi' or name.startswith('lark_oapi.'):\n"
            "        raise ImportError('No module named lark_oapi')\n"
            "    return real(name, *a, **k)\n"
            "builtins.__import__ = fake\n"
            "import api.feishu_api, channels.agent_router, skills.bgi_controller, brain.llm_brain\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertIn("OK", result.stdout or "", result.stderr)


class DiskReportTests(unittest.TestCase):
    def test_report_runs_and_names_the_optional_packages(self):
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "disk_report.py")],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout
        self.assertIn("venv", output)
        self.assertIn("playwright", output)
        self.assertIn("lark_oapi", output)
        self.assertIn("WebView2", output)

    def test_report_only_reads(self):
        """体检工具不许删东西：源码里不能出现删除调用。"""
        source = (PROJECT_ROOT / "scripts" / "disk_report.py").read_text(encoding="utf-8")

        for forbidden in ("os.remove", "shutil.rmtree", "os.unlink"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
