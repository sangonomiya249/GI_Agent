# 米游社个人战绩：查"不在展柜的角色"

> 一句话：**游戏展柜一次只能放 8 个角色**，所以 Agent 以前只能认识这 8 个。
> 填一份米游社 cookie，它就能读到**你账号里的全部角色**（等级 / 天赋等级 / 武器），
> 材料照样由本地百科字典算 —— 不需要你把角色挨个换进展柜。

这个功能**默认关闭**：`.env` 里 `MYS_COOKIE` 留空时，代码一行网络请求都不会发。

---

## 1. 它到底是什么、不是什么

| | |
| --- | --- |
| ✅ 它做的 | 读你自己账号的角色列表：等级、**天赋等级**、武器等级、命座、好感度 |
| ✅ 它不做的 | 不改游戏数据、不签到、不领奖励、不碰你的圣遗物 |
| ❌ 不是官方接口 | 这是米游社 App/网页自己用的接口（社区叫"战绩接口"），**风控严格**，米游社随时可能改 |
| ❌ 不是共享凭据 | cookie 只存在你自己的 `.env` 里，只发往 `api-takumi.mihoyo.com` / `api-takumi-record.mihoyo.com` |

> 喵喵插件（Yunzai 的 miao-plugin）里的「#绑定cookie」做的就是这件事；
> 本项目的实现对齐了它底层那套签名方式：
> `DS = md5("salt={salt}&t={t}&r={r}&b={body}&q={query}")`，
> 国服 salt `xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs` + `x-rpc-app_version: 2.40.1` + `x-rpc-client_type: 5`。

---

## 2. 怎么拿 cookie

### 推荐：用 Studio 的「扫码登录」

Studio → 左边 **「配置」** → 找 **「米游社个人战绩（可选）」** 那张卡 → 点 **「扫码登录（推荐）」**：

```text
点「扫码登录」
    ↓  二维码**直接画在页面上**（不用装东西、也不会弹浏览器窗口）
手机「米游社 App」→ 右上角「+」→ 扫一扫 → 扫页面上的二维码
    ↓  手机上点「确认登录」
程序自动：换 v1 ltoken → 验证（真的能用才收）→ 写进 .env
```

二维码**只活两分钟左右**，所以面板上有倒计时；过期了会自动换一张，你重新扫就行。

命令行等价：

```bash
python -m skills.mys_login --qr      # 终端里画出二维码，手机扫（第一张过期会自动再出）
python -m skills.mys_login           # 只看当前状态（cookie 全不全、缺什么）
python -m skills.mys_login --status  # 同上
```

#### 它为什么能拿到 `ltoken`（而别的方法拿不到）

它不是"读浏览器的 cookie"，而是**直接走米游社自己的登录接口**：

```text
createQRLogin（passport-api）        → 一张二维码（URL + ticket）
    ↓  玩家扫码并在手机上确认
queryQRLoginStatus 轮询到 Confirmed  → 拿到 stoken + aid/mid
    ↓
getLTokenBySToken                    → **v1 ltoken**（计算器/战绩唯一认的那个）
    ↓
verifyLtoken 验证真能用               → 才写进 .env
```

所以**全程不碰浏览器、不碰 cookie 库、不涉及任何解密** ——
之前那套"开独立窗口读 cookie"踩过的坑（App-Bound 加密、库被占用、新版 Edge 拒绝调试协议）
统统绕开了。

> 两个必须对齐的细节（都是实测踩出来的，改代码时别动）：
> * `x-rpc-device_id` 必须是 **32 位大写**、且建码与轮询**用同一个**，否则
>   `-3503 请求失败，当前设备或网络环境存在风险`；
> * `x-rpc-device_fp`（设备指纹）**不能省**，少了它同样 `-3503`。

#### 兜底：手动复制（自动路径全都不可用时）

老路子（打开网页登录窗口 → 自己从开发者工具抄 cookie）仍然保留，但它现在**拿不到 v1
`ltoken`** —— 米游社网页登录只发 `*_v2` 那一套（见下面第 4 条）。所以只在你确实不想扫码时用：

```bash
python -m skills.mys_login --devtools   # 打开登录窗口 + 开发者工具
```

1. 用米游社 App 扫码登录；
2. 开发者工具里点 **Application**（应用程序）→ 左侧 **Cookies** → 选 `https://user.mihoyo.com`；
3. 抄下 **`ltuid`**、**`ltoken`**、**`cookie_token`** 三行的 Value
   （`ltoken` 比较长，双击单元格再 Ctrl+C）；
