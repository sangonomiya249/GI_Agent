
# 🤖 GI_Agent - 原神自动化的最后一步

> 本项目构建了一个基于 BetterGI 配置文件动态修改的原神自动化养成智能体。它可以基于 LLM 决策，自动灵活地执行包括每日邮件、每日任务、好感领取、消耗体力、每日狗粮收集、挖矿等一系列基于 BetterGI 脚本的任务。

## ✨ 核心能力

目前 Agent 已经兼容的体力消耗任务包括：
- ✅ **自动地脉花**
- ✅ **智能秘境**：自动按所选角色 / 当前日期 / 当前角色武器，精准推导并刷取对应的武器/天赋素材。
- ✅ **自动刷取角色养成 Boss**：除机制 Boss 外（如无相系列），支持常规 Boss 讨伐（*注：可能需要力大砖飞的队伍*）。
- ✅ **自动刷取圣遗物**：支持玩家黑话（如“冰套”、“绝缘套”），内置字典自动映射副本。
- ⏳ 自动幽境危战斗（*待支持，但可手动开启*）。

**🧠 智能感知：**
只需将对应的角色放到“角色展柜”中，Agent 将会自动读取展柜中的角色，并根据当天的日期（星期几）给出最合理的体力规划建议。
*(注：Agent 目前只能读取角色展柜的数据，无法获知今天是否为“紊乱爆发期”或“材料全开日”，如遇特殊时期，请直接通过对话让 AI 强行修改计划。)*

---

## 🖥️ 图形界面

### GI Agent Studio（新版，推荐）

双击 **`GI-Agent-Studio.exe`** —— 一个**真正的桌面程序**（约 0.8MB 单文件）：
WinForms 窗口里嵌 WebView2 控件显示界面，任务栏里是它自己，没有地址栏 / 标签页 / 浏览器菜单；
关掉窗口时后端 Python 一起退出。界面与后端仍是"HTML/CSS/JS + 本地 Flask（只监听 127.0.0.1）"，
Flask 是项目自带依赖，**不需要额外安装任何东西**。

* **概览**：Agent / LLM / BetterGI / 当前一条龙 / 展柜新鲜度 / 各类目路线数 六张卡片，
  右侧直接显示 BetterGI 最近日志里**最值得看的那一条问题**（战斗策略缺失、传送点不可点击…）；
* **运行**：「审批卡」在顶部，一个按钮拍板（`y` 批准 / `t` 仅写配置 / `exit`）+ 分色实时日志 + 对话输入；
* **任务与路线**：每个脚本组的路线数、战斗策略是否真的存在、有没有登记进一条龙；
* **配置**：图形化编辑 `.env`，**只提交改动过的键**，保留注释与键顺序，自动备份；
* **体检**：`python main.py doctor` 的图形版（这个按钮和 CLI 用同一份 `skills/health_check.py`）；
* **使用说明**：一页纸搞定首次配置 —— 上手清单（10 项实时状态 + 每项怎么补，含可选的米游社战绩）、
  「一条龙要添加哪些调度器」表格（组名 / 用途 / 已登记还是缺组）、在 BetterGI 里配一条龙的步骤、
  `.env` 速查（LLM 模型 / 密钥 / 接口地址 / UID / 路径 / 脚本组名 / 路线策略 / JS 设置）、
  PowerShell 三个脚本（`setup_start_bettergi_task.ps1` 一次注册 `StartBetterGI`+`StopBetterGI`+`StopGenshin`
  三个免 UAC 计划任务、`stop_bettergi.ps1` / `stop_genshin.ps1` 按 PID 精确关闭）
  **+ 这三个任务的逐条环境检测**、常用命令与排错速查，右上角还有 **🔧 一键修复**；
* **BetterGI 日志**：直接读 `BetterGI\log\*.log`，行号 + 关键字过滤 + 已知坑高亮。

重新编译 exe（不需要 PyInstaller；会用官方 NuGet 包里的 WebView2 程序集并嵌进 exe）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_studio_exe.ps1
```

架构、编译细节与排错（`logs\studio-host.log` / `logs\studio-access.log`）见 [`docs/STUDIO.md`](docs/STUDIO.md)。
想用浏览器模式（手机/平板也能看）就 `python app_web.py --no-window`；
`GI-Agent-Studio.exe --browser` 则强制走浏览器应用窗口（排查时用）。

### GI Agent 控制台（旧版 tkinter，保留为备用）

双击 **`启动GI-Agent控制台.bat`**（或 `python main.py gui`）。零依赖、纯 stdlib，
功能对齐：启动/停止、实时日志、审批快捷键、`.env` 表单编辑、维护工具、帮助。
打包脚本 `scripts/build_exe.ps1`（需要联网装一次 PyInstaller）。
设计与实现见 [`docs/GUI.md`](docs/GUI.md)。

> 两版都只是"壳"：`main.py` 依旧是唯一执行体，CLI 单独跑（`python main.py`）永远可用。


---

## 🧭 功能边界与开发状态

### 已有的稳定能力

- 基于 LLM 的自然语言任务规划，以及 CLI、飞书两种交互入口；
- 读取 Enka 展柜数据，并结合角色养成材料、Boss、圣遗物等本地字典生成建议；
- **可选**接入米游社个人战绩：展柜只有 8 个角色，填一份 cookie（`MYS_COOKIE`）就能读到你账号下
  **全部角色**的等级/天赋等级/武器，问起展柜外的角色时不用再回"展柜里没有"（见 [米游社 cookie 指南](docs/MYS_COOKIE.md)）；
- 对已适配的固定 BetterGI 配置执行地脉、秘境、Boss、地图素材等任务配置修改；
- 通过 Windows 任务计划程序触发 BetterGI 一条龙任务；
- 对话记忆、任务审批，以及圣遗物俗称到副本名称的映射。

### 本次近期更新（已实现）

- 将 UID、BetterGI 路径、配置文件名等个人配置统一迁移至 `.env`，并提供 `.env.example`；
- BetterGI JSON 配置写入改为事务：写入前备份、生成 Diff、原子替换、回读校验；
- 写入或回读失败时，自动恢复本次事务已经写入的全部配置文件；
- 提供手动恢复能力：可列出历史事务、查看变更，并恢复 BetterGI 本地配置；
- 在 CLI `main.py` 中可输入 `rollback` 或 `rollback <事务ID>` 调用恢复流程；
- 为配置事务、自动回滚、手动恢复和 CLI 命令补充自动化测试与使用文档；
- 采集物 48 小时冷却（读 BetterGI 日志，手动跑过的也算）+ 防闪退隔离带开关（见下方各节）；
- 下发计划时若 BetterGI 正在跑任务：**等它跑完自动继续**，不再让你重新下一次指令；
  点名的目标一条路线都没命中时**跳过启动**，不白开一次 BetterGI（见 §4.6）。

### 尚未实现的规划能力

- 面向整个 BetterGI 脚本库的自动同步、README 解析、结构化检索与版本兼容判断；
- 根据自然语言从任意脚本中选择能力、自动理解参数并生成适配器；
- 通用脚本注册表、JSON Schema 校验、脚本依赖/冲突检测；
- Web 管理界面、多用户权限、BetterGI 运行状态观测与完整审计平台。

> 当前 Agent 只能可靠地处理已在代码中适配的任务类型。LLM 不会直接修改任意 BetterGI 脚本；“脚本库智能编排”属于后续架构演进目标。

---

## ⏳ 采集物冷却（地区特产 48 小时刷新）

地区特产（霜仙花 / 清水玉 / 月莲 / 星螺这类）采完 **48 小时**才刷新。所以"去采集霜仙花"不能无脑跑 ——
Agent 会在**排期前**先查这个材料刷了没：

```text
你：去采集霜仙花
Agent：⏳ 霜仙花：09-13 12:34 采过（40 分钟前），还没刷新，还要等约 1 天 23 小时
       （想强跑就说「强制采集」）
