import numpy as np
import pytest

from spectra_better import processing as P
from spectra_better.hw.base import (Region, exposure_to_unit_value, full_frame_region, spectrum_region,
                                    unit_value_to_us)


def gauss(x, c, s, a):
    return a * np.exp(-0.5 * ((x - c) / s) ** 2)


def test_exposure_units_like_spectrasp():
    assert exposure_to_unit_value(10) == (1, 10)
    assert exposure_to_unit_value(65535) == (1, 65535)
    assert exposure_to_unit_value(1_000_000) == (2, 1000)          # 1 с → 1000 мс (как в журнале)
    assert exposure_to_unit_value(30_000_000) == (2, 30000)
    assert exposure_to_unit_value(100_000_000) == (3, 100)
    assert exposure_to_unit_value(7_200_000_000) == (3, 7200)
    for us in (10, 5000, 1_000_000, 123_456_789):
        u, v = exposure_to_unit_value(us)
        assert abs(unit_value_to_us(u, v) - us) / us < 0.01


def test_regions():
    r = spectrum_region(2048, 506, 282, 311, True)
    assert (r.bin_y, r.out_rows, r.out_cols) == (30, 1, 2048)
    r.validate(2048, 506)
    r2 = spectrum_region(2048, 506, 282, 311, False)
    assert r2.out_rows == 30
    assert full_frame_region(2048, 506).out_pixels == 2048 * 506
    with pytest.raises(ValueError):
        Region(0, 0, 2048, 10).validate(2048, 506)
    with pytest.raises(ValueError):
        Region(0, 0, 2047, 10, 1, 4).validate(2048, 506)     # 11 строк не делятся на 4


def test_bin_rows_and_to_spectrum():
    img = np.arange(12, dtype=np.uint16).reshape(3, 4)
    assert np.allclose(P.bin_rows(img, 1, 2), img[1] + img[2])
    assert np.allclose(P.to_spectrum(img[:1]), img[0])
    assert np.allclose(P.to_spectrum(img), img.sum(axis=0))


def test_remove_spikes_keeps_lines():
    rng = np.random.default_rng(0)
    x = np.arange(2048.0)
    y = 500 + gauss(x, 700, 1.3, 20000) + gauss(x, 1500, 2.0, 3000) + rng.normal(0, 5, x.size)
    spiked = y.copy()
    spiked[1000] += 5000
    spiked[300] += 3000
    r = P.remove_spikes(spiked)
    assert abs(r[1000] - 500) < 50 and abs(r[300] - 500) < 50
    assert abs(r[700] - y[700]) < 1e-9 and abs(r[1500] - y[1500]) < 1e-9


def test_combine_rejects_cosmics():
    base = np.full(100, 1000.0)
    frames = [base + np.random.default_rng(i).normal(0, 3, 100) for i in range(5)]
    frames[2][40] += 20000
    avg = P.combine_frames(frames, "average", reject_cosmics=True)
    assert avg[40] < 1100
    s = P.combine_frames(frames, "sum", reject_cosmics=False)
    assert s[40] > 20000


def test_find_peaks_centroid_and_width():
    x = np.linspace(500, 520, 2001)
    y = 100 + gauss(x, 507.3217, 0.05, 5000) + gauss(x, 512.0, 0.08, 2000)
    peaks = P.find_peaks(x, y)
    pos = sorted(p.x for p in peaks)
    assert len(pos) == 2
    assert abs(pos[0] - 507.3217) < 0.002
    p0 = min(peaks, key=lambda p: abs(p.x - 507.32))
    assert abs(p0.fwhm - 2.3548 * 0.05) < 0.01


def test_stitch_overlap():
    f = lambda x: 1000 + 200 * np.sin(x / 7.0)
    x1 = np.linspace(400, 420, 500)
    x2 = np.linspace(415, 435, 500)
    gx, gy = P.stitch([(x1, f(x1)), (x2[::-1], f(x2[::-1]))])
    assert gx[0] == pytest.approx(400) and gx[-1] == pytest.approx(435, abs=0.05)
    assert np.max(np.abs(gy - f(gx))) < 1.0
