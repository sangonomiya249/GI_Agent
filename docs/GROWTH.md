# 角色养成：米游社算需求，BetterGI 只负责跑

> 一句话：**材料需求问米游社养成计算器，真实库存问米游社背包接口，BetterGI 只管执行。**
> 它捡到多少从来不写回库存 —— 库存只在"下一次米游社同步"之后变化。

---

## 1. 它解决什么问题

以前要回答"胡桃拉到 90 还差什么"，只能靠模型翻展柜 + 本地百科字典估算，
换个世界等级、换个天赋方案就不准了。现在这条链路是确定性的：

```text
角色目标（你设的 90 / 10-9-8 / 武器 90）
        │
        ▼
米游社养成计算器  ──►  还差哪些材料、各多少（**唯一的需求来源**）
        │
        ▼
米游社背包接口    ──►  现在真实有多少（**唯一的库存来源**）
        │
        ▼
材料缺口 + BetterGI 任务（共享池扣减，不重复规划）
        │
        ▼
BetterGI 执行（采路线 / 打秘境 / 刷地脉 / 讨伐 Boss）
        │
        ▼
任务结束 ──► 重新同步米游社 ──► 重新算缺口（闭环）
```

三条硬规矩（都在代码注释里写明了原因）：

1. **BetterGI 不许改库存。** 它不知道自己捡了几个，本地 +50 只会越攒越离谱。
2. **代码不许硬编码材料需求表。** 需求表会随游戏版本过期，而过期的表现是"悄悄地规划错"。
3. **旧快照不许冒充实时数据。** 库存超过 12 小时没同步，界面必须标出来。

---

## 2. 三分钟上手

### ① 配米游社 Cookie

**最省事：Studio → 配置 → 「米游社个人战绩」卡 → 点「扫码登录（推荐）」。**
二维码会**直接画在那张卡上** → 手机「米游社 App」扫一扫 → 手机上点确认 →
程序自动换 v1 `ltoken`、验证、写进 `.env`
（**这样拿到的 cookie 一定带 `ltoken`**，而计算器与战绩接口都需要它）。
二维码只活两分钟左右，过期会自动换一张。

命令行等价：`python -m skills.mys_login --qr`（终端里也会把二维码画出来）。

也可以手工填 `MYS_COOKIE`（怎么拿见 [`docs/MYS_COOKIE.md`](MYS_COOKIE.md)）。
⚠️ 手工复制**很容易**只拿到 `*_v2` 那一套 —— 它能列出角色（看起来配好了），
但养成计算器会一直报未登录、算材料返回空。缺什么可以自查：

```bash
python -m skills.mys_api --audit     # 不发请求，直接告诉你 cookie 缺什么
```

> Cookie 等于账号凭据：只存在你自己的 `.env` 里，日志里永远只出现掩码，
> 而且**不会**发往米游社以外的任何域名。

### ② 打开「角色养成」页

Studio 左边栏 → **角色养成**。第一次打开会按 `GROWTH_STARTUP_SYNC=1` 自动同步一次背包。

### ③ 添加角色并设目标

点 **添加角色** → 列出你账号里的全部角色（等级 / 天赋 / 武器都读自米游社）
→ 点一个就建好默认目标（角色 88、天赋 1/9/9、武器 90）→ 在列表里点 **编辑** 改。

- **角色等级支持 1~90 的任意值**：想卡 81 就填 81（这是规格书明确要求的，不是只能选 80/90）；
- **三个天赋各自独立**：`10/9/8`、`1/10/10` 都行；
- **武器第一版只规划等级**，不含精炼（第二阶段再加）。

### ④ 开始规划 / 开始执行

- **重新计算缺口**：可选先同步库存，然后重算所有角色的材料缺口；
- **开始执行**：把**当前角色**的可执行任务下发给 BetterGI（会走原有的配置事务 + 备份 + 可回滚）；
- 勾上 **只写配置（不启动）** 相当于只改 BetterGI 配置、不真的启动（和 CLI 里回 `t` 一样）。

---

## 3. 界面上的每个数字是什么

| 位置 | 含义 | 数据来源 |
| --- | --- | --- |
| 米游社库存 · 最后同步 | 这份库存是什么时候拉的 | `memory/mys/inventory_latest.json` |
| 米游社库存 · **非实时** | 超过 `GROWTH_INVENTORY_STALE_HOURS`（默认 12 小时）没同步 | 同上，时间戳算出来的 |
| 当前养成计划 | 状态机在哪一步（等待确认 / 受阻 / 全部完成…） | `brain/growth_planner.build_plan()` |
| 任务表的「缺口」 | 需求 − 库存（**共享池**：先给排在前面的角色扣） | 养成计算器 + 库存 |
| 任务表的「来源」 | 秘境（带开放日程）/ 地脉花 / 采集路线 / Boss | 本地百科日程 + 现有路线索引 |
| 今日预计体力 | 仅供规划参考，**不是**库存依据 | 按任务类型的固定估算 |
| 角色列表的「素材缺口」 | 这个角色还差多少件材料（不含已齐的） | 同上 |
| 同步与执行历史 | 成功/失败次数、每次同步的快照号、执行记录 | `memory/growth.db` |

