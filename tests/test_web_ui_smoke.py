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

    def test_app_js_renders_the_update_page(self):
        """「系统 → 版本更新」页：四张卡 + 正文 + 更新说明 + 侧边栏红点 + 两个按钮。"""
        result = self._run(str(HARNESS))

        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        for line in (
            "✅ 版本页：四张卡写明本地版本 / 最新 release / 状态 / 检查方式",
            "✅ 版本页：正文写清结论与更新方式",
            "✅ 版本页：更新说明贴在下面那张卡",
            "✅ 版本页：有新版本时侧边栏亮红点",
            "✅ 版本页：「打开发布页」被启用且会走 /api/open",
            "✅ 版本页：点「检查更新」会忽略缓存重查（force=1）",
            "✅ 版本页：文案里不放 emoji（界面用自己的图标）",
        ):
            self.assertIn(line, result.stdout)

    def test_update_page_elements_exist(self):
        html = (PROJECT_ROOT / "studio" / "web" / "index.html").read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")

        # 版本更新是「系统」栏里的独立页面，不再是概览页里那张卡
        self.assertIn('data-page="update"', html)
        self.assertIn(">版本更新", html)
        for element_id in ("update-cards", "update-body", "update-notes",
                           "btn-update-check", "btn-update-open", "nav-badge-update"):
            self.assertIn(f'id="{element_id}"', html, element_id)
        self.assertNotIn('id="dash-update"', html)
        self.assertIn("/api/update", js)
        self.assertIn("loadUpdate", js)
        self.assertIn('if (page === "update") loadUpdate();', js)      # 切到这一页才去查（走缓存）

    def test_app_js_renders_the_cooldown_page(self):
        """「资源冷却」页：切过去要能渲染出**类别切换 + 该类别自己的表**，点了还要能换。"""
        result = self._run(str(HARNESS))

        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        for line in (
            "✅ 冷却页：顶部有类别切换（特产/矿物/魔物）",
            "✅ 冷却页：默认选中地区特产",
            "✅ 冷却页：类别 chip 带冷却中角标",
            "✅ 冷却页：汇总卡只统计当前类别",
            "✅ 冷却页：表格只显示当前类别",
            "✅ 冷却页：冷却中的目标带剩余时间",
            "✅ 冷却页：部分完成标注出来了",
            "✅ 冷却页：每行都有登记/清除按钮",
            "✅ 冷却页：页脚写明清单来源（全量目录，不是上次跑的那些）",
            "✅ 点「敌人与魔物」→ 表格换成魔物的",
            "✅ 点「敌人与魔物」→ 卡片/标题/chip 高亮都跟着换",
            "✅ 点「矿物」→ 显示矿物（页脚也跟着换）",
            "✅ 点回「地区特产」→ 恢复特产的表",
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
        """配置页里的米游社面板（状态 + 验证 Cookie + 拉名单 + 扫码登录）要整块存在。"""
        from pathlib import Path

        html = (PROJECT_ROOT / "studio" / "web" / "index.html").read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")

        for element_id in ("mys-status", "mys-result", "btn-mys-check", "btn-mys-refresh"):
            self.assertIn(f'id="{element_id}"', html, element_id)
        self.assertIn("/api/mys", js)
        self.assertIn("MYS_COOKIE", Path(PROJECT_ROOT / "skills" / "env_config.py").read_text(encoding="utf-8"))

    def test_mys_scan_login_panel_exists(self):
        """扫码登录那一套：按钮 / 面板 / 进度区 / 取消 / 手动粘贴 + 四个接口调用。

        为什么单独一条：这是**替玩家拿 ltoken** 的唯一入口（网页复制到的 cookie 常常只有 v2），
        少了任何一个 id，界面上就是"点了没反应"。
        """
        from pathlib import Path

        html = (PROJECT_ROOT / "studio" / "web" / "index.html").read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        css = (PROJECT_ROOT / "studio" / "web" / "app.css").read_text(encoding="utf-8")

        for element_id in ("btn-mys-scan", "btn-mys-scan-web", "mys-scan-panel", "mys-scan-hint",
                           "mys-scan-progress", "btn-mys-scan-cancel", "btn-mys-scan-manual"):
            self.assertIn(f'id="{element_id}"', html, element_id)
        for endpoint in ("/api/mys/login/status", "/api/mys/login/cancel", "/api/mys/login/manual"):
            self.assertIn(endpoint, js, endpoint)
        # ★ 扫码那两个接口按"app 版没有前缀 / 网页版加 /web"拼：
        #     app → `/api/mys/login/start`、`/api/mys/login/poll`
        #     web → `/api/mys/login/web/start`、`/api/mys/login/web/poll`
        #   踩过的坑：按 kind 直接拼出 `.../login/` + `app/start` —— 那个路径不存在，
        #   POST 落到 SPA 兜底路由，玩家点「扫码登录」只看到一坨 `405 Method Not Allowed` 的 HTML。
        self.assertIn('`/api/mys/login${mysScanKind === "web" ? "/web" : ""}/start`', js)
        self.assertIn('`/api/mys/login${mysScanKind === "web" ? "/web" : ""}/poll`', js)
        self.assertNotIn("/api/mys/login/app/", js)
        self.assertIn("startMysScan", js)
        self.assertIn("pollMysScan", js)
        self.assertIn(".scan-panel", css)
        # 面板默认是收起的（没点之前不该占地方）
        self.assertIn('class="scan-panel hidden"', html)

    def test_channel_elements_exist_in_html(self):
        html = (PROJECT_ROOT / "studio" / "web" / "index.html").read_text(encoding="utf-8")

        for element_id in ("chan-cards", "chan-hint", "dash-channels", "dash-chan-hint"):
            self.assertIn(f'id="{element_id}"', html, element_id)
        self.assertIn('data-page="channels"', html)

    def test_cooldown_page_elements_exist(self):
        """「资源冷却」页的骨架 + 接口调用都要在（少一个 id，点进去就是白屏/报错）。"""
        html = (PROJECT_ROOT / "studio" / "web" / "index.html").read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        css = (PROJECT_ROOT / "studio" / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn('data-page="cooldown"', html)
        for element_id in (
            "cooldown-tabs", "cooldown-title", "cooldown-cards", "cooldown-table",
            "cooldown-hint", "cooldown-foot",
            "cooldown-search", "cooldown-only-cooling", "btn-cooldown-reload",
        ):
            self.assertIn(f'id="{element_id}"', html, element_id)
        self.assertIn("/api/cooldown", js)
        self.assertIn("cooldown: [", js)          # PAGE_META 里有这一页，标题栏才对
        self.assertIn("loadCooldown", js)
        self.assertIn("data-cool-tab", js)        # 点类别切换（特产/矿物/食材/魔物）
        self.assertIn("路线仓库", js)              # 页脚写清清单来自全量目录
        self.assertIn(".cooldown-tabs", css)


if __name__ == "__main__":
    unittest.main()
