"""«Защита от дурака»: команды, опасные для приборов, отклоняются или выполняются безопасно (имитатор)."""
import threading
import time

import pytest

from spectra_better.config import AppSettings
from spectra_better.hw.base import (MECH_GRATING, MECH_MIRROR, MECH_SLIT, MECH_TURRET, CameraState, DeviceError,
                                    warm_up_camera)
from spectra_better.instrument import Instrument


@pytest.fixture
def inst(spectrasp_dir):
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s.exposure_ms = 5
    i = Instrument(s, simulate=True)
    i.connect_camera()
    i.connect_mono()
    i.camera.time_scale = 0.01            # охлаждение/отогрев имитатора в 100 раз быстрее
    yield i
    i.shutdown()


# ------------------------------------------------------------------ монохроматор
def test_bad_mono_commands_rejected_before_device(inst):
    wl0 = inst.mono_wl
    for bad in (lambda: inst.set_grating(7), lambda: inst.set_grating(-1),
                lambda: inst.set_mirror(5), lambda: inst.set_slit(0, -10),
                lambda: inst.set_slit(0, inst.slit_max(0) + 1), lambda: inst.set_slit(3, 100),
                lambda: inst.goto_wavelength(float("nan")), lambda: inst.goto_wavelength(-5)):
        with pytest.raises(DeviceError):
            bad()
    assert inst.mono_wl == wl0
    assert inst.set_slit(0, inst.slit_max(0)) == pytest.approx(inst.slit_max(0))    # ровно предел — можно


def test_slit_limit_from_device_file(inst):
    # настоящий драйвер получает пределы из MS7504_20126.cfg: (4262 − 299) шагов × 0,5 мкм
    tpl = inst._mono_template()
    assert tpl.slit_max_um[0] == 1981.0 and tpl.step_limits[(MECH_MIRROR, 0)] == 48700


def test_service_steps_need_known_limit(inst):
    limits = inst.mono.info.step_limits
    del limits[(MECH_MIRROR, 0)]
    with pytest.raises(DeviceError, match="неизвестен предел"):
        inst.service_set_steps(MECH_MIRROR, 0, 100)
    with pytest.raises(DeviceError, match="неизвестен предел"):
        inst.service_move_steps(MECH_MIRROR, 0, 10)
    inst.service_reset(MECH_MIRROR, 0)                      # сброс по датчику — можно
    turret = limits[(MECH_TURRET, 0)]
    with pytest.raises(DeviceError, match="вне допустимого"):
        inst.service_set_steps(MECH_TURRET, 0, turret + 1)
    pos = inst.service_snapshot()["steps"][(MECH_SLIT, 1)]
    with pytest.raises(DeviceError, match="вне допустимого"):
        inst.service_move_steps(MECH_SLIT, 1, limits[(MECH_SLIT, 1)] - pos + 1)
    with pytest.raises(DeviceError, match="вне допустимого"):
        inst.service_move_steps(MECH_GRATING, 0, -10_000_000)
    assert inst.service_snapshot()["steps"][(MECH_SLIT, 1)] == pos


def test_panorama_has_exclusive_control(inst):
    result = {}
    started = threading.Event()
    release = threading.Event()

    def panorama():
        with inst.mono_exclusive("панорама"):
            inst.goto_wavelength(510)                       # владелец может двигать
            started.set()
            release.wait(5)
            result["own"] = inst.mono_wl

    t = threading.Thread(target=panorama)
    t.start()
    assert started.wait(10)
    with pytest.raises(DeviceError, match="панорама"):
        inst.goto_wavelength(520)                           # ручная команда из другого потока
    with pytest.raises(DeviceError, match="панорама"):
        inst.set_shutter(False)
    assert inst.get_slit(0) > 0                             # чтение не мешает
    release.set()
    t.join(10)
    assert result["own"] == pytest.approx(510)
    inst.goto_wavelength(520)                               # после панорамы — снова можно
    assert inst.mono_wl == pytest.approx(520)


def test_disconnect_waits_for_motion_before_close(inst):
    mono = inst.mono
    mono.speed = 20.0                                       # движение ~2,5 с
    closed_while_busy = []
    orig_close = mono.close

    def close():
        closed_while_busy.append(mono.is_busy())
        orig_close()
    mono.close = close
    inst.mono_thread.call(mono.set_wavelength, 550.0, False)
    assert mono.is_busy()
    inst.disconnect(warm_up=False)
    assert closed_while_busy == [False]                     # Close — только после остановки
    assert mono._wl == pytest.approx(550.0)
    assert not inst.mono_connected
    with pytest.raises(DeviceError):
        mono.set_wavelength(600.0)                          # после Close команды в прибор не уходят


# ------------------------------------------------------------------ камера
def test_disconnect_warms_up_cold_ccd(inst):
    inst.set_cooling(True, -20)
    deadline = time.monotonic() + 10
    while inst.cam_thread.call(inst.camera.get_state).temperature > -15 and time.monotonic() < deadline:
        time.sleep(0.05)
    said = []
    inst.disconnect(warm_up=True, say=said.append)
    assert inst.camera._temp >= 1.0                         # закрыта уже отогретой
    assert any("Отогрев ПЗС" in s for s in said)


def test_warm_up_waits_for_temperature_not_stale_status():
    # камера сразу после смены уставки ещё сообщает «стабилизировано» (на старой уставке)
    temps = iter([-20, -20, -15, -9, -4, 0, 1, 2])
    calls = []

    def get_state():
        return CameraState(temperature=next(temps), cooling_state=2)
    ok = warm_up_camera(get_state, lambda on, t: calls.append((on, t)), poll_s=0.001)
    assert ok and calls == [(True, 2)]
    assert next(temps) == 2                                 # ждали до +1 °C, а не вышли на первом «2»


def test_warm_up_can_be_cancelled_and_skipped():
    cancel = threading.Event()
    cancel.set()
    calls = []
    assert not warm_up_camera(lambda: CameraState(temperature=-30), lambda on, t: calls.append(t),
                              cancel=cancel, poll_s=0.01)
    assert calls == [2]                                     # уставка +2 °C всё равно отправлена
    assert warm_up_camera(lambda: CameraState(temperature=5), lambda on, t: calls.append(t))
    assert calls == [2]                                     # тёплую ПЗС не трогаем
