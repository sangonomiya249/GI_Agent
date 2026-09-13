"""BetterGI 运行状态哨兵。

BetterGI 一条龙是异步跑的：Agent 触发之后就返回了，终端此前完全不知道
任务跑到哪一步、什么时候结束。本模块在后台盯住 BetterGI 自己写的日志和
进度文件，一旦捕获到「执行结束」标记，就把结果打回终端（顺带推一次飞书）。

不改动 BetterGI 任何配置，只读它自己的日志。

⚠️ 标记层级（很重要，实测于 BetterGI 0.64.0）：
    脚本执行结束: "01-月莲-护世森下-7个.json", 耗时: 2分5.495秒   ← 每条【路线】一次！
    → "任务结束"                                                  ← 每组一次，且路线级也会出现
    配置组 "地图素材" 执行结束                                      ← 【配置组】结束
    一条龙和配置组任务结束                                          ← 整个【一条龙流程】结束

  所以绝不能用「脚本执行结束」当完成信号 —— 那会在第一条路线跑完时就误报完成。
  真正的终态标记只有后两个。
"""

import json
import os
import re
import threading
import time

import config
from api import feishu_api

POLL_INTERVAL_SECONDS = 15
# 超过这个时间还没有任何一条龙活动迹象，就提醒一次（多半是没启动起来）
START_GRACE_SECONDS = 180
# 捕获到「游戏已退出」后，还要再安静这么久才认定一条龙真结束了。
# 原神中途崩溃/被重开时也会打这行，立刻上报会误判成任务完成。
GAME_EXIT_SETTLE_SECONDS = 120

# 日志里出现这些才算「一条龙真的在动」，避免把 BetterGI 开窗的日常日志误判成运行中
_ACTIVITY_MARKERS = (
    "TaskControl",
    "ScriptService",
    "TaskRunner",
    "配置组",
    "脚本执行结束",
    "一条龙",
)

# 更严格的「一条龙已启动」证据：只有真正开跑才会打这些
_START_MARKERS = ('→ "任务启动！"', "脚本执行结束:", '配置组 "', "一条龙和配置组")

# 终态标记：一条龙流程结束（最权威）
_FLOW_DONE_RE = re.compile(r"一条龙和配置组任务结束")
# 终态标记：单个配置组结束（流程标记的上一级，实测可能只出现这个）
_GROUP_DONE_RE = re.compile(r'配置组\s*"([^"]+)"\s*执行结束')
# 仅供统计用的路线级明细，不能当完成信号
_ROUTE_DONE_RE = re.compile(
    r'脚本执行结束:\s*"([^"]+)"\s*(?:,|，)\s*耗时:\s*([0-9.]+[^\s,，]*)'
)

_GAME_EXIT_MARKER = "游戏已退出"
_FAILURE_MARKER = "任务执行失败"
_PICKUP_MARKER = "交互或拾取"
# 🌟 脚本自己处理的失败：这些**不影响**整轮结束（脚本会 continue 下一个目标），
#    但玩家有权知道 —— 实测 2026-09-12 那次 Boss 讨伐：
#       已进入征讨之花领奖界面 → 领取失败，可能是原粹树脂不足 → ❌讨伐『恒常机关阵列』失败
#    整条流程照样打「配置组执行结束」，报告里只有一句 🎉 完成，玩家根本看不出白跑了一趟。
#    先匹配具体的句式，最后一条是兜底（别的脚本有自己的报错措辞）。
_SCRIPT_FAILURE_PATTERNS = (
    (re.compile(r"❌讨伐『([^』]+)』失败"), "讨伐失败"),
    (re.compile(r"💀战斗失败，跳过当前BOSS\s*(\S+)"), "战斗失败跳过"),
    (re.compile(r"领取奖励失败[:：]\s*(.+)"), "领奖失败"),
    (re.compile(r"❌\s*([^\n❌]{2,30}?)失败[:：]"), "脚本报错"),
)
# 已经有配置组跑完、但一直等不到整条流程的结束标记时，日志安静这么久就认定结束了
_GROUP_QUIET_SECONDS = 300
# 判定"结束"之前再等这么久、再读一次日志：被取消时「任务被取消」比「执行结束」晚 1 秒落地
_FINAL_CONFIRM_SECONDS = 2.0

