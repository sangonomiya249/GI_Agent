"""远程通道管理（studio/channel_runner.py）的测试。

玩家要的逻辑：
* 点「启动 Agent」时，**已配置**且开着「随 Agent 自动启动」的通道被一起唤醒；
* 没填配置的通道不启动，并写清缺哪一项（绝不静默）；
* 飞书默认不自动启动（它是 Webhook 服务，没内网穿透时收不到消息）。

全程不真的拉子进程：把 `ChannelRunner.start/stop` 换成记录用的假实现。
"""

import unittest
from unittest.mock import patch

from studio import channel_runner


def make_manager(values):
    """建一个用固定 .env 内容的管理器（不读真实 .env）。"""
    return channel_runner.ChannelManager(env_loader=lambda: dict(values))


QQ_READY = {"QQ_BOT_APPID": "10001", "QQ_BOT_SECRET": "s3cret"}
FEISHU_READY = {"FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "y"}


class FakeProc:
    def __init__(self, pid=4321, exit_code=None):
        self.pid = pid
        self._exit_code = exit_code

    def poll(self):
        return self._exit_code


class ChannelConfigTests(unittest.TestCase):
    def test_qq_needs_both_keys(self):
        spec = channel_runner.spec_for("qq")

        self.assertTrue(channel_runner.channel_is_configured(spec, QQ_READY))
        self.assertFalse(channel_runner.channel_is_configured(spec, {"QQ_BOT_APPID": "10001"}))
        self.assertFalse(channel_runner.channel_is_configured(spec, {}))
        self.assertEqual(
            channel_runner.missing_keys(spec, {"QQ_BOT_APPID": "10001"}), ["QQ_BOT_SECRET"]
        )

    def test_blank_values_do_not_count_as_configured(self):
        spec = channel_runner.spec_for("qq")
        self.assertFalse(channel_runner.channel_is_configured(spec, {"QQ_BOT_APPID": "  ", "QQ_BOT_SECRET": "x"}))

    def test_auto_start_defaults(self):
        qq = channel_runner.spec_for("qq")
        feishu = channel_runner.spec_for("feishu")

        self.assertTrue(channel_runner.auto_start_enabled(qq, {}))            # QQ 默认开
        self.assertFalse(channel_runner.auto_start_enabled(feishu, {}))       # 飞书默认关
        self.assertFalse(channel_runner.auto_start_enabled(qq, {"AUTO_START_QQ_BOT": "0"}))
        self.assertTrue(channel_runner.auto_start_enabled(feishu, {"AUTO_START_FEISHU_BOT": "1"}))

    def test_spec_lookup(self):
        self.assertEqual(channel_runner.spec_for("qq")["script"], "qq_main.py")
        self.assertIsNone(channel_runner.spec_for("nope"))


class ChannelManagerTests(unittest.TestCase):
    def test_summary_reports_configuration(self):
        manager = make_manager(QQ_READY)
        summary = {row["name"]: row for row in manager.summary()}

        self.assertTrue(summary["qq"]["configured"])
        self.assertEqual(summary["qq"]["state"], "stopped")
        self.assertFalse(summary["feishu"]["configured"])
        self.assertEqual(summary["feishu"]["missing"], ["FEISHU_APP_ID", "FEISHU_APP_SECRET"])

    def test_start_refuses_unconfigured_channel(self):
        manager = make_manager({})

        result = manager.start("qq")

        self.assertFalse(result["ok"])
        self.assertIn("QQ_BOT_SECRET", result["error"])
        # 原因也写进通道日志里，界面看得到
        lines = manager.runners["qq"].snapshot()["lines"]
        self.assertTrue(any("没配置" in line["text"] for line in lines))

    def test_start_launches_the_script(self):
        manager = make_manager(QQ_READY)
        started = {}

        def fake_start(reason="手动启动"):
            started["reason"] = reason
            return {"ok": True, "pid": 4321, "name": "qq"}

        with patch.object(manager.runners["qq"], "start", fake_start):
            result = manager.start("qq", reason="随 Agent 自动启动")

        self.assertTrue(result["ok"])
        self.assertEqual(started["reason"], "随 Agent 自动启动")

    def test_unknown_channel(self):
        manager = make_manager({})
        self.assertFalse(manager.start("nope")["ok"])
        self.assertFalse(manager.stop("nope")["ok"])
        self.assertFalse(manager.clear("nope")["ok"])
        self.assertIsNone(manager.view("nope"))

    def test_python_command_uses_the_channel_entrypoint(self):
        manager = make_manager(QQ_READY)
        command = manager.runners["qq"].command()

        self.assertTrue(command[-1].endswith("qq_main.py"), command)
        self.assertIn("-u", command)


class AutoStartTests(unittest.TestCase):
    """点「启动 Agent」时到底唤醒谁。"""

    def _manager(self, values, started=None):
        manager = make_manager(values)
        self.started = started if started is not None else []

        def fake_start(reason="手动启动"):
            self.started.append(reason)
            return {"ok": True, "pid": 111}

        for runner in manager.runners.values():
            patch.object(runner, "start", fake_start).start()
            self.addCleanup(patch.stopall)
        return manager

    def test_wakes_configured_channel_with_auto_start(self):
        manager = self._manager(QQ_READY)

        notes = manager.start_automatic()

        self.assertEqual(self.started, ["随 Agent 自动启动"])
        self.assertTrue(any("已随 Agent 启动" in note for note in notes))

    def test_skips_unconfigured_channel_with_a_reason(self):
        manager = self._manager({})

        notes = manager.start_automatic()

        self.assertEqual(self.started, [])
        self.assertTrue(any("没配置" in note and "QQ_BOT_APPID" in note for note in notes))
        self.assertTrue(any("飞书" in note for note in notes))

    def test_skips_when_auto_start_is_off(self):
        manager = self._manager({**QQ_READY, "AUTO_START_QQ_BOT": "0"})

        notes = manager.start_automatic()

        self.assertEqual(self.started, [])
        self.assertTrue(any("自动启动已关闭" in note for note in notes))

    def test_feishu_starts_when_explicitly_enabled(self):
        manager = self._manager({**FEISHU_READY, "AUTO_START_FEISHU_BOT": "1"})

        notes = manager.start_automatic()

        self.assertEqual(len(self.started), 1)
        self.assertTrue(any("飞书服务端：已随 Agent 启动" in note for note in notes))

    def test_already_running_is_not_started_twice(self):
        manager = self._manager(QQ_READY)
        manager.runners["qq"]._proc = FakeProc()
        manager.runners["qq"]._state = "running"

        notes = manager.start_automatic()

        self.assertEqual(self.started, [])
        self.assertTrue(any("已经在运行" in note for note in notes))

    def test_failure_is_reported(self):
        manager = make_manager(QQ_READY)
        with patch.object(manager.runners["qq"], "start", lambda reason="": {"ok": False, "error": "锁被占用"}):
            notes = manager.start_automatic()

        self.assertTrue(any("启动失败" in note and "锁被占用" in note for note in notes))

    def test_env_loader_failure_does_not_crash(self):
        def boom():
            raise RuntimeError("读不到 .env")

        manager = channel_runner.ChannelManager(env_loader=boom)
        notes = manager.start_automatic()

        self.assertTrue(notes)
        self.assertTrue(all("没配置" in note for note in notes), notes)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_merges_view_and_log(self):
        manager = make_manager(QQ_READY)
        manager.runners["qq"].note("hello")

        payload = manager.snapshot({"qq": 0})

        row = payload["channels"][0]
        self.assertTrue(row["configured"])
        self.assertTrue(row["lines"])
        self.assertEqual(row["lines"][0]["text"].strip(), "hello")

    def test_snapshot_can_be_incremental(self):
        manager = make_manager(QQ_READY)
        manager.runners["qq"].note("第一行")
        seq = manager.runners["qq"].snapshot()["seq"]

        payload = manager.snapshot({"qq": seq})

        self.assertEqual(payload["channels"][0]["lines"], [])

    def test_stop_all_covers_every_channel(self):
        manager = make_manager({})
        for runner in manager.runners.values():
            runner._proc = FakeProc()
            runner._state = "running"

        results = manager.stop_all()

        self.assertEqual({row["name"] for row in results}, {"qq", "feishu"})
        self.assertEqual([row["name"] for row in results if row.get("pid")], ["qq", "feishu"])


if __name__ == "__main__":
    unittest.main()
