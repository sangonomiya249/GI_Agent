"""扫码登录米游社：向米游社要一张二维码，玩家用手机扫码，我们直接换回 v1 `ltoken`。

## 为什么需要这个文件

米游社的"长期令牌" `ltoken` 是**战绩接口与养成计算器必须的**，而它**只在真正的登录流程里发放**。
从网页 cookie 里换是换不到的（实测：`ltoken_v2` 补成 v1 键名、放进 `x-rpc-ltoken` 头、
走 `getActionTokenByCookieToken` / `getWebTokensByAuthKey` 全都不行）。
最坑的是：**只带 v2 的 cookie 能通过公共接口**（能列出你名下的角色），
所以看起来"配好了"，但拉角色/算材料一律报"未登录 / 账号数据异常"。

## 主路径：扫码换 token（`start_qr_login` → `query_qr_login` → `exchange_tokens`）

```text
① createQRLogin（passport-api）        → 一张二维码（URL + ticket）
        ↓
② 玩家用手机「米游社」App 扫码 → 手机上点「确认登录」
        ↓
③ queryQRLoginStatus 轮询到 Confirmed → data 里带 stoken 和 aid/mid
        ↓
④ getLTokenBySToken 换 **v1 ltoken**（+ getCookieAccountInfoBySToken 换 cookie_token）
        ↓
⑤ verifyLtoken 验证真能用（**不验证就不算成功**）→ 组装 `k=v; k=v` 写进 .env 的 MYS_COOKIE
```

这条路**完全不碰浏览器、不碰 cookie 库、不涉及任何解密** —— 早先那套"开独立窗口读 cookie"
踩过的坑（App-Bound 加密、库被占用、CDP 被新版 Edge 拒）全都绕过去了。
那些老函数（`launch_login_window` / `read_cookies` / `_read_cookies_from_store`）留在这里
只作**手动抄 cookie 的兜底**（`--start / --devtools / --finish`），不再是推荐路径。

## 两个必须记住的细节（都是实测踩出来的）

* **设备号**：`x-rpc-device_id` 必须是 32 位大写、且建码与轮询**用同一个**
  —— 换了就是 `-3503 请求失败，当前设备或网络环境存在风险`；
* **`x-rpc-device_fp`（设备指纹）不能省** —— 少了它同样是 `-3503`。

## 安全边界（认真看）

* **绝不碰你的密码**：本模块不接收、不保存、不发送任何账号密码；扫码登录时凭据只在你手机和
  米游社之间，我们拿到的只是 `stoken` 换出来的 token；
* **cookie 只写进 `.env`**（`.gitignore` 已忽略），日志与接口响应里永远只出现掩码；
* **二维码只活两分钟左右**，过期就作废、重新出一张；用过的 ticket 我们会主动丢掉；
* 老兜底路径的边界：读 cookie 库时先复制一份再读、绝不写回，只挑 `mihoyo.com` 域，
  用的也是项目内的独立配置目录（`.studio-profile/mys-login`），不是你日常浏览器的 profile。

## 老路径的技术细节（保留给"手动抄 cookie"那条兜底）

* Chromium 的 cookie 值用 **AES-256-GCM** 加密，密钥本身用 **Windows DPAPI** 保护
  （`Local State` 里的 `os_crypt.encrypted_key`，去掉 `DPAPI` 前缀后再解）；
* Chromium 137+ 引入了 **App-Bound 加密（`v20` 前缀）**：那种密文需要浏览器的
  App-Bound 密钥（DPAPI 单独解不出来）。真遇到就明确告诉玩家"读不出来"，
  让他手动复制 —— **不要**假装成功；
* 浏览器运行时会锁住 cookie 库，所以读取失败要重试（`_copy_file_sharing` 用
  `FILE_SHARE_READ|WRITE|DELETE` 打开）。
"""

import asyncio
import base64
import contextlib
import ctypes
import ctypes.wintypes
import datetime
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time

import config

# ==========================================
# 🌟 常量
# ==========================================

# 登录页：玩家在这里用米游社 App 扫码
LOGIN_URL = "https://user.mihoyo.com/"

# 关心的 cookie 名（拿不到 ltoken 就等于没登录成功）
WANTED_KEYS = (
    "ltuid", "account_id", "ltuid_v2", "account_id_v2",
    "ltoken", "cookie_token", "ltoken_v2", "cookie_token_v2",
    "account_mid_v2", "ltmid_v2",
)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


# ==========================================
# 🌟 主路径：游戏账号扫码（能拿到 v1 ltoken）
# ==========================================
#
# ## 为什么必须是这一条（实测踩出来的）
#
# 网页登录（user.mihoyo.com）**只发 v2 那套**（`ltoken_v2` / `cookie_token_v2`），
# 而养成计算器与战绩接口**只认 v1**：
#   · 计算器 `/v1/sync/avatar/list` → `-100 请先登录后参与活动`
#   · 计算器 `/v2/compute`（算材料）→ `retcode 0` 但材料清单**是空的**（它的静默失败）
#   · 战绩 `character/list` → `5003`
# 我把 v2 值补成 v1 键名、放进 `x-rpc-ltoken` 头、去掉 DS……全都不行（都实测过）。
#
# 而**游戏账号扫码**这条路能拿到 v1 `ltoken`，流程（参考 xiaoyao-cvs-plugin 的实现）：
#
#   ① POST passport-api/account/ma-cn-passport/app/createQRLogin   → 拿 url(ticket)
#   ② POST passport-api/account/ma-cn-passport/app/queryQRLoginStatus 轮询
#          → status 从 Init → Scanned → Confirmed
#          → Confirmed 时返回 user_info(aid/mid) + tokens(含 stoken)
#   ③ GET  api-takumi/auth/api/getCookieAccountInfoBySToken?stoken=…&uid=…&mid=…
#          → cookie_token
#   ④ GET  passport-api/account/auth/api/getLTokenBySToken?stoken=…&uid=…&mid=…
#          → **ltoken**   ← 就是计算器要的那个
#
# 拼出来：`ltoken=…;ltuid=<aid>;cookie_token=…`
#
# ⚠️ ①②必须用 **App 那套 salt 与版本号**（`MYS_APP_SALT` / `MYS_APP_VERSION_APP`），
#    用网页那套一律"参数不合法"。
#
# ## 安全边界
# 只读登录态、不接收任何账号密码；stoken 只在本进程内存里用于换 token，**不落盘、不外传**。

PASSPORT_HOST = "https://passport-api.mihoyo.com"
TAKUMI_HOST = "https://api-takumi.mihoyo.com"

_LIVE_QR = {"ticket": "", "device": "", "url": "", "state": "idle", "message": ""}


class LoginError(Exception):
    """扫码登录不可用（环境缺浏览器 / 读不出 cookie 等）。"""


# ==========================================
# 🌟 兜底的 AES-GCM（纯标准库，无第三方依赖）
# ==========================================
#
# 为什么不直接用 `Crypto.Cipher.AES`：它是这个机器上**恰好**装了的库（pycryptodome），
# 并不是本项目的依赖，别的机器上不一定有；`cryptography` 同理，而本项目不能要求联网 pip。
# 所以这里放一份纯标准库实现兜底 —— **它用 NIST 官方向量验证过**
# （FIPS-197 附录 C.3 的 AES-256 单块 + SP 800-38D 的 GCM 向量，见 tests/test_mys_login.py）。
#
# ⚠️ 两个位序坑（都踩过，改之前先读）：
#   1. GCM 的 GF(2^128) 乘法：位串是**大端**，`x` 的第 i 位是 `(x >> (127 - i)) & 1`
#      —— 写成 `(x >> i) & 1` 会让 `_gf_mul(H, 1) != H`（而空明文的用例会掩盖它）；
#   2. AES 状态是**列主序**：`state[column][row] = block[4 * column + row]`。

_SBOX = (
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
)
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36, 0x6c, 0xd8, 0xab, 0x4d)
# GCM 的约简多项式 x^128 + x^7 + x^2 + x + 1
_GCM_R = 0xE1000000000000000000000000000000
_MASK128 = (1 << 128) - 1


def _xtime(value):
    value <<= 1
    return (value ^ 0x1b) & 0xff if value & 0x100 else value


def _mul(a, b):
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result & 0xff


def _expand_key(key):
    nk = len(key) // 4
    nr = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(nr + 1)], nr


def _encrypt_block(block, round_keys, nr):
    state = [[block[4 * column + row] for row in range(4)] for column in range(4)]

    def add_round_key(round_key):
        for column in range(4):
            for row in range(4):
                state[column][row] ^= round_key[4 * column + row]

    def sub_bytes():
        for column in range(4):
            for row in range(4):
                state[column][row] = _SBOX[state[column][row]]

    def shift_rows():
        for row in range(1, 4):
            values = [state[column][row] for column in range(4)]
            values = values[row:] + values[:row]
            for column in range(4):
                state[column][row] = values[column]

    def mix_columns():
        for column in range(4):
            a = list(state[column])
            state[column][0] = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
            state[column][1] = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
            state[column][2] = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
            state[column][3] = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)

    add_round_key(round_keys[0])
    for round_index in range(1, nr):
        sub_bytes()
        shift_rows()
        mix_columns()
        add_round_key(round_keys[round_index])
    sub_bytes()
    shift_rows()
    add_round_key(round_keys[nr])
    return bytes(state[column][row] for column in range(4) for row in range(4))


