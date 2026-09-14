# 内部机制（开发者向）

这份文档写"为什么这么实现"，以及各种手感的来源。只想用的话看 [README](../README.md) 与 [使用说明](USAGE.md)。

---

## 架构

模块化分层，确保大模型"脑抽"时不会影响外挂的物理执行：

| 目录 | 职责 |
| --- | --- |
| `brain/` | 记忆管家 `memory_manager.py`、LLM 思考与拦截层 `llm_brain.py` |
| `skills/` | 执行层：BetterGI 控制器 `bgi_controller.py`、展柜读取 `env_reader.py`、黑话字典 `artifact_match.py`、冷却 `gather_cooldown.py`、关原神 `game_control.py`、窗口 `window_focus.py`、米游社 `mys_api.py` |
| `api/` | 外部通信：飞书收发 `feishu_api.py`、通道分发 `channel_router.py` |
| `channels/` | 聊天通道：`agent_router.py`（飞书/QQ 共用：快捷指令、审批、去重）、`qq_bot.py`（QQ 官方机器人 WebSocket 网关） |
| `studio/` | 原生界面后端（本地 Flask）+ `scripts/native/StudioHost.cs`（WinForms + WebView2 宿主） |
| `memory/` | 运行期数据：字典、上下文状态、虚拟账本 |
| `prompts/` | `system_rules.md`（喂给模型的规则） |

加新通道只要注册一个 target 前缀 + 一个发送函数，不用碰规划/审批/事务逻辑。

### 目录结构

```text
GI_Agent-main/
├─ main.py              终端对话 / 一次性命令（doctor、repair、qq、studio、gui）
├─ qq_main.py           QQ 机器人入口（等价 python main.py qq）
├─ app_web.py           Studio 启动器（本地 Flask + 原生窗口）
├─ feishu_main.py       飞书 Webhook 服务端
├─ gui.py               旧版 tkinter 控制台（备用）
├─ config.py            配置与路径（所有仓库内路径都从这里拼绝对路径）
├─ .env / .env.example  配置（.env 不进版本库）
├─ GI-Agent-Studio.exe  原生窗口宿主
├─ 启动*.bat            双击入口：Studio / QQ 机器人 / 旧版控制台
├─ api/ brain/ channels/ skills/ studio/   代码
├─ prompts/ memory/     提示词与运行期数据
├─ assets/              图标等静态资源
├─ docs/                文档
├─ scripts/             辅助脚本 + 原生宿主源码（scripts/native/）
├─ tests/               单元测试（python -m unittest discover -s tests -t .）
└─ （本地才有，不进仓库）venv/  build/  __pycache__/  .studio-lib/  .studio-profile/  logs/
```

约定：

* 入口脚本与 `config.py` 留在根目录（导入、launcher、文档都按这个路径写），其余脚本进 `scripts/`；
* `memory/` 里的字典是运行期依赖，**不要手工清空**；而 `chat_context.json`（对话记录）、
  `mys_characters.json`（展柜快照）、`agent_registered_tasks.json`（任务台账）、
  `gather_cooldown_manual.json`（手记的冷却）是**你的个人数据**，已在 `.gitignore` 里，别提交；
* 从计划任务/快捷方式启动也没问题：`config.PROJECT_ROOT` 让路径与工作目录无关。

---


---

## 体积与瘦身

`GI-Agent-Studio.exe` 只有约 2 MB —— 它是个 **WinForms + WebView2 的壳**：界面用系统里的 WebView2 渲染
（不打包浏览器内核），真干活的后端是 Python（exe 负责把它拉起来）。

