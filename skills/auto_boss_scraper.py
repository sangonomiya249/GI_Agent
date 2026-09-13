#!/usr/bin/env python3
"""
BOSS敌首掉落素材浏览器爬虫
使用Playwright自动访问每个BOSS页面并提取掉落物品
"""

import json
import time
import asyncio
from typing import Dict, List, Tuple
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config   # noqa: E402  （用 config.project_path 拼绝对路径，别依赖工作目录）

# 所有BOSS的ID和名称（从页面列表提取）
BOSS_LIST = [
    (508281, "丘尔德里克"),
    (508280, "摩诃婆苏提婆耶弗太子"),
    (508282, "辖域守护者"),
    (508279, "守望者·堕天"),
    (508031, "玻瑞亚斯之影"),
    (508006, "蕴光月守宫"),
    (507667, "「博士」"),
    (507662, "蕴光凛狼"),
    (507665, "驰岚·霜夜灵嗣"),
    (507663, "深黯魇语之主"),
    (507664, "金礞·霜夜灵嗣"),
    (507666, "涌流·霜夜灵嗣"),
    (507658, "十六倍曼陀草"),
    (507660, "望乡的孤狼"),
    (507661, "深黯钓客"),
    (507659, "海捷德"),
    (507351, "超重型陆巡舰·机动战垒"),
    (507055, "「猎月人」雷利尔"),
    (506916, "霜夜巡天灵主"),
    (506167, "荒野狂狩士"),
    (506166, "荒野幽徒"),
    (506281, "拉斯科尔尼科夫"),
    (506176, "「蟹沙皇」"),
    (506133, "凌晶·霜夜灵嗣"),
    (506132, "辉电·霜夜灵嗣"),
    (506134, "蔓结·霜夜灵嗣"),
    (506131, "灼烜·霜夜灵嗣"),
    (506159, "西格德"),
    (505689, "巴窟纳瓦"),
    (505677, "最后的特诺奇兹托克人"),
    (505313, "秘源机兵·统御械"),
    (505312, "门扉前的弈局"),
    (508501, "深古秘源机龙"),
    (504935, "天使海兔"),
    (504934, "猎刀鳐"),
    (504932, "帽子水母"),
    (503951, "蚀灭的源焰之主"),
    (503950, "灵觉隐修的迷者"),
    (502358, "莉琉"),
    (501187, "西尼阿斯"),
    (501883, "巴拉奇科"),
    (501882, "科西霍"),
    (501881, "异色三连星"),
    (501880, "海浪中的莎孚"),
    (501879, "金焰绒翼龙暴君"),
    (501884, "贪食匿叶龙山王"),
    (1987, "若陀龙王"),
    (1223, "「公子」"),
    (212, "北风的王狼"),
    (209, "裂空的魔龙"),
    (189, "狂风之核"),
    (173, "无相之雷·阿莱夫"),
    (2626, "雷音权现"),
    (3573, "祸津御建鸣神命"),
    (1769, "洛蒂娅的愤怒"),
    (1770, "深渊使徒·激流"),
    (2461, "无相之火·亚因"),
    (2624, "「女士」"),
    (2625, "无相之水·希伊"),
    (2027, "深渊咏者·紫电"),
]


async def scrape_boss_from_page(page, boss_id: int, boss_name: str) -> Tuple[str, List[str]]:
    """
    从BOSS详情页中提取掉落物品
    """
    try:
        url = f"https://baike.mihoyo.com/ys/obc/content/{boss_id}/detail?bbs_presentation_style=no_header&visit_device=pc"
        
        print(f"  📄 正在访问: {url}")
        await page.goto(url, wait_until='domcontentloaded', timeout=20000)
        await page.wait_for_timeout(1000)
        
        # 提取掉落物品
        drops = await page.evaluate("""
            () => {
                const result = [];
                
                // 查找"掉落物品"行
                const cells = document.querySelectorAll('td');
                for (let i = 0; i < cells.length; i++) {
                    if (cells[i].textContent?.includes('掉落物品')) {
                        // 找到该行的下一个单元格
                        const row = cells[i].closest('tr');
                        if (row) {
                            const nextCell = row.querySelector('td:nth-child(2)');
                            if (nextCell) {
                                // 查找所有列表项
                                const items = nextCell.querySelectorAll('li');
                                items.forEach(item => {
                                    const text = item.textContent?.trim();
                                    if (text && !text.match(/^\\d+$/) && text !== '无') {
                                        const clean = text.replace(/\\s*\\d+\\s*$/, '').trim();
                                        if (clean && !result.includes(clean)) {
                                            result.push(clean);
                                        }
                                    }
                                });
                            }
                        }
                        break;
                    }
                }
                
                return result;
            }
        """)
        
        return (boss_name, drops if drops else [])
    
    except Exception as e:
        print(f"  ❌ 错误: {str(e)}")
        return (boss_name, [])


