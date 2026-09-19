"""米游社扫码登录（skills/mys_login.py）的测试。

重点覆盖**不出网、不碰玩家浏览器**也能验的部分：

  · cookie 库解密：DPAPI 拿密钥的失败路径、`v10` 密文解密、**`v20` 必须明确报读不出来**；
  · 内置的纯标准库 AES-GCM：用可信向量校准（`Crypto` 在就与它逐字节对照）；
  · 浏览器启动命令：`--user-data-dir` 必须指向**项目内的独立目录**（不能碰玩家日常 profile）；
  · 关窗口：必须按配置目录精确匹配（不能 `taskkill /IM msedge.exe` 把玩家的浏览器一起杀掉）；
  · cookie 拼装与"不完整不许保存"的判定。

不真的登录、不发请求、不动玩家的 `.env`。
"""

import builtins
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import config
from skills import mys_login, mys_api


HAS_CRYPTO = True
try:                                    # noqa: SIM105
    from Crypto.Cipher import AES as _AES
except ImportError:                     # pragma: no cover
    HAS_CRYPTO = False


# ==========================================
# 🌟 内置 AES-GCM（纯标准库）
# ==========================================


class PureGcmTests(unittest.TestCase):
    """内置实现的正确性：只用**可信锚点**（NIST 空明文向量）与"自产自解"来验。

    为什么这么小心：AES-GCM 写错时**不会报错**，只会解出乱码；而计数器约定写错时
    **连 tag 都能对上**（tag 用的是 J0，两种约定下都自洽）。这组用例专门盯这两点。
    """

    def test_nist_empty_plaintext_vectors(self):
        """NIST GCM 空明文的 tag == E(K, J0)：这是最可靠的外部锚点。"""
        cases = (
            (bytes(32), bytes(12), "530f8afbc74536b9a963b4f1c4cb738b"),
            (bytes(16), bytes(12), "58e2fccefa7e3061367f1d57a4e7455a"),
        )
        for key, nonce, tag_hex in cases:
            out = mys_login._aes_gcm_decrypt_pure(key, nonce, b"", bytes.fromhex(tag_hex))
            self.assertEqual(out, b"")

    def test_round_trip_is_stable(self):
        """同一段密文解两次必须一致（防"每次结果都不同"的实现错误）。"""
        key, nonce, plaintext = bytes(32), bytes(12), b"ltuid=285006984"
        if HAS_CRYPTO:
            cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
            ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        else:                           # pragma: no cover - 依赖环境
            self.skipTest("没有 Crypto，跳过")
        first = mys_login._aes_gcm_decrypt_pure(key, nonce, ciphertext, tag)
        second = mys_login._aes_gcm_decrypt_pure(key, nonce, ciphertext, tag)

        self.assertEqual(first, plaintext)
        self.assertEqual(first, second)

    def test_tampered_tag_is_rejected(self):
        """改一个 bit 就必须拒绝 —— 这是"绝不放行未经验证的密文"的底线。"""
        if not HAS_CRYPTO:              # pragma: no cover
            self.skipTest("没有 Crypto，跳过")
        key, nonce, plaintext = bytes(32), bytes(12), b"ltoken=secret"
        cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        tampered = bytes([tag[0] ^ 0x01]) + tag[1:]

        with self.assertRaises(ValueError):
            mys_login._aes_gcm_decrypt_pure(key, nonce, ciphertext, tampered)

    def test_multi_block_is_refused_loudly(self):
        """多块明文显式拒绝（而不是"看起来能解但结果错"）。"""
        with self.assertRaises(ValueError) as caught:
            mys_login._aes_gcm_decrypt_pure(bytes(32), bytes(12), b"x" * 32, bytes(16))

        self.assertIn("单块", str(caught.exception))

    @unittest.skipUnless(HAS_CRYPTO, "没有 pycryptodome，跳过对照")
    def test_matches_pycryptodome_for_real_cookie_lengths(self):
        """与 pycryptodome 逐字节对照（cookie 值的真实长度范围）。"""
        for plaintext in (b"ltuid=285006984", b"0123456789abcdef", b"C2SECRETVALUE",
                          b"account_id_v2=1"):
            key, nonce = bytes(32), bytes(12)
            cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
            ciphertext, tag = cipher.encrypt_and_digest(plaintext)

            out = mys_login._aes_gcm_decrypt_pure(key, nonce, ciphertext, tag,
                                                  counter_start=2)

            self.assertEqual(out, plaintext, plaintext)

    def test_gf_multiplication_is_a_consistent_field(self):
        """GF(2^128) 乘法的基本性质：x·1 == x（GCM 位序下的"1"是最低位为 1 的那个值）。"""
        # GCM 位序里，位串的"1"就是整数的最高位
        one = 1 << 127
        for value in (0, 1, 0x66e94bd4ef8a2c3b884cfa59ca342b2e, one):
            self.assertEqual(mys_login._gf_mul(value, one), value)
            self.assertEqual(mys_login._gf_mul(one, value), value)


# ==========================================
# 🌟 cookie 解密分发
# ==========================================


