# GI Agent Studio · 原生桌面程序（WinForms + WebView2）

旧的 tkinter 控制台（`gui.py` / `启动GI-Agent控制台.bat`）**继续保留**，随时可用；
Studio 是"更好看 + 更好用"的那一版，默认推荐它。

## 1. 它是什么：真正的 exe，不是浏览器窗口

`GI-Agent-Studio.exe` 是一个 **WinForms 程序**，窗口里嵌 `WebView2` 控件显示界面；
WebView2 的两个托管程序集与原生加载器都**嵌在 exe 里**（运行时自解压到 `.studio-lib/`），
所以对外就是一个单文件、约 0.8 MB 的桌面程序：

* 任务栏里是它自己（图标即 `assets/gi-agent.ico`；它和 `assets/studio.ico` 内容相同，
  由 `scripts/make_icon.py` 程序化生成，不依赖任何图片文件），标题就是界面标题；
* 没有地址栏 / 标签页 / Edge 菜单，右键菜单、F12、缩放、状态栏都已关掉；
* 由它负责拉起后端（`pythonw app_web.py --no-window`）：**关掉窗口，后端 Python 一起退出**；
  界面里点「⏻ 退出 Studio」也会连带关闭窗口。

```
GI-Agent-Studio.exe（原生窗口 · 0.8MB）
   ├── 内嵌 WebView2 控件  ──►  http://127.0.0.1:<随机端口>/   ← Flask（只监听本机）
   └── 子进程 pythonw app_web.py ──► studio/agent_runner.py ──► main.py（CLI，原样保留）
```

为什么不用别的方案：

| 方案 | 结论 |
| --- | --- |
| tkinter / ttk | 观感停留在 90 年代，做不出侧栏、卡片、主题这些效果 |
| PySide6 / PyQt | 要装 ~200MB 依赖、打包 60MB+，而这套环境里 pip 不可靠 |
| Edge `--app=` 窗口（旧方案） | 样子像应用，但进程是 `msedge.exe`、任务栏图标是 Edge、右键还是浏览器菜单 —— 本质仍是浏览器 |
| **WinForms + WebView2 控件** | **选它**：真 exe、真窗口；界面仍用 HTML/CSS/JS（观感随便做）；WebView2 运行时 Win10/11 标配（BetterGI 自己也要） |