# 🌟 取消/停止标记（实测自玩家自己的 better-genshin-impact 日志）：
#     HotKeyPageViewModel    → 检测到您配置的停止快捷键"Up"按下，停止当前执行任务
#     OneDragonFlowViewModel → 任务被取消，退出执行
#   ⚠️ 关键实测：**被取消的那次 BetterGI 照样会打「配置组 "X" 执行结束」**，顺序是
#        17:43:02  配置组 "批量讨伐角色养成材料BOSS" 执行结束      ← 先报"结束"
#        17:43:03  任务被取消，退出执行                            ← 一秒后才说其实是被停了
#      所以只认完成标记就会把"玩家按了停止"报成「🎉 完成」。
#   注意别把「已获取取消令牌」当成取消信号：那是 BGI 正常起任务时就打的。
_CANCEL_MARKERS = ("任务被取消", "停止当前执行任务")

_watch_lock = threading.Lock()
_active_generation = 0


def today_log_path():
    """BetterGI 主日志路径（按天分割）。"""
    return os.path.join(
        config.BGI_LOG_DIR, time.strftime("better-genshin-impact%Y%m%d.log")
    )


class _LogTail:
    """只读取「触发之后」新增的日志，避免把历史日志里的旧完成标记当成本次结果。

    `start_at_end=True`（默认）：从当前文件末尾开始读 —— 用于"监控起点"。
    `start_at_end=False`：从头读 —— 用于跨天换到新文件时补读已经写进去的行。
    """

    def __init__(self, path, start_at_end=True):
        self.path = path
        try:
            size = os.path.getsize(path) if os.path.exists(path) else 0
        except OSError:      # 文件正好在这一瞬间被工具/轮转删掉
            size = 0
        self.offset = size if start_at_end else 0

    def read_new(self):
        if not os.path.exists(self.path):
            return ""
        try:
            size = os.path.getsize(self.path)
            if size < self.offset:  # 日志被轮转/重建
                self.offset = 0
            if size == self.offset:
                return ""
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.offset)
                text = f.read()
                self.offset = f.tell()
            return text
        except OSError:
            return ""


def _refresh_tail(holder, make_path=None):
    """保证在盯"今天的日志文件"：路径变了（跨午夜）就换一个新 tail。

    `holder` 是 `[tail]` 这种单元素列表 —— 闭包里改外层名字会把它变成局部变量（会
    UnboundLocalError），装进列表就没有重新绑定这回事。

    返回 `(tail, 是否切换过)`。新文件从 offset 0 开始读：换天后的内容都属于本次任务。
    """
    make_path = make_path or today_log_path
    current = make_path()
    tail = holder[0]
    if tail is None or tail.path != current:
        # 新文件从**头**读：跨天那一刻到我们切过来之间写进去的行（可能就有"执行结束"）不能漏
        holder[0] = _LogTail(current, start_at_end=False)
        return holder[0], True
    return tail, False


def _snapshot_progress():
    try:
        return {
            name
            for name in os.listdir(config.BGI_TASK_PROGRESS_DIR)
            if name.endswith(".json")
        }
    except OSError:
        return set()


