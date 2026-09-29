"""Значки, которых нет среди стандартных значков Qt."""
from __future__ import annotations

import math

from PyQt5 import QtCore, QtGui


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
