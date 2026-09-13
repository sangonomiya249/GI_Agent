"""米游社个人战绩（skills/mys_api.py）的测试。

重点覆盖"不出网也要能验"的部分：DS 签名、UID→区服、快照缓存与 TTL、
"只把玩家提到的展柜外角色拿出来"、以及**没配 cookie 时一次请求都不发**。
HTTP 层用假响应打桩（真接口要账号 cookie，而且有风控，不能进单测）。
"""

import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import config
from skills import mys_api


class DsSignTests(unittest.TestCase):
    def test_ds_matches_the_documented_formula(self):
        """DS = md5("salt={salt}&t={t}&r={r}&b={body}&q={query}")，输出 `t,r,md5`。"""
        import hashlib

        ds = mys_api.make_ds(
            query="role_id=100000000&server=cn_gf01",
            body="",
            salt="TESTSALT",
            now=1700000000,
            nonce=100001,
        )
        expected = hashlib.md5(
            "salt=TESTSALT&t=1700000000&r=100001&b=&q=role_id=100000000&server=cn_gf01".encode()
        ).hexdigest()

        self.assertEqual(ds, f"1700000000,100001,{expected}")

    def test_body_is_signed_as_sent(self):
        """POST 的 DS 必须签"实际发出去的那串 body"（键顺序变了签名就不对）。"""
        body = json.dumps({"role_id": "100000000", "server": "cn_gf01"}, ensure_ascii=False)
        ds = mys_api.make_ds(body=body, salt="TESTSALT", now=1, nonce=2)

        import hashlib

        self.assertEqual(
            ds,
            "1,2," + hashlib.md5(f"salt=TESTSALT&t=1&r=2&b={body}&q=".encode()).hexdigest(),
        )

    def test_nonce_is_six_digits(self):
        for _ in range(20):
            _, nonce, _ = mys_api.make_ds(now=1).split(",")
            self.assertEqual(len(nonce), 6)
            self.assertTrue(nonce.isdigit())


class ServerAndCookieTests(unittest.TestCase):
    def test_server_from_uid(self):
        self.assertEqual(mys_api.server_from_uid("100000000"), "cn_gf01")   # 官服
        self.assertEqual(mys_api.server_from_uid("500000001"), "cn_qd01")   # B 服
        self.assertEqual(mys_api.server_from_uid("100000001"), "cn_gf01")
        self.assertEqual(mys_api.server_from_uid(""), "cn_gf01")
        self.assertEqual(mys_api.server_from_uid("abc"), "cn_gf01")

    def test_cookie_never_leaks_in_full(self):
        masked = mys_api.mask_cookie("ltuid=123456789; ltoken=SECRETTOKENVALUE; foo=bar")

        self.assertIn("ltuid=", masked)
        self.assertNotIn("123456789", masked)
        self.assertNotIn("SECRETTOKENVALUE", masked)
        self.assertIn("SECR…ALUE", masked)

    def test_account_id_from_cookie(self):
        self.assertEqual(mys_api.cookie_account_id("ltuid=123456789;ltoken=x"), "123456789")
        self.assertEqual(mys_api.cookie_account_id("account_id_v2=987654321;x=1"), "987654321")
        self.assertEqual(mys_api.cookie_account_id("nothing=1"), "")


class SnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "mys_characters.json")

    def _snapshot(self, hours_ago=0, count=2):
        moment = datetime.datetime.now() - datetime.timedelta(hours=hours_ago)
        return {
            "fetched_at": moment.strftime(mys_api._TIME_FORMAT),
            "uid": "100000000",
            "server": "cn_gf01",
            "nickname": "测试",
            "device_id": "dev-1",
            "avatars": [
                {"id": str(10000000 + index), "name": f"角色{index}", "level": 80}
                for index in range(count)
            ],
        }

    def test_save_and_load_round_trip(self):
        mys_api.save_snapshot(self._snapshot(), path=self.path)

        loaded = mys_api.load_snapshot(self.path)
        self.assertEqual(loaded["uid"], "100000000")
        self.assertEqual(len(loaded["avatars"]), 2)

    def test_atomic_write_leaves_no_tmp(self):
        mys_api.save_snapshot(self._snapshot(), path=self.path)

        self.assertTrue(os.path.isfile(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_missing_or_broken_file_is_tolerated(self):
        self.assertIsNone(mys_api.load_snapshot(self.path))

        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")

        self.assertIsNone(mys_api.load_snapshot(self.path))

    def test_ttl_decides_staleness(self):
        with patch.object(config, "MYS_CACHE_TTL_HOURS", 48):
            self.assertFalse(mys_api.snapshot_is_stale(self._snapshot(hours_ago=1)))
            self.assertTrue(mys_api.snapshot_is_stale(self._snapshot(hours_ago=49)))
        # 没有快照 / 时间戳坏了 → 都算该刷新
        self.assertTrue(mys_api.snapshot_is_stale(None))
        self.assertTrue(mys_api.snapshot_is_stale({"fetched_at": "乱写"}))

    def test_fresh_snapshot_is_not_refetched(self):
        """玩家明确要求："别每次启动都拉，1~3 天一次"。"""
        mys_api.save_snapshot(self._snapshot(hours_ago=2), path=self.path)

        with patch.object(mys_api, "refresh_snapshot") as refresh:
            snapshot = mys_api.get_snapshot(path=self.path)

        refresh.assert_not_called()
        self.assertEqual(snapshot["uid"], "100000000")

    def test_stale_snapshot_is_refetched(self):
        mys_api.save_snapshot(self._snapshot(hours_ago=100), path=self.path)

        with patch.object(config, "MYS_CACHE_TTL_HOURS", 48), patch.object(
            mys_api, "refresh_snapshot", return_value={"fetched_at": "now", "avatars": []}
        ) as refresh:
            mys_api.get_snapshot(path=self.path)

        refresh.assert_called_once()


class HttpTests(unittest.TestCase):
    """HTTP 层：用假 httpx 客户端验证"发到哪、签了没、返回码怎么分类"。"""

    class _Response:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code
            self.text = json.dumps(payload, ensure_ascii=False)

        def json(self):
            if not isinstance(self._payload, dict):
                raise ValueError("not json")
            return self._payload

    class _Client:
        def __init__(self, response, calls):
            self._response = response
            self._calls = calls

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None):
            self._calls.append(("GET", url, headers, None))
            return self._response

        def post(self, url, content=None, headers=None):
            self._calls.append(("POST", url, headers, content))
            return self._response

    def _patch_http(self, payload, status_code=200):
        calls = []
        fake = self._Client(self._Response(payload, status_code), calls)
        patcher = patch("httpx.Client", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def _with_cookie(self):
        patcher = patch.object(config, "MYS_COOKIE", "ltuid=1;ltoken=secret")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_character_list_posts_signed_body(self):
        self._with_cookie()
        calls = self._patch_http({"retcode": 0, "data": {"avatars": [{"id": 10000002, "name": "神里绫华"}]}})

        avatars, server = mys_api.fetch_character_list("100000000")

        self.assertEqual(avatars[0]["name"], "神里绫华")
        self.assertEqual(server, "cn_gf01")
        method, url, headers, content = calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("/game_record/app/genshin/api/character/list", url)
        self.assertEqual(headers["Cookie"], "ltuid=1;ltoken=secret")
        self.assertTrue(headers["DS"])
        # 签名必须覆盖"实际发出去的 body"
        self.assertEqual(
            headers["DS"],
            mys_api.make_ds("", content.decode("utf-8"), salt=config.MYS_SALT,
                            now=int(headers["DS"].split(",")[0]),
                            nonce=int(headers["DS"].split(",")[1])),
        )

    def test_roles_are_filtered_to_genshin_cn(self):
        self._with_cookie()
        self._patch_http({
            "retcode": 0,
            "data": {"list": [
                {"game_biz": "hk4e_cn", "game_uid": "100000000", "region": "cn_gf01",
                 "nickname": "旅行者", "level": 60},
                {"game_biz": "hkrpg_cn", "game_uid": "100", "nickname": "开拓者"},
            ]},
        })

        roles = mys_api.fetch_roles()

        self.assertEqual([role["uid"] for role in roles], ["100000000"])

    def test_auth_retcode_raises_auth_error(self):
        self._with_cookie()
        self._patch_http({"retcode": -100, "message": "not logged in"})

        with self.assertRaises(mys_api.MysAuthError) as caught:
            mys_api.fetch_roles()

        self.assertIn("cookie", str(caught.exception))

    def test_risk_control_retcode_raises_risk_error(self):
        self._with_cookie()
        self._patch_http({"retcode": 10001, "message": "need verify"})

        with self.assertRaises(mys_api.MysRiskControl):
            mys_api.fetch_roles()

    def test_unknown_retcode_raises_api_error(self):
        self._with_cookie()
        self._patch_http({"retcode": -1, "message": "系统繁忙"})

        with self.assertRaises(mys_api.MysApiError):
            mys_api.fetch_roles()

    def test_non_json_response_is_transport_error(self):
        self._with_cookie()
        self._patch_http(["not", "a", "dict"])

        with self.assertRaises(mys_api.MysTransportError):
            mys_api.fetch_roles()

    def test_no_cookie_means_no_request(self):
        """没配 cookie → 一次请求都不发（玩家担心的风控，从源头掐掉）。"""
        with patch.object(config, "MYS_COOKIE", ""), patch("httpx.Client") as client:
            with self.assertRaises(mys_api.MysNotConfigured):
                mys_api.fetch_roles()

        client.assert_not_called()


class MentionTests(unittest.TestCase):
    def test_only_mentioned_names_are_picked(self):
        candidates = ["胡桃", "蓝砚", "钟离"]

        self.assertEqual(mys_api.mentioned_characters("帮我看看胡桃要刷什么", candidates), ["胡桃"])
        self.assertEqual(mys_api.mentioned_characters("今天打什么好", candidates), [])

    def test_longer_name_wins(self):
        candidates = ["胡桃", "桃"]

        self.assertEqual(mys_api.mentioned_characters("胡桃的材料", candidates), ["胡桃"])

    def test_single_char_names_only_in_short_sentences(self):
        """魈/琴 这种单字名：短句才认，否则"钢琴"「琴酒」都会命中。"""
        candidates = ["魈", "琴"]

        self.assertEqual(mys_api.mentioned_characters("魈", candidates), ["魈"])
        self.assertEqual(mys_api.mentioned_characters("我想弹一下钢琴，然后去璃月逛逛", candidates), [])


class ShowcaseGapNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "mys_characters.json")
        self.snapshot = {
            "fetched_at": datetime.datetime.now().strftime(mys_api._TIME_FORMAT),
            "uid": "100000000",
            "server": "cn_gf01",
            "nickname": "旅行者",
            "avatars": [
                {"id": "10000046", "name": "胡桃", "level": 80,
                 "weapon": {"id": "13501", "name": "护摩之杖", "level": 90},
                 "talents": {"普通攻击": 8, "元素战技": 8, "元素爆发": 8},
                 "talents_at": "2026-09-12 10:00:00"},
            ],
        }
        mys_api.save_snapshot(self.snapshot, path=self.path)
        for name, value in (("MYS_COOKIE", "ltuid=1;ltoken=secret"), ("MYS_CACHE_PATH", self.path)):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_no_cookie_returns_empty(self):
        with patch.object(config, "MYS_COOKIE", ""):
            self.assertEqual(
                mys_api.showcase_gap_notice("胡桃要什么材料", ["蓝砚"], path=self.path), ""
            )

    def test_avatar_in_showcase_is_not_injected(self):
        """已经在展柜里的角色不重复注入（展柜才是权威，也省一次请求）。"""
        notice = mys_api.showcase_gap_notice("胡桃要什么材料", ["胡桃"], path=self.path)

        self.assertEqual(notice, "")

    def test_non_showcase_character_is_injected_with_materials(self):
        notice = mys_api.showcase_gap_notice("帮我练一下胡桃", ["蓝砚"], path=self.path)

        self.assertIn("展柜外角色参考", notice)
        self.assertIn("胡桃 Lv.80", notice)
        self.assertIn("天赋 8/8/8", notice)
        self.assertIn("护摩之杖", notice)
        self.assertIn("不要再回答", notice)

    def test_unowned_character_is_called_out(self):
        """账号里压根没有的角色：说清"号里没有"，别和"展柜里没有"混为一谈。"""
        with patch.object(config, "MYS_CACHE_PATH", self.path):
            notice = mys_api.showcase_gap_notice("给我刷个钟离的材料", ["蓝砚"], path=self.path)

        self.assertIn("钟离", notice)
        self.assertIn("这个号里没有", notice)

    def test_api_failure_degrades_without_crashing(self):
        mys_api.save_snapshot(
            dict(self.snapshot, fetched_at="2020-01-01 00:00:00"), path=self.path
        )
        with patch.object(mys_api, "refresh_snapshot", side_effect=mys_api.MysRiskControl("要验证码")):
            notice = mys_api.showcase_gap_notice("胡桃要什么材料", ["蓝砚"], path=self.path)

        self.assertIn("展柜外角色参考", notice)
        self.assertIn("要验证码", notice)
        self.assertIn("不要凭猜测编造", notice)


class AvatarEntryTests(unittest.TestCase):
    """展柜外角色也要产出和展柜一模一样的 material 形状。"""

    def setUp(self):
        from skills import env_reader

        self.env_reader = env_reader
        self.yatta = {
            "10000046": {
                "name": "胡桃",
                "materials": [{"name": "霓裳花", "type": "特产", "schedule": "大世界采集"}],
                "boss_mat_name": "未熟之玉",
                "boss_name": "古岩龙蜥",
            },
            "13501": {"name": "护摩之杖", "materials": [{"name": "漆黑陨铁的一角", "type": "武器材料"}]},
        }
        self.avatar = {
            "id": "10000046", "name": "胡桃", "level": 80,
            "weapon": {"id": "13501", "name": "护摩之杖", "level": 90},
            "talents": {"元素战技": 9},
        }

    def test_entry_shape_matches_showcase(self):
        entry = mys_api.avatar_entry("10000046", snapshot={"avatars": [self.avatar]},
                                     yatta_dict=self.yatta)

        self.assertEqual(entry["name"], "胡桃")
        self.assertEqual(entry["level"], 80)
        self.assertEqual(entry["weapon"]["name"], "护摩之杖")
        self.assertEqual(entry["skills"], {"元素战技": 9})
        types = {item["type"]: item for item in entry["materials"]}
        self.assertEqual(types["特产"]["name"], "霓裳花")
        # 80 级 → 到 81 级还缺 20 个 Boss 材料（和展柜那条路径同一个函数）
        self.assertEqual(types["Boss材料"]["needed"], 20)
        self.assertEqual(types["Boss材料"]["schedule"], "【古岩龙蜥】")

    def test_unknown_avatar_returns_none(self):
        self.assertIsNone(mys_api.avatar_entry("0", snapshot={"avatars": []}, yatta_dict=self.yatta))


if __name__ == "__main__":
    unittest.main()
