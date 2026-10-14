"""Ошибки, найденные на приборе 14.10.2026 (по журналу из лаборатории)."""
import gc
import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
import pyqtgraph as pg  # noqa: E402
from PyQt5 import QtCore  # noqa: E402

from spectra_better.config import ACQ_IMAGE, AppSettings  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def errors(monkeypatch):
    """Необработанные ошибки (в программе их ловит sys.excepthook — как ошибки при перерисовке)."""
    out = []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: out.append(e))
    return out


@pytest.fixture
def win(app, spectrasp_dir, monkeypatch, tmp_path):
    from spectra_better.gui import main_window as mw
    from spectra_better.gui.log_widget import QtLogHandler
    QMB = QtWidgets.QMessageBox
    monkeypatch.setattr(QMB, "warning", staticmethod(lambda *a, **k: QMB.Ok))
    monkeypatch.setattr(QMB, "question", staticmethod(lambda *a, **k: QMB.Yes))
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s._path = str(tmp_path / "settings.json")
    s.warm_up_on_close = False
    w = mw.MainWindow(s, True, QtLogHandler())
    w.inst.connect_camera()
    w.inst.connect_mono()
    w._camera_connected(w.inst.camera.info)
    w._mono_connected(w.inst.mono.info)
    w.inst.camera.time_scale = 0.01
    w.inst.camera._update_temp = lambda: None
    w.resize(1200, 800)
    w.show()
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


def _image_frame(win, app):
    win.s.acq_mode = ACQ_IMAGE
    win.acq_panel.load_from_settings()
    win.snap()
    t0 = time.monotonic()
    while (win._acquiring() or win._image_meas is None) and time.monotonic() - t0 < 20:
        pump(app, 0.02)
    assert win._image_meas is not None


# ------------------------------------------------------------------ логарифмическая ось
def test_log_x_does_not_break_axis_painting(win, app, errors):
    """В лаборатории: «Plot Options → Transforms → Log X» в меню правой кнопки — и окно ошибки
    «unsupported operand type(s) for *: 'NoneType' and 'float'» на каждой перерисовке."""
    _image_frame(win, app)
    for item in (win.plot.plot.getPlotItem(), win.image.plot_item):
        item.setLogMode(x=True, y=False)
    for tab in (0, 1):
        win.tabs.setCurrentIndex(tab)
        pump(app)
        win.grab()
    assert not errors, errors
    for item in (win.plot.plot.getPlotItem(), win.image.plot_item):
        item.setLogMode(x=False, y=False)


def test_transforms_hidden_from_plot_menus(win):
    for item in (win.plot.plot.getPlotItem(), win.image.plot_item):
        acts = {a.text(): a for a in item.ctrlMenu.actions()}
        assert "Transforms" in acts and not acts["Transforms"].isVisible()


# ------------------------------------------------------------------ окно ошибки
def test_repeating_error_shows_one_window():
    from spectra_better.gui.app import ErrorReporter
    shown = []
    rep = ErrorReporter(show=shown.append)

    def fail(n):
        try:
            raise TypeError(f"unsupported operand {n}")
        except TypeError:
            rep(*sys.exc_info())
    for i in range(40):                          # ошибка при каждой перерисовке
        fail(i)
    assert len(shown) == 1
    try:
        raise ValueError("другая ошибка")
    except ValueError:
        rep(*sys.exc_info())
    assert len(shown) == 2                        # новая ошибка — своё окно


def test_no_new_window_while_one_is_open():
    from spectra_better.gui.app import ErrorReporter
    shown = []

    def show(text):
        shown.append(text)
        try:                                      # пока окно открыто, перерисовка даёт новые ошибки
            raise KeyError("во время показа")
        except KeyError:
            rep(*sys.exc_info())
    rep = ErrorReporter(show=show)
    try:
        raise RuntimeError("первая")
    except RuntimeError:
        rep(*sys.exc_info())
    assert len(shown) == 1


# ------------------------------------------------------------------ выход
def test_release_graphics_after_image_raises_nothing(win, app, errors):
    """В лаборатории: при выходе «wrapped C/C++ object of type ImageItem has been deleted»."""
    _image_frame(win, app)
    win.tabs.setCurrentIndex(1)
    pump(app)
    own = [it for gv in win.findChildren(pg.GraphicsView) for it in gv.scene().items()
           if isinstance(it, pg.ViewBox)]
    assert own
    win.release_graphics()
    pump(app)
    win.grab()                                    # окно ещё живо и рисуется
    assert not errors, errors
    assert not any(vb in pg.ViewBox.AllViews for vb in own)


