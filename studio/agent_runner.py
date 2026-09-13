"""Agent 子进程桥：Studio（网页控制台）与任何前端共用的"启动 / 停止 / 喂输入"。

设计：**不重写 Agent**。把 `main.py` 当子进程拉起来，stdout/stderr 按块读、拆成行存进
环形缓冲；输入直接写它的 stdin。前端只跟这个对象打交道。

为什么要按块读：CLI 的 `👤 旅行者 (你): ` 和 `🛑 [系统拦截] 请确认是否执行上述计划？`
都不带换行，按行读会一直卡在缓冲区里，界面看起来像"卡死了"。

状态机：stopped → running → (等你确认) → exited/stopped。
`waiting` 由日志里的审批提示触发 —— 前端据此把"批准/仅写配置"按钮点亮。
"""

import os
import subprocess
import sys
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MAX_LINES = 4000

# 见到这些字样说明 CLI 在等玩家拍板
PENDING_MARKERS = (
    "请确认是否执行上述计划",
    "请决定:",
    "输入要恢复的事务 ID",
    "确认恢复请输入",
)

# 审批摘要的行首（和 route_group.plan_summary_lines 保持一致）
PLAN_PREFIXES = ("⚔️", "🌿", "👹", "🗺️", "⛏️", "🍳", "🎬")


def resolve_console_python(python):
    """pythonw.exe 拉起的界面里，子进程换成同目录的 python.exe 更稳（stdio 走管道）。"""
    python = str(python or "python")
    if python.lower().endswith("pythonw.exe"):
        console_python = os.path.join(os.path.dirname(python), "python.exe")
        if os.path.isfile(console_python):
            return console_python
    return python


