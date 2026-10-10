"""Интегрирование в полосах (как «Stripe Integration» SpectraSP) и история во времени
(заменяет «Peak Tracer»: положение и интенсивность линии от снимка к снимку)."""
from __future__ import annotations

import csv
import datetime as _dt
import os
from collections import deque
from typing import Deque, List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from ..config import AppSettings
from ..processing import BandResult
from .plots import BAND_COLORS

UNIT_TITLES = {"nm": "нм", "px": "пикс."}
# величины для истории: (ключ BandResult, подпись)
QUANTITIES = [("net", "Σ над базовой линией, отсч."), ("total", "Σ, отсч."), ("peak", "Максимум, отсч."),
              ("centroid", "Центр линии"), ("peak_x", "Положение максимума")]
MAX_HISTORY = 20000


class BandHistory:
    """Результаты полос по снимкам: [(время, [BandResult | None, …]), …]."""

    def __init__(self) -> None:
        self.points: Deque[Tuple[_dt.datetime, List[Optional[BandResult]]]] = deque(maxlen=MAX_HISTORY)

    def add(self, when: _dt.datetime, results: Sequence[Optional[BandResult]]) -> None:
        self.points.append((when, list(results)))

    def clear(self) -> None:
        self.points.clear()

    def series(self, band: int, quantity: str) -> Tuple[np.ndarray, np.ndarray]:
        """(секунды от первой точки, значения) для полосы band."""
        if not self.points:
            return np.empty(0), np.empty(0)
        t0 = self.points[0][0]
        ts, vs = [], []
        for when, res in self.points:
            r = res[band] if band < len(res) else None
            if r is not None:
                ts.append((when - t0).total_seconds())
                vs.append(getattr(r, quantity))
        return np.asarray(ts, dtype=float), np.asarray(vs, dtype=float)

    def save_csv(self, path: str, unit: str) -> None:
        n = max((len(r) for _, r in self.points), default=0)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            head = ["time", "seconds"]
            for i in range(n):
                head += [f"band{i + 1}_net", f"band{i + 1}_total", f"band{i + 1}_peak",
                         f"band{i + 1}_centroid_{unit}", f"band{i + 1}_peak_pos_{unit}"]
            w.writerow(head)
            t0 = self.points[0][0] if self.points else None
            for when, res in self.points:
                row = [when.isoformat(timespec="milliseconds"), f"{(when - t0).total_seconds():.3f}"]
                for i in range(n):
                    r = res[i] if i < len(res) else None
                    row += ([f"{r.net:.6g}", f"{r.total:.6g}", f"{r.peak:.6g}", f"{r.centroid:.6f}",
                             f"{r.peak_x:.6f}"] if r is not None else [""] * 5)
                w.writerow(row)


