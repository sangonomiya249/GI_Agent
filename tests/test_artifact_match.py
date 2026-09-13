"""圣遗物 → 秘境名解析（skills/artifact_match.py）的测试。

玩家实测的坑：百科「获取途径」那一列有些行是整段说明文字，以前会把 50~91 个字整段当成副本名
下发 —— 审批屏"体力目标"显示一段散文，玩家点 y 之后 `resolve_domain_target` 直接抛
「无法把 '…' 解析成秘境」，整轮被拒。所以现在要求：**纯中文、3~8 字、不含说明性通用词、
且优先挑"全字典公认的秘境名"**。
"""

import json
import os
import unittest

import config
from skills.artifact_match import (
    EXCEPTION_MAP,
    _known_domain_names,
    _looks_like_domain_name,
    get_domain_by_user_intent,
)


def table(*rows):
    return {"matched_tables": [{"rows": [list(row) for row in rows]}]}


# 结构仿照真实的 memory/artifact_get_methods_raw.json：
# 键是词条 id，title 是**套装名**，「获取途径」那一列才是秘境名（可能埋在散文里）
SAMPLE = {
    "a1": {
        "title": "绝缘之旗印",
        **table(("获取途径", "椛染之庭：小精灵的任务、秘境挑战")),
    },
    "a2": {
        "title": "翠绿之影",
        **table((
            "获取途径",
            "地图宝箱概率获取； 精英级敌人及部分BOSS概率掉落； 铭记之谷：小精灵的任务、秘境挑战",
        )),
    },
    "a3": {
        "title": "晨星与月的晓歌",
        **table(("获取途径", "月童的库藏")),
    },
    "a4": {
        "title": "角斗士的终幕礼",
        **table(("获取途径", "爆炎树的挑战奖励：无相之火概率掉落")),
    },
    "a5": {
        "title": "赌徒",
        **table(("获取途径", "地图宝箱概率获取； 见闻奖励； 深境螺旋")),
    },
    # 例外字典（EXCEPTION_MAP）只负责把「草套」映射成官方套装名，秘境名仍要从字典里取
    "a6": {
        "title": "深林的记忆",
        **table(("获取途径", "椛染之庭：小精灵的任务、秘境挑战")),
    },
}


class NameShapeTests(unittest.TestCase):
    def test_accepts_real_domain_names(self):
        for name in ("椛染之庭", "铭记之谷", "月童的库藏", "芬德尼尔之顶", "纯水精灵·洛蒂娅"):
            self.assertEqual(_looks_like_domain_name(name), name, name)

    def test_rejects_prose_and_labels(self):
        for name in (
            "地图宝箱概率获取",
            "概率掉落",
            "见闻奖励",
            "圣遗物秘境",
            "炼武秘境",
            "IV概率掉落",
            "三等」概率开出",
            "～Ⅴ",
            "",
            "太短",
            "这个副本名字实在是太长了根本放不下",
        ):
            self.assertEqual(_looks_like_domain_name(name), "", name)


class DomainResolutionTests(unittest.TestCase):
    def test_plain_row(self):
        self.assertEqual(get_domain_by_user_intent("绝缘", SAMPLE), "椛染之庭")

    def test_prose_row_picks_the_real_domain(self):
        """散文单元格里要挑出真正的秘境名，而不是"地图宝箱概率获取"这种片段。"""
        self.assertEqual(get_domain_by_user_intent("翠绿之影", SAMPLE), "铭记之谷")

    def test_standalone_value(self):
        self.assertEqual(get_domain_by_user_intent("晨星与月的晓歌", SAMPLE), "月童的库藏")

    def test_boss_drops_are_not_domains(self):
        result = get_domain_by_user_intent("角斗士的终幕礼", SAMPLE)

        self.assertNotIn("爆炎树", result, "首领名不能当成秘境名")
        self.assertLessEqual(len(result), 8)

    def test_pure_prose_gives_no_domain(self):
        self.assertEqual(get_domain_by_user_intent("赌徒", SAMPLE), "未找到对应副本")

    def test_exception_map_still_works(self):
        # 「草套」→ 例外字典给出官方套装名「深林的记忆」→ 再从那一行的获取途径里取秘境名
        self.assertEqual(get_domain_by_user_intent("草套", SAMPLE), "椛染之庭")

    def test_exception_keyword_without_dictionary_entry(self):
        """例外字典命中、但字典里没有这个词条时，老实说"未找到"，不能瞎猜。"""
        self.assertEqual(get_domain_by_user_intent("水套", SAMPLE), "未找到对应副本")

    def test_unknown_keyword(self):
        self.assertEqual(get_domain_by_user_intent("不存在的套装名", SAMPLE), "未找到对应副本")

    def test_known_domain_names_is_clean(self):
        known = _known_domain_names(SAMPLE)

        self.assertIn("椛染之庭", known)
        self.assertIn("铭记之谷", known)
        self.assertNotIn("地图宝箱概率获取", known)
        self.assertNotIn("爆炎树", known)


class RealDictionaryTests(unittest.TestCase):
    """真实字典上的回归（字典不在就跳过）。"""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(config.MEMORY_DIR, "artifact_get_methods_raw.json")
        if not os.path.isfile(path):
            raise unittest.SkipTest("没有 memory/artifact_get_methods_raw.json")
        with open(path, encoding="utf-8") as handle:
            cls.data = json.load(handle)

    def test_no_prose_ever_leaks_as_a_domain(self):
        """全字典扫一遍：任何解析结果都不该超过 8 个字（以前有 50~91 字的）。"""
        worst = ""
        for item in self.data.values():
            title = str((item or {}).get("title") or "")
            if not title:
                continue
            domain = get_domain_by_user_intent(title, self.data)
            if len(domain) > len(worst):
                worst = domain
        self.assertLessEqual(len(worst), 8, f"出现了超长副本名：{worst!r}")

    def test_previously_broken_entries(self):
        """这 4 个以前会把整段说明文字当副本名（审计实测 50/50/58/91 字）。"""
        for title in ("游医", "冒险家", "幸运儿", "战狂"):
            with self.subTest(title=title):
                domain = get_domain_by_user_intent(title, self.data)
                if domain != "未找到对应副本":
                    self.assertLessEqual(len(domain), 8, f"{title} → {domain!r}")
                    self.assertNotIn("；", domain)

    def test_common_domains_are_recognised(self):
        for keyword, expected in (("绝缘", "椛染之庭"), ("翠绿之影", "铭记之谷")):
            with self.subTest(keyword=keyword):
                self.assertEqual(get_domain_by_user_intent(keyword, self.data), expected)


if __name__ == "__main__":
    unittest.main()
