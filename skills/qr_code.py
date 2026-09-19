"""二维码编码器（**纯标准库**，不装任何第三方包）。

**为什么自己写**：扫码登录那条路要把二维码显示给玩家 —— 终端里画 ASCII、Studio 里画图。
常规做法是 `pip install qrcode`（+ `pillow`），但这台机器是离线环境，
`pip install` 直接超时；而"因为少一个可选依赖就整条登录路走不通"是不能接受的。
所以这里把编码器自带一份：只用 `zlib`/`struct` 这类标准库，并且**同时**给出
ASCII 与 SVG 两种渲染，终端和网页都能用。

**算法来源**：移植自社区广泛使用的 `jquery-qrcode`（其内核是 davidshimjs 的 `qrcode.js`，
MIT）。逐位对齐做了交叉验证：`tests/test_qr_code.py` 会拿那份实现的输出当基准，
对同一批文本逐模块比对，确保不是"看起来像个二维码"。

**实现的取舍（都写在明处）**
  · 只支持 **字节模式（8bit byte）**：我们的用途是 URL，字节模式就够了；
  · 文本统一按 **UTF-8 编码**再写入。参考实现用的是 `charCodeAt`（等于只支持 latin1），
    对 ASCII 两者逐位相同；对非 ASCII，我们这份才是对的；
  · 纠错级别、版本号都支持全量（版本 1~40、L/M/Q/H），版本号留空则按数据长度自动选；
  · 掩码图案按参考实现的罚分规则自动挑（不是照抄规范里的罚分表，而是照抄它 ——
    两边算出来必须一样，否则交叉验证过不去）。
"""

import zlib

# ==========================================
# 🌟 常量：模式、纠错级别
# ==========================================

MODE_8BIT_BYTE = 4

# 纠错级别 → 内部编号。**编号不是直觉顺序**：它是参考实现里查纠错块表用的下标，
# 对应关系 L=1 / M=0 / Q=3 / H=2，写死在这里别改。
ECL = {"L": 1, "M": 0, "Q": 3, "H": 2}

# 填充字节（规范里的 0xEC / 0x11）
_PAD0 = 0xEC
_PAD1 = 0x11


class QrError(ValueError):
    """编码失败（数据太长、级别写错之类）—— 调用方按"参数不对"处理即可。"""


# ==========================================
# 🌟 GF(256) 指数 / 对数表
# ==========================================

_EXP_TABLE = [0] * 256
_LOG_TABLE = [0] * 256
for _i in range(8):
    _EXP_TABLE[_i] = 1 << _i
for _i in range(8, 256):
    _EXP_TABLE[_i] = (_EXP_TABLE[_i - 4] ^ _EXP_TABLE[_i - 5]
                      ^ _EXP_TABLE[_i - 6] ^ _EXP_TABLE[_i - 8])
for _i in range(255):
    _LOG_TABLE[_EXP_TABLE[_i]] = _i
del _i


def _gexp(value):
    """α 的 `value` 次幂（指数按 255 循环，允许负数）。"""
    while value < 0:
        value += 255
    while value >= 256:
        value -= 255
    return _EXP_TABLE[value]


def _glog(value):
    """α 的离散对数（0 没有对数，参考实现也是直接报错）。"""
    if value < 1:
        raise QrError(f"glog({value})：0 没有离散对数")
    return _LOG_TABLE[value]


# ==========================================
# 🌟 多项式（GF(256) 上的加减都是异或）
# ==========================================


