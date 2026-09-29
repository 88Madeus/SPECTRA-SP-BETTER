"""Имитаторы камеры и монохроматора.

Нужны для проверки программы без оборудования (ключ --simulate) и для тестов.
Имитируется: спектр газоразрядной лампы (Hg, Ar, Ne) через щель, профиль
полосы на матрице, темновой ток в зависимости от температуры, шумы, насыщение,
космические частицы, время считывания, работа шторки и зеркала.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Dict, Optional

import numpy as np

from ..calibration import GratingGeometry
from ..reference_lines import LINES
from .base import (MECH_GRATING, MECH_MIRROR, MECH_SLIT, MECH_TURRET, AcqSettings, Camera, CameraInfo,
                   CameraState, DeviceError, GratingInfo, Monochromator, MonoInfo)

DEFAULT_GEOMETRIES = {
    0: GratingGeometry(1800, 754.4, 0.0683971, 0.04),
    1: GratingGeometry(600, 754.4, 0.0683971, 0.04),
    2: GratingGeometry(1800, 754.4, 0.0683971, 0.04),
    3: GratingGeometry(600, 754.4, 0.0683971, 0.04),
}


class SimMonochromator(Monochromator):
    def __init__(self, geometries: Optional[Dict[int, GratingGeometry]] = None,
                 info: Optional[MonoInfo] = None, speed_nm_s: float = 200.0, time_scale: float = 1.0):
        self.geometries = geometries or DEFAULT_GEOMETRIES
        self.info = info or MonoInfo()
        if not self.info.gratings:
            self.info.gratings = [GratingInfo(i, g.lines_per_mm) for i, g in sorted(self.geometries.items())]
        if not self.info.slits:
            self.info.slits = ["Входная щель", "Выходная щель (боковая)"]
        if not self.info.mirror_positions:
            self.info.mirror_positions = ["Верх (ПЗС)", "Боковой (щель)"]
        self.info.model = self.info.model or "MS7504i (имитация)"
        self.info.serial = self.info.serial or "SIM"
        self.speed = speed_nm_s
        self.time_scale = time_scale
        self._lock = threading.Lock()
        self._open = False
        self._wl = 500.0
        self._target = 500.0
        self._grating = 0
        self._move_end = 0.0
        self.slits = [50.0, 50.0]
        self.mirror = 0
        self.shutter_open = True
        # положения шаговых двигателей (для сервисного окна)
        self.steps = {(MECH_TURRET, 0): 9528, (MECH_SLIT, 0): 20, (MECH_SLIT, 1): 600,
                      (MECH_MIRROR, 0): 2124}
        if not self.info.step_limits:
            self.info.step_limits = {(MECH_GRATING, 0): 960000, (MECH_SLIT, 0): 4262, (MECH_SLIT, 1): 4262,
                                     (MECH_MIRROR, 0): 48700, (MECH_TURRET, 0): 100000}

    @property
    def is_open(self) -> bool:
        return self._open

    def _range_for(self, grating: int):
        g = self.geometries.get(grating)
        if g is None:
            return 0.0, 2000.0
        max_wl = 2 * 1e6 / g.lines_per_mm * math.cos(g.half_deviation_rad) * 0.95
        return 0.0, round(min(max_wl, 2500.0), 1)

    def open(self) -> MonoInfo:
        time.sleep(0.3 * self.time_scale)
        self._open = True
        self.info.wl_range = self._range_for(self._grating)
        return self.info

    def close(self) -> None:
        self._open = False

    def _req(self):
        if not self._open:
            raise DeviceError("Монохроматор не подключён")

    def is_busy(self) -> bool:
        self._req()
        return time.monotonic() < self._move_end

    def wait_idle(self, timeout_s: float) -> None:
        t0 = time.monotonic()
        while self.is_busy():
            if time.monotonic() - t0 > timeout_s:
                raise DeviceError("Таймаут движения (имитация)")
            time.sleep(0.02)
        self._wl = self._target

    def get_wavelength(self) -> float:
        self._req()
        self.wait_idle(600)
        return self._wl

    def set_wavelength(self, wl_nm: float, wait: bool = True, timeout_s: float = 300.0) -> None:
        self._req()
        lo, hi = self.info.wl_range
        if not lo <= wl_nm <= hi:
            raise DeviceError(f"Длина волны {wl_nm:g} нм вне диапазона {lo:g}…{hi:g} нм")
        self.wait_idle(timeout_s)
        dt = (0.2 + abs(wl_nm - self._wl) / self.speed) * self.time_scale
        self._target = float(wl_nm)
        self._move_end = time.monotonic() + dt
        if wait:
            self.wait_idle(timeout_s)

    def get_grating(self) -> int:
        self._req()
        return self._grating

    def set_grating(self, index: int, wait: bool = True, timeout_s: float = 300.0) -> None:
        self._req()
        if index not in self.geometries:
            raise DeviceError(f"Нет решётки №{index + 1}")
        self.wait_idle(timeout_s)
        self._grating = index
        self.info.wl_range = self._range_for(index)
        self._move_end = time.monotonic() + 1.5 * self.time_scale
        if wait:
            self.wait_idle(timeout_s)

    def get_slit(self, index: int) -> float:
        self._req()
        return self.slits[index]

    def set_slit(self, index: int, width_um: float) -> None:
        self._req()
        self.slits[index] = float(max(0.0, min(2000.0, width_um)))

    def get_mirror(self, index: int) -> int:
        self._req()
        return self.mirror

    def set_mirror(self, index: int, position: int) -> None:
        self._req()
        self.mirror = int(position)

    def get_shutter(self, index: int = 0) -> Optional[bool]:
        self._req()
        return self.shutter_open

    def set_shutter(self, is_open: bool, index: int = 0) -> None:
        self._req()
        self.shutter_open = bool(is_open)

    def pixel_wavelengths(self, center_wl, center_pixel, pixel_width_um, n_pixels):
        g = self.geometries.get(self._grating)
        if g is None:
            return None
        return g.wavelengths(center_wl, center_pixel, pixel_width_um, n_pixels)


    # --- сервисные операции
    def _grating_steps(self) -> int:
        return int(round(self._wl * 1000))           # условная связь шагов с длиной волны

    def get_steps(self, mech: str, index: int = 0) -> int:
        self._req()
        if mech == MECH_GRATING:
            self.wait_idle(600)
            return self._grating_steps()
        if (mech, index) not in self.steps:
            raise DeviceError("Нет такого механизма")
        return self.steps[(mech, index)]

    def set_steps(self, mech: str, index: int, position: int, timeout_s: float = 300.0) -> None:
        self._req()
        limit = self.info.step_limits.get((mech, index))
        if position < 0 or (limit and position > limit):
            raise DeviceError(f"Положение {position} вне диапазона 0…{limit}")
        if mech == MECH_GRATING:
            self.set_wavelength(position / 1000.0, True, timeout_s)
            return
        if (mech, index) not in self.steps:
            raise DeviceError("Нет такого механизма")
        time.sleep(0.2 * self.time_scale)
        self.steps[(mech, index)] = int(position)

    def move_steps(self, mech: str, index: int, delta: int, timeout_s: float = 300.0) -> None:
        self.set_steps(mech, index, self.get_steps(mech, index) + int(delta), timeout_s)

    def reset(self, mech: str, index: int = 0, timeout_s: float = 300.0) -> None:
        self._req()
        time.sleep(0.5 * self.time_scale)
        if mech == MECH_GRATING:
            self._wl = self._target = 0.0
        elif (mech, index) in self.steps:
            self.steps[(mech, index)] = 0
        else:
            raise DeviceError("Нет такого механизма")

    def show_native_window(self) -> None:
        raise DeviceError("В режиме имитации штатного окна DevCtrl нет")


class SimCamera(Camera):
    WIDTH, HEIGHT, PIXEL_UM = 2048, 506, 12.0

    def __init__(self, mono: Optional[SimMonochromator] = None, time_scale: float = 1.0,
                 seed: Optional[int] = None, true_center_pixel: float = 1024.0):
        self.mono = mono
        self.time_scale = time_scale
        self.rng = np.random.default_rng(seed)
        self.true_center_pixel = true_center_pixel
        self.info = CameraInfo()
        self._open = False
        self._settings: Optional[AcqSettings] = None
        self._abort = threading.Event()
        self._temp = 22.0
        self._target: Optional[float] = None
        self._t_last = time.monotonic()
        self.stripe_center, self.stripe_sigma = 297.0, 9.0
        self.lamp = {**{wl: i * 30000 for wl, i in LINES["Hg (ртуть)"]},
                     **{wl: i * 12000 for wl, i in LINES["Ar (аргон)"]},
                     **{wl: i * 9000 for wl, i in LINES["Ne (неон)"]}}
        self.continuum = 40.0          # фон лампы, отсчётов/с на пиксель полосы

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> CameraInfo:
        time.sleep(0.2 * self.time_scale)
        self._open = True
        self.info = CameraInfo(model="PSI CCD (имитация)", serial="SIM-160082", width=self.WIDTH,
                               height=self.HEIGHT, pixel_width_um=self.PIXEL_UM, pixel_height_um=self.PIXEL_UM,
                               readout_speeds=[125, 250, 500], temp_min=-50, temp_max=35, cooling_type=3,
                               max_bin_x=self.WIDTH, max_bin_y=self.HEIGHT, min_exposure=10, max_exposure=10000,
                               firmware="имитация", dll_name="simulated")
        return self.info

    def close(self, warm_up: bool = True) -> None:
        self._open = False

    def _req(self):
        if not self._open:
            raise DeviceError("Камера не подключена")

    def _update_temp(self) -> None:
        now = time.monotonic()
        dt = (now - self._t_last) / max(self.time_scale, 1e-3)
        self._t_last = now
        goal = self._target if self._target is not None else 22.0
        self._temp += (goal - self._temp) * (1 - math.exp(-dt / 20.0))

    def get_state(self) -> CameraState:
        self._req()
        self._update_temp()
        stable = self._target is not None and abs(self._temp - self._target) < 0.5
        return CameraState(temperature=round(self._temp), temperature2=None,
                           cooling_state=2 if stable else (1 if self._target is not None else 0),
                           fan_on=True)

    def set_cooling(self, enabled: bool, target_c: int) -> None:
        self._req()
        self._target = float(target_c) if enabled else None

    def configure(self, settings: AcqSettings) -> None:
        self._req()
        for r in settings.regions:
            r.validate(self.WIDTH, self.HEIGHT)
        self._settings = settings

    def abort(self) -> None:
        self._abort.set()

    def _readout_time(self) -> float:
        s = self._settings
        speed_hz = max(1, s.readout_speed or 250) * 1000.0
        r = s.regions[0]
        return r.out_pixels / speed_hz + r.height * 20e-6

    def _photon_rate(self) -> np.ndarray:
        """Поток на столбец (отсчётов/с) для одной строки в центре полосы."""
        cols = np.arange(self.WIDTH, dtype=float)
        m = self.mono
        if m is None or not m._open or not m.shutter_open or m.mirror != 0:
            return np.zeros(self.WIDTH)
        wl = m.geometries[m._grating].wavelengths(m._wl, self.true_center_pixel, self.PIXEL_UM, self.WIDTH)
        disp = abs(wl[-1] - wl[0]) / (self.WIDTH - 1)
        slit_px = max(1.2, m.slits[0] / self.PIXEL_UM)
        sigma = slit_px / 2.355
        rate = np.full(self.WIDTH, self.continuum * (m.slits[0] / 50.0))
        lo, hi = wl.min(), wl.max()
        for line, amp in self.lamp.items():
            if lo - 1 <= line <= hi + 1:
                px = np.interp(line, wl, cols)
                rate += amp * (m.slits[0] / 50.0) / (sigma * math.sqrt(2 * math.pi)) * \
                    np.exp(-0.5 * ((cols - px) / sigma) ** 2) * (0.01 / max(disp, 1e-4)) ** 0.2
        return rate

    def acquire(self) -> np.ndarray:
        self._req()
        if self._settings is None:
            raise DeviceError("Параметры экспозиции не заданы")
        self._abort.clear()
        s = self._settings
        exp_s = s.exposure_us / 1e6
        total = (exp_s + self._readout_time()) * self.time_scale
        t_end = time.monotonic() + total
        while time.monotonic() < t_end:
            if self._abort.wait(min(0.05, max(0.0, t_end - time.monotonic()))):
                raise DeviceError("Экспозиция прервана")
        self._update_temp()
        r = s.regions[0]
        rows = np.arange(r.top, r.bottom + 1, dtype=float)
        profile = np.exp(-0.5 * ((rows - self.stripe_center) / self.stripe_sigma) ** 2)
        rate = self._photon_rate()[r.left:r.right + 1]
        signal = np.outer(profile, rate) * exp_s
        dark = 0.5 * 2 ** ((self._temp - 20.0) / 6.5) * exp_s
        img = self.rng.poisson(np.clip(signal + dark, 0, None)).astype(float)
        # космические частицы
        n_cr = self.rng.poisson(0.02 * exp_s * r.width * r.height / 1e4)
        for _ in range(n_cr):
            y, x = self.rng.integers(0, img.shape[0]), self.rng.integers(0, img.shape[1])
            img[y, x] += self.rng.uniform(2000, 20000)
        # аппаратное объединение
        rows_b = img.shape[0] // r.bin_y
        cols_b = img.shape[1] // r.bin_x
        img = img[:rows_b * r.bin_y, :cols_b * r.bin_x]
        img = img.reshape(rows_b, r.bin_y, cols_b, r.bin_x).sum(axis=(1, 3))
        img += 300.0 + self.rng.normal(0, 6.0, img.shape)   # смещение и шум считывания
        return np.clip(np.round(img), 0, 65535).astype(np.uint16)
