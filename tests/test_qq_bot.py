"""QQ 官方机器人接口（channels/qq_bot.py）的测试。

HTTP 用 FakeHTTP 顶替（记录请求体），网关用一个本地 WebSocket 假服务器，
所以整套测试不碰外网、也不需要真的 AppID。
"""

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from websockets.asyncio.server import serve as ws_serve

from api import channel_router
from channels import qq_bot

# 假网关会因为客户端主动断开而在服务端打日志（EOFError），测试里不需要看
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload, ensure_ascii=False) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("响应不是 JSON")
        return self._payload


class FakeHTTP:
    """顶替 httpx.AsyncClient：按顺序返回预设响应，并记录所有请求。"""

    def __init__(self, token_payload=None, gateway_payload=None, post_responses=None, put_responses=None):
        self.token_payload = token_payload if token_payload is not None else {"access_token": "TOKEN1", "expires_in": "7200"}
        self.gateway_payload = gateway_payload if gateway_payload is not None else {"url": "wss://api.sgroup.qq.com/websocket/"}
        self.post_responses = list(post_responses or [])
        self.put_responses = list(put_responses or [])
        self.requests = []          # [(method, url, json, headers)]
        self.token_calls = 0
        self.closed = False

    async def post(self, url, json=None, headers=None):
        self.requests.append(("POST", url, json, headers))
        if "getAppAccessToken" in url:
            self.token_calls += 1
            if isinstance(self.token_payload, FakeResponse):
                return self.token_payload
            return FakeResponse(200, self.token_payload)
        if self.post_responses:
            return self.post_responses.pop(0)
        return FakeResponse(200, {})

    async def put(self, url, json=None, headers=None):
        self.requests.append(("PUT", url, json, headers))
        if self.put_responses:
            return self.put_responses.pop(0)
        return FakeResponse(200, {})

    async def get(self, url, headers=None):
        self.requests.append(("GET", url, None, headers))
        if isinstance(self.gateway_payload, FakeResponse):
            return self.gateway_payload
        return FakeResponse(200, self.gateway_payload)

    async def aclose(self):
        self.closed = True


def make_client(http=None, **kwargs):
    config = qq_bot.QQBotConfig(appid="10001", secret="s3cret", **kwargs)
    client = qq_bot.QQBotClient(config)
    if http is not None:
        client._http = http
    return client


def group_event(text="  <@!1234> 帮我刷绝缘本  ", msg_id="MSG1", member="user1"):
    return {
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "id": msg_id,
            "content": text,
            "group_openid": "GROUP_A",
            "author": {"member_openid": member},
        },
    }