class _Polynomial:
    """首部去零的 GF(256) 多项式 —— 去零是为了让"长度"能表示真实次数。"""

    def __init__(self, num, shift=0):
        offset = 0
        while offset < len(num) and num[offset] == 0:
            offset += 1
        self.num = [0] * (len(num) - offset + shift)
        for index in range(len(num) - offset):
            self.num[index] = num[offset + index]

    def get(self, index):
        return self.num[index]

    def get_length(self):
        return len(self.num)

    def multiply(self, other):
        num = [0] * (self.get_length() + other.get_length() - 1)
        for i in range(self.get_length()):
            for j in range(other.get_length()):
                num[i + j] ^= _gexp(_glog(self.get(i)) + _glog(other.get(j)))
        return _Polynomial(num, 0)

    def mod(self, other):
        if self.get_length() - other.get_length() < 0:
            return self
        ratio = _glog(self.get(0)) - _glog(other.get(0))
        num = list(self.num)
        for i in range(other.get_length()):
            num[i] ^= _gexp(_glog(other.get(i)) + ratio)
        return _Polynomial(num, 0).mod(other)


# ==========================================
# 🌟 纠错块表（版本 1~40 × L/M/Q/H）
# ==========================================
#
# 每组是 [块数, 总码字数, 数据码字数]，每三个数一组；版本 v、级别 e 的那一行
# 就是 RS_BLOCK_TABLE[4*(v-1) + 级别内偏移]。
# 这张表是抄自参考实现的（照抄比"凭记忆重打"靠谱得多），改动前请先想清楚。
_RS_BLOCK_TABLE = [
    [1, 26, 19], [1, 26, 16], [1, 26, 13], [1, 26, 9],
    [1, 44, 34], [1, 44, 28], [1, 44, 22], [1, 44, 16],
    [1, 70, 55], [1, 70, 44], [2, 35, 17], [2, 35, 13],
    [1, 100, 80], [2, 50, 32], [2, 50, 24], [4, 25, 9],
    [1, 134, 108], [2, 67, 43], [2, 33, 15, 2, 34, 16], [2, 33, 11, 2, 34, 12],
    [2, 86, 68], [4, 43, 27], [4, 43, 19], [4, 43, 15],
    [2, 98, 78], [4, 49, 31], [2, 32, 14, 4, 33, 15], [4, 39, 13, 1, 40, 14],
    [2, 121, 97], [2, 60, 38, 2, 61, 39], [4, 40, 18, 2, 41, 19], [4, 40, 14, 2, 41, 15],
    [2, 146, 116], [3, 58, 36, 2, 59, 37], [4, 36, 16, 4, 37, 17], [4, 36, 12, 4, 37, 13],
    [2, 86, 68, 2, 87, 69], [4, 69, 43, 1, 70, 44], [6, 43, 19, 2, 44, 20], [6, 43, 15, 2, 44, 16],
    [4, 101, 81], [1, 80, 50, 4, 81, 51], [4, 50, 22, 4, 51, 23], [3, 36, 12, 8, 37, 13],
    [2, 116, 92, 2, 117, 93], [6, 58, 36, 2, 59, 37], [4, 46, 20, 6, 47, 21], [7, 42, 14, 4, 43, 15],
    [4, 133, 107], [8, 59, 37, 1, 60, 38], [8, 44, 20, 4, 45, 21], [12, 33, 11, 4, 34, 12],
    [3, 145, 115, 1, 146, 116], [4, 64, 40, 5, 65, 41], [11, 36, 16, 5, 37, 17], [11, 36, 12, 5, 37, 13],
    [5, 109, 87, 1, 110, 88], [5, 65, 41, 5, 66, 42], [5, 54, 24, 7, 55, 25], [11, 36, 12, 7, 37, 13],
    [5, 122, 98, 1, 123, 99], [7, 73, 45, 3, 74, 46], [15, 43, 19, 2, 44, 20], [3, 45, 15, 13, 46, 16],
    [1, 135, 107, 5, 136, 108], [10, 74, 46, 1, 75, 47], [1, 50, 22, 15, 51, 23], [2, 42, 14, 17, 43, 15],
    [5, 150, 120, 1, 151, 121], [9, 69, 43, 4, 70, 44], [17, 50, 22, 1, 51, 23], [2, 42, 14, 19, 43, 15],
    [3, 141, 113, 4, 142, 114], [3, 70, 44, 11, 71, 45], [17, 47, 21, 4, 48, 22], [9, 39, 13, 16, 40, 14],
    [3, 135, 107, 5, 136, 108], [3, 67, 41, 13, 68, 42], [15, 54, 24, 5, 55, 25], [15, 43, 15, 10, 44, 16],
    [4, 144, 116, 4, 145, 117], [17, 68, 42], [17, 50, 22, 6, 51, 23], [19, 46, 16, 6, 47, 17],
    [2, 139, 111, 7, 140, 112], [17, 74, 46], [7, 54, 24, 16, 55, 25], [34, 37, 13],
    [4, 151, 121, 5, 152, 122], [4, 75, 47, 14, 76, 48], [11, 54, 24, 14, 55, 25], [16, 45, 15, 14, 46, 16],
    [6, 147, 117, 4, 148, 118], [6, 73, 45, 14, 74, 46], [11, 54, 24, 16, 55, 25], [30, 46, 16, 2, 47, 17],
    [8, 132, 106, 4, 133, 107], [8, 75, 47, 13, 76, 48], [7, 54, 24, 22, 55, 25], [22, 45, 15, 13, 46, 16],
    [10, 142, 114, 2, 143, 115], [19, 74, 46, 4, 75, 47], [28, 50, 22, 6, 51, 23], [33, 46, 16, 4, 47, 17],
    [8, 152, 122, 4, 153, 123], [22, 73, 45, 3, 74, 46], [8, 53, 23, 26, 54, 24], [12, 45, 15, 28, 46, 16],
    [3, 147, 117, 10, 148, 118], [3, 73, 45, 23, 74, 46], [4, 54, 24, 31, 55, 25], [11, 45, 15, 31, 46, 16],
    [7, 146, 116, 7, 147, 117], [21, 73, 45, 7, 74, 46], [1, 53, 23, 37, 54, 24], [19, 45, 15, 26, 46, 16],
    [5, 145, 115, 10, 146, 116], [19, 75, 47, 10, 76, 48], [15, 54, 24, 25, 55, 25], [23, 45, 15, 25, 46, 16],
    [13, 145, 115, 3, 146, 116], [2, 74, 46, 29, 75, 47], [42, 54, 24, 1, 55, 25], [23, 45, 15, 28, 46, 16],
    [17, 145, 115], [10, 74, 46, 23, 75, 47], [10, 54, 24, 35, 55, 25], [19, 45, 15, 35, 46, 16],
    [17, 145, 115, 1, 146, 116], [14, 74, 46, 21, 75, 47], [29, 54, 24, 19, 55, 25], [11, 45, 15, 46, 46, 16],
    [13, 145, 115, 6, 146, 116], [14, 74, 46, 23, 75, 47], [44, 54, 24, 7, 55, 25], [59, 46, 16, 1, 47, 17],
    [12, 151, 121, 7, 152, 122], [12, 75, 47, 26, 76, 48], [39, 54, 24, 14, 55, 25], [22, 45, 15, 41, 46, 16],
    [6, 151, 121, 14, 152, 122], [6, 75, 47, 34, 76, 48], [46, 54, 24, 10, 55, 25], [2, 45, 15, 64, 46, 16],
    [17, 152, 122, 4, 153, 123], [29, 74, 46, 14, 75, 47], [49, 54, 24, 10, 55, 25], [24, 45, 15, 46, 46, 16],
    [4, 152, 122, 18, 153, 123], [13, 74, 46, 32, 75, 47], [48, 54, 24, 14, 55, 25], [42, 45, 15, 32, 46, 16],
    [20, 147, 117, 4, 148, 118], [40, 75, 47, 7, 76, 48], [43, 54, 24, 22, 55, 25], [10, 45, 15, 67, 46, 16],
    [19, 148, 118, 6, 149, 119], [18, 75, 47, 31, 76, 48], [34, 54, 24, 34, 55, 25], [20, 45, 15, 61, 46, 16],
]

