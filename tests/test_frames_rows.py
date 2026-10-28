"""Кадр ПЗС в TIFF/PNG пиксель в пиксель, изображение полосы и спектры отдельных строк."""
import gc
import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
from PyQt5 import QtCore  # noqa: E402

from spectra_better.config import ACQ_IMAGE, ACQ_SPECTRUM, AppSettings  # noqa: E402
from spectra_better.storage import load_image, save_image  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# ------------------------------------------------------------------ файлы
def test_tiff_png_roundtrip_pixel_exact(app, tmp_path):
    rng = np.random.default_rng(3)
    img = rng.integers(0, 65535, (137, 2048), dtype=np.uint16)
    meta = {"exposure_ms": 500, "first_row": 52, "comment": "Hg, щель 40 мкм"}
    for ext in (".tif", ".png"):
        p = str(tmp_path / f"f{ext}")
        save_image(p, img, meta)
        back, m = load_image(p)
        assert back.shape == img.shape and np.array_equal(back, img), ext
        assert m["first_row"] == "52" and m["comment"] == "Hg, щель 40 мкм", ext


def test_sum_above_16_bit_goes_to_32_bit_tiff_not_png(tmp_path):
    img = np.full((4, 8), 100000.0)                         # сумма накоплений
    p = str(tmp_path / "s.tif")
    save_image(p, img)
    back, _ = load_image(p)
    assert np.array_equal(back, img)
    with pytest.raises(ValueError, match="65535"):
        save_image(str(tmp_path / "s.png"), img)


def test_screen_picture_is_not_loaded_as_data(app, tmp_path):
    rgb = np.zeros((10, 20, 3), dtype=np.uint8)
    for ext in (".png", ".tif"):
        p = str(tmp_path / f"c{ext}")
        save_image(p, rgb)
        with pytest.raises(ValueError, match="цветная"):
            load_image(p)


# ------------------------------------------------------------------ прибор
@pytest.fixture
def inst(spectrasp_dir):
    from spectra_better.instrument import Instrument
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s.exposure_ms = 100
    s.warm_up_on_close = False
    i = Instrument(s, simulate=True)
    i.connect_camera()
    i.connect_mono()
    i.camera.time_scale = 0.01
    i.camera._update_temp = lambda: None
    i.goto_wavelength(546.0)
    yield i
    i.shutdown()


def test_hardware_binning_gives_no_rows(inst):
    inst.settings.hardware_binning = True
    m = inst.acquire()
    assert m.image is None and m.hardware_binning


def test_stripe_without_binning_keeps_its_rows(inst):
    s = inst.settings
    s.hardware_binning, s.rows_top, s.rows_bottom = False, 280, 314
    s.remove_cosmics = False
    m = inst.acquire()
    assert m.image is not None and m.image.shape == (35, 2048) and m.image_top == 280
    assert np.allclose(m.raw, np.asarray(m.image, dtype=float).sum(axis=0))   # спектр — сумма этих строк
    s.accumulations = 3
    m3 = inst.acquire()
    assert m3.image.shape == (35, 2048)
    assert np.allclose(m3.raw, np.asarray(m3.image).sum(axis=0))             # среднее кадров — как у спектра


def test_dark_frame_of_same_rows_is_attached(inst):
    s = inst.settings
    s.hardware_binning, s.rows_top, s.rows_bottom = False, 280, 314
    s.subtract_background = True
    inst.acquire(background=True)
    m = inst.acquire()
    assert m.background_used and m.image_dark is not None and m.image_dark.shape == m.image.shape


# ------------------------------------------------------------------ окно
@pytest.fixture
def win(app, spectrasp_dir, monkeypatch, tmp_path):
    from spectra_better.gui import main_window as mw
    from spectra_better.gui.log_widget import QtLogHandler
    QMB = QtWidgets.QMessageBox
    msgs = []
    monkeypatch.setattr(QMB, "warning", staticmethod(lambda *a, **k: msgs.append(a[2]) or QMB.Ok))
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
    w.msgs = msgs
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


def snap(win, app, mode=ACQ_SPECTRUM, hw_bin=True):
    win.s.acq_mode = mode
    win.s.hardware_binning = hw_bin
    win.acq_panel.load_from_settings()
    before = win.current
    win.snap()
    t0 = time.monotonic()
    while (win._acquiring() or win.current is before) and time.monotonic() - t0 < 20:
        pump(app, 0.02)
    pump(app, 0.1)
    return win.current


def test_stripe_image_shown_on_its_rows(win, app):
    m = snap(win, app, hw_bin=False)
    top, bottom = win.inst.stripes()[0]
    assert m.image is not None and m.image_top == top and m.image.shape[0] == bottom - top + 1
    assert win._image_meas is m and win.image._top == top
    assert f"строки ПЗС {top}–{bottom}" in win.image.note.text()
    m2 = snap(win, app, hw_bin=True)
    assert m2.image is None and "аппаратным объединением" in win.image.note.text()


