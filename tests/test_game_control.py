"""关闭原神（skills/game_control.py + 两条入口）的测试。

玩家要求："添加一个让 ai 帮我关掉原神的 skill，当用户提到帮我关闭原神/类似的指令就把原神关掉"。
这类操作不可逆（关错了要重开游戏 + 重新登录），所以：
  · 只认明确的"关原神"说法，否定/疑问/游戏内设置都要挡住；
  · 一律先出计划、等玩家回 y 再动手；
  · 关闭结果**如实报告**（判不出来就说判不出来，绝不说"已关闭"）。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import config
from skills import game_control


class IntentTests(unittest.TestCase):
    def test_clear_close_requests(self):
        for text in ("帮我关闭原神", "关掉原神", "退出原神", "把原神关了", "关闭游戏",
                     "原神关了吧", "退游戏", "帮我关一下原神"):
            with self.subTest(text=text):
                intent = game_control.detect_intent(text)
                self.assertIsNotNone(intent, text)
                self.assertEqual(intent["action"], "close_game")
                self.assertFalse(intent["close_bettergi"])

    def test_negation_is_respected(self):
        """玩家说"别关原神"时绝不能关 —— 这是最不能出错的一条。"""
        for text in ("别关原神", "不要关游戏", "先别关原神", "不用关游戏", "暂时别退原神"):
            with self.subTest(text=text):
                self.assertIsNone(game_control.detect_intent(text))

    def test_questions_are_not_commands(self):
        for text in ("原神关了吗", "游戏退了吗", "原神还开着么"):
            with self.subTest(text=text):
                self.assertIsNone(game_control.detect_intent(text))

    def test_in_game_settings_are_not_commands(self):
        """"关掉游戏声音/画质" 不能把游戏关掉。"""
        for text in ("关掉游戏声音", "帮我关掉游戏音效", "关闭游戏全屏", "把游戏画质关掉"):
            with self.subTest(text=text):
                self.assertIsNone(game_control.detect_intent(text))

    def test_unrelated_chat(self):
        for text in ("今天打什么", "帮我刷点骗骗花", ""):
            with self.subTest(text=text):
                self.assertIsNone(game_control.detect_intent(text))

    def test_bgi_can_be_asked_for_explicitly(self):
        for text in ("关掉原神和bgi", "把原神和bettergi都关了", "原神和挂机工具一起关"):
            with self.subTest(text=text):
                intent = game_control.detect_intent(text)
                self.assertIsNotNone(intent, text)
                self.assertTrue(intent["close_bettergi"], text)

    def test_llm_intent_is_normalized(self):
        intent = game_control.normalize_intent({"action": "close_game", "close_bettergi": True})

        self.assertEqual(intent["action"], "close_game")
        self.assertTrue(intent["close_bettergi"])
        self.assertEqual(game_control.normalize_intent({})["action"], "close_game")


def _fake_processes(sequence):
    """按调用顺序返回进程快照（第一次"在跑"、后面"没了"）。"""
    state = {"index": 0}

    def _running():
        index = min(state["index"], len(sequence) - 1)
        state["index"] += 1
        return sequence[index]

    return _running


RUNNING = {"YuanShen.exe": [4242]}
GONE = {"YuanShen.exe": []}
UNKNOWN = {"YuanShen.exe": None}


class CloseGameTests(unittest.TestCase):
    def test_nothing_running_is_reported_honestly(self):
        with patch.object(game_control, "running_processes", _fake_processes([GONE])):
            result = game_control.close_game()

        self.assertFalse(result["opened"])
        self.assertIn("没有在运行", result["summary"])
        self.assertFalse(result["unknown"])

    def test_unknown_state_says_so_instead_of_claiming_success(self):
        # 兜底那条也要打桩：真跑会往 scripts\ 写 PID 记录文件
        with patch.object(game_control, "running_processes", _fake_processes([UNKNOWN])), patch.object(
            game_control, "_close_via_scheduled_task", return_value=(False, "没有可用的计划任务")
        ):
            result = game_control.close_game()

        self.assertTrue(result["unknown"])
        self.assertIn("说不准", result["summary"])

    def test_graceful_close(self):
        with patch.object(game_control, "running_processes", _fake_processes([RUNNING, GONE])), patch.object(
            game_control, "_taskkill", return_value=(True, "")
        ) as kill:
            result = game_control.close_game()

        self.assertEqual(result["closed"]["YuanShen.exe"], [4242])
        self.assertEqual(result["forced"], {})
        self.assertIn("正常关闭", result["summary"])
        kill.assert_called_once()                     # 没走到强杀

    def test_force_close_when_graceful_fails(self):
        # 顺序：① 初始探测 ② 宽限期内复查 ③ 强杀前重新枚举 ④ 最终复核
        sequence = [RUNNING, RUNNING, RUNNING, GONE]
        with patch.object(game_control, "running_processes", _fake_processes(sequence)), \
                patch.object(game_control, "_taskkill", return_value=(True, "")) as kill:
            result = game_control.close_game(grace_seconds=0)

        self.assertEqual(result["forced"]["YuanShen.exe"], [4242])
        self.assertIn("强制结束", result["summary"])
        self.assertEqual(kill.call_count, 2)          # 先正常、后强杀

    def test_still_running_is_not_reported_as_closed(self):
        # 强杀之后那段"再等 5 秒复核"是真实行为，但测试里没必要真睡 —— 直接打桩成"还是没退"。
        # 计划任务兜底同理要打桩：真跑会在 scripts\ 下写出 PID 记录文件（污染仓库）。
        with patch.object(game_control, "running_processes", _fake_processes([RUNNING])), patch.object(
            game_control, "_taskkill", return_value=(False, "拒绝访问")
        ), patch.object(game_control, "_wait_until_gone", return_value=True), patch.object(
            game_control, "_close_via_scheduled_task", return_value=(False, "没有可用的计划任务")
        ):
            result = game_control.close_game(grace_seconds=0)

        self.assertTrue(result["still_running"])
        self.assertIn("仍然在运行", result["summary"])

    def test_process_names_come_from_bgi_config_with_fallbacks(self):
        names = game_control.game_exe_names()

        self.assertIn("YuanShen.exe", names)
        self.assertIn("GenshinImpact.exe", names)
        self.assertEqual(len(names), len(set(names)))


class PermissionFallbackTests(unittest.TestCase):
    """玩家实测（22:2x）：原神是管理员权限跑起来的，普通 taskkill 只得到"拒绝访问"。

    所以关不掉时要**自动改走计划任务 StopGenshin**（和 StopBetterGI 一个套路）。
    """

    def test_access_denied_falls_back_to_the_scheduled_task(self):
        with patch.object(game_control, "running_processes", _fake_processes([RUNNING])), patch.object(
            game_control, "_taskkill", return_value=(False, "错误: 无法终止 PID 为 4242 的进程。原因: 拒绝访问。")
        ), patch.object(
            game_control, "_close_via_scheduled_task", return_value=(True, "权限不足，已改走计划任务（管理员权限）关闭原神")
        ) as fallback, patch.object(
            game_control, "_wait_until_gone", return_value=False
        ):
            result = game_control.close_game(grace_seconds=0)

        fallback.assert_called_once()
        self.assertTrue(result["via_task"])
        self.assertIn("计划任务", result["summary"])

    def test_fallback_is_not_used_when_close_succeeds(self):
        with patch.object(game_control, "running_processes", _fake_processes([RUNNING, GONE])), patch.object(
            game_control, "_taskkill", return_value=(True, "")
        ), patch.object(game_control, "_close_via_scheduled_task") as fallback:
            game_control.close_game()

        fallback.assert_not_called()

    def test_fallback_failure_is_reported_honestly(self):
        note = (
            "计划任务 StopGenshin 不存在或触发失败。请以【管理员身份】运行一次：\n"
            "     powershell -ExecutionPolicy Bypass -File scripts\\setup_start_bettergi_task.ps1"
        )
        with patch.object(game_control, "running_processes", _fake_processes([RUNNING])), patch.object(
            game_control, "_taskkill", return_value=(False, "拒绝访问")
        ), patch.object(
            game_control, "_close_via_scheduled_task", return_value=(False, note)
        ), patch.object(
            game_control, "_wait_until_gone", return_value=True
        ):
            result = game_control.close_game(grace_seconds=0)

        self.assertFalse(result["via_task"])
        self.assertIn("仍然在运行", result["summary"])
        self.assertIn("setup_start_bettergi_task.ps1", result["summary"])

    def test_pid_file_is_written_for_the_task(self):
        """计划任务只按 PID 关（避免 PID 复用误杀），所以要先把目标 PID 写下来。"""
        with patch.object(game_control, "stop_pid_file", return_value=os.path.join(self.tmp.name, "stop_genshin.pids")):
            path = game_control._write_stop_pid_file([11, 22])

            content = open(path, encoding="utf-8").read()

        self.assertIn("created=", content)
        self.assertIn("11", content)
        self.assertIn("22", content)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class PlanTests(unittest.TestCase):
    def test_plan_says_what_will_happen(self):
        with patch.object(game_control, "running_processes", _fake_processes([RUNNING])), patch.object(
            game_control, "bettergi_busy", return_value=False
        ):
            lines = game_control.plan_lines({"action": "close_game"})

        joined = "\n".join(lines)
        self.assertIn("关闭原神", joined)
        self.assertIn("4242", joined)                     # 有 PID 才看得出关的是谁
        self.assertIn("先正常关闭", joined)
        self.assertIn("不会丢档", joined)
        self.assertIn("StopGenshin", joined)              # 权限不够时的兜底也要写清楚
        self.assertNotIn("顺带关闭 BetterGI", joined)      # 没在跑任务就不捎带

    def test_plan_mentions_bettergi_when_it_is_busy(self):
        """BGI 正在跑任务时要在审批屏里写明"连它一起关"，玩家点 y 前能看到。"""
        with patch.object(game_control, "running_processes", _fake_processes([RUNNING])), patch.object(
            game_control, "bettergi_busy", return_value=True
        ):
            lines = game_control.plan_lines({"action": "close_game"})

        joined = "\n".join(lines)
        self.assertIn("顺带关闭 BetterGI", joined)
        self.assertIn("正在跑任务", joined)


class ExecuteTests(unittest.TestCase):
    def test_execute_reports_both_steps(self):
        with patch.object(game_control, "close_game", return_value={
            "opened": True, "closed": {"YuanShen.exe": [1]}, "forced": {},
            "still_running": {}, "unknown": False, "summary": "YuanShen.exe(1) 正常关闭",
        }), patch.object(game_control, "close_bettergi", return_value=(True, "已关闭 BetterGI（PID 9）")), \
                patch.object(game_control, "bettergi_busy", return_value=True):
            report = game_control.execute({"action": "close_game"})

        self.assertIn("已关闭 BetterGI", report)
        self.assertIn("原神已关闭", report)

    def test_execute_sends_a_notice(self):
        sent = []
        with patch.object(game_control, "close_game", return_value={
            "opened": False, "closed": {}, "forced": {}, "still_running": {},
            "unknown": False, "summary": "原神当前没有在运行（不需要关闭）。",
        }), patch.object(game_control, "bettergi_busy", return_value=False), patch(
            "skills.bgi_controller.send_notice", lambda target, text: sent.append((target, text)) or True
        ):
            game_control.execute({"action": "close_game"}, open_id="ou_me")

        self.assertEqual(len(sent), 1)
        self.assertIn("关闭操作完成", sent[0][1])

    def test_execute_does_not_claim_success_when_unsure(self):
        with patch.object(game_control, "close_game", return_value={
            "opened": True, "closed": {}, "forced": {}, "still_running": {"YuanShen.exe": [1]},
            "unknown": False, "summary": "YuanShen.exe(1) 仍然在运行",
        }), patch.object(game_control, "bettergi_busy", return_value=False):
            report = game_control.execute({"action": "close_game"})

        self.assertIn("结果不确定", report)
        self.assertIn("Alt+F4", report)


class RouterTests(unittest.TestCase):
    """关键词快通道：不叫大模型，直接出审批；回 y 才执行。"""

    def setUp(self):
        from channels import agent_router

        self.router = agent_router
        self.replies = []
        self.store = {"uid": "1", "messages": [], "pending_task": None}
        patches = [
            patch.object(agent_router.memory_manager, "load_chat_store", lambda: self.store),
            patch.object(agent_router.memory_manager, "save_chat_store", lambda store: None),
            patch.object(agent_router, "refresh_store_env_context", lambda store, uid, force=False: (True, "ok")),
            patch.object(agent_router, "reply", lambda target, text: self.replies.append(text)),
            patch.object(agent_router.llm_brain, "ask_agent", lambda *a, **k: self.replies.append("__LLM__")),
            patch.object(agent_router.game_control, "plan_lines", lambda intent: ["🛑 系统操作：关闭原神"]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_close_request_creates_an_approval_without_calling_the_model(self):
        self.router.handle_message("帮我关闭原神", "ou_me")

        self.assertEqual(len(self.replies), 1)
        self.assertIn("系统操作", self.replies[0])
        self.assertNotIn("__LLM__", self.replies)
        self.assertEqual(self.store["pending_task"]["system_task"]["action"], "close_game")

    def test_unrelated_message_still_goes_to_the_model(self):
        # ⚠️ 路由是**起线程**去叫大模型的，直接断言会和线程赛跑（整套跑的时候偶发失败）。
        #    这里把 Thread 换成同步执行的假货，断言才稳定。
        def fake_thread(target=None, args=(), **kwargs):
            class T:
                def start(self):
                    target(*args)
            return T()

        with patch.object(self.router.threading, "Thread", fake_thread):
            self.router.handle_message("帮我刷点骗骗花", "ou_me")

        self.assertIn("__LLM__", self.replies)
        self.assertIsNone(self.store["pending_task"])

    def test_approval_runs_the_close(self):
        executed = []
        self.store["pending_task"] = {"system_task": {"action": "close_game"}, "uid": "1"}
        with patch.object(self.router.game_control, "execute", lambda intent, target: executed.append((intent, target))), \
                patch.object(self.router.threading, "Thread",
                             lambda target=None, args=(): type("T", (), {"start": lambda self: target(*args)})()):
            self.router.handle_message("y", "ou_me")

        self.assertEqual(executed, [({"action": "close_game"}, "ou_me")])
        self.assertIsNone(self.store["pending_task"])

    def test_other_reply_abandons_the_system_task(self):
        self.store["pending_task"] = {"system_task": {"action": "close_game"}, "uid": "1"}
        self.router.handle_message("算了还是别关了", "ou_me")

        self.assertIsNone(self.store["pending_task"])
        self.assertTrue(any("已放弃" in text for text in self.replies))


class BettergiBusyTests(unittest.TestCase):
    """`bettergi_busy`：判"在不在跑任务"要看日志里的终态/启动标记，不能只看"最近有没有写"。

    实测（QQ 12:36）：一条龙 12:34:40 就跑完了，BGI 窗口还开着、空闲时也在零碎写日志，
    于是被判成"正在跑任务"——关闭原神那次会莫名捎带关掉 BGI，下发计划那次直接把玩家拒了。
    """

    def test_not_running_is_never_busy(self):
        from skills import bgi_controller

        with patch.object(bgi_controller, "_bettergi_pids", lambda: []), patch.object(
            bgi_controller, "_bettergi_log_recently_written", lambda seconds=None: True
        ):
            self.assertFalse(game_control.bettergi_busy())

    def test_running_but_finished_is_not_busy(self):
        from skills import bgi_controller

        with patch.object(bgi_controller, "_bettergi_pids", lambda: [99]), patch.object(
            bgi_controller, "_bettergi_task_state", lambda log_bytes=None: "idle"
        ):
            self.assertFalse(game_control.bettergi_busy())

    def test_running_a_task_is_busy(self):
        from skills import bgi_controller

        with patch.object(bgi_controller, "_bettergi_pids", lambda: [99]), patch.object(
            bgi_controller, "_bettergi_task_state", lambda log_bytes=None: "running"
        ):
            self.assertTrue(game_control.bettergi_busy())

    def test_probe_failure_is_not_busy(self):
        from skills import bgi_controller

        with patch.object(
            bgi_controller, "_bettergi_pids", side_effect=RuntimeError("boom")
        ):
            self.assertFalse(game_control.bettergi_busy())


class LlmPathTests(unittest.TestCase):
    """大模型那条：识别出 system_task 就走系统操作审批，不去写 BetterGI 配置。"""

    def test_system_task_json_creates_an_approval(self):
        from brain import llm_brain

        captured = {}

        def fake_chat(_client, _model, messages, temperature=0.7):
            return (
                "好的，我准备关闭原神。\n\n```json\n"
                '{"system_task": {"action": "close_game", "close_bettergi": false}}\n```'
            )

        store = {"env_context": "展柜", "env_context_names": [], "wallet": {"mora": 0, "exp_books": 0, "boss_mats": {}}}
        with patch("brain.llm_brain._make_client", return_value=object()), patch(
            "brain.llm_brain.complete_chat", side_effect=fake_chat
        ), patch("brain.memory_manager.save_chat_store"), patch.object(
            game_control, "running_processes", _fake_processes([RUNNING])
        ), patch.object(
            game_control, "bettergi_busy", return_value=False
        ), patch.object(
            llm_brain.feishu_api, "send_feishu_msg", lambda target, text: captured.setdefault("msg", text)
        ):
            llm_brain.ask_agent([{"role": "user", "content": "帮我关闭原神"}], store, "1", "ou_me")

        self.assertEqual(store["pending_task"]["system_task"]["action"], "close_game")
        self.assertIn("关闭原神", captured["msg"])


class PendingTaskPersistenceTests(unittest.TestCase):
    """⚠️ 玩家实测的 bug（22:18 那次）：系统操作计划在**读出时被丢掉**。

    记忆层的校验以前硬要求 `pending_task` 里有 `bgi_cmd`，而"关闭原神"的计划是
    `{"system_task": …}` —— 于是写完就丢，玩家回 y 时看不到待审批计划，消息被当成
    普通聊天喂给大模型，它只好又规划了一遍打 Boss（界面上就成了"y 之后冒出个 Boss 计划"）。
    这里用**真的** memory_manager 走一遍存档 / 读档。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "chat_context.json")
        patcher = patch.object(config, "HISTORY_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_system_task_survives_a_round_trip(self):
        from brain import memory_manager

        store = memory_manager.load_chat_store()
        store["pending_task"] = {
            "system_task": {"action": "close_game", "close_bettergi": False},
            "open_id": "qq:c2c:U1#M1",
            "uid": "100000000",
        }
        memory_manager.save_chat_store(store)

        loaded = memory_manager.load_chat_store()

        self.assertIsNotNone(loaded["pending_task"])
        self.assertEqual(loaded["pending_task"]["system_task"]["action"], "close_game")

    def test_route_plan_still_survives(self):
        from brain import memory_manager

        store = memory_manager.load_chat_store()
        store["pending_task"] = {
            "bgi_cmd": {"energy_task": {"action": "run_boss"}},
            "open_id": "ou_me",
            "uid": "1",
        }
        memory_manager.save_chat_store(store)

        loaded = memory_manager.load_chat_store()

        self.assertIsNotNone(loaded["pending_task"])
        self.assertIn("bgi_cmd", loaded["pending_task"])

    def test_junk_pending_task_is_still_dropped(self):
        """既没有 bgi_cmd 也没有 system_task 的垃圾计划照样丢掉（别让坏数据卡住审批）。"""
        from brain import memory_manager

        store = memory_manager.load_chat_store()
        store["pending_task"] = {"whatever": 1, "open_id": "ou_me"}
        memory_manager.save_chat_store(store)

        self.assertIsNone(memory_manager.load_chat_store()["pending_task"])

    def test_end_to_end_approval_executes_the_close(self):
        """真实存取 + 真实路由：说"帮我关闭原神" → 回 y → 真的走到执行（不叫大模型）。"""
        from channels import agent_router

        executed = []
        llm_calls = []
        replies = []

        def fake_thread(target=None, args=(), **kwargs):
            class T:
                def start(self):
                    target(*args)
            return T()

        with patch.object(agent_router, "refresh_store_env_context",
                          lambda store, uid, force=False: (True, "ok")), patch.object(
            agent_router, "reply", lambda target, text: replies.append(text)
        ), patch.object(agent_router.threading, "Thread", fake_thread), patch.object(
            agent_router.llm_brain, "ask_agent", lambda *a, **k: llm_calls.append("asked")
        ), patch.object(
            game_control, "execute", lambda intent, target: executed.append(intent)
        ), patch.object(
            game_control, "plan_lines", lambda intent: ["🛑 系统操作：关闭原神"]
        ):
            agent_router.handle_message("帮我关闭原神", "qq:c2c:U1#M1")
            agent_router.handle_message("y", "qq:c2c:U1#M2")

        self.assertEqual(len(executed), 1)
        self.assertEqual(llm_calls, [], "不该去打扰大模型")
        self.assertTrue(any("系统操作" in text for text in replies))

    def test_repeated_y_without_pending_task_does_not_replan(self):
        """重复投递的 y 不能让模型重新规划一遍（实测就是这么冒出个 Boss 计划的）。"""
        from channels import agent_router

        llm_calls = []
        replies = []

        with patch.object(agent_router, "refresh_store_env_context",
                          lambda store, uid, force=False: (True, "ok")), patch.object(
            agent_router, "reply", lambda target, text: replies.append(text)
        ), patch.object(
            agent_router.llm_brain, "ask_agent", lambda *a, **k: llm_calls.append("asked")
        ):
            agent_router.handle_message("y", "qq:c2c:U1#M9")

        self.assertEqual(llm_calls, [])
        self.assertTrue(any("没有待确认的计划" in text for text in replies))


    def test_setup_hint_uses_an_absolute_path(self):
        """实测踩过：管理员 PowerShell 默认在 C:\\WINDOWS\\system32，相对路径会"参数不存在"。

        所以提示里必须是完整路径，而且那个文件得真的在。
        """
        hint = game_control.setup_task_hint()

        self.assertIn("setup_start_bettergi_task.ps1", hint)
        self.assertIn(":\\", hint)                       # C:\... 绝对路径
        path = hint.split('"')[1]
        self.assertTrue(os.path.isfile(path), f"提示里的脚本不存在：{path}")

    def test_fallback_failure_mentions_the_full_command(self):
        with patch.object(game_control, "running_processes", _fake_processes([RUNNING])), patch.object(
            game_control, "_taskkill", return_value=(False, "拒绝访问")
        ), patch.object(game_control, "_wait_until_gone", return_value=True), patch.object(
            game_control, "_write_stop_pid_file", return_value="x.pids"
        ), patch.object(
            game_control.subprocess, "run",
            return_value=type("R", (), {"returncode": 1, "stdout": b"", "stderr": b""})(),
        ):
            result = game_control.close_game(grace_seconds=0)

        self.assertIn("setup_start_bettergi_task.ps1", result["fallback_note"])
        self.assertIn(":\\", result["fallback_note"])    # 完整路径


class PowerShellAssetTests(unittest.TestCase):
    """`scripts/*.ps1` 必须带 UTF-8 BOM。

    踩过的坑：这些脚本里写着中文，而 **Windows PowerShell 5.1 在没有 BOM 时会按 ANSI/GBK 读**，
    中文变乱码后甚至会撞出语法错误（实测 `setup_start_bettergi_task.ps1` 丢了 BOM 就直接
    "Missing closing '}'"）。编辑工具容易把 BOM 吃掉，所以这里钉住。
    """

    def test_all_scripts_keep_their_bom(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        folder = os.path.join(repo, "scripts")
        missing = []
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".ps1"):
                continue
            with open(os.path.join(folder, name), "rb") as handle:
                if handle.read(3) != b"\xef\xbb\xbf":
                    missing.append(name)

        self.assertEqual(missing, [], f"这些脚本缺 UTF-8 BOM（PS 5.1 会把中文读成乱码）：{missing}")

    def test_close_script_only_kills_whitelisted_names(self):
        """关闭脚本必须带进程名白名单，且只认 PID 文件（绝不按名字乱杀）。"""
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        text = open(os.path.join(repo, "scripts", "stop_genshin.ps1"), encoding="utf-8-sig").read()

        self.assertIn("YuanShen", text)
        self.assertIn("GenshinImpact", text)
        self.assertIn("-contains $proc.ProcessName", text)     # 白名单校验
        self.assertIn("stop_genshin.pids", text)
        self.assertIn("300", text)                             # PID 记录过期保护


if __name__ == "__main__":
    unittest.main()