def _latest_progress(baseline):
    """返回 baseline 之后新出现的最新一条进度记录（用于补充报告明细）。"""
    try:
        names = [
            name
            for name in os.listdir(config.BGI_TASK_PROGRESS_DIR)
            if name.endswith(".json") and name not in baseline
        ]
    except OSError:
        return None

    for name in sorted(names, reverse=True):
        path = os.path.join(config.BGI_TASK_PROGRESS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _human_duration(seconds):
    seconds = int(max(0, seconds))
    hours, remain = divmod(seconds, 3600)
    minutes, secs = divmod(remain, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def expected_group_names():
    """当前生效的一条龙里，**会打"配置组执行结束"行**的组名（可能有多组）。

    为什么需要："配置组 X 执行结束"是**每组一次**，不是整轮一次。两个组都开着时，
    第一组跑完就报「🎉 完成」是错的（玩家实测：他看到这句时另一个组还没开始）。
    组名以 ScriptGroup/<名>.json 里的 name 字段为准（实测日志里打的就是这个，
    例如 OneDragon 任务叫「狗粮AAA」，但组名可能是另一个文件里的「狗粮」）。

    读不到（BGI 没装/配置刚被删）就返回 []，调用方退回老行为：见到组结束就算结束。
    """
    try:
        from skills import bgi_controller            # 延迟导入：bgi_controller 反过来 import 本模块

        path = bgi_controller.resolve_one_dragon_config_path()
        with open(path, "r", encoding="utf-8") as handle:
            one_dragon = json.load(handle)
    except Exception:                                # noqa: BLE001 —— 读不到就走兜底，不能因此不报信
        return []

    definitions = one_dragon.get("TaskDefinitions")
    definitions = definitions if isinstance(definitions, dict) else {}
    enabled = one_dragon.get("TaskEnabledList")
    if not isinstance(enabled, dict):
        return []

    groups = []
    for key, value in enabled.items():
        if not value:
            continue
        name = str(definitions.get(key, key) or "")
        if not name:
            continue
        # 只有**脚本组**才会打「配置组 "X" 执行结束」；BetterGI 内置任务（领取邮件、合成树脂…）
        # 不会打，算进来的话就永远等不到"全部跑完"，只能干等超时。
        group_file = os.path.join(config.BGI_SCRIPT_GROUP_DIR, f"{name}.json")
        if not os.path.isfile(group_file):
            continue
        aliases = {name}
        try:
            with open(group_file, "r", encoding="utf-8") as handle:
                inner = json.load(handle).get("name")
            if inner:
                aliases.add(str(inner))
        except (OSError, ValueError, AttributeError):
            pass
        groups.append(frozenset(aliases))
    return groups


def _failure_key(text):
    """比对用归一化：去掉标点/引号/空白**和标签词**，只留"到底是哪个目标出事"。

    `讨伐『恒常机关阵列』` 与 `讨伐失败：恒常机关阵列` 会归一化成同一个键 —— 否则兜底规则
    会给同一次失败再加一条同义记录。
    """
    text = re.sub(r"[『』「」【】:：,，、\s]", "", str(text or ""))
    for noise in ("讨伐失败", "战斗失败跳过", "领奖失败", "脚本报错", "讨伐", "失败"):
        text = text.replace(noise, "")
    return text


def script_failures(log_text):
    """脚本自己记下的失败（讨伐失败 / 战斗跳过 / 领奖失败…），去重后返回人话描述。"""
    found = []
    for pattern, label in _SCRIPT_FAILURE_PATTERNS:
        for match in pattern.finditer(log_text or ""):
            detail = str(match.group(1) or "").strip()
            # 兜底那条会重复命中最具体的句式（`❌讨伐『X』失败` 也会被 `❌…失败` 匹配到），
            # 已经记过的内容就别再加一条同义的
            if detail and any(
                _failure_key(detail) in _failure_key(item) for item in found
            ):
                continue
            text = f"{label}：{detail}" if detail else label
            if text not in found:
                found.append(text)
    return found


def parse_terminal(log_text):
    """判断日志片段里是否出现了真正的终态标记，并抽取明细。

    返回 dict：finished / flow_done / groups / routes / route_duration / cancelled
    """
    log_text = log_text or ""
    flow_done = bool(_FLOW_DONE_RE.search(log_text))
    groups = list(dict.fromkeys(_GROUP_DONE_RE.findall(log_text)))
    cancelled = any(marker in log_text for marker in _CANCEL_MARKERS)

    routes = 0
    route_duration = ""
    for match in _ROUTE_DONE_RE.finditer(log_text):
        if match.group(1) and match.group(1).endswith(".json"):
            routes += 1
        if match.group(2):
            route_duration = match.group(2)

    trips = script_failures(log_text)
    return {
        "finished": flow_done or bool(groups),
        "flow_done": flow_done,
        "groups": groups,
        "routes": routes,
        "route_duration": route_duration,
        "cancelled": cancelled,
        "script_failures": trips,
    }


def all_groups_done(info, expected_groups):
    """expected_groups（别名集合的列表）是否都已经打过结束行。

    expected_groups 为空 → 退回老行为（见到任意组结束就算结束）。
    """
    if not expected_groups:
        return True
    seen = {str(name) for name in info.get("groups") or []}
    return all(aliases & seen for aliases in expected_groups)


def build_report(log_text, progress, elapsed_seconds):
    """把日志片段 + 进度记录整理成终端可读的完成报告。"""
    log_text = log_text or ""
    info = parse_terminal(log_text)
    lines = [f"   ⏱️  本次耗时：{_human_duration(elapsed_seconds)}"]

    if len(info["groups"]) == 1:
        lines.append(f"   🗂️  配置组：{info['groups'][0]}")
    elif info["groups"]:
        lines.append(f"   🗂️  配置组：{'、'.join(info['groups'])}")

    if info["routes"]:
        lines.append(f"   ✅  完成路线：{info['routes']} 条")

    if isinstance(progress, dict):
        history = progress.get("history") or []
        done = sum(1 for item in history if isinstance(item, dict) and item.get("taskEnd"))
        if done and not info["routes"]:
            lines.append(f"   ✅  完成路线：{done} 条（依据进度文件）")

    failures = log_text.count(_FAILURE_MARKER)
    if failures:
        lines.append(f"   ❌  失败路线：{failures} 条")
    script_errors = list(info.get("script_failures") or [])
    if script_errors:
        lines.append(f"   ❌  脚本内报错 {len(script_errors)} 处：" + "；".join(script_errors[:5]))
        if len(script_errors) > 5:
            lines.append(f"       （还有 {len(script_errors) - 5} 处，完整内容见上面的日志）")
        lines.append(
            "   ↳ 这类是脚本自己跳过的（整条一条龙照样算结束）："
            "可能已经领到奖励、也可能白扣了体力，材料对不上时先看这几条。"
        )
    pickups = log_text.count(_PICKUP_MARKER)
    if pickups:
        lines.append(f"   🌿  触发拾取：{pickups} 次（近似统计）")
    return lines


def _announce(title, body_lines, open_id):
    banner = ["", "=" * 56, title, *body_lines, "=" * 56, ""]
    text = "\n".join(banner)
    print(text)
    # 这是"迟到通知"：一条龙可能跑几十分钟，被动回复凭据（5 分钟）和回复额度（每条消息 5 次）
    # 早就用完了。QQ 通道会自己改走主动消息、再不行就排队等玩家下次说话时补发（见 channels/qq_bot.py）。
    feishu_api.send_feishu_msg(open_id, text)


def foreground_is_game():
    """前台是不是原神。True / False / None（判不出来）。测试里会打桩。"""
    try:
        from skills import window_focus

        return window_focus.foreground_is_game()
    except Exception:        # noqa: BLE001 —— 探测失败就当判不出来，绝不影响监控
        return None


def focus_game_window():
    """尝试把原神切回前台，返回是否成功（测试里会打桩）。"""
    try:
        from skills import window_focus

        return window_focus.focus_game_window()
    except Exception:        # noqa: BLE001
        return False


def describe_foreground():
    try:
        from skills import window_focus

        return window_focus.describe_foreground()
    except Exception:        # noqa: BLE001
        return "前台窗口未知"


def _handle_focus_lost(open_id, strikes, tries, elapsed, notified):
    """原神不在前台时的兜底。

    BetterGI 的做法是**每秒检查一次前台窗口，不是原神就暂停**（实测日志：
    `当前获取焦点的窗口为: QQ，不是原神，暂停`）。所以"卡死"往往是"在等游戏回前台"。
    这里：① 连续 `BGI_FOCUS_GUARD_STRIKES` 次发现跑偏 → 提示一次（让玩家知道不是崩了）；
          ② `BGI_FOCUS_GUARD=1` 时顺带把原神抢回前台，最多抢 `BGI_FOCUS_GUARD_MAX_TRIES` 次，
             抢不到就罢手（免得和正在用电脑的玩家抢鼠标）。
    返回 (tries, notified)。
    """
    threshold = max(1, int(config.BGI_FOCUS_GUARD_STRIKES))
    if strikes < threshold:
        return tries, notified

    if not notified:
        notified = True
        _announce(
            f"⏸️ 原神不在前台，BetterGI 正在暂停（已连续 {strikes} 次巡检不是原神）。",
            [
                f"   {describe_foreground()}",
                "   BGI 的模拟输入要求游戏保持前台（前台 SendInput），"
                "所以它会一直每秒重试、等着原神回前台。",
                "   ↳ 点一下游戏窗口即可继续；根治办法是在 BetterGI 设置里打开"
                "「失去焦点时自动切回原神」。",
            ],
            open_id,
        )

    if not config.BGI_FOCUS_GUARD:
        return tries, notified

    limit = max(1, int(config.BGI_FOCUS_GUARD_MAX_TRIES))
    if tries >= limit:
        return tries, notified

    if focus_game_window():
        print(f"🪟 已把原神抢回前台（第 {tries + 1} 次）。")
        return tries + 1, notified

    tries += 1
    if tries >= limit:
        _announce(
            "🪟 尝试把原神切回前台失败，已停止自动切换。",
            [
                "   ↳ 可能是 Windows 前台锁或权限问题；请手动点一下游戏窗口。",
                "   ↳ 也可以打开 BetterGI 自己的「失去焦点时自动切回原神」。",
            ],
            open_id,
        )
    return tries, notified


def start_completion_watch(open_id="CLI_USER", timeout_seconds=None):
    """BetterGI 触发成功后调用：后台盯日志，任务结束时在终端报信。"""
    global _active_generation
    with _watch_lock:
        _active_generation += 1
        generation = _active_generation

    timeout = timeout_seconds or config.BGI_WATCH_TIMEOUT_SECONDS
    baseline = _snapshot_progress()
    # ⚠️ 用单元素列表装 tail，而且只在闭包**外面**创建：闭包里任何 `tail = ...`
    #    都会把 tail 变成 _worker 的局部变量，于是"先赋值再使用"成了硬要求 ——
    #    这个坑我踩了两次（第一次 `tail = _LogTail(...)`，第二次 `tail, switched = ...`），
    #    两次都是 UnboundLocalError 把监视线程当场干掉、完成报告永远发不出来。
    tail_holder = [_LogTail(today_log_path())]
    started = time.time()
    # 🌟 本轮"该跑几个组"：配置组结束行是每组一次的，多组时不能拿第一组当整轮结束
    expected_groups = expected_group_names()
    if len(expected_groups) > 1:
        print(f"🔭 本轮一条龙里有 {len(expected_groups)} 个配置组，要全部跑完才会报完成。")

    print(
        f"🔭 已开始监控 BetterGI 运行状态（每 {POLL_INTERVAL_SECONDS} 秒检查一次），"
        "任务完成后会自动报信。"
    )

    def _worker():
        collected = []
        warned = False
        start_confirmed = False
        game_exit_at = None
        game_exit_offset = None
        partial_at = None
        partial_offset = None
        focus_strikes = 0
        focus_tries = 0
        focus_notified = False
        while True:
            time.sleep(POLL_INTERVAL_SECONDS)
            with _watch_lock:
                if generation != _active_generation:
                    return  # 又开新一轮，本轮结果作废
            elapsed = time.time() - started

            # 🌟 跨午夜换文件：BetterGI 的日志按自然日切名（better-genshin-impactYYYYMMDD.log）。
            #    一条龙晚上起跑、过了 0 点才结束时，如果一直读旧文件，就永远看不到"执行结束"标记，
            #    只能白等到 6 小时超时。所以每轮重新算一次路径，变了就换到新文件从头读。
            #    （这里的 `tail` 是 _worker 的局部名字：先赋值、后使用，不会再去找外层。）
            tail, switched = _refresh_tail(tail_holder)
            if switched:
                print(f"📄 BetterGI 日志已跨天，切换到 {os.path.basename(tail.path)}")

            chunk = tail.read_new()
            if chunk:
                collected.append(chunk)
            log_text = "".join(collected)

            # ⓪ 先给一次正面确认：让“到底动没动”不再靠猜
            if not start_confirmed and any(m in log_text for m in _START_MARKERS):
                start_confirmed = True
                print("✅ 已确认 BetterGI 开始执行任务，继续等待完成信号…")
                feishu_api.send_feishu_msg(open_id, "✅ 已确认 BetterGI 开始执行任务。")

            # ① 主信号：真正的终态标记（配置组结束 / 一条龙流程结束），或被停止/取消
            info = parse_terminal(log_text)
            if info["finished"] or info["cancelled"]:
                # ⚠️ 实测的竞态：被停止的那次 BGI **照样**打「配置组 X 执行结束」，而
                #    「任务被取消，退出执行」要晚 1 秒才落地（17:43:02 → 17:43:03）。
                #    轮询间隔 15 秒一般能把两行收在同一个片段里，但刚好卡在这 1 秒中间就会
                #    误报「🎉 完成」—— 所以判定终态之前再看一眼日志，把晚到的那行收进来。
                time.sleep(_FINAL_CONFIRM_SECONDS)
                extra = tail.read_new()
                if extra:
                    collected.append(extra)
                    log_text = "".join(collected)
                    info = parse_terminal(log_text)

                if info["cancelled"] and start_confirmed:
                    _announce(
                        "⛔ BetterGI 任务被取消（按了停止快捷键，或被手动/脚本停止），本轮没跑完。",
                        build_report(log_text, _latest_progress(baseline), elapsed),
                        open_id,
                    )
                    return

                if info["finished"] and (
                    info["flow_done"] or all_groups_done(info, expected_groups)
                ):
                    progress = _latest_progress(baseline)
                    script_errors = info.get("script_failures") or []
                    if script_errors:
                        # 有脚本内报错就别只报 🎉 —— 玩家实测踩过：白跑一趟只看得到"完成"
                        title = (
                            f"⚠️ BetterGI 一条龙执行结束（脚本内有 {len(script_errors)} 处失败）"
                            if info["flow_done"]
                            else f"⚠️ BetterGI 配置组执行结束（脚本内有 {len(script_errors)} 处失败）"
                        )
                    else:
                        title = (
                            "🎉 BetterGI 一条龙执行完成！"
                            if info["flow_done"]
                            else "🎉 BetterGI 配置组执行结束！"
                        )
                    _announce(title, build_report(log_text, progress, elapsed), open_id)
                    return

            # ①.6 有组跑完了、但整条流程的结束标记一直没出现（多组时可能卡在后面的组）：
            #     如果日志也彻底安静下来，就按"结束"报，别干等到超时一个字都不说。
            if info["groups"] and not all_groups_done(info, expected_groups):
                if partial_offset != tail.offset:
                    partial_at = time.time()
                    partial_offset = tail.offset
                elif time.time() - partial_at >= _GROUP_QUIET_SECONDS:
                    _announce(
                        "🏁 已有配置组结束、日志随后一直安静，BetterGI 应已结束"
                        "（未捕获到整条一条龙的结束标记）。",
                        build_report(log_text, _latest_progress(baseline), elapsed),
                        open_id,
                    )
                    return

            # ①.7 前台跑偏：BGI 会一直「不是原神，暂停」等着（实测每秒一行）。
            #     这跟"崩了"长得一模一样，所以要么告诉玩家，要么帮他切回去。
            if foreground_is_game() is False:
                focus_strikes += 1
                focus_tries, focus_notified = _handle_focus_lost(
                    open_id, focus_strikes, focus_tries, elapsed, focus_notified
                )
            else:
                if focus_strikes:
                    print("🪟 原神已回到前台，继续跑。")
                focus_strikes = 0

            # ② 兜底信号：原神已退出 + 日志彻底安静，才认定一条龙结束了。
            #    必须先静默一段时间：中途崩溃重开也会打「游戏已退出」。
            if _GAME_EXIT_MARKER in log_text:
                if game_exit_at is None or tail.offset != game_exit_offset:
                    game_exit_at = time.time()
                    game_exit_offset = tail.offset
                elif time.time() - game_exit_at >= GAME_EXIT_SETTLE_SECONDS:
                    _announce(
                        "🏁 原神已退出且日志已安静，BetterGI 一条龙应已结束（未捕获到明确的结束标记）。",
                        build_report(log_text, _latest_progress(baseline), elapsed),
                        open_id,
                    )
                    return

            # ③ 一条龙迟迟没动静 → 提醒一次（常见于启动参数没生效 / 原神没开）
            if not warned and elapsed > START_GRACE_SECONDS:
                if not any(marker in log_text for marker in _START_MARKERS):
                    warned = True
                    _announce(
                        f"⚠️ 已等待 {_human_duration(elapsed)}，仍未检测到一条龙运行迹象。",
                        [
                            "   可能原因：BetterGI 已在运行时被“热启动”（单实例程序不会触发新任务）、",
                            "             原神没运行、配置里没有启用任何任务，",
                            "             或者回退启动时弹出的 UAC 授权窗口还没点「是」"
                            "（QQ 上现在不发启动回执，所以这里一并说明）。",
                            f"   可查看日志：{tail.path}",
                        ],
                        open_id,
                    )

            if elapsed > timeout:
                _announce(
                    f"⏱️ 等待超过 {_human_duration(timeout)}，仍未收到完成信号，已停止监控。",
                    [
                        "   若 BetterGI 仍在正常跑图，可以忽略本条；",
                        f"   也可查看日志确认：{tail.path}",
                    ],
                    open_id,
                )
                return

    def _guarded():
        """把 `_worker` 包一层：**异常必须留痕**。

        ⚠️ 踩过两次的坑：监视线程里一个未捕获异常（UnboundLocalError 那两次）会让线程静默死掉，
        表现就是"完成报告永远发不出来"，而终端上一句提示都没有。这里至少把堆栈打出来。
        """
        try:
            _worker()
        except Exception as exc:        # noqa: BLE001 —— 任何异常都不许静默
            import traceback

            traceback.print_exc()
            print(f"❌ 完成监视线程异常退出（本轮可能收不到完成报告）：{type(exc).__name__} {exc}")

    threading.Thread(target=_guarded, daemon=True, name="bgi-completion-watch").start()