class BandsPanel(QtWidgets.QWidget):
    """Таблица полос интегрирования и их результатов для текущего спектра."""
    changed = QtCore.pyqtSignal()           # полосы изменены (добавлены, удалены, отредактированы)
    add_requested = QtCore.pyqtSignal()
    history_requested = QtCore.pyqtSignal()
    track_changed = QtCore.pyqtSignal(int)

    COLS = ["∫", "от", "до", "Σ над базой", "Σ", "Максимум", "Центр"]

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.s = settings
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        hint = QtWidgets.QLabel("Сумма отсчётов в участках спектра. Участок можно перетащить на графике "
                                "или ввести границы в таблице. «Σ над базой» — за вычетом прямой между "
                                "краями участка (площадь линии).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")
        lay.addWidget(hint)
        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        hh.setStretchLastSection(True)
        lay.addWidget(self.table, 1)
        row = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("Добавить")
        self.btn_add.setToolTip("Новый участок в середине видимой части графика")
        self.btn_del = QtWidgets.QPushButton("Удалить")
        self.btn_copy = QtWidgets.QPushButton("Копировать")
        self.btn_copy.setToolTip("Скопировать таблицу (для вставки в Excel)")
        self.btn_hist = QtWidgets.QPushButton("История…")
        self.btn_hist.setToolTip("Результаты участков от снимка к снимку (непрерывная съёмка, серия): "
                                 "интенсивность и положение линии во времени")
        for b in (self.btn_add, self.btn_del, self.btn_copy, self.btn_hist):
            row.addWidget(b)
        lay.addLayout(row)
        tr = QtWidgets.QHBoxLayout()
        self.track = QtWidgets.QComboBox()
        self.track.addItem("полоса строк 1", 0)
        self.track.setToolTip("По какой полосе строк ПЗС считать (если их несколько)")
        tr.addWidget(QtWidgets.QLabel("Спектр:"))
        tr.addWidget(self.track, 1)
        lay.addLayout(tr)
        self.btn_add.clicked.connect(self.add_requested.emit)
        self.btn_del.clicked.connect(self._delete)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_hist.clicked.connect(self.history_requested.emit)
        self.table.itemChanged.connect(self._edited)
        self.track.currentIndexChanged.connect(lambda _: self.track_changed.emit(self.current_track()))
        self._filling = False
        self.show_results([])

    def current_track(self) -> int:
        return int(self.track.currentData() or 0)

    def set_track_count(self, n: int) -> None:
        if self.track.count() == max(1, n):
            return
        cur = self.current_track()
        self.track.blockSignals(True)
        self.track.clear()
        for i in range(max(1, n)):
            self.track.addItem(f"полоса строк {i + 1}", i)
        self.track.setCurrentIndex(min(cur, self.track.count() - 1))
        self.track.blockSignals(False)

    def show_results(self, results: Sequence[Optional[BandResult]]) -> None:
        self._filling = True
        bands = self.s.bands
        self.table.setRowCount(len(bands))
        for i, b in enumerate(bands):
            unit = UNIT_TITLES.get(b.get("unit"), "")
            r = results[i] if i < len(results) else None
            dec = 3 if b.get("unit") == "nm" else 1
            cells = [f"{i + 1}", f"{b['lo']:.{dec}f}", f"{b['hi']:.{dec}f}"]
            if r is None:
                cells += ["—", "—", "—", "—"]
            else:
                cells += [f"{r.net:.0f}", f"{r.total:.0f}", f"{r.peak:.0f} @ {r.peak_x:.{dec}f}",
                          f"{r.centroid:.{dec + 1}f} {unit}"]
            for col, text in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(text)
                if col in (1, 2):
                    it.setToolTip(f"граница участка, {unit}")
                else:
                    it.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
                if col == 0:
                    it.setForeground(QtGui.QColor(BAND_COLORS[i % len(BAND_COLORS)]))
                self.table.setItem(i, col, it)
        self.btn_del.setEnabled(bool(bands))
        self.btn_copy.setEnabled(bool(bands))
        self._filling = False

    def _edited(self, item: QtWidgets.QTableWidgetItem) -> None:
        if self._filling or item.column() not in (1, 2):
            return
        i = item.row()
        try:
            v = float(item.text().replace(",", "."))
        except ValueError:
            self.changed.emit()                      # перерисовать прежнее значение
            return
        b = self.s.bands[i]
        b["lo" if item.column() == 1 else "hi"] = v
        if b["lo"] > b["hi"]:
            b["lo"], b["hi"] = b["hi"], b["lo"]
        self.changed.emit()

    def _delete(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.s.bands:
            rows = [len(self.s.bands) - 1]
        for r in rows:
            del self.s.bands[r]
        self.changed.emit()

    def _copy(self) -> None:
        lines = ["\t".join(self.COLS)]
        for r in range(self.table.rowCount()):
            lines.append("\t".join(self.table.item(r, c).text() if self.table.item(r, c) else ""
                                   for c in range(self.table.columnCount())))
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))


class BandsHistoryWindow(QtWidgets.QDialog):
    """История результатов полос по снимкам: график и сохранение в CSV."""

    def __init__(self, history: BandHistory, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.history = history
        self.s = settings
        self.setWindowTitle("История участков интегрирования")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMinMaxButtonsHint)
        lay = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        self.quantity = QtWidgets.QComboBox()
        for key, title in QUANTITIES:
            self.quantity.addItem(title, key)
        self.info = QtWidgets.QLabel("")
        top.addWidget(QtWidgets.QLabel("Величина:"))
        top.addWidget(self.quantity)
        top.addStretch(1)
        top.addWidget(self.info)
        lay.addLayout(top)
        self.plot = pg.PlotWidget(background="w")
        self.plot.setLabel("bottom", "Время от первого снимка, с")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.addLegend()
        lay.addWidget(self.plot, 1)
        bar = QtWidgets.QHBoxLayout()
        self.btn_clear = QtWidgets.QPushButton("Очистить")
        self.btn_save = QtWidgets.QPushButton("Сохранить CSV…")
        bar.addWidget(self.btn_clear)
        bar.addWidget(self.btn_save)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.curves: List[pg.PlotDataItem] = []
        self.quantity.currentIndexChanged.connect(self.refresh)
        self.btn_clear.clicked.connect(self._clear)
        self.btn_save.clicked.connect(self._save)
        self.resize(760, 460)
        self.refresh()

    def refresh(self) -> None:
        key = self.quantity.currentData()
        n = len(self.s.bands)
        while len(self.curves) > n:
            self.plot.removeItem(self.curves.pop())
        while len(self.curves) < n:
            i = len(self.curves)
            self.curves.append(self.plot.plot([], [], pen=pg.mkPen(BAND_COLORS[i % len(BAND_COLORS)], width=1.4),
                                              symbol="o", symbolSize=4, name=f"∫{i + 1}"))
        for i, c in enumerate(self.curves):
            t, v = self.history.series(i, key)
            c.setData(t, v)
        self.plot.setLabel("left", dict(QUANTITIES)[key])
        self.info.setText(f"снимков: {len(self.history.points)}")

    def _clear(self) -> None:
        self.history.clear()
        self.refresh()

    def _save(self) -> None:
        if not self.history.points:
            return
        start = os.path.join(self.s.save_dir or os.path.expanduser("~"),
                             f"integration_{_dt.datetime.now():%Y%m%d_%H%M%S}.csv")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Сохранить историю", start, "CSV (*.csv)")
        if path:
            units = {b.get("unit") for b in self.s.bands}
            self.history.save_csv(path, units.pop() if len(units) == 1 else "units")
