"""Вкладка «Строки ПЗС»: спектр каждой строки кадра и их среднее.

Отдельные строки есть, только если камера их не суммировала: в режиме «Изображение» (весь кадр)
и в режиме «Спектр» без аппаратного объединения строк (строки полос). При аппаратном объединении
камера складывает строки полосы до оцифровки — спектров отдельных строк физически нет.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets

from .plots import curve_pen, hide_transforms

MAX_CURVES = 100          # больше кривых на графике не различить — показывается каждая k-я строка


class RowsPanel(QtWidgets.QWidget):
    save_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: #555; padding: 2px 6px;")
        lay.addWidget(self.info)

        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(6, 0, 6, 0)
        self.row_from = QtWidgets.QSpinBox()
        self.row_to = QtWidgets.QSpinBox()
        for w in (self.row_from, self.row_to):
            w.setKeyboardTracking(False)
        self.btn_stripes = QtWidgets.QToolButton()
        self.btn_stripes.setText("полосы")
        self.btn_stripes.setToolTip("Строки полос (как на вкладке «Спектр»)")
        self.btn_all = QtWidgets.QToolButton()
        self.btn_all.setText("все")
        self.btn_all.setToolTip("Все строки кадра")
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("среднее и выбранная строка", "one")
        self.mode.addItem("все строки", "all")
        self.mode.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.row = QtWidgets.QSpinBox()
        self.row.setKeyboardTracking(False)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.dark = QtWidgets.QCheckBox("вычесть темновой фон")
        self.dark.setChecked(True)
        self.dark.setToolTip("Темновой кадр тех же строк — если при съёмке фон вычитался")
        self.btn_save = QtWidgets.QPushButton("Сохранить строки…")
        self.btn_save.setToolTip("Спектры всех выбранных строк и их среднее — столбцами в TXT/CSV")
        for w in (QtWidgets.QLabel("Строки ПЗС:"), self.row_from, QtWidgets.QLabel("…"), self.row_to,
                  self.btn_stripes, self.btn_all, QtWidgets.QLabel("  Показать:"), self.mode,
                  QtWidgets.QLabel("  Строка:"), self.row):
            bar.addWidget(w)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.dark)
        bar.addWidget(self.btn_save)
        lay.addLayout(bar)

        self.plot = pg.PlotWidget(background="w")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("left", "Интенсивность одной строки, отсч.")
        hide_transforms(self.plot.getPlotItem())
        self.legend = self.plot.addLegend(offset=(-10, 10))
        lay.addWidget(self.plot, 1)

        self._m = None
        self._x: Optional[np.ndarray] = None
        self._x_label = ""
        self._stripes: Tuple[int, int] = (0, 0)
        self._frame_key = None
        self._curves = []
        self.row_from.valueChanged.connect(self._range_changed)
        self.row_to.valueChanged.connect(self._range_changed)
        self.row.valueChanged.connect(self.slider.setValue)
        self.slider.valueChanged.connect(self.row.setValue)
        self.row.valueChanged.connect(lambda _: self._draw())
        self.mode.currentIndexChanged.connect(lambda _: self._draw())
        self.dark.toggled.connect(lambda _: self._draw())
        self.btn_stripes.clicked.connect(lambda: self._set_range(*self._stripes))
        self.btn_all.clicked.connect(self._all_rows)
        self.btn_save.clicked.connect(self.save_requested.emit)
        self.set_frame(None, why="Снимков ещё нет.")

    # ---------------------------------------------------------------- данные
    def set_frame(self, m, x: Optional[np.ndarray] = None, x_label: str = "", stripes=None,
                  note: str = "", why: str = "") -> None:
        """m — измерение с кадром (m.image, m.image_top); x — ось столбцов в единицах графика (или None);
        stripes — (верх, низ) полос по умолчанию; why — почему строк нет (если m None)."""
        has = m is not None and m.image is not None and np.asarray(m.image).ndim == 2
        self._m = m if has else None
        for w in (self.row_from, self.row_to, self.btn_stripes, self.btn_all, self.mode, self.row, self.slider,
                  self.btn_save):
            w.setEnabled(has)
        if not has:
            self.info.setText(why)
            self._clear()
            return
        img = np.asarray(m.image)
        top, n = int(m.image_top), img.shape[0]
        self._x = x if x is not None and len(x) == img.shape[1] else None
        self._x_label = x_label if self._x is not None else "Столбец ПЗС (пиксель)"
        lo, hi = top, top + n - 1
        st = stripes or (lo, hi)
        self._stripes = (max(lo, min(st)), min(hi, max(st)))
        if self._stripes[0] > self._stripes[1]:
            self._stripes = (lo, hi)
        self.dark.setEnabled(m.image_dark is not None)
        key = (top, n)
        for w in (self.row_from, self.row_to, self.row, self.slider):
            w.blockSignals(True)
            w.setRange(lo, hi)
            w.blockSignals(False)
        if key != self._frame_key:                   # другой кадр — строки по умолчанию: полосы
            self._frame_key = key
            self._set_range(*self._stripes, draw=False)
        parts = [f"Кадр от {m.timestamp:%H:%M:%S}: строки ПЗС {lo}–{hi} ({n} шт.)"]
        if m.acq_mode != "image":
            parts.append("в режиме «Спектр» без аппаратного объединения считываются только строки полос")
        if m.accumulations > 1:
            parts.append(f"{'сумма' if m.accumulate_mode == 'sum' else 'среднее'} {m.accumulations} кадров")
        parts.append("на вкладке «Спектр» — сумма строк полосы, здесь — среднее по строкам")
        self.info.setText(" · ".join(parts) + (f"\n{note}" if note else ""))
        self._draw()

    def _set_range(self, a: int, b: int, draw: bool = True) -> None:
        for w in (self.row_from, self.row_to):
            w.blockSignals(True)
        self.row_from.setValue(a)
        self.row_to.setValue(b)
        for w in (self.row_from, self.row_to):
            w.blockSignals(False)
        self._range_changed(draw=draw)

    def _all_rows(self) -> None:
        if self._m is not None:
            top = int(self._m.image_top)
            self._set_range(top, top + np.asarray(self._m.image).shape[0] - 1)

    def _range_changed(self, *_args, draw: bool = True) -> None:
        a, b = sorted((self.row_from.value(), self.row_to.value()))
        for w in (self.row, self.slider):
            w.blockSignals(True)
            w.setRange(a, b)
            w.blockSignals(False)
        if not a <= self.row.value() <= b:
            self.row.setValue((a + b) // 2)
        if draw:
            self._draw()

    def selected(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Tuple[int, int]]:
        """(ось x, строки выбранного диапазона (с вычтенным фоном, если отмечено), их среднее, (верх, низ))."""
        m = self._m
        img = np.asarray(m.image, dtype=np.float64)
        top = int(m.image_top)
        a, b = sorted((self.row_from.value(), self.row_to.value()))
        rows = img[a - top:b - top + 1]
        if self.dark.isChecked() and self.dark.isEnabled() and m.image_dark is not None:
            k = m.accumulations if m.accumulate_mode == "sum" else 1
            rows = rows - np.asarray(m.image_dark, dtype=np.float64)[a - top:b - top + 1] * k
        x = self._x if self._x is not None else np.arange(img.shape[1], dtype=float)
        return x, rows, rows.mean(axis=0), (a, b)

    # ---------------------------------------------------------------- рисование
    def _clear(self) -> None:
        for c in self._curves:
            self.plot.removeItem(c)
        self._curves = []
        self.legend.clear()

    def _draw(self) -> None:
        self._clear()
        if self._m is None:
            return
        one = self.mode.currentData() != "all"
        self.row.setEnabled(one)                     # в режиме «все строки» выбор строки не нужен
        self.slider.setEnabled(one)
        x, rows, mean, (a, b) = self.selected()
        self.plot.setLabel("bottom", self._x_label)
        if self.mode.currentData() == "all":
            step = max(1, math.ceil(rows.shape[0] / MAX_CURVES))
            cmap = pg.colormap.get("viridis")
            idx = list(range(0, rows.shape[0], step))
            for k, i in enumerate(idx):
                color = cmap.map(k / max(1, len(idx) - 1), mode="qcolor")
                c = self.plot.plot(x, rows[i], pen=curve_pen(color))
                self._curves.append(c)
            label = f"строки {a}–{b}" + (f" (каждая {step}-я)" if step > 1 else "")
            self._curves.append(self.plot.plot([], [], pen=curve_pen("#3b528b"), name=label))
        else:
            r = self.row.value()
            self._curves.append(self.plot.plot(x, rows[r - a], pen=curve_pen("#d62728"), name=f"строка {r}"))
        mean_curve = self.plot.plot(x, mean, pen=curve_pen("k"), name=f"среднее по строкам {a}–{b}")
        mean_curve.setZValue(5)                      # среднее — поверх строк
        self._curves.append(mean_curve)