| 部分 | 典型大小 | 说明 |
| --- | --- | --- |
| 仓库里被提交的东西 | 约 16 MB | 代码 + `assets/` + `memory/` 字典（8 MB）+ exe（2 MB） |
| `venv/` | 约 190 MB | **不进仓库**，按 `requirements.txt` 自己装；其中约 139 MB 是两个可选项 |
| `.studio-profile/` | 十几 MB～几百 MB | WebView2 档案目录：缓存、组件，**还有 `Network\Cookies` 与 `Login Data`**（所以也上了 `.gitignore`）。关掉 Studio 后整个删掉即可，会自动重建 |
| 系统 WebView2 运行时 | 约 1 GB | `C:\Program Files (x86)\Microsoft\EdgeWebView`，Windows/Edge 装的，不是本项目的 |
| BetterGI 本体（对照） | 约 1040 MB | Assets 473 MB + Repos 130 MB + exe 98 MB + 原生 DLL ~190 MB + WebView2Data 52 MB |

可选依赖想省空间：

```powershell
venv\Scripts\pip uninstall playwright    # 约 101 MB：只有"重建本地字典"（爬 yatta / 百科）才需要
venv\Scripts\pip uninstall lark_oapi     # 约 37 MB：只有飞书通道才需要（代码里是按需 import）
```

看自己机器上的实际占用：`python scripts\disk_report.py`（只读，不删东西）。

---

## 资源冷却的判定

* **四类资源、四套时长**（都按 `skills/gather_cooldown.py` 里的类别模型走）：

  | 类别 key | 面板名 | 默认时长 | `.env` 键 | 依据 |
  | --- | --- | --- | --- | --- |
  | `specialty` | 🌿 地区特产 | 48 h | `GATHER_COOLDOWN_HOURS` | 特产「采集后经过 48 小时刷新」 |
  | `mine` | ⛏️ 矿物 | 72 h | `MINE_COOLDOWN_HOURS` | 水晶块/紫晶块「第三日」；矿种另有覆盖，见下 |
  | `cook` | 🍳 食材与炼金 | 24 h | `COOK_COOLDOWN_HOURS` | 大部分食材每日刷新 |
  | `hunt` | 👹 敌人与魔物 | 12 h | `HUNT_COOLDOWN_HOURS` | 动物/魔物按 12 小时刷新 |

* **类别是算出来的，不是猜的**：`_collect_group_paths()` 读 `BGI_MAP_CONFIG` / `BGI_MINE_CONFIG` /
  `BGI_COOK_CONFIG` / `BGI_ENEMY_CONFIG` 四个脚本组，建 `材料 → 类别` 索引（`material_category_index()`）；
  同一个材料出现在多个组里时，按 `CATEGORY_ORDER`（特产 → 矿物 → 食材 → 魔物）取先出现的那类。
  `hours_for()` 先套材质覆盖再退回类别默认值 —— 所以 `夜泊石` / `石珀` 虽然躺在"地图素材"组里，
  照样按矿物 72 小时算（wiki 把它们列在"矿石破坏后掉落"里）。
* **矿种覆盖**：`MATERIAL_HOURS` 写死了 wiki 上逐矿的刷新时间 —— 铁块/白铁块 24、星银矿石 48、
  水晶块/紫晶块 72、魔晶块 24、电气水晶 48、夜泊石/石珀 72。改类别默认值不会动这些单品。
* 数据来源是 **BetterGI 自己的日志**：每条路线跑完都会打
  `→ 脚本执行结束: "01-霜仙花-彩冰镇左上-3个.json", 耗时: 0分24.5秒`；
  路线名格式 `NN-材料名-地点-N个.json`，脚本组里每条还有 `folderName`（`地方特产\挪德卡莱\便携轴承`），
  所以"哪种材料、什么时候采的"能确定性读出来 —— 包括**你自己手动跑的那些**（自记账本反而会漏）。
