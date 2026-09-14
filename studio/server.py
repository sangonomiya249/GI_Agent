"""GI Agent Studio 的本地服务端（Flask，127.0.0.1 随机端口，只监听本机）。

为什么用 Flask：它**本来就在 requirements 里**（飞书入口用的就是它），
所以"好看的新界面"不需要装任何新依赖就能跑；界面用 Edge/WebView2 的
`--app=` 窗口打开，看起来就是一个独立桌面程序（见 app_web.py）。

API 一览（前端 studio/web/app.js 消费）：
    GET  /api/state              运行状态 / 环境摘要 / 本轮计划 / BetterGI 状态 / 通道摘要
    POST /api/agent/start|stop|input     启动 Agent（顺带按规则唤醒远程通道）
    GET  /api/channels?since=...  远程通道（QQ 机器人 / 飞书）状态 + 增量日志
    POST /api/channels/<name>/start|stop|clear
    GET  /api/log?since=N        增量日志（含未结束的提示行 partial）
    POST /api/log/clear
    GET  /api/config             配置字段（分组 / 类型 / 当前值）
    POST /api/config             保存（自动备份 .env）
    POST /api/refresh-env        刷新展柜上下文
    GET  /api/doctor             环境体检
    GET  /api/guide              使用说明（实时状态）
    GET  /api/routes             脚本组 / 类目路线概览
    GET  /api/cooldown           地区特产 48 小时冷却一览（哪个还在冷却、还要等多久）
    POST /api/cooldown/manual    登记 / 清除某种材料的"我刚采过"
    GET  /api/transactions       可回滚事务
    POST /api/rollback           回滚
    GET  /api/bettergi-log       读 BetterGI 日志尾部
    POST /api/open               打开本地目录
    POST /api/shutdown           停掉 Agent（与通道）并退出 Studio
"""

import datetime
import os
import sys
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

import config  # noqa: E402

from skills import env_config, health_check  # noqa: E402
from studio import logs as bgi_logs  # noqa: E402
from studio.agent_runner import AgentRunner  # noqa: E402
from studio.channel_runner import ChannelManager  # noqa: E402

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")


def _json_error(message, status=400):
    return jsonify({"ok": False, "error": str(message)}), status


def _int_arg(name, default, minimum=0, maximum=None):
    """读一个整数查询参数：非数字/超范围都退回默认值（**不要**让 `?since=abc` 变成 500）。"""
    raw = request.args.get(name)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def warmup():
    """提前把重量级模块 import 完，别让玩家第一次打开页面就等十几秒。

    实测踩坑：`/api/state` 会用到 `skills.bgi_controller` → `api.feishu_api` → `lark_oapi`，
    冷启动时这几层 import 要好几秒；窗口刚打开就轮询，接口还没热起来 → 前端看起来"连不上"。
    """
    import time as _time

    started = _time.time()
    for module in (
        "skills.bgi_controller",
        "skills.health_check",
        "skills.env_reader",
        "brain.memory_manager",
    ):
        try:
            __import__(module)
        except Exception:
            pass
    return _time.time() - started


