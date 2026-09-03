#!/usr/bin/env python3
"""生成启动图标 PNG —— 纯标准库，不依赖 PIL。

为什么手写：这台机器没有 PIL，也没有 Android Studio 的图标生成器。
装 PIL 要联网装轮子、而且给一个只需要五张小图的项目引一个 C 扩展依赖不值得。
PNG 的最小可用编码（真彩+Alpha、单一 IDAT、zlib deflate）大概三十行，
自己写反而更可控 —— 生成结果确定、可复现、构建脚本里一行就能跑。

画法：把图形当数学函数（点在不在鱼身里），先按 4 倍分辨率采样再做盒式降采样，
这就是最朴素的超采样抗锯齿。图形简单，性能完全够（192px 图也只有 59 万次采样）。

用法：python3 app/tools/mkicon.py app/res
"""

import os
import struct
import sys
import zlib

BG = (0x12, 0x16, 0x1C)
BODY = (0x2F, 0x6F, 0xED)
BELLY = (0x6E, 0xA2, 0xFF)
EYE = (0xE6, 0xEA, 0xF0)
PUPIL = (0x10, 0x16, 0x20)
FIN = (0x24, 0x58, 0xC0)

SS = 4  # 超采样倍数


def write_png(path, width, height, pixels):
    """pixels: [(r,g,b,a), ...] 逐行排列，长度 = width*height。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # 每行的 filter type，0 = None
        row = pixels[y * width:(y + 1) * width]
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag, data):
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def blend(dst, src, alpha):
    return tuple(int(round(d * (1 - alpha) + s * alpha)) for d, s in zip(dst, src))


def sample(u, v, full_bleed):
    """给归一化坐标 (u,v) ∈ [0,1)² 定颜色。返回 (r,g,b,a)。

    full_bleed=True 用于自适应图标的前景层：系统会自己裁形状，
    所以背景要铺满、不能自己画圆角，否则圆角会被裁两次。
    """
    # 背景：圆角方形（自适应前景层则铺满）
    inside_bg = True
    if not full_bleed:
        r = 0.22  # 圆角半径（归一化）
        dx = max(abs(u - 0.5) - (0.5 - r), 0.0)
        dy = max(abs(v - 0.5) - (0.5 - r), 0.0)
        inside_bg = (dx * dx + dy * dy) <= r * r
    if not inside_bg:
        return (0, 0, 0, 0)

    color = BG

    # 坐标换到以中心为原点、右为 +x、上为 +y
    x = (u - 0.5) * 2.0
    y = (0.5 - v) * 2.0

    # 鱼身：椭圆。占比刻意留白，图标缩到 48px 时才看得出形状。
    bx, by = 0.06, 0.02
    a, b = 0.62, 0.40
    ex = (x - bx) / a
    ey = (y - by) / b
    in_body = ex * ex + ey * ey <= 1.0

    # 尾巴：左边一个三角形（两条边的半平面交集）
    tail_tip_x = -0.92
    in_tail = False
    if x <= bx - 0.42:
        span = (bx - 0.42 - x) / (bx - 0.42 - tail_tip_x)  # 0 在身侧，1 在尾尖
        if 0.0 <= span <= 1.0:
            half = 0.10 + 0.34 * span
            in_tail = abs(y - by) <= half

    # 上鳍：身体上方的小三角
    in_fin = False
    if -0.10 <= x - bx <= 0.30:
        s = (x - bx + 0.10) / 0.40
        top = by + 0.33 + 0.20 * (1 - abs(2 * s - 1))
        if by + 0.28 <= y <= top:
            in_fin = True

    if in_tail or in_fin:
        color = FIN
    if in_body:
        color = BODY
        # 肚子：身体下半部提亮，让形状在小尺寸下也读得出来
        if ey < -0.15:
            t = min((-ey - 0.15) / 0.85, 1.0)
            color = blend(BODY, BELLY, 0.55 * t)

    # 眼睛
    if in_body:
        exx, eyy = x - (bx + 0.30), y - (by + 0.12)
        if exx * exx + eyy * eyy <= 0.105 * 0.105:
            color = EYE
        if exx * exx + eyy * eyy <= 0.050 * 0.050:
            color = PUPIL

    return (color[0], color[1], color[2], 255)


def render(size, full_bleed=False):
    """超采样渲染一张 size×size 的图。"""
    big = size * SS
    inv = 1.0 / big
    out = []
    for y in range(size):
        for x in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                vy = (y * SS + sy + 0.5) * inv
                for sx in range(SS):
                    vx = (x * SS + sx + 0.5) * inv
                    r, g, b, al = sample(vx, vy, full_bleed)
                    # 预乘 alpha 再平均，否则透明边缘会带黑边
                    acc[0] += r * al
                    acc[1] += g * al
                    acc[2] += b * al
                    acc[3] += al
            n = SS * SS
            a = acc[3] / n
            if a <= 0.5:
                # 这一格几乎全透明（圆角外侧），直接写全透明，
                # 避免下面除以 acc[3] 时出现 0 除。
                out.append((0, 0, 0, 0))
            else:
                # 预乘的颜色要除回 alpha 总和还原
                out.append((int(round(acc[0] / acc[3])),
                            int(round(acc[1] / acc[3])),
                            int(round(acc[2] / acc[3])),
                            int(round(a))))
    return out


# 各密度桶的启动图标边长（px）。48dp 是启动图标的标准尺寸。
DENSITIES = [
    ("mdpi", 48),
    ("hdpi", 72),
    ("xhdpi", 96),
    ("xxhdpi", 144),
    ("xxxhdpi", 192),
]

# 自适应图标前景层：108dp 画布，但系统只保证中间 72dp 可见，
# 所以图形必须留出四周 18dp 的安全边距 —— 这里用 full_bleed 渲染后
# 靠 XML 的 inset 处理，前景图本身按 108dp 各密度出图。
FOREGROUND = [
    ("mdpi", 108),
    ("hdpi", 162),
    ("xhdpi", 216),
    ("xxhdpi", 324),
    ("xxxhdpi", 432),
]


def main():
    res = sys.argv[1] if len(sys.argv) > 1 else "app/res"
    made = []
    for bucket, size in DENSITIES:
        d = os.path.join(res, "mipmap-" + bucket)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "ic_launcher.png")
        write_png(p, size, size, render(size))
        made.append((p, size))
    for bucket, size in FOREGROUND:
        d = os.path.join(res, "mipmap-" + bucket)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "ic_launcher_foreground.png")
        write_png(p, size, size, render(size, full_bleed=True))
        made.append((p, size))
    for p, size in made:
        print("%4dpx  %s  (%d 字节)" % (size, p, os.path.getsize(p)))


if __name__ == "__main__":
    main()