class ButtonAndInteractionTests(unittest.IsolatedAsyncioTestCase):
    """审批按钮（keyboard）与互动事件（INTERACTION_CREATE）。"""

    def setUp(self):
        channel_router.clear()
        self.addCleanup(channel_router.clear)
        self.routed = []
        self._router_patch = patch.object(
            qq_bot.agent_router, "handle_message_async",
            lambda text, target, *args, **kwargs: self.routed.append((text, target)),
        )
        self._router_patch.start()
        self.addCleanup(self._router_patch.stop)

    def _client(self, **kwargs):
        config = qq_bot.QQBotConfig(appid="10001", secret="s3cret", allowed_users="user1", **kwargs)
        client = qq_bot.QQBotClient(config)
        client._http = FakeHTTP()
        return client

    @staticmethod
    def _button_event(button_data="approve", scene="group", interaction_id="INT1", event_id="EV1",
                      member="user1", kind=11):
        payload = {
            "id": interaction_id,
            "type": kind,
            "scene": scene,
            "data": {"type": kind, "resolved": {"button_data": button_data, "button_id": button_data}},
        }
        if scene == "group":
            payload.update({"group_openid": "GROUP_A", "group_member_openid": member})
        elif scene == "c2c":
            payload.update({"user_openid": member})
        elif scene == "guild":
            payload.update({"channel_id": "CH1", "guild_id": "GU1"})
        return {"id": event_id, "op": 0, "t": "INTERACTION_CREATE", "d": payload}

    # ---------- 键盘数据结构 ----------
    def test_approval_keyboard_matches_official_schema(self):
        keyboard = qq_bot.approval_keyboard()
        rows = keyboard["content"]["rows"]

        self.assertEqual(len(rows), 1)
        buttons = rows[0]["buttons"]
        self.assertEqual([b["id"] for b in buttons], ["approve", "test", "cancel"])
        self.assertEqual([b["action"]["data"] for b in buttons], ["approve", "test", "cancel"])

        for button in buttons:
            self.assertEqual(button["action"]["type"], 1)                      # 回调按钮
            self.assertEqual(button["action"]["permission"]["type"], 2)         # 所有人可点
            self.assertTrue(button["action"]["unsupport_tips"])
            self.assertEqual(button["group_id"], "gi-agent-approval")           # 同组只能点一次
            self.assertLessEqual(len(button["render_data"]["label"]), 10)       # 官方限制 10 字符

    def test_execute_button_asks_for_confirmation(self):
        """✅ 执行 会真的拉起 BetterGI，所以要有二次确认（modal）。"""
        approve = qq_bot.approval_keyboard()["content"]["rows"][0]["buttons"][0]
        modal = approve["action"]["modal"]

        self.assertLessEqual(len(modal["content"]), 40)
        self.assertLessEqual(len(modal["confirm_text"]), 4)
        self.assertLessEqual(len(modal["cancel_text"]), 4)

    def test_button_commands_map_to_text(self):
        self.assertEqual(qq_bot.BUTTON_COMMANDS, {"approve": "y", "test": "t", "cancel": "取消"})

    def test_is_approval_message(self):
        self.assertTrue(qq_bot.is_approval_message("🛑 [系统拦截] 请确认是否执行上述计划？\n⚔️ 体力目标：无"))
        self.assertFalse(qq_bot.is_approval_message("今天体力怎么花？"))
        self.assertFalse(qq_bot.is_approval_message(""))

    # ---------- 发送 ----------
    async def test_send_message_with_keyboard_uses_markdown(self):
        http = FakeHTTP()
        client = self._client()
        client._http = http

        await client.send_message("qq:group:G1#M1", "🛑 待确认", keyboard=qq_bot.approval_keyboard())

        body = [item for item in http.requests if "/v2/groups/" in item[1]][0][2]
        self.assertEqual(body["msg_type"], 2)
        self.assertEqual(body["markdown"]["content"], "🛑 待确认")
        self.assertIn("keyboard", body)
        self.assertEqual(body["msg_id"], "M1")
        self.assertNotIn("content", body)     # 传了 markdown 就不能再传 content

    async def test_send_message_uses_event_id_for_interaction_replies(self):
        http = FakeHTTP()
        client = self._client()
        client._http = http

        await client.send_message(qq_bot.build_target("group", "G1", event_id="EV1"), "已执行")

        body = [item for item in http.requests if "/v2/groups/" in item[1]][0][2]
        self.assertEqual(body["event_id"], "EV1")
        self.assertNotIn("msg_id", body)

    async def test_send_message_falls_back_when_buttons_rejected(self):
        """平台没开通按钮权限 → 退回纯文本重发，并且本次运行不再白试。"""
        http = FakeHTTP(post_responses=[
            FakeResponse(400, {"message": "keyboard 无权限", "code": 304023}),
            FakeResponse(400, {"message": "keyboard 无权限", "code": 304023}),
            FakeResponse(200, {}),          # 退回纯文本后发成功
        ])
        client = self._client()
        client._http = http

        self.assertTrue(await client.send_message("qq:group:G1#M1", "🛑 待确认", keyboard=qq_bot.approval_keyboard()))

        bodies = [item[2] for item in http.requests if "/v2/groups/" in item[1]]
        self.assertEqual(len(bodies), 3)
        self.assertEqual(bodies[0]["msg_type"], 2)          # ① markdown + 按钮
        self.assertIn("keyboard", bodies[0])
        self.assertEqual(bodies[1]["msg_type"], 0)          # ② 纯文本 + 按钮
        self.assertIn("keyboard", bodies[1])
        self.assertNotIn("keyboard", bodies[2])             # ③ 纯文本
        self.assertFalse(client._buttons_enabled)           # 已记住"按钮不可用"

        # 第二条消息不再尝试按钮
        await client.send_message("qq:group:G1#M2", "🛑 待确认", keyboard=qq_bot.approval_keyboard())
        self.assertNotIn("keyboard", [item[2] for item in http.requests if "/v2/groups/" in item[1]][-1])

    async def test_buttons_survive_when_only_markdown_is_rejected(self):
        """只是群聊 markdown 不让发时，纯文本 + 按钮还是要保住按钮。"""
        http = FakeHTTP(post_responses=[
            FakeResponse(400, {"message": "markdown 不支持", "code": 304020}),
            FakeResponse(200, {}),
        ])
        client = self._client()
        client._http = http

        self.assertTrue(await client.send_message("qq:group:G1#M1", "🛑 待确认", keyboard=qq_bot.approval_keyboard()))

        bodies = [item[2] for item in http.requests if "/v2/groups/" in item[1]]
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[1]["msg_type"], 0)
        self.assertIn("keyboard", bodies[1])
        self.assertTrue(client._buttons_enabled)            # 按钮仍然可用

    def test_guild_messages_do_not_get_buttons(self):
        self.assertFalse(qq_bot.QQBotClient._can_have_buttons("guild"))
        self.assertTrue(qq_bot.QQBotClient._can_have_buttons("group"))
        self.assertTrue(qq_bot.QQBotClient._can_have_buttons("c2c"))

    async def test_buttons_disabled_by_config(self):
        client = self._client(buttons=False)
        keyboard = None
        if client.config.buttons and qq_bot.is_approval_message("🛑 [系统拦截] 请确认"):
            keyboard = qq_bot.approval_keyboard()
        self.assertIsNone(keyboard)

    async def test_approval_message_through_channel_router_gets_buttons(self):
        """端到端：Agent 发审批屏 → QQ 通道自动挂按钮。"""
        http = FakeHTTP()
        client = self._client()
        client._http = http
        channel_router.register_sender(channel_router.QQ_PREFIX, client.send_threadsafe)
        channel_router.register_formatter(channel_router.QQ_PREFIX, qq_bot.chat_text.to_plain_text)

        from api import feishu_api

        async def driver():
            client._loop = asyncio.get_running_loop()
            return await asyncio.to_thread(
                feishu_api.send_feishu_msg,
                "qq:group:G1#M1",
                "🛑 [系统拦截] 请确认是否执行上述计划？\n⚔️ 体力目标：秘源机兵·构型械",
            )

        self.assertTrue(await driver())

        body = [item for item in http.requests if "/v2/groups/" in item[1]][0][2]
        self.assertIn("keyboard", body)
        self.assertEqual(body["markdown"]["content"].splitlines()[0], "🛑 [系统拦截] 请确认是否执行上述计划？")

    # ---------- 互动事件 ----------
    async def test_button_click_is_acked_and_routed_as_command(self):
        http = FakeHTTP()
        client = self._client()
        client._http = http

        result = await client.handle_interaction(self._button_event("approve"))

        acks = [item for item in http.requests if item[0] == "PUT"]
        self.assertEqual(len(acks), 1)
        self.assertTrue(acks[0][1].endswith("/interactions/INT1"))
        self.assertEqual(acks[0][2], {"code": 0})
        self.assertEqual(acks[0][3]["Authorization"], "QQBot TOKEN1")
        self.assertEqual(self.routed, [("y", "qq:group:GROUP_A#event:EV1")])
        self.assertEqual(result["button"], "approve")

    async def test_all_three_buttons_route_to_their_text(self):
        for button_data, expected in (("approve", "y"), ("test", "t"), ("cancel", "取消")):
            with self.subTest(button=button_data):
                self.routed.clear()
                http = FakeHTTP()
                client = self._client()
                client._http = http
                await client.handle_interaction(self._button_event(button_data))
                self.assertEqual(self.routed, [(expected, "qq:group:GROUP_A#event:EV1")])

    async def test_c2c_button_click(self):
        client = self._client()
        client._http = FakeHTTP()

        await client.handle_interaction(self._button_event("test", scene="c2c"))

        self.assertEqual(self.routed, [("t", "qq:c2c:user1#event:EV1")])

    async def test_ack_happens_even_for_unknown_button(self):
        """不认识的按钮也要 ack（否则客户端一直转圈），但不能执行任何东西。"""
        http = FakeHTTP()
        client = self._client()
        client._http = http

        await client.handle_interaction(self._button_event("ghost"))

        self.assertEqual([item[2] for item in http.requests if item[0] == "PUT"], [{"code": 1}])
        self.assertEqual(self.routed, [])

    async def test_unauthorized_click_is_refused_but_acked(self):
        http = FakeHTTP()
        client = self._client()
        client._http = http
        refused = []
        client._on_message = lambda *args: None

        # ⚠️ 必须是"能 await 的发送"：这里在事件循环里，用 send_threadsafe 会自己等自己 30 秒
        async def fake_send(target, text, keyboard=None):
            refused.append((target, text))
            return True

        with patch.object(client, "send_message", fake_send):
            await client.handle_interaction(self._button_event("approve", member="stranger"))

        self.assertEqual([item[2] for item in http.requests if item[0] == "PUT"], [{"code": 0}])
        self.assertEqual(self.routed, [])                 # 不能执行
        self.assertEqual(len(refused), 1)                 # 但要告诉他为什么

    async def test_guild_scene_is_not_executed(self):
        """频道场景回不了被动消息 → 只能拒绝，绝不能静默执行。"""
        http = FakeHTTP()
        client = self._client()
        client._http = http

        result = await client.handle_interaction(self._button_event("approve", scene="guild"))

        self.assertEqual([item[2] for item in http.requests if item[0] == "PUT"], [{"code": 4}])
        self.assertEqual(self.routed, [])
        self.assertIsNone(result)

    async def test_clear_session_interaction_clears_memory(self):
        client = self._client()
        client._http = FakeHTTP()

        result = await client.handle_interaction(self._button_event(kind=14, scene="c2c"))

        self.assertEqual(self.routed, [("clear", "qq:c2c:user1#event:EV1")])
        self.assertEqual(result["kind"], "clear_session")

    async def test_feedback_and_other_interactions_are_ignored(self):
        client = self._client()
        http = FakeHTTP()
        client._http = http

        self.assertIsNone(await client.handle_interaction(self._button_event(kind=13)))
        self.assertEqual(http.requests, [])

    async def test_quick_menu_callback_is_acked_but_not_executed(self):
        """单聊快捷菜单（type=12）我们没配菜单项：要 ack，但不能执行任何东西。"""
        http = FakeHTTP()
        client = self._client()
        client._http = http

        result = await client.handle_interaction(self._button_event(kind=12))

        self.assertEqual([item[2] for item in http.requests if item[0] == "PUT"], [{"code": 0}])
        self.assertEqual(self.routed, [])
        self.assertIsNone(result)

    async def test_handle_event_ignores_interactions(self):
        """互动事件由 run_once 里 await handle_interaction 处理，不该走同步入口。"""
        client = self._client()
        self.assertIsNone(client.handle_event(self._button_event()))

    async def test_run_once_acks_and_routes_interaction_over_gateway(self):
        """真连一次本地假网关，确认互动事件这条链路是通的。"""
        async def handler(websocket):
            await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 60000}}))
            await websocket.recv()
            await websocket.send(json.dumps({
                "op": 0, "s": 3, "t": "INTERACTION_CREATE",
                "id": "EV1",
                "d": {
                    "id": "INT1", "type": 11, "scene": "group",
                    "group_openid": "GROUP_A", "group_member_openid": "user1",
                    "data": {"type": 11, "resolved": {"button_data": "approve"}},
                },
            }))
            await asyncio.sleep(0.05)
            await websocket.send(json.dumps({"op": 7}))

        async with ws_serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            def routes(request: httpx.Request) -> httpx.Response:
                if "getAppAccessToken" in str(request.url):
                    return httpx.Response(200, json={"access_token": "TOKEN1", "expires_in": "7200"})
                if "/interactions/" in str(request.url):
                    return httpx.Response(200, json={})
                return httpx.Response(200, json={"url": f"ws://127.0.0.1:{port}"})

            calls = []

            def transport_handler(request: httpx.Request) -> httpx.Response:
                calls.append((request.method, str(request.url)))
                return routes(request)

            config = qq_bot.QQBotConfig(appid="10001", secret="s3cret", allowed_users="user1")
            client = qq_bot.QQBotClient(config)
            client._http = httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))
            await asyncio.wait_for(client.run_once(), timeout=10)
            await client.aclose()

        self.assertIn(("PUT", "https://api.sgroup.qq.com/interactions/INT1"), calls)
        self.assertEqual(self.routed, [("y", "qq:group:GROUP_A#event:EV1")])