```

**数据从哪来**：BetterGI 自己的日志。地图追踪每跑完一条路线都会打

```text
→ 脚本执行结束: "01-霜仙花-彩冰镇左上-3个.json", 耗时: 0分24.5秒
```

路线名是固定格式 `NN-材料名-地点-N个.json`，脚本组里每条还有 `folderName`（`地方特产\挪德卡莱\便携轴承`），
所以"哪种材料、什么时候采的"都能确定性地读出来 —— 而且**玩家自己在 BetterGI 里手动跑过的也算**
（自己记一份账本反而会漏掉手动跑的）。两个细节：

* **失败不算**：紧邻上文有 `此追踪脚本未正常走完！` / `任务执行失败` 的那条不计入冷却
  （实测：`04-便携轴承-蓝珀湖左上1-9个.json` 被停止快捷键打断，就不能算采过）；
* **只跑了一部分不算**：这种材料的路线至少要跑掉 **80%**（`GATHER_COOLDOWN_MIN_ROUTE_PERCENT`）
  才算"采完"。这条是给**防闪退隔离带**擦屁股的 —— 隔离带会给没被点名的材料也打开一条路线
  （见下面那节），玩家实测就踩到了：万相石只跑了 1/16 条、晶化骨髓 1/6、星螺 1/5，却整种材料被记成
  "采过"、白等 48 小时。现在这种会明确说「只采了 1/6 条路线，**不算采完**，可以继续采」；
* **手动登记不受这条比例规则约束**：`--manual 霜仙花` 是你明确说"这种材料我刚采过了"，
  所以直接进冷却（日志里当然没有那几条路线记录，不特判的话会被算成"只跑了一部分"→ 反而不冷却）；
* **跨自然日**：BetterGI 日志按天切文件，48 小时会横跨 3 天，所以往前扫 3 个文件。

**三层拦截**（都在审批前后，玩家看得见）：

| 层 | 位置 | 行为 |
| --- | --- | --- |
| 上下文 | 每轮注入【采集物冷却】表（只列还没刷新的） | 模型据此认出"霜仙花还没刷新"，或改推一个已刷新的材料 |
| 审批屏 | 计划下面多一行 `⏳ 霜仙花：…还没刷新，还要等约 N 小时` | 你点 y 之前就知道这趟其实白跑 |
| 执行前 | `skills/bgi_controller._filter_gather_cooldown()` 把冷却中的目标摘掉 | 摘光了就不排这个组；**而且这次一个可执行任务都没有时会直接跳过启动 BetterGI**（空计划保护，见 §4.6）；**强制采集**时照跑并在计划里写明原因 |

**玩家说的是角色名怎么办**（"去采集奥黛塔的普通突破材料"）：代码会先把它解析成该角色的**采集物材料**再查冷却 ——
展柜数据优先（新角色只有那儿有），再查本地百科字典（覆盖 300+ 角色，例如 蓝砚→清水玉、钟离→石珀）；
**两边都查不到**（例如本地字典没有的新角色）就老实说"认不出这个角色，请直接说材料名"，绝不猜一个材料去排。
提示词里也写明了：`gather` 的 target 一律填材料名。

**玩家自己在游戏里采过**（日志里没有的）：登记一笔即可

```bash
python -m skills.gather_cooldown                  # 看现在哪些还在冷却
python -m skills.gather_cooldown --check 霜仙花    # 查一种（也可以给角色名）
python -m skills.gather_cooldown --manual 霜仙花   # 记为"刚采过"（游戏里手动采的）
python -m skills.gather_cooldown --clear 霜仙花    # 清掉这条记录
python -m skills.gather_cooldown --materials      # 列出脚本组里能采的材料名
```

冷却时长可在 Studio「配置」页改（`GATHER_COOLDOWN_HOURS`，默认 48）；
`GATHER_COOLDOWN_ALLOW_FORCE=0` 可以彻底关掉"强制采集"这个后门。

---

## 🛑 让 Agent 帮你关闭原神

直接说 **"帮我关闭原神"**（或"关掉原神 / 退出游戏 / 把游戏关了"）就行。两种说法都能识别：

* **关键词快通道**（`skills/game_control.py`，不用等大模型）：明确提到原神/游戏 + 关闭动词；
* **大模型通道**：模糊说法（"我不想玩了"）由模型输出 `system_task` 计划。

两条路都**先出计划、等你回 `y`** 再动手（关错了要重开游戏 + 重新登录，所以不能一句话就闷头干）：

```text
🛑 [系统操作] 请确认是否执行？
🛑 系统操作：关闭原神
· 正在运行：YuanShen.exe 13872
· 关闭方式：先正常关闭（等 10 秒），没退再强制结束　—— 原神进度在服务器上，强杀不会丢档
· 顺带关闭 BetterGI：检测到它正在跑任务（日志里最后一次是「任务启动」），留着它会在没有游戏的情况下报错/空转
· 关掉之后要重新启动游戏并登录才能继续跑任务

👉 y = 执行　其它内容 = 放弃
```

关闭策略与安全边界：

| | |
| --- | --- |
| 先礼后兵 | `taskkill /PID`（= 点窗口右上角的叉）→ 等 `GAME_CLOSE_GRACE_SECONDS`（默认 10 秒）→ 还没退才 `taskkill /F` |
| 只按白名单 | `YuanShen.exe` / `GenshinImpact.exe` / BetterGI 配置里那个启动程序名，**不按窗口标题通配、不按父子关系杀** |
| 连 BGI 一起关 | 说了"和 BGI 一起关"就一起关；没说的话——只有检测到它**正在跑任务**才捎带（并在审批屏里写明原因）。"在不在跑"看日志里的终态/启动标记，不看"最近有没有写"（跑完了窗口还开着也会零碎写日志，实测误报过） |
| **权限不够也能关** | 原神常常是**以管理员权限**启动的（被管理员的 BetterGI / 启动器拉起），普通终端 `taskkill` 只会得到「拒绝访问」。这时自动改走**同等权限的计划任务 `StopGenshin`**（和 `StopBetterGI` 一个套路，免 UAC）；任务不存在时会明确告诉你跑哪条命令去注册 |
| 结果如实报 | 关掉了、强杀了、没关掉、**还是判不出来**，都会照原话说（判不出来时绝不说"已关闭"） |
| 不会被误触发 | "别关原神"（否定）、"原神关了吗"（疑问）、"关掉游戏声音/画质/全屏"（游戏内设置）都不会关 |

> 遇到 `无法终止 PID 为 xxx 的进程。原因: 拒绝访问。` 就是这个原因（实测：当前会话完整性级别是
> `Medium`，而原神由管理员的 BetterGI 拉起 → 更高权限）。**解法**：以管理员身份跑一次注册脚本
> （它会注册 `StartBetterGI` / `StopBetterGI` / **`StopGenshin`** 三个任务），之后关原神就免 UAC 了。
> ⚠️ **要用完整路径**：管理员 PowerShell 默认在 `C:\WINDOWS\system32`，相对路径会报「实际参数…不存在」：
>
> ```powershell
> # 把 <仓库路径> 换成你放项目的实际目录（在资源管理器里 Shift+右键 →「复制文件地址」）
> powershell -ExecutionPolicy Bypass -File "<仓库路径>\scripts\setup_start_bettergi_task.ps1"
> ```
>
> 环境检测：`python main.py doctor`（或 Studio 的「使用说明」页）会逐条报告这三个任务在不在，
> 缺了直接给出上面那条完整命令；`python -m skills.task_scheduler` 可以单独查。
> 备选：直接把 QQ 机器人/Studio 以管理员身份启动（那就不用计划任务了）。

命令行也能用（默认只体检，`--close` 才真关）：

```bash
python -m skills.game_control            # 看计划：在不在跑、会怎么关
python -m skills.game_control --close    # 真关原神
python -m skills.game_control --close --with-bgi   # 连 BetterGI 一起关
```

> **踩过的坑（2026-09-12 实测）**：记忆层 `load_chat_store()` 校验待审批计划时，以前硬要求里面有
> `bgi_cmd` —— 而"关闭原神"的计划是 `{"system_task": …}`，于是它**写进文件、读出来就没了**。
> 玩家回 `y` 时看不到待审批计划，消息被当成普通聊天喂给大模型，模型只好又规划了一遍打 Boss；
> QQ 上就表现为"点了 y，冒出来一个和关闭无关的 Boss 计划"（而且游戏一直没关）。
> 现在两种形态都认（`brain/memory_manager.py`），并且**没有待确认计划时的 `y`/`t`/`确认`
> 会直接回一句提示，不再去打扰大模型**（那正是那个凭空出现的 Boss 计划的来源）。

---

## 📦 体积与瘦身（为什么"原生 exe"不等于"体积小"）

`GI-Agent-Studio.exe` 只有 **2 MB 左右** —— 因为它是个 **WinForms + WebView2 的壳**：界面用系统里的
WebView2 渲染（不打包浏览器内核），真干活的后端是 Python（exe 负责把它拉起来）。

所以你在磁盘上看到的"体积"，绝大部分不是这个程序本身：

| 部分 | 典型大小 | 说明 |
| --- | --- | --- |
| 仓库里真正被提交的东西 | **约 16 MB** | 代码 + `assets/` + `memory/` 字典（8 MB）+ `GI-Agent-Studio.exe`（2 MB） |
| `venv/`（Python 依赖） | 约 190 MB | **不进仓库**（`.gitignore` 已忽略），clone 下来按 `requirements.txt` 自己装；其中约 139 MB 是两个可选项 |
| `.studio-profile/`（浏览器档案） | 十几 MB～几百 MB | WebView2/Edge 的缓存与组件，**随手可删** |
| 系统 WebView2 运行时 | **约 1 GB** | `C:\Program Files (x86)\Microsoft\EdgeWebView`，**不是我们的**：Windows/Edge 装的，所有用 WebView2 的程序共用 |
| BetterGI 本体（对照） | 约 1040 MB | Assets 473 MB + Repos 130 MB + exe 98 MB + 各种原生 DLL ~190 MB + WebView2Data 52 MB |

**可选的依赖（只用 QQ / 终端的话可以不装）**：`requirements.txt` 里已经把这两个写成「可选」段，
默认装上去也行、注释掉也行；已经装了想省空间就卸：

```powershell
venv\Scripts\pip uninstall playwright    # 约 101 MB：只有"重建本地字典"（爬米游社百科/yatta）才需要
venv\Scripts\pip uninstall lark_oapi     # 约 37 MB：只有用飞书通道才需要（代码已改成按需 import，
                                         #   没装也能跑，只是飞书发不出消息并给出提示）
