# QQ 官方机器人接口（轻量版）

给 Agent 加一条 QQ 通道：在 QQ 里 @机器人 或私聊它，就能像在 CLI / 飞书里一样规划、审批、执行
BetterGI 一条龙。

**不依赖 LangBot**。LangBot 是个完整平台（插件系统、WebUI、多平台适配、pipeline 编排），
为了连一个 Agent 部署一整套太重。QQ 官方机器人协议本身很简单：取 token → 连 WebSocket 网关 →
收事件 → 调 REST 回消息。这里用项目**已有依赖**（`httpx` + `websockets`，见 `requirements.txt`）
实现，`channels/qq_bot.py` 一共 500 行左右，而且 Agent 的整套逻辑一行没复制。

---

## 1. 它长什么样

```text
QQ 群 / 私聊
   │  ① @机器人 或私聊发一句话
   ▼
channels/qq_bot.py ── WebSocket 网关（收事件）+ REST（回消息）
   │  ② 翻译成 (文本, 会话标识)      target = qq:group:<group_openid>#<msg_id>
   ▼
channels/agent_router.py ── 快捷指令 / 待审批任务的 y、t / 去重，与飞书共用
   │  ③ 规划 → 展柜刷新 → llm_brain.ask_agent
   ▼
brain/llm_brain.py ── 审批屏（plan_summary_lines）
   │  ④ 玩家回 y / t
   ▼
skills/bgi_controller.execute_bgi_task ── 配置事务（备份 / Diff / 原子写 / 回读 / rollback）
   │  ⑤ 所有回话都发回 feishu_api.send_feishu_msg(target, text)
   ▼
api/channel_router.py ── target 带 `qq:` 前缀 → 交给 QQ 通道发送；否则照旧走飞书
```

要点：**回复的落点由 target 前缀决定**。Agent 里所有的提示（包括报错、审批屏、事务回执）都走
`feishu_api.send_feishu_msg`，所以 QQ 通道天然拿到全部能力，包括 `rollback` 提示和
`🔄 已改判为…` 这类拦截提示。

---

## 2. 配置（.env）

```ini
QQ_BOT_APPID=你的 AppID
QQ_BOT_SECRET=你的 AppSecret（密钥）
QQ_BOT_ALLOWED_USERS=            # 允许指挥 Agent 的 openid（不是 QQ 号！），逗号分隔
QQ_BOT_ALLOW_ANYONE=0            # 危险开关：1 = 任何 QQ 用户都能下指令
QQ_BOT_INTENTS=                  # 留空用默认值（见下）
QQ_BOT_RECONNECT_SECONDS=5       # 断线重连间隔
QQ_BOT_REPLY_MODE=compact        # 回复详略：compact（默认）/ full
QQ_BOT_BUTTONS=1                 # 审批屏挂按钮（需要平台开通，默认 1）
QQ_BOT_WS_URL=                   # 进阶，通常不用填：直连网关地址
```

### 2.1 建机器人

1. 打开 <https://q.qq.com>（QQ 机器人开放平台），创建一个机器人。
2. 在「开发设置」里拿到 **AppID** 与 **AppSecret**，填进 `.env`。
3. 权限/事件：
   * **群聊 / 单聊**需要申请开通「群聊/单聊消息」权限（对应 intents 的 `GROUP_AND_C2C_EVENT`，位 `1<<25`）。
   * 频道（子频道 @我 / 私信）需要 `PUBLIC_GUILD_MESSAGES`（`1<<30`）与 `DIRECT_MESSAGE`（`1<<12`）。
   * **审批按钮**点击靠「互动事件」`INTERACTION`（`1<<26`）。
   * 默认 intents = `1<<25 | 1<<30 | 1<<12 | 1<<26` = `1174409216`。只玩群聊就填 `100663296`（`1<<25 | 1<<26`）。
4. 想让它只理你一个人：先随便发一句，机器人会回你「不在白名单」并把你的 **openid** 报出来，
   把那个 ID 填进 `QQ_BOT_ALLOWED_USERS` 再重启。

> `QQ_BOT_ALLOW_ANYONE=1` 意味着**任何**能给机器人发消息的人都能让这台电脑启动游戏自动化、
> 改写 BetterGI 配置。除非你在做公开演示，否则别开。

### 2.2 启动