**受阻原因**会单独列出来 —— 这是玩家最常问的"为什么没得跑"：
秘境今天不开 / 路线还在冷却 / 本地没有这种材料的路线 / 米游社没同步 / 需求算不出来。

---

## 4. 执行顺序（为什么不会"所有角色先刷经验书"）

```text
角色 A（按你的顺序）
  ├─ 角色等级  ── 材料不足？→ BetterGI 去拿 → 重新同步 → 再算
  ├─ 武器等级  ── 同上
  └─ 天赋      ── 同上
角色 A 三段全达标
  ↓
角色 B
```

- 角色顺序默认 **5 星 → 4 星**，你可以拖拽覆盖（拖完自动切成「自定义」模式）；
- 每个角色内部的顺序默认 **角色等级 1 > 武器等级 2 > 天赋 3**，可以改成任意排列；
- **当前角色的材料今天拿不到**（秘境未开放 / 路线冷却 / 缺路线）时，本轮会改做
  后面角色的可执行材料（`execution.fallback`），但**当前角色不会被跳过** ——
  它可执行了立刻回来接着做。

---

## 5. 配置项

| `.env` 键 | 默认 | 说明 |
| --- | --- | --- |
| `GROWTH_STARTUP_SYNC` | `1` | 启动 Studio / Agent 时自动同步一次库存 |
| `GROWTH_SYNC_MODE` | `startup` | `startup` / `30`（分钟）/ `120` / `manual` |
| `GROWTH_POST_TASK_SYNC` | `1` | **任务跑完自动重新同步 + 重算缺口**（核心机制，别关） |
| `GROWTH_INVENTORY_STALE_HOURS` | `12` | 超过多久算"不是实时数据" |
| `GROWTH_COMPUTE_MAX_PER_PLAN` | `8` | 一次规划最多问米游社算几个角色（1 角色 = 1 请求） |
| `GROWTH_COMPUTE_CACHE_MINUTES` | `30` | 同一份目标的材料需求缓存多久 |
| `MYS_CALC_HOST` | 空 | 计算器域名（国服默认 `api-takumi.mihoyo.com`） |
| `MYS_CALC_PREFIX` | 空 | 接口前缀（默认 `/event/e20200928calculate`） |
| `MYS_CALC_PATH_AVATAR_LIST` / `_COMPUTE` / `_MY_ITEMS` | 空 | 三条接口路径（默认 `/v1/avatar/list`、`/v2/compute`、`/v1/sync/avatar/list`） |
| `GROWTH_DB_PATH` | `memory/growth.db` | 养成数据库位置 |
| `GROWTH_MYS_DIR` | `memory/mys` | 库存快照与米游社原始响应目录 |
| `MYS_LOGIN_PROFILE_DIR` | 空 | **老兜底路径**用的独立浏览器配置目录（默认 `.studio-profile\mys-login`）；扫码登录那条路不需要它 |

这些在 Studio 的 **配置 → 角色养成（养成计算器）** 里都能改（保存后重启生效）。

---

## 6. 出问题怎么排查

**第一步永远是自检**，它会依次试"账号角色 / 背包 / 一次真实算材料"，
哪一步先炸，问题就在那一层：

```bash
python -m skills.mys_calculator --check
```

```bash
# 先单独确认接口通不通（图鉴接口**不需要 cookie**，最适合拿来判断"是网络问题还是登录问题"）
python -m skills.mys_calculator --catalog
# 只同步一次库存（等价于点界面上的「同步米游社库存」）
python -m skills.mys_inventory --sync
# 看当前养成计划
python -m brain.growth_tools --plan
# 查某种材料现在有多少
python -m brain.growth_tools --inventory 清心
```

**接口到底在哪**（米游社改版时用）：下面这几条都是**实测确认过的**（HTTP 200 + `retcode 0`），
统一挂在 `https://api-takumi.mihoyo.com` 的 `/event/e20200928calculate/` 命名空间下：

| 用途 | 方法 + 路径 | 登录要求 |
| --- | --- | --- |
| ★ **算材料**（等级 / 天赋 / 武器三个桶） | `POST /v2/compute` | **需要** |
| ★ **单个角色的真实状态**（等级/突破/天赋/武器/圣遗物） | `GET /v1/sync/avatar/detail?avatar_id=` | **需要** |
| 全量角色图鉴（含天赋 id 与槽位名） | `POST /v1/avatar/list`（`is_all` 才全量） | 不需要 |
| 养成方案（**只有天赋**材料，作兜底） | `GET /v1/avatar_cultivation/detail?avatar_id=` | **需要** |
| ~~账号里拥有的角色 + 背包~~ | ~~`POST /v1/sync/avatar/list`~~ | **已废**：带 cookie 也回 `-100` |