class ConfigTests(unittest.TestCase):
    def test_default_intents_cover_group_c2c_and_guild(self):
        self.assertEqual(
            qq_bot.DEFAULT_INTENTS,
            qq_bot.INTENT_GROUP_AND_C2C | qq_bot.INTENT_PUBLIC_GUILD_MESSAGES
            | qq_bot.INTENT_DIRECT_MESSAGE | qq_bot.INTENT_INTERACTION,
        )
        self.assertEqual(qq_bot.INTENT_GROUP_AND_C2C, 1 << 25)
        self.assertEqual(qq_bot.INTENT_INTERACTION, 1 << 26)   # 按钮点击事件要用
        self.assertEqual(qq_bot.DEFAULT_INTENTS, 1174409216)

    def test_from_env(self):
        env = {
            "QQ_BOT_APPID": "10001",
            "QQ_BOT_SECRET": "abc",
            "QQ_BOT_ALLOWED_USERS": "u1, u2，u3",
            "QQ_BOT_ALLOW_ANYONE": "1",
            "QQ_BOT_INTENTS": "33554432",
            "QQ_BOT_RECONNECT_SECONDS": "3",
        }
        with patch.dict("os.environ", env, clear=False):
            config = qq_bot.QQBotConfig.from_env()

        self.assertTrue(config.configured)
        self.assertEqual(config.allowed_users, ["u1", "u2", "u3"])
        self.assertTrue(config.allow_anyone)
        self.assertEqual(config.intents, 33554432)
        self.assertEqual(config.reconnect_delay, 3.0)
        self.assertIn("u1", config.describe())

    def test_from_env_defaults(self):
        # 把所有 QQ 变量都清空：不能依赖本机 .env（玩家填了真配置就会影响断言）
        empty = {name: "" for name in (
            "QQ_BOT_APPID", "QQ_BOT_SECRET", "QQ_BOT_INTENTS", "QQ_BOT_ALLOWED_USERS",
            "QQ_BOT_ALLOW_ANYONE", "QQ_BOT_TOKEN_URL", "QQ_BOT_API_BASE", "QQ_BOT_WS_URL",
            "QQ_BOT_REPLY_MODE",
        )}
        with patch.dict("os.environ", empty, clear=False):
            config = qq_bot.QQBotConfig.from_env()

        self.assertFalse(config.configured)
        self.assertEqual(config.intents, qq_bot.DEFAULT_INTENTS)
        self.assertEqual(config.token_url, qq_bot.DEFAULT_TOKEN_URL)
        self.assertEqual(config.ws_url, qq_bot.DEFAULT_WS_URL)
        self.assertIn("白名单", config.describe())
        self.assertIn("未设置", config.describe())

    def test_bad_intents_falls_back_to_default(self):
        with patch.dict("os.environ", {"QQ_BOT_INTENTS": "abc"}, clear=False):
            config = qq_bot.QQBotConfig.from_env()
        self.assertEqual(config.intents, qq_bot.DEFAULT_INTENTS)

    def test_allow_anyone_only_when_exactly_one(self):
        with patch.dict("os.environ", {"QQ_BOT_ALLOW_ANYONE": "true"}, clear=False):
            self.assertFalse(qq_bot.QQBotConfig.from_env().allow_anyone)
        with patch.dict("os.environ", {"QQ_BOT_ALLOW_ANYONE": "1"}, clear=False):
            self.assertTrue(qq_bot.QQBotConfig.from_env().allow_anyone)

    def test_reply_mode_defaults_to_compact(self):
        with patch.dict("os.environ", {"QQ_BOT_REPLY_MODE": ""}, clear=False):
            config = qq_bot.QQBotConfig.from_env()

        self.assertEqual(config.reply_mode, "compact")
        self.assertTrue(config.concise)

    def test_reply_mode_can_be_full(self):
        for raw in ("full", "FULL", " full "):
            with self.subTest(raw=raw):
                with patch.dict("os.environ", {"QQ_BOT_REPLY_MODE": raw}, clear=False):
                    config = qq_bot.QQBotConfig.from_env()
                self.assertEqual(config.reply_mode, "full")
                self.assertFalse(config.concise)

    def test_unknown_reply_mode_falls_back_to_compact(self):
        with patch.dict("os.environ", {"QQ_BOT_REPLY_MODE": "verbose"}, clear=False):
            self.assertTrue(qq_bot.QQBotConfig.from_env().concise)


class TextTests(unittest.TestCase):
    def test_strip_mentions(self):
        self.assertEqual(qq_bot.strip_mentions("<@!1234> 帮我锄大地"), "帮我锄大地")
        self.assertEqual(qq_bot.strip_mentions("<@1234>锄大地"), "锄大地")
        self.assertEqual(qq_bot.strip_mentions("锄大地"), "锄大地")
        self.assertEqual(qq_bot.strip_mentions("<@!1><@!2> 两个提及"), "两个提及")
        # 别把正文一起吃掉（旧实现的坑）
        self.assertEqual(qq_bot.strip_mentions("<@!1234> a > b"), "a > b")

    def test_split_message(self):
        self.assertEqual(qq_bot.split_message(""), [])
        self.assertEqual(qq_bot.split_message("  hi  "), ["hi"])
        self.assertEqual(qq_bot.split_message("短消息", limit=10), ["短消息"])

        long_text = "\n".join(f"第{i}行" + "内容" * 30 for i in range(30))
        chunks = qq_bot.split_message(long_text, limit=200, max_chunks=99)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 200)
            self.assertTrue(chunk)
        # 分割不丢字（忽略切段时丢掉的空白）
        self.assertEqual("".join(chunks).replace("\n", ""), long_text.replace("\n", ""))

    def test_split_message_hard_cut_without_break_chars(self):
        chunks = qq_bot.split_message("x" * 250, limit=100)
        self.assertEqual([len(c) for c in chunks], [100, 100, 50])

    def test_split_message_prefers_newlines_over_later_punctuation(self):
        """按段落切，别为了"多塞一点"把某一行劈成两半（玩家实测的槽点）。"""
        text = "第一段" + "内容" * 40 + "。\n" + "第二段" + "内容" * 40 + "。"
        chunks = qq_bot.split_message(text, limit=len(text) - 5)

        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].startswith("第一段"))
        self.assertTrue(chunks[1].startswith("第二段"))

    def test_split_message_caps_the_number_of_chunks(self):
        """超长回复别刷屏：最多 4 条，并说明剩下的在电脑端。"""
        chunks = qq_bot.split_message("行\n" * 2000, limit=100)

        self.assertEqual(len(chunks), qq_bot.MAX_CHUNKS)
        self.assertIn("完整内容见电脑端日志", chunks[-1])

    def test_split_message_within_cap_is_not_truncated(self):
        chunks = qq_bot.split_message("行\n" * 30, limit=100)

        self.assertLess(len(chunks), qq_bot.MAX_CHUNKS)
        self.assertNotIn("完整内容见电脑端日志", chunks[-1])


class TargetTests(unittest.TestCase):
    def test_build_and_parse_round_trip(self):
        for kind, cid in (("group", "GROUP_A"), ("c2c", "USER_A"), ("guild", "CHAN_A")):
            target = qq_bot.build_target(kind, cid, "MSG1")
            self.assertEqual(target, f"qq:{kind}:{cid}#MSG1")
            info = qq_bot.parse_target(target)
            self.assertEqual(info, {"kind": kind, "id": cid, "msg_id": "MSG1", "event_id": ""})

    def test_event_credential_round_trip(self):
        """按钮点击的回复要用 event_id（官方要求 msg_id / event_id 二选一）。"""
        target = qq_bot.build_target("group", "G1", event_id="EV1")

        self.assertEqual(target, "qq:group:G1#event:EV1")
        self.assertEqual(
            qq_bot.parse_target(target),
            {"kind": "group", "id": "G1", "msg_id": "", "event_id": "EV1"},
        )

    def test_build_target_without_msg_id(self):
        self.assertEqual(qq_bot.build_target("group", "G1"), "qq:group:G1")

    def test_parse_target_rejects_foreign_or_broken_targets(self):
        for bad in ("ou_123", "", "qq:GROUP_A", "qq::x"):
            with self.assertRaises(qq_bot.QQBotError):
                qq_bot.parse_target(bad)

    def test_reply_endpoints(self):
        client = make_client()
        self.assertEqual(client._reply_endpoint("group", "G1"), "https://api.sgroup.qq.com/v2/groups/G1/messages")
        self.assertEqual(client._reply_endpoint("c2c", "U1"), "https://api.sgroup.qq.com/v2/users/U1/messages")
        self.assertEqual(client._reply_endpoint("guild", "C1"), "https://api.sgroup.qq.com/channels/C1/messages")
        with self.assertRaises(qq_bot.QQBotError):
            client._reply_endpoint("telegram", "X1")