class DecryptCookieValueTests(unittest.TestCase):
    def test_empty_blob_is_not_an_error(self):
        self.assertEqual(mys_login.decrypt_cookie_value(b"", bytes(32)), ("", ""))
        self.assertEqual(mys_login.decrypt_cookie_value(None, bytes(32)), ("", ""))

    def test_plain_text_blob_is_returned_as_is(self):
        """有些 cookie 在库里就是明文（没加密）——要原样返回。"""
        value, why = mys_login.decrypt_cookie_value(b"plain-value", bytes(32))

        self.assertEqual(value, "plain-value")
        self.assertEqual(why, "")

    def test_v20_is_reported_as_unreadable(self):
        """Chromium 137+ 的 App-Bound 加密：必须**明确说读不出来**，不能假装成功。"""
        blob = b"v20" + bytes(12) + bytes(16) + bytes(16)

        value, why = mys_login.decrypt_cookie_value(blob, bytes(32))

        self.assertEqual(value, "")
        self.assertIn("App-Bound", why)
        self.assertIn("手动", why)

    @unittest.skipUnless(HAS_CRYPTO, "没有 pycryptodome，跳过")
    def test_v10_blob_round_trip(self):
        key, nonce, secret = bytes(range(32)), bytes(range(12)), b"ltuid=285006984"
        cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(secret)
        blob = b"v10" + nonce + ciphertext + tag

        value, why = mys_login.decrypt_cookie_value(blob, key)

        self.assertEqual(value, secret.decode())
        self.assertEqual(why, "")

    @unittest.skipUnless(HAS_CRYPTO, "没有 pycryptodome，跳过")
    def test_wrong_key_is_reported_not_silently_wrong(self):
        key, nonce, secret = bytes(32), bytes(12), b"ltoken=SECRET"
        cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(secret)
        blob = b"v10" + nonce + ciphertext + tag

        value, why = mys_login.decrypt_cookie_value(blob, bytes([7] * 32))

        self.assertEqual(value, "")
        self.assertTrue(why)

    def test_broken_ciphertext_does_not_raise(self):
        """坏数据不能让调用方炸掉 —— 返回 (空, 原因)。"""
        value, why = mys_login.decrypt_cookie_value(b"v10" + b"\x00" * 40, bytes(32))

        self.assertEqual(value, "")
        self.assertTrue(why)


class LooksLikeCookieTests(unittest.TestCase):
    def test_accepts_readable_values(self):
        for text in ("ltuid=1", "C2SECRET", "285006984", "abc-._~=+/"):
            self.assertTrue(mys_login._looks_like_cookie_value(text), text)

    def test_rejects_binary_garbage(self):
        """乱码（含控制字符）必须被判为"不像 cookie" —— 这是抓出计数器约定写错的关键。"""
        self.assertFalse(mys_login._looks_like_cookie_value("\x00\x01\x02abc"))
        self.assertFalse(mys_login._looks_like_cookie_value("bad\x01value"))

    def test_rejects_empty_and_absurdly_long(self):
        self.assertFalse(mys_login._looks_like_cookie_value(""))
        self.assertFalse(mys_login._looks_like_cookie_value("a" * 5000))


# ==========================================
# 🌟 浏览器：启动与关闭
# ==========================================


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "MYS_LOGIN_PROFILE_DIR",
                               os.path.join(self.tmp.name, "mys-login"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_profile_is_inside_the_project_not_the_players_browser(self):
        """登录用的是**项目里的独立配置目录**，不能碰玩家日常浏览器的 profile。"""
        profile = mys_login.profile_dir()

        self.assertIn(self.tmp.name, profile)
        self.assertTrue(profile.endswith("mys-login"))
        self.assertNotIn(os.path.expanduser("~"), profile.replace(self.tmp.name, ""))

    def test_launch_command_uses_the_isolated_profile(self):
        launched = {}

        class FakeProcess:
            pid = 4242

        def fake_popen(command, **kwargs):
            launched["command"] = command
            return FakeProcess()

        with patch.object(mys_login, "find_browser", return_value=r"C:\fake\msedge.exe"), \
             patch.object(mys_login.subprocess, "Popen", fake_popen):
            info = mys_login.launch_login_window()

        command = launched["command"]
        self.assertTrue(info["ok"])
        self.assertEqual(info["pid"], 4242)
        self.assertTrue(any(item.startswith("--user-data-dir=") for item in command))
        self.assertIn(f"--user-data-dir={mys_login.profile_dir()}", command)
        self.assertIn(mys_login.LOGIN_URL, command)

    def test_missing_browser_gives_an_actionable_error(self):
        with patch.object(mys_login, "find_browser", return_value=None):
            with self.assertRaises(mys_login.LoginError) as caught:
                mys_login.launch_login_window()

        text = str(caught.exception)
        self.assertIn("Edge", text)
        self.assertIn("docs/MYS_COOKIE.md", text)

    def test_close_matches_the_profile_not_every_browser(self):
        """关窗口必须是"只关我们自己开的"，否则会连玩家日常浏览器一起杀掉。"""
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)

        with patch.object(mys_login.subprocess, "run", fake_run):
            mys_login.close_login_window()

        script = " ".join(commands[0])
        self.assertIn("CommandLine", script)
        self.assertIn("mys-login", script)
        self.assertNotIn("taskkill /IM", script)         # 绝不能按进程名全杀

    def test_close_prefers_our_own_pid(self):
        """第一步按"我们启动时记下的 PID"关（不需要 WMI 权限，也绝不会误杀）。"""
        commands = []
        mys_login._LAUNCHED["pids"] = [4242]

        def fake_run(command, **kwargs):
            commands.append(command)

        with patch.object(mys_login.subprocess, "run", fake_run):
            mys_login.close_login_window()

        taskkill = commands[0]
        self.assertEqual(taskkill[0], "taskkill")
        self.assertIn("4242", taskkill)
        self.assertEqual(mys_login._LAUNCHED["pids"], [])   # 关完要清掉记录

    def test_close_survives_a_dead_process(self):
        """进程早就退了（或没有权限）时不能抛异常 —— 关窗口只是收尾动作。"""
        def boom(command, **kwargs):
            raise OSError("拒绝访问")

        mys_login._LAUNCHED["pids"] = [1]
        with patch.object(mys_login.subprocess, "run", boom):
            result = mys_login.close_login_window()

        self.assertTrue(result["ok"])

    def test_launch_records_the_pid_for_later_closing(self):
        class FakeProcess:
            pid = 777

        with patch.object(mys_login, "find_browser", return_value=r"C:\fake\msedge.exe"), \
             patch.object(mys_login.subprocess, "Popen", lambda *a, **k: FakeProcess()):
            mys_login.launch_login_window()

        self.assertEqual(mys_login._LAUNCHED["pids"], [777])