> ⚠️⚠️ **2026-09 踩过的三个坑（写下来省得再踩）**
> 1. **算材料的请求体不能包 `{"data": ...}` 外壳**（最贵的一个）。
>    米游社计算器前端是这样调的：`batchCost({data: a, headers: {...}})` ——
>    那个 `{data, headers}` 是 **axios 的 config**，不是 HTTP body；真正发出去的就是 `a`（平的）。
>    包了外壳的表现是 **`retcode 0` + 三个空 consume 桶**：不报错，但一个材料都算不出来。
>    （我们一度据此误判成"这个接口废了"，其实是自己的请求形状错了。）
> 2. **字段名**：等级是 `avatar_level_current` / `avatar_level_target`（不是 `level_current`）；
>    天赋条目的 id 用 **`group_id`**，字段名是 `id`（不是 `skill_id`）。写错同样是"静默空桶"。
> 3. `/v1/sync/avatar/list`（"我的角色 / 背包"）**是真的废了**：cookie 换成纯 v1 / 纯 v2 /
>    两者都带、加 `x-rpc-ltoken` 头、`uid`/`region` 放 query 或 body……全都回
>    `-100 请先登录后参与活动`。所以**"背包里已有的材料数量"拿不到**（见第 10 节）。
>    但"有哪些角色 / 这个角色几级"可以从 `/v1/sync/avatar/detail` 一个个问出来 ——
>    没有的角色它会明确回 `-1 伙伴不存在`。
> 4. **图鉴是分页的**：`POST /v1/avatar/list` 不带 `is_all` 只回第一页 10 个（`total` 才是 118）。

万一又改版了，别猜，让它自己去试：

```bash
python -m skills.mys_calculator --probe     # 用你的 cookie 把候选路径挨个试一遍
```

| 现象 | 多半是 | 怎么办 |
| --- | --- | --- |
| `404` + 正文 `404 page not found` | 路径/域名变了 | `--probe` 探出正确路径，填进 `.env` 的 `MYS_CALC_PATH_*` |
| `没有配置米游社 Cookie` | 功能没开 | 点 Studio 的「扫码登录」，或填 `MYS_COOKIE` |
| `Cookie 无效或已过期`（-100/-111） | cookie 过期或不全 | 重新扫码登录一次 |
| **「米游社关了背包接口」**（`error_kind=backpack_unavailable`） | 不是你的问题：`/v1/sync/avatar/list` 真被关了 | 不用重新扫码。材料**需求**照旧算得出来，只是缺口按总需求算 |
| **算出来 0 个材料**（不报错） | 请求形状/参数不对（见上面第 1、2 条） | 跑 `python -m skills.mys_calculator --check`，它会把请求体打出来对照 |
| `retcode -100 请先登录米游社`（战绩那步） | cookie 里没有 `ltoken`（只有 v2） | **扫码登录**一次，或见下面那条"最常踩的坑" |
| `触发米游社风控`（10001）/ 战绩 `账号数据异常` | 请求太频繁 / 要验证码 | 打开米游社 App 点两下完成验证，过几小时再试 |
| 任务表全是「今日未开放」 | 秘境开放日程没对上 | 周日（或全开日）三档全开，可以周日再跑 |
| 任务表出现「缺少路线」 | BetterGI 里没有这种材料的路线 | 在 BetterGI 里订阅对应路线组（`python -m skills.gather_cooldown --materials` 可以列清单） |
| 库存一直显示「非实时」 | 背包数据拿不到（见上） | 缺口会按「总需求」算；这是当前能做到的最老实算法 |

> ⚠️ **最常踩的一个坑：cookie 里只有 `*_v2`**。
> 现在的米游社网页登录只发 `ltoken_v2` / `account_id_v2` / `ltuid_v2` /
> `cookie_token_v2` 这一套。它能通过"列出你名下角色"这种公共接口，
> 所以**看起来一切都配好了**；但需要登录的接口会全部失败
> （`/v1/sync/avatar/list` 报 `-100`、`/v2/compute` **静默返回空清单**、战绩 `character/list` 报 `5003`）。
> `ltoken_v2` **不能**当 `ltoken` 用（试过补成 v1 键名、放进 `x-rpc-ltoken` 头、走
> `getActionTicketByCookieToken` / `getWebTokensByAuthKey`，都不行）。
>
> **解法就是扫码登录**（Studio 配置页那个按钮 / `python -m skills.mys_login --qr`）——
> 它走的是米游社自己的扫码登录接口、直接换出 v1 `ltoken`，
> 不读浏览器 cookie，所以拿到的 cookie 一定带 `ltoken`。
> 程序已经针对这种情况做了防线：空清单会被明确报成"算不出来"，
> **不会**被当成"材料已齐"。

