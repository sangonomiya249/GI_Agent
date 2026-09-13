"""远程通道子进程（QQ 机器人 / 飞书服务端）的进程桥：把"机器人的窗口"内嵌进 Studio。

思路和 `studio/agent_runner.py` 完全一致 —— **不重写机器人逻辑**，把 `qq_main.py` /
`feishu_main.py` 当子进程拉起来，stdout/stderr 按块读、拆成行存进环形缓冲，
界面就能像看 Agent 日志一样看机器人的输出（不再需要单独的控制台窗口）。

比 AgentRunner 简单的地方：通道进程**不需要 stdin**，也没有"等你确认"这个状态。

自动启动规则（玩家要求的逻辑）：

* 只有**填了对应的配置项**才会唤醒 —— QQ 要 `QQ_BOT_APPID` + `QQ_BOT_SECRET`，
  飞书要 `FEISHU_APP_ID` + `FEISHU_APP_SECRET`；没填就跳过，并在日志里写清原因。
* 开关：`AUTO_START_QQ_BOT`（默认 1）、`AUTO_START_FEISHU_BOT`（默认 0，见 README/文档的说明），
  在「配置」页可以直接勾。
* 触发时机：点「▶ 启动 Agent」时顺带拉起；点「■ 停止」或关窗口时一起停。
"""

import os
import subprocess
import sys
import threading
import time