## 2. 怎么编译

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_studio_exe.ps1
#  -NoEmbed ：打成 exe + 旁边三个 dll（排查用）
#  -Console ：保留控制台窗口看报错
```

脚本做四件事：
1. `scripts/fetch_webview2.py` 拉官方 NuGet 包（`Microsoft.Web.WebView2`，约 8MB 包，只取 3 个文件）
   → `build/webview2/`（离线机器可以手动放好这三个文件）；
2. `scripts/make_icon.py` 生成 `.ico`（纯 Python 手写 ICO，不需要 Pillow）；
3. Roslyn `csc.exe`（VS 自带）编译 `scripts/native/StudioHost.cs`，目标 .NET Framework 4.7.2；
4. `/resource:` 把 WebView2 的 `Core.dll` / `WinForms.dll` / `WebView2Loader.dll` 嵌进 exe。

## 3. 界面结构

```
┌──────────┬──────────────────────────────────────────────────────────────────┐
│ GI Agent │  概览                       [● 运行中] [☾] [▶ 启动] [■ 停止]        │
│ Studio   ├──────────────────────────────────────────────────────────────────┤
│ 🏠 概览   │  ┌ Agent ─┐ ┌ LLM ─┐ ┌ BetterGI ─┐ ┌ 一条龙 ─┐ ┌ 展柜 ─┐ ┌ 路线 ┐ │
│ ▶️ 运行   │  └────────┘ └──────┘ └───────────┘ └─────────┘ └───────┘ └──────┘ │
│ 🗺️ 任务   │  ┌ 本轮将执行 ─────────────┐ ┌ BetterGI 最近日志 ──────────────┐ │
│ ⚙️ 配置   │  │ ⚔️ 体力目标：无          │ │ ⚠️ 第 1234 行：战斗策略缺失      │ │
│ 🩺 体检   │  │ 🌿 采集：清心  👹 蕈兽   │ │ …（等宽字体、按级别上色）        │ │
│ 📜 日志   │  └──────────────────────────┘ └──────────────────────────────────┘ │
│ ℹ️ 关于   │  ┌ 快捷操作：刷新展柜 / 体检 / 打开目录 / 回滚配置 ─────────────────┐ │
│          │  ⏻ 退出 Studio                                                     │
└──────────┴──────────────────────────────────────────────────────────────────┘
```

* **运行页**：顶部就是"审批卡"（黄框提示 + `✅ 批准执行 (y)` / `🧾 仅写配置 (t)` / `🚪 退出`），
  下面是实时日志（等宽、`❌/⚠️/🤖/👤/🛑` 分色、自动滚动可关）和对话输入框；
* **使用说明页**（第一次配置看这一页就够）：
  ① **上手清单**：10 项实时状态 —— BetterGI 装好没 / LLM 密钥齐不齐 / UID 填了没 / 一条龙配置可读没 /
  关键脚本组在不在 / 战斗策略对不对得上 / 路径追踪同步了没 / 米游社个人战绩配了没（可选步骤，
  没配只标"可选项"，不报红）/ `StartBetterGI` + `StopBetterGI` 计划任务注册了没 /
  运行环境完整没，每项都给出"怎么补"；
  ② **一条龙要添加哪些调度器**表格：组名 / 用途 / 当前状态（已登记 / 有组未登记 / 缺组），
  并注明哪些 Agent 会代建组、代登记；
  ③ 在 BetterGI 里配一条龙的 5 步 + **Agent 到底会改哪些字段**；
  ④ `.env` 速查：LLM 提供商 / 模型 / 密钥 / 接口地址、UID、BGI 路径、各脚本组名、路线策略、JS 设置、树脂、飞书；
  ⑤ **PowerShell 三个脚本** + 计划任务逐条状态表：
  * `scripts/setup_start_bettergi_task.ps1`（管理员跑一次）注册 **三个**「最高权限」计划任务：
    `StartBetterGI`（免 UAC 拉起一条龙）、`StopBetterGI`（以同等权限关闭 BetterGI）、
    **`StopGenshin`**（以同等权限关闭原神，供「帮我关闭原神」用）；
  * `scripts/stop_bettergi.ps1`（由 `StopBetterGI` 调用，按 PID 精确关闭，绝不按进程名杀）；
  * `scripts/stop_genshin.ps1`（由 `StopGenshin` 调用，按 PID + 进程名白名单关闭原神）；
  * 说明书页会**逐条显示**这三个任务的状态（已注册 / 缺失 / 查不出来）与用途，缺了就给修复命令。
  > ⚠️ 注册命令在界面上给的是**完整路径**：管理员 PowerShell 默认在 `C:\WINDOWS\system32`，
  > 相对路径会报「实际参数…不存在」（实测踩过）。手动跑也一样，或者把脚本文件拖进 PowerShell 窗口。
  ⑥ 常用命令 + 排错速查（策略文件不存在 / 焦点不是原神 / 停止时闪退 / "刷点XX"没反应 /
  讨伐弹配置编辑器 / 一条龙多出 AutoBoss / 展柜查不到角色 / 关不掉 BetterGI）。
  右上角还有 **🔧 一键修复**：跑 `main.py repair` 的同一套逻辑（清无效任务项 + 关脚本编辑器），
  结果用弹窗逐行列出；BetterGI 在跑时会拒绝并提示先关掉。
  状态判断只认"能确证"的结论：判不出来就显示 ❔ 而不是瞎猜（`studio/guide.py`）。
* **资源冷却**（`🌿`）：**四类资源**的刷新一览（地区特产 48 h / 矿物 72 h / 食材与炼金 24 h /
  敌人与魔物 12 h）—— 每类一张汇总卡（冷却中 / 可以去 / 手动登记 / 该类时长与依据），
  最下面一张"判定依据"卡说明这些数字是哪来的；表格按类别分区（分区标题行），列为
  材料 / 状态（`冷却中 · 还要 20 小时`、`可以采`、`部分采集`）/ 上次采集时间 / 路线进度（`11/11 条`）；
  * 右上角能**搜材料名**、**只看冷却中**、**刷新**；每行两个按钮：**记为刚采过**（游戏里自己采的，
    等价 `python -m skills.gather_cooldown --manual 霜仙花`）、**清除**（删掉记录重新以日志为准）；
  * 数据从 `/api/cooldown` 来（`skills/gather_cooldown.overview()`：**一次算完全部材料**，
    内部只解析一遍日志 —— 逐个材料调 `status()` 会把日志重扫几十遍，实测慢几十倍）；
    返回 `sections[]`（按类别分区）+ 扁平 `materials[]`，页面直接照这个渲染；
  * 类别由"这个材料挂在哪个脚本组"决定（四个总组 + 你自己按魔物/材料建的小组：
    蕈兽.json、骗骗花.json、虹滴晶.json…）；敌人与魔物那些组的成员是魔物名（巡陆艇、蕈兽…），
    按各自的 12 小时算，不再被这页排除；
* **任务与路线**：每个调度器脚本组的路线数、战斗策略是否真的存在、有没有登记进一条龙；
  上方卡片是各路线类目（敌人与魔物 1014 / 锄大地 417 / 矿物 665 / 食材与炼金 2342）的条数与建组状态；
* **远程通道**（QQ 机器人 / 飞书服务端）：**不用再开一个控制台窗口** —— 通道是 Studio 的子进程，
  启停按钮和 stdout 都在这一页里（等宽、分级上色、自动滚动），概览页也有一个摘要卡 + 本地图鉴统计。
  详见下面第 3.1 节。
* **配置**：左侧分组、右侧表单；**只提交你改过的键**（后端按 key 合并，未动的键保持原值）；
  切页会把未保存的改动收进"待保存"集合，保存按钮会显示改动数量；保存自动备份 `.env.bak-<时间戳>`；
* **体检**：`skills/health_check.py` 的结果按级别配色（真机上会点出 11 个组配了不存在的 `1.四神挂机[推荐]`）；
* **BetterGI 日志**：直接读 `BetterGI\log\*.log`（只看 `.log`，自动跳过识别失败的截图），
  行号 + 关键字过滤 + 已知坑高亮：`战斗策略文件不存在` / `目标传送点位于不可点击区域` /
  `当前获取焦点的窗口不是原神` / `状态为禁用，跳过执行` / `0xc00000fd` 崩溃。
  这些正是之前反复排查的那几类问题，现在打开页面就能看到。

### 3.1 「远程通道」页：把机器人的窗口内嵌进来 + 随 Agent 自动启动

以前跑 QQ 机器人要双击一个 `.bat`，多出一个控制台窗口；现在它是 Studio 管的子进程：

* **内嵌控制台**：`qq_main.py` / `feishu_main.py` 的 stdout/stderr 按块读进环形缓冲
  （和 Agent 日志同一套 `studio/agent_runner.py` 的读法：CLI 的提示语不带换行，按行读会卡住），
  页面里等宽显示、按级别上色，每 1.2 秒增量刷新；「清空日志」只清缓冲，不影响进程。
* **启停按钮**：每个通道一张卡，`▶ 启动` / `■ 停止` / `清空日志`，状态徽章显示
  运行中 / 未启动 / 已退出（带 PID）；左上角状态表只给摘要，日志走 `/api/channels`。
* **随 Agent 自动启动**（玩家要的逻辑）：
  * 点「▶ 启动 Agent」时，`POST /api/agent/start` 会顺带调用 `ChannelManager.start_automatic()`；
  * **只有填了配置的通道才会被唤醒** —— QQ 要 `QQ_BOT_APPID` + `QQ_BOT_SECRET`，
    飞书要 `FEISHU_APP_ID` + `FEISHU_APP_SECRET`；没填就跳过，并把"缺哪一项"写进通道日志 + 弹提示；
  * 开关存在 `.env`：`AUTO_START_QQ_BOT`（默认 **1**）、`AUTO_START_FEISHU_BOT`（默认 **0**）。
    卡片上的勾选框直接改这两项（走 `/api/config`，只提交这一键）。
  * 点「■ 停止」或关窗口（`/api/shutdown`）时，通道一起停。
* **飞书为什么默认不自动启动**：它是 Webhook 服务（监听 5000 端口），**公网不可达（没做内网穿透）时
  收不到任何消息**，白占一个进程；填好凭证又确实配了穿透，把开关打开即可。
* **通道是独立进程**：QQ 机器人本身就是完整的 Agent 入口（规划/审批/事务全都在它自己进程里），
  所以 Studio 的 Agent 没启动时，机器人照样能用 —— 两者共用 `memory/chat_context.json`，
  **别同时开两个入口**（互相覆盖历史与待审批任务）。
* **单实例保护**：QQ 机器人自己有文件锁（`memory/qq_bot.lock`），重复启动会被拒绝并打日志；
  所以"Studio 里启动 + 又双击了 bat"这种情况不会变成两个机器人在抢会话。

## 4. 无边框窗口（自绘标题栏）

系统标题栏（那条浅色的最小化/最大化/关闭栏）跟这套深色界面拼在一起很割裂，所以整条去掉了：

* `FormBorderStyle.None`：窗口没有系统标题栏与边框，界面顶栏直接顶到窗口上沿；
* **⚠️ 千万别把 WS_THICKFRAME 一起丢掉**（第一版就踩了这个坑）：`FormBorderStyle.None`
  会顺手拿掉 `WS_THICKFRAME`，窗口就**再也拉不动大小**了。正确做法是在 `CreateParams` 里
  **只清 `WS_CAPTION`**，把 `WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX` 补回来 ——
  既没有那截浅色标题栏，又保留可拉伸的边框；
* **拉伸自己做**：补回 `WS_THICKFRAME` 之后边缘命中测试能返回 `HTLEFT` 之类，但实测系统
  **不会**因此进入自己的尺寸调整循环（收不到 `WM_ENTERSIZEMOVE`），所以宿主直接接管：
  按下边缘（四边/四角各 6 像素）时自己 `SetCapture`，收到 `WM_MOUSEMOVE` 就按光标增量
  `SetWindowPos` 改窗口矩形，`MinimumSize` 兜底；
* **拖动**：顶栏当标题栏用。WebView2 虽然在设置里支持 `app-region: drag`
  （`IsNonClientRegionSupportEnabled`），但在"宿主自绘窗口 + WinForms 版控件"下实测命中测试仍返回
  `HTCLIENT`（拖不动），所以改成**界面上报屏幕坐标**：JS 在顶栏 `mousedown/mousemove` 时把
  `screenX/screenY` + `devicePixelRatio` 发给宿主，宿主按增量 `SetWindowPos`（缩放屏也不会错位）；
  双击顶栏空白处 = 最大化/还原。
* **最小化 / 最大化 / 关闭**：界面右上角自绘三个按钮（`.win-controls`），点击时 postMessage 给宿主；
  这组按钮只在原生窗口里显示（`app.js` 检测 `window.chrome.webview`，浏览器模式自动隐藏）。
* **圆角/深色边框**：Win11 上用 `DwmSetWindowAttribute` 设 `DWMWCP_ROUND` + 深色模式（Win10 上调用失败就忽略）；
  无边框窗口最大化会盖住任务栏，所以在 `WM_GETMINMAXINFO` 里按显示器工作区收一下。
* **自检**：放一个 `studio-selftest.flag` 到 exe 旁边（或加 `--selftest`）启动，
  宿主会在界面加载后跑一遍"拖动 120,60 → 最大化 → 还原 → 拉右下角"，把结果写进 `logs\studio-host.log`：

```
[host] 自检：窗口 580,304 -> 700,364（位移 120,60），脚本返回 "drag-probe-sent"
[host] 自检结论：✅ 拖动链路正常（界面 → 宿主 → 移动窗口）
[host] 自检：最大化后 WindowState=Maximized
[host] 自检：还原后 WindowState=Normal
[host] 自检结论：⚠️ 注入的鼠标输入没有送达本窗口（消息计数全 0），本环境无法自动验证拉伸
```

最后那条很重要：**受限环境（沙箱 / UIPI）会拦掉鼠标注入**，注入的点击一条消息都收不到，
所以"拉伸"这一项在那种环境下只能靠真实鼠标确认 —— 自检会如实说明（消息计数全 0 时不报成功也不报失败），
不会给你一个假的结论。

## 5. 关键实现细节（都是踩过的坑）

| 坑 | 处理 |
| --- | --- |
| CLI 的提示不带换行（`🛑 …请确认…`），按行读会卡在缓冲区 | `os.read` 按块读；未结束的那半行作为 `partial` 单独下发，前端渲染成"正在闪烁的提示" |
| `pythonw.exe` 下 `sys.stdout is None`，`print()` 直接抛异常把进程干掉（双击 exe "什么都没发生"） | `app_web.log()` 有控制台才 print，同时写 `logs\studio.log` |
| 冷启动 import（`lark_oapi` 等）要 30 秒，窗口先开就会白等 | `warmup()` 放后台线程，窗口先显示"正在启动…"，宿主轮询到 `/api/state` 通了才导航 |
| `/api/state` 每 1.2 秒被轮询，而它要遍历 4400+ 路线文件、还会打印一行提示 | 环境摘要缓存 10 秒，并吞掉那行 stdout 提示；实测 10 次轮询共 0.13 秒 |
| 按分组页保存配置时，若把没提交的键补成空串会**清空整份 .env** | 后端只更新传进来的键（`{**current, **incoming}`），并拒绝未知键 |
| 自解压 WebView2 依赖时只写 `%LOCALAPPDATA%` → 受限环境下"访问被拒绝"，程序集加载失败、窗口只剩启动画面 | 按 `<exe目录>\.studio-lib` → `%LOCALAPPDATA%\GI_Agent` → `%TEMP%\GI_Agent` 顺序挑**可写**目录（写探针确认） |
| `CoreWebView2Environment.CreateAsync` 在受限令牌/沙箱里报 `0x8000FFFF (E_UNEXPECTED)` | 依次尝试 4 组"用户数据目录 × 浏览器参数"（默认 / `--disable-gpu` / 换临时目录 / `--no-sandbox`），全失败才退回浏览器窗口并明确提示 |
| 关了窗口但后端 Python 还在跑 | 宿主 `FormClosing` 里 `taskkill /PID <后端> /T /F`；后端自己退出（界面点"退出 Studio"）时也会关窗口 |
| `.bat`/`.ps1` 的中文编码 | `.bat` 一律纯 ASCII（cmd 按 OEM 代码页解析）；`.ps1` 存 UTF-8 **带 BOM**（PS 5.1 否则按 ANSI 读，中文会炸） |
| VS 里的 `...\.NETFramework\v4.X` 目录只有 RedistList、没有 dll → 编译报 CS0006 | 编译脚本会逐个确认 `System.dll` 真在目录里，选不到就退回 GAC 里的同名程序集 |

## 6. 用法

```powershell
# 新版（推荐）：双击 exe 就是一个原生窗口
双击 GI-Agent-Studio.exe            # 或 启动GI-Agent-Studio.bat
GI-Agent-Studio.exe --browser       # 强制用"浏览器应用窗口"模式（排查问题时）
python main.py studio               # 走 CLI 入口（等价于 app_web.py）
python app_web.py --no-window       # 只起服务（打印地址，手机/平板也能看）
python app_web.py --port 8848       # 固定端口

