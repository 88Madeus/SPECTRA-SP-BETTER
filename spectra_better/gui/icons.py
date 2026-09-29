"""Значки, которых нет среди стандартных значков Qt, и значок программы."""
from __future__ import annotations

import math
import struct
from typing import Iterable

from PyQt5 import QtCore, QtGui

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def gear_icon(color: str = "#4a4a4a", size: int = 64) -> QtGui.QIcon:
    """Шестерёнка, нарисованная программно (не зависит от шрифтов системы)."""
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    c = size / 2.0
    teeth = 8
    r_out, r_in, r_hole = size * 0.47, size * 0.36, size * 0.15
    path = QtGui.QPainterPath()
    for k in range(teeth * 4):
        a = 2 * math.pi * k / (teeth * 4) - math.pi / (teeth * 4)
        r = r_out if (k % 4) in (1, 2) else r_in
        pt = QtCore.QPointF(c + r * math.cos(a), c + r * math.sin(a))
        path.moveTo(pt) if k == 0 else path.lineTo(pt)
    path.closeSubpath()
    hole = QtGui.QPainterPath()
    hole.addEllipse(QtCore.QPointF(c, c), r_hole, r_hole)
    p.fillPath(path.subtracted(hole), QtGui.QColor(color))
    p.end()
    return QtGui.QIcon(pm)


def app_icon_image(size: int) -> QtGui.QImage:
    """Значок программы: радужная полоса спектра и линии спектра над ней."""
    img = QtGui.QImage(size, size, QtGui.QImage.Format_ARGB32_Premultiplied)
    img.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    s = float(size)
    m = s * 0.03
    bg = QtGui.QLinearGradient(0, 0, 0, s)
    bg.setColorAt(0, QtGui.QColor("#24384d"))
    bg.setColorAt(1, QtGui.QColor("#0e1823"))
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QBrush(bg))
    p.drawRoundedRect(QtCore.QRectF(m, m, s - 2 * m, s - 2 * m), s * 0.18, s * 0.18)

    left, right = s * 0.14, s * 0.86
    band = QtCore.QRectF(left, s * 0.70, right - left, s * 0.13)
    rainbow = QtGui.QLinearGradient(band.left(), 0, band.right(), 0)
    for pos, col in ((0.0, "#7b2cbf"), (0.17, "#3a57e8"), (0.33, "#16b7e0"), (0.5, "#2fbf4a"),
                     (0.67, "#f4d31b"), (0.83, "#f7811e"), (1.0, "#e0262b")):
        rainbow.setColorAt(pos, QtGui.QColor(col))
    p.setBrush(QtGui.QBrush(rainbow))
    p.drawRoundedRect(band, s * 0.02, s * 0.02)

    # спектр: несколько линий разной высоты
    base, top = s * 0.62, s * 0.17
    peaks = ((0.22, 0.45, 0.035), (0.42, 1.0, 0.03), (0.58, 0.35, 0.04), (0.76, 0.7, 0.03))
    path = QtGui.QPainterPath()
    n = max(48, size)
    for i in range(n + 1):
        t = i / n
        y = sum(h * math.exp(-((t - c) / w) ** 2) for c, h, w in peaks)
        pt = QtCore.QPointF(left + t * (right - left), base - y * (base - top))
        path.moveTo(pt) if i == 0 else path.lineTo(pt)
    pen = QtGui.QPen(QtGui.QColor("#ffffff"), max(1.2, s * 0.035))
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(QtCore.Qt.NoBrush)
    p.drawPath(path)
    p.end()
    return img


def app_icon() -> QtGui.QIcon:
    icon = QtGui.QIcon()
    for size in ICO_SIZES:
        icon.addPixmap(QtGui.QPixmap.fromImage(app_icon_image(size)))
    return icon


def _png_bytes(img: QtGui.QImage) -> bytes:
    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def ico_bytes(images: Iterable[QtGui.QImage]) -> bytes:
    """Файл .ico из картинок (внутри — PNG, Windows Vista и новее)."""
    pngs = [(img.width(), _png_bytes(img)) for img in images]
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, data = b"", b""
    for size, png in pngs:
        s = 0 if size >= 256 else size          # 0 означает 256
        entries += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return head + entries + data


def write_app_ico(path: str) -> None:
    with open(path, "wb") as f:
        f.write(ico_bytes(app_icon_image(s) for s in ICO_SIZES))