def create_app(runner=None, env_path=None, channels=None):
    """建 Flask app。测试可以传入自己的 runner / 通道管理器 / env 路径。"""
    app = Flask(__name__, static_folder=None)
    runner = runner or AgentRunner(PROJECT_ROOT)
    env_path = env_path or ENV_PATH
    app.config["RUNNER"] = runner
    app.config["ENV_PATH"] = env_path
    app.config["CHANNELS"] = channels if channels is not None else ChannelManager(
        PROJECT_ROOT, env_loader=lambda: env_config.load_env(env_path)
    )
    app.config["LAST_REQUEST"] = [time.time()]
    app.config["ENV_WARMING"] = True

    @app.before_request
    def _touch():
        app.config["LAST_REQUEST"][0] = time.time()

    # ---------- 静态页面 ----------
    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/<path:filename>")
    def static_files(filename):
        return send_from_directory(WEB_DIR, filename)

    # ---------- 状态 ----------
    @app.get("/api/state")
    def api_state():
        snapshot = runner.snapshot(since=_int_arg("since", 0))
        snapshot.update(_environment_summary())
        channels = app.config.get("CHANNELS")
        # 只给摘要（进程状态 / 有没有配置 / 自动启动开没开）；日志走 /api/channels
        snapshot["channels"] = [channels.view(name) for name in channels.runners] if channels else []
        return jsonify(snapshot)

    def _environment_summary(force=False):
        """环境摘要：**带缓存**。

        前端每 1.2 秒问一次状态，而这些信息的代价不小：
        `resolve_one_dragon_config_path()` 每次都会打印一行"当前生效的一条龙配置"，
        `_route_counts()` 要遍历 AutoPathing 下 4400+ 个文件。缓存 10 秒足够新鲜。
        """
        cache = app.config.setdefault("ENV_CACHE", {"at": 0.0, "data": None, "ttl": 10.0})
        now = time.time()
        if not force and cache["data"] and now - cache["at"] < cache["ttl"]:
            return cache["data"]

        values = env_config.load_env(app.config["ENV_PATH"])
        if app.config.get("ENV_WARMING"):
            return {
                "project_root": PROJECT_ROOT,
                "provider": values.get("LLM_PROVIDER") or os.getenv("LLM_PROVIDER") or "",
                "model": values.get("MODEL_NAME") or os.getenv("MODEL_NAME") or "",
                "uid": values.get("DEFAULT_UID") or os.getenv("DEFAULT_UID") or "",
                "env_path": app.config["ENV_PATH"],
                "bgi_dir": config.BGI_DIR,
                "bgi_running": False,
                "bgi_running_label": "环境加载中",
                "one_dragon": "",
                "showcase_age": "",
                "routes": {},
                "environment_ready": False,
            }

        from skills import bgi_controller

        summary = {
            "project_root": PROJECT_ROOT,
            "provider": values.get("LLM_PROVIDER") or os.getenv("LLM_PROVIDER") or "",
            "model": values.get("MODEL_NAME") or os.getenv("MODEL_NAME") or "",
            "uid": values.get("DEFAULT_UID") or os.getenv("DEFAULT_UID") or "",
            "env_path": app.config["ENV_PATH"],
            "bgi_dir": config.BGI_DIR,
            "bgi_running": health_check.bettergi_running_status() is True,
            "bgi_running_label": health_check.bettergi_running_label(),
            "one_dragon": "",
            "showcase_age": "",
            "routes": _route_counts(),
            "environment_ready": True,
        }
        try:
            # 这行会往 stdout 打一句「当前生效的一条龙配置是『X』」，界面上本来就显示了；
            # 每轮读一次都会打一遍会刷屏，这里吞掉。
            import io

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                summary["one_dragon"] = os.path.basename(bgi_controller.resolve_one_dragon_config_path())
        except Exception:
            summary["one_dragon"] = ""
        try:
            from brain import memory_manager
            from skills.env_reader import env_context_age_note

            summary["showcase_age"] = env_context_age_note(memory_manager.load_chat_store())
        except Exception:
            summary["showcase_age"] = ""

        cache.update(at=now, data=summary)
        return summary

    def _route_counts():
        """各路线类目在 AutoPathing 里的条数（首页卡片用）。"""
        from skills import route_group

        rows = {}
        # Include the gather category here as well: it is kept outside
        # category_specs() for matching purposes, but it is a normal
        # user-visible route category ("地图素材") in the Studio.
        for action, spec in route_group.all_category_specs().items():
            if not spec.folder_prefix:
                continue
            directory = os.path.join(config.BGI_AUTO_PATHING_DIR, spec.folder_prefix)
            rows[action] = {
                "label": spec.label,
                "count": len(route_group.route_files_under(directory)) if os.path.isdir(directory) else 0,
            }
        return rows

    # ---------- Agent 进程 ----------
    @app.post("/api/agent/start")
    def api_start():
        result = runner.start()
        # 玩家要的逻辑：点「启动 Agent」时顺带把"已配置 + 开着自动启动"的远程通道拉起来
        if result.get("ok"):
            channels = app.config.get("CHANNELS")
            result["channels"] = channels.start_automatic() if channels is not None else []
        return jsonify(result)

    @app.post("/api/agent/stop")
    def api_stop():
        result = runner.stop()
        channels = app.config.get("CHANNELS")
        if channels is not None:
            result["channels"] = channels.stop_all()
        return jsonify(result)

    @app.post("/api/agent/input")
    def api_input():
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text") or "")
        if not text.strip():
            return _json_error("输入是空的")
        return jsonify(runner.send(text))

    # ---------- 远程通道（QQ 机器人 / 飞书） ----------
    @app.get("/api/channels")
    def api_channels():
        channels = app.config.get("CHANNELS")
        if channels is None:
            return jsonify({"ok": True, "channels": []})
        since = {}
        for name in channels.runners:
            raw = request.args.get(f"since_{name}", "")
            since[name] = int(raw) if str(raw).isdigit() else 0
        return jsonify(channels.snapshot(since))

    @app.post("/api/channels/<name>/start")
    def api_channel_start(name):
        channels = app.config.get("CHANNELS")
        if channels is None:
            return _json_error("通道管理不可用")
        return jsonify(channels.start(name))

    @app.post("/api/channels/<name>/stop")
    def api_channel_stop(name):
        channels = app.config.get("CHANNELS")
        if channels is None:
            return _json_error("通道管理不可用")
        return jsonify(channels.stop(name))

    @app.post("/api/channels/<name>/clear")
    def api_channel_clear(name):
        channels = app.config.get("CHANNELS")
        if channels is None:
            return _json_error("通道管理不可用")
        return jsonify(channels.clear(name))

    # ---------- 鏃ュ織 ----------
    @app.get("/api/log")
    def api_log():
        return jsonify(runner.snapshot(since=_int_arg("since", 0)))

    @app.post("/api/log/clear")
    def api_log_clear():
        return jsonify(runner.clear())

    # ---------- 閰嶇疆 ----------
    @app.get("/api/config")
    def api_config():
        values = env_config.load_env(app.config["ENV_PATH"])
        groups = []
        for group in env_config.FIELD_GROUPS:
            fields = []
            for field in group.fields:
                fields.append(
                    {
                        "key": field.key,
                        "label": field.label,
                        "kind": field.kind,
                        "choices": list(field.choices),
                        "help": field.help,
                        "default": field.default,
                        "value": values.get(field.key, field.default),
                        "is_set": field.key in values,
                    }
                )
            groups.append({"title": group.title, "fields": fields})
        return jsonify(
            {
                "ok": True,
                "path": app.config["ENV_PATH"],
                "exists": os.path.isfile(app.config["ENV_PATH"]),
                "groups": groups,
            }
        )

    @app.post("/api/config")
    def api_config_save():
        """保存配置：**只更新传进来的键**，没传的键保持原值。

        ⚠️ 这里绝对不能 `{key: values.get(key, "") for key in ALL_FIELDS}` ——
        前端是按分组页提交的（比如只改了「模型」页），把所有键补成空串会把
        玩家其余配置（BGI 路径、脚本组名、路线策略…）一次性抹掉。
        """
        payload = request.get_json(silent=True) or {}
        incoming = payload.get("values")
        if not isinstance(incoming, dict) or not incoming:
            return _json_error("没有收到任何配置项")

        values = {str(key): ("" if value is None else str(value)) for key, value in incoming.items()}
        unknown = [key for key in values if key not in env_config.ALL_FIELDS]
        if unknown:
            return _json_error(f"不认识的配置键：{'、'.join(unknown)}")

        current = env_config.load_env(app.config["ENV_PATH"])
        merged = {**current, **values}
        merged = {key: merged.get(key, "") for key in env_config.ALL_FIELDS}

        warnings = env_config.validate_values(merged)
        try:
            backup = env_config.save_env(app.config["ENV_PATH"], values)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "warnings": warnings})
        return jsonify(
            {
                "ok": True,
                "backup": backup,
                "warnings": warnings,
                "saved": sorted(values),
            }
        )

    # ---------- 米游社个人战绩（可选） ----------
    @app.get("/api/mys")
    def api_mys_status():
        """状态：cookie 配了没、缓存有多少角色、多久前拉的（**不发请求**）。

        ⚠️ 只回掩码，绝不把 cookie 原样吐回给前端。
        """
        try:
            from skills import mys_api

            cookie = str(config.MYS_COOKIE or "")
            snapshot = mys_api.load_snapshot()
            return jsonify({
                "ok": True,
                "configured": bool(cookie.strip()),
                "masked": mys_api.mask_cookie(cookie),
                "count": len((snapshot or {}).get("avatars") or []),
                "age": mys_api.describe_snapshot_age(snapshot),
                "nickname": (snapshot or {}).get("nickname") or "",
                "uid": (snapshot or {}).get("uid") or "",
                "ttl_hours": config.MYS_CACHE_TTL_HOURS,
            })
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    @app.post("/api/mys/check")
    def api_mys_check():
        """验证 cookie：问一次米游社（1 次请求）能不能读到角色。

        前端可能在「还没保存」时点它，所以允许 body 里带 `cookie` 覆盖当前 .env 里的值。
        """
        payload = request.get_json(silent=True) or {}
        candidate = str(payload.get("cookie") or "").strip() or str(config.MYS_COOKIE or "")
        if not candidate:
            return jsonify({"ok": False, "error": "没有配置米游社 Cookie"})

        try:
            from skills import mys_api

            roles = mys_api.fetch_roles(cookie=candidate)
            if not roles:
                return jsonify({
                    "ok": False,
                    "error": "cookie 有效，但名下没有原神角色（hk4e_cn）—— 是不是登成星铁/绝区零的号了？",
                })
            return jsonify({
                "ok": True,
                "roles": [
                    {"nickname": role.get("nickname"), "uid": role.get("uid"),
                     "server": role.get("server"), "level": role.get("level")}
                    for role in roles
                ],
            })
        except Exception as exc:        # noqa: BLE001 —— MysError 的文案本来就是给人看的
            return jsonify({"ok": False, "error": str(exc)})

    @app.post("/api/mys/refresh")
    def api_mys_refresh():
        """强制拉一次全角色名单（玩家主动点的时候才拉；平时只在"问到展柜外角色"时按需拉）。"""
        if not str(config.MYS_COOKIE or "").strip():
            return jsonify({"ok": False, "error": "没有配置米游社 Cookie"})
        try:
            from skills import mys_api

            snapshot = mys_api.refresh_snapshot()
            return jsonify({
                "ok": True,
                "count": len(snapshot.get("avatars") or []),
                "nickname": snapshot.get("nickname") or "",
                "uid": snapshot.get("uid") or "",
            })
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})

    # ---------- 缁存姢 ----------
    @app.post("/api/refresh-env")
    def api_refresh_env():
        try:
            from brain import memory_manager
            from skills.env_reader import refresh_store_env_context

            store = memory_manager.load_chat_store()
            uid = store.get("uid", config.DEFAULT_UID)
            if not uid:
                return _json_error("还没配 DEFAULT_UID，展柜抓不了")
            refreshed, notice = refresh_store_env_context(store, uid, force=True)
            memory_manager.save_chat_store(store)
            return jsonify({"ok": True, "notice": notice, "refreshed": bool(refreshed)})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @app.get("/api/doctor")
    def api_doctor():
        try:
            rows = health_check.run_health_check(app.config["ENV_PATH"])
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify(
            {
                "ok": True,
                "rows": [{"level": level, "text": text} for level, text in rows],
                "summary": {
                    "errors": sum(1 for level, _ in rows if level == "error"),
                    "warns": sum(1 for level, _ in rows if level == "warn"),
                },
            }
        )

    @app.get("/api/guide")
    def api_guide():
        """「使用说明」页的实时数据：上手清单 + 该添加哪些调度器。"""
        from studio import guide

        try:
            data = guide.build_guide(env_path=app.config["ENV_PATH"])
        except Exception as exc:  # 说明书页面永远要能打开
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify(data)

    @app.post("/api/repair")
    def api_repair():
        """一键修复：清掉 Agent 加过又跑不起来的任务项 + 关掉 Boss 脚本的启动编辑器。"""
        import contextlib
        import io

        from skills import bgi_controller

        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                code = bgi_controller.repair_agent_tasks(
                    force=bool((request.get_json(silent=True) or {}).get("force"))
                )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "lines": buffer.getvalue().splitlines()})
        return jsonify(
            {
                "ok": code == 0,
                "code": code,
                "lines": [line for line in buffer.getvalue().splitlines() if line.strip()],
            }
        )

    @app.get("/api/routes")
    def api_routes():
        from skills import bgi_controller, route_group

        registered = bgi_controller._registered_task_names()
        groups = []
        for path in route_group.list_group_files(config.BGI_SCRIPT_GROUP_DIR):
            group = route_group.load_group(path)
            if not group:
                continue
            name = str(group.get("name") or os.path.basename(path))
            pathing = ((group.get("config") or {}).get("pathingConfig") or {})
            strategy = str((pathing.get("autoFightConfig") or {}).get("strategyName") or "")
            groups.append(
                {
                    "name": name,
                    "file": os.path.basename(path),
                    "projects": route_group.group_size(group),
                    "pathing": sum(
                        1 for project in group.get("projects") or [] if route_group._is_pathing_project(project)
                    ),
                    "strategy": strategy,
                    "strategy_ok": not route_group.group_strategy_notice(group),
                    "registered": name in registered,
                    "enabled": name in registered
                    and name
                    in {
                        str(v)
                        for v in (
                            _enabled_task_names().values()
                        )
                    },
                }
            )
        groups.sort(key=lambda row: row["name"])

        categories = []
        # The route page should show every user-visible category, including
        # gather ("地图素材"), which is intentionally separate in matching.
        for action, spec in route_group.all_category_specs().items():
            directory = os.path.join(config.BGI_AUTO_PATHING_DIR, spec.folder_prefix or "")
            group_path = route_group.category_group_path(spec)
            categories.append(
                {
                    "action": action,
                    "label": spec.label,
                    "prefix": spec.folder_prefix,
                    "count": len(route_group.route_files_under(directory)) if spec.folder_prefix else 0,
                    "note": "不含地图素材；地图素材来自「地方特产」目录" if action == "cook" else "",
                    "group": os.path.basename(group_path),
                    "group_exists": os.path.isfile(group_path),
                }
            )
        return jsonify({"ok": True, "groups": groups, "categories": categories})

    @app.get("/api/update")
    def api_update():
        """版本检查（本地 VERSION vs GitHub 最新 release）。

        `?force=1` 忽略缓存立刻重查（Studio 上点「检查更新」用）；默认吃
        `UPDATE_CHECK_HOURS`（6 小时）的缓存，免得每次开页面都打 GitHub 的匿名接口。

        ⚠️ 两个 `ok` 不是一回事：接口的 `ok` 表示"这次请求成了"；
        检查本身的结论看 `status`（`newer`/`same`/`older`/`unknown`/`error`/`disabled`），
        另有 `check_ok` 是检查模块自己的布尔。检查失败**不是**接口失败（页面照常显示那张卡）。
        """
        from skills import update_check

        force = str(request.args.get("force") or "").strip().lower() in ("1", "true", "yes", "on")
        try:
            result = update_check.check(force=force)
        except Exception as exc:            # noqa: BLE001 —— 检查更新永远不该把页面打成 500
            return _json_error(f"检查更新失败：{type(exc).__name__} {exc}", 500)

        payload = dict(result)
        payload["check_ok"] = bool(result.get("ok"))
        payload["ok"] = True
        return jsonify(payload)

    @app.get("/api/cooldown")
    def api_cooldown():
        """四类世界资源的冷却一览（Studio 的「资源冷却」页）。

        数据来自 `skills.gather_cooldown.overview()`：一次算完全部材料（内部只解析一遍日志）。
        类别：地区特产 48 / 矿物 72（按材料还分档）/ 食材与炼金 24 / 敌人与魔物 12 小时。
        """
        from skills import gather_cooldown

        try:
            data = gather_cooldown.overview()
        except Exception as exc:            # noqa: BLE001 —— 页面不该因为日志读不到就 500
            return _json_error(f"读取冷却失败：{type(exc).__name__} {exc}", 500)

        def serialize(result):
            last = result.get("last_at")
            return {
                "material": result["material"],
                "category": result.get("category", ""),
                "category_label": result.get("category_label", ""),
                "hours": result.get("hours", data["hours"]),
                "cooling": bool(result["cooling"]),
                "known": bool(result["known"]),
                "partial": bool(result["partial"]),
                "manual": bool(result["manual"]),
                "hours_left": round(result["hours_left"], 2),
                "hours_left_text": (
                    gather_cooldown.human_hours(result["hours_left"]) if result["cooling"] else ""
                ),
                "hours_ago": None if result["hours_ago"] is None else round(result["hours_ago"], 2),
                "last_at": last.strftime("%m-%d %H:%M") if isinstance(last, datetime.datetime) else "",
                "last_ts": last.timestamp() if isinstance(last, datetime.datetime) else None,
                "total_routes": result["total_routes"],
                "ran_routes": result["ran_routes"],
                "describe": gather_cooldown.describe(result),
            }

        sections = [
            {
                "key": section["key"],
                "label": section["label"],
                "hours": section["hours"],
                "note": section["note"],
                "summary": section["summary"],
                "materials": [serialize(row) for row in section["materials"]],
            }
            for section in data["sections"]
        ]

        return jsonify({
            "ok": True,
            "hours": data["hours"],
            "category_hours": data["category_hours"],
            "min_route_percent": data["min_route_percent"],
            "band_enabled": data["band_enabled"],
            "scanned_days": data["scanned_days"],
            "events": data["events"],
            "generated_at": data["now"].strftime("%H:%M:%S"),
            "summary": data["summary"],
            "sections": sections,
            "materials": [serialize(row) for row in data["materials"]],
        })

    @app.post("/api/cooldown/manual")
    def api_cooldown_manual():
        """登记 / 清除某种材料的"我刚在游戏里采过"（等价于 CLI 的 --manual / --clear）。

        请求体：{"material": "霜仙花", "action": "mark" | "clear"}
        """
        from skills import gather_cooldown

        payload = request.get_json(silent=True) or {}
        material = str(payload.get("material") or "").strip()
        action = str(payload.get("action") or "mark").strip().lower()
        if not material:
            return _json_error("material 不能为空")
        if action not in ("mark", "clear"):
            return _json_error("action 只能是 mark 或 clear")

        if action == "mark":
            ok = gather_cooldown.mark_manual(material)
            message = f"已登记「{material}」为刚采过（48 小时内不再排它）"
        else:
            ok = gather_cooldown.clear_manual(material)
            message = f"已清除「{material}」的冷却记录"
        if not ok:
            return _json_error("写入 memory/gather_cooldown_manual.json 失败（磁盘权限？）", 500)
        return jsonify({"ok": True, "message": message, "action": action, "material": material})

    def _enabled_task_names():
        from skills import bgi_controller

        try:
            with open(bgi_controller.resolve_one_dragon_config_path(), "r", encoding="utf-8") as handle:
                import json

                data = json.load(handle)
        except Exception:
            return {}
        definitions = data.get("TaskDefinitions") or {}
        return {
            str(task_id): str(name)
            for task_id, name in definitions.items()
            if (data.get("TaskEnabledList") or {}).get(task_id)
        }

    @app.get("/api/transactions")
    def api_transactions():
        from skills.config_recovery import list_transactions

        try:
            items = list_transactions(config.BGI_BACKUP_DIR)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({"ok": True, "backup_dir": config.BGI_BACKUP_DIR, "items": items})

    @app.post("/api/rollback")
    def api_rollback():
        from skills.config_recovery import restore_transaction

        payload = request.get_json(silent=True) or {}
        transaction_id = str(payload.get("id") or "").strip()
        if not transaction_id:
            return _json_error("没给事务 ID")
        try:
            result = restore_transaction(config.BGI_BACKUP_DIR, transaction_id)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify(
            {
                "ok": True,
                "restored": [path.name for path in result.restored_files],
                "backup": str(result.backup_dir),
            }
        )

    # ---------- BetterGI 鏃ュ織 ----------
    @app.get("/api/bettergi-log")
    def api_bettergi_log():
        files = bgi_logs.list_log_files(config.BGI_DIR)
        known = {row["name"] for row in files}
        selected = str(request.args.get("file") or "")
        if selected and selected not in known:
            # ⚠️ 以前这里直接把 file 拼进路径：`?file=C:\Windows\win.ini` 或 `..\..\x.log`
            # 就能读任意文本文件的尾部（本机服务，但没必要留这个口子）。只认日志列表里的名字。
            return _json_error(f"不认识的日志文件：{selected}")
        if not selected:
            selected = files[0]["name"] if files else ""
        path = os.path.join(bgi_logs.log_dir(config.BGI_DIR), selected) if selected else ""
        rows = bgi_logs.read_tail(
            path,
            lines=_int_arg("lines", 300),
            keyword=request.args.get("keyword", ""),
        )
        return jsonify(
            {
                "ok": True,
                "dir": bgi_logs.log_dir(config.BGI_DIR),
                "files": [{"name": row["name"], "size": row["size"], "mtime": row["mtime"]} for row in files],
                "selected": selected,
                "rows": rows,
                "summary": bgi_logs.summarise(rows),
                "highlight": bgi_logs.find_first_problem(rows),
            }
        )

    # ---------- 鏉傞」 ----------
    @app.post("/api/open")
    def api_open():
        payload = request.get_json(silent=True) or {}
        targets = {
            "project": PROJECT_ROOT,
            "bgi_dir": config.BGI_DIR,
            "bgi_log": bgi_logs.log_dir(config.BGI_DIR),
            "backups": config.BGI_BACKUP_DIR,
            "one_dragon": os.path.join(config.BGI_DIR, "User", "OneDragon"),
            "script_group": config.BGI_SCRIPT_GROUP_DIR,
        }
        # 也允许打开 GitHub 链接（「版本更新」页的「打开发布页」）。
        # 只放行 github.com：这接口会直接交给系统默认程序，不能让人塞任意 URL / 本地文件进来。
        url = str(payload.get("url") or "").strip()
        if url:
            if not url.startswith(("https://github.com/", "http://github.com/",
                                   "https://www.github.com/")):
                return _json_error("只允许打开 GitHub 链接")
            try:
                os.startfile(url)  # noqa: S606
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)})
            return jsonify({"ok": True, "url": url})

        target = str(payload.get("target") or "")
        path = targets.get(target)
        if not path or not os.path.exists(path):
            return _json_error(f"路径不存在：{path or target}")
        try:
            os.startfile(path)  # noqa: S606
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({"ok": True, "path": path})

    @app.post("/api/shutdown")
    def api_shutdown():
        runner.stop()
        channels = app.config.get("CHANNELS")
        if channels is not None:
            channels.stop_all()
        # 让响应先发出去再退出；测试里把 SHUTDOWN_DELAY 设成 None 就不会真的杀进程
        delay = app.config.get("SHUTDOWN_DELAY", 0.4)
        if delay is not None:
            threading.Timer(float(delay), lambda: os._exit(0)).start()
        return jsonify({"ok": True})

    return app


def main(argv=None):  # pragma: no cover - 需要真实端口
    app = create_app()
    app.run(host="127.0.0.1", port=0, threaded=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