from studio.agent_runner import (
    CREATE_NO_WINDOW,
    MAX_LINES,
    classify,
    resolve_console_python,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 通道清单：加新通道＝往这里加一条（label 给界面看，script 是要拉的入口）
CHANNEL_SPECS = (
    {
        "name": "qq",
        "label": "QQ 机器人",
        "script": "qq_main.py",
        "env_keys": ("QQ_BOT_APPID", "QQ_BOT_SECRET"),
        "env_labels": ("QQ_BOT_APPID", "QQ_BOT_SECRET"),
        "auto_field": "AUTO_START_QQ_BOT",
        "auto_default": "1",
        "hint": "在「配置 → QQ 机器人（可选）」里填 AppID 与 AppSecret",
        "notes": "群里 @机器人 或私聊它即可；审批按钮/菜单见「使用说明 → 三·五」",
    },
    {
        "name": "feishu",
        "label": "飞书服务端",
        "script": "feishu_main.py",
        "env_keys": ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
        "env_labels": ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
        "auto_field": "AUTO_START_FEISHU_BOT",
        "auto_default": "0",
        "hint": "在「配置 → 飞书（可选）」里填 App ID / App Secret",
        "notes": "它是 Webhook 服务（监听 5000 端口）：公网不可达（没有内网穿透）时收不到消息，"
                 "所以默认不随 Agent 自动启动。",
    },
)


def spec_for(name):
    for spec in CHANNEL_SPECS:
        if spec["name"] == name:
            return spec
    return None


def channel_is_configured(spec, values):
    """配置项都填了才算"已配置"。"""
    return all(str((values or {}).get(key) or "").strip() for key in spec["env_keys"])


def missing_keys(spec, values):
    return [key for key in spec["env_labels"] if not str((values or {}).get(key) or "").strip()]


def auto_start_enabled(spec, values):
    raw = str((values or {}).get(spec["auto_field"], "") or "").strip() or spec["auto_default"]
    return raw != "0"


class ChannelRunner:
    """一个通道子进程 + 它的输出缓冲（线程安全）。"""

    def __init__(self, spec, project_root=PROJECT_ROOT, max_lines=MAX_LINES):
        self.spec = dict(spec)
        self.name = self.spec["name"]
        self.label = self.spec["label"]
        self.project_root = str(project_root)
        self.max_lines = int(max_lines)
        self._lock = threading.Lock()
        self._lines = []
        self._seq = 0
        self._partial = ""
        self._state = "stopped"
        self._proc = None
        self._started_at = None
        self._reason = ""

    # ---------- 对外 ----------
    def command(self):
        if getattr(sys, "frozen", False):      # pragma: no cover - 打包场景
            return [sys.executable, "--run-channel", self.name]
        python = resolve_console_python(sys.executable or "python")
        return [python, "-u", os.path.join(self.project_root, self.spec["script"])]

    def start(self, reason="手动启动"):
        """启动通道子进程。

        ⚠️ 判活 + Popen + 记句柄在同一把锁里（和 AgentRunner 同样的竞态：
        以前连点两次会跑出两个机器人进程，只有一个能被停掉）。
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return {"ok": False, "error": f"{self.label} 已经在运行了"}

            command = self.command()
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    env=env,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as exc:
                self._append(f"❌ {self.label} 启动失败：{exc}\n", "error")
                return {"ok": False, "error": f"启动失败：{exc}"}

            self._proc = proc
            self._started_at = time.time()
            self._state = "running"
            self._reason = reason
            self._append(f"\n=== {self.label}：{reason}（{' '.join(command)}）===\n", "dim")

        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        return {"ok": True, "pid": proc.pid, "command": command, "name": self.name}

    def stop(self):
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            with self._lock:
                self._state = "stopped"
            return {"ok": True, "note": f"{self.label} 本来就没在运行"}

        pid = proc.pid
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=20,
                )
            else:  # pragma: no cover - 本机是 Windows
                proc.terminate()
        except Exception as exc:
            with self._lock:
                self._append(f"⚠️ 停止 {self.label} 时出错：{exc}\n", "warn")
        with self._lock:
            self._append(f"\n=== 已请求停止 {self.label}（PID {pid}）===\n", "warn")
        return {"ok": True, "pid": pid, "name": self.name}

    def note(self, text, level="dim"):
        """往这个通道的日志里写一行（比如"未配置，跳过自动启动"）。"""
        with self._lock:
            self._append(str(text) + "\n", level)

    def snapshot(self, since=0):
        with self._lock:
            since = int(since or 0)
            return {
                "ok": True,
                "name": self.name,
                "state": self._state,
                "pid": self._proc.pid if self._proc and self._proc.poll() is None else None,
                "seq": self._seq,
                "lines": [line for line in self._lines if line["i"] > since],
                "partial": self._partial,
                "started_at": self._started_at,
                "reason": self._reason,
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

    def pid(self):
        with self._lock:
            proc = self._proc
            return proc.pid if proc is not None and proc.poll() is None else None

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
                self._append(f"\n=== {self.label} 已退出（返回码 {code}）===\n", "dim")
                self._proc = None
                self._state = "stopped" if code == 0 else "exited"

    def _ingest(self, text):
        with self._lock:
            buffer = self._partial + str(text or "")
            parts = buffer.split("\n")
            self._partial = parts.pop() if parts else ""
            for part in parts:
                self._append(part + "\n", classify(part))

    def _append(self, text, level):
        if not text:
            return
        self._seq += 1
        self._lines.append({"i": self._seq, "text": text, "level": level, "at": time.time()})
        if len(self._lines) > self.max_lines:
            self._lines = self._lines[-self.max_lines :]


class ChannelManager:
    """管一组通道：状态汇总、手动启停、随 Agent 自动启动。"""

    def __init__(self, project_root=PROJECT_ROOT, env_loader=None, specs=CHANNEL_SPECS):
        self.project_root = str(project_root)
        self._env_loader = env_loader or self._default_env_loader
        self.runners = {spec["name"]: ChannelRunner(spec, project_root) for spec in specs}

    def _default_env_loader(self):
        import os as _os

        from skills import env_config

        return env_config.load_env(_os.path.join(self.project_root, ".env"))

    def values(self):
        try:
            return self._env_loader() or {}
        except Exception:
            return {}

    def view(self, name):
        """一条通道的界面用状态（配置齐没齐、自动启动开没开、进程在不在跑）。"""
        runner = self.runners.get(name)
        if runner is None:
            return None
        values = self.values()
        spec = runner.spec
        configured = channel_is_configured(spec, values)
        return {
            "name": name,
            "label": runner.label,
            "state": runner.state(),
            "pid": runner.pid(),
            "configured": configured,
            "missing": missing_keys(spec, values),
            "auto_start": auto_start_enabled(spec, values),
            "auto_field": spec["auto_field"],
            "hint": spec["hint"],
            "notes": spec["notes"],
        }

    def summary(self):
        return [self.view(name) for name in self.runners]

    def snapshot(self, since=None):
        """所有通道的增量日志：`since` 形如 {"qq": 12, "feishu": 0}。"""
        since = since or {}
        return {
            "ok": True,
            "channels": [
                {**self.view(name), **runner.snapshot(since.get(name, 0))}
                for name, runner in self.runners.items()
            ],
        }

    def start(self, name, reason="手动启动"):
        runner = self.runners.get(name)
        if runner is None:
            return {"ok": False, "error": f"没有这个通道：{name}"}
        view = self.view(name)
        if not view["configured"]:
            message = f"没配置 {runner.label}（缺 {'、'.join(view['missing'])}）：{runner.spec['hint']}"
            runner.note(f"⚠️ {message}", "warn")
            print(f"⚠️ {message}")
            return {"ok": False, "error": message}
        result = runner.start(reason=reason)
        if result.get("ok"):
            print(f"🚀 已启动 {runner.label}（PID {result.get('pid')}，{reason}）")
        return result

    def stop(self, name):
        runner = self.runners.get(name)
        if runner is None:
            return {"ok": False, "error": f"没有这个通道：{name}"}
        return runner.stop()

    def clear(self, name):
        runner = self.runners.get(name)
        if runner is None:
            return {"ok": False, "error": f"没有这个通道：{name}"}
        return runner.clear()

    def stop_all(self):
        results = []
        for name in self.runners:
            results.append({"name": name, **self.stop(name)})
        return results

    def start_automatic(self):
        """点「启动 Agent」时调用：按规则唤醒通道，返回给界面显示的说明。"""
        notes = []
        for name, runner in self.runners.items():
            view = self.view(name)
            if not view["configured"]:
                note = f"{runner.label}：没配置（缺 {'、'.join(view['missing'])}），跳过自动启动"
                runner.note(f"ℹ️ {note}", "dim")
                notes.append(note)
                continue
            if not view["auto_start"]:
                note = f"{runner.label}：自动启动已关闭（{view['auto_field']}=0），跳过"
                runner.note(f"ℹ️ {note}", "dim")
                notes.append(note)
                continue
            if view["state"] == "running":
                notes.append(f"{runner.label}：已经在运行（PID {view['pid']}）")
                continue
            result = self.start(name, reason="随 Agent 自动启动")
            if result.get("ok"):
                notes.append(f"{runner.label}：已随 Agent 启动（PID {result.get('pid')}）")
            else:
                notes.append(f"{runner.label}：启动失败 —— {result.get('error')}")
        return notes