# ==========================================
# 🌟 cookie 库：读文件
# ==========================================


class CookieStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        patcher = patch.object(config, "MYS_LOGIN_PROFILE_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_db(self, rows):
        path = os.path.join(self.root, "Default", "Network", "Cookies")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB)"
        )
        connection.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?)", rows)
        connection.commit()
        connection.close()
        return path

    def test_finds_the_cookie_db_in_the_chromium_layout(self):
        self._make_db([])

        self.assertTrue(mys_login.cookie_db_paths())

    def test_no_profile_means_a_clear_note_not_an_exception(self):
        result = mys_login.read_cookies()

        self.assertEqual(result["cookies"], {})
        self.assertTrue(result["notes"])

    def test_only_mihoyo_hosts_and_wanted_names_are_read(self):
        """别的网站的 cookie、以及不在白名单里的名字，一律不许进结果。"""
        self._make_db([
            ("passport-api.mihoyo.com", "ltoken", "TOKEN", b""),
            ("passport-api.mihoyo.com", "mihoyo_other", "OTHER", b""),
            ("example.com", "ltoken", "NOT-OURS", b""),
            (".mihoyo.com", "account_id", "285006984", b""),
        ])

        with patch.object(mys_login, "cookie_key", return_value=bytes(32)):
            result = mys_login.read_cookies()

        self.assertEqual(result["cookies"].get("ltoken"), "TOKEN")
        self.assertEqual(result["cookies"].get("account_id"), "285006984")
        self.assertNotIn("mihoyo_other", result["cookies"])
        self.assertNotIn("NOT-OURS", result["cookies"].values())

    def test_encrypted_values_go_through_the_decryptor(self):
        self._make_db([
            ("passport-api.mihoyo.com", "ltoken", "", b"v20" + bytes(12) + bytes(16) + bytes(16)),
        ])

        with patch.object(mys_login, "cookie_key", return_value=bytes(32)):
            result = mys_login.read_cookies()

        self.assertEqual(result["cookies"], {})
        self.assertTrue(any("App-Bound" in note for note in result["notes"]))

    def test_unreadable_cookie_db_is_a_note_not_a_crash(self):
        self._make_db([("passport-api.mihoyo.com", "ltoken", "X", b"")])
        with patch.object(mys_login, "cookie_key", side_effect=mys_login.LoginError("DPAPI 失败")):
            result = mys_login.read_cookies()

        self.assertEqual(result["cookies"], {})
        self.assertTrue(any("DPAPI" in note for note in result["notes"]))

    def test_shared_read_copy_works_while_the_file_is_open(self):
        """浏览器开着也能读：用**共享读**打开，而不是 `shutil.copy2`。

        实测踩过：`shutil.copy2` 在 Chromium 持有写句柄时会抛 `PermissionError`，
        界面上表现为"cookie 库被占用，请关掉登录窗口再试" —— 而**根本不需要关窗口**。
        """
        path = self._make_db([("passport-api.mihoyo.com", "ltoken", "TOKEN", b"")])
        target = os.path.join(self.root, "copy.db")

        with open(path, "rb") as holder:          # 持有句柄，模拟"文件正在被使用"
            mys_login._copy_file_sharing(path, target)

        self.assertTrue(os.path.getsize(target) > 0)

    def test_read_rows_survives_an_open_handle(self):
        path = self._make_db([("passport-api.mihoyo.com", "ltoken", "TOKEN", b"")])

        with open(path, "rb") as holder:
            rows = mys_login._read_rows(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "ltoken")

    def test_read_rows_picks_up_the_wal(self):
        """刚写入的 cookie 可能还在 `-wal` 里：只复制主库会读到旧数据。"""
        path = self._make_db([])
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO cookies VALUES ('passport-api.mihoyo.com', "
                           "'ltoken', 'IN-WAL', '')")
        connection.commit()
        # 故意不 checkpoint、也不关连接：数据此时在 -wal 里

        try:
            rows = mys_login._read_rows(path)
        finally:
            connection.close()

        self.assertTrue(any(row[2] == "IN-WAL" for row in rows), rows)

    def test_read_rows_cleans_up_its_temp_files(self):
        path = self._make_db([("passport-api.mihoyo.com", "ltoken", "TOKEN", b"")])

        mys_login._read_rows(path)

        leftovers = [name for name in os.listdir(tempfile.gettempdir())
                     if name.startswith("gi_login_") and name.endswith(".db")]
        self.assertFalse([name for name in leftovers if str(os.getpid()) in name], leftovers)

    def test_read_rows_reports_a_missing_file(self):
        with self.assertRaises(OSError):
            mys_login._read_rows(os.path.join(self.root, "nope", "Cookies"))