# 对齐图案（定位用的"小方块"）在每个版本里的中心坐标
_PATTERN_POSITION_TABLE = [
    [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42],
    [6, 26, 46], [6, 28, 50], [6, 30, 54], [6, 32, 58], [6, 34, 62], [6, 26, 46, 66],
    [6, 26, 48, 70], [6, 26, 50, 74], [6, 30, 54, 78], [6, 30, 56, 82], [6, 30, 58, 86],
    [6, 34, 62, 90], [6, 28, 50, 72, 94], [6, 26, 50, 74, 98], [6, 30, 54, 78, 102],
    [6, 28, 54, 80, 106], [6, 32, 58, 84, 110], [6, 30, 58, 86, 114], [6, 34, 62, 90, 118],
    [6, 26, 50, 74, 98, 122], [6, 30, 54, 78, 102, 126], [6, 26, 52, 78, 104, 130],
    [6, 30, 56, 82, 108, 134], [6, 34, 60, 86, 112, 138], [6, 30, 58, 86, 114, 142],
    [6, 34, 62, 90, 118, 146], [6, 30, 54, 78, 102, 126, 150], [6, 24, 50, 76, 102, 128, 154],
    [6, 28, 54, 80, 106, 132, 158], [6, 32, 58, 84, 110, 136, 162], [6, 26, 54, 82, 110, 138, 166],
    [6, 30, 58, 86, 114, 142, 170],
]

