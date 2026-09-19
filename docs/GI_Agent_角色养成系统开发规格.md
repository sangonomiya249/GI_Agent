# GI_Agent：米游社养成计算器 + 角色自动养成系统开发规格

## 1. 项目目标

在现有 GI_Agent 项目中新增“角色养成规划”系统。

核心原则：

1. **米游社养成计算器接口**作为角色养成需求和背包库存的事实来源。
2. **BetterGI**只负责实际执行采集、战斗、秘境、Boss 等任务。
3. BetterGI 日志**不得直接修改真实库存数量**。
4. Studio 每次加载时默认同步一次米游社库存并保存快照。
5. 每次 BetterGI 养成任务完成后重新同步米游社库存，再重新计算材料缺口。
6. 一个角色默认完成当前设定的全部养成目标后，才进入下一个角色。
7. 用户可以自定义角色顺序。
8. 默认角色顺序为 **新出的5星角色> 5 星角色 > 4 星通用辅助> 4 星角色**。
9. 用户可以分别设置角色等级、武器等级、普通攻击、元素战技、元素爆发目标等级。
10. 用户可以分别设置“角色等级 / 武器等级 / 天赋”的优先级。
11. 默认优先级：**角色等级 > 武器等级 > 天赋**。
12. 支持角色等级任意目标值 `1~90`，例如 81、90。
13. 支持三个天赋分别设置目标等级。
14. 第一版武器只规划等级，不实现精炼材料。
15. BetterGI 的路线冷却继续复用现有 `gather_cooldown`，不重复实现。
16. 第一版优先保证**确定性、可追踪、可回滚**，LLM 不直接计算养成材料数量。

---

# 2. 总体架构

```text
                    ┌──────────────────────┐
                    │      GI Agent        │
                    │   LLM / Planner      │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │ Growth Planner       │
                    │ 养成规划核心          │
                    └─────┬─────────┬──────┘
                          │         │
             ┌────────────▼─┐   ┌──▼────────────┐
             │ MiYoShe Sync │   │ User Targets  │
             │ 米游社同步    │   │ 用户养成目标   │
             └──────┬───────┘   └───────────────┘
                    │
             ┌──────▼────────┐
             │ Inventory DB  │
             │ 材料真实库存   │
             └──────┬────────┘
                    │
             ┌──────▼────────┐
             │ Material Need │
             │ 材料缺口计算    │
             └──────┬────────┘
                    │
             ┌──────▼────────┐
             │ BetterGI Plan │
             │ 路线/秘境/BOSS │
             └──────┬────────┘
                    │
             ┌──────▼────────┐
             │ BetterGI      │
             │ 实际执行       │
             └───────────────┘
```

### 系统职责

| 模块 | 职责 |
|---|---|
| 米游社 | 提供真实库存、角色状态、养成需求 |
| 养成计算器 | 计算目标养成所需材料 |
| Inventory | 保存米游社库存快照 |
| Growth Planner | 根据目标 + 库存生成养成计划 |
| Material Planner | 将材料缺口转换为可执行任务 |
| BetterGI | 实际执行采集、战斗、秘境等 |
| LLM / Agent | 理解用户意图、调用工具、协调流程 |
| Studio | 提供用户配置和状态展示 |

核心原则：

> **米游社 = 库存事实来源**  
> **养成计算器 = 需求计算来源**  
> **BetterGI = 执行器**  
> **Agent = 决策 / 调度器**

---

# 3. 为什么不能让 BetterGI 直接维护库存

BetterGI 拾取材料无法精准知道实际获得数量，因此禁止：

```text
BetterGI 预计获得 50
↓
本地库存 +50
```

必须：

```text
BetterGI 执行
↓
任务结束
↓
重新请求米游社
↓
获取真实库存
↓
重新计算缺口
```

例如：

```text
米游社同步：

清心 = 20

角色需要：

清心 = 80

缺口：

60
```

执行 BetterGI 路线后，不假定获得多少。

重新同步：

```text
清心 = 57
```

重新计算：

```text
需求：80
库存：57
缺口：23
```

然后继续规划。

---

# 4. 米游社库存同步

## 4.1 新增模块