class CookieStringTests(unittest.TestCase):
    def test_order_is_stable(self):
        built = mys_login.build_cookie_string(
            {"ltoken": "T", "ltuid": "1", "cookie_token": "C"}
        )

        self.assertEqual(built, "ltuid=1; ltoken=T; cookie_token=C")

    def test_unknown_keys_are_appended_alphabetically(self):
        built = mys_login.build_cookie_string({"zzz": "1", "aaa": "2", "ltuid": "9"})

        self.assertEqual(built, "ltuid=9; aaa=2; zzz=1")


# ==========================================
# 🌟 扫码登录：建码 / 轮询 / 换 token
# ==========================================


class QrPhaseTests(unittest.TestCase):
    """状态归一。

    为什么值得单独测：米游社返回的**未扫码**状态是 `Created`（不是空串、也不是 `Init`）——
    一开始就是按猜的 `Init` 写的，结果轮询把每次回复都当成"未知"，白等两分钟。
    """

    def test_known_states(self):
        self.assertEqual(mys_login.qr_phase("Created"), "pending")
        self.assertEqual(mys_login.qr_phase(""), "pending")
        self.assertEqual(mys_login.qr_phase("Scanned"), "scanned")
        self.assertEqual(mys_login.qr_phase("Confirmed"), "confirmed")
        self.assertEqual(mys_login.qr_phase("Expired"), "expired")

    def test_unknown_state_is_not_silently_treated_as_scanned(self):
        self.assertEqual(mys_login.qr_phase("SomethingNew"), "unknown")


