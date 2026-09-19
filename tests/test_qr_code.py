"""二维码编码器（`skills/qr_code.py`）。

**这一组用例的重点是"它画出来的东西真的是二维码"**，而不是"函数能跑"：
一张扫不出来的二维码比没有二维码更糟 —— 玩家只会觉得"你这功能坏了"。

基准来自社区实现 `jquery-qrcode`（`tests/fixtures/qr_reference.json`，MIT）：
对同一批文本、同一版本、同一纠错级别，逐模块比对。那份实现被无数项目用过，
逐位一致就说明我们的编码、纠错、掩码选择、格式信息都是对的一套。

要重新生成基准：下载 `jquery.qrcode.min.js`，用一个最小 jQuery 垫片在 Node 里跑
`render:"table"`，从单元格背景色把矩阵抠出来（`_meta.how` 里记了做法）。
"""

import json
import os
import re
import unittest

from skills import qr_code

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "qr_reference.json")


def _reference_cases():
    with open(FIXTURE, "r", encoding="utf-8") as handle:
        return json.load(handle)["cases"]


class ReferenceParityTests(unittest.TestCase):
    """与参考实现逐位比对 —— 这是"能扫出来"最硬的证据。"""

    @classmethod
    def setUpClass(cls):
        cls.cases = _reference_cases()

    def test_fixture_is_not_empty(self):
        self.assertGreater(len(self.cases), 30)
    def test_every_case_matches_the_reference_bit_for_bit(self):
        mismatched = []
        for case in self.cases:
            reference = [[cell == "1" for cell in row] for row in case["matrix"]]
            mine = qr_code.matrix(case["text"], level=case["level"],
                                  type_number=case["typeNumber"] or None)
            if len(mine) != len(reference):
                mismatched.append(f"{case['level']}/{case['typeNumber']}: "
                                  f"尺寸 {len(mine)} != {len(reference)}")
                continue
            wrong = sum(1 for y in range(len(mine)) for x in range(len(mine))
                        if mine[y][x] != reference[y][x])
            if wrong:
                mismatched.append(f"{case['level']}/{case['typeNumber']}: {wrong} 个模块不一致")

        self.assertEqual(mismatched, [], "与参考实现不一致：" + "；".join(mismatched))


class StructureTests(unittest.TestCase):
    """规范里几条一眼能验的结构（跟参考实现无关，是二维码本身的硬约束）。"""

    def test_size_follows_the_version(self):
        for version in (1, 2, 5, 10, 25, 40):
            side = qr_code.size_of("x", type_number=version)
            self.assertEqual(side, version * 4 + 17)

    def test_three_finder_patterns(self):
        """三个角上的定位图案（外加一圈白边）——手机就是靠它们找到二维码的。"""
        finder = [
            "1111111",
            "1000001",
            "1011101",
            "1011101",
            "1011101",
            "1000001",
            "1111111",
        ]
        cells = qr_code.matrix("https://example.com/", level="M")
        side = len(cells)
        for row, col in ((0, 0), (0, side - 7), (side - 7, 0)):
            block = ["".join("1" if cells[row + r][col + c] else "0" for c in range(7))
                     for r in range(7)]
            self.assertEqual(block, finder, f"({row},{col}) 的定位图案不对")
        # 左上角定位图案右边/下边的一圈必须是白的（分隔符）
        self.assertFalse(any(cells[7][col] for col in range(8)))
        self.assertFalse(any(cells[row][7] for row in range(8)))

    def test_timing_pattern_alternates(self):
        cells = qr_code.matrix("https://example.com/", level="M")
        side = len(cells)
        for index in range(8, side - 8):
            self.assertEqual(cells[6][index], index % 2 == 0)
            self.assertEqual(cells[index][6], index % 2 == 0)

    def test_auto_version_grows_with_the_data(self):
        sizes = [qr_code.size_of("a" * length)
                 for length in (5, 40, 120, 300)]
        self.assertEqual(sizes, sorted(sizes))
        self.assertLess(sizes[0], sizes[-1])

    def test_higher_error_correction_needs_a_bigger_version(self):
        text = "a" * 60
        self.assertLessEqual(qr_code.size_of(text, level="L"),
                             qr_code.size_of(text, level="H"))

    def test_too_much_data_is_reported_not_silently_truncated(self):
        with self.assertRaises(qr_code.QrError):
            # 版本 1 塞 300 字节：必须报错，不能悄悄丢掉后半截
            qr_code.matrix("a" * 300, level="H", type_number=1)

    def test_bad_level_or_version_is_rejected(self):
        with self.assertRaises(qr_code.QrError):
            qr_code.matrix("x", level="Z")
        with self.assertRaises(qr_code.QrError):
            qr_code.matrix("x", type_number=41)

    def test_non_ascii_text_is_encoded_as_utf8(self):
        """中文链接也得能编 —— 参考实现那条 `charCodeAt` 老路对非 ASCII 是坏的。"""
        cells = qr_code.matrix("米游社扫码登录", level="M")

        self.assertGreater(len(cells), 20)