async def scrape_all_bosses_async():
    """
    异步爬取所有BOSS的掉落物品
    """
    from playwright.async_api import async_playwright
    
    boss_dict = {}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        print(f"\n🚀 开始爬取 {len(BOSS_LIST)} 个BOSS的掉落物品...\n")
        
        for idx, (boss_id, boss_name) in enumerate(BOSS_LIST, 1):
            print(f"[{idx}/{len(BOSS_LIST)}] 正在处理: {boss_name}")
            name, drops = await scrape_boss_from_page(page, boss_id, boss_name)
            
            if drops:
                boss_dict[name] = drops
                print(f"  ✅ 成功: 获得 {len(drops)} 种掉落物品")
            else:
                print(f"  ⚠️  未找到掉落物品")
            
            # 每个请求间隔500ms，避免过快请求
            if idx < len(BOSS_LIST):
                await page.wait_for_timeout(500)
        
        await browser.close()
    
    return boss_dict


def scrape_all_bosses():
    """
    同步包装函数 - 爬取所有BOSS
    """
    boss_dict = asyncio.run(scrape_all_bosses_async())

    # ⚠️ 保存前必须检查结果非空：这个文件是**运行期依赖**（char_boss_match / route_group 都读它），
    #    没网 / 代理不通 / 百科改版导致一条都没抓到时会抓成 {}，直接覆写就把整份字典清空了，
    #    之后"材料 → 首领"反查全废、Boss 任务还会以"该材料的突破材料不来自任何首领"这种错误原因失败。
    output_path = config.project_path("memory", "boss_drops_dict.json")
    if not boss_dict:
        print("❌ 一条掉落都没抓到（没网 / 代理不通 / 百科改版？），**不覆盖**现有字典：", output_path)
        return 1

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 原子写：先写临时文件再替换，避免中途被打断留下半截 JSON
    temp_path = f"{output_path}.tmp"
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(boss_dict, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, output_path)
    
    # 打印摘要
    print(f"\n" + "=" * 70)
    print("✅ 爬虫完成！")
    print("=" * 70)
    print(f"\n📊 统计信息:")
    print(f"  • 成功爬取: {len(boss_dict)} 个BOSS")
    print(f"  • 总掉落物品种类: {sum(len(drops) for drops in boss_dict.values())}")
    print(f"  • 平均每个BOSS: {sum(len(drops) for drops in boss_dict.values()) / len(boss_dict):.1f} 种")
    print(f"\n  • 已保存到: {output_path}")
    
    # 打印前10个BOSS
    print(f"\n📋 前10个BOSS的掉落物品:")
    for i, (name, drops) in enumerate(list(boss_dict.items())[:10], 1):
        print(f"\n  {i}. {name}")
        print(f"     掉落数: {len(drops)}")
        if drops:
            print(f"     物品: {', '.join(drops[:5])}{'...' if len(drops) > 5 else ''}")
    
    if len(boss_dict) > 10:
        print(f"\n  ... 还有 {len(boss_dict) - 10} 个BOSS")
    
    return boss_dict


if __name__ == "__main__":
    print("=" * 70)
    print("BOSS敌首掉落素材自动爬虫 v2.0")
    print("=" * 70)
    print()
    
    try:
        boss_dict = scrape_all_bosses()
        if boss_dict == 1:          # 一条都没抓到（函数返回 1 表示"没覆盖字典"）
            raise SystemExit(1)
    except Exception as e:
        print(f"\n❌ 爬虫运行出错: {str(e)}")
        print("\n💡 建议:")
        print("  1. 检查网络连接")
        print("  2. 确保代理设置正确")
        print("  3. 尝试重新运行脚本")
