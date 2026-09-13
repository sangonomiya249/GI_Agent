# BetterGI 准备清单

Agent 不替你配置 BetterGI —— 它只是**改 BetterGI 的配置并触发一条龙**。
所以动手之前，请先把下面这些做完，并且**每条任务都先在 BetterGI 里手动跑通一次**：

> **什么叫"跑通"**：这条任务在 BetterGI 里手动开启后能正确执行 —— 队伍切对了、路线走完了、
> 战斗策略真的打起来了。没跑通就让 Agent 接手，后面排查会非常痛苦
> （现象往往是"改了配置但什么都没发生"，而原因在 BetterGI 那边）。

---

## 0. BetterGI 启动设置

启动页里勾上 **`同时启动原神`** → 展开下拉 → 勾上 **`自动进入游戏`**。

## 1. 订阅脚本

BetterGI 脚本仓库里搜索并订阅：

| 脚本 | 用途 |
| --- | --- |
| `地方特产` | 大世界采集路线（地图素材组） |
| `批量讨伐角色养成材料BOSS` | 突破材料（Boss 讨伐） |
| `AAA-Artifacts-Bulk-Supply`（AAA狗粮批发） | 狗粮 |
| `万能战斗策略（萌新推荐）` | 战斗策略之一（也可以换成你自己的） |

## 2. 调度器与一条龙

把下面这些脚本先加进**调度器**单独测试，通过后再加入 BetterGI 的**一条龙**，
并确保**名称完全一致**（Agent 按名字找脚本组）。各脚本自己的 README 也要读一下 ——
你可能需要在调度器里给不同任务配不同的战斗队伍 / 行走队伍与策略。

| 组名（默认） | 怎么建 | 必需？ |
| --- | --- | --- |
| `地图素材` | 调度器里把 `地方特产` 下**所有**目录全选加进来（不用管路线重叠），命名 `地图素材`，加入一条龙 | ✅ 必需 |
| `批量讨伐角色养成材料BOSS` | 脚本名保持一致，加入一条龙 | ✅ 突破材料必需 |
| `AAA狗粮批发` | 名称保持一致，加入一条龙 | 可选 |
| `矿物` / `食材与炼金` | 同「敌人与魔物」那套：把 `repo/pathing/矿物`、`repo/pathing/食材与炼金` 下的目录加进来（建议按材料分成小组，如 `石珀`、`虹滴晶`）；也可以建总组（`矿物.json`），Agent 会自动精简 | 可选 |
| `敌人与魔物` | 把 `repo/pathing/敌人与魔物` 下的敌人目录加进来即可，**不用手工拆组** | 可选 |
| `锄大地` | **不用手工建**：Agent 会自动扫 `User\AutoPathing\锄地专区` 生成 `User\ScriptGroup\锄大地.json` | 可选 |

组名都可以在 `.env` 里改（`BGI_MAP_CONFIG_NAME` / `BGI_ENEMY_CONFIG_NAME` / `BGI_MINE_CONFIG_NAME` /
`BGI_COOK_CONFIG_NAME` / `BGI_HOE_CONFIG_NAME`）。

### 组很大怎么办（自动精简）

* 组小的时候（例如你自己按敌人分的 `蕈兽` 14 条、`骗骗花` 86 条、`刀谭` 9 条）：Agent 直接在里面开关路线，**小组优先**；
* 组很大的时候（例如把整个 `敌人与魔物` 1000+ 条塞成一个组）：Agent 会自动把组**精简成"只有本次要打的路线"**，
  完整清单归档到 `User\ScriptGroup\.gi_agent_archive\<组名>.json`，下次换目标自动从归档里重新挑。

