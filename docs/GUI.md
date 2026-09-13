# GI Agent 控制台 · 设计说明（旧版 tkinter，备用）

> **新版界面（推荐）是 `GI-Agent-Studio.exe`** —— 网页内核 + Flask，观感对齐 BetterGI，
> 见 [`STUDIO.md`](STUDIO.md)。本文件描述的是**保留为备用**的 tkinter 版 `gui.py`：
> 零依赖、纯 stdlib，适合"Studio 打不开 / 只想最小依赖"的场景。

给这套 Agent 加一层图形界面的目标只有三个：**能启动/关闭**、**能改配置**、**能看清它在干什么**。
所以界面是「壳」不是「重写」——Agent 的规划、拦截层、审批流、事务回滚全部沿用现有代码。

## 1. 进程模型：界面只做管子

```
┌──────────────────────────┐        stdout/stderr（实时块读 utf-8）
│  GI Agent 控制台 (Tk)     │  ◀──────────────────────────────────┐
│  运行 / 配置 / 维护 / 帮助 │                                     │
│                          │        stdin（y / t / exit / 自然语言）│
│  ▶ 启动  ■ 停止           │  ──────────────────────────────────▶ │
└──────────────────────────┘                          ┌──────────────────────────┐
        │  直接调用库函数（同进程，只读或走备份事务）      │  main.py（子进程，-u）      │
        ├─ 刷新展柜  refresh_store_env_context          │  · LLM 规划               │
        ├─ 环境体检  skills/health_check.py             │  · 拦截层 / 改判 / 审批     │
        ├─ 回滚配置  config_recovery.restore_transaction│  · BetterGI 配置事务写入    │
        └─ 打开目录  os.startfile                        └──────────────────────────┘
```

为什么是子进程而不是把 main.py 改成库：

* `main.py` 的交互是**同步阻塞**的（`input()` 等审批），塞进 GUI 主线程会冻界面；
* 线程化改造要动到拦截层、事务写入、BetterGI 关闭逻辑 —— 风险远大于收益；
* 子进程方案下，界面崩了/被关掉也不影响 CLI 单独跑（`python main.py` 一直是可用的）。

读取用**按块 `os.read`** 而不是按行：CLI 的提示 `👤 旅行者 (你): ` 和
`🛑 [系统拦截] 请确认是否执行上述计划？` 都不带换行，按行读会卡在缓冲区里看不见。

界面把子进程标成三种状态（状态灯 + 顶部横幅）：`未运行 / 运行中 / 等你确认`，
检测到审批提示就变黄、响铃、把窗口提到前面，并在「运行」页顶出一条黄色横幅
—— 玩家不会因为切去看日志而漏掉"该拍板了"。

## 2. 窗口结构

```
┌ GI Agent 控制台 ─────────────────────────────────────────────────────────┐
│ [▶ 启动] [■ 停止]   ● 运行中   PID 12345        openai / deepseek-flash / 1847… │
├──────────────────────────────────────────────────────────────────────────┤
│ ┃ 运行 ┃ 配置 (.env) ┃ 维护工具 ┃ 帮助 ┃                                  │
│                                                                          │
│  【运行】工具栏：[y 批准执行][t 仅写配置][refresh][history][clear][exit]     │
│                            [回滚配置…][清屏]                              │
│   实时日志（分级配色：❌错误 / ⚠️告警 / 🤖Agent / 👤我 / 🛑审批提示）        │
│   …                                                                      │
│   发给 Agent：[_______________________________] [发送]                    │
│                                                                          │
│  【配置】左：分组列表（6 组）   右：字段表单（中文名 + 键名 + 控件 + 说明）    │
│   模型/密钥 · 玩家与记忆 · BetterGI 路径 · 一条龙与脚本组名 · 路线组策略 · 飞书 │
│   [重新载入] [保存] [显示密钥]   —— 保存自动备份 .env.bak-<时间戳>           │
│                                                                          │
│  【维护】[🔄刷新展柜][🩺环境体检][↩️回滚配置][📂项目][📂BGI日志][📂备份]…     │
│   结果输出区（体检报告 / 刷新结果）                                        │
└──────────────────────────────────────────────────────────────────────────┘
```

设计取舍：

