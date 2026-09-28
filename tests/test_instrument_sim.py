"""Сквозные проверки на имитаторах (без оборудования)."""
import threading
import time

import pytest

from spectra_better.config import ACQ_IMAGE, AppSettings
from spectra_better.hw.base import DeviceError
from spectra_better.hw.worker import DeviceThread
from spectra_better.instrument import Instrument
from spectra_better.processing import find_peaks, stitch


@pytest.fixture
def inst(spectrasp_dir):
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s.exposure_ms = 5
    s.readout_speed = 500
    i = Instrument(s, simulate=True)
    i.connect_camera()
    i.connect_mono()
    yield i
    i.shutdown()


def test_worker_thread_runs_in_one_thread():
    t = DeviceThread("t")
    ids = {t.call(threading.get_ident) for _ in range(5)}
    assert len(ids) == 1 and threading.get_ident() not in ids
    with pytest.raises(ZeroDivisionError):
        t.call(lambda: 1 / 0)
    t.stop()


def test_defaults_imported_from_spectrasp(inst):
    assert inst.camera_ip() == "192.168.240.181"
    assert inst.mono_address().startswith("192.168.120.120")
    assert (inst.settings.rows_top, inst.settings.rows_bottom) == (282, 311)
    assert inst.settings.wl_presets[0] == 290.0


def test_acquire_background_and_lines(inst):
    inst.goto_wavelength(546.0)
    assert inst.mono_wl == pytest.approx(546.0)
    bg = inst.acquire(background=True, n=2)
    assert bg.is_background and inst.has_background()
    assert inst.mono.shutter_open                         # шторка возвращена
    m = inst.acquire(n=3)
    assert m.background_used and m.x_unit == "nm" and m.y.size == 2048
    assert m.hardware_binning
    peaks = find_peaks(m.x, m.y)
    assert any(abs(p.x - 546.074) < 0.05 for p in peaks)  # линия Hg
    meta = m.meta()
    assert meta["background_subtracted"] == "yes" and "center_wavelength_nm" in meta


def test_image_mode(inst):
    inst.settings.acq_mode = ACQ_IMAGE
    m = inst.acquire()
    assert m.image.shape == (506, 2048)
    assert m.y.size == 2048


def test_abort_long_exposure(inst):
    inst.settings.exposure_ms = 20000
    t0 = time.monotonic()
    threading.Timer(0.3, inst.abort).start()
    with pytest.raises(DeviceError):
        inst.acquire()
    assert time.monotonic() - t0 < 5


def test_grating_change_and_panorama(inst):
    inst.set_grating(1)
    assert inst.mono_grating == 1
    plan = inst.panorama_plan(400, 480, 0.15)
    assert plan[0] > 400 and plan[-1] < 480 + 60 and len(plan) >= 2
    segs = []
    for c in plan:
        inst.goto_wavelength(c)
        m = inst.acquire()
        segs.append((m.x, m.y))
    x, y = stitch(segs)
    assert x[0] <= 400.5 and x[-1] >= 479.5


def test_out_of_range_wavelength(inst):
    with pytest.raises(DeviceError):
        inst.goto_wavelength(5000)
