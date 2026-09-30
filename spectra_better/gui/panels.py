"""Панели управления: камера, экспозиция, монохроматор, обработка."""
from __future__ import annotations

from typing import Optional, Sequence

from PyQt5 import QtCore, QtWidgets

from ..config import ACQ_IMAGE, ACQ_SPECTRUM, AppSettings
from ..hw.base import SYNC_OUT_MODES, SYNC_OUT_SPECTRASP, CameraInfo, CameraState, MonoInfo


def _big(label: QtWidgets.QLabel, pt: int = 15) -> QtWidgets.QLabel:
    f = label.font()
    f.setPointSize(pt)
    f.setBold(True)
    label.setFont(f)
    return label


def _row(*widgets) -> QtWidgets.QWidget:
    w = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        if isinstance(x, int):
            lay.addStretch(x)
        else:
            lay.addWidget(x)
    return w


class CameraPanel(QtWidgets.QGroupBox):
    cooling_on = QtCore.pyqtSignal(int)
    cooling_off = QtCore.pyqtSignal()
    laser = QtCore.pyqtSignal(bool)                 # включить/выключить лазер
    sync_out = QtCore.pyqtSignal(int, int)          # (номер выхода, режим)

    def __init__(self, parent=None):
        super().__init__("Камера ПЗС", parent)
        form = QtWidgets.QFormLayout(self)
        self.status = QtWidgets.QLabel("не подключена")
        self.status.setWordWrap(True)
        form.addRow(self.status)
        self.temp = _big(QtWidgets.QLabel("— °C"))
        self.cooling = QtWidgets.QLabel("")
        form.addRow("Температура:", _row(self.temp, self.cooling, 1))
        self.target = QtWidgets.QSpinBox()
        self.target.setRange(-60, 40)
        self.target.setSuffix(" °C")
        self.target.setMaximumWidth(90)
        self.btn_on = QtWidgets.QPushButton("Задать")
        self.btn_off = QtWidgets.QPushButton("Выкл.")
        self.btn_on.setToolTip("Установить температуру ПЗС")
        self.btn_off.setToolTip("Выключить охлаждение. Охлаждённую ниже 0 °C матрицу программа предложит "
                                "сначала плавно отогреть до +2 °C")
        form.addRow("Уставка:", _row(self.target, self.btn_on, self.btn_off))
        self.btn_on.clicked.connect(lambda: self.cooling_on.emit(self.target.value()))
        self.btn_off.clicked.connect(self.cooling_off.emit)

        # лазер и выходы синхронизации камеры (как «Выход синхр. 1/2» в SpectraSP)
        self.btn_laser = QtWidgets.QPushButton("Включить")
        self.btn_laser.setCheckable(True)
        self.btn_laser.setToolTip("Лазер, подключённый к выходу синхронизации камеры (какому — «Настройка → Настройки…»)")
        self.lbl_laser = QtWidgets.QLabel("")
        self.lbl_laser.setStyleSheet("color: #555;")
        form.addRow("Лазер:", _row(self.btn_laser, self.lbl_laser, 1))
        self.sync_boxes = []
        for n in (1, 2):
            cb = QtWidgets.QComboBox()
            for mode, text in SYNC_OUT_MODES.items():
                cb.addItem(text, mode)
                cb.setItemData(cb.count() - 1, f"В SpectraSP: {SYNC_OUT_SPECTRASP[mode]}", QtCore.Qt.ToolTipRole)
            cb.setToolTip(f"Режим выхода синхронизации {n} камеры (разъём на камере). «Логическая 1/0» — "
                          "постоянный уровень, остальные — импульсы, привязанные к экспозиции")
            cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(12)
            cb.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            cb.activated.connect(lambda _i, n=n, cb=cb: self.sync_out.emit(n, cb.currentData()))
            form.addRow(f"Синхр. {n}:", cb)
            self.sync_boxes.append(cb)
        self.btn_laser.clicked.connect(lambda checked: self.laser.emit(checked))
        self._laser_output = 0
        self.set_connected(False)

    def set_sync(self, modes: Sequence[int], laser_output: int, laser_on: bool) -> None:
        """Показать режимы выходов и состояние лазера (без сигналов)."""
        for cb, mode in zip(self.sync_boxes, modes):
            cb.blockSignals(True)
            i = cb.findData(mode)
            cb.setCurrentIndex(i)
            cb.blockSignals(False)
        self._laser_output = laser_output
        self.btn_laser.blockSignals(True)
        self.btn_laser.setChecked(laser_on)
        self.btn_laser.blockSignals(False)
        self.btn_laser.setText("Выключить" if laser_on else "Включить")
        self.btn_laser.setStyleSheet("background: #d62728; color: white; font-weight: bold;" if laser_on else "")
        if laser_output in (1, 2):
            self.lbl_laser.setText(("<b style='color:#b00020'>ВКЛ</b>" if laser_on else "выкл.")
                                   + f" (синхр. {laser_output})")
        else:
            self.lbl_laser.setText("не подключён (см. настройки)")
        self.btn_laser.setEnabled(self.btn_on.isEnabled() and laser_output in (1, 2))

    def set_info(self, info: Optional[CameraInfo], ip: str) -> None:
        if info is None:
            self.status.setText(f"не подключена (адрес из SpectraSP: {ip or 'не найден'})")
            return
        self.status.setText(f"{info.model} № {info.serial or '—'}, {info.width}×{info.height}, "
                            f"пиксель {info.pixel_width_um:g} мкм\nIP {ip}, драйвер {info.dll_name}")
        if info.temp_max > info.temp_min:
            self.target.setRange(info.temp_min, info.temp_max)

    def set_state(self, st: Optional[CameraState], note: str = "", warn: bool = False) -> None:
        """note — пояснение вместо состояния охлаждения (например, «выход на уставку…»)."""
        if st is None or st.temperature is None:
            self.temp.setText("— °C")
            self.cooling.setText("")
            return
        self.temp.setText(f"{st.temperature:+.0f} °C")
        txt = note or st.cooling_text
        if st.overheat:
            txt += ", ПЕРЕГРЕВ!"
        self.cooling.setText(txt)
        self.cooling.setWordWrap(True)
        self.cooling.setStyleSheet("color: #b00020;" if st.overheat else ("color: #a15c00;" if warn else "color: #555;"))

    def set_connected(self, on: bool) -> None:
        for w in (self.target, self.btn_on, self.btn_off, *self.sync_boxes):
            w.setEnabled(on)
        self.btn_laser.setEnabled(on and self._laser_output in (1, 2))