> ⚠️ 签名（DS）用的 `salt` 跟米游社 App 版本走。哪天突然**全部**接口都失败，
> 按 [`docs/MYS_COOKIE.md`](MYS_COOKIE.md) 第 5 节换 `MYS_SALT` / `MYS_APP_VERSION` 即可，不用改代码。

---

## 7. 数据都在哪

```text
memory/
├── growth.db                      # SQLite：角色 / 目标 / 计划 / 任务 / 同步与执行历史
└── mys/
    ├── inventory_latest.json      # 当前库存（权威副本，人可读、可 diff、坏了可手工修）
    ├── snapshots/
    │   └── inventory_20260918_140312.json   # 每次同步的历史快照（保留最近 200 份）
    └── calculator/
        ├── avatar_list.json       # 养成计算器的角色列表原始响应
        ├── my_items.json          # 背包原始响应
        └── 10000089_80_to_90.json # 每次 compute 的原始响应（排查接口改版的第一现场）
```

**对比两次同步就知道 BetterGI 实际刷到了多少**：养成页「本次同步的变化」会列出
`清心 20 → 87（+67）`，也可以在 `python -m brain.growth_tools --plan` 的输出里看。

---

## 8. Agent 工具（规格书 §21）

LLM **不允许**直接碰数据库，也不允许自己算材料 —— 它只能调这些函数
（`brain/growth_tools.py`，都在 `python -m brain.growth_tools` 里有对应的自检入口）：

| 工具 | 作用 |
| --- | --- |
| `mys_sync_inventory()` | 调米游社 → 存快照 → 返回同步状态 |
| `get_inventory(材料名…)` | 当前库存 / 最后同步时间 / 是否过期 |
| `get_growth_target(角色)` | 取指定角色的养成目标 |
| `set_growth_target(角色, 等级=…)` | 修改角色目标 |
| `calculate_growth_requirements(角色)` | 调米游社养成计算器算材料需求 |
| `get_growth_plan()` | 当前养成计划（唯一口径） |
| `refresh_growth_plan()` | 重新同步 + 重算缺口 |
| `execute_growth_plan()` | 下发可执行的 BetterGI 任务 |

另外 `growth_context_block()` 会把这些事实拼成一段可以直接塞进 prompt 的文本
（形状对齐现有的【展柜外角色参考】），里面明确写着"数量全部来自米游社，不要自己估算"。

---

## 9. 实现位置

| 文件 | 作用 |
| --- | --- |
| `skills/mys_calculator.py` | 养成计算器接口封装：算材料需求 + 读背包；`--check` 自检 |
| `skills/mys_inventory.py` | 库存同步 / 内部模型 / 快照 / 新旧对比；`--sync` 手动同步 |
| `skills/mys_login.py` | 扫码登录：建码 → 轮询 → 换 v1 `ltoken` → 验证 → 写 `.env`（老浏览器路径留作兜底） |
| `skills/qr_code.py` | 自带二维码编码器（纯标准库；终端画 ASCII、Studio 画 SVG） |
| `brain/growth_db.py` | SQLite 存储层（**只有它 import sqlite3**） |
| `brain/growth_models.py` | 阶段 / 状态机 / 目标校验 / 物品分类 |
| `brain/material_planner.py` | 共享池缺口计算 + 材料来源 + BetterGI 任务生成 |
| `brain/material_family.py` | **材料族**：材料 → 族群 → BetterGI 路线池（第 11 节） |
| `brain/resin_math.py` | 缺口 → 体力 → **预计培养天数**（第 12 节） |
| `brain/execution_queue.py` | 分批次执行队列（一次性 / 一条一条问） |
| `skills/mys_resin.py` | 从米游社每日便笺读**真实体力** |
| `scripts/build_material_families.py` | 离线构建材料族知识表（数据更新时重跑） |
| `scripts/export_genshin_db.js` | 把 Node 包 genshin-db 导成 JSON（只在数据更新时跑一次） |
| `memory/game_knowledge/material_families.json` | 知识表产物：族群 / 材料 / 天赋书日程 / 交叉校验 |
| `memory/game_knowledge/genshin_db_export.json` | genshin-db 导出产物（运行时读这个，不需要 Node） |
| `brain/growth_planner.py` | 规划主入口、状态机、排序、与 BetterGI 的完成联动 |
| `brain/growth_tools.py` | Agent 工具层（薄包装，没有自己的计算逻辑） |
| `studio/server.py` | `/api/growth*` 系列接口 |
| `studio/web/app.js` | 「角色养成」页的前端逻辑 |
| `skills/bgi_watcher.py` | 新增"任务结束订阅"，养成系统靠它做执行后重新同步 |
| `tests/test_mys_calculator.py` / `test_inventory.py` / `test_growth_planner.py` / `test_growth_api.py` | 172 个用例 |