> 为什么必须精简：BetterGI 会为脚本组里**每一条 `Disabled` 的路线写一行日志**，
> 几百条的大组在跑一半按停止快捷键时日志会瞬间爆发（实测 550~700 行/秒），
> 随后 BetterGI 可能在 WPF 层栈溢出崩溃（`0xc00000fd`）。组里全是 `Enabled` 就没这个问题。
>
> 相关配置：`BGI_ROUTE_GROUP_POLICY`（`shrink` 默认 / `refuse` 拒绝 / `allow` 照旧）、
> `BGI_MAX_ROUTE_GROUP_SIZE`（默认 300）、`BGI_FORCE_ENABLE_BAND`（防闪退隔离带开关，见
> [内部机制](INTERNALS.md#防闪退隔离带)）。

### 战斗策略名校验

Agent 写路线组时会检查该组 `pathingConfig.autoFightConfig.strategyName` 在 `User\AutoFight\` 下
**有没有对应的 `.txt`**。BetterGI 是按**平铺**路径解析的（`User\AutoFight\<策略名>.txt`），
放在子目录里的策略必须写成 `群友分享\四神队(进阶版)`。

缺了会告警，并告诉你是「仓库里有、但没导入」还是名字写错了。**不校验的后果**：
跑图走到怪点直接抛 `战斗策略文件不存在` 并中断整条路线，看起来很像"战斗打不起来 / 识别不到战斗结束"。

### 锄大地和敌人与魔物不一样

两边路线名会重名（锄地专区里有 `0_0_飞萤` 子目录、还有叫「三骗骗花」的路线），Agent 用
`folderName` 的目录前缀严格隔离（`敌人与魔物\…` vs `锄地专区\…`）。所以说法要分清：

| 你说 | 走哪条 | 效果 |
| --- | --- | --- |
| `刷点飞萤` / `缺刀镡` / `打点骗骗花` | `hunt`（敌人与魔物） | 只打那个魔物的固定怪点 |
| `锄大地` / `锄地` / `清怪` / `扫图` / `刷精英` / `打传奇` / `锄大地 璃月` | `hoe`（锄大地） | 按地区扫图 |
| `跑一下锄地一条龙` | `script`（AutoHoeingOneDragon） | mno 的一站式锄地 JS 脚本 |

实测：`飞萤` 在 `hunt` 里命中 19 条、在 `hoe` 里命中 13 条，互不串味；`锄大地` 在 `hunt` 里命中 0 条。
锄大地默认**跳过** `低效路线(不跑）`、`稻妻未修正部分（不跑）`、`精英400@汐\3-低效`（417 → 301 条）；
说「连低效一起跑」才带上全部。

全图锄地一次约 300 条路线、耗时以小时计。只想清一块地区就说 `锄大地 璃月`（实测 56 条）或
`锄大地 挪德卡莱`（24 条）。

## 3. 独立任务可用性测试（很重要）

在 BetterGI 里单独测试下面这些，确认能跑之后**全部设为开启**：

* 自动地脉花
* 自动秘境
* 一条龙里的杂项：领取邮件、合成树脂、领取每日奖励、领取尘歌壶奖励

## 4. 免管理员确认启动（必做）

BetterGI 需要管理员权限，直接启动会被 UAC 拦住。**一键版**（推荐）：
右键「Windows PowerShell」→「以管理员身份运行」：

```powershell
powershell -ExecutionPolicy Bypass -File "<你的仓库路径>\scripts\setup_start_bettergi_task.ps1" -RunTest
```

它会读取 `.env` 里的 `BGI_DIR`（读不到就自动探测）、注册任务并试跑一次验证。
注册的是三个任务：`StartBetterGI`（免 UAC 拉起一条龙）、`StopBetterGI`、`StopGenshin`（关原神）。

检查是否建好：

```powershell
python main.py doctor          # 逐条报告三个任务在不在，缺了会给完整注册命令
python -m skills.task_scheduler
```

> ⚠️ 手动建的话**名称必须完全一致**：`StartBetterGI`（代码里硬编码这个名字触发），
> 任务要勾「使用最高权限运行」，程序填 `BetterGI.exe` 完整路径，参数 `--startOneDragon`，
> 「起始于」填 BetterGI 所在**文件夹**（不要带引号）。
> 计划任务不存在时，Agent 会自动回退为"直接启动 BetterGI.exe"（可能弹 UAC），这只是兜底。

手工验证 BetterGI 本体没问题：

```powershell
cd /d "C:\Program Files\BetterGI"
".\BetterGI.exe" --startOneDragon
```

## 5. 一条龙任务为什么会「静默不跑」

BetterGI 新格式配置里，`TaskEnabledList` / `TaskOrder` 用的键是**任务 Id**，
`TaskDefinitions` 才是「Id → 任务名」，运行阶段按**任务名**去取 `User\ScriptGroup\<任务名>.json`。
所以往 `TaskEnabledList` 直接写「组名: true」是**死配置**：BetterGI 连这个任务都不认识
（看着像启用了，其实永远不跑）。

Agent 的处理：

1. **优先用已登记的组**：`骗骗花.json` 没登记、但总组 `敌人与魔物` 登记过 → 退到总组精简成那 86 条再用；
2. 只有**确实存在脚本组文件**的任务才会自动补登记（日志写明 `🆕「X」之前没登记进一条龙，已自动添加为任务项`）；
   BetterGI 内置任务（自动秘境 / 自动地脉花 / 自动首领讨伐…）一律不代登记；
3. 下一轮没再排的、由 Agent 加的任务项会被摘掉（你手动加的绝不乱动）；
4. Agent 开过的调度器，下一轮没排就关掉（`🧹 已关闭上一轮由 Agent 开启、本轮没排的调度器「X」`）；
5. 收尾清理 `TaskEnabledList` 里没有 `TaskDefinitions` 条目的死键。

想立刻清掉历史遗留（关掉 BetterGI 后执行，走备份事务、可 `rollback`）：

```bash
python main.py repair          # 无法确认 BetterGI 在不在跑时会要求加 --force
python main.py repair --force
```

**突破材料走的是哪个任务？** 《批量讨伐角色养成材料BOSS》这个 **JS 脚本组**（Agent 会把队伍 / 策略 / 次数
写进它的 `assets/config/config.json`），**不是** BetterGI 内置的「自动首领讨伐」—— 内置那条读的是流程配置里的
`AutoBossName`，Agent 从来没写过它，打开只会打印"一条龙配置内未选择需要讨伐的首领，跳过"。

**讨伐一开始弹一个要点「保存并关闭」的窗口？** 那是脚本自带的 BOSS 配置编辑器
（`settings.json` 里 `showEditorOnStart: true`）。Agent 会自动把它改成
`{"showEditorOnStart": false, "showStatusPanel": false}`（写进脚本组里那个 JS 项目的 `jsScriptSettingsObject`，
跟其它改动一起走同一个配置事务）。想改内容就设 `.env` 的 `BGI_JS_SCRIPT_SETTINGS`；
想恢复手动编辑，在 BetterGI 里右键该脚本 →「修改JS脚本自定义配置」。

## 6. 路径与名字对不上？

个人路径与配置名都在 `.env` 里，**不要改 `config.py`**：

```dotenv
BGI_DIR=C:\Program Files\BetterGI
BGI_ONE_DRAGON_CONFIG_NAME=默认配置.json     # Agent 会自动识别真正生效的那份
BGI_GLOBAL_CONFIG_RELATIVE_PATH=User/config.json
BGI_BOSS_CONFIG_RELATIVE_PATH=User/JsScript/批量讨伐角色养成材料BOSS/assets/config/config.json
```

`python main.py doctor` 会把「你配的」和「实际生效的」都列出来。

## 7. 避坑与注意事项

* **队伍配置**：不同任务可以单独配不同队伍。
* **Boss 讨伐高危**：强烈建议用挂机输出强 + 生存强的队伍（少机 / 少莉 / 芙茜爱 / 四神等），
  否则成功率没保障（原作者用 6 芙的莉希机芙也会被深罪浸礼者拍死）。
  经常失败就找对应脚本作者，或自己改脚本 json 里的定位（比如三角铁）。
* **狗粮队伍**：无特别要求，移速最快的跑图大队即可。
* **采集特化**：大世界采集**强烈建议带纳西妲和芙宁娜**，部分脚本路径依赖这两个角色提升效率。
* **玄学命中率**：阴间材料（沙脂蛹、慕风蘑菇…）命中率低属正常现象。
* **BetterGI 侧已知问题**（不是 Agent 造成的）：个别传送锚点报
  `目标传送点位于不可点击区域，传送失败`（重试 3 次后该路线失败）；跑大组时按停止快捷键
  `Up` 容易触发日志爆发与栈溢出崩溃 —— 见 [排错 FAQ](TROUBLESHOOTING.md)。