class QrLoginTests(unittest.TestCase):
    """建码 → 轮询 → 换 token 这条主路（网络全部打桩，验协议细节）。"""

    def setUp(self):
        mys_login.cancel_qr_login()
        self.addCleanup(mys_login.cancel_qr_login)

    def test_start_sends_the_plugin_shaped_request(self):
        """建码：body 里只放 device，DS 用 passport 那套；设备号必须是 32 位大写。

        这几个细节都是踩出来的（少一个就 -3005 / -3503），所以钉在用例里防止改回去。
        """
        calls = {}

        def fake_request(method, url, query="", body_obj=None, cookie=None, timeout=15, device=None):
            calls.update({"method": method, "url": url, "body": body_obj, "device": device})
            return {"url": "https://user.mihoyo.com/x?tk=ABC#/login/qr", "ticket": "ABC"}

        with patch("skills.mys_api.app_request", fake_request), \
             patch("skills.mys_api.pass_device_id", return_value="A" * 32):
            started = mys_login.start_qr_login()

        self.assertEqual(calls["method"], "POST")
        self.assertTrue(calls["url"].endswith("/app/createQRLogin"))
        self.assertEqual(calls["device"], "A" * 32)
        self.assertNotIn("app_id", calls["body"])          # app_id 只在请求头里
        self.assertEqual(started["ticket"], "ABC")

    def test_start_can_recover_the_ticket_from_the_url(self):
        """兜底：接口没给 ticket 时，从地址里抠 —— 别把结尾的 `#/login/qr` 一起抠进来。"""
        with patch("skills.mys_api.app_request",
                   return_value={"url": "https://user.mihoyo.com/x?expire=1&ticket=FROMURL&b=2#/qr"}), \
             patch("skills.mys_api.pass_device_id", return_value="B" * 32):
            started = mys_login.start_qr_login()

        self.assertEqual(started["ticket"], "FROMURL")

    def test_start_refuses_an_empty_qr(self):
        with patch("skills.mys_api.app_request", return_value={}), \
             patch("skills.mys_api.pass_device_id", return_value="C" * 32):
            with self.assertRaises(mys_login.LoginError):
                mys_login.start_qr_login()

    def test_query_reuses_the_same_device(self):
        """轮询必须复用建码时的设备号：换了就是 `-3503 风控`（实测）。"""
        devices = []

        def fake_request(method, url, query="", body_obj=None, cookie=None, timeout=15, device=None):
            devices.append(device)
            if len(devices) == 1:
                return {"url": "https://x?tk=T", "ticket": "T"}
            return {"status": "Created", "tokens": [], "user_info": {}}

        with patch("skills.mys_api.app_request", fake_request), \
             patch("skills.mys_api.pass_device_id", return_value="D" * 32):
            mys_login.start_qr_login()
            result = mys_login.query_qr_login()

        self.assertEqual(devices, ["D" * 32, "D" * 32])
        self.assertEqual(result["status"], "Created")
        self.assertEqual(mys_login.qr_phase(result["status"]), "pending")

    def test_query_without_a_qr_is_a_clear_error(self):
        with self.assertRaises(mys_login.LoginError):
            mys_login.query_qr_login()

    def test_query_maps_expiry_retcodes_to_expired(self):
        from skills import mys_api

        with patch("skills.mys_api.app_request",
                   return_value={"url": "https://x?tk=T", "ticket": "T"}), \
             patch("skills.mys_api.pass_device_id", return_value="E" * 32):
            mys_login.start_qr_login()

        with patch("skills.mys_api.app_request",
                   side_effect=mys_api.MysApiError(-3501, "二维码已过期")):
            result = mys_login.query_qr_login()

        self.assertEqual(result["status"], "Expired")

    def test_exchange_builds_a_v1_cookie(self):
        """换 token：拿 stoken 去换 **v1** ltoken —— 计算器认的就是它。"""
        seen = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"retcode": 0, "data": {"cookie_token": "CT"}}

        def fake_get(url, headers=None, timeout=None):
            seen["cookie_url"] = url
            return FakeResponse()

        def fake_request(method, url, query="", body_obj=None, cookie=None, timeout=15, device=None):
            seen["ltoken_query"] = query
            return {"ltoken": "LTOKEN"}

        import httpx

        with patch.object(httpx, "get", fake_get), \
             patch("skills.mys_api.app_request", fake_request):
            result = mys_login.exchange_tokens({
                "user_info": {"aid": 285006984, "mid": "MID"},
                "tokens": [{"name": "stoken", "token": "STOKEN"}],
            })

        self.assertIn("ltoken=LTOKEN", result["cookie"])
        self.assertIn("ltuid=285006984", result["cookie"])
        self.assertIn("cookie_token=CT", result["cookie"])
        self.assertIn("stoken=STOKEN", seen["cookie_url"])
        self.assertIn("uid=285006984", seen["ltoken_query"])
        self.assertTrue(mys_login.has_ltoken(result["cookie"]))

    def test_exchange_needs_a_stoken(self):
        with self.assertRaises(mys_login.LoginError):
            mys_login.exchange_tokens({"user_info": {"aid": 1}, "tokens": []})

    def test_exchange_reports_a_missing_ltoken(self):
        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"retcode": 0, "data": {}}

        import httpx

        with patch.object(httpx, "get", lambda *a, **k: FakeResponse()), \
             patch("skills.mys_api.app_request", return_value={}):
            with self.assertRaises(mys_login.LoginError) as caught:
                mys_login.exchange_tokens({
                    "user_info": {"aid": 1, "mid": "M"},
                    "tokens": [{"name": "stoken", "token": "S"}],
                })

        self.assertIn("ltoken", str(caught.exception))

    def test_poll_stops_on_expiry_instead_of_spinning(self):
        with patch.object(mys_login, "start_qr_login",
                          return_value={"url": "https://x?tk=T", "ticket": "T", "device": "D"}), \
             patch.object(mys_login, "query_qr_login",
                          return_value={"status": "Expired", "data": {}}):
            mys_login.start_qr_login()
            result = mys_login.poll_qr_login(timeout=10, interval=1)

        self.assertFalse(result["ok"])
        self.assertIn("过期", result["error"])

    def test_poll_exchanges_tokens_once_confirmed(self):
        with patch.object(mys_login, "start_qr_login",
                          return_value={"url": "https://x?tk=T", "ticket": "T", "device": "D"}), \
             patch.object(mys_login, "query_qr_login",
                          return_value={"status": "Confirmed", "data": {"tokens": [], "user_info": {}}}), \
             patch.object(mys_login, "exchange_tokens",
                          return_value={"cookie": "ltuid=1; ltoken=L", "ltuid": "1",
                                        "stoken_present": True}):
            mys_login.start_qr_login()
            result = mys_login.poll_qr_login(timeout=10, interval=1)

        self.assertTrue(result["ok"])
        self.assertIn("ltoken=L", result["cookie"])

    def test_login_by_qr_refreshes_an_expired_code(self):
        """二维码只活两分钟：第一张过期要自动再出一张，而不是让玩家重跑一遍。"""
        codes = []

        def fake_start():
            codes.append(len(codes) + 1)
            return {"url": f"https://x?tk=T{len(codes)}", "ticket": f"T{len(codes)}",
                    "device": "D"}

        answers = [{"ok": False, "error": "二维码已过期，请重新扫码", "cookie": ""},
                   {"ok": True, "error": "", "cookie": "ltuid=1; ltoken=L", "ltuid": "1"}]

        with patch.object(mys_login, "start_qr_login", fake_start), \
             patch.object(mys_login, "poll_qr_login", side_effect=answers), \
             patch.object(mys_login, "finish_login",
                          return_value={"ok": True, "step": "done", "cookie_masked": "lt***",
                                        "user": {}, "error": "", "notes": [], "saved": True}):
            result = mys_login.login_by_qr(save=False, quiet=True)

        self.assertTrue(result["ok"])
        self.assertEqual(len(codes), 2)

    def test_login_by_qr_gives_up_after_too_many_expired_codes(self):
        with patch.object(mys_login, "start_qr_login",
                          return_value={"url": "https://x?tk=T", "ticket": "T", "device": "D"}), \
             patch.object(mys_login, "poll_qr_login",
                          return_value={"ok": False, "error": "二维码已过期，请重新扫码",
                                        "cookie": ""}):
            result = mys_login.login_by_qr(save=False, quiet=True, retries=2)

        self.assertFalse(result["ok"])
        self.assertIn("过期", result["error"])

    def test_render_qr_ascii_draws_something_without_third_party_libs(self):
        """终端二维码**不能**依赖 `qrcode` 包（这台机器离线装不上）。"""
        art = mys_login.render_qr_ascii("https://example.com/hello")

        self.assertNotIn("需要 qrcode", art)
        self.assertIn("█", art)
        self.assertGreater(len(art.splitlines()), 10)

    def test_render_qr_svg_is_an_svg(self):
        svg = mys_login.render_qr_svg("https://example.com/hello")

        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("<path", svg)
        self.assertTrue(svg.endswith("</svg>"))


# ==========================================
# 🌟 完成登录：验证与保存
# ==========================================


class FinishLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = os.path.join(self.tmp.name, ".env")
        with open(self.env_path, "w", encoding="utf-8") as handle:
            handle.write("DEFAULT_UID=100000001\n")
        patcher = patch.object(config, "MYS_LOGIN_PROFILE_DIR",
                               os.path.join(self.tmp.name, "login"))
        patcher.start()
        self.addCleanup(patcher.stop)
        # ★ 把"进程里现有的 cookie"固定成空：`finish_login()` 现在会**合并**
        #   （App 扫码给 v1、网页扫码给 v2，合并才不会互相冲掉），不清空就会把
        #   你 `.env` 里那份真实 cookie 混进断言，还可能拿它去打米游社。
        cookie_patch = patch.object(config, "MYS_COOKIE", "")
        cookie_patch.start()
        self.addCleanup(cookie_patch.stop)

    def test_nothing_read_is_reported_with_next_steps(self):
        with patch.object(mys_login, "read_cookies", return_value={"cookies": {}, "notes": ["无记录"]}):
            result = mys_login.finish_login(env_path=self.env_path)

        self.assertFalse(result["ok"])
        self.assertIn("扫码", result["error"])

    def test_v2_only_cookie_is_verified_through_the_calculator(self):
        """只有 v2 的 cookie = 养成计算器网页的会话：**不当场拒绝**，改用计算器接口验。

        为什么不再一律拒绝：玩家在养成计算器网页上是**单独登录**的，那份会话就是 v2，
        而计算器接口认它；拿 `verifyLtoken` 验 v2 只会误报"不可用"。
        """
        v2_only = {"ltuid_v2": "1", "account_id_v2": "1", "ltoken_v2": "SECRET",
                   "cookie_token_v2": "C2"}
        calls = {}

        def fake_verify(cookie, timeout=20):
            calls["cookie"] = cookie
            return {"ok": True, "user": {"aid": "1"}, "error": "", "note": "能读到角色"}

        with patch.object(mys_login, "read_cookies",
                          return_value={"cookies": v2_only, "notes": []}), \
             patch.object(mys_login, "verify_calculator_cookie", fake_verify), \
             patch.object(mys_login, "verify_tokens") as app_verify:
            result = mys_login.finish_login(env_path=self.env_path)

        self.assertTrue(result["ok"], result)
        app_verify.assert_not_called()                # v2 不能用 verifyLtoken 验
        self.assertIn("ltoken_v2", calls["cookie"])
        self.assertIn("ltoken_v2", open(self.env_path, encoding="utf-8").read())
        self.assertEqual(result["mode"], "calculator")

    def test_a_cookie_without_any_token_is_not_saved(self):
        with patch.object(mys_login, "read_cookies",
                          return_value={"cookies": {"ltuid": "1"}, "notes": []}):
            result = mys_login.finish_login(env_path=self.env_path)

        self.assertFalse(result["ok"])
        self.assertFalse(result["saved"])
        self.assertNotIn("MYS_COOKIE=ltuid=1", open(self.env_path, encoding="utf-8").read())

    def test_verification_failure_does_not_save(self):
        good = {"ltuid": "285006984", "ltoken": "SECRET"}
        with patch.object(mys_login, "read_cookies", return_value={"cookies": good, "notes": []}), \
             patch.object(mys_login, "verify_tokens",
                          return_value={"ok": False, "error": "米游社说不可用", "user": {}}):
            result = mys_login.finish_login(env_path=self.env_path)

        self.assertFalse(result["ok"])
        self.assertFalse(result["saved"])
        self.assertNotIn("SECRET", open(self.env_path, encoding="utf-8").read())

    def test_success_saves_the_cookie_and_masks_it(self):
        good = {"ltuid": "285006984", "ltoken": "SECRETVALUE"}
        with patch.object(mys_login, "read_cookies", return_value={"cookies": good, "notes": []}), \
             patch.object(mys_login, "verify_tokens",
                          return_value={"ok": True, "user": {"aid": "285006984"}, "error": ""}):
            result = mys_login.finish_login(env_path=self.env_path)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["saved"])
        self.assertNotIn("SECRETVALUE", result["cookie_masked"])
        content = open(self.env_path, encoding="utf-8").read()
        self.assertIn("MYS_COOKIE=", content)
        self.assertIn("ltoken=SECRETVALUE", content)

    def test_manual_cookie_skips_the_browser_read(self):
        with patch.object(mys_login, "read_cookies") as fake_read, \
             patch.object(mys_login, "verify_tokens",
                          return_value={"ok": True, "user": {}, "error": ""}):
            result = mys_login.finish_login(cookie="ltuid=1; ltoken=X", save=False)

        fake_read.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertFalse(result["saved"])
        self.assertTrue(any("手动" in note for note in result["notes"]))