---

## 10. 已知边界（老实说）

- **养成计算器不是官方开放接口**：风控严格、响应形状会随版本变。
  现在在用的三条（算材料 / 单角色状态 / 图鉴）**都是实测确认过的**（HTTP 200 + `retcode 0`），
  原始响应都落盘在 `memory/mys/calculator/`，改版时照着改一处即可。
  ⚠️ 唯一"真的废了"的是 `/v1/sync/avatar/list`（我的角色 / 背包），见第 6 节。
- ✅ **材料需求是完整且分阶段的**：等级（含经验书 / 突破材料 / 摩拉）、天赋（含周本材料、
  智识之冕）、武器（含精锻用魔矿）都算得出来，分别挂在「角色等级 / 天赋 / 武器等级」三行下。
  实测对照：丽莎 71→90 + 天赋 1→10/10/9，米游社给的摩拉是 1,099,365（等级）
  + 4,257,500（天赋）+ 746,165（武器）——三个桶是**互不重叠的花费**，程序按桶分别记账，
  扁平视图里相加。
- ⚠️ **拿不到背包里已有的材料数量**：米游社关掉了「我的角色 / 背包」接口
  （`/v1/sync/avatar/list` 带完整 cookie 也回 `-100`）。所以缺口退化成
  **"需要多少就刷多少"**（按总需求刷，可能比真实缺口多一些）。
  程序会明确写"库存不可用"，**不会**假装缺口是准的。
- **"这个角色几级 / 有没有这个角色"能拿到**：`/v1/sync/avatar/detail?avatar_id=`，
  没有的角色回 `-1 伙伴不存在`。整号扫描要一个个问（图鉴 118 个），所以只在需要时才扫。
- **第一版不含武器精炼**（规格书 §12 明确留到第二阶段）。
- **不按"缺口 ÷ 每次产出"算秘境趟数**：那需要世界等级，而世界等级没有可信来源。
  改成"一次固定跑几趟 → 重新同步 → 再看还缺多少"，宁可多跑两轮，也不假装算准了。
- **今日体力估算只是规划参考**，不作为库存事实来源。
- **自动角色排序（`mode=auto`）** 是第二阶段的功能，现在选它等价于默认排序。
- **周本材料**目前只会在任务表里以"缺 N 个"的形式出现，不会自动安排周本（第二阶段）。
  （接了材料族之后，理由会写明是哪只周本首领掉的，例如"周本首领「多托雷」的奖励"。）
- **角色图鉴一次只回 10 条**：`POST /v1/avatar/list` 的 `list` 固定 10 条，
  `total` 才是全量（`is_all=true` 放 **query** 时 total 从 118 变成 146）。
  翻页参数（`page`/`size` 放 body 或 query）实测**都不生效**，所以拿不到"全部角色 id"。
  这不影响养成：需要的是**某个角色**的状态与需求，那两条按 id 单查就够。
  （真要做"整号角色扫描"，可以用 `/v1/sync/avatar/detail` 一个个问 —— 没有的角色会回
  `-1 伙伴不存在`，但 146 个请求太密，暂时不做。）

---

## 11. 材料族：为什么"缺异色结晶石"以前匹配不到路线

### 问题

米游社算出来的缺口永远是**具体材料**（异色结晶石），而 BetterGI 的路线是按**族群**
组织的（原海异种）。两边词表不同 —— 本机 `material_category_index()` 里有 `原海异种`，
却**没有** `异色结晶石`。中间缺了一层，表现就是：

```text
缺异海凝珠   → 找「异海凝珠」路线 → 有（但它其实也是族群路线）
缺异色结晶石 → 找「异色结晶石」路线 → 没有 → 「缺少可执行路线」
```

同族的低档材料有来源、高档材料没有，玩家看到的就是"材料匹配不对"。

### 解决：材料 → 材料族 → 敌人族群 → BetterGI 路线池

```text
异海凝珠 ─┐
异海之块 ─┼─→ 材料族「原海异种」─→ 敌人族群「原海异种」─→ 54 条路线文件
异色结晶石 ┘
```

接上之后，三个档位解析到的是**同一条路线**，所以：

- 任务表里每档材料仍然各占一行（各自的数量是准的），但「来源」栏都指向族群路线；
- 下发指令时同一条路线**只发一次**（`tasks_to_bgi_command` 去重）——
  不然玩家会把同一条路线跑三遍，收益一点没多；
- 悬停「来源」栏能看到"同族还有 XX、YY，一条路线全掉"。

### 知识表怎么来的

`scripts/build_material_families.py` 离线生成 `memory/game_knowledge/material_families.json`。
**数据文件都在 `memory/game_knowledge/`**，不再堆在项目根目录：