```bash
python main.py qq            # 等价：python qq_main.py
python main.py qq --check    # 只校验 AppID/Secret 并取一次 token，不连网关
python main.py qq --intents 33554432
python main.py qq --allow-anyone
python main.py qq --no-buttons   # 审批屏不挂按钮（纯文本 y / t）
python main.py qq --force        # 无视"已有实例在跑"的锁，硬启动第二个进程
python main.py qq --setup-menu   # 配单聊底部自定义菜单（✅ 执行 / 🧪 测试 / 🚫 取消）
python main.py qq --show-menu    # 看当前菜单
```

**也可以在 Studio（GI-Agent-Studio.exe）里跑**：左侧「远程通道」页直接启停、日志内嵌，
点「启动 Agent」时会按 `AUTO_START_QQ_BOT`（默认 1）一起唤醒 —— 但**必须先填 AppID/Secret**，
否则跳过并把缺哪一项写在通道日志里。详见 `docs/STUDIO.md` 第 3.1 节。

**同一个 AppID 只能跑一个进程**：两个进程一起连网关会互相抢会话（后一个 IDENTIFY 把前一个踢下线），
表现是"消息时有时无 / 同一条回两遍 / 记忆文件互相覆盖"。所以启动时会拿一个系统文件锁
（`memory/qq_bot.lock`，进程退出或崩溃时由系统自动释放），已经在跑就直接拒绝并提示
（这条提示会出现在 Studio 的通道日志里；确实要强开就加 `--force`）。
`--check` 不占这个锁，机器人跑着的时候也能随时体检。

Windows 上也可以直接双击 `启动QQ机器人.bat`（会自动设好 `PYTHONIOENCODING=utf-8`，
优先用 `venv\Scripts\python.exe`，没有 venv 就退回系统 PATH 里的 `python`）。

日志里看到这些就算成功：

```text
🔑 已获取 QQ access_token（有效期 7200 秒）
🔗 连接网关：wss://api.sgroup.qq.com/websocket
✅ 已发送 IDENTIFY（intents=1107300352）
🤖 QQ 机器人已启动：AppID 10001｜intents=1107300352｜白名单=xxx
```

`--check` 现在会一路走到底：取 token → 取网关地址 → **真连一次网关**并等它的第一帧（HELLO）：

```text
配置：AppID 10001｜intents=1107300352｜白名单=xxx
🔑 已获取 QQ access_token（有效期 6765 秒）
✅ 配置可用，网关地址：wss://api.sgroup.qq.com/websocket
✅ 网关握手成功，首帧：{"d":{"heartbeat_interval":41250},"op":10}
```

**光能取到 token 不等于能用** —— 取网关地址那一步曾经因为路径写错而失败（见 4.2），所以
`--check` 必须连到 HELLO 才算通过。

---

## 3. 在 QQ 里怎么用

和 CLI 一样说话就行：

| 你说 | 结果 |
| --- | --- |
| `今天体力怎么花` | 出今日规划 + 审批屏（⚔️ 体力目标 / 🗺️ 自由任务），底部带按钮 |
| 点 **✅ 执行** / **🧪 仅改配置** / **🚫 取消** | 等价于回复 `y` / `t` / `取消` |
| `y` / `确认` / `执行` / `同意` / `批准` / `yes` | 执行计划（拉起 BetterGI） |
| `t` | 只改配置、不启动游戏（推荐先这么试） |
| `取消` / `算了` / `不用了` | 扔掉这轮待审批计划，不写配置、不启动 |
| 别的话（如 `我要改成刷绝缘本`） | 驳回当前计划，并把新要求喂回大模型重新规划 |
| `refresh` | 立刻重拉展柜（Enka），不等 TTL |
| `history` | 当前历史消息条数 |
| `clear` | 清空对话记忆 |
| `exit` | 服务在跑，回一句「无需手动退出」 |

长回复会自动分段（默认 800 字/段，优先按段落切，最多 4 段），并按官方要求递增 `msg_seq`。

### 3.2 审批按钮（点一下 = 回复 y / t / 取消）

审批屏底部可以挂官方按钮，不用再手打 `y`：

```text
🛑 [系统拦截] 请确认是否执行上述计划？
⚔️ 体力目标：秘源机兵·构型械
🌿 采集目标：清水玉、慕风蘑菇、沙脂蛹

   [ ✅ 执行 ]  [ 🧪 仅改配置 ]  [ 🚫 取消 ]
```