# 重新编译 exe
powershell -ExecutionPolicy Bypass -File scripts\build_studio_exe.ps1

# 旧版（备用）
启动GI-Agent控制台.bat  |  python main.py gui
```

排错看三个文件（都在 `logs\`）：`studio-host.log`（宿主：解压、WebView2 初始化、关闭）、
`studio.log`（后端启动/预热）、`studio-access.log`（界面请求；看到 `GET /` 就说明窗口真的加载了，
2 MB 滚动一个备份，不会无限长）。

## 7. 目录

```
GI-Agent-Studio.exe          原生窗口宿主（编译产物，0.8MB 单文件）
scripts/native/StudioHost.cs 宿主源码：起后端 → 嵌 WebView2 → 关窗口时收摊
scripts/fetch_webview2.py    下载并解包官方 WebView2 SDK（只取 3 个文件）
scripts/build_studio_exe.ps1 一键编译（csc + 嵌入资源）
scripts/make_icon.py         纯 Python 生成 .ico（不依赖 Pillow）
app_web.py                   本地服务入口（--no-window / --browser / --exit-when-idle）
studio/agent_runner.py       Agent 子进程桥（启动/停止/输入/日志缓冲/状态机）
studio/channel_runner.py     远程通道子进程桥（QQ 机器人 / 飞书 + 自动启动规则）
studio/server.py             Flask 接口 + 静态页面 + 环境摘要缓存
studio/logs.py               读 BetterGI 日志 + 已知问题高亮
studio/guide.py              「使用说明」页的上手清单与调度器清单
studio/web/{index.html,app.css,app.js}   界面（无外网依赖、无构建步骤）
```

## 8. 还没做

* 手机端适配只做了"能看"（栅格会自适应），没有专门的移动布局；
* 还没做托盘图标 / 开机自启（目前是"启动 Studio → 点启动 Agent → 通道自动跟上"）；
* 想让 Studio 只读监控（不给启动/停止权限）的话，可以加个 `--readonly` 开关 —— 目前所有接口都可用；
* 单文件 exe 仍需要项目目录（`venv\`、`app_web.py`、`studio\`、`.env`）在旁边；
  想做"自带 Python 运行时、能单独拷走"的版本，得用 PyInstaller 把 Python 一起打包。
