# GI_Agent × genshin-db 养成与材料规划系统架构

## 1. 核心目标

以 `theBowja/genshin-db` 作为 GI_Agent 的静态游戏知识源，米游社作为玩家实时库存的 Source of Truth，BetterGI 作为执行器。

核心职责：

- **genshin-db**：游戏里有什么，以及角色、武器、材料、敌人、天赋等基础知识。
- **米游社**：玩家当前实际拥有多少，以及角色/武器/天赋当前状态。
- **GI_Agent**：计算养成需求、库存缺口、材料族、刷取计划和执行顺序。
- **BetterGI**：执行已经规划好的刷怪/采集路线。

核心原则：

> genshin-db 管“游戏是什么”，米游社管“我有什么”，GI_Agent 管“我还缺什么”，BetterGI 管“去哪里刷、怎么执行”。

---

## 2. 总体架构

```text
                         ┌──────────────────────┐
                         │      genshin-db      │
                         │  静态游戏基础数据源   │
                         └──────────┬───────────┘
                                    │
                             Import / Normalize
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────┐
│                    GI_Agent Game Knowledge                  │
│                                                             │
│  Character       Weapon       Material       Enemy          │
│     │               │            │             │            │
│     └───────────────┴────────────┴─────────────┘            │
│                            │                                │
│                     Material Resolver                       │
│                            │                                │
│             ┌──────────────┴──────────────┐                 │
│             ▼                             ▼                 │
│       Material Family              Enemy Family             │
│       材料族/掉落族                 来源族/敌人族群           │
│             │                             │                 │
│             └──────────────┬──────────────┘                 │
│                            ▼                                │
│                    BetterGI Route DB                        │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
                    Growth Planner
                             │
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
       米游社库存        养成目标          合成关系
       Source of Truth                      │
            │                │                │
            └────────────────┼────────────────┘
                             ▼
                     Material Deficit
                             │
                             ▼
                    BetterGI Task Planner
                             │
                             ▼
                       BetterGI执行
                             │
                             ▼
                      米游社重新同步
```

---

## 3. 三个数据源的职责

### 3.1 genshin-db

负责：

```text
“原神里有什么，以及它们是什么”
```

主要用于：

- Characters
- Weapons
- Materials
- Enemies
- Talents
- Domains
- 其他静态游戏数据

它属于 **Game Knowledge**，不是玩家状态。

### 3.2 米游社

负责：

```text
“玩家现在有什么”
```

包括：

- 材料库存
- 摩拉
- 角色当前等级
- 角色天赋等级
- 武器当前等级
- 其他养成状态

米游社是玩家库存的 **Source of Truth**。

不要使用 BetterGI 的拾取日志作为真实库存。

### 3.3 BetterGI

负责：

```text
“怎么执行”
```

包括：

- BetterGI 路线
- 怪物刷取
- 采集路线
- 执行任务
- 执行历史

BetterGI 不负责判断材料之间的游戏知识关系。

---

# 4. 项目结构

建议新增：

```text
GI_Agent/
│
├── api/
│   ├── mys_calculator.py
│   └── bettergi.py
│
├── brain/
│   ├── growth_planner.py
│   ├── material_planner.py
│   ├── route_planner.py
│   └── execution_planner.py
│
├── knowledge/
│   ├── genshin_db/
│   │   ├── adapter.py
│   │   ├── importer.py
│   │   ├── normalizer.py
│   │   └── version.py
│   │
│   ├── materials/
│   │   ├── resolver.py
│   │   ├── family.py
│   │   └── synthesis.py
│   │
│   ├── enemies/
│   │   └── resolver.py
│   │
│   └── routes/
│       └── repository.py
│
├── skills/
│   ├── mys_inventory.py
│   ├── mys_calculator.py
│   ├── growth.py
│   └── bettergi.py
│
├── memory/
│   ├── growth.db
│   └── game_knowledge.db
│
└── config/
    ├── growth.json
    └── bettergi_routes.json
```

---

# 5. game_knowledge.db

用于保存静态游戏知识及 GI_Agent 标准化后的关联关系。

建议表：

```text
characters
weapons
materials
enemies
domains

material_families
material_family_members

enemy_families
enemy_family_members

material_sources
material_synthesis

bettergi_routes
bettergi_route_sources
```

关系：

```text
characters
    │
    ├── character_materials
    │
    └── talent_materials
             │
             ▼
        materials
             │
             ▼
     material_families
             │
             ▼
      enemy_families
             │
             ▼
          enemies
             │
             ▼
      bettergi_routes
```

---

# 6. Material Family：材料族

这是整个系统的核心抽象。

不要简单做：

```text
材料 → BetterGI路线
```

而应做：

```text
材料
 ↓
Material Family
 ↓
Enemy / Source Family
 ↓
BetterGI Route Pool
```

