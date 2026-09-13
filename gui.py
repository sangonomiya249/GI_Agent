"""GI Agent 控制台（tkinter 桌面版）。

用法：
    venv\\Scripts\\pythonw.exe gui.py        # 双击「启动GI-Agent控制台.bat」等价
    python gui.py --run-cli                  # 内部用：被打包成 exe 后，用自己当子进程跑 CLI

设计取舍：**不重写 Agent 逻辑**。界面把 `main.py` 当子进程拉起来，
stdout/stderr 实时回显，输入框把 `y`/`t`/`exit`/`refresh`/自然语言原样喂给它的 stdin。
好处是 CLI 那套拦截层、审批流、事务回滚一行都不用改，界面挂了也不影响 CLI 单独跑。

四个页签：
  运行     —— 启动/停止、实时日志、审批快捷键、把输入发给 Agent
  配置     —— 图形化编辑 .env（保留注释、保存前自动备份）
  维护工具 —— 刷新展柜、环境体检、回滚 BetterGI 配置、打开各种目录
  帮助     —— 命令、审批流程、常见故障速查
"""

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
STATE_PATH = os.path.join(PROJECT_ROOT, ".gui_state.json")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

STATUS_STYLES = {
    "stopped": ("● 未运行", "#8a8a8a"),
    "running": ("● 运行中", "#1a9c4b"),
    "waiting": ("● 等你确认", "#e0a800"),
    "exited": ("● 已退出", "#b02a2a"),
}

LOG_TAGS = {
    "info": {"foreground": "#1f2328"},
    "dim": {"foreground": "#6a737d"},
    "warn": {"foreground": "#9a6700"},
    "error": {"foreground": "#b02a2a"},
    "agent": {"foreground": "#0b5cad"},
    "user": {"foreground": "#6f42c1"},
    "prompt": {"foreground": "#0b5cad", "background": "#fff3cd"},
}


# ==========================================
# 🌟 打包成 exe 后的 CLI 模式：用自己当子进程
# ==========================================
class _FlushStream:
    """每次写入立刻 flush —— CLI 的 `input()` 提示不带换行，不 flush 界面就看不到。"""

    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        written = self._stream.write(data)
        try:
            self._stream.flush()
        except Exception:
            pass
        return written

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def run_cli():
    """`--run-cli`：在本进程跑 main.py 的交互循环（供打包后的 exe 当子进程用）。"""
    sys.stdout = _FlushStream(sys.stdout)
    sys.stderr = _FlushStream(sys.stderr)
    os.chdir(PROJECT_ROOT)

    try:
        import main as cli
    except Exception as exc:
        print(f"❌ 无法加载 main.py：{exc}")
        return 1

    try:
        cli.main()
    except KeyboardInterrupt:
        print("\n已中断。")
    except EOFError:
        print("\n输入流已关闭，退出。")
    except Exception as exc:
        print(f"❌ CLI 异常退出：{exc}")
    return 0