# ------------------------------------------------------------------ колебания температуры
@pytest.fixture
def inst(spectrasp_dir):
    from spectra_better.instrument import Instrument
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s.warm_up_on_close = False
    i = Instrument(s, simulate=True)
    i.connect_camera()
    i.camera.time_scale = 0.01
    i.camera._update_temp = lambda: None
    yield i
    i.shutdown()


def _temp(inst, t):
    inst.camera._temp = float(t)
    inst.camera_state()


def test_setpoint_oscillation_does_not_toggle_acquisition(inst):
    """В лаборатории: после выхода на уставку «Температура ПЗС установилась» — несколько раз в минуту."""
    _temp(inst, 28)
    inst.set_cooling(True, 20)
    _temp(inst, 28)
    assert inst.temperature_changing()                    # ещё не дошла — съёмка ждёт
    _temp(inst, 20)
    assert inst.temperature_changing() == ""
    for t in (22, 18, 21, 19, 23, 17):                    # регулятор «гуляет» на 2–3 °C
        _temp(inst, t)
        assert inst.temperature_changing() == "", t
        assert not inst.temperature_stalled(), t
    _temp(inst, 25)                                       # ушла на 5 °C — регулирование потеряно
    assert inst.temperature_changing()
    _temp(inst, 22)                                       # пока не вернётся к уставке (±1 °C) — ждём
    assert inst.temperature_changing()
    _temp(inst, 20.5)
    assert inst.temperature_changing() == ""


def test_new_setpoint_waits_again(inst):
    _temp(inst, 20)
    inst.set_cooling(True, 20)
    _temp(inst, 20)
    assert inst.temperature_changing() == ""
    inst.set_cooling(True, 10)                            # новая уставка — снова ждём выхода на неё
    _temp(inst, 18)
    assert inst.temperature_changing()


# ------------------------------------------------------------------ probe --linearity
def test_probe_takes_rows_from_program_settings(monkeypatch):
    """В лаборатории probe снимал строки 238–267 из настроек SpectraSP, а свет был в 52–188."""
    import argparse
    from spectra_better import config
    from spectra_better.tools import probe
    s = AppSettings()
    s.rows_top, s.rows_bottom = 52, 188
    monkeypatch.setattr(config.AppSettings, "load", classmethod(lambda cls, path=None: s))

    class Inst:
        stripe = (238, 267)
    rows, src = probe.default_rows(argparse.Namespace(rows=None), Inst(), 506)
    assert rows == (52, 188) and "Better" in src
    rows, src = probe.default_rows(argparse.Namespace(rows=[300, 280]), Inst(), 506)
    assert rows == (280, 300) and src == "--rows"


def test_probe_linearity_warns_on_weak_signal(inst, tmp_path):
    from spectra_better.hw.base import spectrum_region
    from spectra_better.tools import probe
    inst.connect_mono()
    inst.goto_wavelength(546.0)
    probe.open_shutter(inst.mono)
    rep = probe.Report(str(tmp_path / "probe.txt"))
    info = inst.camera.info
    region = spectrum_region(info.width, info.height, 450, 480, True)   # мимо света, как в лаборатории
    probe.linearity_sweep(rep, inst.camera, info, region, 250, False, str(tmp_path / "lin.csv"))
    text = "\n".join(rep.lines)
    assert "СИГНАЛ СЛАБЫЙ" in text
    assert "свет добавляется" not in text and "съедает" not in text


# ------------------------------------------------------------------ калибровка из SpectraSP
class _Sensor:
    def __init__(self, center, width):
        self.calib_center_pixel, self.calib_pixel_width = center, width


def _cal_inst(spectrasp_dir, center, width, imported=True, record=None):
    from spectra_better.instrument import Instrument
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s.calibration.center_pixel, s.calibration.device_pixel_width = center, width
    s.calibration_imported = imported
    s.calibration_spectrasp = list(record or [])
    return Instrument(s, simulate=True)