def _gf_mul(x, y):
    """GCM 的 GF(2^128) 乘法。

    规范（NIST SP 800-38D 6.3）：`x^i` 指的是 128 位块**位串**里的第 i 位，
    所以判断条件是 `(x >> (127 - i)) & 1`；写成 `(x >> i) & 1` 会得到一个
    "看似能用但结果是错的"乘法（`_gf_mul(H, 1) != H`）。
    """
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        v = ((v >> 1) ^ _GCM_R) if v & 1 else (v >> 1)
    return z & _MASK128


def _ghash(h_int, data):
    y = 0
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16].ljust(16, b"\x00")
        y = _gf_mul(y ^ int.from_bytes(chunk, "big"), h_int)
    return y


def _aes_gcm_decrypt_pure(key, nonce, ciphertext, tag, counter_start=2):
    """纯标准库的 AES-GCM 解密（校验 tag）。返回明文 bytes；tag 不对抛 ValueError。

    按 **NIST SP 800-38D 原始定义**实现：J0 = `nonce‖1`，密钥流从 `nonce‖2` 开始，
    `tag = GHASH(...) ^ E(K, J0)`。实测这条约定与 pycryptodome 在单块密文上完全一致。

    `counter_start` 只是为排查留的开关（另一套库用 `nonce‖0` 当 J0）。

    ⚠️ **只做单块（≤16 字节）**：多块的 GHASH 长度块口径各实现有差异，
    与其"看起来能解但偶尔出错"，不如显式拒绝，交给 pycryptodome / cryptography。
    cookie 值（`ltuid` / `ltoken` / `cookie_token`）都在这个范围内，够用。
    """
    if len(ciphertext) > 16:
        raise ValueError("内置实现只支持单块（≤16 字节）密文")

    round_keys, nr = _expand_key(key)
    h_int = int.from_bytes(_encrypt_block(bytes(16), round_keys, nr), "big")

    plaintext = bytearray()
    for index, offset in enumerate(range(0, len(ciphertext), 16), start=counter_start):
        keystream = _encrypt_block(nonce + struct.pack(">I", index), round_keys, nr)
        plaintext.extend(a ^ b for a, b in zip(ciphertext[offset:offset + 16], keystream))

    def pad(data):
        return data + b"\x00" * ((16 - len(data) % 16) % 16)

    auth = _ghash(h_int, pad(nonce) + pad(ciphertext)
                  + struct.pack(">QQ", 0, len(ciphertext) * 8))
    j0 = nonce + struct.pack(">I", counter_start - 1)
    expected = auth ^ int.from_bytes(_encrypt_block(j0, round_keys, nr), "big")
    if expected.to_bytes(16, "big") != tag:
        raise ValueError("GCM tag 校验失败")
    return bytes(plaintext)


# ==========================================
# 🌟 路径与浏览器
# ==========================================


def profile_dir():
    """独立浏览器配置目录（在项目里，方便清理；不是你日常浏览器的 profile）。"""
    return getattr(config, "MYS_LOGIN_PROFILE_DIR", "") or config.project_path(
        ".studio-profile", "mys-login"
    )