def child_command():
    """启动 Agent 子进程的命令行。

    - 源码运行：`<venv python> -u main.py`
    - exe 运行：`<exe> --run-cli`（项目文件已打进 exe）
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-cli"]

    python = sys.executable or "python"
    # 控制台自己是被 pythonw 拉起来的（无黑窗），子进程换成同目录的 python.exe 更稳：
    # 它带完整控制台运行库，stdio 走管道 + CREATE_NO_WINDOW 一样不会弹窗。
    if python.lower().endswith("pythonw.exe"):
        console_python = os.path.join(os.path.dirname(python), "python.exe")
        if os.path.isfile(console_python):
            python = console_python
    return [python, "-u", os.path.join(PROJECT_ROOT, "main.py")]


# ==========================================
# 🌟 主窗口
# ==========================================
class ConsoleApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GI Agent 控制台")
        self.geometry(self._load_geometry())
        self.minsize(980, 640)

        self.proc = None
        self.output_queue = queue.Queue()
        self.tool_queue = queue.Queue()
        self._tail = ""
        self._state = "stopped"
        self._log_lines = 0
        self._config_vars = {}
        self._config_widgets = {}
        self._raw_text = None
        self._current_group = None

        self._build_menu()
        self._build_header()
        # 状态栏先建：配置页在构建时就会刷新，里面要用到 hint_label
        self._build_statusbar()
        self._build_notebook()

        for tag, options in LOG_TAGS.items():
            self.log_text.tag_configure(tag, **options)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._alive = True
        self._pump_job = self.after(80, self._pump)
        self._append_log("欢迎使用 GI Agent 控制台。点「▶ 启动」开始；审批时用下面那排快捷键。\n", "dim")

    # ---------- 界面搭建 ----------
    def _load_geometry(self):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as handle:
                geometry = json.load(handle).get("geometry")
            if geometry:
                return geometry
        except Exception:
            pass
        return "1040x720"

    def _save_geometry(self):
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as handle:
                json.dump({"geometry": self.winfo_geometry()}, handle)
        except Exception:
            pass

    def _build_menu(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="重新载入 .env", command=self._load_config_form)
        file_menu.add_command(label="打开项目目录", command=lambda: self._open_path(PROJECT_ROOT))
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="刷新展柜上下文", command=self._refresh_env_context)
        tools_menu.add_command(label="环境体检", command=self._run_health_check)
        tools_menu.add_command(label="回滚 BetterGI 配置…", command=self._rollback_dialog)
        tools_menu.add_separator()
        tools_menu.add_command(
            label="打开 BetterGI 日志目录", command=lambda: self._open_path(self._bgi_path("log"))
        )
        tools_menu.add_command(
            label="打开备份目录", command=lambda: self._open_path(self._bgi_path("User/GI_AgentBackups"))
        )
        menubar.add_cascade(label="工具", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="关于", command=self._about)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.config(menu=menubar)

    def _build_header(self):
        header = ttk.Frame(self, padding=(12, 8))
        header.pack(fill="x")

        self.start_button = ttk.Button(header, text="▶ 启动", command=self._start_agent, width=10)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(header, text="■ 停止", command=self._stop_agent, width=10)
        self.stop_button.pack(side="left", padx=(6, 14))
        self.stop_button.state(["disabled"])

        self.status_label = tk.Label(header, text=STATUS_STYLES["stopped"][0],
                                     foreground=STATUS_STYLES["stopped"][1], font=("Microsoft YaHei UI", 10, "bold"))
        self.status_label.pack(side="left")

        self.pid_label = ttk.Label(header, text="", foreground="#6a737d")
        self.pid_label.pack(side="left", padx=(10, 0))

        info = ttk.Frame(header)
        info.pack(side="right")
        self.env_summary = ttk.Label(info, text=self._env_summary(), foreground="#444")
        self.env_summary.pack(side="right")

    def _build_notebook(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        self._build_run_tab()
        self._build_config_tab()
        self._build_tools_tab()
        self._build_help_tab()

    def _build_run_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="运行")

        self.banner = tk.Label(
            frame,
            text="",
            background="#f3f4f6",
            foreground="#7a5b00",
            anchor="w",
            padx=10,
            pady=6,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.banner.pack(fill="x", pady=(4, 0))

        tools = ttk.Frame(frame, padding=(0, 6))
        tools.pack(fill="x")
        ttk.Label(tools, text="快捷指令：").pack(side="left")
        for label, text, width in (
            ("y 批准执行", "y", 10),
            ("t 仅写配置", "t", 11),
            ("refresh 刷新展柜", "refresh", 15),
            ("history 查看历史", "history", 15),
            ("clear 清空记忆", "clear", 13),
            ("exit 退出", "exit", 9),
        ):
            ttk.Button(tools, text=label, width=width,
                       command=lambda value=text: self._send_input(value)).pack(side="left", padx=2)
        ttk.Button(tools, text="清屏", width=6, command=self._clear_log).pack(side="right")
        ttk.Button(tools, text="回滚配置…", width=11, command=self._rollback_dialog).pack(side="right", padx=4)

        self.log_text = ScrolledText(frame, wrap="word", height=20, font=("Consolas", 9),
                                     background="#fbfbfd", state="disabled")
        self.log_text.pack(fill="both", expand=True)

        entry_row = ttk.Frame(frame, padding=(0, 8))
        entry_row.pack(fill="x")
        ttk.Label(entry_row, text="发给 Agent：").pack(side="left")
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(entry_row, textvariable=self.input_var)
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.input_entry.bind("<Return>", lambda _event: self._send_from_entry())
        ttk.Button(entry_row, text="发送", command=self._send_from_entry, width=8).pack(side="left")

    def _build_config_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="配置 (.env)")

        top = ttk.Frame(frame, padding=(0, 6))
        top.pack(fill="x")
        ttk.Label(top, text=f"配置文件：{ENV_PATH}", foreground="#444").pack(side="left")
        ttk.Button(top, text="重新载入", command=self._load_config_form).pack(side="right")
        ttk.Button(top, text="保存", command=self._save_config_form).pack(side="right", padx=4)
        self.show_secret = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="显示密钥", variable=self.show_secret,
                        command=self._toggle_secret_visibility).pack(side="right", padx=8)

        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.group_list = tk.Listbox(left, width=18, exportselection=False,
                                     font=("Microsoft YaHei UI", 10))
        self.group_list.pack(fill="y", expand=True)
        self.group_list.bind("<<ListboxSelect>>", lambda _event: self._show_group())

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.config_canvas = tk.Canvas(right, highlightthickness=0)
        scrollbar = ttk.Scrollbar(right, orient="vertical", command=self.config_canvas.yview)
        self.config_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.config_canvas.pack(side="left", fill="both", expand=True)
        self.config_frame = ttk.Frame(self.config_canvas)
        self.config_canvas.create_window((0, 0), window=self.config_frame, anchor="nw")
        self.config_frame.bind(
            "<Configure>",
            lambda _event: self.config_canvas.configure(scrollregion=self.config_canvas.bbox("all")),
        )

        note = ttk.Label(
            frame,
            text="提示：改完 .env 需要「重新启动」Agent 才生效（config.py 在 import 时就读了环境变量）。"
                 "保存前会自动备份成 .env.bak-<时间戳>。",
            foreground="#6a737d",
            wraplength=900,
            justify="left",
        )
        note.pack(fill="x", pady=(6, 0))

        self._load_config_form()

    def _build_tools_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="维护工具")

        buttons = ttk.Frame(frame, padding=(0, 6))
        buttons.pack(fill="x")
        for label, command in (
            ("🔄 刷新展柜上下文", self._refresh_env_context),
            ("🩺 环境体检", self._run_health_check),
            ("↩️ 回滚 BetterGI 配置", self._rollback_dialog),
            ("📂 打开项目目录", lambda: self._open_path(PROJECT_ROOT)),
            ("📂 BetterGI 日志目录", lambda: self._open_path(self._bgi_path("log"))),
            ("📂 备份目录", lambda: self._open_path(self._bgi_path("User/GI_AgentBackups"))),
            ("📂 一条龙配置目录", lambda: self._open_path(self._bgi_path("User/OneDragon"))),
            ("📂 脚本组目录", lambda: self._open_path(self._bgi_path("User/ScriptGroup"))),
        ):
            ttk.Button(buttons, text=label, command=command).pack(side="left", padx=3, pady=2)

        ttk.Label(
            frame,
            text="体检会检查：LLM 配置、BetterGI 目录、当前生效的一条龙配置、"
                 "各脚本组路线数与战斗策略、AutoPathing 类目、展柜缓存、备份事务。只读，不改任何文件。",
            foreground="#6a737d",
            wraplength=980,
            justify="left",
        ).pack(fill="x", pady=(0, 4))

        self.tool_text = ScrolledText(frame, wrap="word", height=20, font=("Consolas", 9),
                                      background="#fbfbfd", state="disabled")
        self.tool_text.pack(fill="both", expand=True)

    def _build_help_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="帮助")
        text = ScrolledText(frame, wrap="word", font=("Microsoft YaHei UI", 10), background="#fbfbfd")
        text.pack(fill="both", expand=True)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")

    def _build_statusbar(self):
        bar = ttk.Frame(self, padding=(12, 4))
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, text=f"项目目录：{PROJECT_ROOT}", foreground="#6a737d").pack(side="left")
        self.hint_label = ttk.Label(bar, text="", foreground="#6a737d")
        self.hint_label.pack(side="right")

    # ---------- 进程控制 ----------
    def _start_agent(self):
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("已经在运行", "Agent 子进程还在跑，先点「■ 停止」。")
            return

        command = child_command()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        try:
            self.proc = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:
            messagebox.showerror("启动失败", f"拉不起子进程：{exc}")
            return

        self._append_log(f"\n=== 启动：{' '.join(command)} ===\n", "dim")
        self._set_state("running")
        self.pid_label.configure(text=f"PID {self.proc.pid}")
        self.start_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        threading.Thread(target=self._read_output, args=(self.proc,), daemon=True).start()

    def _read_output(self, proc):
        """读子进程输出（按块读，`input()` 提示不带换行也能实时看到）。"""
        stream = proc.stdout
        try:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                self.output_queue.put(chunk.decode("utf-8", errors="replace"))
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _stop_agent(self):
        if not self.proc or self.proc.poll() is not None:
            self._set_state("stopped")
            return
        if not messagebox.askyesno(
            "停止 Agent",
            "确定要停止 Agent 子进程吗？\n\n"
            "· 只停 Agent，不会关掉已经在跑的 BetterGI（游戏里的自动化会继续）。\n"
            "· 已经提交的配置修改不会回滚，可用「回滚 BetterGI 配置」还原。",
        ):
            return

        pid = self.proc.pid
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=20)
            else:
                self.proc.terminate()
        except Exception as exc:
            self._append_log(f"⚠️ 停止进程时出错：{exc}\n", "warn")
        self._append_log(f"\n=== 已请求停止（PID {pid}）===\n", "warn")

    def _send_input(self, text):
        text = str(text or "").strip()
        if not text:
            return
        if not self.proc or self.proc.poll() is not None:
            messagebox.showinfo("还没启动", "先点「▶ 启动」把 Agent 拉起来。")
            return
        try:
            self.proc.stdin.write((text + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except Exception as exc:
            messagebox.showerror("发送失败", f"写不进子进程 stdin：{exc}")
            return
        self._append_log(f"\n👤 我：{text}\n", "user")
        self._set_state("running")

    def _send_from_entry(self):
        text = self.input_var.get()
        self.input_var.set("")
        self._send_input(text)

    # ---------- 输出泵 ----------
    def _pump(self):
        if not getattr(self, "_alive", False):
            return
        self._drain(self.output_queue, self._append_log)
        self._drain(self.tool_queue, self._append_tool)

        if self.proc is not None and self.proc.poll() is not None:
            code = self.proc.returncode
            self._append_log(f"\n=== Agent 已退出（返回码 {code}）===\n", "dim")
            self.proc = None
            self.pid_label.configure(text="")
            self.start_button.state(["!disabled"])
            self.stop_button.state(["disabled"])
            self._set_state("stopped" if code == 0 else "exited")

        self._pump_job = self.after(80, self._pump)

    def destroy(self):
        """停掉定时器再销毁，否则 Tk 会刷一串 `invalid command name ..._pump`。"""
        self._alive = False
        job = getattr(self, "_pump_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
            self._pump_job = None
        super().destroy()

    def _drain(self, source, writer):
        while True:
            try:
                item = source.get_nowait()
            except queue.Empty:
                return
            writer(item)

    def _append_log(self, text, tag=None):
        if not text:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text, tag or self._classify(text))
        self.log_text.configure(state="disabled")
        self.log_text.see("end")
        self._log_lines += text.count("\n")
        self._track_prompt(text)

    def _append_tool(self, text):
        self.tool_text.configure(state="normal")
        self.tool_text.insert("end", text)
        self.tool_text.configure(state="disabled")
        self.tool_text.see("end")

    @staticmethod
    def _classify(text):
        if "❌" in text or "Traceback" in text or "发生错误" in text:
            return "error"
        if "⚠️" in text:
            return "warn"
        if "🤖" in text or "Agent:" in text:
            return "agent"
        if "🛑" in text or "请确认是否执行" in text:
            return "prompt"
        return "info"

    def _track_prompt(self, text):
        """检测「等你拍板」的提示，把状态灯点亮。"""
        self._tail = (self._tail + text)[-400:]
        markers = ("请确认是否执行上述计划", "请决定:", "输入要恢复的事务 ID", "确认恢复请输入")
        if any(marker in self._tail for marker in markers):
            if self._state != "waiting":
                self._set_state("waiting")
                self.bell()
                self.lift()

    def _set_state(self, state):
        self._state = state
        label, colour = STATUS_STYLES.get(state, STATUS_STYLES["stopped"])
        self.status_label.configure(text=label, foreground=colour)
        if state == "waiting":
            self.banner.configure(
                text="🛑 Agent 在等你拍板：y = 批准并启动 BetterGI ｜ t = 只写配置（推荐先看 diff）｜ exit = 退出",
                background="#fff3cd",
            )
            self.notebook.select(0)
        else:
            self.banner.configure(text="", background="#f3f4f6")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._tail = ""

    # ---------- 配置页 ----------
    def _load_config_form(self):
        from skills import env_config

        self._env_values = env_config.load_env(ENV_PATH)
        for widget in self.config_frame.winfo_children():
            widget.destroy()
        self._config_vars.clear()
        self._config_widgets.clear()

        self.group_list.delete(0, "end")
        for group in env_config.FIELD_GROUPS:
            self.group_list.insert("end", group.title)
        if env_config.FIELD_GROUPS:
            self.group_list.selection_set(0)
            self._show_group()

        self.env_summary.configure(text=self._env_summary())
        self.hint_label.configure(text=f"配置来源：{os.path.basename(ENV_PATH)}")

    def _show_group(self):
        from skills import env_config

        selection = self.group_list.curselection()
        if not selection:
            selection = (0,)
        index = selection[0]
        group = env_config.FIELD_GROUPS[index]

        # 先把上一页的编辑结果收进内存，切页不丢改动（点「保存」时一起写盘）
        self._collect_visible_values()
        self._current_group = group

        for widget in self.config_frame.winfo_children():
            widget.destroy()
        self._config_vars.clear()
        self._config_widgets.clear()

        ttk.Label(self.config_frame, text=group.title,
                  font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(4, 8))

        for row, field in enumerate(group.fields, start=1):
            value = self._env_values.get(field.key, field.default)
            variable = tk.StringVar(value=value)
            self._config_vars[field.key] = variable

            ttk.Label(self.config_frame, text=field.label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Label(self.config_frame, text=field.key, foreground="#6a737d",
                      font=("Consolas", 8)).grid(row=row, column=1, sticky="w", padx=(0, 10))

            if field.kind == "bool":
                widget = ttk.Combobox(self.config_frame, textvariable=variable, values=("1", "0"), width=6)
            elif field.kind == "choice":
                widget = ttk.Combobox(self.config_frame, textvariable=variable, values=field.choices, width=28)
            elif field.kind == "secret":
                widget = ttk.Entry(self.config_frame, textvariable=variable, width=46,
                                   show="" if self.show_secret.get() else "•")
            else:
                widget = ttk.Entry(self.config_frame, textvariable=variable, width=46)
            widget.grid(row=row, column=2, sticky="we", pady=3)
            self._config_widgets[field.key] = widget

            if field.kind == "path":
                ttk.Button(self.config_frame, text="…", width=3,
                           command=lambda var=variable: self._pick_directory(var)).grid(row=row, column=3, padx=(4, 0))

            if field.help:
                ttk.Label(self.config_frame, text=field.help, foreground="#6a737d",
                          wraplength=520, justify="left").grid(row=row, column=4, sticky="w", padx=(10, 0))
            if field.choices:
                ttk.Label(self.config_frame, text="可选：" + "、".join(field.choices),
                          foreground="#8a8a8a").grid(row=row, column=5, sticky="w", padx=(10, 0))

        self.config_frame.columnconfigure(2, weight=1)
        self.config_canvas.yview_moveto(0)

        ttk.Button(self.config_frame, text="保存本页并备份 .env", command=self._save_config_form) \
            .grid(row=len(group.fields) + 1, column=0, columnspan=3, sticky="w", pady=(14, 6))

    def _toggle_secret_visibility(self):
        from skills import env_config

        for key, widget in self._config_widgets.items():
            field = env_config.ALL_FIELDS.get(key)
            if field and field.kind == "secret" and isinstance(widget, ttk.Entry):
                widget.configure(show="" if self.show_secret.get() else "•")

    def _pick_directory(self, variable):
        current = variable.get()
        initial = current if os.path.isdir(current) else PROJECT_ROOT
        chosen = filedialog.askdirectory(initialdir=initial, title="选择目录")
        if chosen:
            variable.set(os.path.normpath(chosen))

    def _collect_visible_values(self):
        """把当前页面上各输入框的值收进 self._env_values。"""
        for key, variable in self._config_vars.items():
            self._env_values[key] = variable.get()

    def _save_config_form(self):
        from skills import env_config

        self._collect_visible_values()
        # 只写「这个版本认识的键」，避免把用户自己加的自定义键重排掉
        values = {key: self._env_values.get(key, "") for key in env_config.ALL_FIELDS}
        warnings = env_config.validate_values(values)
        if warnings:
            proceed = messagebox.askyesno(
                "有告警，仍然保存？",
                "\n".join(f"· {warning}" for warning in warnings) + "\n\n（这些只是提醒，不影响保存）",
            )
            if not proceed:
                return

        try:
            backup = env_config.save_env(ENV_PATH, values)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return

        self._env_values.update(values)
        self.env_summary.configure(text=self._env_summary())
        self._append_tool(f"\n✅ 已保存 {len(values)} 项到 {ENV_PATH}\n")
        if backup:
            self._append_tool(f"   备份：{backup}\n")
        messagebox.showinfo(
            "保存成功",
            "配置已写入 .env，并已备份。\n\n"
            "⚠️ 需要「■ 停止」再「▶ 启动」才会生效（config.py 在 import 时就读取了环境变量）。",
        )

    # ---------- 维护工具 ----------
    def _run_in_thread(self, name, worker):
        self._append_tool(f"\n===== {name} =====\n")

        def runner():
            try:
                worker()
            except Exception as exc:
                self.tool_queue.put(f"❌ {name} 失败：{exc}\n")

        threading.Thread(target=runner, daemon=True).start()

    def _refresh_env_context(self):
        def worker():
            from brain import memory_manager
            from skills.env_reader import refresh_store_env_context
            import config

            store = memory_manager.load_chat_store()
            uid = store.get("uid", config.DEFAULT_UID)
            if not uid:
                self.tool_queue.put("⚠️ 没配 DEFAULT_UID，展柜抓不了。先在「配置」页填 UID。\n")
                return
            self.tool_queue.put(f"正在抓取 UID {uid} 的展柜…\n")
            _refreshed, notice = refresh_store_env_context(store, uid, force=True)
            memory_manager.save_chat_store(store)
            self.tool_queue.put(f"{notice}\n")

        self._run_in_thread("刷新展柜上下文", worker)

    def _run_health_check(self):
        def worker():
            from skills import health_check

            rows = health_check.run_health_check(ENV_PATH)
            self.tool_queue.put(health_check.format_report(rows) + "\n")
            errors = sum(1 for level, _text in rows if level == "error")
            warns = sum(1 for level, _text in rows if level == "warn")
            self.tool_queue.put(f"\n小结：{errors} 个错误，{warns} 个告警。\n")

        self._run_in_thread("环境体检", worker)

    def _rollback_dialog(self):
        from skills.config_recovery import list_transactions, load_transaction, restore_transaction
        import config

        try:
            transactions = list_transactions(config.BGI_BACKUP_DIR)
        except Exception as exc:
            messagebox.showerror("读取失败", f"读不到事务记录：{exc}")
            return
        if not transactions:
            messagebox.showinfo("没有可回滚的事务", f"备份目录：{config.BGI_BACKUP_DIR}")
            return

        dialog = tk.Toplevel(self)
        dialog.title("回滚 BetterGI 配置")
        dialog.geometry("720x380")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text=f"备份目录：{config.BGI_BACKUP_DIR}", foreground="#444",
                  wraplength=680, justify="left", padding=(10, 8)).pack(fill="x")
        ttk.Label(dialog, text="选择一条事务，把它执行**之前**的 BetterGI 配置恢复回来"
                              "（只还原文件，不撤销游戏内已发生的操作）。",
                  foreground="#6a737d", wraplength=680, justify="left", padding=(10, 0)).pack(fill="x")

        listbox = tk.Listbox(dialog, font=("Consolas", 9))
        listbox.pack(fill="both", expand=True, padx=10, pady=8)
        for item in transactions:
            listbox.insert(
                "end",
                f"{item.get('id')} | {item.get('status')} | 文件 {item.get('updates')} 个 | {item.get('created_at')}",
            )
        listbox.selection_set(0)

        def do_restore():
            selection = listbox.curselection()
            if not selection:
                return
            transaction = transactions[selection[0]]
            transaction_id = str(transaction.get("id"))
            try:
                _directory, manifest = load_transaction(config.BGI_BACKUP_DIR, transaction_id)
            except Exception as exc:
                messagebox.showerror("读取失败", str(exc))
                return
            files = "\n".join(f"· {u.get('label')}: {u.get('path')}" for u in manifest.get("updates", []))
            if not messagebox.askyesno(
                "确认回滚",
                f"将 {transaction_id} 执行前的配置恢复回来：\n\n{files}\n\n"
                "建议先关掉 BetterGI。确定继续？",
            ):
                return
            try:
                result = restore_transaction(config.BGI_BACKUP_DIR, transaction_id)
            except Exception as exc:
                messagebox.showerror("回滚失败", str(exc))
                return
            names = "、".join(path.name for path in result.restored_files)
            self._append_tool(f"\n↩️ 已回滚 {transaction_id}：恢复 {names}\n   本次快照：{result.backup_dir}\n")
            dialog.destroy()
            messagebox.showinfo("回滚完成", f"已恢复：{names}")

        buttons = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="关闭", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="恢复选中事务", command=do_restore).pack(side="right", padx=6)

    def _about(self):
        messagebox.showinfo(
            "关于",
            "GI Agent 控制台\n\n"
            f"项目目录：{PROJECT_ROOT}\n"
            "运行方式：把 main.py 当子进程拉起，stdout/stdin 双向桥接。\n"
            "打包：scripts\\build_exe.ps1（PyInstaller）。",
        )

    # ---------- 小工具 ----------
    def _env_summary(self):
        try:
            from skills import env_config

            values = env_config.load_env(ENV_PATH)
        except Exception:
            values = {}
        provider = values.get("LLM_PROVIDER") or os.getenv("LLM_PROVIDER") or "未配置"
        model = values.get("MODEL_NAME") or os.getenv("MODEL_NAME") or "-"
        uid = values.get("DEFAULT_UID") or os.getenv("DEFAULT_UID") or "未填 UID"
        return f"{provider} / {model} / {uid}"

    def _bgi_path(self, relative):
        try:
            import config

            return os.path.join(config.BGI_DIR, *relative.split("/"))
        except Exception:
            return PROJECT_ROOT

    def _open_path(self, path):
        if not os.path.exists(path):
            messagebox.showwarning("路径不存在", path)
            return
        try:
            os.startfile(path)  # noqa: S606 - Windows 上就是用它开资源管理器
        except Exception:
            subprocess.Popen(["explorer", os.path.normpath(path)])

    def _on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("还在运行", "Agent 子进程还在跑，退出控制台会一并停掉它。确定退出？"):
                return
            self._stop_agent()
        self._save_geometry()
        self.destroy()


HELP_TEXT = """GI Agent 控制台 · 速查