class AcquisitionPanel(QtWidgets.QGroupBox):
    changed = QtCore.pyqtSignal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__("Экспозиция", parent)
        self.s = settings
        form = QtWidgets.QFormLayout(self)
        self.exposure = QtWidgets.QDoubleSpinBox()
        self.exposure.setRange(0.01, 3_600_000)
        self.exposure.setDecimals(2)
        self.exposure.setSuffix(" мс")
        self.exposure.setKeyboardTracking(False)
        self.exposure.setToolTip("Время экспозиции одного кадра")
        form.addRow("Время:", self.exposure)
        self.accum = QtWidgets.QSpinBox()
        self.accum.setRange(1, 100000)
        self.accum.setKeyboardTracking(False)
        self.accum_mode = QtWidgets.QComboBox()
        self.accum_mode.addItem("среднее", "average")
        self.accum_mode.addItem("сумма", "sum")
        form.addRow("Накоплений:", _row(self.accum, self.accum_mode))
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("Спектр (полоса строк)", ACQ_SPECTRUM)
        self.mode.addItem("Изображение (весь кадр)", ACQ_IMAGE)
        self.mode.setToolTip("«Изображение» — для юстировки и выбора полосы; «Спектр» — для измерений (быстро)")
        form.addRow("Режим:", self.mode)
        self.top = QtWidgets.QSpinBox()
        self.bottom = QtWidgets.QSpinBox()
        for w in (self.top, self.bottom):
            w.setRange(0, 4095)
            w.setKeyboardTracking(False)
        form.addRow("Строки ПЗС:", _row(self.top, QtWidgets.QLabel("…"), self.bottom, 1))
        self.hw_bin = QtWidgets.QCheckBox("аппаратное объединение строк")
        self.hw_bin.setToolTip("Камера сама суммирует строки полосы и передаёт одну строку:\n"
                               "считывание в сотни раз быстрее, чем полный кадр.\n"
                               "Если в ярких линиях появляется насыщение — уменьшите полосу или экспозицию.")
        form.addRow(self.hw_bin)
        self.speed = QtWidgets.QComboBox()
        self.speed.setToolTip("Скорость оцифровки: ниже — меньше шум считывания, выше — быстрее")
        form.addRow("Считывание:", self.speed)
        self.flip = QtWidgets.QCheckBox("развернуть спектр по горизонтали")
        form.addRow(self.flip)
        self.estimate = QtWidgets.QLabel("")
        self.estimate.setStyleSheet("color: #555;")
        form.addRow(self.estimate)
        self.load_from_settings()
        for w in (self.exposure, self.accum, self.top, self.bottom):
            w.valueChanged.connect(self._store)
        for w in (self.accum_mode, self.mode, self.speed):
            w.currentIndexChanged.connect(self._store)
        for w in (self.hw_bin, self.flip):
            w.toggled.connect(self._store)

    def load_from_settings(self) -> None:
        s = self.s
        widgets = (self.exposure, self.accum, self.accum_mode, self.mode, self.top, self.bottom,
                   self.hw_bin, self.flip, self.speed)
        for w in widgets:
            w.blockSignals(True)
        self.exposure.setValue(s.exposure_ms)
        self.accum.setValue(s.accumulations)
        self.accum_mode.setCurrentIndex(max(0, self.accum_mode.findData(s.accumulate_mode)))
        self.mode.setCurrentIndex(max(0, self.mode.findData(s.acq_mode)))
        self.top.setValue(max(0, s.rows_top))
        self.bottom.setValue(max(0, s.rows_bottom))
        self.hw_bin.setChecked(s.hardware_binning)
        self.flip.setChecked(s.flip_x)
        idx = self.speed.findData(s.readout_speed)
        if idx >= 0:
            self.speed.setCurrentIndex(idx)
        for w in widgets:
            w.blockSignals(False)
        self._update_enabled()

    def set_camera(self, info: Optional[CameraInfo]) -> None:
        self.speed.blockSignals(True)
        self.speed.clear()
        if info:
            for sp in info.readout_speeds:
                self.speed.addItem(f"{sp} кГц", sp)
            self.top.setMaximum(info.height - 1)
            self.bottom.setMaximum(info.height - 1)
        idx = self.speed.findData(self.s.readout_speed)
        if idx >= 0:
            self.speed.setCurrentIndex(idx)
        self.speed.blockSignals(False)
        self._store()

    def _update_enabled(self) -> None:
        spectrum = self.mode.currentData() == ACQ_SPECTRUM
        self.hw_bin.setEnabled(spectrum)

    def _store(self) -> None:
        s = self.s
        s.exposure_ms = self.exposure.value()
        s.accumulations = self.accum.value()
        s.accumulate_mode = self.accum_mode.currentData()
        s.acq_mode = self.mode.currentData()
        top, bottom = self.top.value(), self.bottom.value()
        if bottom < top:
            top, bottom = bottom, top
        s.rows_top, s.rows_bottom = top, bottom
        s.hardware_binning = self.hw_bin.isChecked()
        s.flip_x = self.flip.isChecked()
        if self.speed.currentData() is not None:
            s.readout_speed = int(self.speed.currentData())
        self._update_enabled()
        self.changed.emit()

    def set_rows(self, top: int, bottom: int) -> None:
        self.top.blockSignals(True)
        self.bottom.blockSignals(True)
        self.top.setValue(top)
        self.bottom.setValue(bottom)
        self.top.blockSignals(False)
        self.bottom.blockSignals(False)
        self._store()

    def set_estimate(self, seconds: Optional[float]) -> None:
        self.estimate.setText("" if seconds is None else f"≈ {seconds:.2f} с на кадр")