```text
skills/
    mys_api.py
    mys_inventory.py
    mys_calculator.py
```

其中：

- `mys_api.py`：现有米游社基础接口
- `mys_inventory.py`：库存同步和快照
- `mys_calculator.py`：养成计算器 API 封装

---

## 4.2 内部库存模型

不要让项目其它模块直接依赖米游社原始 JSON。

统一转换成内部结构：

```json
{
  "uid": "xxxxxxxx",
  "server": "cn_gf01",
  "fetched_at": "2026-09-18T14:00:00+08:00",
  "items": {
    "item_123": {
      "item_id": 123,
      "name": "清心",
      "count": 87,
      "category": "character_material"
    },
    "item_456": {
      "item_id": 456,
      "name": "摩拉",
      "count": 1250000,
      "category": "currency"
    }
  }
}
```

必须保存：

| 字段 | 用途 |
|---|---|
| item_id | 稳定材料 ID |
| name | UI 显示 |
| count | 当前数量 |
| category | 材料分类 |
| fetched_at | 数据时间 |
| uid | 防止账号串数据 |
| server | 防止服务器串数据 |

---

# 5. 启动时自动同步

默认行为：

```text
Studio 启动
↓
检查 MYS_COOKIE
↓
检查 UID
↓
调用米游社
↓
获取库存
↓
保存 snapshot
↓
更新当前库存
```

失败时：

```text
米游社同步失败
↓
保留上一次成功 snapshot
↓
页面显示同步失败
↓
标记上一次同步时间
↓
禁止将缓存数据伪装成实时数据
```

建议增加配置：

```text
米游社自动同步：

○ 每次启动
○ 每 30 分钟
○ 每 2 小时
○ 手动
```

默认：

```text
每次启动
```

---

# 6. 原始 API 响应保存

建议：

```text
memory/
└── mys/
    ├── inventory_latest.json
    ├── snapshots/
    │   ├── inventory_20260918_140312.json
    │   └── inventory_20260918_152015.json
    ├── character/
    │   └── 10000089.json
    └── calculator/
        └── 10000089_80_to_90.json
```

作用：

1. 调试米游社接口
2. API 改版后排查问题
3. 减少重复请求
4. 对比两次库存
5. 分析 BetterGI 实际采集结果

例如：

```text
14:00
清心 = 20

↓ BetterGI 执行

15:20
清心 = 87

实际增加：
+67
```

---

# 7. 角色养成目标模型

角色目标：

```json
{
  "character_id": 10000089,
  "character_name": "角色名",
  "rarity": 5,
  "enabled": true,
  "priority": 1,

  "target": {
    "level": 90,

    "skills": {
      "normal": 10,
      "skill": 10,
      "burst": 10
    },

    "weapon": {
      "enabled": true,
      "level": 90
    }
  },

  "category_priority": {
    "character_level": 1,
    "weapon_level": 2,
    "talent": 3
  },

  "completion_mode": "sequential"
}
```

---

# 8. 角色目标 UI

新增 Studio 页面：

```text
角色养成
```

顶部：

```text
角色养成规划

[同步米游社库存]

最后同步：
2026-09-18 14:03

[✓] 自动同步
```

---

## 8.1 角色列表

```text
┌────────────────────────────────────────────────────────────┐
│ 角色养成目标                                                │
├────┬────────┬──────┬────────────┬────────────┬─────────────┤
│启用│角色     │星级 │当前状态     │目标         │优先级        │
├────┼────────┼──────┼────────────┼────────────┼─────────────┤
│ ☑  │角色A    │ ★5  │80/8/8/8    │90/10/10/10 │1             │
│ ☑  │角色B    │ ★4  │80/6/8/8    │90/9/9/9    │2             │
│ ☐  │角色C    │ ★5  │70/6/6/6    │90/9/9/9    │3             │
└────┴────────┴──────┴────────────┴────────────┴─────────────┘
```

支持：

- 启用 / 禁用
- 拖拽排序
- 编辑优先级
- 显示当前养成状态
- 显示目标
- 显示材料完成度

---

# 9. 角色目标详细设置

点击角色后：

```text
角色：角色A

当前状态

角色等级：
80

普通攻击：
8

元素战技：
8

元素爆发：
8
```

