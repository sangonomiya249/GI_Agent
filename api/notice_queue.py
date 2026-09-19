"""跨进程的"待发通知"队列 —— 给 QQ 这种"只有机器人进程才发得出去"的通道用。

**为什么必须有它**（玩家反馈过两次："展柜数据本地思考完 QQ 收不到"、"启动推送收不到"）：

```text
CLI / Studio / 完成监视（另一个进程）
    ↓ 要推一条消息给玩家
feishu_api.send_feishu_msg("qq:c2c:xxx#MSG")
    ↓ channel_router.try_send() → 这个进程里**没有注册 QQ 发送器** → False
    ↓ owns() 也是 False（前缀没注册）
    ↓ 于是当成飞书目标去发 → 拿 "qq:..." 当飞书 open_id → 必然失败
结果：消息**凭空消失**，日志里连一句错都没有。
```

QQ 的发送能力只存在于 QQ 机器人进程里（要它的 client 与凭据）。所以：

```text
任何进程 → enqueue(target, text) → memory/agent_push_queue.json
                                        ↓
                       QQ 机器人进程定期 drain() → 真的发出去
                                        ↓ 发不出去（没额度）
                                  退回它自己的补发队列（玩家下次说话时补发）
```

**不是**复用 `memory/qq_pending_notices.json`：那个是机器人**自己**的补发队列
（`_save_pending` 整份覆盖写），两个进程都写会互相冲掉。这里单独一个文件。

写入用"锁文件 + 临时文件 + os.replace"：写它的进程可能有 CLI / Studio / 机器人三个，
直接 `open(w)` 截断再 dump 会留下半截 JSON（项目里为同类问题踩过坑）。
"""

import json
import os
import time

import config

QUEUE_FILE = "agent_push_queue.json"
LOCK_SUFFIX = ".lock"
# 队列上限：机器人一直没启动时别把文件堆成无限大
QUEUE_LIMIT = 200
LOCK_TIMEOUT = 3.0


def _path():
    return os.path.join(os.path.dirname(os.path.abspath(config.HISTORY_FILE)), QUEUE_FILE)


def _lock_path():
    return _path() + LOCK_SUFFIX


class _Lock:
    """极简跨进程锁（锁文件 + 超时）。抢不到就放弃并如实返回 False，不静默丢消息。"""

    def __init__(self, timeout=LOCK_TIMEOUT):
        self.timeout = timeout
        self.acquired = False

    def __enter__(self):
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                handle = os.open(_lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(handle)
                self.acquired = True
                return self
            except FileExistsError:
                time.sleep(0.05)
            except OSError:
                break
        return self

    def __exit__(self, *exc):
        if self.acquired:
            try:
                os.unlink(_lock_path())
            except OSError:
                pass
        return False


def _read():
    path = _path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:                       # noqa: BLE001 —— 坏文件按空队列处理，别把推送卡死
        return []
    items = data.get("items") if isinstance(data, dict) else data
    return [item for item in (items or []) if isinstance(item, dict)]


def _write(items):
    path = _path()
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    temp = f"{path}.tmp-{os.getpid()}"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump({"items": items}, handle, ensure_ascii=False, indent=1)
    os.replace(temp, path)


def enqueue(target, text):
    """把一条要推给玩家的消息放进队列。返回是否成功入队。"""
    target = str(target or "").strip()
    text = str(text or "").strip()
    if not target or not text:
        return False
    item = {"target": target, "text": text, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with _Lock() as lock:
        if not lock.acquired:
            print("⚠️ 推送队列被占用（另一个进程正在写），这条通知没能入队。")
            return False
        items = _read()
        items.append(item)
        del items[:-QUEUE_LIMIT]
        try:
            _write(items)
        except Exception as exc:            # noqa: BLE001
            print(f"⚠️ 写推送队列失败（{type(exc).__name__}: {exc}）")
            return False
    print(f"📮 已把这条推送放进跨进程队列（等 QQ 机器人取走）：{text.splitlines()[0][:40]}")
    return True


def drain(target=None, limit=20):
    """取走队列里的消息（默认全取）。**取走即删除**，避免重复发送。"""
    wanted = str(target or "").strip()
    with _Lock() as lock:
        if not lock.acquired:
            return []
        items = _read()
        if not items:
            return []
        taken, rest = [], []
        for item in items:
            if len(taken) < int(limit) and (not wanted or item.get("target") == wanted):
                taken.append(item)
            else:
                rest.append(item)
        try:
            _write(rest)
        except Exception as exc:            # noqa: BLE001
            print(f"⚠️ 更新推送队列失败（{type(exc).__name__}: {exc}）")
            return []
    return [(item.get("target") or "", item.get("text") or "") for item in taken]


def peek():
    """看一眼队列里有什么（不取走；诊断用）。"""
    return _read()


def clear():
    with _Lock() as lock:
        if lock.acquired:
            _write([])
    return True
