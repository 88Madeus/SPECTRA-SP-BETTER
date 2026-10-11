"""Окна настройки: полосы строк, дефектные пиксели, коррекция интенсивности."""
from __future__ import annotations

import os
from typing import Callable, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from .. import processing as P
from ..config import AppSettings
from ..storage import load_spectrum
from .plots import STRIPE_COLORS

MAX_STRIPES = 8


# ====================================================================== полосы строк
class StripesDialog(QtWidgets.QDialog):
    """Список полос строк ПЗС: с каждой полосы — свой спектр (как «Stripe 1…8» SpectraSP)."""

    def __init__(self, stripes: List[Tuple[int, int]], height: int, image: Optional[np.ndarray] = None,
                 estimate: Optional[Callable[[List[Tuple[int, int]]], Optional[float]]] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Полосы строк ПЗС")
        self.height_px = max(1, int(height))
        self.image = image
        self.estimate = estimate
        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "С каждой полосы строится свой спектр (например, с нескольких волокон). Полоса 1 — основная: "
            "по ней ищутся пики, ей вычитается фон для графика. При одной полосе камера объединяет строки "
            "сама (быстро); при нескольких — читает строки от верхней полосы до нижней и суммирует их "
            "программно. Фон снимается заново для новой раскладки полос.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Полоса", "Верхняя строка", "Нижняя строка"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        lay.addWidget(self.table)
        bar = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("Добавить")
        self.btn_del = QtWidgets.QPushButton("Удалить")
        self.btn_find = QtWidgets.QPushButton("Найти по кадру")
        self.btn_find.setToolTip("Найти освещённые участки на последнем полном кадре (режим «Изображение»)")
        self.btn_find.setEnabled(image is not None)
        for b in (self.btn_add, self.btn_del, self.btn_find):
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.info = QtWidgets.QLabel("")
        self.info.setStyleSheet("color: #555;")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.btn_add.clicked.connect(self._add)
        self.btn_del.clicked.connect(self._delete)
        self.btn_find.clicked.connect(self._find)
        for t, b in stripes:
            self._append(t, b)
        self._update()
        self.resize(460, 420)

    def _append(self, top: int, bottom: int) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for col, v in ((1, top), (2, bottom)):
            sp = QtWidgets.QSpinBox()
            sp.setRange(0, self.height_px - 1)
            sp.setValue(int(v))
            sp.valueChanged.connect(self._update)
            self.table.setCellWidget(r, col, sp)

    def _renumber(self) -> None:
        for r in range(self.table.rowCount()):
            it = QtWidgets.QTableWidgetItem(str(r + 1) + (" (основная)" if r == 0 else ""))
            it.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
            it.setForeground(QtGui.QColor(STRIPE_COLORS[r % len(STRIPE_COLORS)]).darker(120))
            self.table.setItem(r, 0, it)

    def stripes(self) -> List[Tuple[int, int]]:
        out = []
        for r in range(self.table.rowCount()):
            a = self.table.cellWidget(r, 1).value()
            b = self.table.cellWidget(r, 2).value()
            out.append((min(a, b), max(a, b)))
        return out

    def _add(self) -> None:
        if self.table.rowCount() >= MAX_STRIPES:
            return
        st = self.stripes()
        t, b = st[-1] if st else (0, 29)
        h = b - t + 1
        nt = min(self.height_px - h, b + 1 + h)
        self._append(max(0, nt), min(self.height_px - 1, max(0, nt) + h - 1))
        self._update()

    def _delete(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            if self.table.rowCount() > 1:
                self.table.removeRow(r)
        self._update()

    def _find(self) -> None:
        found = P.find_stripes(self.image, MAX_STRIPES) if self.image is not None else []
        if not found:
            QtWidgets.QMessageBox.information(self, "Полосы строк", "На кадре не найдено освещённых участков.")
            return
        self.table.setRowCount(0)
        for t, b in found:
            self._append(t, b)
        self._update()

    def _update(self) -> None:
        self._renumber()
        st = self.stripes()
        self.btn_add.setEnabled(len(st) < MAX_STRIPES)
        self.btn_del.setEnabled(len(st) > 1)
        text = f"Полос: {len(st)}."
        if len(st) > 1:
            lo, hi = min(t for t, _ in st), max(b for _, b in st)
            text += f" Камера будет читать строки {lo}…{hi} ({hi - lo + 1} строк) построчно."
        if self.estimate:
            t = self.estimate(st)
            if t is not None:
                text += f" Кадр ≈ {t:.2f} с."
        overlap = any(a[1] >= b[0] for a, b in zip(sorted(st), sorted(st)[1:]))
        if overlap:
            text += " Внимание: полосы перекрываются."
        self.info.setText(text)


# ====================================================================== дефектные пиксели
class DefectPixelsDialog(QtWidgets.QDialog):
    """Список дефектных столбцов ПЗС (как «Defect Pixel Correction» SpectraSP) и поиск по фону."""

    def __init__(self, settings: AppSettings, width: int, dark: Optional[np.ndarray] = None, parent=None):
        super().__init__(parent)
        self.s = settings
        self.dark = dark
        self.setWindowTitle("Дефектные пиксели ПЗС")
        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "«Горячие» и «мёртвые» столбцы матрицы заменяются линейной интерполяцией между соседними "
            "исправными. Номера — как их отдаёт камера (0 … ширина−1), разворот спектра учитывается сам. "
            "Найти их проще всего по темновому фону с большой экспозицией.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.enabled = QtWidgets.QCheckBox("исправлять при съёмке")
        self.enabled.setChecked(settings.defect_correction)
        lay.addWidget(self.enabled)
        self.list = QtWidgets.QListWidget()
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        lay.addWidget(self.list, 1)
        row = QtWidgets.QHBoxLayout()
        self.col = QtWidgets.QSpinBox()
        self.col.setRange(0, max(0, width - 1))
        self.btn_add = QtWidgets.QPushButton("Добавить")
        self.btn_del = QtWidgets.QPushButton("Удалить")
        self.btn_clear = QtWidgets.QPushButton("Очистить")
        for w in (QtWidgets.QLabel("Столбец:"), self.col, self.btn_add, self.btn_del, self.btn_clear):
            row.addWidget(w)
        row.addStretch(1)
        lay.addLayout(row)
        row2 = QtWidgets.QHBoxLayout()
        self.threshold = QtWidgets.QDoubleSpinBox()
        self.threshold.setRange(3, 100)
        self.threshold.setValue(8)
        self.threshold.setSuffix(" σ")
        self.threshold.setToolTip("Порог: насколько столбец должен отличаться от соседей (в единицах шума)")
        self.btn_find = QtWidgets.QPushButton("Найти по фону")
        self.btn_find.setEnabled(dark is not None)
        self.btn_find.setToolTip("Использовать снятый темновой фон" if dark is not None
                                 else "Сначала снимите фон (кнопка «Фон», лучше с большой экспозицией)")
        for w in (QtWidgets.QLabel("Порог:"), self.threshold, self.btn_find):
            row2.addWidget(w)
        row2.addStretch(1)
        lay.addLayout(row2)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.btn_add.clicked.connect(lambda: self._set(self._cols() | {self.col.value()}))
        self.btn_del.clicked.connect(self._delete)
        self.btn_clear.clicked.connect(lambda: self._set(set()))
        self.btn_find.clicked.connect(self._find)
        self._set(set(settings.defect_pixels))
        self.resize(440, 460)

    def _cols(self) -> set:
        return {int(self.list.item(i).data(QtCore.Qt.UserRole)) for i in range(self.list.count())}

    def _set(self, cols) -> None:
        self.list.clear()
        for c in sorted(cols):
            it = QtWidgets.QListWidgetItem(f"столбец {c}")
            it.setData(QtCore.Qt.UserRole, int(c))
            self.list.addItem(it)

    def _delete(self) -> None:
        drop = {int(it.data(QtCore.Qt.UserRole)) for it in self.list.selectedItems()}
        self._set(self._cols() - drop)

    def _find(self) -> None:
        found = P.find_defects(self.dark, self.threshold.value())
        new = set(found) - self._cols()
        self._set(self._cols() | set(found))
        QtWidgets.QMessageBox.information(
            self, "Дефектные пиксели",
            f"Найдено дефектных столбцов: {len(found)} (новых: {len(new)})." if found
            else "Дефектных столбцов не найдено.")

    def _ok(self) -> None:
        cols = sorted(self._cols())
        if len(cols) > 200:
            QtWidgets.QMessageBox.warning(self, "Дефектные пиксели",
                                          f"Слишком много столбцов ({len(cols)}) — вероятно, порог слишком "
                                          "низкий или в фоне есть свет. Уменьшите список.")
            return
        self.s.defect_pixels = cols
        self.s.defect_correction = self.enabled.isChecked()
        self.accept()


# ====================================================================== коррекция интенсивности
class IntensityCorrectionDialog(QtWidgets.QDialog):
    """Таблица поправок K(x) к интенсивности (как «Intensity Correction» SpectraSP по пикселям и по
    длинам волн) и расчёт по эталонному источнику: лампа накаливания (чёрное тело) или файл."""

    def __init__(self, settings: AppSettings, x_nm: Optional[np.ndarray] = None,
                 measured: Optional[np.ndarray] = None, parent=None):
        super().__init__(parent)
        self.s = settings
        self.x_nm = x_nm
        self.measured = measured
        self.setWindowTitle("Коррекция интенсивности")
        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "Спектр умножается на поправку K, заданную точками (между ними — линейно, за краями — "
            "крайние значения). Так учитывается спектральная чувствительность решётки и ПЗС. Таблицу "
            "можно рассчитать по эталону: снимите спектр лампы накаливания (с вычетом фона, без коррекции) "
            "и укажите её цветовую температуру или файл с её истинным спектром.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        top = QtWidgets.QHBoxLayout()
        self.enabled = QtWidgets.QCheckBox("применять при съёмке")
        self.enabled.setChecked(settings.intensity_corr)
        self.by = QtWidgets.QComboBox()
        self.by.addItem("по длине волны, нм", "wl")
        self.by.addItem("по номеру пикселя", "px")
        self.by.setCurrentIndex(max(0, self.by.findData(settings.intensity_corr_by)))
        top.addWidget(self.enabled)
        top.addStretch(1)
        top.addWidget(QtWidgets.QLabel("Поправка:"))
        top.addWidget(self.by)
        lay.addLayout(top)
        mid = QtWidgets.QHBoxLayout()
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["x", "K"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        mid.addWidget(self.table, 1)
        self.preview = pg.PlotWidget(background="w")
        self.preview.setLabel("left", "K")
        self.preview.showGrid(x=True, y=True, alpha=0.3)
        self.curve = self.preview.plot([], [], pen=pg.mkPen("#1f77b4", width=1.5), symbol="o", symbolSize=5)
        mid.addWidget(self.preview, 2)
        lay.addLayout(mid, 1)
        bar = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("Добавить точку")
        self.btn_del = QtWidgets.QPushButton("Удалить")
        self.btn_clear = QtWidgets.QPushButton("Очистить")
        self.btn_open = QtWidgets.QPushButton("Открыть…")
        self.btn_save = QtWidgets.QPushButton("Сохранить…")
        for b in (self.btn_add, self.btn_del, self.btn_clear, self.btn_open, self.btn_save):
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        ref = QtWidgets.QGroupBox("Рассчитать по эталону (по текущему спектру)")
        rl = QtWidgets.QHBoxLayout(ref)
        self.temp = QtWidgets.QDoubleSpinBox()
        self.temp.setRange(1000, 10000)
        self.temp.setValue(3000)
        self.temp.setSuffix(" K")
        self.temp.setToolTip("Цветовая температура лампы накаливания (галогенные — около 2800…3200 K)")
        self.btn_planck = QtWidgets.QPushButton("Лампа накаливания")
        self.btn_ref_file = QtWidgets.QPushButton("Эталон из файла…")
        ok = x_nm is not None and measured is not None
        for b in (self.btn_planck, self.btn_ref_file):
            b.setEnabled(ok)
            if not ok:
                b.setToolTip("Нужен текущий спектр со шкалой длин волн")
        for w in (QtWidgets.QLabel("Температура:"), self.temp, self.btn_planck, self.btn_ref_file):
            rl.addWidget(w)
        rl.addStretch(1)
        lay.addWidget(ref)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.btn_add.clicked.connect(self._add)
        self.btn_del.clicked.connect(self._delete)
        self.btn_clear.clicked.connect(lambda: self.set_points([]))
        self.btn_open.clicked.connect(self._open)
        self.btn_save.clicked.connect(self._save)
        self.btn_planck.clicked.connect(self._from_planck)
        self.btn_ref_file.clicked.connect(self._from_file)
        self.by.currentIndexChanged.connect(self._axis_label)
        self.table.itemChanged.connect(lambda _: self._refresh_preview())
        self.set_points(settings.intensity_corr_points)
        self._axis_label()
        self.resize(760, 520)

    # ------------------------------------------------------------ таблица
    def points(self) -> List[List[float]]:
        out = []
        for r in range(self.table.rowCount()):
            try:
                out.append([float(self.table.item(r, 0).text().replace(",", ".")),
                            float(self.table.item(r, 1).text().replace(",", "."))])
            except (AttributeError, ValueError):
                continue
        return sorted(out)

    def set_points(self, pts) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for xv, kv in sorted(pts):
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QtWidgets.QTableWidgetItem(f"{xv:.4g}"))
            self.table.setItem(r, 1, QtWidgets.QTableWidgetItem(f"{kv:.5g}"))
        self.table.blockSignals(False)
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        pts = self.points()
        self.curve.setData([p[0] for p in pts], [p[1] for p in pts])

    def _axis_label(self) -> None:
        wl = self.by.currentData() == "wl"
        self.table.setHorizontalHeaderLabels(["Длина волны, нм" if wl else "Пиксель", "K"])
        self.preview.setLabel("bottom", "Длина волны, нм" if wl else "Пиксель")

    def _add(self) -> None:
        pts = self.points()
        x = pts[-1][0] + 10 if pts else (500.0 if self.by.currentData() == "wl" else 0.0)
        self.set_points(pts + [[x, 1.0]])

    def _delete(self) -> None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        self.set_points([p for r, p in enumerate(self.points()) if r not in rows])

    def _open(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Таблица поправок", self.s.save_dir or "",
                                                        "Таблицы (*.txt *.csv *.dat);;Все файлы (*)")
        if not path:
            return
        try:
            x, y, _meta, _hdr = load_spectrum(path)
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, "Таблица поправок", str(e))
            return
        self.set_points([[float(a), float(b)] for a, b in zip(x, y)])

    def _save(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Сохранить таблицу поправок", os.path.join(self.s.save_dir or "", "intensity_correction.txt"),
            "Текст (*.txt)")
        if not path:
            return
        head = "wavelength_nm" if self.by.currentData() == "wl" else "pixel"
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {head}\tK\n")
            for a, b in self.points():
                f.write(f"{a:.6g}\t{b:.6g}\n")

    # ------------------------------------------------------------ расчёт
    def _build(self, reference: np.ndarray, what: str) -> None:
        try:
            pts = P.build_correction(self.x_nm, self.measured, reference)
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Коррекция интенсивности", str(e))
            return
        self.by.setCurrentIndex(self.by.findData("wl"))
        self.set_points(pts)
        self.enabled.setChecked(True)
        QtWidgets.QMessageBox.information(self, "Коррекция интенсивности",
                                          f"Таблица рассчитана по эталону ({what}): {len(pts)} точек, "
                                          "нормирована на среднее 1.")

    def _from_planck(self) -> None:
        self._build(P.planck(self.x_nm, self.temp.value()), f"чёрное тело {self.temp.value():.0f} K")

    def _from_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Истинный спектр эталона (нм, значение)",
                                                        self.s.save_dir or "",
                                                        "Спектры (*.txt *.csv *.dat);;Все файлы (*)")
        if not path:
            return
        try:
            x, y, _meta, _hdr = load_spectrum(path)
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, "Эталон", str(e))
            return
        order = np.argsort(x)
        lo, hi = float(np.min(self.x_nm)), float(np.max(self.x_nm))
        if x[order][0] > lo or x[order][-1] < hi:
            QtWidgets.QMessageBox.warning(self, "Эталон", f"Файл эталона не покрывает диапазон спектра "
                                                          f"{lo:.1f}…{hi:.1f} нм.")
            return
        self._build(np.interp(self.x_nm, x[order], y[order]), os.path.basename(path))

    def _ok(self) -> None:
        pts = self.points()
        if self.enabled.isChecked() and len(pts) < 2:
            QtWidgets.QMessageBox.warning(self, "Коррекция интенсивности", "Нужно хотя бы две точки.")
            return
        if any(k <= 0 for _, k in pts):
            QtWidgets.QMessageBox.warning(self, "Коррекция интенсивности", "Поправка K должна быть больше нуля.")
            return
        self.s.intensity_corr_points = pts
        self.s.intensity_corr_by = self.by.currentData()
        self.s.intensity_corr = self.enabled.isChecked() and len(pts) >= 2
        self.accept()
