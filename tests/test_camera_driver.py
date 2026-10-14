"""Камера PSI: повторная передача параметров, когда драйвер «забыл» настройку съёмки."""
import pytest

from spectra_better.config import AppSettings
from spectra_better.hw.base import AcqSettings, DeviceError, spectrum_region
from spectra_better.hw.psi_camera import PsiCamera
from spectra_better.instrument import Instrument

NOT_INITED = -3900


class FakeDriver:
    """Поддельный psi_ccd2_ver*.dll: как на приборе, после SetCoolingOn/Off следующий
    StartWaitMeasure отвечает «DLL not inited», пока параметры съёмки не переданы заново."""

    def __init__(self):
        self.inited = False
        self.sent = 0                 # сколько раз переданы параметры (SetParameters)
        self.fail_next = 0            # столько следующих экспозиций завершатся ошибкой

    def functions(self):
        def set_params(h, p):
            self.sent += 1
            self.inited = True
            return 0

        def cooling(*a):
            self.inited = False
            return 0

        def start(h, frames, size):
            if not self.inited:
                return NOT_INITED
            if self.fail_next:
                self.fail_next -= 1
                return -1
            size._obj.value *= 2      # драйвер сообщает размер в байтах
            return 0
        return {
            "psiccd2_SetParameters": set_params,
            "psiccd2_SetAcqMode": lambda *a: 0,
            "psiccd2_SetRegions": lambda *a: 0,
            "psiccd2_GetParameters": lambda *a: 0,
            "psiccd2_SetCoolingOn": cooling,
            "psiccd2_SetCoolingOff": cooling,
            "psiccd2_StartWaitMeasure": start,
            "psiccd2_GetData": lambda *a: 0,
            "psiccd2_GetLastError": lambda: b"DLL not inited! Code: Unknown error: 0",
        }


@pytest.fixture
def cam():
    drv = FakeDriver()
    c = PsiCamera("C:/SpectraSP", "192.168.240.181")
    c._f = drv.functions()
    c._h = 0
    c.info.width, c.info.height = 2048, 506
    c.info.temp_min, c.info.temp_max = -50, 35
    c.drv = drv
    return c


ACQ = AcqSettings(10_000_000, 250, (spectrum_region(2048, 506, 280, 310, True),))


def test_parameters_resent_after_cooling_change(cam):
    cam.configure(ACQ)
    assert cam.acquire().shape == (1, 2048)
    sent = cam.drv.sent
    cam.configure(ACQ)                            # параметры не менялись — не передаются
    assert cam.drv.sent == sent
    cam.set_cooling(True, -5)                     # драйвер сбросил настройку съёмки
    cam.configure(ACQ)
    assert cam.drv.sent > sent                    # …и программа передала её заново
    assert cam.acquire().shape == (1, 2048)


def test_failed_exposure_forces_resend(cam):
    cam.configure(ACQ)
    cam.drv.inited = False                        # драйвер «забыл» настройку по другой причине
    with pytest.raises(DeviceError, match="-3900"):
        cam.acquire()
    sent = cam.drv.sent
    cam.configure(ACQ)
    assert cam.drv.sent > sent
    assert cam.acquire().shape == (1, 2048)


@pytest.fixture
def inst(spectrasp_dir):
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s.exposure_ms = 50
    i = Instrument(s, simulate=True)
    i.connect_camera()
    i.connect_mono()
    i.camera.time_scale = 0.01
    i.camera._update_temp = lambda: None
    i.settings.warm_up_on_close = False
    yield i
    i.shutdown()


def test_instrument_retries_frame_once_after_camera_error(inst):
    cam = inst.camera
    orig = cam.acquire
    calls = {"n": 0, "invalidated": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeviceError("Камера: ошибка экспозиции (код -3900): DLL not inited!")
        return orig()
    cam.acquire = flaky
    cam.invalidate = lambda: calls.__setitem__("invalidated", calls["invalidated"] + 1)
    m = inst.acquire(background=True)             # как в лаборатории: фон после смены уставки
    assert m.is_background and calls["n"] == 2 and calls["invalidated"] == 1


def test_instrument_does_not_retry_forever(inst):
    cam = inst.camera
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise DeviceError("Камера: ошибка экспозиции (код -1)")
    cam.acquire = broken
    with pytest.raises(DeviceError, match="код -1"):
        inst.acquire()
    assert calls["n"] == 2                        # одна повторная попытка, не больше
