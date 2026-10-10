"""Диалоги: серия, панорама, калибровка шкалы, настройки."""
from __future__ import annotations

import copy
import datetime as _dt
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import numpy as np
from PyQt5 import QtCore, QtWidgets

from .. import processing as P
from ..calibration import (MODE_DEVICE, MODE_LINEAR, MODE_MODEL, MODE_PIXELS, MODES,
                           CalibrationSettings, Correction, fit_correction)
from ..config import AppSettings
from ..reference_lines import LINES, all_lines


def _dir_row(parent, edit: QtWidgets.QLineEdit, title: str) -> QtWidgets.QWidget:
    w = QtWidgets.QWidget(parent)
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(edit, 1)
    b = QtWidgets.QPushButton("…")
    b.setFixedWidth(32)
    lay.addWidget(b)

    def browse():
        d = QtWidgets.QFileDialog.getExistingDirectory(parent, title, edit.text() or os.path.expanduser("~"))
        if d:
            edit.setText(d)
    b.clicked.connect(browse)
    return w


# ----------------------------------------------------------------- серия

@dataclass
class SeriesParams:
    count: int
    interval_s: float
    save: bool
    directory: str
    prefix: str
    ext: str
    wavelengths: List[float] = field(default_factory=list)   # пусто — на текущей длине волны
    start_at: Optional[_dt.datetime] = None                  # None — сразу
    beep: bool = True


def parse_wavelengths(text: str) -> List[float]:
    """«546.07; 585,25 640.2» → [546.07, 585.25, 640.2] (разделители — ; пробел, запятая между числами)."""
    import re
    vals = []
    for tok in re.split(r"[;\s]+", text.strip()):
        if not tok:
            continue
        for part in re.split(r",(?=\s|$)", tok):
            if part:
                vals.append(float(part.replace(",", ".")))
    return vals


class SeriesDialog(QtWidgets.QDialog):
    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Серия спектров")
        form = QtWidgets.QFormLayout(self)
        self.count = QtWidgets.QSpinBox()
        self.count.setRange(1, 1_000_000)
        self.count.setValue(10)
        form.addRow("Количество спектров:", self.count)
        self.interval = QtWidgets.QDoubleSpinBox()
        self.interval.setRange(0, 86400)
        self.interval.setDecimals(1)
        self.interval.setSuffix(" с")
        self.interval.setToolTip("Пауза между началами спектров (0 — без пауз)")
        form.addRow("Интервал:", self.interval)
        self.wls = QtWidgets.QLineEdit()
        self.wls.setPlaceholderText("пусто — на текущей длине волны")
        self.wls.setToolTip("Список длин волн через пробел или «;» (например: 546.07; 585.25; 640.2). "
                            "На каждой снимается серия из указанного количества спектров.")
        form.addRow("Длины волн, нм:", self.wls)
        start_row = QtWidgets.QHBoxLayout()
        self.delayed = QtWidgets.QCheckBox("начать в")
        self.start_time = QtWidgets.QTimeEdit(QtCore.QTime.currentTime().addSecs(600))
        self.start_time.setDisplayFormat("HH:mm")
        self.start_time.setEnabled(False)
        self.delayed.toggled.connect(self.start_time.setEnabled)
        start_row.addWidget(self.delayed)
        start_row.addWidget(self.start_time)
        start_row.addStretch(1)
        w = QtWidgets.QWidget()
        w.setLayout(start_row)
        start_row.setContentsMargins(0, 0, 0, 0)
        form.addRow("Старт:", w)
        self.beep = QtWidgets.QCheckBox("звуковой сигнал по окончании")
        self.beep.setChecked(True)
        form.addRow(self.beep)
        self.save = QtWidgets.QCheckBox("сохранять каждый спектр")
        self.save.setChecked(settings.autosave_series)
        form.addRow(self.save)
        self.dir = QtWidgets.QLineEdit(settings.save_dir)
        form.addRow("Папка:", _dir_row(self, self.dir, "Папка для серии"))
        self.prefix = QtWidgets.QLineEdit("series")
        form.addRow("Имя файлов:", self.prefix)
        self.ext = QtWidgets.QComboBox()
        self.ext.addItem("TXT (табуляция, точка)", ".txt")
        self.ext.addItem("CSV (для Excel: ; и запятая)", ".csv")
        self.ext.setCurrentIndex(max(0, self.ext.findData(settings.save_format)))
        form.addRow("Формат:", self.ext)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _accept(self) -> None:
        if self.save.isChecked() and not self.dir.text().strip():
            QtWidgets.QMessageBox.warning(self, "Серия", "Укажите папку для сохранения")
            return
        try:
            wls = parse_wavelengths(self.wls.text())
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Серия", "Не удалось прочитать список длин волн")
            return
        if any(not (0 < w < 3000) for w in wls):
            QtWidgets.QMessageBox.warning(self, "Серия", "Длины волн должны быть в пределах 0…3000 нм")
            return
        self.accept()

    def start_at(self) -> Optional[_dt.datetime]:
        if not self.delayed.isChecked():
            return None
        t = self.start_time.time()
        now = _dt.datetime.now()
        when = now.replace(hour=t.hour(), minute=t.minute(), second=0, microsecond=0)
        return when if when > now else when + _dt.timedelta(days=1)

    def params(self) -> SeriesParams:
        return SeriesParams(self.count.value(), self.interval.value(), self.save.isChecked(),
                            self.dir.text().strip(), self.prefix.text().strip() or "series", self.ext.currentData(),
                            parse_wavelengths(self.wls.text()), self.start_at(), self.beep.isChecked())