目标：

```text
角色等级：
[ 90 ▼ ]

普通攻击：
[ 10 ▼ ]

元素战技：
[ 10 ▼ ]

元素爆发：
[ 10 ▼ ]
```

---

# 10. 角色等级目标

角色等级支持：

```text
1 ~ 90
```

例如：

```text
80 → 81
80 → 90
```

都必须合法。

不要只提供：

```text
80 / 90
```

因为用户需要可以选择：

```text
81
82
83
...
90
```

角色突破和升级材料需求必须交给米游社养成计算器处理，不允许 Agent 自己硬编码计算。

---

# 11. 天赋目标

三个天赋必须独立配置：

```text
普通攻击：
[ 10 ]

元素战技：
[ 10 ]

元素爆发：
[ 10 ]
```

必须支持：

```text
10 / 9 / 8
```

或者：

```text
6 / 6 / 6
```

或者：

```text
1 / 10 / 10
```

不能只保存一个统一的“天赋等级”。

---

# 12. 武器目标

第一版：

```text
[✓] 纳入武器养成

当前等级：
80

目标等级：
[90 ▼]
```

支持：

```text
1 ~ 90
```

第一版暂时不规划：

```text
武器精炼
```

第二阶段再增加：

```text
当前精炼
目标精炼
精炼相关需求
```

---

# 13. 单角色内部优先级

三个养成模块可以独立排序：

```text
本角色养成顺序：

① 角色等级      [1 ▼]
② 武器等级      [2 ▼]
③ 天赋          [3 ▼]
```

默认：

```text
角色等级 = 1
武器等级 = 2
天赋 = 3
```

用户可以修改：

```text
天赋 = 1
角色等级 = 2
武器等级 = 3
```

或者：

```text
武器等级 = 1
天赋 = 2
角色等级 = 3
```

---

# 14. 角色整体优先级

默认：

```text
5 星角色
↓
4 星角色
```

排序规则：

```text
用户 priority ASC
↓
rarity DESC
↓
character_id ASC
```

默认情况下：

```text
5星角色A
5星角色B
5星角色C
4星角色A
4星角色B
```

如果用户手动调整：

```text
4星角色A → 第一位
```

则：

```text
4星角色A
5星角色A
5星角色B
...
```

用户手动排序覆盖默认星级排序。

---

# 15. 角色排序模式

建议提供三种模式：

```text
角色排序模式

○ 默认
○ 自定义
○ 自动规划
```

## 默认

```text
5星 → 4星
```

## 自定义

用户拖拽：

```text
1. 角色A
2. 角色B
3. 角色C
4. 角色D
```

## 自动规划

第二阶段实现。

未来可根据：

- 材料缺口
- 当前完成度
- 当日可刷材料
- 体力
- 周本限制
- 路线冷却

自动规划。

第一版不实现自动规划评分算法。

---

# 16. 单角色顺序模式

用户要求：

> 先把这个角色的材料补充完整，再进行下个角色的升级。

因此默认：

```text
completion_mode = sequential
```

流程：

```text
角色A
↓
角色等级
↓
材料不足？
├─ YES → BetterGI 获取材料
└─ NO
↓
执行角色等级养成
↓
武器等级
↓
材料不足？
├─ YES → BetterGI 获取材料
└─ NO
↓
执行武器等级养成
↓
天赋
↓
材料不足？
├─ YES → BetterGI 获取材料
└─ NO
↓
执行天赋养成
↓
角色A全部完成
↓
角色B
```

不能变成：

```text
所有角色先刷经验书
↓
所有角色刷武器材料
↓
所有角色最后刷天赋
```

除非未来增加新的并行规划模式。

---

# 17. 多角色材料共享池

需要考虑多个角色共享同一种材料。

例如：

```text
角色A需要：
168

角色B需要：
168

当前库存：
10
```

总需求：

```text
336
```

真实缺口：

```text
326
```

不能重复规划：

```text
角色A → 158
角色B → 158
```

然后让两个任务系统各自独立计算。

应该建立：

```text
material_pool
```

概念：

```text
真实库存
+
本轮规划中的材料分配
```

注意：