class MonoPanel(QtWidgets.QGroupBox):
    goto = QtCore.pyqtSignal(float)
    grating = QtCore.pyqtSignal(int)
    slit = QtCore.pyqtSignal(int, float)
    mirror = QtCore.pyqtSignal(int)
    shutter = QtCore.pyqtSignal(bool)
    service = QtCore.pyqtSignal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__("Монохроматор", parent)
        self.s = settings
        form = QtWidgets.QFormLayout(self)
        self.status = QtWidgets.QLabel("не подключён")
        self.status.setWordWrap(True)
        from .icons import gear_icon
        self.btn_service = QtWidgets.QToolButton()
        self.btn_service.setIcon(gear_icon())
        self.btn_service.setIconSize(QtCore.QSize(22, 22))
        self.btn_service.setAutoRaise(True)
        self.btn_service.setToolTip("Сервис монохроматора: шторка, зеркало, щели, решётки, "
                                    "шаговые двигатели, сброс механизмов, штатное окно DevCtrl")
        self.btn_service.clicked.connect(self.service.emit)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.btn_service, 0, QtCore.Qt.AlignTop)
        form.addRow(top)
        self.wl = _big(QtWidgets.QLabel("— нм"), 16)
        self.busy = QtWidgets.QLabel("")
        self.busy.setStyleSheet("color: #a15c00;")
        form.addRow("Длина волны:", _row(self.wl, self.busy, 1))
        self.target = QtWidgets.QDoubleSpinBox()
        self.target.setRange(0, 3000)
        self.target.setDecimals(3)
        self.target.setSuffix(" нм")
        self.btn_go = QtWidgets.QPushButton("Перейти")
        form.addRow("Установить:", _row(self.target, self.btn_go))
        self.presets = QtWidgets.QComboBox()
        self.presets.setToolTip("Список длин волн (из SpectraSP)")
        form.addRow("Список:", self.presets)
        self.gratings = QtWidgets.QComboBox()
        self.btn_grating = QtWidgets.QPushButton("Сменить")
        form.addRow("Решётка:", _row(self.gratings, self.btn_grating))
        self.slit_idx = QtWidgets.QComboBox()
        self.slit_w = QtWidgets.QDoubleSpinBox()
        self.slit_w.setRange(0, 3000)
        self.slit_w.setDecimals(1)
        self.slit_w.setSuffix(" мкм")
        self.btn_slit = QtWidgets.QPushButton("Уст.")
        form.addRow("Щель:", _row(self.slit_idx, self.slit_w, self.btn_slit))
        self.mirror_box = QtWidgets.QComboBox()
        self.mirror_box.setToolTip("Выходное зеркало: на ПЗС (верх) или на боковую щель")
        form.addRow("Выход:", self.mirror_box)
        self.btn_shutter = QtWidgets.QPushButton("Шторка: ?")
        self.btn_shutter.setCheckable(True)
        form.addRow("Шторка:", self.btn_shutter)
        self._fill_presets(settings.wl_presets)
        self.btn_go.clicked.connect(lambda: self.goto.emit(self.target.value()))
        self.target.lineEdit().returnPressed.connect(lambda: self.goto.emit(self.target.value()))
        self.presets.activated.connect(self._preset_chosen)
        self.btn_grating.clicked.connect(lambda: self.grating.emit(self.gratings.currentData()))
        self.btn_slit.clicked.connect(lambda: self.slit.emit(self.slit_idx.currentData(), self.slit_w.value()))
        self._slit_max: list = []
        self.slit_idx.currentIndexChanged.connect(self._slit_range)
        self.mirror_box.activated.connect(lambda i: self.mirror.emit(self.mirror_box.itemData(i)))
        self.btn_shutter.clicked.connect(lambda checked: self.shutter.emit(checked))
        self.set_connected(False)

    def _fill_presets(self, presets: Sequence[float]) -> None:
        self.presets.clear()
        self.presets.addItem("— выбрать —", None)
        for v in presets:
            self.presets.addItem(f"{v:g} нм", float(v))

    def _preset_chosen(self, idx: int) -> None:
        v = self.presets.itemData(idx)
        if v is not None:
            self.target.setValue(v)

    def set_info(self, info: Optional[MonoInfo], address: str) -> None:
        if info is None:
            self.status.setText(f"не подключён (адрес из DevCtrl.ini: {address or 'не найден'})")
            return
        self.status.setText(f"{info.model or 'монохроматор'} № {info.serial or '—'}, {address}")
        self.gratings.clear()
        for g in info.gratings:
            self.gratings.addItem(g.title, g.index)
        self._slit_max = list(info.slit_max_um)
        self.slit_idx.clear()
        for i, name in enumerate(info.slits or ["Входная щель"]):
            self.slit_idx.addItem(name, i)
        self._slit_range()
        self.mirror_box.clear()
        for i, name in enumerate(info.mirror_positions):
            self.mirror_box.addItem(name, i)
        lo, hi = info.wl_range
        if hi > lo:
            self.target.setRange(lo, hi)

    def _slit_range(self) -> None:
        """Поле ширины — не шире, чем позволяет щель (из файла прибора)."""
        i = self.slit_idx.currentData()
        mx = self._slit_max[i] if i is not None and 0 <= i < len(self._slit_max) else 0
        self.slit_w.setMaximum(mx or 3000)
        self.slit_w.setToolTip(f"0…{mx:g} мкм" if mx else "")

    def set_connected(self, on: bool) -> None:
        for w in (self.target, self.btn_go, self.presets, self.gratings, self.btn_grating, self.slit_idx,
                  self.slit_w, self.btn_slit, self.mirror_box, self.btn_shutter):
            w.setEnabled(on)

    def set_service_enabled(self, on: bool) -> None:
        self.btn_service.setEnabled(on)

    def set_wavelength(self, wl: Optional[float], grating: Optional[int]) -> None:
        self.wl.setText("— нм" if wl is None else f"{wl:.3f} нм")
        if wl is not None and not self.target.hasFocus():
            self.target.setValue(wl)
        if grating is not None:
            idx = self.gratings.findData(grating)
            if idx >= 0:
                self.gratings.setCurrentIndex(idx)

    def set_busy(self, busy: bool, text: str = "движение…") -> None:
        self.busy.setText(text if busy else "")

    def set_slit_value(self, width: Optional[float]) -> None:
        if width is not None and not self.slit_w.hasFocus():
            self.slit_w.setValue(width)

    def set_mirror_value(self, pos: Optional[int]) -> None:
        if pos is None:
            return
        idx = self.mirror_box.findData(pos)
        if idx >= 0:
            self.mirror_box.setCurrentIndex(idx)

    def set_shutter_value(self, is_open: Optional[bool]) -> None:
        self.btn_shutter.blockSignals(True)
        self.btn_shutter.setChecked(bool(is_open))
        self.btn_shutter.setText("Шторка: ?" if is_open is None else
                                 ("открыта (нажать — закрыть)" if is_open else "закрыта (нажать — открыть)"))
        self.btn_shutter.blockSignals(False)