_G15 = 1335
_G18 = 7973
_G15_MASK = 21522


class _RsBlock:
    __slots__ = ("total_count", "data_count")

    def __init__(self, total_count, data_count):
        self.total_count = total_count
        self.data_count = data_count


def _rs_block_table(type_number, error_correct_level):
    """取某一版本 + 级别那一行（偏移见表头注释）。"""
    offset = {1: 0, 0: 1, 3: 2, 2: 3}.get(error_correct_level)
    if offset is None:
        raise QrError(f"不认识的纠错级别编号：{error_correct_level}")
    if not 1 <= type_number <= 40:
        raise QrError(f"版本号必须在 1~40，收到 {type_number}")
    return _RS_BLOCK_TABLE[4 * (type_number - 1) + offset]


def _rs_blocks(type_number, error_correct_level):
    table = _rs_block_table(type_number, error_correct_level)
    blocks = []
    for index in range(len(table) // 3):
        count, total, data = table[3 * index], table[3 * index + 1], table[3 * index + 2]
        blocks.extend(_RsBlock(total, data) for _ in range(count))
    return blocks


# ==========================================
# 🌟 位缓冲
# ==========================================


class _BitBuffer:
    def __init__(self):
        self.buffer = []
        self.length = 0

    def get(self, index):
        return 1 == ((self.buffer[index // 8] >> (7 - index % 8)) & 1)

    def put(self, num, length):
        for i in range(length):
            self.put_bit(1 == ((num >> (length - i - 1)) & 1))

    def get_length_in_bits(self):
        return self.length

    def put_bit(self, bit):
        index = self.length // 8
        if len(self.buffer) <= index:
            self.buffer.append(0)
        if bit:
            self.buffer[index] |= 0x80 >> (self.length % 8)
        self.length += 1


# ==========================================
# 🌟 工具：BCH 校验、掩码、罚分
# ==========================================


def _get_bch_digit(value):
    digit = 0
    while value != 0:
        digit += 1
        value >>= 1
    return digit


def _get_bch_type_info(data):
    value = data << 10
    while _get_bch_digit(value) - _get_bch_digit(_G15) >= 0:
        value ^= _G15 << (_get_bch_digit(value) - _get_bch_digit(_G15))
    return ((data << 10) | value) ^ _G15_MASK


def _get_bch_type_number(data):
    value = data << 12
    while _get_bch_digit(value) - _get_bch_digit(_G18) >= 0:
        value ^= _G18 << (_get_bch_digit(value) - _get_bch_digit(_G18))
    return (data << 12) | value


def _get_pattern_position(type_number):
    if not 1 <= type_number <= 40:
        raise QrError(f"版本号必须在 1~40，收到 {type_number}")
    return _PATTERN_POSITION_TABLE[type_number - 1]


def _get_mask(mask_pattern, row, col):
    """8 种掩码图案。注意 `row`/`col` 的顺序别写反（照参考实现的 (c,d)=(行,列) 抄）。"""
    if mask_pattern == 0:
        return (row + col) % 2 == 0
    if mask_pattern == 1:
        return row % 2 == 0
    if mask_pattern == 2:
        return col % 3 == 0
    if mask_pattern == 3:
        return (row + col) % 3 == 0
    if mask_pattern == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask_pattern == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask_pattern == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    if mask_pattern == 7:
        return ((row * col) % 3 + (row + col) % 2) % 2 == 0
    raise QrError(f"掩码图案只能是 0~7，收到 {mask_pattern}")


def _get_error_correct_polynomial(count):
    """(x - α^0)(x - α^1)…(x - α^(count-1)) —— 生成多项式。"""
    polynomial = _Polynomial([1], 0)
    for index in range(count):
        polynomial = polynomial.multiply(_Polynomial([1, _gexp(index)], 0))
    return polynomial


def _get_length_in_bits(mode, type_number):
    if 1 <= type_number < 10:
        table = {1: 10, 2: 9, MODE_8BIT_BYTE: 8, 8: 8}
    elif type_number < 27:
        table = {1: 12, 2: 11, MODE_8BIT_BYTE: 16, 8: 10}
    elif type_number < 41:
        table = {1: 14, 2: 13, MODE_8BIT_BYTE: 16, 8: 12}
    else:
        raise QrError(f"版本号必须在 1~40，收到 {type_number}")
    if mode not in table:
        raise QrError(f"不支持的模式：{mode}")
    return table[mode]


def _get_lost_point(code):
    """罚分：掩码选优用的打分函数（照参考实现的四条规则）。"""
    count = code.get_module_count()
    lost = 0

    # 规则一：3×3 邻域里同色邻居超过 5 个
    for row in range(count):
        for col in range(count):
            same = 0
            dark = code.is_dark(row, col)
            for dr in (-1, 0, 1):
                if row + dr < 0 or row + dr >= count:
                    continue
                for dc in (-1, 0, 1):
                    if col + dc < 0 or col + dc >= count:
                        continue
                    if dr == 0 and dc == 0:
                        continue
                    if dark == code.is_dark(row + dr, col + dc):
                        same += 1
            if same > 5:
                lost += 3 + same - 5

    # 规则二：2×2 全同色
    for row in range(count - 1):
        for col in range(count - 1):
            total = 0
            if code.is_dark(row, col):
                total += 1
            if code.is_dark(row + 1, col):
                total += 1
            if code.is_dark(row, col + 1):
                total += 1
            if code.is_dark(row + 1, col + 1):
                total += 1
            if total == 0 or total == 4:
                lost += 3

    # 规则三：横向 / 纵向出现 1:1:3:1:1 那种"像定位图案"的条
    for row in range(count):
        for col in range(count - 6):
            if (code.is_dark(row, col) and not code.is_dark(row, col + 1)
                    and code.is_dark(row, col + 2) and code.is_dark(row, col + 3)
                    and code.is_dark(row, col + 4) and not code.is_dark(row, col + 5)
                    and code.is_dark(row, col + 6)):
                lost += 40
    for col in range(count):
        for row in range(count - 6):
            if (code.is_dark(row, col) and not code.is_dark(row + 1, col)
                    and code.is_dark(row + 2, col) and code.is_dark(row + 3, col)
                    and code.is_dark(row + 4, col) and not code.is_dark(row + 5, col)
                    and code.is_dark(row + 6, col)):
                lost += 40

    # 规则四：黑白比例偏离 50% 的程度
    dark_count = sum(1 for row in range(count) for col in range(count)
                     if code.is_dark(row, col))
    ratio = abs(100 * dark_count / count / count - 50) / 5
    return lost + 10 * ratio


# ==========================================
# 🌟 编码主体
# ==========================================


class _Qr8bitByte:
    """字节模式的数据段：长度按**字节**算，写完就是 8 位一组。"""

    mode = MODE_8BIT_BYTE

    def __init__(self, data):
        self.data = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")

    def get_length(self):
        return len(self.data)

    def write(self, buffer):
        for value in self.data:
            buffer.put(value, 8)


class _QrCode:
    def __init__(self, type_number, error_correct_level):
        self.type_number = type_number if type_number else -1
        self.error_correct_level = error_correct_level
        self.modules = None
        self.module_count = 0
        self.data_cache = None
        self.data_list = []

    def add_data(self, data):
        self.data_list.append(_Qr8bitByte(data))
        self.data_cache = None

    def is_dark(self, row, col):
        if row < 0 or self.module_count <= row or col < 0 or self.module_count <= col:
            raise QrError(f"越界：({row},{col})")
        return self.modules[row][col]

    def get_module_count(self):
        return self.module_count

    def make(self):
        if self.type_number < 1:
            # 自动选版本：从小到大试，第一个装得下就停
            for type_number in range(1, 41):
                blocks = _rs_blocks(type_number, self.error_correct_level)
                data_count = sum(block.data_count for block in blocks)
                buffer = _BitBuffer()
                for item in self.data_list:
                    buffer.put(item.mode, 4)
                    buffer.put(item.get_length(),
                               _get_length_in_bits(item.mode, type_number))
                    item.write(buffer)
                if buffer.get_length_in_bits() <= 8 * data_count:
                    break
            self.type_number = type_number
        self.make_impl(False, self.get_best_mask_pattern())

    def make_impl(self, test, mask_pattern):
        self.module_count = self.type_number * 4 + 17
        self.modules = [[None] * self.module_count for _ in range(self.module_count)]

        self.setup_position_probe_pattern(0, 0)
        self.setup_position_probe_pattern(self.module_count - 7, 0)
        self.setup_position_probe_pattern(0, self.module_count - 7)
        self.setup_position_adjust_pattern()
        self.setup_timing_pattern()
        self.setup_type_info(test, mask_pattern)
        if self.type_number >= 7:
            self.setup_type_number(test)

        if self.data_cache is None:
            self.data_cache = self.create_data(self.type_number, self.error_correct_level,
                                              self.data_list)
        self.map_data(self.data_cache, mask_pattern)

    def setup_position_probe_pattern(self, row, col):
        for r in range(-1, 8):
            if row + r <= -1 or self.module_count <= row + r:
                continue
            for c in range(-1, 8):
                if col + c <= -1 or self.module_count <= col + c:
                    continue
                self.modules[row + r][col + c] = (
                    (0 <= r <= 6 and (c == 0 or c == 6))
                    or (0 <= c <= 6 and (r == 0 or r == 6))
                    or (2 <= r <= 4 and 2 <= c <= 4)
                )

    def get_best_mask_pattern(self):
        best_pattern = 0
        min_lost = 0
        for pattern in range(8):
            self.make_impl(True, pattern)
            lost = _get_lost_point(self)
            if pattern == 0 or min_lost > lost:
                min_lost = lost
                best_pattern = pattern
        return best_pattern

    def setup_timing_pattern(self):
        for index in range(8, self.module_count - 8):
            if self.modules[index][6] is None:
                self.modules[index][6] = index % 2 == 0
        for index in range(8, self.module_count - 8):
            if self.modules[6][index] is None:
                self.modules[6][index] = index % 2 == 0

    def setup_position_adjust_pattern(self):
        positions = _get_pattern_position(self.type_number)
        for row in positions:
            for col in positions:
                if self.modules[row][col] is not None:
                    continue
                for r in range(-2, 3):
                    for c in range(-2, 3):
                        self.modules[row + r][col + c] = (
                            r == -2 or r == 2 or c == -2 or c == 2 or (r == 0 and c == 0)
                        )

    def setup_type_number(self, test):
        bits = _get_bch_type_number(self.type_number)
        for index in range(18):
            dark = (not test) and ((bits >> index) & 1) == 1
            self.modules[index // 3][index % 3 + self.module_count - 8 - 3] = dark
        for index in range(18):
            dark = (not test) and ((bits >> index) & 1) == 1
            self.modules[index % 3 + self.module_count - 8 - 3][index // 3] = dark

    def setup_type_info(self, test, mask_pattern):
        bits = _get_bch_type_info(self.error_correct_level << 3 | mask_pattern)
        for index in range(15):
            dark = (not test) and ((bits >> index) & 1) == 1
            if index < 6:
                self.modules[index][8] = dark
            elif index < 8:
                self.modules[index + 1][8] = dark
            else:
                self.modules[self.module_count - 15 + index][8] = dark
        for index in range(15):
            dark = (not test) and ((bits >> index) & 1) == 1
            if index < 8:
                self.modules[8][self.module_count - index - 1] = dark
            elif index < 9:
                self.modules[8][15 - index - 1 + 1] = dark
            else:
                self.modules[8][15 - index - 1] = dark
        self.modules[self.module_count - 8][8] = not test

    def map_data(self, data, mask_pattern):
        row = self.module_count - 1
        direction = -1
        bit_index = 7
        byte_index = 0
        col = self.module_count - 1
        while col > 0:
            if col == 6:
                col -= 1
            while True:
                for offset in range(2):
                    if self.modules[row][col - offset] is None:
                        dark = False
                        if byte_index < len(data):
                            dark = ((data[byte_index] >> bit_index) & 1) == 1
                        if _get_mask(mask_pattern, row, col - offset):
                            dark = not dark
                        self.modules[row][col - offset] = dark
                        bit_index -= 1
                        if bit_index == -1:
                            byte_index += 1
                            bit_index = 7
                row += direction
                if row < 0 or self.module_count <= row:
                    row -= direction
                    direction = -direction
                    break
            col -= 2

    @staticmethod
    def create_data(type_number, error_correct_level, data_list):
        blocks = _rs_blocks(type_number, error_correct_level)
        buffer = _BitBuffer()
        for item in data_list:
            buffer.put(item.mode, 4)
            buffer.put(item.get_length(), _get_length_in_bits(item.mode, type_number))
            item.write(buffer)

        data_count = sum(block.data_count for block in blocks)
        if buffer.get_length_in_bits() > 8 * data_count:
            raise QrError(f"数据太长：需要 {buffer.get_length_in_bits()} 位，"
                          f"版本 {type_number} 只放得下 {8 * data_count} 位")

        if buffer.get_length_in_bits() + 4 <= 8 * data_count:
            buffer.put(0, 4)
        while buffer.get_length_in_bits() % 8 != 0:
            buffer.put_bit(False)
        while buffer.get_length_in_bits() < 8 * data_count:
            buffer.put(_PAD0, 8)
            if buffer.get_length_in_bits() >= 8 * data_count:
                break
            buffer.put(_PAD1, 8)

        return _QrCode.create_bytes(buffer, blocks)

    @staticmethod
    def create_bytes(buffer, blocks):
        offset = 0
        max_data_count = 0
        max_error_count = 0
        data_blocks = []
        error_blocks = []

        for block in blocks:
            data_count = block.data_count
            error_count = block.total_count - data_count
            max_data_count = max(max_data_count, data_count)
            max_error_count = max(max_error_count, error_count)
            values = [buffer.buffer[offset + index] & 0xFF for index in range(data_count)]
            data_blocks.append(values)
            offset += data_count

            polynomial = _get_error_correct_polynomial(error_count)
            # ⚠️ 移位不能省：要算的是 `数据 · x^纠错字节数` 除以生成多项式的余数。
            #    少了这个移位，纠错字节全错，二维码看着还是个二维码但扫不出来。
            remainder = _Polynomial(values, polynomial.get_length() - 1).mod(polynomial)
            errors = [0] * (polynomial.get_length() - 1)
            for index in range(len(errors)):
                shift = index + remainder.get_length() - len(errors)
                errors[index] = remainder.get(shift) if shift >= 0 else 0
            error_blocks.append(errors)

        total = sum(block.total_count for block in blocks)
        result = [0] * total
        index = 0
        for i in range(max_data_count):
            for block_index in range(len(blocks)):
                if i < len(data_blocks[block_index]):
                    result[index] = data_blocks[block_index][i]
                    index += 1
        for i in range(max_error_count):
            for block_index in range(len(blocks)):
                if i < len(error_blocks[block_index]):
                    result[index] = error_blocks[block_index][i]
                    index += 1
        return result


# ==========================================
# 🌟 对外接口
# ==========================================


def matrix(text, level="M", type_number=None):
    """把文本编码成二维码矩阵：`matrix[row][col]` 是 True/False（深/浅）。"""
    key = str(level or "M").upper()
    if key not in ECL:
        raise QrError(f"纠错级别只能是 L/M/Q/H，收到 {level!r}")
    code = _QrCode(int(type_number or 0), ECL[key])
    code.add_data(text)
    code.make()
    count = code.get_module_count()
    return [[code.is_dark(row, col) for col in range(count)] for row in range(count)]


def to_ascii(text, level="M", border=2, dark="█", light=" "):
    """渲染成终端二维码（两个模块一行，用半角字符拼）。

    用 `▀ ▄ █` 这类"半块"字符：一个字符高度里塞两行模块，图案才不至于被拉长。
    """
    cells = matrix(text, level=level)
    count = len(cells)
    width = count + border * 2
    lines = []
    for row in range(-border, count + border, 2):
        chars = []
        for col in range(-border, count + border):
            top = 0 <= row < count and 0 <= col < count and cells[row][col]
            bottom = 0 <= row + 1 < count and 0 <= col < count and cells[row + 1][col]
            if top and bottom:
                chars.append(dark)
            elif top:
                chars.append("▀")
            elif bottom:
                chars.append("▄")
            else:
                chars.append(light)
        lines.append("".join(chars))
    return "\n".join(lines)


def to_svg(text, level="M", scale=8, border=4, dark="#000000", light="#ffffff"):
    """渲染成 SVG 字符串（Studio 直接塞进 `<img>` 或页面里显示）。

    为什么是 SVG 而不是 PNG：不用图像库就能画（PNG 还得自己拼 zlib 数据），
    而且矢量的二维码放到多大都不糊。
    """
    cells = matrix(text, level=level)
    count = len(cells)
    side = count + border * 2
    parts = []
    for row in range(count):
        for col in range(count):
            if cells[row][col]:
                parts.append(f"M{col + border} {row + border}h1v1h-1z")
    path = "".join(parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{side * scale}" '
        f'height="{side * scale}" viewBox="0 0 {side} {side}" '
        f'shape-rendering="crispEdges" role="img" aria-label="登录二维码">'
        f'<rect width="{side}" height="{side}" fill="{light}"/>'
        f'<path d="{path}" fill="{dark}"/></svg>'
    )


def to_png(text, level="M", scale=8, border=4):
    """渲染成 PNG 字节（纯标准库手写 PNG：IHDR + IDAT + IEND）。

    留给"需要位图"的场合（比如以后要发到聊天软件里）。终端用 `to_ascii`、
    网页用 `to_svg` 就够了，所以这个函数只是备着。
    """
    import struct

    cells = matrix(text, level=level)
    count = len(cells)
    side = (count + border * 2) * scale

    raw = bytearray()
    for y in range(side):
        raw.append(0)                      # 每行的滤波字节（0 = 不滤波）
        row_index = y // scale - border
        for x in range(side):
            col_index = x // scale - border
            dark = (0 <= row_index < count and 0 <= col_index < count
                    and cells[row_index][col_index])
            raw.extend(b"\x00\x00\x00" if dark else b"\xff\xff\xff")

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def size_of(text, level="M", type_number=None):
    """返回矩阵边长（给调用方估算显示尺寸用）。"""
    return len(matrix(text, level=level, type_number=type_number))
