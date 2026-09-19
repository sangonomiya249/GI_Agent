import os
import json
import shutil
import tempfile
import time

import config


def _default_store(uid=None):
    """一份干净的 store（加载失败/文件不存在时用它）。"""
    return {
        "uid": uid if uid is not None else config.DEFAULT_UID,
        "env_context": "",
        # 🌟 展柜数据的抓取时间：缺了它，缓存就会被当成「最新展柜」永久使用
        "env_context_at": "",
        "messages": [],
        "wallet": {"mora": 0, "exp_books": 0, "boss_mats": {}},
        "pending_task": None,
        # 🌟 "上次跟谁说话"：主动推送（启动推执行目标 / 跑完推下一条路线）靠它找到目标。
        #    **必须在这里和 load_chat_store() 的白名单里都列出来** ——
        #    漏一个，值就会被加载时丢掉，表现是"推送静默不出去"（踩过）。
        "last_target": "",
    }


def load_chat_store():
    """加载持久化对话存储。

    ⚠️ 文件坏了（半截 JSON / 手工改坏）时**不会**静默当成空记忆：
    先把原文件改名成 `chat_context.json.corrupt-<时间戳>` 留证据再返回空 store ——
    否则下一次保存会把空 store 落盘，历史对话 / 虚拟账本 / 待审批任务就永久没了。
    """
    default_wallet = {"mora": 0, "exp_books": 0, "boss_mats": {}}

    HISTORY_FILE = config.HISTORY_FILE
    DEFAULT_UID = config.DEFAULT_UID

    if not os.path.exists(HISTORY_FILE):
        return _default_store()

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        if isinstance(raw, list):
            return {
                "uid": DEFAULT_UID,
                "env_context": "",
                "env_context_at": "",
                "messages": raw,
                "wallet": default_wallet,
                "pending_task": None,
            }

        loaded_wallet = raw.get("wallet", {})
        if not isinstance(loaded_wallet, dict):
            loaded_wallet = {}
        merged_wallet = {**default_wallet, **loaded_wallet}

        if not isinstance(merged_wallet.get("boss_mats"), dict):
            merged_wallet["boss_mats"] = {}
        # 摩拉/经验书被写坏成 null / 字符串时，别让后面的 `wallet["mora"] += 480000` 抛 TypeError
        for key in ("mora", "exp_books"):
            try:
                merged_wallet[key] = int(merged_wallet.get(key) or 0)
            except (TypeError, ValueError):
                merged_wallet[key] = 0

        pending_task = raw.get("pending_task")
        if isinstance(pending_task, dict):
            # 🌟 待审批计划有两种形态：
            #    ① 跑图计划：{"bgi_cmd": {...}, "open_id", "uid"}
            #    ② 系统操作：{"system_task": {"action": "close_game"}, "open_id", "uid"}
            #    ⚠️ 踩过的坑：这里以前硬要求 `bgi_cmd` 存在，于是"帮我关闭原神"生成的
            #    系统操作计划**在读出时被整个丢掉** —— 玩家回 y 时看不到待审批计划，
            #    消息被当成普通聊天喂给大模型，它只好又规划了一遍打 Boss（实测 22:18 那次的怪象）。
            has_plan = isinstance(pending_task.get("bgi_cmd"), dict) or isinstance(
                pending_task.get("system_task"), dict
            )
            if not has_plan or "open_id" not in pending_task:
                pending_task = None
        elif isinstance(pending_task, (list, tuple)) and len(pending_task) == 3:
            pending_task = {
                "bgi_cmd": pending_task[0],
                "open_id": pending_task[1],
                "uid": pending_task[2],
            }
        else:
            pending_task = None

        return {
            "uid": str(raw.get("uid", DEFAULT_UID)),
            "env_context": raw.get("env_context", ""),
            "env_context_at": raw.get("env_context_at", ""),
            "messages": raw.get("messages", []),
            "wallet": merged_wallet,
            "pending_task": pending_task,
            # 主动推送的目标（QQ / 飞书的会话标识）—— 漏了这行值就会被丢掉
            "last_target": str(raw.get("last_target") or ""),
        }
    except Exception as exc:
        backup = f"{HISTORY_FILE}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            shutil.copyfile(HISTORY_FILE, backup)
            print(f"⚠️ 记忆文件读不出来（{exc}），已留一份副本：{backup}；这次按空记忆继续。")
        except Exception:
            print(f"⚠️ 记忆文件读不出来（{exc}）；这次按空记忆继续。")
        return _default_store()


def save_chat_store(store):
    """保存持久化对话存储（**原子写**：先写临时文件再替换）。

    终端 / Studio / QQ 机器人是三个进程、共用这一份 `chat_context.json`；
    原来直接 `open(path, "w")` 先截断再 dump —— 写盘被打断就留下半截 JSON，
    下次加载变成"空记忆"并永久落盘。这里改成同目录临时文件 + os.replace。
    """
    HISTORY_FILE = config.HISTORY_FILE
    directory = os.path.dirname(os.path.abspath(HISTORY_FILE))
    os.makedirs(directory, exist_ok=True)

    handle = None
    temp_path = ""
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".chat_context-", suffix=".tmp", dir=directory)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        json.dump(store, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temp_path, HISTORY_FILE)
        temp_path = ""
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def trim_history(messages, max_messages=None):
    """限制历史长度，避免上下文无限增长。"""
    if max_messages is None:
        max_messages = config.MAX_HISTORY_MESSAGES
    if len(messages) <= max_messages:
        return messages
    return messages[-max_messages:]