class TokenAndGatewayTests(unittest.TestCase):
    def test_fetch_access_token_posts_appid_and_secret(self):
        http = FakeHTTP()
        client = make_client(http)

        token = asyncio.run(client.fetch_access_token())

        self.assertEqual(token, "TOKEN1")
        method, url, body, _headers = http.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, qq_bot.DEFAULT_TOKEN_URL)
        self.assertEqual(body, {"appId": "10001", "clientSecret": "s3cret"})

    def test_fetch_access_token_is_cached_until_expiry(self):
        http = FakeHTTP()
        client = make_client(http)

        asyncio.run(client.fetch_access_token())
        asyncio.run(client.fetch_access_token())
        self.assertEqual(http.token_calls, 1)

        asyncio.run(client.fetch_access_token(force=True))
        self.assertEqual(http.token_calls, 2)

    def test_fetch_access_token_errors(self):
        client = make_client(FakeHTTP(token_payload=FakeResponse(401, None, "unauthorized")))
        with self.assertRaises(qq_bot.QQBotError):
            asyncio.run(client.fetch_access_token())

        client = make_client(FakeHTTP(token_payload={"expires_in": 7200}))
        with self.assertRaises(qq_bot.QQBotError):
            asyncio.run(client.fetch_access_token())

    def test_fetch_access_token_requires_config(self):
        client = qq_bot.QQBotClient(qq_bot.QQBotConfig())
        client._http = FakeHTTP()
        with self.assertRaises(qq_bot.QQBotError):
            asyncio.run(client.fetch_access_token())

    def test_request_gateway_uses_official_gateway_path(self):
        """官方 v2 的取网关接口是 GET /gateway。

        曾经的线上事故：误用 `/websocket/`（那是 WebSocket 入口本身），普通 GET 被网关
        挡回来 —— `HTTP 426 WebSocket protocol violation: Upgrade header "" does not contain websocket`。
        """
        http = FakeHTTP()
        client = make_client(http)

        url = asyncio.run(client.request_gateway())

        self.assertEqual(url, "wss://api.sgroup.qq.com/websocket/")
        method, request_url, _body, headers = http.requests[-1]
        self.assertEqual(method, "GET")
        self.assertTrue(request_url.endswith("/gateway"), request_url)
        self.assertNotIn("/websocket", request_url)
        self.assertEqual(headers["Authorization"], "QQBot TOKEN1")

    def test_request_gateway_accepts_legacy_path_as_second_try(self):
        """万一 /gateway 不可用，旧的 /websocket/ 也给一次机会。"""
        class PathAware(FakeHTTP):
            async def get(self, url, headers=None):
                self.requests.append(("GET", url, None, headers))
                if url.endswith("/gateway"):
                    return FakeResponse(404, {"message": "不支持的调用"})
                return FakeResponse(200, {"url": "wss://legacy.example/ws"})

        client = make_client(PathAware())
        self.assertEqual(asyncio.run(client.request_gateway()), "wss://legacy.example/ws")

    def test_request_gateway_falls_back_to_direct_url_when_426(self):
        """两个路径都拿不到时直接退回官方 WSS 地址（连的时候带 Authorization 头）。

        这就是玩家实测那个 426 的现场：token 是好的，只是取地址这一步被挡了。
        """
        class Blocked(FakeHTTP):
            async def get(self, url, headers=None):
                self.requests.append(("GET", url, None, headers))
                return FakeResponse(426, None, 'WebSocket protocol violation: Upgrade header "" does not contain websocket')

        http = Blocked()
        client = make_client(http)

        url = asyncio.run(client.request_gateway())

        self.assertEqual(url, qq_bot.DEFAULT_WS_URL)
        self.assertEqual([item[1] for item in http.requests if item[0] == "GET"],
                         [f"{qq_bot.DEFAULT_API_BASE}{p}" for p in qq_bot.GATEWAY_PATHS])

    def test_request_gateway_falls_back_on_bad_payloads(self):
        client = make_client(FakeHTTP(gateway_payload=FakeResponse(500, None, "boom")))
        self.assertEqual(asyncio.run(client.request_gateway()), qq_bot.DEFAULT_WS_URL)

        client = make_client(FakeHTTP(gateway_payload={}))
        self.assertEqual(asyncio.run(client.request_gateway()), qq_bot.DEFAULT_WS_URL)

    def test_connect_kwargs_carry_authorization_header(self):
        client = make_client(FakeHTTP())

        asyncio.run(client.fetch_access_token())
        kwargs = client._connect_kwargs()

        self.assertEqual(kwargs["ping_interval"], None)
        self.assertEqual(kwargs["max_size"], 2 ** 22)
        headers = kwargs.get("additional_headers") or kwargs.get("extra_headers")
        self.assertEqual(headers, {"Authorization": "QQBot TOKEN1"})
        self.assertEqual(qq_bot._header_kwarg(lambda **kwargs: None), "")
        self.assertEqual(qq_bot._header_kwarg(qq_bot.ws_connect), "additional_headers")