class ProcessingPanel(QtWidgets.QGroupBox):
    changed = QtCore.pyqtSignal()
    take_background = QtCore.pyqtSignal()
    reset_background = QtCore.pyqtSignal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__("Обработка", parent)
        self.s = settings
        form = QtWidgets.QFormLayout(self)
        self.subtract = QtWidgets.QCheckBox("вычитать темновой фон")
        self.bg_status = QtWidgets.QLabel("фон не снят")
        self.bg_status.setStyleSheet("color: #555;")
        form.addRow(self.subtract)
        self.btn_bg = QtWidgets.QPushButton("Снять фон")
        self.btn_bg_reset = QtWidgets.QPushButton("Сбросить")
        form.addRow(_row(self.btn_bg, self.btn_bg_reset, self.bg_status, 1))
        self.close_shutter = QtWidgets.QCheckBox("закрывать шторку при съёмке фона")
        form.addRow(self.close_shutter)
        self.show_bg = QtWidgets.QCheckBox("показывать фон на графике")
        form.addRow(self.show_bg)
        self.cosmics = QtWidgets.QCheckBox("удалять космические частицы")
        self.cosmics.setToolTip("При накоплении ≥3 кадров — отбраковка по медиане, иначе — удаление одиночных выбросов")
        form.addRow(self.cosmics)
        self.peaks = QtWidgets.QCheckBox("искать пики")
        form.addRow(self.peaks)
        note = QtWidgets.QLabel("Единицы и диапазон осей — на панели над графиком.")
        note.setStyleSheet("color: #666;")
        note.setWordWrap(True)
        form.addRow(note)
        self.subtract.setChecked(settings.subtract_background)
        self.close_shutter.setChecked(settings.close_shutter_for_background)
        self.cosmics.setChecked(settings.remove_cosmics)
        self.peaks.setChecked(True)
        for w in (self.subtract, self.close_shutter, self.cosmics, self.show_bg, self.peaks):
            w.toggled.connect(self._store)
        self.btn_bg.clicked.connect(self.take_background.emit)
        self.btn_bg_reset.clicked.connect(self.reset_background.emit)

    def _store(self) -> None:
        s = self.s
        s.subtract_background = self.subtract.isChecked()
        s.close_shutter_for_background = self.close_shutter.isChecked()
        s.remove_cosmics = self.cosmics.isChecked()
        self.changed.emit()

    def set_background_status(self, has: bool, text: str = "") -> None:
        self.bg_status.setText(text or ("фон снят" if has else "фон не снят"))
        self.bg_status.setStyleSheet("color: #2a7a2a;" if has else "color: #555;")
