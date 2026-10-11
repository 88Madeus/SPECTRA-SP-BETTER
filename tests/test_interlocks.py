"""Блокировки действий, которые конфликтуют друг с другом или дублируют друг друга (имитатор)."""
import os
import threading
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from spectra_better.calibration import MODE_DEVICE, CalibrationSettings, WavelengthAxis  # noqa: E402
from spectra_better.config import AppSettings  # noqa: E402
from spectra_better.hw.base import SYNC_LOGICAL_0  # noqa: E402
from spectra_better.instrument import Instrument  # noqa: E402


# ------------------------------------------------------------------ прибор
@pytest.fixture
def inst(spectrasp_dir):
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s.exposure_ms = 100
    i = Instrument(s, simulate=True)
    i.connect_camera()
    i.connect_mono()
    i.camera._update_temp = lambda: None
    i.settings.warm_up_on_close = False
    yield i
    i.shutdown()


def _count_frames(cam):
    """Подменить acquire камеры: считать кадры и запоминать время их начала."""
    starts = []
    orig = cam.acquire

    def acquire():
        starts.append(time.monotonic())
        return orig()
    cam.acquire = acquire
    return starts


def test_frame_waits_for_monochromator_motion(inst):
    inst.camera.time_scale = 0.01
    starts = _count_frames(inst.camera)
    done = {}

    def go():
        inst.goto_wavelength(700.0)                 # ≈1 с у имитатора
        done["t"] = time.monotonic()
    th = threading.Thread(target=go)
    th.start()
    time.sleep(0.1)
    assert inst.light_changing()
    m = inst.acquire()
    th.join()
    assert starts[0] >= done["t"]                   # кадр снят после движения, а не во время
    assert m.center_wl == pytest.approx(700.0, abs=0.01)


def test_frame_retaken_when_slit_changes_during_exposure(inst):
    inst.camera.time_scale = 1.0
    inst.settings.exposure_ms = 600
    starts = _count_frames(inst.camera)
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("m", inst.acquire()))
    th.start()
    time.sleep(0.25)
    inst.set_slit(0, 80)                            # посреди экспозиции
    th.join(10)
    m = out["m"]
    assert len(starts) == 2                         # первый кадр отброшен
    assert starts[1] >= starts[0] + 0.5
    assert m.slit_um == pytest.approx(80)


def test_accumulation_restarts_and_laser_mode_is_not_reverted(inst):
    """Выход синхронизации (лазер), переключённый между кадрами накопления, не возвращается следующим
    кадром в прежнее состояние, а уже снятые кадры накопления отбрасываются."""
    inst.camera.time_scale = 1.0
    inst.settings.exposure_ms = 400
    inst.settings.accumulations = 3
    starts = _count_frames(inst.camera)
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("m", inst.acquire()))
    th.start()
    time.sleep(0.6)                                 # идёт второй кадр
    inst.set_sync_out(2, SYNC_LOGICAL_0)
    th.join(15)
    assert out["m"].accumulations == 3
    assert len(starts) == 5                         # 1-й кадр + прерванный 2-й + три новых
    assert inst.camera.sync_out[1] == SYNC_LOGICAL_0


def test_fallback_axis_is_not_cached_instead_of_device_model():
    geo = {}
    ax = WavelengthAxis(CalibrationSettings(mode=MODE_DEVICE, nm_per_pixel=0.1), geo)
    x1, _ = ax.axis(100, 500.0, 0, None)            # прибор занят — запасная (линейная) шкала
    assert ax.source == "линейная шкала"
    calls = []

    def device(c, cpx, w, n):
        calls.append(1)
        return np.linspace(490.0, 510.0, n)
    x2, _ = ax.axis(100, 500.0, 0, device)
    assert calls and ax.source == "модель прибора" and x2[0] == 490.0
    ax.axis(100, 500.0, 0, device)
    assert len(calls) == 1                          # результат модели прибора запомнен


# ------------------------------------------------------------------ окно
@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _make_window(spectrasp_dir, tmp_path, monkeypatch, connect=True):
    from spectra_better.gui import main_window as mw
    from spectra_better.gui.log_widget import QtLogHandler
    msgs = []
    QMB = QtWidgets.QMessageBox
    monkeypatch.setattr(QMB, "warning", staticmethod(lambda *a, **k: msgs.append(("warning", a[2])) or QMB.Cancel))
    monkeypatch.setattr(QMB, "information", staticmethod(lambda *a, **k: msgs.append(("info", a[2])) or QMB.Ok))
    monkeypatch.setattr(QMB, "question", staticmethod(lambda *a, **k: QMB.Yes))
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s._path = str(tmp_path / "settings.json")
    s.warm_up_on_close = False
    w = mw.MainWindow(s, True, QtLogHandler())
    w.msgs = msgs
    if connect:
        w.inst.connect_camera()
        w.inst.connect_mono()
        w._camera_connected(w.inst.camera.info)
        w._mono_connected(w.inst.mono.info)
        w.inst.camera.time_scale = 0.01
        w.inst.camera._update_temp = lambda: None
    return w


