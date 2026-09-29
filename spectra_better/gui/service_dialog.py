"""Сервисное окно монохроматора (значок шестерёнки).

Собирает в одном месте то, что в SpectraSP было доступно только в штатном
окне драйвера DevCtrl (скрытая кнопка «Show spectra instrument window»):
шторка, выходное зеркало, щели, решётки, длина волны и сервисный режим —
положения шаговых двигателей в шагах, перемещение на заданное число шагов и
сброс механизмов (поиск нулевого положения). Отсюда же можно открыть и само
штатное окно DevCtrl.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Tuple

from PyQt5 import QtCore, QtWidgets

from ..hw.base import MECH_GRATING, MECH_TURRET
from .tasks import TaskRunner

log = logging.getLogger(__name__)


class MonoServiceDialog(QtWidgets.QDialog):
    def __init__(self, inst, tasks: TaskRunner, on_changed: Callable[[], None], parent=None):
        super().__init__(parent)
        self.inst = inst
        self.tasks = tasks
        self.on_changed = on_changed
        self._busy = False
        self.setWindowTitle("Сервис монохроматора")
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)
        self.resize(720, 640)
        lay = QtWidgets.QVBoxLayout(self)

        # --- заголовок
        head = QtWidgets.QHBoxLayout()
        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        head.addWidget(self.info, 1)
        self.btn_refresh = QtWidgets.QPushButton("Обновить")
        self.btn_native = QtWidgets.QPushButton("Штатное окно DevCtrl…")
        self.btn_native.setToolTip("Окно драйвера прибора, которое в SpectraSP открывалось скрытой кнопкой")
        head.addWidget(self.btn_refresh)
        head.addWidget(self.btn_native)
        lay.addLayout(head)
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color: #a15c00;")
        lay.addWidget(self.status)
        self._controls: List[QtWidgets.QWidget] = [self.btn_refresh, self.btn_native]

        # --- основное управление
        box = QtWidgets.QGroupBox("Управление")
        g = QtWidgets.QGridLayout(box)
        row = 0
        g.addWidget(QtWidgets.QLabel("Шторка:"), row, 0)
        self.btn_sh_open = QtWidgets.QPushButton("Открыть")
        self.btn_sh_close = QtWidgets.QPushButton("Закрыть")
        self.lbl_shutter = QtWidgets.QLabel("?")
        g.addWidget(self.btn_sh_open, row, 1)
        g.addWidget(self.btn_sh_close, row, 2)
        g.addWidget(self.lbl_shutter, row, 3)
        self._controls += [self.btn_sh_open, self.btn_sh_close]
        row += 1

        mono_info = inst.mono.info
        self.mirror_group = QtWidgets.QButtonGroup(self)
        if mono_info.mirror_positions:
            g.addWidget(QtWidgets.QLabel("Выходное зеркало:"), row, 0)
            for i, name in enumerate(mono_info.mirror_positions):
                rb = QtWidgets.QRadioButton(name)
                self.mirror_group.addButton(rb, i)
                g.addWidget(rb, row, 1 + i)
                self._controls.append(rb)
            row += 1

        self.slit_spins: List[QtWidgets.QDoubleSpinBox] = []
        self.slit_labels: List[QtWidgets.QLabel] = []
        for i, name in enumerate(mono_info.slits or ["Входная щель"]):
            g.addWidget(QtWidgets.QLabel(f"{name}:"), row, 0)
            sp = QtWidgets.QDoubleSpinBox()
            sp.setRange(0, 3000)
            sp.setDecimals(1)
            sp.setSuffix(" мкм")
            b = QtWidgets.QPushButton("Установить")
            lbl = QtWidgets.QLabel("")
            g.addWidget(sp, row, 1)
            g.addWidget(b, row, 2)
            g.addWidget(lbl, row, 3)
            b.clicked.connect(lambda _=False, i=i, sp=sp: self._run(
                f"щель {i + 1}…", lambda: self.inst.set_slit(i, sp.value())))
            self.slit_spins.append(sp)
            self.slit_labels.append(lbl)
            self._controls += [sp, b]
            row += 1

        g.addWidget(QtWidgets.QLabel("Решётка:"), row, 0)
        self.grating = QtWidgets.QComboBox()
        for gr in mono_info.gratings:
            self.grating.addItem(gr.title, gr.index)
        self.btn_grating = QtWidgets.QPushButton("Сменить")
        self.lbl_grating = QtWidgets.QLabel("")
        g.addWidget(self.grating, row, 1)
        g.addWidget(self.btn_grating, row, 2)
        g.addWidget(self.lbl_grating, row, 3)
        self._controls += [self.grating, self.btn_grating]
        row += 1

        g.addWidget(QtWidgets.QLabel("Длина волны:"), row, 0)
        self.wl = QtWidgets.QDoubleSpinBox()
        self.wl.setRange(0, 3000)
        self.wl.setDecimals(3)
        self.wl.setSuffix(" нм")
        self.btn_wl = QtWidgets.QPushButton("Перейти")
        self.lbl_wl = QtWidgets.QLabel("")
        g.addWidget(self.wl, row, 1)
        g.addWidget(self.btn_wl, row, 2)
        g.addWidget(self.lbl_wl, row, 3)
        self._controls += [self.wl, self.btn_wl]
        g.setColumnStretch(3, 1)
        lay.addWidget(box)

        # --- шаговые двигатели
        box2 = QtWidgets.QGroupBox("Шаговые двигатели")
        v2 = QtWidgets.QVBoxLayout(box2)
        warn = QtWidgets.QLabel("Сервисный режим, то же, что «Test» в DevCtrl. "
                                "Положения — в шагах двигателей. Перемещение по шагам и сброс механизмов — "
                                "сервисные операции: после них проверьте длину волны и ширину щелей. "
                                "Сброс ищет нулевое положение по датчику и может занять до минуты.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #555;")
        v2.addWidget(warn)
        grid = QtWidgets.QGridLayout()
        for col, text in enumerate(("Механизм", "Положение", "Перейти в, шаги", "", "Сдвинуть на", "", "")):
            lb = QtWidgets.QLabel(f"<b>{text}</b>")
            grid.addWidget(lb, 0, col)
        self.step_rows: Dict[Tuple[str, int], Tuple[QtWidgets.QLabel, QtWidgets.QSpinBox]] = {}
        for r, (mech, idx, name) in enumerate(inst.service_mechanisms(), start=1):
            limit = mono_info.step_limits.get((mech, idx), 0) or 2_000_000
            pos_lbl = QtWidgets.QLabel("—")
            abs_sp = QtWidgets.QSpinBox()
            abs_sp.setRange(0, limit)
            abs_sp.setToolTip(f"0…{limit} шагов")
            b_abs = QtWidgets.QPushButton("Перейти")
            rel_sp = QtWidgets.QSpinBox()
            rel_sp.setRange(-limit, limit)
            rel_sp.setValue(100 if mech != MECH_GRATING else 1000)
            b_rel = QtWidgets.QPushButton("Сдвинуть")
            b_reset = QtWidgets.QPushButton("Сброс")
            b_reset.setToolTip("Поиск нулевого положения механизма по датчику")
            for col, w in enumerate((QtWidgets.QLabel(name), pos_lbl, abs_sp, b_abs, rel_sp, b_rel, b_reset)):
                grid.addWidget(w, r, col)
            b_abs.clicked.connect(lambda _=False, m=mech, i=idx, sp=abs_sp, n=name: self._confirm_run(
                f"Переместить «{n}» в положение {sp.value()} шагов?", f"{n}: перемещение…",
                lambda: self.inst.service_set_steps(m, i, sp.value())))
            b_rel.clicked.connect(lambda _=False, m=mech, i=idx, sp=rel_sp, n=name: self._run(
                f"{n}: сдвиг…", lambda: self.inst.service_move_steps(m, i, sp.value())))
            b_reset.clicked.connect(lambda _=False, m=mech, i=idx, n=name: self._confirm_run(
                f"Выполнить сброс (поиск нуля) механизма «{n}»?"
                + ("\nПосле сброса длину волны нужно будет установить заново." if m in (MECH_GRATING, MECH_TURRET) else ""),
                f"{n}: сброс…", lambda: self.inst.service_reset(m, i)))
            self.step_rows[(mech, idx)] = (pos_lbl, abs_sp)
            self._controls += [abs_sp, b_abs, rel_sp, b_rel, b_reset]
        grid.setColumnStretch(0, 1)
        v2.addLayout(grid)
        lay.addWidget(box2, 1)

        close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        close.button(QtWidgets.QDialogButtonBox.Close).setText("Закрыть")
        close.rejected.connect(self.close)
        lay.addWidget(close)

        # --- сигналы
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_native.clicked.connect(self._native)
        self.btn_sh_open.clicked.connect(lambda: self._run("шторка…", lambda: self.inst.set_shutter(True)))
        self.btn_sh_close.clicked.connect(lambda: self._run("шторка…", lambda: self.inst.set_shutter(False)))
        self.mirror_group.buttonClicked[int].connect(
            lambda pos: self._run("зеркало…", lambda: self.inst.set_mirror(pos)))
        self.btn_grating.clicked.connect(self._change_grating)
        self.btn_wl.clicked.connect(lambda: self._run("длина волны…", lambda: self.inst.goto_wavelength(self.wl.value())))
        self._update_header()
        self.refresh()

    # ------------------------------------------------------------------
    def _update_header(self) -> None:
        mi = self.inst.mono.info
        self.info.setText(f"<b>{mi.model or 'Монохроматор'}</b> № {mi.serial or '—'} (тип {mi.device_type}), "
                          f"{self.inst.mono_address() or 'адрес —'}")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = busy
        for w in self._controls:
            w.setEnabled(not busy)
        self.status.setText(text if busy else "")

    def _run(self, text: str, fn, refresh: bool = True) -> None:
        if self._busy:
            return
        self._set_busy(True, "Выполняется: " + text)

        def done(_):
            self._set_busy(False)
            if refresh:
                self.refresh()
            self.on_changed()

        def fail(e):
            self._set_busy(False)
            log.error("Сервис монохроматора: %s", e)
            QtWidgets.QMessageBox.warning(self, "Сервис монохроматора", str(e))
            if refresh:
                self.refresh()
        self.tasks.run(fn, done, fail, "mono-service")

    def _confirm_run(self, question: str, text: str, fn) -> None:
        r = QtWidgets.QMessageBox.question(self, "Сервис монохроматора", question)
        if r == QtWidgets.QMessageBox.Yes:
            self._run(text, fn)

    def _change_grating(self) -> None:
        idx = self.grating.currentData()
        if idx is None:
            return
        self._confirm_run(f"Повернуть турель на решётку {self.grating.currentText()}?\n"
                          "Это может занять около минуты.", "смена решётки…",
                          lambda: self.inst.set_grating(idx))

    def _native(self) -> None:
        r = QtWidgets.QMessageBox.question(
            self, "Штатное окно DevCtrl",
            "Будет открыто штатное окно драйвера прибора (то же, что в SpectraSP открывалось скрытой кнопкой).\n\n"
            "В его меню «Configuration» находятся заводские параметры прибора (нули, фокус, шаг) — "
            "не меняйте их без необходимости. Состояние прибора в программе обновится по кнопке «Обновить».")
        if r != QtWidgets.QMessageBox.Yes:
            return
        self._run("открытие окна DevCtrl…", self.inst.show_native_window, refresh=False)

    def refresh(self) -> None:
        if self._busy:
            return
        self._set_busy(True, "Чтение состояния…")

        def show(snap):
            self._set_busy(False)
            self._show(snap)

        def fail(e):
            self._set_busy(False)
            self.status.setText(f"Ошибка чтения: {e}")
        self.tasks.run(self.inst.service_snapshot, show, fail, "mono-service-read")

    def _show(self, snap: Dict[str, object]) -> None:
        sh = snap.get("shutter")
        self.lbl_shutter.setText({True: "открыта", False: "закрыта"}.get(sh, "неизвестно"))
        mirror = snap.get("mirror")
        if mirror is not None and self.mirror_group.button(mirror) is not None:
            self.mirror_group.button(mirror).setChecked(True)
        for i, (sp, lbl) in enumerate(zip(self.slit_spins, self.slit_labels)):
            w = snap.get(f"slit{i}")
            lbl.setText("—" if w is None else f"сейчас {w:g} мкм")
            if w is not None:
                sp.setValue(w)
        gr = snap.get("grating")
        if gr is not None:
            i = self.grating.findData(gr)
            if i >= 0:
                self.grating.setCurrentIndex(i)
            self.lbl_grating.setText(f"сейчас №{gr + 1}")
        wl = snap.get("wl")
        if wl is not None:
            self.wl.setValue(wl)
            self.lbl_wl.setText(f"сейчас {wl:.3f} нм")
        for key, value in (snap.get("steps") or {}).items():
            row = self.step_rows.get(key)
            if row is None:
                continue
            lbl, abs_sp = row
            lbl.setText("—" if value is None else f"{value}")
            if value is not None:
                abs_sp.setValue(value)
        self.status.setText("Идёт движение" if snap.get("busy") else "")

    def closeEvent(self, event) -> None:
        self.on_changed()
        super().closeEvent(event)