4. 拼成一行：`ltuid=...; ltoken=...; cookie_token=...`；
5. 粘到 Studio 那个卡上的 **「读不出 cookie？手动粘贴」**（会先验证再保存），
   或者直接写进 `.env` 的 `MYS_COOKIE`。

想看"浏览器那条路到底能不能用"，跑：

```bash
python -m skills.mys_login --diagnose
```

它会列出 ① CDP 能不能读到、② cookie 库解密能不能读出、③ **库里到底有哪些 cookie 名**
（不解密，只看名字 —— 这一栏最有信息量：能看到 `ltoken` 就说明"cookie 在、只是读不出明文"）。

> 为什么不做"内嵌网页自动抓 cookie"：内嵌页面拿不到 httpOnly 的 cookie
> （`ltoken` 恰好就是 httpOnly），所以那条路技术上不通。

#### 其他要知道的点

* 它**不碰你的密码**：不接收、不保存、不发送任何账号密码；
* 扫码登录时凭据只在你手机和米游社之间，我们拿到的只是 `stoken` 换出来的 token；
* 用过的 ticket 会立刻丢掉，不会被重复使用；
* 手动粘贴的 cookie 会先经 `verifyLtoken` 验证**再**写 `.env`，验证不过不会保存。

> 为什么不用"网页 cookie 直接换 token"：试过了，换不了。
> `ltoken_v2` 补成 v1 键名、放进 `x-rpc-ltoken` 头、
> 走 `getActionTicketByCookieToken` / `getWebTokensByAuthKey` —— 全部不行。
> `ltoken` 只在**真正的登录流程**里发放，所以这里只能老老实实登一次。

### 备选：手工复制（三分钟）

米游社的 cookie 就是浏览器里那一串。要点是**用已经登录的米游社网页**去复制，
而且要**从发往 `api-takumi.mihoyo.com` 的请求上复制** —— 见下面第 4 条的原因：

1. 浏览器打开 <https://www.mihoyo.com/>，扫码登录米游社账号（**不要用无痕窗口**，无痕关掉就没了）；
2. 按 `F12` 打开开发者工具，切到 **网络 / Network** 面板，勾选 **保留日志 / Preserve log**；
3. **先随便打开一次「个人战绩」页**（<https://webstatic.mihoyo.com/app/community-game-records/>，
   选原神 → 我的角色）。这一步会让浏览器拿到 `ltoken` 并开始带上它；
4. 回到 Network 面板，在请求列表里找**域名是 `api-takumi.mihoyo.com`（或
   `api-takumi-record.mihoyo.com`）** 的那条请求（点它 → 看 Request Headers）；
5. 在 **请求标头 / Request Headers** 里找到 **`Cookie`** 那一行，**整行复制**（右键 → 复制值）；
6. 粘到下面第 3 节说的地方（Studio 配置页，或者 `.env`）。

**必须包含的键**（缺了就读不到）：

| 键 | 作用 | 缺了会怎样 |
| --- | --- | --- |
| `ltuid` 或 `account_id` | 账号 id | 报"未登录" |
| `ltoken` | 长期令牌（**战绩接口与养成计算器必须要**） | 能列出角色，但拉角色/算材料全部失败 |
| `cookie_token` | 网页令牌（部分接口要） | 少数接口失败，建议一起带上 |

> ⚠️ **实测踩过的大坑：v2 cookie**。
> 现在的米游社网页登录只给你发 **`*_v2`** 那一套
> （`ltoken_v2` / `account_id_v2` / `ltuid_v2` / `cookie_token_v2`）。
> 它**能**通过"列出你名下角色"这种公共接口，所以看起来一切正常；
> 但**需要登录的接口会全部失败**：
>
> | 接口 | 只有 v2 时的结果 |
> | --- | --- |
> | `binding/api/getUserGameRolesByCookie`（公共） | ✅ `retcode 0`，能列出角色 |
> | 养成计算器 `/v1/sync/avatar/list` | ❌ `retcode -100`「请先登录后参与活动」 |
> | 养成计算器 `/v2/compute`（算材料） | ❌ `retcode 0` 但**材料清单是空的** |
> | 战绩 `character/list` | ❌ `retcode 5003` |
>
> **`ltoken_v2` 不能当 `ltoken` 用**（试过补成 v1 键名、放进 `x-rpc-ltoken` 头，都不行）。
> 要拿到 `ltoken`，**最省事就是上面的扫码登录**；手工路子则必须先打开一次「个人战绩」页，
> 再从 `api-takumi.mihoyo.com` 的请求上复制。
>
> 不确定自己的 cookie 行不行？跑这个，它会直接告诉你缺什么：
>
> ```bash
> python -m skills.mys_api --audit
> ```
>
> （`python main.py doctor` 也会把这一条以红色报出来；Studio 配置页的状态行同样会标红。）