def _close(w):
    w._closing = True
    w.poll_timer.stop()
    if w._service_dlg is not None:
        w._service_dlg.close()
    w.inst.shutdown()
    _delete(w)


def _delete(w):
    """Удалить окно сразу (а не «когда-нибудь»): иначе pyqtgraph при создании следующего окна в том же
    процессе обращается к меню графиков уже удалённого."""
    import gc
    from PyQt5 import QtCore
    w.release_graphics()
    w.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    gc.collect()


@pytest.fixture
def win(app, spectrasp_dir, monkeypatch, tmp_path):
    w = _make_window(spectrasp_dir, tmp_path, monkeypatch)
    yield w
    _close(w)


def pump(app, t=0.3):
    t0 = time.monotonic()
    while time.monotonic() - t0 < t:
        app.processEvents()
        time.sleep(0.01)


def wait_for(app, cond, t=15.0):
    t0 = time.monotonic()
    while not cond() and time.monotonic() - t0 < t:
        pump(app, 0.02)
    return cond()


def test_connect_twice_opens_drivers_once(app, spectrasp_dir, monkeypatch, tmp_path):
    w = _make_window(spectrasp_dir, tmp_path, monkeypatch, connect=False)
    try:
        calls = []
        orig = w.inst.connect_camera

        def slow_connect():
            calls.append(1)
            time.sleep(0.5)
            return orig()
        w.inst.connect_camera = slow_connect
        w.connect_devices()
        pump(app, 0.05)
        assert not w.act_connect.isEnabled() and not w.act_disconnect.isEnabled()
        assert not w.act_reconnect_cam.isEnabled() and not w.act_settings.isEnabled()
        w.connect_devices()                          # второе нажатие (например, Ctrl+K ещё раз)
        w.reconnect_camera()
        assert wait_for(app, lambda: w.inst.camera_connected and w.inst.mono_connected and not w._dev_task)
        pump(app, 0.2)
        assert len(calls) == 1
        assert w.act_disconnect.isEnabled() and w.act_snap.isEnabled()
    finally:
        _close(w)


def test_service_window_and_mono_panel_share_one_command_queue(win, app):
    win.mono_service()
    dlg = win._service_dlg
    assert wait_for(app, lambda: dlg.btn_wl.isEnabled())
    dlg._run("тест", lambda: time.sleep(0.5))          # команда из окна «Сервис»
    pump(app, 0.05)
    assert not win.mono_panel.btn_go.isEnabled() and not win._mono_ready()
    assert "Сервис" in win.mono_panel.lock_note.text()
    assert not win.act_pano.isEnabled()                 # панорама планируется по текущей решётке
    assert wait_for(app, lambda: win.mono_panel.btn_go.isEnabled())

    win._mono_task("тест", lambda: time.sleep(0.5))     # команда с панели
    pump(app, 0.05)
    assert not dlg.btn_wl.isEnabled() and "панели" in dlg.status.text()
    dlg._run("не должна выполниться", lambda: pytest.fail("команда из окна «Сервис» во время команды с панели"))
    assert wait_for(app, lambda: not win._mono_moving)
    assert wait_for(app, lambda: dlg.btn_wl.isEnabled())

    win.mono_service()                                  # повторное открытие — то же окно
    assert win._service_dlg is dlg


def test_shutter_button_goes_through_command_queue(win, app):
    win.mono_panel.btn_shutter.click()
    pump(app, 0.02)
    assert win._mono_moving and not win.mono_panel.btn_go.isEnabled()
    assert wait_for(app, lambda: not win._mono_moving)
    assert win.mono_panel.btn_go.isEnabled()