* **材料名怎么读**（这段踩过两个坑，都是玩家现场报的）：
  * 顺序是**先路线文件名、再目录**。文件名是 `NN-材料名-地点-…` 的约定，最可靠；
    目录只作兜底（有些路线就叫 `1灵濛山.json`）。
  * 目录必须**由深到浅跳过"分组/变体标签"**：实测玩家的组里有
    `地方特产\蒙德\慕风蘑菇\无草神@Tool_tingsu`、`地方特产\须弥\沙脂蛹\1. 高成功率路线`、
    `地方特产\璃月\清水玉\清水玉@某人\A组鼋背` —— 老逻辑只看最后一段，于是把材料读成
    "无草神"、"1. 高成功率路线"、"A组鼋背"，词表里也就没有慕风蘑菇/沙脂蛹，
    回答自然是"认不出这种采集物"。同理 `夜泊石地下@烤鱼` 归到 `夜泊石`（同一材料的地下路线）。
  * 路线编号前缀不止纯数字：`09A-清心-…`、`A01-清水玉-…` 也真实存在，
    老正则只认 `\d+-`，这些路线的采集**在日志解析里也被整条漏掉**；
  * 作者写错的材料名归一化到正式名（`蒲公英` → `蒲公英籽`、`珊瑚珍珠` → `珊瑚真珠`，
    以本地百科字典为准）；
  * 日志里只有文件名，名字实在读不出来时回查脚本组（文件名 → 材料索引，按组文件 mtime 缓存）。
* **失败不算**：紧邻上文有 `此追踪脚本未走完！` / `任务执行失败` 的那条不计入（实测被停止快捷键打断的那条就不该算）。
* **只跑一部分不算**：这种材料的路线至少要跑掉 80%（`GATHER_COOLDOWN_MIN_ROUTE_PERCENT`）才算"跑完"。
  这条是给**防闪退隔离带**擦屁股的（见下）：隔离带会给没被点名的材料也打开一条路线，
  实测踩到过"万相石只跑了 1/16 就被记成采过、白等 48 小时"。
* **手动登记不受比例规则约束**：`--manual 霜仙花` 是你明确说"这种材料我刚采过了"，
  直接进冷却（那次日志里当然没有路线记录，不特判的话会被算成"只跑了一部分" → 反而不冷却）。
* **跨自然日**：日志按天切文件，往前扫几天由**最长的那类冷却**决定
  （`scan_days()` = 最长时长 ÷ 24 + 1，默认 72 h → 扫 4 天）。
* **三层拦截**：① 每轮上下文注入【资源冷却】表（按类别分区）→ 模型据此改推别的材料；
  ② 审批屏多一行 `⏳ …还要等约 N 小时`；③ 执行前 `_filter_cooldown()` 按动作带类别
  （`gather` → 特产、`hunt`/`mine`/`cook` → 各自类别）把冷却中的目标摘掉，
  **摘光了就直接跳过启动 BetterGI**（空计划保护）；老名字 `_filter_gather_cooldown()` 仍保留为别名。
  Studio 的 `GET /api/cooldown` 返回同样的分区数据（`sections[]` + 扁平 `materials[]`），
  页面按类别分卡片渲染。

---

## 防闪退隔离带

背景：BetterGI 会为脚本组里**每一条 `Disabled` 的路线写一行日志**。几百条的大组在跑一半按停止快捷键时，
日志会瞬间爆发（玩家机器实测 550~700 行/秒，早期记录里出现过 638/773 行/秒），
随后可能在 WPF 层栈溢出崩溃（`0xc00000fd` / `System.StackOverflowException`，BetterGI 0.64.0 实测）。

所以实现里每连续 **150** 条 `Disabled` 就强制打开一条路线，把"连续禁用"截断。
常见现象：你只点了名要采霜仙花，组里却穿插了一条别的材料的路线 —— 那就是隔离带。

```dotenv
BGI_FORCE_ENABLE_BAND=1                # 0 = 完全不插隔离带（组里只有你点名的路线）
BGI_FORCE_ENABLE_AFTER_DISABLED=150    # 每连续多少条禁用插一条
```

> ⚠️ **老实说**：「连续 180 条会触发 bug」这个具体数字查不到出处（BetterGI 源码/文档里没有；
> 代码里用的是 150，来自原作者的实现；旧文档写的 180 没有依据）。能证实的是"日志爆发"和
> "随后的栈溢出"这两件事。关掉隔离带（`BGI_FORCE_ENABLE_BAND=0`）风险回到原始状态，请自行评估 ——
> 这也是把它做成开关的原因。
>
> **冷却判定会跟着开关走**：开隔离带时按"跑了几条 / 一共几条"判（只跑 1 条隔离带路线不算跑完）；
> 关掉隔离带就是**正常冷却**（跑过任意一条即算采过）。