> “规划中预计获取的数量”不能写回真实库存。

---

# 18. 材料规划器

核心输入：

```text
角色当前状态
角色目标
米游社当前库存
米游社养成计算结果
角色优先级
养成阶段优先级
BetterGI 可执行路线
路线冷却
```

输出：

```json
{
  "character": "角色A",
  "phase": "character_level",

  "materials": [
    {
      "name": "经验书",
      "required": 50,
      "owned": 20,
      "missing": 30
    }
  ],

  "tasks": [
    {
      "type": "domain",
      "material": "经验书",
      "amount": 30
    }
  ]
}
```

---

# 19. BetterGI 执行后的重新同步

这是系统的核心机制：

```text
规划
↓
米游社同步
↓
计算材料缺口
↓
生成 BetterGI 任务
↓
BetterGI 执行
↓
任务结束
↓
重新调用米游社
↓
重新计算
↓
还有缺口？
├─ YES → 下一轮
└─ NO → 当前阶段完成
```

永远不要：

```text
BetterGI 路线预计获得数量
↓
直接修改库存
```

---

# 20. 养成状态机

```text
IDLE
 │
 ▼
SYNCING
 │
 ▼
PLANNING
 │
 ▼
WAIT_CONFIRM
 │
 ▼
EXECUTING
 │
 ▼
WAIT_BGI
 │
 ▼
RESYNC
 │
 ▼
REPLANNING
 │
 ├──────────────┐
 │              │
还有缺口         已完成
 │              │
 ▼              ▼
REPLANNING      NEXT_CHARACTER
 │
 ▼
WAIT_CONFIRM
```

全部角色完成：

```text
COMPLETE
```

---

# 21. Agent 工具

LLM 不允许直接操作数据库。

提供以下工具：

```text
mys_sync_inventory()
```

作用：

```text
调用米游社
↓
获取最新库存
↓
保存 snapshot
↓
返回同步状态
```

---

```text
get_inventory()
```

返回：

```text
当前库存
最后同步时间
数据是否过期
```

---

```text
get_growth_target(character_id)
```

获取指定角色目标。

---

```text
set_growth_target(...)
```

修改角色目标。

---

```text
calculate_growth_requirements(character_id)
```

调用米游社养成计算器。

---

```text
get_growth_plan()
```

获取当前养成计划。

---

```text
refresh_growth_plan()
```

重新计算材料缺口和 BetterGI 任务。

---

```text
execute_growth_plan()
```

执行当前可执行 BetterGI 任务。

---

# 22. Agent 用户指令示例

用户：

```text
把角色A拉到90，天赋10/10/10，武器90
```

Agent：

```text
理解用户目标
↓
set_growth_target()
↓
mys_sync_inventory()
↓
calculate_growth_requirements()
↓
refresh_growth_plan()
↓
生成 BetterGI 任务
```

用户：

```text
把角色A的武器优先于天赋
```

Agent：

```text
set_growth_target(
    weapon_priority=2,
    talent_priority=3
)
```

---

# 23. Studio API

新增：

```text
GET  /api/growth
POST /api/growth/sync

GET  /api/growth/characters

GET  /api/growth/targets
POST /api/growth/targets
PUT  /api/growth/targets/<id>
DELETE /api/growth/targets/<id>

POST /api/growth/plan
GET  /api/growth/plan

POST /api/growth/plan/execute

GET /api/growth/history
```

---

# 24. 数据库

建议新增：

```text
memory/growth.db
```

使用 SQLite。

原因：

角色养成涉及：

```text
角色
角色目标
武器
材料
库存
需求
规划
任务
执行历史
同步历史
```

关系复杂度已经超过适合全部使用 JSON 的范围。

---

# 25. 数据库表

## characters

```text
id
character_id
name
rarity
element
```

## weapons

```text
id
weapon_id
name
rarity
type
```

## growth_targets

```text
id
character_id
enabled
priority

level_target

normal_target
skill_target
burst_target

weapon_enabled
weapon_id
weapon_level_target

level_priority
weapon_priority
talent_priority

completion_mode

created_at
updated_at
```

## inventory_snapshots

```text
id
uid
fetched_at
source
success
raw_json_path
```

