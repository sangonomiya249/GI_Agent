# 排错 FAQ

按现象查。技术细节（判定规则、日志标记）在 [内部机制](INTERNALS.md)，BetterGI 侧的准备工作在
[BetterGI 准备清单](BETTERGI_SETUP.md)。

先跑一次体检，多数问题它会直接指出来：

```bash
python main.py doctor
```

---

## 启动 / 安装

**双击 `GI-Agent-Studio.exe` 弹窗说找不到 venv**
exe 只是窗口宿主，后端是它拉起的 `venv\Scripts\pythonw.exe app_web.py`。按提示建好再双击：

```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

**装了依赖还是起不来（比如 ImportError: flask）**
用 venv 里的 Python 跑，别用系统的：`venv\Scripts\python.exe main.py`。
`启动QQ机器人.bat` / `启动GI-Agent控制台.bat` 在没有 venv 时会退回系统 `python`，依赖不全就会报错。

**提示「LLM_PROVIDER 没配」**
`.env` 不存在或没填。`copy .env.example .env`，至少填 `LLM_PROVIDER` / `OPENAI_API_KEY`（或对应提供商的 key）
和 `DEFAULT_UID`。只想验证模型通不通：`python scripts/check_llm_config.py`。

---

## 版本与更新

**「检查更新」说失败 / 说"这个仓库还没有发布过 release"**
只有那一行提示受影响：Agent、Studio、跑图都不需要网络，照常用。几种常见原因：
* **离线 / 被墙 / 代理没配好** → 文案是 `连不上 GitHub（ConnectionError）`。
  不想让它碰网络就设 `UPDATE_CHECK=0`（Studio 配置页有这一项）。
* **`这个仓库还没有发布过 release`** → GitHub 的 Releases 页面确实是空的，不是你的网络问题。
  维护者发版的顺序：把仓库根的 `VERSION` 改成新版本号 → 在 GitHub 上发布 release，
  tag 用同名的 `v1.0.1`（本地版本和 tag 就是靠这个对上的）；
  逐步操作见 [发一个新版本](RELEASE.md)。
* **限流** → GitHub 匿名接口每小时只有 60 次；程序默认把结果缓存 6 小时（`UPDATE_CHECK_HOURS`），
  只有点「检查更新」或 `python main.py update --force` 才真的重查。
* 检查哪个仓库由 `.env` 的 `UPDATE_REPO` 决定（默认 `sangonomiya249/GI_Agent`，fork 了改成自己的）。

**真提示有新版本了，怎么更新？**
程序**只告诉你**，不会自己下载或改文件：
* `git clone` 下来的：在项目目录里 `git pull`（有本地改动先 `git stash`）；
* 下载 zip 的：去 release 页面重新下载，解压覆盖到项目目录 —— **`.env` 与 `memory\` 别覆盖**
  （配置、密钥、冷却记录、虚拟账本都在里面）。

**改了配置没生效**
`.env` 是进程启动时读的，改完要重启 Agent / 机器人 / Studio。

---

## 任务没跑起来

**Agent 说"已跳过启动"（🈳 空计划保护）**
点名的目标在脚本组里一条路线都没命中 —— 材料名写错、或这个材料你根本没订阅路线。
用 `python -m skills.gather_cooldown --materials` 看当前能采哪些材料；路线清单在 Studio「任务与路线」页。
如果这轮你没点名任何目标，这条保护不会触发。

**任务看着启用了，BetterGI 却什么都不跑**
多半是"死键"：新格式一条龙配置里，没登记进 `TaskDefinitions` 的任务会被静默跳过。
关掉 BetterGI 后跑 `python main.py repair --force` 清理，再让 Agent 重新排一次。
详见 [BetterGI 准备清单 §5](BETTERGI_SETUP.md#5-一条龙任务为什么会静默不跑)。

**明明改了组，跑的还是上次那一套**
BetterGI 不支持热启动，而且它退出时会回写配置。Agent 会先关掉 BGI 再冷启动；
如果 BGI 是你手动开的、任务正在跑，它会**等你跑完**（默认最多 20 分钟）再继续。
（想缩短/延长：`BGI_WAIT_RUNNING_TASK_MINUTES`、轮询间隔 `BGI_RUNNING_TASK_POLL_SECONDS`。）

**采集时组里穿插了别的材料**
那是[防闪退隔离带](INTERNALS.md#防闪退隔离带)（每连续 150 条禁用插一条）。
不想插就 `BGI_FORCE_ENABLE_BAND=0`，代价是回到"日志爆发可能崩"的原始状态。

**说"去采集霜仙花"被拦，说还没刷新**
那是冷却没到（地区特产 48 小时；矿物 72、食材 24、魔物 12）。想硬跑就说「强制采集」；确定自己在游戏里采过就打一笔账：
`python -m skills.gather_cooldown --manual 霜仙花`。

**说「打蕈兽 / 只打骗骗花 / 去采集虹滴晶」，审批屏却回一句「认不出…」**
旧版的冷却词表只读四个"总组"（地图素材 / 矿物 / 食材与炼金 / 敌人与魔物），
而蕈兽 / 骗骗花 / 飘浮灵 / 虹滴晶 这些都在你**自己建的小组**里 —— 名字其实能跑，警告纯属噪音。
现在会一并扫脚本组目录里的小组（按目录前缀、矿种表、`AutoPathing/敌人与魔物` 的魔物名单定类别），
查到就照常算冷却；还是定不出类别的**魔物 / 矿物 / 食材**名字不再报警（这次不查冷却而已，路线该跑还是会跑），
只有地区特产那类保留「认不出…请直接说材料名」的提示。

---

## BetterGI 卡住 / 崩溃

**跑一半"卡死"，日志里全是 `不是原神，暂停`**
不是崩了，是**在等原神回到前台**。最省事的解法：BetterGI 设置里打开
「失去焦点时自动切回原神」。三层兜底见 [内部机制](INTERNALS.md#原神窗口前台兜底)。

**跑大组按停止快捷键（`Up`）后 BGI 崩溃（`0xc00000fd`）**
BetterGI 已知问题：会给每条 `Disabled` 路线写一行日志，几百条的大组在停止时日志瞬间爆发。
对策是让组里尽量全是 `Enabled` —— Agent 会自动精简大组（`BGI_ROUTE_GROUP_POLICY=shrink`，默认）。

**`目标传送点位于不可点击区域，传送失败`**
BetterGI / 路线本身的问题（重试 3 次后该路线失败），换个锚点或等地图加载完再跑。

**`战斗策略文件不存在` / 战斗打不起来**
路线组里配的策略名在 `User\AutoFight\` 下没有对应 `.txt`。注意 BetterGI 按**平铺**路径解析，
子目录里的策略要写成 `群友分享\四神队(进阶版)`。`python main.py doctor` 与 Studio「任务与路线」页都会列出来。

**讨伐一开始弹出要手点「保存并关闭」的窗口**
那是 Boss 脚本自带的配置编辑器。Agent 会自动把 `showEditorOnStart` 关掉（写进脚本组配置，走事务）；
没生效就在 BetterGI 里右键该脚本 →「修改JS脚本自定义配置」手动取消勾选。

**Boss 讨伐老是失败**
先换队伍（挂机输出 + 生存强的），机制 Boss（无相系列等）不在支持范围。也可能是脚本自己的定位问题，
按脚本作者的说明改对应 json。

---

## 关原神与关 BetterGI 的权限问题

**`无法终止 PID 为 xxx 的进程。原因: 拒绝访问。`**
原神通常是以管理员权限启动的（被管理员的 BetterGI / 启动器拉起），普通终端关不掉。
以管理员身份跑一次注册脚本，之后就走同等权限的计划任务（免 UAC）：

```powershell
powershell -ExecutionPolicy Bypass -File "<你的仓库路径>\scripts\setup_start_bettergi_task.ps1"
```

**Agent 说"还是判不出来"**
它不敢瞎说"已关闭"：进程查询被权限挡住时会如实报"查不出来"。检查任务管理器，或改用管理员终端。

**`schtasks` / 进程查询一直查不出来**
当前会话权限受限（沙箱、或被 UAC 限制）。用管理员终端跑，或看
[BetterGI 准备清单 §4](BETTERGI_SETUP.md#4-免管理员确认启动必做)。

---

## 远程通道

**QQ 机器人起不来**
先 `python main.py qq --check` 验 AppID/Secret。
报「已有一个实例在跑」是因为一个 AppID 只能跑一个进程（`memory/qq_bot.lock` 文件锁），确实要强开加 `--force`。

**QQ 上收不到完成报告**
一条龙可能跑几十分钟，被动回复凭据早过期了，实现里有三级兜底（被动回复 → 主动消息 → 排队等你下次说话时补发）。
期间可以在 Studio 的「远程通道」页或终端日志看进度。

**飞书服务端没反应**
`feishu_main.py` 是 Webhook 服务（监听 5000 端口），公网不可达就收不到消息 —— 需要内网穿透。
只想手机遥控的话，用 QQ 通道更省事（不需要公网入口）。

---

## 展柜 / 数据

**Agent 说「某角色不在展柜 JSON 中」**
展柜一次只有 8 个角色。CLI 里输入 `refresh` 重拉；想查展柜外的角色就配米游社 cookie
（[指南](MYS_COOKIE.md)），或者轮换展柜里的角色。

**米游社 cookie 失效 / 风控**
米游社改版或签名失效都会导致失败（salt 失效可在 `.env` 改 `MYS_SALT`）。
不想承担这个风险就别配，用展柜轮换。

---

## 配置改错了，想还原

```text
rollback            # CLI 里列出最近事务
rollback <事务ID>
```

每次写入都有备份与 Diff，见 [配置恢复](CONFIG_RECOVERY.md)。

---

## 我要提交 issue，需要带什么

1. `python main.py doctor` 的完整输出；
2. 出问题时终端 / Studio 日志里的那几行（Studio 有「BetterGI 日志」页，带关键字高亮）；
3. BetterGI 自己那份日志（`BetterGI\log\better-genshin-impactYYYYMMDD.log`）里对应时间段的片段；
4. 你当时说的那句话、以及 Agent 出的计划（审批屏那段）。