```

想先看看自己机器上的实际占用：

```powershell
python scripts\disk_report.py            # 只读，不删任何东西
```

**浏览器档案目录 `.studio-profile/`**：Studio 窗口的 Chromium/WebView2 用户数据目录
（缓存、组件、模型，还有 `Default\Network\Cookies` 与 `Login Data` 这类浏览器本地存储 ——
实测这个目录里**确实有** Cookies 文件，所以它也上了 `.gitignore`，**不要提交**）。
它的体积会随 Edge 更新涨落，关掉 Studio 后**整个删掉**即可，下次自动重建
（只是首屏慢一点、主题回到默认）。它同时被两条宿主路径使用：原生 exe 用 `.studio-profile\wv2`（WebView2），
`app_web.py` 兜底路径把整个 `.studio-profile` 当 Edge 的 `--user-data-dir`。

> 💡 仓库在 OneDrive 里的话，建议把它挪出同步范围（junction，不需要管理员权限）：
> ```cmd
> rmdir /S /Q ".studio-profile"
> mkdir "%LOCALAPPDATA%\GI_Agent"
> mklink /J ".studio-profile" "%LOCALAPPDATA%\GI_Agent\studio-profile"
> ```

---

## 🪟 原神窗口不在前台 → BGI "卡死"的兜底

BetterGI 的模拟输入走**前置 SendInput**（它自己的提示就是「前台 SendInput：游戏需要保持前台」），
而且**每秒检查一次前台窗口**：

```text
[WRN] BetterGenshinImpact.GameTask.Common.TaskControl
当前获取焦点的窗口为: GI-Agent-Studio，不是原神，暂停
```

所以"跑一半卡死"绝大多数不是崩了，而是**在等原神回到前台**。玩家实测（2026-09-12）一条龙里
出现 **73 次**这种暂停，前台窗口分别是：`QQ` 37 次、`GI-Agent-Studio` 22 次、`SearchHost` 12 次。

兜底分三层，从"最该先做"到"最后手段"：

| 层 | 做什么 | 怎么用 |
| --- | --- | --- |
| ① BetterGI 自带 | 打开**「失去焦点时自动切回原神」**（`otherConfig.restoreFocusOnLostEnabled`，默认是关的）：BGI 自己会周期性尝试切回，日志里能看到 `尝试恢复窗口` → `已自动切回游戏前台`（失败是 `多次尝试未恢复`） | BetterGI 设置里勾选；`python main.py doctor` 会提醒你还没开 |
| ② Agent 启动时 | 触发一条龙后**自动把原神切到前台一次**（点 y 就是"开始跑图"，这一次符合你的意图） | 默认开启：`BGI_FOCUS_ON_LAUNCH=1`；日志：`🪟 已把原神切到前台。` |
| ③ Agent 运行中 | 巡检发现前台不是原神 → **提示你**（`⏸️ 原神不在前台，BetterGI 正在暂停`）；可选地反复切回 | 提示默认开；自动切回默认关，想开就 `BGI_FOCUS_GUARD=1`（最多抢 3 次，抢不到就罢手） |

顺带两个和它相关的 BGI 设置（`python main.py doctor` 也会报告）：

* **截图方式**：默认 `BitBlt`；被悬浮窗遮挡、Win11「窗口化游戏优化」等情况下换成
  `WindowsGraphicsCapture` 更稳（见 [BGI FAQ](https://github.com/huiyadanli/bettergi-docs/blob/main/src/faq.md)）；
* **自动重启**（`otherConfig.autoRestartConfig.enabled`，默认关）：任务失败/掉线时自动拉起。

自检与手动测试：

```bash
python -m skills.window_focus           # 只看：游戏窗口句柄 / 当前前台是不是原神
python -m skills.window_focus --focus   # 真的把原神切到前台试一次（会抢焦点，故意要手动加参数）
python main.py doctor                   # 里面对上面这些设置逐条给结论
```

---

## ⚠️ 重要前置配置 (BetterGI 准备工作)

Agent 并不是帮你配置BetterGI的，而是帮你修改BetterGI脚本的，如果没有脚本自然没有办法执行！！！因此在正式让 Agent 接手前，请**务必**保证完成以下配置

**何为完成配置**：该任务可以通过 BetterGI 手动开启并正确执行，且执行效果满足你的期望（比如正确切换到了你的指定队伍、正确执行地图追踪、正确执行你选择的或者你自己编写的战斗策略等等，如果这里面没有经过测试，后续问题排查将**及复杂**）


### 0. 修改 BetterGI 启动设置
请在 BetterGI 的启动页面上，打开`同时启动原神`，然后打开下拉页面，勾选`自动进入游戏`

### 1. 订阅必备脚本
请在 BetterGI 的脚本仓库中搜索并订阅以下脚本：
* `AAA-Artifacts-Bulk-Supply` (AAA狗粮批发)
* `批量讨伐角色养成材料BOSS` 
* `地方特产`
* `万能战斗策略（萌新推荐）`

### 2. 调度器与一条龙设置
将以下名称的脚本先添加到调度器中测试，测试通过后添加到 BetterGI 的“一条龙”任务中，并确保**名称完全一致**，遇到问题请先阅读各自脚本的 README 文件，你可能需要在调度器的设置中配置不同的战斗队伍和地图行走队伍并匹配对应的策略：
* **地图素材**：请全选`地方特产`下的所有项（不用在意路线重叠或其他问题），在调度器中命名为“地图素材”并加入一条龙。
* **矿物 / 食材与炼金**（可选）：同敌人与魔物那套 —— 在调度器里把 `repo/pathing/矿物`、`repo/pathing/食材与炼金` 下的目录加进来（建议按材料分开建小组，例如 `石珀`、`虹滴晶`），加入一条龙即可；也可以建一个总组（`矿物.json`、`食材与炼金.json`），Agent 会自动精简。
* **敌人与魔物**（刷怪掉材料，可选）：在调度器里把 `repo/pathing/敌人与魔物` 下的敌人目录加进来并加入一条龙即可。**不用手工拆组**：
  * 组小的时候（例如你自己按敌人分的 `蕈兽` 14 条、`骗骗花` 86 条、`刀谭` 9 条）Agent 直接在里面开关路线，**小组优先**。
  * 组很大的时候（例如把整个 `敌人与魔物` 1000+ 条塞成一个组）Agent 会**自动把组精简成"只有本次要打的路线"**，完整清单归档到 `User/ScriptGroup/.gi_agent_archive/<组名>.json`；下次换目标会自动从归档里重新挑，你在调度器里看到的就是一份精简后的组。
  * 为什么必须精简：BetterGI 会为每条**被禁用**的脚本写一行日志；组里躺着几百条 Disabled 时，跑一半按停止快捷键会让日志瞬间爆发（实测 **638/773 行/秒**），随后 **BetterGI 在 WPF 层栈溢出崩溃**（`0xc00000fd` / `System.StackOverflowException`，0.64.0 实测）。精简后组里全是 Enabled，既不产生禁用日志也不需要防闪退隔离带。
  * 策略可改：`.env` 的 `BGI_ROUTE_GROUP_POLICY`（`shrink` 默认 / `refuse` 拒绝 / `allow` 照旧）与 `BGI_MAX_ROUTE_GROUP_SIZE`（默认 300）。
  * 组名可改：`.env` 里的 `BGI_ENEMY_CONFIG_NAME`。
  * 已知的另外两个 BetterGI 侧问题（不是 Agent 造成的）：个别传送锚点会报 `目标传送点位于不可点击区域，传送失败`（重试 3 次后该路线失败）；跑大组时按停止快捷键 `Up` 容易触发上面的崩溃。
  * **战斗策略名校验**：Agent 写路线组时会检查该组 `pathingConfig.autoFightConfig.strategyName` 在 `User\AutoFight\` 下是否有对应 `.txt`（BetterGI 按**平铺**路径解析：`User\AutoFight\<策略名>.txt`，放在子目录里的策略必须写成 `群友分享\四神队(进阶版)`）。缺失时会告警并指出「仓库里有、但没导入」以及可用策略清单 —— 否则跑图走到怪点会直接抛 `战斗策略文件不存在` 并中断整条路线，表现很像"战斗打不起来/识别不到战斗结束"。
* **锄大地**（`锄地专区`，可选）：`repo/pathing/锄地专区` 下有三套路线 —— `小怪2000@mno`（按地区分目录，含 `0_0_飞萤` 这类小怪密集点）、`精英400@汐`（`0-传奇` / `1-精英` / `2-收尾` / `3-低效`）、`挪德卡莱锄地小怪`。
  * **不用手工建组、也不用手工登记**：调度器里没有「锄大地」组时，Agent 会自动扫 `User\AutoPathing\锄地专区` 生成 `User\ScriptGroup\锄大地.json`；启用时如果它还没登记进一条龙，Agent 会自动补一条 `TaskDefinitions` 项（等价于在界面点一次"添加"，走备份事务，可 `rollback`）。
  * 默认**跳过** `低效路线(不跑）`、`稻妻未修正部分（不跑）`、`精英400@汐\3-低效`（417 → 301 条）；说“连低效一起跑”时才会带上全部 417 条。
  * ⚠️ **锄大地 ≠ 敌人与魔物**（两边路线名会重名，写错就是跑错整套图）：
    * 点名**具体魔物/材料**（飞萤、骗骗花、蕈兽、刀镡…）→ `hunt`，只打那个魔物的固定怪点；
    * 说**范围性清怪**（锄大地 / 锄地 / 清怪 / 扫图 / 刷精英 / 打传奇 / 某地区的怪清一遍）→ `hoe`，按地区扫图；
    * 想跑 **mno 的锄地一条龙 JS 脚本**（`AutoHoeingOneDragon`，一站式锄地+只捡狗粮）→ `script`。
    * Agent 用 **folderName 的目录前缀**做隔离（`敌人与魔物\…` / `锄地专区\…`）：实测「飞萤」在 `hunt` 里命中 19 条（`敌人与魔物\飞萤`）、在 `hoe` 里命中 13 条（`锄地专区\小怪2000@mno\0_0_飞萤`），互不串味；「锄大地」在 `hunt` 里命中 0 条（跑错就会被送去只打一种怪）。
  * 组名可改：`.env` 里的 `BGI_HOE_CONFIG_NAME`；自动建组开关 `BGI_AUTO_CREATE_ROUTE_GROUP`（默认 1），扫描目录 `BGI_AUTO_PATHING_DIR`（默认 `User\AutoPathing`）。
  * 全图锄地一次会跑 300 条左右路线、耗时以小时计；只想清一块地区就说“锄大地 璃月”（实测 56 条）/“锄大地 挪德卡莱”（24 条）。
* **突破材料**：将`批量讨伐角色养成材料BOSS`脚本在调度器中配置为此名称，然后加入一条龙。
* **狗粮**：将`AAA狗粮批发`脚本在调度器中配置为此名称，加入一条龙。

### 3. 独立任务可用性测试(极其重要！)
在 BetterGI 中单独测试以下任务是否能正常运行，然后全部将其设为开启状态：
* **自动地脉花**
* **自动秘境**
* **其他一条龙中的杂项任务**：领取邮件、合成树脂、领取每日奖励、领取尘歌壶奖励

### 💡 避坑指南与注意事项
* **队伍配置**：不同任务可以单独配置不同的队伍。
* **Boss 讨伐高危预警**：强烈建议使用拥有挂机输出且生存能力极强的队伍（如：少机、少莉、芙茜爱、四神等）。否则成功率难以保障（作者使用 6芙的莉希机芙，仍会被深罪浸礼者拍死）。如讨伐经常失败，请联系对应脚本作者，或自行修改对应脚本json文件中的定位（比如三角铁）
* **狗粮队伍**：无特别要求，移速最快的跑图大队即可。
* **采集特化**：大世界材料采集**强烈推荐携带纳西妲和芙宁娜**，部分脚本路径依赖这两个角色以提升采集效率。
* **玄学命中率**：阴间材料（如沙脂蛹、慕风蘑菇等）命中率较低属于正常现象，请谅解或向脚本作者反馈。
* **防 Bug 隔离带**：如果你发现 Agent 生成的采集路线中穿插了你不想要的材料，那是**防闪退隔离带** ——
  BetterGI 会为脚本组里每一条 `Disabled` 的路线写一行日志，大组（几百条）在跑一半按停止时日志会瞬间爆发
  （玩家机器实测 **550~700 行/秒**，README 上面第 318 行记的 638/773 行/秒是同一现象），
  BGI 0.64.0 上我们确实遇到过随之而来的 WPF 层栈溢出崩溃（`0xc00000fd`）。
  所以代码每连续 **150** 条 `Disabled` 就强制打开一条路线，把"连续禁用"截断
  （实测今天日志里禁用行正好被切成 150 行一段，说明它在起作用）。

  **这个机制现在是配置项，可以自己开关和调阈值**（Studio「配置」页 → 玩家与记忆）：

  ```dotenv
  BGI_FORCE_ENABLE_BAND=1          # 0 = 完全不插隔离带（组里只有你点名的路线）
  BGI_FORCE_ENABLE_AFTER_DISABLED=150   # 每连续多少条禁用插一条（旧文档写的 180 无出处）
  ```

  > ⚠️ 老实说：**"连续 180 条会触发 bug"这个具体数字我们查不到出处**（BGI 源码/文档里没有，
  > 代码里用的阈值是 150，来自原作者实现）；能证实的是"日志爆发"和"随后的栈溢出"这两件事。
  > 关掉隔离带（`BGI_FORCE_ENABLE_BAND=0`）后风险回到原始状态，请自行评估 —— 这也是做成开关的原因。
  >
  > **冷却判定会跟着开关走**：开着隔离带时按"跑了几条 / 一共几条"判断（只跑 1 条隔离带路线不算采完，
  > 门槛 `GATHER_COOLDOWN_MIN_ROUTE_PERCENT`，默认 80%）；关掉隔离带就是**正常冷却**（跑过任意一条即算采过）。

---

## 🏗️ 架构概览 (Architecture)

项目采用严格的模块化解耦设计，确保大模型的“脑抽”不会影响外挂的物理执行：

* `brain/`：核心中枢。包含记忆管家 (`memory_manager.py`) 与 LLM 思考与拦截层 (`llm_brain.py`)。
* `skills/`：执行肌肉。包含 BetterGI 控制器 (`bgi_controller.py`)、展柜读取器 (`env_reader.py`)、黑话字典 (`artifact_match.py`)。
* `api/`：外部通信。包含飞书机器人的收发逻辑 (`feishu_api.py`) 与通道分发 (`channel_router.py`)。
* `channels/`：聊天通道。`agent_router.py` 是飞书/QQ 共用的消息路由（快捷指令、审批、去重），
  `qq_bot.py` 是 QQ 官方机器人接口（WebSocket 网关，见 `python main.py qq`）。加新通道只要注册一个
  target 前缀 + 一个发送函数，不用碰 Agent 的规划/审批/事务逻辑。
* `memory/`：数据库。存放上下文状态、虚拟账本以及提瓦特全量字典。
* `prompts/`：思想钢印。存放核心的 `system_rules.md`。

### 目录结构（主目录只放"入口 + 配置 + 文档"）

```text
GI_Agent-main/
├─ main.py              终端对话模式 / 一次性命令（doctor、repair、qq、studio、gui）
├─ qq_main.py           QQ 机器人入口（等价 python main.py qq）
├─ app_web.py           Studio 启动器（本地 Flask + 原生窗口）
├─ feishu_main.py       飞书 Webhook 服务端
├─ gui.py               旧版 tkinter 控制台（备用）
├─ config.py            配置与路径（**所有仓库内路径都从这里拼绝对路径**）
├─ .env / .env.example  配置（.env 不进版本库）
├─ GI-Agent-Studio.exe  原生窗口宿主（双击即用）
├─ 启动*.bat            双击入口：Studio / QQ 机器人 / 旧版控制台
├─ api/ brain/ channels/ skills/ studio/   代码（见上面的模块说明）
├─ prompts/ memory/     提示词与运行期数据（字典、记忆、虚拟账本）
├─ assets/              图标等静态资源
├─ docs/                文档（使用说明、Studio、QQ 机器人、米游社 cookie、配置恢复…）
├─ scripts/             PowerShell / Python 辅助脚本 + 原生宿主源码（scripts/native/）
├─ tests/               单元测试（python -m unittest discover -s tests -t .）
├─ logs/                运行期日志（Studio 后端 / 宿主 / HTTP 访问日志，2 MB 滚动）
└─ （生成物 / 本地才有，都不进仓库）venv/  build/  __pycache__/  .studio-lib/  .studio-profile/  logs/
```

约定：

* **入口脚本和 `config.py` 留在根目录**（导入、launcher、文档都按这个路径写），其余脚本一律进 `scripts/`；
* `logs/` 是运行期产物，`.gitignore` 已忽略；以前的 `studio*.log` 也都迁进来了；
* `memory/` 里的字典是运行期依赖，**不要手工清空**（`boss_drops_dict.json`、`game_dict_*.json` 等）；
* `memory/` 里**你自己**的那几个文件（`chat_context.json` 对话记录、`mys_characters.json` 展柜快照、
  `agent_registered_tasks.json` 任务台账、`gather_cooldown_manual.json` 手记的采集冷却）
  已经在 `.gitignore` 里，**不要提交** —— 它们带你的 UID / 角色 / QQ 会话 id；
* 从别处（计划任务 / 快捷方式）启动也没问题：`config.PROJECT_ROOT` 让所有路径与工作目录无关。

---

## 🚀 快速开始 (Getting Started)

### 1. 环境准备
- 操作系统：Windows 10/11 (或在 WSL 中运行)
- 环境依赖：Python **3.10+**（实测 3.10.11；`websockets 16` / `python-dotenv 1.2` / `requests 2.33`
  都要求 ≥3.10，装到 3.9 上会在装依赖这一步就失败）
- 前置软件：[BetterGI](https://github.com/letieu/BetterGI) (已完成上述前置配置)

### 2. 安装项目
```bash
git clone https://github.com/sangonomiya249/GI_Agent.git

# cd是重要的，因为一些路径会涉及到这个问题，请确保你cd了这个路径
cd GI_Agent

# 建议使用虚拟环境
python -m venv venv
source venv/bin/activate  # Windows 用户使用 venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

> ⚠️ **venv 必须在项目根目录、且名字就叫 `venv`**：`GI-Agent-Studio.exe` 只是「窗口宿主」，
> 真正的后端是它拉起的 `venv\Scripts\pythonw.exe app_web.py`（三个 `启动*.bat` 也一样）。
> 没建好 venv 就双击 exe，会弹窗提示你先 `python -m venv venv` + 装依赖。
> 依赖装不全时也起不来（比如缺 Flask）——先跑一次 `venv\Scripts\python.exe -m unittest discover -s tests -t .`
> 或 `python main.py doctor` 自检。
>
> 另外 `启动GI-Agent-Studio.bat` 会在缺 `.env` 时自动从 `.env.example` 复制一份（但**密钥要你自己填**）。

`requirements.txt` 里只有 **8 个必需依赖**（Flask / openai / httpx / requests / urllib3 / python-dotenv /
websockets / Werkzeug）；另外两个是可选的，用不到可以注释掉：
`lark-oapi`（飞书通道，代码里是延迟导入，不装也能跑，只是飞书发不出消息）、
`playwright`（只在跑 `skills/*_scraper.py` 离线抓字典时才需要，装完还要 `playwright install chromium`）。

**跑测试**（不需要 BetterGI / 原神 / 网络 / `.env`，全都用临时目录和打桩）：

```bash
python -m unittest discover -s tests -t .
```

### 3. 配置环境变量
复制 `.env.example` 文件并重命名为 `.env`：
```bash
cp .env.example .env        # Windows: copy .env.example .env
```
1. 打开 `.env` 文件，配置模型 API 密钥以及 BetterGI 的路径。
2. **极度重要：请在 `.env` 中填写 `DEFAULT_UID` 为你自己的原神 UID！**

*(测试连通性：`python scripts/check_llm_config.py` 测大模型、`python main.py doctor` 做整体自检
（BetterGI 路径 / 计划任务 / 脚本组 / 展柜缓存逐条报告）、`python -m skills.gather_cooldown --materials`
看脚本组里能采哪些材料。)*


### 3.5 ⚠️ 极其重要的步骤：对齐外挂配置文件名

> 当前版本已将个人路径和配置名称迁移到 `.env`。请优先按下方说明填写 `BGI_DIR`、`BGI_ONE_DRAGON_CONFIG_NAME`、`BGI_MAP_CONFIG_NAME`、`BGI_GLOBAL_CONFIG_RELATIVE_PATH` 和 `BGI_BOSS_CONFIG_RELATIVE_PATH`，不要直接修改 `config.py`。下方旧代码片段仅用于解释这些路径的含义。

由于每个玩家在 BetterGI 中设定的任务名称不同，你**必须**确保代码里寻找的配置文件名，与你在 BetterGI 里创建的名字完全一致！

请打开项目根目录下的 `config.py` 文件，重点检查以下几个变量：

```python
# 你的 BetterGI 一条龙配置文件的名字（如果不叫这个，请务必修改！）
BGI_ONE_DRAGON_CONFIG = os.path.join(BGI_DIR, "User", "OneDragon", "默认配置测试.json")

# 你的调度器地图特产脚本名字
BGI_MAP_CONFIG = os.path.join(BGI_DIR, "User", "ScriptGroup", "地图素材.json")

# Boss 讨伐脚本的底层配置（通常无需修改，除非原作者改了路径）
BGI_BOSS_CONFIG = os.path.join(
    BGI_DIR, "User", "JsScript", "批量讨伐角色养成材料BOSS", "assets", "config", "config.json"
)
```
---

### 4. 权限突破：实现免管理员确认启动 (必做)
由于 BetterGI 需要管理员权限运行，直接启动会弹出 Windows UAC 窗口拦截。为了让 Agent 能在后台静默拉起外挂，你需要创建一个“影子任务”。

> **⚡ 一键版（推荐）**：右键点击「Windows PowerShell」→「以管理员身份运行」，然后执行：
> ```powershell
> powershell -ExecutionPolicy Bypass -File .\scripts\setup_start_bettergi_task.ps1 -RunTest
> ```
> 脚本会自动读取 `.env` 里的 `BGI_DIR`（或自动探测安装目录）、注册 `StartBetterGI` 任务并试跑一次验证。
> 想手动配置的，按下面 1–4 步操作即可。

1.  **打开任务计划程序**：按下 `Win + R`，输入 `taskschd.msc` 并回车。
2.  **创建任务**：点击右侧“创建任务”（不是基本任务）。
    -   **名称**：必须填写 `StartBetterGI` (代码中硬编码以此名称触发)。
    -   **通用选项**：勾选 **“使用最高权限运行”**。
3.  **操作设置**：切换到“操作”选项卡，点击“新建”，然后完成如下项的填写。
    -   **操作**：
    -   **程序或脚本**：浏览并选择你的 `BetterGI.exe` 完整路径，例如 `E:\path\to\BetterGI\BetterGI.exe`。
    -   **添加参数**：`--startOneDragon` 这是启动一条龙的指令，会自动打开 BetterGI 并执行一条龙任务
    
    -   **起始于**：只填写 `BetterGI.exe` 所在的**文件夹路径**，例如 `E:\path\to\BetterGI`；不要填写 `BetterGI.exe` 本身，也不要包含双引号。
4.  **条件与设置**：
    -   在“条件”中，取消勾选“只有在计算机使用交流电源时才启动任务”（防止笔记本用户失效）。
    -   在“设置”中，勾选“如果任务运行失败，按以下频率重新启动”。

**原理说明**：Agent 内部会通过命令 `schtasks.exe /run /tn "StartBetterGI"` 来触发这个任务。因为任务已经在系统层级被授权为最高权限，所以启动时**不会弹出任何警告窗口**，从而实现无人值守启动。

**Debug指南**：在执行后你可以通过在终端中运行
```bash
cd /d "C:\Program Files\BetterGI"

".\BetterGI.exe" --startOneDragon
```
如果这种方式能够正确拉起 BetterGI 并执行一条龙任务，那就说明 BetterGI 本体没问题，问题只出在计划任务上——此时请检查 `StartBetterGI` 这个任务名是否拼写正确、是否勾选了“使用最高权限运行”。

你也可以用下面两条命令自查：

```bash
rem 计划任务是否存在（报“系统找不到指定的文件”就是没建成功）
schtasks /query /tn "StartBetterGI"

rem 手动触发一次，看能否拉起
schtasks /run /tn "StartBetterGI"
```

> 注意：任务不存在时，Agent 会自动回退为「直接启动 BetterGI.exe」（可能会弹出 UAC 窗口）。这只是兜底，长期建议还是把上面的一键任务建好。

---

### 4.5 一条龙任务是怎么被启用的（容易踩的坑）

BetterGI 的一条龙配置里，`TaskEnabledList` / `TaskOrder` 用的键是**任务 Id**，
`TaskDefinitions` 才是「Id → 任务名」，而运行阶段是按**任务名**去取
`User\ScriptGroup\<任务名>.json` 的。源码 `OneDragonFlowViewModel.LoadDisplayTaskListFromConfig`：

```csharp
bool isOldFormat = TaskDefinitions == null || TaskDefinitions.Count == 0;
foreach (var key in orderedKeys) {
    if (!TaskEnabledList.TryGetValue(key, out var enabled)) continue;
    if (isOldFormat) { taskItem = new OneDragonTaskItem(key) { IsEnabled = enabled }; }
    else {
        if (!TaskDefinitions.TryGetValue(key, out var name)) continue;   // ★ 没登记 = 直接被跳过
        taskItem = new OneDragonTaskItem(name, key) { IsEnabled = enabled };
    }
}
```

也就是说：**新格式配置里，往 `TaskEnabledList` 直接写「组名: true」是死配置** ——
BetterGI 连这个任务都不认识（实测「骗骗花」就是这样静默失效的：看着像启用了，其实不会跑）。
Agent 现在的处理是：

1. **优先用已经登记过的脚本组**：说「刷点骗骗花」时，如果 `骗骗花.json` 没登记、
   而总组 `敌人与魔物` 登记过，就退到总组把它**精简成那 86 条骗骗花路线**再用
   （不往你的一条龙里塞新任务）；
2. 只有"**确实存在脚本组文件**（`User\ScriptGroup\<名字>.json`）"的任务才会被自动补登记
   （新 UUID → 组名），日志里写明 `🆕「X」之前没登记进一条龙，已自动添加为任务项`；
   **BetterGI 内置任务（自动秘境 / 自动地脉花 / 自动首领讨伐…）一律不代登记** ——
   它们各自读自己的配置字段，替玩家打开一个没配好的内置任务只会添乱；
3. 下一轮如果没再排它，**摘掉上一轮 Agent 自己加的任务项**，避免以后直接从 BetterGI
   跑一条龙时莫名多跑一套（玩家手动添加的组不在此列，绝不乱动）；
4. **Agent 开过的调度器，下一轮没排就关掉**（`🧹 已关闭上一轮由 Agent 开启、本轮没排的调度器「X」`）。
   两类来源：① 三个内置动作（自动秘境 / 自动地脉花 / 批量讨伐角色养成材料BOSS）**无条件**归它管；
   ② `memory/agent_registered_tasks.json` 里 `enabled` 名单记着"上轮开过谁"。
   而且账本是"动态"的：动手前就已经关着的组，本轮就从账本里划掉 ——
   所以你手动打开的组不会被它反复关（玩家开的、Agent 从没碰过的组，它一律不动）；
5. 收尾会清理 `TaskEnabledList` / `TaskOrder` 里没有 `TaskDefinitions` 条目的死键
   （`🧹 已清理 N 条无效的一条龙任务项`）—— 你之前那次「骗骗花」遗留的死键就是这么被清掉的。

> 第 4 条是补一个实测的坑：你的一条龙里本来就登记着《批量讨伐角色养成材料BOSS》，
> Agent 打 Boss 那轮把它打开了 —— 因为它不是"Agent 加的"，第 3 条的名单里没有它，
> 于是**永远关不掉**，之后每次启动一条龙都顺带打一次 Boss（那次跑去打了上个版本的急冻树）。
>
> **现在就想清掉它**（不用等下一轮 Agent）：关掉 BetterGI 后执行
> ```bash
> python main.py repair          # 无法确认 BetterGI 是否在跑时，会要求加 --force
> python main.py repair --force  # 已经手动关掉 BetterGI，强行执行
> ```
> 它会一次性关掉"Agent 开过、之后没再排"的调度器（`🧹 已关闭 Agent 留下的开关：…`），
> 走备份事务，可用 `rollback <事务ID>` 还原。

**突破材料（Boss）用的是哪个任务？** 走《批量讨伐角色养成材料BOSS》这个 **JS 脚本组**
（Agent 会把队伍 / 策略 / 次数写进它的 `assets/config/config.json`），
**不是** BetterGI 内置的「自动首领讨伐」—— 内置那条读的是流程配置里的 `AutoBossName`，
Agent 从来没写过它，打开也只会打印"一条龙配置内未选择需要讨伐的首领，跳过"。

> ⚠️ 早先这里有个 bug：`run_boss` 用的是占位名 `AutoBoss`，在"名字键=死键"的旧语义下无害，
> 但自动登记一上线，就在你的一条龙列表里凭空多出一条**跑不动**的 `AutoBoss` 任务。
> 现在已改成正确的脚本组名，并且**只对真正的脚本组做自动登记**。
> 列表里如果已经躺着一条 `AutoBoss`：**关掉 BetterGI** 后执行
> ```bash
> python main.py repair          # 无法确认 BetterGI 是否在跑时，会要求加 --force
> python main.py repair --force  # 已经手动关掉 BetterGI，强行执行
> ```
> 就会摘掉它（走备份事务，可用 `rollback <事务ID>` 还原）；下次让 Agent 跑任何任务时也会自动清掉。

**为什么讨伐一开始会弹一个要手点"确定/保存并关闭"的窗口？**
那是脚本自带的 **BOSS 配置编辑器**（`assets/html/index.html` 遮罩窗口）：
它的 `settings.json` 里 `showEditorOnStart` 默认 `true`，脚本启动时先弹编辑器、
**必须点"保存并关闭"才会继续讨伐** —— 挂机时就是个卡死的窗口（脚本作者在 README 里也写了
"右键点击脚本-修改JS脚本自定义配置，取消勾选启动时打开配置编辑器以挂机使用"）。

Agent 现在会自动替你关掉它：把 `{"showEditorOnStart": false, "showStatusPanel": false}`
写进 `User\ScriptGroup\<脚本组>.json` 里那个 JS 项目的 `jsScriptSettingsObject`
（BetterGI 会把它当成 JS 里的 `settings` 对象：`ScriptProject.ExecuteAsync` →
`engine.AddHostObject("settings", context)`），并**跟其它改动一起走同一个配置事务**（可回滚）。
想改内容就设 `.env` 的 `BGI_JS_SCRIPT_SETTINGS`（JSON，默认值见 `.env.example`）；
想恢复手动编辑，在 BetterGI 里右键该脚本 →「修改JS脚本自定义配置」→ 勾回 `showEditorOnStart`。

---

### 4.6 下发计划时 BetterGI 还在跑：等它跑完，不让你重新下一次指令

实测（玩家 QQ 记录）：12:34:40 一条龙就跑完了，12:36:16 他又下了条指令（金蕨），Agent 回

```text
⛔ 检测到 BetterGI 正在运行、且日志仍在更新（很可能正在跑任务）
```

—— 他只好把同一句话**重下一遍**。原因有两个，现在都改了：

**① 判定"在不在跑任务"不能看"日志最近有没有写"。** BetterGI 跑完一条龙后窗口还开着，
空闲时也会零碎写日志，于是刚跑完就被当成"正在跑"。现在看**当天日志尾部最后出现的是哪一类标记**
（`skills/bgi_controller._bettergi_task_state()`）：

| 类别 | 标记（实测原文） |
| --- | --- |
| 终态 → 空闲 | `一条龙和配置组任务结束`、`配置组 "X" 执行结束`、`任务被取消`、`主窗体退出`、`游戏已退出` |
| 启动 → 在跑 | `→ "任务启动！"`、`开始执行地图追踪任务`、`加载完成，共 …` |

两边都没有（比如日志刚被清空）才退回"最近 120 秒有没有写"，**宁可多等**也不打断你正在跑的一条龙。
用你的真实日志验过：12:24 开始、12:34:40 结束、12:36 又写了几行 → 判定为**空闲**，
于是它会**关掉 BGI 冷启动**本轮计划（BGI 不支持热启动，重复启动不会触发一条龙）。

**② 真的在跑任务时，等它跑完自动继续**（`_wait_for_bettergi_idle()`）：不再直接拒绝，
而是每隔 `BGI_RUNNING_TASK_POLL_SECONDS`（默认 10 秒）看一次结束标记，等到了就接着执行本轮计划；
只在开始时提醒你一次（不刷屏）。最长等 `BGI_WAIT_RUNNING_TASK_MINUTES`（默认 20 分钟），
超时才回那句"本轮先不执行"。两个值都在 Studio「配置」页 / `.env` 里可改。

**③ 顺带修掉一次白开 BGI：空计划保护。** 那次点名的「金蕨」根本不在他的脚本组里，
旧逻辑照样冷启动 BetterGI —— 日志里留下两次

```text
启用任务总数量: 0
没有配置,退出执行!
```

（12:36:45、12:37:09）白开一次 BGI，还让人以为任务跑了。现在只要**你点了名、但一条路线都没命中**
（材料不在脚本组里、或点名的采集物全在 48 小时冷却里），就会跳过启动并说明原因：

```text
🈳 这次点名的目标在 BetterGI 里没有任何可执行的路线，已跳过启动（不白开一次 BGI）。
   ⚠️ 「金蕨」在地图素材组里没有找到路线（也没读到组文件），本次不采集。
   ↳ 确实想跑的话：先在 BetterGI 里订阅对应路线（例如 地方特产\<地区>\<材料>）并让它进脚本组，再让我排一次。
   ↳ 采集物清单：python -m skills.gather_cooldown --materials
```

> 实测确认过：「金蕨」在你的 `User\AutoPathing` 里**一条路线都没有**（全目录搜"蕨"是 0 命中），
> 所以那次无论怎么排都跑不了 —— 要么先订阅/加路线，要么换成清单里已有的材料。用
> `python -m skills.gather_cooldown --materials` 可以看当前 64 种能采的材料。

> 如果你**没点名任何目标**（自己已经在 BetterGI 里开好了一条龙，只是让 Agent 跑），
> 这条保护不生效 —— 照样启动，保持老行为。

---

### 5. 运行 Agent
**终端极客模式：**
```bash
python main.py
```

> **提示**：如果你在 WSL 环境下运行，请确保你的 `config.py` 中 `BGI_DIR` 路径指向正确（例如 `/mnt/c/Program Files/BetterGI`），Agent 会自动跨越系统边界调用 Windows 命令。

**飞书远程模式：**
```bash
python feishu_main.py
```
*(需配合内网穿透工具及飞书开放平台配置使用，具体请参考飞书官方文档，并在 `.env` 中配置飞书凭证后启用)*

*必须完成第四节中的权限配置，不然无法远程拉起*

**QQ 远程模式（轻量，不需要 LangBot）：**
```bash
python main.py qq          # 或双击 启动QQ机器人.bat / python qq_main.py
python main.py qq --check  # 只校验 AppID/Secret 并取一次 token，不连网关
```
> **也可以在 Studio 里跑**（推荐）：左侧「远程通道」页直接启停，日志内嵌在页面里。
> 点「启动 Agent」时，**已填配置**且开着「随 Agent 自动启动」的通道会被一起唤醒
> （`AUTO_START_QQ_BOT` 默认 1；没填 AppID/Secret 就跳过并写明缺哪一项）。
*(在 https://q.qq.com 建一个机器人，把 `QQ_BOT_APPID` / `QQ_BOT_SECRET` 填进 `.env`；
群聊里 @机器人 或私聊它即可，全套规划 / 审批 / 配置事务逻辑与 CLI、飞书完全一致。
只用项目已有依赖（`httpx` + `websockets`），不引入 QQ 机器人框架。
手机上默认只收**精简审批屏**，底部还挂着官方按钮 **✅ 执行 / 🧪 仅改配置 / 🚫 取消**
（点一下等于回复 `y` / `t` / `取消`，需要 QQ 开放平台开通按钮权限，没开通会自动退回纯文本）；
完整推理留在电脑端日志，想全发设 `QQ_BOT_REPLY_MODE=full`。详见 [QQ 机器人接口](docs/QQ_BOT.md)。)*

---

## 🎮 如何调戏 Agent (Usage)

进入对话模式后，Agent 首先会自动读取你的展柜，并给出今日规划建议。

你可以像对待真人代肝一样和它辩论/下发指令：
* **指定培养**：“去刷蓝砚的天赋”、“去刷地脉”、套`。
  *(⚠️ 警告：对于过于冷门的黑话不支持，比如你“去打芙宁娜的boss材料”、“去刷点摩拉”
* **大世界采集**：如果你想采集展柜角色之外的材料，请输入材料的全名（如 `钩钩果`、`空羽蛾`）。*注意：如果名字输错，对应的跑图路线将不会被激活。*
* **刷怪讨伐**：说敌人名或它的掉落材料都行，例如 `去刷点蕈兽`、`刷点原素花蜜`、`缺刀镡`。
* **整脚本任务**：`去跑狗粮`、`跑一下采集水下` —— 这类不是跑图路线，而是整个 JS 脚本挂在调度器组里跑，走 `free_task` 的 `script`。审批屏会写明**命中了哪个脚本组、跑的是哪个脚本、该组有没有登记进一条龙**（没登记的话写进配置也不会被执行）。
* **挖矿**：`去挖点水晶块`、`挖紫晶块`、`打点星银矿石` —— 走「矿物」脚本组（`.env` 的 `BGI_MINE_CONFIG_NAME`，默认 `矿物.json`）。你已有的 `石珀`、`虹滴晶` 组会被自动认出来直接用。
* **锄大地**：`锄大地`、`锄地`、`清怪`、`刷精英`、`打传奇`、`锄大地 璃月` —— 走「锄大地」脚本组（`BGI_HOE_CONFIG_NAME`，默认 `锄大地.json`，对应 `repo/pathing/锄地专区`）。默认跳过“低效/不跑”子目录，说“连低效一起跑”才带上。
  * ⚠️ **别和「敌人与魔物」混**：具体魔物/材料（`刷点骗骗花`、`打点飞萤`）走 `hunt`；范围清怪（`锄大地`、`清怪`）走 `hoe`。两边路线名会重名（锄地专区里有 `0_0_飞萤` 子目录和“三骗骗花”路线），Agent 按目录前缀严格隔离，实测「飞萤」hunt 19 条 / hoe 13 条，互不串味；「锄大地」在敌人与魔物里命中 0 条。
  * 想跑 **mno 的锄地一条龙 JS 脚本**就说 `跑一下锄地一条龙` —— 那是整脚本任务，走 `script`（需要先在调度器里建好那个脚本组）。
* **食材/炼金材料**：`刷点禽肉`、`采点薄荷`、`打些鱼肉` —— 走「食材与炼金」脚本组（`BGI_COOK_CONFIG_NAME`，默认 `食材与炼金.json`）。
  * 这两类和敌人讨伐走**同一套机制**：小组优先（按材料名找你已经分好的组）→ 找不到才用总组 → 大组自动精简归档 → 命中路线打开、其余关掉（每连续 150 条留一条隔离带）。没有对应组时会给出一条可操作提示（该建哪个组、目标该怎么写、当前有哪些组）。
  * **类目写错会自动改判**：模型把「久雨莲」（在 `食材与炼金` 下）当成特产写成 `gather` 时，系统会检查各类目总组里到底有没有这条路线，再改判到正确类目并在审批屏提示（`🔄 …已改判为…`）。声明正确的目标不受影响。
  * 敌人名走 `敌人与魔物` 路线组；材料名会先用掉落字典反查敌人再匹配路线，不消耗体力。
  * 命中的路线会被打开，同组其它路线会被关掉（每连续 150 条会强制留一条隔离带，防 BetterGI 闪退）。
  * **误判会自动纠正**：模型可能把「异种合成魔兽」这类敌人路线当成 Boss 写成 `run_boss`，系统会改判成 `hunt`（提示 `🔄 …已从 Boss 讨伐改判为敌人讨伐`）。反过来，真首领（如「秘源机兵构型械」，敌人组里也有相近的「秘源机兵」杂兵路线）不会被抢走。
* **武器/天赋秘境**：可以直接说角色名（如 `去打一次蓝砚武器的突破副本`），系统会从展柜里读 TA 当前携带的武器，自动换算成炼武秘境并补上 `domain_index`，不需要你报秘境名。
  * 审批时会顺带提示**该秘境今日是否开放**（例如「凛风奔狼系只在 周二/五/日 掉落，今天是周四」）。这只是提醒 —— 你确认要做的话照样执行。
  * **说"打一次 / 打 N 次"才会限次**：次数会写进 BetterGI 的 `User/config.json → autoDomainConfig`（`specifyResinUse=true` + 对应树脂刷取次数）。**不写次数的话，BetterGI 自动秘境默认是"刷到体力耗尽"** —— 这是它自己的默认行为，不是 Agent 决定的。用哪种树脂由 `.env` 的 `DOMAIN_RESIN_PREFERENCE` 决定（默认 `原粹树脂20`）。
* **圣遗物黑话**：Agent 无法根据角色自动推导圣遗物，你需要明确指定套装。支持常用简称（尽量以“套”结尾），如 `去刷草套`、`去刷猎人套`、`下落说“去刷魔法少女套”，Agent 显然没法帮你打开 HSR 去刷火花的遗器。)*

