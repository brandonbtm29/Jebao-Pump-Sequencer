"""Draw the app icon and save it as assets/jebao.png, .ico and .icns.

Run from the repo root: python scripts/make_icon.py
Needs PyQt6 (an app dependency) and Pillow (only for this script):
pip install pillow
"""

import os
import sys

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
SIZE = 1024


def draw(size=SIZE):
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 1024

    # Rounded-square tile with a teal-to-blue gradient
    tile = QRectF(64 * s, 64 * s, 896 * s, 896 * s)
    grad = QLinearGradient(tile.topLeft(), tile.bottomRight())
    grad.setColorAt(0.0, QColor("#1FB5A8"))
    grad.setColorAt(1.0, QColor("#1456A8"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(tile, 200 * s, 200 * s)

    # A drop of liquid
    cx, top, bottom, r = 512 * s, 168 * s, 640 * s, 178 * s
    drop = QPainterPath()
    drop.moveTo(cx, top)
    drop.cubicTo(
        QPointF(cx + 40 * s, top + 120 * s), QPointF(cx + r, bottom - 1.45 * r), QPointF(cx + r, bottom - r)
    )
    drop.arcTo(QRectF(cx - r, bottom - 2 * r, 2 * r, 2 * r), 0, -180)
    drop.cubicTo(QPointF(cx - r, bottom - 1.45 * r), QPointF(cx - 40 * s, top + 120 * s), QPointF(cx, top))
    p.setBrush(QColor("#FFFFFF"))
    p.drawPath(drop)

    # Highlight on the drop
    p.setBrush(QColor(31, 181, 168, 110))
    p.drawEllipse(QRectF(cx - 105 * s, bottom - 205 * s, 62 * s, 100 * s))

    # Four channels: bars of different heights, like four pump heads dosing
    heights = [120, 190, 150, 220]
    width, gap, base = 92 * s, 40 * s, 900 * s
    x = cx - (4 * width + 3 * gap) / 2
    for h in heights:
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawRoundedRect(QRectF(x, base - h * s, width, h * s), 26 * s, 26 * s)
        x += width + gap
    p.end()
    return img


def main():
    from PIL import Image

    QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    os.makedirs(ASSETS, exist_ok=True)
    png = os.path.join(ASSETS, "jebao.png")
    draw().save(png, "PNG")
    with Image.open(png) as im:
        small = im.resize((512, 512), Image.Resampling.LANCZOS)
        small.save(png)
        im.save(
            os.path.join(ASSETS, "jebao.ico"),
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
        im.save(os.path.join(ASSETS, "jebao.icns"))
    print("Wrote", ", ".join(sorted(os.listdir(ASSETS))))


if __name__ == "__main__":
    main()
