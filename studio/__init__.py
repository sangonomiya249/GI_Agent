"""GI Agent Studio：网页版控制台（更现代的界面）。

- `studio.agent_runner`：Agent 子进程桥（启动/停止/喂输入/日志缓冲）
- `studio.server`：Flask 本地服务（只监听 127.0.0.1）
- `studio.logs`：读 BetterGI 自己的日志并按已知坑位高亮
- `studio.web/`：界面（HTML/CSS/JS，无外网依赖）

入口是项目根目录的 `app_web.py`；旧的 tkinter 版 `gui.py` 保留为备用。
"""

__all__ = ["agent_runner", "logs", "server"]