```text
memory/game_knowledge/
├── mapping.json               ← 材料获取途径的原始数据（TeyvatGuide WIKI，4254 种材料）
├── genshin_db_export.json     ← genshin-db 的导出产物（秘境日程 / 怪物掉落 / 合成配方）
└── material_families.json     ← ★ 构建产物：族群 / 材料 / 天赋书日程 / 档位链（运行时读它）
```

| 数据源 | 提供什么 |
| --- | --- |
| `memory/game_knowledge/mapping.json`（TeyvatGuide WIKI，4254 种材料） | 获取途径文本，如"60级以上原海异种掉落" |
| `memory/game_dict_baike_full.json` | 补 `mapping.json` 没收录的材料（字典较旧） |
| `memory/boss_drops_dict.json` | 首领 → 掉落（校验首领取向） |
| `skills.gather_cooldown` | 本机 BetterGI 路线库：族群 ↔ 路线文件 |
| **genshin-db**（`genshin_db_export.json`） | 秘境开放星期、怪物掉落表、合成配方链、独立交叉校验 |
| `config/knowledge_overrides.json` | 人工修正（最高优先级） |

结果：**248 种材料有族群，其中 158 种能落到本机路线**；113 个族群，55 个有路线，
97 个认得成员怪物。（158 这个数字与 `docs/coverage_report.md` 里独立统计的"怪物掉落 158"完全吻合。）

⚠️ 只认**整行**就是「【N级以上】XX掉落」的来源文本：这些数据里混着大量玩家吐槽
（"掉落几率很小吧…"）和攻略，子串匹配会把吐槽当族群名。解析不出来的宁可不要。

### genshin-db 是 Node 包：怎么装、怎么更新

`genshin-db` **只有 Node 版**，而 GI_Agent 是纯 Python 且要能离线跑。所以按架构文档 §9 的做法
做**一次性导出**（`scripts/export_genshin_db.js`），**运行时不再需要 Node、也不需要那个包**：

```text
npm install                    # 根目录的 package.json 已声明 genshin-db（devDependency）
npm run export:genshin-db      # → memory/game_knowledge/genshin_db_export.json
npm run knowledge              # 上面那条 + python scripts/build_material_families.py（一条龙）
```

**包在哪儿找**（按顺序，第一个能用的就用；脚本每次都会打印 `📦 用的是 …`，排查看这一行）：

1. `--pkg <目录>` 手动指定；
2. `.env` 里的 `GENSHIN_DB_PATH`（装在别处时填这个，配置页也有这一项）；
3. 项目自己的 `node_modules/genshin-db`（`npm install` 之后就在这儿）；
4. 用户主目录的 `node_modules/genshin-db`；
5. 当前目录的 `node_modules`。

`node_modules/` 已经在 `.gitignore` 里，不会误提交。

**运行时不需要 Node，也不需要那个包**。导出三样东西（都是 `mapping.json` 没有的）：

1. `domains.daysOfWeek` → 天赋书出自哪个秘境、**哪几天开放**。
   这条最有用：本地百科索引里 21 个系列的「教导」档全缺，现在有了直接数据
   （`「正义」的教导 → 精通秘境：箴铭 · 苍白的遗荣 · 周二/五/日`），
   而不是靠同系列推算。实测两边日程**一致**（箴铭 = 周二/五/日）。
2. `enemies.rewardPreview` → **族群 → 成员怪物**（架构文档 §8 的 `enemy_families`）。
   313 只怪里 291 只有掉落表，还带掉率。§8 的这条链现在是通的：
   `异色结晶石 → 原海异种族 → 重甲蟹 / 膨膨兽 / 泡泡海马…（10 只）`。
3. `materials.sources` → **独立交叉校验**。它是另一条数据链（GenshinData / wiki），
   和 `mapping.json`（TeyvatGuide）不是同一个来源。实测 **一致 214、冲突 0**
   —— 两条链都指向同一个族群，才说明推导站得住。`tests/test_material_family.py`
   里有一条守卫用例：哪天重跑构建把族群推歪了会立刻炸。

**⚠️ genshin-db 有什么做不到的**：v5.2.7 对应**游戏数据 v6.2**，比 `mapping.json` **旧** ——
至冬 / 挪德卡莱的新材料它一个都没有（幻造晶鳞石、幻造裂晶、嵌合种、源生嵌合体、
扭曲的枯枝、异端的瓶剂、「坚忍」/「慈爱」的天赋书、奥黛塔…）。
所以它的定位是**补充与校验**，覆盖优先级排在 `mapping.json` 之后。

**⚠️ 不要按 id 匹配材料**：米游社计算器的 `id` 字段在不同区块含义不同 ——
实测 `202` 既是「摩拉」又是某个角色的技能「压制」，`301` 既是「岩之印」又是「岩者，六合引之为骨」。
所以材料一律**按名字**匹配（1570 个 id 里只有 47 个能对上 genshin-db，其中 14 个是这种撞车）。