> 手机 App 抓包也能拿到同一份 cookie，但更麻烦；浏览器这条路最省事。
> 复制时**别只复制一半**（比如只复制了 `ltuid`），那会一直在"未登录"和"风控"之间来回。

---

## 3. 怎么填（Studio 界面里就能填，不用手改 .env）

**推荐：在 GI Agent Studio 里填**

1. 打开 Studio → 左边 **「配置」**；
2. 上方标签切到 **「米游社个人战绩（可选）」**，把 cookie 粘进 **`MYS_COOKIE`**（密码框，不明文显示）；
3. 同页下方有 **「验证 Cookie」** 按钮 —— 它直接拿输入框里的内容去问一次米游社（**1 次请求**，不用先保存），
   成功会列出你的角色（昵称 / UID / 服 / 冒险等阶），失败会用人话说明原因（cookie 失效 / 风控 / 登错号）；
4. 验证通过后点右上角 **「保存」**（会备份成 `.env.bak-时间戳`），再**重启 Agent** 生效；
5. 想立刻把全角色名单拉下来，就点 **「拉一次全角色名单」**（平时不用点 —— 只有问到展柜外的角色且缓存过期才自动拉）。

同页还能改这两项（都有默认值，一般不用动）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `MYS_CACHE_TTL_HOURS` | 48 | 名单多久拉一次（你说过"别每次启动都拉，1~3 天一次"） |
| `MYS_DETAIL_MAX_PER_TURN` | 3 | 一轮对话里最多为几个展柜外角色补天赋详情（每个角色 1 次请求） |
| `MYS_UID` | 空 | 留空 = 用「玩家与记忆」里的 UID |
| `MYS_SALT` / `MYS_APP_VERSION` | 空 | 高级项：米游社改版导致签名失效时才需要改 |

**或者手改 `.env`**（等价）：

```dotenv
MYS_COOKIE=ltuid=123456789; ltoken=xxxxxxxxxxxxxxxx; cookie_token=yyyyyyyy; account_id=123456789
```

格式要求见上一节那张"必须包含的键"的表；界面上保存时也会直接提醒你缺了哪一项。

---

## 4. 验证 & 使用（命令行）

Studio 里点按钮是最省事的；命令行等价：

```bash
# ① 只验证 cookie（1 次请求，不会拉角色数据）
python -m skills.mys_api --check
#   ✅ 旅行者｜UID 100000000｜cn_gf01｜冒险等阶 60

# ② 强制拉一次全角色名单（平时不用手动跑，遇到展柜外角色时会自动拉）
python -m skills.mys_api --refresh

# ③ 看某个角色的资料（含按需补的天赋等级）
python -m skills.mys_api --show 胡桃
```

配好之后**不需要再做任何事**：对话里提到展柜外的角色时，Agent 会自动去查，并把结果作为
【展柜外角色参考】贴在本轮的上下文末尾；展柜里已有的角色**不会**重复查、也不会重复注入。

```text
你：帮我看看胡桃还差什么材料
Agent：（读到米游社数据）胡桃 Lv.80，天赋 8/8/8，突破 Boss 材料「未熟之玉」还缺 20 个 …
```

---

## 4. 为什么不每次启动都拉（风控）

玩家明确要求过："别每次启动都拉起 cookie 数据，不然会触发风控，1~3 天拉一次"。
所以这里的策略是：

| 数据 | 何时请求 | 默认频率 |
| --- | --- | --- |
| 全角色名单（1 次请求） | 只有"玩家提到了展柜外角色"且缓存过期 | `MYS_CACHE_TTL_HOURS=48`（2 天） |
| 单角色天赋详情（每个 1 次请求） | 只有那个角色**被提到**、且还没拉过天赋 | 每轮最多 `MYS_DETAIL_MAX_PER_TURN=3` 个 |
| 展柜（Enka） | 照旧，和本功能无关 | 原有 TTL（10 分钟） |

缓存文件：`memory/mys_characters.json`（原子写入；里面**没有** cookie，只有角色资料）。
删掉它 = 下次查展柜外角色时重新拉一份。

嫌 2 天太频繁就调大 `MYS_CACHE_TTL_HOURS`（例如 `72`）；想更实时就调小（风控风险自负）。

---

## 5. 出问题怎么排查

先跑 `python -m skills.mys_api --check`，再对着下表看：

