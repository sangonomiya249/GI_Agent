#!/usr/bin/env python3
"""
BOSS掉落素材数据库演示脚本

这个脚本演示了如何使用BOSS掉落物品管理系统

用法（在项目根目录）：python scripts/demo_boss_drops.py

⚠️ 从 scripts/ 里跑时，`skills` 这个包不在 sys.path 上，所以下面先把项目根加进去 ——
这个脚本原来放在根目录，搬进 scripts/ 后不加这一句就会 ModuleNotFoundError。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from skills.boss_drop_scraper import (
    load_boss_drops, 
    save_boss_drops, 
    add_boss_drops,
    get_all_boss_drops,
    print_boss_drops_summary
)

def main():
    """演示主函数"""
    
    print("=" * 70)
    print("原神BOSS掉落素材管理系统 - 演示")
    print("=" * 70)
    
    # 1. 加载现有数据
    print("\n[步骤1] 加载现有的BOSS数据...")
    boss_dict = load_boss_drops()
    print(f"✅ 成功加载 {len(boss_dict)} 个BOSS")
    
    # 2. 打印摘要
    print("\n[步骤2] 打印BOSS数据摘要...")
    print_boss_drops_summary()
    
    # 3. 查询单个BOSS
    print("\n[步骤3] 查询单个BOSS的掉落物品...")
    boss_name = "深黯魇语之主"
    if boss_name in boss_dict:
        drops = boss_dict[boss_name]
        print(f"\n{boss_name} 掉落物品 ({len(drops)} 种):")
        for i, drop in enumerate(drops, 1):
            print(f"  {i:2d}. {drop}")
    
    # 4. 添加新BOSS
    print("\n[步骤4] 添加新BOSS...")
    new_bosses = [
        ("若陀龙王", ["冒险阅历", "龙骨粉", "龙珠", "摩拉", "雨林野生植物", "异域花卉"]),
        ("无相之水·希伊", ["冒险阅历", "水晶块", "无相之水", "摩拉", "雨林野生植物"]),
        ("无相之风·贝特", ["冒险阅历", "晶块", "无相之风", "摩拉", "火焰花"]),
    ]
    
    for boss_name, drops in new_bosses:
        if boss_name not in boss_dict:
            add_boss_drops(boss_name, drops)
        else:
            print(f"⚠️  {boss_name} 已存在，跳过")
    
    # 5. 再次打印摘要
    print("\n[步骤5] 添加后的数据摘要...")
    print_boss_drops_summary()
    
    # 6. 统计信息
    print("\n[步骤6] 数据统计...")
    all_bosses = get_all_boss_drops()
    total_bosses = len(all_bosses)
    total_materials = sum(len(drops) for drops in all_bosses.values())
    
    print(f"\n📊 统计信息:")
    print(f"  • 总BOSS数: {total_bosses}")
    print(f"  • 总材料数: {total_materials}")
    print(f"  • 平均每个BOSS掉落: {total_materials/total_bosses:.1f} 种材料")
    
    # 7. 按材料数排序
    print("\n[步骤7] 按掉落物品数排序...")
    sorted_bosses = sorted(all_bosses.items(), key=lambda x: len(x[1]), reverse=True)
    print("\n掉落物品最多的5个BOSS:")
    for i, (name, drops) in enumerate(sorted_bosses[:5], 1):
        print(f"  {i}. {name}: {len(drops)} 种")
    
    # 8. 导出说明
    print("\n[步骤8] 数据导出信息...")
    print(f"\n✅ 所有数据已保存到: memory/boss_drops_dict.json")
    print(f"✅ 当前包含 {total_bosses} 个BOSS的掉落物品信息")
    
    # 9. 使用建议
    print("\n[步骤9] 使用建议...")
    print("""
使用此系统的几种方式：

1️⃣  使用管理工具添加更多BOSS:
    python skills/boss_drop_manager.py

2️⃣  在Python代码中使用:
    from skills.boss_drop_scraper import get_all_boss_drops
    bosses = get_all_boss_drops()
    
3️⃣  直接查询特定BOSS:
    from skills.boss_drop_scraper import load_boss_drops
    bosses = load_boss_drops()
    if "若陀龙王" in bosses:
        print(bosses["若陀龙王"])

4️⃣  手动编辑JSON文件:
    memory/boss_drops_dict.json
    """)
    
    print("\n" + "=" * 70)
    print("演示完成！")
    print("=" * 70)

if __name__ == "__main__":
    main()