### 首领与周本

首领（无相之雷、急冻树…）在「敌人与魔物」目录里**没有**路线，它们走 BetterGI 的
「批量讨伐角色养成材料BOSS」（`run_boss`）。所以首领材料翻译成首领名之后，交给现成的
`skills.boss_pathing_guard.validate_boss_target()` 判断能不能跑：

- 校验通过 → 生成 `TASK_BOSS`（可执行）；
- 校验不过（脚本标了不支持 / 名字对不齐）→ 保持"缺少路线"，理由写明是哪只首领；
- 周本首领（多托雷、吞星之鲸…）→ 保持"缺少路线"，理由写明"每周挑战一次"。

### 边界

- 天赋书（「XX」的教导/指引/哲学）来自**秘境**，不是打怪掉落 —— 它们走秘境日程那条路，
  不参与族群合并（`docs/coverage_report.md` 里也专门提过这一点）。
  日程优先用 genshin-db 的 `daysOfWeek`（直接数据），查不到才退回百科索引、最后才是
  同系列推算（理由里会写明是哪一种，例如"开放日程按同系列的高档天赋书推算"）。
  比 genshin-db 还新的系列（「坚忍」的精通秘境：隐修、「慈爱」的默想）本地没有任何日程，
  就**不放行**、只把出处写进理由（§30：不猜日程，宁可让玩家自己安排）。
- 族群名到路线目录名靠**别名表**（`scripts/build_material_families.py` 里的
  `ROUTE_ALIASES`，如「丘丘射手」→「丘丘人射手」）+ 名称包含匹配。别名认不出就不给路线，
  **不会**猜一条相近的（宁可缺路线，也不要跑错地方）。
- 知识表是**构建时快照**，路线库会变。所以每次用之前还会问一遍本机的「敌人与魔物」目录
  （`material_family.route_available()`）；那一组被删了就退回"缺少路线"。
- 族 id 直接用中文名（离线环境没有拼音库）。以后要英文 id，在
  `knowledge_overrides.json` 里给即可。

---

## 12. 执行方式、体力与预计天数

### 执行方式：一次性 / 分批次

`.env` 的 `GROWTH_EXECUTION_MODE`：

| 取值 | 行为 |
| --- | --- |
| `all`（默认） | 按原本调度**一次性全部下发**：`energy_task` 一个 + `free_task` 若干 |
| `stepwise` | **分批次**：先推总路线详情 → 只问第一条「跑不跑」→ 回 y 就跑这条 → 跑完自动推下一条 |

两种方式都**跳过没有路线的材料**（`waiting_route` 不占批次，但照样显示在明细表里）。
回 y 之外的内容 = 放弃队列，按玩家新说的来。

实现：`brain/execution_queue.py`（队列状态存在 `settings` 表 `growth_execution_queue_v1`
——分批次天然跨"几分钟 + 多次消息往返"，进程重启必须还能接着跑）。
推进由 `growth_planner._on_bettergi_finished()` 在"跑完 + 重新同步"之后触发。

分批次在聊天通道上的完整闭环：

```text
启动 Agent（或每次推进）
    ↓ 推「今天的执行目标」+ 总路线详情
    ↓ 只问第一条：「▶️ 第 1/N 条路线：… 👉 y = 就跑这一条」
    ↓ 同时把这一条挂成 pending_task（不然回 y 时路由器只会说"没有待确认的计划"）
玩家回 y  → 只下发这一条（energy_task / free_task 各一条）
    ↓ BetterGI 跑完 → 重新同步米游社 → 自动推第 2 条
玩家回别的内容 / 取消 → 放弃整个队列，按他新说的来
```

⚠️ **必须把当前这一条挂成待审批任务**（`growth_planner._arm_step_approval()`）：
聊天通道的审批是看 `store["pending_task"]` 的，不挂进去玩家回 y 就断在"没有待确认的计划"。
而且只有**真的推出去**（`send_notice` 返回成功）才挂 —— 推不出去却挂了，玩家回 y 会跑一条
他根本没看到的路线。

⚠️ 主动推送要找得到会话目标：`store["last_target"]`（`agent_router.handle_message()` 每条消息
都会写）。这个键以前**没有任何地方写过**，所以"本地处理完 QQ 一条都收不到" —— 修的时候
注意它同时要在 `memory_manager` 的**加载白名单**里，否则下次加载就被丢掉。

⚠️ **不能用"材料还缺不缺"判断这条路线成没成功**：米游社的已有/还差是算出来的，
BetterGI 跑完那一刻背包还没刷新，所以推进只看"任务真的结束了"。

### 数据源：展柜 / 养成计算器

`.env` 的 `GROWTH_DATA_SOURCE`（默认 `showcase`）：

