# 🤖 GI_Agent · 原神自动化的最后一步

用一句话指挥 [BetterGI](https://github.com/letieu/BetterGI) 的一条龙。
说「去刷蓝砚的天赋」，Agent 自己看你的角色展柜、算好今天该刷什么、改好 BetterGI 配置、再把一条龙拉起来。

- 🧠 **会规划**：读角色展柜（Enka），结合材料缺口与今天星期几给建议；听得懂黑话（「去刷绝缘套」）。
- 🎮 **会干活**：自动地脉花 / 秘境（按角色武器与天赋推秘境）/ Boss 讨伐 / 圣遗物 / 大世界采集 / 刷怪 / 挖矿 / 锄大地 / 狗粮。
- ⏳ **会看冷却**：地区特产 48 小时、矿物 72、食材 24、魔物 12 —— 跑过的不重复跑，数据来自 BetterGI 日志，**你自己手动跑的也算**。
- 🛑 **会关游戏**：说「帮我关闭原神」，出计划等你点 `y` 再动手（可连 BetterGI 一起关）。
- 💬 **能远程**：终端 / [QQ 机器人](docs/QQ_BOT.md) / 飞书都能指挥，手机上点一下按钮就批准。
- 🖥️ **有界面**：[GI Agent Studio](docs/STUDIO.md) 一个窗口管配置、审批、日志、体检。
- 🔒 **不瞎写**：每次改 BetterGI 配置都先备份 + 出 Diff，不满意一条 `rollback` 还原。
- 🌱 **算得准**：配了米游社 cookie，材料需求由**米游社养成计算器**给出、真实库存由**米游社背包接口**读取，
  BetterGI 只负责跑 —— 它捡到多少从不写回库存，跑完自动重新同步再算缺口（[角色养成](docs/GROWTH.md)）。
- 🔄 **会看更新**：Studio 概览页 / `python main.py update` 对比 GitHub 上的 release，有新版本会告诉你（只读，不下载）。

> 它不帮你配置 BetterGI，只是**帮你改 BetterGI 的脚本配置并触发一条龙**。
> 所以「这条路线能不能跑」取决于你在 BetterGI 里已经订阅/建好的脚本组。

---

## 🚀 快速开始

### 0. 前置
Windows 10/11 + 已装好的 [BetterGI](https://github.com/letieu/BetterGI)（先手动跑通一次一条龙）。
在 BetterGI 的脚本仓库里订阅：`地方特产`、`批量讨伐角色养成材料BOSS`、`AAA-Artifacts-Bulk-Supply`（狗粮）、
`万能战斗策略（萌新推荐）`。
👉 调度器 / 一条龙要怎么建组，见 **[BetterGI 准备清单](docs/BETTERGI_SETUP.md)**。

### 1. 安装（Python 3.10+）
```bash
git clone https://github.com/sangonomiya249/GI_Agent.git
cd GI_Agent
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt     # Linux/WSL: venv/bin/python -m pip install -r requirements.txt
```
> ⚠️ venv **必须建在项目根目录、名字就叫 `venv`**：`GI-Agent-Studio.exe` 只是窗口宿主，
> 真正的后端是它拉起的 `venv\Scripts\pythonw.exe app_web.py`；三个 `启动*.bat` 也优先用 venv 里的 Python。
> `requirements.txt` 里只有 8 个必需依赖；`lark-oapi`（飞书）和 `playwright`（离线抓字典）是可选的，用不到可以注释掉。

### 2. 配置
```bash
copy .env.example .env        # Linux/WSL: cp .env.example .env
```
最少要填这两项（其余都有默认值）：
```dotenv
DEFAULT_UID=你的原神UID
OPENAI_API_KEY=你的模型密钥      # 配合 LLM_PROVIDER / MODEL_NAME / OPENAI_BASE_URL，也支持 DeepSeek 等兼容接口
```
配置页 / 自检：
```bash
python main.py doctor                    # BetterGI 路径、计划任务、脚本组、展柜缓存逐条体检
python scripts/check_llm_config.py       # 只测大模型通不通
```

### 3. 免 UAC 拉起（一次性，必做）
BetterGI 需要管理员权限，直接用会弹 UAC 窗口。以**管理员身份**打开 PowerShell 跑一次：
```powershell
powershell -ExecutionPolicy Bypass -File "<你的仓库路径>\scripts\setup_start_bettergi_task.ps1"
```
它注册 `StartBetterGI` / `StopBetterGI` / `StopGenshin` 三个计划任务，之后 Agent 就能静默拉起与关闭它们。

### 4. 启动
| 方式 | 怎么做 |
| --- | --- |
| 🖥️ 界面（推荐） | 双击 `GI-Agent-Studio.exe` 或 `启动GI-Agent-Studio.bat` |
| ⌨️ 终端 | `python main.py` |
| 📱 手机（QQ） | 先在 `.env` 填 `QQ_BOT_APPID` / `QQ_BOT_SECRET`，再双击 `启动QQ机器人.bat`｜详见 [QQ 机器人](docs/QQ_BOT.md) |

---

## 🎮 怎么用

```text
你：今天刷什么？           → 读展柜 + 今天星期几，给一份体力规划建议
你：去刷蓝砚的天赋         → 自动推导炼武秘境，并写好次数与树脂
你：去打一次散兵的突破材料 → 角色 → 突破材料 → 首领，绝不按元素属性瞎猜
你：去采集霜仙花           → 没刷新会告诉你还要等多久（想硬跑就说「强制采集」）
你：锄大地 璃月            → 只清璃月；不写地区就是全图（几百条路线，以小时计）
你：帮我关闭原神           → 出计划 → 回 y 才真关
```

**审批**：Agent 出计划后 —— `y` 执行 / `t` 只改配置不启动 / 其它内容当作驳回并重新规划。
建议前几次都用 `t`，在 BetterGI 里看清配置改了什么，再开始 `y`。

其它说法（刷怪、挖矿、食材、整脚本、圣遗物简述、锄大地与刷怪的区别）见
[使用说明](docs/USAGE.md)。

---

## 🌱 角色养成（米游社算需求，BetterGI 只负责跑）

配一份米游社 cookie，就能说「**把胡桃拉到 90，天赋 10/10/10，武器 90**」，
剩下的交给代码：材料需求和「已有多少」都问**米游社养成计算器**、缺口转成 BetterGI 任务；
BetterGI 跑完**自动重新同步再重算缺口**。

```text
角色目标（90 / 10-9-8 / 武器 90）
      ↓
米游社养成计算器 ──► 还差哪些材料、各多少   ← 需求只信这里
                 └─► 每种材料你已有多少     ← 「已有」也只信这里（available_material）
      ↓
材料族匹配 ──► 具体材料落到族群的 BetterGI 路线（异色结晶石 → 原海异种）
      ↓
按优先级 + 当前体力换算趟数 ──► 今日任务（多角色共享同一池材料，不重复规划）
      ↓
BetterGI 执行 ──► 跑完重新同步 ──► 重新算缺口（闭环）
```

三条硬规矩：

1. **BetterGI 不许改库存。** 它不知道自己捡了几个，本地 `+50` 只会越攒越离谱；
   「已有 / 还差」只在米游社同步后更新。
2. **代码不许硬编码材料需求表。** 需求表随游戏版本过期，过期的表现是"悄悄地规划错"；
   所以需求一律现算，材料来源也从**在线数据**里推（见 `docs/GROWTH.md`）。
3. **读不到就说读不到。** 体力读不到、材料算不出、秘境今天不开，界面上都会写明原因，
   绝不拿一个看起来很像的数字糊过去。

### 在 Studio 的 🌱 角色养成 页里能做什么

**① 添加角色**（「添加角色」按钮）

- **两个视图**：`已拥有` / `未拥有` 分栏（米游社关掉了"我的角色"接口，这里是逐角色查询后缓存的结果）；
- **批量导入**：一键添加本栏全部（例如把"已拥有"的一口气全加进来），搜索框按名字/id 过滤；
- 每个角色按当前状态（`Lv 81 · 天赋 1/10/6`）显示，已添加的会置灰；
- 「重新拉取拥有状态」：缓存不准时重新逐角色确认。

**② 目标表**（一行一个角色）

- **拖拽排序** + **序号输入框**：直接填"3"回车，这个角色就排到第 3 位；
- 勾选启用 / 停用、编辑目标、删除；
- 「已拥有 / 未拥有」可手动切换（拥有状态判定不准时自己改）；
- 「养成顺序」列显示每个角色内部 **角色等级 > 武器等级 > 天赋** 的实际次序。

**③ 批量修改默认培养目标**：一键把**全部角色**改成同一套目标（默认 `88 / 1-9-9 / 武器 90`），
新添加的角色也按这套默认值建目标。

**④ 执行**：按优先级挑一条体力任务 + 若干不耗体力的路线；可选
**一次性全部下发** 或**分批次**（推一条路线 → 回 `y` 就跑它 → 跑完推下一条）。
没有可执行路线的材料会**跳过**（明细表里照常显示，不占批次）。

**⑤ 体力与天数**：卡片显示**你实际的体力**（米游社体力接口被风控时，可以手动记一次，
程序按 8 分钟 1 点自动回涨推算），并按它换算趟数；「预计培养天数」是**所有已添加角色合计**
（会给出每人占比）。

角色顺序默认 **5 星 → 4 星**，角色内部默认 **角色等级 > 武器等级 > 天赋**，都能改。

```bash
python -m skills.mys_calculator --check   # 自检：角色列表 / 算材料，哪一步先炸一目了然
python -m skills.mys_inventory --sync     # 手动同步一次（等价于点界面上的按钮）
python -m brain.growth_tools --plan       # 看当前养成计划
npm run knowledge                         # 重算"材料族知识表"（需要 Node，见 docs/GROWTH.md）
```

完整说明、配置项、排错表见 **[角色养成](docs/GROWTH.md)**。

---

## ⏳ 资源冷却（四类刷新时间）

游戏里的材料刷新时间不一样，Agent 按类别分开算，冷却没到就不硬跑：

| 类别 | 默认刷新 | 典型目标 |
| --- | --- | --- |
| 🌿 地区特产 | 48 小时 | 霜仙花 / 清水玉 / 月莲 / 星螺 |
| ⛏️ 矿物 | 72 小时 | 水晶块 / 紫晶块 / 夜泊石 / 石珀（按矿种细分，见下） |
| 🍳 食材与炼金 | 24 小时 | 久雨莲 / 甜甜花 / 薄荷这类食材（**得先建组**，见下） |
| 👹 敌人与魔物 | 12 小时 | 巡陆艇这类讨伐路线 |

矿物的刷新时间各矿不同：铁块与白铁块 24 小时、星银矿石 48、水晶块与紫晶块 72、
魔晶块 24、电气水晶 48、夜泊石与石珀 72 —— 都写死在 `skills/gather_cooldown.py` 里，不用自己填。

```text
你：去采集霜仙花
Agent：⏳ 霜仙花：09-13 12:34 采过（40 分钟前），还没刷新，还要等约 1 天 23 小时
       （想强跑就说「强制采集」）
```

* 类别不是猜的：看**这个材料出现在你的哪个脚本组** —— 四个总组（地图素材 / 矿物 / 食材与炼金 /
  敌人与魔物）加上你**自己建的小组**（蕈兽.json / 骗骗花.json / 虹滴晶.json… 也认）；
* 数据来自 **BetterGI 自己的日志**，所以你在 BGI 里手动跑过的也算；
* 表里列**整类材料**，不是"你的脚本组里那几种"：Agent 每次跑完会把脚本组精简成"本次要跑的几条"，
  完整的目录清单在 `User/ScriptGroup/.gi_agent_archive/`（实测「食材与炼金」组文件只剩 3 条竹笋，
  归档里是 2342 条 / 59 种材料）—— 所以「去采集竹笋」跑完之后，竹笋会老老实实进 24 小时冷却；
* 被打断/失败的路线不算，只跑了零星几条也**不算跑完**（门槛 `GATHER_COOLDOWN_MIN_ROUTE_PERCENT`，默认 80%）；
* 说角色名也行（「去采集奥黛塔的突破材料」）—— 会先解析成对应材料；认不出来会老实说认不出，绝不瞎猜；
* 游戏里自己采过、日志里没有的，登记一笔即可：

```bash
python -m skills.gather_cooldown                   # 现在哪些还在冷却（按类别分区）
python -m skills.gather_cooldown --manual 霜仙花    # 记为「刚采过」
python -m skills.gather_cooldown --materials       # 脚本组里能采的材料清单
```

四类各等多久、认不认「强制采集」，都在 Studio「配置」页（`GATHER_COOLDOWN_HOURS` /
`MINE_COOLDOWN_HOURS` / `COOK_COOLDOWN_HOURS` / `HUNT_COOLDOWN_HOURS`）。
**想看谁在冷却、还要等多久**：Studio 左侧「🌿 资源冷却」页**点顶部类别切换**（默认地区特产，
点矿物 / 食材与炼金 / 敌人与魔物只看那一类；每行还能「记为刚刷过」登记自己在游戏里采的）。
表里列的是**整类材料**（实测：地区特产 59 种、矿物 8、食材与炼金 59、敌人与魔物 55）——
清单来自你 BetterGI 路线仓库的**全量目录**，不是"上次跑过的那几种"，
所以没跑过的也在表里，显示为「没有记录（按已刷新处理）」。命令行也可以 `python -m skills.gather_cooldown`。
实现细节（怎么从日志判定、隔离带为什么会影响判定）见 [内部机制](docs/INTERNALS.md)。

---

## 🔄 检测更新（GitHub release）

```bash
python main.py update            # 看有没有新版本（等价 python -m skills.update_check）
python main.py update --force    # 忽略 6 小时缓存，立刻重查
python main.py update --json     # 给脚本用
```

Studio「系统 → 版本更新」页也有一份：本地版本、最新 release、发布时间、更新说明，点「检查更新」忽略缓存重查，
「打开发布页」用系统浏览器打开 release 页面。**只读** —— 不下载、不改任何文件，要不要更新你自己定：
git 用户 `git pull`，zip 用户去发布页下载覆盖（`.env` 与 `memory\` 别覆盖）。

* 本地版本取仓库根的 `VERSION`；没有就退回 `git describe --tags`；
* 连不上时（离线 / 代理 / 证书报 `SSLError`）会把每次尝试的原因写出来，并给出下一步：
  代理地址填 `UPDATE_PROXY`，证书问题填 `UPDATE_CA_BUNDLE`（程序也会自己试 `.git\win-ca-bundle.pem`）；
* 检查哪个仓库看 `.env` 的 `UPDATE_REPO`（默认 `sangonomiya249/GI_Agent`，fork 了改成自己的）；
  不想要这个功能就设 `UPDATE_CHECK=0`（完全不会碰网络）。
* **维护者发版**：改 `VERSION` → 在 GitHub 上发布 release，tag 用同名的 `v1.0.1`
  （别勾 pre-release：`releases/latest` 看不到它）。

---

## 📚 文档索引

| 文档 | 讲什么 |
| --- | --- |
| [BetterGI 准备清单](docs/BETTERGI_SETUP.md) | 订阅哪些脚本、调度器与一条龙要怎么建组、免 UAC 计划任务、任务为什么会「静默不跑」 |
| [使用说明](docs/USAGE.md) | 各种说法分别对应哪类任务、审批与驳回、展柜刷新、rollback |
| [Studio 界面](docs/STUDIO.md) · [旧版控制台](docs/GUI.md) | 各页面说明、重新编译 exe、排错 |
| [模型配置](docs/LLM_QUICK_START.md) · [提供商对照](docs/LLM_PROVIDER_GUIDE.md) · [本地模型](docs/LOCAL_MODEL_GUIDE.md) | 用哪家模型、怎么填 `.env` |
| [QQ 机器人](docs/QQ_BOT.md) · [米游社 cookie](docs/MYS_COOKIE.md) | 手机远程控制 · 查展柜外的角色 |
| [角色养成](docs/GROWTH.md) | 米游社算材料需求 / 读真实库存，缺口转 BetterGI 任务并自动闭环 |
| [配置恢复](docs/CONFIG_RECOVERY.md) | 事务、Diff、`rollback` |
| [排错 FAQ](docs/TROUBLESHOOTING.md) | BGI 卡死 / 崩溃 / 关不掉 / 讨伐弹窗 / 白开一次 BGI … |
| [内部机制](docs/INTERNALS.md) | 架构与目录、日志判定、防闪退隔离带、冷却判定细节（开发者向） |

---

## ⚠️ 免责与致谢

* 本项目是「**修改 BetterGI 配置 + 触发一条龙**」的调度层，不注入、不修改游戏客户端。
  请自行遵守游戏与 BetterGI 的相关条款，账号风险自负。
* 只可靠处理代码里已适配过的任务类型；大模型不会去改任意 BetterGI 脚本。
* 上游：fork 自 [box-opener/GI_Agent](https://github.com/box-opener/GI_Agent)（上游未附带 LICENSE）。
  依赖 [BetterGI](https://github.com/letieu/BetterGI) 与各位脚本作者（地方特产 / 批量讨伐 / 狗粮批发 / 锄地路线…）。