def test_series_freezes_acquisition_parameters(win, app, monkeypatch):
    from spectra_better.gui import main_window as mw
    from spectra_better.gui.dialogs import SeriesParams

    class FakeSeries:
        def __init__(self, *a, **k):
            pass

        def exec_(self):
            return QtWidgets.QDialog.Accepted

        def params(self):
            return SeriesParams(count=10000, interval_s=0.05, save=False, directory="", prefix="s", ext=".txt",
                                beep=False)
    monkeypatch.setattr(mw, "SeriesDialog", FakeSeries)
    win.series()
    pump(app, 0.2)
    try:
        assert win._acquiring()
        for w in (win.acq_panel.exposure, win.acq_panel.accum, win.acq_panel.speed, win.acq_panel.mode,
                  win.acq_panel.btn_stripes, win.proc_panel.subtract, win.proc_panel.btn_bg_reset,
                  win.proc_panel.btn_defects):
            assert not w.isEnabled(), w
        assert win.acq_panel.comment.isEnabled()            # комментарий можно менять
        for a in (win.act_calib, win.act_defects, win.act_intensity, win.act_stripes, win.act_open_frame):
            assert not a.isEnabled(), a.text()
        assert not win.image.region.movable
        old = win.inst.stripes()
        win._stripes_dragged([(10, 20)])
        assert win.inst.stripes() == old
        assert win.mono_panel.btn_go.isEnabled()            # серия без списка длин волн монохроматор не держит
    finally:
        win.stop()
        assert wait_for(app, lambda: not win._acquiring())
    pump(app, 0.2)
    assert win.acq_panel.exposure.isEnabled() and win.proc_panel.subtract.isEnabled()
    assert win.act_calib.isEnabled() and win.image.region.movable


def test_series_with_wavelengths_locks_mono_from_start(win, app, monkeypatch):
    from spectra_better.gui import main_window as mw
    from spectra_better.gui.dialogs import SeriesParams
    import datetime as dt

    class FakeSeries:
        def __init__(self, *a, **k):
            pass

        def exec_(self):
            return QtWidgets.QDialog.Accepted

        def params(self):                           # старт через минуту: монохроматор ещё не занят серией
            return SeriesParams(count=1, interval_s=0, save=False, directory="", prefix="s", ext=".txt",
                                wavelengths=[500.0, 600.0], start_at=dt.datetime.now() + dt.timedelta(minutes=1),
                                beep=False)
    monkeypatch.setattr(mw, "SeriesDialog", FakeSeries)
    win.series()
    pump(app, 0.2)
    try:
        assert not win.mono_panel.btn_go.isEnabled() and "серия" in win._mono_lock_reason()
    finally:
        win.stop()
        assert wait_for(app, lambda: not win._acquiring())
    pump(app, 0.1)
    assert win.mono_panel.btn_go.isEnabled()


def test_live_keeps_parameters_editable_but_not_stripe_dialog(win, app):
    win.live()
    pump(app, 0.2)
    try:
        assert win.acq_panel.exposure.isEnabled() and win.act_calib.isEnabled()
        assert not win.acq_panel.btn_stripes.isEnabled() and not win.act_stripes.isEnabled()
        assert win.image.region.movable
        assert win.mono_panel.btn_go.isEnabled()            # кадры во время движения отбрасываются прибором
    finally:
        win.stop()
        assert wait_for(app, lambda: not win._acquiring())


def test_laser_rewiring_in_settings_switches_new_output_off(win, app, monkeypatch):
    from spectra_better.gui import main_window as mw
    change = {}

    class FakeSettings:
        def __init__(self, settings, *a, **k):
            self.s = settings

        def exec_(self):
            for k, v in change.items():
                setattr(self.s, k, v)
            return QtWidgets.QDialog.Accepted
    monkeypatch.setattr(mw, "SettingsDialog", FakeSettings)

    # 1) лазер «переехал» на выход 2, где сейчас импульс экспозиции (вспыхивал бы на каждом кадре)
    win.s.laser_output = 0
    win.inst.set_sync_out(2, 0)
    change.update(laser_output=2)
    win.settings_dialog()
    off = win.inst.laser_modes()[1]
    assert wait_for(app, lambda: win.inst.camera.sync_out[1] == off)
    assert win.inst.sync_out_setting(2) == off and not win.inst.laser_on

    # 2) сменилась полярность: прежний уровень «выключен» теперь означал бы «включён»
    change.clear()
    change.update(laser_active_high=not win.s.laser_active_high)
    win.settings_dialog()
    off = win.inst.laser_modes()[1]
    assert wait_for(app, lambda: win.inst.camera.sync_out[1] == off)
    assert not win.inst.laser_on


def test_disconnect_closes_idle_service_window(win, app):
    win.mono_service()
    dlg = win._service_dlg
    assert wait_for(app, lambda: dlg.btn_wl.isEnabled())
    win.disconnect_devices()
    assert wait_for(app, lambda: not win._dev_task and not win.inst.mono_connected)
    assert not dlg.isVisible()
    assert win.act_connect.isEnabled()
