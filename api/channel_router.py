"""会话通道路由：把"回复某个会话"这件事从飞书里解耦出来。

背景：Agent 的全套逻辑（`brain/llm_brain.ask_agent`、`skills/bgi_controller.execute_bgi_task`）
都用 `feishu_api.send_feishu_msg(target, text)` 往玩家那边回话。要加 QQ 接口时，
只要让这个函数在"目标是 QQ 会话"时改走 QQ，就不用把整套审批/拦截/事务逻辑复制一遍。

约定：目标字符串（target）自带通道前缀，形如：

* 飞书：`ou_xxx`（原样，历史数据也是这种）
* QQ 群：`qq:group:<group_openid>#<msg_id>`
* QQ 单聊：`qq:c2c:<user_openid>#<msg_id>`
* QQ 频道：`qq:guild:<channel_id>#<msg_id>`

`#<msg_id>` 是 QQ 官方接口要求的"被动回复"凭据（5 分钟内有效），由发送方解析。

除了"发到哪"，通道还能决定"长什么样"：

* `register_formatter(prefix, fn)`：发给这个通道的文本先过一遍 `fn`（QQ 会把 Markdown、
  ```json 计划块这些"给终端看的"东西去掉，见 `channels/chat_text.py`）；
* `register_chat_channel(prefix, concise=True, quiet=True)`：标记这是手机聊天通道 ——
  `concise` 让 `llm_brain` 只发精简版审批屏（完整推理留在电脑端日志），
  `quiet` 让 `bgi_controller` / `bgi_watcher` 跳过"正在下发配置…""备份目录…"这类进度消息。
  两者都是**实测踩出来的**：不精简时一条审批被切成 4 条；不安静时启动过程要刷 5 条，
  正好吃光官方"每条玩家消息最多回 5 次"的额度，最后 20 分钟的完成报告反而发不出去。

* `set_input_only(prefix, True)`：**单向模式** —— 这个通道只用来"收指令"，
  Agent 的回话（审批屏 / 完成报告 / 报错）不再外发，而是**原样打到本地终端与 Studio 日志**。
  用来实现"QQ/飞书 下达指令 → 本机 Agent 执行，Agent 只在运行界面说话"。
  实现方式是"拦在出口 + 本地回显"：通道发送函数根本不会被调用（不消耗平台额度、
  也不会因为平台限制丢消息），内容一个字不少地留在电脑上 —— 否则你会在本地
  看不到审批屏，也就无从批准 QQ 发来的那条指令。
"""

from typing import Callable, Dict, Optional

# 前缀 → 发送实现。注册后，这个前缀下的目标就由对应通道负责投递。
_SENDERS: Dict[str, Callable[[str, str], bool]] = {}

# 前缀 → 显示处理（把给终端写的文本改成适合聊天软件的样子，见 channels/chat_text.py）。
_FORMATTERS: Dict[str, Callable[[str], str]] = {}

# 前缀 → 是不是"手机聊天通道"。
# 这类通道有两个特点（都是实测踩出来的）：
#   concise=True：审批消息只发"要执行什么"，完整推理留在电脑日志里；
#   quiet=True：进度类消息（"正在下发配置…"、"配置事务已提交…"、"备份目录…"）不发，
#               否则一条龙还没跑起来就先刷 4~5 条，还会把"每条玩家消息最多回 5 次"的额度吃光，
#               导致 20 分钟后真正的完成报告发不出去。
_CHAT_CHANNELS: Dict[str, Dict[str, bool]] = {}

# 前缀 → 只收不发（单向模式，见模块文档）。命中时：通道发送函数**不调用**，
# 内容原样打到本地终端（Studio 的日志就是它的 stdout）。
_INPUT_ONLY: Dict[str, bool] = {}

# 单向模式的说明只打一次，之后每条消息只加一行标记（免得刷屏）
_INPUT_ONLY_NOTICE_SHOWN = False

QQ_PREFIX = "qq:"


def set_input_only(prefix: str, enabled: bool = True) -> None:
    """把这个通道设成/取消"单向模式"（只收指令，不回话；回话改成本地回显）。"""
    if not prefix:
        raise ValueError("prefix 不能为空")
    _INPUT_ONLY[str(prefix)] = bool(enabled)


def _input_only_prefix(target: str) -> str:
    """命中的单向前缀（最长匹配）；不是单向通道就返回空串。"""
    best = ""
    for prefix, enabled in _INPUT_ONLY.items():
        text = str(target or "")
        if enabled and text.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return best


def is_input_only(target: str) -> bool:
    """这个目标是不是"单向通道"（只收指令、不回话）。"""
    return bool(_input_only_prefix(target))


def echo_locally(label: str, text: str) -> None:
    """单向模式下的本地回显：内容照原样打出来（终端 / Studio 日志都能看到）。"""
    global _INPUT_ONLY_NOTICE_SHOWN
    if not _INPUT_ONLY_NOTICE_SHOWN:
        _INPUT_ONLY_NOTICE_SHOWN = True
        print(
            f"🔇 单向模式（{label}）：Agent 的回话不再发到聊天软件，全部只显示在本地 ——\n"
            "   审批请在这里（终端输入 y / t，Studio 的输入框也行），完成报告与报错同样只在这里出现。"
        )
    print(f"🔇 [{label}] {text}")


# 前缀 → 回显时给人看的名字（`qq:` → QQ）
_PREFIX_LABELS = {"qq:": "QQ"}


