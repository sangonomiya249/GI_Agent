"""对着真实的 BetterGI 数据跑一遍「锄大地 vs 敌人与魔物」隔离验证（**不改任何真实文件**）。

用法（项目根目录）：
    .\\venv\\Scripts\\python.exe scripts\\verify_hoe_routes.py

做的事情：
  1. 只读真实 `User\\ScriptGroup\\*.json`；
  2. 用一个临时目录当"目标组目录"，把 锄大地.json / 敌人与魔物.json 复制进去；
  3. 分别按 `hoe` / `hunt` 类目挑路线，打印命中条数与前几条路线名，确认两边互不串味。
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from skills import route_group  # noqa: E402


def _stage_real_groups(target_dir):
    """把真实的 锄大地 / 敌人与魔物 组复制到临时目录。"""
    copied = []
    for name in (
        config.BGI_HOE_CONFIG_NAME,
        config.BGI_ENEMY_CONFIG_NAME,
        config.BGI_MAP_CONFIG_NAME,
    ):
        source = os.path.join(config.BGI_SCRIPT_GROUP_DIR, name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(target_dir, name))
            copied.append(name)
    return copied


def _count(spec, targets, group_dir):
    return route_group.category_preferred_match_count(spec, list(targets), group_dir)


def e2e_dry_run(specs):
    """在临时目录里跑一遍真实的 execute_bgi_task（'t' 测试模式），看玩家会看到什么。

    真实文件全部不动：脚本组先复制到临时目录，一条龙配置也在临时目录里另起一份。
    """
    from unittest.mock import patch

    from skills import bgi_controller

    one_dragon_source = bgi_controller.resolve_one_dragon_config_path()
    with tempfile.TemporaryDirectory() as temp_dir:
        group_dir = os.path.join(temp_dir, "ScriptGroup")
        os.makedirs(group_dir)
        _stage_real_groups(group_dir)

        one_dragon = os.path.join(temp_dir, "OneDragon.json")
        with open(one_dragon_source, "r", encoding="utf-8") as handle:
            one_dragon_data = json.load(handle)
        with open(one_dragon, "w", encoding="utf-8") as handle:
            json.dump(one_dragon_data, handle, ensure_ascii=False, indent=2)

        print("=" * 70)
        print("端到端演练：玩家说「锄大地 璃月」（测试模式，只写临时目录）")
        print("=" * 70)
        with patch.object(config, "BGI_SCRIPT_GROUP_DIR", group_dir), patch.object(
            config, "BGI_HOE_CONFIG", os.path.join(group_dir, config.BGI_HOE_CONFIG_NAME)
        ), patch.object(
            config, "BGI_ENEMY_CONFIG", os.path.join(group_dir, config.BGI_ENEMY_CONFIG_NAME)
        ), patch.object(
            config, "BGI_MAP_CONFIG", os.path.join(group_dir, config.BGI_MAP_CONFIG_NAME)
        ), patch.object(
            config, "BGI_BACKUP_DIR", os.path.join(temp_dir, "backups")
        ), patch.object(
            bgi_controller, "resolve_one_dragon_config_path", return_value=one_dragon
        ), patch.object(
            bgi_controller, "ensure_bettergi_closed", return_value=True
        ), patch.object(
            bgi_controller.feishu_api, "send_feishu_msg"
        ):
            bgi_controller.execute_bgi_task(
                {"energy_task": {}, "free_task": [{"action": "hoe", "target": "璃月"}]},
                "t",
                {},
                "open_id",
                "100000000",
            )

        with open(one_dragon, "r", encoding="utf-8") as handle:
            written = json.load(handle)
        definitions = written.get("TaskDefinitions") or {}
        enabled_keys = [
            key
            for key, value in (written.get("TaskEnabledList") or {}).items()
            if value
        ]
        enabled = [definitions.get(key, f"{key}（未登记，不会被执行）") for key in enabled_keys]
        print(f"\n演练结果：一条龙里被置为启用的任务 = {enabled}")
        hoe_path = os.path.join(group_dir, config.BGI_HOE_CONFIG_NAME)
        if os.path.isfile(hoe_path):
            with open(hoe_path, "r", encoding="utf-8") as handle:
                written_group = json.load(handle)
            projects = written_group.get("projects") or []
            print(
                f"{config.BGI_HOE_CONFIG_NAME}：写入 {len(projects)} 条，"
                f"其中 Enabled {sum(1 for p in projects if p.get('status') == 'Enabled')} 条"
            )
        print(f"（临时目录已清理；真实文件一个都没改）")



def _sample(spec, targets, group_dir, limit=3):
    path = route_group.category_group_path(spec, group_dir)
    group = route_group.load_group(path) or {}
    terms = route_group.category_search_terms(spec, targets)
    hits = [
        project
        for project in group.get("projects") or []
        if route_group.matches_targets(
            project,
            terms,
            spec.match_fields,
            spec.both_ways,
            own_prefix=spec.folder_prefix,
            skip_tokens=route_group.effective_skip_tokens(spec, terms),
        )
    ]
    return hits[:limit], len(hits)


def main():
    print(f"BetterGI 安装目录：{config.BGI_DIR}")
    print(f"AutoPathing 目录 ：{config.BGI_AUTO_PATHING_DIR}")
    print(f"调度器目录       ：{config.BGI_SCRIPT_GROUP_DIR}\n")

    specs = route_group.all_category_specs()
    with tempfile.TemporaryDirectory() as temp_dir:
        copied = _stage_real_groups(temp_dir)
        print(f"已复制到临时目录的组：{copied or '（一个都没有，可能还没建组）'}\n")

        hoe_group = os.path.join(temp_dir, config.BGI_HOE_CONFIG_NAME)
        if not os.path.isfile(hoe_group):
            print(f"⚠️ 真机还没有 {config.BGI_HOE_CONFIG_NAME}，按 AutoPathing 自动生成一份看看：")
            generated = route_group.ensure_category_group(specs["hoe"], group_dir=temp_dir)
            if generated:
                _path, data, notice = generated
                with open(hoe_group, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                print(notice)
            else:
                print("（AutoPathing 里也没有 锄地专区 路线）")

        checks = [
            ("hoe", ["锄大地"]),
            ("hoe", ["璃月"]),
            ("hoe", ["精英"]),
            ("hoe", ["挪德卡莱"]),
            ("hoe", ["锄大地 连低效一起跑"]),
            ("hunt", ["骗骗花"]),
            ("hunt", ["飞萤"]),
            ("hunt", ["锄大地"]),
            ("hunt", ["精英"]),
        ]
        for action, targets in checks:
            spec = specs[action]
            hits, total = _sample(spec, targets, temp_dir)
            folders = sorted({project.get("folderName") for project in hits})
            print(f"[{action}] {targets} → 命中 {total} 条；示例目录：{folders}")

        print("\n隔离结论：")
        print(f"  「锄大地」在 hoe 里命中 {_count(specs['hoe'], ['锄大地'], temp_dir)} 条，"
              f"在 hunt 里命中 {_count(specs['hunt'], ['锄大地'], temp_dir)} 条（应为 0）")
        print(f"  「飞萤」在 hunt 里命中 {_count(specs['hunt'], ['飞萤'], temp_dir)} 条，"
              f"在 hoe 里命中 {_count(specs['hoe'], ['飞萤'], temp_dir)} 条（两边各自独立）")

    e2e_dry_run(specs)


if __name__ == "__main__":
    main()