class SendMessageTests(unittest.TestCase):
    def test_send_message_group_passive_reply(self):
        http = FakeHTTP()
        client = make_client(http)

        ok = asyncio.run(client.send_message("qq:group:GROUP_A#MSG1", "好的"))

        self.assertTrue(ok)
        method, url, body, headers = http.requests[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.sgroup.qq.com/v2/groups/GROUP_A/messages")
        self.assertEqual(body, {"content": "好的", "msg_type": 0, "msg_seq": 1, "msg_id": "MSG1"})
        self.assertEqual(headers["Authorization"], "QQBot TOKEN1")
        self.assertEqual(headers["X-Union-Appid"], "10001")

    def test_send_message_multi_chunk_increments_msg_seq(self):
        http = FakeHTTP()
        client = make_client(http)
        long_text = "\n".join("行" * 100 for _ in range(20))

        asyncio.run(client.send_message("qq:c2c:USER_A#MSG2", long_text))

        posts = [item for item in http.requests if "/v2/users/" in item[1]]
        self.assertGreater(len(posts), 1)
        self.assertEqual([item[2]["msg_seq"] for item in posts], list(range(1, len(posts) + 1)))
        self.assertTrue(all(item[2]["msg_id"] == "MSG2" for item in posts))

    def test_send_message_without_msg_id_omits_field(self):
        http = FakeHTTP()
        client = make_client(http)
        asyncio.run(client.send_message("qq:group:GROUP_A", "hi"))
        self.assertNotIn("msg_id", http.requests[-1][2])

    def test_send_message_empty_text_does_nothing(self):
        http = FakeHTTP()
        client = make_client(http)
        self.assertTrue(asyncio.run(client.send_message("qq:group:GROUP_A#M1", "   ")))
        self.assertEqual([r for r in http.requests if r[0] == "POST" and "getAppAccessToken" not in r[1]], [])

    def test_send_message_reports_failure_status(self):
        http = FakeHTTP(post_responses=[FakeResponse(500, None, "err")])
        client = make_client(http)
        self.assertFalse(asyncio.run(client.send_message("qq:group:G1#M1", "hi")))

    def test_send_threadsafe_without_loop_is_false(self):
        client = make_client(FakeHTTP())
        self.assertFalse(client.send_threadsafe("qq:group:G1#M1", "hi"))

    def test_registered_formatter_cleans_text_on_the_way_to_qq(self):
        """端到端：Agent 发原文 → 通道分发 → 纯文本化 → QQ 请求体里没有 ```json 块。"""
        channel_router.clear()
        self.addCleanup(channel_router.clear)

        http = FakeHTTP()
        client = make_client(http)
        channel_router.register_sender(channel_router.QQ_PREFIX, client.send_threadsafe)
        channel_router.register_formatter(channel_router.QQ_PREFIX, qq_bot.chat_text.to_plain_text)

        from api import feishu_api

        async def driver():
            client._loop = asyncio.get_running_loop()
            # send_threadsafe 是给"别的线程"用的，所以这里也丢到线程里调（否则会阻塞事件循环）
            return await asyncio.to_thread(
                feishu_api.send_feishu_msg,
                "qq:group:G1#M1",
                "### 🎯 今日主攻目标\n**蓝砚**\n```json\n{\"energy_task\": {}}\n```\n👉 回复 'y'",
            )

        self.assertTrue(asyncio.run(driver()))

        body = [item for item in http.requests if "/v2/groups/" in item[1]][0][2]["content"]
        self.assertIn("今日主攻目标", body)
        self.assertIn("蓝砚", body)
        self.assertNotIn("energy_task", body)
        self.assertNotIn("**", body)


    def test_send_threadsafe_from_loop_thread_does_not_self_wait(self):
        """在事件循环线程里调 send_threadsafe 不能自己等自己（实测过：等满 30 秒，消息发不出去）。"""

        async def driver():
            client = make_client(FakeHTTP())
            sent = []

            async def fake_send(target, text, keyboard=None):
                sent.append((target, text))
                return True

            client.send_message = fake_send
            client._loop = asyncio.get_running_loop()

            started = time.time()
            ok = client.send_threadsafe("qq:c2c:U1#M1", "你好")   # 就在循环线程里调
            elapsed = time.time() - started
            await asyncio.sleep(0.05)                            # 让排队的协程真的跑起来
            return ok, elapsed, sent

        ok, elapsed, sent = asyncio.run(driver())

        self.assertTrue(ok)
        self.assertLess(elapsed, 1.0, "send_threadsafe 又在自己等自己了")
        self.assertEqual(len(sent), 1)

    def test_send_soon_reports_failure_without_raising(self):
        async def driver():
            client = make_client(FakeHTTP())

            async def boom(target, text, keyboard=None):
                raise qq_bot.QQBotError("网络炸了")

            client.send_message = boom
            client._loop = asyncio.get_running_loop()
            ok = client.send_soon("qq:c2c:U1#M1", "你好")
            await asyncio.sleep(0.05)          # 失败由 done-callback 打成日志，不该抛出去
            return ok

        self.assertTrue(asyncio.run(driver()))

    def test_send_soon_without_loop_is_false(self):
        client = make_client(FakeHTTP())
        client._loop = None
        self.assertFalse(client.send_soon("qq:c2c:U1#M1", "你好"))


class EventLoopSafetyTests(unittest.IsolatedAsyncioTestCase):
    """事件循环线程里绝不能调会阻塞的发送（白名单提示曾经 100% 丢失 + 卡 30 秒）。"""

    def setUp(self):
        channel_router.clear()
        self.addCleanup(channel_router.clear)

    async def test_unauthorized_message_replies_without_blocking(self):
        client = make_client(FakeHTTP())          # 白名单为空 = 谁都不许用
        sent = []

        async def fake_send(target, text, keyboard=None):
            sent.append((target, text))
            return True

        client.send_message = fake_send
        client._loop = asyncio.get_running_loop()

        started = time.time()
        result = client.handle_event(group_event())
        elapsed = time.time() - started
        await asyncio.sleep(0.05)

        self.assertEqual(result["allowed"], "0")
        self.assertLess(elapsed, 1.0, "handle_event 又阻塞事件循环了")
        self.assertEqual(len(sent), 1, "白名单提示必须真的发出去")
        self.assertIn("QQ_BOT_ALLOWED_USERS", sent[0][1])

    async def test_unauthorized_button_click_replies(self):
        client = make_client(FakeHTTP())
        sent = []

        async def fake_send(target, text, keyboard=None):
            sent.append((target, text))
            return True

        client.send_message = fake_send
        client._loop = asyncio.get_running_loop()

        result = await client.handle_interaction(self._button_event("approve", member="stranger"))

        self.assertEqual(result["allowed"], "0")
        self.assertEqual(len(sent), 1)
        self.assertIn("白名单", sent[0][1])

    @staticmethod
    def _button_event(button_data="approve", member="stranger"):
        return {
            "id": "EV1", "op": 0, "t": "INTERACTION_CREATE",
            "d": {
                "id": "INT1", "type": 11, "scene": "group",
                "group_openid": "GROUP_A", "group_member_openid": member,
                "data": {"type": 11, "resolved": {"button_data": button_data}},
            },
        }


class EventTests(unittest.TestCase):
    def setUp(self):
        channel_router.clear()
        self.received = []

    def tearDown(self):
        channel_router.clear()

    def _client(self, **kwargs):
        def on_message(text, target, user_id, kind):
            self.received.append((text, target, user_id, kind))

        config = qq_bot.QQBotConfig(appid="10001", secret="s3cret", **kwargs)
        client = qq_bot.QQBotClient(config, on_message=on_message)
        client._http = FakeHTTP()
        return client

    def test_group_event(self):
        client = self._client(allowed_users="user1")

        result = client.handle_event(group_event())

        self.assertEqual(result, {
            "text": "帮我刷绝缘本",
            "target": "qq:group:GROUP_A#MSG1",
            "user_id": "user1",
            "allowed": "1",
        })
        self.assertEqual(self.received, [("帮我刷绝缘本", "qq:group:GROUP_A#MSG1", "user1", "GROUP_AT_MESSAGE_CREATE")])

    def test_c2c_event(self):
        client = self._client(allowed_users="USER_A")
        event = {"t": "C2C_MESSAGE_CREATE", "d": {"id": "M9", "content": "今天体力怎么花", "author": {"user_openid": "USER_A"}}}

        result = client.handle_event(event)

        self.assertEqual(result["target"], "qq:c2c:USER_A#M9")
        self.assertEqual(result["allowed"], "1")

    def test_guild_events(self):
        for event_type in ("AT_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"):
            with self.subTest(event_type=event_type):
                self.received.clear()
                client = self._client(allowed_users="U9")
                event = {
                    "t": event_type,
                    "d": {"id": "M5", "content": "<@!1> 跑一条龙", "channel_id": "CH1", "author": {"id": "U9"}},
                }

                result = client.handle_event(event)

                self.assertEqual(result["target"], "qq:guild:CH1#M5")
                self.assertEqual(result["text"], "跑一条龙")
                self.assertEqual(result["allowed"], "1")

    def test_unauthorized_user_is_refused(self):
        """默认不设白名单 = 谁都不能指挥 Agent（避免公网机器人被人乱开游戏）。"""
        client = self._client()

        result = client.handle_event(group_event())

        self.assertEqual(result["allowed"], "0")
        self.assertEqual(self.received, [])

    def test_whitelist_mismatch_is_refused(self):
        client = self._client(allowed_users="someone-else")
        self.assertEqual(client.handle_event(group_event())["allowed"], "0")

    def test_allow_anyone_accepts_stranger(self):
        client = self._client(allow_anyone=True)
        self.assertEqual(client.handle_event(group_event())["allowed"], "1")

    def test_unknown_or_empty_events_are_ignored(self):
        client = self._client(allow_anyone=True)
        self.assertIsNone(client.handle_event({"t": "SOMETHING_ELSE", "d": {}}))
        self.assertIsNone(client.handle_event({"t": "GROUP_AT_MESSAGE_CREATE", "d": {"id": "M", "content": "<@!1>", "group_openid": "G", "author": {"member_openid": "u"}}}))

    def test_default_on_message_goes_through_agent_router(self):
        """没传 on_message 时，消息交给共用路由（这里只验证调用发生，不真跑大模型）。"""
        client = make_client(FakeHTTP(), allowed_users="user1")
        calls = []

        with patch.object(qq_bot.agent_router, "handle_message_async", lambda text, target, *a, **k: calls.append((text, target))):
            client.handle_event(group_event())

        self.assertEqual(calls, [("帮我刷绝缘本", "qq:group:GROUP_A#MSG1")])


class CheckTests(unittest.TestCase):
    def test_check_without_config(self):
        client = qq_bot.QQBotClient(qq_bot.QQBotConfig())
        self.assertFalse(asyncio.run(client.check()))

    def test_check_connects_to_gateway(self):
        http = FakeHTTP()
        client = make_client(http)
        probed = []

        async def probe(_self, url=""):
            probed.append(url)
            return '{"op": 10}'

        with patch.object(qq_bot.QQBotClient, "probe_gateway", probe):
            self.assertTrue(asyncio.run(client.check()))

        self.assertEqual(probed, ["wss://api.sgroup.qq.com/websocket/"])
        self.assertTrue(http.closed)

    def test_check_reports_bad_secret(self):
        client = make_client(FakeHTTP(token_payload=FakeResponse(403, None, "invalid appid or secret")))
        self.assertFalse(asyncio.run(client.check()))

    def test_check_fails_when_gateway_never_says_hello(self):
        client = make_client(FakeHTTP())

        async def probe(_self, url=""):
            raise qq_bot.QQBotError("网关没有按预期先发 HELLO")

        with patch.object(qq_bot.QQBotClient, "probe_gateway", probe):
            self.assertFalse(asyncio.run(client.check()))

    def test_main_without_config_returns_1(self):
        with patch.dict("os.environ", {"QQ_BOT_APPID": "", "QQ_BOT_SECRET": ""}, clear=False):
            self.assertEqual(qq_bot.main(["--check"]), 1)

    def test_main_guards_against_non_utf8_console(self):
        """控制台是 GBK / 输出被重定向时，print emoji 不能把机器人搞挂。"""
        calls = []
        with patch.object(qq_bot, "_ensure_utf8_output", lambda: calls.append(1)):
            with patch.dict("os.environ", {"QQ_BOT_APPID": "", "QQ_BOT_SECRET": ""}, clear=False):
                qq_bot.main(["--check"])
        self.assertEqual(calls, [1])

    def test_ensure_utf8_output_survives_odd_streams(self):
        class Broken:
            encoding = "gbk"

            def reconfigure(self, **kwargs):
                raise OSError("不支持的流")

        with patch.object(qq_bot.sys, "stdout", Broken()):
            qq_bot._ensure_utf8_output()   # 不抛异常即可

    def test_main_check_uses_config(self):
        with patch.dict("os.environ", {"QQ_BOT_APPID": "10001", "QQ_BOT_SECRET": "s"}, clear=False):
            http = FakeHTTP()
            real_client = qq_bot.QQBotClient

            def factory(config, *args, **kwargs):
                client = real_client(config, *args, **kwargs)
                client._http = http
                return client

            async def probe(_self, url=""):
                return '{"op": 10}'

            with patch.object(qq_bot, "QQBotClient", factory), patch.object(
                real_client, "probe_gateway", probe
            ):
                self.assertEqual(qq_bot.main(["--check"]), 0)
            self.assertEqual(http.token_calls, 1)


class GatewayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """真的连一次 WebSocket：本地假网关 + httpx MockTransport（不碰外网）。

    覆盖 run_once() 的握手分支：op=10 HELLO → 发 op=2 IDENTIFY → op=0 事件 → op=7 要求重连收工。
    """

    async def _client_against(self, gateway_url, on_message):
        def handler(request: httpx.Request) -> httpx.Response:
            if "getAppAccessToken" in str(request.url):
                return httpx.Response(200, json={"access_token": "TOKEN1", "expires_in": "7200"})
            if str(request.url).endswith("/websocket/"):
                return httpx.Response(200, json={"url": gateway_url})
            return httpx.Response(404)

        config = qq_bot.QQBotConfig(appid="10001", secret="s3cret", allowed_users="user1")
        client = qq_bot.QQBotClient(config, on_message=on_message)
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client

    async def test_identify_then_event_then_reconnect(self):
        seen = []
        handshake = {}

        def on_message(text, target, user_id, kind):
            seen.append((text, target, user_id, kind))

        async def handler(websocket):
            await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 60000}}))
            handshake.update(json.loads(await websocket.recv()))
            await websocket.send(json.dumps({
                "op": 0,
                "s": 7,
                "t": "GROUP_AT_MESSAGE_CREATE",
                "d": {
                    "id": "MSG1",
                    "content": "<@!1> 今天体力怎么花",
                    "group_openid": "GROUP_A",
                    "author": {"member_openid": "user1"},
                },
            }))
            await asyncio.sleep(0.05)          # 让客户端把事件派发出去
            await websocket.send(json.dumps({"op": 7}))   # 服务端要求重连 → run_once 返回

        async with ws_serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = await self._client_against(f"ws://127.0.0.1:{port}", on_message)
            await asyncio.wait_for(client.run_once(), timeout=10)
            self.assertEqual(client._seq, 7)
            await client.aclose()

        self.assertEqual(handshake["op"], 2)
        self.assertEqual(handshake["d"]["token"], "QQBot TOKEN1")
        self.assertEqual(handshake["d"]["intents"], qq_bot.DEFAULT_INTENTS)
        self.assertEqual(handshake["d"]["shard"], [0, 1])
        self.assertIn(("今天体力怎么花", "qq:group:GROUP_A#MSG1", "user1", "GROUP_AT_MESSAGE_CREATE"), seen)

    async def test_run_forever_registers_channel_sender(self):
        """启动时把自己注册进通道表（发送 + 纯文本化 + 精简审批），Agent 的回复才能找到 QQ 通道。"""
        channel_router.clear()
        self.addCleanup(channel_router.clear)

        async def handler(websocket):
            await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 60000}}))
            await websocket.recv()
            await websocket.send(json.dumps({"op": 7}))

        async with ws_serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = await self._client_against(f"ws://127.0.0.1:{port}", lambda *args: None)
            client.config.reconnect_delay = 30        # 断开后睡久一点，别立刻重连
            task = asyncio.create_task(client.run_forever())
            for _ in range(100):                     # 等到注册完成
                if channel_router.QQ_PREFIX in channel_router.registered_prefixes():
                    break
                await asyncio.sleep(0.02)
            registered = channel_router.QQ_PREFIX in channel_router.registered_prefixes()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            sender = channel_router.sender_for("qq:group:G1#M1")
            formatter = channel_router.formatter_for("qq:group:G1#M1")
            concise = channel_router.wants_concise("qq:group:G1#M1")
            quiet = channel_router.wants_quiet("qq:group:G1#M1")
            await client.aclose()

        self.assertTrue(registered)
        self.assertEqual(sender, client.send_threadsafe)
        self.assertEqual(formatter, qq_bot.chat_text.to_plain_text)
        self.assertTrue(concise)
        self.assertTrue(quiet)      # 进度消息不发（否则吃光"每条消息最多回 5 次"的额度）


    async def test_run_once_falls_back_to_direct_url_when_gateway_api_blocked(self):
        """REST 取地址被 426 挡住时，也要能直连官方 WSS 地址把 IDENTIFY 发出去。

        （玩家现场：`HTTP 426 WebSocket protocol violation: Upgrade header "" does not contain websocket`）
        """
        channel_router.clear()
        self.addCleanup(channel_router.clear)
        handshake = {}

        async def handler(websocket):
            await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 60000}}))
            handshake.update(json.loads(await websocket.recv()))
            await websocket.send(json.dumps({"op": 7}))

        async with ws_serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            def blocked(request: httpx.Request) -> httpx.Response:
                if "getAppAccessToken" in str(request.url):
                    return httpx.Response(200, json={"access_token": "TOKEN1", "expires_in": "7200"})
                return httpx.Response(426, text='WebSocket protocol violation: Upgrade header "" does not contain websocket')

            config = qq_bot.QQBotConfig(
                appid="10001", secret="s3cret", ws_url=f"ws://127.0.0.1:{port}"
            )
            client = qq_bot.QQBotClient(config, on_message=lambda *args: None)
            client._http = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
            await asyncio.wait_for(client.run_once(), timeout=10)
            await client.aclose()

        self.assertEqual(handshake["op"], 2)
        self.assertEqual(handshake["d"]["token"], "QQBot TOKEN1")

    async def test_check_probes_the_gateway(self):
        """--check 会真连一次网关（取到 HELLO 才算通过）。"""
        async def handler(websocket):
            await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 60000}}))
            await websocket.close()

        async with ws_serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            def routes(request: httpx.Request) -> httpx.Response:
                if "getAppAccessToken" in str(request.url):
                    return httpx.Response(200, json={"access_token": "TOKEN1", "expires_in": "7200"})
                return httpx.Response(426, text="blocked")

            config = qq_bot.QQBotConfig(appid="10001", secret="s3cret", ws_url=f"ws://127.0.0.1:{port}")
            client = qq_bot.QQBotClient(config)
            client._http = httpx.AsyncClient(transport=httpx.MockTransport(routes))
            ok = await asyncio.wait_for(client.check(), timeout=10)

        self.assertTrue(ok)

    async def test_probe_gateway_rejects_silent_gateway(self):
        """网关第一帧不是 HELLO 就要报错，别假装成功。"""
        async def handler(websocket):
            await websocket.send(json.dumps({"op": 0, "t": "GROUP_AT_MESSAGE_CREATE"}))

        async with ws_serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = qq_bot.QQBotClient(
                qq_bot.QQBotConfig(appid="10001", secret="s3cret", ws_url=f"ws://127.0.0.1:{port}")
            )
            client._http = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"access_token": "T", "expires_in": "7200"})
                )
            )
            with self.assertRaises(qq_bot.QQBotError):
                await asyncio.wait_for(client.probe_gateway(), timeout=10)
            await client.aclose()