## inventory_items

```text
snapshot_id
item_id
name
category
count
```

## material_requirements

```text
target_id
item_id
item_name
required
owned
missing
```

## plans

```text
id
created_at
status
current_character_id
current_phase
```

## plan_tasks

```text
plan_id
character_id
task_type
material
required
missing
status
```

## execution_history

```text
id
plan_id
started_at
finished_at
status
result
```

## sync_history

```text
id
uid
started_at
finished_at
success
error
snapshot_id
```

---

# 26. 推荐目录结构

```text
GI_Agent/
│
├── api/
│   ├── ...
│   └── mys_calculator.py
│
├── brain/
│   ├── ...
│   ├── growth_planner.py
│   ├── growth_models.py
│   └── material_planner.py
│
├── skills/
│   ├── mys_api.py
│   ├── mys_inventory.py
│   ├── mys_calculator.py
│   ├── gather_cooldown.py
│   └── bgi_controller.py
│
├── memory/
│   ├── growth.db
│   ├── mys/
│   │   ├── inventory_latest.json
│   │   └── snapshots/
│   └── ...
│
├── studio/
│   ├── server.py
│   └── web/
│       ├── growth.html
│       ├── growth.js
│       └── growth.css
│
├── tests/
│   ├── test_mys_calculator.py
│   ├── test_growth_planner.py
│   ├── test_inventory.py
│   └── test_growth_api.py
│
└── config.py
```

---

# 27. 与现有 BetterGI 冷却系统结合

现有：

```text
gather_cooldown
```

继续负责：

```text
地区特产
矿物
食材
敌人 / 魔物
```

养成 Planner 不重复实现冷却系统。

直接查询：

```python
gather_cooldown.overview()
```

判断：

```text
材料是否可采
```

然后：

```text
可采
→ 生成 BetterGI 任务

不可采
→ 延后该材料
→ 寻找其它可执行任务
```

---

# 28. 每日养成计划

建议增加 Studio 首页模块：

```text
今日养成计划

━━━━━━━━━━━━━━━━

① 角色A ★5

角色等级
████████░░

今日：
✓ Boss × 6
✓ 摩拉 × 3
○ 经验书 × 2

━━━━━━━━━━━━━━━━

② 角色B ★4

今日：
✓ 天赋秘境 × 4

━━━━━━━━━━━━━━━━

今日预计体力：
520 / 160

[开始执行]
```

---

# 29. 体力规划

建议第二阶段加入。

显示：

```text
角色等级
████████░░

武器
██████░░░░

天赋
████░░░░░░
```

以及：

```text
预计还需要：

摩拉       1,250,000
经验书     48
突破材料   46
天赋材料   38
Boss材料   6
周本材料   8
```

进一步估算：

```text
预计体力：

角色等级：240
武器：160
天赋：320
Boss：180

合计：900
```

注意：

> 体力估算只是规划参考，不作为库存事实来源。

---

# 30. 每日材料开放限制

根据现有项目的星期判断能力：

```text
如果当天材料副本开放：
    可以生成对应任务

如果当天材料副本未开放：
    标记为不可执行
    延后该任务
    寻找其它养成任务
```

例如：

```text
今天无法刷某天赋书

→ 延后天赋任务
→ 转而刷 Boss / 摩拉 / 经验书，如果这些都满足，这个角色只差天赋，但天赋本没开启，则去刷经验书
```

---

# 31. 周本材料

建议加入：

```text
Weekly Material Planner
```

例如：

```text
本周周本材料

材料A
需求：8
已有：3
缺口：5

状态：
本周需要完成周本
```

需要记录：

```text
本周已完成次数
剩余可完成次数
本周材料需求
```

第一版可以只做提醒和计划，不强制自动选择周本。

---

# 32. 计划历史

建议保存：

```text
养成历史

角色A
2026-09-18
角色等级：80 → 90
完成

角色B
2026-09-18
武器：80 → 90
进行中

米游社同步：
成功 12 次
失败 1 次
```

可以用于排查：

```text
为什么材料一直不足？
为什么任务没有继续？
哪一次同步失败？
```

---

# 33. 错误处理

## 米游社 Cookie 失效