def find_browser():
    """找一个能用的 Chromium 内核浏览器（Edge 优先）。"""
    for path in EDGE_CANDIDATES:
        if os.path.isfile(path):
            return path
    for name in ("msedge", "chrome"):
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(directory, f"{name}.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def launch_login_window(url=None, devtools=False):
    """打开扫码登录窗口。返回 `{"ok", "pid", "port", "url", "profile", "browser", "devtools"}`。

    窗口是**普通的浏览器窗口**（不是无痕），并且带一个**只绑本机**的 DevTools 调试端口：
    登录完成后会尝试用 `Network.getAllCookies` 直接拿**明文** cookie
    （见文件顶部 `_CDP` 那段说明）。

    `devtools=True`：顺带把开发者工具打开（用于**手动兜底** ——
    有些 Edge 版本会拒绝 CDP 读取，那时玩家可以在 DevTools 里
    「Application → Cookies」直接复制，见 docs/MYS_COOKIE.md）。
    """
    browser = find_browser()
    if not browser:
        raise LoginError(
            "本机没找到 Edge / Chrome，扫码登录用不了。\n"
            "可以手动在浏览器里登录米游社，然后按 docs/MYS_COOKIE.md 复制 cookie。"
        )
    profile = profile_dir()
    os.makedirs(profile, exist_ok=True)
    port = _free_port()
    command = [
        browser,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        # 新版 Chromium 要求显式放行 websocket 来源，否则本地 CDP 连接会被拒
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--window-size=560,820",
    ]
    if devtools:
        command.append("--auto-open-devtools-for-tabs")
    command.append(url or LOGIN_URL)
    try:
        process = subprocess.Popen(command, close_fds=True, creationflags=CREATE_NO_WINDOW)
    except Exception as exc:            # noqa: BLE001
        raise LoginError(f"打开登录窗口失败：{type(exc).__name__} {exc}") from exc
    # 记下 PID 与端口：完成后按 PID 精确关闭（比"按进程名杀"安全得多）
    _LAUNCHED["pids"] = [process.pid]
    _CDP["port"] = port
    _CDP["process"] = process
    return {"ok": True, "pid": process.pid, "port": port, "url": url or LOGIN_URL,
            "profile": profile, "browser": browser, "devtools": bool(devtools)}


_LAUNCHED = {"pids": []}


def close_login_window():
    """关掉我们打开的那个登录窗口（**只关我们自己开的**，不会误杀玩家日常浏览器）。

    为什么要这么小心：直接 `taskkill /IM msedge.exe` 会把玩家正在用的浏览器一起杀掉。
    所以按两步走：
      ① 先按"我们启动时记下的 PID"关（最准，也不需要 WMI 权限）；
      ② 再退回按命令行里的配置目录匹配（覆盖"玩家自己又点了那个配置的快捷方式"这种情况）。
    两步都失败也没关系 —— 玩家手动关掉那个窗口即可，功能不受影响。
    """
    # ① 我们自己启动的那些进程（含子进程树）
    for pid in list(_LAUNCHED["pids"]):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=20)
            else:                       # pragma: no cover - 本项目只跑 Windows
                os.kill(pid, 15)
        except Exception:               # noqa: BLE001 —— 进程可能早就退了
            pass
    _LAUNCHED["pids"] = []

    # ② 兜底：按命令行里的配置目录匹配（WMI 被拒时静默跳过）
    #
    # ⚠️ 必须按配置目录**把整棵进程树**都收掉：Edge 的启动进程只是个 launcher，
    # 真正持有调试端口的是它的子进程；只杀 launcher 会出现"窗口关了但 CDP 还活着"
    # （实测踩过），下一次登录就会拿到旧端口、读到旧数据。
    marker = os.path.basename(profile_dir()) or "mys-login"
    script = (
        "$roots = Get-CimInstance Win32_Process -Filter "
        "\"Name='msedge.exe' or Name='chrome.exe'\" -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }}; "
        "foreach ($root in $roots) { "
        "  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "  Where-Object { $_.ParentProcessId -eq $root.ProcessId -or "
        "                 $_.ProcessId -eq $root.ProcessId } | "
        "  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=30)
    except Exception:                   # noqa: BLE001 —— 关不掉就让玩家自己关
        pass
    _CDP["port"] = 0
    _CDP["process"] = None
    return {"ok": True, "profile_matched": marker}


# ==========================================
# 🌟 读 cookie 库（DPAPI + AES-GCM）
# ==========================================


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_unprotect(blob, entropy=None):
    """Windows DPAPI 解密（解 Chromium 的 cookie 加密密钥）。"""
    buffer_in = _DataBlob(len(blob), ctypes.cast(ctypes.create_string_buffer(blob),
                                                 ctypes.POINTER(ctypes.c_char)))
    buffer_out = _DataBlob()
    entropy_blob = None
    entropy_ptr = None
    if entropy:
        entropy_blob = _DataBlob(len(entropy),
                                 ctypes.cast(ctypes.create_string_buffer(entropy),
                                             ctypes.POINTER(ctypes.c_char)))
        entropy_ptr = ctypes.byref(entropy_blob)
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(buffer_in), None, entropy_ptr, None, None, 0, ctypes.byref(buffer_out)
    )
    if not ok:
        raise LoginError(
            "Windows DPAPI 解密失败（拿不到 cookie 加密密钥）。"
            "如果是在受限账户 / 沙箱里运行，扫码登录读不了 cookie，请改用手动复制。"
        )
    try:
        return ctypes.string_at(buffer_out.pbData, buffer_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(buffer_out.pbData)


def cookie_db_paths(root=None):
    """列出可能的 cookie 库路径（Chromium 各版本的目录结构不太一样）。"""
    root = root or profile_dir()
    candidates = []
    for profile in ("Default", "Profile 1", "Profile 2"):
        candidates.append(os.path.join(root, profile, "Network", "Cookies"))
        candidates.append(os.path.join(root, profile, "Cookies"))
    # WebView2 的结构多一层 EBWebView
    for profile in ("Default", "Profile 1"):
        candidates.append(os.path.join(root, "EBWebView", profile, "Network", "Cookies"))
    candidates.append(os.path.join(root, "EBWebView", "Default", "Network", "Cookies"))
    return [path for path in candidates if os.path.isfile(path)]


def cookie_key(root=None):
    """取出 Chromium 用来加密 cookie 的主密钥（DPAPI 保护）。"""
    root = root or profile_dir()
    state_path = os.path.join(root, "Local State")
    with open(state_path, encoding="utf-8") as handle:
        state = json.load(handle)
    os_crypt = state.get("os_crypt") or {}
    encrypted_key = os_crypt.get("encrypted_key")
    if not encrypted_key:
        raise LoginError("这个浏览器配置里没有 cookie 加密密钥（Local State 里找不到 encrypted_key）")
    raw = base64.b64decode(encrypted_key)
    if raw[:5] != b"DPAPI":
        raise LoginError("cookie 加密密钥的格式不认识（不是 DPAPI 保护的）")
    return _dpapi_unprotect(raw[5:])


def _looks_like_cookie_value(text):
    """解出来的东西像不像一个 cookie 值（用来在两种 GCM 计数器约定里挑对的那个）。

    为什么必须做这个检查：计数器约定用错时 **tag 依然能对上**（因为 tag 用的是 J0，
    而 J0 在两种约定下都是"起始计数器减一"），只有明文是乱码 ——
    所以"解密成功"不等于"解对了"。
    """
    if not text:
        return False
    if any(ord(char) < 0x20 and char not in "\t" for char in text):
        return False            # 控制字符 = 几乎肯定是乱码
    return len(text) <= 4096


def _aes_gcm_decrypt(key, nonce, ciphertext, tag):
    """AES-GCM 解密 + 校验 tag + **合理性检查**。返回明文 bytes。

    依次尝试：pycryptodome → cryptography → 内置纯 Python（两种计数器约定都试）。
    任何一步失败都继续往下试；全失败抛 `LoginError`（**绝不放行未经验证的密文**）。
    """
    errors = []

    def acceptable(value, where):
        text = value.decode("utf-8", "replace")
        if _looks_like_cookie_value(text):
            return text
        errors.append(f"{where}: 解出来不像 cookie 值")
        return None

    try:
        from Crypto.Cipher import AES

        text = acceptable(AES.new(key, AES.MODE_GCM, nonce=nonce)
                          .decrypt_and_verify(ciphertext, tag), "pycryptodome")
        if text is not None:
            return text.encode("utf-8")
    except ImportError:
        pass
    except Exception as exc:            # noqa: BLE001 —— tag 不对等
        errors.append(f"pycryptodome: {type(exc).__name__}")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        text = acceptable(AESGCM(key).decrypt(nonce, ciphertext + tag, None), "cryptography")
        if text is not None:
            return text.encode("utf-8")
    except ImportError:
        pass
    except Exception as exc:            # noqa: BLE001
        errors.append(f"cryptography: {type(exc).__name__}")

    # 内置实现：只做单块（≤16 字节），两种计数器约定都试，用"像不像 cookie"定夺
    for counter_start in (2, 1):
        try:
            value = _aes_gcm_decrypt_pure(key, nonce, ciphertext, tag,
                                          counter_start=counter_start)
        except Exception as exc:        # noqa: BLE001
            errors.append(f"内置实现(counter={counter_start}): {type(exc).__name__}")
            continue
        text = acceptable(value, f"内置实现(counter={counter_start})")
        if text is not None:
            return text.encode("utf-8")

    raise LoginError("cookie 解密失败（" + "；".join(errors) + "）")


def decrypt_cookie_value(blob, key):
    """解密一条 cookie 值。返回 `(明文, 说明)`；解不开时明文为空、说明是原因。"""
    if not blob:
        return "", ""
    if isinstance(blob, str):
        blob = blob.encode("utf-8", "replace")
    prefix = blob[:3]

    if prefix in (b"v10", b"v11"):
        # 结构：[3 字节版本][12 字节 nonce][密文][16 字节 tag]
        nonce = blob[3:15]
        ciphertext = blob[15:-16]
        tag = blob[-16:]
        if not ciphertext and not tag:
            return "", "cookie 密文是空的"
        try:
            return _aes_gcm_decrypt(key, nonce, ciphertext, tag).decode("utf-8", "replace"), ""
        except LoginError as exc:
            return "", str(exc)
    if prefix == b"v20":
        # Chromium 137+ 的 App-Bound 加密：密钥绑在浏览器安装上，DPAPI 单独解不出来。
        return "", ("这个浏览器用新版 App-Bound 加密（v20）保存 cookie，本工具读不出来 —— "
                    "请改用手动复制，见 docs/MYS_COOKIE.md")

    try:
        return blob.decode("utf-8", "replace"), ""
    except Exception:                   # noqa: BLE001
        return "", "cookie 值的格式不认识"


def _copy_file_sharing(source, target, attempts=6, delay=0.35):
    """复制一个**可能正被浏览器占用**的文件。

    为什么不能直接用 `shutil.copy2`：Chromium 一直开着 cookie 库的写句柄，
    在 Windows 上那等于"独占"，复制会直接 `PermissionError` ——
    玩家看到的就是"库被占用，请关掉登录窗口再试"，而**根本不需要关窗口**。

    这里改成用 `CreateFileW` **以共享读的方式**打开（`FILE_SHARE_READ | WRITE | DELETE`），
    再自己分块读干净。浏览器照常运行，我们照常读。
    """
    import ctypes
    from ctypes import wintypes

    share_all = 0x00000001 | 0x00000002 | 0x00000004      # READ | WRITE | DELETE
    open_existing = 3
    generic_read = 0x80000000
    invalid = ctypes.c_void_p(-1).value

    last_error = None
    for attempt in range(int(attempts)):
        handle = None
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateFileW.restype = wintypes.HANDLE
            handle = kernel32.CreateFileW(str(source), generic_read, share_all, None,
                                          open_existing, 0x80, None)
            if handle in (None, invalid, 0):
                raise OSError(f"打开共享读失败（err={ctypes.GetLastError()}）")
            with open(target, "wb") as out:
                buffer = ctypes.create_string_buffer(1 << 20)
                read = wintypes.DWORD(0)
                while True:
                    ok = kernel32.ReadFile(handle, buffer, len(buffer),
                                           ctypes.byref(read), None)
                    if not ok or read.value == 0:
                        break
                    out.write(buffer.raw[:read.value])
            return True
        except OSError as exc:
            last_error = exc
            if attempt + 1 < int(attempts):
                time.sleep(delay)
        finally:
            if handle not in (None, invalid, 0):
                with contextlib.suppress(Exception):
                    ctypes.windll.kernel32.CloseHandle(handle)

    raise OSError(str(last_error) if last_error else "复制失败")


def _read_rows(path):
    """把 cookie 库（连同 WAL）复制到临时文件后读出所有行。

    顺带解决两件事：
      · **WAL**：Chromium 的库是 WAL 模式，刚写入的 cookie 可能还在 `-wal` 里 ——
        只复制主库会读到旧数据（表现是"刚扫码登录却读不到 cookie"），所以一起复制；
      · **失败可重试**：浏览器偶尔正在 checkpoint，重试几次就好，不用让玩家动手。
    """
    directory = os.path.dirname(path)
    stamp = f"{os.getpid()}_{abs(hash(path)) & 0xFFFFFF}"
    temp_db = os.path.join(tempfile.gettempdir(), f"gi_login_{stamp}.db")
    temp_files = [temp_db]
    for suffix in ("-wal", "-shm"):
        side = path + suffix
        if os.path.isfile(side):
            target = temp_db + suffix
            temp_files.append(target)
            try:
                _copy_file_sharing(side, target, attempts=2, delay=0.2)
            except OSError:
                pass                          # WAL 读不到也能读主库，不阻断

    try:
        _copy_file_sharing(path, temp_db)
        connection = sqlite3.connect(temp_db)
        try:
            rows = connection.execute(
                "SELECT host_key, name, value, encrypted_value FROM cookies"
            ).fetchall()
        finally:
            connection.close()
        return rows
    except sqlite3.DatabaseError as exc:
        # 主库是好的、只是 WAL 状态冲突时，用不可变模式再试一次（只读快照，不碰锁）
        try:
            connection = sqlite3.connect(f"file:{temp_db}?immutable=1", uri=True)
            try:
                return connection.execute(
                    "SELECT host_key, name, value, encrypted_value FROM cookies"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            raise exc
    finally:
        for temp in temp_files:
            try:
                os.remove(temp)
            except OSError:
                pass


# ==========================================
# 🌟 扫码登录主流程（游戏账号口径 → 拿 v1 ltoken）
# ==========================================


def _random_device(length=16):
    """随机设备号（**浏览器那条路**用的，形如 uuid；passport 那条路用 `pass_device_id()`）。"""
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def start_qr_login(timeout=20):
    """① 创建登录二维码。返回 `{"url", "ticket", "device"}`。

    `url` 是要**渲染成二维码给玩家扫**的地址；`ticket` 用来轮询状态。

    ⚠️ body 里**不带** `app_id`（它只出现在请求头 `x-rpc-app_id` 里，且必须是字符串）；
       设备号也不进 body —— 它只在 `x-rpc-device_id` 请求头里，两次请求必须**同一个**。
       实测 body 里塞 app_id 会变成 `-3005 参数不合法`，换设备号会变成 `-3503 风控`。
    """
    from skills import mys_api

    device = mys_api.pass_device_id()
    data = mys_api.app_request("POST", f"{PASSPORT_HOST}/account/ma-cn-passport/app/createQRLogin",
                               body_obj={}, timeout=timeout, device=device)
    url = str(data.get("url") or "")
    ticket = str(data.get("ticket") or "")
    if not ticket and "ticket=" in url:
        # 兜底从地址里抠：注意结尾还有 `#/login/qr` 这种 hash 片段，别把它一起抠进来
        match = re.search(r"[?&#]ticket=([^&#]+)", url)
        ticket = match.group(1) if match else ""
    if not url or not ticket:
        raise LoginError(f"米游社没返回二维码（data={str(data)[:120]}）")
    _LIVE_QR.update({"ticket": ticket, "device": device, "url": url, "state": "init",
                     "message": ""})
    return {"url": url, "ticket": ticket, "device": device}


def query_qr_login(device=None, ticket=None, timeout=20):
    """② 轮询扫码状态。返回 `{"status", "data"}`。

    `status` 是米游社原样返回的英文状态（实测取值）：
      · `Created`  —— 二维码已生成，还没人扫；
      · `Scanned`  —— 扫了，等手机端点「确认登录」；
      · `Confirmed`—— 确认了，`data` 里带 `tokens`（stoken）和 `user_info`（aid/mid）；
      · `Expired`  —— 过期（米游社用 retcode `-3501` / `-106` 表达，这里归一成同一个词）。

    判定阶段请用 `qr_phase()`，别自己猜字符串。

    ⚠️ **必须用与创建二维码时同一个 `device`**：换了设备号米游社直接拒绝，
    报 `-3503 请求失败，当前设备或网络环境存在风险`（实测踩过）。
    """
    from skills import mys_api

    if not (ticket or _LIVE_QR.get("ticket")):
        raise LoginError("还没有二维码，先调 start_qr_login()")
    resolved_device = device or _LIVE_QR.get("device") or mys_api.pass_device_id()
    body = {"ticket": ticket or _LIVE_QR["ticket"]}
    try:
        data = mys_api.app_request(
            "POST", f"{PASSPORT_HOST}/account/ma-cn-passport/app/queryQRLoginStatus",
            body_obj=body, timeout=timeout, device=resolved_device,
        )
    except mys_api.MysApiError as exc:
        # 二维码过期是"正常"的结束方式，不该当成崩溃
        if exc.retcode in (-3501, -106):
            return {"status": "Expired", "data": {}}
        raise
    status = str(data.get("status") or "")
    _LIVE_QR["state"] = qr_phase(status)
    return {"status": status, "data": data}


# 米游社原样返回的状态 → 我们要的四个阶段。
# `Created` 是**没扫码**时的默认值（不是空字符串）—— 一开始我们猜成 `Init`，
# 轮询会一直当成"未知状态"，所以这里显式写全。
_QR_PENDING = ("", "Created", "Init", "created", "init")


def qr_phase(status):
    """把米游社的原始状态归一成 `pending` / `scanned` / `confirmed` / `expired` / `unknown`。"""
    text = str(status or "").strip()
    if text in _QR_PENDING:
        return "pending"
    lowered = text.lower()
    if lowered in ("scanned", "scan"):
        return "scanned"
    if lowered in ("confirmed", "confirm"):
        return "confirmed"
    if lowered in ("expired", "expire"):
        return "expired"
    return "unknown"


def exchange_tokens(qr_data, timeout=20):
    """③④ 用扫码结果换 `cookie_token` 与 **v1 `ltoken`**。

    返回 `{"cookie": "...", "ltuid": "...", "stoken_present": True}`。
    """
    from skills import mys_api

    user_info = (qr_data or {}).get("user_info") or {}
    aid = str(user_info.get("aid") or user_info.get("uid") or user_info.get("account_id") or "")
    mid = str(user_info.get("mid") or "")
    tokens = (qr_data or {}).get("tokens") or []
    stoken = ""
    for item in tokens:
        if str(item.get("name") or "") in ("stoken", "stoken_v2"):
            stoken = str(item.get("token") or "")
            break
    if not stoken and tokens:
        stoken = str(tokens[0].get("token") or "")
    if not (aid and stoken):
        raise LoginError("扫码结果里没有 stoken / 账号 id（可能没点确认就超时了）")

    # ③ cookie_token
    cookie_token = ""
    try:
        stripped = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
        import httpx

        query = f"game_biz=hk4e_cn&stoken={stoken}&uid={aid}&mid={mid}"
        response = httpx.get(
            f"{TAKUMI_HOST}/auth/api/getCookieAccountInfoBySToken?{query}",
            headers=stripped, timeout=timeout,
        )
        cookie_token = str(((response.json().get("data") or {}).get("cookie_token")) or "")
    except Exception as exc:            # noqa: BLE001 —— 拿不到就少一个键，不阻断
        print(f"⚠️ 取 cookie_token 失败（不影响 ltoken）：{type(exc).__name__}")

    # ④ ltoken（**计算器与战绩接口要的就是它**）
    ltoken = ""
    try:
        data = mys_api.app_request(
            "GET", f"{PASSPORT_HOST}/account/auth/api/getLTokenBySToken",
            query=f"stoken={stoken}&uid={aid}&mid={mid}", timeout=timeout,
        )
        ltoken = str(data.get("ltoken") or "")
    except mys_api.MysError as exc:
        raise LoginError(f"换取 ltoken 失败：{exc}") from exc
    if not ltoken:
        raise LoginError("米游社没返回 ltoken（可能是 stoken 无效，重新扫码试试）")

    parts = [f"ltuid={aid}", f"ltoken={ltoken}"]
    if cookie_token:
        parts.append(f"cookie_token={cookie_token}")
    _LIVE_QR["state"] = "done"
    return {"cookie": "; ".join(parts), "ltuid": aid, "stoken_present": True}


def poll_qr_login(timeout=180, interval=3):
    """阻塞式轮询直到确认（给命令行用）。返回 `{"ok", "cookie", "error"}`。"""
    from skills import mys_api

    deadline = time.time() + max(10, int(timeout))
    scanned_notified = False
    while time.time() < deadline:
        try:
            result = query_qr_login()
        except mys_api.MysError as exc:
            return {"ok": False, "error": str(exc), "cookie": ""}
        phase = qr_phase(result["status"])
        if phase == "pending":
            _LIVE_QR["message"] = "等待扫码…"
        if phase == "scanned" and not scanned_notified:
            scanned_notified = True
            _LIVE_QR["message"] = "已扫码，请在手机上点「确认登录」"
            print("📱 已扫码，请在手机上点「确认登录」…")
        if phase == "confirmed":
            _LIVE_QR["message"] = "已确认，正在换取 cookie…"
            try:
                return {"ok": True, "error": "", **exchange_tokens(result["data"])}
            except LoginError as exc:
                return {"ok": False, "error": str(exc), "cookie": ""}
        if phase == "expired":
            _LIVE_QR["message"] = "二维码已过期"
            return {"ok": False, "error": "二维码已过期，请重新扫码", "cookie": ""}
        time.sleep(max(1, int(interval)))
    return {"ok": False, "error": "等待确认超时", "cookie": ""}


def login_by_qr(timeout=180, save=True, env_path=None, quiet=False, retries=3):
    """一条龙：建二维码 → 画在终端 → 轮询 → 换 token → 验证 → （可选）写 `.env`。

    ⚠️ 二维码**只活两分钟左右**，所以这里会自动重出（最多 `retries` 张）：
    命令行里玩家可能正低头找手机，第一张过期是常态，不该让他重跑一遍命令。
    `quiet=True` 时不打印二维码图案（Studio 会把 SVG 交给前端画）。
    """
    from skills import mys_api

    for attempt in range(max(1, int(retries))):
        started = start_qr_login()
        if not quiet:
            if attempt:
                print()
            print(f"请用**米游社 App** 扫码登录（这张码约两分钟有效）：")
            print(render_qr_ascii(started["url"]))
            print(f"扫码地址：{started['url']}")
        # 别傻等超过二维码的寿命：倒计时还没到就自己"过期重来"，白等一轮不如早点换
        budget = qr_expires_in()
        wait = min(int(timeout), budget + 5) if budget else int(timeout)
        polled = poll_qr_login(timeout=max(30, wait))
        if polled["ok"]:
            break
        # 只有"过期"值得重来；其它错误（网络、风控）重试也没意义
        if polled["error"] != "二维码已过期，请重新扫码":
            return {"ok": False, "error": polled["error"], "cookie_masked": ""}
        if not quiet:
            print("（这张二维码过期了，换一张…）")
    else:
        return {"ok": False, "error": "二维码一直没人扫（换了几张都过期了）", "cookie_masked": ""}

    result = finish_login(cookie=polled["cookie"], save=save, env_path=env_path)
    result["ltuid"] = polled.get("ltuid", "")
    return result


# ==========================================
# 🌟 网页扫码登录：拿**养成计算器**要的那套 v2 cookie
# ==========================================

# 米游社**网页版**通行证（和上面 app 版是两个不同的入口）。
# 为什么要两条路：app 版换取的是 v1 `ltoken`（战绩 / 体力接口认它），
# 网页版扫码确认后返回的 `Set-Cookie` 里是 `ltoken_v2` / `ltuid_v2` /
# `cookie_token_v2` —— 那才是**养成计算器**用的会话。两边各要一份，缺一不可：
#   · 只有计算器 cookie（v2）→ 算得了材料，读不了体力（dailyNote 回"账号数据异常"）；
#   · 只有扫码 cookie（v1）→ 读得了体力/战绩，养成计算器可能未登录。
# 所以最终 `.env` 里那份 MYS_COOKIE 应该是**两边合并**的结果（见 `merge_cookie`）。
WEB_PASSPORT_HOST = "https://passport-api.miyoushe.com"
WEB_APP_ID = "bll8iq97cem8"
WEB_CLIENT_TYPE = "4"
_WEB_PATH_CREATE = "/account/ma-cn-passport/web/createQRLogin"
_WEB_PATH_STATUS = "/account/ma-cn-passport/web/queryQRLoginStatus"

# 网页登录会发下来的 cookie 键（按重要性排；`account_id_v2` 是账号 id）
WEB_COOKIE_KEYS = (
    "ltoken_v2", "ltuid_v2", "ltmid_v2", "account_id_v2", "account_mid_v2",
    "cookie_token_v2",
)

_LIVE_WEB_QR = {"ticket": "", "device": "", "url": "", "state": "idle", "message": "",
                "cookies": {}}


def _web_headers(device):
    """网页通行证接口要的请求头（缺 `x-rpc-app_id` 会回 -3001）。"""
    return {
        "x-rpc-app_id": WEB_APP_ID,
        "x-rpc-client_type": WEB_CLIENT_TYPE,
        "x-rpc-device_id": str(device or ""),
        "Referer": "https://user.mihoyo.com/",
    }


def start_web_qr_login(timeout=20):
    """① 创建**网页版**登录二维码。返回 `{"url", "ticket", "device"}`。

    实测这个接口**不需要先登录、也不需要 cookie**，body 传空对象即可；
    返回的 `url` 渲染成二维码给玩家用米游社 App 扫。
    """
    from skills import mys_api

    device = _random_device(16).upper()
    payload, _response = mys_api.web_request(
        "POST", f"{WEB_PASSPORT_HOST}{_WEB_PATH_CREATE}",
        body_obj={}, headers=_web_headers(device), timeout=timeout,
    )
    if payload.get("retcode") != 0:
        raise LoginError(f"网页二维码创建失败：{payload.get('retcode')} "
                         f"{payload.get('message')}")
    data = payload.get("data") or {}
    url = str(data.get("url") or "")
    ticket = str(data.get("ticket") or "")
    if not url or not ticket:
        raise LoginError(f"网页版没返回二维码（data={str(data)[:120]}）")
    _LIVE_WEB_QR.update({"ticket": ticket, "device": device, "url": url, "state": "init",
                         "message": "", "cookies": {}})
    return {"url": url, "ticket": ticket, "device": device}


def query_web_qr_login(ticket=None, device=None, timeout=20):
    """② 轮询网页版扫码状态；**确认后从 `Set-Cookie` 里取 v2 cookie**。

    返回 `{"status", "cookies", "user_info"}`。状态取值与 app 版一致
    （`Created` / `Scanned` / `Confirmed`），过期用 `-3501` / `-3505` 表达。
    """
    from skills import mys_api

    ticket = ticket or _LIVE_WEB_QR.get("ticket") or ""
    device = device or _LIVE_WEB_QR.get("device") or ""
    if not ticket:
        raise LoginError("还没有网页二维码，先调 start_web_qr_login()")
    payload, response = mys_api.web_request(
        "POST", f"{WEB_PASSPORT_HOST}{_WEB_PATH_STATUS}",
        body_obj={"ticket": ticket}, headers=_web_headers(device), timeout=timeout,
    )
    retcode = payload.get("retcode")
    if retcode in (-3501, -3505, -106):
        _LIVE_WEB_QR["state"] = "expired"
        return {"status": "Expired", "cookies": {}, "user_info": {}}
    if retcode != 0:
        raise LoginError(f"网页扫码状态查询失败：{retcode} {payload.get('message')}")
    data = payload.get("data") or {}
    status = str(data.get("status") or "")
    cookies = {}
    if status == "Confirmed":
        # ★ 关键：令牌在响应头里，不在 body 里（body 的 tokens 永远是空的）
        cookies = mys_api.cookies_from_response(response)
        cookies = {key: value for key, value in cookies.items()
                   if key in WEB_COOKIE_KEYS}
        _LIVE_WEB_QR["cookies"] = dict(cookies)
    _LIVE_WEB_QR["state"] = qr_phase(status)
    return {"status": status, "cookies": cookies, "user_info": data.get("user_info") or {}}


def poll_web_qr_login(timeout=180, interval=3):
    """③ 一直轮询到确认 / 过期（Studio 那头是前端轮询，这条给 CLI 用）。"""
    deadline = time.time() + max(5, int(timeout))
    while time.time() < deadline:
        result = query_web_qr_login()
        status = result.get("status") or ""
        if status == "Confirmed":
            return result
        if status == "Expired":
            raise LoginError("网页二维码已过期，请重新生成")
        time.sleep(max(1, int(interval)))
    raise LoginError("等待扫码超时")


def merge_cookie(*cookies):
    """把几份 cookie 合并成一份（**后给的覆盖前面的键**）。

    为什么需要：app 扫码给 v1（`ltoken`/`ltuid`/`cookie_token`），网页扫码给 v2
    （`ltoken_v2` 那一套）。玩家要的"养成计算器单独登录"不该把米游社那份挤掉 ——
    两份**键名不冲突**，合并之后同一份 MYS_COOKIE 就能同时算材料 + 读体力。
    """
    merged = {}
    order = []
    for text in cookies:
        for chunk in str(text or "").split(";"):
            chunk = chunk.strip()
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key:
                continue
            if key not in merged:
                order.append(key)
            merged[key] = value
    return "; ".join(f"{key}={merged[key]}" for key in order)


def login_by_web_qr(timeout=180, save=True, env_path=None, quiet=False, retries=3):
    """网页扫码登录一条龙：建码 → 轮询 → 取 v2 cookie → **合并进 .env**。

    返回 `{"ok", "cookie", "keys", "calculator_ok", "message"}`。
    `calculator_ok` 是拿新 cookie 真的去问一次养成计算器的结果
    —— 只有它通了才算成功（不靠"接口返回了 cookie"这种间接证据）。

    ⚠️ 合并而不是覆盖：原来那份 cookie 里的 v1 键（读体力要用）必须留着。
    """
    for attempt in range(max(1, int(retries))):
        try:
            started = start_web_qr_login()
            if not quiet:
                print(render_qr_ascii(started["url"]))
                print("📱 用米游社 App 扫码并在手机上确认（这是**网页版**登录，"
                      "给养成计算器用）")
            result = poll_web_qr_login(timeout=timeout)
            cookies = result.get("cookies") or {}
            if not cookies:
                raise LoginError("确认了但没拿到 cookie（米游社改版？）")
            web_cookie = "; ".join(f"{key}={cookies[key]}" for key in WEB_COOKIE_KEYS
                                   if cookies.get(key))
            existing = config.MYS_COOKIE or ""
            final = merge_cookie(existing, web_cookie)
            ok_calc, note = verify_calculator_cookie(final)
            saved = False
            if save and ok_calc:
                saved = _save_cookie(final, env_path)
                if saved:
                    finish_login(final, save=False)
            return {
                "ok": bool(ok_calc),
                "cookie": final,
                "keys": sorted(cookies),
                "calculator_ok": bool(ok_calc),
                "saved": saved,
                "message": ("✅ 网页登录成功，养成计算器已可用" + ("（已写入 .env）" if saved else "")
                            if ok_calc else f"⚠️ 拿到 cookie 但计算器仍不可用：{note}"),
            }
        except LoginError:
            if attempt + 1 >= max(1, int(retries)):
                raise
            time.sleep(1)
    raise LoginError("网页扫码登录失败")


def web_qr_state():
    """给界面看的网页扫码状态。"""
    return dict(_LIVE_WEB_QR)


def cancel_web_qr_login():
    _LIVE_WEB_QR.update({"ticket": "", "device": "", "url": "", "state": "idle",
                         "message": "", "cookies": {}})
    return True


def _save_cookie(cookie, env_path=None):
    """把 cookie 写回 `.env`（只改 `MYS_COOKIE` 一行）。"""
    try:
        import skills.env_config as env_config

        env_config.update_values({"MYS_COOKIE": cookie}, path=env_path)
        config.MYS_COOKIE = cookie
        return True
    except Exception as exc:                # noqa: BLE001
        print(f"⚠️ 写 MYS_COOKIE 失败（{type(exc).__name__}: {exc}）")
        return False


# ==========================================
# 🌟 二维码渲染
# ==========================================


def render_qr_ascii(text, border=2):
    """把字符串渲染成终端二维码（`skills/qr_code.py` 自带编码器，不依赖第三方包）。"""
    from skills import qr_code

    try:
        return qr_code.to_ascii(text, border=border)
    except Exception as exc:            # noqa: BLE001 —— 画不出二维码不该拖垮登录
        return f"（二维码渲染失败：{type(exc).__name__} {exc}；请直接打开上面的扫码地址）"


def render_qr_svg(text, scale=6, border=4):
    """渲染成 SVG 字符串 —— Studio 的扫码面板直接把它塞进页面里显示。"""
    from skills import qr_code

    return qr_code.to_svg(text, level="M", scale=scale, border=border)


def qr_expires_in():
    """二维码还剩几秒（从扫码地址里的 `expire=` 参数读；读不到就返回 0）。

    米游社的二维码**只活两分钟左右**（实测：建码后 2 分钟轮询就返回 `Expired`），
    所以前端必须显示倒计时、过期后重新出一张，不能傻等。
    """
    url = str(_LIVE_QR.get("url") or "")
    match = re.search(r"[?&]expire=(\d+)", url)
    if not match:
        return 0
    return max(0, int(match.group(1)) - int(time.time()))


def qr_state():
    """当前扫码会话的状态（给 Studio 前端轮询用，不含任何凭据）。"""
    return {
        "ticket": _LIVE_QR.get("ticket") or "",
        "url": _LIVE_QR.get("url") or "",
        "state": _LIVE_QR.get("state") or "idle",
        "message": _LIVE_QR.get("message") or "",
        "expires_in": qr_expires_in(),
    }


def cancel_qr_login():
    """丢弃当前二维码（玩家点了「取消」）。

    只清我们自己这份会话状态：二维码本身没法"作废"（米游社没给这个接口），
    但它两分钟就自己过期，而且我们不再轮询了，所以不会有任何后果。
    """
    _LIVE_QR.update({"ticket": "", "device": "", "url": "", "state": "idle", "message": ""})
    return {"ok": True}


# ==========================================
# 🌟 兜底：从浏览器 cookie 库/调试端口读（现代浏览器上多半读不到，见下）
# ==========================================
#
# 为什么这是主路径（实测踩出来的）：
#   从 cookie 库里解密这条路在现代 Edge 上**不可靠** ——
#   DPAPI 那一步是好的（能解出 32 字节主密钥），但 cookie 的 `v10` 密文解出来是垃圾：
#   Edge 用了 **App-Bound 加密**（`Local State` 里不一定暴露 `app_bound_encrypted_key`，
#   但 cookie 确实不是用那把 DPAPI 主密钥加密的）。
#
#   而 CDP 的 `Network.getAllCookies` **直接返回明文 cookie** —— 完全不涉及任何解密，
#   也就不会被 App-Bound / 未来换算法影响。
#
# 安全考虑：调试端口只绑 `127.0.0.1`，端口用系统随机分配（用完即弃），
#   浏览器进程在登录完成后立刻关掉。这跟"开发者工具"用的是同一套本地接口。

_CDP = {"port": 0, "process": None}


def _free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def cdp_alive(port=None):
    """CDP 端口是否还在（浏览器还开着）。"""
    import httpx

    target = int(port or _CDP["port"] or 0)
    if not target:
        return False
    try:
        response = httpx.get(f"http://127.0.0.1:{target}/json/version", timeout=2)
        return response.status_code == 200
    except Exception:                   # noqa: BLE001
        return False


async def _cdp_call(ws_url, calls, timeout=10):
    """在一条 websocket 上按顺序发若干条 CDP 命令，返回最后一条的回复。

    `calls` 形如 `[("Network.enable", None), ("Network.getAllCookies", None)]`。
    **必须先 enable**：浏览器级目标上不先启用 Network 域，
    调 `Network.getAllCookies` 会被拒（实测 `-32601 'Network.getAllCookies' wasn't found`）。
    """
    import websockets

    async with websockets.connect(ws_url, max_size=16 * 1024 * 1024,
                                  open_timeout=5) as socket_conn:
        async def send(message_id, method, params=None):
            payload = {"id": message_id, "method": method}
            if params is not None:
                payload["params"] = params
            await socket_conn.send(json.dumps(payload))
            while True:
                message = json.loads(await asyncio.wait_for(socket_conn.recv(), timeout=timeout))
                if message.get("id") == message_id:
                    return message

        reply = {}
        for index, (method, params) in enumerate(calls, start=1):
            reply = await send(index, method, params)
        return reply


def _cdp_endpoints(port):
    """每次调用都重新问一遍：这次要连哪条 websocket。

    两条都返回（浏览器级优先，页面级兜底）：
      · 浏览器级：`Network.getAllCookies` 能一次拿到**全部** cookie，但要求先 `Network.enable`；
      · 页面级：某些 Edge/Chrome 版本只在页面会话里暴露这条命令 —— 两种都试，
        哪个先成功用哪个（实测不同版本行为不一致，硬挑一条会踩空）。
    """
    import httpx

    endpoints = []
    try:
        version = httpx.get(f"http://127.0.0.1:{int(port)}/json/version", timeout=3).json()
        if version.get("webSocketDebuggerUrl"):
            endpoints.append(version["webSocketDebuggerUrl"])
    except Exception:                   # noqa: BLE001
        pass
    try:
        for item in httpx.get(f"http://127.0.0.1:{int(port)}/json/list", timeout=3).json():
            if item.get("type") == "page" and item.get("webSocketDebuggerUrl"):
                endpoints.append(item["webSocketDebuggerUrl"])
    except Exception:                   # noqa: BLE001
        pass
    return endpoints


def _cdp_fetch_cookies(port, timeout=10):
    """从 CDP 拿全部 cookie（明文）。返回 `(cookies, errors)`。"""
    errors = []
    for ws_url in _cdp_endpoints(port):
        try:
            reply = asyncio.run(_cdp_call(ws_url, (
                ("Network.enable", None),
                ("Network.getAllCookies", None),
            ), timeout=timeout))
        except Exception as exc:        # noqa: BLE001 —— 连接被拒/浏览器已退出等
            errors.append(f"{type(exc).__name__}")
            continue
        if reply.get("error"):
            errors.append(str(reply["error"].get("message") or reply["error"]))
            continue
        return (reply.get("result") or {}).get("cookies") or [], errors
    return [], errors


def read_cookies_via_cdp(port=None, hosts=("mihoyo", "hoyolab")):
    """从 CDP 拿 cookie（明文）。返回和 `read_cookies` 一样的结构。"""
    target = int(port or _CDP["port"] or 0)
    if not target:
        return {"cookies": {}, "notes": ["还没打开登录窗口"], "via": "cdp"}
    if not cdp_alive(target):
        return {"cookies": {}, "notes": ["登录窗口已经关了"], "via": "cdp"}

    raw, errors = _cdp_fetch_cookies(target)
    if not raw and errors:
        return {"cookies": {}, "via": "cdp",
                "notes": ["CDP 读取失败：" + "；".join(errors[:2])]}

    cookies = {}
    for item in raw or ():
        name = str(item.get("name") or "")
        domain = str(item.get("domain") or "")
        if name not in WANTED_KEYS:
            continue
        if not any(token in domain for token in hosts):
            continue
        value = str(item.get("value") or "")
        if value:
            cookies[name] = value
    return {"cookies": cookies, "notes": [], "via": "cdp"}


def read_cookies(root=None, key=None, hosts=("mihoyo", "hoyolab")):
    """读米游社 cookie。**优先 CDP（明文），退回 cookie 库解密**。

    返回 `{"cookies": {name: value}, "notes": [...], "via": "cdp" | "store"}`。
    """
    from_cdp = read_cookies_via_cdp(hosts=hosts)
    if from_cdp["cookies"]:
        from_cdp["via"] = "cdp"
        return from_cdp
    if cdp_alive():
        # 登录窗口还开着但还没读到想要的 cookie —— 那就是还没登录完，别去解库（白费力气）
        from_cdp["via"] = "cdp"
        return from_cdp

    stored = _read_cookies_from_store(root=root, key=key, hosts=hosts)
    stored["notes"] = list(from_cdp["notes"]) + list(stored["notes"])
    stored["via"] = "store"
    return stored


def _read_cookies_from_store(root=None, key=None, hosts=("mihoyo", "hoyolab")):
    """退回路径：直接读 cookie 库并解密（老版本 Edge / Chrome 上仍然有效）。

    ⚠️ 现代 Edge（实测 153）上这条路**会失败**：cookie 用 App-Bound 加密，
    虽然前缀还是 `v10`、DPAPI 也能解出主密钥，但拿它解 cookie 只能得到乱码。
    所以正常流程走 CDP（见 `read_cookies_via_cdp`），这里只作为兜底。
    """
    root = root or profile_dir()
    paths = cookie_db_paths(root)
    if not paths:
        return {"cookies": {}, "notes": ["还没有登录记录（cookie 库不存在）"]}

    if key is None:
        try:
            key = cookie_key(root)
        except LoginError as exc:
            return {"cookies": {}, "notes": [str(exc)]}

    cookies = {}
    notes = []
    for path in paths:
        try:
            rows = _read_rows(path)
        except OSError as exc:
            notes.append(f"cookie 库暂时读不到（{exc}）—— 会在下一轮自动重试")
            continue
        except sqlite3.DatabaseError as exc:
            notes.append(f"读 cookie 库失败：{exc}")
            continue

        for host, name, plain, encrypted in rows:
            if not any(token in str(host) for token in hosts):
                continue
            if name not in WANTED_KEYS:
                continue
            value = plain or ""
            if not value:
                value, why = decrypt_cookie_value(encrypted, key)
                if why:
                    notes.append(f"{name}：{why}")
                    continue
            if value:
                cookies[name] = value

    return {"cookies": cookies, "notes": notes}


def build_cookie_string(cookies):
    """把 cookie 字典拼成 `k=v; k=v`（顺序固定，方便肉眼比对与测试）。"""
    order = [key for key in WANTED_KEYS if key in cookies]
    extra = sorted(key for key in cookies if key not in order)
    return "; ".join(f"{key}={cookies[key]}" for key in order + extra)


# ==========================================
# 🌟 验证拿到的 cookie
# ==========================================


def verify_tokens(cookie=None, timeout=15):
    """调米游社的 `verifyLtoken` 确认这份 cookie 真的有登录态。

    **不验证就不算成功** —— 否则可能把一份"只有 v2、看似能用"的 cookie 写进配置，
    玩家接着就会撞上"能列角色但算不出材料"的坑（这正是这个模块存在的起因）。
    返回 `{"ok", "user", "error"}`。
    """
    import httpx

    text = str(cookie if cookie is not None else config.MYS_COOKIE or "").strip()
    if not text:
        return {"ok": False, "error": "cookie 是空的", "user": {}}
    try:
        response = httpx.post(
            "https://passport-api.mihoyo.com/account/ma-cn-session/web/verifyLtoken",
            json={},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
                "Cookie": text,
                "Accept": "application/json",
                "Referer": "https://user.mihoyo.com/",
                "Origin": "https://user.mihoyo.com",
            },
            timeout=timeout,
        )
        payload = response.json()
    except Exception as exc:            # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__} {exc}", "user": {}}

    if payload.get("retcode") != 0:
        return {"ok": False, "user": {},
                "error": f"米游社说这份凭据不可用：{payload.get('message')}（retcode {payload.get('retcode')}）"}
    data = payload.get("data") or {}
    user = data.get("user_info") or {}
    return {
        "ok": True,
        "user": {
            "aid": str(user.get("aid") or ""),
            "mid": str(user.get("mid") or ""),
            "email": str(user.get("email") or ""),
            "mobile": str(user.get("mobile") or ""),
        },
        "error": "",
    }


def verify_calculator_cookie(cookie, timeout=20):
    """验证**养成计算器网页那套会话**（只有 v2 的那份）。

    不能拿 `verifyLtoken` 验（那个接口只认 v1 ltoken），所以直接问计算器：
    先试它的「我的角色」（能列出角色 = 会话有效），不行再退一步试一次算材料。

    这是"从养成计算器网页复制 cookie"那条路的验证方式 —— **验证过了才保存**。
    """
    from skills import mys_api, mys_calculator

    uid = str(getattr(config, "MYS_UID", "") or getattr(config, "DEFAULT_UID", "") or "")
    notes = []
    try:
        payload = mys_calculator.fetch_my_items(uid=uid, cookie=cookie, save=False)
        avatars = mys_calculator.parse_avatars(payload)
        if avatars:
            names = "、".join(str(item.get("name") or "") for item in avatars[:3])
            return {"ok": True, "user": {"aid": uid, "email": "", "mobile": ""},
                    "error": "",
                    "note": f"这份会话能读到账号里的角色（{names} 等 {len(avatars)} 个）"}
    except mys_api.MysError as exc:
        notes.append(f"「我的角色」读不到：{exc}")

    # 退一步：至少要能算出材料（那说明这份会话被计算器认可）
    try:
        catalog = mys_calculator.fetch_avatar_list(uid=uid, cookie=cookie, save=False)
        avatars = mys_calculator.parse_avatars(catalog)
        target = avatars[0]["id"] if avatars else 10000002
        result = mys_calculator.compute_materials(
            target, 1, 20, cookie=cookie, save=False, uid=uid,
        )
        if result.get("requirements"):
            return {"ok": True, "user": {"aid": uid, "email": "", "mobile": ""},
                    "error": "",
                    "note": ("这份会话能算材料，但**读不到「我的角色」**（账号里已有 "
                             "角色列表拿不到）—— 养成照样能用，只是缺口按总需求算。" +
                             ("；".join(notes) if notes else ""))}
        notes.append("算材料返回了空清单")
    except mys_api.MysError as exc:
        notes.append(f"算材料失败：{exc}")

    return {"ok": False, "user": {},
            "error": ("这份 cookie 米游社不认（计算器接口全部失败）：\n" + "；".join(notes) +
                      "\n确认它是**在养成计算器网页上登录后**复制的那份（不是别的站点）。")}


def has_ltoken(cookie=None):
    """这份 cookie 够不够用（`audit_cookie` 的简写）。

    ⚠️ 名字保留（很多调用方在用），但语义已经放宽：**有 v2 那套也算够用** ——
    因为养成计算器网页的会话就是 v2，`verifyLtoken` 验不了它，但计算器接口认。
    要严格判断"有没有 v1 ltoken"，看 `audit_cookie()["has_v1"]`。
    """
    from skills import mys_api

    return mys_api.audit_cookie(cookie)["ok"]


# ==========================================
# 🌟 对外主流程
# ==========================================


def extract_cookie(text):
    """从粘贴进来的内容里抠出 cookie。

    三种粘贴方式都认（玩家不用学格式，粘什么算什么）：
      · 一行 cookie：`ltuid=...; ltoken=...; ...`
      · **整段 `Copy as cURL`**：取里面 `-H 'cookie: ...'` / `-b '...'` 那一行
      · 只有几条 `k=v` 的碎片（用换行分隔也行）

    ⚠️ 为什么要支持 cURL：**养成计算器是单独登录的网页**，它的会话里有
    `DEVICEFP` / `_MHYUUID` 这类 cookie，手抄经常漏 —— 复制整段 cURL 最省事，
    而且不会漏字段。认不出来时返回空串（调用方据此提示玩家）。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""

    # ① cURL：-H 'cookie: ...' / --header 'Cookie: ...'
    for pattern in (r"-H\s+'[Cc]ookie:\s*([^']+)'", r'-H\s+"[Cc]ookie:\s*([^"]+)"',
                    r"--header\s+'[Cc]ookie:\s*([^']+)'",
                    r'--header\s+"[Cc]ookie:\s*([^"]+)"'):
        match = re.search(pattern, raw)
        if match:
            return match.group(1).strip()

    # ② cURL：-b / --cookie 后面的内容
    for pattern in (r"-b\s+'([^']+)'", r'-b\s+"([^"]+)"', r"--cookie\s+'([^']+)'",
                    r'--cookie\s+"([^"]+)"'):
        match = re.search(pattern, raw)
        if match:
            return match.group(1).strip()

    # ③ 已经是"cookie 文本"：按分号/换行拆开，只留像 k=v 的碎片
    if "curl" in raw.lower() and "=" not in raw:
        return ""
    parts = []
    for chunk in re.split(r"[\r\n;]+", raw):
        chunk = chunk.strip().strip("'\"")
        if not chunk or "=" not in chunk:
            continue
        key = chunk.split("=", 1)[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", key):
            continue
        parts.append(chunk)
    return "; ".join(parts)


def finish_login(cookie=None, save=True, env_path=None, timeout=15):
    """读取 cookie → 验证 → （可选）写进 `.env`。返回一份可以直接给前端的结果。

    读不到 / 验证不过时**如实返回失败**，绝不写一份用不了的 cookie 进配置。
    """
    from skills import mys_api

    result = {"ok": False, "step": "read", "cookie_masked": "", "user": {},
              "error": "", "notes": [], "saved": False, "complete": False}

    if cookie:
        built = str(cookie).strip()
        result["notes"].append("用的是手动传入的 cookie")
    else:
        read = read_cookies()
        result["notes"].extend(read["notes"])
        if not read["cookies"]:
            result["error"] = (
                "没从浏览器里读到登录状态。**推荐改用扫码登录**"
                "（Studio 页面上点「扫码登录」，或跑 `python -m skills.mys_login --qr`）——"
                "它直接向米游社换取 v1 ltoken，不依赖任何浏览器 cookie 库。"
                "如果你确实想走浏览器那条路，请确认：① 登录窗口里已经用米游社 App 扫码并确认；"
                "② 扫码完成后稍等几秒（浏览器写 cookie 需要一点时间）；③ 再点一次。"
                + ("\n" + "；".join(read["notes"]) if read["notes"] else "")
            )
            return result
        built = build_cookie_string(read["cookies"])

    # ★★ 合并而不是覆盖（踩过一次**丢数据**的坑）：
    #    App 扫码只给 v1（ltoken/ltuid/cookie_token），网页扫码只给 v2（ltoken_v2 那一套），
    #    而这两套**各自管一半功能**（v1 → 战绩/体力，v2 → 养成计算器）。
    #    以前这里直接把新 cookie 写进 .env，结果"扫一次 App 码就把计算器 cookie 冲掉了"——
    #    玩家看到的是"登录成功了，但养成计算器又变成未登录"。
    #    同名字段以**新来的**为准（新登录一定是更新的），旧 cookie 独有的键保留。
    previous = str(config.MYS_COOKIE or "")
    built = merge_cookie(previous, built)
    result["merged"] = bool(previous and previous != built)

    result["cookie_masked"] = mys_api.mask_cookie(built)
    audit = mys_api.audit_cookie(built)
    result["complete"] = audit["ok"]
    result["mode"] = audit.get("mode") or ""
    if not audit["ok"]:
        result["step"] = "incomplete"
        result["error"] = ("这份 cookie 连账号 id / 令牌都没有，米游社会直接拒。\n"
                           + (audit.get("hint") or ""))
        return result

    result["step"] = "verify"
    if audit.get("mode") == "calculator":
        # 养成计算器网页那套（只有 v2）：**不能用 verifyLtoken 验**（那个接口只认 v1），
        # 改用计算器自己的接口验：能读出材料就算通过。
        verification = verify_calculator_cookie(built, timeout=timeout)
    else:
        verification = verify_tokens(built, timeout=timeout)
    if not verification["ok"]:
        result["error"] = verification["error"]
        return result
    result["user"] = verification["user"]
    if verification.get("note"):
        result["notes"].append(verification["note"])

    if save:
        result["step"] = "save"
        try:
            from skills import env_config

            backup = env_config.save_env(env_path or config.project_path(".env"),
                                         {"MYS_COOKIE": built})
            result["saved"] = True
            result["backup"] = os.path.basename(str(backup or ""))
            if result.get("merged"):
                result["notes"].append(
                    "已把新 cookie 与原有 cookie **合并**（v1 管战绩/体力，v2 管养成计算器），"
                    "没有覆盖掉另一种能力。")
        except Exception as exc:        # noqa: BLE001
            result["error"] = f"cookie 读出来了，但写入 .env 失败：{type(exc).__name__} {exc}"
            return result
        # 顺手让**当前进程**也用上新 cookie：Studio 的养成页跟这里同一个进程，
        # 不刷新的话玩家会看到"登录成功了，但一同步还是说没登录"，还得重启一次才知道好使。
        # （Agent 是独立进程，那个确实要重启才读得到新 .env。）
        config.MYS_COOKIE = built

    result["ok"] = True
    result["step"] = "done"
    return result


def status():
    """登录相关状态：有没有可用的 cookie、缺不缺 ltoken、扫码那条路能不能用。

    以前这里还会去探"能不能从浏览器 cookie 库里读"（CDP / DPAPI）。
    现在主路径换成了扫码换 token，那两个探测既慢又没有任何用处，已经拿掉。

    ★ 还会报**两种能力各缺什么**（玩家问过"养成计算器要单独登录，怎么办"）：
      · `calculator_ready` —— 算材料 / 已有还差 要的是**网页那套 v2**；
      · `resin_ready`      —— 体力 / 战绩 要的是**扫码那套 v1**。
      两个都不满足时，界面上的提示会直接告诉你**该点哪个按钮**。
    """
    from skills import mys_api

    audit = mys_api.audit_cookie()
    has_v1, has_v2 = bool(audit.get("has_v1")), bool(audit.get("has_v2"))
    calculator_ready = has_v1 or has_v2          # 计算器 v1/v2 都认
    # ⚠️ `resin_ready` 报的是**真的拿得到体力**，不是"cookie 形态看着像支持"：
    #    这个账号拿到 v1 之后，体力接口照样回 `5003 账号数据异常`（实测 8 种头组合全被拒），
    #    只报 has_v1 会让玩家以为"体力 ✅ 了"，然后看着卡片上的 0 发懵（被反馈过）。
    #    所以以**实际能不能给出一个可用体力值**为准（接口读到的 / 手动记下的都算）。
    try:
        from skills import mys_resin

        resin_state = mys_resin.status()
        resin_ready = bool(resin_state.get("available"))
        resin_source = str(resin_state.get("source") or "")
    except Exception:                            # noqa: BLE001
        resin_ready, resin_source = False, ""
    if calculator_ready and resin_ready:
        advice = (f"✅ 计算器和体力都能用（体力来源：{resin_source or '接口'}）。")
    elif calculator_ready and has_v1:
        advice = ("⚠️ 计算器可用；体力**接口被米游社风控挡着**（这个账号一直回「账号数据异常」）——"
                  "在养成页**填一次当前体力**即可，程序会按 8 分钟 1 点自动往后推算。")
    elif calculator_ready:
        advice = ("⚠️ 现在只能算材料（养成计算器）。体力还差 app 版 v1 `ltoken`："
                  "点「扫码登录」（App）再扫一次；或者直接在养成页填当前体力。")
    elif has_v2:
        advice = "⚠️ 只有网页那套 cookie，建议点「扫码登录」（App）补一份 v1。"
    else:
        advice = ("还没有可用的 cookie：点「扫码登录」（App 版，拿体力/战绩）"
                  "或「网页版扫码」（养成计算器），两个都扫一次最省事。")
    return {
        "configured": audit["configured"],
        "complete": audit["ok"],
        "missing": ["/".join(item["keys"]) for item in audit["missing"]],
        "masked": mys_api.mask_cookie(config.MYS_COOKIE),
        "mode": audit.get("mode") or "",
        "has_v1": has_v1,
        "has_v2": has_v2,
        "calculator_ready": calculator_ready,
        "resin_ready": resin_ready,
        "resin_source": resin_source,
        "advice": advice,
        # 扫码登录（主路径）的能力：能不能建码、当前这张码什么状态
        "qr_available": True,
        "qr": qr_state(),
        # 网页版扫码（养成计算器那套 v2）
        "web_qr": {"ticket": bool(_LIVE_WEB_QR.get("ticket")),
                   "state": _LIVE_WEB_QR.get("state") or "idle"},
        "login_url": LOGIN_URL,
    }


# ==========================================
# 🌟 命令行：python -m skills.mys_login
# ==========================================


def _main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="米游社扫码登录（自动换取 v1 ltoken）")
    parser.add_argument("--qr", action="store_true",
                        help="★ 默认就该用这个：终端画一张二维码，手机扫码登录")
    parser.add_argument("--timeout", type=int, default=180, help="--qr 等待扫码的秒数（默认 180）")
    parser.add_argument("--status", action="store_true", help="只看现在的登录状态")
    parser.add_argument("--start", action="store_true",
                        help="打开网页登录窗口（旧路子：网页只发 v2 cookie，通常拿不到 ltoken）")
    parser.add_argument("--devtools", action="store_true",
                        help="打开窗口并**同时打开开发者工具**（手动抄 cookie 的兜底）")
    parser.add_argument("--diagnose", action="store_true", help="诊断：三条读取路径各试一遍")
    parser.add_argument("--finish", action="store_true", help="从浏览器 cookie 库读 cookie 并验证/保存")
    parser.add_argument("--close", action="store_true", help="关掉登录窗口")
    parser.add_argument("--no-save", action="store_true", help="只验证，不写进 .env")
    args = parser.parse_args(argv)

    if args.close:
        close_login_window()
        print("已关闭登录窗口")
        return 0

    if args.qr:
        result = login_by_qr(timeout=args.timeout, save=not args.no_save)
        if not result["ok"]:
            print(f"❌ {result['error']}")
            return 1
        user = result.get("user") or {}
        print(f"✅ 登录成功：aid={result.get('ltuid') or user.get('aid')} "
              f"{user.get('email') or user.get('mobile') or ''}")
        print(f"   掩码：{result['cookie_masked']}")
        print("   已写入 .env（含 ltoken）" if result.get("saved") else "   （未保存，只做了验证）")
        print("   下一步：python -m skills.mys_inventory --sync")
        return 0

    if args.diagnose:
        print("=== 自动读取的三条路径各试一遍（不发请求、不改配置）\n")
        print("① CDP（明文 cookie，最理想）")
        print("   登录窗口开着吗:", cdp_alive(), "| 端口:", _CDP["port"] or "（没有）")
        try:
            probe = read_cookies_via_cdp()
        except Exception as exc:        # noqa: BLE001 —— 诊断工具自己不能崩
            probe = {"cookies": {}, "notes": [f"{type(exc).__name__} {exc}"]}
        print("   结果:", f"{len(probe['cookies'])} 条" if probe["cookies"] else "没读到")
        for note in probe["notes"][:2]:
            print("   提示:", note)

        print("\n② cookie 库解密（老版本浏览器有效）")
        try:
            store = _read_cookies_from_store()
        except Exception as exc:        # noqa: BLE001
            store = {"cookies": {}, "notes": [f"{type(exc).__name__} {exc}"]}
        print("   结果:", f"{len(store['cookies'])} 条" if store["cookies"] else "没读到")
        for note in store["notes"][:6]:
            print("   提示:", note)

        print("\n③ cookie 库里到底有没有我们要的 cookie（不解密，只看名字）")
        paths = cookie_db_paths()
        if not paths:
            print("   还没有 cookie 库 —— 说明这个配置目录里还没登录过"
                  "（先点「扫码登录」并完成扫码）")
        for path in paths:
            print("   库:", path)
            try:
                connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
                for name, count in connection.execute(
                    "SELECT name, COUNT(*) FROM cookies WHERE host_key LIKE '%mihoyo%' "
                    "GROUP BY name ORDER BY name"
                ):
                    mark = "★" if name in WANTED_KEYS else " "
                    print(f"     {mark} {name:22} {count}")
                connection.close()
            except Exception as exc:    # noqa: BLE001
                print("     读不了:", exc)

        print("\n结论：")
        print("  ① 有结果 → 一切自动，什么都不用做；")
        print("  ② 有结果 → 也能自动；")
        print("  ③ 里能看到 ltoken 但 ①② 都读不出明文 → 这个 Edge 版本拒绝 CDP 且 cookie 用了"
              "App-Bound 加密，自动读取走不通 ——")
        print("      改用手动：python -m skills.mys_login --devtools，")
        print("      在 DevTools 的 Application → Cookies 里复制，再粘到 Studio 的"
              "「读不出 cookie？手动粘贴」。")
        return 0

    if args.devtools:
        try:
            info = launch_login_window(devtools=True)
        except LoginError as exc:
            print(f"❌ {exc}")
            return 1
        print(f"✅ 已打开登录窗口（含开发者工具）：{info['browser']}")
        print("   1) 用米游社 App 扫码登录；")
        print("   2) 登录后，在开发者工具里点 **Application**（应用程序）→ 左侧 **Cookies**")
        print("      → 选 `https://user.mihoyo.com`；")
        print("   3) 把 `ltuid` / `ltoken` / `cookie_token` 的值抄下来，拼成")
        print("      `ltuid=...; ltoken=...; cookie_token=...`，")
        print("      粘到 Studio 的「读不出 cookie？手动粘贴」，或写进 .env 的 MYS_COOKIE。")
        return 0

    if args.start:
        try:
            info = launch_login_window()
        except LoginError as exc:
            print(f"❌ {exc}")
            return 1
        print(f"✅ 已打开登录窗口：{info['browser']}")
        print(f"   配置目录：{info['profile']}")
        print("   请用**米游社 App** 扫码并在手机上确认，然后运行：")
        print("   python -m skills.mys_login --finish")
        return 0

    if args.finish:
        result = finish_login(save=not args.no_save)
        for note in result["notes"]:
            print(f"· {note}")
        if not result["ok"]:
            print(f"❌ {result['error']}")
            return 1
        user = result["user"]
        print(f"✅ 登录成功：aid={user.get('aid')} {user.get('email') or user.get('mobile')}")
        print(f"   掩码：{result['cookie_masked']}")
        print("   已写入 .env" if result["saved"] else "   （未保存，只做了验证）")
        return 0

    info = status()
    print(f"cookie：{'已配置' if info['configured'] else '未配置'}"
          f"{'（完整）' if info['complete'] else '（缺 ' + '、'.join(info['missing']) + '）'}")
    print(f"掩码：{info['masked']}")
    qr = info["qr"]
    print(f"扫码会话：{qr['state']}"
          f"{'（还剩 ' + str(qr['expires_in']) + ' 秒）' if qr['ticket'] else ''}")
    print("\n用法：python -m skills.mys_login --qr —— 终端里画一张二维码，手机扫码即可")
    return 0 if info["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