class SingleInstanceTests(unittest.TestCase):
    """同一个 AppID 只能跑一个进程（两个一起连网关会互相抢会话）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patch = patch.object(
            qq_bot, "lock_path", lambda: os.path.join(self.tmp.name, "qq_bot.lock")
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        qq_bot.release_single_instance()
        self.addCleanup(qq_bot.release_single_instance)

    def test_second_instance_is_refused(self):
        self.assertTrue(qq_bot.acquire_single_instance("10001"))
        self.assertFalse(qq_bot.acquire_single_instance("10001"))

    def test_release_allows_another_instance(self):
        self.assertTrue(qq_bot.acquire_single_instance("10001"))
        qq_bot.release_single_instance()
        self.assertTrue(qq_bot.acquire_single_instance("10001"))

    def test_force_skips_the_check(self):
        self.assertTrue(qq_bot.acquire_single_instance("10001"))
        self.assertTrue(qq_bot.acquire_single_instance("10001", force=True))

    def test_lock_file_records_pid(self):
        qq_bot.acquire_single_instance("10001")
        # 锁住的字节区间只有持锁者能读（Windows 语义），这里就从自己的句柄读
        os.lseek(qq_bot._LOCK_HANDLE, 0, os.SEEK_SET)
        content = os.read(qq_bot._LOCK_HANDLE, 128).decode("utf-8")

        self.assertIn(f"pid={os.getpid()}", content)
        self.assertIn("appid=10001", content)

    def test_locked_region_is_protected_from_other_handles(self):
        """锁生效的直接证据：别的句柄读不了被锁的区间。"""
        path = os.path.join(self.tmp.name, "qq_bot.lock")
        qq_bot.acquire_single_instance("10001")

        with open(path, "rb") as handle:
            with self.assertRaises(PermissionError):
                handle.read()

    def test_unwritable_lock_location_does_not_block_startup(self):
        with patch.object(qq_bot, "lock_path", lambda: os.path.join(self.tmp.name, "nope", "\x00bad")):
            self.assertTrue(qq_bot.acquire_single_instance("10001"))


class ReplySeqTests(unittest.IsolatedAsyncioTestCase):
    """msg_seq 记账。

    玩家实测事故：一次消息触发了"🚫 计划已撤销" + "新的审批屏"两条回复，两条都用 msg_seq=1，
    第二条被平台判重：`HTTP 400 {"message":"消息被去重，请检查请求msgseq","code":40054005}`。
    官方规则是 `msg_id + msg_seq` 不能重复，且同一条消息最多回复 5 次。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(qq_bot.release_single_instance)

    def _client(self, post_responses=None):
        config = qq_bot.QQBotConfig(appid="10001", secret="s3cret")
        client = qq_bot.QQBotClient(config)
        client._http = FakeHTTP(post_responses=post_responses)
        # 排队文件写到临时目录，别碰项目里的 memory/
        client._pending_path = lambda: os.path.join(self._tmp.name, "pending.json")
        return client

    @staticmethod
    def _seqs(http, endpoint_part="/v2/users/"):
        """收集被动回复用的 msg_seq（主动消息没有这个字段，不算）。"""
        return [item[2]["msg_seq"] for item in http.requests
                if endpoint_part in item[1] and item[0] == "POST" and "msg_seq" in item[2]]

    async def test_two_replies_to_one_message_use_different_seq(self):
        """就是这次的现场：同一条玩家消息回两条（撤销提示 + 新审批屏）。"""
        client = self._client()
        target = "qq:c2c:USER_A#MSG1"

        await client.send_message(target, "🚫 计划已撤销。正在根据您的要求重新评估...")
        await client.send_message(target, "🛑 [系统拦截] 请确认是否执行上述计划？\n⚔️ 体力目标：无", keyboard=None)

        self.assertEqual(self._seqs(client._http), [1, 2])

    async def test_chunks_then_next_message_continues_numbering(self):
        client = self._client()
        target = "qq:c2c:USER_A#MSG1"
        long_text = "\n".join("行" * 100 for _ in range(20))

        await client.send_message(target, long_text)
        first = self._seqs(client._http)
        self.assertGreater(len(first), 1)
        self.assertEqual(first, list(range(1, len(first) + 1)))

        await client.send_message(target, "第二条回复")
        self.assertEqual(self._seqs(client._http)[-1], len(first) + 1)

    async def test_different_messages_have_independent_counters(self):
        client = self._client()

        await client.send_message("qq:c2c:USER_A#MSG1", "第一条")
        await client.send_message("qq:c2c:USER_A#MSG2", "另一条消息的回复")

        self.assertEqual(self._seqs(client._http), [1, 1])

    async def test_event_credential_has_its_own_counter(self):
        client = self._client()
        target = qq_bot.build_target("group", "G1", event_id="EV1")

        await client.send_message(target, "已执行")
        await client.send_message(target, "配置已下发")

        bodies = [item[2] for item in client._http.requests if "/v2/groups/" in item[1]]
        self.assertEqual([body["msg_seq"] for body in bodies], [1, 2])
        self.assertTrue(all(body["event_id"] == "EV1" for body in bodies))

    async def test_dedupe_error_retries_with_next_seq(self):
        """平台说序号重复时（比如机器人中途重启过）自动换号重发一次。"""
        client = self._client(post_responses=[
            FakeResponse(400, {"message": "消息被去重，请检查请求msgseq", "code": 40054005}),
            FakeResponse(200, {}),
        ])
        target = "qq:c2c:USER_A#MSG1"

        sent = await client.send_message(target, "🛑 待确认")

        self.assertTrue(sent)
        self.assertEqual(self._seqs(client._http), [1, 2])
        # 记账已经推到 2，后续接着用 3
        await client.send_message(target, "下一条")
        self.assertEqual(self._seqs(client._http)[-1], 3)

    async def test_repeated_dedupe_errors_give_up(self):
        """连续判重（凭据已经不能用）→ 改走主动消息，主动消息也不成就排队补发，绝不静默丢。"""
        client = self._client(post_responses=[
            FakeResponse(400, {"code": 40054005, "message": "消息被去重"}),
            FakeResponse(400, {"code": 40054005, "message": "消息被去重"}),
            FakeResponse(400, {"code": 40034024, "message": "请求参数msg_id无效或越权"}),
        ])
        with patch.object(client, "_pending_path", lambda: os.path.join(self._tmp.name, "pending.json")):
            self.assertFalse(await client.send_message("qq:c2c:USER_A#MSG1", "🛑 待确认"))
        self.assertEqual(self._seqs(client._http), [1, 2])
        # 第三次是主动消息（不带 msg_id / msg_seq）
        active_bodies = [item[2] for item in client._http.requests
                         if "/v2/users/" in item[1] and "msg_id" not in item[2]]
        self.assertEqual(len(active_bodies), 1)
        self.assertNotIn("msg_seq", active_bodies[0])
        self.assertEqual(client._pending.get("c2c:USER_A"), ["🛑 待确认"])

    async def test_five_reply_limit_is_respected(self):
        """官方上限是"每条消息最多回 5 次"：第 6 条不再用被动序号，改走主动消息。"""
        client = self._client()
        target = "qq:c2c:USER_A#MSG1"

        for index in range(qq_bot.MAX_REPLIES_PER_MESSAGE):
            self.assertTrue(await client.send_message(target, f"第 {index + 1} 条"), index)

        # 再发一条：被动序号不再增长，而是发成不带凭据的主动消息
        await client.send_message(target, "第六条")
        bodies = [item[2] for item in client._http.requests if "/v2/users/" in item[1]]
        self.assertEqual([body["msg_seq"] for body in bodies[:5]], [1, 2, 3, 4, 5])
        self.assertNotIn("msg_seq", bodies[5])
        self.assertNotIn("msg_id", bodies[5])

    async def test_dedupe_detection_helpers(self):
        self.assertTrue(qq_bot._is_dedupe_error(FakeResponse(400, {"code": 40054005})))
        self.assertTrue(qq_bot._is_dedupe_error(FakeResponse(400, None, "消息被去重，请检查请求msgseq")))
        self.assertFalse(qq_bot._is_dedupe_error(FakeResponse(400, {"code": 1, "message": "参数错误"})))
        self.assertFalse(qq_bot._is_dedupe_error(FakeResponse(200, {})))
        self.assertFalse(qq_bot._is_dedupe_error(None))

    async def test_stale_counters_are_purged(self):
        client = self._client()
        await client.send_message("qq:c2c:USER_A#MSG1", "第一条")
        self.assertTrue(client._reply_seq)

        with patch.object(qq_bot.time, "time", return_value=time.time() + qq_bot.REPLY_SEQ_TTL + 10):
            await client.send_message("qq:c2c:USER_A#MSG2", "很久以后的另一条")

        self.assertEqual(list(client._reply_seq.keys()), ["c2c:USER_A#MSG2"])


