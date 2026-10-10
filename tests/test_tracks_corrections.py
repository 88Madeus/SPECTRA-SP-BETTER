"""Несколько полос строк, дефектные пиксели, коррекция интенсивности, интегрирование в полосе."""
import numpy as np
import pytest

from spectra_better import processing as P
from spectra_better.config import ACQ_IMAGE, AppSettings
from spectra_better.hw.base import DeviceError
from spectra_better.instrument import Instrument


@pytest.fixture
def inst(spectrasp_dir):
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s.exposure_ms = 100
    i = Instrument(s, simulate=True)
    i.connect_camera()
    i.connect_mono()
    i.camera.time_scale = 0.01
    i.camera._update_temp = lambda: None          # температура ПЗС не меняется — съёмка не блокируется
    i.settings.warm_up_on_close = False
    i.goto_wavelength(546.0)
    yield i
    i.shutdown()


def _sig(y):
    return float(np.max(y) - np.median(y))


# ------------------------------------------------------------------ несколько полос
def test_two_stripes_two_spectra(inst):
    inst.camera.fibers = [(150, 3, 1.0), (350, 3, 0.3)]         # два волокна, второе слабее
    inst.set_stripes([(140, 160), (340, 360)])
    acq = inst.acq_settings()
    r = acq.regions[0]
    assert (r.top, r.bottom, r.bin_y) == (140, 360, 1)          # построчно от верхней до нижней полосы
    m = inst.acquire()
    assert m.n_tracks == 2 and m.track_rows == ((140, 160), (340, 360))
    ratio = _sig(m.track(1)) / _sig(m.track(0))
    assert 0.2 < ratio < 0.4
    np.testing.assert_array_equal(m.track(0), m.y)
    assert m.meta()["stripes"] == "140-160, 340-360"


def test_single_stripe_keeps_hardware_binning(inst):
    inst.set_stripes([(280, 310)])
    assert inst.acq_settings().regions[0].bin_y == 31
    m = inst.acquire()
    assert m.tracks is None and m.n_tracks == 1


def test_stripes_in_image_mode(inst):
    inst.camera.fibers = [(150, 3, 1.0), (350, 3, 0.5)]
    inst.settings.acq_mode = ACQ_IMAGE
    inst.set_stripes([(140, 160), (340, 360), (480, 500)])
    m = inst.acquire()
    assert m.n_tracks == 3 and m.image is not None
    assert _sig(m.track(2)) < 0.05 * _sig(m.track(0))          # третья полоса — без света


def test_background_per_stripe(inst):
    inst.camera.fibers = [(150, 3, 1.0)]
    inst.set_stripes([(140, 160), (430, 490)])                 # вторая полоса — тёмная и втрое выше
    inst.set_shutter(False)
    inst.acquire(background=True)
    inst.set_shutter(True)
    m = inst.acquire()
    assert m.background_used
    assert np.median(m.raw) > 21 * 250                         # смещение 300 отсч. × 21 строку
    assert abs(float(np.median(m.track(1)))) < 20              # в тёмной полосе (61 строка) — около нуля
    assert _sig(m.track(0)) > 1000                             # а линии в светлой — на месте
    inst.set_stripes([(140, 160)])                             # другая раскладка — фон не подходит
    assert not inst.has_background()


def test_stripe_limits(inst):
    with pytest.raises(DeviceError):
        inst.set_stripes([])
    with pytest.raises(DeviceError):
        inst.set_stripes([(i * 10, i * 10 + 5) for i in range(9)])
    inst.set_stripes([(5000, 6000), (30, 10)])                 # за пределами матрицы и наоборот
    assert inst.stripes() == [(505, 505), (10, 30)]


# ------------------------------------------------------------------ дефектные пиксели
def test_defect_pixels_found_and_fixed(inst):
    inst.camera.hot_columns = {1000: 3000.0, 1500: 2000.0}
    inst.set_shutter(False)
    bg = inst.acquire(background=True)
    found = P.find_defects(bg.raw)
    assert 1000 in found and 1500 in found and len(found) <= 6
    inst.settings.defect_pixels = [1000, 1500]
    inst.settings.subtract_background = False
    m = inst.acquire()
    assert m.defects_fixed == 2
    assert abs(m.raw[1000] - np.median(m.raw[990:1010])) < 30


def test_defects_respect_flip():
    y = np.zeros(10)
    y[2] = 100.0                                               # столбец 2 камеры после разворота — точка 7
    flipped = y[::-1]
    out = P.fix_defects(flipped, [2], flip=True)
    assert out[7] == 0 and out.sum() == 0
    two = P.fix_defects(np.vstack([y, y * 2]), [2])
    assert two.shape == (2, 10) and two[:, 2].tolist() == [0, 0]


