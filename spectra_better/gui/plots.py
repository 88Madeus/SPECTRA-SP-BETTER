"""Графики: спектр (с наложениями, курсором и пиками) и изображение ПЗС."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets

OVERLAY_COLORS = ["#1f77b4", "#2ca02c", "#9467bd", "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f"]


class SpectrumPlot(QtWidgets.QWidget):
    cursor_moved = QtCore.pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("left", "Интенсивность, отсч.")
        self.legend = self.plot.addLegend(offset=(-10, 10))
        lay.addWidget(self.plot)
        self.readout = QtWidgets.QLabel(" ")
        self.readout.setStyleSheet("color: #444; padding: 2px 6px;")
        lay.addWidget(self.readout)

        self.curve = self.plot.plot([], [], pen=pg.mkPen("#d62728", width=1.4), name="Текущий")
        self.bg_curve = self.plot.plot([], [], pen=pg.mkPen("#888888", width=1, style=QtCore.Qt.DashLine))
        self.bg_curve.setVisible(False)
        self._bg_in_legend = False
        self.overlays: Dict[str, pg.PlotDataItem] = {}
        self.peak_items: List[pg.GraphicsObject] = []
        self._markers: List[pg.InfiniteLine] = []

        self.vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#999", width=1))
        self.hline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#999", width=1))
        self.plot.addItem(self.vline, ignoreBounds=True)
        self.plot.addItem(self.hline, ignoreBounds=True)
        self._proxy = pg.SignalProxy(self.plot.scene().sigMouseMoved, rateLimit=30, slot=self._mouse_moved)
        self._x = np.array([])
        self._y = np.array([])
        self._x_label = ""

    # ------------------------------------------------------------------ данные
    def set_x_label(self, text: str) -> None:
        self._x_label = text
        self.plot.setLabel("bottom", text)

    def set_data(self, x: np.ndarray, y: np.ndarray, name: str = "Текущий") -> None:
        self._x, self._y = np.asarray(x), np.asarray(y)
        self.curve.setData(self._x, self._y)
        self.legend.removeItem(self.curve)
        self.legend.addItem(self.curve, name)

    def set_background(self, x: Optional[np.ndarray], y: Optional[np.ndarray], visible: bool) -> None:
        if x is None or y is None:
            self.bg_curve.setData([], [])
            visible = False
        else:
            self.bg_curve.setData(x, y)
        self.bg_curve.setVisible(visible)
        if visible and not self._bg_in_legend:
            self.legend.addItem(self.bg_curve, "Фон")
            self._bg_in_legend = True
        elif not visible and self._bg_in_legend:
            self.legend.removeItem(self.bg_curve)
            self._bg_in_legend = False

    def add_overlay(self, key: str, x: np.ndarray, y: np.ndarray, name: str) -> None:
        self.remove_overlay(key)
        color = OVERLAY_COLORS[len(self.overlays) % len(OVERLAY_COLORS)]
        self.overlays[key] = self.plot.plot(x, y, pen=pg.mkPen(color, width=1.1), name=name)

    def update_overlay(self, key: str, x: np.ndarray, y: np.ndarray) -> None:
        if key in self.overlays:
            self.overlays[key].setData(x, y)

    def remove_overlay(self, key: str) -> None:
        item = self.overlays.pop(key, None)
        if item is not None:
            self.plot.removeItem(item)
            self.legend.removeItem(item)

    def clear_overlays(self) -> None:
        for k in list(self.overlays):
            self.remove_overlay(k)

    def show_peaks(self, xs: Sequence[float], ys: Sequence[float], labels: Sequence[str]) -> None:
        for it in self.peak_items:
            self.plot.removeItem(it)
        self.peak_items = []
        if not len(xs):
            return
        sc = pg.ScatterPlotItem(list(xs), list(ys), symbol="t", size=9, brush=pg.mkBrush("#ff7f0e"), pen=None)
        self.plot.addItem(sc)
        self.peak_items.append(sc)
        for x, y, t in zip(xs, ys, labels):
            ti = pg.TextItem(t, color="#b35900", anchor=(0.5, 1.2))
            ti.setPos(x, y)
            self.plot.addItem(ti)
            self.peak_items.append(ti)

    def auto_range(self) -> None:
        self.plot.enableAutoRange()

    # ------------------------------------------------------------------ курсор
    def _mouse_moved(self, evt) -> None:
        pos = evt[0]
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        p = self.plot.getPlotItem().vb.mapSceneToView(pos)
        self.vline.setPos(p.x())
        self.hline.setPos(p.y())
        text = f"x = {p.x():.4f}   y = {p.y():.1f}"
        if self._x.size:
            i = int(np.argmin(np.abs(self._x - p.x())))
            text += f"      ближайшая точка: x = {self._x[i]:.4f}, I = {self._y[i]:.1f} (пиксель {i})"
        self.readout.setText(text)
        self.cursor_moved.emit(p.x(), p.y())


class ImagePanel(QtWidgets.QWidget):
    """Кадр ПЗС и выбор полосы строк для спектра."""
    rows_changed = QtCore.pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = pg.ImageView()
        self.view.ui.roiBtn.hide()
        self.view.ui.menuBtn.hide()
        self.view.setPredefinedGradient("viridis")
        self.view.getView().setAspectLocked(False)
        lay.addWidget(self.view)
        hint = QtWidgets.QLabel("Перетащите жёлтую полосу, чтобы выбрать строки ПЗС, по которым строится спектр.")
        hint.setStyleSheet("color: #555; padding: 2px 6px;")
        lay.addWidget(hint)
        self.region = pg.LinearRegionItem(values=(280, 310), orientation="horizontal",
                                          brush=pg.mkBrush(255, 220, 0, 50),
                                          pen=pg.mkPen("#e6b800", width=1.5))
        self.view.getView().addItem(self.region)
        self.region.sigRegionChangeFinished.connect(self._region_changed)
        self._first = True
        self._suppress = False

    def set_image(self, image: np.ndarray) -> None:
        # ImageView ожидает (x, y): транспонируем, чтобы столбцы ПЗС шли по горизонтали
        img = np.asarray(image, dtype=np.float32).T
        self.view.setImage(img, autoLevels=self._first, autoRange=self._first)
        self._first = False

    def set_rows(self, top: int, bottom: int) -> None:
        self._suppress = True
        self.region.setRegion((top, bottom + 1))
        self._suppress = False

    def _region_changed(self) -> None:
        if self._suppress:
            return
        a, b = self.region.getRegion()
        top = int(round(min(a, b)))
        bottom = int(round(max(a, b))) - 1
        self.rows_changed.emit(max(0, top), max(top, bottom))