# ----------------------------------------------------------------- панорама

@dataclass
class PanoramaParams:
    start: float
    end: float
    overlap: float
    return_back: bool
    trim: int = 0
    equalize: bool = False


class PanoramaDialog(QtWidgets.QDialog):
    def __init__(self, start: float, end: float, plan_fn: Callable[..., List[float]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Панорамный спектр")
        self.plan_fn = plan_fn
        form = QtWidgets.QFormLayout(self)
        form.addRow(QtWidgets.QLabel("Спектр широкого диапазона сшивается из нескольких\n"
                                     "положений монохроматора с перекрытием."))
        self.start = QtWidgets.QDoubleSpinBox()
        self.end = QtWidgets.QDoubleSpinBox()
        for w, v in ((self.start, start), (self.end, end)):
            w.setRange(0, 3000)
            w.setDecimals(2)
            w.setSuffix(" нм")
            w.setValue(v)
        form.addRow("От:", self.start)
        form.addRow("До:", self.end)
        self.overlap = QtWidgets.QSpinBox()
        self.overlap.setRange(2, 60)
        self.overlap.setValue(15)
        self.overlap.setSuffix(" %")
        form.addRow("Перекрытие:", self.overlap)
        self.trim = QtWidgets.QSpinBox()
        self.trim.setRange(0, 400)
        self.trim.setSuffix(" пикс.")
        self.trim.setToolTip("Не использовать столько пикселей с каждого края кадра (там хуже фокусировка "
                             "и меньше света) — как «Used range» в SpectraSP")
        form.addRow("Обрезать края:", self.trim)
        self.equalize = QtWidgets.QCheckBox("выравнивать уровни в перекрытиях")
        self.equalize.setToolTip("Каждый следующий участок умножается на множитель, при котором совпадают "
                                 "суммы в общей области (как «Equalization» в SpectraSP)")
        form.addRow(self.equalize)
        self.back = QtWidgets.QCheckBox("после съёмки вернуть исходную длину волны")
        self.back.setChecked(True)
        form.addRow(self.back)
        self.plan = QtWidgets.QLabel("")
        self.plan.setWordWrap(True)
        form.addRow(self.plan)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        for w in (self.start, self.end, self.overlap, self.trim):
            w.valueChanged.connect(self._update_plan)
        self._update_plan()

    def _update_plan(self) -> None:
        try:
            plan = self.plan_fn(self.start.value(), self.end.value(), self.overlap.value() / 100, self.trim.value())
            self.plan.setText(f"Положений: {len(plan)}: " + ", ".join(f"{c:.1f}" for c in plan[:12]) +
                              (" …" if len(plan) > 12 else "") + " нм")
        except Exception as e:  # noqa: BLE001
            self.plan.setText(f"Нельзя рассчитать: {e}")

    def params(self) -> PanoramaParams:
        return PanoramaParams(self.start.value(), self.end.value(), self.overlap.value() / 100, self.back.isChecked(),
                              self.trim.value(), self.equalize.isChecked())


# ----------------------------------------------------------------- калибровка

class CalibrationDialog(QtWidgets.QDialog):
    """Настройка шкалы длин волн и поправки по реперным линиям."""

    def __init__(self, settings: CalibrationSettings, grating: Optional[int], grating_title: str,
                 spectrum: Optional[np.ndarray],
                 model_fn: Callable[[CalibrationSettings], Optional[np.ndarray]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Калибровка шкалы длин волн")
        self.resize(760, 620)
        self.cal = copy.deepcopy(settings)
        self.grating = grating
        self.spectrum = spectrum
        self.model_fn = model_fn
        self.points: List[Tuple[float, float]] = list(self.cal.corrections.get(grating, Correction()).points) \
            if grating is not None else []

        lay = QtWidgets.QVBoxLayout(self)
        box = QtWidgets.QGroupBox("Модель шкалы")
        form = QtWidgets.QFormLayout(box)
        self.mode = QtWidgets.QComboBox()
        for k, v in MODES.items():
            self.mode.addItem(v, k)
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.cal.mode)))
        form.addRow("Способ:", self.mode)
        self.center = QtWidgets.QDoubleSpinBox()
        self.center.setRange(-5000, 10000)
        self.center.setDecimals(2)
        self.center.setValue(self.cal.center_pixel)
        self.center.setToolTip("Пиксель, на который приходится длина волны, установленная на монохроматоре")
        form.addRow("Центральный пиксель:", self.center)
        self.dev_pw = QtWidgets.QDoubleSpinBox()
        self.dev_pw.setRange(0.001, 1000)
        self.dev_pw.setDecimals(4)
        self.dev_pw.setValue(self.cal.device_pixel_width)
        self.dev_pw.setToolTip("Параметр «ширина пикселя» для модели прибора (как в SpectraSP)")
        form.addRow("Ширина пикселя (модель прибора):", self.dev_pw)
        self.pix_um = QtWidgets.QDoubleSpinBox()
        self.pix_um.setRange(0.1, 100)
        self.pix_um.setDecimals(3)
        self.pix_um.setSuffix(" мкм")
        self.pix_um.setValue(self.cal.pixel_um)
        form.addRow("Шаг пикселя (геом. модель):", self.pix_um)
        self.nmpp = QtWidgets.QDoubleSpinBox()
        self.nmpp.setRange(-10, 10)
        self.nmpp.setDecimals(6)
        self.nmpp.setSuffix(" нм/пикс")
        self.nmpp.setValue(self.cal.nm_per_pixel)
        form.addRow("Дисперсия (линейная):", self.nmpp)
        lay.addWidget(box)

        box2 = QtWidgets.QGroupBox(f"Поправка по реперным линиям — решётка {grating_title or '?'}")
        v2 = QtWidgets.QVBoxLayout(box2)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Источник линий:"))
        self.sources = QtWidgets.QComboBox()
        self.sources.addItem("все", None)
        for name in LINES:
            self.sources.addItem(name, name)
        row.addWidget(self.sources)
        row.addWidget(QtWidgets.QLabel("допуск:"))
        self.tol = QtWidgets.QDoubleSpinBox()
        self.tol.setRange(0.01, 20)
        self.tol.setValue(0.5)
        self.tol.setSuffix(" нм")
        row.addWidget(self.tol)
        self.btn_auto = QtWidgets.QPushButton("Найти реперы в текущем спектре")
        row.addWidget(self.btn_auto)
        row.addStretch(1)
        v2.addLayout(row)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Пиксель", "λ по модели, нм", "λ эталона, нм", "Остаток, нм"])
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        v2.addWidget(self.table, 1)
        row2 = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("Добавить строку")
        self.btn_del = QtWidgets.QPushButton("Удалить выбранные")
        self.btn_fit = QtWidgets.QPushButton("Рассчитать поправку")
        self.btn_clear = QtWidgets.QPushButton("Сбросить поправку")
        for b in (self.btn_add, self.btn_del, self.btn_fit, self.btn_clear):
            row2.addWidget(b)
        v2.addLayout(row2)
        self.result = QtWidgets.QLabel("")
        self.result.setWordWrap(True)
        v2.addWidget(self.result)
        lay.addWidget(box2, 1)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.btn_auto.clicked.connect(self._auto)
        self.btn_add.clicked.connect(lambda: self._add_row(0.0, 0.0))
        self.btn_del.clicked.connect(self._delete)
        self.btn_fit.clicked.connect(self._fit)
        self.btn_clear.clicked.connect(self._clear)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._mode_changed()
        for p, wl in self.points:
            self._add_row(p, wl)
        self._refresh_model()
        if grating is None:
            box2.setEnabled(False)

    # ------------------------------------------------------------------
    def _collect_model(self) -> CalibrationSettings:
        c = copy.deepcopy(self.cal)
        c.mode = self.mode.currentData()
        c.center_pixel = self.center.value()
        c.device_pixel_width = self.dev_pw.value()
        c.pixel_um = self.pix_um.value()
        c.nm_per_pixel = self.nmpp.value()
        return c

    def _mode_changed(self) -> None:
        m = self.mode.currentData()
        self.dev_pw.setEnabled(m == MODE_DEVICE)
        self.pix_um.setEnabled(m in (MODE_DEVICE, MODE_MODEL))
        self.nmpp.setEnabled(m == MODE_LINEAR)
        self.center.setEnabled(m != MODE_PIXELS)
        self._refresh_model()

    def _model(self) -> Optional[np.ndarray]:
        c = self._collect_model()
        c.corrections = {}
        try:
            return self.model_fn(c)
        except Exception:  # noqa: BLE001
            return None

    def _add_row(self, pixel: float, wl: float) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for col, val in ((0, f"{pixel:.3f}"), (2, f"{wl:.4f}")):
            self.table.setItem(r, col, QtWidgets.QTableWidgetItem(val))
        for col in (1, 3):
            it = QtWidgets.QTableWidgetItem("")
            it.setFlags(it.flags() & ~QtCore.Qt.ItemIsEditable)
            self.table.setItem(r, col, it)
        self._refresh_model()

    def _rows(self) -> List[Tuple[float, float]]:
        out = []
        for r in range(self.table.rowCount()):
            try:
                p = float(self.table.item(r, 0).text().replace(",", "."))
                wl = float(self.table.item(r, 2).text().replace(",", "."))
            except (AttributeError, ValueError):
                continue
            if wl > 0:
                out.append((p, wl))
        return out

    def _refresh_model(self) -> None:
        model = self._model()
        for r in range(self.table.rowCount()):
            try:
                p = float(self.table.item(r, 0).text().replace(",", "."))
            except (AttributeError, ValueError):
                continue
            if model is not None:
                mw = float(np.interp(p, np.arange(model.size), model))
                self.table.item(r, 1).setText(f"{mw:.4f}")

    def _delete(self) -> None:
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    def _auto(self) -> None:
        if self.spectrum is None:
            QtWidgets.QMessageBox.information(self, "Калибровка", "Сначала снимите спектр эталонной лампы.")
            return
        model = self._model()
        if model is None:
            QtWidgets.QMessageBox.warning(self, "Калибровка", "Шкала по модели недоступна")
            return
        idx = np.arange(self.spectrum.size, dtype=float)
        peaks = P.find_peaks(idx, self.spectrum, max_peaks=60)
        src = self.sources.currentData()
        lines = [(wl, name) for wl, _i, name in all_lines() if src is None or name == src]
        tol = self.tol.value()
        existing = {round(p, 1) for p, _ in self._rows()}
        added = 0
        for pk in peaks:
            mw = float(np.interp(pk.index, idx, model))
            best = min(lines, key=lambda t: abs(t[0] - mw), default=None)
            if best and abs(best[0] - mw) <= tol and round(pk.index, 1) not in existing:
                self._add_row(pk.index, best[0])
                added += 1
        self.result.setText(f"Найдено реперов: {added}. Проверьте соответствие линий и удалите ошибочные.")
        self._refresh_model()

    def _fit(self) -> None:
        pts = self._rows()
        model = self._model()
        if not pts or model is None:
            self.result.setText("Нет данных для расчёта")
            return
        corr, rms = fit_correction([p for p, _ in pts], [w for _, w in pts], model)
        self._pending = corr
        model_c = model + np.polyval(corr.coeffs, np.arange(model.size)) if corr.coeffs else model
        for r in range(self.table.rowCount()):
            try:
                p = float(self.table.item(r, 0).text().replace(",", "."))
                wl = float(self.table.item(r, 2).text().replace(",", "."))
            except (AttributeError, ValueError):
                continue
            self.table.item(r, 3).setText(f"{wl - np.interp(p, np.arange(model.size), model_c):+.4f}")
        deg = len(corr.coeffs) - 1
        kind = {0: "сдвиг", 1: "линейная", 2: "квадратичная"}.get(deg, f"степень {deg}")
        self.result.setText(f"Поправка: {kind}, по {len(pts)} точкам; СКО остатков {rms:.4f} нм. "
                            "Нажмите OK, чтобы применить.")

    def _clear(self) -> None:
        self.table.setRowCount(0)
        self._pending = Correction()
        self.result.setText("Поправка будет сброшена.")

    def _accept(self) -> None:
        c = self._collect_model()
        pending = getattr(self, "_pending", None)
        if self.grating is not None and pending is not None:
            if pending.coeffs:
                c.corrections[self.grating] = pending
            else:
                c.corrections.pop(self.grating, None)
        self.cal = c
        self.accept()

    def result_settings(self) -> CalibrationSettings:
        return self.cal


# ----------------------------------------------------------------- настройки

class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, settings: AppSettings, camera_ip: str, mono_address: str, parent=None,
                 laser_on: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.s = settings
        form = QtWidgets.QFormLayout(self)
        self.dir = QtWidgets.QLineEdit(settings.spectrasp_dir)
        form.addRow("Каталог SpectraSP:", _dir_row(self, self.dir, "Каталог установленной SpectraSP"))
        info = QtWidgets.QLabel(f"Камера: {camera_ip or 'адрес не найден'}\n"
                                f"Монохроматор: {mono_address or 'адрес не найден'}\n"
                                "Адреса берутся из файлов SpectraSP и программой не изменяются.")
        info.setStyleSheet("color: #555;")
        form.addRow(info)
        self.dll = QtWidgets.QComboBox()
        for name in ("auto", "psi_ccd2_ver0.dll", "psi_ccd2_ver1.dll", "psi_ccd2_ver3.dll"):
            self.dll.addItem("автоматически (как SpectraSP)" if name == "auto" else name, name)
        self.dll.setCurrentIndex(max(0, self.dll.findData(settings.camera_dll)))
        form.addRow("Библиотека камеры:", self.dll)
        self.frame_to = QtWidgets.QDoubleSpinBox()
        self.frame_to.setRange(5, 3600)
        self.frame_to.setSuffix(" с")
        self.frame_to.setValue(settings.frame_timeout_s)
        self.frame_to.setToolTip("Если кадр не пришёл за (экспозиция + считывание + это время), экспозиция прерывается")
        form.addRow("Запас времени на кадр:", self.frame_to)
        self.mono_to = QtWidgets.QDoubleSpinBox()
        self.mono_to.setRange(10, 3600)
        self.mono_to.setSuffix(" с")
        self.mono_to.setValue(settings.mono_timeout_s)
        form.addRow("Таймаут движения монохроматора:", self.mono_to)
        self.warm = QtWidgets.QCheckBox("отогревать ПЗС до +2 °C при отключении (как SpectraSP)")
        self.warm.setChecked(settings.warm_up_on_close)
        self.warm.setToolTip("Защита матрицы от конденсата и термоудара. Выключать не рекомендуется.")
        self.warm.toggled.connect(self._warm_toggled)
        form.addRow(self.warm)
        self.laser_out = QtWidgets.QComboBox()
        self.laser_out.addItem("не подключён", 0)
        self.laser_out.addItem("к синхровыходу 1 камеры", 1)
        self.laser_out.addItem("к синхровыходу 2 камеры", 2)
        self.laser_out.setCurrentIndex(max(0, self.laser_out.findData(settings.laser_output)))
        self.laser_level = QtWidgets.QComboBox()
        self.laser_level.addItem("логической 1 (высоким уровнем)", True)
        self.laser_level.addItem("логическим 0 (низким уровнем)", False)
        self.laser_level.setCurrentIndex(0 if settings.laser_active_high else 1)
        form.addRow("Лазер подключён:", self.laser_out)
        form.addRow("Лазер включается:", self.laser_level)
        if laser_on:
            for w in (self.laser_out, self.laser_level):
                w.setEnabled(False)
                w.setToolTip("Сначала выключите лазер")
        self.fmt = QtWidgets.QComboBox()
        self.fmt.addItem("TXT (табуляция, точка)", ".txt")
        self.fmt.addItem("CSV (для Excel: ; и запятая)", ".csv")
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(settings.save_format)))
        form.addRow("Формат файлов по умолчанию:", self.fmt)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _accept(self) -> None:
        s = self.s
        s.spectrasp_dir = self.dir.text().strip()
        s.camera_dll = self.dll.currentData()
        s.frame_timeout_s = self.frame_to.value()
        s.mono_timeout_s = self.mono_to.value()
        s.warm_up_on_close = self.warm.isChecked()
        s.laser_output = int(self.laser_out.currentData())
        s.laser_active_high = bool(self.laser_level.currentData())
        s.save_format = self.fmt.currentData()
        self.accept()

    def _warm_toggled(self, on: bool) -> None:
        if not on and not _confirm_no_warm_up(self):
            self.warm.blockSignals(True)
            self.warm.setChecked(True)
            self.warm.blockSignals(False)


