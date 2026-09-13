"""单元测试包（放这个文件是为了让 `python -m unittest discover` 能用）。

跑全部测试：

    python -m unittest discover -s tests -t .

只跑一个模块：

    python -m unittest tests.test_gather_cooldown

⚠️ 测试**不需要** BetterGI / 原神 / 网络 / `.env`（都用临时目录和打桩），
   缺 `memory/game_dict_baike_full.json` 这类数据文件时相关用例会自动 skip。
"""