* **✅ 执行** → 等价回复 `y`（真拉起 BetterGI）。它会真的启动游戏自动化，所以配了官方
  **二次确认弹窗**（modal）：先弹"将关闭 BetterGI 并写入配置，然后启动一条龙。确定吗？"。
* **🧪 仅改配置** → 等价回复 `t`（只写配置，不启动游戏）。
* **🚫 取消** → 等价回复 `取消`：扔掉这轮计划，**不写配置、不启动**，也不去打扰大模型重新规划
  （以前的版本里"取消"会被当成"新要求"喂回模型，现在有专门的处理）。
* 三个按钮在同一个 `group_id` 里：点过任意一个，其余会自动变灰，避免重复点"执行"下发两次。
* 低版本 QQ 会显示 `unsupport_tips`（"直接在群里回复 y（执行）或 t（仅改配置）即可"），文字通道始终可用。

实现走的是官方三段式（[消息按钮](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/msg-btn.html)、
[互动事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html)）：

1. 审批消息用 `msg_type=2`（Markdown）+ `keyboard` 发出（只有审批屏才挂按钮，其它消息照旧纯文本）；
2. 玩家点击 → 平台推 `INTERACTION_CREATE`（`d.type=11`，intent `1<<26`），我们从
   `d.data.resolved.button_data` 读出按钮标识；
3. **先 ack 再干活**：立刻 `PUT /interactions/{interaction_id}` `{"code":0}`
   （官方要求 3 秒内回应，否则玩家手机上一直转圈），然后才把 `y` / `t` / `取消`
   丢进和"打字"完全相同的审批入口（含消息去重，同一次点击不会被处理两遍）。

回复按钮点击的消息用的是 `event_id` 凭据（官方规定 `msg_id` / `event_id` 二选一），
所以会话标识长这样：`qq:group:<group_openid>#event:<事件 ID>`。

**⚠️ 按钮需要平台开通**：官方文档里，自定义按钮是【内邀开通】、模板按钮是【申请使用】。
没开通时发送会返回错误，程序会**自动退回纯文本**并把原因提示一次（不会卡住审批流程），
本次运行的后续消息也不再白试；想彻底关掉就设 `QQ_BOT_BUTTONS=0`。

**⚠️ 也可能"静默没按钮"**：有些情况平台不报错、只是把 `keyboard` 丢掉了（消息正常发出来但没有按钮）。
那就说明同样需要去平台申请按钮权限 —— 这时日志里不会有 `⚠️ QQ 按钮发送失败`，
但玩家也看不到按钮，文字通道照旧可用。

**⚠️ 别删 intents 里的 `1<<26`**：那是互动事件订阅位（默认值里已经带了）。少了它按钮能显示但点了没反应。

### 3.2.1 拿不到按钮权限怎么办：单聊自定义菜单（不需要内邀）

