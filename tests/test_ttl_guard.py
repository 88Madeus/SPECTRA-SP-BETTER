"""Шторка «по экспозиции»: короче порога (на приборе — 30 мс) снимки не делаются.

Измерено на приборе: при экспозиции короче 12 мс шторка оставалась открытой (кадр засвечен
дольше экспозиции), при 12–25 мс раскрывалась не полностью, от 30 мс работала нормально."""
import gc
import os
import time

import pytest

from spectra_better.config import AppSettings
from spectra_better.hw.base import DeviceError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def inst(spectrasp_dir):
    from spectra_better.instrument import Instrument
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s.exposure_ms = 10
    s.warm_up_on_close = False
    i = Instrument(s, simulate=True)
    i.connect_camera()
    i.connect_mono()
    i.camera.time_scale = 0.01
    i.camera._update_temp = lambda: None
    yield i
    i.shutdown()


def test_short_exposure_refused_with_ttl_shutter(inst):
    inst.set_shutter_ttl(True)
    why = inst.ttl_exposure_problem()
    assert "30 мс" in why and "вручную" in why
    with pytest.raises(DeviceError, match="Снимок не сделан.*30 мс"):
        inst.acquire()


def test_threshold_exposure_allowed(inst):
    inst.set_shutter_ttl(True)
    inst.settings.exposure_ms = 30
    assert inst.ttl_exposure_problem() == ""
    assert inst.acquire().y is not None


def test_background_not_affected(inst):
    inst.set_shutter_ttl(True)                       # фон снимается при закрытой шторке в ручном режиме
    m = inst.acquire(background=True)
    assert m.is_background
    assert inst.shutter_ttl                          # режим «по экспозиции» вернулся после фона


def test_manual_shutter_and_zero_threshold_not_limited(inst):
    assert not inst.shutter_ttl and inst.ttl_exposure_problem() == ""
    inst.set_shutter_ttl(True)
    inst.settings.shutter_ttl_min_ms = 0             # 0 — без ограничения
    assert inst.ttl_exposure_problem() == ""
    inst.acquire()


def test_threshold_is_saved(tmp_path):
    p = str(tmp_path / "settings.json")
    s = AppSettings()
    s._path = p
    s.shutter_ttl_min_ms = 45
    s.save()
    assert AppSettings.load(p).shutter_ttl_min_ms == 45
    assert AppSettings().shutter_ttl_min_ms == 30


# ------------------------------------------------------------------ окно
@pytest.fixture
def win(spectrasp_dir, monkeypatch, tmp_path):
    QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5 import QtCore
    from spectra_better.gui import main_window as mw
    from spectra_better.gui.log_widget import QtLogHandler
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    QMB = QtWidgets.QMessageBox
    msgs = []
    monkeypatch.setattr(QMB, "warning", staticmethod(lambda *a, **k: msgs.append(a[2]) or QMB.Ok))
    monkeypatch.setattr(QMB, "question", staticmethod(lambda *a, **k: QMB.Yes))
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s._path = str(tmp_path / "settings.json")
    s.warm_up_on_close = False
    s.exposure_ms = 10
    w = mw.MainWindow(s, True, QtLogHandler())
    w.inst.connect_camera()
    w.inst.connect_mono()
    w._camera_connected(w.inst.camera.info)
    w._mono_connected(w.inst.mono.info)
    w.inst.camera.time_scale = 0.01
    w.inst.camera._update_temp = lambda: None
    w.msgs = msgs
    w.app = app
    yield w
    w._closing = True
    w.poll_timer.stop()
    w.inst.shutdown()
    w.release_graphics()
    w.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    gc.collect()


def pump(app, t=0.2):
    t0 = time.monotonic()
    while time.monotonic() - t0 < t:
        app.processEvents()
        time.sleep(0.01)


def test_gui_explains_and_refuses(win):
    assert "Снимать нельзя" not in win.acq_panel.estimate.text()
    win.set_shutter_ttl(True)
    t0 = time.monotonic()
    while (win._mono_moving or not win.inst.shutter_ttl) and time.monotonic() - t0 < 10:
        pump(win.app, 0.02)
    pump(win.app, 0.1)
    assert win.inst.shutter_ttl
    assert "Снимать нельзя" in win.acq_panel.estimate.text()     # видно заранее, у поля экспозиции
    before = win.current
    win.snap()
    pump(win.app, 0.3)
    assert not win._acquiring() and win.current is before
    assert win.msgs and "Съёмка не начата" in win.msgs[-1] and "30 мс" in win.msgs[-1]
    win.s.exposure_ms = 50
    win.acq_panel.load_from_settings()
    win._acq_settings_changed()
    assert "Снимать нельзя" not in win.acq_panel.estimate.text()