class RenderTests(unittest.TestCase):
    def test_ascii_uses_half_blocks_and_is_not_empty(self):
        art = qr_code.to_ascii("https://example.com/", border=1)

        self.assertIn("█", art)
        self.assertTrue(all(set(line) <= set("█▀▄ ") for line in art.splitlines()))

    def test_ascii_quiet_zone_exists(self):
        """四周必须留白（静区），否则手机对不上焦。"""
        art = qr_code.to_ascii("https://example.com/", border=2).splitlines()

        self.assertEqual(art[0].strip(), "")
        self.assertEqual(art[-1].strip(), "")

    def test_svg_is_wellformed_and_square(self):
        svg = qr_code.to_svg("https://example.com/", scale=4, border=4)

        self.assertTrue(svg.startswith("<svg "))
        self.assertTrue(svg.endswith("</svg>"))
        side = qr_code.size_of("https://example.com/") + 8
        self.assertIn(f'width="{side * 4}"', svg)
        self.assertIn(f'viewBox="0 0 {side} {side}"', svg)
        # 深色模块都画在 path 里，白色背景靠一个 rect —— 不需要任何图像库
        self.assertIn("<rect", svg)
        self.assertEqual(svg.count("<rect"), 1)
        self.assertIn("<path d=\"M", svg)

    def test_svg_path_has_one_segment_per_dark_module(self):
        cells = qr_code.matrix("https://example.com/", level="M")
        dark = sum(1 for row in cells for cell in row if cell)
        svg = qr_code.to_svg("https://example.com/", level="M")

        self.assertEqual(len(re.findall(r"M\d+ \d+h1v1h-1z", svg)), dark)

    def test_png_pixels_match_the_matrix(self):
        """PNG 是纯手写的（没有 Pillow），所以必须**解回来**核对像素，不能只看文件头。"""
        import struct
        import zlib

        text = "https://example.com/"
        scale, border = 3, 4
        data = qr_code.to_png(text, scale=scale, border=border)

        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", data[16:24])
        cells = qr_code.matrix(text, level="M")
        side = (len(cells) + border * 2) * scale
        self.assertEqual((width, height), (side, side))

        # 逐个 chunk 走一遍，拼出 IDAT
        offset, chunks = 8, {}
        while offset < len(data):
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            tag = data[offset + 4:offset + 8]
            chunks[tag] = data[offset + 8:offset + 8 + length]
            offset += 12 + length

        raw = zlib.decompress(chunks[b"IDAT"])
        stride = width * 3 + 1
        self.assertEqual(len(raw), stride * height)
        for y in range(height):
            self.assertEqual(raw[y * stride], 0)            # 滤波字节必须是 0
            for x in range(width):
                start = y * stride + 1 + x * 3
                pixel = raw[start:start + 3]
                row = y // scale - border
                col = x // scale - border
                inside = 0 <= row < len(cells) and 0 <= col < len(cells)
                dark = inside and cells[row][col]
                expected = b"\x00\x00\x00" if dark else b"\xff\xff\xff"
                if pixel != expected:
                    self.fail(f"像素 ({x},{y}) 是 {pixel!r}，应当是 {expected!r}")


if __name__ == "__main__":
    unittest.main()