```text
米游社同步失败

原因：
Cookie 无效或已过期

请重新配置 MYS_COOKIE。
```

---

## 米游社接口失败

```text
米游社暂时无法同步。

当前使用：
上一次成功快照

最后成功同步：
2026-09-18 14:03
```

必须明确数据不是实时数据。

---

## BetterGI 任务失败

```text
BetterGI 任务失败

任务：
采集材料A

状态：
FAILED

不要修改库存。

下一次重新同步米游社后再规划。
```

---

## 路线不存在

```text
材料A

缺少 BetterGI 可执行路线。

状态：
WAITING_ROUTE

不要让 LLM 猜测路线。
```

---

## 材料处于冷却

```text
材料A
路线冷却中

下一次可执行：
XXXX-XX-XX XX:XX

当前规划自动跳过。
```

---

# 34. 安全与凭据

以下内容禁止进入：

```text
数据库
日志
前端返回值
Git
```

包括：

```text
MYS_COOKIE
完整 Cookie
敏感认证信息
```

Cookie 继续使用现有 `.env` / 配置系统。

如果前端需要显示：

```text
米游社：
● 已配置
```

而不是：

```text
MYS_COOKIE=xxxxxx
```

---

# 35. 测试要求

至少增加：

```text
tests/
├── test_mys_calculator.py
├── test_inventory.py
├── test_growth_planner.py
└── test_growth_api.py
```

必须测试：

1. 81 → 90
2. 80 → 90
3. 90 → 90
4. 天赋 1/1/1 → 10/9/8
5. 武器 80 → 90
6. 角色顺序自定义
7. 5 星默认优先
8. 角色等级 > 武器 > 天赋
9. 天赋 > 角色等级 > 武器
10. 武器 > 天赋 > 角色等级
11. 米游社同步失败
12. Cookie 失效
13. BetterGI 执行失败
14. BetterGI 执行后库存重新同步
15. 两个角色共享同一种材料
16. 当前角色材料不足时不能跳到下一角色
17. 当前角色全部完成后才能进入下一角色
18. 路线处于冷却状态
19. 路线不存在
20. 周本材料不足
21. 快照正确保存
22. 历史快照可以读取
23. 旧库存不会被误认为实时库存

---

# 36. 默认配置

```json
{
  "character_priority": {
    "mode": "default",
    "five_star_first": true
  },

  "completion_mode": "sequential",

  "phase_priority": {
    "character_level": 1,
    "weapon_level": 2,
    "talent": 3
  },

  "startup_inventory_sync": true,

  "post_task_inventory_sync": true,

  "weapon_refinement_planning": false
}
```

---

# 37. 开发阶段

## P0：核心能力

优先级：★★★★★

实现：

- 米游社养成计算器 API 封装
- 米游社库存同步
- 库存快照
- 角色养成目标
- 角色等级目标
- 武器等级目标
- 三项天赋独立目标
- 角色 / 武器 / 天赋三级优先级
- 角色默认 5 星 > 4 星
- 材料缺口计算
- BetterGI 任务生成
- BetterGI 执行后重新同步

---

## P1：Studio UI

优先级：★★★★★

实现：

- 角色养成页面
- 角色列表
- 角色目标编辑
- 角色拖拽排序
- 启用 / 禁用
- 米游社同步状态
- 材料库存
- 材料缺口
- 当前养成阶段
- BetterGI 执行状态
- 养成计划
- 历史记录

---

## P2：日常功能

优先级：★★★★

实现：

- 周本材料
- 每日材料开放限制
- 体力规划
- 今日养成计划
- 材料共享池
- 路线冷却整合
- 计划历史

---

## P3：高级功能

优先级：★★★

实现：

- 武器精炼
- 自动角色排序
- 自动养成规划
- 多角色材料共享优化
- 预计完成时间
- 预计体力消耗
- 跨天任务规划

---

# 38. 开发约束

