"""QQ 官方机器人入口（轻量版）。

    python qq_main.py                # 启动（等价于 python main.py qq）
    python qq_main.py --check        # 只校验 AppID/Secret 并取一次 token
    python qq_main.py --intents 0    # 覆盖 intents
    python qq_main.py --allow-anyone # 允许任何 QQ 用户下指令（危险，默认只认白名单）

配置在 `.env`（见 .env.example 的「QQ 机器人」段）：
    QQ_BOT_APPID / QQ_BOT_SECRET / QQ_BOT_ALLOWED_USERS ...
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from channels import qq_bot  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(qq_bot.main())