| 需求 | 做法 | 原因 |
| --- | --- | --- |
| 改配置 | 图形表单 + 保留 `.env` 原始注释与顺序 | 玩家手写的注释不能丢；只改值不重排 |
| 密钥 | `Entry(show="•")` + 「显示密钥」开关 | 默认防肩窥，又不用记 |
| 保存 | 先 `.env.bak-<时间戳>` 再「临时文件 + `os.replace`」 | 写坏了能立刻回退 |
| 生效 | 保存后弹窗提醒"必须重启 Agent" | `config.py` 在 import 时就读取环境变量 |
| 停止 | `taskkill /PID <pid> /T /F`（只杀 Agent 进程树） | 不碰 BetterGI，游戏里的自动化照跑 |
| 回滚 | 列表选事务 → 二次确认 → `restore_transaction` | 沿用 CLI 的 `rollback` 同一套实现 |
| 打包 | `--run-cli` 自举 + PyInstaller onefile | exe 里没有 main.py 文件，只能让 exe 自己再当一次子进程 |

## 3. 配置页字段来源

字段元数据在 `skills/env_config.py` 的 `FIELD_GROUPS` 里声明（键名 / 中文名 / 类型 /
可选项 / 默认值 / 说明），`.env` 只存值。类型支持 `text / secret / int / bool / choice / path`：

* `int`：非整数、非正数 → 告警；
* `bool`：只接受 `0/1`（与 `config.py` 里 `== "1"` 的读法一致）；
* `choice`：不在推荐值里 → 告警（但仍允许保存，便于兼容新提供商）；
* `path`：目录不存在 → 告警，并带「…」目录选择按钮。

有一条**守护测试**：`FIELD_GROUPS` 里出现的每个键都必须在 `.env.example` 里出现，
防止界面加了字段却忘了写文档。

## 4. 环境体检（`skills/health_check.py`）

界面按钮和 `python main.py doctor` 共用同一份实现，只读不改：

1. LLM：提供商 ↔ 对应密钥字段是否为空、模型名、超时是否过小；
2. BetterGI：安装目录、主程序、**当前真正生效的一条龙配置**（不是 `.env` 里写的那份）、
   已登记任务与当前勾选、BetterGI 是否正在运行；
3. 脚本组：组数量、每个组的路线数、**战斗策略是否真的存在于 `User\AutoFight`**
   （实测真机上 11 个组配的 `1.四神挂机[推荐]` 并不存在 → 走到怪点就抛
   `战斗策略文件不存在`，现象很像"识别不到战斗结束"）；
4. `User\AutoPathing` 里各路线类目（含锄地专区）有多少条、有没有建组；
5. 展柜缓存新鲜度（`env_context_age_note`）；
6. 可回滚的配置事务数量与最近一条。

## 5. 打包成 exe

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1        # 生成 dist\GI-Agent-Console.exe
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Console  # 保留黑窗看报错
```

* `--run-cli`：exe 模式下「▶ 启动」执行的是 `GI-Agent-Console.exe --run-cli`，
  在子进程里 `import main` 并跑 `main()`，同时把 `stdout` 换成"每次写都 flush"的流
  —— 否则 `input()` 提示不带换行，界面看不到；
* `--hidden-import main`：`main` 是运行时才 import 的，必须显式声明，否则打包后会丢；
* `--exclude-module playwright`：`skills/` 里那些爬虫用不到，避免把浏览器驱动打进去；
* 产物放在**项目根目录**运行：`.env`、`memory/`、`logs/` 都按相对路径找。

不想打包也能"双击即用"：`启动GI-Agent控制台.bat` 会用 `venv\Scripts\pythonw.exe` 静默拉起界面
（无黑窗）。这个 .bat **故意写成纯 ASCII** —— cmd.exe 按 OEM 代码页解析批处理，
里面写中文会在非 UTF-8 控制台上把解析搞崩；`scripts/build_exe.ps1` 则带 UTF-8 BOM，
这样 Windows PowerShell 5.1 也能正确读中文。三种启动方式等价：

```
启动GI-Agent控制台.bat      # 双击
python main.py gui          # 走 CLI 入口
venv\Scripts\pythonw.exe gui.py
```

## 6. 已知限制 / 后续可做

* 界面不显示 BetterGI 自己的日志（`BetterGI\log\*.log`）—— 后续可以在「维护工具」里加一个
  日志查看器 + 关键字高亮（`战斗策略文件不存在` / `目标传送点位于不可点击区域` / 崩溃已确认）；
* 关掉控制台窗口不会自动停掉已经在跑的 BetterGI（这是故意的：游戏自动化不该被误杀）；
* 想要手机上看，可以再补一个 Flask 只读监控页（Flask 已在依赖里），复用同一份
  `health_check` 与日志桥；
* 托盘图标 / 开机自启没做（与 BetterGI 自己的调度功能重叠，收益不大）。
