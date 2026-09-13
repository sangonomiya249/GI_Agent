"""生成 Studio 的 .ico（不依赖 Pillow，纯手写 ICO + BMP 像素）。

画的是一个圆角深蓝底 + 白色播放三角（"启动/运行"语义）+ 右下角青色小点（Agent 在线）。
48x48 与 32x32 两档，Windows 会自己缩放。

用法：python scripts/make_icon.py [输出路径]
"""

import os
import struct
import sys

BG_TOP = (76, 141, 255)      # 蓝
BG_BOTTOM = (124, 92, 255)   # 紫
FG = (255, 255, 255)
ACCENT = (126, 224, 192)     # 青（状态点）


def _blend(a, b, ratio):
    return tuple(round(a[i] * (1 - ratio) + b[i] * ratio) for i in range(3))


def render(size):
    """返回 size*size 的 RGBA 像素（每行从上到下，每像素 4 字节 BGRA）。"""
    radius = max(4, size * 0.22)
    pad = size * 0.06
    side = size - pad * 2
    center = size / 2

    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            # 圆角矩形（到内缩矩形的距离场）
            dx = max(pad + radius - x, x - (size - pad - radius), 0)
            dy = max(pad + radius - y, y - (size - pad - radius), 0)
            outside = (dx * dx + dy * dy) ** 0.5 - radius
            if outside > 0.5:
                row.append((0, 0, 0, 0))
                continue

            colour = _blend(BG_TOP, BG_BOTTOM, y / max(1, size - 1))

            # 播放三角：以中心为原点，左边竖线 + 右边尖角
            tri_x0 = center - side * 0.16
            tri_x1 = center + side * 0.26
            half = side * 0.30
            if tri_x0 <= x <= tri_x1:
                span = (tri_x1 - x) / max(1.0, (tri_x1 - tri_x0))
                if abs(y - center) <= half * span:
                    colour = FG

            # 右下状态点
            dot_r = side * 0.13
            dot_x = size - pad - dot_r * 1.15
            dot_y = size - pad - dot_r * 1.15
            if (x - dot_x) ** 2 + (y - dot_y) ** 2 <= dot_r ** 2:
                colour = ACCENT

            row.append((colour[2], colour[1], colour[0], 255))
        rows.append(row)
    return rows


def ico_bytes(sizes=(48, 32, 16)):
    images = []
    for size in sizes:
        rows = render(size)
        # ICO 里的 BMP 是自下而上、行按 4 字节对齐
        pixel_rows = []
        for row in reversed(rows):
            pixel_rows.append(b"".join(struct.pack("4B", *pixel) for pixel in row))
        pixels = b"".join(pixel_rows)

        header = struct.pack(
            "<IiiHHIIiiII",
            40,            # biSize
            size,          # biWidth
            size * 2,      # biHeight（XOR + AND 两张图）
            1,             # biPlanes
            32,            # biBitCount
            0,             # biCompression
            len(pixels),   # biSizeImage
            0, 0, 0, 0,
        )
        mask_stride = ((size + 31) // 32) * 4
        mask = b"\x00" * (mask_stride * size)
        images.append((size, header + pixels + mask))

    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = b""
    for size, blob in images:
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0, 0, 1, 32, len(blob), offset,
        )
        offset += len(blob)
    return header + entries + b"".join(blob for _size, blob in images)


def main(argv):
    target = argv[1] if len(argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "studio.ico"
    )
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(ico_bytes())
    print(f"已生成图标：{target}（{os.path.getsize(target)} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