class StatusTests(unittest.TestCase):
    def test_status_reports_missing_keys(self):
        with patch.object(config, "MYS_COOKIE", "ltuid=1"):
            info = mys_login.status()

        self.assertTrue(info["configured"])
        self.assertFalse(info["complete"])
        self.assertEqual(info["missing"], ["ltoken/cookie_token"])

    def test_status_describes_the_qr_session(self):
        """状态里要带上"扫码那条路"的现状（能不能建码、当前这张码什么状态）。"""
        mys_login.cancel_qr_login()
        self.addCleanup(mys_login.cancel_qr_login)

        info = mys_login.status()

        self.assertTrue(info["qr_available"])
        self.assertEqual(info["qr"]["state"], "idle")
        self.assertEqual(info["qr"]["ticket"], "")
        self.assertIn("login_url", info)

    def test_status_reports_how_long_the_qr_is_still_good_for(self):
        """二维码两分钟就过期，状态里必须能看出还剩多久，否则前端只能瞎猜。"""
        mys_login._LIVE_QR.update({"ticket": "T", "device": "D", "state": "pending",
                                   "url": "https://x/y?expire=%d" % (int(time.time()) + 90)})
        self.addCleanup(mys_login.cancel_qr_login)

        self.assertGreater(mys_login.qr_expires_in(), 80)
        self.assertLessEqual(mys_login.qr_expires_in(), 91)

    def test_qr_expires_in_is_zero_without_a_url(self):
        mys_login.cancel_qr_login()
        self.addCleanup(mys_login.cancel_qr_login)

        self.assertEqual(mys_login.qr_expires_in(), 0)

    def test_has_ltoken_follows_the_cookie_audit(self):
        """`has_ltoken()` 已经放宽成"够不够用"：v1 与**计算器那套 v2** 都算够用。

        （要严格判"有没有 v1 ltoken"请看 `audit_cookie()["has_v1"]`。）
        """
        self.assertTrue(mys_login.has_ltoken("ltuid=1; ltoken=x"))
        self.assertTrue(mys_login.has_ltoken("ltuid_v2=1; ltoken_v2=x"))
        self.assertFalse(mys_login.has_ltoken("ltuid=1"))


class ExtractCookieTests(unittest.TestCase):
    """粘贴 cookie / 整段 cURL 都要能认出 cookie。

    为什么值得单独测：养成计算器网页的会话里有一串 `DEVICEFP` / `_MHYUUID` 之类的
    cookie，让玩家手抄就是等着漏 —— "复制整段 cURL"才是能用的交互。
    """

    def test_plain_cookie_line(self):
        text = "ltuid=1; ltoken=abc; cookie_token=def"
        self.assertEqual(mys_login.extract_cookie(text), text)

    def test_copy_as_curl(self):
        curl = ("curl 'https://api-takumi.mihoyo.com/event/e20200928calculate/v2/compute' \\\n"
                "  -H 'cookie: ltoken_v2=SECRET; ltuid_v2=1; DEVICEFP=abc' \\\n"
                "  -H 'content-type: application/json' \\\n"
                "  --data-raw '{\"avatar_id\":1}'")

        cookie = mys_login.extract_cookie(curl)

        self.assertIn("ltoken_v2=SECRET", cookie)
        self.assertIn("DEVICEFP=abc", cookie)
        self.assertNotIn("curl", cookie)

    def test_cookie_flag_form(self):
        curl = "curl 'https://x/y' -b 'ltuid=1; ltoken=abc' --compressed"
        self.assertEqual(mys_login.extract_cookie(curl), "ltuid=1; ltoken=abc")

    def test_multiline_paste(self):
        text = "ltuid=1;\nltoken=abc\ncookie_token=def"
        self.assertEqual(mys_login.extract_cookie(text), "ltuid=1; ltoken=abc; cookie_token=def")

    def test_a_curl_without_a_cookie_is_not_mistaken_for_one(self):
        self.assertEqual(mys_login.extract_cookie("curl 'https://x/y' --compressed"), "")