例如：

```text
异海凝珠 ─┐
异海之块 ─┼──→ 原海异种材料族
异色结晶石 ─┘
```

然后：

```text
原海异种材料族
        ↓
     原海异种
        ↓
┌───────┼────────┐
↓       ↓        ↓
重甲蟹  膨膨兽  泡泡海马
                 ...
```

这样多个品质的共同掉落材料不会被错误拆成多个独立刷取任务。

---

# 7. Material Family 数据结构

建议：

```json
{
  "family_id": "fontaine_primordial_sea_creatures",
  "name": "原海异种材料",
  "type": "enemy_drop"
}
```

成员关系：

```text
material_families
        │
        ├── 异海凝珠
        ├── 异海之块
        └── 异色结晶石
```

数据库：

```sql
CREATE TABLE material_families (
    family_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL
);

CREATE TABLE material_family_members (
    family_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    PRIMARY KEY (family_id, material_id)
);
```

---

# 8. Enemy Family：敌人族群

建议建立：

```text
enemy_families
```

例如：

```json
{
  "family_id": "fontaine_primordial_sea_creatures",
  "name": "原海异种"
}
```

成员：

```text
重甲蟹
膨膨兽
泡泡海马
球球章鱼
...
```

数据库：

```sql
CREATE TABLE enemy_families (
    family_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE enemy_family_members (
    family_id TEXT NOT NULL,
    enemy_id TEXT NOT NULL,
    PRIMARY KEY (family_id, enemy_id)
);
```

---

# 9. genshin-db Importer

不要让业务逻辑直接依赖 genshin-db 原始 JSON。

采用：

```text
genshin-db
    ↓
GenshinDBImporter
    ↓
GenshinDBAdapter
    ↓
Normalizer
    ↓
GI_Agent Standard Model
    ↓
game_knowledge.db
```

示例：

```python
class GenshinDBImporter:

    def update(self):
        version = self.get_version()

        if version == self.local_version:
            return

        data = self.load_genshin_db()

        self.import_characters(data)
        self.import_weapons(data)
        self.import_materials(data)
        self.import_enemies(data)
        self.import_domains(data)

        self.normalize()
        self.save_version(version)
```

不要让 Planner 直接依赖 genshin-db 的原始字段名，而应该通过 `GenshinDBAdapter` 转成 GI_Agent 自己的数据模型。

---

# 10. GI_Agent Standard Model

建议定义：

```python
@dataclass
class Material:
    id: str
    name: str
    rarity: int | None
    category: str | None
```

```python
@dataclass
class Enemy:
    id: str
    name: str
    family_id: str | None
    drops: list[str]
```

```python
@dataclass
class MaterialFamily:
    id: str
    name: str
    material_ids: list[str]
    enemy_family_id: str | None
```

```python
@dataclass
class EnemyFamily:
    id: str
    name: str
    enemy_ids: list[str]
```

这样 genshin-db 数据格式变化时，只需要修改 `knowledge/genshin_db/`。

---

# 11. 材料解析流程

错误方案：

```text
缺异海凝珠
    ↓
找异海凝珠路线

缺异海之块
    ↓
找异海之块路线

缺异色结晶石
    ↓
找异色结晶石路线
```

正确方案：

```text
缺异海凝珠
缺异海之块
缺异色结晶石
       ↓
Material Resolver
       ↓
共同 Material Family
       ↓
原海异种
       ↓
BetterGI Route Pool
```

核心 API：

```python
resolve_material_family(material_id)
get_family_materials(family_id)
get_family_enemies(family_id)
get_routes_by_family(family_id)
calculate_family_deficit(family_id, inventory, requirements)
```

---

# 12. Material Planner

建议：

```python
def build_material_plan(requirements, inventory):

    deficits = calculate_deficits(
        requirements,
        inventory
    )

    families = resolve_families(deficits)

    tasks = []

    for family in families:
        task = build_family_task(
            family=family,
            deficits=family.deficits
        )

        tasks.append(task)

    return tasks
```

输入：

```json
{
  "异海凝珠": 120,
  "异海之块": 30,
  "异色结晶石": 8
}
```

转换成：

```json
{
  "task_type": "FARM_MATERIAL_FAMILY",
  "family_id": "fontaine_primordial_sea_creatures",
  "missing": {
    "异海凝珠": 120,
    "异海之块": 30,
    "异色结晶石": 8
  }
}
```

BetterGI Planner 最终只需要处理一个“原海异种材料族”任务。

---

# 13. BetterGI Route 数据结构

不要：

```json
{
  "route": "xxx",
  "materials": ["异海凝珠"]
}
```

推荐：

