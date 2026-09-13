import os
import sys
import platform
from dotenv import load_dotenv

# 🌟 强制在此处加载一次环境变量，防止被 main.py 的导入顺序坑到
load_dotenv()

# 项目根目录。**所有仓库内的路径都从这里拼**，不要依赖 os.getcwd()：
# 用计划任务 / 带"起始位置"的快捷方式 / 从别处 `python <路径>\main.py` 启动时，
# 工作目录不是仓库根，相对路径会指向别处 —— 表现是"记忆突然丢了""角色查不到""doctor 说密钥没配"。
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MEMORY_DIR = os.path.join(PROJECT_ROOT, "memory")


def project_path(*parts):
    """把仓库内的相对路径变成绝对路径（老代码里写的是 "memory/xxx.json" 这种）。"""
    return os.path.join(PROJECT_ROOT, *parts)


def ensure_utf8_output():
    """把 stdout/stderr 切到 UTF-8 并开行缓冲（这个项目到处 print emoji）。

    不这么做的话，输出被重定向到文件/管道（`python main.py repair > x.log`、CI、`python -m unittest`）
    时编码是 GBK，一句 emoji 就抛 UnicodeEncodeError；`execute_bgi_task` 的报错分支自己带 emoji，
    会在 except 里二次抛出 —— 玩家连"出错了"都收不到。
    真实控制台本来就是 UTF-8，这里是幂等的空操作。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:          # pythonw / 已被接管：stdout 是 None
            continue
        try:
            kwargs = {"line_buffering": True, "write_through": True}
            if str(getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
                kwargs.update({"encoding": "utf-8", "errors": "replace"})
            reconfigure(**kwargs)
        except Exception:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


# 导入即生效：config 是每个入口（main/app_web/qq_main/feishu_main/gui）和测试都会 import 的模块，
# 放在这里等于给所有入口都铺了兜底，不用逐个入口记得调。
ensure_utf8_output()

HISTORY_FILE = os.path.join(MEMORY_DIR, "chat_context.json")
SYSTEM_RULES_FILE = os.path.join(PROJECT_ROOT, "prompts", "system_rules.md")


def get_env(name, default=""):
    """Return an environment variable, falling back when it is unset or empty."""
    return os.getenv(name) or default


def get_int_env(name, default):
    """Read a positive integer setting without letting an invalid .env value crash startup."""
    raw_value = get_env(name)
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default

# ==========================================
# 🌟 智能路径解析逻辑
# ==========================================
def get_default_bgi_dir():
    """根据系统环境自动推断 BetterGI 的默认安装路径"""
    # 检查是否在 WSL 环境中
    if "microsoft" in platform.uname().release.lower():
        return "/mnt/c/Program Files/BetterGI"
    else:
        # 纯 Windows 环境的默认路径
        return r"C:\Program Files\BetterGI"

# 优先读取 .env 中用户自定义的 BGI_DIR，如果没有配置，则使用智能判断的默认值
BGI_DIR = get_env("BGI_DIR", get_default_bgi_dir())
BGI_EXE = get_env("BGI_EXE", "BetterGI.exe")

BGI_ONE_DRAGON_CONFIG_NAME = get_env("BGI_ONE_DRAGON_CONFIG_NAME", "默认配置.json")
BGI_MAP_CONFIG_NAME = get_env("BGI_MAP_CONFIG_NAME", "地图素材.json")
BGI_GLOBAL_CONFIG_RELATIVE_PATH = get_env(
    "BGI_GLOBAL_CONFIG_RELATIVE_PATH", os.path.join("User", "config.json")
)
BGI_BOSS_CONFIG_RELATIVE_PATH = get_env(
    "BGI_BOSS_CONFIG_RELATIVE_PATH",
    os.path.join(
        "User",
        "JsScript",
        "批量讨伐角色养成材料BOSS",
        "assets",
        "config",
        "config.json",
    ),
)

BGI_ONE_DRAGON_CONFIG = os.path.join(BGI_DIR, "User", "OneDragon", BGI_ONE_DRAGON_CONFIG_NAME)
# 调度器脚本组目录（地图素材 / 敌人与魔物 等路线组都放在这里）
BGI_SCRIPT_GROUP_DIR = os.path.join(BGI_DIR, "User", "ScriptGroup")
BGI_MAP_CONFIG = os.path.join(BGI_SCRIPT_GROUP_DIR, BGI_MAP_CONFIG_NAME)
# 敌人与魔物路线组：BetterGI 调度器里的组名。缺省时会按敌人名在脚本组目录里自动找
# 用户已有的分组（如 蕈兽.json / 骗骗花.json / 刀谭.json）。
BGI_ENEMY_CONFIG_NAME = get_env("BGI_ENEMY_CONFIG_NAME", "敌人与魔物.json")
BGI_ENEMY_CONFIG = os.path.join(BGI_SCRIPT_GROUP_DIR, BGI_ENEMY_CONFIG_NAME)
# 矿物路线组（挖矿）：目标填矿物名，如 水晶块 / 紫晶块 / 星银矿石 / 白铁块 / 铁块 / 魔晶矿 / 萃凝晶 / 虹滴晶
BGI_MINE_CONFIG_NAME = get_env("BGI_MINE_CONFIG_NAME", "矿物.json")
BGI_MINE_CONFIG = os.path.join(BGI_SCRIPT_GROUP_DIR, BGI_MINE_CONFIG_NAME)
# 食材与炼金路线组（食材/炼金材料）：目标填素材名，如 禽肉 / 鱼肉 / 螃蟹 / 蘑菇 / 薄荷 / 甜甜花 / 胡萝卜
BGI_COOK_CONFIG_NAME = get_env("BGI_COOK_CONFIG_NAME", "食材与炼金.json")
BGI_COOK_CONFIG = os.path.join(BGI_SCRIPT_GROUP_DIR, BGI_COOK_CONFIG_NAME)
# 锄大地路线组（`repo/pathing/锄地专区`）：整图/按地区扫图清怪（小怪2000@mno / 精英400@汐 /
# 挪德卡莱锄地小怪）。⚠️ 和「敌人与魔物」是两码事，别混：
#   敌人与魔物 = 指定具体魔物/材料，只开那一个魔物的固定怪点；
#   锄大地     = 不指定魔物，按地区把整片图的怪扫一遍（路线名里会出现 飞萤/骗骗花 这类词，
#                靠目录前缀隔离，绝不和敌人与魔物互相抢路线）。
BGI_HOE_CONFIG_NAME = get_env("BGI_HOE_CONFIG_NAME", "锄大地.json")
BGI_HOE_CONFIG = os.path.join(BGI_SCRIPT_GROUP_DIR, BGI_HOE_CONFIG_NAME)
# BetterGI 把脚本仓库里的路径追踪项目解包到这里：User\AutoPathing\<类目>\<项目>\<子目录>\*.json
BGI_AUTO_PATHING_DIR = get_env(
    "BGI_AUTO_PATHING_DIR", os.path.join(BGI_DIR, "User", "AutoPathing")
)
# 调度器里没有该类目的路线组时，是否按 User\AutoPathing\<类目> 自动生成一个组文件
# （只对「锄大地」这类 400+ 条、手工建组很折磨的类目生效）。生成后仍需要在 BetterGI
# 一条龙界面把它勾选/添加一次，agent 不会替你伪造 TaskDefinitions 登记项。
BGI_AUTO_CREATE_ROUTE_GROUP = get_env("BGI_AUTO_CREATE_ROUTE_GROUP", "1") == "1"
# 路线组大小上限：BetterGI 会为每个 Disabled 的脚本写一行日志，超大组（例如把整个
# 「敌人与魔物」1014 条塞进一个组）在跑一半按停止时会让日志瞬间爆发（实测 638/773 行/秒），
# 随后在 WPF 层栈溢出崩溃（0xc00000fd）。超过这个条数按下面的策略处理。
BGI_MAX_ROUTE_GROUP_SIZE = get_int_env("BGI_MAX_ROUTE_GROUP_SIZE", 300)
# 超大组处理策略：
#   shrink（默认）= 自动把组精简成"只有本次要打的路线"（完整清单归档，可还原，不用手工拆组）
#   refuse        = 拒绝启用并告警
#   allow         = 照旧整组开关（崩溃风险自负）
BGI_ROUTE_GROUP_POLICY = get_env("BGI_ROUTE_GROUP_POLICY", "shrink")
# 兼容旧开关：设 1 等价于 policy=allow
BGI_ALLOW_LARGE_ROUTE_GROUP = get_env("BGI_ALLOW_LARGE_ROUTE_GROUP", "0") == "1"
BGI_GLOBAL_CONFIG = os.path.join(BGI_DIR, BGI_GLOBAL_CONFIG_RELATIVE_PATH)
BGI_BOSS_CONFIG = os.path.join(BGI_DIR, BGI_BOSS_CONFIG_RELATIVE_PATH)
BGI_BACKUP_DIR = get_env("BGI_BACKUP_DIR", os.path.join(BGI_DIR, "User", "GI_AgentBackups"))
# 战斗策略（.txt）目录：JS 脚本里的 AutoFightParam(strategyName) 就是按名字在这里找
BGI_AUTO_FIGHT_DIR = get_env("BGI_AUTO_FIGHT_DIR", os.path.join(BGI_DIR, "User", "AutoFight"))
# Agent 自己往一条龙里加过哪些任务（用于下一轮回收，避免残留任务一直挂着 enabled）。
# 单独抽成配置项是为了测试能指到临时文件 —— 否则跑测试会写坏玩家真实的状态文件。
AGENT_TASK_STATE_PATH = get_env(
    "AGENT_TASK_STATE_PATH", os.path.join(MEMORY_DIR, "agent_registered_tasks.json")
)
# JS 脚本的"自定义配置"：会被写进 `User\ScriptGroup\<脚本组>.json` 里该项目的
# `jsScriptSettingsObject`。BetterGI 在跑 JS 项目时把它当成 JS 里的 `settings` 对象
# （`ScriptProject.ExecuteAsync` → `engine.AddHostObject("settings", context)`）。
#
# 为什么需要它：《批量讨伐角色养成材料BOSS》的 `settings.json` 定义了
# `showEditorOnStart`（默认 true）—— 脚本启动时会弹一个遮罩式"BOSS 配置编辑器"，
# **必须手点"保存并关闭"才会继续**；挂机时这就是个卡死的窗口。
# `showStatusPanel` 是运行时的悬浮状态面板，挂机时关掉可以少一个抢焦点的窗口
# （BetterGI 检测到"当前获取焦点的窗口不是原神"会暂停/中止）。
# 不认识的键会被 BetterGI 自己按脚本的 settings.json 过滤掉，所以这里可以写得通用一点。
BGI_JS_SCRIPT_SETTINGS = get_env(
    "BGI_JS_SCRIPT_SETTINGS", '{"showEditorOnStart": false, "showStatusPanel": false}'
)

# 展柜上下文的保鲜期（分钟）：超过这个时间就重新拉一次 Enka，
# 避免把一份没有时间戳的旧快照当"最新展柜"永久喂给大模型
ENV_CONTEXT_TTL_MINUTES = get_int_env("ENV_CONTEXT_TTL_MINUTES", 10)

# ==========================================
# 🌟 米游社个人战绩（补全"不在展柜的角色"）
# ==========================================
# 展柜（Enka）一次只放 8 个角色，玩家问起展柜外的角色时 Agent 只能干瞪眼。
# 填了米游社 cookie 就能读到**自己账号的全部角色**（等级 / 天赋等级 / 武器）。
#
# ⚠️ cookie 等于账号访问凭据：
#   · 只写进 .env（已在 .gitignore 里），别贴到聊天记录、别提交到仓库；
#   · 它有自己的有效期（ltoken 通常几天~一个月），过期后这里会报"凭据失效"，重新取一份即可；
#   · 这个接口是米游社 App/网页自己用的，**不是**官方开放接口，风控严格时可能要求验证码。
# 留空 = 完全关闭这个功能（一行代码都不会去请求米游社）。
MYS_COOKIE = get_env("MYS_COOKIE", "")
# 查询哪个号：留空则用 DEFAULT_UID；再没有就取 cookie 名下第一个原神角色
MYS_UID = get_env("MYS_UID", "") or get_env("DEFAULT_UID", "")
# 多久拉一次全角色名单（小时）。玩家要的是"别每次启动都拉，1~3 天一次"：
# 只有真的要查展柜外的角色、且缓存比这个时间旧，才会去请求米游社。
MYS_CACHE_TTL_HOURS = get_int_env("MYS_CACHE_TTL_HOURS", 48)
MYS_CACHE_PATH = get_env(
    "MYS_CACHE_PATH", os.path.join(MEMORY_DIR, "mys_characters.json")
)
# DS 签名参数：`salt` 随米游社 App 版本变化，官方没公开。
# 默认值取自 Yunzai/喵喵插件那套仍在用的国服参数（miHoYoBBS 2.40.1 / client_type=5）；
# 米游社改版导致签名失效时，改 .env 里这两个值即可，不用改代码。
MYS_SALT = get_env("MYS_SALT", "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs")
MYS_APP_VERSION = get_env("MYS_APP_VERSION", "2.40.1")
MYS_CLIENT_TYPE = get_env("MYS_CLIENT_TYPE", "5")
# 一次对话里最多为几个"展柜外角色"去拉天赋详情（每次请求都可能触碰风控，别贪）
MYS_DETAIL_MAX_PER_TURN = get_int_env("MYS_DETAIL_MAX_PER_TURN", 3)
# 个人战绩接口的基础域名（国际服是 bbs-api-os.hoyolab.com；这里只做国服）
MYS_RECORD_HOST = get_env("MYS_RECORD_HOST", "https://api-takumi-record.mihoyo.com")
MYS_API_HOST = get_env("MYS_API_HOST", "https://api-takumi.mihoyo.com")

# 刷秘境时用哪种树脂（对应 BetterGI 自动秘境「指定树脂使用次数」里的条目）：
#   原粹树脂20（默认，20 体力一次） / 原粹树脂40 / 浓缩树脂
DOMAIN_RESIN_PREFERENCE = get_env("DOMAIN_RESIN_PREFERENCE", "原粹树脂20")

# ==========================================
# 🌟 BetterGI 运行状态监控（任务完成后回终端报信）
# ==========================================
BGI_LOG_DIR = os.path.join(BGI_DIR, "log")
BGI_TASK_PROGRESS_DIR = os.path.join(BGI_LOG_DIR, "task_progress")
# 等待一条龙完成的超时时间，默认 6 小时（跨夜跑图也够用）
BGI_WATCH_TIMEOUT_SECONDS = get_int_env("BGI_WATCH_TIMEOUT_MINUTES", 360) * 60

# ==========================================
# 🌟 下发计划时发现 BetterGI 正在跑任务 → 等它跑完再自动继续
# ==========================================
# 玩家实测的坑（QQ 记录）：12:34:40 一条龙就跑完了，12:36 他再下一条指令，
# Agent 只看到"日志还在更新"（BGI 开着窗口空闲时也会零碎写日志）就判成"正在跑任务"并拒绝，
# 他只好把同一句话重下一遍。现在改成：先**等**它跑完（轮询日志里的结束标记），
# 等到了就接着执行本轮计划，不用玩家重新下令。
BGI_WAIT_RUNNING_TASK_MINUTES = get_int_env("BGI_WAIT_RUNNING_TASK_MINUTES", 20)
# 轮询间隔（秒）：一条龙一次可能跑几十分钟，10 秒一次足够，也不会把日志读爆。
BGI_RUNNING_TASK_POLL_SECONDS = get_int_env("BGI_RUNNING_TASK_POLL_SECONDS", 10)

# ==========================================
# 🌟 原神窗口前后台兜底（BGI 的模拟输入要求游戏在前台）
# ==========================================
# 实测（玩家 BGI 日志）：一条龙跑到一半出现 73 次
#     「当前获取焦点的窗口为: GI-Agent-Studio，不是原神，暂停」
# 前台分别是 QQ / 我们自己的 Studio / Win 搜索 —— 表现就是"卡死"，其实在等原神回前台。
# 启动后把原神推到前台一次（玩家点 y 就是"开始跑图"的意思，不推上去 BGI 一启动就暂停）
BGI_FOCUS_ON_LAUNCH = get_env("BGI_FOCUS_ON_LAUNCH", "1") == "1"
# 运行中反复把原神抢回前台：**默认关** —— 玩家可能正在用电脑（QQ/浏览器），
# 反复抢会打断他；打开后最多抢 BGI_FOCUS_GUARD_MAX_TRIES 次，然后罢手并说明。
BGI_FOCUS_GUARD = get_env("BGI_FOCUS_GUARD", "0") == "1"
BGI_FOCUS_GUARD_MAX_TRIES = get_int_env("BGI_FOCUS_GUARD_MAX_TRIES", 3)
# 连续几次巡检发现"前台不是原神"才提示/动手（一次 15 秒，2 次≈30 秒，避免误报）
BGI_FOCUS_GUARD_STRIKES = get_int_env("BGI_FOCUS_GUARD_STRIKES", 2)

# ==========================================
# 🌟 "帮我关闭原神"（skills/game_control.py）
# ==========================================
# 先正常关闭（WM_CLOSE，等于点窗口右上角的叉），宽限这么久还没退就强制结束（taskkill /F）。
# 原神进度存在服务器上，强杀不丢档，只是下次启动会提示"上次未正常退出"。
GAME_CLOSE_GRACE_SECONDS = get_int_env("GAME_CLOSE_GRACE_SECONDS", 10)

# ==========================================
# 🌟 采集物冷却（地区特产 48 小时刷新）
# ==========================================
# 采集类任务在**排期前**会先查"这个材料刷了没"：数据来自 BetterGI 自己的日志
# （每条路线跑完都会打「脚本执行结束: "01-霜仙花-彩冰镇左上-3个.json"」），
# 所以玩家自己在 BGI 里手动跑过的也算 —— 48 小时内采过的不重复采，只提示还要等多久。
GATHER_COOLDOWN_HOURS = get_int_env("GATHER_COOLDOWN_HOURS", 48)
# 采完还想马上再采时，允许玩家用「强制采集」这类说法跳过冷却拦截
GATHER_COOLDOWN_ALLOW_FORCE = get_env("GATHER_COOLDOWN_ALLOW_FORCE", "1") == "1"
# "采过了"的判定门槛：这种材料的路线至少要跑掉这个百分比才算采完（默认 80%）。
# 为什么要它：**防闪退隔离带**会给没被点名的材料也打开一条路线（每连续 150 条 Disabled 开一条），
# 玩家实测只跑了 1 条隔离带路线就被记成"采过"、白等 48 小时（万相石 1/16、晶化骨髓 1/6…）。
# 门槛取 80% 而不是 100%：留一条路线失败（坏路线）的余地，但"只采了一半"仍算没采完、可以继续采。
GATHER_COOLDOWN_MIN_ROUTE_PERCENT = get_int_env("GATHER_COOLDOWN_MIN_ROUTE_PERCENT", 80)

# ==========================================
# 🌟 防闪退隔离带（脚本组里"穿插其它材料"的路线就是它）
# ==========================================
# 背景（实测）：BetterGI 会给脚本组里**每一条 Disabled 路线**写一行日志；几百条的大组在跑一半
# 按停止快捷键时，日志会瞬间爆发（玩家机器实测 550~700 行/秒），随后可能在 WPF 层栈溢出崩溃
# （0xc00000fd，BGI 0.64.0 遇到过）。所以原实现"每连续 150 条 Disabled 就强制打开一条"来截断。
#
# ⚠️ 但"连续 N 条会触发 bug"这个**具体阈值查不到出处**（BGI 源码/文档里没有；代码用 150，
#    旧 README 写 180）。玩家要求做成开关自己验证，所以：
BGI_FORCE_ENABLE_BAND = get_env("BGI_FORCE_ENABLE_BAND", "1") == "1"
# 隔多少条 Disabled 插一条隔离带（默认 150，与原实现一致）。只有 BGI_FORCE_ENABLE_BAND=1 才生效。
BGI_FORCE_ENABLE_AFTER_DISABLED = get_int_env("BGI_FORCE_ENABLE_AFTER_DISABLED", 150)

DEFAULT_UID = get_env("DEFAULT_UID")
MAX_HISTORY_MESSAGES = get_int_env("MAX_HISTORY_MESSAGES", 20)