def child_command(project_root=PROJECT_ROOT):
    """启动 Agent 的命令行。

    - 源码运行：`<venv python> -u main.py`
    - 打包成 exe：`<exe> --run-cli`（项目文件已经打进 exe，用它自己当子进程）
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-cli"]
    python = resolve_console_python(sys.executable or "python")
    return [python, "-u", os.path.join(project_root, "main.py")]


def classify(text):
    """日志分级（前端按级别上色）。"""
    if "❌" in text or "Traceback" in text or "发生错误" in text:
        return "error"
    if "⚠️" in text:
        return "warn"
    if "🤖" in text or "Agent:" in text:
        return "agent"
    if "👤" in text:
        return "user"
    if "🛑" in text or "请确认是否执行" in text:
        return "prompt"
    if "🔄" in text or "🧭" in text or "🧩" in text or "🧾" in text:
        return "action"
    return "info"


class AgentRunner:
    """管理 Agent 子进程与它的输出缓冲（线程安全）。"""

    def __init__(self, project_root=PROJECT_ROOT, max_lines=MAX_LINES):
        self.project_root = str(project_root)
        self.max_lines = int(max_lines)
        self._lock = threading.Lock()
        self._lines = []
        self._seq = 0
        self._partial = ""
        self._state = "stopped"
        self._proc = None
        self._started_at = None
        self._reader = None

    # ---------- 对外接口 ----------
    def start(self):
        """启动子进程。

        ⚠️ "判活 + Popen + 记下句柄"必须在**同一把锁**里完成：以前先检查再释放锁才 Popen，
        中间那段窗口里连点两次「启动」会跑出两个 Agent（只会记住后一个，前一个连 stop / 退出
        Studio 都杀不到，变成孤儿继续驱动 BetterGI）。
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return {"ok": False, "error": "Agent 已经在运行了"}

            command = child_command(self.project_root)
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as exc:
                return {"ok": False, "error": f"启动失败：{exc}"}

            self._proc = proc
            self._started_at = time.time()
            self._state = "running"
            self._append(f"\n=== 启动：{' '.join(command)} ===\n", "dim")

        self._reader = threading.Thread(target=self._read, args=(proc,), daemon=True)
        self._reader.start()
        return {"ok": True, "pid": proc.pid, "command": command}

    def stop(self):
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            self._set_state("stopped")
            return {"ok": True, "note": "本来就没在运行"}

        pid = proc.pid
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=20,
                )
            else:
                proc.terminate()
        except Exception as exc:
            with self._lock:
                self._append(f"⚠️ 停止进程时出错：{exc}\n", "warn")
        with self._lock:
            self._append(f"\n=== 已请求停止（PID {pid}）===\n", "warn")
        return {"ok": True, "pid": pid}

    def send(self, text):
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "内容为空"}
        with self._lock:
            proc = self._proc
            alive = proc is not None and proc.poll() is None
        if not alive:
            return {"ok": False, "error": "Agent 没在运行，先点「启动」"}
        try:
            proc.stdin.write((text + "\n").encode("utf-8"))
            proc.stdin.flush()
        except Exception as exc:
            return {"ok": False, "error": f"写入子进程失败：{exc}"}
        with self._lock:
            self._append(f"\n👤 我：{text}\n", "user")
            if self._state == "waiting":
                self._state = "running"
        return {"ok": True}

    def snapshot(self, since=0):
        """增量日志：返回序号 > since 的完整行 + 当前状态 + 未结束的那半行。

        `partial` 单独给前端：CLI 的提示语（`🛑 …请确认…`）不带换行，
        它不是"一行日志"，而是"正在闪烁的输入提示"。
        """
        with self._lock:
            since = int(since or 0)
            fresh = [line for line in self._lines if line["i"] > since]
            return {
                "ok": True,
                "state": self._state,
                "pid": self._proc.pid if self._proc and self._proc.poll() is None else None,
                "seq": self._seq,
                "lines": fresh,
                "partial": self._partial,
                "plan": self.last_plan_locked(),
                "started_at": self._started_at,
            }

    def clear(self):
        with self._lock:
            self._lines = []
            self._seq = 0
            self._partial = ""
        return {"ok": True, "seq": 0}

    def state(self):
        with self._lock:
            return self._state

    # ---------- 内部 ----------
    def _read(self, proc):
        stream = proc.stdout
        try:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                self._ingest(chunk.decode("utf-8", errors="replace"))
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass
            code = proc.poll()
            with self._lock:
                if self._partial.strip():
                    self._append(self._partial, classify(self._partial))
                    self._partial = ""
                self._append(f"\n=== Agent 已退出（返回码 {code}）===\n", "dim")
                self._proc = None
                self._state = "stopped" if code == 0 else "exited"

    def _ingest(self, text):
        """把一块输出拆成"完整行" + "残留尾巴"。"""
        with self._lock:
            buffer = self._partial + str(text or "")
            parts = buffer.split("\n")
            self._partial = parts.pop() if parts else ""

            for part in parts:
                self._append(part + "\n", classify(part))

            # 半行里也可能已经有审批提示（提示语不带换行）
            if self._state != "waiting" and any(
                marker in self._partial for marker in PENDING_MARKERS
            ):
                self._state = "waiting"

    def _append(self, text, level):
        """写入缓冲（调用方需持有锁）。"""
        if not text:
            return
        self._seq += 1
        self._lines.append(
            {
                "i": self._seq,
                "text": text,
                "level": level,
                "at": time.time(),
            }
        )
        if len(self._lines) > self.max_lines:
            self._lines = self._lines[-self.max_lines :]

        if self._state != "waiting" and any(marker in text for marker in PENDING_MARKERS):
            self._state = "waiting"

    def _set_state(self, state):
        with self._lock:
            self._state = state

    def last_plan_locked(self):
        """从日志里抽出最近一次「本轮将执行」摘要（审批屏那几行）。"""
        starts = None
        for index in range(len(self._lines) - 1, -1, -1):
            text = str(self._lines[index]["text"]).strip()
            if text.startswith("⚔️"):
                starts = index
                break
        if starts is None:
            return None

        lines = []
        for line in self._lines[starts : starts + 8]:
            text = str(line["text"]).strip()
            if text.startswith(PLAN_PREFIXES):
                lines.append(text)
            elif lines:
                break
        return {"lines": lines} if lines else None

    def last_plan(self):
        with self._lock:
            return self.last_plan_locked()
