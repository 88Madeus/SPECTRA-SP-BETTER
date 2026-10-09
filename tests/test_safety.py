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


# ------------------------------------------------------------------ лазер на выходе синхронизации камеры
def test_sync_outputs_applied_like_spectrasp(inst):
    # после включения питания камера сообщает (0, 0); SpectraSP выставляет свои (0, 6 = LOGICAL 0)
    assert inst.camera.sync_out == (0, 6)
    assert not inst.laser_on


def test_laser_on_off_and_off_on_disconnect(inst):
    from spectra_better.hw.base import SYNC_LOGICAL_0, SYNC_LOGICAL_1
    inst.set_laser(True)
    assert inst.laser_on and inst.camera.sync_out[1] == SYNC_LOGICAL_1
    inst.acquire()                                        # съёмка не сбрасывает выход
    assert inst.camera.sync_out[1] == SYNC_LOGICAL_1
    cam = inst.camera
    inst.disconnect(warm_up=False)
    assert cam.sync_out[1] == SYNC_LOGICAL_0              # лазер выключен перед отключением


def test_laser_is_off_after_connect_even_if_saved_on(spectrasp_dir):
    from spectra_better.hw.base import SYNC_LOGICAL_0, SYNC_LOGICAL_1
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s.sync_out2 = SYNC_LOGICAL_1                          # «остался включённым» в файле настроек
    i = Instrument(s, simulate=True)
    try:
        i.connect_camera()
        assert i.camera.sync_out[1] == SYNC_LOGICAL_0 and not i.laser_on
    finally:
        i.shutdown()


def test_laser_active_low_and_bad_requests(inst):
    from spectra_better.hw.base import SYNC_LOGICAL_0, SYNC_LOGICAL_1
    inst.settings.laser_active_high = False
    inst.set_laser(True)
    assert inst.camera.sync_out[1] == SYNC_LOGICAL_0
    inst.set_laser(False)
    assert inst.camera.sync_out[1] == SYNC_LOGICAL_1
    for bad in (lambda: inst.set_sync_out(3, 0), lambda: inst.set_sync_out(1, 42)):
        with pytest.raises(DeviceError):
            bad()
    inst.settings.laser_output = 0
    with pytest.raises(DeviceError, match="не указано|Не указано"):
        inst.set_laser(True)


def test_laser_off_interrupts_long_exposure(inst):
    from spectra_better.hw.base import SYNC_LOGICAL_0
    inst.set_laser(True)
    inst.camera.time_scale = 1.0
    inst.settings.exposure_ms = 20000                      # 20-секундный кадр
    t = threading.Thread(target=lambda: pytest.raises(DeviceError, inst.acquire))
    t.start()
    time.sleep(0.5)
    t0 = time.monotonic()
    inst.set_laser(False)
    assert time.monotonic() - t0 < 5                       # не ждали конца кадра
    assert inst.camera.sync_out[1] == SYNC_LOGICAL_0
    t.join(10)


# ------------------------------------------------------------------ съёмка при смене температуры ПЗС
def _freeze(inst, temp):
    """Остановить «тепловую модель» имитатора и поставить температуру ПЗС вручную."""
    inst.camera._update_temp = lambda: None
    inst.settings.warm_up_on_close = False     # «замороженная» ПЗС не отогреется — не ждать при выходе
    inst.camera._temp = float(temp)
    inst.camera_state()


def test_no_acquisition_while_temperature_changes(inst):
    _freeze(inst, 22)
    inst.set_cooling(True, -10)
    inst.camera_state()
    assert "−10" in inst.temperature_changing() or "-10" in inst.temperature_changing()
    with pytest.raises(DeviceError, match="изменение температуры"):
        inst.acquire()
    with pytest.raises(DeviceError, match="изменение температуры"):
        inst.acquire(background=True)
    inst.camera._temp = -9.5                                # в пределах 1 °C от уставки
    inst.camera_state()
    assert inst.temperature_changing() == ""
    inst.acquire()


def test_unreachable_setpoint_does_not_block_forever(inst):
    _freeze(inst, -38)
    inst.set_cooling(True, -50)
    inst.camera_state()
    assert inst.temperature_changing()
    now = time.monotonic()
    inst._temp_hist.clear()
    for k in range(80):                                     # 2 минуты температура стоит на −38 °C
        inst._temp_hist.append((now - 120 + k * 1.5, -38.0))
    assert inst.temperature_changing() == "" and inst.temperature_stalled()
    inst.acquire()


def test_background_from_other_temperature_not_subtracted(inst):
    _freeze(inst, -20)
    inst.set_cooling(True, -20)
    inst.camera_state()
    inst.acquire(background=True)
    assert inst.has_background() and inst.acquire().background_used
    inst.set_cooling(True, -10)
    inst.camera._temp = -10.0
    inst.camera_state()
    assert "фон снят при -20" in inst.background_problem() and not inst.has_background()
    assert not inst.acquire().background_used              # тёмный ток при −10 °C другой — не вычитаем


