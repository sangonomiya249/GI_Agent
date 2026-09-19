"""角色养成系统的 SQLite 存储层（`memory/growth.db`）。

**为什么单独一个库**：角色养成要同时管住这些互相牵连的东西 ——
角色、角色目标、武器、库存快照、库存明细、材料需求、计划、计划任务、执行历史、同步历史。
塞进 JSON 的结果会是"每次改一个天赋等级就整份重写，写坏一次全丢"，所以按规格书 §24/§25 上 SQLite。

**这一层的边界（很重要）**：
  · 只有本模块 import `sqlite3`。LLM **不允许**直接碰这个库（规格书 §38.10），
    它只能经由 `growth_planner` / Studio API 这些有校验的入口；
  · 真实库存的**权威副本**是 `memory/mys/inventory_latest.json`（人可读、可 diff、坏了能手工修），
    这里的 `inventory_snapshots` / `inventory_items` 是历史账本，用于"两次同步之间涨了多少"的对比；
  · 任何一次写入都要能在失败时不影响上一个好状态：连接用 WAL + 显式事务。

表结构见规格书 §25，字段名与规格书一一对应（`character_id` / `level_target` / `normal_target` …）。
"""

import contextlib
import datetime
import json
import os
import sqlite3
import threading

import config

# ==========================================
# 🌟 库位置与连接
# ==========================================

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def db_path(path=None):
    """库文件位置。测试通过参数指到临时目录，绝不写玩家真实的 memory/growth.db。"""
    return path or getattr(config, "GROWTH_DB_PATH", "") or config.project_path(
        "memory", "growth.db"
    )


def now_text(moment=None):
    """统一的时间戳文本（本地时区，秒级）。"""
    return (moment or datetime.datetime.now()).strftime(_TIME_FORMAT)


