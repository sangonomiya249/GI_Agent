"""前端冒烟：用 node + 假 DOM 真跑一遍 `studio/web/app.js`。

为什么需要它：界面里 90% 的问题不是"接口错了"，而是"JS 渲染时炸了"（引用了不存在的元素、
函数名写错、新的渲染分支没接上）—— 这类错误在 Python 测试里完全看不见，而沙箱/CI 里
Edge 又起不来 headless（实测 `msedge --headless` 会被拒绝：请在交互式会话中打开）。

所以这里退一步：用 `tests/web_ui_smoke.js` 里的假 DOM 跑 app.js，重点验证「远程通道」这条
新链路真的能渲染出来（卡片、内嵌控制台、状态徽章、自动启动勾选框、概览摘要）。

没装 node 就跳过（本机有 node v22，测试因此是真跑过的）。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
APP_JS = PROJECT_ROOT / "studio" / "web" / "app.js"
HARNESS = TESTS_DIR / "web_ui_smoke.js"

NODE = shutil.which("node")


@unittest.skipUnless(NODE, "没装 node：跳过前端冒烟（界面逻辑改动请手动开一次 Studio 看）")
class WebUiSmokeTests(unittest.TestCase):
    def _run(self, *args, timeout=90):
        return subprocess.run(
            [NODE, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )

    def test_app_js_has_valid_syntax(self):
        result = self._run("--check", str(APP_JS))

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_app_js_boots_and_renders_channels(self):
        """跑 boot() → /api/state + /api/channels → 渲染通道卡片（有异常就直接失败）。"""
        result = self._run(str(HARNESS))

        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertIn("✅ 通道卡片渲染出来了", result.stdout)
        self.assertIn("✅ 内嵌控制台容器存在", result.stdout)
        self.assertNotIn("❌", result.stdout)

    def test_app_js_renders_the_cooldown_page(self):
        """「采集冷却」页：切过去要能真渲染出汇总卡与表格（含"还要等多久"和操作按钮）。"""
        result = self._run(str(HARNESS))

        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        for line in (
            "✅ 采集冷却：汇总卡渲染出来了",
            "✅ 采集冷却：冷却中的材料带剩余时间",
            "✅ 采集冷却：部分采集标注出来了",
            "✅ 采集冷却：没有记录的按可以采显示",
            "✅ 采集冷却：每行都有登记/清除按钮",
        ):
            self.assertIn(line, result.stdout)

    def test_every_element_the_app_looks_up_exists_or_is_created_by_js(self):
        """app.js 里 $('#id') 用到的 id 必须能在 index.html 找到，或者由 JS 自己创建。"""
        html = (PROJECT_ROOT / "studio" / "web" / "index.html").read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")

        import re

        html_ids = set(re.findall(r'id="([^"]+)"', html))
        created_by_js = set(re.findall(r'id="f-\$\{|id="f-', js))          # 配置表单字段
        dynamic = {"about-root", "f-LLM_PROVIDER", "f-MODEL_NAME", "f-OPENAI_BASE_URL"}
        # 配置表单里的字段控件是**按 FIELD_GROUPS 动态生成**的，都在 html 里找不到；
        # 这里按前缀放行 `f-*`（米游社那块会去读 f-MYS_COOKIE 方便"先验证再保存"）。
        used = set(re.findall(r'\$\("#([A-Za-z0-9_\-]+)"\)', js))

        missing = sorted(
            element_id for element_id in used - html_ids - dynamic - created_by_js
            if not element_id.startswith("f-")
        )
        self.assertEqual(missing, [], f"这些 id 在 index.html 里找不到：{missing}")

    def test_mys_panel_elements_exist(self):
        """配置页里的米游社面板（状态 + 验证 Cookie + 拉名单）要整块存在。"""
        from pathlib import Path

        html = (PROJECT_ROOT / "studio" / "web" / "index.html").read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")

        for element_id in ("mys-status", "mys-result", "btn-mys-check", "btn-mys-refresh"):
            self.assertIn(f'id="{element_id}"', html, element_id)
        self.assertIn("/api/mys", js)
        self.assertIn("MYS_COOKIE", Path(PROJECT_ROOT / "skills" / "env_config.py").read_text(encoding="utf-8"))

    def test_channel_elements_exist_in_html(self):
        html = (PROJECT_ROOT / "studio" / "web" / "index.html").read_text(encoding="utf-8")

        for element_id in ("chan-cards", "chan-hint", "dash-channels", "dash-chan-hint"):
            self.assertIn(f'id="{element_id}"', html, element_id)
        self.assertIn('data-page="channels"', html)

    def test_cooldown_page_elements_exist(self):
        """「采集冷却」页的骨架 + 接口调用都要在（少一个 id，点进去就是白屏/报错）。"""
        html = (PROJECT_ROOT / "studio" / "web" / "index.html").read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")

        self.assertIn('data-page="cooldown"', html)
        for element_id in (
            "cooldown-cards", "cooldown-table", "cooldown-hint",
            "cooldown-search", "cooldown-only-cooling", "btn-cooldown-reload",
        ):
            self.assertIn(f'id="{element_id}"', html, element_id)
        self.assertIn("/api/cooldown", js)
        self.assertIn("cooldown: [", js)          # PAGE_META 里有这一页，标题栏才对
        self.assertIn("loadCooldown", js)


if __name__ == "__main__":
    unittest.main()
