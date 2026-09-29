"""Драйвер монохроматора SOL instruments (MS7504i и др.) через SSPL_IM.dll.

SSPL_IM.dll — модуль самой SpectraSP (параметр CtrlDllName в SpectraSP.xml),
он загружает DevCtrl.dll и SOLCon.DLL из своего каталога. Адрес прибора и
интерфейс связи DevCtrl берёт из DevCtrl.ini; программа их не трогает.
Прототипы восстановлены по вызовам ctrl_* в SpectraSP.exe и телу функций
SSPL_IM.dll (docs/API_SSPL_IM.md).

Особенность SSPL: большинство функций (включая чтение длины волны) внутри
ждут окончания предыдущего движения. Поэтому о завершении движения узнаём
через GetStatus(), который не блокирует.
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import POINTER, c_bool, c_double, c_int32, c_void_p
from typing import Optional

import numpy as np

from .base import (MECH_GRATING, MECH_MIRROR, MECH_SLIT, MECH_TURRET, MECH_TITLES, DeviceError,
                   Monochromator, MonoInfo)
from .winutil import DllDirectory, load_dll, pump_messages, stdcall_function

log = logging.getLogger(__name__)

SSPL_DLL = "SSPL_IM.dll"

# имя: (тип результата, аргументы, байт аргументов по `ret N`)
PROTOTYPES = {
    "Init": (c_int32, [c_void_p, c_int32], 8),
    "Close": (None, [], 0),
    "GetStatus": (c_int32, [], 0),
    "GetDeviceTypeAndSerial": (c_bool, [POINTER(c_int32), POINTER(c_int32)], 8),
    "GetWaveLengthByTurret": (c_double, [c_int32], 4),
    "SetWaveLengthByTurret": (c_int32, [c_int32, c_double, c_int32, c_int32], 20),
    "GetActiveGratingByTurret": (c_int32, [c_int32], 4),
    "SetActiveGratingByTurret": (c_bool, [c_int32, c_int32], 8),
    "GetGratingCountByTurret": (c_int32, [c_int32], 4),
    "GetWLRangeByTurret": (c_bool, [c_int32, c_int32, POINTER(c_double), POINTER(c_double)], 16),
    "GetSlitWidth": (c_bool, [c_int32, POINTER(c_double)], 8),
    "SetSlitWidth": (c_bool, [c_int32, c_double], 12),
    "GetMirrorState": (c_bool, [c_int32, POINTER(c_int32)], 8),
    "SetMirrorState": (c_bool, [c_int32, c_int32], 8),
    "GetSignalSourceState": (c_int32, [c_int32], 4),
    "SetSignalSourceState": (c_int32, [c_int32, c_int32], 8),
    "GetCalibrationByTurret": (c_int32, [c_int32, c_double, c_int32, c_double, c_int32, POINTER(c_double)], 32),
    # сервисные функции (режим «Тест» окна DevCtrl); положения — в шагах двигателей
    "GetGratingStepByTurret": (c_bool, [c_int32, c_int32, POINTER(c_int32)], 12),   # 2-й аргумент не используется
    "SetGratingStepByTurret": (c_bool, [c_int32, c_int32], 8),
    "MoveWLByStepsByTurret": (c_bool, [c_int32, c_int32], 8),
    "ResetGratingByTurret": (c_int32, [c_int32], 4),
    "GetTurretStep": (c_bool, [c_int32, POINTER(c_int32)], 8),
    "SetTurretStepByTurret": (c_bool, [c_int32, c_int32], 8),
    "ResetTurret": (c_bool, [], 0),
    "GetSlitStep": (c_bool, [c_int32, POINTER(c_int32)], 8),
    "SetSlitStep": (c_bool, [c_int32, c_int32], 8),
    "MoveSlitsBySteps": (c_bool, [c_int32, c_int32], 8),
    "ResetSlit": (c_bool, [c_int32], 4),
    "GetMirrorStep": (c_bool, [c_int32, POINTER(c_int32)], 8),
    "SetMirrorStep": (c_bool, [c_int32, c_int32], 8),
    "ResetMirror": (c_bool, [c_int32], 4),
    "ShowWnd": (None, [c_int32, c_int32], 8),       # → DevCtrl.ShowDeviceWindowEx(0)
}

STATUS_IDLE, STATUS_BUSY, STATUS_NOT_INIT = 0, 1, 2
SSS_UNKNOWN, SSS_ON, SSS_OFF = 0, 1, 2     # TSignalSrcState: ON — шторка открыта
TURRET = 0


class SsplMonochromator(Monochromator):
    def __init__(self, spectrasp_dir: str, info_template: Optional[MonoInfo] = None):
        self.dir = spectrasp_dir
        self.info = info_template or MonoInfo()
        self._dll = None
        self._f = {}
        self._open = False

    def _fn(self, name):
        try:
            return self._f[name]
        except KeyError:
            raise DeviceError(f"В {SSPL_DLL} нет функции {name}") from None

    @property
    def is_open(self) -> bool:
        return self._open

    def _require_open(self) -> None:
        if not self._open:
            raise DeviceError("Монохроматор не подключён")

    # ------------------------------------------------------------ открытие
    def open(self) -> MonoInfo:
        if self._open:
            return self.info
        with DllDirectory(self.dir):
            if self._dll is None:
                self._dll = load_dll(os.path.join(self.dir, SSPL_DLL))
                for name, (restype, argtypes, nbytes) in PROTOTYPES.items():
                    try:
                        self._f[name] = stdcall_function(self._dll, name, restype, argtypes, nbytes)
                    except AttributeError:
                        log.warning("В %s нет функции %s", SSPL_DLL, name)
            log.info("Монохроматор: инициализация DevCtrl...")
            t0 = time.monotonic()
            ok = self._fn("Init")(None, 0)
            pump_messages()
            if not ok:
                raise DeviceError("Монохроматор не отвечает (SSPL_IM.Init вернула ошибку). "
                                  "Проверьте питание прибора и сетевое подключение.")
            log.info("Монохроматор инициализирован за %.1f с", time.monotonic() - t0)
        self._open = True
        dev_type, serial = c_int32(0), c_int32(0)
        if self._fn("GetDeviceTypeAndSerial")(ctypes.byref(dev_type), ctypes.byref(serial)):
            self.info.device_type = dev_type.value
            self.info.serial = str(serial.value)
        self._refresh_gratings()
        return self.info

    def _refresh_gratings(self) -> None:
        try:
            n = self._fn("GetGratingCountByTurret")(TURRET)
        except DeviceError:
            n = -1
        if n > 0 and len(self.info.gratings) != n:
            from .base import GratingInfo
            known = {g.index: g for g in self.info.gratings}
            self.info.gratings = [known.get(i, GratingInfo(i, 0.0)) for i in range(n)]
        try:
            self.info.wl_range = self.wavelength_range()
        except DeviceError:
            pass

    def close(self) -> None:
        # Библиотеку не выгружаем: SSPL_IM сама загрузила DevCtrl.dll, и её
        # выгрузка «на ходу» в SpectraSP никогда не выполняется. При повторном
        # подключении снова вызывается Init.
        if self._open:
            try:
                self._fn("Close")()
            except Exception:  # noqa: BLE001
                log.warning("SSPL_IM.Close завершилась с ошибкой", exc_info=True)
            self._open = False

    # ------------------------------------------------------------ состояние
    def status(self) -> int:
        self._require_open()
        return int(self._fn("GetStatus")())

    def is_busy(self) -> bool:
        return self.status() == STATUS_BUSY

    def wait_idle(self, timeout_s: float) -> None:
        t0 = time.monotonic()
        while True:
            st = self.status()
            if st == STATUS_IDLE:
                return
            if st == STATUS_NOT_INIT:
                raise DeviceError("Монохроматор не инициализирован")
            if time.monotonic() - t0 > timeout_s:
                raise DeviceError(f"Монохроматор не завершил движение за {timeout_s:.0f} с")
            pump_messages()
            time.sleep(0.1)

    # ------------------------------------------------------------ решётки и длина волны
    def get_wavelength(self) -> float:
        self._require_open()
        return float(self._fn("GetWaveLengthByTurret")(TURRET))

    def set_wavelength(self, wl_nm: float, wait: bool = True, timeout_s: float = 300.0) -> None:
        self._require_open()
        lo, hi = self.info.wl_range
        if hi > lo and not (lo <= wl_nm <= hi):
            raise DeviceError(f"Длина волны {wl_nm:g} нм вне диапазона решётки {lo:g}…{hi:g} нм")
        log.info("Монохроматор: установка %.3f нм", wl_nm)
        if not self._fn("SetWaveLengthByTurret")(TURRET, float(wl_nm), 0, -1):
            raise DeviceError(f"Монохроматор отказался устанавливать {wl_nm:g} нм")
        if wait:
            self.wait_idle(timeout_s)

    def wavelength_range(self) -> tuple:
        lo, hi = c_double(0), c_double(0)
        if not self._fn("GetWLRangeByTurret")(TURRET, 0, ctypes.byref(lo), ctypes.byref(hi)):
            raise DeviceError("Не удалось получить диапазон длин волн")
        return float(lo.value), float(hi.value)

    def get_grating(self) -> int:
        self._require_open()
        return int(self._fn("GetActiveGratingByTurret")(TURRET))

    def set_grating(self, index: int, wait: bool = True, timeout_s: float = 300.0) -> None:
        self._require_open()
        log.info("Монохроматор: смена решётки на №%d", index + 1)
        if not self._fn("SetActiveGratingByTurret")(TURRET, int(index)):
            raise DeviceError(f"Не удалось выбрать решётку №{index + 1}")
        if wait:
            self.wait_idle(timeout_s)
        try:
            self.info.wl_range = self.wavelength_range()
        except DeviceError:
            pass

    # ------------------------------------------------------------ щели, зеркало, шторка
    def get_slit(self, index: int) -> float:
        self._require_open()
        w = c_double(0)
        if not self._fn("GetSlitWidth")(int(index), ctypes.byref(w)):
            raise DeviceError(f"Не удалось прочитать ширину щели {index + 1}")
        return float(w.value)

    def set_slit(self, index: int, width_um: float) -> None:
        self._require_open()
        if not self._fn("SetSlitWidth")(int(index), float(width_um)):
            raise DeviceError(f"Не удалось установить ширину щели {index + 1}")

    def get_mirror(self, index: int) -> int:
        self._require_open()
        pos = c_int32(-1)
        if not self._fn("GetMirrorState")(int(index), ctypes.byref(pos)):
            raise DeviceError("Не удалось прочитать положение зеркала")
        return int(pos.value)

    def set_mirror(self, index: int, position: int) -> None:
        self._require_open()
        if not self._fn("SetMirrorState")(int(index), int(position)):
            raise DeviceError("Не удалось переключить зеркало")

    def get_shutter(self, index: int = 0) -> Optional[bool]:
        self._require_open()
        state = self._fn("GetSignalSourceState")(int(index))
        return {SSS_ON: True, SSS_OFF: False}.get(state)

    def set_shutter(self, is_open: bool, index: int = 0) -> None:
        self._require_open()
        if not self._fn("SetSignalSourceState")(int(index), SSS_ON if is_open else SSS_OFF):
            raise DeviceError("Не удалось переключить шторку")

    # ------------------------------------------------------------ калибровка
    def pixel_wavelengths(self, center_wl: float, center_pixel: float,
                          pixel_width_um: float, n_pixels: int) -> Optional[np.ndarray]:
        """Модель прибора из DevCtrl (то же, что «Автоматическая» калибровка SpectraSP)."""
        if not self._open or "GetCalibrationByTurret" not in self._f or n_pixels <= 0:
            return None
        arr = (c_double * n_pixels)()
        ok = self._f["GetCalibrationByTurret"](TURRET, float(center_wl), int(round(center_pixel)),
                                               float(pixel_width_um), int(n_pixels), arr)
        if not ok:
            return None
        wl = np.frombuffer(arr, dtype=np.float64).copy()
        if not np.all(np.isfinite(wl)) or np.ptp(wl) <= 0:
            return None
        return wl

    # ------------------------------------------------------------ сервис (шаги, сброс)
    def _mech_name(self, mech: str, index: int) -> str:
        if mech == MECH_SLIT and index < len(self.info.slits):
            return self.info.slits[index]
        return MECH_TITLES.get(mech, mech) + (f" {index + 1}" if mech in (MECH_SLIT, MECH_MIRROR) else "")

    def get_steps(self, mech: str, index: int = 0) -> int:
        self._require_open()
        pos = c_int32(0)
        if mech == MECH_GRATING:
            ok = self._fn("GetGratingStepByTurret")(TURRET, 0, ctypes.byref(pos))
        elif mech == MECH_TURRET:
            ok = self._fn("GetTurretStep")(TURRET, ctypes.byref(pos))
        elif mech == MECH_SLIT:
            ok = self._fn("GetSlitStep")(int(index), ctypes.byref(pos))
        elif mech == MECH_MIRROR:
            ok = self._fn("GetMirrorStep")(int(index), ctypes.byref(pos))
        else:
            raise DeviceError(f"Неизвестный механизм: {mech}")
        if not ok:
            raise DeviceError(f"Не удалось прочитать положение: {self._mech_name(mech, index)}")
        return int(pos.value)

    def _check_limit(self, mech: str, index: int, position: int) -> None:
        limit = self.info.step_limits.get((mech, index))
        if position < 0 or (limit and position > limit):
            raise DeviceError(f"Положение {position} вне допустимого диапазона 0…{limit or '?'} шагов")

    def set_steps(self, mech: str, index: int, position: int, timeout_s: float = 300.0) -> None:
        self._require_open()
        self._check_limit(mech, index, position)
        log.info("Сервис: %s → %d шагов", self._mech_name(mech, index), position)
        fn = {MECH_GRATING: lambda: self._fn("SetGratingStepByTurret")(TURRET, int(position)),
              MECH_TURRET: lambda: self._fn("SetTurretStepByTurret")(TURRET, int(position)),
              MECH_SLIT: lambda: self._fn("SetSlitStep")(int(index), int(position)),
              MECH_MIRROR: lambda: self._fn("SetMirrorStep")(int(index), int(position))}.get(mech)
        if fn is None:
            raise DeviceError(f"Неизвестный механизм: {mech}")
        if not fn():
            raise DeviceError(f"Не удалось переместить: {self._mech_name(mech, index)}")
        self.wait_idle(timeout_s)

    def move_steps(self, mech: str, index: int, delta: int, timeout_s: float = 300.0) -> None:
        self._require_open()
        log.info("Сервис: %s сдвиг на %+d шагов", self._mech_name(mech, index), delta)
        if mech in (MECH_GRATING, MECH_SLIT):
            self._check_limit(mech, index, self.get_steps(mech, index) + int(delta))
        if mech == MECH_GRATING:
            ok = self._fn("MoveWLByStepsByTurret")(TURRET, int(delta))
        elif mech == MECH_SLIT:
            ok = self._fn("MoveSlitsBySteps")(int(index), int(delta))
        else:
            self.set_steps(mech, index, self.get_steps(mech, index) + int(delta), timeout_s)
            return
        if not ok:
            raise DeviceError(f"Не удалось сдвинуть: {self._mech_name(mech, index)}")
        self.wait_idle(timeout_s)

    def reset(self, mech: str, index: int = 0, timeout_s: float = 300.0) -> None:
        self._require_open()
        log.info("Сервис: сброс (поиск нуля) — %s", self._mech_name(mech, index))
        if mech == MECH_GRATING:
            ok = self._fn("ResetGratingByTurret")(TURRET) != 0
        elif mech == MECH_TURRET:
            ok = self._fn("ResetTurret")()
        elif mech == MECH_SLIT:
            ok = self._fn("ResetSlit")(int(index))
        elif mech == MECH_MIRROR:
            ok = self._fn("ResetMirror")(int(index))
        else:
            raise DeviceError(f"Неизвестный механизм: {mech}")
        if not ok:
            raise DeviceError(f"Сброс не выполнен: {self._mech_name(mech, index)}")
        self.wait_idle(timeout_s)
        if mech in (MECH_GRATING, MECH_TURRET):
            try:
                self.info.wl_range = self.wavelength_range()
            except DeviceError:
                pass

    def show_native_window(self) -> None:
        """Штатное окно DevCtrl — то, что в SpectraSP открывает скрытая кнопка
        «Show spectra instrument window» (ctrl_ShowWnd → SSPL.ShowWnd(-1, -1))."""
        self._require_open()
        self._fn("ShowWnd")(-1, -1)
        pump_messages()