| 现象 | 含义 | 怎么办 |
| --- | --- | --- |
| `未登录或 cookie 已失效`（retcode -100/-111） | cookie 过期或复制得不全 | 按第 2 节重新扫码一份 |
| `触发米游社风控，需要在米游社 App 里完成验证`（10001） | 账号被要求验证，或 cookie 缺 `ltoken` | 打开米游社 App 随便点两下完成验证；确认 cookie 里有 `ltoken`；过几小时再试 |
| `请求异常（多半是风控/参数被拒）`（-2016） | 请求太频繁 / 参数被拒 | 等一会儿；别手动连点 `--refresh` |
| `cookie 名下没有原神角色` | 登错号了（比如登成了星铁/绝区零账号） | 换正确的米游社账号 |
| 二维码扫了没反应 / 手机提示"二维码已失效" | 二维码只活两分钟 | 点「换一张二维码」重新扫（页面会自动换） |
| 扫码时报 `-3503 请求失败，当前设备或网络环境存在风险` | 建码与轮询的设备号不一致，或少了设备指纹头 | 一般不用管（代码已保证一致）；真遇到就重启 Studio 再来一次 |
| 扫码时报 `-3005 参数不合法` | passport 那套参数被米游社改了 | 更新 `.env` 里的 `MYS_APP_SALT` / `MYS_APP_VERSION_APP` / `MYS_APP_ID` |
| 签名突然全部失败 | 米游社改了 App 版本对应的 salt | 见下 |

**salt 失效了怎么办**：`salt` 跟着米游社 App 版本走，官方不公开。真改版时改 `.env` 就行，不用改代码：

```dotenv
MYS_SALT=新的32位salt
MYS_APP_VERSION=对应的版本号
MYS_APP_SALT=扫码登录那套（社区叫 passSalt）
MYS_APP_VERSION_APP=扫码登录用的 App 版本号
MYS_APP_ID=扫码登录用的 app_id（字符串）
```

去哪里找新 salt：搜 `mihoyo-api-collect` 的 salt 汇总 issue，或看喵喵插件/Yunzai 仓库最近一次
更新里 `mysApi.js` 的 `getDs()`。本项目不打包任何"自动更新 salt"的逻辑 —— 那需要联网信任第三方。

---

## 6. 安全边界（请认真看一遍）

- **cookie = 账号访问凭据**。拿到它的人可以读你的战绩、绑定的角色信息；米游社侧虽然不能改游戏数据，
  但也不该外传。所以：
  - 只写进 `.env`（`.gitignore` 已忽略），**别**发到群里、别写进截图、别提交到 GitHub；
  - Agent 的日志里永远只打印掩码（`ltuid=1***…`），不会出现完整 cookie（有测试盯着这条）；
  - 本项目的代码**不会**把 cookie 发往米游社以外的任何域名。
- 想临时关掉：`.env` 里把 `MYS_COOKIE` 清空重启即可 —— 功能整体关闭，展柜照旧能用。
- 不想给 cookie 也行：把角色放进游戏展柜（一次 8 个）是零风险的替代方案。

---

## 7. 实现位置

| 文件 | 作用 |
| --- | --- |
| `skills/mys_login.py` | **扫码登录**：建码 → 轮询 → 换 v1 `ltoken` → 验证 → 写 `.env`（老浏览器路径留作兜底） |
| `skills/qr_code.py` | 自带二维码编码器（纯标准库，不依赖 `qrcode`/`Pillow`），终端画 ASCII、页面画 SVG |
| `skills/mys_api.py` | 签名、请求、缓存、按需注入、`--check/--refresh/--show/--audit` 自检 |
| `skills/env_reader.py` | `build_avatar_entry()`：展柜和米游社**共用**的角色装配（材料形状一致） |
| `brain/llm_brain.py` | 只在"玩家提到展柜外角色"时注入【展柜外角色参考】 |
| `prompts/system_rules.md` | 告诉模型：展柜没有 ≠ 玩家没有；有参考就按参考规划，没参考别编造 |
| `skills/health_check.py` | `python main.py doctor` / Studio 体检里会报告 cookie 是否配置、缓存有多少角色 |
| `tests/test_mys_api.py` | 签名公式、UID→区服、TTL、按需注入、错误分类、没配 cookie 不发请求 |
| `tests/test_mys_login.py` | 扫码流程的协议细节（设备号复用、过期重出、换 token、状态归一） |
| `tests/test_qr_code.py` | 二维码编码器：与社区实现**逐位比对**（基准见 `tests/fixtures/qr_reference.json`） |
