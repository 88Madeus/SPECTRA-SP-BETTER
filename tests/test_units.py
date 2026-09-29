import numpy as np
import pytest

from spectra_better.units import (X_EV, X_NM, X_PX, X_RAMAN, X_WAVENUMBER, AxisMap, convert_range, from_nm,
                                  to_nm)


@pytest.mark.parametrize("unit", [X_NM, X_WAVENUMBER, X_RAMAN, X_EV])
def test_roundtrip(unit):
    nm = np.array([250.0, 532.0, 600.0, 1064.0])
    back = to_nm(from_nm(nm, unit, 532.0), unit, 532.0)
    assert np.allclose(back, nm)


def test_known_values():
    assert from_nm(500.0, X_WAVENUMBER) == pytest.approx(20000.0)
    assert from_nm(1239.841984, X_EV) == pytest.approx(1.0)
    # линия алмаза 1332 см⁻¹ при лазере 532 нм лежит на 572.575 нм
    assert from_nm(572.575, X_RAMAN, 532.0) == pytest.approx(1332.0, abs=0.1)
    assert from_nm(532.0, X_RAMAN, 532.0) == pytest.approx(0.0, abs=1e-9)


def test_axis_map_pixels():
    wl = 500.0 + 0.02 * np.arange(1024)                 # 500 … 520.46 нм
    amap = AxisMap(wl)
    assert amap.calibrated
    assert amap.convert([0, 1023], X_PX, X_NM) == pytest.approx([500.0, wl[-1]])
    assert amap.convert([510.0], X_NM, X_PX)[0] == pytest.approx(500.0)
    # за краями кадра — линейное продолжение, а не «прилипание» к краю
    assert amap.convert([-10.0], X_PX, X_NM)[0] == pytest.approx(499.8)
    cm = amap.convert([100.0], X_PX, X_WAVENUMBER)[0]
    assert cm == pytest.approx(1e7 / 502.0)
    assert amap.convert([cm], X_WAVENUMBER, X_PX)[0] == pytest.approx(100.0)


def test_axis_map_reversed_scale():
    wl = 520.0 - 0.02 * np.arange(1000)                 # шкала с отражением по X
    amap = AxisMap(wl)
    assert amap.convert([510.0], X_NM, X_PX)[0] == pytest.approx(500.0)


def test_axis_map_without_calibration():
    amap = AxisMap(None)
    assert not amap.calibrated
    assert amap.available(X_PX) and not amap.available(X_NM)
    assert amap.can_convert(X_NM, X_WAVENUMBER)          # аналитически — можно
    assert not amap.can_convert(X_PX, X_NM)
    with pytest.raises(ValueError):
        amap.convert([1.0], X_PX, X_NM)


def test_convert_range():
    assert convert_range([400, 500], X_NM, X_WAVENUMBER) == pytest.approx((20000.0, 25000.0))
    assert convert_range([0, 100], X_PX, X_NM) is None
    wl = 500.0 + 0.02 * np.arange(1024)
    assert convert_range([0, 500], X_PX, X_NM, AxisMap(wl)) == pytest.approx((500.0, 510.0))


def test_tick_labels():
    from spectra_better.gui.plots import format_ticks
    assert format_ticks([19500.0, 20000.0], 500.0) == ["19500", "20000"]     # без 1.95e+04
    assert format_ticks([500.0, 500.5], 0.5) == ["500.0", "500.5"]
    assert format_ticks([-1e-12], 0.1) == ["0.0"]
