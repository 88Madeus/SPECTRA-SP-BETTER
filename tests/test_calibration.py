import numpy as np
import pytest

from spectra_better.calibration import (MODE_DEVICE, MODE_LINEAR, MODE_MODEL, MODE_PIXELS, CalibrationSettings,
                                        GratingGeometry, WavelengthAxis, fit_correction, raman_shift)


def test_geometry_matches_measured_dispersion():
    # В каталоге SpectraSP есть калибровка BaseWLClbr_1.wlc: пиксель 1070 → 1064 нм, 1832 → 1083 нм
    # (решётка 600 штр/мм). Модель по параметрам MS7504_20126.cfg должна давать близкую дисперсию.
    g = GratingGeometry(600, 754.4, 0.0683971, 0.04)
    wl = g.wavelengths(1073.5, 1024, 12.0, 2048)
    disp = (wl[1832] - wl[1070]) / 762
    assert disp == pytest.approx(19.0 / 762, rel=0.05)
    assert np.all(np.diff(wl) > 0)
    assert wl[1024] == pytest.approx(1073.5, abs=1e-9)


def test_unreachable_wavelength():
    g = GratingGeometry(1800, 754.4, 0.0684)
    with pytest.raises(ValueError):
        g.wavelengths(1200, 1024, 12, 10)


def test_fit_correction_recovers_shift_and_scale():
    model = np.linspace(500, 516, 2048)
    true = model + 0.3 + 1e-4 * np.arange(2048)
    px = [100.0, 800.0, 1500.0, 1900.0]
    corr, rms = fit_correction(px, np.interp(px, np.arange(2048), true), model)
    assert rms < 1e-6
    assert np.allclose(corr.apply(np.arange(2048.0), model), true, atol=1e-6)


def test_axis_modes_and_fallbacks():
    geoms = {0: GratingGeometry(1800, 754.4, 0.0684, 0.04)}
    s = CalibrationSettings(mode=MODE_DEVICE, center_pixel=1024, device_pixel_width=12, pixel_um=12)
    ax = WavelengthAxis(s, geoms)
    calls = []

    def device_model(c, cp, pw, n):
        calls.append((c, cp, pw, n))
        return np.linspace(c - 5, c + 5, n)
    x, unit = ax.axis(2048, 600.0, 0, device_model)
    assert unit == "nm" and x[0] == pytest.approx(595) and ax.source == "модель прибора"
    x2, _ = ax.axis(2048, 600.0, 0, device_model)
    assert len(calls) == 1 and x2 is x                       # кэш
    x, unit = ax.axis(2048, 610.0, 0, lambda *a: None)      # модель недоступна → геометрия
    assert unit == "nm" and ax.source == "геометрическая модель"
    s.mode = MODE_LINEAR
    s.nm_per_pixel = 0.01
    x, _ = ax.axis(2048, 600.0, 0, device_model)
    assert x[1] - x[0] == pytest.approx(0.01)
    s.mode = MODE_PIXELS
    x, unit = ax.axis(10, 600.0, 0, device_model)
    assert unit == "px" and x[-1] == 9
    x, unit = WavelengthAxis(CalibrationSettings(mode=MODE_MODEL), geoms).axis(10, None, 0)
    assert unit == "px"


def test_raman():
    assert raman_shift(np.array([532.0]), 532.0)[0] == pytest.approx(0)
    assert raman_shift(np.array([561.0]), 532.0)[0] == pytest.approx(971.7, abs=0.2)