**闲聊与上下文：**
系统默认保留 `20` 条对话上下文。如果你的 Token 额度很充裕，可以将其调高。你完全可以在里面和它聊原神无关的话题，甚至尝试攻略你的 Agent（大家都是玩 AI 的，这里不限制你的想象力 🐶）。

当你觉得Agent规划的任务符合您的预期之后，就可以发送`y`一键执行拉起BetterGI或者发送`t`仅执行配置修改，然后你手动检查之后再执行，推荐一开始使用`t`检查各配置文件是否成功修改。

---

### 展柜数据的新鲜度

Agent 每次启动都会重新拉取展柜（Enka），并把**抓取时间**一起写进上下文；飞书模式下缓存超过
`ENV_CONTEXT_TTL_MINUTES`（默认 10 分钟，见 `.env.example`）也会自动重拉。

会话中想立刻刷新——比如你刚换过展柜，或者发现 Agent 说「某角色不在展柜 JSON 中」——在 CLI 里输入：

```text
refresh
```

以前这份展柜只在「首次为空」时抓一次就永久缓存（还跨会话写进 `memory/chat_context.json`，且没有时间戳），
会让 Agent 拿着一份过期快照做规划。

#### 展柜里每个角色的 `materials` 有四类

| `type` | 怎么刷 | 关键字段 |
| --- | --- | --- |
| `特产` | 大世界采集（`gather`） | `schedule` = 采集地点 |
| `秘境材料` | 刷秘境（`run_domain`） | `schedule` = 秘境名 + 开放日程 |
| `Boss材料` | 首领讨伐（`run_boss`） | `needed` = 到 81 级还缺多少 |
| `敌人掉落` | **普通怪物掉落**，讨伐大世界魔物（`hunt`） | `schedule` = `讨伐【魔物名】`、`total` = 满突破总需求（≠ 当前缺口） |

