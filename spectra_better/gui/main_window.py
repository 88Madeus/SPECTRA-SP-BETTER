"""Главное окно программы."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Dict, Optional

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from .. import APP_NAME, __version__
from .. import processing as P
from ..calibration import WavelengthAxis
from ..config import COOLING_TARGET_DEFAULT_C, AppSettings
from ..hw.base import WARM_UP_TARGET_C, DeviceError
from ..hw.winutil import spectrasp_is_running
from ..instrument import Instrument, Measurement
from ..reference_lines import nearest_line
from ..storage import load_spectrum, save_image, save_spectrum, unique_path
from ..units import (X_DECIMALS, X_EV, X_NM, X_PX, X_RAMAN, X_SHORT, X_UNITS, X_WAVENUMBER, AxisMap, from_nm,
                     is_spectral)
from .dialogs import BusyDialog, CalibrationDialog, PanoramaDialog, SeriesDialog, SettingsDialog
from .icons import gear_icon
from .log_widget import LogView, QtLogHandler
from .panels import AcquisitionPanel, CameraPanel, MonoPanel, ProcessingPanel
from .plots import ImagePanel, SpectrumPlot
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
        self.overlays: Dict[str, Measurement] = {}
        self._overlay_seq = 0
        self._acq_thread: Optional[threading.Thread] = None
        self._acq_stop = threading.Event()
        self._acq_kind = ""
        self._poll_cam = False
        self._poll_mono = False
        self._mono_was_busy = False
        self._mono_moving = False
        self._closing = False
        self._service_dlg = None
        # защита ПЗС
        self._warm_then_off = False       # идёт плавный отогрев перед выключением охлаждения
        self._overheat_count = 0          # сколько опросов подряд камера сообщает о перегреве
        self._overheat_handled = False
        self._fan_warned = False
        self._foreign = False             # запущена SpectraSP/DevCtrl (две программы на одних приборах)
        self._poll_ticks = 0
        self._image_meas: Optional[Measurement] = None   # измерение, из которого показан кадр ПЗС

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
        self.act_open = act("Открыть спектр…", SP.SP_DialogOpenButton, "Ctrl+O", "Загрузить спектр для наложения")
        self.act_pin = act("Закрепить", SP.SP_DialogYesButton, "Ctrl+D", "Оставить текущий спектр на графике для сравнения")
        self.act_clear = act("Очистить наложения", SP.SP_TrashIcon, None, "Убрать все наложенные спектры")
        self.act_autorange = act("Весь спектр", SP.SP_TitleBarMaxButton, "Ctrl+A",
                                 "Показать весь спектр по X и подобрать масштаб по Y")
        self.act_calib = act("Калибровка шкалы…", None, None, "Центральный пиксель, модель, поправка по реперным линиям")
        self.act_settings = act("Настройки…", SP.SP_FileDialogDetailedView, None, "Каталог SpectraSP, таймауты")
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
        for a in (self.act_open, self.act_save, self.act_save_img, None, self.act_exit):
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
        m = mb.addMenu("&Настройка")
        for a in (self.act_calib, self.act_settings):
            m.addAction(a)
        m = mb.addMenu("&Справка")
        m.addAction(self.act_about)

        # центр
        self.tabs = QtWidgets.QTabWidget()
        self.plot = SpectrumPlot(self.s)
        self.image = ImagePanel()
        self.tabs.addTab(self.plot, "Спектр")
        self.tabs.addTab(self.image, "Изображение ПЗС")
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
        rv.addWidget(self.peaks_table, 2)
        rv.addWidget(QtWidgets.QLabel("Наложенные спектры:"))
        self.overlay_list = QtWidgets.QListWidget()
        self.overlay_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        rv.addWidget(self.overlay_list, 1)
        self.btn_overlay_del = QtWidgets.QPushButton("Убрать выбранные")
        rv.addWidget(self.btn_overlay_del)
        right.setMinimumWidth(330)
        rdock = QtWidgets.QDockWidget("Анализ", self)
        rdock.setObjectName("dock_analysis")
        rdock.setWidget(right)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, rdock)

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
        self.act_clear.triggered.connect(self.clear_overlays)
        self.act_autorange.triggered.connect(self.plot.show_all)
        self.plot.units_changed.connect(self._display_settings_changed)
        self.act_calib.triggered.connect(self.calibration)
        self.act_settings.triggered.connect(self.settings_dialog)
        self.act_about.triggered.connect(self.about)
        self.act_exit.triggered.connect(self.close)
        self.btn_overlay_del.clicked.connect(self.remove_selected_overlays)

        self.cam_panel.cooling_on.connect(self.cooling_on)
        self.cam_panel.cooling_off.connect(self.cooling_off)
        self.acq_panel.changed.connect(self._acq_settings_changed)
        self.image.rows_changed.connect(self.acq_panel.set_rows)
        self.mono_panel.goto.connect(self.goto_wavelength)
        self.mono_panel.grating.connect(self.change_grating)
        self.mono_panel.slit.connect(self.set_slit)
        self.mono_panel.mirror.connect(self.set_mirror)
        self.mono_panel.shutter.connect(self.set_shutter)
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
        self.image.set_rows(max(0, self.s.rows_top), max(0, self.s.rows_bottom))

    def _acquiring(self) -> bool:
        return self._acq_thread is not None and self._acq_thread.is_alive()

    def _mono_lock_reason(self) -> str:
        """Почему ручное управление монохроматором сейчас недоступно ("" — доступно)."""
        if self._foreign:
            return "запущена SpectraSP или DevCtrl — закройте её"
        if self._acquiring() and self._acq_kind == "panorama":
            return "идёт съёмка панорамы"
        if self._acquiring() and self._acq_kind == "background":
            return "снимается темновой фон (шторка закрыта программой)"
        return ""

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
        self.act_connect.setEnabled(not (cam and mono) and not acq)
        self.act_disconnect.setEnabled((cam or mono) and not acq)
        self.act_reconnect_cam.setEnabled(not acq)
        free = not acq and not self._foreign
        for a in (self.act_snap, self.act_live, self.act_bg, self.act_series):
            a.setEnabled(cam and free)
        self.act_pano.setEnabled(cam and mono and free)
        self.act_stop.setEnabled(acq)
        self.act_save.setEnabled(self.current is not None)
        self.act_save_img.setEnabled(self.current is not None and self.current.image is not None)
        self.act_pin.setEnabled(self.current is not None)
        self.cam_panel.set_connected(cam)
        lock = self._mono_lock_reason()
        self.mono_panel.set_connected(mono and not self._mono_moving and not lock)
        self.act_service.setEnabled(mono)
        self.mono_panel.set_service_enabled(mono)
        if self._service_dlg is not None:
            self._service_dlg.set_locked(lock)
        self.proc_panel.btn_bg.setEnabled(cam and free)
        self.lbl_cam.setText("Камера: подключена" if cam else "Камера: —")
        self._update_bg_status()

    def _update_bg_status(self) -> None:
        has = self.inst.has_background()
        txt = ""
        if not has and self.background is not None:
            txt = "фон снят с другими параметрами"
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
        self._foreign = False
        if not self.inst.camera_connected:
            self.lbl_cam.setText("Камера: подключение…")
            log.info("Подключение камеры (%s)…", self.inst.camera_ip() or "?")
            self.tasks.run(self.inst.connect_camera, self._camera_connected,
                           lambda e: (self._error("Камера", e), self._update_actions()), "connect-camera")
        if not self.inst.mono_connected:
            self.lbl_mono.setText("Монохроматор: подключение…")
            log.info("Подключение монохроматора (%s)…", self.inst.mono_address() or "?")
            self.tasks.run(self.inst.connect_mono, self._mono_connected,
                           lambda e: (self._error("Монохроматор", e), self.lbl_mono.setText("Монохроматор: —")),
                           "connect-mono")

    def _camera_connected(self, info) -> None:
        log.info("Камера подключена: %s № %s, %d×%d, драйвер %s", info.model, info.serial, info.width,
                 info.height, info.dll_name)
        self.cam_panel.set_info(info, self.inst.camera_ip())
        self.acq_panel.set_camera(info)
        self.acq_panel.load_from_settings()
        self.image.set_rows(self.s.rows_top, self.s.rows_bottom)
        self.cam_panel.set_state(self.inst.last_state)
        self._acq_settings_changed()
        self._update_actions()

    def _mono_connected(self, info) -> None:
        log.info("Монохроматор подключён: %s № %s", info.model, info.serial)
        self.mono_panel.set_info(info, self.inst.mono_address())
        self._update_mono_view()
        self._update_actions()
        self._refresh_mono_misc()

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
        if self._service_dlg is not None and self._service_dlg.isVisible():
            self._service_dlg.raise_()
            self._service_dlg.activateWindow()
            return
        from .service_dialog import MonoServiceDialog

        def changed():
            self._update_mono_view()
            self._refresh_mono_misc()
        self._service_dlg = MonoServiceDialog(self.inst, self.tasks, changed, self)
        self._service_dlg.set_locked(self._mono_lock_reason())
        self._service_dlg.show()

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
        if self._acquiring():
            return
        self._stop_warm_up()
        dlg, cancel = self._shutdown_dialog()
        self.tasks.run(lambda: self.inst.disconnect(say=dlg.text_changed.emit, cancel_warm_up=cancel),
                       lambda _: self._disconnected(dlg),
                       lambda e: (self._disconnected(dlg), self._error("Отключение", e)), "disconnect")

    def _disconnected(self, dlg) -> None:
        dlg.finish()
        log.info("Приборы отключены")
        self._refresh_static_info()
        self.cam_panel.set_state(None)
        self._update_mono_view()
        self._update_actions()

    def reconnect_camera(self) -> None:
        if self._acquiring():
            return

        def work():
            if self.inst.camera is not None:
                try:
                    self.inst.cam_thread.call(self.inst.camera.close, False, timeout=30)
                except Exception as e:  # noqa: BLE001
                    log.warning("Закрытие камеры: %s", e)
            return self.inst.connect_camera()
        self.tasks.run(work, self._camera_connected, lambda e: (self._error("Камера", e), self._update_actions()),
                       "reconnect-camera")

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
                self.cam_panel.set_state(st)
                if st is not None and st.temperature is not None:
                    self.lbl_cam.setText(f"Камера: {st.temperature:+.0f} °C, {st.cooling_text}"
                                         + (" — отогрев перед выключением" if self._warm_then_off else ""))
                self._camera_safety(st)
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
        log.info("Охлаждение: уставка %+d °C", target)
        self.tasks.run(lambda: self.inst.set_cooling(True, target), None,
                       lambda e: self._error("Охлаждение", e))

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
        self.tasks.run(lambda: self.inst.set_cooling(False, 0), None, lambda e: self._error("Охлаждение", e))

    def _start_warm_up(self) -> None:
        """Плавный отогрев: уставка +2 °C, охлаждение выключится, когда ПЗС отогреется (см. _camera_safety)."""
        log.info("Охлаждение: отогрев до %+d °C, затем выключение", WARM_UP_TARGET_C)
        self._warm_then_off = True
        self.statusBar().showMessage(f"Отогрев ПЗС до {WARM_UP_TARGET_C:+d} °C, затем охлаждение выключится", 8000)
        self.tasks.run(lambda: self.inst.set_cooling(True, WARM_UP_TARGET_C), None,
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
            self.tasks.run(lambda: self.inst.set_cooling(False, 0), None, lambda e: self._error("Охлаждение", e))
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
        self.image.set_rows(self.s.rows_top, self.s.rows_bottom)
        if self.inst.camera_connected:
            try:
                acq = self.inst.acq_settings()
                t = self.inst.expected_frame_time(acq) * max(1, self.s.accumulations)
                self.acq_panel.set_estimate(t)
            except (DeviceError, ValueError) as e:
                self.acq_panel.set_estimate(None)
                log.debug("estimate: %s", e)
        self._update_bg_status()

    # ================================================================ съёмка
    def _start(self, kind: str, fn: Callable[[], None]) -> None:
        if self._acquiring() or self._foreign:
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
        if not self.inst.mono_connected or not self.s.close_shutter_for_background:
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
        self.s.autosave_series = p.save
        if p.save:
            self.s.save_dir = p.directory
        log.info("Серия: %d спектров, интервал %.1f с%s", p.count, p.interval_s,
                 f", сохранение в {p.directory}" if p.save else "")

        def loop():
            for i in range(p.count):
                if self._acq_stop.is_set():
                    break
                t0 = time.monotonic()
                m = self.inst.acquire(progress=lambda a, b, t: None, label=f"серия {i + 1}/{p.count}")
                self.emitter.progress.emit(i + 1, p.count, "серия")
                if p.save:
                    path = unique_path(p.directory, p.prefix, p.ext)
                    self._save_measurement(path, m)
                    self.emitter.message.emit(f"Сохранено: {path}")
                self.emitter.measurement.emit(m)
                while not self._acq_stop.is_set() and time.monotonic() - t0 < p.interval_s:
                    time.sleep(0.05)
        self._start("series", loop)

    def panorama(self) -> None:
        def plan_fn(a, b, ov):
            return self.inst.panorama_plan(a, b, ov)
        start, end = self.inst.setup.main.panorama if self.inst.setup and self.inst.setup.main.panorama[1] > 0 \
            else (400.0, 700.0)
        dlg = PanoramaDialog(start, end, plan_fn, self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        p = dlg.params()
        try:
            plan = self.inst.panorama_plan(p.start, p.end, p.overlap)
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
                    x, y = P.stitch(segments)
                    mm = Measurement(x=x, x_unit="nm", y=y, raw=y, exposure_ms=m.exposure_ms,
                                     accumulations=m.accumulations, center_wl=None, grating=m.grating,
                                     grating_title=m.grating_title, label=f"Панорама {p.start:g}–{p.end:g} нм",
                                     calibration_source=m.calibration_source, background_used=m.background_used)
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
        self.progress.setVisible(False)
        self._update_actions()
        if kind == "panorama":
            self._update_mono_view()

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
        text = f"Кадр ПЗС охватывает {lo:.3f} … {hi:.3f} нм"
        if is_spectral(unit) and unit != X_NM:
            a, b = sorted(from_nm([lo, hi], unit, self.s.laser_nm))
            d = 4 if unit == X_EV else 1
            text += f" = {a:.{d}f} … {b:.{d}f} {X_SHORT[unit]}"
        parts = [text, f"{n} пикс., ≈{(hi - lo) / max(1, n - 1):.4f} нм/пикс."]
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
        if m.is_background:
            self.background = m
            log.info("Фон снят: %d кадр(ов) по %.0f мс, среднее %.1f отсч.", m.accumulations, m.exposure_ms,
                     float(np.mean(m.raw)))
            self._update_bg_status()
            self._redraw()
            return
        self.current = m
        if m.image is not None:
            self._image_meas = m
            self.image.set_image(m.image)
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
        ref = m if m is not None else next(iter(self.overlays.values()), None)
        amap, unit = self._scale_for(ref)
        laser = self.s.laser_nm
        self.plot.set_scale(amap, unit)
        if m is not None:
            x = self._x_in(m, unit, laser)
            self.plot.set_data(x, m.y, m.label or "Текущий")
            self._update_peaks(x, m.y, amap, unit)
        bg = self.background
        if bg is not None and m is not None and bg.y.size == m.y.size:
            self.plot.set_background(self._x_in(m, unit, laser),
                                     bg.raw * (m.accumulations if m.accumulate_mode == "sum" else 1),
                                     self.proc_panel.show_bg.isChecked())
        else:
            self.plot.set_background(None, None, False)
        hidden = set()
        for key, om in self.overlays.items():
            ox = self._x_in(om, unit, laser)
            self.plot.update_overlay(key, ox, om.y)
            if ox is None:
                hidden.add(key)
        for i in range(self.overlay_list.count()):
            item = self.overlay_list.item(i)
            off = item.data(QtCore.Qt.UserRole) in hidden
            item.setForeground(QtGui.QColor("#999" if off else "#000"))
            item.setToolTip(f"Спектр только в пикселях — в единицах «{X_UNITS.get(unit, unit)}» не показывается"
                            if off else "")
        if ref is not None:
            self.plot.set_info(*self._coverage_text(ref, amap, unit))
        self.plot.refresh_view()
        self._update_image_scale()

    def _update_peaks(self, x: np.ndarray, y: np.ndarray, amap: AxisMap, unit: str) -> None:
        self.peaks_table.setRowCount(0)
        self.peaks_table.setHorizontalHeaderLabels([f"Положение, {X_SHORT.get(unit, unit)}", "Высота",
                                                    "Ширина", "Линия"])
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

    # ================================================================ наложения
    def pin_current(self) -> None:
        if self.current is None:
            return
        self._add_overlay(self.current, self.current.label or f"{self.current.timestamp:%H:%M:%S}")

    def _add_overlay(self, m: Measurement, name: str) -> None:
        self._overlay_seq += 1
        key = f"ov{self._overlay_seq}"
        self.overlays[key] = m
        self.plot.add_overlay(key, None, m.y, name)
        item = QtWidgets.QListWidgetItem(name)
        item.setData(QtCore.Qt.UserRole, key)
        self.overlay_list.addItem(item)
        self._redraw()

    def remove_selected_overlays(self) -> None:
        for item in self.overlay_list.selectedItems():
            key = item.data(QtCore.Qt.UserRole)
            self.plot.remove_overlay(key)
            self.overlays.pop(key, None)
            self.overlay_list.takeItem(self.overlay_list.row(item))
        self._redraw()

    def clear_overlays(self) -> None:
        self.plot.clear_overlays()
        self.overlays.clear()
        self.overlay_list.clear()
        self._redraw()

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
            self._add_overlay(m, os.path.basename(path))
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
        if m.background_used:
            extra["raw_without_background_subtraction"] = m.raw
        save_spectrum(path, m.x if m.x_unit == "nm" else np.arange(m.y.size), m.y, meta,
                      x_name=x_name, extra=extra)

    def save_current(self) -> None:
        m = self.current
        if m is None:
            return
        stem = f"{m.timestamp:%Y%m%d_%H%M%S}" + (f"_{m.center_wl:.0f}nm" if m.center_wl else "")
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

    def save_frame(self) -> None:
        m = self.current
        if m is None or m.image is None:
            return
        start = os.path.join(self.s.save_dir or os.path.expanduser("~"), f"{m.timestamp:%Y%m%d_%H%M%S}_frame.npy")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Сохранить кадр", start, "NumPy (*.npy);;Текст (*.txt);;CSV (*.csv)")
        if not path:
            return
        try:
            save_image(path, m.image, m.meta())
        except OSError as e:
            self._error("Сохранение", e)
            return
        log.info("Кадр сохранён: %s", path)

    # ================================================================ монохроматор
    def _mono_task(self, text: str, fn, after=None) -> None:
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

    def set_shutter(self, is_open: bool) -> None:
        if not self._mono_ready():
            self._refresh_mono_misc()          # вернуть кнопке шторки фактическое состояние
            return

        def work():
            self.inst.set_shutter(is_open)
            return self.inst.get_shutter()
        self.tasks.run(work, self.mono_panel.set_shutter_value,
                       lambda e: (self._error("Шторка", e), self.mono_panel.set_shutter_value(None)))

    # ================================================================ настройки
    def calibration(self) -> None:
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
        old_dir = self.s.spectrasp_dir
        dlg = SettingsDialog(self.s, self.inst.camera_ip(), self.inst.mono_address(), self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
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
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._closing:
            event.accept()
            return
        if self._acquiring():
            self.stop()
        self.s.window_state = bytes(self.saveState().toBase64()).decode("ascii")
        try:
            self.s.save()
        except OSError as e:
            log.warning("Настройки не сохранены: %s", e)
        if self.inst.camera_connected or self.inst.mono_connected:
            r = QtWidgets.QMessageBox.question(self, APP_NAME, "Отключить приборы и выйти?")
            if r != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
            self._closing = True
            self.poll_timer.stop()
            self._stop_warm_up()
            dlg, cancel = self._shutdown_dialog()
            self.tasks.run(lambda: self.inst.shutdown(say=dlg.text_changed.emit, cancel_warm_up=cancel),
                           lambda _: (dlg.finish(), self.close()),
                           lambda e: (dlg.finish(), self.close()), "shutdown")
            event.ignore()
            return
        self._closing = True
        self.poll_timer.stop()
        try:
            self.inst.shutdown()
        except Exception:  # noqa: BLE001
            pass
        event.accept()
