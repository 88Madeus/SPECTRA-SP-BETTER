"""Главное окно программы."""
from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
import os
import threading
import time
from typing import Callable, Optional

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from .. import APP_NAME, __version__
from .. import processing as P
from ..calibration import WavelengthAxis
from ..config import COOLING_TARGET_DEFAULT_C, AppSettings
from ..hw.base import SYNC_LOGICAL_1, WARM_UP_TARGET_C, DeviceError
from ..hw.winutil import spectrasp_is_running
from ..buffer import SpectrumBuffer
from ..instrument import Instrument, Measurement
from ..reference_lines import nearest_line
from ..storage import load_image, load_spectrum, save_image, save_spectrum, unique_path
from ..units import (X_DECIMALS, X_EV, X_NM, X_PX, X_RAMAN, X_SHORT, X_UNITS, X_WAVENUMBER, AxisMap, axis_label,
                     from_nm, is_spectral)
from .bands import BandHistory, BandsHistoryWindow, BandsPanel
from .calculator import CalculatorDialog
from .buffer_panel import BufferPanel, entry_color
from .tools_dialogs import DefectPixelsDialog, IntensityCorrectionDialog, StripesDialog
from .dialogs import BusyDialog, CalibrationDialog, PanoramaDialog, SeriesDialog, SettingsDialog
from .icons import gear_icon
from .log_widget import LogView, QtLogHandler
from .panels import AcquisitionPanel, CameraPanel, MonoPanel, ProcessingPanel
from .plots import ImagePanel, SpectrumPlot
from .rows_panel import RowsPanel
from .tasks import Emitter, TaskRunner