1. **先后端，后 UI。**
2. 先实现米游社 API 和库存同步，再实现规划器。
3. 再接 BetterGI。
4. 最后实现 Studio 页面。
5. 不要一次性重构整个项目。
6. 保持现有功能继续可运行。
7. 继续使用现有 backup + diff + rollback 机制。
8. 不允许 LLM 自行猜测米游社材料数量。
9. 不允许 BetterGI 直接修改真实库存。
10. 不允许 LLM 直接操作 SQLite。
11. 所有养成需求必须通过统一 `growth_planner` 计算。
12. 所有真实库存必须来自米游社同步。
13. 所有 BetterGI 执行完成后必须重新同步。
14. 米游社 API 发生变化时，只修改 API Adapter 层，避免影响 Planner。
15. 对米游社 API 请求增加超时、重试和错误分类。
16. 对 Cookie 失效和接口限流进行明确提示。
17. 不要把米游社原始 JSON 直接暴露给前端。
18. 前端只拿内部标准化后的数据模型。

---

# 39. 推荐最终用户体验

第一次打开：

```text
GI Agent

↓
米游社同步

✓ 背包同步完成
最后同步：14:03

↓
角色养成

[+ 自动拉取全部角色后按优先级排序]

角色A ★5
优先级：1

目标：
角色 90
武器 90
天赋 10 / 10 / 10

养成顺序：

① 角色
② 武器
③ 天赋

[保存]
```

点击：

```text
开始规划
```

Agent：

```text
正在计算角色A养成需求……

米游社库存：
✓ 已同步

角色等级：
缺少经验书 48
缺少摩拉 1,240,000
缺少突破材料 XX

武器：
缺少 XX

天赋：
缺少 XX

当前可执行：

① Boss
② 经验书
③ 摩拉

[开始执行]
```

BetterGI 执行后：

```text
BetterGI 任务完成

正在重新同步米游社……
```

同步完成：

```text
经验书：
142 → 179

角色等级：
材料仍缺 11

继续执行下一批任务。
```

直到：

```text
角色A目标完成

角色等级：90
天赋：10 / 10 / 10
武器：90

进入下一角色：
角色B ★4
```

---

# 40. 第一版完成标准

以下条件全部满足后，才认为“角色养成系统 V1”完成：

```text
[✓] 可以从米游社同步库存
[✓] 可以保存库存快照
[✓] 可以配置角色
[✓] 可以设置角色优先级
[✓] 默认 5 星 > 4 星
[✓] 可以拖拽角色顺序
[✓] 可以设置角色目标等级
[✓] 支持 81、90 等任意等级
[✓] 可以分别设置三项天赋
[✓] 可以设置武器等级
[✓] 可以设置角色 / 武器 / 天赋优先级
[✓] 默认角色 > 武器 > 天赋
[✓] 可以计算材料缺口
[✓] 可以生成 BetterGI 任务
[✓] BetterGI 不直接修改库存
[✓] BetterGI 完成后自动重新同步
[✓] 材料不足时继续规划
[✓] 当前角色完成后进入下一角色
[✓] 米游社失败有缓存和错误提示
[✓] BetterGI 失败不会污染库存
[✓] 有完整执行历史
[✓] 有自动化测试
```

---

# 41. 核心设计结论

整个系统最终应该形成：

```text
                 用户设置目标
                       │
                       ▼
                ┌─────────────┐
                │ Growth Goal │
                └──────┬──────┘
                       │
                       ▼
                ┌─────────────┐
                │ 米游社同步   │
                └──────┬──────┘
                       │
                       ▼
                ┌─────────────┐
                │ 养成计算器   │
                └──────┬──────┘
                       │
                       ▼
                ┌─────────────┐
                │ 材料缺口计算 │
                └──────┬──────┘
                       │
                       ▼
                ┌─────────────┐
                │ Growth Plan │
                └──────┬──────┘
                       │
                       ▼
                ┌─────────────┐
                │ BetterGI    │
                │ 执行任务     │
                └──────┬──────┘
                       │
                       ▼
                ┌─────────────┐
                │ 米游社重新同步│
                └──────┬──────┘
                       │
                       └──────────→ 重新计算
```

最重要的一条规则：

> **不要相信 BetterGI “捡到了多少”，只相信下一次米游社同步后“实际剩多少”。**

这样整个 GI_Agent 才能真正成为一个“根据账号真实库存自动规划养成并调用 BetterGI 执行”的闭环系统。
