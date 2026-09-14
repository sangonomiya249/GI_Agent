"""Studio（网页版控制台）的测试：进程桥的日志缓冲、BetterGI 日志高亮、HTTP 接口。

都不启动真的 Agent：用 FakeRunner 顶替子进程，用临时目录顶替 BetterGI 安装目录。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from skills import gather_cooldown
from studio import agent_runner, channel_runner, logs, server


class FakeRunner:
    """顶替 AgentRunner：只记录调用，不拉子进程。"""

    def __init__(self):
        self.calls = []
        self._state = "stopped"
        self._lines = []
        self._seq = 0

    def start(self):
        self.calls.append("start")
        self._state = "running"
        return {"ok": True, "pid": 4321}

    def stop(self):
        self.calls.append("stop")
        self._state = "stopped"
        return {"ok": True}

    def send(self, text):
        text = str(text or "").strip()
        self.calls.append(("send", text))
        return {"ok": True} if text else {"ok": False, "error": "内容为空"}

    def snapshot(self, since=0):
        return {
            "ok": True,
            "state": self._state,
            "pid": 4321 if self._state == "running" else None,
            "seq": self._seq,
            "lines": [],
            "partial": "",
            "plan": {"lines": ["⚔️ 体力目标：无", "🗺️ 锄大地：璃月"]},
            "started_at": None,
        }

    def clear(self):
        self.calls.append("clear")
        return {"ok": True, "seq": 0}


class RunnerLogTests(unittest.TestCase):
    """日志缓冲：拆行、分级、半行提示、计划摘要。"""

    def setUp(self):
        self.runner = agent_runner.AgentRunner(project_root=".")

    def test_complete_lines_are_buffered_and_classified(self):
        self.runner._ingest("普通一行\n❌ 出错了\n⚠️ 小心\n🤖 Agent: 你好\n")

        snapshot = self.runner.snapshot(since=0)
        levels = [line["level"] for line in snapshot["lines"]]
        self.assertEqual(levels, ["info", "error", "warn", "agent"])
        self.assertEqual(snapshot["partial"], "")

    def test_incomplete_prompt_is_exposed_as_partial_and_sets_waiting(self):
        self.runner._ingest("🛑 [系统拦截] 请确认是否执行上述计划？")

        snapshot = self.runner.snapshot(since=0)
        self.assertEqual(snapshot["state"], "waiting")
        self.assertIn("请确认是否执行", snapshot["partial"])
        self.assertEqual(snapshot["lines"], [])

        # 后续输出把这一行补完，partial 清空、完整行进缓冲
        self.runner._ingest(" （y/t/exit）\n")
        snapshot = self.runner.snapshot(since=0)
        self.assertEqual(snapshot["partial"], "")
        self.assertEqual(len(snapshot["lines"]), 1)
        self.assertIn("y/t/exit", snapshot["lines"][0]["text"])

    def test_incremental_snapshot_only_returns_new_lines(self):
        self.runner._ingest("第一行\n第二行\n")
        first = self.runner.snapshot(since=0)
        self.runner._ingest("第三行\n")
        second = self.runner.snapshot(since=first["seq"])

        self.assertEqual([line["text"].strip() for line in second["lines"]], ["第三行"])

    def test_plan_is_extracted_from_the_approval_block(self):
        self.runner._ingest(
            "🧠 大脑正在思考...\n"
            "⚔️ 体力目标：无\n"
            "🌿 采集目标：清心\n"
            "👹 敌人讨伐：蕈兽\n"
            "🗺️ 锄大地：璃月\n"
            "🛑 [系统拦截] 请确认是否执行上述计划？\n"
        )

        plan = self.runner.last_plan()
        self.assertEqual(
            plan["lines"],
            ["⚔️ 体力目标：无", "🌿 采集目标：清心", "👹 敌人讨伐：蕈兽", "🗺️ 锄大地：璃月"],
        )

    def test_clear_empties_the_buffer(self):
        self.runner._ingest("一行\n")
        self.runner.clear()

        snapshot = self.runner.snapshot(since=0)
        self.assertEqual(snapshot["lines"], [])
        self.assertEqual(snapshot["seq"], 0)

    def test_send_without_process_reports_an_error(self):
        result = self.runner.send("y")

        self.assertFalse(result["ok"])
        self.assertIn("没在运行", result["error"])

    def test_child_command_uses_main_py(self):
        command = agent_runner.child_command("C:\\proj")

        self.assertIn("-u", command)
        self.assertTrue(command[-1].endswith(os.path.join("C:\\proj", "main.py")))

    def test_classify_covers_the_special_levels(self):
        self.assertEqual(agent_runner.classify("🔄 已改判"), "action")
        self.assertEqual(agent_runner.classify("👤 我：y"), "user")
        self.assertEqual(agent_runner.classify("=== Agent 已退出 ==="), "info")


class ChannelApiTests(unittest.TestCase):
    """/api/channels 与「启动 Agent 顺带唤醒通道」的接口行为。"""

    class FakeChannels:
        def __init__(self):
            self.calls = []
            self.runners = {"qq": object(), "feishu": object()}

        def view(self, name):
            return {
                "name": name,
                "label": "QQ 机器人" if name == "qq" else "飞书服务端",
                "state": "stopped",
                "pid": None,
                "configured": name == "qq",
                "missing": [] if name == "qq" else ["FEISHU_APP_ID"],
                "auto_start": name == "qq",
                "auto_field": f"AUTO_START_{name.upper()}_BOT",
                "hint": "去配置页填",
                "notes": "",
            }

        def snapshot(self, since=None):
            return {"ok": True, "channels": [{**self.view(name), "seq": 3, "lines": [{"i": 1, "text": "hello\n", "level": "info"}]} for name in self.runners]}

        def start(self, name, reason="手动启动"):
            self.calls.append(("start", name, reason))
            return {"ok": True, "pid": 999, "name": name}

        def stop(self, name):
            self.calls.append(("stop", name))
            return {"ok": True, "name": name}

        def clear(self, name):
            self.calls.append(("clear", name))
            return {"ok": True, "seq": 0}

        def start_automatic(self):
            self.calls.append("auto")
            return ["QQ 机器人：已随 Agent 启动（PID 999）", "飞书服务端：没配置（缺 FEISHU_APP_ID），跳过自动启动"]

        def stop_all(self):
            self.calls.append("stop_all")
            return [{"name": "qq", "ok": True}]

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.env_path = self.root / ".env"
        self.env_path.write_text("LLM_PROVIDER=openai\n", encoding="utf-8")
        self.runner = FakeRunner()
        self.channels = self.FakeChannels()
        self.app = server.create_app(runner=self.runner, env_path=str(self.env_path), channels=self.channels)
        self.app.config["SHUTDOWN_DELAY"] = None
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_state_includes_channel_summary(self):
        data = self.client.get("/api/state").get_json()

        names = [row["name"] for row in data["channels"]]
        self.assertEqual(names, ["qq", "feishu"])

    def test_channels_endpoint_returns_logs(self):
        data = self.client.get("/api/channels?since_qq=0&since_feishu=0").get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(len(data["channels"]), 2)
        self.assertEqual(data["channels"][0]["lines"][0]["text"], "hello\n")

    def test_channel_start_stop_clear(self):
        self.assertEqual(self.client.post("/api/channels/qq/start").get_json()["pid"], 999)
        self.client.post("/api/channels/qq/stop")
        self.client.post("/api/channels/qq/clear")

        self.assertIn(("start", "qq", "手动启动"), self.channels.calls)
        self.assertIn(("stop", "qq"), self.channels.calls)
        self.assertIn(("clear", "qq"), self.channels.calls)

    def test_starting_the_agent_wakes_configured_channels(self):
        data = self.client.post("/api/agent/start").get_json()

        self.assertIn("auto", self.channels.calls)
        self.assertEqual(len(data["channels"]), 2)
        self.assertIn("已随 Agent 启动", data["channels"][0])

    def test_stopping_the_agent_stops_channels_too(self):
        self.client.post("/api/agent/stop")

        self.assertIn("stop_all", self.channels.calls)

    def test_shutdown_stops_channels(self):
        self.client.post("/api/shutdown")

        self.assertIn("stop_all", self.channels.calls)


class NoChannelApiTests(unittest.TestCase):
    """一个通道都没配时（specs 为空），接口也要正常返回空列表。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temp_dir.name) / ".env"
        self.env_path.write_text("LLM_PROVIDER=openai\n", encoding="utf-8")
        self.app = server.create_app(
            runner=FakeRunner(),
            env_path=str(self.env_path),
            channels=channel_runner.ChannelManager(env_loader=lambda: {}, specs=()),
        )
        self.app.config["SHUTDOWN_DELAY"] = None
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_channels_endpoint_is_empty(self):
        self.assertEqual(self.client.get("/api/channels").get_json()["channels"], [])
        self.assertFalse(self.client.post("/api/channels/qq/start").get_json()["ok"])

    def test_agent_start_still_works(self):
        data = self.client.post("/api/agent/start").get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["channels"], [])


class BetterGiLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "log").mkdir()
        self.log_file = self.root / "log" / "bettergi.log"
        self.log_file.write_text(
            "\n".join(
                [
                    "2026-09-10 22:00:00.001 | INFO  | 开始一条龙",
                    "2026-09-10 22:01:10.500 | ERROR | 战斗策略文件不存在",
                    "2026-09-10 22:01:11.000 | ERROR | 目标传送点位于不可点击区域，传送失败",
                    "2026-09-10 22:01:12.000 | DEBUG | 脚本 \"蕈兽\" 状态为禁用，跳过执行",
                    "2026-09-10 22:01:13.000 | INFO  | 正常一行",
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_tail_returns_line_numbers_and_problem_levels(self):
        rows = logs.read_tail(str(self.log_file), lines=3)

        # 只取最后 3 行，但行号仍是原文件里的行号
        self.assertEqual([row["n"] for row in rows], [3, 4, 5])
        self.assertEqual(rows[0]["level"], "warn")
        self.assertIn("传送点不可点击", rows[0]["note"])
        self.assertEqual(rows[2]["level"], None)

    def test_keyword_filter_keeps_original_line_numbers(self):
        rows = logs.read_tail(str(self.log_file), lines=100, keyword="战斗策略")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["n"], 2)
        self.assertEqual(rows[0]["level"], "error")

    def test_summary_counts_known_problems(self):
        rows = logs.read_tail(str(self.log_file), lines=100)

        summary = logs.summarise(rows)
        self.assertEqual(summary.get("战斗策略缺失：组里的策略名在 User\\AutoFight 下找不到 txt"), 1)
        self.assertTrue(any("禁用" in key for key in summary))

    def test_first_problem_prefers_errors(self):
        rows = logs.read_tail(str(self.log_file), lines=100)

        problem = logs.find_first_problem(rows)
        self.assertEqual(problem["level"], "error")

    def test_list_log_files_sorted_by_mtime(self):
        older = self.root / "log" / "old.log"
        older.write_text("x", encoding="utf-8")
        os.utime(older, (1, 1))

        files = logs.list_log_files(str(self.root))

        self.assertEqual(files[0]["name"], "bettergi.log")
        self.assertEqual(files[-1]["name"], "old.log")

    def test_list_log_files_ignores_screenshots(self):
        """log 目录里混着识别失败的截图，不能出现在日志下拉框里。"""
        (self.root / "log" / "avatar_side_classify_error.png").write_bytes(b"\x89PNG")

        names = [row["name"] for row in logs.list_log_files(str(self.root))]

        self.assertEqual(names, ["bettergi.log"])

    def test_first_problem_skips_the_generic_error_bucket(self):
        rows = [
            {"n": 1, "text": "普通错误日志", "level": "warn", "note": "异常/错误"},
            {"n": 2, "text": "战斗策略文件不存在", "level": "error", "note": "战斗策略缺失：组里的策略名在 User\\AutoFight 下找不到 txt"},
        ]

        problem = logs.find_first_problem(rows)

        self.assertEqual(problem["n"], 2)

    def test_missing_file_returns_empty(self):
        self.assertEqual(logs.read_tail(str(self.root / "nope.log")), [])
        self.assertEqual(logs.list_log_files(str(self.root / "nope")), [])


class MysApiTests(unittest.TestCase):
    """配置页要能填米游社 cookie，并当场验证（不用手改 .env）。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.env_path = self.root / ".env"
        self.env_path.write_text("DEFAULT_UID=100000000\n", encoding="utf-8")
        self.runner = FakeRunner()
        self.app = server.create_app(runner=self.runner, env_path=str(self.env_path))
        self.app.config["SHUTDOWN_DELAY"] = None
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cookie_field_is_exposed_in_the_config_schema(self):
        data = self.client.get("/api/config").get_json()
        groups = {group["title"]: group for group in data["groups"]}

        self.assertIn("米游社个人战绩（可选）", groups)
        keys = {field["key"]: field for field in groups["米游社个人战绩（可选）"]["fields"]}
        self.assertEqual(keys["MYS_COOKIE"]["kind"], "secret")        # 密码框，不明文显示
        self.assertIn("MYS_CACHE_TTL_HOURS", keys)

    def test_cookie_is_written_to_env(self):
        data = self.client.post(
            "/api/config",
            json={"values": {"MYS_COOKIE": "ltuid=123456789; ltoken=secret-token; account_id=123456789"}},
        ).get_json()

        self.assertTrue(data["ok"])
        self.assertIn("MYS_COOKIE", self.env_path.read_text(encoding="utf-8"))

    def test_incomplete_cookie_gets_a_warning_but_still_saves(self):
        data = self.client.post(
            "/api/config", json={"values": {"MYS_COOKIE": "ltuid=123456789"}}
        ).get_json()

        self.assertTrue(data["ok"], data)
        self.assertTrue(any("ltoken" in warning for warning in data["warnings"]), data["warnings"])

    def test_status_endpoint_never_leaks_the_cookie(self):
        with patch.object(config, "MYS_COOKIE", "ltuid=123456789; ltoken=SECRETVALUE") as _patched:
            data = self.client.get("/api/mys").get_json()

        self.assertTrue(data["ok"])
        self.assertTrue(data["configured"])
        self.assertNotIn("SECRETVALUE", json.dumps(data, ensure_ascii=False))
        self.assertIn("…", data["masked"])

    def test_status_without_cookie_says_so(self):
        with patch.object(config, "MYS_COOKIE", ""):
            data = self.client.get("/api/mys").get_json()

        self.assertFalse(data["configured"])
        self.assertEqual(data["count"], 0)

    def test_check_uses_the_cookie_from_the_form_before_saving(self):
        """「验证 Cookie」要能直接验输入框里的内容（还没保存）。"""
        from skills import mys_api

        roles = [{"nickname": "旅行者", "uid": "100000000", "server": "cn_gf01", "level": 60}]
        with patch.object(mys_api, "fetch_roles", return_value=roles) as fetch:
            data = self.client.post(
                "/api/mys/check", json={"cookie": "ltuid=1; ltoken=x"}
            ).get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["roles"][0]["nickname"], "旅行者")
        self.assertEqual(fetch.call_args.kwargs["cookie"], "ltuid=1; ltoken=x")

    def test_check_reports_auth_failure_in_plain_words(self):
        from skills import mys_api

        with patch.object(mys_api, "fetch_roles", side_effect=mys_api.MysAuthError("未登录或 cookie 已失效")):
            data = self.client.post("/api/mys/check", json={"cookie": "ltuid=1"}).get_json()

        self.assertFalse(data["ok"])
        self.assertIn("cookie", data["error"])

    def test_check_without_any_cookie_is_rejected(self):
        with patch.object(config, "MYS_COOKIE", ""):
            data = self.client.post("/api/mys/check", json={}).get_json()

        self.assertFalse(data["ok"])
        self.assertIn("没有配置", data["error"])

    def test_refresh_pulls_the_snapshot(self):
        from skills import mys_api

        snapshot = {"nickname": "旅行者", "uid": "100000000", "avatars": [{"name": "胡桃"}]}
        with patch.object(config, "MYS_COOKIE", "ltuid=1; ltoken=x"), patch.object(
            mys_api, "refresh_snapshot", return_value=snapshot
        ):
            data = self.client.post("/api/mys/refresh").get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 1)

    def test_refresh_without_cookie_is_rejected(self):
        with patch.object(config, "MYS_COOKIE", ""):
            data = self.client.post("/api/mys/refresh").get_json()

        self.assertFalse(data["ok"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.env_path = self.root / ".env"
        self.env_path.write_text("LLM_PROVIDER=openai\nMODEL_NAME=deepseek-chat\n", encoding="utf-8")
        self.runner = FakeRunner()
        self.app = server.create_app(runner=self.runner, env_path=str(self.env_path))
        self.app.config["SHUTDOWN_DELAY"] = None
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_index_serves_the_ui(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("GI Agent Studio", response.get_data(as_text=True))

    def test_static_assets_are_served(self):
        for name in ("app.css", "app.js"):
            response = self.client.get(f"/{name}")
            self.assertEqual(response.status_code, 200, name)

    def test_state_contains_environment_summary(self):
        data = self.client.get("/api/state").get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["provider"], "openai")
        self.assertEqual(data["model"], "deepseek-chat")
        self.assertEqual(data["plan"]["lines"][0], "⚔️ 体力目标：无")

    def test_agent_endpoints_delegate_to_the_runner(self):
        self.assertEqual(self.client.post("/api/agent/start").get_json()["pid"], 4321)
        self.client.post("/api/agent/input", json={"text": "y"})
        self.client.post("/api/agent/stop")

        self.assertIn("start", self.runner.calls)
        self.assertIn(("send", "y"), self.runner.calls)
        self.assertIn("stop", self.runner.calls)

    def test_empty_input_is_rejected(self):
        data = self.client.post("/api/agent/input", json={"text": "  "}).get_json()

        self.assertFalse(data["ok"])

    def test_config_get_lists_groups_and_values(self):
        data = self.client.get("/api/config").get_json()

        keys = {field["key"] for group in data["groups"] for field in group["fields"]}
        self.assertIn("LLM_PROVIDER", keys)
        self.assertIn("BGI_HOE_CONFIG_NAME", keys)
        provider = [
            field for group in data["groups"] for field in group["fields"] if field["key"] == "LLM_PROVIDER"
        ][0]
        self.assertTrue(provider["is_set"])
        self.assertIsInstance(provider["choices"], list)

    def test_config_save_writes_env_and_backs_up(self):
        data = self.client.post(
            "/api/config", json={"values": {"MODEL_NAME": "new-model", "DEFAULT_UID": "100000000"}}
        ).get_json()

        self.assertTrue(data["ok"])
        self.assertTrue(data["backup"])
        written = self.env_path.read_text(encoding="utf-8")
        self.assertIn("MODEL_NAME=new-model", written)
        self.assertIn("DEFAULT_UID=100000000", written)
        self.assertIn("LLM_PROVIDER=openai", written)

    def test_config_save_reports_warnings(self):
        data = self.client.post(
            "/api/config", json={"values": {"BGI_ROUTE_GROUP_POLICY": "nonsense"}}
        ).get_json()

        self.assertTrue(data["ok"])
        self.assertTrue(any("不在推荐值里" in warning for warning in data["warnings"]))

    def test_config_save_does_not_blank_untouched_keys(self):
        """按分组页提交时，别的键必须保持原值（曾经把整份 .env 清空过）。"""
        self.client.post("/api/config", json={"values": {"MODEL_NAME": "only-this"}})

        written = self.env_path.read_text(encoding="utf-8")
        self.assertIn("MODEL_NAME=only-this", written)
        self.assertIn("LLM_PROVIDER=openai", written)

    def test_config_save_rejects_unknown_keys(self):
        data = self.client.post("/api/config", json={"values": {"NOT_A_FIELD": "x"}}).get_json()

        self.assertFalse(data["ok"])
        self.assertIn("NOT_A_FIELD", data["error"])

    def test_doctor_endpoint_returns_rows(self):
        with patch("studio.server.health_check.run_health_check") as fake:
            fake.return_value = [("ok", "一切正常"), ("warn", "注意"), ("error", "坏了")]
            data = self.client.get("/api/doctor").get_json()

        self.assertEqual(data["summary"], {"errors": 1, "warns": 1})
        self.assertEqual(len(data["rows"]), 3)

    def test_transactions_and_rollback_endpoints(self):
        with patch("skills.config_recovery.list_transactions", return_value=[{"id": "t1", "status": "committed", "updates": 2, "created_at": "now"}]), patch(
            "skills.config_recovery.restore_transaction"
        ) as restore:
            restore.return_value.restored_files = [Path("地图素材.json")]
            restore.return_value.backup_dir = "backups/x"

            items = self.client.get("/api/transactions").get_json()
            result = self.client.post("/api/rollback", json={"id": "t1"}).get_json()

        self.assertEqual(items["items"][0]["id"], "t1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["restored"], ["地图素材.json"])

    def test_rollback_without_id_is_rejected(self):
        data = self.client.post("/api/rollback", json={}).get_json()

        self.assertFalse(data["ok"])

    def test_bettergi_log_endpoint(self):
        log_dir = self.root / "log"
        log_dir.mkdir()
        (log_dir / "bgi.log").write_text("normal\n战斗策略文件不存在\n", encoding="utf-8")

        with patch.object(config, "BGI_DIR", str(self.root)):
            data = self.client.get("/api/bettergi-log").get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["selected"], "bgi.log")
        self.assertEqual(data["highlight"]["level"], "error")

    def test_open_endpoint_rejects_unknown_target(self):
        data = self.client.post("/api/open", json={"target": "nope"}).get_json()

        self.assertFalse(data["ok"])

    def test_open_endpoint_calls_startfile(self):
        with patch("studio.server.os.startfile", create=True) as startfile:
            data = self.client.post("/api/open", json={"target": "project"}).get_json()

        self.assertTrue(data["ok"])
        startfile.assert_called_once()

    def test_shutdown_stops_the_runner(self):
        data = self.client.post("/api/shutdown").get_json()

        self.assertTrue(data["ok"])
        self.assertIn("stop", self.runner.calls)

    def test_routes_endpoint_lists_categories_and_groups(self):
        group_dir = self.root / "ScriptGroup"
        group_dir.mkdir()
        (group_dir / "敌人与魔物.json").write_text(
            json.dumps(
                {
                    "name": "敌人与魔物",
                    "index": 1,
                    "config": {"pathingConfig": {"autoFightConfig": {"strategyName": "根据队伍自动选择"}}},
                    "projects": [
                        {"name": "a.json", "folderName": "敌人与魔物\\蕈兽", "type": "Pathing"},
                        {"name": "s", "folderName": "x", "type": "Javascript"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        pathing = self.root / "AutoPathing" / "锄地专区" / "小怪2000@mno" / "1_2_璃月"
        pathing.mkdir(parents=True)
        (pathing / "a.json").write_text("{}", encoding="utf-8")

        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(group_dir)), patch.object(
            config, "BGI_AUTO_PATHING_DIR", str(self.root / "AutoPathing")
        ):
            data = self.client.get("/api/routes").get_json()

        groups = {row["name"]: row for row in data["groups"]}
        self.assertEqual(groups["敌人与魔物"]["pathing"], 1)
        self.assertTrue(groups["敌人与魔物"]["strategy_ok"])
        categories = {row["action"]: row for row in data["categories"]}
        self.assertEqual(categories["hoe"]["count"], 1)
        self.assertEqual(categories["hoe"]["label"], "锄大地")

    # ---------- 资源冷却页（Studio「资源冷却」） ----------

    def _write_map_group(self, projects):
        group_dir = self.root / "ScriptGroup"
        group_dir.mkdir(exist_ok=True)
        path = group_dir / "地图素材.json"
        path.write_text(
            json.dumps({"name": "地图素材", "projects": projects}, ensure_ascii=False),
            encoding="utf-8",
        )
        return group_dir, path

    def _cooldown_env(self):
        """把资源冷却相关的路径都指到临时目录（别读开发机真实的日志/组/路线仓库）。"""
        manual = self.root / "gather_cooldown_manual.json"
        log_dir = self.root / "bgi-log"
        log_dir.mkdir(exist_ok=True)
        pathing = self.root / "AutoPathing"
        pathing.mkdir(exist_ok=True)
        try:
            gather_cooldown._PATHING_CACHE.update({"key": None, "names": {}})
        except AttributeError:
            pass
        patches = [
            patch.object(config, "BGI_LOG_DIR", str(log_dir)),
            patch.object(config, "BGI_AUTO_PATHING_DIR", str(pathing)),
            patch.object(gather_cooldown, "manual_state_path", return_value=str(manual)),
            patch.object(gather_cooldown, "_ROUTE_TOTALS_CACHE_KEY", {"key": None}),
            patch.object(gather_cooldown, "_ROUTE_INDEX_KEY", {"key": None}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_cooldown_endpoint_lists_what_is_not_in_any_group(self):
        """页脚那句"仓库里有、你没建组"：接口要真的把材料名带出来。

        玩家实测问过"食材与炼金怎么只有一个久雨莲，是读不到吗" ——
        不是读不到，是他的脚本组里只有久雨莲；这里把两者的差集给出来。
        """
        group_dir, group_path = self._write_map_group([
            {"name": "01-久雨莲-厄里那斯-7个.json",
             "folderName": "食材与炼金\\久雨莲", "type": "Pathing"},
        ])
        pathing = self.root / "AutoPathing" / "食材与炼金"
        for name in ("久雨莲", "甜甜花", "薄荷", "提瓦特食材一条龙"):
            (pathing / name).mkdir(parents=True, exist_ok=True)
        self._cooldown_env()

        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(group_dir)), patch.object(
            config, "BGI_COOK_CONFIG", str(group_path)
        ), patch.object(config, "BGI_MAP_CONFIG", ""), patch.object(
            config, "BGI_MINE_CONFIG", ""
        ), patch.object(config, "BGI_ENEMY_CONFIG", ""):
            data = self.client.get("/api/cooldown").get_json()

        section = next(item for item in data["sections"] if item["key"] == "cook")
        self.assertIn("久雨莲", [row["material"] for row in section["materials"]])
        # 仓库里有、组里没有的：甜甜花 / 薄荷；整包脚本「一条龙」不算材料，久雨莲已建组
        self.assertEqual(section["unsubscribed"], ["甜甜花", "薄荷"])

    def test_cooldown_endpoint_lists_materials_with_status(self):
        group_dir, group_path = self._write_map_group([
            {"name": "01-霜仙花-彩冰镇左上-3个.json",
             "folderName": "地方特产\\挪德卡莱\\霜仙花\\无草神@某人", "type": "Pathing"},
            {"name": "01-慕风蘑菇-晨曦酒庄-7个.json",
             "folderName": "地方特产\\蒙德\\慕风蘑菇", "type": "Pathing"},
        ])
        self._cooldown_env()

        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", str(group_dir)), patch.object(
            config, "BGI_MAP_CONFIG", str(group_path)
        ), patch.object(config, "BGI_MINE_CONFIG", ""), patch.object(config, "BGI_COOK_CONFIG", ""):
            data = self.client.get("/api/cooldown").get_json()
            marked = self.client.post(
                "/api/cooldown/manual", json={"material": "霜仙花", "action": "mark"}
            ).get_json()
            after = self.client.get("/api/cooldown").get_json()
            cleared = self.client.post(
                "/api/cooldown/manual", json={"material": "霜仙花", "action": "clear"}
            ).get_json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["hours"], 48)
        self.assertIn("summary", data)
        rows = {row["material"]: row for row in data["materials"]}
        # 材料名要按"先文件名"的规则读出来（慕风蘑菇那条以前会被读成目录末段）
        self.assertIn("霜仙花", rows)
        self.assertIn("慕风蘑菇", rows)
        self.assertIn("describe", rows["霜仙花"])

        self.assertTrue(marked["ok"])
        cooling = {row["material"]: row for row in after["materials"]}
        self.assertTrue(cooling["霜仙花"]["cooling"])
        self.assertTrue(cooling["霜仙花"]["manual"])
        self.assertGreater(cooling["霜仙花"]["hours_left"], 47)      # 刚登记 ≈ 满 48 小时
        self.assertIn("还没刷新", cooling["霜仙花"]["describe"])

        self.assertTrue(cleared["ok"])
        self.assertFalse(
            next(row for row in self.client.get("/api/cooldown").get_json()["materials"]
                 if row["material"] == "霜仙花")["cooling"]
        )

    def test_cooldown_manual_validates_input(self):
        self._cooldown_env()

        missing = self.client.post("/api/cooldown/manual", json={})
        bad_action = self.client.post("/api/cooldown/manual", json={"material": "x", "action": "boom"})

        self.assertEqual(missing.status_code, 400)
        self.assertFalse(missing.get_json()["ok"])
        self.assertEqual(bad_action.status_code, 400)

    def test_cooldown_endpoint_survives_a_broken_log_dir(self):
        """日志目录读不到也不能 500：页面要能打开并说明情况。"""
        self._cooldown_env()

        with patch.object(
            gather_cooldown, "overview", side_effect=OSError("磁盘炸了")
        ):
            response = self.client.get("/api/cooldown")

        self.assertEqual(response.status_code, 500)
        self.assertIn("读取冷却失败", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