def parse_time(value):
    """把 `now_text` 写下的时间戳读回来；坏值返回 None（调用方一律当"没有时间戳"）。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (_TIME_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


# 建表语句。用 `IF NOT EXISTS` + 末尾的 `_migrate()` 兜住"以后加字段"，
# 这样老库文件不会被新版本判成损坏，也不会要求玩家删库重来。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL UNIQUE,
    name         TEXT    NOT NULL DEFAULT '',
    rarity       INTEGER NOT NULL DEFAULT 0,
    element      TEXT    NOT NULL DEFAULT '',
    level        INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS weapons (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    weapon_id INTEGER NOT NULL UNIQUE,
    name      TEXT    NOT NULL DEFAULT '',
    rarity    INTEGER NOT NULL DEFAULT 0,
    type      TEXT    NOT NULL DEFAULT '',
    updated_at TEXT   NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS growth_targets (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id       INTEGER NOT NULL UNIQUE,
    enabled            INTEGER NOT NULL DEFAULT 1,
    priority           INTEGER NOT NULL DEFAULT 0,
    order_index        INTEGER NOT NULL DEFAULT 0,
    level_target       INTEGER NOT NULL DEFAULT 90,
    normal_target      INTEGER NOT NULL DEFAULT 1,
    skill_target       INTEGER NOT NULL DEFAULT 10,
    burst_target       INTEGER NOT NULL DEFAULT 10,
    weapon_enabled     INTEGER NOT NULL DEFAULT 1,
    weapon_id          INTEGER NOT NULL DEFAULT 0,
    weapon_level_target INTEGER NOT NULL DEFAULT 90,
    level_priority     INTEGER NOT NULL DEFAULT 1,
    weapon_priority    INTEGER NOT NULL DEFAULT 2,
    talent_priority    INTEGER NOT NULL DEFAULT 3,
    completion_mode    TEXT    NOT NULL DEFAULT 'sequential',
    created_at         TEXT    NOT NULL DEFAULT '',
    updated_at         TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT    NOT NULL DEFAULT '',
    server        TEXT    NOT NULL DEFAULT '',
    fetched_at    TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT 'mys',
    success       INTEGER NOT NULL DEFAULT 1,
    item_count    INTEGER NOT NULL DEFAULT 0,
    raw_json_path TEXT    NOT NULL DEFAULT '',
    error         TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS inventory_items (
    snapshot_id INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,
    name        TEXT    NOT NULL DEFAULT '',
    category    TEXT    NOT NULL DEFAULT '',
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_inventory_items_item ON inventory_items (item_id);

CREATE TABLE IF NOT EXISTS material_requirements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id  INTEGER NOT NULL,
    phase      TEXT    NOT NULL DEFAULT '',
    item_id    INTEGER NOT NULL DEFAULT 0,
    item_name  TEXT    NOT NULL DEFAULT '',
    required   INTEGER NOT NULL DEFAULT 0,
    owned      INTEGER NOT NULL DEFAULT 0,
    missing    INTEGER NOT NULL DEFAULT 0,
    computed_at TEXT   NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_material_requirements_target ON material_requirements (target_id);

CREATE TABLE IF NOT EXISTS plans (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at           TEXT    NOT NULL DEFAULT '',
    updated_at           TEXT    NOT NULL DEFAULT '',
    status               TEXT    NOT NULL DEFAULT 'IDLE',
    current_character_id INTEGER NOT NULL DEFAULT 0,
    current_phase        TEXT    NOT NULL DEFAULT '',
    summary              TEXT    NOT NULL DEFAULT '',
    detail_json          TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS plan_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id      INTEGER NOT NULL,
    character_id INTEGER NOT NULL DEFAULT 0,
    phase        TEXT    NOT NULL DEFAULT '',
    task_type    TEXT    NOT NULL DEFAULT '',
    material     TEXT    NOT NULL DEFAULT '',
    item_id      INTEGER NOT NULL DEFAULT 0,
    required     INTEGER NOT NULL DEFAULT 0,
    missing      INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'pending',
    note         TEXT    NOT NULL DEFAULT '',
    updated_at   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_plan_tasks_plan ON plan_tasks (plan_id);

CREATE TABLE IF NOT EXISTS execution_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id      INTEGER NOT NULL DEFAULT 0,
    character_id INTEGER NOT NULL DEFAULT 0,
    phase        TEXT    NOT NULL DEFAULT '',
    started_at   TEXT    NOT NULL DEFAULT '',
    finished_at  TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT '',
    result       TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT    NOT NULL DEFAULT '',
    started_at  TEXT    NOT NULL DEFAULT '',
    finished_at TEXT    NOT NULL DEFAULT '',
    success     INTEGER NOT NULL DEFAULT 0,
    error       TEXT    NOT NULL DEFAULT '',
    snapshot_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

_EXPECTED_COLUMNS = {
    "characters": {"element": "TEXT NOT NULL DEFAULT ''", "level": "INTEGER NOT NULL DEFAULT 0"},
    # `owned` = 这个角色**这个号有没有**。
    # ⚠️ 米游社那个"我的角色"接口已经下线，程序问不到；所以这个值来自两处：
    #   · 算材料时顺带问到的真实状态（`/v1/sync/avatar/detail`）；
    #   · 玩家自己在「添加角色」里选的。
    # 默认 1（当作已有）—— 绝大多数要养成目标的角色都是已经抽到的。
    "growth_targets": {"order_index": "INTEGER NOT NULL DEFAULT 0",
                       "owned": "INTEGER NOT NULL DEFAULT 1"},
    "inventory_snapshots": {
        "item_count": "INTEGER NOT NULL DEFAULT 0",
        "error": "TEXT NOT NULL DEFAULT ''",
    },
    "material_requirements": {"phase": "TEXT NOT NULL DEFAULT ''"},
    "plans": {"summary": "TEXT NOT NULL DEFAULT ''", "detail_json": "TEXT NOT NULL DEFAULT ''"},
    "plan_tasks": {"phase": "TEXT NOT NULL DEFAULT ''", "note": "TEXT NOT NULL DEFAULT ''"},
    "execution_history": {"character_id": "INTEGER NOT NULL DEFAULT 0", "phase": "TEXT NOT NULL DEFAULT ''"},
}

# 每个线程一个连接（sqlite3 的连接默认不能跨线程用，而 Studio 是多线程的 Flask）
_LOCAL = threading.local()
_INIT_LOCK = threading.Lock()
_INITIALISED = set()


def _configure(connection):
    connection.row_factory = sqlite3.Row
    with contextlib.suppress(sqlite3.DatabaseError):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")


def connect(path=None):
    """取本线程的连接（第一次调用时建库 + 迁移）。"""
    target = str(db_path(path))
    connection = getattr(_LOCAL, "connection", None)
    if connection is not None and getattr(_LOCAL, "path", None) == target:
        return connection

    # ⚠️ 换了库文件（切换账号 / 测试换了临时目录）时必须先关掉旧连接：
    #    sqlite 的连接持有文件句柄，泄漏的连接会让 Windows 上删不掉旧库文件
    #    （表现是测试清理阶段抛 PermissionError，把结果搞成一片红）。
    if connection is not None:
        with contextlib.suppress(sqlite3.DatabaseError):
            connection.close()

    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(target, timeout=10.0)
    _configure(connection)
    _ensure_schema(connection, target)
    _LOCAL.connection = connection
    _LOCAL.path = target
    return connection


def _ensure_schema(connection, target):
    with _INIT_LOCK:
        connection.executescript(_SCHEMA)
        _migrate(connection)
        connection.commit()
        _INITIALISED.add(target)


def _migrate(connection):
    """老库文件补列。`ALTER TABLE ... ADD COLUMN` 在列已存在时会报错，所以先查表结构。"""
    for table, columns in _EXPECTED_COLUMNS.items():
        try:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.DatabaseError:
            continue
        existing = {str(row["name"]) for row in rows}
        if not existing:
            continue
        for name, spec in columns.items():
            if name in existing:
                continue
            with contextlib.suppress(sqlite3.DatabaseError):
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def close(path=None):
    """关掉本线程的连接（测试清理 / 换库文件时用；平时的长连接不用管）。

    `path` 给定时只关"当前正连着的这个库"；不给就无条件关掉。
    另外顺手清掉 `_INITIALISED` 的登记，避免库文件被换成新的之后还认为已经迁移过。
    """
    connection = getattr(_LOCAL, "connection", None)
    if connection is None:
        _LOCAL.path = None
        return
    if path is not None and getattr(_LOCAL, "path", None) != str(db_path(path)):
        return
    with contextlib.suppress(sqlite3.DatabaseError):
        connection.close()
    _INITIALISED.discard(str(getattr(_LOCAL, "path", "") or ""))
    _LOCAL.connection = None
    _LOCAL.path = None


@contextlib.contextmanager
def transaction(path=None):
    """显式事务：中途抛异常就回滚，绝不留下"改了一半"的角色目标。"""
    connection = connect(path)
    try:
        yield connection
    except Exception:
        with contextlib.suppress(sqlite3.DatabaseError):
            connection.rollback()
        raise
    else:
        connection.commit()


# ==========================================
# 🌟 角色 / 武器：从米游社同步下来的"账号事实"
# ==========================================


def upsert_character(character_id, name="", rarity=0, element="", level=0, path=None):
    """登记一个角色（存在就更新名字/星级/元素/等级）。返回其主键。"""
    with transaction(path) as connection:
        connection.execute(
            """
            INSERT INTO characters (character_id, name, rarity, element, level, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(character_id) DO UPDATE SET
                name = excluded.name,
                rarity = excluded.rarity,
                element = excluded.element,
                level = excluded.level,
                updated_at = excluded.updated_at
            """,
            (int(character_id), str(name or ""), int(rarity or 0), str(element or ""),
             int(level or 0), now_text()),
        )
        row = connection.execute(
            "SELECT id FROM characters WHERE character_id = ?", (int(character_id),)
        ).fetchone()
    return int(row["id"]) if row else 0


def upsert_weapon(weapon_id, name="", rarity=0, type_="", path=None):
    """登记一把武器。"""
    with transaction(path) as connection:
        connection.execute(
            """
            INSERT INTO weapons (weapon_id, name, rarity, type, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(weapon_id) DO UPDATE SET
                name = excluded.name,
                rarity = excluded.rarity,
                type = excluded.type,
                updated_at = excluded.updated_at
            """,
            (int(weapon_id), str(name or ""), int(rarity or 0), str(type_ or ""), now_text()),
        )
        row = connection.execute(
            "SELECT id FROM weapons WHERE weapon_id = ?", (int(weapon_id),)
        ).fetchone()
    return int(row["id"]) if row else 0


def get_character(character_id, path=None):
    row = connect(path).execute(
        "SELECT * FROM characters WHERE character_id = ?", (int(character_id),)
    ).fetchone()
    return dict(row) if row else None


def list_characters(path=None):
    """全部已知角色，按 `rarity DESC, character_id ASC`（和默认养成顺序一致）。"""
    rows = connect(path).execute(
        "SELECT * FROM characters ORDER BY rarity DESC, character_id ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def character_name(character_id, path=None):
    row = connect(path).execute(
        "SELECT name FROM characters WHERE character_id = ?", (int(character_id),)
    ).fetchone()
    return str(row["name"]) if row and row["name"] else ""


# ==========================================
# 🌟 角色养成目标（growth_targets）
# ==========================================

TARGET_FIELDS = (
    "enabled",
    "priority",
    "order_index",
    "level_target",
    "normal_target",
    "skill_target",
    "burst_target",
    "weapon_enabled",
    "weapon_id",
    "weapon_level_target",
    "level_priority",
    "weapon_priority",
    "talent_priority",
    "completion_mode",
    # 这个号有没有这个角色（玩家选的；程序问不到 —— 「我的角色」接口已下线）
    "owned",
)


def _target_row_to_dict(row, character=None):
    data = dict(row)
    character = character if character is not None else get_character(data["character_id"])
    if character:
        data["character_name"] = character.get("name") or ""
        data["rarity"] = int(character.get("rarity") or 0)
        data["element"] = character.get("element") or ""
    else:
        data.setdefault("character_name", "")
        data.setdefault("rarity", 0)
        data.setdefault("element", "")
    data["enabled"] = bool(data.get("enabled"))
    data["weapon_enabled"] = bool(data.get("weapon_enabled"))
    return data


def get_target(character_id, path=None):
    """取一个角色的养成目标（没有就返回 None —— 调用方决定要不要建默认目标）。"""
    row = connect(path).execute(
        "SELECT * FROM growth_targets WHERE character_id = ?", (int(character_id),)
    ).fetchone()
    if not row:
        return None
    return _target_row_to_dict(row)


def list_targets(path=None, enabled_only=False):
    """全部养成目标，按规格书 §14 的排序规则：priority ASC → rarity DESC → character_id ASC。"""
    rows = connect(path).execute(
        """
        SELECT t.*, COALESCE(c.rarity, 0) AS rarity, COALESCE(c.name, '') AS character_name,
               COALESCE(c.element, '') AS element
        FROM growth_targets t
        LEFT JOIN characters c ON c.character_id = t.character_id
        ORDER BY t.priority ASC, rarity DESC, t.character_id ASC
        """
    ).fetchall()
    items = []
    for row in rows:
        data = dict(row)
        data["enabled"] = bool(data.get("enabled"))
        data["weapon_enabled"] = bool(data.get("weapon_enabled"))
        # `owned` = 这个号有没有这个角色（玩家选的 / 算材料时问到的）
        data["owned"] = bool(data.get("owned", 1))
        if enabled_only and not data["enabled"]:
            continue
        items.append(data)
    return items


def save_target(character_id, values=None, path=None, **kwargs):
    """新建 / 更新一个角色目标。返回值是落库后的完整目标（含 JOIN 出来的角色名）。

    `values` 与关键字参数都只覆盖**传进来的字段** —— 没传的保持原值，
    这样 Studio 上"只改天赋等级"的那次 PUT 不会把武器目标顺手清零。
    """
    payload = dict(values or {})
    payload.update(kwargs)
    character_id = int(character_id)

    existing = connect(path).execute(
        "SELECT * FROM growth_targets WHERE character_id = ?", (character_id,)
    ).fetchone()

    if existing is None:
        defaults = {
            "enabled": 1,
            "priority": _next_priority(path),
            "order_index": _next_priority(path),
            "level_target": 88,
            "normal_target": 1,
            "skill_target": 9,
            "burst_target": 9,
            "weapon_enabled": 1,
            "weapon_id": 0,
            "weapon_level_target": 90,
            "level_priority": 1,
            "weapon_priority": 2,
            "talent_priority": 3,
            "completion_mode": "sequential",
        }
        defaults.update({key: value for key, value in payload.items() if key in TARGET_FIELDS})
        columns = ["character_id"] + sorted(defaults)
        values_sql = ", ".join("?" for _ in columns)
        params = [character_id] + [_coerce(defaults[key]) for key in sorted(defaults)]
        with transaction(path) as connection:
            connection.execute(
                f"INSERT INTO growth_targets ({', '.join(columns)}, created_at, updated_at) "
                f"VALUES ({values_sql}, ?, ?)",
                params + [now_text(), now_text()],
            )
    else:
        updates = {key: value for key, value in payload.items() if key in TARGET_FIELDS}
        if updates:
            assignments = ", ".join(f"{key} = ?" for key in sorted(updates))
            params = [_coerce(updates[key]) for key in sorted(updates)]
            with transaction(path) as connection:
                connection.execute(
                    f"UPDATE growth_targets SET {assignments}, updated_at = ? WHERE character_id = ?",
                    params + [now_text(), character_id],
                )
    return get_target(character_id, path=path)


def bulk_update_targets(values, path=None):
    """一次事务更新所有已有角色目标，保留排序 / 启用 / 拥有状态等个性字段。"""
    updates = {key: value for key, value in (values or {}).items() if key in TARGET_FIELDS}
    if not updates:
        return 0
    assignments = ", ".join(f"{key} = ?" for key in sorted(updates))
    params = [_coerce(updates[key]) for key in sorted(updates)]
    timestamp = now_text()
    with transaction(path) as connection:
        cursor = connection.execute(
            f"UPDATE growth_targets SET {assignments}, updated_at = ?",
            params + [timestamp],
        )
    return int(cursor.rowcount or 0)


def delete_target(character_id, path=None):
    """删掉一个角色目标。返回是否真的删了（没这个角色时返回 False，接口据此回 404）。"""
    with transaction(path) as connection:
        cursor = connection.execute(
            "DELETE FROM growth_targets WHERE character_id = ?", (int(character_id),)
        )
    return cursor.rowcount > 0


def clear_targets(path=None):
    """Delete all growth targets and their stored requirement rows."""
    with transaction(path) as connection:
        count_row = connection.execute(
            "SELECT COUNT(*) AS count FROM growth_targets"
        ).fetchone()
        count = int(count_row["count"] or 0)
        connection.execute(
            "DELETE FROM material_requirements "
            "WHERE target_id IN (SELECT id FROM growth_targets)"
        )
        connection.execute("DELETE FROM growth_targets")
    return count


def reorder_targets(character_ids, path=None):
    """按给定顺序重排优先级（拖拽排序落盘）。

    `priority` 就是 1..N 的顺序号；`order_index` 同步写成一样的值，
    这样以后想做"自定义顺序 vs 默认星级顺序"两套视图时不用再迁一次库。
    """
    ids = [int(item) for item in character_ids or ()]
    with transaction(path) as connection:
        for index, character_id in enumerate(ids, start=1):
            connection.execute(
                "UPDATE growth_targets SET priority = ?, order_index = ?, updated_at = ? "
                "WHERE character_id = ?",
                (index, index, now_text(), character_id),
            )
    return list_targets(path=path)


def _next_priority(path=None):
    row = connect(path).execute("SELECT COALESCE(MAX(priority), 0) AS top FROM growth_targets").fetchone()
    return int(row["top"] or 0) + 1


def _coerce(value):
    """SQLite 只认 int/float/str/bytes/None：bool 转 0/1，字典列表转 JSON 文本。"""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return value


# ==========================================
# 🌟 库存快照（inventory_snapshots / inventory_items）
# ==========================================


def record_snapshot(uid, server, fetched_at, items, source="mys", success=True,
                    raw_json_path="", error="", path=None):
    """把一次库存同步记进账本，返回 snapshot_id。

    `items` 是内部库存模型的 `{item_key: {...}}` 或条目列表；空库存 + success=False
    也会落一条记录 —— 失败的同步同样是"可追踪"的一部分（规格书 §32）。
    """
    entries = _normalise_items(items)
    with transaction(path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO inventory_snapshots
                (uid, server, fetched_at, source, success, item_count, raw_json_path, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uid or ""), str(server or ""), str(fetched_at or now_text()),
             str(source or "mys"), 1 if success else 0, len(entries),
             str(raw_json_path or ""), str(error or "")),
        )
        snapshot_id = int(cursor.lastrowid)
        rows = [
            (snapshot_id, int(item["item_id"]), str(item.get("name") or ""),
             str(item.get("category") or "other"), int(item.get("count") or 0))
            for item in entries
            if item.get("item_id")
        ]
        if rows:
            connection.executemany(
                "INSERT OR REPLACE INTO inventory_items (snapshot_id, item_id, name, category, count) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
    return snapshot_id


def _normalise_items(items):
    if isinstance(items, dict):
        iterable = list(items.values())
    elif isinstance(items, (list, tuple)):
        iterable = list(items)
    else:
        return []
    result = []
    for item in iterable:
        if not isinstance(item, dict):
            continue
        result.append(item)
    return result


def latest_snapshot(path=None, success_only=True):
    """最近一次库存快照（默认只要成功的 —— 失败的快照没有库存可言）。"""
    sql = "SELECT * FROM inventory_snapshots"
    if success_only:
        sql += " WHERE success = 1 AND item_count > 0"
    sql += " ORDER BY id DESC LIMIT 1"
    row = connect(path).execute(sql).fetchone()
    return dict(row) if row else None


def snapshot_items(snapshot_id, path=None):
    """某个快照的库存明细，返回 `{item_id: {item_id,name,category,count}}`。"""
    rows = connect(path).execute(
        "SELECT item_id, name, category, count FROM inventory_items WHERE snapshot_id = ?",
        (int(snapshot_id),),
    ).fetchall()
    return {
        int(row["item_id"]): {
            "item_id": int(row["item_id"]),
            "name": str(row["name"] or ""),
            "category": str(row["category"] or ""),
            "count": int(row["count"] or 0),
        }
        for row in rows
    }


def previous_snapshot(snapshot_id, path=None):
    """上一条成功快照（"这次同步比上次多了多少"要用它）。"""
    row = connect(path).execute(
        "SELECT * FROM inventory_snapshots WHERE success = 1 AND item_count > 0 AND id < ? "
        "ORDER BY id DESC LIMIT 1",
        (int(snapshot_id),),
    ).fetchone()
    return dict(row) if row else None


def list_snapshots(limit=30, path=None):
    rows = connect(path).execute(
        "SELECT * FROM inventory_snapshots ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [dict(row) for row in rows]


def inventory_delta(new_snapshot_id, old_snapshot_id=None, path=None, limit=None):
    """两次快照之间每种材料的变化量（规格书 §6「对比两次库存」）。

    返回 `[{item_id, name, before, after, delta}]`，按 |delta| 倒序 —— 最值得看的变化在最前面。
    """
    new_items = snapshot_items(new_snapshot_id, path=path)
    if old_snapshot_id is None:
        old = previous_snapshot(new_snapshot_id, path=path)
        old_snapshot_id = old["id"] if old else 0
    old_items = snapshot_items(old_snapshot_id, path=path) if old_snapshot_id else {}

    rows = []
    for item_id in set(new_items) | set(old_items):
        before = int((old_items.get(item_id) or {}).get("count") or 0)
        after = int((new_items.get(item_id) or {}).get("count") or 0)
        if before == after:
            continue
        entry = new_items.get(item_id) or old_items.get(item_id) or {}
        rows.append({
            "item_id": int(item_id),
            "name": str(entry.get("name") or ""),
            "before": before,
            "after": after,
            "delta": after - before,
        })
    rows.sort(key=lambda row: abs(row["delta"]), reverse=True)
    if limit:
        rows = rows[: int(limit)]
    return rows


# ==========================================
# 🌟 同步历史（sync_history）
# ==========================================


def start_sync(uid="", path=None):
    """开一条同步记录，返回它的 id（同步结果无论成败都要收尾，见 `finish_sync`）。"""
    with transaction(path) as connection:
        cursor = connection.execute(
            "INSERT INTO sync_history (uid, started_at, success) VALUES (?, ?, 0)",
            (str(uid or ""), now_text()),
        )
    return int(cursor.lastrowid)


def abort_sync(sync_id, path=None):
    """**撤销**一条同步记录（整行删掉）。

    ⚠️ 为什么需要它：`start_sync` 是"先插一条 `success=0` 的记录，做完再更新"。
    如果这次同步**根本不该算一次尝试**（数据源已下线、我们主动跳过），
    就必须把这条记录删掉 —— 否则它会永远停在"失败 + 空错误"，
    界面上就是一串莫名其妙的"失败"（踩过：6 条孤儿记录，谁也看不出哪来的）。
    """
    with transaction(path) as connection:
        connection.execute("DELETE FROM sync_history WHERE id = ?", (int(sync_id),))


def finish_sync(sync_id, success, error="", snapshot_id=0, path=None):
    with transaction(path) as connection:
        connection.execute(
            "UPDATE sync_history SET finished_at = ?, success = ?, error = ?, snapshot_id = ? WHERE id = ?",
            (now_text(), 1 if success else 0, str(error or ""), int(snapshot_id or 0), int(sync_id)),
        )


def sync_summary(path=None, limit=50):
    """最近若干次同步的成败统计（规格书 §32 的"成功 12 次 / 失败 1 次"）。"""
    rows = connect(path).execute(
        "SELECT * FROM sync_history ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    items = [dict(row) for row in rows]
    return {
        "total": len(items),
        "success": sum(1 for row in items if row["success"]),
        "failed": sum(1 for row in items if not row["success"]),
        "items": items,
    }


def list_sync_history(limit=30, path=None):
    rows = connect(path).execute(
        "SELECT * FROM sync_history ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [dict(row) for row in rows]


# ==========================================
# 🌟 材料需求（material_requirements）
# ==========================================


def replace_requirements(target_id, rows, path=None):
    """整体替换某个目标的材料需求快照（先删后插，避免残留上一次算出来的旧行）。"""
    with transaction(path) as connection:
        connection.execute("DELETE FROM material_requirements WHERE target_id = ?", (int(target_id),))
        payload = []
        for row in rows or ():
            payload.append((
                int(target_id),
                str(row.get("phase") or ""),
                int(row.get("item_id") or 0),
                str(row.get("item_name") or row.get("name") or ""),
                int(row.get("required") or 0),
                int(row.get("owned") or 0),
                int(row.get("missing") or 0),
                now_text(),
            ))
        if payload:
            connection.executemany(
                "INSERT INTO material_requirements "
                "(target_id, phase, item_id, item_name, required, owned, missing, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
    return len(payload)


def list_requirements(target_id, path=None, missing_only=False):
    sql = "SELECT * FROM material_requirements WHERE target_id = ?"
    if missing_only:
        sql += " AND missing > 0"
    sql += " ORDER BY missing DESC, item_id ASC"
    rows = connect(path).execute(sql, (int(target_id),)).fetchall()
    return [dict(row) for row in rows]


# ==========================================
# 🌟 计划与任务（plans / plan_tasks / execution_history）
# ==========================================


def create_plan(status="IDLE", current_character_id=0, current_phase="", summary="",
                detail=None, path=None):
    with transaction(path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO plans (created_at, updated_at, status, current_character_id,
                               current_phase, summary, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now_text(), now_text(), str(status or "IDLE"), int(current_character_id or 0),
             str(current_phase or ""), str(summary or ""),
             json.dumps(detail or {}, ensure_ascii=False)),
        )
    return int(cursor.lastrowid)


def update_plan(plan_id, patch=None, path=None, **kwargs):
    payload = dict(patch or {})
    payload.update(kwargs)
    allowed = {"status", "current_character_id", "current_phase", "summary", "detail_json"}
    updates = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key == "detail_json" and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        updates[key] = value
    if not updates:
        return get_plan(plan_id, path=path)
    assignments = ", ".join(f"{key} = ?" for key in sorted(updates))
    with transaction(path) as connection:
        connection.execute(
            f"UPDATE plans SET {assignments}, updated_at = ? WHERE id = ?",
            [_coerce(updates[key]) for key in sorted(updates)] + [now_text(), int(plan_id)],
        )
    return get_plan(plan_id, path=path)


def get_plan(plan_id, path=None):
    row = connect(path).execute("SELECT * FROM plans WHERE id = ?", (int(plan_id),)).fetchone()
    if not row:
        return None
    data = dict(row)
    detail = str(data.pop("detail_json", "") or "")
    try:
        data["detail"] = json.loads(detail) if detail else {}
    except ValueError:
        data["detail"] = {}
    return data


def latest_plan(path=None):
    row = connect(path).execute("SELECT id FROM plans ORDER BY id DESC LIMIT 1").fetchone()
    return get_plan(row["id"], path=path) if row else None


def list_plans(limit=20, path=None):
    """最近的若干份计划（**不含** detail 大字段 —— 列表页不需要那份 JSON）。"""
    rows = connect(path).execute(
        "SELECT id, created_at, updated_at, status, current_character_id, current_phase, summary "
        "FROM plans ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def replace_plan_tasks(plan_id, tasks, path=None):
    """整体替换一份计划的任务列表（重规划 = 重新生成，不做增量 patch）。"""
    with transaction(path) as connection:
        connection.execute("DELETE FROM plan_tasks WHERE plan_id = ?", (int(plan_id),))
        payload = []
        for task in tasks or ():
            payload.append((
                int(plan_id),
                int(task.get("character_id") or 0),
                str(task.get("phase") or ""),
                str(task.get("task_type") or task.get("type") or ""),
                str(task.get("material") or ""),
                int(task.get("item_id") or 0),
                int(task.get("required") or 0),
                int(task.get("missing") or 0),
                str(task.get("status") or "pending"),
                str(task.get("note") or ""),
                now_text(),
            ))
        if payload:
            connection.executemany(
                "INSERT INTO plan_tasks (plan_id, character_id, phase, task_type, material, "
                "item_id, required, missing, status, note, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
    return len(payload)


def list_plan_tasks(plan_id, path=None, status=None):
    sql = "SELECT * FROM plan_tasks WHERE plan_id = ?"
    params = [int(plan_id)]
    if status:
        sql += " AND status = ?"
        params.append(str(status))
    sql += " ORDER BY id ASC"
    rows = connect(path).execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def update_plan_task(task_id, patch=None, path=None, **kwargs):
    payload = dict(patch or {})
    payload.update(kwargs)
    allowed = {"status", "note", "missing", "required"}
    updates = {key: value for key, value in payload.items() if key in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{key} = ?" for key in sorted(updates))
    with transaction(path) as connection:
        connection.execute(
            f"UPDATE plan_tasks SET {assignments}, updated_at = ? WHERE id = ?",
            [_coerce(updates[key]) for key in sorted(updates)] + [now_text(), int(task_id)],
        )


def record_execution(plan_id, character_id, phase, status, result, started_at=None,
                     finished_at=None, path=None):
    with transaction(path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO execution_history
                (plan_id, character_id, phase, started_at, finished_at, status, result)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(plan_id or 0), int(character_id or 0), str(phase or ""),
             str(started_at or now_text()), str(finished_at or now_text()),
             str(status or ""), str(result or "")),
        )
    return int(cursor.lastrowid)


def list_executions(limit=50, path=None, plan_id=None):
    sql = "SELECT * FROM execution_history"
    params = []
    if plan_id is not None:
        sql += " WHERE plan_id = ?"
        params.append(int(plan_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    rows = connect(path).execute(sql, params).fetchall()
    return [dict(row) for row in rows]


# ==========================================
# 🌟 杂项设置（settings）：养成系统的开关就放这，不污染 .env
# ==========================================


def get_setting(key, default=None, path=None):
    row = connect(path).execute("SELECT value FROM settings WHERE key = ?", (str(key),)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return row["value"]


def set_setting(key, value, path=None):
    with transaction(path) as connection:
        connection.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(key), json.dumps(value, ensure_ascii=False)),
        )
    return value


def all_settings(path=None):
    rows = connect(path).execute("SELECT key, value FROM settings").fetchall()
    result = {}
    for row in rows:
        try:
            result[str(row["key"])] = json.loads(row["value"])
        except (TypeError, ValueError):
            result[str(row["key"])] = row["value"]
    return result