class LateNoticeTests(unittest.IsolatedAsyncioTestCase):
    """迟到通知：一条龙跑 23 分钟后才出的完成报告。

    玩家实测：那条报告发不出去 —— 被动回复凭据只有 5 分钟，而且"每条消息最多回 5 次"的额度
    被启动过程的进度消息吃光了。所以现在：被动不行就改主动消息，主动不行就排队补发。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _client(self, post_responses=None):
        config = qq_bot.QQBotConfig(appid="10001", secret="s3cret")
        client = qq_bot.QQBotClient(config)
        client._http = FakeHTTP(post_responses=post_responses)
        client._pending_path = lambda: os.path.join(self._tmp.name, "pending.json")
        return client

    async def test_queue_limit_after_five_replies(self):
        """额度用光后不再硬撞：先试主动消息，主动消息也没额度就排队等下次补发。"""

        class PassiveOnly(FakeHTTP):
            """被动回复正常，但没有主动消息额度（很常见的账号状态）。"""

            async def post(self, url, json=None, headers=None):
                if "getAppAccessToken" in url:
                    return await FakeHTTP.post(self, url, json=json, headers=headers)
                self.requests.append(("POST", url, json, headers))
                if "msg_seq" in (json or {}):
                    return FakeResponse(200, {})
                return FakeResponse(400, {"code": 40054005, "message": "主动消息额度不足"})

        client = self._client()
        client._http = PassiveOnly()
        target = "qq:c2c:USER_A#MSG1"
        for index in range(qq_bot.MAX_REPLIES_PER_MESSAGE):
            self.assertTrue(await client.send_message(target, f"第 {index + 1} 条"), index)

        self.assertFalse(await client.send_message(target, "迟到的完成报告"))

        # 第 6 次先试了主动消息（不带 msg_id / msg_seq），失败后排进队列
        bodies = [item[2] for item in client._http.requests if "/v2/users/" in item[1]]
        self.assertNotIn("msg_seq", bodies[-1])
        self.assertNotIn("msg_id", bodies[-1])
        self.assertEqual(client._pending.get("c2c:USER_A"), ["迟到的完成报告"])

    async def test_active_message_succeeds(self):
        """主动消息能发出去时就不排队（平台给了主动消息额度的情况）。"""
        client = self._client(post_responses=[FakeResponse(200, {})])
        client._reply_seq["c2c:USER_A#MSG1"] = (qq_bot.MAX_REPLIES_PER_MESSAGE, time.time())

        self.assertTrue(await client.send_message("qq:c2c:USER_A#MSG1", "完成报告"))
        self.assertEqual(client._pending, {})
        body = [item[2] for item in client._http.requests if "/v2/users/" in item[1]][-1]
        self.assertNotIn("msg_id", body)
        self.assertNotIn("msg_seq", body)

    async def test_expired_credential_switches_to_active(self):
        """凭据过期（40034024）→ 直接改主动消息，不排队。"""
        client = self._client(post_responses=[
            FakeResponse(400, {"code": 40034024, "message": "请求参数msg_id无效或越权"}),
            FakeResponse(200, {}),
        ])

        self.assertTrue(await client.send_message("qq:c2c:USER_A#MSG1", "完成报告"))

        bodies = [item[2] for item in client._http.requests if "/v2/users/" in item[1]]
        self.assertEqual(bodies[0]["msg_seq"], 1)
        self.assertIn("msg_id", bodies[0])
        self.assertNotIn("msg_id", bodies[1])
        self.assertEqual(client._pending, {})

    async def test_flush_pending_uses_next_message_credential(self):
        """排队的内容在玩家下次说话时用新凭据补发。"""
        client = self._client()
        client._pending = {"c2c:USER_A": ["迟到的完成报告", "还有一条备份提醒"]}
        client._loop = asyncio.get_running_loop()

        await asyncio.to_thread(client.flush_pending, "qq:c2c:USER_A#MSG2")

        bodies = [item[2] for item in client._http.requests if "/v2/users/" in item[1]]
        self.assertEqual([body["msg_id"] for body in bodies], ["MSG2", "MSG2"])
        self.assertEqual([body["msg_seq"] for body in bodies], [1, 2])
        self.assertEqual(client._pending, {})

    async def test_flush_keeps_what_still_fails(self):
        client = self._client(post_responses=[FakeResponse(200, {}), FakeResponse(500, None, "boom"),
                                              FakeResponse(500, None, "boom"), FakeResponse(500, None, "boom")])
        client._pending = {"c2c:USER_A": ["第一条", "第二条"]}
        client._loop = asyncio.get_running_loop()

        await asyncio.to_thread(client.flush_pending, "qq:c2c:USER_A#MSG2")

        self.assertEqual(client._pending["c2c:USER_A"], ["第二条"])

    async def test_pending_queue_is_persisted_and_reloaded(self):
        client = self._client()
        client.queue_notice(qq_bot.parse_target("qq:c2c:USER_A#MSG1"), "迟到的完成报告")

        fresh = self._client()
        fresh._load_pending()

        self.assertEqual(fresh._pending, {"c2c:USER_A": ["迟到的完成报告"]})

    async def test_pending_queue_is_bounded(self):
        client = self._client()
        info = qq_bot.parse_target("qq:c2c:USER_A#MSG1")
        for index in range(qq_bot.PENDING_QUEUE_LIMIT + 3):
            client.queue_notice(info, f"第 {index} 条")

        self.assertEqual(len(client._pending["c2c:USER_A"]), qq_bot.PENDING_QUEUE_LIMIT)
        self.assertEqual(client._pending["c2c:USER_A"][-1], f"第 {qq_bot.PENDING_QUEUE_LIMIT + 2} 条")

    async def test_broken_pending_file_is_ignored(self):
        client = self._client()
        with open(os.path.join(self._tmp.name, "pending.json"), "w", encoding="utf-8") as handle:
            handle.write("{不是 json")

        client._load_pending()

        self.assertEqual(client._pending, {})


class CustomMenuTests(unittest.IsolatedAsyncioTestCase):
    """单聊自定义菜单（`PUT /v2/menu`）：拿不到消息按钮权限时的替代方案。

    官方文档：自定义菜单只支持 C2C，`send_message` 类型点击后把文本填进输入框，
    而且**没有内邀/申请门槛** —— 正好能把审批词 y / t / 取消 放在玩家手边。
    """

    def _client(self, get_payload=None, put_payload=None, put_status=200):
        config = qq_bot.QQBotConfig(appid="10001", secret="s3cret")
        client = qq_bot.QQBotClient(config)

        class MenuHTTP(FakeHTTP):
            async def get(self, url, headers=None):
                if "/v2/menu" in url:
                    self.requests.append(("GET", url, None, headers))
                    return FakeResponse(200, get_payload if get_payload is not None else {"version": 1})
                return await super().get(url, headers=headers)

            async def put(self, url, json=None, headers=None):
                if "/v2/menu" in url:
                    self.requests.append(("PUT", url, json, headers))
                    return FakeResponse(put_status, put_payload if put_payload is not None else {"version": 41})
                return await super().put(url, json=json, headers=headers)

        client._http = MenuHTTP()
        return client

    def test_menu_items_are_within_official_limits(self):
        self.assertLessEqual(len(qq_bot.MENU_ITEMS), 10)
        for item in qq_bot.MENU_ITEMS:
            self.assertEqual(item["type"], "send_message")
            self.assertLessEqual(qq_bot.QQBotClient._menu_name_len(item["name"]), qq_bot.MENU_NAME_LIMIT)

    def test_menu_sends_the_approval_words(self):
        """菜单点下去的文本必须和审批词一致，否则后端认不出来。"""
        texts = [item["send_message"] for item in qq_bot.MENU_ITEMS]

        self.assertIn("y", texts)
        self.assertIn("t", texts)
        self.assertIn("取消", texts)

    async def test_setup_menu_puts_items(self):
        client = self._client()

        self.assertTrue(await client.setup_menu())

        method, url, body, headers = [item for item in client._http.requests if item[0] == "PUT"][0]
        self.assertTrue(url.endswith("/v2/menu"))
        self.assertEqual([item["send_message"] for item in body["menu"]["items"]], ["y", "t", "取消"])
        self.assertEqual(headers["Authorization"], "QQBot TOKEN1")

    async def test_setup_menu_reports_failure(self):
        client = self._client(put_status=403, put_payload={"message": "没有权限", "code": 11244})

        self.assertFalse(await client.setup_menu())

    async def test_setup_menu_rejects_too_long_names(self):
        client = self._client()

        with self.assertRaises(qq_bot.QQBotError):
            await client.setup_menu([{"type": "send_message", "name": "这个名字实在是太长了放不下", "send_message": "y"}])
        self.assertEqual([item for item in client._http.requests if item[0] == "PUT"], [])

    async def test_fetch_menu(self):
        client = self._client(get_payload={"version": 7, "menu": {"items": [{"name": "帮助", "type": "send_message", "send_message": "/help"}]}})

        payload = await client.fetch_menu()

        self.assertEqual(payload["version"], 7)
        self.assertEqual(payload["menu"]["items"][0]["name"], "帮助")

    async def test_fetch_menu_error_raises(self):
        client = self._client()
        client._http.gateway_payload = FakeResponse(500, None, "boom")

        class BadHTTP(FakeHTTP):
            async def get(self, url, headers=None):
                return FakeResponse(500, None, "boom")

        client._http = BadHTTP()
        with self.assertRaises(qq_bot.QQBotError):
            await client.fetch_menu()

    async def test_ensure_menu_sets_it_when_empty(self):
        client = self._client(get_payload={"version": 1})

        self.assertTrue(await client.ensure_menu())

        self.assertTrue([item for item in client._http.requests if item[0] == "PUT"])

    async def test_ensure_menu_keeps_a_foreign_menu(self):
        """别人配过菜单就别乱动（提示可以用 --setup-menu 覆盖）。"""
        client = self._client(get_payload={"version": 3, "menu": {"items": [{"name": "帮助", "type": "send_message", "send_message": "/help"}]}})

        self.assertFalse(await client.ensure_menu())

        self.assertEqual([item for item in client._http.requests if item[0] == "PUT"], [])

    async def test_ensure_menu_recognises_our_own_menu(self):
        client = self._client(get_payload={"version": 4, "menu": {"items": [dict(item) for item in qq_bot.MENU_ITEMS]}})

        self.assertTrue(await client.ensure_menu())

        self.assertEqual([item for item in client._http.requests if item[0] == "PUT"], [])

    async def test_ensure_menu_never_blocks_startup(self):
        class BrokenHTTP(FakeHTTP):
            async def get(self, url, headers=None):
                raise RuntimeError("网络炸了")

        client = self._client()
        client._http = BrokenHTTP()

        self.assertFalse(await client.ensure_menu())     # 不抛异常


if __name__ == "__main__":
    unittest.main()