def test_calibration_follows_spectrasp_change(spectrasp_dir):
    """На приборе: в программе 469 / 8 мкм (перенесено при первом запуске), в SpectraSP уже 1071 / 12 мкм."""
    inst = _cal_inst(spectrasp_dir, 469, 8)
    inst._follow_spectrasp_calibration(_Sensor(1071, 12))
    c = inst.settings.calibration
    assert (c.center_pixel, c.device_pixel_width) == (1071, 12)
    assert inst.settings.calibration_spectrasp == [1071, 12]


def test_first_start_takes_spectrasp_calibration(spectrasp_dir):
    inst = _cal_inst(spectrasp_dir, 1024, 12, imported=False)
    inst._follow_spectrasp_calibration(_Sensor(469, 8))
    c = inst.settings.calibration
    assert (c.center_pixel, c.device_pixel_width) == (469, 8) and inst.settings.calibration_imported


def test_calibration_set_in_program_is_kept(spectrasp_dir, caplog):
    inst = _cal_inst(spectrasp_dir, 1000, 12, record=[1071, 12])     # пользователь поправил в программе
    inst._follow_spectrasp_calibration(_Sensor(1071, 12))          # SpectraSP не менялась — тихо
    assert inst.settings.calibration.center_pixel == 1000 and "отличается" not in caplog.text
    inst._follow_spectrasp_calibration(_Sensor(1100, 12))          # поменяли в SpectraSP — предупредить
    assert inst.settings.calibration.center_pixel == 1000 and "отличается" in caplog.text


# ------------------------------------------------------------------ кадры ПЗС в буфере
def _meas(img=None, label="m"):
    import numpy as np
    from spectra_better.instrument import Measurement
    y = np.arange(16, dtype=float)
    return Measurement(x=np.arange(16, dtype=float), x_unit="px", y=y, raw=y, image=img, label=label,
                       acq_mode="image" if img is not None else "spectrum")


def test_buffer_keeps_frames_and_saves_them(tmp_path):
    import numpy as np
    from spectra_better import buffer as B
    buf = B.SpectrumBuffer()
    frame = (np.arange(8 * 16).reshape(8, 16) % 4000).astype(np.uint16)[:, ::-1]   # как после разворота
    a = buf.add(_meas(frame, "кадр"))
    b = buf.add(_meas(None, "спектр"))
    assert a.m.image is not None and a.m.image.dtype == np.uint16 and np.array_equal(a.m.image, frame)
    assert b.m.image is None
    frame2 = np.full((8, 16), 7, dtype=np.uint16)
    p1, p2 = _meas(frame2, "полоса 1"), _meas(None, "полоса 2")
    p2.image = p1.image                                        # полосы одного снимка — один кадр
    e1, e2 = buf.add(p1), buf.add(p2)
    assert e1.m.image is e2.m.image
    path = str(tmp_path / "buffer.npz")
    buf.save_state(path)
    with np.load(path) as z:
        assert sorted(k for k in z.files if k.startswith("img")) == ["img0", "img1"]
    again = B.SpectrumBuffer()
    assert again.load_state(path) == 4
    imgs = [e.m.image for e in again.entries]
    assert imgs[1] is None and imgs[2] is imgs[3] and np.array_equal(imgs[0], frame)
    assert ("Кадр ПЗС", "нет") in again.properties(again.entries[1].key)


def test_buffer_keeps_only_last_frames():
    import numpy as np
    from spectra_better import buffer as B
    buf = B.SpectrumBuffer()
    for k in range(B.MAX_FRAMES + 5):
        buf.add(_meas(np.full((4, 16), k, dtype=np.uint16), f"m{k}"))
    with_frames = [e for e in buf.entries if e.m.image is not None]
    assert len(with_frames) == B.MAX_FRAMES and with_frames[0].name == "m5"


def test_double_click_in_buffer_shows_its_frame(win, app):
    import numpy as np
    _image_frame(win, app)
    first = win._image_meas
    win.pin_current()
    key = win.buffer.entries[-1].key
    win.snap()                                                 # новый кадр — другой
    t0 = time.monotonic()
    while (win._acquiring() or win._image_meas is first) and time.monotonic() - t0 < 20:
        pump(app, 0.02)
    win.buffer_to_current(key)
    shown = win.image.view.getImageItem().image
    assert np.array_equal(shown, np.asarray(first.image, dtype=np.float32).T)
    assert "из буфера" in win.image.note.text()
    spec = win.buffer.add(_meas(None, "без кадра"))
    win.buffer_to_current(spec.key)
    assert "нет кадра ПЗС" in win.image.note.text()