log = logging.getLogger(__name__)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, settings: AppSettings, simulate: bool, log_handler: QtLogHandler):
        super().__init__()
        self.s = settings
        self.simulate = simulate
        self.inst = Instrument(settings, simulate=simulate)
        self.tasks = TaskRunner(self)
        self.emitter = Emitter()
        self.current: Optional[Measurement] = None
        self.background: Optional[Measurement] = None
        self.buffer = SpectrumBuffer()
        self.band_history = BandHistory()
        self._hist_win: Optional[BandsHistoryWindow] = None
        self._calc: Optional[CalculatorDialog] = None
        self._series_beep = False
        self._scale = None                        # (пересчёт единиц, единицы) последней перерисовки
        try:
            n = self.buffer.load_state(self._buffer_path())
            if n:
                log.info("Буфер спектров: загружено %d спектр(ов) с прошлого сеанса", n)
        except Exception as e:  # noqa: BLE001 — повреждённый файл (обрыв записи и т. п.) не должен мешать запуску
            self.buffer.clear()
            bad = self._buffer_path() + ".bad"
            try:
                os.replace(self._buffer_path(), bad)
            except OSError:
                bad = ""
            log.warning("Буфер спектров прошлого сеанса не прочитан (%s)%s", e,
                        f"; файл отложен как {bad}" if bad else "")
        self._acq_thread: Optional[threading.Thread] = None
        self._acq_stop = threading.Event()
        self._acq_kind = ""
        self._poll_cam = False
        self._poll_mono = False
        self._mono_was_busy = False
        self._poll_shutter = False
        self._mono_moving = False         # выполняется команда с панели «Монохроматор» (движение, шторка)
        self._service_cmd = False         # выполняется команда из окна «Сервис монохроматора»
        self._dev_task = ""               # идёт подключение/отключение приборов — что именно ("" — нет)
        self._dev_cam = False             # … и затрагивает ли оно камеру
        self._dev_mono = False            # … и монохроматор
        self._series_moves_mono = False   # текущая серия сама переставляет монохроматор (список длин волн)
        self._closing = False
        self._service_dlg = None
        # защита ПЗС
        self._warm_then_off = False       # идёт плавный отогрев перед выключением охлаждения
        self._overheat_count = 0          # сколько опросов подряд камера сообщает о перегреве
        self._overheat_handled = False
        self._fan_warned = False
        self._foreign = False             # запущена SpectraSP/DevCtrl (две программы на одних приборах)
        self._temp_block = ""             # почему съёмка недоступна из-за температуры ПЗС ("" — доступна)
        self._poll_ticks = 0
        self._image_meas: Optional[Measurement] = None   # измерение, из которого показан кадр ПЗС
        self._rows_unit = X_PX                            # единицы оси на вкладке «Строки ПЗС»

        title = f"{APP_NAME} {__version__}" + ("  [ИМИТАЦИЯ]" if simulate else "")
        self.setWindowTitle(title)
        self.resize(1400, 900)
        self._build_ui(log_handler)
        self._connect_signals()
        self._update_actions()
        self._refresh_static_info()

        self.poll_timer = QtCore.QTimer(self)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(1500)
        if self.s.window_state:
            try:
                self.restoreState(QtCore.QByteArray.fromBase64(self.s.window_state.encode("ascii")))
            except Exception:  # noqa: BLE001
                pass
        log.info("%s %s запущена%s", APP_NAME, __version__, " в режиме имитации" if simulate else "")
        if not simulate and not self.inst.setup:
            QtCore.QTimer.singleShot(300, self._ask_spectrasp_dir)

    # ================================================================ интерфейс
    def _build_ui(self, log_handler: QtLogHandler) -> None:
        st = self.style()
        A = QtWidgets.QAction

        def act(text, icon=None, shortcut=None, tip=None, checkable=False):
            a = A(st.standardIcon(icon) if icon is not None else QtGui.QIcon(), text, self)
            if shortcut:
                a.setShortcut(QtGui.QKeySequence(shortcut))
            if tip:
                a.setToolTip(tip)
                a.setStatusTip(tip)
            a.setCheckable(checkable)
            return a

        SP = QtWidgets.QStyle
        self.act_connect = act("Подключить", SP.SP_DialogApplyButton, "Ctrl+K", "Подключить камеру и монохроматор")
        self.act_disconnect = act("Отключить", SP.SP_DialogCancelButton, None, "Отключить приборы")
        self.act_reconnect_cam = act("Переподключить камеру", None, None, "Закрыть и заново открыть камеру")
        self.act_service = act("Сервис прибора", None, "Ctrl+Shift+D",
                               "Сервис монохроматора: шторка, зеркало, щели, решётки, шаговые двигатели, "
                               "сброс механизмов, штатное окно DevCtrl")
        self.act_service.setIcon(gear_icon())
        self.act_snap = act("Снимок", SP.SP_MediaPlay, "F5", "Снять один спектр (с накоплением)")
        self.act_live = act("Непрерывно", SP.SP_BrowserReload, "F6", "Непрерывная съёмка")
        self.act_stop = act("Стоп", SP.SP_MediaStop, "Esc", "Остановить съёмку / прервать экспозицию")
        self.act_bg = act("Фон", SP.SP_DialogResetButton, "F8", "Снять темновой фон (шторка закрывается)")
        self.act_series = act("Серия…", SP.SP_FileDialogListView, None, "Серия спектров с сохранением")
        self.act_pano = act("Панорама…", SP.SP_ArrowRight, None, "Спектр широкого диапазона со сшивкой")
        self.act_save = act("Сохранить спектр…", SP.SP_DialogSaveButton, "Ctrl+S", "Сохранить текущий спектр")
        self.act_save_img = act("Сохранить кадр…", None, None, "Сохранить изображение ПЗС")
        self.act_open_frame = act("Открыть кадр ПЗС…", None, None,
                                  "Посмотреть сохранённый кадр (.npy, .txt, .csv, 16-битный TIFF SpectraSP) "
                                  "и построить спектры полос по нему")
        self.act_export = act("Сохранить график…", None, "Ctrl+Shift+S",
                              "Картинка PNG или SVG того, что сейчас на графике (спектр или кадр ПЗС) — для отчёта")
        self.act_open = act("Открыть спектр…", SP.SP_DialogOpenButton, "Ctrl+O",
                            "Загрузить спектры из файлов в буфер для сравнения")
        self.act_pin = act("В буфер", SP.SP_DialogYesButton, "Ctrl+D",
                           "Положить текущий спектр в буфер спектров — для сравнения с другими")
        self.act_clear = act("Очистить буфер", SP.SP_TrashIcon, None, "Удалить все спектры из буфера")
        self.act_autorange = act("Весь спектр", SP.SP_TitleBarMaxButton, "Ctrl+A",
                                 "Показать весь спектр по X и подобрать масштаб по Y")
        self.act_calib = act("Калибровка шкалы…", None, None, "Центральный пиксель, модель, поправка по реперным линиям")
        self.act_settings = act("Настройки…", SP.SP_FileDialogDetailedView, None, "Каталог SpectraSP, таймауты")
        self.act_calc = act("Калькулятор спектров…", None, "Ctrl+M",
                            "Разность, отношение, пропускание, оптическая плотность, нормировка")
        self.act_bands_peaks = act("Участки по пикам", None, None,
                                   "Создать участки интегрирования вокруг найденных пиков текущего спектра")
        self.act_band_hist = act("История участков…", None, None, "Результаты участков интегрирования во времени")
        self.act_stripes = act("Полосы строк…", None, None, "Несколько полос строк ПЗС — свой спектр с каждой")
        self.act_defects = act("Дефектные пиксели…", None, None, "Список «горячих» и «мёртвых» столбцов ПЗС")
        self.act_intensity = act("Коррекция интенсивности…", None, None,
                                 "Поправка на спектральную чувствительность, расчёт по эталонной лампе")
        self.act_about = act("О программе", SP.SP_MessageBoxInformation)
        self.act_exit = act("Выход", None, "Alt+F4")

        tb = self.addToolBar("Основные")
        tb.setObjectName("main_toolbar")
        tb.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        for a in (self.act_connect, self.act_disconnect, self.act_service, None, self.act_snap, self.act_live,
                  self.act_stop,
                  self.act_bg, None, self.act_series, self.act_pano, None, self.act_save, self.act_open,
                  self.act_pin, self.act_clear, self.act_autorange):
            tb.addSeparator() if a is None else tb.addAction(a)

        mb = self.menuBar()
        m = mb.addMenu("&Файл")
        for a in (self.act_open, self.act_open_frame, self.act_save, self.act_save_img, self.act_export, None,
                  self.act_exit):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Прибор")
        for a in (self.act_connect, self.act_disconnect, self.act_reconnect_cam, None, self.act_service):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Измерение")
        for a in (self.act_snap, self.act_live, self.act_stop, None, self.act_bg, self.act_series, self.act_pano):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Вид")
        for a in (self.act_autorange, self.act_pin, self.act_clear):
            m.addAction(a)
        m = mb.addMenu("&Анализ")
        for a in (self.act_calc, None, self.act_bands_peaks, self.act_band_hist):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Настройка")
        for a in (self.act_calib, self.act_stripes, self.act_defects, self.act_intensity, self.act_settings):
            m.addAction(a)
        m = mb.addMenu("&Справка")
        m.addAction(self.act_about)

        # центр
        self.tabs = QtWidgets.QTabWidget()
        self.plot = SpectrumPlot(self.s)
        self.image = ImagePanel()
        self.rows_panel = RowsPanel()
        self.tabs.addTab(self.plot, "Спектр")
        self.tabs.addTab(self.image, "Изображение ПЗС")
        self.tabs.addTab(self.rows_panel, "Строки ПЗС")
        self.tabs.setTabToolTip(2, "Спектр каждой строки кадра и их среднее (режим «Изображение» или полоса "
                                   "без аппаратного объединения строк)")
        self.setCentralWidget(self.tabs)

        # слева — управление
        self.cam_panel = CameraPanel()
        self.cam_panel.target.setValue(COOLING_TARGET_DEFAULT_C)
        self.acq_panel = AcquisitionPanel(self.s)
        self.mono_panel = MonoPanel(self.s)
        self.proc_panel = ProcessingPanel(self.s)
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        for w in (self.cam_panel, self.acq_panel, self.mono_panel, self.proc_panel):
            lv.addWidget(w)
        lv.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        left.setMinimumWidth(410)
        scroll.setMinimumWidth(440)
        dock = QtWidgets.QDockWidget("Управление", self)
        dock.setObjectName("dock_controls")
        dock.setWidget(scroll)
        dock.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable | QtWidgets.QDockWidget.DockWidgetFloatable)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, dock)

        # справа — пики и наложения
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.addWidget(QtWidgets.QLabel("Пики текущего спектра:"))
        self.peaks_table = QtWidgets.QTableWidget(0, 4)
        self.peaks_table.setHorizontalHeaderLabels(["Положение", "Высота", "Ширина", "Линия"])
        hh = self.peaks_table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        hh.setStretchLastSection(True)
        self.peaks_table.verticalHeader().setVisible(False)
        self.peaks_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        rv.addWidget(self.peaks_table, 1)
        self.analysis_tabs = QtWidgets.QTabWidget()
        self.analysis_tabs.addTab(right, "Пики")
        self.bands_panel = BandsPanel(self.s)
        self.analysis_tabs.addTab(self.bands_panel, "Интегрирование")
        self.analysis_tabs.setMinimumWidth(330)
        rdock = QtWidgets.QDockWidget("Анализ", self)
        rdock.setObjectName("dock_analysis")
        rdock.setWidget(self.analysis_tabs)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, rdock)
        self.buffer_panel = BufferPanel(self.buffer)
        self.buffer_panel.auto.setChecked(self.s.buffer_auto)
        self.buffer_panel.offset.setValue(self.s.buffer_offset)
        bdock = QtWidgets.QDockWidget("Буфер спектров", self)
        bdock.setObjectName("dock_buffer")
        bdock.setWidget(self.buffer_panel)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, bdock)
        self.splitDockWidget(rdock, bdock, QtCore.Qt.Vertical)

        # снизу — журнал
        self.log_view = LogView(log_handler)
        ldock = QtWidgets.QDockWidget("Журнал", self)
        ldock.setObjectName("dock_log")
        ldock.setWidget(self.log_view)
        self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, ldock)
        self.resizeDocks([ldock], [140], QtCore.Qt.Vertical)

        # строка состояния
        sb = self.statusBar()
        self.lbl_cam = QtWidgets.QLabel("Камера: —")
        self.lbl_mono = QtWidgets.QLabel("Монохроматор: —")
        self.lbl_meas = QtWidgets.QLabel("")
        self.lbl_laser = QtWidgets.QLabel("<b style='color:white; background:#d62728'>&nbsp;ЛАЗЕР ВКЛ&nbsp;</b>")
        self.lbl_laser.setVisible(False)
        sb.addPermanentWidget(self.lbl_laser)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        for w in (self.lbl_cam, self.lbl_mono, self.lbl_meas):
            sb.addPermanentWidget(w)
        sb.addPermanentWidget(self.progress)

    def _connect_signals(self) -> None:
        self.act_connect.triggered.connect(self.connect_devices)
        self.act_disconnect.triggered.connect(self.disconnect_devices)
        self.act_reconnect_cam.triggered.connect(self.reconnect_camera)
        self.act_service.triggered.connect(self.mono_service)
        self.mono_panel.service.connect(self.mono_service)
        self.act_snap.triggered.connect(self.snap)
        self.act_live.triggered.connect(self.live)
        self.act_stop.triggered.connect(self.stop)
        self.act_bg.triggered.connect(self.take_background)
        self.act_series.triggered.connect(self.series)
        self.act_pano.triggered.connect(self.panorama)
        self.act_save.triggered.connect(self.save_current)
        self.act_save_img.triggered.connect(self.save_frame)
        self.act_open.triggered.connect(self.open_spectrum)
        self.act_pin.triggered.connect(self.pin_current)
        self.act_clear.triggered.connect(self.buffer_panel._ask_clear)
        self.act_autorange.triggered.connect(self.plot.show_all)
        self.plot.units_changed.connect(self._display_settings_changed)
        self.act_calib.triggered.connect(self.calibration)
        self.act_settings.triggered.connect(self.settings_dialog)
        self.act_about.triggered.connect(self.about)
        self.act_exit.triggered.connect(self.close)
        self.bands_panel.changed.connect(self._redraw)
        self.act_calc.triggered.connect(self.calculator)
        self.act_export.triggered.connect(self.export_plot)
        self.act_open_frame.triggered.connect(self.open_frame)
        self.act_bands_peaks.triggered.connect(self.bands_from_peaks)
        self.act_band_hist.triggered.connect(self.show_band_history)
        self.bands_panel.track_changed.connect(lambda _: self._redraw())
        self.bands_panel.add_requested.connect(self.add_band)
        self.bands_panel.history_requested.connect(self.show_band_history)
        self.plot.band_moved.connect(self._band_moved)
        bp = self.buffer_panel
        bp.changed.connect(self._redraw)
        bp.add_current.connect(self.pin_current)
        bp.load_files.connect(self.open_spectrum)
        bp.save_entries.connect(self.save_buffer_entries)
        bp.make_current.connect(self.buffer_to_current)
        bp.auto.toggled.connect(lambda on: setattr(self.s, "buffer_auto", on))
        bp.offset.valueChanged.connect(lambda v: setattr(self.s, "buffer_offset", float(v)))

        self.cam_panel.cooling_on.connect(self.cooling_on)
        self.cam_panel.cooling_off.connect(self.cooling_off)
        self.cam_panel.laser.connect(self.set_laser)
        self.cam_panel.sync_out.connect(self.set_sync_out)
        self.acq_panel.changed.connect(self._acq_settings_changed)
        self.image.stripes_changed.connect(self._stripes_dragged)
        self.tabs.currentChanged.connect(lambda _: self._update_rows_tab())
        self.rows_panel.save_requested.connect(self.save_rows)
        self.acq_panel.stripes_requested.connect(self.edit_stripes)
        self.act_stripes.triggered.connect(self.edit_stripes)
        self.proc_panel.defects_requested.connect(self.edit_defects)
        self.act_defects.triggered.connect(self.edit_defects)
        self.proc_panel.intensity_requested.connect(self.edit_intensity)
        self.act_intensity.triggered.connect(self.edit_intensity)
        self.mono_panel.goto.connect(self.goto_wavelength)
        self.mono_panel.grating.connect(self.change_grating)
        self.mono_panel.slit.connect(self.set_slit)
        self.mono_panel.mirror.connect(self.set_mirror)
        self.mono_panel.shutter.connect(self.set_shutter)
        self.mono_panel.shutter_ttl.connect(self.set_shutter_ttl)
        self.proc_panel.changed.connect(self._display_settings_changed)
        self.proc_panel.take_background.connect(self.take_background)
        self.proc_panel.reset_background.connect(self.reset_background)

        self.emitter.measurement.connect(self._on_measurement)
        self.emitter.progress.connect(self._on_progress)
        self.emitter.message.connect(lambda t: self.statusBar().showMessage(t, 8000))
        self.emitter.error.connect(self._on_acq_error)
        self.emitter.finished.connect(self._on_acq_finished)

    # ================================================================ состояние
    def _refresh_static_info(self) -> None:
        self.cam_panel.set_info(self.inst.camera.info if self.inst.camera_connected else None, self.inst.camera_ip())
        self.mono_panel.set_info(self.inst.mono.info if self.inst.mono_connected else None, self.inst.mono_address())
        self.image.set_stripes(self.inst.stripes())

    def _acquiring(self) -> bool:
        return self._acq_thread is not None and self._acq_thread.is_alive()

    def _mono_lock_reason(self, for_service: bool = False) -> str:
        """Почему ручное управление монохроматором сейчас недоступно ("" — доступно).
        for_service — для окна «Сервис монохроматора» (у него и у панели — общая очередь команд)."""
        if self._dev_mono:
            return self._dev_task
        if self._foreign:
            return "запущена SpectraSP или DevCtrl — закройте её"
        acq = self._acquiring()
        if acq and self._acq_kind == "panorama":
            return "идёт съёмка панорамы"
        if acq and self._acq_kind == "background":
            return "снимается темновой фон (шторка закрыта программой)"
        if acq and self._acq_kind == "series" and self._series_moves_mono:
            return "идёт серия по списку длин волн"
        excl = self.inst._mono_exclusive
        if excl is not None:
            return f"монохроматором управляет {excl[0]}"
        if for_service and self._mono_moving:
            return "выполняется команда с панели «Монохроматор»"
        if not for_service and self._service_cmd:
            return "выполняется команда в окне «Сервис монохроматора»"
        return ""

    def _data_lock_reason(self) -> str:
        """Почему нельзя менять параметры, от которых зависят данные ("" — можно): серия и панорама
        снимаются с одними и теми же экспозицией, полосами, фоном и коррекциями."""
        if self._acquiring() and self._acq_kind in ("series", "panorama"):
            return "идёт серия" if self._acq_kind == "series" else "идёт панорама"
        return ""

    def _refuse_during_run(self, what: str) -> bool:
        reason = self._data_lock_reason()
        if reason:
            self.statusBar().showMessage(f"{what}: недоступно, {reason}", 5000)
        return bool(reason)

    def _mono_ready(self) -> bool:
        """Можно ли сейчас отправить ручную команду монохроматору (иначе — объяснить почему)."""
        if not self.inst.mono_connected:
            return False
        reason = self._mono_lock_reason()
        if reason:
            self.statusBar().showMessage(f"Монохроматор недоступен: {reason}", 5000)
            return False
        if self._mono_moving:
            self.statusBar().showMessage("Монохроматор ещё выполняет предыдущую команду", 5000)
            return False
        return True

    def _update_actions(self) -> None:
        cam = self.inst.camera_connected
        mono = self.inst.mono_connected
        acq = self._acquiring()
        dev = bool(self._dev_task)
        # подключение и отключение — только одно за раз и не во время съёмки
        self.act_connect.setEnabled(not (cam and mono) and not acq and not dev)
        self.act_disconnect.setEnabled((cam or mono) and not acq and not dev)
        self.act_reconnect_cam.setEnabled(not acq and not dev)
        self.act_settings.setEnabled(not dev)
        self._temp_block = self.inst.temperature_changing()
        free = not acq and not self._foreign and not self._temp_block and not self._dev_cam
        for a in (self.act_snap, self.act_live, self.act_bg, self.act_series):
            a.setEnabled(cam and free)
        # панорама планируется по текущей решётке — не пока монохроматор выполняет команду
        self.act_pano.setEnabled(cam and mono and free and not self._dev_mono and not self._mono_moving
                                 and not self._service_cmd)
        self.act_stop.setEnabled(acq)
        self.act_save.setEnabled(self.current is not None)
        self.act_save_img.setEnabled(self.current is not None and self.current.image is not None)
        self.act_pin.setEnabled(self.current is not None)
        self.act_open_frame.setEnabled(not acq)
        self.act_stripes.setEnabled(not acq)
        run = self._data_lock_reason()
        for a in (self.act_defects, self.act_intensity, self.act_calib):
            a.setEnabled(not run)
        self.acq_panel.set_locked(run, stripes_locked=acq)
        self.proc_panel.set_locked(run)
        self.image.set_stripes_movable(not run)
        self.cam_panel.set_connected(cam and not self._dev_cam)
        lock = self._mono_lock_reason()
        self.mono_panel.set_connected(mono and not self._mono_moving and not lock)
        self.mono_panel.set_lock_reason(lock if mono or self._dev_mono else "")
        self.act_service.setEnabled(mono and not self._dev_mono)
        self.mono_panel.set_service_enabled(mono and not self._dev_mono)
        if self._service_dlg is not None:
            self._service_dlg.set_locked(self._mono_lock_reason(for_service=True) if mono
                                         else "монохроматор не подключён")
        self.proc_panel.btn_bg.setEnabled(cam and free)
        self.lbl_cam.setText("Камера: подключена" if cam else "Камера: —")
        self._update_bg_status()

    def _update_bg_status(self) -> None:
        has = self.inst.has_background()
        txt = ""
        if not has and self.background is not None:
            txt = self.inst.background_problem() or "фон снят с другими параметрами"
        self.proc_panel.set_background_status(has, txt)

    def _error(self, title: str, exc: BaseException) -> None:
        msg = str(exc) or exc.__class__.__name__
        log.error("%s: %s", title, msg)
        if not self._closing:
            QtWidgets.QMessageBox.warning(self, title, msg)

    # ================================================================ подключение
    def _ask_spectrasp_dir(self) -> None:
        QtWidgets.QMessageBox.information(
            self, "Каталог SpectraSP",
            "Не найден каталог установленной SpectraSP (с файлами SSPL_IM.dll, psi_ccd2_ver*.dll и настройками).\n"
            "Укажите его в следующем окне. Обычно это C:\\ProgramData\\SpectraSP for …")
        self.settings_dialog()

    def connect_devices(self) -> None:
        # две программы, одновременно командующие монохроматором и камерой, могут столкнуть
        # механизмы или сбить положение — подключаемся только когда SpectraSP/DevCtrl закрыты
        while not self.simulate and spectrasp_is_running():
            r = QtWidgets.QMessageBox.warning(
                self, "SpectraSP запущена",
                "Запущена оригинальная SpectraSP (или DevCtrl). Две программы не должны одновременно "
                "управлять приборами.\n\nЗакройте SpectraSP/DevCtrl и нажмите «Повторить».",
                QtWidgets.QMessageBox.Retry | QtWidgets.QMessageBox.Cancel)
            if r != QtWidgets.QMessageBox.Retry:
                return
        if self._dev_task:
            return
        self._foreign = False
        cam, mono = not self.inst.camera_connected, not self.inst.mono_connected
        if not (cam or mono):
            return
        # пока драйверы открываются, повторное «Подключить» (и отключение) недоступно: двойной вызов
        # открытия драйвера камеры или монохроматора недопустим
        self._dev_task = "идёт подключение приборов"
        self._dev_cam, self._dev_mono = cam, mono
        self._update_actions()

        def finished(which: str):
            setattr(self, which, False)
            if not (self._dev_cam or self._dev_mono):
                self._dev_task = ""
            self._update_actions()
        if cam:
            self.lbl_cam.setText("Камера: подключение…")
            log.info("Подключение камеры (%s)…", self.inst.camera_ip() or "?")
            self.tasks.run(self.inst.connect_camera,
                           lambda info: (finished("_dev_cam"), self._camera_connected(info)),
                           lambda e: (finished("_dev_cam"), self._error("Камера", e)), "connect-camera")
        if mono:
            self.lbl_mono.setText("Монохроматор: подключение…")
            log.info("Подключение монохроматора (%s)…", self.inst.mono_address() or "?")
            self.tasks.run(self.inst.connect_mono,
                           lambda info: (finished("_dev_mono"), self._mono_connected(info)),
                           lambda e: (finished("_dev_mono"), self._error("Монохроматор", e),
                                      self.lbl_mono.setText("Монохроматор: —")),
                           "connect-mono")

    def _camera_connected(self, info) -> None:
        log.info("Камера подключена: %s № %s, %d×%d, драйвер %s", info.model, info.serial, info.width,
                 info.height, info.dll_name)
        self.cam_panel.set_info(info, self.inst.camera_ip())
        self.acq_panel.set_camera(info)
        self.acq_panel.load_from_settings()
        self.image.set_stripes(self.inst.stripes())
        self.cam_panel.set_state(self.inst.last_state)
        self._show_sync()
        self._acq_settings_changed()
        self._update_actions()

    def _mono_connected(self, info) -> None:
        log.info("Монохроматор подключён: %s № %s", info.model, info.serial)
        self.mono_panel.set_info(info, self.inst.mono_address())
        self._show_shutter_mode()
        self._update_mono_view()
        self._update_actions()
        self._refresh_mono_misc()

    def _show_shutter_mode(self) -> None:
        m = self.inst.mono
        available = bool(m is not None and m.info.has_shutter and m.info.shutter_ttl)
        self.mono_panel.set_shutter_mode(self.inst.shutter_ttl, available)
        self._acq_settings_changed()          # пометка о минимальной экспозиции для шторки «по экспозиции»

    def _refresh_mono_misc(self) -> None:
        """Прочитать положение зеркала, шторки и ширину щели для панели монохроматора."""
        def read_misc():
            out = {}
            for key, fn in (("mirror", self.inst.get_mirror), ("shutter", self.inst.get_shutter),
                            ("slit", lambda: self.inst.get_slit(0))):
                try:
                    out[key] = fn()
                except DeviceError as e:
                    log.info("Не удалось прочитать %s: %s", key, e)
            return out

        def show(d):
            self.mono_panel.set_mirror_value(d.get("mirror"))
            self.mono_panel.set_shutter_value(d.get("shutter"))
            self.mono_panel.set_slit_value(d.get("slit"))
        self.tasks.run(read_misc, show, lambda e: None, "mono-misc")

    def mono_service(self) -> None:
        """Сервисное окно монохроматора (значок шестерёнки)."""
        if not self.inst.mono_connected:
            QtWidgets.QMessageBox.information(self, "Сервис прибора", "Сначала подключите монохроматор.")
            return
        dlg = self._service_dlg
        # окно одно: у второго была бы своя очередь команд, и они шли бы вперемешку
        if dlg is not None and (dlg.mono is self.inst.mono or dlg.command_running):
            dlg.set_locked(self._mono_lock_reason(for_service=True))
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            if not dlg.command_running:
                dlg.refresh()
            return
        from .service_dialog import MonoServiceDialog

        def changed():
            self._update_mono_view()
            self._refresh_mono_misc()
        self._service_dlg = MonoServiceDialog(self.inst, self.tasks, changed, self,
                                              on_command=self._service_command)
        self._service_dlg.set_locked(self._mono_lock_reason(for_service=True))
        self._service_dlg.show()

    def _service_command(self, running: bool) -> None:
        self._service_cmd = running
        self._update_actions()

    def _update_mono_view(self) -> None:
        self.mono_panel.set_wavelength(self.inst.mono_wl, self.inst.mono_grating)
        self.mono_panel.set_slit_value(self.inst.mono_slit)
        if self.inst.mono_connected:
            wl = f"{self.inst.mono_wl:.3f} нм" if self.inst.mono_wl is not None else "?"
            self.lbl_mono.setText(f"Монохроматор: {wl}, {self.inst.grating_title(self.inst.mono_grating)}")
        else:
            self.lbl_mono.setText("Монохроматор: —")

    def _shutdown_dialog(self):
        """Окно ожидания отключения и событие «не ждать отогрева»."""
        cancel = threading.Event()
        warm = self.s.warm_up_on_close and self.inst.camera_connected
        dlg = BusyDialog(self, "Отключение приборов…",
                         "Не ждать отогрева" if warm else "", cancel.set,
                         "Уставка +2 °C останется в камере, ПЗС отогреется сама. "
                         "Не выключайте питание камеры, пока она холодная.")
        dlg.show()
        return dlg, cancel

    def disconnect_devices(self) -> None:
        if self._acquiring() or self._dev_task:
            return
        self._stop_warm_up()
        self._dev_task = "идёт отключение приборов"
        self._dev_cam = self._dev_mono = True
        self._update_actions()
        if self._service_dlg is not None and not self._service_dlg.command_running:
            self._service_dlg.close()
        dlg, cancel = self._shutdown_dialog()
        self.tasks.run(lambda: self.inst.disconnect(say=dlg.text_changed.emit, cancel_warm_up=cancel),
                       lambda _: self._disconnected(dlg),
                       lambda e: (self._disconnected(dlg), self._error("Отключение", e)), "disconnect")

    def _disconnected(self, dlg) -> None:
        self._dev_task = ""
        self._dev_cam = self._dev_mono = False
        dlg.finish()
        self._show_sync()
        log.info("Приборы отключены")
        self._refresh_static_info()
        self.cam_panel.set_state(None)
        self._update_mono_view()
        self._update_actions()

    def reconnect_camera(self) -> None:
        if self._acquiring() or self._dev_task:
            return
        self._dev_task = "идёт переподключение камеры"
        self._dev_cam = True
        self._update_actions()

        def finished():
            self._dev_task = ""
            self._dev_cam = False
            self._update_actions()

        def work():
            if self.inst.camera is not None:
                try:
                    self.inst.cam_thread.call(self.inst.camera.close, False, timeout=30)
                except Exception as e:  # noqa: BLE001
                    log.warning("Закрытие камеры: %s", e)
            return self.inst.connect_camera()
        self.tasks.run(work, lambda info: (finished(), self._camera_connected(info)),
                       lambda e: (finished(), self._error("Камера", e)), "reconnect-camera")

    # ================================================================ опрос
    def _check_foreign(self) -> None:
        """Если при подключённых приборах запустили SpectraSP/DevCtrl — остановить съёмку и заблокировать команды."""
        running = spectrasp_is_running()
        if running and not self._foreign:
            self._foreign = True
            log.error("Запущена SpectraSP/DevCtrl при подключённых приборах — съёмка остановлена, "
                      "управление монохроматором заблокировано")
            self.stop()
            self._update_actions()
            QtWidgets.QMessageBox.warning(
                self, "SpectraSP запущена",
                "Запущена оригинальная SpectraSP (или DevCtrl), а приборы подключены к этой программе. "
                "Две программы не должны управлять приборами одновременно.\n\n"
                "Съёмка остановлена, команды монохроматору заблокированы. Закройте SpectraSP/DevCtrl — "
                "управление вернётся само. Если нужна именно SpectraSP — сначала нажмите «Отключить» здесь.")
        elif not running and self._foreign:
            self._foreign = False
            log.info("SpectraSP/DevCtrl закрыта — управление приборами снова доступно")
            self.statusBar().showMessage("SpectraSP закрыта — управление приборами снова доступно", 8000)
            self._update_actions()

    def _poll(self) -> None:
        self._poll_ticks += 1
        if not self.simulate and self._poll_ticks % 4 == 0 and \
                (self.inst.camera_connected or self.inst.mono_connected):
            self._check_foreign()
        if self.inst.camera_connected and not self._poll_cam:
            self._poll_cam = True

            def done(st):
                self._poll_cam = False
                block = self.inst.temperature_changing()
                if block != self._temp_block:
                    if not block and self._temp_block:
                        log.info("Температура ПЗС установилась — съёмка снова доступна")
                    self._update_actions()
                if block:
                    self.cam_panel.set_state(st, f"выход на {self.inst.cooling_target:+d} °C — съёмка "
                                                 "недоступна", warn=True)
                elif self.inst.temperature_stalled():
                    self.cam_panel.set_state(st, f"уставка {self.inst.cooling_target:+d} °C недостижима — "
                                                 "съёмка разрешена", warn=True)
                else:
                    self.cam_panel.set_state(st)
                if st is not None and st.temperature is not None:
                    self.lbl_cam.setText(f"Камера: {st.temperature:+.0f} °C, {st.cooling_text}"
                                         + (" — отогрев перед выключением" if self._warm_then_off else ""))
                self._camera_safety(st)
                self._update_bg_status()           # фон мог «устареть» из-за смены температуры
            self.tasks.run(self.inst.camera_state, done, lambda e: setattr(self, "_poll_cam", False), "poll-cam")
        if self.inst.mono_connected and not self._poll_mono and not self._mono_moving:
            self._poll_mono = True

            def done_m(busy):
                self._poll_mono = False
                self.mono_panel.set_busy(bool(busy))
                if self._mono_was_busy and not busy:
                    self.tasks.run(self.inst.refresh_mono, lambda _: self._update_mono_view(), lambda e: None)
                self._mono_was_busy = bool(busy)
            self.tasks.run(self.inst.mono_busy, done_m, lambda e: setattr(self, "_poll_mono", False), "poll-mono")
        if self._mono_moving:
            t = self.inst.mono_thread.busy_for
            self.mono_panel.set_busy(True, f"движение… {t:.0f} с" if t else "движение…")
        elif self.inst.mono_connected and self._poll_ticks % 2 == 0 and not self._service_cmd \
                and not self.inst.shutter_ttl \
                and not self._poll_shutter and not self._acquiring():
            # шторку могут переключить и в штатном окне DevCtrl — кнопка должна показывать факт
            self._poll_shutter = True

            def done_s(state):
                self._poll_shutter = False
                if not self._mono_moving:          # иначе это состояние до команды, которая ещё идёт
                    self.mono_panel.set_shutter_value(state)
            self.tasks.run(self.inst.get_shutter, done_s, lambda e: setattr(self, "_poll_shutter", False),
                           "poll-shutter")

    # ================================================================ камера
    def cooling_on(self, target: int) -> None:
        st = self.inst.last_state
        if target < 0:
            now = f"Сейчас ПЗС: {st.temperature:+.0f} °C.\n" if st and st.temperature is not None else ""
            r = QtWidgets.QMessageBox.question(
                self, "Охлаждение ПЗС",
                f"Охладить ПЗС до {target:+d} °C?\n{now}\n"
                "Охлаждённую ниже 0 °C матрицу нельзя резко отогревать: не выключайте охлаждение кнопкой "
                "в обход программы и не отключайте питание камеры, пока она холодная. При выключении "
                "охлаждения и при выходе программа сама плавно отогреет ПЗС до +2 °C.")
            if r != QtWidgets.QMessageBox.Yes:
                return
        if self._warm_then_off:
            log.info("Отогрев перед выключением отменён: задана новая уставка")
        self._stop_warm_up()
        if self._acquiring():
            # пока ПЗС выходит на новую уставку, снимать нельзя (как в SpectraSP)
            self.stop()
            self.statusBar().showMessage("Съёмка остановлена: меняется температура ПЗС", 8000)
        log.info("Охлаждение: уставка %+d °C", target)
        self.tasks.run(lambda: self.inst.set_cooling(True, target), lambda _: self._update_actions(),
                       lambda e: self._error("Охлаждение", e))

    # ---------------------------------------------------------------- лазер и выходы синхронизации
    def _show_sync(self) -> None:
        inst = self.inst
        self.cam_panel.set_sync((inst.sync_out_setting(1), inst.sync_out_setting(2)),
                                self.s.laser_output, inst.laser_on and inst.camera_connected)
        self.lbl_laser.setVisible(inst.laser_on and inst.camera_connected)

    def _confirm_laser_on(self) -> bool:
        n = self.s.laser_output
        r = QtWidgets.QMessageBox.warning(
            self, "Включение лазера",
            f"Включить лазер (синхровыход {n} камеры)?\n\n"
            "Перед включением убедитесь, что луч направлен в прибор, посторонние предметы убраны "
            "с его пути, а все в помещении в защитных очках.\n\n"
            "Лазер выключается этой же кнопкой, а также сам — при отключении приборов и выходе из программы.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
        return r == QtWidgets.QMessageBox.Yes

    def set_laser(self, on: bool) -> None:
        if not self.inst.camera_connected:
            self._show_sync()
            return
        if on and not self._confirm_laser_on():
            self._show_sync()
            return
        if not on and self._acquiring():
            self.stop()               # выключить сразу, не дожидаясь конца кадра
        self.tasks.run(lambda: self.inst.set_laser(on), lambda _: self._show_sync(),
                       lambda e: (self._error("Лазер", e), self._show_sync()), "laser")

    def set_sync_out(self, n: int, mode: int) -> None:
        if not self.inst.camera_connected or mode == self.inst.sync_out_setting(n):
            self._show_sync()
            return
        on_mode = self.inst.laser_modes()[0]
        if n == self.s.laser_output and mode == on_mode:
            if not self._confirm_laser_on():
                self._show_sync()
                return
        elif mode == SYNC_LOGICAL_1:
            r = QtWidgets.QMessageBox.question(
                self, "Выход синхронизации",
                f"На синхровыходе {n} камеры будет постоянно высокий уровень (логическая 1). Если к этому "
                "выходу что-то подключено (лазер, затвор), оно сработает.\n\nПродолжить?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
            if r != QtWidgets.QMessageBox.Yes:
                self._show_sync()
                return
        self.tasks.run(lambda: self.inst.set_sync_out(n, mode), lambda _: self._show_sync(),
                       lambda e: (self._error("Выход синхронизации", e), self._show_sync()), "sync-out")

    def _ask_cooling_off(self, temperature: float) -> str:
        """Как выключать охлаждение холодной ПЗС: "warm" (отогреть и выключить), "now" или ""."""
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Warning, "Выключение охлаждения",
                                    f"ПЗС охлаждена до {temperature:+.0f} °C. Резкое выключение "
                                    "охлаждения грозит конденсацией влаги на холодной матрице.\n\n"
                                    f"Рекомендуется: плавно отогреть до {WARM_UP_TARGET_C:+d} °C и "
                                    "выключить охлаждение после этого (программа сделает это сама).",
                                    parent=self)
        b_warm = box.addButton("Отогреть и выключить", QtWidgets.QMessageBox.AcceptRole)
        b_now = box.addButton("Выключить сразу", QtWidgets.QMessageBox.DestructiveRole)
        box.addButton("Отмена", QtWidgets.QMessageBox.RejectRole)
        box.setDefaultButton(b_warm)
        box.exec_()
        return {b_warm: "warm", b_now: "now"}.get(box.clickedButton(), "")

    def cooling_off(self) -> None:
        st = self.inst.last_state
        if st and st.temperature is not None and st.temperature < 0 and not self._warm_then_off:
            answer = self._ask_cooling_off(st.temperature)
            if answer == "warm":
                self._start_warm_up()
                return
            if answer != "now":
                return
        self._stop_warm_up()
        log.info("Охлаждение: выключение")
        self.tasks.run(lambda: self.inst.set_cooling(False, 0), lambda _: self._update_actions(),
                       lambda e: self._error("Охлаждение", e))

    def _start_warm_up(self) -> None:
        """Плавный отогрев: уставка +2 °C, охлаждение выключится, когда ПЗС отогреется (см. _camera_safety)."""
        log.info("Охлаждение: отогрев до %+d °C, затем выключение", WARM_UP_TARGET_C)
        self._warm_then_off = True
        if self._acquiring():
            self.stop()
        self.statusBar().showMessage(f"Отогрев ПЗС до {WARM_UP_TARGET_C:+d} °C, затем охлаждение выключится", 8000)
        self.tasks.run(lambda: self.inst.set_cooling(True, WARM_UP_TARGET_C), lambda _: self._update_actions(),
                       lambda e: (self._stop_warm_up(), self._error("Охлаждение", e)))

    def _stop_warm_up(self) -> None:
        self._warm_then_off = False

    def _camera_safety(self, st) -> None:
        """Защита камеры по данным опроса: завершение отогрева, перегрев, остановка вентилятора."""
        if st is None:
            return
        cooling = st.cooling_state != 0
        if self._warm_then_off and st.temperature is not None and st.temperature >= WARM_UP_TARGET_C - 1:
            self._stop_warm_up()
            log.info("ПЗС отогрета до %+.0f °C — охлаждение выключается", st.temperature)
            self.tasks.run(lambda: self.inst.set_cooling(False, 0), lambda _: self._update_actions(),
                           lambda e: self._error("Охлаждение", e))
        # перегрев: два опроса подряд, чтобы не сработать от единичного сбоя
        self._overheat_count = self._overheat_count + 1 if st.overheat else 0
        if self._overheat_count >= 2 and not self._overheat_handled:
            self._overheat_handled = True
            self._stop_warm_up()
            log.error("Камера сообщает о ПЕРЕГРЕВЕ — охлаждение выключается, съёмка останавливается")
            self.stop()
            self.tasks.run(lambda: self.inst.set_cooling(False, 0), None, lambda e: self._error("Охлаждение", e))
            if not self._closing:
                QtWidgets.QMessageBox.critical(
                    self, "Перегрев камеры",
                    "Камера сообщает о перегреве! Охлаждение ПЗС выключено, съёмка остановлена.\n\n"
                    "Проверьте, что вентилятор камеры работает, вентиляционные отверстия не закрыты "
                    "(и, если есть, подключено водяное охлаждение). Включайте охлаждение снова только "
                    "после устранения причины.")
        elif not st.overheat and self._overheat_handled:
            self._overheat_handled = False
            log.info("Камера больше не сообщает о перегреве")
        # вентилятор остановился при включённом охлаждении
        if cooling and not st.fan_on and not self._fan_warned:
            self._fan_warned = True
            log.warning("Вентилятор камеры не работает при включённом охлаждении")
            if not self._closing:
                QtWidgets.QMessageBox.warning(
                    self, "Вентилятор камеры",
                    "Камера сообщает, что вентилятор не работает, а охлаждение включено. Это может привести "
                    "к перегреву. Проверьте вентилятор; при сомнениях выключите охлаждение (с отогревом).")
        elif st.fan_on:
            self._fan_warned = False

    def _acq_settings_changed(self) -> None:
        stripes = self.inst.stripes()
        self.image.set_stripes(stripes)
        self.acq_panel.set_stripes_count(len(stripes))
        if self.inst.camera_connected:
            try:
                acq = self.inst.acq_settings()
                t = self.inst.expected_frame_time(acq) * max(1, self.s.accumulations)
                want_us = self.s.exposure_ms * 1000
                note = ""
                if abs(acq.exposure_us - want_us) > max(0.5, 0.002 * want_us):
                    real = acq.exposure_us
                    shown = f"{real / 1e6:g} с" if real >= 1e6 else (f"{real / 1e3:g} мс" if real >= 1e3
                                                                       else f"{real:g} мкс")
                    note = f"камера снимет {shown} (её точность/минимум)"
                ttl = self.inst.ttl_exposure_problem(acq.exposure_us)
                if ttl:
                    note = (note + "<br>" if note else "") + "Снимать нельзя: " + ttl
                self.acq_panel.set_estimate(t, note)
            except (DeviceError, ValueError) as e:
                self.acq_panel.set_estimate(None)
                log.debug("estimate: %s", e)
        self._update_bg_status()

    # ================================================================ съёмка
    def _start(self, kind: str, fn: Callable[[], None]) -> None:
        if self._acquiring() or self._foreign or self._dev_task:
            return
        block = self.inst.temperature_changing()
        if block:
            self.statusBar().showMessage(f"Съёмка недоступна: {block}. Дождитесь, пока температура установится.",
                                         8000)
            return
        if kind != "background":
            why = self.inst.ttl_exposure_problem()
            if why:
                self._error("Съёмка", DeviceError(f"Съёмка не начата: {why}."))
                return
        self._acq_stop.clear()
        self._acq_kind = kind
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)

        def wrapper():
            try:
                fn()
            except DeviceError as e:
                if not self._acq_stop.is_set():
                    self.emitter.error.emit(e)
            except Exception as e:  # noqa: BLE001
                log.exception("Ошибка съёмки")
                self.emitter.error.emit(e)
            finally:
                self.emitter.finished.emit(kind, threading.current_thread())

        self._acq_thread = threading.Thread(target=wrapper, name=f"acq-{kind}", daemon=True)
        self._acq_thread.start()
        self._update_actions()

    def _progress_cb(self, i: int, n: int, text: str) -> None:
        self.emitter.progress.emit(i, n, text)

    def snap(self) -> None:
        self._start("snap", lambda: self.emitter.measurement.emit(self.inst.acquire(progress=self._progress_cb)))

    def live(self) -> None:
        def loop():
            while not self._acq_stop.is_set():
                self.emitter.measurement.emit(self.inst.acquire(progress=self._progress_cb))
        self._start("live", loop)

    def take_background(self) -> None:
        if self._acquiring():
            return
        auto_close = self.inst.mono_connected and (self.s.close_shutter_for_background or self.inst.shutter_ttl)
        if not auto_close:
            r = QtWidgets.QMessageBox.question(
                self, "Темновой фон",
                "Шторка монохроматора не будет закрыта автоматически.\n"
                "Перекройте свет на входе прибора и нажмите «Да».")
            if r != QtWidgets.QMessageBox.Yes:
                return
        self._start("background", lambda: self.emitter.measurement.emit(
            self.inst.acquire(progress=self._progress_cb, background=True)))

    def reset_background(self) -> None:
        self.inst.clear_background()
        self.background = None
        self.plot.set_background(None, None, False)
        log.info("Фон сброшен")
        self._update_bg_status()

    def stop(self) -> None:
        if not self._acquiring():
            return
        self._acq_stop.set()
        self.inst.abort()
        self.statusBar().showMessage("Остановка…", 3000)
        log.info("Съёмка остановлена пользователем")

    def series(self) -> None:
        dlg = SeriesDialog(self.s, self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        p = dlg.params()
        if p.wavelengths and not self.inst.mono_connected:
            self._error("Серия", DeviceError("Для списка длин волн нужен подключённый монохроматор"))
            return
        if p.wavelengths and (self._mono_moving or self._service_cmd):
            self._error("Серия", DeviceError("Монохроматор ещё выполняет команду — запустите серию после неё"))
            return
        if self._acquiring():
            return
        self.s.autosave_series = p.save
        if p.save:
            self.s.save_dir = p.directory
        log.info("Серия: %d спектров, интервал %.1f с%s%s%s", p.count, p.interval_s,
                 f", длины волн {', '.join(f'{w:g}' for w in p.wavelengths)} нм" if p.wavelengths else "",
                 f", старт в {p.start_at:%H:%M}" if p.start_at else "",
                 f", сохранение в {p.directory}" if p.save else "")
        self._series_beep = p.beep
        total = p.count * max(1, len(p.wavelengths))

        def wait_start():
            while p.start_at and not self._acq_stop.is_set():
                left = (p.start_at - _dt.datetime.now()).total_seconds()
                if left <= 0:
                    break
                self.emitter.progress.emit(0, 0, f"ожидание старта в {p.start_at:%H:%M} ({left / 60:.0f} мин)")
                time.sleep(min(1.0, left))

        def run_at(wl, done):
            for i in range(p.count):
                if self._acq_stop.is_set():
                    return done
                t0 = time.monotonic()
                tag = f" {wl:g} нм" if wl is not None else ""
                m = self.inst.acquire(progress=lambda a, b, t: None, label=f"серия{tag} {i + 1}/{p.count}")
                done += 1
                self.emitter.progress.emit(done, total, "серия")
                if p.save:
                    prefix = p.prefix + (f"_{wl:g}nm" if wl is not None else "")
                    path = unique_path(p.directory, prefix, p.ext)
                    self._save_measurement(path, m)
                    self.emitter.message.emit(f"Сохранено: {path}")
                self.emitter.measurement.emit(m)
                while not self._acq_stop.is_set() and time.monotonic() - t0 < p.interval_s:
                    time.sleep(0.05)
            return done

        def loop():
            wait_start()
            if not p.wavelengths:
                run_at(None, 0)
                return
            done = 0
            with self.inst.mono_exclusive("серия"):
                for wl in p.wavelengths:
                    if self._acq_stop.is_set():
                        break
                    self.emitter.progress.emit(done, total, f"серия: переход на {wl:g} нм")
                    self.inst.goto_wavelength(wl)
                    done = run_at(wl, done)
        self._series_moves_mono = bool(p.wavelengths)
        self._start("series", loop)

    def panorama(self) -> None:
        def plan_fn(a, b, ov, trim=0):
            return self.inst.panorama_plan(a, b, ov, trim)
        start, end = self.inst.setup.main.panorama if self.inst.setup and self.inst.setup.main.panorama[1] > 0 \
            else (400.0, 700.0)
        dlg = PanoramaDialog(start, end, plan_fn, self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        p = dlg.params()
        if self._mono_moving or self._service_cmd or self._acquiring():
            self._error("Панорама", DeviceError("Монохроматор или камера заняты — запустите панораму, когда "
                                                "закончится текущая команда"))
            return
        try:
            plan = self.inst.panorama_plan(p.start, p.end, p.overlap, p.trim)
        except DeviceError as e:
            self._error("Панорама", e)
            return
        log.info("Панорама %.1f–%.1f нм: %d положений", p.start, p.end, len(plan))
        wl0 = self.inst.mono_wl

        def run():
            with self.inst.mono_exclusive("панорама"):
                run_locked()

        def run_locked():
            segments = []
            try:
                for i, c in enumerate(plan):
                    if self._acq_stop.is_set():
                        break
                    self.emitter.progress.emit(i, len(plan), f"панорама: {c:.1f} нм")
                    self.inst.goto_wavelength(c)
                    m = self.inst.acquire(progress=lambda a, b, t: None, label=f"панорама {c:.1f} нм")
                    if m.x_unit != "nm":
                        raise DeviceError("Нет калибровки шкалы длин волн")
                    segments.append((m.x, m.y))
                    self.emitter.measurement.emit(m)
                if segments and not self._acq_stop.is_set():
                    x, y = P.stitch(segments, trim=p.trim, equalize=p.equalize)
                    mm = Measurement(x=x, x_unit="nm", y=y, raw=y, exposure_ms=m.exposure_ms,
                                     accumulations=m.accumulations, center_wl=None, grating=m.grating,
                                     grating_title=m.grating_title, label=f"Панорама {p.start:g}–{p.end:g} нм",
                                     calibration_source=m.calibration_source, background_used=m.background_used,
                                     stitched=True)
                    self.emitter.measurement.emit(mm)
            finally:
                if p.return_back and wl0 is not None:
                    try:
                        self.inst.goto_wavelength(wl0)
                    except DeviceError as e:
                        log.warning("Не удалось вернуть длину волны: %s", e)
        self._start("panorama", run)

    def _on_progress(self, i: int, n: int, text: str) -> None:
        if n > 1:
            self.progress.setRange(0, n)
            self.progress.setValue(i)
        else:
            self.progress.setRange(0, 0)
        self.progress.setFormat(f"{text} %v/%m")

    def _on_acq_error(self, exc: BaseException) -> None:
        self._error("Съёмка", exc)

    def _on_acq_finished(self, kind: str, thread) -> None:
        # сигнал приходит с задержкой: к этому моменту могла начаться новая съёмка
        if thread is self._acq_thread:
            self._acq_thread = None
            self._series_moves_mono = False
        self.progress.setVisible(False)
        self._update_actions()
        if kind in ("panorama", "series"):
            self._update_mono_view()
        if kind == "series" and self._series_beep:
            QtWidgets.QApplication.beep()
            self.statusBar().showMessage("Серия завершена", 15000)

    # ================================================================ отображение
    @staticmethod
    def _x_in(m: Measurement, unit: str, laser_nm: float) -> Optional[np.ndarray]:
        """Ось X измерения в заданных единицах (None — без шкалы длин волн нельзя)."""
        if unit == X_PX:
            if m.x_unit == X_PX and m.x.size == m.y.size:
                return np.asarray(m.x, dtype=float)
            return np.arange(m.y.size, dtype=float)
        if m.x_unit != "nm":
            return None
        return from_nm(m.x, unit, laser_nm)

    def _scale_for(self, m: Optional[Measurement]):
        """(пересчёт единиц, единицы, в которых реально можно показать m)."""
        amap = AxisMap(m.x if m is not None and m.x_unit == "nm" else None, self.s.laser_nm)
        unit = self.s.x_unit if self.s.x_unit in X_UNITS else X_NM
        if m is not None and not amap.available(unit):
            unit = X_PX
        return amap, unit

    def _coverage_text(self, m: Measurement, amap: AxisMap, unit: str):
        """Строка над графиком: что охватывает кадр (или почему шкала в пикселях)."""
        n = m.y.size
        if not amap.calibrated:
            if m.center_wl is None and m.x_unit != "nm":
                why = "длина волны неизвестна (монохроматор не подключён)"
            else:
                why = "в «Калибровке шкалы» выбран режим «пиксели»"
            warn = self.s.x_unit != X_PX
            return (f"Шкала только в пикселях: {why}. Кадр: {n} пикс.", warn)
        wl = amap.wl
        lo, hi = float(min(wl[0], wl[-1])), float(max(wl[0], wl[-1]))
        single = m.center_wl is not None          # один кадр, а не сшитая панорама
        text = f"{'Кадр ПЗС' if single else 'Спектр'} охватывает {lo:.3f} … {hi:.3f} нм"
        if is_spectral(unit) and unit != X_NM:
            a, b = sorted(from_nm([lo, hi], unit, self.s.laser_nm))
            d = 4 if unit == X_EV else 1
            text += f" = {a:.{d}f} … {b:.{d}f} {X_SHORT[unit]}"
        parts = [text, f"{n} пикс., ≈{(hi - lo) / max(1, n - 1):.4f} нм/пикс." if single
                 else f"{n} точек" + (", сшит из нескольких кадров" if m.stitched else "")]
        if m.center_wl is not None:
            parts.append(f"центр {m.center_wl:.3f} нм")
        if m.grating_title:
            parts.append(f"решётка {m.grating_title}")
        if m.calibration_source:
            parts.append(f"шкала: {m.calibration_source}")
        return " · ".join(parts), False

    def _update_image_scale(self) -> None:
        im = self._image_meas
        if im is None or im.image is None:
            return
        amap, unit = self._scale_for(im)
        self.image.set_x_scale(amap, unit, im.image.shape[1])

    def _on_measurement(self, m: Measurement) -> None:
        if self._closing:
            return                     # кадр, дошедший во время выхода: графики уже закрыты
        if self._acquiring() and self._acq_kind in ("series", "panorama"):
            self._update_mono_view()   # серия и панорама сами переставляют монохроматор
        if m.is_background:
            self.background = m
            log.info("Фон снят: %d кадр(ов) по %g мс, среднее %.1f отсч.", m.accumulations, m.exposure_ms,
                     float(np.mean(m.raw)))
            self._update_bg_status()
            self._redraw()
            return
        self.current = m
        if self.s.bands:
            self.band_history.add(m.timestamp, self._band_results(m))
            if self._hist_win is not None and self._hist_win.isVisible():
                self._hist_win.refresh()
        kind = self._acq_kind
        if self.buffer_panel.auto.isChecked() and (kind in ("snap", "series") or (kind == "panorama" and m.stitched)):
            added = self._buffer_add(m)
            self.buffer_panel.refresh()
            self.buffer_panel.select(added[-1].key)
        if m.image is not None:
            self._image_meas = m
            self.image.set_image(m.image, m.image_top)
            self.image.set_note(self._frame_note(m))
        elif self._image_meas is not None:
            self.image.set_note(f"Показан кадр ПЗС от {self._image_meas.timestamp:%H:%M:%S}. "
                                + self.NO_ROWS_HW_BIN)
        self._redraw()
        if m.saturated:
            self.lbl_meas.setText("<b style='color:#b00020'>НАСЫЩЕНИЕ!</b> уменьшите экспозицию")
        else:
            peak = float(np.max(m.raw)) if m.raw.size else 0.0
            self.lbl_meas.setText(f"{m.timestamp:%H:%M:%S}  {m.duration_s:.2f} с,  макс. {peak:.0f} отсч."
                                  + ("  (фон вычтен)" if m.background_used else ""))
        self._update_actions()

    def _redraw(self) -> None:
        m = self.current
        ref = m if m is not None else next((e.m for e in self.buffer.visible()), None)
        amap, unit = self._scale_for(ref)
        laser = self.s.laser_nm
        # у сшитой панорамы «пиксель» — это номер точки, а не пиксель ПЗС
        self.plot.set_scale(amap, unit, pixels_ok=not (ref is not None and ref.stitched))
        if m is not None:
            x = self._x_in(m, unit, laser)
            multi = m.n_tracks > 1
            name = m.label or "Текущий"
            if multi:
                a, b = m.track_rows[0]
                name = f"Полоса 1 (строки {a}–{b})"
            self.plot.set_data(x, m.y, name)
            self.plot.set_tracks([(x, m.track(i), f"Полоса {i + 1} (строки {m.track_rows[i][0]}–{m.track_rows[i][1]})")
                                  for i in range(1, m.n_tracks)] if multi else [])
            self._update_peaks(x, m.y, amap, unit)
        self._scale = (amap, unit)
        self._update_bands(m, amap, unit)
        bg = self.background
        if bg is not None and m is not None and bg.y.size == m.y.size:
            self.plot.set_background(self._x_in(m, unit, laser),
                                     bg.raw * (m.accumulations if m.accumulate_mode == "sum" else 1),
                                     self.proc_panel.show_bg.isChecked())
        else:
            self.plot.set_background(None, None, False)
        self._draw_buffer(unit, laser)
        if ref is not None:
            self.plot.set_info(*self._coverage_text(ref, amap, unit))
        self.plot.refresh_view()
        self._update_image_scale()
        self._update_rows_tab()

    def _update_peaks(self, x: np.ndarray, y: np.ndarray, amap: AxisMap, unit: str) -> None:
        self.peaks_table.setRowCount(0)
        self.peaks_table.setHorizontalHeaderLabels([f"Положение, {X_SHORT.get(unit, unit)}", "Высота",
                                                    "Ширина", "Линия"])
        ok = np.isfinite(y) & np.isfinite(x)
        if not ok.all():                             # результат калькулятора может содержать NaN
            x, y = x[ok], y[ok]
        if not self.proc_panel.peaks.isChecked() or y.size < 5:
            self.plot.show_peaks([], [], [])
            return
        peaks = P.find_peaks(x, y, max_peaks=25)
        xs, ys, labels = [], [], []
        dec = X_DECIMALS.get(unit, 3)
        for pk in peaks:
            r = self.peaks_table.rowCount()
            self.peaks_table.insertRow(r)
            line = ""
            if amap.calibrated:
                # справочную линию ищем по длине волны в любых единицах оси
                c_nm, e_nm = amap.convert([pk.x, pk.x + pk.fwhm / 2], unit, X_NM)
                wl, src = nearest_line(float(c_nm), tolerance=max(0.3, 3 * abs(e_nm - c_nm)))
                if wl == wl:
                    line = f"{wl:.3f} нм {src.split()[0]}"
            for col, val in enumerate((f"{pk.x:.{dec}f}", f"{pk.height:.0f}", f"{pk.fwhm:.{dec}f}", line)):
                self.peaks_table.setItem(r, col, QtWidgets.QTableWidgetItem(val))
            xs.append(pk.x)
            ys.append(pk.height + float(np.median(y)))
            labels.append(f"{pk.x:.{min(dec, 2)}f}")
        self.plot.show_peaks(xs, ys, labels)

    def _display_settings_changed(self) -> None:
        self._redraw()
        self._update_bg_status()

    # ================================================================ интегрирование в участках
    MAX_BANDS = 8

    def _band_results(self, m: Optional[Measurement]) -> list:
        """Результаты всех участков для измерения m (по выбранной полосе строк)."""
        if m is None:
            return []
        y = m.track(min(self.bands_panel.current_track(), m.n_tracks - 1))
        out = []
        for b in self.s.bands:
            if b.get("unit") == "px":
                x = np.asarray(m.x, dtype=float) if m.x_unit == X_PX and m.x.size == y.size \
                    else np.arange(y.size, dtype=float)
            elif m.x_unit == "nm":
                x = m.x
            else:
                out.append(None)
                continue
            out.append(P.band_integral(x, y, b["lo"], b["hi"]))
        return out

    def _update_bands(self, m: Optional[Measurement], amap: AxisMap, unit: str) -> None:
        disp = []
        for b in self.s.bands:
            bu = b.get("unit", "nm")
            if amap.can_convert(bu, unit):
                lo, hi = sorted(float(v) for v in amap.convert([b["lo"], b["hi"]], bu, unit))
                disp.append((lo, hi))
            else:
                disp.append(None)
        self.plot.set_bands(disp)
        self.bands_panel.set_track_count(m.n_tracks if m is not None else 1)
        self.bands_panel.show_results(self._band_results(m))

    def _band_moved(self, i: int, lo: float, hi: float) -> None:
        if self._scale is None or i >= len(self.s.bands):
            return
        amap, unit = self._scale
        b = self.s.bands[i]
        bu = b.get("unit", "nm")
        if not amap.can_convert(unit, bu):
            return
        b["lo"], b["hi"] = sorted(float(v) for v in amap.convert([lo, hi], unit, bu))
        self._redraw()

    def add_band(self) -> None:
        if len(self.s.bands) >= self.MAX_BANDS:
            self.statusBar().showMessage(f"Не больше {self.MAX_BANDS} участков", 5000)
            return
        if self._scale is None:
            return
        amap, unit = self._scale
        a, b = self.plot.vb.viewRange()[0]
        c, w = (a + b) / 2, abs(b - a) * 0.025
        store = "nm" if amap.calibrated else "px"
        if not amap.can_convert(unit, store):
            store = unit if unit in ("nm", "px") else "px"
        lo, hi = sorted(float(v) for v in amap.convert([c - w, c + w], unit, store))
        self.s.bands.append({"lo": lo, "hi": hi, "unit": store})
        self.analysis_tabs.setCurrentWidget(self.bands_panel)
        self._redraw()

    def bands_from_peaks(self) -> None:
        """Участки вокруг найденных пиков (±1.5 ширины на полувысоте), не больше MAX_BANDS."""
        m = self.current
        if m is None:
            return
        unit = "nm" if m.x_unit == "nm" else "px"
        x = m.x if unit == "nm" else np.arange(m.y.size, dtype=float)
        ok = np.isfinite(m.y)
        peaks = sorted(P.find_peaks(x[ok], m.y[ok], max_peaks=self.MAX_BANDS), key=lambda p: -p.height)
        if not peaks:
            self.statusBar().showMessage("Пиков не найдено", 5000)
            return
        self.s.bands = [{"lo": p.x - 1.5 * max(p.fwhm, 1e-6), "hi": p.x + 1.5 * max(p.fwhm, 1e-6), "unit": unit}
                        for p in sorted(peaks[:self.MAX_BANDS], key=lambda p: p.x)]
        self.analysis_tabs.setCurrentWidget(self.bands_panel)
        self._redraw()
        log.info("Участки интегрирования по пикам: %d", len(self.s.bands))

    def calculator(self) -> None:
        def sources():
            out = [("Текущий спектр", self.current)] if self.current is not None else []
            return out + [(e.name, e.m) for e in self.buffer.entries]

        def result(m, name, props):
            e = self.buffer.add(m, name, props=props)
            self.buffer_panel.refresh()
            self.buffer_panel.select(e.key)
            self._redraw()
            log.info("Калькулятор: %s", props.get("formula", name))
        if self._calc is None:
            self._calc = CalculatorDialog(sources, result, self)
            self.buffer_panel.changed.connect(self._calc.refresh)
        self._calc.refresh()
        self._calc.show()
        self._calc.raise_()

    def show_band_history(self) -> None:
        if self._hist_win is None:
            self._hist_win = BandsHistoryWindow(self.band_history, self.s, self)
        self._hist_win.refresh()
        self._hist_win.show()
        self._hist_win.raise_()

    # ================================================================ полосы, дефекты, коррекция
    def _stripes_dragged(self, stripes: list) -> None:
        """Полосу перетащили на изображении ПЗС."""
        if self._refuse_during_run("Полосы строк"):
            self.image.set_stripes(self.inst.stripes())
            return
        try:
            self.inst.set_stripes(stripes)
        except DeviceError as e:
            self._error("Полосы строк", e)
        self.acq_panel.set_rows(*self.inst.stripes()[0])          # → _acq_settings_changed
        self._refresh_file_frame()

    def edit_stripes(self) -> None:
        if self._acquiring():
            self.statusBar().showMessage("Полосы меняются, когда съёмка остановлена", 5000)
            return
        cam = self.inst.camera
        height = cam.info.height if cam is not None and cam.info.height else 506
        img = None
        im = self._image_meas
        if im is not None and im.image is not None:
            img = np.asarray(im.image, dtype=float)
            top = int(im.image_top)
            if top or img.shape[0] != height:              # изображение полосы — на своих строках ПЗС
                full = np.full((height, img.shape[1]), float(np.median(img)))
                part = img[:max(0, height - top)]
                full[top:top + part.shape[0]] = part
                img = full

        def estimate(stripes):
            if not self.inst.camera_connected:
                return None
            old = self.inst.stripes()
            try:
                self.inst.set_stripes(stripes)
                return self.inst.expected_frame_time(self.inst.acq_settings())
            except DeviceError:
                return None
            finally:
                self.inst.set_stripes(old)
        dlg = StripesDialog(self.inst.stripes(), height, img, estimate, self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        try:
            self.inst.set_stripes(dlg.stripes())
        except DeviceError as e:
            self._error("Полосы строк", e)
            return
        self.acq_panel.load_from_settings()
        self._acq_settings_changed()
        self._refresh_file_frame()
        n = len(self.inst.stripes())
        log.info("Полосы строк: %s", ", ".join(f"{a}–{b}" for a, b in self.inst.stripes()))
        if n > 1:
            log.info("Несколько полос: камера читает строки построчно; фон нужно снять заново")

    def edit_defects(self) -> None:
        if self._refuse_during_run("Дефектные пиксели"):
            return
        cam = self.inst.camera
        width = cam.info.width if cam is not None and cam.info.width else 2048
        dark = self.background.raw if self.background is not None else None
        dlg = DefectPixelsDialog(self.s, width, dark, self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self.proc_panel.load_corrections()
            log.info("Дефектные пиксели: %d, исправление %s", len(self.s.defect_pixels),
                     "включено" if self.s.defect_correction else "выключено")

    def edit_intensity(self) -> None:
        if self._refuse_during_run("Коррекция интенсивности"):
            return
        m = self.current
        x_nm = measured = None
        if m is not None and m.x_unit == "nm" and not m.intensity_corrected and not m.stitched:
            x_nm, measured = m.x, m.y
        dlg = IntensityCorrectionDialog(self.s, x_nm, measured, self)
        if m is not None and m.intensity_corrected:
            dlg.btn_planck.setToolTip("Текущий спектр уже скорректирован — снимите эталон с выключенной коррекцией")
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self.proc_panel.load_corrections()
            log.info("Коррекция интенсивности: %s, %d точек", "включена" if self.s.intensity_corr else "выключена",
                     len(self.s.intensity_corr_points))

    # ================================================================ буфер спектров
    def _buffer_path(self) -> str:
        base = getattr(self.s, "_path", None) or AppSettings.default_path()
        return os.path.join(os.path.dirname(os.path.abspath(base)), "buffer.npz")

    def _draw_buffer(self, unit: str, laser: float) -> None:
        """Отмеченные спектры буфера — поверх текущего, каждый своим постоянным цветом."""
        shown = {e.key: e for e in self.buffer.visible()}
        for key in list(self.plot.overlays):
            if key not in shown:
                self.plot.remove_overlay(key)
        hidden = set()
        step = float(self.buffer_panel.offset.value())
        for n, (key, e) in enumerate(shown.items()):
            x = self._x_in(e.m, unit, laser)
            y = e.m.y + step * (n + 1) if step else e.m.y      # «лесенка»: текущий спектр — внизу
            item = self.plot.overlays.get(key)
            if item is None or item.name() != e.name:
                self.plot.add_overlay(key, x, y, e.name, entry_color(e))
            else:
                self.plot.update_overlay(key, x, y)
            if x is None:
                hidden.add(key)
        self.buffer_panel.mark_unavailable(
            hidden, f"Спектр только в пикселях — в единицах «{X_UNITS.get(unit, unit)}» не показывается")

    def pin_current(self) -> None:
        """Положить текущий спектр в буфер."""
        if self.current is None:
            return
        e = None
        for e in self._buffer_add(self.current):
            pass
        self.buffer_panel.refresh()
        self.buffer_panel.select(e.key)
        self._redraw()
        log.info("В буфер: %s", e.name)

    def _buffer_add(self, m: Measurement):
        """В буфер — каждую полосу отдельным спектром (у одной полосы — как есть)."""
        if m.n_tracks <= 1:
            return [self.buffer.add(m)]
        base = SpectrumBuffer.default_name(m)
        out = []
        for i in range(m.n_tracks):
            a, b = m.track_rows[i]
            part = dataclasses.replace(m, y=m.track(i), raw=m.track(i) if i else m.raw, tracks=None,
                                       track_rows=(m.track_rows[i],))
            out.append(self.buffer.add(part, f"{base} · полоса {i + 1} ({a}–{b})"))
        return out

    def buffer_to_current(self, key: str) -> None:
        """Сделать спектр из буфера текущим (пики, сохранение, «В буфер» под другим именем)."""
        e = self.buffer.get(key)
        if e is None:
            return
        if self._acquiring():
            self.statusBar().showMessage("Во время съёмки текущим остаётся снятый спектр — остановите съёмку", 5000)
            return
        self.current = e.m
        if e.m.image is not None:
            self._image_meas = e.m
            self.image._first = True
            self.image.set_image(e.m.image, e.m.image_top)
            self.image.set_note(f"Кадр из буфера: {e.name}. {self._frame_note(e.m)}".strip())
        elif self._image_meas is not None:
            self.image.set_note(f"У спектра «{e.name}» нет кадра ПЗС (снят в режиме «Спектр» или кадр уже "
                                f"вытеснен из буфера) — показан кадр от {self._image_meas.timestamp:%H:%M:%S}")
        self._redraw()
        self.lbl_meas.setText(f"Из буфера: {e.name}")
        self._update_actions()

    def save_buffer_entries(self, keys: list) -> None:
        entries = [e for e in (self.buffer.get(k) for k in keys) if e is not None]
        if not entries:
            return
        d = QtWidgets.QFileDialog.getExistingDirectory(self, f"Папка для {len(entries)} спектр(ов) из буфера",
                                                       self.s.save_dir or os.path.expanduser("~"))
        if not d:
            return
        bad = '<>:"/\\|?*'
        for e in entries:
            stem = "".join("_" if c in bad else c for c in e.name).strip() or e.key
            path = unique_path(d, stem, self.s.save_format)
            try:
                self._save_measurement(path, e.m)
            except OSError as err:
                self._error("Сохранение буфера", err)
                return
        log.info("Сохранено спектров из буфера: %d → %s", len(entries), d)
        self.statusBar().showMessage(f"Сохранено {len(entries)} спектр(ов) в {d}", 6000)

    def clear_overlays(self) -> None:
        """Очистить буфер (без вопроса — для программного вызова)."""
        self.buffer_panel.clear()

    def open_spectrum(self) -> None:
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Открыть спектр", self.s.save_dir or "", "Спектры (*.txt *.csv *.dat *.asc);;Все файлы (*)")
        for path in paths:
            try:
                x, y, meta, header = load_spectrum(path)
            except (OSError, ValueError) as e:
                self._error("Открытие файла", e)
                continue
            first = header[0].lower() if header else ""
            is_px = "pixel" in first or "пиксел" in first or not (100 < float(np.median(x)) < 5000)
            m = Measurement(x=x, x_unit=X_PX if is_px else X_NM, y=y, raw=y, label=os.path.basename(path))
            props = {str(k): str(v) for k, v in meta.items()}
            props["source_file"] = path
            self.buffer.add(m, os.path.basename(path), props=props)
            self.buffer_panel.refresh()
            self._redraw()
            log.info("Загружен спектр %s (%d точек)", path, x.size)

    # ================================================================ сохранение
    def _save_measurement(self, path: str, m: Measurement) -> None:
        extra = {}
        meta = m.meta()
        if m.x_unit == "nm":
            x_name = "wavelength_nm"
            extra["pixel"] = np.arange(m.y.size)
            # дополнительно — столбцы в единицах, выбранных для осей графика
            names = {X_WAVENUMBER: "wavenumber_cm-1", X_RAMAN: "raman_shift_cm-1", X_EV: "photon_energy_eV"}
            for u in (self.s.x_unit, self.s.x_top_unit):
                if u in names:
                    extra[names[u]] = from_nm(m.x, u, self.s.laser_nm)
            if "raman_shift_cm-1" in extra:
                meta["laser_nm"] = self.s.laser_nm
        else:
            x_name = "pixel"
        for i in range(1, m.n_tracks):
            a, b = m.track_rows[i]
            extra[f"stripe{i + 1}_rows_{a}-{b}"] = m.track(i)
        if m.background_used or m.intensity_corrected:
            # «сырые» отсчёты: без вычета фона и без коррекции интенсивности
            extra["raw_counts"] = m.raw
        save_spectrum(path, m.x if m.x_unit == "nm" else np.arange(m.y.size), m.y, meta,
                      x_name=x_name, extra=extra)

    def open_frame(self, path: str = "") -> None:
        """Сохранённый кадр ПЗС: показать и построить спектры по текущим полосам строк."""
        if self._acquiring():
            return
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Открыть кадр ПЗС", self.s.save_dir or "",
                "Кадры (*.npy *.tif *.tiff *.png *.txt *.csv);;Все файлы (*)")
            if not path:
                return
        try:
            img, meta = load_image(path)
        except (OSError, ValueError) as e:
            self._error("Открытие кадра", e)
            return
        try:
            first_row = max(0, int(float(meta.get("first_row", 0))))
        except ValueError:
            first_row = 0
        m = Measurement(x=np.arange(img.shape[1], dtype=float), x_unit=X_PX, y=np.zeros(img.shape[1]),
                        raw=np.zeros(img.shape[1]), image=img, acq_mode="image", label=os.path.basename(path),
                        comment=meta.get("comment", ""), source_file=path, image_top=first_row)
        self._frame_tracks(m)
        self.current = m
        self._image_meas = m
        self.image._first = True
        self.image.set_image(img, first_row)
        self.image.set_note(f"Кадр из файла: {os.path.basename(path)}")
        self.tabs.setCurrentIndex(1)
        self._redraw()
        self._update_actions()
        log.info("Открыт кадр %s (%d×%d)", path, img.shape[1], img.shape[0])

    def _frame_tracks(self, m: Measurement) -> None:
        """Спектры полос по кадру из файла (программное суммирование строк, без фона)."""
        top, h = int(m.image_top), m.image.shape[0]
        clip = lambda r: min(max(r, top), top + h - 1)                          # noqa: E731
        stripes = [(clip(t), clip(b)) for t, b in self.inst.stripes()]
        tracks = np.vstack([P.bin_rows(m.image, t - top, b - top) for t, b in stripes])
        m.y, m.raw = tracks[0], tracks[0]
        m.tracks = tracks if tracks.shape[0] > 1 else None
        m.track_rows = tuple(stripes)
        m.rows = stripes[0]

    def _refresh_file_frame(self) -> None:
        m = self.current
        if m is not None and m.source_file and m.image is not None:
            self._frame_tracks(m)
            self._redraw()

    def export_plot(self) -> None:
        """Картинка графика (PNG — растровая, SVG — векторная, для статей и отчётов)."""
        import pyqtgraph.exporters as pex
        tab = self.tabs.currentWidget()
        image_tab = tab is self.image
        item = (self.image.plot_item if image_tab else
                self.rows_panel.plot.getPlotItem() if tab is self.rows_panel else self.plot.plot.getPlotItem())
        m = self.current
        stem = ("ccd_" if image_tab else "spectrum_") + (f"{m.timestamp:%Y%m%d_%H%M%S}" if m is not None else "plot")
        start = os.path.join(self.s.save_dir or os.path.expanduser("~"), stem + ".png")
        path, flt = QtWidgets.QFileDialog.getSaveFileName(self, "Сохранить график", start,
                                                          "PNG (*.png);;SVG — векторный (*.svg)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".svg" if "svg" in flt.lower() else ".png"
        try:
            if path.lower().endswith(".svg"):
                pex.SVGExporter(item).export(path)
            else:
                ex = pex.ImageExporter(item)
                ex.parameters()["width"] = 1600
                ex.export(path)
        except Exception as e:  # noqa: BLE001 — сообщить, а не упасть
            self._error("Сохранение графика", e)
            return
        log.info("График сохранён: %s", path)
        self.statusBar().showMessage(f"График сохранён: {path}", 6000)

    def save_current(self) -> None:
        m = self.current
        if m is None:
            return
        stem = f"{m.timestamp:%Y%m%d_%H%M%S}" + (f"_{m.center_wl:.0f}nm" if m.center_wl else "")
        if m.comment:
            safe = "".join("_" if c in '<>:"/\\|?*' else c for c in m.comment).strip()[:40]
            stem = f"{safe}_{stem}" if safe else stem
        start = os.path.join(self.s.save_dir or os.path.expanduser("~"), stem + self.s.save_format)
        path, flt = QtWidgets.QFileDialog.getSaveFileName(
            self, "Сохранить спектр", start, "Текст, табуляция (*.txt);;CSV для Excel (*.csv)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".csv" if "csv" in flt.lower() else ".txt"
        try:
            self._save_measurement(path, m)
        except OSError as e:
            self._error("Сохранение", e)
            return
        self.s.save_dir = os.path.dirname(path)
        log.info("Спектр сохранён: %s", path)

    # форматы «Сохранить кадр»: (фильтр диалога, расширение, что пишется)
    FRAME_FORMATS = [
        ("TIFF, 16 бит — отсчёты камеры, пиксель в пиксель (*.tif)", ".tif", "data"),
        ("PNG, 16 бит — отсчёты камеры, пиксель в пиксель (*.png)", ".png", "data"),
        ("PNG — как на экране: палитра и уровни, пиксель в пиксель (*.png)", ".png", "screen"),
        ("TIFF — как на экране: палитра и уровни, пиксель в пиксель (*.tif)", ".tif", "screen"),
        ("NumPy (*.npy)", ".npy", "data"),
        ("Текст (*.txt)", ".txt", "data"),
        ("CSV (*.csv)", ".csv", "data"),
    ]
    NO_ROWS_HW_BIN = ("Снимок сделан с аппаратным объединением строк: камера складывает строки полосы ещё до "
                      "оцифровки, поэтому ни изображения полосы, ни спектров отдельных строк нет. Чтобы они были, "
                      "снимите флажок «аппаратное объединение строк» (считывание медленнее) или снимайте в режиме "
                      "«Изображение».")

    @staticmethod
    def _frame_note(m: Measurement) -> str:
        """Пометка над кадром: изображение полосы — не весь кадр."""
        if m.image is None or m.acq_mode == "image":
            return ""
        top = int(m.image_top)
        return (f"Изображение полосы: строки ПЗС {top}–{top + np.asarray(m.image).shape[0] - 1} (в режиме «Спектр» "
                "без аппаратного объединения камера считывает только строки полос)")

    def save_frame(self) -> None:
        m = self.current
        if m is None or m.image is None:
            return
        img = np.asarray(m.image)
        top, h = int(m.image_top), img.shape[0]
        rows = "" if m.acq_mode == "image" and top == 0 else f"_rows{top}-{top + h - 1}"
        start = os.path.join(self.s.save_dir or os.path.expanduser("~"), f"{m.timestamp:%Y%m%d_%H%M%S}_frame{rows}.tif")
        path, flt = QtWidgets.QFileDialog.getSaveFileName(self, "Сохранить кадр", start,
                                                          ";;".join(f for f, _, _ in self.FRAME_FORMATS))
        if not path:
            return
        ext, kind = next(((e, k) for f, e, k in self.FRAME_FORMATS if f == flt), (".tif", "data"))
        if not os.path.splitext(path)[1]:
            path += ext
        meta = m.meta()
        meta["first_row"] = top
        meta["frame_size"] = f"{img.shape[1]}x{h}"
        try:
            if kind == "screen":
                if self._image_meas is not m:              # на экране — кадр этого спектра
                    self._image_meas = m
                    self.image.set_image(m.image, top)
                data = self.image.render_rgb()
                if data is None:
                    raise ValueError("Кадр не показан на экране")
                meta["view"] = "как на экране: палитра и уровни окна программы (не отсчёты камеры)"
            else:
                data = img
            save_image(path, data, meta)
        except (OSError, ValueError) as e:
            self._error("Сохранение кадра", e)
            return
        self.s.save_dir = os.path.dirname(path)
        log.info("Кадр сохранён: %s (%d×%d%s)", path, img.shape[1], h, ", как на экране" if kind == "screen" else "")

    # ================================================================ строки ПЗС
    def _update_rows_tab(self) -> None:
        """Вкладка «Строки ПЗС» — только когда она открыта (в непрерывной съёмке лишнего не считаем)."""
        if self.tabs.currentWidget() is not self.rows_panel:
            return
        m, note = self.current, ""
        if m is None or m.image is None:
            why = (self.NO_ROWS_HW_BIN if m is not None and m.hardware_binning and m.acq_mode != "image"
                   else "У текущего спектра нет кадра с отдельными строками.")
            if self._image_meas is None:
                self.rows_panel.set_frame(None, why=why)
                return
            m = self._image_meas
            note = f"У текущего спектра отдельных строк нет — показаны строки кадра от {m.timestamp:%H:%M:%S}. {why}"
        amap, unit = self._scale_for(m)
        x = self._x_in(m, unit, self.s.laser_nm)
        rows = list(m.track_rows) or self.inst.stripes()
        stripes = (min(a for a, _ in rows), max(b for _, b in rows))
        self.rows_panel.set_frame(m, x, axis_label(unit, self.s.laser_nm) if x is not None else "", stripes,
                                  note=note)
        self._rows_unit = unit if x is not None else X_PX

    def save_rows(self) -> None:
        rp = self.rows_panel
        m = rp._m
        if m is None:
            return
        x, rows, mean, (a, b) = rp.selected()
        start = os.path.join(self.s.save_dir or os.path.expanduser("~"),
                             f"{m.timestamp:%Y%m%d_%H%M%S}_rows{a}-{b}{self.s.save_format}")
        path, flt = QtWidgets.QFileDialog.getSaveFileName(self, "Сохранить спектры строк", start,
                                                          "Текст, табуляция (*.txt);;CSV для Excel (*.csv)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".csv" if "csv" in flt.lower() else ".txt"
        names = {X_NM: "wavelength_nm", X_WAVENUMBER: "wavenumber_cm-1", X_RAMAN: "raman_shift_cm-1",
                 X_EV: "photon_energy_eV", X_PX: "pixel"}
        unit = getattr(self, "_rows_unit", X_PX)
        meta = m.meta()
        meta["ccd_rows"] = f"{a}-{b}"
        dark = rp.dark.isChecked() and rp.dark.isEnabled()
        meta["values"] = "отсчёты одной строки" + (" за вычетом темнового фона" if dark else "")
        if unit == X_RAMAN:
            meta["laser_nm"] = self.s.laser_nm
        try:
            save_spectrum(path, x, mean, meta, x_name=names.get(unit, "x"), y_name=f"mean_rows_{a}-{b}",
                          extra={f"row_{a + i}": rows[i] for i in range(rows.shape[0])})
        except OSError as e:
            self._error("Сохранение строк", e)
            return
        self.s.save_dir = os.path.dirname(path)
        log.info("Спектры строк %d–%d сохранены: %s", a, b, path)

    # ================================================================ монохроматор
    def _mono_task(self, text: str, fn, after=None, on_fail=None) -> None:
        """Команда с панели «Монохроматор»: одна за раз, панель и окно «Сервис» на это время заблокированы."""
        self._mono_moving = True
        self.mono_panel.set_busy(True, text)
        self._update_actions()

        def done(res):
            self._mono_moving = False
            self.mono_panel.set_busy(False)
            self._update_mono_view()
            if after:
                after(res)
            self._update_actions()

        def fail(e):
            self._mono_moving = False
            self.mono_panel.set_busy(False)
            self._update_actions()
            self._update_mono_view()
            self._refresh_mono_misc()                 # вернуть панели фактические щель/зеркало/шторку
            if on_fail:
                on_fail(e)
            else:
                self._error("Монохроматор", e)
        self.tasks.run(fn, done, fail, "mono")

    def goto_wavelength(self, wl: float) -> None:
        if not self._mono_ready():
            return
        log.info("Переход на %.3f нм", wl)
        self._mono_task("движение…", lambda: self.inst.goto_wavelength(wl),
                        lambda res: log.info("Установлено %.3f нм", res))

    def change_grating(self, index) -> None:
        if index is None or not self._mono_ready():
            return
        if index == self.inst.mono_grating:
            return
        r = QtWidgets.QMessageBox.question(self, "Смена решётки",
                                           f"Повернуть турель на решётку {self.inst.grating_title(index)}?\n"
                                           "Это может занять около минуты.")
        if r != QtWidgets.QMessageBox.Yes:
            return
        self._mono_task("смена решётки…", lambda: self.inst.set_grating(index))

    def set_slit(self, index: int, width: float) -> None:
        if not self._mono_ready():
            return
        self._mono_task("щель…", lambda: self.inst.set_slit(index, width),
                        lambda w: (self.mono_panel.set_slit_value(w), log.info("Щель %d: %.1f мкм", index + 1, w)))

    def set_mirror(self, pos) -> None:
        if pos is None:
            return
        if not self._mono_ready():
            self._refresh_mono_misc()          # вернуть списку фактическое положение зеркала
            return
        self._mono_task("зеркало…", lambda: self.inst.set_mirror(pos),
                        lambda _: log.info("Выходное зеркало: положение %d", pos))

    def set_shutter_ttl(self, on: bool) -> None:
        if not self._mono_ready():
            self._show_shutter_mode()
            return

        def done(_):
            self._show_shutter_mode()
            if not on:
                self._refresh_mono_misc()

        def failed(e):
            self._error("Режим шторки", e)
            self._show_shutter_mode()
        self._mono_task("режим шторки…", lambda: self.inst.set_shutter_ttl(on), done, failed)

    def set_shutter(self, is_open: bool) -> None:
        if not self._mono_ready():
            self._refresh_mono_misc()          # вернуть кнопке шторки фактическое состояние
            return

        def work():
            self.inst.set_shutter(is_open)         # ждёт, пока прибор подтвердит новое состояние
            return self.inst.get_shutter()
        self.mono_panel.btn_shutter.setText("шторка: открывается…" if is_open else "шторка: закрывается…")
        self._mono_task("шторка…", work, self.mono_panel.set_shutter_value, lambda e: self._error("Шторка", e))

    # ================================================================ настройки
    def calibration(self) -> None:
        if self._refuse_during_run("Калибровка шкалы"):
            return
        cur = self.current
        spectrum = cur.raw if cur is not None else None
        n = spectrum.size if spectrum is not None else (self.inst.camera.info.width if self.inst.camera_connected else 2048)
        wl, gr = self.inst.mono_wl, self.inst.mono_grating
        if cur is not None and cur.center_wl is not None:
            wl, gr = cur.center_wl, cur.grating

        def model_fn(cal):
            axis = WavelengthAxis(cal, self.inst.axis.geometries)
            x, unit = axis.axis(n, wl, gr, self.inst.device_model if self.inst.mono_connected else None)
            return x if unit == "nm" else None

        dlg = CalibrationDialog(self.s.calibration, gr, self.inst.grating_title(gr), spectrum, model_fn, self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        new = dlg.result_settings()
        self.s.calibration = new
        self.inst.axis.settings = new
        self.inst.axis.invalidate()
        log.info("Калибровка обновлена: %s, центр. пиксель %.2f", new.mode, new.center_pixel)
        if cur is not None:
            def recompute():
                return self.inst.axis.axis(cur.raw.size, cur.center_wl, cur.grating,
                                           self.inst.device_model if self.inst.mono_connected else None)

            def apply(res):
                cur.x, cur.x_unit = res
                cur.calibration_source = self.inst.axis.source
                self._redraw()
            self.tasks.run(recompute, apply, lambda e: self._error("Калибровка", e))
        self.s.save()

    def settings_dialog(self) -> None:
        if self._dev_task:
            return
        old_dir = self.s.spectrasp_dir
        old_laser = (self.s.laser_output, self.s.laser_active_high)
        dlg = SettingsDialog(self.s, self.inst.camera_ip(), self.inst.mono_address(), self,
                             laser_on=self.inst.laser_on and self.inst.camera_connected)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        self._acq_settings_changed()          # мог измениться порог экспозиции для шторки «по экспозиции»
        if (self.s.laser_output, self.s.laser_active_high) != old_laser and self.s.laser_output in (1, 2) \
                and self.inst.camera_connected:
            # лазер перенесён на другой выход или сменилась полярность: прежний уровень на выходе может
            # теперь означать «включён» (а режим «экспозиция» — вспышки на каждом кадре) — сразу выключаем
            log.info("Лазер перенастроен (синхровыход %d) — выход переводится в «выключен»", self.s.laser_output)
            if self._acquiring():
                self.stop()
            self.tasks.run(lambda: self.inst.set_laser(False), lambda _: self._show_sync(),
                           lambda e: (self._error("Лазер", e), self._show_sync()), "laser")
        self._show_sync()
        if self.s.spectrasp_dir != old_dir:
            if self.inst.camera_connected or self.inst.mono_connected:
                QtWidgets.QMessageBox.information(self, "Настройки", "Новый каталог будет использован после "
                                                  "отключения и повторного подключения приборов.")
            else:
                self.inst.camera = None
                self.inst.mono = None
                self.inst.load_installation()
                self.mono_panel._fill_presets(self.s.wl_presets)
                self._refresh_static_info()
        self.s.save()

    def about(self) -> None:
        QtWidgets.QMessageBox.about(
            self, f"О программе {APP_NAME}",
            f"<h3>{APP_NAME} {__version__}</h3>"
            "<p>Открытая замена SpectraSP для ПЗС-камеры PSI (ProScan) и монохроматора-спектрографа "
            "SOL instruments MS7504i. Работает через штатные драйверы SpectraSP и те же адреса устройств.</p>"
            "<ul>"
            "<li>окно не «замирает» во время экспозиции и движения монохроматора;</li>"
            "<li>спектральный режим с аппаратным объединением строк — кадр в разы быстрее;</li>"
            "<li>параметры камеры передаются только при изменении;</li>"
            "<li>прерывание долгой экспозиции, контроль зависаний, понятные сообщения;</li>"
            "<li>фон со шторкой, накопление с отбраковкой космических частиц, пики, панорама, серии;</li>"
            "<li>калибровка шкалы по реперным линиям, файлы TXT/CSV с метаданными.</li>"
            "</ul><p>Журнал работы: папка logs в каталоге настроек.</p>")

    # ================================================================ закрытие
    def release_graphics(self) -> None:
        """Убрать графики окна из общих списков pyqtgraph: он помнит все ViewBox процесса, и окно,
        созданное следом (так бывает в тестах), обращалось бы к меню графиков уже удалённого окна.
        Сами графики не закрываем: ImageView.close() очищает сцену, и pyqtgraph посреди этого
        обращается к уже удалённому изображению (ошибка при выходе, замеченная в лаборатории)."""
        for gv in self.findChildren(pg.GraphicsView):
            scene = gv.scene()
            for item in scene.items() if scene is not None else []:
                if isinstance(item, pg.ViewBox):
                    pg.ViewBox.AllViews.pop(item, None)
                    if item.name is not None and pg.ViewBox.NamedViews.get(item.name) is item:
                        pg.ViewBox.NamedViews.pop(item.name, None)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._closing:
            self.release_graphics()
            event.accept()
            return
        connected = self.inst.camera_connected or self.inst.mono_connected
        if connected:
            r = QtWidgets.QMessageBox.question(self, APP_NAME, "Отключить приборы и выйти?")
            if r != QtWidgets.QMessageBox.Yes:
                event.ignore()                         # съёмку не трогаем — пользователь передумал
                return
        if self._acquiring():
            self.stop()
        self.s.window_state = bytes(self.saveState().toBase64()).decode("ascii")
        try:
            self.s.save()
        except OSError as e:
            log.warning("Настройки не сохранены: %s", e)
        try:
            self.buffer.save_state(self._buffer_path())
        except OSError as e:
            log.warning("Буфер спектров не сохранён: %s", e)
        if connected:
            self._closing = True
            self.poll_timer.stop()
            self._stop_warm_up()
            log.info("Выход: отключение приборов")
            dlg, cancel = self._shutdown_dialog()
            self.tasks.run(lambda: self.inst.shutdown(say=dlg.text_changed.emit, cancel_warm_up=cancel),
                           lambda _: (log.info("Приборы отключены, программа закрывается"), dlg.finish(),
                                      self.close()),
                           lambda e: (dlg.finish(), self.close()), "shutdown")
            event.ignore()
            return
        self._closing = True
        self.poll_timer.stop()
        try:
            self.inst.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.release_graphics()
        event.accept()