def reset_notices() -> None:
    """测试用：把"提示只打一次"的状态清掉。"""
    global _INPUT_ONLY_NOTICE_SHOWN
    _INPUT_ONLY_NOTICE_SHOWN = False


def register_sender(prefix: str, sender: Callable[[str, str], bool]) -> None:
    """注册一个通道（prefix 形如 `qq:`），sender(target, text) 返回是否发出去。"""
    if not prefix or not callable(sender):
        raise ValueError("prefix 和 sender 都不能为空")
    _SENDERS[str(prefix)] = sender


def unregister_sender(prefix: str) -> None:
    _SENDERS.pop(str(prefix), None)


def register_formatter(prefix: str, formatter: Callable[[str], str]) -> None:
    """注册显示处理：发给这个通道的每一条消息都会先过一遍 formatter(text) -> text。"""
    if not prefix or not callable(formatter):
        raise ValueError("prefix 和 formatter 都不能为空")
    _FORMATTERS[str(prefix)] = formatter


def register_chat_channel(prefix: str, concise: bool = True, quiet: bool = True) -> None:
    """标记这个通道是"手机聊天通道"（决定审批消息详略 + 要不要发进度消息）。"""
    if not prefix:
        raise ValueError("prefix 不能为空")
    _CHAT_CHANNELS[str(prefix)] = {"concise": bool(concise), "quiet": bool(quiet)}


def clear() -> None:
    _SENDERS.clear()
    _FORMATTERS.clear()
    _CHAT_CHANNELS.clear()
    _INPUT_ONLY.clear()
    reset_notices()


def registered_prefixes():
    return tuple(_SENDERS.keys())


def _longest_match(mapping: Dict[str, object], target: str):
    target = str(target or "")
    best = None
    for prefix, value in mapping.items():
        if target.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, value)
    return best[1] if best else None


def sender_for(target: str) -> Optional[Callable[[str, str], bool]]:
    """按最长前缀匹配找出负责这个目标的通道；没有就返回 None（= 走飞书默认逻辑）。"""
    return _longest_match(_SENDERS, target)


def formatter_for(target: str) -> Optional[Callable[[str], str]]:
    return _longest_match(_FORMATTERS, target)


def _chat_flag(target: str, name: str) -> bool:
    style = _longest_match(_CHAT_CHANNELS, target)
    return bool((style or {}).get(name))


def wants_concise(target: str) -> bool:
    """这个目标是不是"手机聊天通道"（审批消息只发精简版）。"""
    return _chat_flag(target, "concise")


def wants_quiet(target: str) -> bool:
    """这个目标要不要跳过"进度类"消息（终端/飞书照发，手机聊天通道不发）。"""
    return _chat_flag(target, "quiet")


def owns(target: str) -> bool:
    """这个目标是不是属于某个已注册通道（用来避免"QQ 发送失败后又拿它去调飞书"）。"""
    return sender_for(target) is not None


def format_for(target: str, text: str) -> str:
    """过一遍通道的显示处理；formatter 出错就原样返回（显示问题不该弄丢消息）。"""
    formatter = formatter_for(target)
    if formatter is None:
        return text
    try:
        return formatter(text)
    except Exception as exc:  # pragma: no cover - 显示处理必须无副作用
        print(f"⚠️ 通道文本处理失败（{target}）：{exc}")
        return text


def try_send(target: str, text: str) -> bool:
    """如果目标属于已注册的其它通道就由它发送并返回 True；否则返回 False（调用方继续走原逻辑）。

    ★ 还有一种情况：目标属于**别的进程**才发得出去的通道（QQ 只能由 QQ 机器人进程发）。
    这时不能返回 False —— 那会让调用方（`feishu_api.send_feishu_msg`）拿 `qq:...`
    当飞书 open_id 去发，最后**消息凭空消失、日志里连错都没有**（玩家反馈过两次）。
    所以我们把它塞进跨进程队列（`api/notice_queue.py`），由 QQ 机器人进程取走发送。
    """
    sender = sender_for(target)
    if sender is None:
        if belongs_to_another_process(target):
            from api import notice_queue

            if notice_queue.enqueue(target, text):
                return True
            # 入队都失败（队列被占 / 磁盘问题）：至少别静默 —— 打出来让人看得见
            echo_locally("推送待补发（入队失败）", text)
        return False
    formatted = format_for(target, text)
    prefix = _input_only_prefix(target)
    if prefix:
        # 单向模式：**不发**，但内容一定要在本地看到（否则审批屏就丢了，没法批准）
        label = _PREFIX_LABELS.get(prefix, prefix.rstrip(":") or str(target))
        echo_locally(label, formatted)
        return True
    try:
        return bool(sender(target, formatted))
    except Exception as exc:  # 通道出错不能把主流程带崩
        print(f"❌ 通道投递失败（{target}）：{exc}")
        return False


def belongs_to_another_process(target: str) -> bool:
    """这个目标是不是"本进程发不了、得靠另一个进程发的通道"（目前只有 QQ）。

    QQ 的发送实现（`qq_bot` 的 sender）只在**机器人进程**里注册；CLI / Studio
    这两个进程里 `sender_for()` 一定是 None —— 但目标本身是合法的 QQ 会话标识。
    判据要同时满足：① 是 QQ 目标；② 本进程**没有**注册它的发送器。
    """
    text = str(target or "")
    if not text.startswith(QQ_PREFIX):
        return False
    return sender_for(text) is None