| 取值 | 行为 |
| --- | --- |
| `showcase`（默认） | 保持原行为：角色展柜（Enka）是主上下文，养成计算器那份缺口作为参考注入 |
| `calculator` | **以养成计算器为准**：注入真实缺口 + 今天该跑的执行路线 + 预计天数 + 体力，并明确告诉模型"不要用展柜推算缺口"（展柜没有「已有/还差」） |

实现：`growth_tools.growth_source_block()`（在 `llm_brain` 的上下文装配处调用）。
选 `calculator` 时启动推送会把当前养成计划（含执行路线）推给玩家。

### 体力：按当前体力换算次数

以前副本 / 地脉的趟数是**硬编码**的（秘境 2 趟、地脉 6 趟），跟身上多少体力无关 ——
体力只剩 20 也照排 6 趟，等于排了个跑不完的计划。现在：

- `skills/mys_resin.py` 从米游社**每日便笺**读真实体力；
- `material_planner.allocate_resin()` 按刷取优先级把这一轮体力分下去，`_apply_runs()`
  按"体力够几趟 / 缺口需要几趟"取小值；
- **读不到体力时**退化成默认趟数，并在说明里写「⚠️ 读不到体力，按默认 N 趟排」
  —— 不假装体力是 0，也不假装算准了。

⚠️ **体力接口要 app 版 cookie**（v1 `ltoken`）。只有养成计算器那套 v2 cookie 时，
打过去会回「账号数据异常」（实测），所以 `mys_resin.probe()` **先看 cookie 支不支持再决定
要不要发请求**，避免每次规划都白打一次风控接口。

### 刷取优先级（玩家给定）

```text
① 等级突破首领（世界 Boss）
② 采集物 / 普通魔物掉落（含天赋与武器的）—— 不耗体力，顺手做
③ 经验地脉「启示之花」
④ 角色武器的突破副本
⑤ 天赋副本
⑥ 摩拉地脉「藏金之花」
```

- 独占的体力任务（秘境 / 地脉 / 首领）**一轮只能选一个**，按这个优先级挑
  （`tasks_to_bgi_command()` 不再"谁先出现谁上"）；
- 当前角色的**体力目标做完了 / 今天没开**（副本日期不对）→ 体力花在**下个角色**上
  （同样按这个顺序），当前角色的采集 / 魔物任务保留不丢；等日期到了回来补刷
  （`growth_planner._fallback_tasks()`）。

### 预计培养天数

`brain/resin_math.py`，按每 **1 点体力**的产出折算（玩家实测比例）：

| 来源 | 每点体力 | 折算方式 |
| --- | --- | --- |
| 天赋秘境 | 0.5135 | **等效绿色**天赋素材（指引 ×3、哲学 ×9） |
| 武器突破秘境 | 0.5090 | 等效绿色武器突破素材（靠合成配方链 3 低换 1 高） |
| 经验地脉 | 0.3235 本 | 大英雄的经验（冒险家 ×0.25、流浪者 ×0.05） |
| 摩拉地脉 | 3050 摩拉 | 世界 9 |
| 世界 9 首领 | 0.0765 个 | 角色突破材料 |

体力 1 点 / 8 分钟 → 一天 180 点 → `天数 = 总体力 / 180`。
档位换算优先用 genshin-db 的合成配方链，其次天赋书名字后缀（教导/指引/哲学 = 1/3/9，
新系列也能定），都不认识就按最低档算并**明确标注"天数可能偏低"**。

这是**参考天数**，不是承诺：产出比例是实测值、秘境只在特定星期开放、周本/活动/合成台
都会影响实际进度。**采集物与普通魔物掉落不耗体力**，不参与天数（界面单独说明）。

### 两个登录：一份 cookie 同时算材料 + 读体力

| 登录入口 | 换到的东西 | 谁认它 |
| --- | --- | --- |
| 「扫码登录」（米游社 App） | v1 `ltoken` / `ltuid` / `cookie_token` | 体力（dailyNote）、战绩接口 |
| 「网页版扫码（养成计算器）」 | `ltoken_v2` / `ltuid_v2` / `cookie_token_v2` | **养成计算器**（算材料、已有/还差） |

这就是"养成计算器要单独登录"的解法：**两个都扫一次**，服务端把两份 cookie
**合并**（键名不冲突，见 `mys_login.merge_cookie`）写进同一份 `MYS_COOKIE`，
顺带保留各自要用的键 —— 不再需要手动去网页 F12 复制。

网页版扫码走 `passport-api.miyoushe.com/account/ma-cn-passport/web/createQRLogin` +
`queryQRLoginStatus`，令牌在确认后的 **`Set-Cookie` 响应头**里（body 的 `tokens` 永远是空的），
所以 `mys_api.web_request()` 单独留了一条能拿到响应头的通道。