`敌人掉落` 这类曾经被整类丢掉，于是模型只能说"展柜里不含普通怪物掉落，得问你"。
数据其实一直在本地字典里（`ascension_total_cost` + 来源行里的「怪物掉落」），现在提取出来了，
例如：雅珂达 → 毁损机轴 ← 巡陆艇；蓝砚 → 骗骗花蜜 ← 骗骗花；芭芭拉 → 导能绘卷 ← 丘丘萨满。

> 统计口径：344 条词条里 307 条能点名魔物（38 种），其余 144 条退回"讨伐大世界魔物"
> ——**宁可说不知道，也不瞎猜一只怪**（百科没写魔物名的就老实写"大世界魔物"）。

---

### 展柜外的角色：米游社个人战绩（可选）

游戏展柜一次只能放 8 个角色，所以"我某个没进展柜的角色还差什么材料"以前是答不了的。
**在 Studio 里就能填**：左边「配置」→ 标签「米游社个人战绩（可选）」→ 粘进 `MYS_COOKIE`（密码框）
→ 点 **「验证 Cookie」**（当场问一次米游社，直接列角色，不用先保存）→ 保存 → 重启 Agent。

```dotenv
# 也可以手改 .env（等价）
MYS_COOKIE=ltuid=123456789; ltoken=xxx; cookie_token=yyy; account_id=123456789
MYS_CACHE_TTL_HOURS=48      # 名单 2 天才拉一次（玩家要求：别每次启动都拉，会触发风控）
```