def test_disconnect_forgets_setpoint(inst):
    _freeze(inst, 5)
    inst.set_cooling(True, 5)
    inst.disconnect(warm_up=False)
    assert inst.cooling_target is None


# ------------------------------------------------------------------ шторка (DevCtrl выполняет команду не сразу)
def test_shutter_state_confirmed_after_command(inst):
    assert inst.mono.shutter_delay > 0                     # имитатор, как DevCtrl, отвечает с задержкой
    inst.set_shutter(False)
    assert inst.get_shutter() is False                     # сразу после команды — уже новое состояние
    inst.set_shutter(True)
    assert inst.get_shutter() is True


def test_background_taken_only_with_closed_shutter(inst, monkeypatch):
    inst.settings.close_shutter_for_background = True
    assert inst.get_shutter() is True
    seen = []
    orig = inst._frame

    def frame(acq):
        inst.mono._apply_shutter()
        seen.append(inst.mono.shutter_open)
        return orig(acq)
    monkeypatch.setattr(inst, "_frame", frame)
    inst.acquire(background=True)
    assert seen and not any(seen)                          # ни одного кадра фона при открытой шторке
    assert inst.get_shutter() is True                      # после фона шторка снова открыта


def test_background_refused_if_shutter_does_not_close(inst, monkeypatch):
    import spectra_better.instrument as I
    monkeypatch.setattr(I, "SHUTTER_CONFIRM_S", 0.5)
    inst.settings.close_shutter_for_background = True
    monkeypatch.setattr(inst.mono, "set_shutter", lambda is_open, index=0: None)     # «заела»
    shots = []
    monkeypatch.setattr(inst, "_frame", lambda acq: shots.append(1))
    with pytest.raises(DeviceError, match="Шторка не закрылась"):
        inst.acquire(background=True)
    assert not shots and not inst.has_background()


def test_warns_once_when_shooting_with_closed_shutter(inst, caplog):
    import logging
    inst.set_shutter(False)
    with caplog.at_level(logging.WARNING, logger="spectra_better.instrument"):
        inst.acquire()
        inst.acquire()
        inst.acquire(background=True)                      # фон при закрытой шторке — норма
    warns = [r for r in caplog.records if "шторка монохроматора закрыта" in r.getMessage()]
    assert len(warns) == 1
    inst.set_shutter(True)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="spectra_better.instrument"):
        inst.acquire()
    assert not [r for r in caplog.records if "шторка" in r.getMessage()]


# ------------------------------------------------------------------ шторка по экспозиции (TTL)
def _signal(m):
    """Сигнал над уровнем смещения камеры."""
    import numpy as np
    return float(m.raw.max() - np.median(m.raw))


def _bright(inst):
    inst.settings.exposure_ms = 200
    inst.goto_wavelength(546.0)                            # линия ртути в спектре лампы имитатора


def test_shutter_ttl_mode_light_only_during_exposure(inst):
    assert inst.mono.info.shutter_ttl                      # в MS7504_20126.cfg: [Shutter1] TTL=1
    assert inst.shutter_ttl is False                       # после подключения — ручной режим (настройка)
    _bright(inst)
    inst.set_shutter(False)
    dark = _signal(inst.acquire())
    inst.set_shutter_ttl(True)
    assert inst.mono.shutter_ttl and inst.settings.shutter_ttl
    lit = _signal(inst.acquire())
    assert lit > dark * 20                                 # шторку открыл импульс экспозиции камеры
    with pytest.raises(DeviceError, match="по экспозиции"):
        inst.set_shutter(True)                             # вручную — только после смены режима
    inst.set_shutter_ttl(False)
    assert not inst.mono.shutter_ttl
    inst.set_shutter(True)


def test_background_in_ttl_mode_is_dark_and_mode_restored(inst):
    inst.settings.close_shutter_for_background = False    # даже так: по TTL шторка открылась бы на фон
    _bright(inst)
    inst.set_shutter_ttl(True)
    lit = _signal(inst.acquire())
    bg = _signal(inst.acquire(background=True))
    assert bg < lit / 20                                   # фон тёмный
    assert inst.shutter_ttl and inst.mono.shutter_ttl     # режим по экспозиции вернулся


def test_ttl_mode_applied_on_connect(spectrasp_dir):
    s = AppSettings()
    s.spectrasp_dir = spectrasp_dir
    s.shutter_ttl = True
    i = Instrument(s, simulate=True)
    try:
        i.connect_mono()
        assert i.shutter_ttl and i.mono.shutter_ttl
    finally:
        i.shutdown()


def test_ttl_mode_warns_without_exposure_output(inst, caplog):
    import logging
    inst.set_sync_out(1, 6)                                 # оба выхода — постоянный уровень
    with caplog.at_level(logging.WARNING, logger="spectra_better.instrument"):
        inst.set_shutter_ttl(True)
    assert any("сигнал экспозиции" in r.getMessage() for r in caplog.records)
