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
    GET  /api/growth             养成系统全量状态（目标 / 计划 / 库存 / 历史）
    POST /api/growth/sync        同步米游社库存（养成系统的"材料拉取"入口）
    GET  /api/growth/characters  米游社账号里的角色一览（用来添加养成目标）
    GET  /api/growth/targets     角色养成目标列表
    POST /api/growth/targets     新增 / 更新一个角色目标
    POST /api/growth/targets/bulk 批量新增角色目标
    POST /api/growth/targets/bulk-update 批量修改全部角色目标
    PUT  /api/growth/targets/<id>   更新一个角色目标
    DELETE /api/growth/targets/<id> 删除一个角色目标
    POST /api/growth/targets/reorder 拖拽排序
    GET  /api/growth/plan        取当前养成计划（可按需重算）
    POST /api/growth/plan        重新生成养成计划
    POST /api/growth/plan/execute 执行计划里可执行的 BetterGI 任务
    GET  /api/growth/history     同步历史 / 执行历史
    GET  /api/transactions       可回滚事务
    POST /api/rollback           回滚
    GET  /api/bettergi-log       读 BetterGI 日志尾部
    POST /api/open               打开本地目录
    POST /api/shutdown           停掉 Agent（与通道）并退出 Studio
"""

import contextlib
import datetime
import os
import shutil
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

    顺带在这里挂上**养成系统的两件开机动作**（都放后台线程，绝不阻塞界面）：
      ① 把"BetterGI 跑完 → 重新同步米游社"的联动注册上（§19 的核心机制）；
      ② 按配置自动同步一次米游社库存（§5，默认"每次启动"）。
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
    _startup_growth_sync()
    return _time.time() - started


# 自动同步的进程内状态：只在"本次启动"里生效，不写盘。
_GROWTH_SYNC_STATE = {"started": False, "at": 0.0}


def _startup_growth_sync():
    """按 `GROWTH_SYNC_MODE` / `GROWTH_STARTUP_SYNC` 决定要不要开机同步米游社库存。

    ⚠️ 只在一个后台线程里跑，并且**失败只打印**：米游社风控/断网时 Studio 必须照常可用。
    """
    if not getattr(config, "GROWTH_STARTUP_SYNC", True):
        return False
    mode = str(getattr(config, "GROWTH_SYNC_MODE", "startup") or "startup").strip().lower()
    if mode == "manual":
        return False

    interval = 0.0
    if mode not in ("startup", "manual"):
        try:
            interval = max(1.0, float(mode)) * 60.0
        except ValueError:
            interval = 0.0

    now = time.time()
    if _GROWTH_SYNC_STATE["started"] and interval <= 0:
        return False                                     # 每次启动模式：本次进程只同步一次
    if _GROWTH_SYNC_STATE["started"] and now - _GROWTH_SYNC_STATE["at"] < interval:
        return False

    def _worker():
        try:
            from brain import growth_planner

            growth_planner.register_watcher_hook()
            result = growth_planner.sync_inventory()
            if result.get("ok"):
                print(f"✅ 米游社库存已同步：{result.get('item_count')} 种材料"
                      f"（快照 {os.path.basename(result.get('snapshot_file') or '')}）")
            elif result.get("skipped"):
                # 背包接口已经下线，这个数据源**不用了**（材料的「已有/还差」走养成计算器）——
                # 开机时不该为它报一句"失败"，那只会让人以为功能坏了。
                pass
            else:
                print(f"⚠️ 米游社库存同步失败（沿用上一次成功快照）："
                      f"{result.get('error')}")
            # ★ 每次启动都把"今天要执行的目标"推给玩家（`GROWTH_STARTUP_PUSH`）。
            #   为什么必须推：米游社的"已有/还差"是算出来的，BetterGI 跑完那一刻
            #   背包还没刷新，所以今天的执行目标只能靠推送告诉玩家。
            if getattr(config, "GROWTH_STARTUP_PUSH", True):
                try:
                    growth_planner.push_execution_targets()
                except Exception as exc:    # noqa: BLE001 —— 推送失败不该影响开机
                    print(f"⚠️ 启动推送执行目标失败：{type(exc).__name__} {exc}")
        except Exception as exc:        # noqa: BLE001 —— 开机动作绝不能影响 Studio
            print(f"⚠️ 启动时同步米游社库存失败：{type(exc).__name__} {exc}")
        finally:
            _GROWTH_SYNC_STATE["started"] = True
            _GROWTH_SYNC_STATE["at"] = time.time()

    threading.Thread(target=_worker, daemon=True, name="growth-startup-sync").start()
    return True


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

    # 🌟 养成系统与 BetterGI 的联动："一条龙跑完 → 重新同步米游社 → 重算缺口"（§19）。
    #    放在这里而不是只在 warmup() 里，是因为测试 / 嵌入式用法不一定调 warmup()，
    #    而漏注册的表现是"跑完任务库存永远不更新"——很难查。注册是幂等的。
    try:
        from brain import growth_planner

        growth_planner.register_watcher_hook()
    except Exception as exc:            # noqa: BLE001 —— 注册失败不该让 Studio 起不来
        print(f"⚠️ 养成系统联动注册失败（任务完成后不会自动重新同步）：{type(exc).__name__} {exc}")

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

        # 养成系统的库存状态（概览页那张卡片用）。**不发网络请求**，纯读本地快照。
        try:
            from brain import growth_db
            from skills import mys_inventory

            status = mys_inventory.status()
            known = mys_inventory.load_known_materials()
            summary["growth"] = {
                "configured": bool(status.get("configured")),
                "available": bool(status.get("available")),
                "stale": bool(status.get("stale")),
                "age_text": status.get("age_text") or "从未同步",
                "item_count": int(status.get("item_count") or 0),
                # ⚠️ 材料数据现在由养成计算器提供（背包接口已下线），
                #    "库存快照"那个概念在概览页上别再当主指标了。
                "known_count": len(known.get("items") or []),
                "known_at": known.get("fetched_at") or "",
                "targets": len(growth_db.list_targets(enabled_only=True)),
            }
        except Exception as exc:        # noqa: BLE001 —— 概览页不该因为养成模块挂掉就空白
            summary["growth"] = {"error": f"{type(exc).__name__} {exc}"}

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
            # ⚠️ 能列出角色 ≠ cookie 完整：只有 v2 键的 cookie 这一关能过，
            # 但养成计算器与战绩接口会全部失败（实测踩过）。所以这里顺带把缺的键说出来，
            # 否则玩家看到"验证通过"就去拉材料，然后对着"未登录"一头雾水。
            audit = mys_api.audit_cookie(candidate)
            return jsonify({
                "ok": True,
                "roles": [
                    {"nickname": role.get("nickname"), "uid": role.get("uid"),
                     "server": role.get("server"), "level": role.get("level")}
                    for role in roles
                ],
                "complete": audit["ok"],
                "missing_keys": ["/".join(item["keys"]) for item in audit["missing"]],
                "warning": "" if audit["ok"] else audit["hint"],
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

    # ---------- 扫码登录（自动取回 cookie，省掉 F12 那一套） ----------
    @app.get("/api/mys/login/status")
    def api_mys_login_status():
        """登录状态：配了没、完不完整、缺什么、缺 Windows DPAPI 之类能不能用。"""
        try:
            from skills import mys_login

            return jsonify({"ok": True, "status": mys_login.status()})
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__} {exc}"})

    @app.post("/api/mys/login/start")
    def api_mys_login_start():
        """出一张登录二维码，并把 SVG 一起返回（前端直接画在页面上）。

        **为什么是这条路**：`ltoken`（计算器/战绩接口唯一认的那个）只在真正的登录流程里发放，
        从浏览器 cookie 库里翻出来的往往只有 `*_v2` 那套。所以这里让玩家用米游社 App 扫码，
        我们直接调米游社的"游戏账号扫码登录"接口，拿 stoken 换 **v1 ltoken** ——
        全程不碰浏览器、不碰 DPAPI、不碰 cookie 库。

        二维码只活两分钟左右，过期了前端会调这个接口再要一张。
        """
        try:
            from skills import mys_login

            started = mys_login.start_qr_login()
            svg = mys_login.render_qr_svg(started["url"])
        except Exception as exc:        # noqa: BLE001 —— LoginError 的文案本来就是给人看的
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({
            "ok": True,
            "url": started["url"],
            "svg": svg,
            "expires_in": mys_login.qr_expires_in(),
            "note": "用手机「米游社」App 扫码，再在手机上点「确认登录」。",
        })

    @app.get("/api/mys/login/poll")
    def api_mys_login_poll():
        """轮询二维码状态；一旦确认就换 token、验证、写 `.env`。

        状态：`pending`（还没扫）/ `scanned`（扫了，等手机点确认）/ `done` /
        `expired`（点「换一张」）/ `cancelled` / `failed`。

        `?cancel=1` 表示前端点了「取消」——丢弃这张码，不做任何保存。
        """
        try:
            from skills import mys_login

            if str(request.args.get("cancel") or "").strip().lower() in ("1", "true", "yes"):
                mys_login.cancel_qr_login()
                return jsonify({"ok": True, "state": "cancelled"})

            session = mys_login.qr_state()
            if not session["ticket"]:
                return jsonify({"ok": False, "state": "idle",
                                "error": "还没有二维码，先点「扫码登录」"})

            result = mys_login.query_qr_login()
            phase = mys_login.qr_phase(result["status"])
            if phase in ("pending", "unknown"):
                return jsonify({"ok": True, "state": "pending",
                                "raw": result["status"],
                                "expires_in": mys_login.qr_expires_in()})
            if phase == "scanned":
                return jsonify({"ok": True, "state": "scanned",
                                "expires_in": mys_login.qr_expires_in()})
            if phase == "expired":
                return jsonify({"ok": True, "state": "expired",
                                "note": "二维码过期了（它只活两分钟），点「换一张」重新扫。"})

            # confirmed：换 v1 ltoken → 验证 → 落盘
            exchanged = mys_login.exchange_tokens(result["data"])
            final = mys_login.finish_login(cookie=exchanged["cookie"], save=True,
                                           env_path=app.config["ENV_PATH"])
            if not final["ok"]:
                return jsonify({"ok": False, "state": "failed", "error": final["error"],
                                "notes": final["notes"]})
            mys_login.cancel_qr_login()          # 这张码用完就丢，别留着被重放

            # 登录成功顺手同步一次真实库存：玩家扫码图的就是"材料能算出来"，
            # 不多拉这一下的话他还得自己再点一次「同步米游社库存」。
            # ⚠️ 失败不影响登录结果（风控/断网都可能），所以只带回一句话。
            inventory = {"ok": False, "avatars": 0, "error": ""}
            try:
                from skills import mys_inventory

                snapshot = mys_inventory.sync()
                inventory = {"ok": True,
                             "avatars": len((snapshot or {}).get("avatars") or []),
                             "error": ""}
            except Exception as exc:            # noqa: BLE001
                inventory["error"] = f"{type(exc).__name__} {exc}"

            return jsonify({
                "ok": True,
                "state": "done",
                "masked": final["cookie_masked"],
                "user": final["user"],
                "backup": final.get("backup") or "",
                "inventory": inventory,
                "note": "cookie 已写入 .env（含 ltoken），重启 Agent 后生效。",
            })
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "state": "failed",
                            "error": f"{type(exc).__name__} {exc}"})

    @app.post("/api/mys/login/app/start")
    def api_mys_login_app_start():
        """`/api/mys/login/start` 的别名（app 版扫码）。

        为什么要这个别名：前端有一版把路径拼成了 `/api/mys/login/app/start`，那个路径不存在，
        POST 落到 SPA 的兜底路由上 → 返回一坨 **405 Method Not Allowed 的 HTML**，
        玩家看到的就是"点扫码登录弹出一段乱码"（踩过）。两个路径都留着，谁拼都不会再撞。
        """
        return api_mys_login_start()

    @app.get("/api/mys/login/app/poll")
    def api_mys_login_app_poll():
        """`/api/mys/login/poll` 的别名（app 版扫码）。"""
        return api_mys_login_poll()

    @app.post("/api/mys/login/web/start")
    def api_mys_login_web_start():
        """出一张**网页版**登录二维码（给养成计算器换 v2 cookie 用）。

        **为什么还要第二个扫码入口**：上面那个 app 扫码换到的是 **v1 `ltoken`**
        （读体力 / 战绩认它），而**养成计算器**要的是网页那套 **`ltoken_v2`**。
        两条路换到的 cookie 键名不冲突，所以登录成功后会**合并**进同一份 MYS_COOKIE
        —— 一份 cookie 同时能算材料、读体力。
        """
        try:
            from skills import mys_login

            started = mys_login.start_web_qr_login()
            svg = mys_login.render_qr_svg(started["url"])
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({
            "ok": True,
            "url": started["url"],
            "svg": svg,
            "expires_in": mys_login.qr_expires_in(),
            "note": "用手机「米游社」App 扫码并确认 —— 这是**网页版**登录，"
                    "换来的是养成计算器要的那套 cookie（v2）。",
        })

    @app.get("/api/mys/login/web/poll")
    def api_mys_login_web_poll():
        """轮询网页扫码状态；确认后合并 cookie、验证计算器、写 `.env`。"""
        try:
            from skills import mys_login

            if str(request.args.get("cancel") or "").strip().lower() in ("1", "true", "yes"):
                mys_login.cancel_web_qr_login()
                return jsonify({"ok": True, "state": "cancelled"})

            session = mys_login.web_qr_state()
            if not session.get("ticket"):
                return jsonify({"ok": False, "state": "idle",
                                "error": "还没有网页二维码，先点「网页版扫码登录」"})
            result = mys_login.query_web_qr_login()
            phase = mys_login.qr_phase(result.get("status"))
            if phase in ("pending", "unknown"):
                return jsonify({"ok": True, "state": "pending",
                                "raw": result.get("status"),
                                "expires_in": mys_login.qr_expires_in()})
            if phase == "scanned":
                return jsonify({"ok": True, "state": "scanned",
                                "expires_in": mys_login.qr_expires_in()})
            if phase == "expired":
                return jsonify({"ok": True, "state": "expired",
                                "note": "网页二维码过期了，点「换一张」重新扫。"})

            cookies = result.get("cookies") or {}
            if not cookies:
                return jsonify({"ok": False, "state": "failed",
                                "error": "确认了但没拿到 cookie（米游社改版？）"})
            web_cookie = "; ".join(f"{key}={cookies[key]}"
                                   for key in mys_login.WEB_COOKIE_KEYS if cookies.get(key))
            # ★ 合并而不是覆盖：原来那份里的 v1 键（读体力要用）必须留着
            final_cookie = mys_login.merge_cookie(mys_login.config.MYS_COOKIE or "", web_cookie)
            ok_calc, note = mys_login.verify_calculator_cookie(final_cookie)
            saved = False
            if ok_calc:
                saved = mys_login._save_cookie(final_cookie, app.config["ENV_PATH"])
            mys_login.cancel_web_qr_login()
            return jsonify({
                "ok": bool(ok_calc),
                "state": "done" if ok_calc else "failed",
                "keys": sorted(cookies),
                "saved": saved,
                "error": "" if ok_calc else note,
                "note": ("✅ 网页登录成功：养成计算器已可用" +
                         ("，cookie 已合并写入 .env（v1 的体力键保留）" if saved else "")) if ok_calc
                        else f"⚠️ 拿到 cookie 但计算器仍不可用：{note}",
            })
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "state": "failed",
                            "error": f"{type(exc).__name__} {exc}"})

    @app.post("/api/mys/login/cancel")
    def api_mys_login_cancel():
        """取消扫码登录：丢掉这张二维码，不动任何配置。"""
        try:
            from skills import mys_login

            mys_login.cancel_qr_login()
            mys_login.close_login_window()       # 顺手关掉历史遗留的登录窗口（老路子留下的）
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({"ok": True})

    @app.post("/api/mys/login/manual")
    def api_mys_login_manual():
        """手动粘贴 cookie（**养成计算器网页那套也能用**）。

        请求体：`{"cookie": "..."}`，可以是
          · 一行 cookie（`ltuid=...; ltoken_v2=...`），或者
          · **整段 `Copy as cURL`** —— 我们自动把 `Cookie:` 那一行抠出来。

        ⚠️ 为什么要支持 cURL：养成计算器是**单独登录**的网页，它的会话里有一堆
        `DEVICEFP`/`_MHYUUID` 之类的 cookie，手抄容易漏。复制整段 cURL 最省事。
        验证仍然是真的（会打接口），验证不过不会保存。
        """
        payload = request.get_json(silent=True) or {}
        raw = str(payload.get("cookie") or "").strip()
        if not raw:
            return _json_error("cookie 是空的")
        try:
            from skills import mys_login

            cookie = mys_login.extract_cookie(raw)
            if not cookie:
                return _json_error("没从这段内容里认出 cookie（cookie 行或 Copy as cURL 都行）")
            result = mys_login.finish_login(cookie=cookie, save=True,
                                            env_path=app.config["ENV_PATH"])
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__} {exc}"})
        if not result["ok"]:
            return jsonify({"ok": False, "error": result["error"]})
        return jsonify({
            "ok": True,
            "masked": result["cookie_masked"],
            "user": result["user"],
            "note": "cookie 已写入 .env，重启 Agent 后生效。",
        })

    # ---------- 缁存姢 ----------
    # ---------- 角色养成（米游社库存 + 养成计算器 + BetterGI 执行） ----------
    def _growth():
        """懒加载养成模块：Studio 的其它页面不该因为养成模块 import 失败就全挂。"""
        from brain import growth_db, growth_planner, growth_models, material_planner
        from skills import mys_inventory, mys_calculator

        return growth_db, growth_planner, growth_models, material_planner, mys_inventory, mys_calculator

    @app.get("/api/growth")
    def api_growth():
        """养成系统全量状态：角色目标、当前计划、库存状态、同步与执行历史。

        ⚠️ 这个接口**不会**主动请求米游社（除了计划里按缓存判定的 compute）——
        它要给页面每几秒刷一次用。所以想刷新库存必须显式调 `POST /api/growth/sync`。
        """
        try:
            growth_db, growth_planner, growth_models, material_planner, mys_inventory, mys_calculator = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})

        try:
            plan = growth_planner.build_plan(record=False)
        except Exception as exc:        # noqa: BLE001 —— 页面永远要能打开
            plan = {"error": f"{type(exc).__name__} {exc}", "status": "BLOCKED"}

        return jsonify({
            "ok": True,
            "plan": plan,
            "targets": growth_db.list_targets(),
            "sort_mode": growth_planner.sort_mode(),
            "sort_modes": [
                {"value": mode, "label": growth_models.SORT_MODE_LABELS[mode]}
                for mode in growth_models.SORT_MODES
            ],
            "inventory": mys_inventory.status(),
            "phase_labels": growth_models.PHASE_LABELS,
            "state_labels": growth_models.STATE_LABELS,
            "task_type_labels": material_planner.TASK_TYPE_LABELS,
            "status_labels": material_planner.STATUS_LABELS,
            "snapshots": mys_inventory.list_snapshots(limit=10),
            "sync_history": growth_db.list_sync_history(limit=12),
            "sync_summary": growth_db.sync_summary(limit=50),
            "executions": growth_db.list_executions(limit=12),
            "mys": {
                "configured": _mys_configured(),
                "masked": _masked_cookie(),
            },
        })

    def _mys_configured():
        from skills import mys_api

        return bool(str(config.MYS_COOKIE or "").strip())

    def _masked_cookie():
        from skills import mys_api

        return mys_api.mask_cookie(config.MYS_COOKIE)

    @app.post("/api/growth/sync")
    def api_growth_sync():
        """**旧**的"同步米游社库存"入口（走背包接口）。

        ⚠️ 米游社已经关掉了那个接口，所以这个端点在真实环境里永远是「跳过」：
        材料的「已有 / 还差」现在由**养成计算器**在算材料时一起给
        （`/api/growth/plan` 里的 `known_inventory`）。
        保留它只为兼容旧前端/脚本，返回里带 `skipped=True`，前端据此**不报错**。
        """
        try:
            _, growth_planner, _, _, mys_inventory, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})

        payload = request.get_json(silent=True) or {}
        uid = str(payload.get("uid") or "").strip() or None
        try:
            result = growth_planner.sync_inventory(uid=uid)
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__} {exc}"})

        if not result.get("ok"):
            return jsonify({
                "ok": False,
                "error": result.get("error") or "同步失败",
                "error_kind": result.get("error_kind") or "",
                # `skipped` = 这个数据源不用了（不是错误）→ 前端别弹红字
                "skipped": bool(result.get("skipped")),
                "advice": "" if result.get("skipped")
                          else mys_inventory.error_advice(result.get("error_kind")),
                "stale": True,
                "last_success_at": result.get("last_success_at") or "",
                "inventory": mys_inventory.status(),
            })

        delta = [row for row in (result.get("delta") or []) if row.get("delta")][:12]
        return jsonify({
            "ok": True,
            "item_count": result.get("item_count") or 0,
            "snapshot_id": result.get("snapshot_id") or 0,
            "snapshot_file": os.path.basename(result.get("snapshot_file") or ""),
            "deposited": [row for row in delta if row["delta"] > 0],
            "consumed": [row for row in delta if row["delta"] < 0],
            "inventory": mys_inventory.status(),
            "plan": growth_planner.build_plan(record=False),
        })

    @app.get("/api/growth/characters")
    def api_growth_characters():
        """"添加培养目标"的候选角色。

        `?refresh=1` 会真的去请求米游社（默认吃缓存）。

        ⚠️ 这里**不能**再依赖"战绩"接口（`getUserGameRolesByCookie` 那套）—— 它现在被
        风控卡着、返回空列表，表现就是"添加培养目标一个都没有了"。现在的来源：
        养成计算器的「我的角色」（能用就用，带真实等级）→ **完整图鉴**（翻页 + 缓存 7 天，
        保证列表不为空）→ 本地记账过的角色。
        """
        try:
            _, growth_planner, growth_models, _, _, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})

        force = str(request.args.get("refresh") or "").strip().lower() in ("1", "true", "yes", "on")
        try:
            avatars = growth_planner.available_characters(force=force) or {}
        except Exception as exc:        # noqa: BLE001 —— 页面永远要能打开
            return jsonify({"ok": False, "configured": True, "characters": [],
                            "error": f"{type(exc).__name__} {exc}"})

        scan_status = growth_planner.ownership_scan_status()
        rows = []
        for avatar in avatars.values():
            level, talents, weapon_level = growth_models.current_levels(avatar)
            rows.append({
                "character_id": int(avatar["id"]),
                "name": avatar.get("name") or "",
                "rarity": int(avatar.get("rarity") or 0),
                "element": avatar.get("element") or "",
                "level": level,
                "talents": talents,
                "owned": bool(avatar.get("owned")),
                # `owned_known=False` means the account query has not confirmed it yet.
                "owned_known": bool(avatar.get("owned_known")),
                "weapon": {
                    "id": (avatar.get("weapon") or {}).get("id") or 0,
                    "name": (avatar.get("weapon") or {}).get("name") or "",
                    "level": weapon_level,
                },
                "summary": f"{level}/{talents['normal']}/{talents['skill']}/{talents['burst']}",
            })
        # 已确认拥有的排前面，再按星级、id
        rows.sort(key=lambda row: (not row["owned_known"], not row["owned"],
                                   -int(row["rarity"] or 0), row["character_id"]))
        owned = sum(1 for row in rows if row["owned_known"] and row["owned"])
        unknown = sum(1 for row in rows if not row["owned_known"])
        note = f"共 {len(rows)} 个角色：已知已拥有 {owned} 个，未知 {unknown} 个。"
        if unknown:
            if scan_status.get("error"):
                note += f"（拥有状态同步未完成：{scan_status['error']}；可以先手动选择。）"
            elif not scan_status.get("configured"):
                note += "（未配置米游社 Cookie，无法自动确认；可以手动选择。）"
            else:
                note += "（拥有状态尚未完成同步；可以先手动选择。）"
        return jsonify({
            "ok": True,
            "configured": bool(scan_status.get("configured")),
            "characters": rows,
            "note": note,
            "ownership": scan_status,
        })

    @app.get("/api/growth/targets")
    def api_growth_targets():
        try:
            growth_db, growth_planner, _, _, _, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})
        return jsonify({
            "ok": True,
            "targets": growth_db.list_targets(),
            "sort_mode": growth_planner.sort_mode(),
        })

    @app.post("/api/growth/targets")
    def api_growth_target_create():
        """新增 / 更新一个角色养成目标（同一个 character_id 重复提交 = 更新）。"""
        return _save_growth_target()

    @app.post("/api/growth/targets/bulk")
    def api_growth_target_bulk_create():
        """批量新增角色养成目标，供添加角色弹窗的一键添加使用。"""
        try:
            growth_db, growth_planner, growth_models, _, _, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})

        payload = request.get_json(silent=True) or {}
        characters = payload.get("characters")
        if not isinstance(characters, list) or not characters:
            return _json_error("没有收到角色列表")

        target = growth_models.normalise_target_values({
            "level_target": 88,
            "normal_target": 1,
            "skill_target": 9,
            "burst_target": 9,
            "weapon_enabled": 1,
            "weapon_level_target": 90,
        })
        added = []
        for row in characters:
            if not isinstance(row, dict):
                continue
            try:
                character_id = int(row.get("character_id") or 0)
            except (TypeError, ValueError):
                character_id = 0
            if not character_id:
                continue

            owned = bool(row.get("owned"))
            growth_planner.set_manual_ownership(character_id, owned)
            if not growth_db.get_character(character_id):
                growth_db.upsert_character(
                    character_id,
                    name=str(row.get("name") or row.get("character_name") or ""),
                    rarity=int(row.get("rarity") or 0),
                    element=str(row.get("element") or ""),
                    level=int(row.get("level") or 0),
                )
            growth_db.save_target(character_id, {**target, "owned": 1 if owned else 0})
            growth_planner.invalidate_cache(character_id)
            added.append(character_id)
        return jsonify({"ok": True, "added": len(added), "character_ids": added})

    @app.post("/api/growth/targets/bulk-update")
    def api_growth_target_bulk_update():
        """批量修改所有已有角色目标，保留每个角色的排序 / 拥有状态。"""
        try:
            growth_db, growth_planner, growth_models, _, _, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})

        payload = request.get_json(silent=True) or {}
        source = payload.get("target") or payload
        values = growth_models.normalise_target_values(source)
        allowed = {
            "level_target", "normal_target", "skill_target", "burst_target",
            "weapon_enabled", "weapon_level_target",
        }
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return _json_error("没有收到可批量修改的培养目标")

        updated = growth_db.bulk_update_targets(values)
        # 目标字段变了，旧的单角色材料结果都不能继续使用。
        growth_planner.invalidate_cache()
        return jsonify({
            "ok": True,
            "updated": updated,
            "target": values,
            "message": f"已更新 {updated} 个角色的培养目标，材料缺口将在下一次规划时重算。",
        })

    @app.put("/api/growth/targets/<int:character_id>")
    def api_growth_target_update(character_id):
        return _save_growth_target(character_id)

    @app.delete("/api/growth/targets")
    def api_growth_targets_clear():
        try:
            growth_db, growth_planner, _, _, _, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})

        db_file = growth_db.db_path()
        backup = ""
        if os.path.exists(db_file):
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = f"{db_file}.bak-clear-targets-{stamp}"
            shutil.copy2(db_file, backup)
        deleted = growth_db.clear_targets()
        growth_planner.invalidate_cache()
        return jsonify({"ok": True, "deleted": deleted, "backup": backup})

    def _save_growth_target(character_id=None):
        try:
            growth_db, growth_planner, growth_models, _, _, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})

        payload = request.get_json(silent=True) or {}
        if character_id is None:
            try:
                character_id = int(payload.get("character_id") or 0)
            except (TypeError, ValueError):
                character_id = 0
        if not character_id:
            return _json_error("character_id 不能为空")

        # 目标字段全部走 clamp：等级 1~90 任意值（81 必须合法）、天赋各自 1~10（§10/§11）
        source = payload.get("target") or payload
        values = growth_models.normalise_target_values(source)
        if not values and "owned" not in payload and "owned" not in source:
            return _json_error("没有收到任何可更新的字段")
        # 「这个号有没有这个角色」由玩家定（米游社的"我的角色"接口已下线，程序问不到）；
        # 记下来之后「添加角色」的「已拥有 / 未拥有」两个列表就有依据了。
        if "owned" in payload or "owned" in source:
            raw = payload.get("owned", source.get("owned"))
            owned = 1 if str(raw) in ("1", "true", "True", "yes", "on") else 0
            values["owned"] = owned
            growth_planner.set_manual_ownership(character_id, bool(owned))

        if not growth_db.get_character(character_id):
            # 米游社档案没同步过也要能建目标：名字从请求里拿，拿不到就用 id 占位
            growth_db.upsert_character(
                character_id,
                name=str(payload.get("character_name") or payload.get("name") or ""),
                rarity=int(payload.get("rarity") or 0),
                element=str(payload.get("element") or ""),
                level=int(payload.get("level") or 0),
            )
        target = growth_db.save_target(character_id, values)
        # 目标变了 → 材料需求缓存必须失效（否则页面还显示旧缺口）
        growth_planner.invalidate_cache(character_id)
        return jsonify({"ok": True, "target": target})

    @app.delete("/api/growth/targets/<int:character_id>")
    def api_growth_target_delete(character_id):
        try:
            growth_db, growth_planner, _, _, _, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})
        if not growth_db.delete_target(character_id):
            return _json_error("没有这个角色的养成目标", 404)
        growth_planner.invalidate_cache(character_id)
        return jsonify({"ok": True, "character_id": character_id})

    @app.post("/api/growth/targets/<int:character_id>/refresh-requirements")
    def api_growth_target_refresh_requirements(character_id):
        try:
            growth_db, growth_planner, growth_models, material_planner, mys_inventory, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})

        target = growth_db.get_target(character_id)
        if not target:
            return _json_error("没有这个角色的养成目标", 404)

        growth_planner.invalidate_cache(character_id)
        computed = growth_planner.requirements_for(
            target, force=True, uid=str(getattr(config, "MYS_UID", "") or getattr(config, "DEFAULT_UID", "") or "")
        )
        plan = growth_planner.build_plan(record=False, compute_budget_limit=0)
        return jsonify({
            "ok": True,
            "refresh_ok": not bool(computed.get("error")),
            "character_id": character_id,
            "requirements": computed,
            "plan": plan,
            "targets": growth_db.list_targets(),
            "sort_mode": growth_planner.sort_mode(),
            "sort_modes": [
                {"value": mode, "label": growth_models.SORT_MODE_LABELS[mode]}
                for mode in growth_models.SORT_MODES
            ],
            "inventory": mys_inventory.status(),
            "phase_labels": growth_models.PHASE_LABELS,
            "state_labels": growth_models.STATE_LABELS,
            "task_type_labels": material_planner.TASK_TYPE_LABELS,
            "status_labels": material_planner.STATUS_LABELS,
            "snapshots": mys_inventory.list_snapshots(limit=10),
            "sync_history": growth_db.list_sync_history(limit=12),
            "sync_summary": growth_db.sync_summary(limit=50),
            "executions": growth_db.list_executions(limit=12),
            "mys": {
                "configured": _mys_configured(),
                "masked": _masked_cookie(),
            },
            "refresh_error": computed.get("error") or "",
            "refresh_error_kind": computed.get("error_kind") or "",
        })

    @app.post("/api/growth/targets/reorder")
    def api_growth_target_reorder():
        """拖拽排序（§15 自定义顺序）。请求体：{"order": [10000089, 10000102, ...]}"""
        try:
            growth_db, growth_planner, growth_models, _, _, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})

        payload = request.get_json(silent=True) or {}
        order = payload.get("order")
        if not isinstance(order, list) or not order:
            return _json_error("order 必须是非空数组")
        try:
            ids = [int(item) for item in order]
        except (TypeError, ValueError):
            return _json_error("order 里必须是角色 id 数字")

        growth_db.reorder_targets(ids)
        # 用户手动排过 = 意图明确，顺手切到自定义排序模式
        growth_planner.set_sort_mode(growth_models.SORT_MODE_CUSTOM)
        return jsonify({"ok": True, "targets": growth_db.list_targets(),
                        "sort_mode": growth_planner.sort_mode()})

    @app.post("/api/growth/sort-mode")
    def api_growth_sort_mode():
        try:
            _, growth_planner, growth_models, _, _, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})
        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("mode") or "").strip()
        if mode not in growth_models.SORT_MODES:
            return _json_error(f"不认识的排序模式：{mode}")
        return jsonify({"ok": True, "sort_mode": growth_planner.set_sort_mode(mode)})

    @app.get("/api/growth/plan")
    def api_growth_plan_get():
        try:
            _, growth_planner, _, _, _, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})
        force = str(request.args.get("force") or "").strip().lower() in ("1", "true", "yes", "on")
        plan = growth_planner.build_plan(force_compute=force, record=False)
        return jsonify({"ok": True, "plan": plan})

    @app.post("/api/growth/plan")
    def api_growth_plan_post():
        """重新生成计划（可选先同步一次库存）。

        请求体：{"sync": true, "force_compute": false, "lines": true}
        """
        try:
            _, growth_planner, _, _, mys_inventory, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})

        payload = request.get_json(silent=True) or {}
        sync_result = None
        if payload.get("sync"):
            sync_result = growth_planner.sync_inventory()

        plan = growth_planner.build_plan(force_compute=bool(payload.get("force_compute")))
        response = {"ok": True, "plan": plan}
        if payload.get("lines", True):
            response["lines"] = growth_planner.plan_lines(plan)
        if sync_result is not None:
            response["sync"] = {
                "ok": bool(sync_result.get("ok")),
                "error": sync_result.get("error") or "",
                "advice": mys_inventory.error_advice(sync_result.get("error_kind")),
                "item_count": sync_result.get("item_count") or 0,
            }
        return jsonify(response)

    @app.post("/api/growth/plan/execute")
    def api_growth_plan_execute():
        """执行计划里可执行的 BetterGI 任务。

        ⚠️ 这一步**只下发任务**，绝不写任何库存数字：真实结果要等 BetterGI 跑完、
        完成监视触发"重新同步米游社"之后才知道（§19）。

        请求体：{"decision": "y" | "t"}（默认 y = 真的启动 BetterGI）
        """
        try:
            _, growth_planner, _, material_planner, _, _ = _growth()
            from skills import bgi_controller
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"模块加载失败：{exc}"})

        payload = request.get_json(silent=True) or {}
        decision = str(payload.get("decision") or "y").strip().lower()
        if decision not in ("y", "t"):
            return _json_error("decision 只能是 y（执行）或 t（只写配置）")

        plan = growth_planner.build_plan(record=True)
        execution = plan.get("execution") or {}
        tasks = execution.get("tasks") or []
        command = plan.get("bettergi_cmd") or {}

        # ★ 分批次模式（`GROWTH_EXECUTION_MODE=stepwise`）：一次只下发**当前这一条路线**。
        #   请求体 `step`：
        #     · 不传（默认）—— 跑**当前**这一条；队列没了（或已经跑完）才重新起一个。
        #       这样"连点几次执行"就是"一条一条往下跑"，不会把游标重置回第一条。
        #     · `start` —— 强制重新起队列（换了目标 / 想从头再来时用）。
        #     · `next`  —— 明确推进到下一条再跑。
        from brain import execution_queue

        if execution_queue.is_stepwise():
            step_mode = str(payload.get("step") or "").strip().lower()
            state = execution_queue.load()
            if step_mode == "start":
                state = execution_queue.start(tasks, plan_id=plan.get("plan_id") or 0,
                                              character_id=plan.get("current_character_id") or 0)
            elif step_mode == "next":
                execution_queue.advance()
                state = execution_queue.load()
                if not execution_queue.current(state):
                    state = execution_queue.start(
                        tasks, plan_id=plan.get("plan_id") or 0,
                        character_id=plan.get("current_character_id") or 0)
            else:
                # 默认：让队列跟当前计划对齐（路线变了就重建、没变就保留进度并刷新数字）
                state = execution_queue.sync(
                    tasks, plan_id=plan.get("plan_id") or 0,
                    character_id=plan.get("current_character_id") or 0)
            step = execution_queue.current(state)
            if not step:
                return jsonify({
                    "ok": False,
                    "stepwise": True,
                    "error": "这一轮没有可执行的路线（材料已满足 / 没路线 / 都在冷却）",
                    "steps": execution_queue.render_steps(tasks),
                    # 形状和"一次性执行"那条失败路径保持一致：界面/调用方都按 blockers 找原因
                    "blockers": plan.get("blockers") or [],
                    "plan": plan,
                })
            tasks = [dict(step, status="runnable")]
            command = step.get("command") or {}

        if not tasks or not command:
            return jsonify({
                "ok": False,
                "error": "当前没有可执行的 BetterGI 任务",
                "blockers": plan.get("blockers") or [],
                "plan": plan,
            })

        # 完成监视 → 重新同步米游社（这是"执行后重算缺口"的唯一触发点）
        growth_planner.register_watcher_hook()
        growth_planner.note_execution(
            plan_id=plan.get("plan_id") or 0,
            character_id=plan.get("current_character_id") or 0,
            phase=plan.get("current_phase") or "",
        )

        store = {}
        try:
            from brain import memory_manager

            store = memory_manager.load_chat_store()
        except Exception:               # noqa: BLE001 —— 账本不是必需品
            store = {}

        # 下发是阻塞的（要关掉正在跑的 BetterGI、改配置、冷启动），所以放后台线程，
        # 接口立刻返回"已下发"，进度看日志页（Studio 的运行页）。
        thread = threading.Thread(
            target=_run_growth_command,
            args=(bgi_controller, command, decision, store),
            daemon=True,
            name="growth-execute",
        )
        thread.start()
        stepwise = execution_queue.is_stepwise()
        return jsonify({
            "ok": True,
            "started": True,
            "decision": decision,
            "stepwise": stepwise,
            "steps": execution_queue.render_steps(plan.get("execution") or {}) if stepwise else [],
            "queue": execution_queue.status() if stepwise else None,
            "tasks": tasks,
            "bettergi_cmd": command,
            "fallback": bool(execution.get("fallback")),
            "note": execution.get("note") or "任务已下发；BetterGI 跑完后会自动重新同步米游社并重算缺口。",
            "plan": plan,
        })

    def _run_growth_command(bgi_controller, command, decision, store):
        """后台执行：把养成计划当成一次普通的 BetterGI 轮次下发。"""
        open_id = "STUDIO_GROWTH"
        try:
            bgi_controller.execute_bgi_task(command, decision, store, open_id, config.DEFAULT_UID)
        except Exception as exc:        # noqa: BLE001 —— 后台线程必须留痕，不能静默死
            import traceback

            traceback.print_exc()
            print(f"❌ 养成计划下发失败：{type(exc).__name__} {exc}")

    @app.post("/api/growth/resin")
    def api_growth_resin():
        """记一次**当前体力**（米游社那个接口被风控挡着，读不到时的主力方案）。

        请求体：`{"value": 27, "max": 200}`（`max` 可省）。
        为什么要有这个接口：体力接口对这个账号一直回 `5003 账号数据异常`（实测 8 种
        cookie / DS / 客户端类型组合全被拒），读不到真实值。而体力是**按 8 分钟 1 点**
        线性回涨的 —— 玩家填一个准数，程序就能往后推算，几小时内都够用，
        趟数也就能按真实体力算，而不是硬编码"秘境 2 趟、地脉 6 趟"。
        """
        try:
            from skills import mys_resin
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"模块加载失败：{exc}"})
        payload = request.get_json(silent=True) or {}
        if "value" not in payload:
            return _json_error("要带 value（当前体力）")
        result = mys_resin.set_manual(payload.get("value"), top=payload.get("max"))
        if not result.get("ok"):
            return _json_error(result.get("error") or "保存失败")
        return jsonify({"ok": True, "resin": result.get("state"),
                        "record": result.get("record"),
                        "note": "已记下。体力按 8 分钟回涨 1 点自动往后推算，趟数会按它算。"})

    @app.get("/api/growth/history")
    def api_growth_history():
        """同步历史 + 执行历史 + 快照列表（规格书 §32）。"""
        try:
            growth_db, _, _, _, mys_inventory, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})
        limit = _int_arg("limit", 50, minimum=1, maximum=500)
        return jsonify({
            "ok": True,
            "sync": growth_db.list_sync_history(limit=limit),
            "sync_summary": growth_db.sync_summary(limit=limit),
            "executions": growth_db.list_executions(limit=limit),
            "snapshots": mys_inventory.list_snapshots(limit=limit),
            "plans": [
                {
                    "id": plan["id"],
                    "created_at": plan["created_at"],
                    "status": plan["status"],
                    "current_character_id": plan["current_character_id"],
                    "current_phase": plan["current_phase"],
                    "summary": plan["summary"],
                }
                for plan in growth_db.list_plans(limit=20)
            ],
        })

    @app.post("/api/growth/inventory/refresh")
    def api_growth_inventory_refresh():
        """只重读磁盘上的库存副本（不同步米游社）—— 排查"文件被手工改过"时用。"""
        try:
            _, growth_planner, _, _, mys_inventory, _ = _growth()
        except Exception as exc:        # noqa: BLE001
            return jsonify({"ok": False, "error": f"养成模块加载失败：{exc}"})
        growth_planner.invalidate_cache()
        return jsonify({"ok": True, "inventory": mys_inventory.status(),
                        "plan": growth_planner.build_plan(record=False)})

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