class DiagnoseTests(unittest.TestCase):
    """`--diagnose`：三条读取路径各试一遍，并列出库里**有哪些 cookie 名**（不解密）。

    为什么值得一条用例：玩家报"读不到"时，这个命令是唯一能一眼分清
    "cookie 不在" / "cookie 在但解不开" / "CDP 被拒绝" 的手段。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def _make_db(self):
        path = os.path.join(self.root, "Default", "Network", "Cookies")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
                           "encrypted_value BLOB)")
        connection.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?)", [
            ("passport-api.mihoyo.com", "ltoken", "PLAINTOKEN", b""),
            ("passport-api.mihoyo.com", "ltoken_v2", "", b"v10garbage"),
            ("example.com", "ltoken", "NOT-OURS", b""),
        ])
        connection.commit()
        connection.close()
        return path

    def test_diagnose_lists_cookie_names_without_decrypting(self):
        self._make_db()
        codes = []

        with patch.object(config, "MYS_LOGIN_PROFILE_DIR", self.root), \
             patch.object(mys_login, "cdp_alive", return_value=False), \
             patch.object(mys_login, "read_cookies_via_cdp",
                          return_value={"cookies": {}, "notes": ["还没打开登录窗口"],
                                        "via": "cdp"}), \
             patch("builtins.print", lambda *a, **k: codes.append(" ".join(str(x) for x in a))):
            code = mys_login._main(["--diagnose"])

        text = "\n".join(codes)
        self.assertEqual(code, 0)
        self.assertIn("ltoken", text)          # 名字要列出来
        self.assertIn("CDP", text)
        self.assertIn("结论", text)

    def test_diagnose_reports_cdp_hits(self):
        codes = []
        with patch.object(config, "MYS_LOGIN_PROFILE_DIR", self.root), \
             patch.object(mys_login, "cdp_alive", return_value=True), \
             patch.object(mys_login, "read_cookies_via_cdp",
                          return_value={"cookies": {"ltoken": "T"}, "notes": [], "via": "cdp"}), \
             patch("builtins.print", lambda *a, **k: codes.append(" ".join(str(x) for x in a))):
            mys_login._main(["--diagnose"])

        self.assertIn("1 条", "\n".join(codes))

    def test_diagnose_survives_a_missing_profile(self):
        codes = []
        with patch.object(config, "MYS_LOGIN_PROFILE_DIR", os.path.join(self.root, "nope")), \
             patch.object(mys_login, "cdp_alive", return_value=False), \
             patch.object(mys_login, "read_cookies_via_cdp",
                          return_value={"cookies": {}, "notes": [], "via": "cdp"}), \
             patch("builtins.print", lambda *a, **k: codes.append(" ".join(str(x) for x in a))):
            code = mys_login._main(["--diagnose"])

        self.assertEqual(code, 0)

    def test_devtools_launcher_prints_the_manual_steps(self):
        codes = []
        with patch.object(mys_login, "launch_login_window",
                          return_value={"ok": True, "pid": 1, "port": 2,
                                        "profile": "p", "browser": "b", "devtools": True}), \
             patch("builtins.print", lambda *a, **k: codes.append(" ".join(str(x) for x in a))):
            code = mys_login._main(["--devtools"])

        text = "\n".join(codes)
        self.assertEqual(code, 0)
        self.assertIn("Application", text)
        self.assertIn("ltoken", text)


class WebQrLoginTests(unittest.TestCase):
    """网页版扫码登录：拿的是**养成计算器**要的 v2 cookie（和 app 那套合并）。"""

    def setUp(self):
        mys_login.cancel_web_qr_login()

    def test_start_web_qr_login_returns_url_and_ticket(self):
        payload = {"retcode": 0, "message": "OK",
                   "data": {"url": "https://user.mihoyo.com/login-platform/mobile.html?tk=T1",
                            "ticket": "T1"}}
        with patch("skills.mys_api.web_request", return_value=(payload, None)) as call:
            started = mys_login.start_web_qr_login()
        self.assertEqual(started["ticket"], "T1")
        self.assertIn("user.mihoyo.com", started["url"])
        # 请求头必须带 x-rpc-app_id（缺了米游社回 -3001）
        headers = call.call_args.kwargs.get("headers") or {}
        self.assertEqual(headers.get("x-rpc-app_id"), mys_login.WEB_APP_ID)
        self.assertTrue(headers.get("x-rpc-device_id"))

    def test_start_web_qr_login_rejects_bad_retcode(self):
        payload = {"retcode": -3001, "message": "缺少参数"}
        with patch("skills.mys_api.web_request", return_value=(payload, None)):
            with self.assertRaises(mys_login.LoginError):
                mys_login.start_web_qr_login()

    def test_query_reads_cookies_from_set_cookie_headers(self):
        """令牌在**响应头**里（body 的 tokens 永远是空的），必须从头里抠。"""

        class FakeHeaders:
            def get_list(self, name):
                return ["ltuid_v2=123; Path=/; HttpOnly",
                        "ltoken_v2=TOKEN; Path=/",
                        "cookie_token_v2=CT; Path=/; Expires=Wed"]

        class FakeResponse:
            headers = FakeHeaders()

        payload = {"retcode": 0, "data": {"status": "Confirmed", "user_info": {"aid": "9"}}}
        mys_login._LIVE_WEB_QR.update({"ticket": "T1", "device": "D1"})
        with patch("skills.mys_api.web_request", return_value=(payload, FakeResponse())):
            result = mys_login.query_web_qr_login()
        self.assertEqual(result["status"], "Confirmed")
        self.assertEqual(result["cookies"]["ltoken_v2"], "TOKEN")
        self.assertEqual(result["cookies"]["ltuid_v2"], "123")
        self.assertNotIn("Path", result["cookies"])          # 属性不能被当成 cookie 键
        self.assertNotIn("Expires", result["cookies"])

    def test_query_without_ticket_is_rejected(self):
        with self.assertRaises(mys_login.LoginError):
            mys_login.query_web_qr_login()

    def test_expired_status_is_normalised(self):
        mys_login._LIVE_WEB_QR.update({"ticket": "T1", "device": "D1"})
        payload = {"retcode": -3501, "message": "二维码已失效"}
        with patch("skills.mys_api.web_request", return_value=(payload, None)):
            result = mys_login.query_web_qr_login()
        self.assertEqual(result["status"], "Expired")
        self.assertEqual(result["cookies"], {})

    def test_merge_cookie_keeps_both_generations(self):
        """合并后 v1（读体力）与 v2（算材料）都要在 —— 这就是"两个登录"的落点。"""
        merged = mys_login.merge_cookie("ltuid=1; ltoken=APP",
                                        "ltuid_v2=2; ltoken_v2=WEB; cookie_token_v2=CT")
        for piece in ("ltoken=APP", "ltoken_v2=WEB", "ltuid=1", "ltuid_v2=2"):
            self.assertIn(piece, merged)

    def test_merge_cookie_later_wins_on_conflict(self):
        merged = mys_login.merge_cookie("a=1; b=1", "b=2")
        self.assertIn("b=2", merged)
        self.assertNotIn("b=1", merged)

    def test_cookies_from_response_ignores_attributes(self):
        class FakeHeaders:
            def get_list(self, name):
                return ["ltoken_v2=X; Path=/; Domain=.mihoyo.com; Secure; HttpOnly"]

        class FakeResponse:
            headers = FakeHeaders()

        self.assertEqual(mys_api.cookies_from_response(FakeResponse()), {"ltoken_v2": "X"})

    def test_cookies_from_response_tolerates_missing_header_api(self):
        class FakeHeaders:
            def get(self, name, default=""):
                return "ltuid_v2=7; Path=/"

        class FakeResponse:
            headers = FakeHeaders()

        self.assertEqual(mys_api.cookies_from_response(FakeResponse()), {"ltuid_v2": "7"})


if __name__ == "__main__":
    unittest.main()