```json
{
  "route_id": "fontaine_sea_01",
  "name": "枫丹原海异种路线 01",
  "source_family_id": "fontaine_primordial_sea_creatures",
  "monsters": [
    "重甲蟹",
    "膨膨兽",
    "泡泡海马"
  ],
  "enabled": true
}
```

路线通过 `source_family_id` 与材料族关联。

因此：

```text
异海凝珠
异海之块
异色结晶石
```

都能找到：

```text
fontaine_primordial_sea_creatures
```

对应的 BetterGI 路线池。

---

# 14. 材料合成系统

养成材料存在等级转换关系时，Planner 必须考虑可转换库存。

建立：

```text
material_synthesis
```

例如：

```json
{
  "input_material": "异海凝珠",
  "input_count": 3,
  "output_material": "异海之块",
  "output_count": 1
}
```

以及：

```json
{
  "input_material": "异海之块",
  "input_count": 3,
  "output_material": "异色结晶石",
  "output_count": 1
}
```

Planner：

```text
当前库存
    ↓
可合成数量
    ↓
有效库存
    ↓
实际缺口
    ↓
需要刷取的数量
```

第一版建议：

```text
[✓] 计算合成后的有效库存
[ ] 自动执行合成
```

---

# 15. 角色养成 Planner

角色目标：

```text
角色 Lv 90
普通攻击 1
元素战技 10
元素爆发 10
```

流程：

```text
CharacterTarget
      ↓
GrowthCalculator
      ↓
RequiredMaterials
      ↓
MiHoYo Inventory
      ↓
Material Deficit
```

结果：

```json
{
  "character": "xxx",
  "requirements": [
    {
      "material_id": "xxx",
      "required": 46,
      "owned": 12,
      "deficit": 34
    }
  ]
}
```

然后进入 Material Family Resolver。

---

# 16. 完整执行流程

```text
Studio 启动
    ↓
检查 genshin-db 版本
    ↓
必要时更新 Game Knowledge
    ↓
米游社库存同步
    ↓
读取角色养成目标
    ↓
计算角色/武器/天赋需求
    ↓
计算库存缺口
    ↓
Material Resolver
    ↓
Material Family 合并
    ↓
Synthesis Resolver
    ↓
BetterGI Route Planner
    ↓
生成执行计划
    ↓
执行 BetterGI
    ↓
执行完成
    ↓
米游社重新同步
    ↓
重新计算缺口
    ↓
仍有缺口？
 ┌──┴──┐
是    否
│      │
↓      ↓
继续   下一阶段/
刷取   下一角色
```

---

# 17. 与角色优先级系统结合

角色默认：

```text
5星角色
    ↓
4星角色
```

用户可以手动覆盖：

```text
角色 A
角色 C
角色 B
```

每个角色独立配置：

```text
角色等级：1～90
普通攻击：1～10
元素战技：1～10
元素爆发：1～10

武器：
启用/禁用
等级：1～90
```

内部阶段默认：

```text
角色等级
    >
武器等级
    >
天赋
```

允许用户修改。

---

# 18. Material Family 与角色养成

例如：

```text
角色 A
需要：
异海凝珠
异海之块
异色结晶石
```

Planner：

```text
角色 A
  ↓
需求计算
  ↓
材料缺口
  ↓
┌────────────────────────┐
│ 原海异种材料族          │
│                        │
│ 异海凝珠 ×120          │
│ 异海之块 ×30           │
│ 异色结晶石 ×8          │
└────────────────────────┘
  ↓
原海异种
  ↓
BetterGI Routes
```

最终生成一个统一刷怪任务，而不是三个任务。

---

# 19. API / Skill

建议增加：

```text
mys_sync_inventory()
get_inventory()

get_growth_target(character_id)
set_growth_target(...)

calculate_growth_requirements(character_id)

resolve_material_family(material_id)
get_family_materials(family_id)
get_family_enemies(family_id)
get_routes_by_family(family_id)

calculate_family_deficit(family_id)

get_growth_plan()
refresh_growth_plan()

execute_growth_plan()
```

---

# 20. Agent 状态机

```text
IDLE
  ↓
SYNCING
  ↓
PLANNING
  ↓
WAIT_CONFIRM
  ↓
EXECUTING
  ↓
WAIT_BGI
  ↓
RESYNC
  ↓
REPLANNING
  ↓
┌───────────────┐
│               │
NEXT_CHARACTER  COMPLETE
```

BetterGI 失败：

```text
WAIT_BGI
   ↓
EXECUTION_FAILED
   ↓
记录 execution_history
   ↓
重新规划
```

---

# 21. 版本管理

必须记录：

```text
game_version
genshin_db_version
import_time
schema_version
```

例如：

```text
game_knowledge_meta

game_version = "6.x"
genshin_db_version = "xxxxx"
schema_version = 1
import_time = ...
```

更新流程：