# ------------------------------------------------------------------ коррекция интенсивности
def test_intensity_correction_by_wavelength_and_pixel(inst):
    inst.settings.subtract_background = False
    base = inst.acquire()
    inst.settings.intensity_corr = True
    inst.settings.intensity_corr_by = "wl"
    inst.settings.intensity_corr_points = [[400, 2.0], [800, 2.0]]
    m = inst.acquire()
    assert "длинам волн" in m.intensity_corrected
    assert 1.8 < m.y.max() / base.y.max() < 2.2
    np.testing.assert_allclose(m.raw.max(), base.raw.max(), rtol=0.1)     # «сырые» не трогаются
    inst.settings.intensity_corr_by = "px"
    inst.settings.intensity_corr_points = [[0, 0.5], [2047, 0.5]]
    m = inst.acquire()
    assert "пикселям" in m.intensity_corrected and 0.4 < m.y.max() / base.y.max() < 0.6


def test_correction_curve_and_build():
    x = np.linspace(400, 800, 401)
    k = P.correction_curve(x, [[500, 1.0], [700, 3.0]])
    assert k[0] == 1.0 and k[-1] == 3.0 and abs(k[200] - 2.0) < 1e-9     # 600 нм — посередине
    truth = P.planck(x, 3000.0)
    response = 1.0 + 0.5 * np.sin((x - 400) / 60)                         # «чувствительность прибора»
    measured = truth * response
    pts = P.build_correction(x, measured, truth, n_points=30, smooth=1)
    corrected = measured * P.correction_curve(x, pts)
    shape = corrected / truth
    assert np.std(shape / shape.mean()) < 0.03                            # форма восстановлена
    with pytest.raises(ValueError):
        P.build_correction(x, np.zeros_like(x), truth)


def test_planck_peak_wien():
    x = np.linspace(300, 3000, 2701)
    assert abs(x[np.argmax(P.planck(x, 3000.0))] - 2897.77 / 3.0) < 2     # закон Вина


# ------------------------------------------------------------------ интегрирование
def test_band_integral():
    x = np.arange(100, dtype=float)
    y = 10.0 + np.where((x >= 40) & (x <= 60), 5.0, 0.0)
    r = P.band_integral(x, y, 30, 70)
    assert r.points == 41 and r.total == pytest.approx(41 * 10 + 21 * 5)
    assert r.net == pytest.approx(21 * 5)                                  # над базовой линией
    assert r.peak == 15 and 40 <= r.peak_x <= 60
    assert P.band_integral(x, y, 200, 300) is None
    assert P.band_integral(x[::-1], y[::-1], 70, 30).net == pytest.approx(21 * 5)


# ------------------------------------------------------------------ калькулятор
def test_spectrum_math():
    xa = np.linspace(500, 600, 101)
    a = np.full(101, 10.0)
    xb = np.linspace(550, 650, 101)                           # B сдвинут: перекрытие 550…600 нм
    b = np.full(101, 4.0)
    d = P.spectrum_math("sub", xa, a, xb, b)
    assert np.isnan(d[:50]).all() and np.allclose(d[50:], 6.0)  # вне диапазона B — NaN
    t = P.spectrum_math("trans", xa, a, xa, b * 2.5)
    assert np.allclose(t, 100.0)
    od = P.spectrum_math("absorb", xa, a, xa, a * 10)
    assert np.allclose(od, 1.0)                                # пропускание 10 % → D = 1
    z = P.spectrum_math("div", xa, a, xa, np.zeros(101))
    assert np.isnan(z).all()
    assert np.allclose(P.spectrum_math("norm_max", xa, a * np.linspace(0, 2, 101)).max(), 1.0)
    assert np.allclose(P.spectrum_math("scale", xa, a, k=3), 30.0)
    with pytest.raises(ValueError):
        P.spectrum_math("sub", xa, a, None, b[:5], same_axis=True)


# ------------------------------------------------------------------ панорама: обрезка и выравнивание
def test_stitch_trim_and_equalize():
    x1 = np.linspace(500, 520, 201)
    x2 = np.linspace(515, 535, 201)
    truth = lambda x: 100 + 50 * np.sin(x / 3)                 # noqa: E731
    y1 = truth(x1)
    y2 = truth(x2) * 1.5                                     # второй кадр на 50 % ярче
    y2[:5] = 1e5                                             # испорченный край кадра
    x, y = P.stitch([(x1, y1), (x2, y2)], trim=5, equalize=True)
    assert x[0] >= x1[5] - 1e-9 and x[-1] <= x2[-6] + 1e-9   # края отброшены
    assert np.max(np.abs(y / truth(x) - 1)) < 0.03           # уровни совпали, выброса нет
    _, y_raw = P.stitch([(x1, y1), (x2, y2)])
    assert np.max(np.abs(y_raw / truth(np.linspace(500, 535, y_raw.size)) - 1)) > 0.2


def test_comment_goes_into_measurement(inst):
    inst.settings.comment = "  кристалл №3 "
    m = inst.acquire()
    assert m.comment == "кристалл №3" and m.meta()["comment"] == "кристалл №3"
