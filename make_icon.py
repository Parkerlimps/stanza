"""生成 Stanza 图标 assets/stanza.ico。

用 Qt 渲染（和界面同一套抗锯齿），Pillow 负责封装多尺寸 ICO。
小尺寸去掉底部的章节刻度，只留播放三角，否则 16px 下糊成一团。

用法：python make_icon.py
"""
import sys
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "assets"
ICO = OUT_DIR / "stanza.ico"

BG = QColor("#16171b")
FG = QColor("#ffffff")

SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)

    # 圆角方块底
    m = size * 0.035
    r = QRectF(m, m, size - 2 * m, size - 2 * m)
    p.setPen(Qt.NoPen)
    p.setBrush(BG)
    p.drawRoundedRect(r, size * 0.22, size * 0.22)

    detailed = size >= 48

    # 播放三角：光学居中，略右偏
    h = size * (0.40 if detailed else 0.46)
    w = h * 0.86
    cy = size * (0.455 if detailed else 0.5)
    cx = size * 0.5 + size * 0.035
    tri = QPainterPath()
    tri.moveTo(cx - w / 2, cy - h / 2)
    tri.lineTo(cx + w / 2, cy)
    tri.lineTo(cx - w / 2, cy + h / 2)
    tri.closeSubpath()
    p.setBrush(FG)
    p.drawPath(tri)

    if detailed:
        # 底部章节刻度：三段，表示自动分段
        bar_h = max(1.5, size * 0.055)
        y = size * 0.745
        x0, x1 = size * 0.28, size * 0.72
        gap = size * 0.045
        seg = (x1 - x0 - 2 * gap) / 3
        for i in range(3):
            bx = x0 + i * (seg + gap)
            p.drawRoundedRect(QRectF(bx, y, seg, bar_h), bar_h / 2, bar_h / 2)

    p.end()
    return img


def main():
    app = QApplication(sys.argv)   # noqa: F841  QPainter 需要 GUI 上下文
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    imgs = [draw(s) for s in SIZES]

    # QImage -> PIL，交给 Pillow 封装成标准多尺寸 ICO
    from PIL import Image

    pil = []
    for img in imgs:
        img = img.convertToFormat(QImage.Format_RGBA8888)
        ptr = img.constBits()
        data = bytes(ptr)
        pil.append(Image.frombytes("RGBA", (img.width(), img.height()), data))

    base = pil[-1]                       # 256
    base.save(ICO, format="ICO",
              sizes=[(s, s) for s in SIZES],
              append_images=pil[:-1])
    print(f"已生成 {ICO} ({ICO.stat().st_size} 字节)")

    # 顺手拼一张预览图，方便肉眼检查
    pad, cell = 12, 160
    sheet = Image.new("RGBA", (cell * len(SIZES) + pad * 2, cell + pad * 2), (255, 255, 255, 255))
    x = pad
    for im in pil:
        big = im.resize((cell - 24, cell - 24), Image.NEAREST)
        sheet.alpha_composite(big, (x, pad + 12))
        x += cell
    sheet.save(OUT_DIR / "_preview_sheet.png")
    print("预览图 assets/_preview_sheet.png")


if __name__ == "__main__":
    main()
