"""`.env` 的读写（控制台「配置」页用）。

设计目标：**只改值，不动别的**。
- 保留原有注释、空行、键顺序（玩家自己写的注释不能丢）；
- 支持 `export KEY=value`、单/双引号；
- 保存前自动备份成 `.env.bak-<时间戳>`，写文件用「临时文件 + 替换」避免写坏；
- 提供字段元数据（分组 / 中文名 / 类型 / 可选项 / 说明），供 GUI 生成表单。

⚠️ `config.py` 是在 import 时读 `.env` 的，所以改完配置必须**重启 Agent 子进程**才生效
（控制台会在保存后提示）。
"""

import datetime
import os
import re
import shutil
from typing import NamedTuple

ENTRY_PATTERN = re.compile(r"^(\s*)(export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
APPEND_SECTION = "# ==================== 由控制台新增 ===================="

SECRET_MASK = "••••••••"


class Field(NamedTuple):
    """一个可编辑的配置项。"""

    key: str
    label: str
    kind: str = "text"          # text | secret | int | bool | choice | path
    choices: tuple = ()
    default: str = ""
    help: str = ""


class FieldGroup(NamedTuple):
    title: str
    fields: tuple


FIELD_GROUPS = (
    FieldGroup(
        "LLM 模型",
        (
            Field("LLM_PROVIDER", "提供商", "choice", ("openai", "github", "nvidia", "custom", "local"), "openai",
                  "用哪家的接口。填 openai 时可以靠 OPENAI_BASE_URL 指向任何 OpenAI 兼容服务（如 DeepSeek）。"),
            Field("MODEL_NAME", "模型名", "text", (), "deepseek-chat", "例如 deepseek-chat / gpt-4o-mini / qwen2.5:14b。"),
            Field("OPENAI_API_KEY", "OpenAI 密钥", "secret", (), "", "LLM_PROVIDER=openai 时必填（sk- 开头）。"),
            Field("OPENAI_BASE_URL", "OpenAI 接口地址", "text", (), "https://api.openai.com/v1",
                  "换成 https://api.deepseek.com/v1 就是用 DeepSeek。"),
            Field("GITHUB_TOKEN", "GitHub Token", "secret", (), "", "LLM_PROVIDER=github 时必填。"),
            Field("NVIDIA_API_KEY", "NVIDIA 密钥", "secret", (), "", "LLM_PROVIDER=nvidia 时必填。"),
            Field("NVIDIA_BASE_URL", "NVIDIA 接口地址", "text", (), "https://integrate.api.nvidia.com/v1"),
            Field("CUSTOM_API_KEY", "自定义密钥", "secret", (), "", "LLM_PROVIDER=custom 时必填。"),
            Field("CUSTOM_BASE_URL", "自定义接口地址", "text", (), "", "LLM_PROVIDER=custom 时必填。"),
            Field("LOCAL_BASE_URL", "本地模型地址", "text", (), "", "LLM_PROVIDER=local 时必填，例如 http://127.0.0.1:11434/v1。"),
            Field("LOCAL_API_KEY", "本地模型密钥", "secret", (), "local"),
            Field("LLM_TIMEOUT_SECONDS", "读超时（秒）", "int", (), "300",
                  "不设时 OpenAI SDK 默认 600s；推理型模型 + 长 prompt 容易等到超时。"),
            Field("LLM_MAX_RETRIES", "自动重试次数", "int", (), "2"),
            Field("LLM_STREAM", "流式输出", "bool", (), "1", "长回答不会撞读超时；中转不支持时设 0。"),
        ),
    ),
    FieldGroup(
        "玩家与记忆",
        (
            Field("DEFAULT_UID", "原神 UID", "text", (), "", "展柜数据按这个 UID 抓取。"),
            Field("MAX_HISTORY_MESSAGES", "对话历史条数", "int", (), "20"),
            Field("ENV_CONTEXT_TTL_MINUTES", "展柜保鲜期（分钟）", "int", (), "10"),
            Field("DOMAIN_RESIN_PREFERENCE", "刷本树脂", "choice", ("原粹树脂20", "原粹树脂40", "浓缩树脂"), "原粹树脂20",
                  "说「打 N 次」时写进 BetterGI 的 autoDomainConfig。"),
            Field("BGI_WATCH_TIMEOUT_MINUTES", "一条龙超时（分钟）", "int", (), "360"),
            Field("BGI_WAIT_RUNNING_TASK_MINUTES", "BGI 在跑任务时等它多久（分钟）", "int", (), "20",
                  "下发计划时如果 BetterGI 正在跑任务，Agent 不会直接拒绝，而是等它跑完自动继续"
                  "（你不用重新下一遍指令）。这是最长等待时间；超时才会回「本轮先不执行」。"),
            Field("BGI_RUNNING_TASK_POLL_SECONDS", "等待时的轮询间隔（秒）", "int", (), "10",
                  "等待期间每隔这么久看一次 BetterGI 日志里的结束标记（默认 10 秒）。"),
            Field("GATHER_COOLDOWN_HOURS", "采集物刷新冷却（小时）", "int", (), "48",
                  "地区特产（霜仙花 / 清水玉这类）48 小时刷新。采集类任务排期前会先查冷却，"
                  "没刷新就只提示、不执行；数据来自 BetterGI 日志，所以你自己在 BGI 里手动跑过的也算。"),
            Field("GATHER_COOLDOWN_ALLOW_FORCE", "允许「强制采集」跳过冷却", "bool", (), "1",
                  "1 = 玩家说「强制采集 / 我知道没刷新」时照跑；0 = 一律拦。"),
            Field("GATHER_COOLDOWN_MIN_ROUTE_PERCENT", "算「采完」的路线比例（%）", "int", (), "80",
                  "只在这类材料的路线跑到这个比例时才认为采完、开始 48 小时冷却。"
                  "调低 = 更容易判成采完；调高 = 更容易判成'还没采完、可以继续采'。"),
            Field("BGI_FORCE_ENABLE_BAND", "防闪退隔离带", "bool", (), "1",
                  "1 = 脚本组里每连续若干条禁用路线就强制打开一条（防止 BetterGI 在我们这种几百条的大组里"
                  "按停止时日志爆发、WPF 栈溢出崩溃）。\n"
                  "0 = 完全不插隔离带：组里就只有你点名的路线（不会穿插别的材料）。\n"
                  "⚠️ 「连续多少条会触发 bug」这个阈值没有权威出处，你可以关掉自己观察一段时间。"),
            Field("BGI_FORCE_ENABLE_AFTER_DISABLED", "隔离带间隔（条）", "int", (), "150",
                  "每连续 N 条禁用路线插一条隔离带（默认 150，与原实现一致；旧文档写的 180 无出处）。"
                  "只在上面开关为 1 时生效。"),
        ),
    ),
    FieldGroup(
        "BetterGI 路径",
        (
            Field("BGI_DIR", "BetterGI 安装目录", "path", (), r"C:\Program Files\BetterGI"),
            Field("BGI_EXE", "主程序文件名", "text", (), "BetterGI.exe"),
            Field("BGI_BACKUP_DIR", "备份目录", "path", (), "", "留空 = BetterGI\\User\\GI_AgentBackups。"),
            Field("BGI_AUTO_FIGHT_DIR", "战斗策略目录", "path", (), "", "留空 = BetterGI\\User\\AutoFight。"),
            Field("BGI_AUTO_PATHING_DIR", "路径追踪目录", "path", (), "", "留空 = BetterGI\\User\\AutoPathing（自动建组时扫这里）。"),
            Field("BGI_GLOBAL_CONFIG_RELATIVE_PATH", "全局配置相对路径", "text", (), "User/config.json"),
            Field("BGI_BOSS_CONFIG_RELATIVE_PATH", "BOSS 脚本配置相对路径", "text", (),
                  "User/JsScript/批量讨伐角色养成材料BOSS/assets/config/config.json"),
        ),
    ),
    FieldGroup(
        "一条龙与脚本组名",
        (
            Field("BGI_ONE_DRAGON_CONFIG_NAME", "一条龙配置名", "text", (), "默认配置.json",
                  "真正生效的那份会被自动识别（例如 地图素材.json）。"),
            Field("BGI_MAP_CONFIG_NAME", "地图素材组", "text", (), "地图素材.json"),
            Field("BGI_ENEMY_CONFIG_NAME", "敌人与魔物组", "text", (), "敌人与魔物.json"),
            Field("BGI_MINE_CONFIG_NAME", "矿物组", "text", (), "矿物.json"),
            Field("BGI_COOK_CONFIG_NAME", "食材与炼金组", "text", (), "食材与炼金.json"),
            Field("BGI_HOE_CONFIG_NAME", "锄大地组", "text", (), "锄大地.json", "对应 repo/pathing/锄地专区，和敌人与魔物严格隔离。"),
        ),
    ),
    FieldGroup(
        "路线组策略",
        (
            Field("BGI_MAX_ROUTE_GROUP_SIZE", "组体量上限", "int", (), "300",
                  "超过就按下面的策略处理（BetterGI 会为每条 Disabled 写一行日志，大组容易崩）。"),
            Field("BGI_ROUTE_GROUP_POLICY", "超限策略", "choice", ("shrink", "refuse", "allow"), "shrink",
                  "shrink=自动精简并归档 / refuse=拒绝启用 / allow=照旧整组开关（崩溃风险自负）。"),
            Field("BGI_ALLOW_LARGE_ROUTE_GROUP", "兼容旧开关", "bool", (), "0", "设 1 等价于策略 allow。"),
            Field("BGI_AUTO_CREATE_ROUTE_GROUP", "自动建路线组", "bool", (), "1",
                  "调度器里没有该类目的组时，按 User\\AutoPathing\\<类目> 自动生成（目前用于锄大地）。"),
        ),
    ),
    FieldGroup(
        "飞书（可选）",
        (
            Field("FEISHU_APP_ID", "App ID", "secret", (), ""),
            Field("FEISHU_APP_SECRET", "App Secret", "secret", (), ""),
            Field("FEISHU_VERIFICATION_TOKEN", "校验 Token", "secret", (), ""),
            Field("AUTO_START_FEISHU_BOT", "随 Agent 自动启动", "bool", (), "0",
                  "1=点「启动 Agent」时顺带把飞书服务端也拉起来。默认 0：它是 Webhook 服务（监听 5000 端口），"
                  "公网不可达（没做内网穿透）时收不到任何消息，白占一个窗口。"
                  "填了 App ID / App Secret 才可能被唤醒。"),
        ),
    ),
    FieldGroup(
        "QQ 机器人（可选）",
        (
            Field("QQ_BOT_APPID", "AppID", "secret", (), "",
                  "在 https://q.qq.com 建一个机器人后拿到；填了才能用 `python main.py qq` 启动 QQ 通道。"),
            Field("QQ_BOT_SECRET", "AppSecret（密钥）", "secret", (), "", "和 AppID 配套，填错会在取 token 时返回 401/403。"),
            Field("QQ_BOT_ALLOWED_USERS", "允许的 openid", "text", (), "",
                  "逗号分隔，只认列表里的 QQ 用户；留空且没开下面那个开关时会拒收所有指令，并把对方 openid 回给 TA（先发一句就能拿到自己的 ID）。"),
            Field("QQ_BOT_ALLOW_ANYONE", "允许任何人指挥（危险）", "bool", (), "0",
                  "任何能给机器人发消息的人都能启动游戏自动化、改写 BetterGI 配置。仅公开演示时开。"),
            Field("QQ_BOT_INTENTS", "订阅事件（位掩码）", "text", (), "",
                  "留空用默认：群聊/单聊 1<<25 + 频道@我 1<<30 + 频道私信 1<<12 = 1107300352。只玩群聊填 33554432。"),
            Field("QQ_BOT_RECONNECT_SECONDS", "断线重连间隔（秒）", "int", (), "5", "网关断开后等多久重连。"),
            Field("QQ_BOT_REPLY_MODE", "回复详略", "choice", ("compact", "full"), "compact",
                  "compact=QQ 只收「要执行什么」的审批屏，完整推理留在电脑端日志（推荐）；"
                  "full=连大模型推理正文一起发到 QQ（手机上会刷屏、可能被切成好几条）。"),
            Field("QQ_BOT_BUTTONS", "审批屏挂按钮", "bool", (), "1",
                  "1=审批屏底部挂「✅ 执行 / 🧪 仅改配置 / 🚫 取消」按钮（点一下等于回复 y / t / 取消）。"
                  "按钮要在 QQ 开放平台开通（自定义按钮=内邀开通，模板按钮=申请使用）；"
                  "没开通时会自动退回纯文本，不影响使用。"
                  "没权限也能有按钮感：python main.py qq --setup-menu 会配好单聊底部自定义菜单（不需要内邀）。"),
            Field("QQ_BOT_MENU_COMMANDS", "快捷菜单 ID 映射", "text", (), "",
                  "形如 my_id=approve,another=test。如果你在开放平台管理端配了单聊快捷菜单，"
                  "把菜单项的功能 ID 写进来就能被识别（没配映射的会被忽略）。"),
            Field("AUTO_START_QQ_BOT", "随 Agent 自动启动", "bool", (), "1",
                  "1=点「启动 Agent」时顺带把 QQ 机器人拉起来（内嵌在「远程通道」页里看日志）。"
                  "只有填了 QQ_BOT_APPID / QQ_BOT_SECRET 才会被唤醒，没填就跳过并写清原因。"),
        ),
    ),
    FieldGroup(
        "米游社个人战绩（可选）",
        (
            Field("MYS_COOKIE", "米游社 Cookie", "secret", (), "",
                  "游戏展柜一次只放 8 个角色，所以「没进展柜的角色」以前问不了。填了这份 cookie，Agent 就能"
                  "直接读你账号里的全部角色（等级 / 天赋等级 / 武器），材料仍由本地字典算。\n"
                  "怎么拿：浏览器登录 www.mihoyo.com → F12 → 网络 → 点任意 mihoyo 请求 → 复制整行 Cookie"
                  "（要含 ltuid 或 account_id，以及 ltoken；建议也带上 cookie_token）。\n"
                  "⚠️ 这是账号级凭据：只保存在本机 .env（日志里只会出现掩码），别外传。留空 = 完全不启用，"
                  "一行网络请求都不会发。详细说明与排错见 docs/MYS_COOKIE.md。"),
            Field("MYS_UID", "查询的 UID", "text", (), "",
                  "留空 = 用上面的「原神 UID」；再没有就取 cookie 名下第一个原神角色。"),
            Field("MYS_CACHE_TTL_HOURS", "全角色名单缓存（小时）", "int", (), "48",
                  "名单多久拉一次，默认 2 天（玩家要求「别每次启动都拉，会触发风控」）。"
                  "天赋详情是按需拉的，不受这个值影响。想更实时就调小，嫌麻烦就调大。"),
            Field("MYS_DETAIL_MAX_PER_TURN", "每次最多补几个角色的天赋", "int", (), "3",
                  "每个角色 1 次请求；米游社风控严时别调大。"),
            Field("MYS_SALT", "签名 salt（高级，一般不用填）", "text", (), "",
                  "默认用喵喵插件/Yunzai 那套仍在用的国服参数。哪天米游社改版导致签名失效，改这里即可"
                  "（配套改下面的版本号）。留空 = 用默认值。"),
            Field("MYS_APP_VERSION", "米游社版本号（高级）", "text", (), "",
                  "和上面的 salt 配套；留空 = 默认 2.40.1。"),
        ),
    ),
)

# Keep the provider controls aligned with the current OpenAI-compatible setup.
# The override also makes older checked-out metadata understand DeepSeek.
_llm_fields = list(FIELD_GROUPS[0].fields)
_llm_fields[0] = Field(
    "LLM_PROVIDER", "模型供应商", "choice",
    ("deepseek", "openai", "github", "nvidia", "custom", "local"),
    "deepseek", "选择实际使用的模型服务。DeepSeek 使用官方兼容接口。",
)
_llm_fields[1] = Field(
    "MODEL_NAME", "模型名称", "text", (), "deepseek-chat",
    "DeepSeek 推荐 deepseek-chat；推理模型可填写 deepseek-reasoner。",
)
_llm_fields[2] = Field(
    "OPENAI_API_KEY", "模型 API 密钥", "secret", (), "",
    "填写 DeepSeek 或其他兼容服务提供的 API Key。",
)
_llm_fields[3] = Field(
    "OPENAI_BASE_URL", "兼容接口地址", "text", (),
    "https://api.deepseek.com/v1",
    "DeepSeek 官方地址：https://api.deepseek.com/v1。",
)
FIELD_GROUPS = (FieldGroup(FIELD_GROUPS[0].title, tuple(_llm_fields)),) + FIELD_GROUPS[1:]

ALL_FIELDS = {field.key: field for group in FIELD_GROUPS for field in group.fields}


def parse_env_text(text):
    """把 `.env` 文本拆成 [{key, value}]（注释、空行、无效行忽略）。"""
    entries = []
    for line in str(text or "").splitlines():
        match = ENTRY_PATTERN.match(line)
        if not match:
            continue
        entries.append({"key": match.group(3), "value": _unquote(match.group(4))})
    return entries


def env_values(text):
    """`.env` 文本 → {KEY: value}（同名键取最后一个）。"""
    return {entry["key"]: entry["value"] for entry in parse_env_text(text)}


def load_env(path):
    """读 `.env`；文件不存在时返回空字典。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return env_values(handle.read())
    except OSError:
        return {}


def update_env_text(text, updates):
    """把 updates 里的键写回文本：已有的**原地改值**，没有的追加到末尾。

    注释、空行、键顺序一律保留 —— 玩家手写的注释比整齐的排版重要。
    """
    lines = str(text or "").splitlines()
    remaining = {str(key): _stringify(value) for key, value in dict(updates or {}).items()}
    output = []

    for line in lines:
        match = ENTRY_PATTERN.match(line)
        if match and match.group(3) in remaining:
            key = match.group(3)
            indent, export = match.group(1), match.group(2) or ""
            output.append(f"{indent}{export}{key}={remaining.pop(key)}")
        else:
            output.append(line)

    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append(APPEND_SECTION)
        for key, value in remaining.items():
            output.append(f"{key}={value}")

    return "\n".join(output).rstrip("\n") + "\n"


def save_env(path, updates, backup=True):
    """把 updates 写进 `.env`（先备份再原子替换）；返回备份文件路径或 None。"""
    text = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()

    updated = update_env_text(text, updates)

    backup_path = None
    if backup and os.path.isfile(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = f"{path}.bak-{stamp}"
        shutil.copy2(path, backup_path)

    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    os.replace(temp_path, path)
    return backup_path


def mask_secret(value):
    """密钥类字段的展示值（空值显示"未设置"）。"""
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 6:
        return SECRET_MASK
    return f"{value[:3]}{SECRET_MASK}{value[-3:]}"


def validate_value(field, value):
    """校验一个字段；返回告警文案（空串 = 没问题）。只提醒，不阻断保存。"""
    value = str(value or "").strip()

    if field.kind == "int" and value:
        try:
            number = int(value)
        except ValueError:
            return f"{field.label} 必须是整数（当前：{value}）"
        if number <= 0:
            return f"{field.label} 必须是正整数（当前：{value}）"

    if field.kind == "bool" and value not in ("", "0", "1"):
        return f"{field.label} 只能填 0 或 1（当前：{value}）"

    if field.kind == "choice" and value and field.choices and value not in field.choices:
        return f"{field.label} 不在推荐值里（当前：{value}；常用：{'、'.join(field.choices)}）"

    if field.kind == "path" and value and not os.path.exists(value):
        return f"{field.label} 指向的路径不存在：{value}"

    if field.key == "MYS_COOKIE" and value:
        # 缺键的 cookie 会一路"看起来配好了"，实际每次都报未登录/风控 —— 保存时就提醒
        missing = []
        if not re.search(r"(ltuid|account_id)(_v2)?=\d{4,12}", value):
            missing.append("ltuid 或 account_id")
        if not re.search(r"(ltoken|cookie_token)=[^;\s]+", value):
            missing.append("ltoken（战绩接口必须要）")
        if missing:
            return (
                f"{field.label} 看起来不完整：缺 {'、'.join(missing)}。"
                "复制时要把整行 Cookie 都带上（用「验证 Cookie」按钮可以直接试）。"
            )

    return ""


def validate_values(values):
    """批量校验，返回告警列表。"""
    warnings = []
    for key, value in (values or {}).items():
        field = ALL_FIELDS.get(key)
        if not field:
            continue
        warning = validate_value(field, value)
        if warning:
            warnings.append(warning)
    return warnings


def _unquote(raw):
    value = str(raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _stringify(value):
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").replace("\r", " ")
    if text != text.strip():
        return f'"{text.strip()}"'
    return text