消息按钮要平台开通，**自定义菜单不用**。官方文档：[自定义菜单与指令面板](https://bot.qq.com/wiki/develop/api-v2/server-inter/menu-panel/)、
[修改全局自定义菜单](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_menu.put.html) ——
菜单展示在**单聊窗口底部**，`send_message` 类型的菜单项点一下会把文本自动填进输入框（再按一下发送）。

本程序已经把它接好了：

```bash
python main.py qq --setup-menu   # 设置菜单：✅ 执行(y) / 🧪 测试(t) / 🚫 取消(取消)
python main.py qq --show-menu    # 看当前菜单
```

* 机器人启动时会自动检查一次：**菜单为空就自动配上**；如果这台机器人已经有别人设的菜单，就不动它，
  只提示可以用 `--setup-menu` 覆盖。
* 官方限制：`name` 上限 10 字符（**一个汉字算 2 个字符**），items 最多 10 个，仅单聊（C2C）生效，
  `PUT /v2/menu` 限 5 QPM。
* 和消息按钮的区别：菜单是**常驻**的（不跟着某条审批屏走），点一下只是**填字**、还要按发送；
  好处是零门槛，而且填进去的就是审批词 `y` / `t` / `取消`，后端逻辑完全一样。

> 实测：`python main.py qq --setup-menu` → `✅ 已设置单聊自定义菜单（版本 41）`，
> 回读 `--show-menu` 能看到三项（`✅ 执行 → send_message='y'` …），无需任何平台审核。

---

### 3.3 手机上看到的消息是"精简版"

Agent 的回复本来是给终端 + 飞书写的：大模型推理正文 + ```` ```json ```` 计划块 + 审批屏，
一条就有 2000+ 字符。原样丢到 QQ 上的实测效果是 **一条审批被切成 4 条消息**，还把
「🔎 角色解析：蓝砚」和后半句拆到了两条里 —— 手机上根本读不下去。

所以 QQ 通道做了两件事（都在显示层，不动业务逻辑）：

1. **纯文本化**（`channels/chat_text.py`，注册给 `channel_router` 的 formatter）：
   去掉 ```json 计划块、`###` 标题、`**加粗**`、`---` 分隔线，压掉多余空行；
2. **精简审批**（`channel_router.register_concise`）：QQ 只收"要执行什么"，不带推理正文：

```text
🛑 [系统拦截] 请确认是否执行上述计划？
⚔️ 体力目标：秘源机兵·构型械
🌿 采集目标：清水玉、慕风蘑菇、沙脂蛹
🔎 角色解析：蓝砚 的突破材料「秘刻金纹的源核」→「秘源机兵·构型械」

👉 y = 执行　t = 仅改配置　其他内容 = 驳回并重新规划
```

同一份规划，实测 **764 字 → 144 字、1 条消息**；玩家那次 4 条的情况也归到 1 条。

**信息没丢**：完整推理会打到电脑端日志（`Studio → 日志` 或终端里搜 `🧠 规划全文`），
原始文本也照旧完整写进 `memory/chat_context.json`。想让 QQ 也收全文就设
`QQ_BOT_REPLY_MODE=full`（会退回刷屏模式）。飞书 / 终端不受影响，照旧发全文。

---

## 4. 实现细节（对排错有用的部分）

### 4.1 被动回复凭据
QQ 官方接口不给机器人"主动推送"的余量：回消息必须带 `msg_id`（被动回复），**5 分钟内**有效。
所以会话标识长这样：

```text
qq:group:<group_openid>#<msg_id>    群聊   POST /v2/groups/{group_openid}/messages
qq:c2c:<user_openid>#<msg_id>       单聊   POST /v2/users/{user_openid}/messages
qq:guild:<channel_id>#<msg_id>      频道   POST /channels/{channel_id}/messages
```

同一条消息的多段回复要递增 `msg_seq`，否则官方会当重复消息丢掉——`split_message` + `send_message`
已经处理了。**但 `msg_seq` 的含义比"这次消息的第几段"更严**（下一节）。

**副作用**：审批屏发出后如果玩家 5 分钟内没回 `y`，那条回复凭据就过期了（后续消息发不出去，日志会打
HTTP 4xx）。这是官方限制，不是 Agent 的 bug；重新发一句话就会开一段新的会话凭据。

### 4.1.1 msg_seq 是"对同一条玩家消息的第几次回复"（踩过的坑：HTTP 400 40054005）

官方规则：`msg_id + msg_seq` 这个组合**不能重复**，而且**同一条玩家消息最多回复 5 次**。
所以序号不是"本次发送的第几段"，而是按凭据累计的。玩家实测踩到过：

```text
👤 去采集月莲，只要月莲
🚫 审批已驳回。正在将你的要求反馈给大脑重新规划...   ← 第 1 条回复（msg_seq=1）
🛑 [系统拦截] 请确认是否执行上述计划？…              ← 第 2 条回复，也用了 msg_seq=1
❌ QQ 消息发送失败：HTTP 400 {"message":"消息被去重，请检查请求msgseq","code":40054005}
```

"驳回并重新规划"这类流程天然会对同一条消息回两条（撤销提示 + 新审批屏），所以现在按**凭据**记账：

* 同一个 `msg_id`（或按钮点击的 `event_id`）连续回几条，就用 `msg_seq = 1、2、3…`；
* 分段的长回复每段各占一个序号，下一轮回复接着往后排；
* 满 5 次就停下并提示"让玩家再发一句话"（被动回复额度是按"每条玩家消息"算的）；
* 记账是内存里的，凭据 6 分钟后自动清理；万一序号对不上（例如机器人中途重启过），
  平台回 `40054005` 时程序会**自动换一个序号重发一次**，不会让消息就这么丢掉。

### 4.1.2 聊天通道会"少发消息"（quiet 模式）

一次带配置的执行本来会连发六条：`⚙️ 指令已确认` → `🧾 配置事务已提交` → `🗂️ 备份与 Diff` →
`🚀 正在通过任务计划启动` → `🎉 任务计划已触发` → `✅ 已确认 BetterGI 开始执行任务。`

玩家实测的后果不只是刷屏：官方有 **"同一条玩家消息最多回复 5 次"** 的限制，这六条正好把额度吃光，
于是 23 分钟后真正的完成报告被平台判超限、发不出去。

所以 `channel_router.register_chat_channel(prefix, quiet=True)` 之后，**QQ 上一轮执行只有两条**：

```text
你：y
机器人：✅ 已确认 BetterGI 开始执行任务。          ← 哨兵确认真开跑了才发
（几十分钟后）
机器人：🎉 BetterGI 一条龙执行完成！(或 ⚠️ 脚本内有 N 处失败 / ⛔ 被取消)
```

具体规则（`bgi_controller.send_notice`）：

| 消息 | 参数 | QQ 发不发 |
| --- | --- | --- |
| 启动过程的进度消息（`⚙️ 指令已确认`/`🚀 正在启动…`） | `progress=True` | ❌ |
| 配置回执（`🧾 配置已写入…`/`🗂️ 备份与 Diff…`） | `chat=False` | ❌（终端、Studio、飞书照旧） |
| 启动成功回执（`🎉 已触发一条龙（任务计划 StartBetterGI）`、回退方案的 `🎉 已直接拉起…`） | `chat=False` | ❌ |
| `✅ 已确认 BetterGI 开始执行任务。` | 哨兵直接发 | ✅ **玩家要求只留这一句** |
| 完成 / 有脚本内失败 / 被取消的报告 | 哨兵直接发 | ✅ |
| 报错、以及需要玩家动手的消息（关不掉 BetterGI、**启动失败**） | `chat` 默认 | ✅ 一条都不少 |
| `t`（只改配置不启动）的 `🛠️ 配置已改好` | `chat` 默认 | ✅（那是那条命令唯一的反馈） |

> 回滚 ID 不在 QQ 上出现没关系：终端与 Studio 日志里照旧有完整事务路径，
> 而在电脑上直接说 `rollback` 会自己列出最近的事务。

还有两条是"玩家必须知道"的，照发（都在下面 §4.1.5）：

| 消息 | 什么时候发 |
| --- | --- |
| `⏳ 检测到 BetterGI 正在跑任务：先等它跑完再自动继续（最多等 N 分钟），你不用重新下一次指令。` | 下发计划时 BGI 真的在跑任务（**只等的时候发一次**，不刷屏） |
| `🈳 这次点名的目标在 BetterGI 里没有任何可执行的路线，已跳过启动` | 点名了目标、但一条路线都没命中（材料不在脚本组里 / 全在冷却里） |

**代价**：QQ 上不再有"已触发"这类回执，所以启动如果卡住（比如回退方案弹了 UAC 窗口没人点），
你要等到哨兵 3 分钟后的那句
`⚠️ 已等待 3 分，仍未检测到一条龙运行迹象。`（那条提示里已写明 UAC / 热启动等可能原因）。

### 4.1.3 迟到通知：20 分钟后才出的完成报告怎么送

一条龙可能跑几十分钟，等它跑完时：被动回复凭据（5 分钟）早过期了，回复额度（5 次）多半也满了。
所以 `channels/qq_bot.py` 对这类消息做了三级兜底：

1. **被动回复**（正常路径，带 `msg_seq`）；
2. 凭据不可用（`40034024 请求参数msg_id无效或越权`）或额度用光 → **主动消息**（不带 `msg_id`/`msg_seq`，
   官方允许但有频控：个人认证 60/qpm、未认证 30/qpm）；
3. 主动消息也发不出去 → **排队**（`memory/qq_pending_notices.json`，每会话最多 5 条），
   **玩家下次说话时用新凭据自动补发**，并在日志里写明"已排队 N 条待补发通知"。

任何一步成功都不会重复发送；三步都失败的内容也一定在终端/Studio 日志里（`print` 从没停过）。

### 4.1.4 完成报告什么时候发（`skills/bgi_watcher.py`）

BetterGI 的完成标记是分层的，**不能见到"结束"就报完成**（实测日志，BetterGI 0.64.0）：

```text
→ 脚本执行结束: "01-月莲-护世森下-7个.json", 耗时: 2分5.495秒      ← 每条【路线】一次
→ "任务结束"                                                      ← 每个脚本一次
配置组 "地图素材" 执行结束                                         ← 每个【配置组】一次
一条龙和配置组任务结束                                             ← 整条【一条龙流程】结束（最权威）
```

所以判定规则是：

1. `一条龙和配置组任务结束` → 直接报完成；
2. 只看到 `配置组 "X" 执行结束` 时，先读出**本轮有几个组会跑**（当前生效的那份一条龙配置里
   开启着、且 `User\ScriptGroup\<名>.json` 确实存在的任务；内置任务如领取邮件/合成树脂不打这行，
   不算进去）。**全部**都打了结束行才算完成 ——
   两个组都开着时，第一组跑完就报 🎉 是错的（玩家实测过）；组名以脚本组文件里的 `name` 为准
   （一条龙任务可能叫「狗粮AAA」，日志里打的却是另一个文件里的「狗粮」）；
3. 有组跑完、但整条流程的结束标记一直不出现，且日志安静 5 分钟 → 按"结束"报，并注明没抓到流程标记
   （否则要白等到超时）；
4. **被停止/取消 → 报 `⛔`，不报 🎉**。实测坑：玩家按停止快捷键那次，日志是
   `配置组 "批量讨伐角色养成材料BOSS" 执行结束`（17:43:02）**然后**才 `任务被取消，退出执行`（17:43:03）
   —— 先有"结束"、一秒后才知道是被停了。所以判定终态前会再等 2 秒重读一次日志，
   把晚到的取消行收进来；另外只有"本轮确实跑起来过"（见过任务启动/脚本结束/配置组标记）才认取消，
   免得你在别的场景按一下停止快捷键就把还没开跑的这一轮误判掉。
   （`已获取取消令牌` 是 BGI 起任务时正常打的，**不**算取消信号。）
5. **脚本自己跳过的失败 → 标题改成 `⚠️`，并把失败明细列出来**。这类失败不阻断整条流程
   （脚本 `continue` 下一个目标），所以照样会打「配置组/一条龙执行结束」，光看完成标记会以为一切正常。
   实测那条 Boss 讨伐日志：

   ```text
   已进入征讨之花领奖界面
   领取失败，可能是原粹树脂不足，尝试关闭领取界面
   错误信息："识别文字[点击空白区域继续]在 3000ms 后超时未出现！"
   领取奖励失败：未成功回到主界面
   ❌讨伐『恒常机关阵列』失败: Error: 未成功回到主界面
   ```

   报告会变成 `⚠️ BetterGI 配置组执行结束（脚本内有 2 处失败）` + 明细 + 一句提醒
   （"可能已经领到奖励、也可能白扣了体力"）。识别这些句式：`❌讨伐『X』失败`、
   `💀战斗失败，跳过当前BOSS X`、`领取奖励失败：…`，外加一条兜底的 `❌…失败：`。

---

### 4.1.5 "BGI 还在跑 / 白开一次 BGI"：等它跑完 + 空计划不启动

实测（玩家 09-13 的 QQ 记录）：12:34:40 一条龙就跑完了，12:36:16 他再下一条指令（金蕨），
机器人却回 `⛔ 检测到 BetterGI 正在运行、且日志仍在更新（很可能正在跑任务）`，他只好**重下一遍**；
而那次点名的「金蕨」根本不在他的脚本组里，BetterGI 还是被冷启动了，日志里留下两次
`启用任务总数量: 0 / 没有配置,退出执行!`（12:36:45、12:37:09）。

两处都改了（判定细节见 README §4.6）：

1. **"在不在跑任务"看日志里的终态/启动标记**（`skills/bgi_controller._bettergi_task_state()`），
   不再看"日志最近有没有写" —— BGI 跑完一条龙后窗口还开着、空闲时也会零碎写日志，
   这正是那次误判的来源；
2. **真在跑 → 等它跑完自动继续**（`_wait_for_bettergi_idle`，轮询 10 秒 /
   最长 `BGI_WAIT_RUNNING_TASK_MINUTES`=20 分钟），等待期间只在开始时提醒一次；
3. **点名了目标却一条路线都没命中 → 跳过启动**（空计划保护），
   不再白开一次 BetterGI；没点名任何目标时保持原行为（照样启动）。

> 排查这类问题时看终端：`⏳ 检测到 BetterGI 正在运行（PID …，空闲状态）` 表示判定为空闲、
> 会关掉它冷启动；`⏳ 检测到 BetterGI 正在运行（PID …）且像是在跑任务，先等它跑完…` 才是真在跑。
> 用 `python -m skills.gather_cooldown --materials` 可以看"你脚本组里到底能采哪些材料"，
> 点名一个不在表里的材料就会走空计划保护。

---

### 4.2 取网关地址（踩过的坑：HTTP 426）

官方 v2 的「获取通用 WSS 接入点」是 **`GET /gateway`**
（[文档](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/gateway.get.html)），
返回 `{"url": "wss://api.sgroup.qq.com/websocket"}`；注意**带尾斜杠会 404**。

第一版实现误用了 `/websocket/`，那是 **WebSocket 入口本身**，普通 GET 会被 TAPISIX 网关挡回来：

```text
⚠️ QQ 连接中断：取网关失败：HTTP 426 WebSocket protocol violation:
   Upgrade header "" does not contain websocket；5 秒后重连…
```

现在的实现按顺序做了三件事，任何一步成功就继续：

1. `GET {api_base}/gateway`（官方正路）；
2. `GET {api_base}/websocket/`（旧路径，给别的区域/版本留后路）；
3. 都拿不到 → 直接连 `QQ_BOT_WS_URL`（默认 `wss://api.sgroup.qq.com/websocket`），
   WebSocket 握手时带上 `Authorization: QQBot <access_token>` 头 —— 实测这条直连路径可用且能收到
   `{"op":10,"d":{"heartbeat_interval":41250}}`。

所以以后再看到 426 不再是致命的：日志会写明"取网关地址的接口没成功，直接连官方地址"，
然后照常 IDENTIFY 工作。

### 4.3 跨线程投递

Agent 的规划跑在普通线程里，而 `httpx.AsyncClient` 属于事件循环，所以 QQ 通道注册给
`channel_router` 的是 `QQBotClient.send_threadsafe`：内部用
`asyncio.run_coroutine_threadsafe` 把发送甩回事件循环线程，并等最多 30 秒。

### 4.4 白名单为空 = 谁都不能用

`QQ_BOT_ALLOWED_USERS` 为空且没开 `ALLOW_ANYONE` 时，任何消息都会被拒（并回一份 openid 指引）。
这比"默认谁都能用"安全得多 —— 这台机器会真的启动游戏自动化。

⚠️ **白名单要填 openid，不是 QQ 号**。openid 是官方按"机器人 + 用户"维度生成的字符串，
和 QQ 号长得完全不一样；填错的表现就是"机器人一直说我不在白名单里"。
最省事的做法：先随便发一句，把它回给你的那串 ID 抄进 `.env`。

### 4.5 消息去重

`agent_router.PROCESSED_MESSAGES` 按 `message_id` 去重（TTL 300 秒）。网关断线重连或官方重推时
同一条消息不会被规划两遍（否则会重复写配置、重复拉起 BetterGI）。

### 4.6 编码与日志实时性

机器人是长跑进程，日志里到处是 emoji：`python main.py qq > bot.log` 这种把输出重定向到文件的场景，
控制台编码是 GBK 时一句 emoji 就会抛 `UnicodeEncodeError` 把进程搞挂。所以入口启动时会把
stdout/stderr 切成 UTF-8（失败只替换字符）**并开行缓冲** —— 否则重定向到文件时 Python 默认块缓冲
（攒满 8KB 才落盘），日志看起来像卡住了，排查连接问题会很难受。
`启动QQ机器人.bat` 也预设了 `PYTHONIOENCODING=utf-8`。

> 顺带一提：`python -m unittest` 全套跑完时偶尔能看到一条
> `ResourceWarning: unclosed event loop`，那是**飞书 SDK** (`lark_oapi.ws.client`) 在 import 时调用
> `asyncio.get_event_loop()` 留下的缓存循环，与 QQ 通道无关。

### 4.7 与终端 / Studio 同时运行

QQ 通道是**独立进程**（`python main.py qq`），和终端对话模式、Studio 里的 Agent 是三个入口。
三者共用同一份记忆文件 `memory/chat_context.json`，所以：

* 想"在电脑上接着 QQ 里那段对话聊"是可行的（历史互通）；
* 但**别同时开着两个入口**：两边都会读写历史与 `pending_task`，后写的会盖掉前写的 ——
  表现是"我刚在 QQ 里批准的计划不见了"。

挂 QQ 机器人时就别再点 Studio 的「▶ 启动」，Studio 只用来改配置 / 看日志。

### 4.8 体检

```bash
python main.py doctor
```

会多出一行 QQ 通道状态：`未配置（可选）` / `已配置（AppID 10001），只认白名单里的 2 个 openid` /
配了密钥却没设白名单的告警 / `QQ_BOT_ALLOW_ANYONE=1` 的危险告警。

---

## 5. 加一个新通道（比如 Telegram / 微信）

不用碰 Agent 逻辑，两步：

1. 写一个 `send(target, text) -> bool`：从 target 里解析出会话 ID，把文本发出去。
2. 启动时注册前缀：

```python
from api import channel_router
channel_router.register_sender("tg:", my_sender)
```

之后任何 `feishu_api.send_feishu_msg("tg:12345", "…")` 都会走你的发送函数；把收到的消息交给
`channels.agent_router.handle_message_async(text, target, message_id)` 就算接通了。

---

## 6. 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| 不知道配没配对 | `python main.py qq --check`（一路验到网关 HELLO）；`python main.py doctor` 看通道状态那行 |
| `❌ 没配置 QQ_BOT_APPID / QQ_BOT_SECRET` | `.env` 没填，或没在项目根目录启动 |
| `取 token 失败：HTTP 401/403` | AppID 或 AppSecret 抄错；`python main.py qq --check` 复现 |
| `取网关失败：HTTP 426 … Upgrade header "" does not contain websocket` | 取网关地址用错了路径（老版本代码的问题，已修）。现在会自动改走 `/gateway` → 旧路径 → 直连 `QQ_BOT_WS_URL`，日志里会写"取网关地址的接口没成功，直接连官方地址"，然后照常工作 |
| 连上网关但收不到群消息 | 没申请「群聊/单聊消息」权限，或 intents 里没带 `1<<25` |
| 收到消息但机器人回「不在白名单」 | 填的是 QQ 号而不是 openid → 把机器人回给你的那串 ID 抄进 `QQ_BOT_ALLOWED_USERS` 再重启 |
| 回复报 HTTP 4xx / 消息发不出去 | 被动回复凭据超过 5 分钟；重新发一句话 |
| `HTTP 400 40054005 消息被去重，请检查请求msgseq` | 同一条玩家消息回超过 5 次（官方上限），或序号对不上；程序已按凭据记账并会自动换号重发，剩下只能让玩家再发一句话（见 4.1.1） |
| 一条消息被切成好几条 / 满屏 Markdown | 说明没走精简模式：确认 `QQ_BOT_REPLY_MODE=compact`（默认），日志里启动时会提示"回复精简模式" |
| 想看大模型完整的推理 | 设 `QQ_BOT_REPLY_MODE=full`，或直接在电脑端日志里搜 `🧠 规划全文` |
| 审批屏没有按钮 | ① 平台没开通按钮权限（日志会打 `⚠️ QQ 按钮发送失败 …`，已自动退回纯文本）；② 设了 `QQ_BOT_BUTTONS=0`；③ 频道场景不挂按钮。**替代方案见 3.2.1：单聊自定义菜单不需要内邀** |
| 完成报告（跑完 20 多分钟）没收到 | 已排队，玩家下次说话时自动补发（见 4.1.3）；日志里会写 `已排队 N 条待补发通知` |
| 按钮点了没反应（一直转圈） | intents 里少了互动事件位 `1<<26` → 把 `QQ_BOT_INTENTS` 清空用默认值，或自己带上 `1<<26` |
| 点按钮提示"没有权限" | 点的人不在 `QQ_BOT_ALLOWED_USERS` 白名单里（按钮本身谁都能点，判定在后端） |
| 启动时报"已经有一个 QQ 机器人进程在运行" | 同一个 AppID 不能跑两份（会互相抢会话）→ 先关掉那个控制台窗口，或加 `--force` |
| `⚠️ QQ 事件循环还没起来` | 消息在网关连上之前就到了（通常是刚启动）；等 IDENTIFY 后再发 |
| 反复 `⚠️ QQ 连接中断` | 代理问题：代码已把 `qq.com` 加进 `NO_PROXY`；仍不行就关掉系统代理再看 |

测试覆盖在 `tests/test_qq_bot.py`（协议、取网关的三级兜底、分段、白名单、发送体、真连本地假网关做
握手）、`tests/test_channel_router.py`（前缀分发）、`tests/test_agent_router.py`
（快捷指令、审批、去重、落点），全部不打外网。