```text
下载新版本
   ↓
Adapter
   ↓
Normalizer
   ↓
Schema Validation
   ↓
Diff
   ↓
写入 game_knowledge.db
```

建议保留上一版本，异常时可以回滚。

---

# 22. 数据覆盖原则

优先级：

```text
genshin-db
    ↓
GI_Agent Standard Knowledge
    ↓
GI_Agent Override
```

不要修改原始 genshin-db 数据。

如果某个材料族无法自动确定，可在：

```text
config/
└── knowledge_overrides.json
```

中补充。

例如：

```json
{
  "material_family_overrides": [
    {
      "family_id": "fontaine_primordial_sea_creatures",
      "materials": [
        "xxx",
        "xxx",
        "xxx"
      ]
    }
  ]
}
```

这样自动数据和人工修正可以共存。

---

# 23. BetterGI 路线与游戏知识解耦

必须保持：

```text
游戏知识
    ≠
BetterGI 路线
```

游戏知识：

```text
genshin-db
    ↓
原海异种
    ↓
怪物/掉落关系
```

执行路线：

```text
BetterGI
    ↓
原海异种路线 01
原海异种路线 02
原海异种路线 03
```

BetterGI 路线只是：

```text
source_family_id
```

的执行实现。

未来更换执行器时：

```text
Game Knowledge
       │
       ├── BetterGI
       ├── 手动路线
       └── 其他执行器
```

无需修改游戏知识层。

---

# 24. 数据职责总表

| 数据 | 来源 | 用途 |
|---|---|---|
| 角色 | genshin-db | 角色基础资料 |
| 武器 | genshin-db | 武器基础资料 |
| 材料 | genshin-db | 材料 ID、名称、等级 |
| 敌人 | genshin-db | 敌人基础数据 |
| 天赋 | genshin-db | 天赋养成关系 |
| 材料族 | GI_Agent 标准化层 | 合并共同掉落材料 |
| 敌人族群 | GI_Agent 标准化层 | 材料与敌人的中间层 |
| 合成关系 | GI_Agent 标准化层 | 计算有效库存 |
| 玩家库存 | 米游社 | 真实库存 |
| 养成目标 | GI_Agent | 用户配置 |
| BetterGI 路线 | GI_Agent | 实际刷取路径 |
| 执行结果 | GI_Agent | 历史记录 |

---

# 25. 第一阶段开发顺序

## Phase 1：genshin-db 接入

实现：

```text
GenshinDBImporter
GenshinDBAdapter
Normalizer
VersionManager
```

完成：

```text
角色
武器
材料
敌人
天赋
```

导入 `game_knowledge.db`。

## Phase 2：Material Family

实现：

```text
material_families
material_family_members
enemy_families
enemy_family_members
```

完成：

```text
Material
    ↓
MaterialFamily
    ↓
EnemyFamily
    ↓
Enemy
```

## Phase 3：合成系统

实现：

```text
material_synthesis
```

Planner 计算：

```text
库存 + 可合成材料
```

得到有效库存。

## Phase 4：BetterGI Route

将现有 BetterGI 路线改成：

```text
route
    ↓
source_family_id
```

而不是：

```text
route
    ↓
material_id
```

## Phase 5：Growth Planner

接入：

```text
角色目标
武器目标
天赋目标
米游社库存
```

自动计算缺口。

## Phase 6：自动执行

```text
缺口
 ↓
Material Family
 ↓
BetterGI Route Pool
 ↓
执行
 ↓
米游社同步
 ↓
重新规划
```

---

# 26. 最终核心模型

```text
                    ┌──────────────┐
                    │  Character   │
                    └──────┬───────┘
                           │
                     requires
                           ↓
                    ┌──────────────┐
                    │   Material   │
                    └──────┬───────┘
                           │
                    belongs_to
                           ↓
                  ┌──────────────────┐
                  │ Material Family  │
                  └────────┬─────────┘
                           │
                     dropped_by
                           ↓
                  ┌──────────────────┐
                  │  Enemy Family    │
                  └────────┬─────────┘
                           │
                         has
                           ↓
                  ┌──────────────────┐
                  │      Enemy       │
                  └────────┬─────────┘
                           │
                     farmable_by
                           ↓
                  ┌──────────────────┐
                  │ BetterGI Route   │
                  └──────────────────┘
```

玩家状态从旁边接入：

```text
              MiHoYo Inventory
                     │
                     ▼
               Current State
                     │
                     ▼
Material Requirement ──→ Deficit
                     │
                     ▼
               Material Family
                     │
                     ▼
               BetterGI Plan
```

这套结构解决“同一怪物族群掉落多个材料等级，却被系统拆成多个独立刷取任务”的问题，同时避免 GI_Agent 被 genshin-db 或 BetterGI 的具体数据格式绑定。