def _confirm_no_warm_up(parent) -> bool:
    r = QtWidgets.QMessageBox.question(
        parent, "Отогрев ПЗС",
        "Без отогрева охлаждённая ПЗС останется холодной после отключения программы. Если затем выключить "
        "питание камеры, на матрице может выпасть конденсат.\n\nВсё равно не отогревать?",
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
    return r == QtWidgets.QMessageBox.Yes


class BusyDialog(QtWidgets.QDialog):
    """Окно «подождите»: текст обновляет фоновая задача (text_changed можно вызывать из любого
    потока), закрыть его может только сама программа (finish). Кнопка — необязательная."""
    text_changed = QtCore.pyqtSignal(str)

    def __init__(self, parent, text: str, button_text: str = "", on_button: Optional[Callable[[], None]] = None,
                 button_tip: str = ""):
        super().__init__(parent)
        from .. import APP_NAME
        self.setWindowTitle(APP_NAME)
        self.setModal(True)
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(QtCore.Qt.WindowCloseButtonHint, False)
        self._done = False
        lay = QtWidgets.QVBoxLayout(self)
        self.label = QtWidgets.QLabel(text)
        self.label.setWordWrap(True)
        self.label.setMinimumWidth(420)
        lay.addWidget(self.label)
        bar = QtWidgets.QProgressBar()
        bar.setRange(0, 0)
        lay.addWidget(bar)
        if button_text:
            self.button = QtWidgets.QPushButton(button_text)
            self.button.setToolTip(button_tip)

            def pressed():
                self.button.setEnabled(False)
                if on_button:
                    on_button()
            self.button.clicked.connect(pressed)
            lay.addWidget(self.button, 0, QtCore.Qt.AlignRight)
        self.text_changed.connect(self.label.setText)

    def finish(self) -> None:
        self._done = True
        self.accept()

    def reject(self) -> None:             # Esc не закрывает окно, пока задача не закончилась
        if self._done:
            super().reject()

    def closeEvent(self, event) -> None:
        if self._done:
            event.accept()
        else:
            event.ignore()