一、怎么用
  1. 「▶ 启动」拉起 Agent（等于在终端跑 python main.py）；
  2. 在下面输入框写自然语言需求，例如「锄大地 璃月」「去打一次蓝砚武器的突破副本」；
  3. Agent 给出方案并弹出审批时，状态灯变黄，用快捷键拍板：
       y    = 批准执行并启动 BetterGI
       t    = 只写配置（推荐先用它，然后自己在 BetterGI 里核对 diff）
       exit = 退出 Agent
       也可以直接在输入框里打字反驳（例如「不想刷这个，去打地脉」）。
  4. 其它内置命令：refresh 刷新展柜上下文 ｜ history 查看历史 ｜ clear 清空记忆 ｜ rollback [事务ID]

二、配置页
  · 图形化编辑 .env，保存时自动备份成 .env.bak-<时间戳>；
  · 密钥默认打码显示，勾「显示密钥」才明文；
  · 改完必须重启 Agent 才生效（config.py 在 import 时就读取环境变量）。

三、维护工具
  · 刷新展柜上下文：立刻重新抓一遍 Enka 展柜（不用重启 Agent）；
  · 环境体检：检查 LLM 配置、BetterGI 目录、当前生效的一条龙配置、各脚本组路线数与
    战斗策略、AutoPathing 类目、展柜缓存、备份事务 —— 只读，不改文件；
  · 回滚 BetterGI 配置：选一条事务，把执行前的 BetterGI 配置恢复回来
    （只还原文件，不撤销游戏内已发生的操作；建议先关掉 BetterGI）。

四、常见故障速查
  · 「战斗策略文件不存在」→ 组里配的策略名在 User\\AutoFight 下没有对应 txt。
    在「维护工具 → 环境体检」里会直接列出是哪几个组；改成「根据队伍自动选择」即可。
  · 「当前获取焦点的窗口不是原神」→ 跑图时别切到别的窗口（BetterGI 会暂停甚至中止）。
  · 停止任务时 BetterGI 闪退（0xc00000fd）→ 组里躺着大量 Disabled 路线导致的
    日志爆发；控制台里把 BGI_ROUTE_GROUP_POLICY 保持 shrink（自动精简）。
  · 展柜里查不到角色 → 先点「刷新展柜上下文」；还不行就检查 DEFAULT_UID。

五、打包成 exe
  在项目根目录执行：  powershell -ExecutionPolicy Bypass -File scripts\\build_exe.ps1
  产物：dist\\GI-Agent-Console.exe（放在项目根目录双击运行，会读取同目录的 .env / memory）。
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--run-cli" in argv:
        return run_cli()
    if "--doctor" in argv:
        from skills import health_check

        return health_check.main()

    app = ConsoleApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