配好之后**不用再做任何事**：对话里提到展柜外的角色时，Agent 自动去查，并把这一个角色的
等级 / 天赋等级 / 武器 / 材料作为【展柜外角色参考】贴进本轮上下文；**展柜里已有的角色不会重复查**。

- 三个自检命令：`python -m skills.mys_api --check / --refresh / --show 胡桃`（Studio 配置页里也有「验证 Cookie」「拉一次全角色名单」两个按钮）；
- 缓存落在 `memory/mys_characters.json`（只存角色资料，**不存 cookie**）；
- 拿 cookie 的步骤、风控/过期怎么处理、salt 失效怎么办、安全边界 → [米游社 cookie 指南](docs/MYS_COOKIE.md)。

> 风险说在前面：这是米游社 App 自己用的接口（不是官方开放接口），**风控严格**，米游社改版就可能失效；
> cookie 属于账号凭据，只写进 `.env`（已忽略提交），日志里一律只打印掩码。
> 不想给 cookie 的替代方案：把角色放进游戏展柜（8 个一组轮换）——零风险。

---

### 配置恢复

每次 Agent 写入 BetterGI 配置时，都会创建备份、Diff 和事务记录。若配置成功写入但不符合预期，在 CLI 中输入：

```text
rollback
```

按提示选择事务 ID、核对受影响文件，并输入 `RESTORE` 即可恢复 BetterGI 本地配置。也可直接输入 `rollback <事务ID>`。恢复不会撤销已经发生的游戏内操作；执行前建议关闭 BetterGI。详细说明见 [配置备份与手动恢复](docs/CONFIG_RECOVERY.md)。

---

## 🤝 贡献与交流 (Contributing)
目前提瓦特的字典映射仍在不断完善中，如果你发现了大模型无法解析的新“黑话”，或者有更好的逻辑优化建议，欢迎提交 PR 或 Issue！

1. Fork 本仓库
2. 创建你的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交你的更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启一个 Pull Request

---
📜 **免责声明**：本项目仅供学习与 AI 架构研究使用，请合理遵守游戏官方相关协议。一切因此而产生的后果与本项目无关。
