"""Панель вида над спектром: единицы оси X и диапазон (без оборудования и без экрана)."""
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from spectra_better.config import AppSettings  # noqa: E402
from spectra_better.units import X_NM, X_PX, X_WAVENUMBER, AxisMap, from_nm  # noqa: E402

WL = 491.6 + 0.0081 * np.arange(2048)


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def plot(app):
    from spectra_better.gui.plots import SpectrumPlot
    s = AppSettings()
    p = SpectrumPlot(s)
    p.resize(800, 500)
    yield p
    p.deleteLater()


def show(p, unit, wl=WL):
    amap = AxisMap(wl, p.s.laser_nm)
    if not amap.available(unit):
        unit = X_PX
    x = np.arange(wl.size, dtype=float) if unit == X_PX else from_nm(wl, unit, p.s.laser_nm)
    p.set_scale(amap, unit)
    p.set_data(x, np.random.rand(wl.size) * 100 + 300)
    p.refresh_view()


def xrange_(p):
    return p.vb.viewRange()[0]


def test_default_is_whole_spectrum_in_nm(plot):
    assert plot.s.x_unit == X_NM and plot.s.x_auto
    show(plot, X_NM)
    assert xrange_(plot) == pytest.approx([WL[0], WL[-1]])
    assert plot.x_from.value() == pytest.approx(WL[0], abs=1e-3)


def test_manual_range_kept_and_converted(plot):
    show(plot, X_NM)
    plot.x_from.setValue(495.0)
    plot.x_to.setValue(505.0)
    assert not plot.s.x_auto
    assert xrange_(plot) == pytest.approx([495.0, 505.0])
    show(plot, X_NM)                                   # новый спектр — участок не меняется
    assert xrange_(plot) == pytest.approx([495.0, 505.0])
    plot.s.x_unit = X_WAVENUMBER
    show(plot, X_WAVENUMBER)                           # тот же участок в см⁻¹
    assert xrange_(plot) == pytest.approx([1e7 / 505.0, 1e7 / 495.0])
    plot.s.x_unit = X_PX
    show(plot, X_PX)                                   # и в пикселях — по шкале спектра
    assert xrange_(plot) == pytest.approx([(495.0 - 491.6) / 0.0081, (505.0 - 491.6) / 0.0081], abs=0.01)


def test_without_wavelength_scale_falls_back_to_pixels(plot):
    show(plot, X_NM)
    plot.x_from.setValue(495.123)                      # три знака: при смене точности поля не должны
    plot.x_to.setValue(505.456)                        # «сами» задать новый диапазон
    show(plot, X_NM, wl=np.zeros(2048))                # шкалы нет → пиксели, весь кадр
    assert plot._unit == X_PX
    assert xrange_(plot) == pytest.approx([0, 2047])
    assert plot.s.x_range_unit == X_NM and plot.s.x_range == [495.123, 505.456]   # участок не потерян
    show(plot, X_NM)
    assert xrange_(plot) == pytest.approx([495.123, 505.456])


def test_mouse_zoom_switches_to_manual(plot):
    show(plot, X_NM)
    plot.vb.scaleBy((0.5, 0.5))
    plot.vb.sigRangeChangedManually.emit([True, True])
    assert not plot.s.x_auto and not plot.s.y_auto
    assert plot.s.x_range == pytest.approx(list(xrange_(plot)))
    assert not plot.x_auto.isChecked()
    plot.show_all()
    assert plot.s.x_auto and plot.s.y_auto
    assert xrange_(plot) == pytest.approx([WL[0], WL[-1]])


def test_top_axis_in_pixels(plot):
    plot.top_box.setCurrentIndex(plot.top_box.findData(X_PX))
    show(plot, X_NM)
    assert plot.plot.getPlotItem().getAxis("top").isVisible()
    lo, hi = xrange_(plot)
    levels = plot.top_axis.tickValues(lo, hi, 600)
    labels = plot.top_axis.tickStrings(levels[0][1], 1, levels[0][0])
    assert labels[:3] == ["0", "500", "1000"]
    assert "пиксель" in plot.cursor_text(500.0, 1.0)


def test_stitched_spectrum_has_no_pixel_scale(plot):
    plot.top_box.setCurrentIndex(plot.top_box.findData(X_PX))
    amap = AxisMap(np.linspace(530, 675, 5540), plot.s.laser_nm)
    plot.set_scale(amap, X_NM, pixels_ok=False)        # панорама: номер точки — не пиксель ПЗС
    assert not plot.plot.getPlotItem().getAxis("top").isVisible()
    assert "пиксель" not in plot.cursor_text(600.0, 1.0)