---

## 「BGI 在不在跑任务」与空计划保护

实测（玩家 QQ 记录）：12:34:40 一条龙就跑完了，12:36:16 再下指令时 Agent 却回
`⛔ 检测到 BetterGI 正在运行、且日志仍在更新` —— 玩家只好重下一遍。

1. **判定不能看"日志最近有没有写"**：BGI 跑完一条龙后窗口还开着，空闲时也会零碎写日志。
   现在 `skills/bgi_controller._bettergi_task_state()` 看当天日志**尾部**最后出现的是哪一类标记：

   | 类别 | 标记（实测原文） |
   | --- | --- |
   | 终态 → 空闲 | `一条龙和配置组任务结束`、`配置组 "X" 执行结束`、`任务被取消`、`主窗体退出`、`游戏已退出` |
   | 启动 → 在跑 | `→ "任务启动！"`、`开始执行地图追踪任务`、`加载完成，共 …` |

   两边都没有（日志刚被清空等）才退回"最近 120 秒有没有写"，**宁可多等**也不打断正在跑的一条龙。
   判定为空闲时会关掉 BGI 冷启动本轮计划（BGI 不支持热启动，重复启动不会触发一条龙）。

2. **真在跑就等它跑完**（`_wait_for_bettergi_idle()`）：每 `BGI_RUNNING_TASK_POLL_SECONDS`（默认 10 秒）
   看一次结束标记，等到了就继续执行本轮；只在开始时提醒一次。最长等
   `BGI_WAIT_RUNNING_TASK_MINUTES`（默认 20 分钟），超时才回"本轮先不执行"。
   一轮执行期间有互斥锁，第二条指令会被明确拒绝（否则两个线程会同时改配置、各启动一次）。

3. **空计划保护**：点名了目标却一条路线都没命中（材料不在脚本组里、或全在 48 小时冷却里）时**跳过启动**。
   实测那次点名的「金蕨」在 `User\AutoPathing` 里一条路线都没有（全目录搜"蕨" 0 命中），
   旧逻辑照样冷启动，日志里留下 `启用任务总数量: 0 / 没有配置,退出执行!`。
   如果这轮本来就没点名任何目标（你自己开好了一条龙只是让 Agent 跑），这条保护不生效。

---

## 原神窗口前台兜底

BetterGI 的模拟输入走**前置 SendInput**（它自己的提示就是"前台 SendInput：游戏需要保持前台"），
而且每秒检查一次前台窗口：

```text
[WRN] BetterGenshinImpact.GameTask.Common.TaskControl
当前获取焦点的窗口为: GI-Agent-Studio，不是原神，暂停
```

所以"跑一半卡死"绝大多数不是崩了，而是在**等原神回到前台**（实测一条龙里出现过 73 次这种暂停，
前台分别是 QQ / Studio / 搜索框）。三层兜底：

| 层 | 做什么 | 怎么开 |
| --- | --- | --- |
| ① BetterGI 自带 | 打开「失去焦点时自动切回原神」（`otherConfig.restoreFocusOnLostEnabled`，默认关） | BetterGI 设置里勾；`python main.py doctor` 会提醒 |
| ② Agent 启动时 | 触发一条龙后自动把原神切前台一次 | 默认开（`BGI_FOCUS_ON_LAUNCH=1`） |
| ③ Agent 运行中 | 巡检发现前台不是原神 → 提示你；可选反复切回 | 提示默认开；自动切回默认关（`BGI_FOCUS_GUARD=1`，最多抢 3 次） |

自查：`python -m skills.window_focus`（只看）/ `--focus`（真抢一次焦点）；
`doctor` 也会报告截图方式建议（被悬浮窗遮挡时把 `BitBlt` 换成 `WindowsGraphicsCapture`）
与 BetterGI 的自动重启开关。