def test_rows_tab_mean_and_each_row(win, app):
    m = snap(win, app, mode=ACQ_IMAGE)
    win.tabs.setCurrentWidget(win.rows_panel)
    pump(app)
    rp = win.rows_panel
    x, rows, mean, (a, b) = rp.selected()
    assert (a, b) == win.inst.stripes()[0]                  # по умолчанию — строки полосы
    assert np.allclose(mean, np.asarray(m.image, float)[a:b + 1].mean(axis=0))
    assert np.allclose(rows[0], np.asarray(m.image, float)[a])
    rp._all_rows()
    _, rows, _, (a, b) = rp.selected()
    assert (a, b) == (0, m.image.shape[0] - 1)
    rp.mode.setCurrentIndex(rp.mode.findData("all"))
    pump(app)
    from spectra_better.gui.rows_panel import MAX_CURVES
    assert len(rp._curves) <= MAX_CURVES + 2              # 506 строк — каждая 6-я, график читаемый
    win.grab()


def test_rows_tab_explains_hardware_binning(win, app):
    snap(win, app, hw_bin=True)
    win.tabs.setCurrentWidget(win.rows_panel)
    pump(app)
    assert win.rows_panel._m is None and "аппаратным объединением" in win.rows_panel.info.text()


def test_save_rows_file(win, app, tmp_path, monkeypatch):
    snap(win, app, hw_bin=False)
    win.tabs.setCurrentWidget(win.rows_panel)
    pump(app)
    path = str(tmp_path / "rows.txt")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (path, "Текст, табуляция (*.txt)")))
    win.save_rows()
    head = [line for line in open(path, encoding="utf-8") if not line.startswith("#")][0].split("\t")
    a, b = win.inst.stripes()[0]
    assert head[0] == "wavelength_nm" and head[1] == f"mean_rows_{a}-{b}"
    assert len(head) == 2 + (b - a + 1)


def test_save_frame_formats(win, app, tmp_path, monkeypatch):
    m = snap(win, app, mode=ACQ_IMAGE)
    fmts = {k: f for f, _e, k in [(f, e, (e, k)) for f, e, k in win.FRAME_FORMATS]}
    for (ext, kind), flt in fmts.items():
        path = str(tmp_path / f"frame_{kind}{ext}")
        monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(lambda *a, p=path, f=flt, **k: (p, f)))
        win.save_frame()
        assert os.path.isfile(path), (ext, kind)
        if kind == "data":
            back, meta = load_image(path)
            assert back.shape == m.image.shape and np.array_equal(back, np.asarray(m.image, float)), ext
        else:
            from PyQt5 import QtGui
            q = QtGui.QImage(path)
            assert (q.height(), q.width()) == m.image.shape and not q.isGrayscale()   # пиксель в пиксель
    assert not win.msgs


def test_open_saved_stripe_frame_keeps_rows(win, app, tmp_path, monkeypatch):
    m = snap(win, app, hw_bin=False)
    path = str(tmp_path / "stripe.tif")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (path, win.FRAME_FORMATS[0][0])))
    win.save_frame()
    win.open_frame(path)
    assert win.current.image_top == m.image_top
    assert np.allclose(win.current.raw, np.asarray(m.image, float).sum(axis=0))   # полоса — те же строки


def test_buffer_keeps_frame_rows(tmp_path):
    from spectra_better.buffer import SpectrumBuffer
    from spectra_better.instrument import Measurement
    y = np.arange(8, dtype=float)
    m = Measurement(x=y, x_unit="px", y=y, raw=y, image=np.ones((3, 8), np.uint16), image_top=280)
    b = SpectrumBuffer()
    b.add(m)
    p = str(tmp_path / "b.npz")
    b.save_state(p)
    b2 = SpectrumBuffer()
    b2.load_state(p)
    assert b2.entries[0].m.image_top == 280 and b2.entries[0].m.image.shape == (3, 8)


def test_curves_are_thin_for_fast_redraw(win, app):
    """Сглаженную линию толще 1 пикселя Qt рисует в десятки раз медленнее: у шумного спектра график
    еле перетаскивался (лаборатория, 10.2026). Все кривые спектров — толщиной 1 пиксель."""
    snap(win, app, mode=ACQ_IMAGE)
    win.buffer.add(win.current)                      # наложение из буфера
    win._redraw()
    win.tabs.setCurrentWidget(win.rows_panel)
    pump(app)
    for mode in ("one", "all"):
        win.rows_panel.mode.setCurrentIndex(win.rows_panel.mode.findData(mode))
        pump(app, 0.05)
        items = win.plot.plot.getPlotItem().listDataItems() + win.rows_panel.plot.getPlotItem().listDataItems()
        assert len(items) > 3
        assert all(it.opts["pen"].widthF() <= 1 for it in items), mode
